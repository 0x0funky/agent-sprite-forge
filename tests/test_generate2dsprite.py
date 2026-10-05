from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
