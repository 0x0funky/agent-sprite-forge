"""Behavioral regression tests for map composition (compose_layered_preview)."""
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


COMPOSE = load("compose_layered_preview")


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


if __name__ == "__main__":
    unittest.main()
