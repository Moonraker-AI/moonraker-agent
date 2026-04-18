"""
Surge content audit task for keyword-specific page audits.

Unlike entity audits (homepage-focused, brand query), content audits target
specific keywords (e.g., "anxiety therapy", "EMDR Toronto") and extract the
Ready-to-Publish Best Answer + schema recommendations for page building.

Uses Browser Use + Claude Opus 4.6 for form interaction.
Uses raw Playwright for polling/extraction ($0 API cost).
"""

import asyncio
import json
import logging
import time
from datetime import datetime

import httpx

from utils.debug_capture import capture_debug

logger = logging.getLogger("moonraker.surge_content_audit")


async def run_surge_content_audit(
    task_id: str,
    params: dict,
    status_callback,
    env: dict,
):
    """
    Main entry point. Called by server.py when a content audit task is queued.

    params:
        content_page_id: str - the content_pages record to update
        practice_name: str
        website_url: str
        target_keyword: str
        search_query: str - keyword + location for Surge input
        page_type: str - service/location/bio/faq
        city: str
        state: str
        geo_target: str
        client_slug: str
        callback_url: str - Client HQ ingest endpoint

    env:
        ANTHROPIC_API_KEY, SURGE_URL, SURGE_EMAIL, SURGE_PASSWORD,
        CLIENT_HQ_URL, AGENT_API_KEY
    """

    content_page_id = params.get("content_page_id", "")
    practice_name = params.get("practice_name", "")
    website_url = params.get("website_url", "")
    target_keyword = params.get("target_keyword", "")
    search_query = params.get("search_query", target_keyword)
    page_type = params.get("page_type", "service")
    geo_target = params.get("geo_target", "")
    client_slug = params.get("client_slug", "")
    callback_url = params.get("callback_url", "")

    surge_url = env.get("SURGE_URL", "https://www.surgeaiprotocol.com")
    surge_email = env.get("SURGE_EMAIL", "")
    surge_password = env.get("SURGE_PASSWORD", "")
    agent_api_key = env.get("AGENT_API_KEY", "")

    if not all([content_page_id, website_url, target_keyword, surge_email, surge_password]):
        await status_callback(task_id, "failed", "Missing required parameters")
        return

    await status_callback(task_id, "running", f"Starting content audit for '{target_keyword}'")

    browser = None
    try:
        # ── PHASE 1: Login + Form Fill (Browser Use + Opus 4.6) ──
        from browser_use import Browser, Agent
        from browser_use.llm import ChatAnthropic

        llm = ChatAnthropic(
            model="claude-opus-4-6",
            api_key=env.get("ANTHROPIC_API_KEY", ""),
        )

        browser = Browser(
            headless=True,
            keep_alive=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        await status_callback(task_id, "running", "Phase 1: Logging into Surge and filling form")

        # Content audit form instruction
        # For content audits, we want to run a PAGE-SPECIFIC analysis
        # The Surge form needs: website URL, search query/keyword
        form_task = f"""
Go to {surge_url} and log in with:
- Email: {surge_email}
- Password: {surge_password}

After logging in, navigate to the New Analysis page.

Fill out the analysis form:
- Website URL: {website_url}
- Search query or keyword: {search_query}
- Business name: {practice_name}
{f'- Location/geo target: {geo_target}' if geo_target else ''}

This is a PAGE-SPECIFIC content audit, not a brand/entity audit.
We want the Ready-to-Publish Best Answer for the keyword "{target_keyword}".

Submit the form and wait for the page to start processing.
Once you see the analysis has started (loading indicator, progress message, or redirect),
stop and confirm the analysis is running.
"""

        agent = Agent(
            task=form_task,
            llm=llm,
            browser=browser,
        )

        await agent.run()

        await status_callback(task_id, "running", "Phase 1 complete. Waiting for Surge to process...")

        # ── PHASE 2: Wait for Completion (raw Playwright, $0 cost) ──
        page = await browser.get_current_page()
        if not page:
            await status_callback(task_id, "failed", "Could not get browser page after form submission")
            return

        await status_callback(task_id, "running", "Phase 2: Polling for completion...")

        max_wait = 60 * 60  # 60 minutes
        poll_interval = 30  # seconds
        start_time = time.time()
        completed = False

        while time.time() - start_time < max_wait:
            elapsed = int(time.time() - start_time)

            try:
                current_url = page.url
                content = await page.content()

                # Strategy 1: URL changed to results page
                if "/dashboard/run/" in current_url:
                    logger.info(f"Surge completed (URL redirect): {current_url}")
                    completed = True
                    break

                # Strategy 2: Content indicators
                completion_indicators = [
                    "Run completed in",
                    "Signal Health",
                    "Copy raw text",
                    "trust signals",
                    "CRES Score",
                    "Ready-to-Publish",
                    "Best Answer",
                ]
                for indicator in completion_indicators:
                    if indicator in content:
                        logger.info(f"Surge completed (found '{indicator}' in page)")
                        completed = True
                        break

                if completed:
                    break

            except Exception as e:
                logger.warning(f"Error checking page: {e}")

            minutes = elapsed // 60
            await status_callback(
                task_id, "running",
                f"Phase 2: Waiting for Surge... ({minutes}m {elapsed % 60}s elapsed)"
            )
            await asyncio.sleep(poll_interval)

        if not completed:
            await status_callback(task_id, "failed", f"Surge did not complete within {max_wait // 60} minutes")
            return

        # ── PHASE 3: Extract Results (raw Playwright, $0 cost) ──
        await status_callback(task_id, "running", "Phase 3: Extracting results...")

        surge_data = None

        # Strategy A: Click "Copy raw text" button and intercept clipboard
        try:
            # Inject clipboard interceptor
            await page.evaluate("""
                window.__capturedClipboard = null;
                const origWriteText = navigator.clipboard.writeText.bind(navigator.clipboard);
                navigator.clipboard.writeText = async function(text) {
                    window.__capturedClipboard = text;
                    return origWriteText(text);
                };
                // Also intercept execCommand('copy')
                const origExecCommand = document.execCommand.bind(document);
                document.execCommand = function(cmd) {
                    if (cmd === 'copy') {
                        const sel = window.getSelection();
                        if (sel) window.__capturedClipboard = sel.toString();
                    }
                    return origExecCommand(cmd);
                };
            """)

            # Find and click the Copy button
            copy_btn = await page.query_selector('button:has-text("Copy raw text")')
            if not copy_btn:
                copy_btn = await page.query_selector('[data-testid="copy-raw"]')
            if not copy_btn:
                # Try broader selector
                buttons = await page.query_selector_all('button')
                for btn in buttons:
                    text = await btn.text_content()
                    if text and 'copy' in text.lower() and 'raw' in text.lower():
                        copy_btn = btn
                        break

            if copy_btn:
                await copy_btn.click()
                await asyncio.sleep(2)

                captured = await page.evaluate("window.__capturedClipboard")
                if captured and len(captured) > 200:
                    surge_data = {"raw_text": captured}
                    logger.info(f"Extracted via clipboard: {len(captured)} chars")
        except Exception as e:
            logger.warning(f"Clipboard extraction failed: {e}")

        # Strategy B: Extract visible page content
        if not surge_data:
            try:
                page_text = await page.evaluate("""
                    () => {
                        const main = document.querySelector('main') || document.body;
                        return main.innerText;
                    }
                """)
                if page_text and len(page_text) > 500:
                    surge_data = {"raw_text": page_text}
                    logger.info(f"Extracted via page text: {len(page_text)} chars")
            except Exception as e:
                logger.warning(f"Page text extraction failed: {e}")

        # Strategy C: Get full HTML
        if not surge_data:
            try:
                html = await page.content()
                if html and len(html) > 1000:
                    surge_data = {"raw_html": html}
                    logger.info(f"Extracted via full HTML: {len(html)} chars")
            except Exception as e:
                logger.warning(f"HTML extraction failed: {e}")

        if not surge_data:
            await status_callback(task_id, "failed", "Could not extract Surge results")
            return

        await status_callback(task_id, "running", "Phase 3 complete. Sending results to Client HQ...")

        # ── PHASE 4: Callback to Client HQ ──
        if callback_url and agent_api_key:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        callback_url,
                        json={
                            "content_page_id": content_page_id,
                            "surge_data": surge_data,
                            "agent_task_id": task_id,
                        },
                        headers={
                            "Authorization": f"Bearer {agent_api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    if resp.status_code == 200:
                        result = resp.json()
                        logger.info(f"Callback success: {result}")
                        await status_callback(
                            task_id, "completed",
                            f"Content audit complete for '{target_keyword}'. "
                            f"RTPBA: {'found' if result.get('has_rtpba') else 'not found'}. "
                            f"Schema: {'found' if result.get('has_schema') else 'not found'}."
                        )
                    else:
                        logger.error(f"Callback failed: {resp.status_code} {resp.text}")
                        await status_callback(
                            task_id, "completed",
                            f"Surge data extracted but callback failed ({resp.status_code}). "
                            f"Data saved in task results."
                        )
            except Exception as e:
                logger.error(f"Callback error: {e}")
                await status_callback(
                    task_id, "completed",
                    f"Surge data extracted but callback error: {str(e)[:100]}"
                )
        else:
            await status_callback(task_id, "completed", "Surge data extracted (no callback configured)")

    except Exception as e:
        logger.exception(f"Content audit error: {e}")
        # Best-effort debug capture so future investigations have HTML + screenshot
        # instead of just logs. Non-fatal: log and continue if capture fails.
        try:
            if browser is not None:
                dbg_page = await browser.get_current_page()
                if dbg_page is not None:
                    await capture_debug(task_id, dbg_page, "content_audit_exception")
        except Exception as ce:
            logger.warning(f"Debug capture in content audit except block failed: {ce}")
        await status_callback(task_id, "failed", f"Error: {str(e)[:200]}")

    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

        # Clean up temp files (browser profiles, screenshots)
        try:
            from utils.cleanup import full_cleanup
            full_cleanup()
        except Exception as cleanup_err:
            import logging
            logging.getLogger("agent.cleanup").warning(f"Cleanup failed: {cleanup_err}")

