import importlib.util
import contextlib
import io
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from forge_testutils import assert_cli_help, assert_valid_contract

SPEC = importlib.util.spec_from_file_location("animation_review", Path(__file__).resolve().parents[1] / "skills/video2dsprite/scripts/animation_review.py")
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)


def make_frames(directory, count=12):
    directory.mkdir()
    for i in range(count):
        im = Image.new("RGBA", (24, 32))
        im.paste((40, 160, 220, 200), (4 + i % 4, 6, 12 + i % 4, 27 - i % 2))
        im.save(directory / f"frame-{i:03d}.png")


def make_runner(directory, count=96, period=16):
    """A side-view runner with distinguishable legs and a 16-frame stride."""
    directory.mkdir()
    for t in range(count):
        image = Image.new("RGBA", (96, 96))
        draw = ImageDraw.Draw(image)
        phase = 2 * math.pi * t / period
        hip = 58 + 2 * math.cos(2 * phase)
        draw.rectangle((40, hip - 30, 56, hip), fill=(36, 128, 126, 255))
        draw.ellipse((40, hip - 46, 56, hip - 30), fill=(240, 200, 170, 255))
        for sign, color in ((1, (40, 52, 84, 255)), (-1, (90, 40, 30, 255))):
            angle = 0.6 * sign * math.sin(phase)
            foot = (48 + 30 * math.sin(angle), hip + 30 * math.cos(angle))
            draw.line((48, hip, *foot), fill=color, width=6)
            draw.ellipse((foot[0] - 4, foot[1] - 3, foot[0] + 6, foot[1] + 3), fill=(110, 70, 40, 255))
        image.save(directory / f"run_{t:03d}.png")


class ReviewTests(unittest.TestCase):
    def test_candidates_recur_without_fabricating_more_frames(self):
        rng = np.random.default_rng(123)
        cycle = rng.random((8, 4, 4, 4), dtype=np.float32)
        result = R.candidates(np.concatenate([cycle] * 4), 6, 12)
        self.assertTrue(result)
        self.assertEqual(result[0]["frameCount"], 8)
        self.assertEqual(result[0]["recurrenceMAE"], 0)
        self.assertEqual(R.candidates(np.zeros((32, 4, 4, 4)), 6, 12), [])

    def test_insufficient_repetition_is_not_auto_approved(self):
        self.assertEqual(R.candidates(np.ones((8, 4, 4, 4)), 6, 12), [])
        with self.assertRaises(ValueError):
            R.candidates(np.ones((8, 4, 4, 4)), 8, 4)

    def test_arbitrary_moving_windows_are_not_recurrence_candidates(self):
        drift = np.arange(16, dtype=np.float32).reshape(16, 1, 1, 1) / 16
        self.assertEqual(R.candidates(drift, 3, 7), [])

    def test_byte_preserved_cut_keeps_lift_and_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src")
            originals = sorted((root / "src").glob("*.png"))
            result = R.cut(root / "src", root / "cut", 2, 6, 24)
            self.assertEqual(result["durationSeconds"], 4/24)
            for i, path in enumerate(sorted((root / "cut").glob("*.png"))):
                self.assertEqual(path.read_bytes(), originals[i+2].read_bytes())
            with self.assertRaisesRegex(ValueError, "already exists"):
                R.cut(root / "src", root / "cut", 2, 6, 24)

    def test_hash_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src")
            with self.assertRaisesRegex(ValueError, "hashes"):
                R.cut(root / "src", root / "out", 0, 2, 24, ["wrong"] * 2)
            self.assertFalse((root / "out").exists())

    def test_source_change_during_copy_fails_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src", 4)
            copyfile = shutil.copyfile
            def changed_copy(source, destination):
                with Image.open(source) as image:
                    image.putpixel((0, 0), (0, 0, 0, 255))
                    image.save(source)
                return copyfile(source, destination)
            with mock.patch.object(R.shutil, "copyfile", side_effect=changed_copy):
                with self.assertRaisesRegex(ValueError, "changed during copy"):
                    R.cut(root / "src", root / "out", 0, 2, 24)
            self.assertFalse((root / "out").exists())

    def test_source_change_during_review_fails_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src", 4)
            features = R.features
            def changed_features(paths):
                result = features(paths)
                with Image.open(paths[0]) as image:
                    image.putpixel((0, 0), (0, 0, 0, 255))
                    image.save(paths[0])
                return result
            with mock.patch.object(R, "features", side_effect=changed_features):
                with self.assertRaisesRegex(ValueError, "changed during review"):
                    R.review(root / "src", root / "out", 24, 2, 3)
            self.assertFalse((root / "out").exists())

    def test_review_local_preview_provenance_and_visible_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src")
            first = root / "src/frame-000.png"
            second = root / "src/frame-001.png"
            with Image.open(first) as im:
                im.putpixel((0, 0), (255, 0, 0, 0))
                im.save(second)
            report = R.review(root / "src", root / "review", 24, 3, 5, 96)
            self.assertEqual(report["status"], "needs-visual-review")
            self.assertIn(1, report["diagnostics"]["adjacentExactDuplicates"])
            self.assertEqual(report["sourceSize"], [24, 32])
            self.assertEqual(len(report["frames"]), 12)
            self.assertTrue((root / "review/index.html").is_file())
            for frame in report["frames"]:
                self.assertTrue((root / "review" / frame["preview"]).is_file())
                self.assertEqual(frame["sha256"], R.signature(root / "review" / frame["source"]))

    def test_review_and_v1_cut_write_no_absolute_paths(self):
        """r2-conventions finding 5: review.json (and the index.html that embeds it) and the v1 cut's
        selection.json held absolute paths. sourceDirectory and frames[].source are relative to the output
        folder now, or only the name when there is no relative route (another drive); the sha256 values bind them."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src")
            names = sorted(p.name for p in (root / "src").glob("*.png"))
            report = R.review(root / "src", root / "review", 24, 3, 5, 96)
            self.assertEqual((report["sourceDirectory"], report["sourceDirectoryName"]), ("../src", "src"))
            self.assertEqual([frame["source"] for frame in report["frames"]], [f"../src/{name}" for name in names])
            self.assertEqual(R.cut(root / "src", root / "cut", 2, 6, 24)["sourceDirectory"], "../src")
            absolute = {str(root.resolve()), root.resolve().as_posix(), json.dumps(str(root.resolve()))[1:-1]}
            for path in (root / "review" / "review.json", root / "review" / "index.html", root / "cut" / "selection.json"):
                text = path.read_text(encoding="utf-8")
                self.assertFalse([value for value in absolute if value in text], path.name)
            off_drive = lambda path, base: Path(path).resolve().as_posix()  # what portable_path gives across drives
            with mock.patch.object(R.forge_core, "portable_path", off_drive):
                report = R.review(root / "src", root / "review-2", 24, 3, 5, 96)
                cut = R.cut(root / "src", root / "cut-2", 2, 6, 24)
            self.assertEqual((report["sourceDirectory"], cut["sourceDirectory"]), ("src", "src"))
            self.assertEqual([frame["source"] for frame in report["frames"]], names)

    def test_v1_selections_from_the_page_and_old_absolute_files_both_cut(self):
        """r2-conventions finding 5: the page saves sourceDirectory as the frames folder's name (a downloaded file
        has no known location; the hashes bind it) and cut resolves v1 like v2 (gait_loop.resolve_directory):
        relative to the selection file, or by name. Old v1 files with an absolute path still cut."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src")
            report = R.review(root / "src", root / "review", 24, 3, 5, 96)
            page = {"schema": "forge-frame-selection/v1", "sourceDirectory": report["sourceDirectoryName"],
                    "start": 1, "endExclusive": 4, "fps": 24,
                    "sourceHashes": [frame["sha256"] for frame in report["frames"][1:4]],
                    "status": "selected-needs-visual-review"}
            downloads = root / "Downloads"
            downloads.mkdir()
            cases = [(downloads, page), (root, {**page, "sourceDirectory": str((root / "src").resolve())}),
                     (downloads, {**page, "sourceDirectory": "../src"})]
            for index, (folder, selection) in enumerate(cases):
                file = folder / f"selection-{index}.json"
                file.write_text(json.dumps(selection), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(R.main(["cut", "--frames-dir", str(root / "src"), "--out-dir",
                                             str(root / f"cut-{index}"), "--selection", str(file)]), 0)
                self.assertEqual(len(list((root / f"cut-{index}").glob("frame-*.png"))), 3)
            wrong = downloads / "wrong.json"
            wrong.write_text(json.dumps({**page, "sourceDirectory": "other"}), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                R.main(["cut", "--frames-dir", str(root / "src"), "--out-dir", str(root / "cut-wrong"),
                        "--selection", str(wrong)])
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("source directory mismatch", stderr.getvalue())
            self.assertFalse((root / "cut-wrong").exists())

    def test_review_previews_zero_rgb_under_alpha_0(self):
        """r2-conventions finding 4: the frames/frame-NNNN.png previews (LANCZOS thumbnails) kept colour under
        alpha 0; they are written with forge_core.save_png now (Appendix D)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            y, x = np.mgrid[:256, :256]
            for i in range(6):  # a dark body with a faint bright rim, RGB zero under alpha 0
                pixels = np.zeros((256, 256, 4), np.uint8)
                inside = (x - 128 - i) ** 2 / 70 ** 2 + (y - 128) ** 2 / 100 ** 2 <= 1
                rim = ((x - 128 - i) ** 2 / 74 ** 2 + (y - 128) ** 2 / 104 ** 2 <= 1) & ~inside
                pixels[inside] = (20, 24, 32, 255)
                pixels[rim] = (250, 240, 200, 24)
                Image.fromarray(pixels, "RGBA").save(root / "src" / f"frame-{i:03d}.png")

            def coloured(image):
                pixels = np.asarray(image.convert("RGBA"))
                return int(((pixels[..., 3] == 0) & pixels[..., :3].any(axis=2)).sum())

            with Image.open(root / "src" / "frame-000.png") as image:
                image.thumbnail((192, 192), Image.Resampling.LANCZOS)
                self.assertGreater(coloured(image), 0)  # the thumbnail itself leaves colour behind
            report = R.review(root / "src", root / "review", 24, 2, 3)
            for frame in report["frames"]:
                with Image.open(root / "review" / frame["preview"]) as preview:
                    self.assertEqual((preview.size, coloured(preview)), ((192, 192), 0), frame["preview"])

    def test_selection_cli_rejects_changed_or_wrong_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src")
            selected = sorted((root / "src").glob("*.png"))[1:4]
            selection = {"schema": "forge-frame-selection/v1", "sourceDirectory": str((root / "src").resolve()),
                         "start": 1, "endExclusive": 4, "fps": 24,
                         "sourceHashes": [R.signature(p) for p in selected]}
            (root / "selection.json").write_text(json.dumps(selection), encoding="utf-8")
            self.assertEqual(R.main(["cut", "--frames-dir", str(root / "src"), "--out-dir", str(root / "out"), "--selection", str(root / "selection.json")]), 0)
            with Image.open(selected[0]) as im:
                im.putpixel((1, 1), (0, 0, 0, 255))
                im.save(selected[0])
            with self.assertRaises(SystemExit) as caught:
                R.main(["cut", "--frames-dir", str(root / "src"), "--out-dir", str(root / "invalid"), "--selection", str(root / "selection.json")])
            self.assertEqual(caught.exception.code, 1)
            self.assertFalse((root / "invalid").exists())

    def test_malformed_selection_cannot_bypass_hashes_or_crash_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src", 4)
            paths = sorted((root / "src").glob("*.png"))[:2]
            valid = {"schema": "forge-frame-selection/v1", "sourceDirectory": str((root / "src").resolve()),
                     "start": 0, "endExclusive": 2, "fps": 24,
                     "sourceHashes": [R.signature(path) for path in paths]}
            malformed = [None, [], 3, "selection",
                         {**valid, "sourceHashes": None}, {**valid, "sourceHashes": "none"},
                         {**valid, "sourceHashes": [None, None]}, {**valid, "sourceHashes": []},
                         {**valid, "start": False}, {**valid, "start": 0.5},
                         {**valid, "endExclusive": True}, {**valid, "fps": True},
                         {**valid, "fps": "24"}, {**valid, "sourceDirectory": ""}]
            for index, selection in enumerate(malformed):
                with self.subTest(selection=selection):
                    file = root / "selection.json"
                    file.write_text(json.dumps(selection), encoding="utf-8")
                    output = root / f"out-{index}"
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                        R.main(["cut", "--frames-dir", str(root / "src"), "--out-dir", str(output),
                                "--selection", str(file)])
                    self.assertEqual(caught.exception.code, 1)
                    self.assertTrue(stderr.getvalue().startswith("error:"))
                    self.assertFalse(output.exists())

    @unittest.skipUnless(shutil.which("node"), "Node.js required to exercise the actual reviewer JavaScript")
    def test_ui_uses_source_timing_and_integer_selected_interval(self):
        data = {"frames": [{"preview": f"frame-{i}.png", "sha256": "a" * 64, "bounds": None} for i in range(8)],
                "fps": 20, "sourceSize": [48, 64], "previewMaxSide": 192,
                "sourceDirectory": "../frames", "sourceDirectoryName": "frames", "candidates": [], "diagnostics": {}}
        script = R.HTML.split("<script>", 1)[1].split("</script>", 1)[0].replace("__DATA__", json.dumps(data))
        harness = r'''
const vm = require('node:vm');
let clock=0, callback;
const elements={};
const drawing={fillRect(){},drawImage(image){if(!image)throw Error('invalid image index')},beginPath(){},moveTo(){},lineTo(){},stroke(){},strokeRect(){}};
function element(id){return elements[id] ||= {value:id==='speed'?'1':id==='back'?'light':'',checked:false,append(){},getContext(){return drawing}}}
const sandbox={document:{getElementById:element,createElement:()=>({append(){}})},
  Image:class{decode(){return Promise.resolve()}},performance:{now:()=>clock},requestAnimationFrame:fn=>callback=fn};
const context=vm.createContext(sandbox);
vm.runInContext(SCRIPT,context);
setImmediate(()=>{
  const read=()=>vm.runInContext('index',context);
  const tick=time=>{clock=time;callback(time);return read()};
  const results={delayed1x:tick(250)};
  element('speed').value='0.5';results.halfSpeed=tick(450);
  element('play').onclick();results.paused=tick(1000);
  element('play').onclick();results.halfStep=tick(1050);results.fullStep=tick(1100);
  element('start').value='1.7';element('end').value='5.8';element('start').onchange();
  results.selection=vm.runInContext('selection()',context);
  element('prev').onclick();results.previousWrap=read();element('next').onclick();results.nextWrap=read();
  element('scrub').value=7;element('scrub').oninput();element('play').onclick();results.resumeInsideRange=read();
  console.log(JSON.stringify(results));
});
'''.replace("SCRIPT", json.dumps(script))
        completed = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, check=True, timeout=10)
        result = json.loads(completed.stdout)
        self.assertEqual([result[k] for k in ["delayed1x", "halfSpeed", "paused", "halfStep", "fullStep"]], [5, 7, 7, 7, 0])
        self.assertEqual((result["selection"]["start"], result["selection"]["endExclusive"]), (1, 5))
        self.assertEqual(len(result["selection"]["sourceHashes"]), 4)
        # r2-conventions finding 5: the saved selection names the frames folder, not a review-relative path
        self.assertEqual(result["selection"]["sourceDirectory"], "frames")
        self.assertEqual([result[k] for k in ["previousWrap", "nextWrap", "resumeInsideRange"]], [4, 1, 1])

    def test_mixed_canvas_and_animated_png_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src", 2)
            Image.new("RGBA", (30, 30)).save(root / "src/frame-001.png")
            with self.assertRaisesRegex(ValueError, "common source"):
                R.sources(root / "src")

    def test_invalid_time_and_ranges_publish_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src", 2)
            for fps in [0, -1, float("nan"), float("inf")]:
                with self.subTest(fps=fps), self.assertRaises(ValueError):
                    R.cut(root / "src", root / "cut", 0, 2, fps)
            for a, b in [(-1, 2), (2, 2), (0, 3)]:
                with self.subTest(interval=(a, b)), self.assertRaises(ValueError):
                    R.cut(root / "src", root / "cut", a, b, 24)
            self.assertFalse((root / "cut").exists())

    def test_help_is_ascii_under_cp1252_and_cp950(self):
        assert_cli_help("video2dsprite", "animation_review")


class SelectTests(unittest.TestCase):
    """B07-T3: select classifies candidates instead of listing raw recurrence windows."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        make_runner(cls.root / "src")
        cls.out = cls.root / "select"
        with contextlib.redirect_stdout(io.StringIO()):
            cls.code = R.main(["select", "--frames-dir", str(cls.root / "src"), "--fps", "24",
                               "--output-dir", str(cls.out)])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_review_lists_rejections_with_reasons(self):
        """report v2 P0-4: invalid helper candidates are listed as rejected, each with its reasons."""
        self.assertEqual(self.code, 0)
        report = json.loads((self.out / "candidates.json").read_text(encoding="utf-8"))
        rejected = [c for c in report["candidates"] if c["class"] == "rejected"]
        self.assertTrue(rejected)
        for candidate in rejected:
            self.assertTrue(candidate["reasons"] and all(isinstance(r, str) and r for r in candidate["reasons"]))
        half = [c for c in rejected if c["frameCount"] == 8]
        self.assertTrue(half and half[0]["reasons"][0].startswith("half stride"))
        classes = {c["class"] for c in report["candidates"]}
        self.assertEqual(classes, {"valid-1-cycle", "valid-2-cycle", "rejected"})
        self.assertEqual(report["counts"]["rejected"], len(rejected))
        self.assertTrue(any(c["source"] == "recurrence" for c in rejected))
        recommendation = report["recommendation"]
        self.assertIn(recommendation["endExclusive"] - recommendation["start"], (15, 16, 17))
        assert_valid_contract(report["qa"], "common", "qaEnvelope", skill="video2dsprite")
        selection = json.loads((self.out / "selection.json").read_text(encoding="utf-8"))
        assert_valid_contract(selection, "video", "frame_selection_v2", skill="video2dsprite")
        self.assertEqual(selection["status"], "selected-needs-visual-review")

    def test_selected_loop_cuts_byte_preserved(self):
        selection = json.loads((self.out / "selection.json").read_text(encoding="utf-8"))
        cut = self.root / "cut"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(R.main(["cut", "--frames-dir", str(self.root / "src"), "--selection",
                                     str(self.out / "selection.json"), "--out-dir", str(cut)]), 0)
        sources = sorted((self.root / "src").glob("*.png"))
        copies = sorted(cut.glob("*.png"))
        self.assertEqual([p.read_bytes() for p in copies], [sources[i].read_bytes() for i in selection["sourceIndices"]])

    def test_select_refuses_existing_output_and_publishes_nothing_on_failure(self):
        for argv, output in ((["--frames-dir", str(self.root / "src")], self.out),
                             (["--frames-dir", str(self.root / "still")], self.root / "nothing")):
            if "still" in argv[1] and not (self.root / "still").exists():
                (self.root / "still").mkdir()
                for i in range(12):
                    Image.new("RGBA", (24, 32), (40, 160, 220, 255)).save(self.root / "still" / f"s-{i:02d}.png")
            before = sorted(p.name for p in output.iterdir()) if output.exists() else None
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                R.main(["select", *argv, "--fps", "24", "--output-dir", str(output)])
            self.assertEqual(caught.exception.code, 1)
            self.assertTrue(stderr.getvalue().startswith("error:"))
            self.assertEqual(sorted(p.name for p in output.iterdir()) if output.exists() else None, before)
        self.assertFalse([p for p in self.root.iterdir() if ".stage-" in p.name])


class CutV2Tests(unittest.TestCase):
    """B07-T3: cut accepts forge-frame-selection v1 and v2."""

    def write_selection(self, root, **changes):
        paths = sorted((root / "src").glob("*.png"))
        selection = {"schema": "forge-frame-selection/v2", "sourceDirectory": "src",
                     "sourceHashes": [R.signature(p) for p in paths[2:6]], "start": 2, "endExclusive": 6,
                     "sourceIndices": [2, 3, 3, 5, 4], "durations_ms": [40, 40, 80, 40, 40], "loopPolicy": "oneshot",
                     "events": [{"name": "hit", "atMs": 80, "frame": 2}], "impactMs": 80,
                     "status": "selected-needs-visual-review", "method": "hand-made"}
        selection.update(changes)
        (root / "selection.json").write_text(json.dumps(selection), encoding="utf-8")
        return paths

    def cut(self, root, frames, selection, out):
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            try:
                return R.main(["cut", "--frames-dir", str(frames), "--selection", str(selection),
                               "--out-dir", str(out)]), stderr.getvalue()
            except SystemExit as exit_:
                return exit_.code, stderr.getvalue()

    def test_cut_accepts_v2_selection_in_played_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src", 8)
            paths = self.write_selection(root)
            self.assertEqual(self.cut(root, root / "src", root / "selection.json", root / "cut")[0], 0)
            copies = sorted((root / "cut").glob("*.png"))
            self.assertEqual([p.read_bytes() for p in copies], [paths[i].read_bytes() for i in (2, 3, 3, 5, 4)])
            rebased = json.loads((root / "cut" / "selection.json").read_text(encoding="utf-8"))
            assert_valid_contract(rebased, "video", "frame_selection_v2", skill="video2dsprite")
            self.assertEqual(rebased["sourceIndices"], [0, 1, 2, 3, 4])
            self.assertEqual(rebased["origin"]["sourceIndices"], [2, 3, 3, 5, 4])
            self.assertEqual((rebased["impactMs"], rebased["durations_ms"]), (80, [40, 40, 80, 40, 40]))
            self.assertEqual(rebased["sourceDirectory"], ".")
            # the cut folder describes itself: it can be cut again
            self.assertEqual(self.cut(root, root / "cut", root / "cut" / "selection.json", root / "again")[0], 0)
            self.assertEqual([p.read_bytes() for p in sorted((root / "again").glob("*.png"))],
                             [p.read_bytes() for p in copies])

    def test_cut_v2_refuses_changed_frames_wrong_directory_and_bad_indices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_frames(root / "src", 8)
            cases = [({"sourceDirectory": "elsewhere"}, "directory"),
                     ({"sourceIndices": [2, 6, 3, 5, 4]}, "sourceIndices"),
                     ({"durations_ms": [40, 0, 80, 40, 40]}, "durations_ms")]
            for index, (changes, message) in enumerate(cases):
                with self.subTest(changes=changes):
                    self.write_selection(root, **changes)
                    code, error = self.cut(root, root / "src", root / "selection.json", root / f"out-{index}")
                    self.assertEqual(code, 1)
                    self.assertIn(message, error)
                    self.assertFalse((root / f"out-{index}").exists())
            paths = self.write_selection(root)
            with Image.open(paths[3]) as image:
                image.putpixel((0, 0), (1, 2, 3, 255))
                image.save(paths[3])
            code, error = self.cut(root, root / "src", root / "selection.json", root / "changed")
            self.assertEqual(code, 1)
            self.assertIn("hashes no longer match", error)
            self.assertFalse((root / "changed").exists())


class IntegrationTests(unittest.TestCase):
    """D27: an unexpected exception is one 'error: internal error (...)' line and exit 1 (SystemExit, as the
    review and cut verbs always exited)."""

    def test_internal_error_is_one_line(self):
        stderr = io.StringIO()
        with mock.patch.object(R, "review", side_effect=ZeroDivisionError("boom")), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            R.main(["review", "--frames-dir", "src", "--out-dir", "out", "--fps", "24"])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(stderr.getvalue().strip(), "error: internal error (ZeroDivisionError: boom)")


if __name__ == "__main__":
    unittest.main()
