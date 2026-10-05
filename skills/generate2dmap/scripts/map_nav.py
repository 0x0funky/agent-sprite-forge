#!/usr/bin/env python3
"""Collision and navigation from map-bundle data, with reachability, portal and anchor checks.

map_nav.py is the command line over the vendored forge_nav.py (integration decision D4).
The blocking set (D2), validity, segmentClear with the one_way and thin-gap rules, the
navigation grid, the BFS and the target checks are forge_nav's rule book, rules N1-N15 of
its docstring; references/layered-map-contract.md quotes them. This file adds what is
about a bundle: the starts and targets it names, portal triggers and arrivals, the links
between maps (--link) and the published outputs.

Verbs:
  check  build the navigation grid of a map bundle and prove that every interaction, exit,
         anchor slot and approach point can be reached from the spawns and arrivals; check
         portal triggers and arrivals (and, with --link, the links between maps). Publishes
         nav-grid.json, nav-report.json and nav-debug.png into a new folder; a failed check
         publishes nothing unless --publish-on-fail.
  query  print whether points are valid actor positions and whether segments are clear.

Collision semantics (forge_nav rules N1-N15; map-runtime.mjs implements the same rules):
  * World pixels, y down. The actor footprint is an ellipse rx = r, ry = r * ySquash.
  * A point P is valid when P and 8 samples on the footprint ellipse (0, 45, ..., 315
    degrees; the diagonals use Math.SQRT1_2) all lie in the walk area (inside a walk region
    polygon and outside its holes, even-odd test; without regions the closed world box) and
    off every blocker. Blockers are closed sets (D1): collision.solids, collision.rects,
    solid object footprints scaled once (basis world_px never, D7; mirrored with flip_x,
    D6), tile collision, and material-map pixels of class solid (or liquid/hazard unless
    walkable). Shapes without area block nothing.
  * The grid cell is max(1, round(r / 2)) px (half up); node (col, row) sits at
    ((col + 0.5) * cell, (row + 0.5) * cell). A 4-neighbour move between two valid nodes is
    open when segmentClear holds between them.
  * segmentClear(a, b): every sample a + (b - a) * k / n, n = ceil(|b - a| / (cell / 2)),
    is valid, and (thin-gap rule) the actor's centre stays in the walk area and outside
    every blocker along the whole segment, tested exactly between boundary crossings, so a
    gap or wall thinner than the sample spacing is never jumped.
  * one_way material blocks from above only: a move with a downward (+y) component may not
    bring a footprint sample, nor the centre path, from another material onto one_way.

Library users take forge_nav directly. map_nav.CollisionModel is B13's model as built before
integration, kept only as the reference tests/test_forge_nav.py pins D1's differences
against; the verbs never use it.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_nav  # noqa: E402  (the shared collision rule book, D4)
import map_bundle  # noqa: E402
from forge_nav import (  # noqa: E402,F401  (map_nav's public names, now forge_nav's)
    BLOCK, FREE, MAX_GRID_NODES, MOVE_E, MOVE_N, MOVE_S, MOVE_W, ONE_WAY, SQRT1_2, NavGrid, Navigation, Reach,
    Trigger, attach, footprint_offsets, grid_bfs, merge_rects, moves_from_mask, nav_cell, pnpoly, reachable_mask,
)
from map_bundle import BundleError  # noqa: E402

TOOL_NAME = "map_nav.py"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # D29: QA envelopes carry the package version
GRID_SCHEMA = "generate2dmap.nav_grid.v1"
REPORT_SCHEMA = "generate2dmap.nav_report.v1"
_HEX = np.array(list("0123456789abcdef"))

__all__ = [
    "CollisionModel", "NavGrid", "build_grid", "grid_bfs", "reachable_mask", "moves_from_mask",
    "merge_rects", "nav_cell", "footprint_offsets", "pnpoly", "check_bundle", "collision_model", "MOVE_E", "MOVE_S",
    "MOVE_W", "MOVE_N",
]


# --------------------------------------------------------------------------- the collision model

def collision_model(bundle: map_bundle.Bundle) -> forge_nav.CollisionModel:
    """The forge_nav collision model of a readable bundle: its D2 blocking set (D4)."""
    if bundle.collision is None:
        raise BundleError("the bundle has no collision block (map_nav needs collision.actorRadius)")
    return map_bundle.blocking_set(bundle).model()


def build_grid(model: forge_nav.CollisionModel) -> NavGrid:
    """forge_nav.build_grid (rules N12, N13); an oversized grid raises BundleError, as it always did."""
    try:
        return forge_nav.build_grid(model)
    except forge_nav.NavError as error:
        raise BundleError(str(error)) from None


class CollisionModel(forge_nav.CollisionModel):
    """B13's map_nav model as built before integration, kept only as the reference that
    tests/test_forge_nav.py pins forge_nav's model differences against (D1): polygon solids
    use the even-odd test alone (a point on a polygon solid's right or bottom edge was free)
    and shapes without area still block. Everything else is forge_nav's code. The verbs never
    use it; delete it once that test freezes its own reference copy (handoff B13, section 6)."""

    def __init__(self, width: float, height: float, radius: float, y_squash: float = 1.0,
                 regions: Sequence[tuple[Any, Sequence[Any]]] = (), solids: Sequence[dict] = (),
                 material_codes: np.ndarray | None = None, material_scale: int = 1) -> None:
        super().__init__(width, height, radius, y_squash, regions, (), material_codes, material_scale)
        self.solids = [forge_nav._Polygon(solid["points"]) if solid["shape"] == "polygon"  # open, even-odd only
                       else forge_nav._shape(solid) for solid in solids]
        self.solid_sources = [solid.get("source", "") for solid in solids]

    @classmethod
    def from_bundle(cls, bundle: map_bundle.Bundle) -> "CollisionModel":
        if bundle.collision is None:
            raise BundleError("the bundle has no collision block (map_nav needs collision.actorRadius)")
        material = bundle.material
        return cls(bundle.width, bundle.height, bundle.collision.actor_radius, bundle.collision.y_squash,
                   bundle.collision.regions, map_bundle.world_solids(bundle),
                   None if material is None else material.class_codes(), 1 if material is None else material.scale)


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

    def take(self, reach: Reach) -> "Target":
        """Copy a forge_nav answer (N14) into this target."""
        self.reachable, self.steps, self.node = reach.reachable, reach.steps, reach.node
        self.reason = None if reach.reachable else reach.reason
        return self


@dataclass
class NavResult:
    bundle: map_bundle.Bundle
    model: forge_nav.CollisionModel
    grid: NavGrid
    distance: np.ndarray  # grid_bfs distances from every start
    starts: list[Target]
    targets: list[Target]
    problems: list[map_bundle.Problem]
    checks: list[dict]
    links: list[dict]
    pockets: list[dict]
    rects: list[tuple[int, int, int, int]]  # merge_rects of the blocked nodes, in cells
    linked: list[map_bundle.Bundle] = field(default_factory=list)  # the --link bundles the checks read

    @property
    def status(self) -> str:
        statuses = {check["status"] for check in self.checks}
        return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"


def _navigation(model: forge_nav.CollisionModel, grid: NavGrid, distance: np.ndarray) -> Navigation:
    return Navigation(model, grid, distance, [], [])


def check_point_target(model: forge_nav.CollisionModel, grid: NavGrid, distance: np.ndarray, target: Target) -> None:
    """The actor must stand at the target: valid, and joined to a reached node (N14)."""
    target.take(_navigation(model, grid, distance).point_target(target.point))


def check_reach_target(grid: NavGrid, distance: np.ndarray, target: Target, reach: float) -> None:
    """A reached node centre within ``reach`` of the target (N14)."""
    target.take(_navigation(None, grid, distance).reach_target(target.point, reach))


def check_exit(model: forge_nav.CollisionModel, grid: NavGrid, distance: np.ndarray, portal: map_bundle.Portal,
               target: Target) -> None:
    """An exit's trigger reached for its activation (N14)."""
    trigger = Trigger(rect=portal.rect, circle=None) if portal.rect is not None else Trigger(circle=portal.circle)
    target.take(_navigation(model, grid, distance).exit_target(trigger, portal.activation, portal.radius))


def _portal_inside(bundle: map_bundle.Bundle, portal: map_bundle.Portal) -> bool:
    x0, y0, x1, y1 = portal.bounds()
    return x0 >= 0 and y0 >= 0 and x1 <= bundle.width and y1 <= bundle.height


def _arrival_problems(bundle: map_bundle.Bundle, model: forge_nav.CollisionModel, grid: NavGrid,
                      point: tuple[float, float], label: str) -> list[str]:
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
    model = collision_model(bundle)
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
    navigation = forge_nav.navigate(model, [start.point for start in starts], grid)
    for start, reach in zip(starts, navigation.starts):
        start.take(reach)
        if not reach.reachable:
            error(f"{start.kind}:{start.id}", f"{start.kind} {start.id!r}: {start.reason}")
    if not starts:
        error("$.spawns", "no spawns or portal arrivals to start from")
    distance = navigation.distance

    # targets: interactions (within reach), anchor slots and approach points (stand there), exits
    targets: list[Target] = []
    for entry in bundle.interactions:
        target = Target("interaction", entry["id"], (entry["x"], entry["y"]))
        if entry["reach"] is None:
            target.take(navigation.point_target(target.point))
        else:
            target.take(navigation.reach_target(target.point, entry["reach"]))
        targets.append(target)
    for name, anchor in bundle.anchors.items():
        points = [("slot", f"{name}/slots[{k}]", p) for k, p in enumerate(anchor["slots"])]
        points += [("approach", f"{name}/approach[{k}]", p) for k, p in enumerate(anchor["approach"])]
        for kind, target_id, point in points:
            targets.append(Target(kind, target_id, point).take(navigation.point_target(point)))
    for portal in bundle.portals:
        x0, y0, x1, y1 = portal.bounds()
        target = Target("exit", portal.id, ((x0 + x1) / 2, (y0 + y1) / 2))
        trigger = Trigger(rect=portal.rect) if portal.rect is not None else Trigger(circle=portal.circle)
        targets.append(target.take(navigation.exit_target(trigger, portal.activation, portal.radius)))
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
    return NavResult(bundle, model, grid, distance, starts, targets, problems, checks, link_rows, pockets, rects,
                     [other for other in links if other is not bundle])


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
    models: dict[str, tuple[forge_nav.CollisionModel, NavGrid]] = {}
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
                    other_model = collision_model(other)
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
        "bundle": forge_core.file_ref(result.bundle.path, base),
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
    inputs = map_bundle.bundle_inputs(bundle, base)
    for other in result.linked:  # the --link bundles the link checks read
        seen = {ref["path"] for ref in inputs}
        inputs += [ref for ref in map_bundle.bundle_inputs(other, base) if ref["path"] not in seen]
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
        "inputs": inputs,
        "outputs": [forge_core.file_ref(path, base) for path in outputs],
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
    blocking = map_bundle.blocking_set(bundle)
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
    for polygon, holes in blocking.regions:
        for part in (polygon, *holes):
            draw.polygon([tuple(p) for p in np.asarray(part, np.float64)], outline=(80, 220, 255, 255))
    for solid in blocking.solids:
        _draw_solid(draw, solid)
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


def _draw_solid(draw: ImageDraw.ImageDraw, solid: dict) -> None:
    """Outline one blocking shape (a forge_nav solid dict) in white; a rotated ellipse as a polygon."""
    outline = (255, 255, 255, 220)
    if solid["shape"] == "rect":
        x0, y0 = float(solid["x"]), float(solid["y"])
        draw.rectangle((x0, y0, x0 + float(solid["w"]), y0 + float(solid["h"])), outline=outline)
        return
    if solid["shape"] == "polygon":
        draw.polygon([tuple(p) for p in np.asarray(solid["points"], np.float64)], outline=outline)
        return
    cx, cy, rx, ry = (float(solid[key]) for key in ("cx", "cy", "rx", "ry"))
    rotate = solid.get("rotate", 0)
    theta = math.radians(float(rotate or 0.0))
    cos, sin = (1.0, 0.0) if not rotate else (math.cos(theta), math.sin(theta))
    if sin == 0.0:
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), outline=outline)
        return
    angles = np.linspace(0, 2 * math.pi, 32, endpoint=False)
    u, v = rx * np.cos(angles), ry * np.sin(angles)
    points = np.stack([cx + u * cos - v * sin, cy + u * sin + v * cos], axis=1)
    draw.polygon([tuple(p) for p in points], outline=outline)


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
    model = collision_model(bundle)
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
        description="Collision and navigation from map-bundle data (the forge_nav rule book, plan Appendix C): "
                    "reachability, portal and anchor checks, nav-grid.json and a debug PNG.")
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


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def main(argv: list[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27): usage errors exit 2, expected errors print
    'error: <message>' and exit 1, anything else 'error: internal error (<Type>: <message>)'."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
