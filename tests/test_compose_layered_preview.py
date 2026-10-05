"""Behavioral regression tests for map composition (compose_layered_preview)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from forge_testutils import load_script, run_cli, script_path


COMPOSE = load_script("generate2dmap", "compose_layered_preview")
SCRIPT = script_path("generate2dmap", "compose_layered_preview")


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


class ForkLayeredPreviewTests(unittest.TestCase):
    """The improved fork's five compose tests (B12-T1), bodies unchanged except module loading."""

    def test_all_render_arrays_reach_output_and_foreground_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new('RGBA', (8, 8), 'black').save(root / 'base.png')
            for name, color in [('tree', 'green'), ('actor', 'red'), ('roof', 'blue')]:
                Image.new('RGBA', (2, 2), color).save(root / f'{name}.png')
            data = {'props': [{'image': 'tree.png', 'x': 2, 'y': 2}],
                    'actors': [{'image': 'actor.png', 'x': 2, 'y': 2}],
                    'objects': [{'image': 'actor.png', 'x': 5, 'y': 5}],
                    'foreground': [{'image': 'roof.png', 'x': 2, 'y': 2, 'sortY': -20}]}
            (root / 'scene.json').write_text(json.dumps(data))
            run = run_cli([SCRIPT, '--base', str(root / 'base.png'),
                           '--placements', str(root / 'scene.json'), '--output', str(root / 'out.png'),
                           '--report', str(root / 'report.json')])
            self.assertEqual(run.returncode, 0, run.stderr)
            with Image.open(root / 'out.png') as image:
                self.assertEqual(image.getpixel((1, 1)), (0, 0, 255, 255))
                self.assertEqual(image.getpixel((4, 4)), (255, 0, 0, 255))
            self.assertEqual(len(json.loads((root / 'report.json').read_text())['pasted']), 4)

    def test_native_alpha_is_composited_once_and_nearest_preserves_palette(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = Image.new('RGBA', (2, 1))
            source.putdata([(200, 100, 50, 128), (0, 0, 255, 255)])
            source.save(root / 'prop.png')
            canvas = Image.new('RGBA', (4, 2), (0, 0, 0, 0))
            COMPOSE.paste_prop(canvas, {'image': 'prop.png', 'anchor': 'top-left', 'w': 4, 'h': 2},
                               [root], 'nearest')
            colors = lambda image: {image.getpixel((x, y)) for x in range(image.width) for y in range(image.height)}
            self.assertEqual(colors(canvas), colors(source))

    def test_source_contact_anchor_survives_scaling(self):
        self.assertEqual(COMPOSE.placement_xy({'x': 100, 'y': 80, 'anchorPx': [25, 40]},
                                              100, 100, (50, 50)), (50, 0))
        with self.assertRaises(ValueError):
            COMPOSE.placement_xy({'anchorPx': [51, 40]}, 100, 100, (50, 50))
        with self.assertRaises(ValueError):
            COMPOSE.placement_xy({'anchor': 'typo'}, 10, 10)

    def test_invalid_array_does_not_silently_drop_objects(self):
        with self.assertRaises(ValueError):
            COMPOSE.load_props({'props': [], 'foreground': {}})

    def test_report_sort_baseline_matches_sorting_when_y_is_omitted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new('RGBA', (10, 10), 'red').save(root / 'prop.png')
            prop = {'image': 'prop.png', 'anchor': 'center'}
            report = COMPOSE.paste_prop(Image.new('RGBA', (20, 20)), prop, [root])
            self.assertEqual(report['sortY'], COMPOSE.effective_sort_y(prop))
            self.assertEqual(report['sortY'], 0)


if __name__ == "__main__":
    unittest.main()
