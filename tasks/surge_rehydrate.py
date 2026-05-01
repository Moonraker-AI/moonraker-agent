"""
Surge Audit Rehydration
========================
Re-pulls Diagnosis and Action plan content for an existing audit from
Surge's History tab, then POSTs both pane texts to Client HQ for
re-extraction with the dual-pane parser path.

Lighter than surge_audit.py:
  - No new Surge run (free, doesn't burn credits)
  - No Phase 2 wait
  - Reuses login + clipboard-intercept patterns from surge_audit.py
  - Captures TWO tabs: Diagnosis and Action plan (skips Opportunities — redundant)

Used to enrich the 65 active clients whose original audits captured only
the Diagnosis pane. After rehydration:
  - entity_audits.surge_raw_diagnosis populated
  - entity_audits.surge_raw_action_plan populated
  - scores->rtpba regenerated against the merged dual-pane payload
  - checklist_items diff-and-appended (existing rows preserved)
"""

import asyncio
import logging
import os
from typing import Callable

import httpx
from browser_use import Browser

from utils.debug_capture import capture_debug
from utils.supabase_patch import (
    patch_audit_retriable,
)

logger = logging.getLogger("agent.surge_rehydrate")

SURGE_URL = os.getenv("SURGE_URL", "https://www.surgeaiprotocol.com")
SURGE_EMAIL = os.getenv("SURGE_EMAIL", "")
SURGE_PASSWORD = os.getenv("SURGE_PASSWORD", "")
CLIENT_HQ_URL = os.getenv("CLIENT_HQ_URL", "https://clients.moonraker.ai")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")

# Per-pane minimums. Diagnosis is the heavyweight; action plan is shorter
# but has structural blueprint phases. Lower than surge_audit.py's Phase 3
# guard (100k) because action plan alone won't hit that.
MIN_DIAGNOSIS_CHARS = 50_000
MIN_ACTION_PLAN_CHARS = 20_000

CALLBACK_TIMEOUT_SEC = 600
TAB_SETTLE_SEC = 3


async def execute_surge_rehydrate(
    task_id: str,
    tasks: dict,
    update_task: Callable,
):
    """
    Rehydration lifecycle:
      1. Login to Surge (raw DOM fill, zero LLM)
      2. Open History tab on dashboard
      3. Find audit row matching practice_name + audit_date
      4. Click into the audit detail
      5. Diagnosis tab → click "Copy raw text" → capture clipboard
      6. Action plan tab → click "Copy raw text" → capture clipboard
      7. Validate min char floors
      8. POST { audit_id, surge_raw_diagnosis, surge_raw_action_plan, rehydrate: true }
         to Client HQ /api/process-entity-audit
    """
    req = tasks[task_id]["request"]
    audit_id = req["audit_id"]
    practice_name = req["practice_name"]
    audit_date = req.get("audit_date")
    client_slug = req["client_slug"]

    browser = None
    page = None

    try:
        update_task(task_id, "login", "Launching browser and logging into Surge")
        browser = Browser(
            keep_alive=True,
            headless=True,
            disable_security=True,  # clipboard access
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        page = await browser.new_page()

        await page.goto(f"{SURGE_URL}/login")
        await page.wait_for_selector('input[type="email"]', timeout=30000)
        await page.fill('input[type="email"]', SURGE_EMAIL)
        await page.fill('input[type="password"]', SURGE_PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_url("**/dashboard**", timeout=60000)

        update_task(task_id, "history", "Opening Surge history")
        # NOTE: selectors below are best-effort. Verify against the live
        # Surge UI (https://www.surgeaiprotocol.com/dashboard). If they
        # drift, capture a screenshot via capture_debug for diagnosis.
        try:
            await page.click('text=History', timeout=15000)
        except Exception:
            await page.click('a[href*="history"]', timeout=15000)
        await asyncio.sleep(2)

        update_task(task_id, "finding_audit",
                    f"Locating history row for {practice_name}" +
                    (f" on {audit_date}" if audit_date else ""))

        # Match by practice name (Surge typically lists brand/URL per row).
        # If multiple history entries match the brand, prefer the row whose
        # date matches audit_date.
        match_clicked = False
        try:
            rows = await page.locator(f'tr:has-text("{practice_name}")').all()
            if not rows:
                rows = await page.locator(f'[role="row"]:has-text("{practice_name}")').all()
            if not rows:
                # Fallback: text match on link/cell
                rows = await page.locator(f'text="{practice_name}"').all()

            chosen = None
            if audit_date and rows:
                for r in rows:
                    txt = (await r.text_content()) or ""
                    if audit_date in txt or audit_date.replace("-", "/") in txt:
                        chosen = r
                        break
            if not chosen and rows:
                chosen = rows[0]

            if chosen:
                await chosen.click()
                match_clicked = True
                await asyncio.sleep(TAB_SETTLE_SEC)
        except Exception as e:
            logger.warning(f"History row match failed: {e}")

        if not match_clicked:
            return await _fail_retriable(
                task_id, audit_id, update_task, page,
                "history_row_not_found",
                f"Could not locate a Surge history row for '{practice_name}'"
                + (f" on {audit_date}" if audit_date else "")
            )

        # Capture Diagnosis
        update_task(task_id, "extracting_diagnosis", "Copying Diagnosis tab")
        diagnosis_text = await _capture_tab(page, tab_label="Diagnosis")
        if not diagnosis_text or len(diagnosis_text) < MIN_DIAGNOSIS_CHARS:
            return await _fail_retriable(
                task_id, audit_id, update_task, page,
                "diagnosis_pane_short",
                f"Diagnosis returned {len(diagnosis_text or '')} chars, "
                f"minimum {MIN_DIAGNOSIS_CHARS}"
            )
        logger.info(f"Diagnosis captured: {len(diagnosis_text):,} chars")

        # Capture Action plan
        update_task(task_id, "extracting_action_plan", "Copying Action plan tab")
        action_plan_text = await _capture_tab(page, tab_label="Action plan")
        if not action_plan_text or len(action_plan_text) < MIN_ACTION_PLAN_CHARS:
            return await _fail_retriable(
                task_id, audit_id, update_task, page,
                "action_plan_pane_short",
                f"Action plan returned {len(action_plan_text or '')} chars, "
                f"minimum {MIN_ACTION_PLAN_CHARS}"
            )
        logger.info(f"Action plan captured: {len(action_plan_text):,} chars")

        # Callback to CHQ
        update_task(task_id, "callback",
                    f"Sending {len(diagnosis_text):,} + {len(action_plan_text):,} chars to Client HQ")
        async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_SEC) as client:
            resp = await client.post(
                f"{CLIENT_HQ_URL}/api/process-entity-audit",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {AGENT_API_KEY}",
                },
                json={
                    "audit_id": audit_id,
                    "surge_raw_diagnosis": diagnosis_text,
                    "surge_raw_action_plan": action_plan_text,
                    "rehydrate": True,
                    "rehydration_source": "history_scrape",
                },
            )
            if resp.status_code >= 400:
                return await _fail_retriable(
                    task_id, audit_id, update_task, page,
                    "callback_error",
                    f"CHQ returned {resp.status_code}: {resp.text[:300]}"
                )

        update_task(
            task_id, "complete",
            f"Rehydrated {practice_name}: "
            f"{len(diagnosis_text):,} diagnosis + {len(action_plan_text):,} action plan chars"
        )

    except Exception as e:
        logger.exception(f"Rehydrate task {task_id[:12]} failed")
        await _fail_retriable(
            task_id, audit_id, update_task, page,
            "unexpected_error", str(e)[:300]
        )
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def _capture_tab(page, tab_label: str) -> str:
    """Click the named tab, then click its 'Copy raw text' button, then
    read the intercepted clipboard text."""
    # Click tab
    try:
        await page.click(f'text={tab_label}', timeout=10000)
    except Exception:
        await page.click(f'[role="tab"]:has-text("{tab_label}")', timeout=10000)
    await asyncio.sleep(TAB_SETTLE_SEC)

    # Inject clipboard interceptor (must happen each tab — switching tabs
    # may reset window globals depending on Surge's SPA behavior).
    await page.evaluate("""() => {
        window.__surgeCopiedText = null;
        const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = (text) => {
            window.__surgeCopiedText = text;
            return orig(text);
        };
        const origExec = document.execCommand.bind(document);
        document.execCommand = (cmd, ...args) => {
            if (cmd === 'copy') {
                const sel = window.getSelection();
                if (sel) window.__surgeCopiedText = sel.toString();
            }
            return origExec(cmd, ...args);
        };
    }""")

    btns = await page.get_elements_by_css_selector('button[title="Copy raw text"]')
    if not btns:
        return ""

    await btns[0].click()
    await asyncio.sleep(2)
    text = await page.evaluate("() => window.__surgeCopiedText")
    return text or ""


async def _fail_retriable(task_id, audit_id, update_task, page, code, detail):
    if page:
        try:
            await capture_debug(task_id, page, code)
        except Exception:
            pass
    try:
        await patch_audit_retriable(audit_id, code, detail)
    except Exception:
        pass
    update_task(task_id, code, detail, error=detail)
