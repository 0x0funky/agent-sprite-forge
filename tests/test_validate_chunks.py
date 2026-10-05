"""Tests for validate_chunks.py: room_chunk.v1 sockets, stitching and reachability (B14-T3)."""
from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, run_cli,
                             script_path)


CHUNKS = load_script("generate2dmap", "validate_chunks")
SCRIPT = script_path("generate2dmap", "validate_chunks")
SKILL = "generate2dmap"
CELL = 16
SIZE = (160, 96)  # 10 x 6 cells
EAST_WEST = {"offset": 32, "width": 32, "material": "floor"}  # rows 2-3
NORTH_SOUTH = {"offset": 64, "width": 32, "material": "floor"}  # columns 4-5

# The $def this module asks integration to add to map.schema.json (handoff section 5), applied in memory.
REQUESTED_MAP_DEFS = {
    "chunk_validation_v1": {
        "description": "validate_chunks.py report: placements, socket pairing per shared edge, chunk-graph and "
                       "walkability-grid reachability, and the QA envelope.",
        "type": "object",
        "required": ["schema", "tool", "status", "mode", "placements", "sockets", "qa"],
        "properties": {
            "schema": {"const": "generate2dmap.chunk_validation.v1"},
            "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
            "status": {"enum": ["pass", "warn", "fail"]},
            "mode": {"enum": ["rows", "edges", "kit"]},
            "start": {"anyOf": [{"type": "null"}, {"type": "object", "required": ["chunk"],
                                                   "properties": {"chunk": {"type": "string", "minLength": 1},
                                                                  "point": {"anyOf": [
                                                                      {"type": "null"},
                                                                      {"$ref": "common.schema.json#/$defs/point2"}]}}}]},
            "unused": {"type": "array", "items": {"type": "string"}},
            "unplaced": {"type": "array", "items": {"type": "string"}},
            "placements": {"type": "array", "items": {
                "type": "object", "required": ["id", "rect"],
                "properties": {"id": {"type": "string", "minLength": 1},
                               "rect": {"$ref": "common.schema.json#/$defs/rectXYWH"}}}},
            "contacts": {"type": "array", "items": {
                "type": "object", "required": ["a", "side", "b", "start", "end"],
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}, "side": {"enum": ["E", "S"]},
                               "b_side": {"enum": ["W", "N"]}, "start": {"type": "number"},
                               "end": {"type": "number"}}}},
            "sockets": {"type": "array", "items": {
                "type": "object", "required": ["chunk", "side", "index", "offset", "width", "status"],
                "properties": {"chunk": {"type": "string"}, "side": {"enum": ["N", "E", "S", "W"]},
                               "index": {"type": "integer", "minimum": 0}, "offset": {"type": "number"},
                               "width": {"type": "number"}, "material": {"type": ["string", "null"]},
                               "status": {"enum": ["paired", "mismatch", "straddle", "dangling", "unplaced"]},
                               "world_span": {"anyOf": [{"type": "null"}, {"type": "array", "items": {
                                   "type": "number"}, "minItems": 2, "maxItems": 2}]},
                               "partner": {"anyOf": [{"type": "null"}, {"type": "object"}]}}}},
            "reachability": {"anyOf": [{"type": "null"}, {"type": "object"}]},
            "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
        },
    },
}


# Additions to the existing room_chunk_v1 contract that validate_chunks.py reads (handoff section 5).
REQUESTED_MAP_PATCHES = {
    "/$defs/room_chunk_v1/properties/cell": {"description": "Pixels per grid character (chunks may override).",
                                             "type": "integer", "minimum": 1},
    "/$defs/room_chunk_v1/properties/start": {
        "description": "Start chunk (or chunk instance such as corridor@0,1), optionally with a point in its pixels.",
        "anyOf": [{"type": "string", "minLength": 1},
                  {"type": "object", "required": ["chunk"],
                   "properties": {"chunk": {"type": "string", "minLength": 1}, "x": {"type": "number"},
                                  "y": {"type": "number"}}}]},
    "/$defs/room_chunk_v1/properties/chunks/items/properties/grid": {
        "description": "Walkability rows, top first: '.' walkable, '#' blocked; size = grid size x cell.",
        "type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^[.#]+$"}},
    "/$defs/room_chunk_v1/properties/chunks/items/properties/cell": {"type": "integer", "minimum": 1},
    "/$defs/room_chunk_v1/properties/graph/items": {
        "description": "Either layout rows of chunk ids (null for an empty cell; a repeated id is one instance per "
                       "cell) or edges: b sits on a's side, shifted offset px along it.",
        "anyOf": [{"type": "array", "items": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]}},
                  {"type": "object", "required": ["from", "to", "side"],
                   "properties": {"from": {"type": "string", "minLength": 1}, "to": {"type": "string", "minLength": 1},
                                  "side": {"enum": ["N", "E", "S", "W"]}, "offset": {"type": "number"}}}]},
}


def requested_contract_errors(document, name="chunk_validation_v1"):
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


def room(ident, sockets, *, walls=(), grid=True):
    """A walled room with its door cells opened; `walls` adds (row, col) blocked cells."""
    cols, rows = SIZE[0] // CELL, SIZE[1] // CELL
    cells = [["#" if r in (0, rows - 1) or c in (0, cols - 1) else "." for c in range(cols)] for r in range(rows)]
    for side, items in sockets.items():
        for item in items:
            for k in range(item["offset"] // CELL, (item["offset"] + item["width"]) // CELL):
                row, col = {"N": (0, k), "S": (rows - 1, k), "W": (k, 0), "E": (k, cols - 1)}[side]
                cells[row][col] = "."
    for row, col in walls:
        cells[row][col] = "#"
    chunk = {"id": ident, "size": list(SIZE), "sockets": copy.deepcopy(sockets)}
    if grid:
        chunk["grid"] = ["".join(row) for row in cells]
    return chunk


def two_by_two(graph="rows"):
    chunks = [room("a", {"E": [EAST_WEST], "S": [NORTH_SOUTH]}), room("b", {"W": [EAST_WEST], "S": [NORTH_SOUTH]}),
              room("c", {"N": [NORTH_SOUTH], "E": [EAST_WEST]}), room("d", {"N": [NORTH_SOUTH], "W": [EAST_WEST]})]
    if graph == "rows":
        layout = [["a", "b"], ["c", "d"]]
    else:
        layout = [{"from": "a", "to": "b", "side": "E"}, {"from": "a", "to": "c", "side": "S"},
                  {"from": "b", "to": "d", "side": "S"}, {"from": "c", "to": "d", "side": "E"}]
    return {"schema": "generate2dmap.room_chunk.v1", "cell": CELL, "chunks": chunks, "graph": layout}


def statuses(report):
    return {check["id"]: check["status"] for check in report["checks"]}


class ValidateChunksTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def cli(self, document, *extra):
        path = self.root / f"chunks-{len(list(self.root.glob('chunks-*.json')))}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return run_cli([SCRIPT, "--chunks", path, *extra])

    def test_fixtures_satisfy_room_chunk_v1(self):
        for form in ("rows", "edges"):
            assert_valid_contract(two_by_two(form), "map", "room_chunk_v1", skill=SKILL)
            self.assertEqual(requested_contract_errors(two_by_two(form), "room_chunk_v1"), [])
        holes = two_by_two()
        holes["graph"] = [["a", None], ["c", "d"]]
        holes["start"] = {"chunk": "a", "x": 40, "y": 40}
        self.assertEqual(requested_contract_errors(holes, "room_chunk_v1"), [])
        bad = two_by_two()
        bad["chunks"][0]["grid"][1] = "#..x.....#"
        self.assertTrue(requested_contract_errors(bad, "room_chunk_v1"))
        bad = two_by_two("edges")
        bad["graph"][0]["side"] = "up"
        self.assertTrue(requested_contract_errors(bad, "room_chunk_v1"))

    def test_two_by_two_layout_passes(self):
        result = self.cli(two_by_two("rows"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary["status"], summary["placed"], summary["failed"]), ("pass", 4, []))
        report = CHUNKS.validate(two_by_two("rows"))
        self.assertEqual(statuses(report)["grid_reachability"], "pass")
        self.assertEqual(report["reachability"]["graph"]["reached"], ["a", "b", "c", "d"])
        self.assertEqual({row["status"] for row in report["sockets"]}, {"paired"})
        self.assertEqual(sorted((p["id"], tuple(p["rect"][:2])) for p in report["placements"]),
                         [("a", (0, 0)), ("b", (160, 0)), ("c", (0, 96)), ("d", (160, 96))])

    def test_two_by_two_edge_graph_passes(self):
        report = CHUNKS.validate(two_by_two("edges"))
        self.assertEqual(report["status"], "pass", report["checks"])
        self.assertEqual(statuses(report)["edges_connected"], "pass")
        self.assertEqual(sorted((p["id"], tuple(p["rect"][:2])) for p in report["placements"]),
                         [("a", (0, 0)), ("b", (160, 0)), ("c", (0, 96)), ("d", (160, 96))])

    def test_width_mismatch_fails(self):
        document = two_by_two()
        document["chunks"][1]["sockets"]["W"][0]["width"] = 24
        result = self.cli(document)
        self.assertEqual(result.returncode, 1)
        summary = json.loads(result.stdout)
        self.assertIn("socket_pairs", summary["failed"])
        self.assertTrue(any("a.E[0] [32, 64) width 32 does not match b.W[0] [32, 56) width 24" in problem
                            for problem in summary["problems"]), summary["problems"])
        self.assertIn("error: chunk validation failed", result.stderr)

    def test_offset_and_material_mismatches_fail(self):
        shifted = two_by_two()
        shifted["chunks"][2]["sockets"]["E"][0]["offset"] = 48
        self.assertEqual(statuses(CHUNKS.validate(shifted))["socket_pairs"], "fail")
        water = two_by_two()
        water["chunks"][3]["sockets"]["N"][0]["material"] = "water"
        report = CHUNKS.validate(water)
        self.assertEqual(statuses(report)["socket_materials"], "fail")
        self.assertIn("b.S[0] is 'floor' but d.N[0] is 'water'", report["checks"][4]["value"][0])
        self.assertIn("d", report["reachability"]["graph"]["reached"])  # still reachable through c

    def test_door_into_wall_fails(self):
        document = two_by_two()
        document["chunks"][1] = room("b", {"W": [EAST_WEST], "S": [NORTH_SOUTH]}, walls=[(2, 0), (3, 0)])
        report = CHUNKS.validate(document)
        self.assertEqual(statuses(report)["grid_reachability"], "fail")
        self.assertTrue(any("door a.E[0] <-> b.W[0] opens into a wall" in problem
                            for problem in report["reachability"]["grid"]["problems"]))

    def test_unreachable_on_foot_fails_although_doors_pair(self):
        corridor = room("b", {"W": [EAST_WEST], "E": [EAST_WEST]}, walls=[(row, 5) for row in range(6)])
        document = {"schema": "generate2dmap.room_chunk.v1", "cell": CELL, "graph": [["a", "b", "c"]],
                    "chunks": [room("a", {"E": [EAST_WEST]}), corridor, room("c", {"W": [EAST_WEST]})]}
        report = CHUNKS.validate(document)
        self.assertEqual(statuses(report)["graph_reachability"], "pass")  # doors pair...
        grid = report["reachability"]["grid"]
        self.assertEqual(grid["status"], "fail")  # ...but the wall in b cuts the walk
        self.assertIn("chunk c cannot be reached on foot from a", grid["problems"])
        self.assertTrue(any("b: " in warning and "cannot be reached (islands)" in warning
                            for warning in grid["warnings"]))

    def test_reused_chunk_becomes_instances(self):
        corridor = room("corridor", {"W": [EAST_WEST], "E": [EAST_WEST]})
        document = {"schema": "generate2dmap.room_chunk.v1", "cell": CELL,
                    "graph": [["a", "corridor", "corridor", "c"]],
                    "chunks": [room("a", {"E": [EAST_WEST]}), corridor, room("c", {"W": [EAST_WEST]})]}
        report = CHUNKS.validate(document)
        self.assertEqual(report["status"], "pass", report["checks"])
        self.assertEqual([item["id"] for item in report["placements"]], ["a", "corridor@0,1", "corridor@0,2", "c"])
        self.assertEqual(report["unused"], [])
        result = self.cli(document, "--start", "corridor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("chunk corridor is placed 2 times; start at one of corridor@0,1, corridor@0,2", result.stderr)
        self.assertEqual(CHUNKS.validate(document, "corridor@0,2")["status"], "pass")

    def test_reference_doc_example_passes(self):
        doc = (SKILLS_DIR / SKILL / "references" / "engine-maps.md").read_text(encoding="utf-8")
        example = next(block for block in re.findall(r"```json\n(.*?)```", doc, re.S) if "room_chunk.v1" in block)
        document = json.loads(example)
        assert_valid_contract(document, "map", "room_chunk_v1", skill=SKILL)
        self.assertEqual(requested_contract_errors(document, "room_chunk_v1"), [])
        self.assertEqual(CHUNKS.validate(document)["status"], "pass")

    def test_dangling_socket_and_open_edge_warn(self):
        document = two_by_two()
        document["chunks"][0]["sockets"]["N"] = [{"offset": 16, "width": 16, "material": "floor"}]
        document["chunks"][0]["grid"][0] = "#.########"
        result = self.cli(document)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "warn")
        self.assertTrue(any("a.N[0] [16, 32) touches no chunk" in warning for warning in summary["warnings"]))
        leaky = two_by_two()
        leaky["chunks"][0]["grid"][0] = "#....#####"
        report = CHUNKS.validate(leaky)
        self.assertTrue(any("a.N has 4 walkable cells on the outer edge without a socket" in warning
                            for warning in report["reachability"]["grid"]["warnings"]))

    def test_edge_graph_needs_a_door_and_one_consistent_placement(self):
        doorless = two_by_two("edges")
        doorless["chunks"][0]["sockets"].pop("E")
        doorless["chunks"][1]["sockets"].pop("W")
        report = CHUNKS.validate(doorless)
        self.assertEqual(statuses(report)["edges_connected"], "fail")
        conflict = two_by_two("edges")
        conflict["graph"].append({"from": "b", "to": "c", "side": "S"})
        self.assertEqual(statuses(CHUNKS.validate(conflict))["placement"], "fail")
        overlap = two_by_two("edges")
        overlap["graph"] = [{"from": "a", "to": "b", "side": "E"}, {"from": "a", "to": "c", "side": "E"}]
        report = CHUNKS.validate(overlap)
        self.assertIn("chunks b and c overlap", report["checks"][2]["value"])

    def test_kit_without_graph_checks_partners(self):
        kit = two_by_two()
        kit["graph"] = []
        kit["chunks"][0]["sockets"]["E"][0]["width"] = 48
        report = CHUNKS.validate(kit)
        self.assertEqual(report["mode"], "kit")
        self.assertEqual(statuses(report)["kit_partners"], "warn")
        self.assertIn("a.E[0] (width 48, floor) has no partner on any W edge in the kit",
                      report["checks"][2]["value"])

    def test_start_point_and_unknown_start(self):
        result = self.cli(two_by_two(), "--start", "d:40,40")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = CHUNKS.validate(two_by_two(), "d:40,40")
        self.assertEqual(report["start"], {"chunk": "d", "point": [40.0, 40.0]})
        wall = self.cli(two_by_two(), "--start", "d:8,8")
        self.assertEqual(wall.returncode, 1)
        self.assertIn("not on a walkable cell", wall.stdout)
        unknown = self.cli(two_by_two(), "--start", "zz")
        self.assertEqual(unknown.returncode, 1)
        self.assertTrue(unknown.stderr.startswith("error: start chunk 'zz'"))

    def test_report_and_debug_png(self):
        out = self.root / "check"
        result = self.cli(two_by_two(), "--output-dir", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(Path(summary["report"]), (out / "chunk-report.json").resolve())
        report = json.loads((out / "chunk-report.json").read_text(encoding="utf-8"))
        self.assertEqual(requested_contract_errors(report), [])
        assert_valid_contract(report["qa"], "common", "qaEnvelope", skill=SKILL)
        self.assertEqual(report["qa"]["outputs"][0]["path"], "chunk-debug.png")
        with Image.open(out / "chunk-debug.png") as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertGreaterEqual(min(image.size), 256)
        self.assertNotIn(":/", json.dumps(report))

    def test_bad_input_is_an_error(self):
        bad_side = two_by_two()
        bad_side["chunks"][0]["sockets"]["X"] = []
        bad_grid = two_by_two()
        bad_grid["chunks"][0]["grid"] = bad_grid["chunks"][0]["grid"][:-1]
        for document in (bad_side, bad_grid, {"schema": "nope", "chunks": [], "graph": []}):
            result = self.cli(document)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(result.stderr.startswith("error: "), result.stderr)

    def test_help_works_under_cp1252_and_cp950(self):
        assert_cli_help(SKILL, "validate_chunks")  # cp1252 and cp950

    def test_refuses_an_existing_output_dir(self):
        out = self.root / "exists"
        out.mkdir()
        result = self.cli(two_by_two(), "--output-dir", out)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertEqual(list(out.iterdir()), [])

    def test_strict_qc_failure_publishes_nothing(self):
        document = two_by_two()
        document["chunks"][1]["sockets"]["W"][0]["width"] = 24
        lenient = self.cli(document, "--output-dir", self.root / "lenient")
        self.assertEqual(lenient.returncode, 1)
        self.assertTrue((self.root / "lenient" / "chunk-report.json").is_file())  # a failed check is still reported
        strict = self.cli(document, "--output-dir", self.root / "strict", "--strict-qc")
        self.assertEqual(strict.returncode, 1)
        self.assertIn("strict QC failed", strict.stderr)
        self.assertFalse((self.root / "strict").exists())
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.startswith(".strict")], [])


if __name__ == "__main__":
    unittest.main()
