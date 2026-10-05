from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / "skills/generate2dsprite/scripts/assemble_frames.py"
SPEC = importlib.util.spec_from_file_location("assemble_frames", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AssembleFramesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"

    def png(self, name: str, image: Image.Image) -> Path:
        path = self.root / name
        image.save(path)
        return path

    def args(self, *arguments: str) -> object:
        return MODULE.build_parser().parse_args([*arguments, "--output-dir", str(self.output)])

    def assert_clean_failure(self) -> None:
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".output.stage-*")), [])

    def test_rectangular_odd_dimensions_preserve_native_pixels_and_provenance(self) -> None:
        first = Image.new("RGB", (7, 5), (255, 0, 255))
        second = first.copy()
        second.putpixel((6, 4), (3, 70, 240))
        paths = [self.png("one.png", first), self.png("two.png", second)]
        originals = [path.read_bytes() for path in paths]
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"prompt": "A quiet harbor", "references": ["master.png"]}), encoding="utf-8")
        result = MODULE.assemble(self.args("--input", *map(str, paths), "--manifest", str(manifest)))
        self.assertEqual(result["frame_size"], [7, 5])
        self.assertEqual(result["atlas"]["size"], [14, 5])
        self.assertEqual(result["sequence"], [0, 1])
        self.assertEqual(result["webp"]["total_duration_ms"], 500)
        self.assertFalse(result["validation"]["visual_approval"])
        self.assertEqual(result["provenance"]["user_manifest"]["prompt"], "A quiet harbor")
        self.assertEqual([path.read_bytes() for path in paths], originals)
        for index, expected in enumerate([first, second]):
            with Image.open(self.output / "frames" / f"frame-{index:02d}.png") as saved:
                self.assertEqual(saved.size, expected.size)
                np.testing.assert_array_equal(np.asarray(saved), np.asarray(expected))
        self.assertTrue(result["transitions"][-1]["wrap"])
        self.assertGreater(result["transitions"][-1]["premultiplied_rgb_mae"], 0)
        metadata = json.loads((self.output / "animation.json").read_text(encoding="utf-8"))
        self.assertNotIn(".stage-", json.dumps(metadata))
        self.assertEqual(metadata["sources"][0]["file_sha256"], MODULE.digest(originals[0]))

    def test_alpha_and_hidden_rgb_survive_png_and_atlas_without_a_mask(self) -> None:
        pixels = np.zeros((5, 9, 4), dtype=np.uint8)
        pixels[:] = (60, 70, 80, 0)  # Hidden RGB must still survive PNG and atlas.
        pixels[1:4, 2:7] = (255, 0, 255, 128)
        pixels[2, 4] = (20, 100, 240, 255)
        altered = pixels.copy()
        altered[2, 4] = (40, 80, 200, 128)
        paths = [self.png(f"alpha-{index}.png", Image.fromarray(array))
                 for index, array in enumerate([pixels, altered])]
        result = MODULE.assemble(self.args("--input", *map(str, paths)))
        self.assertTrue(result["webp"]["alpha_and_visible_rgb_exact"])
        with Image.open(self.output / "atlas.png") as atlas:
            np.testing.assert_array_equal(np.asarray(atlas.crop((0, 0, 9, 5))), pixels)
            np.testing.assert_array_equal(np.asarray(atlas.crop((9, 0, 18, 5))), altered)
        with Image.open(self.output / "frames/frame-00.png") as frame:
            np.testing.assert_array_equal(np.asarray(frame), pixels)

    def test_sheet_grid_slices_rectangles_without_alignment(self) -> None:
        sheet = Image.new("RGBA", (10, 3), (20, 50, 100, 100))
        sheet.paste((200, 0, 180, 220), (5, 0, 10, 3))
        path = self.png("sheet.png", sheet)
        result = MODULE.assemble(self.args("--sheet", str(path), "--rows", "1", "--cols", "2"))
        self.assertEqual(result["frame_size"], [5, 3])
        self.assertEqual(result["frames"][1]["crop_box"], [5, 0, 10, 3])
        with Image.open(self.output / "frames/frame-01.png") as frame:
            np.testing.assert_array_equal(np.asarray(frame), np.asarray(sheet.crop((5, 0, 10, 3))))

    def test_explicit_crop_boxes_support_nonuniform_gaps(self) -> None:
        sheet = Image.new("RGB", (11, 7), (250, 2, 3))
        sheet.paste((20, 50, 80), (1, 1, 5, 4))
        sheet.paste((150, 200, 80), (7, 3, 11, 6))
        path = self.png("gaps.png", sheet)
        boxes = self.root / "boxes.json"
        boxes.write_text("[[1,1,5,4],[7,3,11,6]]", encoding="utf-8")
        result = MODULE.assemble(self.args("--sheet", str(path), "--crop-boxes", str(boxes)))
        self.assertEqual(result["frame_size"], [4, 3])
        self.assertEqual(result["frames"][1]["crop_box"], [7, 3, 11, 6])

    def test_nondivisible_sheet_is_rejected_without_publication(self) -> None:
        path = self.png("unequal.png", Image.new("RGB", (9, 5)))
        with self.assertRaisesRegex(ValueError, "not divisible"):
            MODULE.assemble(self.args("--sheet", str(path), "--rows", "2", "--cols", "2"))
        self.assert_clean_failure()

    def test_mismatched_input_sizes_are_rejected(self) -> None:
        first = self.png("first.png", Image.new("RGB", (7, 5)))
        second = self.png("second.png", Image.new("RGB", (8, 5)))
        with self.assertRaisesRegex(ValueError, "dimensions differ"):
            MODULE.assemble(self.args("--input", str(first), str(second)))
        self.assert_clean_failure()

    def test_crop_bounds_and_sizes_are_validated(self) -> None:
        path = self.png("sheet.png", Image.new("RGB", (9, 5)))
        for boxes, message in [
            ("[[0,0,10,5]]", "outside"),
            ("[[0,0,4,5],[4,0,9,5]]", "dimensions differ"),
            ("[[0,0,0,5]]", "empty"),
            ("[[false,0,4,5]]", "integers"),
        ]:
            with self.subTest(boxes=boxes), self.assertRaisesRegex(ValueError, message):
                MODULE.assemble(self.args("--sheet", str(path), "--crop-boxes", boxes))
            self.assert_clean_failure()

    def test_duplicate_sequence_retains_decoded_total_and_each_hold(self) -> None:
        first = self.png("first.png", Image.new("RGB", (7, 5), (255, 20, 90)))
        second = self.png("second.png", Image.new("RGB", (7, 5), (0, 180, 20)))
        result = MODULE.assemble(self.args("--input", str(first), str(second),
                                           "--sequence", "0,0,1,1,0", "--duration", "125"))
        self.assertEqual(result["sequence"], [0, 0, 1, 1, 0])
        self.assertEqual(result["total_duration_ms"], 625)
        self.assertEqual(result["webp"]["total_duration_ms"], 625)
        self.assertEqual(sum(frame["duration_ms"] for frame in result["webp"]["decoded_frames"]), 625)
        self.assertLess(result["webp"]["decoded_frame_count"], 5)
        self.assertTrue(result["webp"]["timeline_verified"])

    def test_all_identical_frames_keep_timed_animation_including_alpha(self) -> None:
        path = self.png("same.png", Image.new("RGBA", (7, 5), (50, 90, 170, 100)))
        result = MODULE.assemble(self.args("--input", str(path), "--sequence", "0,0,0", "--duration", "137"))
        self.assertTrue(result["webp"]["timed_static_container"])
        self.assertEqual(result["webp"]["decoded_frame_count"], 1)
        self.assertEqual(result["webp"]["decoded_frames"][0]["duration_ms"], 411)
        self.assertEqual(result["webp"]["loop"], 0)

    def test_single_frame_is_a_timed_webp(self) -> None:
        path = self.png("still.png", Image.new("RGB", (3, 5), (0, 80, 180)))
        result = MODULE.assemble(self.args("--input", str(path)))
        self.assertEqual(result["webp"]["total_duration_ms"], 250)
        self.assertEqual(result["transitions"][0]["changed_visible_pixels"], 0)

    def test_existing_output_is_never_changed_even_when_empty(self) -> None:
        path = self.png("first.png", Image.new("RGB", (7, 5)))
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            MODULE.assemble(self.args("--input", str(path)))
        self.assertEqual(list(self.output.iterdir()), [])
        marker = self.output / "keep.txt"
        marker.write_bytes(b"must remain")
        with self.assertRaises(FileExistsError):
            MODULE.assemble(self.args("--input", str(path)))
        self.assertEqual(marker.read_bytes(), b"must remain")

    def test_encoder_failure_removes_stage_and_publishes_nothing(self) -> None:
        path = self.png("first.png", Image.new("RGB", (7, 5)))
        with mock.patch.object(MODULE, "encode_webp", side_effect=ValueError("injected decode failure")):
            with self.assertRaisesRegex(ValueError, "injected decode failure"):
                MODULE.assemble(self.args("--input", str(path)))
        self.assert_clean_failure()

    def test_competing_output_at_publication_is_preserved(self) -> None:
        path = self.png("first.png", Image.new("RGB", (7, 5)))
        publish = MODULE.publish_directory

        def competing_publish(stage: Path, final: Path) -> None:
            final.mkdir()
            (final / "someone-else.txt").write_bytes(b"keep")
            publish(stage, final)

        with mock.patch.object(MODULE, "publish_directory", side_effect=competing_publish):
            with self.assertRaises(FileExistsError):
                MODULE.assemble(self.args("--input", str(path)))
        self.assertEqual((self.output / "someone-else.txt").read_bytes(), b"keep")
        self.assertEqual(list(self.root.glob(".output.stage-*")), [])

    def test_duration_sequence_and_option_combinations_reject_without_output(self) -> None:
        path = self.png("first.png", Image.new("RGB", (7, 5)))
        for extra in [("--duration", "0"), ("--sequence", "-1"), ("--sequence", "0,2"),
                      ("--sequence", "0,"), ("--rows", "1", "--cols", "1")]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                MODULE.assemble(self.args("--input", str(path), *extra))
            self.assert_clean_failure()

    def test_static_regions_are_diagnostics_and_do_not_modify_frames(self) -> None:
        first = Image.new("RGB", (7, 5), (50, 80, 110))
        second = first.copy()
        second.putpixel((6, 4), (230, 10, 40))
        paths = [self.png("one.png", first), self.png("two.png", second)]
        result = MODULE.assemble(self.args("--input", *map(str, paths),
                                           "--static-regions", '{"wall":[0,0,3,3],"motion":[6,4,7,5]}'))
        self.assertEqual(result["static_regions"]["wall"]["against_reference"][1]["changed_visible_pixels"], 0)
        self.assertEqual(result["static_regions"]["motion"]["against_reference"][1]["changed_visible_pixels"], 1)
        self.assertFalse(result["static_regions"]["wall"]["registration_performed"])
        self.assertFalse(result["validation"]["visual_approval"])

    def test_decoder_validation_catches_wrong_timeline_and_pixels(self) -> None:
        frame = Image.new("RGB", (7, 5), (0, 80, 100))
        path = self.root / "single.webp"
        MODULE._timed_static_webp(path, frame, 250)
        with self.assertRaisesRegex(ValueError, "duration"):
            MODULE.validate_webp(path, [MODULE.pixel_array(frame)], [0, 0], 250)
        other = Image.new("RGB", (7, 5), (1, 80, 100))
        with self.assertRaisesRegex(ValueError, "mismatch"):
            MODULE.validate_webp(path, [MODULE.pixel_array(other)], [0], 250)


if __name__ == "__main__":
    unittest.main()
