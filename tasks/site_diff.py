"""
tasks/site_diff.py
==================
Site-migration job 4: visual diff between origin and staging.

After a successful site-rewrite-build-deploy cycle, render the staging
URL in Playwright at the same viewport as origin capture, compare to
origin's screenshot-{desktop,mobile}.png, and write back a single
0–100 score plus a side-by-side overlay PNG to R2.

Used by operator review (`/admin/migrations/[id]/diff`) and by the
auto-iteration loop (phase 3.3, not yet implemented).

Spec: docs/site-migration-agent-job-spec.md §4 (to be added).

Tier classification: Tier 2 (Light Browser) — acquires browser_lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import (
    TimeoutError as PWTimeoutError,
    async_playwright,
)

from utils import r2_client
from utils.site_migration_db import log_error, patch_page

logger = logging.getLogger("agent.site_diff")

NAV_TIMEOUT_MS = 30_000
NETWORK_IDLE_MS = 8_000
DEFAULT_UA = "MoonrakerSiteMigrator/1.0 (+https://moonraker.ai/migrator)"

# Standard capture viewports (must match site_capture defaults so diffs are apples-to-apples)
VIEWPORTS = [
    {"name": "desktop", "width": 1440, "height": 900},
    {"name": "mobile",  "width": 375,  "height": 812},
]


def _r2_worker_base() -> Optional[str]:
    base = os.getenv("R2_INGEST_URL", "").rstrip("/")
    return base or None


def _staging_url_for(migration_id: str, page_path: str) -> Optional[str]:
    """Construct the public staging URL for a given page path under
    the moonraker-r2-worker /serve route. Returns None if the worker
    base URL isn't configured."""
    base = _r2_worker_base()
    if not base:
        return None
    # Astro `format: 'directory'` builds index.html for the homepage at
    # dist/index.html. For non-root paths, dist/<path>/index.html.
    p = page_path.strip()
    if not p or p == "/":
        leaf = "index.html"
    else:
        leaf = f"{p.strip('/')}/index.html"
    return f"{base}/serve/migration/{migration_id}/dist/{leaf}"


async def _capture_staging(
    page_url: str,
    width: int,
    height: int,
    timeout_ms: int = NAV_TIMEOUT_MS,
) -> Optional[bytes]:
    """Render the staging URL and return a full-page PNG."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=DEFAULT_UA,
                java_script_enabled=True,
                locale="en-US",
            )
            page = await context.new_page()
            page.set_default_navigation_timeout(timeout_ms)
            await page.set_viewport_size({"width": width, "height": height})
            try:
                await page.goto(page_url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
                except PWTimeoutError:
                    await page.wait_for_timeout(2000)
                shot = await page.screenshot(full_page=True, type="png")
                return shot
            finally:
                await page.close()
                await context.close()
        finally:
            await browser.close()


def _diff_images(
    origin_png: bytes,
    staging_png: bytes,
) -> Tuple[float, bytes]:
    """Return (score 0..100, overlay PNG bytes).

    Score: mean per-pixel L2 distance over RGB channels, normalized to
    a 0–100 scale where 0 = identical and 100 = inverse of every pixel.
    Both inputs are resized to a common reference size (the smaller of
    the two on each dimension) before comparison.

    Overlay: side-by-side composite [origin | staging | diff-heatmap].
    """
    from PIL import Image, ImageChops, ImageOps

    origin = Image.open(io.BytesIO(origin_png)).convert("RGB")
    staging = Image.open(io.BytesIO(staging_png)).convert("RGB")

    # Normalize to a common width — preserve aspect ratio. If pages are
    # very different in height that's already a strong signal of mismatch.
    target_w = min(origin.size[0], staging.size[0])
    if origin.size[0] != target_w:
        origin = origin.resize(
            (target_w, int(origin.size[1] * target_w / origin.size[0])),
            Image.LANCZOS,
        )
    if staging.size[0] != target_w:
        staging = staging.resize(
            (target_w, int(staging.size[1] * target_w / staging.size[0])),
            Image.LANCZOS,
        )

    # Crop to common height for diff math
    common_h = min(origin.size[1], staging.size[1])
    o_crop = origin.crop((0, 0, target_w, common_h))
    s_crop = staging.crop((0, 0, target_w, common_h))

    diff = ImageChops.difference(o_crop, s_crop)

    # Mean per-pixel difference across RGB channels.
    pixels = diff.getdata()
    n = max(1, len(pixels))
    total = 0
    for px in pixels:
        # px is (r, g, b)
        total += px[0] + px[1] + px[2]
    mean_per_channel = total / (n * 3.0)
    score = round((mean_per_channel / 255.0) * 100.0, 2)

    # Penalty for height mismatch — capped at +20.
    h_origin = origin.size[1]
    h_staging = staging.size[1]
    h_max = max(h_origin, h_staging)
    h_diff_pct = abs(h_origin - h_staging) / h_max if h_max else 0
    score = min(100.0, round(score + (h_diff_pct * 20.0), 2))

    # Build heatmap of diff (boost contrast).
    heatmap = ImageOps.autocontrast(diff.convert("L"), cutoff=2)
    heatmap_rgb = Image.merge(
        "RGB", (heatmap, Image.new("L", heatmap.size, 0), Image.new("L", heatmap.size, 0))
    )

    # Composite [origin | staging | heatmap]
    panel_w = target_w
    panel_h = max(origin.size[1], staging.size[1], heatmap_rgb.size[1])
    overlay = Image.new("RGB", (panel_w * 3, panel_h), (255, 255, 255))
    overlay.paste(origin, (0, 0))
    overlay.paste(staging, (panel_w, 0))
    overlay.paste(heatmap_rgb, (panel_w * 2, 0))

    buf = io.BytesIO()
    overlay.save(buf, format="PNG", optimize=True)
    return score, buf.getvalue()


# ── Public entrypoint ────────────────────────────────────────────────────────

async def run_site_diff(task_id, params, status_callback, env=None):
    """Top-level driver invoked by server._run_site_diff_with_lock."""
    migration_id = params.get("migration_id") or ""
    page_id = params.get("page_id") or ""
    if not migration_id or not page_id:
        msg = "missing migration_id or page_id"
        logger.error(f"site-diff {task_id[:12]} aborted: {msg}")
        await log_error(kind="site-diff", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    # Pre-flight
    if not r2_client.is_configured():
        msg = "R2 not configured"
        logger.error(f"site-diff {task_id[:12]} aborted: {msg}")
        await log_error(kind="site-diff", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    await status_callback(task_id, "running", "site-diff starting")

    # Load page row to get URL path
    try:
        from utils.site_migration_db import get_page
        page_row = await get_page(page_id)
    except Exception as e:
        msg = f"failed to load page row: {e}"
        await log_error(kind="site-diff", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return
    if not page_row:
        msg = f"page row not found: {page_id}"
        await log_error(kind="site-diff", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    page_path = page_row.get("path") or "/"
    origin_url = page_row.get("url") or ""

    # Build staging URL for this page
    staging_url = _staging_url_for(migration_id, page_path)
    if not staging_url:
        msg = "R2_INGEST_URL not set; cannot build staging URL"
        await log_error(kind="site-diff", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    # Origin screenshot key (matches site_capture's hash scheme)
    page_path_sha = hashlib.sha256(page_path.encode("utf-8")).hexdigest()
    raw_prefix = f"migration/{migration_id}/raw/{page_path_sha}"

    scores: dict[str, float] = {}
    diff_root_key_prefix = f"migration/{migration_id}/diff/{page_id}"

    for vp in VIEWPORTS:
        vp_name = vp["name"]
        await status_callback(task_id, "running", f"diffing {vp_name}")

        # 1. Pull origin screenshot
        try:
            origin_png = await r2_client.get_object(f"{raw_prefix}/screenshot-{vp_name}.png")
        except Exception as e:
            logger.warning(f"site-diff {task_id[:12]} origin {vp_name} fetch failed: {e}")
            continue

        # 2. Render staging at same viewport
        try:
            staging_png = await _capture_staging(staging_url, vp["width"], vp["height"])
        except Exception as e:
            logger.warning(f"site-diff {task_id[:12]} staging {vp_name} capture failed: {e}")
            continue
        if not staging_png:
            continue

        # 3. Compute score + overlay
        try:
            score, overlay_png = await asyncio.to_thread(_diff_images, origin_png, staging_png)
        except Exception as e:
            logger.warning(f"site-diff {task_id[:12]} {vp_name} diff math failed: {e}")
            continue

        scores[vp_name] = score

        # 4. Push overlay + raw staging shot to R2
        try:
            await r2_client.put_object(
                f"{diff_root_key_prefix}/overlay-{vp_name}.png",
                overlay_png,
                content_type="image/png",
            )
            await r2_client.put_object(
                f"{diff_root_key_prefix}/staging-{vp_name}.png",
                staging_png,
                content_type="image/png",
            )
        except Exception as e:
            logger.warning(f"site-diff {task_id[:12]} R2 push failed ({vp_name}): {e}")

    if not scores:
        msg = "no viewports diffed (origin missing or staging unreachable)"
        await log_error(kind="site-diff", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    # Composite score = average of viewport scores
    composite = round(sum(scores.values()) / len(scores), 2)

    await patch_page(page_id, {
        "visual_diff_score": composite,
    })

    logger.info(
        f"site-diff {task_id[:12]} done: composite={composite} "
        f"per_vp={scores} staging_url={staging_url} origin_url={origin_url}"
    )
    await status_callback(
        task_id,
        "complete",
        f"site-diff complete score={composite} ({', '.join(f'{k}={v}' for k,v in scores.items())})",
    )
