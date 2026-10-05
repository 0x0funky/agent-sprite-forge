"""Bake code-drawn game FX from a codeart2d.fx.v1 spec (codeart2d, no image model).

Presets (primitive types), all deterministic: slash (an arc whose head reaches its end
angle exactly at impactMs), sparks (a seeded spark burst), ring (an expanding impact ring),
flash (a hit flash star), dust (seeded dust puffs) and projectile (an in-flight loop). Colours
come from the spec palette only; ramps list colours brightest first.

Timing: frames are cut so one frame starts exactly at impactMs (the hit frame, with a "hit"
event); every frame shows the effect at the middle of its time on screen. Fully transparent
frames at the tail are dropped and their time is merged into the last kept frame, because
build_animation_clips refuses empty frames.

Routes: pixel (default) gives exact palette pixel art through codeart_core.pixel_finish
(8x coverage per colour run, optional 1 px outline); vector renders the same shapes
anti-aliased at an integer --zoom. --export-runtime also writes fx-runtime.mjs, the fx.v1
canvas runtime with the same geometry (spawn an effect at hitAt - impactMs), and checks it
with fx_verify.mjs when node is on PATH.

Outputs, staged and published only after QA into a new --output-dir: frames/*.png, clips.json
(build_animation_clips input, one clip per effect, role fx), fx-report.json, codeart-meta.json
(art_source code, QA envelope over every frame), review/*.png, optional compiled-clips/ and
fx-runtime.mjs plus fx-verify.json.

Usage, from the project root:
  python "<skill-dir>/scripts/fx_build.py" --spec slash.fx.json --output-dir out/fx-slash-v1 --route pixel --build-clips --export-runtime
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rig_animate as rig  # noqa: E402  (also checks numpy and Pillow first)

import numpy as np  # noqa: E402

import codeart_core as core  # noqa: E402
import forge_core  # noqa: E402


TOOL = "codeart2d/fx_build.py"
FX_SCHEMA = "codeart2d.fx.v1"
FX_REPORT_SCHEMA = "codeart2d.fx_report.v1"
PRESETS = ("slash", "sparks", "ring", "flash", "dust", "projectile")
RUNTIME_TEMPLATE = Path(__file__).resolve().parents[1] / "references" / "runtime" / "fx-template.mjs"
VERIFIER = Path(__file__).resolve().parent / "fx_verify.mjs"
DATA_START, DATA_END = "/*FX-DATA*/", "/*END-FX-DATA*/"
DEFAULT_FRAME_MS = 50  # 3 ticks at 60 Hz
MAX_PARTICLES = 256
CodeArtError = core.CodeArtError

M32 = 0xFFFFFFFF
DEG = math.pi / 180.0
TAU = 2.0 * math.pi


# ----------------------------------------------------------------------------- shared math (mirrored in fx-template.mjs)

def hash01(seed: int, index: int) -> float:
    """Deterministic value in [0, 1) for (seed, index): a 32-bit integer mix that the fx.v1
    runtime computes bit for bit the same way with Math.imul."""
    h = (seed & M32) ^ (((index + 1) * 0x9E3779B1) & M32)
    h = ((h ^ (h >> 16)) * 0x85EBCA6B) & M32
    h = ((h ^ (h >> 13)) * 0xC2B2AE35) & M32
    h ^= h >> 16
    return h / 4294967296.0


def stream(seed: int, primitive: int, variable: int) -> int:
    """Independent random stream per primitive and per random variable."""
    return (seed ^ (((primitive + 1) * 0x27D4EB2D) & M32) ^ ((variable * 0x165667B1) & M32)) & M32


def _clamp01(u: float) -> float:
    return 0.0 if u <= 0.0 else 1.0 if u >= 1.0 else u


def ease_out(u: float) -> float:
    r = 1.0 - _clamp01(u)
    return 1.0 - r * r * r


def ease_in(u: float) -> float:
    c = _clamp01(u)
    return c * c * c


def _ramp_colour(colors: Sequence[str], q: float) -> str:
    return colors[min(len(colors) - 1, math.floor(q * len(colors)))]


def _poly(points: list[list[float]], colour: str) -> dict:
    return {"kind": "poly", "points": points, "color": colour}


def _disc(cx: float, cy: float, r: float, colour: str) -> dict:
    return {"kind": "disc", "cx": cx, "cy": cy, "r": r, "color": colour}


def _slash(p: dict, t: float, seed: int, index: int, out: list) -> None:
    if t < p["startMs"]:
        return
    head = ease_out((t - p["startMs"]) / max(1.0, p["endMs"] - p["startMs"]))
    colors = p["colors"]
    layers = min(3, len(colors))
    if t <= p["endMs"]:
        tail, thin, shift = max(0.0, head - p["trail"]), 1.0, 0
    else:
        e = ease_in((t - p["endMs"]) / max(1.0, p["fadeMs"]))
        base = max(0.0, 1.0 - p["trail"])
        tail, thin = base + (1.0 - base) * e, 1.0 - 0.7 * e
        shift = min(len(colors) - layers, math.floor(e * (len(colors) - layers + 1)))
    if head - tail <= 0.002:
        return
    start, end = p["from"] * DEG, p["to"] * DEG
    tail_angle, head_angle = start + (end - start) * tail, start + (end - start) * head
    segments = max(6, math.ceil(abs(head_angle - tail_angle) * p["radius"] / 1.5))
    cx, cy = p["center"]
    for layer in range(layers):
        factor = 1.0 - layer / layers
        outer, inner = [], []
        for i in range(segments + 1):
            u = i / segments
            angle = tail_angle + (head_angle - tail_angle) * u
            half = p["width"] * factor * thin * math.sin(math.pi * u * u) / 2.0
            c, s = math.cos(angle), math.sin(angle)
            outer.append([cx + (p["radius"] + half) * c, cy + (p["radius"] + half) * s * p["squash"]])
            inner.append([cx + (p["radius"] - half) * c, cy + (p["radius"] - half) * s * p["squash"]])
        out.append(_poly(outer + inner[::-1], colors[shift + layers - 1 - layer]))


def _sparks(p: dict, t: float, seed: int, index: int, out: list) -> None:
    tau = t - p["atMs"]
    if tau < 0.0:
        return
    first, second, third = (stream(seed, index, variable) for variable in range(3))
    ox, oy = p["origin"]
    for i in range(p["count"]):
        life = p["lifeMs"] * (0.6 + 0.4 * hash01(third, i))
        if tau >= life:
            continue
        q = tau / life
        angle = (p["angle"] + (hash01(first, i) - 0.5) * p["spread"]) * DEG
        speed = p["speed"] * (0.5 + 0.5 * hash01(second, i))
        distance = speed * life / 1000.0 * ease_out(q)
        seconds = tau / 1000.0
        c, s = math.cos(angle), math.sin(angle)
        hx, hy = ox + c * distance, oy + s * distance + 0.5 * p["gravity"] * seconds * seconds
        length = p["length"] * (1.0 - q)
        out.append({"kind": "capsule", "x1": hx - c * length, "y1": hy - s * length, "x2": hx, "y2": hy,
                    "w": p["width"], "color": _ramp_colour(p["colors"], q)})


def _ring(p: dict, t: float, seed: int, index: int, out: list) -> None:
    tau = t - p["atMs"]
    if tau < 0.0 or tau >= p["lifeMs"]:
        return
    q = tau / p["lifeMs"]
    r0, r1 = p["radius"]
    w0, w1 = p["width"]
    radius = r0 + (r1 - r0) * ease_out(q)
    width = w0 + (w1 - w0) * q
    outer, inner = radius + width / 2.0, max(0.0, radius - width / 2.0)
    out.append({"kind": "ring", "cx": p["origin"][0], "cy": p["origin"][1], "rx": outer, "ry": outer * p["squash"],
                "irx": inner, "iry": inner * p["squash"], "color": _ramp_colour(p["colors"], q)})


def _flash(p: dict, t: float, seed: int, index: int, out: list) -> None:
    tau = t - p["atMs"]
    if tau < 0.0 or tau >= p["lifeMs"]:
        return
    q = tau / p["lifeMs"]
    colors = p["colors"]
    count = len(colors)
    core_radius = p["radius"] * (1.0 - q)
    ray = p["rayLength"] * (1.0 - 0.5 * q)
    inner = max(core_radius * 0.5, 0.75)
    ox, oy = p["origin"]
    points = []
    for k in range(2 * p["rays"]):
        angle = (p["rotation"] + k * 180.0 / p["rays"]) * DEG
        reach = ray if k % 2 == 0 else inner
        points.append([ox + reach * math.cos(angle), oy + reach * math.sin(angle)])
    star = colors[min(count - 1, 1 + math.floor(q * (count - 1)))] if count > 1 else colors[0]
    out.append(_poly(points, star))
    if core_radius >= 0.5:
        out.append(_disc(ox, oy, core_radius, _ramp_colour(colors, q)))


def _dust(p: dict, t: float, seed: int, index: int, out: list) -> None:
    first, second, third = (stream(seed, index, variable) for variable in range(3))
    ox, oy = p["origin"]
    r0, r1 = p["radius"]
    for i in range(p["count"]):
        a, b, c = hash01(first, i), hash01(second, i), hash01(third, i)
        delay = 0.25 * p["lifeMs"] * i / p["count"]  # staggered: the first puff shows at atMs
        life = p["lifeMs"] * (0.7 + 0.3 * c)
        tau = t - p["atMs"] - delay
        if tau < 0.0 or tau >= life:
            continue
        q = tau / life
        e = ease_out(q)
        side = -1.0 if a < 0.5 else 1.0
        x = ox + (a - 0.5) * p["spread"] + side * p["drift"] * e
        y = oy - p["rise"] * e * (0.6 + 0.4 * b)
        out.append(_disc(x, y, r0 + (r1 - r0) * e, _ramp_colour(p["colors"], q)))


def triangle(x: float) -> float:
    """Triangle wave with period 1: 0 at 0, 1 at 0.25, 0 at 0.5, -1 at 0.75. Its constant speed
    keeps loop steps even (a sampled sine has tiny steps at its peaks and big ones between)."""
    return 4.0 * abs(math.fmod(math.fmod(x - 0.25, 1.0) + 1.0, 1.0) - 0.5) - 1.0


def _projectile(p: dict, t: float, seed: int, index: int, out: list) -> None:
    phase = math.fmod(t, p["periodMs"]) / p["periodMs"]
    colors = p["colors"]
    glow = colors[min(1, len(colors) - 1)]
    angle = p["angle"] * DEG
    c, s = math.cos(angle), math.sin(angle)
    ox, oy = p["origin"]
    radius = p["radius"] * (1.0 + 0.15 * triangle(phase))
    trail = p["trail"] * (1.0 + 0.1 * triangle(2.0 * phase))
    left, right = [], []
    for i in range(9):
        u = i / 8.0
        half = radius * (1.0 - u) * 0.9
        x, y = ox - c * trail * u, oy - s * trail * u
        left.append([x - s * half, y + c * half])
        right.append([x + s * half, y - c * half])
    out.append(_poly(left + right[::-1], colors[-1]))
    out.append(_disc(ox, oy, radius * 1.35, glow))
    out.append(_disc(ox, oy, radius, colors[0]))
    for k in range(p["orbiters"]):
        orbit = TAU * (k / p["orbiters"] + phase)
        out.append(_disc(ox + math.cos(orbit) * radius * 1.9, oy + math.sin(orbit) * radius * 1.9, 0.9, glow))


PRIMITIVES: dict[str, Callable[[dict, float, int, int, list], None]] = {
    "slash": _slash, "sparks": _sparks, "ring": _ring, "flash": _flash, "dust": _dust, "projectile": _projectile}


def effect_shapes(effect: dict, t: float, seed: int | None = None) -> list[dict]:
    """The shapes of a normalised effect at time t (ms since spawn), in draw order."""
    seed = effect["seed"] if seed is None else seed
    shapes: list[dict] = []
    for index, primitive in enumerate(effect["primitives"]):
        PRIMITIVES[primitive["type"]](primitive, t, seed, index, shapes)
    return shapes


def slash_progress(primitive: dict, t: float) -> float:
    """Head progress of a slash primitive at t: exactly 1 from its endMs on."""
    if t < primitive["startMs"]:
        return 0.0
    return ease_out((t - primitive["startMs"]) / max(1.0, primitive["endMs"] - primitive["startMs"]))


# ----------------------------------------------------------------------------- spec normalisation

def _finite(value: Any, label: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    number = rig._finite(value, label)
    if minimum is not None and number < minimum:
        raise CodeArtError(f"{label} must be >= {minimum:g}")
    if maximum is not None and number > maximum:
        raise CodeArtError(f"{label} must be <= {maximum:g}")
    return number


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CodeArtError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def _pair(value: Any, label: str, *, minimum: float | None = None) -> list[float]:
    x, y = rig._point_value(value, label)
    if minimum is not None and min(x, y) < minimum:
        raise CodeArtError(f"{label} values must be >= {minimum:g}")
    return [x, y]


class FxSpec:
    """A validated codeart2d.fx.v1 spec with every default filled in (what the runtime embeds)."""

    def __init__(self, data: Any, *, base: Path, label: str = "spec") -> None:
        if not isinstance(data, dict):
            raise CodeArtError(f"{label} must be a JSON object")
        if data.get("schema") != FX_SCHEMA:
            raise CodeArtError(f"{label}: schema must be {FX_SCHEMA!r}")
        canvas = data.get("canvas", [48, 48])
        if not (isinstance(canvas, list) and len(canvas) == 2 and all(
                isinstance(v, int) and not isinstance(v, bool) and 4 <= v <= 1024 for v in canvas)):
            raise CodeArtError(f"{label}: canvas must be [width, height] in whole pixels (4..1024)")
        self.canvas = canvas
        self.origin = _pair(data.get("origin", [canvas[0] / 2, canvas[1] / 2]), f"{label} origin")
        self.palette = self._palette(data.get("palette"), base, label)
        effects = data.get("effects")
        if not isinstance(effects, list) or not effects:
            raise CodeArtError(f"{label}: effects must be a non-empty list")
        self.effects = [self._effect(effect, f"{label} effects[{index}]") for index, effect in enumerate(effects)]
        ids = [effect["id"] for effect in self.effects]
        if len(set(ids)) != len(ids):
            raise CodeArtError(f"{label}: effect ids must be unique")

    def _palette(self, raw: Any, base: Path, label: str) -> dict[str, str]:
        if raw is None:
            raise CodeArtError(f"{label}: palette is required (exact colours keep FX on palette)")
        source = base / raw if isinstance(raw, str) else raw
        try:
            parsed = core.parse_palette(source)
        except CodeArtError as exc:
            raise CodeArtError(f"{label} palette: {exc}") from None
        colours = {}
        for name, value in parsed.resolve().items():
            if value[3] != 255:
                raise CodeArtError(f"{label}: palette colour {name!r} must be opaque")
            colours[name] = core.rgba_to_hex(value)
        return colours

    def colour(self, value: Any, label: str) -> str:
        """A palette colour by name or by an exact hex value that is in the palette."""
        if isinstance(value, str) and value in self.palette:
            return self.palette[value]
        try:
            hexed = core.rgba_to_hex(core.hex_to_rgba(value))
        except CodeArtError:
            raise CodeArtError(f"{label}: {value!r} is neither a palette name nor a colour") from None
        if hexed not in self.palette.values():
            raise CodeArtError(f"{label}: colour {hexed} is not in the palette (exact palette only)")
        return hexed

    def _effect(self, raw: Any, label: str) -> dict:
        if not isinstance(raw, dict):
            raise CodeArtError(f"{label} must be an object")
        effect_id = raw.get("id")
        if not isinstance(effect_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", effect_id):
            raise CodeArtError(f"{label}: id must be ASCII letters, digits, '-' or '_' (it names the frame files)")
        label = f"effect {effect_id}"
        duration = _integer(raw.get("durationMs"), f"{label} durationMs", 1, 60000)
        impact = _integer(raw.get("impactMs"), f"{label} impactMs", 0, duration)
        loop = rig._bool_value(raw.get("loop", False), f"{label} loop")
        if not loop and impact >= duration:
            raise CodeArtError(f"{label}: impactMs must be below durationMs so the hit frame is visible")
        effect = {
            "id": effect_id, "durationMs": duration, "impactMs": impact, "loop": loop,
            "seed": _integer(raw.get("seed"), f"{label} seed", 0, 2**31 - 1),
            "frameMs": _integer(raw.get("frameMs", DEFAULT_FRAME_MS), f"{label} frameMs", 1, duration),
            "outline": None if raw.get("outline") is None else self.colour(raw["outline"], f"{label} outline"),
        }
        raw_ramps = raw.get("ramps", {})
        if not isinstance(raw_ramps, dict):
            raise CodeArtError(f"{label}: ramps must map names to colour lists")
        ramps = {name: self._ramp(colours, f"{label} ramp {name}") for name, colours in raw_ramps.items()}
        primitives = raw.get("primitives")
        if not isinstance(primitives, list) or not primitives:
            raise CodeArtError(f"{label}: primitives must be a non-empty list")
        effect["primitives"] = [self._primitive(primitive, effect, ramps, f"{label} primitives[{index}]")
                                for index, primitive in enumerate(primitives)]
        events = []
        for index, event in enumerate(raw.get("events", [])):
            if not isinstance(event, dict):
                raise CodeArtError(f"{label} events[{index}] must be {{atMs, name}}")
            events.append({"atMs": _integer(event.get("atMs"), f"{label} events[{index}].atMs", 0, duration - 1),
                           "name": rig._event_name(event.get("name"), f"{label} events[{index}].name")})
        effect["events"] = events
        return effect

    def _ramp(self, colours: Any, label: str) -> list[str]:
        if not isinstance(colours, list) or not colours:
            raise CodeArtError(f"{label} must be a non-empty list of palette colours, brightest first")
        return [self.colour(value, f"{label}[{index}]") for index, value in enumerate(colours)]

    def _primitive(self, raw: Any, effect: dict, ramps: dict[str, list[str]], label: str) -> dict:
        if not isinstance(raw, dict) or raw.get("type") not in PRESETS:
            raise CodeArtError(f"{label}: type must be one of {', '.join(PRESETS)}")
        kind = raw["type"]
        size = float(min(self.canvas))
        duration, impact = effect["durationMs"], effect["impactMs"]
        if "colors" in raw and "ramp" in raw:
            raise CodeArtError(f"{label}: give ramp (a ramp name) or colors, not both")
        if "ramp" in raw:
            if raw["ramp"] not in ramps:
                raise CodeArtError(f"{label}: unknown ramp {raw['ramp']!r}")
            colors = ramps[raw["ramp"]]
        elif "colors" in raw:
            colors = self._ramp(raw["colors"], f"{label} colors")
        else:
            colors = next(iter(ramps.values()), list(self.palette.values()))
        known = {"type", "ramp", "colors"}

        def number(key: str, default: float, **limits: float) -> float:
            known.add(key)
            return _finite(raw.get(key, default), f"{label} {key}", **limits)

        def count(key: str, default: int, maximum: int = MAX_PARTICLES, minimum: int = 1) -> int:
            known.add(key)
            return _integer(raw.get(key, default), f"{label} {key}", minimum, maximum)

        def point(key: str, default: list[float]) -> list[float]:
            known.add(key)
            return _pair(raw.get(key, default), f"{label} {key}")

        def span(key: str, default: list[float]) -> list[float]:
            known.add(key)
            return _pair(raw.get(key, default), f"{label} {key}", minimum=0.0)

        def start(key: str, default: float | str) -> float:
            known.add(key)
            value = raw.get(key, default)
            if value == "impact":
                return float(impact)
            return _finite(value, f"{label} {key}", minimum=0.0, maximum=duration)

        origin = list(self.origin)
        primitive: dict[str, Any] = {"type": kind}
        if kind == "slash":
            primitive.update(center=point("center", origin), radius=number("radius", 0.35 * size, minimum=1.0),
                             **{"from": number("from", -150.0)}, to=number("to", 30.0),
                             width=number("width", max(2.0, 0.11 * size), minimum=0.5),
                             squash=number("squash", 1.0, minimum=0.05, maximum=4.0),
                             trail=number("trail", 0.5, minimum=0.0, maximum=1.0),
                             startMs=start("startMs", 0.0), endMs=start("endMs", "impact"))
            if primitive["endMs"] <= primitive["startMs"]:
                raise CodeArtError(f"{label}: the arc must sweep before it ends: endMs (default impactMs) must come "
                                   "after startMs")
            primitive["fadeMs"] = number("fadeMs", max(1.0, duration - primitive["endMs"]), minimum=1.0)
        elif kind == "sparks":
            primitive.update(origin=point("origin", origin), atMs=start("atMs", "impact"), count=count("count", 10),
                             angle=number("angle", -90.0), spread=number("spread", 360.0, minimum=0.0),
                             speed=number("speed", 1.6 * size, minimum=0.0),
                             lifeMs=number("lifeMs", min(250.0, float(duration)), minimum=1.0),
                             length=number("length", max(2.0, 0.1 * size), minimum=0.0),
                             width=number("width", 1.5, minimum=0.5), gravity=number("gravity", 0.0))
        elif kind == "ring":
            primitive.update(origin=point("origin", origin), atMs=start("atMs", "impact"),
                             lifeMs=number("lifeMs", min(250.0, float(duration)), minimum=1.0),
                             radius=span("radius", [0.08 * size, 0.42 * size]),
                             width=span("width", [max(1.5, 0.08 * size), 1.0]),
                             squash=number("squash", 1.0, minimum=0.05, maximum=4.0))
        elif kind == "flash":
            primitive.update(origin=point("origin", origin), atMs=start("atMs", "impact"),
                             lifeMs=number("lifeMs", min(120.0, float(duration)), minimum=1.0),
                             radius=number("radius", max(1.5, 0.12 * size), minimum=0.5),
                             rays=count("rays", 4, 16), rayLength=number("rayLength", 0.3 * size, minimum=1.0),
                             rotation=number("rotation", 0.0))
        elif kind == "dust":
            primitive.update(origin=point("origin", origin), atMs=start("atMs", 0.0), count=count("count", 6),
                             spread=number("spread", 0.4 * size, minimum=0.0),
                             rise=number("rise", 0.2 * size, minimum=0.0), drift=number("drift", 0.15 * size),
                             radius=span("radius", [max(1.0, 0.04 * size), max(1.5, 0.1 * size)]),
                             lifeMs=number("lifeMs", float(duration), minimum=1.0))
        else:
            primitive.update(origin=point("origin", origin), angle=number("angle", 0.0),
                             radius=number("radius", max(1.5, 0.12 * size), minimum=0.5),
                             trail=number("trail", 0.4 * size, minimum=0.0), orbiters=count("orbiters", 3, 16, 0),
                             periodMs=number("periodMs", float(duration), minimum=1.0))
            if effect["loop"] and abs(duration / primitive["periodMs"] - round(duration / primitive["periodMs"])) > 1e-9:
                raise CodeArtError(f"{label}: a looping projectile needs durationMs to be a whole number of periods")
        unknown = sorted(set(raw) - known)
        if unknown:
            raise CodeArtError(f"{label}: unknown {kind} parameter {unknown[0]!r}")
        primitive["colors"] = colors
        return primitive

    def runtime_data(self) -> dict:
        """The normalised spec the fx.v1 runtime embeds (identical numbers to the bake)."""
        return {"schema": FX_SCHEMA, "canvas": self.canvas, "origin": self.origin, "palette": self.palette,
                "effects": self.effects}


# ----------------------------------------------------------------------------- frames

def frame_schedule(effect: dict) -> tuple[list[int], int | None]:
    """Integer frame durations and the hit frame index. One-shots split [0, impactMs) and
    [impactMs, durationMs) into frames of about frameMs, so a frame starts exactly at impactMs."""
    duration, impact, step = effect["durationMs"], effect["impactMs"], effect["frameMs"]

    def parts(total: int) -> list[int]:
        count = max(1, math.floor(total / step + 0.5))
        return forge_core.frame_durations(total, min(count, total))

    if effect["loop"]:
        return parts(duration), None
    before = parts(impact) if impact > 0 else []
    return before + parts(duration - impact), len(before)


def svg_shape(shape: dict, colour: str, grow: float = 0.0) -> str:
    """One shape as an SVG element filled with `colour`, grown by `grow` px (outline pass)."""
    n = rig.svg_number
    kind = shape["kind"]
    if kind == "poly":
        points = " ".join(f"{n(x)},{n(y)}" for x, y in shape["points"])
        stroke = (f' stroke="{colour}" stroke-width="{n(2 * grow)}" stroke-linejoin="round"' if grow else "")
        return f'<polygon points="{points}" fill="{colour}"{stroke}/>'
    if kind == "disc":
        return f'<circle cx="{n(shape["cx"])}" cy="{n(shape["cy"])}" r="{n(shape["r"] + grow)}" fill="{colour}"/>'
    if kind == "ring":
        cx, cy = shape["cx"], shape["cy"]
        rx, ry = shape["rx"] + grow, shape["ry"] + grow
        irx, iry = shape["irx"] - grow, shape["iry"] - grow
        path = (f"M{n(cx + rx)} {n(cy)}A{n(rx)} {n(ry)} 0 1 0 {n(cx - rx)} {n(cy)}"
                f"A{n(rx)} {n(ry)} 0 1 0 {n(cx + rx)} {n(cy)}Z")
        if irx > 0.0 and iry > 0.0:
            path += (f"M{n(cx + irx)} {n(cy)}A{n(irx)} {n(iry)} 0 1 0 {n(cx - irx)} {n(cy)}"
                     f"A{n(irx)} {n(iry)} 0 1 0 {n(cx + irx)} {n(cy)}Z")
        return f'<path d="{path}" fill="{colour}" fill-rule="evenodd"/>'
    width = shape["w"] + 2 * grow
    if math.hypot(shape["x2"] - shape["x1"], shape["y2"] - shape["y1"]) < 1e-6:
        return f'<circle cx="{n(shape["x2"])}" cy="{n(shape["y2"])}" r="{n(width / 2)}" fill="{colour}"/>'
    return (f'<line x1="{n(shape["x1"])}" y1="{n(shape["y1"])}" x2="{n(shape["x2"])}" y2="{n(shape["y2"])}" '
            f'stroke="{colour}" stroke-width="{n(width)}" stroke-linecap="round"/>')


def shape_bbox(shape: dict) -> tuple[float, float, float, float]:
    kind = shape["kind"]
    if kind == "poly":
        xs, ys = [p[0] for p in shape["points"]], [p[1] for p in shape["points"]]
        return min(xs), min(ys), max(xs), max(ys)
    if kind == "disc":
        return shape["cx"] - shape["r"], shape["cy"] - shape["r"], shape["cx"] + shape["r"], shape["cy"] + shape["r"]
    if kind == "ring":
        return shape["cx"] - shape["rx"], shape["cy"] - shape["ry"], shape["cx"] + shape["rx"], shape["cy"] + shape["ry"]
    half = shape["w"] / 2.0
    return (min(shape["x1"], shape["x2"]) - half, min(shape["y1"], shape["y2"]) - half,
            max(shape["x1"], shape["x2"]) + half, max(shape["y1"], shape["y2"]) + half)


def _svg(width: int, height: int, body: str, view: tuple[int, int, int, int] | None = None) -> str:
    x0, y0, w, h = view or (0, 0, width, height)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{x0} {y0} {w} {h}">'
            f"{body}</svg>")


class FxRenderer:
    """Shapes to RGBA: pixel finishing per colour run, or one anti-aliased SVG."""

    def __init__(self, spec: FxSpec, *, route: str, zoom: int, ss: int, coverage: float, backend: str) -> None:
        self.spec, self.route, self.zoom, self.ss, self.coverage, self.backend = spec, route, zoom, ss, coverage, backend
        self.renderer_info: dict | None = None
        self._cache: dict[str, np.ndarray] = {}

    def render(self, effect: dict, shapes: list[dict]) -> np.ndarray:
        width, height = self.spec.canvas
        if self.route == "vector":
            outline = "".join(svg_shape(shape, effect["outline"], 1.0) for shape in shapes) if effect["outline"] else ""
            svg = _svg(width, height, outline + "".join(svg_shape(shape, shape["color"]) for shape in shapes))
            rgba, info = core.rasterize(svg, zoom=self.zoom, backend=self.backend)
            self.renderer_info = self.renderer_info or info
            return rgba
        runs: list[tuple[str, list[dict]]] = []
        for shape in shapes:  # consecutive shapes of one colour share a coverage slot
            if runs and runs[-1][0] == shape["color"]:
                runs[-1][1].append(shape)
            else:
                runs.append((shape["color"], [shape]))
        slots = []
        for index, (colour, members) in enumerate(runs):
            alpha = self._coverage(members)
            if alpha is not None:
                slots.append({"alpha": alpha, "fill": colour, "name": f"run{index}"})
        if not slots:
            return np.zeros((height, width, 4), np.uint8)
        rgba, _ = core.pixel_finish(slots, (width, height), ss=self.ss, coverage=self.coverage,
                                    outline="solid" if effect["outline"] else "none",
                                    outline_color=effect["outline"] or "#000000", light=None, inner_lines=False)
        return rgba

    def _coverage(self, shapes: list[dict]) -> np.ndarray | None:
        width, height = self.spec.canvas
        boxes = [shape_bbox(shape) for shape in shapes]
        x0, y0 = max(0, math.floor(min(b[0] for b in boxes)) - 1), max(0, math.floor(min(b[1] for b in boxes)) - 1)
        x1 = min(width, math.ceil(max(b[2] for b in boxes)) + 1)
        y1 = min(height, math.ceil(max(b[3] for b in boxes)) + 1)
        if x1 <= x0 or y1 <= y0:
            return None
        svg = _svg(width, height, "".join(svg_shape(shape, "#ffffff") for shape in shapes),
                   view=(x0, y0, x1 - x0, y1 - y0))
        region = self._cache.get(svg)
        if region is None:
            pixels, info = core.rasterize(svg, zoom=self.ss, backend=self.backend)
            self.renderer_info = self.renderer_info or info
            region = self._cache[svg] = pixels[..., 3]
        full = np.zeros((height * self.ss, width * self.ss), np.uint8)
        full[y0 * self.ss:y1 * self.ss, x0 * self.ss:x1 * self.ss] = region
        return full


def bake_effect(effect: dict, renderer: FxRenderer) -> dict:
    """Frames, durations, hit frame and dropped tail of one effect (frames sample mid-frame)."""
    durations, hit = frame_schedule(effect)
    starts = [sum(durations[:index]) for index in range(len(durations))]
    samples = [start + duration / 2.0 for start, duration in zip(starts, durations)]
    images = [renderer.render(effect, effect_shapes(effect, t)) for t in samples]
    visible = [bool(image[..., 3].any()) for image in images]
    keep = len(images)
    while not effect["loop"] and keep > 1 and not visible[keep - 1] and keep - 1 > hit:
        keep -= 1
    dropped = len(images) - keep
    if dropped:
        durations = durations[:keep - 1] + [sum(durations[keep - 1:])]
    empty = [index for index in range(keep) if not visible[index]]
    if empty:
        index = empty[0]
        raise CodeArtError(f"effect {effect['id']}: frame {index} (t={samples[index]:g} ms) is fully transparent; "
                           "build_animation_clips needs visible pixels in every frame. Start a primitive earlier "
                           "(startMs, atMs) or shorten the gap.")
    return {"images": images[:keep], "durations": durations, "starts": starts[:keep], "samples": samples[:keep],
            "hit": hit, "dropped_tail": dropped}


# ----------------------------------------------------------------------------- outputs

def event_frame(starts: Sequence[int], at_ms: float) -> int:
    """Index of the frame on screen at at_ms."""
    index = 0
    for position, start in enumerate(starts):
        if start <= at_ms:
            index = position
    return index


def export_runtime(spec: FxSpec, path: Path, spec_name: str) -> None:
    """fx-runtime.mjs: the template with the normalised spec in its data block."""
    template = RUNTIME_TEMPLATE.read_text(encoding="utf-8")
    start, end = template.find(DATA_START), template.find(DATA_END)
    if start < 0 or end < start:
        raise CodeArtError(f"runtime template {RUNTIME_TEMPLATE} lacks its {DATA_START} block")
    data = json.dumps(spec.runtime_data(), ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    header = f"// fx.v1 runtime generated by codeart2d fx_build.py from {forge_core.ascii_text(spec_name)}.\n"
    text = header + template[:start + len(DATA_START)] + data + template[end:]
    path.write_text(text, encoding="utf-8", newline="\n")


def verify_runtime(module: Path, report: Path) -> dict:
    """Run fx_verify.mjs when node is available; skipped (not failed) without node."""
    node = shutil.which("node")
    if node is None:
        return {"status": "skipped", "reason": "node is not on PATH"}
    try:
        result = subprocess.run([node, str(VERIFIER), str(module), "--report", str(report)], capture_output=True,
                                encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return {"status": "fail", "reason": "fx_verify.mjs timed out"}
    record = {"status": "pass" if result.returncode == 0 else "fail", "returncode": result.returncode}
    if result.returncode != 0:
        record["reason"] = forge_core.ascii_text((result.stderr or result.stdout).strip()[-400:])
    return record


def build(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output_dir)
    if os.path.lexists(output_dir):
        raise CodeArtError(f"refusing to replace existing output: {output_dir}")
    spec_path = Path(args.spec)
    spec = FxSpec(rig._strict_json(spec_path), base=spec_path.parent, label=spec_path.name)
    effects = spec.effects
    if args.effects:
        names = [name.strip() for name in args.effects.split(",")]
        unknown = [name for name in names if name not in {effect["id"] for effect in effects}]
        if unknown:
            raise CodeArtError(f"unknown effect {unknown[0]!r}")
        effects = [next(effect for effect in spec.effects if effect["id"] == name) for name in names]
    zoom = args.zoom if args.zoom is not None else (1 if args.route == "pixel" else 4)
    if zoom < 1 or (args.route == "pixel" and zoom != 1):
        raise CodeArtError("--zoom is a whole number >= 1 and only for the vector route")
    if not 2 <= args.ss <= 16 or not 0.0 < args.coverage < 1.0:
        raise CodeArtError("--ss must lie in 2..16 and --coverage in (0, 1)")
    renderer = FxRenderer(spec, route=args.route, zoom=zoom, ss=args.ss, coverage=args.coverage, backend=args.backend)
    palette = sorted(set(spec.palette.values()))
    seam_low, seam_high = args.seam_range
    margin_limit = args.margin * zoom

    baked, report_effects, frame_qa = {}, {}, []
    margin_failures, arc_records, seam_checks = [], [], []
    for effect in effects:
        result = bake_effect(effect, renderer)
        baked[effect["id"]] = result
        ids = [f"{effect['id']}-{index:02d}" for index in range(len(result["images"]))]
        frames = []
        for index, (frame_id, image) in enumerate(zip(ids, result["images"])):
            outline = effect["outline"] if args.route == "pixel" else None
            qa = core.qa_pixels(image, palette, outline) if args.route == "pixel" else core.qa_pixels(image)
            qa = {key: value for key, value in qa.items() if not key.endswith("_examples")}
            if args.route == "pixel":
                frame_qa.append((qa, effect["outline"] is not None))
            margin = rig.margin_px(image[..., 3])
            if margin is None or margin < margin_limit:
                margin_failures.append({"frame": frame_id, "margin_px": margin})
            frames.append({"id": frame_id, "start_ms": result["starts"][index], "duration_ms": result["durations"][index],
                           "sample_ms": result["samples"][index], "qa": qa, "margin_px": margin})
        hit = result["hit"]
        events = []
        if hit is not None:
            events.append({"at": hit, "name": "hit", "data": {"impactMs": effect["impactMs"]}})
            for primitive in effect["primitives"]:
                if primitive["type"] != "slash":
                    continue
                progress = [slash_progress(primitive, t) for t in result["samples"]]
                complete = next((index for index, value in enumerate(progress) if value >= 1.0), None)
                arc_records.append({"effect": effect["id"], "hit_frame": hit, "arc_complete_frame": complete,
                                    "progress": [round(value, 6) for value in progress]})
        events += [{"at": event_frame(result["starts"], event["atMs"]), "name": event["name"]}
                   for event in effect["events"]]
        seam = None
        if effect["loop"] and len(result["images"]) >= 2:
            seam = forge_core.seam_report(result["images"])
            if len(result["images"]) >= 3:
                ratio = seam["seam_over_median"]
                # warn, not fail: a smooth loop whose speed varies (a sine) can exceed the ratio without a pop
                seam_checks.append(rig.check(f"seam:{effect['id']}", seam_low <= ratio <= seam_high, round(ratio, 4),
                                             [seam_low, seam_high], warn=True))
            seam = {key: round(value, 6) if isinstance(value, float) else value for key, value in seam.items()}
        report_effects[effect["id"]] = {
            "durationMs": effect["durationMs"], "impactMs": effect["impactMs"], "loop": effect["loop"],
            "frames": frames, "duration_ms": result["durations"], "hit_frame": hit,
            "dropped_tail_frames": result["dropped_tail"], "events": events, "seam": seam,
            "hit_on_60hz_tick": effect["impactMs"] * 60 % 1000 == 0}

    with forge_core.staged_output(output_dir) as stage:
        rendered = [(f"{effect['id']}-{index:02d}", image) for effect in effects
                    for index, image in enumerate(baked[effect["id"]]["images"])]
        unique, stored_as = rig.dedupe_frames(rendered)
        files = {}
        for frame_id, image in unique:
            core.save_png(image, stage / "frames" / f"{frame_id}.png")
            files[frame_id] = f"frames/{frame_id}.png"
        clips = {}
        for effect in effects:
            record = report_effects[effect["id"]]
            for frame in record["frames"]:
                frame["file"] = files[stored_as[frame["id"]]]
            clip = {"frames": [stored_as[frame["id"]] for frame in record["frames"]],
                    "duration_ms": record["duration_ms"], "loop": effect["loop"],
                    "loop_policy": "cycle" if effect["loop"] else "oneshot", "role": "fx"}
            if record["events"]:
                clip["events"] = record["events"]
            clips[effect["id"]] = clip
        manifest = {"schema": rig.CLIPS_SCHEMAS[args.clips_schema],
                    "frames": [{"name": frame_id, "file": files[frame_id]} for frame_id, _ in unique],
                    "anchor_px": [spec.origin[0] * zoom, spec.origin[1] * zoom], "clips": clips,
                    "sampling": "nearest" if args.route == "pixel" else "linear",
                    "pixel_art": args.route == "pixel", "art_source": "code", "placeholder": False}
        clips_path = stage / "clips.json"
        forge_core.write_json(clips_path, manifest)
        build_record = rig.run_clips_builder(Path(args.clips_builder), clips_path, stage / "compiled-clips") \
            if args.build_clips else None
        runtime_record = None
        if args.export_runtime:
            module = stage / "fx-runtime.mjs"
            export_runtime(spec, module, spec_path.name)
            runtime_record = {"module": "fx-runtime.mjs"}
            if not args.no_verify:
                runtime_record["verify"] = verify_runtime(module, stage / "fx-verify.json")
        review = {}
        for effect in effects:
            images = baked[effect["id"]]["images"]
            sheet = core.review_sheet(images, scales=(1, 2, 4) if args.route == "pixel" else (1,), onion=True,
                                      palette=palette if args.route == "pixel" else None,
                                      qa=[frame["qa"] for frame in report_effects[effect["id"]]["frames"]]
                                      if args.route == "pixel" else None,
                                      anchor=(spec.origin[0] * zoom, spec.origin[1] * zoom),
                                      title=f"{effect['id']} ({args.route} route, code-drawn)", loop=effect["loop"])
            review[effect["id"]] = f"review/{effect['id']}.png"
            rig.save_review(sheet, stage / review[effect["id"]])

        checks = []
        for name, limit in core.QA_PIXEL_GATES:
            outlined = name in ("outline_gaps", "l_corners")
            values = [qa[name] for qa, has_outline in frame_qa if qa.get(name) is not None and (has_outline or not outlined)]
            if not values:
                checks.append(rig.check(name, None, None, limit))
            else:
                checks.append(rig.check(name, max(values) <= limit, max(values), limit, warn=name == "l_corners"))
        if arc_records:
            late = [record["effect"] for record in arc_records if record["arc_complete_frame"] != record["hit_frame"]]
            checks.append(rig.check("arc_on_hit", not late, late, "slash head completes on the hit frame"))
        checks += seam_checks
        checks.append(rig.check("margins", not margin_failures, len(margin_failures), margin_limit))
        totals = {effect["id"]: sum(report_effects[effect["id"]]["duration_ms"]) for effect in effects}
        expected = {effect["id"]: effect["durationMs"] for effect in effects}
        checks.append(rig.check("timing", totals == expected, totals, expected))
        checks.append(rig.check("build_clips", None if build_record is None else build_record["returncode"] == 0,
                                None if build_record is None else build_record["returncode"], 0))
        verify = (runtime_record or {}).get("verify")
        checks.append(rig.check("runtime_verify", None if verify is None or verify["status"] == "skipped"
                                else verify["status"] == "pass", None if verify is None else verify["status"], "pass"))
        final = output_dir.parent.resolve() / output_dir.name
        outputs = [rig.output_ref(stage / files[frame_id], stage) for frame_id, _ in unique] + \
            [rig.output_ref(clips_path, stage)]
        if runtime_record:
            outputs.append(rig.output_ref(stage / "fx-runtime.mjs", stage))
        inputs = [rig.input_ref(spec_path, final)]
        not_proven = ["Readability and impact at game scale and in motion: look at review/*.png and play the clips.",
                      "That the effect suits the move it accompanies; the checks prove timing and palette only."]
        if args.route == "vector":
            not_proven.append("Vector frames are anti-aliased: partial alpha and blended edges are expected.")
        if build_record is None:
            not_proven.append("build_animation_clips was not run (--build-clips not given).")
        if verify is not None and verify["status"] == "skipped":
            not_proven.append("The fx.v1 runtime was not verified: node is not on PATH.")
        envelope = rig.qa_envelope(
            checks, tool=TOOL, inputs=inputs, outputs=outputs, not_proven=not_proven,
            method=("fx_build: frames cut at impactMs and sampled mid-frame; codeart_core.qa_pixels per frame against "
                    "the spec palette (pixel route); slash head progress at each frame; forge_core.seam_report on "
                    "loops; transparent margin; optional build_animation_clips and fx_verify.mjs runs"))
        report = {"schema": FX_REPORT_SCHEMA, "route": args.route, "canvas": [spec.canvas[0] * zoom, spec.canvas[1] * zoom],
                  "origin": [spec.origin[0] * zoom, spec.origin[1] * zoom], "zoom": zoom, "palette": palette,
                  "effects": report_effects, "arcs": arc_records, "margin_failures": margin_failures,
                  "build_clips": build_record, "runtime": runtime_record, "review": review, "qa": envelope}
        forge_core.write_json(stage / "fx-report.json", report)
        renderer_record = {**(renderer.renderer_info or {"name": "none", "version": "0"}), "route": args.route,
                           "zoom": zoom, "size": [spec.canvas[0] * zoom, spec.canvas[1] * zoom]}
        if args.route == "pixel":
            renderer_record.update({"coverage_ss": args.ss, "coverage": args.coverage,
                                    "finish": "codeart_core.pixel_finish route D"})
        meta = core.write_codeart_meta(
            stage / "codeart-meta.json", generator=TOOL, spec_sha256=forge_core.sha256_file(spec_path),
            renderer=renderer_record, palette=spec.palette,
            outputs=[stage / files[frame_id] for frame_id, _ in unique] + [clips_path] +
            ([stage / "fx-runtime.mjs"] if runtime_record else []), qa=envelope,
            extra={"route": args.route, "spec": inputs[0]})
        if args.strict_qc and envelope["status"] == "fail":
            failed = [item["id"] for item in envelope["checks"] if item["status"] == "fail"]
            raise CodeArtError(f"strict QC failed ({', '.join(failed)}); nothing was published. "
                               "Run without --strict-qc to inspect fx-report.json")
    return {"status": "ok", "qa": meta["qa"]["status"], "output": str(final),
            "metadata": str(final / "codeart-meta.json"), "report": str(final / "fx-report.json"),
            "effects": [effect["id"] for effect in effects], "frames": len(unique), "route": args.route,
            "runtime": str(final / "fx-runtime.mjs") if runtime_record else None}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fx_build.py",
        description="Bake code-drawn FX (slash, sparks, ring, flash, dust, projectile) from a codeart2d.fx.v1 spec "
                    "into exact-palette frames, a build_animation_clips manifest with hit events and, optionally, "
                    "the fx.v1 canvas runtime. Code-drawn, no image model.",
        epilog="Example: python \"<skill-dir>/scripts/fx_build.py\" --spec slash.fx.json --output-dir out/fx-slash-v1 "
               "--route pixel --build-clips --export-runtime. See references/fx-language.md.")
    parser.add_argument("--spec", required=True, help="codeart2d.fx.v1 JSON")
    parser.add_argument("--output-dir", required=True, help="new output directory (refused when it exists)")
    parser.add_argument("--route", choices=("pixel", "vector"), default="pixel",
                        help="pixel: exact palette, binary alpha (default); vector: anti-aliased")
    parser.add_argument("--effects", help="comma-separated effect ids (default: all)")
    parser.add_argument("--zoom", type=int, help="vector route integer zoom (default 4); the pixel route is 1")
    parser.add_argument("--ss", type=int, default=8, help="pixel route coverage supersampling (default 8)")
    parser.add_argument("--coverage", type=float, default=0.5, help="pixel route coverage threshold (default 0.5)")
    parser.add_argument("--backend", choices=("auto",) + core.RASTER_BACKENDS, default="auto",
                        help="SVG rasterizer (default auto: resvg-py, then resvg-js CLI, then Chrome)")
    parser.add_argument("--margin", type=int, default=1, help="minimum transparent margin in canvas pixels (default 1)")
    parser.add_argument("--seam-range", type=rig._seam_range, default=rig.SEAM_RANGE,
                        help="allowed loop seam / median step ratio for looping effects (default 0.8,1.25)")
    parser.add_argument("--build-clips", action="store_true",
                        help="run generate2dsprite build_animation_clips.py on clips.json (into compiled-clips/)")
    parser.add_argument("--clips-builder", default=str(rig.DEFAULT_BUILDER),
                        help="path to build_animation_clips.py (default: the sibling generate2dsprite skill)")
    parser.add_argument("--clips-schema", choices=tuple(rig.CLIPS_SCHEMAS), default="v1",
                        help="clips.json schema id (default v1; v2 for clips v2 builders)")
    parser.add_argument("--export-runtime", action="store_true",
                        help="also write fx-runtime.mjs (fx.v1 canvas runtime with the same geometry)")
    parser.add_argument("--no-verify", action="store_true", help="do not run fx_verify.mjs on the exported runtime")
    parser.add_argument("--strict-qc", action="store_true", help="exit 1 and publish nothing when a QA check fails")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        summary = build(args)
    except (CodeArtError, core.RasterError, ValueError, OSError) as exc:
        print(forge_core.ascii_text(f"error: {rig.describe_error(exc)}"), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
