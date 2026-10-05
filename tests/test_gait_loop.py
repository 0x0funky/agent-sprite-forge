"""B07-video-motion: gait_loop select and measure-stride on synthetic, deterministic clips.

The eight runner cases port the report v2 prototype synth.py (P0-4); idle, hover,
push-in, held-frame and treadmill clips are drawn here. The real-clip acceptance (Ryo
bench: 16 frames from 81-84, frames 0-25 unusable, six invalid helper candidates
rejected) is the opt-in tests/benchmarks/loop_bench.py.
"""
from __future__ import annotations

import json
import math
import re
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from forge_testutils import assert_cli_help, assert_valid_contract, load_script, run_cli, script_path

G = load_script("video2dsprite", "gait_loop")
SKILL = "video2dsprite"

# --------------------------------------------------------------------------- runners (port of synth.py)

W = H = 256
NAVY, NAVY_FAR, STRIPE, TEAL, SKIN, BOOT = ((40, 52, 84), (28, 36, 60), (40, 190, 170), (36, 128, 126),
                                           (240, 200, 170), (110, 70, 40))


def _limb(d, x0, y0, ang, length, width, color, stripe=None, foot=None, knee=0.0):
    xk, yk = x0 + 0.5 * length * math.sin(ang), y0 + 0.5 * length * math.cos(ang)
    x1, y1 = xk + 0.5 * length * math.sin(ang - knee), yk + 0.5 * length * math.cos(ang - knee)
    d.line((x0, y0, xk, yk), fill=color, width=width)
    d.line((xk, yk, x1, y1), fill=color, width=width)
    d.ellipse((xk - width / 2, yk - width / 2, xk + width / 2, yk + width / 2), fill=color)
    if stripe:
        d.line((x0 + 3, y0, xk + 3, yk), fill=stripe, width=3)
        d.line((xk + 3, yk, x1 + 3, y1), fill=stripe, width=3)
    if foot:
        d.ellipse((x1 - 9, y1 - 6, x1 + 13, y1 + 6), fill=foot)


def runner_frame(t, P, *, distinct, swap=False, dx=0.0, jitter=0.0, rng=None, random_pose=False, asym_boots=False):
    """A side-view runner whose legs and arms swing in antiphase with stride period P frames."""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ph = 2 * math.pi * t / P
    if random_pose:
        ph = rng.uniform(0, 2 * math.pi)
    bob = 6 * math.cos(2 * ph)
    cx, hip = 128 + dx, 150 + bob
    j = rng.normal(0, jitter) if (rng is not None and jitter) else 0.0
    if not distinct:
        ph += j
        j = 0.0
    a_near, a_far = 0.6 * math.sin(ph) + j, 0.6 * math.sin(ph + math.pi) - j
    if swap:  # leg-identity glitch: the near leg takes the far leg's phase
        a_near, a_far = a_far, a_near
    k_near = 1.1 * max(0.0, math.cos(ph + (math.pi if swap else 0.0)))
    k_far = 1.1 * max(0.0, math.cos(ph + (0.0 if swap else math.pi)))
    far_col = NAVY_FAR if distinct else NAVY
    arm_far, arm_near = -0.7 * math.sin(ph + math.pi), -0.7 * math.sin(ph)
    alike_legs = not distinct and not asym_boots
    _limb(d, cx, hip, a_far, 70, 18, far_col, foot=None if alike_legs else BOOT, knee=k_far)
    if distinct:
        _limb(d, cx, hip - 55, arm_far, 45, 12, (28, 100, 98))
    d.rounded_rectangle((cx - 22, hip - 75, cx + 22, hip + 4), 8, fill=TEAL)
    d.ellipse((cx - 26, hip - 125, cx + 26, hip - 73), fill=SKIN)
    _limb(d, cx, hip, a_near, 70, 18, NAVY, stripe=STRIPE if distinct else None,
          foot=None if alike_legs else BOOT, knee=k_near)
    if alike_legs:
        for a_, k_ in ((a_far, k_far), (a_near, k_near)):
            xk, yk = cx + 35 * math.sin(a_), hip + 35 * math.cos(a_)
            x1, y1 = xk + 35 * math.sin(a_ - k_), yk + 35 * math.cos(a_ - k_)
            d.ellipse((x1 - 9, y1 - 6, x1 + 13, y1 + 6), fill=BOOT)
    if not distinct:
        _limb(d, cx, hip - 55, arm_far, 45, 12, TEAL)
    _limb(d, cx, hip - 55, arm_near, 45, 12, TEAL)
    return im


CASES = {
    # name: (P, n, state, kwargs, expectation) -- the prototype's synth.py table
    "distinct_P16": (16, 96, "run", {"distinct": True, "jitter": 0.02},
                     {"verdict": "full-stride", "T": 16, "window_len": [15, 16, 17]}),
    "alike_P16": (16, 96, "run", {"distinct": False, "jitter": 0.02},
                  {"verdict": "one-step-double-it", "T": 8, "window_len": [15, 16, 17]}),
    "alike_P16_boots_overlap": (16, 96, "run", {"distinct": False, "jitter": 0.02, "asym_boots": True},
                                {"verdict_any": ["one-step-double-it", "full-stride-legs-alike"],
                                 "window_len": [15, 16, 17]}),
    "alike_P24": (24, 120, "run", {"distinct": False, "jitter": 0.02},
                  {"verdict": "ambiguous", "T": 12, "window_len": [23, 24, 25]}),
    "alike_P24_walk": (24, 120, "walk", {"distinct": False, "jitter": 0.02},
                       {"verdict": "one-step-double-it", "T": 12, "window_len": [23, 24, 25]}),
    "distinct_P16_glitch": (16, 112, "run", {"distinct": True, "jitter": 0.02, "glitch": (40, 48)},
                            {"verdict": "full-stride", "T": 16, "window_len": [15, 16, 17], "avoid": [38, 50]}),
    "distinct_P16_drift": (16, 96, "run", {"distinct": True, "jitter": 0.02, "drift": 0.5},
                           {"verdict": "full-stride", "T": 16, "window_len": [15, 16, 17]}),
    "random_poses": (16, 96, "run", {"distinct": True, "random_pose": True}, {"verdict": "no-period-or-low-confidence"}),
}


def render_runner(case: str, out: Path, *, hold: int = 1, frames: int | None = None) -> Path:
    """Render a case; ``hold`` > 1 repeats every pose (12 fps content in a 24 fps clip)."""
    P, n, _, kw, _ = CASES[case]
    n = frames or n
    rng = np.random.default_rng(7)
    out.mkdir(parents=True, exist_ok=True)
    g0, g1 = kw.get("glitch", (-1, -1))
    drift = kw.get("drift", 0.0)
    for t in range(0, n, hold):
        image = runner_frame(t, P, distinct=kw["distinct"], swap=g0 <= t < g1, asym_boots=kw.get("asym_boots", False),
                             dx=drift * t - drift * n / 2, jitter=kw.get("jitter", 0.0), rng=rng,
                             random_pose=kw.get("random_pose", False))
        for copy in range(hold):
            image.save(out / f"frame_{t + copy + 1:04d}.png")
    return out


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    """Rendered and analysed runner cases, computed once per module."""
    cache = {}

    def get(case):
        if case not in cache:
            directory = render_runner(case, tmp_path_factory.mktemp(case))
            try:
                analysis = G.analyse(directory, 24, "gait", CASES[case][2])
                cache[case] = (directory, analysis, G.recommend(analysis), None)
            except ValueError as error:
                cache[case] = (directory, None, None, error)
        return cache[case]

    return get


@pytest.fixture(scope="module")
def small_runner(tmp_path_factory):
    """Three strides (48 frames) of the distinct runner: enough for the CLI tests."""
    out = tmp_path_factory.mktemp("small")
    rng = np.random.default_rng(7)
    for t in range(48):
        runner_frame(t, 16, distinct=True, jitter=0.02, rng=rng).save(out / f"run_{t:04d}.png")
    return out


@pytest.fixture(scope="module")
def random_poses(tmp_path_factory):
    out = tmp_path_factory.mktemp("random")
    rng = np.random.default_rng(7)
    for t in range(48):
        runner_frame(t, 16, distinct=True, random_pose=True, rng=rng).save(out / f"pose_{t:04d}.png")
    return out


# --------------------------------------------------------------------------- idle, hover, treadmill

def idle_frame(t, *, period=48, head_shift=0.0, scale=1.0, bob=0.0, size=192, supersample=2):
    """A standing figure that breathes (torso width and head height follow a sine), drawn at
    2x and box-reduced so motion is sub-pixel."""
    S = supersample
    im = Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    breath = math.sin(2 * math.pi * t / period)
    lift = bob * math.sin(2 * math.pi * t / period)

    def at(x, y):
        return (96 + (x - 96) * scale) * S, (176 + (y - 176) * scale - lift) * S

    def shape(x0, y0, x1, y1, fill, ellipse=False):
        (p0, q0), (p1, q1) = at(x0, y0), at(x1, y1)
        (d.ellipse if ellipse else d.rectangle)((p0, q0, p1, q1), fill=fill)

    shape(84, 130, 93, 176, (50, 60, 90))
    shape(99, 130, 108, 176, (50, 60, 90))
    w = 22 + 2.0 * breath
    shape(96 - w, 80 - 1.5 * breath, 96 + w, 134, (40, 130, 120), ellipse=True)
    shape(96 - w - 9, 86, 96 - w + 1, 128, (230, 190, 160))
    shape(96 + w - 1, 86, 96 + w + 9, 128, (230, 190, 160))
    hx = 96 + head_shift * t
    shape(hx - 16, 46 - 1.2 * breath, hx + 16, 80 - 1.2 * breath, (240, 200, 170), ellipse=True)
    shape(hx - 6, 58 - 1.2 * breath, hx - 2, 64 - 1.2 * breath, (30, 30, 30))
    return im.resize((size, size), Image.Resampling.BOX)


def render_idle(out: Path, n: int, *, push_from: int | None = None, **kw) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    for t in range(n):
        scale = 1.0 + (0.005 * (t - push_from) if push_from is not None and t > push_from else 0.0)
        idle_frame(t, scale=scale, **kw).save(out / f"idle_{t:04d}.png")
    return out


def treadmill_frame(t, *, period=16, speed=4.0, rest=False, supersample=4):
    """Two legs on a treadmill: the planted foot moves back ``speed`` px per frame on the floor,
    the other swings forward 10 px above it. ``rest`` stands with both feet under the hip."""
    S, half = supersample, period // 2
    im = Image.new("RGBA", (160 * S, 120 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for leg, color in ((0, (40, 52, 84)), (1, (28, 36, 60))):
        phase = (t + leg * half) % period
        if rest:
            x, lift = 80.0, 0.0
        elif phase < half:
            x, lift = 80 + speed * (half / 2 - phase), 0.0
        else:
            x, lift = 80 - speed * (half / 2 - (phase - half)), 10.0
        d.line([v * S for v in (80, 70, x, 100 - lift)], fill=color, width=7 * S)
        d.ellipse([v * S for v in (x - 7, 100 - lift, x + 9, 106 - lift)], fill=(110, 70, 40))
    d.rectangle([v * S for v in (70, 30, 90, 72)], fill=(36, 128, 126))
    d.ellipse([v * S for v in (68, 8, 92, 32)], fill=(240, 200, 170))
    return im.resize((160, 120), Image.Resampling.BOX)


def render_treadmill(out: Path, frames: int = 48) -> Path:
    """Frame 0 is the rest pose; frames 1..frames walk on the treadmill (4 px per frame)."""
    out.mkdir(parents=True, exist_ok=True)
    treadmill_frame(0, rest=True).save(out / "step_0000.png")
    for t in range(1, frames + 1):
        treadmill_frame(t).save(out / f"step_{t:04d}.png")
    return out


def _select(directory: Path, out: Path, *extra: str) -> int:
    return G.main(["select", "--frames-dir", str(directory), "--fps", "24", "--output-dir", str(out), *extra])


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_no_absolute_paths(text: str) -> None:
    assert not re.search(r'"(?:[A-Za-z]:[\\/]|/|\\\\)', text), "manifest holds an absolute path"


def _no_stage_left(parent: Path) -> None:
    assert not [p for p in parent.iterdir() if ".stage-" in p.name], "a staging directory was left behind"


# --------------------------------------------------------------------------- B07-T1: gait period and windows

@pytest.mark.parametrize("case", list(CASES))
def test_synthetic_runner_cases(runner, case):
    """B07-T1 (report v2 P0-4, synth.py): verdict, period and loop length of each runner."""
    expected = CASES[case][4]
    _, analysis, rec, error = runner(case)
    if expected.get("verdict") == "no-period-or-low-confidence":
        assert (error is not None and "no gait period" in str(error)) or rec["confidence"]["label"] == "low"
        return
    assert error is None, error
    verdict = analysis.period["verdict"]
    if "verdict_any" in expected:
        assert verdict in expected["verdict_any"]
    else:
        assert verdict == expected["verdict"]
        assert abs(analysis.period["T_frames"] - expected["T"]) <= 0.6
    window = rec["window"]
    assert window["frames"] in expected["window_len"]
    assert window["classification"]["class"] == "valid-1-cycle"
    if "avoid" in expected:
        a, b = expected["avoid"]
        assert window["endExclusive"] <= a or window["start"] >= b


@pytest.mark.parametrize("case", ["distinct_P16", "alike_P16"])
def test_eight_frame_single_step_is_rejected(runner, case):
    """report v2 P0-4: an 8-frame single step is rejected even though its wrap looks ordinary."""
    _, analysis, rec, _ = runner(case)
    start = rec["window"]["start"]
    window = G.classify_window(analysis, start, start + 8)
    assert window["classification"]["class"] == "rejected"
    assert any(reason.startswith("half stride") for reason in window["classification"]["reasons"])
    assert 0.5 <= window["wrap_ratio"] <= 2.0


def test_two_stride_window_is_a_valid_2_cycle(runner):
    _, analysis, _, _ = runner("distinct_P16")
    assert G.classify_window(analysis, 10, 42)["classification"]["class"] == "valid-2-cycle"
    three = G.classify_window(analysis, 10, 58)["classification"]
    assert three["class"] == "rejected" and "3.00 strides" in three["reasons"][0]


def test_identity_glitch_frames_are_unusable(runner):
    """Frames whose legs swap identity (no alternation) are unusable, and loops avoid them."""
    _, analysis, rec, _ = runner("distinct_P16_glitch")
    assert set(range(40, 48)) <= set(analysis.unusable)
    window = G.classify_window(analysis, 36, 52)
    assert window["classification"]["class"] == "rejected"
    assert "unusable frames" in window["classification"]["reasons"][0]


def test_root_drift_lowers_confidence_and_is_noted(runner):
    _, _, rec, _ = runner("distinct_P16_drift")
    assert rec["confidence"]["label"] == "low"
    assert any("root drifts" in note for note in rec["window"]["classification"]["notes"])


def test_gait_never_pingpongs(small_runner, tmp_path, capsys):
    assert _select(small_runner, tmp_path / "pp", "--policy", "pingpong", "--no-aids") == 1
    assert "walks never ping-pong" in capsys.readouterr().err
    assert not (tmp_path / "pp").exists()
    _no_stage_left(tmp_path)


# --------------------------------------------------------------------------- selection, report, aids

def test_select_writes_contract_valid_selection_report_and_aids(small_runner, tmp_path):
    """B07-T1: frame_selection v2 with 0-based indices and file names, a QA envelope, a
    forge_core seam report and the four review aids."""
    directory = small_runner
    out = tmp_path / "loop"
    assert _select(directory, out, "--evaluate", "0:8") == 0
    selection = _json(out / "selection.json")
    assert_valid_contract(selection, "video", "frame_selection_v2", skill=SKILL)
    start, end = selection["start"], selection["endExclusive"]
    names = [path.name for path in sorted(directory.glob("*.png"))]
    assert selection["sourceFiles"] == names[start:end]
    assert selection["sourceIndices"] == list(range(start, end))
    assert sum(selection["durations_ms"]) == round((end - start) * 1000 / 24)
    assert selection["fps"] == "24/1" and selection["loopPolicy"] == "cycle"
    assert selection["status"] == "selected-needs-visual-review"
    assert_valid_contract(selection["seam"], "common", "seamReport", skill=SKILL)
    report = _json(out / "loop-report.json")
    assert_valid_contract(report["qa"], "common", "qaEnvelope", skill=SKILL)
    assert report["qa"]["status"] == "needs-visual-review" and report["qa"]["notProven"]
    assert report["evaluated"][0]["classification"]["class"] == "rejected"
    assert report["period"]["verdict"] == "full-stride"
    for name in ("loop3x.gif", "seam.png", "onion.png", "timeline.png"):
        assert (out / "aids" / name).is_file()
    with Image.open(out / "aids" / "loop3x.gif") as gif:
        assert gif.n_frames == 3 * len(selection["sourceIndices"])
    for name in ("selection.json", "loop-report.json"):
        _assert_no_absolute_paths((out / name).read_text(encoding="utf-8"))


def test_select_output_is_byte_deterministic(small_runner, tmp_path):
    for name in ("a", "b"):
        assert _select(small_runner, tmp_path / name) == 0
    for rel in ("selection.json", "loop-report.json", "aids/loop3x.gif", "aids/seam.png", "aids/onion.png",
                "aids/timeline.png"):
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes(), rel


# --------------------------------------------------------------------------- B07-T2: idle, hover, holds, drift

IDLE_CLIPS = {"closes": ("idle", 100, {}), "no_closure": ("idle", 96, {"head_shift": 0.25}),
              "push_in": ("idle", 90, {"push_from": 60}), "hover": ("hover", 72, {"bob": 6.0, "period": 24})}


@pytest.fixture(scope="module")
def idle_clips(tmp_path_factory):
    """Rendered and analysed idle/hover clips (directory, analysis), computed once."""
    root = tmp_path_factory.mktemp("idle")
    cache = {}

    def get(name):
        if name not in cache:
            kind, frames, options = IDLE_CLIPS[name]
            directory = render_idle(root / name, frames, **options)
            cache[name] = (directory, G.analyse(directory, 24, kind))
        return cache[name]

    return get


def test_idle_breathing_cycle_closes(idle_clips):
    _, analysis = idle_clips("closes")
    rec = G.recommend(analysis)
    assert rec["policy"] == "cycle"
    assert rec["window"]["frames"] == 48  # the breathing period
    assert rec["window"]["closure_steps"] <= 0.05
    assert rec["window"]["classification"]["class"] == "valid-cycle"


def test_breathing_without_closure_falls_back_to_pingpong(idle_clips, tmp_path):
    """B07-T2: a breathing clip whose head keeps turning never closes, so it ping-pongs."""
    _, analysis = idle_clips("no_closure")
    assert G.search_ambient(analysis) == []
    rec = G.recommend(analysis)
    assert rec["policy"] == "pingpong" and rec["window"]["start"] == 0
    window = rec["window"]
    indices = G.kept_indices(analysis, window["start"], window["endExclusive"])
    selection = G.build_selection(analysis.clip, tmp_path, indices,
                                  G.kept_durations(window["start"], window["endExclusive"], indices, analysis.clip.fps),
                                  policy=rec["policy"], method="test", review=" ".join(rec["confidence"]["review"]),
                                  kind="idle", window=(window["start"], window["endExclusive"]))
    assert_valid_contract(G.jsonable(selection), "video", "frame_selection_v2", skill=SKILL)
    assert selection["sourceIndices"] == sorted(selection["sourceIndices"])  # forward frames; runtimes mirror
    assert "pingpong" in selection["reviewReason"]
    with pytest.raises(ValueError, match="closes"):
        G.recommend(analysis, "cycle")


def test_push_in_recommended_range_ends_before_drift(idle_clips):
    """B07-T2 (hd2d CHARACTER-QA): on a push-in the usable range and the loop end before the drift."""
    _, analysis = idle_clips("push_in")
    assert analysis.usable["endExclusive"] <= 66 and "scale" in analysis.usable["reason"]
    rec = G.recommend(analysis)
    assert rec["window"]["endExclusive"] <= 61  # the push-in starts after frame 60
    late = G.classify_window(analysis, 30, 78)
    assert any("drift-free range" in reason for reason in late["classification"]["reasons"])


def test_hover_bob_is_motion_not_drift(idle_clips):
    _, analysis = idle_clips("hover")
    assert analysis.usable["endExclusive"] == 72
    rec = G.recommend(analysis)
    assert rec["policy"] == "cycle" and rec["window"]["frames"] in (24, 48)


def test_near_duplicate_holds_are_dropped_with_exact_timing(tmp_path):
    """B07-T2 (pixelate.py dedupe): 12 fps content in a 24 fps clip keeps one frame per pose."""
    directory = render_runner("distinct_P16", tmp_path / "held", hold=2, frames=64)
    out = tmp_path / "loop"
    assert _select(directory, out, "--no-aids") == 0
    selection = _json(out / "selection.json")
    report = _json(out / "loop-report.json")
    assert report["holds"] == list(range(1, 64, 2))
    start, end = selection["start"], selection["endExclusive"]
    kept = selection["sourceIndices"]
    assert all(index % 2 == 0 for index in kept[1:]) and len(kept) <= (end - start + 1) // 2 + 1
    assert sum(selection["durations_ms"]) == round((end - start) * 1000 / 24)
    assert_valid_contract(selection, "video", "frame_selection_v2", skill=SKILL)
    every = tmp_path / "every"
    assert _select(directory, every, "--no-aids", "--dedupe-cap", "0") == 0
    plain = _json(every / "selection.json")
    assert plain["sourceIndices"] == list(range(plain["start"], plain["endExclusive"]))


def test_detect_holds_ignores_smooth_motion():
    steps = np.array([3.0, 3.1, 0.05, 3.0, 2.9, 3.2, 0.04, 3.1])
    holds = G.detect_holds(steps, G.DEDUPE_CAP)
    assert np.nonzero(holds)[0].tolist() == [3, 7]
    assert not G.detect_holds(np.linspace(0.2, 0.3, 8), G.DEDUPE_CAP).any()  # slow but steady motion
    assert not G.detect_holds(steps, 0).any()


# --------------------------------------------------------------------------- B07-T5: stride and entry frame

@pytest.fixture(scope="module")
def treadmill(tmp_path_factory):
    return render_treadmill(tmp_path_factory.mktemp("treadmill"))


def test_treadmill_stride_measures_4px_per_frame(treadmill, tmp_path):
    """B07-T5: a 4 px/frame treadmill measures 4 +- 0.25; the entry frame is rest-like."""
    out = tmp_path / "stride"
    assert G.main(["measure-stride", "--frames-dir", str(treadmill), "--fps", "24", "--output-dir", str(out),
                   "--range", "1:49", "--rest-frame", "0"]) == 0
    report = _json(out / "stride.json")
    assert abs(report["stride"]["stridePxPerFrame"] - 4.0) <= 0.25
    assert report["stride"]["direction"].startswith("ground moves left")
    assert_valid_contract(report["qa"], "common", "qaEnvelope", skill=SKILL)
    entry = report["entryFrame"]
    xors = [frame["xorRatio"] for frame in entry["perFrame"]]
    assert entry["xorRatio"] == min(xors) and entry["xorRatio"] < 0.05
    assert entry["sourceIndex"] % 8 == 4  # the passing pose: both feet under the hip, like the rest pose


def test_measure_stride_adds_stride_fields_to_a_selection(treadmill, tmp_path):
    loop = tmp_path / "loop"
    assert _select(treadmill, loop, "--state", "walk", "--no-aids") == 0
    out = tmp_path / "stride"
    assert G.main(["measure-stride", "--frames-dir", str(treadmill), "--fps", "24", "--output-dir", str(out),
                   "--selection", str(loop / "selection.json"), "--px-per-unit", "32"]) == 0
    selection = _json(out / "selection.json")
    assert_valid_contract(selection, "video", "frame_selection_v2", skill=SKILL)
    frames = selection["endExclusive"] - selection["start"]
    assert abs(selection["stridePxPerFrame"] - 4.0) <= 0.25
    assert selection["cadenceMs"] == sum(selection["durations_ms"])
    assert selection["strideWorldUnits"] == pytest.approx(selection["stridePxPerFrame"] * frames / 32)
    assert selection["speedRef"] == pytest.approx(selection["strideWorldUnits"] * 1000 / selection["cadenceMs"])
    assert 0 <= selection["entryFrame"] < len(selection["sourceIndices"])


def test_measure_stride_needs_ground_contact(idle_clips, tmp_path, capsys):
    """A hovering body never plants a foot: no stride, nothing published."""
    out = tmp_path / "stride"
    hover, _ = idle_clips("hover")
    for band in ([], ["--band-rows", "1"]):
        assert G.main(["measure-stride", "--frames-dir", str(hover), "--fps", "24", "--output-dir", str(out),
                       *band]) == 1
        assert "no stride to measure" in capsys.readouterr().err
        assert not out.exists()


# --------------------------------------------------------------------------- helpers and selection I/O

def test_parse_fps_and_durations():
    assert G.parse_fps("24000/1001") == Fraction(24000, 1001)
    assert G.parse_fps(12.5) == Fraction(25, 2)
    for bad in ("0", "-1", "abc", True, float("nan"), "1/0"):
        with pytest.raises(ValueError):
            G.parse_fps(bad)
    assert sum(G.source_durations(16, Fraction(24))) == 667
    assert G.kept_durations(10, 26, [10, 12, 14, 15, 20], Fraction(24)) == [83, 84, 41, 209, 250]
    assert sum(G.kept_durations(10, 26, [10, 12, 14, 15, 20], Fraction(24))) == 667


def test_pairwise_l1_numpy_fallback_matches_scipy(monkeypatch):
    rng = np.random.default_rng(3)
    features = rng.random((9, 50))
    expected = np.abs(features[:, None, :] - features[None, :, :]).sum(axis=-1)
    assert np.allclose(G.pairwise_l1(features), expected)
    monkeypatch.setenv("FORGE_CORE_NO_SCIPY", "1")
    assert np.allclose(G.pairwise_l1(features), expected)


def test_read_selection_v1_and_malformed_v2(tmp_path):
    hashes = ["a" * 64, "b" * 64]
    v1 = {"schema": "forge-frame-selection/v1", "sourceDirectory": "/x", "start": 3, "endExclusive": 5, "fps": 24,
          "sourceHashes": hashes, "status": "selected-needs-visual-review"}
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(v1), encoding="utf-8")
    parsed = G.read_selection(path)
    assert parsed["indices"] == [3, 4] and sum(parsed["durations"]) == 83
    v2 = {**v1, "schema": "forge-frame-selection/v2", "sourceIndices": [3, 4, 4], "durations_ms": [40, 40, 40],
          "loopPolicy": "oneshot", "events": [], "method": "test"}
    for change in ({"sourceIndices": [3, 5]}, {"durations_ms": [40, 0, 40]}, {"loopPolicy": "loop"},
                   {"sourceHashes": hashes[:1]}, {"start": True}, {"events": None}):
        path.write_text(json.dumps({**v2, **change}), encoding="utf-8")
        with pytest.raises(ValueError):
            G.read_selection(path)
    path.write_text(json.dumps(v2), encoding="utf-8")
    assert G.read_selection(path)["indices"] == [3, 4, 4]


# --------------------------------------------------------------------------- CLI conventions (plan Appendix D/E)

def test_gait_loop_help_is_ascii_under_cp1252_and_cp950():
    assert_cli_help(SKILL, "gait_loop")


def test_select_refuses_an_existing_output(small_runner, tmp_path):
    directory = small_runner
    out = tmp_path / "taken"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    assert _select(directory, out, "--no-aids") == 1
    assert [p.name for p in out.iterdir()] == ["keep.txt"]
    stride = G.main(["measure-stride", "--frames-dir", str(directory), "--fps", "24", "--output-dir", str(out)])
    assert stride == 1 and [p.name for p in out.iterdir()] == ["keep.txt"]


def test_select_failure_publishes_nothing(random_poses, tmp_path):
    """No period (random poses) is a QC failure: exit 1, no output, no stage left behind."""
    result = run_cli([script_path(SKILL, "gait_loop"), "select", "--frames-dir", random_poses, "--fps", "24",
                      "--output-dir", tmp_path / "out"], "cp1252")
    assert result.returncode == 1
    assert result.stderr.startswith("error: no gait period")
    assert not (tmp_path / "out").exists()
    _no_stage_left(tmp_path)


def test_select_cli_prints_one_ascii_json_line(small_runner, tmp_path):
    result = run_cli([script_path(SKILL, "gait_loop"), "select", "--frames-dir", small_runner, "--fps", "24",
                      "--output-dir", tmp_path / "out", "--no-aids"], "cp1252")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].isascii()
    summary = json.loads(lines[0])
    assert Path(summary["selection"]).is_file() and Path(summary["metadata"]).is_file()
    assert summary["frames"] == summary["endExclusive"] - summary["start"]


def test_internal_errors_are_one_line(monkeypatch, capsys):
    """D27: an exception that is not a refused input prints 'error: internal error (<Type>: <msg>)', no traceback."""
    monkeypatch.setattr(G, "cmd_select", lambda args: {}["boom"])
    assert G.main(["select", "--frames-dir", "frames", "--fps", "24", "--output-dir", "out"]) == 1
    assert capsys.readouterr().err.strip() == "error: internal error (KeyError: 'boom')"


def test_file_refs_are_forge_core_file_refs(tmp_path):
    """D30: gait_loop's fileRef and directory reference are forge_core.file_ref and manifest_path."""
    target = tmp_path / "a" / "frame.png"
    target.parent.mkdir()
    target.write_bytes(b"png")
    assert G.file_ref(target, tmp_path) == G.forge_core.file_ref(target, tmp_path)
    assert G.file_ref(target, tmp_path)["path"] == "a/frame.png" and G.file_ref(target, tmp_path)["bytes"] == 3
    assert G.directory_ref(target.parent, tmp_path) == "a" and G.round_half_up is G.forge_core.round_half_up
