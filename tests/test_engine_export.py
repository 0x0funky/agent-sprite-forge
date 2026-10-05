"""cfed170 engine_export behaviour that 3.0 keeps (copied from test_video2dsprite.py by A0-T3).

Geometry, staged no-clobber publishing, the PNG fallback without ffmpeg and the packed/VP9
round trip still hold for the old ``package(args)`` namespace of video2dsprite.py. The new
3.0 behaviour is tested in test_engine_export_v3.py.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from forge_testutils import load_script

E = load_script("video2dsprite", "engine_export")
AV = load_script("video2dsprite", "forge_av")


def frame(box=(10, 10, 22, 28), color=(40, 160, 210, 255), size=(32, 32)):
    im = Image.new("RGBA", size)
    im.paste(color, box)
    return im


def args(source, out, **kw):
    values = dict(clean_dir=str(source), out_dir=str(out), name="idle", fps=12.0,
                  source_size="64,64", source_anchor="32,56", max_side=24,
                  crop_union=False, formats="png", loop=True)
    values.update(kw)
    return argparse.Namespace(**values)


class RegistrationTests(unittest.TestCase):
    def test_geometry_survives_mobile_downscale_and_union_crop(self):
        images = [frame(), frame((12, 12, 25, 28))]
        output, geo = E.prepare(images, (448, 448), (224, 392), 16, True)
        self.assertEqual(geo["sourceSize"], [448, 448])
        self.assertEqual(geo["sourceAnchor"], [224, 392])
        self.assertEqual(geo["sourceRect"], [140, 140, 210, 252])
        rx, ry, rw, rh = geo["sourceRect"]
        cw, ch = geo["contentSize"]
        ax, ay = geo["encodedAnchor"]
        self.assertAlmostEqual(rx+ax*rw/cw, 224)
        self.assertAlmostEqual(ry+ay*rh/ch, 392)
        self.assertTrue(all(im.size == output[0].size for im in output))

    def test_odd_padding_is_transparent_and_described(self):
        output, geo = E.prepare([frame((1, 1, 12, 14), size=(15, 17))], (15, 17), (7, 14), 384, False)
        self.assertEqual(geo["encodedSize"], [16, 18])
        self.assertEqual(geo["contentSize"], [15, 17])
        self.assertEqual(output[0].getpixel((15, 17))[3], 0)

    def test_source_aspect_mismatch_requires_explicit_registration(self):
        with self.assertRaisesRegex(ValueError, "aspect differs"):
            E.prepare([frame()], (448, 256), (224, 240), 320, False)

    def test_union_crop_keeps_faint_nonzero_alpha(self):
        im = frame()
        im.putpixel((0, 8), (180, 240, 255, 20))
        output, geo = E.prepare([im], (32, 32), (16, 28), 32, True)
        self.assertEqual(geo["sourceRect"][0], 0)
        self.assertEqual(output[0].getpixel((0, 0))[3], 20)

    def test_seam_and_contact_are_diagnostics_not_false_pass(self):
        qa = E.analyze([frame(), frame((10, 4, 22, 20))], 12, True)
        self.assertEqual(qa["status"], "needs-visual-review")
        self.assertEqual(qa["boundsBottomSpanPx"], 8)
        self.assertGreater(qa["seamPremultipliedMAE"], 0)
        self.assertNotIn("pass", qa)

    def test_transparent_rgb_does_not_inflate_loop_error(self):
        a, b = frame(), frame()
        b.paste((255, 0, 255, 0), (0, 0, 3, 3))
        self.assertEqual(E.analyze([a, b], 12, True)["seamPremultipliedMAE"], 0)


class PackageTests(unittest.TestCase):
    def make_frames(self, source):
        source.mkdir()
        for i in range(4):
            frame((8+i, 8, 23, 28)).save(source/f"clean_{i:04d}.png")

    def test_png_fallback_works_without_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(E.shutil, "which", return_value=None):
            source, out = Path(tmp)/"clean", Path(tmp)/"out"
            self.make_frames(source)
            meta = E.package(args(source, out))
            self.assertEqual(meta["sourceSize"], [64, 64])
            self.assertEqual(meta["sourceAnchor"], [32, 56])
            self.assertEqual(meta["frameCount"], 4)
            self.assertTrue((out/meta["poster"]["file"]).is_file())
            self.assertTrue((out/meta["fallback"]["pages"][0]["file"]).is_file())
            self.assertEqual(json.loads((out/"animation.json").read_text())["hitEvents"], [])

    def test_missing_encoder_is_actionable_and_does_not_partially_write(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(E, "capabilities", return_value={"packed": False}):
            source, out = Path(tmp)/"clean", Path(tmp)/"out"
            self.make_frames(source)
            with self.assertRaisesRegex(RuntimeError, "--formats png"):
                E.package(args(source, out, formats="packed"))
            self.assertFalse(out.exists())

    def test_out_must_differ_from_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"clean"; self.make_frames(source)
            with self.assertRaisesRegex(ValueError, "differ"):
                E.package(args(source, source))

    def test_existing_package_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp)/"clean", Path(tmp)/"out"
            self.make_frames(source)
            E.package(args(source, out))
            original = (out/"animation.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                E.package(args(source, out))
            self.assertEqual((out/"animation.json").read_bytes(), original)

    def test_failed_encoding_does_not_publish_partial_package(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(E, "encode", side_effect=RuntimeError("injected encoder failure")):
            source, out = Path(tmp)/"clean", Path(tmp)/"out"
            self.make_frames(source)
            with self.assertRaisesRegex(RuntimeError, "injected encoder failure"):
                E.package(args(source, out))
            self.assertFalse(out.exists())
            self.assertEqual(list(Path(tmp).glob(".idle-stage-*")), [])

    def test_nonfinite_fps_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"clean"; self.make_frames(source)
            with self.assertRaises(ValueError): E.package(args(source, Path(tmp)/"out", fps=float("nan")))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg + ffprobe optional")
    def test_real_video_roundtrip_alpha_dimensions_and_trim(self):
        caps = E.capabilities()
        if not caps["packed"] or not caps["webm"]: self.skipTest("libx264 + libvpx-vp9 required")
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp)/"clean", Path(tmp)/"out"
            self.make_frames(source)
            meta = E.package(args(source, out, formats="png,packed,webm", max_side=32))
            packed = out/meta["packedAlpha"]["file"]
            decoded = Path(tmp)/"packed.png"
            E.run([caps["ffmpeg"], "-hide_banner", "-loglevel", "error", "-i", str(packed), "-frames:v", "1", str(decoded)])
            image = np.asarray(Image.open(decoded).convert("RGB"))
            self.assertEqual(image.shape[:2], (32, 64))
            self.assertLess(image[2, 34, 0], 8)
            self.assertGreater(image[15, 47, 0], 245)
            alpha = Path(tmp)/"alpha.png"
            E.run([caps["ffmpeg"], "-hide_banner", "-loglevel", "error", "-c:v", "libvpx-vp9", "-i",
                   str(out/meta["webm"]["file"]), "-frames:v", "1", "-vf", "alphaextract", str(alpha)])
            self.assertEqual(Image.open(alpha).getextrema(), (0, 255))
            decoded_alpha = AV.extract_frames(out/meta["webm"]["file"], Path(tmp)/"webm-extract", alpha="on")
            self.assertEqual(Image.open(decoded_alpha[0]).getchannel("A").getextrema(), (0, 255))
            frames = AV.extract_frames(packed, Path(tmp)/"extract", fps=12, start=1/12, duration=2/12)
            self.assertEqual(len(frames), 2)
            self.assertTrue(all(v["fullDecodePassed"] for v in [meta["webm"], meta["packedAlpha"]]))


if __name__ == "__main__":
    unittest.main()
