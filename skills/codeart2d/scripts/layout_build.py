#!/usr/bin/env python3
"""Build a playable top-down map from a code-art layout spec (codeart2d; no image model).

The map is data first. For one spec and seed every output is byte-identical:

1. terrain: materials are painted on the (W+1) x (H+1) vertex grid in spec order
   (ellipse, rect, polygon and Catmull-Rom spline roads), then hygiene passes keep
   every cell drawable by the given tilesets, fill diagonal saddles and drop lone
   vertices;
2. tiles: corner-Wang autotiling from tileset_v1 manifests (--tiles), with
   seeded interior variants that avoid repeating their left and upper neighbours;
   without a tileset the ground is a flat-colour image (placeholder art);
3. objects: fixed placements plus seeded scatter groups (weighted kinds,
   variants, mirroring, Poisson-disk spacing, density noise and clearance from
   roads, blocked terrain, objects, exits and spawns);
4. collision: blocked terrain as exact half-tile rectangles, prop footprints
   (scaled once) as ellipse/rect solids, and merged rectangles whose union equals
   the blocked cell raster;
5. navigation (plan Appendix C): the actor footprint ellipse is tested at cell
   centres (cell = max(1, round(r / 2))) and on the midpoint of every
   4-neighbour move; every exit, spawn and interaction must be reachable from
   the first spawn, and every exit gets an arrival spawn outside its trigger;
6. output: map-bundle.json (generate2dmap.map_bundle.v2), terrain-vertices.json,
   copied tilesets, prop images, optional preview.png and debug.png,
   layout-qa.json (QA envelope) and codeart-meta.json, staged and published only
   when complete.

Run from the project root:
  python "<skill-dir>/scripts/layout_build.py" --spec meadow-layout.json --output-dir out/map-v1 --seed 7 --preview --strict-qc
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
except ImportError as _missing:  # an environment problem: report it without a traceback
    sys.stderr.write(f"error: missing Python module {_missing.name}; install with: "
                     "python -m pip install numpy Pillow scipy\n")
    raise SystemExit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codeart_core  # noqa: E402
import forge_core  # noqa: E402


TOOL = "layout_build"
TOOL_VERSION = "1.0"
SPEC_SCHEMA = "codeart2d.layout_spec.v1"
BUNDLE_SCHEMA = "generate2dmap.map_bundle.v2"
VERTEX_GRID_SCHEMA = "generate2dmap.vertex_grid.v1"
TILESET_SCHEMA = "generate2dmap.tileset.v1"
PROP_PACK_SCHEMA = "generate2dmap.prop_pack.v2"

TERRAIN_SHAPES = ("ellipse", "rect", "polygon", "road")
OCCLUSION_CLASSES = ("low", "tall", "foreground")
FOOTPRINT_SHAPES = ("ellipse", "rect", "none")
EDGES = {"west": (-1.0, 0.0), "east": (1.0, 0.0), "north": (0.0, -1.0), "south": (0.0, 1.0)}
OPPOSITE_FACING = {"west": "east", "east": "west", "north": "south", "south": "north"}
CLEARANCE_KEYS = ("road", "blocked", "object", "exit", "spawn", "edge")
CLEARANCE_DEFAULTS = {"road": 12.0, "blocked": 10.0, "object": 6.0, "exit": 24.0, "spawn": 20.0, "edge": 0.0}
PLACEHOLDER_COLOURS = {"tall": "#3f7a3a", "low": "#6a9a48", "foreground": "#4d7f3c", "rect": "#9a6b4a"}
PLACEHOLDER_SIZES = {"tall": (24, 32), "low": (16, 12), "foreground": (16, 10), "rect": (32, 32)}
SAMPLE_ANGLES = tuple(k * math.pi / 4.0 for k in range(8))
VARIETY_WARN_SHARE = 0.35
POCKET_MIN_CELLS = 8
HYGIENE_MAX_PASSES = 10
MAX_WORLD_PIXELS = 4096 * 4096
FIELD_TARGET_CELLS = 4_000_000
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class LayoutError(ValueError):
    """The layout spec or an input it names is invalid."""


class LayoutQAError(ValueError):
    """Strict QC failed; nothing is published."""


# ----------------------------------------------------------------------------- small helpers

def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def _num(value: float) -> int | float:
    """A JSON-friendly number: integral values as int, others rounded to 4 decimals."""
    rounded = round(float(value), 4)
    return int(rounded) if rounded == int(rounded) else rounded


def _mapping(value: Any, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise LayoutError(f"{label} must be a JSON object")
    return value


def _finite(value: Any, label: str, *, minimum: float | None = None, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LayoutError(f"{label} must be a finite number")
    if positive and value <= 0:
        raise LayoutError(f"{label} must be greater than 0")
    if minimum is not None and value < minimum:
        raise LayoutError(f"{label} must be at least {minimum}")
    return float(value)


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LayoutError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise LayoutError(f"{label} must be at least {minimum}")
    return int(value)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise LayoutError(f"{label} must be true or false")
    return value


def _point(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise LayoutError(f"{label} must be [x, y]")
    return _finite(value[0], label), _finite(value[1], label)


def _points(value: Any, label: str, minimum: int) -> list[tuple[float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) < minimum:
        raise LayoutError(f"{label} must list at least {minimum} [x, y] points")
    return [_point(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _ident(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise LayoutError(f"{label} must be an id of letters, digits, '_', '.' or '-' (got {value!r})")
    return value


def _colour(value: Any, label: str) -> tuple[int, int, int, int]:
    try:
        return codeart_core.hex_to_rgba(value)
    except codeart_core.CodeArtError as error:
        raise LayoutError(f"{label}: {error}") from None


def _hex(colour: Sequence[int]) -> str:
    return codeart_core.rgba_to_hex(tuple(int(v) for v in colour))


def _relative(path: Path, base: Path) -> str:
    """POSIX path of `path` relative to `base`; a file on another drive keeps its name only."""
    try:
        return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:
        return path.name


def _file_ref(path: Path, base: Path) -> dict:
    return {"path": _relative(path, base), "sha256": forge_core.sha256_file(path), "bytes": path.stat().st_size}


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LayoutError(f"{label} not found: {path}") from None
    except json.JSONDecodeError as error:
        raise LayoutError(f"{label} {path.name} is not valid JSON: {error}") from None


def _load_image(path: Path, label: str) -> np.ndarray:
    """An input image as 8-bit straight-alpha RGBA (forge_core.load_rgba) with a readable error."""
    try:
        return np.asarray(forge_core.load_rgba(path)[0]).copy()
    except FileNotFoundError:
        raise LayoutError(f"{label} not found: {path}") from None
    except UnidentifiedImageError:
        raise LayoutError(f"{label} is not a readable image: {path}") from None


def _shade(colour: Sequence[int], factor: float) -> tuple[int, int, int, int]:
    return tuple(_round_half_up(c * factor) for c in colour[:3]) + (255,)


# ----------------------------------------------------------------------------- geometry

def _inside_polygon(xs: np.ndarray, ys: np.ndarray, polygon: Sequence[Sequence[float]]) -> np.ndarray:
    """Even-odd point-in-polygon test, vectorised over the points."""
    inside = np.zeros(np.broadcast(xs, ys).shape, bool)
    count = len(polygon)
    for index in range(count):
        x0, y0 = polygon[index]
        x1, y1 = polygon[(index + 1) % count]
        if y0 == y1:
            continue
        crosses = (ys >= min(y0, y1)) & (ys < max(y0, y1))
        x_cross = x0 + (ys - y0) * (x1 - x0) / (y1 - y0)
        inside ^= crosses & (xs < x_cross)
    return inside


def _catmull_rom(points: Sequence[tuple[float, float]], smooth: bool) -> np.ndarray:
    """Uniform Catmull-Rom spline through `points` (end points repeated), or the polyline itself."""
    controls = np.asarray(points, np.float64)
    if not smooth or len(controls) < 3:
        return controls
    padded = np.vstack([controls[:1], controls, controls[-1:]])
    pieces = []
    for index in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[index - 1:index + 3]
        steps = max(8, int(math.ceil(4.0 * float(np.hypot(*(p2 - p1))))))
        t = (np.arange(steps) / steps)[:, None]
        pieces.append(0.5 * (2 * p1 + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
                             + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3))
    pieces.append(controls[-1:])
    return np.vstack(pieces)


def _distance_to_polyline(xs: np.ndarray, ys: np.ndarray, polyline: np.ndarray) -> np.ndarray:
    """Euclidean distance from each point to the polyline, vectorised in segment chunks."""
    best = np.full(xs.shape, np.inf)
    px, py = xs.reshape(-1, 1), ys.reshape(-1, 1)
    flat = best.reshape(-1)
    if len(polyline) == 1:
        return np.hypot(xs - polyline[0, 0], ys - polyline[0, 1])
    for start in range(0, len(polyline) - 1, 64):
        a = polyline[start:start + 64]
        b = polyline[start + 1:start + 65]
        a = a[:len(b)]
        dx, dy = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
        length2 = np.maximum(dx * dx + dy * dy, 1e-12)
        t = np.clip(((px - a[:, 0]) * dx + (py - a[:, 1]) * dy) / length2, 0.0, 1.0)
        distance = np.hypot(px - (a[:, 0] + t * dx), py - (a[:, 1] + t * dy)).min(axis=1)
        np.minimum(flat, distance, out=flat)
    return best


def _run_rects(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Disjoint (column, row, width, height) cell rectangles whose union is exactly `mask`.

    Each row splits into maximal runs; a run with the same span as a rectangle that
    ended on the previous row extends it downwards. Deterministic, loops over runs only."""
    rects: list[list[int]] = []
    open_spans: dict[tuple[int, int], int] = {}
    padded = np.zeros(mask.shape[1] + 2, np.int8)
    for row in range(mask.shape[0]):
        padded[1:-1] = mask[row]
        edges = np.flatnonzero(np.diff(padded))
        current: dict[tuple[int, int], int] = {}
        for start, end in zip(edges[0::2].tolist(), edges[1::2].tolist()):
            index = open_spans.get((start, end))
            if index is None:
                rects.append([start, row, end - start, 1])
                index = len(rects) - 1
            else:
                rects[index][3] += 1
            current[(start, end)] = index
        open_spans = current
    return [tuple(rect) for rect in rects]


def _distance_field(mask: np.ndarray, cap: float) -> np.ndarray:
    """Euclidean distance (in mask cells) from every cell to the nearest True cell, exact up to `cap`
    and infinity beyond it (or when the mask is empty).

    scipy.ndimage when available (honouring FORGE_CORE_NO_SCIPY), otherwise _local_capped_edt; both
    give identical values, because every distance compared against a threshold is at most `cap`."""
    if not mask.any():
        return np.full(mask.shape, np.inf)
    ndimage = None
    if os.environ.get("FORGE_CORE_NO_SCIPY", "") in ("", "0"):
        try:
            from scipy import ndimage
        except ImportError:
            ndimage = None
    if ndimage is None:
        return _local_capped_edt(mask, cap)
    distance = ndimage.distance_transform_edt(~mask)
    distance[distance > cap] = np.inf
    return distance


def _local_capped_edt(target: np.ndarray, cap: float) -> np.ndarray:
    """Exact Euclidean distance to the nearest True cell where it is at most `cap`, else infinity (numpy).

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


# ----------------------------------------------------------------------------- spec model

@dataclass(frozen=True)
class Material:
    name: str
    index: int
    colour: tuple[int, int, int, int]
    walkable: bool
    priority: float


@dataclass
class PropVariant:
    prop_id: str
    image: np.ndarray
    anchor: tuple[float, float]
    origin: str  # "image", "pixelspec", "pack" or "placeholder"
    source: Path | None = None


@dataclass
class PropKind:
    name: str
    variants: list[PropVariant]
    footprint: dict
    solid: bool
    occlusion: str
    flip: bool
    scale: float
    interactions: list[dict]

    @property
    def placeholder(self) -> bool:
        return any(variant.origin == "placeholder" for variant in self.variants)


@dataclass
class Instance:
    id: str
    kind: PropKind
    variant: int
    x: float
    y: float
    flip: bool
    scale: float
    group: str | None = None

    @property
    def look(self) -> tuple[str, int, bool]:
        return self.kind.name, self.variant, self.flip

    @property
    def prop(self) -> PropVariant:
        return self.kind.variants[self.variant]

    def sprite_rect(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) of the drawn sprite in world pixels (half-up rounding)."""
        image = self.prop.image
        width = max(1, _round_half_up(image.shape[1] * self.scale))
        height = max(1, _round_half_up(image.shape[0] * self.scale))
        anchor_x = image.shape[1] - self.prop.anchor[0] if self.flip else self.prop.anchor[0]
        left = _round_half_up(self.x - anchor_x * self.scale)
        top = _round_half_up(self.y - self.prop.anchor[1] * self.scale)
        return left, top, width, height

    def footprint(self) -> dict:
        """The prop footprint as placed: mirrored with the sprite, in prop image pixels."""
        print_ = dict(self.kind.footprint)
        if self.flip and print_["shape"] != "none":
            print_["offset"] = [-print_["offset"][0], print_["offset"][1]]
            print_["rotate"] = -print_["rotate"]
        return print_


@dataclass
class Layout:
    spec_path: Path
    spec_bytes: bytes
    map_id: str
    width: int
    height: int
    tile: int
    seed: int
    materials: list[Material]
    base: Material
    terrain: list[dict]
    hygiene: dict
    actor_radius: float
    y_squash: float
    kinds: dict[str, PropKind]
    objects: list[dict]
    scatter: list[dict]
    exits: list[dict]
    spawns: list[dict]
    interactions: list[dict]
    inputs: list[Path] = field(default_factory=list)

    @property
    def world(self) -> tuple[int, int]:
        return self.width * self.tile, self.height * self.tile

    def material(self, name: Any, label: str) -> Material:
        for material in self.materials:
            if material.name == name:
                return material
        known = ", ".join(material.name for material in self.materials)
        raise LayoutError(f"{label}: unknown material {name!r}; known: {known}")


def _parse_materials(raw: Any) -> list[Material]:
    entries = _mapping(raw, "materials")
    if not entries:
        raise LayoutError("materials must name at least one material")
    materials = []
    for index, (name, value) in enumerate(entries.items()):
        _ident(name, "material name")
        value = _mapping(value, f"materials.{name}")
        if "color" not in value:
            raise LayoutError(f"materials.{name} needs a color")
        walkable = _boolean(value.get("walkable", True), f"materials.{name}.walkable")
        priority = _finite(value.get("priority", index), f"materials.{name}.priority")
        materials.append(Material(name, index, _colour(value["color"], f"materials.{name}.color"), walkable, priority))
    if len(materials) > 64:
        raise LayoutError("at most 64 materials are supported")
    return materials


def _parse_terrain(raw: Any, layout: Layout) -> list[dict]:
    if not isinstance(raw, list):
        raise LayoutError("terrain must be a list of paint operations")
    ops, ids = [], set()
    for index, item in enumerate(raw):
        label = f"terrain[{index}]"
        item = _mapping(item, label)
        shape = item.get("shape")
        if shape not in TERRAIN_SHAPES:
            raise LayoutError(f"{label}.shape must be one of {', '.join(TERRAIN_SHAPES)}")
        op: dict[str, Any] = {"id": _ident(item.get("id", f"{shape}-{index + 1}"), f"{label}.id"), "shape": shape,
                              "material": layout.material(item.get("material"), f"{label}.material")}
        if op["id"] in ids:
            raise LayoutError(f"{label}: duplicate terrain id {op['id']!r}")
        ids.add(op["id"])
        if shape == "ellipse":
            op["center"] = _point(item.get("center"), f"{label}.center")
            radius = item.get("radius")
            rx, ry = _point(radius, f"{label}.radius") if isinstance(radius, (list, tuple)) else \
                (_finite(radius, f"{label}.radius"),) * 2
            if rx <= 0 or ry <= 0:
                raise LayoutError(f"{label}.radius must be greater than 0")
            op["radius"] = (rx, ry)
            op["rotate"] = _finite(item.get("rotate", 0), f"{label}.rotate")
            op["wobble"] = _finite(item.get("wobble", 0), f"{label}.wobble", minimum=0)
        elif shape == "rect":
            box = item.get("box")
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                raise LayoutError(f"{label}.box must be [x0, y0, x1, y1] in tiles")
            x0, y0, x1, y1 = (_finite(v, f"{label}.box") for v in box)
            if x0 > x1 or y0 > y1:
                raise LayoutError(f"{label}.box must have x0 <= x1 and y0 <= y1")
            op["box"] = (x0, y0, x1, y1)
        elif shape == "polygon":
            op["points"] = _points(item.get("points"), f"{label}.points", 3)
        else:
            op["points"] = _points(item.get("points"), f"{label}.points", 2)
            op["width"] = _finite(item.get("width", 1.5), f"{label}.width", positive=True)
            op["smooth"] = _boolean(item.get("smooth", True), f"{label}.smooth")
        ops.append(op)
    return ops


def _prop_pack_items(raw: Any, spec_dir: Path, layout: Layout) -> tuple[dict[str, dict], Path | None]:
    if raw is None:
        return {}, None
    if not isinstance(raw, str):
        raise LayoutError("prop_pack must be a path to a prop pack manifest")
    path = (spec_dir / raw).resolve()
    data = _mapping(_load_json(path, "prop pack"), "prop pack")
    if data.get("schema") not in (None, PROP_PACK_SCHEMA):
        raise LayoutError(f"prop pack {path.name} has schema {data.get('schema')!r}; expected {PROP_PACK_SCHEMA}")
    items = {}
    for item in data.get("accepted") or []:
        if isinstance(item, Mapping) and isinstance(item.get("label"), str):
            items[item["label"]] = dict(item)
    layout.inputs.append(path)
    return items, path.parent


def _variant_from_source(source: Mapping, kind: str, number: int, spec_dir: Path, pack: dict[str, dict],
                         pack_dir: Path | None, layout: Layout, label: str) -> tuple[PropVariant, dict]:
    """One prop image from {image}, {pixelspec} or {pack}; returns the variant and pack metadata."""
    prop_id = kind if number == 0 else f"{kind}-v{number}"
    anchor = _point(source["anchor_px"], f"{label}.anchor_px") if "anchor_px" in source else None
    pack_meta: dict = {}
    if "pack" in source:
        if pack_dir is None:
            raise LayoutError(f"{label}.pack needs a top-level prop_pack manifest")
        item = pack.get(source["pack"])
        if item is None:
            raise LayoutError(f"{label}.pack: no accepted item labelled {source['pack']!r} in the prop pack")
        path = (pack_dir / str(item.get("image", ""))).resolve()
        image = _load_image(path, f"{label}.pack image")
        layout.inputs.append(path)
        pack_meta = item
        if anchor is None and "anchor_px" in item:
            anchor = _point(item["anchor_px"], f"{label} pack anchor_px")
        variant = PropVariant(prop_id, image, anchor or (0.0, 0.0), "pack", path)
    elif "image" in source:
        path = (spec_dir / str(source["image"])).resolve()
        image = _load_image(path, f"{label}.image")
        layout.inputs.append(path)
        variant = PropVariant(prop_id, image, anchor or (0.0, 0.0), "image", path)
    elif "pixelspec" in source:
        spec = source["pixelspec"]
        if isinstance(spec, str):
            spec_file = (spec_dir / spec).resolve()
            spec = _load_json(spec_file, f"{label}.pixelspec")
            layout.inputs.append(spec_file)
        spec = _mapping(spec, f"{label}.pixelspec")
        try:
            image = codeart_core.render_pixelspec(spec)
        except codeart_core.CodeArtError as error:
            raise LayoutError(f"{label}.pixelspec: {error}") from None
        if anchor is None and "anchor_px" in spec:
            anchor = _point(spec["anchor_px"], f"{label}.pixelspec.anchor_px")
        variant = PropVariant(prop_id, image, anchor or (0.0, 0.0), "pixelspec")
    else:
        return PropVariant(prop_id, np.zeros((1, 1, 4), np.uint8), (0.0, 0.0), "placeholder"), {}
    if anchor is None:  # bottom centre: the ground line is the bottom edge of the last row
        variant.anchor = (variant.image.shape[1] / 2.0, float(variant.image.shape[0]))
    height, width = variant.image.shape[:2]
    if not (0 <= variant.anchor[0] <= width and 0 <= variant.anchor[1] <= height):
        raise LayoutError(f"{label}: anchor_px {list(variant.anchor)} lies outside the {width}x{height} image")
    return variant, pack_meta


def _placeholder_image(size: tuple[int, int], colour: tuple[int, int, int, int], occlusion: str,
                       rect: bool) -> np.ndarray:
    """A flat-shaded stand-in sprite (tree, mound or block) with a 1 px outline, palette-exact."""
    width, height = size
    ys, xs = np.mgrid[0:height, 0:width] + 0.5
    body, dark, line = colour, _shade(colour, 0.62), _shade(colour, 0.32)
    if rect:
        roof_bottom = 0.45 * height
        wall = (ys >= 0.4 * height) & (ys < height - 1) & (xs >= 1.5) & (xs < width - 1.5)
        roof = (ys >= 1) & (ys < roof_bottom) & (np.abs(xs - width / 2.0) <= (ys / roof_bottom) * (width / 2.0 - 1.0))
        shape = wall | roof
        shaded = roof
    elif occlusion == "tall":
        canopy = ((xs - width / 2.0) / (0.46 * width - 1)) ** 2 + ((ys - 0.4 * height) / (0.36 * height)) ** 2 <= 1.0
        trunk = (np.abs(xs - width / 2.0) <= max(1.0, width / 10.0)) & (ys >= 0.55 * height) & (ys < height - 1)
        shape = canopy | trunk
        shaded = trunk | (canopy & ((xs - width / 2.0) / width + (ys - 0.4 * height) / height > 0.12))
    else:
        dome = ((xs - width / 2.0) / (0.5 * width - 1)) ** 2 + ((ys - (height - 1)) / (height - 2)) ** 2 <= 1.0
        shape = dome & (ys < height - 1)
        shaded = shape & ((xs - width / 2.0) / width > 0.15)
    image = np.zeros((height, width, 4), np.uint8)
    image[shape] = body
    image[shape & shaded] = dark
    padded = np.pad(shape, 1)
    ring = (padded[:-2, 1:-1] | padded[2:, 1:-1] | padded[1:-1, :-2] | padded[1:-1, 2:]) & ~shape
    image[ring] = line
    return image


def _default_footprint(size: tuple[int, int], occlusion: str, rect: bool) -> dict:
    width, height = size
    if occlusion == "foreground":
        return {"shape": "none", "width": 0.0, "depth": 0.0, "offset": [0.0, 0.0], "rotate": 0.0}
    if rect:
        across, depth = 0.9 * width, 0.45 * height
    elif occlusion == "tall":
        across, depth = max(2.0, 0.35 * width), max(2.0, 0.2 * width)
    else:
        across, depth = max(2.0, 0.8 * width), max(2.0, 0.35 * width)
    across, depth = round(across, 2), round(depth, 2)
    return {"shape": "rect" if rect else "ellipse", "width": across, "depth": depth,
            "offset": [0.0, round(-depth / 2.0, 2)], "rotate": 0.0}


def _parse_footprint(raw: Any, label: str) -> dict:
    raw = _mapping(raw, label)
    shape = raw.get("shape")
    if shape not in FOOTPRINT_SHAPES:
        raise LayoutError(f"{label}.shape must be one of {', '.join(FOOTPRINT_SHAPES)}")
    if shape == "none":
        return {"shape": "none", "width": 0.0, "depth": 0.0, "offset": [0.0, 0.0], "rotate": 0.0}
    offset = _point(raw.get("offset", [0, 0]), f"{label}.offset")
    return {"shape": shape, "width": _finite(raw.get("width"), f"{label}.width", positive=True),
            "depth": _finite(raw.get("depth"), f"{label}.depth", positive=True),
            "offset": [offset[0], offset[1]], "rotate": _finite(raw.get("rotate", 0), f"{label}.rotate")}


def _parse_props(raw: Any, spec: Mapping, spec_dir: Path, layout: Layout) -> dict[str, PropKind]:
    entries = _mapping(raw if raw is not None else {}, "props")
    pack, pack_dir = _prop_pack_items(spec.get("prop_pack"), spec_dir, layout)
    kinds: dict[str, PropKind] = {}
    for name, value in entries.items():
        _ident(name, "prop kind name")
        label = f"props.{name}"
        value = _mapping(value, label)
        sources = value.get("variants")
        if sources is None:
            sources = [value]
        elif not isinstance(sources, list) or not sources:
            raise LayoutError(f"{label}.variants must be a nonempty list of {{image|pixelspec|pack}} objects")
        elif any(key in value for key in ("image", "pixelspec", "pack")):
            raise LayoutError(f"{label}: give image, pixelspec or pack inside variants, not beside them")
        variants, pack_meta = [], {}
        for number, source in enumerate(sources):
            source = _mapping(source, f"{label}.variants[{number}]")
            if number and not any(key in source for key in ("image", "pixelspec", "pack")):
                raise LayoutError(f"{label}.variants[{number}] needs image, pixelspec or pack")
            variant, meta = _variant_from_source(source, name, number, spec_dir, pack, pack_dir, layout, label)
            variants.append(variant)
            pack_meta = pack_meta or meta
        occlusion = value.get("occlusion", pack_meta.get("occlusion_class", "low"))
        if occlusion not in OCCLUSION_CLASSES:
            raise LayoutError(f"{label}.occlusion must be one of {', '.join(OCCLUSION_CLASSES)}")
        footprint_raw = value.get("footprint", pack_meta.get("footprint"))
        is_rect = isinstance(footprint_raw, Mapping) and footprint_raw.get("shape") == "rect"
        if variants[0].origin == "placeholder":
            size_raw = value.get("size", PLACEHOLDER_SIZES["rect" if is_rect else occlusion])
            if not isinstance(size_raw, (list, tuple)) or len(size_raw) != 2:
                raise LayoutError(f"{label}.size must be [width, height] in pixels")
            size = (_integer(size_raw[0], f"{label}.size", minimum=4),
                    _integer(size_raw[1], f"{label}.size", minimum=4))
            colour = _colour(value.get("color", PLACEHOLDER_COLOURS["rect" if is_rect else occlusion]),
                             f"{label}.color")
            image = _placeholder_image(size, colour, occlusion, is_rect)
            anchor = _point(value["anchor_px"], f"{label}.anchor_px") if "anchor_px" in value else \
                (size[0] / 2.0, float(size[1]))
            variants[0] = PropVariant(name, image, anchor, "placeholder")
        size0 = (variants[0].image.shape[1], variants[0].image.shape[0])
        footprint = (_parse_footprint(footprint_raw, f"{label}.footprint") if footprint_raw is not None
                     else _default_footprint(size0, occlusion, is_rect))
        solid_default = bool(pack_meta.get("solid", footprint["shape"] != "none"))
        solid = _boolean(value.get("solid", solid_default), f"{label}.solid")
        if solid and footprint["shape"] == "none":
            raise LayoutError(f"{label}: a solid prop needs an ellipse or rect footprint")
        interactions = []
        for index, item in enumerate(value.get("interactions") or []):
            item = _mapping(item, f"{label}.interactions[{index}]")
            interactions.append({"name": _ident(item.get("name"), f"{label}.interactions[{index}].name"),
                                 "offset": _point(item.get("offset", [0, 0]), f"{label}.interactions[{index}].offset"),
                                 "reach": _finite(item["reach"], f"{label}.interactions[{index}].reach", positive=True)
                                 if "reach" in item else None})
        kinds[name] = PropKind(name, variants, footprint, solid, occlusion,
                               _boolean(value.get("flip", True), f"{label}.flip"),
                               _finite(value.get("scale", 1), f"{label}.scale", positive=True), interactions)
    prop_ids = [variant.prop_id for kind in kinds.values() for variant in kind.variants]
    if len(set(prop_ids)) != len(prop_ids):
        raise LayoutError("prop variant ids collide; a kind named '<other>-v<n>' clashes with a variant id")
    return kinds


def _parse_clearance(raw: Any, label: str) -> dict[str, float]:
    values = dict(CLEARANCE_DEFAULTS)
    for key, value in _mapping(raw if raw is not None else {}, label).items():
        if key not in CLEARANCE_KEYS:
            raise LayoutError(f"{label}: unknown clearance {key!r}; use {', '.join(CLEARANCE_KEYS)}")
        values[key] = _finite(value, f"{label}.{key}", minimum=0)
    return values


def _parse_scatter(raw: Any, kinds: dict[str, PropKind], world: tuple[int, int]) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LayoutError("scatter must be a list of scatter groups")
    groups, ids = [], set()
    for index, item in enumerate(raw):
        label = f"scatter[{index}]"
        item = _mapping(item, label)
        group_id = _ident(item.get("id", f"scatter-{index + 1}"), f"{label}.id")
        if group_id in ids:
            raise LayoutError(f"{label}: duplicate scatter id {group_id!r}")
        ids.add(group_id)
        weights = _mapping(item.get("kinds"), f"{label}.kinds")
        if not weights:
            raise LayoutError(f"{label}.kinds must name at least one prop kind with a weight")
        for name, weight in weights.items():
            if name not in kinds:
                raise LayoutError(f"{label}.kinds: unknown prop kind {name!r}")
            _finite(weight, f"{label}.kinds.{name}", positive=True)
        density = _mapping(item.get("density", {}), f"{label}.density")
        region = item.get("region", [0, 0, world[0], world[1]])
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise LayoutError(f"{label}.region must be [x0, y0, x1, y1] in world pixels")
        x0, y0, x1, y1 = (_finite(v, f"{label}.region") for v in region)
        region = (max(0.0, x0), max(0.0, y0), min(float(world[0]), x1), min(float(world[1]), y1))
        if region[0] >= region[2] or region[1] >= region[3]:
            raise LayoutError(f"{label}.region must have x0 < x1 and y0 < y1 inside the map")
        near = {}
        for key, band in _mapping(item.get("near", {}), f"{label}.near").items():
            if key not in CLEARANCE_KEYS:
                raise LayoutError(f"{label}.near: unknown field {key!r}; use {', '.join(CLEARANCE_KEYS)}")
            if not isinstance(band, (list, tuple)) or len(band) != 2:
                raise LayoutError(f"{label}.near.{key} must be [min, max] pixels")
            low = _finite(band[0], f"{label}.near.{key}", minimum=0)
            high = _finite(band[1], f"{label}.near.{key}")
            if high < low:
                raise LayoutError(f"{label}.near.{key} must be ordered [min, max]")
            near[key] = (low, high)
        count = _integer(item.get("count"), f"{label}.count", minimum=0)
        groups.append({
            "id": group_id, "kinds": {name: float(weight) for name, weight in weights.items()},
            "count": count,
            "spacing": _finite(item.get("spacing"), f"{label}.spacing", positive=True),
            "attempts": _integer(item.get("attempts", max(200, 40 * count)), f"{label}.attempts", minimum=1),
            "density": {"scale": _finite(density.get("scale", 96), f"{label}.density.scale", positive=True),
                        "octaves": _integer(density.get("octaves", 2), f"{label}.density.octaves", minimum=1),
                        "threshold": _finite(density.get("threshold", 0.0), f"{label}.density.threshold"),
                        "edge_bonus": _finite(density.get("edge_bonus", 0.0), f"{label}.density.edge_bonus"),
                        "edge_distance": _finite(density.get("edge_distance", 64), f"{label}.density.edge_distance",
                                                 positive=True)},
            "clearance": _parse_clearance(item.get("clearance"), f"{label}.clearance"),
            "near": near, "region": region,
        })
    return groups


def _parse_exits(raw: Any, layout: Layout) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LayoutError("exits must be a list")
    roads = {op["id"]: op for op in layout.terrain if op["shape"] == "road"}
    exits, ids = [], set()
    for index, item in enumerate(raw):
        label = f"exits[{index}]"
        item = _mapping(item, label)
        exit_id = _ident(item.get("id"), f"{label}.id")
        if exit_id in ids:
            raise LayoutError(f"{label}: duplicate exit id {exit_id!r}")
        ids.add(exit_id)
        edge = item.get("edge")
        if edge not in EDGES:
            raise LayoutError(f"{label}.edge must be one of {', '.join(EDGES)}")
        target = item.get("to")
        if not isinstance(target, str) or not target.strip():
            raise LayoutError(f"{label}.to must name the destination ('map' or 'map:spawn')")
        entry = {"id": exit_id, "edge": edge, "to": target,
                 "depth": _finite(item.get("depth", layout.tile / 2.0), f"{label}.depth", positive=True),
                 "radius": _finite(item.get("radius", layout.actor_radius), f"{label}.radius", minimum=0),
                 "arrival": _ident(item.get("arrival", f"from-{exit_id}"), f"{label}.arrival"),
                 "road": None, "span": None}
        if "span" in item:
            span = item["span"]
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                raise LayoutError(f"{label}.span must be [a, b] in tiles along the edge")
            a, b = (_finite(v, f"{label}.span") for v in span)
            if a >= b:
                raise LayoutError(f"{label}.span must have a < b")
            entry["span"] = (a, b)
        elif "road" in item:
            if item["road"] not in roads:
                raise LayoutError(f"{label}.road: no road terrain op with id {item['road']!r}")
            entry["road"] = item["road"]
        else:
            raise LayoutError(f"{label} needs road (a road id) or span [a, b]")
        exits.append(entry)
    return exits


def _parse_points(raw: Any, name: str, *, reach_default: float | None) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LayoutError(f"{name} must be a list")
    items, ids = [], set()
    for index, item in enumerate(raw):
        label = f"{name}[{index}]"
        item = _mapping(item, label)
        identity = _ident(item.get("id"), f"{label}.id")
        if identity in ids:
            raise LayoutError(f"{label}: duplicate id {identity!r}")
        ids.add(identity)
        entry: dict[str, Any] = {"id": identity, "x": _finite(item.get("x"), f"{label}.x"),
                                 "y": _finite(item.get("y"), f"{label}.y")}
        if reach_default is None:
            facing = item.get("facing", "south")
            if not isinstance(facing, (str, int, float)) or isinstance(facing, bool):
                raise LayoutError(f"{label}.facing must be a direction name or an angle")
            entry["facing"] = facing
        else:
            entry["reach"] = _finite(item.get("reach", reach_default), f"{label}.reach", positive=True)
        items.append(entry)
    return items


def load_layout(spec_path: Path, seed_override: int | None) -> Layout:
    """Read and check a codeart2d.layout_spec.v1 file."""
    try:
        raw_bytes = spec_path.read_bytes()
    except FileNotFoundError:
        raise LayoutError(f"spec not found: {spec_path}") from None
    try:
        spec = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LayoutError(f"spec {spec_path.name} is not valid UTF-8 JSON: {error}") from None
    spec = _mapping(spec, "spec")
    if spec.get("schema") != SPEC_SCHEMA:
        raise LayoutError(f"spec schema must be {SPEC_SCHEMA!r} (got {spec.get('schema')!r})")
    size = spec.get("size")
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise LayoutError("size must be [width, height] in tiles")
    width, height = (_integer(v, "size", minimum=2) for v in size)
    tile = _integer(spec.get("tile_size", 16), "tile_size", minimum=2)
    if width * height * tile * tile > MAX_WORLD_PIXELS:
        raise LayoutError(f"the map is {width * tile}x{height * tile} px; at most {MAX_WORLD_PIXELS} pixels "
                          "are supported")
    materials = _parse_materials(spec.get("materials"))
    seed = seed_override if seed_override is not None else _integer(spec.get("seed", 0), "seed", minimum=0)
    actor = _mapping(spec.get("actor", {}), "actor")
    layout = Layout(
        spec_path=spec_path, spec_bytes=raw_bytes,
        map_id=_ident(spec.get("map_id", spec_path.stem.split(".")[0] or "map"), "map_id"),
        width=width, height=height, tile=tile, seed=seed, materials=materials, base=materials[0], terrain=[],
        hygiene={}, actor_radius=_finite(actor.get("radius", max(1, _round_half_up(0.3 * tile))), "actor.radius",
                                         positive=True),
        y_squash=_finite(actor.get("ySquash", 1.0), "actor.ySquash", positive=True),
        kinds={}, objects=[], scatter=[], exits=[], spawns=[], interactions=[])
    layout.base = layout.material(spec.get("base", materials[0].name), "base")
    if not layout.base.walkable:
        raise LayoutError(f"base material {layout.base.name!r} must be walkable")
    hygiene = _mapping(spec.get("hygiene", {}), "hygiene")
    layout.hygiene = {key: _boolean(hygiene.get(key, True), f"hygiene.{key}") for key in ("saddles", "specks")}
    layout.terrain = _parse_terrain(spec.get("terrain", []), layout)
    spec_dir = spec_path.resolve().parent
    layout.kinds = _parse_props(spec.get("props"), spec, spec_dir, layout)
    for index, item in enumerate(spec.get("objects") or []):
        label = f"objects[{index}]"
        item = _mapping(item, label)
        kind_name = item.get("prop")
        if kind_name not in layout.kinds:
            raise LayoutError(f"{label}.prop: unknown prop kind {kind_name!r}")
        kind = layout.kinds[kind_name]
        variant = _integer(item.get("variant", 0), f"{label}.variant", minimum=0)
        if variant >= len(kind.variants):
            raise LayoutError(f"{label}.variant {variant} is out of range for {kind_name!r}")
        layout.objects.append({"id": _ident(item.get("id"), f"{label}.id"), "kind": kind, "variant": variant,
                               "x": _finite(item.get("x"), f"{label}.x"), "y": _finite(item.get("y"), f"{label}.y"),
                               "flip": _boolean(item.get("flip_x", False), f"{label}.flip_x"),
                               "scale": _finite(item.get("scale", kind.scale), f"{label}.scale", positive=True)})
    layout.scatter = _parse_scatter(spec.get("scatter"), layout.kinds, layout.world)
    layout.exits = _parse_exits(spec.get("exits"), layout)
    layout.spawns = _parse_points(spec.get("spawns"), "spawns", reach_default=None)
    reach_default = layout.actor_radius + tile / 2.0
    layout.interactions = _parse_points(spec.get("interactions"), "interactions", reach_default=reach_default)
    if not layout.spawns and not layout.exits:
        raise LayoutError("the layout needs at least one spawn or one exit to judge reachability from")
    names = [entry["id"] for entry in layout.objects] + [group["id"] for group in layout.scatter]
    if len(set(names)) != len(names):
        raise LayoutError("object ids and scatter group ids must be unique")
    spawn_ids = [spawn["id"] for spawn in layout.spawns] + [entry["arrival"] for entry in layout.exits]
    if len(set(spawn_ids)) != len(spawn_ids):
        raise LayoutError("spawn ids and exit arrival ids must all differ (set exits[].arrival to rename one)")
    return layout


# ----------------------------------------------------------------------------- terrain

def paint_vertices(layout: Layout) -> tuple[np.ndarray, list[dict]]:
    """Paint the vertex grid in spec order; returns the grid and the road centre lines (tiles)."""
    grid = np.full((layout.height + 1, layout.width + 1), layout.base.index, np.int16)
    vy, vx = np.mgrid[0:layout.height + 1, 0:layout.width + 1].astype(np.float64)
    roads = []
    for order, op in enumerate(layout.terrain):
        if op["shape"] == "ellipse":
            (cx, cy), (rx, ry) = op["center"], op["radius"]
            dx, dy = vx - cx, vy - cy
            if op["rotate"]:
                angle = math.radians(op["rotate"])
                dx, dy = dx * math.cos(angle) + dy * math.sin(angle), -dx * math.sin(angle) + dy * math.cos(angle)
            rho = np.hypot(dx / rx, dy / ry)
            limit = 1.0
            if op["wobble"] > 0:
                rng = np.random.default_rng([layout.seed, 17, order])
                phases = rng.uniform(0.0, 2.0 * math.pi, 3)
                theta = np.arctan2(dy / ry, dx / rx)
                wave = sum(weight * np.sin(k * theta + phase)
                           for (k, weight), phase in zip(((2, 1.0), (3, 0.6), (5, 0.3)), phases)) / 1.9
                limit = 1.0 + op["wobble"] / ((rx + ry) / 2.0) * wave
            mask = rho <= limit
        elif op["shape"] == "rect":
            x0, y0, x1, y1 = op["box"]
            mask = (vx >= x0) & (vx <= x1) & (vy >= y0) & (vy <= y1)
        elif op["shape"] == "polygon":
            mask = _inside_polygon(vx, vy, op["points"])
        else:
            line = _catmull_rom(op["points"], op["smooth"])
            mask = _distance_to_polyline(vx, vy, line) <= op["width"] / 2.0
            roads.append({"id": op["id"], "material": op["material"].name, "width": op["width"], "line": line})
        grid[mask] = op["material"].index
    return grid, roads


def _cell_keys(grid: np.ndarray, count: int) -> np.ndarray:
    """Corner key ((tl * M + tr) * M + bl) * M + br per cell, M = material count."""
    g = grid.astype(np.int64)
    return ((g[:-1, :-1] * count + g[:-1, 1:]) * count + g[1:, :-1]) * count + g[1:, 1:]


def _demote_unrepresentable(grid: np.ndarray, allowed: np.ndarray | None, layout: Layout) -> int:
    """Turn the lowest-priority non-base corner material of every undrawable cell into the base."""
    if allowed is None:
        return 0
    count, base = len(layout.materials), layout.base.index
    priority = np.array([material.priority for material in layout.materials], np.float64)
    priority[base] = np.inf
    demoted = 0
    while True:
        bad = ~np.isin(_cell_keys(grid, count), allowed)
        if not bad.any():
            return demoted
        rows, cols = np.nonzero(bad)
        corners = np.stack([grid[rows, cols], grid[rows, cols + 1], grid[rows + 1, cols], grid[rows + 1, cols + 1]], 1)
        ranks = priority[corners]
        loser = corners[np.arange(len(rows)), np.argmin(ranks, axis=1)]
        if np.all(loser == base):
            raise LayoutError("a cell of the base material alone has no tile; the tilesets need a full base tile")
        for dy, dx, column in ((0, 0, 0), (0, 1, 1), (1, 0, 2), (1, 1, 3)):
            hit = (corners[:, column] == loser) & (loser != base)
            grid[rows[hit] + dy, cols[hit] + dx] = base
            demoted += int(hit.sum())


def run_hygiene(grid: np.ndarray, allowed: np.ndarray | None, layout: Layout) -> dict:
    """Make every cell drawable, fill diagonal saddles and drop lone vertices (iterated to a fixed point)."""
    report = {"demoted_vertices": 0, "saddles_filled": 0, "specks_removed": 0, "passes": 0, "converged": False}
    base = layout.base.index
    ordered = sorted((material for material in layout.materials if material.index != base),
                     key=lambda material: (-material.priority, material.index))
    for _ in range(HYGIENE_MAX_PASSES):
        before = grid.copy()
        report["passes"] += 1
        report["demoted_vertices"] += _demote_unrepresentable(grid, allowed, layout)
        if layout.hygiene["saddles"]:
            for material in ordered:
                m = grid == material.index
                tl, tr, bl, br = m[:-1, :-1], m[:-1, 1:], m[1:, :-1], m[1:, 1:]
                down = tl & br & ~tr & ~bl   # fill the top-right corner
                up = tr & bl & ~tl & ~br     # fill the top-left corner
                rows, cols = np.nonzero(down)
                grid[rows, cols + 1] = material.index
                rows2, cols2 = np.nonzero(up)
                grid[rows2, cols2] = material.index
                report["saddles_filled"] += int(len(rows) + len(rows2))
            report["demoted_vertices"] += _demote_unrepresentable(grid, allowed, layout)
        if layout.hygiene["specks"]:
            padded = np.pad(grid, 1, mode="edge")
            same = ((padded[:-2, 1:-1] == grid) | (padded[2:, 1:-1] == grid)
                    | (padded[1:-1, :-2] == grid) | (padded[1:-1, 2:] == grid))
            lone = (grid != base) & ~same
            grid[lone] = base
            report["specks_removed"] += int(lone.sum())
        if np.array_equal(before, grid):
            report["converged"] = True
            break
    report["demoted_vertices"] += _demote_unrepresentable(grid, allowed, layout)
    return report


def pixel_materials(grid: np.ndarray, tile: int, world: tuple[int, int]) -> np.ndarray:
    """Material index per world pixel: the nearest vertex, so each vertex owns a tile-sized square."""
    columns = np.floor((np.arange(world[0]) + 0.5) / tile + 0.5).astype(np.int64)
    rows = np.floor((np.arange(world[1]) + 0.5) / tile + 0.5).astype(np.int64)
    return grid[np.ix_(rows, columns)]


# ----------------------------------------------------------------------------- tilesets

@dataclass
class Tileset:
    id: str
    manifest_path: Path
    manifest: dict
    image_path: Path
    tiles: np.ndarray  # (count, T, T, 4) in index order
    lookup: dict[int, list[int]]
    ignored_tiles: int


def load_tilesets(paths: Sequence[Path], layout: Layout) -> list[Tileset]:
    tilesets: list[Tileset] = []
    names = {material.name: material.index for material in layout.materials}
    count = len(layout.materials)
    for path in paths:
        path = path.resolve()
        data = _mapping(_load_json(path, "tileset manifest"), f"tileset {path.name}")
        if data.get("schema") != TILESET_SCHEMA:
            raise LayoutError(f"tileset {path.name}: schema must be {TILESET_SCHEMA}")
        kind = data.get("kind")
        if kind not in ("wang_corner", "flat"):
            raise LayoutError(f"tileset {path.name}: layout_build reads wang_corner and flat tilesets, not {kind!r}")
        size = data.get("tile_size")
        if isinstance(size, list):
            if len(size) != 2 or size[0] != size[1]:
                raise LayoutError(f"tileset {path.name}: tiles must be square")
            size = size[0]
        if size != layout.tile:
            raise LayoutError(f"tileset {path.name}: tile_size {size} does not match the layout tile_size "
                              f"{layout.tile}")
        columns = _integer(data.get("columns"), f"tileset {path.name} columns", minimum=1)
        materials = data.get("materials")
        if not isinstance(materials, list) or not materials:
            raise LayoutError(f"tileset {path.name}: materials must be a nonempty list")
        image_path = (path.parent / str(data.get("image", ""))).resolve()
        atlas = _load_image(image_path, f"tileset {path.name} image")
        rows_available = atlas.shape[0] // layout.tile
        tiles_raw = data.get("tiles")
        if not isinstance(tiles_raw, list) or not tiles_raw or not all(isinstance(t, Mapping) for t in tiles_raw):
            raise LayoutError(f"tileset {path.name}: tiles must be a nonempty list of objects")
        top = max(_integer(tile.get("index"), f"tileset {path.name} tile index", minimum=0) for tile in tiles_raw)
        if top // columns >= rows_available or columns * layout.tile > atlas.shape[1]:
            raise LayoutError(f"tileset {path.name}: tile {top} lies outside the "
                              f"{atlas.shape[1]}x{atlas.shape[0]} image")
        stack = np.zeros((top + 1, layout.tile, layout.tile, 4), np.uint8)
        lookup: dict[int, list[tuple[int, int]]] = {}
        ignored = 0
        for tile in tiles_raw:
            index = tile["index"]
            row, column = divmod(index, columns)
            tile_px = layout.tile
            stack[index] = atlas[row * tile_px:(row + 1) * tile_px, column * tile_px:(column + 1) * tile_px]
            if kind == "flat":
                corners = [0, 0, 0, 0] if len(materials) == 1 else None
            else:
                corners = tile.get("wang")
            if not isinstance(corners, list) or len(corners) != 4 or \
                    not all(isinstance(c, int) and 0 <= c < len(materials) for c in corners):
                ignored += 1
                continue
            local = [names.get(materials[c]) for c in corners]
            if any(value is None for value in local):
                ignored += 1
                continue
            key = ((local[0] * count + local[1]) * count + local[2]) * count + local[3]
            lookup.setdefault(key, []).append((int(tile.get("variant", 0)), index))
        identity = data.get("id") if isinstance(data.get("id"), str) and ID_PATTERN.fullmatch(data.get("id", "")) \
            else "-".join(str(name) for name in materials)
        identity = _ident(re.sub(r"[^A-Za-z0-9_.-]", "-", identity).strip("-.") or "tileset", "tileset id")
        while identity in {item.id for item in tilesets}:
            identity += "-2"
        layout.inputs.extend([path, image_path])
        tilesets.append(Tileset(identity, path, dict(data), image_path, stack,
                                {key: [index for _, index in sorted(values)] for key, values in lookup.items()},
                                ignored))
    return tilesets


def allowed_keys(tilesets: Sequence[Tileset]) -> np.ndarray | None:
    if not tilesets:
        return None
    return np.array(sorted({key for tileset in tilesets for key in tileset.lookup}), np.int64)


def _hash01(*values: int) -> np.ndarray | float:
    """A deterministic integer hash mapped to [0, 1); vectorised over numpy integer arrays."""
    h = np.uint64(0x9E3779B97F4A7C15)
    mask = np.uint64(0xFFFFFFFFFFFFFFFF)
    with np.errstate(over="ignore"):
        for value in values:
            h = (h ^ (np.asarray(value).astype(np.uint64) + np.uint64(0x9E3779B97F4A7C15))) & mask
            h = (h ^ (h >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
            h = (h ^ (h >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
            h = h ^ (h >> np.uint64(31))
    return (h >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def autotile(grid: np.ndarray, tilesets: Sequence[Tileset], layout: Layout) -> tuple[list[np.ndarray], int]:
    """Tile index per cell for each tileset (-1 elsewhere) and the number of cells left without a tile."""
    keys = _cell_keys(grid, len(layout.materials))
    height, width = keys.shape
    owner = np.full(keys.shape, -1, np.int16)
    chosen = np.full(keys.shape, -1, np.int32)
    pick = _hash01(layout.seed, 4099, np.arange(height)[:, None], np.arange(width)[None, :])
    for key in np.unique(keys).tolist():
        for number, tileset in enumerate(tilesets):
            if key in tileset.lookup:
                owner[keys == key] = number
                break
    missing = int((owner < 0).sum())
    for y in range(height):
        for x in range(width):
            number = int(owner[y, x])
            if number < 0:
                continue
            candidates = tilesets[number].lookup[int(keys[y, x])]
            start = int(pick[y, x] * len(candidates))
            choice = candidates[start]
            if len(candidates) > 1:
                neighbours = {int(chosen[y, x - 1]) if x else -1, int(chosen[y - 1, x]) if y else -1}
                for step in range(len(candidates)):
                    candidate = candidates[(start + step) % len(candidates)]
                    if candidate not in neighbours:
                        choice = candidate
                        break
            chosen[y, x] = choice
    layers = [np.where(owner == number, chosen, -1) for number in range(len(tilesets))]
    return layers, missing


# ----------------------------------------------------------------------------- placement

@dataclass
class Fields:
    """Clearance distance fields in world pixels, sampled on a `step` px grid."""
    step: int
    road: np.ndarray
    blocked: np.ndarray
    object: np.ndarray
    exit: np.ndarray
    spawn: np.ndarray
    world: tuple[int, int]

    def sample(self, name: str, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        if name == "edge":
            return np.minimum.reduce([xs, ys, self.world[0] - xs, self.world[1] - ys])
        data = getattr(self, name)
        rows = np.clip((ys // self.step).astype(np.int64), 0, data.shape[0] - 1)
        cols = np.clip((xs // self.step).astype(np.int64), 0, data.shape[1] - 1)
        return data[rows, cols] * self.step


def exit_geometry(exits: Sequence[dict], grid: np.ndarray, roads: Sequence[dict], layout: Layout) -> None:
    """Fill in each exit's trigger rect (world px), span (tiles) and travel direction.

    A road exit spans the run of the road's material along the edge where the road
    centre line meets it, extended by half a tile at both ends (the vertex squares)."""
    road_by_id = {road["id"]: road for road in roads}
    tile = layout.tile
    for entry in exits:
        edge = entry["edge"]
        horizontal = edge in ("north", "south")
        limit = layout.width if horizontal else layout.height
        if entry["span"] is not None:
            a, b = entry["span"]
        else:
            road = road_by_id[entry["road"]]
            line = road["line"]
            along, across = (line[:, 0], line[:, 1]) if horizontal else (line[:, 1], line[:, 0])
            target = {"west": 0.0, "north": 0.0, "east": float(layout.width), "south": float(layout.height)}[edge]
            nearest = int(np.argmin(np.abs(across - target)))
            if abs(float(across[nearest]) - target) > road["width"] / 2.0:
                raise LayoutError(f"exit {entry['id']}: road {entry['road']} does not reach the {edge} edge")
            vertex = int(np.clip(_round_half_up(float(along[nearest])), 0, limit))
            column = {"west": grid[:, 0], "east": grid[:, -1], "north": grid[0, :], "south": grid[-1, :]}[edge]
            column = column == layout.material(road["material"], "road").index
            if not column[vertex]:
                raise LayoutError(f"exit {entry['id']}: road {entry['road']} does not reach the {edge} edge "
                                  "after hygiene (widen the road or end it beyond the edge)")
            start = vertex
            while start > 0 and column[start - 1]:
                start -= 1
            end = vertex
            while end < limit and column[end + 1]:
                end += 1
            a, b = max(0.0, start - 0.5), min(float(limit), end + 0.5)
        if not (0 <= a < b <= limit):
            raise LayoutError(f"exit {entry['id']}: span [{a}, {b}] must lie within the {edge} edge (0..{limit} tiles)")
        world_w, world_h = layout.world
        depth = entry["depth"]
        if edge == "west":
            rect = [0.0, a * tile, depth, (b - a) * tile]
        elif edge == "east":
            rect = [world_w - depth, a * tile, depth, (b - a) * tile]
        elif edge == "north":
            rect = [a * tile, 0.0, (b - a) * tile, depth]
        else:
            rect = [a * tile, world_h - depth, (b - a) * tile, depth]
        entry["span_tiles"] = (a, b)
        entry["rect"] = rect
        entry["direction"] = EDGES[edge]


def build_fields(layout: Layout, materials_px: np.ndarray, road_materials: set[int], fixed: Sequence[Instance],
                 exits: Sequence[dict], points: Sequence[tuple[float, float]]) -> Fields:
    world_w, world_h = layout.world
    step = max(1, int(math.ceil(math.sqrt(world_w * world_h / FIELD_TARGET_CELLS))))
    sampled = materials_px[step // 2::step, step // 2::step]
    shape = sampled.shape
    walkable = np.array([material.walkable for material in layout.materials])
    road = np.isin(sampled, sorted(road_materials)) if road_materials else np.zeros(shape, bool)
    blocked = ~walkable[sampled]
    objects = np.zeros(shape, bool)
    for instance in fixed:
        left, top, width, height = instance.sprite_rect()
        objects[max(0, top // step):max(0, -(-(top + height) // step)),
                max(0, left // step):max(0, -(-(left + width) // step))] = True
    exits_mask = np.zeros(shape, bool)
    for entry in exits:
        x, y, w, h = entry["rect"]
        reach = entry["depth"] + entry["radius"] + 2.0 * layout.tile
        dx, dy = entry["direction"]
        x0, y0, x1, y1 = x, y, x + w, y + h
        x0, x1 = (x0 - reach, x1) if dx > 0 else (x0, x1 + reach) if dx < 0 else (x0, x1)
        y0, y1 = (y0 - reach, y1) if dy > 0 else (y0, y1 + reach) if dy < 0 else (y0, y1)
        rows = slice(max(0, int(y0 // step)), max(0, int(-(-y1 // step))))
        exits_mask[rows, max(0, int(x0 // step)):max(0, int(-(-x1 // step)))] = True
    spawn = np.zeros(shape, bool)
    for x, y in points:
        spawn[int(np.clip(y // step, 0, shape[0] - 1)), int(np.clip(x // step, 0, shape[1] - 1))] = True
    reach = [value for group in layout.scatter for value in group["clearance"].values()]
    reach += [band[1] for group in layout.scatter for band in group["near"].values()]
    cap = (max(reach, default=0.0) + 2.0 * step) / step  # every compared distance stays below the cap
    return Fields(step, _distance_field(road, cap), _distance_field(blocked, cap), _distance_field(objects, cap),
                  _distance_field(exits_mask, cap), _distance_field(spawn, cap), (world_w, world_h))


def _value_noise(xs: np.ndarray, ys: np.ndarray, world: tuple[int, int], scale: float, octaves: int,
                 rng: np.random.Generator) -> np.ndarray:
    """Smooth fBm value noise in [0, 1] at world points (bilinear lattice with smoothstep)."""
    total = np.zeros(xs.shape)
    amplitude, norm, frequency = 1.0, 0.0, 1.0 / scale
    for _ in range(octaves):
        lattice = rng.random((int(world[1] * frequency) + 3, int(world[0] * frequency) + 3))
        fx, fy = xs * frequency, ys * frequency
        x0, y0 = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
        tx, ty = fx - x0, fy - y0
        sx, sy = tx * tx * (3 - 2 * tx), ty * ty * (3 - 2 * ty)
        top = lattice[y0, x0] * (1 - sx) + lattice[y0, x0 + 1] * sx
        bottom = lattice[y0 + 1, x0] * (1 - sx) + lattice[y0 + 1, x0 + 1] * sx
        total += amplitude * (top * (1 - sy) + bottom * sy)
        norm += amplitude
        amplitude *= 0.5
        frequency *= 2.0
    return total / norm


def scatter_props(layout: Layout, fields: Fields) -> tuple[list[Instance], list[dict]]:
    """Seeded scatter groups: candidates filtered by density and clearance, Poisson-disk spacing,
    weighted looks (kind, variant, mirror) that avoid repeating the nearest neighbour's look."""
    placed: list[Instance] = []
    spacing_of: list[float] = []
    reports = []
    bucket: dict[tuple[int, int], list[int]] = {}
    cell = max(group["spacing"] for group in layout.scatter) if layout.scatter else 1.0

    def neighbours(x: float, y: float, radius: float) -> list[int]:
        found = []
        reach = int(math.ceil(radius / cell))
        cx, cy = int(x // cell), int(y // cell)
        for gy in range(cy - reach, cy + reach + 1):
            for gx in range(cx - reach, cx + reach + 1):
                found.extend(bucket.get((gx, gy), ()))
        return found

    for number, group in enumerate(layout.scatter):
        rng = np.random.default_rng([layout.seed, 1009, number])
        x0, y0, x1, y1 = group["region"]
        xs = rng.uniform(x0, x1, group["attempts"])
        ys = rng.uniform(y0, y1, group["attempts"])
        density = group["density"]
        keep = np.ones(xs.shape, bool)
        if density["threshold"] > 0 or density["edge_bonus"]:
            noise = _value_noise(xs, ys, layout.world, density["scale"], density["octaves"], rng)
            if density["edge_bonus"]:
                edge = fields.sample("edge", xs, ys)
                noise = noise + density["edge_bonus"] * np.clip(1.0 - edge / density["edge_distance"], 0.0, 1.0)
            keep &= noise >= density["threshold"]
        for name, minimum in group["clearance"].items():
            if minimum > 0:
                keep &= fields.sample(name, xs, ys) >= minimum
        for name, (low, high) in group["near"].items():
            distance = fields.sample(name, xs, ys)
            keep &= (distance >= low) & (distance <= high)
        looks = [(name, variant, mirrored) for name in group["kinds"]
                 for variant in range(len(layout.kinds[name].variants))
                 for mirrored in ((False, True) if layout.kinds[name].flip else (False,))]
        log_weights = np.log(np.array([group["kinds"][name] / (len(layout.kinds[name].variants)
                                                               * (2 if layout.kinds[name].flip else 1))
                                       for name, _, _ in looks]))
        index_of = {look: number for number, look in enumerate(looks)}
        reach = 2.0 * group["spacing"]
        count = 0
        mine: list[int] = []
        for x, y in zip(xs[keep].tolist(), ys[keep].tolist()):
            if count >= group["count"]:
                break
            near = neighbours(x, y, 2.0 * cell)
            if any(math.hypot(x - placed[i].x, y - placed[i].y) < (group["spacing"] + spacing_of[i]) / 2.0
                   for i in near):
                continue
            # Weighted choice by the Gumbel-max trick; each same-group neighbour within two spacings
            # penalises its own look, closer ones more, so nearby props rarely repeat a look.
            penalty = np.zeros(len(looks))
            for i in near:
                distance = math.hypot(x - placed[i].x, y - placed[i].y)
                if placed[i].group == group["id"] and distance <= reach:
                    penalty[index_of[placed[i].look]] += reach / max(distance, 1.0)
            gumbel = -np.log(-np.log(rng.uniform(1e-12, 1.0, len(looks))))
            look = looks[int(np.argmax(log_weights - 3.0 * penalty + gumbel))]
            kind = layout.kinds[look[0]]
            count += 1
            instance = Instance(f"{group['id']}-{count}", kind, look[1], float(_round_half_up(x)),
                                float(_round_half_up(y)), look[2], kind.scale, group["id"])
            placed.append(instance)
            spacing_of.append(group["spacing"])
            mine.append(len(placed) - 1)
            bucket.setdefault((int(instance.x // cell), int(instance.y // cell)), []).append(len(placed) - 1)
        reports.append({"id": group["id"], "requested": group["count"], "placed": count,
                        "candidates": int(keep.sum()), "looks": len(looks),
                        "same_look_neighbour_share": _same_look_share([placed[i] for i in mine], reach)})
    return placed, reports


def _same_look_share(instances: Sequence[Instance], reach: float) -> float | None:
    """Share of instances whose nearest group neighbour lies within `reach` px and has the same look."""
    if len(instances) < 2:
        return None
    xs = np.array([item.x for item in instances], np.float64)
    ys = np.array([item.y for item in instances], np.float64)
    distance = np.hypot(xs[:, None] - xs[None, :], ys[:, None] - ys[None, :])
    np.fill_diagonal(distance, np.inf)
    nearest = np.argmin(distance, axis=1)
    same = [instances[i].look == instances[int(j)].look and distance[i, j] <= reach for i, j in enumerate(nearest)]
    return round(float(np.mean(same)), 4)


# ----------------------------------------------------------------------------- collision

def terrain_solids(grid: np.ndarray, layout: Layout) -> list[dict]:
    """Blocked terrain as exact rectangles: the union of tile-sized squares centred on vertices of
    non-walkable materials, merged per material on the half-tile grid and clipped to the map."""
    half = layout.tile / 2.0
    rows = (np.arange(2 * layout.height) + 1) // 2
    cols = (np.arange(2 * layout.width) + 1) // 2
    halves = grid[np.ix_(rows, cols)]
    solids = []
    for material in layout.materials:
        if material.walkable:
            continue
        for number, (col, row, width, height) in enumerate(_run_rects(halves == material.index), 1):
            solids.append({"id": f"terrain-{material.name}-{number}", "shape": "rect", "x": _num(col * half),
                           "y": _num(row * half), "w": _num(width * half), "h": _num(height * half),
                           "material": material.name})
    return solids


def object_solid(instance: Instance) -> dict | None:
    """The placed footprint of a solid prop in world pixels (scaled once by the instance scale)."""
    if not instance.kind.solid:
        return None
    footprint = instance.footprint()
    scale = instance.scale
    cx = instance.x + footprint["offset"][0] * scale
    cy = instance.y + footprint["offset"][1] * scale
    width, depth, rotate = footprint["width"] * scale, footprint["depth"] * scale, footprint["rotate"]
    if footprint["shape"] == "ellipse":
        solid = {"id": instance.id, "shape": "ellipse", "cx": _num(cx), "cy": _num(cy), "rx": _num(width / 2.0),
                 "ry": _num(depth / 2.0)}
        if rotate:
            solid["rotate"] = _num(rotate)
        return solid
    if not rotate:
        return {"id": instance.id, "shape": "rect", "x": _num(cx - width / 2.0), "y": _num(cy - depth / 2.0),
                "w": _num(width), "h": _num(depth)}
    angle = math.radians(rotate)
    corners = []
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        dx, dy = sx * width / 2.0, sy * depth / 2.0
        corners.append([_num(cx + dx * math.cos(angle) - dy * math.sin(angle)),
                        _num(cy + dx * math.sin(angle) + dy * math.cos(angle))])
    return {"id": instance.id, "shape": "polygon", "points": corners}


class SolidSet:
    """Closed point-in-solid tests (plan Appendix C solids), vectorised over points."""

    def __init__(self, solids: Sequence[Mapping]):
        self.solids = [dict(solid) for solid in solids]
        self.boxes = [self._bbox(solid) for solid in self.solids]

    @staticmethod
    def _bbox(solid: Mapping) -> tuple[float, float, float, float]:
        if solid["shape"] == "rect":
            return solid["x"], solid["y"], solid["x"] + solid["w"], solid["y"] + solid["h"]
        if solid["shape"] == "ellipse":
            reach = max(solid["rx"], solid["ry"]) if solid.get("rotate") else None
            rx, ry = (reach, reach) if reach is not None else (solid["rx"], solid["ry"])
            return solid["cx"] - rx, solid["cy"] - ry, solid["cx"] + rx, solid["cy"] + ry
        xs = [point[0] for point in solid["points"]]
        ys = [point[1] for point in solid["points"]]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def contains(solid: Mapping, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        if solid["shape"] == "rect":
            return (xs >= solid["x"]) & (xs <= solid["x"] + solid["w"]) & (ys >= solid["y"]) & \
                (ys <= solid["y"] + solid["h"])
        if solid["shape"] == "ellipse":
            if solid["rx"] <= 0 or solid["ry"] <= 0:
                return np.zeros(np.broadcast(xs, ys).shape, bool)
            dx, dy = xs - solid["cx"], ys - solid["cy"]
            if solid.get("rotate"):
                angle = math.radians(solid["rotate"])
                dx, dy = dx * math.cos(angle) + dy * math.sin(angle), -dx * math.sin(angle) + dy * math.cos(angle)
            return (dx / solid["rx"]) ** 2 + (dy / solid["ry"]) ** 2 <= 1.0
        return _inside_polygon(xs, ys, solid["points"])

    def any_contains(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        hit = np.zeros(np.broadcast(xs, ys).shape, bool)
        for solid, (x0, y0, x1, y1) in zip(self.solids, self.boxes):
            near = (xs >= x0) & (xs <= x1) & (ys >= y0) & (ys <= y1)
            if near.any():
                hit[near] |= self.contains(solid, xs[near], ys[near])
        return hit


def blocked_raster(solids: SolidSet, world: tuple[int, int], cell: float) -> np.ndarray:
    """Cells of the collision raster whose centre lies inside a solid (closed test)."""
    columns, rows = int(math.ceil(world[0] / cell)), int(math.ceil(world[1] / cell))
    centres_x = (np.arange(columns) + 0.5) * cell
    centres_y = (np.arange(rows) + 0.5) * cell
    raster = np.zeros((rows, columns), bool)
    for solid, (x0, y0, x1, y1) in zip(solids.solids, solids.boxes):
        c0, c1 = max(0, int(math.floor(x0 / cell - 0.5))), min(columns, int(math.ceil(x1 / cell + 0.5)) + 1)
        r0, r1 = max(0, int(math.floor(y0 / cell - 0.5))), min(rows, int(math.ceil(y1 / cell + 0.5)) + 1)
        if c0 >= c1 or r0 >= r1:
            continue
        xs, ys = np.meshgrid(centres_x[c0:c1], centres_y[r0:r1])
        raster[r0:r1, c0:c1] |= SolidSet.contains(solid, xs, ys)
    return raster


def raster_rects(raster: np.ndarray, world: tuple[int, int], cell: float) -> list[list[int | float]]:
    """Merged [x, y, w, h] rectangles (world px, clipped to the map) covering exactly the raster."""
    rects = []
    for col, row, width, height in _run_rects(raster):
        x, y = col * cell, row * cell
        rects.append([_num(x), _num(y), _num(min(width * cell, world[0] - x)), _num(min(height * cell, world[1] - y))])
    return rects


def rect_coverage(rects: Sequence[Sequence[float]], shape: tuple[int, int], cell: float) -> np.ndarray:
    """How many rectangles cover each raster cell centre (the QA re-rasterisation)."""
    cover = np.zeros(shape, np.int32)
    centres_x = (np.arange(shape[1]) + 0.5) * cell
    centres_y = (np.arange(shape[0]) + 0.5) * cell
    for x, y, w, h in rects:
        cols = (centres_x >= x) & (centres_x <= x + w)
        rows = (centres_y >= y) & (centres_y <= y + h)
        cover[np.ix_(rows, cols)] += 1
    return cover


# ----------------------------------------------------------------------------- navigation

@dataclass
class Navigation:
    cell: int
    lattice_valid: np.ndarray
    labels: np.ndarray
    primary: int  # component label of the first spawn (0 when it is blocked)
    origin: str

    @property
    def cells_valid(self) -> np.ndarray:
        return self.lattice_valid[0::2, 0::2]

    @property
    def cells_reachable(self) -> np.ndarray:
        return (self.labels[0::2, 0::2] == self.primary) & (self.primary > 0)

    def centres(self) -> tuple[np.ndarray, np.ndarray]:
        rows, cols = self.cells_valid.shape
        return (np.arange(cols) + 0.5) * self.cell, (np.arange(rows) + 0.5) * self.cell


class Walker:
    """Plan Appendix C validity for one actor footprint ellipse (rx = r, ry = r * ySquash)."""

    def __init__(self, solids: SolidSet, world: tuple[int, int], radius: float, squash: float):
        self.solids, self.world, self.radius, self.squash = solids, world, radius, squash
        self.offsets = [(0.0, 0.0)] + [(radius * math.cos(a), radius * squash * math.sin(a)) for a in SAMPLE_ANGLES]
        self.cell = max(1, _round_half_up(radius / 2.0))

    def valid(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        xs, ys = np.asarray(xs, np.float64), np.asarray(ys, np.float64)
        ok = np.ones(np.broadcast(xs, ys).shape, bool)
        for dx, dy in self.offsets:
            sx, sy = xs + dx, ys + dy
            ok &= (sx >= 0) & (sx <= self.world[0]) & (sy >= 0) & (sy <= self.world[1])
            ok &= ~self.solids.any_contains(sx, sy)
        return ok

    def segment_clear(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        steps = max(1, int(math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) / (self.cell / 2.0))))
        t = np.linspace(0.0, 1.0, steps + 1)
        return bool(self.valid(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t).all())

    def lattice(self) -> np.ndarray:
        """Validity on the half-cell lattice: (even, even) are cell centres, mixed parity the
        midpoints of 4-neighbour moves, (odd, odd) are never valid (no diagonal moves)."""
        cell, half = self.cell, self.cell / 2.0
        columns = max(1, int(math.ceil(self.world[0] / cell - 0.5)))
        rows = max(1, int(math.ceil(self.world[1] / cell - 0.5)))
        xs = (np.arange(2 * columns - 1) + 1) * half
        ys = (np.arange(2 * rows - 1) + 1) * half
        valid = np.ones((len(ys), len(xs)), bool)
        reach_x, reach_y = self.radius, self.radius * self.squash
        grid_x, grid_y = np.meshgrid(xs, ys)
        for dx, dy in self.offsets:
            sx, sy = grid_x + dx, grid_y + dy
            valid &= (sx >= 0) & (sx <= self.world[0]) & (sy >= 0) & (sy <= self.world[1])
        for solid, (x0, y0, x1, y1) in zip(self.solids.solids, self.solids.boxes):
            a0 = max(0, int(math.floor((x0 - reach_x) / half)) - 1)
            a1 = min(len(xs), int(math.ceil((x1 + reach_x) / half)) + 1)
            b0 = max(0, int(math.floor((y0 - reach_y) / half)) - 1)
            b1 = min(len(ys), int(math.ceil((y1 + reach_y) / half)) + 1)
            if a0 >= a1 or b0 >= b1:
                continue
            wx, wy = np.meshgrid(xs[a0:a1], ys[b0:b1])
            hit = np.zeros(wx.shape, bool)
            for dx, dy in self.offsets:
                hit |= SolidSet.contains(solid, wx + dx, wy + dy)
            valid[b0:b1, a0:a1] &= ~hit
        valid[1::2, 1::2] = False
        return valid


def _rect_distance(xs: np.ndarray, ys: np.ndarray, rect: Sequence[float]) -> np.ndarray:
    x, y, w, h = rect
    return np.hypot(np.maximum.reduce([x - xs, np.zeros_like(xs), xs - (x + w)]),
                    np.maximum.reduce([y - ys, np.zeros_like(ys), ys - (y + h)]))


def entry_cell(walker: Walker, nav: Navigation, point: tuple[float, float]) -> tuple[int, int] | None:
    """The nearest valid cell centre within 1.5 cells of a valid point, joined by a clear segment."""
    if not bool(walker.valid(np.array([point[0]]), np.array([point[1]]))[0]):
        return None
    cell = nav.cell
    valid = nav.cells_valid
    col0, row0 = int(point[0] // cell), int(point[1] // cell)
    candidates = []
    for row in range(row0 - 2, row0 + 3):
        for col in range(col0 - 2, col0 + 3):
            if 0 <= row < valid.shape[0] and 0 <= col < valid.shape[1] and valid[row, col]:
                cx, cy = (col + 0.5) * cell, (row + 0.5) * cell
                distance = math.hypot(cx - point[0], cy - point[1])
                if distance <= 1.5 * cell:
                    candidates.append((distance, row, col, cx, cy))
    for _, row, col, cx, cy in sorted(candidates):
        if walker.segment_clear(point, (cx, cy)):
            return row, col
    return None


def navigate(layout: Layout, walker: Walker, exits: list[dict], spawns: list[dict],
             interactions: Sequence[dict]) -> tuple[Navigation, dict]:
    """Reachability per plan Appendix C; places one arrival spawn per exit and reports every target."""
    lattice = walker.lattice()
    labels, _ = forge_core.label_components(lattice, connectivity=4)
    nav = Navigation(walker.cell, lattice, labels, 0, "")
    centres_x, centres_y = nav.centres()
    grid_x, grid_y = np.meshgrid(centres_x, centres_y)
    cell_labels = labels[0::2, 0::2]
    report: dict[str, Any] = {"cell": walker.cell, "spawn_problems": [], "unreachable_exits": [], "no_arrival": [],
                              "unreachable_interactions": [], "pockets": []}

    def arrival_for(entry: dict, component: int | None) -> dict | None:
        x, y, w, h = entry["rect"]
        dx, dy = entry["direction"]
        mid_x, mid_y = x + w / 2.0, y + h / 2.0
        reach = entry["depth"] + entry["radius"] + 1.5 * walker.cell
        ideal_x, ideal_y = mid_x - dx * reach, mid_y - dy * reach
        if dx:
            ideal_x = (x + w if dx < 0 else x) - dx * (entry["radius"] + 1.5 * walker.cell)
        if dy:
            ideal_y = (y + h if dy < 0 else y) - dy * (entry["radius"] + 1.5 * walker.cell)
        distance = _rect_distance(grid_x, grid_y, entry["rect"])
        usable = (nav.cells_valid & (distance > entry["radius"] + 1e-6)
                  & (distance <= entry["radius"] + 4.0 * walker.cell))
        if component is not None:
            usable &= cell_labels == component
        lo, hi = entry["span_tiles"][0] * layout.tile, entry["span_tiles"][1] * layout.tile
        along = grid_y if dx else grid_x
        usable &= (along >= lo) & (along <= hi)
        if not usable.any():
            return None
        score = np.where(usable, np.hypot(grid_x - ideal_x, grid_y - ideal_y), np.inf)
        row, col = np.unravel_index(int(np.argmin(score)), score.shape)
        return {"id": entry["arrival"], "x": _num(centres_x[col]), "y": _num(centres_y[row]),
                "facing": OPPOSITE_FACING[entry["edge"]], "cell": (int(row), int(col))}

    if spawns:
        first = entry_cell(walker, nav, (spawns[0]["x"], spawns[0]["y"]))
        if first is None:
            report["spawn_problems"].append(spawns[0]["id"])
        else:
            nav.primary = int(cell_labels[first])
        nav.origin = spawns[0]["id"]
    else:
        arrival = arrival_for(exits[0], None)
        if arrival is not None:
            nav.primary = int(cell_labels[arrival["cell"]])
        nav.origin = exits[0]["arrival"]
    for spawn in spawns[1:]:
        cell = entry_cell(walker, nav, (spawn["x"], spawn["y"]))
        if cell is None or int(cell_labels[cell]) != nav.primary or nav.primary == 0:
            report["spawn_problems"].append(spawn["id"])
    reachable = nav.cells_reachable
    for entry in exits:
        near = _rect_distance(grid_x, grid_y, entry["rect"]) <= entry["radius"] + 1e-9
        if not (near & reachable).any():
            report["unreachable_exits"].append(entry["id"])
        arrival = arrival_for(entry, nav.primary if nav.primary else -1)
        if arrival is None:
            report["no_arrival"].append(entry["id"])
            entry["arrival_spawn"] = None
        else:
            entry["arrival_spawn"] = arrival
    for item in interactions:
        near = np.hypot(grid_x - item["x"], grid_y - item["y"]) <= item["reach"]
        if not (near & reachable).any():
            report["unreachable_interactions"].append(item["id"])
    counts = np.bincount(cell_labels[nav.cells_valid].ravel(), minlength=labels.max() + 1)
    for label in np.flatnonzero(counts >= POCKET_MIN_CELLS).tolist():
        if label == 0 or label == nav.primary:
            continue
        rows, cols = np.nonzero(cell_labels == label)
        report["pockets"].append({"cells": int(counts[label]),
                                  "bbox_px": [_num(cols.min() * walker.cell), _num(rows.min() * walker.cell),
                                              _num((cols.max() + 1) * walker.cell),
                                              _num((rows.max() + 1) * walker.cell)]})
    valid_count = int(nav.cells_valid.sum())
    report["valid_cells"] = valid_count
    report["reachable_cells"] = int(reachable.sum())
    report["reachable_fraction"] = round(int(reachable.sum()) / valid_count, 4) if valid_count else 0.0
    return nav, report


# ----------------------------------------------------------------------------- rendering

def _over(base: np.ndarray, top: np.ndarray) -> np.ndarray:
    """Straight-alpha 'over' of uint8 RGBA arrays (exact where `top` is opaque or clear), in row bands."""
    out = np.empty_like(base)
    for start in range(0, base.shape[0], 256):
        base_f = base[start:start + 256].astype(np.float64)
        top_f = top[start:start + 256].astype(np.float64)
        alpha_t, alpha_b = top_f[..., 3:] / 255.0, base_f[..., 3:] / 255.0
        alpha = alpha_t + alpha_b * (1.0 - alpha_t)
        colour = top_f[..., :3] * alpha_t + base_f[..., :3] * alpha_b * (1.0 - alpha_t)
        band = np.zeros_like(base_f)
        np.divide(colour, alpha, out=band[..., :3], where=alpha > 0)
        band[..., 3:] = alpha * 255.0
        out[start:start + 256] = np.floor(band + 0.5).clip(0, 255).astype(np.uint8)
    return out


def render_ground(layout: Layout, grid: np.ndarray, tilesets: Sequence[Tileset],
                  layers: Sequence[np.ndarray]) -> np.ndarray:
    """The ground image: tiles gathered per cell (each cell belongs to exactly one tileset layer), or
    flat material colours from the vertex grid when there is no tileset."""
    world_w, world_h = layout.world
    if not tilesets:
        colours = np.array([material.colour for material in layout.materials], np.uint8)
        return colours[pixel_materials(grid, layout.tile, layout.world)]
    canvas = np.zeros((world_h, world_w, 4), np.uint8)
    tile = layout.tile
    for tileset, indices in zip(tilesets, layers):
        if not (indices >= 0).any():
            continue
        image = tileset.tiles[np.clip(indices, 0, None)].transpose(0, 2, 1, 3, 4).reshape(world_h, world_w, 4)
        mask = np.repeat(np.repeat(indices >= 0, tile, axis=0), tile, axis=1)
        canvas[mask] = image[mask]
    return canvas


def sprite_pixels(instance: Instance) -> np.ndarray:
    image = instance.prop.image
    if instance.flip:
        image = image[:, ::-1]
    left, top, width, height = instance.sprite_rect()
    if (width, height) != (image.shape[1], image.shape[0]):
        resized = Image.fromarray(np.ascontiguousarray(image)).resize((width, height), Image.Resampling.NEAREST)
        image = np.asarray(resized)
    return np.ascontiguousarray(image)


def render_preview(ground: np.ndarray, instances: Sequence[Instance]) -> np.ndarray:
    canvas = Image.fromarray(ground.copy())
    for instance in sorted(instances, key=lambda item: (item.y, item.x, item.id)):
        pixels = sprite_pixels(instance)
        left, top, width, height = instance.sprite_rect()
        x0, y0 = max(0, left), max(0, top)
        x1, y1 = min(canvas.width, left + width), min(canvas.height, top + height)
        if x0 >= x1 or y0 >= y1:
            continue
        crop = Image.fromarray(pixels[y0 - top:y1 - top, x0 - left:x1 - left])
        canvas.alpha_composite(crop, (x0, y0))
    return np.asarray(canvas).copy()


def render_debug(preview: np.ndarray, layout: Layout, nav: Navigation, solids: Sequence[dict],
                 rects: Sequence[Sequence[float]], exits: Sequence[dict], spawns: Sequence[dict],
                 interactions: Sequence[dict], roads: Sequence[dict], scale: int) -> np.ndarray:
    """Preview upscaled by `scale` with navigation and collision drawn over it."""
    world_w, world_h = layout.world
    base = codeart_core.upscale_nearest(preview, scale)
    tint = np.zeros((world_h, world_w, 4), np.uint8)
    cell_state = np.zeros(nav.cells_valid.shape, np.uint8)
    cell_state[~nav.cells_valid] = 1
    cell_state[nav.cells_valid & ~nav.cells_reachable] = 2
    states = np.repeat(np.repeat(cell_state, nav.cell, axis=0), nav.cell, axis=1)
    states = np.pad(states, ((0, max(0, world_h - states.shape[0])), (0, max(0, world_w - states.shape[1]))),
                    constant_values=1)[:world_h, :world_w]
    tint[states == 1] = (16, 16, 32, 96)
    tint[states == 2] = (255, 0, 200, 120)
    image = Image.fromarray(_over(base, codeart_core.upscale_nearest(tint, scale)))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    k = scale

    def box(x0: float, y0: float, x1: float, y1: float) -> list[float]:
        return [x0 * k, y0 * k, max(x0 * k, x1 * k - 1), max(y0 * k, y1 * k - 1)]

    for x, y, w, h in rects:
        draw.rectangle(box(x, y, x + w, y + h), fill=(255, 60, 40, 48), outline=(255, 60, 40, 150))
    for solid in solids:
        colour = (60, 140, 255, 230) if str(solid.get("id", "")).startswith("terrain-") else (255, 230, 40, 230)
        if solid["shape"] == "rect":
            draw.rectangle(box(solid["x"], solid["y"], solid["x"] + solid["w"], solid["y"] + solid["h"]),
                           outline=colour)
        elif solid["shape"] == "ellipse" and not solid.get("rotate"):
            draw.ellipse(box(solid["cx"] - solid["rx"], solid["cy"] - solid["ry"], solid["cx"] + solid["rx"],
                             solid["cy"] + solid["ry"]), outline=colour)
        else:
            points = solid.get("points") or [
                (solid["cx"] + solid["rx"] * math.cos(t) * math.cos(math.radians(solid["rotate"]))
                 - solid["ry"] * math.sin(t) * math.sin(math.radians(solid["rotate"])),
                 solid["cy"] + solid["rx"] * math.cos(t) * math.sin(math.radians(solid["rotate"]))
                 + solid["ry"] * math.sin(t) * math.cos(math.radians(solid["rotate"])))
                for t in np.linspace(0, 2 * math.pi, 24, endpoint=False)]
            draw.polygon([(px * k, py * k) for px, py in points], outline=colour)
    for road in roads:
        line = [(float(px) * layout.tile * k, float(py) * layout.tile * k) for px, py in road["line"]]
        if len(line) > 1:
            draw.line(line, fill=(255, 150, 30, 220), width=max(1, k // 2))
    for entry in exits:
        x, y, w, h = entry["rect"]
        r = entry["radius"]
        draw.rectangle(box(x - r, y - r, x + w + r, y + h + r), outline=(0, 200, 220, 140))
        draw.rectangle(box(x, y, x + w, y + h), outline=(0, 255, 255, 255), width=2)
        draw.text((min(x * k, image.width - 60), min(y * k, image.height - 12)), entry["id"], fill=(0, 255, 255, 255),
                  font=font)
    for item in interactions:
        cx, cy, reach = item["x"] * k, item["y"] * k, item["reach"] * k
        draw.ellipse([cx - reach, cy - reach, cx + reach, cy + reach], outline=(255, 240, 0, 220))
        draw.text((cx + 3, cy - 12), item["id"], fill=(255, 240, 0, 255), font=font)
    for spawn in spawns:
        cx, cy = spawn["x"] * k, spawn["y"] * k
        draw.ellipse([cx - 3 * k / 2, cy - 3 * k / 2, cx + 3 * k / 2, cy + 3 * k / 2], fill=(255, 255, 255, 255),
                     outline=(0, 0, 0, 255))
        draw.text((cx + 4, cy + 2), spawn["id"], fill=(255, 255, 255, 255), font=font)
    image.alpha_composite(overlay)
    return np.asarray(image).copy()


# ----------------------------------------------------------------------------- QA and output

def _check(identity: str, status: str, value: Any, threshold: Any) -> dict:
    return {"id": identity, "status": status, "value": value, "threshold": threshold}


def _status(checks: Sequence[dict]) -> str:
    states = {check["status"] for check in checks}
    return "fail" if "fail" in states else "warn" if "warn" in states else "pass"


def prop_item(kind: PropKind, variant: PropVariant, image_path: str, sha256: str) -> dict:
    """A propItem (map.schema.json) describing one prop image of the bundle."""
    footprint = dict(kind.footprint)
    item = {"label": variant.prop_id, "display_name": kind.name, "image": image_path, "sha256": sha256,
            "anchor_px": [_num(variant.anchor[0]), _num(variant.anchor[1])],
            "footprint": {key: ([_num(v) for v in value] if isinstance(value, list) else
                                _num(value) if isinstance(value, float) else value)
                          for key, value in footprint.items()},
            "solid": kind.solid, "occlusion_class": kind.occlusion,
            "status": "placeholder" if variant.origin == "placeholder" else "accepted",
            "size": [int(variant.image.shape[1]), int(variant.image.shape[0])], "origin": variant.origin}
    if footprint["shape"] == "none":
        item["footprint"] = {"shape": "none"}
    return item


def _local_write_codeart_meta(path: Path, *, placeholder: bool, **kwargs: Any) -> dict:
    """codeart_core.write_codeart_meta that records `placeholder` honestly.

    The A3 writer always writes placeholder false and refuses it as an extra field, so a
    placeholder meta is written through the library to a scratch name (all its checks
    run) and then stored under `path` with placeholder true. Remove once the writer
    takes a placeholder keyword (handoff section 6)."""
    if not placeholder:
        return codeart_core.write_codeart_meta(path, **kwargs)
    scratch = path.with_name(f".{path.name}.partial")
    try:
        meta = codeart_core.write_codeart_meta(scratch, **kwargs)
    finally:
        scratch.unlink(missing_ok=True)
    meta["placeholder"] = True
    forge_core.write_json(path, meta)
    return meta


def build(args: argparse.Namespace) -> dict:
    spec_path = Path(args.spec)
    layout = load_layout(spec_path, args.seed)
    tilesets = load_tilesets([Path(path) for path in args.tiles or []], layout)
    world = layout.world
    rect_cell = float(args.rect_cell) if args.rect_cell else \
        (layout.tile / 2.0 if layout.tile % 2 == 0 else float(layout.tile))
    if rect_cell <= 0:
        raise LayoutError("--rect-cell must be a positive number of pixels")

    grid, roads = paint_vertices(layout)
    hygiene = run_hygiene(grid, allowed_keys(tilesets), layout)
    layers, missing_tiles = autotile(grid, tilesets, layout) if tilesets else ([], 0)
    materials_px = pixel_materials(grid, layout.tile, world)
    exit_geometry(layout.exits, grid, roads, layout)

    fixed = [Instance(entry["id"], entry["kind"], entry["variant"], entry["x"], entry["y"], entry["flip"],
                      entry["scale"]) for entry in layout.objects]
    object_interactions = []
    for instance in fixed:
        for item in instance.kind.interactions:
            sign = -1.0 if instance.flip else 1.0
            object_interactions.append({
                "id": f"{instance.id}.{item['name']}",
                "x": instance.x + sign * item["offset"][0] * instance.scale,
                "y": instance.y + item["offset"][1] * instance.scale,
                "reach": item["reach"] if item["reach"] is not None else layout.actor_radius + layout.tile / 2.0})
    interactions = layout.interactions + object_interactions
    road_materials = {layout.material(road["material"], "road").index for road in roads}
    points = [(spawn["x"], spawn["y"]) for spawn in layout.spawns] + [(item["x"], item["y"]) for item in interactions]
    interaction_ids = [item["id"] for item in interactions]
    if len(set(interaction_ids)) != len(interaction_ids):
        raise LayoutError("interaction ids collide (object interactions are named '<object id>.<name>')")
    fields = build_fields(layout, materials_px, road_materials, fixed, layout.exits, points)
    scattered, scatter_report = scatter_props(layout, fields)
    instances = fixed + scattered

    solids = terrain_solids(grid, layout)
    solids += [solid for solid in (object_solid(instance) for instance in instances) if solid is not None]
    solid_set = SolidSet(solids)
    raster = blocked_raster(solid_set, world, rect_cell)
    rects = raster_rects(raster, world, rect_cell)
    coverage = rect_coverage(rects, raster.shape, rect_cell)
    walker = Walker(solid_set, world, layout.actor_radius, layout.y_squash)
    nav, nav_report = navigate(layout, walker, layout.exits, layout.spawns, interactions)

    spawns_out = [{"id": spawn["id"], "x": _num(spawn["x"]), "y": _num(spawn["y"]), "facing": spawn["facing"]}
                  for spawn in layout.spawns]
    portals = []
    for entry in layout.exits:
        arrival = entry.get("arrival_spawn")
        target_map = entry["to"].split(":", 1)[0]
        portal = {"id": entry["id"], "rect": [_num(v) for v in entry["rect"]], "to": entry["to"],
                  "activation": "intent", "travelDirection": [_num(v) for v in entry["direction"]],
                  "radius": _num(entry["radius"]), "latch": True, "requiresMovement": True, "edge": entry["edge"]}
        if arrival is not None:
            portal["entranceByFrom"] = {target_map: arrival["id"]}
            spawns_out.append({key: arrival[key] for key in ("id", "x", "y", "facing")})
        portals.append(portal)

    variety = [item["same_look_neighbour_share"] for item in scatter_report
               if item["looks"] > 1 and item["same_look_neighbour_share"] is not None]
    short = [item["id"] for item in scatter_report if item["placed"] < item["requested"]]
    checks = [
        _check("tiles_drawable", ("pass" if missing_tiles == 0 else "fail") if tilesets else "skipped",
               missing_tiles, 0),
        _check("rect_union_equals_blocked", "pass" if np.array_equal(coverage > 0, raster) else "fail",
               int(np.count_nonzero((coverage > 0) != raster)), 0),
        _check("rects_disjoint", "pass" if int((coverage > 1).sum()) == 0 else "fail", int((coverage > 1).sum()), 0),
        _check("spawns_reachable", "pass" if not nav_report["spawn_problems"] else "fail",
               nav_report["spawn_problems"], []),
        _check("exits_reachable", "pass" if not nav_report["unreachable_exits"] else "fail",
               nav_report["unreachable_exits"], []),
        _check("arrivals_outside_triggers", "pass" if not nav_report["no_arrival"] else "fail",
               nav_report["no_arrival"], []),
        _check("interactions_reachable", "pass" if not nav_report["unreachable_interactions"] else "fail",
               nav_report["unreachable_interactions"], []),
        _check("enclosed_pockets", "pass" if not nav_report["pockets"] else "warn", len(nav_report["pockets"]), 0),
        _check("prop_variety", "skipped" if not variety else ("pass" if max(variety) <= VARIETY_WARN_SHARE else "warn"),
               max(variety) if variety else None, VARIETY_WARN_SHARE),
        _check("scatter_filled", "pass" if not short else "warn", short, []),
        _check("hygiene_converged", "pass" if hygiene["converged"] else "warn", hygiene["passes"], HYGIENE_MAX_PASSES),
    ]
    status = _status(checks)
    if args.strict_qc and status == "fail":
        failed = "; ".join(f"{check['id']}={check['value']}" for check in checks if check["status"] == "fail")
        raise LayoutQAError(f"layout QA failed ({failed}); nothing was published")

    with forge_core.staged_output(Path(args.output_dir)) as stage:
        outputs: list[Path] = []
        vertex_doc = {"schema": VERTEX_GRID_SCHEMA, "size": [layout.width + 1, layout.height + 1],
                      "tile_size": layout.tile, "materials": [material.name for material in layout.materials],
                      "base": layout.base.name, "data": grid.astype(int).tolist()}
        forge_core.write_json(stage / "terrain-vertices.json", vertex_doc)
        outputs.append(stage / "terrain-vertices.json")

        tileset_entries = []
        for tileset in tilesets:
            folder = stage / "tilesets" / tileset.id
            folder.mkdir(parents=True)
            image_name = tileset.image_path.name
            forge_core.publish_file_no_replace(tileset.image_path, folder / image_name)
            manifest = dict(tileset.manifest)
            manifest["image"] = image_name
            forge_core.write_json(folder / tileset.manifest_path.name, manifest)
            outputs += [folder / image_name, folder / tileset.manifest_path.name]
            tileset_entries.append({"id": tileset.id, "manifest": f"tilesets/{tileset.id}/{tileset.manifest_path.name}",
                                    "sha256": forge_core.sha256_file(folder / tileset.manifest_path.name)})

        props_out = {}
        (stage / "props").mkdir()
        for kind in layout.kinds.values():
            for variant in kind.variants:
                path = stage / "props" / f"{variant.prop_id}.png"
                codeart_core.save_png(variant.image, path)
                outputs.append(path)
                props_out[variant.prop_id] = prop_item(kind, variant, f"props/{variant.prop_id}.png",
                                                       forge_core.sha256_file(path))

        ground = render_ground(layout, grid, tilesets, layers)
        bundle_layers: list[dict] = []
        if tilesets:
            for tileset, indices in zip(tilesets, layers):
                if (indices >= 0).any():
                    name = "ground" if not bundle_layers else f"ground-{tileset.id}"
                    bundle_layers.append({"name": name, "kind": "tiles", "tileset": tileset.id,
                                          "data": indices.astype(int).tolist()})
        else:
            codeart_core.save_png(ground, stage / "ground.png")
            outputs.append(stage / "ground.png")
            bundle_layers.append({"name": "ground", "kind": "image", "image": "ground.png",
                                  "sha256": forge_core.sha256_file(stage / "ground.png")})
        bundle_layers.append({"name": "props", "kind": "objects"})

        if args.preview:
            preview = render_preview(ground, instances)
            codeart_core.save_png(preview, stage / "preview.png")
            scale = args.debug_scale or (2 if max(world) <= 1024 else 1)
            debug = render_debug(preview, layout, nav, solids, rects, layout.exits, spawns_out, interactions, roads,
                                 scale)
            codeart_core.save_png(debug, stage / "debug.png")
            outputs += [stage / "preview.png", stage / "debug.png"]

        objects = []
        for instance in instances:
            item = {"id": instance.id, "prop": instance.prop.prop_id, "kind": instance.kind.name,
                    "x": _num(instance.x), "y": _num(instance.y),
                    "anchor_px": [_num(instance.prop.anchor[0]), _num(instance.prop.anchor[1])],
                    "solid": instance.kind.solid, "sortY": _num(instance.y), "occlusion": instance.kind.occlusion,
                    "scale": _num(instance.scale), "flip_x": instance.flip}
            footprint = instance.footprint()
            if footprint["shape"] != "none":
                item["footprint"] = {"shape": footprint["shape"], "width": _num(footprint["width"]),
                                     "depth": _num(footprint["depth"]),
                                     "offset": [_num(v) for v in footprint["offset"]],
                                     "rotate": _num(footprint["rotate"])}
            if instance.group:
                item["group"] = instance.group
            objects.append(item)

        placeholder = not tilesets or any(kind.placeholder for kind in layout.kinds.values())
        external_art = bool(tilesets) or any(variant.origin in ("image", "pack")
                                             for kind in layout.kinds.values() for variant in kind.variants)
        bundle = {
            "schema": BUNDLE_SCHEMA, "id": layout.map_id, "tile_size": layout.tile,
            "world": {"width": world[0], "height": world[1], "unit": "px"},
            "terrain": {"vertex_grid": "terrain-vertices.json",
                        "materials": [material.name for material in layout.materials]},
        }
        if tileset_entries:
            bundle["tilesets"] = tileset_entries
        bundle.update({
            "layers": bundle_layers, "props": props_out, "objects": objects,
            "collision": {"actorRadius": _num(layout.actor_radius), "ySquash": _num(layout.y_squash),
                          "solids": solids, "rects": rects, "rectCell": _num(rect_cell)},
            "portals": portals, "spawns": spawns_out,
            "interactions": [{"id": item["id"], "x": _num(item["x"]), "y": _num(item["y"]),
                              "reach": _num(item["reach"])} for item in interactions],
            "roads": [{"id": road["id"], "material": road["material"],
                       "width": _num(road["width"] * layout.tile),
                       "polyline": [[_num(x * layout.tile), _num(y * layout.tile)] for x, y in road["line"]]}
                      for road in roads],
            "camera": {"bounds": [0, 0, world[0], world[1]]},
            "art_source": "mixed" if external_art else "code", "placeholder": placeholder,
            "qa": {"status": status, "report": "layout-qa.json",
                   "checks": {check["id"]: check["status"] for check in checks},
                   "reachability": {key: nav_report[key] for key in ("cell", "valid_cells", "reachable_cells",
                                                                     "reachable_fraction")}},
        })
        inputs = [layout.spec_path.resolve()] + list(dict.fromkeys(path.resolve() for path in layout.inputs))
        bundle["provenance"] = {
            "tool": TOOL, "version": TOOL_VERSION,
            "params": {"seed": layout.seed, "rect_cell": _num(rect_cell), "preview": bool(args.preview),
                       "strict_qc": bool(args.strict_qc), "hygiene": hygiene,
                       "scatter": scatter_report, "navigation": nav_report},
            "inputs": [_file_ref(path, stage) for path in inputs],
        }
        forge_core.write_json(stage / "map-bundle.json", bundle)
        outputs.insert(0, stage / "map-bundle.json")

        envelope = {
            "status": status,
            "method": (f"{TOOL}: vertex-grid painting and hygiene, corner-Wang tile lookup, seeded scatter; "
                       f"collision rectangles re-rasterised on the {_num(rect_cell)} px blocked raster; "
                       f"plan Appendix C footprint validity (r={_num(layout.actor_radius)}, "
                       f"ySquash={_num(layout.y_squash)}) at {walker.cell} px cell centres and 4-neighbour move "
                       "midpoints, components labelled from the first spawn"),
            "notProven": [
                "How the map looks: open preview.png and debug.png (magenta = walkable but unreachable, dark = "
                "too tight for the actor).",
                f"Engines that collide with collision.rects only get the {_num(rect_cell)} px cell approximation of "
                "prop footprints; the solids are exact.",
                "Reachability is proven for one actor size; larger actors, jumping, doors that open and moving "
                "objects are not simulated.",
                "Tile seams: layout_build trusts the tileset manifest's seam proof.",
            ],
            "checks": checks,
            "inputs": [_file_ref(path, stage) for path in inputs],
            "outputs": [_file_ref(path, stage) for path in outputs],
            "tool": {"name": TOOL, "version": TOOL_VERSION},
        }
        forge_core.write_json(stage / "layout-qa.json", envelope)
        palette = {f"material-{material.name}": _hex(material.colour) for material in layout.materials}
        _local_write_codeart_meta(
            stage / "codeart-meta.json", placeholder=placeholder, generator=TOOL,
            spec_sha256=forge_core.sha256_bytes(layout.spec_bytes),
            renderer={"name": TOOL, "version": TOOL_VERSION}, palette=palette, outputs=outputs, qa=envelope)
    final = Path(args.output_dir).resolve()
    return {"status": status, "output": final.as_posix(), "bundle": (final / "map-bundle.json").as_posix(),
            "metadata": (final / "codeart-meta.json").as_posix(), "qa": (final / "layout-qa.json").as_posix(),
            "objects": len(objects), "solids": len(solids), "rects": len(rects), "portals": len(portals),
            "reachable_fraction": nav_report["reachable_fraction"]}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a playable top-down map (map_bundle.v2) from a codeart2d layout spec: vertex-grid "
                    "terrain, spline roads, corner-Wang tiles, seeded prop scatter, exact collision rectangles, "
                    "exits with arrival spawns and a reachability gate. No image model is used.",
        epilog="Example: python layout_build.py --spec meadow-layout.json --output-dir out/map-v1 --seed 7 "
               "--preview --strict-qc")
    parser.add_argument("--spec", required=True, help="codeart2d.layout_spec.v1 JSON file")
    parser.add_argument("--output-dir", required=True, help="new folder to create; an existing path is refused")
    parser.add_argument("--tiles", action="append", metavar="MANIFEST",
                        help="tileset_v1 manifest (wang_corner or flat); repeat for several sets. Without it the "
                             "ground is a flat-colour placeholder image")
    parser.add_argument("--seed", type=int, help="override the spec seed (scatter, wobble and tile variants)")
    parser.add_argument("--preview", action="store_true", help="also write preview.png and debug.png")
    parser.add_argument("--strict-qc", action="store_true",
                        help="exit 1 and publish nothing when a QA check fails (unreachable exit, spawn or "
                             "interaction, missing tile, inexact rectangles)")
    parser.add_argument("--rect-cell", type=float,
                        help="cell size in px of the blocked raster behind collision.rects (default: half a tile)")
    parser.add_argument("--debug-scale", type=int, choices=(1, 2, 3, 4),
                        help="integer upscale of debug.png (default 2, or 1 for maps wider than 1024 px)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = parse_args(argv)
    if args.seed is not None and args.seed < 0:
        print("error: --seed must be 0 or greater", file=sys.stderr)
        return 1
    if os.path.lexists(args.output_dir):
        print(f"error: output directory already exists: {forge_core.ascii_text(str(args.output_dir))}",
              file=sys.stderr)
        return 1
    try:
        summary = build(args)
    except (LayoutError, LayoutQAError, codeart_core.CodeArtError, OSError, ValueError) as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
