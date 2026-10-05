#!/usr/bin/env python3
"""Validate an HD-2D stage (generate2dmap.stage.v1) and solve its battle layout at several screen aspects.

Static checks, in the stage's UV space ([0, 1] of sourceSize, y down):
  - every slot (hero, enemy, boss) and approach point stands on a ground polygon, and so does a small
    footprint ellipse around it (--foot-radius as a fraction of the plate width, squashed by --y-squash);
  - no slot or approach point stands in a protected region that applies to walking, or on water (a
    ripple effect); slots lie inside playableBand when the stage declares one;
  - ground polygons are simple and have an area; effects stay clear of regions protected from motion;
  - the plate image, when given, has the stage's sourceSize.

Layout solver: for each --aspects entry (default 4:3, 16:9, 21:9 and 9:19.5) the plate is projected with
the stage's fit (cover crops, contain letterboxes; objectPosition places the window) onto a reference
viewport (720 px high, or 390 px wide for portrait). Each actor starts at its slot, with the formation
squeezed sideways to stay on screen. When that foot is not standable or the actor box hits the screen
edge, a reserved UI panel or another actor, a grid search over the ground band finds the best valid
foot, and the box shrinks along a scale ladder (1.0 to --min-scale in 0.1 steps) before the actor is
reported as unplaced. Actors are drawn in foot-y order. The party-vs-enemies and party-vs-boss
formations are solved separately. A failure inside reviewedAspectRange fails the stage; outside it, it
only warns. Both ends of reviewedAspectRange are solved too.

Outputs, in a new --output-dir: stage-qa.json (a QA envelope with every check and the solved layouts),
stage-overlay.png (the plate with ground, regions, effects and slots drawn on it) and one
layout-<aspect>.png render per aspect (plus layout-<aspect>-boss.png when the stage has a boss slot).
Exit status 1 when a check fails; --strict then publishes nothing.

This module is also the stage library of scene_layout_guide.py, extract_scene_lights.py and
edit_locality_check.py (load_stage, region masks, file references and QA envelopes).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)


STAGE_SCHEMA = "generate2dmap.stage.v1"
QA_SCHEMA = "generate2dmap.stage_qa.v1"
TOOL = {"name": "validate_stage", "version": "1.0"}
FITS = ("cover", "contain")
EFFECT_KINDS = ("ripple", "shimmer", "sway", "glow")
WATER_KINDS = frozenset({"ripple"})
USES = ("walk", "motion", "edit")
ROLE_PREFIX = {"hero": "H", "enemy": "E", "boss": "B"}
DEFAULT_ASPECTS = "4:3,16:9,21:9,9:19.5"
LANDSCAPE_HEIGHT = 720
PORTRAIT_WIDTH = 390
DEFAULT_FOOT_RADIUS = 0.01
DEFAULT_Y_SQUASH = 0.58  # plan Appendix C default for HD-2D plates
DEFAULT_ACTOR_HEIGHT = {"hero": 0.20, "enemy": 0.18, "boss": 0.40}
DEFAULT_ACTOR_ASPECT = 1.0
ACTOR_ANCHOR = (0.5, 0.96)  # foot root inside the actor box (a 448 px canvas with its foot at y 430)
MIN_ACTOR_PX = 38
SCREEN_MARGIN_PX = 6.0
UI_PAD_PX = 6.0
OVERLAP_PAD_PX = 2.0
OVERLAP_PENALTY = 0.016
CROWD_PENALTY = 0.3
CROWD_DISTANCE = 0.35
GRID_STEPS_X, GRID_STEPS_Y = 24, 26
SIDE_GAP = 0.02
WARN_SCALE = 0.7
ASPECT_SLACK = 1e-3
RASTER_CAP = 2048

# Reserved UI panels as viewport fractions [x0, y0, x1, y1), measured from a shipped battle HUD at
# 1280x720 (landscape) and 390x844 (portrait): top bar, party, command, detail, target list and log.
UI_PROFILES: dict[str, dict[str, tuple[tuple[str, tuple[float, float, float, float]], ...]]] = {
    "battle": {
        "landscape": (("top", (0.02, 0.035, 0.78, 0.16)), ("party", (0.80, 0.035, 0.98, 0.34)),
                      ("command", (0.80, 0.45, 0.98, 0.85)), ("detail", (0.80, 0.86, 0.98, 0.97)),
                      ("targets", (0.02, 0.85, 0.40, 0.97)), ("log", (0.02, 0.80, 0.38, 0.84))),
        "portrait": (("top", (0.03, 0.015, 0.97, 0.09)), ("targets", (0.03, 0.105, 0.50, 0.235)),
                     ("party", (0.53, 0.105, 0.97, 0.285)), ("log", (0.03, 0.245, 0.50, 0.285)),
                     ("command", (0.03, 0.645, 0.50, 0.985)), ("detail", (0.53, 0.86, 0.97, 0.985))),
    },
    "none": {"landscape": (), "portrait": ()},
}

NOT_PROVEN = [
    "Geometry is checked against the stage's own polygons; the tool does not see whether the painted plate "
    "really has dry, open ground there. Look at stage-overlay.png and the layout renders.",
    "Actor boxes are rectangles of the given height, aspect and foot anchor, not real silhouettes; the UI "
    "panels are the chosen profile, not the game's own HUD unless --ui gives it.",
    "Layouts are solved at reference viewports (720 px high, 390 px wide in portrait) with the stage's fit and "
    "objectPosition; other projections, safe-area insets and depth-dependent actor scale are not modelled.",
    "A solved layout is one valid answer from a greedy search; the game's own layout code must still place "
    "actors on ground (run it, or compare with the solved feet).",
]

Point = tuple[float, float]
Box = tuple[float, float, float, float]

GROUND_RGB = (70, 230, 160)
WALK_RGB = (255, 96, 64)
GUARD_RGB = (255, 205, 70)
WATER_RGB = (80, 190, 255)
EFFECT_RGB = (190, 150, 255)
BAND_RGB = (255, 255, 255)
UI_RGB = (230, 232, 240)
ROLE_RGB = {"hero": (110, 220, 255), "enemy": (255, 180, 90), "boss": (255, 100, 110)}
FAIL_RGB = (255, 60, 60)


class StageError(ValueError):
    """A stage document that does not follow generate2dmap.stage.v1 (reported as ``error: ...``)."""


# --------------------------------------------------------------------------- stage model

@dataclass(frozen=True)
class Region:
    """A protected region in UV: a polygon or a half-open box [u0, v0, u1, v1)."""
    id: str
    kind: str  # "polygon" or "box"
    polygon: tuple[Point, ...] | None
    box: Box | None
    applies_to: tuple[str, ...] | None  # None: every use (walk, motion, edit)

    def applies(self, use: str) -> bool:
        return self.applies_to is None or use in self.applies_to

    def outline(self) -> tuple[Point, ...]:
        if self.polygon is not None:
            return self.polygon
        u0, v0, u1, v1 = self.box
        return (u0, v0), (u1, v0), (u1, v1), (u0, v1)

    def contains(self, points: np.ndarray) -> np.ndarray:
        if self.polygon is not None:
            return _local_points_in_polygon(points, self.polygon)
        u0, v0, u1, v1 = self.box
        return (points[..., 0] >= u0) & (points[..., 0] < u1) & (points[..., 1] >= v0) & (points[..., 1] < v1)

    def mask(self, size: tuple[int, int]) -> np.ndarray:
        """Pixel-centre raster of the region at ``size`` (the UV square stretched over it)."""
        scale = np.asarray(size, float)
        if self.polygon is not None:
            return _local_polygon_mask(np.asarray(self.polygon, float) * scale, size)
        u0, v0, u1, v1 = self.box
        return _local_box_mask((u0 * size[0], v0 * size[1], u1 * size[0], v1 * size[1]), size)


@dataclass(frozen=True)
class Effect:
    id: str
    kind: str
    polygon: tuple[Point, ...]


@dataclass(frozen=True)
class Stage:
    source_size: tuple[int, int]
    fit: str
    aspect_range: tuple[float, float]
    ground: tuple[tuple[Point, ...], ...]
    slots: tuple[tuple[str, str, Point], ...]  # (label, role, uv): H1.., E1.., B
    protected: tuple[Region, ...]
    effects: tuple[Effect, ...]
    band: tuple[float, float] | None
    approach: tuple[Point, ...]
    baked: dict[str, Any] | None
    plate: str | None
    object_position: Point
    notes: tuple[str, ...]

    def roles(self, *roles: str) -> list[tuple[str, str, Point]]:
        return [slot for slot in self.slots if slot[1] in roles]

    def regions_for(self, use: str) -> list[Region]:
        return [region for region in self.protected if region.applies(use)]


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StageError(f"{where} must be a finite number")
    return float(value)


def _uv(value: Any, where: str) -> float:
    number = _finite(value, where)
    if not 0.0 <= number <= 1.0:
        raise StageError(f"{where} must lie in 0..1 (UV of sourceSize); got {number:g}")
    return number


def _uv_point(value: Any, where: str) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise StageError(f"{where} must be [u, v]")
    return _uv(value[0], f"{where}[0]"), _uv(value[1], f"{where}[1]")


def _uv_polygon(value: Any, where: str) -> tuple[Point, ...]:
    """At least three [u, v] vertices; a repeated closing vertex (last == first) is dropped."""
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        raise StageError(f"{where} must be a polygon of at least three [u, v] points")
    points = tuple(_uv_point(point, f"{where}[{index}]") for index, point in enumerate(value))
    if len(points) > 3 and points[0] == points[-1]:
        points = points[:-1]
    return points


def _uv_box(value: Any, where: str) -> Box:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise StageError(f"{where} must be [u0, v0, u1, v1]")
    u0, v0, u1, v1 = (_uv(item, f"{where}[{index}]") for index, item in enumerate(value))
    if u0 >= u1 or v0 >= v1:
        raise StageError(f"{where} must have u0 < u1 and v0 < v1")
    return u0, v0, u1, v1


def _identifier(value: Any, where: str, seen: set[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StageError(f"{where} must be a non-empty string")
    if value in seen:
        raise StageError(f"{where} {value!r} is used twice")
    seen.add(value)
    return value


def parse_stage(data: Any) -> Stage:
    """Check a stage document against generate2dmap.stage.v1 plus its cross-field rules; return the model.

    Raises StageError naming the JSON path of the first problem. ``objectPosition`` ([fx, fy] in 0..1,
    default [0.5, 0.5]) is an optional extension: where the projection window sits in the plate.
    """
    if not isinstance(data, dict):
        raise StageError("the stage must be a JSON object")
    if data.get("schema") != STAGE_SCHEMA:
        raise StageError(f"$.schema must be {STAGE_SCHEMA!r}; got {data.get('schema')!r}")
    for key in ("sourceSize", "fit", "reviewedAspectRange", "groundPolygons", "slots"):
        if key not in data:
            raise StageError(f"$.{key} is required")
    size = data["sourceSize"]
    if (not isinstance(size, list) or len(size) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in size)):
        raise StageError("$.sourceSize must be [width, height] in whole pixels")
    if data["fit"] not in FITS:
        raise StageError(f"$.fit must be one of {', '.join(FITS)}")
    span = data["reviewedAspectRange"]
    if not isinstance(span, list) or len(span) != 2:
        raise StageError("$.reviewedAspectRange must be [min, max] width/height")
    low, high = (_finite(v, f"$.reviewedAspectRange[{i}]") for i, v in enumerate(span))
    if low <= 0 or low > high:
        raise StageError("$.reviewedAspectRange must be [min, max] with 0 < min <= max")
    polygons = data["groundPolygons"]
    if not isinstance(polygons, list) or not polygons:
        raise StageError("$.groundPolygons must list at least one polygon")
    ground = tuple(_uv_polygon(p, f"$.groundPolygons[{i}]") for i, p in enumerate(polygons))

    slots_data = data["slots"]
    if not isinstance(slots_data, dict):
        raise StageError("$.slots must be an object with hero and enemy lists")
    slots: list[tuple[str, str, Point]] = []
    for role in ("hero", "enemy"):
        entries = slots_data.get(role)
        if not isinstance(entries, list):
            raise StageError(f"$.slots.{role} must be a list of [u, v] foot roots")
        slots += [(f"{ROLE_PREFIX[role]}{i + 1}", role, _uv_point(p, f"$.slots.{role}[{i}]"))
                  for i, p in enumerate(entries)]
    if "boss" in slots_data:
        slots.append(("B", "boss", _uv_point(slots_data["boss"], "$.slots.boss")))

    for key in ("protectedRegions", "effects", "approachPoints"):
        if data.get(key) is not None and not isinstance(data[key], list):
            raise StageError(f"$.{key} must be a list")
    notes: list[str] = []
    protected: list[Region] = []
    seen: set[str] = set()
    for i, entry in enumerate(data.get("protectedRegions") or []):
        where = f"$.protectedRegions[{i}]"
        if not isinstance(entry, dict):
            raise StageError(f"{where} must be an object")
        ident = _identifier(entry.get("id"), f"{where}.id", seen)
        if ("polygon" in entry) == ("box" in entry):
            raise StageError(f"{where} needs exactly one of polygon or box")
        applies = entry.get("appliesTo")
        if applies is not None:
            if not isinstance(applies, list) or not all(isinstance(a, str) and a for a in applies):
                raise StageError(f"{where}.appliesTo must be a list of non-empty strings")
            unknown = sorted(set(applies) - set(USES))
            if unknown:
                notes.append(f"protected region {ident}: appliesTo {', '.join(unknown)} is not one of "
                             f"{', '.join(USES)} and is ignored by these tools")
            applies = tuple(applies)
        if "polygon" in entry:
            protected.append(Region(ident, "polygon", _uv_polygon(entry["polygon"], f"{where}.polygon"), None, applies))
        else:
            protected.append(Region(ident, "box", None, _uv_box(entry["box"], f"{where}.box"), applies))

    effects: list[Effect] = []
    seen = set()
    for i, entry in enumerate(data.get("effects") or []):
        where = f"$.effects[{i}]"
        if not isinstance(entry, dict):
            raise StageError(f"{where} must be an object")
        ident = _identifier(entry.get("id"), f"{where}.id", seen)
        if entry.get("kind") not in EFFECT_KINDS:
            raise StageError(f"{where}.kind must be one of {', '.join(EFFECT_KINDS)}")
        for key, positive in (("period", True), ("amplitude", False), ("wavelength", True)):
            if key in entry:
                value = _finite(entry[key], f"{where}.{key}")
                if value < 0 or (positive and value == 0):
                    raise StageError(f"{where}.{key} must be {'positive' if positive else 'zero or more'}")
        if "axis" in entry and entry["axis"] not in ("x", "y"):
            raise StageError(f"{where}.axis must be x or y")
        effects.append(Effect(ident, entry["kind"], _uv_polygon(entry.get("polygon"), f"{where}.polygon")))

    band = None
    if data.get("playableBand") is not None:
        value = data["playableBand"]
        if not isinstance(value, list) or len(value) != 2:
            raise StageError("$.playableBand must be [v0, v1]")
        band = (_uv(value[0], "$.playableBand[0]"), _uv(value[1], "$.playableBand[1]"))
        if band[0] >= band[1]:
            raise StageError("$.playableBand must have v0 < v1")
    approach = tuple(_uv_point(p, f"$.approachPoints[{i}]") for i, p in enumerate(data.get("approachPoints") or []))
    baked = data.get("bakedContent")
    if baked is not None:
        if not isinstance(baked, dict):
            raise StageError("$.bakedContent must be an object")
        for key in ("actors", "collectibles"):
            if key in baked and not isinstance(baked[key], (bool, list)):
                raise StageError(f"$.bakedContent.{key} must be true, false or a list")
    plate = data.get("plate")
    if plate is not None and (not isinstance(plate, str) or not plate or plate.startswith("/") or "\\" in plate
                              or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", plate)):
        raise StageError("$.plate must be a relative POSIX path (no drive, leading slash, backslash or URL)")
    position = (0.5, 0.5)
    if "objectPosition" in data:
        position = _uv_point(data["objectPosition"], "$.objectPosition")
    return Stage((size[0], size[1]), data["fit"], (low, high), ground, tuple(slots), tuple(protected),
                 tuple(effects), band, approach, baked, plate, position, tuple(notes))


def load_stage(path: str | os.PathLike) -> Stage:
    """Read and parse a stage file; JSON and contract problems raise StageError naming the file."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StageError(f"{path.name} is not valid JSON: {error}") from None
    try:
        return parse_stage(data)
    except StageError as error:
        raise StageError(f"{path.name}: {error}") from None


# --------------------------------------------------------------------------- geometry (vectorised)

def _local_points_in_polygon(points: Any, polygon: Sequence[Point]) -> np.ndarray:
    """Even-odd (crossing-number) test of every point against one polygon, vectorised over points.

    The same rule as the reference battle layout's ``inside()`` and plan Appendix C walk regions;
    points exactly on an edge may fall on either side. ``points`` has shape (..., 2).
    """
    points = np.asarray(points, float)
    x, y = points[..., 0], points[..., 1]
    inside = np.zeros(x.shape, bool)
    vertices = np.asarray(polygon, float)
    for (ax, ay), (bx, by) in zip(vertices, np.roll(vertices, 1, axis=0)):
        if ay == by:
            continue
        crosses = (ay > y) != (by > y)
        inside ^= crosses & (x < (bx - ax) * (y - ay) / (by - ay) + ax)
    return inside


def _local_polygon_mask(polygon_px: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Boolean (height, width) mask of the pixels whose centres lie inside ``polygon_px`` (even-odd)."""
    width, height = size
    mask = np.zeros((height, width), bool)
    x0 = max(0, math.floor(polygon_px[:, 0].min() - 0.5))
    x1 = min(width, math.ceil(polygon_px[:, 0].max() + 0.5))
    y0 = max(0, math.floor(polygon_px[:, 1].min() - 0.5))
    y1 = min(height, math.ceil(polygon_px[:, 1].max() + 0.5))
    if x0 >= x1 or y0 >= y1:
        return mask
    ys, xs = np.mgrid[y0:y1, x0:x1]
    centres = np.stack([xs + 0.5, ys + 0.5], axis=-1)
    mask[y0:y1, x0:x1] = _local_points_in_polygon(centres, polygon_px)
    return mask


def _local_box_mask(box_px: Sequence[float], size: tuple[int, int]) -> np.ndarray:
    """Boolean mask of the pixels whose centres lie in the half-open box [x0, x1) x [y0, y1)."""
    width, height = size
    mask = np.zeros((height, width), bool)
    columns = [min(width, max(0, math.ceil(edge - 0.5))) for edge in (box_px[0], box_px[2])]
    rows = [min(height, max(0, math.ceil(edge - 0.5))) for edge in (box_px[1], box_px[3])]
    mask[rows[0]:rows[1], columns[0]:columns[1]] = True
    return mask


def _local_box_mean(plane: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a (2r+1)^2 window with replicated edges, from a summed-area table (float64)."""
    if radius <= 0:
        return plane.astype(np.float64)
    size = 2 * radius + 1
    padded = np.pad(plane.astype(np.float64), radius, mode="edge")
    table = np.zeros((padded.shape[0] + 1, padded.shape[1] + 1))
    table[1:, 1:] = padded.cumsum(0).cumsum(1)
    window = table[size:, size:] - table[:-size, size:] - table[size:, :-size] + table[:-size, :-size]
    return window / (size * size)


def _local_dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Chebyshev (square) dilation of a boolean mask by ``radius`` pixels, from a summed-area table."""
    if radius <= 0:
        return mask.copy()
    size = 2 * radius + 1
    table = np.zeros((mask.shape[0] + size, mask.shape[1] + size), np.int64)
    table[1:, 1:] = np.pad(mask, radius).astype(np.int64).cumsum(0).cumsum(1)
    window = table[size:, size:] - table[:-size, size:] - table[size:, :-size] + table[:-size, :-size]
    return window > 0


def polygon_problems(polygon: Sequence[Point], size: tuple[int, int]) -> list[str]:
    """Why a UV polygon is not a simple polygon with an area at ``size`` (empty when it is)."""
    points = np.asarray(polygon, float) * np.asarray(size, float)
    points = points[np.any(points != np.roll(points, 1, axis=0), axis=1)]  # drop repeated consecutive vertices
    x, y = points[:, 0], points[:, 1]
    area = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
    problems = [] if area >= 1.0 and len(points) >= 3 else [f"area {area:.2f} px^2 (no area)"]
    count = len(points)
    for i in range(count):
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            if _segments_touch(points[i], points[(i + 1) % count], points[j], points[(j + 1) % count]):
                problems.append(f"edges {i}-{(i + 1) % count} and {j}-{(j + 1) % count} cross")
    return problems


def _segments_touch(p1: np.ndarray, p2: np.ndarray, q1: np.ndarray, q2: np.ndarray) -> bool:
    def orient(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
        return float((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))

    def between(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> bool:
        return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])

    d1, d2, d3, d4 = orient(q1, q2, p1), orient(q1, q2, p2), orient(p1, p2, q1), orient(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return True
    return ((d1 == 0 and between(q1, q2, p1)) or (d2 == 0 and between(q1, q2, p2))
            or (d3 == 0 and between(p1, p2, q1)) or (d4 == 0 and between(p1, p2, q2)))


def footprint_samples(points: Any, source_size: tuple[int, int], foot_radius: float, y_squash: float) -> np.ndarray:
    """Each foot point plus 8 samples on its footprint ellipse (plan Appendix C), shape (n, 9, 2), in UV.

    ``foot_radius`` is a fraction of the plate width; the ellipse's vertical radius is that many pixels
    times ``y_squash``.
    """
    points = np.asarray(points, float).reshape(-1, 2)
    width, height = source_size
    angles = np.arange(8) * (math.pi / 4)
    ring = np.stack([np.cos(angles) * foot_radius, np.sin(angles) * foot_radius * y_squash * width / height], 1)
    offsets = np.vstack([np.zeros((1, 2)), ring])
    return points[:, None, :] + offsets[None, :, :]


def stand_report(stage: Stage, points: Any, *, foot_radius: float, y_squash: float) -> dict[str, np.ndarray]:
    """Per point: whole footprint on ground, any sample in a walk-protected region, any sample on water."""
    samples = footprint_samples(points, stage.source_size, foot_radius, y_squash)
    flat = samples.reshape(-1, 2)
    on_ground = np.zeros(len(flat), bool)
    for polygon in stage.ground:
        on_ground |= _local_points_in_polygon(flat, polygon)
    protected = np.zeros(len(flat), bool)
    for region in stage.regions_for("walk"):
        protected |= region.contains(flat)
    water = np.zeros(len(flat), bool)
    for effect in stage.effects:
        if effect.kind in WATER_KINDS:
            water |= _local_points_in_polygon(flat, effect.polygon)
    shape = samples.shape[:2]
    return {"ground": on_ground.reshape(shape).all(axis=1), "protected": protected.reshape(shape).any(axis=1),
            "water": water.reshape(shape).any(axis=1)}


def standable(stage: Stage, points: Any, *, foot_radius: float, y_squash: float) -> np.ndarray:
    """True where the whole footprint is on ground and clear of walk-protected regions and water."""
    report = stand_report(stage, points, foot_radius=foot_radius, y_squash=y_squash)
    return report["ground"] & ~report["protected"] & ~report["water"]


def ground_band(stage: Stage) -> tuple[float, float]:
    """playableBand, or the vertical extent of the ground polygons."""
    if stage.band is not None:
        return stage.band
    rows = [point[1] for polygon in stage.ground for point in polygon]
    return min(rows), max(rows)


# --------------------------------------------------------------------------- shared output helpers

def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def rounded(value: Any, digits: int = 4) -> Any:
    """JSON-friendly copy with floats rounded (and -0.0 made 0.0); tuples become lists."""
    if isinstance(value, (float, np.floating)):
        return round(float(value), digits) + 0.0
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, dict):
        return {key: rounded(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [rounded(item, digits) for item in value]
    return value


def _local_file_ref(path: str | os.PathLike, base: str | os.PathLike, sha256: str | None = None) -> dict[str, Any]:
    """fileRef of ``path`` relative to the directory ``base``; only the file name when there is no
    relative route (another drive), never an absolute path (plan Appendix A, portable_path note)."""
    relative = forge_core.portable_path(path, base)
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        relative = Path(path).name
    return {"path": relative, "sha256": sha256 or forge_core.sha256_file(path), "bytes": Path(path).stat().st_size}


def _local_qa_envelope(checks: list[dict[str, Any]], *, method: str, not_proven: Sequence[str],
                       inputs: list[dict[str, Any]], outputs: list[dict[str, Any]], tool: dict[str, str],
                       visual: bool = False) -> dict[str, Any]:
    """A common qaEnvelope. Status: fail > warn > needs-visual-review (a check asks for it, or ``visual``
    says the numbers cannot judge the art) > pass. No createdAt, so reruns are byte-identical."""
    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        status = "fail"
    elif "warn" in statuses:
        status = "warn"
    elif visual or "needs-visual-review" in statuses:
        status = "needs-visual-review"
    else:
        status = "pass"
    return {"status": status, "method": method, "notProven": list(not_proven), "checks": checks,
            "inputs": inputs, "outputs": outputs, "tool": dict(tool)}


def check(ident: str, status: str, value: Any = None, threshold: Any = None) -> dict[str, Any]:
    return {"id": ident, "status": status, "value": rounded(value), "threshold": rounded(threshold)}


def font(size: int) -> ImageFont.ImageFont:
    """Pillow's bundled font at ``size`` px (bitmap fallback without FreeType)."""
    try:
        return ImageFont.load_default(size=max(8, size))
    except (TypeError, OSError, ImportError):
        return ImageFont.load_default()


def text_height(typeface: Any) -> int:
    """Nominal line height of a font from font(): its size, or 11 px for the bitmap fallback."""
    return int(getattr(typeface, "size", 11))


def label(draw: ImageDraw.ImageDraw, xy: Sequence[float], text: str, fill: Sequence[int], typeface: Any,
          stroke: int = 2) -> None:
    """ASCII label with a dark stroke where the font supports one."""
    kwargs: dict[str, Any] = {"font": typeface}
    if isinstance(typeface, ImageFont.FreeTypeFont):
        kwargs.update(stroke_width=stroke, stroke_fill=(8, 12, 20, 255))
    draw.text((float(xy[0]), float(xy[1])), forge_core.ascii_text(text), fill=tuple(fill), **kwargs)


def refuse_existing(path: Path) -> None:
    if os.path.lexists(path):
        raise FileExistsError(f"Refusing to replace existing output: {path}")


# --------------------------------------------------------------------------- aspects, UI and actors

def parse_aspects(text: str) -> list[tuple[str, float]]:
    """'4:3,16:9,9:19.5' or plain ratios such as 1.7778, as (name, width / height)."""
    aspects: list[tuple[str, float]] = []
    for part in (item.strip() for item in text.split(",")):
        if not part:
            continue
        match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(?:[:/]\s*([0-9]*\.?[0-9]+))?", part)
        ratio = float(match.group(1)) / float(match.group(2) or 1) if match and float(match.group(2) or 1) else 0.0
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError(f"aspect {part!r} must look like 16:9, 9:19.5 or 1.7778")
        if part not in (name for name, _ in aspects):
            aspects.append((part, ratio))
    if not aspects:
        raise ValueError("--aspects needs at least one aspect")
    return aspects


def viewport_for(ratio: float) -> tuple[int, int]:
    """Reference viewport in CSS pixels: 720 px high for landscape, 390 px wide for portrait."""
    if ratio >= 1:
        return max(1, round_half_up(LANDSCAPE_HEIGHT * ratio)), LANDSCAPE_HEIGHT
    return PORTRAIT_WIDTH, max(1, round_half_up(PORTRAIT_WIDTH / ratio))


def aspect_file_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z.]+", "x", name)


def load_ui(profile: str, path: Path | None) -> dict[str, tuple[tuple[str, Box], ...]]:
    """UI panels per orientation as viewport fractions: a built-in profile or a JSON file
    ``{"landscape": [{"id", "box": [x0, y0, x1, y1]}], "portrait": [...]}`` (missing orientation: none)."""
    if path is None:
        return UI_PROFILES[profile]
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the UI file must be an object with landscape and portrait panel lists")
    panels: dict[str, tuple[tuple[str, Box], ...]] = {}
    for orientation in ("landscape", "portrait"):
        entries = data.get(orientation, [])
        if not isinstance(entries, list):
            raise ValueError(f"{path}: {orientation} must be a list of {{id, box}} panels")
        parsed = []
        for index, entry in enumerate(entries):
            where = f"{path.name}: {orientation}[{index}]"
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                raise ValueError(f"{where} needs an id and a box")
            try:
                parsed.append((entry["id"], _uv_box(entry.get("box"), f"{where}.box")))
            except StageError as error:
                raise ValueError(str(error)) from None
        panels[orientation] = tuple(parsed)
    return panels


@dataclass(frozen=True)
class ActorSpec:
    height: float  # box height as a fraction of the plate height
    aspect: float  # box width / height
    anchor: Point = ACTOR_ANCHOR


@dataclass(frozen=True)
class Projection:
    """Plate (source pixels) to viewport pixels: screen = offset + uv * sourceSize * scale."""
    viewport: tuple[int, int]
    source_size: tuple[int, int]
    scale: float
    offset: Point

    def to_screen(self, uv: Any) -> np.ndarray:
        uv = np.asarray(uv, float)
        return np.stack([self.offset[0] + uv[..., 0] * self.source_size[0] * self.scale,
                         self.offset[1] + uv[..., 1] * self.source_size[1] * self.scale], axis=-1)

    def to_uv(self, xy: Any) -> np.ndarray:
        xy = np.asarray(xy, float)
        return np.stack([(xy[..., 0] - self.offset[0]) / (self.source_size[0] * self.scale),
                         (xy[..., 1] - self.offset[1]) / (self.source_size[1] * self.scale)], axis=-1)

    def visible_uv(self) -> Box:
        corners = self.to_uv([[0.0, 0.0], [float(self.viewport[0]), float(self.viewport[1])]])
        u0, v0 = np.clip(corners[0], 0.0, 1.0)
        u1, v1 = np.clip(corners[1], 0.0, 1.0)
        return float(u0), float(v0), float(u1), float(v1)


def make_projection(stage: Stage, viewport: tuple[int, int]) -> Projection:
    """cover (largest scale, crops) or contain (smallest scale, letterboxes), placed by objectPosition."""
    (source_w, source_h), (width, height) = stage.source_size, viewport
    scale = (max if stage.fit == "cover" else min)(width / source_w, height / source_h)
    fx, fy = stage.object_position
    return Projection(viewport, stage.source_size, scale,
                      ((width - source_w * scale) * fx, (height - source_h * scale) * fy))


# --------------------------------------------------------------------------- layout solver

def _ladder(min_scale: float) -> list[float]:
    steps = int(math.floor((1.0 - min_scale) / 0.1 + 1e-9))
    return [round(1.0 - 0.1 * step, 10) for step in range(steps + 1)]


def _squeeze(xs: np.ndarray, half_widths: np.ndarray, width: int) -> np.ndarray:
    """Shift, or compress around the centre, the formation's x positions so every box fits on screen
    while keeping the left-to-right order (the floating-layout rule)."""
    if not len(xs):
        return xs
    pad = SCREEN_MARGIN_PX + float(half_widths.max())
    low, high = pad, width - pad
    if high <= low:
        return np.full_like(xs, width / 2.0)
    first, last = float(xs.min()), float(xs.max())
    if last - first > high - low:
        return low + (xs - first) * (high - low) / (last - first)
    shift = low - first if first < low else (high - last if last > high else 0.0)
    return xs + shift


def _boxes(feet: np.ndarray, size: float, spec: ActorSpec) -> np.ndarray:
    width = size * spec.aspect
    left = feet[:, 0] - width * spec.anchor[0]
    top = feet[:, 1] - size * spec.anchor[1]
    return np.stack([left, top, left + width, top + size], axis=1)


def _overlaps(boxes: np.ndarray, other: Sequence[float], pad: float) -> np.ndarray:
    return ((boxes[:, 0] < other[2] + pad) & (boxes[:, 2] > other[0] - pad)
            & (boxes[:, 1] < other[3] + pad) & (boxes[:, 3] > other[1] - pad))


def solve_layout(stage: Stage, viewport: tuple[int, int], formation: Sequence[tuple[str, str, Point]], *,
                 ui_panels: Sequence[tuple[str, Box]], actors: dict[str, ActorSpec], foot_radius: float,
                 y_squash: float, min_scale: float) -> dict[str, Any]:
    """Place each actor of ``formation`` on standable ground clear of the UI, edges and other actors.

    Returns the projection, the UI panels in pixels, one record per actor (slot, desired and chosen foot,
    box, scale factor, placed, grounded) and the foot-y draw order.
    """
    width, height = viewport
    projection = make_projection(stage, viewport)
    panels = [(ident, (box[0] * width, box[1] * height, box[2] * width, box[3] * height)) for ident, box in ui_panels]
    band = ground_band(stage)
    band_y = projection.to_screen(np.array([[0.0, band[0]], [0.0, band[1]]]))[:, 1]
    y_low, y_high = max(0.0, float(band_y[0])), min(float(height), float(band_y[1]))
    base = {role: max(MIN_ACTOR_PX, spec.height * stage.source_size[1] * projection.scale)
            for role, spec in actors.items()}

    slots = np.array([slot[2] for slot in formation], float).reshape(-1, 2)
    desired = projection.to_screen(slots) if len(formation) else np.zeros((0, 2))
    if len(formation):
        half = np.array([base[role] * actors[role].aspect * actors[role].anchor[0] for _, role, _ in formation])
        desired[:, 0] = _squeeze(desired[:, 0], half, width)
    heroes = [i for i, slot in enumerate(formation) if slot[1] == "hero"]
    foes = [i for i, slot in enumerate(formation) if slot[1] != "hero"]
    bounds = {i: (0.0, float(width)) for i in range(len(formation))}
    if heroes and foes:
        hero_x, foe_x = float(desired[heroes, 0].mean()), float(desired[foes, 0].mean())
        middle, gap = (hero_x + foe_x) / 2.0, SIDE_GAP * width / 2.0
        right, left = (middle + gap, float(width)), (0.0, middle - gap)
        for i in heroes:
            bounds[i] = right if hero_x >= foe_x else left
        for i in foes:
            bounds[i] = left if hero_x >= foe_x else right

    placed: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, (ident, role, slot) in enumerate(formation):
        spec = actors[role]
        target = desired[index]
        x_bounds = bounds[index]
        chosen = None
        for factor in _ladder(min_scale):
            size = float(max(MIN_ACTOR_PX, math.floor(base[role] * factor)))
            candidates = target[None, :]
            for search in (False, True):
                if search:
                    xs = np.concatenate([[target[0]], x_bounds[0] + (x_bounds[1] - x_bounds[0])
                                         * np.arange(GRID_STEPS_X + 1) / GRID_STEPS_X])
                    ys = y_low + (y_high - y_low) * np.arange(GRID_STEPS_Y + 1) / GRID_STEPS_Y
                    if 0.0 <= target[1] <= height:
                        ys = np.concatenate([[target[1]], ys])
                    grid = np.stack(np.meshgrid(xs, ys, indexing="ij"), axis=-1).reshape(-1, 2)
                    candidates = np.vstack([target[None, :], grid])
                boxes = _boxes(candidates, size, spec)
                uv = projection.to_uv(candidates)
                valid = standable(stage, uv, foot_radius=foot_radius, y_squash=y_squash)
                valid &= (uv[:, 1] >= band[0]) & (uv[:, 1] <= band[1])
                valid &= (candidates[:, 0] >= x_bounds[0]) & (candidates[:, 0] <= x_bounds[1])
                valid &= ((boxes[:, 0] >= SCREEN_MARGIN_PX) & (boxes[:, 2] <= width - SCREEN_MARGIN_PX)
                          & (boxes[:, 1] >= SCREEN_MARGIN_PX) & (boxes[:, 3] <= height - SCREEN_MARGIN_PX))
                for _, panel in panels:
                    valid &= ~_overlaps(boxes, panel, UI_PAD_PX)
                score = (2.0 * ((candidates[:, 0] - target[0]) / width) ** 2
                         + ((candidates[:, 1] - target[1]) / height) ** 2)
                for other in placed:
                    score += OVERLAP_PENALTY * _overlaps(boxes, other["box"], OVERLAP_PAD_PX)
                    distance = np.hypot(candidates[:, 0] - other["foot"][0], candidates[:, 1] - other["foot"][1])
                    score += CROWD_PENALTY * (distance < size * CROWD_DISTANCE)
                if not search and valid[0] and score[0] <= 1e-5:
                    chosen = (candidates[0], boxes[0], size, factor)
                    break
                if search and valid.any():
                    best = int(np.flatnonzero(valid)[np.argmin(score[valid])])
                    chosen = (candidates[best], boxes[best], size, factor)
            if chosen is not None:
                break
        if chosen is None:
            size = float(max(MIN_ACTOR_PX, math.floor(base[role] * min(_ladder(min_scale)))))
            chosen = (target, _boxes(target[None, :], size, spec)[0], size, None)
            errors.append(f"no standable ground clear of the UI and edges for {ident} "
                          f"(down to x{min(_ladder(min_scale)):g})")
        foot, box, size, factor = chosen
        foot_uv = projection.to_uv(foot)
        grounded = bool(standable(stage, foot_uv[None, :], foot_radius=foot_radius, y_squash=y_squash)[0])
        placed.append({"id": ident, "role": role, "slot": list(slot), "desired": target.tolist(),
                       "foot": foot.tolist(), "footUv": foot_uv.tolist(), "box": box.tolist(), "sizePx": size,
                       "scale": factor, "placed": factor is not None, "grounded": grounded,
                       "movedPx": float(np.hypot(*(foot - target)))})
    order = sorted(placed, key=lambda actor: (actor["foot"][1], actor["foot"][0], actor["id"]))
    overlaps = [[a["id"], b["id"]] for i, a in enumerate(placed) for b in placed[i + 1:]
                if bool(_overlaps(np.array([a["box"]]), b["box"], 0.0)[0])]
    return {"viewport": list(viewport), "fit": stage.fit, "scale": projection.scale, "offset": list(projection.offset),
            "visibleUv": list(projection.visible_uv()), "ui": [{"id": i, "box": list(b)} for i, b in panels],
            "actors": placed, "drawOrder": [actor["id"] for actor in order], "overlaps": overlaps, "errors": errors,
            "_projection": projection}


def formations(stage: Stage) -> list[tuple[str, list[tuple[str, str, Point]]]]:
    """party-vs-enemies, and party-vs-boss when the stage has a boss slot."""
    result = [("party-vs-enemies", stage.roles("hero", "enemy"))]
    if stage.roles("boss"):
        result.append(("party-vs-boss", stage.roles("hero", "boss")))
    return result


# --------------------------------------------------------------------------- static checks

def static_checks(stage: Stage, *, foot_radius: float, y_squash: float) -> list[dict[str, Any]]:
    """Checks that need no viewport: polygons, slots, approach points, band, effects and bakedContent."""
    size = stage.source_size
    threshold = {"footRadius": foot_radius, "ySquash": y_squash}
    checks = []
    problems = {f"ground[{i}]": found for i, polygon in enumerate(stage.ground)
                if (found := polygon_problems(polygon, size))}
    checks.append(check("ground polygons are simple", "fail" if problems else "pass", problems or None))

    points = np.array([slot[2] for slot in stage.slots], float).reshape(-1, 2)
    report = stand_report(stage, points, foot_radius=foot_radius, y_squash=y_squash)
    labels = [slot[0] for slot in stage.slots]
    for key, ident in (("ground", "slots stand on ground"), ("protected", "slots avoid walk-protected regions"),
                       ("water", "slots stay off water")):
        bad = report[key] if key != "ground" else ~report[key]
        offenders = [{"slot": labels[i], "uv": list(stage.slots[i][2])} for i in np.flatnonzero(bad)]
        status = "skipped" if not len(points) else ("fail" if offenders else "pass")
        checks.append(check(ident, status, offenders or None, threshold))
    if stage.band is None:
        checks.append(check("slots inside playableBand", "skipped", "no playableBand"))
    else:
        outside = [{"slot": label_, "v": uv[1]} for label_, _, uv in stage.slots
                   if not stage.band[0] <= uv[1] <= stage.band[1]]
        checks.append(check("slots inside playableBand", "fail" if outside else "pass", outside or None,
                            list(stage.band)))
    if stage.approach:
        ok = standable(stage, np.array(stage.approach), foot_radius=foot_radius, y_squash=y_squash)
        bad_points = [{"point": i, "uv": list(stage.approach[i])} for i in np.flatnonzero(~ok)]
        checks.append(check("approach points are standable", "fail" if bad_points else "pass",
                            bad_points or None, threshold))
    else:
        checks.append(check("approach points are standable", "skipped", "no approachPoints"))
    crowded = []
    radius_px = foot_radius * size[0]
    for i, a in enumerate(stage.slots):
        for b in stage.slots[i + 1:]:
            if {a[1], b[1]} == {"enemy", "boss"} or not radius_px:
                continue  # boss and enemies are alternative formations
            dx, dy = (a[2][0] - b[2][0]) * size[0], (a[2][1] - b[2][1]) * size[1]
            if math.hypot(dx, dy / y_squash) < 2 * radius_px:  # two equal ellipses touch
                crowded.append({"slots": [a[0], b[0]], "distancePx": math.hypot(dx, dy)})
    checks.append(check("slot footprints do not overlap", "warn" if crowded else "pass", crowded or None,
                        {"footprintPx": [2 * radius_px, 2 * radius_px * y_squash]}))

    scale = min(1.0, RASTER_CAP / max(size))
    raster = (max(1, round_half_up(size[0] * scale)), max(1, round_half_up(size[1] * scale)))
    guarded = [(region, region.mask(raster)) for region in stage.regions_for("motion")]
    clashes, wet_ground = [], []
    ground_mask = np.zeros((raster[1], raster[0]), bool)
    for polygon in stage.ground:
        ground_mask |= _local_polygon_mask(np.asarray(polygon, float) * raster, raster)
    for effect in stage.effects:
        effect_mask = _local_polygon_mask(np.asarray(effect.polygon, float) * raster, raster)
        for region, region_mask in guarded:
            shared = int((effect_mask & region_mask).sum())
            if shared:
                clashes.append({"effect": effect.id, "region": region.id, "overlapPx": shared / scale ** 2})
        if effect.kind in WATER_KINDS and (shared := int((effect_mask & ground_mask).sum())):
            wet_ground.append({"effect": effect.id, "overlapPx": shared / scale ** 2})
    checks.append(check("effects avoid motion-protected regions", "fail" if clashes else "pass", clashes or None))
    checks.append(check("water stays off ground polygons", "warn" if wet_ground else "pass", wet_ground or None,
                        "split the ground polygon around the water: a runtime that tests only groundPolygons "
                        "would let actors stand in it (validate_stage also tests ripple polygons)"))
    baked = stage.baked
    if baked is None:
        checks.append(check("bakedContent declared", "warn", "bakedContent is missing: say whether the plate "
                                                             "paints actors and collectibles"))
    else:
        painted = [key for key in ("actors", "collectibles") if baked.get(key) not in (None, False, [])]
        status = "warn" if "actors" in painted and stage.slots else "pass"
        checks.append(check("bakedContent declared", status, {"painted": painted}))
    return checks


def in_reviewed_range(stage: Stage, ratio: float) -> bool:
    """Inside reviewedAspectRange, with 0.1% slack because ranges are written rounded (2.3333 for 21:9)."""
    return stage.aspect_range[0] * (1 - ASPECT_SLACK) <= ratio <= stage.aspect_range[1] * (1 + ASPECT_SLACK)


def layout_check(name: str, ratio: float, formation: str, layout: dict[str, Any], stage: Stage) -> dict[str, Any]:
    in_range = in_reviewed_range(stage, ratio)
    unplaced = [actor["id"] for actor in layout["actors"] if not actor["placed"]]
    scales = [actor["scale"] for actor in layout["actors"] if actor["scale"] is not None]
    lowest = min(scales) if scales else None
    if unplaced:
        status = "fail" if in_range else "warn"
    elif not layout["actors"]:
        status = "skipped"
    else:
        status = "warn" if in_range and lowest is not None and lowest < WARN_SCALE else "pass"
    value = {"aspect": name, "ratio": ratio, "formation": formation, "viewport": layout["viewport"],
             "inReviewedRange": in_range, "placed": len(layout["actors"]) - len(unplaced), "unplaced": unplaced,
             "minScale": lowest, "drawOrder": layout["drawOrder"], "errors": layout["errors"]}
    return check(f"layout {name} {formation}", status, value,
                 {"reviewedAspectRange": list(stage.aspect_range), "warnBelowScale": WARN_SCALE})


# --------------------------------------------------------------------------- drawing

def _canvas(plate: Image.Image | None, size: tuple[int, int]) -> Image.Image:
    if plate is not None:
        return plate.convert("RGBA")
    canvas = Image.new("RGBA", size, (26, 32, 44, 255))
    draw = ImageDraw.Draw(canvas)
    for step in range(1, 10):
        x, y = size[0] * step / 10.0, size[1] * step / 10.0
        draw.line([(x, 0), (x, size[1])], fill=(48, 58, 76, 255))
        draw.line([(0, y), (size[0], y)], fill=(48, 58, 76, 255))
    return canvas


def _polygon(draw: ImageDraw.ImageDraw, points: Iterable[Sequence[float]], rgb: Sequence[int], fill_alpha: int,
             line: int) -> None:
    xy = [(float(x), float(y)) for x, y in points]
    draw.polygon(xy, fill=(*rgb, fill_alpha))
    draw.line(xy + xy[:1], fill=(*rgb, 235), width=line, joint="curve")


def render_overlay(stage: Stage, plate: Image.Image | None, *, foot_radius: float, y_squash: float) -> Image.Image:
    """The plate (or a grid at sourceSize) with ground, band, regions, effects, slots and approach points."""
    image = _canvas(plate, stage.source_size)
    width, height = image.size
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    line = max(1, round_half_up(min(width, height) / 320))
    typeface = font(max(10, round_half_up(min(width, height) / 42)))
    to_px = np.array([width, height], float)
    for polygon in stage.ground:
        _polygon(draw, np.asarray(polygon) * to_px, GROUND_RGB, 38, line + 1)
    for v in stage.band or ():
        draw.line([(0, v * height), (width, v * height)], fill=(*BAND_RGB, 170), width=line)
    for effect in stage.effects:
        rgb = WATER_RGB if effect.kind in WATER_KINDS else EFFECT_RGB
        _polygon(draw, np.asarray(effect.polygon) * to_px, rgb, 45, line)
        x, y = np.asarray(effect.polygon).min(axis=0) * to_px
        label(draw, (x + 2 * line, y + line), f"{effect.id} ({effect.kind})", (*rgb, 255), typeface)
    for region in stage.protected:
        rgb = WALK_RGB if region.applies("walk") else GUARD_RGB
        _polygon(draw, np.asarray(region.outline()) * to_px, rgb, 50 if region.applies("walk") else 20, line + 1)
        uses = "all" if region.applies_to is None else "+".join(region.applies_to)
        x, y = np.asarray(region.outline()).min(axis=0) * to_px
        label(draw, (x + 2 * line, y + line), f"{region.id} [{uses}]", (*rgb, 255), typeface)
    points = np.array([slot[2] for slot in stage.slots] + list(stage.approach), float).reshape(-1, 2)
    ok = standable(stage, points, foot_radius=foot_radius, y_squash=y_squash) if len(points) else np.zeros(0, bool)
    rx, ry = foot_radius * width, foot_radius * width * y_squash
    for index, uv in enumerate(points):
        is_slot = index < len(stage.slots)
        rgb = ROLE_RGB[stage.slots[index][1]] if is_slot else BAND_RGB
        text = stage.slots[index][0] if is_slot else f"A{index - len(stage.slots) + 1}"
        x, y = uv * to_px
        draw.ellipse((x - rx, y - ry, x + rx, y + ry), outline=(*(rgb if ok[index] else FAIL_RGB), 255),
                     width=line + 1, fill=(*rgb, 60))
        arm = max(rx, 6 * line)
        draw.line([(x - arm, y), (x + arm, y)], fill=(*rgb, 255), width=line)
        draw.line([(x, y - arm), (x, y + arm)], fill=(*rgb, 255), width=line)
        mark = "" if ok[index] else " NOT STANDABLE"
        label(draw, (x + arm + 2 * line, y - text_height(typeface)), f"{text} ({uv[0]:.2f}, {uv[1]:.2f}){mark}",
              (*(rgb if ok[index] else FAIL_RGB), 255), typeface)
    header = (f"STAGE {width}x{height}  fit {stage.fit}  aspects {stage.aspect_range[0]:.3g}-"
              f"{stage.aspect_range[1]:.3g}  green ground / red no-walk / yellow motion-or-edit / blue water")
    draw.rectangle((0, 0, width, 6 * line + text_height(typeface)), fill=(4, 8, 16, 190))
    label(draw, (3 * line, 2 * line), header, (255, 255, 255, 255), typeface)
    image.alpha_composite(layer)
    return image


def render_layout(stage: Stage, plate: Image.Image | None, layout: dict[str, Any], title: str, status: str, *,
                  foot_radius: float, y_squash: float) -> Image.Image:
    """The viewport as the game shows it: projected plate, ground, no-walk regions, water, UI panels
    and the solved actor boxes with their feet, labels, scale factors and draw order."""
    projection: Projection = layout["_projection"]
    width, height = projection.viewport
    image = Image.new("RGBA", (width, height), (12, 14, 20, 255))
    source = _canvas(plate, stage.source_size)
    sw, sh = source.size
    window = projection.to_uv([[0.0, 0.0], [float(width), float(height)]])
    if stage.fit == "cover":
        box = (max(0.0, window[0][0] * sw), max(0.0, window[0][1] * sh),
               min(float(sw), window[1][0] * sw), min(float(sh), window[1][1] * sh))  # float noise at the edges
        image.alpha_composite(source.resize((width, height), Image.Resampling.LANCZOS, box=box))
    else:
        shown = (max(1, round_half_up(stage.source_size[0] * projection.scale)),
                 max(1, round_half_up(stage.source_size[1] * projection.scale)))
        image.paste(source.resize(shown, Image.Resampling.LANCZOS),
                    (round_half_up(projection.offset[0]), round_half_up(projection.offset[1])))
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    line = max(1, round_half_up(min(width, height) / 360))
    typeface = font(max(10, round_half_up(min(width, height) / 48)))
    for polygon in stage.ground:
        _polygon(draw, projection.to_screen(polygon), GROUND_RGB, 30, line)
    for region in stage.regions_for("walk"):
        _polygon(draw, projection.to_screen(region.outline()), WALK_RGB, 40, line)
    for effect in stage.effects:
        if effect.kind in WATER_KINDS:
            _polygon(draw, projection.to_screen(effect.polygon), WATER_RGB, 40, line)
    for panel in layout["ui"]:
        x0, y0, x1, y1 = panel["box"]
        draw.rectangle((x0, y0, x1, y1), fill=(10, 14, 24, 150), outline=(*UI_RGB, 200), width=line)
        label(draw, (x0 + 3 * line, y0 + 2 * line), f"UI {panel['id']}", (*UI_RGB, 255), typeface)
    radius_px = foot_radius * stage.source_size[0] * projection.scale
    for order, ident in enumerate(layout["drawOrder"], start=1):
        actor = next(item for item in layout["actors"] if item["id"] == ident)
        rgb = ROLE_RGB[actor["role"]] if actor["placed"] and actor["grounded"] else FAIL_RGB
        x0, y0, x1, y1 = actor["box"]
        draw.rectangle((x0, y0, x1, y1), outline=(*rgb, 255), fill=(*rgb, 45), width=line + 1)
        fx, fy = actor["foot"]
        draw.ellipse((fx - radius_px, fy - radius_px * y_squash, fx + radius_px, fy + radius_px * y_squash),
                     outline=(*rgb, 255), width=line + 1)
        draw.line([(fx - 2 * radius_px, fy), (fx + 2 * radius_px, fy)], fill=(*rgb, 255), width=line)
        scale = "unplaced" if actor["scale"] is None else f"x{actor['scale']:.1f}"
        label(draw, (x0 + 2 * line, y0 + 2 * line), f"{ident} {scale} #{order}", (*rgb, 255), typeface)
    header = f"{title}  {width}x{height}  {stage.fit} scale {projection.scale:.3f}  {status.upper()}"
    draw.rectangle((0, height - 6 * line - text_height(typeface), width, height), fill=(4, 8, 16, 200))
    label(draw, (3 * line, height - 3 * line - text_height(typeface)), header, (255, 255, 255, 255), typeface)
    image.alpha_composite(layer)
    return image


# --------------------------------------------------------------------------- CLI

def _actor_specs(pairs: Sequence[str] | None, aspect: float) -> dict[str, ActorSpec]:
    heights = dict(DEFAULT_ACTOR_HEIGHT)
    for text in pairs or []:
        role, separator, value = text.partition("=")
        role = role.strip()
        try:
            number = float(value)
        except ValueError:
            number = math.nan
        if not separator or role not in heights or not math.isfinite(number) or not 0 < number <= 1:
            raise ValueError(f"--actor-height {text!r} must look like hero=0.2 (role hero, enemy or boss; "
                             f"a fraction of the plate height in 0..1)")
        heights[role] = number
    return {role: ActorSpec(height, aspect) for role, height in heights.items()}


def _load_plate(stage_path: Path, stage: Stage, explicit: Path | None) -> tuple[Path | None, list[str]]:
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"--plate {explicit} does not exist")
        return explicit, []
    if stage.plate is None:
        return None, []
    candidate = stage_path.parent / stage.plate
    if candidate.is_file():
        return candidate, []
    return None, [f"plate {stage.plate} named by the stage was not found; validated geometry only"]


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not math.isfinite(args.foot_radius) or not 0 <= args.foot_radius < 0.5:
        raise ValueError("--foot-radius must be a fraction of the plate width in 0..0.5")
    if not math.isfinite(args.y_squash) or not 0 < args.y_squash <= 1:
        raise ValueError("--y-squash must be in (0, 1]")
    if not math.isfinite(args.min_scale) or not 0.1 <= args.min_scale <= 1:
        raise ValueError("--min-scale must be in 0.1..1")
    if not math.isfinite(args.actor_aspect) or args.actor_aspect <= 0:
        raise ValueError("--actor-aspect must be positive")
    final = Path(args.output_dir)
    refuse_existing(final)
    stage_path = Path(args.stage)
    stage = load_stage(stage_path)
    aspects = parse_aspects(args.aspects)
    ui = load_ui(args.ui_profile, args.ui)
    actors = _actor_specs(args.actor_height, args.actor_aspect)
    plate_path, warnings = _load_plate(stage_path, stage, args.plate)
    warnings += list(stage.notes)
    plate = info = None
    if plate_path is not None:
        plate, info = forge_core.load_rgba(plate_path)
    kw = {"foot_radius": args.foot_radius, "y_squash": args.y_squash}

    checks = static_checks(stage, **kw)
    if plate is None:
        checks.append(check("plate matches sourceSize", "skipped", "no plate image"))
    else:
        same = list(plate.size) == list(stage.source_size)
        checks.append(check("plate matches sourceSize", "pass" if same else "fail", list(plate.size),
                            list(stage.source_size)))
        if plate.getchannel("A").getextrema()[0] < 255:
            warnings.append("the plate has transparent pixels; a stage plate is usually opaque")

    solve_kw = {"actors": actors, "min_scale": args.min_scale, **kw}
    layouts, renders = [], []
    for name, ratio in aspects:
        viewport = viewport_for(ratio)
        panels = ui["portrait" if ratio < 1 else "landscape"]
        for formation, roster in formations(stage):
            layout = solve_layout(stage, viewport, roster, ui_panels=panels, **solve_kw)
            result = layout_check(name, ratio, formation, layout, stage)
            checks.append(result)
            suffix = "" if formation == "party-vs-enemies" else "-boss"
            filename = f"layout-{aspect_file_name(name)}{suffix}.png"
            render = render_layout(stage, plate, layout, f"{name} {formation}", result["status"], **kw)
            renders.append((filename, render))
            layout.pop("_projection")
            layouts.append({"aspect": name, "ratio": ratio, "formation": formation, "render": filename, **layout})
    ends = []
    for ratio in sorted(set(stage.aspect_range)):
        viewport = viewport_for(ratio)
        panels = ui["portrait" if ratio < 1 else "landscape"]
        for formation, roster in formations(stage):
            layout = solve_layout(stage, viewport, roster, ui_panels=panels, **solve_kw)
            ends.append({"ratio": ratio, "formation": formation, "viewport": list(viewport),
                         "unplaced": [a["id"] for a in layout["actors"] if not a["placed"]]})
    failed_ends = [end for end in ends if end["unplaced"]]
    checks.append(check("layout solves at both ends of reviewedAspectRange", "fail" if failed_ends else "pass",
                        ends, list(stage.aspect_range)))

    overlay = render_overlay(stage, plate, **kw)
    method = ("validate_stage: stage.v1 structure and cross-field rules; even-odd point-in-polygon tests of each "
              "slot and approach point plus 8 footprint-ellipse samples against ground polygons, walk-protected "
              "regions and ripple polygons; pixel-centre rasters for effect overlaps; a greedy grid-search layout "
              "solver per aspect (cover/contain projection, UI panels, scale ladder, foot-y draw order).")
    failed = [item["id"] for item in checks if item["status"] == "fail"]
    if args.strict and failed:
        raise StageError(f"strict check failed ({'; '.join(failed)}); nothing was written")

    with forge_core.staged_output(final) as stage_dir:
        forge_core.save_png(overlay, stage_dir / "stage-overlay.png")
        for filename, image in renders:
            forge_core.save_png(image, stage_dir / filename)
        inputs = [_local_file_ref(stage_path, final)]
        if plate_path is not None:
            inputs.append(_local_file_ref(plate_path, final, info["sha256"]))
        outputs = [_local_file_ref(stage_dir / name, stage_dir)
                   for name in ["stage-overlay.png", *(filename for filename, _ in renders)]]
        report = {"schema": QA_SCHEMA,
                  **_local_qa_envelope(checks, method=method, not_proven=NOT_PROVEN, inputs=inputs, outputs=outputs,
                                       tool=TOOL, visual=plate is not None),
                  "stage": {"sourceSize": list(stage.source_size), "fit": stage.fit,
                            "reviewedAspectRange": list(stage.aspect_range),
                            "objectPosition": list(stage.object_position), "slots": len(stage.slots)},
                  "settings": {"footRadius": args.foot_radius, "ySquash": args.y_squash, "minScale": args.min_scale,
                               "actors": {role: {"height": spec.height, "aspect": spec.aspect,
                                                 "anchor": list(spec.anchor)} for role, spec in actors.items()},
                               "ui": "file" if args.ui is not None else args.ui_profile,
                               "minActorPx": MIN_ACTOR_PX},
                  "layouts": rounded(layouts, 3),
                  "warnings": warnings}
        forge_core.write_json(stage_dir / "stage-qa.json", report)
    return {"output_dir": str(final.resolve()), "metadata": str((final / "stage-qa.json").resolve()),
            "overlay": str((final / "stage-overlay.png").resolve()),
            "renders": [str((final / filename).resolve()) for filename, _ in renders],
            "status": report["status"], "failed": failed, "_warnings": warnings}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", type=Path, required=True, help="stage.json (generate2dmap.stage.v1).")
    parser.add_argument("--output-dir", type=Path, required=True, help="New folder for the report and renders.")
    parser.add_argument("--plate", type=Path, help="Plate image (default: the stage's plate, relative to the stage).")
    parser.add_argument("--aspects", default=DEFAULT_ASPECTS,
                        help=f"Screen aspects to solve and render (default {DEFAULT_ASPECTS}).")
    parser.add_argument("--ui-profile", choices=tuple(UI_PROFILES), default="battle",
                        help="Built-in reserved UI panels (default battle; none disables UI avoidance).")
    parser.add_argument("--ui", type=Path,
                        help="JSON panels {landscape: [{id, box}], portrait: [...]}, boxes as viewport fractions "
                             "[x0, y0, x1, y1]; replaces --ui-profile.")
    parser.add_argument("--actor-height", action="append", metavar="ROLE=FRACTION",
                        help="Actor box height as a fraction of the plate height (repeatable; defaults hero=0.2, "
                             "enemy=0.18, boss=0.4).")
    parser.add_argument("--actor-aspect", type=float, default=DEFAULT_ACTOR_ASPECT,
                        help="Actor box width / height (default 1.0, a square canvas).")
    parser.add_argument("--foot-radius", type=float, default=DEFAULT_FOOT_RADIUS,
                        help="Footprint radius as a fraction of the plate width (default 0.01; 0 tests the foot "
                             "point only).")
    parser.add_argument("--y-squash", type=float, default=DEFAULT_Y_SQUASH,
                        help="Footprint vertical/horizontal ratio (default 0.58).")
    parser.add_argument("--min-scale", type=float, default=0.5,
                        help="Smallest scale-ladder factor before an actor counts as unplaced (default 0.5).")
    parser.add_argument("--strict", action="store_true", help="Publish nothing and exit 1 when a check fails.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        summary = run(args)
    except (ValueError, OSError, Image.DecompressionBombError) as error:
        print(f"error: {forge_core.ascii_text(str(error) or type(error).__name__)}", file=sys.stderr)
        return 1
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if summary["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
