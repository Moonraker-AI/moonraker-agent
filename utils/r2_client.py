"""
utils/r2_client.py
==================
Thin HTTP client for the Cloudflare Worker that fronts our R2 bucket
(`moonraker-r2-worker`, repo Moonraker-AI/moonraker-r2-worker).

The Worker owns R2 object writes via an R2 binding, so the agent VPS no
longer needs S3 SigV4 access keys. We talk to it with a single shared
Bearer secret instead.

Worker contract (see Moonraker-AI/moonraker-r2-worker / src/index.js):
  PUT    /ingest/<key>   body -> R2.put(); X-Meta-* headers become custom
                         metadata. Returns {ok, key, etag, version, size}.
  GET    /ingest/<key>   returns object body (private read).
  HEAD   /ingest/<key>   stat (returns headers, no body).
  DELETE /ingest/<key>   delete.
  GET    /serve/<key>    public read (only `dist/`, `staging/`, `assets/`
                         prefixes; routed through public_url()).

Auth: all /ingest/* endpoints require `Authorization: Bearer <R2_INGEST_SECRET>`.

Env vars consumed (read at call time so a test harness can inject):
  R2_INGEST_URL          Worker base URL, e.g.
                         https://moonraker-r2-worker.<account>.workers.dev
                         (trailing slashes are stripped).
  R2_INGEST_SECRET       Shared bearer secret matching the Worker.
  R2_MIGRATION_BUCKET    Sanity anchor; must equal "client-sites" because
                         the Worker is bound to that one bucket. We do NOT
                         use this for routing — only validated.

DEPRECATED env vars (no longer read by this module; safe to remove from
the agent .env once the Worker is live):
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_ENDPOINT

Public surface (compatible with existing call sites):
  is_configured()                    -> bool
  put_object(key, body, ...)         -> str   (returns key on success)
  get_object(key, ...)               -> bytes
  put(key, body, ...)                -> dict  (raw worker JSON response)
  get(key, ...)                      -> bytes
  head(key, ...)                     -> dict | None  (None on 404)
  delete(key, ...)                   -> None
  public_url(key)                    -> str   (https://.../serve/<key>)

Errors:
  R2NotConfigured     env vars missing
  R2NotFound          404 on a GET (head() swallows; get() raises)
  R2Error             any other non-2xx
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger("agent.r2_client")

# Timeouts: large bodies on PUT, snappy on metadata ops.
_PUT_TIMEOUT_S = 60.0
_GET_TIMEOUT_S = 60.0
_HEAD_TIMEOUT_S = 15.0
_DELETE_TIMEOUT_S = 15.0

# Worker is bound to this single bucket; any other value is config drift.
_EXPECTED_BUCKET = "client-sites"


class R2NotConfigured(RuntimeError):
    pass


class R2Error(RuntimeError):
    pass


class R2NotFound(R2Error):
    pass


# ── Env + URL helpers ────────────────────────────────────────────────────────

def _env() -> Tuple[str, str, str]:
    """Return (base_url, secret, bucket). Raises R2NotConfigured if any missing."""
    base = (os.getenv("R2_INGEST_URL") or "").rstrip("/")
    secret = os.getenv("R2_INGEST_SECRET") or ""
    bucket = os.getenv("R2_MIGRATION_BUCKET") or ""
    missing = [
        name for name, val in (
            ("R2_INGEST_URL", base),
            ("R2_INGEST_SECRET", secret),
            ("R2_MIGRATION_BUCKET", bucket),
        ) if not val
    ]
    if missing:
        raise R2NotConfigured(f"R2 not configured (missing: {', '.join(missing)})")
    if bucket != _EXPECTED_BUCKET:
        # Don't fail — caller may have a reason — but log loudly.
        logger.warning(
            "R2_MIGRATION_BUCKET=%r does not match expected %r; the Worker "
            "is bound to a single bucket so this value is only a sanity anchor.",
            bucket, _EXPECTED_BUCKET,
        )
    return base, secret, bucket


def _encode_key(key: str) -> str:
    """URL-encode each path segment (preserve slashes between segments).

    Validates against the Worker's key-shape rules: no '..', no '//',
    max 1024 chars, non-empty.
    """
    if not key:
        raise ValueError("R2 key cannot be empty")
    if ".." in key.split("/"):
        raise ValueError(f"R2 key cannot contain '..' segment: {key!r}")
    if "//" in key:
        raise ValueError(f"R2 key cannot contain '//': {key!r}")
    if len(key) > 1024:
        raise ValueError(f"R2 key exceeds 1024 chars (got {len(key)})")
    return "/".join(quote(seg, safe="") for seg in key.split("/"))


def _auth_header(secret: str) -> dict:
    # Format must be exact: "Bearer <secret>", no quotes, no extra spaces.
    return {"Authorization": f"Bearer {secret}"}


def _meta_headers(metadata: Optional[dict]) -> dict:
    """Convert metadata dict -> {'X-Meta-<key>': '<value>'} headers.

    Keys are passed through verbatim (the Worker is case-insensitive on
    header names per HTTP spec). None values are dropped; everything else
    is str()-ified.
    """
    if not metadata:
        return {}
    out: dict = {}
    for k, v in metadata.items():
        if v is None:
            continue
        clean_key = str(k).strip()
        if not clean_key:
            continue
        out[f"X-Meta-{clean_key}"] = str(v)
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def is_configured() -> bool:
    try:
        _env()
        return True
    except R2NotConfigured:
        return False


def public_url(key: str) -> str:
    """Construct the public /serve URL for a key.

    The Worker only serves `dist/`, `staging/`, `assets/` prefixes publicly;
    this helper does not enforce that — the caller is responsible for
    handing out URLs that will actually resolve. Used for admin UI preview
    links and migration result emails.
    """
    base = (os.getenv("R2_INGEST_URL") or "").rstrip("/")
    if not base:
        raise R2NotConfigured("R2_INGEST_URL not set")
    return f"{base}/serve/{_encode_key(key)}"


async def put_object(
    key: str,
    body: bytes,
    *,
    content_type: str = "application/octet-stream",
    cache_control: Optional[str] = None,
    metadata: Optional[dict] = None,
    timeout: float = _PUT_TIMEOUT_S,
) -> str:
    """Upload bytes to R2 via the Worker. Returns the key on success.

    `metadata` dict entries become `X-Meta-<key>` request headers, which the
    Worker persists as R2 customMetadata on the object.
    """
    base, secret, _bucket = _env()
    url = f"{base}/ingest/{_encode_key(key)}"
    headers = _auth_header(secret)
    headers["Content-Type"] = content_type
    if cache_control:
        headers["Cache-Control"] = cache_control
    headers.update(_meta_headers(metadata))

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.put(url, content=body, headers=headers)
    if resp.status_code >= 300:
        raise R2Error(
            f"R2 PUT {key} failed {resp.status_code}: {resp.text[:300]}"
        )
    logger.info("R2 PUT ok key=%s bytes=%d", key, len(body))
    return key


async def put(
    key: str,
    body: bytes,
    *,
    content_type: Optional[str] = None,
    metadata: Optional[dict] = None,
    timeout: float = _PUT_TIMEOUT_S,
) -> dict:
    """Spec-aligned PUT. Returns the parsed Worker JSON response."""
    base, secret, _bucket = _env()
    url = f"{base}/ingest/{_encode_key(key)}"
    headers = _auth_header(secret)
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(_meta_headers(metadata))

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.put(url, content=body, headers=headers)
    if resp.status_code >= 300:
        raise R2Error(f"R2 PUT {key} failed {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except Exception:
        return {"ok": True, "key": key, "size": len(body)}


async def get_object(key: str, *, timeout: float = _GET_TIMEOUT_S) -> bytes:
    """Read raw bytes for a key. Raises R2NotFound on 404, R2Error otherwise."""
    base, secret, _bucket = _env()
    url = f"{base}/ingest/{_encode_key(key)}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=_auth_header(secret))
    if resp.status_code == 404:
        raise R2NotFound(f"R2 GET {key} -> 404")
    if resp.status_code >= 300:
        raise R2Error(f"R2 GET {key} failed {resp.status_code}: {resp.text[:300]}")
    return resp.content


async def get(key: str, *, timeout: float = _GET_TIMEOUT_S) -> bytes:
    """Spec-aligned alias for get_object."""
    return await get_object(key, timeout=timeout)


async def head(key: str, *, timeout: float = _HEAD_TIMEOUT_S) -> Optional[dict]:
    """Stat a key. Returns response headers as a dict, or None on 404."""
    base, secret, _bucket = _env()
    url = f"{base}/ingest/{_encode_key(key)}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.head(url, headers=_auth_header(secret))
    if resp.status_code == 404:
        return None
    if resp.status_code >= 300:
        raise R2Error(f"R2 HEAD {key} failed {resp.status_code}: {resp.text[:300]}")
    return dict(resp.headers)


async def delete(key: str, *, timeout: float = _DELETE_TIMEOUT_S) -> None:
    """Delete a key. 404 is treated as already-gone (idempotent)."""
    base, secret, _bucket = _env()
    url = f"{base}/ingest/{_encode_key(key)}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.delete(url, headers=_auth_header(secret))
    if resp.status_code == 404:
        logger.info("R2 DELETE %s -> 404 (already gone)", key)
        return
    if resp.status_code >= 300:
        raise R2Error(f"R2 DELETE {key} failed {resp.status_code}: {resp.text[:300]}")
    logger.info("R2 DELETE ok key=%s", key)
