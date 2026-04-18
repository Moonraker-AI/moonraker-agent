"""
Debug Capture
=============
Persists page HTML + screenshot + metadata to /tmp/agent-debug/<task_id>/ when
the agent hits any abnormal terminal state (surge_maintenance, surge_rejected,
credits_exhausted, timeout, generic exception).

This exists because the initial credits_exhausted false-positive bug on
2026-04-18 (Surge maintenance banner matched the loose "no credits" substring
check) was diagnosed by tailing container logs and piecing together what the
Browser Use agent reported. Capturing raw evidence makes the next diagnosis
trivial.

Retention: files are cleaned up by utils.cleanup.full_cleanup at end of each
task run. If the agent crashes hard, a separate sweep (next task start) clears
anything older than 7 days.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("agent.debug_capture")

DEBUG_ROOT = Path("/tmp/agent-debug")


async def capture_debug(task_id: str, page, reason: str, extra: dict = None) -> str:
    """
    Save page HTML + viewport screenshot + reason metadata.

    Args:
        task_id: agent task UUID (used as directory name)
        page: Browser Use / Playwright page wrapper exposing
              .content() / .evaluate() / .screenshot()
        reason: short label for the capture (e.g. 'surge_maintenance')
        extra: optional dict persisted to metadata.json alongside the capture

    Returns:
        Absolute path to the capture directory as a string, or an empty string
        if the capture failed. Never raises.
    """
    try:
        capture_dir = DEBUG_ROOT / task_id
        capture_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        prefix = f"{ts}_{reason}"

        # Page HTML — best-effort; Browser Use 0.12.x exposes evaluate() on the
        # wrapper, which lets us grab document.documentElement.outerHTML without
        # relying on Playwright's native .content() method.
        try:
            html = await page.evaluate(
                "() => document.documentElement ? document.documentElement.outerHTML : ''"
            )
            if html:
                (capture_dir / f"{prefix}.html").write_text(html, encoding="utf-8")
        except Exception as e:
            logger.warning(f"capture_debug: could not save HTML: {e}")

        # Page innerText (smaller, easier to grep later)
        try:
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if text:
                (capture_dir / f"{prefix}.txt").write_text(text, encoding="utf-8")
        except Exception as e:
            logger.warning(f"capture_debug: could not save innerText: {e}")

        # Screenshot — Browser Use 0.12.x exposes take_screenshot on the Page
        # wrapper; fall back to Playwright's .screenshot() if that signature
        # isn't available. Either path is best-effort.
        shot_path = capture_dir / f"{prefix}.png"
        try:
            if hasattr(page, "take_screenshot"):
                await page.take_screenshot(path=str(shot_path))
            elif hasattr(page, "screenshot"):
                await page.screenshot(path=str(shot_path))
        except Exception as e:
            logger.warning(f"capture_debug: could not save screenshot: {e}")

        # Current URL
        try:
            url = await page.get_url() if hasattr(page, "get_url") else None
        except Exception:
            url = None

        # Metadata sidecar
        meta = {
            "task_id": task_id,
            "reason": reason,
            "captured_at": ts,
            "url": url,
        }
        if extra:
            meta["extra"] = extra
        (capture_dir / f"{prefix}.meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

        logger.info(f"Debug capture saved: {capture_dir} (reason={reason})")
        return str(capture_dir)

    except Exception as e:
        logger.warning(f"capture_debug failed entirely: {e}")
        return ""
