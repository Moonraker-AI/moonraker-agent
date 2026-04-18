"""
Supabase Patch Helper
=====================
Centralizes direct PATCH calls from the agent to entity_audits when a task
hits a terminal non-retriable failure (credits exhausted, Surge maintenance
mode, silent server-side rejection).

Flips the Supabase row to status='agent_error' with agent_error_retriable=false
so the Client HQ cron does NOT auto-requeue on its 30-minute cycle. Manual
intervention from Client HQ is required to dispatch again.

Service-role credentials read from env:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""

import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("agent.supabase_patch")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


async def patch_audit_terminal(audit_id: str, reason_code: str, detail: str) -> bool:
    """
    PATCH entity_audits row to status='agent_error' with retriable=false.

    Args:
        audit_id: entity_audits.id UUID
        reason_code: short code (e.g. 'surge_maintenance', 'credits_exhausted')
                     prepended to last_agent_error so admins can filter
        detail: human-readable explanation written to last_agent_error

    Returns:
        True on success, False on any failure (never raises — caller should
        still update local task state and send notification regardless).
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
        "last_agent_error": f"[{reason_code}] {detail}",
        "last_agent_error_at": datetime.now(timezone.utc).isoformat(),
        "agent_task_id": None,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/entity_audits?id=eq.{audit_id}",
                json=payload,
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
            )
            if resp.status_code < 300:
                logger.info(
                    f"Flipped audit {audit_id} to agent_error (retriable=false) "
                    f"with reason_code={reason_code}"
                )
                return True
            else:
                logger.warning(
                    f"Supabase PATCH returned {resp.status_code} for audit {audit_id}: "
                    f"{resp.text[:200]}"
                )
                return False
    except Exception as e:
        logger.warning(f"Supabase PATCH failed for audit {audit_id}: {e}")
        return False
