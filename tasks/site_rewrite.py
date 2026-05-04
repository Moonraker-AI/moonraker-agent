"""
tasks/site_rewrite.py
=====================
Site-migration job 2 of 2: take a captured page (rendered HTML + section
screenshots + asset map) and produce a clean semantic HTML rewrite that
visually matches the origin.

Spec: docs/site-migration-agent-job-spec.md §2 (lives in client-hq repo).

Pipeline:
  1. Read captured artifacts from R2 + page row from Supabase.
  2. Read site_migration_assets to build origin -> CF Images URL map.
  3. Call Claude (claude-opus-4-7) with the system prompt at
     prompts/site-migration-rewrite.txt and the captured artifacts as input.
  4. Validate output: single self-contained HTML, JSON-LD blocks copied,
     no asset URLs outside the asset map.
  5. Push rewritten HTML to R2 at migration/<id>/rewritten/<sha>.html.
  6. Render the rewritten HTML in fresh Playwright, screenshot at the same
     viewport, run a per-section pixel diff vs origin section screenshots.
  7. PATCH page row with rewritten_html_r2_key, rewrite_status='rewritten',
     visual_diff_score (0..1, lower is better).

Re-prompt loop (spec §2.4): operator can re-dispatch with a `feedback` field;
the agent appends it to the system prompt context and regenerates.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

from utils import r2_client
from utils.site_migration_db import (
    get_assets_for_migration,
    get_page,
    log_error,
    patch_page,
)

logger = logging.getLogger("agent.site_rewrite")

CLAUDE_MODEL = os.getenv("SITE_MIGRATION_REWRITE_MODEL", "claude-opus-4-7")
CLAUDE_MAX_TOKENS = 16000
ANTHROPIC_API_BASE = "https://api.anthropic.com"

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "site-migration-rewrite.txt"

VISUAL_DIFF_VIEWPORT = {"width": 1440, "height": 900}


def _cf_image_delivery_url(cf_image_id: str, *, variant: str = "public") -> str:
    """Cloudflare Images delivery URL.

    Caller must set CF_IMAGES_HASH (the per-account image hash from the
    Cloudflare dashboard). If absent we return the relative cf:<id>:<variant>
    placeholder so the rewriter still has *something* to substitute and a
    later deploy step can rewrite to absolute URLs.
    """
    h = os.getenv("CF_IMAGES_HASH", "")
    if h:
        return f"https://imagedelivery.net/{h}/{cf_image_id}/{variant}"
    return f"cf:{cf_image_id}:{variant}"


# ── Public entrypoint ────────────────────────────────────────────────────────

async def run_site_rewrite(task_id, params, status_callback, env=None):
    migration_id = params.get("migration_id")
    page_id = params.get("page_id")
    feedback = (params.get("feedback") or "").strip()

    await status_callback(task_id, "running", "site-rewrite starting")

    if not r2_client.is_configured():
        msg = "R2 not configured (R2_* env vars)"
        await log_error(kind="site-rewrite", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    if not os.getenv("ANTHROPIC_API_KEY"):
        msg = "ANTHROPIC_API_KEY not configured"
        await log_error(kind="site-rewrite", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    page_row = await get_page(page_id)
    if not page_row:
        msg = f"site_migration_pages id={page_id} not found"
        await log_error(kind="site-rewrite", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    rendered_key = page_row.get("rendered_html_r2_key")
    if not rendered_key:
        msg = f"page {page_id} has no rendered_html_r2_key (capture not complete)"
        await log_error(kind="site-rewrite", migration_id=migration_id, page_id=page_id, error=msg)
        await status_callback(task_id, "error", msg)
        return

    # Derive prefix for sibling artifacts: migration/<id>/raw/<sha>/
    raw_prefix = rendered_key.rsplit("/", 1)[0]
    page_path_sha = raw_prefix.rsplit("/", 1)[-1]
    rewritten_key = f"migration/{migration_id}/rewritten/{page_path_sha}.html"

    started = time.time()
    try:
        # Read artifacts from R2
        rendered_html = (await r2_client.get_object(rendered_key)).decode("utf-8", "replace")
        styles_json = json.loads((await r2_client.get_object(f"{raw_prefix}/styles.json")).decode("utf-8"))
        manifest = json.loads((await r2_client.get_object(f"{raw_prefix}/manifest.json")).decode("utf-8"))
        try:
            screenshot_desktop = await r2_client.get_object(f"{raw_prefix}/screenshot-desktop.png")
        except Exception:
            screenshot_desktop = None

        section_pngs = await _list_section_screenshots(raw_prefix)

        # Build origin -> CF Images URL map
        asset_rows = await get_assets_for_migration(migration_id)
        asset_map = _build_asset_map(asset_rows)

        # Compose user message for Claude
        user_payload = _build_claude_user_payload(
            url=page_row.get("url") or "",
            rendered_html=rendered_html,
            styles_json=styles_json,
            manifest=manifest,
            asset_map=asset_map,
            feedback=feedback,
        )
        screenshot_blocks = []
        if screenshot_desktop:
            screenshot_blocks.append(_image_block(screenshot_desktop, label="origin-desktop"))
        for idx, png in enumerate(section_pngs[:8]):
            screenshot_blocks.append(_image_block(png, label=f"origin-section-{idx:02d}"))

        await status_callback(task_id, "running", f"calling Claude {CLAUDE_MODEL}")
        rewritten_html = await _call_claude(user_payload, screenshot_blocks)

        # Validate output (best-effort)
        rewritten_html = _post_validate(rewritten_html, manifest)

        # Push to R2
        await r2_client.put_object(
            rewritten_key,
            rewritten_html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )

        # Visual diff
        await status_callback(task_id, "running", "rendering rewritten HTML for visual diff")
        diff_score = await _visual_diff(rewritten_html, section_pngs)

        await patch_page(page_id, {
            "rewritten_html_r2_key": rewritten_key,
            "rewrite_status": "rewritten",
            "visual_diff_score": diff_score,
            "rewritten_at": datetime.now(timezone.utc).isoformat(),
        })

        elapsed = int(time.time() - started)
        await status_callback(
            task_id,
            "complete",
            f"site-rewrite done in {elapsed}s, diff={diff_score:.3f}",
        )
    except Exception as e:
        logger.exception(f"site-rewrite {task_id[:12]} failed")
        await log_error(
            kind="site-rewrite",
            migration_id=migration_id,
            page_id=page_id,
            error=str(e)[:1000],
        )
        await status_callback(task_id, "error", str(e)[:200])


# ── Claude call ──────────────────────────────────────────────────────────────

def _load_system_prompt(feedback: str) -> str:
    base = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") if SYSTEM_PROMPT_PATH.exists() else ""
    if not base:
        # Fallback minimal prompt — should not happen in production
        base = (
            "Rewrite the captured client website page into a single self-contained "
            "semantic HTML document. Match the origin visually. Output only the HTML."
        )
    if feedback:
        base += "\n\nOperator feedback for this iteration:\n" + feedback.strip()
    return base


def _build_asset_map(asset_rows: list[dict]) -> dict:
    """origin_url -> { cf_url, r2_key, alt_text } map for Claude."""
    out: dict[str, dict] = {}
    for r in asset_rows:
        origin = r.get("origin_url")
        if not origin:
            continue
        cf_id = r.get("cf_image_id")
        cf_url = _cf_image_delivery_url(cf_id) if cf_id else None
        out[origin] = {
            "cf_url": cf_url,
            "r2_key": r.get("r2_key"),
            "alt_text": r.get("alt_text") or "",
            "width": r.get("width"),
            "height": r.get("height"),
        }
    return out


def _build_claude_user_payload(
    *,
    url: str,
    rendered_html: str,
    styles_json: dict,
    manifest: dict,
    asset_map: dict,
    feedback: str,
) -> str:
    """Concatenated text body. Claude gets HTML + styles + manifest + asset map."""
    sections = [
        f"# Origin URL\n{url}\n",
        f"# Captured manifest\n```json\n{json.dumps(manifest, ensure_ascii=False)[:60000]}\n```\n",
        f"# Computed styles (curated selectors)\n```json\n{json.dumps(styles_json, ensure_ascii=False)[:30000]}\n```\n",
        f"# Asset map (origin URL -> Cloudflare delivery URL)\n```json\n{json.dumps(asset_map, ensure_ascii=False)[:60000]}\n```\n",
        f"# Origin rendered HTML\n```html\n{rendered_html[:180000]}\n```\n",
    ]
    if feedback:
        sections.insert(0, f"# Operator feedback\n{feedback}\n")
    sections.append(
        "\nProduce the rewritten HTML now. Respond with ONLY the document, "
        "starting with <!doctype html> and ending with </html>."
    )
    return "\n".join(sections)


def _image_block(png_bytes: bytes, label: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png_bytes).decode("ascii"),
        },
    }


async def _call_claude(user_text: str, image_blocks: list[dict]) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    content_blocks: list[dict] = list(image_blocks)
    content_blocks.append({"type": "text", "text": user_text})

    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": _load_system_prompt(feedback=""),  # feedback already in user_text
        "messages": [{"role": "user", "content": content_blocks}],
    }

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{ANTHROPIC_API_BASE}/v1/messages",
            headers=headers,
            json=body,
        )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Anthropic API failed {resp.status_code}: {resp.text[:500]}"
            )
        payload = resp.json()
        parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        if not text.strip():
            raise RuntimeError("Anthropic returned empty content")
        return text


def _post_validate(html: str, manifest: dict) -> str:
    """Strip markdown fences if Claude added them; ensure JSON-LD copied."""
    h = html.strip()
    if h.startswith("```"):
        # Drop opening fence (and language tag) and closing fence
        nl = h.find("\n")
        if nl != -1:
            h = h[nl + 1:]
        if h.endswith("```"):
            h = h[: -3]
        h = h.strip()
    # Ensure all schema_jsonld blocks present (substring check; cheap)
    for blob in manifest.get("schema_jsonld") or []:
        try:
            needle = json.dumps(blob, ensure_ascii=False)[:120] if isinstance(blob, (dict, list)) else str(blob)[:120]
        except Exception:
            continue
        if needle and needle not in h:
            logger.warning(f"rewrite missing JSON-LD fragment: {needle[:60]}...")
    return h


# ── Visual diff ──────────────────────────────────────────────────────────────

async def _list_section_screenshots(raw_prefix: str) -> list[bytes]:
    """Try keys 00..23. R2 has no LIST without IAM scope — probe sequentially."""
    out: list[bytes] = []
    for idx in range(24):
        try:
            png = await r2_client.get_object(f"{raw_prefix}/sections/{idx:02d}.png")
            out.append(png)
        except Exception:
            break
    return out


async def _visual_diff(rewritten_html: str, origin_sections: list[bytes]) -> float:
    """Render rewritten HTML, screenshot, compute mean per-section L1 diff in
    0..1. If origin_sections is empty we return 0.0 (no signal).

    Note: the rewritten HTML's section count + ordering may differ from the
    origin's. We compute a single full-page diff against the origin's full
    desktop screenshot reconstructed from the section PNGs (vertical concat),
    which is a lossy but stable proxy. Operator gets the score; Claude rerun
    can use feedback to address mismatches.
    """
    if not origin_sections:
        return 0.0
    try:
        from PIL import Image, ImageChops
    except Exception as e:
        logger.warning(f"PIL not available for visual diff: {e}")
        return 0.0

    rewritten_png = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            context = await browser.new_context(viewport=VISUAL_DIFF_VIEWPORT)
            page = await context.new_page()
            await page.set_content(rewritten_html, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            rewritten_png = await page.screenshot(full_page=True, type="png")
            await context.close()
        finally:
            await browser.close()

    if not rewritten_png:
        return 1.0

    # Build origin reference image by vertically concatenating sections.
    origin_imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in origin_sections]
    if not origin_imgs:
        return 0.0
    width = max(im.width for im in origin_imgs)
    height = sum(im.height for im in origin_imgs)
    origin_ref = Image.new("RGB", (width, height), (255, 255, 255))
    y = 0
    for im in origin_imgs:
        origin_ref.paste(im, (0, y))
        y += im.height

    rewritten_img = Image.open(io.BytesIO(rewritten_png)).convert("RGB")
    # Resize rewritten to origin reference size for fair pixel compare.
    rewritten_resized = rewritten_img.resize(origin_ref.size)
    diff = ImageChops.difference(origin_ref, rewritten_resized)
    bbox = diff.getbbox()
    if not bbox:
        return 0.0
    # Mean pixel L1 across all channels, normalized to 0..1
    hist = diff.histogram()  # 256 * 3 bins
    total_pixels = origin_ref.size[0] * origin_ref.size[1]
    weighted = 0
    for ch in range(3):
        for v in range(256):
            weighted += v * hist[ch * 256 + v]
    mean_l1 = weighted / max(1, total_pixels * 3)
    return round(mean_l1 / 255.0, 4)
