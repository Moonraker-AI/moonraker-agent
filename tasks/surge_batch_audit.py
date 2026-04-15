"""
Surge batch audit task for multi-page keyword audits.

Automates the Surge Batch workflow:
  Phase 1: Browser Use (Opus 4.6) handles login + batch form wizard
           (Setup → Mode → Page Groups → Review → Launch)
  Phase 2: Raw Playwright waits for all runs to complete (~30 min)
  Phase 3: Raw Playwright extracts each page's results from History
  Phase 3.5: Save raw data to Supabase (safety net before callback)
  Phase 4: Optional synthesis extraction (Generate Synthesis → extract)
  Phase 5: Callback to Client HQ with all data
  Phase 6: Notifications

One browser session, ~40-50 min total.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime

import httpx

logger = logging.getLogger("moonraker.surge_batch_audit")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


async def run_surge_batch_audit(
    task_id: str,
    params: dict,
    status_callback,
    env: dict,
):
    """
    Main entry point. Called by server.py when a batch audit task is queued.

    params:
        batch_id: str - the content_audit_batches record
        client_slug: str
        brand_name: str
        gbp_url: str - Google Maps share URL
        entity_type: str - "Local Business"
        geo_target: str
        website_url: str
        pages: [{ content_page_id, keyword, target_url }]
        callback_url: str - Client HQ ingest-batch-audit endpoint

    env:
        ANTHROPIC_API_KEY, SURGE_URL, SURGE_EMAIL, SURGE_PASSWORD,
        CLIENT_HQ_URL, AGENT_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
    """

    batch_id = params.get("batch_id", "")
    client_slug = params.get("client_slug", "")
    brand_name = params.get("brand_name", "")
    gbp_url = params.get("gbp_url", "")
    entity_type = params.get("entity_type", "Local Business")
    website_url = params.get("website_url", "")
    pages = params.get("pages", [])
    callback_url = params.get("callback_url", "")

    surge_url = env.get("SURGE_URL", "https://www.surgeaiprotocol.com")
    surge_email = env.get("SURGE_EMAIL", "")
    surge_password = env.get("SURGE_PASSWORD", "")
    agent_api_key = env.get("AGENT_API_KEY", "")

    if not all([batch_id, brand_name, gbp_url, pages, surge_email, surge_password]):
        await status_callback(task_id, "failed", "Missing required parameters")
        return

    await status_callback(
        task_id, "running",
        f"Starting batch audit for '{brand_name}' ({len(pages)} pages)"
    )

    browser = None
    extracted_pages = []

    try:
        # ── PHASE 1: Login + Batch Form Wizard (Browser Use + Opus 4.6) ──
        from browser_use import Browser, Agent
        from browser_use.llm import ChatAnthropic

        llm = ChatAnthropic(
            model="claude-sonnet-4-20250514",
            api_key=env.get("ANTHROPIC_API_KEY", ""),
            timeout=180,
        )

        browser = Browser(
            headless=True,
            keep_alive=True,
            disable_security=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        await status_callback(task_id, "running", "Phase 1: Logging into Surge...")

        # Build the page groups section for the prompt
        page_groups_text = ""
        for i, pg in enumerate(pages, 1):
            page_groups_text += (
                f"\n   Page Group {i}:\n"
                f"     - Target URL: {pg['target_url']}\n"
                f"     - Keywords: {pg['keyword']}\n"
            )

        form_task = f"""Complete these steps in EXACT order. Do NOT skip steps.

1. Go to {surge_url} and log in with:
   - Email: {surge_email}
   - Password: {surge_password}

2. After logging in, you should see the dashboard/command center.
   Click on the "Batch" button/tab to start a new Batch analysis.
   (It may say "Batch (BETA)" — click it.)

3. SETUP step: Fill in the batch details:
   - Batch Name: {brand_name}
   - Brand Name: {brand_name}
   - Entity Type: Select "{entity_type}" from the dropdown
   - Google Maps URL: {gbp_url}
   Then click "Next: Choose Mode" button.

4. MODE step: Select "Multi-Page" mode.
   (Multi-Page = different keywords for different pages.)
   Then click "Next: Add Keywords" button.

5. PAGE GROUPS step: You need to create {len(pages)} page groups.
   For each page group, fill in the Target URL and Keywords fields:{page_groups_text}
   
   If there are fewer page groups shown than needed, click "+ Add Page Group" 
   to add more until you have {len(pages)} groups total.
   
   After all {len(pages)} groups are filled in, click "Next: Review & Run".

6. REVIEW step: Verify the batch shows {len(pages)} page groups.
   Make sure "Force fresh data" is checked if available.
   Then click "Launch Batch Analysis" to start the batch.

7. After launching, wait a moment for the batch to start processing.
   You should see a confirmation or be redirected to the batch progress page.
   Once you confirm the batch has started, stop.

IMPORTANT: Do not close the browser or navigate away after launching."""

        agent = Agent(
            task=form_task,
            llm=llm,
            browser=browser,
        )

        await agent.run()
        await status_callback(task_id, "running", "Phase 1 complete. Batch launched.")

        # ── PHASE 2: Wait for All Runs to Complete (raw Playwright, $0) ──
        await status_callback(task_id, "running", "Phase 2: Waiting for all runs to complete...")

        page = await browser.get_current_page()
        if not page:
            await status_callback(task_id, "failed", "Could not get browser page after batch launch")
            return

        # Navigate to History to monitor runs
        await asyncio.sleep(5)
        await page.goto(f"{surge_url}/dashboard/history", wait_until="networkidle")
        await asyncio.sleep(3)

        max_wait = 55 * 60  # 55 minutes
        poll_interval = 30
        start_time = time.time()
        all_complete = False

        while time.time() - start_time < max_wait:
            elapsed = int(time.time() - start_time)
            minutes = elapsed // 60

            try:
                # Reload history page to check progress
                await page.reload(wait_until="networkidle")
                await asyncio.sleep(3)

                # Count runs with the brand name that have View buttons
                # (View button = run is complete)
                page_content = await page.content()

                # Check for completed runs by looking for the brand name rows
                # with View buttons in the actions column
                completed_runs = await page.evaluate(f"""
                    () => {{
                        const rows = document.querySelectorAll('tr, [class*="row"], [class*="item"]');
                        let completed = 0;
                        let total = 0;
                        for (const row of rows) {{
                            const text = row.textContent || '';
                            if (text.includes('{brand_name}')) {{
                                total++;
                                // Check for View button (indicates completion)
                                const viewBtn = row.querySelector('a[href*="/dashboard/run/"], button:has-text("View")');
                                const hasView = text.includes('View →') || text.includes('View→') || viewBtn;
                                if (hasView) completed++;
                            }}
                        }}
                        return {{ completed, total }};
                    }}
                """)

                comp = completed_runs.get("completed", 0)
                total = completed_runs.get("total", 0)

                await status_callback(
                    task_id, "running",
                    f"Phase 2: {comp}/{len(pages)} runs complete ({minutes}m elapsed)"
                )

                if comp >= len(pages):
                    logger.info(f"All {len(pages)} runs complete after {minutes}m")
                    all_complete = True
                    break

            except Exception as e:
                logger.warning(f"Error checking history: {e}")

            await asyncio.sleep(poll_interval)

        if not all_complete:
            await status_callback(
                task_id, "running",
                f"Warning: Not all runs completed after {max_wait // 60}m. Extracting what we can."
            )

        # ── PHASE 3: Extract Each Page's Results ──
        await status_callback(task_id, "running", "Phase 3: Extracting results from each run...")

        # Find all run links for this brand from the history page
        await page.goto(f"{surge_url}/dashboard/history", wait_until="networkidle")
        await asyncio.sleep(3)

        # Get all run URLs for our brand
        run_links = await page.evaluate(f"""
            () => {{
                const links = [];
                // Find all View links/buttons associated with our brand
                const allLinks = document.querySelectorAll('a[href*="/dashboard/run/"]');
                for (const link of allLinks) {{
                    // Check if this link is in a row containing our brand name
                    const row = link.closest('tr') || link.closest('[class*="row"]') || link.parentElement?.parentElement;
                    if (row && row.textContent.includes('{brand_name}')) {{
                        links.push(link.href);
                    }}
                }}
                // Also try broader approach: any /dashboard/run/ link near brand text
                if (links.length === 0) {{
                    const rows = document.querySelectorAll('tr, div[class*="row"]');
                    for (const row of rows) {{
                        if (row.textContent.includes('{brand_name}')) {{
                            const viewLink = row.querySelector('a[href*="/dashboard/run/"]');
                            if (viewLink) links.push(viewLink.href);
                        }}
                    }}
                }}
                return [...new Set(links)]; // Deduplicate
            }}
        """)

        logger.info(f"Found {len(run_links)} run links for '{brand_name}'")

        # Also try to get the batch page URL
        batch_url = await page.evaluate(f"""
            () => {{
                const links = document.querySelectorAll('a[href*="/dashboard?batch="]');
                for (const link of links) {{
                    const row = link.closest('tr') || link.closest('div');
                    if (row && row.textContent.includes('{brand_name}')) {{
                        return link.href;
                    }}
                }}
                // Try finding batch link via orange text/badge near brand name
                const badges = document.querySelectorAll('[class*="badge"], span[style*="color"]');
                for (const badge of badges) {{
                    if (badge.textContent.includes('{brand_name}')) {{
                        const link = badge.closest('a');
                        if (link && link.href.includes('batch=')) return link.href;
                    }}
                }}
                return null;
            }}
        """)

        # Extract data from each run
        for idx, run_url in enumerate(run_links):
            run_num = idx + 1
            await status_callback(
                task_id, "running",
                f"Phase 3: Extracting run {run_num}/{len(run_links)}..."
            )

            try:
                await page.goto(run_url, wait_until="networkidle")
                await asyncio.sleep(3)

                # Try clipboard extraction first (same pattern as entity audit)
                surge_raw = None

                # Inject clipboard interceptor
                await page.evaluate("""
                    window.__capturedClipboard = null;
                    const origWriteText = navigator.clipboard.writeText.bind(navigator.clipboard);
                    navigator.clipboard.writeText = async function(text) {
                        window.__capturedClipboard = text;
                        return origWriteText(text);
                    };
                    const origExecCommand = document.execCommand.bind(document);
                    document.execCommand = function(cmd) {
                        if (cmd === 'copy') {
                            const sel = window.getSelection();
                            if (sel) window.__capturedClipboard = sel.toString();
                        }
                        return origExecCommand(cmd);
                    };
                """)

                # Find and click Copy button
                copy_btn = await page.query_selector('button:has-text("Copy raw text")')
                if not copy_btn:
                    buttons = await page.query_selector_all('button')
                    for btn in buttons:
                        text = await btn.text_content()
                        if text and 'copy' in text.lower():
                            copy_btn = btn
                            break

                if copy_btn:
                    await copy_btn.click()
                    await asyncio.sleep(2)
                    captured = await page.evaluate("window.__capturedClipboard")
                    if captured and len(captured) > 200:
                        surge_raw = captured
                        logger.info(f"Run {run_num}: clipboard extraction, {len(captured)} chars")

                # Fallback: page text extraction
                if not surge_raw:
                    page_text = await page.evaluate("""
                        () => {
                            const main = document.querySelector('main') || document.body;
                            return main.innerText;
                        }
                    """)
                    if page_text and len(page_text) > 500:
                        surge_raw = page_text
                        logger.info(f"Run {run_num}: text extraction, {len(page_text)} chars")

                if not surge_raw:
                    logger.warning(f"Run {run_num}: could not extract data")
                    continue

                # Extract variance score and keyword from page
                run_info = await page.evaluate("""
                    () => {
                        const text = document.body.innerText || '';
                        // Look for variance score pattern like "62/100"
                        const scoreMatch = text.match(/(\\d+)\\/100/);
                        // Look for keyword in header/title
                        const h1 = document.querySelector('h1, h2');
                        const keyword = h1 ? h1.textContent.trim() : '';
                        return {
                            variance_score: scoreMatch ? parseInt(scoreMatch[1]) : null,
                            keyword: keyword,
                            url: window.location.href
                        };
                    }
                """)

                # Match this run to a page in our list
                matched_page = match_run_to_page(run_info, surge_raw, pages)

                extracted_pages.append({
                    "content_page_id": matched_page["content_page_id"] if matched_page else None,
                    "surge_raw_data": surge_raw,
                    "variance_score": run_info.get("variance_score"),
                    "variance_label": classify_variance(run_info.get("variance_score")),
                    "keyword": run_info.get("keyword", ""),
                    "run_url": run_url,
                })

            except Exception as e:
                logger.error(f"Error extracting run {run_num}: {e}")
                continue

        logger.info(f"Extracted {len(extracted_pages)} of {len(run_links)} runs")

        # ── PHASE 3.5: Save Raw Data to Supabase (safety net) ──
        await status_callback(task_id, "running", "Phase 3.5: Saving raw data to Supabase...")

        if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
            sb_headers = {
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }

            async with httpx.AsyncClient(timeout=30) as client:
                for ep in extracted_pages:
                    if ep["content_page_id"] and ep["surge_raw_data"]:
                        try:
                            resp = await client.patch(
                                f"{SUPABASE_URL}/rest/v1/content_pages?id=eq.{ep['content_page_id']}",
                                json={
                                    "surge_raw_data": ep["surge_raw_data"],
                                    "surge_status": "raw_stored",
                                    "variance_score": ep.get("variance_score"),
                                    "variance_label": ep.get("variance_label"),
                                },
                                headers=sb_headers,
                            )
                            if resp.status_code < 300:
                                logger.info(f"Saved raw data for {ep['content_page_id'][:8]}")
                            else:
                                logger.error(f"Supabase save failed: {resp.status_code}")
                        except Exception as e:
                            logger.error(f"Supabase save error: {e}")

                # Update batch status
                try:
                    await client.patch(
                        f"{SUPABASE_URL}/rest/v1/content_audit_batches?id=eq.{batch_id}",
                        json={
                            "pages_extracted": len([p for p in extracted_pages if p["content_page_id"]]),
                            "status": "extracting",
                        },
                        headers=sb_headers,
                    )
                except Exception as e:
                    logger.error(f"Batch status update error: {e}")

        # ── PHASE 4: Synthesis Extraction (optional) ──
        synthesis_raw = None

        if batch_url:
            await status_callback(
                task_id, "running",
                "Phase 4: Generating batch synthesis..."
            )

            try:
                await page.goto(batch_url, wait_until="networkidle")
                await asyncio.sleep(3)

                # Look for "Generate Synthesis" button
                gen_btn = None
                buttons = await page.query_selector_all('button')
                for btn in buttons:
                    text = await btn.text_content()
                    if text and 'generate synthesis' in text.lower():
                        gen_btn = btn
                        break

                if gen_btn:
                    await gen_btn.click()
                    logger.info("Clicked Generate Synthesis button")

                    # Wait for synthesis to appear (5-10 min)
                    synth_wait = 12 * 60  # 12 minutes max
                    synth_start = time.time()
                    synth_poll = 15  # Check every 15 seconds

                    while time.time() - synth_start < synth_wait:
                        elapsed = int(time.time() - synth_start)
                        minutes = elapsed // 60

                        try:
                            # Check for synthesis content
                            synth_text = await page.evaluate("""
                                () => {
                                    // Look for synthesis section
                                    const sections = document.querySelectorAll('[class*="synthesis"], [class*="Synthesis"]');
                                    for (const s of sections) {
                                        if (s.textContent.length > 500) return s.innerText;
                                    }
                                    // Broader: check if substantial text appeared after the button
                                    const body = document.body.innerText;
                                    if (body.includes('Site Audit Synthesis') || body.includes('Unified Action Plan')) {
                                        return body;
                                    }
                                    return null;
                                }
                            """)

                            if synth_text and len(synth_text) > 500:
                                # Try to extract just the synthesis portion
                                synth_start_idx = synth_text.find("Site Audit Synthesis")
                                if synth_start_idx == -1:
                                    synth_start_idx = synth_text.find("Cluster Synthesis")
                                if synth_start_idx > -1:
                                    synthesis_raw = synth_text[synth_start_idx:]
                                else:
                                    synthesis_raw = synth_text

                                logger.info(f"Synthesis extracted: {len(synthesis_raw)} chars")
                                break

                        except Exception as e:
                            logger.warning(f"Synthesis check error: {e}")

                        # If page might have hung, try reload
                        if elapsed > 5 * 60 and elapsed % 60 < synth_poll:
                            try:
                                await page.reload(wait_until="networkidle")
                                await asyncio.sleep(3)
                            except Exception:
                                pass

                        await status_callback(
                            task_id, "running",
                            f"Phase 4: Waiting for synthesis... ({minutes}m)"
                        )
                        await asyncio.sleep(synth_poll)

                    if not synthesis_raw:
                        logger.warning("Synthesis did not generate within timeout")
                else:
                    # Synthesis may already be visible
                    synth_text = await page.evaluate("""
                        () => {
                            const body = document.body.innerText;
                            if (body.includes('Site Audit Synthesis') || body.includes('Unified Action Plan')) {
                                const idx = body.indexOf('Site Audit Synthesis');
                                if (idx > -1) return body.substring(idx);
                            }
                            return null;
                        }
                    """)
                    if synth_text and len(synth_text) > 500:
                        synthesis_raw = synth_text
                        logger.info(f"Synthesis already present: {len(synthesis_raw)} chars")

            except Exception as e:
                logger.error(f"Synthesis extraction error: {e}")

        # Save synthesis to Supabase if we got it
        if synthesis_raw and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    await client.patch(
                        f"{SUPABASE_URL}/rest/v1/content_audit_batches?id=eq.{batch_id}",
                        json={"synthesis_raw": synthesis_raw},
                        headers={
                            "apikey": SUPABASE_SERVICE_ROLE_KEY,
                            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                            "Content-Type": "application/json",
                            "Prefer": "return=minimal",
                        },
                    )
            except Exception as e:
                logger.error(f"Synthesis save error: {e}")

        # ── PHASE 5: Callback to Client HQ ──
        await status_callback(task_id, "running", "Phase 5: Sending results to Client HQ...")

        callback_success = False
        if callback_url and agent_api_key:
            try:
                callback_payload = {
                    "batch_id": batch_id,
                    "pages": [
                        {
                            "content_page_id": ep["content_page_id"],
                            "surge_raw_data": ep["surge_raw_data"],
                            "variance_score": ep.get("variance_score"),
                            "variance_label": ep.get("variance_label"),
                        }
                        for ep in extracted_pages
                        if ep["content_page_id"]
                    ],
                    "synthesis_raw": synthesis_raw,
                    "surge_batch_url": batch_url,
                }

                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(
                        callback_url,
                        json=callback_payload,
                        headers={
                            "Authorization": f"Bearer {agent_api_key}",
                            "Content-Type": "application/json",
                        },
                    )

                    if resp.status_code == 200:
                        result = resp.json()
                        logger.info(f"Callback success: {result}")
                        callback_success = True
                    else:
                        logger.error(f"Callback failed: {resp.status_code} {resp.text}")

            except Exception as e:
                logger.error(f"Callback error: {e}")

        # ── PHASE 6: Final Status ──
        pages_with_data = len([p for p in extracted_pages if p["content_page_id"]])
        synth_status = "extracted" if synthesis_raw else "not generated"

        if callback_success:
            await status_callback(
                task_id, "completed",
                f"Batch audit complete. {pages_with_data}/{len(pages)} pages extracted. "
                f"Synthesis: {synth_status}. Data sent to Client HQ."
            )
        else:
            # Data was saved to Supabase directly (Phase 3.5), so it's recoverable
            await status_callback(
                task_id, "completed",
                f"Batch data extracted ({pages_with_data}/{len(pages)} pages, "
                f"synthesis: {synth_status}) but callback failed. "
                f"Raw data saved to Supabase for recovery."
            )

        # Send notification
        try:
            from utils.notifications import send_batch_notification
            await send_batch_notification(
                brand_name=brand_name,
                client_slug=client_slug,
                pages_extracted=pages_with_data,
                pages_total=len(pages),
                has_synthesis=bool(synthesis_raw),
                task_id=task_id,
                env=env,
            )
        except Exception as e:
            logger.warning(f"Notification failed: {e}")

    except Exception as e:
        logger.exception(f"Batch audit error: {e}")
        await status_callback(task_id, "failed", f"Error: {str(e)[:200]}")

    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

        try:
            from utils.cleanup import full_cleanup
            full_cleanup()
        except Exception as cleanup_err:
            logger.warning(f"Cleanup failed: {cleanup_err}")


def match_run_to_page(run_info: dict, surge_raw: str, pages: list) -> dict | None:
    """
    Match an extracted Surge run to one of our content pages.
    Uses keyword matching between the run data and our page list.
    """
    run_keyword = (run_info.get("keyword") or "").lower().strip()
    raw_lower = surge_raw.lower()[:5000]  # Check beginning of raw data

    best_match = None
    best_score = 0

    for pg in pages:
        pg_keyword = pg["keyword"].lower().strip()
        pg_url = pg["target_url"].lower().strip()

        score = 0

        # Exact keyword match in run title
        if pg_keyword in run_keyword or run_keyword in pg_keyword:
            score += 10

        # Keyword appears in raw data
        if pg_keyword in raw_lower:
            score += 5

        # Target URL appears in raw data
        url_path = pg_url.split("/")[-1] if "/" in pg_url else pg_url
        if url_path and url_path in raw_lower:
            score += 8

        # Full URL match
        if pg_url in raw_lower:
            score += 10

        if score > best_score:
            best_score = score
            best_match = pg

    if best_match and best_score >= 5:
        return best_match

    logger.warning(
        f"Could not confidently match run '{run_keyword}' to any page. "
        f"Best score: {best_score}"
    )
    return None


def classify_variance(score: int | None) -> str | None:
    """Classify variance score into a label."""
    if score is None:
        return None
    if score >= 80:
        return "Critical Variance"
    if score >= 60:
        return "High Variance"
    if score >= 40:
        return "Moderate Variance"
    if score >= 20:
        return "Near Target"
    return "Optimized"
