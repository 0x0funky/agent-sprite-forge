from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "generate2dmap"
    / "scripts"
    / "extract_terrain_tiles.py"
)
SPEC = importlib.util.spec_from_file_location("extract_terrain_tiles", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class TerrainTileBundleTests(unittest.TestCase):
    def test_extracts_rows_as_terrain_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Image.new("RGB", (96, 64), (30, 30, 30))
            draw = ImageDraw.Draw(source)
            for row in range(2):
                for col in range(3):
                    x0 = col * 32
                    y0 = row * 32
                    color = (80 + row * 70, 70 + col * 30, 40 + row * 50)
                    draw.rectangle((x0, y0, x0 + 31, y0 + 31), fill=color)
                    draw.line((x0, y0 + col * 4, x0 + 31, y0 + 31 - col * 3), fill=(240, 220, 180), width=2)
            input_path = root / "atlas.png"
            output_dir = root / "tiles"
            source.save(input_path)

            args = MODULE.build_parser().parse_args(
                [
                    "--input", str(input_path),
                    "--output-dir", str(output_dir),
                    "--rows", "2",
                    "--cols", "3",
                    "--terrain-row", "plain=0",
                    "--terrain-row", "forest=1",
                    "--tile-size", "64",
                    "--emission", "forest=0.2",
                ]
            )
            payload = MODULE.extract(args)

            self.assertEqual(payload["schema"], MODULE.SCHEMA)
            self.assertEqual(len(payload["terrains"]["plain"]["variants"]), 3)
            self.assertEqual(payload["terrains"]["forest"]["material"]["emission_energy"], 0.2)
            self.assertEqual(Image.open(output_dir / "plain-1.png").size, (64, 64))
            self.assertTrue((output_dir / "terrain-bundle.json").exists())

    def test_rejects_incomplete_row_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "atlas.png"
            Image.new("RGB", (96, 64), (50, 50, 50)).save(input_path)
            args = MODULE.build_parser().parse_args(
                [
                    "--input", str(input_path),
                    "--output-dir", str(root / "tiles"),
                    "--rows", "2",
                    "--cols", "3",
                    "--terrain-row", "plain=0",
                ]
            )
            with self.assertRaisesRegex(ValueError, "cover every row"):
                MODULE.extract(args)


if __name__ == "__main__":
    unittest.main()
