"""map-runtime.mjs (B17-T1) under Node, plus a reference comparison of its collision query with forge_nav.

forge_nav (the generate2dmap vendored copy of shared/forge_nav.py) is the rule book N1-N15 that
map-runtime.mjs mirrors rule for rule (integration decisions D1, D2, D4). Every fixture below is read
by forge_nav as a map_bundle.v2 document and by the runtime as the same bundle; the two must agree on
every lattice point (centre test and 9-sample validity), on random segments (with and without the
thin-gap rule), on every grid node and 4-neighbour move, on BFS distances from the spawns and on the
N14 verdict of every target. Material maps are integer-scale with every pixel classified (D4). The
only exemption is a point within 1e-9 of a rotated shape's edge, where the platform's sin and cos
(V8 against the C runtime) may differ by one ulp. The full JS/Python parity suite is a later stage.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import REPO_ROOT, SKILLS_DIR, load_script, require_node, run_cli, script_path

RUNTIME = SKILLS_DIR / "generate2dmap" / "references" / "runtime" / "map-runtime.mjs"
NODE_SUITE = REPO_ROOT / "tests" / "js" / "map-runtime.test.mjs"
NAV = load_script("generate2dmap", "forge_nav")
BUNDLE = "generate2dmap.map_bundle.v2"
SLIVER_PX_JS = re.compile(r"const SLIVER_PX = ([0-9.e-]+);")


def run_node(code: str, payload) -> dict:
    node = require_node()
    completed = subprocess.run([node, "--input-type=module", "-e", f"import * as rt from {json.dumps(RUNTIME.as_uri())};\n"
                                + "import {readFileSync} from 'node:fs';\nconst job = JSON.parse(readFileSync(0, 'utf8'));\n"
                                + code], input=json.dumps(payload), capture_output=True, encoding="utf-8",
                               errors="replace", timeout=600, check=False)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------- fixtures

def walk_fixture(root: Path) -> tuple[dict, dict | None]:
    return {
        "schema": BUNDLE, "world": {"width": 96, "height": 64},
        "collision": {
            "actorRadius": 5, "ySquash": 0.58,
            "walkRegions": [
                {"polygon": [[2, 2], [70, 2], [70, 30], [40, 30], [40, 60], [2, 60]],
                 "holes": [[[10, 10], [24, 10], [24, 22], [10, 22]]]},
                {"polygon": [[30, 20], [94, 14.5], [90, 62], [36, 62]]}],
            "solids": [
                {"shape": "rect", "x": 50, "y": 40, "w": 8.5, "h": 6},
                {"shape": "ellipse", "cx": 20, "cy": 45, "rx": 7, "ry": 3.5},
                {"shape": "ellipse", "cx": 75, "cy": 30, "rx": 8, "ry": 3, "rotate": 30},
                {"shape": "polygon", "points": [[60, 50], [72, 52], [64, 58.25]]},
                {"shape": "rect", "x": 26, "y": 30, "w": 0.2, "h": 20}],
            "rects": [[80, 50, 6, 4], [44, 8, 0.3, 10]]},
        "spawns": [{"id": "west", "x": 8, "y": 40}, {"id": "east", "x": 80, "y": 24}],
        "interactions": [{"id": "sign", "x": 56, "y": 24, "reach": 8}, {"id": "post", "x": 33, "y": 50}],
        "anchors": {"well": {"point": [86, 40], "slots": [[84, 44]], "approach": [[78, 44], [96, 70]]}},
        "portals": [{"id": "gate", "rect": [88, 30, 6, 10], "to": "next", "activation": "intent", "travelDirection": [1, 0],
                     "radius": 6},
                    {"id": "hole", "circle": [16, 50, 3], "to": "cave"}],
    }, None


def props_fixture(root: Path) -> tuple[dict, dict | None]:
    def tree(ident, x, y, **extra):
        return {"id": ident, "prop": "tree", "x": x, "y": y, "anchor_px": [8, 30], **extra}
    return {
        "schema": BUNDLE, "world": {"width": 96, "height": 64},
        "collision": {"actorRadius": 4},
        "props": {"tree": {"image": "tree.png", "anchor_px": [8, 30],
                           "footprint": {"shape": "ellipse", "width": 6, "depth": 3, "offset": [2, -1]}},
                  "rock": {"image": "rock.png", "anchor_px": [4, 8], "solid": False,
                           "footprint": {"shape": "rect", "width": 6, "depth": 3}}},
        "objects": [
            tree("a", 20, 20, scale=1.5, footprint={"shape": "ellipse", "width": 10, "depth": 5, "offset": [1, -2]}),
            tree("b", 50, 20, footprint={"shape": "rect", "width": 9, "depth": 4.5, "offset": [0.5, -1]}, scale=1.25),
            tree("c", 75, 25, footprint={"shape": "rect", "width": 12, "depth": 4, "rotate": 30}),
            tree("d", 25, 48, scale=3, footprint={"shape": "ellipse", "width": 6, "depth": 3, "basis": "world_px"}),
            tree("e", 55, 48, footprint={"shape": "ellipse", "width": 10, "depth": 5}, solid=False),
            tree("f", 75, 50, footprint={"shape": "none"}),
            tree("g", 85, 10, footprint={"shape": "ellipse", "width": 7, "depth": 7, "rotate": 45}, scale=0.5),
            tree("h", 40, 40, flip_x=True, scale=2),
            tree("i", 64, 36, flip_x=True, footprint={"shape": "rect", "width": 8, "depth": 3, "offset": [3, 0],
                                                      "rotate": 20, "basis": "image_px"}),
            tree("j", 12, 56),
            {"id": "k", "prop": "rock", "x": 88, "y": 58, "anchor_px": [4, 8]},
            {"id": "l", "prop": "rock", "x": 30, "y": 8, "anchor_px": [4, 8], "solid": True}],
        "spawns": [{"id": "start", "x": 6, "y": 6}],
        "interactions": [{"id": "rock", "x": 30, "y": 14, "reach": 5}],
    }, None


def tangent_fixture(root: Path) -> tuple[dict, dict | None]:
    """Ellipses that grid moves only touch, the case the integration pass found on layout_build's meadow
    example (forge_nav N10: one touching point, one double root): one under row 19 (cell 3, touching
    (8, 58.5) between nodes x 7.5 and 10.5) and one right of column 10 (touching (31.5, 23) between nodes
    y 22.5 and 25.5); a third under row 15 is crossed by 1e-6 px."""
    return {
        "schema": BUNDLE, "world": {"width": 48, "height": 80},
        "collision": {"actorRadius": 5, "solids": [
            {"shape": "ellipse", "cx": 8.0, "cy": 60.25, "rx": 3.0, "ry": 1.75},
            {"shape": "ellipse", "cx": 33.25, "cy": 23.0, "rx": 1.75, "ry": 3.0},
            {"shape": "ellipse", "cx": 25.0, "cy": 48.249999, "rx": 3.0, "ry": 1.75}]},
        "spawns": [{"id": "start", "x": 4.5, "y": 4.5}],
        "interactions": [{"id": "far", "x": 43.5, "y": 76.5}],
    }, None


MATERIAL_CLASSES = {"grass": {"class": "decor", "color": "#40a040"}, "rock": {"class": "solid", "color": "#505050"},
                    "water": {"class": "liquid", "color": "#2050c0"},
                    "shallows": {"class": "liquid", "color": "#3070e0", "walkable": True},
                    "lava": {"class": "hazard", "color": "#e04020"},
                    "ledge": {"class": "one_way", "color": "#e0e000"}}
MATERIAL_SHARES = (0.6, 0.08, 0.06, 0.08, 0.04, 0.14)


def material_pixels(seed: int, size: tuple[int, int]) -> np.ndarray:
    """Every pixel classified (D4): an opaque RGBA image of the six material colours."""
    rng = np.random.default_rng(seed)
    names = list(MATERIAL_CLASSES)
    choice = rng.choice(len(names), size=(size[1], size[0]), p=MATERIAL_SHARES)
    palette = np.array([[int(MATERIAL_CLASSES[n]["color"][k:k + 2], 16) for k in (1, 3, 5)] + [255] for n in names],
                       np.uint8)
    return palette[choice]


def material_fixture(root: Path, radius: float, seed: int = 7) -> tuple[dict, dict]:
    pixels = material_pixels(seed, (24, 16))
    Image.fromarray(pixels).save(root / "materials.png")
    bundle = {"schema": BUNDLE, "world": {"width": 96, "height": 64},
              "collision": {"actorRadius": radius, "rects": [[10, 10, 3, 30], [40.25, 5, 10, 2.5]]},
              "material_map": {"image": "materials.png", "materials": MATERIAL_CLASSES},
              "spawns": [{"id": "a", "x": 2, "y": 2}, {"id": "b", "x": 60, "y": 40}]}
    return bundle, {"rgba": pixels.ravel().tolist(), "width": 24, "height": 16}


def one_way_fixture(root: Path) -> tuple[dict, dict]:
    """A side-view room: a one_way ledge row the actor may jump up through but not drop onto (N11)."""
    pixels = np.zeros((12, 16, 4), np.uint8)
    pixels[..., :3] = (64, 160, 64)
    pixels[..., 3] = 255
    pixels[6, 2:14, :3] = (224, 224, 0)   # ledge
    pixels[10, 6:9, :3] = (80, 80, 80)    # a rock below it
    Image.fromarray(pixels).save(root / "materials.png")
    bundle = {"schema": BUNDLE, "world": {"width": 64, "height": 48},
              "collision": {"actorRadius": 2},
              "material_map": {"image": "materials.png", "materials": {
                  "air": {"class": "decor", "color": "#40a040"}, "ledge": {"class": "one_way", "color": "#e0e000"},
                  "rock": {"class": "solid", "color": "#505050"}}},
              "spawns": [{"id": "below", "x": 20, "y": 40}, {"id": "above", "x": 40, "y": 8}],
              "interactions": [{"id": "top", "x": 30, "y": 10}, {"id": "bottom", "x": 30, "y": 36}]}
    return bundle, {"rgba": pixels.ravel().tolist(), "width": 16, "height": 12}


FIXTURES = {"walk-regions": walk_fixture, "props": props_fixture,
            "material-point": lambda root: material_fixture(root, 0),
            "material-r3": lambda root: material_fixture(root, 3, seed=11),
            "one-way": one_way_fixture}


def sample_points(width: float, height: float) -> np.ndarray:
    """A quarter-pixel lattice with a margin, so exact shape edges and pixel boundaries are included."""
    xs = np.arange(-4, width + 4, 0.25)
    ys = np.arange(-4, height + 4, 0.25)
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel()])


def sample_segments(width: float, height: float, count: int = 400, seed: int = 11) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    ends = rng.uniform([0, 0, 0, 0], [width, height, width, height], (count, 4))
    segments = (np.round(ends * 4) / 4).tolist()
    # short vertical drops and climbs, where one_way and thin walls decide
    starts = rng.uniform([0, 0], [width, height], (count // 2, 2))
    for (x, y), length in zip(np.round(starts * 2) / 2, rng.uniform(1, 12, count // 2)):
        segments.append([float(x), float(y), float(x), float(round(y + length * rng.choice([-1, 1]), 2))])
    return segments


def _rotated_shapes(blocking) -> list[dict]:
    """Solids whose geometry used sin and cos: rotated ellipses and rotated rect footprints."""
    shapes = []
    for solid in blocking.solids:
        if solid["shape"] == "ellipse" and solid.get("rotate", 0):
            shapes.append(solid)
        elif solid["shape"] == "polygon" and str(solid.get("source", "")).startswith("object:"):
            shapes.append(solid)
    return shapes


def _near_rotated(shapes: list[dict], px: np.ndarray, py: np.ndarray, tolerance: float = 1e-9) -> np.ndarray:
    near = np.zeros(px.shape, bool)
    for shape in shapes:
        if shape["shape"] == "ellipse":
            theta = math.radians(shape["rotate"])
            c, s = math.cos(theta), math.sin(theta)
            ex, ey = px - shape["cx"], py - shape["cy"]
            u, v = ex * c + ey * s, ey * c - ex * s
            near |= np.abs((u / shape["rx"]) ** 2 + (v / shape["ry"]) ** 2 - 1) < 1e-9
            continue
        points = shape["points"]
        for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
            dx, dy = bx - ax, by - ay
            t = np.clip(((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy), 0, 1)
            near |= np.hypot(px - ax - t * dx, py - ay - t * dy) < tolerance
    return near


class Reference:
    """forge_nav's answers for one fixture."""

    def __init__(self, bundle: dict, root: Path) -> None:
        self.blocking = NAV.blocking_set_from_document(bundle, root)
        self.model = self.blocking.model()
        self.rotated = _rotated_shapes(self.blocking)

    def exempt(self, px: np.ndarray, py: np.ndarray, *, footprint: bool) -> np.ndarray:
        offsets = self.model.offsets if footprint else self.model.offsets[:1]
        near = np.zeros(px.shape, bool)
        for ox, oy in offsets:
            near |= _near_rotated(self.rotated, px + ox, py + oy)
        return near

    def material_grid(self) -> dict | None:
        """The grid build_scene_preview embeds: BLOCK and ONE_WAY bit planes of forge_nav's codes."""
        import base64
        codes = self.blocking.material_codes
        if codes is None:
            return None
        plane = lambda mask: base64.b64encode(np.packbits(mask.ravel(), bitorder="little").tobytes()).decode("ascii")
        scale = self.blocking.material_scale
        return {"width": int(codes.shape[1]), "height": int(codes.shape[0]), "cellWidth": scale, "cellHeight": scale,
                "bits": plane(codes == NAV.BLOCK), "oneWay": plane(codes == NAV.ONE_WAY)}


_JS_QUERY = """
const world = rt.createMapRuntime(job.bundle, {materialGrid: job.grid});
const free = job.points.map(([x, y]) => (rt.pointFree(world, x, y) ? 1 : 0));
const valid = job.points.map(([x, y]) => (rt.isValid(world, x, y) ? 1 : 0));
const clear = job.segments.map(([ax, ay, bx, by]) => (rt.segmentClear(world, ax, ay, bx, by) ? 1 : 0));
const sampled = job.segments.map(([ax, ay, bx, by]) => (rt.segmentClear(world, ax, ay, bx, by, {thinGap: false}) ? 1 : 0));
const nav = rt.navGrid(world), total = nav.cols * nav.rows;
const nodes = [], moves = [];
for (let k = 0; k < total; k++) {
  nodes.push(rt.cellValid(world, k) ? 1 : 0);
  const i = k % nav.cols;
  let bits = 0;
  if (i + 1 < nav.cols && rt.moveOpen(world, k, k + 1)) bits |= 1;
  if (k + nav.cols < total && rt.moveOpen(world, k, k + nav.cols)) bits |= 2;
  if (i > 0 && rt.moveOpen(world, k, k - 1)) bits |= 4;
  if (k >= nav.cols && rt.moveOpen(world, k, k - nav.cols)) bits |= 8;
  moves.push(bits);
}
const starts = rt.routeStarts(world).map((s) => [s.x, s.y]);
const field = rt.floodFrom(world, starts);
const verdicts = {};
for (const item of world.interactions) {
  verdicts[`interaction:${item.id}`] = item.reach === null ? rt.pointTarget(world, field, item.x, item.y).node >= 0
    : rt.reachTarget(world, field, item.x, item.y, item.reach).node >= 0;
}
for (const anchor of world.anchors) {
  anchor.slots.forEach(([x, y], k) => { verdicts[`slot:${anchor.name}/${k}`] = rt.pointTarget(world, field, x, y).node >= 0; });
  anchor.approach.forEach(([x, y], k) => { verdicts[`approach:${anchor.name}/${k}`] = rt.pointTarget(world, field, x, y).node >= 0; });
}
for (const portal of world.portals) verdicts[`exit:${portal.id}`] = rt.exitTarget(world, field, portal).node >= 0;
const grid = job.rgba ? rt.materialGridFromRGBA(Uint8Array.from(job.rgba), job.width, job.height,
  job.bundle.material_map, job.bundle.world.width, job.bundle.world.height) : null;
process.stdout.write(JSON.stringify({free, valid, clear, sampled, nodes, moves, cols: nav.cols, rows: nav.rows,
  cell: world.cell, dist: [...field.dist], verdicts,
  rgbaGrid: grid && {bits: [...grid.bits], oneWay: [...grid.oneWay], cell: grid.cellWidth}}));
"""


def _verdicts(reference: Reference, bundle: dict) -> tuple[dict, np.ndarray]:
    """forge_nav's N14 answers, keyed like the JS job."""
    starts = [(s["x"], s["y"]) for s in bundle.get("spawns", [])]
    for portal in bundle.get("portals", []):
        starts += [tuple(p) for p in (portal.get("entranceByFrom") or {}).values() if isinstance(p, list)]
    navigation = NAV.navigate(reference.model, starts)
    verdicts = {}
    for item in bundle.get("interactions", []):
        point = (item["x"], item["y"])
        answer = (navigation.point_target(point) if "reach" not in item
                  else navigation.reach_target(point, item["reach"]))
        verdicts[f"interaction:{item['id']}"] = answer.reachable
    for name, anchor in (bundle.get("anchors") or {}).items():
        for k, slot in enumerate(anchor.get("slots") or []):
            verdicts[f"slot:{name}/{k}"] = navigation.point_target(slot).reachable
        approach = anchor.get("approach") or []
        approach = [approach] if approach and not isinstance(approach[0], list) else approach
        for k, point in enumerate(approach):
            verdicts[f"approach:{name}/{k}"] = navigation.point_target(point).reachable
    for portal in bundle.get("portals", []):
        verdicts[f"exit:{portal['id']}"] = navigation.exit_target(
            NAV.Trigger.from_portal(portal), portal.get("activation", "crossing"), portal.get("radius", 0)).reachable
    return verdicts, navigation.distance


# --------------------------------------------------------------------------- tests

@pytest.mark.node
class NodeSuiteTests(unittest.TestCase):
    def test_node_test_suite_passes(self):
        node = require_node()
        completed = subprocess.run([node, "--test", str(NODE_SUITE)], capture_output=True, encoding="utf-8",
                                   errors="replace", timeout=300, check=False, cwd=REPO_ROOT)
        self.assertEqual(completed.returncode, 0, completed.stdout[-4000:] + completed.stderr[-2000:])
        self.assertIn("# fail 0", completed.stdout)


@pytest.mark.node
class ForgeNavReferenceTests(unittest.TestCase):
    """map-runtime.mjs against forge_nav (D1, D2, D4) on synthetic fixtures."""

    def compare(self, name: str, fixture) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, pixels = fixture(root)
            reference = Reference(bundle, root)
            width, height = bundle["world"]["width"], bundle["world"]["height"]
            points = sample_points(width, height)
            segments = sample_segments(width, height)
            job = {"bundle": bundle, "grid": reference.material_grid(), "points": points.tolist(), "segments": segments,
                   **(pixels or {})}
            result = run_node(_JS_QUERY, job)
            model = reference.model
            px, py = points[:, 0], points[:, 1]
            for query, js, expected, footprint in (("pointFree", result["free"], model.centre_ok(px, py), False),
                                                   ("isValid", result["valid"], model.valid(px, py), True)):
                exempt = reference.exempt(px, py, footprint=footprint)
                self.assertLess(exempt.sum(), 0.002 * len(points), "only boundary points of rotated shapes are exempt")
                bad = np.flatnonzero((np.array(js, bool) != expected) & ~exempt)
                self.assertEqual(bad.size, 0, f"{name} {query} differs at {points[bad[:5]].tolist()}")
            for key, thin in (("clear", True), ("sampled", False)):
                expected = [int(model.segment_clear(s[:2], s[2:], thin_gap=thin)) for s in segments]
                bad = [s for s, a, b in zip(segments, result[key], expected) if a != b]
                self.assertEqual(bad, [], f"{name} segmentClear(thinGap={thin}) differs")
                self.assertTrue(0 < sum(expected) < len(expected), "both clear and blocked segments are sampled")
            grid = NAV.build_grid(model)
            self.assertEqual((result["cell"], result["rows"], result["cols"]), (grid.cell, grid.rows, grid.cols))
            np.testing.assert_array_equal(np.array(result["nodes"], bool).reshape(grid.rows, grid.cols), grid.valid,
                                          f"{name}: node validity")
            np.testing.assert_array_equal(np.array(result["moves"], np.uint8).reshape(grid.rows, grid.cols), grid.moves,
                                          f"{name}: open moves")
            verdicts, distance = _verdicts(reference, bundle)
            np.testing.assert_array_equal(np.array(result["dist"], np.int32).reshape(grid.rows, grid.cols), distance,
                                          f"{name}: BFS distances from the starts")
            self.assertEqual(result["verdicts"], verdicts, f"{name}: N14 target verdicts")
            if pixels is not None:
                codes = reference.blocking.material_codes
                bits = np.unpackbits(np.array(result["rgbaGrid"]["bits"], np.uint8), bitorder="little")[:codes.size]
                one_way = np.unpackbits(np.array(result["rgbaGrid"]["oneWay"], np.uint8), bitorder="little")[:codes.size]
                np.testing.assert_array_equal(bits.reshape(codes.shape), codes == NAV.BLOCK)
                np.testing.assert_array_equal(one_way.reshape(codes.shape), codes == NAV.ONE_WAY)
                self.assertEqual(result["rgbaGrid"]["cell"], reference.blocking.material_scale)

    def test_walk_regions_holes_and_closed_solids(self):
        self.compare("walk-regions", walk_fixture)

    def test_footprints_props_registry_basis_and_flip_x(self):
        self.compare("props", props_fixture)

    def test_material_classes_on_points(self):
        self.compare("material-point", FIXTURES["material-point"])

    def test_material_classes_with_an_actor(self):
        self.compare("material-r3", FIXTURES["material-r3"])

    def test_one_way_blocks_downward_moves_only(self):
        self.compare("one-way", one_way_fixture)

    def test_tangent_ellipses_touch_at_one_point(self):
        """N10 tangents (integration pass, meadow example): the runtime's grid moves and segments agree with
        forge_nav where an ellipse only touches a row, and a row 1e-6 px inside still crosses it."""
        self.compare("tangent", tangent_fixture)
        with tempfile.TemporaryDirectory() as temporary:
            bundle, _ = tangent_fixture(Path(temporary))
            grid = NAV.build_grid(Reference(bundle, Path(temporary)).model)
        self.assertTrue(grid.valid[19, 2] and grid.valid[19, 3] and grid.valid[7, 10] and grid.valid[8, 10])
        self.assertTrue(grid.moves[19, 2] & NAV.MOVE_E and grid.moves[19, 3] & NAV.MOVE_W, "row tangent: open")
        self.assertTrue(grid.moves[7, 10] & NAV.MOVE_S and grid.moves[8, 10] & NAV.MOVE_N, "column tangent: open")
        self.assertTrue(grid.valid[15, 7] and grid.valid[15, 8])
        self.assertFalse(grid.moves[15, 7] & NAV.MOVE_E or grid.moves[15, 8] & NAV.MOVE_W, "crossed by 1e-6 px: closed")

    def test_the_fixtures_exercise_every_rule(self):
        """Sanity: the fixtures hold thin gaps, one_way moves, flipped footprints and unreachable targets."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            walk, _ = walk_fixture(root)
            grid = NAV.build_grid(Reference(walk, root).model)
            self.assertTrue(grid.thin_gaps, "a wall thinner than half a cell is closed by the thin-gap rule only")
            one_way, _ = one_way_fixture(root)
            reference = Reference(one_way, root)
            self.assertGreater(NAV.build_grid(reference.model).one_way_blocked, 0)
            verdicts, _ = _verdicts(reference, one_way)
            self.assertEqual(verdicts, {"interaction:top": True, "interaction:bottom": True})
            props, _ = props_fixture(root)
            sources = [s["source"] for s in Reference(props, root).blocking.footprints]
            self.assertIn("object:h", sources, "a footprint from the props registry, mirrored")
            self.assertNotIn("object:k", sources, "the registry's solid: false")
            self.assertIn("object:l", sources, "the object's own solid flag wins")
            verdicts, _ = _verdicts(Reference(walk, root), walk)
            self.assertIn(False, verdicts.values(), "some target is unreachable")
            self.assertIn(True, verdicts.values())

    def test_nav_cell_rounds_half_up(self):
        radii = [k / 4 for k in range(0, 81)]
        result = run_node("process.stdout.write(JSON.stringify(job.radii.map((r) => rt.navCellSize(r))));",
                          {"radii": radii})
        self.assertEqual(result, [NAV.nav_cell(r) for r in radii])
        self.assertEqual(result[radii.index(5.0)], 3, "5 / 2 = 2.5 rounds up (Python round() would give 2)")


def tiled_hazard_map(root: Path) -> Path:
    """Review r2 finding 3: one tile whose collision is a full-cell rect, and a hazard pixel in the material map,
    a bundle map_bundle validate passes. map-runtime.mjs given it directly walked through both."""
    (root / "tiles").mkdir(parents=True)
    Image.new("RGBA", (32, 16), (90, 90, 90, 255)).save(root / "tiles" / "t.png")
    (root / "tiles" / "t.tileset.json").write_text(json.dumps({
        "schema": "generate2dmap.tileset.v1", "id": "t", "image": "t.png", "tile_size": 16, "columns": 2,
        "tilecount": 2, "kind": "flat", "materials": ["stone"], "seamless_verified": False,
        "tiles": [{"index": 0}, {"index": 1, "collision": [{"shape": "rect", "x": 0, "y": 0, "w": 16, "h": 16}]}]}),
        encoding="utf-8")
    pixels = np.zeros((4, 4, 4), np.uint8)
    pixels[...] = (128, 128, 128, 255)
    pixels[0, 3] = (224, 64, 16, 255)  # a lava pixel: world (24..32, 0..8)
    Image.fromarray(pixels).save(root / "materials.png")
    bundle = {"schema": BUNDLE, "id": "wall", "tile_size": 16, "world": {"width": 32, "height": 32, "unit": "px"},
              "tilesets": [{"id": "t", "manifest": "tiles/t.tileset.json"}],
              "layers": [{"name": "ground", "kind": "tiles", "tileset": "t", "data": [[1, 0], [0, 0]]}],
              "material_map": {"image": "materials.png", "materials": {
                  "floor": {"class": "decor", "color": "#808080"}, "lava": {"class": "hazard", "color": "#e04010"}}},
              "collision": {"actorRadius": 1}, "spawns": [{"id": "start", "x": 16, "y": 24}],
              "interactions": [{"id": "corner", "x": 20, "y": 4}]}
    path = root / "map-bundle.json"
    path.write_text(json.dumps(bundle, indent=1), encoding="utf-8")
    return path


_JS_DIRECT = """
const answers = {};
try { rt.createMapRuntime(job.bundle); answers.bare = "built"; } catch (error) { answers.bare = `${error.name}: ${error.message}`; }
try { rt.createMapRuntime(job.bundle, {tileSolids: job.inputs.tileSolids}); answers.tilesOnly = "built"; }
catch (error) { answers.tilesOnly = `${error.name}: ${error.message}`; }
const world = rt.createMapRuntime(job.bundle, job.inputs);
answers.valid = job.points.map(([x, y]) => rt.isValid(world, x, y));
answers.free = job.lattice.map(([x, y]) => (rt.pointFree(world, x, y) ? 1 : 0));
answers.routes = rt.traverseRoutes(world, {speed: 60}).ok;
process.stdout.write(JSON.stringify(answers));
"""


@pytest.mark.node
class ResolvedInputsTests(unittest.TestCase):
    """The custom-engine route (references/map-presets.md): a game reads map-bundle.json itself and hands
    map-runtime.mjs the tile collision and material grid that map_nav check writes into nav-grid.json (D2)."""

    def test_nav_grid_runtime_inputs_make_the_runtime_answer_like_map_nav(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = tiled_hazard_map(root)
            checked = run_cli([script_path("generate2dmap", "map_bundle"), "validate", "--bundle", path])
            self.assertEqual(checked.returncode, 0, checked.stderr)
            nav = run_cli([script_path("generate2dmap", "map_nav"), "check", "--bundle", path, "--output-dir",
                           root / "nav"])
            self.assertEqual(nav.returncode, 0, nav.stderr)
            grid = json.loads((root / "nav" / "nav-grid.json").read_text(encoding="utf-8"))
            blocking = NAV.read_blocking_set(path)
            self.assertEqual(grid["runtimeInputs"], json.loads(json.dumps(NAV.runtime_inputs(blocking))))
            self.assertEqual([solid["source"] for solid in grid["runtimeInputs"]["tileSolids"]], ["tiles:ground"])
            points = [[8, 8], [28, 4], [16, 24]]  # in the wall tile, on the lava pixel, open floor
            query = run_cli([script_path("generate2dmap", "map_nav"), "query", "--bundle", path,
                             *[arg for x, y in points for arg in ("--point", f"{x},{y}")]])
            expected = [point["valid"] for point in json.loads(query.stdout)["points"]]
            self.assertEqual(expected, [False, False, True])
            lattice = sample_points(32, 32)
            result = run_node(_JS_DIRECT, {"bundle": json.loads(path.read_text(encoding="utf-8")),
                                           "inputs": grid["runtimeInputs"], "points": points,
                                           "lattice": lattice.tolist()})
        self.assertRegex(result["bare"], r"^TypeError: layer \"ground\" is a tiles layer, but its tile collision")
        self.assertRegex(result["tilesOnly"], r"^TypeError: material_map \(N8\) needs options\.materialGrid")
        self.assertEqual(result["valid"], expected, "the runtime blocks the tile wall and the lava pixel")
        np.testing.assert_array_equal(np.array(result["free"], bool),
                                      blocking.model().centre_ok(lattice[:, 0], lattice[:, 1]))
        self.assertTrue(result["routes"], "the corner interaction is walked around the wall and the lava")


class RuntimeSourceTests(unittest.TestCase):
    def test_runtime_can_be_inlined_into_a_page(self):
        source = RUNTIME.read_text(encoding="utf-8")
        self.assertTrue(source.isascii())
        for marker in ("</script", "<script", "<!--", "://"):
            self.assertNotIn(marker, source.lower())
        self.assertNotRegex(source, r"\b(?:fetch|XMLHttpRequest|WebSocket|Math\.random|Date\.now|performance\.now)\b",
                            "the runtime is deterministic and offline")
        # 1.1.1: N10 tangent ellipses; 1.1.2: N10 touching slivers, forge_nav's rect cuts, resolved tiles/materials
        self.assertRegex(source, r'export const RUNTIME_VERSION = "1\.1\.2";')
        self.assertEqual(SLIVER_PX_JS.findall(source), ["1e-9"])
        self.assertEqual(NAV._SLIVER_PX, 1e-9)
        self.assertIn("const EDGE_U_SLACK = 1e-12;", source)
        self.assertEqual(NAV._EDGE_U_SLACK, 1e-12)

    def test_runtime_names_the_forge_nav_rule_book(self):
        source = RUNTIME.read_text(encoding="utf-8")
        for rule in (f"N{number}" for number in range(1, 16)):
            self.assertRegex(source, rf"\b{rule}\b", rule)
        self.assertIn("forge_nav", source)
        self.assertEqual(NAV.SQRT1_2, 0.7071067811865476)
        self.assertIn("const SQRT1_2 = 0.7071067811865476;", source)


if __name__ == "__main__":
    unittest.main()
