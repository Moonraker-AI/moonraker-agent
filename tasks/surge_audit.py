"""
Surge Audit Automation
======================
Runs a Surge entity audit for a given client.

Architecture (hybrid approach to minimize API costs):
  Phase 1: Browser Use agent handles login + form fill + submit (~2 min, ~$0.05-0.10)
  Phase 2: Raw Playwright waits for Surge to complete (~20-35 min, $0 API cost)
  Phase 3: Raw Playwright extracts results via Copy button ($0 API cost)
  Phase 4: POST results to Client HQ + send notifications

Browser Use is only used for the interactive UI phase where LLM intelligence
handles form fields, modals, and UI quirks. The long wait and mechanical
extraction are done with raw Playwright to avoid burning API credits.
"""

import asyncio
import logging
import os
import time
from typing import Callable

import httpx
from browser_use import Agent, Browser
from browser_use.llm import ChatAnthropic
from playwright.async_api import Page

logger = logging.getLogger("agent.surge")

# ── Config ───────────────────────────────────────────────────────────────────

SURGE_URL = os.getenv("SURGE_URL", "https://www.surgeaiprotocol.com")
SURGE_EMAIL = os.getenv("SURGE_EMAIL", "")
SURGE_PASSWORD = os.getenv("SURGE_PASSWORD", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLIENT_HQ_URL = os.getenv("CLIENT_HQ_URL", "https://clients.moonraker.ai")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Timing
POLL_INTERVAL_SECONDS = 30       # Check every 30s during Surge processing
MAX_WAIT_MINUTES = 50            # Give up after 50 minutes
FORM_FILL_TIMEOUT_SECONDS = 120  # Max time for login + form fill phase


# ── Main entry point ─────────────────────────────────────────────────────────

async def execute_surge_audit(
    task_id: str,
    tasks: dict,
    update_task: Callable,
):
    """
    Full Surge audit lifecycle:
    1. Login + fill form (Browser Use)
    2. Wait for completion (Playwright)
    3. Extract results (Playwright)
    4. Post to Client HQ + notify
    """
    req = tasks[task_id]["request"]
    practice_name = req["practice_name"]
    website_url = req["website_url"]
    city = req["city"]
    state = req["state"]
    geo_target_override = req.get("geo_target") or ""
    gbp_link = req.get("gbp_link") or ""
    audit_id = req["audit_id"]
    client_slug = req["client_slug"]

    browser = None

    try:
        # ── Phase 1: Login + Form Fill (Browser Use) ─────────────────────
        update_task(task_id, "login", "Launching browser and logging into Surge")

        browser = Browser(
            keep_alive=True,
            headless=True,
            disable_security=True,  # Needed for clipboard access
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        llm = ChatAnthropic(
            model="claude-sonnet-4-20250514",
            api_key=ANTHROPIC_API_KEY,
            timeout=120,
        )

        # Build the form fill task prompt
        geo_target = geo_target_override or (f"{city}, {state}" if city and state else city or state or "")
        gbp_instruction = ""
        if gbp_link:
            gbp_instruction = f'\n       - GBP Location: {gbp_link}'

        agent_task = f"""Complete these steps in exact order:

1. Go to {SURGE_URL}
2. You should see a login form. Enter email "{SURGE_EMAIL}" and password "{SURGE_PASSWORD}", then submit the form to log in.
3. After login, you should be on the dashboard. Click "New Analysis" in the top navigation bar.
4. If a modal or popup appears asking about starting a new analysis, confirm/accept it.
5. Fill in the analysis form with these exact values:
   - Search Query: {practice_name}
   - Brand / Entity Name: {practice_name}
   - Target URL: {website_url}
   - Geographic Target: {geo_target}
   - Category / Industry: Mental health{gbp_instruction}
   - Leave ALL other fields as their defaults (Entity Type, Model Selection, checkboxes, etc.)
   - Do NOT click Keyword Discovery or Query Intelligence
   - Do NOT add any Ranking Terms
6. Click the "Build My Surge Protocol" button to start the analysis.
7. STOP after clicking the button. Do not navigate away from the page. Your task is complete."""

        agent = Agent(
            task=agent_task,
            llm=llm,
            browser=browser,
            max_actions_per_step=4,
        )

        update_task(task_id, "filling_form", "Filling Surge analysis form")
        result = await agent.run(max_steps=25)

        logger.info(f"Browser Use agent completed form fill phase for {practice_name}")

        # ── Phase 2: Wait for Surge completion (Raw Playwright) ──────────
        update_task(
            task_id, "waiting_for_surge",
            "Surge is processing the audit (typically 20 to 35 minutes)"
        )

        # Get the Playwright page from Browser Use's browser
        page = await browser.get_current_page()
        if not page:
            raise RuntimeError(
                "Could not access Playwright page from Browser Use. "
                "The browser may have closed unexpectedly."
            )

        # Grant clipboard permissions
        try:
            await page.context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin=SURGE_URL,
            )
        except Exception as e:
            logger.warning(f"Could not grant clipboard permissions: {e}")

        # Wait for Surge to finish processing
        start_wait = time.time()
        max_wait = MAX_WAIT_MINUTES * 60
        completed = False
        last_status_update = 0

        while (time.time() - start_wait) < max_wait:
            elapsed_min = int((time.time() - start_wait) / 60)

            # Update status every 2 minutes so Client HQ shows progress
            if elapsed_min > last_status_update:
                last_status_update = elapsed_min
                update_task(
                    task_id, "waiting_for_surge",
                    f"Surge processing ({elapsed_min} min elapsed, typically 20 to 35 min)"
                )

            # Check for completion indicators
            try:
                content = await page.content()

                # Check for "Run completed in" text — definitive completion signal
                if "Run completed in" in content:
                    logger.info(
                        f"Surge completed for {practice_name} after {elapsed_min} min"
                    )
                    completed = True
                    break

                # Check for results page indicators (backup detection)
                if "Signal health" in content and "PAGE VARIANCE SCORE" in content:
                    logger.info(
                        f"Results page detected for {practice_name} after {elapsed_min} min"
                    )
                    completed = True
                    break

                # Check for error states
                if "error" in content.lower() and "analysis failed" in content.lower():
                    raise RuntimeError("Surge reported an analysis failure on the page")

                # Check for credit exhaustion
                if "insufficient credits" in content.lower() or "no credits" in content.lower():
                    update_task(
                        task_id, "credits_exhausted",
                        "Surge credits exhausted. Contact the Surge team to re-up.",
                        error="Credits exhausted",
                    )
                    from utils.notifications import send_credits_notification
                    await send_credits_notification(practice_name, client_slug)
                    return

            except Exception as e:
                logger.warning(f"Error checking page during wait: {e}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        if not completed:
            raise RuntimeError(
                f"Surge did not complete within {MAX_WAIT_MINUTES} minutes"
            )

        # ── Phase 3: Extract results (Raw Playwright) ────────────────────
        update_task(task_id, "extracting", "Extracting audit results from Surge")

        # Brief pause for page to fully render
        await asyncio.sleep(3)

        surge_data = await _extract_surge_data(page)

        if not surge_data or len(surge_data) < 100:
            raise RuntimeError(
                f"Extracted data seems too short ({len(surge_data or '')} chars). "
                "The Copy button may not have worked correctly."
            )

        logger.info(
            f"Extracted {len(surge_data)} chars of Surge data for {practice_name}"
        )

        # ── Phase 4: Post results to Client HQ ──────────────────────────
        update_task(task_id, "posting_results", "Sending results to Client HQ for processing")

        await _post_results_to_client_hq(audit_id, surge_data)

        # ── Phase 5: Send notifications ──────────────────────────────────
        from utils.notifications import send_success_notification
        await send_success_notification(
            practice_name=practice_name,
            client_slug=client_slug,
            audit_id=audit_id,
            duration_minutes=int((time.time() - start_wait) / 60),
            data_length=len(surge_data),
        )

        update_task(task_id, "complete", f"Audit complete for {practice_name}")

    except Exception as e:
        logger.exception(f"Surge audit failed for {req.get('practice_name', 'unknown')}")
        raise  # Re-raise so server.py handles notification

    finally:
        # Always close the browser
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_playwright_page(agent: Agent, browser: Browser) -> Page | None:
    """
    Extract the active Playwright Page from Browser Use internals.
    Browser Use wraps Playwright, so we need to reach into its objects.
    Tries multiple access patterns for compatibility across versions.
    """
    # Pattern 1: agent.browser_context has a current_page
    try:
        if hasattr(agent, "browser_context") and agent.browser_context:
            ctx = agent.browser_context
            if hasattr(ctx, "current_page") and ctx.current_page:
                return ctx.current_page
            if hasattr(ctx, "get_current_page"):
                return await ctx.get_current_page()
    except Exception as e:
        logger.debug(f"Pattern 1 failed: {e}")

    # Pattern 2: browser object has context with pages
    try:
        if hasattr(browser, "playwright_browser") and browser.playwright_browser:
            contexts = browser.playwright_browser.contexts
            if contexts and contexts[0].pages:
                return contexts[0].pages[-1]
    except Exception as e:
        logger.debug(f"Pattern 2 failed: {e}")

    # Pattern 3: browser has _browser or _playwright_browser
    try:
        for attr in ("_browser", "_playwright_browser", "browser"):
            pw_browser = getattr(browser, attr, None)
            if pw_browser and hasattr(pw_browser, "contexts"):
                contexts = pw_browser.contexts
                if contexts and contexts[0].pages:
                    return contexts[0].pages[-1]
    except Exception as e:
        logger.debug(f"Pattern 3 failed: {e}")

    # Pattern 4: browser_context has _context (Playwright BrowserContext)
    try:
        if hasattr(agent, "browser_context") and agent.browser_context:
            ctx = agent.browser_context
            for attr in ("_context", "context", "playwright_context"):
                pw_ctx = getattr(ctx, attr, None)
                if pw_ctx and hasattr(pw_ctx, "pages") and pw_ctx.pages:
                    return pw_ctx.pages[-1]
    except Exception as e:
        logger.debug(f"Pattern 4 failed: {e}")

    logger.error("All patterns to access Playwright page failed")
    return None


async def _extract_surge_data(page: Page) -> str:
    """
    Extract the Surge audit data from the results page.

    Strategy:
    1. Inject clipboard interceptor
    2. Click the "Copy raw text" button
    3. Read intercepted text
    4. Fallback: try navigator.clipboard.readText()
    5. Fallback: extract from Export Report if available
    """
    surge_data = None

    # Strategy 1: Inject clipboard interceptor + click Copy button
    try:
        # Inject interceptor before clicking
        await page.evaluate("""() => {
            window.__surgeCopiedText = null;

            // Intercept clipboard.writeText
            const origWriteText = navigator.clipboard.writeText.bind(navigator.clipboard);
            navigator.clipboard.writeText = async (text) => {
                window.__surgeCopiedText = text;
                return origWriteText(text);
            };

            // Also intercept execCommand('copy') for older approach
            const origExecCommand = document.execCommand.bind(document);
            document.execCommand = function(cmd, ...args) {
                if (cmd === 'copy') {
                    const selection = window.getSelection();
                    if (selection) {
                        window.__surgeCopiedText = selection.toString();
                    }
                }
                return origExecCommand(cmd, ...args);
            };
        }""")

        # Find and click the Copy button between Signal health and trust signals
        # The button has title="Copy raw text"
        copy_buttons = await page.query_selector_all('button[title="Copy raw text"]')

        if copy_buttons:
            # Click the FIRST one (the main copy button between sections)
            await copy_buttons[0].click()
            await asyncio.sleep(2)

            # Read intercepted text
            surge_data = await page.evaluate("window.__surgeCopiedText")

            if surge_data:
                logger.info(f"Extracted {len(surge_data)} chars via clipboard intercept")
                return surge_data

    except Exception as e:
        logger.warning(f"Clipboard intercept strategy failed: {e}")

    # Strategy 2: Try reading clipboard directly
    try:
        await page.evaluate("""async () => {
            // Re-click the button
            const btn = document.querySelector('button[title="Copy raw text"]');
            if (btn) btn.click();
        }""")
        await asyncio.sleep(2)

        surge_data = await page.evaluate("navigator.clipboard.readText()")
        if surge_data and len(surge_data) > 50:
            logger.info(f"Extracted {len(surge_data)} chars via clipboard.readText()")
            return surge_data

    except Exception as e:
        logger.warning(f"Direct clipboard read failed: {e}")

    # Strategy 3: Click all section Copy buttons and concatenate
    try:
        copy_buttons = await page.query_selector_all('button[title="Copy raw text"]')
        if not copy_buttons:
            # Try broader selector
            copy_buttons = await page.query_selector_all(
                'button:has-text("Copy")'
            )

        all_text = []
        for btn in copy_buttons:
            await page.evaluate("""() => { window.__surgeCopiedText = null; }""")
            await btn.click()
            await asyncio.sleep(1)
            text = await page.evaluate("window.__surgeCopiedText")
            if text:
                all_text.append(text)

        if all_text:
            surge_data = "\n\n---\n\n".join(all_text)
            logger.info(
                f"Extracted {len(surge_data)} chars via {len(all_text)} copy buttons"
            )
            return surge_data

    except Exception as e:
        logger.warning(f"Multi-button copy strategy failed: {e}")

    # Strategy 4: Extract page text content as last resort
    try:
        surge_data = await page.evaluate("""() => {
            // Try to find the main content area
            const main = document.querySelector('main')
                || document.querySelector('[class*="report"]')
                || document.querySelector('[class*="result"]')
                || document.body;
            return main.innerText;
        }""")

        if surge_data and len(surge_data) > 200:
            logger.warning(
                f"Using page text fallback: {len(surge_data)} chars. "
                "Copy button extraction failed."
            )
            return surge_data

    except Exception as e:
        logger.warning(f"Page text fallback failed: {e}")

    raise RuntimeError("All extraction strategies failed. Could not get Surge data.")


async def _post_results_to_client_hq(audit_id: str, surge_data: str):
    """POST extracted Surge data to Client HQ's process-entity-audit endpoint."""
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{CLIENT_HQ_URL}/api/process-entity-audit",
            json={
                "audit_id": audit_id,
                "surge_data": surge_data,
            },
            headers={
                "Content-Type": "application/json",
                # Use Supabase service role for auth (same as internal API calls)
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
        )

        if response.status_code != 200:
            body = response.text[:500]
            raise RuntimeError(
                f"Client HQ returned {response.status_code}: {body}"
            )

        logger.info(f"Successfully posted audit results for audit_id={audit_id}")
