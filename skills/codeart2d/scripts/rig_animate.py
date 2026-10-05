"""Animate a code-art SVG rig into game frames (codeart2d, no image model).

A rig is portable SVG (references/rig-animation.md):
  root    <svg width=W height=H viewBox="0 0 W H" data-codeart="rig.v1" data-anchor="X Y"
          [data-ground="Y"] [data-facing="right|left"] [data-outline="#rrggbb"]>
  bones   <g id=NAME data-pivot="X Y">, nested parent to child, never a transform attribute
  slots   rect, circle, ellipse, line, polyline, polygon or path inside the bones; data-z sets
          the draw order (inherited from groups, default 0); data-variant=NAME marks a swap
          variant of the enclosing bone (the first variant is the default).
An animation is codeart2d.rig_anim.v1 JSON: clips of keyframed bone tracks (rotate, translate,
scale, show, swap at t in [0, 1]) eased by linear, sine, in, out, step, cubic-bezier(a,b,c,d) or
spring(k,d), plus optional two-bone IK chains planted on the ground line (gait or targets).

Sampling: a loop of n frames uses t = i/n (the last frame never repeats the first), a one-shot
t = i/(n-1). Every bone composes world = parent * T(d) * T(p) * R * S * T(-p) about its pivot p.
Slots are flattened into one world matrix each and drawn by data-z, so a front arm can cover a
front leg whatever the hierarchy. IK replaces the rotations of its chain; the ground constraint
lifts a foot until no rotated foot vertex (plus the outline allowance) is below the ground.

Routes: vector renders the flattened SVG at an integer --zoom (anti-aliased, authored strokes);
pixel runs pixel finishing route D (codeart_core.pixel_finish: 8x coverage per slot, cleanup,
optional ramp shading, inner lines between bones, a 1 px outline last) and gates every frame
at 0 partial alpha, 0 off-palette pixels, 0 outline gaps and at most 10 L-corners.

Outputs, staged and published only after QA into a new --output-dir: frames/*.png (8-bit RGBA,
identical poses stored once), clips.json (build_animation_clips input with events, stride and
entry_frame), rig-report.json (contact report, IK clamps, seam stats, per-frame QA),
codeart-meta.json (art_source code and a QA envelope over every frame), review/*.png (review
sheets with onion skins, cycle onion, world-travel strip), godot/*.json (--godot-world-height)
and compiled-clips/ (--build-clips runs the sibling generate2dsprite build_animation_clips.py by
path). Code art never goes through generate2dsprite process.

Usage, from the project root:
  python "<skill-dir>/scripts/rig_animate.py" --rig hero.rig.svg --anim hero.anim.json --output-dir out/hero-v1 --route pixel --build-clips --strict-qc
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import copy
from dataclasses import dataclass, field
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence
import xml.etree.ElementTree as ET


sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import numpy as np
    from PIL import Image, ImageDraw

    import codeart_core as core
    import forge_core
except ImportError as _import_error:  # a clean machine: --help still works and main() prints the pip command
    _MISSING: ImportError | None = _import_error
else:
    _MISSING = None


def missing_modules_message() -> str | None:
    """The pip command for missing numpy/Pillow (None when everything imported); used by main() after
    argparse has had its chance to answer --help, so --help works on a clean machine too."""
    if _MISSING is None:
        return None
    missing = [name for name in ("numpy", "PIL") if importlib.util.find_spec(name) is None]
    if not missing:
        return f"error: cannot import the codeart2d libraries ({_MISSING}); reinstall the codeart2d skill"
    packages = " ".join("Pillow" if name == "PIL" else name for name in missing)
    interpreter = sys.executable.encode("ascii", "backslashreplace").decode("ascii")
    return (f"error: missing Python module(s): {', '.join(missing)}\n"
            f"install with: python -m pip install {packages}\n"
            f"(run it with the interpreter that runs this tool: {interpreter})")


TOOL = "codeart2d/rig_animate.py"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION if _MISSING is None else "0.4.0"  # D29: the package version
RASTER_BACKENDS = ("resvg_py", "resvg_js_cli", "chrome")  # codeart_core.RASTER_BACKENDS, for --help without numpy
RIG_ANIM_SCHEMA = "codeart2d.rig_anim.v1"
RIG_ANIM_ALIASES = ("codeart.rig_anim.v1",)  # spelling of the design prototype
RIG_REPORT_SCHEMA = "codeart2d.rig_report.v1"
CLIPS_SCHEMAS = {"v1": "generate2dsprite.animation_clips.v1", "v2": "generate2dsprite.animation_clips.v2"}
EVENT_NAMES = frozenset({"in", "tell", "hit", "active_end", "cancel", "chain", "impact", "hold", "end", "sfx",
                         "step_l", "step_r"})
EASE_NAMES = ("linear", "sine", "in", "out", "step")
TRACK_KINDS = ("rotate", "translate", "scale", "show", "swap")
SEAM_RANGE = (0.8, 1.25)
DRIFT_TOLERANCE = 1e-6
CONTACT_TOLERANCE = 1e-6
DEFAULT_BUILDER = Path(__file__).resolve().parents[2] / "generate2dsprite" / "scripts" / "build_animation_clips.py"

SVG_NS = "http://www.w3.org/2000/svg"  # codeart_core.SVG_NS
if _MISSING is None:
    CodeArtError = core.CodeArtError
else:
    class CodeArtError(ValueError):  # type: ignore[no-redef]  (never raised: main() stops first)
        """Stand-in so the module imports on a clean machine."""


def _q(tag: str) -> str:
    return f"{{{SVG_NS}}}{tag}"


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def svg_number(value: float) -> str:
    """Six-decimal SVG number (deterministic, no '-0')."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


# ----------------------------------------------------------------------------- easing

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_BEZIER = re.compile(rf"cubic-bezier\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)")
_SPRING = re.compile(rf"spring\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)")


def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """CSS cubic-bezier timing function: solve x(s) = u for s and return y(s)."""
    if not (0.0 <= x1 <= 1.0 and 0.0 <= x2 <= 1.0):
        raise CodeArtError(f"cubic-bezier x1 and x2 must lie in [0, 1], got {x1:g} and {x2:g}")

    def coord(s: float, a: float, b: float) -> float:
        return ((1.0 - 3.0 * b + 3.0 * a) * s + (3.0 * b - 6.0 * a)) * s * s + 3.0 * a * s

    def slope(s: float, a: float, b: float) -> float:
        return 3.0 * (1.0 - 3.0 * b + 3.0 * a) * s * s + 2.0 * (3.0 * b - 6.0 * a) * s + 3.0 * a

    def ease(u: float) -> float:
        if u <= 0.0:
            return 0.0
        if u >= 1.0:
            return 1.0
        s = u
        for _ in range(8):  # Newton, then bisection when the slope is flat or s leaves [0, 1]
            error = coord(s, x1, x2) - u
            if abs(error) < 1e-12:
                return coord(s, y1, y2)
            gradient = slope(s, x1, x2)
            if abs(gradient) < 1e-9:
                break
            s -= error / gradient
            if not 0.0 <= s <= 1.0:
                break
        low, high = 0.0, 1.0
        s = u
        for _ in range(80):
            x = coord(s, x1, x2)
            if abs(x - u) < 1e-13:
                break
            low, high = (s, high) if x < u else (low, s)
            s = (low + high) / 2.0
        return coord(s, y1, y2)

    return ease


def spring(stiffness: float, damping: float) -> Callable[[float], float]:
    """Unit-mass spring released from 0 toward 1 (stiffness k, damping d, both > 0).

    The keyframe span covers the time the oscillation envelope needs to fall to 0.1%
    (T = ln(1000) / decay rate); the residue is spread linearly so the end is exactly 1.
    Under-damped springs (d*d < 4k) overshoot, over-damped ones approach monotonically."""
    if not (stiffness > 0.0 and damping > 0.0):
        raise CodeArtError(f"spring(k, d) needs k > 0 and d > 0, got spring({stiffness:g}, {damping:g})")
    omega = math.sqrt(stiffness)
    zeta = damping / (2.0 * omega)
    if zeta < 1.0 - 1e-9:
        damped = omega * math.sqrt(1.0 - zeta * zeta)
        decay = zeta * omega

        def position(tau: float) -> float:
            return 1.0 - math.exp(-decay * tau) * (math.cos(damped * tau) + decay / damped * math.sin(damped * tau))
    elif zeta <= 1.0 + 1e-9:
        decay = omega

        def position(tau: float) -> float:
            return 1.0 - math.exp(-omega * tau) * (1.0 + omega * tau)
    else:
        root = math.sqrt(zeta * zeta - 1.0)
        slow, fast = -omega * (zeta - root), -omega * (zeta + root)
        decay = -slow

        def position(tau: float) -> float:
            return 1.0 + (fast * math.exp(slow * tau) - slow * math.exp(fast * tau)) / (slow - fast)
    span = math.log(1000.0) / decay
    residue = 1.0 - position(span)

    def ease(u: float) -> float:
        u = min(max(u, 0.0), 1.0)
        return position(u * span) + u * residue

    return ease


_EASE_CACHE: dict[str, Callable[[float], float]] = {}


def parse_ease(spec: Any) -> Callable[[float], float]:
    """Easing function for linear, sine, in, out, step, cubic-bezier(a,b,c,d) or spring(k,d)."""
    if not isinstance(spec, str):
        raise CodeArtError(f"ease must be a string, got {spec!r}")
    text = spec.strip()
    if text in _EASE_CACHE:
        return _EASE_CACHE[text]
    if text == "linear":
        function = lambda u: u  # noqa: E731
    elif text == "sine":
        function = lambda u: 0.5 - 0.5 * math.cos(math.pi * u)  # noqa: E731
    elif text == "in":
        function = lambda u: u * u  # noqa: E731
    elif text == "out":
        function = lambda u: 1.0 - (1.0 - u) * (1.0 - u)  # noqa: E731
    elif text == "step":
        function = lambda u: 1.0 if u >= 1.0 else 0.0  # noqa: E731
    elif (match := _BEZIER.fullmatch(text)):
        function = cubic_bezier(*(float(value) for value in match.groups()))
    elif (match := _SPRING.fullmatch(text)):
        function = spring(*(float(value) for value in match.groups()))
    else:
        raise CodeArtError(f"unknown ease {spec!r}; use {', '.join(EASE_NAMES)}, cubic-bezier(a,b,c,d) or spring(k,d)")
    _EASE_CACHE[text] = function
    return function


def clip_times(frames: int, loop: bool) -> list[float]:
    """Sample times: loops t = i/n (no duplicated wrap frame), one-shots t = i/(n-1)."""
    if frames < 1:
        raise CodeArtError("a clip needs at least one frame")
    if loop:
        return [index / frames for index in range(frames)]
    return [index / (frames - 1) if frames > 1 else 0.0 for index in range(frames)]


def sample_keys(keys: Sequence[tuple[float, Any]], t: float, ease: Callable[[float], float], *,
                hold: bool = False) -> Any:
    """Value of sorted (t, value) keyframes at t. Before the first key the first value holds;
    between keys the segment is eased; `hold` (show, swap) keeps the last key's value."""
    index = bisect_right([key[0] for key in keys], t) - 1
    if index < 0:
        return keys[0][1]
    if hold or index >= len(keys) - 1:
        return keys[index][1]
    (t0, v0), (t1, v1) = keys[index], keys[index + 1]
    u = ease((t - t0) / (t1 - t0))
    if isinstance(v0, tuple):
        return tuple(a + (b - a) * u for a, b in zip(v0, v1))
    return v0 + (v1 - v0) * u


# ----------------------------------------------------------------------------- 2D affine math

def translate(dx: float, dy: float) -> np.ndarray:
    return np.array([[1.0, 0.0, dx], [0.0, 1.0, dy], [0.0, 0.0, 1.0]])


def rotate(degrees: float, cx: float = 0.0, cy: float = 0.0) -> np.ndarray:
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    return translate(cx, cy) @ np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]) @ translate(-cx, -cy)


def scale(sx: float, sy: float, cx: float = 0.0, cy: float = 0.0) -> np.ndarray:
    return translate(cx, cy) @ np.diag([sx, sy, 1.0]) @ translate(-cx, -cy)


def apply(matrix: np.ndarray, point: Sequence[float]) -> tuple[float, float]:
    x, y = float(point[0]), float(point[1])
    return (float(matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2]),
            float(matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2]))


def matrix_angle(matrix: np.ndarray) -> float:
    """Rotation of a rotation-translation matrix, in degrees."""
    return math.degrees(math.atan2(matrix[1, 0], matrix[0, 0]))


def matrix_attribute(matrix: np.ndarray) -> str:
    values = (matrix[0, 0], matrix[1, 0], matrix[0, 1], matrix[1, 1], matrix[0, 2], matrix[1, 2])
    if not all(math.isfinite(float(v)) for v in values):
        raise CodeArtError("non-finite transform (NaN or infinity) while flattening the rig")
    return "matrix(" + " ".join(svg_number(float(v)) for v in values) + ")"


_TRANSFORM_ITEM = re.compile(r"\s*(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)\s*,?")


def parse_transform(text: str | None, label: str = "transform") -> np.ndarray:
    """SVG transform attribute as a 3x3 matrix (functions apply left to right)."""
    result = np.eye(3)
    if not text or not text.strip():
        return result
    position = 0
    for match in _TRANSFORM_ITEM.finditer(text):
        if match.start() != position:
            break
        position = match.end()
        name = match.group(1)
        try:
            args = [float(value) for value in re.split(r"[\s,]+", match.group(2).strip()) if value]
        except ValueError:
            raise CodeArtError(f"{label}: bad numbers in {match.group(0).strip()!r}") from None
        if not all(math.isfinite(value) for value in args):
            raise CodeArtError(f"{label}: NaN or infinity in {match.group(0).strip()!r}")
        counts = {"matrix": (6,), "translate": (1, 2), "scale": (1, 2), "rotate": (1, 3), "skewX": (1,),
                  "skewY": (1,)}[name]
        if len(args) not in counts:
            raise CodeArtError(f"{label}: {name}() takes {' or '.join(map(str, counts))} numbers")
        if name == "matrix":
            a, b, c, d, e, f = args
            step = np.array([[a, c, e], [b, d, f], [0.0, 0.0, 1.0]])
        elif name == "translate":
            step = translate(args[0], args[1] if len(args) > 1 else 0.0)
        elif name == "scale":
            step = scale(args[0], args[1] if len(args) > 1 else args[0])
        elif name == "rotate":
            step = rotate(args[0], *(args[1:] or (0.0, 0.0)))
        elif name == "skewX":
            step = np.array([[1.0, math.tan(math.radians(args[0])), 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        else:
            step = np.array([[1.0, 0.0, 0.0], [math.tan(math.radians(args[0])), 1.0, 0.0], [0.0, 0.0, 1.0]])
        result = result @ step
    if text[position:].strip():
        raise CodeArtError(f"{label}: cannot parse {text[position:].strip()[:40]!r}")
    return result


# ----------------------------------------------------------------------------- shape geometry

TAU = 2.0 * math.pi


@dataclass
class Geometry:
    """Outline pieces of one shape in its own user space (the fill edge; strokes excluded).

    extent() is exact: straight pieces by their vertices, elliptical arcs and Bezier curves
    by their analytic extremes, so a ground contact computed here is exact, not sampled."""

    points: list[tuple[float, float]] = field(default_factory=list)
    arcs: list[tuple[float, float, float, float, float, float, float]] = field(default_factory=list)
    cubics: list[tuple[tuple[float, float], ...]] = field(default_factory=list)
    quads: list[tuple[tuple[float, float], ...]] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.points or self.arcs or self.cubics or self.quads)

    def extent(self, matrix: np.ndarray, direction: tuple[float, float]) -> float:
        """Largest direction . (matrix @ p) over the outline (-inf for an empty shape)."""
        ux, uy = direction
        gx = ux * matrix[0, 0] + uy * matrix[1, 0]
        gy = ux * matrix[0, 1] + uy * matrix[1, 1]
        offset = ux * matrix[0, 2] + uy * matrix[1, 2]
        best = -math.inf
        for x, y in self.points:
            best = max(best, gx * x + gy * y)
        for cx, cy, rx, ry, phi, start, sweep in self.arcs:
            vx = gx * math.cos(phi) + gy * math.sin(phi)
            vy = -gx * math.sin(phi) + gy * math.cos(phi)
            p, q = vx * rx, vy * ry
            centre = gx * cx + gy * cy
            radius = math.hypot(p, q)
            low, high = min(start, start + sweep), max(start, start + sweep)
            peak = math.atan2(q, p)
            if high - low >= TAU - 1e-12 or peak + TAU * math.ceil((low - peak) / TAU) <= high:
                best = max(best, centre + radius)
            else:
                best = max(best, centre + p * math.cos(low) + q * math.sin(low),
                           centre + p * math.cos(high) + q * math.sin(high))
        for p0, p1, p2, p3 in self.cubics:
            c0, c1, c2, c3 = (gx * x + gy * y for x, y in (p0, p1, p2, p3))
            d0, d1, d2 = c1 - c0, c2 - c1, c3 - c2
            candidates = [0.0, 1.0] + _unit_roots(d0 - 2.0 * d1 + d2, 2.0 * (d1 - d0), d0)
            for s in candidates:
                r = 1.0 - s
                best = max(best, r * r * r * c0 + 3.0 * r * r * s * c1 + 3.0 * r * s * s * c2 + s * s * s * c3)
        for p0, p1, p2 in self.quads:
            c0, c1, c2 = (gx * x + gy * y for x, y in (p0, p1, p2))
            candidates = [0.0, 1.0]
            denominator = c0 - 2.0 * c1 + c2
            if abs(denominator) > 1e-15 and 0.0 < (c0 - c1) / denominator < 1.0:
                candidates.append((c0 - c1) / denominator)
            for s in candidates:
                r = 1.0 - s
                best = max(best, r * r * c0 + 2.0 * r * s * c1 + s * s * c2)
        return best + offset

    def bbox(self, matrix: np.ndarray) -> tuple[float, float, float, float]:
        return (-self.extent(matrix, (-1.0, 0.0)), -self.extent(matrix, (0.0, -1.0)),
                self.extent(matrix, (1.0, 0.0)), self.extent(matrix, (0.0, 1.0)))


def _unit_roots(a: float, b: float, c: float) -> list[float]:
    """Roots of a s^2 + b s + c strictly inside (0, 1)."""
    if abs(a) < 1e-15:
        roots = [-c / b] if abs(b) > 1e-15 else []
    else:
        discriminant = b * b - 4.0 * a * c
        if discriminant < 0.0:
            return []
        root = math.sqrt(discriminant)
        roots = [(-b + root) / (2.0 * a), (-b - root) / (2.0 * a)]
    return [s for s in roots if 0.0 < s < 1.0]


def _number(element: ET.Element, name: str, default: float | None = None) -> float:
    raw = element.get(name)
    if raw is None:
        if default is None:
            raise CodeArtError(f"<{_local(element.tag)}> needs a numeric {name}")
        return default
    match = re.fullmatch(rf"\s*({_NUMBER})\s*(?:px)?\s*", raw)
    if not match:
        raise CodeArtError(f"<{_local(element.tag)}> {name}={raw!r} must be a plain number")
    value = float(match.group(1))
    if not math.isfinite(value):
        raise CodeArtError(f"<{_local(element.tag)}> {name} is NaN or infinite")
    return value


def _point_list(text: str | None, tag: str) -> list[tuple[float, float]]:
    try:
        values = [float(v) for v in re.split(r"[\s,]+", (text or "").strip()) if v]
    except ValueError:
        raise CodeArtError(f"<{tag}> points must be numbers") from None
    if len(values) % 2 or not all(math.isfinite(v) for v in values):
        raise CodeArtError(f"<{tag}> points must be finite x,y pairs")
    return list(zip(values[0::2], values[1::2]))


def _ellipse_arc(cx: float, cy: float, rx: float, ry: float, start: float = 0.0,
                 sweep: float = TAU) -> tuple[float, ...]:
    return (cx, cy, rx, ry, 0.0, start, sweep)


class _PathReader:
    """Cursor over SVG path data (numbers may be packed, arc flags may lack separators)."""

    _NUM = re.compile(_NUMBER)

    def __init__(self, data: str) -> None:
        self.data, self.index = data, 0

    def _skip(self) -> None:
        while self.index < len(self.data) and self.data[self.index] in " \t\r\n,":
            self.index += 1

    def done(self) -> bool:
        self._skip()
        return self.index >= len(self.data)

    def command(self) -> str | None:
        self._skip()
        if self.index < len(self.data) and self.data[self.index].isalpha():
            self.index += 1
            return self.data[self.index - 1]
        return None

    def number(self) -> float:
        self._skip()
        match = self._NUM.match(self.data, self.index)
        if not match:
            raise CodeArtError(f"path data: expected a number at {self.data[self.index:self.index + 12]!r}")
        self.index = match.end()
        value = float(match.group(0))
        if not math.isfinite(value):
            raise CodeArtError("path data: NaN or infinity")
        return value

    def flag(self) -> bool:
        self._skip()
        if self.index < len(self.data) and self.data[self.index] in "01":
            self.index += 1
            return self.data[self.index - 1] == "1"
        raise CodeArtError("path data: an arc flag must be 0 or 1")


def _arc_pieces(start: tuple[float, float], rx: float, ry: float, angle: float, large: bool, sweep: bool,
                end: tuple[float, float], geometry: Geometry) -> None:
    """Endpoint arc to centre parameterisation (SVG 1.1 implementation notes F.6.5)."""
    (x1, y1), (x2, y2) = start, end
    rx, ry = abs(rx), abs(ry)
    if (x1, y1) == (x2, y2):
        return
    if rx == 0.0 or ry == 0.0:
        geometry.points.append(end)
        return
    phi = math.radians(angle % 360.0)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p, y1p = cos_phi * dx + sin_phi * dy, -sin_phi * dx + cos_phi * dy
    scale_up = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
    if scale_up > 1.0:
        rx, ry = rx * math.sqrt(scale_up), ry * math.sqrt(scale_up)
    numerator = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    factor = math.sqrt(max(0.0, numerator / denominator)) * (-1.0 if large == sweep else 1.0)
    cxp, cyp = factor * rx * y1p / ry, -factor * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0
    start_angle = math.atan2((y1p - cyp) / ry, (x1p - cxp) / rx)
    end_angle = math.atan2((-y1p - cyp) / ry, (-x1p - cxp) / rx)
    delta = (end_angle - start_angle) % TAU
    if not sweep and delta > 0.0:
        delta -= TAU
    geometry.arcs.append((cx, cy, rx, ry, phi, start_angle, delta))
    geometry.points.append(end)


def path_geometry(data: str) -> Geometry:
    """Outline pieces of SVG path data (all commands, absolute and relative)."""
    geometry = Geometry()
    reader = _PathReader(data)
    current = start = (0.0, 0.0)
    cubic_control = quad_control = None
    command = None
    while not reader.done():
        letter = reader.command()
        if letter is None:
            if command is None:
                raise CodeArtError("path data must start with a command (M)")
            letter = {"M": "L", "m": "l"}.get(command, command)
        elif letter not in "MmLlHhVvCcSsQqTtAaZz":
            raise CodeArtError(f"path data: unknown command {letter!r}")
        relative = letter.islower()
        upper = letter.upper()
        base = current if relative else (0.0, 0.0)

        def point() -> tuple[float, float]:
            x, y = reader.number(), reader.number()
            return (base[0] + x, base[1] + y)

        next_cubic = next_quad = None
        if upper == "Z":
            current = start
            geometry.points.append(current)
        elif upper == "M":
            current = start = point()
            geometry.points.append(current)
        elif upper == "L":
            current = point()
            geometry.points.append(current)
        elif upper == "H":
            current = ((current[0] if relative else 0.0) + reader.number(), current[1])
            geometry.points.append(current)
        elif upper == "V":
            current = (current[0], (current[1] if relative else 0.0) + reader.number())
            geometry.points.append(current)
        elif upper in "CS":
            if upper == "C":
                first = point()
            else:
                previous = cubic_control if command and command.upper() in "CS" else None
                first = (2.0 * current[0] - previous[0], 2.0 * current[1] - previous[1]) if previous else current
            second, end = point(), point()
            geometry.cubics.append((current, first, second, end))
            next_cubic, current = second, end
            geometry.points.append(current)
        elif upper in "QT":
            if upper == "Q":
                control = point()
            else:
                previous = quad_control if command and command.upper() in "QT" else None
                control = (2.0 * current[0] - previous[0], 2.0 * current[1] - previous[1]) if previous else current
            end = point()
            geometry.quads.append((current, control, end))
            next_quad, current = control, end
            geometry.points.append(current)
        else:  # A
            rx, ry, angle = reader.number(), reader.number(), reader.number()
            large, sweep = reader.flag(), reader.flag()
            end = point()
            _arc_pieces(current, rx, ry, angle, large, sweep, end, geometry)
            current = end
        cubic_control, quad_control = next_cubic, next_quad
        command = letter
    return geometry


def shape_geometry(element: ET.Element) -> Geometry:
    """Outline geometry of rect, circle, ellipse, line, polyline, polygon or path."""
    tag = _local(element.tag)
    geometry = Geometry()
    if tag == "rect":
        x, y = _number(element, "x", 0.0), _number(element, "y", 0.0)
        width, height = _number(element, "width"), _number(element, "height")
        if width <= 0.0 or height <= 0.0:
            return geometry
        rx_raw, ry_raw = element.get("rx"), element.get("ry")
        rx = _number(element, "rx") if rx_raw is not None else None
        ry = _number(element, "ry") if ry_raw is not None else None
        rx, ry = (rx if rx is not None else ry) or 0.0, (ry if ry is not None else rx) or 0.0
        rx, ry = min(max(rx, 0.0), width / 2.0), min(max(ry, 0.0), height / 2.0)
        if rx > 0.0 and ry > 0.0:
            quarter = math.pi / 2.0
            geometry.arcs += [_ellipse_arc(x + width - rx, y + ry, rx, ry, -quarter, quarter),
                              _ellipse_arc(x + width - rx, y + height - ry, rx, ry, 0.0, quarter),
                              _ellipse_arc(x + rx, y + height - ry, rx, ry, quarter, quarter),
                              _ellipse_arc(x + rx, y + ry, rx, ry, math.pi, quarter)]
        else:
            geometry.points += [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    elif tag == "circle":
        r = _number(element, "r")
        if r > 0.0:
            geometry.arcs.append(_ellipse_arc(_number(element, "cx", 0.0), _number(element, "cy", 0.0), r, r))
    elif tag == "ellipse":
        rx, ry = _number(element, "rx"), _number(element, "ry")
        if rx > 0.0 and ry > 0.0:
            geometry.arcs.append(_ellipse_arc(_number(element, "cx", 0.0), _number(element, "cy", 0.0), rx, ry))
    elif tag == "line":
        geometry.points += [(_number(element, "x1", 0.0), _number(element, "y1", 0.0)),
                            (_number(element, "x2", 0.0), _number(element, "y2", 0.0))]
    elif tag in ("polyline", "polygon"):
        geometry.points += _point_list(element.get("points"), tag)
    elif tag == "path":
        geometry = path_geometry(element.get("d") or "")
    else:
        raise CodeArtError(f"<{tag}> is not a supported rig shape")
    return geometry


# ----------------------------------------------------------------------------- paint cascade

_INHERITED = ("fill", "stroke", "stroke-width", "fill-opacity", "stroke-opacity", "visibility", "color")
_PAINT_PROPERTIES = _INHERITED + ("opacity", "display")
_INITIAL = {"fill": "#000000", "stroke": "none", "stroke-width": "1", "fill-opacity": "1", "stroke-opacity": "1",
            "visibility": "visible", "color": "#000000", "opacity": "1", "display": "inline"}
_NAMED_COLOURS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "lime": "#00ff00", "green": "#008000",
    "blue": "#0000ff", "yellow": "#ffff00", "cyan": "#00ffff", "aqua": "#00ffff", "magenta": "#ff00ff",
    "fuchsia": "#ff00ff", "gray": "#808080", "grey": "#808080", "silver": "#c0c0c0", "maroon": "#800000",
    "olive": "#808000", "navy": "#000080", "purple": "#800080", "teal": "#008080", "orange": "#ffa500",
    "transparent": "#00000000"}
_SIMPLE_SELECTOR = re.compile(r"(\*|[A-Za-z][\w-]*)?((?:\.[\w-]+)*)")


def _declarations(css: str) -> list[tuple[str, str]]:
    pairs = []
    for part in css.split(";"):
        if ":" in part:
            name, value = part.split(":", 1)
            pairs.append((name.strip().lower(), value.replace("!important", "").strip()))
    return pairs


def _stylesheet(texts: Iterable[str]) -> list[tuple[tuple[int, int, int], str | None, frozenset, list]]:
    """Flat rules ordered by (specificity, source order): simple tag and class selectors only."""
    rules = []
    order = 0
    for css in texts:
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            declarations = _declarations(match.group(2))
            for selector in match.group(1).split(","):
                selector = selector.strip()
                parsed = _SIMPLE_SELECTOR.fullmatch(selector)
                if not selector or not parsed:
                    raise CodeArtError(f"rig <style>: unsupported selector {selector!r}; slots are flattened, so "
                                       "use simple tag or class selectors such as .c-skin or rect.c-out")
                tag = None if parsed.group(1) in (None, "*") else parsed.group(1)
                classes = frozenset(re.findall(r"\.([\w-]+)", parsed.group(2)))
                rules.append(((0, len(classes), 1 if tag else 0), order, tag, classes, declarations))
                order += 1
    rules.sort(key=lambda rule: (rule[0], rule[1]))
    return [(rule[0], rule[2], rule[3], rule[4]) for rule in rules]


def _colour(value: str, label: str) -> str | None:
    """Normalised #rrggbb[aa], None for 'none', or the url(...) text of a paint server."""
    text = value.strip()
    lowered = text.lower()
    if lowered == "none":
        return None
    if lowered.startswith("url("):
        return text
    if lowered in _NAMED_COLOURS:
        return _NAMED_COLOURS[lowered]
    match = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", lowered)
    if match:
        r, g, b = (min(255, int(v)) for v in match.groups()[:3])
        alpha = 255 if match.group(4) is None else round(min(1.0, float(match.group(4))) * 255)
        return core.rgba_to_hex((r, g, b, alpha))
    try:
        return core.rgba_to_hex(core.hex_to_rgba(text))
    except CodeArtError:
        raise CodeArtError(f"{label}: unsupported colour {value!r}; use #rrggbb") from None


def resolve_paint(chain: Sequence[ET.Element], rules: Sequence) -> dict:
    """Computed fill, stroke, stroke width, opacity and visibility of the last element of
    `chain` (root first): presentation attributes < <style> rules < style attribute, with
    CSS inheritance and currentColor. Group opacity multiplies down the chain."""
    computed = dict(_INITIAL)
    opacity, displayed = 1.0, True
    for element in chain:
        specified = {name: element.get(name).strip() for name in _PAINT_PROPERTIES if element.get(name) is not None}
        tag, classes = _local(element.tag), set((element.get("class") or "").split())
        for _, rule_tag, rule_classes, declarations in rules:
            if (rule_tag is None or rule_tag == tag) and rule_classes <= classes:
                specified.update((name, value) for name, value in declarations if name in _PAINT_PROPERTIES)
        specified.update((name, value) for name, value in _declarations(element.get("style") or "")
                         if name in _PAINT_PROPERTIES)
        updated = {}
        for name in _PAINT_PROPERTIES:
            value = specified.get(name)
            if value is None:
                updated[name] = computed[name] if name in _INHERITED else _INITIAL[name]
            else:
                updated[name] = computed[name] if value == "inherit" else value
        if updated["display"] == "none":
            displayed = False
        opacity *= _unit_number(updated["opacity"], "opacity")
        computed = updated
    label = f"<{_local(chain[-1].tag)}>"
    paints = {}
    for name in ("fill", "stroke"):
        value = computed[name]
        paints[name] = _colour(computed["color"] if value == "currentColor" else value, f"{label} {name}")
    width = re.fullmatch(rf"\s*({_NUMBER})\s*(?:px)?\s*", computed["stroke-width"])
    if not width or not math.isfinite(float(width.group(1))) or float(width.group(1)) < 0:
        raise CodeArtError(f"{label} stroke-width {computed['stroke-width']!r} must be a plain number")
    return {"fill": paints["fill"], "stroke": paints["stroke"], "stroke_width": float(width.group(1)),
            "fill_opacity": _unit_number(computed["fill-opacity"], "fill-opacity"),
            "stroke_opacity": _unit_number(computed["stroke-opacity"], "stroke-opacity"),
            "opacity": opacity, "visible": displayed and computed["visibility"] not in ("hidden", "collapse")}


def _unit_number(text: str, label: str) -> float:
    match = re.fullmatch(rf"\s*({_NUMBER})\s*(%?)\s*", text)
    if not match:
        raise CodeArtError(f"{label} {text!r} must be a number")
    value = float(match.group(1)) / (100.0 if match.group(2) else 1.0)
    return min(max(value, 0.0), 1.0)


# ----------------------------------------------------------------------------- rig model

_RESOURCE_TAGS = frozenset({"style", "defs", "clipPath", "mask", "linearGradient", "radialGradient", "pattern",
                            "filter", "symbol", "marker", "title", "desc", "metadata"})
_SHAPE_TAGS = frozenset({"rect", "circle", "ellipse", "line", "polyline", "polygon", "path"})
_GROUP_EFFECTS = ("clip-path", "mask", "filter")


@dataclass
class Bone:
    id: str
    pivot: tuple[float, float]
    parent: str | None
    variants: tuple[str, ...] = ()


@dataclass
class Slot:
    index: int
    name: str
    element: ET.Element                  # the shape (data-* removed)
    chain: list[ET.Element]              # attribute-only copies of its ancestor groups, outermost first
    bone: str | None
    local: np.ndarray                    # transforms of plain groups below the bone, then the slot's own
    z: float
    variant: tuple[str, str] | None      # (bone, variant name)
    paint: dict
    geometry: Geometry
    classes: tuple[str, ...]


def _attribute_copy(element: ET.Element) -> ET.Element:
    """A group's attributes without id and data-* (bones never carry a transform, plain groups keep theirs)."""
    attributes = {key: value for key, value in element.attrib.items() if key != "id" and not key.startswith("data-")}
    return ET.Element(element.tag, attributes)


def _numbers(text: str | None) -> list[float]:
    try:
        return [float(v) for v in re.split(r"[\s,]+", (text or "").strip()) if v]
    except ValueError:
        return []


def _pair(text: str | None, label: str) -> tuple[float, float]:
    values = _numbers(text)
    if len(values) != 2 or not all(math.isfinite(v) for v in values):
        raise CodeArtError(f"{label} must be two finite numbers 'x y', got {text!r}")
    return values[0], values[1]


def _single(text: str | None, label: str) -> float:
    values = _numbers(text)
    if len(values) != 1 or not math.isfinite(values[0]):
        raise CodeArtError(f"{label} must be one finite number, got {text!r}")
    return values[0]


class Rig:
    """A compiled SVG rig: bones (pivots, hierarchy, swap variants) and flattenable slots."""

    def __init__(self, svg_text: str, *, palette: Any = None, variant: str | None = None) -> None:
        compiled = core.compile_svg(svg_text, palette, variant)  # var(), palette classes, <use>, NaN/complex
        problems = core.lint_portable_svg(compiled)
        if problems:
            raise CodeArtError("rig is outside the portable SVG profile: " + "; ".join(problems[:3]))
        root = ET.fromstring(compiled)
        ids: dict[str, int] = {}
        for element in root.iter():
            if element.get("id"):
                ids[element.get("id")] = ids.get(element.get("id"), 0) + 1
        duplicates = sorted(name for name, count in ids.items() if count > 1)
        if duplicates:
            raise CodeArtError(f"rig: duplicate id {duplicates[0]!r} ({ids[duplicates[0]]} elements). Every id "
                               "must be unique; clipPath and gradient ids are also prefixed per frame so frames "
                               "batched in one page never share a clip")
        view_box = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox", "").strip())]
        if view_box[:2] != [0.0, 0.0]:
            raise CodeArtError('rig: the viewBox must start at "0 0" (rig coordinates are canvas pixels)')
        self.size = (int(view_box[2]), int(view_box[3]))
        if root.get("data-anchor") is None:
            raise CodeArtError('rig: the root <svg> needs data-anchor="X Y" (the root on the ground line)')
        self.anchor = _pair(root.get("data-anchor"), "rig data-anchor")
        self.ground = _single(root.get("data-ground"), "rig data-ground") \
            if root.get("data-ground") is not None else self.anchor[1]
        self.facing = (root.get("data-facing") or "right").strip()
        if self.facing not in ("right", "left"):
            raise CodeArtError("rig data-facing must be right or left")
        self.outline_hint = _colour(root.get("data-outline"), "rig data-outline") \
            if root.get("data-outline") else None
        self.root_attributes = {key: value for key, value in root.attrib.items()
                                if key not in ("width", "height", "viewBox") and not key.startswith("data-")}
        self.resources: list[ET.Element] = []
        self.bones: dict[str, Bone] = {}
        self.slots: list[Slot] = []
        self.rules = _stylesheet(element.text or "" for element in root.iter() if _local(element.tag) == "style")
        self._variants: dict[str, list[str]] = {}
        self._walk(root, bone=None, z=0.0, chain=[], ancestors=[root], local=np.eye(3), variant=None)
        for bone_id, names in self._variants.items():
            self.bones[bone_id].variants = tuple(dict.fromkeys(names))
        if not self.slots:
            raise CodeArtError("rig has no drawable shapes")

    @classmethod
    def from_file(cls, path: Path, *, palette: Any = None, variant: str | None = None) -> "Rig":
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CodeArtError(f"cannot read the rig {path}: {exc.strerror or exc}") from None
        return cls(text, palette=palette, variant=variant)

    def _walk(self, element: ET.Element, *, bone: str | None, z: float, chain: list[ET.Element],
              ancestors: list[ET.Element], local: np.ndarray, variant: tuple[str, str] | None) -> None:
        for child in element:
            tag = _local(child.tag)
            if tag in _RESOURCE_TAGS:
                self.resources.append(copy.deepcopy(child))
                continue
            child_z = z
            if child.get("data-z") is not None:
                child_z = _single(child.get("data-z"), f"data-z of <{tag}>")
            if tag == "g" and child.get("data-pivot") is not None:
                bone_id = child.get("id")
                if not bone_id:
                    raise CodeArtError("rig: every bone <g data-pivot> needs an id")
                if child.get("transform"):
                    raise CodeArtError(f"rig: bone {bone_id!r} has a transform attribute; put rest geometry in "
                                       "coordinates and motion in animation tracks")
                if child.get("data-variant") is not None:
                    raise CodeArtError(f"rig: bone {bone_id!r} has data-variant; variants are slots or plain "
                                       "groups inside a bone")
                if any(child.get(name) for name in _GROUP_EFFECTS) and \
                        any(node.get("data-pivot") is not None for node in child.iter() if node is not child):
                    raise CodeArtError(f"rig: bone {bone_id!r} has clip-path, mask or filter and child bones; "
                                       "put the effect on its slots or on a plain group of slots")
                self.bones[bone_id] = Bone(bone_id, _pair(child.get("data-pivot"), f"data-pivot of {bone_id}"), bone)
                self._walk(child, bone=bone_id, z=child_z, chain=chain + [_attribute_copy(child)],
                           ancestors=ancestors + [child], local=np.eye(3), variant=None)
            elif tag == "g":
                has_bones = any(node.get("data-pivot") is not None for node in child.iter() if node is not child)
                if child.get("transform") and has_bones:
                    raise CodeArtError("rig: a plain <g> with a transform contains bones; move the transform into "
                                       "the coordinates or the animation")
                group_variant = self._variant(child, bone, variant)
                self._walk(child, bone=bone, z=child_z,
                           chain=chain + [_attribute_copy(child)], ancestors=ancestors + [child],
                           local=local @ parse_transform(child.get("transform"), "group transform"),
                           variant=group_variant)
            elif tag in _SHAPE_TAGS:
                slot_variant = self._variant(child, bone, variant)
                element_copy = copy.deepcopy(child)
                for key in [key for key in element_copy.attrib if key.startswith("data-")]:
                    del element_copy.attrib[key]
                index = len(self.slots)
                name = child.get("id") or f"{bone or 'root'}:{tag}{index}"
                self.slots.append(Slot(
                    index=index, name=name, element=element_copy, chain=list(chain), bone=bone,
                    local=local @ parse_transform(child.get("transform"), f"transform of {name}"), z=child_z,
                    variant=slot_variant, paint=resolve_paint(ancestors + [child], self.rules),
                    geometry=shape_geometry(child), classes=tuple((child.get("class") or "").split())))
            else:
                raise CodeArtError(f"rig: <{tag}> is not supported in a rig (use g, rect, circle, ellipse, line, "
                                   "polyline, polygon or path; nested svg, symbol instances, text and images are "
                                   "outside the profile)")

    def _variant(self, element: ET.Element, bone: str | None,
                 inherited: tuple[str, str] | None) -> tuple[str, str] | None:
        name = element.get("data-variant")
        if name is None:
            return inherited
        if bone is None:
            raise CodeArtError(f"rig: data-variant={name!r} must sit inside a bone")
        if inherited is not None:
            raise CodeArtError(f"rig: data-variant={name!r} is nested inside variant {inherited[1]!r}")
        self._variants.setdefault(bone, []).append(name)
        return (bone, name)

    def ancestors(self, bone_id: str | None) -> list[str]:
        result = []
        while bone_id is not None:
            result.append(bone_id)
            bone_id = self.bones[bone_id].parent
        return result


# ----------------------------------------------------------------------------- animation model

@dataclass
class BonePose:
    rotate: float = 0.0
    translate: tuple[float, float] = (0.0, 0.0)
    scale: tuple[float, float] = (1.0, 1.0)
    show: bool = True
    swap: str | None = None


@dataclass
class Track:
    bone: str
    channels: dict[str, list[tuple[float, Any]]]
    ease: Callable[[float], float] | None


@dataclass
class IKChain:
    name: str
    root: str
    mid: str
    end: str | None
    end_point: tuple[float, float]
    pole: tuple[float, float] | None
    target: list[tuple[float, tuple[float, float]]] | None
    angle: list[tuple[float, float]] | None
    gait: dict | None
    ground: bool
    ease: Callable[[float], float]


@dataclass
class Clip:
    name: str
    frames: int
    loop: bool
    durations: list[int]
    ease: Callable[[float], float]
    stride: float | None
    events: list[dict]
    tracks: list[Track]
    ik: list[IKChain]
    grounded: bool
    entry_frame: int | None
    transitions: list[dict]
    role: str | None

    def times(self) -> list[float]:
        return clip_times(self.frames, self.loop)


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CodeArtError(f"{label} must be a finite number, got {value!r}")
    return float(value)


def _point_value(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise CodeArtError(f"{label} must be [x, y]")
    return _finite(value[0], label), _finite(value[1], label)


def _keyframes(raw: Any, label: str, convert: Callable[[Any, str], Any]) -> list[tuple[float, Any]]:
    if not isinstance(raw, list) or not raw:
        raise CodeArtError(f"{label} must be a non-empty list of [t, value] pairs")
    keys = []
    for index, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise CodeArtError(f"{label}[{index}] must be [t, value]")
        t = _finite(item[0], f"{label}[{index}] t")
        if not 0.0 <= t <= 1.0:
            raise CodeArtError(f"{label}[{index}] t must lie in [0, 1]")
        keys.append((t, convert(item[1], f"{label}[{index}] value")))
    keys.sort(key=lambda key: key[0])
    return keys


def _scale_value(value: Any, label: str) -> tuple[float, float]:
    if isinstance(value, (list, tuple)):
        return _point_value(value, label)
    uniform = _finite(value, label)
    return uniform, uniform


def _bool_value(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise CodeArtError(f"{label} must be true or false")
    return value


def _variant_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CodeArtError(f"{label} must be a variant name")
    return value


def _event_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not (value in EVENT_NAMES or re.fullmatch(r"custom:\S+", value)):
        raise CodeArtError(f"{label} {value!r} must be one of {', '.join(sorted(EVENT_NAMES))} or custom:NAME")
    return value


def _strict_json(path: Path) -> Any:
    """Strict JSON through forge_core.read_json (D28): UTF-8 with an optional BOM; NaN, infinity
    (1e999 too) and duplicate keys are refused, which Python's json alone would accept."""
    try:
        return forge_core.read_json(path, strict=True)
    except OSError as exc:
        raise CodeArtError(f"cannot read {path}: {exc.strerror or exc}") from None
    except json.JSONDecodeError as exc:
        raise CodeArtError(f"{path.name} is not valid JSON: {exc}") from None
    except ValueError as exc:  # NaN, infinity, a duplicate key, or text that is not UTF-8
        raise CodeArtError(f"{path.name}: {exc}; animation values must be finite numbers and keys unique") from None


class Animation:
    """A validated codeart2d.rig_anim.v1 document bound to its rig."""

    def __init__(self, data: Any, rig: Rig, *, label: str = "animation") -> None:
        if not isinstance(data, dict):
            raise CodeArtError(f"{label} must be a JSON object")
        if data.get("schema") not in (RIG_ANIM_SCHEMA, *RIG_ANIM_ALIASES):
            raise CodeArtError(f"{label}: schema must be {RIG_ANIM_SCHEMA!r}")
        raw_clips = data.get("clips")
        if not isinstance(raw_clips, dict) or not raw_clips:
            raise CodeArtError(f"{label}: clips must be a non-empty object")
        self.rig = rig
        clash = core.case_clash(list(raw_clips))
        if clash:  # clip names name files (frames/<clip>-NN.png, review/<clip>.png), one file on Windows and macOS
            raise CodeArtError(f"{label}: clip names {clash[0]!r} and {clash[1]!r} differ only in letter case; "
                               "they name files, so clip names must differ in more than case")
        self.clips = {name: self._clip(str(name), raw, f"clip {name}") for name, raw in raw_clips.items()}
        states = data.get("states", {})
        if not isinstance(states, dict) or not all(isinstance(v, str) and v in self.clips for v in states.values()):
            raise CodeArtError(f"{label}: states must map state names to clip names")
        self.states = dict(states)
        reference = data.get("entry_reference")
        if reference is not None and (not isinstance(reference, str) or reference not in self.clips):
            raise CodeArtError(f"{label}: entry_reference {reference!r} is not a clip name")
        for clip in self.clips.values():  # transitions name clips of this animation (as render_pixelspec checks)
            for index, item in enumerate(clip.transitions):
                target = self.clips.get(item["to"])
                if target is None:
                    raise CodeArtError(f"{label}: clip {clip.name} transitions[{index}] goes to unknown clip "
                                       f"{item['to']!r}; clips: {', '.join(self.clips)}")
                entry = item.get("entry_frame", 0)
                if isinstance(entry, bool) or not isinstance(entry, int) or not 0 <= entry < target.frames:
                    raise CodeArtError(f"{label}: clip {clip.name} transitions[{index}].entry_frame must be a frame "
                                       f"index of {item['to']!r} in [0, {target.frames})")
        self.entry_reference = reference if reference is not None else ("idle" if "idle" in self.clips else None)
        self.pixel = self._pixel_options(data.get("pixel", {}), label)

    def _pixel_options(self, raw: Any, label: str) -> dict:
        if not isinstance(raw, dict):
            raise CodeArtError(f"{label}: pixel must be an object")
        ramps = raw.get("ramps", {})
        if not isinstance(ramps, dict):
            raise CodeArtError(f"{label}: pixel.ramps must map a slot class or fill colour to a ramp")
        parsed = {}
        for key, value in ramps.items():
            ramp = core.Ramp.coerce(value)
            target = key if str(key).startswith("c-") else _colour(str(key), f"pixel.ramps key {key!r}")
            parsed[target] = ramp
        light = raw.get("light", [-1, -1])
        options = {"ramps": parsed, "light": None if light is None else _point_value(light, "pixel.light"),
                   "inner_lines": _bool_value(raw.get("inner_lines", True), "pixel.inner_lines")}
        if "outline" in raw:
            options["outline"] = raw["outline"]
        if "outline_mode" in raw:
            if raw["outline_mode"] not in ("solid", "selout", "none"):
                raise CodeArtError("pixel.outline_mode must be solid, selout or none")
            options["outline_mode"] = raw["outline_mode"]
        return options

    def _clip(self, name: str, raw: Any, label: str) -> Clip:
        if not isinstance(raw, dict):
            raise CodeArtError(f"{label} must be an object")
        frames = raw.get("frames")
        if isinstance(frames, bool) or not isinstance(frames, int) or frames < 1:
            raise CodeArtError(f"{label}: frames must be an integer >= 1")
        loop = _bool_value(raw.get("loop"), f"{label} loop")
        timing = raw.get("duration_ms")
        durations = [timing] * frames if isinstance(timing, int) and not isinstance(timing, bool) else timing
        if not isinstance(durations, list) or len(durations) != frames or not all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in durations):
            raise CodeArtError(f"{label}: duration_ms must be an integer >= 1 or one per frame ({frames})")
        ease_name = raw.get("ease", "linear")
        ease = parse_ease(ease_name)
        stride = raw.get("stride_world_units")
        if stride is not None:
            stride = _finite(stride, f"{label} stride_world_units")
            if stride <= 0:
                raise CodeArtError(f"{label}: stride_world_units must be > 0")
        events = []
        for index, event in enumerate(raw.get("events", [])):
            if not isinstance(event, dict):
                raise CodeArtError(f"{label} events[{index}] must be an object {{t, name}}")
            t = _finite(event.get("t"), f"{label} events[{index}].t")
            if not 0.0 <= t <= 1.0:
                raise CodeArtError(f"{label} events[{index}].t must lie in [0, 1]")
            record = {"t": t, "name": _event_name(event.get("name"), f"{label} events[{index}].name")}
            if "data" in event:
                record["data"] = event["data"]
            events.append(record)
        tracks = []
        raw_tracks = raw.get("tracks", {})
        if not isinstance(raw_tracks, dict):
            raise CodeArtError(f"{label}: tracks must map bone ids to tracks")
        for bone_id, track in raw_tracks.items():
            tracks.append(self._track(str(bone_id), track, f"{label} track {bone_id}"))
        raw_ik = raw.get("ik", {})
        if raw_ik is None:
            raw_ik = {}
        if not isinstance(raw_ik, dict):
            raise CodeArtError(f"{label}: ik must map chain names to two-bone chains, for example "
                               "{\"leg\": {\"bones\": [\"thigh\", \"shin\"], \"end\": \"foot\", ...}}")
        ik = [self._ik(str(chain_name), spec, f"{label} ik {chain_name}", loop, stride, ease)
              for chain_name, spec in raw_ik.items()]
        self._check_ik(ik, tracks, label)
        entry = raw.get("entry_frame")
        if entry is not None and (isinstance(entry, bool) or not isinstance(entry, int) or not 0 <= entry < frames):
            raise CodeArtError(f"{label}: entry_frame must be a frame index in [0, {frames})")
        transitions = raw.get("transitions", [])
        if not isinstance(transitions, list) or not all(isinstance(item, dict) and isinstance(item.get("to"), str)
                                                        for item in transitions):
            raise CodeArtError(f"{label}: transitions must be a list of {{to, entry_frame?, dissolve_ms?, mode?}}")
        role = raw.get("role")
        if role is not None and role not in ("player", "enemy", "npc", "fx", "prop"):
            raise CodeArtError(f"{label}: role must be player, enemy, npc, fx or prop")
        grounded = _bool_value(raw.get("grounded", True), f"{label} grounded")
        return Clip(name, frames, loop, durations, ease, stride, events, tracks, ik, grounded, entry,
                    transitions, role)

    def _track(self, bone_id: str, raw: Any, label: str) -> Track:
        if bone_id not in self.rig.bones:
            raise CodeArtError(f"{label}: the rig has no bone {bone_id!r}")
        if not isinstance(raw, dict) or not raw:
            raise CodeArtError(f"{label} must be an object of channels")
        unknown = sorted(set(raw) - set(TRACK_KINDS) - {"ease"})
        if unknown:
            raise CodeArtError(f"{label}: unknown channel {unknown[0]!r}; use {', '.join(TRACK_KINDS)} or ease")
        converters = {"rotate": _finite, "translate": _point_value, "scale": _scale_value, "show": _bool_value,
                      "swap": _variant_name}
        channels = {kind: _keyframes(raw[kind], f"{label} {kind}", converters[kind])
                    for kind in TRACK_KINDS if kind in raw}
        for _, variant in channels.get("swap", []):
            if variant not in self.rig.bones[bone_id].variants:
                known = ", ".join(self.rig.bones[bone_id].variants) or "none"
                raise CodeArtError(f"{label}: bone {bone_id!r} has no variant {variant!r} (known: {known})")
        return Track(bone_id, channels, parse_ease(raw["ease"]) if "ease" in raw else None)

    def _ik(self, name: str, raw: Any, label: str, loop: bool, stride: float | None,
            clip_ease: Callable[[float], float]) -> IKChain:
        if not isinstance(raw, dict):
            raise CodeArtError(f"{label} must be an object")
        bones = raw.get("bones")
        if not isinstance(bones, list) or len(bones) != 2 or not all(isinstance(b, str) and b in self.rig.bones
                                                                     for b in bones):
            raise CodeArtError(f"{label}: bones must name two rig bones [root, middle]")
        root, mid = bones
        if self.rig.bones[mid].parent != root:
            raise CodeArtError(f"{label}: bone {mid!r} must be a direct child bone of {root!r}")
        end = raw.get("end")
        if isinstance(end, str):
            if end not in self.rig.bones or self.rig.bones[end].parent != mid:
                raise CodeArtError(f"{label}: end must be a direct child bone of {mid!r} or a rest point [x, y]")
            end_bone, end_point = end, self.rig.bones[end].pivot
        else:
            end_bone, end_point = None, _point_value(end, f"{label} end")
        p1, p2 = self.rig.bones[root].pivot, self.rig.bones[mid].pivot
        if math.dist(p1, p2) < 1e-6 or math.dist(p2, end_point) < 1e-6:
            raise CodeArtError(f"{label}: both IK bones need a length (distinct pivots)")
        pole = _point_value(raw["pole"], f"{label} pole") if "pole" in raw else None
        if pole is None:
            bend = (end_point[0] - p1[0]) * (p2[1] - p1[1]) - (end_point[1] - p1[1]) * (p2[0] - p1[0])
            if abs(bend) < 1e-9:
                raise CodeArtError(f"{label}: the rest pose is straight, so give pole [x, y], the direction the "
                                   "middle joint bends toward (e.g. [1, 0] for a knee pointing +x)")
        target = gait = None
        if ("target" in raw) == ("gait" in raw):
            raise CodeArtError(f"{label}: give exactly one of target (keyframes) or gait")
        if "target" in raw:
            target = _keyframes(raw["target"], f"{label} target", _point_value)
        else:
            gait = self._gait(raw["gait"], f"{label} gait", loop, stride, end_point)
        angle = _keyframes(raw["angle"], f"{label} angle", _finite) if "angle" in raw else None
        if angle is not None and gait is not None:
            raise CodeArtError(f"{label}: a gait sets the foot angle itself (use gait.roll)")
        ground = _bool_value(raw.get("ground", True), f"{label} ground")
        ease = parse_ease(raw["ease"]) if "ease" in raw else clip_ease
        return IKChain(name, root, mid, end_bone, end_point, pole, target, angle, gait, ground, ease)

    @staticmethod
    def _gait(raw: Any, label: str, loop: bool, stride: float | None, end_point: tuple[float, float]) -> dict:
        if not isinstance(raw, dict):
            raise CodeArtError(f"{label} must be an object")
        if not loop or stride is None:
            raise CodeArtError(f"{label} needs a loop clip with stride_world_units (the foot travel per cycle)")
        stance = _finite(raw.get("stance", 0.6), f"{label} stance")
        if not 0.05 <= stance <= 0.95:
            raise CodeArtError(f"{label}: stance must lie in [0.05, 0.95] of the cycle")
        lift = _finite(raw.get("lift", 0.0), f"{label} lift")
        if lift < 0:
            raise CodeArtError(f"{label}: lift must be >= 0")
        return {"phase": _finite(raw.get("phase", 0.0), f"{label} phase") % 1.0, "stance": stance, "lift": lift,
                "roll": _finite(raw.get("roll", 0.0), f"{label} roll"),
                "x": _finite(raw.get("x", end_point[0]), f"{label} x")}

    def _check_ik(self, chains: list[IKChain], tracks: list[Track], label: str) -> None:
        by_bone = {track.bone: track for track in tracks}
        driven: dict[str, str] = {}
        for chain in chains:
            owned = [chain.root, chain.mid] + ([chain.end] if chain.end else [])
            for bone in owned:
                if bone in driven:
                    raise CodeArtError(f"{label}: bone {bone!r} is in IK chains {driven[bone]!r} and {chain.name!r}")
                driven[bone] = chain.name
                track = by_bone.get(bone)
                if track and "rotate" in track.channels:
                    raise CodeArtError(f"{label}: bone {bone!r} is driven by IK chain {chain.name!r}; remove its "
                                       "rotate track")
                if track and bone != chain.root and "translate" in track.channels:
                    raise CodeArtError(f"{label}: translating {bone!r} would change the IK bone lengths")
            for bone in self.rig.ancestors(chain.mid):
                track = by_bone.get(bone)
                if track and "scale" in track.channels:
                    raise CodeArtError(f"{label}: IK chain {chain.name!r} assumes unscaled bones, but {bone!r} "
                                       "has a scale track")


# ----------------------------------------------------------------------------- posing, FK and IK

@dataclass
class Frame:
    clip: str
    index: int
    t: float
    world: dict[str | None, np.ndarray]
    variants: dict[str, str]
    hidden: set[str]
    contacts: dict[str, dict]
    clamps: list[dict]


def local_matrix(bone: Bone, pose: BonePose) -> np.ndarray:
    """T(d) * T(p) * R * S * T(-p): translate, then rotate and scale about the pivot p."""
    px, py = bone.pivot
    return (translate(*pose.translate) @ translate(px, py) @ rotate(pose.rotate) @ np.diag([*pose.scale, 1.0])
            @ translate(-px, -py))


def world_matrices(rig: Rig, poses: Mapping[str, BonePose]) -> dict[str | None, np.ndarray]:
    """World matrix of every bone (document order puts parents first); None is the root space."""
    world: dict[str | None, np.ndarray] = {None: np.eye(3)}
    rest = BonePose()
    for bone in rig.bones.values():
        world[bone.id] = world[bone.parent] @ local_matrix(bone, poses.get(bone.id, rest))
    return world


def solve_two_bone(root: tuple[float, float], target: tuple[float, float], upper: float, lower: float,
                   side: float) -> tuple[float, float, tuple[float, float], float]:
    """World angles (radians) of the two bones reaching from `root` toward `target`.

    side > 0 bends the middle joint clockwise on screen from the root-target line (y down).
    Returns (upper angle, lower angle, reached end point, clamp): clamp is how far the
    target lay outside the reachable ring [|upper - lower|, upper + lower] (0 when reached)."""
    dx, dy = target[0] - root[0], target[1] - root[1]
    distance = math.hypot(dx, dy)
    longest, shortest = upper + lower, abs(upper - lower)
    clamp = 0.0
    if distance < 1e-12:
        dx, dy, distance = 0.0, 1.0, 0.0
    if distance > longest:
        clamp = distance - longest
        distance = longest
    elif distance < shortest:
        clamp = shortest - distance
        distance = shortest
    base = math.atan2(dy, dx)
    cosine = (upper * upper + distance * distance - lower * lower) / (2.0 * upper * distance) if distance else 1.0
    bend = math.acos(min(1.0, max(-1.0, cosine)))
    first = base + (1.0 if side > 0 else -1.0) * bend
    middle = (root[0] + upper * math.cos(first), root[1] + upper * math.sin(first))
    end = (root[0] + distance * math.cos(base), root[1] + distance * math.sin(base)) if clamp else target
    second = math.atan2(end[1] - middle[1], end[0] - middle[0])
    return first, second, end, clamp


class Poser:
    """Samples clips into frames: tracks, then IK chains, then world matrices and contacts."""

    def __init__(self, rig: Rig, *, clearance: Callable[[Slot], float]) -> None:
        self.rig = rig
        self.clearance = clearance  # outline allowance below a slot's fill edge for the ground constraint

    def contact_slots(self, chain: IKChain) -> list[Slot]:
        return [slot for slot in self.rig.slots if chain.end is not None and slot.bone == chain.end
                and not slot.geometry.empty()]

    def foot_offset(self, chain: IKChain, angle: float, slots: Sequence[Slot]) -> float:
        """Lowest point of the end bone's slots below its pivot at world angle `angle`, with clearance."""
        if not slots:
            return 0.0
        px, py = chain.end_point
        spin = rotate(angle) @ translate(-px, -py)
        return max(slot.geometry.extent(spin @ slot.local, (0.0, 1.0)) + self.clearance(slot) for slot in slots)

    def foot_extent(self, slots: Sequence[Slot], matrix: np.ndarray, direction: tuple[float, float]) -> float:
        return max(slot.geometry.extent(matrix @ slot.local, direction) for slot in slots)

    def pose(self, clip: Clip, index: int, t: float) -> Frame:
        poses = {bone_id: BonePose() for bone_id in self.rig.bones}
        for bone in self.rig.bones.values():
            if bone.variants:
                poses[bone.id].swap = bone.variants[0]
        for track in clip.tracks:
            ease = track.ease or clip.ease
            pose = poses[track.bone]
            for kind, keys in track.channels.items():
                value = sample_keys(keys, t, ease, hold=kind in ("show", "swap"))
                setattr(pose, kind, value)
        contacts: dict[str, dict] = {}
        clamps: list[dict] = []
        depth = {bone_id: len(self.rig.ancestors(bone_id)) for bone_id in self.rig.bones}
        for chain in sorted(clip.ik, key=lambda c: depth[c.root]):
            record = self._solve(chain, clip, t, poses)
            contacts[chain.name] = record
            if record["clamp_px"] > 0.0:
                clamps.append({"chain": chain.name, "frame": index, "t": t, "clamp_px": record["clamp_px"]})
        world = world_matrices(self.rig, poses)
        for chain in clip.ik:
            self._measure(chain, contacts[chain.name], world)
        hidden: set[str] = set()
        for bone in self.rig.bones.values():
            if not poses[bone.id].show or (bone.parent is not None and bone.parent in hidden):
                hidden.add(bone.id)
        variants = {bone_id: pose.swap for bone_id, pose in poses.items() if pose.swap is not None}
        return Frame(clip.name, index, t, world, variants, hidden, contacts, clamps)

    def _target(self, chain: IKChain, clip: Clip, t: float, ground_y: Callable[[float], float]) -> tuple:
        """(target x, target y, foot world angle) before the ground constraint."""
        if chain.gait is None:
            x, y = sample_keys(chain.target, t, chain.ease)
            angle = sample_keys(chain.angle, t, chain.ease) if chain.angle else 0.0
            return x, y, angle
        gait = chain.gait
        cycle = (t + gait["phase"]) % 1.0
        step = clip.stride * gait["stance"]
        if cycle < gait["stance"]:
            x = gait["x"] + step / 2.0 - step * (cycle / gait["stance"])
            return x, ground_y(0.0), 0.0
        u = (cycle - gait["stance"]) / (1.0 - gait["stance"])
        x = gait["x"] - step / 2.0 + step * (0.5 - 0.5 * math.cos(math.pi * u))
        angle = gait["roll"] * math.sin(math.pi * u)
        return x, ground_y(angle) - gait["lift"] * math.sin(math.pi * u), angle

    def _solve(self, chain: IKChain, clip: Clip, t: float, poses: dict[str, BonePose]) -> dict:
        rig = self.rig
        slots = self.contact_slots(chain)
        grounded = chain.ground

        def ground_y(angle: float) -> float:
            return rig.ground - self.foot_offset(chain, angle, slots)

        x, y, angle = self._target(chain, clip, t, ground_y)
        if grounded:
            y = min(y, ground_y(angle))
        world = world_matrices(rig, poses)
        root_bone = rig.bones[chain.root]
        parent = world[root_bone.parent]
        root_pose = poses[chain.root]
        hip = apply(parent, (root_bone.pivot[0] + root_pose.translate[0], root_bone.pivot[1] + root_pose.translate[1]))
        parent_angle = matrix_angle(parent)
        p1, p2, pe = root_bone.pivot, rig.bones[chain.mid].pivot, chain.end_point
        upper, lower = math.dist(p1, p2), math.dist(p2, pe)
        if chain.pole is not None:
            side = (x - hip[0]) * chain.pole[1] - (y - hip[1]) * chain.pole[0]
        else:
            side = (pe[0] - p1[0]) * (p2[1] - p1[1]) - (pe[1] - p1[1]) * (p2[0] - p1[0])
        first, second, _, clamp = solve_two_bone(hip, (x, y), upper, lower, side)
        rest_first = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
        rest_second = math.atan2(pe[1] - p2[1], pe[0] - p2[0])
        r1 = math.degrees(first - rest_first) - parent_angle
        r2 = math.degrees(second - rest_second) - parent_angle - r1
        poses[chain.root].rotate = r1
        poses[chain.mid].rotate = r2
        if chain.end is not None:
            poses[chain.end].rotate = angle - parent_angle - r1 - r2
        return {"target": [x, y], "angle": angle, "clamp_px": clamp, "grounded": grounded}

    def _measure(self, chain: IKChain, record: dict, world: dict[str | None, np.ndarray]) -> None:
        """Contact data from the rig itself: ankle, lowest point, toe and heel, planted or not."""
        holder = chain.end if chain.end is not None else chain.mid
        ankle = apply(world[holder], chain.end_point)
        record["ankle"] = [ankle[0], ankle[1]]
        slots = self.contact_slots(chain)
        if slots:
            matrix = world[chain.end]
            lowest = max(slot.geometry.extent(matrix @ slot.local, (0.0, 1.0)) + self.clearance(slot)
                         for slot in slots)
            front, back = (1.0, 0.0), (-1.0, 0.0)
            if self.rig.facing == "left":
                front, back = back, front
            toe = self.foot_extent(slots, matrix, front) * front[0]
            heel = self.foot_extent(slots, matrix, back) * back[0]
        else:
            lowest, toe, heel = ankle[1], ankle[0], ankle[0]
        record.update({"lowest_y": lowest, "toe_x": toe, "heel_x": heel,
                       "planted": bool(record["grounded"] and abs(lowest - self.rig.ground) <= CONTACT_TOLERANCE)})


def planted_runs(planted: Sequence[bool], loop: bool) -> list[list[int]]:
    """Runs of consecutive planted frames; for loops a run may wrap from the last frame to the first."""
    count = len(planted)
    if not any(planted):
        return []
    if all(planted):
        return [list(range(count))]
    start = next(i for i in range(count) if not planted[i]) + 1 if loop else 0
    runs, current = [], []
    for step in range(count):
        index = (start + step) % count
        if planted[index]:
            current.append(index)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def contact_drift(frames: Sequence[Frame], clip: Clip) -> dict:
    """World-space slide of every planted foot: ankle x + travel and ankle y over each run of
    consecutive planted frames (cyclic for loops). travel(t) = stride_world_units * t."""
    result = {}
    count = len(frames)
    travel = [(clip.stride or 0.0) * frame.t for frame in frames]
    cycle = clip.stride or 0.0
    for chain in clip.ik:
        planted = [frame.contacts[chain.name]["planted"] for frame in frames]
        runs = planted_runs(planted, clip.loop)
        drift_x = drift_y = 0.0
        run_records = []
        for run in runs:
            xs, ys = [], []
            laps = 0
            for position, i in enumerate(run):
                if position and i < run[position - 1]:
                    laps += 1
                ankle = frames[i].contacts[chain.name]["ankle"]
                xs.append(ankle[0] + travel[i] + laps * cycle)
                ys.append(ankle[1])
            span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
            drift_x, drift_y = max(drift_x, span_x), max(drift_y, span_y)
            run_records.append({"frames": run, "world_x": round(xs[0], 6), "drift_x_px": round(span_x, 6),
                                "drift_y_px": round(span_y, 6)})
        result[chain.name] = {"planted_frames": [i for i in range(count) if planted[i]], "runs": run_records,
                              "drift_x_px": round(drift_x, 6), "drift_y_px": round(drift_y, 6)}
    return result


# ----------------------------------------------------------------------------- rendering

def visible_slots(rig: Rig, frame: Frame) -> list[Slot]:
    """Slots drawn in this frame, back to front by (data-z, document order)."""
    chosen = []
    for slot in rig.slots:
        painted = slot.paint["fill"] is not None or slot.paint["stroke"] is not None
        if not painted or not slot.paint["visible"] or slot.bone in frame.hidden:
            continue
        if slot.variant is not None and frame.variants.get(slot.variant[0]) != slot.variant[1]:
            continue
        chosen.append(slot)
    return sorted(chosen, key=lambda slot: (slot.z, slot.index))


def flat_svg(rig: Rig, frame: Frame, slots: Sequence[Slot], *, view: tuple[int, int, int, int] | None = None,
             coverage: bool = False, stroke_scale: float = 1.0) -> str:
    """One frame as flat SVG: every slot wrapped in its bone's world matrix (plus attribute
    copies of its ancestor groups), in draw order. `coverage` paints the slot white without
    its stroke (or its stroke alone when it has no fill) for pixel finishing."""
    x0, y0, width, height = view or (0, 0, rig.size[0], rig.size[1])
    root = ET.Element(_q("svg"), {**rig.root_attributes, "width": str(width), "height": str(height),
                                  "viewBox": f"{x0} {y0} {width} {height}"})
    for resource in rig.resources:
        root.append(copy.deepcopy(resource))
    for slot in slots:
        node = ET.SubElement(root, _q("g"), {"transform": matrix_attribute(frame.world[slot.bone])})
        for link in slot.chain:
            child = copy.copy(link)
            child.attrib = dict(link.attrib)
            node.append(child)
            node = child
        element = copy.deepcopy(slot.element)
        style = element.get("style", "").strip().rstrip(";")
        if coverage:
            paint = ("fill:#ffffff;stroke:none" if slot.paint["fill"] is not None
                     else "fill:none;stroke:#ffffff")
            element.set("style", ";".join(filter(None, [style, paint, "opacity:1;fill-opacity:1;stroke-opacity:1"])))
        elif stroke_scale != 1.0 and slot.paint["stroke"] is not None:
            width_text = svg_number(slot.paint["stroke_width"] * stroke_scale)
            element.set("style", ";".join(filter(None, [style, f"stroke-width:{width_text}"])))
        node.append(element)
    return ET.tostring(root, encoding="unicode")


@dataclass
class RouteOptions:
    route: str = "pixel"
    zoom: int = 1
    ss: int = 8
    coverage: float = 0.5
    outline: str | None = None
    outline_mode: str = "solid"
    stroke_scale: float = 1.0
    backend: str = "auto"
    ramps: dict = field(default_factory=dict)
    light: tuple[float, float] | None = (-1.0, -1.0)
    inner_lines: bool = True

    def line_colour(self) -> str | None:
        """The colour the pixel route draws its outline and inner lines with, when it draws any."""
        drawn = self.outline_mode != "none" or self.inner_lines
        return core.rgba_to_hex(core.hex_to_rgba(self.outline)) if self.outline and drawn else None


class Renderer:
    """Turns posed frames into RGBA through the vector or the pixel route."""

    def __init__(self, rig: Rig, options: RouteOptions) -> None:
        self.rig, self.options = rig, options
        self.renderer_info: dict | None = None
        self._coverage_cache: dict[str, np.ndarray] = {}
        self.slot_ramps = {slot.index: self._ramp_for(slot) for slot in rig.slots}

    def _ramp_for(self, slot: Slot) -> core.Ramp | None:
        ramps = self.options.ramps
        for name in slot.classes:
            if name in ramps:
                return ramps[name]
        return ramps.get(slot.paint["fill"]) if slot.paint["fill"] else None

    def clearance(self, slot: Slot) -> float:
        """How far the drawn outline reaches below a slot's fill edge."""
        if self.options.route == "pixel":
            return 1.0 if self.options.outline_mode != "none" else 0.0
        if slot.paint["stroke"] is None:
            return 0.0
        return slot.paint["stroke_width"] * self.options.stroke_scale / 2.0

    def render(self, frame: Frame, frame_id: str) -> np.ndarray:
        slots = visible_slots(self.rig, frame)
        if self.options.route == "vector":
            svg = core.compile_svg(flat_svg(self.rig, frame, slots, stroke_scale=self.options.stroke_scale),
                                   id_prefix=f"{frame_id}_")
            problems = core.lint_portable_svg(svg)
            if problems:
                raise CodeArtError(f"frame {frame_id} is outside the portable profile: {problems[0]}")
            rgba, info = core.rasterize(svg, zoom=self.options.zoom, backend=self.options.backend)
            self.renderer_info = self.renderer_info or info
            return rgba
        width, height = self.rig.size
        ss = self.options.ss
        entries = []
        for slot in slots:
            alpha = self._coverage(frame, slot)
            if alpha is None:
                continue
            entry = {"alpha": alpha, "group": slot.bone or slot.name, "name": slot.name}
            ramp = self.slot_ramps[slot.index]
            colour = slot.paint["fill"] if slot.paint["fill"] is not None else slot.paint["stroke"]
            if ramp is not None and slot.paint["fill"] is not None:
                entry["ramp"] = ramp
            else:
                entry["fill"] = colour
            entries.append(entry)
        if not entries:
            return np.zeros((height, width, 4), np.uint8)
        rgba, _ = core.pixel_finish(entries, (width, height), ss=ss, coverage=self.options.coverage,
                                    outline=self.options.outline_mode, outline_color=self.options.outline or "#000000",
                                    light=self.options.light, inner_lines=self.options.inner_lines)
        return rgba

    def _coverage(self, frame: Frame, slot: Slot) -> np.ndarray | None:
        """(H*ss, W*ss) coverage of one slot, rendered only over its pixel-aligned bounding box."""
        width, height = self.rig.size
        ss = self.options.ss
        matrix = frame.world[slot.bone] @ slot.local
        if slot.geometry.empty():
            return None
        left, top, right, bottom = slot.geometry.bbox(matrix)
        reach = 1.0 if slot.paint["fill"] is not None else slot.paint["stroke_width"] * 2.0 + 1.0
        x0, y0 = max(0, math.floor(left - reach)), max(0, math.floor(top - reach))
        x1, y1 = min(width, math.ceil(right + reach)), min(height, math.ceil(bottom + reach))
        if x1 <= x0 or y1 <= y0:
            return None
        svg = flat_svg(self.rig, frame, [slot], view=(x0, y0, x1 - x0, y1 - y0), coverage=True)
        region = self._coverage_cache.get(svg)
        if region is None:
            pixels, info = core.rasterize(svg, zoom=ss, backend=self.options.backend)
            self.renderer_info = self.renderer_info or info
            region = pixels[..., 3]
            self._coverage_cache[svg] = region
        full = np.zeros((height * ss, width * ss), np.uint8)
        full[y0 * ss:y1 * ss, x0 * ss:x1 * ss] = region
        return full


# ----------------------------------------------------------------------------- shared output helpers
# fx_build.py imports these, so both codeart2d animation tools write the same contracts.

def output_ref(path: Path, base: Path) -> dict:
    """fileRef of a published output: POSIX path relative to `base`, sha256 and bytes (forge_core.file_ref)."""
    return forge_core.file_ref(path, base)


def input_ref(path: Path, base: Path) -> dict:
    """fileRef of an input: relative to `base`, or the file name when it lives on another drive (D30)."""
    return forge_core.file_ref(path, base)


def qa_envelope(checks: list[dict], *, method: str, not_proven: list[str], inputs: list[dict],
                outputs: list[dict], tool: str) -> dict:
    """Common qaEnvelope: status is fail when a measured check fails, else warn, else pass."""
    measured = {check["status"] for check in checks if check["status"] != "skipped"}
    status = "fail" if "fail" in measured else "warn" if "warn" in measured else "pass"
    return {"status": status, "method": method, "notProven": not_proven, "checks": checks, "inputs": inputs,
            "outputs": outputs, "tool": {"name": tool, "version": TOOL_VERSION}}


def check(check_id: str, passed: bool | None, value: Any, threshold: Any, *, warn: bool = False) -> dict:
    """One qaCheck; passed None means skipped; warn downgrades a failure to a warning."""
    status = "skipped" if passed is None else "pass" if passed else "warn" if warn else "fail"
    return {"id": check_id, "status": status, "value": value, "threshold": threshold}


def pixel_gate_checks(frame_qa: Sequence[dict], *, outline: bool) -> list[dict]:
    """codeart_core.QA_PIXEL_GATES over every frame: the worst frame is the check's value."""
    checks = []
    for name, limit in core.QA_PIXEL_GATES:
        values = [qa[name] for qa in frame_qa if qa.get(name) is not None]
        if not values or (name in ("outline_gaps", "l_corners") and not outline):
            checks.append(check(name, None, None, limit))
        else:
            worst = max(values)
            checks.append(check(name, worst <= limit, worst, limit))
    return checks


def margin_px(alpha: np.ndarray) -> int | None:
    """Smallest distance in pixels from a visible pixel to the canvas edge (None when empty)."""
    ys, xs = np.nonzero(alpha > 0)
    if not xs.size:
        return None
    height, width = alpha.shape
    return int(min(xs.min(), ys.min(), width - 1 - xs.max(), height - 1 - ys.max()))


def dedupe_frames(rendered: Sequence[tuple[str, np.ndarray]]) -> tuple[list[tuple[str, np.ndarray]], dict[str, str]]:
    """Unique frames in first-seen order and a map from every frame id to its stored frame name."""
    unique, by_hash, names = [], {}, {}
    for frame_id, rgba in rendered:
        digest = core.rgba_sha256(rgba)
        if digest not in by_hash:
            by_hash[digest] = frame_id
            unique.append((frame_id, rgba))
        names[frame_id] = by_hash[digest]
    return unique, names


def run_clips_builder(builder: Path, manifest: Path, output: Path) -> dict:
    """Run generate2dsprite build_animation_clips.py by path (never `process`)."""
    if not builder.is_file():
        raise CodeArtError(f"build_animation_clips.py not found at {builder}; install the sibling generate2dsprite "
                           "skill next to codeart2d or pass --clips-builder")
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run([sys.executable, str(builder), "--manifest", str(manifest), "--output-dir", str(output)],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=900, env=environment)
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "message": "build_animation_clips timed out"}
    record = {"returncode": result.returncode}
    if result.returncode == 0:
        record["output"] = Path(os.path.relpath(output / "animation-clips.json", manifest.parent)).as_posix()
    else:
        record["message"] = forge_core.ascii_text((result.stderr or result.stdout or "").strip()[-400:])
    return record


def describe_error(exc: BaseException) -> str:
    """One-line user message for a CLI failure (no traceback)."""
    if isinstance(exc, FileExistsError):
        return f"refusing to replace existing output: {exc.filename or exc}"
    if isinstance(exc, OSError) and exc.strerror:
        return f"{exc.strerror}: {exc.filename}" if exc.filename else exc.strerror
    return str(exc)


def save_review(image: Image.Image | np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    core.save_png(np.asarray(image.convert("RGBA")) if isinstance(image, Image.Image) else image, path)


def onion_cycle(frames: Sequence[np.ndarray], scale_factor: int) -> np.ndarray:
    """All frames of a clip over each other on a light backdrop, older frames fainter."""
    height, width = frames[0].shape[:2]
    canvas = Image.new("RGBA", (width * scale_factor, height * scale_factor), (236, 236, 240, 255))
    count = len(frames)
    for index, frame in enumerate(frames):
        ghost = core.upscale_nearest(frame, scale_factor).copy()
        weight = 1.0 if index == count - 1 else 0.25 + 0.5 * index / max(1, count - 1)
        ghost[..., 3] = (ghost[..., 3].astype(np.float32) * weight).astype(np.uint8)
        canvas.alpha_composite(Image.fromarray(ghost, "RGBA"))
    return np.asarray(canvas)


# ----------------------------------------------------------------------------- clip QA and outputs

def ground_edge(alpha: np.ndarray) -> int | None:
    """Bottom edge (row index + 1) of the lowest row above the geometry threshold."""
    mask = forge_core.subject_mask(alpha)
    if not mask.any():
        return None
    return int(forge_core.ground_row(mask))


def event_position(t: float, frames: int, loop: bool) -> int:
    """Frame nearest to an event time: round(t*n) mod n for loops, round(t*(n-1)) for one-shots."""
    if loop:
        return int(math.floor(t * frames + 0.5)) % frames
    return int(math.floor(t * (frames - 1) + 0.5))


def travel_strip(frames: Sequence[np.ndarray], offsets: Sequence[float], ground: float, marks: Sequence[list],
                 scale_factor: int) -> np.ndarray:
    """Frames laid over each other at their world travel offset: a planted foot stays put.
    marks: per frame a list of planted ankle points (canvas units, output pixels)."""
    height, width = frames[0].shape[:2]
    span = int(math.ceil(max(offsets) - min(offsets)))
    canvas = Image.new("RGBA", ((width + span) * scale_factor, height * scale_factor), (236, 236, 240, 255))
    shift = -min(offsets)
    for frame, offset in zip(frames, offsets):
        ghost = core.upscale_nearest(frame, scale_factor).copy()
        ghost[..., 3] = (ghost[..., 3].astype(np.uint16) * 140 // 255).astype(np.uint8)
        canvas.alpha_composite(Image.fromarray(ghost, "RGBA"), (int(round((offset + shift) * scale_factor)), 0))
    draw = ImageDraw.Draw(canvas)
    line_y = int(round(ground * scale_factor)) - 1
    draw.line([(0, line_y), (canvas.width, line_y)], fill=(40, 120, 220, 255), width=1)
    for frame_marks, offset in zip(marks, offsets):
        for x, y in frame_marks:
            cx, cy = (x + offset + shift) * scale_factor, y * scale_factor
            draw.rectangle([cx - 2, cy - 2, cx + 2, cy + 2], outline=(220, 40, 60, 255))
    return np.asarray(canvas)


@dataclass
class ClipResult:
    clip: Clip
    frames: list[Frame]
    images: list[np.ndarray]
    frame_ids: list[str]


def godot_contract(clip: Clip, frame_files: Sequence[str], size: tuple[int, int], anchor: tuple[float, float],
                   subject_height: float, world_height: float) -> dict:
    """generate2dsprite.godot_sprite3d.v1 for one clip (the keys of generate2dsprite.py)."""
    width, height = size
    pixel_size = world_height / subject_height
    return {
        "schema": "generate2dsprite.godot_sprite3d.v1",
        "frame_size": [width, height],
        "output_origin": [anchor[0], anchor[1]],
        "sprite3d_offset": [width / 2 - anchor[0], anchor[1] - height / 2],
        "reference_subject_height_px": subject_height,
        "world_height": world_height,
        "recommended_pixel_size": pixel_size,
        "rendered_subject_height_world": subject_height * pixel_size,
        "scale_source": "measured_subject_height",
        "billboard": "enabled",
        "duration_ms": clip.durations[0],
        "durations_ms": list(clip.durations),
        "fps": 1000.0 / clip.durations[0],
        "loop": clip.loop,
        "frames": list(frame_files),
    }


def build(args: argparse.Namespace) -> dict:
    """Render, check and publish one rig animation; returns the one-line summary."""
    output_dir = Path(args.output_dir)
    if os.path.lexists(output_dir):
        raise CodeArtError(f"refusing to replace existing output: {output_dir}")
    final = output_dir.parent.resolve() / output_dir.name
    anim_path = Path(args.anim)
    anim_data = _strict_json(anim_path)
    rig_path = Path(args.rig) if args.rig else None
    if rig_path is None:
        rig_name = anim_data.get("rig") if isinstance(anim_data, dict) else None
        if not isinstance(rig_name, str) or not rig_name:
            raise CodeArtError("give --rig or a rig path in the animation")
        rig_path = anim_path.parent / rig_name
    palette = core.parse_palette(args.palette) if args.palette else None
    if args.variant and palette is None:
        raise CodeArtError("--variant needs --palette")
    rig = Rig.from_file(rig_path, palette=palette, variant=args.variant)
    animation = Animation(anim_data, rig, label=anim_path.name)
    names = [name.strip() for name in args.clips.split(",")] if args.clips else list(animation.clips)
    unknown = [name for name in names if name not in animation.clips]
    if unknown:
        raise CodeArtError(f"unknown clip {unknown[0]!r}; the animation has {', '.join(animation.clips)}")
    options = _route_options(args, rig, animation)
    renderer = Renderer(rig, options)
    allowed = None if palette is None else {core.rgba_to_hex(colour) for colour in palette.resolve(args.variant).values()}
    _check_route_paints(rig, options, allowed)
    poser = Poser(rig, clearance=renderer.clearance)
    zoom = options.zoom

    results: list[ClipResult] = []
    for name in names:
        clip = animation.clips[name]
        frames = [poser.pose(clip, index, t) for index, t in enumerate(clip.times())]
        frame_ids = [f"{name}-{index:02d}" for index in range(clip.frames)]
        images = [renderer.render(frame, frame_id) for frame, frame_id in zip(frames, frame_ids)]
        results.append(ClipResult(clip, frames, images, frame_ids))
    reference = _reference_frame(animation, results, poser, renderer)

    palette_colours = _route_palette(rig, options, allowed, renderer)
    outline_colours = sorted({options.line_colour()} | {ramp.out for ramp in options.ramps.values()}) \
        if options.route == "pixel" and options.outline_mode != "none" else None
    margin_limit = args.margin * zoom
    seam_low, seam_high = args.seam_range
    report_clips, frame_qa_all = {}, []
    clamp_records, ground_failures, margin_failures, seam_checks, drift_max = [], [], [], [], 0.0
    for result in results:
        clip = result.clip
        drift = contact_drift(result.frames, clip)
        drift_max = max([drift_max] + [max(record["drift_x_px"], record["drift_y_px"]) for record in drift.values()])
        frame_records = []
        for frame, image, frame_id in zip(result.frames, result.images, result.frame_ids):
            alpha = image[..., 3]
            qa = core.qa_pixels(image, palette_colours, outline_colours) if options.route == "pixel" \
                else core.qa_pixels(image)
            qa = {key: value for key, value in qa.items() if not key.endswith("_examples")}
            if options.route == "pixel":
                frame_qa_all.append(qa)
            planted = any(contact["planted"] for contact in frame.contacts.values())
            grounded = clip.grounded and (planted if clip.ik else True)
            edge = ground_edge(alpha)
            expected = int(round(rig.ground * zoom))
            gap = None if edge is None else expected - edge
            if grounded and gap != 0:
                ground_failures.append({"frame": frame_id, "edge": edge, "ground": expected, "gap_px": gap})
            margin = margin_px(alpha)
            if margin is None or margin < margin_limit:
                margin_failures.append({"frame": frame_id, "margin_px": margin})
            clamp_records += [{**item, "frame": frame_id} for item in frame.clamps]
            frame_records.append({
                "id": frame_id, "t": round(frame.t, 6), "qa": qa,
                "ground": {"grounded": grounded, "edge": edge, "gap_px": gap}, "margin_px": margin,
                "contacts": {name: _contact_record(contact, zoom) for name, contact in frame.contacts.items()},
                "clamps": frame.clamps})
        seam = None
        if clip.loop and clip.frames >= 2:
            seam = forge_core.seam_report(result.images)
            ratio = 1.0 if seam["seam"] == seam["adjacent_max"] == 0.0 else seam["seam_over_median"]  # a static hold
            if clip.frames >= 3:
                seam_checks.append(check(f"seam:{clip.name}", seam_low <= ratio <= seam_high, round(ratio, 4),
                                         [seam_low, seam_high]))
            seam = {key: round(value, 6) if isinstance(value, float) else value for key, value in seam.items()}
        entry = clip.entry_frame if clip.entry_frame is not None else _entry_frame(result.images, reference)
        report_clips[clip.name] = {
            "frames": frame_records, "loop": clip.loop, "duration_ms": clip.durations, "seam": seam,
            "contact": drift, "entry_frame": entry,
            "stride_world_units": None if clip.stride is None else clip.stride * zoom,
            "stride_px_per_frame": None if clip.stride is None or not clip.loop else clip.stride * zoom / clip.frames,
            "events": [{"at": event_position(event["t"], clip.frames, clip.loop), "name": event["name"],
                        **({"data": event["data"]} if "data" in event else {})} for event in clip.events]}

    with forge_core.staged_output(output_dir) as stage:
        all_frames = [(frame_id, image) for result in results for frame_id, image in zip(result.frame_ids, result.images)]
        unique, stored_as = dedupe_frames(all_frames)
        frame_files = {}
        for frame_id, image in unique:
            path = stage / "frames" / f"{frame_id}.png"
            core.save_png(image, path)
            frame_files[frame_id] = f"frames/{frame_id}.png"
        for result in results:
            for record in report_clips[result.clip.name]["frames"]:
                record["file"] = frame_files[stored_as[record["id"]]]
                record["stored_as"] = stored_as[record["id"]]
        anchor_px = [rig.anchor[0] * zoom, rig.anchor[1] * zoom]
        body_height = _body_height(reference)
        clips_manifest = _clips_manifest(results, report_clips, unique, stored_as, frame_files, anchor_px, animation,
                                         options, args.clips_schema, body_height)
        clips_path = stage / "clips.json"
        forge_core.write_json(clips_path, clips_manifest)

        build_record = None
        if args.build_clips:
            build_record = run_clips_builder(Path(args.clips_builder), clips_path, stage / "compiled-clips")
        review = _write_reviews(stage, results, report_clips, rig, options, palette_colours, zoom)
        godot = _write_godot(stage, results, report_clips, frame_files, stored_as, rig, zoom, body_height,
                             args.godot_world_height) if args.godot_world_height else None

        checks = []
        if options.route == "pixel":
            checks += pixel_gate_checks(frame_qa_all, outline=outline_colours is not None)
        else:
            checks += [check(name, None, None, limit) for name, limit in core.QA_PIXEL_GATES]
        checks += seam_checks
        if any(result.clip.ik for result in results):
            checks.append(check("ik_foot_drift", drift_max <= DRIFT_TOLERANCE, round(drift_max, 6), 0.0))
        checks.append(check("ik_clamp", not clamp_records, len(clamp_records), 0))
        checks.append(check("ground_row", not ground_failures, len(ground_failures), 0))
        checks.append(check("margins", not margin_failures, len(margin_failures), margin_limit))
        checks.append(check("build_clips", None if build_record is None else build_record["returncode"] == 0,
                            None if build_record is None else build_record["returncode"], 0))
        outputs = [output_ref(stage / frame_files[frame_id], stage) for frame_id, _ in unique] + \
            [output_ref(clips_path, stage)]
        sources = [rig_path, anim_path] + ([Path(args.palette)] if args.palette else [])
        inputs = [input_ref(path, final) for path in sources]
        envelope = qa_envelope(checks, method=_method(options), not_proven=_not_proven(options, build_record),
                               inputs=inputs, outputs=outputs, tool=TOOL)
        report = {
            "schema": RIG_REPORT_SCHEMA, "route": options.route, "frame_size": [rig.size[0] * zoom, rig.size[1] * zoom],
            "anchor_px": anchor_px, "ground_px": rig.ground * zoom, "zoom": zoom,
            "palette": palette_colours if options.route == "pixel" else None,
            "clips": report_clips, "ik_clamps": clamp_records, "ground_failures": ground_failures,
            "margin_failures": margin_failures, "build_clips": build_record, "review": review, "godot": godot,
            "qa": envelope}
        forge_core.write_json(stage / "rig-report.json", report)
        renderer_record = {**(renderer.renderer_info or {"name": "none", "version": "0"}), "route": options.route,
                           "zoom": zoom, "size": [rig.size[0] * zoom, rig.size[1] * zoom]}
        if options.route == "pixel":
            renderer_record.update({"coverage_ss": options.ss, "coverage": options.coverage,
                                    "finish": "codeart_core.pixel_finish route D"})
        meta = core.write_codeart_meta(
            stage / "codeart-meta.json", generator=TOOL, spec_sha256=forge_core.sha256_file(anim_path),
            renderer=renderer_record, palette=_meta_palette(rig, palette_colours, options),
            outputs=[stage / frame_files[frame_id] for frame_id, _ in unique] + [clips_path], qa=envelope,
            extra={"route": options.route, "rig": inputs[0], "anim": inputs[1]})
        if args.strict_qc and envelope["status"] == "fail":
            failed = [item["id"] for item in envelope["checks"] if item["status"] == "fail"]
            raise CodeArtError(f"strict QC failed ({', '.join(failed)}); nothing was published. "
                               "Run without --strict-qc to inspect rig-report.json")
    return {"status": "ok", "qa": meta["qa"]["status"], "output": str(final),
            "metadata": str(final / "codeart-meta.json"), "report": str(final / "rig-report.json"),
            "clips": names, "frames": len(unique), "route": options.route,
            "failed_checks": [item["id"] for item in envelope["checks"] if item["status"] == "fail"]}


def _route_options(args: argparse.Namespace, rig: Rig, animation: Animation) -> RouteOptions:
    pixel = animation.pixel
    route = args.route
    zoom = args.zoom if args.zoom is not None else (1 if route == "pixel" else 4)
    if zoom < 1:
        raise CodeArtError("--zoom must be a whole number >= 1")
    if route == "pixel" and zoom != 1:
        raise CodeArtError("the pixel route renders logical pixels (zoom 1); upscale previews with nearest "
                           "neighbour, never by re-rendering")
    if not 2 <= args.ss <= 16:
        raise CodeArtError("--ss must lie in 2..16")
    if not 0.0 < args.coverage < 1.0:
        raise CodeArtError("--coverage must lie in (0, 1)")
    if args.stroke_scale <= 0.0 or not math.isfinite(args.stroke_scale):
        raise CodeArtError("--stroke-scale must be > 0")
    mode = args.outline_mode or pixel.get("outline_mode", "solid")
    outline_arg = args.outline if args.outline is not None else pixel.get("outline", "auto")
    outline = None
    if outline_arg == "none":
        mode = "none"
    elif outline_arg == "auto":
        outline = rig.outline_hint or _common_stroke(rig)
        if outline is None:
            mode = "none"
    else:
        outline = _colour(str(outline_arg), "--outline")
    if route == "pixel" and outline is not None and core.hex_to_rgba(outline)[3] != 255:
        raise CodeArtError("the pixel-route outline colour must be opaque")
    if mode == "selout" and not pixel["ramps"]:
        raise CodeArtError("--outline-mode selout needs pixel.ramps with 'out' colours in the animation")
    return RouteOptions(route=route, zoom=zoom, ss=args.ss, coverage=args.coverage, outline=outline,
                        outline_mode=mode, stroke_scale=args.stroke_scale, backend=args.backend,
                        ramps=pixel["ramps"], light=pixel["light"],
                        inner_lines=pixel["inner_lines"] and outline is not None)  # lines need a line colour


def _common_stroke(rig: Rig) -> str | None:
    counts: dict[str, int] = {}
    for slot in rig.slots:
        stroke = slot.paint["stroke"]
        if stroke and not stroke.startswith("url(") and slot.paint["fill"] is not None:
            counts[stroke] = counts.get(stroke, 0) + 1
    return max(sorted(counts), key=counts.__getitem__) if counts else None


def _check_route_paints(rig: Rig, options: RouteOptions, allowed: set[str] | None) -> None:
    """The pixel route needs opaque flat colours: no gradients, masks, filters or opacity; with a
    --palette (`allowed`: its colours with the chosen variant applied) every colour must be in it."""
    if options.route != "pixel":
        return
    for slot in rig.slots:
        paint = slot.paint
        if not paint["visible"]:
            continue
        colour = paint["fill"] if paint["fill"] is not None else paint["stroke"]
        if colour is None:
            continue
        if colour.startswith("url(") or core.hex_to_rgba(colour)[3] != 255:
            raise CodeArtError(f"slot {slot.name}: the pixel route needs an opaque hex colour, got {colour}; "
                               "gradients and translucent paints belong to the vector route")
        if min(paint["opacity"], paint["fill_opacity"], paint["stroke_opacity"]) < 1.0:
            raise CodeArtError(f"slot {slot.name}: the pixel route forbids opacity below 1 (it creates partial alpha)")
        for element in slot.chain + [slot.element]:
            if any(element.get(name) for name in ("mask", "filter")):
                raise CodeArtError(f"slot {slot.name}: the pixel route forbids masks and filters")
        if allowed is not None and colour not in allowed:
            raise CodeArtError(f"slot {slot.name}: colour {colour} is not in the --palette")
    if allowed is not None:
        extra = ([options.line_colour()] if options.line_colour() else []) + \
            [colour for ramp in options.ramps.values() for colour in (ramp.hi, ramp.mid, ramp.lo, ramp.dark, ramp.out)]
        for colour in extra:
            if core.rgba_to_hex(core.hex_to_rgba(colour)) not in allowed:
                raise CodeArtError(f"outline or ramp colour {colour} is not in the --palette")


def _route_palette(rig: Rig, options: RouteOptions, allowed: set[str] | None, renderer: Renderer) -> list[str]:
    """Every colour the pixel route may emit (the exact-palette gate checks against it): the --palette
    with its variant, else the rig's own slot colours, ramps and line colour."""
    if allowed is not None:
        return sorted(allowed)
    colours = set()
    for slot in rig.slots:
        if not slot.paint["visible"]:
            continue
        ramp = renderer.slot_ramps[slot.index]
        if ramp is not None and slot.paint["fill"] is not None:
            colours |= {core.rgba_to_hex(core.hex_to_rgba(c)) for c in (ramp.hi, ramp.mid, ramp.lo, ramp.dark, ramp.out)}
        else:
            colour = slot.paint["fill"] if slot.paint["fill"] is not None else slot.paint["stroke"]
            if colour is not None and not colour.startswith("url("):
                colours.add(colour)
    if options.line_colour():
        colours.add(options.line_colour())
    return sorted(colours)


def _meta_palette(rig: Rig, colours: Sequence[str], options: RouteOptions) -> dict:
    """Named palette for codeart-meta: c-NAME classes name their colour, the rest are numbered."""
    named: dict[str, str] = {}
    for slot in rig.slots:
        for name in slot.classes:
            if name.startswith("c-") and slot.paint["fill"] and slot.paint["fill"] in colours:
                named.setdefault(slot.paint["fill"], name[2:])
    if options.outline in colours:
        named.setdefault(options.outline, "outline")
    used = set()
    result = {}
    for index, colour in enumerate(colours):
        name = named.get(colour, f"colour{index}")
        name = name if name not in used else f"{name}-{index}"
        used.add(name)
        result[name] = colour
    if not result:  # vector route without a palette: record the authored fills
        fills = sorted({slot.paint["fill"] for slot in rig.slots if slot.paint["fill"]
                        and not slot.paint["fill"].startswith("url(")})
        result = {f"colour{index}": colour for index, colour in enumerate(fills)} or {"black": "#000000"}
    return result


def _reference_frame(animation: Animation, results: Sequence[ClipResult], poser: Poser,
                     renderer: Renderer) -> np.ndarray:
    """The frame other clips enter from: frame 0 of the entry reference clip, else the rest pose."""
    name = animation.entry_reference
    for result in results:
        if result.clip.name == name:
            return result.images[0]
    if name is not None:
        clip = animation.clips[name]
        return renderer.render(poser.pose(clip, 0, clip.times()[0]), f"{name}-ref")
    rest = Frame("rest", 0, 0.0, world_matrices(animation.rig, {}), {}, set(), {}, [])
    for bone in animation.rig.bones.values():
        if bone.variants:
            rest.variants[bone.id] = bone.variants[0]
    return renderer.render(rest, "rest")


def _entry_frame(images: Sequence[np.ndarray], reference: np.ndarray) -> int:
    """Frame closest to the reference pose (premultiplied MAE): where to enter the clip."""
    differences = [forge_core.transition_mae(image, reference) for image in images]
    return int(min(range(len(images)), key=lambda index: (round(differences[index], 9), index)))


def _body_height(reference: np.ndarray) -> float:
    box = forge_core.subject_bbox(reference[..., 3])
    return float(box[3] - box[1]) if box else 0.0


def _contact_record(contact: dict, zoom: int) -> dict:
    """One IK chain's contact data in output pixels, rounded for the report."""
    def px(value: float) -> float:
        return round(value * zoom, 6)

    return {"planted": contact["planted"], "grounded": contact["grounded"],
            "ankle": [px(value) for value in contact["ankle"]], "target": [px(value) for value in contact["target"]],
            "angle": round(contact["angle"], 6), "lowest_y": px(contact["lowest_y"]), "toe_x": px(contact["toe_x"]),
            "heel_x": px(contact["heel_x"]), "clamp_px": px(contact["clamp_px"])}


def _clips_manifest(results: Sequence[ClipResult], report_clips: dict, unique: Sequence, stored_as: dict,
                    frame_files: dict, anchor_px: list, animation: Animation, options: RouteOptions,
                    schema: str, body_height: float) -> dict:
    """build_animation_clips input (sprite clips_input): shared canvas, one anchor, named frames."""
    clips = {}
    for result in results:
        clip, record = result.clip, report_clips[result.clip.name]
        spec: dict[str, Any] = {"frames": [stored_as[frame_id] for frame_id in result.frame_ids],
                                "duration_ms": clip.durations if len(set(clip.durations)) > 1 else clip.durations[0],
                                "loop": clip.loop, "loop_policy": "cycle" if clip.loop else "oneshot",
                                "entry_frame": record["entry_frame"]}
        if clip.stride is not None:
            spec["stride_world_units"] = record["stride_world_units"]
            if clip.loop:
                spec["stride_px_per_frame"] = record["stride_px_per_frame"]
        if record["events"]:
            spec["events"] = record["events"]
        if clip.transitions:
            spec["transitions"] = clip.transitions
        if clip.role:
            spec["role"] = clip.role
        clips[clip.name] = spec
    manifest = {"schema": CLIPS_SCHEMAS[schema],
                "frames": [{"name": frame_id, "file": frame_files[frame_id]} for frame_id, _ in unique],
                "anchor_px": anchor_px, "clips": clips,
                "sampling": "nearest" if options.route == "pixel" else "linear",
                "pixel_art": options.route == "pixel", "art_source": "code", "placeholder": False}
    if body_height > 0:
        manifest["body_height_px"] = body_height
    states = {state: clip for state, clip in animation.states.items() if clip in clips}
    if states:
        manifest["states"] = states
    return manifest


def _write_reviews(stage: Path, results: Sequence[ClipResult], report_clips: dict, rig: Rig, options: RouteOptions,
                   palette_colours: Sequence[str], zoom: int) -> dict:
    """review/<clip>.png (scales, backgrounds, onion row, swatches, QA lines), <clip>-onion.png
    (the whole cycle) and, for clips with a stride, <clip>-travel.png (frames at world travel)."""
    written = {}
    anchor = (rig.anchor[0] * zoom, rig.anchor[1] * zoom)
    scales = (1, 2, 4) if options.route == "pixel" else (1,)
    preview = 4 if options.route == "pixel" else 1
    for result in results:
        name = result.clip.name
        qa = [record["qa"] for record in report_clips[name]["frames"]] if options.route == "pixel" else None
        sheet = core.review_sheet(result.images, scales=scales, onion=True,
                                  palette=palette_colours if options.route == "pixel" and palette_colours else None,
                                  qa=qa, anchor=anchor, title=f"{name} ({options.route} route, code-drawn)",
                                  loop=result.clip.loop)
        files = {"sheet": f"review/{name}.png"}
        save_review(sheet, stage / files["sheet"])
        if len(result.images) > 1:
            files["onion"] = f"review/{name}-onion.png"
            save_review(onion_cycle(result.images, preview), stage / files["onion"])
        if result.clip.stride is not None:
            offsets = [result.clip.stride * frame.t * zoom for frame in result.frames]
            marks = [[(contact["ankle"][0] * zoom, contact["ankle"][1] * zoom)
                      for contact in frame.contacts.values() if contact["planted"]] for frame in result.frames]
            files["travel"] = f"review/{name}-travel.png"
            save_review(travel_strip(result.images, offsets, rig.ground * zoom, marks, max(1, preview // 2)),
                        stage / files["travel"])
        written[name] = files
    return written


def _write_godot(stage: Path, results: Sequence[ClipResult], report_clips: dict, frame_files: dict, stored_as: dict,
                 rig: Rig, zoom: int, body_height: float, world_height: float) -> dict:
    if not (world_height > 0 and math.isfinite(world_height)):
        raise CodeArtError("--godot-world-height must be > 0")
    if body_height <= 0:
        raise CodeArtError("cannot size Sprite3D: the reference frame is empty")
    size = (rig.size[0] * zoom, rig.size[1] * zoom)
    anchor = (rig.anchor[0] * zoom, rig.anchor[1] * zoom)
    actions = {}
    for result in results:
        files = [Path(os.path.relpath(stage / frame_files[stored_as[frame_id]], stage / "godot")).as_posix()
                 for frame_id in result.frame_ids]
        contract = godot_contract(result.clip, files, size, anchor, body_height, world_height)
        relative = f"godot/{result.clip.name}.sprite3d.json"
        (stage / "godot").mkdir(exist_ok=True)
        forge_core.write_json(stage / relative, contract)
        actions[result.clip.name] = {"contract": f"{result.clip.name}.sprite3d.json", "loop": result.clip.loop}
    default = next((result.clip.name for result in results if result.clip.loop), results[0].clip.name)
    bundle = {"schema": "generate2dsprite.godot_sprite3d_bundle.v1", "default_action": default,
              "world_height": world_height, "world_height_max_drift": 0.0,
              "pixel_size": world_height / body_height, "pixel_size_max_drift": 0.0, "actions": actions}
    forge_core.write_json(stage / "godot" / "sprite3d-bundle.json", bundle)
    return {"bundle": "godot/sprite3d-bundle.json", "actions": sorted(actions)}


def _method(options: RouteOptions) -> str:
    route = ("pixel route: codeart_core.qa_pixels on every published frame against the route palette "
             "(partial alpha, exact palette, outline gaps, L-corners per codeart_core.QA_PIXEL_GATES)"
             if options.route == "pixel" else "vector route: anti-aliased frames, pixel gates not applicable")
    return (f"rig_animate: FK and two-bone IK sampled per clip; {route}; forge_core.seam_report "
            "(premultiplied MAE, seam over median adjacent step) on each loop of 3+ frames; planted-foot drift "
            "measured on the rig geometry (ankle x + stride * t, ankle y) per run of planted frames; IK clamps; "
            "lowest alpha row of grounded frames against the ground line; transparent margin; optional "
            "build_animation_clips run")


def _not_proven(options: RouteOptions, build_record: dict | None) -> list[str]:
    items = ["Appeal, silhouette, anatomy and readability at game scale: look at review/*.png.",
             "Timing, weight and acting in motion: the checks measure geometry, not feel.",
             "Planted-foot drift is measured on rig geometry; rendered pixels may still differ by coverage "
             "rounding or anti-aliasing when the per-frame travel is not a whole number of pixels."]
    if options.route == "vector":
        items.append("Vector frames are anti-aliased: partial alpha and blended edge colours are expected, so "
                     "palette conformance is not proven.")
    if build_record is None:
        items.append("build_animation_clips was not run (--build-clips not given).")
    return items


# ----------------------------------------------------------------------------- CLI

def _seam_range(text: str) -> tuple[float, float]:
    try:
        low, high = (float(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("use LOW,HIGH, e.g. 0.8,1.25") from None
    if not 0.0 < low <= 1.0 <= high:
        raise argparse.ArgumentTypeError("need 0 < LOW <= 1 <= HIGH")
    return low, high


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rig_animate.py",
        description="Animate a code-art SVG rig (FK, two-bone IK with a ground constraint, eased keyframes) into "
                    "checked 8-bit RGBA frames and a build_animation_clips manifest. Code-drawn, no image model.",
        epilog="Example: python \"<skill-dir>/scripts/rig_animate.py\" --rig hero.rig.svg --anim hero.anim.json "
               "--output-dir out/hero-v1 --route pixel --build-clips --strict-qc. "
               "See references/rig-animation.md.")
    parser.add_argument("--rig", help="rig SVG (default: the animation's rig path, relative to the animation)")
    parser.add_argument("--anim", required=True, help="codeart2d.rig_anim.v1 JSON")
    parser.add_argument("--output-dir", required=True, help="new output directory (refused when it exists)")
    parser.add_argument("--route", choices=("pixel", "vector"), default="pixel",
                        help="pixel: route D pixel finishing with exact palette (default); vector: anti-aliased SVG")
    parser.add_argument("--clips", help="comma-separated clip names to render (default: all, in file order)")
    parser.add_argument("--zoom", type=int, help="vector route integer zoom (default 4); the pixel route is 1")
    parser.add_argument("--ss", type=int, default=8, help="pixel route coverage supersampling (default 8)")
    parser.add_argument("--coverage", type=float, default=0.5, help="pixel route coverage threshold (default 0.5)")
    parser.add_argument("--outline", help="outline colour, auto (rig data-outline, else the common stroke) or none")
    parser.add_argument("--outline-mode", choices=("solid", "selout"), help="pixel route outline (default solid)")
    parser.add_argument("--stroke-scale", type=float, default=1.0,
                        help="vector route stroke-width multiplier (thicker lines at small sizes, e.g. 1.8 at 64 px)")
    parser.add_argument("--palette", help="palette file (.json, .hex, .gpl) for c-NAME classes and the palette gate")
    parser.add_argument("--variant", help="palette variant to apply (needs --palette)")
    parser.add_argument("--backend", choices=("auto",) + RASTER_BACKENDS, default="auto",
                        help="SVG rasterizer (default auto: resvg-py, then resvg-js CLI, then Chrome)")
    parser.add_argument("--margin", type=int, default=1, help="minimum transparent margin in rig pixels (default 1)")
    parser.add_argument("--seam-range", type=_seam_range, default=SEAM_RANGE,
                        help="allowed loop seam / median step ratio (default 0.8,1.25)")
    parser.add_argument("--build-clips", action="store_true",
                        help="run generate2dsprite build_animation_clips.py on clips.json (into compiled-clips/)")
    parser.add_argument("--clips-builder", default=str(DEFAULT_BUILDER),
                        help="path to build_animation_clips.py (default: the sibling generate2dsprite skill)")
    parser.add_argument("--clips-schema", choices=tuple(CLIPS_SCHEMAS), default="v2",
                        help="clips.json schema id (default v2, so events reach events_ms; v1 for builders that "
                             "predate the v2 reader)")
    parser.add_argument("--godot-world-height", type=float,
                        help="also write godot/*.sprite3d.json (godot_sprite3d.v1) for this subject height")
    parser.add_argument("--strict-qc", action="store_true", help="exit 1 and publish nothing when a QA check fails")
    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = build(args)
    except (CodeArtError, core.RasterError, ValueError, OSError) as exc:
        print(forge_core.ascii_text(f"error: {describe_error(exc)}"), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    return published_fail_status(summary)


def published_fail_status(summary: dict) -> int:
    """D26: an output published with QA status fail (no --strict-qc) still exits 1, with one error line that
    names the failed checks and the report; pass and warn exit 0. fx_build uses it too."""
    if summary.get("qa") != "fail":
        return 0
    print(forge_core.ascii_text(f"error: published with QA status fail: {', '.join(summary['failed_checks'])} "
                                f"(see {summary['report']})"), file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """argparse first (so --help and usage errors work everywhere, exit 2), then the run inside
    forge_core.run_cli: anything unexpected becomes one "error: internal error (...)" line, exit 1 (D27)."""
    problem = missing_modules_message()
    if problem:
        build_parser().parse_args(argv)
        print(problem, file=sys.stderr)
        return 1
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
