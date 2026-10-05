"""Tests for validate_layout.py: layout.v1 side-scroll grammar against the controller physics (B14-T4).

Rules come from the owner's side-scroller stages (game-opus55 st_ch2b.js L131-165, action.js L153-206): a
3-row deck lets a standing body sink through, props stand on their own column's ground, dunes stay under
the slope limit, and an arena is at least the view plus the camera travel.
"""
from __future__ import annotations

import json
import math
import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, run_cli,
                             script_path)


LAYOUT = load_script("generate2dmap", "validate_layout")
SCRIPT = script_path("generate2dmap", "validate_layout")
SKILL = "generate2dmap"
PHYSICS = {"jumpHeight": 72, "jumpDistance": 120, "maxSlopeDeg": 35, "stepUp": 8, "colliderSubstep": 3}

# The $def this module asks integration to add to map.schema.json (handoff section 5), applied in memory.
REQUESTED_MAP_DEFS = {
    "layout_validation_v1": {
        "description": "validate_layout.py report: standable spans, ledges and steep faces, gaps with their jump "
                       "numbers, prop support, arena widths, reachability, and the QA envelope.",
        "type": "object",
        "required": ["schema", "tool", "status", "extent", "physics", "spans", "gaps", "props", "qa"],
        "properties": {
            "schema": {"const": "generate2dmap.layout_validation.v1"},
            "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
            "status": {"enum": ["pass", "warn", "fail"]},
            "extent": {"type": "object", "required": ["x0", "x1"],
                       "properties": {"x0": {"type": "integer"}, "x1": {"type": "integer"}}},
            "physics": {"type": "object", "required": ["jumpHeight", "jumpDistance", "maxSlopeDeg", "stepUp",
                                                       "minDeckThickness", "groundTolerance"]},
            "spans": {"type": "array", "items": {
                "type": "object", "required": ["id", "kind", "x0", "x1", "reachable"],
                "properties": {"id": {"type": "string"}, "kind": {"enum": ["ground", "deck"]},
                               "x0": {"type": "integer"}, "x1": {"type": "integer"},
                               "top_min": {"type": "number"}, "top_max": {"type": "number"},
                               "reachable": {"type": "boolean"}}}},
            "pieces": {"type": "array", "items": {"type": "object", "required": ["kind"],
                                                  "properties": {"kind": {"enum": ["ledge", "steep"]}}}},
            "gaps": {"type": "array", "items": {
                "type": "object", "required": ["x0", "x1", "width", "kind", "crossable"],
                "properties": {"kind": {"enum": ["gap", "hazard"]}, "crossable": {"type": ["boolean", "null"]},
                               "direct": {"anyOf": [{"type": "null"}, {"type": "object",
                                                                       "required": ["distance", "rise", "reach",
                                                                                    "feasible"]}]}}}},
            "props": {"type": "array", "items": {
                "type": "object", "required": ["id", "x", "y", "status"],
                "properties": {"status": {"enum": ["supported", "floating", "sunk", "allowed"]}}}},
            "arenas": {"type": "array", "items": {"type": "object", "required": ["arena", "viewport", "needed",
                                                                                 "status"]}},
            "reachability": {"type": "object"},
            "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
        },
    },
}


# Additions to the existing layout_v1 contract that validate_layout.py reads (handoff section 5).
_POINT = {"$ref": "common.schema.json#/$defs/point2"}
REQUESTED_MAP_PATCHES = {
    "/$defs/layout_v1/properties/surface": {
        "description": "Ground surface polyline [[x, y], ...] in world pixels (y down); x never decreases and a "
                       "repeated x is a vertical step. Give surface or groundY.",
        "type": "array", "minItems": 2, "items": _POINT},
    "/$defs/layout_v1/properties/groundY": {"description": "Flat ground height when there is no surface.",
                                            "type": "number"},
    "/$defs/layout_v1/properties/kinds": {"description": "Extra segment kinds and the class each one maps to.",
                                          "type": "object", "additionalProperties": {"enum": ["ground", "gap",
                                                                                               "hazard"]}},
    "/$defs/layout_v1/properties/props/items": {
        "description": "A prop: (x, y) is the middle of its base and w its width; floating opts out of the support "
                       "check. Kinds deck, platform, plank and bridge are one-way standable surfaces "
                       "{x0, x1, y, thickness} (or x and w).",
        "type": "object",
        "properties": {"id": {"type": "string", "minLength": 1}, "kind": {"type": "string"},
                       "x": {"type": "number"}, "y": {"type": "number"}, "w": {"type": "number", "minimum": 0},
                       "x0": {"type": "number"}, "x1": {"type": "number"},
                       "thickness": {"type": "number", "minimum": 0}, "floating": {"type": "boolean"}}},
    "/$defs/layout_v1/properties/spawns/items": {
        "type": "object", "required": ["x", "y"],
        "properties": {"id": {"type": "string", "minLength": 1}, "x": {"type": "number"}, "y": {"type": "number"}}},
    "/$defs/layout_v1/properties/exits": {
        "description": "Places that must be reachable from the first spawn (default: the level end).",
        "type": "array", "items": {"type": "object", "required": ["x"],
                                   "properties": {"id": {"type": "string", "minLength": 1}, "x": {"type": "number"},
                                                  "y": {"type": "number"}}}},
    "/$defs/layout_v1/properties/arenas": {
        "description": "Ranges that must be at least the view width plus camera travel (default: the level).",
        "type": "array", "items": {"type": "object", "required": ["x0", "x1"],
                                   "properties": {"id": {"type": "string", "minLength": 1}, "x0": {"type": "number"},
                                                  "x1": {"type": "number"},
                                                  "travel": {"type": "number", "minimum": 0}}}},
    "/$defs/layout_v1/properties/camera": {
        "type": "object", "properties": {"viewHeight": {"type": "number", "exclusiveMinimum": 0},
                                         "travel": {"type": "number", "minimum": 0}}},
    "/$defs/layout_v1/properties/physics/properties/minDeckThickness": {"type": "number", "minimum": 0},
    "/$defs/layout_v1/properties/physics/properties/groundTolerance": {"type": "number", "minimum": 0},
    "/$defs/layout_v1/not": {"required": ["surface", "groundY"]},
}


def requested_contract_errors(document, name="layout_validation_v1"):
    """Errors against the vendored generate2dmap schemas plus REQUESTED_MAP_DEFS and REQUESTED_MAP_PATCHES
    (handoff section 5)."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    folder = SKILLS_DIR / SKILL / "references" / "schemas"
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.schema.json"))]
    map_schema = next(schema for schema in schemas if schema["$id"].endswith("/map.schema.json"))
    for key, fragment in REQUESTED_MAP_DEFS.items():
        map_schema["$defs"].setdefault(key, fragment)
    for pointer, fragment in REQUESTED_MAP_PATCHES.items():  # JSON pointer into map.schema.json -> new value
        *parents, leaf = pointer.lstrip("/").split("/")
        node = map_schema
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = fragment
    registry = Registry().with_resources((schema["$id"], DRAFT202012.create_resource(schema)) for schema in schemas)
    validator = Draft202012Validator({"$ref": f"{map_schema['$id']}#/$defs/{name}"}, registry=registry)
    return [f"{error.json_path}: {error.message}" for error in validator.iter_errors(document)]


def level(**overrides):
    """A 900 px level: ground, an 80 px gap, ground with a 4-row deck; flat at y=200 unless overridden."""
    document = {
        "schema": "generate2dmap.layout.v1",
        "segments": [[0, 300, "ground"], [300, 380, "gap"], [380, 900, "ground"]],
        "groundY": 200,
        "props": [{"id": "lamp", "x": 100, "y": 200, "w": 10},
                  {"id": "plank", "kind": "deck", "x0": 500, "x1": 560, "y": 160, "thickness": 4},
                  {"id": "crate", "x": 530, "y": 160, "w": 12}],
        "spawns": [{"id": "start", "x": 20, "y": 200}],
        "exits": [{"id": "goal", "x": 880}],
        "camera": {"viewHeight": 270, "travel": 80},
        "physics": dict(PHYSICS),
        "viewports": [[480, 270], "19.5:9"],
    }
    document.update(overrides)
    return document


def statuses(report):
    return {check["id"]: check["status"] for check in report["checks"]}


def check(report, ident):
    return next(item for item in report["checks"] if item["id"] == ident)


class ValidateLayoutTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def cli(self, document, *extra):
        path = self.root / f"layout-{len(list(self.root.glob('layout-*.json')))}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return run_cli([SCRIPT, "--layout", path, *extra])

    def test_fixture_satisfies_layout_v1(self):
        assert_valid_contract(level(), "map", "layout_v1", skill=SKILL)
        surface = level(surface=[[0, 200], [700, 200], [700, 184], [900, 184]], kinds={"bridge": "ground"},
                        arenas=[{"id": "boss", "x0": 0, "x1": 700, "travel": 80}])
        surface.pop("groundY")
        for document in (level(), surface):
            self.assertEqual(requested_contract_errors(document, "layout_v1"), [])
        both = level(surface=[[0, 200], [900, 200]])
        self.assertTrue(requested_contract_errors(both, "layout_v1"))  # surface or groundY, not both
        thin = level()
        thin["props"][1]["thickness"] = -1
        self.assertTrue(requested_contract_errors(thin, "layout_v1"))

    def test_valid_level_passes(self):
        result = self.cli(level())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "pass")
        report = LAYOUT.validate(level())
        self.assertEqual(set(statuses(report).values()), {"pass"})
        self.assertTrue(all(span["reachable"] for span in report["spans"]))

    def test_jump_reach_follows_the_parabola(self):
        physics = LAYOUT.parse_physics(PHYSICS)
        self.assertAlmostEqual(float(physics.reach(0)), 120)
        self.assertAlmostEqual(float(physics.reach(72)), 60)
        self.assertAlmostEqual(float(physics.reach(-72)), 60 * (1 + math.sqrt(2)))
        self.assertAlmostEqual(float(physics.height(60)), 72)
        self.assertEqual(physics.min_deck_thickness, 4)  # colliderSubstep 3 + 1

    def test_three_row_deck_fails(self):
        document = level()
        document["props"][1]["thickness"] = 3
        result = self.cli(document)
        self.assertEqual(result.returncode, 1)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["failed"], ["deck_thickness"])
        self.assertIn("deck plank is 3 px thick", summary["problems"][0])
        physics = dict(PHYSICS)
        physics.pop("colliderSubstep")
        self.assertEqual(statuses(LAYOUT.validate(level(physics=physics)))["deck_thickness"], "pass")  # default 4
        physics["minDeckThickness"] = 6
        self.assertEqual(statuses(LAYOUT.validate(level(physics=physics)))["deck_thickness"], "fail")

    def test_unjumpable_gap_fails(self):
        wide = level(segments=[[0, 300, "ground"], [300, 438, "gap"], [438, 900, "ground"]])
        result = self.cli(wide)
        self.assertEqual(result.returncode, 1)
        report = LAYOUT.validate(wide)
        self.assertEqual(statuses(report)["gaps_jumpable"], "fail")
        self.assertEqual(statuses(report)["reachability"], "fail")  # the exit lies beyond it
        gap = report["gaps"][0]
        self.assertEqual((gap["width"], gap["crossable"], gap["direct"]["feasible"]), (138, False, False))
        self.assertIn("gap x=300..438 (138 px) cannot be crossed", check(report, "gaps_jumpable")["value"][0])
        narrow = LAYOUT.validate(level())
        self.assertEqual(narrow["gaps"][0]["direct"]["feasible"], True)
        self.assertLessEqual(narrow["gaps"][0]["direct"]["distance"], 120)

    def test_gap_to_higher_ground_uses_the_falling_branch(self):
        surface = [[0, 200], [380, 200], [380, 140], [900, 140]]  # far side 60 px higher
        reach = 60 * (1 + math.sqrt(1 - 60 / 72))  # 84.5 px
        for width, crossable in ((70, True), (100, False)):
            document = level(segments=[[0, 380 - width, "ground"], [380 - width, 380, "gap"], [380, 900, "ground"]],
                             surface=surface, props=[], exits=[{"id": "goal", "x": 880}])
            document.pop("groundY")
            report = LAYOUT.validate(document)
            self.assertEqual(report["gaps"][0]["crossable"], crossable, width)
            self.assertAlmostEqual(report["gaps"][0]["direct"]["reach"], reach, places=3)

    def test_deck_bridges_a_gap_too_wide_to_jump(self):
        document = level(segments=[[0, 300, "ground"], [300, 500, "hazard"], [500, 900, "ground"]],
                         props=[{"id": "raft", "kind": "deck", "x0": 370, "x1": 430, "y": 196, "thickness": 6}])
        report = LAYOUT.validate(document)
        gap = report["gaps"][0]
        self.assertEqual((gap["kind"], gap["direct"]["feasible"], gap["crossable"]), ("hazard", False, True))
        self.assertEqual(report["status"], "pass")

    def test_floating_prop_fails(self):
        floating = level()
        floating["props"].append({"id": "lantern", "x": 200, "y": 184, "w": 9})
        result = self.cli(floating)
        self.assertEqual(result.returncode, 1)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["failed"], ["prop_support"])
        self.assertIn("prop lantern at x=200: 9 of 9 columns float (up to 16 px above the ground)",
                      summary["problems"][0])
        over_gap = level()
        over_gap["props"].append({"id": "post", "x": 340, "y": 200})
        self.assertEqual(check(LAYOUT.validate(over_gap), "prop_support")["status"], "fail")
        allowed = level()
        allowed["props"].append({"id": "lantern", "x": 200, "y": 150, "floating": True})
        self.assertEqual(statuses(LAYOUT.validate(allowed))["prop_support"], "pass")
        sunk = level()
        sunk["props"].append({"id": "rock", "x": 200, "y": 206, "w": 8})
        report = LAYOUT.validate(sunk)
        self.assertEqual(statuses(report)["prop_support"], "warn")
        self.assertEqual({prop["id"]: prop["status"] for prop in report["props"]}["crate"], "supported")  # on the deck

    def test_prop_straddling_a_slope_edge_floats(self):
        document = level(surface=[[0, 200], [200, 200], [240, 180], [900, 180]])
        document.pop("groundY")
        document["props"] = [{"id": "torii", "x": 200, "y": 200, "w": 40}]
        report = LAYOUT.validate(document)
        self.assertEqual(report["props"][0]["status"], "sunk")  # the slope rises under its east half
        document["props"] = [{"id": "torii", "x": 250, "y": 180, "w": 40}]
        self.assertEqual(LAYOUT.validate(document)["props"][0]["status"], "floating")  # west half over the slope

    def test_slope_and_step_limits(self):
        steep = level(surface=[[0, 200], [600, 200], [620, 154], [900, 154]])
        steep.pop("groundY")
        report = LAYOUT.validate(steep)
        self.assertEqual(statuses(report)["slope_limit"], "fail")
        self.assertIn("slope x=600..620 is 66.501 deg over 46 px", check(report, "slope_limit")["value"][0])
        dune = level(surface=[[0, 200], [600, 200], [680, 160], [760, 200], [900, 200]])  # 26.6 deg
        dune.pop("groundY")
        self.assertEqual(statuses(LAYOUT.validate(dune))["slope_limit"], "pass")
        ledge = level(surface=[[0, 200], [700, 200], [700, 184], [900, 184]])
        ledge.pop("groundY")
        report = LAYOUT.validate(ledge)
        self.assertEqual(report["pieces"], [{"kind": "ledge", "x": 700.0, "height": 16.0, "up": "east"}])
        self.assertEqual(report["status"], "pass")  # 16 px is jumped
        wall = level(surface=[[0, 200], [700, 200], [700, 100], [900, 100]])
        wall.pop("groundY")
        report = LAYOUT.validate(wall)
        self.assertEqual(statuses(report)["step_limit"], "warn")
        self.assertEqual(statuses(report)["reachability"], "fail")  # the exit is on top of a 100 px wall

    def test_arena_width_at_16_9_and_19_5_9(self):
        document = level(segments=[[0, 600, "ground"]], props=[], exits=[], viewports=[])
        report = LAYOUT.validate(document)
        arenas = {item["viewport"]: item for item in report["arenas"]}
        self.assertEqual((arenas["16:9"]["needed"], arenas["16:9"]["status"]), (560, "pass"))  # 480 + 80
        self.assertEqual((arenas["19.5:9"]["needed"], arenas["19.5:9"]["status"]), (665, "fail"))  # 585 + 80
        self.assertEqual(statuses(report)["arena_width"], "fail")
        boss = level(arenas=[{"id": "boss", "x0": 0, "x1": 700, "travel": 80}])
        self.assertEqual(statuses(LAYOUT.validate(boss))["arena_width"], "pass")
        no_height = level(viewports=[], camera={"travel": 80})
        self.assertEqual(statuses(LAYOUT.validate(no_height))["arena_width"], "skipped")

    def test_spawn_off_the_ground_fails(self):
        report = LAYOUT.validate(level(spawns=[{"id": "start", "x": 20, "y": 150}]))
        self.assertEqual(statuses(report)["spawns_grounded"], "fail")
        self.assertEqual(statuses(report)["reachability"], "fail")

    def test_short_kind_names_and_custom_kinds(self):
        document = level(segments=[[0, 300, "c"], [300, 380, "g"], [380, 600, "s"], [600, 900, "bridge"]],
                         kinds={"bridge": "ground"})
        self.assertEqual(LAYOUT.validate(document)["status"], "pass")
        document["kinds"] = {}
        result = self.cli(document)
        self.assertEqual(result.returncode, 1)
        self.assertIn("kind 'bridge' is unknown", result.stderr)

    def test_report_and_debug_png(self):
        out = self.root / "check"
        result = self.cli(level(), "--output-dir", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(Path(summary["debug"]), (out / "layout-debug.png").resolve())
        report = json.loads((out / "layout-report.json").read_text(encoding="utf-8"))
        self.assertEqual(requested_contract_errors(report), [])
        assert_valid_contract(report["qa"], "common", "qaEnvelope", skill=SKILL)
        with Image.open(out / "layout-debug.png") as image:
            self.assertEqual(image.width, 900)
            pixels = image.convert("RGB")
            self.assertEqual(pixels.getpixel((10, image.height - 2)), (150, 116, 82))  # ground column
        self.assertNotIn(":/", json.dumps(report))

    def test_reference_doc_example_passes(self):
        doc = (SKILLS_DIR / SKILL / "references" / "engine-maps.md").read_text(encoding="utf-8")
        example = next(block for block in re.findall(r"```json\n(.*?)```", doc, re.S) if "layout.v1" in block)
        document = json.loads(example)
        assert_valid_contract(document, "map", "layout_v1", skill=SKILL)
        self.assertEqual(requested_contract_errors(document, "layout_v1"), [])
        self.assertEqual(LAYOUT.validate(document)["status"], "pass")

    def test_bad_input_is_an_error(self):
        no_thickness = level()
        no_thickness["props"][1].pop("thickness")
        both = level(surface=[[0, 200], [900, 200]])
        for document in (no_thickness, both, level(physics={"jumpHeight": 72})):
            result = self.cli(document)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(result.stderr.startswith("error: "), result.stderr)

    def test_help_works_under_cp1252_and_cp950(self):
        assert_cli_help(SKILL, "validate_layout")  # cp1252 and cp950

    def test_refuses_an_existing_output_dir(self):
        out = self.root / "exists"
        out.mkdir()
        result = self.cli(level(), "--output-dir", out)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertEqual(list(out.iterdir()), [])

    def test_strict_qc_failure_publishes_nothing(self):
        document = level()
        document["props"][1]["thickness"] = 3
        lenient = self.cli(document, "--output-dir", self.root / "lenient")
        self.assertEqual(lenient.returncode, 1)
        self.assertTrue((self.root / "lenient" / "layout-debug.png").is_file())  # a failed check is still reported
        strict = self.cli(document, "--output-dir", self.root / "strict", "--strict-qc")
        self.assertEqual(strict.returncode, 1)
        self.assertIn("strict QC failed (deck_thickness)", strict.stderr)
        self.assertFalse((self.root / "strict").exists())
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.startswith(".strict")], [])


if __name__ == "__main__":
    unittest.main()
