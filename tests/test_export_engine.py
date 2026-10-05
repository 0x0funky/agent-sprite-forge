"""export_engine.py (B09-T1): Aseprite JSON, Godot 4 SpriteFrames and Sprite3D exports of built clips.

Fixtures are synthetic: frames are drawn with numpy and built manifests are
written the way build_animation_clips.py writes them (one test runs the real
builder). Engine imports themselves are not tested here (no editors in CI).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import assert_cli_help, assert_valid_contract, load_script, run_cli, script_path

E = load_script("generate2dsprite", "export_engine")
TOOL = script_path("generate2dsprite", "export_engine")
BUILDER = script_path("generate2dsprite", "build_animation_clips")
V1, V2 = "generate2dsprite.animation_clips.v1", "generate2dsprite.animation_clips.v2"

def assert_valid_sprite(document: dict, name: str) -> None:
    """Validate against generate2dsprite's vendored sprite.schema.json (B09's section 5 defs landed there)."""
    assert_valid_contract(document, "sprite", name, skill="generate2dsprite")


# --------------------------------------------------------------------------- fixtures

def sprite_frame(i: int, size=(40, 48), *, edge_touch: bool = False) -> np.ndarray:
    """A 40x48 RGBA body that differs per frame, with a half-transparent top row."""
    w, h = size
    frame = np.zeros((h, w, 4), np.uint8)
    x0 = 10 + i % 5
    frame[12 + i % 3:44, x0:x0 + 16] = (40 + 23 * i % 200, 150, 210 - 9 * i % 150, 255)
    frame[11 + i % 3, x0:x0 + 16] = (90, 90, 90, 128)
    frame[30, x0 + 3] = (255, 255, 255, 255)
    if edge_touch:
        frame[:, 0] = (200, 30, 30, 255)
        frame[0, :] = (30, 200, 30, 255)
    return frame


def write_built(folder: Path, frames: list[np.ndarray], clips: dict, *, schema: str = V2, anchor=(20, 44),
                states: dict | None = None, top: dict | None = None) -> Path:
    """A built animation-clips.json shaped like build_animation_clips.py output (v1 or v2)."""
    (folder / "frames").mkdir(parents=True, exist_ok=True)
    records = []
    for i, pixels in enumerate(frames):
        Image.fromarray(pixels).save(folder / "frames" / f"frame-{i:02d}.png")
        records.append({"index": i, "name": f"pose-{i}", "file": f"frames/frame-{i:02d}.png",
                        "rgba_pixel_sha256": hashlib.sha256(pixels.tobytes()).hexdigest()})
    built = {}
    for name, clip in clips.items():
        clip = dict(clip)
        clip["total_duration_ms"] = sum(clip["duration_ms"])
        if schema == V2:
            clip.setdefault("events_ms", [])
            clip.setdefault("tick_grid", {"hz": 60, "max_drift_ms": 0.0})
        built[name] = clip
    manifest = {"schema": schema, "source_manifest": {"path": "clips.json", "file_sha256": "0" * 64},
                "frame_size": [frames[0].shape[1], frames[0].shape[0]], "anchor_px": list(anchor),
                "frames": records, "clips": built,
                "states": states if states is not None else {"idle": next(iter(clips))},
                **(top or {})}
    path = folder / "animation-clips.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def hero(tmp_path: Path, **kwargs) -> Path:
    """Six distinct frames; idle reuses a frame (pingpong expanded), attack is a oneshot with events."""
    frames = [sprite_frame(i) for i in range(6)]
    clips = {
        "idle": {"frames": [0, 1, 2, 1], "duration_ms": [100, 100, 100, 100], "loop": True, "loop_policy": "pingpong",
                 "events_ms": [{"name": "step_l", "at_ms": 0, "at": 0}, {"name": "step_r", "at_ms": 200, "at": 2}],
                 "stride_world_units": 24, "entry_frame": 0},
        "attack": {"frames": [3, 4, 5], "duration_ms": [80, 60, 200], "loop": False, "loop_policy": "oneshot",
                   "events_ms": [{"name": "tell", "at_ms": 80, "at": 1}, {"name": "hit", "at_ms": 140, "at": 2,
                                                                              "data": {"damage": 2}}],
                   "keys": {"wind_start": 0, "strike": 2}, "hitstop_ticks": 4,
                   "transitions": [{"to": "idle", "entry_frame": 0, "dissolve_ms": 60, "mode": "dither"}]},
    }
    return write_built(tmp_path / "built", frames, clips, states={"idle": "idle", "attack": "attack"}, **kwargs)


def export(tmp_path: Path, clips: Path, *args: str, out: str = "engine") -> tuple[Path, dict]:
    output = tmp_path / out
    result = run_cli([TOOL, "--clips", clips, "--output-dir", output, *args])
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert result.stdout.isascii() and len(result.stdout.strip().splitlines()) == 1
    return output, summary


def canonical(pixels: np.ndarray) -> np.ndarray:
    clean = pixels.copy()
    clean[clean[..., 3] == 0] = 0
    return clean


# --------------------------------------------------------------------------- acceptance tests (plan B09-T1)

def test_aseprite_roundtrip(tmp_path):
    clips_path = hero(tmp_path)
    output, summary = export(tmp_path, clips_path, "--target", "aseprite-json", "--name", "hero")
    assert summary["targets"] == ["aseprite-json"] and summary["status"] == "pass"
    manifest = json.loads(clips_path.read_text(encoding="utf-8"))
    document = json.loads((output / "aseprite" / "hero.json").read_text(encoding="utf-8"))
    atlas = np.asarray(Image.open(output / "aseprite" / document["meta"]["image"]).convert("RGBA"))
    meta = document["meta"]
    assert [meta["size"]["w"], meta["size"]["h"]] == [atlas.shape[1], atlas.shape[0]]
    # Phaser createFromAseprite reads frames by "{frame}" names and tag ranges.
    assert [f["filename"] for f in document["frames"]] == [str(i) for i in range(7)]
    tags = {tag["name"]: tag for tag in meta["frameTags"]}
    assert (tags["idle"]["from"], tags["idle"]["to"], tags["attack"]["from"], tags["attack"]["to"]) == (0, 3, 4, 6)
    assert "repeat" not in tags["idle"] and tags["attack"]["repeat"] == "1"
    assert all(tag["direction"] == "forward" for tag in tags.values())    # pingpong arrives expanded
    sources = [canonical(np.asarray(Image.open(clips_path.parent / r["file"]))) for r in manifest["frames"]]
    for name, clip in manifest["clips"].items():
        tag = tags[name]
        entries = document["frames"][tag["from"]:tag["to"] + 1]
        assert [entry["duration"] for entry in entries] == clip["duration_ms"]
        for entry, index in zip(entries, clip["frames"]):
            rect = entry["frame"]
            crop = atlas[rect["y"]:rect["y"] + rect["h"], rect["x"]:rect["x"] + rect["w"]]
            assert np.array_equal(crop, sources[index])
            assert entry["sourceSize"] == {"w": 40, "h": 48} and entry["trimmed"] is False
        assert document["animations"][name] == [str(i) for i in range(tag["from"], tag["to"] + 1)]
    # Events ride in the tag user data, at timeline frames.
    assert json.loads(tags["attack"]["data"])["events"] == [{"name": "tell", "frame": 5, "at_ms": 80},
                                                            {"name": "hit", "frame": 6, "at_ms": 140}]
    assert json.loads(tags["idle"]["data"]) == {"events": [{"name": "step_l", "frame": 0, "at_ms": 0},
                                                           {"name": "step_r", "frame": 2, "at_ms": 200}],
                                                "loop_policy": "pingpong"}
    # The shared anchor is the slice pivot; repeated poses share one atlas cell.
    key = meta["slices"][0]["keys"][0]
    assert key["pivot"] == {"x": 20, "y": 44} and key["bounds"] == {"x": 0, "y": 0, "w": 40, "h": 48}
    assert len({(f["frame"]["x"], f["frame"]["y"]) for f in document["frames"]}) == 6


def godot_animations(text: str) -> list[dict]:
    """Independent reader of the animation blocks of a SpriteFrames .tres (not the tool's own parser)."""
    body = text.split("[resource]", 1)[1]
    pattern = r'"frames": \[(.*?)\],\n"loop": (true|false),\n"name": &"([^"]*)",\n"speed": ([-0-9.e]+)'
    blocks = re.findall(pattern, body, re.S)
    return [{"name": name, "loop": loop == "true", "speed": float(speed),
             "durations": [float(v) for v in re.findall(r'"duration": ([-0-9.e]+)', frames)]}
            for frames, loop, name, speed in blocks]


def test_spriteframes_relative_duration(tmp_path):
    frames = [sprite_frame(i) for i in range(6)]
    clips = {
        "idle": {"frames": [0, 1, 2, 3], "duration_ms": [100, 100, 100, 100], "loop": True},
        "attack": {"frames": [3, 4, 5, 0], "duration_ms": [80, 60, 120, 200], "loop": False},
        # 5, 5 and 6 ticks at 60 Hz, rounded to whole ms on cumulative edges (83, 167, 267).
        "dash": {"frames": [1, 2, 3], "duration_ms": [83, 84, 100], "loop": True, "ticks": [5, 5, 6], "tick_hz": 60},
    }
    clips_path = write_built(tmp_path / "built", frames, clips)
    output, _ = export(tmp_path, clips_path, "--target", "godot-spriteframes", "--name", "hero")
    animations = {a["name"]: a for a in godot_animations((output / "godot" / "hero.tres").read_text(encoding="utf-8"))}
    assert list(animations) == ["idle", "attack", "dash"]
    for name in ("idle", "attack"):
        animation, ms = animations[name], clips[name]["duration_ms"]
        assert animation["loop"] is clips[name]["loop"]
        for relative, duration in zip(animation["durations"], ms):
            assert relative == pytest.approx(duration * animation["speed"] / 1000, abs=1e-12)
    assert animations["idle"]["speed"] == 10.0 and animations["idle"]["durations"] == [1.0] * 4
    assert animations["attack"]["speed"] == pytest.approx(1000 / 60)
    assert animations["dash"]["speed"] == 60.0 and animations["dash"]["durations"] == [5.0, 5.0, 6.0]
    record = json.loads((output / "engine-export.json").read_text(encoding="utf-8"))
    assert record["clips"]["dash"]["godot"]["basis"] == "ticks at 60 Hz"
    # A fixed Godot fps applies ms x fps / 1000 to every clip.
    fixed, _ = export(tmp_path, clips_path, "--target", "godot-spriteframes", "--name", "hero", "--godot-fps", "12",
                      out="fixed")
    for animation in godot_animations((fixed / "godot" / "hero.tres").read_text(encoding="utf-8")):
        assert animation["speed"] == 12.0
        assert animation["durations"] == pytest.approx([d * 12 / 1000 for d in clips[animation["name"]]["duration_ms"]])
    scene = (output / "godot" / "hero.tscn").read_text(encoding="utf-8")
    assert "texture_filter = 2" in scene and "offset = Vector2(-20.0, -44.0)" in scene and "centered = false" in scene


def test_sprite3d_paths_relative(tmp_path):
    clips_path = hero(tmp_path)
    output, _ = export(tmp_path, clips_path, "--target", "godot-sprite3d", "--name", "hero", "--world-height", "1.6")
    # Move the export somewhere else and delete the source bundle: every reference must still resolve.
    moved = tmp_path / "elsewhere" / "deep" / "hero-engine"
    shutil.copytree(output, moved)
    shutil.rmtree(clips_path.parent)
    folder = moved / "godot-sprite3d"
    bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
    assert_valid_sprite(bundle, "godot_sprite3d_bundle_v1")
    assert bundle["default_action"] == "idle" and set(bundle["actions"]) == {"idle", "attack"}
    for action in bundle["actions"].values():
        contract_path = folder / action["contract"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        assert_valid_sprite(contract, "godot_sprite3d_v1")
        assert contract["recommended_pixel_size"] == pytest.approx(1.6 / contract["reference_subject_height_px"])
        assert contract["sprite3d_offset"] == [0.0, 20.0]     # [w/2 - ax, ay - h/2], +Y up
        for relative, digest in zip(contract["frames"], contract["frame_sha256"]):
            assert not re.match(r"^(/|[A-Za-z]:)", relative) and "\\" not in relative
            assert hashlib.sha256((contract_path.parent / relative).read_bytes()).hexdigest() == digest
    tres = (folder / "hero.tres").read_text(encoding="utf-8")
    for path in re.findall(r'path="([^"]+)"', tres):
        assert (folder / path).is_file() and not re.match(r"^(/|[A-Za-z]:|res://)", path)
    scene = (folder / "hero.tscn").read_text(encoding="utf-8")
    assert 'type="AnimatedSprite3D"' in scene and "billboard = 1" in scene and "offset = Vector2(0.0, 20.0)" in scene
    # No written text file mentions an absolute path of this machine.
    for path in moved.rglob("*"):
        if path.suffix in (".json", ".tres", ".tscn"):
            text = path.read_text(encoding="utf-8")
            assert str(tmp_path) not in text and tmp_path.as_posix() not in text, path


# --------------------------------------------------------------------------- standard CLI tests

def test_help_under_cp1252_and_cp950():
    assert_cli_help("generate2dsprite", "export_engine")


def test_refuses_existing_output_directory(tmp_path):
    clips_path = hero(tmp_path)
    output = tmp_path / "engine"
    output.mkdir()
    (output / "keep.txt").write_text("accepted asset", encoding="utf-8")
    result = run_cli([TOOL, "--clips", clips_path, "--output-dir", output], "cp1252")
    assert result.returncode == 1 and result.stderr.startswith("error: ") and "Traceback" not in result.stderr
    assert sorted(p.name for p in output.iterdir()) == ["keep.txt"]


def test_qc_failure_publishes_nothing(tmp_path):
    clips_path = hero(tmp_path)
    tampered = clips_path.parent / "frames" / "frame-04.png"
    pixels = np.asarray(Image.open(tampered)).copy()
    pixels[30, 20] = (1, 2, 3, 255)
    Image.fromarray(pixels).save(tampered)
    result = run_cli([TOOL, "--clips", clips_path, "--output-dir", tmp_path / "engine"])
    assert result.returncode == 1 and "rgba_pixel_sha256" in result.stderr
    assert not (tmp_path / "engine").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["built"]


def test_failed_round_trip_publishes_nothing(tmp_path, monkeypatch):
    """A writer bug caught by the round trip (a corrupted atlas) must leave no output and no stage."""
    clips_path = hero(tmp_path)
    original = E.Job.write_png

    def corrupt(job, path, pixels):
        damaged = pixels.copy()
        damaged[pixels[..., 3] > 0] ^= np.uint8(1)
        original(job, path, damaged)
    monkeypatch.setattr(E.Job, "write_png", corrupt)
    assert E.main(["--clips", str(clips_path), "--output-dir", str(tmp_path / "engine")]) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["built"]


# --------------------------------------------------------------------------- behaviour

def test_v1_manifest_from_the_real_builder(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    for i in range(4):
        Image.fromarray(sprite_frame(i)).save(source / f"walk-{i}.png")
    (source / "clips.json").write_text(json.dumps({
        "schema": V1, "frames": [f"walk-{i}.png" for i in range(4)], "anchor_px": [20, 44],
        "clips": {"walk": {"frames": [0, 1, 2, 3], "duration_ms": [90, 90, 90, 90], "loop": True},
                  "pose": {"frames": [2], "duration_ms": 300, "loop": False}},
        "states": {"moving": "walk"}}), encoding="utf-8")
    built = run_cli([BUILDER, "--manifest", source / "clips.json", "--output-dir", tmp_path / "built"])
    assert built.returncode == 0, built.stderr
    clips_path = tmp_path / "built" / "animation-clips.json"
    output, summary = export(tmp_path, clips_path, "--world-height", "2")
    assert summary["status"] == "pass" and summary["clips"] == 2 and summary["frames"] == 4
    record = json.loads((output / "engine-export.json").read_text(encoding="utf-8"))
    assert record["source"]["schema"] == json.loads(clips_path.read_text(encoding="utf-8"))["schema"]
    assert record["default_clip"] == "walk"          # no idle state: the first clip
    assert_valid_contract(record["qa"], "common", "qaEnvelope")
    assert_valid_sprite(record, "engine_export_v1")


def test_engine_export_document_and_contracts(tmp_path):
    clips_path = hero(tmp_path, top={"pixel_art": True, "art_source": "code", "placeholder": False})
    manifest = json.loads(clips_path.read_text(encoding="utf-8"))
    assert_valid_contract(manifest, "sprite", "animation_clips_v2", skill="generate2dsprite")
    output, summary = export(tmp_path, clips_path, "--name", "hero", "--camera-pitch-deg", "30", "--ppu", "16")
    record = json.loads((output / "engine-export.json").read_text(encoding="utf-8"))
    assert_valid_contract(record["qa"], "common", "qaEnvelope", skill="generate2dsprite")
    assert_valid_sprite(record, "engine_export_v1")
    assert record["qa"]["status"] == summary["status"] == "pass"
    # D29: the QA envelope and the document name the package version.
    assert record["tool"]["version"] == record["qa"]["tool"]["version"] == E.forge_core.FORGE_PACKAGE_VERSION == "0.4.0"
    assert {c["id"] for c in record["qa"]["checks"]} >= {"aseprite_roundtrip", "godot_spriteframes_roundtrip",
                                                         "godot_sprite3d_frames_roundtrip", "sprite3d_paths_relative"}
    assert any("not run" in text for text in record["qa"]["notProven"])
    written = {item["path"] for item in record["files"]}
    assert written == {p.relative_to(output).as_posix() for p in output.rglob("*")
                       if p.is_file() and p.name != "engine-export.json"}
    for item in record["files"]:
        assert hashlib.sha256((output / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert record["source"]["path"] == "../built/animation-clips.json"
    # Clip metadata a runtime needs: events (D12: at authored, position played), hints, keys and the Godot timing.
    attack = record["clips"]["attack"]
    assert attack["events_ms"][1] == {"name": "hit", "at_ms": 140, "at": 2, "position": 2, "data": {"damage": 2}}
    # The fixture's legacy hint items in transitions are read (D13) and written as transition_hints.
    assert attack["transition_hints"] == [{"to": "idle", "entry_frame": 0, "dissolve_ms": 60, "mode": "dither"}]
    assert "transitions" not in attack
    assert attack["keys"] == {"wind_start": 0, "strike": 2} and attack["hitstop_ticks"] == 4
    assert record["clips"]["idle"]["stride_world_units"] == 24
    # pixel_art -> nearest everywhere; the mapping table follows the anchor.
    assert record["sampling"] == "nearest"
    assert "texture_filter = 1" in (output / "godot" / "hero.tscn").read_text(encoding="utf-8")
    assert "texture_filter = 0" in (output / "godot-sprite3d" / "hero.tscn").read_text(encoding="utf-8")
    mapping = record["mapping"]
    assert mapping["unity"] == {"pivot": [0.5, pytest.approx(1 - 44 / 48)], "pixels_per_unit": 16.0,
                                "filter_mode": "Point"}
    assert mapping["phaser"]["origin"] == [0.5, pytest.approx(44 / 48)]
    assert mapping["godot"]["animated_sprite_2d"]["offset"] == [-20.0, -44.0]
    assert mapping["pitch_compensation"]["scale_y"] == pytest.approx(1 / math.cos(math.radians(30)))


def test_paging_keeps_every_clip_on_one_page(tmp_path):
    frames = [sprite_frame(i) for i in range(6)]
    clips = {"a": {"frames": [0, 1, 2], "duration_ms": [100] * 3, "loop": True},
             "b": {"frames": [3, 4, 5], "duration_ms": [100] * 3, "loop": True},
             "c": {"frames": [0, 3], "duration_ms": [100] * 2, "loop": False}}
    clips_path = write_built(tmp_path / "built", frames, clips)
    # 40x48 cells with padding 2 and extrude 1: a 128 px page holds 2 x 2 cells.
    output, summary = export(tmp_path, clips_path, "--max-atlas-size", "128", "--name", "hero")
    assert summary["pages"] == 2
    page_of = {}
    for page in (0, 1):
        document = json.loads((output / "aseprite" / f"hero-{page:02d}.json").read_text(encoding="utf-8"))
        assert max(document["meta"]["size"].values()) <= 128
        for tag in document["meta"]["frameTags"]:
            page_of[tag["name"]] = page
            assert tag["from"] >= 0 and tag["to"] < len(document["frames"])
    assert page_of == {"a": 0, "b": 1, "c": 1}
    tres = (output / "godot" / "hero.tres").read_text(encoding="utf-8")
    assert tres.count('[ext_resource type="Texture2D"') == 2
    big = write_built(tmp_path / "big", frames,
                      {"long": {"frames": [0, 1, 2, 3, 4], "duration_ms": [100] * 5, "loop": True}})
    result = run_cli([TOOL, "--clips", big, "--output-dir", tmp_path / "big-out", "--max-atlas-size", "128"])
    assert result.returncode == 1 and "needs 5 distinct frames" in result.stderr
    assert not (tmp_path / "big-out").exists()


def test_extrude_replicates_cell_edges(tmp_path):
    frames = [sprite_frame(0, edge_touch=True), sprite_frame(1)]
    clips_path = write_built(tmp_path / "built", frames,
                             {"idle": {"frames": [0, 1], "duration_ms": [100, 100], "loop": True}})
    output, _ = export(tmp_path, clips_path, "--target", "aseprite-json", "--name", "hero", "--padding", "3",
                       "--extrude", "2")
    document = json.loads((output / "aseprite" / "hero.json").read_text(encoding="utf-8"))
    atlas = np.asarray(Image.open(output / "aseprite" / "hero.png").convert("RGBA"))
    rect = document["frames"][0]["frame"]
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    cell = atlas[y:y + h, x:x + w]
    assert np.array_equal(atlas[y:y + h, x - 2], cell[:, 0]) and np.array_equal(atlas[y:y + h, x - 1], cell[:, 0])
    assert np.array_equal(atlas[y - 1, x:x + w], cell[0])
    assert np.array_equal(atlas[y - 2, x - 2], cell[0, 0])
    assert (x, y) == (5, 5) and not atlas[:3].any() and not atlas[:, :3].any()   # padding stays transparent


def test_clips_input_manifest_points_to_the_builder(tmp_path):
    clips = tmp_path / "clips.json"
    clips.write_text(json.dumps({"schema": V2, "frames": ["a.png"], "anchor_px": [1, 1],
                                 "clips": {"idle": {"frames": [0], "duration_ms": 100, "loop": True}}}),
                     encoding="utf-8")
    result = run_cli([TOOL, "--clips", clips, "--output-dir", tmp_path / "out"])
    assert result.returncode == 1 and "build_animation_clips.py" in result.stderr
    assert not (tmp_path / "out").exists()


def test_fractional_anchor_rounds_the_aseprite_pivot_and_warns(tmp_path):
    clips_path = hero(tmp_path, anchor=(20.5, 43.25))
    output, summary = export(tmp_path, clips_path, "--target", "aseprite-json", "--name", "hero")
    assert summary["status"] == "warn"
    document = json.loads((output / "aseprite" / "hero.json").read_text(encoding="utf-8"))
    slice_ = document["meta"]["slices"][0]
    assert slice_["keys"][0]["pivot"] == {"x": 21, "y": 43}
    assert json.loads(slice_["data"]) == {"anchor_px": [20.5, 43.25]}
    record = json.loads((output / "engine-export.json").read_text(encoding="utf-8"))
    check = next(c for c in record["qa"]["checks"] if c["id"] == "aseprite_pivot_rounding_px")
    assert check["status"] == "warn" and check["value"] == 0.5


def test_identical_inputs_give_identical_bytes(tmp_path):
    clips_path = hero(tmp_path)
    first, _ = export(tmp_path, clips_path, "--name", "hero", out="one")
    second, _ = export(tmp_path, clips_path, "--name", "hero", out="two")
    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
    for relative in files:
        assert (first / relative).read_bytes() == (second / relative).read_bytes(), relative


def test_subject_height_and_pixel_size_sources(tmp_path):
    clips_path = hero(tmp_path, top={"body_height_px": 32})
    output, _ = export(tmp_path, clips_path, "--target", "godot-sprite3d", "--name", "hero", "--world-height", "1.6")
    scale = json.loads((output / "engine-export.json").read_text(encoding="utf-8"))["scale"]
    assert scale["subject_height_px"] == 32.0 and scale["pixel_size"] == pytest.approx(0.05)
    measured, _ = export(tmp_path, hero(tmp_path / "m"), "--target", "godot-sprite3d", "--name", "hero", out="measured")
    scale = json.loads((measured / "engine-export.json").read_text(encoding="utf-8"))["scale"]
    # Visible rows (alpha > 32) of frames 0, 1, 2 are 33, 32 and 31 tall: the median is 32.
    assert scale["subject_height_px"] == 32.0 and scale["pixel_size"] == 0.01
    assert scale["source"] == "Godot default pixel size"
    result = run_cli([TOOL, "--clips", clips_path, "--output-dir", tmp_path / "bad", "--reference-clip", "run"])
    assert result.returncode == 1 and "--reference-clip" in result.stderr


def test_action_names_are_bundle_safe_and_unique():
    clips = [E.Clip(name, [0], [100], True, "cycle", [], {}, []) for name in ("Walk Left", "walk-left", "_x", "Idle")]
    assert E.action_names(clips) == {"Walk Left": "walk-left", "walk-left": "walk-left-2", "_x": "x", "Idle": "idle"}


def test_file_refs_never_record_absolute_paths(tmp_path, monkeypatch):
    """export_engine records its inputs with forge_core.file_ref (D30): another drive keeps only the file name."""
    target = tmp_path / "frame.png"
    target.write_bytes(b"png")
    monkeypatch.setattr(E.forge_core, "portable_path", lambda path, base: "D:/other-drive/frame.png")
    assert E.forge_core.file_ref(target, tmp_path)["path"] == "frame.png"
    assert not hasattr(E, "_local_file_ref") and not hasattr(E, "_local_round_half_up")


def test_godot_parser_reads_back_what_the_writer_writes():
    text = E.spriteframes_text([("1_a", "a.png")], [("Atlas_0", "1_a", (1, 2, 3, 4))], [
        {"name": 'odd "name"', "loop": False, "speed": 16.666666666666668,
         "frames": [('SubResource("Atlas_0")', 1.3333333333333333), ('SubResource("Atlas_0")', 2.0)]}])
    sections = E.parse_godot_resource(text)
    assert [s["tag"] for s in sections] == ["gd_resource", "ext_resource", "sub_resource", "resource"]
    assert sections[2]["properties"]["region"] == ("Rect2", [1, 2, 3, 4])
    animation = sections[3]["properties"]["animations"][0]
    assert animation["name"] == ("StringName", 'odd "name"') and animation["loop"] is False
    assert animation["frames"][0] == {"duration": 1.3333333333333333, "texture": ("SubResource", ["Atlas_0"])}
    with pytest.raises(E.ExportError):
        E.parse_godot_resource(text.replace("]\n", "\n", 1))


def test_engine_export_doc_has_single_line_commands_and_says_imports_are_unverified():
    doc = (script_path("generate2dsprite", "export_engine").parents[1] / "references" / "engine-export.md")
    text = doc.read_text(encoding="utf-8")
    assert "not verified in editors" in text.lower()
    commands = [line for block in re.findall(r"```bash\n(.*?)```", text, re.S) for line in block.splitlines() if line]
    assert len(commands) >= 4
    for command in commands:
        assert command.startswith('python "<skill-dir>/scripts/export_engine.py" ') and not command.endswith("\\")
        options = re.findall(r"--[a-z-]+", command)
        assert set(options) <= {action for action in E.build_parser()._option_string_actions}, command
    for row in ("Godot AnimatedSprite2D", "Godot Sprite3D", "Unity", "Phaser", "PixiJS", "billboard"):
        assert row in text


# --------------------------------------------------------------------------- D12/D13 on real build_animation_clips.py output

REAL_CLIPS = {
    # pingpong from ticks: 3 authored poses play 0 1 2 1; step_l (authored position 1) fires twice.
    "idle": {"frames": [0, 1, 2], "ticks": 6, "loop_policy": "pingpong",
             "events": [{"at": 1, "name": "step_l"}, {"at": 2, "name": "step_r"}]},
    "attack": {"frames": [3, 4, 5], "duration_ms": [80, 60, 200], "loop": False,
               "events": [{"at": 1, "name": "tell"}, {"at": 2, "name": "hit", "data": {"damage": 2}}],
               "keys": {"wind_start": 0, "strike": 2}, "hitstop_ticks": 4,
               "transitions": [{"to": "idle", "entry_frame": 1, "dissolve_ms": 60, "mode": "dither"}]},
    # Uneven authored ticks: Godot must play them mapped onto the played order 0 1 2 3 2 1.
    "walk": {"frames": [0, 1, 2, 3], "ticks": [5, 5, 6, 4], "loop_policy": "pingpong", "entry_frame": 2,
             "events": [{"at": 0, "name": "step_l"}, {"at": 2, "name": "step_r"}]},
}


def build_real(tmp_path: Path, clips: dict = REAL_CLIPS) -> Path:
    """Run the real build_animation_clips.py on a v2 clips manifest and return its animation-clips.json."""
    source = tmp_path / "src"
    source.mkdir(parents=True)
    for i in range(6):
        Image.fromarray(sprite_frame(i)).save(source / f"f{i}.png")
    (source / "clips.json").write_text(json.dumps({
        "schema": V2, "frames": [f"f{i}.png" for i in range(6)], "anchor_px": [20, 44], "clips": clips,
        "states": {"idle": "idle", "attack": "attack"}}), encoding="utf-8")
    built = run_cli([BUILDER, "--manifest", source / "clips.json", "--output-dir", tmp_path / "built", "--no-reviews"])
    assert built.returncode == 0, built.stderr
    return tmp_path / "built" / "animation-clips.json"


def shown_at(durations: list[int], at_ms: int) -> int:
    """The played frame a player shows at at_ms (an end-edge event names the last frame)."""
    edges = np.cumsum([0, *durations])
    return min(len(durations) - 1, int(np.searchsorted(edges, at_ms, side="right")) - 1)


def test_real_builder_v2_events_ticks_and_hints(tmp_path):
    """Reviewer repro b02_to_b09_e2e.py: B02's v2 output keeps at authored, position played, ticks authored and
    the hints in transition_hints (D12, D13); every engine output must place and time them by the played frames."""
    clips_path = build_real(tmp_path)
    built = json.loads(clips_path.read_text(encoding="utf-8"))["clips"]
    # What B02 writes (if this changes, the D12/D13 readers below must follow).
    assert built["idle"]["frames"] == [0, 1, 2, 1] and built["idle"]["authored_frames"] == [0, 1, 2]
    assert built["idle"]["ticks"] == [6, 6, 6] and len(built["idle"]["duration_ms"]) == 4
    assert [(e["name"], e["at"], e["position"]) for e in built["idle"]["events_ms"]] == [
        ("step_l", 1, 1), ("step_r", 2, 2), ("step_l", 1, 3)]
    assert built["attack"]["transition_hints"][0]["to"] == "idle"
    assert all("to" not in item for item in built["attack"]["transitions"])  # the v1 frame-step metrics

    output, summary = export(tmp_path, clips_path, "--name", "hero", "--world-height", "1.6")
    assert summary["status"] == "pass"
    record = json.loads((output / "engine-export.json").read_text(encoding="utf-8"))
    assert_valid_sprite(record, "engine_export_v1")
    clips = record["clips"]
    for name, clip in clips.items():
        source_events = built[name]["events_ms"]
        assert [(e["name"], e["at_ms"], e["at"], e["position"]) for e in clip["events_ms"]] == [
            (e["name"], e["at_ms"], e["at"], e["position"]) for e in source_events], name
        for event in clip["events_ms"]:
            assert event["position"] == shown_at(clip["duration_ms"], event["at_ms"]), (name, event)
    # Hints survive (D13), with the builder's resolved entry_ms and dissolve_ticks.
    assert clips["attack"]["transition_hints"] == built["attack"]["transition_hints"]
    assert "transition_hints" not in clips["idle"] and "transitions" not in clips["attack"]
    # Authored ticks drive Godot on the played frames (D12): 0 1 2 1 and 0 1 2 3 2 1.
    assert clips["idle"]["godot"] == {"speed": 60.0, "basis": "ticks at 60 Hz", "relative_durations": [6.0] * 4}
    assert clips["walk"]["godot"]["basis"] == "ticks at 60 Hz"
    assert clips["walk"]["godot"]["relative_durations"] == [5.0, 5.0, 6.0, 4.0, 6.0, 5.0]
    # ticks, keys and entry_frame stay authored positions; authored_frames and the *_ms times come along.
    assert clips["idle"]["ticks"] == [6, 6, 6] and clips["idle"]["authored_frames"] == [0, 1, 2]
    assert (clips["walk"]["entry_frame"], clips["walk"]["entry_ms"]) == (2, built["walk"]["entry_ms"])
    assert clips["attack"]["keys_ms"] == built["attack"]["keys_ms"] and clips["attack"]["hitstop_ms"] == 67
    animations = {a["name"]: a for a in godot_animations((output / "godot" / "hero.tres").read_text(encoding="utf-8"))}
    assert animations["idle"]["speed"] == 60.0 and animations["idle"]["durations"] == [6.0] * 4
    assert animations["walk"]["durations"] == [5.0, 5.0, 6.0, 4.0, 6.0, 5.0]
    # Aseprite: every event sits on the frame that is shown at its at_ms (the second step_l on frame 3).
    document = json.loads((output / "aseprite" / "hero.json").read_text(encoding="utf-8"))
    durations = [entry["duration"] for entry in document["frames"]]
    for tag in document["meta"]["frameTags"]:
        events = json.loads(tag.get("data", "{}")).get("events", [])
        for event in events:
            assert event["frame"] - tag["from"] == shown_at(durations[tag["from"]:tag["to"] + 1], event["at_ms"])
        if tag["name"] == "idle":
            assert [(e["name"], e["frame"] - tag["from"]) for e in events] == [("step_l", 1), ("step_r", 2),
                                                                               ("step_l", 3)]
    contract = json.loads((output / "godot-sprite3d" / "action-idle.json").read_text(encoding="utf-8"))
    assert_valid_sprite(contract, "godot_sprite3d_v1")
    assert [e["position"] for e in contract["events_ms"]] == [1, 2, 3]


def test_transition_hints_win_over_frame_step_metrics(tmp_path):
    """D13: transition_hints first; legacy transitions items count only when they name a target with to."""
    frames = [sprite_frame(i) for i in range(4)]
    metrics = [{"from_position": 0, "to_position": 1, "premultiplied_rgb_mae": 3.0, "alpha_mae": 0.0,
                "changed_visible_pixels": 10}]
    hint = {"to": "idle", "entry_frame": 1, "entry_ms": 100, "dissolve_ms": 40, "dissolve_ticks": 2,
            "mode": "premultiplied"}
    clips = {"idle": {"frames": [0, 1], "duration_ms": [100, 100], "loop": True, "transitions": metrics},
             "attack": {"frames": [2, 3], "duration_ms": [80, 120], "loop": False,
                        "transitions": [*metrics, {"to": "attack", "entry_frame": 0}], "transition_hints": [hint]}}
    clips_path = write_built(tmp_path / "built", frames, clips)
    output, _ = export(tmp_path, clips_path, "--target", "aseprite-json", "--name", "hero")
    record = json.loads((output / "engine-export.json").read_text(encoding="utf-8"))
    assert record["clips"]["attack"]["transition_hints"] == [hint]
    assert "transition_hints" not in record["clips"]["idle"]
    assert_valid_sprite(record, "engine_export_v1")


def test_event_positions_must_agree_with_at_ms(tmp_path):
    """D12: at_ms is authoritative; a position or an authored at that contradicts it is refused, as is an
    event name outside common/eventName (reviewer b09_spot.py case 4). Nothing is published."""
    frames = [sprite_frame(i) for i in range(3)]
    idle = {"frames": [0, 1, 2, 1], "duration_ms": [100] * 4, "loop": True, "loop_policy": "pingpong",
            "authored_frames": [0, 1, 2]}
    cases = [
        ([{"name": "step_l", "at_ms": 300, "at": 1, "position": 1}], "position 1 disagrees with at_ms 300"),
        # at read as a played position (the pre-D12 reading) names authored position 3, which does not exist.
        ([{"name": "step_l", "at_ms": 300, "at": 3}], "shows authored position 1"),
        ([{"name": "footstep", "at_ms": 100, "at": 1}], "custom:<name>"),
    ]
    for number, (events, message) in enumerate(cases):
        clips_path = write_built(tmp_path / f"in{number}", frames, {"idle": {**idle, "events_ms": events}})
        result = run_cli([TOOL, "--clips", clips_path, "--output-dir", tmp_path / f"out{number}"])
        assert result.returncode == 1 and message in result.stderr, result.stderr
        assert "Traceback" not in result.stderr and not (tmp_path / f"out{number}").exists()
    # authored_frames must expand to frames under the loop policy.
    clips_path = write_built(tmp_path / "bad", frames, {"idle": {**idle, "authored_frames": [0, 2, 1]}})
    result = run_cli([TOOL, "--clips", clips_path, "--output-dir", tmp_path / "bad-out"])
    assert result.returncode == 1 and "pingpong playback of its authored_frames" in result.stderr
    # An event that gives only at_ms and its authored at lands on the played frame shown at that time.
    clips_path = write_built(tmp_path / "ok", frames, {"idle": {**idle, "events_ms": [
        {"name": "step_l", "at_ms": 100, "at": 1}, {"name": "custom:land", "at_ms": 300, "at": 1}]}})
    output, _ = export(tmp_path, clips_path, "--target", "aseprite-json", "--name", "hero", out="ok-out")
    tag = json.loads((output / "aseprite" / "hero.json").read_text(encoding="utf-8"))["meta"]["frameTags"][0]
    assert [e["frame"] for e in json.loads(tag["data"])["events"]] == [1, 3]


def test_bad_references_are_clean_errors(tmp_path):
    """Reviewer b09_spot.py case 3: a non-string state was an unhashable TypeError traceback."""
    frames = [sprite_frame(i) for i in range(2)]
    idle = {"frames": [0, 1], "duration_ms": [100, 100], "loop": True}
    cases = [
        ({"states": {"idle": ["idle"]}}, {}, "states must map state names to clip names"),
        ({"states": {"idle": "run"}}, {}, "states must map state names to clip names"),
        ({}, {"transition_hints": [{"to": "run"}]}, "'run', which is not a clip"),
        ({}, {"transition_hints": [{"to": "idle", "entry_frame": 2}]}, "entry_frame 2 is not one of its 2"),
        ({}, {"transition_hints": [{"to": "idle", "mode": "wipe"}]}, "mode must be dither or premultiplied"),
    ]
    for number, (manifest_extra, clip_extra, message) in enumerate(cases):
        clips_path = write_built(tmp_path / f"in{number}", frames, {"idle": {**idle, **clip_extra}}, **manifest_extra)
        result = run_cli([TOOL, "--clips", clips_path, "--output-dir", tmp_path / f"out{number}"])
        assert result.returncode == 1 and message in result.stderr, result.stderr
        assert "Traceback" not in result.stderr and not (tmp_path / f"out{number}").exists()


def test_faint_fx_exports_without_a_subject_height(tmp_path):
    """Reviewer b09_spot.py case 2: frames with nothing above alpha 32 need a subject height only for Sprite3D."""
    faint = []
    for i in range(3):
        pixels = np.zeros((32, 32, 4), np.uint8)
        pixels[8 + i:30, 8:24] = (200, 60 + 20 * i, 90, 30)
        faint.append(pixels)
    clips_path = write_built(tmp_path / "built", faint,
                             {"puff": {"frames": [0, 1, 2], "duration_ms": [50, 50, 50], "loop": False}},
                             anchor=(16, 30))
    output, summary = export(tmp_path, clips_path, "--target", "aseprite-json", "--name", "puff")
    record = json.loads((output / "engine-export.json").read_text(encoding="utf-8"))
    assert summary["status"] == "pass" and "scale" not in record
    assert record["mapping"]["godot"]["animated_sprite_3d"]["pixel_size"] == 0.01
    assert record["mapping"]["unity"]["pixels_per_unit"] == 100.0
    assert_valid_sprite(record, "engine_export_v1")
    result = run_cli([TOOL, "--clips", clips_path, "--output-dir", tmp_path / "s3d", "--target", "godot-sprite3d"])
    assert result.returncode == 1 and "--subject-height-px" in result.stderr and not (tmp_path / "s3d").exists()
    export(tmp_path, clips_path, "--target", "godot-sprite3d", "--subject-height-px", "22", out="s3d-ok")


def test_sprite3d_only_export_needs_no_atlas(tmp_path):
    """Reviewer b09_sprite3d_atlas.py: per-frame Sprite3D textures are not bound by the atlas page size."""
    frames = [sprite_frame(i) for i in range(5)]
    clips_path = write_built(tmp_path / "built", frames,
                             {"long": {"frames": [0, 1, 2, 3, 4], "duration_ms": [100] * 5, "loop": True}})
    # 40x48 cells with padding 2 and extrude 1: a 128 px page holds 4 distinct frames, so atlas targets refuse ...
    result = run_cli([TOOL, "--clips", clips_path, "--output-dir", tmp_path / "atlas", "--max-atlas-size", "128"])
    assert result.returncode == 1 and "needs 5 distinct frames" in result.stderr
    # ... and Sprite3D alone exports the clip.
    output, summary = export(tmp_path, clips_path, "--target", "godot-sprite3d", "--max-atlas-size", "128",
                             "--name", "hero", out="s3d")
    record = json.loads((output / "engine-export.json").read_text(encoding="utf-8"))
    assert summary["pages"] == 0 and record["atlas"]["pages"] == [] and record["clips"]["long"]["page"] == 0
    assert next(c for c in record["qa"]["checks"] if c["id"] == "atlas_max_side_px")["status"] == "skipped"
    assert not (output / "aseprite").exists() and not (output / "godot").exists()
    assert_valid_sprite(record, "engine_export_v1")


def test_error_conventions(tmp_path, monkeypatch, capsys):
    """D26: usage errors exit 2 with argparse's usage line. D27: anything unexpected is one clean line, exit 1.
    D28: a manifest with a UTF-8 BOM (Windows PowerShell 5.1) loads; NaN is refused."""
    usage = run_cli([TOOL, "--clips", "x.json", "--output-dir", tmp_path / "out", "--target", "everything"])
    assert usage.returncode == 2 and "usage:" in usage.stderr and "Traceback" not in usage.stderr
    clips_path = hero(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(E, "plan_pages", broken)
    assert E.main(["--clips", str(clips_path), "--output-dir", str(tmp_path / "engine")]) == 1
    assert capsys.readouterr().err.strip() == "error: internal error (RuntimeError: boom)"
    assert not (tmp_path / "engine").exists()
    monkeypatch.undo()
    clips_path.write_bytes(b"\xef\xbb\xbf" + clips_path.read_bytes())
    export(tmp_path, clips_path, "--target", "aseprite-json", "--name", "hero", out="bom")
    clips_path.write_bytes(clips_path.read_bytes().replace(b'"loop": true', b'"loop": NaN', 1))
    result = run_cli([TOOL, "--clips", clips_path, "--output-dir", tmp_path / "nan"])
    assert result.returncode == 1 and "not usable JSON" in result.stderr
