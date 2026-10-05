"""B06-video-registration: prepare_i2v_input.py and register_clip.py (plan B06-T1..T5).

Registration by construction: prepare_i2v_input records one reference transform and
register_clip applies its inverse to every frame. Takes are synthetic and deterministic:
the master is placed exactly as the job placed it (a perfect keyer returns that alpha),
then cropped, stretched, scaled, bobbed or zoomed the way providers and generators do.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (REPO_ROOT, assert_cli_help, assert_valid_contract, load_script, require_ffmpeg,
                             run_cli, script_path)

SKILL = "video2dsprite"
PREP = load_script(SKILL, "prepare_i2v_input")
RC = load_script(SKILL, "register_clip")
CORE = RC.forge_core  # the scripts' own vendored forge_core

MAGENTA = (255, 0, 255)
BOOT = (96, 62, 32)
FLASH = (60, 200, 40)


# --------------------------------------------------------------------------- schema requests (handoff section 5)

_COMMON = "common.schema.json#/$defs/"


def _ref(name: str) -> dict:
    return {"$ref": _COMMON + name}


_PAIR_POSITIVE = {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 2, "maxItems": 2}
_NULL_OR_PAIR = {"anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "number"}, "minItems": 2,
                                                "maxItems": 2}]}

REQUESTED_DEFS = {
    "registration_v1": {
        "description": "registration.json written by register_clip.py apply (registration by construction). One "
                       "transform maps every frame of the provider video onto the source canvas: output = video * "
                       "transform.inverse.scale + transform.inverse.offset + the frame's whole-pixel lock shift. "
                       "sourceSize/sourceAnchor are the padded canvas (animation.json sourceSize/sourceAnchor); "
                       "baseSize/baseAnchor the master canvas.",
        "type": "object",
        "required": ["schema", "mode", "jobSha256", "clip", "action", "profile", "master", "anchorMode", "keyColor",
                     "referenceCanvas", "referenceScale", "referenceOffset", "baseSize", "baseAnchor", "padding",
                     "sourceSize", "sourceAnchor", "video", "transform", "lock", "frames", "qa"],
        "properties": {
            "schema": {"const": "video2dsprite.registration.v1"},
            "mode": {"const": "construction"},
            "tool": _ref("toolInfo"),
            "jobSha256": _ref("sha256"),
            "job": _ref("fileRef"),
            "clip": {"type": "string", "minLength": 1},
            "action": {"type": "string", "minLength": 1},
            "profile": {"enum": ["actor", "fx"]},
            "master": {"allOf": [_ref("fileRef")], "required": ["size", "anchor"],
                       "properties": {"size": _ref("size2"), "anchor": _ref("point2"), "viewBox": _ref("box")}},
            "anchorMode": {"type": "string", "minLength": 1},
            "keyColor": _ref("keyColor"),
            "referenceCanvas": _ref("size2"),
            "referenceScale": {"type": "number", "exclusiveMinimum": 0},
            "referenceOffset": _ref("point2"),
            "baseSize": _ref("size2"),
            "baseAnchor": _ref("point2"),
            "padding": _ref("padding4"),
            "sourceSize": _ref("size2"),
            "sourceAnchor": _ref("point2"),
            "displayScale": {"type": "number", "exclusiveMinimum": 0},
            "masterGeometry": {"type": "object"},
            "video": {"type": "object", "required": ["size", "frameCount", "range"],
                      "properties": {"size": _ref("size2"), "frameCount": {"type": "integer", "minimum": 1},
                                     "range": {"type": "array", "items": {"type": "integer", "minimum": 0},
                                               "minItems": 2, "maxItems": 2},
                                     "frameDirectory": _ref("relPath")}},
            "transform": {"type": "object",
                          "required": ["fit", "fitApplied", "videoScale", "videoOffset", "inverse", "resampler"],
                          "properties": {"fit": {"enum": ["auto", "stretch", "cover", "contain"]},
                                         "fitApplied": {"enum": ["uniform", "stretch", "cover", "contain"]},
                                         "videoScale": _PAIR_POSITIVE, "videoOffset": _ref("point2"),
                                         "anchorVideo": _ref("point2"), "anchorOutput": _ref("point2"),
                                         "inverse": {"type": "object", "required": ["scale", "offset"],
                                                     "properties": {"scale": _PAIR_POSITIVE,
                                                                    "offset": _ref("point2")}},
                                         "resampler": {"enum": ["box", "lanczos", "nearest"]}}},
            "lock": {"type": "object", "required": ["modes"],
                     "properties": {"modes": {"type": "array", "items": {"enum": ["feet", "x", "hip"]},
                                              "uniqueItems": True},
                                    "maxShift": {"type": ["integer", "null"], "minimum": 0},
                                    "groundMinRun": {"type": "integer", "minimum": 1},
                                    "targets": {"type": "object"}}},
            "rest": {"anyOf": [{"type": "null"},
                               {"type": "object", "required": ["frames", "nativeFootBottom", "anchorError"],
                                "properties": {"frames": {"type": "array", "minItems": 1,
                                                          "items": {"type": "integer", "minimum": 0}},
                                               "nativeFootBottom": {"type": "number"},
                                               "anchorError": _ref("point2"),
                                               "heightRatio": {"type": "number", "minimum": 0},
                                               "areaRatio": {"type": "number", "minimum": 0}}}]},
            "nativeFootBottomRange": _NULL_OR_PAIR,
            "frames": {"type": "array", "minItems": 1,
                       "items": {"type": "object", "required": ["file", "sha256", "sourceIndex", "shift"],
                                 "properties": {"file": _ref("relPath"), "sha256": _ref("sha256"),
                                                "sourceIndex": {"type": "integer", "minimum": 0},
                                                "sourceFile": {"type": "string", "minLength": 1},
                                                "sourceSha256": _ref("sha256"),
                                                "shift": {"type": "array", "items": {"type": "integer"},
                                                          "minItems": 2, "maxItems": 2},
                                                "nativeFootBottom": {"type": ["number", "null"]}}}},
            "fx": {"type": "object",
                   "properties": {"edgeFadePx": {"type": "number", "minimum": 0}, "fadeRect": _ref("box"),
                                  "fadeInFrames": {"type": "integer", "minimum": 0},
                                  "dissolveTailFrames": {"type": "integer", "minimum": 0}}},
            "review": {"type": "object", "properties": {"path": _ref("relPath"),
                                                        "frames": {"type": "array",
                                                                   "items": {"type": "integer", "minimum": 0}}}},
            "characterProfile": {"allOf": [_ref("fileRef")]},
            "qa": _ref("qaEnvelope"),
        },
    },
    "palette_repair_v1": {
        "description": "palette-repair.json from register_clip.py palette-repair: a QA envelope plus the rule, regions, "
                       "palette and per-frame changes. Alpha never changes.",
        "type": "object",
        "allOf": [_ref("qaEnvelope")],
        "required": ["schema", "rule", "regions", "palette", "frames", "totalChangedPx", "alphaUnchanged"],
        "properties": {
            "schema": {"const": "video2dsprite.palette_repair.v1"},
            "rule": {"type": "object", "required": ["hueRange", "minSaturation", "minValue", "minAlpha"]},
            "regions": {"type": "array", "minItems": 1, "items": _ref("box")},
            "paletteSource": {"allOf": [_ref("fileRef")]},
            "palette": {"type": "array", "minItems": 1, "items": _ref("hexColor")},
            "frames": {"type": "array", "items": {"type": "object",
                                                  "required": ["file", "changedPx", "beforeSha256", "afterSha256"],
                                                  "properties": {"file": _ref("relPath"),
                                                                 "changedPx": {"type": "integer", "minimum": 0},
                                                                 "beforeSha256": _ref("sha256"),
                                                                 "afterSha256": _ref("sha256")}}},
            "totalChangedPx": {"type": "integer", "minimum": 0},
            "alphaUnchanged": {"const": True},
        },
    },
}

REQUESTED_PROPERTIES = {
    "registration_job_v1": {
        "tool": _ref("toolInfo"),
        "actionKind": {"enum": ["loop", "oneshot", "hold", "fx"]},
        "returnsToRest": {"type": "boolean"},
        "sourceSize": _ref("size2"),
        "sourceAnchor": _ref("point2"),
        "anchorMode": {"type": "string", "minLength": 1},
        "referenceRoot": _ref("point2"),
        "pixelArt": {"type": "boolean"},
        "placementResampler": {"enum": ["nearest", "lanczos"]},
        "keyChoice": {"type": "object"},
        "workRegionClipped": {"type": "array", "items": {"type": "number", "minimum": 0}, "minItems": 4,
                              "maxItems": 4},
        "margin": {"type": "number", "minimum": 0},
        "durationS": {"type": "number", "exclusiveMinimum": 0},
        "calmSpanS": {"type": "number", "minimum": 0},
        "timeline": {"type": "array", "items": {"type": "object", "required": ["startS", "endS", "text"],
                                                "properties": {"startS": {"type": "number", "minimum": 0},
                                                               "endS": {"type": "number", "exclusiveMinimum": 0},
                                                               "text": {"type": "string", "minLength": 1}}}},
        "input": {"allOf": [_ref("fileRef")], "properties": {"size": _ref("size2")}},
        "reviewGuide": {"allOf": [_ref("fileRef")]},
        "lint": {"type": "array", "items": {"type": "object", "required": ["rule", "message"]}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "registration_job_v1/master": {
        "viewBox": _ref("box"),
        "sourceName": {"type": "string", "minLength": 1},
    },
    "character_profile_v1": {
        "nativeFootBottomRange": _NULL_OR_PAIR,
        "tool": _ref("toolInfo"),
    },
    "character_profile_v1/registration": {
        "masterSha256": _ref("sha256"),
        "referenceCanvas": _ref("size2"),
        "fit": {"enum": ["auto", "stretch", "cover", "contain"]},
        "resampler": {"enum": ["box", "lanczos", "nearest"]},
        "groundMinRun": {"type": "integer", "minimum": 1},
        "restFootBottom": {"type": ["number", "null"]},
    },
    "character_profile_v1/clip": {
        "registration": _ref("fileRef"),
        "jobSha256": _ref("sha256"),
        "nativeFootBottom": {"type": ["number", "null"]},
    },
    "take_v1": {
        "job": _ref("fileRef"),
        "action": {"type": "string", "minLength": 1},
        "warnings": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "source": {"type": "object", "required": ["kind", "name", "frames", "size"],
                   "properties": {"kind": {"enum": ["frames", "video"]}, "frames": {"type": "integer", "minimum": 1},
                                  "size": _ref("size2"), "sha256": _ref("sha256"),
                                  "fps": {"type": "number", "exclusiveMinimum": 0}}},
        "qa": _ref("qaEnvelope"),
    },
}


def _video_schema_with_requests() -> dict:
    folder = REPO_ROOT / "skills" / SKILL / "references" / "schemas"
    video = copy.deepcopy(json.loads((folder / "video.schema.json").read_text(encoding="utf-8")))
    defs = video["$defs"]
    defs.update(copy.deepcopy(REQUESTED_DEFS))
    targets = {"registration_job_v1": defs["registration_job_v1"]["properties"],
               "registration_job_v1/master": defs["registration_job_v1"]["properties"]["master"]["properties"],
               "character_profile_v1": defs["character_profile_v1"]["properties"],
               "character_profile_v1/registration": defs["character_profile_v1"]["properties"]["registration"][
                   "properties"],
               "character_profile_v1/clip": defs["character_profile_v1"]["properties"]["clips"][
                   "additionalProperties"]["properties"],
               "take_v1": defs["take_v1"]["properties"]}
    for name, properties in REQUESTED_PROPERTIES.items():
        clash = set(properties) & set(targets[name])
        assert not clash, f"requested properties already exist in {name}: {clash}"
        targets[name].update(copy.deepcopy(properties))
    return video


def assert_requested(instance: dict, name: str) -> None:
    """Validate against the vendored video schema with the section 5 requests applied in memory."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    folder = REPO_ROOT / "skills" / SKILL / "references" / "schemas"
    common = json.loads((folder / "common.schema.json").read_text(encoding="utf-8"))
    video = _video_schema_with_requests()
    registry = Registry().with_resources([(common["$id"], DRAFT202012.create_resource(common)),
                                          (video["$id"], DRAFT202012.create_resource(video))])
    validator = Draft202012Validator({"$ref": f"{video['$id']}#/$defs/{name}"}, registry=registry)
    errors = [f"{error.json_path}: {error.message}" for error in validator.iter_errors(instance)]
    assert not errors, f"violates requested video/{name}:\n  " + "\n  ".join(errors)


# --------------------------------------------------------------------------- synthetic art and takes

def make_master(width: int = 64, height: int = 72) -> np.ndarray:
    """A textured RGBA character with two boots on the ground line (row height - 4)."""
    pixels = np.zeros((height, width, 4), np.uint8)
    cx, ground = width // 2, height - 4
    pixels[ground - 6:ground, cx - 12:cx - 3] = (*BOOT, 255)
    pixels[ground - 6:ground, cx + 3:cx + 12] = (*BOOT, 255)
    pixels[ground - 20:ground - 6, cx - 10:cx - 4] = (40, 60, 120, 255)
    pixels[ground - 20:ground - 6, cx + 4:cx + 10] = (40, 60, 120, 255)
    body = pixels[ground - 44:ground - 20, cx - 12:cx + 12]
    body[...] = (40, 110, 200, 255)
    body[::4] = (220, 200, 60, 255)
    pixels[ground - 42:ground - 26, cx - 18:cx - 12] = (40, 110, 200, 255)
    pixels[ground - 60:ground - 44, cx - 9:cx + 9] = (232, 190, 150, 255)
    pixels[ground - 54:ground - 51, cx - 5:cx - 2] = (20, 20, 30, 255)
    pixels[ground - 54:ground - 51, cx + 2:cx + 5] = (20, 20, 30, 255)
    pixels[ground - 64:ground - 58, cx - 10:cx + 10] = (70, 40, 20, 255)
    return pixels


def read_png(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA")).copy()


def write_frames(folder: Path, frames) -> Path:
    folder.mkdir(parents=True)
    for index, frame in enumerate(frames):
        CORE.save_png(np.asarray(frame), folder / f"frame_{index:06d}.png")
    return folder


def prepare(tmp_path: Path, name: str, *extra: str, master: np.ndarray | None = None, action: str = "idle",
            canvas: str = "320,180", master_name: str | None = None) -> tuple[Path, dict]:
    master_path = tmp_path / (master_name or f"{name}-master.png")
    CORE.save_png(make_master() if master is None else master, master_path)
    out = tmp_path / name
    PREP.run(["prepare", "--master", str(master_path), "--action", action, "--canvas", canvas,
              "--output-dir", str(out), *extra])
    path = out / "registration_job.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def placed(job_path: Path, dy: float = 0.0) -> np.ndarray:
    """The master on the provider canvas exactly as the job placed it, optionally bobbed by dy px."""
    job = RC.load_job(job_path)
    view = RC.load_master_view(job)
    if job.pixel_art:
        return PREP.place_master(view, job.scale, job.offset, job.canvas, True)
    return np.asarray(CORE.resample_rgba(view, job.scale, "lanczos", anchor_src=(0.0, 0.0),
                                         anchor_dst=(job.offset[0], job.offset[1] + dy), out_size=job.canvas))


def register(tmp_path: Path, job_path: Path, frames: Path, name: str, *extra: str) -> tuple[dict, list[np.ndarray]]:
    out = tmp_path / name
    RC.run(["apply", "--job", str(job_path), "--frames", str(frames), "--output-dir", str(out), *extra])
    document = json.loads((out / "registration.json").read_text(encoding="utf-8"))
    return document, [read_png(out / record["file"]) for record in document["frames"]]


def weighted_centroid(rgba: np.ndarray) -> np.ndarray:
    alpha = rgba[..., 3].astype(np.float64)
    ys, xs = np.mgrid[:alpha.shape[0], :alpha.shape[1]]
    return np.array([((xs + 0.5) * alpha).sum(), ((ys + 0.5) * alpha).sum()]) / alpha.sum()


def ground(rgba: np.ndarray) -> int:
    """Foot line of the body: the 50% alpha contour, as register_clip measures it."""
    return CORE.ground_row(rgba[..., 3] > 127, min_run=3)


def over_key(rgba: np.ndarray, key=MAGENTA) -> np.ndarray:
    alpha = rgba[..., 3:].astype(np.float64) / 255.0
    return np.floor(rgba[..., :3] * alpha + np.asarray(key, np.float64) * (1 - alpha) + 0.5).astype(np.uint8)


def stage_leftovers(parent: Path, name: str) -> list[Path]:
    return [path for path in parent.iterdir() if path.name.startswith(f".{name}.stage-")]


# --------------------------------------------------------------------------- B06-T1 prepare_i2v_input

def test_job_roundtrip_schema(tmp_path):
    job_path, job = prepare(tmp_path, "hero-attack", "--action", "attack", "--subject", "test knight",
                            master_name="hero.png")
    assert_valid_contract(job, "video", "registration_job_v1", skill=SKILL)
    assert_requested(job, "registration_job_v1")
    folder = job_path.parent
    assert {path.name for path in folder.iterdir()} == {"registration_job.json", "input.png", "prompt.txt",
                                                       "master.png", "review-guide.png"}
    assert CORE.sha256_file(folder / "master.png") == job["master"]["sha256"]
    assert CORE.sha256_file(folder / "prompt.txt") == job["prompt"]["sha256"]
    assert CORE.sha256_file(folder / "input.png") == job["input"]["sha256"]

    loaded = RC.load_job(job_path)
    assert loaded.source_size == tuple(job["sourceSize"]) == (64, 72)
    assert loaded.source_anchor == tuple(job["sourceAnchor"])
    assert loaded.scale == job["referenceScale"] and loaded.offset == tuple(job["referenceOffset"])
    assert loaded.padding == tuple(job["padding"]) and loaded.key == "magenta" and loaded.returns_to_rest
    # the recorded transform puts the root on the fixed pixel
    root = np.array(job["referenceOffset"]) + job["referenceScale"] * np.array(job["sourceAnchor"])
    np.testing.assert_allclose(root, job["referenceRoot"], atol=1e-9)
    np.testing.assert_allclose(job["referenceRoot"], [160, 155], atol=1e-9)
    # written with sorted, stable formatting: the same inputs give the same bytes
    again, _ = prepare(tmp_path, "hero-attack-again", "--action", "attack", "--subject", "test knight",
                       master_name="hero.png")
    for name in ("registration_job.json", "input.png", "prompt.txt"):
        assert (folder / name).read_bytes() == (again.parent / name).read_bytes(), name
    assert json.loads(json.dumps(job)) == job

    # a hand-written job with only the frozen v1 fields loads too
    minimal = {key: job[key] for key in ("schema", "master", "referenceCanvas", "referenceScale", "referenceOffset",
                                         "keyColor", "workRegion", "action", "padding", "prompt")}
    minimal["master"] = {key: job["master"][key] for key in ("path", "sha256", "size", "anchor")}
    assert_valid_contract(minimal, "video", "registration_job_v1", skill=SKILL)
    (folder / "minimal.json").write_text(json.dumps(minimal), encoding="utf-8")
    plain = RC.load_job(folder / "minimal.json")
    assert (plain.source_size, plain.source_anchor, plain.scale) == (loaded.source_size, loaded.source_anchor,
                                                                      loaded.scale)


def test_view_box_selects_one_view_of_a_sheet(tmp_path):
    sheet = np.zeros((72, 128, 4), np.uint8)
    sheet[:, :64] = make_master()
    sheet[:, 64:] = make_master()[:, ::-1]  # second view: mirrored
    job_path, job = prepare(tmp_path, "sheet", "--view-box", "64,0,128,72", master=sheet)
    assert job["master"]["viewBox"] == [64, 0, 128, 72] and job["sourceSize"] == [64, 72]
    assert job["master"]["anchor"][0] == job["sourceAnchor"][0] + 64
    assert_requested(job, "registration_job_v1")
    frames = write_frames(tmp_path / "sheet-frames", [placed(job_path)])
    document, out = register(tmp_path, job_path, frames, "sheet-reg")
    assert document["baseSize"] == [64, 72] and document["master"]["viewBox"] == [64, 0, 128, 72]
    np.testing.assert_allclose(weighted_centroid(out[0]), weighted_centroid(sheet[:, 64:]), atol=0.5)


def test_pixel_master_integer_scale(tmp_path):
    rng = np.random.default_rng(6)
    palette = np.array([(90, 70, 50), (140, 110, 80), (60, 60, 70), (200, 170, 140), (110, 90, 100)], np.uint8)
    master = np.zeros((20, 16, 4), np.uint8)
    master[2:20, 3:13, :3] = palette[rng.integers(0, len(palette), (18, 10))]  # muted: no key conflict
    master[2:20, 3:13, 3] = 255
    master[0:2, 6:10] = (200, 160, 120, 255)
    job_path, job = prepare(tmp_path, "pixel", "--pixel-art", master=master)
    scale = job["referenceScale"]
    assert isinstance(scale, int) and scale >= 2
    assert job["placementResampler"] == "nearest" and job["pixelArt"] is True
    ox, oy = job["referenceOffset"]
    assert float(ox).is_integer() and float(oy).is_integer()
    assert abs(job["referenceRoot"][0] - 160) <= 0.5 and abs(job["referenceRoot"][1] - 155) <= 0.5
    rgb = read_png(job_path.parent / "input.png")[..., :3]
    expected = np.repeat(np.repeat(over_key(master), scale, axis=0), scale, axis=1)
    ox, oy = int(ox), int(oy)
    assert np.array_equal(rgb[oy:oy + 20 * scale, ox:ox + 16 * scale], expected)
    outside = np.ones(rgb.shape[:2], bool)
    outside[oy:oy + 20 * scale, ox:ox + 16 * scale] = False
    assert (rgb[outside] == MAGENTA).all()
    # NEAREST back to the master grid is lossless for pixel art
    frames = write_frames(tmp_path / "pixel-frames", [placed(job_path)])
    _, out = register(tmp_path, job_path, frames, "pixel-reg", "--resampler", "nearest")
    assert np.array_equal(out[0], master)
    # a fractional scale is refused for pixel art and nothing is published
    with pytest.raises(ValueError, match="integer scale"):
        PREP.run(["prepare", "--master", str(tmp_path / "pixel-master.png"), "--action", "idle", "--pixel-art",
                  "--canvas", "320,180", "--scale", "2.5", "--output-dir", str(tmp_path / "pixel-bad")])
    assert not (tmp_path / "pixel-bad").exists()


def test_prompt_contains_work_region_and_timeline(tmp_path):
    job_path, job = prepare(tmp_path, "walk", "--action", "walk", "--facing", "right", "--subject", "test knight")
    prompt = (job_path.parent / "prompt.txt").read_text(encoding="utf-8")
    x0, y0, x1, y1 = job["workRegion"]
    assert f"work region x={x0}..{x1 - 1}, y={y0}..{y1 - 1} of this 320x180 image" in prompt
    assert x0 >= 20 and y0 >= 20 and x1 <= 300 and y1 <= 160
    assert "LOCKED camera" in prompt and "no zoom, push-in" in prompt
    assert "IN PLACE" in prompt and "treadmill" in prompt and "pair of steps about every 0.8 seconds" in prompt
    assert "rings, starbursts, pulsing glow" in prompt and "magenta-coloured light" in prompt
    assert "root fixed at pixel (160, 155)" in prompt and "facing screen right" in prompt
    for row in job["timeline"]:
        assert f"{row['startS']:.1f}-{row['endS']:.1f} s {row['text']}" in prompt
    assert job["timeline"][0]["startS"] == 0.0 and job["timeline"][0]["endS"] == 0.4  # early calm span
    assert job["timeline"][-1]["endS"] == 6.0 and job["lint"] == [] and job["warnings"] == []
    # every action template is lint-clean, and longer takes keep the 0.4 s calm span
    for action in PREP.ACTIONS:
        rows = PREP.timeline(PREP.ACTIONS[action], 8.0)
        assert rows[0]["endS"] == 0.4 and rows[-1]["endS"] == 8.0
        text = PREP.build_prompt(PREP.ACTIONS[action], subject="", facing=None, key="green",
                                 region=[20, 20, 300, 160], canvas=(320, 180), root=(160, 155), duration=8.0,
                                 rows=rows, pixel_art=False, extra=None)
        assert PREP.lint_prompt(text, "green") == [], action


def test_lint_warns_on_tiny_and_extremely_slow(tmp_path):
    findings = PREP.lint_prompt("Tiny breathing, extremely slow sway. Locked camera. Work region x=1..9, y=1..9; "
                                "0.0-0.4 s hold; no slow-motion freeze; no push-in.")
    weak = [item["match"] for item in findings if item["rule"] == "weak-amplitude"]
    assert weak == ["tiny", "extremely slow"]
    assert not [item for item in findings if item["rule"] != "weak-amplitude"]
    missing = {item["rule"] for item in PREP.lint_prompt("The hero waves; zoom in on the magenta glow.", "magenta")}
    assert {"camera-move", "missing-locked-camera", "missing-work-region", "missing-timeline",
            "key-coloured-light"} <= missing
    result = PREP.run(["lint", "--text", "a tiny wave"])
    assert result["status"] == "warn"
    with pytest.raises(ValueError, match="weak-amplitude"):
        PREP.run(["lint", "--text", "a tiny wave", "--strict"])
    with pytest.raises(ValueError, match="strict-lint"):
        prepare(tmp_path, "lint-strict", "--extra", "Keep it tiny.", "--strict-lint")
    assert not (tmp_path / "lint-strict").exists()


def test_key_choice_avoids_the_masters_colours(tmp_path):
    purple = make_master()
    purple[purple[..., 3] > 0, :3] = (180, 60, 200)  # a violet design fights magenta
    _, job = prepare(tmp_path, "violet", master=purple)
    assert job["keyColor"] == "green" and job["keyChoice"]["status"] == "ok"
    rgb = read_png(tmp_path / "violet" / "input.png")
    assert tuple(rgb[0, 0, :3]) == (0, 255, 0)
    with pytest.raises(ValueError, match="fight the key"):
        prepare(tmp_path, "violet-magenta", "--key", "magenta", master=purple)
    _, forced = prepare(tmp_path, "violet-forced", "--key", "magenta", "--allow-key-conflict", master=purple)
    assert forced["keyColor"] == "magenta" and any("overlaps" in item for item in forced["warnings"])


# --------------------------------------------------------------------------- B06-T2 apply

@pytest.mark.parametrize("variant", ["same", "crop-1264", "stretch-1264", "square-960"])
def test_anchor_error_within_half_pixel(tmp_path, variant):
    """One stored inverse transform registers takes the provider returned at another size."""
    canvas = "128,128" if variant == "square-960" else "320,180"
    job_path, job = prepare(tmp_path, "job", canvas=canvas)
    reference = placed(job_path)
    if variant == "crop-1264":       # Grok 1264x720 from 1280x720: a fixed centre crop
        video, extra = reference[:, 2:-2], []
    elif variant == "stretch-1264":  # a provider that resizes to the new width instead
        video = np.asarray(CORE.resample_rgba(reference, 1.0, "lanczos", out_size=(316, 180)))
        extra = ["--fit", "stretch"]
    elif variant == "square-960":    # the Ryo clip: 512x512 input, 960x960 output
        video, extra = np.asarray(CORE.resample_rgba(reference, 1.875, "lanczos")), []
    else:
        video, extra = reference, []
    frames = write_frames(tmp_path / "frames", [video, video])
    document, out = register(tmp_path, job_path, frames, "reg", *extra)
    master = make_master()
    expected = weighted_centroid(master)
    assert np.abs(weighted_centroid(out[0]) - expected).max() <= 0.5
    assert document["mode"] == "construction" and document["sourceAnchor"] == job["sourceAnchor"]
    assert document["transform"]["fitApplied"] == {"same": "uniform", "crop-1264": "cover", "stretch-1264": "stretch",
                                                   "square-960": "uniform"}[variant]
    # the stored inverse maps the job's root exactly onto the output anchor
    inverse = document["transform"]["inverse"]
    root_video = document["transform"]["anchorVideo"]
    mapped = [root_video[axis] * inverse["scale"][axis] + inverse["offset"][axis] for axis in (0, 1)]
    np.testing.assert_allclose(mapped, document["sourceAnchor"], atol=1e-9)
    assert {check["id"]: check["status"] for check in document["qa"]["checks"]}["rest-anchor-error"] == "pass"
    assert_requested(document, "registration_v1")
    assert_valid_contract(document["qa"], "common", "qaEnvelope", skill=SKILL)


def test_action_padding_canvas_and_anchor(tmp_path):
    """Base 448 plus [96, 80, 96, 24] is a 640x552 canvas with the anchor at [320, 510]; padding
    grows the canvas and shifts the anchor, never rescales the body."""
    master = np.zeros((448, 448, 4), np.uint8)
    master[110:430, 64:384] = make_master()[4:68].repeat(5, axis=0).repeat(5, axis=1)  # ground line 430, stance 224
    master_path = tmp_path / "big-master.png"
    CORE.save_png(master, master_path)
    PREP.run(["prepare", "--master", str(master_path), "--action", "attack", "--output-dir", str(tmp_path / "big")])
    job_path = tmp_path / "big" / "registration_job.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["sourceAnchor"] == [224.0, 430.0] and job["padding"] == [96, 80, 96, 24]
    frames = write_frames(tmp_path / "big-frames", [placed(job_path)])
    padded, padded_out = register(tmp_path, job_path, frames, "big-reg")
    assert padded["sourceSize"] == [640, 552] and padded["sourceAnchor"] == [320.0, 510.0]
    assert padded["baseSize"] == [448, 448] and padded["padding"] == [96, 80, 96, 24]
    plain, plain_out = register(tmp_path, job_path, frames, "big-plain", "--action-padding", "0,0,0,0")
    assert plain["sourceSize"] == [448, 448] and plain["sourceAnchor"] == [224.0, 430.0]
    # the same pixels, moved by the padding: one scale, pinned grid, nothing clamped
    difference = np.abs(padded_out[0][80:528, 96:544].astype(int) - plain_out[0].astype(int))
    assert difference.max() <= 1
    assert np.array_equal(padded_out[0][80:528, 96:544, 3] > 16, plain_out[0][..., 3] > 16)
    assert not padded_out[0][:80].any() and not padded_out[0][528:].any()


def test_registration_is_deterministic_and_validates(tmp_path):
    job_path, _ = prepare(tmp_path, "det")
    frames = write_frames(tmp_path / "det-frames", [placed(job_path, dy) for dy in (0.0, 1.5, 3.0)])
    first, _ = register(tmp_path, job_path, frames, "det-a")
    second, _ = register(tmp_path, job_path, frames, "det-b")
    documents = [(tmp_path / name / "registration.json").read_bytes() for name in ("det-a", "det-b")]
    assert documents[0] == documents[1]
    for record in first["frames"]:
        assert (tmp_path / "det-a" / record["file"]).read_bytes() == (tmp_path / "det-b" / record["file"]).read_bytes()
    assert first["jobSha256"] == CORE.sha256_file(job_path) and first["job"]["path"] == "../det/registration_job.json"
    assert [record["sourceIndex"] for record in first["frames"]] == [0, 1, 2]
    assert (tmp_path / "det-a" / "review-contact.png").is_file()
    assert_requested(first, "registration_v1")
    # --range keeps the take's frame 0 as the rest pose (rest frames are source indices)
    ranged, _ = register(tmp_path, job_path, frames, "det-range", "--range", "1:3")
    assert [record["sourceIndex"] for record in ranged["frames"]] == [1, 2] and ranged["video"]["range"] == [1, 3]
    assert ranged["rest"]["frames"] == [0] and ranged["rest"]["anchorError"] == first["rest"]["anchorError"]


def test_qc_accepts_keyed_frames(tmp_path):
    """Already keyed takes are judged on their alpha (edge purity = subject pixels in the band)."""
    job_path, _ = prepare(tmp_path, "keyed")
    frames = write_frames(tmp_path / "keyed-frames", [placed(job_path, dy) for dy in (0.0, 0.0, 1.0, 0.0)])
    result = RC.run(["qc", "--job", str(job_path), "--frames", str(frames), "--fps", "10"])
    assert result["status"] == "kept", result["reasons"]
    line = _take_line(job_path.parent / "takes.jsonl", "keyed-frames")
    assert line["metrics"]["cameraScale"]["start"] == pytest.approx(1.0, abs=1e-6)
    assert line["metrics"]["keyDrift"]["max"] == 0.0


# --------------------------------------------------------------------------- B06-T3 locks and profiles

def test_feet_lock_zero_ground_drift(tmp_path):
    job_path, _ = prepare(tmp_path, "bob")
    bob = (0.0, 3.0, 6.5, 2.0, -2.5, 0.0)  # video px: the feet drift up and down
    frames = write_frames(tmp_path / "bob-frames", [placed(job_path, dy) for dy in bob])
    loose, loose_out = register(tmp_path, job_path, frames, "bob-loose")
    assert len({ground(frame) for frame in loose_out}) > 1
    low, high = loose["nativeFootBottomRange"]
    assert high - low >= 2
    locked, locked_out = register(tmp_path, job_path, frames, "bob-locked", "--lock", "feet")
    rest = ground(locked_out[0])
    assert [ground(frame) for frame in locked_out] == [rest] * len(bob)  # zero ground drift
    shifts = [record["shift"] for record in locked["frames"]]
    assert shifts[0] == [0, 0] and any(dy for _, dy in shifts) and all(dx == 0 for dx, _ in shifts)
    for record, frame in zip(locked["frames"], loose_out):
        assert ground(frame) + record["shift"][1] == rest
    assert locked["lock"]["targets"]["source"] == "rest"
    assert_requested(locked, "registration_v1")


def test_profile_mismatch_fails(tmp_path):
    job_a, a = prepare(tmp_path, "idle")
    reg_a, _ = register(tmp_path, job_a, write_frames(tmp_path / "idle-frames", [placed(job_a)] * 2), "idle-reg")
    profile_path = tmp_path / "hero.profile.json"
    summary = RC.run(["profile", "--registration", str(tmp_path / "idle-reg" / "registration.json"), "--id", "hero",
                      "--output", str(profile_path)])
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert_valid_contract(profile, "video", "character_profile_v1", skill=SKILL)
    assert_requested(profile, "character_profile_v1")
    assert profile["registration"]["scale"] == a["referenceScale"] and summary["clips"] == ["idle"]
    assert profile["matte"] == {"mode": "soft", "key": "magenta", "erode": 0.0, "unmix": True, "despill": "auto"}

    # a clip prepared at another scale cannot inherit the profile, and nothing is published
    job_b, _ = prepare(tmp_path, "attack-small", "--action", "attack", "--scale", str(a["referenceScale"] * 0.8))
    frames_b = write_frames(tmp_path / "attack-small-frames", [placed(job_b)] * 2)
    with pytest.raises(ValueError, match="referenceScale"):
        register(tmp_path, job_b, frames_b, "attack-small-reg", "--character-profile", str(profile_path))
    assert not (tmp_path / "attack-small-reg").exists()
    register(tmp_path, job_b, frames_b, "attack-small-plain")
    with pytest.raises(ValueError, match="profile validation failed.*referenceScale"):
        RC.run(["validate-profile", "--profile", str(profile_path), "--registration",
                str(tmp_path / "attack-small-plain" / "registration.json")])
    with pytest.raises(ValueError, match="does not match"):
        RC.run(["profile", "--registration", str(tmp_path / "idle-reg" / "registration.json"), "--registration",
                str(tmp_path / "attack-small-plain" / "registration.json"), "--id", "hero",
                "--output", str(tmp_path / "mixed.profile.json")])
    assert not (tmp_path / "mixed.profile.json").exists()

    # a clip from the same master and scale inherits it and validates across clips
    job_c, _ = prepare(tmp_path, "attack", "--action", "attack")
    reg_c, _ = register(tmp_path, job_c, write_frames(tmp_path / "attack-frames", [placed(job_c)] * 2), "attack-reg",
                        "--character-profile", str(profile_path))
    assert reg_c["characterProfile"]["id"] == "hero"
    report = RC.run(["validate-profile", "--profile", str(profile_path),
                     "--registration", str(tmp_path / "idle-reg" / "registration.json"),
                     "--registration", str(tmp_path / "attack-reg" / "registration.json")])
    assert report["status"] == "pass" and report["clips"] == 2
    low, high = report["nativeFootBottomRange"]
    assert high - low <= 2 and abs(low - reg_a["masterGeometry"]["groundLine"]) <= 2


# --------------------------------------------------------------------------- B06-T4 take QC

def _qc_frames(input_rgb: np.ndarray, count: int, zoom: float = 0.0, flare: bool = False) -> list[np.ndarray]:
    """Opaque raw frames: the provider input with codec-like noise, a swinging hand mid-clip,
    an optional push-in about the frame centre and an optional flare at the left border."""
    rng = np.random.default_rng(3)
    height, width = input_rgb.shape[:2]
    opaque = np.dstack([input_rgb, np.full((height, width), 255, np.uint8)])
    frames = []
    for index in range(count):
        scale = 1.0 + zoom * index / (count - 1)
        frame = np.asarray(CORE.resample_rgba(opaque, scale, "lanczos", anchor_src=(width / 2, height / 2),
                                              anchor_dst=(width / 2, height / 2), out_size=(width, height)))[..., :3]
        frame = np.clip(frame.astype(np.int16) + rng.integers(-2, 3, frame.shape), 0, 255).astype(np.uint8)
        if count // 3 <= index < 2 * count // 3:
            x = 120 + 2 * (index % 5)
            frame[80:92, x:x + 8] = (40, 110, 200)
        if flare and index % 4 == 0:
            frame[60:100, 0:14] = (255, 240, 200)
        frames.append(frame)
    return frames


def _take_line(path: Path, take: str) -> dict:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return next(line for line in lines if line["take"] == take)


def test_qc_flags_push_in_and_keeps_locked_clip(tmp_path):
    job_path, _ = prepare(tmp_path, "qc-idle")
    input_rgb = read_png(job_path.parent / "input.png")[..., :3]
    locked = write_frames(tmp_path / "locked", _qc_frames(input_rgb, 30))
    push = write_frames(tmp_path / "push", _qc_frames(input_rgb, 30, zoom=0.12))
    flare = write_frames(tmp_path / "flare", _qc_frames(input_rgb, 30, flare=True))
    takes = job_path.parent / "takes.jsonl"
    kept = RC.run(["qc", "--job", str(job_path), "--frames", str(locked), "--take", "locked-1"])
    rejected = RC.run(["qc", "--job", str(job_path), "--frames", str(push), "--take", "push-1"])
    edged = RC.run(["qc", "--job", str(job_path), "--frames", str(flare), "--take", "flare-1"])
    assert kept["status"] == "kept" and kept["reasons"] == []
    assert rejected["status"] == "rejected" and any("push-in" in reason for reason in rejected["reasons"])
    assert edged["status"] == "rejected" and any("edge key purity" in reason for reason in edged["reasons"])
    for take in ("locked-1", "push-1", "flare-1"):
        line = _take_line(takes, take)
        assert_valid_contract(line, "video", "take_v1", skill=SKILL)
        assert_valid_contract(line["qa"], "common", "qaEnvelope", skill=SKILL)
        assert_requested(line, "take_v1")
    locked_line = _take_line(takes, "locked-1")
    inputs = locked_line["qa"]["inputs"]
    assert inputs[0]["path"] == "registration_job.json" and len(inputs) == 31
    assert inputs[1] == {"path": "../locked/frame_000000.png", "sha256": CORE.sha256_file(locked / "frame_000000.png")}
    assert abs(locked_line["metrics"]["cameraScale"]["end"] - 1) < 0.01
    assert locked_line["metrics"]["landmarks"]["perFrame"][0]["matched"] >= 3
    assert locked_line["metrics"]["identity"]["minCalm"] > 0.9
    assert _take_line(takes, "push-1")["metrics"]["cameraScale"]["end"] > 1.08
    # a take id is recorded once; the log is not rewritten
    before = takes.read_bytes()
    with pytest.raises(ValueError, match="already recorded"):
        RC.run(["qc", "--job", str(job_path), "--frames", str(locked), "--take", "locked-1"])
    with pytest.raises(ValueError, match="rejected"):
        RC.run(["qc", "--job", str(job_path), "--frames", str(push), "--take", "push-2", "--strict"])
    assert takes.read_bytes().startswith(before) and _take_line(takes, "push-2")["status"] == "rejected"


@pytest.mark.ffmpeg
def test_qc_reads_a_video_take(tmp_path):
    """qc --video decodes the take with forge_av; an H.264 round trip of a locked take is kept."""
    require_ffmpeg()
    av = load_script(SKILL, "forge_av")
    job_path, _ = prepare(tmp_path, "qc-video")
    input_rgb = read_png(job_path.parent / "input.png")[..., :3]
    video = tmp_path / "take-1.mp4"
    av.encode_h264_loop([np.dstack([frame, np.full(frame.shape[:2], 255, np.uint8)])
                         for frame in _qc_frames(input_rgb, 24)], video, "24/1", crf=12)
    result = RC.run(["qc", "--job", str(job_path), "--video", str(video)])
    assert result["take"] == "take-1" and result["status"] == "kept", result["reasons"]
    line = _take_line(job_path.parent / "takes.jsonl", "take-1")
    assert line["source"]["kind"] == "video" and line["source"]["fps"] == 24.0 and line["source"]["frames"] == 24
    assert line["qa"]["inputs"][1] == {"path": "take-1.mp4", "sha256": CORE.sha256_file(video)}
    assert_requested(line, "take_v1")


# --------------------------------------------------------------------------- B06-T5 fx profile and palette repair

def test_fx_profile_fades_edges(tmp_path):
    effect = np.zeros((48, 48, 4), np.uint8)
    yy, xx = np.mgrid[:48, :48]
    effect[(xx - 24) ** 2 + (yy - 24) ** 2 < 18 ** 2] = (255, 200, 80, 255)
    job_path, job = prepare(tmp_path, "fx", "--scale", "1.5", master=effect, action="fx")
    assert job["anchorMode"] == "canvas-center" and job["padding"] == [17, 17, 17, 17]
    assert job["referenceRoot"] == [160.0, 90.0]
    glow = np.zeros((180, 320, 4), np.uint8)
    glow[...] = (255, 200, 80, 255)  # the effect fills the whole video frame
    frames = write_frames(tmp_path / "fx-frames", [glow] * 9)
    document, out = register(tmp_path, job_path, frames, "fx-reg", "--profile", "fx")
    assert document["profile"] == "fx" and document["sourceSize"] == [82, 82]
    assert document["displayScale"] == pytest.approx(82 / 48)
    fade = document["fx"]["edgeFadePx"]
    middle = out[4][..., 3]
    assert middle[0].max() == 0 and middle[-1].max() == 0 and middle[:, 0].max() == 0 and middle[:, -1].max() == 0
    row = middle[41, :41].astype(int)
    assert (np.diff(row) >= 0).all() and row[int(np.ceil(fade)) + 1:].min() == 255
    assert middle[int(fade) + 2:-int(fade) - 2, int(fade) + 2:-int(fade) - 2].min() == 255
    assert out[0][..., 3].max() == 0 and out[-1][..., 3].max() == 0  # appearance and dissolve tail
    assert out[1][..., 3].max() < out[4][..., 3].max()
    assert {check["id"]: check["status"] for check in document["qa"]["checks"]}["video-edge-touch"] == "warn"
    assert_requested(document, "registration_v1")


def test_palette_repair_alpha_unchanged(tmp_path):
    master = make_master()
    master_path = tmp_path / "master.png"
    CORE.save_png(master, master_path)
    frames = []
    for index in range(3):
        frame = master.copy()
        boots = np.zeros(frame.shape[:2], bool)
        boots[62:68, 20:44] = (frame[62:68, 20:44, :3] == BOOT).all(-1)
        boots[62:68, 20 + index:44:3] = False  # keep some brown in every frame
        frame[boots, :3] = FLASH
        frame[30, 30, :3] = FLASH  # outside the region: must stay
        frames.append(frame)
    folder = write_frames(tmp_path / "frames", frames)
    out = tmp_path / "repaired"
    summary = RC.run(["palette-repair", "--frames", str(folder), "--master", str(master_path), "--region", "16,58,48,68",
                      "--hue", "70,170", "--palette-box", "16,62,48,68", "--output-dir", str(out)])
    report = json.loads((out / "palette-repair.json").read_text(encoding="utf-8"))
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
    assert_requested(report, "palette_repair_v1")
    assert report["palette"] == ["#603e20"] and report["alphaUnchanged"] is True
    assert summary["totalChangedPx"] == report["totalChangedPx"] > 0
    for before, record in zip(frames, report["frames"]):
        after = read_png(out / record["file"])
        assert np.array_equal(after[..., 3], before[..., 3])  # alpha unchanged
        assert not (after[58:68, 16:48, :3] == FLASH).all(-1).any()
        changed = (after != before).any(-1)
        assert changed.sum() == record["changedPx"] and not changed[:58].any()
        assert tuple(after[30, 30, :3]) == FLASH  # region-limited
    mask = read_png(out / "changed-mask.png")
    assert mask[..., 0].astype(bool).sum() > 0 and not mask[:58, ..., 0].any()
    with pytest.raises(RC.RegistrationQAError, match="narrow --hue"):
        RC.run(["palette-repair", "--frames", str(folder), "--master", str(master_path), "--region", "16,58,48,68",
                "--hue", "70,170", "--max-changed-fraction", "0.01", "--output-dir", str(tmp_path / "too-much")])
    assert not (tmp_path / "too-much").exists() and not stage_leftovers(tmp_path, "too-much")


# --------------------------------------------------------------------------- CLI conventions (plan Appendix D/E)

def test_cli_help_cp1252():
    assert_cli_help(SKILL, "prepare_i2v_input")
    assert_cli_help(SKILL, "register_clip")
    for name, verbs in (("prepare_i2v_input", ("prepare", "lint")),
                        ("register_clip", ("apply", "profile", "validate-profile", "qc", "palette-repair"))):
        for verb in verbs:
            result = run_cli([script_path(SKILL, name), verb, "--help"], "cp1252", timeout=120)
            assert result.returncode == 0 and result.stdout.isascii() and "usage:" in result.stdout, (name, verb)


def test_cli_refuses_existing_output(tmp_path):
    master = tmp_path / "master.png"
    CORE.save_png(make_master(), master)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    result = run_cli([script_path(SKILL, "prepare_i2v_input"), "prepare", "--master", master, "--action", "idle",
                      "--canvas", "320,180", "--output-dir", existing])
    assert result.returncode == 1 and result.stderr.startswith("error:") and "existing" in result.stderr.lower()
    job_path, _ = prepare(tmp_path, "job")
    frames = write_frames(tmp_path / "frames", [placed(job_path)])
    result = run_cli([script_path(SKILL, "register_clip"), "apply", "--job", job_path, "--frames", frames,
                      "--output-dir", existing])
    assert result.returncode == 1 and result.stderr.startswith("error:")
    assert sorted(path.name for path in existing.iterdir()) == ["keep.txt"]
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    register(tmp_path, job_path, frames, "reg")
    with pytest.raises(FileExistsError):
        RC.run(["profile", "--registration", str(tmp_path / "reg" / "registration.json"), "--id", "hero",
                "--output", str(profile)])
    with pytest.raises(FileExistsError):
        RC.run(["palette-repair", "--frames", str(frames), "--master", str(master), "--region", "0,0,8,8",
                "--hue", "70,170", "--output-dir", str(existing)])
    assert profile.read_text(encoding="utf-8") == "{}"


def test_cli_no_partial_output_on_qc_failure(tmp_path):
    master = tmp_path / "master.png"
    CORE.save_png(make_master(), master)
    result = run_cli([script_path(SKILL, "prepare_i2v_input"), "prepare", "--master", master, "--action", "idle",
                      "--canvas", "320,180", "--scale", "9", "--output-dir", tmp_path / "too-big"])
    assert result.returncode == 1 and "Placement QC failed" in result.stderr
    assert not (tmp_path / "too-big").exists() and not stage_leftovers(tmp_path, "too-big")
    job_path, _ = prepare(tmp_path, "job")
    job = RC.load_job(job_path)
    view = RC.load_master_view(job)
    stray = np.asarray(CORE.resample_rgba(view, job.scale, "lanczos", anchor_src=(0.0, 0.0),
                                          anchor_dst=(job.offset[0] - 60, job.offset[1]), out_size=job.canvas))
    frames = write_frames(tmp_path / "frames", [placed(job_path), stray])
    result = run_cli([script_path(SKILL, "register_clip"), "apply", "--job", job_path, "--frames", frames,
                      "--output-dir", tmp_path / "reg"])
    assert result.returncode == 1 and "canvas-overflow" in result.stderr and "--action-padding" in result.stderr
    assert not (tmp_path / "reg").exists() and not stage_leftovers(tmp_path, "reg")
    # the hint is exact: the suggested padding contains the stray frame
    hint = result.stderr.split("--action-padding ")[1].split(" ")[0]
    document, _ = register(tmp_path, job_path, frames, "reg-grown", "--action-padding", hint)
    assert {check["id"]: check["status"] for check in document["qa"]["checks"]}["canvas-overflow"] == "pass"
