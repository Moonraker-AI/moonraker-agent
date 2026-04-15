"""
wp_scout.py

Playwright-based reconnaissance of a WordPress admin dashboard.
Logs in, navigates key admin pages, extracts environment info,
takes screenshots, and returns a structured report.

No LLM needed — pure DOM extraction. ~30-60 seconds total.

Report includes:
  - WordPress version
  - Active theme (name, type: classic vs block/FSE)
  - Installed/active plugins (with SEO plugin and page builder detection)
  - Editor type (Gutenberg, Classic Editor, Elementor, etc.)
  - Pages list (title, slug, status)
  - Menu structure
  - Permalink structure
  - Media library stats
"""

import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import httpx
from playwright.async_api import async_playwright

logger = logging.getLogger("moonraker.wp_scout")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


async def run_wp_scout(task_id, params, status_callback, env):
    """
    params:
        wp_admin_url: str - e.g. "https://moonraker.ai/wp-admin" or "https://moonraker.ai/admin-dashboard"
        wp_username: str
        wp_password: str
        client_slug: str (optional, for organizing results)
        callback_url: str (optional, Client HQ endpoint to receive results)
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

    await status_callback(task_id, "running", "Launching browser for WP scout...")

    # Derive the base site URL and login URL from the admin URL
    # Handle both /wp-admin and custom admin URLs
    base_url = wp_admin_url.rsplit("/wp-admin", 1)[0] if "/wp-admin" in wp_admin_url else wp_admin_url.rsplit("/", 1)[0]

    # Try standard login path first, fall back to direct admin URL
    login_url = f"{base_url}/wp-login.php"

    browser = None
    playwright_instance = None
    report = {
        "scout_version": "1.0",
        "scanned_at": datetime.utcnow().isoformat() + "Z",
        "wp_admin_url": wp_admin_url,
        "base_url": base_url,
        "login": {"success": False, "login_url_used": "", "notes": ""},
        "wordpress": {"version": "", "multisite": False},
        "theme": {"name": "", "version": "", "type": "", "parent_theme": ""},
        "seo_plugin": {"name": "none", "version": ""},
        "page_builder": {"name": "none", "version": ""},
        "editor_type": "gutenberg",  # default assumption
        "plugins": [],
        "pages": [],
        "menus": {"locations": [], "items": []},
        "permalink_structure": "",
        "media_stats": {"total_items": 0},
        "screenshots": {},
        "errors": [],
    }

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
        # Anti-detection: override navigator.webdriver
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        """)
        page = await context.new_page()
        page.set_default_timeout(20000)

        # ── STEP 1: Login ────────────────────────────────────────────────
        await status_callback(task_id, "running", "Step 1/7: Logging in...")

        login_success = False

        # Try standard wp-login.php first
        for attempt_url in [login_url, wp_admin_url]:
            try:
                logger.info(f"Attempting login at: {attempt_url}")
                resp = await page.goto(attempt_url, wait_until="networkidle", timeout=20000)

                current_url = page.url
                title = await page.title()
                logger.info(f"Landed on: {current_url} (title: {title})")

                # Handle SiteGround / WAF JS challenges (403 pages)
                if "403" in title or "forbidden" in title.lower() or "challenge" in title.lower():
                    logger.info(f"Detected security challenge at {attempt_url}, waiting for JS execution...")
                    # Wait for the JS challenge to complete and redirect
                    for wait_round in range(6):
                        await asyncio.sleep(3)
                        new_title = await page.title()
                        new_url = page.url
                        logger.info(f"Challenge wait {wait_round+1}: title='{new_title}', url={new_url}")
                        if "403" not in new_title and "forbidden" not in new_title.lower():
                            logger.info("Challenge resolved!")
                            break
                    else:
                        logger.warning(f"Security challenge did not resolve after 18s at {attempt_url}")
                        continue

                # Wait briefly for any JS rendering
                await asyncio.sleep(1)

                # Try to find the WP login form with an explicit wait
                user_field = None
                pass_field = None
                try:
                    user_field = await page.wait_for_selector(
                        "#user_login, input[name='log']", timeout=5000
                    )
                    pass_field = await page.wait_for_selector(
                        "#user_pass, input[name='pwd']", timeout=3000
                    )
                except Exception:
                    logger.info(f"No WP login form found at {attempt_url}")

                if user_field and pass_field:
                    report["login"]["login_url_used"] = attempt_url
                    await user_field.fill(wp_username)
                    await pass_field.fill(wp_password)

                    # Check for remember me
                    remember = await page.query_selector("#rememberme")
                    if remember:
                        await remember.check()

                    # Click submit
                    submit = await page.query_selector("#wp-submit, input[type='submit']")
                    if submit:
                        await submit.click()
                    else:
                        await pass_field.press("Enter")

                    # Wait for navigation after login
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await asyncio.sleep(2)

                    # Check if login succeeded
                    current_url = page.url
                    if "/wp-admin" in current_url or "dashboard" in current_url.lower():
                        login_success = True
                        report["login"]["success"] = True
                        report["login"]["notes"] = f"Logged in via {attempt_url}"
                        logger.info(f"Login successful. Now at: {current_url}")
                        break
                    elif "wp-login.php" in current_url:
                        # Still on login page — check for error
                        error_el = await page.query_selector("#login_error")
                        if error_el:
                            error_text = await error_el.inner_text()
                            report["login"]["notes"] = f"Login error: {error_text.strip()[:200]}"
                            logger.warning(f"Login failed at {attempt_url}: {error_text.strip()[:100]}")
                        continue
                elif "/wp-admin" in current_url:
                    # Already logged in (session cookie still valid)
                    login_success = True
                    report["login"]["success"] = True
                    report["login"]["login_url_used"] = attempt_url
                    report["login"]["notes"] = "Already authenticated (session cookie)"
                    break
            except Exception as e:
                logger.warning(f"Login attempt at {attempt_url} failed: {e}")
                continue

        if not login_success:
            report["login"]["notes"] = report["login"].get("notes") or "Could not find WP login form or login failed"
            report["errors"].append("Login failed — cannot proceed with scout")
            await status_callback(task_id, "complete", "Scout complete (login failed)")

            # Still return whatever we gathered
            await _send_results(task_id, report, callback_url, agent_api_key)
            return

        # Take screenshot of dashboard
        report["screenshots"]["dashboard"] = await _take_screenshot(page)
        logger.info("Dashboard screenshot captured")

        # ── STEP 2: WordPress version + dashboard info ───────────────────
        await status_callback(task_id, "running", "Step 2/7: Reading WP version and environment...")

        # WP version from the admin footer
        wp_version_el = await page.query_selector("#footer-upgrade, #wp-version")
        if wp_version_el:
            version_text = await wp_version_el.inner_text()
            version_match = re.search(r"(\d+\.\d+(?:\.\d+)?)", version_text)
            if version_match:
                report["wordpress"]["version"] = version_match.group(1)

        # Also try the generator meta tag approach via admin page source
        try:
            admin_html = await page.content()
            gen_match = re.search(r'content="WordPress (\d+\.\d+(?:\.\d+)?)"', admin_html)
            if gen_match and not report["wordpress"]["version"]:
                report["wordpress"]["version"] = gen_match.group(1)
        except Exception:
            pass

        # Check for multisite
        multisite_el = await page.query_selector("#wp-admin-bar-my-sites")
        report["wordpress"]["multisite"] = multisite_el is not None

        # ── STEP 3: Active theme ─────────────────────────────────────────
        await status_callback(task_id, "running", "Step 3/7: Checking active theme...")

        try:
            await page.goto(f"{base_url}/wp-admin/themes.php", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)

            # Active theme is the first .theme element with .active class
            active_theme = await page.query_selector(".theme.active")
            if active_theme:
                theme_name_el = await active_theme.query_selector(".theme-name")
                if theme_name_el:
                    theme_name = await theme_name_el.inner_text()
                    # "Active: Theme Name" -> "Theme Name"
                    report["theme"]["name"] = re.sub(r"^Active:\s*", "", theme_name).strip()

            # Check if this is a block theme by looking for Site Editor menu item
            site_editor_link = await page.query_selector("a[href*='site-editor.php']")
            if site_editor_link:
                report["theme"]["type"] = "block"
            else:
                # Check for customize.php (classic theme indicator)
                customizer_link = await page.query_selector("a[href*='customize.php']")
                report["theme"]["type"] = "classic" if customizer_link else "unknown"

            report["screenshots"]["themes"] = await _take_screenshot(page)

        except Exception as e:
            report["errors"].append(f"Theme check failed: {str(e)[:200]}")
            logger.warning(f"Theme check error: {e}")

        # ── STEP 4: Plugins ──────────────────────────────────────────────
        await status_callback(task_id, "running", "Step 4/7: Scanning plugins...")

        try:
            await page.goto(f"{base_url}/wp-admin/plugins.php", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)

            # Parse the plugins table
            plugin_rows = await page.query_selector_all("tr.active, tr.inactive")

            for row in plugin_rows:
                try:
                    is_active = "active" in (await row.get_attribute("class") or "")

                    name_el = await row.query_selector("td.plugin-title strong, .plugin-title strong")
                    name = await name_el.inner_text() if name_el else "Unknown"

                    version_el = await row.query_selector(".plugin-version-author-uri")
                    version_text = await version_el.inner_text() if version_el else ""
                    version_match = re.search(r"Version\s+(\S+)", version_text)
                    version = version_match.group(1) if version_match else ""

                    desc_el = await row.query_selector(".plugin-description p")
                    description = (await desc_el.inner_text())[:150] if desc_el else ""

                    plugin_info = {
                        "name": name.strip(),
                        "version": version,
                        "active": is_active,
                        "description": description.strip(),
                    }
                    report["plugins"].append(plugin_info)

                    # Detect SEO plugin
                    name_lower = name.strip().lower()
                    if is_active:
                        if "rank math" in name_lower:
                            report["seo_plugin"] = {"name": "rankmath", "version": version}
                        elif "yoast" in name_lower and "seo" in name_lower:
                            report["seo_plugin"] = {"name": "yoast", "version": version}
                        elif "all in one seo" in name_lower or "aioseo" in name_lower:
                            report["seo_plugin"] = {"name": "aioseo", "version": version}

                        # Detect page builder
                        if "elementor" in name_lower:
                            report["page_builder"] = {"name": "elementor", "version": version}
                        elif "divi" in name_lower:
                            report["page_builder"] = {"name": "divi", "version": version}
                        elif "beaver" in name_lower:
                            report["page_builder"] = {"name": "beaver_builder", "version": version}
                        elif "wpbakery" in name_lower or "js_composer" in name_lower:
                            report["page_builder"] = {"name": "wpbakery", "version": version}
                        elif "bricks" in name_lower:
                            report["page_builder"] = {"name": "bricks", "version": version}

                        # Detect Classic Editor
                        if "classic editor" in name_lower:
                            report["editor_type"] = "classic"

                except Exception as row_err:
                    logger.warning(f"Error parsing plugin row: {row_err}")
                    continue

            report["screenshots"]["plugins"] = await _take_screenshot(page)
            logger.info(f"Found {len(report['plugins'])} plugins ({sum(1 for p in report['plugins'] if p['active'])} active)")

        except Exception as e:
            report["errors"].append(f"Plugin scan failed: {str(e)[:200]}")
            logger.warning(f"Plugin scan error: {e}")

        # ── STEP 5: Pages ────────────────────────────────────────────────
        await status_callback(task_id, "running", "Step 5/7: Listing pages...")

        try:
            # Get all pages (increase per_page)
            await page.goto(
                f"{base_url}/wp-admin/edit.php?post_type=page&posts_per_page=100&post_status=all",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(1)

            page_rows = await page.query_selector_all("#the-list tr")

            for row in page_rows:
                try:
                    title_el = await row.query_selector(".row-title, a.row-title")
                    title = await title_el.inner_text() if title_el else ""

                    # Get the edit link to extract the post ID
                    edit_link = await row.query_selector("a.row-title")
                    post_id = ""
                    slug = ""
                    if edit_link:
                        href = await edit_link.get_attribute("href") or ""
                        id_match = re.search(r"post=(\d+)", href)
                        if id_match:
                            post_id = id_match.group(1)

                    # Get slug from the "View" link
                    view_link = await row.query_selector("span.view a")
                    if view_link:
                        view_href = await view_link.get_attribute("href") or ""
                        slug = view_href.rstrip("/").split("/")[-1] if view_href else ""

                    # Status from the row class or post-state span
                    status_el = await row.query_selector(".post-state")
                    status = (await status_el.inner_text()).strip(" —") if status_el else "Published"

                    # Date
                    date_el = await row.query_selector("td.date")
                    date_text = (await date_el.inner_text()).strip() if date_el else ""

                    report["pages"].append({
                        "id": post_id,
                        "title": title.strip(),
                        "slug": slug,
                        "status": status,
                        "date": date_text[:30],
                    })
                except Exception as row_err:
                    logger.warning(f"Error parsing page row: {row_err}")
                    continue

            report["screenshots"]["pages"] = await _take_screenshot(page)
            logger.info(f"Found {len(report['pages'])} pages")

        except Exception as e:
            report["errors"].append(f"Page scan failed: {str(e)[:200]}")
            logger.warning(f"Page scan error: {e}")

        # ── STEP 6: Menus ────────────────────────────────────────────────
        await status_callback(task_id, "running", "Step 6/7: Checking navigation menus...")

        try:
            if report["theme"]["type"] == "block":
                # Block themes use the Site Editor for navigation
                report["menus"]["notes"] = "Block theme — navigation managed via Site Editor"
                # Try to access the nav menus anyway (some block themes still support it)
                await page.goto(f"{base_url}/wp-admin/nav-menus.php", wait_until="domcontentloaded", timeout=10000)
            else:
                await page.goto(f"{base_url}/wp-admin/nav-menus.php", wait_until="domcontentloaded", timeout=15000)

            await asyncio.sleep(1)

            # Check if the menus page exists (some themes don't register locations)
            no_menus_msg = await page.query_selector(".manage-menus")

            # Get menu locations
            location_checkboxes = await page.query_selector_all("input[name^='menu-locations']")
            for cb in location_checkboxes:
                label = await page.evaluate(
                    """(el) => {
                        const label = el.closest('label') || el.parentElement;
                        return label ? label.textContent.trim() : '';
                    }""",
                    cb,
                )
                checked = await cb.is_checked()
                report["menus"]["locations"].append({
                    "name": label[:100],
                    "has_menu_assigned": checked,
                })

            # Get current menu items
            menu_items = await page.query_selector_all("#menu-to-edit .menu-item")
            for item in menu_items[:50]:  # Cap at 50
                try:
                    item_title_el = await item.query_selector(".menu-item-title")
                    item_type_el = await item.query_selector(".item-type")
                    item_title = await item_title_el.inner_text() if item_title_el else ""
                    item_type = await item_type_el.inner_text() if item_type_el else ""

                    # Check depth (submenu level) from class
                    classes = await item.get_attribute("class") or ""
                    depth = 0
                    depth_match = re.search(r"menu-item-depth-(\d+)", classes)
                    if depth_match:
                        depth = int(depth_match.group(1))

                    report["menus"]["items"].append({
                        "title": item_title.strip(),
                        "type": item_type.strip(),
                        "depth": depth,
                    })
                except Exception:
                    continue

            report["screenshots"]["menus"] = await _take_screenshot(page)
            logger.info(f"Found {len(report['menus']['items'])} menu items, {len(report['menus']['locations'])} locations")

        except Exception as e:
            report["errors"].append(f"Menu scan failed: {str(e)[:200]}")
            logger.warning(f"Menu scan error: {e}")

        # ── STEP 7: Settings ─────────────────────────────────────────────
        await status_callback(task_id, "running", "Step 7/7: Checking permalinks and settings...")

        try:
            await page.goto(f"{base_url}/wp-admin/options-permalink.php", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)

            # Find the checked permalink radio button
            checked_radio = await page.query_selector("input[name='selection']:checked")
            if checked_radio:
                value = await checked_radio.get_attribute("value") or ""
                report["permalink_structure"] = value

            # If custom structure, get it
            custom_el = await page.query_selector("#permalink_structure")
            if custom_el:
                custom_val = await custom_el.input_value()
                if custom_val:
                    report["permalink_structure"] = custom_val

        except Exception as e:
            report["errors"].append(f"Permalink check failed: {str(e)[:200]}")

        # Quick media stats
        try:
            await page.goto(f"{base_url}/wp-admin/upload.php?mode=list", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)

            # Total items from the "X items" count
            count_el = await page.query_selector(".displaying-num")
            if count_el:
                count_text = await count_el.inner_text()
                count_match = re.search(r"([\d,]+)\s+items?", count_text)
                if count_match:
                    report["media_stats"]["total_items"] = int(count_match.group(1).replace(",", ""))

            report["screenshots"]["media"] = await _take_screenshot(page)

        except Exception as e:
            report["errors"].append(f"Media check failed: {str(e)[:200]}")

        # ── Done ─────────────────────────────────────────────────────────
        logger.info(f"WP Scout complete for {wp_admin_url}")

        # Build summary
        summary_parts = [
            f"WP {report['wordpress']['version']}" if report["wordpress"]["version"] else "WP version unknown",
            f"Theme: {report['theme']['name']} ({report['theme']['type']})" if report["theme"]["name"] else "Theme unknown",
            f"SEO: {report['seo_plugin']['name']}",
            f"Builder: {report['page_builder']['name']}",
            f"Editor: {report['editor_type']}",
            f"{len(report['pages'])} pages",
            f"{len(report['plugins'])} plugins ({sum(1 for p in report['plugins'] if p['active'])} active)",
        ]
        summary = " | ".join(summary_parts)

        await status_callback(task_id, "complete", f"Scout complete: {summary}")

        # Send results
        await _send_results(task_id, report, callback_url, agent_api_key)

    except Exception as e:
        logger.exception(f"WP Scout failed: {e}")
        report["errors"].append(f"Fatal error: {str(e)[:300]}")
        await status_callback(task_id, "error", f"Scout failed: {str(e)[:200]}")
        await _send_results(task_id, report, callback_url, agent_api_key)

    finally:
        try:
            if browser:
                await browser.close()
            if playwright_instance:
                await playwright_instance.stop()
        except Exception:
            pass


async def _take_screenshot(page, max_width=1440, quality=60):
    """Take a screenshot and return as base64 JPEG string."""
    try:
        screenshot_bytes = await page.screenshot(
            type="jpeg",
            quality=quality,
            full_page=False,
        )
        return base64.b64encode(screenshot_bytes).decode("utf-8")
    except Exception as e:
        logger.warning(f"Screenshot failed: {e}")
        return ""


async def _send_results(task_id, report, callback_url, agent_api_key):
    """Post results back to Client HQ or just log them."""
    if not callback_url:
        logger.info(f"No callback URL — storing results in task data only")
        return

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                callback_url,
                json={
                    "task_id": task_id,
                    "report": report,
                },
                headers={
                    "Authorization": f"Bearer {agent_api_key}",
                    "Content-Type": "application/json",
                },
            )
            logger.info(f"Callback response: {resp.status_code}")
    except Exception as e:
        logger.error(f"Callback failed: {e}")
