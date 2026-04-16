"""
sq_scout.py

Reconnaissance of a Squarespace site. Gathers page inventory, template info,
navigation, SEO state, connected services, and blog structure.

Strategy: Public-side crawl first (httpx, fast, no auth needed), then
browser-based admin panel scan if contributor credentials are provided.

Duration: ~10-20s public-only, 30-60s with admin panel.
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

logger = logging.getLogger("moonraker.sq_scout")

# Squarespace template families detectable from source
TEMPLATE_FAMILIES = {
    "bedford": "Bedford",
    "brine": "Brine",
    "farro": "Farro",
    "galapagos": "Galapagos",
    "hayden": "Hayden",
    "jasper": "Jasper",
    "montauk": "Montauk",
    "native": "Native",
    "pacific": "Pacific",
    "rally": "Rally",
    "skye": "Skye",
    "tremont": "Tremont",
    "tudor": "Tudor",
    "wells": "Wells",
    "york": "York",
    "avenue": "Avenue",
    "adirondack": "Adirondack",
    "momentum": "Momentum",
    "cinco": "Cinco",
    "horizon": "Horizon",
    "miller": "Miller",
    "fulton": "Fulton",
    "shift": "Shift",
    "hester": "Hester",
    "bryant": "Bryant",
    "pedro": "Pedro",
    "sahara": "Sahara",
    "mercer": "Mercer",
    "wexley": "Wexley",
    "west": "West",
    # 7.1 templates (Fluid Engine)
    "fluid-engine": "Fluid Engine (7.1)",
}


async def run_sq_scout(task_id, params, status_callback, env):
    """
    params:
        website_url: str - e.g. "https://www.example.com"
        client_slug: str
        sq_email: str (optional) - Squarespace contributor email
        sq_password: str (optional) - Squarespace contributor password
        sq_site_id: str (optional) - Squarespace site identifier (for multi-site accounts)
        callback_url: str (optional)
    """
    website_url = params.get("website_url", "").rstrip("/")
    client_slug = params.get("client_slug", "")
    sq_email = params.get("sq_email", "") or env.get("SQ_EMAIL", "")
    sq_password = params.get("sq_password", "") or env.get("SQ_PASSWORD", "")
    sq_site_id = params.get("sq_site_id", "")
    callback_url = params.get("callback_url", "")
    agent_api_key = env.get("AGENT_API_KEY", "")

    if not website_url:
        await status_callback(task_id, "failed", "website_url required")
        return

    report = {
        "scout_version": "1.0",
        "platform": "squarespace",
        "scanned_at": datetime.utcnow().isoformat() + "Z",
        "website_url": website_url,
        "client_slug": client_slug,
        "method": "",  # "public", "public+admin", "admin"
        "site_info": {
            "title": "",
            "description": "",
            "language": "",
            "squarespace_version": "",  # "7.0" or "7.1"
            "template_family": "",
            "template_name": "",
            "site_id": "",
            "collection_type_slug": "",
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
            "custom_css": False,
            "header_code_injection": False,
            "footer_code_injection": False,
            "custom_fonts": [],
            "detected_fonts": [],
        },
        "media": {
            "image_count": 0,
            "uses_stock_images": False,
        },
        "admin_panel": {
            "accessed": False,
            "contributor_role": "",
            "hidden_pages": [],
            "disabled_pages": [],
            "integrations": [],
            "connected_domains": [],
        },
        "screenshots": {},
        "errors": [],
    }

    await status_callback(task_id, "running", "Starting public-side crawl...")

    # ── PHASE 1: Public-side crawl (fast, no auth) ──────────────────────
    public_success = await _crawl_public_site(
        website_url, report, status_callback, task_id
    )

    if public_success:
        report["method"] = "public"

    # ── PHASE 2: Admin panel (if credentials provided) ──────────────────
    if sq_email and sq_password:
        await status_callback(task_id, "running", "Logging in to Squarespace admin...")
        admin_success = await _scan_admin_panel(
            website_url, sq_email, sq_password, sq_site_id,
            report, status_callback, task_id
        )
        if admin_success:
            report["method"] = "public+admin" if public_success else "admin"
            report["admin_panel"]["accessed"] = True

    summary = _build_summary(report)
    await status_callback(task_id, "complete", f"Scout complete: {summary}")
    await _send_results(task_id, report, callback_url, agent_api_key)
    return report


# ── Public-side crawl ────────────────────────────────────────────────────

async def _crawl_public_site(base_url, report, status_callback, task_id):
    """Extract everything we can from the public-facing site."""

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

        # Step 2: Detect Squarespace version and template
        await status_callback(task_id, "running", "Detecting template and version...")
        _detect_sq_version_and_template(html, report)

        # Step 3: Extract site metadata
        _extract_meta_tags(html, report)

        # Step 4: Extract navigation from HTML
        _extract_navigation(html, report)

        # Step 5: Detect connected services (GA, GTM, FB Pixel, etc.)
        _detect_connected_services(html, report)

        # Step 6: Detect custom code injection
        _detect_code_injection(html, report)

        # Step 7: Detect fonts
        _detect_fonts(html, report)

        # Step 8: Check for blog
        await status_callback(task_id, "running", "Checking for blog...")
        await _check_blog(client, base_url, html, report)

        # Step 9: Crawl pages from navigation
        await status_callback(task_id, "running", "Crawling discovered pages...")
        await _crawl_nav_pages(client, base_url, report, status_callback, task_id)

        # Step 10: Check sitemap and robots.txt
        await _check_seo_files(client, base_url, report)

        # Step 11: Check for JSON-LD schema
        _detect_schema(html, report)

        # Step 12: Try Squarespace's internal JSON endpoint
        await _try_sq_json_api(client, base_url, report)

        return True


def _detect_sq_version_and_template(html, report):
    """Detect Squarespace version (7.0 vs 7.1) and template family."""

    # Squarespace adds data attributes and meta tags
    # data-controller="..." on body
    # <meta property="og:site_name" content="...">

    # Version detection: 7.1 uses "sqs-block-content" and Fluid Engine markers
    if "Static.SQUARESPACE_CONTEXT" in html:
        # Extract the SQUARESPACE_CONTEXT JSON
        ctx_match = re.search(
            r'Static\.SQUARESPACE_CONTEXT\s*=\s*(\{.*?\})\s*;',
            html,
            re.DOTALL,
        )
        if ctx_match:
            try:
                ctx = json.loads(ctx_match.group(1))
                report["site_info"]["site_id"] = ctx.get("websiteId", "")
                template_id = ctx.get("templateId", "")
                report["site_info"]["template_name"] = template_id

                # Map template ID to family
                for key, family in TEMPLATE_FAMILIES.items():
                    if key in template_id.lower():
                        report["site_info"]["template_family"] = family
                        break
            except (json.JSONDecodeError, TypeError):
                pass

    # 7.1 markers
    if any(marker in html for marker in [
        "sqs-fluid-engine",
        "fluid-engine",
        "data-fluid-engine",
        '"templateVersion":"7.1"',
        "squarespace-v7.1",
    ]):
        report["site_info"]["squarespace_version"] = "7.1"
    elif any(marker in html for marker in [
        "Static.SQUARESPACE_CONTEXT",
        "squarespace.com/universal/scripts-compressed",
        "data-content-field",
    ]):
        # Could be 7.0 or 7.1 — check further
        if "sqs-layout" in html and "sqs-block" in html:
            report["site_info"]["squarespace_version"] = "7.0"
        else:
            report["site_info"]["squarespace_version"] = "7.1"
    else:
        report["site_info"]["squarespace_version"] = "unknown"

    # Try to get template from CSS class on body
    body_class_match = re.search(r'<body[^>]*class="([^"]*)"', html)
    if body_class_match:
        body_classes = body_class_match.group(1).lower()
        for key, family in TEMPLATE_FAMILIES.items():
            if key in body_classes:
                report["site_info"]["template_family"] = family
                break

    # Also check for "collection-type-" pattern
    coll_match = re.search(r'collection-type-(\w+)', html)
    if coll_match:
        report["site_info"]["collection_type_slug"] = coll_match.group(1)


def _extract_meta_tags(html, report):
    """Extract meta tags for SEO and site info."""

    # Title
    title_match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if title_match:
        report["seo"]["meta_title"] = title_match.group(1).strip()
        report["site_info"]["title"] = title_match.group(1).strip()

    # Meta description
    desc_match = re.search(
        r'<meta\s+name="description"\s+content="([^"]*)"', html, re.IGNORECASE
    )
    if desc_match:
        report["seo"]["meta_description"] = desc_match.group(1).strip()
        report["site_info"]["description"] = desc_match.group(1).strip()

    # OG tags
    og_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
    og_desc = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
    og_image = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html)
    report["seo"]["has_og_tags"] = bool(og_title or og_desc or og_image)
    if og_image:
        report["seo"]["og_image"] = og_image.group(1)

    # Canonical
    canonical = re.search(r'<link\s+rel="canonical"\s+href="([^"]*)"', html)
    if canonical:
        report["seo"]["canonical_url"] = canonical.group(1)

    # Language
    lang_match = re.search(r'<html[^>]*lang="([^"]*)"', html)
    if lang_match:
        report["site_info"]["language"] = lang_match.group(1)

    # OG site name (often the practice name)
    site_name = re.search(
        r'<meta\s+property="og:site_name"\s+content="([^"]*)"', html
    )
    if site_name and not report["site_info"]["title"]:
        report["site_info"]["title"] = site_name.group(1).strip()


def _extract_navigation(html, report):
    """Extract navigation links from the HTML."""

    # Squarespace uses specific patterns for navigation
    # 7.1: <nav> with data-folder attribute, or nav inside header
    # 7.0: #mainNavigation or .main-nav

    # Try multiple patterns
    nav_patterns = [
        # 7.1 pattern: header nav links
        r'<header[^>]*>.*?<nav[^>]*>(.*?)</nav>',
        # 7.0 pattern: main navigation div
        r'<nav[^>]*id="[^"]*[Nn]av[^"]*"[^>]*>(.*?)</nav>',
        # Generic nav
        r'<nav[^>]*class="[^"]*header[^"]*"[^>]*>(.*?)</nav>',
        # data-content-field nav
        r'<div[^>]*data-content-field="navigation"[^>]*>(.*?)</div>',
    ]

    nav_html = ""
    for pattern in nav_patterns:
        match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if match:
            nav_html = match.group(1)
            break

    if nav_html:
        # Extract links
        links = re.findall(
            r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            nav_html,
            re.DOTALL,
        )
        for href, text in links:
            clean_text = re.sub(r"<[^>]+>", "", text).strip()
            if clean_text and href and not href.startswith("#"):
                report["navigation"]["main_nav"].append({
                    "title": clean_text,
                    "url": href,
                })

    # Footer navigation
    footer_patterns = [
        r'<footer[^>]*>(.*?)</footer>',
        r'<div[^>]*class="[^"]*footer[^"]*"[^>]*>(.*?)</div>',
    ]
    for pattern in footer_patterns:
        match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if match:
            footer_html = match.group(1)
            links = re.findall(
                r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                footer_html,
                re.DOTALL,
            )
            for href, text in links:
                clean_text = re.sub(r"<[^>]+>", "", text).strip()
                if clean_text and href and not href.startswith(("http://", "https://", "mailto:", "tel:", "#")):
                    report["navigation"]["footer_nav"].append({
                        "title": clean_text,
                        "url": href,
                    })
            break


def _detect_connected_services(html, report):
    """Detect Google Analytics, GTM, Facebook Pixel, etc."""

    # Google Analytics (UA or GA4)
    ga_patterns = [
        r"(?:gtag|ga)\('config',\s*'((?:UA-|G-)\w+)'",
        r"google-analytics\.com/(?:analytics|ga|gtag)\.js\?id=((?:UA-|G-)\w+)",
        r'"(?:UA-|G-)(\w+)"',
    ]
    for pattern in ga_patterns:
        match = re.search(pattern, html)
        if match:
            ga_id = match.group(1) if match.group(1).startswith(("UA-", "G-")) else match.group(0).strip('"')
            report["connected_services"]["google_analytics"] = ga_id
            break

    # Google Tag Manager
    gtm_match = re.search(r"GTM-(\w+)", html)
    if gtm_match:
        report["connected_services"]["google_tag_manager"] = f"GTM-{gtm_match.group(1)}"

    # Facebook Pixel
    fb_match = re.search(r"fbq\('init',\s*'(\d+)'\)", html)
    if fb_match:
        report["connected_services"]["facebook_pixel"] = fb_match.group(1)

    # Other tracking scripts
    tracking_patterns = [
        (r"hotjar\.com.*?hjid:(\d+)", "Hotjar"),
        (r"clarity\.ms.*?\"(\w+)\"", "Microsoft Clarity"),
        (r"intercom.*?app_id:\s*['\"](\w+)['\"]", "Intercom"),
        (r"crisp\.chat.*?CRISP_WEBSITE_ID\s*=\s*['\"]([^'\"]+)['\"]", "Crisp"),
        (r"hubspot\.com.*?\/(\d+)\.js", "HubSpot"),
        (r"zoho.*?widgetcode.*?([a-f0-9]+)", "Zoho"),
        (r"simplepractice\.com", "SimplePractice Widget"),
        (r"psychologytoday\.com", "Psychology Today Widget"),
        (r"therapyportal\.com", "TherapyPortal Widget"),
        (r"janeapp\.com", "Jane App Widget"),
    ]
    for pattern, name in tracking_patterns:
        if re.search(pattern, html, re.IGNORECASE):
            report["connected_services"]["other_scripts"].append(name)


def _detect_code_injection(html, report):
    """Detect custom CSS and code injection."""

    # Custom CSS (Squarespace stores it in a <style> block with specific markers)
    if re.search(r"<style[^>]*id=\"custom-css\"", html) or \
       re.search(r"sqs-custom-css", html):
        report["design"]["custom_css"] = True

    # Header code injection — look for custom scripts/styles not from Squarespace
    non_sq_scripts = re.findall(r'<script[^>]*src="([^"]*)"', html)
    for src in non_sq_scripts:
        if "squarespace" not in src.lower() and "static1.squarespace.com" not in src:
            report["design"]["header_code_injection"] = True
            break


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

    # Squarespace built-in font families from CSS
    font_family_matches = re.findall(
        r'font-family:\s*["\']?([^;"\'}\n]+)',
        html,
    )
    for match in font_family_matches:
        font_name = match.strip().split(",")[0].strip().strip("'\"")
        if (
            font_name
            and font_name not in report["design"]["detected_fonts"]
            and font_name.lower() not in ("inherit", "sans-serif", "serif", "monospace", "initial")
            and len(font_name) < 50
        ):
            report["design"]["detected_fonts"].append(font_name)

    # Deduplicate
    report["design"]["detected_fonts"] = list(set(report["design"]["detected_fonts"]))[:10]


async def _check_blog(client, base_url, html, report):
    """Check if the site has a blog."""

    # Common Squarespace blog URL patterns
    blog_slugs = ["/blog", "/journal", "/news", "/articles", "/posts", "/insights"]

    # Check navigation first
    for nav_item in report["navigation"]["main_nav"]:
        url_lower = nav_item["url"].lower()
        for slug in blog_slugs:
            if slug in url_lower:
                report["blog"]["has_blog"] = True
                report["blog"]["blog_url"] = nav_item["url"]
                break

    # Also check homepage for blog section markers
    if "class=\"blog-" in html or "collection-type-blog" in html:
        report["blog"]["has_blog"] = True

    # If blog found, try to get recent posts
    if report["blog"]["blog_url"]:
        blog_url = report["blog"]["blog_url"]
        if blog_url.startswith("/"):
            blog_url = base_url + blog_url

        try:
            # Try the JSON endpoint for the blog collection
            json_url = blog_url.rstrip("/") + "?format=json"
            resp = await client.get(json_url, timeout=10)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    items = data.get("items", [])
                    report["blog"]["post_count"] = len(items)
                    for item in items[:5]:
                        report["blog"]["recent_posts"].append({
                            "title": item.get("title", ""),
                            "url": item.get("fullUrl", ""),
                            "published": item.get("publishOn", ""),
                            "excerpt": (item.get("excerpt", "") or "")[:200],
                        })
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass

    # If no blog URL found but blog detected, try common slugs
    if not report["blog"]["blog_url"] and not report["blog"]["has_blog"]:
        for slug in blog_slugs[:3]:
            try:
                resp = await client.get(base_url + slug, timeout=5)
                if resp.status_code == 200 and "blog" in resp.text.lower():
                    report["blog"]["has_blog"] = True
                    report["blog"]["blog_url"] = slug
                    break
            except Exception:
                continue


async def _crawl_nav_pages(client, base_url, report, status_callback, task_id):
    """Crawl each page found in navigation to get SEO data."""

    nav_urls = [item["url"] for item in report["navigation"]["main_nav"]]

    # Add homepage if not in nav
    if "/" not in nav_urls:
        nav_urls.insert(0, "/")

    for url in nav_urls[:20]:  # Cap at 20 pages
        if url.startswith(("http://", "https://")):
            full_url = url
        else:
            full_url = base_url + url

        try:
            resp = await client.get(full_url, timeout=10)
            if resp.status_code != 200:
                continue

            page_html = resp.text

            # Extract page-level meta
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

            # Count images on the page
            images = re.findall(r"<img[^>]*>", page_html)

            # Detect h1
            h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.DOTALL)
            h1_text = re.sub(r"<[^>]+>", "", h1_match.group(1)).strip() if h1_match else ""

            # Detect page type from Squarespace markers
            page_type = "page"
            if "collection-type-blog" in page_html:
                page_type = "blog"
            elif "collection-type-gallery" in page_html:
                page_type = "gallery"
            elif "collection-type-events" in page_html:
                page_type = "events"
            elif "collection-type-products" in page_html:
                page_type = "products"

            report["pages"].append({
                "url": url,
                "full_url": full_url,
                "title": title,
                "meta_description": description,
                "h1": h1_text,
                "image_count": len(images),
                "page_type": page_type,
                "has_meta_description": bool(description),
            })

            report["media"]["image_count"] += len(images)

        except Exception as e:
            logger.debug(f"Failed to crawl {full_url}: {e}")
            continue


async def _check_seo_files(client, base_url, report):
    """Check for sitemap.xml and robots.txt."""

    # Sitemap
    try:
        resp = await client.get(base_url + "/sitemap.xml", timeout=10)
        if resp.status_code == 200 and "<?xml" in resp.text[:100]:
            report["seo"]["has_sitemap"] = True
    except Exception:
        pass

    # Robots.txt
    try:
        resp = await client.get(base_url + "/robots.txt", timeout=10)
        if resp.status_code == 200 and ("user-agent" in resp.text.lower() or "sitemap" in resp.text.lower()):
            report["seo"]["has_robots_txt"] = True
    except Exception:
        pass


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
                    schema_type = schema_data.get("@type", "")
                    if schema_type and schema_type not in report["seo"]["schema_types"]:
                        report["seo"]["schema_types"].append(schema_type)
                elif isinstance(schema_data, list):
                    for item in schema_data:
                        schema_type = item.get("@type", "") if isinstance(item, dict) else ""
                        if schema_type and schema_type not in report["seo"]["schema_types"]:
                            report["seo"]["schema_types"].append(schema_type)
            except (json.JSONDecodeError, TypeError):
                pass


async def _try_sq_json_api(client, base_url, report):
    """
    Squarespace exposes ?format=json on many pages (especially 7.0).
    Try to get the site-wide collection data.
    """
    try:
        resp = await client.get(base_url + "/?format=json", timeout=10)
        if resp.status_code == 200:
            try:
                data = resp.json()
                # Extract collection info
                website = data.get("website", {})
                if website:
                    report["site_info"]["site_id"] = website.get("id", report["site_info"]["site_id"])
                    report["site_info"]["title"] = website.get("siteTitle", "") or report["site_info"]["title"]
                    report["site_info"]["description"] = website.get("siteDescription", "") or report["site_info"]["description"]

                # Navigation from JSON
                if not report["navigation"]["main_nav"]:
                    nav_items = website.get("navigation", [])
                    for item in nav_items:
                        title = item.get("title", "")
                        url_id = item.get("urlId", "")
                        if title and url_id:
                            report["navigation"]["main_nav"].append({
                                "title": title,
                                "url": f"/{url_id}",
                                "collection_id": item.get("collectionId", ""),
                            })
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception:
        pass


# ── Admin panel scan (browser-based) ─────────────────────────────────────

async def _scan_admin_panel(
    website_url, sq_email, sq_password, sq_site_id,
    report, status_callback, task_id,
):
    """Log in to Squarespace admin and gather additional data."""

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
            locale="en-US",
            timezone_id="America/New_York",
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = {runtime: {}};
        """)
        page = await context.new_page()
        page.set_default_timeout(20000)

        # Step 1: Log in to Squarespace
        await status_callback(task_id, "running", "Logging in to Squarespace...")
        login_success = await _sq_login(page, sq_email, sq_password, report)

        if not login_success:
            report["errors"].append("Squarespace login failed")
            return False

        # Step 2: Navigate to the correct site (if multi-site account)
        await status_callback(task_id, "running", "Navigating to site dashboard...")
        site_nav_success = await _navigate_to_site(
            page, website_url, sq_site_id, report
        )

        if not site_nav_success:
            report["errors"].append("Could not navigate to site dashboard")
            # Take screenshot of where we are
            report["screenshots"]["login_result"] = await _take_screenshot(page)
            return False

        # Step 3: Capture dashboard screenshot
        report["screenshots"]["dashboard"] = await _take_screenshot(page)

        # Step 4: Scan pages panel
        await status_callback(task_id, "running", "Scanning pages panel...")
        await _scan_pages_panel(page, report)

        # Step 5: Check design/template settings
        await status_callback(task_id, "running", "Checking design settings...")
        await _scan_design_settings(page, report)

        # Step 6: Check connected domains
        await status_callback(task_id, "running", "Checking domain settings...")
        await _scan_domain_settings(page, report)

        return True

    except Exception as e:
        logger.exception(f"Admin panel scan failed: {e}")
        report["errors"].append(f"Admin panel error: {str(e)[:200]}")
        return False

    finally:
        try:
            if browser:
                await browser.close()
            if playwright_instance:
                await playwright_instance.stop()
        except Exception:
            pass


async def _sq_login(page, email, password, report):
    """Log in to Squarespace at login.squarespace.com.
    Uses App Password which bypasses 2FA."""

    try:
        await page.goto("https://login.squarespace.com/", wait_until="networkidle", timeout=25000)
        await asyncio.sleep(2)

        # Look for email field
        email_field = None
        email_selectors = [
            'input[name="email"]',
            'input[type="email"]',
            'input#email',
            'input[data-test="email-input"]',
        ]
        for sel in email_selectors:
            try:
                email_field = await page.wait_for_selector(sel, timeout=3000)
                if email_field:
                    break
            except Exception:
                continue

        if not email_field:
            report["errors"].append("Could not find email field on Squarespace login")
            report["screenshots"]["login_page"] = await _take_screenshot(page)
            return False

        await email_field.fill(email)
        # Also dispatch input event for React-controlled forms
        await page.evaluate("el => el.dispatchEvent(new Event('input', {bubbles: true}))", email_field)
        await asyncio.sleep(0.3)

        # Look for password field (may be on same page or appear after email)
        pass_field = None
        pass_selectors = [
            'input[name="password"]',
            'input[type="password"]',
            'input#password',
            'input[data-test="password-input"]',
        ]
        for sel in pass_selectors:
            try:
                pass_field = await page.query_selector(sel)
                if pass_field:
                    break
            except Exception:
                continue

        if not pass_field:
            # Two-step login: click continue to reveal password field
            continue_btn = await page.query_selector(
                'button[type="submit"], button[data-test="login-button"], '
                'button.login-button, input[type="submit"]'
            )
            if continue_btn:
                await continue_btn.click()
                await asyncio.sleep(2)

            for sel in pass_selectors:
                try:
                    pass_field = await page.wait_for_selector(sel, timeout=5000)
                    if pass_field:
                        break
                except Exception:
                    continue

        if not pass_field:
            report["errors"].append("Could not find password field on Squarespace login")
            report["screenshots"]["login_no_password"] = await _take_screenshot(page)
            return False

        # Use click + keyboard typing for password (more reliable with React forms)
        await pass_field.click()
        await asyncio.sleep(0.2)
        await page.keyboard.type(password, delay=15)
        await asyncio.sleep(0.3)

        # Click login button
        # Take pre-submit screenshot for debugging
        report["screenshots"]["pre_login"] = await _take_screenshot(page)
        logger.info("Clicking LOG IN button...")
        submit_btn = await page.query_selector(
            'button[type="submit"], button[data-test="login-button"], '
            'button.login-button, input[type="submit"]'
        )
        if submit_btn:
            await submit_btn.click()
        else:
            await pass_field.press("Enter")

        # Wait for the full redirect chain to complete.
        # Squarespace login goes: login.squarespace.com -> OAuth flow -> account.squarespace.com
        # We need to wait until we're OFF login.squarespace.com entirely.
        logger.info("Login submitted, waiting for redirect chain...")
        for attempt in range(15):
            await asyncio.sleep(2)
            current_url = page.url
            current_host = urlparse(current_url).hostname or ""
            logger.info(f"Login redirect check {attempt}: {current_host} - {current_url[:120]}")

            # Success: we've left the login domain
            if current_host not in ("login.squarespace.com", ""):
                logger.info(f"Squarespace login successful, landed at: {current_url[:120]}")
                report["login"] = {"success": True, "method": "app_password", "landed_url": current_url[:200]}
                return True

            # Check for error messages on login page
            error_el = await page.query_selector(
                '.error-message, [data-test="error-message"], .form-error, '
                '[class*="error"], [class*="Error"]'
            )
            if error_el:
                error_text = await error_el.inner_text()
                if error_text.strip():
                    report["errors"].append(f"Login error: {error_text.strip()[:200]}")
                    report["screenshots"]["login_error"] = await _take_screenshot(page)
                    return False

        # Timed out waiting for redirect
        report["errors"].append(f"Login redirect timed out. Still on: {page.url[:200]}")
        report["screenshots"]["login_timeout"] = await _take_screenshot(page)
        return False

    except Exception as e:
        report["errors"].append(f"Login exception: {str(e)[:200]}")
        return False


async def _navigate_to_site(page, website_url, sq_site_id, report):
    """Navigate to the correct site using the dashboard search box.
    After login, we land on account.squarespace.com with a site selector.
    We use the Search input to find the right site by domain."""

    current_url = page.url
    logger.info(f"Navigate to site: starting from {current_url[:120]}")

    # If we're already on a site config page, we're good
    if "/config" in current_url:
        return True

    # Parse target domain for matching
    parsed = urlparse(website_url)
    target_domain = parsed.netloc.replace("www.", "").lower()
    # Short search term: just the domain name without TLD
    search_term = target_domain.split(".")[0]

    # Step 1: Make sure we're on the account dashboard
    if "account.squarespace.com" not in current_url:
        try:
            await page.goto("https://account.squarespace.com", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"Failed to navigate to account dashboard: {e}")

    # Take screenshot of dashboard
    report["screenshots"]["site_selector"] = await _take_screenshot(page)

    # Step 2: Find and use the Search box
    search_input = None
    search_selectors = [
        'input[type="search"]',
        'input[placeholder*="Search"]',
        'input[placeholder*="search"]',
        'input[aria-label*="Search"]',
        'input[aria-label*="search"]',
    ]
    for sel in search_selectors:
        try:
            search_input = await page.query_selector(sel)
            if search_input:
                break
        except Exception:
            continue

    if search_input:
        logger.info(f"Found search box, typing: {search_term}")
        await search_input.click()
        await asyncio.sleep(0.3)
        await page.keyboard.type(search_term, delay=50)
        await asyncio.sleep(2)  # Wait for search results to filter

        # Take screenshot of filtered results
        report["screenshots"]["search_results"] = await _take_screenshot(page)

        # Now look for a WEBSITE button or site link matching our domain
        # The filtered results should show only matching sites
        website_buttons = await page.query_selector_all('a, button')
        for btn in website_buttons:
            try:
                text = (await btn.inner_text() or "").strip()
                href = (await btn.get_attribute("href") or "")

                # Click "WEBSITE" button which goes to /config
                if text.upper() == "WEBSITE" and href:
                    logger.info(f"Clicking WEBSITE button: href={href[:80]}")
                    await btn.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await asyncio.sleep(3)
                    if "/config" in page.url:
                        logger.info(f"Successfully entered site config: {page.url[:80]}")
                        return True
                    # The WEBSITE button might have opened a new context
                    break
            except Exception:
                continue

        # If WEBSITE button didn't work, try clicking the site card/name itself
        all_links = await page.query_selector_all("a[href]")
        for link in all_links:
            try:
                href = (await link.get_attribute("href") or "").lower()
                text = (await link.inner_text() or "").lower().strip()
                if target_domain in href or target_domain in text:
                    logger.info(f"Clicking domain match: text={text[:40]}, href={href[:60]}")
                    await link.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await asyncio.sleep(2)
                    if "/config" in page.url:
                        return True
            except Exception:
                continue
    else:
        logger.warning("Could not find search box on dashboard")

    # Fallback: try direct URL approaches
    # Some Squarespace sites can be accessed directly via their squarespace subdomain
    direct_urls = []
    if sq_site_id:
        direct_urls.append(f"https://{sq_site_id}.squarespace.com/config")
    # Also try the custom domain config (sometimes works when authenticated)
    direct_urls.append(f"{website_url.rstrip('/')}/config")

    for url in direct_urls:
        try:
            logger.info(f"Trying direct config URL: {url[:80]}")
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            if "/config" in page.url and "SITE DELETED" not in (await page.title()):
                logger.info(f"Direct URL worked: {page.url[:80]}")
                return True
        except Exception:
            continue

    # All strategies failed
    report["screenshots"]["nav_failed"] = await _take_screenshot(page)
    logger.warning(f"Could not navigate to site. Final URL: {page.url[:120]}")
    return False


async def _scan_pages_panel(page, report):
    """Scan the Pages panel for hidden/disabled pages."""

    try:
        # Navigate to pages section
        # Click "Pages" in the left sidebar
        pages_link = await page.query_selector(
            'a[href*="/config/pages"], a[data-test="pages"], '
            '[class*="pages" i] a'
        )
        if pages_link:
            await pages_link.click()
            await asyncio.sleep(2)

        # Or navigate directly
        if "/pages" not in page.url:
            current_base = page.url.split("/config")[0] if "/config" in page.url else page.url
            await page.goto(current_base + "/config/pages", wait_until="domcontentloaded", timeout=10000)
            await asyncio.sleep(2)

        # Take screenshot of pages panel
        report["screenshots"]["pages_panel"] = await _take_screenshot(page)

        # Look for page items in the panel
        # Squarespace pages panel shows enabled/disabled status
        page_items = await page.query_selector_all(
            '[class*="page-item"], [class*="PageItem"], '
            '[data-test*="page"], .navigation-item'
        )

        for item in page_items:
            try:
                text = await item.inner_text()
                classes = await item.get_attribute("class") or ""

                if "disabled" in classes.lower() or "hidden" in classes.lower():
                    report["admin_panel"]["disabled_pages"].append(text.strip()[:100])
                elif "not-linked" in classes.lower() or "unlinked" in classes.lower():
                    report["admin_panel"]["hidden_pages"].append(text.strip()[:100])
            except Exception:
                continue

    except Exception as e:
        report["errors"].append(f"Pages panel scan failed: {str(e)[:200]}")


async def _scan_design_settings(page, report):
    """Check design/template settings."""

    try:
        current_base = page.url.split("/config")[0] if "/config" in page.url else page.url
        await page.goto(
            current_base + "/config/design",
            wait_until="domcontentloaded",
            timeout=10000,
        )
        await asyncio.sleep(2)

        report["screenshots"]["design"] = await _take_screenshot(page)

        # Try to extract template info from the design panel
        template_el = await page.query_selector(
            '[class*="template"], [class*="Template"], '
            '[data-test*="template"]'
        )
        if template_el:
            template_text = await template_el.inner_text()
            if template_text and not report["site_info"]["template_name"]:
                report["site_info"]["template_name"] = template_text.strip()[:100]

    except Exception as e:
        report["errors"].append(f"Design settings scan failed: {str(e)[:200]}")


async def _scan_domain_settings(page, report):
    """Check connected domains."""

    try:
        current_base = page.url.split("/config")[0] if "/config" in page.url else page.url
        await page.goto(
            current_base + "/config/domains",
            wait_until="domcontentloaded",
            timeout=10000,
        )
        await asyncio.sleep(2)

        # Look for domain entries
        domain_items = await page.query_selector_all(
            '[class*="domain"], [class*="Domain"], '
            '[data-test*="domain"]'
        )
        for item in domain_items:
            try:
                text = await item.inner_text()
                if "." in text and len(text) < 100:
                    report["admin_panel"]["connected_domains"].append(text.strip())
            except Exception:
                continue

    except Exception as e:
        report["errors"].append(f"Domain settings scan failed: {str(e)[:200]}")


# ── Helpers ──────────────────────────────────────────────────────────────

def _build_summary(report):
    parts = [
        f"SQ {report['site_info']['squarespace_version']}"
        if report["site_info"]["squarespace_version"] != "unknown" else "SQ version unknown",
        f"Template: {report['site_info']['template_family'] or report['site_info']['template_name'] or 'unknown'}",
        f"{len(report['pages'])} pages crawled",
        f"{len(report['navigation']['main_nav'])} nav items",
        f"Blog: {'yes' if report['blog']['has_blog'] else 'no'}",
        f"GA: {'yes' if report['connected_services']['google_analytics'] else 'no'}",
        f"Schema: {'yes' if report['seo']['has_schema'] else 'no'}",
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
