from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import math
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
import pytest
from pathlib import Path

from PIL import Image

from forge_testutils import assert_cli_help, make_magenta_sheet, run_cli


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "generate2dsprite"
    / "scripts"
    / "generate2dsprite.py"
)
SPEC = importlib.util.spec_from_file_location("generate2dsprite", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ANCHOR_SCRIPT_PATH = SCRIPT_PATH.with_name("make_anchor_layout.py")
ANCHOR_SPEC = importlib.util.spec_from_file_location("make_anchor_layout", ANCHOR_SCRIPT_PATH)
assert ANCHOR_SPEC and ANCHOR_SPEC.loader
ANCHOR_MODULE = importlib.util.module_from_spec(ANCHOR_SPEC)
sys.modules[ANCHOR_SPEC.name] = ANCHOR_MODULE
ANCHOR_SPEC.loader.exec_module(ANCHOR_MODULE)


MAGENTA = (255, 0, 255, 255)
SUBJECT = (20, 40, 60, 255)


class LocomotionPlanningTests(unittest.TestCase):
    def test_legacy_four_pose_process_still_exports_four_frames_with_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "four-poses.png"
            image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            for row in range(2):
                for col in range(2):
                    image.paste(SUBJECT, (col * 16 + 5, row * 16 + 4, col * 16 + 11, row * 16 + 12))
            image.save(source)
            for intentional in (False, True):
                output = base / ("intentional" if intentional else "advisory")
                argv = ["process", "--input", str(source), "--target", "asset", "--mode", "run",
                        "--output-dir", str(output), "--background-mode", "native_alpha",
                        "--resampler", "nearest", "--cell-size", "16", "--pixel-scale", "1"]
                if intentional:
                    argv.append("--intentional-low-frame-count")
                stderr = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    MODULE.cmd_process(MODULE.build_parser().parse_args(argv))
                meta = json.loads((output / "pipeline-meta.json").read_text(encoding="utf-8"))
                self.assertEqual((meta["rows"], meta["cols"]), (2, 2))
                self.assertEqual(meta["frame_labels"], ["run-1", "run-2", "run-3", "run-4"])
                self.assertTrue(all((output / f"run-{i}.png").exists() for i in range(1, 5)))
                self.assertEqual(meta["intentional_low_frame_count"], intentional)
                self.assertEqual(bool(meta["planning_warnings"]), not intentional)
                self.assertEqual("8-12 useful poses" in stderr.getvalue(), not intentional)

    def test_directional_count_and_explicit_eight_pose_layout(self) -> None:
        self.assertIsNotNone(MODULE.locomotion_planning_warning("player_sheet", 4, 4))
        self.assertIsNone(MODULE.locomotion_planning_warning("run", 2, 4))
        self.assertIsNone(MODULE.locomotion_planning_warning("walk", 3, 4))
        self.assertIsNone(MODULE.locomotion_planning_warning("idle", 2, 2))
        self.assertEqual(MODULE.GRID_SHAPES["walk"], (2, 2))

    def test_custom_eight_pose_process_uses_measured_grid_without_sparse_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, output = base / "eight-poses.png", base / "output"
            image = Image.new("RGBA", (64, 32), (0, 0, 0, 0))
            for index in range(8):
                x, y = (index % 4) * 16, (index // 4) * 16
                image.paste((20 + index, 40, 60, 255), (x + 5, y + 4, x + 11, y + 12))
            image.save(source)
            args = MODULE.build_parser().parse_args([
                "process", "--input", str(source), "--target", "asset", "--mode", "run",
                "--output-dir", str(output), "--background-mode", "native_alpha",
                "--resampler", "nearest", "--cell-size", "16", "--rows", "2", "--cols", "4", "--pixel-scale", "1",
            ])
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                MODULE.cmd_process(args)
            meta = json.loads((output / "pipeline-meta.json").read_text(encoding="utf-8"))
            self.assertEqual((meta["rows"], meta["cols"]), (2, 4))
            self.assertEqual(len(meta["frame_labels"]), 8)
            self.assertEqual(meta["planning_warnings"], [])
            self.assertEqual(stderr.getvalue(), "")
            self.assertTrue((output / "run-8.png").exists())

    def test_build_prompt_warns_without_changing_legacy_layout_or_prompt_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "prompt.json"
            argv = ["build-prompt", "--target", "asset", "--mode", "run", "--prompt", "fox",
                    "--write-json", str(path)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                MODULE.cmd_build_prompt(MODULE.build_parser().parse_args(argv))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(stdout.getvalue().startswith("A 2x2"))
            self.assertNotIn("Locomotion planning:", stdout.getvalue())
            self.assertIn("Locomotion planning:", stderr.getvalue())
            self.assertEqual(len(payload["planning_warnings"]), 1)
            self.assertIn("full motion envelope", payload["generated_prompt"])
            self.assertIn("15% clear background", payload["generated_prompt"])
            self.assertNotIn("same bounding box", payload["generated_prompt"])
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                MODULE.cmd_build_prompt(MODULE.build_parser().parse_args(argv + ["--intentional-low-frame-count"]))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stderr.getvalue(), "")
            self.assertTrue(payload["intentional_low_frame_count"])
            self.assertEqual(payload["planning_warnings"], [])


class SplitGridTests(unittest.TestCase):
    def test_edge_touch_uses_trimmed_frame_dimensions(self) -> None:
        image = Image.new("RGBA", (20, 20), MAGENTA)
        image.paste(SUBJECT, (2, 5, 18, 15))

        _frames, info = MODULE.split_grid(
            image,
            rows=1,
            cols=1,
            cell_size=20,
            threshold=100,
            edge_threshold=150,
            trim_border_px=2,
            edge_clean_depth=0,
            component_mode="largest",
        )

        self.assertEqual(info[0]["source_frame_size"], [16, 16])
        self.assertTrue(info[0]["source_edge_touch"])
        self.assertTrue(info[0]["edge_touch"])

    def test_empty_frame_is_reported_as_empty(self) -> None:
        image = Image.new("RGBA", (20, 20), MAGENTA)

        frames, info = MODULE.split_grid(
            image,
            rows=1,
            cols=1,
            cell_size=20,
            threshold=100,
            edge_threshold=150,
            trim_border_px=0,
            edge_clean_depth=0,
            scale_strategy="preserve",
        )

        self.assertTrue(info[0]["is_empty"])
        self.assertEqual(info[0]["output_size"], [0, 0])
        self.assertIsNone(frames[0].getbbox())

    def test_preserve_aligns_equal_size_subjects_to_same_anchor(self) -> None:
        image = Image.new("RGBA", (40, 20), MAGENTA)
        image.paste(SUBJECT, (3, 3, 9, 15))
        image.paste(SUBJECT, (28, 3, 34, 15))

        frames, info = MODULE.split_grid(
            image,
            rows=1,
            cols=2,
            cell_size=40,
            threshold=100,
            edge_threshold=150,
            fit_scale=0.7,
            trim_border_px=0,
            edge_clean_depth=0,
            align="feet",
            component_mode="largest",
            scale_strategy="preserve",
        )

        bboxes = [frame.getbbox() for frame in frames]
        self.assertEqual(bboxes[0], bboxes[1])
        self.assertEqual(frames[0].tobytes(), frames[1].tobytes())
        self.assertEqual(info[0]["anchor_target"], info[1]["anchor_target"])
        self.assertFalse(info[0]["paste_clamped"])
        self.assertFalse(info[1]["paste_clamped"])

    def test_qc_summary_reports_model_scale_and_anchor_drift(self) -> None:
        image = Image.new("RGBA", (48, 24), MAGENTA)
        image.paste(SUBJECT, (8, 8, 12, 16))
        image.paste(SUBJECT, (32, 4, 40, 20))

        _frames, info = MODULE.split_grid(
            image,
            rows=1,
            cols=2,
            cell_size=48,
            threshold=100,
            edge_threshold=150,
            fit_scale=0.7,
            trim_border_px=0,
            edge_clean_depth=0,
            align="feet",
            component_mode="largest",
            scale_strategy="preserve",
        )

        summary = MODULE.summarize_frame_qc(info)
        self.assertEqual(summary["frame_count"], 2)
        self.assertEqual(summary["valid_frame_count"], 2)
        self.assertGreater(summary["body_scale_mean"], 0)
        self.assertGreater(summary["output_subject_height_mean"], 0)
        self.assertGreater(summary["body_scale_cv"], 0.2)
        self.assertGreater(summary["anchor_y_std"], 0.05)


class ScaleProfileTests(unittest.TestCase):
    def make_metadata(self) -> dict[str, object]:
        return {
            "target": "player",
            "mode": "run",
            "rows": 2,
            "cols": 3,
            "cell_size": 128,
            "fit_scale": 0.8,
            "trim_border": 4,
            "edge_clean_depth": 3,
            "align": "feet",
            "shared_scale": False,
            "scale_strategy": "preserve",
            "component_mode": "largest",
            "component_padding": 8,
            "min_component_area": 1,
            "edge_touch_margin": 0,
            "qc_summary": {
                "body_scale_mean": 0.2,
                "body_scale_cv": 0.03,
                "anchor_y_mean": 0.82,
                "scale_reference_height_px": 100.0,
                "legacy_body_scale_mean": 0.2,
            },
        }

    def test_profile_round_trip_and_processing_contract(self) -> None:
        profile = MODULE.build_scale_profile(self.make_metadata(), "ronin", 0.08)
        self.assertEqual(profile["output_origin"], [64.0, 116])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            loaded = MODULE.load_scale_profile(path)

        args = argparse.Namespace(
            cell_size=96,
            fit_scale=0.95,
            trim_border=0,
            edge_clean_depth=0,
            align="center",
            shared_scale=True,
            scale_strategy="fit",
            component_mode="all",
            component_padding=0,
            min_component_area=10,
            edge_touch_margin=2,
        )
        MODULE.apply_scale_profile(args, loaded)

        self.assertEqual(args.cell_size, 128)
        self.assertEqual(args.fit_scale, 0.8)
        self.assertEqual(args.align, "feet")
        self.assertEqual(args.scale_strategy, "preserve")
        self.assertEqual(args.component_mode, "largest")
        self.assertEqual(args.component_padding, 8)

    def test_profile_scale_drift_compares_generation_scale(self) -> None:
        """S02: version 2 profiles compare the output subject height; version 1 keeps its metric."""
        profile = MODULE.build_scale_profile(self.make_metadata(), "ronin", 0.08)
        self.assertEqual((profile["version"], profile["reference"]["output_subject_height_px"]), (2, 100.0))
        drift = MODULE.profile_scale_drift({"scale_reference_height_px": 110.0, "body_scale_mean": 0.2}, profile)
        self.assertAlmostEqual(drift, 0.10)
        legacy = {**profile, "version": 1}
        self.assertAlmostEqual(MODULE.profile_scale_drift({"legacy_body_scale_mean": 0.22}, legacy), 0.10)

    def test_godot_sprite3d_contract_uses_feet_origin_and_subject_height(self) -> None:
        metadata = self.make_metadata()
        metadata["duration"] = 125
        metadata["frame_labels"] = ["idle-1", "idle-2"]
        metadata["output_origin"] = [64.0, 116.0]
        metadata["qc_summary"]["output_subject_height_mean"] = 100.0

        contract = MODULE.build_godot_sprite3d_metadata(metadata, world_height=0.7)

        self.assertEqual(contract["schema"], "generate2dsprite.godot_sprite3d.v1")
        self.assertEqual(contract["sprite3d_offset"], [0.0, 52.0])
        self.assertAlmostEqual(contract["recommended_pixel_size"], 0.007)
        self.assertEqual(contract["scale_source"], "measured_subject_height")
        self.assertEqual(contract["fps"], 8.0)
        self.assertEqual(contract["frames"], ["idle-1.png", "idle-2.png"])


class AnchorLayoutTests(unittest.TestCase):
    def test_anchor_layout_repeats_identical_scale_and_feet_line(self) -> None:
        source = Image.new("RGBA", (40, 40), MAGENTA)
        source.paste(SUBJECT, (10, 5, 30, 35))

        layout = ANCHOR_MODULE.build_anchor_layout(
            source,
            rows=2,
            cols=3,
            cell_width=40,
            cell_height=40,
            subject_height_ratio=0.5,
            subject_width_ratio=0.8,
            feet_ratio=0.8,
            threshold=100,
            edge_threshold=150,
        )

        self.assertEqual(layout.size, (120, 80))
        bboxes = []
        for row in range(2):
            for col in range(3):
                cell = layout.crop((col * 40, row * 40, (col + 1) * 40, (row + 1) * 40))
                cleaned = MODULE.remove_bg_magenta(cell, 100, 150)
                bboxes.append(cleaned.getbbox())
        self.assertTrue(all(bbox == bboxes[0] for bbox in bboxes))
        self.assertEqual(bboxes[0][3], 32)


class GodotSprite3DBundleTests(unittest.TestCase):
    def make_contract(self, world_height: float, pixel_size: float = 0.0042) -> dict[str, object]:
        return {
            "schema": "generate2dsprite.godot_sprite3d.v1",
            "world_height": world_height,
            "frames": ["frame-1.png", "frame-2.png"],
            "recommended_pixel_size": pixel_size,
        }

    def test_bundle_marks_one_shots_and_preserves_default(self) -> None:
        bundle = MODULE.build_godot_sprite3d_bundle(
            {
                "idle": ("idle/godot-sprite3d.json", self.make_contract(0.7)),
                "hurt": ("hurt/godot-sprite3d.json", self.make_contract(0.705)),
            },
            default_action="idle",
            one_shot_actions={"hurt"},
        )

        self.assertEqual(bundle["default_action"], "idle")
        self.assertTrue(bundle["actions"]["idle"]["loop"])
        self.assertFalse(bundle["actions"]["hurt"]["loop"])
        self.assertLess(bundle["world_height_max_drift"], 0.02)

    def test_bundle_rejects_cross_action_world_height_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "world-height drift"):
            MODULE.build_godot_sprite3d_bundle(
                {
                    "idle": ("idle.json", self.make_contract(0.7)),
                    "attack": ("attack.json", self.make_contract(0.9)),
                },
                default_action="idle",
                one_shot_actions={"attack"},
            )

    def test_bundle_rejects_per_action_runtime_rescaling(self) -> None:
        with self.assertRaisesRegex(ValueError, "pixel-size drift"):
            MODULE.build_godot_sprite3d_bundle(
                {
                    "idle": ("idle.json", self.make_contract(0.7, 0.0042)),
                    "hurt": ("hurt.json", self.make_contract(0.7, 0.0050)),
                },
                default_action="idle",
                one_shot_actions={"hurt"},
            )

    def test_scale_profile_locks_runtime_pixel_size(self) -> None:
        metadata = ScaleProfileTests().make_metadata()
        metadata["godot_sprite3d"] = {
            "world_height": 0.7,
            "recommended_pixel_size": 0.0042,
        }
        profile = MODULE.build_scale_profile(metadata, "creature", 0.15)
        self.assertEqual(profile["godot_sprite3d"]["pixel_size"], 0.0042)


class ParserTests(unittest.TestCase):
    def test_source_edge_override_is_explicit_and_opt_in(self) -> None:
        parser = MODULE.build_parser()
        args = parser.parse_args(
            [
                "process",
                "--input", "raw.png",
                "--target", "creature",
                "--mode", "idle",
                "--output-dir", "out",
                "--allow-source-edge-touch",
            ]
        )
        self.assertTrue(args.allow_source_edge_touch)


class AlphaAndSamplingTests(unittest.TestCase):
    def split_native(self, image: Image.Image, **options):
        return MODULE.split_grid(
            image, rows=1, cols=1, cell_size=16, threshold=100, edge_threshold=150,
            fit_scale=1, scale_strategy="preserve", background_mode="native_alpha",
            resampler="nearest", **options,
        )

    def test_native_alpha_retains_magenta_and_soft_edges_through_atlas(self) -> None:
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        image.paste((255, 0, 255, 128), (4, 4, 12, 12))
        frames, _ = self.split_native(image)
        sheet = MODULE.compose_sheet(frames, 1, 1, 16)
        self.assertEqual(sheet.getpixel((8, 8)), (255, 0, 255, 128))
        self.assertEqual(sheet.tobytes(), frames[0].tobytes())

    def test_native_alpha_skips_destructive_legacy_edge_cleanup(self) -> None:
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        image.paste((5, 5, 5, 255), (0, 0, 16, 4))
        _, info = self.split_native(image, trim_border_px=4, edge_clean_depth=3)
        self.assertEqual(info[0]["source_frame_size"], [16, 16])
        self.assertEqual(info[0]["subject_area"], 64)
        self.assertTrue(info[0]["source_edge_touch"])

    def test_native_alpha_rejects_opaque_or_empty_input(self) -> None:
        for fill in ((10, 10, 10, 255), (0, 0, 0, 0)):
            with self.subTest(fill=fill), self.assertRaisesRegex(ValueError, "native_alpha"):
                MODULE.prepare_background(Image.new("RGBA", (8, 8), fill), "native_alpha", 100, 150)

    def test_nearest_resize_adds_no_new_pixel_colors(self) -> None:
        image = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        image.paste((255, 40, 10, 255), (2, 2, 4, 6))
        image.paste((20, 60, 255, 255), (4, 2, 6, 6))
        frames, _ = self.split_native(image)
        self.assertLessEqual(set(map(tuple, np.asarray(frames[0]).reshape(-1, 4))), set(map(tuple, np.asarray(image).reshape(-1, 4))))

    def test_largest_removes_disconnected_pixels_inside_its_bbox(self) -> None:
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        image.paste(SUBJECT, (3, 3, 13, 13))
        image.paste((0, 0, 0, 0), (4, 4, 12, 12))
        image.putpixel((8, 8), (255, 255, 255, 255))
        frames, info = self.split_native(image, component_mode="largest")
        self.assertEqual(info[0]["component_count"], 2)
        self.assertNotIn((255, 255, 255, 255), set(map(tuple, np.asarray(frames[0]).reshape(-1, 4))))
        self.assertEqual(int(np.count_nonzero(np.asarray(frames[0])[:, :, 3])), 36)

    def test_all_component_area_threshold_really_filters_noise(self) -> None:
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        image.paste(SUBJECT, (5, 5, 9, 9))
        image.putpixel((12, 12), (255, 255, 255, 255))
        frames, _ = self.split_native(image, component_mode="all", min_component_area=2)
        self.assertNotIn((255, 255, 255, 255), set(map(tuple, np.asarray(frames[0]).reshape(-1, 4))))

    def test_remainder_pixels_are_not_silently_discarded(self) -> None:
        with self.assertRaisesRegex(ValueError, "not divisible"):
            MODULE.split_grid(Image.new("RGBA", (17, 16), MAGENTA), 1, 2, 16, 100, 150)

    def test_invalid_fit_scale_is_rejected(self) -> None:
        for scale in (0, -1, 1.5, float("nan")):
            with self.subTest(scale=scale), self.assertRaisesRegex(ValueError, "fit_scale"):
                MODULE.split_grid(Image.new("RGBA", (16, 16), MAGENTA), 1, 1, 16, 100, 150, fit_scale=scale)

    def test_native_anchor_guide_preserves_real_alpha(self) -> None:
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        image.paste((255, 0, 255, 128), (4, 4, 12, 12))
        result = ANCHOR_MODULE.build_anchor_layout(
            image, rows=2, cols=2, cell_width=16, cell_height=16,
            subject_height_ratio=.5, subject_width_ratio=.5, feet_ratio=.75,
            threshold=100, edge_threshold=150, background_mode="native_alpha", resampler="nearest",
        )
        self.assertEqual(result.getpixel((8, 8)), (255, 0, 255, 128))
        self.assertEqual(result.getpixel((0, 0)), (0, 0, 0, 0))


class SafePublicationTests(unittest.TestCase):
    def args(self, source: Path, output: Path, *extra: str):
        return MODULE.build_parser().parse_args([
            "process", "--input", str(source), "--output-dir", str(output),
            "--target", "asset", "--mode", "idle", "--rows", "1", "--cols", "1",
            "--strict-qc", "--trim-border", "0", "--edge-clean-depth", "0", *extra,
        ])

    def test_strict_failure_leaves_no_final_bundle_or_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, output, profile = base / "raw.png", base / "out", base / "profile.json"
            Image.new("RGBA", (16, 16), MAGENTA).save(source)
            args = self.args(source, output, "--write-scale-profile", str(profile))
            with self.assertRaisesRegex(ValueError, "empty"):
                MODULE.cmd_process(args)
            self.assertFalse(output.exists())
            self.assertFalse(profile.exists())
            self.assertEqual(sorted(p.name for p in base.iterdir()), ["raw.png"])

    def test_existing_bundle_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            output = base / "out"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("accepted", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                MODULE.cmd_process(self.args(base / "absent.png", output))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "accepted")

    def test_label_prefix_cannot_escape_staged_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, output = base / "raw.png", base / "out"
            image = Image.new("RGBA", (16, 16), MAGENTA)
            image.paste(SUBJECT, (4, 4, 12, 12))
            image.save(source)
            with self.assertRaisesRegex(ValueError, "label prefix"):
                MODULE.cmd_process(self.args(source, output, "--label-prefix", "../../escape"))
            self.assertFalse(output.exists())
            self.assertFalse((base / "escape-1.png").exists())

    def test_strict_single_rejects_empty_art(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "raw.png"
            Image.new("RGBA", (16, 16), MAGENTA).save(source)
            args = MODULE.build_parser().parse_args([
                "process", "--input", str(source), "--output-dir", str(base / "out"),
                "--target", "asset", "--mode", "single", "--strict-qc",
            ])
            with self.assertRaisesRegex(ValueError, "empty"):
                MODULE.cmd_process(args)
            self.assertFalse((base / "out").exists())

    def test_successful_cli_bundle_preserves_alpha_and_records_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, output = base / "raw.png", base / "out"
            image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
            image.paste((255, 0, 255, 128), (4, 4, 12, 12))
            image.save(source)
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.cmd_process(self.args(source, output, "--background-mode", "native_alpha", "--resampler", "nearest",
                                             "--pixel-scale", "8"))
            meta = json.loads((output / "pipeline-meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["background_mode"], "native_alpha")
            with Image.open(output / "sheet-transparent.png") as sheet:
                self.assertEqual(sheet.getpixel((64, 64)), (255, 0, 255, 128))


class RegisteredFrameExportTests(unittest.TestCase):
    @staticmethod
    def load_helper(name: str):
        spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH.with_name(name + ".py"))
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_registered_clips_preserve_png_bytes_root_and_one_shot_timing(self) -> None:
        clips = self.load_helper("build_animation_clips")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            for index in range(2):
                frame = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
                frame.paste((200, 60, 20, 128), (5, 5-index, 10, 12-index))
                frame.save(base / f"pose-{index}.png")
            contract = {"frames": ["pose-0.png", "pose-1.png"], "anchor_px": [8, 13],
                        "clips": {"attack": {"frames": [0, 1], "duration_ms": [80, 160], "loop": False}},
                        "states": {"attacking": "attack"}}
            manifest = base / "clips.json"
            manifest.write_text(json.dumps(contract), encoding="utf-8")
            result = clips.build(manifest, base / "out")
            self.assertEqual(result["anchor_px"], [8, 13])
            self.assertEqual(result["clips"]["attack"]["preview"]["total_duration_ms"], 240)
            self.assertFalse(result["clips"]["attack"]["loop"])
            for index in range(2):
                self.assertEqual((base / f"pose-{index}.png").read_bytes(),
                                 (base / "out" / "frames" / f"frame-{index:02d}.png").read_bytes())

    def test_complete_frames_are_not_keyed_or_resized(self) -> None:
        assembler = self.load_helper("assemble_frames")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            paths = []
            for index in range(2):
                image = Image.new("RGB", (23, 17), (255, 0, 255))
                image.putpixel((index, index), (0, 10, 20))
                path = base / f"phase-{index}.png"
                image.save(path)
                paths.append(str(path))
            args = assembler.build_parser().parse_args(["--input", *paths, "--duration", "90", "--output-dir", str(base / "out")])
            result = assembler.assemble(args)
            self.assertEqual(result["frame_size"], [23, 17])
            self.assertEqual(result["total_duration_ms"], 180)
            with Image.open(base / "out" / "frames" / "frame-00.png") as output:
                self.assertEqual(output.getpixel((10, 10)), (255, 0, 255))

    def test_clips_reject_mismatched_canvas_without_publishing(self) -> None:
        clips = self.load_helper("build_animation_clips")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            for index in range(2):
                image = Image.new("RGBA", (16+index, 16), (0, 0, 0, 0))
                image.putpixel((5, 5), SUBJECT)
                image.save(base / f"pose-{index}.png")
            manifest = base / "clips.json"
            manifest.write_text(json.dumps({"frames": ["pose-0.png", "pose-1.png"], "anchor_px": [8, 13]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "same native canvas"):
                clips.build(manifest, base / "out")
            self.assertFalse((base / "out").exists())


class LegacyDefaultParserTests(unittest.TestCase):
    def test_legacy_background_and_resize_defaults_are_preserved(self) -> None:
        """Profile-governed flags parse as unset (S09); the effective defaults stay the legacy ones."""
        with tempfile.TemporaryDirectory() as temp:
            args = MODULE.build_parser().parse_args([
                "process", "--input", "raw.png", "--target", "asset", "--mode", "idle",
                "--output-dir", str(Path(temp) / "out"),
            ])
            self.assertIsNone(args.background_mode)
            options = MODULE.plan_process(args).options
        self.assertEqual(options.background_mode, "chroma_key")
        self.assertEqual(options.resampler, "lanczos")
        self.assertEqual(options.despill_radius, 0)


class DespillTests(unittest.TestCase):
    def fixture(self):
        image = Image.new("RGBA", (13, 13), (20, 0, 25, 0))
        image.paste((30, 100, 40, 255), (1, 1, 12, 12))
        for x in (1, 2, 3, 4, 6):
            image.putpixel((x, 6), (90, 25, 100, 255))
        image.putpixel((11, 6), (90, 25, 100, 128))
        image.putpixel((1, 2), (35, 25, 40, 255))  # Below the fixed spill margin.
        image.putpixel((1, 3), (100, 65, 40, 255))  # Brown edge.
        return image

    def test_dark_magenta_fringe_is_reduced_only_within_requested_source_radius(self):
        image = self.fixture()
        original = image.tobytes()
        for radius in (1, 2, 3):
            with self.subTest(radius=radius):
                output = MODULE.prepare_background(image, "chroma_key", 100, 150, radius)
                for x in (1, 2, 3, 4):
                    expected = (25, 25, 35, 255) if x <= radius else image.getpixel((x, 6))
                    self.assertEqual(output.getpixel((x, 6)), expected)
                self.assertEqual(output.getpixel((6, 6)), image.getpixel((6, 6)))
                self.assertEqual(output.getpixel((11, 6)), (25, 25, 35, 128))
                self.assertEqual(output.getchannel("A").tobytes(), image.getchannel("A").tobytes())
        self.assertEqual(image.tobytes(), original)

    def test_default_off_and_unaffected_colors_are_unchanged(self):
        image = self.fixture()
        self.assertEqual(MODULE.prepare_background(image, "chroma_key", 100, 150).tobytes(), image.tobytes())
        output = MODULE.despill_chroma_edges(image, 3)
        for point in ((0, 6), (1, 2), (1, 3), (1, 4), (6, 6)):
            with self.subTest(point=point):
                self.assertEqual(output.getpixel(point), image.getpixel(point))

    def test_canvas_exterior_is_not_treated_as_a_transparent_boundary(self):
        image = Image.new("RGBA", (9, 9), (90, 25, 100, 255))
        self.assertEqual(MODULE.despill_chroma_edges(image, 3).tobytes(), image.tobytes())

    def test_native_alpha_and_opaque_modes_ignore_despill(self):
        for mode, image in (("native_alpha", self.fixture()),
                            ("opaque", Image.new("RGBA", (9, 9), (90, 25, 100, 255)))):
            with self.subTest(mode=mode), mock.patch.object(MODULE, "despill_chroma_edges") as despill:
                output = MODULE.prepare_background(image, mode, 100, 150, 3)
                self.assertEqual(output.tobytes(), image.tobytes())
                despill.assert_not_called()

    def test_radius_validation_is_bounded_and_integer(self):
        for radius in (-1, 4, 1.5, True):
            with self.subTest(radius=radius), self.assertRaisesRegex(ValueError, "integer from 0 to 3"):
                MODULE.despill_chroma_edges(self.fixture(), radius)


class AlphaAndResamplingTests(unittest.TestCase):
    def split(self, image, **options):
        kwargs = dict(rows=1, cols=1, cell_size=image.width, threshold=100,
                      edge_threshold=150, fit_scale=1, background_mode="native_alpha",
                      resampler="nearest", scale_strategy="preserve")
        kwargs.update(options)
        return MODULE.split_grid(image, **kwargs)

    def test_partial_rgba_survives_frame_and_atlas_without_extra_alpha_multiplication(self):
        image = Image.new("RGBA", (8, 8), (200, 100, 50, 128))
        original = image.tobytes()
        for strategy in ("fit", "preserve"):
            with self.subTest(strategy=strategy):
                frames, _info = self.split(image, scale_strategy=strategy)
                atlas = MODULE.compose_sheet(frames, 1, 1, 8)
                self.assertEqual(frames[0].tobytes(), original)
                self.assertEqual(atlas.tobytes(), original)
        self.assertEqual(image.tobytes(), original)

    def test_native_alpha_keeps_magenta_and_dark_edge_pixels_without_cleanup(self):
        image = Image.new("RGBA", (8, 8), MAGENTA)
        image.putpixel((0, 1), (0, 0, 0, 255))
        image.putpixel((0, 0), (0, 0, 0, 0))
        image.putpixel((2, 2), (200, 60, 40, 127))
        with mock.patch.object(MODULE, "remove_bg_magenta", wraps=MODULE.remove_bg_magenta) as chroma, \
             mock.patch.object(MODULE, "clean_edges", wraps=MODULE.clean_edges) as edges:
            frames, info = self.split(image)
        self.assertEqual(frames[0].tobytes(), image.tobytes())
        self.assertEqual(info[0]["source_frame_size"], [8, 8])
        chroma.assert_not_called()
        edges.assert_not_called()

    def test_opaque_mode_keeps_colors_but_rejects_input_with_transparency(self):
        image = Image.new("RGBA", (8, 8), MAGENTA)
        image.putpixel((0, 0), (0, 0, 0, 255))
        frames, _info = self.split(image, background_mode="opaque")
        self.assertEqual(frames[0].tobytes(), image.tobytes())
        image.putpixel((1, 1), (20, 30, 40, 128))
        with self.assertRaisesRegex(ValueError, "fully opaque"):
            self.split(image, background_mode="opaque")

    def test_single_sprite_native_alpha_keeps_semtransparent_magenta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGBA", (8, 8), (255, 0, 255, 128)).save(root / "single.png")
            args = MODULE.build_parser().parse_args([
                "process", "--input", str(root / "single.png"), "--target", "asset", "--mode", "single",
                "--output-dir", str(root / "out"), "--background-mode", "native_alpha", "--resampler", "nearest",
                "--pixel-scale", "1", "--single-size", "8"])
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.cmd_process(args)
            with Image.open(root / "out" / "clean.png") as output:
                self.assertEqual(output.getpixel((4, 4)), (255, 0, 255, 128))

    def test_native_alpha_rejects_no_transparency_and_fully_empty_inputs(self):
        for mode, color in (("RGB", (220, 220, 220)), ("RGBA", (220, 220, 220, 255)),
                            ("RGBA", (0, 0, 0, 0))):
            with self.subTest(mode=mode, color=color):
                image = Image.new(mode, (8, 8), color)
                with self.assertRaisesRegex(ValueError, "actual transparency and visible pixels"):
                    self.split(image)

    def test_nearest_preserves_two_color_pixel_art_when_enlarging(self):
        image = Image.new("RGBA", (8, 8), (0, 0, 0, 255))
        for y in range(8):
            for x in range(8):
                if (x + y) % 2:
                    image.putpixel((x, y), (255, 255, 255, 255))
        nearest, _info = self.split(image, cell_size=16, background_mode="opaque")
        smooth, _info = self.split(image, cell_size=16, background_mode="opaque", resampler="lanczos")
        self.assertEqual(len(nearest[0].getcolors(256)), 2)
        self.assertGreater(len(smooth[0].getcolors(256)), 2)

    def test_nondivisible_grid_fails_before_cleanup_without_discarding_edges(self):
        image = Image.new("RGBA", (9, 7), SUBJECT)
        original = image.tobytes()
        with mock.patch.object(MODULE, "prepare_sheet") as prepare:
            with self.assertRaisesRegex(ValueError, "remainder pixels"):
                self.split(image, rows=2, cols=2)
        prepare.assert_not_called()
        self.assertEqual(image.tobytes(), original)

    def test_invalid_grid_dimensions_are_rejected(self):
        image = Image.new("RGBA", (8, 8), SUBJECT)
        for options in ({"rows": 0}, {"cols": -1}, {"cell_size": 0}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "positive"):
                self.split(image, **options)


class GifExportTests(unittest.TestCase):
    def test_fully_opaque_rgb_input_does_not_create_transparent_pixels(self):
        image = Image.new("RGB", (16, 16), (30, 80, 180))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opaque.gif"
            MODULE.save_transparent_gif([image], path, 250)
            with Image.open(path) as decoded:
                self.assertNotIn("transparency", decoded.info)
                self.assertEqual(decoded.convert("RGBA").tobytes(), image.convert("RGBA").tobytes())

    def test_foreground_matching_old_color_key_survives_and_motion_clears_background(self):
        first = Image.new("RGBA", (16, 16))
        second = Image.new("RGBA", (16, 16))
        color = (255, 0, 254, 255)
        first.paste(color, (2, 2, 6, 6))
        second.paste(color, (9, 9, 13, 13))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "move.gif"
            MODULE.save_transparent_gif([first, second], path, 250)
            with Image.open(path) as decoded:
                self.assertEqual(decoded.n_frames, 2)
                self.assertEqual(decoded.info.get("loop"), 0)
                self.assertEqual(decoded.info.get("duration"), 250)
                self.assertEqual(decoded.convert("RGBA").getpixel((3, 3)), color)
                decoded.seek(1)
                rgba = decoded.convert("RGBA")
                self.assertEqual(rgba.getpixel((3, 3))[3], 0)
                self.assertEqual(rgba.getpixel((10, 10)), color)
                self.assertEqual(decoded.info.get("duration"), 250)

    def test_gif_alpha_threshold_is_explicit_while_png_alpha_is_unchanged(self):
        image = Image.new("RGBA", (2, 1))
        image.putpixel((0, 0), (200, 10, 20, 127))
        image.putpixel((1, 0), (200, 10, 20, 128))
        original = image.tobytes()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alpha.gif"
            MODULE.save_transparent_gif([image], path, 250)
            with Image.open(path) as decoded:
                rgba = decoded.convert("RGBA")
                self.assertEqual(rgba.getpixel((0, 0))[3], 0)
                self.assertEqual(rgba.getpixel((1, 0)), (200, 10, 20, 255))
        self.assertEqual(image.tobytes(), original)


class PublicationTests(unittest.TestCase):
    def make_args(self, root, *extra):
        source = root / "input.png"
        image = Image.new("RGBA", (32, 32), MAGENTA)
        image.paste(SUBJECT, (10, 8, 22, 24))
        image.save(source)
        return MODULE.build_parser().parse_args([
            "process", "--input", str(source), "--target", "asset", "--mode", "idle",
            "--rows", "1", "--cols", "1", "--output-dir", str(root / "output"), *extra,
        ])

    def test_strict_failure_publishes_no_output_or_scale_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            args = self.make_args(root, "--strict-qc", "--write-scale-profile", str(profile))
            Image.new("RGBA", (32, 32), MAGENTA).save(args.input)
            with self.assertRaisesRegex(ValueError, "QC failed:.*empty frames"):
                MODULE.cmd_process(args)
            self.assertFalse(args.output_dir.exists())
            self.assertFalse(profile.exists())
            self.assertEqual(list(root.glob(".output.staging-*")), [])

    def test_existing_output_and_sidecar_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.make_args(root)
            args.output_dir.mkdir()
            marker = args.output_dir / "keep.txt"
            marker.write_bytes(b"existing output")
            with self.assertRaises(FileExistsError):
                MODULE.cmd_process(args)
            self.assertEqual(marker.read_bytes(), b"existing output")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "profile.json"
            sidecar.write_bytes(b"existing sidecar")
            args = self.make_args(root, "--write-scale-profile", str(sidecar))
            with self.assertRaises(FileExistsError):
                MODULE.cmd_process(args)
            self.assertEqual(sidecar.read_bytes(), b"existing sidecar")
            self.assertFalse(args.output_dir.exists())

    def test_single_cleanup_and_internal_sidecars_have_final_contract_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "output" / "scale.json"
            args = self.make_args(root, "--strict-qc", "--godot-world-height", "0.7",
                                  "--write-scale-profile", str(profile), "--key-quality", "hard")
            source_bytes = args.input.read_bytes()
            with mock.patch.object(MODULE, "remove_bg_magenta", wraps=MODULE.remove_bg_magenta) as remove:
                MODULE.cmd_process(args)
            self.assertEqual(remove.call_count, 1)
            metadata = json.loads((args.output_dir / "pipeline-meta.json").read_text())
            contract = json.loads((args.output_dir / "godot-sprite3d.json").read_text())
            saved_profile = json.loads(profile.read_text())
            # Manifest paths are relative POSIX fileRefs, never absolute (F-03 publication, MAP-24).
            self.assertEqual(metadata["scale_profile_output"],
                             {"path": "scale.json", "sha256": MODULE.forge_core.sha256_file(profile)})
            self.assertEqual(metadata["godot_sprite3d_output"]["path"], "godot-sprite3d.json")
            self.assertNotIn(".stage-", json.dumps(metadata))
            self.assertNotIn(".sidecars-", json.dumps(metadata))
            self.assertEqual(contract["frames"], ["idle-1.png"])
            self.assertEqual(saved_profile["godot_sprite3d"]["pixel_size"], contract["recommended_pixel_size"])
            self.assertEqual(args.input.read_bytes(), source_bytes)

    def test_external_sidecars_publish_with_final_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            godot, profile = root / "contracts" / "sprite.json", root / "profiles" / "scale.json"
            args = self.make_args(root, "--godot-world-height", "0.7",
                                  "--write-godot-sprite3d-meta", str(godot),
                                  "--write-scale-profile", str(profile))
            MODULE.cmd_process(args)
            metadata = json.loads((args.output_dir / "pipeline-meta.json").read_text())
            self.assertEqual(metadata["godot_sprite3d_output"]["path"], "../contracts/sprite.json")
            self.assertEqual(metadata["scale_profile_output"]["path"], "../profiles/scale.json")
            self.assertTrue(godot.is_file() and profile.is_file())
            # S19: the contract's frames resolve relative to the contract file.
            contract = json.loads(godot.read_text(encoding="utf-8"))
            self.assertEqual(contract["frames"], ["../output/idle-1.png"])
            self.assertTrue(all((godot.parent / frame).is_file() for frame in contract["frames"]))
            self.assertNotIn(".staging-", json.dumps(metadata))

    def test_late_profile_failure_keeps_all_outputs_unpublished(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            godot, profile = root / "sprite.json", root / "scale.json"
            args = self.make_args(root, "--godot-world-height", "0.7",
                                  "--write-godot-sprite3d-meta", str(godot),
                                  "--write-scale-profile", str(profile))
            with mock.patch.object(MODULE, "build_scale_profile", side_effect=ValueError("profile failure")):
                with self.assertRaisesRegex(ValueError, "profile failure"):
                    MODULE.cmd_process(args)
            self.assertFalse(args.output_dir.exists())
            self.assertFalse(godot.exists())
            self.assertFalse(profile.exists())

    def test_publish_failure_rolls_back_own_sidecar_without_removing_concurrent_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            godot, profile = root / "sprite.json", root / "scale.json"
            args = self.make_args(root, "--godot-world-height", "0.7",
                                  "--write-godot-sprite3d-meta", str(godot),
                                  "--write-scale-profile", str(profile))
            original = MODULE.forge_core.publish_file_no_replace
            def interrupted_publish(source, destination):
                if destination == profile:
                    profile.write_bytes(b"concurrent file")
                    raise FileExistsError("concurrent creation")
                original(source, destination)
            with mock.patch.object(MODULE.forge_core, "publish_file_no_replace", side_effect=interrupted_publish):
                with self.assertRaises(FileExistsError):
                    MODULE.cmd_process(args)
            self.assertFalse(args.output_dir.exists())
            self.assertFalse(godot.exists())
            self.assertEqual(profile.read_bytes(), b"concurrent file")

    def test_odd_grid_failure_does_not_publish_partial_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.make_args(root)
            args.cols = 2
            Image.new("RGBA", (33, 32), SUBJECT).save(args.input)
            original = args.input.read_bytes()
            with self.assertRaisesRegex(ValueError, "remainder pixels"):
                MODULE.cmd_process(args)
            self.assertFalse(args.output_dir.exists())
            self.assertEqual(args.input.read_bytes(), original)

    def test_native_alpha_rgb_checkerboard_is_rejected_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.make_args(root, "--background-mode", "native_alpha", "--strict-qc")
            checker = Image.new("RGB", (32, 32), (240, 240, 240))
            for y in range(0, 32, 8):
                for x in range(0, 32, 8):
                    if (x // 8 + y // 8) % 2:
                        checker.paste((170, 170, 170), (x, y, x + 8, y + 8))
            checker.save(args.input)
            with self.assertRaisesRegex(ValueError, "baked checkerboard is not transparent"):
                MODULE.cmd_process(args)
            self.assertFalse(args.output_dir.exists())

    def test_sidecar_cannot_overwrite_a_generated_frame_inside_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.make_args(root, "--write-scale-profile", str(root / "output" / "idle-1.png"))
            with self.assertRaises(FileExistsError):
                MODULE.cmd_process(args)
            self.assertFalse(args.output_dir.exists())

    def test_directory_publish_does_not_replace_a_competing_empty_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage, final = root / "stage", root / "final"
            stage.mkdir()
            (stage / "keep.txt").write_bytes(b"staged content")
            final.mkdir()
            identity = final.stat().st_ino
            # Simulate a destination appearing immediately after the preflight check.
            with mock.patch.object(MODULE.forge_core.os.path, "lexists", return_value=False):
                with self.assertRaises(FileExistsError):
                    MODULE.forge_core.publish_directory_no_replace(stage, final)
            self.assertEqual(final.stat().st_ino, identity)
            self.assertEqual(list(final.iterdir()), [])
            self.assertEqual((stage / "keep.txt").read_bytes(), b"staged content")

    def test_despill_is_recorded_and_applied_to_cleaned_source_before_scaling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.make_args(root, "--despill-radius", "2")
            image = DespillTests().fixture()
            image.save(args.input)
            MODULE.cmd_process(args)
            metadata = json.loads((args.output_dir / "pipeline-meta.json").read_text())
            self.assertEqual(metadata["despill_radius"], 2)
            with Image.open(args.output_dir / "raw-sheet-clean.png") as cleaned:
                self.assertEqual(cleaned.getpixel((1, 6)), (25, 25, 35, 255))
                self.assertEqual(cleaned.getpixel((3, 6)), (90, 25, 100, 255))


class ExtendedScaleProfileTests(unittest.TestCase):
    def test_new_profiles_lock_background_and_resampler_without_breaking_old_profiles(self):
        metadata = ScaleProfileTests().make_metadata()
        metadata.update(background_mode="native_alpha", resampler="nearest")
        profile = MODULE.build_scale_profile(metadata, "soft-sprite", 0.08)
        args = argparse.Namespace(background_mode="chroma_key", resampler="lanczos")
        MODULE.apply_scale_profile(args, profile)
        self.assertEqual((args.background_mode, args.resampler), ("native_alpha", "nearest"))
        del profile["processing"]["background_mode"]
        del profile["processing"]["resampler"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            legacy = MODULE.load_scale_profile(path)
        MODULE.apply_scale_profile(args, legacy)
        self.assertEqual((args.background_mode, args.resampler), ("native_alpha", "nearest"))

    def test_profiles_store_optional_despill_without_requiring_it_in_legacy_profiles(self):
        metadata = ScaleProfileTests().make_metadata()
        metadata["despill_radius"] = 2
        profile = MODULE.build_scale_profile(metadata, "edge-cleaned", 0.08)
        args = argparse.Namespace(despill_radius=0)
        MODULE.apply_scale_profile(args, profile)
        self.assertEqual(args.despill_radius, 2)
        del profile["processing"]["despill_radius"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            legacy = MODULE.load_scale_profile(path)
        MODULE.apply_scale_profile(args, legacy)
        self.assertEqual(args.despill_radius, 2)


# --------------------------------------------------------------------------- B01 (geometry v2 upgrade)

def run_process(*argv: str) -> tuple[dict, str, Path]:
    """Run ``process`` in-process; returns (pipeline-meta, stderr, output dir)."""
    args = MODULE.build_parser().parse_args(["process", *argv])
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        MODULE.cmd_process(args)
    summary = json.loads(stdout.getvalue().strip().splitlines()[-1])
    assert summary["output_dir"] == str(args.output_dir.resolve())
    meta = json.loads((args.output_dir / "pipeline-meta.json").read_text(encoding="utf-8"))
    return meta, stderr.getvalue(), args.output_dir


def cli(*argv: str, cwd: Path | None = None):
    return run_cli([SCRIPT_PATH, *argv], cwd=cwd)


def leftovers(root: Path) -> list[str]:
    """Hidden stage or sidecar directories that a failed run must not leave behind."""
    return sorted(path.name for path in root.iterdir() if path.name.startswith("."))


class SharedCoreAdoptionTests(unittest.TestCase):
    """B01-T2: forge_core / forge_matte adoption (F-03, S03, S04, S16)."""

    def test_diagonal_sword_kept_8conn(self) -> None:
        """r01: 8-connected components keep 1-px diagonal strokes; --connectivity 4 is the legacy split."""
        body, blade = (40, 60, 90, 255), (230, 230, 240, 255)
        sword = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        sword.paste(body, (10, 10, 20, 20))
        for step in range(11):
            sword.putpixel((20 + step, 20 + step), blade)
        spear = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        spear.paste(body, (2, 2, 8, 8))
        for step in range(15):
            spear.putpixel((20 + step, 5 + step), blade)

        def run(image, **options):
            frames, info = MODULE.split_grid(
                image, rows=1, cols=1, cell_size=40, fit_scale=1.0, scale_strategy="preserve",
                background_mode="native_alpha", resampler="nearest", **options)
            pixels = np.asarray(frames[0])
            return int(np.all(pixels[..., :3] == blade[:3], axis=2).sum()), int((pixels[..., 3] > 0).sum()), info[0]

        kept, visible, info = run(sword, component_mode="largest")
        self.assertEqual((kept, visible, info["component_count"]), (11, 111, 1))
        kept, visible, info = run(spear, min_component_area=2)
        self.assertEqual((kept, visible), (15, 51))
        kept, visible, info = run(sword, component_mode="largest", connectivity=4)
        self.assertEqual((kept, visible, info["component_count"]), (0, 100, 12))
        kept, _visible, _info = run(spear, min_component_area=2, connectivity=4)
        self.assertEqual(kept, 0)

    def test_16bit_gray_not_white(self) -> None:
        """r10, S16: a 16-bit grey PNG keeps its grey (high byte) instead of turning white."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            grey = np.full((64, 64), 40000, np.uint16)
            grey[16:48, 20:44] = 8000
            Image.frombytes("I;16", (64, 64), grey.astype("<u2").tobytes()).save(root / "grey16.png")
            meta, _stderr, out = run_process(
                "--input", str(root / "grey16.png"), "--target", "asset", "--mode", "single",
                "--output-dir", str(root / "out"), "--background-mode", "opaque", "--resampler", "nearest",
                "--pixel-scale", "2")
            self.assertEqual(meta["provenance"]["bit_depth"], 16)
            self.assertIn("16-bit grey", meta["provenance"]["conversion"])
            with Image.open(out / "clean.png") as clean:
                pixels = np.asarray(clean.convert("RGBA"))
            visible = pixels[pixels[..., 3] > 0]
            self.assertEqual({tuple(color) for color in visible.tolist()},
                             {(8000 >> 8,) * 3 + (255,), (40000 >> 8,) * 3 + (255,)})

    def test_animated_input_rejected(self) -> None:
        """r10, S16: an animated PNG is refused instead of silently using frame 0."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            first.paste(SUBJECT, (8, 8, 24, 24))
            second = Image.new("RGBA", (32, 32), (200, 40, 40, 255))
            first.save(root / "anim.png", save_all=True, append_images=[second], duration=100)
            result = cli("process", "--input", str(root / "anim.png"), "--target", "asset", "--mode", "single",
                         "--output-dir", str(root / "out"), "--background-mode", "native_alpha")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("error: Input is animated (2 frames)", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((root / "out").exists())

    def test_hard_key_and_despill_stay_bit_exact(self) -> None:
        """S03, S24: --key-quality hard is the cfed170 keyer byte for byte (forge_matte.legacy_hard_key),
        and --despill-radius keeps the cfed170 edge despill."""
        sheet = make_magenta_sheet(2, 2, 48, fringe=True)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sheet.save(root / "sheet.png")
            meta, _stderr, out = run_process(
                "--input", str(root / "sheet.png"), "--target", "asset", "--mode", "idle",
                "--output-dir", str(root / "out"), "--key-quality", "hard", "--despill-radius", "2")
            with Image.open(out / "raw-sheet-clean.png") as cleaned:
                actual = np.asarray(cleaned.convert("RGBA"))
        expected = np.array(MODULE.despill_chroma_edges(MODULE.forge_matte.legacy_hard_key(sheet, 100, 150), 2))
        expected[expected[..., 3] == 0] = 0  # published PNGs zero RGB under alpha 0
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual((meta["matte"]["quality"], meta["matte"]["requested_quality"]), ("hard", "hard"))
        self.assertGreater(meta["matte"]["despill"]["changed_px"], 0)

    def test_split_grid_accepts_grid_options_and_legacy_parameters(self) -> None:
        """S28 (partial): GridOptions carries every setting; the legacy positional call still works."""
        sheet = make_magenta_sheet(1, 2, 32)
        options = MODULE.GridOptions(1, 2, 32, trim_border_px=0, edge_clean_depth=0)
        frames_a, info_a = MODULE.split_grid(sheet, options)
        frames_b, info_b = MODULE.split_grid(sheet, 1, 2, 32, 100, 150, 0.85, 0, 0)
        self.assertEqual([frame.tobytes() for frame in frames_a], [frame.tobytes() for frame in frames_b])
        self.assertEqual(info_a, info_b)
        with self.assertRaisesRegex(ValueError, "Unknown anchor mode"):
            MODULE.GridOptions(1, 1, 16, anchor_mode="toes")

    @pytest.mark.perf
    def test_process_2048_chroma_under_3s(self) -> None:
        """S03 (22-29 s per 2048^2 chroma sheet in cfed170): the shared-core pipeline with the binary
        keyer S03 measured processes a 2048^2 4x4 sheet end to end in at most 3 s (best of 2)."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_magenta_sheet(4, 4, 512, margin=100, fringe=True).save(root / "sheet.png")
            make_magenta_sheet(4, 4, 64).save(root / "warm.png")
            common = ("--target", "asset", "--mode", "sheet", "--rows", "4", "--cols", "4", "--key-quality", "hard")
            run_process("--input", str(root / "warm.png"), "--output-dir", str(root / "warm"), *common)
            best = math.inf
            for attempt in range(2):
                started = time.perf_counter()
                meta, _stderr, _out = run_process("--input", str(root / "sheet.png"),
                                                  "--output-dir", str(root / f"run{attempt}"), *common)
                best = min(best, time.perf_counter() - started)
            self.assertEqual(meta["qc_summary"]["valid_frame_count"], 16)
            self.assertLessEqual(best, 3.0)


class SilentErrorTests(unittest.TestCase):
    """B01-T3: invalid input fails with a clean ``error:`` line and publishes nothing (S08, S09, S18, S22, S27)."""

    def sheet(self, root: Path, rows: int = 2, cols: int = 2, cell: int = 64) -> Path:
        path = root / "sheet.png"
        make_magenta_sheet(rows, cols, cell).save(path)
        return path

    def test_typo_mode_fails(self) -> None:
        """r02, S08: a mistyped or grid-less mode no longer becomes one shrunken clean.png."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.sheet(root)
            for target, mode, message in (("player", "idel", "Mode 'idel' is invalid for target 'player'"),
                                          ("asset", "sheet", "pass --rows and --cols"),
                                          ("creature", "player_sheet", "invalid for target 'creature'")):
                with self.subTest(target=target, mode=mode):
                    result = cli("process", "--input", str(source), "--target", target, "--mode", mode,
                                 "--output-dir", str(root / "out"), "--strict-qc")
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("error: ", result.stderr)
                    self.assertIn(message, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse((root / "out").exists())

    def make_profile(self, root: Path) -> Path:
        reference = Image.new("RGB", (128, 128), (255, 0, 255))
        for row in range(2):
            for col in range(2):
                reference.paste((40, 90, 150), (col * 64 + 20, row * 64 + 12, col * 64 + 44, row * 64 + 52))
        reference.save(root / "reference.png")
        profile = root / "profile.json"
        run_process("--input", str(root / "reference.png"), "--target", "asset", "--mode", "idle",
                    "--output-dir", str(root / "reference"), "--align", "feet", "--scale-strategy", "preserve",
                    "--write-scale-profile", str(profile))
        native = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        for row in range(2):
            for col in range(2):
                native.paste((40, 90, 150, 255), (col * 64 + 20, row * 64 + 12, col * 64 + 44, row * 64 + 52))
                native.paste((190, 40, 200, 255), (col * 64 + 22, row * 64 + 24, col * 64 + 42, row * 64 + 36))
        native.save(root / "native-purple.png")
        return profile

    def test_profile_conflict_errors(self) -> None:
        """r17, S09: explicit flags that differ from --scale-profile fail unless --profile-override."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = self.make_profile(root)
            base = ["--input", str(root / "native-purple.png"), "--target", "asset", "--mode", "idle",
                    "--background-mode", "native_alpha", "--resampler", "nearest", "--pixel-scale", "2",
                    "--scale-profile", str(profile)]
            args = MODULE.build_parser().parse_args(["process", *base, "--output-dir", str(root / "out")])
            with self.assertRaises(ValueError) as caught:
                MODULE.cmd_process(args)
            message = str(caught.exception)
            self.assertIn("--background-mode native_alpha (profile: chroma_key)", message)
            self.assertIn("--resampler nearest (profile: lanczos)", message)
            self.assertIn("--profile-override", message)
            self.assertFalse((root / "out").exists())

            meta, stderr, out = run_process(*base, "--output-dir", str(root / "override"), "--profile-override")
            self.assertEqual((meta["background_mode"], meta["resampler"]), ("native_alpha", "nearest"))
            overridden = {item["key"]: (item["profile"], item["flag"]) for item in meta["profile_applied"]["overrides"]}
            self.assertEqual(overridden["background_mode"], ("chroma_key", "native_alpha"))
            self.assertEqual(meta["profile_applied"]["applied"]["scale_strategy"], "preserve")
            self.assertIn("scale profile", stderr)
            with Image.open(out / "idle-1.png") as frame:
                pixels = np.asarray(frame)
            purple = np.all(np.abs(pixels[..., :3].astype(int) - (190, 40, 200)) < 30, axis=2) & (pixels[..., 3] > 0)
            self.assertGreater(int(purple.sum()), 0)

    def test_profile_applies_matching_flags_and_reports_values(self) -> None:
        """S09: a flag equal to the profile is not a conflict; applied values reach stderr and metadata."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = self.make_profile(root)
            meta, stderr, _out = run_process(
                "--input", str(root / "reference.png"), "--target", "asset", "--mode", "idle",
                "--output-dir", str(root / "again"), "--align", "feet", "--scale-profile", str(profile))
            self.assertEqual(meta["profile_applied"]["overrides"], [])
            self.assertEqual(meta["profile_applied"]["path"]["path"], "../profile.json")
            self.assertIn("applied cell_size=128", stderr)
            self.assertEqual(meta["qc_summary"]["profile_body_scale_drift"], 0.0)

    def test_cell_size_zero_rejected(self) -> None:
        """r11a, S18: --cell-size 0 is refused, not treated as the default."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = cli("process", "--input", str(self.sheet(root)), "--target", "asset", "--mode", "idle",
                         "--output-dir", str(root / "out"), "--cell-size", "0")
            self.assertEqual(result.returncode, 2)
            self.assertIn("--cell-size: must be a positive integer", result.stderr)
            self.assertFalse((root / "out").exists())

    def test_align_bottom_is_a_deprecated_alias_of_feet(self) -> None:
        """r11b, S18: --align bottom warns and behaves exactly like --align feet."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.sheet(root)
            common = ("--input", str(source), "--target", "asset", "--mode", "idle", "--scale-strategy", "preserve")
            bottom, stderr, out_bottom = run_process(*common, "--output-dir", str(root / "bottom"), "--align", "bottom")
            _feet, _stderr, out_feet = run_process(*common, "--output-dir", str(root / "feet"), "--align", "feet")
            self.assertIn("--align bottom is a deprecated alias of --align feet", stderr)
            self.assertEqual((bottom["align"], bottom["anchor_mode"]), ("feet", "feet"))
            self.assertEqual((out_bottom / "sheet-transparent.png").read_bytes(),
                             (out_feet / "sheet-transparent.png").read_bytes())

    def test_opaque_grid_rejected(self) -> None:
        """r15, S22: opaque art cannot be split into sprite cells; the error points to assemble_frames.py."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            opaque = Image.new("RGB", (128, 128), (90, 140, 90))
            opaque.paste((60, 40, 30), (24, 10, 40, 40))
            opaque.save(root / "opaque.png")
            result = cli("process", "--input", str(root / "opaque.png"), "--target", "asset", "--mode", "idle",
                         "--output-dir", str(root / "out"), "--background-mode", "opaque", "--strict-qc")
            self.assertEqual(result.returncode, 1)
            self.assertIn("error: opaque input cannot be split into sprite cells", result.stderr)
            self.assertIn("assemble_frames.py", result.stderr)
            self.assertFalse((root / "out").exists())

    def test_missing_prompt_file_fails(self) -> None:
        """r21, S27: a --prompt-file that does not exist is an error, not silently skipped."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.sheet(root)
            result = cli("process", "--input", "sheet.png", "--target", "asset", "--mode", "idle",
                         "--output-dir", "out", "--prompt-file", "missing-prompt.txt", cwd=root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("error: --prompt-file not found: missing-prompt.txt", result.stderr)
            self.assertFalse((root / "out").exists())
            self.assertEqual(leftovers(root), [])

    def test_provenance_recorded(self) -> None:
        """r21, S27: input hash, mode and conversion are recorded, the raw copy is byte-identical,
        and the metadata holds no absolute path."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rgb = Image.new("RGB", (128, 128), (255, 0, 255))
            rgb.paste((40, 90, 150), (20, 12, 44, 52))
            rgb.save(root / "sheet.png")
            (root / "prompt.txt").write_text("A small blue knight.", encoding="utf-8")
            result = cli("process", "--input", "sheet.png", "--target", "asset", "--mode", "idle", "--output-dir",
                         "out", "--prompt-file", "prompt.txt", cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(Path(summary["metadata"]), (root / "out" / "pipeline-meta.json").resolve())
            text = (root / "out" / "pipeline-meta.json").read_text(encoding="utf-8")
            meta = json.loads(text)
            source = (root / "sheet.png").read_bytes()
            self.assertEqual(meta["input"], "sheet.png")
            self.assertEqual(meta["provenance"]["input_sha256"], MODULE.forge_core.sha256_bytes(source))
            self.assertEqual(meta["provenance"]["source_mode"], "RGB")
            self.assertEqual(meta["provenance"]["raw_copy"], "raw-sheet.png")
            self.assertEqual((root / "out" / "raw-sheet.png").read_bytes(), source)
            self.assertEqual((root / "out" / "prompt-used.txt").read_text(encoding="utf-8"), "A small blue knight.")
            self.assertEqual(meta["prompt_file"], "prompt.txt")
            self.assertEqual(meta["qa"]["inputs"][0]["sha256"], meta["provenance"]["input_sha256"])
            for absolute in (str(root.resolve()), root.resolve().as_posix()):
                self.assertNotIn(absolute, text)

    def test_clamp_is_clean_cli_error(self) -> None:
        """Probe (generate2dsprite.py:1148, :1171-1173): a clamped frame under strict QC is an
        ``error:`` line with exit 1, never a traceback, and nothing is published."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            image.paste(SUBJECT, (20, 10, 44, 60))
            image.save(root / "tall.png")
            result = cli("process", "--input", str(root / "tall.png"), "--target", "asset", "--mode", "idle",
                         "--rows", "1", "--cols", "1", "--output-dir", str(root / "out"),
                         "--background-mode", "native_alpha", "--resampler", "nearest", "--pixel-scale", "1",
                         "--cell-size", "64", "--scale-strategy", "preserve", "--align", "feet", "--fit-scale", "1",
                         "--anchor-px", "32,30", "--strict-qc")
            self.assertEqual(result.returncode, 1)
            self.assertRegex(result.stderr, r"^error: QC failed: clamped frames: \[\[0, 0\]\]")
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((root / "out").exists())
            self.assertEqual(leftovers(root), [])


def soft_disk_sheet(size: int = 96, radius: float = 30.0, color=(40, 160, 210)) -> Image.Image:
    """An anti-aliased disk composited over #FF00FF, the way a generator draws soft edges."""
    supersample = 8
    grid = (np.arange(size * supersample) + 0.5) / supersample
    ys, xs = np.meshgrid(grid, grid, indexing="ij")
    inside = ((xs - size / 2) ** 2 + (ys - size / 2) ** 2 <= radius ** 2).astype(np.float64)
    coverage = inside.reshape(size, supersample, size, supersample).mean(axis=(1, 3))[..., None]
    rgb = coverage * np.array(color, np.float64) + (1 - coverage) * np.array([255.0, 0.0, 255.0])
    return Image.fromarray(np.floor(rgb + 0.5).astype(np.uint8), "RGB")


class CleanupTests(unittest.TestCase):
    """B01-T6: soft still key, direction order, bundle safety, neutral prompts, GIF timing."""

    def test_soft_key_no_magenta_soft_edges(self) -> None:
        """S24, report v2 P2-2: the default soft key keeps anti-aliased edges without a magenta fringe;
        --key-quality hard is the binary legacy key that leaves one."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            soft_disk_sheet().save(root / "disk.png")
            common = ("--input", str(root / "disk.png"), "--target", "asset", "--mode", "idle",
                      "--rows", "1", "--cols", "1")
            soft, _stderr, out = run_process(*common, "--output-dir", str(root / "soft"))
            with Image.open(out / "raw-sheet-clean.png") as image:
                keyed = np.asarray(image.convert("RGBA")).astype(int)
            hard, _stderr, out_hard = run_process(*common, "--output-dir", str(root / "hard"), "--key-quality", "hard")
            with Image.open(out_hard / "raw-sheet-clean.png") as image:
                binary = np.asarray(image.convert("RGBA")).astype(int)
        def edge_report(rgba: np.ndarray) -> tuple[int, int, int]:
            solid = rgba[..., 3] >= 128
            spill = np.minimum(rgba[..., 0], rgba[..., 2]) - rgba[..., 1]
            error = np.abs(rgba[..., :3] - np.array([40, 160, 210])).max(axis=-1)
            soft_edge = (rgba[..., 3] > 0) & (rgba[..., 3] < 255)
            return int(soft_edge.sum()), int(((rgba[..., 3] > 0) & (spill > 0)).sum()), int(error[solid].max())

        self.assertEqual((soft["matte"]["quality"], hard["matte"]["quality"]), ("soft", "hard"))
        soft_edges, tinted, worst = edge_report(keyed)
        self.assertGreater(soft_edges, 50)       # anti-aliased edges survive as partial alpha
        self.assertEqual(tinted, 0)              # no visible pixel leans to magenta
        self.assertLessEqual(worst, 4)           # edge colours are un-mixed back to the subject colour
        self.assertEqual(soft["matte"]["qa"]["opaque_key_px"], 0)
        self.assertEqual(soft["matte"]["qa"]["outer_ring_spill_fraction"], 0.0)
        soft_edges, tinted, worst = edge_report(binary)
        self.assertEqual(soft_edges, 0)          # the binary key has hard edges only ...
        self.assertGreater(tinted, 0)            # ... and keeps magenta-mixed rim pixels
        self.assertGreater(worst, 40)

    def test_soft_key_falls_back_for_green_screens(self) -> None:
        """auto picks the soft matte for a green key even with nearest; hard refuses non-magenta keys."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            green = Image.new("RGB", (64, 64), (0, 255, 0))
            green.paste((200, 40, 120), (16, 16, 48, 48))
            green.save(root / "green.png")
            common = ("--input", str(root / "green.png"), "--target", "asset", "--mode", "idle", "--rows", "1",
                      "--cols", "1", "--key", "green", "--resampler", "nearest", "--pixel-scale", "2")
            meta, _stderr, out = run_process(*common, "--output-dir", str(root / "auto"))
            self.assertEqual(meta["matte"]["quality"], "soft")
            with Image.open(out / "raw-sheet-clean.png") as image:
                alpha = np.asarray(image.convert("RGBA"))[..., 3]
            self.assertEqual(int(alpha[16:48, 16:48].min()), 255)
            self.assertEqual(int(alpha[:8].max()), 0)
            args = MODULE.build_parser().parse_args(["process", *common, "--output-dir", str(root / "hard"),
                                                     "--key-quality", "hard"])
            with self.assertRaisesRegex(ValueError, "legacy magenta keyer"):
                MODULE.cmd_process(args)

    def test_direction_order_strips(self) -> None:
        """DOC-08: --direction-order names the facing of each row; strips, GIFs and labels follow it."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_magenta_sheet(4, 4, 32, margin=6).save(root / "sheet.png")
            common = ("--input", str(root / "sheet.png"), "--target", "player", "--mode", "player_sheet")
            meta, _stderr, out = run_process(*common, "--output-dir", str(root / "custom"),
                                             "--direction-order", "down,up,left,right")
            self.assertEqual(meta["directions"], ["down", "up", "left", "right"])
            self.assertEqual(meta["frame_labels"][4:8], ["up-1", "up-2", "up-3", "up-4"])
            for direction in meta["directions"]:
                self.assertTrue((out / f"{direction}-strip.png").is_file())
                self.assertTrue((out / f"{direction}.gif").is_file())
            with Image.open(out / "up-strip.png") as strip, Image.open(out / "sheet-transparent.png") as sheet:
                self.assertEqual(strip.tobytes(), sheet.crop((0, 96, 384, 192)).tobytes())
            default, _stderr, _out = run_process(*common, "--output-dir", str(root / "default"))
            self.assertEqual(default["directions"], ["down", "left", "right", "up"])
            self.assertEqual(default["frame_labels"], MODULE.FRAME_LABELS["player_sheet"])
            run, _stderr, out_run = run_process(
                "--input", str(root / "sheet.png"), "--target", "asset", "--mode", "run", "--rows", "4",
                "--cols", "4", "--direction-order", "down-left,left,up-left,up", "--output-dir", str(root / "run"))
            self.assertEqual(run["frame_labels"][:2], ["run-down-left-1", "run-down-left-2"])
            self.assertTrue((out_run / "up-left.gif").is_file())
            for order, message in (("down,down,left,right", "distinct"), ("down,left,right", "4 rows"),
                                   ("down,left,right,north", "Unknown direction")):
                with self.subTest(order=order):
                    args = MODULE.build_parser().parse_args(["process", *common, "--direction-order", order,
                                                             "--output-dir", str(root / "bad")])
                    with self.assertRaisesRegex(ValueError, message):
                        MODULE.cmd_process(args)

    def test_bundle_refuses_existing(self) -> None:
        """r11c/d, S19: build-godot-bundle never replaces a file, stores contract paths relative to the
        bundle and refuses contracts whose frames do not resolve."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_magenta_sheet(1, 2, 64).save(root / "sheet.png")
            run_process("--input", str(root / "sheet.png"), "--target", "asset", "--mode", "idle", "--rows", "1",
                        "--cols", "2", "--align", "feet", "--godot-world-height", "0.7",
                        "--output-dir", str(root / "idle"))
            bundle = root / "bundle.json"
            command = ("build-godot-bundle", "--action", f"idle={root / 'idle' / 'godot-sprite3d.json'}",
                       "--default-action", "idle", "--output", str(bundle))
            first = cli(*command)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["actions"], ["idle"])
            written = bundle.read_bytes()
            self.assertEqual(json.loads(written)["actions"]["idle"]["contract"], "idle/godot-sprite3d.json")
            second = cli(*command)
            self.assertEqual(second.returncode, 1)
            self.assertIn("error: Refusing to overwrite existing bundle", second.stderr)
            self.assertEqual(bundle.read_bytes(), written)
            (root / "idle" / "idle-2.png").unlink()
            broken = cli("build-godot-bundle", "--action", f"idle={root / 'idle' / 'godot-sprite3d.json'}",
                         "--default-action", "idle", "--output", str(root / "other.json"))
            self.assertEqual(broken.returncode, 1)
            self.assertIn("do not resolve next to its contract: idle-2.png", broken.stderr)
            self.assertFalse((root / "other.json").exists())

    def test_default_prompt_has_no_franchise_names(self) -> None:
        """DOC-16, S26: default prompts name no franchise; --legacy-style keeps the old text."""
        franchises = ("digimon", "pokemon", "pokémon", "zelda", "final fantasy", "mario", "dragon quest",
                      "chrono trigger", "earthbound", "sonic")
        for target, modes in MODULE.TARGET_MODES.items():
            for mode in modes:
                with self.subTest(target=target, mode=mode):
                    text, _seed = MODULE.build_prompt(target, mode, "a wooden chest", "villager", seed=7)
                    self.assertFalse([name for name in franchises if name in text.lower()])
        chest, _seed = MODULE.build_prompt("asset", "single", "a wooden chest", seed=7)
        self.assertNotIn("monster creature", chest)
        legacy, _seed = MODULE.build_prompt("asset", "single", "a wooden chest", seed=7, legacy_style=True)
        self.assertIn("Digimon/Pokemon inspired", legacy)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "prompt.json"
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.cmd_build_prompt(MODULE.build_parser().parse_args([
                    "build-prompt", "--target", "creature", "--mode", "single", "--prompt", "fox",
                    "--legacy-style", "--write-json", str(path)]))
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["style"], "legacy")
        self.assertIn("Digimon/Pokemon", payload["generated_prompt"])

    def frames_sheet(self, root: Path, empty_last: bool = False) -> Path:
        sheet = Image.new("RGBA", (128, 32), (0, 0, 0, 0))
        for index in range(4 - int(empty_last)):
            sheet.paste((40 + 50 * index, 90, 150, 255), (index * 32 + 8, 6, index * 32 + 24, 28))
        path = root / "frames.png"
        sheet.save(path)
        return path

    def test_gif_decoded_duration_is_recorded_with_a_10ms_warning(self) -> None:
        """asf-improved GIF timing: GIF stores 10 ms units, so 125 ms plays as 120 ms; say so."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            common = ("--input", str(self.frames_sheet(root)), "--target", "asset", "--mode", "idle", "--rows",
                      "1", "--cols", "4", "--background-mode", "native_alpha", "--cell-size", "32")
            meta, stderr, _out = run_process(*common, "--output-dir", str(root / "a"), "--duration", "125")
            self.assertEqual(meta["gif_decoded_duration_ms"], {"animation.gif": [120, 120, 120, 120]})
            self.assertIn("plays as 120 ms", stderr)
            self.assertIn("10 ms units", meta["warnings"][0])
            meta, stderr, _out = run_process(*common, "--output-dir", str(root / "b"), "--duration", "120")
            self.assertEqual(meta["gif_decoded_duration_ms"], {"animation.gif": [120, 120, 120, 120]})
            self.assertNotIn("warnings", meta)
            self.assertEqual(stderr, "")

    def test_gif_with_a_blank_frame_decodes(self) -> None:
        """Pillow 12.3 wrote a second GIF header for a blank frame after a disposal-2 frame (corrupt
        file); every frame of the preview now decodes and the blank one stays blank."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta, _stderr, out = run_process(
                "--input", str(self.frames_sheet(root, empty_last=True)), "--target", "asset", "--mode", "idle",
                "--rows", "1", "--cols", "4", "--background-mode", "native_alpha", "--cell-size", "32",
                "--output-dir", str(root / "out"))
            self.assertEqual(meta["empty_frames"], [[0, 3]])
            with Image.open(out / "animation.gif") as gif:
                visible = []
                for index in range(gif.n_frames):
                    gif.seek(index)
                    visible.append(int((np.asarray(gif.convert("RGBA"))[..., 3] > 0).sum()))
        self.assertEqual(len(visible), 4)
        self.assertEqual(visible[3], 0)
        self.assertTrue(all(count > 0 for count in visible[:3]))


class CliConventionTests(unittest.TestCase):
    """Plan Appendix D: ASCII --help under cp1252/cp950, no overwrite, no partial output on QC failure."""

    def test_help_is_ascii_under_cp1252(self) -> None:
        assert_cli_help("generate2dsprite", "generate2dsprite")
        for command in ("process", "build-prompt", "build-godot-bundle", "list-options"):
            for encoding in ("cp1252", "cp950"):
                with self.subTest(command=command, encoding=encoding):
                    result = run_cli([SCRIPT_PATH, command, "--help"], encoding)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.isascii())

    def test_cli_refuses_existing_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_magenta_sheet(2, 2, 64).save(root / "sheet.png")
            (root / "out").mkdir()
            (root / "out" / "keep.txt").write_text("accepted", encoding="utf-8")
            result = cli("process", "--input", str(root / "sheet.png"), "--target", "asset", "--mode", "idle",
                         "--output-dir", str(root / "out"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("error: Refusing to overwrite existing output directory", result.stderr)
            self.assertEqual(sorted(path.name for path in (root / "out").iterdir()), ["keep.txt"])

    def test_cli_strict_qc_failure_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new("RGB", (128, 128), (255, 0, 255)).save(root / "blank.png")
            result = cli("process", "--input", str(root / "blank.png"), "--target", "asset", "--mode", "idle",
                         "--output-dir", str(root / "out"), "--strict-qc",
                         "--write-scale-profile", str(root / "profile.json"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("error: QC failed: empty frames", result.stderr)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["blank.png"])


if __name__ == "__main__":
    unittest.main()
