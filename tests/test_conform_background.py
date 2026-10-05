"""Tests for conform_background.py: cover / ground-fit conforming and per-aspect subject crops (B12-T5)."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from forge_testutils import (assert_cli_help, assert_valid_contract, contract_errors, load_script, run_cli,
                             script_path)


CONFORM = load_script("generate2dmap", "conform_background")
SCRIPT = script_path("generate2dmap", "conform_background")
SKILL = "generate2dmap"

# handoff/B12-map-compose-parallax.md section 5 is integrated into shared/schemas/map.schema.json; the tests
# validate against the vendored generate2dmap copy.
def requested_contract_errors(document, name="conform_v1"):
    """Errors against the vendored generate2dmap schemas, which hold this module's section 5 requests."""
    return contract_errors(document, "map", name, skill=SKILL)


def conform_cli(*arguments):
    return run_cli([SCRIPT, *(str(argument) for argument in arguments)])


def conform_main(*arguments):
    """main() in-process (for error paths): returns (exit status, stdout, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = CONFORM.main([str(argument) for argument in arguments])
    return status, stdout.getvalue(), stderr.getvalue()


def painting(path, size=(1672, 941), ground_row=719, seed=0):
    """A host-sized painting (1672x941, as the image tool returned for a 1280x720 request) with sky above
    ``ground_row`` and grass from it down, plus texture so resampling differences would show."""
    width, height = size
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    pixels = np.zeros((height, width, 4), np.uint8)
    noise = rng.integers(0, 24, (height, width))
    pixels[..., 0] = (xx * 200 // width + noise).astype(np.uint8)
    pixels[..., 1] = np.where(yy >= ground_row, 170, 70) + noise // 2
    pixels[..., 2] = np.where(yy >= ground_row, 40, 210) - noise // 2
    pixels[..., 3] = 255
    Image.fromarray(pixels).save(path)
    return path


def edge_row(path, column_range=(200, 1000)):
    """Sub-pixel row where the blue channel crosses midway from sky to grass (row centres at r + 0.5)."""
    with Image.open(path) as image:
        blue = np.asarray(image.convert("RGB"))[:, column_range[0]:column_range[1], 2].astype(float).mean(axis=1)
    middle = (blue[:300].mean() + blue[-60:].mean()) / 2
    index = int(np.flatnonzero(blue < middle)[0])
    return index - 0.5 + (blue[index - 1] - middle) / (blue[index - 1] - blue[index])


class ConformTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = painting(self.root / "forest.png")

    def test_cover_matches_imageops_fit_exactly(self):
        """Acceptance: the cover output equals PIL ImageOps.fit pixel for pixel (max diff 0)."""
        with Image.open(self.source) as image:
            original = image.convert("RGBA")
        portrait = painting(self.root / "portrait.png", size=(600, 900))
        with Image.open(portrait) as image:
            tall = image.convert("RGBA")
        cases = [(self.source, original, "1280x720", (1280, 720), (0.5, 0.5)),
                 (self.source, original, "1280x720", (1280, 720), (0.2, 0.9)),
                 (portrait, tall, "960x540", (960, 540), (0.5, 0.3))]
        for index, (source, image, size_text, size, focus) in enumerate(cases):
            with self.subTest(case=index):
                out = self.root / f"cover-{index}"
                run = conform_cli("conform", "--input", source, "--size", size_text, "--focus",
                                  f"{focus[0]},{focus[1]}", "--output-dir", out)
                self.assertEqual(run.returncode, 0, run.stderr)
                expected = np.asarray(ImageOps.fit(image, size, Image.Resampling.LANCZOS, centering=focus)).astype(int)
                with Image.open(out / "background.png") as produced:
                    actual = np.asarray(produced.convert("RGBA")).astype(int)
                self.assertEqual(int(np.abs(actual - expected).max()), 0)
                record = json.loads((out / "conform.json").read_text(encoding="utf-8"))
                transform = CONFORM.cover_transform(image.size, size, focus)
                self.assertEqual(record["transform"]["src_rect"], list(transform.src_rect))
                self.assertEqual(record["transform"]["zoom_vs_cover"], 1.0)
                self.assertEqual(requested_contract_errors(record), [])
        record = json.loads((self.root / "cover-0" / "conform.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(record["transform"]["scale"], 0.765550239, places=9)
        self.assertEqual(record["transform"]["src_rect"], [0.0, 0.25, 1672.0, 940.75])

    def test_ground_fit_puts_the_floor_at_610_replica(self):
        """Acceptance (report v2 5.5 replica): ground-fit lands the painted ground row on y = 610."""
        out = self.root / "fit"
        run = conform_cli("conform", "--input", self.source, "--size", "1280x720", "--mode", "ground-fit",
                          "--ground-y", "718.6875", "--floor-y", "610", "--output-dir", out)
        self.assertEqual(run.returncode, 0, run.stderr)
        record = json.loads((out / "conform.json").read_text(encoding="utf-8"))
        transform = record["transform"]
        self.assertAlmostEqual(transform["scale"], 0.848769458, places=9)
        np.testing.assert_allclose(transform["src_rect"], [81.967213, 0.0, 1590.032787, 848.286885], atol=1e-6)
        self.assertAlmostEqual(transform["zoom_vs_cover"], 1.1087, places=4)
        self.assertEqual(transform["ground"], {"source_y": 718.6875, "output_y": 610.0})
        checks = {check["id"]: check for check in record["qa"]["checks"]}
        self.assertEqual((checks["ground row on floor"]["status"], checks["ground row on floor"]["value"]),
                         ("pass", 610.0))
        self.assertEqual(requested_contract_errors(record), [])
        # The painted sky/grass boundary is at source row 719; fitting it to 610 puts the edge on 610 +- 0.25.
        run = conform_cli("conform", "--input", self.source, "--size", "1280x720", "--mode", "ground-fit",
                          "--ground-y", "719", "--floor-y", "610", "--output-dir", self.root / "fit-edge")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertAlmostEqual(edge_row(self.root / "fit-edge" / "background.png"), 610.0, delta=0.25)
        run = conform_cli("conform", "--input", self.source, "--size", "1280x720", "--output-dir",
                          self.root / "cover")
        self.assertAlmostEqual(edge_row(self.root / "cover" / "background.png"), 719 * 1280 / 1672 - 0.25 * 1280 / 1672,
                               delta=0.25)

    def test_transform_maps_points_and_subjects_both_ways(self):
        transform = CONFORM.ground_fit_transform((1672, 941), (1280, 720), 718.6875, 610)
        self.assertAlmostEqual(transform.to_out(0, 718.6875)[1], 610.0, places=9)
        for point in ((0.0, 0.0), (812.5, 333.25), (1590.032787, 848.286885)):
            back = transform.to_src(*transform.to_out(*point))
            self.assertAlmostEqual(back[0], point[0], places=9)
            self.assertAlmostEqual(back[1], point[1], places=9)
        run = conform_cli("conform", "--input", self.source, "--size", "1280x720", "--mode", "ground-fit",
                          "--ground-y", "718.6875", "--floor-y", "610", "--subject", "well=700,500,820,700",
                          "--output-dir", self.root / "out")
        self.assertEqual(run.returncode, 0, run.stderr)
        record = json.loads((self.root / "out" / "conform.json").read_text(encoding="utf-8"))
        subject = record["subjects"][0]
        np.testing.assert_allclose(subject["output_box"], transform.box_to_out((700, 500, 820, 700)), atol=1e-6)
        self.assertEqual(record["qa"]["status"], "pass")
        self.assertEqual(record["source"]["path"], "../forest.png")
        self.assertEqual(record["output"]["path"], "background.png")

    def test_bad_ground_and_mode_arguments_are_errors(self):
        cases = [["--mode", "ground-fit", "--ground-y", "0", "--floor-y", "610"],
                 ["--mode", "ground-fit", "--ground-y", "500", "--floor-y", "720"],
                 ["--mode", "ground-fit", "--ground-y", "500"],
                 ["--ground-y", "500", "--floor-y", "600"],
                 ["--subject", "crest=10,10,5,20"],
                 ["--subject", "crest=0,0,2000,20"],
                 ["--aspects", "16:9"]]
        for index, extra in enumerate(cases):
            with self.subTest(case=extra):
                status, _, stderr = conform_main("conform", "--input", self.source, "--size", "1280x720",
                                                 "--output-dir", self.root / f"bad-{index}", *extra)
                self.assertEqual(status, 1)
                self.assertTrue(stderr.startswith("error: "), stderr)
                self.assertFalse((self.root / f"bad-{index}").exists())

    def test_help_works_under_cp1252(self):
        assert_cli_help(SKILL, "conform_background")
        for verb in ("conform", "validate-crops"):
            run = run_cli([SCRIPT, verb, "--help"], "cp1252")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(run.stdout.isascii())

    def test_refuses_an_existing_output_dir(self):
        out = self.root / "taken"
        out.mkdir()
        (out / "keep.txt").write_text("keep", encoding="utf-8")
        run = conform_cli("conform", "--input", self.source, "--size", "1280x720", "--output-dir", out)
        self.assertEqual(run.returncode, 1)
        self.assertIn("Refusing to replace existing output", run.stderr)
        self.assertEqual(sorted(path.name for path in out.iterdir()), ["keep.txt"])

    def test_strict_subject_failure_publishes_nothing(self):
        """A subject near the left edge is cut when ground-fit zooms in; --strict publishes nothing."""
        common = ["conform", "--input", self.source, "--size", "1280x720", "--mode", "ground-fit", "--ground-y",
                  "718.6875", "--floor-y", "610", "--subject", "crest=40,100,200,300"]
        run = conform_cli(*common, "--strict", "--output-dir", self.root / "strict")
        self.assertEqual(run.returncode, 1)
        self.assertIn("strict check failed (subjects in output)", run.stderr)
        self.assertFalse((self.root / "strict").exists())
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.startswith(".")], [])
        run = conform_cli(*common, "--output-dir", self.root / "lenient")
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertEqual(summary["qa_status"], "fail")
        record = json.loads((self.root / "lenient" / "conform.json").read_text(encoding="utf-8"))
        cut = record["qa"]["checks"][0]
        self.assertEqual((cut["id"], cut["value"]["cut"]), ("subjects in output", ["crest"]))
        self.assertLess(cut["value"]["subjects"]["crest"]["left"], 0)


class ValidateCropsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plate = painting(self.root / "plate.png", size=(1280, 720), ground_row=560)

    def test_subject_margins_per_aspect(self):
        """16:9 shows the whole plate; 19.5:9 cuts the top of the crest; 4:3 cuts the sign on the left."""
        out = self.root / "crops"
        run = conform_cli("validate-crops", "--input", self.plate, "--subject", "crest=560,40,720,200",
                          "--subject", "sign=10,500,90,560", "--subject", "hero=600,400,680,560",
                          "--min-margin", "4", "--output-dir", out)
        self.assertEqual(run.returncode, 1)
        summary = json.loads(run.stdout)
        self.assertEqual((summary["status"], summary["cut"]),
                         ("fail", ["crop 19.5:9 focus 0.5,0.5", "crop 4:3 focus 0.5,0.5"]))
        report = json.loads((out / "crops-qa.json").read_text(encoding="utf-8"))
        assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
        checks = {check["value"]["aspect"]: check for check in report["checks"]}
        self.assertEqual(checks["16:9"]["value"]["window"], [0.0, 0.0, 1280.0, 720.0])
        self.assertEqual(checks["16:9"]["value"]["subjects"]["sign"]["left"], 10.0)
        window = checks["19.5:9"]["value"]["window"]
        self.assertAlmostEqual(window[3] - window[1], 1280 * 9 / 19.5, places=5)
        self.assertEqual(checks["19.5:9"]["value"]["cut"], ["crest"])
        self.assertAlmostEqual(checks["19.5:9"]["value"]["subjects"]["crest"]["top"], 40 - (720 - 1280 * 9 / 19.5) / 2,
                               places=5)
        self.assertEqual((checks["4:3"]["value"]["window"], checks["4:3"]["value"]["cut"]),
                         ([160.0, 0.0, 1120.0, 720.0], ["sign"]))
        self.assertEqual(checks["4:3"]["value"]["subjects"]["sign"]["visible_fraction"], 0.0)
        self.assertEqual(report["outputs"][0]["path"], "crops-overlay.png")
        with Image.open(out / "crops-overlay.png") as overlay:
            self.assertEqual(overlay.size, (1280, 720))

    def test_focus_list_and_plate_pan_zoom(self):
        out = self.root / "pan"
        run = conform_cli("validate-crops", "--input", self.plate, "--aspects", "16:9", "--zoom", "1.12",
                          "--focus", "0,0.5", "--focus", "1,0.5", "--subject", "centre=600,300,680,380",
                          "--subject", "edge=1200,300,1270,380", "--output-dir", out)
        self.assertEqual(run.returncode, 1)
        report = json.loads((out / "crops-qa.json").read_text(encoding="utf-8"))
        left, right = report["checks"]
        self.assertEqual(left["value"]["window"][0], 0.0)
        self.assertAlmostEqual(right["value"]["window"][2], 1280.0, places=6)
        self.assertAlmostEqual(left["value"]["window"][2], 1280 / 1.12, places=5)
        self.assertEqual((left["value"]["cut"], right["value"]["cut"]), (["edge"], []))

    def test_crop_windows_match_the_cover_crop(self):
        for size, aspect, out_size, focus in (((1280, 720), (4, 3), (400, 300), (0.5, 0.5)),
                                              ((1000, 1500), (16, 9), (1600, 900), (0.3, 0.7)),
                                              ((900, 400), (19.5, 9), (390, 180), (1.0, 0.0))):
            with self.subTest(size=size, aspect=aspect):
                window = CONFORM._local_cover_window(size, aspect, 1.0, focus)
                expected = CONFORM.cover_transform(size, out_size, focus).src_rect
                np.testing.assert_allclose(window, expected, rtol=1e-12, atol=1e-9)

    def test_strict_failure_publishes_nothing_and_existing_dirs_are_refused(self):
        run = conform_cli("validate-crops", "--input", self.plate, "--subject", "sign=10,500,90,560", "--strict",
                          "--output-dir", self.root / "strict")
        self.assertEqual(run.returncode, 1)
        self.assertIn("strict check failed", run.stderr)
        self.assertFalse((self.root / "strict").exists())
        (self.root / "taken").mkdir()
        run = conform_cli("validate-crops", "--input", self.plate, "--subject", "hero=600,400,680,560",
                          "--output-dir", self.root / "taken")
        self.assertEqual(run.returncode, 1)
        self.assertEqual(list((self.root / "taken").iterdir()), [])
        run = conform_cli("validate-crops", "--input", self.plate, "--subject", "hero=600,400,680,560",
                          "--output-dir", self.root / "fine")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "pass")

    def test_subjects_file_and_input_errors(self):
        (self.root / "subjects.json").write_text(json.dumps(
            {"subjects": [{"id": "crest", "box": [560, 40, 720, 200]}]}), encoding="utf-8")
        status, stdout, stderr = conform_main("validate-crops", "--input", self.plate, "--subjects",
                                              self.root / "subjects.json", "--aspects", "16:9", "--output-dir",
                                              self.root / "ok")
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "pass")
        for extra in (["--subject", "crest=560,40,720,200", "--subject", "crest=1,1,2,2"], ["--subject", "x"],
                      ["--subject", "crest=1,1,2,2", "--aspects", "wide"],
                      ["--subject", "crest=1,1,2,2", "--zoom", "0.5"],
                      ["--aspects", "16:9"]):
            with self.subTest(extra=extra):
                status, _, stderr = conform_main("validate-crops", "--input", self.plate, "--output-dir",
                                                 self.root / "bad", *extra)
                self.assertEqual(status, 1)
                self.assertTrue(stderr.startswith("error: "), stderr)
                self.assertFalse((self.root / "bad").exists())
        with self.assertRaises(ValueError):
            CONFORM.parse_subjects(["crest=1,2,3"], None)


if __name__ == "__main__":
    unittest.main()
