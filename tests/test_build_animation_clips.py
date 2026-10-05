from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / "skills/generate2dsprite/scripts/build_animation_clips.py"
SPEC = importlib.util.spec_from_file_location("build_animation_clips", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AnimationClipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "bundle"
        self.manifest = self.root / "clips.json"

    def sprite(self, name: str, phase: int = 0, size: tuple[int, int] = (18, 22)) -> str:
        frame = Image.new("RGBA", size, (80, 90, 100, 0))
        frame.paste((50 + phase * 15, 100, 160, 128), (5, 4 + phase % 3, 12, 17 + phase % 3))
        frame.putpixel((8, 8), (255, 0, 255, 255))
        frame.save(self.root / name)
        return name

    def contract(self) -> dict:
        return {
            "schema": MODULE.SCHEMA,
            "frames": [self.sprite("run-1.png", 0), self.sprite("run-2.png", 1)],
            "anchor_px": [9, 20],
            "clips": {"run": {"frames": [0, 1], "duration_ms": 80, "loop": True}},
            "states": {"moving": "run"},
        }

    def build(self, contract: dict) -> dict:
        self.manifest.write_text(json.dumps(contract), encoding="utf-8")
        return MODULE.build(self.manifest, self.output)

    def assert_unpublished(self) -> None:
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".bundle.stage-*")), [])

    def test_nine_cells_export_eight_run_frames_and_separate_idle(self) -> None:
        names = [self.sprite(f"pose-{index}.png", index) for index in range(9)]
        source_bytes = [(self.root / name).read_bytes() for name in names]
        contract = {
            "frames": names, "anchor_px": [9, 20],
            "clips": {
                "run": {"frames": list(range(8)), "duration_ms": 80, "loop": True, "stride_world_units": 1.28},
                "idle": {"frames": [8], "duration_ms": 400, "loop": True},
            }, "states": {"moving": "run", "idle": "idle"},
        }
        result = self.build(contract)
        self.assertEqual(result["frame_size"], [18, 22])
        self.assertEqual(result["anchor_px"], [9, 20])
        self.assertEqual(result["clips"]["run"]["frames"], list(range(8)))
        self.assertEqual(result["clips"]["idle"]["frames"], [8])
        self.assertEqual(result["clips"]["run"]["duration_ms"], [80] * 8)
        self.assertEqual(result["clips"]["run"]["nominal_fps"], 12.5)
        self.assertEqual(result["clips"]["run"]["nominal_travel_speed_world_units_per_second"], 2)
        self.assertEqual(result["clips"]["idle"]["preview"]["total_duration_ms"], 400)
        self.assertEqual(result["states"]["idle"], "idle")
        for index, original in enumerate(source_bytes):
            self.assertEqual((self.output / "frames" / f"frame-{index:02d}.png").read_bytes(), original)
            self.assertEqual((self.root / names[index]).read_bytes(), original)
        self.assertFalse(result["diagnostics"]["visual_approval"])
        self.assertFalse(result["processing"]["per_frame_alignment"])
        self.assertEqual((self.output / "source-manifest.json").read_bytes(), self.manifest.read_bytes())
        with Image.open(self.output / "contact-sheet.png") as contact:
            self.assertGreater(contact.width, 18 * 3)
        self.assertNotIn(".stage-", (self.output / "animation-clips.json").read_text())

    def test_named_variable_duration_single_play_and_duplicate_merging(self) -> None:
        contract = self.contract()
        contract["frames"] = [{"name": "contact", "file": "run-1.png"}, {"name": "air", "file": "run-2.png"}]
        contract["clips"] = {"jump": {"frames": ["contact", "contact", "air"],
                                               "duration_ms": [60, 100, 240], "loop": False}}
        contract["states"] = {"jumping": "jump"}
        result = self.build(contract)
        clip = result["clips"]["jump"]
        self.assertEqual(clip["frames"], [0, 0, 1])
        self.assertEqual(clip["duration_ms"], [60, 100, 240])
        self.assertIsNone(clip["nominal_fps"])
        self.assertEqual(clip["preview"]["total_duration_ms"], 400)
        self.assertEqual(clip["preview"]["native_loop_count"], 1)
        self.assertEqual([frame["duration_ms"] for frame in clip["preview"]["decoded_frames"]], [160, 240])
        self.assertFalse(clip["last_to_first"]["applies_to_playback"])

    def test_single_frame_single_play_preview_keeps_duration_and_alpha(self) -> None:
        contract = self.contract()
        contract["clips"]["run"] = {"frames": [0], "duration_ms": 137, "loop": False}
        result = self.build(contract)
        preview = result["clips"]["run"]["preview"]
        self.assertEqual(preview["decoded_frame_count"], 1)
        self.assertEqual(preview["total_duration_ms"], 137)
        self.assertEqual(preview["native_loop_count"], 1)
        with Image.open(self.output / preview.get("file", result["clips"]["run"]["preview"]["file"])) as decoded:
            decoded.load()
            self.assertEqual(decoded.info["loop"], 1)
            self.assertEqual(decoded.info["duration"], 137)
            with Image.open(self.root / "run-1.png") as original:
                self.assertTrue(MODULE.FRAME_UTILS.same_visible_pixels(np.asarray(decoded.convert("RGBA")), np.asarray(original)))

    def test_duplicate_diagnostics_ignore_only_hidden_rgb(self) -> None:
        contract = self.contract()
        with Image.open(self.root / "run-1.png") as original:
            array = np.array(original)
        array[array[..., 3] == 0, :3] = (200, 210, 220)
        Image.fromarray(array).save(self.root / "run-2.png")
        result = self.build(contract)
        self.assertEqual(result["diagnostics"]["visible_pixel_duplicate_groups"], [[0, 1]])
        self.assertEqual(result["clips"]["run"]["last_to_first"]["changed_visible_pixels"], 0)
        self.assertNotEqual(result["frames"][0]["rgba_pixel_sha256"], result["frames"][1]["rgba_pixel_sha256"])

    def test_airborne_bottom_changes_do_not_change_common_anchor_or_canvas(self) -> None:
        contract = self.contract()
        contract["anchor_px"] = [9.5, 22]
        result = self.build(contract)
        self.assertEqual(result["anchor_px"], [9.5, 22])
        for record in result["frames"]:
            with Image.open(self.output / record["file"]) as frame:
                self.assertEqual(frame.size, (18, 22))
        self.assertEqual(result["frames"][0]["visible_pixel_count"], result["frames"][1]["visible_pixel_count"])

    def test_invalid_duration_reference_state_and_stride_reject_before_publication(self) -> None:
        cases = [
            ("duration_ms", 0, "durations"), ("duration_ms", [80], "one integer per frame"),
            ("duration_ms", [80, False], "durations"), ("frames", [0, 2], "out-of-range"),
            ("frames", ["missing"], "unknown frame"), ("frames", [True], "integer indices"),
            ("loop", "yes", "true or false"), ("stride_world_units", 0, "positive finite"),
        ]
        for key, value, message in cases:
            with self.subTest(key=key, value=value):
                contract = self.contract()
                contract["clips"]["run"][key] = value
                with self.assertRaisesRegex(ValueError, message):
                    self.build(contract)
                self.assert_unpublished()
        contract = self.contract()
        contract["states"] = {"idle": "missing"}
        with self.assertRaisesRegex(ValueError, "existing clip"):
            self.build(contract)
        self.assert_unpublished()

    def test_bad_shared_anchors_and_per_frame_anchor_are_rejected(self) -> None:
        for anchor in [[19, 20], [-1, 20], [9, 23], [True, 20], [9, float("nan")], [9]]:
            with self.subTest(anchor=anchor):
                contract = self.contract()
                contract["anchor_px"] = anchor
                with self.assertRaises(ValueError):
                    self.build(contract)
                self.assert_unpublished()
        contract = self.contract()
        contract["frames"][0] = {"name": "first", "file": "run-1.png", "anchor_px": [9, 19]}
        with self.assertRaisesRegex(ValueError, "Per-frame anchors"):
            self.build(contract)
        self.assert_unpublished()

    def test_mismatched_size_rejected(self) -> None:
        contract = self.contract()
        self.sprite("run-2.png", 1, (19, 22))
        with self.assertRaisesRegex(ValueError, "same native canvas"):
            self.build(contract)
        self.assert_unpublished()

    def test_opaque_and_empty_inputs_are_not_credible_sprite_alpha(self) -> None:
        for mode, color in [("RGB", (20, 40, 60)), ("RGBA", (20, 40, 60, 255)), ("RGBA", (20, 40, 60, 0))]:
            with self.subTest(mode=mode, color=color):
                contract = self.contract()
                Image.new(mode, (18, 22), color).save(self.root / "run-1.png")
                with self.assertRaisesRegex(ValueError, "transparent and visible"):
                    self.build(contract)
                self.assert_unpublished()

    def test_duplicate_names_missing_files_and_unknown_schema_are_rejected(self) -> None:
        contract = self.contract()
        contract["frames"] = ["run-1.png", "run-1.png"]
        with self.assertRaisesRegex(ValueError, "unique"):
            self.build(contract)
        self.assert_unpublished()
        contract = self.contract()
        contract["frames"][1] = "missing.png"
        with self.assertRaises(FileNotFoundError):
            self.build(contract)
        self.assert_unpublished()
        contract = self.contract()
        contract["schema"] = "unknown"
        with self.assertRaisesRegex(ValueError, "schema"):
            self.build(contract)
        self.assert_unpublished()

    def test_failure_late_in_preview_leaves_no_output(self) -> None:
        contract = self.contract()
        with mock.patch.object(MODULE, "make_contact_sheet", side_effect=ValueError("preview failure")):
            with self.assertRaisesRegex(ValueError, "preview failure"):
                self.build(contract)
        self.assert_unpublished()

    def test_existing_and_competing_output_are_preserved(self) -> None:
        contract = self.contract()
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_bytes(b"original")
        with self.assertRaisesRegex(ValueError, "Refusing existing"):
            self.build(contract)
        self.assertEqual(marker.read_bytes(), b"original")

    def test_competing_destination_at_publish_is_not_replaced(self) -> None:
        contract = self.contract()
        publish = MODULE.FRAME_UTILS.publish_directory

        def conflict(stage: Path, final: Path) -> None:
            final.mkdir()
            (final / "winner.txt").write_bytes(b"another run")
            publish(stage, final)

        with mock.patch.object(MODULE.FRAME_UTILS, "publish_directory", side_effect=conflict):
            with self.assertRaises(FileExistsError):
                self.build(contract)
        self.assertEqual((self.output / "winner.txt").read_bytes(), b"another run")
        self.assertEqual(list(self.root.glob(".bundle.stage-*")), [])


if __name__ == "__main__":
    unittest.main()
