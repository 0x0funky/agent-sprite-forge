from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / "skills/generate2dsprite/scripts/make_anchor_layout.py"
SPEC = importlib.util.spec_from_file_location("anchor_layout_native", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AnchorTemplateTests(unittest.TestCase):
    def source(self):
        image = Image.new("RGBA", (6, 6), (0, 0, 0, 0))
        image.putpixel((2, 2), (255, 0, 255, 255))
        image.putpixel((3, 2), (255, 0, 255, 128))
        image.putpixel((2, 3), (20, 40, 80, 255))
        image.putpixel((3, 3), (20, 40, 80, 128))
        return image

    def build(self, source=None, **overrides):
        options = dict(rows=2, cols=3, cell_width=8, cell_height=8,
                       subject_height_ratio=0.5, subject_width_ratio=0.5, feet_ratio=0.75,
                       threshold=100, edge_threshold=150, background_mode="native_alpha", resampler="nearest")
        options.update(overrides)
        return MODULE.build_anchor_layout(source or self.source(), **options)

    def test_native_alpha_keeps_purple_partial_alpha_and_palette(self):
        source = self.source()
        template = self.build(source)
        self.assertEqual(template.mode, "RGBA")
        self.assertEqual(template.size, (24, 16))
        original_colors = {source.getpixel((x, y)) for x in range(6) for y in range(6)}
        output_colors = {template.getpixel((x, y)) for x in range(24) for y in range(16)}
        self.assertEqual(output_colors, original_colors)
        self.assertEqual(template.getpixel((4, 2)), (255, 0, 255, 128))
        self.assertEqual(template.getpixel((0, 0)), (0, 0, 0, 0))

    def test_repeated_cells_share_exact_pixels_scale_and_bottom_line(self):
        template = self.build()
        first = template.crop((0, 0, 8, 8))
        self.assertEqual(first.getchannel("A").getbbox(), (2, 2, 6, 6))
        for row in range(2):
            for col in range(3):
                self.assertEqual(template.crop((col * 8, row * 8, col * 8 + 8, row * 8 + 8)).tobytes(), first.tobytes())

    def test_default_magenta_layout_matches_legacy_algorithm(self):
        source = Image.new("RGBA", (12, 12), (255, 0, 255, 255))
        source.paste((20, 40, 80, 255), (4, 2, 8, 10))
        options = dict(rows=1, cols=2, cell_width=20, cell_height=20, subject_height_ratio=0.6,
                       subject_width_ratio=0.6, feet_ratio=0.8, threshold=100, edge_threshold=150)
        actual = MODULE.build_anchor_layout(source, **options)
        cleaned = MODULE.sprite_helpers().remove_bg_magenta(source, 100, 150)
        subject = cleaned.crop(cleaned.getchannel("A").getbbox()).resize((6, 12), Image.Resampling.LANCZOS)
        expected = Image.new("RGBA", (40, 20), (255, 0, 255, 255))
        expected.alpha_composite(subject, (7, 4))
        expected.alpha_composite(subject, (27, 4))
        self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_background_cleanup_reused_once_without_chroma_for_native(self):
        helpers = MODULE.sprite_helpers()
        with mock.patch.object(helpers, "prepare_background", wraps=helpers.prepare_background) as prepared:
            with mock.patch.object(helpers, "remove_bg_magenta", side_effect=AssertionError("native cannot key")):
                self.build()
        prepared.assert_called_once()

    def test_fully_opaque_and_empty_fake_native_alpha_rejected(self):
        for image in (Image.new("RGB", (6, 6), "white"), Image.new("RGBA", (6, 6))):
            with self.assertRaisesRegex(ValueError, "actual transparency"):
                self.build(image)

    def test_opaque_template_rejected_instead_of_treating_rectangle_as_subject(self):
        with self.assertRaisesRegex(ValueError, "does not isolate the subject"):
            self.build(Image.new("RGB", (6, 6), "white"), background_mode="opaque")

    def test_out_of_cell_top_or_bottom_contract_rejected(self):
        for overrides in ({"feet_ratio": 0.2}, {"feet_ratio": 1.2}, {"subject_height_ratio": 1.2}):
            with self.assertRaises(ValueError):
                self.build(**overrides)

    def test_invalid_resampler_is_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError, "resampler"):
            self.build(resampler="bilinear")

    def test_cli_native_rgba_and_legacy_rgb_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.source().save(root / "input.png")
            command = [sys.executable, "-B", str(SCRIPT), "--input", str(root / "input.png"),
                       "--rows", "1", "--cols", "2", "--cell-width", "8", "--cell-height", "8",
                       "--subject-height-ratio", "0.5", "--subject-width-ratio", "0.5", "--feet-ratio", "0.75"]
            run = subprocess.run(command + ["--background-mode", "native_alpha", "--resampler", "nearest",
                                            "--output", str(root / "native.png")], cwd=root, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            with Image.open(root / "native.png") as image:
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.getpixel((4, 2)), (255, 0, 255, 128))
            chroma = Image.new("RGB", (6, 6), (255, 0, 255))
            chroma.paste((20, 40, 80), (2, 2, 4, 4))
            chroma.save(root / "input.png")
            run = subprocess.run(command + ["--output", str(root / "legacy.png")], cwd=root, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            with Image.open(root / "legacy.png") as image:
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.getpixel((0, 0)), (255, 0, 255))


if __name__ == "__main__":
    unittest.main()
