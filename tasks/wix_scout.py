"""
wix_scout.py

Reconnaissance of a Wix site. Gathers page inventory, template info,
navigation, SEO state, connected services, and blog structure.

Strategy: Public-side crawl (httpx, fast). Wix does not have useful
admin API access for our use case, so everything is extracted from
the public site. Browser fallback for client-side-rendered navigation.

Duration: ~10-30s.
Cost: $0 (no LLM).
"""

import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import async_playwright

logger = logging.getLogger("moonraker.wix_scout")


async def run_wix_scout(task_id, params, status_callback, env):
    """
    params:
        website_url: str - e.g. "https://www.example.com"
        client_slug: str
        callback_url: str (optional)
    """
    website_url = params.get("website_url", "").rstrip("/")
    client_slug = params.get("client_slug", "")
    callback_url = params.get("callback_url", "")
    agent_api_key = env.get("AGENT_API_KEY", "")

    if not website_url:
        await status_callback(task_id, "failed", "website_url required")
        return

    report = {
        "scout_version": "1.0",
        "platform": "wix",
        "scanned_at": datetime.utcnow().isoformat() + "Z",
        "website_url": website_url,
        "client_slug": client_slug,
        "method": "",  # "public" or "public+browser"
        "site_info": {
            "title": "",
            "description": "",
            "language": "",
            "wix_site_id": "",
            "template_name": "",
            "is_wix_studio": False,
            "is_editor_x": False,
        },
        "pages": [],
        "navigation": {
            "main_nav": [],
            "footer_nav": [],
        },
        "blog": {
            "has_blog": False,
            "blog_url": "",
            "recent_posts": [],
            "post_count": 0,
        },
        "seo": {
            "meta_title": "",
            "meta_description": "",
            "og_image": "",
            "has_og_tags": False,
            "has_schema": False,
            "schema_types": [],
            "has_sitemap": False,
            "has_robots_txt": False,
            "canonical_url": "",
        },
        "connected_services": {
            "google_analytics": "",
            "google_tag_manager": "",
            "facebook_pixel": "",
            "other_scripts": [],
        },
        "design": {
            "detected_fonts": [],
            "custom_code": False,
        },
        "media": {
            "image_count": 0,
        },
        "wix_apps": [],
        "screenshots": {},
        "errors": [],
    }

    await status_callback(task_id, "running", "Starting public-side crawl...")

    # ── PHASE 1: Try httpx first for speed ──────────────────────────────
    public_success = await _crawl_public_site(
        website_url, report, status_callback, task_id
    )

    # ── PHASE 2: Wix sites are heavily JS-rendered, so if nav is empty
    #    fall back to browser to get rendered DOM ─────────────────────────
    needs_browser = len(report["navigation"]["main_nav"]) == 0

    if needs_browser:
        await status_callback(task_id, "running", "Navigation not in HTML, using browser...")
        browser_success = await _crawl_with_browser(
            website_url, report, status_callback, task_id
        )
        if browser_success:
            report["method"] = "public+browser"
        else:
            report["method"] = "public"
    else:
        report["method"] = "public"

    summary = _build_summary(report)
    await status_callback(task_id, "complete", f"Scout complete: {summary}")
    await _send_results(task_id, report, callback_url, agent_api_key)
    return report


# ── Public-side crawl (httpx) ────────────────────────────────────────────

async def _crawl_public_site(base_url, report, status_callback, task_id):
    """Extract everything we can from the public-facing site via HTTP."""

    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        },
    ) as client:

        # Step 1: Fetch homepage HTML
        try:
            await status_callback(task_id, "running", "Fetching homepage...")
            resp = await client.get(base_url)
            if resp.status_code != 200:
                report["errors"].append(f"Homepage returned {resp.status_code}")
                return False
            html = resp.text
        except Exception as e:
            report["errors"].append(f"Homepage fetch failed: {str(e)[:200]}")
            return False

        # Step 2: Detect Wix markers
        _detect_wix_info(html, report)

        # Step 3: Extract meta tags
        _extract_meta_tags(html, report)

        # Step 4: Extract navigation (may be empty for JS-rendered sites)
        _extract_navigation(html, report)

        # Step 5: Detect connected services
        _detect_connected_services(html, report)

        # Step 6: Detect fonts
        _detect_fonts(html, report)

        # Step 7: Check sitemap (Wix auto-generates these)
        await _check_seo_files(client, base_url, report)

        # Step 8: Detect schema
        _detect_schema(html, report)

        # Step 9: Try to find blog
        await _check_blog(client, base_url, html, report)

        # Step 10: Detect Wix apps/integrations from HTML
        _detect_wix_apps(html, report)

        # Step 11: Crawl discovered pages
        if report["navigation"]["main_nav"]:
            await status_callback(task_id, "running", "Crawling discovered pages...")
            await _crawl_nav_pages(client, base_url, report)

        return True


def _detect_wix_info(html, report):
    """Detect Wix-specific markers, site ID, and template info."""

    # Wix site ID
    site_id_match = re.search(r'"siteId"\s*:\s*"([a-f0-9-]+)"', html)
    if site_id_match:
        report["site_info"]["wix_site_id"] = site_id_match.group(1)

    # Wix Studio (formerly Editor X)
    if "wix-studio" in html.lower() or "editor-x" in html.lower():
        report["site_info"]["is_wix_studio"] = True

    if "editorx.com" in html:
        report["site_info"]["is_editor_x"] = True

    # Template detection from Wix meta
    template_match = re.search(r'"templateId"\s*:\s*"([^"]+)"', html)
    if template_match:
        report["site_info"]["template_name"] = template_match.group(1)

    # Also check for Wix ADI (Artificial Design Intelligence) markers
    if "wixADI" in html or "wix-adi" in html.lower():
        report["site_info"]["template_name"] = "Wix ADI"


def _extract_meta_tags(html, report):
    """Extract meta tags for SEO and site info."""

    title_match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if title_match:
        report["seo"]["meta_title"] = title_match.group(1).strip()
        report["site_info"]["title"] = title_match.group(1).strip()

    desc_match = re.search(
        r'<meta\s+name="description"\s+content="([^"]*)"', html, re.IGNORECASE
    )
    if desc_match:
        report["seo"]["meta_description"] = desc_match.group(1).strip()
        report["site_info"]["description"] = desc_match.group(1).strip()

    og_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
    og_desc = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
    og_image = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html)
    report["seo"]["has_og_tags"] = bool(og_title or og_desc or og_image)
    if og_image:
        report["seo"]["og_image"] = og_image.group(1)

    canonical = re.search(r'<link\s+rel="canonical"\s+href="([^"]*)"', html)
    if canonical:
        report["seo"]["canonical_url"] = canonical.group(1)

    lang_match = re.search(r'<html[^>]*lang="([^"]*)"', html)
    if lang_match:
        report["site_info"]["language"] = lang_match.group(1)


def _extract_navigation(html, report):
    """Extract navigation links. Wix sites are heavily JS-rendered,
    so this may return empty. Browser fallback handles that case."""

    # Try multiple nav patterns
    nav_patterns = [
        r'<nav[^>]*>(.*?)</nav>',
        r'<div[^>]*id="[^"]*[Nn]av[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]*data-testid="[^"]*navigation[^"]*"[^>]*>(.*?)</div>',
    ]

    for pattern in nav_patterns:
        matches = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)
        for nav_html in matches:
            links = re.findall(
                r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                nav_html,
                re.DOTALL,
            )
            for href, text in links:
                clean_text = re.sub(r"<[^>]+>", "", text).strip()
                if (
                    clean_text
                    and href
                    and not href.startswith(("#", "mailto:", "tel:", "javascript:"))
                    and len(clean_text) < 100
                ):
                    # Deduplicate
                    existing_urls = [i["url"] for i in report["navigation"]["main_nav"]]
                    if href not in existing_urls:
                        report["navigation"]["main_nav"].append({
                            "title": clean_text,
                            "url": href,
                        })

    # Wix sometimes has navigation data in JSON config
    nav_json_match = re.search(
        r'"menuItems"\s*:\s*(\[.*?\])\s*[,}]',
        html,
        re.DOTALL,
    )
    if nav_json_match and not report["navigation"]["main_nav"]:
        try:
            items = json.loads(nav_json_match.group(1))
            for item in items:
                label = item.get("label", "") or item.get("title", "")
                link = item.get("link", {})
                url = link.get("url", "") or link.get("href", "") if isinstance(link, dict) else ""
                if label and url:
                    report["navigation"]["main_nav"].append({
                        "title": label,
                        "url": url,
                    })
        except (json.JSONDecodeError, TypeError):
            pass


def _detect_connected_services(html, report):
    """Detect tracking scripts."""

    # Google Analytics
    ga_patterns = [
        r"(?:gtag|ga)\('config',\s*'((?:UA-|G-)\w+)'",
        r"google-analytics\.com/(?:analytics|ga|gtag)\.js\?id=((?:UA-|G-)\w+)",
    ]
    for pattern in ga_patterns:
        match = re.search(pattern, html)
        if match:
            report["connected_services"]["google_analytics"] = match.group(1)
            break

    # GTM
    gtm_match = re.search(r"GTM-(\w+)", html)
    if gtm_match:
        report["connected_services"]["google_tag_manager"] = f"GTM-{gtm_match.group(1)}"

    # Facebook Pixel
    fb_match = re.search(r"fbq\('init',\s*'(\d+)'\)", html)
    if fb_match:
        report["connected_services"]["facebook_pixel"] = fb_match.group(1)

    # Other services
    tracking_patterns = [
        (r"hotjar\.com", "Hotjar"),
        (r"clarity\.ms", "Microsoft Clarity"),
        (r"simplepractice\.com", "SimplePractice Widget"),
        (r"psychologytoday\.com", "Psychology Today Widget"),
        (r"janeapp\.com", "Jane App Widget"),
        (r"calendly\.com", "Calendly"),
        (r"acuityscheduling\.com", "Acuity Scheduling"),
    ]
    for pattern, name in tracking_patterns:
        if re.search(pattern, html, re.IGNORECASE):
            report["connected_services"]["other_scripts"].append(name)


def _detect_fonts(html, report):
    """Detect fonts used on the site."""

    # Google Fonts
    gf_matches = re.findall(r"fonts\.googleapis\.com/css[^\"']*family=([^\"'&]+)", html)
    for match in gf_matches:
        fonts = match.replace("+", " ").split("|")
        for font in fonts:
            font_name = font.split(":")[0].strip()
            if font_name and font_name not in report["design"]["detected_fonts"]:
                report["design"]["detected_fonts"].append(font_name)

    # Wix static fonts
    wix_font_matches = re.findall(r"static\.parastorage\.com/services/fonts[^\"']*?/([^/\"']+?)\.woff", html)
    for font_name in wix_font_matches:
        clean = font_name.replace("-", " ").title()
        if clean and clean not in report["design"]["detected_fonts"] and len(clean) < 50:
            report["design"]["detected_fonts"].append(clean)

    report["design"]["detected_fonts"] = list(set(report["design"]["detected_fonts"]))[:10]


def _detect_schema(html, report):
    """Detect JSON-LD structured data."""

    schemas = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if schemas:
        report["seo"]["has_schema"] = True
        for schema_text in schemas:
            try:
                schema_data = json.loads(schema_text)
                if isinstance(schema_data, dict):
                    st = schema_data.get("@type", "")
                    if st and st not in report["seo"]["schema_types"]:
                        report["seo"]["schema_types"].append(st)
                elif isinstance(schema_data, list):
                    for item in schema_data:
                        st = item.get("@type", "") if isinstance(item, dict) else ""
                        if st and st not in report["seo"]["schema_types"]:
                            report["seo"]["schema_types"].append(st)
            except (json.JSONDecodeError, TypeError):
                pass


def _detect_wix_apps(html, report):
    """Detect installed Wix apps from HTML markers."""

    app_patterns = [
        (r"wix-bookings", "Wix Bookings"),
        (r"wix-events", "Wix Events"),
        (r"wix-blog", "Wix Blog"),
        (r"wix-stores", "Wix Stores"),
        (r"wix-chat", "Wix Chat"),
        (r"wix-forms", "Wix Forms"),
        (r"wix-pro-gallery", "Wix Pro Gallery"),
        (r"wix-video", "Wix Video"),
        (r"wix-forum", "Wix Forum"),
        (r"wix-pricing-plans", "Wix Pricing Plans"),
        (r"members-area", "Wix Members Area"),
    ]
    for pattern, name in app_patterns:
        if re.search(pattern, html, re.IGNORECASE):
            if name not in report["wix_apps"]:
                report["wix_apps"].append(name)


async def _check_blog(client, base_url, html, report):
    """Check for Wix blog."""

    # Check for Wix Blog app markers
    if "wix-blog" in html.lower() or "blog-post" in html.lower():
        report["blog"]["has_blog"] = True

    # Check common blog URLs
    blog_slugs = ["/blog", "/journal", "/news", "/articles", "/posts"]
    for slug in blog_slugs:
        try:
            resp = await client.get(base_url + slug, timeout=8)
            if resp.status_code == 200 and any(
                marker in resp.text.lower()
                for marker in ["blog", "article", "post"]
            ):
                report["blog"]["has_blog"] = True
                report["blog"]["blog_url"] = slug
                break
        except Exception:
            continue


async def _check_seo_files(client, base_url, report):
    """Check for sitemap.xml and robots.txt."""

    try:
        resp = await client.get(base_url + "/sitemap.xml", timeout=10)
        if resp.status_code == 200 and ("<?xml" in resp.text[:100] or "<urlset" in resp.text[:200]):
            report["seo"]["has_sitemap"] = True

            # Parse sitemap for page URLs
            urls = re.findall(r"<loc>([^<]+)</loc>", resp.text)
            parsed_base = urlparse(base_url)
            for url in urls:
                parsed = urlparse(url)
                if parsed.netloc == parsed_base.netloc:
                    path = parsed.path.rstrip("/") or "/"
                    # Add to pages if not already discovered
                    existing = [p["url"] for p in report["pages"]]
                    existing_nav = [n["url"] for n in report["navigation"]["main_nav"]]
                    if path not in existing and path not in existing_nav:
                        report["pages"].append({
                            "url": path,
                            "full_url": url,
                            "title": "",
                            "source": "sitemap",
                        })
    except Exception:
        pass

    try:
        resp = await client.get(base_url + "/robots.txt", timeout=10)
        if resp.status_code == 200 and "user-agent" in resp.text.lower():
            report["seo"]["has_robots_txt"] = True
    except Exception:
        pass


async def _crawl_nav_pages(client, base_url, report):
    """Crawl each discovered page for SEO data."""

    nav_urls = [item["url"] for item in report["navigation"]["main_nav"]]
    if "/" not in nav_urls:
        nav_urls.insert(0, "/")

    for url in nav_urls[:20]:
        if url.startswith(("http://", "https://")):
            full_url = url
        else:
            full_url = base_url + url

        try:
            resp = await client.get(full_url, timeout=10)
            if resp.status_code != 200:
                continue

            page_html = resp.text

            title = ""
            title_match = re.search(r"<title>([^<]+)</title>", page_html, re.IGNORECASE)
            if title_match:
                title = title_match.group(1).strip()

            description = ""
            desc_match = re.search(
                r'<meta\s+name="description"\s+content="([^"]*)"',
                page_html,
                re.IGNORECASE,
            )
            if desc_match:
                description = desc_match.group(1).strip()

            h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.DOTALL)
            h1_text = re.sub(r"<[^>]+>", "", h1_match.group(1)).strip() if h1_match else ""

            images = re.findall(r"<img[^>]*>", page_html)

            report["pages"].append({
                "url": url if not url.startswith("http") else urlparse(url).path,
                "full_url": full_url,
                "title": title,
                "meta_description": description,
                "h1": h1_text,
                "image_count": len(images),
                "has_meta_description": bool(description),
            })

            report["media"]["image_count"] += len(images)

        except Exception as e:
            logger.debug(f"Failed to crawl {full_url}: {e}")
            continue


# ── Browser fallback (for JS-rendered navigation) ───────────────────────

async def _crawl_with_browser(base_url, report, status_callback, task_id):
    """Use Playwright to render the page and extract JS-rendered navigation."""

    browser = None
    playwright_instance = None

    try:
        playwright_instance = await async_playwright().start()
        browser = await playwright_instance.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        await status_callback(task_id, "running", "Loading page in browser...")
        await page.goto(base_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)  # Let Wix's JS finish rendering

        # Take screenshot
        report["screenshots"]["homepage"] = await _take_screenshot(page)

        # Extract rendered navigation
        nav_links = await page.evaluate("""
            () => {
                const links = [];
                // Try multiple selectors for Wix navigation
                const selectors = [
                    'nav a[href]',
                    '[data-testid*="nav"] a[href]',
                    '[id*="nav"] a[href]',
                    'header a[href]',
                ];
                const seen = new Set();
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(a => {
                        const href = a.getAttribute('href');
                        const text = a.textContent.trim();
                        if (text && href && !href.startsWith('#') && !href.startsWith('mailto:')
                            && !href.startsWith('tel:') && !seen.has(href) && text.length < 100) {
                            seen.add(href);
                            links.push({title: text, url: href});
                        }
                    });
                    if (links.length > 0) break;
                }
                return links;
            }
        """)

        for link in nav_links:
            existing_urls = [i["url"] for i in report["navigation"]["main_nav"]]
            if link["url"] not in existing_urls:
                report["navigation"]["main_nav"].append(link)

        # Extract H1 if not found in httpx crawl
        if not report["seo"]["meta_title"]:
            title = await page.title()
            if title:
                report["seo"]["meta_title"] = title
                report["site_info"]["title"] = title

        # Now crawl discovered nav pages via httpx
        if report["navigation"]["main_nav"]:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as client:
                await _crawl_nav_pages(client, base_url, report)

        return True

    except Exception as e:
        logger.exception(f"Browser crawl failed: {e}")
        report["errors"].append(f"Browser crawl error: {str(e)[:200]}")
        return False

    finally:
        try:
            if browser:
                await browser.close()
            if playwright_instance:
                await playwright_instance.stop()
        except Exception:
            pass


# ── Helpers ──────────────────────────────────────────────────────────────

def _build_summary(report):
    parts = [
        "Wix" + (" Studio" if report["site_info"]["is_wix_studio"] else ""),
        f"Template: {report['site_info']['template_name'] or 'unknown'}",
        f"{len(report['pages'])} pages",
        f"{len(report['navigation']['main_nav'])} nav items",
        f"Blog: {'yes' if report['blog']['has_blog'] else 'no'}",
        f"GA: {'yes' if report['connected_services']['google_analytics'] else 'no'}",
        f"Schema: {'yes' if report['seo']['has_schema'] else 'no'}",
        f"Apps: {len(report['wix_apps'])}",
        f"via {report['method']}",
    ]
    return " | ".join(parts)


async def _take_screenshot(page, quality=60):
    try:
        screenshot_bytes = await page.screenshot(type="jpeg", quality=quality, full_page=False)
        return base64.b64encode(screenshot_bytes).decode("utf-8")
    except Exception as e:
        logger.warning(f"Screenshot failed: {e}")
        return ""


async def _send_results(task_id, report, callback_url, agent_api_key):
    if not callback_url:
        logger.info("No callback URL, results stored in task data only")
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                callback_url,
                json={"task_id": task_id, "report": report},
                headers={
                    "Authorization": f"Bearer {agent_api_key}",
                    "Content-Type": "application/json",
                },
            )
            logger.info(f"Callback response: {resp.status_code}")
    except Exception as e:
        logger.error(f"Callback failed: {e}")
