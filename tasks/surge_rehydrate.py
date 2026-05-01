"""
Surge Audit Rehydration
========================
Re-pulls Diagnosis and Action plan content for an existing audit from
Surge's History tab, then POSTs both pane texts to Client HQ for
re-extraction with the dual-pane parser path.

Lighter than surge_audit.py:
  - No new Surge run (free, doesn't burn credits)
  - No Phase 2 wait
  - Reuses utils.surge_run_extract for login + per-tab clipboard extraction
  - Captures TWO tabs: Diagnosis and Action plan (skips Opportunities — redundant)

Implementation note: this task uses raw Playwright (via async_playwright)
rather than Browser-Use, because:
  1. Browser-Use's Page wrapper exposes only goto/evaluate/screenshot —
     no wait_for_selector/fill/locator. Manual login requires those.
  2. clipboard-read permission must be granted at the BrowserContext
     level, which surge_run_extract already does via
     pw.chromium.launch().new_context(permissions=[...]).
  3. The shared _login + _extract_one_run helpers in utils/surge_run_extract
     already implement the exact tab-walk we need; reusing them avoids
     selector duplication.

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
import re
from typing import Callable, Optional

import httpx
from playwright.async_api import async_playwright

from utils.supabase_patch import patch_audit_retriable
from utils.surge_run_extract import _login

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


async def execute_surge_rehydrate(
    task_id: str,
    tasks: dict,
    update_task: Callable,
):
    """
    Rehydration lifecycle:
      1. Launch fresh Playwright + login to Surge
      2. Navigate to history listing
      3. Find audit row matching practice_name + audit_date, extract run_url
      4. Reuse _extract_one_run() for tab-walk Diagnosis + Action plan capture
      5. Validate min char floors
      6. POST { audit_id, surge_raw_diagnosis, surge_raw_action_plan,
                rehydrate: true } to /api/process-entity-audit
    """
    req = tasks[task_id]["request"]
    audit_id = req["audit_id"]
    practice_name = req["practice_name"]
    audit_date = req.get("audit_date")
    client_slug = req["client_slug"]

    if not SURGE_EMAIL or not SURGE_PASSWORD:
        return await _fail_retriable(
            task_id, audit_id, update_task, None,
            "missing_credentials",
            "SURGE_EMAIL/SURGE_PASSWORD not configured on agent"
        )

    try:
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
                ctx = await browser.new_context(
                    permissions=["clipboard-read", "clipboard-write"],
                )
                page = await ctx.new_page()

                update_task(task_id, "login", "Logging into Surge")
                await _login(page, SURGE_URL, SURGE_EMAIL, SURGE_PASSWORD)

                update_task(task_id, "history", "Opening Surge history")
                run_url = await _find_run_url(page, practice_name, audit_date)
                if not run_url:
                    return await _fail_retriable(
                        task_id, audit_id, update_task, page,
                        "history_row_not_found",
                        f"Could not locate a Surge history run for "
                        f"'{practice_name}'"
                        + (f" on {audit_date}" if audit_date else "")
                    )

                logger.info(f"Rehydrate match: {run_url}")
                update_task(task_id, "extracting",
                            f"Walking Diagnosis + Action plan tabs at {run_url}")

                diagnosis_text, action_plan_text = await _walk_run_panes(page, run_url)

                if len(diagnosis_text) < MIN_DIAGNOSIS_CHARS:
                    return await _fail_retriable(
                        task_id, audit_id, update_task, page,
                        "diagnosis_pane_short",
                        f"Diagnosis returned {len(diagnosis_text)} chars, "
                        f"minimum {MIN_DIAGNOSIS_CHARS}"
                    )
                if len(action_plan_text) < MIN_ACTION_PLAN_CHARS:
                    return await _fail_retriable(
                        task_id, audit_id, update_task, page,
                        "action_plan_pane_short",
                        f"Action plan returned {len(action_plan_text)} chars, "
                        f"minimum {MIN_ACTION_PLAN_CHARS}"
                    )
                logger.info(
                    f"Captured panes: diagnosis={len(diagnosis_text):,}, "
                    f"action_plan={len(action_plan_text):,}"
                )

                update_task(
                    task_id, "callback",
                    f"Sending {len(diagnosis_text):,} + "
                    f"{len(action_plan_text):,} chars to Client HQ"
                )
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
                            f"CHQ returned {resp.status_code}: "
                            f"{resp.text[:300]}"
                        )

                update_task(
                    task_id, "complete",
                    f"Rehydrated {practice_name}: "
                    f"{len(diagnosis_text):,} diagnosis + "
                    f"{len(action_plan_text):,} action plan chars"
                )

            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    except Exception as e:
        logger.exception(f"Rehydrate task {task_id[:18]} failed")
        update_task(task_id, "error", f"Unexpected error: {str(e)[:200]}", error=str(e))
        try:
            await patch_audit_retriable(audit_id, "unexpected_error", str(e)[:300])
        except Exception:
            pass


async def _find_run_url(
    page,
    practice_name: str,
    audit_date: Optional[str],
) -> Optional[str]:
    """
    Walk Surge's dashboard / history view and return the
    /dashboard/run/<id> URL whose row text matches the practice name
    (and audit_date when available).

    Surge's history UI varies between dashboard layouts; this tries
    /dashboard, then /dashboard/history, then a header link click as
    a last resort. Match strategy: scan every <a> with /dashboard/run/
    in href, pick the one whose nearest container text includes the
    practice name. Prefer the one whose row text also contains the
    audit_date (YYYY-MM-DD or YYYY/MM/DD) when audit_date is provided.
    """
    candidates = [
        SURGE_URL.rstrip("/") + "/dashboard/history",
        SURGE_URL.rstrip("/") + "/dashboard",
    ]
    for url in candidates:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            continue
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        run_links = await page.evaluate(
            """({ practice, dateA, dateB }) => {
                const anchors = Array.from(document.querySelectorAll(
                    'a[href*="/dashboard/run/"]'
                ));
                const out = [];
                for (const a of anchors) {
                    const row = a.closest('tr')
                        || a.closest('[role="row"]')
                        || a.closest('[class*="row" i]')
                        || a.parentElement
                        || a;
                    const text = (row.innerText || a.innerText || '').trim();
                    out.push({
                        href: a.href,
                        match_practice: practice
                            ? text.toLowerCase().includes(practice.toLowerCase())
                            : true,
                        match_date: (dateA && text.includes(dateA))
                                 || (dateB && text.includes(dateB))
                                 || false,
                        text_preview: text.substring(0, 200),
                    });
                }
                return out;
            }""",
            {
                "practice": practice_name or "",
                "dateA": audit_date or "",
                "dateB": (audit_date or "").replace("-", "/"),
            },
        )

        if not run_links:
            continue

        # Filter to practice matches (or fall back to all if practice-name
        # filter eliminates everything — Surge sometimes renders the brand
        # in a separate column the row scoping doesn't reach).
        practice_matches = [r for r in run_links if r.get("match_practice")]
        pool = practice_matches if practice_matches else run_links

        # Date-priority pick when audit_date provided
        if audit_date:
            for r in pool:
                if r.get("match_date"):
                    logger.info(
                        f"_find_run_url date+practice match: "
                        f"{r['href']} via {url}"
                    )
                    return r["href"]

        if pool:
            logger.info(
                f"_find_run_url practice match (no date): "
                f"{pool[0]['href']} via {url}"
            )
            return pool[0]["href"]

    return None


async def _walk_run_panes(page, run_url: str) -> tuple:
    """
    Navigate to a /dashboard/run/<id> URL and capture the Diagnosis (tab 0)
    and Action plan (tab 2) panes via the Copy raw text button.

    Functionally equivalent to utils.surge_run_extract._extract_one_run
    but returns the two pane texts as a tuple instead of the combined +
    fenced single string. Inlined here so this task does not depend on
    private internals of surge_run_extract.
    """
    await page.goto(run_url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(5000)

    # Defensive in-page intercept: covers Surge builds that write to
    # in-page state instead of the system clipboard.
    await page.evaluate("""() => {
        window.__surgeChunks = [];
        const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = (text) => {
            if (text) window.__surgeChunks.push(text);
            return orig(text);
        };
    }""")

    try:
        await page.wait_for_selector('#report-view-tabs', timeout=15000)
    except Exception:
        logger.warning("_walk_run_panes: #report-view-tabs not present")
        return ("", "")

    async def click_tab_and_copy(idx, label):
        clicked = await page.evaluate(
            """(i) => {
                const root = document.querySelector('#report-view-tabs');
                if (!root) return false;
                const list = Array.from(root.children);
                const el = list[i];
                if (!el) return false;
                el.scrollIntoView({block: 'center'});
                const inner = el.querySelector('button, [role=button], [role=tab]');
                (inner || el).click();
                return true;
            }""",
            idx,
        )
        if not clicked:
            return ""
        await page.wait_for_timeout(2500)
        try:
            await page.evaluate(
                "() => navigator.clipboard.writeText('').catch(()=>{})"
            )
        except Exception:
            pass
        await page.evaluate("""() => {
            const btn = document.querySelector('button[title="Copy raw text"]');
            if (btn) btn.click();
        }""")
        await page.wait_for_timeout(3500)

        txt = ""
        try:
            clip = await page.evaluate(
                "() => navigator.clipboard.readText().catch(() => '')"
            )
            if isinstance(clip, str) and len(clip) > 500:
                txt = clip
        except Exception:
            pass
        if not txt:
            chunks = await page.evaluate("() => window.__surgeChunks || []")
            if isinstance(chunks, list) and chunks:
                last = chunks[-1]
                if last and len(last) > 500:
                    txt = last
        if txt:
            logger.info(f"_walk_run_panes: tab {idx} ({label}) {len(txt):,} chars")
        else:
            logger.warning(f"_walk_run_panes: tab {idx} ({label}) empty")
        return txt

    diagnosis = await click_tab_and_copy(0, "Diagnosis")
    action_plan = await click_tab_and_copy(2, "Action plan")
    return (diagnosis, action_plan)


async def _fail_retriable(task_id, audit_id, update_task, page, code, detail):
    if page is not None:
        try:
            from utils.debug_capture import capture_debug
            await capture_debug(task_id, page, code)
        except Exception:
            pass
    try:
        await patch_audit_retriable(audit_id, code, detail)
    except Exception:
        pass
    update_task(task_id, code, detail, error=detail)
