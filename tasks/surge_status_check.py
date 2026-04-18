"""
Surge Status Check
==================
Lightweight Surge probe used by the Client HQ hourly auto-heal cron
(/api/cron/check-surge-blocks → this agent's /admin/surge-status endpoint).

Spawns a fresh headless Chrome (keep_alive=False), logs in via raw DOM fill
(no LLM calls — this runs hourly and API cost must be zero), and reports:

    {
      "maintenance_active": bool,
      "credits": int | None,
      "logged_in": bool,
      "duration_seconds": float,
      "error": str | None,
    }

Runs OUTSIDE the audit asyncio lock so a long-running entity audit does not
block status probes. Never raises — all errors surface through the `error`
field so the caller (cron) can make policy decisions without try/except.
"""

import asyncio
import logging
import os
import re
import time
from typing import Optional

from browser_use import Browser

logger = logging.getLogger("agent.surge_status")

SURGE_URL = os.getenv("SURGE_URL", "https://www.surgeaiprotocol.com")
SURGE_EMAIL = os.getenv("SURGE_EMAIL", "")
SURGE_PASSWORD = os.getenv("SURGE_PASSWORD", "")

# Substrings (lowercased) that indicate Surge is in maintenance mode and
# refusing new runs. Matched against document.body.innerText on the post-
# login dashboard.
MAINTENANCE_SIGNALS = [
    "pushing system updates",
    "maintenance active",
    "new runs blocked",
]

# Ordered list of regexes that can find the remaining Surge credit count
# on the dashboard. First match wins. All flagged case-insensitive.
CREDITS_PATTERNS = [
    r"SURGE\s+(\d{1,6})\b",
    r"Balance[:\s]+(\d{1,6})\s+credits?",
    r"(\d{1,6})\s+remaining",
]


async def check_surge_status(timeout_seconds: int = 60) -> dict:
    """
    Probe Surge without disturbing the audit pipeline.

    Args:
        timeout_seconds: outer deadline in seconds. 60 is plenty for a fresh
                         Playwright spawn + DOM fill + login redirect.

    Returns:
        dict (never raises):
          maintenance_active  bool
          credits             int | None   (None if we could not parse)
          logged_in           bool
          duration_seconds    float
          error               str | None   (non-null if something went wrong)
    """
    start = time.time()
    result = {
        "maintenance_active": False,
        "credits": None,
        "logged_in": False,
        "duration_seconds": 0.0,
        "error": None,
    }

    if not SURGE_EMAIL or not SURGE_PASSWORD:
        result["error"] = "SURGE_EMAIL or SURGE_PASSWORD not configured"
        result["duration_seconds"] = round(time.time() - start, 2)
        return result

    browser: Optional[Browser] = None
    try:
        browser = Browser(
            keep_alive=False,
            headless=True,
            disable_security=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        async def _probe():
            # Create the underlying browser + navigate. Browser Use 0.12.x
            # implicitly starts the browser on first navigation when we use
            # get_current_page after a direct page open; we drive navigation
            # via evaluate() since we are not using the Agent here.
            page = await browser.get_current_page()
            if page is None:
                raise RuntimeError("Browser did not produce a page handle")

            # Navigate to Surge root and wait for it to settle
            await page.navigate(SURGE_URL)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                # networkidle can fail on long-running analytics; the dom
                # fill below tolerates a partially-loaded page.
                pass

            # Fill email + password via DOM evaluate. Dispatch input + change
            # events so React controlled inputs sync their internal state.
            filled = await page.evaluate(
                """(creds) => {
                    function setNative(el, value) {
                        const setter = Object.getOwnPropertyDescriptor(el.__proto__, 'value').set;
                        setter.call(el, value);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    const email = document.querySelector('input[type=email],input[name=email],input[placeholder*="mail" i]');
                    const pwd = document.querySelector('input[type=password],input[name=password]');
                    if (!email || !pwd) return { found: false };
                    setNative(email, creds.email);
                    setNative(pwd, creds.password);
                    return { found: true };
                }""",
                {"email": SURGE_EMAIL, "password": SURGE_PASSWORD},
            )

            if not filled or not filled.get("found"):
                raise RuntimeError("Login form inputs not found on Surge landing page")

            # Submit: try the enclosing form first, else dispatch Enter on
            # the password field. Surge's React login handles both paths.
            await page.evaluate(
                """() => {
                    const pwd = document.querySelector('input[type=password],input[name=password]');
                    if (!pwd) return;
                    if (pwd.form && typeof pwd.form.requestSubmit === 'function') {
                        pwd.form.requestSubmit();
                        return;
                    }
                    const btn = document.querySelector('button[type=submit],form button');
                    if (btn) { btn.click(); return; }
                    pwd.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
                }"""
            )

            # Poll URL + content for login confirmation. 25s deadline inside
            # the outer timeout gives the dashboard time to render.
            deadline = time.time() + 25
            content = ""
            while time.time() < deadline:
                try:
                    current_url = await page.get_url()
                except Exception:
                    current_url = ""
                try:
                    content = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    ) or ""
                except Exception:
                    content = ""

                url_ok = "/dashboard" in (current_url or "")
                content_ok = any(
                    re.search(pat, content, re.IGNORECASE) for pat in CREDITS_PATTERNS
                )
                if url_ok or content_ok:
                    result["logged_in"] = True
                    break
                await asyncio.sleep(1)

            if not result["logged_in"]:
                # Still try to classify — Surge sometimes shows the
                # maintenance banner BEFORE full login, which is an
                # important signal even without a logged-in dashboard.
                if content and any(s in content.lower() for s in MAINTENANCE_SIGNALS):
                    result["maintenance_active"] = True
                    result["error"] = "Did not reach logged-in state (maintenance banner detected)"
                else:
                    result["error"] = "Login did not complete within 25s"
                return

            # Final innerText read for classification
            try:
                content = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                ) or ""
            except Exception:
                pass

            content_lower = content.lower()
            result["maintenance_active"] = any(
                s in content_lower for s in MAINTENANCE_SIGNALS
            )

            for pat in CREDITS_PATTERNS:
                m = re.search(pat, content, re.IGNORECASE)
                if m:
                    try:
                        result["credits"] = int(m.group(1))
                    except (ValueError, IndexError):
                        pass
                    break

        await asyncio.wait_for(_probe(), timeout=timeout_seconds)

    except asyncio.TimeoutError:
        result["error"] = f"Surge status probe timed out after {timeout_seconds}s"
    except Exception as e:
        result["error"] = f"Surge status probe failed: {e}"
        logger.warning(f"check_surge_status error: {e}")
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

    result["duration_seconds"] = round(time.time() - start, 2)
    logger.info(
        f"Surge status probe: logged_in={result['logged_in']} "
        f"maintenance={result['maintenance_active']} credits={result['credits']} "
        f"duration={result['duration_seconds']}s error={result['error']}"
    )
    return result
