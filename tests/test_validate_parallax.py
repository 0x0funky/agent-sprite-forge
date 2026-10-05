"""Behavioral regression tests for parallax coverage (validate_parallax)."""
from __future__ import annotations

import importlib.util
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


PARALLAX = load("validate_parallax")


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


if __name__ == "__main__":
    unittest.main()
