"""
utils/cf_images.py
==================
Tiny client for Cloudflare Images REST API.

Used by the site-migration capture task to mirror image originals into
Cloudflare Images so the rewritten HTML can reference Image Delivery URLs.

Reads creds from env at call time:
  CF_API_TOKEN     (must have Images:Edit scope)
  CF_ACCOUNT_ID
  CF_IMAGES_HASH   (per-account image hash; required to construct
                    imagedelivery.net URLs in the rewriter)

When CF_IMAGES_HASH is unset, the integration is OFF: every public
function in this module returns None / no-op without raising. This is
the default since 2026-05 — the Moonraker Cloudflare account runs on the
free Images tier which does not include hosted images. Site migrations
serve assets directly through the R2 Worker's `/serve/<key>` route via
`utils.r2_client.public_url()` instead.

If CF_IMAGES_HASH is set later, the original behavior is restored
without any code changes — re-enable per-migration if/when a fleet site
justifies the cost.

If `is_configured()` returns True but a single upload fails, callers
should log + continue — the migration can still proceed using R2-served
origins. The CF Images mirror is a delivery convenience, not a
correctness gate.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("agent.cf_images")


class CFImagesNotConfigured(RuntimeError):
    pass


class CFImagesError(RuntimeError):
    pass


# One-shot startup notice when the integration is disabled at import time.
# We only log this once per process to keep capture logs quiet.
if not os.getenv("CF_IMAGES_HASH", "").strip():
    logger.info(
        "cf_images: CF_IMAGES_HASH not set; Cloudflare Images integration disabled"
    )


def _env():
    token = os.getenv("CF_API_TOKEN", "")
    account = os.getenv("CF_ACCOUNT_ID", "")
    image_hash = os.getenv("CF_IMAGES_HASH", "")
    missing = [
        k for k, v in (
            ("CF_API_TOKEN", token),
            ("CF_ACCOUNT_ID", account),
            ("CF_IMAGES_HASH", image_hash),
        ) if not v
    ]
    if missing:
        raise CFImagesNotConfigured(f"CF Images not configured (missing: {', '.join(missing)})")
    return token, account


def is_configured() -> bool:
    """True only when CF_API_TOKEN, CF_ACCOUNT_ID, AND CF_IMAGES_HASH are all set.

    CF_IMAGES_HASH is required because the rewriter needs it to construct
    `imagedelivery.net/<hash>/<id>/<variant>` URLs. Without it, the
    cf_image_id alone is useless to downstream consumers, so we flip the
    whole integration off rather than half-enable it.
    """
    try:
        _env()
        return True
    except CFImagesNotConfigured:
        return False


async def upload_bytes(
    body: bytes,
    *,
    filename: str,
    image_id: Optional[str] = None,
    content_type: str = "application/octet-stream",
    timeout: float = 60.0,
) -> Optional[str]:
    """Upload raw bytes to CF Images. Returns the cf_image_id on success.

    Returns None (no-op) when the integration is disabled — callers
    should treat that as success-without-cf-id and store NULL for
    `cf_image_id`.

    `image_id` is optional; if provided CF uses it as the canonical id.
    We pass the asset SHA-256 here so re-running capture is idempotent at
    the CF Images layer too.
    """
    if not is_configured():
        # Graceful no-op — caller continues with R2-only delivery.
        return None
    token, account = _env()
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/images/v1"
    files = {"file": (filename, body, content_type)}
    data = {}
    if image_id:
        data["id"] = image_id

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            files=files,
        )
        if resp.status_code >= 300:
            # 409 = id already exists; treat as success with the supplied id
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            errors = payload.get("errors") or []
            if any(e.get("code") == 5409 for e in errors) and image_id:
                logger.info(f"CF Images id={image_id} already exists; reusing")
                return image_id
            raise CFImagesError(
                f"CF Images upload failed {resp.status_code}: {resp.text[:300]}"
            )
        payload = resp.json()
        cf_id = (payload.get("result") or {}).get("id")
        if not cf_id:
            raise CFImagesError(f"CF Images response missing id: {payload}")
        return cf_id
