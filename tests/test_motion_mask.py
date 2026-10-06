"""Tests for build_motion_mask.py: motion_plan.v1 regions, exact-zero protected cores and clip envelopes (B16-T1)."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from forge_testutils import (FIXTURES_DIR, assert_cli_help, assert_valid_contract, contract_errors, load_script,
                             load_shared, run_cli, script_path)


MASK = load_script("generate2dmap", "build_motion_mask")
SCRIPT = script_path("generate2dmap", "build_motion_mask")
SKILL = "generate2dmap"


def requested_errors(document, name):
    """Errors against the vendored generate2dmap schemas, which hold this module's section 5 requests (D33)."""
    return contract_errors(document, "map", name, skill=SKILL)


def smoothstep(value):
    value = min(1.0, max(0.0, value))
    return value * value * (3 - 2 * value)


def half_up(value):
    return int(math.floor(value + 0.5))


def gradient_plate(width, height, seed=0):
    """An opaque painted-looking plate: smooth gradients plus mild texture."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    pixels = np.zeros((height, width, 3), np.uint8)
    pixels[..., 0] = 60 + 80 * xx // max(1, width - 1)
    pixels[..., 1] = 70 + 60 * yy // max(1, height - 1)
    pixels[..., 2] = 120 + rng.integers(0, 12, (height, width))
    return pixels


def plan_document(size, regions, protected=(), loop=None):
    return {"schema": "generate2dmap.motion_plan.v1", "sourceSize": list(size), "regions": list(regions),
            "protected": list(protected), "loop": loop or {"policy": "forward-overlap", "overlap": 2}}


def mask_main(*arguments):
    """build_motion_mask.main() in-process: (exit status, stdout, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = MASK.main([str(argument) for argument in arguments])
    return status, stdout.getvalue(), stderr.getvalue()


class _Workspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_inputs(self, plate, document, name="scene"):
        folder = self.root / name
        folder.mkdir()
        Image.fromarray(plate).save(folder / "plate.png")
        (folder / "plan.json").write_text(json.dumps(document), encoding="utf-8")
        return folder / "plan.json", folder / "plate.png"

    def run_mask(self, plan, plate, output, *extra):
        status, stdout, stderr = mask_main("--plan", plan, "--plate", plate, "--output-dir", output, *extra)
        self.assertEqual(status, 0, stderr)
        summary = json.loads(stdout)
        mask = np.asarray(Image.open(output / "motion-mask.png"))
        report = json.loads((output / "mask-qa.json").read_text(encoding="utf-8"))
        return summary, mask, report


class RasterTests(unittest.TestCase):
    def test_box_is_half_open_on_pixel_centres(self):
        mask = MASK.rasterize_box((2, 1, 5, 3), (8, 6))
        self.assertEqual(np.argwhere(mask).tolist(), [[1, 2], [1, 3], [1, 4], [2, 2], [2, 3], [2, 4]])
        # A fractional edge includes a pixel only when its centre is inside: 1.4 keeps 1 (centre 1.5), 1.6 drops it.
        self.assertTrue(MASK.rasterize_box((1.4, 0, 3, 1), (8, 6))[0, 1])
        self.assertFalse(MASK.rasterize_box((1.6, 0, 3, 1), (8, 6))[0, 1])

    def test_rectangle_polygon_equals_box(self):
        rng = np.random.default_rng(4)
        for _ in range(60):
            x0, y0 = rng.uniform(-6, 40, 2)
            x1, y1 = x0 + rng.uniform(0.3, 30), y0 + rng.uniform(0.3, 30)
            polygon = MASK.rasterize_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], (48, 40))
            np.testing.assert_array_equal(polygon, MASK.rasterize_box((x0, y0, x1, y1), (48, 40)))

    def test_polygon_matches_a_brute_force_centre_test(self):
        points = [(3.2, 1.7), (40.5, 10.1), (22.0, 15.0), (12.3, 33.9)]  # concave

        def inside(px, py):
            crossings = 0
            for (xa, ya), (xb, yb) in zip(points, points[1:] + points[:1]):
                if (ya <= py < yb) or (yb <= py < ya):
                    crossings += xa + (py - ya) * (xb - xa) / (yb - ya) <= px
            return crossings % 2 == 1

        expected = np.array([[inside(x + 0.5, y + 0.5) for x in range(50)] for y in range(40)])
        np.testing.assert_array_equal(MASK.rasterize_polygon(points, (50, 40)), expected)

    def test_distance_backends_are_identical(self):
        rng = np.random.default_rng(9)
        for _ in range(25):
            height, width = rng.integers(4, 70, 2)
            shape = rng.random((height, width)) < rng.uniform(0.002, 0.2)
            limit = float(rng.uniform(0, 25))
            with mock.patch.dict(os.environ, {"FORGE_CORE_NO_SCIPY": ""}):
                scipy_values = MASK.distance_from(shape, limit)
            with mock.patch.dict(os.environ, {"FORGE_CORE_NO_SCIPY": "1"}):
                numpy_values = MASK.distance_from(shape, limit)
            np.testing.assert_array_equal(scipy_values, numpy_values)
        self.assertTrue(np.isinf(MASK.distance_from(np.zeros((5, 5), bool), 3)).all())

    def test_distance_is_euclidean_between_centres(self):
        shape = np.zeros((9, 9), bool)
        shape[4, 4] = True
        distance = MASK.distance_from(shape, 3.0)
        self.assertEqual(distance[4, 4], 0)
        self.assertAlmostEqual(distance[6, 6], math.sqrt(8))
        self.assertEqual(distance[4, 7], 3)
        self.assertTrue(np.isinf(distance[4, 8]))  # 4 px is beyond the limit


class RegionTests(_Workspace):
    def test_region_is_full_inside_and_feathers_only_outside(self):
        plate = gradient_plate(64, 48)
        region = {"id": "pond", "kind": "rect", "box": [8, 8, 40, 40], "feather": 6, "strength": 0.8}
        plan, plate_path = self.write_inputs(plate, plan_document((64, 48), [region]))
        _summary, mask, report = self.run_mask(plan, plate_path, self.root / "out")
        full = half_up(255 * 0.8)
        self.assertTrue((mask[8:40, 8:40] == full).all())
        for k in range(10):  # column 40 + k is k + 1 px from the last region column (39)
            expected = half_up(255 * 0.8 * (1 - smoothstep((k + 1) / 6))) if k + 1 < 6 else 0
            self.assertEqual(mask[20, 40 + k], expected, k)
        self.assertEqual(report["protectedMax"], 0)
        self.assertEqual(report["regions"][0]["visiblePx"], int((mask > 0).sum()))
        self.assertGreaterEqual(report["regions"][0]["supportPx"], report["regions"][0]["visiblePx"])

    def test_protected_core_is_exact_zero_with_its_transition_outside(self):
        plate = gradient_plate(64, 48)
        region = {"id": "pond", "kind": "rect", "box": [4, 4, 60, 44], "feather": 0, "strength": 1}
        rock = {"id": "rock", "box": [28, 20, 36, 28], "margin": 2, "feather": 5}
        plan, plate_path = self.write_inputs(plate, plan_document((64, 48), [region], [rock]))
        _summary, mask, report = self.run_mask(plan, plate_path, self.root / "out")
        # The core is the box grown by a Euclidean 2 px margin: straight sides 2 px out, rounded corners.
        self.assertTrue((mask[20:28, 26:38] == 0).all() and (mask[18:30, 28:36] == 0).all())
        self.assertEqual(mask[18, 26], half_up(255 * smoothstep((math.sqrt(8) - 2) / 5)))
        self.assertEqual(report["protectedMax"], 0)
        self.assertEqual(report["protected"], [{"id": "rock", "corePx": report["protected"][0]["corePx"], "max": 0}])
        # Along row 24 the core ends at column 37 (distance 2); the transition then rises over 5 px.
        for column in range(38, 44):
            distance = column - 35
            expected = half_up(255 * smoothstep((distance - 2) / 5)) if distance - 2 < 5 else 255
            self.assertEqual(mask[24, column], expected, column)

    def test_landmark_margin_and_polygon_region(self):
        plate = gradient_plate(64, 48)
        flag = {"id": "flag", "kind": "landmark", "box": [20, 20, 30, 30], "margin": 4, "feather": 0, "strength": 1,
                "motion": "sway"}
        cloud = {"id": "cloud", "kind": "polygon", "points": [[40, 2], [60, 2], [60, 12]], "feather": 0,
                 "strength": 0.5}
        plan, plate_path = self.write_inputs(plate, plan_document((64, 48), [flag, cloud]))
        _summary, mask, report = self.run_mask(plan, plate_path, self.root / "out")
        self.assertEqual(mask[25, 33], 255)  # 4 px right of the last box column (29)
        self.assertEqual(mask[25, 34], 0)
        self.assertEqual(mask[3, 58], 128)
        self.assertEqual(mask[10, 42], 0)  # outside the triangle
        self.assertEqual([item["motion"] for item in report["regions"]], ["sway", "flow"])

    def test_luma_band_selects_bright_plate_pixels_inside_its_box(self):
        plate = np.full((48, 64, 3), 40, np.uint8)
        plate[8:16, 8:16] = 220   # inside the box
        plate[30:38, 40:48] = 220  # outside the box
        plate[20, 20] = 255        # a speck inside the box
        region = {"id": "lantern", "kind": "luma_band", "band": [180, 255], "box": [0, 0, 32, 32], "feather": 0,
                  "strength": 1}
        plan, plate_path = self.write_inputs(plate, plan_document((64, 48), [region]))
        _summary, mask, _report = self.run_mask(plan, plate_path, self.root / "out")
        self.assertTrue((mask[9:15, 9:15] == 255).all())  # the blurred interior of the bright block
        self.assertFalse(mask[30:38, 40:48].any())
        self.assertEqual(mask[20, 20], 0)

    def test_mean_opacity_below_seven_percent_warns(self):
        plate = gradient_plate(64, 48)
        region = {"id": "candle", "kind": "rect", "box": [10, 10, 16, 16], "feather": 2, "strength": 1}
        plan, plate_path = self.write_inputs(plate, plan_document((64, 48), [region]))
        summary, _mask, report = self.run_mask(plan, plate_path, self.root / "out")
        self.assertLess(report["meanOpacity"], 0.07)
        self.assertEqual(summary["status"], "warn")
        check = next(check for check in report["checks"] if check["id"] == "mean_opacity")
        self.assertEqual((check["status"], check["threshold"]), ("warn", 0.07))

    def test_contract_example_plan_builds_on_its_plate_size(self):
        document = json.loads((FIXTURES_DIR / "contracts" / "map.motion_plan_v1.valid.json").read_text("utf-8"))
        plate = gradient_plate(*document["sourceSize"])
        plate[300:420, 1200:1300] = 230  # a bright lantern glow for the luma band
        plan = MASK.normalize_plan(document)
        build = MASK.build_mask(plan, plate)
        stats = MASK.mask_statistics(build)
        self.assertEqual(stats["protectedMax"], 0)
        self.assertGreater(stats["regions"][0]["visiblePx"], 0)
        self.assertGreater(stats["regions"][1]["visiblePx"], 0)


class EnvelopeTests(_Workspace):
    """A flag whose generated tips wave 6 px beyond its painted box (repack-flag-v2.py: the cloth outgrew its mask)."""

    size = (96, 64)

    def make_clip(self, folder, plate, frames=12):
        folder.mkdir()
        for index in range(frames):
            frame = plate.copy()
            reach = 52 + round(6 * (0.5 + 0.5 * math.sin(index / frames * 2 * math.pi)))
            frame[12:28, 40:reach] = (200, 40, 40) if index % 2 else (230, 60, 50)  # waving flag and its tips
            frame[34:46, 12:18] = (90, 90, 200) if index % 3 else (140, 140, 240)   # motion inside the tower
            frame[50:58, 80:88] = (250, 250, 250) if index % 2 else (20, 20, 20)    # hallucinated far away
            Image.fromarray(frame).save(folder / f"frame_{index:06d}.png")
        return folder

    def scene(self):
        plate = gradient_plate(*self.size)
        plate[12:28, 40:52] = (220, 50, 45)  # the painted flag
        flag = {"id": "flag", "kind": "landmark", "box": [40, 12, 52, 28], "feather": 3, "strength": 1,
                "motion": "sway"}
        tower = {"id": "tower", "box": [10, 30, 20, 50], "margin": 1}
        plan, plate_path = self.write_inputs(plate, plan_document(self.size, [flag], [tower]))
        return plate, plan, plate_path

    def test_envelope_contains_every_moving_pixel(self):
        plate, plan, plate_path = self.scene()
        clip = self.make_clip(self.root / "clip", plate)
        summary, mask, report = self.run_mask(plan, plate_path, self.root / "out", "--envelope-from", clip,
                                              "--fps", "24", "--envelope-reach", "16")
        envelope = np.asarray(Image.open(self.root / "out" / "motion-envelope.png")) > 0
        flag_motion = envelope.copy()
        flag_motion[:, :34] = False
        flag_motion[44:, :] = False
        self.assertGreater(flag_motion.sum(), 50)
        self.assertTrue((mask[flag_motion] == 255).all())  # every moving flag pixel at full opacity
        self.assertTrue((mask[29:51, 9:21] == 0).all())  # tower core stays exact zero
        moving = report["envelope"]
        self.assertEqual(moving["uncoveredPx"], 0)
        self.assertEqual(moving["containedPx"], moving["admittedPx"])
        self.assertGreater(moving["excludedProtectedPx"], 0)
        self.assertGreater(moving["excludedOutsidePx"], 0)
        self.assertEqual(summary["status"], "warn")
        self.assertEqual(next(c for c in report["checks"] if c["id"] == "envelope_excluded")["status"], "warn")
        self.assertEqual(requested_errors(report, "motion_mask_qa_v1"), [])
        # Without the envelope the waving tips fall outside the 3 px feather and would be cut off.
        _summary, plain, _report = self.run_mask(plan, plate_path, self.root / "plain")
        self.assertTrue((plain[flag_motion] < 255).any())
        self.assertTrue((plain[flag_motion] == 0).any())

    def test_clip_of_another_size_is_registered_with_one_fixed_transform(self):
        plate, plan, plate_path = self.scene()
        clip = self.make_clip(self.root / "full", plate, frames=6)
        small = self.root / "small"
        small.mkdir()
        for path in sorted(clip.glob("*.png")):  # the provider returned the scene at half size
            Image.open(path).resize((48, 32), Image.Resampling.LANCZOS).save(small / path.name)
        _summary, _mask, report = self.run_mask(plan, plate_path, self.root / "out", "--envelope-from", small,
                                                "--fps", "24000/1001", "--envelope-reach", "16")
        transform = report["envelope"]["transform"]
        self.assertEqual((transform["fit"], transform["scale"], transform["offset"]), ("contain", 2.0, [0.0, 0.0]))
        clip_ref = next(item for item in report["inputs"] if item.get("kind") == "frames")
        self.assertEqual((clip_ref["frames"], clip_ref["size"], clip_ref["fps"]), (6, [48, 32], "24000/1001"))
        self.assertGreater(report["envelope"]["movingPx"], 0)


class TransformTests(unittest.TestCase):
    def test_contain_cover_and_explicit(self):
        contain = MASK.make_transform((100, 50), (120, 80))
        self.assertEqual((contain.scale, contain.offset, contain.footprint), (1.2, (0.0, 10.0), (0.0, 10.0, 120.0,
                                                                                                    70.0)))
        cover = MASK.make_transform((100, 50), (120, 80), fit="cover")
        self.assertEqual((cover.scale, cover.offset), (1.6, (-20.0, 0.0)))
        explicit = MASK.make_transform((100, 50), (120, 80), scale=1.25, offset=(2.5, -1.0))
        self.assertEqual((explicit.fit, explicit.footprint), ("explicit", (2.5, -1.0, 127.5, 61.5)))

    def test_coverage_fades_only_inside_real_gaps(self):
        transform = MASK.make_transform((100, 50), (120, 80))  # letterboxed: 10 px gaps above and below
        coverage = MASK.coverage_map(transform, 8.0)
        self.assertTrue((coverage[:10] == 0).all() and (coverage[70:] == 0).all())
        self.assertAlmostEqual(float(coverage[10, 60]), smoothstep(0.5 / 8), places=6)
        self.assertTrue((coverage[18:62] == 1).all())
        self.assertTrue((coverage[40, :] == 1).all())  # the clip spans the full width: no side fade
        full = MASK.coverage_map(MASK.make_transform((120, 80), (120, 80)), 8.0)
        self.assertTrue((full == 1).all())

    def test_alignment_is_exact_at_unit_scale_and_matches_forge_core_resampling(self):
        rng = np.random.default_rng(2)
        frame = (rng.random((40, 60, 3)) * 255).astype(np.uint8)
        same = MASK.make_transform((60, 40), (60, 40))
        np.testing.assert_array_equal(MASK.align_frame(frame, same), frame)
        shifted = MASK.align_frame(frame, MASK.make_transform((60, 40), (64, 44), scale=1.0, offset=(2.0, 3.0)))
        np.testing.assert_array_equal(shifted[3:43, 2:62], frame)
        smooth = np.asarray(Image.fromarray(frame).resize((30, 20), Image.Resampling.BOX).resize(
            (60, 40), Image.Resampling.BICUBIC))
        transform = MASK.make_transform((60, 40), (80, 52), scale=1.27, offset=(1.3, 0.6))
        ours = MASK.align_frame(smooth, transform).astype(int)
        core = load_shared("forge_core")
        rgba = np.dstack([smooth, np.full(smooth.shape[:2], 255, np.uint8)])
        reference = np.asarray(core.resample_rgba(rgba, 1.27, "lanczos", anchor_src=(0, 0), anchor_dst=(1.3, 0.6),
                                                  out_size=(80, 52)))[..., :3].astype(int)
        interior = (slice(6, 46), slice(6, 70))
        self.assertLessEqual(np.abs(ours[interior] - reference[interior]).max(), 1)


class PlanValidationTests(_Workspace):
    def errors_for(self, document, size=(32, 24)):
        plan, plate = self.write_inputs(gradient_plate(*size), document, name=f"case{len(list(self.root.iterdir()))}")
        status, stdout, stderr = mask_main("--plan", plan, "--plate", plate, "--output-dir", plan.parent / "out")
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("error: "), stderr)
        self.assertFalse((plan.parent / "out").exists())
        return stderr

    def test_invalid_plans_are_refused_with_the_field_named(self):
        region = {"id": "a", "kind": "rect", "box": [1, 1, 8, 8], "feather": 1, "strength": 1}
        base = plan_document((32, 24), [region])
        cases = [
            (lambda d: d.update(sourceSize=[40, 24]), "sourceSize"),
            (lambda d: d["regions"][0].update(kind="ellipse"), "regions[0].kind"),
            (lambda d: d["regions"][0].update(box=[8, 1, 1, 8]), "x0 < x1"),
            (lambda d: d["regions"][0].update(strength=2), "regions[0].strength"),
            (lambda d: d["regions"][0].update(motion="drift"), "regions[0].motion"),
            (lambda d: d["regions"].append(dict(region)), "used twice"),
            (lambda d: d.update(loop={"policy": "pingpong", "overlap": 4}), "overlap"),
            (lambda d: d.update(loop={"policy": "reverse"}), "loop.policy"),
            (lambda d: d.update(protected=[{"id": "p", "box": [0, 0, 4, 4], "polygon": [[0, 0], [1, 0], [1, 1]]}]),
             "exactly one of polygon or box"),
            (lambda d: d["regions"].__setitem__(0, {"id": "b", "kind": "luma_band", "band": [200, 100],
                                                    "feather": 0, "strength": 1}), "ordered"),
            (lambda d: d.update(encode={"crf": 0}), "encode.crf"),
            (lambda d: d.update(schema="generate2dmap.motion_plan.v2"), "plan.schema"),
        ]
        for mutate, needle in cases:
            document = copy.deepcopy(base)
            mutate(document)
            with self.subTest(needle=needle):
                self.assertIn(needle, self.errors_for(document))

    def test_optional_plan_fields_follow_the_requested_schema(self):
        region = {"id": "flag", "kind": "landmark", "box": [1, 1, 8, 8], "margin": 2, "feather": 1, "strength": 1,
                  "motion": "sway"}
        document = plan_document((32, 24), [region], [{"id": "p", "box": [10, 10, 14, 14], "feather": 3}])
        document["registration"] = {"scale": 1.25, "offset": [0, 0.5]}
        self.assertEqual(requested_errors(document, "motion_plan_v1"), [])
        assert_valid_contract(document, "map", "motion_plan_v1", skill=SKILL)  # the frozen schema is open
        plan = MASK.normalize_plan(document)
        self.assertEqual(plan["registration"], {"fit": "explicit", "scale": 1.25, "offset": (0.0, 0.5)})
        self.assertEqual((plan["regions"][0]["motion"], plan["protected"][0]["feather"]), ("sway", 3.0))
        for mutate in (lambda d: d["regions"][0].update(motion="drift"),
                       lambda d: d["registration"].pop("offset"),
                       lambda d: d["protected"][0].update(feather=-1)):
            broken = copy.deepcopy(document)
            mutate(broken)
            self.assertNotEqual(requested_errors(broken, "motion_plan_v1"), [])

    def test_transparent_plate_is_refused(self):
        plate = np.dstack([gradient_plate(32, 24), np.full((24, 32), 255, np.uint8)])
        plate[0, 0, 3] = 0
        folder = self.root / "alpha"
        folder.mkdir()
        Image.fromarray(plate).save(folder / "plate.png")
        region = {"id": "a", "kind": "rect", "box": [1, 1, 8, 8], "feather": 1, "strength": 1}
        (folder / "plan.json").write_text(json.dumps(plan_document((32, 24), [region])), encoding="utf-8")
        status, _stdout, stderr = mask_main("--plan", folder / "plan.json", "--plate", folder / "plate.png",
                                            "--output-dir", folder / "out")
        self.assertEqual(status, 1)
        self.assertIn("opaque", stderr)


class CliTests(_Workspace):
    def scene(self):
        region = {"id": "pond", "kind": "rect", "box": [4, 4, 28, 20], "feather": 2, "strength": 1}
        return self.write_inputs(gradient_plate(32, 24), plan_document((32, 24), [region]))

    def test_help_is_ascii_under_legacy_consoles(self):
        assert_cli_help(SKILL, "build_motion_mask")

    def test_refuses_an_existing_output_directory(self):
        plan, plate = self.scene()
        existing = self.root / "out"
        existing.mkdir()
        result = run_cli([SCRIPT, "--plan", plan, "--plate", plate, "--output-dir", existing], "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("error: "), result.stderr)
        self.assertEqual(list(existing.iterdir()), [])

    def test_failed_mask_qa_publishes_nothing(self):
        region = {"id": "pond", "kind": "rect", "box": [4, 4, 28, 20], "feather": 2, "strength": 1}
        rock = {"id": "rock", "box": [0, 0, 32, 24]}  # protects the whole plate: the mask would be empty
        plan, plate = self.write_inputs(gradient_plate(32, 24), plan_document((32, 24), [region], [rock]))
        output = self.root / "out"
        result = run_cli([SCRIPT, "--plan", plan, "--plate", plate, "--output-dir", output])
        self.assertEqual(result.returncode, 1)
        self.assertIn("mask_not_empty", result.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["scene"])  # no stage left behind

    def test_success_prints_one_json_line_and_outputs_are_deterministic(self):
        plan, plate = self.scene()
        first = run_cli([SCRIPT, "--plan", plan, "--plate", plate, "--output-dir", self.root / "a"], "cp1252")
        second = run_cli([SCRIPT, "--plan", plan, "--plate", plate, "--output-dir", self.root / "b"], "cp1252")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(first.stdout.strip().splitlines()), 1)
        summary = json.loads(first.stdout)
        self.assertEqual(Path(summary["metadata"]).name, "mask-qa.json")
        self.assertEqual(summary["protected_max"], 0)
        for name in ("motion-mask.png", "mask-overlay.png", "mask-qa.json"):
            self.assertEqual((self.root / "a" / name).read_bytes(), (self.root / "b" / name).read_bytes(), name)
        report = json.loads((self.root / "a" / "mask-qa.json").read_text(encoding="utf-8"))
        assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
        self.assertEqual(requested_errors(report, "motion_mask_qa_v1"), [])
        plan_document_on_disk = json.loads(plan.read_text(encoding="utf-8"))
        assert_valid_contract(plan_document_on_disk, "map", "motion_plan_v1", skill=SKILL)
        self.assertEqual(report["inputs"][0]["path"], "../scene/plan.json")


if __name__ == "__main__":
    unittest.main()
