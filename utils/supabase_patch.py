"""
Supabase Patch Helper
=====================
Centralizes direct PATCH calls from the agent to entity_audits when a task
hits a terminal non-retriable failure (credits exhausted, Surge maintenance
mode, silent server-side rejection).

Flips the Supabase row to status='agent_error' with agent_error_retriable=false
so the Client HQ cron does NOT auto-requeue on its 30-minute cycle. Auto-heal
(hourly check-surge-blocks cron on Client HQ) handles Surge-side restoration
for the surge_maintenance and credits_exhausted codes; other codes require
manual intervention via the Client HQ admin UI.

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


async def patch_audit_terminal(
    audit_id: str,
    reason_code: str,
    detail: str,
    debug_path: str = "",
) -> bool:
    """
    PATCH entity_audits row to status='agent_error' with retriable=false.

    Args:
        audit_id: entity_audits.id UUID
        reason_code: short code (e.g. 'surge_maintenance', 'credits_exhausted',
                     'surge_rejected', 'generic_exception'). Written to
                     last_agent_error_code so the admin UI pill badges and
                     the auto-heal cron can filter on it.
        detail: human-readable explanation written to last_agent_error.
                No prefix is added — the code lives in its own column now.
        debug_path: optional /tmp/agent-debug/<task_id> path; persisted to
                    last_debug_path when non-empty.

    Returns:
        True on success, False on any failure (never raises — caller should
        still update local task state and send notification regardless).

    Notes:
        - updated_at is set by the `set_updated_at` BEFORE UPDATE trigger, so
          it is NOT included in the payload.
        - agent_task_id is cleared so a downstream requeue does not carry
          stale state into the next dispatch.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning(
            "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set; skipping terminal PATCH"
        )
        return False

    if not audit_id:
        logger.warning("patch_audit_terminal called with empty audit_id; skipping")
        return False

    payload = {
        "status": "agent_error",
        "agent_error_retriable": False,
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
                    f"Flipped audit {audit_id} to agent_error (retriable=false) "
                    f"with reason_code={reason_code}"
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


async def should_suppress_notification(
    reason_code: str,
    exclude_audit_id: str,
    window_hours: int = 2,
) -> bool:
    """
    Return True if another audit has already failed with the SAME reason_code
    in the past `window_hours`, so we avoid flooding the team with N identical
    emails when Surge is systemically down.

    Fails open (returns False) on any error so the first email always gets
    through even if the DB check errors. The downside of a false-negative is
    one extra email; the downside of a false-positive (suppressing incorrectly)
    is missing a real alert, so open-failure is the right default.

    Args:
        reason_code: the code just persisted (e.g. 'surge_maintenance')
        exclude_audit_id: the current audit's id, excluded from the match so
                          the row we just wrote doesn't suppress its own email
        window_hours: lookback window (default 2h)
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
        "agent_error_retriable": "eq.false",
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
                    f"(found {len(rows)} prior in last {window_hours}h)"
                )
                return True
            return False
    except Exception as e:
        logger.warning(f"should_suppress_notification failed: {e}")
        return False
