"""Platform strips (generate2dmap/scripts/extract_platform_strip.py).

PlatformStripTests are the improved fork's 16 tests (plan B11-T1, study-asf-improved/port), ported
with three marked adaptations to the shared core: the manifest is generate2dmap.platform_strip.v2,
published PNGs carry no RGB under alpha 0 (plan Appendix D), and the race test patches the publish
step inside forge_core.staged_output. The other classes cover MAP-07 (repro_14), MAP-19
(repro_15), MAP-14 (repro_16), middle variants, the fork's despill hunk, the proposed contract and
the CLI conventions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageFilter

from forge_testutils import SKILLS_DIR, assert_cli_help, load_script, run_cli, script_path

MODULE = load_script("generate2dmap", "extract_platform_strip")
SCRIPT = script_path("generate2dmap", "extract_platform_strip")


def zero_hidden_rgb(image: Image.Image) -> Image.Image:
    """The published form of an RGBA crop: RGB is 0 wherever alpha is 0 (plan Appendix D)."""
    pixels = np.array(image.convert("RGBA"))
    pixels[pixels[..., 3] == 0] = 0
    return Image.fromarray(pixels)


# --------------------------------------------------------------------------- proposed contract (handoff section 5)

SEAM = {
    "description": "Normalised seam of one join or wrap: common seamReport plus seam_ratio, near_median and "
                   "verdict. seam_ratio is the larger of seam / adjacent_max and the worst 4-row window of the join "
                   "over the worst window of the interior steps near it: about 1 or less looks like the art, well "
                   "above 1 is a seam. duplicate_edge is a join much flatter than its neighbourhood (a stutter).",
    "allOf": [{"$ref": "common.schema.json#/$defs/seamReport"}],
    "required": ["seam_ratio", "near_median", "verdict"],
    "properties": {
        "seam_ratio": {"type": "number", "minimum": 0},
        "near_median": {"type": "number", "minimum": 0},
        "verdict": {"enum": ["continuous", "seam", "duplicate_edge"]},
    },
}
_INDEX_LIST = {"type": "array", "items": {"type": "integer", "minimum": 0}}
PLATFORM_STRIP_V2 = {
    "description": "platform-strip.json from extract_platform_strip.py: exact rectangular crops of a left cap, one or "
                   "more interchangeable middle variants and a right cap, published with their sha256. Collision is "
                   "declared metadata: collision_rect_px is [x, y, width, height] in piece pixels, collision_span_px "
                   "a half-open column range. surface records the art's top per collision column against the "
                   "declared surface. Every join is measured (seam: edgeSeam). v1 manifests (schema "
                   "generate2dmap.platform_strip.v1) stored absolute source paths and had no qa block.",
    "type": "object",
    "required": ["schema", "source", "spec", "processing", "surface_y_px", "collision_depth_px", "pieces", "joins",
                 "preview", "qc", "qa"],
    "properties": {
        "schema": {"const": "generate2dmap.platform_strip.v2"},
        "source": {"allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}], "required": ["size", "mode"],
                   "properties": {"size": {"$ref": "common.schema.json#/$defs/size2"},
                                  "mode": {"type": "string", "minLength": 1},
                                  "bit_depth": {"type": "integer", "minimum": 1},
                                  "conversion": {"type": "string"}}},
        "spec": {"allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}], "required": ["content"],
                 "properties": {"content": {"type": "object"}}},
        "processing": {"type": "object", "required": ["background_mode", "geometry", "resized"],
                       "properties": {"background_mode": {"enum": ["chroma_key", "native_alpha", "opaque"]},
                                      "despill_radius": {"type": "integer", "minimum": 0, "maximum": 3},
                                      "geometry": {"const": "explicit_native_rectangles"},
                                      "resized": {"const": False}, "trimmed": {"const": False},
                                      "aligned": {"const": False}}},
        "surface_y_px": {"type": "integer", "minimum": 0},
        "collision_depth_px": {"type": "integer", "minimum": 1},
        "pieces": {"type": "array", "minItems": 3, "items": {
            "type": "object",
            "required": ["id", "role", "source_box", "size", "collision_span_px", "path", "sha256", "anchor_px",
                         "collision_rect_px", "coverage", "surface"],
            "properties": {
                "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]*$"},
                "role": {"enum": ["left_cap", "middle", "right_cap"]},
                "source_box": {"$ref": "common.schema.json#/$defs/box"},
                "size": {"$ref": "common.schema.json#/$defs/size2"},
                "collision_span_px": {**_INDEX_LIST, "minItems": 2, "maxItems": 2},
                "path": {"$ref": "common.schema.json#/$defs/relPath"},
                "sha256": {"$ref": "common.schema.json#/$defs/sha256"},
                "anchor_px": {"$ref": "common.schema.json#/$defs/point2"},
                "collision_rect_px": {"$ref": "common.schema.json#/$defs/rectXYWH"},
                "surface_y_source_px": {"type": "integer", "minimum": 0},
                "coverage": {"type": "object",
                             "required": ["solid_fraction_by_column", "minimum_column_fraction",
                                          "insufficient_columns", "missing_surface_columns", "non_solid_band_pixels"],
                             "properties": {
                                 "solid_fraction_by_column": {"type": "array",
                                                              "items": {"type": "number", "minimum": 0, "maximum": 1}},
                                 "minimum_column_fraction": {"type": "number", "minimum": 0, "maximum": 1},
                                 "insufficient_columns": _INDEX_LIST,
                                 "missing_surface_columns": _INDEX_LIST,
                                 "non_solid_band_pixels": {"type": "integer", "minimum": 0}}},
                "surface": {"type": "object",
                            "required": ["declared_y_px", "measured", "max_rise_px", "columns_above_tolerance",
                                         "decoration_band_px"],
                            "properties": {
                                "declared_y_px": {"type": "integer", "minimum": 0},
                                "measured": {"type": "boolean"},
                                "measured_y_px_by_column": {"type": ["array", "null"],
                                                            "items": {"type": ["integer", "null"], "minimum": 0}},
                                "max_rise_px": {"type": "integer", "minimum": 0},
                                "columns_above_tolerance": _INDEX_LIST,
                                "decoration_band_px": {"type": "integer", "minimum": 0},
                                "decoration_px": {"type": "integer", "minimum": 0}}}}}},
        "joins": {"type": "array", "minItems": 3, "items": {
            "type": "object", "required": ["join", "left", "right", "full_edge", "contact_band", "seam"],
            "properties": {"join": {"enum": ["left_cap->middle", "middle->middle", "middle->right_cap"]},
                           "left": {"type": "string"}, "right": {"type": "string"},
                           "full_edge": {"type": "object"}, "contact_band": {"type": "object"},
                           "seam": {"$ref": "#/$defs/edgeSeam"}}}},
        "preview": {"type": "object", "required": ["path", "size", "sha256", "placements"],
                    "properties": {"path": {"$ref": "common.schema.json#/$defs/relPath"},
                                   "size": {"$ref": "common.schema.json#/$defs/size2"},
                                   "sha256": {"$ref": "common.schema.json#/$defs/sha256"},
                                   "placements": {"type": "array", "minItems": 5}}},
        "qc": {"type": "object", "required": ["passed", "structural_passed", "issues", "warnings"],
               "properties": {"passed": {"type": "boolean"}, "structural_passed": {"type": "boolean"},
                              "issues": {"type": "array", "items": {"type": "string"}},
                              "warnings": {"type": "array", "items": {"type": "string"}},
                              "max_seam_ratio": {"type": ["number", "null"], "exclusiveMinimum": 0}}},
        "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
    },
}
# Section 5 of handoff/B11-map-terrain-platform.md: map.schema.json $defs added by B11 (the terrain bundle
# is in tests/test_generate2dmap_terrain.py and imports SEAM from here).
PROPOSED_PLATFORM_DEFS = {"edgeSeam": SEAM, "platform_strip_v2": PLATFORM_STRIP_V2}


def proposed_map_errors(instance, name: str, extra_defs: dict | None = None) -> list[str]:
    """Validate against the vendored map.schema.json with B11's proposed $defs applied in memory."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    directory = SKILLS_DIR / "generate2dmap" / "references" / "schemas"
    schemas = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.schema.json")}
    patched = copy.deepcopy(schemas["map.schema.json"])
    additions = {**PROPOSED_PLATFORM_DEFS, **(extra_defs or {})}
    clash = sorted(set(patched["$defs"]) & set(additions))
    if clash:
        raise AssertionError(f"proposed $defs already exist in map.schema.json: {clash}")
    patched["$defs"].update(copy.deepcopy(additions))
    Draft202012Validator.check_schema(patched)
    schemas["map.schema.json"] = patched
    registry = Registry().with_resources(
        (schema["$id"], DRAFT202012.create_resource(schema)) for schema in schemas.values())
    validator = Draft202012Validator({"$ref": f"{patched['$id']}#/$defs/{name}"}, registry=registry)
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(map(str, error.absolute_path)))
    return [f"{error.json_path}: {error.message}" for error in errors]


# --------------------------------------------------------------------------- the fork's 16 tests

class PlatformStripTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = Image.new("RGBA", (12, 6), (199, 20, 80, 0))
        # Synthetic testing pixels, including native alpha and legitimate magenta.
        for x in range(12):
            for y in range(2, 6):
                self.source.putpixel((x, y), (10 + x * 10, 90 + y, 80, 255))
        self.source.putpixel((1, 0), (255, 0, 255, 128))
        self.spec = {"surface_y_px": 2, "collision_depth_px": 2, "pieces": [
            {"id": "left", "role": "left_cap", "source_box": [0, 0, 3, 6]},
            {"id": "mid", "role": "middle", "source_box": [3, 0, 8, 6]},
            {"id": "right", "role": "right_cap", "source_box": [8, 0, 12, 6]},
        ]}

    def args(self, *extra):
        self.source.save(self.root / "source.png")
        (self.root / "spec.json").write_text(json.dumps(self.spec), encoding="utf-8")
        return MODULE.build_parser().parse_args([
            "--input", str(self.root / "source.png"), "--spec", str(self.root / "spec.json"),
            "--output-dir", str(self.root / "out"), "--background-mode", "native_alpha",
            *extra,
        ])

    def test_native_rectangles_exact_pixels_hashes_and_three_middle_preview(self):
        """Port: schema v2 instead of v1; expected crops have RGB zeroed under alpha 0."""
        payload = MODULE.extract(self.args("--strict-qc"))
        self.assertTrue(payload["qc"]["passed"])
        self.assertEqual(payload["schema"], "generate2dmap.platform_strip.v2")
        self.assertEqual(payload["preview"]["size"], [22, 6])
        self.assertEqual([p["left"] for p in payload["preview"]["placements"]], [0, 3, 8, 13, 18])
        expected = []
        for piece in payload["pieces"]:
            crop = zero_hidden_rgb(self.source.crop(piece["source_box"]))
            with Image.open(self.root / "out" / piece["path"]) as actual:
                self.assertEqual(actual.size, crop.size)
                self.assertEqual(actual.tobytes(), crop.tobytes())
            self.assertEqual(piece["sha256"], hashlib.sha256((self.root / "out" / piece["path"]).read_bytes()).hexdigest())
            self.assertEqual(piece["anchor_px"], [0, 2])
            expected.append(crop)
        with Image.open(self.root / "out/strip-preview.png") as preview:
            for item, offset in zip([expected[0], expected[1], expected[1], expected[1], expected[2]], [0, 3, 8, 13, 18]):
                self.assertEqual(preview.crop((offset, 0, offset + item.width, 6)).tobytes(), item.tobytes())
        self.assertEqual(payload["source"]["sha256"], hashlib.sha256((self.root / "source.png").read_bytes()).hexdigest())

    def test_all_three_joins_are_measured_and_nonzero_rgb_is_diagnostic(self):
        payload = MODULE.extract(self.args("--strict-qc"))
        self.assertEqual([j["join"] for j in payload["joins"]], ["left_cap->middle", "middle->middle", "middle->right_cap"])
        self.assertTrue(all(j["full_edge"]["visible_rgb_mae"] > 0 for j in payload["joins"]))
        self.assertTrue(all(j["contact_band"]["solid_contact_fraction"] == 1 for j in payload["joins"]))

    def test_reviewed_rgb_threshold_failure_does_not_publish(self):
        with self.assertRaisesRegex(ValueError, "RGB edge MAE"):
            MODULE.extract(self.args("--strict-qc", "--max-seam-rgb-mae", "0"))
        self.assertFalse((self.root / "out").exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["source.png", "spec.json"])

    def test_reviewed_alpha_threshold_exposes_transparent_edge_discontinuity(self):
        self.source.putpixel((2, 0), (1, 1, 1, 255))
        with self.assertRaisesRegex(ValueError, "alpha edge MAE"):
            MODULE.extract(self.args("--strict-qc", "--max-seam-alpha-mae", "0"))
        self.assertFalse((self.root / "out").exists())

    def test_middle_transparent_join_column_is_structural_failure(self):
        for y in range(6):
            self.source.putpixel((3, y), (255, 0, 255, 0))
        with self.assertRaisesRegex(ValueError, "mid: structural"):
            MODULE.extract(self.args("--strict-qc"))
        self.assertFalse((self.root / "out").exists())

    def test_declared_surface_shift_caught_even_with_relaxed_band_coverage(self):
        self.source.putpixel((4, 2), (20, 30, 40, 0))
        with self.assertRaisesRegex(ValueError, "declared surface"):
            MODULE.extract(self.args("--strict-qc", "--min-column-coverage", "0.5"))

    def test_interior_structural_hole_reported_per_column(self):
        self.source.putpixel((5, 3), (10, 20, 30, 0))
        result = MODULE.extract(self.args())
        self.assertFalse(result["qc"]["passed"])
        coverage = result["pieces"][1]["coverage"]
        self.assertEqual(coverage["insufficient_columns"], [2])
        self.assertEqual(coverage["non_solid_band_pixels"], 1)
        self.assertEqual(coverage["solid_fraction_by_column"], [1, 1, 0.5, 1, 1])

    def test_cap_outer_padding_allowed_by_explicit_collision_span(self):
        for x in [0, 11]:
            for y in range(6):
                self.source.putpixel((x, y), (5, 5, 5, 0))
        self.spec["pieces"][0]["collision_span_px"] = [1, 3]
        self.spec["pieces"][2]["collision_span_px"] = [0, 3]
        result = MODULE.extract(self.args("--strict-qc"))
        self.assertEqual(result["pieces"][0]["collision_rect_px"], [1, 2, 2, 2])
        self.assertEqual(result["pieces"][0]["size"], [3, 6])
        self.assertTrue(result["qc"]["passed"])

    def test_span_cannot_hide_join_or_middle_holes(self):
        for index, span in [(0, [0, 2]), (1, [1, 5]), (2, [1, 4])]:
            with self.subTest(index=index):
                data = copy.deepcopy(self.spec)
                data["pieces"][index]["collision_span_px"] = span
                with self.assertRaisesRegex(ValueError, "reach every join"):
                    MODULE.validate_spec(data, self.source.size)

    def test_rejects_invalid_geometry_and_roles(self):
        mutations = [
            ("height", lambda d: d["pieces"][1].update(source_box=[3, 0, 8, 5])),
            ("bounds", lambda d: d["pieces"][1].update(source_box=[3, 0, 13, 6])),
            ("fractional", lambda d: d["pieces"][1].update(source_box=[3.5, 0, 8, 6])),
            ("role", lambda d: d["pieces"][1].update(role="left_cap")),
            ("duplicate", lambda d: d["pieces"][1].update(id="LEFT")),
            ("path", lambda d: d["pieces"][1].update(id="../outside")),
            ("reserved", lambda d: d["pieces"][1].update(id="strip-preview")),
            ("band", lambda d: d.update(collision_depth_px=5)),
        ]
        for name, mutate in mutations:
            with self.subTest(name=name):
                data = copy.deepcopy(self.spec)
                mutate(data)
                with self.assertRaises(ValueError):
                    MODULE.validate_spec(data, self.source.size)

    def test_background_modes_validate_alpha_without_geometry_changes(self):
        self.source = self.source.convert("RGB")
        with self.assertRaisesRegex(ValueError, "transparen"):
            MODULE.extract(self.args("--strict-qc"))
        result = MODULE.extract(self.args("--background-mode", "opaque", "--strict-qc"))
        self.assertTrue(result["qc"]["passed"])
        with Image.open(self.root / "out/left.png") as image:
            self.assertEqual(image.tobytes(), self.source.crop((0, 0, 3, 6)).convert("RGBA").tobytes())

    def test_opaque_mismatch_fails_without_output(self):
        with self.assertRaisesRegex(ValueError, "opaque"):
            MODULE.extract(self.args("--background-mode", "opaque"))
        self.assertFalse((self.root / "out").exists())

    def test_chroma_keys_background_without_removing_solid_boundary_pixels(self):
        pixels = np.array(self.source)
        pixels[pixels[:, :, 3] < 255] = [255, 0, 255, 255]
        self.source = Image.fromarray(pixels).convert("RGB")
        result = MODULE.extract(self.args("--background-mode", "chroma_key", "--strict-qc"))
        self.assertTrue(result["qc"]["passed"])
        with Image.open(self.root / "out/mid.png") as image:
            self.assertEqual(image.size, (5, 6))
            self.assertEqual(image.getpixel((0, 2)), self.source.getpixel((3, 2)) + (255,))
            self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_existing_output_preserved(self):
        args = self.args("--strict-qc")
        args.output_dir.mkdir()
        sentinel = args.output_dir / "keep.txt"
        sentinel.write_text("keep")
        with self.assertRaises(FileExistsError):
            MODULE.extract(args)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_competing_output_directory_cannot_be_replaced(self):
        """Port: the publish step now lives in forge_core.staged_output, so the race is injected there."""
        args = self.args("--strict-qc")
        original = MODULE.forge_core.publish_directory_no_replace
        def compete(stage, final):
            Path(final).mkdir()
            original(stage, final)
        with mock.patch.object(MODULE.forge_core, "publish_directory_no_replace", side_effect=compete):
            with self.assertRaises(FileExistsError):
                MODULE.extract(args)
        self.assertEqual(list(args.output_dir.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["out", "source.png", "spec.json"])

    def test_cli_import_is_independent_of_working_directory(self):
        self.args()
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--input", str(self.root / "source.png"),
                                 "--spec", str(self.root / "spec.json"), "--output-dir", str(self.root / "cli"),
                                 "--background-mode", "native_alpha", "--strict-qc"], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["qc"]["passed"])


# --------------------------------------------------------------------------- cfed170 regression tests (A0 split)

class PlatformTests(unittest.TestCase):
    def fixture(self, root, hole=False):
        image = Image.new("RGBA", (24, 8))
        for x in range(24):
            for y in range(2, 6):
                image.putpixel((x, y), (110, 80, 60, 255))
        if hole:
            image.putpixel((12, 2), (0, 0, 0, 0))
        image.save(root / "strip.png")
        spec = {"surface_y_px": 2, "collision_depth_px": 4,
                "pieces": [{"id": name, "role": role, "source_box": [index * 8, 0, (index + 1) * 8, 8]}
                           for index, (name, role) in enumerate(zip(("left", "mid", "right"), MODULE.ROLES))]}
        (root / "strip.json").write_text(json.dumps(spec), encoding="utf-8")
        return MODULE.build_parser().parse_args([
            "--input", str(root / "strip.png"), "--spec", str(root / "strip.json"),
            "--output-dir", str(root / "out"), "--background-mode", "native_alpha", "--strict-qc"])

    def test_fixed_geometry_and_repeated_middle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = MODULE.extract(self.fixture(root))
            self.assertTrue(result["qc"]["passed"])
            self.assertFalse(result["processing"]["resized"])
            self.assertEqual(result["preview"]["size"], [40, 8])
            self.assertEqual(result["pieces"][0]["anchor_px"], [0, 2])

    def test_structural_hole_fails_without_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "coverage fails"):
                MODULE.extract(self.fixture(root, hole=True))
            self.assertFalse((root / "out").exists())

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.fixture(root)
            (root / "out").mkdir()
            with self.assertRaises(FileExistsError):
                MODULE.extract(args)


# --------------------------------------------------------------------------- helpers for the B11 tests

class StripCase(unittest.TestCase):
    """A temporary project folder with helpers to write a strip and its spec."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, pixels, spec, name="strip"):
        image = pixels if isinstance(pixels, Image.Image) else Image.fromarray(np.asarray(pixels, np.uint8))
        image.save(self.root / f"{name}.png")
        (self.root / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")
        return self.root / f"{name}.png", self.root / f"{name}.json"

    def options(self, name="strip", out="kit", *extra):
        return MODULE.build_parser().parse_args([
            "--input", str(self.root / f"{name}.png"), "--spec", str(self.root / f"{name}.json"),
            "--output-dir", str(self.root / out), *extra])


def three_pieces(width, height, surface, depth, ids=("left", "mid", "right")):
    return {"surface_y_px": surface, "collision_depth_px": depth, "pieces": [
        {"id": ids[0], "role": "left_cap", "source_box": [0, 0, width, height]},
        {"id": ids[1], "role": "middle", "source_box": [width, 0, 2 * width, height]},
        {"id": ids[2], "role": "right_cap", "source_box": [2 * width, 0, 3 * width, height]}]}


# --------------------------------------------------------------------------- MAP-07: measured surface

class SurfaceTests(StripCase):
    def lip_strip(self):
        """repro_14: solid art starts at y=4 (grass lip rows 4-6), 8 px above a declared surface of 12."""
        pixels = np.zeros((32, 96, 4), np.uint8)
        pixels[4:7] = (70, 150, 60, 255)
        pixels[7:] = (110, 80, 60, 255)
        return pixels

    def test_repro_14_art_above_declared_surface_fails(self):
        """repro_14: a walkable surface declared 8 px inside the platform must fail strict QC."""
        self.write(self.lip_strip(), three_pieces(32, 32, 12, 8))
        with self.assertRaisesRegex(ValueError, "rises up to 8 px above the declared surface"):
            MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", "--strict-qc"))
        self.assertFalse((self.root / "kit").exists())
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha"))
        self.assertFalse(result["qc"]["passed"])
        surface = result["pieces"][1]["surface"]
        self.assertEqual(surface["measured_y_px_by_column"], [4] * 32)
        self.assertEqual(surface["max_rise_px"], 8)
        self.assertEqual(surface["columns_above_tolerance"], list(range(32)))
        self.assertEqual({c["id"]: c["status"] for c in result["qa"]["checks"]}["surface_rise_px"], "fail")

    def test_v_map07_taller_canvas_also_fails(self):
        """verify/repros/v_map07.py: solid from y=4 in a 40 px canvas, surface declared at 12."""
        pixels = np.zeros((40, 192, 4), np.uint8)
        pixels[4:] = (110, 90, 60, 255)
        self.write(pixels, three_pieces(64, 40, 12, 8, ("l", "m", "r")))
        with self.assertRaisesRegex(ValueError, "declared surface"):
            MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", "--strict-qc"))

    def test_true_surface_passes(self):
        self.write(self.lip_strip(), three_pieces(32, 32, 4, 8))
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", "--strict-qc"))
        self.assertTrue(result["qc"]["passed"])
        self.assertEqual(result["pieces"][0]["surface"]["max_rise_px"], 0)

    def tufted_strip(self):
        """Probe-map platform style: surface at 6, sparse 1-3 px grass tips above it in some columns."""
        pixels = np.zeros((24, 72, 4), np.uint8)
        pixels[6:] = (110, 80, 60, 255)
        for x in range(0, 72, 5):
            pixels[6 - 1 - x % 3:6, x] = (70, 150, 60, 255)
        return pixels

    def test_decoration_band_allows_grass_tips_and_tolerance_allows_small_bumps(self):
        self.write(self.tufted_strip(), three_pieces(24, 24, 6, 8))
        with self.assertRaisesRegex(ValueError, "rises up to 3 px"):
            MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", "--strict-qc"))
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", "--strict-qc",
                                             "--decoration-band-px", "3"))
        self.assertTrue(result["qc"]["passed"])
        self.assertGreater(result["pieces"][1]["surface"]["decoration_px"], 0)
        with self.assertRaisesRegex(ValueError, "tolerance 2 px"):
            MODULE.extract(self.options("strip", "kit2", "--background-mode", "native_alpha", "--strict-qc",
                                        "--surface-tolerance-px", "2"))
        result = MODULE.extract(self.options("strip", "kit3", "--background-mode", "native_alpha", "--strict-qc",
                                             "--surface-tolerance-px", "3"))
        self.assertTrue(result["qc"]["passed"])

    def test_art_above_the_decoration_band_still_fails(self):
        pixels = self.tufted_strip()
        pixels[0:2, 30] = (70, 150, 60, 255)  # a 6 px spike in the middle piece, taller than the 3 px band
        self.write(pixels, three_pieces(24, 24, 6, 8))
        with self.assertRaisesRegex(ValueError, "mid: art rises up to 6 px"):
            MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", "--strict-qc",
                                        "--decoration-band-px", "3"))

    def test_outer_cap_padding_is_not_measured(self):
        pixels = self.lip_strip()[:, :, :].copy()
        pixels[:, :4] = 0
        pixels[0:4, 1] = (90, 90, 90, 255)  # a post in the left cap's outer padding
        spec = three_pieces(32, 32, 4, 8)
        spec["pieces"][0]["collision_span_px"] = [4, 32]
        self.write(pixels, spec)
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", "--strict-qc"))
        self.assertTrue(result["qc"]["passed"])
        self.assertEqual(len(result["pieces"][0]["surface"]["measured_y_px_by_column"]), 28)

    def test_opaque_mode_records_surface_as_not_measurable(self):
        self.write(np.full((8, 24, 3), 120, np.uint8), three_pieces(8, 8, 3, 4))
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "opaque", "--strict-qc"))
        self.assertFalse(result["pieces"][0]["surface"]["measured"])
        self.assertEqual({c["id"]: c["status"] for c in result["qa"]["checks"]}["surface_rise_px"], "skipped")

    def test_negative_tolerance_rejected(self):
        self.write(self.lip_strip(), three_pieces(32, 32, 4, 8))
        for flag in ("--surface-tolerance-px", "--decoration-band-px"):
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, "zero or positive"):
                MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", flag, "-1"))


# --------------------------------------------------------------------------- MAP-19: palette and grey inputs

class InputModeTests(StripCase):
    def rgba(self):
        image = Image.new("RGBA", (24, 8), (0, 0, 0, 0))
        for x in range(24):
            for y in range(2, 8):
                image.putpixel((x, y), (110, 80, 60, 255))
        return image

    def test_repro_15_indexed_png_is_accepted(self):
        """repro_15: the literal palette PNG (quantize, then transparency=0) is accepted and its mode recorded.

        quantize() puts the brown at index 0, so that file makes the platform the transparent colour; it shows
        acceptance only. A palette whose transparent index is the background passes strict QC.
        """
        self.rgba().quantize(colors=4, method=Image.Quantize.FASTOCTREE).save(self.root / "strip.png", transparency=0)
        (self.root / "strip.json").write_text(json.dumps(three_pieces(8, 8, 2, 4, ("l", "m", "r"))), encoding="utf-8")
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha"))
        self.assertEqual(result["source"]["mode"], "P")
        self.assertIn("tRNS", result["source"]["conversion"])

        indexed = Image.new("P", (24, 8), 0)
        indexed.putpalette([0, 0, 0, 110, 80, 60] + [0] * 762)
        for x in range(24):
            for y in range(2, 8):
                indexed.putpixel((x, y), 1)
        indexed.save(self.root / "proper.png", transparency=0)
        (self.root / "proper.json").write_text(json.dumps(three_pieces(8, 8, 2, 4, ("l", "m", "r"))), encoding="utf-8")
        result = MODULE.extract(self.options("proper", "kit2", "--background-mode", "native_alpha", "--strict-qc"))
        self.assertTrue(result["qc"]["passed"])
        with Image.open(self.root / "kit2/m.png") as piece:
            self.assertEqual(piece.mode, "RGBA")
            self.assertEqual(piece.getpixel((3, 5)), (110, 80, 60, 255))
            self.assertEqual(piece.getpixel((3, 0)), (0, 0, 0, 0))

    def test_grey_alpha_and_grey_trns_inputs(self):
        """PNG has no palette+alpha mode; its grey forms are LA and L with a tRNS grey key."""
        grey = self.rgba().convert("LA")
        keyed = Image.fromarray(np.where(np.asarray(grey)[..., 1] > 0, np.asarray(grey)[..., 0], 7).astype(np.uint8))
        for mode, image, options in (("LA", grey, {}), ("L", keyed, {"transparency": 7})):
            with self.subTest(mode=mode):
                name = f"strip_{mode}"
                image.save(self.root / f"{name}.png", **options)
                (self.root / f"{name}.json").write_text(json.dumps(three_pieces(8, 8, 2, 4, ("l", "m", "r"))),
                                                         encoding="utf-8")
                result = MODULE.extract(self.options(name, f"kit_{mode}", "--background-mode", "native_alpha",
                                                     "--strict-qc"))
                self.assertTrue(result["qc"]["passed"])
                self.assertEqual(result["source"]["mode"], mode)
                with Image.open(self.root / f"kit_{mode}" / "m.png") as piece:
                    self.assertEqual(piece.getpixel((0, 0)), (0, 0, 0, 0))
                    self.assertEqual(piece.getpixel((0, 4))[3], 255)


# --------------------------------------------------------------------------- MAP-14: normalised seams

class SeamRatioTests(StripCase):
    def repeat(self, tile, tag):
        strip = np.concatenate([tile] * 3, axis=1).astype(np.uint8)
        self.write(strip, three_pieces(tile.shape[1], 16, 0, 8, ("l", "m", "r")), tag)

    @staticmethod
    def sine(period):
        v = 128 + 80 * np.sin(2 * np.pi * np.arange(32) / period)
        return np.repeat(np.stack([v, v * 0.8, v * 0.5], -1)[None], 16, axis=0)

    def test_repro_16_true_join_passes_and_duplicated_edge_is_flagged(self):
        """repro_16: a periodic strip passed raw-MAE gates only when it stuttered; the ratio fixes both."""
        self.repeat(self.sine(32), "seamless")
        result = MODULE.extract(self.options("seamless", "kit-a", "--background-mode", "opaque",
                                             "--max-seam-ratio", "1.25", "--strict-qc"))
        self.assertTrue(result["qc"]["passed"])
        for join in result["joins"]:
            self.assertEqual(join["seam"]["verdict"], "continuous")
            self.assertLess(join["seam"]["seam_ratio"], 1.25)
        with self.assertRaisesRegex(ValueError, "RGB edge MAE"):  # the legacy equality gate rejects the true join
            MODULE.extract(self.options("seamless", "kit-b", "--background-mode", "opaque",
                                        "--max-seam-rgb-mae", "10", "--strict-qc"))

        self.repeat(self.sine(31), "stutter")  # column 31 repeats column 0
        with self.assertRaisesRegex(ValueError, "duplicated edge"):
            MODULE.extract(self.options("stutter", "kit-c", "--background-mode", "opaque",
                                        "--max-seam-ratio", "1.25", "--strict-qc"))
        self.assertFalse((self.root / "kit-c").exists())
        legacy = MODULE.extract(self.options("stutter", "kit-d", "--background-mode", "opaque",
                                             "--max-seam-rgb-mae", "10", "--strict-qc"))
        self.assertTrue(legacy["qc"]["passed"])  # the old gate misses the stutter: the reason for MAP-14
        self.assertEqual({j["seam"]["verdict"] for j in legacy["joins"]}, {"duplicate_edge"})
        self.assertTrue(legacy["qc"]["warnings"])

    def test_repro_16_bricks_join_on_the_mortar_line_passes(self):
        mortar = (np.arange(32) % 32) < 3
        bricks = np.where(mortar[:, None], np.array([215, 205, 185]), np.array([150, 60, 40]))
        self.repeat(np.repeat(bricks[None], 16, axis=0), "bricks")
        result = MODULE.extract(self.options("bricks", "kit", "--background-mode", "opaque",
                                             "--max-seam-ratio", "1.25", "--strict-qc"))
        self.assertEqual([j["seam"]["seam_ratio"] for j in result["joins"]], [1.0, 1.0, 1.0])

    def test_real_seam_fails_only_when_gated(self):
        rng = np.random.default_rng(5)
        tile = rng.integers(90, 130, (16, 32, 3))
        strip = np.concatenate([tile, tile + 40, tile], axis=1)  # the middle is much brighter
        self.write(strip.astype(np.uint8), three_pieces(32, 16, 0, 8, ("l", "m", "r")), "seam")
        diagnostic = MODULE.extract(self.options("seam", "kit", "--background-mode", "opaque", "--strict-qc"))
        self.assertTrue(diagnostic["qc"]["passed"])
        self.assertEqual([j["seam"]["verdict"] for j in diagnostic["joins"]], ["seam", "continuous", "seam"])
        self.assertEqual(diagnostic["qa"]["status"], "warn")
        with self.assertRaisesRegex(ValueError, "normalised seam ratio"):
            MODULE.extract(self.options("seam", "kit2", "--background-mode", "opaque", "--max-seam-ratio", "1.25",
                                        "--strict-qc"))

    def test_partial_seam_in_the_grass_rows_is_found(self):
        rng = np.random.default_rng(5)
        base = rng.integers(90, 130, (32, 96, 3))
        base[:6, 32:64] = np.clip(base[:6, 32:64] + np.array([-40, 40, -20]), 0, 255)
        self.write(base.astype(np.uint8), three_pieces(32, 32, 0, 8, ("l", "m", "r")), "partial")
        result = MODULE.extract(self.options("partial", "kit", "--background-mode", "opaque"))
        self.assertEqual(result["joins"][0]["seam"]["verdict"], "seam")

    def test_seam_neighbourhood_ignores_outer_cap_padding(self):
        """A transparent outer column of a narrow cap (a huge alpha step) must not hide a seam at its join."""
        left = np.zeros((8, 6, 4), np.uint8)
        left[:, 1:] = (100, 100, 100, 255)  # column 0 is outer padding, within 8 steps of the join
        middle = np.zeros((8, 12, 4), np.uint8)
        middle[:] = (160, 100, 100, 255)
        right = middle.copy()
        spec = {"surface_y_px": 0, "collision_depth_px": 4, "pieces": [
            {"id": "l", "role": "left_cap", "source_box": [0, 0, 6, 8], "collision_span_px": [1, 6]},
            {"id": "m", "role": "middle", "source_box": [6, 0, 18, 8]},
            {"id": "r", "role": "right_cap", "source_box": [18, 0, 30, 8]}]}
        self.write(np.concatenate([left, middle, right], axis=1), spec, "padded")
        result = MODULE.extract(self.options("padded", "kit", "--background-mode", "native_alpha"))
        self.assertEqual(result["joins"][0]["seam"]["verdict"], "seam")
        unguarded = MODULE._local_edge_seam_report(left, middle)  # measuring the padding would hide it
        self.assertEqual(unguarded["verdict"], "continuous")

    def test_seam_ratio_must_be_positive(self):
        self.repeat(self.sine(32), "seamless")
        with self.assertRaisesRegex(ValueError, "max-seam-ratio"):
            MODULE.extract(self.options("seamless", "kit", "--background-mode", "opaque", "--max-seam-ratio", "0"))


# --------------------------------------------------------------------------- middle variants

class MiddleVariantTests(StripCase):
    def variant_spec(self, middles=2):
        pieces = [{"id": "left", "role": "left_cap", "source_box": [0, 0, 8, 8]}]
        pieces += [{"id": f"mid{index + 1}", "role": "middle", "source_box": [8 * (index + 1), 0, 8 * (index + 2), 8]}
                   for index in range(middles)]
        pieces.append({"id": "right", "role": "right_cap", "source_box": [8 * (middles + 1), 0, 8 * (middles + 2), 8]})
        return {"surface_y_px": 2, "collision_depth_px": 4, "pieces": pieces}

    def strip(self, middles=2):
        pixels = np.zeros((8, 8 * (middles + 2), 4), np.uint8)
        pixels[2:] = (110, 80, 60, 255)
        pixels[2:, 8:8 * (middles + 1):3] = (120, 90, 70, 255)  # a little texture
        return pixels

    def test_two_middle_variants_join_every_way(self):
        self.write(self.strip(2), self.variant_spec(2))
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", "--strict-qc"))
        self.assertEqual([p["role"] for p in result["pieces"]], ["left_cap", "middle", "middle", "right_cap"])
        pairs = [(j["left"], j["right"]) for j in result["joins"]]
        self.assertEqual(pairs, [("left", "mid1"), ("left", "mid2"), ("mid1", "mid1"), ("mid1", "mid2"),
                                 ("mid2", "mid1"), ("mid2", "mid2"), ("mid1", "right"), ("mid2", "right")])
        self.assertEqual([p["id"] for p in result["preview"]["placements"]], ["left", "mid1", "mid2", "mid1", "right"])
        self.assertEqual(result["preview"]["size"], [40, 8])
        for piece in result["pieces"]:
            self.assertTrue((self.root / "kit" / piece["path"]).is_file())

    def test_four_middles_each_appear_once_in_the_preview(self):
        self.write(self.strip(4), self.variant_spec(4))
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha"))
        self.assertEqual([p["id"] for p in result["preview"]["placements"]],
                         ["left", "mid1", "mid2", "mid3", "mid4", "right"])
        self.assertEqual(len(result["joins"]), 4 + 16 + 4)

    def test_variant_seam_issue_names_both_pieces(self):
        pixels = self.strip(2)
        pixels[2:, 16:24, :3] = (200, 80, 60)
        self.write(pixels, self.variant_spec(2))
        with self.assertRaisesRegex(ValueError, r"middle->middle \(mid1\|mid2\)"):
            MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", "--max-seam-ratio", "1.25",
                                        "--strict-qc"))

    def test_role_counts(self):
        cases = {
            "no middle": [p for p in self.variant_spec(1)["pieces"] if p["role"] != "middle"]
            + [{"id": "extra", "role": "left_cap", "source_box": [8, 0, 16, 8]}],
            "two right caps": self.variant_spec(1)["pieces"] + [{"id": "r2", "role": "right_cap",
                                                                  "source_box": [8, 0, 16, 8]}],
            "too many middles": self.variant_spec(17)["pieces"],
        }
        for name, pieces in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                MODULE.validate_spec({"surface_y_px": 2, "collision_depth_px": 4, "pieces": pieces}, (200, 8))


# --------------------------------------------------------------------------- the fork's despill hunk

def _fork_color_distance(rgb):
    r, g, b = rgb
    return math.sqrt((r - 255) ** 2 + g ** 2 + (b - 255) ** 2)


def _fork_remove_bg_magenta(img, threshold, edge_threshold):
    """Verbatim logic of the improved fork's extract_prop_pack.remove_bg_magenta (the oracle)."""
    img = img.convert("RGBA")
    pixels = img.load()
    width, height = img.size
    for x in range(width):
        for y in range(height):
            r, g, b, a = pixels[x, y]
            if a > 0 and _fork_color_distance((r, g, b)) < threshold:
                pixels[x, y] = (0, 0, 0, 0)
    visited = set()
    queue = deque()
    for x in range(width):
        queue.append((x, 0))
        queue.append((x, height - 1))
    for y in range(height):
        queue.append((0, y))
        queue.append((width - 1, y))
    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or x < 0 or x >= width or y < 0 or y >= height:
            continue
        visited.add((x, y))
        r, g, b, a = pixels[x, y]
        should_expand = a == 0
        if a > 0 and _fork_color_distance((r, g, b)) < edge_threshold:
            pixels[x, y] = (0, 0, 0, 0)
            should_expand = True
        if should_expand:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if (dx or dy) and (x + dx, y + dy) not in visited:
                        queue.append((x + dx, y + dy))
    return img


def _fork_despill_chroma_edges(img, radius):
    """Verbatim logic of the improved fork's despill_chroma_edges (margin 12) (the oracle)."""
    if radius == 0:
        return img
    pixels = np.array(img.convert("RGBA"))
    transparent = pixels[:, :, 3] == 0
    if not transparent.any():
        return img
    boundary = Image.fromarray(transparent.astype(np.uint8) * 255)
    nearby = np.asarray(boundary.filter(ImageFilter.MaxFilter(2 * radius + 1))) > 0
    red, green, blue = (pixels[:, :, channel].astype(np.int16) for channel in range(3))
    excess = np.minimum(red, blue) - green
    affected = nearby & (pixels[:, :, 3] > 0) & (excess > 12)
    pixels[:, :, 0][affected] = (red[affected] - excess[affected]).astype(np.uint8)
    pixels[:, :, 2][affected] = (blue[affected] - excess[affected]).astype(np.uint8)
    return Image.fromarray(pixels)


class DespillPortTests(StripCase):
    def test_chroma_despill_matches_the_fork_for_radius_0_to_3(self):
        """study-asf-improved validate_despill_port.py: identical pieces for --despill-radius 0..3."""
        atlas = np.zeros((16, 48, 3), np.uint8)
        atlas[:] = (255, 0, 255)
        atlas[4:16] = (90, 140, 60)
        atlas[3] = (120, 30, 125)      # dark magenta fringe above the contact top
        atlas[4, ::5] = (110, 40, 120)  # fringe pixels inside the top row
        spec = {"surface_y_px": 4, "collision_depth_px": 6, "pieces": [
            {"id": "left", "role": "left_cap", "source_box": [0, 0, 16, 16]},
            {"id": "mid", "role": "middle", "source_box": [16, 0, 32, 16]},
            {"id": "right", "role": "right_cap", "source_box": [32, 0, 48, 16]}]}
        self.write(atlas, spec)
        source = Image.fromarray(atlas)
        changed = []
        for radius in range(4):
            with self.subTest(radius=radius):
                result = MODULE.extract(self.options("strip", f"kit-r{radius}", "--despill-radius", str(radius)))
                oracle = zero_hidden_rgb(_fork_despill_chroma_edges(_fork_remove_bg_magenta(source, 100, 150), radius))
                for piece in result["pieces"]:
                    with Image.open(self.root / f"kit-r{radius}" / piece["path"]) as image:
                        self.assertEqual(image.tobytes(), oracle.crop(piece["source_box"]).tobytes())
                self.assertEqual(result["processing"]["despill_radius"], radius)
                changed.append(result["processing"].get("despill_changed_px", 0))
        self.assertEqual(changed[0], 0)
        self.assertGreater(changed[1], 0)

    def test_despill_is_ignored_outside_chroma(self):
        pixels = np.zeros((8, 24, 4), np.uint8)
        pixels[2:] = (200, 60, 210, 255)
        self.write(pixels, three_pieces(8, 8, 2, 4))
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha",
                                             "--despill-radius", "2"))
        self.assertEqual(result["processing"]["despill_radius"], 0)
        with Image.open(self.root / "kit/mid.png") as image:
            self.assertEqual(image.getpixel((0, 3)), (200, 60, 210, 255))

    def test_no_importlib_or_private_publish_remains(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for needle in ("importlib", "sprite_helpers", "ctypes", "renameat2", "tempfile"):
            self.assertNotIn(needle, source)
        self.assertIn("forge_core.staged_output", source)
        self.assertIn("forge_matte.legacy_hard_key", source)


# --------------------------------------------------------------------------- contract and QA envelope

class ContractTests(StripCase):
    def produce(self, *extra, middles=1):
        pixels = np.zeros((8, 8 * (middles + 2), 4), np.uint8)
        pixels[2:] = (110, 80, 60, 255)
        pieces = [{"id": "left", "role": "left_cap", "source_box": [0, 0, 8, 8], "collision_span_px": [1, 8]}]
        pieces += [{"id": f"m{i}", "role": "middle", "source_box": [8 * (i + 1), 0, 8 * (i + 2), 8]}
                   for i in range(middles)]
        pieces.append({"id": "right", "role": "right_cap", "source_box": [8 * (middles + 1), 0, 8 * (middles + 2), 8]})
        self.write(pixels, {"surface_y_px": 2, "collision_depth_px": 4, "pieces": pieces})
        result = MODULE.extract(self.options("strip", "kit", "--background-mode", "native_alpha", *extra))
        written = json.loads((self.root / "kit" / MODULE.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(written, json.loads(json.dumps(result)))
        return written

    def test_manifest_validates_against_the_proposed_contract(self):
        for middles, extra in ((1, ()), (3, ("--max-seam-ratio", "2", "--decoration-band-px", "1"))):
            with self.subTest(middles=middles):
                if (self.root / "kit").exists():
                    shutil.rmtree(self.root / "kit")
                manifest = self.produce(*extra, middles=middles)
                self.assertEqual(proposed_map_errors(manifest, "platform_strip_v2"), [])
                for join in manifest["joins"]:
                    self.assertEqual(proposed_map_errors(join["seam"], "edgeSeam"), [])

    def test_contract_rejects_v1_ids_absolute_paths_and_failed_checks_in_a_pass(self):
        manifest = self.produce()
        mutations = {
            "v1 id": lambda d: d.update(schema="generate2dmap.platform_strip.v1"),
            "absolute source": lambda d: d["source"].update(path="D:/art/strip.png"),
            "resized": lambda d: d["processing"].update(resized=True),
            "pass with a failed check": lambda d: d["qa"]["checks"][0].update(status="fail"),
            "unknown verdict": lambda d: d["joins"][0]["seam"].update(verdict="ok"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                document = copy.deepcopy(manifest)
                mutate(document)
                self.assertTrue(proposed_map_errors(document, "platform_strip_v2"))

    def test_paths_are_manifest_relative_and_hashes_bind_every_file(self):
        manifest = self.produce()
        self.assertEqual(manifest["source"]["path"], "../strip.png")
        self.assertEqual(manifest["spec"]["path"], "../strip.json")
        qa = manifest["qa"]
        self.assertEqual(qa["status"], "pass")
        self.assertEqual([ref["path"] for ref in qa["inputs"]], ["../strip.png", "../strip.json"])
        self.assertEqual(sorted(ref["path"] for ref in qa["outputs"]),
                         ["left.png", "m0.png", "right.png", "strip-preview.png"])
        for ref in qa["outputs"] + qa["inputs"]:
            path = (self.root / "kit" / ref["path"]).resolve()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), ref["sha256"])
            self.assertEqual(path.stat().st_size, ref["bytes"])
        self.assertTrue(qa["notProven"])
        self.assertTrue(qa["method"])

    def test_input_on_another_drive_records_the_file_name(self):
        """forge_core.portable_path returns an absolute path when no relative route exists (A1)."""
        for absolute in ("Z:/art/strip.png", "/mnt/art/strip.png"):
            with self.subTest(absolute=absolute), \
                    mock.patch.object(MODULE.forge_core, "portable_path", return_value=absolute):
                reference = MODULE._local_file_ref(Path("strip.png"), self.root, "0" * 64, 1)
                self.assertEqual(reference, {"path": "strip.png", "sha256": "0" * 64, "bytes": 1})

    def test_output_is_deterministic(self):
        first = self.produce()
        shutil.rmtree(self.root / "kit")
        second = self.produce()
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- CLI conventions (plan Appendix D)

class CliTests(StripCase):
    def setUp(self):
        super().setUp()
        pixels = np.zeros((8, 24, 4), np.uint8)
        pixels[2:] = (110, 80, 60, 255)
        self.write(pixels, three_pieces(8, 8, 2, 4))

    def argv(self, out, *extra):
        return [SCRIPT, "--input", self.root / "strip.png", "--spec", self.root / "strip.json",
                "--output-dir", self.root / out, "--background-mode", "native_alpha", *extra]

    def test_help_is_ascii_under_cp1252_and_cp950(self):
        assert_cli_help("generate2dmap", "extract_platform_strip")

    def test_refuses_existing_output(self):
        (self.root / "kit").mkdir()
        (self.root / "kit" / "keep.txt").write_text("keep", encoding="utf-8")
        result = run_cli(self.argv("kit"), "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("error: "), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(sorted(p.name for p in (self.root / "kit").iterdir()), ["keep.txt"])

    def test_strict_qc_failure_publishes_nothing(self):
        pixels = np.zeros((8, 24, 4), np.uint8)
        pixels[0:] = (110, 80, 60, 255)  # art rises 2 px above the declared surface
        pixels[0, 0] = 0
        self.write(pixels, three_pieces(8, 8, 2, 4))
        result = run_cli(self.argv("kit", "--strict-qc"), "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: Platform strip QC failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["strip.json", "strip.png"])

    def test_success_prints_one_ascii_json_line(self):
        result = run_cli(self.argv("kit", "--strict-qc"), "cp1252")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].isascii())
        summary = json.loads(lines[0])
        self.assertEqual(Path(summary["manifest"]), (self.root / "kit" / "platform-strip.json").absolute())
        self.assertEqual(summary["status"], "pass")
        self.assertTrue(summary["qc"]["passed"])

    def test_bad_inputs_are_friendly_errors(self):
        (self.root / "broken.json").write_text("{not json", encoding="utf-8")
        cases = {
            "missing input": [SCRIPT, "--input", self.root / "nope.png", "--spec", self.root / "strip.json",
                              "--output-dir", self.root / "k1"],
            "broken spec": [SCRIPT, "--input", self.root / "strip.png", "--spec", self.root / "broken.json",
                            "--output-dir", self.root / "k2"],
            "bad threshold": self.argv("k3", "--max-seam-alpha-mae", "-1"),
        }
        for name, argv in cases.items():
            with self.subTest(name=name):
                result = run_cli(argv, "cp1252")
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.startswith("error: "), result.stderr)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
