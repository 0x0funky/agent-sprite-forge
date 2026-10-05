"""Map collision and navigation for every map tool: one blocking set, one rule book.

forge_nav is the canonical implementation of plan Appendix C as decided for Phase 3
(integration decisions D1-D7). It was extracted from B13's map_nav.py and map_bundle.py.
Canonical copy: shared/forge_nav.py; byte-identical copies ship in
skills/generate2dmap/scripts and skills/codeart2d/scripts (shared/VENDORED.json). It imports
only the standard library and numpy. merge_rects (D30) and reading a material-map image use
the sibling forge_core.py (API 1.1 or later), imported on first use, and through it Pillow.
There is no scipy path: map_nav never had one.

Consumers (D2, D4): map_nav.py (the CLI and its reports), the compose_layered_preview
actor-feet audit (--bundle), export_godot / export_ldtk / export_tiled (the blocking set),
layout_build (the reachability proof) and build_scene_preview. map-runtime.mjs mirrors the
rules below rule for rule, and layered-map-contract.md quotes them.

API
  read_blocking_set(path) / blocking_set_from_document(doc, base_dir) -> BlockingSet
      The D2 blocking set of one map bundle (v2, or v1 read the way map_bundle.py reads it).
      This reader only gathers what collision needs; map_bundle.py validate stays the gate.
  BlockingSet.model() -> CollisionModel; BlockingSet.solids lists every blocking shape.
  CollisionModel(width, height, radius, y_squash, regions, solids, material_codes, material_scale)
      .valid(xs, ys)  .blocked(xs, ys)  .area_ok(xs, ys)  .centre_ok(xs, ys)
      .segment_status(a, b, thin_gap=True) -> None or the reason   .segment_clear(a, b)
      .with_actor(radius, y_squash) -> the same blockers for another actor
  build_grid(model) -> NavGrid (nodes, open moves, thin gaps)
  grid_bfs(passable, starts, moves) / reachable_mask / moves_from_mask: the generic grid BFS
  navigate(model, starts) -> Navigation, with .point_target(p), .reach_target(p, reach) and
      .exit_target(trigger, activation, radius) answering as map_nav.py check does
  Building blocks: footprint_solid, object_solid, tile_solids, material_codes,
      collision_shapes, merge_rects, nav_cell, footprint_offsets, pnpoly, on_polygon_edge,
      Trigger, attach.

THE RULES
Arithmetic is IEEE-754 double. Every formula is evaluated in the order written (left to
right, with the grouping shown), so a port with the same operation order gets bit-identical
answers. The one exception is the sine and cosine of a rotation, which come from the
platform's libm (CPython calls the C runtime, V8 its own fdlibm port; they can differ by one
ulp, for example at sin(pi / 4)). Points exactly on a rotated edge can then fall on
different sides, so parity fixtures keep rotations at 0 or away from boundaries.

N1  Space. World pixels, x to the right, y down. W = world.width, H = world.height.
    rotate is in degrees, positive = clockwise on screen. theta = rotate * (pi / 180)
    (math.radians), c = cos(theta), s = sin(theta). A rotate of 0 (or none) means c = 1 and
    s = 0 exactly: no trigonometry is evaluated.

N2  Actor. r = collision.actorRadius, rx = r, ry = r * ySquash (ySquash defaults to 1.0;
    HD-2D plates use about 0.58). The 9 samples of a position P are P + o for these offsets,
    in this order: (0, 0), (rx, 0), (dx, dy), (0, ry), (-dx, dy), (-rx, 0), (-dx, -dy),
    (0, -ry), (dx, -dy), with dx = rx * SQRT1_2, dy = ry * SQRT1_2 and
    SQRT1_2 = 0.7071067811865476 (Math.SQRT1_2). A sample is (P.x + o.x, P.y + o.y).

N3  Walk area. Without walk regions it is the closed box 0 <= x <= W and 0 <= y <= H.
    With walk regions a point is in the walk area when some region's polygon contains it
    and none of that same region's holes contains it. Containment is the even-odd crossing
    test (pnpoly): start outside; for i = 0 .. n-1 take a = poly[i] and b = poly[i - 1]
    (i = 0 pairs with the last vertex); skip the edge when a.y == b.y; otherwise toggle when
    ((a.y > y) != (b.y > y)) and x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x. Left and
    top boundaries count as inside and right and bottom boundaries as outside, so regions
    sharing an edge leave no seam; for a hole the same rule means its left and top
    boundaries are not walkable and its right and bottom boundaries are. Membership is
    decided for each sample separately. Regions are never inflated or shrunk.

N4  The blocking set (D2) is identical for every consumer. It is the union of:
      a. collision.solids, in world px;
      b. collision.rects [x, y, w, h], each the closed rect solid (D3: exact blocking
         rectangles, never an approximation of something else);
      c. the footprints of objects whose solid is not false (N6);
      d. the per-tile collision of placed tiles (N7);
      e. the material-map pixels of a blocking class (N8).
    A shape without area blocks nothing and is dropped, wherever it comes from: a rect with
    w <= 0 or h <= 0, an ellipse with rx <= 0 or ry <= 0, a polygon whose shoelace sum
    sum(x[i] * y[i + 1]) - sum(y[i] * x[i + 1]) is exactly 0 (map_bundle.py refuses such a
    collision polygon outright). A point is blocked when it lies in any member. Walk regions
    (N3) bound the walk area; they are not blockers.

N5  Solids are closed sets (D1); a point on a solid's boundary is blocked.
      rect x, y, w, h:   x <= px <= x + w and y <= py <= y + h (x + w, y + h computed first)
      ellipse cx, cy, rx, ry, rotate:  ex = px - cx, ey = py - cy; u = ex, v = ey when the
         rotation is 0, else u = ex * c + ey * s and v = ey * c - ex * s; then
         nu = u / rx, nv = v / ry and the point is blocked when nu * nu + nv * nv <= 1
      polygon points:    the even-odd test of N3, OR the point lies on an edge: for an edge
         a -> b, (b.x - a.x) * (py - a.y) - (b.y - a.y) * (px - a.x) == 0 and
         min(a.x, b.x) <= px <= max(a.x, b.x) and min(a.y, b.y) <= py <= max(a.y, b.y).
         (Exact for axis-aligned edges and vertices; a point on a slanted edge counts when
         that product rounds to zero.)
    Solids are never inflated by the actor size: the samples of N2 add it, once.

N6  Object footprints (D2, D6, D7). An object's footprint and solid come from the object,
    else from its props-registry entry (bundle.props[prop]: an inline item, or the
    accepted prop-pack item named by pack + label, the entry's own fields overriding the
    pack item's). prop_packs, the art lookup of D6 step 3, do not supply footprints. When
    solid is given neither way, the object is solid exactly when its footprint shape is
    ellipse or rect. A solid object with an ellipse or rect footprint blocks this shape:
      k = 1 when footprint.basis is "world_px", else the instance scale (objects[].scale,
         default 1); basis "prop_px" (canonical) and its legacy alias "image_px" are
         measured in prop-image pixels and scaled once (D7); any other basis is an error;
      (ox, oy) = footprint.offset (default [0, 0]); rot = footprint.rotate (default 0);
      flip_x true mirrors the footprint around the anchor x (D6): ox = -ox and rot = -rot;
      cx = x + k * ox, cy = y + k * oy, w = k * width, h = k * depth;
      ellipse:  centre (cx, cy), rx = w / 2, ry = h / 2, rotate rot;
      rect, rot 0:  the rect (cx - w / 2, cy - h / 2, w, h);
      rect, rot != 0:  the polygon of the corners (u, v) = (-w / 2, -h / 2), (w / 2, -h / 2),
         (w / 2, h / 2), (-w / 2, h / 2), each at (cx + u * c - v * s, cy + u * s + v * c).
    Footprints are never inflated by the actor radius.

N7  Tile collision (D5: tileset_v1 tiles[].collision is authoritative). A tiles layer places
    tile index g at grid cell (row, col), whose top-left corner is (col * tw, row * th) for
    the tileset's tile size tw x th; -1 (or null) is an empty cell. A placed tile blocks
    with its collision shapes (tile pixels) moved by that corner: rect x, y, ellipse cx, cy
    and every polygon point shift by (col * tw, row * th). A tile without shapes whose
    properties.walkable is false blocks its whole cell, the rect (0, 0, tw, th). A rect
    whose x, y, w, h are whole numbers with w, h > 0 is clipped to the cell and painted into
    a per-layer pixel mask; the mask is merged into disjoint rects (merge_rects) that cover
    exactly the same closed set. If such a rect reaches outside its cell it is also kept
    whole, as its own solid. Every other shape is kept as its own solid for each placed tile.

N8  Material map. The image covers the world in squares of s = W / image width px; s must
    be a whole number >= 1 with s * image height == H. A point (x, y) reads the pixel
    (floor(x / s), floor(y / s)) when 0 <= floor(x / s) < image width and
    0 <= floor(y / s) < image height; outside the image there is no material. A pixel
    therefore covers [mx * s, (mx + 1) * s) x [my * s, (my + 1) * s): material squares are
    half-open (their right and bottom edges belong to the next pixel), unlike solids.
    Colour images (decoded to 8-bit straight RGBA): a pixel with alpha 0 has no material;
    otherwise its RGB must equal exactly one material's color. A P or L image whose
    materials all have an index matches pixel values to indices. A pixel that matches no
    material refuses the bundle. Classes: solid blocks (even with walkable: true); liquid
    and hazard block unless walkable is true; decor never blocks; one_way never blocks a
    point (N11).

N9  Validity. P is valid when each of its 9 samples (N2) is in the walk area (N3) and not
    blocked (N4).

N10 segmentClear(a, b). dx = b.x - a.x, dy = b.y - a.y, len = sqrt(dx * dx + dy * dy),
    n = max(1, ceil(len / (cell / 2))) with the cell of N12. The samples are
    x_k = a.x + dx * k / n and y_k = a.y + dy * k / n for k = 0 .. n (dx * k first, then
    / n, then + a.x; the last sample is therefore not always exactly b). The move is clear
    when every sample is valid (N9), the one_way rule holds (N11) and, unless the caller
    asks for the sampled rule alone, the thin-gap rule holds:
      Thin gaps: the actor's centre must stay in the walk area and off every blocker along
      the whole segment, so a wall or a gap thinner than the sample spacing is never
      jumped. The segment p + t * d (t in [0, 1]) is cut at every t in (0, 1) where it can
      cross a boundary: polygon edges of regions, holes and solids (proper crossings with
      0 <= u <= 1, and both end points of a collinear overlap), rect sides, both roots of
      each ellipse's quadratic, material pixel edges (x = m * s, y = m * s inside the image)
      and, without walk regions, the sides of the world box. The midpoint of every piece of
      positive length must be in the walk area and not blocked. A single touching point is
      not a failure. Cut positions only need to be accurate, not bit-identical: each piece's
      status is constant on its interior.

N11 one_way (D2: blocks from above only; a side-scroll class). A move with b.y > a.y
    (moving down) is blocked when, between consecutive samples k - 1 and k of N10, any of
    the 9 footprint samples goes from a pixel that is not one_way onto a one_way pixel;
    with the thin-gap rule the centre also may not enter one_way along a, the piece
    midpoints of N10 and b. Upward and sideways moves always pass (jump up through a
    platform, stand on it). Bundles carry no view mode, so the rule applies to every map:
    a top-down map should not use one_way.

N12 Grid. cell = max(1, floor(r / 2 + 0.5)) px (Math.round for r >= 0).
    cols = max(1, ceil(W / cell)), rows = max(1, ceil(H / cell)); node (col, row) sits at
    ((col + 0.5) * cell, (row + 0.5) * cell). A grid of more than 2^24 nodes is refused
    (split the map into chunks). A node is valid when its centre is valid (N9).

N13 Moves and BFS. 4-neighbour moves only. A move between two valid neighbours is open
    exactly when segmentClear (N10, with the thin-gap rule) holds between the two node
    centres; here n = 2, so the move's midpoint, (col + 1) * cell or (row + 1) * cell, is
    sampled too. Moves down (S) and up (N) differ only through one_way. Breadth-first
    search over open moves from the start nodes gives every node's distance in moves
    (-1 when unreachable). grid_bfs also serves passable grids that are not map grids.

N14 Reachability (what map_nav.py check proves).
      Join: a point joins the grid at every valid node within two cells of the cell that
        contains it ((floor(x / cell), floor(y / cell)) clamped to the grid) that it
        reaches by segmentClear; candidates are tried nearest first by squared distance,
        then by row and column. An invalid point joins nothing.
      Starts (spawns, arrival points): every joined node seeds the BFS. A start that is not
        valid, or that joins nothing, is an error.
      Best node: among reached candidates, the fewest moves, then the lowest row, then the
        lowest column.
      Point targets (anchor slots, approach points, interactions without reach): the actor
        must stand there: valid, and joined to a reached node.
      Reach targets (interactions with reach): a reached node centre within reach:
        dx * dx + dy * dy <= reach * reach.
      Exits: trigger distance is 0 inside the closed rect [x, x + w] x [y, y + h] or the
        closed circle, else rect: sqrt(ex * ex + ey * ey) with ex = max(x - px, 0, px - (x + w))
        and ey alike; circle: max(sqrt((px - cx) * (px - cx) + (py - cy) * (py - cy)) - r, 0).
        intent: a reached node at trigger distance <= radius. crossing: a reached node at
        distance 0; else, taking reached nodes within two cells in order of distance (ties
        in row-major order), the first whose straight move to the trigger's closest point is
        clear (segmentClear, and that point valid).

N15 Not modelled here (runtime duties): intent firing (normalised dot(intent,
    travelDirection) > 0.25), latch (default true) and requiresMovement (default true).
    Portal bookkeeping (arrivals outside triggers, reciprocal links) lives in map_nav.py.

Differences from B13's map_nav.py as built: polygon solids are closed (N5, per D1; map_nav
used the even-odd test alone, so a point on a polygon solid's right or bottom edge was
free); footprint basis (N6, D7) and flip_x (N6, D6); and shapes without area block nothing
wherever they come from (N4; map_nav dropped them only from collision.solids and
collision.rects, so a zero-width footprint or tile rect blocked the line it spans).
Everything else is map_nav's behaviour; tests/test_forge_nav.py proves it against map_nav
loaded by path.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)  # the sibling forge_core.py (shared/ or the skill's scripts/)

FORGE_NAV_API_VERSION = "1"
FREE, BLOCK, ONE_WAY = 0, 1, 2
MOVE_E, MOVE_S, MOVE_W, MOVE_N = 1, 2, 4, 8
MAX_GRID_NODES = 1 << 24  # a 4096 x 4096 node grid; larger maps should be split into chunks
SQRT1_2 = 0.7071067811865476  # Math.SQRT1_2, the exact double JavaScript uses
FOOTPRINT_BASES = ("prop_px", "world_px", "image_px")  # D7: image_px is the legacy alias of prop_px
MATERIAL_CLASSES = ("solid", "one_way", "liquid", "hazard", "decor")
BUNDLE_SCHEMAS = ("generate2dmap.map_bundle.v1", "generate2dmap.map_bundle.v2")
_PAD = 1e-7  # bounding boxes are widened by this much for culling only; never for decisions
_REL_PATH = re.compile(r"^(?!/)(?![A-Za-z][A-Za-z0-9+.-]*:)[^\\]+$")  # common.schema.json relPath
_REASON_STAND = "the actor cannot stand here (footprint blocked)"

__all__ = [
    "FORGE_NAV_API_VERSION", "FREE", "BLOCK", "ONE_WAY", "MOVE_E", "MOVE_S", "MOVE_W", "MOVE_N", "MAX_GRID_NODES",
    "SQRT1_2", "FOOTPRINT_BASES", "MATERIAL_CLASSES", "BUNDLE_SCHEMAS", "NavError",
    "footprint_offsets", "pnpoly", "on_polygon_edge", "nav_cell", "round_half_up", "merge_rects",
    "CollisionModel", "NavGrid", "build_grid", "moves_from_mask", "grid_bfs", "reachable_mask",
    "Trigger", "Reach", "Navigation", "attach", "navigate",
    "footprint_solid", "object_solid", "TileLayer", "tile_solids", "material_codes", "collision_shapes",
    "BlockingSet", "read_json", "read_blocking_set", "blocking_set_from_document", "upgrade_v1_footprints",
]


class NavError(ValueError):
    """Map data that collision cannot use (unreadable, malformed or out of contract)."""


# --------------------------------------------------------------------------- numbers and grids

def round_half_up(value: float) -> int:
    """floor(value + 0.5): the forge rounding rule (never banker's rounding)."""
    return math.floor(value + 0.5)


def nav_cell(actor_radius: float) -> int:
    """Navigation grid cell (N12): max(1, round(r / 2)) with half-up rounding."""
    return max(1, round_half_up(actor_radius / 2))


def merge_rects(mask: Any) -> list[tuple[int, int, int, int]]:
    """Cover a boolean grid with disjoint axis-aligned rectangles (x, y, w, h) in cells.

    This is forge_core.merge_rects (D30), the one cover every map tool shares. Greedy:
    rows are scanned top to bottom; each maximal run of uncovered True cells in a row
    becomes a rectangle that grows downward while the whole run stays True and uncovered.
    The rectangles are disjoint and their union is exactly the True cells. A mask that is
    not 2-D raises ValueError.
    """
    return _forge_core("merge_rects", "merge_rects").merge_rects(mask)


def _forge_core(purpose: str, needs: str) -> Any:
    """The sibling forge_core.py (shared/ or the skill's scripts/), imported on first use."""
    try:
        import forge_core  # the sibling copy (shared/ or the skill's scripts/)
    except ImportError as error:
        raise NavError(f"{purpose} needs forge_core.py beside forge_nav.py ({error})") from None
    if not callable(getattr(forge_core, needs, None)):
        raise NavError(f"{purpose} needs forge_core.{needs}, which the forge_core.py beside forge_nav.py "
                       "lacks (a stale copy: run tools/vendor_sync.py --write)")
    return forge_core


# --------------------------------------------------------------------------- primitive geometry

def footprint_offsets(radius: float, y_squash: float = 1.0) -> np.ndarray:
    """The 9 sample offsets of N2, (9, 2): the centre, then 0, 45, ..., 315 degrees on the
    ellipse rx = r, ry = r * ySquash (y down, so 45 degrees is down-right)."""
    rx = float(radius)
    ry = rx * float(y_squash)
    dx, dy = rx * SQRT1_2, ry * SQRT1_2
    return np.array([[0.0, 0.0], [rx, 0.0], [dx, dy], [0.0, ry], [-dx, dy],
                     [-rx, 0.0], [-dx, -dy], [0.0, -ry], [dx, -dy]], np.float64)


def pnpoly(xs: Any, ys: Any, polygon: Any) -> np.ndarray:
    """Even-odd point-in-polygon (W. R. Franklin's crossing test, N3), broadcasting xs against ys.

    For each edge a = polygon[i], b = polygon[i - 1] a point crosses when
    (a.y > y) != (b.y > y) and x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x,
    evaluated in exactly this order so map-runtime.mjs gets bit-identical answers.
    """
    xs = np.asarray(xs, np.float64)
    ys = np.asarray(ys, np.float64)
    inside = np.zeros(np.broadcast_shapes(xs.shape, ys.shape), bool)
    for i in range(len(polygon)):
        ax, ay = float(polygon[i][0]), float(polygon[i][1])
        bx, by = float(polygon[i - 1][0]), float(polygon[i - 1][1])
        if ay == by:  # horizontal edges never satisfy the crossing condition
            continue
        crossing = (ay > ys) != (by > ys)
        if not crossing.any():
            continue
        inside ^= crossing & (xs < (bx - ax) * (ys - ay) / (by - ay) + ax)
    return inside


def on_polygon_edge(xs: Any, ys: Any, polygon: Any) -> np.ndarray:
    """True where a point lies on an edge of the polygon (N5): for the edge a = polygon[i],
    b = polygon[i - 1], (b.x - a.x) * (y - a.y) - (b.y - a.y) * (x - a.x) == 0 with the point
    inside the edge's bounding box. Exact for axis-aligned edges and for vertices."""
    xs = np.asarray(xs, np.float64)
    ys = np.asarray(ys, np.float64)
    hit = np.zeros(np.broadcast_shapes(xs.shape, ys.shape), bool)
    for i in range(len(polygon)):
        ax, ay = float(polygon[i][0]), float(polygon[i][1])
        bx, by = float(polygon[i - 1][0]), float(polygon[i - 1][1])
        within = (xs >= min(ax, bx)) & (xs <= max(ax, bx)) & (ys >= min(ay, by)) & (ys <= max(ay, by))
        if not within.any():
            continue
        hit |= within & ((bx - ax) * (ys - ay) - (by - ay) * (xs - ax) == 0)
    return hit


def _segment_edge_params(px: float, py: float, dx: float, dy: float, edges: np.ndarray) -> np.ndarray:
    """Parameters t in (0, 1) where the segment p + t * d meets the edges (E, 4) [ax, ay, bx, by]."""
    ax, ay, bx, by = edges.T
    ex, ey = bx - ax, by - ay
    wx, wy = ax - px, ay - py
    denom = dx * ey - dy * ex
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (wx * ey - wy * ex) / denom
        u = (wx * dy - wy * dx) / denom
        hits = t[(denom != 0) & (u >= 0) & (u <= 1)]
        length = dx * dx + dy * dy
        collinear = (denom == 0) & (wx * dy - wy * dx == 0)
        if collinear.any() and length > 0:  # overlapping edges: their end points split the segment
            ends = np.concatenate([(wx * dx + wy * dy)[collinear], ((bx - px) * dx + (by - py) * dy)[collinear]])
            hits = np.concatenate([hits, ends / length])
    return hits[(hits > 0) & (hits < 1)]


def _polygon_edges(polygon: np.ndarray) -> np.ndarray:
    return np.concatenate([polygon, np.roll(polygon, 1, axis=0)], axis=1)  # [ax, ay, bx, by] for a=i, b=i-1


def _joined(lines: Iterable[np.ndarray], values: Iterable[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    lines, values = list(lines), list(values)
    if not lines:
        return np.empty(0, np.int64), np.empty(0, np.float64)
    return np.concatenate(lines).astype(np.int64), np.concatenate(values).astype(np.float64)


class _Polygon:
    """A polygon: a closed solid (N5: interior or edge) or, with closed=False, a walk-region
    outline or hole (N3: the even-odd test alone)."""

    def __init__(self, points: Any, closed: bool = False) -> None:
        self.points = np.asarray(points, np.float64)
        self.closed = bool(closed)
        self.edges = _polygon_edges(self.points)
        lo, hi = self.points.min(axis=0), self.points.max(axis=0)
        self.bounds = (lo[0] - _PAD, lo[1] - _PAD, hi[0] + _PAD, hi[1] + _PAD)

    def contains(self, xs: Any, ys: Any) -> np.ndarray:
        inside = pnpoly(xs, ys, self.points)
        if self.closed:
            inside |= on_polygon_edge(xs, ys, self.points)
        return inside

    def h_breaks(self, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(line index, x) where the containment can change along the horizontal lines y = ys."""
        lines, values = [], []
        for ax, ay, bx, by in self.edges:
            if ay == by:
                if self.closed:  # a closed solid's horizontal edge lying on the line blocks between its ends
                    on = np.flatnonzero(ys == ay)
                    if on.size:
                        lines += [on, on]
                        values += [np.full(on.size, ax), np.full(on.size, bx)]
                continue
            crossing = np.flatnonzero((ay > ys) != (by > ys))
            if crossing.size:
                lines.append(crossing)
                values.append((bx - ax) * (ys[crossing] - ay) / (by - ay) + ax)
        return _joined(lines, values)

    def v_breaks(self, xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lines, values = [], []
        for ax, ay, bx, by in self.edges:
            if ax == bx:
                if self.closed:  # a closed solid's vertical edge lying on the line
                    on = np.flatnonzero(xs == ax)
                    if on.size:
                        lines += [on, on]
                        values += [np.full(on.size, ay), np.full(on.size, by)]
                continue
            crossing = np.flatnonzero((ax > xs) != (bx > xs))
            if crossing.size:
                lines.append(crossing)
                values.append((by - ay) * (xs[crossing] - ax) / (bx - ax) + ay)
        return _joined(lines, values)

    def seg_breaks(self, px: float, py: float, dx: float, dy: float) -> np.ndarray:
        return _segment_edge_params(px, py, dx, dy, self.edges)


class _Region:
    """A walk region: inside its polygon and outside all of its holes (N3)."""

    def __init__(self, polygon: np.ndarray, holes: Sequence[np.ndarray]) -> None:
        self.outline = _Polygon(polygon)
        self.holes = [_Polygon(hole) for hole in holes]
        self.bounds = self.outline.bounds

    def contains(self, xs: Any, ys: Any) -> np.ndarray:
        inside = self.outline.contains(xs, ys)
        for hole in self.holes:
            inside &= ~hole.contains(xs, ys)
        return inside

    def _parts(self) -> list[_Polygon]:
        return [self.outline, *self.holes]

    def h_breaks(self, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return _joined(*zip(*(part.h_breaks(ys) for part in self._parts())))

    def v_breaks(self, xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return _joined(*zip(*(part.v_breaks(xs) for part in self._parts())))

    def seg_breaks(self, px: float, py: float, dx: float, dy: float) -> np.ndarray:
        return np.concatenate([part.seg_breaks(px, py, dx, dy) for part in self._parts()])


class _Rect:
    """A closed axis-aligned rectangle [x, x + w] x [y, y + h] (N5)."""

    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self.x0, self.y0 = float(x), float(y)
        self.x1, self.y1 = self.x0 + float(w), self.y0 + float(h)
        self.bounds = (self.x0 - _PAD, self.y0 - _PAD, self.x1 + _PAD, self.y1 + _PAD)

    def contains(self, xs: Any, ys: Any) -> np.ndarray:
        return (xs >= self.x0) & (xs <= self.x1) & (ys >= self.y0) & (ys <= self.y1)

    def h_breaks(self, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lines = np.flatnonzero((ys >= self.y0) & (ys <= self.y1))
        return np.concatenate([lines, lines]), np.repeat([self.x0, self.x1], lines.size)

    def v_breaks(self, xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lines = np.flatnonzero((xs >= self.x0) & (xs <= self.x1))
        return np.concatenate([lines, lines]), np.repeat([self.y0, self.y1], lines.size)

    def seg_breaks(self, px: float, py: float, dx: float, dy: float) -> np.ndarray:
        values = []
        if dx:
            values += [(self.x0 - px) / dx, (self.x1 - px) / dx]
        if dy:
            values += [(self.y0 - py) / dy, (self.y1 - py) / dy]
        values = np.asarray(values, np.float64)
        return values[(values > 0) & (values < 1)]


class _Ellipse:
    """A closed ellipse (u / rx)^2 + (v / ry)^2 <= 1 in its own frame, rotated clockwise (y down)."""

    def __init__(self, cx: float, cy: float, rx: float, ry: float, rotate: float = 0.0) -> None:
        self.cx, self.cy, self.rx, self.ry = float(cx), float(cy), float(rx), float(ry)
        theta = math.radians(float(rotate or 0.0))
        self.cos, self.sin = (1.0, 0.0) if not rotate else (math.cos(theta), math.sin(theta))
        half_w = math.hypot(self.rx * self.cos, self.ry * self.sin)
        half_h = math.hypot(self.rx * self.sin, self.ry * self.cos)
        pad = _PAD + 1e-9 * (half_w + half_h)
        self.bounds = (self.cx - half_w - pad, self.cy - half_h - pad, self.cx + half_w + pad, self.cy + half_h + pad)

    def _local(self, xs: Any, ys: Any) -> tuple[Any, Any]:
        dx, dy = xs - self.cx, ys - self.cy
        if self.sin == 0.0 and self.cos == 1.0:
            return dx, dy
        return dx * self.cos + dy * self.sin, dy * self.cos - dx * self.sin

    def contains(self, xs: Any, ys: Any) -> np.ndarray:
        u, v = self._local(xs, ys)
        ur, vr = u / self.rx, v / self.ry
        return ur * ur + vr * vr <= 1.0

    def _roots(self, px: Any, py: Any, dx: float, dy: float) -> np.ndarray:
        """Both roots t of |p + t d| on the ellipse boundary (NaN where the line misses), shape (2, ...)."""
        u0, v0 = self._local(np.asarray(px, np.float64), np.asarray(py, np.float64))
        du, dv = dx * self.cos + dy * self.sin, dy * self.cos - dx * self.sin
        a = (du / self.rx) ** 2 + (dv / self.ry) ** 2
        b = 2 * (u0 * du / self.rx ** 2 + v0 * dv / self.ry ** 2)
        c = (u0 / self.rx) ** 2 + (v0 / self.ry) ** 2 - 1
        disc = b * b - 4 * a * c
        with np.errstate(invalid="ignore"):
            root = np.sqrt(np.where(disc >= 0, disc, np.nan))
        return np.stack([(-b - root) / (2 * a), (-b + root) / (2 * a)])

    def h_breaks(self, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        roots = self._roots(0.0, ys, 1.0, 0.0)
        lines = np.flatnonzero(np.isfinite(roots[0]))
        return np.concatenate([lines, lines]), np.concatenate([roots[0][lines], roots[1][lines]])

    def v_breaks(self, xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        roots = self._roots(xs, 0.0, 0.0, 1.0)
        lines = np.flatnonzero(np.isfinite(roots[0]))
        return np.concatenate([lines, lines]), np.concatenate([roots[0][lines], roots[1][lines]])

    def seg_breaks(self, px: float, py: float, dx: float, dy: float) -> np.ndarray:
        if dx == 0 and dy == 0:
            return np.empty(0)
        roots = self._roots(px, py, dx, dy).ravel()
        return roots[np.isfinite(roots) & (roots > 0) & (roots < 1)]


def _shape(solid: Mapping[str, Any]) -> _Rect | _Ellipse | _Polygon:
    if solid["shape"] == "rect":
        return _Rect(solid["x"], solid["y"], solid["w"], solid["h"])
    if solid["shape"] == "ellipse":
        return _Ellipse(solid["cx"], solid["cy"], solid["rx"], solid["ry"], solid.get("rotate", 0))
    return _Polygon(solid["points"], closed=True)


def _polygon_area2(points: Any) -> float:
    """Twice the signed shoelace area of a polygon."""
    array = np.asarray(points, np.float64)
    x, y = array[:, 0], array[:, 1]
    return float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _has_area(solid: Mapping[str, Any]) -> bool:
    """N4: a rect with w or h <= 0, an ellipse with rx or ry <= 0 and a polygon with zero
    shoelace area block nothing, wherever they come from."""
    if solid["shape"] == "rect":
        return solid["w"] > 0 and solid["h"] > 0
    if solid["shape"] == "ellipse":
        return solid["rx"] > 0 and solid["ry"] > 0
    return _polygon_area2(solid["points"]) != 0


class _Materials:
    """Material-map classes (FREE, BLOCK, ONE_WAY) on squares of ``scale`` world pixels; no
    material (FREE) outside the image. Pixel (mx, my) covers [mx * s, (mx + 1) * s) x ... (N8)."""

    def __init__(self, codes: np.ndarray, scale: int) -> None:
        self.codes = np.asarray(codes, np.uint8)
        self.scale = int(scale)
        self.height, self.width = self.codes.shape

    def lookup(self, xs: Any, ys: Any) -> np.ndarray:
        xs, ys = np.broadcast_arrays(np.asarray(xs, np.float64), np.asarray(ys, np.float64))
        mx, my = np.floor(xs / self.scale), np.floor(ys / self.scale)
        inside = (mx >= 0) & (mx < self.width) & (my >= 0) & (my < self.height)
        out = np.zeros(xs.shape, np.uint8)
        out[inside] = self.codes[my[inside].astype(np.int64), mx[inside].astype(np.int64)]
        return out

    def _breaks(self, line_index: np.ndarray, rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Breakpoints along the lines ``line_index`` whose material rows (or columns) are ``rows``
        (L, n): every pixel boundary where the class changes, FREE assumed beyond the image."""
        padded = np.concatenate([np.zeros((rows.shape[0], 1), np.uint8), rows,
                                 np.zeros((rows.shape[0], 1), np.uint8)], axis=1)
        line, column = np.nonzero(padded[:, 1:] != padded[:, :-1])
        return line_index[line], column.astype(np.float64) * self.scale

    def h_breaks(self, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        my = np.floor(ys / self.scale)
        lines = np.flatnonzero((my >= 0) & (my < self.height))
        return self._breaks(lines, self.codes[my[lines].astype(np.int64)])

    def v_breaks(self, xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mx = np.floor(xs / self.scale)
        lines = np.flatnonzero((mx >= 0) & (mx < self.width))
        return self._breaks(lines, self.codes[:, mx[lines].astype(np.int64)].T)

    def seg_breaks(self, px: float, py: float, dx: float, dy: float) -> np.ndarray:
        values = []
        for start, delta, limit in ((px, dx, self.width), (py, dy, self.height)):
            if not delta:
                continue
            lo, hi = sorted((start, start + delta))
            grid = np.arange(max(0, math.ceil(lo / self.scale)), min(limit, math.floor(hi / self.scale)) + 1)
            values.append((grid * self.scale - start) / delta)
        values = np.concatenate(values) if values else np.empty(0)
        return values[(values > 0) & (values < 1)]


# --------------------------------------------------------------------------- the collision model

class CollisionModel:
    """Validity (N9), segmentClear with the one_way and thin-gap rules (N10, N11) for one map.

    regions: (polygon, [holes]) pairs (N3); solids: shape dicts {"shape": "rect", x, y, w, h},
    {"shape": "ellipse", cx, cy, rx, ry, rotate?} or {"shape": "polygon", points}, each with
    an optional "source" label (N4, N5); material_codes: (h, w) FREE / BLOCK / ONE_WAY per
    material pixel, on squares of material_scale world px (N8).
    """

    def __init__(self, width: float, height: float, radius: float, y_squash: float = 1.0,
                 regions: Sequence[tuple[Any, Sequence[Any]]] = (),
                 solids: Sequence[Mapping[str, Any]] = (), material_codes: np.ndarray | None = None,
                 material_scale: int = 1) -> None:
        self.width, self.height = float(width), float(height)
        self.radius, self.y_squash = float(radius), float(y_squash)
        self.cell = nav_cell(self.radius)
        self.offsets = footprint_offsets(self.radius, self.y_squash)
        self.regions = [_Region(np.asarray(polygon, np.float64), [np.asarray(h, np.float64) for h in holes])
                        for polygon, holes in regions]
        kept = [solid for solid in solids if _has_area(solid)]  # shapes without area block nothing (N4)
        self.solids = [_shape(solid) for solid in kept]
        self.solid_sources = [str(solid.get("source", "")) for solid in kept]
        self.materials = None if material_codes is None else _Materials(material_codes, material_scale)
        self.has_one_way = self.materials is not None and bool((self.materials.codes == ONE_WAY).any())

    @classmethod
    def from_blocking_set(cls, blocking: "BlockingSet") -> "CollisionModel":
        return blocking.model()

    def with_actor(self, radius: float, y_squash: float | None = None) -> "CollisionModel":
        """The same blockers and walk area for another actor (radius, ySquash)."""
        other = object.__new__(CollisionModel)
        other.__dict__.update(self.__dict__)
        other.radius = float(radius)
        other.y_squash = self.y_squash if y_squash is None else float(y_squash)
        other.cell = nav_cell(other.radius)
        other.offsets = footprint_offsets(other.radius, other.y_squash)
        return other

    # -- point predicates

    def area_ok(self, xs: Any, ys: Any) -> np.ndarray:
        """In the walk area (N3)."""
        xs, ys = np.broadcast_arrays(np.asarray(xs, np.float64), np.asarray(ys, np.float64))
        if not self.regions:
            return (xs >= 0) & (xs <= self.width) & (ys >= 0) & (ys <= self.height)
        return self._any(self.regions, xs, ys)

    def blocked(self, xs: Any, ys: Any) -> np.ndarray:
        """True where a point lies on a blocker (a solid or a blocking material, N4)."""
        xs, ys = np.broadcast_arrays(np.asarray(xs, np.float64), np.asarray(ys, np.float64))
        hit = self._any(self.solids, xs, ys)
        if self.materials is not None:
            hit |= self.materials.lookup(xs, ys) == BLOCK
        return hit

    def centre_ok(self, xs: Any, ys: Any) -> np.ndarray:
        """One sample: in the walk area and on no blocker."""
        return self.area_ok(xs, ys) & ~self.blocked(xs, ys)

    def _samples(self, xs: Any, ys: Any) -> tuple[np.ndarray, np.ndarray]:
        """The 9 footprint samples of every point, shape (..., 9): x + offset_x, y + offset_y."""
        xs, ys = np.broadcast_arrays(np.asarray(xs, np.float64), np.asarray(ys, np.float64))
        return xs[..., None] + self.offsets[:, 0], ys[..., None] + self.offsets[:, 1]

    def valid(self, xs: Any, ys: Any) -> np.ndarray:
        """Validity of actor positions (N9: all 9 footprint samples)."""
        return self.centre_ok(*self._samples(xs, ys)).all(axis=-1)

    def one_way_bits(self, xs: Any, ys: Any) -> np.ndarray:
        """Bit k set where footprint sample k lies on a one_way pixel."""
        sx, sy = self._samples(xs, ys)
        if not self.has_one_way:
            return np.zeros(sx.shape[:-1], np.uint16)
        weights = (1 << np.arange(len(self.offsets))).astype(np.uint16)
        return ((self.materials.lookup(sx, sy) == ONE_WAY) * weights).sum(axis=-1).astype(np.uint16)

    @staticmethod
    def _any(shapes: Sequence[Any], xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """OR of shape.contains, evaluated only inside each shape's bounding box."""
        hit = np.zeros(xs.shape, bool)
        for shape in shapes:
            x0, y0, x1, y1 = shape.bounds
            near = (xs >= x0) & (xs <= x1) & (ys >= y0) & (ys <= y1) & ~hit
            if near.any():
                hit[near] = shape.contains(xs[near], ys[near])
        return hit

    # -- lattices (the grid fast path; identical answers to the point predicates)

    def _lattice_any(self, shapes: Sequence[Any], sx: np.ndarray, sy: np.ndarray) -> np.ndarray:
        hit = np.zeros((sy.size, sx.size), bool)
        for shape in shapes:
            x0, y0, x1, y1 = shape.bounds
            i0, i1 = np.searchsorted(sx, x0, "left"), np.searchsorted(sx, x1, "right")
            j0, j1 = np.searchsorted(sy, y0, "left"), np.searchsorted(sy, y1, "right")
            if i0 < i1 and j0 < j1:
                hit[j0:j1, i0:i1] |= shape.contains(sx[None, i0:i1], sy[j0:j1, None])
        return hit

    def _lattice_centre_ok(self, sx: np.ndarray, sy: np.ndarray) -> np.ndarray:
        if self.regions:
            ok = self._lattice_any(self.regions, sx, sy)
        else:
            ok = ((sx >= 0) & (sx <= self.width))[None, :] & ((sy >= 0) & (sy <= self.height))[:, None]
        ok &= ~self._lattice_any(self.solids, sx, sy)
        if self.materials is not None:
            ok &= self.materials.lookup(sx[None, :], sy[:, None]) != BLOCK
        return ok

    def valid_lattice(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """valid() on every (x, y) of ascending xs and ys, shape (len(ys), len(xs))."""
        xs, ys = np.asarray(xs, np.float64), np.asarray(ys, np.float64)
        ok = np.ones((ys.size, xs.size), bool)
        for ox, oy in self.offsets:
            ok &= self._lattice_centre_ok(xs + ox, ys + oy)
        return ok

    def one_way_lattice(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        return self.one_way_bits(np.asarray(xs, np.float64)[None, :], np.asarray(ys, np.float64)[:, None])

    # -- segments

    def _boundary_parts(self) -> list[Any]:
        parts: list[Any] = list(self.regions) + list(self.solids)
        if self.materials is not None:
            parts.append(self.materials)
        return parts

    def segment_breaks(self, a: Sequence[float], b: Sequence[float]) -> np.ndarray:
        """Sorted parameters in [0, 1] where any boundary may cross the segment a -> b (0 and 1 included)."""
        px, py = float(a[0]), float(a[1])
        dx, dy = float(b[0]) - px, float(b[1]) - py
        values = [np.array([0.0, 1.0])]
        if dx or dy:
            values += [part.seg_breaks(px, py, dx, dy) for part in self._boundary_parts()]
            if not self.regions:
                values.append(_Rect(0, 0, self.width, self.height).seg_breaks(px, py, dx, dy))
        return np.unique(np.concatenate(values))

    def segment_status(self, a: Sequence[float], b: Sequence[float], *, thin_gap: bool = True) -> str | None:
        """None when the actor can move straight from a to b (segmentClear, N10); else the reason.

        thin_gap=False tests only the sampled rule (footprint samples every cell / 2 and the
        one_way direction of those samples), for parity checks with a runtime that does not
        implement the thin-gap rule; map_nav itself always applies it.
        """
        ax, ay, bx, by = float(a[0]), float(a[1]), float(b[0]), float(b[1])
        dx, dy = bx - ax, by - ay
        length = math.sqrt(dx * dx + dy * dy)
        n = max(1, math.ceil(length / (self.cell / 2)))
        ks = np.arange(n + 1, dtype=np.float64)
        px, py = ax + dx * ks / n, ay + dy * ks / n
        ok = self.valid(px, py)
        if not ok.all():
            k = int(np.argmin(ok))
            return f"actor footprint blocked at ({px[k]:g}, {py[k]:g})"
        downward = by > ay and self.has_one_way
        if downward:
            bits = self.one_way_bits(px, py)
            if ((bits[1:] & ~bits[:-1]) != 0).any():
                return "one_way material blocks moving down onto it"
        if not thin_gap:
            return None
        ts = self.segment_breaks((ax, ay), (bx, by))
        pieces = ts[1:] > ts[:-1]
        mid = (ts[:-1][pieces] + ts[1:][pieces]) / 2
        mx, my = ax + dx * mid, ay + dy * mid
        centre = self.centre_ok(mx, my)
        if not centre.all():
            k = int(np.argmin(centre))
            return f"centre path leaves the walk area or crosses a blocker near ({mx[k]:g}, {my[k]:g}) (thin-gap rule)"
        if downward:
            codes = np.concatenate([self.materials.lookup(ax, ay).ravel(), self.materials.lookup(mx, my),
                                    self.materials.lookup(bx, by).ravel()])
            if ((codes[1:] == ONE_WAY) & (codes[:-1] != ONE_WAY)).any():
                return "centre path enters one_way material from above"
        return None

    def segment_clear(self, a: Sequence[float], b: Sequence[float], *, thin_gap: bool = True) -> bool:
        return self.segment_status(a, b, thin_gap=thin_gap) is None

    # -- grid edges: the exact centre path of many axis-aligned unit moves at once

    def centre_path_failures(self, axis: str, xs: np.ndarray, ys: np.ndarray,
                             candidates: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """For the moves between neighbouring nodes along rows (axis "h") or columns ("v"),
        return (blocked, one_way_entry): the thin-gap rule fails, or (columns only) the centre
        path enters one_way material while moving down. Only ``candidates`` are tested."""
        lines, positions = (ys, xs) if axis == "h" else (xs, ys)
        blocked = np.zeros(candidates.shape, bool)
        entry = np.zeros(candidates.shape, bool)
        parts = self._boundary_parts()
        if not parts or not candidates.any():
            return blocked, entry
        found = [part.h_breaks(lines) if axis == "h" else part.v_breaks(lines) for part in parts]
        line_of, value = _joined([f[0] for f in found], [f[1] for f in found])
        if line_of.size == 0:
            return blocked, entry
        order = np.lexsort((value, line_of))
        line_of, value = line_of[order], value[order]
        bounds = np.searchsorted(line_of, np.arange(lines.size + 1))
        starts, ends = positions[:-1], positions[1:]
        piece_line, piece_edge, piece_lo, piece_hi = [], [], [], []
        for line in np.unique(line_of):
            breaks = np.unique(value[bounds[line]:bounds[line + 1]])
            lo = np.searchsorted(breaks, starts, "left")
            hi = np.searchsorted(breaks, ends, "right")
            edges = np.flatnonzero(_edge_row(candidates, axis, line) & (hi > lo))
            if edges.size == 0:
                continue
            edge_of, seg_lo, seg_hi = _split(breaks, starts[edges], ends[edges], lo[edges], hi[edges])
            piece_line.append(np.full(edge_of.size, line))
            piece_edge.append(edges[edge_of])
            piece_lo.append(seg_lo)
            piece_hi.append(seg_hi)
        if not piece_line:
            return blocked, entry
        line_idx = np.concatenate(piece_line)
        edge_idx = np.concatenate(piece_edge)
        mid = (np.concatenate(piece_lo) + np.concatenate(piece_hi)) / 2
        px, py = (mid, lines[line_idx]) if axis == "h" else (lines[line_idx], mid)
        failed = ~self.centre_ok(px, py)
        if axis == "h":
            blocked[line_idx[failed], edge_idx[failed]] = True
        else:
            blocked[edge_idx[failed], line_idx[failed]] = True
            if self.has_one_way:
                codes = self.materials.lookup(px, py)
                first = np.r_[True, (line_idx[1:] != line_idx[:-1]) | (edge_idx[1:] != edge_idx[:-1])]
                last = np.r_[first[1:], True]
                before = np.where(first, self.materials.lookup(lines[line_idx], positions[edge_idx]), np.roll(codes, 1))
                after_end = self.materials.lookup(lines[line_idx], positions[edge_idx + 1])
                enters = (codes == ONE_WAY) & (before != ONE_WAY)
                enters |= last & (after_end == ONE_WAY) & (codes != ONE_WAY)
                entry[edge_idx[enters], line_idx[enters]] = True
        return blocked, entry


def _edge_row(candidates: np.ndarray, axis: str, line: int) -> np.ndarray:
    return candidates[line] if axis == "h" else candidates[:, line]


def _split(breaks: np.ndarray, starts: np.ndarray, ends: np.ndarray, lo: np.ndarray,
           hi: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split each [starts[e], ends[e]] at breaks[lo[e]:hi[e]] (sorted, inside the closed range).

    Returns (edge index, piece start, piece end) for every piece of positive length, pieces
    of one edge in ascending order.
    """
    counts = hi - lo
    total = counts + 2
    edge_of = np.repeat(np.arange(starts.size), total)
    offsets = np.cumsum(total) - total
    position = np.arange(int(total.sum())) - np.repeat(offsets, total)
    values = np.empty(position.size)
    first = position == 0
    last = position == np.repeat(total - 1, total)
    middle = ~first & ~last
    values[first] = starts
    values[last] = ends
    values[middle] = breaks[np.repeat(lo, counts) + position[middle] - 1]
    keep = (edge_of[:-1] == edge_of[1:]) & (values[1:] > values[:-1])
    return edge_of[:-1][keep], values[:-1][keep], values[1:][keep]


# --------------------------------------------------------------------------- the grid and BFS

@dataclass
class NavGrid:
    """Nodes at cell centres (N12); moves holds the open 4-neighbour moves of every node (N13)."""
    cell: int
    xs: np.ndarray  # node centre x per column
    ys: np.ndarray  # node centre y per row
    valid: np.ndarray  # (rows, cols) bool
    moves: np.ndarray  # (rows, cols) uint8: MOVE_E | MOVE_S | MOVE_W | MOVE_N
    thin_gaps: list[tuple[float, float, float, float]] = field(default_factory=list)
    one_way_blocked: int = 0

    @property
    def rows(self) -> int:
        return self.valid.shape[0]

    @property
    def cols(self) -> int:
        return self.valid.shape[1]

    def node_of(self, x: float, y: float) -> tuple[int, int]:
        """(row, col) of the cell containing (x, y), clamped to the grid."""
        col = min(max(math.floor(x / self.cell), 0), self.cols - 1)
        row = min(max(math.floor(y / self.cell), 0), self.rows - 1)
        return row, col


def build_grid(model: CollisionModel) -> NavGrid:
    """Rasterise a collision model onto its navigation grid (N12, N13)."""
    cell = model.cell
    cols = max(1, math.ceil(model.width / cell))
    rows = max(1, math.ceil(model.height / cell))
    if cols * rows > MAX_GRID_NODES:
        raise NavError(f"the navigation grid would have {cols * rows} nodes (cell {cell} px for actorRadius "
                       f"{model.radius:g} on a {model.width:g}x{model.height:g} world; the limit is "
                       f"{MAX_GRID_NODES}): split the map into chunks or check the actor radius")
    xs = (np.arange(cols) + 0.5) * cell
    ys = (np.arange(rows) + 0.5) * cell
    valid = model.valid_lattice(xs, ys)
    h_mid = (np.arange(cols - 1) + 1.0) * cell  # == xs[:-1] + cell / 2 exactly
    v_mid = (np.arange(rows - 1) + 1.0) * cell
    h_ok = valid[:, :-1] & valid[:, 1:] & model.valid_lattice(h_mid, ys)
    v_ok = valid[:-1, :] & valid[1:, :] & model.valid_lattice(xs, v_mid)
    h_gap, _ = model.centre_path_failures("h", xs, ys, h_ok)
    v_gap, v_entry = model.centre_path_failures("v", xs, ys, v_ok)
    thin = [(xs[c], ys[r], xs[c + 1], ys[r]) for r, c in zip(*np.nonzero(h_ok & h_gap))]
    thin += [(xs[c], ys[r], xs[c], ys[r + 1]) for r, c in zip(*np.nonzero(v_ok & v_gap))]
    h_ok &= ~h_gap
    v_ok &= ~v_gap
    down_ok = v_ok.copy()
    if model.has_one_way:
        bits = model.one_way_lattice(xs, ys)
        mid_bits = model.one_way_lattice(xs, v_mid)
        down_ok &= ((mid_bits & ~bits[:-1]) == 0) & ((bits[1:] & ~mid_bits) == 0) & ~v_entry
    moves = np.zeros((rows, cols), np.uint8)
    moves[:, :-1] |= np.where(h_ok, MOVE_E, 0).astype(np.uint8)
    moves[:, 1:] |= np.where(h_ok, MOVE_W, 0).astype(np.uint8)
    moves[:-1, :] |= np.where(down_ok, MOVE_S, 0).astype(np.uint8)
    moves[1:, :] |= np.where(v_ok, MOVE_N, 0).astype(np.uint8)
    return NavGrid(cell=cell, xs=xs, ys=ys, valid=valid, moves=moves, thin_gaps=thin,
                   one_way_blocked=int((v_ok & ~down_ok).sum()))


def moves_from_mask(passable: Any) -> np.ndarray:
    """Open moves between every pair of passable 4-neighbours, (rows, cols) uint8."""
    passable = np.asarray(passable, bool)
    moves = np.zeros(passable.shape, np.uint8)
    horizontal = passable[:, :-1] & passable[:, 1:]
    vertical = passable[:-1, :] & passable[1:, :]
    moves[:, :-1] |= np.where(horizontal, MOVE_E, 0).astype(np.uint8)
    moves[:, 1:] |= np.where(horizontal, MOVE_W, 0).astype(np.uint8)
    moves[:-1, :] |= np.where(vertical, MOVE_S, 0).astype(np.uint8)
    moves[1:, :] |= np.where(vertical, MOVE_N, 0).astype(np.uint8)
    return moves


def grid_bfs(passable: Any, starts: Iterable[tuple[int, int]], moves: Any = None) -> np.ndarray:
    """Breadth-first distances (in moves) on a grid, -1 where unreachable (N13).

    passable: (rows, cols) bool. starts: (row, col) pairs; impassable or out-of-grid starts
    are ignored. moves: optional (rows, cols) uint8 of open moves per cell (MOVE_E = +x,
    MOVE_S = +y, MOVE_W, MOVE_N; directed, so one-way steps are expressible); without it every
    step between passable 4-neighbours is open. A step must land on a passable cell. This is
    the reachability every map validator shares (numpy only, deterministic).
    """
    passable = np.asarray(passable, bool)
    if passable.ndim != 2:
        raise ValueError("grid_bfs needs a 2-D passable grid")
    rows, cols = passable.shape
    moves = moves_from_mask(passable) if moves is None else np.asarray(moves, np.uint8).copy()
    moves[:, -1] &= ~np.uint8(MOVE_E)
    moves[:, 0] &= ~np.uint8(MOVE_W)
    moves[-1, :] &= ~np.uint8(MOVE_S)
    moves[0, :] &= ~np.uint8(MOVE_N)
    flat_moves, flat_ok = moves.ravel(), passable.ravel()
    distance = np.full(rows * cols, -1, np.int32)
    seeds = [r * cols + c for r, c in starts if 0 <= r < rows and 0 <= c < cols and passable[r, c]]
    frontier = np.unique(np.asarray(seeds, np.int64))
    distance[frontier] = 0
    steps = ((MOVE_E, 1), (MOVE_S, cols), (MOVE_W, -1), (MOVE_N, -cols))
    level = 0
    while frontier.size:
        level += 1
        candidates = np.concatenate([frontier[(flat_moves[frontier] & bit) != 0] + delta for bit, delta in steps])
        candidates = candidates[(distance[candidates] < 0) & flat_ok[candidates]]
        frontier = np.unique(candidates)
        distance[frontier] = level
    return distance.reshape(rows, cols)


def reachable_mask(passable: Any, starts: Iterable[tuple[int, int]], moves: Any = None) -> np.ndarray:
    """grid_bfs(...) >= 0."""
    return grid_bfs(passable, starts, moves) >= 0


# --------------------------------------------------------------------------- reachability (N14)

@dataclass(frozen=True)
class Trigger:
    """A portal trigger: a closed rect [x, y, w, h] or a closed circle [cx, cy, r]."""
    rect: tuple[float, float, float, float] | None = None
    circle: tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        if (self.rect is None) == (self.circle is None):
            raise NavError("a trigger is exactly one of rect [x, y, w, h] or circle [cx, cy, r]")

    @classmethod
    def from_portal(cls, portal: Mapping[str, Any]) -> "Trigger":
        """The trigger of a bundle portal ({rect: [x, y, w, h]} or {circle: [cx, cy, r]})."""
        if "rect" in portal:
            return cls(rect=tuple(float(v) for v in portal["rect"]))
        if "circle" in portal:
            return cls(circle=tuple(float(v) for v in portal["circle"]))
        raise NavError(f"portal {portal.get('id')!r} needs rect [x, y, w, h] or circle [cx, cy, r]")

    def bounds(self) -> tuple[float, float, float, float]:
        if self.rect is not None:
            x, y, w, h = self.rect
            return x, y, x + w, y + h
        cx, cy, r = self.circle
        return cx - r, cy - r, cx + r, cy + r

    def distance(self, x: Any, y: Any) -> Any:
        """Distance from points to the closed trigger area (0 inside)."""
        if self.rect is not None:
            x0, y0, x1, y1 = self.bounds()
            dx = np.maximum(np.maximum(x0 - x, 0.0), x - x1)
            dy = np.maximum(np.maximum(y0 - y, 0.0), y - y1)
            return np.sqrt(dx * dx + dy * dy)
        cx, cy, r = self.circle
        return np.maximum(np.sqrt((x - cx) * (x - cx) + (y - cy) * (y - cy)) - r, 0.0)

    def closest_point(self, x: float, y: float) -> tuple[float, float]:
        if self.rect is not None:
            x0, y0, x1, y1 = self.bounds()
            return min(max(x, x0), x1), min(max(y, y0), y1)
        cx, cy, r = self.circle
        d = math.hypot(x - cx, y - cy)
        if d <= r:
            return x, y
        return cx + (x - cx) * r / d, cy + (y - cy) * r / d


@dataclass
class Reach:
    """The answer for one start or target: reachable, in how many moves, where the actor stands
    (a node centre, or the trigger point a crossing exit is entered at), and why not."""
    reachable: bool = False
    steps: int | None = None
    node: tuple[float, float] | None = None
    cell: tuple[int, int] | None = None  # (row, col) of the node reached
    reason: str | None = None


def attach(model: CollisionModel, grid: NavGrid, point: Sequence[float]) -> list[tuple[int, int]]:
    """Valid nodes within two cells of a valid point that the actor reaches in a straight line,
    nearest first (how spawns, arrivals and point targets join the grid, N14)."""
    x, y = float(point[0]), float(point[1])
    if not model.valid(x, y):
        return []
    row, col = grid.node_of(x, y)
    found = []
    for r in range(max(0, row - 2), min(grid.rows, row + 3)):
        for c in range(max(0, col - 2), min(grid.cols, col + 3)):
            if grid.valid[r, c]:
                found.append(((grid.xs[c] - x) ** 2 + (grid.ys[r] - y) ** 2, r, c))
    return [(r, c) for _, r, c in sorted(found) if model.segment_clear((x, y), (grid.xs[c], grid.ys[r]))]


def _nodes_within(grid: NavGrid, x0: float, y0: float, x1: float, y1: float) -> tuple[np.ndarray, np.ndarray]:
    c0, c1 = max(0, math.floor(x0 / grid.cell) - 1), min(grid.cols, math.floor(x1 / grid.cell) + 2)
    r0, r1 = max(0, math.floor(y0 / grid.cell) - 1), min(grid.rows, math.floor(y1 / grid.cell) + 2)
    rr, cc = np.mgrid[r0:r1, c0:c1]
    return rr.ravel(), cc.ravel()


@dataclass
class Navigation:
    """BFS from a set of starts over one map's grid, and the target checks of N14."""
    model: CollisionModel
    grid: NavGrid
    distance: np.ndarray  # grid_bfs distances from every start (-1 unreachable)
    starts: list[Reach]  # one per start, in the order given
    seeds: list[tuple[int, int]]  # every (row, col) a start joined

    @property
    def reachable(self) -> np.ndarray:
        return self.distance >= 0

    def _best(self, nodes: Iterable[tuple[int, int]]) -> tuple[int, int] | None:
        reached = [(int(self.distance[r, c]), int(r), int(c)) for r, c in nodes if self.distance[r, c] >= 0]
        if not reached:
            return None
        _, r, c = min(reached)
        return r, c

    def _mark(self, node: tuple[int, int] | None, reason: str) -> Reach:
        if node is None:
            return Reach(reason=reason)
        r, c = node
        return Reach(True, int(self.distance[r, c]), (float(self.grid.xs[c]), float(self.grid.ys[r])), (r, c))

    def point_target(self, point: Sequence[float]) -> Reach:
        """The actor must stand at the point: valid, and joined to a reached node."""
        if not self.model.valid(float(point[0]), float(point[1])):
            return Reach(reason=_REASON_STAND)
        nodes = attach(self.model, self.grid, point)
        return self._mark(self._best(nodes),
                          "no reachable node within two cells in a clear straight line" if nodes else
                          "valid but off the grid (no valid node within two cells in a clear straight line)")

    def reach_target(self, point: Sequence[float], reach: float) -> Reach:
        """A reached node centre within ``reach`` of the point (dx * dx + dy * dy <= reach * reach)."""
        x, y, reach = float(point[0]), float(point[1]), float(reach)
        rr, cc = _nodes_within(self.grid, x - reach, y - reach, x + reach, y + reach)
        dx, dy = self.grid.xs[cc] - x, self.grid.ys[rr] - y
        close = dx * dx + dy * dy <= reach * reach
        return self._mark(self._best(zip(rr[close], cc[close])), f"no reachable node within reach {reach:g} px")

    def exit_target(self, trigger: Trigger, activation: str = "crossing", radius: float = 0.0) -> Reach:
        """intent: a reached node within ``radius`` of the trigger; crossing: a reached node inside
        it, or the nearest reached node within two cells whose straight move into it is clear."""
        grid, radius = self.grid, float(radius)
        x0, y0, x1, y1 = trigger.bounds()
        margin = radius if activation == "intent" else 2 * grid.cell
        rr, cc = _nodes_within(grid, x0 - margin, y0 - margin, x1 + margin, y1 + margin)
        gap = trigger.distance(grid.xs[cc], grid.ys[rr])
        reached = self.distance[rr, cc] >= 0
        if activation == "intent":
            near = gap <= radius
            return self._mark(self._best(zip(rr[near], cc[near])),
                              f"no reachable node within the activation radius {radius:g} px")
        inside = reached & (gap == 0)
        if inside.any():
            return self._mark(self._best(zip(rr[inside], cc[inside])), "")
        for k in np.argsort(gap, kind="stable"):
            if reached[k] and gap[k] <= margin:
                node = (float(grid.xs[cc[k]]), float(grid.ys[rr[k]]))
                closest = trigger.closest_point(*node)
                if self.model.valid(*closest) and self.model.segment_clear(node, closest):
                    found = self._mark((int(rr[k]), int(cc[k])), "")
                    found.node = closest
                    return found
        return Reach(reason="the actor's centre cannot enter the trigger from any reachable node")


def navigate(model: CollisionModel, starts: Iterable[Sequence[float]], grid: NavGrid | None = None) -> Navigation:
    """Join every start to the grid and run the BFS (N14). Each start's Reach is reachable with
    steps 0 when it joined, else carries "not a valid actor position" or "cannot reach any grid node"."""
    grid = build_grid(model) if grid is None else grid
    results: list[Reach] = []
    seeds: list[tuple[int, int]] = []
    for point in starts:
        nodes = attach(model, grid, point)
        if nodes:
            seeds.extend(nodes)
            r, c = nodes[0]
            results.append(Reach(True, 0, (float(grid.xs[c]), float(grid.ys[r])), (r, c)))
        else:
            valid = bool(model.valid(float(point[0]), float(point[1])))
            results.append(Reach(reason="not a valid actor position" if not valid else "cannot reach any grid node"))
    return Navigation(model, grid, grid_bfs(grid.valid, seeds, grid.moves), results, seeds)


# --------------------------------------------------------------------------- the blocking set (N4-N8)

def _number(value: Any, where: str, *, positive: bool = False, minimum: float | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise NavError(f"{where} must be a finite number, got {value!r}")
    if positive and value <= 0:
        raise NavError(f"{where} must be greater than 0, got {value!r}")
    if minimum is not None and value < minimum:
        raise NavError(f"{where} must be at least {minimum:g}, got {value!r}")
    return value


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise NavError(f"{where} must be true or false, got {value!r}")
    return value


def footprint_solid(x: float, y: float, footprint: Mapping[str, Any] | None, *, scale: float = 1.0,
                    flip_x: bool = False, source: str = "") -> dict | None:
    """The world solid of a footprint anchored at (x, y) (N6), or None for shape none or a
    missing footprint. Measured in prop pixels and scaled once by ``scale`` unless basis is
    "world_px" (D7); flip_x mirrors it around x (D6). Never inflated by the actor radius."""
    if not footprint or footprint.get("shape") not in ("ellipse", "rect"):
        return None
    basis = footprint.get("basis", "prop_px")
    if basis not in FOOTPRINT_BASES:
        raise NavError(f"{source or 'footprint'}: footprint basis {basis!r} is not one of {', '.join(FOOTPRINT_BASES)}")
    k = 1.0 if basis == "world_px" else scale
    offset = footprint.get("offset") or [0, 0]
    ox, oy = offset[0], offset[1]
    rotate = float(footprint.get("rotate", 0) or 0)
    if flip_x:
        if ox:
            ox = -ox
        if rotate:
            rotate = -rotate
    cx, cy = x + k * ox, y + k * oy
    width, depth = k * footprint["width"], k * footprint["depth"]
    if width <= 0 or depth <= 0:
        return None  # no area: blocks nothing (N4)
    if footprint["shape"] == "ellipse":
        return {"shape": "ellipse", "cx": cx, "cy": cy, "rx": width / 2, "ry": depth / 2, "rotate": rotate,
                "source": source}
    if rotate == 0:
        return {"shape": "rect", "x": cx - width / 2, "y": cy - depth / 2, "w": width, "h": depth, "source": source}
    theta = math.radians(rotate)
    cos, sin = math.cos(theta), math.sin(theta)
    corners = [(-width / 2, -depth / 2), (width / 2, -depth / 2), (width / 2, depth / 2), (-width / 2, depth / 2)]
    points = [[cx + u * cos - v * sin, cy + u * sin + v * cos] for u, v in corners]
    return {"shape": "polygon", "points": points, "source": source}


def object_solid(obj: Mapping[str, Any], prop: Mapping[str, Any] | None = None) -> dict | None:
    """The blocking footprint of one bundle object (N6) or None. ``prop`` is the object's
    props-registry item (already merged with a prop-pack item), if any."""
    ident = str(obj.get("id", ""))
    where = f"object {ident!r}"
    footprint = obj.get("footprint", prop.get("footprint") if prop else None)
    if footprint is not None and not isinstance(footprint, Mapping):
        raise NavError(f"{where}: footprint must be an object")
    solid = obj.get("solid", prop.get("solid") if prop and prop.get("solid") is not None else None)
    if solid is None:
        solid = bool(footprint) and footprint.get("shape") in ("ellipse", "rect")
    if not _flag(solid, f"{where} solid") or not footprint or footprint.get("shape") not in ("ellipse", "rect"):
        return None
    for key in ("width", "depth"):
        _number(footprint.get(key), f"{where} footprint.{key}", minimum=0)
    offset = footprint.get("offset")
    if offset is not None and not (isinstance(offset, (list, tuple)) and len(offset) == 2):
        raise NavError(f"{where}: footprint.offset must be [x, y]")
    if offset:
        for value in offset:
            _number(value, f"{where} footprint.offset")
    if "rotate" in footprint:
        _number(footprint["rotate"], f"{where} footprint.rotate")
    x = float(_number(obj.get("x"), f"{where} x"))
    y = float(_number(obj.get("y"), f"{where} y"))
    scale = float(_number(obj.get("scale", 1), f"{where} scale", positive=True))
    flip = _flag(obj.get("flip_x", False), f"{where} flip_x")
    return footprint_solid(x, y, footprint, scale=scale, flip_x=flip, source=f"object:{ident}")


@dataclass
class TileLayer:
    """One placed tiles layer for tile_solids(): tileset-local indices (-1 empty) and the tileset's tiles[]."""
    name: str
    grid: np.ndarray  # (rows, cols) integer tile indices, -1 for an empty cell
    tile_w: int
    tile_h: int
    tiles: Mapping[int, Mapping[str, Any]]  # tiles[] entries by index
    tile_count: int | None = None  # tiles in the atlas; default: one past the largest index in use


def _integral_rect(shape: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    if shape.get("shape") != "rect":
        return None
    values = [shape.get(key) for key in ("x", "y", "w", "h")]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer() for v in values) \
            or values[2] <= 0 or values[3] <= 0:
        return None
    x, y, w, h = (int(v) for v in values)
    return x, y, x + w, y + h


def _translate_solid(shape: Mapping[str, Any], dx: int, dy: int, source: str) -> dict:
    moved = {key: value for key, value in shape.items() if key != "id"}
    if shape["shape"] == "rect":
        moved.update(x=shape["x"] + dx, y=shape["y"] + dy)
    elif shape["shape"] == "ellipse":
        moved.update(cx=shape["cx"] + dx, cy=shape["cy"] + dy)
    else:
        moved["points"] = [[px + dx, py + dy] for px, py in shape["points"]]
    moved["source"] = source
    return moved


def tile_solids(layers: Iterable[TileLayer]) -> list[dict]:
    """World solids from the collision of every placed tile (N7), layer by layer.

    Whole-pixel rects are clipped to their cell, unioned per layer and re-merged into
    disjoint rects (the same closed set); every other shape, and any whole-pixel rect that
    reaches outside its cell, is kept as its own solid for each placed tile.
    """
    solids: list[dict] = []
    for layer in layers:
        grid = np.asarray(layer.grid, np.int64)
        tw, th = int(layer.tile_w), int(layer.tile_h)
        count = layer.tile_count
        if count is None:
            used = int(grid.max()) + 1 if grid.size else 0
            count = max(1, used, max((int(i) + 1 for i in layer.tiles), default=0))
        tile_masks = np.zeros((count + 1, th, tw), bool)
        loose: dict[int, list[Mapping[str, Any]]] = {}
        for index, tile in layer.tiles.items():
            index = int(index)
            if not 0 <= index < count:
                continue
            shapes = list(tile.get("collision") or [])
            if not shapes and (tile.get("properties") or {}).get("walkable") is False:
                shapes = [{"shape": "rect", "x": 0, "y": 0, "w": tw, "h": th}]
            for shape in shapes:
                box = _integral_rect(shape)
                if box is not None:
                    x0, y0, x1, y1 = (min(max(v, 0), limit) for v, limit in zip(box, (tw, th, tw, th)))
                    tile_masks[index, y0:y1, x0:x1] = True
                    if box != (x0, y0, x1, y1):  # the part outside the cell stays an exact solid
                        loose.setdefault(index, []).append(shape)
                elif _has_area(shape):  # shapes without area block nothing (N4)
                    loose.setdefault(index, []).append(shape)
        cells = np.where((grid >= 0) & (grid < count), grid, count)
        rows, cols = cells.shape
        mask = tile_masks[cells].transpose(0, 2, 1, 3).reshape(rows * th, cols * tw)
        source = f"tiles:{layer.name}"
        for x, y, w, h in merge_rects(mask):
            solids.append({"shape": "rect", "x": x, "y": y, "w": w, "h": h, "source": source})
        for index, shapes in sorted(loose.items()):
            for row, col in zip(*np.nonzero(grid == index)):
                for shape in shapes:
                    solids.append(_translate_solid(shape, int(col) * tw, int(row) * th, source))
    return solids


def material_codes(index: Any, classes: Sequence[str], walkable: Sequence[bool]) -> np.ndarray:
    """(h, w) uint8 FREE, BLOCK or ONE_WAY per material pixel (N8) from the material index of
    every pixel (-1 = no material) and each material's class and walkable flag."""
    codes = np.zeros(len(classes) + 1, np.uint8)  # the extra last entry is "no material"
    for i, (klass, walk) in enumerate(zip(classes, walkable)):
        if klass not in MATERIAL_CLASSES:
            raise NavError(f"material class {klass!r} is not one of {', '.join(MATERIAL_CLASSES)}")
        if klass == "solid" or (klass in ("liquid", "hazard") and not walk):
            codes[i] = BLOCK
        elif klass == "one_way":
            codes[i] = ONE_WAY
    return codes[np.asarray(index, np.int64)]


def _polygon_array(points: Any, where: str) -> np.ndarray:
    try:
        array = np.asarray(points, np.float64)
    except (TypeError, ValueError):
        raise NavError(f"{where} must be a list of [x, y] points") from None
    if array.ndim != 2 or array.shape[0] < 3 or array.shape[1] != 2 or not np.isfinite(array).all():
        raise NavError(f"{where} must be at least three finite [x, y] points")
    if _polygon_area2(array) == 0:
        raise NavError(f"{where}: polygon has zero area")
    return array


def collision_shapes(collision: Mapping[str, Any]) -> tuple[list[tuple[np.ndarray, list[np.ndarray]]], list[dict],
                                                              list[dict]]:
    """(walk regions, collision.solids, collision.rects) of a bundle's collision block, in the
    form CollisionModel takes (N3, N4a, N4b). Each solid keeps its fields plus "source" (its
    id, else collision.solids[i]); each rect becomes {"shape": "rect", ...} with source
    collision.rects[i]; zero-size rects and ellipses are dropped (they block nothing)."""
    if not isinstance(collision, Mapping):
        raise NavError("collision must be an object")
    regions: list[tuple[np.ndarray, list[np.ndarray]]] = []
    for i, region in enumerate(collision.get("walkRegions") or []):
        where = f"collision.walkRegions[{i}]"
        if not isinstance(region, Mapping):
            raise NavError(f"{where} must be an object")
        polygon = _polygon_array(region.get("polygon"), f"{where}.polygon")
        holes = [_polygon_array(hole, f"{where}.holes[{k}]") for k, hole in enumerate(region.get("holes") or [])]
        regions.append((polygon, holes))
    solids: list[dict] = []
    for i, shape in enumerate(collision.get("solids") or []):
        where = f"collision.solids[{i}]"
        if not isinstance(shape, Mapping) or shape.get("shape") not in ("rect", "ellipse", "polygon"):
            raise NavError(f"{where} must be a rect, ellipse or polygon solid")
        keys = {"rect": ("x", "y", "w", "h"), "ellipse": ("cx", "cy", "rx", "ry"), "polygon": ()}[shape["shape"]]
        for key in keys:
            _number(shape.get(key), f"{where}.{key}")
        if shape["shape"] == "ellipse" and "rotate" in shape:
            _number(shape["rotate"], f"{where}.rotate")
        if shape["shape"] == "polygon":
            _polygon_array(shape.get("points"), f"{where}.points")
        normalised = dict(shape, source=shape.get("id", f"collision.solids[{i}]"))
        if _has_area(normalised):
            solids.append(normalised)
    rects: list[dict] = []
    for i, rect in enumerate(collision.get("rects") or []):
        where = f"collision.rects[{i}]"
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise NavError(f"{where} must be [x, y, w, h]")
        x, y, w, h = (_number(v, where) for v in rect)
        if w <= 0 or h <= 0:
            continue
        rects.append({"shape": "rect", "x": x, "y": y, "w": w, "h": h, "source": f"collision.rects[{i}]"})
    return regions, solids, rects


@dataclass
class BlockingSet:
    """The D2 blocking set of one map, plus the walk area and the actor (N2-N8)."""
    width: float
    height: float
    actor_radius: float
    y_squash: float = 1.0
    regions: list[tuple[np.ndarray, list[np.ndarray]]] = field(default_factory=list)
    collision_solids: list[dict] = field(default_factory=list)  # N4a
    rects: list[dict] = field(default_factory=list)  # N4b
    footprints: list[dict] = field(default_factory=list)  # N4c, source "object:<id>"
    tiles: list[dict] = field(default_factory=list)  # N4d, source "tiles:<layer>"
    material_codes: np.ndarray | None = None  # N4e: (h, w) FREE / BLOCK / ONE_WAY
    material_scale: int = 1
    materials: list[dict] = field(default_factory=list)  # {"name", "class", "walkable", "code"} in bundle order

    @property
    def solids(self) -> list[dict]:
        """Every blocking shape in map_nav order: collision.solids, rects, footprints, tiles."""
        return [*self.collision_solids, *self.rects, *self.footprints, *self.tiles]

    @property
    def blocking_material_classes(self) -> list[str]:
        """Material classes of this map that block (solid, liquid/hazard not walkable) or are one_way."""
        found = {entry["class"] for entry in self.materials if entry["code"] != FREE}
        return [klass for klass in MATERIAL_CLASSES if klass in found]

    def model(self) -> CollisionModel:
        return CollisionModel(self.width, self.height, self.actor_radius, self.y_squash, self.regions, self.solids,
                              self.material_codes, self.material_scale)


# --------------------------------------------------------------------------- reading a bundle

def read_json(path: str | os.PathLike) -> Any:
    """Strict JSON (D28): UTF-8 with an optional BOM, no NaN or Infinity, no duplicate keys."""
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise NavError(f"cannot read JSON {Path(path).name}: duplicate key {key!r}")
            result[key] = value
        return result

    def no_constants(name: str) -> Any:
        raise NavError(f"cannot read JSON {Path(path).name}: {name} is not valid JSON")

    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise NavError(f"cannot read JSON {Path(path).name}: {error}") from None
    try:
        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=no_constants)
    except NavError:
        raise
    except ValueError as error:
        raise NavError(f"cannot read JSON {Path(path).name}: {error}") from None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def upgrade_v1_footprints(doc: Mapping[str, Any]) -> dict:
    """A copy of a map_bundle.v1 document whose object footprints use the v2 fields, the way
    map_bundle.py reads v1: {type|shape: ellipse, rx, ry, cx?, cy?} becomes an ellipse
    width 2 rx, depth 2 ry, offset [cx, cy]; {x?, y?, w, h} becomes a rect width w, depth h
    with offset the rect centre relative to the anchor."""
    doc = copy.deepcopy(dict(doc))
    for obj in doc.get("objects") or []:
        fp = obj.get("footprint") if isinstance(obj, dict) else None
        if not isinstance(fp, dict) or "shape" in fp and "width" in fp:
            continue
        kind = fp.get("shape", fp.get("type"))
        if kind == "ellipse" and _is_number(fp.get("rx")) and _is_number(fp.get("ry")):
            obj["footprint"] = {"shape": "ellipse", "width": 2 * fp["rx"], "depth": 2 * fp["ry"],
                                "offset": [fp.get("cx", 0), fp.get("cy", 0)]}
        elif kind in ("rect", None) and all(_is_number(fp.get(k)) for k in ("w", "h")):
            obj["footprint"] = {"shape": "rect", "width": fp["w"], "depth": fp["h"],
                                "offset": [fp.get("x", -fp["w"] / 2) + fp["w"] / 2,
                                           fp.get("y", -fp["h"] / 2) + fp["h"] / 2]}
    return doc


def _size2(value: Any, where: str) -> tuple[int, int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value, value
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in value):
        return int(value[0]), int(value[1])
    raise NavError(f"{where} must be a positive whole number or [width, height]")


def _rgb(colour: Any, where: str) -> tuple[int, int, int]:
    if isinstance(colour, str) and len(colour) >= 7 and colour.startswith("#"):
        try:
            return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)
        except ValueError:
            pass
    elif isinstance(colour, (list, tuple)) and len(colour) >= 3 and all(
            isinstance(v, int) and not isinstance(v, bool) for v in colour[:3]):
        return int(colour[0]), int(colour[1]), int(colour[2])
    raise NavError(f"{where} must be #rrggbb or [r, g, b], got {colour!r}")


class _Reader:
    """The collision-relevant part of a map bundle, read the way map_bundle.py reads it."""

    def __init__(self, doc: Any, base_dir: Path) -> None:
        if not isinstance(doc, Mapping):
            raise NavError("a map bundle must be a JSON object")
        if doc.get("schema") not in BUNDLE_SCHEMAS:
            raise NavError(f"schema {doc.get('schema')!r} is not one of {', '.join(BUNDLE_SCHEMAS)}")
        self.version = 1 if doc["schema"] == BUNDLE_SCHEMAS[0] else 2
        self.doc = upgrade_v1_footprints(doc) if self.version == 1 else doc
        self.base = Path(base_dir)

    def path(self, rel: Any, where: str, base: Path | None = None) -> Path:
        if not isinstance(rel, str) or not _REL_PATH.match(rel):
            raise NavError(f"{where} must be a relative POSIX path, got {rel!r}")
        path = ((base or self.base) / rel).resolve()
        if not path.is_file():
            raise NavError(f"{where}: file not found: {rel}")
        return path

    def run(self) -> BlockingSet:
        doc = self.doc
        tile_size = _size2(doc["tile_size"], "tile_size") if doc.get("tile_size") is not None else None
        tilesets = self.tilesets(tile_size)
        layers, grids, image_layers = self.layers(tilesets)
        width, height = self.world(tile_size, grids, image_layers)
        collision = doc.get("collision")
        if collision is None:
            raise NavError("the bundle has no collision block (navigation needs collision.actorRadius)")
        if not isinstance(collision, Mapping):
            raise NavError("collision must be an object")
        radius = float(_number(collision.get("actorRadius"), "collision.actorRadius", minimum=0))
        y_squash = float(_number(collision.get("ySquash", 1.0), "collision.ySquash", positive=True))
        regions, solids, rects = collision_shapes(collision)
        footprints = self.footprints()
        codes, scale, materials = self.material_map(width, height)
        return BlockingSet(width, height, radius, y_squash, regions, solids, rects, footprints, tile_solids(layers),
                           codes, scale, materials)

    # -- tiles

    def tilesets(self, tile_size: tuple[int, int] | None) -> dict[str, tuple[int, int, dict[int, Mapping]]]:
        found: dict[str, tuple[int, int, dict[int, Mapping]]] = {}
        for i, entry in enumerate(self.doc.get("tilesets") or []):
            where = f"tilesets[{i}]"
            if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str) or not entry["id"]:
                raise NavError(f"{where} needs an id")
            if entry["id"] in found:
                raise NavError(f"{where}: duplicate tileset id {entry['id']!r}")
            if "manifest" in entry:
                manifest = read_json(self.path(entry["manifest"], f"{where}.manifest"))
                if not isinstance(manifest, Mapping):
                    raise NavError(f"{where}.manifest must hold a tileset object")
                tw, th = _size2(manifest.get("tile_size"), f"{where} tile_size")
                tiles_list = manifest.get("tiles")
            elif self.version == 1 and isinstance(entry.get("image"), str):
                if tile_size is None:
                    raise NavError(f"{where}: a v1 inline tileset needs the bundle's tile_size")
                tw, th = tile_size
                tiles_list = entry.get("tiles") or [{"index": 0}]
            else:
                raise NavError(f"{where} needs a manifest")
            if tile_size is not None and (tw, th) != tile_size:
                raise NavError(f"{where}: tileset tiles are {tw}x{th}, the bundle's are {tile_size[0]}x{tile_size[1]}")
            if not isinstance(tiles_list, list):
                raise NavError(f"{where}: tiles must be a list")
            tiles: dict[int, Mapping] = {}
            for k, tile in enumerate(tiles_list):
                if not isinstance(tile, Mapping) or not isinstance(tile.get("index"), int) \
                        or isinstance(tile.get("index"), bool) or tile["index"] < 0:
                    raise NavError(f"{where} tiles[{k}] needs a whole index >= 0")
                if tile["index"] in tiles:
                    raise NavError(f"{where} tiles[{k}]: duplicate tile index {tile['index']}")
                tiles[tile["index"]] = tile
            found[entry["id"]] = (tw, th, tiles)
        return found

    def tile_grid(self, data: Any, where: str) -> np.ndarray:
        rows: Any = data
        if isinstance(data, str):
            path = self.path(data, f"{where}.data")
            try:
                if path.suffix.lower() == ".csv":
                    lines = path.read_text(encoding="utf-8-sig").splitlines()
                    rows = [[int(cell) for cell in line.split(",")] for line in lines if line.strip()]
                else:
                    rows = read_json(path)
                    rows = rows.get("data") if isinstance(rows, dict) else rows
            except (OSError, UnicodeDecodeError, ValueError) as error:
                raise NavError(f"{where}.data: cannot read tile data {data}: {error}") from None
        if (not isinstance(rows, list) or not rows or not all(isinstance(row, list) and row for row in rows)
                or len({len(row) for row in rows}) != 1):
            raise NavError(f"{where}.data: tile data must be a non-empty list of equally long rows")
        if not all(isinstance(v, int) and not isinstance(v, bool) or v is None for row in rows for v in row):
            raise NavError(f"{where}.data: tile data holds whole tile indices (-1 or null for empty)")
        grid = np.array([[-1 if v is None else v for v in row] for row in rows], np.int64)
        if (grid < -1).any():
            raise NavError(f"{where}.data: tile indices below -1")
        return grid

    def layers(self, tilesets: dict) -> tuple[list[TileLayer], list[tuple[str, np.ndarray]], list[Path]]:
        """(tiles layers with collision, every tiles grid (name, grid), image layer files)."""
        tile_layers: list[TileLayer] = []
        grids: list[tuple[str, np.ndarray]] = []
        images: list[Path] = []
        for i, entry in enumerate(self.doc.get("layers") or []):
            where = f"layers[{i}]"
            if not isinstance(entry, Mapping):
                raise NavError(f"{where} must be an object")
            if entry.get("kind") == "image" and isinstance(entry.get("image"), str):
                images.append(self.path(entry["image"], f"{where}.image"))
            if entry.get("kind") != "tiles":
                continue
            name = str(entry.get("name", f"layer{i}"))
            grid = self.tile_grid(entry.get("data"), where)
            grids.append((name, grid))
            tileset = entry.get("tileset")
            if tileset is None:
                if len(tilesets) != 1:
                    if self.version == 1:
                        continue  # map_bundle warns and leaves the layer without tile collision
                    raise NavError(f"{where}: a tiles layer must name its tileset when the bundle has none or several")
                tileset = next(iter(tilesets))
            if tileset not in tilesets:
                raise NavError(f"{where}: unknown tileset {tileset!r}")
            tw, th, tiles = tilesets[tileset]
            tile_layers.append(TileLayer(name, grid, tw, th, tiles))
        return tile_layers, grids, images

    def world(self, tile_size: tuple[int, int] | None, grids: list[tuple[str, np.ndarray]],
              images: list[Path]) -> tuple[float, float]:
        """world {width, height}; without it the first tiles layer (in bundle tiles), else the
        first image layer; every tiles layer must cover the world exactly (map_bundle's rules)."""
        if grids and tile_size is None:
            raise NavError("tiles layers need the bundle's tile_size")
        world = self.doc.get("world")
        if world is not None:
            if not isinstance(world, Mapping):
                raise NavError("world must be {width, height, unit: px}")
            width = float(_number(world.get("width"), "world.width", positive=True))
            height = float(_number(world.get("height"), "world.height", positive=True))
        elif grids:
            rows, cols = grids[0][1].shape
            width, height = float(cols * tile_size[0]), float(rows * tile_size[1])
        elif images:
            from PIL import Image
            with Image.open(images[0]) as handle:
                width, height = (float(v) for v in handle.size)
        else:
            raise NavError("no world size: give world {width, height, unit: px}")
        for name, grid in grids:
            extent = (grid.shape[1] * tile_size[0], grid.shape[0] * tile_size[1])
            if extent != (width, height):
                raise NavError(f"tiles layer {name!r} covers {extent[0]}x{extent[1]} px, the world is "
                               f"{width:g}x{height:g}")
        return width, height

    # -- objects

    def props(self) -> dict[str, Mapping]:
        registry = self.doc.get("props")
        if registry is None:
            return {}
        if not isinstance(registry, Mapping):
            raise NavError("props must be an object of prop id -> {image | pack + label, ...}")
        found: dict[str, Mapping] = {}
        for name, entry in registry.items():
            where = f"props[{name!r}]"
            if not isinstance(entry, Mapping):
                raise NavError(f"{where} must be an object")
            item: dict = {}
            if "pack" in entry:
                manifest = read_json(self.path(entry["pack"], f"{where}.pack"))
                accepted = manifest.get("accepted") if isinstance(manifest, Mapping) else None
                label = entry.get("label")
                matches = [dict(i) for i in accepted or [] if isinstance(i, Mapping) and i.get("label") == label]
                if not matches:
                    raise NavError(f"{where}: prop pack {entry['pack']} has no accepted item {label!r}")
                item = matches[0]
            merged = {**item, **{k: v for k, v in entry.items() if k not in ("pack", "label")}}
            if merged.get("solid") is not None:
                _flag(merged["solid"], f"{where}.solid")
            found[name] = merged
        return found

    def footprints(self) -> list[dict]:
        props = self.props()
        solids: list[dict] = []
        for i, obj in enumerate(self.doc.get("objects") or []):
            if not isinstance(obj, Mapping) or not isinstance(obj.get("id"), str):
                raise NavError(f"objects[{i}] needs an id")
            prop = props.get(obj.get("prop"))
            if props and prop is None:
                raise NavError(f"objects[{i}] ({obj['id']}): unknown prop {obj.get('prop')!r}")
            solid = object_solid(obj, prop)
            if solid is not None:
                solids.append(solid)
        return solids

    # -- material map

    def material_map(self, width: float, height: float) -> tuple[np.ndarray | None, int, list[dict]]:
        block = self.doc.get("material_map")
        if block is None:
            return None, 1, []
        if not isinstance(block, Mapping) or not isinstance(block.get("materials"), Mapping):
            raise NavError("material_map needs an image and a materials object")
        path = self.path(block.get("image"), "material_map.image")
        names = list(block["materials"])
        entries = [block["materials"][name] for name in names]
        for name, entry in zip(names, entries):
            if not isinstance(entry, Mapping) or entry.get("class") not in MATERIAL_CLASSES:
                raise NavError(f"material_map.materials[{name!r}].class must be one of {', '.join(MATERIAL_CLASSES)}")
            if "walkable" in entry:
                _flag(entry["walkable"], f"material_map.materials[{name!r}].walkable")
        from PIL import Image
        try:
            with Image.open(path) as handle:
                mode, size = handle.mode, handle.size
                by_index = mode in ("P", "L") and all("index" in entry for entry in entries)
                raw = np.asarray(handle) if by_index else None
        except OSError as error:
            raise NavError(f"material_map.image: cannot open {block['image']}: {error}") from None
        scale = width / size[0]
        if not scale.is_integer() or scale < 1 or scale * size[1] != height:
            raise NavError(f"material_map.image: {size[0]}x{size[1]} px does not divide the {width:g}x{height:g} "
                           "world into whole squares")
        index = np.full((size[1], size[0]), -1, np.int16)
        if by_index:
            values = [entry["index"] for entry in entries]
            if len(set(values)) != len(values):
                raise NavError("material_map.materials: material indices must be unique")
            for i, value in enumerate(values):
                index[raw == value] = i
            unmatched = index < 0
        else:
            if not all("color" in entry for entry in entries):
                raise NavError("material_map.materials: every material needs a color (or an index for a P/L image)")
            colours = [_rgb(entry["color"], f"material_map.materials[{name!r}].color")
                       for name, entry in zip(names, entries)]
            if len(set(colours)) != len(colours):
                raise NavError("material_map.materials: material colors must be unique")
            rgba = _load_rgba(path)
            for i, colour in enumerate(colours):
                index[(rgba[..., 3] > 0) & np.all(rgba[..., :3] == colour, axis=-1)] = i
            unmatched = (index < 0) & (rgba[..., 3] > 0)
        if unmatched.any():
            y, x = np.argwhere(unmatched)[0]
            raise NavError(f"material_map.image: {int(unmatched.sum())} pixel(s) match no material (first at "
                           f"x={x}, y={y})")
        classes = [entry["class"] for entry in entries]
        walkable = [bool(entry.get("walkable", False)) for entry in entries]
        lut = material_codes(np.arange(-1, len(classes)), classes, walkable)
        materials = [{"name": name, "class": klass, "walkable": walk, "code": int(lut[i + 1])}
                     for i, (name, klass, walk) in enumerate(zip(names, classes, walkable))]
        return material_codes(index, classes, walkable), int(scale), materials


def _load_rgba(path: Path) -> np.ndarray:
    """8-bit straight RGBA pixels, decoded exactly as forge_core.load_rgba decodes them."""
    forge_core = _forge_core("reading a colour material map", "load_rgba")
    try:
        return np.asarray(forge_core.load_rgba(path)[0])
    except (OSError, ValueError) as error:
        raise NavError(f"material_map.image: cannot read {Path(path).name}: {error}") from None


def blocking_set_from_document(doc: Any, base_dir: str | os.PathLike) -> BlockingSet:
    """The blocking set of a bundle document whose relative paths resolve from ``base_dir``
    (the folder of the bundle file). Raises NavError when collision cannot be read."""
    return _Reader(doc, Path(base_dir)).run()


def read_blocking_set(path: str | os.PathLike) -> BlockingSet:
    """Read a map bundle file (JSON, BOM tolerated) and return its blocking set."""
    path = Path(path)
    return blocking_set_from_document(read_json(path), path.resolve().parent)
