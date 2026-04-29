"""
sitemap_scout_stealth.py

Tier 2 (browser lock) sibling of `sitemap_scout`. Identical report shape;
identical categorization/nav-extraction logic. The difference is the fetch
layer: Patchright (stealth-patched Chromium) drives /sitemap.xml,
/sitemap_index.xml, /robots.txt and the homepage so Cloudflare / WAF
challenges that would 202 a plain `httpx` request resolve normally.

When to call this instead of the Tier 1 `sitemap_scout`:
  - Tier 1 returned 0 URLs because the sitemap probe got a 202/403/503
    challenge response.
  - The site is fronted by Cloudflare bot-fight or similar WAF that gates
    on TLS/UA/JS fingerprints rather than auth.

Why a separate endpoint instead of an opt-in flag:
  - Resource profile is fundamentally different. Tier 1 is async httpx, no
    lock, runs in parallel with other tasks. Tier 2 holds the browser_lock
    (one chromium at a time, 4GB RAM ceiling — see syslog OOM kill 2026-04-12
    for what happens otherwise) and runs strictly sequential with Surge.
  - Caller (Client HQ) routes on `mode: 'http' | 'stealth'` so observability
    can split metrics cleanly.

Strategy:
  1. Launch ephemeral Patchright Chromium (no persistent profile — sitemap
     scouts are anonymous, no login state to preserve).
  2. Fetch /sitemap.xml -> /sitemap_index.xml -> /robots.txt via the browser
     context. Cloudflare cookies set during the first request stick for the
     rest of the run.
  3. If sitemap found -> parse <loc> entries (recurse sitemap indexes,
     capped at MAX_SUBSITEMAPS) and feed to the same _categorize_url
     pipeline as Tier 1.
  4. If no sitemap -> render the homepage and BFS-crawl rendered DOM links
     to depth 2, max 200 pages, same-origin only.
  5. Extract main-nav URLs from the homepage's rendered HTML using the
     existing _extract_nav_urls helper.
  6. POST callback in the same shape as Tier 1, with one extra field:
     report["tier"] = "stealth".

Hard timeout: 90s wall-clock. If exceeded, the report is sent with an
error_message ("stealth scout timeout") and partial results (whatever URLs
have been collected so far).

Cost: $0 if no LLM cleanup needed, ~$0.01 if there are >0 unknowns. Same
budget as Tier 1.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse

# Re-use every piece of categorization / nav / callback machinery from the
# Tier 1 scout. Keeps the two tiers in lockstep — a regex tweak in Tier 1
# benefits Tier 2 automatically.
from tasks.sitemap_scout import (
    COLLAPSE_THRESHOLD,
    MAX_SUBSITEMAPS,
    MAX_TOTAL_URLS,
    USER_AGENT,
    _apply_parent_inheritance,
    _categorize_url,
    _extract_locs,
    _extract_nav_urls,
    _llm_classify_unknowns,
    _looks_like_sitemap_index,
    _send_callback,
)
from utils.browser import chromium_launch_args

logger = logging.getLogger("moonraker.sitemap_scout_stealth")

# ── Tunables (stealth-specific) ─────────────────────────────────────────
HARD_TIMEOUT_SECONDS = 90        # wall-clock cap for the whole scout
NAV_TIMEOUT_MS = 20_000          # per page.goto() timeout
WAIT_AFTER_NAV_MS = 1_500        # let Cloudflare challenge JS settle
CRAWL_MAX_PAGES = 200            # parity with Tier 1 MAX_CRAWL_PAGES
CRAWL_MAX_DEPTH = 2              # spec asks for depth 2 (Tier 1 uses 3)
SITEMAP_PROBE_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt")


# ── Browser fetch helpers ───────────────────────────────────────────────

async def _browser_fetch_text(page, url: str) -> tuple[int, str]:
    """Navigate to `url` via the browser context and return (status, body).

    The first navigation against a Cloudflare-protected origin tends to land
    on a 202 challenge page; the patchright driver passes the JS check and
    sets `cf_clearance` automatically. Subsequent calls reuse that cookie.

    For XML / robots.txt responses Chrome shows an inline pretty-printer
    rather than the raw text. We pull the raw bytes off the response object
    when possible, falling back to rendered innerText.
    """
    try:
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
        )
    except Exception as e:
        logger.debug(f"stealth fetch nav error {url}: {e}")
        return (0, "")

    if response is None:
        return (0, "")

    status = response.status

    # Brief settle so any Cloudflare interstitial JS finishes.
    try:
        await page.wait_for_timeout(WAIT_AFTER_NAV_MS)
    except Exception:
        pass

    # Prefer the raw response body for XML/text endpoints — Chrome's XML
    # viewer wraps content in <body><div id="webkit-xml-viewer-source-xml">
    # which would trip our parser. response.text() returns the original.
    body = ""
    try:
        body = await response.text()
    except Exception:
        # Some responses (e.g. binary 202 challenge) raise; fall back to
        # rendered text.
        try:
            body = await page.evaluate("document.documentElement.innerText")
        except Exception:
            body = ""

    return (status, body or "")


async def _discover_sitemap_via_browser(page, base_url: str) -> tuple[list[str], str]:
    """Return (sitemap_urls, source_label).

    Tries /sitemap.xml first, then /sitemap_index.xml, then parses
    /robots.txt for `Sitemap:` directives. Returns ([], "") if nothing is
    discoverable.
    """
    # Direct probes
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        url = base_url.rstrip("/") + path
        status, body = await _browser_fetch_text(page, url)
        if status == 200 and body and ("<urlset" in body or "<sitemapindex" in body):
            source = "sitemap_index.xml" if "<sitemapindex" in body else "sitemap.xml"
            return ([url], source)

    # robots.txt
    robots_url = base_url.rstrip("/") + "/robots.txt"
    status, body = await _browser_fetch_text(page, robots_url)
    if status == 200 and body:
        sitemaps = []
        for line in body.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                u = line.split(":", 1)[1].strip()
                if u:
                    sitemaps.append(u)
        if sitemaps:
            return (sitemaps, "robots.txt")

    return ([], "")


async def _walk_sitemaps_browser(page, sitemap_urls: list[str]) -> list[str]:
    """Recursively walk sitemap indexes via the browser. Capped at
    MAX_SUBSITEMAPS subsitemaps and MAX_TOTAL_URLS total locs."""
    queue = list(sitemap_urls)
    visited: set[str] = set()
    locs: list[str] = []
    walked = 0

    while queue and walked < MAX_SUBSITEMAPS and len(locs) < MAX_TOTAL_URLS:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        walked += 1

        status, body = await _browser_fetch_text(page, url)
        if status != 200 or not body:
            continue

        if _looks_like_sitemap_index(body):
            # Subsitemap URLs live in <loc> tags exactly like regular sitemaps
            for sub in _extract_locs(body):
                if sub not in visited:
                    queue.append(sub)
        else:
            for u in _extract_locs(body):
                locs.append(u)
                if len(locs) >= MAX_TOTAL_URLS:
                    break

    return locs


# ── Bounded rendered crawl fallback ─────────────────────────────────────

_HREF_ATTR_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))',
    re.IGNORECASE,
)


def _same_origin(url: str, root: str) -> bool:
    a, b = urlparse(url), urlparse(root)
    return (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())


def _extract_anchor_hrefs(html: str, page_url: str, root_url: str) -> list[str]:
    """Pull every same-origin href out of rendered HTML, resolving relative
    URLs against `page_url`. Strips fragments + query strings (we only care
    about page identity, not analytics params)."""
    out: list[str] = []
    for m in _HREF_ATTR_RE.finditer(html):
        href = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if not href:
            continue
        # Skip non-navigational hrefs
        if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        try:
            absolute = urljoin(page_url, href)
        except Exception:
            continue
        absolute, _ = urldefrag(absolute)
        # Strip query string for crawl identity (keeps catalog pages from
        # exploding into ?ref=... variants).
        absolute = absolute.split("?", 1)[0]
        if not _same_origin(absolute, root_url):
            continue
        out.append(absolute.rstrip("/"))
    return out


async def _bounded_crawl_browser(page, root_url: str) -> list[str]:
    """BFS crawl rendered DOM. Same-origin only, capped at CRAWL_MAX_PAGES
    URLs and CRAWL_MAX_DEPTH levels."""
    root_norm = root_url.rstrip("/")
    visited: set[str] = {root_norm}
    discovered: list[str] = [root_norm]
    queue: list[tuple[str, int]] = [(root_norm, 0)]

    while queue and len(discovered) < CRAWL_MAX_PAGES:
        url, depth = queue.pop(0)
        if depth >= CRAWL_MAX_DEPTH:
            continue

        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            )
            if response is None or response.status >= 400:
                continue
            await page.wait_for_timeout(WAIT_AFTER_NAV_MS)
            html = await page.content()
        except Exception as e:
            logger.debug(f"stealth crawl error {url}: {e}")
            continue

        for href in _extract_anchor_hrefs(html, url, root_url):
            if href in visited:
                continue
            visited.add(href)
            discovered.append(href)
            if len(discovered) >= CRAWL_MAX_PAGES:
                break
            queue.append((href, depth + 1))

    return discovered


# ── Main entry point ────────────────────────────────────────────────────

async def run_sitemap_scout_stealth(task_id, params, status_callback, env):
    """
    params: same as run_sitemap_scout (root_url, client_slug, callback_url)
    env:    AGENT_API_KEY, ANTHROPIC_API_KEY (optional)

    Caller MUST hold the browser_lock before invoking this — chromium memory
    pressure on the 4GB VPS can OOM-kill the container if a Surge audit and
    a stealth sitemap scout run concurrently.
    """
    root_url = (params.get("root_url") or "").strip().rstrip("/")
    client_slug = params.get("client_slug", "")
    callback_url = params.get("callback_url", "")
    agent_api_key = env.get("AGENT_API_KEY", "")
    anthropic_key = env.get("ANTHROPIC_API_KEY", "")

    if not root_url:
        await status_callback(task_id, "failed", "root_url required")
        return None

    started_at = datetime.now(timezone.utc)

    report = {
        "scout_version": "1.1",
        "tier": "stealth",
        "scanned_at": started_at.isoformat(),
        "root_url": root_url,
        "client_slug": client_slug,
        "sitemap_source": "",
        "total_pages": 0,
        "pages_by_category": {},
        "collapsed_categories": {},
        "nav_urls": [],
        "nav_extraction_method": None,
        "errors": [],
        "duration_seconds": 0,
    }

    async def _finalise(final_status: str, final_msg: str):
        report["duration_seconds"] = int(
            (datetime.now(timezone.utc) - started_at).total_seconds()
        )
        await status_callback(task_id, final_status, final_msg)
        if callback_url:
            try:
                await _send_callback(callback_url, agent_api_key, task_id, report)
            except Exception as e:
                logger.warning(f"stealth scout callback failed: {e}")
        return report

    # Late import keeps Patchright off the hot path for non-stealth callers
    # and lets the agent module-load even if patchright isn't installed.
    try:
        from patchright.async_api import async_playwright as _patchright_async_playwright
    except ImportError as e:
        report["errors"].append(f"patchright unavailable: {e}")
        return await _finalise("error", "patchright not installed")

    pw = None
    browser = None
    context = None
    try:
        async def _scout():
            nonlocal pw, browser, context
            await status_callback(task_id, "running", "Launching stealth browser...")
            pw = await _patchright_async_playwright().start()
            # Ephemeral browser (no persistent profile — sitemap scouts are
            # anonymous, no login state to preserve). Patchright's stealth
            # value is in the patched driver, so we get the WAF-bypass
            # benefits without needing /data/profiles/<id>.
            browser = await pw.chromium.launch(**chromium_launch_args(stealth=True))
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=USER_AGENT,
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = await context.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)

            # ── Step 1: Sitemap discovery via browser ──────────────────
            await status_callback(task_id, "running", "Discovering sitemap (stealth)...")
            sitemap_urls, source = await _discover_sitemap_via_browser(page, root_url)

            urls: list[str] = []
            if sitemap_urls:
                report["sitemap_source"] = source
                await status_callback(task_id, "running", f"Walking {len(sitemap_urls)} sitemap(s)...")
                urls = await _walk_sitemaps_browser(page, sitemap_urls)
                logger.info(f"stealth-sitemap {task_id[:12]}: {source} -> {len(urls)} URLs")
            else:
                report["sitemap_source"] = "crawl"
                await status_callback(task_id, "running", "No sitemap, rendered crawl...")
                urls = await _bounded_crawl_browser(page, root_url)
                logger.info(f"stealth-sitemap {task_id[:12]}: crawl -> {len(urls)} URLs")

            # ── Step 2: Extract nav from homepage (rendered HTML) ─────
            try:
                response = await page.goto(
                    root_url + "/",
                    wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS,
                )
                await page.wait_for_timeout(WAIT_AFTER_NAV_MS)
                hp_html = await page.content() if response else ""
                if hp_html:
                    nav_urls, nav_method = _extract_nav_urls(hp_html, root_url)
                    report["nav_urls"] = nav_urls
                    report["nav_extraction_method"] = nav_method
                    logger.info(
                        f"stealth-sitemap {task_id[:12]} nav: {nav_method} -> {len(nav_urls)} link(s)"
                    )
                else:
                    report["nav_extraction_method"] = "unsupported"
            except Exception as e:
                report["nav_extraction_method"] = "unsupported"
                report["errors"].append(f"nav extraction failed: {e}")

            if not urls:
                report["errors"].append(
                    "No URLs discovered (stealth sitemap missing and crawl returned 0)."
                )
                return "complete", "No URLs found"

            # ── Step 3: Dedupe + categorize (mirror Tier 1) ───────────
            seen: set[str] = set()
            uniq_urls: list[str] = []
            for u in urls:
                norm = u.rstrip("/").lower()
                if norm not in seen:
                    seen.add(norm)
                    uniq_urls.append(u)

            report["total_pages"] = len(uniq_urls)

            await status_callback(task_id, "running", f"Categorizing {len(uniq_urls)} URLs...")
            categorized: dict[str, list[str]] = {}
            unknowns: list[str] = []
            for u in uniq_urls:
                cat = _categorize_url(u)
                if cat == "_excluded":
                    continue
                if cat == "unknown":
                    unknowns.append(u)
                    continue
                categorized.setdefault(cat, []).append(u)

            categorized, unknowns = _apply_parent_inheritance(
                uniq_urls, categorized, unknowns
            )

            if unknowns:
                await status_callback(
                    task_id, "running", f"Classifying {len(unknowns)} ambiguous URLs..."
                )
                llm_results = await _llm_classify_unknowns(unknowns, anthropic_key)
                for u, cat in llm_results.items():
                    categorized.setdefault(cat, []).append(u)

            # Collapse oversized buckets exactly like Tier 1 so the CHQ
            # ingest path doesn't need to special-case stealth output.
            pages_by_category: dict = {}
            collapsed: dict = {}
            for cat, cat_urls in categorized.items():
                sorted_urls = sorted(set(cat_urls), key=lambda u: (urlparse(u).path or "/"))
                if len(sorted_urls) > COLLAPSE_THRESHOLD and cat in (
                    "blog_post", "blog_index", "bio", "location"
                ):
                    sample = sorted_urls[:COLLAPSE_THRESHOLD]
                    pages_by_category[cat] = [{"url": u} for u in sample]
                    collapsed[cat] = {
                        "count": len(sorted_urls),
                        "sample_count": len(sample),
                        "all_urls": sorted_urls,
                    }
                else:
                    pages_by_category[cat] = [{"url": u} for u in sorted_urls]

            report["pages_by_category"] = pages_by_category
            report["collapsed_categories"] = collapsed

            cat_summary = ", ".join(
                f"{k}={len(v)}" for k, v in sorted(pages_by_category.items())
            )
            logger.info(
                f"stealth-sitemap {task_id[:12]} complete: {report['total_pages']} pages, {cat_summary}"
            )
            return "complete", f"{report['total_pages']} pages categorized (stealth)"

        try:
            final_status, final_msg = await asyncio.wait_for(
                _scout(), timeout=HARD_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            report["errors"].append(
                f"stealth scout exceeded hard timeout ({HARD_TIMEOUT_SECONDS}s)"
            )
            return await _finalise(
                "error", f"timeout after {HARD_TIMEOUT_SECONDS}s"
            )

        return await _finalise(final_status, final_msg)

    except Exception as e:
        logger.exception(f"stealth-sitemap {task_id[:12]} crashed")
        report["errors"].append(f"Fatal: {str(e)[:300]}")
        return await _finalise("error", f"Failed: {str(e)[:200]}")
    finally:
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass
