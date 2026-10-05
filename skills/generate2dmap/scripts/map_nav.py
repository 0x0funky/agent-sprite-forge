#!/usr/bin/env python3
"""Collision and navigation from map-bundle data, with reachability, portal and anchor checks.

Verbs:
  check  build the navigation grid of a map bundle and prove that every interaction, exit,
         anchor slot and approach point can be reached from the spawns and arrivals; check
         portal triggers and arrivals (and, with --link, the links between maps). Publishes
         nav-grid.json, nav-report.json and nav-debug.png into a new folder; a failed check
         publishes nothing unless --publish-on-fail.
  query  print whether points are valid actor positions and whether segments are clear.

Collision semantics (plan Appendix C; map-runtime.mjs implements the same rules):
  * World pixels, y down. The actor footprint is an ellipse rx = r, ry = r * ySquash.
  * A point P is valid when P and 8 samples on the footprint ellipse (0, 45, ..., 315
    degrees; the diagonals use Math.SQRT1_2) all lie in the walk area (inside a walk region
    polygon and outside its holes, even-odd test; without regions the closed world box) and
    outside every blocker: solids and collision.rects (closed shapes), solid object
    footprints scaled once by the instance scale, tile collision, and material-map pixels
    of class solid (or liquid/hazard unless walkable).
  * The grid cell is max(1, round(r / 2)) px (half up); node (col, row) sits at
    ((col + 0.5) * cell, (row + 0.5) * cell). A 4-neighbour move between two valid nodes is
    open when segmentClear holds between them.
  * segmentClear(a, b): every sample a + (b - a) * k / n, n = ceil(|b - a| / (cell / 2)),
    is valid, and (thin-gap rule) the actor's centre stays in the walk area and outside
    every blocker along the whole segment, tested exactly between boundary crossings, so a
    gap or wall thinner than the sample spacing is never jumped.
  * one_way material blocks from above only: a move with a downward (+y) component may not
    bring a footprint sample, nor the centre path, from another material onto one_way.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)
import map_bundle  # noqa: E402
from map_bundle import BLOCK, ONE_WAY, BundleError, merge_rects, nav_cell  # noqa: E402

TOOL_NAME = "map_nav.py"
TOOL_VERSION = "1.0.0"
GRID_SCHEMA = "generate2dmap.nav_grid.v1"
REPORT_SCHEMA = "generate2dmap.nav_report.v1"
MOVE_E, MOVE_S, MOVE_W, MOVE_N = 1, 2, 4, 8
MAX_GRID_NODES = 1 << 24  # a 4096 x 4096 node grid; larger maps should be split into chunks
SQRT1_2 = 0.7071067811865476  # Math.SQRT1_2, the exact double JavaScript uses
_PAD = 1e-7  # bounding boxes are widened by this much for culling only; never for decisions
_HEX = np.array(list("0123456789abcdef"))

__all__ = [
    "CollisionModel", "NavGrid", "build_grid", "grid_bfs", "reachable_mask", "moves_from_mask",
    "merge_rects", "nav_cell", "footprint_offsets", "pnpoly", "check_bundle", "MOVE_E", "MOVE_S",
    "MOVE_W", "MOVE_N",
]


# --------------------------------------------------------------------------- primitive geometry

def footprint_offsets(radius: float, y_squash: float = 1.0) -> np.ndarray:
    """The 9 sample offsets of plan Appendix C, (9, 2): the centre, then 0, 45, ..., 315 degrees
    on the ellipse rx = r, ry = r * ySquash (y down, so 45 degrees is down-right)."""
    rx = float(radius)
    ry = rx * float(y_squash)
    dx, dy = rx * SQRT1_2, ry * SQRT1_2
    return np.array([[0.0, 0.0], [rx, 0.0], [dx, dy], [0.0, ry], [-dx, dy],
                     [-rx, 0.0], [-dx, -dy], [0.0, -ry], [dx, -dy]], np.float64)


def pnpoly(xs: Any, ys: Any, polygon: np.ndarray) -> np.ndarray:
    """Even-odd point-in-polygon (W. R. Franklin's crossing test), broadcasting xs against ys.

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


class _Polygon:
    """A polygon blocker or walk-area boundary, tested with pnpoly()."""

    def __init__(self, points: Any) -> None:
        self.points = np.asarray(points, np.float64)
        self.edges = _polygon_edges(self.points)
        lo, hi = self.points.min(axis=0), self.points.max(axis=0)
        self.bounds = (lo[0] - _PAD, lo[1] - _PAD, hi[0] + _PAD, hi[1] + _PAD)

    def contains(self, xs: Any, ys: Any) -> np.ndarray:
        return pnpoly(xs, ys, self.points)

    def h_breaks(self, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(line index, x) where pnpoly can change along the horizontal lines y = ys."""
        lines, values = [], []
        for ax, ay, bx, by in self.edges:
            if ay == by:
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
                continue
            crossing = np.flatnonzero((ax > xs) != (bx > xs))
            if crossing.size:
                lines.append(crossing)
                values.append((by - ay) * (xs[crossing] - ax) / (bx - ax) + ay)
        return _joined(lines, values)

    def seg_breaks(self, px: float, py: float, dx: float, dy: float) -> np.ndarray:
        return _segment_edge_params(px, py, dx, dy, self.edges)


class _Region:
    """A walk region: inside its polygon and outside all of its holes."""

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
    """A closed axis-aligned rectangle [x, x + w] x [y, y + h]."""

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


def _joined(lines: Iterable[np.ndarray], values: Iterable[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    lines, values = list(lines), list(values)
    if not lines:
        return np.empty(0, np.int64), np.empty(0, np.float64)
    return np.concatenate(lines).astype(np.int64), np.concatenate(values).astype(np.float64)


def _shape(solid: dict) -> _Rect | _Ellipse | _Polygon:
    if solid["shape"] == "rect":
        return _Rect(solid["x"], solid["y"], solid["w"], solid["h"])
    if solid["shape"] == "ellipse":
        return _Ellipse(solid["cx"], solid["cy"], solid["rx"], solid["ry"], solid.get("rotate", 0))
    return _Polygon(solid["points"])


class _Materials:
    """Material-map classes (FREE, BLOCK, ONE_WAY) on squares of ``scale`` world pixels; no
    material (FREE) outside the image. Pixel (mx, my) covers [mx * s, (mx + 1) * s) x ..."""

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
    """Plan Appendix C validity, segmentClear and the thin-gap rule for one map."""

    def __init__(self, width: float, height: float, radius: float, y_squash: float = 1.0,
                 regions: Sequence[tuple[np.ndarray, Sequence[np.ndarray]]] = (),
                 solids: Sequence[dict] = (), material_codes: np.ndarray | None = None,
                 material_scale: int = 1) -> None:
        self.width, self.height = float(width), float(height)
        self.radius, self.y_squash = float(radius), float(y_squash)
        self.cell = nav_cell(self.radius)
        self.offsets = footprint_offsets(self.radius, self.y_squash)
        self.regions = [_Region(np.asarray(polygon, np.float64), [np.asarray(h, np.float64) for h in holes])
                        for polygon, holes in regions]
        self.solids = [_shape(solid) for solid in solids]
        self.solid_sources = [solid.get("source", "") for solid in solids]
        self.materials = None if material_codes is None else _Materials(material_codes, material_scale)
        self.has_one_way = self.materials is not None and bool((self.materials.codes == ONE_WAY).any())

    @classmethod
    def from_bundle(cls, bundle: map_bundle.Bundle) -> "CollisionModel":
        if bundle.collision is None:
            raise BundleError("the bundle has no collision block (map_nav needs collision.actorRadius)")
        material = bundle.material
        return cls(bundle.width, bundle.height, bundle.collision.actor_radius, bundle.collision.y_squash,
                   bundle.collision.regions, map_bundle.world_solids(bundle),
                   None if material is None else material.class_codes(), 1 if material is None else material.scale)

    # -- point predicates

    def area_ok(self, xs: Any, ys: Any) -> np.ndarray:
        xs, ys = np.broadcast_arrays(np.asarray(xs, np.float64), np.asarray(ys, np.float64))
        if not self.regions:
            return (xs >= 0) & (xs <= self.width) & (ys >= 0) & (ys <= self.height)
        return self._any(self.regions, xs, ys)

    def blocked(self, xs: Any, ys: Any) -> np.ndarray:
        """True where a point lies on a blocker (a solid or a blocking material)."""
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
        """Plan Appendix C validity of actor positions (all 9 footprint samples)."""
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
        ok = np.ones((ys.size, xs.size), bool)
        for ox, oy in self.offsets:
            ok &= self._lattice_centre_ok(xs + ox, ys + oy)
        return ok

    def one_way_lattice(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        return self.one_way_bits(xs[None, :], ys[:, None])

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
        """None when the actor can move straight from a to b (segmentClear); else the reason.

        thin_gap=False tests only what plan Appendix C spells out (footprint samples every
        cell / 2 and the one_way direction of those samples), for runtime parity checks;
        map_nav itself always applies the thin-gap rule.
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
    """Nodes at cell centres; moves holds the open 4-neighbour moves of every node."""
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
    """Rasterise a collision model onto its navigation grid (plan Appendix C)."""
    cell = model.cell
    cols = max(1, math.ceil(model.width / cell))
    rows = max(1, math.ceil(model.height / cell))
    if cols * rows > MAX_GRID_NODES:
        raise BundleError(f"the navigation grid would have {cols * rows} nodes (cell {cell} px for actorRadius "
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
    """Breadth-first distances (in moves) on a grid, -1 where unreachable.

    passable: (rows, cols) bool. starts: (row, col) pairs; impassable or out-of-grid starts
    are ignored. moves: optional (rows, cols) uint8 of open moves per cell (MOVE_E = +x,
    MOVE_S = +y, MOVE_W, MOVE_N; directed, so one-way steps are expressible); without it every
    step between passable 4-neighbours is open. A step must land on a passable cell. This is
    the reachability every generate2dmap validator should share (numpy only, deterministic).
    """
    passable = np.asarray(passable, bool)
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


# --------------------------------------------------------------------------- map checks

@dataclass
class Target:
    kind: str  # spawn, arrival, interaction, slot, approach, exit
    id: str
    point: tuple[float, float]
    reachable: bool = False
    steps: int | None = None
    node: tuple[float, float] | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "point": [self.point[0], self.point[1]],
                "reachable": self.reachable, "steps": self.steps,
                "node": None if self.node is None else [self.node[0], self.node[1]], "reason": self.reason}


@dataclass
class NavResult:
    bundle: map_bundle.Bundle
    model: CollisionModel
    grid: NavGrid
    distance: np.ndarray  # grid_bfs distances from every start
    starts: list[Target]
    targets: list[Target]
    problems: list[map_bundle.Problem]
    checks: list[dict]
    links: list[dict]
    pockets: list[dict]
    rects: list[tuple[int, int, int, int]]  # merge_rects of the blocked nodes, in cells

    @property
    def status(self) -> str:
        statuses = {check["status"] for check in self.checks}
        return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"


def attach(model: CollisionModel, grid: NavGrid, point: tuple[float, float]) -> list[tuple[int, int]]:
    """Valid nodes within two cells of a valid point that the actor reaches in a straight line,
    nearest first (how spawns, arrivals and point targets join the grid)."""
    if not model.valid(point[0], point[1]):
        return []
    row, col = grid.node_of(*point)
    found = []
    for r in range(max(0, row - 2), min(grid.rows, row + 3)):
        for c in range(max(0, col - 2), min(grid.cols, col + 3)):
            if grid.valid[r, c]:
                found.append(((grid.xs[c] - point[0]) ** 2 + (grid.ys[r] - point[1]) ** 2, r, c))
    return [(r, c) for _, r, c in sorted(found) if model.segment_clear(point, (grid.xs[c], grid.ys[r]))]


def _best(grid: NavGrid, distance: np.ndarray, nodes: Iterable[tuple[int, int]]) -> tuple[int, int] | None:
    reached = [(int(distance[r, c]), r, c) for r, c in nodes if distance[r, c] >= 0]
    if not reached:
        return None
    _, r, c = min(reached)
    return r, c


def _mark(target: Target, grid: NavGrid, distance: np.ndarray, node: tuple[int, int] | None, reason: str) -> None:
    if node is None:
        target.reason = reason
        return
    r, c = node
    target.reachable, target.steps, target.node = True, int(distance[r, c]), (float(grid.xs[c]), float(grid.ys[r]))


def _nodes_within(grid: NavGrid, x0: float, y0: float, x1: float, y1: float) -> tuple[np.ndarray, np.ndarray]:
    c0, c1 = max(0, math.floor(x0 / grid.cell) - 1), min(grid.cols, math.floor(x1 / grid.cell) + 2)
    r0, r1 = max(0, math.floor(y0 / grid.cell) - 1), min(grid.rows, math.floor(y1 / grid.cell) + 2)
    rr, cc = np.mgrid[r0:r1, c0:c1]
    return rr.ravel(), cc.ravel()


def check_point_target(model: CollisionModel, grid: NavGrid, distance: np.ndarray, target: Target) -> None:
    if not model.valid(*target.point):
        target.reason = "the actor cannot stand here (footprint blocked)"
        return
    nodes = attach(model, grid, target.point)
    _mark(target, grid, distance, _best(grid, distance, nodes),
          "no reachable node within two cells in a clear straight line" if nodes else
          "valid but off the grid (no valid node within two cells in a clear straight line)")


def check_reach_target(grid: NavGrid, distance: np.ndarray, target: Target, reach: float) -> None:
    x, y = target.point
    rr, cc = _nodes_within(grid, x - reach, y - reach, x + reach, y + reach)
    dx, dy = grid.xs[cc] - x, grid.ys[rr] - y
    close = dx * dx + dy * dy <= reach * reach
    _mark(target, grid, distance, _best(grid, distance, zip(rr[close], cc[close])),
          f"no reachable node within reach {reach:g} px")


def check_exit(model: CollisionModel, grid: NavGrid, distance: np.ndarray, portal: map_bundle.Portal,
               target: Target) -> None:
    x0, y0, x1, y1 = portal.bounds()
    margin = portal.radius if portal.activation == "intent" else 2 * grid.cell
    rr, cc = _nodes_within(grid, x0 - margin, y0 - margin, x1 + margin, y1 + margin)
    gap = portal.distance(grid.xs[cc], grid.ys[rr])
    reached = distance[rr, cc] >= 0
    if portal.activation == "intent":
        _mark(target, grid, distance, _best(grid, distance, zip(rr[gap <= portal.radius], cc[gap <= portal.radius])),
              f"no reachable node within the activation radius {portal.radius:g} px")
        return
    inside = reached & (gap == 0)
    if inside.any():
        _mark(target, grid, distance, _best(grid, distance, zip(rr[inside], cc[inside])), "")
        return
    for k in np.argsort(gap, kind="stable"):
        if reached[k] and gap[k] <= margin:
            node = (float(grid.xs[cc[k]]), float(grid.ys[rr[k]]))
            closest = portal.closest_point(*node)
            if model.valid(*closest) and model.segment_clear(node, closest):
                _mark(target, grid, distance, (int(rr[k]), int(cc[k])), "")
                target.node = closest
                return
    target.reason = "the actor's centre cannot enter the trigger from any reachable node"


def _portal_inside(bundle: map_bundle.Bundle, portal: map_bundle.Portal) -> bool:
    x0, y0, x1, y1 = portal.bounds()
    return x0 >= 0 and y0 >= 0 and x1 <= bundle.width and y1 <= bundle.height


def _arrival_problems(bundle: map_bundle.Bundle, model: CollisionModel, grid: NavGrid, point: tuple[float, float],
                      label: str) -> list[str]:
    """Why an arrival point is unusable: inside a trigger (bounce-back), blocked, or off the grid."""
    problems = [f"{label} lies inside the trigger of portal {other.id!r} (bounce-back)"
                for other in bundle.portals if other.distance(*point) == 0]
    if not model.valid(*point):
        problems.append(f"{label} is not a valid actor position")
    elif not attach(model, grid, point):
        problems.append(f"{label} cannot reach any grid node")
    return problems


def check_bundle(bundle: map_bundle.Bundle, links: Sequence[map_bundle.Bundle] = ()) -> NavResult:
    """Build the grid and run every reachability, portal and link check of one bundle."""
    model = CollisionModel.from_bundle(bundle)
    grid = build_grid(model)
    problems: list[map_bundle.Problem] = []

    def error(path: str, message: str) -> None:
        problems.append(map_bundle.Problem("error", path, message))

    def warn(path: str, message: str) -> None:
        problems.append(map_bundle.Problem("warning", path, message))

    # starts: spawns and portal arrivals given as points (arrivals named by spawn id are spawns)
    starts = [Target("spawn", name, point) for name, point in bundle.spawns.items()]
    for portal in bundle.portals:
        for source, point in portal.entrances.items():
            if portal.entrance_refs.get(source) is None:
                starts.append(Target("arrival", f"{portal.id}<-{source}", point))
    seeds: list[tuple[int, int]] = []
    for start in starts:
        nodes = attach(model, grid, start.point)
        if nodes:
            seeds.extend(nodes)
            start.reachable, start.steps = True, 0
            start.node = (float(grid.xs[nodes[0][1]]), float(grid.ys[nodes[0][0]]))
        else:
            start.reason = ("not a valid actor position" if not model.valid(*start.point)
                            else "cannot reach any grid node")
            error(f"{start.kind}:{start.id}", f"{start.kind} {start.id!r}: {start.reason}")
    if not starts:
        error("$.spawns", "no spawns or portal arrivals to start from")
    distance = grid_bfs(grid.valid, seeds, grid.moves)

    # targets: interactions (within reach), anchor slots and approach points (stand there), exits
    targets: list[Target] = []
    for entry in bundle.interactions:
        target = Target("interaction", entry["id"], (entry["x"], entry["y"]))
        if entry["reach"] is None:
            check_point_target(model, grid, distance, target)
        else:
            check_reach_target(grid, distance, target, entry["reach"])
        targets.append(target)
    for name, anchor in bundle.anchors.items():
        points = [("slot", f"{name}/slots[{k}]", p) for k, p in enumerate(anchor["slots"])]
        points += [("approach", f"{name}/approach[{k}]", p) for k, p in enumerate(anchor["approach"])]
        for kind, target_id, point in points:
            target = Target(kind, target_id, point)
            check_point_target(model, grid, distance, target)
            targets.append(target)
    for portal in bundle.portals:
        x0, y0, x1, y1 = portal.bounds()
        target = Target("exit", portal.id, ((x0 + x1) / 2, (y0 + y1) / 2))
        check_exit(model, grid, distance, portal, target)
        targets.append(target)
    for target in targets:
        if not target.reachable:
            error(f"{target.kind}:{target.id}", f"{target.kind} {target.id!r} is unreachable: {target.reason}")

    # portals: inside the world, arrivals outside every trigger, same-map targets
    outside = [p.id for p in bundle.portals if not _portal_inside(bundle, p)]
    for portal_id in outside:
        error(f"portal:{portal_id}", f"portal {portal_id!r} trigger extends outside the world")
    arrival_errors: list[str] = []
    for portal in bundle.portals:
        for source, point in portal.entrances.items():
            label = f"arrival from {source!r} at portal {portal.id!r}"
            arrival_errors += _arrival_problems(bundle, model, grid, point, label)
        if portal.to_map == bundle.id and portal.to_target:
            arrival_errors += _arrival_problems(bundle, model, grid, bundle.point_of(portal.to_target),
                                                f"same-map arrival {portal.to_target!r} of portal {portal.id!r}")
    for message in arrival_errors:
        error("$.portals", message)

    # reciprocity: locally (an arrival for travellers coming back) and across --link bundles
    link_rows, reciprocity_errors, skipped = check_links(bundle, links)
    for portal in bundle.portals:
        if portal.to_map != bundle.id and portal.to_map not in portal.entrances and portal.reciprocal:
            warn(f"portal:{portal.id}", f"portal {portal.id!r} has no entranceByFrom[{portal.to_map!r}]: travellers "
                                        "returning from that map arrive at its default spawn")
    for message in reciprocity_errors:
        error("$.portals", message)

    blocked_cells = ~grid.valid
    rects = merge_rects(blocked_cells)
    union = np.zeros_like(blocked_cells)
    for x, y, w, h in rects:
        union[y:y + h, x:x + w] = True
    pockets = _pockets(grid, distance)
    footprints = sum(1 for source in model.solid_sources if source.startswith("object:"))

    def status(failed: bool, warned: bool = False) -> str:
        return "fail" if failed else "warn" if warned else "pass"

    unreachable = [t.id for t in targets if not t.reachable]
    checks = [
        {"id": "starts_valid", "status": status(any(not s.reachable for s in starts) or not starts),
         "value": {"starts": len(starts), "blocked": [s.id for s in starts if not s.reachable]},
         "threshold": {"blocked": 0}},
        {"id": "targets_reachable", "status": status(bool(unreachable)),
         "value": {"reachable": len(targets) - len(unreachable), "total": len(targets), "unreachable": unreachable},
         "threshold": {"unreachable": 0}},
        {"id": "portals_inside_world", "status": status(bool(outside)), "value": {"outside": outside},
         "threshold": {"outside": 0}},
        {"id": "arrivals_outside_triggers", "status": status(bool(arrival_errors)),
         "value": {"problems": len(arrival_errors)}, "threshold": {"problems": 0}},
        {"id": "reciprocal_links", "status": "fail" if reciprocity_errors else "skipped" if skipped and not link_rows
         else "pass", "value": {"links": link_rows, "notChecked": skipped}, "threshold": {"errors": 0}},
        {"id": "thin_gaps", "status": status(False, bool(grid.thin_gaps)), "value": len(grid.thin_gaps),
         "threshold": "reported: moves the thin-gap rule closed although every footprint sample was valid"},
        {"id": "unreachable_walkable_area", "status": status(False, bool(pockets)),
         "value": {"pockets": len(pockets), "cells": int(sum(p["cells"] for p in pockets))}, "threshold": "reported"},
        {"id": "blocked_rects_union", "status": status(not np.array_equal(union, blocked_cells)),
         "value": {"rects": len(rects), "blockedCells": int(blocked_cells.sum())},
         "threshold": "union == blocked cells"},
        {"id": "footprints_scaled_once", "status": "pass", "value": {"objectSolids": footprints},
         "threshold": "object footprint x scale, never inflated by the actor radius"},
    ]
    return NavResult(bundle, model, grid, distance, starts, targets, problems, checks, link_rows, pockets, rects)


def _pockets(grid: NavGrid, distance: np.ndarray) -> list[dict]:
    """Walkable cells no start reaches, grouped 4-connected (largest first, at most 10)."""
    lonely = grid.valid & (distance < 0)
    if not lonely.any():
        return []
    labels, count = forge_core.label_components(lonely, connectivity=4)
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    pockets = []
    for label in np.argsort(-sizes[1:], kind="stable")[:10] + 1:
        r, c = np.argwhere(labels == label)[0]
        pockets.append({"cells": int(sizes[label]), "at": [float(grid.xs[c]), float(grid.ys[r])]})
    return pockets


def check_links(bundle: map_bundle.Bundle,
                links: Sequence[map_bundle.Bundle]) -> tuple[list[dict], list[str], list[str]]:
    """Portal links to the --link bundles: the destination arrival exists, lies outside every
    trigger there and can reach the grid, and the destination has a portal back (unless the
    portal says reciprocal: false). Returns (rows, errors, destinations not provided)."""
    by_id = {other.id: other for other in links}
    rows: list[dict] = []
    errors: list[str] = []
    skipped = sorted({p.to_map for p in bundle.portals if p.to_map != bundle.id and p.to_map not in by_id})
    models: dict[str, tuple[CollisionModel, NavGrid]] = {}
    for portal in bundle.portals:
        other = by_id.get(portal.to_map)
        if other is None or other is bundle:
            continue
        row = {"portal": portal.id, "to": portal.to_map, "arrival": None, "returnPortals": [], "status": "pass"}
        problems: list[str] = []
        if portal.to_target:
            arrival = other.point_of(portal.to_target)
            if arrival is None:
                problems.append(f"{portal.to_map!r} has no spawn or anchor {portal.to_target!r}")
        else:
            arrivals = sorted((q.id, q.entrances[bundle.id]) for q in other.portals if bundle.id in q.entrances)
            arrival = arrivals[0][1] if arrivals else None
            if arrival is None:
                problems.append(f"{portal.to_map!r} names no arrival for travellers from {bundle.id!r}")
        back = sorted(q.id for q in other.portals if q.to_map == bundle.id)
        row["returnPortals"] = back
        if not back and portal.reciprocal:
            problems.append(f"{portal.to_map!r} has no portal back to {bundle.id!r} "
                            "(set reciprocal: false for a one-way exit)")
        if arrival is not None:
            row["arrival"] = [arrival[0], arrival[1]]
            if other.collision is None:
                problems.append(f"{portal.to_map!r} has no collision block")
            else:
                if other.id not in models:
                    other_model = CollisionModel.from_bundle(other)
                    models[other.id] = (other_model, build_grid(other_model))
                problems += _arrival_problems(other, *models[other.id], arrival, f"arrival in {portal.to_map!r}")
        if problems:
            row["status"] = "fail"
            errors += [f"portal {portal.id!r} -> {portal.to_map!r}: {message}" for message in problems]
        rows.append(row)
    return rows, errors, skipped


# --------------------------------------------------------------------------- outputs

def grid_document(result: NavResult, base: Path) -> dict:
    grid = result.grid
    hexes = _HEX[grid.moves & 15]
    text = np.where(grid.valid, hexes, "#")
    reachable = np.where(result.distance >= 0, "1", "0")
    return {
        "schema": GRID_SCHEMA,
        "map": result.bundle.id,
        "bundle": map_bundle._local_file_ref(result.bundle.path, base),
        "world": {"width": result.bundle.width, "height": result.bundle.height},
        "actor": {"radius": result.model.radius, "ySquash": result.model.y_squash,
                  "samples": result.model.offsets.tolist()},
        "cell": grid.cell,
        "cols": grid.cols,
        "rows": grid.rows,
        "nodeCentre": "x = (col + 0.5) * cell, y = (row + 0.5) * cell",
        "moveBits": {"E": MOVE_E, "S": MOVE_S, "W": MOVE_W, "N": MOVE_N},
        "moves": ["".join(row) for row in text],
        "reachable": ["".join(row) for row in reachable],
        "blockedRects": [[x * grid.cell, y * grid.cell, w * grid.cell, h * grid.cell] for x, y, w, h in result.rects],
    }


def report_document(result: NavResult, base: Path, outputs: list[Path]) -> dict:
    bundle = result.bundle
    return {
        "schema": REPORT_SCHEMA,
        "status": result.status,
        "method": ("plan Appendix C validity (centre + 8 footprint samples against walk regions, solids, object "
                   "footprints scaled once, tile collision and material classes) on a grid of cell "
                   "max(1, round(r/2)) px; 4-neighbour moves open when segmentClear holds between node centres "
                   "(samples every cell/2, plus the exact thin-gap centre path and one_way direction); BFS from "
                   "every spawn and portal arrival to every interaction, exit, anchor slot and approach point"),
        "notProven": [
            "that the collision data matches the painted art",
            "positions closer than one grid cell to a blocker (the grid samples cell centres)",
            "runtime movement code: map-runtime.mjs parity is tested during integration",
            "links to maps not passed with --link",
        ],
        "checks": result.checks,
        "inputs": map_bundle.bundle_inputs(bundle, base),
        "outputs": [map_bundle._local_file_ref(path, base) for path in outputs],
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "map": bundle.id,
        "cell": result.grid.cell,
        "starts": [start.as_dict() for start in result.starts],
        "targets": [target.as_dict() for target in result.targets],
        "links": result.links,
        "thinGaps": [list(edge) for edge in result.grid.thin_gaps],
        "unreachablePockets": result.pockets,
        "bundleNav": {"cell": result.grid.cell, "grid": "nav-grid.json"},
        "problems": [problem.as_dict() for problem in bundle.warnings + result.problems],
    }


def debug_image(result: NavResult, scale: int = 1) -> Image.Image:
    """World-sized picture of the checks: art dimmed, cells tinted (green reachable, yellow
    walkable but unreachable, red blocked), blockers outlined white, walk regions cyan,
    triggers blue, starts white, targets green (reachable) or a magenta cross, thin gaps orange."""
    bundle, grid, model = result.bundle, result.grid, result.model
    size = map_bundle.canvas_size(bundle)
    canvas = Image.new("RGBA", size, (24, 24, 28, 255))
    try:
        art = Image.fromarray(map_bundle.render_map(bundle))
        art.putalpha(art.getchannel("A").point(lambda a: a // 2))
        canvas.alpha_composite(art)
    except (OSError, ValueError, KeyError):
        pass
    tint = np.zeros((grid.rows, grid.cols, 4), np.uint8)
    tint[~grid.valid] = (220, 40, 40, 110)
    tint[grid.valid & (result.distance >= 0)] = (40, 200, 90, 70)
    tint[grid.valid & (result.distance < 0)] = (240, 200, 40, 120)
    cells = Image.fromarray(tint).resize((grid.cols * grid.cell, grid.rows * grid.cell), Image.Resampling.NEAREST)
    canvas.alpha_composite(cells.crop((0, 0) + size))
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for region in model.regions:
        for part in region._parts():
            draw.polygon([tuple(p) for p in part.points], outline=(80, 220, 255, 255))
    for shape in model.solids:
        if isinstance(shape, _Rect):
            draw.rectangle((shape.x0, shape.y0, shape.x1, shape.y1), outline=(255, 255, 255, 220))
        elif isinstance(shape, _Ellipse) and shape.sin == 0.0:
            draw.ellipse((shape.cx - shape.rx, shape.cy - shape.ry, shape.cx + shape.rx, shape.cy + shape.ry),
                         outline=(255, 255, 255, 220))
        else:
            points = shape.points if isinstance(shape, _Polygon) else _ellipse_outline(shape)
            draw.polygon([tuple(p) for p in points], outline=(255, 255, 255, 220))
    for portal in bundle.portals:
        x0, y0, x1, y1 = portal.bounds()
        box = draw.rectangle if portal.rect is not None else draw.ellipse
        box((x0, y0, x1, y1), outline=(70, 120, 255, 255))
        if portal.activation == "intent" and portal.radius:
            box((x0 - portal.radius, y0 - portal.radius, x1 + portal.radius, y1 + portal.radius),
                outline=(150, 190, 255, 200))
    for x0, y0, x1, y1 in grid.thin_gaps:
        draw.line((x0, y0, x1, y1), fill=(255, 150, 30, 255))
    rx, ry = model.radius, model.radius * model.y_squash
    for start in result.starts:
        x, y = start.point
        draw.ellipse((x - rx, y - ry, x + rx, y + ry), outline=(255, 255, 255, 255))
    for target in result.targets:
        x, y = target.point
        if target.reachable:
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), outline=(60, 255, 120, 255))
        else:
            draw.line((x - 4, y - 4, x + 4, y + 4), fill=(255, 40, 220, 255))
            draw.line((x - 4, y + 4, x + 4, y - 4), fill=(255, 40, 220, 255))
    canvas.alpha_composite(overlay)
    if scale > 1:
        canvas = canvas.resize((size[0] * scale, size[1] * scale), Image.Resampling.NEAREST)
    return canvas


def _ellipse_outline(shape: _Ellipse, steps: int = 32) -> np.ndarray:
    angles = np.linspace(0, 2 * math.pi, steps, endpoint=False)
    u, v = shape.rx * np.cos(angles), shape.ry * np.sin(angles)
    return np.stack([shape.cx + u * shape.cos - v * shape.sin, shape.cy + u * shape.sin + v * shape.cos], axis=1)


# --------------------------------------------------------------------------- CLI

class QAFailed(Exception):
    """The navigation check failed; nothing is published."""


def _load_checked(path: Path) -> map_bundle.Bundle:
    bundle = map_bundle.load_bundle(path)
    if bundle.errors or not bundle.readable:
        map_bundle.print_problems(bundle.errors)
        raise BundleError(f"{path.name} does not validate; run map_bundle.py validate first (nothing was written)")
    return bundle


def cmd_check(args: argparse.Namespace) -> int:
    final = args.output_dir.resolve()
    if final.exists() or final.is_symlink():
        raise BundleError(f"refusing to replace existing output: {final}")
    bundle = _load_checked(args.bundle)
    links = [_load_checked(path) for path in args.link]
    ids = [bundle.id] + [other.id for other in links]
    if len(set(ids)) != len(ids):
        raise BundleError(f"map ids must be unique across --bundle and --link: {ids}")
    result = check_bundle(bundle, links)
    failed = result.status == "fail"
    try:
        with forge_core.staged_output(final) as stage:
            grid_path, debug_path = stage / "nav-grid.json", stage / "nav-debug.png"
            report_path = stage / "nav-report.json"
            forge_core.write_json(grid_path, grid_document(result, stage))
            forge_core.save_png(debug_image(result, args.debug_scale), debug_path)
            forge_core.write_json(report_path, report_document(result, stage, [grid_path, debug_path]))
            if failed and not args.publish_on_fail:
                raise QAFailed()
    except QAFailed:
        map_bundle.print_problems([p for p in result.problems if p.severity == "error"])
        print("error: navigation check failed; nothing was published (rerun with --publish-on-fail to inspect "
              "nav-debug.png)", file=sys.stderr)
        return 1
    if failed:
        map_bundle.print_problems([p for p in result.problems if p.severity == "error"])
        print(forge_core.ascii_text(f"error: navigation check failed; outputs published for inspection in {final}"),
              file=sys.stderr)
    summary = {"status": result.status, "output": str(final), "metadata": str(final / "nav-report.json"),
               "grid": str(final / "nav-grid.json"), "debug": str(final / "nav-debug.png"),
               "map": forge_core.ascii_text(bundle.id), "cell": result.grid.cell,
               "targets": len(result.targets), "unreachable": sum(not t.reachable for t in result.targets),
               "thinGaps": len(result.grid.thin_gaps)}
    print(json.dumps(summary))
    return 1 if failed else 0


def _pairs(text: str, count: int) -> tuple[float, ...]:
    try:
        values = tuple(float(v) for v in text.split(","))
    except ValueError:
        values = ()
    if len(values) != count or not all(math.isfinite(v) for v in values):
        raise argparse.ArgumentTypeError(f"expected {count} comma-separated numbers, got {text!r}")
    return values


def cmd_query(args: argparse.Namespace) -> int:
    bundle = _load_checked(args.bundle)
    model = CollisionModel.from_bundle(bundle)
    points = [{"point": list(p), "valid": bool(model.valid(*p))} for p in args.point]
    segments = []
    for x0, y0, x1, y1 in args.segment:
        reason = model.segment_status((x0, y0), (x1, y1), thin_gap=not args.sampled_only)
        segments.append({"from": [x0, y0], "to": [x1, y1], "clear": reason is None, "reason": reason})
    print(json.dumps({"map": forge_core.ascii_text(bundle.id), "cell": model.cell, "points": points,
                      "segments": segments}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="map_nav.py",
        description="Collision and navigation from map-bundle data (plan Appendix C): reachability, portal and "
                    "anchor checks, nav-grid.json and a debug PNG.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    check = verbs.add_parser("check", help="build the nav grid and prove every target reachable",
                             description="Build the navigation grid, run reachability, portal and link checks and "
                                         "publish nav-grid.json, nav-report.json and nav-debug.png into a new folder. "
                                         "Exit 1 when a check fails (nothing is published unless --publish-on-fail).")
    check.add_argument("--bundle", required=True, type=Path, help="map bundle JSON")
    check.add_argument("--output-dir", required=True, type=Path, help="new folder for the outputs (must not exist)")
    check.add_argument("--link", action="append", default=[], type=Path,
                       help="another map's bundle, to check portal links and arrivals in both directions (repeatable)")
    check.add_argument("--publish-on-fail", action="store_true",
                       help="publish the outputs even when a check fails (still exits 1)")
    check.add_argument("--debug-scale", type=int, default=1, choices=range(1, 9), metavar="N",
                       help="integer upscale of nav-debug.png (1-8, default 1)")
    check.set_defaults(func=cmd_check)
    query = verbs.add_parser("query", help="test points and segments against the collision model",
                             description="Print whether points are valid actor positions and segments are clear.")
    query.add_argument("--bundle", required=True, type=Path, help="map bundle JSON")
    query.add_argument("--point", action="append", default=[], type=lambda t: _pairs(t, 2), metavar="X,Y")
    query.add_argument("--segment", action="append", default=[], type=lambda t: _pairs(t, 4), metavar="X0,Y0,X1,Y1")
    query.add_argument("--sampled-only", action="store_true",
                       help="test segments with the plan Appendix C samples only, without the thin-gap rule "
                            "(for comparing with a runtime that does not implement it)")
    query.set_defaults(func=cmd_query)
    return parser


def main(argv: list[str] | None = None) -> int:
    return map_bundle._local_run_cli(build_parser(), argv)


if __name__ == "__main__":
    raise SystemExit(main())
