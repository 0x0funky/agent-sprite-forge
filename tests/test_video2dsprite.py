"""Tests for video2dsprite.py: registration, the legacy keyer API, previews, CLI conventions and triage (B05).

The 13 engine_export tests that used to live here are in tests/test_engine_export.py (A0-T3, B05-T1).
Keying tests are in tests/test_video2dsprite_matte.py.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (assert_cli_help, assert_valid_contract, load_script, require_ffmpeg, run_cli,
                             script_path)

V = load_script("video2dsprite", "video2dsprite")
SCRIPT = script_path("video2dsprite", "video2dsprite")
SKILL = "video2dsprite"
VERBS = ("triage", "key-plan", "extract", "clean", "sample", "process", "package", "verify", "doctor")


def frame(box=(10, 10, 22, 28), color=(40, 160, 210, 255), size=(32, 32)):
    im = Image.new("RGBA", size)
    im.paste(color, box)
    return im


class RegistrationTests(unittest.TestCase):
    def test_shared_scale_preserves_crouch_and_lift(self):
        originals = [frame(), frame((10, 18, 22, 28)), frame((10, 6, 22, 24))]
        output, info = V.fixed_envelope(originals, 64, 48, 58, "feet")
        boxes = [V.content_bbox(im) for im in output]
        self.assertEqual(info["mode"], "fixed-envelope")
        self.assertLess(boxes[1][3]-boxes[1][1], (boxes[0][3]-boxes[0][1])*.75)
        self.assertLess(boxes[2][3], boxes[0][3])

    def test_rgba_alpha_not_squared_in_normalize_or_sheet(self):
        original = frame(color=(20, 40, 60, 128))
        fixed, _ = V.fixed_envelope([original], 32, 18, 28, "feet")
        with tempfile.TemporaryDirectory() as tmp:
            info = V.build_exports(fixed, Path(tmp), "", 1)
            strip = Image.open(info["strip"]).convert("RGBA")
            self.assertEqual(strip.getchannel("A").getextrema()[1], 128)
            self.assertEqual(V.normalize_sprite(original, 32, 18, 28).getchannel("A").getextrema()[1], 128)

    def test_common_source_canvas_required(self):
        with self.assertRaisesRegex(ValueError, "one source canvas"):
            V.fixed_envelope([frame(), frame(size=(40, 40))], 64, 48, 58, "feet")

    def test_chroma_preserves_enclosed_costume_magenta(self):
        im = Image.new("RGBA", (32, 32), (255, 0, 255, 255))
        im.paste((20, 40, 60, 255), (8, 8, 24, 28))
        im.paste((255, 0, 255, 255), (12, 12, 20, 20))
        clean = V.chroma_key_rgba(im)
        self.assertEqual(clean.getpixel((0, 0))[3], 0)
        self.assertEqual(clean.getpixel((15, 15)), (255, 0, 255, 255))

    def test_native_alpha_preserved_and_stale_clean_frames_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, clean = root/"raw", root/"clean"
            raw.mkdir(); clean.mkdir()
            frame(color=(255, 0, 255, 128)).save(raw/"frame_0001.png")
            frame().save(clean/"clean_0099.png")
            paths = V.clean_frames(raw, clean, despill=1)
            self.assertEqual(len(list(clean.glob("*.png"))), 1)
            self.assertEqual(Image.open(paths[0]).getpixel((15, 15)), (255, 0, 255, 128))

    def test_opt_in_despill_changes_only_boundary_rgb_preserving_alpha(self):
        im = Image.new("RGBA", (12, 12), (255, 0, 255, 255))
        # Dark violet is below key threshold, representative of compressed fringe.
        im.paste((120, 50, 130, 255), (3, 3, 9, 10))
        untouched = V.chroma_key_rgba(im)
        corrected = V.chroma_key_rgba(im, despill=.5)
        self.assertEqual(untouched.getpixel((3, 5)), (120, 50, 130, 255))
        self.assertEqual(corrected.getpixel((3, 5)), (85, 50, 95, 255))
        self.assertEqual(corrected.getpixel((5, 5)), (120, 50, 130, 255))
        self.assertTrue(np.array_equal(np.asarray(corrected.getchannel("A")), np.asarray(untouched.getchannel("A"))))
        with self.assertRaises(ValueError): V.chroma_key_rgba(im, despill=1.2)

    def test_actual_count_and_specified_duration_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); clean = root/"clean"; clean.mkdir()
            for i in range(3): frame().save(clean/f"clean_{i:04d}.png")
            meta = V.sample_and_export(clean, root/"out", [8], duration=.6)
            self.assertEqual(meta["sets"][0]["count"], 3)
            self.assertEqual(meta["sets"][0]["requestedCount"], 8)
            self.assertEqual(meta["sets"][0]["gif_ms"], 200)


# --------------------------------------------------------------------------- helpers

KEY = (255, 0, 255)
NAVY, OUTLINE = (30, 40, 90), (20, 24, 32)


def subject_frames(count: int, size: int = 64, touch: tuple[int, ...] = ()) -> list[np.ndarray]:
    """Opaque RGB frames: an outlined navy body stepping 1 px right per frame on a magenta key.

    Frames listed in ``touch`` stretch the body to the right border (a cropped limb).
    """
    frames = []
    for index in range(count):
        rgb = np.empty((size, size, 3), np.uint8)
        rgb[...] = KEY
        x0, x1 = 16 + index % 4, 40 + index % 4
        if index in touch:
            x1 = size
        rgb[12:size - 12, x0:x1] = OUTLINE
        rgb[14:size - 14, x0 + 2:x1 - 2] = NAVY
        frames.append(rgb)
    return frames


def write_raw_frames(folder: Path, frames: list[np.ndarray]) -> Path:
    folder.mkdir(parents=True)
    for index, rgb in enumerate(frames):
        Image.fromarray(rgb).save(folder / f"frame_{index:06d}.png")
    return folder


def write_clip(path: Path, frames: list[np.ndarray], fps: int = 12, audio: bool = False) -> Path:
    """Encode RGB frames as H.264 yuv420p MP4 (optionally with an AAC sine track) with ffmpeg."""
    ffmpeg = require_ffmpeg()
    height, width = frames[0].shape[:2]
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "rawvideo",
               "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-shortest", "-c:a", "aac"]
    command += ["-c:v", "libx264", "-crf", "12", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(command, input=b"".join(np.ascontiguousarray(f).tobytes() for f in frames), check=True,
                   capture_output=True, timeout=120)
    return path


def run_main(argv, capsys) -> tuple[int, str, str]:
    code = V.main([str(part) for part in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def summary(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1 and stdout.isascii(), stdout
    return json.loads(lines[0])


def no_stage_left(parent: Path) -> bool:
    return not any(".stage-" in path.name for path in parent.iterdir())


def strings(document):
    if isinstance(document, dict):
        for value in document.values():
            yield from strings(value)
    elif isinstance(document, list):
        for value in document:
            yield from strings(value)
    elif isinstance(document, str):
        yield document


# ----------------------------------------------------------------- B05-T4 console encoding and CLI conventions

@pytest.mark.ffmpeg
def test_help_and_extract_under_cp1252_and_cp950(tmp_path):
    """report v2 P1-1, 3.4: cp1252 --help exited 1 and extract died printing an arrow after writing frames."""
    assert SCRIPT.read_bytes().isascii()
    assert_cli_help(SKILL, "video2dsprite")
    for verb in VERBS:
        result = run_cli([SCRIPT, verb, "--help"], "cp1252")
        assert result.returncode == 0 and result.stdout.isascii() and "usage" in result.stdout, (verb, result.stderr)
    clip = write_clip(tmp_path / "clip.mp4", subject_frames(6))
    for encoding in ("cp1252", "cp950"):
        out = tmp_path / f"frames-{encoding}"
        result = run_cli([SCRIPT, "extract", "--video", clip, "--output-dir", out], encoding)
        assert result.returncode == 0, result.stderr
        assert result.stdout.isascii() and result.stderr.isascii()
        line = summary(result.stdout)
        assert line["frames"] == 6 and line["first"] == "frame_000000.png" and line["last"] == "frame_000005.png"
        assert sorted(p.name for p in out.iterdir()) == [f"frame_{i:06d}.png" for i in range(6)]


def test_clean_refuses_existing_output(tmp_path, capsys):
    """plan Appendix D: an existing --output-dir is refused and left exactly as it was."""
    raw = write_raw_frames(tmp_path / "raw", subject_frames(3))
    out = tmp_path / "clean"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    code, stdout, stderr = run_main(["clean", "--raw-dir", raw, "--output-dir", out], capsys)
    assert code == 1 and stdout == "" and stderr.startswith("error:") and "already exists" in stderr
    assert sorted(p.name for p in out.iterdir()) == ["keep.txt"] and no_stage_left(tmp_path)
    code, _, stderr = run_main(["sample", "--clean-dir", raw, "--output-dir", out], capsys)
    assert code == 1 and "already exists" in stderr and sorted(p.name for p in out.iterdir()) == ["keep.txt"]


def test_strict_matte_failure_publishes_nothing(tmp_path, capsys):
    """plan Appendix D: --strict refuses key residue; the binary keyer keeps an enclosed hole (report v2 P0-1)."""
    frames = subject_frames(3)
    for rgb in frames:
        rgb[24:36, 26:32] = KEY  # a key-coloured hole enclosed by the body
    raw = write_raw_frames(tmp_path / "raw", frames)
    out = tmp_path / "clean"
    args = ["clean", "--raw-dir", raw, "--output-dir", out, "--matte", "binary", "--despill-mode", "off"]
    code, stdout, stderr = run_main(args + ["--strict"], capsys)
    assert code == 1 and stdout == "" and "matte QA failed" in stderr and "enclosed_key_pockets" in stderr
    assert not out.exists() and no_stage_left(tmp_path)
    code, stdout, stderr = run_main(args, capsys)  # without --strict the frames are published for review
    assert code == 0 and "warning: matte QA failed" in stderr
    assert summary(stdout)["status"] == "fail" and len(list(out.glob("clean_*.png"))) == 3


def test_old_out_dir_spelling_and_one_line_summary(tmp_path, capsys):
    """--out-dir still works; the summary names the output and metadata paths (report v2 P1-7)."""
    raw = write_raw_frames(tmp_path / "raw", subject_frames(4))
    code, stdout, stderr = run_main(["clean", "--raw-dir", raw, "--out-dir", tmp_path / "clean"], capsys)
    assert code == 0, stderr
    line = summary(stdout)
    assert Path(line["output"]) == (tmp_path / "clean").resolve()
    assert Path(line["metadata"]) == (tmp_path / "clean" / "matte-report.json").resolve()
    code, stdout, _ = run_main(["sample", "--clean-dir", tmp_path / "clean", "--output-dir", tmp_path / "sampled",
                                "--frame-counts", "2,4"], capsys)
    line = summary(stdout)
    assert code == 0 and Path(line["metadata"]).is_file() and [s["count"] for s in line["sets"]] == [2, 4]


def test_sample_meta_paths_are_output_relative(tmp_path, capsys):
    """MAP-24 style portability: pipeline-meta.json holds no absolute or staging paths."""
    raw = write_raw_frames(tmp_path / "raw", subject_frames(4))
    assert run_main(["clean", "--raw-dir", raw, "--output-dir", tmp_path / "clean"], capsys)[0] == 0
    out = tmp_path / "sampled"
    assert run_main(["sample", "--clean-dir", tmp_path / "clean", "--output-dir", out, "--frame-counts", "4"],
                    capsys)[0] == 0
    meta = json.loads((out / "pipeline-meta.json").read_text(encoding="utf-8"))
    assert meta["clean_dir"] == "../clean" and meta["matte_report"]["path"] == "../clean/matte-report.json"
    for text in strings(meta):
        assert not Path(text).is_absolute() and "stage" not in text, text
    for path in [*meta["sets"][0]["sprites"], meta["sets"][0]["gif"], meta["sets"][0]["strip"]]:
        assert (out / path).is_file(), path
    assert "README.txt" in {p.name for p in out.iterdir()}


# --------------------------------------------------------------------------- B05-T7 preview timing

def test_preview_durations_sum_exactly(tmp_path):
    """Dusk build-clips.py L8: per-frame GIF delays sum exactly to the requested preview length."""
    durations, nominal = V.preview_durations(24, 2.0)
    assert sum(durations) == 2000 and set(durations) == {80, 90} and nominal == 83
    assert all(ms % 10 == 0 for ms in durations)  # GIF stores whole centiseconds
    assert V.preview_durations(3, 0.6) == ([200, 200, 200], 200)
    assert V.preview_durations(48) == ([20] * 48, 25)  # cfed170 asked for 25 ms; GIF stores 20
    with pytest.raises(ValueError, match="too short"):
        V.preview_durations(24, 0.1)
    clean = tmp_path / "clean"
    clean.mkdir()
    for index in range(7):
        frame((4 + index, 10, 18 + index, 28)).save(clean / f"clean_{index:04d}.png")
    meta = V.sample_and_export(clean, tmp_path / "out", [7], duration=1.0)
    entry = meta["sets"][0]
    assert sum(entry["gif_durations_ms"]) == 1000 and len(entry["gif_durations_ms"]) == 7
    with Image.open(tmp_path / "out" / entry["gif"]) as gif:
        stored = []
        for index in range(gif.n_frames):
            gif.seek(index)
            stored.append(gif.info["duration"])
    assert stored == entry["gif_durations_ms"] and sum(stored) == 1000


# --------------------------------------------------------------------------- B05-T5 triage

@pytest.mark.ffmpeg
def test_triage_flags_border_frames_and_audio(tmp_path, capsys):
    """Dusk qa-review-raw.py L9-26, report v2 3.5: border-touching frames and the audio stream are reported."""
    clip = write_clip(tmp_path / "clip.mp4", subject_frames(12, touch=(4, 5, 9)), audio=True)
    out = tmp_path / "triage"
    code, stdout, stderr = run_main(["triage", "--video", clip, "--output-dir", out], capsys)
    assert code == 0, stderr
    assert "touches the border in 3 frame(s) (4, 5, 9)" in stderr and "never pad" in stderr
    report = json.loads((out / "raw-triage.json").read_text(encoding="utf-8"))
    assert_valid_contract(report, "video", "raw_triage_v1", skill=SKILL)
    assert report["borderTouchFrames"] == [4, 5, 9] and report["audioStreams"] == 1
    assert report["size"] == [64, 64] and report["fps"] == "12/1" and report["frames"] == 12
    assert report["border"]["touches"][0]["edges"] == {"right": 40}
    checks = {check["id"]: check["status"] for check in report["checks"]}
    assert checks == {"border_touch_frames": "warn", "audio_streams": "warn", "frame_count": "pass",
                      "key_drift": "pass"} and report["status"] == "warn"
    assert report["keyDrift"]["declared"] == "magenta" and report["keyDrift"]["distance"] < 10
    assert report["inputs"][0]["path"] == "../clip.mp4" and report["outputs"][0]["path"] == "triage-sheet.png"
    with Image.open(out / "triage-sheet.png") as sheet:
        assert sheet.mode == "RGB" and sheet.width > 4 * 64
    line = summary(stdout)
    assert line["borderTouchFrames"] == 3 and line["audioStreams"] == 1 and Path(line["metadata"]).is_file()


@pytest.mark.ffmpeg
def test_triage_clean_clip_and_strict_refusal(tmp_path, capsys):
    """A clean clip passes; --strict publishes nothing when the subject touches the border."""
    clean_clip = write_clip(tmp_path / "clean.mp4", subject_frames(8))
    code, stdout, stderr = run_main(["triage", "--video", clean_clip, "--output-dir", tmp_path / "ok"], capsys)
    report = json.loads((tmp_path / "ok" / "raw-triage.json").read_text(encoding="utf-8"))
    assert code == 0 and report["borderTouchFrames"] == [] and report["audioStreams"] == 0
    assert report["status"] == "needs-visual-review" and "warning" not in stderr
    cropped = write_clip(tmp_path / "cropped.mp4", subject_frames(8, touch=(2,)))
    code, stdout, stderr = run_main(["triage", "--video", cropped, "--output-dir", tmp_path / "strict", "--strict"],
                                    capsys)
    assert code == 1 and stdout == "" and "nothing was published" in stderr
    assert not (tmp_path / "strict").exists() and no_stage_left(tmp_path)


# --------------------------------------------------------------------------- extraction and process

@pytest.mark.ffmpeg
def test_extract_trims_input_side_and_refuses_existing_output(tmp_path, capsys):
    """A5 BREAKING: 0-based frame_000000.png names and input-side [start, start + duration) trimming."""
    clip = write_clip(tmp_path / "clip.mp4", subject_frames(8))
    out = tmp_path / "frames"
    code, stdout, stderr = run_main(["extract", "--video", clip, "--output-dir", out, "--start", 2 / 12,
                                     "--duration", 3 / 12], capsys)
    assert code == 0, stderr
    assert sorted(p.name for p in out.iterdir()) == ["frame_000000.png", "frame_000001.png", "frame_000002.png"]
    assert summary(stdout)["frames"] == 3
    code, _, stderr = run_main(["extract", "--video", clip, "--output-dir", out], capsys)
    assert code == 1 and "already exists" in stderr and len(list(out.iterdir())) == 3


@pytest.mark.ffmpeg
def test_process_publishes_once_with_portable_meta(tmp_path, capsys):
    """report v2 P1-7: process prints the metadata path and an output list; nothing absolute is stored."""
    clip = write_clip(tmp_path / "clip.mp4", subject_frames(8), audio=True)
    out = tmp_path / "work"
    code, stdout, stderr = run_main(["process", "--video", clip, "--output-dir", out, "--frame-counts", "4,8",
                                     "--playback-duration", "0.5"], capsys)
    assert code == 0, stderr
    line = summary(stdout)
    assert Path(line["metadata"]) == (out / "pipeline-meta.json").resolve()
    assert set(line["outputs"]) == {"frames_raw", "frames_clean", "matte_report", "sprite", "readme"}
    assert all(Path(path).exists() for path in line["outputs"].values())
    assert line["frames"] == 8 and line["matte"]["mode"] == "soft"
    meta = json.loads((out / "pipeline-meta.json").read_text(encoding="utf-8"))
    for text in strings(meta):
        assert not Path(text).is_absolute() and "stage-" not in text, text
    assert meta["video"]["path"] == "../clip.mp4" and meta["probe"]["audio_streams"] == 1
    assert meta["extract"]["naming"].startswith("frame_000000.png") and meta["raw_frames"] == 8
    for entry in meta["sets"]:
        assert sum(entry["gif_durations_ms"]) == 500 and len(entry["gif_durations_ms"]) == entry["count"]
    assert sorted(p.name for p in (out / "frames-raw").iterdir())[0] == "frame_000000.png"
    code, _, stderr = run_main(["process", "--video", clip, "--output-dir", out], capsys)
    assert code == 1 and "already exists" in stderr


@pytest.mark.ffmpeg
def test_process_strict_failure_publishes_nothing(tmp_path, capsys):
    """plan Appendix D: a strict matte QA failure leaves no output directory and no stage behind."""
    frames = subject_frames(4)
    for rgb in frames:
        rgb[24:36, 26:32] = KEY
    clip = write_clip(tmp_path / "clip.mp4", frames)
    out = tmp_path / "work"
    code, stdout, stderr = run_main(["process", "--video", clip, "--output-dir", out, "--matte", "binary",
                                     "--despill-mode", "off", "--strict", "--frame-counts", "4"], capsys)
    assert code == 1 and stdout == "" and "nothing was published" in stderr
    assert not out.exists() and no_stage_left(tmp_path)


@pytest.mark.ffmpeg
def test_doctor_reports_functional_probe(capsys):
    """A5: doctor proves encoders by encoding and decoding (no -encoders text matching); old keys kept."""
    require_ffmpeg()
    code, stdout, _ = run_main(["doctor"], capsys)
    report = summary(stdout)
    assert code == 0 and set(report["functional"]) == {"vp9_alpha", "libx264"}
    assert report["webm"] == report["functional"]["vp9_alpha"] and report["png"] is True


if __name__ == "__main__":
    unittest.main()
