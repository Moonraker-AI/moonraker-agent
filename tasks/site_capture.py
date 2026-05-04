"""
tasks/site_capture.py
=====================
Site-migration job 1 of 2: render a single client page in stealth Playwright,
harvest everything needed to rebuild it (HTML, computed styles, assets, fonts,
screenshots, manifest), and write results back to Supabase + R2.

Spec: docs/site-migration-agent-job-spec.md §1 (lives in client-hq repo).
This module implements §1.3 through §1.8.

Tier classification: Tier 2 (Light Browser) — acquires browser_lock in
server.py wrapper. One capture at a time. ~30-90s per page.

Idempotency: re-running for the same (migration_id, page_id) re-fetches the
origin (origin may have changed), re-uploads R2 artifacts (overwrite), and
sha256-deduplicates assets.

Failure handling (spec §1.7):
  - Network/timeout                -> error_log row, page row stays pending
  - Origin blocked us (4xx/5xx)    -> rewrite_status='excluded', notes=blocked
  - Stealth fingerprint detected   -> retry once with stealth UA, then exclude
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import os
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

from utils import cf_images, r2_client
from utils.site_migration_db import (
    log_error,
    patch_page,
    upsert_asset,
)

logger = logging.getLogger("agent.site_capture")

DEFAULT_UA = "MoonrakerSiteMigrator/1.0 (+https://moonraker.ai/migrator)"
STEALTH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

NAV_TIMEOUT_MS = 30_000
NETWORK_IDLE_MS = 8_000
SCROLL_PAUSE_MS = 500
PER_ORIGIN_SLEEP_S = 1.0  # politeness, jittered ±200ms

# Curated selector set (spec §1.3 step 9)
COMPUTED_STYLE_SELECTORS = [
    "body", "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "a", "button", "nav", "header", "footer", "section",
    ".button", '[class*="btn"]',
]

COMPUTED_STYLE_PROPS = [
    "fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
    "color", "backgroundColor", "padding", "margin", "border",
    "borderRadius", "textTransform",
]


# ── Public entrypoint ────────────────────────────────────────────────────────

async def run_site_capture(task_id, params, status_callback, env=None):
    """Top-level driver invoked by server._run_site_capture_with_lock.

    `params` is the validated request body (dict) from the FastAPI route.
    `status_callback(task_id, status, message)` updates the in-memory task
    store; the wrapper handles cleanup + cooldown.
    """
    migration_id = params.get("migration_id")
    page_id = params.get("page_id")
    url = params.get("url")
    viewports = params.get("viewports") or [
        {"name": "desktop", "width": 1440, "height": 900},
        {"name": "mobile", "width": 375, "height": 812},
    ]
    block_third_party = params.get("block_third_party") or []
    scroll_to_load = bool(params.get("scroll_to_load", True))
    max_scroll_passes = int(params.get("max_scroll_passes") or 6)

    await status_callback(task_id, "running", f"site-capture starting for {url}")

    # Pre-flight: R2 must be configured. CF Images optional.
    if not r2_client.is_configured():
        msg = "R2 not configured (R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT / R2_MIGRATION_BUCKET)"
        logger.error(f"site-capture {task_id[:12]} aborted: {msg}")
        await log_error(kind="site-capture", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    page_path = urlparse(url).path or "/"
    page_path_sha = hashlib.sha256(page_path.encode("utf-8")).hexdigest()
    r2_prefix = f"migration/{migration_id}/raw/{page_path_sha}"

    started = time.time()
    use_stealth_ua = False
    block_reason: Optional[str] = None
    final_http_status: Optional[int] = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            for attempt in range(2):  # default UA -> stealth UA fallback
                ua = STEALTH_UA if use_stealth_ua else DEFAULT_UA
                context = await browser.new_context(
                    user_agent=ua,
                    java_script_enabled=True,
                    locale="en-US",
                )
                _attach_route_blocker(context, block_third_party)

                screenshots: dict[str, bytes] = {}
                section_screenshots: list[bytes] = []
                rendered_html: str = ""
                styles_obj: dict = {}
                fonts_obj: list = []
                manifest_assets: list[str] = []
                head_meta: dict = {}
                forms_obj: list = []
                embeds_obj: list = []
                links_obj: dict = {"internal": [], "external": []}
                headings_obj: list = []

                # Run desktop first; section screenshots taken on desktop only.
                ordered_vps = sorted(
                    viewports,
                    key=lambda v: 0 if v.get("name") == "desktop" else 1,
                )

                aborted = False
                for vp in ordered_vps:
                    page = await context.new_page()
                    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                    await page.set_viewport_size({
                        "width": int(vp["width"]),
                        "height": int(vp["height"]),
                    })

                    # politeness jitter
                    await asyncio.sleep(PER_ORIGIN_SLEEP_S + random.uniform(-0.2, 0.2))

                    try:
                        resp = await page.goto(url, wait_until="domcontentloaded")
                        final_http_status = resp.status if resp else None
                    except PWTimeoutError as te:
                        await page.close()
                        await context.close()
                        block_reason = f"navigation timeout: {te}"
                        await log_error(
                            kind="site-capture",
                            migration_id=migration_id,
                            page_id=page_id,
                            error=block_reason,
                        )
                        aborted = True
                        break

                    if final_http_status and final_http_status >= 400:
                        # Origin actively blocked us.
                        block_reason = f"http {final_http_status}"
                        if not use_stealth_ua and final_http_status in (403, 429, 503):
                            await page.close()
                            await context.close()
                            use_stealth_ua = True
                            aborted = True  # restart outer loop with stealth UA
                            break
                        await page.close()
                        await context.close()
                        aborted = True
                        break

                    # network-idle with hard cap; falls back to fixed buffer
                    try:
                        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
                    except PWTimeoutError:
                        await page.wait_for_timeout(2000)

                    # Lazy-load triggering
                    if scroll_to_load:
                        await _scroll_passes(page, max_scroll_passes)

                    await _resolve_lazy_attrs(page)

                    # Full-page screenshot
                    full = await page.screenshot(full_page=True, type="png")
                    screenshots[vp["name"]] = full

                    # Section screenshots — desktop only
                    if vp.get("name") == "desktop":
                        section_screenshots = await _section_screenshots(page)

                        # Capture rendered HTML + computed styles + assets +
                        # head meta + forms + embeds + link graph (once is
                        # enough; the desktop DOM is the canonical one).
                        rendered_html = await page.content()
                        styles_obj = await _read_computed_styles(page)
                        fonts_obj = await _read_fonts(page)
                        manifest_assets = await _read_assets(page)
                        head_meta = await _read_head_meta(page)
                        forms_obj = await _read_forms(page)
                        embeds_obj = await _read_embeds(page)
                        links_obj = await _read_link_graph(page, url)
                        headings_obj = await _read_headings(page)

                    await page.close()

                if aborted:
                    await context.close()
                    if use_stealth_ua and attempt == 0:
                        # Re-loop with stealth UA
                        continue
                    break

                # ── Successful capture: push artifacts to R2 ─────────────
                # Write HTML / json artifacts
                await r2_client.put_object(
                    f"{r2_prefix}/rendered.html",
                    rendered_html.encode("utf-8"),
                    content_type="text/html; charset=utf-8",
                )
                await r2_client.put_object(
                    f"{r2_prefix}/styles.json",
                    json.dumps(styles_obj, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json",
                )
                await r2_client.put_object(
                    f"{r2_prefix}/assets.json",
                    json.dumps(manifest_assets, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json",
                )
                await r2_client.put_object(
                    f"{r2_prefix}/fonts.json",
                    json.dumps(fonts_obj, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json",
                )

                manifest = {
                    "assets": manifest_assets,
                    "forms": forms_obj,
                    "embeds": embeds_obj,
                    "internal_links": links_obj.get("internal", []),
                    "external_links": links_obj.get("external", []),
                    "headings": headings_obj,
                    "og": head_meta.get("og", {}),
                    "twitter": head_meta.get("twitter", {}),
                    "canonical": head_meta.get("canonical"),
                    "schema_jsonld": head_meta.get("schema_jsonld", []),
                    "fonts": fonts_obj,
                }
                await r2_client.put_object(
                    f"{r2_prefix}/manifest.json",
                    json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json",
                )
                for vp_name, png in screenshots.items():
                    await r2_client.put_object(
                        f"{r2_prefix}/screenshot-{vp_name}.png",
                        png,
                        content_type="image/png",
                    )
                for idx, png in enumerate(section_screenshots):
                    await r2_client.put_object(
                        f"{r2_prefix}/sections/{idx:02d}.png",
                        png,
                        content_type="image/png",
                    )

                # ── Asset originals (spec §1.5) ──────────────────────────
                await _ingest_asset_originals(
                    migration_id=migration_id,
                    base_url=url,
                    asset_urls=manifest_assets,
                    head_meta=head_meta,
                    page_path=page_path,
                )

                # ── Compute scalar metadata for page row ─────────────────
                title = head_meta.get("title") or ""
                meta_description = head_meta.get("meta_description") or ""
                word_count = _approx_word_count(rendered_html)

                await patch_page(page_id, {
                    "rendered_html_r2_key": f"{r2_prefix}/rendered.html",
                    "screenshot_r2_key": f"{r2_prefix}/screenshot-desktop.png",
                    "rewrite_status": "captured",
                    "http_status": final_http_status or 200,
                    "title": title[:500],
                    "meta_description": meta_description[:1000],
                    "word_count": word_count,
                    "manifest": manifest,
                    "last_crawled_at": datetime.now(timezone.utc).isoformat(),
                })

                await context.close()
                elapsed = int(time.time() - started)
                await status_callback(
                    task_id,
                    "complete",
                    f"site-capture done in {elapsed}s ({len(manifest_assets)} assets, "
                    f"{len(section_screenshots)} sections)",
                )
                return

            # Outer for-loop fell through without success.
            note = f"blocked: {block_reason or 'unknown'}"
            await patch_page(page_id, {
                "rewrite_status": "excluded",
                "http_status": final_http_status,
                "notes": note,
                "last_crawled_at": datetime.now(timezone.utc).isoformat(),
            })
            await log_error(
                kind="site-capture",
                migration_id=migration_id,
                page_id=page_id,
                error=note,
            )
            await status_callback(task_id, "error", note)
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# ── Page-level helpers ───────────────────────────────────────────────────────

def _attach_route_blocker(context: BrowserContext, block_third_party: list[str]):
    if not block_third_party:
        return
    patterns = [p.lower() for p in block_third_party]

    async def _route(route, request):
        host = (urlparse(request.url).hostname or "").lower()
        if any(p in host for p in patterns):
            await route.abort()
            return
        await route.continue_()

    asyncio.ensure_future(context.route("**/*", _route))


async def _scroll_passes(page: Page, passes: int):
    """Scroll bottom-to-top in N passes with 500ms between for IO observers."""
    height = await page.evaluate("document.documentElement.scrollHeight") or 0
    if not height:
        return
    step = max(1, height // max(1, passes))
    # bottom first
    for y in range(height, -1, -step):
        await page.evaluate("y => window.scrollTo(0, y)", y)
        await page.wait_for_timeout(SCROLL_PAUSE_MS)
    # back to top
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(SCROLL_PAUSE_MS)


async def _resolve_lazy_attrs(page: Page):
    """Resolve <img data-src> / <source data-srcset> patterns into real attrs."""
    await page.evaluate(
        """
        () => {
          for (const img of document.querySelectorAll('img[data-src]')) {
            if (!img.src && img.dataset.src) img.src = img.dataset.src;
          }
          for (const s of document.querySelectorAll('source[data-srcset]')) {
            if (!s.srcset && s.dataset.srcset) s.srcset = s.dataset.srcset;
          }
        }
        """
    )


async def _section_screenshots(page: Page) -> list[bytes]:
    """Identify top-level sections and screenshot each one."""
    handles = await page.evaluate_handle(
        """
        () => {
          const sels = ['section', 'main > div', 'article', '[role="region"]'];
          const out = [];
          for (const sel of sels) {
            for (const el of document.querySelectorAll(sel)) {
              if (out.includes(el)) continue;
              const r = el.getBoundingClientRect();
              if (r.height > 80 && r.width > 200) out.push(el);
            }
          }
          return out;
        }
        """
    )
    props = await handles.get_properties()
    out: list[bytes] = []
    for _, h in props.items():
        try:
            elem = h.as_element()
            if not elem:
                continue
            png = await elem.screenshot(type="png")
            out.append(png)
        except Exception as e:
            logger.warning(f"section screenshot failed: {e}")
        if len(out) >= 24:
            break
    return out


async def _read_computed_styles(page: Page) -> dict:
    return await page.evaluate(
        """
        ({selectors, props}) => {
          const out = {};
          for (const sel of selectors) {
            const matches = document.querySelectorAll(sel);
            const sample = [];
            for (const el of matches) {
              if (sample.length >= 3) break;
              const cs = getComputedStyle(el);
              const o = {};
              for (const p of props) o[p] = cs[p];
              sample.push(o);
            }
            if (sample.length) out[sel] = sample;
          }
          return out;
        }
        """,
        {"selectors": COMPUTED_STYLE_SELECTORS, "props": COMPUTED_STYLE_PROPS},
    )


async def _read_fonts(page: Page) -> list[dict]:
    return await page.evaluate(
        """
        () => {
          const out = [];
          for (const sheet of Array.from(document.styleSheets)) {
            let rules;
            try { rules = sheet.cssRules; } catch (e) { continue; }
            if (!rules) continue;
            for (const r of Array.from(rules)) {
              if (r.type === CSSRule.FONT_FACE_RULE) {
                const family = (r.style.getPropertyValue('font-family') || '').replace(/['"]/g, '').trim();
                const src = r.style.getPropertyValue('src') || '';
                const weight = r.style.getPropertyValue('font-weight') || '';
                out.push({ family, src, weight });
              }
            }
          }
          return out;
        }
        """
    )


async def _read_assets(page: Page) -> list[str]:
    """Build a complete asset URL list from performance entries + DOM."""
    return await page.evaluate(
        """
        () => {
          const urls = new Set();
          for (const e of performance.getEntriesByType('resource')) {
            const t = e.initiatorType;
            if (['img', 'css', 'link', 'script', 'font', 'fetch'].includes(t)) {
              urls.add(e.name);
            }
          }
          for (const img of document.querySelectorAll('img[src]')) urls.add(img.src);
          for (const img of document.querySelectorAll('img[srcset]')) {
            for (const part of img.srcset.split(',')) {
              const u = part.trim().split(' ')[0];
              if (u) urls.add(u);
            }
          }
          for (const s of document.querySelectorAll('source[srcset]')) {
            for (const part of s.srcset.split(',')) {
              const u = part.trim().split(' ')[0];
              if (u) urls.add(u);
            }
          }
          for (const el of document.querySelectorAll('*')) {
            const bg = getComputedStyle(el).backgroundImage;
            if (bg && bg !== 'none') {
              const m = bg.match(/url\\((['\"]?)(.*?)\\1\\)/g);
              if (m) for (const u of m) {
                const inner = u.replace(/url\\((['\"]?)(.*?)\\1\\)/, '$2');
                if (inner) urls.add(inner);
              }
            }
          }
          for (const l of document.querySelectorAll('link[href]')) urls.add(l.href);
          return Array.from(urls);
        }
        """
    )


async def _read_head_meta(page: Page) -> dict:
    return await page.evaluate(
        """
        () => {
          const out = { og: {}, twitter: {}, schema_jsonld: [] };
          out.title = (document.title || '').trim();
          for (const m of document.querySelectorAll('meta')) {
            const name = m.getAttribute('name') || m.getAttribute('property') || '';
            const content = m.getAttribute('content') || '';
            if (!name) continue;
            if (name === 'description') out.meta_description = content;
            if (name.startsWith('og:')) out.og[name] = content;
            if (name.startsWith('twitter:')) out.twitter[name] = content;
          }
          const can = document.querySelector('link[rel="canonical"]');
          if (can) out.canonical = can.href;
          for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
            try { out.schema_jsonld.push(JSON.parse(s.textContent)); }
            catch (e) { out.schema_jsonld.push(s.textContent); }
          }
          return out;
        }
        """
    )


async def _read_forms(page: Page) -> list[dict]:
    return await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('form')).map(f => {
          const fields = Array.from(f.querySelectorAll('input,textarea,select')).map(el => ({
            name: el.name || el.id || '',
            type: el.type || el.tagName.toLowerCase(),
            required: !!el.required,
            placeholder: el.placeholder || '',
          }));
          const r = f.getBoundingClientRect();
          return {
            action: f.action || '',
            method: (f.method || 'get').toLowerCase(),
            fields,
            visible: r.width > 0 && r.height > 0,
          };
        })
        """
    )


async def _read_embeds(page: Page) -> list[dict]:
    return await page.evaluate(
        """
        () => {
          const out = [];
          for (const f of document.querySelectorAll('iframe[src]')) {
            const src = f.src || '';
            let kind = 'unknown';
            if (/youtube\\.com|youtu\\.be/.test(src)) kind = 'youtube';
            else if (/vimeo\\.com/.test(src)) kind = 'vimeo';
            else if (/calendly\\.com/.test(src)) kind = 'calendly';
            else if (/gohighlevel\\.com|leadconnectorhq/.test(src)) kind = 'ghl';
            else if (/simplepractice\\.com|clientsecure\\.me/.test(src)) kind = 'simplepractice';
            else if (/google\\.com\\/maps/.test(src)) kind = 'maps';
            out.push({ src, kind });
          }
          for (const s of document.querySelectorAll('script[src]')) {
            const src = s.src || '';
            let kind = null;
            if (/calendly/.test(src)) kind = 'calendly';
            else if (/leadconnectorhq|gohighlevel/.test(src)) kind = 'ghl';
            if (kind) out.push({ src, kind });
          }
          return out;
        }
        """
    )


async def _read_link_graph(page: Page, base_url: str) -> dict:
    base_host = urlparse(base_url).hostname or ""
    return await page.evaluate(
        """
        (baseHost) => {
          const internal = [], external = [];
          for (const a of document.querySelectorAll('a[href]')) {
            const href = a.href || '';
            const text = (a.textContent || '').trim().slice(0, 200);
            try {
              const u = new URL(href);
              if (u.hostname && u.hostname === baseHost) internal.push({ href, text });
              else if (u.hostname) external.push({ href, text });
            } catch (e) { /* relative or javascript: — skip */ }
          }
          return { internal, external };
        }
        """,
        base_host,
    )


async def _read_headings(page: Page) -> list[dict]:
    return await page.evaluate(
        """
        () => {
          const out = [];
          for (let lv = 1; lv <= 6; lv++) {
            for (const h of document.querySelectorAll('h' + lv)) {
              out.push({ level: lv, text: (h.textContent || '').trim().slice(0, 500) });
            }
          }
          return out;
        }
        """
    )


def _approx_word_count(html: str) -> int:
    # Strip tags crudely, then split on whitespace. Good enough for ranking
    # decisions in CHQ; not a substitute for a real readability pass.
    import re
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return len([w for w in text.split() if w.strip()])


# ── Asset-original ingest (spec §1.5) ────────────────────────────────────────

async def _ingest_asset_originals(
    *,
    migration_id: str,
    base_url: str,
    asset_urls: list[str],
    head_meta: dict,
    page_path: str,
):
    """Download each unique asset, push to R2 (and CF Images for images),
    upsert into site_migration_assets."""
    seen_sha: set[str] = set()
    for raw in asset_urls:
        try:
            url = _clean_asset_url(raw, base_url)
            if not url or not url.lower().startswith(("http://", "https://")):
                continue
            sha = hashlib.sha256(url.encode("utf-8")).hexdigest()
            if sha in seen_sha:
                continue
            seen_sha.add(sha)

            try:
                async with httpx.AsyncClient(
                    timeout=30,
                    follow_redirects=True,
                    headers={"User-Agent": DEFAULT_UA},
                ) as client:
                    resp = await client.get(url)
                    if resp.status_code >= 400:
                        logger.info(f"asset {url} -> {resp.status_code}; skipping")
                        continue
                    body = resp.content
                    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            except Exception as fe:
                logger.warning(f"asset fetch failed {url}: {fe}")
                continue

            ext = _ext_from_url_or_ct(url, content_type)
            r2_key = f"migration/{migration_id}/assets/{sha}{ext}"
            try:
                await r2_client.put_object(
                    r2_key,
                    body,
                    content_type=content_type or "application/octet-stream",
                    cache_control="public, max-age=31536000, immutable",
                )
            except Exception as ke:
                logger.warning(f"R2 PUT failed for {url}: {ke}")
                continue

            cf_image_id: Optional[str] = None
            width: Optional[int] = None
            height: Optional[int] = None
            if content_type.startswith("image/") and not content_type.endswith("svg+xml"):
                # Probe dimensions with PIL when possible
                try:
                    from PIL import Image
                    im = Image.open(io.BytesIO(body))
                    width, height = im.size
                except Exception:
                    pass
                if cf_images.is_configured():
                    try:
                        cf_image_id = await cf_images.upload_bytes(
                            body,
                            filename=os.path.basename(urlparse(url).path) or sha,
                            image_id=f"mr-{migration_id[:8]}-{sha[:24]}",
                            content_type=content_type,
                        )
                    except Exception as ce:
                        logger.warning(f"CF Images upload failed for {url}: {ce}")

            await upsert_asset({
                "migration_id": migration_id,
                "sha256": sha,
                "origin_url": url,
                "r2_key": r2_key,
                "cf_image_id": cf_image_id,
                "bytes": len(body),
                "width": width,
                "height": height,
                "content_type": content_type or None,
                "first_seen_path": page_path,
            })
        except Exception as e:
            logger.warning(f"asset ingest unexpected error for {raw}: {e}")
            continue


def _clean_asset_url(raw: str, base_url: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    # Squarespace `?format=` strip — request master
    try:
        u = urljoin(base_url, raw)
        parsed = urlparse(u)
        if "squarespace" in (parsed.hostname or "") or "sqsp" in (parsed.hostname or ""):
            # drop query so we get the original
            u = u.split("?", 1)[0]
        return u
    except Exception:
        return raw


def _ext_from_url_or_ct(url: str, content_type: str) -> str:
    path = urlparse(url).path
    _, dot, ext = path.rpartition(".")
    if dot and 1 <= len(ext) <= 5 and ext.isalnum():
        return f".{ext.lower()}"
    if content_type:
        guess = mimetypes.guess_extension(content_type)
        if guess:
            return guess
    return ""
