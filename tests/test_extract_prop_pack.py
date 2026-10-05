"""Behavioral regression tests for prop-pack extraction (extract_prop_pack)."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/generate2dmap/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(f"map_test_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROPS = load("extract_prop_pack")


class PropExtractionTests(unittest.TestCase):
    def options(self, root, *extra):
        return PROPS.build_parser().parse_args([
            "--input", str(root / "props.png"), "--output-dir", str(root / "out"),
            "--rows", "1", "--cols", "1", "--labels", "purple-tree", "--min-component-area", "1", *extra])

    def fixture(self, root, edge=False):
        image = Image.new("RGBA", (16, 16))
        for x in range(0 if edge else 4, 10):
            for y in range(4, 12):
                image.putpixel((x, y), (255, 0, 255, 128))
        image.save(root / "props.png")

    def test_auto_preserves_native_purple_alpha(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            result = PROPS.extract(self.options(root))
            self.assertEqual(result["background_mode"], "native_alpha")
            with Image.open(root / "out/purple-tree/prop.png") as out:
                self.assertIn((255, 0, 255, 128), [value for _, value in out.getcolors()])

    def test_strict_edges_fail_before_images_publish_even_with_trim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, edge=True)
            with self.assertRaisesRegex(ValueError, "cell edge"):
                PROPS.extract(self.options(root, "--trim-border", "1", "--reject-edge-touch"))
            self.assertFalse((root / "out").exists())

    def test_duplicate_normalized_labels_fail(self):
        args = self.options(Path("."), "--cols", "2", "--labels", "Oak Tree,oak-tree")
        with self.assertRaisesRegex(ValueError, "unique"):
            PROPS.parse_labels(args, 2)

    def test_non_divisible_grid_fails(self):
        with self.assertRaisesRegex(ValueError, "exactly"):
            list(PROPS.iter_cells(Image.new("RGBA", (17, 16)), 1, 2))

    def test_component_threshold_applies_when_nothing_survives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            result = PROPS.extract(self.options(root, "--min-component-area", "500"))
            self.assertEqual(result["accepted"], [])
            self.assertEqual(result["rejected"][0]["status"], "empty")

    def test_all_mode_preserves_separate_parts_without_alpha_squaring(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = Image.new("RGBA", (12, 12))
            image.putpixel((3, 3), (200, 10, 200, 128))
            image.putpixel((8, 8), (200, 10, 200, 128))
            image.save(root / "props.png")
            result = PROPS.extract(self.options(root, "--component-mode", "all"))
            self.assertEqual(result["accepted"][0]["component_count"], 2)
            with Image.open(root / "out/purple-tree/prop.png") as out:
                self.assertEqual(out.getchannel("A").histogram()[128], 2)

    def test_manifest_paths_are_manifest_relative(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            manifest = root / "metadata/kit.json"
            result = PROPS.extract(self.options(root, "--manifest", str(manifest)))
            path = manifest.parent / result["accepted"][0]["image"]
            self.assertTrue(path.is_file())

    def test_inspected_boxes_recover_complete_tall_prop_without_resizing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = Image.new("RGBA", (30, 24))
            for x in range(3, 9):
                for y in range(3, 17):
                    image.putpixel((x, y), (255, 0, 255, 128))
            for x in range(18, 24):
                for y in range(18, 21):
                    image.putpixel((x, y), (60, 120, 30, 255))
            image.save(root / "props.png")
            (root / "boxes.json").write_text(json.dumps({"props": [
                {"label": "tree", "source_box": [0, 0, 12, 20]},
                {"label": "rock", "source_box": [14, 14, 28, 24]},
            ]}), encoding="utf-8")
            args = PROPS.build_parser().parse_args([
                "--input", str(root / "props.png"), "--boxes-file", str(root / "boxes.json"),
                "--output-dir", str(root / "out"), "--background-mode", "native_alpha",
                "--min-component-area", "1", "--component-padding", "0", "--reject-edge-touch"])
            result = PROPS.extract(args)
            self.assertEqual(result["layout_mode"], "explicit_boxes")
            self.assertEqual(result["source_size"], [30, 24])
            self.assertEqual(result["accepted"][0]["source_box"], [0, 0, 12, 20])
            self.assertEqual(result["accepted"][0]["grid"], None)
            with Image.open(root / "out/tree/prop.png") as extracted:
                self.assertEqual(extracted.size, (6, 14))
                self.assertEqual(extracted.getchannel("A").histogram()[128], 84)
                self.assertEqual(extracted.getpixel((0, 0)), (255, 0, 255, 128))

    def test_alpha_floor_is_explicit_and_preserves_source_and_visible_alpha(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with Image.open(root / "props.png") as opened:
                image = opened.copy()
            image.putpixel((0, 0), (255, 0, 255, 1))
            image.save(root / "props.png")
            original = (root / "props.png").read_bytes()
            with self.assertRaisesRegex(ValueError, "cell edge"):
                PROPS.extract(self.options(root, "--reject-edge-touch"))
            result = PROPS.extract(self.options(root, "--alpha-floor", "4", "--reject-edge-touch"))
            self.assertEqual(result["alpha_floor_pixels_removed"], 1)
            self.assertEqual((root / "props.png").read_bytes(), original)
            with Image.open(root / "out/purple-tree/prop.png") as out:
                self.assertEqual(out.getchannel("A").histogram()[128], 48)
                self.assertIn((255, 0, 255, 128), [value for _, value in out.getcolors()])

    def test_small_alpha_floor_does_not_hide_real_edge_clipping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, edge=True)
            with self.assertRaisesRegex(ValueError, "cell edge"):
                PROPS.extract(self.options(root, "--alpha-floor", "4", "--reject-edge-touch"))
            self.assertFalse((root / "out").exists())

    def test_crop_boxes_reject_overlap_and_out_of_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "boxes.json"
            for boxes in (
                [[0, 0, 10, 10], [8, 8, 16, 16]],
                [[0, 0, 10, 10], [12, 12, 17, 16]],
                [[0, 0, 10, 10], [12, 12, 15.5, 16]],
            ):
                path.write_text(json.dumps({"props": [
                    {"label": str(index), "source_box": box} for index, box in enumerate(boxes)
                ]}), encoding="utf-8")
                with self.subTest(boxes=boxes), self.assertRaises(ValueError):
                    PROPS.read_crop_boxes(path, (16, 16))

    def test_boxes_reject_grid_flags_instead_of_guessing(self):
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            PROPS.extract(self.options(Path("."), "--boxes-file", "boxes.json"))

    def test_crop_spec_cannot_be_overwritten_by_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            path = root / "boxes.json"
            path.write_text(json.dumps({"props": [{"label": "tree", "source_box": [0, 0, 16, 16]}]}), encoding="utf-8")
            original = path.read_bytes()
            args = PROPS.build_parser().parse_args([
                "--input", str(root / "props.png"), "--boxes-file", str(path),
                "--output-dir", str(root / "out"), "--manifest", str(path), "--min-component-area", "1"])
            with self.assertRaisesRegex(ValueError, "aliases"):
                PROPS.extract(args)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
