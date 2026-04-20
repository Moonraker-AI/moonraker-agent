"""
Browser engine selector for agent tasks.

- stealth=False: vanilla Playwright. Use for Surge audits, public-page scraping,
  screenshotting — any flow where detection is not a concern.
- stealth=True:  Patchright. Use for authenticated admin flows on platforms
  that actively probe for automation (Squarespace, WordPress admin).

Both engines expose the same `async_playwright()` / `sync_playwright()` API, so
task code is identical apart from the import. This helper makes the choice
explicit and centralised.

Persistent profile pattern (SQSP/WP):
    async with get_playwright(stealth=True) as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir_for(credential_id),
            **chromium_launch_args(stealth=True),
        )

Profiles are backed up nightly to Hetzner Storage Box. Never store profiles
on ephemeral container storage.
"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

PROFILE_ROOT = Path("/data/profiles")

# Accepts UUIDs, slugs, and hex digests. Rejects anything that could escape
# PROFILE_ROOT or pollute the filesystem.
_CREDENTIAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@asynccontextmanager
async def get_playwright(stealth: bool = False):
    """Yield an async playwright context. Patchright is drop-in API-compatible."""
    if stealth:
        from patchright.async_api import async_playwright
    else:
        from playwright.async_api import async_playwright
    async with async_playwright() as p:
        yield p


def chromium_launch_args(stealth: bool = False) -> dict:
    """
    Launch kwargs for chromium.launch() or chromium.launch_persistent_context().

    Currently returns headless args for both stealth and non-stealth paths.
    Patchright's stealth value comes primarily from its patched driver, not
    from headful mode, so headless still strips webdriver, Runtime.enable,
    and most CDP tells. Moving to headful requires Xvfb in the Dockerfile
    (DISPLAY=:99 is set but no Xvfb process is started) — future PR.

    --no-sandbox is required because the container runs as non-root appuser.
    --disable-dev-shm-usage avoids /dev/shm exhaustion in small shm_size
    allocations (compose gives us 2gb, but defensive anyway).
    """
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if stealth:
        args.append("--disable-blink-features=AutomationControlled")
    return {"headless": True, "args": args}


def profile_dir_for(credential_id: str) -> str:
    """
    Per-credential persistent profile directory.

    Key on workspace_credentials.id (UUID). One row per site+platform pair,
    so Kelly Chisholm's two WP sites get two profiles naturally. Shared
    Moonraker-admin SQSP account uses a single credential row ->
    single shared profile dir.

    Directory is created on first use; subsequent runs restore cookies,
    localStorage, service workers, and IndexedDB from prior sessions.
    """
    if not isinstance(credential_id, str) or not _CREDENTIAL_ID_RE.match(credential_id):
        raise ValueError(f"invalid credential_id: {credential_id!r}")
    path = PROFILE_ROOT / credential_id
    path.mkdir(parents=True, exist_ok=True)
    return str(path)
