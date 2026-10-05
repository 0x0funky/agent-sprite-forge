"""Terrain tiles (generate2dmap/scripts/extract_terrain_tiles.py).

TerrainTileBundleTests are the cfed170 regression tests (one adapted: runtime fields are omitted
unless given, plan Appendix H). The other classes cover plan B11-T2: runtime defaults (repro_11,
DOC-14, MAP-16), border frames (repro_12, MAP-10), iso-diamond, hex and overlay tiles (repro_13,
MAP-11), wrap-aware resizing and seam checks (repro_10/10b, MAP-15, MAP-14), Wang masks, CJK
names (MAP-23), grid rounding (MAP-04), staged publication (DOC-10), the proposed contract and
the CLI conventions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw

from forge_testutils import assert_cli_help, load_script, run_cli, script_path
from test_extract_platform_strip import proposed_map_errors  # applies edgeSeam and platform_strip_v2 too

MODULE = load_script("generate2dmap", "extract_terrain_tiles")
SCRIPT = script_path("generate2dmap", "extract_terrain_tiles")


# --------------------------------------------------------------------------- proposed contract (handoff section 5)

_NUMBER_01 = {"type": "number", "minimum": 0, "maximum": 1}
TERRAIN_TILE_BUNDLE_V2 = {
    "description": "terrain-bundle.json from extract_terrain_tiles.py. Each atlas row is one terrain: a fill or a Wang "
                   "corner transition row (variants carry wang [TL, TR, BL, BR] material indices into materials). "
                   "Tiles are square, rect, iso-diamond or hex, base or overlay; tile.footprint is the polygon in "
                   "tile pixels. runtime and material numbers appear only when given, or with "
                   "--emit-runtime-defaults (listed in runtime_defaults_applied); v1 bundles "
                   "(generate2dmap.terrain_tile_bundle.v1) always wrote them. seamless_verified stays false: image "
                   "tiles have no exact seam proof; the seamless policy records wrap and Wang seams (edgeSeam).",
    "type": "object",
    "required": ["schema", "source", "grid", "tile", "terrains", "processing", "qc", "qa"],
    "properties": {
        "schema": {"const": "generate2dmap.terrain_tile_bundle.v2"},
        "source": {"allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}], "required": ["size", "mode"],
                   "properties": {"size": {"$ref": "common.schema.json#/$defs/size2"},
                                  "mode": {"type": "string", "minLength": 1},
                                  "bit_depth": {"type": "integer", "minimum": 1},
                                  "conversion": {"type": "string"}}},
        "prompt": {"$ref": "common.schema.json#/$defs/fileRef"},
        "grid": {"type": "object", "required": ["rows", "cols", "rounding", "source_cell_size", "output_tile_size"],
                 "properties": {"rows": {"type": "integer", "minimum": 1}, "cols": {"type": "integer", "minimum": 1},
                                "rounding": {"enum": ["exact", "nearest"]},
                                "source_cell_size": {"$ref": "common.schema.json#/$defs/size2"},
                                "source_cell_size_max": {"$ref": "common.schema.json#/$defs/size2"},
                                "output_tile_size": {"$ref": "common.schema.json#/$defs/size2"}}},
        "tile": {"type": "object", "required": ["shape", "layer", "footprint"],
                 "properties": {"shape": {"enum": ["square", "rect", "iso-diamond", "hex-pointy", "hex-flat"]},
                                "layer": {"enum": ["base", "overlay"]},
                                "footprint": {"$ref": "common.schema.json#/$defs/polygon"}}},
        "terrains": {"type": "object", "minProperties": 1,
                     "propertyNames": {"pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"},
                     "additionalProperties": {
                         "type": "object",
                         "required": ["display_name", "row", "kind", "variants", "variant_difference_min"],
                         "properties": {
                             "display_name": {"type": "string", "minLength": 1},
                             "row": {"type": "integer", "minimum": 0},
                             "kind": {"enum": ["fill", "wang_corner"]},
                             "materials": {"type": "array", "minItems": 2, "maxItems": 9,
                                           "items": {"type": "string", "minLength": 1}},
                             "variant_difference_min": _NUMBER_01,
                             "material": {"type": "object",
                                          "properties": {"roughness": _NUMBER_01,
                                                         "emission_energy": {"type": "number", "minimum": 0}}},
                             "wang_coverage": {"type": "object", "required": ["masks", "of", "complete"]},
                             "wang_seams": {"type": "object", "required": ["legal_joins", "failed"]},
                             "cross_variant_seams": {"type": "object"},
                             "variants": {"type": "array", "minItems": 1, "items": {
                                 "type": "object",
                                 "required": ["path", "sha256", "source_cell", "source_box", "resized",
                                              "mean_luminance", "contrast"],
                                 "properties": {
                                     "path": {"$ref": "common.schema.json#/$defs/relPath"},
                                     "sha256": {"$ref": "common.schema.json#/$defs/sha256"},
                                     "source_cell": {"type": "array", "items": {"type": "integer", "minimum": 0},
                                                     "minItems": 2, "maxItems": 2},
                                     "source_box": {"$ref": "common.schema.json#/$defs/box"},
                                     "crop_box": {"$ref": "common.schema.json#/$defs/box"},
                                     "resized": {"type": "boolean"},
                                     "mean_luminance": _NUMBER_01, "contrast": _NUMBER_01,
                                     "visible_fraction": _NUMBER_01,
                                     "wang": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 8},
                                              "minItems": 4, "maxItems": 4},
                                     "variant": {"type": "integer", "minimum": 0},
                                     "border": {"type": "object", "required": ["checked", "frame", "frames"]},
                                     "shape": {"type": "object",
                                               "required": ["footprint_px", "coverage", "spill_px", "spill_fraction"]},
                                     "shape_fill": {"type": "object"},
                                     "wrap": {"type": "object", "required": ["x", "y"],
                                              "properties": {"x": {"$ref": "#/$defs/edgeSeam"},
                                                             "y": {"$ref": "#/$defs/edgeSeam"}}}}}}},
                         "if": {"properties": {"kind": {"const": "wang_corner"}}, "required": ["kind"]},
                         "then": {"required": ["materials"],
                                  "properties": {"variants": {"items": {"required": ["wang"]}}}}}},
        "runtime": {"type": "object",
                    "properties": {"engine_target": {"type": "string", "minLength": 1},
                                   "world_size": {"type": "number", "exclusiveMinimum": 0},
                                   "surface_y": {"type": "number"},
                                   "edge_policy": {"enum": ["isolated", "seamless"]}}},
        "runtime_defaults_applied": {"type": "array", "items": {"type": "string"}},
        "processing": {"type": "object", "required": ["background_mode", "resampler", "edge_policy", "grid_rounding"],
                       "properties": {"background_mode": {"enum": ["opaque", "native_alpha", "chroma_key",
                                                                   "shape_fill"]},
                                      "resampler": {"enum": ["nearest", "lanczos"]},
                                      "edge_policy": {"enum": ["isolated", "seamless"]},
                                      "grid_rounding": {"enum": ["exact", "nearest"]},
                                      "wrap_aware_resize": {"type": "boolean"}}},
        "qc": {"type": "object", "required": ["warnings", "passed", "seamless_verified"],
               "properties": {"warnings": {"type": "array", "items": {"type": "string"}},
                              "passed": {"type": "boolean"}, "seamless_verified": {"const": False}}},
        "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
    },
}
PROPOSED_TERRAIN_DEFS = {"terrain_tile_bundle_v2": TERRAIN_TILE_BUNDLE_V2}  # edgeSeam: see the platform tests


def terrain_errors(instance) -> list[str]:
    return proposed_map_errors(instance, "terrain_tile_bundle_v2", PROPOSED_TERRAIN_DEFS)


# --------------------------------------------------------------------------- fixtures

def periodic_noise(size=256, seed=7):
    """repro_10: random-phase noise, periodic by construction (opaque RGB)."""
    rng = np.random.default_rng(seed)
    spectrum = np.zeros((size, size), complex)
    fy, fx = np.meshgrid(np.fft.fftfreq(size) * size, np.fft.fftfreq(size) * size, indexing="ij")
    radius = np.hypot(fx, fy)
    band = (radius > 0) & (radius < 60)
    spectrum[band] = (rng.normal(size=band.sum()) + 1j * rng.normal(size=band.sum())) / radius[band] ** 0.9
    field = np.real(np.fft.ifft2(spectrum))
    field = (field - field.min()) / (field.max() - field.min())
    return np.stack([40 + 120 * field, 90 + 110 * field, 30 + 60 * field], -1).astype(np.uint8)


def periodic_bricks(size=256):
    """repro_10b: hard-edged bricks whose mortar crosses the top and left edges; period 256."""
    y, x = np.mgrid[0:size, 0:size]
    shift = (y // 32 % 2) * 32
    mortar = ((y % 32) < 3) | (((x + shift) % 64) < 3)
    return np.where(mortar[..., None], np.array([200, 190, 170]), np.array([150, 60, 40])).astype(np.uint8)


def wrap_aware_reference(tile, size):
    """repro_10's reference: Lanczos on a 3x3 repeat, centre copy."""
    big = Image.fromarray(np.tile(tile, (3, 3, 1))).resize((size * 3, size * 3), Image.Resampling.LANCZOS)
    return np.asarray(big)[size:2 * size, size:2 * size]


def gutter_atlas():
    """repro_12: two rows of noise cells with 2 px light grid lines drawn over every cell boundary."""
    rng = np.random.default_rng(3)
    atlas = Image.new("RGB", (192, 128))
    for row in range(2):
        for col in range(3):
            base = np.array([[70, 120, 50], [120, 110, 100]][row])
            cell = np.clip(base + rng.normal(0, 18, (64, 64, 3)), 0, 255).astype(np.uint8)
            atlas.paste(Image.fromarray(cell), (col * 64, row * 64))
    draw = ImageDraw.Draw(atlas)
    for i in range(4):
        draw.rectangle((i * 64 - 2, 0, i * 64 + 1, 127), fill=(245, 245, 245))
    for j in range(3):
        draw.rectangle((0, j * 64 - 2, 191, j * 64 + 1), fill=(245, 245, 245))
    return atlas


def textured_diamonds(alpha: bool):
    """repro_13 with texture: two 128x64 iso diamonds drawn with Pillow, on black or on transparency."""
    rng = np.random.default_rng(13)
    mode, fill = ("RGBA", (0, 0, 0, 0)) if alpha else ("RGB", (0, 0, 0))
    atlas = Image.new(mode, (256, 64), fill)
    mask = Image.new("L", (256, 64), 0)
    draw = ImageDraw.Draw(mask)
    for col in range(2):
        x0 = col * 128
        draw.polygon([(x0 + 64, 0), (x0 + 127, 32), (x0 + 64, 63), (x0, 32)], fill=255)
    texture = np.clip(np.array([80, 140, 60]) + rng.normal(0, 20, (64, 256, 3)), 30, 255).astype(np.uint8)
    texture[:, 128:] = np.clip(texture[:, 128:].astype(int) + np.array([30, -20, 10]), 30, 255)
    atlas.paste(Image.fromarray(texture).convert(mode), (0, 0), mask)
    return atlas, np.asarray(mask) > 0


def wang_tile(corners, size=16, seed=1):
    """proto_wang_tileset.py: a 2-material corner tile; bilinear corner field plus wrap-periodic noise."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size] + 0.5
    noise = np.zeros((size, size))
    for _ in range(6):
        kx, ky = rng.integers(-3, 4, 2)
        if kx or ky:
            noise += rng.uniform(0.3, 1.0) * np.sin(2 * np.pi * (kx * x + ky * y) / size + rng.uniform(0, 2 * np.pi))
    noise /= np.abs(noise).max()
    v, u = (np.mgrid[0:size, 0:size] + 0.5) / size
    nw, ne, sw, se = corners
    field = nw * (1 - u) * (1 - v) + ne * u * (1 - v) + sw * (1 - u) * v + se * u * v + 0.22 * noise
    water = np.array([44, 98, 168]) + (noise[..., None] * 18)
    grass = np.array([78, 150, 64]) + (noise[..., None] * 18)
    tile = np.where((field > 0.5)[..., None], grass, water)
    return np.clip(tile, 0, 255).astype(np.uint8)


class TerrainCase(unittest.TestCase):
    """A temporary project folder; ``run_extract`` parses CLI-style arguments and calls extract()."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def save(self, image, name="atlas.png"):
        image = image if isinstance(image, Image.Image) else Image.fromarray(np.asarray(image, np.uint8))
        image.save(self.root / name)
        return self.root / name

    def args(self, *argv, atlas="atlas.png", out="tiles"):
        return MODULE.build_parser().parse_args(
            ["--input", str(self.root / atlas), "--output-dir", str(self.root / out), *argv])

    def run_extract(self, *argv, **kwargs):
        return MODULE.extract(self.args(*argv, **kwargs))

    def tile(self, name, out="tiles"):
        with Image.open(self.root / out / name) as image:
            return np.asarray(image.convert("RGBA")).copy()


# --------------------------------------------------------------------------- cfed170 regression tests

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
        """Adapted for B11 (plan Appendix H): runtime numbers are omitted unless given."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Image.new("RGB", (2, 2), "black")
            source.putpixel((0, 0), (255, 255, 255))
            source.save(root / "atlas.png")
            result = MODULE.extract(self.options(root, "--tile-size", "8", "--resampler", "nearest", "--edge-policy", "seamless"))
            with Image.open(root / "tiles/plain-1.png") as output:
                self.assertEqual(len(output.getcolors()), 2)
            self.assertFalse(result["qc"]["seamless_verified"])
            self.assertNotIn("runtime", result)
            self.assertEqual(result["processing"]["edge_policy"], "seamless")

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


# --------------------------------------------------------------------------- repro_11: no hidden runtime defaults

class RuntimeDefaultsTests(TerrainCase):
    def setUp(self):
        super().setUp()
        image = Image.new("RGB", (32, 16), (70, 110, 50))
        draw = ImageDraw.Draw(image)
        draw.line((0, 0, 15, 15), fill=(200, 200, 120))
        draw.line((16, 15, 31, 0), fill=(30, 60, 20))
        self.save(image)
        self.base = ["--rows", "1", "--cols", "2", "--terrain-row", "grass=0", "--resampler", "nearest"]

    def test_repro_11_no_runtime_or_material_numbers_unless_given(self):
        """repro_11 (DOC-14, MAP-16): 0.94 / 0.011 / 0.88 are not written into a plain 2D bundle."""
        result = self.run_extract(*self.base)
        self.assertNotIn("runtime", result)
        self.assertNotIn("runtime_defaults_applied", result)
        self.assertNotIn("material", result["terrains"]["grass"])
        text = (self.root / "tiles/terrain-bundle.json").read_text(encoding="utf-8")
        for number in ("0.94", "0.011", "0.88", "project-native"):
            self.assertNotIn(number, text)

    def test_given_values_only(self):
        result = self.run_extract(*self.base, "--engine-target", "godot4", "--surface-y", "0.02", "--roughness", "0.5",
                                  "--emission", "grass=1.5")
        self.assertEqual(result["runtime"], {"engine_target": "godot4", "surface_y": 0.02})
        self.assertEqual(result["terrains"]["grass"]["material"], {"roughness": 0.5, "emission_energy": 1.5})
        self.assertNotIn("runtime_defaults_applied", result)

    def test_emit_runtime_defaults_restores_v1_values_and_says_so(self):
        result = self.run_extract(*self.base, "--emit-runtime-defaults", "--runtime-world-size", "2")
        self.assertEqual(result["runtime"], {"world_size": 2.0, "engine_target": "project-native", "surface_y": 0.011,
                                             "edge_policy": "isolated"})
        self.assertEqual(result["terrains"]["grass"]["material"], {"roughness": 0.88, "emission_energy": 0.0})
        self.assertEqual(result["runtime_defaults_applied"],
                         ["runtime.engine_target", "runtime.surface_y", "material.roughness",
                          "material.emission_energy"])

    def test_runtime_values_are_validated(self):
        for extra in (("--runtime-world-size", "0"), ("--roughness", "1.5"), ("--surface-y", "inf")):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.run_extract(*self.base, *extra)


# --------------------------------------------------------------------------- repro_12: drawn frames and gutters

class BorderCheckTests(TerrainCase):
    rows = ["--rows", "2", "--cols", "3", "--terrain-row", "grass=0", "--terrain-row", "stone=1",
            "--resampler", "nearest"]

    def test_repro_12_gutter_atlas_fails_strict_qc_and_publishes_nothing(self):
        """repro_12 (MAP-10): drawn grid lines framing every cell no longer pass --strict-qc."""
        self.save(gutter_atlas())
        with self.assertRaisesRegex(ValueError, "border frame on its left/right edges"):
            self.run_extract(*self.rows, "--strict-qc")
        self.assertFalse((self.root / "tiles").exists())
        result = self.run_extract(*self.rows)
        self.assertFalse(result["qc"]["passed"])
        self.assertEqual(sum("border frame" in warning for warning in result["qc"]["warnings"]), 6)
        for terrain in result["terrains"].values():
            for variant in terrain["variants"]:
                self.assertTrue(variant["border"]["frame"])
                self.assertEqual({frame["axis"] for frame in variant["border"]["frames"]}, {"x", "y"})
                self.assertGreater(variant["border"]["max_delta"], 0.3)
        self.assertEqual({c["id"]: c["status"] for c in result["qa"]["checks"]}["border_frame"], "fail")

    def test_same_cells_without_grid_lines_pass(self):
        rng = np.random.default_rng(3)
        atlas = np.zeros((128, 192, 3), np.uint8)
        for row, base in enumerate(([70, 120, 50], [120, 110, 100])):
            atlas[row * 64:(row + 1) * 64] = np.clip(np.array(base) + rng.normal(0, 18, (64, 192, 3)), 0, 255)
        self.save(atlas)
        result = self.run_extract(*self.rows, "--strict-qc")
        self.assertTrue(result["qc"]["passed"])
        self.assertFalse(any(v["border"]["frame"] for t in result["terrains"].values() for v in t["variants"]))

    def test_dark_one_pixel_outline_is_found(self):
        tile = np.clip(np.array([90, 130, 70]) + np.random.default_rng(1).normal(0, 12, (32, 32, 3)), 0, 255)
        tile[[0, -1], :] = (20, 20, 20)
        tile[:, [0, -1]] = (20, 20, 20)
        self.save(tile.astype(np.uint8))
        result = self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "moss=0")
        frames = result["terrains"]["moss"]["variants"][0]["border"]["frames"]
        self.assertIn({"axis": "x", "depth_px": 1, "tone": "darker"}, [
            {key: frame[key] for key in ("axis", "depth_px", "tone")} for frame in frames])

    def test_art_crossing_one_edge_and_wrapping_texture_are_not_frames(self):
        """repro_10b bricks: mortar runs along the top edge only; it wraps onto brick, not onto another line."""
        self.save(periodic_bricks())
        result = self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "brick=0", "--tile-size", "48")
        self.assertFalse(result["terrains"]["brick"]["variants"][0]["border"]["frame"])
        self.save(periodic_noise(), "noise.png")
        result = self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "grass=0", atlas="noise.png",
                                  out="noise")
        self.assertFalse(result["terrains"]["grass"]["variants"][0]["border"]["frame"])

    def test_threshold_one_disables_the_check(self):
        self.save(gutter_atlas())
        result = self.run_extract(*self.rows, "--max-border-delta", "1", "--strict-qc")
        self.assertTrue(result["qc"]["passed"])

    def test_tiny_tiles_are_not_judged(self):
        self.save(np.random.default_rng(2).integers(0, 255, (6, 6, 3), np.uint8))
        result = self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "dots=0")
        self.assertFalse(result["terrains"]["dots"]["variants"][0]["border"]["checked"])
        self.assertEqual({c["id"]: c["status"] for c in result["qa"]["checks"]}["border_frame"], "skipped")


# --------------------------------------------------------------------------- repro_13: iso-diamond, hex and overlays

class ShapeTests(TerrainCase):
    iso = ["--rows", "1", "--cols", "2", "--terrain-row", "grass=0", "--shape", "iso-diamond"]

    def test_repro_13_opaque_atlas_with_flat_corners_keeps_every_diamond_pixel(self):
        """repro_13 (MAP-11): 128x64 diamonds on black; shape_fill keeps 100% of the drawn diamond pixels."""
        atlas, diamond = textured_diamonds(alpha=False)
        self.save(atlas)
        result = self.run_extract(*self.iso, "--background-mode", "shape_fill", "--strict-qc")
        self.assertEqual(result["grid"]["output_tile_size"], [128, 64])
        source = np.asarray(atlas)
        for col in range(2):
            tile = self.tile(f"grass-{col + 1}.png")
            cell, inside = source[:, col * 128:(col + 1) * 128], diamond[:, col * 128:(col + 1) * 128]
            np.testing.assert_array_equal(tile[inside][:, :3], cell[inside])
            self.assertTrue((tile[inside][:, 3] == 255).all())
            self.assertTrue((tile[~inside] == 0).all())
            shape = result["terrains"]["grass"]["variants"][col]["shape"]
            self.assertEqual(shape["footprint_px"], 4096)
            self.assertEqual(shape["spill_px"], 24)  # Pillow's diamond overhangs the exact footprint by 24 px
            self.assertGreaterEqual(shape["coverage"], 0.98)
            self.assertEqual(result["terrains"]["grass"]["variants"][col]["shape_fill"]["fill_rgb"], [0, 0, 0])
        self.assertEqual(result["tile"]["footprint"], [[64, 0], [128, 32], [64, 64], [0, 32]])

    def test_repro_13_transparent_corner_atlas_is_kept_exactly(self):
        atlas, _ = textured_diamonds(alpha=True)
        self.save(atlas)
        result = self.run_extract(*self.iso, "--strict-qc")
        self.assertEqual(result["processing"]["background_mode"], "native_alpha")
        self.assertEqual(result["processing"]["background_mode_requested"], "auto")
        expected = np.asarray(atlas).copy()
        expected[expected[..., 3] == 0] = 0
        np.testing.assert_array_equal(self.tile("grass-1.png"), expected[:, :128])
        np.testing.assert_array_equal(self.tile("grass-2.png"), expected[:, 128:])

    def test_legacy_crop_square_still_loses_the_corners(self):
        """The old opt-in remains available and still says what it does: a centred square crop."""
        atlas, _ = textured_diamonds(alpha=False)
        self.save(atlas)
        result = self.run_extract("--rows", "1", "--cols", "2", "--terrain-row", "grass=0", "--cell-shape",
                                  "crop-square")
        self.assertEqual(result["terrains"]["grass"]["variants"][0]["crop_box"], [32, 0, 96, 64])

    def test_iso_tiles_downscale_premultiplied_and_keep_the_aspect(self):
        atlas, _ = textured_diamonds(alpha=True)
        self.save(atlas)
        result = self.run_extract(*self.iso, "--tile-width", "64")
        self.assertEqual(result["grid"]["output_tile_size"], [64, 32])
        tile = self.tile("grass-1.png")
        self.assertTrue((tile[tile[..., 3] == 0][:, :3] == 0).all())
        self.assertGreater(result["terrains"]["grass"]["variants"][0]["shape"]["coverage"], 0.97)
        with self.assertRaisesRegex(ValueError, "stretch"):
            self.run_extract(*self.iso, "--tile-width", "64", "--tile-height", "64", out="stretched")

    def test_hex_footprints_partition_the_plane(self):
        for shape, (width, height), (step_x, step_y, offset) in (
                ("hex-pointy", (28, 32), (28, 24, 14)), ("hex-flat", (32, 28), (24, 28, 14))):
            with self.subTest(shape=shape):
                mask = MODULE._local_shape_mask(shape, width, height)
                plane = np.zeros((200, 200), np.int32)
                for j in range(-2, 12):
                    for i in range(-2, 12):
                        if shape == "hex-pointy":
                            x, y = i * step_x + (offset if j % 2 else 0), j * step_y
                        else:
                            x, y = i * step_x, j * step_y + (offset if i % 2 else 0)
                        ys, xs = np.nonzero(mask)
                        ys, xs = ys + y, xs + x
                        keep = (ys >= 0) & (ys < 200) & (xs >= 0) & (xs < 200)
                        np.add.at(plane, (ys[keep], xs[keep]), 1)
                self.assertTrue((plane[40:160, 40:160] == 1).all())

    def test_iso_diamond_partitions_the_plane(self):
        mask = MODULE._local_shape_mask("iso-diamond", 32, 16)
        plane = np.zeros((96, 96), np.int32)
        for j in range(-2, 12):
            for i in range(-2, 6):
                x, y = i * 32 + (16 if j % 2 else 0), j * 8
                ys, xs = np.nonzero(mask)
                ys, xs = ys + y, xs + x
                keep = (ys >= 0) & (ys < 96) & (xs >= 0) & (xs < 96)
                np.add.at(plane, (ys[keep], xs[keep]), 1)
        self.assertTrue((plane[16:80, 16:80] == 1).all())

    def test_hex_tiles_from_a_flat_fill(self):
        canvas = Image.new("RGB", (64, 32), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        texture = np.random.default_rng(4).integers(60, 160, (32, 64, 3), np.uint8)
        mask = Image.fromarray((MODULE._local_shape_mask("hex-pointy", 32, 32) * 255).astype(np.uint8))
        for col in range(2):
            canvas.paste(Image.fromarray(texture[:, col * 32:(col + 1) * 32]), (col * 32, 0), mask)
        draw.point((0, 0), fill=(250, 250, 250))  # fill noise within tolerance
        self.save(canvas)
        result = self.run_extract("--rows", "1", "--cols", "2", "--terrain-row", "sand=0", "--shape", "hex-pointy",
                                  "--background-mode", "shape_fill", "--strict-qc")
        tile = self.tile("sand-1.png")
        footprint = MODULE._local_shape_mask("hex-pointy", 32, 32)
        self.assertTrue((tile[footprint][:, 3] == 255).all())
        self.assertTrue((tile[~footprint][:, 3] == 0).all())
        self.assertEqual(result["terrains"]["sand"]["variants"][0]["shape"]["coverage"], 1.0)

    def test_overlay_tiles_keep_alpha_and_skip_opacity_rules(self):
        overlay = np.zeros((32, 64, 4), np.uint8)
        rng = np.random.default_rng(6)
        for col in range(2):
            ys, xs = rng.integers(0, 32, 40), rng.integers(col * 32, col * 32 + 32, 40)
            overlay[ys, xs] = (60, 170, 50, 255)
            overlay[ys, np.minimum(xs + 1, 63)] = (90, 200, 70, 160)
        self.save(overlay)
        result = self.run_extract("--rows", "1", "--cols", "2", "--terrain-row", "tufts=0", "--layer", "overlay",
                                  "--shape", "rect", "--strict-qc", "--min-contrast", "0")
        self.assertEqual(result["tile"]["layer"], "overlay")
        variant = result["terrains"]["tufts"]["variants"][0]
        self.assertNotIn("border", variant)
        self.assertNotIn("shape", variant)
        self.assertLess(variant["visible_fraction"], 0.2)
        expected = overlay[:, :32].copy()
        expected[expected[..., 3] == 0] = 0
        np.testing.assert_array_equal(self.tile("tufts-1.png"), expected)

    def test_chroma_key_overlay(self):
        atlas = np.zeros((16, 32, 3), np.uint8)
        atlas[:] = (255, 0, 255)
        atlas[4:12, 4:12] = (40, 160, 60)
        atlas[4:12, 20:28] = (60, 140, 40)
        atlas[6:10, 6:10] = (80, 200, 90)
        atlas[6:10, 22:26] = (90, 180, 60)
        self.save(atlas)
        result = self.run_extract("--rows", "1", "--cols", "2", "--terrain-row", "bush=0", "--layer", "overlay",
                                  "--shape", "rect", "--background-mode", "chroma_key", "--despill-radius", "1")
        self.assertEqual(result["processing"]["keyer"], "forge_matte.legacy_hard_key")
        tile = self.tile("bush-1.png")
        self.assertEqual(tile[0, 0].tolist(), [0, 0, 0, 0])
        self.assertEqual(tile[5, 5].tolist(), [40, 160, 60, 255])

    def test_background_mode_rules_are_explicit(self):
        atlas, _ = textured_diamonds(alpha=False)
        self.save(atlas)
        cases = [
            ((*self.iso,), "native_alpha needs real transparency"),
            ((*self.iso, "--background-mode", "opaque"), "cannot make iso-diamond base tiles"),
            (("--rows", "1", "--cols", "2", "--terrain-row", "g=0", "--background-mode", "shape_fill",
              "--cell-shape", "crop-square"), "Base square and rect tiles are opaque"),
            (("--rows", "1", "--cols", "2", "--terrain-row", "g=0", "--shape", "rect", "--layer", "overlay",
              "--background-mode", "shape_fill"), "shape_fill keys the flat fill"),
            ((*self.iso, "--tile-size", "64"), "--tile-size and --cell-shape crop-square are for square tiles"),
            (("--rows", "1", "--cols", "2", "--terrain-row", "g=0", "--tile-width", "64"), "use --tile-size"),
            ((*self.iso, "--edge-policy", "seamless"), "cannot be wrap-verified"),
        ]
        for argv, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.run_extract(*argv)
        self.assertFalse((self.root / "tiles").exists())

    def test_shape_fill_refuses_a_busy_background(self):
        atlas, diamonds = textured_diamonds(alpha=False)
        noisy = np.asarray(atlas).copy()
        noisy[~diamonds] = np.random.default_rng(9).integers(0, 255, ((~diamonds).sum(), 3))
        self.save(noisy)
        with self.assertRaisesRegex(ValueError, "not one flat colour"):
            self.run_extract(*self.iso, "--background-mode", "shape_fill")

    def test_low_coverage_and_spill_are_reported(self):
        atlas, _ = textured_diamonds(alpha=True)
        pixels = np.asarray(atlas).copy()
        pixels[:, 128:] = 0                              # an empty second cell
        pixels[0:12, 0:20] = (200, 30, 30, 255)          # art in the first cell's corner, outside the footprint
        self.save(pixels)
        result = self.run_extract(*self.iso, "--min-contrast", "0")
        warnings = " ".join(result["qc"]["warnings"])
        self.assertIn("grass-2 is empty", warnings)
        self.assertIn("grass-2 covers 0.0%", warnings)
        self.assertIn("grass-1 spills", warnings)
        checks = {c["id"]: c["status"] for c in result["qa"]["checks"]}
        self.assertEqual((checks["shape_coverage"], checks["shape_spill"]), ("fail", "fail"))


# --------------------------------------------------------------------------- repro_10/10b: wrap-aware resize, seams

class SeamlessTests(TerrainCase):
    def test_repro_10_wrap_aware_resize_matches_the_reference(self):
        """repro_10 (MAP-15): a periodic tile downscaled 256 -> 64 stays within 1/255 of the wrap-aware result."""
        tile = periodic_noise()
        variant = np.roll(tile, (128, 85), axis=(0, 1))
        self.save(np.concatenate([tile, variant], 1))
        result = self.run_extract("--rows", "1", "--cols", "2", "--terrain-row", "grass=0", "--tile-size", "64",
                                  "--edge-policy", "seamless", "--strict-qc")
        self.assertTrue(result["processing"]["wrap_aware_resize"])
        for name, source in (("grass-1.png", tile), ("grass-2.png", variant)):
            difference = np.abs(self.tile(name)[..., :3].astype(int) - wrap_aware_reference(source, 64))
            self.assertLessEqual(int(difference.max()), 1)
        for variant_payload in result["terrains"]["grass"]["variants"]:
            self.assertEqual({axis: report["verdict"] for axis, report in variant_payload["wrap"].items()},
                             {"x": "continuous", "y": "continuous"})
        cross = result["terrains"]["grass"]["cross_variant_seams"]
        self.assertEqual(cross["pairs"], 4)
        self.assertTrue(cross["not_continuous"])  # rolled copies do not join each other: reported, not gated

    def test_repro_10b_bricks_wrap_within_one_level(self):
        """repro_10b (MAP-15): hard-edged bricks, 256 -> 48; the old edge-clamped resize was 19/255 off."""
        bricks = periodic_bricks()
        self.save(bricks)
        self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "brick=0", "--tile-size", "48",
                         "--edge-policy", "seamless", "--strict-qc")
        reference = np.asarray(Image.fromarray(np.tile(bricks, (3, 3, 1))).resize((144, 144),
                                                                                 Image.Resampling.LANCZOS))[48:96, 48:96]
        self.assertLessEqual(int(np.abs(self.tile("brick-1.png")[..., :3].astype(int) - reference).max()), 1)
        clamped = np.asarray(Image.fromarray(bricks).resize((48, 48), Image.Resampling.LANCZOS))
        self.assertGreater(int(np.abs(clamped.astype(int) - reference).max()), 10)

    def test_isolated_policy_keeps_the_legacy_pixels(self):
        tile = periodic_noise()
        self.save(tile)
        result = self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "grass=0", "--tile-size", "64")
        legacy = np.asarray(Image.fromarray(tile).resize((64, 64), Image.Resampling.LANCZOS))
        np.testing.assert_array_equal(self.tile("grass-1.png")[..., :3], legacy)
        self.assertFalse(result["processing"]["wrap_aware_resize"])
        self.assertNotIn("wrap", result["terrains"]["grass"]["variants"][0])

    def test_tile_that_does_not_wrap_fails_the_seamless_claim(self):
        field = periodic_noise(512, seed=11)[:128, :96]  # a crop: its edges are unrelated
        self.save(field)
        with self.assertRaisesRegex(ValueError, "has a seam"):
            self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "grass=0", "--shape", "rect",
                             "--edge-policy", "seamless", "--strict-qc")
        self.assertFalse((self.root / "tiles").exists())

    def test_seamless_claims_are_checked_on_both_axes(self):
        tile = periodic_noise(96, seed=5).astype(int)
        tile[:48] += 50  # the top half is brighter: rows wrap onto a step, columns still wrap cleanly
        self.save(np.clip(tile, 0, 255).astype(np.uint8))
        result = self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "grass=0", "--edge-policy",
                                  "seamless")
        wrap = result["terrains"]["grass"]["variants"][0]["wrap"]
        self.assertEqual(wrap["x"]["verdict"], "continuous")
        self.assertEqual(wrap["y"]["verdict"], "seam")
        self.assertIn("top/bottom edges wrap", " ".join(result["qc"]["warnings"]))

    def test_duplicated_edge_is_flagged(self):
        x = np.arange(32)
        v = 128 + 80 * np.sin(2 * np.pi * x / 31)  # column 31 equals column 0
        tile = np.repeat(np.stack([v, v * 0.8, v * 0.5], -1)[None], 32, axis=0).astype(np.uint8)
        self.save(tile)
        result = self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "wave=0", "--edge-policy", "seamless")
        self.assertEqual(result["terrains"]["wave"]["variants"][0]["wrap"]["x"]["verdict"], "duplicate_edge")
        self.assertIn("duplicates its edge", " ".join(result["qc"]["warnings"]))


# --------------------------------------------------------------------------- Wang masks per transition row

class WangTests(TerrainCase):
    masks = ["0000", "0001", "0010", "0011", "0100", "0101", "0110", "0111",
             "1000", "1001", "1010", "1011", "1100", "1101", "1110", "1111"]

    def wang_atlas(self, broken=None):
        tiles = [wang_tile(tuple(int(c) for c in mask)) for mask in self.masks]
        if broken is not None:
            tiles[broken] = np.clip(tiles[broken].astype(int) + 60, 0, 255).astype(np.uint8)
        return np.concatenate(tiles, axis=1)

    def argv(self, *extra):
        return ("--rows", "1", "--cols", "16", "--terrain-row", "shore=0", "--resampler", "nearest",
                "--wang", "shore=water/grass:" + ",".join(self.masks), "--min-contrast", "0", *extra)

    def test_masks_are_recorded_per_variant(self):
        self.save(self.wang_atlas())
        result = self.run_extract(*self.argv())
        shore = result["terrains"]["shore"]
        self.assertEqual(shore["kind"], "wang_corner")
        self.assertEqual(shore["materials"], ["water", "grass"])
        self.assertEqual([variant["wang"] for variant in shore["variants"]],
                         [[int(c) for c in mask] for mask in self.masks])
        self.assertEqual(shore["wang_coverage"], {"masks": 16, "of": 16, "complete": True, "missing_count": 0,
                                                  "missing": []})
        self.assertEqual(shore["variant_difference_min"], 1.0)  # no two variants share a mask

    def test_exact_wang_set_passes_seamless_verification_and_a_broken_tile_fails(self):
        self.save(self.wang_atlas())
        result = self.run_extract(*self.argv("--edge-policy", "seamless", "--strict-qc"))
        seams = result["terrains"]["shore"]["wang_seams"]
        self.assertEqual(seams["failed"], [])
        self.assertEqual(seams["legal_joins"], 2 * 16 * 4)  # each tile has 4 legal right and 4 legal lower neighbours
        self.save(self.wang_atlas(broken=5), "broken.png")
        with self.assertRaisesRegex(ValueError, "Wang join shore-6"):
            self.run_extract(*self.argv("--edge-policy", "seamless", "--strict-qc"), atlas="broken.png", out="broken")

    def test_fills_named_after_materials_complete_and_join_the_set(self):
        transitions = [mask for mask in self.masks if mask not in ("0000", "1111")]
        atlas = np.concatenate([
            np.concatenate([wang_tile((0, 0, 0, 0))] * 14, axis=1),
            np.concatenate([wang_tile(tuple(int(c) for c in mask)) for mask in transitions], axis=1),
            np.concatenate([wang_tile((1, 1, 1, 1))] * 14, axis=1)], axis=0)
        self.save(atlas)
        result = self.run_extract("--rows", "3", "--cols", "14", "--terrain-row", "water=0", "--terrain-row", "shore=1",
                                  "--terrain-row", "grass=2", "--resampler", "nearest", "--min-contrast", "0",
                                  "--min-variant-difference", "0", "--wang", "shore=water/grass:" + ",".join(transitions),
                                  "--edge-policy", "seamless", "--strict-qc")
        shore = result["terrains"]["shore"]
        self.assertTrue(shore["wang_coverage"]["complete"])
        self.assertGreater(shore["wang_seams"]["legal_joins"], 2 * 14 * 4)
        self.assertEqual(result["terrains"]["water"]["kind"], "fill")

    def test_incomplete_set_lists_missing_masks(self):
        self.save(self.wang_atlas()[:, :16 * 4])
        result = self.run_extract("--rows", "1", "--cols", "4", "--terrain-row", "shore=0", "--resampler", "nearest",
                                  "--wang", "shore=water/grass:0000,0001,0010,0011", "--min-contrast", "0")
        coverage = result["terrains"]["shore"]["wang_coverage"]
        self.assertEqual((coverage["masks"], coverage["missing_count"]), (4, 12))
        self.assertIn("1111", coverage["missing"])

    def test_duplicate_masks_are_variants_and_compared_for_similarity(self):
        tile = wang_tile((0, 0, 0, 1))
        self.save(np.concatenate([tile, tile], axis=1))
        result = self.run_extract("--rows", "1", "--cols", "2", "--terrain-row", "shore=0", "--resampler", "nearest",
                                  "--wang", "shore=water/grass:0001,0001", "--min-contrast", "0")
        self.assertEqual([v["variant"] for v in result["terrains"]["shore"]["variants"]], [0, 1])
        self.assertIn("too similar", " ".join(result["qc"]["warnings"]))

    def test_invalid_wang_arguments(self):
        self.save(self.wang_atlas())
        parser_errors = ["shore", "shore=water:0001", "shore=water/water:0001", "shore=water/grass:001",
                         "shore=water/grass:0002", "shore=water/grass:00x1"]
        for value in parser_errors:
            with self.subTest(value=value), self.assertRaises(SystemExit), \
                    mock.patch("sys.stderr"):
                MODULE.build_parser().parse_args(["--input", "a", "--output-dir", "b", "--rows", "1", "--cols", "1",
                                                  "--terrain-row", "a=0", "--wang", value])
        for argv, message in (
                (("--rows", "1", "--cols", "16", "--terrain-row", "shore=0", "--wang", "shore=a/b:0001"),
                 "lists 1 masks"),
                (("--rows", "1", "--cols", "16", "--terrain-row", "shore=0", "--wang",
                  "coast=a/b:" + ",".join(self.masks)), "unknown terrain"),
                (("--rows", "1", "--cols", "16", "--terrain-row", "shore=0", "--wang", "shore=a/b:" + ",".join(self.masks),
                  "--wang", "Shore=a/b:" + ",".join(self.masks)), "given twice")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.run_extract(*argv)


# --------------------------------------------------------------------------- MAP-23, MAP-04: names and grids

class NamesAndGridTests(TerrainCase):
    def test_cjk_terrain_names_are_kept_as_display_names(self):
        """MAP-23: a CJK terrain name was rejected; it now gets a portable file stem."""
        rng = np.random.default_rng(8)
        self.save(rng.integers(40, 200, (32, 32, 3), np.uint8))
        result = self.run_extract("--rows", "2", "--cols", "2", "--terrain-row", "草地=0", "--terrain-row", "Snow Field=1",
                                  "--emission", "草地=0.5")
        self.assertEqual(sorted(result["terrains"]), ["snow-field", "terrain-0"])
        self.assertEqual(result["terrains"]["terrain-0"]["display_name"], "草地")
        self.assertEqual(result["terrains"]["terrain-0"]["material"], {"emission_energy": 0.5})
        self.assertTrue((self.root / "tiles/terrain-0-1.png").is_file())
        manifest = json.loads((self.root / "tiles/terrain-bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["terrains"]["terrain-0"]["display_name"], "草地")

    def test_names_that_collide_as_file_stems_are_rejected(self):
        self.save(np.zeros((16, 16, 3), np.uint8))
        with self.assertRaisesRegex(ValueError, "only be mapped once"):
            self.run_extract("--rows", "2", "--cols", "1", "--terrain-row", "Grass=0", "--terrain-row", "grass=1",
                             "--cell-shape", "crop-square")

    def test_grid_rounding_slices_1254_px_into_4x4(self):
        """MAP-04: a 1254^2 atlas (the repo's own showcase size) in a 4x4 grid."""
        rng = np.random.default_rng(12)
        atlas = rng.integers(40, 200, (1254, 1254, 3), np.uint8)
        self.save(atlas)
        names = [f"--terrain-row=t{row}={row}" for row in range(4)]
        argv = ("--rows", "4", "--cols", "4", *names, "--resampler", "nearest", "--min-variant-difference", "0")
        with self.assertRaisesRegex(ValueError, "--grid-rounding nearest"):
            self.run_extract(*argv)
        result = self.run_extract(*argv, "--grid-rounding", "nearest")
        self.assertEqual(result["grid"]["source_cell_size"], [313, 313])
        self.assertEqual(result["grid"]["source_cell_size_max"], [314, 314])
        self.assertEqual(result["grid"]["output_tile_size"], [313, 313])
        boxes = [v["source_box"] for t in result["terrains"].values() for v in t["variants"]]
        self.assertEqual(sorted({b[0] for b in boxes}), [0, 314, 627, 941])
        covered = np.zeros((1254, 1254), np.int32)
        for x0, y0, x1, y1 in boxes:
            covered[y0:y1, x0:x1] += 1
        self.assertTrue((covered == 1).all())
        exact = [v for t in result["terrains"].values() for v in t["variants"] if not v["resized"]]
        self.assertEqual(len(exact), 4)  # the 313 x 313 cells are copied, the others resampled
        x0, y0, x1, y1 = result["terrains"]["t1"]["variants"][1]["source_box"]
        np.testing.assert_array_equal(self.tile("t1-2.png")[..., :3], atlas[y0:y1, x0:x1])


# --------------------------------------------------------------------------- DOC-10: staged publication

class PublicationTests(TerrainCase):
    def setUp(self):
        super().setUp()
        self.save(np.random.default_rng(1).integers(40, 200, (16, 32, 3), np.uint8))
        self.argv = ("--rows", "1", "--cols", "2", "--terrain-row", "grass=0")

    def test_existing_output_directory_is_refused_and_untouched(self):
        """DOC-10: stale files were mixed into a fresh manifest; an existing directory is now refused."""
        (self.root / "tiles").mkdir()
        (self.root / "tiles" / "rock-1.png").write_bytes(b"stale")
        with self.assertRaises(FileExistsError):
            self.run_extract(*self.argv)
        self.assertEqual([p.name for p in (self.root / "tiles").iterdir()], ["rock-1.png"])

    def test_failure_leaves_no_stage_behind(self):
        with self.assertRaises(ValueError):
            self.run_extract(*self.argv, "--strict-qc", "--min-contrast", "1")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["atlas.png"])

    def test_manifest_sidecar_is_published_and_rolled_back(self):
        result = self.run_extract(*self.argv, "--manifest", str(self.root / "meta" / "grass.json"))
        manifest = json.loads((self.root / "meta" / "grass.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["terrains"]["grass"]["variants"][0]["path"], "../tiles/grass-1.png")
        self.assertEqual(manifest, json.loads(json.dumps(result)))
        self.assertFalse((self.root / "tiles" / "terrain-bundle.json").exists())
        with self.assertRaises(FileExistsError):  # the sidecar already exists
            self.run_extract(*self.argv, "--manifest", str(self.root / "meta" / "grass.json"), out="tiles2")
        self.assertFalse((self.root / "tiles2").exists())

        original = MODULE.forge_core.publish_directory_no_replace
        def compete(stage, final):
            Path(final).mkdir()
            original(stage, final)
        with mock.patch.object(MODULE.forge_core, "publish_directory_no_replace", side_effect=compete):
            with self.assertRaises(FileExistsError):
                self.run_extract(*self.argv, "--manifest", str(self.root / "meta" / "other.json"), out="tiles3")
        self.assertFalse((self.root / "meta" / "other.json").exists())
        self.assertEqual(list((self.root / "tiles3").iterdir()), [])

    def test_manifest_in_a_subfolder_of_the_output(self):
        result = self.run_extract(*self.argv, "--manifest", str(self.root / "tiles" / "meta" / "bundle.json"))
        self.assertEqual(result["terrains"]["grass"]["variants"][0]["path"], "../grass-1.png")
        self.assertTrue((self.root / "tiles" / "meta" / "bundle.json").is_file())

    def test_prompt_is_recorded_by_hash(self):
        (self.root / "prompt.txt").write_text("grass, two variants", encoding="utf-8")
        result = self.run_extract(*self.argv, "--prompt", str(self.root / "prompt.txt"))
        self.assertEqual(result["prompt"]["path"], "../prompt.txt")
        self.assertEqual(result["prompt"]["sha256"],
                         hashlib.sha256((self.root / "prompt.txt").read_bytes()).hexdigest())
        self.assertIn(result["prompt"], result["qa"]["inputs"])


# --------------------------------------------------------------------------- contract and QA envelope

class ContractTests(TerrainCase):
    def test_bundles_validate_against_the_proposed_contract(self):
        noise = periodic_noise()
        atlas, _ = textured_diamonds(alpha=True)
        cases = {
            "fill": (noise, ("--rows", "1", "--cols", "1", "--terrain-row", "grass=0", "--tile-size", "64",
                             "--edge-policy", "seamless", "--prompt", str(self.root / "p.txt"))),
            "legacy defaults": (noise, ("--rows", "1", "--cols", "1", "--terrain-row", "grass=0",
                                        "--emit-runtime-defaults")),
            "iso": (atlas, ("--rows", "1", "--cols", "2", "--terrain-row", "grass=0", "--shape", "iso-diamond")),
            "wang": (np.concatenate([wang_tile((0, 0, 0, 1)), wang_tile((0, 0, 1, 1))], 1),
                     ("--rows", "1", "--cols", "2", "--terrain-row", "shore=0", "--wang", "shore=a/b:0001,0011",
                      "--edge-policy", "seamless")),
        }
        (self.root / "p.txt").write_text("prompt", encoding="utf-8")
        for name, (image, argv) in cases.items():
            with self.subTest(name=name):
                self.save(image, f"{name}.png")
                result = self.run_extract(*argv, atlas=f"{name}.png", out=name)
                written = json.loads((self.root / name / "terrain-bundle.json").read_text(encoding="utf-8"))
                self.assertEqual(written, json.loads(json.dumps(result)))
                self.assertEqual(terrain_errors(written), [])

    def test_contract_rejects_bad_documents(self):
        self.save(periodic_noise())
        manifest = json.loads(json.dumps(self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "grass=0")))
        mutations = {
            "v1 id": lambda d: d.update(schema="generate2dmap.terrain_tile_bundle.v1"),
            "absolute tile path": lambda d: d["terrains"]["grass"]["variants"][0].update(path="C:/x/grass-1.png"),
            "seamless claim": lambda d: d["qc"].update(seamless_verified=True),
            "wang row without masks": lambda d: d["terrains"]["grass"].update(kind="wang_corner"),
            "unknown shape": lambda d: d["tile"].update(shape="octagon"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                document = copy.deepcopy(manifest)
                mutate(document)
                self.assertTrue(terrain_errors(document))

    def test_qa_envelope_binds_inputs_and_outputs(self):
        self.save(periodic_noise())
        result = self.run_extract("--rows", "1", "--cols", "1", "--terrain-row", "grass=0", "--tile-size", "32")
        qa = result["qa"]
        self.assertEqual(qa["status"], "pass")
        self.assertEqual(qa["tool"], {"name": "extract_terrain_tiles.py", "version": "2.0"})
        self.assertEqual(qa["inputs"][0]["path"], "../atlas.png")
        for ref in qa["inputs"] + qa["outputs"]:
            path = (self.root / "tiles" / ref["path"]).resolve()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), ref["sha256"])
        self.assertTrue(any("seam" in text for text in qa["notProven"]))
        checks = {check["id"]: check["status"] for check in qa["checks"]}
        self.assertEqual(checks, {"contrast": "pass", "variant_difference": "pass", "border_frame": "pass",
                                  "shape_coverage": "skipped", "shape_spill": "skipped", "wrap_seams": "skipped",
                                  "wang_seams": "skipped"})

    def test_output_is_deterministic(self):
        self.save(periodic_noise())
        argv = ("--rows", "1", "--cols", "1", "--terrain-row", "grass=0", "--tile-size", "48", "--edge-policy",
                "seamless")
        first = self.run_extract(*argv, out="a")
        second = self.run_extract(*argv, out="b")
        self.assertEqual(first, second)
        self.assertEqual((self.root / "a/grass-1.png").read_bytes(), (self.root / "b/grass-1.png").read_bytes())


# --------------------------------------------------------------------------- CLI conventions (plan Appendix D)

class CliTests(TerrainCase):
    def setUp(self):
        super().setUp()
        self.save(np.random.default_rng(1).integers(40, 200, (16, 32, 3), np.uint8))

    def argv(self, out="tiles", *extra):
        return [SCRIPT, "--input", self.root / "atlas.png", "--output-dir", self.root / out, "--rows", "1",
                "--cols", "2", "--terrain-row", "grass=0", *extra]

    def test_help_is_ascii_under_cp1252_and_cp950(self):
        assert_cli_help("generate2dmap", "extract_terrain_tiles")

    def test_refuses_existing_output(self):
        (self.root / "tiles").mkdir()
        result = run_cli(self.argv(), "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("error: Output directory already exists"), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(list((self.root / "tiles").iterdir()), [])

    def test_strict_qc_failure_publishes_nothing(self):
        result = run_cli(self.argv("tiles", "--strict-qc", "--min-contrast", "1"), "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: Terrain atlas QC failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["atlas.png"])

    def test_success_prints_one_ascii_json_line_even_for_cjk_names(self):
        argv = [SCRIPT, "--input", self.root / "atlas.png", "--output-dir", self.root / "tiles", "--rows", "1",
                "--cols", "2", "--terrain-row", "草地=0", "--strict-qc"]
        result = run_cli(argv, "cp1252")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].isascii())
        summary = json.loads(lines[0])
        self.assertEqual(Path(summary["manifest"]), (self.root / "tiles" / "terrain-bundle.json").absolute())
        self.assertTrue(summary["qc"]["passed"])

    def test_friendly_errors(self):
        cases = {
            "missing input": [SCRIPT, "--input", self.root / "nope.png", "--output-dir", self.root / "a", "--rows",
                              "1", "--cols", "1", "--terrain-row", "a=0"],
            "indivisible grid": [SCRIPT, "--input", self.root / "atlas.png", "--output-dir", self.root / "b",
                                 "--rows", "1", "--cols", "3", "--terrain-row", "a=0"],
            "unknown emission": self.argv("c", "--emission", "lava=1"),
        }
        for name, argv in cases.items():
            with self.subTest(name=name):
                result = run_cli(argv, "cp1252")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(result.stderr.startswith("error: "), result.stderr)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
