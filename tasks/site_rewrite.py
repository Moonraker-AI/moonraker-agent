"""
tasks/site_rewrite.py
=====================
Site-migration job 2 of 2 (v4 — Astro output).

Spec: docs/site-migration-agent-job-spec.md §2 (Astro output)
      docs/site-migration-spec.md §8 + §11A (Astro template architecture)

Pipeline:
  1. Ensure per-migration working tree at /tmp/build/<migration_id>/.
     Clone moonraker-site-template (depth=1) on first call.
  2. Read captured artifacts from R2 (rendered.html, styles.json,
     manifest.json, screenshot-desktop.png, sections/*.png) and the page
     row from Supabase. Read site_migration_assets for the asset map.
  3. On first rewrite for the migration (or force_tokens=true), generate
     src/styles/tokens.override.css from styles.json (spec §2.5) and push
     a copy to R2 at migration/<id>/tokens.override.css.
  4. Call Claude with the system prompt at prompts/site-migration-rewrite.txt
     and the captured screenshots as multimodal content.
  5. Validate output: must be a valid Astro page (frontmatter, Site import,
     <Site> wrapper, asset-map-only image refs). Retry once with feedback.
  6. Map page.path -> src/pages/<route>.astro on disk. Write the file.
     Push a copy to R2 at migration/<id>/src/<route>.astro.
  7. If build_after=true (default): run §2.6 build + §2.7 deploy. Patch
     site_migrations.last_built_at + last_deployed_at.
  8. Patch the page row with rewritten_html_r2_key (the .astro key),
     rewrite_status='rewritten' or 'published', visual_diff_score.

Re-prompt loop (spec §2.4): operator can re-dispatch with a `feedback`
field; the agent appends it to the system prompt context and regenerates
just that page, then rebuilds.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from utils import r2_client
from utils.asset_urls import public_asset_url
from utils.astro_tokens import derive_overrides, render_override_css
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

TEMPLATE_REPO_URL = os.getenv(
    "TEMPLATE_REPO_URL",
    os.getenv(
        "SITE_TEMPLATE_REPO_URL",
        "https://github.com/Moonraker-AI/moonraker-site-template.git",
    ),
)


_PAT_RE = re.compile(r'(x-access-token|oauth2|[\w.-]+):[^@\s]+@')


def _redact_pat(s: str) -> str:
    """Strip embedded GitHub PATs from URLs before logging.

    site_rewrite uses a https://x-access-token:<PAT>@github.com/... URL
    for cloning the private template repo. Subprocess error output and
    Python exceptions surface that URL verbatim, which leaks the PAT to
    logs / Supabase error_log / operator transcripts. Run all log
    strings through this before they leave the process.
    """
    if not s:
        return s
    return _PAT_RE.sub(r'\1:REDACTED@', s)
WORK_TREE_BASE = Path(os.getenv("SITE_BUILD_BASE", "/tmp/build"))

NPM_INSTALL_TIMEOUT_S = 120
ASTRO_BUILD_TIMEOUT_S = 90

# R2 cache-control by file extension (spec §2.7)
CACHE_CONTROL_HTML = "public, max-age=300, s-maxage=86400, stale-while-revalidate=604800"
CACHE_CONTROL_IMMUTABLE = "public, max-age=31536000, immutable"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".xml": "application/xml",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".webmanifest": "application/manifest+json",
    ".pdf": "application/pdf",
}


# ── Public entrypoint ────────────────────────────────────────────────────────

async def run_site_rewrite(task_id, params, status_callback, env=None):
    migration_id = params.get("migration_id")
    page_id = params.get("page_id")
    feedback = (params.get("feedback") or "").strip()
    build_after = bool(params.get("build_after", True))
    deploy_after = bool(params.get("deploy_after", True))
    force_tokens = bool(params.get("force_tokens", False))

    await status_callback(task_id, "running", "site-rewrite starting")

    if not r2_client.is_configured():
        msg = "R2 not configured (R2_INGEST_URL / R2_INGEST_SECRET / R2_MIGRATION_BUCKET)"
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

    # Working tree: /tmp/build/<migration_id>/
    work_tree = WORK_TREE_BASE / str(migration_id)

    # Derive prefix for sibling artifacts: migration/<id>/raw/<sha>/
    raw_prefix = rendered_key.rsplit("/", 1)[0]
    page_path = page_row.get("path") or "/"
    route = _path_to_astro_route(page_path)
    rewritten_r2_key = f"migration/{migration_id}/src/{route}.astro"

    started = time.time()
    try:
        # 1. Ensure working tree
        await status_callback(task_id, "running", f"preparing working tree {work_tree}")
        _ensure_work_tree(work_tree)

        # 2. Read captured artifacts from R2
        await status_callback(task_id, "running", "reading captured artifacts from R2")
        rendered_html = (await r2_client.get_object(rendered_key)).decode("utf-8", "replace")
        styles_json = json.loads(
            (await r2_client.get_object(f"{raw_prefix}/styles.json")).decode("utf-8")
        )
        manifest = json.loads(
            (await r2_client.get_object(f"{raw_prefix}/manifest.json")).decode("utf-8")
        )
        try:
            screenshot_desktop = await r2_client.get_object(f"{raw_prefix}/screenshot-desktop.png")
        except Exception:
            screenshot_desktop = None
        try:
            screenshot_mobile = await r2_client.get_object(f"{raw_prefix}/screenshot-mobile.png")
        except Exception:
            screenshot_mobile = None

        section_pngs = await _list_section_screenshots(raw_prefix)

        # 3. Asset map
        asset_rows = await get_assets_for_migration(migration_id)
        asset_map = _build_asset_map(asset_rows)

        # 4. Token override (one-shot per migration unless forced)
        tokens_path = work_tree / "src" / "styles" / "tokens.override.css"
        tokens_marker = work_tree / ".tokens-applied"
        if force_tokens or not tokens_marker.exists():
            await status_callback(task_id, "running", "generating tokens.override.css")
            overrides = derive_overrides(styles_json)
            css = render_override_css(overrides)
            tokens_path.parent.mkdir(parents=True, exist_ok=True)
            tokens_path.write_text(css, encoding="utf-8")
            tokens_marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
            try:
                await r2_client.put_object(
                    f"migration/{migration_id}/tokens.override.css",
                    css.encode("utf-8"),
                    content_type="text/css; charset=utf-8",
                )
            except Exception as e:
                logger.warning(f"tokens.override.css R2 push failed: {e}")

        # 5. Call Claude (with one retry on validation failure)
        astro_text, validation_error = await _generate_and_validate(
            status_callback=status_callback,
            task_id=task_id,
            page_url=page_row.get("url") or "",
            rendered_html=rendered_html,
            styles_json=styles_json,
            manifest=manifest,
            asset_map=asset_map,
            screenshot_desktop=screenshot_desktop,
            screenshot_mobile=screenshot_mobile,
            section_pngs=section_pngs,
            feedback=feedback,
        )

        if validation_error and not astro_text:
            await log_error(
                kind="site-rewrite",
                migration_id=migration_id,
                page_id=page_id,
                error=f"validation failed: {validation_error}",
            )
            await patch_page(page_id, {"rewrite_status": "pending"})
            await status_callback(task_id, "error", f"rewrite validation failed: {validation_error}")
            return

        # 6. Write Astro page to working tree + R2
        out_path = work_tree / "src" / "pages" / f"{route}.astro"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(astro_text, encoding="utf-8")
        try:
            await r2_client.put_object(
                rewritten_r2_key,
                astro_text.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
        except Exception as e:
            logger.warning(f"R2 push of rewritten .astro failed for {rewritten_r2_key}: {e}")

        # 7. Optionally build + deploy
        rewrite_status = "rewritten"
        diff_score: Optional[float] = None
        if build_after:
            build_ok = await _run_build(work_tree, migration_id, status_callback, task_id)
            if build_ok and deploy_after:
                deployed = await _deploy_dist(
                    work_tree=work_tree,
                    migration_id=migration_id,
                    target_prefix=f"migration/{migration_id}/dist/",
                    status_callback=status_callback,
                    task_id=task_id,
                )
                if deployed:
                    rewrite_status = "published"
                    await _patch_migration(migration_id, {
                        "last_built_at": _now(),
                        "last_deployed_at": _now(),
                    })
                else:
                    await _patch_migration(migration_id, {"last_built_at": _now()})
            elif build_ok:
                await _patch_migration(migration_id, {"last_built_at": _now()})

        # 8. Patch the page row
        page_patch: dict = {
            "rewritten_html_r2_key": rewritten_r2_key,
            "rewrite_status": rewrite_status,
        }
        if diff_score is not None:
            page_patch["visual_diff_score"] = diff_score
        await patch_page(page_id, page_patch)

        elapsed = int(time.time() - started)
        await status_callback(
            task_id,
            "complete",
            f"site-rewrite done in {elapsed}s, status={rewrite_status}",
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


# ── Working tree management ─────────────────────────────────────────────────

def _ensure_work_tree(work_tree: Path) -> None:
    """Ensure /tmp/build/<id>/ exists and is a clone of the template repo.

    Idempotent: if the marker exists we reuse the tree (preserves
    operator hand-edits and node_modules).
    """
    marker = work_tree / ".cloned"
    if marker.exists():
        return

    work_tree.parent.mkdir(parents=True, exist_ok=True)
    if work_tree.exists():
        # Stale partial clone — nuke and retry.
        shutil.rmtree(work_tree, ignore_errors=True)

    cmd = ["git", "clone", "--depth=1", TEMPLATE_REPO_URL, str(work_tree)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        safe_url = _redact_pat(TEMPLATE_REPO_URL)
        safe_err = _redact_pat((proc.stderr or proc.stdout)[:500])
        raise RuntimeError(
            f"git clone {safe_url} failed (rc={proc.returncode}): {safe_err}"
        )
    marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def _path_to_astro_route(page_path: str) -> str:
    """Map a page row's `path` to a route on disk under src/pages/.

    /              -> index
    /about         -> about
    /blog/foo      -> blog/foo
    Trailing slash collapses to the bare segment.
    """
    p = (page_path or "/").strip()
    if not p.startswith("/"):
        p = "/" + p
    if p in ("/", ""):
        return "index"
    p = p.rstrip("/")
    p = p.lstrip("/")
    # Sanitize unsafe path components — refuse `..`, absolute paths.
    parts = []
    for seg in p.split("/"):
        if not seg or seg in (".", ".."):
            continue
        seg = re.sub(r"[^A-Za-z0-9_\-]", "-", seg)
        parts.append(seg)
    return "/".join(parts) if parts else "index"


# ── Asset map ────────────────────────────────────────────────────────────────

def _build_asset_map(asset_rows: list[dict]) -> dict:
    """Build origin -> public-URL map for the rewriter.

    Each entry's `url` is the canonical public URL for the asset
    (resolved via `public_asset_url`). When CF Images is enabled this
    is an `imagedelivery.net` URL; otherwise it is the R2 Worker's
    `/serve/<r2_key>` URL. Either way, the rewriter just uses what's
    in `url` — Claude does not have to choose between providers.

    `cf_url` is preserved for backwards compatibility with prompt
    versions that referenced it; it mirrors `url` when CF Images is
    active and is None otherwise.
    """
    out: dict[str, dict] = {}
    for r in asset_rows:
        origin = r.get("origin_url")
        if not origin:
            continue
        url = public_asset_url(r)
        if not url:
            # No usable URL (no cf_image_id with hash, no r2_key) — skip
            # so Claude doesn't see a half-populated entry.
            continue
        cf_id = r.get("cf_image_id")
        # cf_url stays populated only when CF Images actually delivers the
        # final URL — i.e., url itself is on imagedelivery.net.
        cf_url = url if cf_id and url.startswith("https://imagedelivery.net/") else None
        out[origin] = {
            "url": url,
            "cf_url": cf_url,
            "r2_key": r.get("r2_key"),
            "alt_text": r.get("alt_text") or "",
            "width": r.get("width"),
            "height": r.get("height"),
        }
    return out


# ── Claude call + validation ────────────────────────────────────────────────

def _load_system_prompt(feedback: str) -> str:
    base = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") if SYSTEM_PROMPT_PATH.exists() else ""
    if not base:
        base = (
            "Rewrite the captured page as an Astro page component using the "
            "shared Site.astro layout. Output only the .astro file contents."
        )
    if feedback:
        base += "\n\nOperator feedback for this iteration:\n" + feedback.strip()
    return base


def _build_claude_user_payload(
    *,
    url: str,
    rendered_html: str,
    styles_json: dict,
    manifest: dict,
    asset_map: dict,
    feedback: str,
    extra_validation_feedback: str = "",
) -> str:
    sections = [
        f"# Origin URL\n{url}\n",
        f"# Captured manifest\n```json\n{json.dumps(manifest, ensure_ascii=False)[:60000]}\n```\n",
        f"# Computed styles (curated selectors)\n```json\n{json.dumps(styles_json, ensure_ascii=False)[:30000]}\n```\n",
        f"# Asset map (origin URL -> Cloudflare delivery URL + dims + alt)\n```json\n{json.dumps(asset_map, ensure_ascii=False)[:60000]}\n```\n",
        f"# Origin rendered HTML\n```html\n{rendered_html[:180000]}\n```\n",
    ]
    if feedback:
        sections.insert(0, f"# Operator feedback\n{feedback}\n")
    if extra_validation_feedback:
        sections.append(
            "# Validation feedback from previous attempt\n"
            f"{extra_validation_feedback}\n"
            "Regenerate the file. Fix all issues listed above. Output ONLY the .astro file.\n"
        )
    else:
        sections.append(
            "\nProduce the rewritten Astro page now. Respond with ONLY the .astro "
            "file contents, starting with the `---` frontmatter delimiter."
        )
    return "\n".join(sections)


# Anthropic vision API rejects single images larger than 5 MB raw bytes
# (server returns 400 invalid_request_error). Full-page Squarespace
# captures routinely exceed 7 MB. Cap at 4 MB to leave headroom for the
# 33% base64 expansion + multipart envelope. Anthropic also recommends
# ≤1568 px on the long edge for best vision performance.
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_IMAGE_LONG_EDGE = 1568


def _compress_for_anthropic(png_bytes: bytes) -> tuple[bytes, str]:
    """Return (bytes, media_type) suitable for the Anthropic vision API.

    Pass-through if the input is already under the size cap. Otherwise
    resize the long edge to <=1568 px and re-encode as JPEG quality 85,
    which is what Anthropic's documentation recommends for full-page
    screenshots.
    """
    if len(png_bytes) <= _MAX_IMAGE_BYTES:
        return png_bytes, "image/png"
    try:
        from PIL import Image
    except ImportError:
        # Pillow ships transitively via qrcode[pil]; if missing, surface
        # raw bytes and let Anthropic 400 the request — caller handles it.
        return png_bytes, "image/png"

    import io
    img = Image.open(io.BytesIO(png_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > _MAX_IMAGE_LONG_EDGE:
        scale = _MAX_IMAGE_LONG_EDGE / float(long_edge)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue(), "image/jpeg"


def _image_block(png_bytes: bytes, label: str) -> dict:
    data, media_type = _compress_for_anthropic(png_bytes)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


async def _call_claude(user_text: str, image_blocks: list[dict], feedback: str) -> str:
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
        "system": _load_system_prompt(feedback=feedback),
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


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
    return t


def _validate_astro(text: str, asset_map: dict) -> Optional[str]:
    """Return None if valid, else a feedback string describing what's wrong."""
    if not text or not text.strip():
        return "Output was empty."

    if not text.lstrip().startswith("---"):
        return "Output must start with `---` (Astro frontmatter delimiter)."

    # Locate frontmatter block
    rest = text.lstrip()
    body_after_open = rest[3:]
    fm_close = body_after_open.find("\n---")
    if fm_close == -1:
        return "Frontmatter block is not closed with a `---` line."
    frontmatter = body_after_open[:fm_close]
    body = body_after_open[fm_close + 4:]

    if "import Site from '../layouts/Site.astro'" not in frontmatter \
       and 'import Site from "../layouts/Site.astro"' not in frontmatter:
        return (
            "Frontmatter must contain `import Site from '../layouts/Site.astro';` "
            "exactly."
        )

    # Body must wrap content in <Site ...>...</Site>
    if "<Site" not in body or "</Site>" not in body:
        return "Body must wrap content in `<Site meta={meta} schema={schema}>...</Site>`."

    # No raw <html>/<body>/<head>
    forbidden = ["<html", "<head>", "<body", "<!doctype", "<!DOCTYPE"]
    for tag in forbidden:
        if tag in body or tag in frontmatter:
            return f"Output must not contain `{tag}` — the layout owns the document shell."

    # No inline <script>
    if re.search(r"<script\b", body, re.IGNORECASE):
        return "Output must not contain `<script>` tags."

    # Must declare meta and schema
    if "const meta" not in frontmatter:
        return "Frontmatter must define `const meta = { ... }`."
    if "const schema" not in frontmatter:
        return "Frontmatter must define `const schema = [ ... ]`."

    # Body must reference at least one component import OR semantic HTML.
    component_imports = re.findall(
        r"import\s+(\w+)\s+from\s+['\"]\.\./components/(\w+)\.astro['\"]",
        frontmatter,
    )
    used_any_component = any(
        re.search(rf"<{name}\b", body) for name, _ in component_imports
    )
    has_semantic = bool(re.search(r"<(section|article|header|main|aside)\b", body))
    if not used_any_component and not has_semantic:
        return (
            "Body must use at least one shared component (Hero, Section, "
            "TwoColumn, Button, ImageBlock, BulletList) or semantic HTML "
            "tag (<section>, <article>, <header>, <main>, <aside>)."
        )

    # ImageBlock src values must come from the asset map (best-effort —
    # only enforce when ImageBlock is actually used). Accept either the
    # canonical `url` (R2 Worker /serve/...) or the CF Images `cf_url`
    # when present, since both are valid public URLs for the same asset.
    if "<ImageBlock" in body and asset_map:
        allowed_srcs: set[str] = set()
        for v in asset_map.values():
            for k in ("url", "cf_url"):
                u = v.get(k)
                if u:
                    allowed_srcs.add(u)
        for m in re.finditer(r'<ImageBlock\s+[^>]*src=["\']([^"\']+)["\']', body):
            src = m.group(1)
            if allowed_srcs and src not in allowed_srcs:
                return (
                    f"ImageBlock src `{src}` is not in the asset map. Use only "
                    "URLs listed under `url` (or `cf_url` when present) in the "
                    "asset map."
                )

    return None


async def _generate_and_validate(
    *,
    status_callback,
    task_id: str,
    page_url: str,
    rendered_html: str,
    styles_json: dict,
    manifest: dict,
    asset_map: dict,
    screenshot_desktop: Optional[bytes],
    screenshot_mobile: Optional[bytes],
    section_pngs: list[bytes],
    feedback: str,
) -> tuple[Optional[str], Optional[str]]:
    """Returns (astro_text, error). astro_text is None on terminal failure."""
    extra_feedback = ""
    last_error: Optional[str] = None

    for attempt in (1, 2):
        await status_callback(
            task_id,
            "running",
            f"calling Claude {CLAUDE_MODEL} (attempt {attempt})",
        )

        user_payload = _build_claude_user_payload(
            url=page_url,
            rendered_html=rendered_html,
            styles_json=styles_json,
            manifest=manifest,
            asset_map=asset_map,
            feedback=feedback,
            extra_validation_feedback=extra_feedback,
        )
        screenshot_blocks = []
        if screenshot_desktop:
            screenshot_blocks.append(_image_block(screenshot_desktop, "origin-desktop"))
        if screenshot_mobile:
            screenshot_blocks.append(_image_block(screenshot_mobile, "origin-mobile"))
        for idx, png in enumerate(section_pngs[:8]):
            screenshot_blocks.append(_image_block(png, f"origin-section-{idx:02d}"))

        try:
            raw = await _call_claude(user_payload, screenshot_blocks, feedback=feedback)
        except Exception as e:
            return None, f"Claude call failed: {e}"

        astro_text = _strip_fences(raw)
        err = _validate_astro(astro_text, asset_map)
        if err is None:
            return astro_text, None
        logger.warning(f"site-rewrite validation attempt {attempt} failed: {err}")
        last_error = err
        extra_feedback = err

    return None, last_error


# ── Build + deploy ──────────────────────────────────────────────────────────

async def _run_build(work_tree: Path, migration_id: str, status_callback, task_id: str) -> bool:
    installed_marker = work_tree / "node_modules" / ".installed"
    if not installed_marker.exists():
        await status_callback(task_id, "running", "npm install --omit=dev")
        rc, log = await _run_subprocess(
            ["npm", "install", "--omit=dev"],
            cwd=work_tree,
            timeout=NPM_INSTALL_TIMEOUT_S,
        )
        if rc != 0:
            await log_error(
                kind="site-rewrite-build",
                migration_id=migration_id,
                page_id=None,
                error=f"npm install failed rc={rc}: {log[-1500:]}",
            )
            await status_callback(task_id, "error", f"npm install failed rc={rc}")
            return False
        installed_marker.parent.mkdir(parents=True, exist_ok=True)
        installed_marker.write_text(_now(), encoding="utf-8")

    public_url = os.getenv("PUBLIC_SITE_URL", "")
    env = dict(os.environ)
    if public_url:
        env["PUBLIC_SITE_URL"] = public_url

    # Staging dist is served from the Worker under /serve/migration/<id>/dist,
    # so Astro must emit asset hrefs prefixed with that path. Production
    # cutover sets PUBLIC_BASE_PATH=/ explicitly via deploy_to=production.
    env.setdefault(
        "PUBLIC_BASE_PATH",
        f"/serve/migration/{migration_id}/dist",
    )

    await status_callback(task_id, "running", "astro build")
    rc, log = await _run_subprocess(
        ["npx", "astro", "build"],
        cwd=work_tree,
        timeout=ASTRO_BUILD_TIMEOUT_S,
        env=env,
    )
    if rc != 0:
        await log_error(
            kind="site-rewrite-build",
            migration_id=migration_id,
            page_id=None,
            error=f"astro build failed rc={rc}: {log[-1500:]}",
        )
        await status_callback(task_id, "error", f"astro build failed rc={rc}")
        return False

    return True


async def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: Optional[dict] = None,
) -> tuple[int, str]:
    """Run a subprocess, capture combined output, surface clear timeout error."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, f"TIMEOUT after {timeout}s running {' '.join(cmd)}"
    return proc.returncode or 0, stdout.decode("utf-8", "replace")


def _content_type_for(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _cache_control_for(path: Path) -> str:
    if path.suffix.lower() == ".html":
        return CACHE_CONTROL_HTML
    return CACHE_CONTROL_IMMUTABLE


async def _deploy_dist(
    *,
    work_tree: Path,
    migration_id: str,
    target_prefix: str,
    status_callback,
    task_id: str,
) -> bool:
    dist = work_tree / "dist"
    if not dist.is_dir():
        await log_error(
            kind="site-rewrite-deploy",
            migration_id=migration_id,
            page_id=None,
            error=f"dist/ not found at {dist}",
        )
        return False

    if not target_prefix.endswith("/"):
        target_prefix += "/"

    files = [p for p in dist.rglob("*") if p.is_file()]
    await status_callback(task_id, "running", f"deploying {len(files)} files to {target_prefix}")

    uploaded = 0
    for fp in files:
        rel = fp.relative_to(dist).as_posix()
        key = f"{target_prefix}{rel}"
        try:
            body = fp.read_bytes()
            await r2_client.put_object(
                key,
                body,
                content_type=_content_type_for(fp),
                cache_control=_cache_control_for(fp),
            )
            uploaded += 1
        except Exception as e:
            logger.warning(f"R2 PUT {key} failed: {e}")

    if uploaded == 0:
        return False
    await status_callback(task_id, "running", f"deployed {uploaded}/{len(files)} files")
    return True


# ── Section screenshot fetcher ───────────────────────────────────────────────

async def _list_section_screenshots(raw_prefix: str) -> list[bytes]:
    out: list[bytes] = []
    for idx in range(24):
        try:
            png = await r2_client.get_object(f"{raw_prefix}/sections/{idx:02d}.png")
            out.append(png)
        except Exception:
            break
    return out


# ── site_migrations row patcher (best-effort REST PATCH) ────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _patch_migration(migration_id: str, payload: dict) -> bool:
    """PATCH site_migrations by id. Best-effort; logs and returns False on
    failure. Tolerant of missing columns (PostgREST 400 on unknown column —
    spec §2 + §11A added last_built_at/last_deployed_at; if migration not
    yet applied, we log + continue).
    """
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not sb_url or not sb_key or not migration_id:
        return False
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{sb_url}/rest/v1/site_migrations?id=eq.{migration_id}",
                json=payload,
                headers=headers,
            )
            if resp.status_code >= 300:
                logger.warning(
                    f"patch_migration {migration_id} {resp.status_code}: "
                    f"{resp.text[:300]}"
                )
                return False
            return True
    except Exception as e:
        logger.warning(f"patch_migration {migration_id} failed: {e}")
        return False
