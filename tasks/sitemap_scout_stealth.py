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
NAV_TIMEOUT_MS = 20_000          # per page.goto() timeout (sub-pages)
ROOT_NAV_TIMEOUT_MS = 30_000     # per page.goto() timeout for the very
                                 # first hit on the origin (CF / SiteGround
                                 # `sg-captcha: challenge` JS interstitials
                                 # commonly take 6-15s to clear)
WAIT_AFTER_NAV_MS = 1_500        # generic settle after each sub-page load
WAIT_AFTER_ROOT_NAV_MS = 4_000   # extra settle on the first paint so any
                                 # WAF JS-challenge cookie gets minted before
                                 # we ask for anchors
CRAWL_MAX_PAGES = 200            # parity with Tier 1 MAX_CRAWL_PAGES
CRAWL_MAX_DEPTH = 2              # spec asks for depth 2 (Tier 1 uses 3)
SITEMAP_PROBE_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt")
# Signals an interstitial / WAF challenge response that we need to retry
# with a longer settle. SiteGround returns 202 + `sg-captcha: challenge`
# header. Cloudflare returns 403/503 with cf-mitigated. Both clear once
# the JS challenge runs in the patchright-driven page.
_INTERSTITIAL_STATUS = (202, 403, 503)


# ── Browser fetch helpers ───────────────────────────────────────────────

async def _browser_fetch_text(page, url: str) -> tuple[int, str]:
    """Navigate to `url` via the browser context and return (status, body).

    Uses `wait_until="load"` so that meta-refresh / JS-redirect challenge
    chains (Cloudflare IUAM, SiteGround sg-captcha) follow through to the
    actual content. Once the warm-up step has minted the WAF session cookie,
    each call here resolves with status 200 and the real body.

    For XML / robots.txt responses Chrome shows an inline pretty-printer
    rather than the raw text. We pull the raw bytes off the response object
    when possible, falling back to rendered innerText.
    """
    try:
        response = await page.goto(
            url, wait_until="load", timeout=NAV_TIMEOUT_MS
        )
    except Exception as e:
        logger.debug(f"stealth fetch nav error {url}: {e}")
        return (0, "")

    if response is None:
        return (0, "")

    status = response.status

    # Brief settle so any interstitial JS finishes.
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


_NON_HTML_EXT_RE = re.compile(
    r"\.(pdf|jpg|jpeg|png|gif|webp|svg|zip|css|js|ico|mp4|mp3|woff2?|ttf|eot)(\?|$)",
    re.IGNORECASE,
)


async def _evaluate_rendered_links(page) -> list[str]:
    """Pull every <a href> off the rendered DOM via Patchright's
    `page.evaluate`. This is the form the task spec calls out — it survives
    JS-rendered nav (Wix, Squarespace, lazy-loaded CF challenge bridges)
    that the raw-HTML regex misses.

    Returns a list of resolved absolute URLs (browser already resolves
    relative hrefs against document.baseURI). Caller filters same-origin /
    dedupes."""
    try:
        return await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
        )
    except Exception as e:
        logger.debug(f"stealth crawl evaluate failed: {e}")
        return []


_DOM_NAV_SELECTORS_JS = """
() => {
  const selectors = [
    'nav a[href]',
    'header a[href]',
    '[role="navigation"] a[href]',
    '.menu a[href]',
    '.main-menu a[href]',
    '.primary-menu a[href]',
    '.site-nav a[href]',
    '.navbar a[href]',
    '.nav a[href]',
    '#main-menu a[href]',
    '#primary-menu a[href]',
    '#site-navigation a[href]',
  ];
  const out = [];
  const seen = new Set();
  for (const sel of selectors) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch (_) { continue; }
    for (const n of nodes) {
      const href = n.href;
      if (!href || seen.has(href)) continue;
      seen.add(href);
      out.push(href);
    }
  }
  return out;
}
"""


async def _evaluate_dom_nav_links(page, root_url: str) -> list[str]:
    """Return a deduped list of same-origin nav URLs by querying the
    rendered DOM for `<nav>`, `<header>`, and common menu container
    selectors. Mirrors Tier 1's regex strategy but works against rendered
    DOM, so it captures JS-only nav implementations."""
    try:
        raw = await page.evaluate(_DOM_NAV_SELECTORS_JS)
    except Exception as e:
        logger.debug(f"stealth nav dom evaluate failed: {e}")
        return []

    out: list[str] = []
    seen: set[str] = set()
    for href in raw or []:
        normalized = _normalize_crawl_url(href, root_url)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    # Tier 1 only considers nav credible when ≥ 2 links are found; honour
    # the same threshold so downstream consumers don't ingest noise.
    return out if len(out) >= 2 else []


def _normalize_crawl_url(href: str, root_url: str) -> str | None:
    """Apply same-origin + fragment + query-strip filtering used during the
    BFS. Returns the canonical URL or None if it should be skipped."""
    if not href:
        return None
    if href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    try:
        absolute, _ = urldefrag(href)
    except Exception:
        return None
    absolute = absolute.split("?", 1)[0]
    if not absolute.lower().startswith(("http://", "https://")):
        return None
    if not _same_origin(absolute, root_url):
        return None
    if _NON_HTML_EXT_RE.search(absolute):
        return None
    return absolute.rstrip("/")


async def _bounded_crawl_browser(page, root_url: str) -> tuple[list[str], int]:
    """BFS crawl rendered DOM. Same-origin only, capped at CRAWL_MAX_PAGES
    URLs and CRAWL_MAX_DEPTH levels.

    Uses `page.evaluate("Array.from(document.querySelectorAll('a[href]'))...")
    rather than regex on raw HTML so JS-rendered nav and CF-injected page
    bodies are visible. The browser resolves relative hrefs against the
    document's baseURI for us.

    Returns:
        (discovered_urls, root_raw_href_count)

    `root_raw_href_count` is the number of <a href> entries that
    `document.querySelectorAll('a[href]')` returned for the depth-0 fetch
    of `root_url`. The main flow uses this to detect WAF soft-blocks and
    JS-heavy SPA placeholder DOMs (0 hrefs -> target_blocked hint).
    """
    root_norm = root_url.rstrip("/")
    visited: set[str] = {root_norm}
    discovered: list[str] = [root_norm]
    queue: list[tuple[str, int]] = [(root_norm, 0)]
    root_raw_href_count: int = 0

    while queue and len(discovered) < CRAWL_MAX_PAGES:
        url, depth = queue.pop(0)
        is_root = url == root_norm

        try:
            # `wait_until="load"` follows meta-refresh / JS challenge
            # redirects through to the final page. Important for sites
            # behind sg-captcha / IUAM where domcontentloaded fires on
            # the interstitial stub.
            response = await page.goto(
                url,
                wait_until="load",
                timeout=ROOT_NAV_TIMEOUT_MS if is_root else NAV_TIMEOUT_MS,
            )
            if response is None:
                logger.info(f"stealth crawl no-response {url}")
                continue
            status = response.status
            await page.wait_for_timeout(
                WAIT_AFTER_ROOT_NAV_MS if is_root else WAIT_AFTER_NAV_MS
            )

            # If the response status is a known WAF challenge code, give the
            # JS interstitial more time to clear and verify we actually have
            # markup before bailing out. SiteGround / CF challenges flip the
            # document body to the real site only after their challenge JS
            # mints a session cookie.
            if status in _INTERSTITIAL_STATUS:
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=ROOT_NAV_TIMEOUT_MS
                    )
                except Exception:
                    pass
                # Extra grace for slow challenges (mcculloughfamilytherapy
                # SiteGround sg-captcha is the canonical 202 case).
                await page.wait_for_timeout(WAIT_AFTER_ROOT_NAV_MS)
            elif status >= 400:
                logger.info(f"stealth crawl skip {url}: status={status}")
                continue
        except Exception as e:
            logger.info(f"stealth crawl nav error {url}: {e}")
            continue

        # Don't expand beyond the depth cap, but still record THIS page.
        if depth >= CRAWL_MAX_DEPTH:
            continue

        raw_hrefs = await _evaluate_rendered_links(page)
        # If the first attempt found nothing, give the page another beat
        # and re-query — single-page apps and WAF interstitials commonly
        # finish painting after domcontentloaded.
        if not raw_hrefs:
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await page.wait_for_timeout(2_000)
            raw_hrefs = await _evaluate_rendered_links(page)

        # Capture the depth-0 raw href count so the main flow can detect
        # WAF soft-blocks / JS-SPA placeholder DOMs cleanly. This is the
        # signal we already log as "0 raw hrefs" — surfacing it lets us
        # set status_hint=target_blocked instead of returning a misleading
        # "complete with 1 page" result.
        if is_root:
            root_raw_href_count = len(raw_hrefs or [])

        for href in raw_hrefs:
            normalized = _normalize_crawl_url(href, root_url)
            if normalized is None or normalized in visited:
                continue
            visited.add(normalized)
            discovered.append(normalized)
            if len(discovered) >= CRAWL_MAX_PAGES:
                break
            queue.append((normalized, depth + 1))

        logger.info(
            f"stealth crawl depth={depth} url={url} status={status} "
            f"-> {len(raw_hrefs)} raw hrefs, discovered={len(discovered)}"
        )

    return discovered, root_raw_href_count


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

            # ── Step 0: Warm-up — solve any WAF interstitial up-front ─
            # SiteGround's sg-captcha and Cloudflare's IUAM both work via a
            # JS-challenge -> redirect chain. We need to hit the origin once
            # with `wait_until="load"` (which follows meta-refresh and JS
            # navigations to completion) so the WAF mints its session cookie.
            # All subsequent /sitemap.xml + crawl probes then arrive with the
            # cookie set and bypass the challenge cleanly.
            await status_callback(task_id, "running", "Warming up stealth session...")
            try:
                warmup = await page.goto(
                    root_url + "/",
                    wait_until="load",
                    timeout=ROOT_NAV_TIMEOUT_MS,
                )
                await page.wait_for_timeout(WAIT_AFTER_ROOT_NAV_MS)
                # If the warm-up still landed on a challenge code, give it a
                # second beat in case the challenge JS is mid-execution.
                if warmup is not None and warmup.status in _INTERSTITIAL_STATUS:
                    try:
                        await page.wait_for_load_state(
                            "networkidle", timeout=ROOT_NAV_TIMEOUT_MS
                        )
                    except Exception:
                        pass
                    await page.wait_for_timeout(WAIT_AFTER_ROOT_NAV_MS)
                    # And try one more `goto load` — challenge cookie should
                    # now be present so this resolves to the real page.
                    try:
                        warmup = await page.goto(
                            root_url + "/",
                            wait_until="load",
                            timeout=ROOT_NAV_TIMEOUT_MS,
                        )
                        await page.wait_for_timeout(WAIT_AFTER_NAV_MS)
                    except Exception as e:
                        logger.info(f"stealth warmup retry failed: {e}")
                logger.info(
                    f"stealth-sitemap {task_id[:12]} warmup status="
                    f"{getattr(warmup, 'status', 'no-response')}"
                )
            except Exception as e:
                logger.info(f"stealth-sitemap {task_id[:12]} warmup error: {e}")

            # ── Step 1: Sitemap discovery via browser ──────────────────
            await status_callback(task_id, "running", "Discovering sitemap (stealth)...")
            sitemap_urls, source = await _discover_sitemap_via_browser(page, root_url)

            urls: list[str] = []
            # Tracks how many raw <a href> entries the depth-0 crawl saw on
            # root_url. -1 means "we never ran the crawl path" (sitemap was
            # found and walked instead). 0 means the crawl ran and the page
            # rendered an empty/placeholder DOM — the canonical WAF soft-
            # block signal.
            root_raw_href_count: int = -1
            if sitemap_urls:
                report["sitemap_source"] = source
                await status_callback(task_id, "running", f"Walking {len(sitemap_urls)} sitemap(s)...")
                urls = await _walk_sitemaps_browser(page, sitemap_urls)
                logger.info(f"stealth-sitemap {task_id[:12]}: {source} -> {len(urls)} URLs")
            else:
                report["sitemap_source"] = "crawl"
                await status_callback(task_id, "running", "No sitemap, rendered crawl...")
                urls, root_raw_href_count = await _bounded_crawl_browser(page, root_url)
                logger.info(
                    f"stealth-sitemap {task_id[:12]}: crawl -> {len(urls)} URLs "
                    f"(root_raw_hrefs={root_raw_href_count})"
                )

            # ── Step 2: Extract nav from homepage (rendered DOM) ─────
            try:
                response = await page.goto(
                    root_url + "/",
                    wait_until="load",
                    timeout=ROOT_NAV_TIMEOUT_MS,
                )
                await page.wait_for_timeout(WAIT_AFTER_ROOT_NAV_MS)
                # WAF-challenge aware: wait for networkidle once if status
                # was an interstitial code so the real homepage paints.
                hp_status = response.status if response else 0
                if hp_status in _INTERSTITIAL_STATUS:
                    try:
                        await page.wait_for_load_state(
                            "networkidle", timeout=ROOT_NAV_TIMEOUT_MS
                        )
                    except Exception:
                        pass
                    await page.wait_for_timeout(WAIT_AFTER_ROOT_NAV_MS)

                # Tier 1 regex strategy first — works on most server-rendered
                # therapy sites and yields a labelled nav_extraction_method
                # so downstream UI can show "where the nav came from".
                hp_html = await page.content() if response else ""
                nav_urls, nav_method = ([], "unsupported")
                if hp_html:
                    nav_urls, nav_method = _extract_nav_urls(hp_html, root_url)

                # Fallback: query the rendered DOM for <nav>, <header>, and
                # common menu containers. Captures Wix / Squarespace /
                # JS-rendered navs that the regex misses.
                if not nav_urls:
                    dom_nav = await _evaluate_dom_nav_links(page, root_url)
                    if dom_nav:
                        nav_urls = dom_nav
                        nav_method = "dom_nav"

                report["nav_urls"] = nav_urls
                report["nav_extraction_method"] = nav_method or "unsupported"
                logger.info(
                    f"stealth-sitemap {task_id[:12]} nav: "
                    f"{report['nav_extraction_method']} (hp_status={hp_status}) "
                    f"-> {len(nav_urls)} link(s)"
                )
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

            # ── Soft-block detection ───────────────────────────────────
            # Surface "Patchright reached the page but got nothing useful"
            # as a status_hint so CHQ can branch on it. Three conditions
            # must all be true:
            #   1. We fell to the crawl path (no sitemap.xml / robots
            #      sitemap discovered).
            #   2. The depth-0 fetch on root_url returned 0 raw hrefs from
            #      `document.querySelectorAll('a[href]')` — the canonical
            #      signal for WAF soft-blocks (sg-captcha that never
            #      cleared, CF challenge that swallowed the body) and
            #      JS-heavy SPAs that paint a placeholder shell with no
            #      anchors.
            #   3. Final categorized total_pages <= 1 (i.e. only the
            #      root_url itself made it through, which is meaningless
            #      for a real audit).
            # We do NOT change `total_pages` or the overall task status —
            # CHQ ingest reads `status_hint` and writes
            # `sitemap_scouts.status='blocked'` separately.
            if (
                report["sitemap_source"] == "crawl"
                and root_raw_href_count == 0
                and report["total_pages"] <= 1
            ):
                report["status_hint"] = "target_blocked"
                report["errors"].append(
                    "Patchright reached the page but the rendered DOM "
                    "exposed 0 internal links (WAF soft-block or JS-heavy "
                    "SPA — manual sitemap entry required)"
                )
                logger.info(
                    f"stealth-sitemap {task_id[:12]} status_hint=target_blocked "
                    f"(crawl, 0 raw hrefs, total_pages={report['total_pages']})"
                )

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
