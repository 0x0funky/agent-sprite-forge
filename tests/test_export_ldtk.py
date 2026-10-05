"""Tests for export_ldtk.py: an LDtk 1.5.3 project from a map bundle (B14-T2).

LDtk is not installed here: the project is checked against the exporter's required-field snapshot,
against an independent list of the fields loaders read, and read back against the bundle.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from forge_testutils import assert_cli_help, assert_valid_contract, load_script, run_cli, script_path
from test_export_godot import build_bundle, requested_contract_errors


LDTK = load_script("generate2dmap", "export_ldtk")
SCRIPT = script_path("generate2dmap", "export_ldtk")
SKILL = "generate2dmap"
T = 16

# Independent of the exporter's snapshot: the fields LDtk loaders (the official Haxe/JSON API and the
# common engine importers) read first, per object type.
LOADER_FIELDS = {
    "project": ("jsonVersion", "defs", "levels", "worldLayout", "iid"),
    "defs": ("layers", "entities", "tilesets", "enums", "levelFields"),
    "tileset": ("uid", "identifier", "relPath", "pxWid", "pxHei", "tileGridSize", "__cWid", "__cHei", "spacing",
                "padding"),
    "layer_def": ("uid", "identifier", "type", "__type", "gridSize", "tilesetDefUid"),
    "entity_def": ("uid", "identifier", "width", "height", "pivotX", "pivotY", "fieldDefs", "tileRect"),
    "level": ("uid", "iid", "identifier", "worldX", "worldY", "pxWid", "pxHei", "layerInstances",
              "fieldInstances", "bgRelPath"),
    "layer": ("__identifier", "__type", "__cWid", "__cHei", "__gridSize", "layerDefUid", "gridTiles",
              "entityInstances", "intGridCsv", "autoLayerTiles", "__tilesetDefUid", "__tilesetRelPath"),
    "tile": ("px", "src", "f", "t", "d"),
    "entity": ("__identifier", "__grid", "__pivot", "iid", "defUid", "px", "width", "height", "fieldInstances"),
    "field": ("__identifier", "__type", "__value", "defUid"),
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def export(bundle, out, *extra):
    return run_cli([SCRIPT, "--bundle", bundle, "--output-dir", out, "--name", "town", *extra])


def fields(entity):
    return {field["__identifier"]: field["__value"] for field in entity["fieldInstances"]}


class ExportLdtkTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.bundle, self.grid = build_bundle(self.root / "map")
        self.data = json.loads(self.bundle.read_text(encoding="utf-8"))
        self.out = self.root / "ldtk"

    def tearDown(self):
        self._temporary.cleanup()

    def run_export(self, *extra, bundle=None, out=None):
        result = export(bundle or self.bundle, out or self.out, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def project(self, out=None):
        return json.loads(((out or self.out) / "town.ldtk").read_text(encoding="utf-8"))

    def entities(self, project):
        return [entity for layer in project["levels"][0]["layerInstances"] for entity in layer["entityInstances"]]

    def test_ldtk_required_fields(self):
        summary = self.run_export()
        project = self.project()
        self.assertEqual(LDTK.ldtk_spec_errors(project), [])
        self.assertEqual(LDTK.ldtk_id_errors(project), [])
        self.assertEqual(project["jsonVersion"], "1.5.3")
        defs, level = project["defs"], project["levels"][0]
        checks = [("project", project), ("defs", defs)] + [("tileset", item) for item in defs["tilesets"]] \
            + [("layer_def", item) for item in defs["layers"]] + [("entity_def", item) for item in defs["entities"]] \
            + [("level", level)] + [("layer", item) for item in level["layerInstances"]]
        for layer in level["layerInstances"]:
            checks += [("tile", tile) for tile in layer["gridTiles"]]
            for entity in layer["entityInstances"]:
                checks += [("entity", entity)] + [("field", field) for field in entity["fieldInstances"]]
        for kind, item in checks:
            missing = [key for key in LOADER_FIELDS[kind] if key not in item]
            self.assertEqual(missing, [], f"{kind} {item.get('identifier', item.get('__identifier', ''))}")
        # the snapshot catches a missing required field and a wrong type
        broken = json.loads(json.dumps(project))
        del broken["levels"][0]["layerInstances"][0]["iid"]
        broken["defs"]["tilesets"][0]["pxWid"] = "64"
        errors = LDTK.ldtk_spec_errors(broken)
        self.assertTrue(any("misses required field 'iid'" in error for error in errors), errors)
        self.assertTrue(any("pxWid: expected int" in error for error in errors), errors)
        report = json.loads((self.out / "ldtk-export.json").read_text(encoding="utf-8"))
        status = {check["id"]: check["status"] for check in report["qa"]["checks"]}
        self.assertEqual(status["ldtk_required_fields"], "pass")
        self.assertEqual(summary["status"], "pass")

    def test_entities_positions_match_bundle(self):
        self.run_export()
        project = self.project()
        by_id = {(entity["__identifier"].split("_")[0], fields(entity)["BundleId"]): entity
                 for entity in self.entities(project)}
        for spawn in self.data["spawns"]:
            entity = by_id[("Spawn", spawn["id"])]
            self.assertEqual((entity["__identifier"], entity["px"]), ("Spawn", [spawn["x"], spawn["y"]]))
            self.assertEqual(entity["__pivot"], [0.5, 1.0])
            self.assertEqual(fields(entity)["Facing"], spawn["facing"])
        for portal in self.data["portals"]:
            entity = by_id[("Portal", portal["id"])]
            self.assertEqual(entity["__identifier"], "Portal")
            if "rect" in portal:
                x, y, w, h = portal["rect"]
                self.assertEqual((entity["px"], entity["width"], entity["height"]), ([x, y], w, h))
                self.assertEqual(fields(entity)["Shape"], "rect")
                self.assertEqual((fields(entity)["TravelX"], fields(entity)["TravelY"]), (1.0, 0.0))
                self.assertEqual(json.loads(fields(entity)["EntranceByFrom"]), portal["entranceByFrom"])
            else:
                cx, cy, radius = portal["circle"]
                self.assertEqual((entity["px"], entity["width"]), ([cx - radius, cy - radius], 2 * radius))
                self.assertEqual(fields(entity)["Shape"], "circle")
            self.assertEqual(fields(entity)["To"], portal["to"])
        for item in self.data["interactions"]:
            entity = by_id[("Interaction", item["id"])]
            self.assertEqual((entity["__identifier"], entity["px"]), ("Interaction", [item["x"], item["y"]]))
            self.assertEqual(fields(entity)["Reach"], None if "reach" not in item else float(item["reach"]))
        for name, anchor in self.data["anchors"].items():
            entity = by_id[("Anchor", name)]
            self.assertEqual((entity["__identifier"], entity["px"]), ("Anchor", anchor["point"]))
            self.assertEqual(json.loads(fields(entity)["Slots"]), anchor["slots"])
        definitions = {item["uid"]: item for item in project["defs"]["entities"]}
        for item in self.data["objects"]:
            entity = by_id[("Prop", item["id"])]
            definition = definitions[entity["defUid"]]
            self.assertEqual(entity["px"], [item["x"], item["y"]])  # the pivot is the prop's anchor
            size = Image.open(self.root / "map" / "props" / item["prop"] / "prop.png").size
            self.assertEqual((definition["pivotX"] * size[0], definition["pivotY"] * size[1]), tuple(item["anchor_px"]))
            scale = item.get("scale", 1)
            self.assertEqual((entity["width"], entity["height"]), (size[0] * scale, size[1] * scale))
            self.assertEqual(entity["__worldX"], entity["px"][0])
            self.assertEqual(entity["__grid"], [entity["px"][0] // T, entity["px"][1] // T])
            self.assertEqual(fields(entity)["Prop"], item["prop"])

    def test_tiles_layer_matches_bundle_grid(self):
        self.run_export()
        project = self.project()
        level = project["levels"][0]
        layer = next(item for item in level["layerInstances"] if item["__type"] == "Tiles")
        tileset = next(item for item in project["defs"]["tilesets"] if item["uid"] == layer["__tilesetDefUid"])
        self.assertEqual((layer["__cWid"], layer["__cHei"], layer["__gridSize"]), (10, 6, T))
        self.assertEqual(tileset["relPath"], layer["__tilesetRelPath"])
        self.assertEqual(_sha(self.out / tileset["relPath"]), _sha(self.root / "map" / "tiles" / "terrain.png"))
        got = np.full(self.grid.shape, -1)
        for tile in layer["gridTiles"]:
            col, row = tile["px"][0] // T, tile["px"][1] // T
            ax, ay = tile["src"][0] // T, tile["src"][1] // T
            self.assertEqual(tile["t"], ax + ay * tileset["__cWid"])
            self.assertEqual(tile["d"], [col + row * layer["__cWid"]])
            got[row, col] = ay * 4 + ax
        self.assertTrue(np.array_equal(got, self.grid))
        custom = {item["tileId"]: json.loads(item["data"]) for item in tileset["customData"]}
        self.assertEqual(custom[0]["wang"], [0, 0, 0, 0])
        self.assertEqual(len(custom[0]["collision"]), 4)
        # LDtk lists layers top first; the bottom image layer is the level background
        self.assertEqual([item["__identifier"] for item in level["layerInstances"]], ["Markers", "Objects", "Ground"])
        self.assertEqual(level["bgRelPath"], "assets/layers/base.png")

    def test_props_atlas_holds_each_prop_image(self):
        self.run_export()
        project = self.project()
        atlas = np.asarray(Image.open(self.out / "assets" / "props-atlas.png").convert("RGBA"))
        for definition in project["defs"]["entities"]:
            rect = definition["tileRect"]
            if rect is None:
                continue
            label = definition["identifier"].removeprefix("Prop_")
            source = np.asarray(Image.open(self.root / "map" / "props" / label / "prop.png").convert("RGBA"))
            region = atlas[rect["y"]:rect["y"] + rect["h"], rect["x"]:rect["x"] + rect["w"]]
            self.assertTrue(np.array_equal(region, source), label)
            self.assertEqual(rect["x"] % T, 0)

    def test_rounded_positions_warn_and_strict_qc_publishes_nothing(self):
        bundle, _ = build_bundle(self.root / "frac", object_x=40.5)
        summary = self.run_export(bundle=bundle, out=self.root / "frac-out")
        self.assertEqual(summary["status"], "warn")
        report = json.loads((self.root / "frac-out" / "ldtk-export.json").read_text(encoding="utf-8"))
        check = next(check for check in report["qa"]["checks"] if check["id"] == "entity_positions")
        self.assertEqual(check["status"], "warn")
        self.assertIn("object tree-1 (40.5, 60) rounded to (41, 60)", check["value"])
        strict = export(bundle, self.root / "frac-strict", "--strict-qc")
        self.assertEqual(strict.returncode, 1)
        self.assertIn("entity_positions", strict.stderr)
        self.assertFalse((self.root / "frac-strict").exists())

    def test_deterministic_output(self):
        self.run_export()
        self.run_export(out=self.root / "again")
        files = sorted(path.relative_to(self.out) for path in self.out.rglob("*") if path.is_file())
        self.assertEqual(files, sorted(path.relative_to(self.root / "again")
                                       for path in (self.root / "again").rglob("*") if path.is_file()))
        for relative in files:
            self.assertEqual((self.out / relative).read_bytes(), (self.root / "again" / relative).read_bytes())

    def test_report_satisfies_the_requested_contract(self):
        self.run_export()
        report = json.loads((self.out / "ldtk-export.json").read_text(encoding="utf-8"))
        self.assertEqual(requested_contract_errors(report, "engine_export_v1"), [])
        assert_valid_contract(report["qa"], "common", "qaEnvelope", skill=SKILL)
        self.assertEqual(report["engine"]["name"], "ldtk")
        self.assertTrue(any(item.startswith("collision:") for item in report["notExported"]))

    def test_non_square_tiles_are_refused(self):
        self.data["tile_size"] = [16, 8]
        path = self.root / "map" / "wide.json"
        path.write_text(json.dumps(self.data), encoding="utf-8")
        result = export(path, self.root / "wide")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertFalse((self.root / "wide").exists())

    def test_help_works_under_cp1252_and_cp950(self):
        assert_cli_help(SKILL, "export_ldtk")  # cp1252 and cp950

    def test_refuses_an_existing_output_dir(self):
        self.out.mkdir()
        result = export(self.bundle, self.out)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_strict_qc_failure_publishes_nothing(self):
        bundle, _ = build_bundle(self.root / "outside", extra_objects=[
            {"id": "lost", "prop": "rock", "x": 400, "y": 20, "anchor_px": [7, 10]}])
        result = export(bundle, self.root / "strict", "--strict-qc")
        self.assertEqual(result.returncode, 1)
        self.assertIn("objects_in_world", result.stderr)
        self.assertFalse((self.root / "strict").exists())
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.startswith(".strict")], [])


if __name__ == "__main__":
    unittest.main()
