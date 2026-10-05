"""Geometry v2 of ``generate2dsprite.py process`` (B01-T4, B01-T5).

Most tests convert a sprite-audit repro (``.tmp/claude-audit-20261005/sprite-audit/repros/rNN_*.py``)
into an assertion; the repro id is in each docstring. The fox tests use the provenance-checked
real fixture ``tests/fixtures/real/raw-fox-run-v1.png``.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from forge_testutils import contract_errors, load_script, make_magenta_sheet, real_fixture

G = load_script("generate2dsprite", "generate2dsprite")
BODY = (60, 80, 140, 255)


def process(*argv: str) -> tuple[dict, Path]:
    """Run ``process`` in-process and return (pipeline-meta, output dir)."""
    args = G.build_parser().parse_args(["process", *argv])
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        G.cmd_process(args)
    return json.loads((args.output_dir / "pipeline-meta.json").read_text(encoding="utf-8")), args.output_dir


def process_error(*argv: str) -> str:
    """Run ``process`` expecting a ValueError; return its message."""
    args = G.build_parser().parse_args(["process", *argv])
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            G.cmd_process(args)
        except ValueError as error:
            return str(error)
    raise AssertionError("process succeeded")


def pipeline_meta_errors(document: dict) -> list[str]:
    """Errors against the vendored pipeline_meta_v2 (handoff/B01 section 5 is integrated: an empty
    frame's ``source_rect`` is null)."""
    return contract_errors(document, "sprite", "pipeline_meta_v2", skill="generate2dsprite")


def fox_args(root: Path, *extra: str) -> list[str]:
    """r03: the cold-trial fox run sheet, native alpha, nearest 1/10, feet, preserve."""
    return ["--input", str(real_fixture("raw-fox-run-v1.png")), "--target", "asset", "--mode", "run", "--rows",
            "2", "--cols", "4", "--output-dir", str(root / "fox"), "--background-mode", "native_alpha",
            "--resampler", "nearest", "--align", "feet", "--scale-strategy", "preserve", "--cell-size", "64",
            "--fit-scale", "0.8", "--intentional-low-frame-count", *extra]


class AlphaGeometryTests(unittest.TestCase):
    """B01-T4: geometry counts alpha above 16 and hygiene removes generator haze (S01, DOC-04)."""

    def test_fox_strict_qc_flags_exactly_the_two_real_tail_overflows(self) -> None:
        """r03, report v2 P1-4: strict QC reports frames [0, 2] and [0, 3] only, and nothing is clamped."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            message = process_error(*fox_args(root, "--strict-qc"))
            self.assertEqual(message, "QC failed: raw subjects touch a source-cell edge: [[0, 2], [0, 3]]")
            meta, _out = process(*fox_args(root, "--strict-qc", "--allow-source-edge-touch"))
        self.assertEqual(meta["source_edge_touch_frames"], [[0, 2], [0, 3]])
        self.assertEqual((meta["paste_clamped_frames"], meta["output_edge_touch_frames"]), ([], []))
        self.assertEqual(meta["hygiene"]["floor_px"], 67907)
        self.assertEqual((meta["hygiene"]["detached_components"], meta["hygiene"]["max_removed_alpha"]), (7, 5))
        self.assertEqual(meta["geometry"]["scale_ratio"], [1, 10])
        self.assertEqual(meta["qc_summary"]["pixel_grid_off_edges"], 0)

    def test_fox_godot_height_uses_the_visible_subject(self) -> None:
        """r03b: the Godot pixel size comes from the visible 37.5 px, not the 41 px haze-inflated height."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta, out = process(*fox_args(root, "--allow-source-edge-touch", "--godot-world-height", "1.0"))
            contract = json.loads((out / "godot-sprite3d.json").read_text(encoding="utf-8"))
            legacy, _out = process(*fox_args(root / "legacy"), "--alpha-geometry-threshold", "0",
                                   "--alpha-hygiene", "none")
        self.assertEqual(contract["reference_subject_height_px"], 37.5)
        self.assertAlmostEqual(contract["recommended_pixel_size"], 1.0 / 37.5)
        self.assertGreaterEqual(legacy["qc_summary"]["output_subject_height_mean"], 40.0)

    def test_legacy_switches_restore_alpha_above_zero_geometry(self) -> None:
        """Appendix H: --alpha-geometry-threshold 0 --alpha-hygiene none count the alpha 1-4 haze again
        (r03: all eight fox cells touch their edges)."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta, _out = process(*fox_args(root), "--alpha-geometry-threshold", "0", "--alpha-hygiene", "none")
        self.assertEqual(len(meta["source_edge_touch_frames"]), 8)
        self.assertEqual(meta["hygiene"], {"mode": "none"})

    def test_geometry_threshold_never_changes_pixels(self) -> None:
        """S01: faint haze is ignored for measurement but published unchanged."""
        cell = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        cell.paste((30, 30, 30, 10), (0, 0, 32, 3))   # faint haze along the top edge
        cell.paste(BODY, (10, 6, 22, 26))              # centred: the centre anchor needs no shift
        frames, info = G.split_grid(cell, G.GridOptions(
            1, 1, 32, fit_scale=1.0, scale_strategy="preserve", background_mode="native_alpha",
            resampler="nearest", alpha_hygiene="none"))
        self.assertFalse(info[0]["source_edge_touch"])
        self.assertEqual(info[0]["subject_bbox"], [10, 6, 22, 26])
        self.assertEqual(info[0]["offset_px"], [0, 0])
        self.assertEqual(frames[0].tobytes(), cell.tobytes())


class AnchorTests(unittest.TestCase):
    """B01-T4: anchors on the ground line, measured on the subject (S14, S15, S20, S21)."""

    def test_r06_component_padding_does_not_fake_an_edge_touch(self) -> None:
        """r06, S20: the edge test uses the unpadded subject box; padding cannot move a preserved frame."""
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        image.paste((50, 80, 140, 255), (4, 10, 30, 54))
        results = []
        for padding in (0, 8):
            frames, info = G.split_grid(image, 1, 1, 64, fit_scale=0.8, align="feet", scale_strategy="preserve",
                                        background_mode="native_alpha", component_mode="largest",
                                        component_padding=padding)
            results.append((frames[0].tobytes(), info[0]))
        self.assertEqual(results[0][0], results[1][0])
        self.assertFalse(results[0][1]["source_edge_touch"] or results[1][1]["source_edge_touch"])
        self.assertEqual(results[1][1]["crop_bbox"], [0, 2, 38, 62])

    def test_r13_trim_offset_and_source_rect_map_back_to_the_sheet(self) -> None:
        """r13, S21: per-frame trim offsets and sheet-space subject boxes are recorded."""
        sheet = Image.new("RGBA", (128, 64), (255, 0, 255, 255))
        sheet.paste((40, 90, 150, 255), (84, 10, 108, 50))
        _frames, info = G.split_grid(sheet, 1, 2, 64, trim_border_px=4, edge_clean_depth=0, align="feet",
                                     scale_strategy="preserve")
        self.assertEqual(info[1]["trim_offset"], [4, 4])
        self.assertEqual(info[1]["source_rect"], [84, 10, 108, 50])
        self.assertEqual(info[0]["source_rect"], None)
        tiny = sheet.resize((16, 8), Image.Resampling.NEAREST)
        _frames, info = G.split_grid(tiny, 1, 2, 64, trim_border_px=4, edge_clean_depth=0, align="feet",
                                     scale_strategy="preserve")
        self.assertEqual(info[1]["trim_offset"], [0, 0])  # 8 px cells are too small to trim 4 px
        self.assertEqual(info[1]["source_rect"], [10, 1, 13, 6])

    def test_r14_anchor_is_measured_on_the_subject_not_its_spear(self) -> None:
        """r14, S15: a spear held at chest height no longer becomes the feet, so the feet stay on the
        ground line (the long spear may still need a sideways shift, which is reported).
        --anchor-mode legacy-p98 reproduces the old anchor on the spear and the vertical clamp."""
        cell = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        cell.paste(BODY, (4, 30, 24, 110))
        cell.paste((200, 200, 210, 255), (24, 70, 112, 73))
        common = dict(fit_scale=0.8, align="feet", scale_strategy="preserve", background_mode="native_alpha")
        _frames, info = G.split_grid(cell, 1, 1, 64, **common)
        self.assertEqual(info[0]["anchor_source"], [14.0, 110.0])
        self.assertEqual(info[0]["output_anchor"][1], info[0]["anchor_target"][1])
        self.assertEqual(info[0]["paste_position"][1], info[0]["unclamped_paste_position"][1])
        _frames, legacy = G.split_grid(cell, 1, 1, 64, anchor_mode="legacy-p98", **common)
        self.assertEqual(legacy[0]["anchor_source"][1], 72.0)
        self.assertLess(legacy[0]["paste_position"][1], legacy[0]["unclamped_paste_position"][1])

    def test_r20_feet_land_exactly_on_the_output_origin(self) -> None:
        """r20, S14: the ground line is the bottom edge of the lowest row, so grounded feet sit on the
        declared origin instead of hanging 2-3 px below it (legacy-p98)."""
        cell = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        cell.paste(BODY, (12, 4, 28, 30))
        cell.paste((20, 20, 20, 255), (12, 30, 28, 36))
        for anchor_mode, below in (("feet", 0), ("legacy-p98", 3)):
            frames, info = G.split_grid(cell, G.GridOptions(
                1, 1, 128, fit_scale=0.85, align="feet", scale_strategy="preserve",
                background_mode="native_alpha", resampler="nearest", pixel_scale=3, anchor_mode=anchor_mode))
            bottom = int(np.nonzero(np.asarray(frames[0])[..., 3])[0].max()) + 1
            with self.subTest(anchor_mode=anchor_mode):
                self.assertEqual(info[0]["anchor_target"], [64.0, 119.0])
                self.assertEqual(bottom - 119, below)

    def test_fit_scale_one_with_feet_no_longer_clamps(self) -> None:
        """Probe (generate2dsprite.py:1148): fit 1.0 + feet used to clamp every frame; the feet now sit
        on the cell bottom, which strict QC reports as output-edge contact."""
        sheet = Image.new("RGBA", (128, 64), (0, 0, 0, 0))
        sheet.paste(BODY, (10, 8, 40, 60))
        sheet.paste(BODY, (80, 12, 110, 60))
        _frames, info = G.split_grid(sheet, 1, 2, 64, fit_scale=1.0, align="feet", scale_strategy="preserve",
                                     background_mode="native_alpha")
        self.assertEqual([frame["paste_clamped"] for frame in info], [False, False])
        self.assertEqual([frame["aligned_bbox"][3] for frame in info], [64, 64])

    def test_stance_anchor_mirrors_exactly(self) -> None:
        """S14: the stance anchor is the middle of the support span, so a mirrored pose has a mirrored
        anchor and a turn does not slide the feet."""
        cell = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
        cell.paste(BODY, (10, 6, 30, 34))
        cell.paste(BODY, (10, 34, 16, 44))
        cell.paste(BODY, (24, 34, 33, 44))
        mirrored = cell.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        anchors = [G.split_grid(image, 1, 1, 48, fit_scale=1.0, align="feet", scale_strategy="preserve",
                                background_mode="native_alpha", anchor_mode="stance")[1][0]["anchor_source"]
                   for image in (cell, mirrored)]
        self.assertEqual(anchors[0][0] + anchors[1][0], 48.0)
        self.assertEqual(anchors[0][1], anchors[1][1])

    def test_anchor_px_registers_every_frame(self) -> None:
        """S14: --anchor-px pins the declared anchor; frames keep their drawn offsets."""
        sheet = Image.new("RGBA", (128, 64), (0, 0, 0, 0))
        sheet.paste(BODY, (20, 10, 44, 56))
        sheet.paste(BODY, (84, 4, 108, 50))
        _frames, info = G.split_grid(sheet, G.GridOptions(
            1, 2, 64, fit_scale=1.0, align="feet", scale_strategy="preserve", background_mode="native_alpha",
            resampler="nearest", anchor_px=(32.0, 56.0)))
        self.assertEqual([frame["offset_px"] for frame in info], [[0, 8], [0, 8]])
        self.assertEqual([frame["aligned_bbox"][3] for frame in info], [64, 58])


class ResamplingTests(unittest.TestCase):
    """B01-T5: one shared sampling grid, integer nearest, registered scale, output-space QC."""

    def test_r07_static_body_has_zero_shimmer(self) -> None:
        """r07, S05: a body that does not move gets identical pixels when an arm changes the bbox."""
        rows = np.where((np.arange(48) // 3) % 2 == 0, 160, 60)
        columns = np.where((np.arange(24) // 2) % 2 == 0, 200, 40)
        body = np.zeros((48, 24, 4), np.uint8)
        body[..., 0], body[..., 1], body[..., 2], body[..., 3] = columns[None, :], rows[:, None], 90, 255
        sheet = Image.new("RGBA", (256, 128), (0, 0, 0, 0))
        sheet.paste(Image.fromarray(body), (52, 60))
        sheet.paste(Image.fromarray(body), (180, 60))
        sheet.paste((220, 220, 60, 255), (173, 40, 183, 62))  # a raised arm in cell B only
        for resampler, fit in (("lanczos", 0.8), ("nearest", 1.0)):
            with self.subTest(resampler=resampler):
                frames, info = G.split_grid(sheet, 1, 2, 64, fit_scale=fit, align="feet",
                                            scale_strategy="preserve", background_mode="native_alpha",
                                            resampler=resampler)
                self.assertEqual(info[0]["offset_px"], info[1]["offset_px"])
                first = int(np.floor(75 * info[0]["source_to_output_scale"])) + info[0]["offset_px"][1]
                a, b = (np.asarray(frame)[first:] for frame in frames)
                self.assertTrue(a[..., 3].any())
                self.assertEqual(int(np.any(a != b, axis=-1).sum()), 0)

    def test_r08_integer_nearest_blocks_and_fractional_rejected(self) -> None:
        """r08, S06: nearest refuses a fractional scale, gives uniform 8 px blocks at --pixel-scale 8,
        and recovers 8x-upscaled logical art exactly with --logical-pixel 8."""
        checker = np.zeros((16, 16, 4), np.uint8)
        on = np.add.outer(np.arange(16), np.arange(16)) % 2 == 0
        checker[on] = (230, 90, 40, 255)
        checker[~on] = (40, 60, 160, 255)
        cell = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        cell.paste(Image.fromarray(checker), (8, 8))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cell.save(root / "checker.png")
            common = ("--input", str(root / "checker.png"), "--target", "asset", "--mode", "idle", "--rows", "1",
                      "--cols", "1", "--background-mode", "native_alpha", "--resampler", "nearest",
                      "--scale-strategy", "preserve")
            message = process_error(*common, "--output-dir", str(root / "fractional"), "--cell-size", "54")
            self.assertIn("integer scales (N or 1/N)", message)
            self.assertIn("1.4344", message)
            meta, out = process(*common, "--output-dir", str(root / "blocks"), "--cell-size", "256",
                                "--pixel-scale", "8")
            with Image.open(out / "idle-1.png") as frame:
                pixels = np.asarray(frame)
            legacy, _out = process(*common, "--output-dir", str(root / "legacy"), "--cell-size", "54",
                                   "--legacy-fractional-nearest")
        self.assertEqual(meta["qc_summary"]["pixel_grid_off_edges"], 0)
        ys, xs = np.nonzero(pixels[..., 3])
        art = pixels[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        self.assertEqual(art.shape[:2], (128, 128))
        np.testing.assert_array_equal(art[::8, ::8], checker)
        np.testing.assert_array_equal(art, np.repeat(np.repeat(checker, 8, axis=0), 8, axis=1))
        self.assertIsNone(legacy["qc_summary"]["pixel_grid_off_edges"])

        rng = np.random.default_rng(7)
        palette = rng.integers(0, 256, size=(8, 3), dtype=np.uint8)
        logical = np.dstack([palette[rng.integers(0, 8, size=(48, 48))], np.full((48, 48), 255, np.uint8)])
        big = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        big.paste(Image.fromarray(np.repeat(np.repeat(logical, 8, axis=0), 8, axis=1)), (8, 8))
        frames, info = G.split_grid(big, G.GridOptions(
            1, 1, 64, scale_strategy="preserve", background_mode="native_alpha", resampler="nearest",
            pixel_scale=1, logical_pixel=8))
        pixels = np.asarray(frames[0])
        ys, xs = np.nonzero(pixels[..., 3])
        np.testing.assert_array_equal(pixels[ys.min():ys.max() + 1, xs.min():xs.max() + 1], logical)

    def test_r16_registered_keeps_the_jump(self) -> None:
        """r16, S07: --scale-strategy registered keeps a 30 px jump (15 px at 1/2); preserve flattens it
        and reports the injected shift."""
        sheet = Image.new("RGBA", (256, 128), (0, 0, 0, 0))
        sheet.paste(BODY, (44, 40, 84, 110))
        sheet.paste(BODY, (172, 10, 212, 80))
        common = dict(fit_scale=1.0, align="feet", background_mode="native_alpha", resampler="nearest")
        result = G.process_sheet(sheet, G.GridOptions(1, 2, 64, scale_strategy="registered", **common))
        bottoms = [frame.getbbox()[3] for frame in result.frames]
        self.assertEqual(bottoms[0] - bottoms[1], 15)
        self.assertEqual(result.report["registration_anchor"], [64.0, 110.0])
        self.assertEqual(result.report["injected_shift_px"]["y"], [0.0, 0.0])
        summary = G.summarize_frame_qc(result.info)
        self.assertEqual(summary["scale_reference_frames"], [[0, 0]])
        preserved = G.process_sheet(sheet, G.GridOptions(1, 2, 64, scale_strategy="preserve", **common))
        self.assertEqual([frame.getbbox()[3] for frame in preserved.frames], [64, 64])
        self.assertEqual(preserved.report["injected_shift_px"]["y"], [-7.5, 7.5])

    def character_sheet(self, path: Path, rows: int, cols: int, cell_w: int, cell_h: int, scale: float = 1.0) -> None:
        """r04/r04b: one 30x80 character, pasted feet-down 20 px above each cell's bottom."""
        character = Image.new("RGBA", (30, 80), (0, 0, 0, 0))
        character.paste((60, 90, 150, 255), (5, 0, 25, 25))
        character.paste((40, 60, 110, 255), (0, 25, 30, 60))
        character.paste((30, 30, 30, 255), (3, 60, 12, 80))
        character.paste((30, 30, 30, 255), (18, 60, 27, 80))
        if scale != 1.0:
            character = character.resize((round(30 * scale), round(80 * scale)), Image.Resampling.NEAREST)
        sheet = Image.new("RGBA", (cols * cell_w, rows * cell_h), (0, 0, 0, 0))
        for row in range(rows):
            for col in range(cols):
                sheet.paste(character, (col * cell_w + (cell_w - character.width) // 2,
                                        row * cell_h + cell_h - 20 - character.height))
        sheet.save(path)

    def test_r04_profile_judges_drift_in_output_pixels(self) -> None:
        """r04, r04b, S02: identical pixels in non-square cells pass the profile; a 13% smaller character
        fails it (the cell-area metric had it backwards)."""
        common = ("--target", "asset", "--background-mode", "native_alpha", "--align", "feet",
                  "--scale-strategy", "preserve", "--cell-size", "64", "--fit-scale", "0.8", "--strict-qc",
                  "--intentional-low-frame-count")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.character_sheet(root / "idle.png", 2, 2, 128, 128)
            self.character_sheet(root / "run.png", 2, 4, 96, 128)
            self.character_sheet(root / "smaller.png", 2, 4, 96, 128, 0.866)
            reference, _out = process("--input", str(root / "idle.png"), "--mode", "idle", *common,
                                      "--output-dir", str(root / "idle"),
                                      "--write-scale-profile", str(root / "profile.json"))
            profile = json.loads((root / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(profile["reference"]["output_subject_height_px"], 32.0)
            self.assertEqual(contract_errors(profile, "sprite", "scale_profile_v2", skill="generate2dsprite"), [])
            follow = ("--mode", "run", "--rows", "2", "--cols", "4", "--scale-profile", str(root / "profile.json"))
            run, _out = process("--input", str(root / "run.png"), *follow, *common, "--output-dir", str(root / "run"))
            message = process_error("--input", str(root / "smaller.png"), *follow, *common,
                                    "--output-dir", str(root / "smaller"))
        self.assertEqual(run["qc_summary"]["profile_body_scale_drift"], 0.0)
        self.assertEqual(run["scale_profile"]["version"], 2)
        self.assertRegex(message, r"profile body-scale drift 0\.1[0-9]+ exceeds 0\.1000")

    def test_r05_body_scale_cv_ignores_lateral_drift(self) -> None:
        """r05, S10: an identical character drifting sideways has a body-scale CV of 0."""
        character = Image.new("RGBA", (50, 90), (0, 0, 0, 0))
        character.paste((50, 80, 140, 255), (0, 0, 50, 70))
        character.paste((25, 25, 25, 255), (8, 70, 20, 90))
        character.paste((25, 25, 25, 255), (30, 70, 42, 90))
        sheet = Image.new("RGBA", (512, 128), (0, 0, 0, 0))
        for index, shift in enumerate((0, 10, 20, 30)):
            sheet.paste(character, (index * 128 + 39 + shift, 20))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sheet.save(root / "drift.png")
            meta, _out = process("--input", str(root / "drift.png"), "--target", "asset", "--mode", "idle",
                                 "--rows", "1", "--cols", "4", "--output-dir", str(root / "out"),
                                 "--background-mode", "native_alpha", "--align", "feet", "--scale-strategy",
                                 "preserve", "--cell-size", "128", "--fit-scale", "0.8", "--strict-qc",
                                 "--max-body-scale-cv", "0.08")
        self.assertLessEqual(meta["qc_summary"]["body_scale_cv"], 0.01)
        self.assertGreater(meta["qc_summary"]["legacy_body_scale_cv"], 0.08)

    def test_1254_square_sheet_in_4x4_processes(self) -> None:
        """DOC-03: a 1254^2 host image fills a 4x4 grid with rounded cells or lossless padding; the
        exact mode explains itself in rows x columns."""
        sheet = Image.new("RGBA", (1254, 1254), (0, 0, 0, 0))
        boxes = G.forge_core.rounded_grid_boxes(1254, 1254, 4, 4)
        for x0, y0, x1, y1 in boxes:
            sheet.paste(BODY, (x0 + 100, y0 + 60, x0 + 200, y0 + 260))
        options = dict(background_mode="native_alpha", align="feet", scale_strategy="preserve")
        with self.assertRaisesRegex(ValueError, r"1254x1254 is not divisible into 4 rows x 4 columns"):
            G.split_grid(sheet, 4, 4, 96, **options)
        rounded = G.process_sheet(sheet, G.GridOptions(4, 4, 96, grid_rounding="nearest", **options))
        self.assertEqual([frame["source_box"][2] - frame["source_box"][0] for frame in rounded.info[:4]],
                         [314, 313, 314, 313])
        self.assertEqual(rounded.info[5]["source_rect"], [414, 374, 514, 574])
        padded = G.process_sheet(sheet, G.GridOptions(4, 4, 96, pad_to_grid=True, **options))
        self.assertEqual(padded.report["pad_offset"], [1, 1])
        self.assertEqual(padded.cleaned.size, (1256, 1256))
        self.assertEqual(padded.info[5]["source_rect"], [414, 374, 514, 574])
        self.assertFalse(any(frame["is_empty"] for frame in padded.info))

    def test_single_mode_runs_as_a_one_cell_grid(self) -> None:
        """r11e, r19, S17: single images honour --fit-scale, --align and component cleanup, and strict QC
        rejects art cut off by the image border."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            single = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
            single.paste((40, 90, 150, 255), (10, 10, 60, 90))
            single.save(root / "single.png")
            boxes = []
            for fit in ("0.3", "1.0"):
                meta, out = process("--input", str(root / "single.png"), "--target", "asset", "--mode", "single",
                                    "--output-dir", str(root / f"fit-{fit}"), "--background-mode", "native_alpha",
                                    "--fit-scale", fit, "--align", "feet")
                with Image.open(out / "clean.png") as clean:
                    boxes.append(clean.getbbox())
            self.assertNotEqual(boxes[0], boxes[1])
            self.assertEqual(meta["single_size"], 256)
            self.assertEqual(boxes[1][3], 256)
            clipped = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
            clipped.paste((40, 90, 150, 255), (30, 20, 100, 80))
            clipped.putpixel((5, 95), (255, 255, 255, 255))
            clipped.save(root / "clipped.png")
            common = ("--input", str(root / "clipped.png"), "--target", "asset", "--mode", "single",
                      "--background-mode", "native_alpha", "--component-mode", "largest")
            message = process_error(*common, "--output-dir", str(root / "strict"), "--strict-qc")
            self.assertEqual(message, "QC failed: raw subjects touch a source-cell edge: [[0, 0]]")
            meta, _out = process(*common, "--output-dir", str(root / "loose"))
        self.assertEqual(meta["frames"][0]["subject_bbox"], [30, 20, 100, 80])
        self.assertEqual(meta["frames"][0]["component_count"], 2)

    def test_v1_profile_is_still_read(self) -> None:
        """S02 compatibility: a cfed170 version 1 profile applies, and its drift keeps the v1 metric."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_magenta_sheet(2, 2, 64).save(root / "sheet.png")
            legacy = {
                "version": 1, "name": "hero",
                "processing": {"cell_size": 96, "fit_scale": 0.8, "trim_border": 4, "edge_clean_depth": 3,
                               "align": "bottom", "shared_scale": False, "scale_strategy": "preserve",
                               "component_mode": "all", "component_padding": 0, "min_component_area": 1,
                               "edge_touch_margin": 0},
                "output_origin": [48.0, 87],
                "reference": {"target": "asset", "mode": "idle", "rows": 2, "cols": 2, "body_scale_mean": 0.3},
                "qc": {"max_body_scale_drift": 0.5},
            }
            self.assertEqual(contract_errors(legacy, "sprite", "scale_profile_v2", skill="generate2dsprite"), [])
            (root / "v1.json").write_text(json.dumps(legacy), encoding="utf-8")
            meta, _out = process("--input", str(root / "sheet.png"), "--target", "asset", "--mode", "idle",
                                 "--output-dir", str(root / "out"), "--scale-profile", str(root / "v1.json"))
        self.assertEqual((meta["scale_profile"]["version"], meta["cell_size"], meta["align"]), (1, 96, "feet"))
        expected = abs(meta["qc_summary"]["legacy_body_scale_mean"] / 0.3 - 1)
        self.assertAlmostEqual(meta["qc_summary"]["profile_body_scale_drift"], expected)
        self.assertEqual(meta["qc_config"]["max_profile_scale_drift"], 0.5)


class ContractTests(unittest.TestCase):
    """B01-T6: pipeline-meta.json is generate2dsprite.pipeline_meta.v2 with a common qaEnvelope."""

    def test_pipeline_meta_validates_as_v2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_magenta_sheet(2, 2, 64, fringe=True).save(root / "chroma.png")
            native = Image.new("RGBA", (128, 64), (0, 0, 0, 0))
            native.paste(BODY, (10, 8, 40, 60))
            native.paste(BODY, (80, 4, 110, 56))
            native.save(root / "native.png")
            runs = {
                "soft": ("--input", str(root / "chroma.png"), "--mode", "idle"),
                "hard": ("--input", str(root / "chroma.png"), "--mode", "idle", "--key-quality", "hard",
                         "--godot-world-height", "1"),
                "registered": ("--input", str(root / "native.png"), "--mode", "idle", "--rows", "1", "--cols", "2",
                               "--background-mode", "native_alpha", "--scale-strategy", "registered",
                               "--align", "feet", "--pad-to-grid"),
                "single": ("--input", str(root / "native.png"), "--mode", "single", "--background-mode",
                           "native_alpha"),
            }
            for name, argv in runs.items():
                with self.subTest(run=name):
                    meta, _out = process(*argv, "--target", "asset", "--output-dir", str(root / name))
                    self.assertEqual(meta["schema"], "generate2dsprite.pipeline_meta.v2")
                    self.assertEqual(contract_errors(meta, "sprite", "pipeline_meta_v2", skill="generate2dsprite"), [])
                    self.assertEqual(contract_errors(meta["qa"], "common", "qaEnvelope", skill="generate2dsprite"), [])
                    # The binary key leaves the fringe of the anti-aliased edge: key_ring_spill warns (review fix).
                    warned = [check["id"] for check in meta["qa"]["checks"] if check["status"] == "warn"]
                    self.assertEqual(warned, ["key_ring_spill"] if name == "hard" else [])
                    self.assertEqual(meta["qa"]["status"], "warn" if name == "hard" else "pass")
                    self.assertEqual({item["path"] for item in meta["qa"]["outputs"]} <= set(
                        path.name for path in (root / name).iterdir()), True)

    def test_empty_frame_source_rect_is_null(self) -> None:
        """An empty frame has no subject box: ``source_rect`` is null, which the integrated schema allows
        (handoff/B01 section 5, ``anyOf [box, null]``; per D33) while a box stays a 4-number array."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sheet = make_magenta_sheet(2, 2, 64)
            sheet.paste((255, 0, 255, 255), (64, 64, 128, 128))
            sheet.save(root / "sheet.png")
            meta, _out = process("--input", str(root / "sheet.png"), "--target", "asset", "--mode", "idle",
                                 "--output-dir", str(root / "out"))
        self.assertEqual(meta["empty_frames"], [[1, 1]])
        self.assertEqual(meta["qa"]["status"], "warn")
        self.assertIsNone(meta["frames"][3]["source_rect"])
        self.assertEqual(pipeline_meta_errors(meta), [])
        broken = copy.deepcopy(meta)
        broken["frames"][3]["source_rect"] = [0, 0, 1]
        self.assertTrue(pipeline_meta_errors(broken), "a source_rect that is not null must be a box")


class SoftRegionTests(unittest.TestCase):
    """D15: process keys through forge_matte.key_still, whose region matte (B01's former
    _local_soft_matte_regions, promoted with an exactness guard) mattes only the content regions of a
    sheet and gives the same bytes as one forge_matte.soft_matte call over the whole sheet."""

    def sheets(self) -> list[tuple[str, np.ndarray, str]]:
        rng = np.random.default_rng(11)
        accents = np.array(make_magenta_sheet(3, 3, 96, fringe=True))
        accents[30:40, 30:44, :3] = (180, 60, 200)   # purple design detail
        accents[140:150, 130:140, :3] = (255, 0, 255)  # enclosed key hole
        noisy = np.array(make_magenta_sheet(2, 3, 80, margin=4))
        noisy[..., :3] = np.clip(noisy[..., :3].astype(int) + rng.integers(-6, 7, noisy[..., :3].shape), 0, 255)
        translucent = np.array(make_magenta_sheet(2, 2, 72))
        translucent[:, :20, 3] = 0
        translucent[100:110, 30:60, 3] = 120
        green = np.array(make_magenta_sheet(2, 2, 64, key=(0, 255, 0), color=(200, 60, 160)))
        return [("magenta", accents, "magenta"), ("magenta", noisy, "magenta"),
                ("magenta", translucent, "magenta"), ("green", green, "green")]

    def test_region_matte_equals_whole_sheet_matte(self) -> None:
        fm = G.forge_matte
        for index, (declared, pixels, key_name) in enumerate(self.sheets()):
            key, _info = fm.estimate_key(pixels, key_name)
            for interior in (False, True):
                params = fm.KeyParams(**{**fm.STILL_KEY_PARAMS.to_dict(), "interior_despill": interior})
                with self.subTest(sheet=index, key=declared, interior_despill=interior):
                    np.testing.assert_array_equal(fm.soft_matte_regions(pixels, params, key),
                                                  fm.soft_matte(pixels, params, key))

    def test_process_soft_key_is_key_still_with_whole_sheet_bytes(self) -> None:
        """key_sheet makes one key_still call, which mattes by regions; the result is one whole-sheet soft
        matte with key_still's parameters. The private region copy is gone (D15, D30)."""
        fm = G.forge_matte
        self.assertFalse(hasattr(G, "_local_soft_matte_regions"))
        for index, (declared, pixels, key_name) in enumerate(self.sheets()):
            with self.subTest(sheet=index, key=declared), \
                 mock.patch.object(fm, "key_still", wraps=fm.key_still) as still, \
                 mock.patch.object(fm, "soft_matte_regions", wraps=fm.soft_matte_regions) as regions:
                keyed, info = G.key_sheet(Image.fromarray(pixels), quality="auto", key=key_name)
                self.assertEqual((still.call_count, regions.call_count), (1, 1))
                self.assertEqual((info["quality"], info["requested_quality"]), ("soft", "auto"))
                key, _estimate = fm.estimate_key(pixels, key_name)
                expected = fm.soft_matte(pixels, fm.KeyParams(**info["params"]), key)
                np.testing.assert_array_equal(np.asarray(keyed), expected)
                self.assertEqual(info["qa"], fm.matte_qa(expected, key))

    def test_hard_key_is_the_legacy_keyer_against_ff00ff(self) -> None:
        """--key-quality hard never estimates a backdrop: legacy_hard_key with the user's thresholds, QA against
        #FF00FF (the cfed170 bytes, S03)."""
        fm = G.forge_matte
        sheet = make_magenta_sheet(2, 2, 64, fringe=True)
        keyed, info = G.key_sheet(sheet, quality="hard", threshold=90, edge_threshold=140)
        expected = np.asarray(fm.legacy_hard_key(sheet, 90, 140))
        np.testing.assert_array_equal(np.asarray(keyed), expected)
        self.assertEqual((info["quality"], info["key"], info["key_estimate"]), ("hard", [255.0, 0.0, 255.0], None))
        self.assertEqual(info["thresholds"], {"threshold": 90, "edge_threshold": 140})
        self.assertEqual(info["qa"], fm.matte_qa(expected, (255, 0, 255)))
        auto_nearest, auto_info = G.key_sheet(sheet, quality="auto", resampler="nearest")
        np.testing.assert_array_equal(np.asarray(auto_nearest), np.asarray(fm.legacy_hard_key(sheet, 100, 150)))
        self.assertEqual((auto_info["quality"], auto_info["requested_quality"]), ("hard", "auto"))

    def test_regions_skip_the_empty_backdrop(self) -> None:
        sheet = np.array(make_magenta_sheet(3, 3, 256, margin=100))
        fm = G.forge_matte
        key, _info = fm.estimate_key(sheet, "magenta")
        areas = []
        original = fm.soft_matte

        def record(pixels, *args, **kwargs):
            areas.append(pixels.shape[0] * pixels.shape[1])
            return original(pixels, *args, **kwargs)

        with mock.patch.object(fm, "soft_matte", side_effect=record):
            fm.soft_matte_regions(sheet, fm.STILL_KEY_PARAMS, key)
        self.assertEqual(len(areas), 9)
        self.assertLess(sum(areas), 0.6 * sheet.shape[0] * sheet.shape[1])

        specks = np.zeros((512, 512, 4), np.uint8)
        specks[...] = (255, 0, 255, 255)
        for y in range(10, 512, 48):
            for x in range(10, 512, 48):
                specks[y:y + 4, x:x + 4, :3] = (40, 90, 150)  # 121 separate groups: one whole-sheet call
        areas.clear()
        with mock.patch.object(fm, "soft_matte", side_effect=record):
            result = fm.soft_matte_regions(specks, fm.STILL_KEY_PARAMS, key)
        self.assertEqual(areas, [512 * 512])
        np.testing.assert_array_equal(result, original(specks, fm.STILL_KEY_PARAMS, key))


if __name__ == "__main__":
    unittest.main()
