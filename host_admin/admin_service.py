"""
Moonraker Host Admin Service
=============================
Tiny FastAPI service running on the VPS HOST (not in Docker).
Provides remote command execution for Claude via HTTPS.

Runs as a systemd service on port 8001, proxied through Caddy/nginx
at agent.moonraker.ai/admin/*

Security:
- Same bearer token auth as the agent service
- Command timeout (60s default, 300s max)
- Output size capped at 1MB
- Audit logging of every command to /var/log/moonraker-admin/app.log
- Per-IP in-process rate limit (10 req / 60s) on /admin/exec
- Off-host Supabase audit tee for /admin/exec calls (fire-and-forget)
"""

import asyncio
import json
import logging
import os
import secrets
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from threading import Lock
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Logging ──────────────────────────────────────────────────────────────────

os.makedirs("/var/log", exist_ok=True)

file_handler = RotatingFileHandler(
    "/var/log/moonraker-admin/app.log",
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=3,
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)

logger = logging.getLogger("admin")
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(logging.StreamHandler())

# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="Moonraker Host Admin", version="1.1.0")

ADMIN_API_KEY = os.getenv("AGENT_API_KEY", "")
MAX_TIMEOUT = 300
DEFAULT_TIMEOUT = 60
MAX_OUTPUT_BYTES = 1024 * 1024  # 1MB

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
AUDIT_TABLE = "vps_admin_audit_log"

# ── Rate limit (per-IP, in-process) ──────────────────────────────────────────

_rate_lock = Lock()
_rate_state: dict[str, deque] = {}
_RATE_LIMIT = 10
_RATE_WINDOW = 60.0


def _client_ip(request: Request) -> str:
    """Prefer X-Forwarded-For (Caddy forwards it), fallback to client.host."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_check(ip: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        dq = _rate_state.setdefault(ip, deque())
        while dq and dq[0] <= now - _RATE_WINDOW:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            return False
        dq.append(now)
        return True


# ── Supabase audit tee (fire-and-forget) ─────────────────────────────────────

def _supabase_insert_sync(payload: dict) -> None:
    """Blocking POST to Supabase REST. Called from a thread via asyncio."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return
    try:
        url = f"{SUPABASE_URL}/rest/v1/{AUDIT_TABLE}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Prefer": "return=minimal",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status >= 300:
                logger.warning(f"supabase_audit.non2xx status={resp.status}")
    except Exception as e:
        # Never raise: we're fire-and-forget.
        logger.warning(f"supabase_audit.error {type(e).__name__}: {e}")


async def _audit_tee(
    client_ip: str,
    command: str,
    exit_code: int,
    duration_ms: int,
) -> None:
    """Insert an audit row into Supabase without blocking the request path."""
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "client_ip": client_ip,
        "command_truncated": (command or "")[:4096],
        "exit_code": exit_code,
        "duration_ms": duration_ms,
    }
    try:
        await asyncio.to_thread(_supabase_insert_sync, payload)
    except Exception as e:
        logger.warning(f"supabase_audit.schedule_error {type(e).__name__}: {e}")


# ── Auth ─────────────────────────────────────────────────────────────────────

async def verify_key(authorization: str = Header(default="")):
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=500, detail="AGENT_API_KEY not configured")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if not secrets.compare_digest(authorization[7:], ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Models ───────────────────────────────────────────────────────────────────

class ExecRequest(BaseModel):
    command: str
    timeout: Optional[int] = DEFAULT_TIMEOUT
    working_dir: Optional[str] = "/tmp"

class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    truncated: bool = False


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/admin/health")
async def admin_health(authorization: str = Header(default="")):
    await verify_key(authorization)
    return {
        "status": "ok",
        "service": "moonraker-host-admin",
        "version": "1.1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/admin/exec", response_model=ExecResponse)
async def exec_command(
    req: ExecRequest,
    request: Request,
    authorization: str = Header(default=""),
):
    # Rate limit BEFORE auth so throttled callers don't even reach the bearer check.
    ip = _client_ip(request)
    if not _rate_check(ip):
        logger.warning(f"rate_limit.exceeded ip={ip} limit={_RATE_LIMIT}/{_RATE_WINDOW}s")
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limit exceeded"},
            headers={"Retry-After": str(int(_RATE_WINDOW))},
        )

    await verify_key(authorization)

    # Clamp timeout
    timeout = min(req.timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)

    logger.info(f"EXEC ip={ip}: {req.command!r} (timeout={timeout}s, cwd={req.working_dir})")

    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_shell(
            req.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=req.working_dir,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning(f"TIMEOUT after {timeout}s: {req.command!r}")
            asyncio.create_task(_audit_tee(ip, req.command, -1, duration_ms))
            return ExecResponse(
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                exit_code=-1,
                duration_ms=duration_ms,
                truncated=False,
            )

    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error(f"EXEC ERROR: {e}")
        asyncio.create_task(_audit_tee(ip, req.command, -1, duration_ms))
        return ExecResponse(
            stdout="",
            stderr=str(e),
            exit_code=-1,
            duration_ms=duration_ms,
            truncated=False,
        )

    duration_ms = int((time.monotonic() - start) * 1000)

    # Truncate large output
    truncated = False
    stdout_str = stdout_bytes.decode("utf-8", errors="replace")
    stderr_str = stderr_bytes.decode("utf-8", errors="replace")

    if len(stdout_str) > MAX_OUTPUT_BYTES:
        stdout_str = stdout_str[:MAX_OUTPUT_BYTES] + "\n... [truncated]"
        truncated = True
    if len(stderr_str) > MAX_OUTPUT_BYTES:
        stderr_str = stderr_str[:MAX_OUTPUT_BYTES] + "\n... [truncated]"
        truncated = True

    logger.info(
        f"DONE: exit={proc.returncode} duration={duration_ms}ms "
        f"stdout={len(stdout_str)}b stderr={len(stderr_str)}b"
    )

    # Fire-and-forget audit tee to Supabase.
    asyncio.create_task(_audit_tee(ip, req.command, proc.returncode, duration_ms))

    return ExecResponse(
        stdout=stdout_str,
        stderr=stderr_str,
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        truncated=truncated,
    )


@app.get("/admin/system")
async def system_info(authorization: str = Header(default="")):
    """Quick system overview without needing to craft commands."""
    await verify_key(authorization)

    async def run(cmd):
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        return out.decode("utf-8", errors="replace").strip()

    memory = await run("free -m")
    disk = await run("df -h /")
    docker_ps = await run("docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'")
    zombies = await run("ps aux | grep -c ' Z '")
    uptime = await run("uptime")
    load = await run("cat /proc/loadavg")

    return {
        "uptime": uptime,
        "load": load,
        "memory": memory,
        "disk": disk,
        "docker": docker_ps,
        "zombie_count": int(zombies.strip()) - 1 if zombies.strip().isdigit() else zombies,
    }


@app.post("/admin/docker/{action}")
async def docker_action(action: str, container: str = "moonraker-agent", authorization: str = Header(default="")):
    """Quick Docker management: restart, stop, start, logs."""
    await verify_key(authorization)

    allowed_actions = {
        "restart": f"docker restart {container}",
        "stop": f"docker stop {container}",
        "start": f"docker start {container}",
        "logs": f"docker logs --tail 100 {container}",
        "stats": f"docker stats --no-stream {container}",
    }

    if action not in allowed_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{action}'. Allowed: {list(allowed_actions.keys())}",
        )

    logger.info(f"DOCKER: {action} {container}")

    proc = await asyncio.create_subprocess_shell(
        allowed_actions[action],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail=f"Docker {action} timed out")

    return {
        "action": action,
        "container": container,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "exit_code": proc.returncode,
    }
