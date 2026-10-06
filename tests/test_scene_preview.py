"""build_scene_preview.py (B17-T2): a deterministic, self-contained, at most 16 MB preview.html.

Fixtures are synthetic and written per test. Contracts are checked against the generate2dmap vendored
schemas. Collision is forge_nav's D2 blocking set (integration decisions D1, D2, D4-D7, D33): tile
collision becomes world solids, material classes become BLOCK and one_way pixel planes, and the
page's route check (map-runtime.mjs) must agree with forge_nav's reachability on the same bundle.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, contract_errors, load_script,
                             require_node, run_cli, script_path)
from test_export_godot import build_bundle as build_engine_bundle

SKILL = "generate2dmap"
SCRIPT = script_path(SKILL, "build_scene_preview")
PREVIEW = load_script(SKILL, "build_scene_preview")
NAV = load_script(SKILL, "forge_nav")
RUNTIME = SKILLS_DIR / SKILL / "references" / "runtime" / "map-runtime.mjs"


def schema_errors(instance, name: str) -> list[str]:
    """Validation errors against the vendored map.schema.json."""
    return contract_errors(instance, "map", name, skill=SKILL)


# --------------------------------------------------------------------------- fixtures

def png(path: Path, pixels) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.asarray(pixels, np.uint8)).save(path)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


TILE_ROWS = ["0,1" + ",-1" * 6, "2,3" + ",-1" * 6] + [",".join(["-1"] * 8)] * 4


def make_scene(root: Path, edit=None) -> Path:
    """A 64 x 48 world: ground image, an 8 px tile layer, a wall, two props, a material map, two spawns,
    an intent exit, an interaction and an anchor. ``edit(bundle, root)`` may change the bundle."""
    ground = np.zeros((48, 64, 4), np.uint8)
    ground[...] = (70, 120, 60, 255)
    png(root / "ground.png", ground)
    atlas = np.zeros((16, 16, 4), np.uint8)
    atlas[:8, :8] = (200, 0, 0, 255)
    atlas[:8, 8:] = (0, 200, 0, 255)
    atlas[8:, :8] = (0, 0, 200, 255)
    atlas[8:, 8:] = (200, 200, 0, 128)
    png(root / "tiles" / "atlas.png", atlas)
    (root / "tiles" / "deco.tileset.json").write_text(json.dumps({
        "schema": "generate2dmap.tileset.v1", "image": "atlas.png", "sha256": sha(root / "tiles" / "atlas.png"),
        "tile_size": 8, "columns": 2, "kind": "flat", "materials": ["deco"], "tiles": [{"index": k} for k in range(4)],
        "seamless_verified": False}), encoding="utf-8")
    (root / "deco.csv").write_text("\n".join(TILE_ROWS) + "\n", encoding="utf-8")
    prop = np.zeros((16, 8, 4), np.uint8)
    prop[:10, :, :] = (20, 90, 30, 255)
    prop[10:, 3:5, :] = (90, 60, 30, 255)
    png(root / "props" / "tree.png", prop)
    material = np.zeros((6, 8, 4), np.uint8)
    material[0, 5:7] = (59, 93, 201, 255)
    png(root / "materials.png", material)
    bundle = {
        "schema": "generate2dmap.map_bundle.v2", "name": "Test glade",
        "world": {"width": 64, "height": 48, "unit": "px"}, "tile_size": 8,
        "tilesets": [{"id": "deco", "manifest": "tiles/deco.tileset.json"}],
        "layers": [{"name": "ground", "kind": "image", "image": "ground.png"},
                   {"name": "deco", "kind": "tiles", "data": "deco.csv", "tileset": "deco"},
                   {"name": "props", "kind": "objects"}],
        "objects": [
            {"id": "tree-b", "prop": "tree", "x": 20, "y": 30, "anchor_px": [4, 15], "image": "props/tree.png",
             "image_sha256": sha(root / "props" / "tree.png"), "footprint": {"shape": "ellipse", "width": 4, "depth": 2}},
            {"id": "tree-a", "prop": "tree", "x": 20, "y": 30, "anchor_px": [4, 15], "image": "props/tree.png",
             "footprint": {"shape": "ellipse", "width": 4, "depth": 2}},
            {"id": "bush", "prop": "tree", "x": 44, "y": 20, "sortY": 40, "anchor_px": [4, 15], "scale": 2,
             "image": "props/tree.png", "footprint": {"shape": "rect", "width": 4, "depth": 2}, "solid": False},
        ],
        "collision": {"actorRadius": 2, "solids": [{"shape": "rect", "x": 30, "y": 0, "w": 2, "h": 24}]},
        "material_map": {"image": "materials.png", "materials": {"water": {"class": "liquid", "color": "#3b5dc9"}}},
        "spawns": [{"id": "west", "x": 8, "y": 30, "facing": "east"}, {"id": "east", "x": 50, "y": 30}],
        "portals": [{"id": "exit-east", "rect": [60, 24, 4, 12], "to": "meadow:west", "activation": "intent",
                     "travelDirection": [1, 0], "radius": 4, "entranceByFrom": {"meadow": "east"}}],
        "interactions": [{"id": "sign", "x": 12, "y": 12, "reach": 6}],
        "anchors": {"well": {"point": [48, 10], "slots": [[44, 16]], "approach": [48, 16]}},
    }
    if edit is not None:
        edit(bundle, root)
    path = root / "map_bundle.json"
    path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    return path


def with_tile_collision(bundle: dict, root: Path) -> None:
    """Tile 2 blocks its lower half with a rect, tile 3 with a triangle, tile 1 is walkable: false (no shapes)."""
    manifest = json.loads((root / "tiles" / "deco.tileset.json").read_text(encoding="utf-8"))
    manifest["tiles"] = [{"index": 0}, {"index": 1, "properties": {"walkable": False}},
                         {"index": 2, "collision": [{"shape": "rect", "x": 0, "y": 4, "w": 8, "h": 4}]},
                         {"index": 3, "collision": [{"shape": "polygon", "points": [[0, 8], [8, 8], [8, 0]]}]}]
    (root / "tiles" / "deco.tileset.json").write_text(json.dumps(manifest), encoding="utf-8")
    rows = [[-1] * 8 for _ in range(6)]
    rows[0][:2], rows[1][:2] = [0, 1], [2, 3]
    rows[3][3:6] = [1, 2, 3]
    (root / "deco.csv").write_text("\n".join(",".join(map(str, row)) for row in rows) + "\n", encoding="utf-8")


def build(bundle: Path, out: Path, *extra, env=None, encoding=None):
    return run_cli([SCRIPT, "--bundle", bundle, "--output-dir", out, *extra], encoding, env=env, timeout=300)


def page_data(html_text: str) -> tuple[dict, dict]:
    """(SCENE, IMAGE_DATA) as the page script reads them."""
    scene = re.search(r"^const SCENE = (.*);$", html_text, re.M).group(1)
    images = re.search(r"^const IMAGE_DATA = (.*);$", html_text, re.M).group(1)
    return json.loads(scene), json.loads(images)


def decode(images: dict, image_id: str) -> np.ndarray:
    mime, payload = images[image_id]
    assert mime == "image/png"
    with Image.open(io.BytesIO(base64.b64decode(payload))) as image:
        return np.asarray(image.convert("RGBA"))


def plane(grid: dict, key: str, count: int) -> list[int]:
    """Indices of the set bits of one material plane (bits = BLOCK, oneWay = ONE_WAY)."""
    if key not in grid:
        return []
    bits = np.unpackbits(np.frombuffer(base64.b64decode(grid[key]), np.uint8), bitorder="little")[:count]
    return np.flatnonzero(bits).tolist()


_RUN_SCENE = """
import {readFileSync} from 'node:fs';
const scene = JSON.parse(readFileSync(0, 'utf8'));
const world = rt.createMapRuntime(scene.bundle, {materialGrid: scene.materialGrid});
const spawn = world.spawnById.get(scene.start);
const actor = rt.createActor(world, spawn.x, spawn.y, spawn.facing);
const routes = rt.traverseRoutes(world, {speed: scene.speed});
const points = scene.points || [];
const valid = points.map(([x, y]) => (rt.isValid(world, x, y) ? 1 : 0));
process.stdout.write(JSON.stringify({snapshot: rt.runtimeSnapshot(world, actor, {ready: true, tick: 0, routes,
  events: [{tick: 0, type: 'arrive', id: spawn.id}], drawOrder: scene.objects.map((o) => o.id)}), valid}));
"""


def run_scene(scene: dict) -> dict:
    node = require_node()
    code = f"import * as rt from {json.dumps(RUNTIME.as_uri())};\n" + _RUN_SCENE
    completed = subprocess.run([node, "--input-type=module", "-e", code], input=json.dumps(scene),
                               capture_output=True, encoding="utf-8", errors="replace", timeout=300, check=False)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


class PreviewCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def built(self, edit=None, *extra, out="out"):
        bundle = make_scene(self.root / "map", edit)
        result = build(bundle, self.root / out, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        html_text = (self.root / out / "preview.html").read_text(encoding="ascii")
        report = json.loads((self.root / out / "preview-qa.json").read_text(encoding="utf-8"))
        return html_text, report, json.loads(result.stdout)

    def assertNothingPublished(self, out: Path):
        self.assertFalse(out.exists())
        self.assertEqual(list(out.parent.glob(f".{out.name}.stage-*")), [], "no stage directory is left behind")


# --------------------------------------------------------------------------- the CLI conventions

class CliConventionTests(PreviewCase):
    def test_help_works_under_cp1252_and_cp950(self):
        assert_cli_help(SKILL, "build_scene_preview")

    def test_refuses_an_existing_output_directory(self):
        bundle = make_scene(self.root / "map")
        out = self.root / "out"
        out.mkdir()
        (out / "keep.txt").write_text("mine", encoding="utf-8")
        result = build(bundle, out)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("error: "), result.stderr)
        self.assertEqual(sorted(path.name for path in out.iterdir()), ["keep.txt"])

    def test_a_failed_qa_gate_publishes_nothing(self):
        bundle = make_scene(self.root / "map")
        out = self.root / "out"
        result = build(bundle, out, "--max-bytes", "1000")
        self.assertEqual(result.returncode, 1)
        self.assertIn("QA failed, nothing published: html-size", result.stderr)
        self.assertNothingPublished(out)

    def test_strict_turns_warnings_into_a_failure_that_publishes_nothing(self):
        def no_art(bundle, root):
            del bundle["objects"][2]["image"]
        bundle = make_scene(self.root / "map", no_art)
        out = self.root / "out"
        result = build(bundle, out, "--strict")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--strict: object-art, build-warnings did not pass", result.stderr)
        self.assertNothingPublished(out)
        relaxed = build(bundle, self.root / "relaxed")
        self.assertEqual(relaxed.returncode, 0, relaxed.stderr)
        self.assertEqual(json.loads(relaxed.stdout)["status"], "warn")

    def test_usage_errors_exit_2(self):
        """D26: argparse's convention for usage errors."""
        bundle = make_scene(self.root / "map")
        result = build(bundle, self.root / "out", "--zoom", "large")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("usage:", result.stderr)
        self.assertIn("error:", result.stderr)
        self.assertNothingPublished(self.root / "out")

    def test_a_failed_verify_publishes_the_report_and_exits_1(self):
        """D26: a published report whose status is fail exits 1 (the browser run is simulated)."""
        bundle = make_scene(self.root / "map")
        out = self.root / "out"
        failed = ({"status": "fail", "reason": "simulated"}, [{"id": "browser-routes", "status": "fail",
                                                               "value": ["exit-east: did not fire"], "threshold": []}])
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(PREVIEW, "run_verify", return_value=failed), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = PREVIEW.main(["--bundle", str(bundle), "--output-dir", str(out), "--verify"])
        self.assertEqual(code, 1)
        summary = json.loads(stdout.getvalue())
        self.assertEqual((summary["status"], summary["verify"], summary["failed"]), ("fail", "fail", ["browser-routes"]))
        self.assertIn("published with status fail (browser-routes)", stderr.getvalue())
        report = json.loads((out / "preview-qa.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "fail")
        assert_valid_contract(report, "map", "scene_preview_qa_v1", skill=SKILL)
        with mock.patch.object(PREVIEW, "run_verify", return_value=failed), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            strict = PREVIEW.main(["--bundle", str(bundle), "--output-dir", str(self.root / "strict"), "--verify",
                                   "--strict"])
        self.assertEqual(strict, 1)
        self.assertNothingPublished(self.root / "strict")


# --------------------------------------------------------------------------- the page

class PageTests(PreviewCase):
    def test_one_self_contained_ascii_page_with_the_runtime_inlined(self):
        html_text, report, summary = self.built()
        self.assertEqual(sorted(path.name for path in (self.root / "out").iterdir()), ["preview-qa.json", "preview.html"])
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["verify"], "not-requested")
        self.assertTrue(html_text.isascii())
        self.assertNotIn("://", html_text)
        self.assertNotIn("@@", html_text, "every template placeholder is filled")
        self.assertEqual(html_text.lower().count("<script"), 1)
        self.assertIn('<script type="module">', html_text)
        runtime = RUNTIME.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
        self.assertIn(runtime, html_text, "map-runtime.mjs is inlined byte for byte (LF line ends)")
        self.assertEqual(report["preview"]["runtime"]["sha256"],
                         hashlib.sha256((runtime + "\n").encode("ascii")).hexdigest())
        self.assertNotIn(str(self.root), html_text)
        self.assertNotIn(self.root.as_posix(), html_text)
        scene, images = page_data(html_text)
        self.assertEqual(scene["start"], "west")
        self.assertEqual(scene["title"], "Test glade")
        self.assertEqual(len(images), 3, "ground, the rendered tile layer and one prop image (deduplicated)")
        for mime, payload in images.values():
            self.assertIn(mime, ("image/png", "image/jpeg", "image/webp", "image/gif"))
            base64.b64decode(payload, validate=True)
        self.assertLessEqual(report["preview"]["bytes"], 16_000_000)
        self.assertEqual(report["preview"]["bytes"], len(html_text))

    def test_output_is_byte_identical_across_runs(self):
        bundle = make_scene(self.root / "map", with_tile_collision)
        for out in ("first", "second"):
            self.assertEqual(build(bundle, self.root / out).returncode, 0)
        for name in ("preview.html", "preview-qa.json"):
            self.assertEqual((self.root / "first" / name).read_bytes(), (self.root / "second" / name).read_bytes(), name)

    def test_tile_layers_are_rendered_from_the_tileset_exactly(self):
        html_text, report, _ = self.built()
        scene, images = page_data(html_text)
        self.assertEqual([layer["kind"] for layer in scene["layers"]], ["image", "image", "objects"])
        tiles = decode(images, scene["layers"][1]["image"])
        self.assertEqual(tiles.shape, (48, 64, 4))
        expected = np.zeros((48, 64, 4), np.uint8)
        expected[:8, :8] = (200, 0, 0, 255)
        expected[:8, 8:16] = (0, 200, 0, 255)
        expected[8:16, :8] = (0, 0, 200, 255)
        expected[8:16, 8:16] = (200, 200, 0, 128)
        np.testing.assert_array_equal(tiles, expected)
        self.assertEqual(report["preview"]["layers"][1], {"name": "deco", "source": "tiles", "size": [64, 48]})

    def test_tile_collision_becomes_world_solids(self):
        """D5, D33: placed tiles' collision (tileset_v1 tiles[].collision, walkable false) is part of the
        blocking set the page walks, exactly as forge_nav reads it (N7)."""
        html_text, report, _ = self.built(with_tile_collision)
        scene, _ = page_data(html_text)
        tiles = [solid for solid in scene["bundle"]["collision"]["solids"] if solid.get("source") == "tiles:deco"]
        blocking = NAV.read_blocking_set(self.root / "map" / "map_bundle.json")
        self.assertEqual(len(tiles), len(blocking.tiles))
        self.assertGreater(len(tiles), 2)
        rects = sorted((s["x"], s["y"], s["w"], s["h"]) for s in tiles if s["shape"] == "rect")
        self.assertIn((8, 0, 8, 8), rects, "tile 1 is walkable: false and has no shapes: its whole cell")
        self.assertTrue(any(s["shape"] == "polygon" and [8, 16] in s["points"] for s in tiles),
                        "tile 3's triangle moved to its cell (col 1, row 1)")
        self.assertEqual(report["preview"]["collision"]["tileSolids"], len(tiles))
        self.assertIn("tile", " ".join(report["notProven"]) + report["method"])
        # Review r2 finding 3: the page says its tile collision is resolved (map-runtime.mjs refuses a tiles layer
        # otherwise), and its tile solids are forge_nav.runtime_inputs', the ones map_nav check writes for games.
        self.assertIs(scene["bundle"]["collision"]["tilesResolved"], True)
        self.assertEqual(tiles, json.loads(json.dumps(NAV.runtime_inputs(blocking)["tileSolids"])))

    def test_objects_are_in_ground_line_order_with_sorty_x_id_ties(self):
        html_text, report, _ = self.built()
        scene, _ = page_data(html_text)
        self.assertEqual([item["id"] for item in scene["objects"]], ["tree-a", "tree-b", "bush"])
        self.assertEqual(report["preview"]["drawOrder"], ["tree-a", "tree-b", "bush"])
        self.assertEqual([item["sortY"] for item in scene["objects"]], [30, 30, 40])
        runtime_objects = {item["id"]: item for item in scene["bundle"]["objects"]}
        self.assertEqual(runtime_objects["bush"]["solid"], False)
        self.assertEqual(runtime_objects["bush"]["scale"], 2)

    def test_material_map_becomes_block_and_one_way_planes(self):
        def materials(bundle, root):
            pixels = np.zeros((6, 8, 4), np.uint8)
            pixels[0, 0] = (59, 93, 201, 255)   # water: liquid, blocks
            pixels[0, 1] = (10, 10, 10, 255)    # rock: solid, blocks
            pixels[0, 2] = (200, 0, 0, 255)     # flowers: decor
            pixels[0, 3] = (0, 120, 255, 255)   # shallows: liquid but walkable
            pixels[0, 4] = (255, 255, 0, 255)   # ledge: one_way (blocks moving down onto it, N11)
            pixels[1, 1] = (59, 93, 201, 0)     # transparent water colour: no material
            png(root / "materials.png", pixels)
            bundle["material_map"]["materials"] = {
                "water": {"class": "liquid", "color": "#3b5dc9"}, "rock": {"class": "solid", "color": [10, 10, 10]},
                "flowers": {"class": "decor", "color": "#c80000"},
                "shallows": {"class": "liquid", "color": "#0078ff", "walkable": True},
                "ledge": {"class": "one_way", "color": "#ffff00"}}
        html_text, report, _ = self.built(materials)
        scene, _ = page_data(html_text)
        grid = scene["materialGrid"]
        self.assertEqual(plane(grid, "bits", 48), [0, 1])
        self.assertEqual(plane(grid, "oneWay", 48), [4])
        self.assertEqual((grid["width"], grid["height"], grid["cellWidth"], grid["cellHeight"]), (8, 6, 8, 8))
        info = report["preview"]["materialMap"]
        self.assertEqual((info["blockedCells"], info["oneWayCells"]), (2, 1))
        self.assertEqual(report["preview"]["collision"]["blockingMaterialClasses"], ["solid", "one_way", "liquid"])
        blocking = NAV.read_blocking_set(self.root / "map" / "map_bundle.json")
        self.assertEqual(grid, NAV.runtime_inputs(blocking)["materialGrid"], "the grid map_nav writes for games")

    def test_palette_material_maps_match_by_index(self):
        def indexed(bundle, root):
            image = Image.new("P", (8, 6), 0)
            image.putpalette([0, 0, 0, 59, 93, 201, 10, 10, 10] + [0] * (256 * 3 - 9))
            image.putpixel((2, 0), 1)
            image.putpixel((3, 0), 2)
            image.info["transparency"] = 0
            image.save(root / "materials.png", transparency=0)
            bundle["material_map"]["materials"] = {"floor": {"class": "decor", "index": 0},
                                                   "deep": {"class": "liquid", "index": 1},
                                                   "rock": {"class": "solid", "index": 2}}
        html_text, report, _ = self.built(indexed)
        scene, _ = page_data(html_text)
        self.assertEqual(plane(scene["materialGrid"], "bits", 48), [2, 3])
        self.assertEqual(report["preview"]["materialMap"]["blockedCells"], 2)

    def test_object_art_is_found_in_the_d6_order(self):
        """objects[].image, then the bundle's props registry, then prop packs by label, then occluder.source."""
        def lookup(bundle, root):
            png(root / "pack" / "tree" / "prop.png", np.full((4, 4, 4), (1, 2, 3, 255)))
            png(root / "pack" / "rock" / "prop.png", np.full((4, 4, 4), (4, 5, 6, 255)))
            png(root / "pack" / "fern" / "prop.png", np.full((4, 4, 4), (7, 7, 7, 255)))
            (root / "pack" / "prop-pack.json").write_text(json.dumps({
                "schema": "generate2dmap.prop_pack.v2", "accepted": [
                    {"label": "tree", "image": "tree/prop.png", "sha256": sha(root / "pack" / "tree" / "prop.png"),
                     "status": "accepted"},
                    {"label": "rock", "image": "rock/prop.png", "status": "accepted"},
                    {"label": "fern", "image": "fern/prop.png", "anchor_px": [2, 4], "status": "accepted",
                     "footprint": {"shape": "ellipse", "width": 2, "depth": 1}}], "rejected": []}),
                encoding="utf-8")
            png(root / "occluders" / "stump.png", np.full((2, 2, 4), (7, 8, 9, 255)))
            png(root / "registry" / "bush.png", np.full((6, 6, 4), (9, 9, 9, 255)))
            bundle["prop_packs"] = [{"manifest": "pack/prop-pack.json"}]
            bundle["props"] = {"bush": {"image": "registry/bush.png", "anchor_px": [3, 6],
                                        "sha256": sha(root / "registry" / "bush.png")},
                               "fern": {"pack": "pack/prop-pack.json", "label": "fern"},
                               "tree": {"image": "registry/bush.png"}, "rock": {"image": "registry/bush.png"},
                               "stump": {"footprint": {"shape": "none"}}, "ghost": {"footprint": {"shape": "none"}}}
            bundle["objects"] = [
                {"id": "a", "prop": "tree", "x": 10, "y": 40, "anchor_px": [2, 4], "image": "props/tree.png"},
                {"id": "b", "prop": "bush", "x": 20, "y": 41},
                {"id": "c", "prop": "stump", "x": 30, "y": 42, "anchor_px": [1, 2],
                 "occluder": {"alphaThreshold": 16, "source": "occluders/stump.png"}},
                {"id": "d", "prop": "fern", "x": 40, "y": 43, "anchor_px": [2, 4]},
                {"id": "e", "prop": "ghost", "x": 50, "y": 44, "anchor_px": [2, 4]}]
        same_pack = self.root / "map" / "pack" / "prop-pack.json"
        html_text, report, summary = self.built(lookup, "--prop-pack", str(same_pack))
        self.assertFalse([w for w in report["warnings"] if "more than one prop pack" in w],
                         "a manifest named twice (flag and bundle) is read once")
        scene, images = page_data(html_text)
        art = {item["id"]: item["art"] for item in scene["objects"]}
        self.assertEqual(art, {"a": "object.image", "b": "props", "c": "occluder.source", "d": "props", "e": "missing"})
        drawn = {item["id"]: item for item in scene["objects"]}
        self.assertEqual(drawn["b"]["anchor"], [3, 6], "the registry item's anchor when the object has none")
        self.assertEqual(decode(images, drawn["d"]["image"])[0, 0].tolist(), [7, 7, 7, 255], "a pack + label item")
        self.assertEqual(report["preview"]["objectArt"], {"object.image": 1, "props": 2, "prop-pack": 0,
                                                          "occluder.source": 1, "missing": ["e"]})
        self.assertEqual(scene["bundle"]["props"]["fern"], {"footprint": {"shape": "ellipse", "width": 2, "depth": 1}},
                         "the runtime gets the resolved registry (N6)")
        self.assertEqual(summary["status"], "warn")

    def test_flip_x_objects_are_drawn_mirrored_and_their_footprint_follows(self):
        def flipped(bundle, root):
            bundle["objects"][2].update(flip_x=True, solid=True, footprint={"shape": "rect", "width": 4, "depth": 2,
                                                                            "offset": [3, 0]})
        html_text, _, _ = self.built(flipped)
        scene, _ = page_data(html_text)
        drawn = {item["id"]: item for item in scene["objects"]}
        self.assertIs(drawn["bush"]["flipX"], True)
        self.assertIs(drawn["tree-a"]["flipX"], False)
        runtime = {item["id"]: item for item in scene["bundle"]["objects"]}
        self.assertIs(runtime["bush"]["flip_x"], True)
        self.assertIn("ctx.scale(-1, 1)", html_text, "the page mirrors the art around the anchor x")

    def test_data_cannot_close_the_script_or_spell_a_url(self):
        hostile = '</script><img src=x onerror=alert(1)><!--'

        def inject(bundle, root):
            bundle["name"] = hostile
            bundle["objects"][0]["id"] = hostile
            bundle["portals"][0]["to"] = "https://example.invalid/x:spawn"
        html_text, report, _ = self.built(inject)
        self.assertEqual(html_text.lower().count("</script"), 1)
        self.assertNotIn("<img", html_text.lower())
        self.assertNotIn("://", html_text)
        self.assertNotIn("<!--", html_text)
        scene, _ = page_data(html_text)
        self.assertEqual(scene["title"], hostile, "the escapes read back to the original text")
        self.assertEqual(scene["bundle"]["portals"][0]["to"], "https://example.invalid/x:spawn")
        self.assertIn("&lt;/script&gt;", html_text, "the title markup is HTML-escaped")
        self.assertEqual(report["status"], "pass")

    def test_non_ascii_names_stay_ascii_in_the_page(self):
        def named(bundle, root):
            bundle["name"] = "\u82d4\u5f91 glade"
            bundle["spawns"][0]["id"] = "\u5165\u53e3"
        html_text, _, _ = self.built(named)
        self.assertTrue(html_text.isascii())
        self.assertIn(f"&#{0x82d4};&#{0x5f91}; glade", html_text)
        scene, _ = page_data(html_text)
        self.assertEqual(scene["start"], "\u5165\u53e3")


class ExternalReferenceScanTests(unittest.TestCase):
    def skeleton(self, script="", markup=""):
        return f"<!doctype html><html><head>{markup}</head><body><script type=\"module\">{script}</script></body></html>"

    def test_every_kind_of_outside_reference_is_found(self):
        cases = [
            ("script", 'const a = "https" + "://x";'), ("script", "location.href = 'http:x';"),
            ("script", "image.src = '//cdn.example/x.png';"), ("script", "fetch(path);"),
            ("script", "new Worker(path);"), ("script", "import('x.mjs');"), ("script", "node.innerHTML = x;"),
            ("script", "document.createElement('script');"), ("script", "const c = 'url(' + x;"),
            ("markup", '<img alt="x">'), ("markup", '<link rel="stylesheet">'), ("markup", '<a href="#x">'),
            ("markup", "<style>@import 'x.css';</style>"), ("markup", '<meta http-equiv="refresh">'),
            ("markup", '<div data="x"></div>'),
        ]
        for where, text in cases:
            with self.subTest(text=text):
                page = self.skeleton(**{where: text})
                self.assertTrue(PREVIEW.external_references(page, [], "{}", "{}"), text)

    def test_ordinary_code_and_markup_pass(self):
        page = self.skeleton(script="if (k < portal.radius && a.x < source.y) image.src = 'data' + mime;",
                             markup='<canvas id="view" tabindex="0"></canvas><select id="spawn"></select>')
        self.assertEqual(PREVIEW.external_references(page, ["a &lt;b&gt; c&#58;"], '{"a":"b\\u003a"}',
                                                     '{"i0":["image/png","QUJD"]}'), [])

    def test_data_and_texts_are_checked_on_their_own(self):
        page = self.skeleton()
        self.assertTrue(PREVIEW.external_references(page, [], '{"a":"<x>"}', "{}"))
        self.assertTrue(PREVIEW.external_references(page, [], "{}", '{"i0":["text/html","QUJD"]}'))
        self.assertTrue(PREVIEW.external_references(page, [], "{}", '{"i0":["image/png","not base64!"]}'))
        self.assertTrue(PREVIEW.external_references(page, ["<b>"], "{}", "{}"), "texts must arrive escaped")
        self.assertTrue(PREVIEW.external_references(page, ["x://y"], "{}", "{}"))

    def test_script_json_escapes_markup_and_colons_only_inside_strings(self):
        value = {"a:b": ["</script>", "x&y", "data:image/png"], "n": 1.5}
        text = PREVIEW.script_json(value)
        self.assertNotRegex(text, r"[<>&]")
        self.assertNotIn("data:", text)
        self.assertEqual(json.loads(text), value)
        self.assertEqual(text.count(":"), 2, "the two separators between keys and values stay")


# --------------------------------------------------------------------------- inputs

class InputTests(PreviewCase):
    def failing(self, edit, message, *extra):
        bundle = make_scene(self.root / "map", edit)
        out = self.root / "out"
        result = build(bundle, out, *extra)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(message, result.stderr)
        self.assertTrue(result.stderr.startswith("error: "), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNothingPublished(out)

    def test_sha256_mismatch_fails(self):
        def stale(bundle, root):
            bundle["objects"][0]["image_sha256"] = "0" * 64
        self.failing(stale, "sha256 mismatch for tree.png")

    def test_references_must_be_relative_posix_paths_to_existing_files(self):
        for value, message in (("C:/art/x.png", "must be a relative POSIX path"),
                               ("https://example.invalid/x.png", "must be a relative POSIX path"),
                               ("props\\tree.png", "must be a relative POSIX path"),
                               ("/abs/x.png", "must be a relative POSIX path"),
                               ("props/missing.png", "names a missing file")):
            with self.subTest(value=value):
                def edit(bundle, root, value=value):
                    bundle["layers"][0]["image"] = value
                self.failing(edit, message)

    def test_bundle_shape_errors_name_the_field(self):
        cases = [
            (lambda b, r: b.update(schema="generate2dmap.map_bundle.v3"), "is not a map bundle"),
            (lambda b, r: b.pop("collision"), "The bundle needs collision"),
            (lambda b, r: b.update(spawns=[]), "The bundle has no spawns"),
            (lambda b, r: b["collision"].update(actorRadius=float("nan")), "is not valid JSON"),
            (lambda b, r: b["collision"].update(actorRadius="1"), "collision.actorRadius must be a finite number"),
            (lambda b, r: b["portals"][0].pop("radius"), "intent portals need travelDirection and radius"),
            (lambda b, r: b["portals"][0].update(circle=[1, 2, 3]), "needs exactly one of rect"),
            (lambda b, r: b["objects"][0]["footprint"].update(shape="blob"), "footprint.shape must be ellipse, rect or none"),
            (lambda b, r: b["objects"][0]["footprint"].update(basis="metres"), "basis must be prop_px, world_px"),
            (lambda b, r: b["objects"][0].update(flip_x="yes"), "flip_x must be true or false"),
            (lambda b, r: b["collision"]["solids"].append({"shape": "polygon", "points": [[0, 0], [1, 1]]}),
             "collision.solids[1].points must list at least 3"),
            (lambda b, r: b["layers"][1].update(tileset="nope"), "layers[1].tileset 'nope' is not in tilesets"),
            (lambda b, r: b.update(tile_size=16), "the bundle tile_size is 16"),
            (lambda b, r: b["spawns"].append({"id": "west", "x": 1, "y": 1}), "Duplicate spawn id 'west'"),
            (lambda b, r: b.update(props={"tree": {"image": "props/tree.png"}}, objects=[
                {"id": "x", "prop": "ghost", "x": 5, "y": 5, "anchor_px": [0, 0]}]), "unknown prop 'ghost'"),
        ]
        for edit, message in cases:
            with self.subTest(message=message):
                self.failing(edit, message)

    def test_unknown_start_spawn_fails(self):
        self.failing(None, "--spawn 'nowhere' is not a spawn id", "--spawn", "nowhere")

    def test_tile_indices_outside_the_tileset_fail(self):
        def big(bundle, root):
            (root / "deco.csv").write_text("0,9" + ",-1" * 6 + "\n" + "\n".join(TILE_ROWS[1:]) + "\n", encoding="utf-8")
        self.failing(big, "tile index 9 is outside tileset deco (4 tiles)")

    def test_tile_indices_below_minus_one_fail(self):
        def negative(bundle, root):
            (root / "deco.csv").write_text("0,-2" + ",-1" * 6 + "\n" + "\n".join(TILE_ROWS[1:]) + "\n", encoding="utf-8")
        self.failing(negative, "-2' is not a tile index (-1 or null is an empty cell)")

    def test_tile_grids_must_cover_the_world_as_forge_nav_reads_them(self):
        def partial(bundle, root):
            (root / "deco.csv").write_text("0,1,-1\n2,3,-1\n", encoding="utf-8")
        self.failing(partial, "collision (forge_nav): tiles layer 'deco' covers 24x16 px, the world is 64x48")

    def test_ambiguous_material_colours_fail(self):
        def twice(bundle, root):
            bundle["material_map"]["materials"]["sea"] = {"class": "liquid", "color": [59, 93, 201]}
        self.failing(twice, "material colors must be unique")

    def test_unclassified_material_pixels_fail(self):
        """N8: an opaque pixel that matches no material refuses the bundle (forge_nav, map_nav and the preview)."""
        def stray(bundle, root):
            pixels = np.asarray(Image.open(root / "materials.png").convert("RGBA")).copy()
            pixels[1, 0] = (1, 2, 3, 255)
            png(root / "materials.png", pixels)
        self.failing(stray, "1 pixel(s) match no material (first at x=0, y=1)")

    def test_material_maps_must_divide_the_world_into_whole_squares(self):
        def fractional(bundle, root):
            png(root / "materials.png", np.zeros((5, 7, 4), np.uint8))
        self.failing(fractional, "does not divide the 64x48 world into whole squares")

    def test_v1_bundles_with_world_and_collision_build(self):
        def legacy(bundle, root):
            bundle["schema"] = "generate2dmap.map_bundle.v1"
            bundle["objects"][1]["footprint"] = {"type": "ellipse", "rx": 2, "ry": 1}
        html_text, report, _ = self.built(legacy)
        self.assertEqual(report["status"], "pass")
        scene, _ = page_data(html_text)
        runtime = {item["id"]: item for item in scene["bundle"]["objects"]}
        self.assertEqual(runtime["tree-a"]["footprint"], {"shape": "ellipse", "width": 4, "depth": 2, "offset": [0, 0]},
                         "a v1 footprint is upgraded the way forge_nav and map_bundle read it")


# --------------------------------------------------------------------------- contracts

class ContractTests(PreviewCase):
    def test_fixture_bundle_is_a_valid_map_bundle_v2(self):
        bundle = json.loads(make_scene(self.root / "map", with_tile_collision).read_text(encoding="utf-8"))
        assert_valid_contract(bundle, "map", "map_bundle_v2", skill=SKILL)
        manifest = json.loads((self.root / "map" / "tiles" / "deco.tileset.json").read_text(encoding="utf-8"))
        assert_valid_contract(manifest, "map", "tileset_v1", skill=SKILL)
        bundle["objects"][0]["image"] = "C:/abs.png"
        self.assertTrue(schema_errors(bundle, "map_bundle_v2"), "the image field is a relPath")

    def test_report_is_a_qa_envelope_and_a_scene_preview_qa(self):
        _, report, _ = self.built()
        assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
        assert_valid_contract(report, "map", "scene_preview_qa_v1", skill=SKILL)
        self.assertEqual(report["schema"], "generate2dmap.scene_preview_qa.v1")
        self.assertEqual(report["tool"], {"name": "build_scene_preview", "version": "0.4.0"}, "D29")
        self.assertTrue(report["notProven"])
        self.assertEqual(report["outputs"][0]["path"], "preview.html")
        self.assertEqual(report["outputs"][0]["sha256"], report["preview"]["sha256"])
        paths = [item["path"] for item in report["inputs"]]
        self.assertEqual(paths[0], "../map/map_bundle.json")
        self.assertIn("../map/tiles/atlas.png", paths)
        self.assertEqual(report["preview"]["hashes"], {"checked": 2, "unchecked": 6})

    @pytest.mark.node
    def test_runtime_snapshot_and_route_check_match_the_requested_contract(self):
        html_text, _, _ = self.built()
        scene, _ = page_data(html_text)
        snapshot = run_scene(scene)["snapshot"]
        assert_valid_contract(snapshot, "map", "scene_snapshot_v1", skill=SKILL)
        self.assertTrue(snapshot["routes"]["ok"], json.dumps(snapshot["routes"]["results"]))
        self.assertEqual([item["target"] for item in snapshot["routes"]["results"]],
                         ["exit-east", "sign", "well.approach[0]", "well.slot[0]"])
        broken = copy.deepcopy(snapshot)
        broken["routes"]["results"][0]["kind"] = "door"
        self.assertTrue(schema_errors(broken, "scene_snapshot_v1"))


# --------------------------------------------------------------------------- agreement with forge_nav (D1, D2, D4)

def _forge_nav_verdicts(bundle_path: Path) -> tuple[dict, dict]:
    """forge_nav's N14 answers for a bundle file: ({target: reachable}, {start: valid and joined})."""
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    model = NAV.read_blocking_set(bundle_path).model()
    starts = {s["id"]: (s["x"], s["y"]) for s in bundle.get("spawns", [])}
    navigation = NAV.navigate(model, list(starts.values()))
    verdicts = {}
    for item in bundle.get("interactions", []):
        point = (item["x"], item["y"])
        verdicts[item["id"]] = (navigation.point_target(point) if "reach" not in item
                                else navigation.reach_target(point, item["reach"])).reachable
    for name, anchor in (bundle.get("anchors") or {}).items():
        for k, slot in enumerate(anchor.get("slots") or []):
            verdicts[f"{name}.slot[{k}]"] = navigation.point_target(slot).reachable
        approach = anchor.get("approach") or []
        for k, point in enumerate([approach] if approach and not isinstance(approach[0], list) else approach):
            verdicts[f"{name}.approach[{k}]"] = navigation.point_target(point).reachable
    for portal in bundle.get("portals", []):
        verdicts[portal["id"]] = navigation.exit_target(NAV.Trigger.from_portal(portal), portal.get("activation", "crossing"),
                                                        portal.get("radius", 0)).reachable
    return verdicts, {name: reach.reachable for name, reach in zip(starts, navigation.starts)}


@pytest.mark.node
class ForgeNavAgreementTests(PreviewCase):
    def test_route_check_agrees_with_forge_nav_on_the_engine_export_fixture(self):
        """The Wave B review's blocking case: on B14's synthetic bundle the old preview said every route was fine
        while map_nav, counting tile collision, found a blocked spawn and unreachable targets."""
        bundle_path, _ = build_engine_bundle(self.root / "engine")
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["spawns"].append({"id": "spawn-east", "x": 140, "y": 48, "facing": "west"})
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        result = build(bundle_path, self.root / "out")
        self.assertEqual(result.returncode, 0, result.stderr)
        scene, _ = page_data((self.root / "out" / "preview.html").read_text(encoding="ascii"))
        routes = run_scene(scene)["snapshot"]["routes"]
        verdicts, starts = _forge_nav_verdicts(bundle_path)
        self.assertEqual({item["target"]: item["reachable"] for item in routes["results"]}, verdicts)
        self.assertEqual({item["id"]: item["valid"] and item["reachableCells"] > 0 for item in routes["spawns"]}, starts)
        self.assertIn(False, verdicts.values(), "the fixture has unreachable targets (tile collision blocks them)")
        self.assertFalse(routes["ok"])
        self.assertGreater(len([s for s in scene["bundle"]["collision"]["solids"]
                                if str(s.get("source", "")).startswith("tiles:")]), 0)

    def test_page_validity_agrees_with_forge_nav_with_tiles_footprints_and_materials(self):
        def everything(bundle, root):
            with_tile_collision(bundle, root)
            bundle["objects"][2].update(solid=True, flip_x=True, footprint={"shape": "rect", "width": 4, "depth": 2,
                                                                            "offset": [2, 0], "rotate": 30})
        html_text, _, _ = self.built(everything)
        scene, _ = page_data(html_text)
        xs, ys = np.meshgrid(np.arange(-2, 66, 0.5), np.arange(-2, 50, 0.5))
        points = np.column_stack([xs.ravel(), ys.ravel()])
        valid = np.array(run_scene({**scene, "points": points.tolist()})["valid"], bool)
        model = NAV.read_blocking_set(self.root / "map" / "map_bundle.json").model()
        expected = model.valid(points[:, 0], points[:, 1])
        mismatch = np.flatnonzero(valid != expected)
        self.assertLessEqual(mismatch.size, 2, f"only points on the rotated footprint's edge may differ: "
                                               f"{points[mismatch[:5]].tolist()}")
        self.assertGreater((~expected).sum(), 100)


# --------------------------------------------------------------------------- --verify

def _playwright_resolvable(node: str, cwd: Path, env: dict) -> bool:
    probe = subprocess.run([node, "-e", "require.resolve('playwright')"], cwd=cwd, env=env, capture_output=True,
                           timeout=60, check=False)
    return probe.returncode == 0


class VerifyTests(PreviewCase):
    def test_verify_prints_skipped_without_node(self):
        bundle = make_scene(self.root / "map")
        env = {**os.environ, "PATH": str(Path(sys.executable).parent)}
        if shutil.which("node", path=env["PATH"]):
            self.skipTest("node sits next to the Python interpreter")
        result = build(bundle, self.root / "out", "--verify", "--strict", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verify: SKIPPED (node is not on PATH)", result.stderr)
        self.assertEqual(json.loads(result.stdout)["verify"], "SKIPPED")
        report = json.loads((self.root / "out" / "preview-qa.json").read_text(encoding="utf-8"))
        self.assertIn({"id": "browser-verify", "status": "skipped", "value": "node missing", "threshold": None},
                      report["checks"])
        self.assertEqual(report["status"], "pass", "a skipped browser run is not a failure, even with --strict")

    @pytest.mark.node
    def test_verify_prints_skipped_without_playwright(self):
        node = require_node()
        empty = self.root / "no-modules"
        empty.mkdir()
        env = {**os.environ, "NODE_PATH": str(empty)}
        if _playwright_resolvable(node, empty, env):
            self.skipTest("playwright is installed globally")
        bundle = make_scene(self.root / "map")
        result = run_cli([SCRIPT, "--bundle", bundle, "--output-dir", self.root / "out", "--verify"], cwd=empty,
                         env=env, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verify: SKIPPED (the playwright npm package was not found", result.stderr)

    @pytest.mark.node
    def test_verify_walks_the_page_in_headless_chromium(self):
        """Runs only where node can require playwright (a project install or NODE_PATH) and Chromium is installed."""
        node = require_node()
        if not _playwright_resolvable(node, Path.cwd(), dict(os.environ)):
            self.skipTest("the playwright npm package is not resolvable; set NODE_PATH to run this test")
        bundle = make_scene(self.root / "map")
        out = self.root / "out"
        result = build(bundle, out, "--verify")
        if json.loads(result.stdout or "{}").get("verify") == "SKIPPED":
            self.skipTest(result.stderr.strip())
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        report = json.loads((out / "preview-qa.json").read_text(encoding="utf-8"))
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(summary["verify"], "pass", json.dumps(report["checks"], indent=1))
        for name in ("browser-page-errors", "browser-network-requests", "browser-assets-loaded",
                     "browser-keyboard-walk", "browser-routes", "browser-y-sort"):
            self.assertEqual(checks[name]["status"], "pass", name)
        snapshot = json.loads((out / "scene-snapshot.json").read_text(encoding="utf-8"))
        assert_valid_contract(snapshot, "map", "scene_snapshot_v1", skill=SKILL)
        self.assertTrue(snapshot["routes"]["ok"])
        for name in ("preview-screen.png", "preview-debug.png"):
            with Image.open(out / name) as image:
                self.assertGreater(image.width, 100)
        self.assertEqual(sorted(item["path"] for item in report["outputs"]),
                         ["preview-debug.png", "preview-screen.png", "preview.html", "scene-snapshot.json"])
        assert_valid_contract(report, "map", "scene_preview_qa_v1", skill=SKILL)


if __name__ == "__main__":
    unittest.main()
