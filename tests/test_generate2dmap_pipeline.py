"""Behavioral regression tests for map composition, extraction and coverage."""
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


COMPOSE = load("compose_layered_preview")
PROPS = load("extract_prop_pack")
PARALLAX = load("validate_parallax")
PLATFORM = load("extract_platform_strip")


class CompositionTests(unittest.TestCase):
    def test_collects_all_layers_including_actor(self):
        entries = COMPOSE.load_props({"props": [{"id": "tree"}], "objects": [{"id": "chest"}],
                                     "actors": [{"id": "hero"}], "foreground": [{"id": "roof"}]})
        self.assertEqual([item["id"] for item in entries], ["tree", "chest", "hero", "roof"])
        self.assertEqual(entries[-1]["layer"], "foreground")

    def test_source_anchor_scales_with_art(self):
        self.assertEqual(COMPOSE.placement_xy({"x": 80, "y": 100, "anchorPx": [10, 30]}, 40, 80, (20, 40)), (60, 40))

    def test_invalid_anchor_fails(self):
        for prop in ({"anchor": "misspelled"}, {"anchorPx": [2, 50]}, {"anchorPx": [float("nan"), 0]}):
            with self.subTest(prop=prop), self.assertRaises(ValueError):
                COMPOSE.placement_xy(prop, 20, 40)

    def test_nearest_preserves_alpha_and_clipping_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Image.new("RGBA", (2, 2), (255, 0, 255, 128))
            source.putpixel((1, 1), (0, 0, 0, 0))
            source.save(root / "prop.png")
            canvas = Image.new("RGBA", (10, 10))
            report = COMPOSE.paste_prop(canvas, {"image": "prop.png", "w": 4, "h": 4, "x": -1, "y": 0, "anchor": "top-left"}, [root], "nearest")
            self.assertEqual(canvas.getpixel((0, 0)), (255, 0, 255, 128))
            self.assertTrue(report["clipped"])

    def test_rejects_invalid_opacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGBA", (2, 2)).save(root / "p.png")
            with self.assertRaises(ValueError):
                COMPOSE.paste_prop(Image.new("RGBA", (4, 4)), {"image": "p.png", "opacity": float("nan")}, [root])


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


class PlatformTests(unittest.TestCase):
    def fixture(self, root, hole=False):
        image = Image.new("RGBA", (24, 8))
        for x in range(24):
            for y in range(2, 6):
                image.putpixel((x, y), (110, 80, 60, 255))
        if hole:
            image.putpixel((12, 2), (0, 0, 0, 0))
        image.save(root / "strip.png")
        spec = {"surface_y_px": 2, "collision_depth_px": 4,
                "pieces": [{"id": name, "role": role, "source_box": [index * 8, 0, (index + 1) * 8, 8]}
                           for index, (name, role) in enumerate(zip(("left", "mid", "right"), PLATFORM.ROLES))]}
        (root / "strip.json").write_text(json.dumps(spec), encoding="utf-8")
        return PLATFORM.build_parser().parse_args([
            "--input", str(root / "strip.png"), "--spec", str(root / "strip.json"),
            "--output-dir", str(root / "out"), "--background-mode", "native_alpha", "--strict-qc"])

    def test_fixed_geometry_and_repeated_middle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = PLATFORM.extract(self.fixture(root))
            self.assertTrue(result["qc"]["passed"])
            self.assertFalse(result["processing"]["resized"])
            self.assertEqual(result["preview"]["size"], [40, 8])
            self.assertEqual(result["pieces"][0]["anchor_px"], [0, 2])

    def test_structural_hole_fails_without_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "coverage fails"):
                PLATFORM.extract(self.fixture(root, hole=True))
            self.assertFalse((root / "out").exists())

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.fixture(root)
            (root / "out").mkdir()
            with self.assertRaises(FileExistsError):
                PLATFORM.extract(args)


if __name__ == "__main__":
    unittest.main()
