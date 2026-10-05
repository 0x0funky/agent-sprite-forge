"""Shared library for the codeart2d skill: code-authored 2D game art.

Art is written as text (portable SVG or PixelSpec JSON) and turned into checked
8-bit straight-alpha RGBA pixels here. Nothing in this module calls an image
model; outputs are disclosed as "code-drawn, no image model".

Sections:
  palette    hex_to_rgba, rgba_to_hex, Palette, parse_palette
  svg        inline_css_vars, compile_svg, lint_portable_svg
  raster     rasterize, backend_info, available_backends, RasterError
  doctor     doctor_cases, check_doctor_case, run_doctor
  pixels     add_outline, snap_exact, Ramp, pixel_finish
  pixelspec  render_pixelspec
  qa         qa_pixels, detect_grid
  review     upscale_nearest, review_sheet
  masks      inside_polygon, distance_field (shared by the codeart2d map and plate tools)
  output     save_png, write_codeart_meta

Only numpy and Pillow are required (scipy is optional and only speeds up
distance_field). Rasterizing SVG needs one backend: resvg-py
(python -m pip install "resvg-py>=0.5,<0.6"), the resvg-js CLI on PATH, or a
headless Chrome/Edge/Chromium. PyMuPDF, skia-python and cairosvg are never used:
they silently ignore crispEdges, <style>, clipPath or gradients.

API 1.1 (Phase 3 integration) adds, without changing any 1.0 name or behaviour:
write_codeart_meta(placeholder=...) (B21 request, D30), FORGE_PACKAGE_VERSION as the
QA envelope tool version (D29), inside_polygon and distance_field, and the lint code
"reference" (an href that does not point inside the document). Hashing and JSON
writing go through the vendored forge_core (D30).

Sibling scripts load this file by path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import codeart_core
It imports the forge_core.py beside it (the skill's vendored copy).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)  # the sibling forge_core.py (this skill's vendored copy)
import forge_core  # noqa: E402


CODEART_CORE_API_VERSION = "1.1"
FORGE_PACKAGE_VERSION = forge_core.FORGE_PACKAGE_VERSION  # D29: the tool.version of every codeart2d QA envelope
PIXELSPEC_SCHEMA = "codeart2d.pixelspec.v1"
PIXELSPEC_SCHEMA_ALIASES = ("codeart.pixelspec.v1",)  # spelling used by roadmap 4.5 and the prototype
CODEART_META_SCHEMA = "codeart2d.codeart_meta.v1"
DISCLOSURE = "code-drawn, no image model"
OUTLINE_MODES = ("none", "solid", "selout")
RASTER_BACKENDS = ("resvg_py", "resvg_js_cli", "chrome")
RESVG_PIP_HINT = 'python -m pip install "resvg-py>=0.5,<0.6"'


class CodeArtError(ValueError):
    """Invalid code-art input: a palette, SVG source, PixelSpec or pixel array."""


class RasterError(RuntimeError):
    """No usable SVG rasterizer was found, or the selected rasterizer failed."""


# ----------------------------------------------------------------------------- array helpers

_DIRS4 = ((-1, 0), (1, 0), (0, -1), (0, 1))  # (dy, dx): up, down, left, right
_DIAGONALS = ((-1, -1), (-1, 1), (1, -1), (1, 1))


def _neighbour(array: np.ndarray, dy: int, dx: int, fill: Any) -> np.ndarray:
    """Each pixel's neighbour value at (y + dy, x + dx); `fill` where that falls outside."""
    out = np.full_like(array, fill)
    h, w = array.shape[:2]
    out[max(0, -dy):h - max(0, dy), max(0, -dx):w - max(0, dx)] = \
        array[max(0, dy):h - max(0, -dy), max(0, dx):w - max(0, -dx)]
    return out


def _ring(visible: np.ndarray) -> np.ndarray:
    """Exterior 4-neighbour ring of a mask, clipped to the canvas."""
    grown = np.zeros_like(visible)
    for dy, dx in _DIRS4:
        grown |= _neighbour(visible, dy, dx, False)
    return grown & ~visible


def _border(mask: np.ndarray) -> np.ndarray:
    edge = np.zeros_like(mask)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    return mask & edge


def _l_corner_mask(line: np.ndarray) -> np.ndarray:
    """Line pixels joining one horizontal and one vertical line neighbour whose shared
    diagonal is empty: removable for a pixel-perfect 1 px line."""
    p = np.pad(line, 1)
    up, down, left, right = p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]
    ul, ur, dl, dr = p[:-2, :-2], p[:-2, 2:], p[2:, :-2], p[2:, 2:]
    n4 = up.astype(np.uint8) + down + left + right
    return line & (n4 == 2) & ((up & left & ~ul) | (up & right & ~ur) | (down & left & ~dl) | (down & right & ~dr))


def _pack(rgba: np.ndarray) -> np.ndarray:
    c = rgba.astype(np.uint32)
    return (c[..., 0] << 24) | (c[..., 1] << 16) | (c[..., 2] << 8) | c[..., 3]


def _unpack(keys: np.ndarray) -> np.ndarray:
    return np.stack([(keys >> shift) & 255 for shift in (24, 16, 8, 0)], axis=-1).astype(np.uint8)


def _key(rgba: Sequence[int]) -> int:
    r, g, b, a = (int(v) for v in rgba)
    return (r << 24) | (g << 16) | (b << 8) | a


def _luma(keys: np.ndarray) -> np.ndarray:
    return 0.299 * ((keys >> 24) & 255) + 0.587 * ((keys >> 16) & 255) + 0.114 * ((keys >> 8) & 255)


def _lookup(keys: np.ndarray, table_in: np.ndarray, table_out: np.ndarray, default: int) -> np.ndarray:
    """Map packed colours through a sorted lookup table; unmatched keys become `default`."""
    pos = np.minimum(np.searchsorted(table_in, keys), len(table_in) - 1)
    return np.where(table_in[pos] == keys, table_out[pos], np.uint32(default)).astype(np.uint32)


def _darkest_candidate(ring: np.ndarray, candidates: list[tuple[np.ndarray, np.ndarray]], default: int) -> np.ndarray:
    """Per ring pixel, the lowest-luma valid candidate colour (earlier candidates win ties)."""
    best = np.full(ring.shape, default, np.uint32)
    best_luma = np.full(ring.shape, np.inf)
    for valid, keys in candidates:
        luma = _luma(keys)
        take = ring & valid & (luma < best_luma)
        best[take] = keys[take]
        best_luma[take] = luma[take]
    return best


def _as_rgba(image: Any) -> np.ndarray:
    """Copy an RGBA/RGB array or Pillow image into canonical (H, W, 4) uint8 RGBA (RGB zeroed under alpha 0)."""
    if isinstance(image, Image.Image):
        array = np.asarray(image.convert("RGBA"))
    else:
        array = np.asarray(image)
        if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] not in (3, 4):
            raise CodeArtError(f"expected an (H, W, 3|4) uint8 pixel array, got {array.dtype} {array.shape}")
        if array.shape[2] == 3:
            array = np.concatenate([array, np.full(array.shape[:2] + (1,), 255, np.uint8)], axis=2)
    out = np.array(array, dtype=np.uint8, copy=True)
    out[out[..., 3] == 0, :3] = 0
    return out


def _ascii(text: str) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


# ----------------------------------------------------------------------------- palette

_HEX_DIGITS = re.compile(r"#?([0-9A-Fa-f]+)")


def hex_to_rgba(value: Any) -> tuple[int, int, int, int]:
    """Parse #rgb, #rgba, #rrggbb or #rrggbbaa ('#' optional) or a 3/4-integer sequence."""
    if isinstance(value, str):
        match = _HEX_DIGITS.fullmatch(value.strip())
        digits = match.group(1) if match else ""
        if len(digits) in (3, 4):
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) == 6:
            digits += "ff"
        if len(digits) != 8:
            raise CodeArtError(f"bad colour {value!r}; use #rrggbb or #rrggbbaa")
        r, g, b, a = (int(digits[i:i + 2], 16) for i in range(0, 8, 2))
        return r, g, b, a
    if isinstance(value, (tuple, list, np.ndarray)) and len(value) in (3, 4):
        items = [v for v in value]
        if all(isinstance(v, (int, np.integer)) and not isinstance(v, bool) and 0 <= v <= 255 for v in items):
            r, g, b = (int(v) for v in items[:3])
            return r, g, b, int(items[3]) if len(items) == 4 else 255
    raise CodeArtError(f"bad colour {value!r}; use #rrggbb, #rrggbbaa or 3-4 integers in 0..255")


def rgba_to_hex(value: Any) -> str:
    """Format a colour as #rrggbb, or #rrggbbaa when it is not fully opaque."""
    r, g, b, a = hex_to_rgba(value)
    return f"#{r:02x}{g:02x}{b:02x}" + ("" if a == 255 else f"{a:02x}")


@dataclass(frozen=True)
class Palette:
    """Named base colours plus named variants (partial overrides of the base colours)."""

    colors: dict[str, tuple[int, int, int, int]]
    variants: dict[str, dict[str, tuple[int, int, int, int]]] = field(default_factory=dict)

    def resolve(self, variant: str | Mapping | None = None) -> dict[str, tuple[int, int, int, int]]:
        """Base colours with a named variant (or an ad hoc {name: colour} mapping) applied."""
        colors = dict(self.colors)
        if variant is None:
            return colors
        if isinstance(variant, str):
            if variant not in self.variants:
                known = ", ".join(sorted(self.variants)) or "none"
                raise CodeArtError(f"unknown palette variant {variant!r}; known variants: {known}")
            colors.update(self.variants[variant])
        else:
            colors.update(_colour_overrides(variant, self.colors, "variant"))
        return colors

    def hex(self, variant: str | Mapping | None = None) -> dict[str, str]:
        return {name: rgba_to_hex(colour) for name, colour in self.resolve(variant).items()}


def _palette_entries(entries: Any) -> dict[str, tuple[int, int, int, int]]:
    colors: dict[str, tuple[int, int, int, int]] = {}
    if isinstance(entries, Mapping):
        items = [(str(name), value) for name, value in entries.items()]
    elif isinstance(entries, (list, tuple)):
        items = []
        for index, entry in enumerate(entries):
            if isinstance(entry, Mapping):
                if "hex" not in entry:
                    raise CodeArtError(f"palette entry {index} needs a 'hex' colour")
                items.append((str(entry.get("name", index)), entry["hex"]))
            else:
                items.append((str(index), entry))
    else:
        raise CodeArtError("palette colours must be a {name: colour} mapping or a list")
    for name, value in items:
        if not name or name in colors:
            raise CodeArtError(f"palette colour names must be unique and nonempty (got {name!r})")
        colors[name] = hex_to_rgba(value)
    if not colors:
        raise CodeArtError("palette is empty")
    return colors


def _colour_overrides(overrides: Any, base: Mapping[str, Any], label: str) -> dict[str, tuple[int, int, int, int]]:
    if not isinstance(overrides, Mapping):
        raise CodeArtError(f"{label} must be a {{name: colour}} mapping")
    unknown = [str(name) for name in overrides if str(name) not in base]
    if unknown:
        raise CodeArtError(f"{label} overrides unknown palette colour(s): {', '.join(unknown)}")
    return {str(name): hex_to_rgba(value) for name, value in overrides.items()}


def _read_palette_file(path: Path) -> Palette:
    if not path.is_file():
        raise CodeArtError(f"palette file not found: {path}")
    try:
        text = path.read_bytes().decode("utf-8-sig")  # a BOM is tolerated (D28)
    except UnicodeDecodeError:
        raise CodeArtError(f"palette file {path.name} is not UTF-8 text") from None
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return parse_palette(forge_core.parse_json(text))
        except json.JSONDecodeError as exc:
            raise CodeArtError(f"palette file {path.name} is not valid JSON: {exc}") from None
    if suffix == ".hex":
        return parse_palette([line.strip() for line in text.splitlines() if line.strip() and not line.startswith(";")])
    if suffix == ".gpl":
        colours = {}
        for line in text.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
                name = parts[3].strip() if len(parts) == 4 else str(len(colours))
                colours[name if name not in colours else f"{name}-{len(colours)}"] = tuple(int(v) for v in parts[:3])
        return parse_palette(colours)
    raise CodeArtError(f"unsupported palette file {path.name}; use .json, .hex or .gpl")


def parse_palette(source: Any) -> Palette:
    """Read a palette from a Palette, mapping, list or .json/.hex/.gpl file path.

    Accepted shapes: {"name": colour}; {"colors": {...} | [...], "variants": {...}};
    {"palette": {...}, "variants": {...}} (PixelSpec style); palette_v1
    {"colors": [{"hex": ..., "name": ...}]}; and ["#hex", ...]. Unnamed list entries
    are named by index ("0", "1", ...). Variants may only override base colours.
    """
    if isinstance(source, Palette):
        return source
    if isinstance(source, (str, os.PathLike)):
        return _read_palette_file(Path(source))
    if isinstance(source, Mapping):
        if "colors" in source or "palette" in source:
            entries = source["colors"] if "colors" in source else source["palette"]
            raw_variants = source.get("variants") or {}
        else:
            entries, raw_variants = source, {}
    elif isinstance(source, (list, tuple)):
        entries, raw_variants = source, {}
    else:
        raise CodeArtError(f"cannot read a palette from {type(source).__name__}")
    colors = _palette_entries(entries)
    if not isinstance(raw_variants, Mapping):
        raise CodeArtError("palette variants must be a {variant: {name: colour}} mapping")
    variants = {str(name): _colour_overrides(over, colors, f"palette variant {name!r}")
                for name, over in raw_variants.items()}
    return Palette(colors, variants)


def _palette_rgb(palette: Any) -> np.ndarray:
    """Unique (n, 3) uint8 RGB rows of a palette in first-seen order (base colours of a Palette)."""
    if isinstance(palette, np.ndarray):
        rows = [tuple(int(v) for v in row[:3]) for row in palette.reshape(-1, palette.shape[-1])]
    else:
        rows = [colour[:3] for colour in parse_palette(palette).resolve().values()]
    return np.array(list(dict.fromkeys(rows)), np.uint8).reshape(-1, 3)


# ----------------------------------------------------------------------------- SVG compile and lint

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)

COLOUR_PROPERTIES = frozenset({"fill", "stroke", "stop-color", "flood-color", "lighting-color", "color"})
NUMERIC_ATTRIBUTES = frozenset({
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "fx", "fy", "fr", "width", "height",
    "points", "transform", "gradientTransform", "patternTransform", "offset", "opacity", "fill-opacity",
    "stroke-opacity", "stop-opacity", "flood-opacity", "stroke-width", "stroke-dasharray", "stroke-dashoffset",
    "stroke-miterlimit", "stdDeviation", "dx", "dy", "radius", "viewBox", "pathLength", "data-pivot",
    "data-anchor", "data-z",
})
_VAR_USE = re.compile(r"var\(\s*--([A-Za-z0-9_-]+)\s*(?:,\s*((?:[^()]|\([^()]*\))*))?\)")
_VAR_DECL = re.compile(r"--([A-Za-z0-9_-]+)\s*:\s*([^;{}]*)")
_VAR_DECL_FULL = re.compile(r"--[A-Za-z0-9_-]+\s*:[^;{}]*;?")
_STYLE_BLOCK = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.S | re.I)
_STYLE_ATTR = re.compile(r"(\sstyle\s*=\s*)(\"[^\"]*\"|'[^']*')", re.I)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_PALETTE_CLASS = re.compile(r"c-([A-Za-z0-9_-]+)")
_URL_REF = re.compile(r"url\(\s*(['\"]?)#([^'\")\s]+)\1\s*\)")
_PATH_DATA = re.compile(r"[MmZzLlHhVvCcSsQqTtAa0-9eE.,+\-\s]*")
_COMPLEX_NUMBER = re.compile(r"[\d.][jJ](?![A-Za-z])")
_NONFINITE_NUMBER = re.compile(r"(?i)(?<![a-z])[-+]?(?:nan|inf(?:inity)?)(?![a-z])")
_MAX_USE_DEPTH = 16


def _css_property(name: str) -> re.Pattern:
    """Pattern for a CSS declaration of property `name` (a regex fragment)."""
    return re.compile(r"(?:^|[;{\s])" + name + r"\s*:")


def _q(tag: str) -> str:
    return f"{{{SVG_NS}}}{tag}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _describe(element: ET.Element) -> str:
    ident = element.get("id")
    return f"<{_local(element.tag)}{' id=' + repr(ident) if ident else ''}>"


def _parse_svg(svg: str) -> ET.Element:
    if not isinstance(svg, str):
        raise CodeArtError("SVG source must be text")
    if re.search(r"<!(?:DOCTYPE|ENTITY)", svg, re.I):
        raise CodeArtError("DOCTYPE/ENTITY declarations are not allowed in code-art SVG")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise CodeArtError(f"SVG is not well-formed XML: {exc}") from None
    if root.tag != _q("svg"):
        raise CodeArtError('root element must be <svg xmlns="http://www.w3.org/2000/svg">')
    return root


def _length(value: str | None) -> float | None:
    match = re.fullmatch(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(?:px)?\s*", value or "")
    return float(match.group(1)) if match else None


def _fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _substitute_vars(text: str, values: Mapping[str, str]) -> str:
    """Replace var(--name[, fallback]) with literal values; undefined names without a fallback raise."""
    def substitute(match: re.Match) -> str:
        name, fallback = match.group(1), match.group(2)
        if name in values:
            return values[name]
        if fallback is not None and fallback.strip():
            return _substitute_vars(fallback.strip(), values)
        raise CodeArtError(f"undefined CSS variable --{name}; add it to the palette or declare a default")

    for _ in range(8):  # custom properties may be defined through other custom properties
        updated = _VAR_USE.sub(substitute, text)
        if updated == text:
            break
        text = updated
    if "var(" in text:
        raise CodeArtError(f"cannot resolve var() in {text[:60]!r}")
    return text


def _custom_properties(css: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in _VAR_DECL.finditer(_CSS_COMMENT.sub("", css))}


def _strip_custom_properties(css: str) -> str:
    css = _VAR_DECL_FULL.sub("", css)
    return re.sub(r"[^{}]+\{\s*\}", "", css)


def _css_rules(css: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Parse a flat stylesheet into (selector, [(property, value)]) pairs."""
    css = _CSS_COMMENT.sub("", css)
    if "@" in css:
        raise CodeArtError("@-rules are not supported in code-art <style> blocks")
    rules, position = [], 0
    for match in _CSS_RULE.finditer(css):
        if css[position:match.start()].strip():
            raise CodeArtError(f"cannot parse CSS near {css[position:match.start()].strip()[:40]!r}")
        position = match.end()
        declarations = []
        for part in match.group(2).split(";"):
            if not part.strip():
                continue
            if ":" not in part:
                raise CodeArtError(f"bad CSS declaration {part.strip()!r}")
            name, value = part.split(":", 1)
            declarations.append((name.strip().lower(), value.strip()))
        rules.append((" ".join(match.group(1).split()), declarations))
    if css[position:].strip():
        raise CodeArtError(f"cannot parse CSS near {css[position:].strip()[:40]!r}")
    return rules


def _same_colour(a: str, b: str) -> bool:
    try:
        return hex_to_rgba(a) == hex_to_rgba(b)
    except CodeArtError:
        return a.strip().lower() == b.strip().lower()


def inline_css_vars(svg: str, palette: Any = None) -> str:
    """Replace every var(--name) with a literal value and drop custom-property declarations.

    resvg, skia and MuPDF do not resolve var() (they paint black), so CSS variables may
    appear only in source files. Values come from the source's own --name declarations
    (later wins), overridden by `palette` colours (palette names are the property names
    without the leading --). Text-level: formatting outside the substitutions is kept.
    """
    values: dict[str, str] = {}
    for block in _STYLE_BLOCK.finditer(svg):
        values.update(_custom_properties(block.group(2)))
    for attribute in _STYLE_ATTR.finditer(svg):
        values.update(_custom_properties(attribute.group(2)[1:-1]))
    if palette is not None:
        values.update(parse_palette(palette).hex())

    def fix_css(css: str) -> str:
        return _strip_custom_properties(_substitute_vars(css, values))

    svg = _STYLE_BLOCK.sub(lambda m: m.group(1) + fix_css(m.group(2)) + m.group(3), svg)
    svg = _STYLE_ATTR.sub(lambda m: m.group(1) + m.group(2)[0] + fix_css(m.group(2)[1:-1]) + m.group(2)[0], svg)
    return _substitute_vars(svg, values)


def _compile_css(css: str, base: Mapping[str, str], effective: Mapping[str, str], palette_base: Mapping[str, str],
                 palette_effective: Mapping[str, str]) -> tuple[str, list[str], set[str]]:
    """Return (base rules, variant override rules, palette classes that have a rule)."""
    base_rules, override_rules, defined = [], [], set()
    for selector, declarations in _css_rules(css):
        match = re.fullmatch(r"\.c-([A-Za-z0-9_-]+)", selector)
        if match:
            defined.add(match.group(1))
        palette_name = match.group(1) if match and match.group(1) in palette_base else None
        kept, changed = [], []
        for name, value in declarations:
            if name.startswith("--"):
                continue
            value_base = _substitute_vars(value, base)
            value_effective = _substitute_vars(value, effective)
            if (palette_name and "var(" not in value and name in COLOUR_PROPERTIES
                    and _same_colour(value_base, palette_base[palette_name])):
                value_effective = palette_effective[palette_name]  # literal-hex class rule .c-NAME
            kept.append(f"{name}:{value_base}")
            if value_effective != value_base:
                changed.append(f"{name}:{value_effective}")
        if kept:
            base_rules.append(f"{selector}{{{';'.join(kept)}}}")
        if changed:
            override_rules.append(f"{selector}{{{';'.join(changed)}}}")
    return "".join(base_rules), override_rules, defined


def _palette_classes(root: ET.Element) -> list[str]:
    names: dict[str, None] = {}
    for element in root.iter():
        for token in (element.get("class") or "").split():
            match = _PALETTE_CLASS.fullmatch(token)
            if match:
                names.setdefault(match.group(1))
    return list(names)


def _number_problem(name: str, value: str) -> str | None:
    if _COMPLEX_NUMBER.search(value):
        return "complex number (a Python complex leaked into the SVG)"
    if _NONFINITE_NUMBER.search(value):
        return "NaN or infinity"
    if name == "d" and not _PATH_DATA.fullmatch(value):
        return "invalid character in path data"
    return None


def _number_problems(root: ET.Element) -> list[str]:
    """Messages for NaN, infinity or complex numbers in geometry and numeric style values."""
    problems = []
    for element in root.iter():
        checks = [(_local(key), value) for key, value in element.attrib.items()
                  if _local(key) == "d" or _local(key) in NUMERIC_ATTRIBUTES]
        css = element.get("style") or ""
        if _local(element.tag) == "style":
            css += ";" + (element.text or "")
        checks += [(m.group(1).lower(), m.group(2)) for m in re.finditer(r"([A-Za-z-]+)\s*:\s*([^;{}]*)", css)
                   if m.group(1).lower() in NUMERIC_ATTRIBUTES]
        for name, value in checks:
            kind = _number_problem(name, value)
            if kind:
                problems.append(f"{kind} in {_describe(element)} {name}={value[:48]!r}; "
                                "write plain finite decimals, e.g. round(float(v.real), 3)")
    return problems


def _strip_ids(element: ET.Element) -> ET.Element:
    clone = copy.deepcopy(element)
    for node in clone.iter():
        node.attrib.pop("id", None)
    return clone


def _instantiate_use(use: ET.Element, target: ET.Element) -> ET.Element:
    """The <g> a <use> stands for: use attributes, transform + translate(x y), copied content."""
    x, y = (_length(use.get(name, "0")) for name in ("x", "y"))
    if x is None or y is None:
        raise CodeArtError(f"{_describe(use)} x/y must be plain numbers")
    attributes = {key: value for key, value in use.attrib.items()
                  if _local(key) not in ("x", "y", "width", "height", "href")}
    transform = " ".join(part for part in (attributes.pop("transform", ""),
                                           f"translate({_fmt(x)} {_fmt(y)})" if x or y else "") if part)
    group = ET.Element(_q("g"), attributes)
    if transform:
        group.set("transform", transform)
    group.tail = use.tail
    if _local(target.tag) == "symbol":
        viewport = ET.SubElement(group, _q("svg"))
        for name in ("viewBox", "preserveAspectRatio"):
            if target.get(name) is not None:
                viewport.set(name, target.get(name))
        viewport.set("width", use.get("width") or target.get("width") or "100%")
        viewport.set("height", use.get("height") or target.get("height") or "100%")
        for child in target:
            viewport.append(_strip_ids(child))
    else:
        group.append(_strip_ids(target))
    return group


def _css_id_selectors(root: ET.Element) -> set[str]:
    return {name for element in root.iter() if _local(element.tag) == "style"
            for selector, _ in _css_rules(element.text or "") for name in re.findall(r"#([A-Za-z_][\w.-]*)", selector)}


def _expand_uses(root: ET.Element) -> None:
    """Replace every <use> by its content (resolves href and xlink:href; symbols keep their viewBox).

    Copies drop their ids, so content styled through a CSS #id selector cannot be expanded
    faithfully and raises instead (style reused content with classes)."""
    styled = _css_id_selectors(root)
    for _ in range(_MAX_USE_DEPTH):
        uses = [element for element in root.iter() if _local(element.tag) == "use"]
        if not uses:
            return
        parents = {child: parent for parent in root.iter() for child in parent}
        ids = {element.get("id"): element for element in root.iter() if element.get("id")}
        for use in uses:
            href = use.get("href") or use.get(f"{{{XLINK_NS}}}href") or ""
            if not href.startswith("#") or href[1:] not in ids:
                raise CodeArtError(f"{_describe(use)} must reference an element of this document (got {href!r})")
            target, node = ids[href[1:]], use
            while node is not None:
                if node is target:
                    raise CodeArtError(f"{_describe(use)} references its own ancestor {href}")
                node = parents.get(node)
            clash = sorted({element.get("id") for element in target.iter()} & styled)
            if clash:
                raise CodeArtError(f"{_describe(use)} reuses #{clash[0]}, which a CSS #id selector styles; "
                                   "style reused content with classes")
            parent = parents[use]
            index = list(parent).index(use)
            parent.remove(use)
            parent.insert(index, _instantiate_use(use, target))
    raise CodeArtError(f"<use> nesting deeper than {_MAX_USE_DEPTH} levels; is there a reference cycle?")


def _prefix_css(css: str, ids: set[str], prefix: str) -> str:
    def selector(match: re.Match) -> str:
        return f"#{prefix}{match.group(1)}" if match.group(1) in ids else match.group(0)

    rules = []
    for sel, declarations in _css_rules(css):
        body = ";".join(f"{name}:{_prefix_urls(value, ids, prefix)}" for name, value in declarations)
        rules.append(f"{re.sub(r'#([A-Za-z_][A-Za-z0-9_.-]*)', selector, sel)}{{{body}}}")
    return "".join(rules)


def _prefix_urls(value: str, ids: set[str], prefix: str) -> str:
    return _URL_REF.sub(lambda m: f"url(#{prefix}{m.group(2)})" if m.group(2) in ids else m.group(0), value)


def _prefix_ids(root: ET.Element, prefix: str) -> None:
    """Make ids unique per document: rename ids and rewrite url(#..), href and CSS #id selectors."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", prefix):
        raise CodeArtError(f"id prefix {prefix!r} must be a valid XML name start, e.g. 'f0_'")
    ids = {element.get("id") for element in root.iter() if element.get("id")}
    for element in root.iter():
        for key, value in list(element.attrib.items()):
            if key == "id":
                element.set(key, prefix + value)
            elif _local(key) == "href" and value.startswith("#") and value[1:] in ids:
                element.set(key, f"#{prefix}{value[1:]}")
            elif "url(" in value:
                element.set(key, _prefix_urls(value, ids, prefix))
        if _local(element.tag) == "style" and element.text:
            element.text = _prefix_css(element.text, ids, prefix)


def compile_svg(svg: str, palette: Any = None, variant: str | Mapping | None = None, *,
                id_prefix: str | None = None) -> str:
    """Compile an authored SVG into the portable profile.

    - var(--name) is inlined: <style> rules get the base palette, presentation and
      style attributes get the effective (variant) palette; --name declarations are dropped.
    - Palette classes: an element with class "c-NAME" and no ".c-NAME" rule gets a generated
      ".c-NAME{fill:#hex}" rule (literal hex). A variant appends one override block,
      <style data-codeart-variant="..."> at the end of the root, re-declaring only the
      rules whose colours change (proven exact in resvg and Chrome, raster tests t16/t17).
    - <use> is expanded in place (href or xlink:href; symbols keep viewBox/preserveAspectRatio).
    - NaN, infinity and complex numbers in geometry raise CodeArtError.
    - id_prefix renames every id and its url(#..), href and CSS #id references, so several
      frames can share one HTML page without clipPath/gradient id collisions.
    Lint the result with lint_portable_svg.
    """
    if variant is not None and palette is None:
        raise CodeArtError("a palette variant needs a palette")
    pal = parse_palette(palette) if palette is not None else Palette({})
    base_hex, effective_hex = pal.hex(), pal.hex(variant)
    root = _parse_svg(svg)
    declared: dict[str, str] = {}
    for element in root.iter():
        if _local(element.tag) == "style":
            declared.update(_custom_properties(element.text or ""))
        if element.get("style"):
            declared.update(_custom_properties(element.get("style")))
    base_values, effective_values = {**declared, **base_hex}, {**declared, **effective_hex}

    overrides: list[str] = []
    defined: set[str] = set()
    for element in root.iter():
        if _local(element.tag) == "style":
            element.text, extra, classes = _compile_css(element.text or "", base_values, effective_values,
                                                        base_hex, effective_hex)
            overrides += extra
            defined |= classes
        for key, value in list(element.attrib.items()):
            updated = _strip_custom_properties(value) if _local(key) == "style" else value
            if "var(" in updated:
                updated = _substitute_vars(updated, effective_values)
            if updated != value:
                if updated.strip():
                    element.set(key, updated)
                else:
                    del element.attrib[key]

    missing = [name for name in _palette_classes(root) if name not in defined]
    unknown = [name for name in missing if name not in base_hex]
    if unknown:
        raise CodeArtError(f"class c-{unknown[0]} has no palette colour and no <style> rule")
    if missing:
        generated = ET.Element(_q("style"))
        generated.text = "".join(f".c-{name}{{fill:{base_hex[name]}}}" for name in missing)
        root.insert(0, generated)
        overrides += [f".c-{name}{{fill:{effective_hex[name]}}}" for name in missing
                      if effective_hex[name] != base_hex[name]]
    if overrides:
        block = ET.SubElement(root, _q("style"))
        block.set("data-codeart-variant", variant if isinstance(variant, str) else "custom")
        block.text = "".join(overrides)

    _expand_uses(root)
    problems = _number_problems(root)
    if problems:
        raise CodeArtError(problems[0])
    if id_prefix is not None:
        _prefix_ids(root, id_prefix)
    return ET.tostring(root, encoding="unicode")


_FORBIDDEN_ELEMENTS = (
    ("text", ("text", "tspan", "textPath"), "<text> renders with renderer-specific fonts; convert the text to paths"),
    ("image", ("image",), "<image> is not portable (pixelated scaling differs between renderers); "
                          "composite raster art in Python"),
    ("feTurbulence", ("feTurbulence",), "feTurbulence noise differs between renderers; bake noise in code"),
    ("feDisplacementMap", ("feDisplacementMap",), "feDisplacementMap loses most pixels in resvg 0.48; "
                                                  "bake the displacement in code"),
    ("foreignObject", ("foreignObject",), "<foreignObject> is rendered only by browsers"),
)
_LINT_CSS = (  # (code, presentation attribute with the same meaning or None, CSS pattern, message)
    ("css-transform", None, _css_property(r"(?:-webkit-)?transform(?:-box)?"),
     "CSS transform/transform-box is ignored by non-browser renderers; use the transform attribute, "
     "e.g. rotate(a cx cy)"),
    ("transform-origin", "transform-origin", _css_property("transform-origin"),
     "transform-origin is not portable (resvg-js draws the part off-canvas); use rotate(a cx cy) or translate()"),
    ("mix-blend-mode", "mix-blend-mode", _css_property("mix-blend-mode"),
     "mix-blend-mode is renderer-dependent; blend layers in Python"),
)
_PIXEL_PROFILE = (
    ("gradient", ("linearGradient", "radialGradient"), "gradients create off-palette colours"),
    ("mask", ("mask",), "masks create partial alpha"),
    ("filter", ("filter",), "filters create partial alpha and off-palette colours"),
)


def lint_portable_svg(svg: str, profile: str = "portable") -> list[str]:
    """List constructs that render differently across resvg-py, resvg-js and Chrome.

    Each problem is one ASCII line "<code>: <message>". Profile "portable" checks the
    root viewBox (integers, equal to width/height: 1 unit = 1 logical pixel), CSS
    transform, transform-origin, var(), <text>, <image>, feTurbulence,
    feDisplacementMap, mix-blend-mode, <foreignObject>, non-finite or complex
    numbers, and hrefs that do not name an element of the document as #id (a file,
    a URL or a missing id; compile_svg refuses such <use> references, so the raw
    file fails lint where render would fail). Profile "pixel" adds what breaks
    exact palettes: a root without shape-rendering="crispEdges", gradients, masks,
    filters and opacity below 1.
    An empty list means the SVG is inside the profile.
    """
    if profile not in ("portable", "pixel"):
        raise CodeArtError(f"unknown lint profile {profile!r}; use 'portable' or 'pixel'")
    try:
        root = _parse_svg(svg)
    except CodeArtError as exc:
        return [f"xml: {exc}"]
    problems: list[str] = []
    view_box = root.get("viewBox")
    numbers = [_length(part) for part in re.split(r"[\s,]+", (view_box or "").strip())]
    if view_box is None:
        problems.append('viewbox: missing viewBox; set viewBox="0 0 W H" equal to the logical pixel canvas')
    elif len(numbers) != 4 or any(n is None or not math.isfinite(n) for n in numbers):
        problems.append(f"viewbox: viewBox must be four numbers 'min-x min-y width height', got {view_box!r}")
    elif any(n != round(n) for n in numbers):
        problems.append(f"viewbox: viewBox must use integers (1 unit = 1 logical pixel), got {view_box!r}")
    else:
        width, height = _length(root.get("width")), _length(root.get("height"))
        if (width, height) != (numbers[2], numbers[3]):
            problems.append(f"viewbox: root width/height ({root.get('width')} x {root.get('height')}) must equal "
                            f"the viewBox size ({numbers[2]:g} x {numbers[3]:g}); 1 unit = 1 logical pixel")

    elements = list(root.iter())
    for code, tags, message in _FORBIDDEN_ELEMENTS:
        found = [element for element in elements if _local(element.tag) in tags]
        if found:
            problems.append(f"{code}: {message} ({len(found)} found, first {_describe(found[0])})")
    ids = {element.get("id") for element in elements if element.get("id")}
    forbidden_tags = {tag for _, tags, _ in _FORBIDDEN_ELEMENTS for tag in tags}  # already reported above
    outside = [(element, value) for element in elements if _local(element.tag) not in forbidden_tags
               for key, value in element.attrib.items()
               if _local(key) == "href" and not (value.startswith("#") and value[1:] in ids)]
    if outside:
        element, value = outside[0]
        problems.append(f"reference: href must name an element of this document as #id; files, URLs and missing "
                        f"ids render differently or not at all ({len(outside)} found, first {_describe(element)} "
                        f"href={_ascii(value[:80])!r})")
    css = [element.text or "" for element in elements if _local(element.tag) == "style"]
    css += [element.get("style") for element in elements if element.get("style")]
    for code, attribute, pattern, message in _LINT_CSS:
        as_attribute = attribute is not None and any(element.get(attribute) is not None for element in elements)
        if as_attribute or any(pattern.search(text) for text in css):
            problems.append(f"{code}: {message}")
    if "var(" in svg:
        problems.append("var: var() resolves only in browsers; inline palette colours with compile_svg")
    problems += [f"number: {message}" for message in _number_problems(root)]

    if profile == "pixel":
        crisp = root.get("shape-rendering") == "crispEdges" or "crispedges" in (root.get("style") or "").lower()
        if not crisp:
            problems.append('crisp-edges: the pixel profile needs shape-rendering="crispEdges" on the root <svg>')
        for code, tags, message in _PIXEL_PROFILE:
            if any(_local(element.tag) in tags for element in elements) or (
                    code == "filter" and any(element.get("filter") for element in elements)):
                problems.append(f"{code}: pixel profile forbids {'/'.join(tags)}: {message}")
        opacity_names = ("opacity", "fill-opacity", "stroke-opacity")
        values = [element.get(name) for element in elements for name in opacity_names if element.get(name)]
        values += [m.group(1) for text in css
                   for m in re.finditer(r"(?:^|[;{\s])(?:fill-|stroke-)?opacity\s*:\s*([^;}]+)", text)]
        if any((_length(value) if _length(value) is not None else 1.0) < 1.0 for value in values):
            problems.append("opacity: pixel profile forbids opacity below 1 (it creates partial alpha)")
    return problems


# ----------------------------------------------------------------------------- rasterizers

def _root_size(svg: str) -> tuple[float, float]:
    """Root width/height in CSS px (falls back to the viewBox size)."""
    match = re.search(r"<svg\b([^>]*)>", svg)
    attributes = dict(re.findall(r"([\w:-]+)\s*=\s*[\"']([^\"']*)[\"']", match.group(1))) if match else {}
    width, height = _length(attributes.get("width")), _length(attributes.get("height"))
    if (width is None or height is None) and attributes.get("viewBox"):
        parts = [_length(part) for part in re.split(r"[\s,]+", attributes["viewBox"].strip())]
        if len(parts) == 4 and None not in parts:
            width = parts[2] if width is None else width
            height = parts[3] if height is None else height
    if not width or not height or width <= 0 or height <= 0:
        raise CodeArtError("the root <svg> needs a positive numeric width/height (or viewBox)")
    return width, height


def _windows_file_version(path: str) -> str | None:
    """Product version from a Windows executable's version resource (never launches it)."""
    import ctypes
    from ctypes import wintypes

    api = ctypes.windll.version
    size = api.GetFileVersionInfoSizeW(path, None)
    buffer = ctypes.create_string_buffer(size) if size else None
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not buffer or not api.GetFileVersionInfoW(path, 0, size, buffer) or \
            not api.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
    return f"{words[2] >> 16}.{words[2] & 0xFFFF}.{words[3] >> 16}.{words[3] & 0xFFFF}"


def _chrome_candidates() -> list[str]:
    if sys.platform == "win32":
        roots = [os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        tails = (r"Google\Chrome\Application\chrome.exe", r"Chromium\Application\chrome.exe",
                 r"Microsoft\Edge\Application\msedge.exe")
        return [str(Path(root) / tail) for tail in tails for root in roots if root]
    if sys.platform == "darwin":
        apps = ("Google Chrome", "Chromium", "Microsoft Edge")
        return [f"/Applications/{app}.app/Contents/MacOS/{app}" for app in apps]
    return [found for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                               "microsoft-edge", "microsoft-edge-stable") if (found := shutil.which(name))]


def _find_chrome() -> str | None:
    override = os.environ.get("CHROME_PATH")
    if override:
        return override if Path(override).is_file() else None
    return next((candidate for candidate in _chrome_candidates() if Path(candidate).is_file()), None)


def _run(command: list[str], timeout: float, label: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RasterError(f"{label} timed out after {timeout:g} s") from None
    except OSError as exc:
        raise RasterError(f"{label} could not start: {exc}") from None


@lru_cache(maxsize=None)
def _cli_version(executable: str, label: str) -> str:
    """Backend version. Chrome on Windows is read from the exe's version resource, because
    `chrome.exe --version` there hands off to a running browser session instead of printing."""
    if label == "chrome" and sys.platform == "win32":
        return _windows_file_version(executable) or "unknown"
    try:
        result = _run([executable, "--version"], 30, label)
    except RasterError:
        return "unknown"
    match = re.search(r"\d+(?:\.\d+){1,3}(?:-[\w.]+)?", result.stdout or "")
    return match.group(0) if match else "unknown"


def backend_info(name: str) -> dict:
    """Name and version of an installed rasterizer backend; RasterError when it is not available."""
    if name == "resvg_py":
        try:
            import resvg_py
        except ImportError:
            raise RasterError(f"resvg_py is not installed: {RESVG_PIP_HINT}") from None
        return {"backend": name, "name": "resvg-py", "version": str(getattr(resvg_py, "__version__", "unknown")),
                "engine": f"resvg {getattr(resvg_py, '__resvg_version__', 'unknown')}"}
    if name == "resvg_js_cli":
        executable = shutil.which("resvg-js-cli")
        if not executable:
            raise RasterError("resvg-js-cli is not on PATH (npm install -g @resvg/resvg-js-cli)")
        return {"backend": name, "name": "resvg-js-cli", "version": _cli_version(executable, name)}
    if name == "chrome":
        executable = _find_chrome()
        if not executable:
            raise RasterError("no Chrome/Edge/Chromium found (set CHROME_PATH to the browser executable)")
        return {"backend": name, "name": Path(executable).stem.lower(), "version": _cli_version(executable, name)}
    raise CodeArtError(f"unknown rasterizer backend {name!r}; use auto, {', '.join(RASTER_BACKENDS)}")


def available_backends() -> list[str]:
    """Installed rasterizer backends in auto-selection order."""
    available = []
    for name in RASTER_BACKENDS:
        try:
            backend_info(name)
        except RasterError:
            continue
        available.append(name)
    return available


def _render_resvg_py(svg: str, zoom: float, timeout: float) -> bytes:
    import resvg_py

    options: dict[str, Any] = {"svg_string": svg, "skip_system_fonts": "<text" not in svg}
    if zoom != 1:
        options["zoom"] = float(zoom)
    try:
        return bytes(resvg_py.svg_to_bytes(**options))
    except Exception as exc:  # resvg_py reports parse and size errors as plain exceptions
        raise RasterError(f"resvg_py: {exc}") from None


def _render_resvg_js_cli(svg: str, zoom: float, timeout: float) -> bytes:
    executable = shutil.which("resvg-js-cli")
    if not executable:
        raise RasterError("resvg-js-cli is not on PATH")
    with tempfile.TemporaryDirectory(prefix="codeart-resvg-js-", ignore_cleanup_errors=True) as temporary:
        source, target = Path(temporary) / "in.svg", Path(temporary) / "out.png"
        source.write_text(svg, encoding="utf-8")
        command = [executable] + (["--fit-zoom", _fmt(zoom)] if zoom != 1 else [])
        command += ([] if "<text" in svg else ["--no-system-font"]) + [str(source), str(target)]
        result = _run(command, timeout, "resvg-js-cli")
        if result.returncode != 0 or not target.is_file():
            raise RasterError(f"resvg-js-cli failed ({result.returncode}): {(result.stderr or '').strip()[-300:]}")
        return target.read_bytes()


def _render_chrome(svg: str, zoom: float, timeout: float) -> bytes:
    executable = _find_chrome()
    if not executable:
        raise RasterError("no Chrome/Edge/Chromium found")
    width, height = _root_size(svg)
    with tempfile.TemporaryDirectory(prefix="codeart-chrome-", ignore_cleanup_errors=True) as temporary:
        source, target = Path(temporary) / "in.svg", Path(temporary) / "out.png"
        source.write_text(svg, encoding="utf-8")
        command = [executable, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                   "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
                   "--disable-component-update", "--disable-sync", "--metrics-recording-only", "--mute-audio",
                   "--force-color-profile=srgb", f"--user-data-dir={Path(temporary) / 'profile'}",
                   f"--force-device-scale-factor={_fmt(zoom)}", "--default-background-color=00000000",
                   f"--window-size={round(width)},{round(height)}", f"--screenshot={target}", source.as_uri()]
        if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
            command.insert(1, "--no-sandbox")  # root inside CI containers cannot use the sandbox
        result = _run(command, timeout, "chrome")
        if result.returncode != 0 or not target.is_file():
            raise RasterError(f"chrome failed ({result.returncode}): {(result.stderr or '').strip()[-300:]}")
        return target.read_bytes()


_RENDERERS = {"resvg_py": _render_resvg_py, "resvg_js_cli": _render_resvg_js_cli, "chrome": _render_chrome}


def rasterize(svg: str, zoom: float = 1, backend: str = "auto", *, timeout: float = 120.0) -> tuple[np.ndarray, dict]:
    """Render SVG text to (H*zoom, W*zoom, 4) uint8 straight-alpha RGBA.

    backend: "auto" (first installed of resvg_py, resvg_js_cli, chrome), or one name.
    Chrome runs headless on a file:// copy with a throwaway profile. The second value
    records the renderer for codeart-meta: backend, name, version (resvg-py also gives
    the bundled resvg engine), zoom and output size. A render error never falls through
    to another backend, so results are never silently mixed between renderers.
    """
    width, height = _root_size(svg)
    if not (isinstance(zoom, (int, float)) and math.isfinite(zoom) and zoom > 0):
        raise CodeArtError(f"zoom must be a positive number, got {zoom!r}")
    out_w, out_h = width * zoom, height * zoom
    if abs(out_w - round(out_w)) > 1e-6 or abs(out_h - round(out_h)) > 1e-6:
        raise CodeArtError(f"{width:g}x{height:g} at zoom {zoom:g} is not a whole number of pixels")
    if backend == "auto":
        names = available_backends()
        if not names:
            raise RasterError(f"no SVG rasterizer available: {RESVG_PIP_HINT} "
                              "(fallbacks: resvg-js-cli on PATH, or Chrome/Edge)")
        backend = names[0]
    info = backend_info(backend)
    png = _RENDERERS[backend](svg, zoom, timeout)
    try:
        with Image.open(io.BytesIO(png)) as image:
            pixels = _as_rgba(image)
    except (OSError, SyntaxError) as exc:
        raise RasterError(f"{backend} returned an unreadable PNG: {exc}") from None
    if pixels.shape[:2] != (round(out_h), round(out_w)):
        raise RasterError(f"{backend} returned {pixels.shape[1]}x{pixels.shape[0]}, expected "
                          f"{round(out_w)}x{round(out_h)}")
    return pixels, {**info, "zoom": zoom, "size": [round(out_w), round(out_h)]}


# ----------------------------------------------------------------------------- doctor corpus

_DOCTOR_COLOURS = {"o": (0x22, 0x14, 0x2B), "s": (0xF0, 0xC0, 0x90), "h": (0xFF, 0xE0, 0xB0), "r": (0xC0, 0x30, 0x40),
                   "d": (0x80, 0x18, 0x30), "b": (0x30, 0x60, 0xC0), "w": (0xFF, 0xFF, 0xFF)}
_DOCTOR_SWAP = {"r": (0x2A, 0x80, 0x40), "d": (0x18, 0x50, 0x28)}
_DOCTOR_GRID = (
    "......oooo......", ".....osssso.....", "....oshhssso....", "....osswswso....",
    "....osssssso....", ".....osssso.....", "....oorrrroo....", "...orrrrrrddo...",
    "..osorrrrrdoso..", "..osorrrrrdoso..", "...oorrrrddoo...", "....obbbbbbo....",
    "....obbo.obbo...", "....obbo.obbo...", "...ooooo.ooooo..", "................",
)
_SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg"'
# sha256 of the RGBA bytes resvg-py 0.5.0 (resvg 0.48.1) renders for the golden cases on Windows.
# Bit-exactness is only claimed for that version and OS; elsewhere the probes below apply.
_DOCTOR_PINNED = {"resvg_py": "0.5.0", "platform": "win32", "sha256": {
    "t09@1x": "b05bbc2678d47c8c3823de9bdf2152e2daac719df1b20b1446732a5bb070c59a",
    "t10@1x": "92ddfe88a570af7ea9f2b36cee2fa41e3a532f48d10eff581008e783189bc7a7",
    "t13@1x": "789defd071a1700e6d142672fbc0681bded267635d677dfc0884d24c6ff0c550"}}
# Probe pixels (x, y, RGBA) read from the Chrome 154 reference renders, away from edges; resvg-py
# 0.5.0 agrees within 2. They catch gradients painted black, ignored clipPath/mask/pattern and
# opacity applied per element instead of per group.
_PROBE_TOLERANCE = 8
_DOCTOR_PROBES = {
    "t09": ((8, 16, (70, 35, 60, 255)), (32, 16, (192, 50, 65, 255)), (56, 16, (240, 182, 149, 255)),
            (90, 26, (145, 170, 221, 255)), (100, 20, (51, 98, 193, 255)), (127, 0, (0, 0, 0, 0)),
            (4, 36, (149, 116, 99, 255)), (8, 40, (46, 30, 49, 255)), (20, 50, (124, 95, 87, 255))),
    "t10": ((32, 32, (192, 48, 64, 255)), (35, 32, (255, 224, 176, 255)), (4, 4, (0, 0, 0, 0)),
            (96, 32, (0, 0, 0, 0)), (70, 40, (48, 96, 192, 255)), (70, 4, (48, 96, 191, 128))),
    "t13": ((2, 2, (255, 224, 176, 255)), (12, 32, (192, 48, 64, 255)), (40, 32, (48, 96, 192, 255)),
            (65, 13, (143, 121, 108, 255)), (80, 30, (143, 121, 108, 255)), (80, 50, (143, 121, 108, 255))),
}


@dataclass(frozen=True, eq=False)
class DoctorCase:
    """One rasterizer conformance case (design/raster-tests corpus, portable subset).

    kind: "exact" (pixels equal `truth`, scaled by zoom), "palette" (only `allowed_rgb`,
    no partial alpha), "golden" (pinned resvg-py hash, otherwise `probes` within
    tolerance) or "lint" (lint_portable_svg must report every code in `lint_codes`).
    """

    id: str
    title: str
    svg: str
    kind: str
    zooms: tuple[int, ...] = (1,)
    truth: np.ndarray | None = None
    allowed_rgb: tuple[tuple[int, int, int], ...] = ()
    probes: tuple[tuple[int, int, tuple[int, int, int, int]], ...] = ()
    lint_codes: tuple[str, ...] = ()


def _grid_truth(swap: bool = False) -> np.ndarray:
    colours = {**_DOCTOR_COLOURS, **(_DOCTOR_SWAP if swap else {})}
    truth = np.zeros((16, 16, 4), np.uint8)
    for y, row in enumerate(_DOCTOR_GRID):
        for x, char in enumerate(row):
            if char != ".":
                truth[y, x] = (*colours[char], 255)
    return truth


def _grid_svg(classes: bool, swap_block: bool = False) -> str:
    rects = []
    for y, row in enumerate(_DOCTOR_GRID):
        for match in re.finditer(r"([^.])\1*", row):
            x, run, char = match.start(), len(match.group(0)), match.group(1)
            paint = f'class="c-{char}" x="{x}" y="{y}" width="{run}" height="1"' if classes else \
                f'x="{x}" y="{y}" width="{run}" height="1" fill="#%02x%02x%02x"' % _DOCTOR_COLOURS[char]
            rects.append(f"<rect {paint}/>")
    def rules(colours: Mapping[str, tuple[int, int, int]]) -> str:
        return "<style>" + "".join(f".c-{k}{{fill:#%02x%02x%02x}}" % v for k, v in colours.items()) + "</style>"

    style = (rules(_DOCTOR_COLOURS) + (rules(_DOCTOR_SWAP) if swap_block else "")) if classes else ""
    return (f'{_SVG_OPEN} width="16" height="16" viewBox="0 0 16 16" shape-rendering="crispEdges">'
            f"{style}<g>{''.join(rects)}</g></svg>")


def _rect_truth(size: tuple[int, int], boxes: Sequence[tuple[int, int, int, int, tuple[int, int, int]]]) -> np.ndarray:
    truth = np.zeros((size[1], size[0], 4), np.uint8)
    for x0, y0, x1, y1, rgb in boxes:
        truth[y0:y1, x0:x1] = (*rgb, 255)
    return truth


def doctor_cases() -> list[DoctorCase]:
    """The rasterizer conformance corpus used by `svg_render.py doctor`.

    Render cases t01 (integer rects, 1x and 4x), t03 (crispEdges shapes stay on
    palette), t06A (rotate(a cx cy) pivot), t07 (<use> + <symbol>), t09 (gradients),
    t10 (clipPath, mask, pattern), t13 (group opacity), t16 (palette class rules) and
    t17 (variant override block); lint cases t06B (CSS transform), t06C
    (transform-origin) and t13B (mix-blend-mode) must be rejected by the portable lint.
    """
    t03 = (f'{_SVG_OPEN} width="32" height="32" viewBox="0 0 32 32" shape-rendering="crispEdges">'
           '<circle cx="10.5" cy="10.5" r="7" fill="#c03040" stroke="#22142b" stroke-width="1"/>'
           '<polygon points="18,30 30,30 24,16" fill="#3060c0"/>'
           '<path d="M2 28 Q 8 18 16 26" fill="none" stroke="#f0c090" stroke-width="2"/>'
           '<line x1="20" y1="2" x2="30" y2="12" stroke="#22142b" stroke-width="1"/></svg>')
    rig = f'{_SVG_OPEN} width="32" height="32" viewBox="0 0 32 32" shape-rendering="crispEdges">'
    t07 = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="32" '
           'height="16" viewBox="0 0 32 16" shape-rendering="crispEdges"><defs><g id="leaf"><rect x="0" y="0" '
           'width="4" height="4" '
           'fill="#2a8040"/><rect x="1" y="1" width="2" height="2" fill="#ffe0b0"/></g><symbol id="sym" '
           'viewBox="0 0 4 4"><rect width="4" height="4" fill="#3060c0"/></symbol></defs><use href="#leaf" x="2" '
           'y="2"/><use xlink:href="#leaf" x="10" y="2"/><use href="#leaf" transform="translate(26 2) scale(-1 1)"/>'
           '<use href="#sym" x="2" y="9" width="8" height="4"/></svg>')
    leaves = [box for ox in (2, 10, 22)
              for box in ((ox, 2, ox + 4, 6, (0x2A, 0x80, 0x40)), (ox + 1, 3, ox + 3, 5, (0xFF, 0xE0, 0xB0)))]
    t07_truth = _rect_truth((32, 16), leaves + [(4, 9, 8, 13, (0x30, 0x60, 0xC0))])
    t09 = (f'{_SVG_OPEN} width="128" height="64" viewBox="0 0 128 64"><defs><linearGradient id="lg" x1="0" y1="0" '
           'x2="1" y2="0"><stop offset="0" stop-color="#1b1f3b"/><stop offset="0.5" stop-color="#c03040"/><stop '
           'offset="1" stop-color="#ffe0b0"/></linearGradient><radialGradient id="rg" cx="0.35" cy="0.35" r="0.65" '
           'fx="0.3" fy="0.3"><stop offset="0" stop-color="#ffffff"/><stop offset="0.4" stop-color="#3060c0"/><stop '
           'offset="1" stop-color="#3060c0" stop-opacity="0"/></radialGradient><linearGradient id="rep" x1="0" y1="0" '
           'x2="8" y2="8" gradientUnits="userSpaceOnUse" spreadMethod="repeat"><stop offset="0" stop-color="#22142b"/>'
           '<stop offset="1" stop-color="#f0c090"/></linearGradient></defs><rect x="0" y="0" width="64" height="32" '
           'fill="url(#lg)"/><circle cx="96" cy="32" r="30" fill="url(#rg)"/><rect x="0" y="32" width="64" '
           'height="32" fill="url(#rep)"/></svg>')
    t10 = (f'{_SVG_OPEN} width="128" height="64" viewBox="0 0 128 64"><defs><clipPath id="cp"><circle cx="32" '
           'cy="32" r="24"/></clipPath><mask id="mk"><rect x="64" y="0" width="64" height="64" fill="#fff"/>'
           '<circle cx="96" cy="32" r="16" fill="#000"/><rect x="64" y="0" width="64" height="8" fill="#808080"/>'
           '</mask><pattern '
           'id="stripe" width="6" height="6" patternUnits="userSpaceOnUse"><rect width="3" height="6" fill="#c03040"/>'
           '<rect x="3" width="3" height="6" fill="#ffe0b0"/></pattern></defs><rect x="0" y="0" width="64" height="64" '
           'fill="url(#stripe)" clip-path="url(#cp)"/><rect x="64" y="0" width="64" height="64" fill="#3060c0" '
           'mask="url(#mk)"/></svg>')
    arm_a = ('<g id="armA" data-pivot="10 10" transform="rotate(90 10 10)"><rect x="10" y="10" width="10" '
             'height="2" fill="#c03040"/></g></svg>')
    arm_b = ('<g id="armB" style="transform-box:view-box;transform-origin:22px 10px;transform:rotate(90deg)">'
             '<rect x="22" y="10" width="10" height="2" fill="#3060c0"/></g></svg>')
    arm_c = ('<g id="armC" transform-origin="10 24" transform="rotate(-90)"><rect x="10" y="24" width="6" height="2" '
             'fill="#22142b"/></g></svg>')
    backdrop = (f'{_SVG_OPEN} width="96" height="64" viewBox="0 0 96 64">'
                '<rect x="0" y="0" width="96" height="64" fill="#ffe0b0"/>')
    circles = '<circle cx="30" cy="32" r="22" fill="#c03040"/><circle cx="50" cy="32" r="22" fill="#3060c0"/>'
    blended = ('<g style="isolation:isolate"><circle cx="30" cy="32" r="22" fill="#c03040"/><circle cx="50" cy="32" '
               'r="22" fill="#3060c0" style="mix-blend-mode:multiply"/></g>')
    opacity_group = ('<g opacity="0.5"><rect x="60" y="8" width="30" height="30" fill="#22142b"/><rect x="70" y="18" '
                     'width="20" height="40" fill="#22142b"/></g></svg>')
    return [
        DoctorCase("t01", "integer rects, crispEdges, 1x and 4x", _grid_svg(classes=False), "exact", (1, 4),
                   truth=_grid_truth()),
        DoctorCase("t03", "crispEdges shapes stay on palette", t03, "palette",
                   allowed_rgb=((34, 20, 43), (192, 48, 64), (48, 96, 192), (240, 192, 144))),
        DoctorCase("t06A", "bone pivot via rotate(a cx cy)", rig + arm_a, "exact",
                   truth=_rect_truth((32, 32), [(8, 10, 10, 20, (0xC0, 0x30, 0x40))])),
        DoctorCase("t07", "<use> of a group and of a <symbol>", t07, "exact", truth=t07_truth),
        DoctorCase("t09", "linear, radial and repeating gradients", t09, "golden", probes=_DOCTOR_PROBES["t09"]),
        DoctorCase("t10", "clipPath, luminance mask and pattern", t10, "golden", probes=_DOCTOR_PROBES["t10"]),
        DoctorCase("t13", "group opacity composited once", backdrop + f"<g>{circles}</g>" + opacity_group, "golden",
                   probes=_DOCTOR_PROBES["t13"]),
        DoctorCase("t16", "palette as class rules with literal hex", _grid_svg(classes=True), "exact",
                   truth=_grid_truth()),
        DoctorCase("t17", "palette variant as a later override <style>", _grid_svg(classes=True, swap_block=True),
                   "exact", truth=_grid_truth(swap=True)),
        DoctorCase("t06B", "CSS transform on a bone", rig + arm_b, "lint",
                   lint_codes=("css-transform", "transform-origin")),
        DoctorCase("t06C", "transform-origin attribute", rig + arm_c, "lint", lint_codes=("transform-origin",)),
        DoctorCase("t13B", "mix-blend-mode", backdrop + blended + opacity_group, "lint",
                   lint_codes=("mix-blend-mode",)),
    ]


def rgba_sha256(rgba: np.ndarray) -> str:
    """sha256 of canonical RGBA pixel bytes (independent of PNG encoding)."""
    return hashlib.sha256(_as_rgba(rgba).tobytes()).hexdigest()


def check_doctor_case(case: DoctorCase, rgba: np.ndarray | None, zoom: int = 1, info: Mapping | None = None) -> dict:
    """Score one render of a doctor case. Returns {"status": "pass" | "fail", "method", ...metrics}."""
    if case.kind == "lint":
        codes = sorted({problem.split(":", 1)[0] for problem in lint_portable_svg(case.svg)})
        missing = [code for code in case.lint_codes if code not in codes]
        return {"status": "fail" if missing else "pass", "method": "lint", "codes": codes, "missing": missing}
    pixels = _as_rgba(rgba)
    visible = pixels[..., 3] > 0
    partial = int(np.count_nonzero(visible & (pixels[..., 3] < 255)))
    if case.kind == "exact":
        truth = np.repeat(np.repeat(case.truth, zoom, axis=0), zoom, axis=1)
        if truth.shape != pixels.shape:
            return {"status": "fail", "method": "truth", "error": f"shape {pixels.shape} != {truth.shape}"}
        mismatch = int(np.count_nonzero(np.any(truth != pixels, axis=-1)))
        return {"status": "pass" if mismatch == 0 else "fail", "method": "truth", "mismatch_px": mismatch}
    if case.kind == "palette":
        allowed = np.array([_key((*rgb, 0)) >> 8 for rgb in case.allowed_rgb], np.uint32)
        off_palette = int(np.count_nonzero(~np.isin(_pack(pixels[visible]) >> 8, allowed)))
        status = "pass" if off_palette == 0 and partial == 0 else "fail"
        return {"status": status, "method": "palette", "off_palette_px": off_palette, "partial_alpha_px": partial}
    key = f"{case.id}@{zoom}x"
    info = info or {}
    pinned = (info.get("backend") == "resvg_py" and info.get("version") == _DOCTOR_PINNED["resvg_py"]
              and sys.platform == _DOCTOR_PINNED["platform"] and key in _DOCTOR_PINNED["sha256"])
    if pinned:
        digest = rgba_sha256(pixels)
        return {"status": "pass" if digest == _DOCTOR_PINNED["sha256"][key] else "fail", "method": "sha256",
                "sha256": digest}
    worst = 0
    for x, y, expected in case.probes:
        worst = max(worst, int(np.abs(pixels[y * zoom, x * zoom].astype(int) - np.array(expected)).max()))
    return {"status": "pass" if worst <= _PROBE_TOLERANCE else "fail", "method": "probes", "max_probe_error": worst}


def run_doctor(backends: Sequence[str] | None = None) -> dict:
    """Render the doctor corpus on each requested (default: every) backend and score it.

    The primary backend is the first available one in the requested order (default: the
    auto order); the report's status is "pass" only when the primary backend passes every
    case and every lint case is rejected. With no backend available the status is "fail"
    and `hint` says what to install.
    """
    cases = doctor_cases()
    report: dict = {"cases": [case.id for case in cases], "backends": {}, "primary": None}
    for name in backends or RASTER_BACKENDS:
        try:
            renderer = backend_info(name)
        except RasterError as exc:
            report["backends"][name] = {"status": "unavailable", "reason": str(exc)}
            continue
        results = []
        for case in cases:
            for zoom in case.zooms if case.kind != "lint" else ():
                try:
                    pixels, info = rasterize(case.svg, zoom, name)
                except RasterError as exc:
                    results.append({"case": case.id, "zoom": zoom, "status": "fail", "error": str(exc)})
                    continue
                results.append({"case": case.id, "zoom": zoom, **check_doctor_case(case, pixels, zoom, info)})
        status = "pass" if all(result["status"] == "pass" for result in results) else "fail"
        report["backends"][name] = {"status": status, "renderer": renderer, "results": results}
        report["primary"] = report["primary"] or name
    report["lint"] = [{"case": case.id, **check_doctor_case(case, None)} for case in cases if case.kind == "lint"]
    lint_ok = all(result["status"] == "pass" for result in report["lint"])
    primary = report["primary"]
    report["status"] = "pass" if primary and report["backends"][primary]["status"] == "pass" and lint_ok else "fail"
    if not primary:
        report["hint"] = f"no SVG rasterizer available: {RESVG_PIP_HINT}"
    return report


# ----------------------------------------------------------------------------- pixel finishing

PIXEL_FINISH_STAGES = ("coverage", "labels", "cleanup", "shading", "inner_lines", "outline")


def add_outline(rgba: Any, color: Any, mode: str = "solid", *, selout: Mapping | None = None) -> np.ndarray:
    """Draw a 1 px exterior outline on the 4-neighbour ring of the visible silhouette.

    A 4-neighbour ring never thickens diagonals. "none" returns a copy; "solid" paints
    every ring pixel `color`; "selout" (selective outline) paints each ring pixel with
    the darkest outline colour that `selout` ({fill colour: outline colour}) assigns to
    its visible neighbours, unmapped fills falling back to `color`. Ring pixels outside
    the canvas are not drawn; leave a 1 px margin (render_pixelspec raises instead).
    """
    if mode not in OUTLINE_MODES:
        raise CodeArtError(f"outline mode must be one of {', '.join(OUTLINE_MODES)}, got {mode!r}")
    out = _as_rgba(rgba)
    if mode == "none":
        return out
    visible = out[..., 3] > 0
    ring = _ring(visible)
    default = _key(hex_to_rgba(color))
    if mode == "solid":
        keys = np.full(ring.shape, default, np.uint32)
    else:
        if not selout:
            raise CodeArtError("a selout outline needs a {fill colour: outline colour} mapping")
        pairs = sorted((_key(hex_to_rgba(fill)), _key(hex_to_rgba(line))) for fill, line in selout.items())
        table_in = np.array([pair[0] for pair in pairs], np.uint32)
        table_out = np.array([pair[1] for pair in pairs], np.uint32)
        packed = _pack(out)
        candidates = [(_neighbour(visible, dy, dx, False),
                       _lookup(_neighbour(packed, dy, dx, 0), table_in, table_out, default)) for dy, dx in _DIRS4]
        keys = _darkest_candidate(ring, candidates, default)
    out[ring] = _unpack(keys[ring])
    return out


def snap_exact(rgba: Any, palette: Any, *, alpha_threshold: int = 128, max_distance: float | None = None) -> np.ndarray:
    """Snap to exact palette colours: alpha becomes 0 or 255 at `alpha_threshold` and every
    visible pixel takes the nearest palette RGB (Euclidean). With `max_distance`, a visible
    pixel farther than that from every palette colour raises CodeArtError (a wrong colour,
    not anti-aliasing noise)."""
    source = _as_rgba(rgba)
    colours = _palette_rgb(palette).astype(np.int32)
    visible = source[..., 3] >= alpha_threshold
    out = np.zeros_like(source)
    if visible.any():
        unique, inverse = np.unique(_pack(source[visible]) >> 8, return_inverse=True)
        rgb = np.stack([(unique >> 16) & 255, (unique >> 8) & 255, unique & 255], axis=-1).astype(np.int32)
        distance2 = ((rgb[:, None, :] - colours[None, :, :]) ** 2).sum(-1)
        nearest = distance2.argmin(axis=1)
        if max_distance is not None:
            worst = float(np.sqrt(distance2.min(axis=1).max()))
            if worst > max_distance:
                raise CodeArtError(f"a visible pixel is {worst:.1f} RGB units from the palette (max {max_distance:g})")
        out[visible, :3] = colours[nearest][inverse.reshape(-1)].astype(np.uint8)
        out[visible, 3] = 255
    return out


@dataclass(frozen=True)
class Ramp:
    """Material shades light to dark plus the selective-outline colour (pixel.py Ramp)."""

    hi: str
    mid: str
    lo: str
    dark: str
    out: str

    @classmethod
    def coerce(cls, value: Any) -> "Ramp":
        """Accept a Ramp, {"mid": .., optional "hi", "lo", "dark", "out"} or [hi, mid, lo, dark, out]."""
        if isinstance(value, Ramp):
            ramp = value
        elif isinstance(value, Mapping):
            if "mid" not in value:
                raise CodeArtError("a ramp mapping needs at least 'mid'")
            lo = value.get("lo", value["mid"])
            dark = value.get("dark", lo)
            ramp = cls(value.get("hi", value["mid"]), value["mid"], lo, dark, value.get("out", dark))
        elif isinstance(value, (list, tuple)) and len(value) == 5:
            ramp = cls(*value)
        else:
            raise CodeArtError("a ramp is a Ramp, a {hi, mid, lo, dark, out} mapping or a 5-colour list")
        for colour in (ramp.hi, ramp.mid, ramp.lo, ramp.dark, ramp.out):
            hex_to_rgba(colour)
        return ramp


def _slot_coverage(alpha: Any, size: tuple[int, int], ss: int, coverage: float, name: str) -> np.ndarray:
    """Boolean (H, W) mask: pixels whose ss x ss block is at least `coverage` inside the slot."""
    width, height = size
    array = np.asarray(alpha)
    if array.ndim == 3 and array.shape[2] == 4:
        array = array[..., 3]
    if array.shape != (height * ss, width * ss):
        raise CodeArtError(f"slot {name}: coverage must be {height * ss}x{width * ss} (H x W at ss={ss}), "
                           f"got {array.shape}")
    blocks = array.reshape(height, ss, width, ss)
    if array.dtype == np.bool_:
        return blocks.sum(axis=(1, 3)) >= math.ceil(coverage * ss * ss - 1e-9)
    if array.dtype == np.uint8:
        return blocks.sum(axis=(1, 3), dtype=np.uint32) >= math.ceil(coverage * 255 * ss * ss - 1e-6)
    if np.issubdtype(array.dtype, np.floating):
        return blocks.mean(axis=(1, 3)) >= coverage - 1e-9
    raise CodeArtError(f"slot {name}: coverage must be bool, uint8 or float, got {array.dtype}")


def _opaque_key(colour: Any, label: str) -> int:
    rgba = hex_to_rgba(colour)
    if rgba[3] != 255:
        raise CodeArtError(f"{label} colour {colour!r} must be opaque (pixel art keeps alpha at 0 or 255)")
    return _key(rgba)


def _mode_of_neighbours(neighbours: list[np.ndarray]) -> np.ndarray:
    """Most frequent of the 4 neighbour labels per pixel; ties prefer the larger label."""
    stack = np.stack(neighbours)
    counts = (stack[:, None] == stack[None, :]).sum(axis=1)
    best = np.argmax(counts * 4096 + stack + 1, axis=0)
    return np.take_along_axis(stack, best[None], axis=0)[0]


def _cleanup_labels(label: np.ndarray, passes: int = 3) -> tuple[np.ndarray, dict]:
    """Merge or drop label specks (no same-label 8-neighbour) and fill 1 px holes.

    Using 8-neighbours keeps thin diagonal features; the QA orphan metric stays 4-neighbour."""
    stats = {"specks_merged": 0, "specks_removed": 0, "holes_filled": 0}
    for _ in range(passes):
        four = [_neighbour(label, dy, dx, -1) for dy, dx in _DIRS4]
        eight = four + [_neighbour(label, dy, dx, -1) for dy, dx in _DIAGONALS]
        same = sum((other == label).astype(np.uint8) for other in eight)
        speck = (label >= 0) & (same == 0)
        hole = (label < 0) & np.logical_and.reduce([other >= 0 for other in four])
        if not (speck.any() or hole.any()):
            break
        replacement = _mode_of_neighbours(four)
        stats["specks_merged"] += int(np.count_nonzero(speck & (replacement >= 0)))
        stats["specks_removed"] += int(np.count_nonzero(speck & (replacement < 0)))
        stats["holes_filled"] += int(np.count_nonzero(hole))
        label = np.where(speck | hole, replacement, label)
    return label, stats


def _run_positions(mask: np.ndarray) -> np.ndarray:
    """Relative position (pixel centres, 0..1) of each mask pixel inside its horizontal run."""
    height, width = mask.shape
    xs = np.broadcast_to(np.arange(width), (height, width))
    start = np.maximum.accumulate(np.where(mask & ~_neighbour(mask, 0, -1, False), xs, -1), axis=1)
    end = np.minimum.accumulate(np.where(mask & ~_neighbour(mask, 0, 1, False), xs, width)[:, ::-1], axis=1)[:, ::-1]
    return (xs - start + 0.5) / np.maximum(end - start + 1, 1)


def _form_tone(mask: np.ndarray, light: tuple[float, float]) -> np.ndarray:
    """Run-length form shading: 0 on the side facing `light`, 1 on the far side."""
    lx, ly = float(light[0]), float(light[1])
    weight = abs(lx) + abs(ly)
    tone = np.zeros(mask.shape)
    if lx:
        px = _run_positions(mask)
        tone += abs(lx) / weight * (px if lx < 0 else 1 - px)
    if ly:
        py = _run_positions(mask.T).T
        tone += abs(ly) / weight * (py if ly < 0 else 1 - py)
    return tone


def _pixel_perfect(line: np.ndarray) -> np.ndarray:
    """Drop L-corner pixels in two checkerboard passes so adjacent corners never both go."""
    parity = (np.indices(line.shape).sum(axis=0) & 1).astype(bool)
    for phase in (False, True):
        line = line & ~(_l_corner_mask(line) & (parity == phase))
    return line


def pixel_finish(slots: Sequence[Mapping], size: tuple[int, int], *, ss: int = 8, coverage: float = 0.5,
                 outline: str = "solid", outline_color: Any = "#1a1c2c",
                 light: tuple[float, float] | None = (-1.0, -1.0),
                 bands: tuple[float, float, float] = (0.2, 0.65, 0.88),
                 inner_lines: bool = True, stamps: Sequence | None = None) -> tuple[np.ndarray, dict]:
    """Pixel-finishing route D: per-slot supersampled coverage to clean palette pixel art.

    Stages always run in this order (outlining before cleanup reopened 18-37 outline gaps
    per frame in the design measurements):
      1. coverage: a slot covers a pixel when >= `coverage` of its ss x ss block is inside;
      2. labels: the last (front-most) covering slot owns the pixel;
      3. cleanup: label specks without a same-label 8-neighbour join their 4-neighbour
         majority (or vanish when that is the background); 1 px holes are filled;
      4. shading: ramp slots get run-length form shading toward `light` (screen direction
         of the light, (-1, -1) = top-left; None = flat mid); `bands` split the 0..1 tone
         into hi/mid/lo/dark. Flat slots use "fill". `stamps` [(x, y, colour)] (eyes,
         claws) are painted last. All colours must be opaque;
      5. inner lines: a slot pixel bordering a lower slot of another group becomes a
         contour pixel, made pixel-perfect (L-corners removed); stamps are kept;
      6. outline: 1 px exterior 4-neighbour ring; none | solid | selout (each ring pixel
         takes the darkest "out" colour of the adjacent slots).

    slots: back-to-front [{"alpha": coverage of the shape, (H*ss, W*ss) uint8 0-255, float
    0-1, bool, or an RGBA render whose alpha is used; "fill": colour | "ramp": Ramp
    value; "group": bone id for inner lines (default: own index); "name": optional}].
    Returns (rgba, report): every colour comes from fills, ramps, stamps and the outline
    colour, and alpha is 0 or 255.
    """
    if outline not in OUTLINE_MODES:
        raise CodeArtError(f"outline mode must be one of {', '.join(OUTLINE_MODES)}, got {outline!r}")
    if not slots or len(slots) > 4000:
        raise CodeArtError("pixel_finish needs between 1 and 4000 slots")
    if len(bands) != 3 or not 0 < bands[0] <= bands[1] <= bands[2] < 1:
        raise CodeArtError("bands must be three increasing tone thresholds inside (0, 1)")
    width, height = int(size[0]), int(size[1])
    outline_key = _opaque_key(outline_color, "outline")
    names = [str(slot.get("name", index)) for index, slot in enumerate(slots)]
    masks = [_slot_coverage(slot.get("alpha"), (width, height), ss, coverage, names[i]) for i, slot in enumerate(slots)]

    label = np.full((height, width), -1, np.int32)
    for index, mask in enumerate(masks):
        label[mask] = index
    label, cleanup = _cleanup_labels(label)

    shade_keys, out_keys, group_index = [], [], {}
    for index, slot in enumerate(slots):
        if ("fill" in slot) == ("ramp" in slot):
            raise CodeArtError(f"slot {names[index]}: give exactly one of 'fill' or 'ramp'")
        if "ramp" in slot:
            ramp = Ramp.coerce(slot["ramp"])
            context = f"slot {names[index]} ramp"
            shade_keys.append([_opaque_key(c, context) for c in (ramp.hi, ramp.mid, ramp.lo, ramp.dark)])
            out_keys.append(_opaque_key(ramp.out, context))
        else:
            shade_keys.append([_opaque_key(slot["fill"], f"slot {names[index]} fill")] * 4)
            out_keys.append(outline_key)
        group_index.setdefault(slot.get("group", index), len(group_index))
    slot_group = np.array([group_index[slot.get("group", i)] for i, slot in enumerate(slots)] + [-1], np.int32)
    slot_out = np.array(out_keys + [outline_key], np.uint32)

    keys = np.zeros((height, width), np.uint32)
    thresholds = np.asarray(bands, float)
    for index, mask in enumerate(masks):
        region = label == index
        if not region.any():
            continue
        if light is None or not any(light) or len(set(shade_keys[index])) == 1:
            keys[region] = shade_keys[index][1]
        else:
            shade = np.searchsorted(thresholds, _form_tone(mask, light), side="right")
            keys[region] = np.array(shade_keys[index], np.uint32)[shade[region]]
    stamped = np.zeros((height, width), bool)
    for stamp in stamps or ():
        x, y, colour = stamp
        if not (0 <= int(x) < width and 0 <= int(y) < height):
            raise CodeArtError(f"stamp ({x}, {y}) is outside the {width}x{height} canvas")
        keys[int(y), int(x)] = _opaque_key(colour, f"stamp ({x}, {y})")
        stamped[int(y), int(x)] = True

    line = np.zeros((height, width), bool)
    if inner_lines:
        own_group = slot_group[label]
        for dy, dx in _DIRS4:
            other = _neighbour(label, dy, dx, -1)
            line |= (label >= 0) & (other >= 0) & (other < label) & (slot_group[other] != own_group)
        line = _pixel_perfect(line & ~stamped)
        keys[line] = outline_key if outline != "selout" else slot_out[label[line]]

    visible = (keys & 255) > 0
    ring = _ring(visible) if outline != "none" else np.zeros_like(visible)
    if outline == "solid":
        keys[ring] = outline_key
    elif outline == "selout":
        candidates = [(_neighbour(visible, dy, dx, False), slot_out[_neighbour(label, dy, dx, -1)])
                      for dy, dx in _DIRS4]
        keys[ring] = _darkest_candidate(ring, candidates, outline_key)[ring]
    report = {"stages": list(PIXEL_FINISH_STAGES), "ss": ss, "coverage": coverage, "slots": len(slots),
              "cleanup": cleanup, "inner_line_px": int(np.count_nonzero(line)),
              "outline_px": int(np.count_nonzero(ring)), "stamped_px": int(np.count_nonzero(stamped)),
              "border_contact_px": int(np.count_nonzero(_border(visible)))}
    return _unpack(keys), report


# ----------------------------------------------------------------------------- PixelSpec

TRANSPARENT_CHARS = frozenset(". ")
_RLE_TOKEN = re.compile(r"(\d*)(\D)")


def _spec_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise CodeArtError(f"{label} must be an integer, got {value!r}")
    return int(value)


def _spec_pair(value: Any, label: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise CodeArtError(f"{label} must be [x, y]")
    return _spec_int(value[0], label), _spec_int(value[1], label)


def _rle_row(text: Any, label: str) -> str:
    if not isinstance(text, str) or not re.fullmatch(r"(?:\d*\D)*", text):
        raise CodeArtError(f"{label}: segments are run-length rows like '13.6l13.' (count, then one character)")
    row = []
    for count, char in _RLE_TOKEN.findall(text):
        if count and int(count) == 0:
            raise CodeArtError(f"{label}: zero-length run in {text!r}")
        row.append(char * int(count or 1))
    return "".join(row)


def _pixel_grid(spec: Mapping, value: Any, kind: str, label: str, depth: int = 0) -> np.ndarray:
    """Character grid (h, w) from ASCII rows, run-length segments or a pose name."""
    if depth > 4:
        raise CodeArtError(f"{label}: pose references nest too deeply")
    if isinstance(value, str) and kind == "rows":
        poses = spec.get("poses") or {}
        if value not in poses:
            raise CodeArtError(f"{label}: unknown pose {value!r}")
        pose = poses[value]
        if isinstance(pose, Mapping):
            pose_kind = "segments" if "segments" in pose else "rows"
            return _pixel_grid(spec, pose.get(pose_kind), pose_kind, f"{label} pose {value!r}", depth + 1)
        return _pixel_grid(spec, pose, "rows", f"{label} pose {value!r}", depth + 1)
    if not isinstance(value, list) or not value or not all(isinstance(row, str) for row in value):
        raise CodeArtError(f"{label}: {kind} must be a nonempty list of strings")
    rows = [_rle_row(row, label) for row in value] if kind == "segments" else value
    width = max(len(row) for row in rows)
    if width == 0:
        raise CodeArtError(f"{label}: rows are empty")
    return np.array([list(row.ljust(width, ".")) for row in rows], dtype="<U1")


def _pixelspec_colours(spec: Mapping, variant: str | Mapping | None) -> dict[str, tuple[int, int, int, int]]:
    palette = spec.get("palette")
    if not isinstance(palette, Mapping) or not palette:
        raise CodeArtError("pixelspec palette must map single characters to colours")
    for char in palette:
        if not isinstance(char, str) or len(char) != 1 or char in TRANSPARENT_CHARS:
            raise CodeArtError(f"pixelspec palette key {char!r} must be one character other than '.' or space")
    return parse_palette({"colors": dict(palette), "variants": spec.get("variants") or {}}).resolve(variant)


def _pixelspec_frame(spec: Mapping, frame: Any) -> Mapping:
    frames = spec.get("frames") or []
    if frame is None:
        return {}
    if isinstance(frame, Mapping):
        return frame
    if isinstance(frame, bool) or not isinstance(frame, (int, str)):
        raise CodeArtError("frame must be None, an index, a frame name or a frame object")
    if isinstance(frame, int):
        if not 0 <= frame < len(frames):
            raise CodeArtError(f"frame index {frame} is out of range (0..{len(frames) - 1})")
        return frames[frame]
    for candidate in frames:
        if isinstance(candidate, Mapping) and candidate.get("name") == frame:
            return candidate
    raise CodeArtError(f"unknown frame {frame!r}")


def render_pixelspec(spec: Mapping, frame: Any = None, variant: str | Mapping | None = None) -> np.ndarray:
    """Render one PixelSpec frame to (H, W, 4) uint8 straight-alpha RGBA.

    Spec (schema codeart2d.pixelspec.v1; the roadmap spelling codeart.pixelspec.v1 is
    accepted): canvas [W, H]; palette {char: colour} where '.' and space are transparent
    and a colour with alpha 0 erases lower layers; variants {name: {char: colour}};
    poses {name: rows | {"rows"|"segments": [...]}}; layers [{name, z, origin [x, y],
    rows (list or pose name) | segments, mirror, mirror_safe, hidden}]; frames [{name,
    dx, dy, flip_x, layers: {layer: {rows|segments, dx, dy, mirror, hidden}}}];
    outline {mode: none|solid|selout, color: char, map: {fill char: outline char}}.
    Segments are run-length rows ('13.6l13.'; digits are counts, so segment rows cannot
    use digit palette characters), recommended above 64 px wide. Layers paint in
    z order (ties keep list order) and replace pixels. flip_x mirrors every layer's
    placement; mirror_safe: false layers keep their own orientation (emblems, text).
    The outline is applied after compositing. Any pixel outside the canvas, including
    an outline that would leave it, raises CodeArtError.
    """
    schema = spec.get("schema") if isinstance(spec, Mapping) else None
    if schema not in (PIXELSPEC_SCHEMA, *PIXELSPEC_SCHEMA_ALIASES):
        raise CodeArtError(f"expected schema {PIXELSPEC_SCHEMA}, got {schema!r}")
    width, height = _spec_pair(spec.get("canvas"), "canvas")
    if width <= 0 or height <= 0:
        raise CodeArtError("canvas must be two positive integers")
    colours = _pixelspec_colours(spec, variant)
    chars = sorted(colours)
    table_chars = np.array(chars, dtype="<U1")
    table_rgba = np.array([colours[char] for char in chars], np.uint8)
    current = _pixelspec_frame(spec, frame)
    frame_name = current.get("name", frame if isinstance(frame, (int, str)) else "base")
    layers = spec.get("layers")
    if not isinstance(layers, list) or not layers or not all(isinstance(layer, Mapping) for layer in layers):
        raise CodeArtError("pixelspec layers must be a nonempty list of objects")
    layer_names = [layer.get("name") for layer in layers]
    if len(set(layer_names)) != len(layer_names) or not all(isinstance(name, str) and name for name in layer_names):
        raise CodeArtError("pixelspec layer names must be unique nonempty strings")
    overrides = current.get("layers") or {}
    unknown = [name for name in overrides if name not in layer_names]
    if unknown:
        raise CodeArtError(f"frame {frame_name!r} overrides unknown layer(s): {', '.join(map(str, unknown))}")
    flip = bool(current.get("flip_x", False))
    frame_dx, frame_dy = _spec_int(current.get("dx", 0), "frame dx"), _spec_int(current.get("dy", 0), "frame dy")

    canvas = np.zeros((height, width, 4), np.uint8)
    for layer in sorted(layers, key=lambda item: item.get("z", 0)):
        name = layer["name"]
        override = overrides.get(name) or {}
        if override.get("hidden", layer.get("hidden", False)):
            continue
        label = f"frame {frame_name!r} layer {name!r}"
        source = override if ("rows" in override or "segments" in override) else layer
        kind = "segments" if "segments" in source else "rows"
        grid = _pixel_grid(spec, source.get(kind), kind, label)
        if override.get("mirror", layer.get("mirror", False)):
            grid = grid[:, ::-1]
        origin_x, origin_y = _spec_pair(layer.get("origin", [0, 0]), f"{label} origin")
        x0 = origin_x + _spec_int(override.get("dx", 0), f"{label} dx") + frame_dx
        y0 = origin_y + _spec_int(override.get("dy", 0), f"{label} dy") + frame_dy
        if flip:
            x0 = width - (x0 + grid.shape[1])
            if layer.get("mirror_safe", True):
                grid = grid[:, ::-1]
        position = np.minimum(np.searchsorted(table_chars, grid), len(chars) - 1)
        known = table_chars[position] == grid
        transparent = np.isin(grid, list(TRANSPARENT_CHARS))
        if not np.all(known | transparent):
            missing = sorted(set(grid[~(known | transparent)].tolist()))
            raise CodeArtError(f"{label}: character(s) {', '.join(map(repr, missing))} not in the palette")
        ys, xs = np.nonzero(known)
        ys, xs = ys + y0, xs + x0
        outside = (ys < 0) | (ys >= height) | (xs < 0) | (xs >= width)
        if outside.any():
            first = int(np.argmax(outside))
            raise CodeArtError(f"{label}: pixel ({int(xs[first])}, {int(ys[first])}) overflows the "
                               f"{width}x{height} canvas")
        canvas[ys, xs] = table_rgba[position[known]]
    canvas[canvas[..., 3] == 0, :3] = 0

    outline = spec.get("outline") or {"mode": "none"}
    mode = outline.get("mode", "none")
    if mode == "none":
        return canvas
    if mode not in OUTLINE_MODES:
        raise CodeArtError(f"outline mode must be one of {', '.join(OUTLINE_MODES)}, got {mode!r}")
    edge = _border(canvas[..., 3] > 0)
    if edge.any():
        y, x = (int(v[0]) for v in np.nonzero(edge))
        raise CodeArtError(f"frame {frame_name!r}: the outline would overflow the canvas at ({x}, {y}); "
                           "leave a 1 px margin")
    if outline.get("color") not in colours:
        raise CodeArtError("outline color must be a palette character")
    selout = None
    if mode == "selout":
        mapping = outline.get("map") or {}
        bad = [char for pair in mapping.items() for char in pair if char not in colours]
        if not mapping or bad:
            raise CodeArtError("a selout outline needs map {fill char: outline char} using palette characters")
        selout = {colours[fill]: colours[line] for fill, line in mapping.items()}
    return add_outline(canvas, colours[outline["color"]], mode, selout=selout)


# ----------------------------------------------------------------------------- QA

# Pixel-art gates (plan A3-T4, B18, B19): at most this many pixels per metric, so 0 partial
# alpha, 0 off-palette, 0 outline gaps and at most 10 L-corners. write_codeart_meta checks
# qa_pixels metrics against them; a metric above its limit fails.
QA_PIXEL_GATES = (("partial_alpha", 0), ("off_palette", 0), ("outline_gaps", 0), ("l_corners", 10))


def qa_pixels(rgba: Any, palette: Any = None, outline: Any = None) -> dict:
    """Pixel-art QA metrics.

    visible; partial_alpha (0 < alpha < 255); colors (unique visible RGBA); off_palette
    (visible RGB not in `palette`, None without one); orphans (visible pixels with no
    identical 4-neighbour); l_corners (outline pixels removable for a pixel-perfect line)
    and outline_gaps (non-outline visible pixels touching transparency or the canvas
    edge), both None unless `outline` gives the outline colour or a list of them; bbox
    [x0, y0, x1, y1) of the visible pixels (None when empty). These are raw metrics, not
    a QA document: write_codeart_meta wraps them in a qaEnvelope (see QA_PIXEL_GATES).
    """
    pixels = _as_rgba(rgba)
    alpha = pixels[..., 3]
    visible = alpha > 0
    keys = _pack(pixels)
    report: dict[str, Any] = {"visible": int(np.count_nonzero(visible)),
                              "partial_alpha": int(np.count_nonzero(visible & (alpha < 255))),
                              "colors": int(np.unique(keys[visible]).size), "off_palette": None}
    if palette is not None:
        allowed = np.array([_key((*rgb, 0)) >> 8 for rgb in _palette_rgb(palette)], np.uint32)
        report["off_palette"] = int(np.count_nonzero(~np.isin(keys[visible] >> 8, allowed)))
    same = sum((_neighbour(keys, dy, dx, 0) == keys).astype(np.uint8) for dy, dx in _DIRS4)
    orphan = visible & (same == 0)
    report["orphans"] = int(np.count_nonzero(orphan))
    report["orphan_examples"] = [[int(x), int(y)] for y, x in np.argwhere(orphan)[:8]]
    report["l_corners"] = report["outline_gaps"] = None
    if outline is not None:
        single = isinstance(outline, str) or (isinstance(outline, (tuple, list)) and outline
                                              and isinstance(outline[0], (int, np.integer)))
        line_rgb = np.array([_key(hex_to_rgba(c)) >> 8 for c in ([outline] if single else outline)], np.uint32)
        line = visible & np.isin(keys >> 8, line_rgb)
        touches = np.zeros_like(visible)
        for dy, dx in _DIRS4:
            touches |= ~_neighbour(visible, dy, dx, False)
        gaps = visible & ~line & touches
        report["l_corners"] = int(np.count_nonzero(_l_corner_mask(line)))
        report["outline_gaps"] = int(np.count_nonzero(gaps))
        report["gap_examples"] = [[int(x), int(y)] for y, x in np.argwhere(gaps)[:8]]
    ys, xs = np.nonzero(visible)
    report["bbox"] = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1] if xs.size else None
    return report


def detect_grid(rgba: Any, max_period: int = 24, tol: int = 24, min_score: float = 0.5) -> dict:
    """Estimate the logical-pixel lattice of an upscaled or pixel-like image.

    Colour/alpha changes larger than `tol` between neighbours are boundaries. For every
    integer period 2..max_period the best x and y phases are found; score is the share
    of boundaries on that lattice rescaled so 0 = chance (1/period) and 1 = all of them.
    Divisors of the true period score the same, so the largest period within 0.02 of the
    best wins. uniformity is the share of non-empty period x period blocks whose pixels
    all lie within `tol` of the block median. has_grid = score >= min_score. Design
    measurements: nearest x6 slime 1.0; image-model crops 0.005-0.03.
    """
    pixels = _as_rgba(rgba).astype(np.int16)
    height, width = pixels.shape[:2]
    max_period = max(2, min(int(max_period), max(height, width) // 2))
    change_x = np.abs(np.diff(pixels, axis=1)).max(axis=-1) > tol
    change_y = np.abs(np.diff(pixels, axis=0)).max(axis=-1) > tol
    counts_x, counts_y = change_x.sum(axis=0).astype(float), change_y.sum(axis=1).astype(float)
    total = counts_x.sum() + counts_y.sum()
    if total == 0:
        return {"period": 1, "phase": [0, 0], "score": 0.0, "uniformity": 1.0, "boundary_hit_rate": 0.0,
                "chance_rate": 1.0, "has_grid": False}
    positions_x, positions_y = np.arange(1, width), np.arange(1, height)
    candidates = []
    for period in range(2, max_period + 1):
        hits_x = np.bincount(positions_x % period, weights=counts_x, minlength=period)
        hits_y = np.bincount(positions_y % period, weights=counts_y, minlength=period)
        phase_x, phase_y = int(np.argmax(hits_x)), int(np.argmax(hits_y))
        rate = (hits_x[phase_x] + hits_y[phase_y]) / total
        candidates.append(((rate - 1.0 / period) / (1.0 - 1.0 / period), rate, period, phase_x, phase_y))
    top = max(candidate[0] for candidate in candidates)
    score, rate, period, phase_x, phase_y = max((c for c in candidates if c[0] >= top - 0.02),
                                                key=lambda c: (c[2], c[0]))
    rows, cols = (height - phase_y) // period, (width - phase_x) // period
    uniformity = 1.0
    if rows and cols:
        blocks = pixels[phase_y:phase_y + rows * period, phase_x:phase_x + cols * period]
        blocks = blocks.reshape(rows, period, cols, period, 4).transpose(0, 2, 1, 3, 4).reshape(rows, cols, -1, 4)
        uniform = np.abs(blocks - np.median(blocks, axis=2, keepdims=True)).max(axis=(2, 3)) <= tol
        occupied = (blocks[..., 3] > 0).any(axis=2)
        uniformity = float(uniform[occupied].mean()) if occupied.any() else float(uniform.mean())
    return {"period": int(period), "phase": [phase_x, phase_y], "score": round(float(score), 3),
            "uniformity": round(uniformity, 3), "boundary_hit_rate": round(float(rate), 3),
            "chance_rate": round(1.0 / period, 3), "has_grid": bool(score >= min_score)}


# ----------------------------------------------------------------------------- review sheet

REVIEW_PAD = 8
REVIEW_GAP = 4
REVIEW_LINE = 12
REVIEW_SWATCH = 12
REVIEW_MIN_CONTENT = 224
REVIEW_BACKGROUNDS = {"light": ((236, 236, 240), (236, 236, 240)), "dark": ((36, 38, 48), (36, 38, 48)),
                      "checker": ((214, 214, 222), (170, 170, 182))}
_REVIEW_SHEET_COLOUR = (200, 200, 206)
_ONION_PREVIOUS, _ONION_NEXT, _ANCHOR = (220, 60, 60), (60, 90, 220), (255, 0, 255)


def upscale_nearest(rgba: Any, scale: int) -> np.ndarray:
    """Integer nearest-neighbour upscale (the only resampling used for pixel-art previews)."""
    if isinstance(scale, bool) or not isinstance(scale, int) or scale < 1:
        raise CodeArtError(f"scale must be a positive integer, got {scale!r}")
    return np.repeat(np.repeat(_as_rgba(rgba), scale, axis=0), scale, axis=1)


def _background(name: str, height: int, width: int, cell: int) -> np.ndarray:
    if name not in REVIEW_BACKGROUNDS:
        raise CodeArtError(f"unknown review background {name!r}; use {', '.join(REVIEW_BACKGROUNDS)}")
    first, second = REVIEW_BACKGROUNDS[name]
    ys, xs = np.indices((height, width))
    checker = ((ys // cell + xs // cell) % 2).astype(bool)
    out = np.empty((height, width, 4), np.uint8)
    out[..., :3] = np.where(checker[..., None], second, first)
    out[..., 3] = 255
    return out


def _over(base: np.ndarray, top: np.ndarray) -> np.ndarray:
    return np.asarray(Image.alpha_composite(Image.fromarray(base), Image.fromarray(top))).copy()


def _ghost(frame: np.ndarray, colour: tuple[int, int, int]) -> np.ndarray:
    out = np.zeros_like(frame)
    out[..., :3] = colour
    out[..., 3] = (frame[..., 3].astype(np.uint16) * 35 // 100).astype(np.uint8)
    return out


def _qa_lines(qa: Any) -> list[str]:
    if qa is None:
        return []
    if isinstance(qa, Mapping):
        lines = []
        for key, value in qa.items():
            if str(key).endswith("_examples"):
                continue
            if isinstance(value, Mapping):
                value = ", ".join(f"{k}={v}" for k, v in value.items() if isinstance(v, (int, float, str, bool)))
            elif isinstance(value, (list, tuple)):
                value = ", ".join(map(str, value))[:96]
            lines.append(f"{key}: {value}")
        return lines
    keys = ("partial_alpha", "off_palette", "orphans", "l_corners", "outline_gaps", "colors")
    return [f"frame {index}: " + ", ".join(f"{k}={item[k]}" for k in keys if item.get(k) is not None)
            for index, item in enumerate(qa)]


def review_sheet(frames: Any, *, scales: Sequence[int] = (1, 2, 4),
                 backgrounds: Sequence[str] = ("light", "dark", "checker"), onion: bool = True, palette: Any = None,
                 qa: Any = None, anchor: Sequence[float] | None = None, title: str = "codeart2d review",
                 loop: bool = True) -> Image.Image:
    """Contact sheet for reviewing code art at game size (RGBA Pillow image).

    Layout in px for n frames of W x H (text is ASCII and clipped, so sizes never
    depend on the font):
      width  = 2*PAD + max(MIN_CONTENT, n*W*s + (n-1)*GAP for every scale s)
      height = 2*PAD + LINE*(2 + QA lines)            title, size line, QA lines
             + per scale s and background b: LINE + H*s + GAP
             + onion block when n > 1: LINE + H*max(scales) + GAP
             + swatch block with a palette: LINE + rows*(SWATCH + GAP)
    Frames are nearest-upscaled on light, dark and checker (2 logical px squares)
    backgrounds. The onion row puts the previous frame (red) and the next (blue) behind
    each frame, wrapping when `loop`, and a magenta cross at `anchor` (pixel-edge
    coordinates). qa is a dict (one "key: value" line each) or a list of qa_pixels
    dicts (one line per frame).
    """
    frames = [_as_rgba(frame) for frame in (frames if isinstance(frames, (list, tuple)) else [frames])]
    if not frames or len({frame.shape for frame in frames}) != 1:
        raise CodeArtError("review_sheet needs one or more frames of the same size")
    if not scales or any(isinstance(s, bool) or not isinstance(s, int) or s < 1 for s in scales):
        raise CodeArtError("review scales must be positive integers")
    height, width = frames[0].shape[:2]
    count = len(frames)
    onion_scale = max(scales) if onion and count > 1 else 0
    content = max([REVIEW_MIN_CONTENT] + [count * width * s + (count - 1) * REVIEW_GAP for s in scales])
    swatches = [tuple(int(v) for v in rgb) for rgb in _palette_rgb(palette)] if palette is not None else []
    per_row = max(1, (content + REVIEW_GAP) // (REVIEW_SWATCH + REVIEW_GAP))
    text = [title, f"{count} frame(s), {width}x{height} px, nearest x{'/'.join(map(str, scales))}"] + _qa_lines(qa)
    sheet_height = (2 * REVIEW_PAD + REVIEW_LINE * len(text)
                    + sum(REVIEW_LINE + height * s + REVIEW_GAP for s in scales for _ in backgrounds)
                    + (REVIEW_LINE + height * onion_scale + REVIEW_GAP if onion_scale else 0)
                    + (REVIEW_LINE + -(-len(swatches) // per_row) * (REVIEW_SWATCH + REVIEW_GAP) if swatches else 0))
    sheet = np.empty((sheet_height, content + 2 * REVIEW_PAD, 4), np.uint8)
    sheet[..., :3], sheet[..., 3] = _REVIEW_SHEET_COLOUR, 255
    labels = [(REVIEW_PAD + REVIEW_LINE * i, line) for i, line in enumerate(text)]
    top = REVIEW_PAD + REVIEW_LINE * len(text)

    def paste(cell: np.ndarray, row_top: int, index: int, scale: int) -> None:
        left = REVIEW_PAD + index * (width * scale + REVIEW_GAP)
        sheet[row_top:row_top + cell.shape[0], left:left + cell.shape[1]] = cell

    for scale in scales:
        for name in backgrounds:
            labels.append((top, f"x{scale} {name}"))
            top += REVIEW_LINE
            for index, frame in enumerate(frames):
                backdrop = _background(name, height * scale, width * scale, 2 * scale)
                paste(_over(backdrop, upscale_nearest(frame, scale)), top, index, scale)
            top += height * scale + REVIEW_GAP
    if onion_scale:
        labels.append((top, f"x{onion_scale} onion: previous red, next blue" + (", anchor magenta" if anchor else "")))
        top += REVIEW_LINE
        for index, frame in enumerate(frames):
            cell = _background("light", height, width, 2)
            for other, colour in ((index - 1, _ONION_PREVIOUS), (index + 1, _ONION_NEXT)):
                if loop or 0 <= other < count:
                    cell = _over(cell, _ghost(frames[other % count], colour))
            cell = upscale_nearest(_over(cell, frame), onion_scale)
            if anchor is not None:
                ax, ay = (int(round(float(v) * onion_scale)) for v in anchor)
                arm = 2 * onion_scale
                cell[max(0, ay - 1):ay + 1, max(0, ax - arm):ax + arm, :3] = _ANCHOR
                cell[max(0, ay - arm):ay + arm, max(0, ax - 1):ax + 1, :3] = _ANCHOR
            paste(cell, top, index, onion_scale)
        top += height * onion_scale + REVIEW_GAP
    if swatches:
        labels.append((top, f"palette: {len(swatches)} colour(s)"))
        top += REVIEW_LINE
        for index, rgb in enumerate(swatches):
            y = top + (index // per_row) * (REVIEW_SWATCH + REVIEW_GAP)
            x = REVIEW_PAD + (index % per_row) * (REVIEW_SWATCH + REVIEW_GAP)
            sheet[y:y + REVIEW_SWATCH, x:x + REVIEW_SWATCH, :3] = rgb
    image = Image.fromarray(sheet)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for y, line in labels:
        draw.text((REVIEW_PAD, y), _ascii(line), fill=(16, 16, 24, 255), font=font)
    return image


# ----------------------------------------------------------------------------- masks

def inside_polygon(xs: Any, ys: Any, polygon: Sequence[Sequence[float]]) -> np.ndarray:
    """Even-odd point-in-polygon test, vectorised over the points (API 1.1).

    An edge counts for a point when ys lies in [min(y0, y1), max(y0, y1)) and xs lies left of
    the edge's crossing, so left and top boundaries are inside and right and bottom ones are
    not. Horizontal edges never count. Arrays keep their dtype. The one copy behind
    layout_build's terrain polygons and ambient_bake's effect regions (it was duplicated in both)."""
    xs, ys = np.asarray(xs), np.asarray(ys)
    inside = np.zeros(np.broadcast(xs, ys).shape, bool)
    count = len(polygon)
    for index in range(count):
        x0, y0 = polygon[index]
        x1, y1 = polygon[(index + 1) % count]
        if y0 == y1:
            continue
        crosses = (ys >= min(y0, y1)) & (ys < max(y0, y1))
        inside ^= crosses & (xs < x0 + (ys - y0) * (x1 - x0) / (y1 - y0))
    return inside


def _scipy_ndimage() -> Any:
    """scipy.ndimage, or None when scipy is missing or FORGE_CORE_NO_SCIPY is set (as forge_core does)."""
    if os.environ.get("FORGE_CORE_NO_SCIPY", "") not in ("", "0"):
        return None
    try:
        from scipy import ndimage
    except ImportError:
        return None
    return ndimage


def _capped_edt(target: np.ndarray, cap: float) -> np.ndarray:
    """The numpy path of distance_field (two separable passes; at most floor(cap) column shifts).

    d^2 = min over columns k of (nearest target in column k)^2 + (x - k)^2; a distance <= cap needs
    only |x - k| <= cap, so the second pass shifts at most floor(cap) columns each way."""
    rows = target.shape[0]
    limit = int(math.floor(cap))
    index = np.arange(rows, dtype=np.float64)[:, None]
    above = index - np.maximum.accumulate(np.where(target, index, -np.inf), axis=0)
    below = np.minimum.accumulate(np.where(target, index, np.inf)[::-1], axis=0)[::-1] - index
    vertical = np.minimum(above, below)
    vertical = np.where(vertical <= limit, vertical * vertical, np.inf)
    best = vertical.copy()
    for shift in range(1, min(limit, target.shape[1] - 1) + 1):
        cost = float(shift * shift)
        np.minimum(best[:, shift:], vertical[:, :-shift] + cost, out=best[:, shift:])
        np.minimum(best[:, :-shift], vertical[:, shift:] + cost, out=best[:, :-shift])
    return np.where(best <= cap * cap, np.sqrt(best), np.inf)


def distance_field(target: Any, cap: float) -> np.ndarray:
    """Exact Euclidean distance (in cells) from every cell to the nearest True cell of a 2-D mask,
    where it is at most ``cap``; infinity beyond the cap and everywhere when the mask is empty
    (API 1.1). True cells get 0.

    scipy.ndimage.distance_transform_edt when scipy is installed (FORGE_CORE_NO_SCIPY=1 forces
    the numpy path), otherwise two separable numpy passes; both give identical float64 values,
    because every distance kept is at most ``cap``. forge_core.distance_to is the capped
    Chebyshev distance; this Euclidean one serves layout_build's scatter clearances and
    ambient_bake's feather (the one copy of what both tools carried as _local_capped_edt)."""
    target = np.asarray(target, bool)
    if target.ndim != 2:
        raise CodeArtError(f"distance_field needs a 2-D mask; got shape {target.shape}")
    cap = float(cap)
    if not math.isfinite(cap) or cap < 0:
        raise CodeArtError(f"distance_field cap must be a finite number >= 0; got {cap!r}")
    if not target.any():
        return np.full(target.shape, np.inf)
    ndimage = _scipy_ndimage()
    if ndimage is None:
        return _capped_edt(target, cap)
    distance = ndimage.distance_transform_edt(~target)
    distance[distance > cap] = np.inf
    return distance


# ----------------------------------------------------------------------------- output

def save_png(image: Any, path: str | os.PathLike) -> None:
    """Write 8-bit straight-alpha RGBA PNG (RGB zeroed under alpha 0, no metadata chunks).

    The same pixels give the same bytes for a given Pillow/zlib build."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_as_rgba(image)).save(target, format="PNG", compress_level=9)


def _json_default(value: Any) -> Any:
    """numpy scalars and arrays as plain JSON values, as forge_core.write_json does."""
    if isinstance(value, (np.generic, np.ndarray)):
        return value.tolist()
    raise TypeError(f"{type(value).__name__} is not JSON data")


def _json_text(data: Any, indent: int | None = None) -> str:
    """Strict JSON: numpy values become plain JSON; NaN and infinity are refused."""
    return json.dumps(data, indent=indent, ensure_ascii=False, allow_nan=False, default=_json_default)


def _json_data(value: Any, label: str) -> Any:
    """`value` as plain JSON data: tuples become lists and numpy values Python numbers."""
    try:
        return json.loads(_json_text(value))
    except (TypeError, ValueError) as exc:
        raise CodeArtError(f"{label} must be JSON data without NaN or infinity: {_ascii(exc)}") from None


# common.schema.json (A0): qaStatus, qaCheck status, the required qaEnvelope keys, sha256, relPath, timestamp.
QA_STATUSES = ("pass", "fail", "warn", "needs-visual-review")
QA_CHECK_STATUSES = QA_STATUSES + ("skipped",)
QA_ENVELOPE_KEYS = ("status", "method", "notProven", "checks", "inputs", "outputs", "tool")
QA_PIXELS_METHOD = ("codeart_core.qa_pixels: census of the rendered 8-bit RGBA pixels (partial alpha, exact palette "
                    "lookup, 4-neighbour outline gaps and L-corners), each against its gate in "
                    "codeart_core.QA_PIXEL_GATES")
QA_PIXELS_NOT_PROVEN = (
    "Readability, silhouette and appeal at game scale; check them on the review sheet.",
    "Motion, timing and consistency between frames.",
    "That the art matches the brief and the intent of the spec.",
    "That the metrics were measured on the listed outputs: write_codeart_meta records them as given.",
)
_QA_SEVERITY = {"pass": 0, "needs-visual-review": 1, "warn": 2, "fail": 3}
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_REL_PATH = re.compile(r"(?!/)(?![A-Za-z][A-Za-z0-9+.-]*:)[^\\]+")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})")


def _check_file_ref(item: Any, label: str) -> None:
    """The common fileRef rules: {path: relPath, sha256: lowercase hex, bytes?: integer >= 0}."""
    if not isinstance(item, Mapping):
        raise CodeArtError(f"{label} must be a fileRef {{path, sha256, bytes?}}")
    path, size = item.get("path"), item.get("bytes")
    if not isinstance(path, str) or not _REL_PATH.fullmatch(path):
        raise CodeArtError(f"{label} path must be a relative POSIX path (no drive, URL scheme, leading / or "
                           f"backslash), got {ascii(path)}")
    if not isinstance(item.get("sha256"), str) or not _SHA256_HEX.fullmatch(item["sha256"]):
        raise CodeArtError(f"{label} needs a lowercase sha256 hex 'sha256'")
    if "bytes" in item and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise CodeArtError(f"{label} bytes must be a whole number >= 0")


def _file_list(items: Any, label: str) -> list:
    if isinstance(items, (str, bytes, os.PathLike, Mapping)):
        raise CodeArtError(f"{label} must be a list of file paths or fileRef dicts, not a single item")
    try:
        return list(items)
    except TypeError:
        raise CodeArtError(f"{label} must be a list of file paths or fileRef dicts") from None


def _file_ref(item: Any, base: Path, kind: str = "output") -> dict:
    """A checked fileRef: a ready dict, or a file recorded relative to `base` (POSIX) with
    sha256 and bytes. An input on another drive records its file name (common relPath)."""
    if isinstance(item, Mapping):
        _check_file_ref(item, f"an {kind} fileRef")
        return dict(item)
    file = Path(item)
    if not file.is_file():
        raise CodeArtError(f"{kind} file not found: {file}")
    try:
        relative = Path(os.path.relpath(file.resolve(), base.resolve())).as_posix()
    except ValueError:  # Windows, another drive
        if kind != "input":
            raise CodeArtError(f"{kind} file {file} is not on the drive of the meta file") from None
        relative = file.name
    ref = {"path": relative, "sha256": forge_core.sha256_file(file), "bytes": file.stat().st_size}
    _check_file_ref(ref, f"{kind} file {_ascii(file)}")
    return ref


def _check_qa_envelope(qa: Mapping) -> None:
    """The common qaEnvelope rules, raised as CodeArtError."""
    missing = [key for key in QA_ENVELOPE_KEYS if key not in qa]
    if missing:
        raise CodeArtError(f"the qa envelope lacks {', '.join(missing)}; a qaEnvelope has "
                           f"{', '.join(QA_ENVELOPE_KEYS)} and optionally createdAt")
    if qa["status"] not in QA_STATUSES:
        raise CodeArtError(f"qa status must be one of {', '.join(QA_STATUSES)}")
    if not isinstance(qa["method"], str) or not qa["method"].strip():
        raise CodeArtError("qa method must say how the outputs were judged")
    if not isinstance(qa["notProven"], list) or not all(isinstance(text, str) and text for text in qa["notProven"]):
        raise CodeArtError("qa notProven must be a list of non-empty strings")
    checks = qa["checks"]
    if not isinstance(checks, list) or not all(isinstance(check, Mapping) and isinstance(check.get("id"), str)
                                               and check["id"] and check.get("status") in QA_CHECK_STATUSES
                                               for check in checks):
        raise CodeArtError("qa checks must be a list of {id, status, value, threshold} with status one of "
                           + ", ".join(QA_CHECK_STATUSES))
    if qa["status"] == "pass" and any(check["status"] == "fail" for check in checks):
        raise CodeArtError("a pass qa envelope cannot contain a failed check")
    for key in ("inputs", "outputs"):
        if not isinstance(qa[key], list):
            raise CodeArtError(f"qa {key} must be a list of fileRefs")
        for item in qa[key]:
            _check_file_ref(item, f"a qa {key[:-1]}")
    tool = qa["tool"]
    if not isinstance(tool, Mapping) or not all(isinstance(tool.get(key), str) and tool[key]
                                                for key in ("name", "version")):
        raise CodeArtError("qa tool must give name and version strings")
    if "createdAt" in qa and not (isinstance(qa["createdAt"], str) and _TIMESTAMP.fullmatch(qa["createdAt"])):
        raise CodeArtError("qa createdAt must be an RFC 3339 date-time with an offset, e.g. 2026-10-05T04:16:00Z")


def _qa_from_metrics(metrics: dict, inputs: list, outputs: list) -> dict:
    """qa_pixels() metrics wrapped in a qaEnvelope over `outputs` (see write_codeart_meta)."""
    missing = [name for name, _ in QA_PIXEL_GATES if name not in metrics]
    if missing:
        raise CodeArtError(f"qa_pixels metrics lack {', '.join(missing)}")
    checks = []
    for name, limit in QA_PIXEL_GATES:
        value = metrics[name]
        if value is None and name != "partial_alpha":
            status = "skipped"  # qa_pixels had no palette or outline colour to measure it
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            status = "pass" if value <= limit else "fail"
        else:
            raise CodeArtError(f"qa_pixels metric {name} must be a pixel count >= 0, got {_ascii(value)}")
        checks.append({"id": name, "status": status, "value": value, "threshold": limit})
    not_proven = list(QA_PIXELS_NOT_PROVEN)
    if metrics["off_palette"] is None:
        not_proven.append("Palette conformance: qa_pixels was given no palette.")
    if metrics["outline_gaps"] is None or metrics["l_corners"] is None:
        not_proven.append("Outline continuity and pixel-perfect lines: qa_pixels was given no outline colour.")
    envelope = {"status": max((check["status"] for check in checks if check["status"] != "skipped"),
                              key=_QA_SEVERITY.__getitem__),
                "method": QA_PIXELS_METHOD, "notProven": not_proven, "checks": checks, "inputs": inputs,
                "outputs": outputs, "tool": {"name": "codeart_core", "version": FORGE_PACKAGE_VERSION},
                "metrics": metrics}
    _check_qa_envelope(envelope)
    return envelope


def _codeart_qa(qa: Any, inputs: Any, outputs: list, base: Path) -> dict:
    """codeart-meta qa: a ready qaEnvelope (checked), or qa_pixels() metrics wrapped in one."""
    data = _json_data(dict(qa), "qa") if isinstance(qa, Mapping) else None
    if data is not None and any(key in data for key in QA_ENVELOPE_KEYS):
        if inputs is not None:
            raise CodeArtError("inputs only applies to qa_pixels metrics; a ready qaEnvelope lists its own inputs")
        _check_qa_envelope(data)
        return data
    if data is not None and "partial_alpha" in data:
        input_refs = [_file_ref(item, base, "input") for item in _file_list(inputs or [], "inputs")]
        return _qa_from_metrics(data, input_refs, list(outputs))
    raise CodeArtError("qa must be a qaEnvelope (status, method, notProven, checks, inputs, outputs, tool) "
                       "or the metrics dict from qa_pixels()")


def write_codeart_meta(path: str | os.PathLike, *, generator: str, spec_sha256: str, renderer: Mapping,
                       palette: Any, outputs: Sequence, qa: Mapping, extra: Mapping | None = None,
                       inputs: Sequence | None = None, placeholder: bool = False) -> dict:
    """Write codeart-meta (codeart_meta_v1, art_source "code") and return it.

    renderer: {"name", "version", ...}, e.g. the info dict from rasterize() or
    {"name": "codeart_core.render_pixelspec", "version": CODEART_CORE_API_VERSION}.
    outputs: one or more file paths (recorded relative to the meta file, POSIX, with
    sha256 and bytes) or ready fileRef dicts. palette: anything parse_palette reads,
    written as {"colors": {name: hex}, "variants": {...}}. extra: more top-level fields;
    they may not replace core ones. placeholder (API 1.1): true when the outputs
    contain stand-in art (for example layout_build's flat-colour ground); common
    artSource keeps art_source "code" and records placeholder separately. Refuses to
    overwrite (forge_core.write_json). No timestamps, so identical inputs give
    identical bytes.

    qa is stored as a common qaEnvelope {status, method, notProven, checks, inputs,
    outputs, tool, createdAt?}:
      - a ready envelope is checked against the qaEnvelope rules and stored as given;
      - the metrics dict from qa_pixels() is wrapped in one: a check per QA_PIXEL_GATES
        entry ({id, status, value, threshold}; skipped when qa_pixels had no palette or
        outline colour for it), status = the worst measured check, method
        QA_PIXELS_METHOD, notProven QA_PIXELS_NOT_PROVEN plus the skipped measurements,
        outputs = this meta's outputs, inputs = `inputs` (the spec and other source files
        as paths or fileRefs; one on another drive records its file name), tool
        {"name": "codeart_core", "version": FORGE_PACKAGE_VERSION} (D29), and the raw
        metrics under "metrics".
    Any other qa, and `inputs` given with a ready envelope, raise CodeArtError.
    """
    target = Path(path)
    if not isinstance(placeholder, bool):
        raise CodeArtError("placeholder must be true or false")
    if not isinstance(generator, str) or not generator.strip():
        raise CodeArtError("generator must name the tool that made the art")
    if not isinstance(spec_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", spec_sha256):
        raise CodeArtError("spec_sha256 must be the lowercase sha256 hex digest of the authored spec")
    if not isinstance(renderer, Mapping) or not renderer.get("name") or not renderer.get("version"):
        raise CodeArtError("renderer must give at least name and version")
    parsed = parse_palette(palette)
    output_refs = [_file_ref(item, target.parent) for item in _file_list(outputs, "outputs")]
    if not output_refs:
        raise CodeArtError("outputs must list at least one file that the meta describes")
    meta = {
        "schema": CODEART_META_SCHEMA,
        "art_source": "code",
        "placeholder": placeholder,
        "disclosure": DISCLOSURE,
        "generator": generator,
        "spec_sha256": spec_sha256,
        "renderer": {**renderer, "name": str(renderer["name"]), "version": str(renderer["version"])},
        "palette": {"colors": parsed.hex(),
                    "variants": {name: {key: rgba_to_hex(value) for key, value in over.items()}
                                 for name, over in parsed.variants.items()}},
        "outputs": output_refs,
        "qa": _codeart_qa(qa, inputs, output_refs, target.parent),
    }
    clashes = sorted(set(extra or {}) & set(meta))
    if clashes:
        raise CodeArtError(f"extra fields may not replace core codeart-meta fields: {', '.join(clashes)}")
    meta.update(extra or {})
    meta = _json_data(meta, "codeart-meta")
    target.parent.mkdir(parents=True, exist_ok=True)
    forge_core.write_json(target, meta)
    return meta
