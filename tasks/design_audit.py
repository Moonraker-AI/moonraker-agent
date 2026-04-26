"""
design_audit.py

Headless design quality audit using the impeccable browser-mode detector.
Loads a target URL in chromium, injects the upstream detector, runs the scan,
and returns structured findings. Works on any rendered page regardless of
platform (WordPress, Squarespace, Wix, R2, etc.) because it inspects the
final computed-style output, not source HTML.

Synchronous: caller awaits findings directly. Designed for ~5-10s execution.
Hard timeout at 30s.

Entry: run_design_audit(url, viewport=None, wait_for=None, vendor="playwright")
       returns: {
           "url": str,
           "scanned_at": iso8601,
           "viewport": {"width": int, "height": int},
           "duration_ms": int,
           "findings": [...],
           "summary": {
               "total": int,
               "by_severity": {"absolute": n, "strong": n, "advisory": n},
               "by_category": {"slop": n, "quality": n}
           }
       }
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger("moonraker.design_audit")

# Path to the vendored upstream detector. Resolved relative to this file
# so it works from /app/tasks/ in the container.
DETECTOR_PATH = Path(__file__).parent.parent / "static" / "impeccable-browser-detector.js"

# Severity classification — applied post-scan, since the upstream detector's
# category field is "slop" or "quality", not severity. We map known IDs.
ABSOLUTE_BAN_IDS = {
    "side-tab",
    "border-accent-on-rounded",
    "gradient-text",
}

STRONG_BAN_IDS = {
    "ai-color-palette",
    "nested-cards",
    "pure-black-white",
    "gray-on-color",
    "bounce-easing",
    "dark-glow",
    "icon-tile-stack",
    "layout-transition",
    "low-contrast",
    "everything-centered",
    "monotonous-spacing",
}

# Anything else the upstream detector finds gets "advisory".


def _severity_for(finding_id: str) -> str:
    if finding_id in ABSOLUTE_BAN_IDS:
        return "absolute"
    if finding_id in STRONG_BAN_IDS:
        return "strong"
    return "advisory"


# JS that runs inside page.evaluate() AFTER the detector script has been
# injected. The detector defines window.impeccableScan; we call it and serialize
# results manually since serializeFindings() is private to the IIFE.
_SCAN_JS = r"""
async () => {
  if (typeof window.impeccableScan !== 'function') {
    throw new Error('impeccableScan not available — detector script did not load');
  }
  // The detector scan returns an array of { el: Element, findings: [...] }.
  // DOM elements don't serialize, so flatten + extract what we need.
  const all = window.impeccableScan();
  const out = [];
  for (const entry of all || []) {
    const el = entry.el;
    let selector = '';
    let tagName = '';
    let rect = null;
    let isPageLevel = false;
    try {
      tagName = (el && el.tagName ? el.tagName.toLowerCase() : 'unknown');
      isPageLevel = (el === document.body || el === document.documentElement);
      // Best-effort selector: id, then classes, then tag.
      if (el && el.id) {
        selector = '#' + el.id;
      } else if (el && el.classList && el.classList.length) {
        selector = tagName + '.' + Array.from(el.classList).slice(0, 3).join('.');
      } else {
        selector = tagName;
      }
      if (!isPageLevel && el && el.getBoundingClientRect) {
        const r = el.getBoundingClientRect();
        rect = { x: r.x, y: r.y, width: r.width, height: r.height };
      }
    } catch (e) {
      selector = 'unknown';
    }
    for (const f of entry.findings || []) {
      out.push({
        id: f.type || f.id,
        detail: f.detail || f.snippet || '',
        selector: selector,
        tagName: tagName,
        rect: rect,
        isPageLevel: isPageLevel
      });
    }
  }
  return out;
}
"""


async def run_design_audit(
    url: str,
    viewport: dict | None = None,
    wait_for: str | None = None,
    timeout_seconds: int = 30,
):
    """
    Run a design audit against a URL.

    Args:
        url: Full URL to audit.
        viewport: Optional dict with width/height. Default 1440x900.
        wait_for: Optional CSS selector to wait for before scanning.
                  Useful for slow-painting frameworks. Skipped if None.
        timeout_seconds: Hard upper bound on the entire run.

    Returns:
        Dict with findings + summary (see module docstring).

    Raises:
        TimeoutError if the run exceeds timeout_seconds.
        FileNotFoundError if the detector JS isn't on disk.
        RuntimeError for other browser failures.
    """
    if not DETECTOR_PATH.exists():
        raise FileNotFoundError(f"Detector not found at {DETECTOR_PATH}")
    detector_js = DETECTOR_PATH.read_text(encoding="utf-8")

    vp = viewport or {"width": 1440, "height": 900}
    started = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat()

    async def _do():
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                context = await browser.new_context(
                    viewport=vp,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()
                # Mark the document so the detector enters extension mode.
                # Done via init script so it runs before any page script.
                await context.add_init_script(
                    "document.documentElement.dataset.impeccableExtension = 'true';"
                )

                logger.info(f"design_audit: navigating to {url}")
                # networkidle is too aggressive for some sites; use load + small settle.
                await page.goto(url, wait_until="load", timeout=20000)

                if wait_for:
                    try:
                        await page.wait_for_selector(wait_for, timeout=8000)
                    except PWTimeout:
                        logger.warning(f"design_audit: wait_for selector '{wait_for}' timed out, continuing")

                # Brief settle for fonts/post-paint.
                await page.wait_for_timeout(800)

                # Inject the detector. add_script_tag with content runs synchronously
                # in document context, so window.impeccableScan is defined immediately.
                await page.add_script_tag(content=detector_js)

                # Fonts may still be loading. Wait briefly for document.fonts.ready
                # so typography rules see actual rendered metrics.
                await page.evaluate(
                    "() => (document.fonts && document.fonts.ready) ? document.fonts.ready : null"
                )

                # Run the scan.
                findings_raw = await page.evaluate(_SCAN_JS)

                return findings_raw or []
            finally:
                await browser.close()

    try:
        findings_raw = await asyncio.wait_for(_do(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Design audit exceeded {timeout_seconds}s for {url}")

    duration_ms = int((time.monotonic() - started) * 1000)

    # Annotate severity. Tally summary.
    by_sev = {"absolute": 0, "strong": 0, "advisory": 0}
    by_cat = {"slop": 0, "quality": 0, "other": 0}
    findings = []
    for f in findings_raw:
        sev = _severity_for(f["id"])
        f["severity"] = sev
        by_sev[sev] += 1
        # Best-effort category from upstream's known IDs (kept loose since
        # the detector's category is per-rule, not exposed in evaluate output).
        cat = "slop" if f["id"] in (ABSOLUTE_BAN_IDS | {
            "ai-color-palette", "nested-cards", "bounce-easing",
            "dark-glow", "icon-tile-stack", "monotonous-spacing",
            "everything-centered", "single-font", "flat-type-hierarchy",
            "overused-font",
        }) else "quality"
        f["category"] = cat
        by_cat[cat] = by_cat.get(cat, 0) + 1
        findings.append(f)

    # Stable sort: absolute → strong → advisory, then by id.
    sev_rank = {"absolute": 0, "strong": 1, "advisory": 2}
    findings.sort(key=lambda x: (sev_rank.get(x["severity"], 3), x["id"]))

    return {
        "url": url,
        "scanned_at": started_iso,
        "viewport": vp,
        "duration_ms": duration_ms,
        "findings": findings,
        "summary": {
            "total": len(findings),
            "by_severity": by_sev,
            "by_category": by_cat,
        },
    }
