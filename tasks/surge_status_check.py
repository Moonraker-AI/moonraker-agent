"""
Surge Status Check
==================
Lightweight Surge probe used by the Client HQ hourly auto-heal cron
(/api/cron/check-surge-blocks -> this agent's /ops/surge-status endpoint).

Spawns a fresh headless Chromium via Playwright directly (NOT Browser Use).
Browser Use is an LLM-driven agent layer designed around an Agent/Task loop,
and its Page handle is only populated after the Agent opens a page. Since
this probe has zero LLM work to do (it just fills a login form and reads
the dashboard), Playwright is both simpler and more deterministic.

Contract:

    {
      "maintenance_active": bool,
      "credits": int | None,
      "logged_in": bool,
      "duration_seconds": float,
      "error": str | None,
    }

Runs OUTSIDE the audit asyncio lock so a long-running entity audit does not
block status probes. Never raises - all errors surface through the `error`
field so the caller (cron) can make policy decisions without try/except.
"""

import asyncio
import logging
import os
import re
import time
from typing import Any

from playwright.async_api import async_playwright

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
                         browser spawn + DOM fill + login redirect.

    Returns:
        dict (never raises):
          maintenance_active  bool
          credits             int | None   (None if we could not parse)
          logged_in           bool
          duration_seconds    float
          error               str | None   (non-null if something went wrong)
    """
    start = time.time()
    result: dict[str, Any] = {
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

    try:
        await asyncio.wait_for(_probe(result), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        result["error"] = f"Surge status probe timed out after {timeout_seconds}s"
    except Exception as e:
        result["error"] = f"Surge status probe failed: {e}"
        logger.warning(f"check_surge_status error: {e}")

    result["duration_seconds"] = round(time.time() - start, 2)
    logger.info(
        f"Surge status probe: logged_in={result['logged_in']} "
        f"maintenance={result['maintenance_active']} credits={result['credits']} "
        f"duration={result['duration_seconds']}s error={result['error']}"
    )
    return result


async def _login_form_present(page) -> bool:
    """Return True when both an email and password input are in the DOM."""
    try:
        return bool(await page.evaluate(
            """() => {
                const e = document.querySelector('input[type=email],input[name=email],input[placeholder*="mail" i]');
                const p = document.querySelector('input[type=password],input[name=password]');
                return !!(e && p);
            }"""
        ))
    except Exception:
        return False


async def _probe(result: dict) -> None:
    """Inner probe - writes into `result` in place. May raise; wrapped by caller."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(SURGE_URL, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                # networkidle can fail on long-running analytics; the DOM fill
                # below tolerates a partially-loaded page.
                pass

            # SURGE_URL points at the marketing root (surgeaiprotocol.com),
            # which has no form - the login lives behind a "Sign In" link
            # at /sign-in. Click-through is more resilient than hardcoding
            # the path, since the link text is UI-facing and stable.
            if not await _login_form_present(page):
                try:
                    clicked = await page.evaluate(
                        """() => {
                            const els = Array.from(document.querySelectorAll('button,a'));
                            const el = els.find(e => {
                                const t = (e.innerText || '').trim().toLowerCase();
                                return t === 'sign in' || t === 'log in' || t === 'login';
                            });
                            if (el) { el.click(); return true; }
                            return false;
                        }"""
                    )
                    if clicked:
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=10000)
                        except Exception:
                            pass
                        # Give React time to mount the form
                        await asyncio.sleep(1.5)
                except Exception as click_err:
                    logger.warning(f"Sign-in click failed: {click_err}")

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

            # Submit: try the enclosing form first, else the submit button,
            # else dispatch Enter on the password field.
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
                    current_url = page.url
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
                # Still try to classify - Surge sometimes shows the
                # maintenance banner BEFORE full login, which is an
                # important signal even without a logged-in dashboard.
                if content and any(s in content.lower() for s in MAINTENANCE_SIGNALS):
                    result["maintenance_active"] = True
                    result["error"] = (
                        "Did not reach logged-in state (maintenance banner detected)"
                    )
                else:
                    result["error"] = "Login did not complete within 25s"
                return

            # Post-login settling: the React dashboard hydrates in stages.
            # A read immediately after URL change can miss the maintenance
            # banner AND the credit counter. Poll until we see either a
            # credit count OR a maintenance signal, for up to 8s, so the
            # final classification read catches a fully-rendered page.
            settle_deadline = time.time() + 8
            classify_ready = False
            while time.time() < settle_deadline:
                try:
                    content = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    ) or ""
                except Exception:
                    content = ""
                lowered = content.lower()
                has_credits = any(
                    re.search(pat, content, re.IGNORECASE) for pat in CREDITS_PATTERNS
                )
                has_maint = any(s in lowered for s in MAINTENANCE_SIGNALS)
                if has_credits or has_maint:
                    classify_ready = True
                    break
                await asyncio.sleep(0.5)

            if not classify_ready:
                # Still do one final read so `content` reflects the latest
                # DOM state even if neither signal rendered (e.g. Surge UI
                # changed layout or moved the credit counter behind a
                # widget we don't scan).
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
        finally:
            try:
                await browser.close()
            except Exception:
                pass
