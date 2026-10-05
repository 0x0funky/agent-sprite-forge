"""Behavioral regression tests for parallax coverage (validate_parallax)."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, run_cli,
                             script_path)


PARALLAX = load_script("generate2dmap", "validate_parallax")
SCRIPT = script_path("generate2dmap", "validate_parallax")
MODULE = PARALLAX
FORGE_CORE = load_script("generate2dmap", "forge_core")
SKILL = "generate2dmap"

# The $defs this module asks integration to add to map.schema.json (handoff section 5), applied in memory.
_POINT = {"$ref": "common.schema.json#/$defs/point2"}
_PAIR = {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 2, "maxItems": 2}
_BOOL_PAIR = {"type": "array", "items": {"type": "boolean"}, "minItems": 2, "maxItems": 2}
_STRINGS = {"type": "array", "items": {"type": "string"}}
_RATIO = {"type": "number", "minimum": 0}
REQUESTED_MAP_DEFS = {
    "parallaxSeam": {
        "description": "Wrap-seam diagnostics of one repeat axis: v1 edge-equality numbers plus forge_core.seam_report "
                       "over the columns (x) or rows (y) and a verdict. Never a seamlessness proof.",
        "type": "object", "required": ["axis", "alpha_mae", "premultiplied_rgb_mae", "seamless_verified"],
        "properties": {
            "axis": {"enum": ["x", "y"]}, "alpha_mae": _RATIO,
            "visible_rgb_mae": {"anyOf": [_RATIO, {"type": "null"}]},
            "jointly_visible_pixels": {"type": "integer", "minimum": 0}, "premultiplied_rgb_mae": _RATIO,
            "seamless_verified": {"const": False},
            "verdict": {"enum": ["continuous", "seam", "duplicate-edge", "flat", "too-small"]},
            "frames": {"type": "integer", "minimum": 2}, "seam": _RATIO, "adjacent_median": _RATIO,
            "adjacent_p95": _RATIO, "adjacent_max": _RATIO, "seam_over_median": _RATIO, "seam_over_p95": _RATIO,
            "method": {"type": "string", "minLength": 1}}},
    "parallaxExtreme": {
        "type": "object",
        "required": ["camera", "zoom", "screen_canvas_rect", "canvas_coverage_axes", "canvas_covers_viewport"],
        "properties": {
            "camera": _POINT, "zoom": {"type": "number", "exclusiveMinimum": 0},
            "screen_canvas_rect": {"$ref": "common.schema.json#/$defs/box"},
            "canvas_coverage_axes": _BOOL_PAIR, "canvas_covers_viewport": {"type": "boolean"},
            "canvas_visible": {"type": "boolean"},
            "gaps_px": {"type": "array", "items": _RATIO, "minItems": 4, "maxItems": 4},
            "nonzero_alpha_bbox_screen": {"anyOf": [{"$ref": "common.schema.json#/$defs/box"}, {"type": "null"}]}}},
    "parallaxLayerReport": {
        "type": "object",
        "required": ["id", "role", "image", "source_size", "scale", "display_size", "repeat", "alpha",
                     "require_canvas_coverage", "canvas_coverage_passed", "extrema", "repeat_seams", "passed",
                     "issues"],
        "properties": {
            "id": {"type": "string", "minLength": 1}, "role": {"type": "string", "minLength": 1},
            "image": {"type": "string", "minLength": 1},
            "source_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
            "source_size": {"$ref": "common.schema.json#/$defs/size2"},
            "scale": {"type": "number", "exclusiveMinimum": 0}, "display_size": _PAIR, "repeat": _BOOL_PAIR,
            "require_canvas_coverage": {"type": "boolean"}, "coverage_rule": {"type": "string"},
            "canvas_coverage_passed": {"type": "boolean"}, "opaque_viewport_coverage_verified": {"type": "boolean"},
            "extrema": {"type": "array", "items": {"$ref": "#/$defs/parallaxExtreme"}},
            "repeat_seams": {"type": "array", "items": {"$ref": "#/$defs/parallaxSeam"}},
            "pixel_grid": {"type": "object",
                           "required": ["integer_pixels", "rest_position_integral", "subpixel_scroll"]},
            "passed": {"type": "boolean"}, "issues": _STRINGS}},
    "parallax_validation_v2": {
        "description": "validate_parallax.py result (stdout and --report): verdict, issues and warnings, the "
                       "resolved pivot and coverage policy, per-layer coverage at every camera/zoom extreme, seam "
                       "diagnostics, the pixel grid and the aspect sweep. A malformed plan gives only schema, "
                       "passed false and issues.",
        "type": "object", "required": ["schema", "passed", "issues"],
        "properties": {
            "schema": {"enum": ["generate2dmap.parallax_validation.v1", "generate2dmap.parallax_validation.v2"]},
            "passed": {"type": "boolean"}, "issues": _STRINGS, "warnings": _STRINGS, "viewport": _PAIR,
            "camera": {"type": "object", "properties": {"x": _POINT, "y": _POINT, "zoom": _PAIR}},
            "pivot": {"type": "object", "required": ["mode", "point"],
                      "properties": {"mode": {"enum": ["top-left", "center", "point"]}, "point": _POINT}},
            "coverage_policy": {"enum": ["auto", "sky-only", "all"]},
            "camera_extrema_checked": {"type": "integer", "minimum": 1},
            "layers": {"type": "array", "items": {"$ref": "#/$defs/parallaxLayerReport"}},
            "aspect_sweep": {"type": "object", "required": ["policy", "aspects"], "properties": {
                "policy": {"enum": ["expand", "fixed-height", "fixed-width"]},
                "aspects": {"type": "array", "items": {
                    "type": "object", "required": ["aspect", "ratio", "viewport", "required", "passed",
                                                   "failing_layers"],
                    "properties": {"aspect": {"type": "string"}, "ratio": {"type": "number", "exclusiveMinimum": 0},
                                   "viewport": _PAIR, "camera_shift": _POINT, "required": {"type": "boolean"},
                                   "passed": {"type": "boolean"}, "failing_layers": {"type": "array"}}}}}},
            "pixel_grid": {"type": "object", "required": ["enforced"],
                           "properties": {"enforced": {"type": "boolean"},
                                          "source": {"type": ["string", "null"]}}},
            "transform": {"type": "string"}, "limits": _STRINGS, "report": {"type": ["string", "null"]}},
        "if": {"properties": {"passed": {"const": True}}, "required": ["passed"]},
        "then": {"properties": {"issues": {"maxItems": 0}}}},
}
# Optional plan fields this module reads, requested for map.schema.json $defs/parallax_plan/properties.
REQUESTED_PLAN_PROPERTIES = {
    "pixel_art": {"type": "boolean"},
    "sampling": {"$ref": "common.schema.json#/$defs/sampling"},
    "aspects": {"type": "array", "items": {"type": "string", "pattern": r"^[0-9]*\.?[0-9]+([:/][0-9]*\.?[0-9]+)?$"}},
    "aspect_policy": {"enum": ["expand", "fixed-height", "fixed-width"]},
}


def requested_contract_errors(document, name="parallax_validation_v2"):
    """Errors against the vendored generate2dmap schemas plus this module's requests (handoff section 5)."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    folder = SKILLS_DIR / SKILL / "references" / "schemas"
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.schema.json"))]
    map_schema = next(schema for schema in schemas if schema["$id"].endswith("/map.schema.json"))
    for key, fragment in REQUESTED_MAP_DEFS.items():
        map_schema["$defs"].setdefault(key, fragment)
    for key, fragment in REQUESTED_PLAN_PROPERTIES.items():
        map_schema["$defs"]["parallax_plan"]["properties"].setdefault(key, fragment)
    registry = Registry().with_resources((schema["$id"], DRAFT202012.create_resource(schema)) for schema in schemas)
    validator = Draft202012Validator({"$ref": f"{map_schema['$id']}#/$defs/{name}"}, registry=registry)
    return [f"{error.json_path}: {error.message}" for error in validator.iter_errors(document)]


def parallax_cli(*arguments, cwd=None):
    return run_cli([SCRIPT, *(str(argument) for argument in arguments)], cwd=cwd)


def strip(columns, height=8):
    """An opaque RGBA strip whose column i has the colour columns[i] (an (N, 3) array)."""
    columns = np.asarray(columns, np.float64)
    pixels = np.zeros((height, len(columns), 4), np.uint8)
    pixels[..., :3] = np.clip(np.round(columns), 0, 255).astype(np.uint8)[None]
    pixels[..., 3] = 255
    return Image.fromarray(pixels)


class ParallaxTests(unittest.TestCase):
    def plan(self):
        return {"viewport": [32, 16], "camera": {"x": [0, 64], "zoom": [1, 2]},
                "layers": [{"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque", "scroll_factor": [0, 0]},
                           {"id": "near", "role": "foreground", "image": "near.png", "alpha": "transparent", "repeat": [True, False]}]}

    def fixture(self, root):
        Image.new("RGB", (32, 16), "blue").save(root / "sky.png")
        layer = Image.new("RGBA", (32, 16))
        layer.putpixel((5, 5), (10, 200, 10, 255))
        layer.save(root / "near.png")

    def test_opaque_sky_and_real_alpha_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            report = PARALLAX.validate_plan(self.plan(), root)
            self.assertTrue(report["passed"])
            self.assertFalse(report["layers"][1]["repeat_seams"][0]["seamless_verified"])

    def test_camera_extreme_gap_is_found(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            plan = self.plan()
            plan["layers"][0]["scroll_factor"] = [1, 0]
            report = PARALLAX.validate_plan(plan, root)
            self.assertFalse(report["passed"])
            self.assertTrue(any("cover" in issue for issue in report["issues"]))

    def test_fake_alpha_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            Image.new("RGB", (32, 16), "gray").save(root / "near.png")
            self.assertFalse(PARALLAX.validate_plan(self.plan(), root)["passed"])

    def test_report_cannot_alias_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, "aliases"):
                PARALLAX.check_report_destination(root / "sky.png", root / "plan.json", self.plan())


class ForkParallaxTests(unittest.TestCase):
    """The improved fork's 22 parallax tests (B12-T1), bodies unchanged except module loading. Only the CLI
    test that re-ran onto the same --report path changed: reports never replace a file (Appendix D)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        Image.new("RGB", (12, 6), "navy").save(self.root / "sky.png")
        cloud = Image.new("RGBA", (8, 4))
        cloud.putpixel((2, 1), (100, 150, 200, 255))
        cloud.putpixel((3, 1), (100, 150, 200, 128))
        cloud.save(self.root / "cloud.png")
        self.plan = {"viewport": [12, 6], "camera": {"x": [0, 12], "y": [0, 0], "zoom": [1, 1]},
                     "layers": [{"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque",
                                 "scale": 1, "offset": [0, 0], "scroll_factor": [0, 0], "repeat": [False, False]}]}

    def validate(self):
        return MODULE.validate_plan(self.plan, self.root)

    def add_cloud(self, **overrides):
        layer = {"id": "cloud", "role": "far_bg", "image": "cloud.png", "alpha": "transparent",
                 "scroll_factor": [0.2, 0], "scale": 1, "offset": [0, 0]}
        layer.update(overrides)
        self.plan["layers"].append(layer)
        return layer

    def test_stationary_opaque_sky_and_transparent_layer_report_actual_alpha(self):
        self.add_cloud()
        result = self.validate()
        self.assertTrue(result["passed"])
        self.assertTrue(result["layers"][0]["opaque_viewport_coverage_verified"])
        layer = result["layers"][1]
        self.assertEqual(layer["source_size"], [8, 4])
        self.assertEqual(layer["alpha"]["fully_clear_pixels"], 30)
        self.assertEqual(layer["alpha"]["partly_transparent_pixels"], 1)
        self.assertEqual(layer["alpha"]["nonzero_bbox_px"], [2, 1, 4, 2])
        self.assertFalse(layer["opaque_viewport_coverage_verified"])
        self.assertFalse(layer["canvas_coverage_passed"])

    def test_finite_sky_too_short_at_camera_end_fails(self):
        self.plan["layers"][0]["scroll_factor"] = [0.5, 0]
        result = self.validate()
        self.assertFalse(result["passed"])
        self.assertEqual([s["canvas_covers_viewport"] for s in result["layers"][0]["extrema"]], [True, False])

    def test_negative_scroll_and_vertical_camera_ranges_check_all_extrema(self):
        self.plan["camera"] = {"x": [-2, 3], "y": [-1, 4], "zoom": [1, 2]}
        self.plan["layers"][0].update(scale=3, offset=[-4, -6], scroll_factor=[-1, -1])
        result = self.validate()
        self.assertEqual(result["camera_extrema_checked"], 8)
        self.assertTrue(result["passed"])
        self.plan["layers"][0]["offset"] = [0, 0]
        self.assertFalse(self.validate()["passed"])

    def test_zoom_out_may_reveal_gap_even_when_zoom_one_passes(self):
        self.plan["camera"]["zoom"] = [0.5, 1]
        result = self.validate()
        self.assertFalse(result["passed"])
        self.assertEqual(result["layers"][0]["extrema"][0]["screen_canvas_rect"], [0, 0, 6, 3])

    def test_anchor_is_source_pixels_and_uniform_scale_applies_once(self):
        self.plan["camera"] = {"x": [0, 0], "y": [0, 0], "zoom": [2, 2]}
        self.plan["layers"][0].update(scale=2, anchor_px=[3, 2], offset=[6, 4])
        result = self.validate()
        self.assertTrue(result["passed"])
        layer = result["layers"][0]
        self.assertEqual(layer["display_size"], [24, 12])
        self.assertEqual(layer["extrema"][0]["screen_canvas_rect"], [0, 0, 48, 24])

    def test_repetition_uses_native_display_period_and_does_not_certify_seam(self):
        sky = self.plan["layers"][0]
        sky.update(scale=0.5, repeat=[True, True], offset=[1000, -2000], scroll_factor=[-5, 8])
        result = self.validate()
        self.assertTrue(result["passed"])
        layer = result["layers"][0]
        self.assertEqual(layer["repeat_period_world_px"], [6, 3])
        self.assertEqual(len(layer["repeat_seams"]), 2)
        self.assertTrue(all(not m["seamless_verified"] for m in layer["repeat_seams"]))

    def test_repeat_one_axis_does_not_hide_finite_other_axis_gap(self):
        self.plan["layers"][0].update(scale=0.5, repeat=[True, False])
        self.assertFalse(self.validate()["passed"])

    def test_foreground_fully_opaque_rejected_even_if_declared_opaque(self):
        self.add_cloud(role="foreground", image="sky.png", alpha="opaque")
        result = self.validate()
        self.assertFalse(result["passed"])
        self.assertTrue(any("Foreground overlays" in issue for issue in result["issues"]))

    def test_rgb_checkerboard_does_not_satisfy_transparent_declaration(self):
        self.add_cloud(image="sky.png")
        self.assertFalse(self.validate()["passed"])

    def test_fully_empty_overlay_requires_explicit_allow_empty(self):
        Image.new("RGBA", (8, 4)).save(self.root / "cloud.png")
        cloud = self.add_cloud()
        self.assertFalse(self.validate()["passed"])
        cloud["allow_empty"] = True
        result = self.validate()
        self.assertTrue(result["passed"])
        self.assertIsNone(result["layers"][1]["alpha"]["nonzero_bbox_px"])

    def test_transparent_canvas_full_coverage_not_content_coverage(self):
        self.add_cloud(scale=3, scroll_factor=[0, 0], require_canvas_coverage=True)
        result = self.validate()
        self.assertTrue(result["passed"])
        layer = result["layers"][1]
        self.assertTrue(layer["canvas_coverage_passed"])
        self.assertFalse(layer["opaque_viewport_coverage_verified"])
        self.assertEqual(layer["extrema"][0]["nonzero_alpha_bbox_screen"], [6, 3, 12, 6])

    def test_sky_coverage_cannot_be_disabled_or_replaced_by_transparent_image(self):
        self.plan["layers"][0].update(scale=0.5, require_canvas_coverage=False)
        self.assertFalse(self.validate()["passed"])
        self.plan["layers"][0].update(scale=1, image="cloud.png", alpha="transparent", repeat=[True, True])
        self.assertFalse(self.validate()["passed"])

    def test_nonfinite_invalid_zoom_and_independent_display_size_rejected(self):
        for mutation in [lambda p: p["camera"].update(zoom=[0, 1]),
                         lambda p: p["camera"].update(x=[2, 1]),
                         lambda p: p["layers"][0].update(scale=float("nan")),
                         lambda p: p["layers"][0].update(scroll_factor=[float("inf"), 0]),
                         lambda p: p["layers"][0].update(alpha=[]),
                         lambda p: p["layers"][0].update(display_size=[12, 6]),
                         lambda p: p["layers"][0].update(repeat=["false", False])]:
            plan = copy.deepcopy(self.plan)
            mutation(plan)
            with self.assertRaises(ValueError):
                MODULE.validate_plan(plan, self.root)

    def test_transformed_bounds_overflow_rejected_instead_of_json_infinity(self):
        self.plan["layers"][0].update(scale=1e307, offset=[1e308, 0])
        with self.assertRaisesRegex(ValueError, "bounds are not finite"):
            self.validate()

    def test_exactly_one_sky_required(self):
        original = self.plan["layers"].pop()
        self.add_cloud()
        self.assertFalse(self.validate()["passed"])
        self.plan["layers"] = [original, {**original, "id": "another-sky"}]
        self.assertFalse(self.validate()["passed"])

    def test_repeat_rgb_and_alpha_differences_reported_without_automatic_failure(self):
        cloud = Image.new("RGBA", (8, 6))
        for y in range(6):
            cloud.putpixel((0, y), (255, 0, 0, 255))
            cloud.putpixel((7, y), (0, 0, 255, 128))
        cloud.save(self.root / "cloud.png")
        self.add_cloud(repeat=[True, False])
        result = self.validate()
        self.assertTrue(result["passed"])
        metrics = result["layers"][1]["repeat_seams"][0]
        self.assertEqual(metrics["alpha_mae"], 127)
        self.assertEqual(metrics["visible_rgb_mae"], 170)
        self.assertFalse(metrics["seamless_verified"])

    def test_cli_resolves_images_relative_to_spec_and_has_failure_exit_status(self):
        path = self.root / "plan.json"
        path.write_text(json.dumps(self.plan), encoding="utf-8")
        command = [SCRIPT, "--spec", str(path), "--report", str(self.root / "report.json")]
        run = run_cli(command, cwd=SCRIPT.parent)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(json.loads((self.root / "report.json").read_text())["passed"])
        self.plan["layers"][0]["scale"] = 0.1
        path.write_text(json.dumps(self.plan), encoding="utf-8")
        # Reports never replace a file (Appendix D), so the second run needs its own report path.
        command[-1] = str(self.root / "report-2.json")
        run = run_cli(command, cwd=SCRIPT.parent)
        self.assertEqual(run.returncode, 1)
        self.assertFalse(json.loads(run.stdout)["passed"])
        self.assertFalse(json.loads((self.root / "report-2.json").read_text())["passed"])
        self.assertTrue(json.loads((self.root / "report.json").read_text())["passed"])

    def assert_cli_rejects_input_report(self, report, plan=None):
        path = self.root / "plan.json"
        path.write_text(json.dumps(self.plan if plan is None else plan), encoding="utf-8")
        originals = {p: p.read_bytes() for p in (path, self.root / "sky.png", self.root / "cloud.png")}
        run = run_cli([SCRIPT, "--spec", "plan.json", "--report", str(report)], cwd=self.root)
        self.assertEqual(run.returncode, 1, run.stderr)
        result = json.loads(run.stdout)
        self.assertFalse(result["passed"])
        self.assertTrue(any("Report path aliases an input file" in issue for issue in result["issues"]), result)
        for source, original in originals.items():
            self.assertEqual(source.read_bytes(), original, str(source))

    def test_cli_rejects_spec_report_and_relative_path_alias(self):
        (self.root / "nested").mkdir()
        for report in (self.root / "plan.json", Path("nested") / ".." / "plan.json"):
            with self.subTest(report=report):
                self.assert_cli_rejects_input_report(report)

    def test_cli_rejects_any_source_report_even_if_validation_fails_early(self):
        self.add_cloud(image="nested/../cloud.png")
        (self.root / "nested").mkdir()
        for report, invalid in ((self.root / "sky.png", False), (Path("cloud.png"), False),
                                (Path("cloud.png"), True)):
            with self.subTest(report=report, invalid=invalid):
                plan = copy.deepcopy(self.plan)
                if invalid:
                    plan["viewport"] = [0, 6]
                self.assert_cli_rejects_input_report(report, plan)

    def test_cli_rejects_symlink_report_aliases(self):
        (self.root / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")
        for source in ("plan.json", "sky.png"):
            alias = self.root / f"link-{source}"
            try:
                alias.symlink_to(self.root / source)
            except OSError as error:
                self.skipTest(f"File symlinks unavailable: {error}")
            with self.subTest(source=source):
                self.assert_cli_rejects_input_report(alias)

    def test_cli_rejects_hardlink_report_alias(self):
        alias = self.root / "linked-sky.png"
        alias.hardlink_to(self.root / "sky.png")
        self.assert_cli_rejects_input_report(alias)

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive path behavior")
    def test_cli_rejects_windows_case_aliases(self):
        for source in ("plan.json", "sky.png"):
            with self.subTest(source=source):
                self.assert_cli_rejects_input_report(str(self.root / source).upper())


class PivotTests(unittest.TestCase):
    """B12-T4: camera.pivot (MAP-08). The zoom origin changes which side a zoom-out uncovers."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        Image.new("RGB", (1200, 600), "blue").save(self.root / "wide_sky.png")

    def repro_17_plan(self, pivot=None, zoom=(0.75, 1)):
        """repro_17 part B: a 320x180 view, a 1200x600 sky at offset -40 and a zoom range."""
        camera = {"x": [0, 0], "zoom": list(zoom)}
        if pivot is not None:
            camera["pivot"] = pivot
        return {"viewport": [320, 180], "camera": camera,
                "layers": [{"id": "sky", "role": "sky", "image": "wide_sky.png", "alpha": "opaque",
                            "offset": [-40, -40], "scroll_factor": [0, 0]}]}

    def test_centre_pivot_catches_the_10_px_gap_at_zoom_0_75(self):
        """Acceptance: the plan passes with the top-left pivot, a centred camera shows a 10 px gap at z=0.75."""
        self.assertTrue(PARALLAX.validate_plan(self.repro_17_plan(), self.root)["passed"])
        result = PARALLAX.validate_plan(self.repro_17_plan("center"), self.root)
        self.assertFalse(result["passed"])
        self.assertEqual(result["pivot"], {"mode": "center", "point": [160.0, 90.0]})
        extremes = {sample["zoom"]: sample for sample in result["layers"][0]["extrema"]}
        self.assertEqual(extremes[0.75]["gaps_px"], [10.0, 0.0, 0.0, 0.0])
        self.assertEqual(extremes[0.75]["screen_canvas_rect"], [10.0, -7.5, 910.0, 442.5])
        self.assertTrue(extremes[1.0]["canvas_covers_viewport"])
        self.assertIn("worst gap 10 px", result["issues"][0])
        result = PARALLAX.validate_plan(self.repro_17_plan("center", zoom=(0.5, 1)), self.root)
        self.assertEqual(max(result["layers"][0]["extrema"][0]["gaps_px"]), 60.0)

    def test_point_pivot_and_invalid_pivots(self):
        same = PARALLAX.validate_plan(self.repro_17_plan([0, 0]), self.root)
        top_left = PARALLAX.validate_plan(self.repro_17_plan(), self.root)
        self.assertEqual(same["layers"][0]["extrema"], top_left["layers"][0]["extrema"])
        corner = PARALLAX.validate_plan(self.repro_17_plan([320, 180]), self.root)
        self.assertEqual(corner["pivot"]["mode"], "point")
        self.assertEqual(corner["layers"][0]["extrema"][0]["gaps_px"][0], 50.0)
        for pivot in ("middle", [1, "2"], [float("nan"), 0], [1, 2, 3]):
            with self.subTest(pivot=pivot), self.assertRaises(ValueError):
                PARALLAX.validate_plan(self.repro_17_plan(pivot), self.root)

    def test_corner_checks_stay_exact_under_every_pivot(self):
        """repro_17 part A with pivots: whenever the validator passes, no interior camera/zoom sample has a gap."""
        Image.new("RGB", (64, 32), "blue").save(self.root / "sky.png")
        rng = np.random.default_rng(1)
        passes = 0
        for trial in range(240):
            viewport = [float(rng.choice([32, 48, 64])), float(rng.choice([16, 24, 32]))]
            point = [float(rng.uniform(0, viewport[0])), float(rng.uniform(0, viewport[1]))]
            pivot = ["top-left", "center", point][trial % 3]
            camera = {"x": sorted(rng.uniform(-50, 150, 2).tolist()), "y": sorted(rng.uniform(-20, 20, 2).tolist()),
                      "zoom": sorted(rng.uniform(0.5, 2.0, 2).tolist()), "pivot": pivot}
            layer = {"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque",
                     "scale": float(rng.uniform(0.5, 4)), "offset": rng.uniform([-100, -60], [20, 10]).tolist(),
                     "anchor_px": rng.uniform([0, 0], [64, 32]).tolist(),
                     "scroll_factor": rng.uniform(-1, 1.5, 2).tolist()}
            result = PARALLAX.validate_plan({"viewport": viewport, "camera": camera, "layers": [layer]}, self.root,
                                            aspects=[])
            if not result["passed"]:
                continue
            passes += 1
            point = result["pivot"]["point"]
            cx, cy, zoom = np.meshgrid(np.linspace(*camera["x"], 9), np.linspace(*camera["y"], 5),
                                       np.linspace(*camera["zoom"], 9), indexing="ij")
            base = [layer["offset"][i] - layer["anchor_px"][i] * layer["scale"] for i in range(2)]
            left = point[0] + (base[0] - cx * layer["scroll_factor"][0] - point[0]) * zoom
            top = point[1] + (base[1] - cy * layer["scroll_factor"][1] - point[1]) * zoom
            gaps = ((left > 1e-6) | (top > 1e-6) | (left + 64 * layer["scale"] * zoom < viewport[0] - 1e-6)
                    | (top + 32 * layer["scale"] * zoom < viewport[1] - 1e-6))
            self.assertFalse(gaps.any(), f"trial {trial}: interior gap despite a pass")
        self.assertGreater(passes, 20)


class CoverageDefaultTests(unittest.TestCase):
    """B12-T4: coverage is required for repeated and near layers by default; --coverage sky-only is legacy."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        Image.new("RGB", (96, 54), "navy").save(self.root / "sky.png")
        forest = Image.new("RGBA", (192, 54))
        forest.paste((20, 80, 40, 255), (0, 30, 192, 54))
        forest.save(self.root / "forest.png")
        hills = Image.new("RGBA", (96, 40))
        hills.paste((90, 90, 120, 255), (0, 20, 96, 40))
        hills.save(self.root / "hills.png")

    def plan(self, **forest):
        """The probe-map shape: a non-repeating near layer that scrolls off screen at the camera maximum."""
        layer = {"id": "forest", "role": "near", "image": "forest.png", "alpha": "transparent",
                 "scroll_factor": [0.5, 0], "repeat": [False, False], **forest}
        return {"viewport": [96, 54], "camera": {"x": [0, 384], "y": [0, 0], "zoom": [1, 1]},
                "layers": [{"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque",
                            "scroll_factor": [0, 0]}, layer]}

    def test_the_probe_near_layer_fails_by_default(self):
        """Acceptance: the probe's near layer (screen rect [-192, 0, 0, 54] at the camera maximum) fails."""
        result = PARALLAX.validate_plan(self.plan(), self.root)
        self.assertFalse(result["passed"])
        layer = result["layers"][1]
        self.assertEqual((layer["require_canvas_coverage"], layer["coverage_rule"]), (True, "near or foreground role"))
        self.assertEqual(layer["extrema"][-1]["screen_canvas_rect"], [-192.0, 0.0, 0.0, 54.0])
        self.assertEqual(layer["extrema"][-1]["gaps_px"][2], 96.0)

    def test_legacy_sky_only_and_explicit_opt_out_pass_with_a_warning(self):
        for result in (PARALLAX.validate_plan(self.plan(), self.root, coverage="sky-only"),
                       PARALLAX.validate_plan(self.plan(require_canvas_coverage=False), self.root)):
            self.assertTrue(result["passed"], result["issues"])
            self.assertTrue(any("leaves the viewport entirely" in warning for warning in result["warnings"]))
        self.assertEqual(PARALLAX.validate_plan(self.plan(), self.root, coverage="sky-only")["coverage_policy"],
                         "sky-only")

    def test_repeated_layers_need_the_other_axis_covered(self):
        plan = self.plan(repeat=[True, False])
        plan["layers"][1].update(image="hills.png", role="far")
        result = PARALLAX.validate_plan(plan, self.root)
        self.assertFalse(result["passed"])
        self.assertEqual(result["layers"][1]["coverage_rule"], "repeated layer")
        self.assertEqual(result["layers"][1]["extrema"][0]["gaps_px"], [0.0, 0.0, 0.0, 14.0])
        self.assertTrue(PARALLAX.validate_plan(plan, self.root, coverage="sky-only")["passed"])

    def test_policy_all_and_role_names(self):
        plan = self.plan(role="far", repeat=[False, False])
        self.assertTrue(PARALLAX.validate_plan(plan, self.root)["passed"])
        self.assertFalse(PARALLAX.validate_plan(plan, self.root, coverage="all")["passed"])
        for role, near in (("near", True), ("near_trees", True), ("Foreground-vines", True),
                           ("foreground_overlay", True), ("far_bg", False), ("mid", False), ("nearby", False)):
            with self.subTest(role=role):
                self.assertEqual(PARALLAX.is_near_role(role), near)
        with self.assertRaises(ValueError):
            PARALLAX.validate_plan(plan, self.root, coverage="most")


class SeamTests(unittest.TestCase):
    """B12-T4: normalised repeat seams (MAP-14, repro_16): the wrap step against the layer's own steps."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        x = np.arange(32)
        sine = lambda period: 128 + 80 * np.sin(2 * np.pi * x / period)
        self.layers = {
            "seamless": np.stack([sine(32), sine(32) * 0.8, sine(32) * 0.5], -1),
            "duplicate": np.stack([sine(31), sine(31) * 0.8, sine(31) * 0.5], -1),
            "jump": np.stack([x * 8.0, x * 4.0, x * 2.0], -1),
            "flat": np.full((32, 3), 90.0),
        }
        Image.new("RGB", (32, 8), "navy").save(self.root / "sky.png")
        for name, columns in self.layers.items():
            strip(columns).save(self.root / f"{name}.png")

    def plan(self, name):
        return {"viewport": [32, 8], "camera": {"x": [0, 100]},
                "layers": [{"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque", "scroll_factor": [0, 0]},
                           {"id": name, "role": "far", "image": f"{name}.png", "alpha": "opaque",
                            "scroll_factor": [0.5, 0], "repeat": [True, False]}]}

    def test_verdicts_separate_a_true_join_from_a_duplicated_edge(self):
        expected = {"seamless": "continuous", "duplicate": "duplicate-edge", "jump": "seam", "flat": "flat"}
        for name, verdict in expected.items():
            with self.subTest(layer=name):
                result = PARALLAX.validate_plan(self.plan(name), self.root)
                seam = result["layers"][1]["repeat_seams"][0]
                self.assertEqual(seam["verdict"], verdict)
                self.assertFalse(seam["seamless_verified"])
                self.assertTrue(result["passed"])
                flagged = any("Repeat seam" in warning for warning in result["warnings"])
                self.assertEqual(flagged, verdict in ("seam", "duplicate-edge"))
                strict = PARALLAX.validate_plan(self.plan(name), self.root, strict_seams=True)
                self.assertEqual(strict["passed"], verdict in ("continuous", "flat"))
        duplicate = PARALLAX.validate_plan(self.plan("duplicate"), self.root)["layers"][1]["repeat_seams"][0]
        self.assertEqual(duplicate["alpha_mae"], 0)
        self.assertEqual(duplicate["visible_rgb_mae"], 0)  # edge equality calls the stutter perfect

    def test_ratios_are_forge_core_seam_report_over_columns(self):
        with Image.open(self.root / "seamless.png") as image:
            pixels = np.asarray(image.convert("RGBA"))
        seam = PARALLAX.seam_metrics(pixels, "x")
        reference = FORGE_CORE.seam_report(pixels[:, i:i + 1] for i in range(pixels.shape[1]))
        for key in ("seam", "adjacent_median", "adjacent_p95", "seam_over_median", "seam_over_p95"):
            self.assertAlmostEqual(seam[key], reference[key], places=12)
        self.assertLessEqual(seam["seam_over_p95"], 1.0)
        rows = PARALLAX.seam_metrics(np.ascontiguousarray(pixels.transpose(1, 0, 2)), "y")
        self.assertAlmostEqual(rows["seam_over_p95"], seam["seam_over_p95"], places=12)
        self.assertEqual(PARALLAX.seam_metrics(pixels[:, :1], "x")["verdict"], "too-small")


class PixelGridTests(unittest.TestCase):
    """B12-T4: pixel-art plans keep whole screen pixels per source pixel (sub-pixel presentation)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        Image.new("RGB", (64, 36), "navy").save(self.root / "sky.png")
        far = Image.new("RGBA", (64, 36))
        far.paste((50, 60, 70, 255), (0, 20, 64, 36))
        far.save(self.root / "far.png")

    def plan(self, zoom=(1, 2), offset=(0, 0), factor=(0.5, 0), pivot="top-left", viewport=(32, 18), **extra):
        return {"viewport": list(viewport), "camera": {"x": [0, 16], "zoom": list(zoom), "pivot": pivot},
                "layers": [{"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque", "scroll_factor": [0, 0]},
                           {"id": "far", "role": "far", "image": "far.png", "alpha": "transparent",
                            "offset": list(offset), "scroll_factor": list(factor), "repeat": [True, False]}],
                **extra}

    def test_integer_grid_passes_and_subpixel_scroll_only_warns(self):
        result = PARALLAX.validate_plan(self.plan(pixel_art=True, factor=(0.25, 0)), self.root, aspects=[])
        self.assertTrue(result["passed"], result["issues"])
        self.assertEqual(result["pixel_grid"], {"enforced": True, "source": "plan pixel_art"})
        grid = result["layers"][1]["pixel_grid"]
        self.assertEqual((grid["screen_px_per_source_px"], grid["integer_pixels"], grid["subpixel_scroll"]),
                         ([1.0, 2.0], True, True))
        self.assertTrue(any("sub-pixels" in warning for warning in result["warnings"]))

    def test_fractional_pixels_and_off_grid_rest_positions_fail_pixel_art(self):
        cases = {"zoom 1.5": self.plan(zoom=(1, 1.5), pixel_art=True),
                 "half-pixel offset": self.plan(offset=(0.5, 0), pixel_art=True),
                 "centre pivot on an odd viewport": self.plan(pivot="center", viewport=(33, 18), pixel_art=True),
                 "sampling nearest": self.plan(zoom=(1, 1.5), sampling="nearest")}
        for name, plan in cases.items():
            with self.subTest(case=name):
                result = PARALLAX.validate_plan(plan, self.root, aspects=[])
                self.assertFalse(result["passed"])
                self.assertTrue(any("Pixel art" in issue for issue in result["issues"]), result["issues"])
        loose = PARALLAX.validate_plan(self.plan(zoom=(1, 1.5)), self.root, aspects=[])
        self.assertTrue(loose["passed"])
        self.assertFalse(loose["layers"][1]["pixel_grid"]["integer_pixels"])
        forced = PARALLAX.validate_plan(self.plan(zoom=(1, 1.5)), self.root, aspects=[], pixel_grid=True)
        self.assertEqual((forced["passed"], forced["pixel_grid"]["source"]), (False, "--pixel-grid"))


class AspectSweepTests(unittest.TestCase):
    """B12-T4: coverage re-checked at 16:9, 19.5:9 and 4:3; aspects the plan lists must pass."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        Image.new("RGB", (96, 54), "navy").save(self.root / "sky.png")
        Image.new("RGB", (140, 50), "navy").save(self.root / "wide.png")

    def plan(self, **extra):
        return {"viewport": [96, 54], "camera": {"x": [0, 0]},
                "layers": [{"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque",
                            "scroll_factor": [0, 0]}], **extra}

    def test_default_sweep_warns_and_listed_aspects_fail(self):
        result = PARALLAX.validate_plan(self.plan(), self.root)
        self.assertTrue(result["passed"])
        sweep = {entry["aspect"]: entry for entry in result["aspect_sweep"]["aspects"]}
        self.assertEqual(list(sweep), ["16:9", "19.5:9", "4:3"])
        self.assertEqual((sweep["16:9"]["viewport"], sweep["16:9"]["passed"]), ([96.0, 54.0], True))
        self.assertEqual((sweep["19.5:9"]["viewport"], sweep["19.5:9"]["passed"]), ([117.0, 54.0], False))
        self.assertEqual((sweep["4:3"]["viewport"], sweep["4:3"]["passed"]), ([96.0, 72.0], False))
        self.assertEqual(sweep["19.5:9"]["failing_layers"], [{"id": "sky", "worst_gap_px": 21.0}])
        self.assertEqual(len([w for w in result["warnings"] if w.startswith("aspect ")]), 2)
        strict = PARALLAX.validate_plan(self.plan(aspects=["16:9", "19.5:9"]), self.root)
        self.assertFalse(strict["passed"])
        self.assertTrue(strict["issues"][0].startswith("aspect 19.5:9"))
        self.assertTrue({entry["aspect"]: entry for entry in strict["aspect_sweep"]["aspects"]}["19.5:9"]["required"])

    def test_policies_and_a_centred_camera_growing_both_sides(self):
        for policy, expected in (("fixed-height", [72.0, 54.0]), ("fixed-width", [96.0, 72.0])):
            with self.subTest(policy=policy):
                result = PARALLAX.validate_plan(self.plan(aspect_policy=policy), self.root, aspects=["4:3"])
                self.assertEqual(result["aspect_sweep"]["aspects"][0]["viewport"], expected)
        plan = {"viewport": [100, 50], "camera": {"x": [0, 0], "pivot": "center"},
                "layers": [{"id": "sky", "role": "sky", "image": "wide.png", "alpha": "opaque", "offset": [-20, 0],
                            "scroll_factor": [1, 0]}]}
        centred = PARALLAX.validate_plan(plan, self.root, aspects=["2.8"])
        self.assertEqual(centred["aspect_sweep"]["aspects"][0]["camera_shift"], [-20.0, -0.0])
        self.assertTrue(centred["aspect_sweep"]["aspects"][0]["passed"])
        plan["camera"]["pivot"] = "top-left"
        top_left = PARALLAX.validate_plan(plan, self.root, aspects=["2.8"])
        self.assertFalse(top_left["aspect_sweep"]["aspects"][0]["passed"])
        for bad in ("wide", "0:9", "-4:3"):
            with self.subTest(aspect=bad), self.assertRaises(ValueError):
                PARALLAX._local_parse_aspect(bad)


class ParallaxCliTests(unittest.TestCase):
    """Standard CLI tests (Appendix E) and the contracts of the plan and the result."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        Image.new("RGB", (96, 54), "navy").save(self.root / "sky.png")
        far = Image.new("RGBA", (96, 54))
        far.paste((50, 60, 70, 255), (0, 30, 96, 54))
        far.save(self.root / "far.png")
        self.plan = {"schema": "generate2dmap.parallax_plan.v1", "viewport": [96, 54],
                     "camera": {"x": [0, 96], "y": [0, 0], "zoom": [1, 1.5], "pivot": "center"},
                     "pixel_art": False, "aspects": ["16:9"],
                     "layers": [{"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque",
                                 "scroll_factor": [0, 0], "scale": 1.5, "offset": [-24, -14]},
                                {"id": "far", "role": "far", "image": "far.png", "alpha": "transparent",
                                 "scroll_factor": [0.5, 0], "repeat": [True, False], "scale": 1.5,
                                 "offset": [0, -14]}]}
        (self.root / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")

    def test_help_works_under_cp1252(self):
        assert_cli_help(SKILL, "validate_parallax")

    def test_reference_example_plan_passes_as_documented(self):
        """parallax-backgrounds.md section 2: the example plan passes with the stated image sizes."""
        text = (SKILLS_DIR / SKILL / "references" / "parallax-backgrounds.md").read_text(encoding="utf-8")
        plan = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
        assert_valid_contract(plan, "map", "parallax_plan", skill=SKILL)
        Image.new("RGB", (1040, 480), "navy").save(self.root / "sky.png")
        for name, size in (("far.png", (1200, 480)), ("near.png", (1920, 540))):
            layer = Image.new("RGBA", size)
            layer.paste((60, 70, 90, 255), (0, size[1] - 200, size[0], size[1]))
            layer.save(self.root / name)
        result = PARALLAX.validate_plan(plan, self.root)
        self.assertTrue(result["passed"], result["issues"])
        self.assertEqual(result["warnings"], ["aspect 4:3 (960x720 viewport, expand): sky, far, near do not cover it."])
        self.assertTrue({entry["aspect"]: entry for entry in result["aspect_sweep"]["aspects"]}["19.5:9"]["required"])

    def test_plan_and_result_follow_their_contracts(self):
        assert_valid_contract(self.plan, "map", "parallax_plan", skill=SKILL)
        self.assertEqual(requested_contract_errors(self.plan, "parallax_plan"), [])
        run = parallax_cli("--spec", self.root / "plan.json", "--report", self.root / "qa.json")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(run.stdout.isascii())
        printed = json.loads(run.stdout)
        saved = json.loads((self.root / "qa.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(printed.pop("report")), (self.root / "qa.json").resolve())
        self.assertEqual(printed, saved)
        self.assertEqual(requested_contract_errors(saved), [])
        self.assertEqual(saved["schema"], "generate2dmap.parallax_validation.v2")
        self.assertEqual(saved["layers"][1]["image"], "far.png")
        broken = json.loads(json.dumps(saved))
        broken["passed"] = True
        broken["issues"] = ["something"]
        self.assertTrue(requested_contract_errors(broken))
        failing = parallax_cli("--spec", self.root / "missing.json")
        self.assertEqual(requested_contract_errors(json.loads(failing.stdout)), [])

    def test_refuses_an_existing_report(self):
        (self.root / "qa.json").write_text("keep", encoding="utf-8")
        run = parallax_cli("--spec", self.root / "plan.json", "--report", self.root / "qa.json")
        self.assertEqual(run.returncode, 1)
        self.assertIn("refusing to replace existing report", run.stderr)
        self.assertFalse(json.loads(run.stdout)["passed"])
        self.assertEqual((self.root / "qa.json").read_text(encoding="utf-8"), "keep")

    def test_strict_failure_publishes_nothing(self):
        self.plan["layers"][0]["scale"] = 0.5
        (self.root / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")
        run = parallax_cli("--spec", self.root / "plan.json", "--report", self.root / "qa.json", "--strict")
        self.assertEqual(run.returncode, 1)
        result = json.loads(run.stdout)
        self.assertEqual((result["passed"], result["report"]), (False, None))
        self.assertFalse((self.root / "qa.json").exists())
        run = parallax_cli("--spec", self.root / "plan.json", "--report", self.root / "qa.json")
        self.assertEqual(run.returncode, 1)
        self.assertFalse(json.loads((self.root / "qa.json").read_text(encoding="utf-8"))["passed"])

    def test_cli_options_reach_the_validator(self):
        probe = {"viewport": [96, 54], "camera": {"x": [0, 384]},
                 "layers": [{"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque",
                             "scroll_factor": [0, 0]},
                            {"id": "near", "role": "near", "image": "far.png", "alpha": "transparent",
                             "scroll_factor": [0.5, 0]}]}
        (self.root / "probe.json").write_text(json.dumps(probe), encoding="utf-8")
        self.assertEqual(parallax_cli("--spec", self.root / "probe.json").returncode, 1)
        legacy = parallax_cli("--spec", self.root / "probe.json", "--coverage", "sky-only", "--aspects", "none")
        self.assertEqual(legacy.returncode, 0, legacy.stdout)
        result = json.loads(legacy.stdout)
        self.assertEqual((result["coverage_policy"], result["aspect_sweep"]["aspects"]), ("sky-only", []))
        grid = parallax_cli("--spec", self.root / "plan.json", "--pixel-grid", "--aspects", "4:3",
                            "--aspect-policy", "fixed-width")
        result = json.loads(grid.stdout)
        self.assertEqual(grid.returncode, 1)
        self.assertEqual(result["pixel_grid"]["source"], "--pixel-grid")
        self.assertEqual(result["aspect_sweep"]["policy"], "fixed-width")
        bad = parallax_cli("--spec", self.root / "plan.json", "--aspects", "wide")
        self.assertEqual(bad.returncode, 1)
        self.assertTrue(bad.stderr.startswith("error: "), bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)


if __name__ == "__main__":
    unittest.main()
