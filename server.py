"""
Moonraker Agent Service
=======================
FastAPI server that manages browser automation tasks.
Workflows: Surge entity audits, content audits, batch audits.

Architecture:
- Tasks stored in-memory (dict) — sufficient for low volume
- Background execution via asyncio.create_task
- Sequential execution (one browser at a time) to keep resource usage low
- Client HQ polls GET /tasks/{id}/status for progress updates
- On completion, agent POSTs results to Client HQ
"""

import asyncio
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from tasks.surge_content_audit import run_surge_content_audit
from tasks.surge_batch_audit import run_surge_batch_audit
from tasks.capture_design_assets import run_capture_design_assets
from tasks.apply_neo_overlay import run_apply_neo_overlay
from tasks.wp_scout import run_wp_scout
from tasks.sq_scout import run_sq_scout
from tasks.wix_scout import run_wix_scout
from tasks.surge_status_check import check_surge_status

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agent")

# Install secret redaction filter ASAP so no subsequent import can log a raw
# secret. Scrubs SURGE_PASSWORD, API keys, bearer tokens, etc. from all log
# records. See utils/log_redact.py for scope and residual risk.
from utils.log_redact import install as _install_log_redact
_install_log_redact()

AGENT_VERSION = "0.6.2"

app = FastAPI(
    title="Moonraker Agent Service",
    version=AGENT_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://clients.moonraker.ai"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Auth ─────────────────────────────────────────────────────────────────────

AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")

async def verify_api_key(authorization: str = Header(default="")):
    if not AGENT_API_KEY:
        raise HTTPException(status_code=500, detail="AGENT_API_KEY not configured")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if not secrets.compare_digest(authorization[7:], AGENT_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Task store ───────────────────────────────────────────────────────────────

tasks: dict[str, dict] = {}

# ── Tiered Execution ─────────────────────────────────────────────────────────
#
# Tier 1 (NO LOCK): CPU-only tasks — run immediately in parallel
#   - NEO image overlay (PIL compositing, ~1-2s)
#   - Image processing / conversions
#
# Tier 2 (BROWSER LOCK): Browser automation — sequential, one at a time
#   - Surge entity audits (Browser Use + Chrome, 20-35 min)
#   - Surge content audits (Browser Use + Chrome, 15-25 min)
#   - Surge batch audits (Browser Use + Chrome, 30-60 min)
#   - Design asset capture (Playwright, ~30s)
#   - WordPress playbook (Browser Use + Chrome, variable)
#
# The browser_lock ensures only one browser instance runs at a time
# (critical for 4GB RAM). Tier 1 tasks bypass it entirely.

browser_lock = asyncio.Lock()

# Cooldown between heavy browser tasks (seconds) — lets OS reclaim memory
HEAVY_TASK_COOLDOWN = 10
LIGHT_TASK_COOLDOWN = 2


# ── Models ───────────────────────────────────────────────────────────────────

# URL + list validation helpers. Shared across every task request model so a
# malformed URL is rejected at 422 before any task is queued — prevents SSRF
# probes like `file:///etc/passwd` or `http://169.254.169.254/...` from
# reaching Browser Use / Playwright with agent-side privileges. Bearer auth
# is still the primary control; this is defence in depth.

MAX_URL_LEN = 2048
MAX_TEXT_LEN = 4096
MAX_BATCH_PAGES = 100


def _validate_http_url(v: str) -> str:
    if not isinstance(v, str) or not v:
        raise ValueError("URL must be a non-empty string")
    if not v.lower().startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    if len(v) > MAX_URL_LEN:
        raise ValueError(f"URL too long (max {MAX_URL_LEN} chars)")
    return v


def _validate_optional_http_url(v):
    if v is None or v == "":
        return v
    return _validate_http_url(v)


# Mirrors utils/browser.py::_CREDENTIAL_ID_RE so malformed IDs are rejected at
# the API boundary, not at launch time. Accepts UUIDs, slugs, hex digests.
_CREDENTIAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_optional_credential_id(v):
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not _CREDENTIAL_ID_RE.match(v):
        raise ValueError("credential_id must match ^[A-Za-z0-9_-]{1,64}$")
    return v


class SurgeAuditRequest(BaseModel):
    audit_id: str
    practice_name: str
    website_url: str
    city: str
    state: str
    geo_target: Optional[str] = None
    gbp_link: Optional[str] = None
    client_slug: str

    _v_website = field_validator("website_url")(classmethod(lambda cls, v: _validate_http_url(v)))
    _v_gbp = field_validator("gbp_link")(classmethod(lambda cls, v: _validate_optional_http_url(v)))

class SurgeContentAuditRequest(BaseModel):
    content_page_id: str
    website_url: str
    target_keyword: str
    search_query: Optional[str] = None
    practice_name: Optional[str] = None
    page_type: Optional[str] = "service"
    city: Optional[str] = None
    state: Optional[str] = None
    geo_target: Optional[str] = None
    client_slug: Optional[str] = None
    callback_url: Optional[str] = None

    _v_website = field_validator("website_url")(classmethod(lambda cls, v: _validate_http_url(v)))
    _v_callback = field_validator("callback_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))

class BatchPageItem(BaseModel):
    content_page_id: str
    keyword: str
    target_url: str

    _v_target = field_validator("target_url")(classmethod(lambda cls, v: _validate_http_url(v)))

class SurgeBatchAuditRequest(BaseModel):
    batch_id: str
    client_slug: str
    brand_name: str
    gbp_url: str
    entity_type: Optional[str] = "Local Business"
    geo_target: Optional[str] = None
    website_url: Optional[str] = None
    pages: List[BatchPageItem] = Field(..., max_length=MAX_BATCH_PAGES)
    callback_url: Optional[str] = None

    _v_gbp = field_validator("gbp_url")(classmethod(lambda cls, v: _validate_http_url(v)))
    _v_website = field_validator("website_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))
    _v_callback = field_validator("callback_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))

class CaptureDesignAssetsRequest(BaseModel):
    design_spec_id: str
    client_slug: str
    website_url: str
    service_page_url: Optional[str] = None
    about_page_url: Optional[str] = None
    callback_url: Optional[str] = None

    _v_website = field_validator("website_url")(classmethod(lambda cls, v: _validate_http_url(v)))
    _v_service = field_validator("service_page_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))
    _v_about = field_validator("about_page_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))
    _v_callback = field_validator("callback_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))

class NeoOverlayRequest(BaseModel):
    base_image_url: str
    client_slug: str
    practice_name: str = ""
    plus_code: str = ""
    gbp_share_link: str = ""
    logo_drive_file_id: Optional[str] = None
    logo_url: Optional[str] = None
    output_name: Optional[str] = None
    neo_image_id: Optional[str] = None
    callback_url: Optional[str] = None

    _v_base = field_validator("base_image_url")(classmethod(lambda cls, v: _validate_http_url(v)))
    _v_logo = field_validator("logo_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))
    _v_gbp = field_validator("gbp_share_link")(classmethod(lambda cls, v: _validate_optional_http_url(v)))
    _v_callback = field_validator("callback_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))


class WpScoutRequest(BaseModel):
    wp_admin_url: str
    wp_username: str
    wp_password: str
    client_slug: Optional[str] = None
    callback_url: Optional[str] = None
    # When set, the browser fallback uses Patchright + a persistent
    # Chromium profile rooted at /data/profiles/<credential_id>. Reuses
    # cookies/sessions across runs so repeated scouts do not trigger
    # "unusual login" emails. Absent -> legacy ephemeral Playwright.
    credential_id: Optional[str] = None

    _v_admin = field_validator("wp_admin_url")(classmethod(lambda cls, v: _validate_http_url(v)))
    _v_callback = field_validator("callback_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))
    _v_credential = field_validator("credential_id")(classmethod(lambda cls, v: _validate_optional_credential_id(v)))


class SqScoutRequest(BaseModel):
    website_url: str
    client_slug: Optional[str] = None
    sq_email: Optional[str] = None
    sq_password: Optional[str] = None
    sq_site_id: Optional[str] = None
    callback_url: Optional[str] = None
    # When set, the admin panel scan uses Patchright + a persistent Chromium
    # profile at /data/profiles/<credential_id>. Typically one shared SQSP
    # "Moonraker admin" credential row drives every client site (SQSP's
    # contributor model lets one login access many sites), so this value
    # is usually the same across scouts. Absent -> legacy ephemeral path.
    credential_id: Optional[str] = None

    _v_website = field_validator("website_url")(classmethod(lambda cls, v: _validate_http_url(v)))
    _v_callback = field_validator("callback_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))
    _v_credential = field_validator("credential_id")(classmethod(lambda cls, v: _validate_optional_credential_id(v)))


class WixScoutRequest(BaseModel):
    website_url: str
    client_slug: Optional[str] = None
    callback_url: Optional[str] = None

    _v_website = field_validator("website_url")(classmethod(lambda cls, v: _validate_http_url(v)))
    _v_callback = field_validator("callback_url")(classmethod(lambda cls, v: _validate_optional_http_url(v)))
class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    message: str
    created_at: str
    updated_at: str
    error: Optional[str] = None
    duration_seconds: Optional[int] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def update_task(task_id: str, status: str, message: str, error: str = None):
    """Update task status in the in-memory store."""
    if task_id not in tasks:
        return
    now = datetime.now(timezone.utc).isoformat()
    tasks[task_id]["status"] = status
    tasks[task_id]["message"] = message
    tasks[task_id]["updated_at"] = now
    if error:
        tasks[task_id]["error"] = error
    # Calculate duration
    created = datetime.fromisoformat(tasks[task_id]["created_at"])
    tasks[task_id]["duration_seconds"] = int(
        (datetime.now(timezone.utc) - created).total_seconds()
    )
    logger.info(f"Task {task_id[:8]}: {status} — {message}")


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe for Docker HEALTHCHECK.
    Returns minimal state only — no task counts, no version disclosure.
    Container port is bound to 127.0.0.1 on the host so this endpoint is
    not reachable from the public internet (Caddy does not proxy /healthz)."""
    return {"status": "ok"}


@app.get("/health", dependencies=[Depends(verify_api_key)])
async def health():
    # Terminal states that do NOT count against active_tasks. Kept in sync
    # with the status codes the agent emits in terminal-failure paths:
    # complete/error for normal endings; credits_exhausted, surge_maintenance,
    # surge_rejected, and target_blocked for Phase 1.5 + retriable=false
    # failures (target_blocked = target site's WAF refused Surge's crawl).
    active_tasks = sum(
        1 for t in tasks.values()
        if t["status"] not in (
            "complete",
            "error",
            "credits_exhausted",
            "surge_maintenance",
            "surge_rejected",
            "target_blocked",
        )
    )
    return {
        "status": "ok",
        "service": "moonraker-agent",
        "version": AGENT_VERSION,
        "active_tasks": active_tasks,
        "total_tasks": len(tasks),
    }


@app.get("/ops/surge-status", dependencies=[Depends(verify_api_key)])
async def surge_status():
    """Independent Surge probe used by Client HQ's hourly auto-heal cron.

    Spawns a throwaway headless browser (does NOT acquire the audit lock),
    logs in via raw DOM fill (zero LLM calls), and reports maintenance +
    credit state. All error paths surface through the `error` field rather
    than raising, so the caller can make policy decisions without wrapping
    in try/except.

    Exposed under /ops/* (not /admin/*) because Caddy routes /admin/* on
    this host to the out-of-band admin service on port 8001. /ops/* falls
    through to the agent container on port 8000.
    """
    return await check_surge_status(timeout_seconds=60)


@app.post("/tasks/surge-audit", dependencies=[Depends(verify_api_key)])
async def create_surge_audit(request: SurgeAuditRequest):
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    tasks[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "message": "Audit queued, waiting for browser",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "duration_seconds": None,
        "request": request.model_dump(),
    }

    # Launch background task
    asyncio.create_task(_run_with_lock(task_id))
    logger.info(
        f"Queued surge audit {task_id[:8]} for {request.practice_name} "
        f"(audit_id={request.audit_id})"
    )

    return {"task_id": task_id, "status": "queued"}


@app.post("/tasks/surge-content-audit", dependencies=[Depends(verify_api_key)])
async def create_surge_content_audit(request: SurgeContentAuditRequest):
    task_id = f"content-{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat()

    tasks[task_id] = {
        "task_id": task_id,
        "type": "surge-content-audit",
        "status": "queued",
        "message": "Content audit queued, waiting for browser",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "duration_seconds": None,
        "request": request.model_dump(),
    }

    asyncio.create_task(_run_content_audit_with_lock(task_id))
    logger.info(
        f"Queued content audit {task_id[:12]} for keyword '{request.target_keyword}' "
        f"(content_page_id={request.content_page_id})"
    )

    return {"task_id": task_id, "status": "queued"}


@app.post("/tasks/surge-batch-audit", dependencies=[Depends(verify_api_key)])
async def create_surge_batch_audit(request: SurgeBatchAuditRequest):
    task_id = f"batch-{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat()

    tasks[task_id] = {
        "task_id": task_id,
        "type": "surge-batch-audit",
        "status": "queued",
        "message": f"Batch audit queued ({len(request.pages)} pages), waiting for browser",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "duration_seconds": None,
        "request": request.model_dump(),
    }

    asyncio.create_task(_run_batch_audit_with_lock(task_id))
    logger.info(
        f"Queued batch audit {task_id[:12]} for '{request.brand_name}' "
        f"({len(request.pages)} pages, batch_id={request.batch_id})"
    )

    return {"task_id": task_id, "status": "queued"}


@app.post("/tasks/capture-design-assets", dependencies=[Depends(verify_api_key)])
async def create_capture_design_assets(request: CaptureDesignAssetsRequest):
    task_id = f"design-{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat()

    tasks[task_id] = {
        "task_id": task_id,
        "type": "capture-design-assets",
        "status": "queued",
        "message": "Design asset capture queued, waiting for browser",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "duration_seconds": None,
        "request": request.model_dump(),
    }

    asyncio.create_task(_run_capture_design_with_lock(task_id))
    logger.info(
        f"Queued design capture {task_id[:12]} for '{request.client_slug}' "
        f"(url={request.website_url})"
    )

    return {"task_id": task_id, "status": "queued"}


@app.post("/tasks/apply-neo-overlay", dependencies=[Depends(verify_api_key)])
async def create_neo_overlay(request: NeoOverlayRequest):
    task_id = f"neo-{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat()

    tasks[task_id] = {
        "task_id": task_id,
        "type": "apply-neo-overlay",
        "status": "queued",
        "message": "NEO overlay queued, waiting for browser",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "duration_seconds": None,
        "request": request.model_dump(),
    }

    asyncio.create_task(_run_neo_overlay_no_lock(task_id))
    logger.info(
        f"Queued NEO overlay {task_id[:12]} for '{request.client_slug}' "
        f"(base={request.base_image_url[:60]})"
    )

    return {"task_id": task_id, "status": "queued"}




@app.post("/tasks/wp-scout", dependencies=[Depends(verify_api_key)])
async def create_wp_scout(request: WpScoutRequest):
    task_id = f"scout-{__import__('uuid').uuid4()}"
    now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()

    tasks[task_id] = {
        "task_id": task_id,
        "type": "wp-scout",
        "status": "queued",
        "message": "WP scout queued, waiting for browser",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "duration_seconds": None,
        "request": request.model_dump(),
        "result": None,
    }

    asyncio.create_task(_run_wp_scout_with_lock(task_id))
    logger.info(
        f"Queued WP scout {task_id[:12]} for '{request.wp_admin_url}'"
    )

    return {"task_id": task_id, "status": "queued"}

@app.post("/tasks/sq-scout", dependencies=[Depends(verify_api_key)])
async def create_sq_scout(request: SqScoutRequest):
    task_id = f"sqscout-{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat()

    tasks[task_id] = {
        "task_id": task_id,
        "type": "sq-scout",
        "status": "queued",
        "message": "Squarespace scout queued",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "duration_seconds": None,
        "request": request.model_dump(),
        "result": None,
    }

    asyncio.create_task(_run_sq_scout_with_lock(task_id))
    logger.info(
        f"Queued SQ scout {task_id[:12]} for '{request.website_url}'"
    )

    return {"task_id": task_id, "status": "queued"}

@app.post("/tasks/wix-scout", dependencies=[Depends(verify_api_key)])
async def create_wix_scout(request: WixScoutRequest):
    task_id = f"wixscout-{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat()

    tasks[task_id] = {
        "task_id": task_id,
        "type": "wix-scout",
        "status": "queued",
        "message": "Wix scout queued",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "duration_seconds": None,
        "request": request.model_dump(),
        "result": None,
    }

    asyncio.create_task(_run_wix_scout_with_lock(task_id))
    logger.info(
        f"Queued Wix scout {task_id[:12]} for '{request.website_url}'"
    )

    return {"task_id": task_id, "status": "queued"}

@app.get("/tasks/{task_id}/status", dependencies=[Depends(verify_api_key)])
async def get_task_status(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    t = tasks[task_id]
    return TaskStatusResponse(
        task_id=t["task_id"],
        status=t["status"],
        message=t["message"],
        created_at=t["created_at"],
        updated_at=t["updated_at"],
        error=t.get("error"),
        duration_seconds=t.get("duration_seconds"),
    )


@app.get("/tasks/{task_id}/result", dependencies=[Depends(verify_api_key)])
async def get_task_result(task_id: str):
    """Get the full result data for a completed task."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    t = tasks[task_id]
    if t["status"] not in ("complete", "error"):
        raise HTTPException(status_code=409, detail=f"Task still {t['status']}, no result yet")
    return {
        "task_id": t["task_id"],
        "status": t["status"],
        "type": t.get("type", ""),
        "result": t.get("result"),
        "duration_seconds": t.get("duration_seconds"),
    }


@app.get("/tasks", dependencies=[Depends(verify_api_key)])
async def list_tasks(status: Optional[str] = None, limit: int = 20):
    """List recent tasks, optionally filtered by status."""
    result = sorted(tasks.values(), key=lambda t: t["created_at"], reverse=True)
    if status:
        result = [t for t in result if t["status"] == status]
    return [
        TaskStatusResponse(
            task_id=t["task_id"],
            status=t["status"],
            message=t["message"],
            created_at=t["created_at"],
            updated_at=t["updated_at"],
            error=t.get("error"),
            duration_seconds=t.get("duration_seconds"),
        )
        for t in result[:limit]
    ]


# ── Task execution ───────────────────────────────────────────────────────────

async def _run_with_lock(task_id: str):
    """Tier 2 (Heavy Browser): Entity audit via Surge."""
    async with browser_lock:
        try:
            from utils.cleanup import preflight_cleanup
            logger.info(f"Task {task_id[:8]}: running pre-flight cleanup")
            await asyncio.to_thread(preflight_cleanup)
        except Exception as cleanup_err:
            logger.warning(f"Pre-flight cleanup failed: {cleanup_err}")

        update_task(task_id, "running", "Starting browser automation")
        try:
            from tasks.surge_audit import execute_surge_audit
            await execute_surge_audit(task_id, tasks, update_task)
        except Exception as e:
            logger.exception(f"Task {task_id[:8]} failed with unexpected error")
            update_task(task_id, "error", f"Unexpected error: {str(e)[:200]}", error=str(e))
            try:
                from utils.notifications import send_error_notification
                req = tasks[task_id].get("request", {})
                await send_error_notification(
                    practice_name=req.get("practice_name", "Unknown"),
                    client_slug=req.get("client_slug", ""),
                    error_message=str(e),
                    task_id=task_id,
                )
            except Exception as notify_err:
                logger.error(f"Failed to send error notification: {notify_err}")

        logger.info(f"Post-audit cooldown: {HEAVY_TASK_COOLDOWN}s before next task")
        await asyncio.sleep(HEAVY_TASK_COOLDOWN)


async def _run_content_audit_with_lock(task_id: str):
    """Tier 2 (Heavy Browser): Content audit via Surge."""
    async with browser_lock:
        try:
            from utils.cleanup import preflight_cleanup
            logger.info(f"Task {task_id[:12]}: running pre-flight cleanup")
            await asyncio.to_thread(preflight_cleanup)
        except Exception as cleanup_err:
            logger.warning(f"Pre-flight cleanup failed: {cleanup_err}")

        update_task(task_id, "running", "Starting content audit browser automation")
        try:
            env = {
                "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
                "SURGE_URL": os.getenv("SURGE_URL", ""),
                "SURGE_EMAIL": os.getenv("SURGE_EMAIL", ""),
                "SURGE_PASSWORD": os.getenv("SURGE_PASSWORD", ""),
                "CLIENT_HQ_URL": os.getenv("CLIENT_HQ_URL", ""),
                "AGENT_API_KEY": os.getenv("AGENT_API_KEY", ""),
            }
            await run_surge_content_audit(
                task_id=task_id,
                params=tasks[task_id]["request"],
                status_callback=_async_update_task,
                env=env,
            )
        except Exception as e:
            logger.exception(f"Task {task_id[:12]} failed with unexpected error")
            update_task(task_id, "error", f"Unexpected error: {str(e)[:200]}", error=str(e))
            try:
                from utils.notifications import send_error_notification
                req = tasks[task_id].get("request", {})
                await send_error_notification(
                    practice_name=req.get("practice_name", "Unknown"),
                    client_slug=req.get("client_slug", ""),
                    error_message=str(e),
                    task_id=task_id,
                )
            except Exception as notify_err:
                logger.error(f"Failed to send error notification: {notify_err}")

        logger.info(f"Post-audit cooldown: {HEAVY_TASK_COOLDOWN}s before next task")
        await asyncio.sleep(HEAVY_TASK_COOLDOWN)


async def _run_batch_audit_with_lock(task_id: str):
    """Tier 2 (Heavy Browser): Batch audit via Surge."""
    async with browser_lock:
        try:
            from utils.cleanup import preflight_cleanup
            logger.info(f"Task {task_id[:12]}: running pre-flight cleanup")
            await asyncio.to_thread(preflight_cleanup)
        except Exception as cleanup_err:
            logger.warning(f"Pre-flight cleanup failed: {cleanup_err}")

        update_task(task_id, "running", "Starting batch audit browser automation")
        try:
            env = {
                "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
                "SURGE_URL": os.getenv("SURGE_URL", ""),
                "SURGE_EMAIL": os.getenv("SURGE_EMAIL", ""),
                "SURGE_PASSWORD": os.getenv("SURGE_PASSWORD", ""),
                "CLIENT_HQ_URL": os.getenv("CLIENT_HQ_URL", ""),
                "AGENT_API_KEY": os.getenv("AGENT_API_KEY", ""),
                "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
                "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
                "RESEND_API_KEY": os.getenv("RESEND_API_KEY", ""),
            }
            await run_surge_batch_audit(
                task_id=task_id,
                params=tasks[task_id]["request"],
                status_callback=_async_update_task,
                env=env,
            )
        except Exception as e:
            logger.exception(f"Task {task_id[:12]} failed with unexpected error")
            update_task(task_id, "error", f"Unexpected error: {str(e)[:200]}", error=str(e))
            try:
                from utils.notifications import send_error_notification
                req = tasks[task_id].get("request", {})
                await send_error_notification(
                    practice_name=req.get("brand_name", "Unknown"),
                    client_slug=req.get("client_slug", ""),
                    error_message=str(e),
                    task_id=task_id,
                )
            except Exception as notify_err:
                logger.error(f"Failed to send error notification: {notify_err}")

        logger.info(f"Post-audit cooldown: {HEAVY_TASK_COOLDOWN}s before next task")
        await asyncio.sleep(HEAVY_TASK_COOLDOWN)


async def _async_update_task(task_id: str, status: str, message: str):
    """Async wrapper around update_task for use as a status_callback."""
    update_task(task_id, status, message)


async def _run_capture_design_with_lock(task_id: str):
    """Tier 2 (Light Browser): Design asset capture via Playwright (~30s)."""
    async with browser_lock:
        update_task(task_id, "running", "Starting design asset capture")
        try:
            env = {
                "AGENT_API_KEY": os.getenv("AGENT_API_KEY", ""),
                "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
                "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            }
            await run_capture_design_assets(
                task_id=task_id,
                params=tasks[task_id]["request"],
                status_callback=_async_update_task,
                env=env,
            )
        except Exception as e:
            logger.exception(f"Design capture {task_id[:12]} failed")
            update_task(task_id, "error", f"Unexpected error: {str(e)[:200]}", error=str(e))
        # Light browser task — short cooldown
        await asyncio.sleep(LIGHT_TASK_COOLDOWN)


async def _run_neo_overlay_no_lock(task_id: str):
    """Tier 1 (No Lock): CPU-only image compositing. Runs immediately."""
    update_task(task_id, "running", "Starting NEO overlay")
    try:
        env = {
            "AGENT_API_KEY": os.getenv("AGENT_API_KEY", ""),
            "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
            "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            "GOOGLE_SERVICE_ACCOUNT_JSON": os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
        }
        await run_apply_neo_overlay(
            task_id=task_id,
            params=tasks[task_id]["request"],
            status_callback=_async_update_task,
            env=env,
        )
    except Exception as e:
        logger.exception(f"NEO overlay {task_id[:12]} failed")
        update_task(task_id, "error", f"Unexpected error: {str(e)[:200]}", error=str(e))


async def _run_wp_scout_with_lock(task_id: str):
    """Acquire the sequential execution lock, then run the WP scout."""
    async with browser_lock:
        try:
            from utils.cleanup import preflight_cleanup
            logger.info(f"Task {task_id[:12]}: running pre-flight cleanup")
            await asyncio.to_thread(preflight_cleanup)
        except Exception as cleanup_err:
            logger.warning(f"Pre-flight cleanup failed: {cleanup_err}")

        update_task(task_id, "running", "Starting WP scout browser automation")
        try:
            env = {
                "AGENT_API_KEY": os.getenv("AGENT_API_KEY", ""),
            }
            result = await run_wp_scout(
                task_id=task_id,
                params=tasks[task_id]["request"],
                status_callback=_async_update_task,
                env=env,
            )
            if task_id in tasks and result:
                tasks[task_id]["result"] = result
        except Exception as e:
            logger.exception(f"WP Scout {task_id[:12]} failed")
            update_task(task_id, "error", f"Scout failed: {str(e)[:200]}", error=str(e))

        logger.info(f"Post-task cooldown: {LIGHT_TASK_COOLDOWN}s")
        await asyncio.sleep(LIGHT_TASK_COOLDOWN)


async def _run_sq_scout_with_lock(task_id: str):
    """Acquire the sequential execution lock, then run the SQ scout."""
    async with browser_lock:
        try:
            from utils.cleanup import preflight_cleanup
            logger.info(f"Task {task_id[:12]}: running pre-flight cleanup")
            await asyncio.to_thread(preflight_cleanup)
        except Exception as cleanup_err:
            logger.warning(f"Pre-flight cleanup failed: {cleanup_err}")

        update_task(task_id, "running", "Starting Squarespace scout")
        try:
            env = {
                "AGENT_API_KEY": os.getenv("AGENT_API_KEY", ""),
                "SQ_EMAIL": os.getenv("SQ_EMAIL", ""),
                "SQ_PASSWORD": os.getenv("SQ_PASSWORD", ""),
            }
            result = await run_sq_scout(
                task_id=task_id,
                params=tasks[task_id]["request"],
                status_callback=_async_update_task,
                env=env,
            )
            if task_id in tasks and result:
                tasks[task_id]["result"] = result
        except Exception as e:
            logger.exception(f"SQ Scout {task_id[:12]} failed")
            update_task(task_id, "error", f"Scout failed: {str(e)[:200]}", error=str(e))

        logger.info(f"Post-task cooldown: {LIGHT_TASK_COOLDOWN}s")
        await asyncio.sleep(LIGHT_TASK_COOLDOWN)


async def _run_wix_scout_with_lock(task_id: str):
    """Acquire the sequential execution lock, then run the Wix scout."""
    async with browser_lock:
        try:
            from utils.cleanup import preflight_cleanup
            logger.info(f"Task {task_id[:12]}: running pre-flight cleanup")
            await asyncio.to_thread(preflight_cleanup)
        except Exception as cleanup_err:
            logger.warning(f"Pre-flight cleanup failed: {cleanup_err}")

        update_task(task_id, "running", "Starting Wix scout")
        try:
            env = {
                "AGENT_API_KEY": os.getenv("AGENT_API_KEY", ""),
            }
            result = await run_wix_scout(
                task_id=task_id,
                params=tasks[task_id]["request"],
                status_callback=_async_update_task,
                env=env,
            )
            if task_id in tasks and result:
                tasks[task_id]["result"] = result
        except Exception as e:
            logger.exception(f"Wix Scout {task_id[:12]} failed")
            update_task(task_id, "error", f"Scout failed: {str(e)[:200]}", error=str(e))

        logger.info(f"Post-task cooldown: {LIGHT_TASK_COOLDOWN}s")
        await asyncio.sleep(LIGHT_TASK_COOLDOWN)
