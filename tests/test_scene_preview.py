"""build_scene_preview.py (B17-T2): a deterministic, self-contained, at most 16 MB preview.html.

Fixtures are synthetic and written per test. Contracts are checked against the generate2dmap vendored
schemas, which hold the additions this module requested (handoff/B17-map-scene-preview.md section 5).
"""
from __future__ import annotations

import base64
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

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, contract_errors, load_script,
                             require_node, run_cli, script_path)

SKILL = "generate2dmap"
SCRIPT = script_path(SKILL, "build_scene_preview")
PREVIEW = load_script(SKILL, "build_scene_preview")
RUNTIME = SKILLS_DIR / SKILL / "references" / "runtime" / "map-runtime.mjs"


def patched_errors(instance, name: str) -> list[str]:
    """Validation errors against the vendored map.schema.json, which holds the section 5 additions."""
    return contract_errors(instance, "map", name, skill=SKILL)


# --------------------------------------------------------------------------- fixtures

def png(path: Path, pixels) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.asarray(pixels, np.uint8)).save(path)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    (root / "deco.csv").write_text("0,1,-1,\n2,3,-1\n", encoding="utf-8")
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


# --------------------------------------------------------------------------- the three CLI tests

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
        bundle = make_scene(self.root / "map")
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

    def test_objects_are_in_ground_line_order_with_sorty_x_id_ties(self):
        html_text, report, _ = self.built()
        scene, _ = page_data(html_text)
        self.assertEqual([item["id"] for item in scene["objects"]], ["tree-a", "tree-b", "bush"])
        self.assertEqual(report["preview"]["drawOrder"], ["tree-a", "tree-b", "bush"])
        self.assertEqual([item["sortY"] for item in scene["objects"]], [30, 30, 40])
        runtime_objects = {item["id"]: item for item in scene["bundle"]["objects"]}
        self.assertEqual(runtime_objects["bush"]["solid"], False)
        self.assertEqual(runtime_objects["bush"]["scale"], 2)

    def test_material_map_becomes_a_blocked_cell_grid(self):
        def materials(bundle, root):
            pixels = np.zeros((6, 8, 4), np.uint8)
            pixels[0, 0] = (59, 93, 201, 255)   # water: liquid, blocks
            pixels[0, 1] = (10, 10, 10, 255)    # rock: solid, blocks
            pixels[0, 2] = (200, 0, 0, 255)     # flowers: decor
            pixels[0, 3] = (0, 120, 255, 255)   # shallows: liquid but walkable
            pixels[0, 4] = (255, 255, 0, 255)   # ledge: one_way, top-down walkers pass
            pixels[1, 0] = (1, 2, 3, 255)       # unclassified
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
        bits = np.unpackbits(np.frombuffer(base64.b64decode(grid["bits"]), np.uint8), bitorder="little")[:48]
        self.assertEqual(np.flatnonzero(bits).tolist(), [0, 1])
        self.assertEqual((grid["width"], grid["height"], grid["cellWidth"], grid["cellHeight"]), (8, 6, 8.0, 8.0))
        self.assertEqual(report["preview"]["materialMap"]["unclassified"], 1)
        self.assertIn("material_map: 1 opaque pixels match no material and do not block.", report["warnings"])

    def test_palette_material_maps_match_by_index(self):
        def indexed(bundle, root):
            image = Image.new("P", (8, 6), 0)
            image.putpalette([0, 0, 0, 59, 93, 201, 10, 10, 10] + [0] * (256 * 3 - 9))
            image.putpixel((2, 0), 1)
            image.putpixel((3, 0), 2)
            image.info["transparency"] = 0
            image.save(root / "materials.png", transparency=0)
            bundle["material_map"]["materials"] = {"deep": {"class": "liquid", "index": 1},
                                                   "rock": {"class": "solid", "index": 2}}
        html_text, report, _ = self.built(indexed)
        scene, _ = page_data(html_text)
        bits = np.unpackbits(np.frombuffer(base64.b64decode(scene["materialGrid"]["bits"]), np.uint8), bitorder="little")
        self.assertEqual(np.flatnonzero(bits[:48]).tolist(), [2, 3])
        self.assertEqual(report["preview"]["materialMap"]["unclassified"], 0, "index 0 is transparent")

    def test_object_art_is_found_by_image_then_prop_pack_then_occluder(self):
        def lookup(bundle, root):
            png(root / "pack" / "tree" / "prop.png", np.full((4, 4, 4), (1, 2, 3, 255)))
            png(root / "pack" / "rock" / "prop.png", np.full((4, 4, 4), (4, 5, 6, 255)))
            (root / "pack" / "prop-pack.json").write_text(json.dumps({
                "schema": "generate2dmap.prop_pack.v2", "accepted": [
                    {"label": "tree", "image": "tree/prop.png", "sha256": sha(root / "pack" / "tree" / "prop.png"),
                     "status": "accepted"},
                    {"label": "rock", "image": "rock/prop.png", "status": "accepted"}], "rejected": []}),
                encoding="utf-8")
            png(root / "occluders" / "stump.png", np.full((2, 2, 4), (7, 8, 9, 255)))
            bundle["prop_packs"] = [{"manifest": "pack/prop-pack.json"}]
            bundle["objects"] = [
                {"id": "a", "prop": "tree", "x": 10, "y": 40, "anchor_px": [2, 4], "image": "props/tree.png"},
                {"id": "b", "prop": "tree", "x": 20, "y": 41, "anchor_px": [2, 4]},
                {"id": "c", "prop": "stump", "x": 30, "y": 42, "anchor_px": [1, 2],
                 "occluder": {"alphaThreshold": 16, "source": "occluders/stump.png"}},
                {"id": "d", "prop": "rock", "x": 40, "y": 43, "anchor_px": [2, 4]},
                {"id": "e", "prop": "ghost", "x": 50, "y": 44, "anchor_px": [2, 4]}]
        same_pack = self.root / "map" / "pack" / "prop-pack.json"
        html_text, report, summary = self.built(lookup, "--prop-pack", str(same_pack))
        self.assertFalse([w for w in report["warnings"] if "more than one prop pack" in w],
                         "a manifest named twice (flag and bundle) is read once")
        scene, images = page_data(html_text)
        art = {item["id"]: item["art"] for item in scene["objects"]}
        self.assertEqual(art, {"a": "object.image", "b": "prop-pack", "c": "occluder.source", "d": "prop-pack",
                               "e": "missing"})
        self.assertEqual(decode(images, scene["objects"][1]["image"])[0, 0].tolist(), [1, 2, 3, 255])
        self.assertEqual(report["preview"]["objectArt"], {"object.image": 1, "prop-pack": 2, "occluder.source": 1,
                                                          "missing": ["e"]})
        self.assertEqual(summary["status"], "warn")

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
            (lambda b, r: b["collision"].update(actorRadius=float("nan")), "collision.actorRadius must be a finite number"),
            (lambda b, r: b["portals"][0].pop("radius"), "intent portals need travelDirection and radius"),
            (lambda b, r: b["portals"][0].update(circle=[1, 2, 3]), "needs exactly one of rect"),
            (lambda b, r: b["objects"][0]["footprint"].update(shape="blob"), "footprint.shape must be ellipse, rect or none"),
            (lambda b, r: b["collision"]["solids"].append({"shape": "polygon", "points": [[0, 0], [1, 1]]}),
             "collision.solids[1].points must list at least 3"),
            (lambda b, r: b["layers"][1].update(tileset="nope"), "layers[1].tileset 'nope' is not in tilesets"),
            (lambda b, r: b.update(tile_size=16), "the bundle tile_size is 16"),
            (lambda b, r: b["spawns"].append({"id": "west", "x": 1, "y": 1}), "Duplicate spawn id 'west'"),
        ]
        for edit, message in cases:
            with self.subTest(message=message):
                self.failing(edit, message)

    def test_unknown_start_spawn_fails(self):
        self.failing(None, "--spawn 'nowhere' is not a spawn id", "--spawn", "nowhere")

    def test_tile_indices_outside_the_tileset_fail(self):
        def big(bundle, root):
            (root / "deco.csv").write_text("0,9\n", encoding="utf-8")
        self.failing(big, "tile index 9 is outside tileset deco (4 tiles)")

    def test_ambiguous_material_colours_fail(self):
        def twice(bundle, root):
            bundle["material_map"]["materials"]["sea"] = {"class": "liquid", "color": [59, 93, 201]}
        self.failing(twice, "pixels match more than one material")

    def test_v1_bundles_with_world_and_collision_build(self):
        def legacy(bundle, root):
            bundle["schema"] = "generate2dmap.map_bundle.v1"
        _, report, _ = self.built(legacy)
        self.assertEqual(report["status"], "pass")


# --------------------------------------------------------------------------- contracts

class ContractTests(PreviewCase):
    def test_fixture_bundle_is_a_valid_map_bundle_v2(self):
        bundle = json.loads(make_scene(self.root / "map").read_text(encoding="utf-8"))
        assert_valid_contract(bundle, "map", "map_bundle_v2", skill=SKILL)
        self.assertEqual(patched_errors(bundle, "map_bundle_v2"), [])
        bundle["objects"][0]["image"] = "C:/abs.png"
        self.assertTrue(patched_errors(bundle, "map_bundle_v2"), "the requested image field is a relPath")

    def test_report_is_a_qa_envelope_and_a_scene_preview_qa(self):
        _, report, _ = self.built()
        assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
        self.assertEqual(patched_errors(report, "scene_preview_qa_v1"), [])
        self.assertEqual(report["schema"], "generate2dmap.scene_preview_qa.v1")
        self.assertEqual(report["tool"], {"name": "build_scene_preview", "version": "1.0"})
        self.assertTrue(report["notProven"])
        self.assertEqual(report["outputs"][0]["path"], "preview.html")
        self.assertEqual(report["outputs"][0]["sha256"], report["preview"]["sha256"])
        paths = [item["path"] for item in report["inputs"]]
        self.assertEqual(paths[0], "../map/map_bundle.json")
        self.assertIn("../map/tiles/atlas.png", paths)
        self.assertEqual(report["preview"]["hashes"], {"checked": 2, "unchecked": 6})

    @pytest.mark.node
    def test_runtime_snapshot_and_route_check_match_the_requested_contract(self):
        node = require_node()
        html_text, _, _ = self.built()
        scene, _ = page_data(html_text)
        code = (
            f"import * as rt from {json.dumps(RUNTIME.as_uri())};\n"
            "import {readFileSync} from 'node:fs';\n"
            "const scene = JSON.parse(readFileSync(0, 'utf8'));\n"
            "const world = rt.createMapRuntime(scene.bundle, {materialGrid: scene.materialGrid});\n"
            "const spawn = world.spawnById.get(scene.start);\n"
            "const actor = rt.createActor(world, spawn.x, spawn.y, spawn.facing);\n"
            "const routes = rt.traverseRoutes(world, {speed: scene.speed});\n"
            "process.stdout.write(JSON.stringify(rt.runtimeSnapshot(world, actor, {ready: true, tick: 0, routes,"
            " events: [{tick: 0, type: 'arrive', id: spawn.id}], drawOrder: scene.objects.map((o) => o.id)})));\n")
        completed = subprocess.run([node, "--input-type=module", "-e", code], input=json.dumps(scene),
                                   capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        snapshot = json.loads(completed.stdout)
        self.assertEqual(patched_errors(snapshot, "scene_snapshot_v1"), [])
        self.assertTrue(snapshot["routes"]["ok"], json.dumps(snapshot["routes"]["results"]))
        self.assertEqual([item["target"] for item in snapshot["routes"]["results"]],
                         ["exit-east", "sign", "well.approach[0]", "well.slot[0]"])
        broken = copy.deepcopy(snapshot)
        broken["routes"]["results"][0]["kind"] = "door"
        self.assertTrue(patched_errors(broken, "scene_snapshot_v1"))


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
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        if summary["verify"] == "SKIPPED":
            self.skipTest(result.stderr.strip())
        report = json.loads((out / "preview-qa.json").read_text(encoding="utf-8"))
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(summary["verify"], "pass", json.dumps(report["checks"], indent=1))
        for name in ("browser-page-errors", "browser-network-requests", "browser-assets-loaded",
                     "browser-keyboard-walk", "browser-routes", "browser-y-sort"):
            self.assertEqual(checks[name]["status"], "pass", name)
        snapshot = json.loads((out / "scene-snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(patched_errors(snapshot, "scene_snapshot_v1"), [])
        self.assertTrue(snapshot["routes"]["ok"])
        for name in ("preview-screen.png", "preview-debug.png"):
            with Image.open(out / name) as image:
                self.assertGreater(image.width, 100)
        self.assertEqual(sorted(item["path"] for item in report["outputs"]),
                         ["preview-debug.png", "preview-screen.png", "preview.html", "scene-snapshot.json"])
        self.assertEqual(patched_errors(report, "scene_preview_qa_v1"), [])


if __name__ == "__main__":
    unittest.main()
