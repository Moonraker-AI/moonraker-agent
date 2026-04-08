"""
Moonraker Agent Service
=======================
FastAPI server that manages browser automation tasks.
First workflow: Surge entity audits.

Architecture:
- Tasks stored in-memory (dict) — sufficient for low volume (~24 audits/month)
- Background execution via asyncio.create_task
- Sequential execution (one browser at a time) to keep resource usage low
- Client HQ polls GET /tasks/{id}/status for progress updates
- On completion, agent POSTs results to Client HQ's /api/process-entity-audit
"""

import asyncio
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agent")

app = FastAPI(title="Moonraker Agent Service", version="0.2.1")

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

# Sequential execution lock — one browser task at a time
task_lock = asyncio.Lock()


# ── Models ───────────────────────────────────────────────────────────────────

class SurgeAuditRequest(BaseModel):
    audit_id: str
    practice_name: str
    website_url: str
    city: str
    state: str
    geo_target: Optional[str] = None
    gbp_link: Optional[str] = None
    client_slug: str

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

@app.get("/health")
async def health():
    active_tasks = sum(
        1 for t in tasks.values()
        if t["status"] not in ("complete", "error", "credits_exhausted")
    )
    return {
        "status": "ok",
        "service": "moonraker-agent",
        "version": "0.2.1",
        "active_tasks": active_tasks,
        "total_tasks": len(tasks),
    }


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
    """Acquire the sequential execution lock, then run the audit."""
    async with task_lock:
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
