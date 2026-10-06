"""B07-video-motion: retime.py (spans, impact/hold, three-key map, ticks, loop policies, cadence).

Frames are small synthetic RGBA strips; the timing tests only need frame counts and
hashes, the contact tests draw a strike with a known reach peak and a walk with a known
widest stance. Source cases: Dusk review-overrides.json (spans, impactSourceFrame),
process-fx-v160.py (three-key map), game-opus55 a_homura_atk1 (27 tick rows).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from forge_testutils import assert_cli_help, assert_valid_contract, load_script, run_cli, script_path

R = load_script("video2dsprite", "retime")
SKILL = "video2dsprite"
DUSK_ATTACK = [12, 15, 18, 21, 24, 27, 30, 33, 36, 38, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 52, 56, 64,
               70, 72, 74, 76, 78, 80, 82, 84, 86, 88]
DUSK_ATTACK_SPANS = "12:36:3,36:40:2,40:51,52,56,64,70:89:2"
DUSK_HURT = [12, 13, 14, 15, 16, 17, 18, 19, 20, 20, 20, 19, 18, 17, 16, 15, 14, 13, 12]


def strike_frame(t: int, peak: int = 44) -> Image.Image:
    """A body with an arm whose reach peaks at frame ``peak``."""
    image = Image.new("RGBA", (64, 48))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 10, 28, 40), fill=(40, 120, 200, 255))
    reach = 2 + max(0, 14 - 2 * abs(t - peak))
    draw.rectangle((29, 20, 29 + reach, 23), fill=(220, 180, 140, 255))
    draw.point((t % 18, 46), fill=(10, 10, 10, 255))  # every frame differs
    return image


def walk_frame(t: int) -> Image.Image:
    """Two feet whose spread follows |sin|: widest at t = 4 (mod 8)."""
    image = Image.new("RGBA", (64, 48))
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 6, 36, 34), fill=(40, 120, 200, 255))
    spread = round(12 * abs(math.sin(2 * math.pi * t / 16)))
    for x in (32 - spread, 32 + spread):
        draw.line((32, 34, x, 44), fill=(50, 60, 90, 255), width=3)
        draw.rectangle((x - 2, 43, x + 3, 46), fill=(110, 70, 40, 255))
    return image


@pytest.fixture(scope="module")
def strike(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("strike")
    for t in range(120):
        strike_frame(t).save(out / f"src_{t:04d}.png")
    return out


@pytest.fixture(scope="module")
def walk(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("walk")
    for t in range(32):
        walk_frame(t).save(out / f"walk_{t:04d}.png")
    return out


def retime(frames: Path, out: Path, *args: str) -> int:
    return R.main(["--frames-dir", str(frames), "--fps", "24", "--output-dir", str(out), *args])


def outputs(out: Path) -> tuple[dict, dict]:
    selection = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    report = json.loads((out / "retime-report.json").read_text(encoding="utf-8"))
    assert_valid_contract(selection, "video", "frame_selection_v2", skill=SKILL)
    assert_valid_contract(report["qa"], "common", "qaEnvelope", skill=SKILL)
    assert selection["status"] == "selected-needs-visual-review"
    return selection, report


# --------------------------------------------------------------------------- B07-T4 acceptance tests

def test_spans_exact_indices(strike, tmp_path):
    """Dusk traveler-attack and traveler-hurt sourceIndices, written as spans."""
    assert R.parse_spans(DUSK_ATTACK_SPANS, 120) == DUSK_ATTACK
    assert R.parse_spans("12:21,20*2,19:11:-1", 120) == DUSK_HURT
    for bad in ("5:5", "9:3", "a:b", "3*0", "1:5:0", "", "200", "1,,2"):
        with pytest.raises(ValueError):
            R.parse_spans(bad, 120)
    assert retime(strike, tmp_path / "attack", "--kind", "attack", "--spans", DUSK_ATTACK_SPANS) == 0
    selection, _ = outputs(tmp_path / "attack")
    assert selection["sourceIndices"] == DUSK_ATTACK
    assert (selection["start"], selection["endExclusive"]) == (12, 89)
    assert len(selection["sourceHashes"]) == len(selection["sourceFiles"]) == 89 - 12


def test_impact_ms_from_nearest_selected_frame(strike, tmp_path):
    """impactMs is the start of the output frame whose source index is nearest the impact
    source frame (Dusk process-clips.py: round(nearest / count * duration))."""
    out = tmp_path / "attack"
    assert retime(strike, out, "--kind", "attack", "--spans", DUSK_ATTACK_SPANS, "--duration", "950",
                  "--impact-source", "44", "--hold-source", "54") == 0
    selection, report = outputs(out)
    assert selection["impactMs"] == round(14 / 34 * 950) == 391
    assert selection["holdMs"] == round(21 / 34 * 950)  # 54 is as near 52 as 56: the earlier frame wins
    hit = [event for event in selection["events"] if event["name"] == "hit"]
    assert hit == [{"name": "hit", "atMs": 391, "frame": 14}]
    assert report["timeline"][14]["sourceIndex"] == 44 and report["timeline"][14]["startMs"] == 391
    assert retime(strike, tmp_path / "far", "--kind", "attack", "--spans", "12:40", "--impact-source", "90") == 1
    assert not (tmp_path / "far").exists()


def test_three_key_map_hits_peak_at_impact(strike, tmp_path):
    """process-fx-v160.py: start/peak@ms/end over --duration at --output-fps; the frame that
    starts at the impact ms shows the peak source frame."""
    out = tmp_path / "fx"
    assert retime(strike, out, "--kind", "fx", "--map", "0.65s/1.65s@350/4.5s", "--duration", "1400",
                  "--output-fps", "40") == 0
    selection, report = outputs(out)
    assert len(selection["sourceIndices"]) == 56
    assert selection["sourceIndices"][14] == round(1.65 * 24)  # source frame 40 at output frame 14
    assert selection["impactMs"] == 350 and report["timeline"][14]["startMs"] == 350
    assert selection["sourceIndices"][0] == round(0.65 * 24) and selection["sourceIndices"][-1] == 108
    assert selection["sourceIndices"] == sorted(selection["sourceIndices"])
    assert {"name": "hit", "atMs": 350, "frame": 14} in selection["events"]
    frames, impact, info = R.three_key_map("16/44@300/100", 900, R.gait_loop.parse_fps(30), R.gait_loop.parse_fps(24), 120)
    assert frames[impact] == 44 and info["outputFrames"] == 27 and frames[0] == 16 and frames[-1] == 100


@pytest.mark.parametrize("count,duration", [(34, 950), (19, 520), (16, 452), (7, 1000), (60, 61), (3, 3)])
def test_durations_sum_to_duration(strike, tmp_path, count, duration):
    out = tmp_path / "timing"
    assert retime(strike, out, "--kind", "other", "--spans", f"0:{count}", "--duration", str(duration)) == 0
    selection, _ = outputs(out)
    durations = selection["durations_ms"]
    assert sum(durations) == duration and min(durations) >= 1 and max(durations) - min(durations) <= 1
    assert all(isinstance(value, int) for value in durations)


def test_walk_rejects_pingpong(walk, tmp_path, capsys):
    """Walks never ping-pong (a mirrored gait moonwalks) and never play backwards."""
    out = tmp_path / "walk"
    assert retime(walk, out, "--kind", "walk", "--spans", "0:16", "--policy", "pingpong") == 1
    assert "walks never ping-pong" in capsys.readouterr().err
    assert not out.exists()
    assert retime(walk, out, "--kind", "run", "--spans", "0:8,7:0:-1") == 1
    assert "plays forward" in capsys.readouterr().err
    assert retime(walk, out, "--kind", "walk", "--spans", "0:16") == 0
    assert outputs(out)[0]["loopPolicy"] == "cycle"


def test_cadence_452ms_for_1_65_over_3_65(walk, tmp_path):
    """Dusk walk metadata: cadence = 1000 x stride / speed = 452 ms for 1.65 units at 3.65 units/s."""
    out = tmp_path / "walk"
    assert retime(walk, out, "--kind", "walk", "--spans", "0:16", "--stride", "1.65", "--speed", "3.65") == 0
    selection, report = outputs(out)
    assert selection["cadenceMs"] == 452 and sum(selection["durations_ms"]) == 452
    assert selection["strideWorldUnits"] == 1.65 and selection["speedRef"] == 3.65 and selection["cycles"] == 1
    assert report["cadence"]["exactCadenceMs"] == pytest.approx(1000 * 1.65 / 3.65)
    # the same cadence from a selection, over two cycles: durations keep their proportions
    again = tmp_path / "again"
    assert retime(walk, again, "--selection", str(out / "selection.json"), "--stride", "1.65", "--speed", "3.65",
                  "--cycles", "2") == 0
    selection, _ = outputs(again)
    assert sum(selection["durations_ms"]) == round(2 * 1000 * 1.65 / 3.65) == 904
    assert selection["cadenceMs"] == 452 and selection["cycles"] == 2 and selection["kind"] == "walk"


# --------------------------------------------------------------------------- ticks, events, policies, contact

def test_ticks_rows_with_in_hit_cancel_end_events(strike, tmp_path):
    """game-opus55 a_homura_atk1: 15 frames on 27 ticks (60 Hz) with in/hit/cancel/end events."""
    rows = "1,1,1,1,2,1,2,1,1,1,2,3,3,3,4"
    out = tmp_path / "atk1"
    assert retime(strike, out, "--kind", "attack", "--spans", "0:15", "--ticks", rows, "--impact-source", "6",
                  "--event", "cancel@12t", "--event", "end@23t") == 0
    selection, report = outputs(out)
    assert selection["ticks"] == [int(v) for v in rows.split(",")] and selection["tickHz"] == 60
    assert sum(selection["durations_ms"]) == 450  # 27 ticks
    events = {event["name"]: event for event in selection["events"]}
    assert set(events) == {"in", "hit", "cancel", "end"}
    assert (events["in"]["tick"], events["hit"]["tick"], events["cancel"]["tick"], events["end"]["tick"]) == (0, 7, 12, 23)
    assert events["hit"]["frame"] == 6 and events["hit"]["atMs"] == selection["impactMs"] == 117
    assert events["end"]["atMs"] == 383 and report["timeline"][6]["startTick"] == 7
    spread = tmp_path / "spread"
    assert retime(strike, spread, "--kind", "attack", "--spans", "0:15", "--ticks", "27") == 0
    ticks = outputs(spread)[0]["ticks"]
    assert sum(ticks) == 27 and max(ticks) - min(ticks) <= 1
    assert retime(strike, tmp_path / "few", "--kind", "attack", "--spans", "0:15", "--ticks", "10") == 1
    assert retime(strike, tmp_path / "ms", "--kind", "attack", "--spans", "0:15", "--event", "cancel@12t") == 1


def test_recoveries_never_reverse(strike, tmp_path, capsys):
    """Attacks recover forward; a reaction (hurt) may return to its stance by reversal."""
    assert retime(strike, tmp_path / "a", "--kind", "attack", "--spans", "12:21,20*2,19:11:-1",
                  "--impact-source", "17") == 1
    assert "recoveries never reverse" in capsys.readouterr().err
    assert retime(strike, tmp_path / "b", "--kind", "cast", "--spans", "10:40", "--policy", "pingpong") == 1
    assert "recoveries never reverse" in capsys.readouterr().err
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()
    assert retime(strike, tmp_path / "c", "--kind", "hurt", "--spans", "12:21,20*2,19:11:-1", "--duration", "520",
                  "--impact-source", "17") == 0
    selection, _ = outputs(tmp_path / "c")
    assert selection["sourceIndices"] == DUSK_HURT and selection["loopPolicy"] == "oneshot"


def test_suggests_contact_frames(strike, walk, tmp_path):
    assert retime(strike, tmp_path / "strike", "--kind", "attack", "--spans", "30:60") == 0
    contact = outputs(tmp_path / "strike")[0]["suggestedContact"]
    assert contact["sourceIndex"] == 44 and contact["outputFrame"] == 14
    assert retime(walk, tmp_path / "walk", "--kind", "walk", "--spans", "0:16") == 0
    contact = outputs(tmp_path / "walk")[0]["suggestedContact"]
    assert contact["sourceIndex"] % 8 == 4  # legs spread widest: the contact pose
    assert retime(strike, tmp_path / "idle", "--kind", "idle", "--spans", "30:60") == 0
    assert "suggestedContact" not in outputs(tmp_path / "idle")[0]


def test_reads_v1_and_v2_selections(strike, tmp_path):
    hashes = [R.forge_core.sha256_file(path) for path in sorted(strike.glob("*.png"))[10:20]]
    v1 = {"schema": "forge-frame-selection/v1", "sourceDirectory": str(strike.resolve()), "start": 10,
          "endExclusive": 20, "fps": 12, "sourceHashes": hashes, "status": "selected-needs-visual-review"}
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(v1), encoding="utf-8")
    assert retime(strike, tmp_path / "from-v1", "--kind", "attack", "--selection", str(path)) == 0
    selection, _ = outputs(tmp_path / "from-v1")
    assert selection["sourceIndices"] == list(range(10, 20)) and sum(selection["durations_ms"]) == 833
    tampered = {**v1, "sourceHashes": hashes[:-1] + ["0" * 64]}
    path.write_text(json.dumps(tampered), encoding="utf-8")
    assert retime(strike, tmp_path / "tampered", "--kind", "attack", "--selection", str(path)) == 1
    assert not (tmp_path / "tampered").exists()


# --------------------------------------------------------------------------- CLI conventions

def test_retime_help_is_ascii_under_cp1252_and_cp950():
    assert_cli_help(SKILL, "retime")


def test_retime_refuses_an_existing_output(strike, tmp_path):
    out = tmp_path / "taken"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    assert retime(strike, out, "--kind", "attack", "--spans", "0:10") == 1
    assert [p.name for p in out.iterdir()] == ["keep.txt"]


def test_retime_failure_publishes_nothing(walk, tmp_path):
    """A rule failure (a ping-pong walk) exits 1 with an error line and leaves nothing behind."""
    result = run_cli([script_path(SKILL, "retime"), "--frames-dir", walk, "--fps", "24", "--output-dir",
                      tmp_path / "out", "--kind", "walk", "--spans", "0:16", "--policy", "pingpong"], "cp1252")
    assert result.returncode == 1 and result.stderr.startswith("error: walks never ping-pong")
    assert not (tmp_path / "out").exists()
    assert not [p for p in tmp_path.iterdir() if ".stage-" in p.name]


def test_retime_cli_prints_one_ascii_json_line(strike, tmp_path):
    result = run_cli([script_path(SKILL, "retime"), "--frames-dir", strike, "--fps", "24", "--output-dir",
                      tmp_path / "out", "--kind", "attack", "--spans", DUSK_ATTACK_SPANS, "--duration", "950",
                      "--impact-source", "44"], "cp1252")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].isascii()
    summary = json.loads(lines[0])
    assert summary["impactMs"] == 391 and summary["durationMs"] == 950
    assert Path(summary["selection"]).is_file() and Path(summary["metadata"]).is_file()


def test_internal_errors_are_one_line(monkeypatch, capsys):
    """D27: an exception that is not a refused input prints 'error: internal error (<Type>: <msg>)', no traceback."""
    monkeypatch.setattr(R, "retime", lambda args: [][3])
    code = R.main(["--frames-dir", "frames", "--fps", "24", "--output-dir", "out", "--kind", "attack", "--spans", "0:2"])
    assert code == 1 and capsys.readouterr().err.strip() == "error: internal error (IndexError: list index out of range)"


def test_selection_with_a_bom_is_read(strike, tmp_path):
    """D28: a selection saved with a UTF-8 BOM (PowerShell 5.1) is read like any other."""
    first = tmp_path / "first"
    assert R.main(["--frames-dir", str(strike), "--fps", "24", "--output-dir", str(first), "--kind", "attack",
                   "--spans", "0:6"]) == 0
    raw = (first / "selection.json").read_bytes()
    (tmp_path / "bom.json").write_bytes(b"\xef\xbb\xbf" + raw)
    selection = R.gait_loop.read_selection(tmp_path / "bom.json")
    assert selection["indices"] == [0, 1, 2, 3, 4, 5]


# --------------------------------------------------------------------------- automatic one-shot retiming

def slow_attack_frame(t: int) -> Image.Image:
    """A generator-style slow-motion attack: rest 0-9, wind-up 10-25 (the arm pulls back 16 px), a fast strike
    26-31 (out 30 px), a frozen hold 32-71 (the 2026-10-06 thrust held 1.7 s), recovery 72-101, rest 102-119."""
    if t < 10 or t > 101:
        reach = 20.0
    elif t < 26:
        reach = 20.0 - (t - 9)
    elif t < 32:
        reach = 4.0 + 6.0 * (t - 25)
    elif t <= 71:
        reach = 40.0
    else:
        reach = 40.0 - 20.0 * (t - 71) / 30
    image = Image.new("RGBA", (96, 48))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 10, 30, 40), fill=(40, 120, 200, 255))
    draw.rectangle((31, 18, 31 + round(reach), 25), fill=(220, 180, 140, 255))
    draw.point((t % 3, 47), fill=(10, 10, 10, 255))  # every frame differs by one pixel (no motion)
    return image


@pytest.fixture(scope="module")
def slow_attack(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("slow-attack")
    for t in range(120):
        slow_attack_frame(t).save(out / f"src_{t:04d}.png")
    return out


def test_tick_grid_fits_80_ms_frames():
    assert R.tick_grid(R.Fraction(12)) == (60, 5)
    assert R.tick_grid(R.Fraction(24)) == (24, 1)
    assert R.tick_grid(R.Fraction(25, 2)) == (25, 2)            # 12.5 fps: two 40 ms ticks = exactly 80 ms
    durations, _ = R.tick_durations([2] * 8, 25)
    assert durations == [80] * 8


def test_auto_oneshot_compresses_slow_motion_and_keeps_the_hit(slow_attack, tmp_path):
    out = tmp_path / "auto"
    assert retime(slow_attack, out, "--kind", "attack", "--range", "0:120", "--auto-oneshot", "--duration", "700",
                  "--key", "hit=31@0.4+80", "--event-source", "cancel=80") == 0
    selection, report = outputs(out)
    indices, durations = selection["sourceIndices"], selection["durations_ms"]
    total = sum(durations)
    assert abs(total - 700) <= 42                                   # the game length, within one 24 fps frame
    assert all(b >= a for a, b in zip(indices, indices[1:]))         # never plays backwards
    assert 8 <= indices[0] <= 10 and 96 <= indices[-1] <= 102       # rest before the onset ... back at rest
    held = [i for i in indices if 33 <= i <= 71]
    assert len(held) <= 2                                            # the 1.7 s frozen hold is cut to a beat
    auto = report["auto"]
    assert auto["cutFrames"] >= 30 and auto["onset"] in range(9, 12) and auto["holds"]
    events = {event["name"]: event for event in selection["events"]}
    assert abs(events["hit"]["atMs"] - 280) <= 42 and selection["impactMs"] == events["hit"]["atMs"]
    starts = [sum(durations[:p]) for p in range(len(durations))]
    shown = indices[max(p for p, start in enumerate(starts) if start <= events["hit"]["atMs"])]
    assert shown == 31                                               # the strike lands on the hit tick
    hit_frame = indices.index(31)
    assert durations[hit_frame] >= 80                                # and is held (hit-stop)
    assert events["hit"]["atMs"] < events["cancel"]["atMs"] < events["end"]["atMs"] == total
    assert selection["loopPolicy"] == "oneshot"


def test_auto_oneshot_on_an_80_ms_grid_and_its_refusals(slow_attack, tmp_path):
    out = tmp_path / "grid"
    assert retime(slow_attack, out, "--kind", "other", "--range", "0:120", "--auto-oneshot", "--duration", "640",
                  "--output-fps", "25/2") == 0
    selection, _ = outputs(out)
    assert set(selection["durations_ms"]) <= {80, 160, 240} and sum(selection["durations_ms"]) == 640
    for args, message in (
            (["--range", "0:120", "--auto-oneshot"], "--duration"),
            (["--range", "0:120", "--auto-oneshot", "--duration", "700", "--key", "hit=115@0.4"], "outside the motion"),
            (["--range", "0:120", "--auto-oneshot", "--duration", "700", "--key", "hit=31@1.4"], "between 0 and 1"),
            (["--range", "0:120", "--auto-oneshot", "--duration", "700", "--ticks", "12"], "drop --ticks")):
        result = run_cli([script_path(SKILL, "retime"), "--frames-dir", slow_attack, "--fps", "24", "--output-dir",
                          tmp_path / "refused", "--kind", "attack", *args], "cp1252")
        assert result.returncode == 1 and message in result.stderr, (args, result.stderr)
        assert not (tmp_path / "refused").exists()
    loop = run_cli([script_path(SKILL, "retime"), "--frames-dir", slow_attack, "--fps", "24", "--output-dir",
                    tmp_path / "loop", "--kind", "walk", "--range", "0:120", "--auto-oneshot", "--duration", "700"])
    assert loop.returncode == 1 and "one-shots" in loop.stderr
