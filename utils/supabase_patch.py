"""
Supabase Patch Helper
=====================
Centralizes direct PATCH calls from the agent to entity_audits when a task
hits a terminal or retriable failure.

Two modes:
  * patch_audit_terminal  — retriable=false. Cron does NOT auto-requeue.
    Used for credits_exhausted, surge_maintenance, surge_rejected,
    target_blocked, generic_exception. Auto-heal (hourly check-surge-blocks
    cron) handles Surge-side restoration for the surge_maintenance and
    credits_exhausted codes; other codes require manual intervention via
    the Client HQ admin UI.

  * patch_audit_retriable — retriable=true. Cron Step 0.5 in
    process-audit-queue flips the row back to status='queued' after a
    5-min backoff. Used for phase2_timeout and truncated_extraction where
    the failure is most likely a transient Surge render issue that will
    resolve on the next attempt.

Service-role credentials read from env:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("agent.supabase_patch")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


def _headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


async def _patch_audit(
    audit_id: str,
    reason_code: str,
    detail: str,
    debug_path: str,
    retriable: bool,
) -> bool:
    """
    Shared PATCH core. Writes status='agent_error' + retriable flag +
    reason_code + detail + timestamp + clears agent_task_id.

    Callers should prefer patch_audit_terminal or patch_audit_retriable.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning(
            "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set; skipping PATCH"
        )
        return False

    if not audit_id:
        logger.warning("_patch_audit called with empty audit_id; skipping")
        return False

    payload = {
        "status": "agent_error",
        "agent_error_retriable": bool(retriable),
        "last_agent_error_code": reason_code,
        "last_agent_error": detail,
        "last_agent_error_at": datetime.now(timezone.utc).isoformat(),
        "agent_task_id": None,
    }
    if debug_path:
        payload["last_debug_path"] = debug_path

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/entity_audits?id=eq.{audit_id}",
                json=payload,
                headers={**_headers(), "Prefer": "return=minimal"},
            )
            if resp.status_code < 300:
                logger.info(
                    f"Flipped audit {audit_id} to agent_error "
                    f"(retriable={retriable}) with reason_code={reason_code}"
                    + (f" debug_path={debug_path}" if debug_path else "")
                )
                return True
            logger.warning(
                f"Supabase PATCH returned {resp.status_code} for audit {audit_id}: "
                f"{resp.text[:200]}"
            )
            return False
    except Exception as e:
        logger.warning(f"Supabase PATCH failed for audit {audit_id}: {e}")
        return False


async def patch_audit_terminal(
    audit_id: str,
    reason_code: str,
    detail: str,
    debug_path: str = "",
) -> bool:
    """
    PATCH entity_audits row to status='agent_error' with retriable=false.

    Use when the failure requires manual intervention (bad credentials,
    exhausted credits, target WAF block) or must be gated by the
    check-surge-blocks auto-heal cron (surge_maintenance, credits_exhausted).

    Notes:
        - updated_at is set by the `set_updated_at` BEFORE UPDATE trigger, so
          it is NOT included in the payload.
        - agent_task_id is cleared so a downstream requeue does not carry
          stale state into the next dispatch.
        - Returns True on success, False on any failure (never raises — caller
          should still update local task state and send notification regardless).
    """
    return await _patch_audit(audit_id, reason_code, detail, debug_path, retriable=False)


async def patch_audit_retriable(
    audit_id: str,
    reason_code: str,
    detail: str,
    debug_path: str = "",
) -> bool:
    """
    PATCH entity_audits row to status='agent_error' with retriable=true.

    Use when the failure is likely transient and the next dispatch on the
    same row should be attempted automatically. Step 0.5 in the Client HQ
    process-audit-queue cron flips status=agent_error + retriable=true
    rows back to status=queued after a 5-minute backoff.

    Current callers:
        - phase2_timeout          — Phase 2 marker fired but DOM never settled
                                    above the extraction threshold within the
                                    settle window
        - truncated_extraction    — Phase 3 extraction produced too few chars
                                    to trust; most likely a partial SPA render
                                    that will reload cleanly on retry

    Returns True on success, False on any failure (never raises).
    """
    return await _patch_audit(audit_id, reason_code, detail, debug_path, retriable=True)


async def should_suppress_notification(
    reason_code: str,
    exclude_audit_id: str,
    window_hours: int = 2,
    retriable: bool = False,
) -> bool:
    """
    Return True if another audit has already failed with the SAME reason_code
    AND the same retriable flag in the past `window_hours`, so we avoid
    flooding the team with N identical emails when Surge is systemically
    misbehaving (whether terminally or transiently).

    Fails open (returns False) on any error so the first email always gets
    through even if the DB check errors. The downside of a false-negative is
    one extra email; the downside of a false-positive (suppressing incorrectly)
    is missing a real alert, so open-failure is the right default.

    Args:
        reason_code: the code just persisted (e.g. 'surge_maintenance',
                     'phase2_timeout')
        exclude_audit_id: the current audit's id, excluded from the match so
                          the row we just wrote doesn't suppress its own email
        window_hours: lookback window (default 2h)
        retriable: match on agent_error_retriable. Defaults to False (terminal)
                   for back-compat with existing callers; retriable-failure
                   callers should pass retriable=True.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return False
    if not reason_code:
        return False

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ).isoformat()

    params = {
        "status": "eq.agent_error",
        "agent_error_retriable": f"eq.{'true' if retriable else 'false'}",
        "last_agent_error_code": f"eq.{reason_code}",
        "last_agent_error_at": f"gt.{cutoff}",
        "select": "id",
        "limit": "2",
    }
    if exclude_audit_id:
        params["id"] = f"neq.{exclude_audit_id}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/entity_audits",
                params=params,
                headers=_headers(),
            )
            if resp.status_code >= 300:
                logger.warning(
                    f"should_suppress_notification query returned {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                return False
            rows = resp.json()
            if isinstance(rows, list) and len(rows) > 0:
                logger.info(
                    f"Suppressing duplicate notification for reason={reason_code} "
                    f"retriable={retriable} "
                    f"(found {len(rows)} prior in last {window_hours}h)"
                )
                return True
            return False
    except Exception as e:
        logger.warning(f"should_suppress_notification failed: {e}")
        return False
