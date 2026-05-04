"""
utils/astro_tokens.py
=====================
Map captured `styles.json` (from site-capture step) to per-site CSS variable
overrides for the Astro template's `src/styles/tokens.override.css`.

Spec: docs/site-migration-agent-job-spec.md §2.5

Inputs vary in shape across captured sites. We probe a handful of
sensible paths inside `styles.json` and fall back to template defaults
(Moonraker brand) when a value is missing or unparseable.

Heuristics:
- --color-primary: walk every captured `button.*` selector, pick the most
  saturated non-neutral background color. If none qualifies, fall back to
  the most saturated link color, then to the Moonraker brand green.
- --font-display vs --font-body: if h1.fontFamily == body.fontFamily, only
  emit --font-body so the template default for display can stand.
- --type-scale-h1: derive a clamp(min, vw, max) from desktop+mobile h1 sizes
  if both present; otherwise emit the desktop value as a fixed rem.
- --spacing-section: scrape section.paddingTop (raw px), wrap in clamp().
- --radius-md: button.borderRadius first, otherwise fall back.

The function is pure and side-effect free; the caller writes the file.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("agent.astro_tokens")

# Moonraker template defaults — used as fallbacks when capture did not
# resolve a usable value. Keep these in sync with src/styles/tokens.css
# in moonraker-site-template.
DEFAULT_PRIMARY = "#00D47E"
DEFAULT_BG = "#FAFAF7"
DEFAULT_TEXT = "#1A1A1A"
DEFAULT_FONT_BODY = "'Inter', sans-serif"
DEFAULT_FONT_DISPLAY = "'Outfit', sans-serif"
DEFAULT_RADIUS_MD = "12px"

NEUTRAL_HEXES = {
    "#000", "#000000", "#fff", "#ffffff",
    "#fafafa", "#f5f5f5", "#eeeeee", "#dddddd",
    "#cccccc", "#bbbbbb", "#999999", "#666666", "#333333",
    "#1a1a1a", "#fafaf7",
}


# ── Color helpers ────────────────────────────────────────────────────────────

def _normalize_hex(c: str) -> Optional[str]:
    if not c:
        return None
    c = c.strip().lower()
    if c.startswith("#"):
        if len(c) == 4:  # #abc -> #aabbcc
            c = "#" + "".join(ch * 2 for ch in c[1:])
        if re.fullmatch(r"#[0-9a-f]{6}", c):
            return c
        return None
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d.]+\s*)?\)", c)
    if m:
        r, g, b = (int(x) for x in m.groups())
        return f"#{r:02x}{g:02x}{b:02x}"
    return None


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _saturation(hex_color: str) -> float:
    """HSL saturation in 0..1. Neutrals (gray/black/white) score very low."""
    r, g, b = (v / 255.0 for v in _hex_to_rgb(hex_color))
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return 0.0
    light = (mx + mn) / 2
    d = mx - mn
    return d / (2 - mx - mn) if light > 0.5 else d / (mx + mn)


def _is_neutral(hex_color: str) -> bool:
    if hex_color in NEUTRAL_HEXES:
        return True
    return _saturation(hex_color) < 0.12


# ── styles.json walkers (defensive — schema varies by capture) ───────────────

def _get_path(obj: Any, *keys: str) -> Any:
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def _find_selector_props(styles: dict, selector_prefix: str) -> list[dict]:
    """Return all entries in styles whose key starts with `selector_prefix`.

    `styles.json` from site-capture is a dict keyed by selector (e.g.
    "body", "h1", "a", "button.btn-primary", "section.hero"), where each
    value is a dict of computed-style props. Some captures wrap entries
    under a top-level `selectors` key.
    """
    bag: dict
    if isinstance(styles.get("selectors"), dict):
        bag = styles["selectors"]
    else:
        bag = styles
    out = []
    for sel, props in (bag or {}).items():
        if isinstance(sel, str) and isinstance(props, dict) and sel.startswith(selector_prefix):
            out.append(props)
    return out


def _pick_primary_color(styles: dict) -> str:
    candidates: list[str] = []
    for props in _find_selector_props(styles, "button"):
        bg = _normalize_hex(props.get("backgroundColor") or props.get("background-color") or "")
        if bg and not _is_neutral(bg):
            candidates.append(bg)
    if candidates:
        return max(candidates, key=_saturation)
    # Fall back to anchor color
    link_candidates: list[str] = []
    for props in _find_selector_props(styles, "a"):
        c = _normalize_hex(props.get("color") or "")
        if c and not _is_neutral(c):
            link_candidates.append(c)
    if link_candidates:
        return max(link_candidates, key=_saturation)
    return DEFAULT_PRIMARY


def _pick_font(styles: dict, selector: str) -> Optional[str]:
    props = _get_path(styles, selector) or _get_path(styles, "selectors", selector)
    if not isinstance(props, dict):
        return None
    fam = props.get("fontFamily") or props.get("font-family")
    if not fam or not isinstance(fam, str):
        return None
    return fam.strip()


def _pick_color(styles: dict, selector: str, prop: str) -> Optional[str]:
    props = _get_path(styles, selector) or _get_path(styles, "selectors", selector)
    if not isinstance(props, dict):
        return None
    return _normalize_hex(props.get(prop) or "")


def _parse_px(value: str) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"\s*([\d.]+)\s*px\s*$", value)
    return float(m.group(1)) if m else None


def _h1_clamp(styles: dict) -> Optional[str]:
    """Derive a clamp() for --type-scale-h1.

    Capture may include desktop and mobile passes under different keys:
    `h1`, `h1@desktop`, `h1@mobile`. Best-effort.
    """
    h1 = (
        _get_path(styles, "h1")
        or _get_path(styles, "selectors", "h1")
        or {}
    )
    desktop = h1.get("fontSize") or h1.get("font-size") or ""
    mobile = (
        _get_path(styles, "h1@mobile", "fontSize")
        or _get_path(styles, "h1@mobile", "font-size")
        or _get_path(styles, "selectors", "h1@mobile", "fontSize")
        or ""
    )
    d_px = _parse_px(desktop)
    m_px = _parse_px(mobile)
    if d_px and m_px and d_px != m_px:
        lo = min(d_px, m_px) / 16.0
        hi = max(d_px, m_px) / 16.0
        return f"clamp({lo:.2f}rem, 6vw, {hi:.2f}rem)"
    if d_px:
        return f"{d_px / 16.0:.2f}rem"
    return None


def _section_spacing(styles: dict) -> Optional[str]:
    pt = (
        _get_path(styles, "section", "paddingTop")
        or _get_path(styles, "selectors", "section", "paddingTop")
        or _get_path(styles, "section", "padding-top")
    )
    px = _parse_px(pt) if isinstance(pt, str) else None
    if not px:
        return None
    rem = px / 16.0
    lo = max(2.0, rem * 0.6)
    hi = rem
    return f"clamp({lo:.2f}rem, 8vw, {hi:.2f}rem)"


def _button_radius(styles: dict) -> Optional[str]:
    for props in _find_selector_props(styles, "button"):
        br = props.get("borderRadius") or props.get("border-radius")
        if isinstance(br, str) and br.strip() and br.strip() != "0px":
            return br.strip()
    return None


# ── Public API ───────────────────────────────────────────────────────────────

def derive_overrides(styles: dict) -> dict[str, str]:
    """Return a CSS-var-name -> value mapping. Missing values omitted so the
    template default (in tokens.css) wins via cascade."""
    if not isinstance(styles, dict):
        styles = {}

    out: dict[str, str] = {}

    primary = _pick_primary_color(styles)
    out["--color-primary"] = primary

    bg = _pick_color(styles, "body", "backgroundColor") or _pick_color(styles, "body", "background-color")
    if bg:
        out["--color-bg"] = bg

    text = _pick_color(styles, "body", "color")
    if text:
        out["--color-text"] = text

    body_font = _pick_font(styles, "body")
    h1_font = _pick_font(styles, "h1")
    if body_font:
        out["--font-body"] = body_font
    if h1_font and (not body_font or h1_font.strip() != body_font.strip()):
        out["--font-display"] = h1_font

    h1_clamp = _h1_clamp(styles)
    if h1_clamp:
        out["--type-scale-h1"] = h1_clamp

    spacing = _section_spacing(styles)
    if spacing:
        out["--spacing-section"] = spacing

    radius = _button_radius(styles)
    if radius:
        out["--radius-md"] = radius

    return out


def render_override_css(overrides: dict[str, str]) -> str:
    """Render the overrides dict to a stable CSS string."""
    if not overrides:
        return "/* No per-site overrides — template defaults stand. */\n:root {}\n"
    lines = [
        "/* Auto-generated by agent.astro_tokens — derived from captured styles.json. */",
        "/* Variables NOT listed here keep their value from tokens.css (Moonraker defaults). */",
        ":root {",
    ]
    # Stable ordering (cosmetic; aids diffing across re-runs)
    order = [
        "--color-primary", "--color-bg", "--color-text",
        "--font-display", "--font-body",
        "--type-scale-h1",
        "--spacing-section",
        "--radius-md",
    ]
    seen: set[str] = set()
    for key in order:
        if key in overrides:
            lines.append(f"  {key}: {overrides[key]};")
            seen.add(key)
    for k, v in overrides.items():
        if k in seen:
            continue
        lines.append(f"  {k}: {v};")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)
