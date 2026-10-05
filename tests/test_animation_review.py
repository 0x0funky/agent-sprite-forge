import importlib.util
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
from PIL import Image

SPEC = importlib.util.spec_from_file_location("animation_review", Path(__file__).resolve().parents[1] / "skills/video2dsprite/scripts/animation_review.py")
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)


def make_frames(directory, count=12):
    directory.mkdir()
    for i in range(count):
        im = Image.new("RGBA", (24, 32))
        im.paste((40, 160, 220, 200), (4 + i % 4, 6, 12 + i % 4, 27 - i % 2))
        im.save(directory / f"frame-{i:03d}.png")


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
                self.assertEqual(frame["sha256"], R.signature(Path(frame["source"])))

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
                "sourceDirectory": "/frames", "candidates": [], "diagnostics": {}}
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


if __name__ == "__main__":
    unittest.main()
