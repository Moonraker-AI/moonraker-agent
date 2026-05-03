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
  - Captures TWO tabs: Diagnosis and Action plan (skips Opportunities, redundant)

Implementation note: this task uses raw Playwright (via async_playwright)
rather than Browser-Use, because:
  1. Browser-Use's Page wrapper exposes only goto/evaluate/screenshot,
     no wait_for_selector/fill/locator. Manual login requires those.
  2. clipboard-read permission must be granted at the BrowserContext
     level, which surge_run_extract already does via
     pw.chromium.launch().new_context(permissions=[...]).
  3. The shared _login + _extract_one_run helpers in utils/surge_run_extract
     already implement the exact tab-walk we need; reusing them avoids
     selector duplication.

History-page DOM truth (2026-05-03 capture):
  Surge's history listing no longer exposes <a href="/dashboard/run/<id>">
  anchors. Each row has:
    - Batch pill   <a href="/dashboard?batch=<batch-id>">  (links to batch
      view, not run view)
    - "View ->"    <button>View ->></button>  (no href, JS onClick handler;
      this is the actual drill-into-run navigation)
    - Rerun pill   <a href="/dashboard?rerun=<batch-id>">  (irrelevant)
  Match strategy: enumerate batch anchors, walk to the row container,
  match by practice-name (and date when provided), click the row's
  View button via JS evaluate, then wait for #report-view-tabs.

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
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional

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

_MONTHS_ABBR = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _date_acceptance_strings(audit_date: Optional[str]) -> List[str]:
    """
    Soften date matching against Surge UI drift.

    Surge displays "Apr 12" while CHQ stores audit_date=2026-04-10 — a
    ~2-day delta is normal (created_at vs run_finished_at + UTC offset).
    Generate a small acceptance set spanning [-2, +2] days, including
    YYYY-MM-DD, YYYY/MM/DD, MMM D, and MMM D, YYYY.

    Returns [] when audit_date is missing or unparseable.
    """
    if not audit_date:
        return []
    try:
        base = datetime.strptime(audit_date[:10], "%Y-%m-%d").date()
    except Exception:
        return []
    out: List[str] = []
    for delta in range(-2, 3):
        d = base + timedelta(days=delta)
        iso = d.strftime("%Y-%m-%d")
        slash = d.strftime("%Y/%m/%d")
        mon = _MONTHS_ABBR[d.month - 1]
        short = f"{mon} {d.day}"
        long_ = f"{mon} {d.day}, {d.year}"
        for s in (iso, slash, short, long_):
            if s not in out:
                out.append(s)
    return out


async def execute_surge_rehydrate(
    task_id: str,
    tasks: dict,
    update_task: Callable,
):
    """
    Rehydration lifecycle:
      1. Launch fresh Playwright + login to Surge
      2. SPA-click into history listing
      3. Find row matching practice_name + audit_date, click its View button
      4. Reuse _walk_run_panes() for tab-walk Diagnosis + Action plan capture
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
                opened = await _find_and_open_run(
                    page, practice_name, audit_date, task_id
                )
                if not opened:
                    return await _fail_retriable(
                        task_id, audit_id, update_task, page,
                        "history_row_not_found",
                        f"Could not locate a Surge history run for "
                        f"'{practice_name}'"
                        + (f" on {audit_date}" if audit_date else "")
                    )

                # Post-click snapshot of whatever the run-detail view rendered.
                # Always emitted so future failure modes (selector drift,
                # post-click URL pattern shift) are debuggable.
                try:
                    from utils.debug_capture import capture_debug
                    await capture_debug(task_id, page, "post_view_click")
                    logger.info("post_view_click snapshot captured")
                except Exception as cap_err:
                    logger.info(
                        f"post_view_click capture failed: {cap_err!r}"
                    )

                logger.info(f"Rehydrate row opened, current URL: {page.url}")
                update_task(
                    task_id, "extracting",
                    f"Walking Diagnosis + Action plan tabs at {page.url}"
                )

                diagnosis_text, action_plan_text = await _walk_run_panes(
                    page, "", skip_goto=True
                )

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


async def _find_and_open_run(
    page,
    practice_name: str,
    audit_date: Optional[str],
    task_id: Optional[str] = None,
) -> bool:
    """
    SPA-navigate to /dashboard/history, locate the row matching
    practice_name (+ optional audit_date), and click its View button.

    Surge's history page no longer exposes <a href="/dashboard/run/<id>">
    anchors. Each row contains:
      - <a href="/dashboard?batch=<batch-id>">   (the batch pill)
      - <button>View -></button>                 (the drill-into-run trigger,
                                                  pure JS onClick, no href)
    We enumerate batch-pill anchors to find row containers, match each
    row's innerText against practice (and date if given), then JS-click
    the matched row's View button. Run-detail load is detected via
    #report-view-tabs (timeout 20s).

    Returns True if a row was matched + clicked + #report-view-tabs
    rendered. False otherwise (caller emits history_row_not_found).
    """

    # Phase 1: get to /dashboard/history via the SPA nav anchor.
    # Caller (_login) just landed on /dashboard, so the rail is hydrated.
    try:
        await page.wait_for_selector(
            'a[href="/dashboard/history"]', timeout=8000
        )
    except Exception:
        logger.info("History nav anchor not visible within 8s")
        return False

    clicked = await page.evaluate(
        """() => {
            const a = document.querySelector('a[href="/dashboard/history"]');
            if (!a) return false;
            a.click();
            return true;
        }"""
    )
    if not clicked:
        logger.info("History nav anchor evaluate-click returned false")
        return False
    logger.info("History nav clicked")

    try:
        await page.wait_for_url("**/dashboard/history", timeout=15000)
    except Exception as e:
        logger.info(f"wait_for_url(/dashboard/history) failed: {e!r}")
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass

    # Unconditional snapshot of /dashboard/history BEFORE we attempt
    # to find rows. Lets us inspect Surge's real markup even when our
    # selector guess is wrong. Wrapped so capture errors never block
    # the main flow.
    if task_id:
        try:
            from utils.debug_capture import capture_debug
            await capture_debug(task_id, page, "history_page_snapshot")
            logger.info("history_page_snapshot captured")
        except Exception as cap_err:
            logger.info(
                f"history_page_snapshot capture failed: {cap_err!r}"
            )

    # Phase 2: wait for any row to render. Soften date matching ±2 days
    # to absorb created_at vs run_finished_at + UTC drift; soften the row
    # discovery to walk up from View buttons (the only reliable click
    # target; rows are pure <div>s with no batch-pill anchor in filtered
    # views). The batch-pill selector was dropping to 0 hits post-filter
    # while the View buttons remained — see Apr-23 audit notes.
    try:
        await page.wait_for_function(
            """() => {
                const VIEW_RE = /^view\\s*[\\u2192>]?\\s*$/i;
                return Array.from(document.querySelectorAll('button'))
                    .some(b => VIEW_RE.test((b.innerText || '').trim()));
            }""",
            timeout=15000,
        )
    except Exception:
        logger.info("No View buttons rendered within 15s")
        return False

    button_count = await page.evaluate(
        """() => {
            const VIEW_RE = /^view\\s*[\\u2192>]?\\s*$/i;
            return Array.from(document.querySelectorAll('button'))
                .filter(b => VIEW_RE.test((b.innerText || '').trim())).length;
        }"""
    )
    logger.info(
        f"History listing rendered with {button_count} View buttons "
        f"(default top-5 view)"
    )

    # Phase 2.5: filter to the practice. Search input drives a debounced
    # client-side filter; Surge re-renders rows using <div> containers
    # only (no batch-pill anchor), so we re-count via View buttons.
    if practice_name:
        try:
            search_sel = 'input[placeholder*="Search by query, brand"]'
            await page.wait_for_selector(search_sel, timeout=4000)
            await page.fill(search_sel, practice_name)
            await page.wait_for_timeout(1200)  # Surge debounce
            new_count = await page.evaluate(
                """() => {
                    const VIEW_RE = /^view\\s*[\\u2192>]?\\s*$/i;
                    return Array.from(document.querySelectorAll('button'))
                        .filter(b => VIEW_RE.test(
                            (b.innerText || '').trim()
                        )).length;
                }"""
            )
            logger.info(
                f"Search filter applied: {practice_name!r} -> "
                f"{new_count} View buttons visible"
            )
        except Exception as e:
            logger.info(
                f"Search input not present, continuing without filter: {e!r}"
            )

    # Phase 3: discover rows by walking UP from each View button. Match
    # against a softened date acceptance set; single-practice-match
    # auto-clicks even without a date hit. Multiple practice matches
    # require date disambiguation.
    date_strings = _date_acceptance_strings(audit_date)
    logger.info(
        f"Date acceptance set ({len(date_strings)} strings): "
        f"{date_strings!r}"
    )

    result = await page.evaluate(
        """({ practice, dateStrings }) => {
            const VIEW_RE = /^view\\s*[\\u2192>]?\\s*$/i;
            // Row signature: a container that holds the practice name AND
            // a View button AND some when-text (date abbrev or "ago" /
            // "Yesterday"). Walk UP from each View button up to N parents
            // until we hit a container that satisfies the signature.
            const viewBtns = Array.from(
                document.querySelectorAll('button')
            ).filter(b => VIEW_RE.test((b.innerText || '').trim()));
            const candidates = [];
            const seenRows = new Set();
            const practiceLc = (practice || '').toLowerCase();
            const WHEN_RE = /\\b(ago|Yesterday|Today|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\\d{4}-\\d{2}-\\d{2}|\\d{4}\\/\\d{2}\\/\\d{2})\\b/;
            for (const btn of viewBtns) {
                let row = null;
                let cur = btn;
                for (let i = 0; i < 8 && cur; i++) {
                    cur = cur.parentElement;
                    if (!cur) break;
                    const text = (cur.innerText || '').trim();
                    const hasPractice = practiceLc
                        ? text.toLowerCase().includes(practiceLc)
                        : true;
                    const hasWhen = WHEN_RE.test(text);
                    const hasView = Array.from(
                        cur.querySelectorAll('button')
                    ).some(b => VIEW_RE.test(
                        (b.innerText || '').trim()
                    ));
                    if (hasPractice && hasWhen && hasView) {
                        row = cur;
                        break;
                    }
                }
                if (!row) continue;
                if (seenRows.has(row)) continue;
                seenRows.add(row);
                const text = (row.innerText || '').trim();
                const matchPractice = practiceLc
                    ? text.toLowerCase().includes(practiceLc)
                    : true;
                const matchDate = (dateStrings || []).some(
                    s => s && text.includes(s)
                );
                candidates.push({
                    row, btn, text, matchPractice, matchDate,
                });
            }

            const practiceMatches = candidates.filter(c => c.matchPractice);

            // Pick: single practice match auto-clicks (date drift is OK).
            // Multiple practice matches require a date hit; if none match
            // any date in the soft acceptance set, fail loud rather than
            // guessing.
            let pick = null;
            if (practiceMatches.length === 1) {
                pick = practiceMatches[0];
            } else if (practiceMatches.length > 1) {
                if ((dateStrings || []).length) {
                    pick = practiceMatches.find(c => c.matchDate) || null;
                }
                if (!pick) {
                    return {
                        matched: false,
                        reason: 'multiple_practice_matches_no_date',
                        anchorCount: viewBtns.length,
                        candidateCount: practiceMatches.length,
                    };
                }
            }

            if (!pick) {
                return {
                    matched: false,
                    reason: 'practice_not_in_history',
                    anchorCount: viewBtns.length,
                    candidateCount: candidates.length,
                };
            }

            pick.btn.scrollIntoView({block: 'center'});
            pick.btn.click();
            return {
                matched: true,
                text_preview: pick.text.substring(0, 240),
                matchDate: pick.matchDate,
                matchPractice: pick.matchPractice,
                practiceMatchCount: practiceMatches.length,
            };
        }""",
        {
            "practice": practice_name or "",
            "dateStrings": date_strings,
        },
    )

    if not result or not result.get("matched"):
        reason = (result or {}).get("reason", "unknown")
        logger.info(
            f"_find_and_open_run no match: reason={reason} "
            f"detail={result!r}"
        )
        return False

    logger.info(
        f"_find_and_open_run clicked View on row "
        f"date_match={result.get('matchDate')} "
        f"practice_match={result.get('matchPractice')} "
        f"practice_matches={result.get('practiceMatchCount')} "
        f"preview={result.get('text_preview', '')[:120]!r}"
    )

    # Phase 4: wait for the run-detail view marker. Don't wait_for_url,
    # we don't know what URL pattern Surge uses (could be
    # /dashboard?batch=...&run=..., a hash route, or modal-overlay state).
    try:
        await page.wait_for_selector('#report-view-tabs', timeout=20000)
    except Exception as e:
        logger.info(
            f"#report-view-tabs not present after View click: {e!r}"
        )
        return False

    return True


async def _walk_run_panes(
    page,
    run_url: str,
    skip_goto: bool = False,
) -> tuple:
    """
    Capture the Diagnosis (tab 0) and Action plan (tab 2) panes via the
    Copy raw text button.

    When skip_goto=False (default), navigates to run_url via page.goto.
    When skip_goto=True (rehydrate flow), assumes the page is already on
    the run-detail view and that #report-view-tabs is already mounted
    (caller's responsibility, e.g. _find_and_open_run).

    Functionally equivalent to utils.surge_run_extract._extract_one_run
    but returns the two pane texts as a tuple instead of the combined +
    fenced single string. Inlined here so this task does not depend on
    private internals of surge_run_extract.
    """
    if not skip_goto:
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

    if not skip_goto:
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
