"""Tests for skills/generate2dsprite/scripts/scale_frames.py (module B03-sprite-authoring-qc, task B03-T3).

Synthetic frames are built here; the fox numbers come from report v2 P1-5 (fox_root_lock_report.json,
fox_transitions_same_canvas.json) on the provenance-tracked fixture tests/fixtures/real/raw-fox-run-v1.png.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from forge_testutils import (
    assert_cli_help, assert_valid_contract, load_script, real_fixture, run_cli, script_path,
)

SKILL = "generate2dsprite"
SCRIPT = script_path(SKILL, "scale_frames")
BUILDER = script_path(SKILL, "build_animation_clips")
sq = load_script(SKILL, "sheet_qc")
FOX_TUNIC = "#255e5e,#152e2d"  # report v2 palette10 teal groups 4 and 7 (the tunic)
FOX_CANVAS_AREA = 396 * 512  # report v2 ownership canvas of the fox frames


# --------------------------------------------------------------------------- helpers

def report_v2_mae(first: np.ndarray, second: np.ndarray, area: float) -> float:
    """report v2 transition metric: mean |premultiplied RGB| difference over a reference canvas area.

    Frames that only grew transparent padding keep the same sum, so normalising by the original
    canvas area compares outputs of any canvas size with the report's 396x512 numbers.
    """
    def premultiplied(frame):
        return frame[..., :3].astype(np.float64) * frame[..., 3:4] / 255.0
    return float(np.abs(premultiplied(first) - premultiplied(second)).sum() / (area * 3))


def figure(*, dx: int = 0, dy: int = 0, tail: bool = True, mirror: bool = False, size=(96, 96)) -> np.ndarray:
    """An asymmetric grounded figure facing right: body, head, a long tail behind, two feet."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40 + dx, 30 + dy, 55 + dx, 70 + dy), fill=(40, 92, 160, 255))
    draw.rectangle((38 + dx, 12 + dy, 58 + dx, 30 + dy), fill=(236, 204, 160, 255))
    if tail:
        draw.rectangle((12 + dx, 50 + dy, 40 + dx, 56 + dy), fill=(220, 120, 40, 255))
    draw.rectangle((40 + dx, 70 + dy, 45 + dx, 84 + dy), fill=(150, 72, 22, 255))
    draw.rectangle((51 + dx, 70 + dy, 56 + dx, 84 + dy), fill=(150, 72, 22, 255))
    pixels = np.asarray(image).copy()
    return pixels[:, ::-1].copy() if mirror else pixels


def save(array: np.ndarray, path: Path) -> Path:
    Image.fromarray(array, "RGBA").save(path)
    return path


def run(*args) -> dict:
    result = run_cli([SCRIPT, *map(str, args)])
    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii()
    return json.loads(result.stdout)


def outputs(record: dict, folder: Path) -> list[np.ndarray]:
    return [np.asarray(Image.open(folder / item["output"]["path"]).convert("RGBA")) for item in record["frames"]]


def load_record(summary: dict) -> dict:
    return json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- CLI conventions

def test_help_works_under_cp1252():
    assert_cli_help(SKILL, "scale_frames")


def test_refuses_an_existing_output_dir(tmp_path):
    frame = save(figure(), tmp_path / "f.png")
    existing = tmp_path / "out"
    existing.mkdir()
    result = run_cli([SCRIPT, "--frames", frame, "--scale-from", "1", "--output-dir", existing])
    assert result.returncode == 1 and result.stderr.startswith("error:")
    assert list(existing.iterdir()) == []


def test_clamp_case_exits_1_and_publishes_nothing(tmp_path):
    """B03-T3 acceptance: a frame that would leave a fixed canvas is never clamped; the run fails
    with the padding it would need, and nothing is published."""
    frames = [save(figure(), tmp_path / "a.png"), save(figure(dx=20), tmp_path / "b.png")]
    out = tmp_path / "out"
    result = run_cli([SCRIPT, "--frames", *frames, "--scale-from", "1", "--canvas", "60,90", "--anchor-px", "40,85",
                      "--output-dir", out])
    assert result.returncode == 1
    assert "never clamped" in result.stderr and "--action-padding" in result.stderr
    assert "Traceback" not in result.stderr
    assert not out.exists() and not any(".stage-" in child.name for child in tmp_path.iterdir())


# --------------------------------------------------------------------------- B03-T3 acceptance

def test_fox_root_lock_brings_3_to_4_under_8_1_without_cropping(tmp_path):
    """report v2 P1-5: torso-x root lock (whole game px) and one y shift per sheet row take the fox
    3->4 transition from 26.54 (nominal cells; 21.46 after the trial's hand-fixed crops) to 8.10,
    with every visible pixel kept. At game size (1/8, nearest) the same metric is 7.99."""
    fox = real_fixture("raw-fox-run-v1.png")
    import forge_core
    pixels, _ = sq.load_input(fox)
    sliced, _ = sq.ownership_slice(pixels, forge_core.rounded_grid_boxes(1536, 1024, 2, 4))
    assert report_v2_mae(sliced[3], sliced[4], FOX_CANVAS_AREA) == pytest.approx(26.54, abs=0.01)

    source = tmp_path / "fox-source"
    summary = run("--sheet", fox, "--rows", 2, "--cols", 4, "--scale-from", 1, "--resampler", "nearest",
                  "--shift-quantum", 8, "--root-lock", "torso-x", "--torso-colors", FOX_TUNIC, "--row-baseline",
                  "--output-dir", source)
    record = load_record(summary)
    assert_valid_contract(record, "sprite", "scale_frames_v1", skill=SKILL)
    locked = outputs(record, source)
    assert report_v2_mae(locked[3], locked[4], FOX_CANVAS_AREA) <= 8.1
    assert [int((frame[..., 3] > 0).sum()) for frame in locked] == [int((frame[..., 3] > 0).sum()) for frame in sliced]
    assert record["root_lock"] == "torso-x" and record["row_baseline"] is True
    assert all(dx % 8 == 0 and dy % 8 == 0 for dx, dy in (item["shift"] for item in record["frames"]))
    assert [item["shift"][1] for item in record["frames"]] == [0, 0, 0, 0, 8, 8, 8, 8]  # row 1: one 8 px step
    assert record["transitions"]["after"][3] < 0.5 * record["transitions"]["before"][3]

    game = tmp_path / "fox-game"
    summary = run("--sheet", fox, "--rows", 2, "--cols", 4, "--scale-from", "1/8", "--resampler", "nearest",
                  "--root-lock", "torso-x", "--torso-colors", FOX_TUNIC, "--row-baseline", "--output-dir", game)
    record = load_record(summary)
    assert record["scale"] == 0.125 and record["scale_from"] == "1/8"
    small = outputs(record, game)
    assert report_v2_mae(small[3], small[4], FOX_CANVAS_AREA / 64) <= 8.1
    assert record["qa"]["checks"][0] == {"id": "no_clipping", "status": "pass", "value": 0, "threshold": 0,
                                         "note": "every visible source pixel is on the canvas"}


def test_stance_turn_slide_is_zero(tmp_path):
    """B03-T3 acceptance (game-opus55 exp_anchor_turnslide.json): with the stance anchor a mirrored turn
    keeps the feet in place; a bbox anchor dragged by the tail slides them."""
    right = save(figure(), tmp_path / "right.png")
    left = save(figure(mirror=True), tmp_path / "left.png")
    summary = run("--frames", right, left, "--scale-from", 1, "--anchor", "stance", "--root-lock", "stance",
                  "--output-dir", tmp_path / "stance")
    record = load_record(summary)
    assert [item["turn_slide_px"] for item in record["frames"]] == [0.0, 0.0]
    assert [item["turn_slide_output_px"] for item in record["frames"]] == [0.0, 0.0]
    # At 1/2 size whole output-pixel shifts cannot split the 1 px stance offset of the mirror: the
    # residual stays within the pixel grid (at most 1 px), against 16 px for the bbox anchor below.
    halved = load_record(run("--frames", right, left, "--scale-from", "1/2", "--resampler", "nearest", "--anchor",
                             "stance", "--root-lock", "stance", "--output-dir", tmp_path / "half"))
    assert halved["frames"][0]["turn_slide_px"] == 0.0
    assert all(item["turn_slide_px"] <= 1.0 and item["turn_slide_output_px"] <= 1.0 for item in halved["frames"])
    bbox = load_record(run("--frames", right, "--scale-from", 1, "--anchor", "bbox", "--output-dir", tmp_path / "bbox"))
    assert bbox["frames"][0]["turn_slide_px"] == 26.0  # the tail pulls the bbox centre 13 px behind the stance


def test_emitted_clips_json_builds(tmp_path):
    """B03-T3 acceptance: --emit-clips writes a clips manifest that build_animation_clips.py builds. D11: it is
    generate2dsprite.animation_clips.v2; exact --duration-ms stays exact."""
    frames = [save(figure(dx=dx), tmp_path / f"walk-{index}.png") for index, dx in enumerate((0, 2, 4, 2))]
    out = tmp_path / "scaled"
    summary = run("--frames", *frames, "--scale-from", "1/2", "--resampler", "box", "--root-lock", "torso-x",
                  "--emit-clips", "--clip-name", "walk", "--duration-ms", "80,90,80,90", "--output-dir", out)
    clips = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    assert_valid_contract(clips, "sprite", "clips_input", skill=SKILL)
    record = load_record(summary)
    assert clips["schema"] == "generate2dsprite.animation_clips.v2" and clips["anchor_px"] == record["anchor_px"]
    assert clips["clips"] == {"walk": {"frames": [0, 1, 2, 3], "duration_ms": [80, 90, 80, 90],
                                       "loop_policy": "cycle"}}
    assert "pixel_art" not in clips and "sampling" not in clips  # box resampling is not pixel art
    built = run_cli([BUILDER, "--manifest", out / "clips.json", "--output-dir", tmp_path / "compiled"])
    assert built.returncode == 0, built.stderr
    assert (tmp_path / "compiled" / "animation-clips.json").is_file()


def test_emitted_clips_are_v2_ticks_and_pixel_art_for_nearest(tmp_path):
    """D11 (sprite review B03): the documented 80 ms clip tripped the builder's uneven_ticks lint, so
    --strict failed. --emit-clips now writes v2 with ticks at tick_hz (default 6 ticks, 100 ms at 60 Hz) and,
    for integer nearest output, pixel_art and nearest sampling; the strict build is clean."""
    frames = [save(figure(dx=dx), tmp_path / f"run-{index}.png") for index, dx in enumerate((0, 4, 8, 4))]
    out = tmp_path / "game"
    run("--frames", *frames, "--scale-from", "1/2", "--resampler", "nearest", "--emit-clips", "--clip-name", "run",
        "--ticks", "5", "--no-loop", "--output-dir", out)
    clips = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    assert_valid_contract(clips, "sprite", "clips_input", skill=SKILL)
    assert clips["schema"] == "generate2dsprite.animation_clips.v2"
    assert clips["clips"] == {"run": {"frames": [0, 1, 2, 3], "ticks": 5, "tick_hz": 60, "loop_policy": "oneshot"}}
    assert (clips["pixel_art"], clips["sampling"]) == (True, "nearest")
    built = run_cli([BUILDER, "--manifest", out / "clips.json", "--output-dir", tmp_path / "built", "--strict"])
    assert built.returncode == 0, built.stderr
    result = json.loads((tmp_path / "built" / "animation-clips.json").read_text(encoding="utf-8"))
    run_clip = result["clips"]["run"]
    assert run_clip["duration_ms"] == [83, 84, 83, 83] and run_clip["tick_grid"]["even"]
    assert (result["pixel_art"], result["sampling"]) == (True, "nearest")
    default = tmp_path / "default"
    run("--frames", *frames, "--scale-from", "1/2", "--resampler", "box", "--emit-clips", "--output-dir", default)
    clip = json.loads((default / "clips.json").read_text(encoding="utf-8"))["clips"]["action"]
    assert (clip["ticks"], clip["tick_hz"], clip["loop_policy"]) == (6, 60, "cycle")
    both = run_cli([SCRIPT, "--frames", *frames, "--emit-clips", "--ticks", "5", "--duration-ms", "80",
                    "--output-dir", tmp_path / "both"])
    assert both.returncode == 2 and "not allowed with argument" in both.stderr  # D26: a usage error
    assert not (tmp_path / "both").exists()


# --------------------------------------------------------------------------- behaviour

def test_registration_by_whole_pixel_shifts_keeps_static_pixels_identical(tmp_path):
    """S05/S07: one scale, a pinned sampling grid and whole output-pixel shifts; a frame that only moved
    by whole game pixels comes out byte-identical after the lock."""
    frames = [save(figure(), tmp_path / "a.png"), save(figure(dx=8), tmp_path / "b.png"),
              save(figure(dx=-8), tmp_path / "c.png")]
    out = tmp_path / "out"
    record = load_record(run("--frames", *frames, "--scale-from", "1/4", "--resampler", "nearest", "--root-lock",
                             "torso-x", "--output-dir", out))
    assert [item["shift"] for item in record["frames"]] == [[0, 0], [-2, 0], [2, 0]]
    first, second, third = outputs(record, out)
    assert np.array_equal(first, second) and np.array_equal(first, third)
    assert record["resampler"] == "nearest" and record["qa"]["checks"][1]["status"] == "pass"


def test_row_baseline_keeps_bob_and_flight_but_lock_feet_pins_every_frame(tmp_path):
    """One y shift per sheet row (report v2 P1-5) keeps a flight frame in the air; --lock feet is for
    grounded actions and pins every frame's ground line."""
    frames = [figure(), figure(dy=-12), figure(dy=4), figure(dy=-8)]  # row 1 sits 4 px lower, frame 3 flies
    paths = [save(frame, tmp_path / f"f{index}.png") for index, frame in enumerate(frames)]
    rows = load_record(run("--frames", *paths, "--frames-per-row", 2, "--scale-from", 1, "--row-baseline",
                           "--output-dir", tmp_path / "rows"))
    assert [item["shift"] for item in rows["frames"]] == [[0, 0], [0, 0], [0, -4], [0, -4]]
    locked = outputs(rows, tmp_path / "rows")
    grounds = [forge_ground(frame) for frame in locked]
    assert grounds[0] == grounds[2] and grounds[0] - grounds[1] == 12 and grounds[0] - grounds[3] == 12
    feet = load_record(run("--frames", *paths, "--scale-from", 1, "--lock", "feet", "--output-dir", tmp_path / "feet"))
    assert len({forge_ground(frame) for frame in outputs(feet, tmp_path / "feet")}) == 1


def forge_ground(frame: np.ndarray) -> int:
    rows = np.flatnonzero((frame[..., 3] > 16).any(axis=1))
    return int(rows[-1]) + 1


def test_action_padding_grows_the_canvas_and_is_recorded(tmp_path):
    frames = [save(figure(), tmp_path / "a.png"), save(figure(dx=20), tmp_path / "b.png")]
    record = load_record(run("--frames", *frames, "--scale-from", 1, "--canvas", "60,90", "--anchor-px", "40,85",
                             "--action-padding", "0,0,20,0", "--output-dir", tmp_path / "padded"))
    assert record["canvas"] == [80, 90] and record["anchor_px"] == [40, 85]
    assert record["padding"] == [0, 0, 20, 0] and record["base_canvas"] == [60, 90]
    assert record["needed_padding"][2] > 0 and record["needed_padding"][2] <= 20
    assert_valid_contract(record, "sprite", "scale_frames_v1", skill=SKILL)


def test_profile_reuses_scale_canvas_and_root(tmp_path):
    idle = [save(figure(), tmp_path / "idle.png")]
    first = load_record(run("--frames", *idle, "--scale-from", "1/2", "--resampler", "box", "--margin", 6,
                            "--output-dir", tmp_path / "idle-out"))
    attack = [save(figure(dx=2), tmp_path / "attack-0.png"), save(figure(dx=4), tmp_path / "attack-1.png")]
    second = load_record(run("--frames", *attack, "--profile", tmp_path / "idle-out" / "scale-frames.json",
                             "--output-dir", tmp_path / "attack-out"))
    assert (second["scale"], second["resampler"], second["canvas"], second["anchor_px"]) == (
        first["scale"], first["resampler"], first["canvas"], first["anchor_px"])
    assert second["scale_from"] == "profile" and second["profile"]["path"] == "../idle-out/scale-frames.json"
    clash = run_cli([SCRIPT, "--frames", *attack, "--profile", tmp_path / "idle-out" / "scale-frames.json",
                     "--scale-from", "1", "--output-dir", tmp_path / "clash"])
    assert clash.returncode == 1 and "--profile already fixes" in clash.stderr


def test_nearest_needs_an_integer_factor_and_suggests_one(tmp_path):
    frame = save(figure(), tmp_path / "f.png")
    result = run_cli([SCRIPT, "--frames", frame, "--target-body-px", "30", "--resampler", "nearest",
                      "--output-dir", tmp_path / "out"])
    assert result.returncode == 1
    assert "integer factor" in result.stderr and "1/2" in result.stderr and "1/3" in result.stderr
    ok = load_record(run("--frames", frame, "--target-body-px", 36, "--output-dir", tmp_path / "hd"))
    assert ok["scale_from"] == "neutral" and ok["scale"] == pytest.approx(36 / 73)  # body rows 12..84


def test_union_scale_and_the_centroid_and_hip_locks(tmp_path):
    """--scale-from union fits the whole action's height; --lock x pins the alpha centroid (dragged by the
    tail), --lock hip the widest run of the hip band (a tail-free root for lunges and knockback)."""
    frames = [save(figure(), tmp_path / "a.png"), save(figure(dy=-10), tmp_path / "b.png"),
              save(figure(dx=6, tail=False), tmp_path / "c.png")]
    union = load_record(run("--frames", *frames, "--scale-from", "union", "--target-body-px", 41,
                            "--output-dir", tmp_path / "union"))
    assert union["scale_from"] == "union" and union["scale"] == pytest.approx(41 / 83)  # rows 2..84
    by_x = load_record(run("--frames", *frames, "--scale-from", 1, "--lock", "x", "--output-dir", tmp_path / "x"))
    by_hip = load_record(run("--frames", *frames, "--scale-from", 1, "--lock", "hip", "--output-dir", tmp_path / "hip"))
    assert by_x["frames"][2]["shift"][0] != -6  # losing the tail moves the centroid as well as the body
    assert by_hip["frames"][2]["shift"] == [-6, 0] and by_hip["lock"] == ["hip"]
    assert by_hip["frames"][1]["shift"] == [0, 0]  # no vertical lock: the jump keeps its height


def test_premultiplied_resampling_keeps_edges_free_of_dark_fringes(tmp_path):
    disc = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(disc).ellipse((8, 8, 56, 56), fill=(255, 255, 255, 255))
    path = save(np.asarray(disc), tmp_path / "disc.png")
    for resampler in ("lanczos", "box"):
        out = tmp_path / resampler
        record = load_record(run("--frames", path, "--scale-from", "0.37", "--resampler", resampler,
                                 "--output-dir", out))
        frame = outputs(record, out)[0]
        visible = frame[..., 3] > 0
        assert visible.any() and frame[visible][:, :3].min() >= 250  # no darkened soft edge (S05)
        assert (frame[~visible] == 0).all()  # RGB zeroed under alpha 0


def test_lock_and_option_conflicts_are_clean_errors(tmp_path):
    frame = save(figure(), tmp_path / "f.png")
    cases = [
        (["--root-lock", "torso-x", "--lock", "x"], "one horizontal lock"),
        (["--row-baseline", "--lock", "feet"], "choose one"),
        (["--lock", "knee"], "--lock takes"),
        (["--canvas", "60,90"], "go together"),
        (["--duration-ms", "80,90", "--emit-clips"], "--duration-ms"),
        (["--ticks", "0", "--emit-clips"], "--ticks"),
        (["--tick-hz", "0", "--emit-clips"], "--tick-hz"),
    ]
    for extra, message in cases:
        result = run_cli([SCRIPT, "--frames", frame, "--scale-from", "1", *extra, "--output-dir", tmp_path / "x"])
        assert result.returncode == 1 and message in result.stderr, (extra, result.stderr)
        assert "Traceback" not in result.stderr
