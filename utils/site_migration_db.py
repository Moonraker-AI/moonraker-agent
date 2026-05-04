"""
utils/site_migration_db.py
==========================
Direct Supabase REST helpers for the site-migration tables. Mirrors the
service-role-key pattern used by utils/supabase_patch.py.

Tables touched:
  site_migration_pages       — patched on capture + rewrite completion
  site_migration_assets      — upserted on every capture (sha256 dedupe)
  error_log                  — appended on capture/rewrite failure

All helpers are async + best-effort: they log on non-2xx and return False
rather than raising. Tasks already wrap their bodies in try/finally.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("agent.site_migration_db")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


def _headers(prefer: Optional[str] = None) -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ready() -> bool:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing; skipping write")
        return False
    return True


async def patch_page(page_id: str, payload: dict) -> bool:
    if not _ready() or not page_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/site_migration_pages?id=eq.{page_id}",
                json=payload,
                headers=_headers(prefer="return=minimal"),
            )
            if resp.status_code >= 300:
                logger.warning(
                    f"patch_page {page_id} returned {resp.status_code}: {resp.text[:300]}"
                )
                return False
            return True
    except Exception as e:
        logger.warning(f"patch_page {page_id} failed: {e}")
        return False


async def get_page(page_id: str) -> Optional[dict]:
    if not _ready() or not page_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/site_migration_pages?id=eq.{page_id}&select=*",
                headers=_headers(),
            )
            if resp.status_code >= 300:
                logger.warning(f"get_page {page_id} returned {resp.status_code}")
                return None
            rows = resp.json()
            return rows[0] if isinstance(rows, list) and rows else None
    except Exception as e:
        logger.warning(f"get_page {page_id} failed: {e}")
        return None


async def upsert_asset(row: dict) -> Optional[dict]:
    """Upsert into site_migration_assets keyed on (migration_id, sha256).

    Caller must pre-populate at least migration_id, sha256, origin_url.
    Returns the persisted row, or None on failure.
    """
    if not _ready():
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/site_migration_assets"
                "?on_conflict=migration_id,sha256",
                json=row,
                headers=_headers(prefer="resolution=merge-duplicates,return=representation"),
            )
            if resp.status_code >= 300:
                logger.warning(
                    f"upsert_asset failed {resp.status_code}: {resp.text[:300]}"
                )
                return None
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning(f"upsert_asset failed: {e}")
        return None


async def get_assets_for_migration(migration_id: str) -> list[dict]:
    """Return all asset rows for a migration. Used by the rewrite task to
    build the origin -> CF Images URL map fed to Claude."""
    if not _ready() or not migration_id:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/site_migration_assets"
                f"?migration_id=eq.{migration_id}&select=*",
                headers=_headers(),
            )
            if resp.status_code >= 300:
                logger.warning(
                    f"get_assets_for_migration {migration_id} {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                return []
            rows = resp.json()
            return rows if isinstance(rows, list) else []
    except Exception as e:
        logger.warning(f"get_assets_for_migration failed: {e}")
        return []


async def log_error(
    *,
    kind: str,
    migration_id: str,
    page_id: Optional[str],
    error: str,
    extra: Optional[dict] = None,
) -> bool:
    """Append a row to error_log so Vercel/CHQ can surface failures."""
    if not _ready():
        return False
    payload: dict[str, Any] = {
        "kind": kind,
        "migration_id": migration_id,
        "page_id": page_id,
        "error": error[:4000] if error else "",
        "created_at": _now(),
    }
    if extra:
        payload["extra"] = extra
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/error_log",
                json=payload,
                headers=_headers(prefer="return=minimal"),
            )
            if resp.status_code >= 300:
                # error_log table may not be present in dev — log + continue
                logger.warning(
                    f"log_error {kind} returned {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                return False
            return True
    except Exception as e:
        logger.warning(f"log_error {kind} failed: {e}")
        return False
