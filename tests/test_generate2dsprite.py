from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
