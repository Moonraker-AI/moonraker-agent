"""
capture_design_assets.py

Lightweight Playwright task that captures design assets from a client website:
  1. Homepage: full-page screenshot + getComputedStyle + text extraction
  2. Service page: same (auto-discovered or provided)
  3. About/Bio page: same (auto-discovered or provided)

Uploads screenshots to Supabase Storage, callbacks to Client HQ.
~30-60 seconds total. No AI/LLM needed.

2026-04-26: rewritten for honest status reporting and partial-success handling.
- capture_page never raises; returns dict with `ok` flag + `error` string.
- Navigation uses domcontentloaded + bounded load wait, not networkidle (which
  hangs on sites with persistent analytics/chat connections).
- One automatic retry per page on navigation timeout.
- Outer status: 'completed' only if all 3 pages captured; 'partial' if 1-2;
  'failed' if 0. Per-page errors flow back to /api/ingest-design-assets in
  the `capture_errors` field for surface-level visibility.
- CSS extraction falls back across pages: if homepage fails, try service,
  then about, so CSS is captured whenever any page loads.
"""

import asyncio
import io
import json
import logging
import os
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

logger = logging.getLogger("moonraker.capture_design_assets")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
BUCKET = "images"

NAV_TIMEOUT_MS = 25000
LOAD_WAIT_MS = 8000
SETTLE_MS = 2500


async def run_capture_design_assets(task_id, params, status_callback, env):
    """
    params:
        design_spec_id: str
        client_slug: str
        website_url: str
        service_page_url: str (optional, auto-discovered if missing)
        about_page_url: str (optional, auto-discovered if missing)
        callback_url: str
    """
    design_spec_id = params.get("design_spec_id", "")
    client_slug = params.get("client_slug", "")
    website_url = params.get("website_url", "").rstrip("/")
    service_url = params.get("service_page_url", "")
    about_url = params.get("about_page_url", "")
    callback_url = params.get("callback_url", "")
    agent_api_key = env.get("AGENT_API_KEY", "")

    if not website_url:
        await status_callback(task_id, "failed", "website_url required")
        return

    await status_callback(task_id, "running", "Launching browser...")

    browser = None
    result = {
        "screenshots": {},
        "computed_css": {},
        "crawled_text": {},
        "crawled_urls": {},
        "capture_errors": {},  # per-page errors, surfaced to client-hq
    }
    pages_attempted = []
    pages_succeeded = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = await context.new_page()

            # ── HOMEPAGE ──
            await status_callback(task_id, "running", "Capturing homepage...")
            pages_attempted.append("homepage")
            homepage_data = await capture_page(page, website_url, client_slug, "homepage", extract_css=True)
            if homepage_data.get("ok"):
                pages_succeeded.append("homepage")
                if homepage_data.get("screenshot_url"):
                    result["screenshots"]["homepage"] = homepage_data["screenshot_url"]
                if homepage_data.get("css"):
                    result["computed_css"] = homepage_data["css"]
                if homepage_data.get("text"):
                    result["crawled_text"]["homepage"] = homepage_data["text"]
                result["crawled_urls"]["homepage"] = website_url
            else:
                result["capture_errors"]["homepage"] = homepage_data.get("error", "unknown error")
                logger.error(f"Homepage capture failed: {homepage_data.get('error')}")

            # ── DISCOVER SERVICE & ABOUT PAGES ──
            # Only meaningful if homepage loaded; otherwise skip discovery.
            if homepage_data.get("ok") and (not service_url or not about_url):
                await status_callback(task_id, "running", "Discovering pages...")
                try:
                    links = await discover_pages(page, website_url)
                    if not service_url:
                        service_url = links.get("service", "")
                    if not about_url:
                        about_url = links.get("about", "")
                    logger.info(f"Discovered: service={service_url}, about={about_url}")
                except Exception as disc_err:
                    logger.warning(f"Page discovery failed: {disc_err}")

            # ── SERVICE PAGE ──
            if service_url:
                pages_attempted.append("service")
                await status_callback(task_id, "running", "Capturing service page...")
                # Fall back to harvesting CSS here if homepage didn't supply it.
                need_css = not result["computed_css"]
                svc_data = await capture_page(page, service_url, client_slug, "service", extract_css=need_css)
                if svc_data.get("ok"):
                    pages_succeeded.append("service")
                    if svc_data.get("screenshot_url"):
                        result["screenshots"]["service"] = svc_data["screenshot_url"]
                    if need_css and svc_data.get("css"):
                        result["computed_css"] = svc_data["css"]
                    if svc_data.get("text"):
                        result["crawled_text"]["service"] = svc_data["text"]
                    result["crawled_urls"]["service"] = service_url
                else:
                    result["capture_errors"]["service"] = svc_data.get("error", "unknown error")
                    logger.error(f"Service capture failed: {svc_data.get('error')}")
            else:
                logger.warning("No service page found")

            # ── ABOUT/BIO PAGE ──
            if about_url:
                pages_attempted.append("about")
                await status_callback(task_id, "running", "Capturing about page...")
                need_css = not result["computed_css"]
                about_data = await capture_page(page, about_url, client_slug, "about", extract_css=need_css)
                if about_data.get("ok"):
                    pages_succeeded.append("about")
                    if about_data.get("screenshot_url"):
                        result["screenshots"]["about"] = about_data["screenshot_url"]
                    if need_css and about_data.get("css"):
                        result["computed_css"] = about_data["css"]
                    if about_data.get("text"):
                        result["crawled_text"]["about"] = about_data["text"]
                    result["crawled_urls"]["about"] = about_url
                else:
                    result["capture_errors"]["about"] = about_data.get("error", "unknown error")
                    logger.error(f"About capture failed: {about_data.get('error')}")
            else:
                logger.warning("No about page found")

            await browser.close()
            browser = None

        # ── CALLBACK ──
        await status_callback(task_id, "running", "Sending results to Client HQ...")
        callback_ok = False
        if callback_url and agent_api_key:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        callback_url,
                        json={
                            "design_spec_id": design_spec_id,
                            "screenshots": result["screenshots"],
                            "computed_css": result["computed_css"],
                            "crawled_text": result["crawled_text"],
                            "crawled_urls": result["crawled_urls"],
                            "capture_errors": result["capture_errors"],
                            "pages_attempted": pages_attempted,
                            "pages_succeeded": pages_succeeded,
                        },
                        headers={
                            "Authorization": f"Bearer {agent_api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    if resp.status_code == 200:
                        callback_ok = True
                        logger.info("Callback success")
                    else:
                        logger.error(f"Callback failed: {resp.status_code} {resp.text[:200]}")
            except Exception as e:
                logger.error(f"Callback error: {e}")

        # ── HONEST STATUS ──
        succeeded_count = len(pages_succeeded)
        attempted_count = len(pages_attempted)
        if succeeded_count == 0:
            err_summary = "; ".join(f"{k}: {v[:80]}" for k, v in result["capture_errors"].items())
            await status_callback(
                task_id, "failed",
                f"All page captures failed. {err_summary or 'no errors recorded'}",
            )
        elif succeeded_count < attempted_count:
            err_summary = "; ".join(f"{k}: {v[:80]}" for k, v in result["capture_errors"].items())
            msg = (
                f"Partial capture: {succeeded_count}/{attempted_count} pages "
                f"({', '.join(pages_succeeded)} ok; failed: {err_summary})"
            )
            # Surface as 'completed' with caveat in message so callers can opt
            # to inspect; client-hq ingest now writes capture_status='partial'.
            await status_callback(task_id, "completed", msg)
        else:
            await status_callback(
                task_id, "completed",
                f"All {succeeded_count} pages captured. CSS: {'yes' if result['computed_css'] else 'no'}.",
            )

    except Exception as e:
        logger.error(f"Capture error: {e}", exc_info=True)
        # Best-effort callback so client-hq doesn't sit on capture_status='running'.
        if callback_url and agent_api_key:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        callback_url,
                        json={
                            "design_spec_id": design_spec_id,
                            "screenshots": result["screenshots"],
                            "computed_css": result["computed_css"],
                            "crawled_text": result["crawled_text"],
                            "crawled_urls": result["crawled_urls"],
                            "capture_errors": dict(result["capture_errors"], _outer=str(e)[:200]),
                            "pages_attempted": pages_attempted,
                            "pages_succeeded": pages_succeeded,
                        },
                        headers={
                            "Authorization": f"Bearer {agent_api_key}",
                            "Content-Type": "application/json",
                        },
                    )
            except Exception:
                pass
        await status_callback(task_id, "failed", f"Error: {str(e)[:200]}")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def _navigate_with_retry(page, url):
    """Navigate to URL; retry once on timeout. Returns (ok, error_str)."""
    for attempt in (1, 2):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            # Try to reach 'load', but don't fail the capture if persistent
            # connections (analytics, chat widgets) prevent it.
            try:
                await page.wait_for_load_state("load", timeout=LOAD_WAIT_MS)
            except PWTimeoutError:
                logger.info(f"load state timeout on {url}, proceeding with DOM-ready content")
            await page.wait_for_timeout(SETTLE_MS)
            return True, None
        except PWTimeoutError as e:
            if attempt == 1:
                logger.warning(f"Navigation timeout on {url}, retrying...")
                continue
            return False, f"navigation timeout after {NAV_TIMEOUT_MS}ms (2 attempts)"
        except Exception as e:
            return False, f"navigation error: {str(e)[:120]}"
    return False, "navigation failed (unreachable)"


async def capture_page(page, url, client_slug, page_type, extract_css=False):
    """Navigate to URL, capture screenshot + (optional) CSS + text.

    Never raises; always returns a dict with `ok` flag and either data or
    an `error` string.
    """
    out = {"ok": False, "error": None}

    nav_ok, nav_err = await _navigate_with_retry(page, url)
    if not nav_ok:
        out["error"] = nav_err
        return out

    # Screenshot
    try:
        screenshot_bytes = await page.screenshot(full_page=True, type="png", timeout=20000)
        img = Image.open(io.BytesIO(screenshot_bytes))
        if img.width > 1440:
            ratio = 1440 / img.width
            img = img.resize((1440, int(img.height * ratio)), Image.LANCZOS)
        # WebP max dimension is 16383px. Cap height to avoid encoding error.
        if img.height > 12000:
            img = img.crop((0, 0, img.width, 12000))
        webp_buf = io.BytesIO()
        img.save(webp_buf, format="WEBP", quality=85)
        webp_bytes = webp_buf.getvalue()

        storage_path = f"{client_slug}/design-spec/{page_type}.webp"
        upload_url = await upload_to_storage(storage_path, webp_bytes)
        if upload_url:
            out["screenshot_url"] = upload_url
            logger.info(f"Screenshot uploaded: {storage_path} ({len(webp_bytes)} bytes)")
        else:
            out["error"] = "screenshot upload failed"
            return out
    except Exception as e:
        out["error"] = f"screenshot error: {str(e)[:120]}"
        return out

    # CSS extraction
    if extract_css:
        try:
            css = await page.evaluate("""() => {
                function getStyles(selector, label) {
                    const el = document.querySelector(selector);
                    if (!el) return null;
                    const cs = window.getComputedStyle(el);
                    return {
                        element: label,
                        fontFamily: cs.fontFamily,
                        fontSize: cs.fontSize,
                        fontWeight: cs.fontWeight,
                        lineHeight: cs.lineHeight,
                        color: cs.color,
                        letterSpacing: cs.letterSpacing,
                        backgroundColor: cs.backgroundColor,
                    };
                }

                const body = document.querySelector('body');
                const bodyCs = body ? window.getComputedStyle(body) : null;

                return {
                    body: getStyles('body', 'body'),
                    h1: getStyles('h1', 'h1'),
                    h2: getStyles('h2', 'h2'),
                    h3: getStyles('h3', 'h3'),
                    p: getStyles('p', 'paragraph'),
                    a: getStyles('a', 'link'),
                    button: getStyles('button', 'button') || getStyles('a.btn, a.button, .cta, [class*="btn"]', 'button-like'),
                    nav: getStyles('nav, header', 'navigation'),
                    footer: getStyles('footer', 'footer'),
                    bodyBackground: bodyCs ? bodyCs.backgroundColor : null,
                    allColors: (() => {
                        const colors = new Set();
                        const els = document.querySelectorAll('h1,h2,h3,h4,p,a,button,nav,header,footer,.btn,[class*="cta"]');
                        els.forEach(el => {
                            const cs = window.getComputedStyle(el);
                            colors.add(cs.color);
                            if (cs.backgroundColor !== 'rgba(0, 0, 0, 0)') colors.add(cs.backgroundColor);
                        });
                        return [...colors].slice(0, 20);
                    })(),
                    allFonts: (() => {
                        const fonts = new Set();
                        const els = document.querySelectorAll('h1,h2,h3,h4,p,a,button,li,span');
                        els.forEach(el => {
                            fonts.add(window.getComputedStyle(el).fontFamily);
                        });
                        return [...fonts].slice(0, 10);
                    })(),
                };
            }""")
            out["css"] = css
        except Exception as e:
            logger.warning(f"CSS extract failed on {url}: {e}")
            # Don't fail the page on CSS error; screenshot is still useful.

    # Text content
    try:
        text = await page.evaluate("""() => {
            const selectors = ['h1','h2','h3','h4','p','li','blockquote','.hero','[class*="intro"]','[class*="about"]'];
            const seen = new Set();
            const parts = [];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    const t = el.innerText.trim();
                    if (t && t.length > 10 && !seen.has(t)) {
                        seen.add(t);
                        parts.push(t);
                    }
                });
            });
            return parts.join('\\n\\n').substring(0, 15000);
        }""")
        out["text"] = text
    except Exception as e:
        logger.warning(f"Text extract failed on {url}: {e}")

    out["ok"] = True
    return out


async def discover_pages(page, base_url):
    """Find service and about pages by scanning homepage links."""
    links = await page.evaluate("""(baseUrl) => {
        const anchors = [...document.querySelectorAll('a[href]')];
        const urls = anchors.map(a => {
            try { return new URL(a.href, baseUrl).href; } catch { return null; }
        }).filter(Boolean);

        const base = new URL(baseUrl);
        const internal = urls.filter(u => {
            try { return new URL(u).hostname === base.hostname; } catch { return false; }
        });

        const unique = [...new Set(internal)];

        // Service page patterns
        const servicePatterns = [/therapy/i, /counseling/i, /treatment/i, /service/i, /emdr/i, /anxiety/i, /depression/i, /trauma/i, /ptsd/i, /couples/i, /pain/i, /specialt/i];
        const service = unique.find(u => {
            const path = new URL(u).pathname.toLowerCase();
            return path !== '/' && servicePatterns.some(p => p.test(path));
        });

        // About page patterns
        const aboutPatterns = [/about/i, /bio/i, /team/i, /therapist/i, /staff/i, /clinician/i, /meet/i, /our-/i];
        const about = unique.find(u => {
            const path = new URL(u).pathname.toLowerCase();
            return path !== '/' && aboutPatterns.some(p => p.test(path));
        });

        return { service: service || null, about: about || null, all_links: unique.slice(0, 30) };
    }""", base_url)

    return links


async def upload_to_storage(path, data):
    """Upload bytes to Supabase Storage, return hosted URL."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("Supabase not configured, skipping upload")
        return None

    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            content=data,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "image/webp",
                "x-upsert": "true",
            },
        )
        if resp.status_code not in (200, 201):
            logger.error(f"Upload failed: {resp.status_code} {resp.text[:200]}")
            return None

    # Return the clean hosted URL via Vercel rewrite
    # URL includes /img/ to match Vercel rewrite pattern
    parts = path.split("/", 1)  # split: slug / rest
    return f"https://clients.moonraker.ai/{parts[0]}/img/{parts[1]}" if len(parts) > 1 else f"https://clients.moonraker.ai/img/{path}"
