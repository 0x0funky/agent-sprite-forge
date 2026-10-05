#!/usr/bin/env python3
"""Validate a side-scroll level grammar (generate2dmap.layout.v1) against the controller's physics.

The level is a row of `segments` [x0, x1, kind] in world pixels (y points down).
Kinds map to three classes: ground (standable; aliases floor, solid, crest, c,
slope, s), gap (pit, void, g) and hazard (water, lava, spikes); `kinds` maps
other names, e.g. {"bridge": "ground"}. The ground surface comes from
`surface` (a polyline [[x, y], ...]; a repeated x is a vertical step) or a flat
`groundY`. Props are {id, x, y, w?}: (x, y) is the middle of the base. Props of
kind deck, platform, plank or bridge are standable surfaces {x0, x1, y, thickness}.

Checks (physics: jumpHeight, jumpDistance, maxSlopeDeg, stepUp, colliderSubstep):

  segments         contiguous, no holes or overlaps
  slope_limit      a slope steeper than maxSlopeDeg must rise no more than stepUp
                   (draw a taller one as a vertical ledge)
  step_limit       ledges taller than stepUp need a jump; taller than jumpHeight warns
  deck_thickness   decks at least minDeckThickness px (default colliderSubstep + 1,
                   else 4: a 3-row deck lets a standing body sink through)
  prop_support     every footprint column of a prop rests on ground or a deck within
                   groundTolerance (default 1 px); floating: true opts out
  spawns_grounded  spawns stand on a surface
  gaps_jumpable    the far side of every gap or hazard is reachable from the near side
                   (jump arc: apex jumpHeight, same-height range jumpDistance)
  reachability     from the first spawn: every spawn, exit and (without exits) the
                   level end; unreachable decks warn
  arena_width      each arena (default the level) is at least the viewport width plus
                   camera travel at 16:9, 19.5:9 and every listed viewport

Segment bounds are whole world pixels (columns are pixels). A gap is measured
between the centres of its edge columns, so the widest gap a jump of
jumpDistance crosses is jumpDistance - 1 px wide. Reachability searches the
directed graph of spans linked by walks, drops and jump arcs; it is a side-view
graph, not the top-down collision grid of forge_nav (whose grid_bfs serves the
4-neighbour grids of validate_chunks and the map tools).

Without --output-dir only the one-line summary is printed; with it,
layout-report.json and layout-debug.png are published into the new folder.
Exit 1 when a check fails; --strict-qc then publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)


INPUT_SCHEMA = "generate2dmap.layout.v1"
REPORT_SCHEMA = "generate2dmap.layout_validation.v1"
TOOL = {"name": "validate_layout", "version": forge_core.FORGE_PACKAGE_VERSION}
KIND_CLASSES = {
    "ground": "ground", "floor": "ground", "solid": "ground", "crest": "ground", "c": "ground",
    "slope": "ground", "s": "ground",
    "gap": "gap", "pit": "gap", "void": "gap", "g": "gap",
    "hazard": "hazard", "water": "hazard", "lava": "hazard", "spikes": "hazard",
}
CLASSES = ("ground", "gap", "hazard")
DECK_KINDS = {"deck", "platform", "plank", "bridge"}
DEFAULT_ASPECTS = ("16:9", "19.5:9")
DEFAULT_DECK_THICKNESS = 4
DEFAULT_GROUND_TOLERANCE = 1.0
MAX_SAMPLES = 64
EPSILON = 1e-6
NOT_PROVEN = [
    "The actor is a point at its feet: collider width, head room under decks and ceilings are not modelled.",
    "Jumps follow one parabola (apex jumpHeight, range jumpDistance) with full air control down to zero "
    "horizontal speed; coyote time, double jumps, dashes and variable gravity are not modelled.",
    "Decks are one-way (landable from any arc that reaches their top); they never block an arc.",
    "Floating and sunk props are judged from their base line only; their art and colliders are not inspected.",
    "Camera travel is the value given (default 0); the tool does not simulate the camera.",
]
COLORS = {
    "sky": (226, 236, 246, 255), "ground": (150, 116, 82, 255), "face": (214, 70, 190, 255),
    "gap": (150, 30, 40, 255), "hazard": (60, 110, 210, 255), "deck": (176, 128, 70, 255),
    "bad": (230, 50, 50, 255), "ok": (40, 170, 80, 255), "warn": (240, 150, 30, 255),
    "prop": (90, 90, 100, 255), "spawn": (20, 150, 60, 255), "exit": (40, 90, 220, 255),
    "arc": (0, 170, 200, 255),
}


# --------------------------------------------------------------------------- input model

def _number(value: Any, name: str, *, minimum: float | None = None, exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if minimum is not None and (value <= minimum if exclusive else value < minimum):
        raise ValueError(f"{name} must be {'greater than' if exclusive else 'at least'} {minimum:g}.")
    return float(value)


def _whole(value: Any, name: str) -> int:
    number = _number(value, name)
    if number != int(number):
        raise ValueError(f"{name} must be a whole number of world pixels (got {number:g}).")
    return int(number)


@dataclass
class Physics:
    jump_height: float
    jump_distance: float
    max_slope_deg: float
    step_up: float
    collider_substep: float | None
    min_deck_thickness: float
    ground_tolerance: float

    def reach(self, rise: np.ndarray | float) -> np.ndarray:
        """Longest horizontal distance that lands `rise` px higher (negative: lower) on the falling branch."""
        rise = np.asarray(rise, dtype=float)
        return self.jump_distance / 2 * (1 + np.sqrt(np.maximum(0.0, 1 - rise / self.jump_height)))

    def height(self, t: np.ndarray) -> np.ndarray:
        """Height above the take-off point after travelling `t` px along the full-speed arc."""
        return self.jump_height * (1 - (2 * np.asarray(t, dtype=float) / self.jump_distance - 1) ** 2)


@dataclass
class Span:
    id: str
    kind: str  # "ground" or "deck"
    columns: np.ndarray  # world column indices (int)
    heights: np.ndarray  # surface y per column
    source: str | None = None  # deck prop id
    reachable: bool = False
    edges: list[int] = field(default_factory=list)

    @property
    def x0(self) -> int:
        return int(self.columns[0])

    @property
    def x1(self) -> int:
        return int(self.columns[-1]) + 1


@dataclass
class Level:
    x0: int
    x1: int
    classes: np.ndarray  # per column: 0 ground, 1 gap, 2 hazard
    heights: np.ndarray  # surface y per column (NaN where not ground)
    face: np.ndarray  # bool per column: ground too steep to stand on
    physics: Physics
    segments: list[tuple[int, int, str, str]]
    decks: list[dict[str, Any]]
    props: list[dict[str, Any]]
    spawns: list[dict[str, Any]]
    exits: list[dict[str, Any]]
    pieces: list[dict[str, Any]]

    @property
    def width(self) -> int:
        return self.x1 - self.x0


def parse_physics(data: Any) -> Physics:
    if not isinstance(data, dict):
        raise ValueError("physics must be an object with jumpHeight, jumpDistance, maxSlopeDeg and stepUp.")
    substep = data.get("colliderSubstep")
    substep = None if substep is None else _number(substep, "physics.colliderSubstep", minimum=0, exclusive=True)
    if "minDeckThickness" in data:
        deck = _number(data["minDeckThickness"], "physics.minDeckThickness", minimum=0)
    else:
        deck = math.floor(substep) + 1 if substep is not None else DEFAULT_DECK_THICKNESS
    slope = _number(data.get("maxSlopeDeg"), "physics.maxSlopeDeg", minimum=0)
    if slope > 90:
        raise ValueError("physics.maxSlopeDeg must be at most 90.")
    return Physics(_number(data.get("jumpHeight"), "physics.jumpHeight", minimum=0, exclusive=True),
                   _number(data.get("jumpDistance"), "physics.jumpDistance", minimum=0, exclusive=True),
                   slope, _number(data.get("stepUp"), "physics.stepUp", minimum=0), substep, deck,
                   _number(data.get("groundTolerance", DEFAULT_GROUND_TOLERANCE), "physics.groundTolerance",
                           minimum=0))


def _surface(document: dict[str, Any]) -> np.ndarray:
    """The ground polyline as an (n, 2) array; a flat groundY becomes two points far apart."""
    if "surface" in document and "groundY" in document:
        raise ValueError("give either surface (a polyline) or groundY (flat ground), not both.")
    if "groundY" in document:
        y = _number(document["groundY"], "groundY")
        return np.array([[-math.inf, y], [math.inf, y]])
    points = document.get("surface")
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError("the layout needs surface (at least two [x, y] points) or groundY.")
    array = np.array([[_number(value, f"surface[{index}]") for value in point] if isinstance(point, list)
                      and len(point) == 2 else [math.nan, math.nan] for index, point in enumerate(points)])
    if not np.isfinite(array).all():
        raise ValueError("every surface point must be [x, y] with finite numbers.")
    if (np.diff(array[:, 0]) < 0).any():
        raise ValueError("surface x values must not decrease (repeat an x for a vertical step).")
    return array


def surface_at(polyline: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """Surface y at each x; at a repeated x (a vertical step) the later point applies from that x on."""
    xs = np.asarray(xs, dtype=float)
    px, py = polyline[:, 0], polyline[:, 1]
    index = np.clip(np.searchsorted(px, xs, side="right") - 1, 0, len(px) - 2)
    x0, x1 = px[index], px[index + 1]
    y0, y1 = py[index], py[index + 1]
    span = x1 - x0
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.where(np.isfinite(span) & (span > 0), (xs - x0) / span, 0.0)
    flat = ~np.isfinite(x0) | ~np.isfinite(x1)
    return np.where(flat, y0, y0 + (y1 - y0) * np.clip(t, 0, 1))


def parse_level(document: Any) -> Level:
    if not isinstance(document, dict):
        raise ValueError("The layout must be a JSON object.")
    if document.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"schema must be {INPUT_SCHEMA!r} (got {document.get('schema')!r}).")
    physics = parse_physics(document.get("physics"))
    mapping = dict(KIND_CLASSES)
    custom = document.get("kinds", {})
    if not isinstance(custom, dict):
        raise ValueError("kinds must map segment kinds to ground, gap or hazard.")
    for name, target in custom.items():
        if target not in CLASSES:
            raise ValueError(f"kinds.{name} must be one of {', '.join(CLASSES)}.")
        mapping[name] = target
    entries = document.get("segments")
    if not isinstance(entries, list) or not entries:
        raise ValueError("segments must be a non-empty list of [x0, x1, kind].")
    segments = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, list) or len(entry) != 3 or not isinstance(entry[2], str):
            raise ValueError(f"segments[{index}] must be [x0, x1, kind].")
        x0, x1 = _whole(entry[0], f"segments[{index}][0]"), _whole(entry[1], f"segments[{index}][1]")
        if x1 <= x0:
            raise ValueError(f"segments[{index}] must have x1 > x0.")
        if entry[2] not in mapping:
            raise ValueError(f"segments[{index}] kind {entry[2]!r} is unknown; use one of {sorted(KIND_CLASSES)} "
                             f"or map it with kinds, e.g. \"kinds\": {{\"{entry[2]}\": \"ground\"}}.")
        segments.append((x0, x1, entry[2], mapping[entry[2]]))
    level_x0 = min(segment[0] for segment in segments)
    level_x1 = max(segment[1] for segment in segments)
    classes = np.full(level_x1 - level_x0, 1, np.int8)  # undefined columns count as gaps
    for x0, x1, _, cls in reversed(segments):
        classes[x0 - level_x0:x1 - level_x0] = CLASSES.index(cls)
    polyline = _surface(document)
    centers = np.arange(level_x0, level_x1) + 0.5
    ground = classes == 0
    if ground.any() and (polyline[0, 0] > centers[ground][0] or polyline[-1, 0] < centers[ground][-1]):
        raise ValueError(f"surface must cover the ground from x={level_x0} to x={level_x1}.")
    heights = np.where(ground, surface_at(polyline, centers), np.nan)
    pieces, face = _classify_pieces(polyline, centers, ground, physics)
    decks, props = [], []
    for index, prop in enumerate(document.get("props", []) or []):
        if not isinstance(prop, dict):
            raise ValueError(f"props[{index}] must be an object.")
        ident = str(prop.get("id", f"prop-{index}"))
        if str(prop.get("kind", "")).lower() in DECK_KINDS:
            if "x0" in prop or "x1" in prop:
                x0 = _number(prop.get("x0"), f"deck {ident} x0")
                x1 = _number(prop.get("x1"), f"deck {ident} x1")
            else:
                center, width = _number(prop.get("x"), f"deck {ident} x"), _number(prop.get("w"), f"deck {ident} w")
                x0, x1 = center - width / 2, center + width / 2
            if x1 <= x0:
                raise ValueError(f"deck {ident} needs x1 > x0.")
            if "thickness" not in prop:
                raise ValueError(f"deck {ident} needs thickness (rows of solid deck under its top).")
            decks.append({"id": ident, "x0": x0, "x1": x1, "y": _number(prop.get("y"), f"deck {ident} y"),
                          "thickness": _number(prop["thickness"], f"deck {ident} thickness", minimum=0)})
        else:
            width = prop.get("w", prop.get("width", 0))
            props.append({"id": ident, "x": _number(prop.get("x"), f"prop {ident} x"),
                          "y": _number(prop.get("y"), f"prop {ident} y"),
                          "w": _number(width, f"prop {ident} w", minimum=0),
                          "floating": bool(prop.get("floating", False))})
    spawns = [_point(item, f"spawns[{index}]", needs_y=True) for index, item in enumerate(document.get("spawns", [])
                                                                                          or [])]
    exits = [_point(item, f"exits[{index}]", needs_y=False) for index, item in enumerate(document.get("exits", [])
                                                                                         or [])]
    return Level(level_x0, level_x1, classes, heights, face, physics, segments, decks, props, spawns, exits, pieces)


def _point(item: Any, name: str, *, needs_y: bool) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"{name} must be an object with id, x and y.")
    point = {"id": str(item.get("id", name)), "x": _number(item.get("x"), f"{name}.x")}
    if needs_y or "y" in item:
        point["y"] = _number(item.get("y"), f"{name}.y")
    return point


def _classify_pieces(polyline: np.ndarray, centers: np.ndarray, ground: np.ndarray,
                     physics: Physics) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Ledges (vertical steps above stepUp) and steep faces (slopes above maxSlopeDeg rising above stepUp)."""
    pieces = []
    face = np.zeros(len(centers), bool)
    limit = math.tan(math.radians(min(physics.max_slope_deg, 89.999)))
    for (xa, ya), (xb, yb) in zip(polyline[:-1], polyline[1:]):
        if not (math.isfinite(xa) and math.isfinite(xb)):
            continue
        rise = abs(yb - ya)
        if xb == xa:
            # vertical step at xa: the columns either side must both be ground for it to matter
            left, right = np.searchsorted(centers, xa) - 1, np.searchsorted(centers, xa)
            if rise > physics.step_up + EPSILON and 0 <= left and right < len(centers) and ground[left] \
                    and ground[right]:
                pieces.append({"kind": "ledge", "x": float(xa), "height": float(rise),
                               "up": "east" if yb < ya else "west"})
            continue
        inside = (centers >= xa) & (centers <= xb) & ground
        if not inside.any():
            continue
        if rise / (xb - xa) > limit + EPSILON and rise > physics.step_up + EPSILON:
            face |= inside
            pieces.append({"kind": "steep", "x0": float(xa), "x1": float(xb), "rise": float(rise),
                           "angle_deg": round(math.degrees(math.atan2(rise, xb - xa)), 3)})
    return pieces, face


# --------------------------------------------------------------------------- spans and jumps

def _covered_columns(x0: float, x1: float) -> tuple[int, int]:
    """First and last column whose centre lies in the half-open [x0, x1) (last < first when none does)."""
    return math.ceil(x0 - 0.5), math.ceil(x1 - 0.5) - 1


def build_spans(level: Level) -> list[Span]:
    """Standable runs: ground columns linked by walkable transitions, and each deck."""
    physics = level.physics
    standable = (level.classes == 0) & ~level.face
    tolerance = physics.step_up + math.tan(math.radians(min(physics.max_slope_deg, 89.999))) + EPSILON
    steps = np.abs(np.diff(level.heights))
    linked = standable[:-1] & standable[1:] & (steps <= tolerance)
    spans: list[Span] = []
    columns = np.flatnonzero(standable)
    if columns.size:
        breaks = np.flatnonzero(~linked[columns[:-1]] | (np.diff(columns) != 1)) + 1
        for number, run in enumerate(np.split(columns, breaks)):
            spans.append(Span(f"ground-{number}", "ground", run + level.x0, level.heights[run]))
    for deck in level.decks:
        start, stop = _covered_columns(deck["x0"], deck["x1"])
        if stop < start:
            continue
        run = np.arange(start, stop + 1)
        spans.append(Span(f"deck-{deck['id']}", "deck", run, np.full(run.shape, deck["y"]), deck["id"]))
    return spans


def _samples(span: Span, lo: float, hi: float) -> np.ndarray:
    """Up to MAX_SAMPLES column positions of `span` inside [lo, hi], always keeping its ends."""
    mask = (span.columns + 0.5 >= lo) & (span.columns + 0.5 <= hi)
    chosen = np.flatnonzero(mask)
    if chosen.size > MAX_SAMPLES:
        chosen = chosen[np.unique(np.linspace(0, chosen.size - 1, MAX_SAMPLES).round().astype(int))]
    return chosen


def find_jump(level: Level, source: Span, target: Span, max_reach: float) -> dict[str, Any] | None:
    """A take-off column on `source` and a landing column on `target` the arc connects, or None."""
    physics = level.physics
    take = _samples(source, target.x0 - max_reach, target.x1 + max_reach)
    land = _samples(target, source.x0 - max_reach, source.x1 + max_reach)
    if not take.size or not land.size:
        return None
    xa, ya = source.columns[take] + 0.5, source.heights[take]
    xb, yb = target.columns[land] + 0.5, target.heights[land]
    distance = np.abs(xb[None, :] - xa[:, None])
    rise = ya[:, None] - yb[None, :]
    feasible = (rise <= physics.jump_height + EPSILON) & (distance <= physics.reach(rise) + EPSILON)
    candidates = np.argwhere(feasible)
    if not candidates.size:
        return None
    order = np.lexsort((np.abs(rise[feasible]), distance[feasible]))
    for i, j in candidates[order][:32]:
        if _clear(level, xa[i], ya[i], xb[j], yb[j]):
            return {"from": [float(xa[i]), float(ya[i])], "to": [float(xb[j]), float(yb[j])],
                    "distance": float(distance[i, j]), "rise": float(rise[i, j]),
                    "reach": float(physics.reach(rise[i, j]))}
    return None


def arc_points(physics: Physics, start: tuple[float, float], end: tuple[float, float], count: int = 33) -> np.ndarray:
    """The arc from `start` that lands at `end` with horizontal speed slowed to fit (y down)."""
    (xa, ya), (xb, yb) = start, end
    rise = ya - yb
    full = float(physics.reach(rise))
    t = np.linspace(0, full, count)
    xs = xa + np.sign(xb - xa) * t * (abs(xb - xa) / full if full > 0 else 0)
    return np.column_stack([xs, ya - physics.height(t)])


def _clear(level: Level, xa: float, ya: float, xb: float, yb: float) -> bool:
    """The arc stays above solid ground at every column strictly between take-off and landing."""
    lo, hi = sorted((xa, xb))
    columns = np.arange(math.floor(lo + 0.5), math.ceil(hi - 0.5)) - level.x0
    columns = columns[(columns >= 0) & (columns < level.width)]
    columns = columns[(columns + level.x0 + 0.5 > lo) & (columns + level.x0 + 0.5 < hi)]
    solid = columns[level.classes[columns] == 0]
    if not solid.size:
        return True
    physics = level.physics
    full = float(physics.reach(ya - yb))
    progress = np.abs(solid + level.x0 + 0.5 - xa) / max(abs(xb - xa), EPSILON)
    actor_y = ya - physics.height(progress * full)
    return bool((actor_y <= level.heights[solid] + EPSILON).all())


def link_spans(level: Level, spans: list[Span]) -> None:
    """Directed jump/drop/walk-across edges between spans (stored as indices in Span.edges)."""
    if not spans:
        return
    tops = np.concatenate([span.heights for span in spans])
    max_reach = float(level.physics.reach(tops.min() - tops.max()))
    for i, source in enumerate(spans):
        for j, target in enumerate(spans):
            if i == j or target.x0 - source.x1 > max_reach or source.x0 - target.x1 > max_reach:
                continue
            if find_jump(level, source, target, max_reach) is not None:
                source.edges.append(j)


def reach_from(spans: list[Span], starts: list[int]) -> set[int]:
    reached = set(starts)
    queue = deque(starts)
    while queue:
        for neighbour in spans[queue.popleft()].edges:
            if neighbour not in reached:
                reached.add(neighbour)
                queue.append(neighbour)
    return reached


def span_at(spans: list[Span], x: float, y: float | None, tolerance: float) -> int | None:
    """The span under (x, y): the surface within tolerance, or the first span covering x when y is None."""
    column = math.floor(x)
    best = None
    for index, span in enumerate(spans):
        if not span.x0 <= column < span.x1:
            continue
        top = span.heights[column - span.x0]
        if y is None:
            if best is None or span.kind == "ground":
                best = index
            continue
        if abs(top - y) <= tolerance + EPSILON:
            return index
    return best


# --------------------------------------------------------------------------- checks

def _check(ident: str, status: str, value: Any = None, threshold: Any = None) -> dict[str, Any]:
    return {"id": ident, "status": status, "value": value, "threshold": threshold}


def check_segments(level: Level) -> dict[str, Any]:
    problems = []
    ordered = sorted(level.segments)
    for (a0, a1, kind_a, _), (b0, b1, kind_b, _) in zip(ordered, ordered[1:]):
        if b0 > a1:
            problems.append(f"hole between x={a1} and x={b0} (no segment; counted as a gap)")
        elif b0 < a1:
            problems.append(f"segments [{a0}, {a1}, {kind_a}] and [{b0}, {b1}, {kind_b}] overlap")
    return _check("segments", "fail" if problems else "pass", problems)


def check_props(level: Level) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Every footprint column of a prop needs ground or a deck within the tolerance under its base."""
    tolerance = level.physics.ground_tolerance
    results, floating, sunk = [], [], []
    for prop in level.props:
        if prop["w"] > 0:
            start, stop = _covered_columns(prop["x"] - prop["w"] / 2, prop["x"] + prop["w"] / 2)
            columns = np.arange(start, max(start, stop) + 1)
        else:
            columns = np.array([math.floor(prop["x"])])
        index = columns - level.x0
        inside = (index >= 0) & (index < level.width)
        ground = np.full(columns.shape, np.nan)
        ground[inside] = np.where(level.classes[index[inside]] == 0, level.heights[index[inside]], np.nan)
        supported = np.abs(ground - prop["y"]) <= tolerance + EPSILON
        for deck in level.decks:
            covered = (columns + 0.5 >= deck["x0"]) & (columns + 0.5 < deck["x1"])
            supported |= covered & (abs(deck["y"] - prop["y"]) <= tolerance + EPSILON)
        sunk_columns = ~supported & (ground < prop["y"] - tolerance - EPSILON)
        floating_columns = ~supported & ~sunk_columns
        status = "allowed" if prop["floating"] else (
            "floating" if floating_columns.any() else ("sunk" if sunk_columns.any() else "supported"))
        gaps = ground[floating_columns] - prop["y"]
        result = {"id": prop["id"], "x": prop["x"], "y": prop["y"], "w": prop["w"], "status": status,
                  "floating_columns": int(floating_columns.sum()), "sunk_columns": int(sunk_columns.sum()),
                  "max_gap_px": None if not np.isfinite(gaps).any() else float(np.nanmax(gaps))}
        results.append(result)
        if status == "floating":
            gap_text = "over a gap" if result["max_gap_px"] is None else f"up to {result['max_gap_px']:g} px above"
            floating.append(f"prop {prop['id']} at x={prop['x']:g}: {result['floating_columns']} of {columns.size} "
                            f"columns float ({gap_text} the ground)")
        elif status == "sunk":
            sunk.append(f"prop {prop['id']} at x={prop['x']:g}: {result['sunk_columns']} columns sit below the "
                        f"ground surface")
    status = "fail" if floating else ("warn" if sunk else "pass")
    return _check("prop_support", status, floating + sunk, {"ground_tolerance_px": tolerance}), results


def check_decks(level: Level) -> dict[str, Any]:
    minimum = level.physics.min_deck_thickness
    thin = [f"deck {deck['id']} is {deck['thickness']:g} px thick; a standing body sinks through decks under "
            f"{minimum:g} px" for deck in level.decks if deck["thickness"] < minimum - EPSILON]
    return _check("deck_thickness", "fail" if thin else "pass", thin, {"min_px": minimum})


def check_slopes(level: Level) -> tuple[dict[str, Any], dict[str, Any]]:
    physics = level.physics
    steep = [f"slope x={piece['x0']:g}..{piece['x1']:g} is {piece['angle_deg']:g} deg over {piece['rise']:g} px "
             f"(max {physics.max_slope_deg:g} deg, or rises up to stepUp {physics.step_up:g} px); make it a "
             f"vertical ledge or flatten it" for piece in level.pieces if piece["kind"] == "steep"]
    ledges = [piece for piece in level.pieces if piece["kind"] == "ledge"]
    tall = [f"ledge at x={piece['x']:g} is {piece['height']:g} px, above jumpHeight {physics.jump_height:g}: "
            f"impassable going {piece['up']}" for piece in ledges if piece["height"] > physics.jump_height + EPSILON]
    return (_check("slope_limit", "fail" if steep else "pass", steep,
                   {"max_slope_deg": physics.max_slope_deg, "step_up_px": physics.step_up}),
            _check("step_limit", "warn" if tall else "pass",
                   {"ledges": [{"x": piece["x"], "height": piece["height"], "up": piece["up"]} for piece in ledges],
                    "impassable": tall},
                   {"step_up_px": physics.step_up, "jump_height_px": physics.jump_height}))


def check_gaps(level: Level, spans: list[Span]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The far side of each gap or hazard run is reachable from the near side (possibly over decks)."""
    physics = level.physics
    runs, results, failures = [], [], []
    blocked = level.classes != 0
    columns = np.flatnonzero(blocked)
    if columns.size:
        runs = np.split(columns, np.flatnonzero(np.diff(columns) != 1) + 1)
    ground_spans = [index for index, span in enumerate(spans) if span.kind == "ground"]
    for run in runs:
        x0, x1 = int(run[0]) + level.x0, int(run[-1]) + 1 + level.x0
        kind = CLASSES[int(level.classes[run[0]])]
        near = next((i for i in ground_spans if spans[i].x1 == x0), None)
        far = next((i for i in ground_spans if spans[i].x0 == x1), None)
        result = {"x0": x0, "x1": x1, "width": x1 - x0, "kind": kind, "near": None, "far": None,
                  "direct": None, "crossable": None}
        if near is None or far is None:
            result["note"] = "level edge or no standable ground beside it; not judged"
            results.append(result)
            continue
        result["near"], result["far"] = spans[near].id, spans[far].id
        direct = find_jump(level, spans[near], spans[far], float("inf"))
        rise = float(spans[near].heights[-1] - spans[far].heights[0])
        result["direct"] = direct or {"distance": x1 - x0 + 1.0, "rise": rise,
                                      "reach": float(physics.reach(rise)), "feasible": False}
        if direct:
            result["direct"]["feasible"] = True
        result["crossable"] = far in reach_from(spans, [near])
        if not result["crossable"]:
            failures.append(f"{kind} x={x0}..{x1} ({x1 - x0} px) cannot be crossed: best reach "
                            f"{result['direct']['reach']:.1f} px at rise {rise:g} px, jumpHeight "
                            f"{physics.jump_height:g}, jumpDistance {physics.jump_distance:g}")
        results.append(result)
    return _check("gaps_jumpable", "fail" if failures else "pass", failures,
                  {"jump_height_px": physics.jump_height, "jump_distance_px": physics.jump_distance}), results


def check_reachability(level: Level, spans: list[Span]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    tolerance = level.physics.ground_tolerance
    ungrounded, homes = [], []
    for spawn in level.spawns:
        index = span_at(spans, spawn["x"], spawn["y"], tolerance)
        homes.append(index)
        if index is None:
            ungrounded.append(f"spawn {spawn['id']} at ({spawn['x']:g}, {spawn['y']:g}) is not on ground or a deck")
    grounded = _check("spawns_grounded", "fail" if ungrounded else ("pass" if level.spawns else "skipped"),
                      ungrounded or (None if level.spawns else "no spawns"), {"ground_tolerance_px": tolerance})
    if not level.spawns or homes[0] is None:
        reason = "no spawns" if not level.spawns else "the first spawn is not on a surface"
        return grounded, _check("reachability", "fail" if level.spawns else "skipped", reason), {}
    reached = reach_from(spans, [homes[0]])
    for index in reached:
        spans[index].reachable = True
    failures, warnings, targets = [], [], []
    for spawn, home in zip(level.spawns[1:], homes[1:]):
        if home is not None:
            targets.append(("spawn", spawn["id"], home))
    for exit_point in level.exits:
        home = span_at(spans, exit_point["x"], exit_point.get("y"), tolerance)
        if home is None:
            failures.append(f"exit {exit_point['id']} at x={exit_point['x']:g} is not on a surface")
        else:
            targets.append(("exit", exit_point["id"], home))
    if not level.exits:
        ground = [index for index, span in enumerate(spans) if span.kind == "ground"]
        if ground:
            last = max(ground, key=lambda index: spans[index].x1)
            targets.append(("level end", f"x={spans[last].x1}", last))
    for kind, ident, home in targets:
        if home not in reached:
            failures.append(f"{kind} {ident} ({spans[home].id}) cannot be reached from spawn {level.spawns[0]['id']}")
    for index, span in enumerate(spans):
        if span.kind == "deck" and index not in reached:
            warnings.append(f"deck {span.source} cannot be reached")
    status = "fail" if failures else ("warn" if warnings else "pass")
    summary = {"start": level.spawns[0]["id"], "reached_spans": sorted(spans[i].id for i in reached),
               "unreached_spans": sorted(span.id for i, span in enumerate(spans) if i not in reached)}
    return grounded, _check("reachability", status, failures + warnings), summary


def parse_viewports(document: dict[str, Any]) -> tuple[list[tuple[str, float | None, float | None]], float | None]:
    """(label, width or None, aspect or None) for every viewport plus the default 16:9 and 19.5:9."""
    entries = document.get("viewports", []) or []
    camera = document.get("camera", {}) or {}
    if not isinstance(camera, dict):
        raise ValueError("camera must be an object such as {\"viewHeight\": 270, \"travel\": 80}.")
    view_height = camera.get("viewHeight")
    view_height = None if view_height is None else _number(view_height, "camera.viewHeight", minimum=0,
                                                           exclusive=True)
    sizes, aspects = [], []
    for index, entry in enumerate(entries):
        if isinstance(entry, list) and len(entry) == 2:
            width = _number(entry[0], f"viewports[{index}][0]", minimum=0, exclusive=True)
            height = _number(entry[1], f"viewports[{index}][1]", minimum=0, exclusive=True)
            sizes.append((f"{width:g}x{height:g}", width, None))
            view_height = view_height or height
        elif isinstance(entry, str) and re.fullmatch(r"[0-9]+(\.[0-9]+)?:[0-9]+(\.[0-9]+)?", entry):
            aspects.append(entry)
        else:
            raise ValueError(f"viewports[{index}] must be [width, height] or an aspect such as 19.5:9.")
    for aspect in DEFAULT_ASPECTS:
        if aspect not in aspects:
            aspects.append(aspect)
    for aspect in aspects:
        across, down = (float(part) for part in aspect.split(":"))
        sizes.append((aspect, None, across / down))
    return sizes, view_height


def check_arenas(document: dict[str, Any], level: Level) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    viewports, view_height = parse_viewports(document)
    camera = document.get("camera", {}) or {}
    default_travel = _number(camera.get("travel", 0), "camera.travel", minimum=0)
    arenas = document.get("arenas") or [{"id": "level", "x0": level.x0, "x1": level.x1}]
    results, failures, skipped = [], [], []
    for index, arena in enumerate(arenas):
        if not isinstance(arena, dict):
            raise ValueError(f"arenas[{index}] must be an object with id, x0 and x1.")
        ident = str(arena.get("id", f"arena-{index}"))
        x0, x1 = _number(arena.get("x0"), f"arena {ident} x0"), _number(arena.get("x1"), f"arena {ident} x1")
        travel = _number(arena.get("travel", default_travel), f"arena {ident} travel", minimum=0)
        for label, width, aspect in viewports:
            view = width if width is not None else (None if view_height is None else view_height * aspect)
            if view is None:
                skipped.append(f"{ident} at {label}: no camera.viewHeight or [w, h] viewport")
                continue
            need = view + travel
            ok = x1 - x0 >= need - EPSILON
            results.append({"arena": ident, "viewport": label, "arena_width": x1 - x0, "view_width": round(view, 3),
                            "travel": travel, "needed": round(need, 3), "status": "pass" if ok else "fail"})
            if not ok:
                failures.append(f"arena {ident} is {x1 - x0:g} px wide; {label} needs {need:.1f} px "
                                f"(view {view:.1f} + travel {travel:g})")
    status = "fail" if failures else ("pass" if results else "skipped")
    value = failures if failures else (results if results else skipped)
    return _check("arena_width", status, value, {"aspects": list(DEFAULT_ASPECTS), "view_height": view_height}), \
        results


# --------------------------------------------------------------------------- validation

def validate(document: Any) -> dict[str, Any]:
    level = parse_level(document)
    spans = build_spans(level)
    link_spans(level, spans)
    checks = [check_segments(level)]
    slope_check, step_check = check_slopes(level)
    checks += [slope_check, step_check, check_decks(level)]
    prop_check, prop_results = check_props(level)
    checks.append(prop_check)
    grounded, reachability, reach_summary = check_reachability(level, spans)
    gap_check, gaps = check_gaps(level, spans)
    arena_check, arenas = check_arenas(document, level)
    checks += [grounded, gap_check, reachability, arena_check]
    statuses = {check["status"] for check in checks}
    return {
        "status": "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "pass"),
        "extent": {"x0": level.x0, "x1": level.x1},
        "physics": {"jumpHeight": level.physics.jump_height, "jumpDistance": level.physics.jump_distance,
                    "maxSlopeDeg": level.physics.max_slope_deg, "stepUp": level.physics.step_up,
                    "colliderSubstep": level.physics.collider_substep,
                    "minDeckThickness": level.physics.min_deck_thickness,
                    "groundTolerance": level.physics.ground_tolerance},
        "spans": [{"id": span.id, "kind": span.kind, "x0": span.x0, "x1": span.x1,
                   "top_min": float(span.heights.min()), "top_max": float(span.heights.max()),
                   "reachable": span.reachable} for span in spans],
        "pieces": level.pieces, "gaps": gaps, "props": prop_results, "arenas": arenas,
        "reachability": reach_summary, "checks": checks,
        "_level": level, "_spans": spans,
    }


def messages_of(report: dict[str, Any], status: str = "fail", limit: int = 8) -> list[str]:
    """The first messages of checks with `status` (fail or warn), for the console summary."""
    found = []
    for check in report["checks"]:
        if check["status"] != status:
            continue
        value = check["value"]
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, list):
            items = [item for item in value if isinstance(item, str)]
        elif isinstance(value, dict):
            items = [item for key in ("problems", "warnings", "impassable") for item in value.get(key) or []]
            if value.get("unreached"):
                items.append(f"unreachable chunks {value['unreached']}")
        else:
            items = []
        found += [f"{check['id']}: {item}" for item in items]
    return [forge_core.ascii_text(item) for item in found[:limit]]


# --------------------------------------------------------------------------- debug image

def render_debug(report: dict[str, Any]) -> Image.Image:
    """Side view: ground, gaps, hazards, steep faces, decks, props, spawns, exits, reachable tops and gap arcs."""
    level: Level = report["_level"]
    spans: list[Span] = report["_spans"]
    physics = level.physics
    tops = [level.heights[np.isfinite(level.heights)]] + [np.array([deck["y"]]) for deck in level.decks]
    tops += [np.array([item["y"]]) for item in level.props + level.spawns]
    values = np.concatenate([array for array in tops if array.size]) if any(a.size for a in tops) else np.array([0.0])
    y_top = math.floor(values.min() - physics.jump_height - 24)
    y_bottom = math.ceil(values.max() + 40)
    width, height = level.width, max(1, y_bottom - y_top)
    canvas = np.empty((height, width, 4), np.uint8)
    canvas[...] = COLORS["sky"]
    rows = np.arange(height)[:, None] + y_top
    ground = (level.classes == 0)[None, :] & (rows >= np.nan_to_num(level.heights, nan=np.inf)[None, :])
    canvas[ground] = COLORS["ground"]
    face = ground & level.face[None, :]
    canvas[face] = COLORS["face"]
    canvas[height - 24:, level.classes == 2] = COLORS["hazard"]
    canvas[height - 6:, level.classes == 1] = COLORS["gap"]
    image = Image.fromarray(canvas)
    draw = ImageDraw.Draw(image)

    def at(x: float, y: float) -> tuple[float, float]:
        return x - level.x0, y - y_top

    for deck in level.decks:
        color = COLORS["bad"] if deck["thickness"] < physics.min_deck_thickness - EPSILON else COLORS["deck"]
        draw.rectangle([at(deck["x0"], deck["y"]),
                        at(max(deck["x0"], deck["x1"] - 1), deck["y"] + max(deck["thickness"], 1) - 1)], fill=color)
    for span in spans:
        color = COLORS["ok"] if span.reachable else COLORS["bad"]
        points = [at(x + 0.5, y) for x, y in zip(span.columns, span.heights)]
        if len(points) == 1:
            points.append((points[0][0] + 1, points[0][1]))
        draw.line(points, fill=color, width=2)
    for gap in report["gaps"]:
        direct = gap.get("direct")
        if not direct or "from" not in direct:
            if direct and gap.get("near"):
                near = next(span for span in spans if span.id == gap["near"])
                start = (near.x1 - 0.5, float(near.heights[-1]))
                reach = float(physics.reach(0.0))
                end = (start[0] + reach, start[1])
                draw.line([at(*point) for point in arc_points(physics, start, end)], fill=COLORS["bad"], width=1)
            continue
        draw.line([at(*point) for point in arc_points(physics, tuple(direct["from"]), tuple(direct["to"]))],
                  fill=COLORS["arc"], width=1)
    for prop in report["props"]:
        color = {"floating": COLORS["bad"], "sunk": COLORS["warn"]}.get(prop["status"], COLORS["prop"])
        half = max(prop["w"] / 2, 1)
        draw.rectangle([at(prop["x"] - half, prop["y"] - 12), at(prop["x"] + half - 1, prop["y"] - 1)],
                       outline=color, width=2)
    for spawn in level.spawns:
        x, y = at(spawn["x"], spawn["y"])
        draw.polygon([(x, y - 14), (x - 6, y), (x + 6, y)], fill=COLORS["spawn"])
    for exit_point in level.exits:
        home = span_at(spans, exit_point["x"], exit_point.get("y"), physics.ground_tolerance)
        surface = (spans[home].heights[math.floor(exit_point["x"]) - spans[home].x0] if home is not None
                   else y_top + height - 30)
        x, y = at(exit_point["x"], exit_point.get("y", surface))
        draw.rectangle([x - 4, y - 16, x + 4, y], fill=COLORS["exit"])
    if width > 4096:
        image = image.resize((4096, max(1, round(height * 4096 / width))), Image.Resampling.BOX)
    elif width < 512:
        factor = max(1, 512 // width)
        image = image.resize((width * factor, height * factor), Image.Resampling.NEAREST)
    return image


# --------------------------------------------------------------------------- CLI

def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        document = forge_core.read_json(args.layout, strict=True)  # D28
    except ValueError as error:
        raise ValueError(f"{Path(args.layout).name} is not valid JSON ({error}).") from None
    report = validate(document)
    failed = [check["id"] for check in report["checks"] if check["status"] == "fail"]
    summary: dict[str, Any] = {"status": report["status"], "spans": len(report["spans"]), "failed": failed,
                               "problems": messages_of(report), "warnings": messages_of(report, "warn")}
    if args.output_dir is None:
        return summary, 1 if failed else 0
    if args.strict_qc and failed:
        raise ValueError(f"strict QC failed ({', '.join(failed)}); nothing was written.")
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        forge_core.save_png(render_debug(report), stage / "layout-debug.png")
        public = {key: value for key, value in report.items() if not key.startswith("_") and key != "checks"}
        record = {"schema": REPORT_SCHEMA, "tool": dict(TOOL), **public,
                  "qa": {"status": report["status"],
                         "method": "validate_layout: per-column ground from segments and the surface polyline; "
                                   "standable spans linked by a parabolic jump arc (apex jumpHeight, range "
                                   "jumpDistance) checked for clearance over solid columns; breadth-first search "
                                   "from the first spawn; base-line support for props.",
                         "notProven": list(NOT_PROVEN), "checks": report["checks"],
                         "inputs": [forge_core.file_ref(Path(args.layout), final)],
                         "outputs": [forge_core.file_ref(stage / "layout-debug.png", stage)], "tool": dict(TOOL)}}
        forge_core.write_json(stage / "layout-report.json", record)
    summary.update(output_dir=str(final.resolve()), report=str((final / "layout-report.json").resolve()),
                   metadata=str((final / "layout-report.json").resolve()),
                   debug=str((final / "layout-debug.png").resolve()))
    return summary, 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--layout", type=Path, required=True, help="layout.v1 JSON (schema generate2dmap.layout.v1).")
    parser.add_argument("--output-dir", type=Path,
                        help="New folder for layout-report.json and layout-debug.png; must not exist. "
                             "Omit to print the summary only.")
    parser.add_argument("--strict-qc", action="store_true",
                        help="With --output-dir: publish nothing when a check fails.")
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary, code = run(args)
    if code:
        print("error: layout validation failed: " + "; ".join(summary["problems"][:3] or summary["failed"]),
              file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 (pass or warn), 1 (a failed check, its report published when --output-dir is given; or an error),
    2 (usage) (D26, D27)."""
    return forge_core.run_cli(_main, argv)


if __name__ == "__main__":
    raise SystemExit(main())
