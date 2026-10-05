from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from pathlib import Path

from PIL import Image


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
                        "--resampler", "nearest", "--cell-size", "16"]
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
                "--resampler", "nearest", "--cell-size", "16", "--rows", "2", "--cols", "4",
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
        image.paste(SUBJECT, (28, 6, 34, 18))

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
        profile = MODULE.build_scale_profile(self.make_metadata(), "ronin", 0.08)
        drift = MODULE.profile_scale_drift({"body_scale_mean": 0.22}, profile)
        self.assertAlmostEqual(drift, 0.10)

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
            "--target", "player", "--mode", "idle", "--rows", "1", "--cols", "1",
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
                MODULE.cmd_process(self.args(source, output, "--background-mode", "native_alpha", "--resampler", "nearest"))
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
        args = MODULE.build_parser().parse_args([
            "process", "--input", "raw.png", "--target", "asset", "--mode", "idle",
            "--output-dir", "out",
        ])
        self.assertEqual(args.background_mode, "chroma_key")
        self.assertEqual(args.resampler, "lanczos")
        self.assertEqual(args.despill_radius, 0)


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
        image = Image.new("RGBA", (8, 8), (255, 0, 255, 128))
        output = MODULE.center_single_sprite(
            image, 8, 100, 150, background_mode="native_alpha", resampler="nearest"
        )
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
        with mock.patch.object(MODULE, "prepare_background") as prepare:
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
                                  "--write-scale-profile", str(profile))
            source_bytes = args.input.read_bytes()
            with mock.patch.object(MODULE, "remove_bg_magenta", wraps=MODULE.remove_bg_magenta) as remove:
                MODULE.cmd_process(args)
            self.assertEqual(remove.call_count, 1)
            metadata = json.loads((args.output_dir / "pipeline-meta.json").read_text())
            contract = json.loads((args.output_dir / "godot-sprite3d.json").read_text())
            saved_profile = json.loads(profile.read_text())
            self.assertEqual(metadata["scale_profile_output"], str(profile.resolve()))
            self.assertEqual(metadata["godot_sprite3d_output"], str((args.output_dir / "godot-sprite3d.json").resolve()))
            self.assertNotIn(".staging-", json.dumps(metadata))
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
            self.assertEqual(metadata["godot_sprite3d_output"], str(godot.resolve()))
            self.assertEqual(metadata["scale_profile_output"], str(profile.resolve()))
            self.assertTrue(godot.is_file() and profile.is_file())
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
            original = MODULE._publish_file_no_replace
            def interrupted_publish(source, destination):
                if destination == profile:
                    profile.write_bytes(b"concurrent file")
                    raise FileExistsError("concurrent creation")
                original(source, destination)
            with mock.patch.object(MODULE, "_publish_file_no_replace", side_effect=interrupted_publish):
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
            with mock.patch.object(MODULE.os.path, "lexists", return_value=False):
                with self.assertRaises(FileExistsError):
                    MODULE._publish_directory_no_replace(stage, final)
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


if __name__ == "__main__":
    unittest.main()
