"""Behavioral regression tests for parallax coverage (validate_parallax)."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from forge_testutils import load_script, run_cli, script_path


PARALLAX = load_script("generate2dmap", "validate_parallax")
SCRIPT = script_path("generate2dmap", "validate_parallax")
MODULE = PARALLAX


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
    """The improved fork's 22 parallax tests (B12-T1), bodies unchanged except module loading."""

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
        run = run_cli(command, cwd=SCRIPT.parent)
        self.assertEqual(run.returncode, 1)
        self.assertFalse(json.loads(run.stdout)["passed"])
        self.assertFalse(json.loads((self.root / "report.json").read_text())["passed"])

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


if __name__ == "__main__":
    unittest.main()
