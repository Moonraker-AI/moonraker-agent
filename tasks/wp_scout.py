"""
wp_scout.py

Reconnaissance of a WordPress site. Gathers environment info (theme, plugins,
pages, menus, SEO, permalink structure) to inform future automation tasks.

Strategy: REST API first (fast, no WAF issues), browser fallback for
anything the API can't reach (SEO plugin details, visual verification).

Duration: ~10-30 seconds via API, 30-60 seconds if browser fallback needed.
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

logger = logging.getLogger("moonraker.wp_scout")


async def run_wp_scout(task_id, params, status_callback, env):
    """
    params:
        wp_admin_url: str - e.g. "https://example.com/wp-admin"
        wp_username: str
        wp_password: str
        client_slug: str (optional)
        callback_url: str (optional)
    """
    wp_admin_url = params.get("wp_admin_url", "").rstrip("/")
    wp_username = params.get("wp_username", "")
    wp_password = params.get("wp_password", "")
    client_slug = params.get("client_slug", "")
    callback_url = params.get("callback_url", "")
    agent_api_key = env.get("AGENT_API_KEY", "")

    if not all([wp_admin_url, wp_username, wp_password]):
        await status_callback(task_id, "failed", "wp_admin_url, wp_username, wp_password required")
        return

    # Derive base site URL
    parsed = urlparse(wp_admin_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    report = {
        "scout_version": "2.0",
        "scanned_at": datetime.utcnow().isoformat() + "Z",
        "wp_admin_url": wp_admin_url,
        "base_url": base_url,
        "method": "",  # "rest_api" or "browser" or "rest_api+browser"
        "login": {"success": False, "method": "", "notes": ""},
        "wordpress": {"version": "", "multisite": False},
        "theme": {"name": "", "version": "", "type": "", "stylesheet": ""},
        "seo_plugin": {"name": "none", "version": ""},
        "page_builder": {"name": "none", "version": ""},
        "editor_type": "gutenberg",
        "plugins": [],
        "pages": [],
        "menus": {"locations": [], "items": []},
        "permalink_structure": "",
        "media_stats": {"total_items": 0},
        "rest_api": {"available": False, "auth_works": False, "url": ""},
        "screenshots": {},
        "errors": [],
    }

    await status_callback(task_id, "running", "Probing REST API...")

    # ── PHASE 1: REST API (fast, bypasses WAF) ──────────────────────────

    api_success = await _try_rest_api(
        base_url, wp_username, wp_password, report, status_callback, task_id
    )

    if api_success:
        report["method"] = "rest_api"
        summary = _build_summary(report)
        await status_callback(task_id, "complete", f"Scout complete (REST API): {summary}")
        await _send_results(task_id, report, callback_url, agent_api_key)
        return report
        return

    # ── PHASE 2: Browser fallback ────────────────────────────────────────

    await status_callback(task_id, "running", "REST API unavailable, trying browser...")
    logger.info("REST API failed or insufficient, falling back to browser")

    browser_success = await _try_browser(
        base_url, wp_admin_url, wp_username, wp_password,
        report, status_callback, task_id
    )

    if browser_success:
        report["method"] = "rest_api+browser" if report["rest_api"]["available"] else "browser"

    summary = _build_summary(report)
    final_status = "complete" if (api_success or browser_success) else "complete"
    msg = f"Scout complete: {summary}"
    if not report["login"]["success"]:
        msg = f"Scout complete (limited, login failed): {summary}"

    await status_callback(task_id, final_status, msg)
    await _send_results(task_id, report, callback_url, agent_api_key)
    return report


# ── REST API approach ────────────────────────────────────────────────────

async def _try_rest_api(base_url, username, password, report, status_callback, task_id):
    """Try to gather data via the WordPress REST API. Returns True if successful."""

    api_root = f"{base_url}/wp-json"
    auth = httpx.BasicAuth(username, password)

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:

        # Step 1: Check if REST API is available (no auth needed)
        try:
            await status_callback(task_id, "running", "Checking REST API availability...")
            resp = await client.get(f"{api_root}/")
            if resp.status_code == 200:
                data = resp.json()
                report["rest_api"]["available"] = True
                report["rest_api"]["url"] = api_root

                # WP version from the API root
                if "name" in data:
                    report["wordpress"]["site_name"] = data["name"]
                # Some installs expose the version in namespaces
                namespaces = data.get("namespaces", [])
                logger.info(f"REST API available. Namespaces: {len(namespaces)}")
            else:
                logger.info(f"REST API returned {resp.status_code}")
                report["rest_api"]["available"] = False
                return False
        except Exception as e:
            logger.info(f"REST API not available: {e}")
            report["rest_api"]["available"] = False
            return False

        # Step 2: Test authentication
        try:
            await status_callback(task_id, "running", "Testing REST API authentication...")
            resp = await client.get(f"{api_root}/wp/v2/users/me", auth=auth)
            if resp.status_code == 200:
                user_data = resp.json()
                report["rest_api"]["auth_works"] = True
                report["login"]["success"] = True
                report["login"]["method"] = "rest_api_basic_auth"
                report["login"]["notes"] = f"Authenticated as {user_data.get('name', username)} (role: {', '.join(user_data.get('roles', []))})"
                logger.info(f"REST API auth successful: {user_data.get('name')}")
            else:
                logger.info(f"REST API auth failed: {resp.status_code}")
                report["login"]["notes"] = f"REST API auth returned {resp.status_code}. May need Application Password."
                # Can still get public data without auth
        except Exception as e:
            logger.info(f"REST API auth error: {e}")

        # Step 3: Get pages (public pages available without auth, drafts need auth)
        try:
            await status_callback(task_id, "running", "Fetching pages via REST API...")
            params = {"per_page": 100, "status": "publish,draft,private", "orderby": "title", "order": "asc"}
            resp = await client.get(f"{api_root}/wp/v2/pages", params=params, auth=auth)
            if resp.status_code == 200:
                pages = resp.json()
                total_pages = int(resp.headers.get("X-WP-Total", len(pages)))
                for p in pages:
                    report["pages"].append({
                        "id": str(p.get("id", "")),
                        "title": p.get("title", {}).get("rendered", ""),
                        "slug": p.get("slug", ""),
                        "status": p.get("status", ""),
                        "template": p.get("template", ""),
                        "parent": str(p.get("parent", 0)),
                        "link": p.get("link", ""),
                    })
                logger.info(f"Found {len(report['pages'])} pages (total: {total_pages})")
            elif resp.status_code == 401:
                # Try without auth for public pages
                resp2 = await client.get(f"{api_root}/wp/v2/pages", params={"per_page": 100})
                if resp2.status_code == 200:
                    for p in resp2.json():
                        report["pages"].append({
                            "id": str(p.get("id", "")),
                            "title": p.get("title", {}).get("rendered", ""),
                            "slug": p.get("slug", ""),
                            "status": p.get("status", ""),
                            "link": p.get("link", ""),
                        })
        except Exception as e:
            report["errors"].append(f"Pages API error: {str(e)[:200]}")

        # Step 4: Get plugins (requires auth, WP 5.5+)
        if report["rest_api"]["auth_works"]:
            try:
                await status_callback(task_id, "running", "Fetching plugins via REST API...")
                resp = await client.get(f"{api_root}/wp/v2/plugins", auth=auth)
                if resp.status_code == 200:
                    plugins = resp.json()
                    for p in plugins:
                        name = p.get("name", "")
                        version = p.get("version", "")
                        is_active = p.get("status", "") == "active"
                        desc = p.get("description", {})
                        if isinstance(desc, dict):
                            desc = desc.get("raw", "")

                        plugin_info = {
                            "name": name,
                            "version": version,
                            "active": is_active,
                            "plugin_file": p.get("plugin", ""),
                            "description": str(desc)[:150],
                        }
                        report["plugins"].append(plugin_info)

                        # Detect SEO plugin
                        name_lower = name.lower()
                        if is_active:
                            if "rank math" in name_lower:
                                report["seo_plugin"] = {"name": "rankmath", "version": version}
                            elif "yoast" in name_lower and "seo" in name_lower:
                                report["seo_plugin"] = {"name": "yoast", "version": version}
                            elif "all in one seo" in name_lower:
                                report["seo_plugin"] = {"name": "aioseo", "version": version}

                            # Detect page builder
                            if "elementor" in name_lower:
                                report["page_builder"] = {"name": "elementor", "version": version}
                            elif "divi" in name_lower:
                                report["page_builder"] = {"name": "divi", "version": version}
                            elif "bricks" in name_lower:
                                report["page_builder"] = {"name": "bricks", "version": version}
                            elif "beaver" in name_lower:
                                report["page_builder"] = {"name": "beaver_builder", "version": version}

                            # Classic Editor
                            if "classic editor" in name_lower:
                                report["editor_type"] = "classic"

                    logger.info(f"Found {len(report['plugins'])} plugins ({sum(1 for p in report['plugins'] if p['active'])} active)")
                elif resp.status_code == 403:
                    report["errors"].append("Plugins endpoint forbidden (user may lack manage_plugins capability)")
                    logger.info("Plugins API returned 403")
            except Exception as e:
                report["errors"].append(f"Plugins API error: {str(e)[:200]}")

        # Step 5: Get theme info
        try:
            await status_callback(task_id, "running", "Fetching theme info...")
            # Try the themes endpoint (WP 5.0+)
            resp = await client.get(f"{api_root}/wp/v2/themes", auth=auth)
            if resp.status_code == 200:
                themes = resp.json()
                for t in themes:
                    if t.get("status") == "active":
                        report["theme"]["name"] = t.get("name", {}).get("rendered", t.get("name", ""))
                        if isinstance(report["theme"]["name"], dict):
                            report["theme"]["name"] = report["theme"]["name"].get("rendered", "")
                        report["theme"]["version"] = t.get("version", "")
                        report["theme"]["stylesheet"] = t.get("stylesheet", "")

                        # Detect block theme from theme_supports
                        theme_supports = t.get("theme_supports", {})
                        if theme_supports.get("block-templates") or theme_supports.get("block-template-parts"):
                            report["theme"]["type"] = "block"
                        else:
                            report["theme"]["type"] = "classic"
                        break
                logger.info(f"Active theme: {report['theme']['name']} ({report['theme']['type']})")
        except Exception as e:
            report["errors"].append(f"Themes API error: {str(e)[:200]}")

        # Step 6: Get settings (permalink structure, requires auth)
        if report["rest_api"]["auth_works"]:
            try:
                resp = await client.get(f"{api_root}/wp/v2/settings", auth=auth)
                if resp.status_code == 200:
                    settings = resp.json()
                    report["permalink_structure"] = settings.get("permalink_structure", "")
            except Exception:
                pass

        # Step 7: Get menus (WP 5.9+ with nav block support)
        try:
            resp = await client.get(f"{api_root}/wp/v2/menu-items", params={"per_page": 100}, auth=auth)
            if resp.status_code == 200:
                items = resp.json()
                for item in items:
                    report["menus"]["items"].append({
                        "title": item.get("title", {}).get("rendered", ""),
                        "url": item.get("url", ""),
                        "menu_order": item.get("menu_order", 0),
                        "parent": str(item.get("parent", 0)),
                        "type": item.get("type", ""),
                    })
            # Also try wp/v2/menus
            resp2 = await client.get(f"{api_root}/wp/v2/menus", auth=auth)
            if resp2.status_code == 200:
                menus = resp2.json()
                for m in menus:
                    report["menus"]["locations"].append({
                        "name": m.get("name", ""),
                        "slug": m.get("slug", ""),
                        "count": m.get("count", 0),
                    })
        except Exception as e:
            # Menus API not available on all WP versions
            logger.info(f"Menus API not available: {e}")

        # Step 8: Media count
        try:
            resp = await client.get(f"{api_root}/wp/v2/media", params={"per_page": 1}, auth=auth)
            if resp.status_code == 200:
                total = int(resp.headers.get("X-WP-Total", 0))
                report["media_stats"]["total_items"] = total
        except Exception:
            pass

        # Step 9: WP version from the homepage generator tag
        try:
            resp = await client.get(base_url, follow_redirects=True)
            if resp.status_code == 200:
                gen_match = re.search(r'content="WordPress (\d+\.\d+(?:\.\d+)?)"', resp.text)
                if gen_match:
                    report["wordpress"]["version"] = gen_match.group(1)
        except Exception:
            pass

    # Determine if we got enough data
    has_plugins = len(report["plugins"]) > 0
    has_pages = len(report["pages"]) > 0
    has_theme = bool(report["theme"]["name"])

    if has_plugins and has_pages and has_theme:
        logger.info("REST API provided complete data")
        return True
    elif has_pages or has_theme:
        logger.info("REST API provided partial data")
        return True  # Good enough — we got the essentials
    else:
        logger.info("REST API provided insufficient data")
        return False


# ── Browser fallback ─────────────────────────────────────────────────────

async def _try_browser(base_url, wp_admin_url, username, password, report, status_callback, task_id):
    """Fall back to browser-based login and navigation."""

    login_url = f"{base_url}/wp-login.php"
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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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

        await status_callback(task_id, "running", "Browser: attempting login...")

        # Try login URLs
        login_success = False
        for attempt_url in [login_url, wp_admin_url]:
            try:
                logger.info(f"Browser login attempt: {attempt_url}")
                await page.goto(attempt_url, wait_until="networkidle", timeout=20000)

                title = await page.title()
                current_url = page.url
                logger.info(f"Landed on: {current_url} (title: {title})")

                # Handle WAF/security challenges
                if "403" in title or "forbidden" in title.lower():
                    logger.info("Detected WAF challenge, waiting...")
                    for i in range(5):
                        await asyncio.sleep(3)
                        new_title = await page.title()
                        if "403" not in new_title:
                            logger.info("Challenge resolved")
                            break
                    else:
                        logger.warning(f"WAF challenge not resolved at {attempt_url}")
                        report["errors"].append(f"WAF blocked browser at {attempt_url}")
                        continue

                await asyncio.sleep(1)

                # Look for login form
                user_field = None
                try:
                    user_field = await page.wait_for_selector("#user_login, input[name='log']", timeout=5000)
                except Exception:
                    pass

                if user_field:
                    pass_field = await page.query_selector("#user_pass, input[name='pwd']")
                    if pass_field:
                        await user_field.fill(username)
                        await pass_field.fill(password)

                        remember = await page.query_selector("#rememberme")
                        if remember:
                            await remember.check()

                        submit = await page.query_selector("#wp-submit, input[type='submit']")
                        if submit:
                            await submit.click()
                        else:
                            await pass_field.press("Enter")

                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                        await asyncio.sleep(2)

                        if "/wp-admin" in page.url:
                            login_success = True
                            report["login"]["success"] = True
                            report["login"]["method"] = "browser"
                            report["login"]["notes"] = f"Browser login via {attempt_url}"
                            break
                elif "/wp-admin" in current_url:
                    login_success = True
                    report["login"]["success"] = True
                    report["login"]["method"] = "browser_session"
                    break

            except Exception as e:
                logger.warning(f"Browser login at {attempt_url} failed: {e}")
                continue

        if not login_success:
            report["errors"].append("Browser login failed (WAF or credentials)")
            return False

        # Take dashboard screenshot
        report["screenshots"]["dashboard"] = await _take_screenshot(page)

        # If we don't have plugins from REST API, get them from browser
        if not report["plugins"]:
            await status_callback(task_id, "running", "Browser: scanning plugins...")
            try:
                await page.goto(f"{base_url}/wp-admin/plugins.php", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                rows = await page.query_selector_all("tr.active, tr.inactive")
                for row in rows:
                    try:
                        is_active = "active" in (await row.get_attribute("class") or "")
                        name_el = await row.query_selector("td.plugin-title strong, .plugin-title strong")
                        name = await name_el.inner_text() if name_el else "Unknown"
                        report["plugins"].append({"name": name.strip(), "active": is_active})
                    except Exception:
                        continue
                report["screenshots"]["plugins"] = await _take_screenshot(page)
            except Exception as e:
                report["errors"].append(f"Browser plugin scan failed: {str(e)[:200]}")

        # If we don't have theme from REST API, get it from browser
        if not report["theme"]["name"]:
            try:
                await page.goto(f"{base_url}/wp-admin/themes.php", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                active = await page.query_selector(".theme.active .theme-name")
                if active:
                    name = await active.inner_text()
                    report["theme"]["name"] = re.sub(r"^Active:\s*", "", name).strip()
                # Check for block theme
                site_editor = await page.query_selector("a[href*='site-editor.php']")
                report["theme"]["type"] = "block" if site_editor else "classic"
            except Exception as e:
                report["errors"].append(f"Browser theme check failed: {str(e)[:200]}")

        # Pages list screenshot
        if report["pages"]:
            try:
                await page.goto(f"{base_url}/wp-admin/edit.php?post_type=page", wait_until="domcontentloaded", timeout=15000)
                report["screenshots"]["pages"] = await _take_screenshot(page)
            except Exception:
                pass

        return True

    except Exception as e:
        logger.exception(f"Browser fallback failed: {e}")
        report["errors"].append(f"Browser fatal: {str(e)[:200]}")
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
        f"WP {report['wordpress']['version']}" if report["wordpress"]["version"] else "WP version unknown",
        f"Theme: {report['theme']['name']} ({report['theme']['type']})" if report["theme"]["name"] else "Theme unknown",
        f"SEO: {report['seo_plugin']['name']}",
        f"Builder: {report['page_builder']['name']}",
        f"Editor: {report['editor_type']}",
        f"{len(report['pages'])} pages",
        f"{len(report['plugins'])} plugins ({sum(1 for p in report['plugins'] if p.get('active'))} active)",
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
