"""CLI tests for skills/video2dsprite/scripts/pixel_motion.py: a 1x pixel sprite's motion from an image-to-video clip.

The clip is synthetic (forge_pixel_fixtures): the job's own input picture with the sprite moved by whole cells, then
H.264, so the frames come back with compression noise on a known lattice. Tests that run ffmpeg carry the ffmpeg marker.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_pixel_fixtures import HEAD_ROWS, LINE, on_canvas, toy_sprite, video_frame, walk_frames, with_arm, with_blade
from forge_testutils import assert_cli_help, load_script, require_ffmpeg, run_cli, script_path

TOOL = script_path("video2dsprite", "pixel_motion")
motion = load_script("video2dsprite", "pixel_motion")
av = load_script("video2dsprite", "forge_av")
CANVAS = (640, 360)
MINE = (4, 3)             # cells of room this test draws its poses in


def ok(*args) -> dict:
    result = run_cli([TOOL, *map(str, args)])
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].isascii() and result.stderr == ""
    return json.loads(lines[0])


def fails(*args, code: int = 1) -> str:
    result = run_cli([TOOL, *map(str, args)])
    assert result.returncode == code, (result.stdout, result.stderr)
    assert result.stdout == ""
    return result.stderr.strip().splitlines()[-1]


def rgba(path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGBA"))


def pose(number: int, size: tuple[int, int], at: tuple[int, int]) -> np.ndarray:
    """One of four poses of the toy sprite on a canvas: at rest, one cell up, and both with an arm held out."""
    sprite = toy_sprite()
    up = number in (1, 3)
    canvas = on_canvas(sprite, size, (at[0], at[1] - 1 if up else at[1]))
    return with_arm(canvas, (at[0] + 12, at[1] + (10 if up else 11))) if number >= 2 else canvas


def test_help_is_ascii():
    assert_cli_help("video2dsprite", "pixel_motion")


def test_fitting_scale_takes_the_largest_scale_whose_room_fits():
    job = {"referenceCanvas": [1280, 720], "margin": 20.0, "sourceSize": [24, 52], "sourceAnchor": [12.0, 52.0],
           "referenceRoot": [640.0, 620.0], "padding": [5, 9, 5, 3], "referenceScale": 10}
    assert motion.fitting_scale(job) == 9          # the 52 + 9 cells above the root must fit its 600 px
    assert motion.fitting_scale({**job, "padding": [0, 0, 0, 0]}) == 10


def test_default_room_is_the_actions_room_or_a_share_of_the_sprite_inside_the_canvas():
    job = {"referenceScale": 12, "referenceOffset": [478, 116], "sourceSize": [24, 42], "workRegion": [478, 116, 766, 620]}
    assert motion.room_limits(job, (1280, 720)) == (39, 9, 41, 7)
    assert motion.default_room(job, (1280, 720)) == (39, 9, 41, 6)        # the sprite's larger side, half its height (as
    wide = {**job, "workRegion": [100, 20, 1200, 700]}                     # far as the canvas goes), a little below
    assert motion.default_room(wide, (1280, 720)) == (39, 9, 41, 7)       # a larger work region wins where it is larger
    small = {**job, "sourceSize": [10, 12], "workRegion": [478, 116, 598, 260]}
    assert motion.default_room(small, (1280, 720)) == (12, 6, 12, 2)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """A prepared job, the clip a video model would give back for it, and the frames read from it."""
    require_ffmpeg()
    root = tmp_path_factory.mktemp("pixel-motion")
    sprite = toy_sprite()
    Image.fromarray(sprite, "RGBA").save(root / "hero.png")
    job_dir = root / "job"
    prepared = ok("prepare", "--sprite", root / "hero.png", "--action", "walk", "--canvas", "%d,%d" % CANVAS, "--subject",
                  "a toy knight", "--facing", "front", "--extra", "The legs step in turn.", "--output-dir", job_dir)
    job = json.loads((job_dir / "registration_job.json").read_text(encoding="utf-8"))
    scale, (ox, oy) = job["referenceScale"], job["referenceOffset"]
    size = (16 + 2 * MINE[0], 24 + 2 * MINE[1])
    corner = (ox - MINE[0] * scale, oy - MINE[1] * scale)
    key = tuple(int(v) for v in rgba(job_dir / "input.png")[0, 0, :3])
    order = [0] * 6 + [(i // 3) % 4 for i in range(60)] + [0] * 6
    frames = [video_frame(pose(n, size, MINE), scale, corner, CANVAS, key) for n in range(4)]
    assert (frames[0] == rgba(job_dir / "input.png")).all()                  # the clip starts on the job's own picture
    av.encode_h264_loop([frames[n] for n in order], root / "clip.mp4", "24/1", crf=10)
    read = ok("frames", "--clip", root / "clip.mp4", "--job", job_dir, "--output-dir", root / "frames")
    return {"root": root, "job": job, "prepared": prepared, "order": order, "read": read}


@pytest.mark.ffmpeg
def test_prepare_adds_the_pixel_paragraph_and_keeps_a_whole_scale(clip):
    job, prepared = clip["job"], clip["prepared"]
    assert job["pixelArt"] is True and isinstance(job["referenceScale"], int) and prepared["scale"] == job["referenceScale"]
    prompt = Path(prepared["prompt"]).read_text(encoding="utf-8")
    assert motion.PIXEL_PARAGRAPH + " The legs step in turn." in prompt and "a toy knight" in prompt
    assert prepared["scaleLowered"] is None and prepared["warnings"] == []
    assert "exists already" in fails("prepare", "--sprite", clip["root"] / "hero.png", "--action", "walk", "--output-dir",
                                     clip["root"] / "job")


@pytest.mark.ffmpeg
def test_frames_puts_every_frame_back_on_the_sprites_grid_and_palette(clip):
    read, root = clip["read"], clip["root"]
    assert read["frames"] == 72 and read["fps"] == "24/1" and read["firstFrameDiffers"] == 0 and read["warnings"] == []
    assert read["cycle"]["period"] == 12 and read["cycle"]["closure"] == 0
    record = json.loads((root / "frames" / "frames.json").read_text(encoding="utf-8"))
    assert record["schema"] == motion.FRAMES_SCHEMA and record["lattice"].startswith("the first frame")
    assert record["sprite"]["colours"] == 7 and record["framesTouchingEdge"] == [] and len(record["frames"]) == 72
    size, at = tuple(record["canvas"]), tuple(record["spriteAt"])
    assert (rgba(root / "frames" / "sprite.png") == toy_sprite()).all()
    for index, number in enumerate(clip["order"]):
        assert (rgba(root / "frames" / "frames" / f"frame_{index:03d}.png") == pose(number, size, at)).all(), index
    assert max(r["notFlat"] for r in record["frames"]) == 0 and (root / "frames" / "frames-sheet.png").is_file()


@pytest.mark.ffmpeg
def test_frames_reads_a_clip_whose_lattice_lies_a_little_before_the_jobs_where_the_sprite_is(tmp_path):
    require_ffmpeg()
    sprite = toy_sprite()
    Image.fromarray(sprite, "RGBA").save(tmp_path / "hero.png")
    ok("prepare", "--sprite", tmp_path / "hero.png", "--action", "walk", "--canvas", "%d,%d" % CANVAS, "--scale", 8, "--subject",
       "a toy knight", "--output-dir", tmp_path / "job")
    job = json.loads((tmp_path / "job" / "registration_job.json").read_text(encoding="utf-8"))
    scale, (ox, oy) = job["referenceScale"], job["referenceOffset"]
    size = (16 + 2 * MINE[0], 24 + 2 * MINE[1])
    key = tuple(int(v) for v in rgba(tmp_path / "job" / "input.png")[0, 0, :3])
    corner = (ox - MINE[0] * scale - 3, oy - MINE[1] * scale - 3)             # the model drew everything 3 px up and to the left
    frames = [video_frame(pose(n, size, MINE), scale, corner, CANVAS, key) for n in (0, 0, 0, 1, 1, 1, 2, 2, 2, 0, 0, 0)]
    av.encode_h264_loop(frames, tmp_path / "clip.mp4", "24/1", crf=10)
    read = ok("frames", "--clip", tmp_path / "clip.mp4", "--job", tmp_path / "job", "--output-dir", tmp_path / "frames")
    assert scale == 8 and read["firstFrameDiffers"] == 0 and read["warnings"] == []
    record = json.loads((tmp_path / "frames" / "frames.json").read_text(encoding="utf-8"))
    assert all(-8 < v < 0 for v in record["frames"][0]["shift"])                 # a shift backwards, not 5 px forward
    at = tuple(record["spriteAt"])
    assert (rgba(tmp_path / "frames" / "frames" / "frame_000.png") == pose(0, tuple(record["canvas"]), at)).all()
    assert (rgba(tmp_path / "frames" / "frames" / "frame_007.png") == pose(2, tuple(record["canvas"]), at)).all()


@pytest.mark.ffmpeg
def test_finish_picks_one_cycle_cleans_it_and_packages_it(clip):
    root = clip["root"]
    out = root / "walk"
    done = ok("finish", "--frames-dir", root / "frames", "--name", "toy_walk", "--count", 4, "--ms", 100, "--closed-outline",
              "--head-rows", HEAD_ROWS, "--output-dir", out)
    assert done["frames"] == 4 and done["loopPolicy"] == "cycle" and done["durationsMs"] == [100] * 4
    assert done["picked"]["mode"] == "cycle" and done["picked"]["period"] == 12 and done["headLocked"] == [True] * 4
    record = json.loads((root / "frames" / "frames.json").read_text(encoding="utf-8"))
    info = json.loads((out / "motion.json").read_text(encoding="utf-8"))
    x0, y0, x1, y1 = info["trimmedTo"]
    size, at = tuple(record["canvas"]), tuple(record["spriteAt"])
    assert info["frameSize"] == [x1 - x0, y1 - y0] == [16 + 2, 24 + 1 + 2]           # the drawings and one cell around them
    assert info["anchor"] == [record["anchor"][0] - x0, record["anchor"][1] - y0]
    drawn = [rgba(out / "clean" / f"frame_{n:03d}.png") for n in range(4)]
    for picture, index in zip(drawn, done["videoFrames"]):
        assert (picture == pose(clip["order"][index], size, at)[y0:y1, x0:x1]).all()
    assert len({picture.tobytes() for picture in drawn}) == 4                        # one drawing of each pose
    strip = rgba(out / "strip.png")
    assert strip.shape[:2] == (y1 - y0, 4 * (x1 - x0)) and (strip[:, :x1 - x0] == drawn[0]).all()
    selection = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    assert selection["schema"] == "forge-frame-selection/v2" and selection["sourceIndices"] == [0, 1, 2, 3]
    assert selection["durations_ms"] == [100] * 4 and selection["loopPolicy"] == "cycle" and selection["events"] == []
    manifest = json.loads((out / "package" / "animation.json").read_text(encoding="utf-8"))
    assert manifest["frameCount"] == 4 and manifest["durationsMs"] == [100] * 4 and manifest["sourceSize"] == info["frameSize"]
    assert manifest["sourceAnchor"] == info["anchor"] and (out / "review.png").is_file()
    assert "exists already" in fails("finish", "--frames-dir", root / "frames", "--name", "toy_walk", "--output-dir", out)


@pytest.mark.ffmpeg
def test_finish_takes_named_frames_with_events_mirrored_and_untrimmed(clip):
    root = clip["root"]
    out = root / "hit"
    done = ok("finish", "--frames-dir", root / "frames", "--name", "toy_hit", "--pick", "6,9,12", "--ms-list", "60,90,150",
              "--event", "hit=60", "--loop", "oneshot", "--flip", "--no-trim", "--no-package", "--output-dir", out)
    record = json.loads((root / "frames" / "frames.json").read_text(encoding="utf-8"))
    size, at = tuple(record["canvas"]), tuple(record["spriteAt"])
    assert done["videoFrames"] == [6, 9, 12] and done["loopPolicy"] == "oneshot" and done["package"] is None
    assert done["frameSize"] == list(size) and done["anchor"] == [size[0] - record["anchor"][0], record["anchor"][1]]
    for n, index in enumerate(done["videoFrames"]):
        assert (rgba(out / "clean" / f"frame_{n:03d}.png") == pose(clip["order"][index], size, at)[:, ::-1]).all()
    selection = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    assert selection["events"] == [{"name": "hit", "atMs": 60}] and selection["durations_ms"] == [60, 90, 150]
    assert not (out / "package").exists()
    action = ok("finish", "--frames-dir", root / "frames", "--name", "toy_step", "--mode", "action", "--from", 6, "--to", 18,
                "--count", 3, "--total-ms", 250, "--no-package", "--output-dir", root / "step")
    assert action["picked"]["mode"] == "action" and action["videoFrames"][0] == 6 and action["videoFrames"][-1] == 18
    assert action["durationsMs"] == [83, 84, 83] and action["loopPolicy"] == "cycle"       # walk is a loop action


@pytest.mark.ffmpeg
def test_finish_takes_a_neighbour_when_a_drawings_head_cannot_be_locked(clip, tmp_path):
    root = clip["root"]
    spoiled = tmp_path / "frames"
    shutil.copytree(root / "frames", spoiled)
    record = json.loads((spoiled / "frames.json").read_text(encoding="utf-8"))
    (x, y), bad = record["spriteAt"], 13                          # the cycle's second drawing: its face boils away
    frame = rgba(spoiled / "frames" / f"frame_{bad:03d}.png").copy()
    head = frame[y:y + HEAD_ROWS, x:x + 16]
    head[head[..., 3] > 0] = (238, 208, 96, 255)
    Image.fromarray(frame, "RGBA").save(spoiled / "frames" / f"frame_{bad:03d}.png")
    done = ok("finish", "--frames-dir", spoiled, "--name", "toy_walk", "--count", 4, "--closed-outline", "--head-rows", HEAD_ROWS,
              "--no-package", "--output-dir", tmp_path / "walk")
    assert done["videoFrames"] == [10, 14, 16, 19] and done["headLocked"] == [True] * 4
    info = json.loads((tmp_path / "walk" / "motion.json").read_text(encoding="utf-8"))
    assert info["clean"][1]["insteadOf"] == bad and "insteadOf" not in info["clean"][0]
    named = ok("finish", "--frames-dir", spoiled, "--name", "toy_named", "--pick", "10,13,16,19", "--head-rows", HEAD_ROWS,
               "--no-package", "--output-dir", tmp_path / "named")
    assert named["videoFrames"] == [10, 13, 16, 19] and named["headLocked"] == [True, False, True, True]


@pytest.mark.ffmpeg
def test_refusals(clip, tmp_path):
    root = clip["root"]
    frames_dir = root / "frames"
    assert "outside 0..71" in fails("finish", "--frames-dir", frames_dir, "--name", "x", "--pick", "3,99", "--output-dir", tmp_path / "f1")
    assert "NAME=MS" in fails("finish", "--frames-dir", frames_dir, "--name", "x", "--event", "hit", "--output-dir", tmp_path / "f2")
    assert "--head-rows" in fails("finish", "--frames-dir", frames_dir, "--name", "x", "--head-rows", 24, "--output-dir", tmp_path / "f3")
    assert "--head-box" in fails("finish", "--frames-dir", frames_dir, "--name", "x", "--head-box", "0,0,17,9", "--output-dir", tmp_path / "f6")
    boxed = ok("finish", "--frames-dir", frames_dir, "--name", "toy_box", "--count", 4, "--head-box", "0,0,16,9", "--no-package",
               "--output-dir", tmp_path / "boxed")
    assert boxed["headLocked"] == [True] * 4
    assert "holds no frames.json" in fails("finish", "--frames-dir", root / "job", "--name", "x", "--output-dir", tmp_path / "f4")
    fails("finish", "--frames-dir", frames_dir, "--name", "x", "--ms", 100, "--total-ms", 400, "--output-dir", tmp_path / "f5", code=2)
    assert not any((tmp_path / name).exists() for name in ("f1", "f2", "f3", "f4", "f5"))
    # a run that fails after its drawings were written (the package refuses the name) leaves nothing behind either
    assert "nothing was written" in fails("finish", "--frames-dir", frames_dir, "--name", "bad name", "--count", 4,
                                          "--output-dir", tmp_path / "f7")
    assert not (tmp_path / "f7").exists() and not list(tmp_path.glob(".f7*"))
    smooth = tmp_path / "smooth-job"
    shutil.copytree(root / "job", smooth)
    job = json.loads((smooth / "registration_job.json").read_text(encoding="utf-8"))
    (smooth / "registration_job.json").write_text(json.dumps({**job, "pixelArt": False}), encoding="utf-8")
    assert "--pixel-art" in fails("frames", "--clip", root / "clip.mp4", "--job", smooth, "--output-dir", tmp_path / "r1")
    assert "does not fit the video canvas" in fails("frames", "--clip", root / "clip.mp4", "--job", root / "job", "--room",
                                                    "90,3,3,3", "--output-dir", tmp_path / "r2")
    assert "does not exist" in fails("frames", "--clip", tmp_path / "none.mp4", "--job", root / "job", "--output-dir", tmp_path / "r3")
    assert not any((tmp_path / name).exists() for name in ("r1", "r2", "r3"))


# --------------------------------------------------------------------------- prepare: the root is the feet

def test_prepare_roots_the_sprite_at_its_feet_and_not_at_a_blade(tmp_path):
    armed = with_blade(toy_sprite(), 8)                         # 24 wide: the picture's middle is not the feet's
    Image.fromarray(armed, "RGBA").save(tmp_path / "armed.png")
    base = ("prepare", "--sprite", tmp_path / "armed.png", "--action", "attack", "--canvas", "%d,%d" % CANVAS)
    prepared = ok(*base, "--output-dir", tmp_path / "job")
    assert prepared["anchor"] == [8 + 8.0, 24.0]
    job = json.loads((tmp_path / "job" / "registration_job.json").read_text(encoding="utf-8"))
    assert job["anchorMode"] == "explicit" and job["sourceAnchor"] == [16.0, 24.0]
    named = ok(*base, "--anchor", "12,24", "--output-dir", tmp_path / "named")
    assert named["anchor"] == [12.0, 24.0]
    assert motion.PIXEL_PARAGRAPH in Path(named["prompt"]).read_text(encoding="utf-8")
    assert prepared["keyColor"] == "magenta" and motion.quiet_key(armed) == "magenta"
    purplish = armed.copy()                                    # a lit, purplish edge: on magenta the package would count it
    ring = (purplish[..., 3] > 0) & (purplish[..., :3] == np.asarray(LINE)).all(axis=2)
    purplish[ring, :3] = (123, 96, 118)
    green = (purplish[..., 3] > 0) & (purplish[..., :3] == np.asarray((92, 160, 84))).all(axis=2)
    purplish[green, :3] = (226, 178, 132)                      # no green of its own: green is a key it does not fight
    Image.fromarray(purplish, "RGBA").save(tmp_path / "purplish.png")
    quiet = ok("prepare", "--sprite", tmp_path / "purplish.png", "--action", "attack", "--canvas", "%d,%d" % CANVAS,
               "--output-dir", tmp_path / "quiet")
    assert quiet["keyColor"] == "green" and motion.quiet_key(purplish) == "green" and quiet["warnings"] == []
    leans = motion.key_leanings(purplish)
    assert leans["magenta"][0] > 0.9 and leans["green"] == (0.0, 0.0) and leans["blue"][1] > motion.OWN_MOST
    asked = ok("prepare", "--sprite", tmp_path / "purplish.png", "--action", "attack", "--canvas", "%d,%d" % CANVAS, "--key",
               "magenta", "--output-dir", tmp_path / "asked")
    assert asked["keyColor"] == "magenta" and "lean to the magenta key" in asked["warnings"][0]
    drawn = ok(*base, "--outline", "drawn", "--output-dir", tmp_path / "drawn")       # a look without an outline
    words = Path(drawn["prompt"]).read_text(encoding="utf-8")
    assert motion.PIXEL_PARAGRAPH_DRAWN in words and "the dark outline" not in words
    fails(*base, "--anchor", "left", "--output-dir", tmp_path / "bad", code=2)
    assert not (tmp_path / "bad").exists()


# --------------------------------------------------------------------------- measure: a walk before it is picked

def frames_folder(folder: Path, frames, sprite: np.ndarray, at) -> Path:
    """What pixel_motion.py frames writes, from frames that are on the grid already (no clip, no ffmpeg)."""
    (folder / "frames").mkdir(parents=True)
    for n, frame in enumerate(frames):
        Image.fromarray(frame, "RGBA").save(folder / "frames" / f"frame_{n:03d}.png")
    Image.fromarray(sprite, "RGBA").save(folder / "sprite.png")
    record = {"schema": motion.FRAMES_SCHEMA, "clip": {"frames": len(frames), "fps": "24/1"}, "sprite": {"path": "sprite.png"},
              "spriteAt": list(at), "anchor": [at[0] + 8.0, at[1] + 24.0], "canvas": [frames[0].shape[1], frames[0].shape[0]],
              "job": {"action": "walk", "actionKind": "loop"}, "keyColor": "magenta"}
    (folder / "frames.json").write_text(json.dumps(record), encoding="utf-8")
    return folder


def test_measure_finds_the_footfalls_the_step_the_speed_and_the_drawings_to_take(tmp_path):
    frames, sprite, at = walk_frames(steps=6, pairs=4, still=4)
    folder = frames_folder(tmp_path / "walk", frames, sprite, at)
    out = tmp_path / "measure"
    found = ok("measure", "--frames-dir", folder, "--output-dir", out)
    assert found["facing"] == "right" and found["footfalls"] == [4 + 6 * n for n in range(8)] and found["stepFrames"] == 6.0
    assert found["onTwos"] is False and found["repeatingFrames"] == 0 and found["warnings"] == []
    assert found["speedPxPerSecond"] == 24.0                    # the standing foot goes back one cell a frame, 24 frames a second
    assert [found["stepPair"][k] for k in ("from", "to", "cellsThatDiffer")] == [34, 46, 0]     # even steps, the latest pair
    assert found["marks"].startswith("footfalls") and found["stepByLags"] == [6, 12]
    assert found["pick"] == [34, 35, 37, 38, 40, 41, 43, 44] and found["msList"] == [42, 83] * 4
    assert found["finish"] == "--pick 34,35,37,38,40,41,43,44 --ms-list 42,83,42,83,42,83,42,83 --loop cycle"
    record = json.loads((out / "measure.json").read_text(encoding="utf-8"))
    assert record["schema"] == motion.WALK_SCHEMA and record["legsShow"] == found["pick"] and record["stepSeconds"] == 0.25
    assert Image.open(out / "legs.png").width > 4 * 16 and sorted(p.name for p in out.iterdir()) == ["legs.png", "measure.json"]
    three = ok("measure", "--frames-dir", folder, "--per-step", 3, "--show", "4,10,16", "--output-dir", tmp_path / "three")
    assert three["pick"] == [34, 36, 38, 40, 42, 44] and three["msList"] == [83] * 6
    done = ok("finish", "--frames-dir", folder, "--name", "toy_walk", *found["finish"].split(), "--no-package", "--output-dir",
              tmp_path / "set")
    assert done["videoFrames"] == found["pick"] and done["durationsMs"] == found["msList"] and done["loopPolicy"] == "cycle"


def test_measure_reads_a_walk_to_the_left_and_a_clip_on_twos(tmp_path):
    frames, sprite, at = walk_frames(steps=6, pairs=4, still=4, facing="left")
    left = ok("measure", "--frames-dir", frames_folder(tmp_path / "left", frames, sprite, at), "--output-dir", tmp_path / "m-left")
    assert left["facing"] == "left" and left["footfalls"] == [4 + 6 * n for n in range(8)] and left["speedPxPerSecond"] == 24.0
    single, sprite, at = walk_frames(steps=6, pairs=4, still=2)
    ones = ok("measure", "--frames-dir", frames_folder(tmp_path / "ones", single, sprite, at), "--output-dir", tmp_path / "m-ones")
    frames, sprite, at = walk_frames(steps=6, pairs=4, still=2, twice=True)
    twos = ok("measure", "--frames-dir", frames_folder(tmp_path / "twos", frames, sprite, at), "--output-dir", tmp_path / "m-twos")
    assert twos["onTwos"] is True and twos["stepFrames"] == 12.0 and twos["speedPxPerSecond"] == 12.0
    assert all(b - a >= 2 for a, b in zip(twos["pick"], twos["pick"][1:]))            # never a frame and its repeat
    assert [frames[k].tobytes() for k in twos["pick"]] == [single[k].tobytes() for k in ones["pick"]]    # the same drawings
    assert ones["msList"] == [42, 83] * 4 and twos["msList"] == [83, 167] * 4 and sum(twos["msList"]) == 1000
    still = [frames[0]] * 30
    none = ok("measure", "--frames-dir", frames_folder(tmp_path / "still", still, sprite, at), "--output-dir", tmp_path / "m-still")
    assert none["pick"] is None and none["finish"] is None and "no step was found" in none["warnings"][0]
    poled, sprite, at = walk_frames(steps=6, pairs=4, still=4, pole=True)           # a staff set on the ground in front
    staff = ok("measure", "--frames-dir", frames_folder(tmp_path / "pole", poled, sprite, at), "--output-dir", tmp_path / "m-pole")
    assert staff["footfalls"] == left["footfalls"] and staff["speedPxPerSecond"] == 24.0 and staff["warnings"] == []
    assert "--per-step" in fails("measure", "--frames-dir", tmp_path / "left", "--per-step", 1, "--output-dir", tmp_path / "m1")
    assert "outside 0.." in fails("measure", "--frames-dir", tmp_path / "left", "--show", "3,999", "--output-dir", tmp_path / "m2")
    assert "holds no frames.json" in fails("measure", "--frames-dir", tmp_path, "--output-dir", tmp_path / "m3")
    assert not any((tmp_path / name).exists() for name in ("m1", "m2", "m3"))


# --------------------------------------------------------------------------- breath: an idle made from the sprite

def test_breath_lowers_the_upper_part_by_one_cell_and_keeps_the_feet(tmp_path):
    sprite = toy_sprite()
    Image.fromarray(sprite, "RGBA").save(tmp_path / "hero.png")
    out = tmp_path / "idle"
    done = ok("breath", "--sprite", tmp_path / "hero.png", "--name", "hero_idle", "--output-dir", out)
    assert done["frames"] == 2 and done["durationsMs"] == [620, 520] and done["loopPolicy"] == "cycle"
    assert done["frameSize"] == [16 + 2, 24 + 2] and done["anchor"] == [8.0 + 1, 24 + 1]        # one empty cell around
    row = done["rowTakenOut"]
    assert 17 <= row < 21                                       # a row of the lower legs
    still, down = rgba(out / "clean" / "frame_000.png"), rgba(out / "clean" / "frame_001.png")
    assert not still[0].any() and not still[-1].any() and not still[:, 0].any() and not still[:, -1].any()
    still, down = still[1:-1, 1:-1], down[1:-1, 1:-1]
    assert (still == sprite).all()
    assert (down[1:row + 1] == sprite[:row]).all() and not down[0, :, 3].any()        # what is above came down one cell
    assert (down[row + 1:] == sprite[row + 1:]).all()                                 # what is below stands
    info = json.loads((out / "motion.json").read_text(encoding="utf-8"))
    assert info["schema"] == motion.MOTION_SCHEMA and info["picked"]["mode"] == "breath" and info["anchor"] == [9.0, 25.0]
    assert info["videoFrames"] == [] and info["frames"] is None and info["totalMs"] == 1140
    selection = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    assert selection["durations_ms"] == [620, 520] and selection["loopPolicy"] == "cycle"
    manifest = json.loads((out / "package" / "animation.json").read_text(encoding="utf-8"))
    assert manifest["frameCount"] == 2 and manifest["sourceAnchor"] == [9.0, 25.0] and (out / "review.png").is_file()
    qa = json.loads((out / "package" / "animation-qa.json").read_text(encoding="utf-8"))
    assert [c["status"] for c in qa["checks"] if c["id"] == "edge_touch_frames"] == ["pass"]     # no drawing touches its frame


def test_breath_brings_a_hanging_blade_down_whole_and_takes_its_options(tmp_path):
    armed = with_blade(toy_sprite(), 8)
    Image.fromarray(armed, "RGBA").save(tmp_path / "armed.png")
    (tmp_path / "armed.json").write_text(json.dumps({"key": "green"}), encoding="utf-8")
    out = tmp_path / "idle"
    done = ok("breath", "--sprite", tmp_path / "armed.png", "--name", "armed_idle", "--ms-list", "500,400", "--rows", "18,20",
              "--no-package", "--output-dir", out)
    assert done["anchor"] == [17.0, 25] and done["durationsMs"] == [500, 400] and done["rowTakenOut"] in (18, 19)
    down = rgba(out / "clean" / "frame_001.png")[1:-1, 1:-1]
    row = done["rowTakenOut"]
    blade, moved = armed[:, :8], down[:, :8]                    # the blade's columns left of the body
    assert (moved[1:] == blade[:-1]).all() and not moved[0, :, 3].any()     # the whole blade one cell lower; its tip's last
    assert (down[row + 1:, 8:] == armed[row + 1:, 8:]).all()                # row is cut at the ground; the feet stand
    assert LINE == tuple(int(v) for v in moved[-1, 1, :3])                  # the blade is still drawn in its own colour
    assert not (out / "package").exists()
    assert "two whole durations" in fails("breath", "--sprite", tmp_path / "armed.png", "--name", "x", "--ms-list", "500",
                                          "--output-dir", tmp_path / "b1")
    assert "--rows is A,B" in fails("breath", "--sprite", tmp_path / "armed.png", "--name", "x", "--rows", "18", "--output-dir",
                                    tmp_path / "b2")
    assert "no row to take out" in fails("breath", "--sprite", tmp_path / "armed.png", "--name", "x", "--rows", "30,40",
                                         "--output-dir", tmp_path / "b3")
    assert "does not exist" in fails("breath", "--sprite", tmp_path / "none.png", "--name", "x", "--output-dir", tmp_path / "b4")
    assert not any((tmp_path / name).exists() for name in ("b1", "b2", "b3", "b4"))
