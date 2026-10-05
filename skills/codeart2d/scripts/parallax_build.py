#!/usr/bin/env python3
"""Build stylised parallax layers from a code-art spec (codeart2d; no image model).

Every generated repeating layer is periodic by construction: ridge lines use a
wrapping noise lattice, and trees, clouds, stars and grass are drawn at periodic
distances. That is proven per layer by rendering it twice as wide and comparing
the result with the layer tiled twice (zero differing pixels). The layer origin is
then rolled to its calmest column, so the wrap step is the smallest column step
(loop step <= p95 of the column steps). Canvases are sized from the camera
envelope so they cover the viewport at every camera and zoom extreme.

Outputs (staged, published only when complete): one PNG per layer, a parallax
plan (generate2dmap parallax_plan; screenTopLeft = (offset - anchor_px * scale -
camera * scroll_factor) * zoom, top-left pivot), the report of the sibling
generate2dmap validate_parallax.py, a camera sweep (sweep-sheet.png and
sweep.webp), parallax-qa.json (QA envelope) and codeart-meta.json.

Run from the project root:
  python "<skill-dir>/scripts/parallax_build.py" --spec parallax-gen.json --output-dir out/bg-v1 --validate --sweep-frames 49
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence

try:
    import numpy as np
    from PIL import Image, UnidentifiedImageError, features
except ImportError as _missing:  # an environment problem: report it without a traceback
    sys.stderr.write(f"error: missing Python module {_missing.name}; install with: "
                     "python -m pip install numpy Pillow scipy\n")
    raise SystemExit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codeart_core  # noqa: E402
import forge_core  # noqa: E402


TOOL = "parallax_build"
TOOL_VERSION = "1.0"
SPEC_SCHEMA = "codeart2d.parallax_spec.v1"
PLAN_SCHEMA = "generate2dmap.parallax_plan.v1"
KINDS = ("sky", "ridge", "clouds", "foreground", "image")
ROLE_DEFAULTS = {"sky": "sky", "ridge": "far", "clouds": "far", "foreground": "foreground"}
SUPERSAMPLE = 4
LOOP_STEP_LIMIT = 1.0  # wrap step / p95 of the layer's own column steps
SHEET_MAX_WIDTH = 2048
BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], np.float64) / 16.0 + 1.0 / 32.0
VALIDATOR = Path(__file__).resolve().parents[2] / "generate2dmap" / "scripts" / "validate_parallax.py"


class ParallaxError(ValueError):
    """The parallax spec or an input it names is invalid."""


class ParallaxQAError(ValueError):
    """Strict QC failed; nothing is published."""


# ----------------------------------------------------------------------------- helpers

def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def _num(value: float) -> int | float:
    rounded = round(float(value), 4)
    return int(rounded) if rounded == int(rounded) else rounded


def _mapping(value: Any, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ParallaxError(f"{label} must be a JSON object")
    return value


def _finite(value: Any, label: str, *, minimum: float | None = None, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ParallaxError(f"{label} must be a finite number")
    if positive and value <= 0:
        raise ParallaxError(f"{label} must be greater than 0")
    if minimum is not None and value < minimum:
        raise ParallaxError(f"{label} must be at least {minimum}")
    return float(value)


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParallaxError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ParallaxError(f"{label} must be at least {minimum}")
    return int(value)


def _pair(value: Any, label: str, *, positive: bool = False) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ParallaxError(f"{label} must contain two numbers")
    return _finite(value[0], label, positive=positive), _finite(value[1], label, positive=positive)


def _range(value: Any, label: str, *, positive: bool = False) -> tuple[float, float]:
    low, high = _pair(value, label, positive=positive)
    if low > high:
        raise ParallaxError(f"{label} must be ordered [minimum, maximum]")
    return low, high


def _colour(value: Any, label: str) -> tuple[int, int, int, int]:
    try:
        return codeart_core.hex_to_rgba(value)
    except codeart_core.CodeArtError as error:
        raise ParallaxError(f"{label}: {error}") from None


def _relative(path: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:
        return path.name


def _file_ref(path: Path, base: Path) -> dict:
    return {"path": _relative(path, base), "sha256": forge_core.sha256_file(path), "bytes": path.stat().st_size}


def _block_mean(fine: np.ndarray, scale: int) -> np.ndarray:
    """Premultiplied s x s box reduction of a float RGBA image in 0..255 (exact, periodic-safe)."""
    if scale == 1:
        return np.floor(fine + 0.5).clip(0, 255).astype(np.uint8)
    height, width = fine.shape[0] // scale, fine.shape[1] // scale
    premultiplied = fine.copy()
    premultiplied[..., :3] *= premultiplied[..., 3:] / 255.0
    blocks = premultiplied.reshape(height, scale, width, scale, 4).mean(axis=(1, 3))
    out = np.zeros_like(blocks)
    alpha = blocks[..., 3:]
    np.divide(blocks[..., :3] * 255.0, alpha, out=out[..., :3], where=alpha > 0)
    out[..., 3:] = alpha
    return np.floor(out + 0.5).clip(0, 255).astype(np.uint8)


# ----------------------------------------------------------------------------- spec model

@dataclass
class Layer:
    id: str
    kind: str
    role: str
    scroll: tuple[float, float]
    repeat: tuple[bool, bool]
    period: int
    params: Mapping
    seed: int
    index: int
    source: Path | None = None
    image: np.ndarray | None = None
    pad_top: int = 0
    pad_left: int = 0
    height: int = 0
    width: int = 0
    alpha: str = "transparent"
    anchor: tuple[float, float] = (0.0, 0.0)
    offset: tuple[float, float] = (0.0, 0.0)
    scale: float = 1.0
    require_coverage: bool = True
    rolled: int = 0


@dataclass
class Spec:
    path: Path
    raw: bytes
    viewport: tuple[int, int]
    camera: dict
    seed: int
    pixel_art: bool
    sweep_frames: int
    layers: list[Layer]


def load_spec(path: Path, sweep_override: int | None) -> Spec:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ParallaxError(f"spec not found: {path}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ParallaxError(f"spec {path.name} is not valid UTF-8 JSON: {error}") from None
    data = _mapping(data, "spec")
    if data.get("schema") != SPEC_SCHEMA:
        raise ParallaxError(f"spec schema must be {SPEC_SCHEMA!r} (got {data.get('schema')!r})")
    viewport = data.get("viewport")
    if not isinstance(viewport, (list, tuple)) or len(viewport) != 2:
        raise ParallaxError("viewport must be [width, height] in pixels")
    viewport = (_integer(viewport[0], "viewport", minimum=1), _integer(viewport[1], "viewport", minimum=1))
    camera_raw = _mapping(data.get("camera", {}), "camera")
    camera = {"x": _range(camera_raw.get("x", [0, 0]), "camera.x"),
              "y": _range(camera_raw.get("y", [0, 0]), "camera.y"),
              "zoom": _range(camera_raw.get("zoom", [1, 1]), "camera.zoom", positive=True)}
    seed = _integer(data.get("seed", 0), "seed", minimum=0)
    pixel_art = data.get("pixel_art", True)
    if not isinstance(pixel_art, bool):
        raise ParallaxError("pixel_art must be true or false")
    sweep = sweep_override if sweep_override is not None else _integer(data.get("sweep_frames", 49), "sweep_frames",
                                                                         minimum=0)
    if sweep == 1:
        raise ParallaxError("a camera sweep needs 0 (off) or at least 2 frames")
    raw_layers = data.get("layers")
    if not isinstance(raw_layers, list) or not raw_layers:
        raise ParallaxError("layers must be a nonempty list, back to front")
    layers, ids = [], set()
    for index, item in enumerate(raw_layers):
        label = f"layers[{index}]"
        item = _mapping(item, label)
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or not all(c.isalnum() or c in "-_." for c in identity) \
                or identity in ids:
            raise ParallaxError(f"{label}.id must be a unique id of letters, digits, '-', '_' or '.'")
        ids.add(identity)
        kind = item.get("kind")
        if kind not in KINDS:
            raise ParallaxError(f"{label}.kind must be one of {', '.join(KINDS)}")
        role = item.get("role", ROLE_DEFAULTS.get(kind))
        if not isinstance(role, str) or not role.strip():
            raise ParallaxError(f"{label}.role is required for image layers (sky, far, mid, near or foreground)")
        if kind == "sky":
            scroll_raw = item.get("scroll", [0, 0])
        elif "scroll" not in item:
            raise ParallaxError(f"{label}.scroll is required: the camera factor, e.g. 0.2 for far layers, "
                                "1.0 for the play layer")
        else:
            scroll_raw = item["scroll"]
        scroll = (_finite(scroll_raw, f"{label}.scroll"), 0.0) if isinstance(scroll_raw, (int, float)) \
            and not isinstance(scroll_raw, bool) else _pair(scroll_raw, f"{label}.scroll")
        # generated layers repeat by default (the sky only when it scrolls); image layers only when declared
        repeat_default = [kind not in ("sky", "image") or (kind == "sky" and scroll[0] != 0), False]
        repeat_raw = item.get("repeat", repeat_default)
        if not isinstance(repeat_raw, (list, tuple)) or len(repeat_raw) != 2 or \
                not all(isinstance(v, bool) for v in repeat_raw):
            raise ParallaxError(f"{label}.repeat must be [true|false, true|false]")
        if repeat_raw[1] and kind != "image":
            raise ParallaxError(f"{label}: generated layers repeat horizontally only")
        period = _integer(item.get("period", viewport[0]), f"{label}.period", minimum=8)
        layer = Layer(identity, kind, role.strip(), scroll, (repeat_raw[0], repeat_raw[1]), period, item,
                      _integer(item.get("seed", seed * 1000 + index), f"{label}.seed", minimum=0), index)
        if kind == "image":
            source = (path.resolve().parent / str(item.get("image", ""))).resolve()
            try:
                image, _ = forge_core.load_rgba(source)
            except FileNotFoundError:
                raise ParallaxError(f"{label}.image not found: {source}") from None
            except UnidentifiedImageError:
                raise ParallaxError(f"{label}.image is not a readable image: {source}") from None
            layer.source, layer.image = source, np.asarray(image).copy()
            layer.scale = _finite(item.get("scale", 1), f"{label}.scale", positive=True)
            layer.anchor = _pair(item.get("anchor_px", [0, 0]), f"{label}.anchor_px")
            layer.offset = _pair(item.get("offset", [0, 0]), f"{label}.offset")
            coverage = item.get("require_canvas_coverage", True)
            if not isinstance(coverage, bool):
                raise ParallaxError(f"{label}.require_canvas_coverage must be true or false")
            layer.require_coverage = coverage
        elif (layer.repeat[0] and kind == "sky" and item.get("bands", 8 if pixel_art else 0)
              and item.get("dither", True) and period % 4):
            raise ParallaxError(f"{label}: a repeating dithered sky needs a period divisible by 4")
        layers.append(layer)
    skies = [layer for layer in layers if layer.role == "sky"]
    if len(skies) != 1:
        raise ParallaxError("the spec needs exactly one layer with role sky (the opaque base)")
    return Spec(path, raw, viewport, camera, seed, pixel_art, sweep, layers)


def size_canvases(spec: Spec) -> None:
    """Pick each generated layer's canvas so it covers the viewport at every camera and zoom extreme.

    Content is authored in viewport pixels at camera 0 and zoom 1; a canvas taller than the
    viewport keeps that alignment through offset_y = -pad_top."""
    vw, vh = spec.viewport
    zoom_min = spec.camera["zoom"][0]
    for layer in spec.layers:
        if layer.kind == "image":
            layer.height, layer.width = layer.image.shape[:2]
            layer.alpha = "opaque" if int(layer.image[..., 3].min()) == 255 else "transparent"
            layer.alpha = layer.params.get("alpha", layer.alpha)
            if layer.alpha not in ("opaque", "transparent"):
                raise ParallaxError(f"layer {layer.id}: alpha must be opaque or transparent")
            continue
        fy = layer.scroll[1]
        shifts_y = [cam * fy for cam in spec.camera["y"]]
        top = min(0.0, min(shifts_y))
        layer.pad_top = int(math.ceil(-top))
        layer.height = int(math.ceil(vh / zoom_min + max(0.0, max(shifts_y)) + layer.pad_top))
        if layer.repeat[0]:
            layer.width = layer.period
        else:
            shifts_x = [cam * layer.scroll[0] for cam in spec.camera["x"]]
            left = min(0.0, min(shifts_x))
            layer.pad_left = int(math.ceil(-left))
            layer.width = int(math.ceil(vw / zoom_min + max(0.0, max(shifts_x)) + layer.pad_left))
        layer.offset = (-float(layer.pad_left), -float(layer.pad_top))
        layer.alpha = "opaque" if layer.kind == "sky" else "transparent"


# ----------------------------------------------------------------------------- generators

ROW_BAND = 64  # output rows rendered at a time, so supersampling never holds a whole fine canvas


class PeriodicNoise:
    """fBm in [0, 1] with period 1 in u: every octave's lattice wraps (smoothstep interpolation)."""

    def __init__(self, cells: int, octaves: int, rng: np.random.Generator):
        self.lattices = [rng.random(cells * 2 ** octave) for octave in range(octaves)]

    def __call__(self, u: Any) -> np.ndarray:
        u = np.asarray(u, np.float64)
        total = np.zeros(u.shape)
        amplitude, norm = 1.0, 0.0
        for lattice in self.lattices:
            count = len(lattice)
            f = (u % 1.0) * count
            i0 = np.floor(f).astype(np.int64)
            t = f - i0
            s = t * t * (3 - 2 * t)
            total += amplitude * (lattice[i0 % count] * (1 - s) + lattice[(i0 + 1) % count] * s)
            norm += amplitude
            amplitude *= 0.5
        return total / norm


def _periodic_dx(xs: np.ndarray, centre: float, period: float | None) -> np.ndarray:
    """Signed x distance, wrapped to [-period/2, period/2) when the layer repeats."""
    dx = xs - centre
    return dx if period is None else (dx + period / 2.0) % period - period / 2.0


@dataclass
class Shape:
    """A shape painted over the base fill, centred at (cx, cy) in layer x and viewport rows.

    mask(dx, dy) is evaluated only where |dx| <= reach and top <= dy <= bottom; dx is the periodic
    x distance from cx and dy = y - cy. shade = (colour, predicate(dx, dy)) recolours part of it."""
    cx: float
    cy: float
    reach: float
    top: float
    bottom: float
    colour: tuple[int, int, int, int]
    mask: Callable[[np.ndarray, np.ndarray], np.ndarray]
    shade: tuple[tuple[int, int, int, int], Callable[[np.ndarray, np.ndarray], np.ndarray]] | None = None


@dataclass
class LayerModel:
    """Everything random about a layer, drawn once; rendering at any width reuses it."""
    base: Callable[..., None] | None
    shapes: list[Shape]


def _star(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
    return (np.abs(dx) < 0.5) & (np.abs(dy) < 0.5)


def _disc(radius: float) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
    return lambda dx, dy: np.hypot(dx, dy) <= radius


def _cloud(radius: float, offsets: np.ndarray, sizes: np.ndarray) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
    def mask(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
        body = ((dx / (radius * 1.9)) ** 2 + ((dy - radius * 0.25) / (radius * 0.45)) ** 2) <= 1.0
        for (ox, oy), size in zip(offsets, sizes):
            body = body | (((dx - ox) / (size * 1.25)) ** 2 + ((dy - oy) / size) ** 2 <= 1.0)
        return body & (dy <= radius * 0.55)
    return mask


def _conifer(height: float, ratio: float, tiers: int) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
    """A tiered conifer silhouette with its apex at dy = 0, overlapping the ground by 1 px."""
    def mask(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
        frac = np.clip(dy / height, 0.0, 1.0)
        half = (height * ratio / 2.0) * frac * (0.72 + 0.28 * ((frac * tiers) % 1.0))
        return (dy >= 0) & (dy <= height + 1.0) & (np.abs(dx) <= half + 0.25)
    return mask


def _blade(height: float, slope: float) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
    """A 1 px grass blade rising from its foot at dy = 0, leaning by `slope` px per px."""
    def mask(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
        rise = -dy
        return (rise >= -0.5) & (rise <= height) & (np.abs(dx - slope * np.clip(rise, 0.0, height)) <= 0.5)
    return mask


def build_model(layer: Layer, spec: Spec) -> LayerModel:
    """Parse a generated layer's parameters and draw all its random values (rng order is fixed)."""
    params, label, vh = layer.params, f"layer {layer.id}", spec.viewport[1]
    span = float(layer.width)
    origin = 0.0 if layer.repeat[0] else -float(layer.pad_left)  # x positions in viewport px
    rng = np.random.default_rng([spec.seed, 7919, layer.seed])
    shapes: list[Shape] = []
    if layer.kind == "sky":
        colours = params.get("colors")
        if not isinstance(colours, list) or not colours:
            raise ParallaxError(f"{label}.colors must list one or more gradient colours, top to bottom")
        stops = np.array([_colour(c, f"{label}.colors")[:3] for c in colours], np.float64)
        bands = _integer(params.get("bands", 8 if spec.pixel_art else 0), f"{label}.bands", minimum=0)
        dither = params.get("dither", True)
        if not isinstance(dither, bool):
            raise ParallaxError(f"{label}.dither must be true or false")

        def sky(fine: np.ndarray, xs: np.ndarray, ys: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> None:
            t = np.broadcast_to(np.clip(ys / max(1.0, vh - 1.0), 0.0, 1.0)[:, None], fine.shape[:2])
            if bands >= 2:
                levels = t * (bands - 1)
                levels = np.floor(levels + BAYER4[rows[:, None] % 4, cols[None, :] % 4]) if dither \
                    else np.floor(levels + 0.5)
                t = np.clip(levels, 0, bands - 1) / (bands - 1)
            position = t * (len(stops) - 1)
            low = np.clip(np.floor(position).astype(np.int64), 0, len(stops) - 1)
            high = np.clip(low + 1, 0, len(stops) - 1)
            mix = (position - low)[..., None]
            rgb = stops[low] * (1 - mix) + stops[high] * mix
            fine[..., :3] = np.floor(rgb + 0.5) if bands >= 2 else rgb
            fine[..., 3] = 255.0

        stars = _mapping(params.get("stars", {}), f"{label}.stars")
        if stars:
            count = _integer(stars.get("count", 40), f"{label}.stars.count", minimum=0)
            colour = _colour(stars.get("color", "#f4f4f4"), f"{label}.stars.color")
            top, bottom = _range(stars.get("band", [0.0, 0.5]), f"{label}.stars.band")
            for x, y in zip(origin + rng.uniform(0, span, count), rng.uniform(top * vh, bottom * vh, count)):
                shapes.append(Shape(math.floor(x) + 0.5, math.floor(y) + 0.5, 0.5, -0.5, 0.5, colour, _star))
        sun = _mapping(params.get("sun", {}), f"{label}.sun")
        if sun:
            sx, sy = _pair([sun.get("x"), sun.get("y")], f"{label}.sun x/y")
            radius = _finite(sun.get("radius", 12), f"{label}.sun.radius", positive=True)
            shapes.append(Shape(sx, sy, radius, -radius, radius,
                                _colour(sun.get("color", "#fff3c4"), f"{label}.sun.color"), _disc(radius)))
        return LayerModel(sky, shapes)

    if layer.kind == "clouds":
        count = _integer(params.get("count", 6), f"{label}.count", minimum=0)
        y0, y1 = _range(params.get("y_range", [vh * 0.1, vh * 0.4]), f"{label}.y_range")
        r0, r1 = _range(params.get("size", [10, 22]), f"{label}.size", positive=True)
        puffs = _integer(params.get("puffs", 5), f"{label}.puffs", minimum=1)
        fill = _colour(params.get("fill", "#f4f4f4"), f"{label}.fill")
        shade = _colour(params.get("shade", "#c2c3c7"), f"{label}.shade")
        for _ in range(count):
            cx, cy, radius = origin + rng.uniform(0, span), rng.uniform(y0, y1), rng.uniform(r0, r1)
            offsets = rng.uniform(-1.0, 1.0, (puffs, 2)) * [radius * 1.2, radius * 0.25]
            sizes = rng.uniform(0.55, 1.0, puffs) * radius
            shapes.append(Shape(cx, cy, radius * 2.5, -radius * 1.3, radius * 0.6, fill,
                                _cloud(radius, offsets, sizes), (shade, lambda dx, dy, r=radius: dy > r * 0.15)))
        return LayerModel(None, shapes)

    # ridge and foreground: a periodic silhouette from the bottom, with trees or grass on top
    ridge = layer.kind == "ridge"
    base_y = _finite(params.get("base_y", vh * (0.62 if ridge else 0.92)), f"{label}.base_y")
    amplitude = _finite(params.get("amplitude", vh * (0.25 if ridge else 0.05)), f"{label}.amplitude", minimum=0)
    noise = PeriodicNoise(_integer(params.get("cells", 4 if ridge else 6), f"{label}.cells", minimum=1),
                          _integer(params.get("octaves", 4), f"{label}.octaves", minimum=1), rng)
    fill = _colour(params.get("fill", "#333c57" if ridge else "#1a1c2c"), f"{label}.fill")
    rim = _colour(params["rim"], f"{label}.rim") if "rim" in params else None
    rim_px = _finite(params.get("rim_px", 2), f"{label}.rim_px", positive=True)

    def ground(x: Any) -> np.ndarray:
        return base_y - amplitude * noise(np.asarray(x, np.float64) / span)

    def silhouette(fine: np.ndarray, xs: np.ndarray, ys: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> None:
        top = ground(xs)[None, :]
        solid = ys[:, None] >= top
        fine[solid] = fill
        if rim is not None:
            fine[solid & (ys[:, None] < top + rim_px)] = rim

    trees = _mapping(params.get("trees", {}), f"{label}.trees")
    if trees:
        count = _integer(trees.get("count", 24), f"{label}.trees.count", minimum=0)
        low_h, high_h = _range(trees.get("height", [8, 18]), f"{label}.trees.height", positive=True)
        ratio = _finite(trees.get("width_ratio", 0.5), f"{label}.trees.width_ratio", positive=True)
        colour = _colour(trees.get("color", params.get("fill", "#333c57")), f"{label}.trees.color")
        tiers = _integer(trees.get("tiers", 3), f"{label}.trees.tiers", minimum=1)
        for x, height in zip(origin + rng.uniform(0, span, count), rng.uniform(low_h, high_h, count)):
            apex = float(ground(x)) - height
            shapes.append(Shape(x, apex, height * ratio / 2.0 + 1.0, 0.0, height + 1.0, colour,
                                _conifer(height, ratio, tiers)))
    grass = _mapping(params.get("grass", {}), f"{label}.grass")
    if grass:
        count = _integer(grass.get("count", 60), f"{label}.grass.count", minimum=0)
        low_h, high_h = _range(grass.get("height", [3, 8]), f"{label}.grass.height", positive=True)
        colour = _colour(grass.get("color", params.get("fill", "#1a1c2c")), f"{label}.grass.color")
        for x, height, slope in zip(origin + rng.uniform(0, span, count), rng.uniform(low_h, high_h, count),
                                    rng.uniform(-0.5, 0.5, count)):
            shapes.append(Shape(x, float(ground(x)), abs(slope) * height + 1.0, -height, 0.5, colour,
                                _blade(height, slope)))
    return LayerModel(silhouette, shapes)


def render_layer(layer: Layer, spec: Spec, width: int) -> np.ndarray:
    """Render a generated layer `width` px wide (it may span several periods, for the periodicity proof).

    Pixel art renders at 1x; smooth layers at 4x and box-reduce in premultiplied space. Rows are
    rendered in bands and each shape only inside its (periodic) window."""
    model = build_model(layer, spec)
    scale = 1 if spec.pixel_art else SUPERSAMPLE
    period = float(layer.width) if layer.repeat[0] else None
    out = np.zeros((layer.height, width, 4), np.uint8)
    cols_fine = np.arange(width * scale)
    xs = (cols_fine + 0.5) / scale - layer.pad_left
    for row0 in range(0, layer.height, ROW_BAND):
        row1 = min(layer.height, row0 + ROW_BAND)
        rows_fine = np.arange(row0 * scale, row1 * scale)
        ys = (rows_fine + 0.5) / scale - layer.pad_top
        fine = np.zeros((len(ys), len(xs), 4), np.float64)
        if model.base is not None:
            model.base(fine, xs, ys, rows_fine // scale, cols_fine // scale)
        for shape in model.shapes:
            row_hit = np.flatnonzero((ys >= shape.cy + shape.top - 1.0) & (ys <= shape.cy + shape.bottom + 1.0))
            if not row_hit.size:
                continue
            dx_all = _periodic_dx(xs, shape.cx, period)
            col_hit = np.flatnonzero(np.abs(dx_all) <= shape.reach + 1.0)
            if not col_hit.size:
                continue
            dx, dy = dx_all[col_hit][None, :], ys[row_hit][:, None] - shape.cy
            hit = shape.mask(dx, dy)
            window = fine[np.ix_(row_hit, col_hit)]
            window[hit] = shape.colour
            if shape.shade is not None:
                window[hit & shape.shade[1](dx, dy)] = shape.shade[0]
            fine[np.ix_(row_hit, col_hit)] = window
        out[row0:row1] = _block_mean(fine, scale)
    return out


def roll_to_calmest(image: np.ndarray) -> tuple[np.ndarray, int]:
    """Roll a periodic layer so its wrap falls on its smallest column step; returns the shift used."""
    premultiplied = image.astype(np.float64)
    premultiplied[..., :3] *= premultiplied[..., 3:] / 255.0
    steps = np.abs(premultiplied - np.roll(premultiplied, -1, axis=1)).mean(axis=(0, 2))
    calm = int(np.argmin(steps))  # step from column calm to calm + 1 (cyclic)
    shift = (calm + 1) % image.shape[1]
    return np.roll(image, -shift, axis=1), shift


def column_seam(image: np.ndarray) -> dict:
    """forge_core.seam_report with each column as a frame: the wrap step against the column steps."""
    report = forge_core.seam_report(image[:, index:index + 1] for index in range(image.shape[1]))
    report["method"] = ("forge_core.seam_report over the layer's columns: seam = premultiplied RGBA mean "
                        "absolute difference of the last and first column (0-255), steps between neighbours")
    return report


# ----------------------------------------------------------------------------- camera sweep

def _display_image(layer: Layer, pixel_art: bool) -> np.ndarray:
    image = layer.image
    if layer.scale == 1:
        return image
    width = max(1, _round_half_up(image.shape[1] * layer.scale))
    height = max(1, _round_half_up(image.shape[0] * layer.scale))
    if pixel_art:
        return np.asarray(Image.fromarray(image).resize((width, height), Image.Resampling.NEAREST))
    return np.asarray(forge_core.resample_rgba(Image.fromarray(image), layer.scale, "lanczos",
                                               out_size=(width, height)))


def compose_frame(spec: Spec, images: Mapping[str, np.ndarray], cam_x: float, cam_y: float,
                  zoom: float) -> tuple[np.ndarray, dict[str, bool]]:
    """One viewport frame with the plan transform at integer pixel positions (top-left pivot).

    Returns the frame and, per layer, whether its canvas (tiled where it repeats) covered the frame."""
    vw, vh = spec.viewport
    world_w, world_h = int(math.ceil(vw / zoom)), int(math.ceil(vh / zoom))
    canvas = Image.new("RGBA", (world_w, world_h), (0, 0, 0, 0))
    covered: dict[str, bool] = {}
    for layer in spec.layers:
        image = images[layer.id]
        height, width = image.shape[:2]
        left = math.floor(layer.offset[0] - layer.anchor[0] * layer.scale - cam_x * layer.scroll[0])
        top = math.floor(layer.offset[1] - layer.anchor[1] * layer.scale - cam_y * layer.scroll[1])
        xs = [left - math.ceil(left / width) * width + k * width for k in range(world_w // width + 2)] \
            if layer.repeat[0] else [left]
        ys_ = [top - math.ceil(top / height) * height + k * height for k in range(world_h // height + 2)] \
            if layer.repeat[1] else [top]
        source = Image.fromarray(image)
        for y in ys_:
            for x in xs:
                x0, y0 = max(0, x), max(0, y)
                x1, y1 = min(world_w, x + width), min(world_h, y + height)
                if x0 < x1 and y0 < y1:
                    canvas.alpha_composite(source.crop((x0 - x, y0 - y, x1 - x, y1 - y)), (x0, y0))
        span_x = (min(xs), max(xs) + width)
        span_y = (min(ys_), max(ys_) + height)
        covered[layer.id] = span_x[0] <= 0 and span_x[1] >= world_w and span_y[0] <= 0 and span_y[1] >= world_h
    frame = canvas if (world_w, world_h) == (vw, vh) else canvas.resize((vw, vh), Image.Resampling.NEAREST)
    return np.asarray(frame).copy(), covered


def sweep(spec: Spec, images: Mapping[str, np.ndarray]) -> tuple[list[np.ndarray], dict]:
    """Frames along the camera path from (x0, y0) to (x1, y1) at the widest zoom."""
    count = spec.sweep_frames
    (x0, x1), (y0, y1), zoom = spec.camera["x"], spec.camera["y"], spec.camera["zoom"][0]
    frames, gaps, uncovered = [], 0, {}
    for index in range(count):
        t = index / (count - 1)
        frame, covered = compose_frame(spec, images, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, zoom)
        frames.append(frame)
        gaps += int((frame[..., 3] < 255).any())
        for layer in spec.layers:
            if layer.require_coverage and not covered[layer.id]:
                uncovered.setdefault(layer.id, []).append(index)
    steps = [forge_core.transition_mae(frames[i], frames[i + 1]) for i in range(count - 1)]
    return frames, {"frames": count, "zoom": _num(zoom), "frames_with_transparency": gaps,
                    "uncovered": uncovered, "step_mae_median": round(float(np.median(steps)), 4) if steps else 0.0,
                    "step_mae_max": round(float(max(steps)), 4) if steps else 0.0}


def contact_sheet(frames: Sequence[np.ndarray]) -> np.ndarray:
    """All sweep frames in a grid (row-major), downscaled by an integer factor to fit 2048 px."""
    height, width = frames[0].shape[:2]
    columns = max(1, min(len(frames), int(math.ceil(math.sqrt(len(frames))))))
    factor = max(1, int(math.ceil(columns * (width + 2) / SHEET_MAX_WIDTH)))
    cell_w, cell_h = width // factor, height // factor
    rows = int(math.ceil(len(frames) / columns))
    sheet = np.zeros((rows * (cell_h + 2) + 2, columns * (cell_w + 2) + 2, 4), np.uint8)
    sheet[..., :3], sheet[..., 3] = 40, 255
    for index, frame in enumerate(frames):
        small = frame[::factor, ::factor][:cell_h, :cell_w]
        row, column = divmod(index, columns)
        y, x = 2 + row * (cell_h + 2), 2 + column * (cell_w + 2)
        sheet[y:y + cell_h, x:x + cell_w] = small
    return sheet


# ----------------------------------------------------------------------------- validation

def run_validator(validator: Path, plan_path: Path, timeout: float = 300.0) -> dict:
    """Run the sibling generate2dmap validate_parallax.py and return its JSON report."""
    completed = subprocess.run([sys.executable, str(validator), "--spec", str(plan_path)], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=timeout, cwd=str(plan_path.parent),
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        tail = (completed.stderr or completed.stdout).strip().splitlines()[-1:] or ["no output"]
        raise ParallaxError(f"validate_parallax.py exited {completed.returncode}: {tail[0]}") from None
    if not isinstance(report, dict) or "passed" not in report:
        raise ParallaxError("validate_parallax.py printed no report")
    for layer in report.get("layers") or []:  # its paths are absolute and point into the stage
        if isinstance(layer, dict) and "image" in layer:
            layer["image"] = Path(str(layer["image"])).name
    return report


# ----------------------------------------------------------------------------- build

def build(args: argparse.Namespace) -> dict:
    spec = load_spec(Path(args.spec), args.sweep_frames)
    size_canvases(spec)
    validator = Path(args.validator) if args.validator else VALIDATOR
    if args.validate is True and not validator.is_file():
        raise ParallaxError(f"--validate needs the sibling validator, not found: {validator}; "
                            "install generate2dmap beside codeart2d or pass --validator")
    use_validator = args.validate is not False and validator.is_file()

    images: dict[str, np.ndarray] = {}
    checks: list[dict] = []
    seams: dict[str, dict] = {}
    for layer in spec.layers:
        if layer.kind == "image":
            images[layer.id] = _display_image(layer, spec.pixel_art)
            layer.height, layer.width = images[layer.id].shape[:2]
        else:
            image = render_layer(layer, spec, layer.width)
            if layer.repeat[0]:
                doubled = render_layer(layer, spec, 2 * layer.width)
                mismatches = int(np.count_nonzero(np.any(doubled != np.tile(image, (1, 2, 1)), axis=2)))
                checks.append({"id": f"periodic:{layer.id}", "status": "pass" if mismatches == 0 else "fail",
                               "value": mismatches, "threshold": 0})
                image, layer.rolled = roll_to_calmest(image)
            images[layer.id] = image
            if layer.kind == "sky" and int(image[..., 3].min()) != 255:
                raise ParallaxError(f"layer {layer.id}: the sky came out with transparent pixels")
            if layer.kind != "sky" and int(image[..., 3].max()) == 0:
                raise ParallaxError(f"layer {layer.id} is empty; check base_y, amplitude and counts")
        if layer.repeat[0]:
            seams[layer.id] = column_seam(images[layer.id])
            ratio = seams[layer.id]["seam_over_p95"]
            checks.append({"id": f"loop_step:{layer.id}", "status": "pass" if ratio <= LOOP_STEP_LIMIT else "fail",
                           "value": round(ratio, 4), "threshold": LOOP_STEP_LIMIT})

    plan_layers = []
    for layer in spec.layers:
        plan_layers.append({
            "id": layer.id, "role": layer.role, "image": f"{layer.id}.png", "alpha": layer.alpha,
            "scale": _num(layer.scale), "offset": [_num(v) for v in layer.offset],
            "anchor_px": [_num(v) for v in layer.anchor], "scroll_factor": [_num(v) for v in layer.scroll],
            "repeat": list(layer.repeat), "require_canvas_coverage": layer.require_coverage,
            "allow_empty": False, "kind": layer.kind})
    plan = {"schema": PLAN_SCHEMA, "viewport": list(spec.viewport),
            "camera": {"x": [_num(v) for v in spec.camera["x"]], "y": [_num(v) for v in spec.camera["y"]],
                       "zoom": [_num(v) for v in spec.camera["zoom"]], "pivot": "top-left"},
            "layers": plan_layers}

    frames: list[np.ndarray] = []
    sweep_report: dict = {}
    if spec.sweep_frames:
        frames, sweep_report = sweep(spec, images)
        checks.append({"id": "sweep_opaque", "status": "pass" if sweep_report["frames_with_transparency"] == 0
                       else "fail", "value": sweep_report["frames_with_transparency"], "threshold": 0})
        checks.append({"id": "sweep_coverage", "status": "pass" if not sweep_report["uncovered"] else "fail",
                       "value": sweep_report["uncovered"], "threshold": {}})

    with forge_core.staged_output(Path(args.output_dir)) as stage:
        outputs: list[Path] = []
        for layer in spec.layers:
            path = stage / f"{layer.id}.png"
            # image layers keep their source pixels; the plan's scale displays them
            codeart_core.save_png(layer.image if layer.kind == "image" else images[layer.id], path)
            outputs.append(path)
        for entry, path in zip(plan_layers, outputs):
            entry["sha256"] = forge_core.sha256_file(path)
        forge_core.write_json(stage / "parallax-plan.json", plan)
        outputs.append(stage / "parallax-plan.json")
        if use_validator:
            report = run_validator(validator, stage / "parallax-plan.json")
            forge_core.write_json(stage / "validate-parallax.json", report)
            outputs.append(stage / "validate-parallax.json")
            checks.append({"id": "sibling_validator", "status": "pass" if report.get("passed") else "fail",
                           "value": report.get("issues", []), "threshold": []})
        else:
            checks.append({"id": "sibling_validator", "status": "skipped",
                           "value": "validate_parallax.py not run" + ("" if args.validate is False else
                                                                       " (not found beside codeart2d)"),
                           "threshold": []})
        if frames:
            codeart_core.save_png(contact_sheet(frames), stage / "sweep-sheet.png")
            outputs.append(stage / "sweep-sheet.png")
            if features.check("webp"):
                pictures = [Image.fromarray(frame).convert("RGB") for frame in frames]
                pictures[0].save(stage / "sweep.webp", save_all=True, append_images=pictures[1:], duration=60, loop=0,
                                 lossless=True)
                outputs.append(stage / "sweep.webp")
        status = "fail" if any(c["status"] == "fail" for c in checks) else \
            "warn" if any(c["status"] == "warn" for c in checks) else "pass"
        if args.strict_qc and status == "fail":
            failed = "; ".join(f"{c['id']}={c['value']}" for c in checks if c["status"] == "fail")
            raise ParallaxQAError(f"parallax QA failed ({failed}); nothing was published")
        inputs = [spec.path.resolve()] + [layer.source for layer in spec.layers if layer.source is not None]
        not_proven = [
            "How the layers look and read as depth: open sweep-sheet.png (or sweep.webp) and each layer PNG.",
            "Runtime rendering: the sweep composites at integer pixel positions with a top-left zoom pivot; "
            "engines with another camera transform, rotation, culling or sub-pixel scrolling must be checked "
            "in the engine.",
            f"The sweep runs at the widest zoom ({_num(spec.camera['zoom'][0])}) along the camera path from its "
            "minimum to its maximum; validate_parallax.py checks every camera and zoom extreme.",
        ]
        if any(layer.kind == "image" for layer in spec.layers):
            not_proven.append("Image layers are the user's art: they are measured, never repaired.")
        envelope = {
            "status": status,
            "method": ("parallax_build: generated layers rendered twice as wide must equal the layer tiled twice "
                       "(exact periodicity); loop step = forge_core.seam_report over columns, wrap step <= p95 of "
                       "the column steps; sibling generate2dmap validate_parallax.py on the plan; "
                       f"{spec.sweep_frames}-frame camera sweep checked for transparency and canvas coverage"),
            "notProven": not_proven,
            "checks": checks,
            "inputs": [_file_ref(path, stage) for path in inputs],
            "outputs": [_file_ref(path, stage) for path in outputs],
            "tool": {"name": TOOL, "version": TOOL_VERSION},
            "seams": seams,
            "sweep": sweep_report,
            "layers": {layer.id: {"size": [layer.width, layer.height], "rolled_px": layer.rolled,
                                  "pad_top": layer.pad_top, "pad_left": layer.pad_left} for layer in spec.layers},
        }
        forge_core.write_json(stage / "parallax-qa.json", envelope)
        palette = {}
        for layer in spec.layers:
            for key in ("fill", "rim", "shade"):
                if key in layer.params:
                    palette[f"{layer.id}.{key}"] = codeart_core.rgba_to_hex(_colour(layer.params[key], key))
            for number, value in enumerate(layer.params.get("colors") or []):
                palette[f"{layer.id}.color{number}"] = codeart_core.rgba_to_hex(_colour(value, "colors"))
        codeart_core.write_codeart_meta(
            stage / "codeart-meta.json", generator=TOOL, spec_sha256=forge_core.sha256_bytes(spec.raw),
            renderer={"name": TOOL, "version": TOOL_VERSION, "pixel_art": spec.pixel_art},
            palette=palette or {"none": "#000000"}, outputs=outputs, qa=envelope)
    final = Path(args.output_dir).resolve()
    return {"status": status, "output": final.as_posix(), "plan": (final / "parallax-plan.json").as_posix(),
            "metadata": (final / "codeart-meta.json").as_posix(), "qa": (final / "parallax-qa.json").as_posix(),
            "layers": len(spec.layers), "sweep_frames": spec.sweep_frames, "validated": use_validator}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate periodic code-art parallax layers (sky, ridges, clouds, foreground) plus image "
                    "layers, write a parallax plan, run the sibling validate_parallax.py and render a camera "
                    "sweep. No image model is used.",
        epilog="Example: python parallax_build.py --spec parallax-gen.json --output-dir out/bg-v1 --validate "
               "--sweep-frames 49")
    parser.add_argument("--spec", required=True, help="codeart2d.parallax_spec.v1 JSON file")
    parser.add_argument("--output-dir", required=True, help="new folder to create; an existing path is refused")
    parser.add_argument("--validate", action=argparse.BooleanOptionalAction, default=None,
                        help="run generate2dmap/scripts/validate_parallax.py on the plan (default: when found)")
    parser.add_argument("--validator", help="path to validate_parallax.py when generate2dmap is installed elsewhere")
    parser.add_argument("--sweep-frames", type=int, help="camera sweep frames (default: spec sweep_frames or 49; "
                                                         "0 turns the sweep off)")
    parser.add_argument("--strict-qc", action="store_true",
                        help="exit 1 and publish nothing when a QA check fails (periodicity, loop step, validator, "
                             "sweep coverage)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = parse_args(argv)
    if args.sweep_frames is not None and args.sweep_frames < 0:
        print("error: --sweep-frames must be 0 or more", file=sys.stderr)
        return 1
    if os.path.lexists(args.output_dir):
        print(f"error: output directory already exists: {forge_core.ascii_text(str(args.output_dir))}",
              file=sys.stderr)
        return 1
    try:
        summary = build(args)
    except (ParallaxError, ParallaxQAError, codeart_core.CodeArtError, OSError, ValueError,
            subprocess.SubprocessError) as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
