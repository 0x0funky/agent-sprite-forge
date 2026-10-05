"""map-runtime.mjs (B17-T1) under Node, plus a differential check of its Appendix C collision query.

``AppendixC`` below is an independent numpy reading of plan Appendix C with the parity expressions
documented at the top of map-runtime.mjs. The JS query must agree with it on every sampled point and
segment of three fixtures. Integration replaces ``AppendixC`` with map_nav.py's query to get the
JS <-> Python parity test the plan asks for (handoff/B17-map-scene-preview.md sections 6 and 7).
"""
from __future__ import annotations

import base64
import json
import math
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import REPO_ROOT, SKILLS_DIR, load_script, require_node

RUNTIME = SKILLS_DIR / "generate2dmap" / "references" / "runtime" / "map-runtime.mjs"
NODE_SUITE = REPO_ROOT / "tests" / "js" / "map-runtime.test.mjs"
SQRT_HALF = math.sqrt(0.5)
DIRECTIONS = ((1, 0), (SQRT_HALF, SQRT_HALF), (0, 1), (-SQRT_HALF, SQRT_HALF),
              (-1, 0), (-SQRT_HALF, -SQRT_HALF), (0, -1), (SQRT_HALF, -SQRT_HALF))


def run_node(code: str, payload) -> dict:
    node = require_node()
    completed = subprocess.run([node, "--input-type=module", "-e", f"import * as rt from {json.dumps(RUNTIME.as_uri())};\n"
                                + "import {readFileSync} from 'node:fs';\nconst job = JSON.parse(readFileSync(0, 'utf8'));\n"
                                + code], input=json.dumps(payload), capture_output=True, encoding="utf-8",
                               errors="replace", timeout=300, check=False)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------- reference reading of Appendix C

def _inside_polygon(px: np.ndarray, py: np.ndarray, points) -> np.ndarray:
    inside = np.zeros(px.shape, bool)
    for i in range(len(points)):
        (xi, yi), (xj, yj) = points[i], points[i - 1]
        crosses = (yi > py) != (yj > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            inside ^= crosses & (px < xi + (py - yi) * (xj - xi) / (yj - yi))
    return inside


def _footprint_shape(obj: dict):
    footprint = obj.get("footprint")
    if not footprint or obj.get("solid") is False or footprint["shape"] == "none":
        return None
    s = 1 if footprint.get("basis") == "world_px" else obj.get("scale", 1)
    ox, oy = footprint.get("offset", [0, 0])
    w, h = footprint["width"] * s, footprint["depth"] * s
    cx, cy = obj["x"] + ox * s, obj["y"] + oy * s
    rotate = footprint.get("rotate", 0)
    if footprint["shape"] == "ellipse":
        return {"shape": "ellipse", "cx": cx, "cy": cy, "rx": w / 2, "ry": h / 2, "rotate": rotate}
    if rotate == 0:
        return {"shape": "rect", "x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h}
    theta = rotate * math.pi / 180
    cos, sin, hw, hh = math.cos(theta), math.sin(theta), w / 2, h / 2
    return {"shape": "polygon", "rotated": True, "points": [[cx + u * cos - v * sin, cy + u * sin + v * cos]
                                                            for u, v in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]}


def _ellipse_value(shape: dict, px: np.ndarray, py: np.ndarray) -> np.ndarray:
    """(u / rx)^2 + (v / ry)^2 in the ellipse frame; below 1 inside."""
    u, v = px - shape["cx"], py - shape["cy"]
    rotate = shape.get("rotate", 0)
    if rotate != 0:
        theta = rotate * math.pi / 180
        cos, sin = math.cos(theta), math.sin(theta)
        u, v = u * cos + v * sin, -u * sin + v * cos
    nu, nv = u / shape["rx"], v / shape["ry"]
    return nu * nu + nv * nv


def _near_rotated_edge(shape: dict, px: np.ndarray, py: np.ndarray, tolerance: float) -> np.ndarray:
    """Points within tolerance of the boundary of a rotated shape, where cos/sin may differ by an ulp
    between JavaScript engines and C runtimes (V8 and the MSVC CRT disagree on sin(pi / 4))."""
    if shape["shape"] == "ellipse" and shape.get("rotate", 0) != 0 and shape["rx"] > 0 and shape["ry"] > 0:
        return np.abs(_ellipse_value(shape, px, py) - 1) < tolerance
    near = np.zeros(px.shape, bool)
    if shape.get("rotated"):
        points = shape["points"]
        for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
            dx, dy = bx - ax, by - ay
            t = np.clip(((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy), 0, 1)
            near |= np.hypot(px - ax - t * dx, py - ay - t * dy) < tolerance
    return near


def _inside_shape(shape: dict, px: np.ndarray, py: np.ndarray) -> np.ndarray:
    if shape["shape"] == "rect":
        x1, y1 = shape["x"] + shape["w"], shape["y"] + shape["h"]
        return (px >= shape["x"]) & (px < x1) & (py >= shape["y"]) & (py < y1)
    if shape["shape"] == "polygon":
        return _inside_polygon(px, py, shape["points"])
    if not (shape["rx"] > 0 and shape["ry"] > 0):
        return np.zeros(px.shape, bool)
    return _ellipse_value(shape, px, py) < 1


class AppendixC:
    """Plan Appendix C as numpy, written from the plan text and the parity rules, not from the JS."""

    def __init__(self, bundle: dict, material: dict | None = None) -> None:
        self.width, self.height = bundle["world"]["width"], bundle["world"]["height"]
        collision = bundle["collision"]
        self.r = collision["actorRadius"]
        self.ry = self.r * collision.get("ySquash", 1)
        self.cell = max(1, math.floor(self.r / 2 + 0.5))
        self.regions = collision.get("walkRegions", [])
        self.shapes = list(collision.get("solids", []))
        self.shapes += [{"shape": "rect", "x": x, "y": y, "w": w, "h": h} for x, y, w, h in collision.get("rects", [])]
        self.shapes += [shape for shape in map(_footprint_shape, bundle.get("objects", [])) if shape]
        self.material = material
        if material is not None:
            self.bits = np.unpackbits(np.frombuffer(base64.b64decode(material["bits"]), np.uint8), bitorder="little")

    def point_free(self, px: np.ndarray, py: np.ndarray) -> np.ndarray:
        if self.regions:
            free = np.zeros(px.shape, bool)
            for region in self.regions:
                inside = _inside_polygon(px, py, region["polygon"])
                for hole in region.get("holes", []):
                    inside &= ~_inside_polygon(px, py, hole)
                free |= inside
        else:
            free = (px >= 0) & (px < self.width) & (py >= 0) & (py < self.height)
        for shape in self.shapes:
            free &= ~_inside_shape(shape, px, py)
        if self.material is not None:
            grid = self.material
            i, j = np.floor(px / grid["cellWidth"]), np.floor(py / grid["cellHeight"])
            inside = (i >= 0) & (j >= 0) & (i < grid["width"]) & (j < grid["height"])
            k = np.where(inside, j * grid["width"] + i, 0).astype(np.int64)
            free &= ~(inside & (self.bits[k] == 1))
        return free

    def is_blocked(self, px: np.ndarray, py: np.ndarray) -> np.ndarray:
        blocked = ~self.point_free(px, py)
        if self.r > 0:
            for ux, uy in DIRECTIONS:
                blocked |= ~self.point_free(px + self.r * ux, py + self.ry * uy)
        return blocked

    def near_rotated_edge(self, px: np.ndarray, py: np.ndarray, *, footprint: bool, tolerance: float = 1e-9) -> np.ndarray:
        """Points (or, with footprint, any of their 9 samples) within tolerance of a rotated shape's edge."""
        samples = [(px, py)]
        if footprint and self.r > 0:
            samples += [(px + self.r * ux, py + self.ry * uy) for ux, uy in DIRECTIONS]
        near = np.zeros(px.shape, bool)
        for sx, sy in samples:
            for shape in self.shapes:
                near |= _near_rotated_edge(shape, sx, sy, tolerance)
        return near

    def segment_clear(self, ax: float, ay: float, bx: float, by: float) -> bool:
        dx, dy = bx - ax, by - ay
        n = max(1, math.ceil(math.sqrt(dx * dx + dy * dy) / (self.cell / 2)))
        k = np.arange(n + 1, dtype=np.float64)
        return not self.is_blocked(ax + dx * k / n, ay + dy * k / n).any()


# --------------------------------------------------------------------------- fixtures

def walk_fixture() -> tuple[dict, None]:
    return {
        "world": {"width": 96, "height": 64},
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
                {"shape": "polygon", "points": [[60, 50], [72, 52], [64, 58.25]]}],
            "rects": [[80, 50, 6, 4]]},
    }, None


def props_fixture() -> tuple[dict, None]:
    def tree(ident, x, y, **extra):
        return {"id": ident, "prop": "tree", "x": x, "y": y, "anchor_px": [8, 30], **extra}
    return {
        "world": {"width": 96, "height": 64},
        "collision": {"actorRadius": 4},
        "objects": [
            tree("a", 20, 20, scale=1.5, footprint={"shape": "ellipse", "width": 10, "depth": 5, "offset": [1, -2]}),
            tree("b", 50, 20, footprint={"shape": "rect", "width": 9, "depth": 4.5, "offset": [0.5, -1]}, scale=1.25),
            tree("c", 75, 25, footprint={"shape": "rect", "width": 12, "depth": 4, "rotate": 30}),
            tree("d", 25, 48, scale=3, footprint={"shape": "ellipse", "width": 6, "depth": 3, "basis": "world_px"}),
            tree("e", 55, 48, footprint={"shape": "ellipse", "width": 10, "depth": 5}, solid=False),
            tree("f", 75, 50, footprint={"shape": "none"}),
            tree("g", 85, 10, footprint={"shape": "ellipse", "width": 7, "depth": 7, "rotate": 45}, scale=0.5)],
    }, None


def material_fixture(radius: float) -> tuple[dict, dict]:
    rng = np.random.default_rng(7)
    blocked = rng.random((16, 24)) < 0.15
    bits = base64.b64encode(np.packbits(blocked.ravel(), bitorder="little").tobytes()).decode("ascii")
    grid = {"width": 24, "height": 16, "cellWidth": 4.0, "cellHeight": 4.0, "bits": bits}
    return {"world": {"width": 96, "height": 64},
            "collision": {"actorRadius": radius, "rects": [[10, 10, 3, 30], [40.25, 5, 10, 2.5]]}}, grid


FIXTURES = {"walk-regions": walk_fixture, "props": props_fixture,
            "material-point": lambda: material_fixture(0), "material-r3": lambda: material_fixture(3)}


def sample_points(width: float, height: float) -> np.ndarray:
    """A quarter-pixel lattice with a margin, so exact shape edges and pixel boundaries are included."""
    xs = np.arange(-4, width + 4, 0.25)
    ys = np.arange(-4, height + 4, 0.25)
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel()])


def sample_segments(width: float, height: float, count: int = 400) -> list[list[float]]:
    rng = np.random.default_rng(11)
    ends = rng.uniform([0, 0, 0, 0], [width, height, width, height], (count, 4))
    return (np.round(ends * 4) / 4).tolist()


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
class AppendixCParityTests(unittest.TestCase):
    def test_point_and_footprint_queries_agree_on_every_sample(self):
        for name, fixture in FIXTURES.items():
            with self.subTest(fixture=name):
                bundle, grid = fixture()
                points = sample_points(bundle["world"]["width"], bundle["world"]["height"])
                result = run_node(
                    "const world = rt.createMapRuntime(job.bundle, {materialGrid: job.grid});\n"
                    "const free = job.points.map(([x, y]) => (rt.pointFree(world, x, y) ? 1 : 0));\n"
                    "const blocked = job.points.map(([x, y]) => (rt.isBlocked(world, x, y) ? 1 : 0));\n"
                    "process.stdout.write(JSON.stringify({free, blocked, cell: world.cell}));\n",
                    {"bundle": bundle, "grid": grid, "points": points.tolist()})
                reference = AppendixC(bundle, grid)
                px, py = points[:, 0], points[:, 1]
                free = reference.point_free(px, py)
                blocked = reference.is_blocked(px, py)
                self.assertEqual(result["cell"], reference.cell)
                self.assertGreater(free.sum(), 100, "the fixture has free space")
                self.assertGreater((~free).sum(), 100, "the fixture has blocked space")
                for query, js, expected, footprint in (("pointFree", result["free"], free, False),
                                                       ("isBlocked", result["blocked"], blocked, True)):
                    exempt = reference.near_rotated_edge(px, py, footprint=footprint)
                    self.assertLess(exempt.sum(), 0.002 * len(points), "only boundary points of rotated shapes are exempt")
                    bad = np.flatnonzero((np.array(js, bool) != expected) & ~exempt)
                    self.assertEqual(bad.size, 0, f"{query} differs at {points[bad[:5]].tolist()}")

    def test_segment_clear_agrees(self):
        for name, fixture in FIXTURES.items():
            with self.subTest(fixture=name):
                bundle, grid = fixture()
                segments = sample_segments(bundle["world"]["width"], bundle["world"]["height"])
                result = run_node(
                    "const world = rt.createMapRuntime(job.bundle, {materialGrid: job.grid});\n"
                    "process.stdout.write(JSON.stringify(job.segments.map(([ax, ay, bx, by]) =>"
                    " (rt.segmentClear(world, ax, ay, bx, by) ? 1 : 0))));\n",
                    {"bundle": bundle, "grid": grid, "segments": segments})
                reference = AppendixC(bundle, grid)
                expected = [int(reference.segment_clear(*segment)) for segment in segments]
                self.assertEqual(result, expected)
                self.assertTrue(0 < sum(expected) < len(expected), "both clear and blocked segments are sampled")

    def test_nav_cell_rounds_half_up(self):
        radii = [k / 4 for k in range(0, 81)]
        result = run_node("process.stdout.write(JSON.stringify(job.radii.map((r) => rt.navCellSize(r))));",
                          {"radii": radii})
        self.assertEqual(result, [max(1, math.floor(r / 2 + 0.5)) for r in radii])
        self.assertEqual(result[radii.index(5.0)], 3, "5 / 2 = 2.5 rounds up (Python round() would give 2)")

    def test_material_grid_from_pixels_matches_the_preview_builder(self):
        preview = load_script("generate2dmap", "build_scene_preview")
        rng = np.random.default_rng(5)
        palette = np.array([[59, 93, 201], [10, 10, 10], [200, 0, 0], [0, 120, 255]], np.uint8)
        choice = rng.integers(0, 5, (12, 20))
        rgba = np.zeros((12, 20, 4), np.uint8)
        rgba[choice < 4, :3] = palette[choice[choice < 4]]
        rgba[choice < 4, 3] = 255
        materials = {"water": {"class": "liquid", "color": "#3b5dc9"}, "rock": {"class": "solid", "color": [10, 10, 10]},
                     "flowers": {"class": "decor", "color": "#c80000"},
                     "shallows": {"class": "liquid", "color": "#0078ff", "walkable": True}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.fromarray(rgba).save(root / "materials.png")
            build = preview.Build(root / "bundle.json", root)
            grid, _ = preview.compile_material_grid({"material_map": {"image": "materials.png", "materials": materials}},
                                                    build, (80.0, 48.0))
        result = run_node(
            "const grid = rt.materialGridFromRGBA(Uint8Array.from(job.rgba), 20, 12, {materials: job.materials}, 80, 48);\n"
            "process.stdout.write(JSON.stringify({bytes: [...grid.bytes], cellWidth: grid.cellWidth,"
            " cellHeight: grid.cellHeight}));\n",
            {"rgba": rgba.ravel().tolist(), "materials": materials})
        self.assertEqual(result["bytes"], list(base64.b64decode(grid["bits"])))
        self.assertEqual((result["cellWidth"], result["cellHeight"]), (grid["cellWidth"], grid["cellHeight"]))


class RuntimeSourceTests(unittest.TestCase):
    def test_runtime_can_be_inlined_into_a_page(self):
        source = RUNTIME.read_text(encoding="utf-8")
        self.assertTrue(source.isascii())
        for marker in ("</script", "<script", "<!--", "://"):
            self.assertNotIn(marker, source.lower())
        self.assertNotRegex(source, r"\b(?:fetch|XMLHttpRequest|WebSocket|Math\.random|Date\.now|performance\.now)\b",
                            "the runtime is deterministic and offline")
        self.assertRegex(source, r'export const RUNTIME_VERSION = "1\.0\.0";')


if __name__ == "__main__":
    unittest.main()
