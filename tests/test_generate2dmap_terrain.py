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
    def options(self, root: Path, *extra: str):
        return MODULE.build_parser().parse_args([
            "--input", str(root / "atlas.png"), "--output-dir", str(root / "tiles"),
            "--rows", "1", "--cols", "1", "--terrain-row", "plain=0", *extra,
        ])

    def test_rejects_duplicate_row_assignment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (16, 16), "gray").save(root / "atlas.png")
            with self.assertRaisesRegex(ValueError, "exactly once"):
                MODULE.extract(self.options(root, "--terrain-row", "stone=0"))

    def test_rejects_zero_dimensions_before_division(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "positive"):
                MODULE.extract(self.options(Path(temporary), "--rows", "0"))

    def test_rectangular_cells_require_crop_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (32, 16), "gray").save(root / "atlas.png")
            with self.assertRaisesRegex(ValueError, "crop-square"):
                MODULE.extract(self.options(root))
            result = MODULE.extract(self.options(root, "--cell-shape", "crop-square"))
            self.assertEqual(result["grid"]["output_tile_size"], [16, 16])

    def test_native_transparency_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGBA", (16, 16), (255, 0, 255, 128)).save(root / "atlas.png")
            with self.assertRaisesRegex(ValueError, "opaque"):
                MODULE.extract(self.options(root))

    def test_strict_qc_failure_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (16, 16), "gray").save(root / "atlas.png")
            with self.assertRaisesRegex(ValueError, "QC failed"):
                MODULE.extract(self.options(root, "--strict-qc"))
            self.assertFalse((root / "tiles").exists())

    def test_nearest_keeps_palette_and_seamless_is_not_certified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Image.new("RGB", (2, 2), "black")
            source.putpixel((0, 0), (255, 255, 255))
            source.save(root / "atlas.png")
            result = MODULE.extract(self.options(root, "--tile-size", "8", "--resampler", "nearest", "--edge-policy", "seamless"))
            with Image.open(root / "tiles/plain-1.png") as output:
                self.assertEqual(len(output.getcolors()), 2)
            self.assertFalse(result["qc"]["seamless_verified"])
            self.assertEqual(result["runtime"]["engine_target"], "project-native")

    def test_nonfinite_emission_and_duplicate_emission_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (16, 16), "gray").save(root / "atlas.png")
            for values in [("--emission", "plain=nan"), ("--emission", "plain=1", "--emission", "plain=2")]:
                with self.subTest(values=values), self.assertRaises(ValueError):
                    MODULE.extract(self.options(root, *values))

    def test_manifest_cannot_overwrite_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (16, 16), "gray").save(root / "atlas.png")
            before = (root / "atlas.png").read_bytes()
            with self.assertRaisesRegex(ValueError, "aliases"):
                MODULE.extract(self.options(root, "--manifest", str(root / "atlas.png")))
            self.assertEqual(before, (root / "atlas.png").read_bytes())

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
            with Image.open(output_dir / "plain-1.png") as output:
                self.assertEqual(output.size, (64, 64))
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
