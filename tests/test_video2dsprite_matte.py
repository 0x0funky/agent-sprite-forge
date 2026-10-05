"""Keying tests for video2dsprite.py clean/process (B05-T2, T3, T6) on synthetic clips.

Scenes come from A2's deterministic generators in tests/fixtures/keys/make_key_fixtures.py. The cfed170
video keyer and clean step are copied verbatim below as the oracle of ``--matte binary``. The bench test
(marker bench, opt-in) reads FORGE_BENCH_CLIP, the owner's Ryo clip of report v2, and reuses the edge
metrics of tests/benchmarks/keyer_bench.py.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import statistics
import subprocess
import time
from collections import deque
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import FIXTURES_DIR, REPO_ROOT, assert_valid_contract, load_script, require_ffmpeg

V = load_script("video2dsprite", "video2dsprite")
fm = V.fm
SKILL = "video2dsprite"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fx = _load_module("forge_b05_key_fixtures", FIXTURES_DIR / "keys" / "make_key_fixtures.py")
MAGENTA, PURPLE = (255, 0, 255), (180, 60, 200)


# --------------------------------------------------------------------------- cfed170 oracle (verbatim)

_CFED170_MAGENTA = np.array([255, 0, 255], dtype=np.float32)


def _cfed170_near_magenta_mask(rgb: np.ndarray, dist: float = 55.0) -> np.ndarray:
    f = rgb.astype(np.float32)
    d = np.linalg.norm(f - _CFED170_MAGENTA, axis=2)
    r, g, b = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    pinkish = (r > 160) & (b > 160) & (g < 140) & ((r + b) / 2 - g > 40)
    return (d <= dist) | pinkish


def _cfed170_chroma_key_rgba(im: Image.Image, dist: float = 55.0, despill: float = 0.0) -> Image.Image:
    if not math.isfinite(despill) or not 0 <= despill <= 1:
        raise ValueError("despill must be between 0 and 1")
    rgba = im.convert("RGBA")
    arr = np.array(rgba)
    rgb = arr[:, :, :3]
    h, w = rgb.shape[:2]
    key = _cfed170_near_magenta_mask(rgb, dist=dist)
    visited = np.zeros((h, w), dtype=bool)
    q: deque[tuple[int, int]] = deque()
    for y, x in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)):
        if key[y, x]:
            visited[y, x] = True
            q.append((x, y))
    for x in range(w):
        for y in (0, h - 1):
            if key[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if key[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append((x, y))
    while q:
        x, y = q.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h and not visited[ny, nx] and key[ny, nx]:
                visited[ny, nx] = True
                q.append((nx, ny))
    out = arr.copy()
    out[visited, 3] = 0
    adjacent = np.zeros_like(visited)
    adjacent[1:] |= visited[:-1]
    adjacent[:-1] |= visited[1:]
    adjacent[:, 1:] |= visited[:, :-1]
    adjacent[:, :-1] |= visited[:, 1:]
    channels = out[:, :, :3].astype(np.float32)
    spill = np.maximum(0, np.minimum(channels[:, :, 0], channels[:, :, 2]) - channels[:, :, 1])
    fringe = ~visited & adjacent & (out[:, :, 3] > 0) & (spill > 20)
    if despill and fringe.any():
        fr = out[fringe].astype(np.float32)
        amount = spill[fringe] * despill
        fr[:, 0] = np.clip(fr[:, 0] - amount, 0, 255)
        fr[:, 2] = np.clip(fr[:, 2] - amount, 0, 255)
        out[fringe] = fr.astype(np.uint8)
    out[out[:, :, 3] == 0, :3] = 0
    return Image.fromarray(out, "RGBA")


def _cfed170_clean_frames(raw_dir: Path, clean_dir: Path, dist: float = 55.0, key_mode: str = "auto",
                          despill: float = 0.0) -> list[Path]:
    clean_dir.mkdir(parents=True, exist_ok=True)
    outs = []
    for i, path in enumerate(sorted(raw_dir.glob("frame_*.png"))):
        rgba = Image.open(path).convert("RGBA")
        native_alpha = rgba.getchannel("A").getextrema()[0] < 255
        cleaned = rgba if key_mode == "none" or (key_mode == "auto" and native_alpha) else _cfed170_chroma_key_rgba(rgba, dist=dist, despill=despill)
        out = clean_dir / f"clean_{i:04d}.png"
        cleaned.save(out)
        outs.append(out)
    return outs


# --------------------------------------------------------------------------- helpers

def write_raw(folder: Path, frames) -> Path:
    folder.mkdir(parents=True)
    for index, frame in enumerate(frames):
        Image.fromarray(np.asarray(frame)).save(folder / f"frame_{index:06d}.png")
    return folder


def load(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA")).copy()


def clean_frame(folder: Path, index: int) -> np.ndarray:
    return load(folder / f"clean_{index:04d}.png")


def report_of(folder: Path) -> dict:
    return json.loads((folder / "matte-report.json").read_text(encoding="utf-8"))


def run_main(argv, capsys) -> tuple[int, str, str]:
    code = V.main([str(part) for part in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def clean(capsys, raw: Path, out: Path, *flags) -> tuple[dict, str]:
    """Run the clean verb in-process; return the matte report and stderr."""
    code, _, stderr = run_main(["clean", "--raw-dir", raw, "--output-dir", out, *flags], capsys)
    assert code == 0, stderr
    return report_of(out), stderr


def settings_for(*flags):
    args = V.build_parser().parse_args(["clean", "--raw-dir", "raw", "--output-dir", "out", *map(str, flags)])
    return V.matte_settings(args, warn=lambda message: None)


def body_frames(count: int = 3, size: int = 96, key=MAGENTA) -> list[np.ndarray]:
    """An outlined navy body on a flat key, one px further right each frame."""
    frames = []
    for index in range(count):
        rgb = np.empty((size, size, 3), np.uint8)
        rgb[...] = key
        rgb[16:size - 16, 20 + index:size - 30 + index] = fx.OUTLINE
        rgb[18:size - 18, 22 + index:size - 32 + index] = fx.NAVY
        frames.append(rgb)
    return frames


def flicker_frames(count: int = 8) -> list[np.ndarray]:
    """A static body whose 1 px rim alternates between two colours 18 apart (codec-like jitter).

    The dominance matte turns them into alpha 0.95 and 0.65: a flip every pair that hysteresis can hold.
    """
    frames = []
    for index in range(count):
        rgb = np.empty((48, 48, 3), np.uint8)
        rgb[...] = MAGENTA
        rgb[10:38, 10:38] = fx.NAVY
        rim = (100, 74, 100) if index % 2 == 0 else (118, 56, 118)
        rgb[9, 10:38] = rgb[38, 10:38] = rim
        rgb[10:38, 9] = rgb[10:38, 38] = rim
        frames.append(rgb)
    return frames


# --------------------------------------------------------------------------- B05-T2 matte modes

def test_ring_hole_keyed(tmp_path, capsys):
    """report v2 P0-1 (16 of 145 Ryo frames shipped enclosed key holes): the default matte keys the hole."""
    scene = fx.enclosed_hole_ring()
    hole = scene["hole"]
    assert int(hole.sum()) >= 300
    raw = write_raw(tmp_path / "raw", [scene["rgb"], fx.chroma_420(scene["rgb"]), scene["rgb"]])
    report, _ = clean(capsys, raw, tmp_path / "soft")
    for index in range(3):
        assert clean_frame(tmp_path / "soft", index)[..., 3][hole].max() == 0
    assert report["enclosed_key_pockets"] == 0 and report["opaque_key_px"] == 0
    assert report["status"] == "needs-visual-review" and report["mode"] == "soft"
    # The cfed170 keyer (legacy switch) keeps the hole, and the residue QA says so.
    legacy, stderr = clean(capsys, raw, tmp_path / "binary", "--matte", "binary", "--despill-mode", "off")
    assert clean_frame(tmp_path / "binary", 0)[..., 3][hole].min() == 255
    assert legacy["enclosed_key_pockets"] == 3 and legacy["status"] == "fail" and "matte QA failed" in stderr
    # The same keyer with pocket removal clears it.
    fixed, _ = clean(capsys, raw, tmp_path / "binary-pockets", "--matte", "binary", "--pockets", "remove")
    assert clean_frame(tmp_path / "binary-pockets", 0)[..., 3][hole].max() == 0
    assert fixed["pockets_removed"] == 3 and fixed["enclosed_key_pockets"] == 0


def test_residue_is_counted_before_despill(tmp_path, capsys):
    """A kept key pocket neutralised by despill 'all' is opaque grey residue; QA still counts it."""
    raw = write_raw(tmp_path / "raw", [fx.enclosed_hole_ring()["rgb"]])
    report, _ = clean(capsys, raw, tmp_path / "out", "--matte", "binary", "--despill-mode", "all")
    assert report["despill_applied"] == "all" and report["frames"][0]["despill_px"] > 0
    assert report["opaque_key_px"] > 0 and report["enclosed_key_pockets"] == 1 and report["status"] == "fail"


def test_purple_costume_kept(tmp_path, capsys):
    """report v2 P0-1 acceptance: a real purple costume (180, 60, 200) is never dug out.

    Pocket removal keeps it under any matte. The video soft matte keys light purples near magenta
    (A2 handoff section 8), so the run protects them with --protect-color and says so when it does not.
    """
    rgb, interior = fx.purple_material(purple=PURPLE)
    raw = write_raw(tmp_path / "raw", [rgb, rgb])
    protected, stderr = clean(capsys, raw, tmp_path / "protected", "--protect-color", "#b43cc8")
    keyed = clean_frame(tmp_path / "protected", 0)
    assert np.array_equal(keyed[..., :3][interior], rgb[interior]) and (keyed[..., 3][interior] == 255).all()
    assert protected["interior_despill"]["interior_despill"] is False and protected["protect"]["px"] > 0
    assert protected["status"] == "needs-visual-review" and "warning" not in stderr
    legacy, _ = clean(capsys, raw, tmp_path / "binary", "--matte", "binary", "--pockets", "remove",
                      "--despill-mode", "off")
    keyed = clean_frame(tmp_path / "binary", 0)
    assert legacy["pockets_removed"] == 0 and np.array_equal(keyed[..., :3][interior], rgb[interior])
    unprotected, stderr = clean(capsys, raw, tmp_path / "unprotected")
    assert "owns key-coloured material" in stderr and unprotected["status"] == "warn"
    master = np.zeros((96, 96, 4), np.uint8)
    master[8:88, 16:80] = (*fx.NAVY, 255)
    master[20:60, 16:44] = (*PURPLE, 255)
    Image.fromarray(master).save(tmp_path / "master.png")
    named, stderr = clean(capsys, raw, tmp_path / "named", "--reference", tmp_path / "master.png")
    assert named["design_colours_at_risk"][0]["from"] == "#b43cc8" and "#b43cc8 (1120 px)" in stderr
    on_key = np.empty((96, 96, 3), np.uint8)  # the image-to-video input still: the master on the key
    on_key[...] = MAGENTA
    on_key[8:88, 16:80] = fx.OUTLINE
    on_key[10:86, 18:78] = fx.NAVY
    on_key[20:60, 22:44] = PURPLE
    Image.fromarray(on_key).save(tmp_path / "input.png")
    opaque, _ = clean(capsys, raw, tmp_path / "opaque-reference", "--reference", tmp_path / "input.png")
    assert opaque["design_colours_at_risk"][0]["from"] == "#b43cc8"
    assert opaque["interior_despill"]["reference_share"] > fm.KEY_MATERIAL_SHARE_MAX
    dark, interior_dark = fx.purple_material(purple=(120, 40, 140))
    raw_dark = write_raw(tmp_path / "raw-dark", [dark])
    clean(capsys, raw_dark, tmp_path / "dark")
    keyed = clean_frame(tmp_path / "dark", 0)
    assert np.array_equal(keyed[..., :3][interior_dark], dark[interior_dark])


def _binary_frames() -> list[np.ndarray]:
    sheet = fx.legacy_sheet(256, 2)
    native = np.zeros((128, 128, 4), np.uint8)
    native[16:112, 16:112] = fx.native_alpha(96)["rgba"]
    native[0:4, 0:128] = (200, 30, 220, 0)  # hidden RGB under alpha 0
    return [sheet[:128, :128, :3], sheet[128:, 128:, :3], fx.enclosed_hole_ring(128)["rgb"],
            fx.grok_jpeg_pink(128)["rgb"], native]


@pytest.mark.parametrize("dist, despill, key_mode", [(55.0, 0.0, "auto"), (70.0, 0.5, "auto"), (55.0, 1.0, "magenta")])
def test_binary_matches_cfed170(tmp_path, capsys, dist, despill, key_mode):
    """B05-T2: --matte binary --despill-mode off is the cfed170 clean step, pixel for pixel.

    Output PNGs follow the I/O convention (RGB zeroed under alpha 0), which only touches the hidden
    colour of frames that keep their native alpha.
    """
    raw = write_raw(tmp_path / "raw", _binary_frames())
    oracle = _cfed170_clean_frames(raw, tmp_path / "oracle", dist, key_mode, despill)
    flags = ["--matte", "binary", "--despill-mode", "off", "--dist", dist, "--despill", despill, "--key-mode", key_mode]
    report, _ = clean(capsys, raw, tmp_path / "cli", *flags)
    library = V.clean_frames(raw, tmp_path / "library", dist, key_mode, despill)
    assert len(library) == len(oracle) == 5
    for index, expected_path in enumerate(oracle):
        expected = load(expected_path)
        expected[expected[..., 3] == 0, :3] = 0
        assert np.array_equal(clean_frame(tmp_path / "cli", index), expected), index
        assert np.array_equal(load(library[index]), expected), index
    assert report["pockets"] == "keep" and report["temporal"]["mode"] == "off"
    assert report["legacy"] == {"dist": dist, "despill": despill}
    assert report["passthrough_frames"] == ([4] if key_mode == "auto" else [])


def test_meta_matte_block_validates(tmp_path, capsys):
    """A0 contract: matte-report.json is a video2dsprite.matte_report.v1 document and a QA envelope."""
    raw = write_raw(tmp_path / "raw", body_frames(3))
    report, _ = clean(capsys, raw, tmp_path / "out")
    assert_valid_contract(report, "video", "matte_report_v1", skill=SKILL)
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
    assert all(isinstance(v, int) for v in report["key"]) and report["key"] == [255, 0, 255]
    assert set(report["params"]) == set(fm.KeyParams().to_dict())
    assert report["key_estimate"]["source"] == "border_median" and report["key_estimate"]["valid"]
    assert [ref["path"] for ref in report["inputs"]] == [f"../raw/frame_{i:06d}.png" for i in range(3)]
    for ref in report["outputs"]:
        assert fm.forge_core.sha256_file(tmp_path / "out" / ref["path"]) == ref["sha256"]
    assert report["tool"] == {"name": "video2dsprite.py clean", "version": V.TOOL_VERSION}


def test_clean_is_deterministic(tmp_path, capsys):
    """plan Appendix D: the same frames and options give the same bytes (frames and report)."""
    raw = write_raw(tmp_path / "raw", body_frames(3))
    clean(capsys, raw, tmp_path / "a")
    clean(capsys, raw, tmp_path / "b")
    for name in ["matte-report.json", "clean_0000.png", "clean_0001.png", "clean_0002.png"]:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


@pytest.mark.ffmpeg
def test_process_meta_matte_block_validates(tmp_path, capsys):
    """pipeline-meta.json's matte block is the matte report (without the file lists) and validates."""
    require_ffmpeg()
    clip = tmp_path / "clip.mp4"
    V.fa.encode_h264_loop(body_frames(6, size=64), clip, "12/1", crf=12)
    code, _, stderr = run_main(["process", "--video", clip, "--output-dir", tmp_path / "work", "--frame-counts", "6"],
                               capsys)
    assert code == 0, stderr
    meta = json.loads((tmp_path / "work" / "pipeline-meta.json").read_text(encoding="utf-8"))
    assert_valid_contract(meta["matte"], "video", "matte_report_v1", skill=SKILL)
    assert meta["matte"]["report_file"] == "frames-clean/matte-report.json"
    assert "inputs" not in meta["matte"] and "outputs" not in meta["matte"]
    assert report_of(tmp_path / "work" / "frames-clean")["checks"] == meta["matte"]["checks"]


def test_green_key_detected_and_keyed(tmp_path, capsys):
    """--key auto finds a green backdrop on the border ring (Dusk keys per actor: magenta or green)."""
    frames = []
    for index in range(3):
        rgb = np.empty((96, 96, 3), np.int16)
        rgb[...] = (12, 230, 30)
        for channel in range(3):
            rgb[..., channel] += fx.hash_noise((96, 96), 3, seed=40 + 3 * index + channel)
        rgb[24:72, 24:72] = fx.OUTLINE
        rgb[26:70, 26:70] = fx.NAVY
        frames.append(np.clip(rgb, 0, 255).astype(np.uint8))
    report, _ = clean(capsys, write_raw(tmp_path / "raw", frames), tmp_path / "out")
    assert report["key_declared"] == "green" and np.abs(np.subtract(report["key"], (12, 230, 30))).max() <= 3
    assert report["key_estimate"]["detected"]["ring_share"]["green"] == 3.0
    keyed = clean_frame(tmp_path / "out", 1)
    assert keyed[0, 0, 3] == 0 and (keyed[30:66, 30:66, 3] == 255).all()
    assert report["opaque_key_px"] == 0 and report["status"] == "needs-visual-review"


def test_explicit_hex_key_used_as_given(tmp_path, capsys):
    """#rrggbb is used as given: no border estimate (key_estimate null), every frame keyed with it."""
    raw = write_raw(tmp_path / "raw", body_frames(2, key=(202, 80, 177)))
    report, _ = clean(capsys, raw, tmp_path / "out", "--key", "#CA50B1")
    assert report["key"] == [202, 80, 177] and report["key_estimate"] is None and report["key_request"] == "#ca50b1"
    assert clean_frame(tmp_path / "out", 0)[0, 0, 3] == 0


def test_native_alpha_frames_pass_through(tmp_path, capsys):
    """--key-mode auto keeps frames that already carry real transparency (VP9 alpha WebM)."""
    native = fx.native_alpha(64)["rgba"]
    report, _ = clean(capsys, write_raw(tmp_path / "raw", [native, native]), tmp_path / "out")
    assert np.array_equal(clean_frame(tmp_path / "out", 0), native)
    assert report["passthrough_frames"] == [0, 1] and report["temporal"]["active"] is False
    assert "native-alpha" in report["temporal"]["skipped"]
    none, _ = clean(capsys, write_raw(tmp_path / "raw-key", body_frames(2)), tmp_path / "none", "--key-mode", "none")
    assert none["mode"] == "none" and clean_frame(tmp_path / "none", 0)[0, 0, 3] == 255


@pytest.mark.parametrize("flags, message", [
    (["--matte", "binary", "--key", "green"], "magenta keyer"),
    (["--dist", "60"], "--dist belongs to the cfed170 keyer"),
    (["--despill", "0.5"], "--despill belongs to the cfed170 keyer"),
    (["--matte", "dominance", "--local-background"], "--local-background belongs to the soft matte"),
    (["--matte", "dominance", "--unmix"], "--unmix belongs to the soft matte"),
    (["--key-mode", "magenta", "--key", "green"], "old spelling"),
    (["--erode", "99"], "erode must be a whole number"),
    (["--protect-color", "#zzzzzz"], "unknown key"),
])
def test_matte_option_conflicts_are_errors(tmp_path, capsys, flags, message):
    """Contradictory keying options fail before any output exists."""
    raw = write_raw(tmp_path / "raw", body_frames(1))
    code, stdout, stderr = run_main(["clean", "--raw-dir", raw, "--output-dir", tmp_path / "out", *flags], capsys)
    assert code == 1 and stdout == "" and message in stderr and not (tmp_path / "out").exists()


def test_bad_key_argument_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as raised:
        V.main(["clean", "--raw-dir", "raw", "--output-dir", "out", "--key", "pink"])
    assert raised.value.code == 2 and "unknown key" in capsys.readouterr().err


def test_despill_modes_unmix_and_erode(tmp_path, capsys):
    """--despill-mode drives KeyParams.interior_despill (off also drops the colour band); erode shrinks alpha."""
    raw = write_raw(tmp_path / "raw", body_frames(2))
    auto, _ = clean(capsys, raw, tmp_path / "auto")
    assert auto["despill_applied"] == "all" and auto["params"]["interior_despill"] is True
    decision = auto["interior_despill"]
    assert decision["interior_despill"] is True and decision["sampled_from"] == "keyed frames"
    edge, _ = clean(capsys, raw, tmp_path / "edge", "--despill-mode", "edge")
    assert edge["params"]["interior_despill"] is False and edge["params"]["r_c"] == 6
    off, _ = clean(capsys, raw, tmp_path / "off", "--despill-mode", "off", "--no-unmix")
    assert off["params"]["r_c"] == 0 and off["params"]["unmix_from"] == 1.0 and off["unmix"] is False
    eroded, _ = clean(capsys, raw, tmp_path / "eroded", "--erode", "1")
    plain, shrunk = clean_frame(tmp_path / "auto", 0)[..., 3], clean_frame(tmp_path / "eroded", 0)[..., 3]
    ys, xs = np.nonzero(plain)
    ys2, xs2 = np.nonzero(shrunk)
    assert (ys2.min(), xs2.min(), ys2.max(), xs2.max()) == (ys.min() + 1, xs.min() + 1, ys.max() - 1, xs.max() - 1)
    assert eroded["erode"] == 1
    dominance, _ = clean(capsys, raw, tmp_path / "dominance", "--matte", "dominance", "--despill-mode", "edge",
                         "--despill-radius", "2")
    assert dominance["despill_radius"] == 2 and "params" not in dominance and dominance["opaque_key_px"] == 0
    local, _ = clean(capsys, raw, tmp_path / "local", "--local-background")
    assert local["local_background"] == fm.LOCAL_BACKGROUND_RADIUS


# --------------------------------------------------------------------------- B05-T3 temporal stability

def test_temporal_alpha_cuts_synthetic_flips_by_40_percent(tmp_path, capsys):
    """report v2 P2-1: alpha hysteresis cuts flips per frame pair by at least 40% on a jittering rim."""
    raw = write_raw(tmp_path / "raw", flicker_frames())
    held, _ = clean(capsys, raw, tmp_path / "held", "--matte", "dominance")
    temporal = held["temporal"]
    assert temporal["mode"] == "alpha" and temporal["requested"] == "auto" and temporal["active"]
    assert temporal["flips_before"] >= 100 and temporal["flips_after"] <= 0.6 * temporal["flips_before"]
    assert held["flips_per_frame_pair"] == temporal["flips_after"]
    raw_alpha = [clean_frame(tmp_path / "held", index)[..., 3] for index in range(8)]
    assert all(np.array_equal(a > 0, raw_alpha[0] > 0) for a in raw_alpha)  # never invents or drops coverage
    free, _ = clean(capsys, raw, tmp_path / "free", "--matte", "dominance", "--temporal-stability", "off")
    assert free["temporal"]["mode"] == "off" and free["flips_per_frame_pair"] == temporal["flips_before"]
    assert {c["id"]: c["status"] for c in free["checks"]}["flips_per_frame_pair"] == "warn"


def test_streaming_hysteresis_matches_library():
    """The streaming hysteresis returns forge_matte.temporal_alpha_hysteresis's bytes (loop off)."""
    sequence = fx.noisy_static_sequence(frames=10, size=48)["alphas"]
    rng = np.random.default_rng(5)
    for alphas in ([np.floor(a * 255 + 0.5).astype(np.uint8) for a in sequence],
                   [rng.integers(0, 256, (24, 24), dtype=np.uint8) for _ in range(9)],
                   [np.where(rng.random((16, 16)) < 0.3, 0, rng.integers(90, 170, (16, 16))).astype(np.uint8)
                    for _ in range(7)]):
        expected = fm.temporal_alpha_hysteresis(alphas)
        stream = V._LocalAlphaHysteresis()
        for frame, reference in zip(alphas, expected):
            assert np.array_equal(stream.push(frame), reference)


def test_pair_flips_match_library():
    """_local_pair_flips with one motion mask per pair equals forge_matte.flip_count."""
    rng = np.random.default_rng(11)
    for _ in range(6):
        alphas = [rng.integers(0, 256, (20, 30), dtype=np.uint8) for _ in range(2)]
        raws = [rng.integers(0, 256, (20, 30, 3), dtype=np.uint8)]
        raws.append(np.clip(raws[0].astype(np.int16) + rng.integers(-30, 31, (20, 30, 3)), 0, 255).astype(np.uint8))
        still = V._local_still_mask(raws[0], raws[1])
        assert V._local_pair_flips(alphas[0], alphas[1], still) == fm.flip_count(alphas, raw=raws)


@pytest.mark.ffmpeg
def test_trimmed_process_decides_despill_on_the_whole_clip(tmp_path, capsys):
    """The clip-level despill rule samples the source clip, not the trimmed window (auto_interior_despill)."""
    require_ffmpeg()
    frames = []
    for index in range(24):
        rgb = np.empty((64, 64, 3), np.uint8)
        rgb[...] = MAGENTA
        rgb[8:56, 12:52] = fx.OUTLINE
        rgb[10:54, 14:50] = fx.NAVY
        if index < 4:
            rgb[20:44, 22:42] = PURPLE  # key-coloured material only at the start
        frames.append(rgb)
    clip = tmp_path / "clip.mp4"
    V.fa.encode_h264_loop(frames, clip, "12/1", crf=10)
    code, _, stderr = run_main(["process", "--video", clip, "--output-dir", tmp_path / "trim", "--duration",
                                3 / 12, "--frame-counts", "3"], capsys)
    assert code == 0, stderr
    decision = report_of(tmp_path / "trim" / "frames-clean")["interior_despill"]
    assert decision["sampled_from"] == "source clip" and decision["clip_sample_frames"][-1] == 23
    assert decision["interior_despill"] is True
    code, _, stderr = run_main(["clean", "--raw-dir", tmp_path / "trim" / "frames-raw", "--output-dir",
                                tmp_path / "window"], capsys)
    window = report_of(tmp_path / "window")["interior_despill"]
    assert window["sampled_from"] == "keyed frames" and window["interior_despill"] is False


# --------------------------------------------------------------------------- B05-T6 matte profile and key plan

def _profile(**matte) -> dict:
    return {"schema": "video2dsprite.character_profile.v1", "id": "traveler", "sourceSize": [96, 96],
            "sourceAnchor": [48, 80], "registration": {"scale": 1.0, "anchorMode": "feet"},
            "matte": {"mode": "soft", "key": "magenta", "erode": 1, "unmix": False, "despill": "edge", **matte},
            "clips": {"idle": {"padding": [0, 0, 0, 0], "sourceSize": [96, 96], "sourceAnchor": [48, 80]}}}


def test_profile_pins_settings(tmp_path, capsys):
    """game-opus55 pixelate.py L472-477: a profile pins key, erode, unmix and despill; deviations warn."""
    profile = _profile(params={"r_c": 4})
    assert_valid_contract(profile, "video", "character_profile_v1", skill=SKILL)
    (tmp_path / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    still = body_frames(1)[0]  # static: an eroded silhouette that moves counts as flips (matte.md)
    raw = write_raw(tmp_path / "raw", [still, still])
    pinned, stderr = clean(capsys, raw, tmp_path / "pinned", "--matte-profile", tmp_path / "profile.json")
    assert "warning" not in stderr
    assert (pinned["mode"], pinned["key_request"], pinned["erode"], pinned["unmix"], pinned["despill_mode"]) == \
        ("soft", "magenta", 1, False, "edge")
    assert pinned["params"]["r_c"] == 4 and pinned["params"]["unmix_from"] == 1.0
    assert pinned["params"]["interior_despill"] is False and pinned["despill_applied"] == "edge"
    assert pinned["profile"]["deviations"] == [] and pinned["profile"]["file"]["path"] == "../profile.json"
    assert pinned["profile"]["pinned"]["despill_mode"] == "edge"
    deviating, stderr = clean(capsys, raw, tmp_path / "deviating", "--matte-profile", tmp_path / "profile.json",
                              "--despill-mode", "all", "--erode", "1")
    assert "warning: --despill-mode all deviates from the matte profile (edge)" in stderr and "--erode" not in stderr
    assert deviating["despill_mode"] == "all" and deviating["params"]["interior_despill"] is True
    assert deviating["profile"]["deviations"] == [{"option": "--despill-mode", "profile": "edge", "used": "all"}]
    assert deviating["status"] == "needs-visual-review"  # a deviation is reported, not a QA failure
    (tmp_path / "pinned-auto.json").write_text(json.dumps(_profile(despill="auto", params={"interior_despill": False})),
                                               encoding="utf-8")
    auto, _ = clean(capsys, raw, tmp_path / "auto", "--matte-profile", tmp_path / "pinned-auto.json")
    assert auto["despill_applied"] == "edge" and auto["interior_despill"]["pinned_by_profile"] is False
    (tmp_path / "bad.json").write_text(json.dumps({**profile, "schema": "video2dsprite.registration_job.v1"}),
                                       encoding="utf-8")
    code, _, stderr = run_main(["clean", "--raw-dir", raw, "--output-dir", tmp_path / "bad", "--matte-profile",
                                tmp_path / "bad.json"], capsys)
    assert code == 1 and "character_profile.v1" in stderr and not (tmp_path / "bad").exists()


def test_profile_mode_rules(tmp_path):
    """Profile params are soft-matte KeyParams; a binary profile cannot pin unmix."""
    (tmp_path / "binary.json").write_text(json.dumps(_profile(mode="binary", unmix=True)), encoding="utf-8")
    with pytest.raises(ValueError, match="--unmix belongs to the soft matte"):
        settings_for("--matte-profile", tmp_path / "binary.json")
    (tmp_path / "params.json").write_text(json.dumps(_profile(params={"t_fg": 30.0, "bogus": 1})), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown KeyParams field"):
        settings_for("--matte-profile", tmp_path / "params.json")


def _master(scarf) -> np.ndarray:
    master = np.zeros((96, 96, 4), np.uint8)
    master[8:88, 16:80] = (*fx.NAVY, 255)
    if scarf is not None:
        master[20:60, 16:44] = (*scarf, 255)
    return master


def test_key_plan_rejects_magenta_for_pink_master(tmp_path, capsys):
    """Dusk prepare-actors.py L18: a pink design rejects magenta; key-plan prints the key and the prompt sentence."""
    Image.fromarray(_master((230, 60, 140))).save(tmp_path / "pink.png")
    code, stdout, stderr = run_main(["key-plan", "--master", tmp_path / "pink.png"], capsys)
    plan = json.loads(stdout)
    assert code == 0 and stdout.isascii() and plan["key"] == "green" and plan["status"] == "ok"
    assert plan["process_key"] == "green" and {row["key"]: row["rejected"] for row in plan["candidates"]}["magenta"]
    assert "green (#00ff00)" in plan["background_sentence"] and "green-coloured light" in plan["background_sentence"]
    assert plan["master"]["path"] == "pink.png" and plan["design_colours_at_risk"] == []
    Image.fromarray(_master(None)).save(tmp_path / "navy.png")
    code, stdout, _ = run_main(["key-plan", "--master", tmp_path / "navy.png"], capsys)
    assert json.loads(stdout)["key"] == "magenta"
    code, stdout, stderr = run_main(["key-plan", "--master", tmp_path / "pink.png", "--candidates", "magenta"], capsys)
    assert code == 0 and json.loads(stdout)["status"] == "conflict" and "warning" in stderr
    code, stdout, stderr = run_main(["key-plan", "--master", tmp_path / "pink.png", "--candidates", "magenta",
                                     "--strict"], capsys)
    assert code == 1 and stdout == "" and "fights the master" in stderr


def test_key_plan_names_design_colours_the_video_matte_would_key(tmp_path, capsys):
    """With magenta forced, a purple master lists the colour to protect or recolour."""
    Image.fromarray(_master(PURPLE)).save(tmp_path / "purple.png")
    code, stdout, _ = run_main(["key-plan", "--master", tmp_path / "purple.png", "--candidates", "magenta"], capsys)
    risky = json.loads(stdout)["design_colours_at_risk"]
    assert code == 0 and risky[0]["from"] == "#b43cc8" and risky[0]["px"] == 1120


# --------------------------------------------------------------------------- bench (opt-in)

def _load_keyer_bench():
    return _load_module("forge_b05_keyer_bench", REPO_ROOT / "tests" / "benchmarks" / "keyer_bench.py")


@pytest.mark.bench
def test_bench_ryo_clip_default_matte(tmp_path):
    """B05-T2/T3 bench on FORGE_BENCH_CLIP (the Ryo clip of report v2, 145 frames):
    fringe 0 and leak 0 on frames 57-87 (report v2 metrics), leak 0 and no enclosed pockets on all frames,
    at most 5.7 flips per frame pair, and the soft keyer at most 0.8 s per 960^2 frame."""
    clip = os.environ.get("FORGE_BENCH_CLIP")
    if not clip:
        pytest.skip("set FORGE_BENCH_CLIP to the Ryo grok-native.mp4")
    require_ffmpeg()
    bench = _load_keyer_bench()
    raw = tmp_path / "raw"
    frames = V.extract_frames(Path(clip), raw)
    reference = os.environ.get("FORGE_BENCH_REFERENCE")
    reference_pixels = load(Path(reference)) if reference else None
    started = time.perf_counter()
    report = V.key_frames(raw, tmp_path / "clean", V.MatteSettings(), reference=reference_pixels)
    pipeline_seconds = (time.perf_counter() - started) / len(frames)
    pocket_frames = [row["index"] for row in report["frames"] if row["enclosed_key_pockets"]]
    assert pocket_frames == [] and report["opaque_key_px"] == 0, pocket_frames
    rgb = {index: np.asarray(Image.open(path).convert("RGB")) for index, path in enumerate(frames)}
    keyed = {index: clean_frame(tmp_path / "clean", index) for index in rgb}
    leaks = [index for index in rgb if int((bench.RawFrame(rgb[index]).def_bg & (keyed[index][..., 3] > 38)).sum())]
    assert leaks == [], leaks  # alpha > 0.15 on definite backdrop, every frame
    window = range(57, 88)
    raws = {index: bench.RawFrame(rgb[index]) for index in window}
    rows = [bench.frame_metrics(raws[index], keyed[index], fm.forge_core) for index in window]
    fringe = sum(row["spill_px8"] for row in rows)
    leak = sum(row["leak_px"] for row in rows)
    alphas = {index: keyed[index][..., 3].astype(np.float32) / 255.0 for index in window}
    flips = statistics.mean(bench.pair_flips(raws[a], raws[a + 1], alphas[a], alphas[a + 1]) for a in window[:-1])
    params = fm.KeyParams(**report["params"])
    seconds = []
    for index in list(window)[:6]:
        start = time.perf_counter()
        fm.soft_matte(rgb[index], params, np.asarray(report["frames"][index]["key"], np.float32))
        seconds.append(time.perf_counter() - start)
    keyer_seconds = statistics.median(seconds)
    summary = {"fringe": fringe, "leak": leak, "flips": round(flips, 3), "keyer_s": round(keyer_seconds, 3),
               "pipeline_s_per_frame": round(pipeline_seconds, 3), "decision": report["interior_despill"]}
    print(json.dumps(summary))
    assert fringe == 0 and leak == 0, summary
    assert flips <= 5.7, summary
    assert keyer_seconds <= 0.8, summary
