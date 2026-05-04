"""
utils/r2_client.py
==================
Thin SigV4 client for Cloudflare R2 (S3-compatible).

We deliberately do NOT pull in boto3 — the agent image stays minimal. R2 only
needs PUT/GET on a single bucket, and we already have httpx in the runtime.
This module implements just enough AWS SigV4 to talk to R2.

Reads creds from env at call time (so a test harness can inject):
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_ENDPOINT             e.g. https://<account-id>.r2.cloudflarestorage.com
  R2_MIGRATION_BUCKET     e.g. moonraker-site-migrations

Region is fixed to "auto" per Cloudflare's published guidance.

If creds are missing the helpers raise R2NotConfigured. Callers in the
site-migration tasks turn that into an error_log row + a graceful task ack
(spec §4: surface clear error rather than silently failing).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import logging
import os
from typing import Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger("agent.r2_client")


class R2NotConfigured(RuntimeError):
    pass


class R2Error(RuntimeError):
    pass


def _env() -> Tuple[str, str, str, str]:
    access = os.getenv("R2_ACCESS_KEY_ID", "")
    secret = os.getenv("R2_SECRET_ACCESS_KEY", "")
    endpoint = os.getenv("R2_ENDPOINT", "").rstrip("/")
    bucket = os.getenv("R2_MIGRATION_BUCKET", "")
    missing = [k for k, v in (
        ("R2_ACCESS_KEY_ID", access),
        ("R2_SECRET_ACCESS_KEY", secret),
        ("R2_ENDPOINT", endpoint),
        ("R2_MIGRATION_BUCKET", bucket),
    ) if not v]
    if missing:
        raise R2NotConfigured(f"R2 not configured (missing: {', '.join(missing)})")
    return access, secret, endpoint, bucket


# ── SigV4 ────────────────────────────────────────────────────────────────────

_SERVICE = "s3"
_REGION = "auto"
_ALG = "AWS4-HMAC-SHA256"


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str) -> bytes:
    k = ("AWS4" + secret).encode("utf-8")
    k = _hmac(k, date_stamp)
    k = _hmac(k, _REGION)
    k = _hmac(k, _SERVICE)
    return _hmac(k, "aws4_request")


def _sign_request(
    *,
    method: str,
    endpoint: str,
    bucket: str,
    key: str,
    body: bytes,
    extra_headers: Optional[dict] = None,
) -> Tuple[str, dict]:
    """Return (url, headers) ready for httpx."""
    access, secret, _ep, _bk = _env()  # validates creds present

    # Always use path-style addressing; R2 supports it cleanly.
    # Key is the object path (no leading slash); we url-encode each segment
    # so spaces and unicode are safe.
    encoded_key = "/".join(quote(seg, safe="") for seg in key.split("/"))
    canonical_uri = f"/{bucket}/{encoded_key}"
    url = f"{endpoint}{canonical_uri}"

    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = _sha256_hex(body or b"")
    host = endpoint.split("://", 1)[1]

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if extra_headers:
        for hk, hv in extra_headers.items():
            headers[hk.lower()] = hv

    signed_header_names = sorted(headers.keys())
    canonical_headers = "".join(f"{h}:{headers[h]}\n" for h in signed_header_names)
    signed_headers = ";".join(signed_header_names)

    canonical_request = "\n".join([
        method,
        canonical_uri,
        "",  # canonical query string
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{_REGION}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        _ALG,
        amz_date,
        credential_scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])

    signing_key = _signing_key(secret, date_stamp)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth = (
        f"{_ALG} Credential={access}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    out_headers = {k: v for k, v in headers.items()}
    out_headers["authorization"] = auth
    return url, out_headers


# ── Public API ───────────────────────────────────────────────────────────────

async def put_object(
    key: str,
    body: bytes,
    *,
    content_type: str = "application/octet-stream",
    cache_control: Optional[str] = None,
    timeout: float = 30.0,
) -> str:
    """Upload bytes to R2 at <bucket>/<key>. Returns the key on success."""
    _access, _secret, endpoint, bucket = _env()
    extra = {"content-type": content_type}
    if cache_control:
        extra["cache-control"] = cache_control
    url, headers = _sign_request(
        method="PUT",
        endpoint=endpoint,
        bucket=bucket,
        key=key,
        body=body,
        extra_headers=extra,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.put(url, content=body, headers=headers)
        if resp.status_code >= 300:
            raise R2Error(
                f"R2 PUT {key} failed {resp.status_code}: {resp.text[:300]}"
            )
    logger.info(f"R2 PUT ok bucket={bucket} key={key} bytes={len(body)}")
    return key


async def get_object(key: str, *, timeout: float = 30.0) -> bytes:
    """Read raw bytes from R2 at <bucket>/<key>."""
    _access, _secret, endpoint, bucket = _env()
    url, headers = _sign_request(
        method="GET",
        endpoint=endpoint,
        bucket=bucket,
        key=key,
        body=b"",
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code >= 300:
            raise R2Error(
                f"R2 GET {key} failed {resp.status_code}: {resp.text[:300]}"
            )
        return resp.content


def is_configured() -> bool:
    try:
        _env()
        return True
    except R2NotConfigured:
        return False
