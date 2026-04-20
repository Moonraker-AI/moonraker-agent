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

from contextlib import asynccontextmanager
from pathlib import Path

PROFILE_ROOT = Path("/data/profiles")


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
    Launch kwargs tuned per engine.

    Patchright recommends headed Chrome + xvfb for best stealth (DISPLAY=:99 is
    set in the Dockerfile). Vanilla Playwright runs headless for throughput.

    --no-sandbox is required because the container runs as non-root appuser
    without SYS_ADMIN capability. Chromium sandboxing needs root or
    a user namespace that the slim base image does not provide.
    """
    if stealth:
        return {
            "headless": False,
            "channel": "chrome",
            "no_viewport": True,
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        }
    return {
        "headless": True,
        "args": ["--no-sandbox"],
    }


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
    if not credential_id or "/" in credential_id or ".." in credential_id:
        raise ValueError(f"invalid credential_id: {credential_id!r}")
    path = PROFILE_ROOT / credential_id
    path.mkdir(parents=True, exist_ok=True)
    return str(path)
