"""
capture_design_assets.py

Lightweight Playwright task that captures design assets from a client website:
  1. Homepage: full-page screenshot + getComputedStyle + text extraction
  2. Service page: same (auto-discovered or provided)
  3. About/Bio page: same (auto-discovered or provided)

Uploads screenshots to Supabase Storage, callbacks to Client HQ.
~30-60 seconds total. No AI/LLM needed.
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
from playwright.async_api import async_playwright

logger = logging.getLogger("moonraker.capture_design_assets")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
BUCKET = "images"


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
    }

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
            homepage_data = await capture_page(page, website_url, client_slug, "homepage")
            result["screenshots"]["homepage"] = homepage_data.get("screenshot_url")
            result["computed_css"] = homepage_data.get("css", {})
            result["crawled_text"]["homepage"] = homepage_data.get("text", "")
            result["crawled_urls"]["homepage"] = website_url

            # ── DISCOVER SERVICE & ABOUT PAGES ──
            if not service_url or not about_url:
                await status_callback(task_id, "running", "Discovering pages...")
                links = await discover_pages(page, website_url)
                if not service_url:
                    service_url = links.get("service", "")
                if not about_url:
                    about_url = links.get("about", "")
                logger.info(f"Discovered: service={service_url}, about={about_url}")

            # ── SERVICE PAGE ──
            if service_url:
                await status_callback(task_id, "running", f"Capturing service page...")
                svc_data = await capture_page(page, service_url, client_slug, "service")
                result["screenshots"]["service"] = svc_data.get("screenshot_url")
                result["crawled_text"]["service"] = svc_data.get("text", "")
                result["crawled_urls"]["service"] = service_url
            else:
                logger.warning("No service page found")

            # ── ABOUT/BIO PAGE ──
            if about_url:
                await status_callback(task_id, "running", f"Capturing about page...")
                about_data = await capture_page(page, about_url, client_slug, "about")
                result["screenshots"]["about"] = about_data.get("screenshot_url")
                result["crawled_text"]["about"] = about_data.get("text", "")
                result["crawled_urls"]["about"] = about_url
            else:
                logger.warning("No about page found")

            await browser.close()
            browser = None

        # ── CALLBACK ──
        await status_callback(task_id, "running", "Sending results to Client HQ...")
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
                        },
                        headers={
                            "Authorization": f"Bearer {agent_api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    if resp.status_code == 200:
                        logger.info("Callback success")
                    else:
                        logger.error(f"Callback failed: {resp.status_code}")
            except Exception as e:
                logger.error(f"Callback error: {e}")

        pages_captured = sum(1 for v in result["screenshots"].values() if v)
        await status_callback(
            task_id, "completed",
            f"Design assets captured: {pages_captured} screenshots, CSS extracted."
        )

    except Exception as e:
        logger.error(f"Capture error: {e}", exc_info=True)
        await status_callback(task_id, "failed", f"Error: {str(e)[:200]}")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def capture_page(page, url, client_slug, page_type):
    """Navigate to URL, capture screenshot + CSS + text."""
    data = {}

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)  # let lazy content load

        # Full-page screenshot
        screenshot_bytes = await page.screenshot(full_page=True, type="png")

        # Convert to WebP and upload
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
        data["screenshot_url"] = upload_url
        logger.info(f"Screenshot uploaded: {storage_path} ({len(webp_bytes)} bytes)")

        # Extract computed CSS from key elements
        if page_type == "homepage":
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
            data["css"] = css

        # Extract text content
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
        data["text"] = text

    except Exception as e:
        logger.error(f"Error capturing {url}: {e}")
        data["error"] = str(e)[:200]

    return data


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
