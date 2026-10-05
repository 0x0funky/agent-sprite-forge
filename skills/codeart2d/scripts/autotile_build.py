#!/usr/bin/env python3
"""Build seam-proven autotile sets from a codeart2d material spec (code-drawn, no image model).

Kinds (one tileset per `sets` entry of the spec):
  wang_corner  2 materials -> Wang-16 corner set; 3 materials -> the 81-tile
               three-material corner set (a road can meet water directly)
  blob47       [under, fill] -> 47-tile blob overlay with edge bands
  bevel        [solid] -> 16 full-tile blocks with bevel faces on open sides
  flat         [material] -> plain fill variants

Each set is one rule evaluated on map coordinates, so tiles placed by their
topology key meet exactly:
* Wang: each vertex of the map grid holds a material. A pixel takes the material
  with the largest corner weight K(dx) * K(dy) * (1 + wobble). K is 1 within
  `plateau` px of its vertex, so everything within `plateau` px of a tile edge
  depends only on that edge's two corners, and band look-ups (foam lines, rims,
  the north-shore bank and shadow) that reach at most `plateau` px across an
  edge are exact: a tile computes them from its own corners. (The 2026-10-05 map
  probe sampled above the tile and clamped at its top row, which left a 4 px
  colour jump between water tiles; the plateau removes that by construction.)
* Blob: inside masks with tile-periodic margins, inner-corner notches made of
  the two neighbouring margin bands, and edge bands read from a ring built with
  the neighbours' own rules.
* Bevel: face shading from the 4-neighbour occupancy.
All procedural noise (boundary wobble, texture bands, blob margins) is a sum of
sines with whole cycles per tile, evaluated at map coordinates with exact
integer phase arithmetic. Variants change only tile interiors: texture marks
never touch the outer ring.

QA (autotile-qa.json; the same envelope is the qa of codeart-meta.json):
  seam proof   every 2x2 block of Wang tiles (all 3x3 corner lattices), every 3x3
               neighbourhood and adjacent pair of blob or bevel tiles, every 2x2
               variant block of flat tiles, in variant rotations: tiles
               assembled from the atlas against an independent global render
               (noise at global coordinates, look-ups at their true positions
               across tile edges), pixel for pixel; plus a seeded random 30x20 map
  control      a copy of the set whose noise is detuned by half a cycle per tile
               (it no longer wraps) must fail that comparison
  seam metric  material flips and colour steps across tile seams versus inside
               tiles (catches image textures that do not wrap)
  repetition   luminance correlation of a 13x13 single-material area with itself
               shifted by one tile, variants picked uniformly (target <= 0.35)

Usage, from the project root (single line):
  python "<skill-dir>/scripts/autotile_build.py" --material-spec water-grass.material.json --kind both --tile-size 16 --variants 4 --output-dir out/tiles-v1 --preview-map 24x16 --max-repetition 0.35 --strict-qc
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import errno
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import numpy as np
    import forge_core
    import codeart_core
except ImportError as missing:  # numpy or Pillow is not installed
    sys.stderr.write("error: autotile_build needs numpy and Pillow; install with: python -m pip install -r "
                     "requirements.txt (" + str(missing).encode("ascii", "replace").decode("ascii") + ")\n")
    raise SystemExit(1)


TOOL_NAME = "codeart2d/autotile_build"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # D29: the package version in every QA envelope
SPEC_SCHEMA = "codeart2d.material_spec.v1"
TILESET_SCHEMA = "generate2dmap.tileset.v1"
KINDS = ("wang_corner", "blob47", "bevel", "flat")
KIND_SELECTIONS = {"all": KINDS, "both": ("wang_corner", "blob47"), "wang_corner": ("wang_corner",),
                   "wang": ("wang_corner",), "blob47": ("blob47",), "blob": ("blob47",), "bevel": ("bevel",),
                   "flat": ("flat",)}
SET_MATERIALS = {"wang_corner": (2, 3), "blob47": (2, 2), "bevel": (1, 1), "flat": (1, 1)}
TILE_RANGE = (8, 64)
MAX_VARIANTS = 16
MAX_RAMP = 16
MAX_RULE_COLOURS = 8
DEFAULT_VARIANTS = 4
DEFAULT_WOBBLE = 0.6
REPETITION_TARGET = 0.35
REPETITION_AREA = 13
REPETITION_SAMPLES = 8
PROOF_MAP = (30, 20)
DEFAULT_PREVIEW = (24, 16)
DEFAULT_SEAM_RATIO = 1.0
TEXTURE_WRAP_LIMIT = 1.25
EXHAUSTIVE_PIXEL_LIMIT = 400_000_000
CHUNK_PIXELS = 1_500_000
QA_FILE = "autotile-qa.json"
META_FILE = "codeart-meta.json"
PREVIEW_FILE = "preview-map.png"
REVIEW_FILE = "review.png"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
HEX_PATTERN = re.compile(r"#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?")
REL_PATH_PATTERN = re.compile(r"(?!/)(?![A-Za-z][A-Za-z0-9+.-]*:)[^\\]+")
# Blob neighbour bits as in map.schema.json tile.blob_mask: bit i is neighbour i of N, NE, E, SE, S, SW, W, NW.
BLOB_OFFSETS = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))
BEVEL_SIDES = (("top", -1, 0), ("bottom", 1, 0), ("left", 0, -1), ("right", 0, 1))
DEFAULT_BEVEL = {"top": (2,), "bottom": (-2,), "left": (-1,), "right": (-1,)}
QA_SEVERITY = {"pass": 0, "skipped": 0, "needs-visual-review": 1, "warn": 2, "fail": 3}


class SpecError(ValueError):
    """An invalid material spec, texture or command-line value; printed as `error: ...`."""


class QCFailure(RuntimeError):
    """A failed check under --strict-qc; nothing is published."""


def _check(condition: Any, message: str) -> None:
    if not condition:
        raise SpecError(message)


def derive_seed(seed: int, *labels: Any) -> int:
    """A stable 64-bit seed for one purpose (never Python's salted hash())."""
    text = "|".join([str(seed), *(str(label) for label in labels)])
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")


# ----------------------------------------------------------------------------- noise and kernels

@dataclass(frozen=True)
class PeriodicNoise:
    """A sum of sines with whole cycles per tile, scaled to [-1, 1] over one tile.

    `at` takes doubled pixel-centre coordinates (2 * pixel + 1) and reduces each phase modulo
    2 * tile in exact integer arithmetic, so a tile evaluated at its local coordinates and a map
    evaluated at global coordinates give bit-identical values. A frequency that is not a whole
    number of cycles per tile (see `detuned`) makes them differ: that is what the seam proof sees."""

    tile: int
    terms: tuple[tuple[float, float, float, float], ...]   # (cycles along x, cycles along y, amplitude, phase)
    scale: float = 1.0

    @classmethod
    def make(cls, tile: int, seed: int, *, terms: int = 6, frequency: int = 2, along_x_only: bool = False
             ) -> PeriodicNoise:
        """`terms` random sines of up to `frequency` whole cycles per tile (x only when `along_x_only`)."""
        if frequency < 1 or terms < 1:
            raise ValueError("periodic noise needs at least one term of at least one cycle per tile")
        rng = np.random.default_rng(seed)
        chosen: list[tuple[float, float, float, float]] = []
        while len(chosen) < terms:
            kx, ky = (int(k) for k in rng.integers(-frequency, frequency + 1, 2))
            if along_x_only:
                ky = 0
            if kx == 0 and ky == 0:
                continue
            chosen.append((kx, ky, float(rng.uniform(0.4, 1.0)), float(rng.uniform(0.0, 2.0 * math.pi))))
        local = 2 * np.arange(tile) + 1
        peak = float(np.abs(cls(tile, tuple(chosen)).at(local, local)).max())
        return cls(tile, tuple(chosen), 1.0 / peak if peak > 0 else 1.0)

    def at(self, rows2: Any, cols2: Any) -> np.ndarray:
        rows2 = np.asarray(rows2, np.int64)[:, None]
        cols2 = np.asarray(cols2, np.int64)[None, :]
        total = np.zeros((rows2.shape[0], cols2.shape[1]))
        for kx, ky, amplitude, phase in self.terms:
            turns = np.mod(kx * cols2 + ky * rows2, 2 * self.tile)
            total = total + amplitude * np.sin(np.pi * turns / self.tile + phase)
        return total * self.scale

    def along(self, positions: Any) -> np.ndarray:
        """Values of an x-only noise at pixel positions (used for blob margins along an edge)."""
        return self.at(np.array([1]), 2 * np.asarray(positions, np.int64) + 1)[0]

    def detuned(self) -> PeriodicNoise:
        """The same noise with every frequency off by half a cycle per tile: it no longer wraps."""
        two_d = any(ky for _, ky, _, _ in self.terms)
        return replace(self, terms=tuple((kx + 0.5, ky + 0.5 if two_d else 0, a, p) for kx, ky, a, p in self.terms))


def plateau_kernel(offsets: Any, tile: int, plateau: int) -> np.ndarray:
    """Vertex weight at integer pixel offsets (pixel centres at offset + 0.5): 1 within `plateau` px,
    0 from `tile - plateau` px, smoothstep between; K(d) + K(d - tile) = 1 inside a cell."""
    distance = np.abs(np.asarray(offsets, np.float64) + 0.5)
    z = np.clip((distance - plateau) / float(tile - 2 * plateau), 0.0, 1.0)
    return 1.0 - z * z * (3.0 - 2.0 * z)


def shifted_and(mask: np.ndarray, other: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """mask & other moved by (dy, dx): result[..., y, x] = mask[y, x] & other[y - dy, x - dx]
    (cells moved in from outside count as False)."""
    out = np.zeros_like(mask)
    height, width = mask.shape[-2:]
    if abs(dy) < height and abs(dx) < width:
        target = (Ellipsis, slice(max(dy, 0), height + min(dy, 0)), slice(max(dx, 0), width + min(dx, 0)))
        source = (Ellipsis, slice(max(-dy, 0), height + min(-dy, 0)), slice(max(-dx, 0), width + min(-dx, 0)))
        out[target] = mask[target] & other[source]
    return out


def dilate4(mask: np.ndarray) -> np.ndarray:
    """Grow a mask by one pixel to its 4 neighbours."""
    out = mask.copy()
    out[..., 1:, :] |= mask[..., :-1, :]
    out[..., :-1, :] |= mask[..., 1:, :]
    out[..., :, 1:] |= mask[..., :, :-1]
    out[..., :, :-1] |= mask[..., :, 1:]
    return out


def erode8(mask: np.ndarray) -> np.ndarray:
    """Keep pixels whose whole 3x3 neighbourhood is set (outside counts as unset)."""
    column = mask.copy()
    column[..., 1:, :] &= mask[..., :-1, :]
    column[..., :-1, :] &= mask[..., 1:, :]
    column[..., (0, -1), :] = False
    out = column.copy()
    out[..., :, 1:] &= column[..., :, :-1]
    out[..., :, :-1] &= column[..., :, 1:]
    out[..., :, (0, -1)] = False
    return out


# ----------------------------------------------------------------------------- spec

@dataclass(frozen=True)
class Stamp:
    """A texture mark: pixels (dy, dx, ramp position) inside a height x width box."""

    pixels: tuple[tuple[int, int, int], ...]
    height: int
    width: int
    weight: float


@dataclass
class Texture:
    """How a material fills its pixels: a flat ramp position, shared periodic noise bands,
    per-variant marks, or a one-tile image (used as given, or quantized to the ramp)."""

    base: int
    frequency: int = 0
    levels: tuple[tuple[float, float, int], ...] = ()
    stamps: tuple[Stamp, ...] = ()
    marks_per_tile: tuple[int, int] = (0, 0)
    mark_margin: int = 1
    image_path: Path | None = None
    quantize: bool = False
    image: np.ndarray | None = None
    image_sha256: str | None = None


@dataclass(frozen=True)
class Rule:
    """An edge or shadow band: colours nearest first (ramp positions or RGB tuples) and the
    materials that cause it (None: every other material of the set)."""

    colours: tuple[Any, ...]
    sources: tuple[str, ...] | None


@dataclass
class Material:
    name: str
    ramp: tuple[tuple[int, int, int], ...]
    texture: Texture
    walkable: bool = True
    edges: tuple[Rule, ...] = ()
    shadows: tuple[Rule, ...] = ()
    bevel: dict[str, tuple[int, ...]] = field(default_factory=lambda: dict(DEFAULT_BEVEL))


@dataclass
class SetSpec:
    id: str
    kind: str
    materials: tuple[str, ...]
    radius: int
    plateau: int = 0
    wobble: float = DEFAULT_WOBBLE
    margin: tuple[int, int] = (0, 0)


@dataclass
class Spec:
    path: Path
    sha256: str
    tile: int
    variants: int
    seed: int
    collision_cell: int
    materials: dict[str, Material]
    sets: list[SetSpec]
    params: dict[str, Any]


def _whole(value: Any, label: str, low: int | None = None, high: int | None = None) -> int:
    _check(isinstance(value, int) and not isinstance(value, bool), f"{label} must be a whole number, got {value!r}")
    _check(low is None or value >= low, f"{label} must be at least {low}, got {value}")
    _check(high is None or value <= high, f"{label} must be at most {high}, got {value}")
    return int(value)


def _number(value: Any, label: str, low: float, high: float) -> float:
    _check(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
           f"{label} must be a finite number, got {value!r}")
    _check(low <= value <= high, f"{label} must be within {low}..{high}, got {value}")
    return float(value)


def _rgb(value: Any, label: str) -> tuple[int, int, int]:
    match = HEX_PATTERN.fullmatch(value) if isinstance(value, str) else None
    _check(match, f"{label} must be a #rrggbb colour, got {value!r}")
    _check(match.group(2) in (None, "ff", "FF"), f"{label} must be opaque (#rrggbb), got {value}")
    digits = match.group(1)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _colour_ref(value: Any, ramp_size: int, label: str) -> Any:
    """A ramp position (int, 0 = darkest) or an RGB tuple from #rrggbb."""
    if isinstance(value, int) and not isinstance(value, bool):
        _check(0 <= value < ramp_size, f"{label}: ramp position {value} is outside the ramp 0..{ramp_size - 1}")
        return int(value)
    return _rgb(value, label)


def _rules(value: Any, label: str, ramp_size: int, source_key: str) -> tuple[Rule, ...]:
    if value is None:
        return ()
    items = [value] if isinstance(value, Mapping) else value
    _check(isinstance(items, list), f"{label} must be an object or a list of objects")
    rules = []
    for index, item in enumerate(items):
        where = f"{label}[{index}]"
        _check(isinstance(item, Mapping), f"{where} must be an object with 'colors'")
        colours = item.get("colors")
        _check(isinstance(colours, list) and 1 <= len(colours) <= MAX_RULE_COLOURS,
               f"{where}.colors must list 1-{MAX_RULE_COLOURS} colours, nearest first")
        sources = item.get(source_key)
        if sources is not None:
            _check(isinstance(sources, list) and sources and all(isinstance(s, str) and s for s in sources),
                   f"{where}.{source_key} must be a non-empty list of material names")
        rules.append(Rule(tuple(_colour_ref(c, ramp_size, f"{where}.colors[{j}]") for j, c in enumerate(colours)),
                          tuple(sources) if sources is not None else None))
    return tuple(rules)


def _stamps(value: Any, label: str, ramp_size: int) -> tuple[Stamp, ...]:
    if value is None:
        return ()
    _check(isinstance(value, list), f"{label} must be a list of {{rows, weight?}} marks")
    stamps = []
    for index, item in enumerate(value):
        where = f"{label}[{index}]"
        _check(isinstance(item, Mapping) and isinstance(item.get("rows"), list) and item["rows"]
               and all(isinstance(row, str) for row in item["rows"]),
               f"{where} needs rows: a list of strings (digits = ramp positions, '.' = untouched)")
        pixels = []
        for dy, row in enumerate(item["rows"]):
            for dx, char in enumerate(row):
                if char in ". ":
                    continue
                _check(char.isdigit() and int(char) < ramp_size,
                       f"{where}.rows[{dy}] has {char!r}; use digits 0..{min(ramp_size, 10) - 1} "
                       f"(ramp positions) or '.'")
                pixels.append((dy, dx, int(char)))
        _check(pixels, f"{where} draws no pixels")
        weight = _number(item.get("weight", 1.0), f"{where}.weight", 1e-6, 1e6)
        stamps.append(Stamp(tuple(pixels), max(p[0] for p in pixels) + 1, max(p[1] for p in pixels) + 1, weight))
    return tuple(stamps)


def _spec_relative(base: Path, value: Any, label: str) -> Path:
    _check(isinstance(value, str) and REL_PATH_PATTERN.fullmatch(value),
           f"{label} must be a relative POSIX path (resolved from the spec's folder), got {value!r}")
    return (base / value).resolve()


def _texture(value: Any, label: str, ramp_size: int, spec_dir: Path) -> Texture:
    default_base = ramp_size // 2
    if value is None:
        return Texture(base=default_base)
    if isinstance(value, str):
        return Texture(base=default_base, image_path=_spec_relative(spec_dir, value, label))
    _check(isinstance(value, Mapping), f"{label} must be an object (procedural texture) or an image path")
    texture = Texture(base=_whole(value.get("base", default_base), f"{label}.base", 0, ramp_size - 1))
    if "image" in value:
        texture.image_path = _spec_relative(spec_dir, value["image"], f"{label}.image")
    quantize = value.get("quantize", False)
    _check(isinstance(quantize, bool), f"{label}.quantize must be true or false")
    texture.quantize = quantize
    noise = value.get("noise")
    if noise is not None:
        _check(isinstance(noise, Mapping), f"{label}.noise must be an object {{frequency, levels}}")
        texture.frequency = _whole(noise.get("frequency", 2), f"{label}.noise.frequency", 1, 8)
        levels = noise.get("levels")
        _check(isinstance(levels, list) and levels, f"{label}.noise.levels must list [low, high, ramp position] bands")
        parsed = []
        for index, level in enumerate(levels):
            where = f"{label}.noise.levels[{index}]"
            _check(isinstance(level, list) and len(level) == 3, f"{where} must be [low, high, ramp position]")
            low, high = _number(level[0], where, 0.0, 1.0), _number(level[1], where, 0.0, 1.01)
            _check(low < high, f"{where} needs low < high")
            parsed.append((low, high, _whole(level[2], f"{where} ramp position", 0, ramp_size - 1)))
        texture.levels = tuple(parsed)
    texture.stamps = _stamps(value.get("marks"), f"{label}.marks", ramp_size)
    per_tile = value.get("marks_per_tile", [2, 4] if texture.stamps else 0)
    if isinstance(per_tile, list):
        _check(len(per_tile) == 2, f"{label}.marks_per_tile must be a count or [min, max]")
        low, high = (_whole(v, f"{label}.marks_per_tile", 0, 64) for v in per_tile)
        _check(low <= high, f"{label}.marks_per_tile needs min <= max")
        texture.marks_per_tile = (low, high)
    else:
        count = _whole(per_tile, f"{label}.marks_per_tile", 0, 64)
        texture.marks_per_tile = (count, count)
    texture.mark_margin = _whole(value.get("mark_margin", 1), f"{label}.mark_margin", 1, 16)
    return texture


def _material(name: str, value: Any, spec_dir: Path) -> Material:
    label = f"materials.{name}"
    _check(NAME_PATTERN.fullmatch(name), f"{label}: material names use a-z, 0-9, '-' and '_' (max 48 characters)")
    _check(isinstance(value, Mapping), f"{label} must be an object with a ramp")
    ramp = value.get("ramp")
    _check(isinstance(ramp, list) and 1 <= len(ramp) <= MAX_RAMP,
           f"{label}.ramp must list 1-{MAX_RAMP} colours, dark to light")
    ramp_rgb = tuple(_rgb(colour, f"{label}.ramp[{i}]") for i, colour in enumerate(ramp))
    walkable = value.get("walkable", True)
    _check(isinstance(walkable, bool), f"{label}.walkable must be true or false")
    material = Material(name, ramp_rgb, _texture(value.get("texture"), f"{label}.texture", len(ramp_rgb), spec_dir),
                        walkable, _rules(value.get("edge"), f"{label}.edge", len(ramp_rgb), "against"),
                        _rules(value.get("shadow"), f"{label}.shadow", len(ramp_rgb), "from"))
    bevel = value.get("bevel")
    if bevel is not None:
        _check(isinstance(bevel, Mapping) and set(bevel) <= {"top", "bottom", "left", "right"},
               f"{label}.bevel must map top/bottom/left/right to lists of ramp steps")
        for side, steps in bevel.items():
            _check(isinstance(steps, list) and len(steps) <= MAX_RULE_COLOURS
                   and all(isinstance(s, int) and not isinstance(s, bool) and -MAX_RAMP < s < MAX_RAMP for s in steps),
                   f"{label}.bevel.{side} must list ramp steps (e.g. [2] lighter, [-1] darker), nearest first")
            material.bevel[side] = tuple(steps)
    return material


def _rule_radius(material: Material) -> int:
    return max((len(rule.colours) for rule in material.edges + material.shadows), default=0)


def _set_spec(index: int, value: Any, materials: dict[str, Material], tile: int) -> SetSpec:
    label = f"sets[{index}]"
    _check(isinstance(value, Mapping), f"{label} must be an object {{kind, materials}}")
    kind = value.get("kind")
    _check(kind in KINDS, f"{label}.kind must be one of {', '.join(KINDS)}, got {kind!r}")
    names = value.get("materials")
    low, high = SET_MATERIALS[kind]
    _check(isinstance(names, list) and low <= len(names) <= high and all(isinstance(n, str) for n in names),
           f"{label}: a {kind} set takes {low if low == high else f'{low}-{high}'} material name(s)"
           + (" [under, fill]" if kind == "blob47" else ""))
    _check(len(set(names)) == len(names), f"{label}.materials repeats a material")
    unknown = [name for name in names if name not in materials]
    _check(not unknown, f"{label} names unknown material(s): {', '.join(unknown)}")
    suffix = {"wang_corner": f"wang{len(names) ** 4}", "blob47": "blob47", "bevel": "bevel16", "flat": "flat"}[kind]
    set_id = value.get("id", "-".join(names) + "-" + suffix)
    _check(isinstance(set_id, str) and NAME_PATTERN.fullmatch(set_id),
           f"{label}.id must use a-z, 0-9, '-' and '_' (max 48 characters)")
    spec = SetSpec(set_id, kind, tuple(names), radius=0)
    if kind == "wang_corner":
        spec.radius = max(_rule_radius(materials[name]) for name in names)
        limit = (tile - 2) // 2
        spec.plateau = _whole(value.get("plateau", max(spec.radius, max(1, tile // 8))), f"{label}.plateau", 0, limit)
        _check(spec.plateau >= spec.radius,
               f"{label}: band look-ups reach {spec.radius} px but the plateau is {spec.plateau} px; tiles stay "
               f"exact only when the plateau is at least the longest edge/shadow colour list")
        spec.wobble = _number(value.get("wobble", DEFAULT_WOBBLE), f"{label}.wobble", 0.0, 0.9)
    elif kind == "blob47":
        spec.radius = _rule_radius(materials[names[1]])
        low_default = max(spec.radius, max(1, tile // 8))
        margin = value.get("margin", [low_default, low_default + max(1, tile // 8)])
        _check(isinstance(margin, list) and len(margin) == 2, f"{label}.margin must be [min, max] px")
        low_depth = _whole(margin[0], f"{label}.margin[0]", 1, tile // 2)
        high_depth = _whole(margin[1], f"{label}.margin[1]", low_depth, tile // 2)
        _check(low_depth >= spec.radius,
               f"{label}: the fill's edge/shadow bands reach {spec.radius} px, so margin[0] must be at least "
               f"{spec.radius}")
        _check(high_depth + spec.radius <= tile // 2,
               f"{label}: margin[1] plus the band width must be at most half a tile ({tile // 2} px)")
        spec.margin = (low_depth, high_depth)
    elif kind == "bevel":
        spec.radius = max((len(v) for v in materials[names[0]].bevel.values()), default=0)
        _check(spec.radius <= tile // 2 - 1, f"{label}: bevel faces must be thinner than half a tile")
    return spec


def _load_texture_image(path: Path, tile: int, label: str) -> tuple[np.ndarray, str]:
    _check(path.is_file(), f"{label}: texture image not found: {path}")
    try:
        image, info = forge_core.load_rgba(path)
    except (OSError, ValueError) as exc:
        raise SpecError(f"{label}: cannot read texture image {path.name}: {exc}") from None
    pixels = np.asarray(image)
    _check(pixels.shape[:2] == (tile, tile),
           f"{label}: texture {path.name} is {pixels.shape[1]}x{pixels.shape[0]} px; it must be exactly one tile "
           f"({tile}x{tile}) and wrap seamlessly, because every tile samples it at tile-local coordinates")
    _check(bool((pixels[..., 3] == 255).all()), f"{label}: texture {path.name} must be fully opaque")
    return np.ascontiguousarray(pixels[..., :3]), info["sha256"]


def load_spec(path: Path, *, kinds: tuple[str, ...] = KINDS, tile_size: int | None = None,
              variants: int | None = None, seed: int | None = None, textures: Mapping[str, Path] | None = None,
              quantize: bool = False) -> Spec:
    """Read and validate a codeart2d.material_spec.v1 file; command-line values override the spec."""
    _check(path.is_file(), f"material spec not found: {path}")
    raw = path.read_bytes()
    try:
        data = forge_core.parse_json(raw)  # UTF-8 with an optional BOM (D28)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise SpecError(f"material spec {path.name} is not valid UTF-8 JSON: {exc}") from None
    _check(isinstance(data, Mapping), "the material spec must be a JSON object")
    _check(data.get("schema") == SPEC_SCHEMA, f"the material spec needs \"schema\": \"{SPEC_SCHEMA}\"")
    size = data.get("tile_size")
    if isinstance(size, list):
        _check(len(size) == 2 and size[0] == size[1],
               "tile_size [w, h] must be square; non-square tiles are not supported")
        size = size[0]
    tile = _whole(tile_size if tile_size is not None else size, "tile_size", *TILE_RANGE)
    count = _whole(variants if variants is not None else data.get("variants", DEFAULT_VARIANTS), "variants",
                   1, MAX_VARIANTS)
    base_seed = _whole(seed if seed is not None else data.get("seed"), "seed", 0, 2 ** 63 - 1)
    cell = _whole(data.get("collision_cell", max(1, tile // 4)), "collision_cell", 1, tile)
    _check(tile % cell == 0, f"collision_cell {cell} must divide the tile size {tile}")
    raw_materials = data.get("materials")
    _check(isinstance(raw_materials, Mapping) and raw_materials, "materials must map names to {ramp, texture?}")
    materials = {name: _material(name, value, path.parent) for name, value in raw_materials.items()}
    for material in materials.values():
        for rule in material.edges + material.shadows:
            unknown = [name for name in rule.sources or () if name not in materials]
            _check(not unknown, f"materials.{material.name}: band rule names unknown material(s) {', '.join(unknown)}")
    for name, texture_path in (textures or {}).items():
        _check(name in materials, f"--material-texture names unknown material {name!r}")
        materials[name].texture.image_path = Path(texture_path).resolve()
    for material in materials.values():
        texture = material.texture
        if texture.image_path is not None:
            texture.image, texture.image_sha256 = _load_texture_image(texture.image_path, tile,
                                                                      f"materials.{material.name}.texture")
            texture.quantize = texture.quantize or quantize
        stamp_room = tile - 2 * texture.mark_margin
        for stamp in texture.stamps:
            _check(stamp.height <= stamp_room and stamp.width <= stamp_room,
                   f"materials.{material.name}: a {stamp.width}x{stamp.height} mark does not fit inside a {tile} px "
                   f"tile with mark_margin {texture.mark_margin}")
    raw_sets = data.get("sets")
    _check(isinstance(raw_sets, list) and raw_sets, "sets must list at least one {kind, materials} set")
    sets = [_set_spec(i, value, materials, tile) for i, value in enumerate(raw_sets)]
    ids = [s.id for s in sets]
    _check(len(set(ids)) == len(ids), f"set ids must be unique: {', '.join(ids)}")
    chosen = [s for s in sets if s.kind in kinds]
    _check(chosen, f"the spec has no {' or '.join(kinds)} set (its sets: "
                   f"{', '.join(f'{s.id} ({s.kind})' for s in sets)})")
    params = {"tile_size": tile, "variants": count, "seed": base_seed, "kinds": list(kinds),
              "collision_cell": cell, "quantize_textures": bool(quantize)}
    return Spec(path, forge_core.sha256_bytes(raw), tile, count, base_seed, cell, materials, chosen, params)


# ----------------------------------------------------------------------------- per-set art

@dataclass
class Planes:
    """Per-pixel classification of a tile batch or a map, shape (B, height, width): material
    index (-1 = nothing), band colour (table index, -1 = none) or bevel face step (0 = none).
    mark_fits[material][variant][i] says per cell (B, H, W) whether mark i of that variant fits."""

    mat: np.ndarray
    band: np.ndarray | None = None
    face: np.ndarray | None = None
    mark_fits: list[list[list[np.ndarray]]] | None = None


@dataclass
class SetArt:
    """Everything one tileset is drawn from: colours, textures, marks, band rules and noise."""

    spec: SetSpec
    tile: int
    variants: int
    seed: int
    materials: list[Material]
    painted: tuple[bool, ...]
    table: np.ndarray                     # (C, 4) uint8 colours, index 0 transparent
    palette: dict[str, str]               # name -> #rrggbb of ramp and rule colours (not image colours)
    ramp_index: list[np.ndarray]          # per material: table index of each ramp position
    base: list[int]                       # per material: flat ramp position
    texture_noise: list[PeriodicNoise | None]
    levels: list[tuple[tuple[float, float, int], ...]]
    image_index: list[np.ndarray | None]  # per material (T, T) table indices of an image used as given
    image_pos: list[np.ndarray | None]    # per material (T, T) ramp positions of a quantized image
    marks: list[list[list[tuple[np.ndarray, np.ndarray, np.ndarray]]]]  # [material][variant] -> stamp pixels
    edge_rules: list[tuple[int, np.ndarray, tuple[int, ...]]] = field(default_factory=list)
    shadow_rules: list[tuple[int, np.ndarray, tuple[int, ...]]] = field(default_factory=list)
    faces: dict[str, tuple[int, ...]] = field(default_factory=dict)
    wobble: tuple[PeriodicNoise, ...] = ()          # Wang: boundary noise per material
    margins: tuple[PeriodicNoise, ...] = ()         # blob: margin noise along N, E, S, W
    image_rgb: bool = False
    cache: dict = field(default_factory=dict, repr=False)

    @property
    def n(self) -> int:
        return len(self.materials)

    @property
    def radius(self) -> int:
        return self.spec.radius

    def kernel(self, offsets: Any) -> np.ndarray:
        return plateau_kernel(offsets, self.tile, self.spec.plateau)

    def wobble_factors(self, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
        """(1, n, h, w) float32 Wang weight factors 1 + wobble * noise at pixel rows and columns."""
        key = ("wobble", int(rows[0]), rows.size, int(cols[0]), cols.size)
        if key not in self.cache:
            self.cache[key] = np.stack([1.0 + self.spec.wobble * noise.at(2 * rows + 1, 2 * cols + 1)
                                        for noise in self.wobble])[None].astype(np.float32)
        return self.cache[key]

    def corner_terms(self, rows: np.ndarray, cols: np.ndarray) -> tuple[np.ndarray, ...]:
        """float32 K(dy) * K(dx) planes for the TL, TR, BL, BR corners of a cell, at pixel offsets
        `rows` and `cols` from its top-left vertex."""
        near_y, far_y = self.kernel(rows), self.kernel(rows - self.tile)
        near_x, far_x = self.kernel(cols), self.kernel(cols - self.tile)
        return tuple((y[:, None] * x[None, :]).astype(np.float32)
                     for y, x in ((near_y, near_x), (near_y, far_x), (far_y, near_x), (far_y, far_x)))

    def fill_planes(self, height: int, width: int) -> list[tuple[np.ndarray, np.ndarray]]:
        """Per material: ramp positions (-1 = image pixel) and image table indices for a canvas
        whose top-left pixel is map pixel (0, 0)."""
        key = ("fill", height, width)
        if key not in self.cache:
            rows, cols = np.arange(height), np.arange(width)
            planes = []
            for material in range(self.n):
                positions = np.full((height, width), self.base[material], np.int16)
                images = np.zeros((height, width), np.int16)
                if self.image_index[material] is not None:
                    positions[:] = -1
                    images = self.image_index[material][(rows % self.tile)[:, None], (cols % self.tile)[None, :]]
                elif self.image_pos[material] is not None:
                    positions = self.image_pos[material][(rows % self.tile)[:, None], (cols % self.tile)[None, :]]
                elif self.texture_noise[material] is not None:
                    value = (self.texture_noise[material].at(2 * rows + 1, 2 * cols + 1) + 1.0) / 2.0
                    for low, high, position in self.levels[material]:
                        positions[(value >= low) & (value < high)] = position
                planes.append((positions, images))
            self.cache[key] = planes
        return self.cache[key]

    def margin_depths(self, height: int, width: int) -> np.ndarray:
        """Blob margin depths (height, width, 4, T) per cell of a map: N and S along the cell's
        columns, E and W along its rows, from the margin noise at map coordinates."""
        key = ("margins", height, width)
        if key not in self.cache:
            low, high = self.spec.margin
            tile = self.tile

            def depth(side: int, count: int) -> np.ndarray:
                value = (self.margins[side].along(np.arange(count * tile)) + 1.0) / 2.0
                depths = np.clip(low + np.floor(value * (high - low + 1)), low, high)
                return depths.astype(np.int64).reshape(count, tile)

            north, east, south, west = depth(0, width), depth(1, height), depth(2, width), depth(3, height)
            out = np.empty((height, width, 4, tile), np.int64)
            out[:, :, 0], out[:, :, 2] = north[None], south[None]
            out[:, :, 1], out[:, :, 3] = east[:, None], west[:, None]
            self.cache[key] = out
        return self.cache[key]

    def control(self) -> SetArt | None:
        """A copy whose periodic noise is detuned by half a cycle per tile (so it no longer wraps),
        or None when the set has no periodic noise to detune."""
        texture = [noise.detuned() if noise is not None else None for noise in self.texture_noise]
        wobble = tuple(noise.detuned() for noise in self.wobble) if self.spec.wobble > 0 else self.wobble
        margins = tuple(noise.detuned() for noise in self.margins) if self.spec.margin[1] > self.spec.margin[0] \
            else self.margins
        if texture == self.texture_noise and wobble == self.wobble and margins == self.margins:
            return None
        return replace(self, texture_noise=texture, wobble=wobble, margins=margins, cache={})


class _ColourTable:
    def __init__(self) -> None:
        self.rows: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)]
        self.lookup: dict[tuple[int, int, int], int] = {}

    def add(self, rgb: Any) -> int:
        key = tuple(int(v) for v in rgb)
        if key not in self.lookup:
            self.lookup[key] = len(self.rows)
            self.rows.append((*key, 255))
        return self.lookup[key]


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _place_marks(texture: Texture, tile: int, seed: int) -> list[tuple[int, int, Stamp]]:
    """Non-overlapping, spaced marks inside [margin, tile - margin) for one material variant."""
    if not texture.stamps or texture.marks_per_tile[1] == 0:
        return []
    rng = np.random.default_rng(seed)
    low, high = texture.marks_per_tile
    wanted = int(rng.integers(low, high + 1))
    weights = np.array([stamp.weight for stamp in texture.stamps], np.float64)
    weights /= weights.sum()
    taken = np.zeros((tile, tile), bool)
    placed: list[tuple[int, int, Stamp]] = []
    margin = texture.mark_margin
    for _ in range(40 * max(wanted, 1)):
        if len(placed) >= wanted:
            break
        stamp = texture.stamps[int(rng.choice(len(texture.stamps), p=weights))]
        y = int(rng.integers(margin, tile - margin - stamp.height + 1))
        x = int(rng.integers(margin, tile - margin - stamp.width + 1))
        if taken[max(0, y - 1):y + stamp.height + 1, max(0, x - 1):x + stamp.width + 1].any():
            continue
        taken[y:y + stamp.height, x:x + stamp.width] = True
        placed.append((y, x, stamp))
    return placed


def build_art(spec: Spec, set_spec: SetSpec) -> SetArt:
    """Resolve colours, textures, marks, rules and noise for one tileset."""
    tile, seed = spec.tile, spec.seed
    materials = [spec.materials[name] for name in set_spec.materials]
    painted = (False, True) if set_spec.kind == "blob47" else (True,) * len(materials)
    table = _ColourTable()
    palette: dict[str, str] = {}
    ramp_index, texture_noise, image_index, image_pos, marks = [], [], [], [], []
    image_rgb = False
    for index, material in enumerate(materials):
        ramp = np.array([table.add(rgb) for rgb in material.ramp], np.int16)
        ramp_index.append(ramp)
        if painted[index]:
            palette.update({f"{material.name}-{i}": _hex(rgb) for i, rgb in enumerate(material.ramp)})
        texture = material.texture
        texture_noise.append(PeriodicNoise.make(tile, derive_seed(seed, "texture", material.name), terms=4,
                                                frequency=texture.frequency)
                             if texture.frequency and texture.levels and texture.image is None else None)
        if texture.image is not None and texture.quantize:
            distance = ((texture.image[:, :, None, :].astype(np.int32)
                         - np.array(material.ramp, np.int32)[None, None]) ** 2).sum(-1)
            image_pos.append(distance.argmin(-1).astype(np.int16))
            image_index.append(None)
        elif texture.image is not None:
            image_pos.append(None)
            image_index.append(np.array([[table.add(px) for px in row] for row in texture.image], np.int16))
            image_rgb = image_rgb or painted[index]
        else:
            image_pos.append(None)
            image_index.append(None)
        per_variant = []
        for variant in range(spec.variants):
            instances = []
            for y, x, stamp in _place_marks(texture, tile, derive_seed(seed, "marks", material.name, variant)):
                ys = np.array([y + p[0] for p in stamp.pixels], np.intp)
                xs = np.array([x + p[1] for p in stamp.pixels], np.intp)
                instances.append((ys, xs, ramp[[p[2] for p in stamp.pixels]].astype(np.int16)))
            per_variant.append(instances)
        marks.append(per_variant)

    art = SetArt(set_spec, tile, spec.variants, seed, materials, painted, np.zeros((0, 4), np.uint8), palette,
                 ramp_index, [m.texture.base for m in materials], texture_noise,
                 [m.texture.levels for m in materials], image_index, image_pos, marks, image_rgb=image_rgb)
    names = list(set_spec.materials)
    if set_spec.kind in ("wang_corner", "blob47"):
        for index, material in enumerate(materials):
            if not painted[index]:
                continue
            others = [i for i in range(len(names)) if i != index]
            for kind, rules, target in (("edge", material.edges, art.edge_rules),
                                        ("shadow", material.shadows, art.shadow_rules)):
                for number, rule in enumerate(rules):
                    sources = others if rule.sources is None else [names.index(s) for s in rule.sources if s in names]
                    colours = []
                    for j, ref in enumerate(rule.colours):
                        if isinstance(ref, int):
                            colours.append(int(ramp_index[index][ref]))
                        else:
                            palette[f"{material.name}-{kind}{number}-{j}"] = _hex(ref)
                            colours.append(table.add(ref))
                    if sources:
                        target.append((index, np.array(sources, np.int8), tuple(colours)))
    if set_spec.kind == "wang_corner":
        art.wobble = tuple(PeriodicNoise.make(tile, derive_seed(seed, "wobble", set_spec.id, name)) for name in names)
    elif set_spec.kind == "blob47":
        art.margins = tuple(PeriodicNoise.make(tile, derive_seed(seed, "margin", set_spec.id, side), terms=3,
                                               frequency=2, along_x_only=True) for side in ("N", "E", "S", "W"))
    elif set_spec.kind == "bevel":
        art.faces = dict(materials[0].bevel)
    art.table = np.array(table.rows, np.uint8)
    return art


# ----------------------------------------------------------------------------- planes

def _material_lookup(mat: np.ndarray, materials: np.ndarray) -> np.ndarray:
    """mat is one of `materials` (a handful of material indices)."""
    found = mat == materials[0]
    for material in materials[1:]:
        found |= mat == material
    return found


def compute_bands(art: SetArt, mat: np.ndarray) -> np.ndarray:
    """Edge bands (Manhattan distance to a source material, nearest colour first) then shadows
    (nearest source straight above); later rules override earlier ones, shadows override edges."""
    band = np.full(mat.shape, -1, np.int16)
    for index, sources, colours in art.edge_rules:
        own = mat == index
        reach = _material_lookup(mat, sources)
        for colour in colours:
            grown = dilate4(reach)
            band[own & grown & ~reach] = colour
            reach = grown
    for index, sources, colours in art.shadow_rules:
        own = mat == index
        source = _material_lookup(mat, sources)
        for distance in range(len(colours), 0, -1):
            band[shifted_and(own, source, distance, 0)] = colours[distance - 1]
    return band


def bevel_faces(art: SetArt, solid: np.ndarray) -> np.ndarray:
    """Summed ramp steps of the faces each solid pixel shows (nearest open pixel per side)."""
    face = np.zeros(solid.shape, np.int8)
    open_ = ~solid
    for side, dy, dx in BEVEL_SIDES:
        seen = np.zeros(solid.shape, bool)
        for distance, step in enumerate(art.faces.get(side, ()), start=1):
            hit = shifted_and(solid, open_, -dy * distance, -dx * distance) & ~seen
            face[hit] += step
            seen |= hit
    return face


def strongest(corners: list[np.ndarray], terms: tuple[np.ndarray, ...], factors: np.ndarray) -> np.ndarray:
    """Material with the largest corner weight; ties go to the lower material index.

    corners: TL, TR, BL, BR material planes; terms: the matching float32 K(dy) * K(dx) planes;
    factors: (1, n, h, w) float32 wobble factors. Every path sums the four products in the same
    order and the same dtype, so equal inputs give bit-identical weights."""
    best = mat = None
    zero = np.float32(0.0)
    for material in range(factors.shape[1]):
        blend = np.where(corners[0] == material, terms[0], zero)
        blend += np.where(corners[1] == material, terms[1], zero)
        blend += np.where(corners[2] == material, terms[2], zero)
        blend += np.where(corners[3] == material, terms[3], zero)
        blend *= factors[:, material]
        if best is None:
            best, mat = blend, np.zeros(blend.shape, np.int8)
        else:
            better = blend > best
            mat[better] = material
            np.maximum(best, blend, out=best)
    return mat


def blob_inside(bits: np.ndarray, depths: np.ndarray, tile: int) -> np.ndarray:
    """Inside masks (N, T, T) from raw neighbour bits (N, 8): a margin where a side neighbour is
    missing, a notch (the two margin bands intersected) where both sides are present but the
    diagonal is not. depths is (N, 4, T): margin depth per position along N, E, S, W."""
    ys, xs = np.mgrid[0:tile, 0:tile]
    north, east, south, west = (depths[:, i] for i in range(4))
    top = ys < np.take(north, xs, axis=-1)
    bottom = ys > tile - 1 - np.take(south, xs, axis=-1)
    left = xs < np.take(west, ys, axis=-1)
    right = xs > tile - 1 - np.take(east, ys, axis=-1)
    n, ne, e, se, s, sw, w, nw = (bits[:, i, None, None] for i in range(8))
    outside = ((~n & top) | (~s & bottom) | (~w & left) | (~e & right)
               | (n & w & ~nw & top & left) | (n & e & ~ne & top & right)
               | (s & w & ~sw & bottom & left) | (s & e & ~se & bottom & right))
    return ~outside


def canonical_blob(masks: Any) -> np.ndarray:
    """Drop diagonal bits whose two neighbouring side bits are not both set (blob-47 canonical form)."""
    masks = np.asarray(masks, np.int32)
    bit = [(masks >> i) & 1 for i in range(8)]
    bit[1] &= bit[0] & bit[2]
    bit[3] &= bit[4] & bit[2]
    bit[5] &= bit[4] & bit[6]
    bit[7] &= bit[0] & bit[6]
    return sum(b << i for i, b in enumerate(bit))


def _neighbour_masks(occupancy: np.ndarray, pad: bool) -> np.ndarray:
    """Raw 8-neighbour bit masks (B, H, W) of an occupancy grid; cells outside it take `pad`."""
    batch, height, width = occupancy.shape
    padded = np.pad(occupancy, ((0, 0), (1, 1), (1, 1)), constant_values=pad)
    masks = np.zeros((batch, height, width), np.int32)
    for bit, (dy, dx) in enumerate(BLOB_OFFSETS):
        masks |= padded[:, 1 + dy:1 + dy + height, 1 + dx:1 + dx + width].astype(np.int32) << bit
    return masks


def _to_canvas(cells: np.ndarray) -> np.ndarray:
    """(B, H, W, T, T, ...) per-cell planes -> (B, H*T, W*T, ...)."""
    batch, height, width, tile = cells.shape[:4]
    order = (0, 1, 3, 2, 4) + tuple(range(5, cells.ndim))
    return cells.transpose(order).reshape((batch, height * tile, width * tile) + cells.shape[5:])


def finish_planes(art: SetArt, planes: Planes) -> Planes:
    """Decide per cell which marks fit: every pixel of a mark must lie inside its material, off
    every band and face, with all 8 neighbours of the same material."""
    batch, height_px, width_px = planes.mat.shape
    tile = art.tile
    fits: list[list[list[np.ndarray]]] = []
    for material in range(art.n):
        if not art.painted[material] or not any(art.marks[material]):
            fits.append([[] for _ in range(art.variants)])
            continue
        own = planes.mat == material
        ok = erode8(own)
        if planes.band is not None:
            ok &= planes.band < 0
        if planes.face is not None:
            ok &= planes.face == 0
        ok5 = ok.reshape(batch, height_px // tile, tile, width_px // tile, tile)
        fits.append([[ok5[:, :, ys, :, xs].all(axis=0) for ys, xs, _ in instances]
                     for instances in art.marks[material]])
    planes.mark_fits = fits
    return planes


def base_index(art: SetArt, planes: Planes) -> np.ndarray:
    """Colour-table indices of everything that does not depend on the variant: material fills
    (with bevel face steps) and bands. Canvas top-left is map pixel (0, 0)."""
    mat = planes.mat
    index = np.zeros(mat.shape, np.int16)
    for material, (positions, images) in enumerate(art.fill_planes(*mat.shape[1:])):
        if not art.painted[material]:
            continue
        top = len(art.ramp_index[material]) - 1
        if planes.face is not None:
            face = planes.face
            positions = np.broadcast_to(positions, mat.shape)
            positions = np.where(positions >= 0, np.clip(positions + face, 0, top),
                                 np.where(face > 0, top, np.where(face < 0, 0, -1)))
        colour = np.where(positions >= 0, art.ramp_index[material][np.clip(positions, 0, top)], images)
        index = np.where(mat == material, colour, index)
    if planes.band is not None:
        index = np.where(planes.band >= 0, planes.band, index)
    return index


def packed(rgba: np.ndarray) -> np.ndarray:
    """RGBA uint8 (..., 4) viewed as one uint32 per pixel (a lossless, one-to-one view)."""
    return np.ascontiguousarray(rgba).view(np.uint32)[..., 0]


def compose(art: SetArt, planes: Planes, variants: np.ndarray, base: np.ndarray | None = None, *,
            as_packed: bool = False) -> np.ndarray:
    """RGBA pixels for planes of shape (B, H*T, W*T) with one variant per cell (B, H, W): the
    base indices (see base_index) plus each variant's marks where they fit. With as_packed the
    pixels come back as packed uint32 (see packed)."""
    index = (base_index(art, planes) if base is None else base).copy()
    batch, height_px, width_px = index.shape
    tile = art.tile
    if planes.mark_fits is not None:
        index5 = index.reshape(batch, height_px // tile, tile, width_px // tile, tile)
        for material, per_variant in enumerate(art.marks):
            for variant, instances in enumerate(per_variant):
                if not instances:
                    continue
                cells = variants == variant
                for (ys, xs, colours), fits in zip(instances, planes.mark_fits[material][variant]):
                    place = fits & cells
                    if place.any():
                        index5[:, :, ys, :, xs] = np.where(place[None], colours[:, None, None, None],
                                                           index5[:, :, ys, :, xs])
    return (packed(art.table) if as_packed else art.table)[index]


# ----------------------------------------------------------------------------- builders

@dataclass
class ProofCase:
    name: str
    content: np.ndarray                  # batch of map contents (Wang lattices or occupancy grids)
    compare: np.ndarray                  # (H, W) cells whose pixels are compared
    pads: tuple[bool, ...] = (False,)    # occupancy outside the window (blob, bevel)
    variants: np.ndarray | None = None   # explicit (B, H, W) variants instead of rotations


class Builder:
    """Tiles, assembly, the global render and the seam proof of one tileset kind.

    The tile path renders each topology key once, reading anything outside the tile from the
    key alone; the global path renders whole maps from map coordinates. The proof compares
    them pixel for pixel."""

    kind = ""

    def __init__(self, art: SetArt) -> None:
        self.art = art
        self.keys = self.make_keys()

    # -- kind hooks
    def make_keys(self) -> list:
        raise NotImplementedError

    def tile_planes(self) -> Planes:
        raise NotImplementedError

    def global_planes(self, content: np.ndarray, pad: bool = False) -> Planes:
        raise NotImplementedError

    def cell_keys(self, content: np.ndarray, pad: bool = False) -> np.ndarray:
        raise NotImplementedError

    def tile_fields(self, rank: int) -> dict:
        raise NotImplementedError

    def proof_cases(self) -> list[ProofCase]:
        raise NotImplementedError

    def random_content(self, rng: np.random.Generator, width: int, height: int) -> np.ndarray:
        raise NotImplementedError

    def uniform_content(self, material: int, width: int, height: int) -> np.ndarray:
        raise NotImplementedError

    def wangset(self) -> dict | None:
        return None

    # -- shared machinery
    def blocked(self, planes: Planes) -> np.ndarray:
        """Pixels of painted, non-walkable materials."""
        walkable = np.array([m.walkable for m in self.art.materials])
        painted = np.array(self.art.painted)
        index = np.clip(planes.mat, 0, None)
        return (planes.mat >= 0) & painted[index] & ~walkable[index]

    def render_tiles(self, planes: Planes | None = None) -> np.ndarray:
        """(variants, keys, T, T, 4) tiles, from `planes` (tile_planes()) when given."""
        planes = finish_planes(self.art, planes if planes is not None else self.tile_planes())
        base = base_index(self.art, planes)
        count = len(self.keys)
        return np.stack([compose(self.art, planes, np.full((count, 1, 1), variant), base)
                         for variant in range(self.art.variants)])

    def assemble(self, keys: np.ndarray, variants: np.ndarray, tiles: np.ndarray) -> np.ndarray:
        """Map pixels from atlas tiles (RGBA or packed): a key rank and a variant per cell (key -1 = no tile)."""
        empty = np.zeros((tiles.shape[0], 1) + tiles.shape[2:], tiles.dtype)
        cells = np.concatenate([tiles, empty], axis=1)[variants, np.where(keys >= 0, keys, len(self.keys))]
        return _to_canvas(cells)

    def render_global(self, content: np.ndarray, variants: np.ndarray, pad: bool = False) -> tuple[np.ndarray, Planes]:
        planes = finish_planes(self.art, self.global_planes(content, pad))
        return compose(self.art, planes, variants), planes

    def proof_size(self) -> int:
        tile, count = self.art.tile, self.art.variants
        total = 0
        for case in self.proof_cases():
            rotations = 1 if case.variants is not None else count
            total += int(case.compare.sum()) * tile * tile * len(case.content) * len(case.pads) * rotations
        return total

    def prove(self, tiles: np.ndarray) -> dict:
        """Exhaustive seam proof: tiles assembled by key equal the global render in every case.
        Pixels are compared as packed RGBA, so any channel difference counts."""
        tile, count = self.art.tile, self.art.variants
        tiles = packed(tiles)
        compared = mismatched = 0
        examples: list[dict] = []
        cases = []
        for case in self.proof_cases():
            height, width = case.compare.shape
            rotations = 1 if case.variants is not None else count
            mask = np.repeat(np.repeat(case.compare, tile, 0), tile, 1)
            chunk = max(1, CHUNK_PIXELS // (height * width * tile * tile))
            order = np.arange(height * width).reshape(height, width)
            case_compared = 0
            for pad in case.pads:
                for start in range(0, len(case.content), chunk):
                    content = case.content[start:start + chunk]
                    planes = finish_planes(self.art, self.global_planes(content, pad))
                    base = base_index(self.art, planes)
                    keys = self.cell_keys(content, pad)
                    for rotation in range(rotations):
                        if case.variants is not None:
                            variants = case.variants[start:start + chunk]
                        else:
                            variants = np.broadcast_to((order + rotation) % count, keys.shape)
                        expected = compose(self.art, planes, variants, base, as_packed=True)
                        actual = self.assemble(keys, variants, tiles)
                        bad = (expected != actual) & mask
                        case_compared += int(mask.sum()) * len(content)
                        bad_count = int(bad.sum())
                        if bad_count:
                            mismatched += bad_count
                            if len(examples) < 5:
                                b, y, x = (int(v) for v in np.argwhere(bad)[0])
                                examples.append({"case": case.name, "pad": pad, "configuration": start + b,
                                                 "pixel": [x, y], "variant_pass": rotation})
            compared += case_compared
            cases.append({"case": case.name, "configurations": int(len(case.content)), "pads": list(case.pads),
                          "variant_passes": rotations, "pixels_compared": case_compared})
        return {"pixels_compared": compared, "mismatches": mismatched, "cases": cases, "examples": examples}


class WangBuilder(Builder):
    kind = "wang_corner"

    def make_keys(self) -> list:
        return [tuple(key) for key in itertools.product(range(self.art.n), repeat=4)]   # (tl, tr, bl, br)

    def tile_planes(self) -> Planes:
        # Every pixel within `radius` px around the tile is evaluated from the tile's own four
        # corners; the plateau makes that exact for look-ups up to `plateau` px.
        art, tile, radius = self.art, self.art.tile, self.art.radius
        keys = np.array(self.keys, np.int8)
        span = np.arange(-radius, tile + radius)
        mat = strongest([keys[:, i, None, None] for i in range(4)], art.corner_terms(span, span),
                        art.wobble_factors(span, span))
        band = compute_bands(art, mat)
        inner = slice(radius, radius + tile)
        return Planes(mat[:, inner, inner], band[:, inner, inner])

    def global_planes(self, content: np.ndarray, pad: bool = False) -> Planes:
        # Every pixel uses the corners of the cell it lies in; beyond the map the vertex grid
        # repeats its border (the map continues as it ends).
        art, tile, radius = self.art, self.art.tile, self.art.radius
        height, width = content.shape[1] - 1, content.shape[2] - 1
        lattice = np.pad(content, ((0, 0), (1, 1), (1, 1)), mode="edge")
        gy, gx = np.arange(-radius, height * tile + radius), np.arange(-radius, width * tile + radius)
        cy, cx = gy // tile, gx // tile
        rows, cols = (cy + 1)[:, None], (cx + 1)[None, :]
        corners = [lattice[:, rows, cols], lattice[:, rows, cols + 1],
                   lattice[:, rows + 1, cols], lattice[:, rows + 1, cols + 1]]
        mat = strongest(corners, art.corner_terms(gy - cy * tile, gx - cx * tile), art.wobble_factors(gy, gx))
        band = compute_bands(art, mat)
        inner_y, inner_x = slice(radius, radius + height * tile), slice(radius, radius + width * tile)
        return Planes(mat[:, inner_y, inner_x], band[:, inner_y, inner_x])

    def cell_keys(self, content: np.ndarray, pad: bool = False) -> np.ndarray:
        n = self.art.n
        lattice = content.astype(np.int32)
        return ((lattice[:, :-1, :-1] * n + lattice[:, :-1, 1:]) * n + lattice[:, 1:, :-1]) * n + lattice[:, 1:, 1:]

    def tile_fields(self, rank: int) -> dict:
        tl, tr, bl, br = self.keys[rank]
        return {"wang": [tl, tr, bl, br], "wangid": [0, tr + 1, 0, br + 1, 0, bl + 1, 0, tl + 1]}

    def proof_cases(self) -> list[ProofCase]:
        n = self.art.n
        lattices = np.stack(np.unravel_index(np.arange(n ** 9), (n,) * 9), -1).reshape(-1, 3, 3).astype(np.int8)
        return [ProofCase("every 2x2 tile block (all 3x3 corner lattices)", lattices, np.ones((2, 2), bool))]

    def random_content(self, rng: np.random.Generator, width: int, height: int) -> np.ndarray:
        return rng.integers(0, self.art.n, (1, height + 1, width + 1)).astype(np.int8)

    def uniform_content(self, material: int, width: int, height: int) -> np.ndarray:
        return np.full((1, height + 1, width + 1), material, np.int8)

    def wangset(self) -> dict:
        return {"type": "corner", "colors": _wang_colours(self.art)}


class _OccupancyBuilder(Builder):
    """Shared parts of the occupancy kinds (blob47, bevel): keys, windows, content."""

    def canonical(self, masks: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def make_keys(self) -> list:
        return sorted({int(k) for k in np.ravel(self.canonical(np.arange(256)))})

    def cell_keys(self, content: np.ndarray, pad: bool = False) -> np.ndarray:
        lookup = np.full(256, -1, np.int32)
        lookup[self.keys] = np.arange(len(self.keys))
        return np.where(content, lookup[self.canonical(_neighbour_masks(content, pad))], -1)

    def proof_cases(self) -> list[ProofCase]:
        cases = []
        for name, shape, fixed in (("every 3x3 neighbourhood", (3, 3), [(1, 1)]),
                                   ("every horizontal pair with its neighbourhood", (3, 4), [(1, 1), (1, 2)]),
                                   ("every vertical pair with its neighbourhood", (4, 3), [(1, 1), (2, 1)])):
            free = [cell for cell in itertools.product(range(shape[0]), range(shape[1])) if cell not in fixed]
            patterns = (np.arange(2 ** len(free))[:, None] >> np.arange(len(free))) & 1
            content = np.zeros((len(patterns),) + shape, bool)
            for column, (y, x) in enumerate(free):
                content[:, y, x] = patterns[:, column].astype(bool)
            compare = np.zeros(shape, bool)
            for y, x in fixed:
                content[:, y, x] = True
                compare[y, x] = True
            cases.append(ProofCase(name, content, compare, pads=(False, True)))
        return cases

    def random_content(self, rng: np.random.Generator, width: int, height: int) -> np.ndarray:
        return rng.random((1, height, width)) < 0.55

    def uniform_content(self, material: int, width: int, height: int) -> np.ndarray:
        return np.ones((1, height, width), bool)


class BlobBuilder(_OccupancyBuilder):
    kind = "blob47"

    def canonical(self, masks: np.ndarray) -> np.ndarray:
        return canonical_blob(masks)

    def tile_planes(self) -> Planes:
        # The tile and its 8 neighbours from the canonical key; cells two away are assumed
        # occupied (they never reach the ring the tile reads; the proof checks that).
        art, tile, radius = self.art, self.art.tile, self.art.radius
        count = len(self.keys)
        masks = np.array(self.keys, np.int32)
        occupancy = np.ones((count, 5, 5), bool)
        for bit, (dy, dx) in enumerate(BLOB_OFFSETS):
            occupancy[:, 2 + dy, 2 + dx] = (masks >> bit) & 1
        depths = art.margin_depths(1, 1)[0, 0][None]
        cells = np.zeros((count, 3, 3, tile, tile), bool)
        for cy, cx in itertools.product(range(3), repeat=2):
            neighbours = np.stack([occupancy[:, 1 + cy + dy, 1 + cx + dx] for dy, dx in BLOB_OFFSETS], -1)
            cells[:, cy, cx] = (blob_inside(neighbours, np.broadcast_to(depths, (count, 4, tile)), tile)
                                & occupancy[:, 1 + cy, 1 + cx, None, None])
        window = _to_canvas(cells)[:, tile - radius:2 * tile + radius, tile - radius:2 * tile + radius]
        mat = window.astype(np.int8)
        band = compute_bands(art, mat)
        inner = slice(radius, radius + tile)
        return Planes(mat[:, inner, inner], band[:, inner, inner])

    def global_planes(self, content: np.ndarray, pad: bool = False) -> Planes:
        art, tile, radius = self.art, self.art.tile, self.art.radius
        batch, height, width = content.shape
        masks = _neighbour_masks(content, pad).reshape(-1)
        bits = ((masks[:, None] >> np.arange(8)) & 1).astype(bool)
        depths = np.broadcast_to(art.margin_depths(height, width)[None], (batch, height, width, 4, tile))
        inside = blob_inside(bits, depths.reshape(-1, 4, tile), tile).reshape(batch, height, width, tile, tile)
        inside &= content[..., None, None]
        mat = np.pad(_to_canvas(inside), ((0, 0), (radius, radius), (radius, radius))).astype(np.int8)
        band = compute_bands(art, mat)
        inner_y, inner_x = slice(radius, radius + height * tile), slice(radius, radius + width * tile)
        return Planes(mat[:, inner_y, inner_x], band[:, inner_y, inner_x])

    def tile_fields(self, rank: int) -> dict:
        mask = self.keys[rank]
        return {"blob_mask": mask, "wangid": [2 if mask >> bit & 1 else 1 for bit in range(8)]}

    def wangset(self) -> dict:
        return {"type": "mixed", "colors": _wang_colours(self.art)}


class BevelBuilder(_OccupancyBuilder):
    kind = "bevel"

    def canonical(self, masks: np.ndarray) -> np.ndarray:
        return np.asarray(masks, np.int32) & 0b01010101   # sides only: N, E, S, W

    def _planes(self, solid: np.ndarray, crop: tuple[slice, slice]) -> Planes:
        face = bevel_faces(self.art, solid)[(slice(None),) + crop]
        return Planes(np.where(solid[(slice(None),) + crop], 0, -1).astype(np.int8), face=face)

    def tile_planes(self) -> Planes:
        tile, radius = self.art.tile, self.art.radius
        masks = np.array(self.keys, np.int32)
        size = tile + 2 * radius
        solid = np.zeros((len(masks), size, size), bool)
        core = slice(radius, radius + tile)
        solid[:, core, core] = True
        for bit, region in ((0, (slice(0, radius), core)), (2, (core, slice(radius + tile, size))),
                            (4, (slice(radius + tile, size), core)), (6, (core, slice(0, radius)))):
            solid[(slice(None),) + region] |= ((masks >> bit) & 1).astype(bool)[:, None, None]
        return self._planes(solid, (core, core))

    def global_planes(self, content: np.ndarray, pad: bool = False) -> Planes:
        tile, radius = self.art.tile, self.art.radius
        height, width = content.shape[1:]
        solid = np.repeat(np.repeat(content, tile, axis=1), tile, axis=2)
        solid = np.pad(solid, ((0, 0), (radius, radius), (radius, radius)))
        return self._planes(solid, (slice(radius, radius + height * tile), slice(radius, radius + width * tile)))

    def tile_fields(self, rank: int) -> dict:
        mask = self.keys[rank]
        return {"blob_mask": mask,
                "wangid": [(2 if mask >> bit & 1 else 1) if bit % 2 == 0 else 0 for bit in range(8)]}

    def wangset(self) -> dict:
        return {"type": "edge", "colors": [{"name": "open", "color": "#000000"}] + _wang_colours(self.art)}


class FlatBuilder(Builder):
    kind = "flat"

    def make_keys(self) -> list:
        return [0]

    def tile_planes(self) -> Planes:
        return Planes(np.zeros((1, self.art.tile, self.art.tile), np.int8))

    def global_planes(self, content: np.ndarray, pad: bool = False) -> Planes:
        batch, height, width = content.shape
        return Planes(np.zeros((batch, height * self.art.tile, width * self.art.tile), np.int8))

    def cell_keys(self, content: np.ndarray, pad: bool = False) -> np.ndarray:
        return np.where(content, 0, -1)

    def tile_fields(self, rank: int) -> dict:
        return {}

    def proof_cases(self) -> list[ProofCase]:
        count = self.art.variants
        variants = np.stack(np.unravel_index(np.arange(count ** 4), (count,) * 4), -1).reshape(-1, 2, 2)
        content = np.ones((len(variants), 2, 2), bool)
        return [ProofCase("every 2x2 block of variants", content, np.ones((2, 2), bool), variants=variants)]

    def random_content(self, rng: np.random.Generator, width: int, height: int) -> np.ndarray:
        return np.ones((1, height, width), bool)

    def uniform_content(self, material: int, width: int, height: int) -> np.ndarray:
        return np.ones((1, height, width), bool)


BUILDERS = {"wang_corner": WangBuilder, "blob47": BlobBuilder, "bevel": BevelBuilder, "flat": FlatBuilder}


def _wang_colours(art: SetArt) -> list[dict]:
    return [{"name": m.name, "color": _hex(m.ramp[len(m.ramp) // 2])} for m in art.materials]


# ----------------------------------------------------------------------------- metrics

def _premultiplied(rgba: np.ndarray) -> np.ndarray:
    pixels = rgba.astype(np.float64)
    return pixels[..., :3] * (pixels[..., 3:4] / 255.0)


def _ratio(seam: float, inner: float) -> float | None:
    if inner > 1e-9:
        return round(seam / inner, 4)
    return 0.0 if seam <= 1e-9 else None


def seam_metric(rgba: np.ndarray, mask: np.ndarray | None, tile: int) -> dict:
    """Material flips and premultiplied colour steps across tile seams versus inside tiles.

    Ratios near or below 1 mean seams look like any other pixel boundary; well above 1 means
    the tiles disagree where they meet (for example an image texture that does not wrap)."""
    colour = _premultiplied(rgba)
    step_x = np.abs(np.diff(colour, axis=1)).mean(axis=(0, 2))
    step_y = np.abs(np.diff(colour, axis=0)).mean(axis=(1, 2))
    seam_x = (np.arange(step_x.size) + 1) % tile == 0
    seam_y = (np.arange(step_y.size) + 1) % tile == 0

    def split(x_values: np.ndarray, y_values: np.ndarray) -> tuple[float, float]:
        return (float(np.concatenate([x_values[seam_x], y_values[seam_y]]).mean()),
                float(np.concatenate([x_values[~seam_x], y_values[~seam_y]]).mean()))

    rgb_seam, rgb_inner = split(step_x, step_y)
    report = {"rgb_step_seam": round(rgb_seam, 4), "rgb_step_interior": round(rgb_inner, 4),
              "rgb_ratio": _ratio(rgb_seam, rgb_inner)}
    if mask is not None:
        flip_seam, flip_inner = split((mask[:, 1:] != mask[:, :-1]).mean(axis=0), (mask[1:] != mask[:-1]).mean(axis=1))
        report.update({"mask_flip_seam": round(flip_seam, 5), "mask_flip_interior": round(flip_inner, 5),
                       "mask_ratio": _ratio(flip_seam, flip_inner)})
    return report


def repetition_index(rgba: np.ndarray, tile: int) -> tuple[float, float]:
    """Luminance correlation of an area with itself shifted one tile right and one tile down
    (1 = wallpaper repetition, 0 = none). A flat area has nothing to repeat and scores 0."""
    luma = _premultiplied(rgba) @ np.array([0.299, 0.587, 0.114])

    def correlation(a: np.ndarray, b: np.ndarray) -> float:
        a, b = a.ravel() - a.mean(), b.ravel() - b.mean()
        scale = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
        return float((a * b).sum()) / scale if scale > 1e-9 else 0.0

    return correlation(luma[:, :-tile], luma[:, tile:]), correlation(luma[:-tile], luma[tile:])


def texture_wrap(rgb: np.ndarray) -> dict:
    """Does a one-tile texture wrap? Its steps across the wrap edges against its inside steps (p95)."""
    pixels = rgb.astype(np.float64)
    inner = np.concatenate([np.abs(np.diff(pixels, axis=1)).mean(axis=(0, 2)),
                            np.abs(np.diff(pixels, axis=0)).mean(axis=(1, 2))])
    wrap = max(float(np.abs(pixels[:, 0] - pixels[:, -1]).mean()), float(np.abs(pixels[0] - pixels[-1]).mean()))
    p95 = float(np.percentile(inner, 95))
    return {"wrap_step": round(wrap, 4), "interior_p95": round(p95, 4), "ratio": round(wrap / max(p95, 1.0), 4)}


def collision_rects(blocked: np.ndarray, cell: int) -> list[dict]:
    """Blocked tile pixels at `cell` px resolution (half or more blocked), merged greedily into rects."""
    tile = blocked.shape[0]
    grid = blocked.reshape(tile // cell, cell, tile // cell, cell).mean(axis=(1, 3)) >= 0.5
    used = np.zeros_like(grid)
    rects = []
    for y in range(grid.shape[0]):
        x = 0
        while x < grid.shape[1]:
            if grid[y, x] and not used[y, x]:
                x1 = x
                while x1 < grid.shape[1] and grid[y, x1] and not used[y, x1]:
                    x1 += 1
                y1 = y + 1
                while y1 < grid.shape[0] and grid[y1, x:x1].all() and not used[y1, x:x1].any():
                    y1 += 1
                used[y:y1, x:x1] = True
                rects.append({"shape": "rect", "x": x * cell, "y": y * cell, "w": (x1 - x) * cell,
                              "h": (y1 - y) * cell})
                x = x1
            else:
                x += 1
    return rects


# ----------------------------------------------------------------------------- evaluation

def _check_entry(check_id: str, status: str, value: Any, threshold: Any, note: str | None = None) -> dict:
    entry = {"id": check_id, "status": status, "value": value, "threshold": threshold}
    if note:
        entry["note"] = note
    return entry


def _seam_mask(builder: Builder, planes: Planes, rgba: np.ndarray) -> np.ndarray | None:
    """The class map the seam metric compares: Wang materials, blob coverage, nothing for flat sets."""
    if builder.kind == "wang_corner":
        return planes.mat[0]
    if builder.kind == "blob47":
        return rgba[0, ..., 3] > 0
    return None


@dataclass
class SampleMap:
    """The seeded random map shared by the random-map proof, the control and the seam metric."""

    content: np.ndarray
    variants: np.ndarray
    keys: np.ndarray
    expected: np.ndarray     # global render
    actual: np.ndarray       # assembled from the atlas tiles
    planes: Planes           # global planes


def _sample_map(builder: Builder, tiles: np.ndarray, seed: int) -> SampleMap:
    rng = np.random.default_rng(derive_seed(seed, "proof-map", builder.art.spec.id))
    width, height = PROOF_MAP
    content = builder.random_content(rng, width, height)
    variants = rng.integers(0, builder.art.variants, (1, height, width))
    expected, planes = builder.render_global(content, variants)
    keys = builder.cell_keys(content)
    return SampleMap(content, variants, keys, expected, builder.assemble(keys, variants, tiles), planes)


def _proof_checks(builder: Builder, tiles: np.ndarray, sample: SampleMap, *, skip_proof: bool,
                  strict: bool) -> tuple[list[dict], dict, bool]:
    """The exhaustive seam proof (unless skipped or too large) plus the random map: checks, the
    combined proof record and whether the set is seamless_verified."""
    set_id = builder.art.spec.id
    proof: dict[str, Any] = {"pixels_compared": 0, "mismatches": 0}
    size = builder.proof_size()
    ran = not skip_proof and size <= EXHAUSTIVE_PIXEL_LIMIT
    if ran:
        proof = builder.prove(tiles)
        checks = [_check_entry(f"{set_id}/seam_proof", "pass" if proof["mismatches"] == 0 else "fail",
                               {"pixels_compared": proof["pixels_compared"], "mismatches": proof["mismatches"]}, 0)]
    else:
        reason = ("--skip-seam-proof: tiles were not compared with the global render" if skip_proof else
                  f"the exhaustive proof needs {size} pixel comparisons (limit {EXHAUSTIVE_PIXEL_LIMIT}); "
                  "use fewer variants or smaller tiles")
        checks = [_check_entry(f"{set_id}/seam_proof", "fail" if strict else "warn", None, 0,
                               reason + ("; strict mode requires the seam proof" if strict else ""))]
    mismatches = int((sample.expected != sample.actual).any(-1).sum())
    width, height = PROOF_MAP
    checks.append(_check_entry(f"{set_id}/random_map", "pass" if mismatches == 0 else "fail",
                               {"map": f"{width}x{height}", "mismatches": mismatches}, 0))
    proof["pixels_compared"] += int(sample.expected.shape[1] * sample.expected.shape[2])
    proof["mismatches"] += mismatches
    proof["random_map"] = {"size": [width, height], "mismatches": mismatches}
    if ran:
        checks[0]["note"] = proof_method(proof)
    return checks, proof, ran and proof["mismatches"] == 0


def _control_check(builder: Builder, sample: SampleMap, metrics: dict) -> dict:
    """The same set with its noise detuned by half a cycle per tile must fail the comparison."""
    set_id = builder.art.spec.id
    control_art = builder.art.control()
    if control_art is None:
        return _check_entry(f"{set_id}/nonperiodic_control", "skipped", None, "> 0",
                            "the set has no periodic noise to detune (flat fills, fixed margins)")
    control = type(builder)(control_art)
    expected, _ = control.render_global(sample.content, sample.variants)
    actual = control.assemble(sample.keys, sample.variants, control.render_tiles())
    mismatches = int((expected != actual).any(-1).sum())
    metrics["nonperiodic_control"] = {"mismatches": mismatches,
                                      "seam_metric": seam_metric(actual[0], None, builder.art.tile)}
    return _check_entry(f"{set_id}/nonperiodic_control", "pass" if mismatches else "fail", mismatches, "> 0",
                        "a copy whose noise is detuned by half a cycle per tile (so it no longer wraps) must differ "
                        "from its own global render; it " + ("does" if mismatches else "does not: the proof is blind"))


def _seam_metric_checks(builder: Builder, tiles: np.ndarray, sample: SampleMap, seed: int, max_seam_ratio: float,
                        metrics: dict) -> list[dict]:
    """Seam metric on the random map (it catches fills that do not wrap, such as image textures).
    Bevel block borders are tile borders by design, so bevel sets are measured inside a solid area."""
    art, tile, set_id = builder.art, builder.art.tile, builder.art.spec.id
    if builder.kind == "bevel":
        width, height = PROOF_MAP
        area = builder.uniform_content(0, width + 2, height + 2)
        variants = np.random.default_rng(derive_seed(seed, "solid-area", set_id)).integers(0, art.variants, area.shape)
        mask, image = None, builder.assemble(builder.cell_keys(area), variants, tiles)[0, tile:-tile, tile:-tile]
    else:
        mask, image = _seam_mask(builder, sample.planes, sample.actual), sample.actual[0]
    metric = metrics["seam_metric"] = seam_metric(image, mask, tile)
    checks = []
    if mask is not None:
        ratio = metric["mask_ratio"]
        checks.append(_check_entry(f"{set_id}/seam_metric_mask",
                                   "pass" if ratio is not None and ratio <= max_seam_ratio else "fail",
                                   ratio, max_seam_ratio, "material flips across seams over flips inside tiles"))
    ratio = metric["rgb_ratio"]
    checks.append(_check_entry(f"{set_id}/seam_metric_rgb",
                               "pass" if ratio is not None and ratio <= max_seam_ratio else "warn",
                               ratio, max_seam_ratio, "colour steps across seams over steps inside tiles"
                               + (", inside a solid area" if builder.kind == "bevel" else "")))
    return checks


def _repetition_check(builder: Builder, tiles: np.ndarray, seed: int, max_repetition: float | None,
                      metrics: dict) -> dict:
    """Repetition of single-material areas with uniformly picked variants: the mean over
    REPETITION_SAMPLES seeded arrangements of a 13x13 area, worst material and direction."""
    art, tile, set_id = builder.art, builder.art.tile, builder.art.spec.id
    repetition = {}
    for material, painted in enumerate(art.painted):
        if not painted:
            continue
        area = REPETITION_AREA + 2   # one border cell on each side is cropped away (blob/bevel map edges)
        keys = builder.cell_keys(builder.uniform_content(material, area, area))
        rng = np.random.default_rng(derive_seed(seed, "repetition", set_id, material))
        samples = [repetition_index(builder.assemble(keys, rng.integers(0, art.variants, keys.shape), tiles)
                                    [0, tile:-tile, tile:-tile], tile) for _ in range(REPETITION_SAMPLES)]
        horizontal, vertical = np.mean(samples, axis=0)
        worst = [max(sample) for sample in samples]
        repetition[art.materials[material].name] = {
            "horizontal": round(float(horizontal), 4), "vertical": round(float(vertical), 4),
            "sample_range": [round(float(min(worst)), 4), round(float(max(worst)), 4)]}
    index = max(max(v["horizontal"], v["vertical"]) for v in repetition.values())
    metrics["repetition"] = repetition
    metrics["repetition_index"] = round(max(index, 0.0), 4)
    if max_repetition is not None:
        status, threshold = ("pass" if index <= max_repetition else "fail"), max_repetition
    else:
        status, threshold = ("pass" if index <= REPETITION_TARGET else "warn"), REPETITION_TARGET
    return _check_entry(f"{set_id}/repetition_index", status, metrics["repetition_index"], threshold,
                        f"one-tile-shift luminance correlation of a {REPETITION_AREA}x{REPETITION_AREA} "
                        f"single-material area (larger of right and down shifts, worst material), mean of "
                        f"{REPETITION_SAMPLES} arrangements with uniformly picked variants")


def _structure_checks(builder: Builder, tiles: np.ndarray, atlas: np.ndarray, metrics: dict) -> list[dict]:
    """Interior-only variants, distinct topology tiles, and the alpha and palette census of the atlas."""
    art, tile, set_id = builder.art, builder.art.tile, builder.art.spec.id
    ring = np.ones((tile, tile), bool)
    ring[1:-1, 1:-1] = False
    ring_differences = int((tiles[:, :, ring] != tiles[:1, :, ring]).any(axis=(0, 2, 3)).sum())
    distinct = len({tiles[0, k].tobytes() for k in range(len(builder.keys))})
    distinct_status = "pass" if distinct == len(builder.keys) else ("warn" if builder.kind == "wang_corner" else "fail")
    census = codeart_core.qa_pixels(atlas, palette=None if art.image_rgb else list(art.palette.values()))
    metrics["colors"] = census["colors"]
    checks = [
        _check_entry(f"{set_id}/outer_rings_identical", "pass" if ring_differences == 0 else "fail",
                     ring_differences, 0, "keys whose variants differ on the 1 px outer ring"),
        _check_entry(f"{set_id}/distinct_tiles", distinct_status, distinct, len(builder.keys)),
        _check_entry(f"{set_id}/partial_alpha", "pass" if census["partial_alpha"] == 0 else "fail",
                     census["partial_alpha"], 0),
    ]
    if art.image_rgb:
        checks.append(_check_entry(f"{set_id}/off_palette", "skipped", None, 0,
                                   "an image texture is used as given; its colours are not ramp colours"))
    else:
        checks.append(_check_entry(f"{set_id}/off_palette", "pass" if census["off_palette"] == 0 else "fail",
                                   census["off_palette"], 0))
    return checks


def evaluate_set(builder: Builder, spec: Spec, *, skip_proof: bool, strict: bool, max_repetition: float | None,
                 max_seam_ratio: float) -> dict:
    """Render, prove and measure one tileset. Returns tiles, planes, the atlas, checks and metrics."""
    art = builder.art
    planes = builder.tile_planes()
    tiles = builder.render_tiles(planes)
    metrics: dict[str, Any] = {"kind": art.spec.kind, "materials": list(art.spec.materials),
                               "tiles": len(builder.keys) * art.variants, "keys": len(builder.keys),
                               "variants": art.variants}
    if art.spec.kind == "wang_corner":
        metrics["plateau"] = art.spec.plateau
    sample = _sample_map(builder, tiles, spec.seed)
    checks, proof, seamless = _proof_checks(builder, tiles, sample, skip_proof=skip_proof, strict=strict)
    checks.append(_control_check(builder, sample, metrics))
    checks += _seam_metric_checks(builder, tiles, sample, spec.seed, max_seam_ratio, metrics)
    checks.append(_repetition_check(builder, tiles, spec.seed, max_repetition, metrics))
    atlas = make_atlas(tiles, columns_for(builder))
    checks += _structure_checks(builder, tiles, atlas, metrics)
    metrics["seam_proof"] = proof
    return {"builder": builder, "tiles": tiles, "planes": planes, "atlas": atlas, "checks": checks,
            "metrics": metrics, "proof": proof, "seamless": seamless}


def columns_for(builder: Builder) -> int:
    return {"wang_corner": 16 if builder.art.n == 2 else 9, "blob47": 8, "bevel": 4,
            "flat": min(8, builder.art.variants)}[builder.kind]


def make_atlas(tiles: np.ndarray, columns: int) -> np.ndarray:
    """Tiles in index order (variant * keys + key rank), `columns` per row."""
    count, keys, tile = tiles.shape[0], tiles.shape[1], tiles.shape[2]
    flat = tiles.reshape(count * keys, tile, tile, 4)
    rows = -(-len(flat) // columns)
    atlas = np.zeros((rows * tile, columns * tile, 4), np.uint8)
    for index, image in enumerate(flat):
        y, x = divmod(index, columns)
        atlas[y * tile:(y + 1) * tile, x * tile:(x + 1) * tile] = image
    return atlas


# ----------------------------------------------------------------------------- preview

def _smooth_field(rng: np.random.Generator, height: int, width: int, cell: float = 4.0) -> np.ndarray:
    lattice = rng.random((int(height / cell) + 3, int(width / cell) + 3))
    ys, xs = np.mgrid[0:height, 0:width] / cell
    y0, x0 = ys.astype(int), xs.astype(int)
    ty, tx = ys - y0, xs - x0
    sy, sx = ty * ty * (3 - 2 * ty), tx * tx * (3 - 2 * tx)
    top = lattice[y0, x0] * (1 - sx) + lattice[y0, x0 + 1] * sx
    bottom = lattice[y0 + 1, x0] * (1 - sx) + lattice[y0 + 1, x0 + 1] * sx
    return top * (1 - sy) + bottom * sy


def _walk(rng: np.random.Generator, height: int, width: int, *, vertical: bool = False) -> np.ndarray:
    """A 4-connected meandering path of cells across a height x width grid."""
    long_, short = (height, width) if vertical else (width, height)
    path = np.zeros((short, long_), bool)
    position = int(rng.integers(short // 4, max(short // 4 + 1, 3 * short // 4)))
    for step_along in range(long_):
        path[position, step_along] = True
        step = int(rng.choice((-1, 1)))
        if rng.random() < 0.35 and 1 <= position + step < short - 1:
            position += step
            path[position, step_along] = True
    return path.T if vertical else path


def preview_map(results: list[dict], spec: Spec, width: int, height: int) -> np.ndarray:
    """A seeded sample map, assembled from the atlas tiles only: the first Wang (or flat) set as
    ground, with a road of its third material when it has three (so the road meets water); the
    first blob set as a path; the first bevel set as two blocks."""
    tile = spec.tile
    rng = np.random.default_rng(derive_seed(spec.seed, "preview"))
    canvas = np.zeros((height * tile, width * tile, 4), np.uint8)
    by_kind: dict[str, dict] = {}
    for result in results:
        by_kind.setdefault(result["builder"].kind, result)
    path = _walk(rng, height, width)[None]

    def layer(result: dict, content: np.ndarray) -> np.ndarray:
        builder = result["builder"]
        keys = builder.cell_keys(content)
        variants = rng.integers(0, builder.art.variants, keys.shape)
        return builder.assemble(keys, variants, result["tiles"])[0]

    ground = by_kind.get("wang_corner") or by_kind.get("flat")
    if ground is not None:
        builder = ground["builder"]
        if builder.kind == "wang_corner":
            value = _smooth_field(rng, height + 1, width + 1)
            cuts = np.quantile(value, [0.4] if builder.art.n == 2 else [0.3, 0.75])
            lattice = np.digitize(value, cuts).astype(np.int8)[None]
            if builder.art.n == 3:
                for py, px in zip(*np.nonzero(_walk(rng, height, width, vertical=True))):
                    lattice[0, py:py + 2, px:px + 2] = 2
            blob = by_kind.get("blob47")
            if blob is not None and blob["builder"].art.spec.materials[0] in builder.art.spec.materials:
                under = builder.art.spec.materials.index(blob["builder"].art.spec.materials[0])
                for py, px in zip(*np.nonzero(path[0])):
                    lattice[0, py:py + 2, px:px + 2] = under
            canvas = layer(ground, lattice)
        else:
            canvas = layer(ground, np.ones((1, height, width), bool))
    if "blob47" in by_kind:
        over = layer(by_kind["blob47"], path)
        canvas = np.where(over[..., 3:4] > 0, over, canvas)
    if "bevel" in by_kind:
        blocks = np.zeros((1, height, width), bool)
        for _ in range(2):
            block_h, block_w = int(rng.integers(2, 4)), int(rng.integers(2, 5))
            top, left = int(rng.integers(0, max(1, height - block_h))), int(rng.integers(0, max(1, width - block_w)))
            blocks[0, top:top + block_h, left:left + block_w] = True
        over = layer(by_kind["bevel"], blocks)
        canvas = np.where(over[..., 3:4] > 0, over, canvas)
    return canvas


# ----------------------------------------------------------------------------- outputs

def _file_ref(path: Path, base: Path) -> dict:
    """fileRef for an output inside the output folder (relative POSIX path, sha256, bytes; D30)."""
    return forge_core.file_ref(path, base)


def _input_ref(path: Path, base: Path, sha256: str) -> dict:
    """fileRef for an input: relative to the output folder when possible, else its file name (an input
    on another drive is recorded by name and sha256, never by absolute path; forge_core.file_ref, D30)."""
    return forge_core.file_ref(path, base, sha256=sha256)


def proof_method(proof: dict) -> str:
    """The seam_proof.method text: what was compared and over which cases."""
    cases = [f"{c['case']} ({c['configurations']} configurations, {c['variant_passes']} variant passes"
             + (", cells two away all empty and all full" if len(c["pads"]) > 1 else "") + ")"
             for c in proof.get("cases", [])]
    cases.append(f"a seeded random {PROOF_MAP[0]}x{PROOF_MAP[1]} map")
    return ("Tiles assembled from the atlas by key, compared pixel for pixel (RGBA) with an independent global "
            "render: noise evaluated at map coordinates, every pixel classified from its own cell, band and face "
            "look-ups read at their true positions across tile edges. Cases: " + "; ".join(cases) + ".")


def tileset_manifest(result: dict, spec: Spec, image_name: str, image_path: Path, qa_ref: dict) -> dict:
    """generate2dmap.tileset.v1 for one set (extra fields: id, tilecount, variants, wangset, plateau,
    art_source, generator, spec_sha256, qa, and per tile wangid). qa is a fileRef of autotile-qa.json
    beside the manifest (D5): a consumer that copies the manifest elsewhere rewrites or drops it."""
    builder, art = result["builder"], result["builder"].art
    blocked = builder.blocked(result["planes"])
    tiles = []
    for variant in range(art.variants):
        for rank in range(len(builder.keys)):
            tiles.append({"index": variant * len(builder.keys) + rank, "variant": variant,
                          **builder.tile_fields(rank),
                          "collision": collision_rects(blocked[rank], spec.collision_cell),
                          "properties": {"walkable": bool(float(blocked[rank].mean()) < 0.5)}})
    proof = result["proof"]
    manifest = {
        "schema": TILESET_SCHEMA,
        "id": art.spec.id,
        "image": image_name,
        "sha256": forge_core.sha256_file(image_path),
        "tile_size": art.tile,
        "columns": columns_for(builder),
        "tilecount": len(tiles),
        "kind": art.spec.kind,
        "materials": list(art.spec.materials),
        "variants": art.variants,
        "seamless_verified": bool(result["seamless"]),
        "seam_proof": {"method": proof_method(proof), "pixels_compared": int(proof["pixels_compared"]),
                       "mismatches": int(proof["mismatches"])},
        "repetition_index": result["metrics"]["repetition_index"],
    }
    wangset = builder.wangset()
    if wangset is not None:
        manifest["wangset"] = wangset
    if art.spec.kind == "wang_corner":
        manifest["plateau"] = art.spec.plateau
    manifest.update({"art_source": "code", "generator": {"name": TOOL_NAME, "version": TOOL_VERSION},
                     "spec_sha256": spec.sha256, "qa": qa_ref, "tiles": tiles})
    return manifest


NOT_PROVEN = [
    "How the tiles look at game size: open review.png and the per-set review sheets.",
    "The global render shares the corner-weight formula, band rules and texture code with the tile renderer; the "
    "proof shows that tiles assembled by key reproduce that map exactly, including look-ups across tile edges and "
    "noise at map coordinates, not that the art is pleasant.",
    "Image textures are sampled per tile in both renders; whether they wrap is measured (texture_wrap and the "
    "seam metric), not proven.",
    "Variants enter the exhaustive proof in rotations (every variant at every block position), not every "
    "combination of variants per block (flat sets do enumerate them); variants only change marks inside the "
    "outer ring, which no look-up reads.",
    "Editor behaviour (Tiled terrain brushes, Godot or LDtk import) is not exercised here; wangid values follow "
    "Tiled's documented order but were not opened in the editor.",
    "Collision rectangles approximate blocked pixels at the collision_cell resolution.",
]
NOT_PROVEN_OCCUPANCY = ("Blob and bevel cells two cells away from a tile were tested all empty and all full, not "
                        "enumerated.")


def build(args: argparse.Namespace) -> dict:
    """Validate, render, prove and measure every selected set, then publish (or fail strictly)."""
    output = Path(args.output_dir)
    if os.path.lexists(output):
        raise FileExistsError(errno.EEXIST, "Refusing to replace existing output", str(output))
    spec = load_spec(Path(args.material_spec), kinds=KIND_SELECTIONS[args.kind], tile_size=args.tile_size,
                     variants=args.variants, seed=args.seed, textures=dict(args.material_texture or []),
                     quantize=args.quantize_textures)
    results = [evaluate_set(BUILDERS[s.kind](build_art(spec, s)), spec, skip_proof=args.skip_seam_proof,
                            strict=args.strict_qc, max_repetition=args.max_repetition,
                            max_seam_ratio=args.max_seam_ratio) for s in spec.sets]
    checks = [check for result in results for check in result["checks"]]
    not_proven = list(NOT_PROVEN)
    if any(s.kind in ("blob47", "bevel") for s in spec.sets):
        not_proven.append(NOT_PROVEN_OCCUPANCY)
    used = {name for s in spec.sets for name in s.materials}
    for material in spec.materials.values():
        texture = material.texture
        if texture.image is None or material.name not in used:
            continue
        wrap = texture_wrap(texture.image)
        checks.append(_check_entry(f"texture_wrap/{material.name}",
                                   "pass" if wrap["ratio"] <= TEXTURE_WRAP_LIMIT else "fail", wrap,
                                   TEXTURE_WRAP_LIMIT, "an image texture must wrap: the step across its wrap edges "
                                   "over the 95th percentile of its inside steps"))
        if not texture.quantize:
            not_proven.append(f"The colours of the {material.name} image texture are used as given, not checked "
                              "against its ramp.")
    status = max((c["status"] for c in checks), key=QA_SEVERITY.__getitem__)
    status = "pass" if status == "skipped" else status
    failed = [c["id"] for c in checks if c["status"] == "fail"]
    if args.strict_qc and failed:
        raise QCFailure("strict QC failed: " + ", ".join(failed))
    manifests: list[Path] = []
    with forge_core.staged_output(output) as stage:
        # Order (D5): the art and the review sheets, then autotile-qa.json over them, then the manifests,
        # whose qa is a fileRef (sha256) of that QA file, then codeart-meta.json over everything. The QA
        # envelope cannot list the manifests: each manifest carries the QA file's hash.
        written: list[Path] = []
        for result in results:
            set_id = result["builder"].art.spec.id
            image_path = stage / f"{set_id}.png"
            forge_core.save_png(result["atlas"], image_path)
            review_path = stage / f"review-{set_id}.png"
            sheet = codeart_core.review_sheet(
                [result["atlas"]], scales=(2, 4) if result["atlas"].shape[0] <= 128 else (2,),
                backgrounds=("checker",), onion=False, palette=list(result["builder"].art.palette.values()),
                title=f"{set_id} ({result['builder'].kind}), code-drawn",
                qa={"seamless_verified": result["seamless"],
                    "pixels compared": result["proof"]["pixels_compared"],
                    "repetition index": result["metrics"]["repetition_index"]})
            forge_core.save_png(np.asarray(sheet), review_path)
            written += [image_path, review_path]
        if args.preview_map is not None:
            preview_path, review_path = stage / PREVIEW_FILE, stage / REVIEW_FILE
            preview = preview_map(results, spec, *args.preview_map)
            forge_core.save_png(preview, preview_path)
            palette = sorted({colour for r in results for colour in r["builder"].art.palette.values()})
            sheet = codeart_core.review_sheet([preview], scales=(1, 2), backgrounds=("checker",), onion=False,
                                              palette=palette, title="autotile preview map, code-drawn")
            forge_core.save_png(np.asarray(sheet), review_path)
            written += [preview_path, review_path]
        envelope = {
            "status": status,
            "method": ("autotile_build: per set, an exhaustive seam proof and a seeded random-map proof (tiles "
                       "assembled from the atlas against an independent global render, pixel for pixel), a "
                       "detuned non-periodic control that the comparison must catch, a seam metric, a repetition "
                       "index, and outer-ring, distinct-tile, alpha and palette censuses"),
            "notProven": not_proven,
            "checks": checks,
            "inputs": [_input_ref(spec.path, stage, spec.sha256)]
                      + [_input_ref(m.texture.image_path, stage, m.texture.image_sha256)
                         for m in spec.materials.values() if m.texture.image is not None and m.name in used],
            "outputs": [_file_ref(path, stage) for path in written],
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        }
        qa_path = stage / QA_FILE
        forge_core.write_json(qa_path, {**envelope, "metrics": {r["builder"].art.spec.id: r["metrics"]
                                                                for r in results}})
        qa_ref = _file_ref(qa_path, stage)
        for result in results:
            set_id = result["builder"].art.spec.id
            image_path = stage / f"{set_id}.png"
            manifest_path = stage / f"{set_id}.tileset.json"
            forge_core.write_json(manifest_path, tileset_manifest(result, spec, image_path.name, image_path, qa_ref))
            manifests.append(manifest_path)
        palette = {}
        for result in results:
            palette.update(result["builder"].art.palette)
        codeart_core.write_codeart_meta(
            stage / META_FILE, generator=TOOL_NAME, spec_sha256=spec.sha256,
            renderer={"name": TOOL_NAME, "version": TOOL_VERSION}, palette={"colors": palette},
            outputs=written + manifests + [qa_path], qa=envelope,
            extra={"params": spec.params, "tilesets": [p.name for p in manifests]})
    return {
        "status": status,
        "output": output.as_posix(),
        "metadata": (output / META_FILE).as_posix(),
        "qa": (output / QA_FILE).as_posix(),
        "tilesets": [(output / p.name).as_posix() for p in manifests],
        "preview": (output / PREVIEW_FILE).as_posix() if args.preview_map is not None else None,
        "sets": [{"id": r["builder"].art.spec.id, "kind": r["builder"].kind, "tiles": r["metrics"]["tiles"],
                  "seamless_verified": bool(r["seamless"]), "pixels_compared": int(r["proof"]["pixels_compared"]),
                  "mismatches": int(r["proof"]["mismatches"]), "repetition_index": r["metrics"]["repetition_index"]}
                 for r in results],
        "repetition_index": max(r["metrics"]["repetition_index"] for r in results),
        "failed_checks": failed,
    }


# ----------------------------------------------------------------------------- command line

def _texture_arg(value: str) -> tuple[str, Path]:
    name, _, path = value.partition("=")
    if not name or not path:
        raise argparse.ArgumentTypeError("use NAME=PATH, for example grass=textures/grass16.png")
    return name, Path(path)


def _size_arg(value: str) -> tuple[int, int] | None:
    if value.lower() == "none":
        return None
    match = re.fullmatch(r"(\d+)x(\d+)", value.lower())
    if not match or not (1 <= int(match.group(1)) <= 256 and 1 <= int(match.group(2)) <= 256):
        raise argparse.ArgumentTypeError("use WxH in tiles, 1-256 each (for example 24x16), or none")
    return int(match.group(1)), int(match.group(2))


def _ratio_arg(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a number") from None
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("expected a finite number >= 0")
    return number


def make_parser() -> argparse.ArgumentParser:
    """argparse with its own usage-error convention (D26): usage line, `...: error: ...`, exit status 2."""
    parser = argparse.ArgumentParser(
        prog="autotile_build.py",
        description=("Build seam-proven Wang-16, three-material Wang (81), blob-47, bevel and flat tilesets from a "
                     "codeart2d material spec (code-drawn, no image model). Writes atlases, "
                     "generate2dmap.tileset.v1 manifests, a preview map, review sheets, autotile-qa.json and "
                     "codeart-meta.json into a new --output-dir, and prints a one-line JSON summary."),
        epilog=("Example: python autotile_build.py --material-spec water-grass.material.json --kind both "
                "--tile-size 16 --variants 4 --output-dir out/tiles-v1 --preview-map 24x16 --max-repetition 0.35 "
                "--strict-qc"))
    parser.add_argument("--material-spec", required=True, help="codeart2d.material_spec.v1 JSON file")
    parser.add_argument("--output-dir", required=True, help="new folder for the results (must not exist)")
    parser.add_argument("--kind", choices=tuple(KIND_SELECTIONS), default="all",
                        help="which spec sets to build: all (default), both (wang_corner and blob47), "
                             "wang_corner, blob47, bevel or flat (wang and blob are aliases)")
    parser.add_argument("--tile-size", type=int, help="tile size in px, 8-64 (overrides the spec)")
    parser.add_argument("--variants", type=int, help=f"interior-only variants per tile, 1-{MAX_VARIANTS} "
                                                     f"(overrides the spec; default {DEFAULT_VARIANTS})")
    parser.add_argument("--seed", type=int, help="seed (overrides the spec)")
    parser.add_argument("--material-texture", action="append", type=_texture_arg, metavar="NAME=PATH",
                        help="fill material NAME with a one-tile image that wraps seamlessly (repeatable)")
    parser.add_argument("--quantize-textures", action="store_true",
                        help="snap image textures to their material ramp (keeps the palette exact)")
    parser.add_argument("--preview-map", type=_size_arg, default=DEFAULT_PREVIEW, metavar="WxH",
                        help="preview map size in tiles (default 24x16; none to skip)")
    parser.add_argument("--max-repetition", type=_ratio_arg,
                        help=f"fail when the repetition index exceeds this (default: warn above {REPETITION_TARGET})")
    parser.add_argument("--max-seam-ratio", type=_ratio_arg, default=DEFAULT_SEAM_RATIO,
                        help="seam metric limit: flips or colour steps across seams over those inside tiles "
                             "(default 1.0)")
    parser.add_argument("--skip-seam-proof", action="store_true",
                        help="skip the exhaustive seam proof (faster drafts; seamless_verified becomes false and "
                             "--strict-qc fails)")
    parser.add_argument("--strict-qc", action="store_true", help="exit 1 and publish nothing when any check fails")
    return parser


def _run(argv: list[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = make_parser().parse_args(argv)
    try:
        summary = build(args)
    except FileExistsError as exc:
        print("error: " + forge_core.ascii_text(f"output already exists, refusing to replace it: "
                                                f"{exc.filename or args.output_dir}"), file=sys.stderr)
        return 1
    except (SpecError, QCFailure, codeart_core.CodeArtError, OSError, ValueError) as exc:
        print("error: " + forge_core.ascii_text(str(exc)), file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


def main(argv: list[str] | None = None) -> int:
    """The CLI behind forge_core.run_cli (D26/D27): usage errors exit 2 from argparse, expected errors print
    one "error: ..." line and exit 1, and anything unexpected is one "error: internal error (Type: message)"
    line with exit 1, also when main() is called in-process."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
