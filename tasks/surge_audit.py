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

from utils.debug_capture import capture_debug
from utils.supabase_patch import (
    patch_audit_retriable,
    patch_audit_terminal,
    should_suppress_notification,
)

logger = logging.getLogger("agent.surge")

# Substrings (lowercased) indicating Surge accepted our submit but its own
# downstream crawl of the target URL was blocked by a WAF/firewall. Different
# root cause than surge_rejected (server-side gate on Surge's end) — the work
# to unblock lives on the client site, not on Surge or us. Matched against
# document.body.innerText on the post-submit page.
TARGET_BLOCKED_SIGNALS = [
    "silently blocking us",
    "appears to be blocking",
    "could not be reached",
    "target url could not be reached",
    "unable to reach the target",
    "failed to fetch the target",
    "blocked by the target site",
]

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
MAX_WAIT_MINUTES = 60            # Give up after 60 minutes
FORM_FILL_TIMEOUT_SECONDS = 120  # Max time for login + form fill phase

# ── Phase 2 composite-signal thresholds (Push 1, 2026-04-21) ─────────────────
#
# Prior to Push 1, the Phase 2 detector fired on ANY of 22 substring markers
# matched against document.body.innerText. Several of those markers ("analysis
# complete", "your surge protocol", "run complete") appear in Surge's progress
# UI and nav chrome *before* the full report finishes rendering. On weak
# matches the agent would break out of the wait loop early, extract a
# partially-rendered DOM (as little as 1,708 chars vs normal 150K+), and fire
# the callback to Client HQ with truncated data. Result: runs that looked
# "complete" in the admin UI but had scores.rtpba=null and generic template
# tasks. Reference: Jeanene Wolfe audit 2026-04-21.
#
# Push 1 fix (three-layer defense):
#   1. STRONG_COMPLETION_MARKERS below — progress UI strings removed.
#   2. Settle gate after any marker: innerText must reach
#      MIN_STABLE_EXTRACTION_CHARS and hold for PHASE2_STABLE_POLLS
#      consecutive 30s polls, or the run retriable-fails as phase2_timeout
#      after PHASE2_SETTLE_MAX_SEC.
#   3. Extraction guard below the callback: if Phase 3 returns less than
#      MIN_RAW_CHARS_FOR_CALLBACK, retriable-fail as truncated_extraction
#      before the callback fires.
#
# Thresholds are conservative — every clean audit in production has produced
# well over 150K chars at both stages. The 100K minimum leaves a wide margin
# without catching legitimate small-site audits at the low end.

MIN_STABLE_EXTRACTION_CHARS = 100_000   # Phase 2 settle floor
MIN_RAW_CHARS_FOR_CALLBACK = 100_000    # Phase 3 callback guard
PHASE2_SETTLE_MAX_SEC = 900             # 15 min max settle window after marker
PHASE2_STABLE_POLLS = 2                 # consecutive 30s polls meeting the floor

# Surge completion indicators that are SAFE to break on. Removed from the
# pre-Push-1 list: "analysis complete", "your surge protocol", "surge
# protocol for", "run complete", "run finished" — all observed to render
# during progress phases, not only on completion.
STRONG_COMPLETION_MARKERS = [
    # Legacy + explicit "run finished" markers
    "run completed in",
    "signal health",
    "copy raw text",
    "copy raw data",
    "trust signals",
    "cres score",
    "page variance score",
    # Surge 2.9.9-beta inline results on /dashboard — sections and controls
    # that only render once the run finishes and the report is populated:
    "download report",
    "download raw",
    "export raw",
    "entity recognition score",
    "brand dataset variance",
    "ai extraction readiness",
    "rtpba",
    "ready-to-publish best answer",
    "ready to publish best answer",
]


# ── Terminal failure helper ──────────────────────────────────────────────────

async def _terminal_fail(
    task_id,
    update_task,
    audit_id,
    practice_name,
    client_slug,
    page,
    status_code,
    user_message,
    error_detail,
    notify_fn,
):
    """Handle a terminal non-retriable failure for an audit.

    Five steps, each step best-effort so a downstream error doesn't mask the
    primary failure reason:
      1. capture_debug — save HTML + screenshot + innerText to /tmp/agent-debug/
      2. patch_audit_terminal — flip Supabase to agent_error + retriable=false
      3. update_task — set agent-local task state so Client HQ polling reads it
      4. notify_fn — send the branded team email with the debug path included
      5. return (do not raise) so the caller can proceed to `finally` cleanup

    Note: status_code is agent-local only (e.g. 'surge_maintenance'). Supabase
    gets 'agent_error' with the status_code preserved in last_agent_error.
    """
    # 1. Debug capture — best-effort, returns '' on failure
    debug_path = ""
    try:
        debug_path = await capture_debug(task_id, page, status_code)
    except Exception as ce:
        logger.warning(f"Debug capture failed in _terminal_fail: {ce}")

    # 2. Supabase terminal PATCH — best-effort, returns False on failure
    try:
        await patch_audit_terminal(audit_id, status_code, error_detail, debug_path)
    except Exception as pe:
        logger.warning(f"Supabase PATCH failed in _terminal_fail: {pe}")

    # 3. Agent-local task state
    try:
        update_task(task_id, status_code, user_message, error=error_detail)
    except Exception as ue:
        logger.warning(f"update_task failed in _terminal_fail: {ue}")

    # 4. Team email notification — but suppress if another audit already
    # failed with the same reason_code in the last 2 hours, so a systemic
    # Surge outage does not flood the team's inbox. Fail-open: if the
    # suppression check errors, the email still goes through.
    try:
        suppress = False
        try:
            suppress = await should_suppress_notification(status_code, audit_id)
        except Exception as se:
            logger.warning(f"Suppression check failed in _terminal_fail: {se}")
        if suppress:
            logger.info(
                f"Skipping {status_code} notification for audit {audit_id} "
                f"(another recent failure with same code)"
            )
        else:
            await notify_fn(practice_name, client_slug, debug_path)
    except Exception as ne:
        logger.warning(f"Notification failed in _terminal_fail: {ne}")


# ── Retriable failure helper ─────────────────────────────────────────────────

async def _retriable_fail(
    task_id,
    update_task,
    audit_id,
    practice_name,
    client_slug,
    page,
    status_code,
    user_message,
    error_detail,
):
    """Handle a retriable failure for an audit (Push 1, 2026-04-21).

    Mirrors `_terminal_fail` but routes the Supabase PATCH through
    `patch_audit_retriable` so Step 0.5 in Client HQ's
    process-audit-queue cron picks the row back up and requeues it after a
    5-minute backoff. Notification uses the generic send_error_notification
    (no dedicated retriable template yet) with a "— will auto-retry" suffix
    appended to the body so the team knows no manual action is required.

    Five steps, each step best-effort so a downstream error doesn't mask the
    primary failure reason:
      1. capture_debug — save HTML + screenshot + innerText to /tmp/agent-debug/
      2. patch_audit_retriable — flip Supabase to agent_error + retriable=true
      3. update_task — set agent-local task state so Client HQ polling reads it
      4. notify_fn — send the team email (suppressed if another audit failed
         with the same reason_code + retriable=true in the last 2h)
      5. return (do not raise) so the caller can proceed to `finally` cleanup

    Callers currently: phase2_timeout, truncated_extraction.
    """
    # 1. Debug capture — best-effort, returns '' on failure
    debug_path = ""
    try:
        debug_path = await capture_debug(task_id, page, status_code)
    except Exception as ce:
        logger.warning(f"Debug capture failed in _retriable_fail: {ce}")

    # 2. Supabase retriable PATCH — best-effort
    try:
        await patch_audit_retriable(audit_id, status_code, error_detail, debug_path)
    except Exception as pe:
        logger.warning(f"Supabase PATCH failed in _retriable_fail: {pe}")

    # 3. Agent-local task state
    try:
        update_task(task_id, status_code, user_message, error=error_detail)
    except Exception as ue:
        logger.warning(f"update_task failed in _retriable_fail: {ue}")

    # 4. Team email notification with retriable-scoped suppression
    try:
        suppress = False
        try:
            suppress = await should_suppress_notification(
                status_code, audit_id, retriable=True
            )
        except Exception as se:
            logger.warning(f"Suppression check failed in _retriable_fail: {se}")
        if suppress:
            logger.info(
                f"Skipping {status_code} notification for audit {audit_id} "
                f"(another recent retriable failure with same code)"
            )
        else:
            from utils.notifications import send_error_notification
            suffix = (
                " — retriable; the queue cron will auto-requeue this audit "
                "after a 5-minute cooldown."
            )
            debug_suffix = f" Debug: {debug_path}" if debug_path else ""
            await send_error_notification(
                practice_name=practice_name,
                client_slug=client_slug,
                error_message=f"{error_detail}{suffix}{debug_suffix}",
                task_id=task_id,
            )
    except Exception as ne:
        logger.warning(f"Notification failed in _retriable_fail: {ne}")


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

        # Build the form fill task prompt.
        #
        # Credentials remain interpolated here for now (reverting this adds
        # Browser-Use-API-level complexity — an independent Playwright login
        # with CDP handoff to Browser Use — that needs its own test cycle).
        # Leakage mitigations currently in place:
        #  1. Log redaction filter (utils.log_redact) scrubs SURGE_PASSWORD
        #     and other env secrets from all stdout log records before they
        #     land in docker json-file logs.
        #  2. TODO: full refactor to pre-login via raw Playwright + CDP
        #     handoff will eliminate the Anthropic API exposure too.
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

        # ── Phase 1.5: Post-submit verification ─────────────────────────
        # Browser Use reports "submitted successfully" based on the optimistic
        # UI Surge renders client-side on submit. That UI fires BEFORE the
        # server accepts the job, so a server-side gate (maintenance mode,
        # role check, rate limit) can silently drop the submission while the
        # frontend still shows "Uses 1 credit · 236 remaining".
        #
        # This phase confirms the submission actually landed server-side
        # before entering the 20-35min wait loop, turning silent rejection
        # into a fast deterministic terminal failure instead of a 60-min
        # timeout. On terminal failure, PATCH Supabase to
        # agent_error+retriable=false so the cron does not auto-requeue.
        page = await browser.get_current_page()
        if not page:
            raise RuntimeError(
                "Could not access Playwright page from Browser Use. "
                "The browser may have closed unexpectedly."
            )

        update_task(
            task_id, "verifying_submission",
            "Verifying Surge accepted the submission",
        )

        # Give Surge a moment to process the submit and navigate
        await asyncio.sleep(5)

        submit_confirmed = False
        verification_error = None
        try:
            current_url = await page.get_url()
            content_after = await page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            content_lower_after = (content_after or "").lower()

            # Signal 1 (strongest): URL changed to a run/analysis page
            if "/dashboard/run/" in current_url or "/run/" in current_url:
                submit_confirmed = True
            else:
                # Signal 2: optimistic UI processing indicators, then re-check
                # URL after another few seconds to catch slow server redirects.
                # As of Surge 2.9.9-beta the SPA renders the run UI inline on
                # /dashboard without a URL change; detect its text markers.
                processing_indicators = [
                    "safe to close tab",
                    "safely close this tab",
                    "~20",
                    "processing your audit",
                    "queued for analysis",
                    "surge analysis in progress",
                    "analysis in progress",
                    "activity log",
                    "pre-scan starting",
                    "running intelligence",
                    "elapsed:",
                    "✓ init",
                    "✓ queue",
                ]
                if any(ind in content_lower_after for ind in processing_indicators):
                    # Surge's current SPA renders the run inline on
                    # /dashboard without a URL change — if we see its
                    # processing markers in the DOM, treat that as a
                    # confirmed submit and stop waiting for a redirect.
                    submit_confirmed = True
                    logger.info(
                        f"Post-submit confirmed via inline processing indicator "
                        f"(url unchanged: {current_url})"
                    )

            # If submit didn't confirm, classify precisely: maintenance first
            # (Surge platform down), then target_blocked (target site WAF
            # refusing Surge's downstream crawl), then generic surge_rejected.
            if not submit_confirmed:
                # Re-read content in case it updated
                content_after = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
                content_lower_after = (content_after or "").lower()
                maintenance_signals = [
                    "pushing system updates",
                    "maintenance active",
                    "new runs blocked",
                ]
                if any(sig in content_lower_after for sig in maintenance_signals):
                    from utils.notifications import send_maintenance_notification
                    await _terminal_fail(
                        task_id, update_task, audit_id,
                        practice_name, client_slug, page,
                        "surge_maintenance",
                        "Surge is in maintenance mode. New runs are blocked at the platform level.",
                        "Surge maintenance mode active at submit time",
                        send_maintenance_notification,
                    )
                    return

                # target_blocked: Surge tried to fetch the target URL and the
                # target's WAF refused. This is a client-site issue, not a
                # Surge or agent issue — surface the target URL so the team
                # can investigate Cloudflare/firewall settings for that site.
                matched_blocking_signal = next(
                    (sig for sig in TARGET_BLOCKED_SIGNALS if sig in content_lower_after),
                    None,
                )
                if matched_blocking_signal:
                    from utils.notifications import send_rejected_notification
                    await _terminal_fail(
                        task_id, update_task, audit_id,
                        practice_name, client_slug, page,
                        "target_blocked",
                        "Surge could not crawl the target URL. The site's WAF or firewall blocked the request.",
                        (
                            f"Target WAF block: Surge reported '{matched_blocking_signal}' "
                            f"for target={website_url} (post-submit url={current_url})"
                        ),
                        send_rejected_notification,
                    )
                    return

                from utils.notifications import send_rejected_notification
                await _terminal_fail(
                    task_id, update_task, audit_id,
                    practice_name, client_slug, page,
                    "surge_rejected",
                    "Surge did not accept the submission. The form submitted without producing a processing page.",
                    f"Silent rejection: URL did not change to /run/ after submit (url={current_url})",
                    send_rejected_notification,
                )
                return
        except Exception as ve:
            # If verification itself errors, log and fall through to the wait
            # loop — better to wait 60 min and timeout than to falsely
            # terminal-fail a legitimate audit on a transient DOM error.
            verification_error = str(ve)
            logger.warning(
                f"Post-submit verification errored for {practice_name}, "
                f"falling through to wait loop: {ve}"
            )

        if submit_confirmed:
            logger.info(f"Post-submit verification passed for {practice_name}")
        elif verification_error:
            logger.info(
                f"Post-submit verification skipped due to error "
                f"({verification_error}); entering wait loop anyway"
            )

        # ── Phase 2: Wait for Surge completion (Raw Playwright) ──────────
        update_task(
            task_id, "waiting_for_surge",
            "Surge is processing the audit (typically 20 to 35 minutes)"
        )

        # Note: Browser Use 0.12.x Page wrapper doesn't expose Playwright's
        # context for clipboard permissions. Clipboard intercept in extraction
        # phase handles this via JS injection instead.

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
            # Browser Use 0.12.x returns its own Page wrapper, not Playwright's.
            # Use get_url() and evaluate() with arrow function format.
            try:
                # Strategy 1: URL routed to the legacy results page.
                # Surge 2.9.9-beta usually keeps the run UI inline on
                # /dashboard and only navigates to /dashboard/run/<id> at
                # the very end (observed in Anna Sky run 2026-04-21). We
                # still honor the URL as a completion signal when it
                # does fire, but we no longer *require* it.
                current_url = await page.get_url()
                if "/dashboard/run/" in current_url or "/run/" in current_url:
                    logger.info(
                        f"Surge completed (URL redirect to results): {current_url}"
                    )
                    completed = True
                    break

                # Strategy 2: Check page content for completion indicators.
                # These are substrings of document.body.innerText (matched
                # case-insensitively). Keep this list broad; a single
                # match ends the wait and Phase 3 will verify raw-data
                # extraction. If Phase 3 can't find a Copy button, it
                # falls back to innerText — so false-positives here
                # would surface as empty raw_data, not a silent miss.
                content = await page.evaluate("() => document.body.innerText")
                content_lower = (content or "").lower()

                # Push 1 (2026-04-21): strong markers only. Weak progress-UI
                # substrings moved to the module-level block comment above
                # STRONG_COMPLETION_MARKERS. A match here is necessary but
                # NOT sufficient — the settle gate below must also confirm
                # the report DOM has actually rendered before we break out.
                matched_indicator = None
                for indicator in STRONG_COMPLETION_MARKERS:
                    if indicator in content_lower:
                        matched_indicator = indicator
                        logger.info(
                            f"Phase 2 marker '{indicator}' matched after {elapsed_min} min; "
                            f"entering settle gate (require >={MIN_STABLE_EXTRACTION_CHARS:,} chars "
                            f"stable for {PHASE2_STABLE_POLLS} polls, max {PHASE2_SETTLE_MAX_SEC//60} min)"
                        )
                        break

                if matched_indicator is not None:
                    # ── Phase 2 settle gate ─────────────────────────────
                    # Poll innerText length until we see two consecutive
                    # readings at or above MIN_STABLE_EXTRACTION_CHARS with
                    # the same length (DOM stopped mutating). If that never
                    # happens within PHASE2_SETTLE_MAX_SEC, the marker was
                    # almost certainly a premature progress-UI match and we
                    # retriable-fail so the next attempt gets a clean run.
                    update_task(
                        task_id, "settling",
                        f"Detected completion signal; verifying report finished rendering"
                    )
                    settle_start = time.time()
                    settle_succeeded = False
                    stable_count = 0
                    last_len = -1
                    final_len = 0
                    settle_poll_count = 0

                    while (time.time() - settle_start) < PHASE2_SETTLE_MAX_SEC:
                        settle_poll_count += 1
                        try:
                            content_now = await page.evaluate(
                                "() => document.body ? document.body.innerText : ''"
                            )
                            cur_len = len(content_now or "")
                        except Exception as se:
                            logger.warning(f"Settle-poll evaluate errored: {se}")
                            cur_len = 0

                        final_len = cur_len
                        if settle_poll_count % 3 == 1:
                            logger.info(
                                f"Phase 2 settle poll #{settle_poll_count}: "
                                f"innerText={cur_len:,} chars, "
                                f"stable_count={stable_count}/{PHASE2_STABLE_POLLS}"
                            )

                        if cur_len >= MIN_STABLE_EXTRACTION_CHARS:
                            if cur_len == last_len:
                                stable_count += 1
                                if stable_count >= PHASE2_STABLE_POLLS:
                                    settle_succeeded = True
                                    logger.info(
                                        f"Phase 2 settled: {cur_len:,} chars stable "
                                        f"across {stable_count} polls (marker: '{matched_indicator}')"
                                    )
                                    break
                            else:
                                stable_count = 1
                        else:
                            stable_count = 0

                        last_len = cur_len
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)

                    if settle_succeeded:
                        completed = True
                        break

                    # Settle timed out → retriable fail
                    settle_min = int((time.time() - settle_start) / 60)
                    logger.warning(
                        f"Phase 2 settle timeout after {settle_min} min. "
                        f"Marker '{matched_indicator}' fired but innerText "
                        f"reached only {final_len:,} chars (required "
                        f"{MIN_STABLE_EXTRACTION_CHARS:,} stable for "
                        f"{PHASE2_STABLE_POLLS} polls). URL: {current_url}"
                    )
                    await _retriable_fail(
                        task_id, update_task, audit_id,
                        practice_name, client_slug, page,
                        "phase2_timeout",
                        (
                            f"Audit looked complete but the report did not finish "
                            f"rendering within {PHASE2_SETTLE_MAX_SEC // 60} minutes. "
                            f"Will auto-retry."
                        ),
                        (
                            f"Phase 2 settle timeout: marker='{matched_indicator}' "
                            f"fired at {elapsed_min} min into Phase 2, but innerText "
                            f"reached only {final_len:,} chars over "
                            f"{settle_poll_count} settle polls (required "
                            f">={MIN_STABLE_EXTRACTION_CHARS:,} stable for "
                            f"{PHASE2_STABLE_POLLS} consecutive polls). "
                            f"URL at timeout: {current_url}"
                        ),
                    )
                    return

                # Check for error states
                if "error" in content_lower and "analysis failed" in content_lower:
                    raise RuntimeError("Surge reported an analysis failure on the page")

                # NOTE: The pre-2026-04-18 code had a full-page substring match
                # here for "insufficient credits" / "no credits" that false-
                # positived on Surge's maintenance-mode banner. Removed: if the
                # form submit landed (confirmed in Phase 1.5 above), credits
                # were sufficient. Any failure during the wait loop is now
                # surfaced as a timeout or an explicit "analysis failed" error.

                # Log URL periodically for debugging
                if elapsed_min % 5 == 0 and elapsed_min > 0:
                    logger.info(f"Still waiting — URL: {current_url}")

            except Exception as e:
                logger.warning(f"Error checking page during wait: {e}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        if not completed:
            # Push 2026-04-22: classify the 60-min Phase 2 wait timeout with
            # a dedicated error code instead of bubbling a bare RuntimeError
            # through server.py's generic handler (which never set
            # last_agent_error_code, leaving these audits invisible to the
            # alerter + blocks digest).
            #
            # NOT healable: if Surge didn't reach a results page in a full
            # hour, retry is extremely unlikely to help. Team reviews, then
            # manually re-dispatches if target-side conditions change.
            from utils.notifications import send_rejected_notification
            await _terminal_fail(
                task_id, update_task, audit_id,
                practice_name, client_slug, page,
                "surge_timeout",
                (
                    f"Surge could not produce audit results within "
                    f"{MAX_WAIT_MINUTES} minutes. The dashboard stayed in a "
                    f"processing state. This is typically a Surge-side stall "
                    f"or a target site that takes unusually long to crawl."
                ),
                (
                    f"Phase 2 wait timeout: Surge dashboard URL did not "
                    f"change to /run/ after {MAX_WAIT_MINUTES} min "
                    f"(last url={current_url}, target={website_url})"
                ),
                send_rejected_notification,
            )
            return

        # ── Phase 3: Extract results (Raw Playwright) ────────────────────
        update_task(task_id, "extracting", "Extracting audit results from Surge")

        # Brief pause for page to fully render
        await asyncio.sleep(3)

        surge_data = await _extract_surge_data(page)

        if not surge_data or len(surge_data) < 100:
            # Extraction fundamentally failed (Copy button didn't work AND
            # innerText fallback was empty). Keep the original RuntimeError
            # path — this is a tooling failure, not a truncation, and
            # server.py's generic error handler will notify. Retry is
            # unlikely to help without manual investigation.
            raise RuntimeError(
                f"Extracted data seems too short ({len(surge_data or '')} chars). "
                "The Copy button may not have worked correctly."
            )

        # Push 1 (2026-04-21): Phase 3 callback guard.
        # Catch the class of failure where Phase 2 settle was satisfied (or
        # pre-Push-1, was not even checked) but the Copy-button extraction
        # returned less than a healthy audit's worth of text. Below the
        # callback floor, the callback is not fired — the row is flipped to
        # agent_error+retriable=true so Client HQ's process-audit-queue
        # Step 0.5 requeues after the 5-min backoff.
        raw_len = len(surge_data)
        if raw_len < MIN_RAW_CHARS_FOR_CALLBACK:
            logger.warning(
                f"Phase 3 extraction truncated: {raw_len:,} chars "
                f"(required >={MIN_RAW_CHARS_FOR_CALLBACK:,})"
            )
            await _retriable_fail(
                task_id, update_task, audit_id,
                practice_name, client_slug, page,
                "truncated_extraction",
                (
                    f"Audit extraction returned only {raw_len:,} characters, "
                    f"below the {MIN_RAW_CHARS_FOR_CALLBACK:,}-char floor for a "
                    f"trustworthy report. Will auto-retry."
                ),
                (
                    f"Truncated extraction: surge_data length {raw_len} chars "
                    f"(required >={MIN_RAW_CHARS_FOR_CALLBACK:,}). "
                    f"Phase 2 settle passed but Phase 3 Copy-button/innerText "
                    f"extraction returned short. Callback to Client HQ suppressed."
                ),
            )
            return

        logger.info(
            f"Extracted {len(surge_data)} chars of Surge data for {practice_name}"
        )

        # ── Phase 3.5: Save raw data to Supabase (safety net) ────────────
        # Persists the raw Surge output before attempting the Client HQ callback.
        # If the callback fails, the data can be recovered from surge_raw_data column.
        update_task(task_id, "saving_data", "Saving raw Surge data to database")

        if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
            try:
                async with httpx.AsyncClient(timeout=30) as save_client:
                    save_resp = await save_client.patch(
                        f"{SUPABASE_URL}/rest/v1/entity_audits?id=eq.{audit_id}",
                        json={"surge_raw_data": surge_data},
                        headers={
                            "apikey": SUPABASE_SERVICE_ROLE_KEY,
                            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                            "Content-Type": "application/json",
                            "Prefer": "return=minimal",
                        },
                    )
                    if save_resp.status_code < 300:
                        logger.info(f"Saved {len(surge_data)} chars of raw Surge data to Supabase")
                    else:
                        logger.warning(f"Failed to save raw data to Supabase: {save_resp.status_code}")
            except Exception as save_err:
                logger.warning(f"Could not save raw data to Supabase: {save_err}")
                # Non-fatal: continue with callback even if direct save fails

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
        # Best-effort debug capture before re-raising so server.py still
        # fires its normal error-notification path.
        try:
            if browser is not None:
                dbg_page = await browser.get_current_page()
                if dbg_page is not None:
                    await capture_debug(task_id, dbg_page, "generic_exception")
        except Exception as ce:
            logger.warning(f"Debug capture in except block failed: {ce}")
        raise  # Re-raise so server.py handles notification

    finally:
        # Always close the browser
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
            logger.warning(f"Cleanup failed: {cleanup_err}")


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _extract_surge_data(page) -> str:
    """
    Extract the Surge audit data from the results page.

    Uses Browser Use 0.12.x Page wrapper API:
    - page.evaluate("() => ...") for JS execution (arrow function required)
    - page.get_elements_by_css_selector() for DOM queries

    Strategy:
    1. Inject clipboard interceptor + click Copy button
    2. Fallback: try clipboard.readText()
    3. Fallback: click all Copy buttons and concatenate
    4. Fallback: extract page text content
    """
    surge_data = None

    # Strategy 1: Inject clipboard interceptor + click Copy button
    try:
        # Inject interceptor before clicking
        await page.evaluate("""() => {
            window.__surgeCopiedText = null;

            const origWriteText = navigator.clipboard.writeText.bind(navigator.clipboard);
            navigator.clipboard.writeText = (text) => {
                window.__surgeCopiedText = text;
                return origWriteText(text);
            };

            const origExecCommand = document.execCommand.bind(document);
            document.execCommand = (cmd, ...args) => {
                if (cmd === 'copy') {
                    const sel = window.getSelection();
                    if (sel) window.__surgeCopiedText = sel.toString();
                }
                return origExecCommand(cmd, ...args);
            };
        }""")

        # Find and click the Copy button
        copy_buttons = await page.get_elements_by_css_selector('button[title="Copy raw text"]')

        if copy_buttons:
            await copy_buttons[0].click()
            await asyncio.sleep(2)

            surge_data = await page.evaluate("() => window.__surgeCopiedText")

            if surge_data:
                logger.info(f"Extracted {len(surge_data)} chars via clipboard intercept")
                return surge_data

    except Exception as e:
        logger.warning(f"Clipboard intercept strategy failed: {e}")

    # Strategy 2: Try reading clipboard directly
    try:
        await page.evaluate("""() => {
            const btn = document.querySelector('button[title="Copy raw text"]');
            if (btn) btn.click();
        }""")
        await asyncio.sleep(2)

        surge_data = await page.evaluate("() => navigator.clipboard.readText()")
        if surge_data and len(surge_data) > 50:
            logger.info(f"Extracted {len(surge_data)} chars via clipboard.readText()")
            return surge_data

    except Exception as e:
        logger.warning(f"Direct clipboard read failed: {e}")

    # Strategy 3: Click all section Copy buttons and concatenate
    try:
        copy_buttons = await page.get_elements_by_css_selector('button[title="Copy raw text"]')

        all_text = []
        for btn in copy_buttons:
            await page.evaluate("() => { window.__surgeCopiedText = null; }")
            await btn.click()
            await asyncio.sleep(1)
            text = await page.evaluate("() => window.__surgeCopiedText")
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
                # Use agent API key for auth (accepted by Client HQ requireAdminOrInternal)
                "Authorization": f"Bearer {AGENT_API_KEY}",
            },
        )

        if response.status_code != 200:
            body = response.text[:500]
            raise RuntimeError(
                f"Client HQ returned {response.status_code}: {body}"
            )

        logger.info(f"Successfully posted audit results for audit_id={audit_id}")

