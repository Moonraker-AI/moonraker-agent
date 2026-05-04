"""
utils/asset_urls.py
===================
Single source of truth for "given an asset row, what public URL do we
emit for it?". Used by `tasks/site_rewrite.py` to build the asset map
that ships with the Claude prompt and gets validated against
ImageBlock src values.

Decision tree:

  1. If the row has a `cf_image_id` AND CF_IMAGES_HASH is set in env,
     emit the Cloudflare Images delivery URL:
       https://imagedelivery.net/<hash>/<cf_image_id>/<variant>

  2. Otherwise, emit the R2 Worker public URL for the asset's r2_key:
       <R2_INGEST_URL>/serve/<r2_key>

The R2 Worker only serves `dist/`, `staging/`, and `assets/` prefixes
publicly; site-migration asset rows always live under
`migration/<id>/assets/...`, which the Worker accepts.

The helper is deterministic: every call with the same row + env returns
the same URL. No caching, no I/O, no side effects.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from utils import r2_client

logger = logging.getLogger("agent.asset_urls")


CF_IMAGES_DELIVERY_BASE = "https://imagedelivery.net"


def cf_image_delivery_url(cf_image_id: str, *, variant: str = "public") -> Optional[str]:
    """Build the Cloudflare Images delivery URL for a given image id.

    Returns None if `CF_IMAGES_HASH` is not set in the environment, since
    we cannot construct a valid delivery URL without it.
    """
    h = (os.getenv("CF_IMAGES_HASH") or "").strip()
    if not h or not cf_image_id:
        return None
    return f"{CF_IMAGES_DELIVERY_BASE}/{h}/{cf_image_id}/{variant}"


def public_asset_url(asset_row: dict) -> Optional[str]:
    """Return the public URL for a `site_migration_assets` row.

    Branch 1 — CF Images:
        When `asset_row['cf_image_id']` is truthy AND `CF_IMAGES_HASH`
        is set in env, returns the imagedelivery.net URL.

    Branch 2 — R2 Worker (default):
        Falls back to the R2 Worker's `/serve/<r2_key>` URL via
        `r2_client.public_url(...)`. This is the path used since the
        Cloudflare account was found to be on the free Images tier
        (which does not include hosted images).

    Returns None if neither branch can produce a URL — i.e., the row
    has neither a usable cf_image_id (with hash) nor an r2_key. Callers
    should treat that as a missing asset and omit it from the rewrite.

    The function reads env at call time so a test harness can inject
    different states without re-importing.
    """
    if not isinstance(asset_row, dict):
        return None

    cf_id = asset_row.get("cf_image_id")
    if cf_id:
        cf_url = cf_image_delivery_url(cf_id)
        if cf_url:
            return cf_url
        # cf_image_id present but no hash -> fall through to R2 branch.

    r2_key = asset_row.get("r2_key")
    if not r2_key:
        return None

    try:
        return r2_client.public_url(r2_key)
    except Exception as e:
        # R2_INGEST_URL not set, or key shape invalid. Log + return None
        # so the caller can decide whether to skip the asset.
        logger.warning(
            "public_asset_url: r2_client.public_url(%r) failed: %s", r2_key, e
        )
        return None
