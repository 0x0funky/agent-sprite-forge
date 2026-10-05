"""Behavioral regression tests for platform strips (extract_platform_strip)."""
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


PLATFORM = load("extract_platform_strip")


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
