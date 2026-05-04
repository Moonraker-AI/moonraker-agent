"""
utils/astro_tokens.py
=====================
Map captured `styles.json` (from site-capture step) to per-site CSS variable
overrides for the Astro template's `src/styles/tokens.override.css`.

Spec: docs/site-migration-agent-job-spec.md §2.5

Goal: visual replica of the origin. Emit a CSS variable for every value
we can derive from the captured computed styles. The migration template
ships intentionally neutral defaults (system-ui, white bg, plain blue
link) so any unmapped variable does NOT paint Moonraker brand onto the
client's site — falls back to neutral instead.

Inputs vary in shape across captured sites. We probe a handful of
sensible paths inside `styles.json` and OMIT a variable when no value
can be derived (rather than substituting a Moonraker default).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("agent.astro_tokens")


NEUTRAL_HEXES = {
    "#000", "#000000", "#fff", "#ffffff",
    "#fafafa", "#f5f5f5", "#eeeeee", "#dddddd",
    "#cccccc", "#bbbbbb", "#999999", "#666666", "#333333",
    "#1a1a1a", "#fafaf7",
}


# ── Color helpers ────────────────────────────────────────────────────────────

def _normalize_hex(c: str) -> Optional[str]:
    if not c or not isinstance(c, str):
        return None
    c = c.strip().lower()
    if c.startswith("#"):
        if len(c) == 4:
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
    r, g, b = (v / 255.0 for v in _hex_to_rgb(hex_color))
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return 0.0
    light = (mx + mn) / 2
    d = mx - mn
    return d / (2 - mx - mn) if light > 0.5 else d / (mx + mn)


def _lightness(hex_color: str) -> float:
    r, g, b = (v / 255.0 for v in _hex_to_rgb(hex_color))
    return (max(r, g, b) + min(r, g, b)) / 2


def _is_neutral(hex_color: str) -> bool:
    if hex_color in NEUTRAL_HEXES:
        return True
    return _saturation(hex_color) < 0.12


def _shift_lightness(hex_color: str, delta: float) -> str:
    """Approximate ±delta lightness shift for hover-state derivation."""
    r, g, b = _hex_to_rgb(hex_color)
    if delta < 0:
        r = max(0, int(r * (1 + delta)))
        g = max(0, int(g * (1 + delta)))
        b = max(0, int(b * (1 + delta)))
    else:
        r = min(255, int(r + (255 - r) * delta))
        g = min(255, int(g + (255 - g) * delta))
        b = min(255, int(b + (255 - b) * delta))
    return f"#{r:02x}{g:02x}{b:02x}"


def _contrasting_text(hex_color: str) -> str:
    """Return #ffffff or #000000 whichever has higher contrast against given bg."""
    r, g, b = (v / 255.0 for v in _hex_to_rgb(hex_color))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#000000" if luminance > 0.55 else "#ffffff"


# ── styles.json walkers (defensive — schema varies by capture) ───────────────

def _get_path(obj: Any, *keys: str) -> Any:
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def _styles_bag(styles: dict) -> dict:
    """Return the dict that holds selector→props pairs, handling
    {selectors: {...}} wrapper shape."""
    if not isinstance(styles, dict):
        return {}
    if isinstance(styles.get("selectors"), dict):
        return styles["selectors"]
    return styles


def _selector_props(styles: dict, selector: str) -> Optional[dict]:
    """Return the dict of props for an exact selector match. Coerces lists
    to first dict (some captures yield list-shaped entries)."""
    bag = _styles_bag(styles)
    val = bag.get(selector)
    if isinstance(val, list):
        val = next((x for x in val if isinstance(x, dict)), None)
    if isinstance(val, dict):
        return val
    return None


def _find_selectors_matching(styles: dict, predicate) -> list[tuple[str, dict]]:
    bag = _styles_bag(styles)
    out = []
    for sel, props in (bag or {}).items():
        if not isinstance(sel, str):
            continue
        if isinstance(props, list):
            props = next((x for x in props if isinstance(x, dict)), None)
        if not isinstance(props, dict):
            continue
        if predicate(sel):
            out.append((sel, props))
    return out


def _parse_px(value: Any) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"\s*([\d.]+)\s*px\s*$", value)
    return float(m.group(1)) if m else None


def _px_to_rem(px: float) -> str:
    return f"{px / 16.0:.3f}rem"


def _prop(props: dict, *keys: str) -> Optional[str]:
    """Get the first non-empty string value for any of the given prop keys.
    Tolerates camelCase + kebab-case captures."""
    for k in keys:
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# ── Role binning ─────────────────────────────────────────────────────────────

def _is_button_selector(sel: str) -> bool:
    s = sel.lower()
    if s.startswith("button"):
        return True
    if "btn" in s or "button" in s:
        return True
    if "a.btn" in s or 'a[class*="btn"]' in s:
        return True
    # Squarespace
    if "sqs-block-button-element" in s or "sqs-button-element" in s:
        return True
    return False


def _is_primary_button(sel: str) -> bool:
    s = sel.lower()
    if any(t in s for t in ("primary", "main", "filled", "solid", "submit")):
        return True
    # Squarespace's primary modifier
    if "--primary" in s:
        return True
    return False


def _is_secondary_button(sel: str) -> bool:
    s = sel.lower()
    if any(t in s for t in ("secondary", "outline", "ghost", "tertiary", "alt")):
        return True
    if "--secondary" in s or "--tertiary" in s:
        return True
    return False


def _is_nav_selector(sel: str) -> bool:
    s = sel.lower()
    if s.startswith("nav") or "navbar" in s or "site-nav" in s or s.startswith("header"):
        return True
    # Squarespace
    if any(t in s for t in (
        ".header-nav", ".header-title", ".header-actions", ".header-display",
        ".site-title", ".header-menu",
    )):
        return True
    return False


def _is_hero_selector(sel: str) -> bool:
    s = sel.lower()
    if ".hero" in s or s == "hero" or "banner" in s or s.startswith("section.hero"):
        return True
    # Squarespace banner sections
    if any(t in s for t in (".banner-section", ".banner-text", ".section-banner")):
        return True
    return False


def _is_footer_selector(sel: str) -> bool:
    s = sel.lower()
    if s.startswith("footer") or "site-footer" in s or s == ".footer":
        return True
    # Squarespace footer blocks
    if any(t in s for t in (".footer-blocks", ".sqs-block-summary", ".user-items-list")):
        return True
    return False


def _is_section_selector(sel: str) -> bool:
    s = sel.lower()
    if s == "section" or s.startswith("section ") or s == ".section":
        return True
    # Squarespace page sections
    if any(t in s for t in (".page-section", ".content-wrapper", ".sqs-layout")):
        return True
    return False


def bin_styles_by_role(styles: dict) -> dict[str, dict]:
    """Group selector entries into named roles by simple substring heuristics.

    Returns a dict like:
        {
          "nav": {merged props},
          "hero": {...},
          "primary_button": {...},
          "secondary_button": {...},
          "footer": {...},
          "section": {...},
          "body": {...},
          "h1".."h6": {...},
          "link": {...},
        }

    Each role's props are merged across all matching selectors with later
    entries winning. Rough heuristic — good enough for token derivation.
    """
    bag = _styles_bag(styles)
    roles: dict[str, dict] = {
        "nav": {},
        "hero": {},
        "primary_button": {},
        "secondary_button": {},
        "footer": {},
        "section": {},
        "body": {},
        "link": {},
        "h1": {}, "h2": {}, "h3": {}, "h4": {}, "h5": {}, "h6": {},
    }

    for sel, props in (bag or {}).items():
        if not isinstance(sel, str):
            continue
        if isinstance(props, list):
            props = next((x for x in props if isinstance(x, dict)), None)
        if not isinstance(props, dict):
            continue

        s = sel.strip()
        sl = s.lower()

        if s == "body":
            roles["body"].update(props)
        if s in ("h1", "h2", "h3", "h4", "h5", "h6"):
            roles[s].update(props)
        if s == "a" or sl.startswith("a "):
            roles["link"].update(props)
        if _is_nav_selector(sl):
            roles["nav"].update(props)
        if _is_hero_selector(sl):
            roles["hero"].update(props)
        if _is_footer_selector(sl):
            roles["footer"].update(props)
        if _is_section_selector(sl):
            roles["section"].update(props)
        if _is_button_selector(sl):
            if _is_primary_button(sl):
                roles["primary_button"].update(props)
            elif _is_secondary_button(sl):
                roles["secondary_button"].update(props)
            elif not roles["primary_button"]:
                # Generic .btn falls into primary if no explicit primary seen
                roles["primary_button"].update(props)

    return roles


# ── Derivation routines (one per CSS variable family) ────────────────────────

def _emit_color(out: dict, var: str, hex_value: Optional[str]):
    if hex_value:
        out[var] = hex_value


def _derive_colors(out: dict, styles: dict, roles: dict[str, dict]):
    body = roles.get("body") or {}
    nav = roles.get("nav") or {}
    hero = roles.get("hero") or {}
    footer = roles.get("footer") or {}
    section = roles.get("section") or {}
    link = roles.get("link") or {}
    pri_btn = roles.get("primary_button") or {}
    sec_btn = roles.get("secondary_button") or {}

    bg = _normalize_hex(_prop(body, "backgroundColor", "background-color"))
    text = _normalize_hex(_prop(body, "color"))
    _emit_color(out, "--color-bg", bg)
    _emit_color(out, "--color-text", text)

    # Alt background — try section bg or footer bg if different from body bg
    sec_bg = _normalize_hex(_prop(section, "backgroundColor", "background-color"))
    fb_bg = _normalize_hex(_prop(footer, "backgroundColor", "background-color"))
    alt = None
    for cand in (sec_bg, fb_bg):
        if cand and cand != bg:
            alt = cand
            break
    _emit_color(out, "--color-bg-alt", alt)

    # Border — try common border color from button or section
    sec_border = _normalize_hex(_prop(section, "borderColor", "border-color"))
    if sec_border:
        _emit_color(out, "--color-border", sec_border)

    # Primary color — strongest candidate from primary button bg, then link color
    pri_bg = _normalize_hex(_prop(pri_btn, "backgroundColor", "background-color"))
    link_color = _normalize_hex(_prop(link, "color"))
    primary = None
    if pri_bg and not _is_neutral(pri_bg):
        primary = pri_bg
    elif link_color and not _is_neutral(link_color):
        primary = link_color
    if primary:
        _emit_color(out, "--color-primary", primary)
        _emit_color(out, "--color-primary-text", _contrasting_text(primary))
        _emit_color(out, "--color-link", primary)
        # Hover ≈ 12% darker
        _emit_color(out, "--color-link-hover", _shift_lightness(primary, -0.12))

    # Muted / subtle text — try footer color
    footer_color = _normalize_hex(_prop(footer, "color"))
    if footer_color and footer_color != text:
        _emit_color(out, "--color-muted", footer_color)

    # Nav paint
    nav_bg = _normalize_hex(_prop(nav, "backgroundColor", "background-color"))
    nav_color = _normalize_hex(_prop(nav, "color"))
    _emit_color(out, "--nav-bg", nav_bg)
    _emit_color(out, "--nav-text", nav_color)
    _emit_color(out, "--nav-link-color", nav_color)

    # Hero paint
    hero_color = _normalize_hex(_prop(hero, "color"))
    hero_bg = _normalize_hex(_prop(hero, "backgroundColor", "background-color"))
    _emit_color(out, "--hero-text-color", hero_color)
    if hero_bg:
        _emit_color(out, "--hero-overlay-color", hero_bg)

    # Footer paint
    _emit_color(out, "--footer-bg", fb_bg)
    _emit_color(out, "--footer-text", footer_color)

    # Button paint (primary)
    pri_color = _normalize_hex(_prop(pri_btn, "color"))
    if pri_bg:
        out["--button-bg"] = pri_bg
    if pri_color:
        out["--button-text"] = pri_color


def _derive_typography(out: dict, styles: dict, roles: dict[str, dict]):
    body = roles.get("body") or {}
    body_font = _prop(body, "fontFamily", "font-family")
    if body_font:
        out["--font-body"] = body_font

    # Heading font: take h1's family if different from body's, else inherit body
    h1 = roles.get("h1") or {}
    h1_font = _prop(h1, "fontFamily", "font-family")
    if h1_font and (not body_font or h1_font.strip() != body_font.strip()):
        out["--font-display"] = h1_font
    elif body_font:
        # explicit duplicate so display matches body
        out["--font-display"] = body_font

    # Type scale — desktop sizes for h1..h6
    for h in ("h1", "h2", "h3", "h4", "h5", "h6"):
        size = _parse_px(_prop(roles.get(h, {}), "fontSize", "font-size"))
        if size:
            out[f"--type-scale-{h}"] = _px_to_rem(size)

    # Body type scale
    body_size = _parse_px(_prop(body, "fontSize", "font-size"))
    if body_size:
        out["--type-scale-body"] = _px_to_rem(body_size)

    # Line height + tracking on body
    lh = _prop(body, "lineHeight", "line-height")
    if lh:
        try:
            float(lh)
            out["--type-leading-normal"] = lh
        except ValueError:
            lh_px = _parse_px(lh)
            if lh_px and body_size:
                out["--type-leading-normal"] = f"{lh_px / body_size:.3f}"

    body_tracking = _prop(body, "letterSpacing", "letter-spacing")
    if body_tracking and body_tracking != "normal":
        out["--type-tracking-normal"] = body_tracking

    # Heading line-height + tracking — borrow from h1
    h1_lh = _prop(h1, "lineHeight", "line-height")
    if h1_lh:
        try:
            float(h1_lh)
            out["--type-leading-tight"] = h1_lh
        except ValueError:
            pass
    h1_tracking = _prop(h1, "letterSpacing", "letter-spacing")
    if h1_tracking and h1_tracking != "normal":
        out["--type-tracking-tight"] = h1_tracking

    # Heading weight
    h1_weight = _prop(h1, "fontWeight", "font-weight")
    if h1_weight:
        out["--type-weight-display"] = h1_weight
    body_weight = _prop(body, "fontWeight", "font-weight")
    if body_weight:
        out["--type-weight-body"] = body_weight


def _derive_spacing(out: dict, styles: dict, roles: dict[str, dict]):
    section = roles.get("section") or {}
    pt = _parse_px(_prop(section, "paddingTop", "padding-top"))
    pb = _parse_px(_prop(section, "paddingBottom", "padding-bottom"))
    if pt and pb:
        out["--spacing-section"] = _px_to_rem((pt + pb) / 2.0)
    elif pt:
        out["--spacing-section"] = _px_to_rem(pt)


def _derive_buttons(out: dict, styles: dict, roles: dict[str, dict]):
    pri = roles.get("primary_button") or {}
    radius = _parse_px(_prop(pri, "borderRadius", "border-radius"))
    if radius is not None:
        if radius >= 100:
            out["--button-radius"] = "999px"
            out["--radius-md"] = "999px"
        else:
            out["--button-radius"] = f"{radius:.0f}px"
            out["--radius-md"] = f"{radius:.0f}px"

    pad_y = _parse_px(_prop(pri, "paddingTop", "padding-top"))
    if pad_y:
        out["--button-padding-y"] = _px_to_rem(pad_y)
    pad_x = _parse_px(_prop(pri, "paddingLeft", "padding-left"))
    if pad_x:
        out["--button-padding-x"] = _px_to_rem(pad_x)

    weight = _prop(pri, "fontWeight", "font-weight")
    if weight:
        out["--button-font-weight"] = weight
    tt = _prop(pri, "textTransform", "text-transform")
    if tt and tt != "none":
        out["--button-text-transform"] = tt
    tracking = _prop(pri, "letterSpacing", "letter-spacing")
    if tracking and tracking != "normal":
        out["--button-letter-spacing"] = tracking


def _derive_radii(out: dict, styles: dict, roles: dict[str, dict]):
    """Pull additional radii hints from images / cards if available."""
    hero = roles.get("hero") or {}
    hero_radius = _parse_px(_prop(hero, "borderRadius", "border-radius"))
    if hero_radius and hero_radius > 0:
        out["--radius-lg"] = f"{hero_radius:.0f}px"


# ── Public API ───────────────────────────────────────────────────────────────

def derive_overrides(styles: dict) -> dict[str, str]:
    """Return a CSS-var-name -> value mapping. Missing values omitted so the
    template's neutral default (in tokens.css) wins via cascade.

    Visual-replica goal: emit as many vars as the captured styles support.
    Anything unmapped falls back to neutral system defaults — never to
    Moonraker brand.
    """
    if not isinstance(styles, dict):
        styles = {}

    roles = bin_styles_by_role(styles)
    out: dict[str, str] = {}

    _derive_colors(out, styles, roles)
    _derive_typography(out, styles, roles)
    _derive_spacing(out, styles, roles)
    _derive_buttons(out, styles, roles)
    _derive_radii(out, styles, roles)

    return out


def render_override_css(overrides: dict[str, str]) -> str:
    """Render the overrides dict to a stable CSS string.

    Variables NOT in overrides keep their value from tokens.css (neutral
    system defaults). The migration template has been depoinionated so a
    sparse override file does NOT paint Moonraker brand.
    """
    if not overrides:
        return (
            "/* No per-site overrides — neutral template defaults stand. */\n"
            ":root {}\n"
        )

    lines = [
        "/* Auto-generated by agent.astro_tokens — derived from captured styles.json. */",
        "/* Variables NOT listed here keep their value from tokens.css (neutral defaults). */",
        ":root {",
    ]
    # Stable ordering for diff readability
    order = [
        # Color
        "--color-primary", "--color-primary-text",
        "--color-bg", "--color-bg-alt",
        "--color-text", "--color-muted", "--color-border",
        "--color-link", "--color-link-hover",
        # Typography
        "--font-display", "--font-body",
        "--type-scale-h1", "--type-scale-h2", "--type-scale-h3",
        "--type-scale-h4", "--type-scale-h5", "--type-scale-h6",
        "--type-scale-body",
        "--type-weight-display", "--type-weight-body",
        "--type-leading-tight", "--type-leading-normal",
        "--type-tracking-tight", "--type-tracking-normal",
        # Spacing
        "--spacing-section",
        # Radius
        "--radius-md", "--radius-lg",
        # Button
        "--button-bg", "--button-text",
        "--button-radius", "--button-padding-x", "--button-padding-y",
        "--button-font-weight", "--button-text-transform",
        "--button-letter-spacing",
        # Nav
        "--nav-bg", "--nav-text", "--nav-link-color",
        # Hero
        "--hero-overlay-color", "--hero-text-color",
        # Footer
        "--footer-bg", "--footer-text",
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
