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
"""

import asyncio
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
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

app = FastAPI(title="Moonraker Host Admin", version="1.0.0")

ADMIN_API_KEY = os.getenv("AGENT_API_KEY", "")
MAX_TIMEOUT = 300
DEFAULT_TIMEOUT = 60
MAX_OUTPUT_BYTES = 1024 * 1024  # 1MB


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
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/admin/exec", response_model=ExecResponse)
async def exec_command(req: ExecRequest, authorization: str = Header(default="")):
    await verify_key(authorization)

    # Clamp timeout
    timeout = min(req.timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)

    logger.info(f"EXEC: {req.command!r} (timeout={timeout}s, cwd={req.working_dir})")

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
