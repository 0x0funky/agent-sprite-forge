"""engine_export 3.0 (B08): residue gate, animation.json 3.0, transport, verify and the CLI.

Fixtures are synthetic and deterministic. Tests that run ffmpeg carry the ``ffmpeg`` marker and
skip without a working ffmpeg; the opt-in ``bench`` test reads real frames from
FORGE_BENCH_CYCLE_FRAMES (report v2 P0-3: the forge-cycle frames must fail the residue gate).
"""
from __future__ import annotations

import argparse
import copy
import functools
import hashlib
import json
import os
import re
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, require_ffmpeg,
                             run_cli, script_path)

E = load_script("video2dsprite", "engine_export")
AV = load_script("video2dsprite", "forge_av")
SCRIPT = script_path("video2dsprite", "engine_export")
SKILL = "video2dsprite"

# animation.json 2.0 (cfed170 write_package): every key a 2.0 reader may use.
V2_KEYS = {"schemaVersion", "name", "sourceSize", "sourceAnchor", "inputFrameSize", "sourceRect", "sourceRectFormat",
           "encodedSize", "contentSize", "encodedAnchor", "registration", "fps", "frameCount", "durationSeconds",
           "loop", "reviewStatus", "hitEvents", "poster", "fallback", "qa", "inputFrames", "runtimeNotes"}
V2_QA_KEYS = {"status", "loopRequested", "frameCount", "durationSeconds", "blankFrames", "edgeTouchFrames",
              "boundsBottomSpanPx", "boundsCenterXSpanPx", "seamPremultipliedMAE", "adjacentMedianMAE",
              "seamToMedianRatio", "adjacentMaxMAE", "notes"}


# handoff/B08-video-packaging.md section 5: additions to video.schema.json, applied here in memory so the
# documents B08 writes are proven against exactly the diff integration will apply.
PROPOSED_ANIMATION_V3_PROPERTIES = {
    "fpsRational": {"description": "Exact transport rate of the video files, num/den; fps keeps the 2.0 number.",
                    "type": "string", "pattern": "^[1-9][0-9]*/[1-9][0-9]*$"},
    "fpsCapped": {"description": "The clip was reduced to at most 60 fps without changing its length.",
                  "type": "boolean"},
    "inputFps": {"$ref": "common.schema.json#/$defs/fpsValue"},
    "pingpongBaked": {"description": "A pingpong policy was baked into one cycle (0 1 2 3 2 1).", "type": "boolean"},
}
PROPOSED_VIDEO_DEFS = {
    "package_provenance_v1": {
        "description": "provenance.json beside animation.json (engine_export.py package): common provenance plus the "
                       "input digest a review verdict may bind.",
        "allOf": [{"$ref": "common.schema.json#/$defs/provenance"}],
        "type": "object",
        "required": ["schema", "inputDigest"],
        "properties": {
            "schema": {"const": "video2dsprite.provenance.v1"},
            "inputDigest": {"$ref": "common.schema.json#/$defs/sha256"},
            "inputDirectory": {"$ref": "common.schema.json#/$defs/relPath"},
        },
    },
    "verify_report_v1": {
        "description": "verify-qa.json (engine_export.py verify): a QA envelope over the encoded files of one package, "
                       "bound to its animation.json by sha256.",
        "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
        "type": "object",
        "required": ["schema", "manifestSha256", "transports"],
        "properties": {
            "schema": {"const": "video2dsprite.verify.v1"},
            "manifestSha256": {"$ref": "common.schema.json#/$defs/sha256"},
            "transports": {"type": "object"},
            "thresholds": {"type": "object"},
        },
    },
    "validation_report_v1": {
        "description": "validate_animation.py --report: a QA envelope with one check per rule and package.",
        "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
        "type": "object",
        "required": ["schema"],
        "properties": {"schema": {"const": "video2dsprite.validation.v1"}},
    },
}


def proposed_errors(instance, name: str) -> list[str]:
    """Errors against the vendored video schema with the section 5 additions applied in memory."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    folder = SKILLS_DIR / SKILL / "references" / "schemas"
    schemas = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.schema.json")}
    video = copy.deepcopy(schemas["video.schema.json"])
    video["$defs"]["animation_v3"]["properties"].update(PROPOSED_ANIMATION_V3_PROPERTIES)
    video["$defs"].update(PROPOSED_VIDEO_DEFS)
    schemas["video.schema.json"] = video
    registry = Registry().with_resources((s["$id"], DRAFT202012.create_resource(s)) for s in schemas.values())
    validator = Draft202012Validator({"$ref": f"{video['$id']}#/$defs/{name}"}, registry=registry)
    return [f"{error.json_path}: {error.message}" for error in validator.iter_errors(instance)]


def ffmpeg_test(test):
    """Mark ``test`` @ffmpeg and skip it unless ffmpeg really encodes VP9 alpha and libx264."""
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        require_ffmpeg()
        caps = E.capabilities()
        if not (caps["webm"] and caps["packed"]):
            pytest.skip(f"ffmpeg lacks a working VP9-alpha or libx264 path: {caps['errors']}")
        return test(*args, **kwargs)
    return pytest.mark.ffmpeg(wrapper)


# --------------------------------------------------------------------------- synthetic frames

def body(size=(48, 48), box=(14, 10, 34, 40), dx=0, colour=(40, 160, 210), outline=(20, 30, 40),
         pocket=False, fringe=False) -> np.ndarray:
    """An outlined opaque body on transparency, shifted by ``dx``; optional magenta pocket or key fringe."""
    w, h = size
    frame = np.zeros((h, w, 4), np.uint8)
    x0, y0, x1, y1 = box[0] + dx, box[1], box[2] + dx, box[3]
    frame[y0:y1, x0:x1] = (*outline, 255)
    frame[y0 + 1:y1 - 1, x0 + 1:x1 - 1] = (*colour, 255)
    if pocket:
        frame[y0 + 8:y0 + 14, x0 + 6:x0 + 12] = (255, 0, 255, 255)
    if fringe:
        ring = np.zeros((h, w), bool)
        ring[y0 - 1:y1 + 1, x0 - 1:x1 + 1] = True
        ring[y0:y1, x0:x1] = False
        frame[ring] = (210, 70, 200, 255)
    return frame


def disc(k: int, n: int, size: int = 64, orbit: float = 0.2) -> np.ndarray:
    """Antialiased disc circling the centre (orbit radius ``orbit * size``): frame ``k`` of a seamless loop."""
    yy, xx = np.mgrid[0:size, 0:size] + 0.5
    cx = size / 2 + size * orbit * np.cos(2 * np.pi * k / n)
    cy = size / 2 + size * orbit * np.sin(2 * np.pi * k / n)
    alpha = np.clip(size * 0.18 - np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) + 0.5, 0, 1)
    frame = np.zeros((size, size, 4), np.uint8)
    frame[..., 0], frame[..., 1], frame[..., 2] = 200, (120 + xx * 100 / size).astype(np.uint8), 60
    frame[..., 3] = (alpha * 255 + 0.5).astype(np.uint8)
    frame[frame[..., 3] == 0] = 0
    return frame


def write_frames(folder: Path, frames) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, frame in enumerate(frames):
        path = folder / f"clean_{i:04d}.png"
        Image.fromarray(frame).save(path)
        paths.append(path)
    return paths


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def options(clean: Path, out: Path, **kw) -> argparse.Namespace:
    values = dict(clean_dir=str(clean), out_dir=str(out), name="idle", fps="12", formats="png")
    values.update(kw)
    return argparse.Namespace(**values)


def walk_frames(count=6, **kw):
    return [body(dx=d, **kw) for d in (0, 1, 2, 3, 2, 1)[:count]]


def selection_v2(path: Path, frames: list[Path], start: int, end: int, indices, durations, **extra) -> Path:
    data = {"schema": "forge-frame-selection/v2", "sourceDirectory": "frames-clean",
            "sourceHashes": [sha(p) for p in frames[start:end]], "start": start, "endExclusive": end,
            "sourceIndices": list(indices), "durations_ms": list(durations), "loopPolicy": "cycle", "events": [],
            "status": "selected-needs-visual-review", "method": "test selection"}
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- B08-T1 key-residue gate

def test_synthetic_pocket_fails(tmp_path):
    """report v2 P0-3: an opaque key pocket refuses the package and publishes nothing."""
    clean = tmp_path / "clean"
    write_frames(clean, [body(dx=d, pocket=d == 2) for d in range(4)])
    with pytest.raises(E.QualityGateError, match="key residue: 36 opaque key px"):
        E.package(options(clean, tmp_path / "out"))
    assert not (tmp_path / "out").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["clean"]


def test_key_fringe_ring_fails(tmp_path):
    """Key-tinted spill on more than 1% of the outer ring fails even without opaque key pixels."""
    clean = tmp_path / "clean"
    write_frames(clean, [body(dx=d, fringe=True) for d in range(4)])
    with pytest.raises(E.QualityGateError, match=r"outer-ring spill 100\.0% \(limit 1%\)"):
        E.package(options(clean, tmp_path / "out"))


def test_clean_frames_pass(tmp_path):
    clean = tmp_path / "clean"
    write_frames(clean, walk_frames(4))
    manifest = E.package(options(clean, tmp_path / "out"))
    checks = {c["id"]: c for c in manifest["qa"]["checks"]}
    assert checks["opaque_key_px"] == {"id": "opaque_key_px", "value": 0, "threshold": 0, "status": "pass"}
    assert checks["outer_ring_spill_fraction"]["status"] == "pass"
    assert checks["semitransparent_fraction"]["value"] == 0.0
    residue = manifest["qa"]["keyResidue"]
    assert residue["key"] == [255, 0, 255] and residue["framesMeasured"] == 4
    assert manifest["qa"]["keySource"] == "default"


def test_allow_key_residue_is_recorded(tmp_path):
    clean = tmp_path / "clean"
    write_frames(clean, [body(dx=d, pocket=True) for d in range(3)])
    manifest = E.package(options(clean, tmp_path / "out", allow_key_residue=True))
    checks = {c["id"]: c for c in manifest["qa"]["checks"]}
    assert checks["opaque_key_px"]["status"] == "warn" and checks["opaque_key_px"]["value"] == 108
    assert manifest["qa"]["status"] == "warn" and manifest["qa"]["allowKeyResidue"] is True
    assert read(tmp_path / "out" / "provenance.json")["params"]["allowKeyResidue"] is True


def test_key_none_and_pipeline_meta_key(tmp_path):
    """--key none skips the gate (native alpha); auto reads the matte key of pipeline-meta.json."""
    clean = tmp_path / "clean"
    write_frames(clean, [body(dx=d, pocket=True) for d in range(3)])
    manifest = E.package(options(clean, tmp_path / "none", key="none"))
    assert {c["id"]: c["status"] for c in manifest["qa"]["checks"]}["opaque_key_px"] == "skipped"
    meta = tmp_path / "pipeline-meta.json"
    meta.write_text(json.dumps({"matte": {"mode": "soft", "key": [0.4, 254.6, 0.0]}}), encoding="utf-8")
    manifest = E.package(options(clean, tmp_path / "green", pipeline_meta=str(meta)))
    assert manifest["qa"]["keyResidue"]["key"] == [0, 255, 0] and manifest["qa"]["keySource"] == "pipeline-meta"
    green = tmp_path / "green-clean"
    frames = [body(dx=d) for d in range(3)]
    frames[1][20:26, 20:26] = (0, 255, 0, 255)
    write_frames(green, frames)
    with pytest.raises(E.QualityGateError, match="36 opaque key px"):
        E.package(options(green, tmp_path / "green-out", pipeline_meta=str(meta)))


def test_residue_crop_is_exact():
    """matte_qa on the alpha > 0 crop equals matte_qa on the full frame (edge-touching, blank, noisy)."""
    rng = np.random.default_rng(7)
    frames = [np.zeros((9, 9, 4), np.uint8), body(fringe=True), body(pocket=True)]
    for _ in range(12):
        frame = np.zeros((40, 50, 4), np.uint8)
        y0, x0 = rng.integers(0, 10), rng.integers(0, 15)
        frame[y0:rng.integers(20, 41), x0:rng.integers(30, 51)] = (*rng.integers(0, 256, 3), rng.integers(1, 256))
        frame[rng.integers(0, 40, 30), rng.integers(0, 50, 30)] = (255, 0, 255, 255)
        frames.append(frame)
    keys = ("opaque_key_px", "outer_ring_spill_fraction", "semitransparent_fraction", "enclosed_key_pockets",
            "key_hued_px", "visible_px", "outer_ring_px")
    for frame in frames:
        full = E.forge_matte.matte_qa(frame, (255, 0, 255))
        crop = E.forge_matte.matte_qa(E._visible_crop(frame), (255, 0, 255))
        assert {k: full[k] for k in keys} == {k: crop[k] for k in keys}


def test_analyze_reports_residue_metrics():
    frames = [Image.fromarray(body(dx=d, pocket=d == 1)) for d in range(3)]
    report = E.analyze(frames, 12, True)
    residue = report["keyResidue"]
    assert residue["opaqueKeyPx"] == 36 and residue["framesWithOpaqueKey"] == [1]
    assert residue["enclosedKeyPockets"] == 1 and residue["maxSemitransparentFraction"] == 0.0
    assert report["status"] == "needs-visual-review" and "pass" not in report
    assert "keyResidue" not in E.analyze(frames, 12, True, key=None)


@pytest.mark.bench
def test_forge_cycle_frames_fail(tmp_path):
    """Opt-in (report v2 P0-3 acceptance): the shipped forge-cycle frames fail the residue gate."""
    folder = os.environ.get("FORGE_BENCH_CYCLE_FRAMES")
    if not folder or not Path(folder).is_dir():
        pytest.skip("set FORGE_BENCH_CYCLE_FRAMES to the forge-cycle frames-clean folder")
    with pytest.raises(E.QualityGateError, match="key residue") as caught:
        E.package(options(Path(folder), tmp_path / "out", fps="24", loop=True))
    spill = float(re.search(r"outer-ring spill ([0-9.]+)%", str(caught.value)).group(1))
    assert spill > 50
    assert not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- B08-T2 animation.json 3.0

def _documents(tmp_path: Path, frames: list[Path]) -> dict:
    """A v2 selection (with a held frame), a registration job and an accepted review elsewhere on disk."""
    docs = tmp_path / "work" / "docs"
    docs.mkdir(parents=True)
    selection = selection_v2(docs / "selection.json", frames, 1, 5, [1, 2, 2, 3, 4], [83, 83, 84, 83, 84],
                             loopPolicy="oneshot", impactMs=170, holdMs=300, cadenceMs=452.05,
                             strideWorldUnits=1.65, events=[{"name": "step_l", "atMs": 0},
                                                            {"name": "hit", "atMs": 170, "data": {"damage": 2}}])
    job = docs / "registration.json"
    job.write_text(json.dumps({
        "schema": "video2dsprite.registration_job.v1",
        "master": {"path": "master/hero.png", "sha256": "0" * 64, "size": [40, 40], "anchor": [20, 36]},
        "referenceCanvas": [1280, 720], "referenceScale": 1.0, "referenceOffset": [0, 0], "keyColor": "magenta",
        "workRegion": [0, 0, 1280, 720], "action": "attack", "padding": [4, 6, 4, 2], "prompt": {"sha256": "1" * 64}}),
        encoding="utf-8")
    review = docs / "review.json"
    review.write_text(json.dumps({"schema": "video2dsprite.review_verdict.v1", "status": "accepted", "issues": [],
                                  "evidence": ["contact.png"], "reviewedSha256": sha(selection)}), encoding="utf-8")
    return {"selection": selection, "registration": job, "review": review}


def test_manifest_validates_as_animation_v3(tmp_path):
    clean = tmp_path / "work" / "frames-clean"
    frames = write_frames(clean, [body(dx=d) for d in range(6)])
    docs = _documents(tmp_path, frames)
    manifest = E.package(options(clean, tmp_path / "game" / "attack", name="attack", fps=None,
                                 selection=str(docs["selection"]), registration=str(docs["registration"]),
                                 review=str(docs["review"]), budget_class="actor", body_height_px=30.0,
                                 shadow="9,3,0.3", display_scale=0.5, speed_ref=3.65))
    out = tmp_path / "game" / "attack"
    on_disk = read(out / "animation.json")
    assert on_disk == manifest
    assert_valid_contract(on_disk, "video", "animation_v3", skill=SKILL)
    assert_valid_contract(read(out / "animation-qa.json"), "common", "qaEnvelope", skill=SKILL)
    assert_valid_contract(read(out / "provenance.json"), "common", "provenance", skill=SKILL)
    assert proposed_errors(on_disk, "animation_v3") == []
    assert proposed_errors(read(out / "provenance.json"), "package_provenance_v1") == []
    assert len(proposed_errors({**on_disk, "fpsRational": "12"}, "animation_v3")) == 1
    assert read(out / "animation-qa.json") == manifest["qa"]
    # selection timing, events and gameplay fields
    assert manifest["sourceIndices"] == [1, 2, 2, 3, 4] and manifest["durationsMs"] == [83, 83, 84, 83, 84]
    assert manifest["frameCount"] == 5 and manifest["fpsRational"] == "5000/417" and manifest["durationSeconds"] == 0.417
    assert manifest["loop"] is False and manifest["loopPolicy"] == "oneshot"
    assert manifest["impactMs"] == 170 and manifest["holdMs"] == 300
    assert manifest["events"] == [{"name": "step_l", "atMs": 0, "frame": 0},
                                  {"name": "hit", "atMs": 170, "data": {"damage": 2}, "frame": 2}]
    assert manifest["hitEvents"] == [manifest["events"][1]]
    assert manifest["cadenceMs"] == 452.05 and manifest["strideWorldUnits"] == 1.65 and manifest["speedRef"] == 3.65
    assert (manifest["sampling"], manifest["pixelArt"], manifest["bodyHeightPx"], manifest["displayScale"]) == \
        ("linear", False, 30.0, 0.5)
    assert manifest["shadow"] == {"rx": 9.0, "ry": 3.0, "opacity": 0.3} and manifest["budgetClass"] == "actor"
    # registration by construction: the 40x40 master plus padding [4, 6, 4, 2] is the 48x48 canvas
    assert manifest["registration"] == {"mode": "construction", "jobSha256": sha(docs["registration"]),
                                        "baseSize": [40, 40], "baseAnchor": [20.0, 36.0], "padding": [4, 6, 4, 2],
                                        "sourceSize": [48, 48], "sourceAnchor": [24.0, 42.0], "action": "attack",
                                        "legacyMode": "preserved-source-canvas"}
    assert manifest["sourceSize"] == [48, 48] and manifest["sourceAnchor"] == [24.0, 42.0]
    # the accepted review is bound to the selection file; QA passes
    assert manifest["reviewStatus"] == "accepted" and manifest["qa"]["status"] == "pass"
    provenance = read(out / "provenance.json")
    assert provenance["review"]["boundTo"] == "selection"
    assert manifest["provenanceFile"] == "provenance.json" and manifest["artSource"] == "video"
    assert {r["role"] for r in provenance["inputs"]} == {"frame", "selection", "registration", "review"}
    assert provenance["inputDirectory"] == "../../work/frames-clean"


def test_v2_keys_preserved(tmp_path):
    clean = tmp_path / "clean"
    write_frames(clean, walk_frames(6))
    manifest = E.package(options(clean, tmp_path / "out", loop=True))
    assert V2_KEYS <= set(manifest)
    assert V2_QA_KEYS <= set(manifest["qa"])
    assert set(manifest["poster"]) == {"file", "bytes", "sha256"}
    assert manifest["fallback"]["type"] == "png-atlas"
    assert set(manifest["fallback"]["pages"][0]) == {"file", "bytes", "sha256", "firstFrame", "frameCount", "columns"}
    assert set(manifest["inputFrames"][0]) == {"file", "bytes", "sha256"}
    assert isinstance(manifest["fps"], float) and manifest["fps"] == 12.0
    assert manifest["durationSeconds"] == 0.5 and manifest["loop"] is True and manifest["hitEvents"] == []
    assert manifest["registration"]["legacyMode"] == "preserved-source-canvas"
    assert manifest["runtimeNotes"][:4] == ["Game logic owns hit timings, damage and root motion.",
                                            "Packed MP4 has no native alpha; reconstruct RGB and alpha before displaying.",
                                            "Keep sourceSize/sourceAnchor for world geometry when selecting smaller media.",
                                            "Use the PNG poster until a decoded first frame exists; preload only "
                                            "current scene actions."]


_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|file:)")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_no_absolute_paths_in_runtime_manifest(tmp_path):
    clean = tmp_path / "work" / "frames-clean"
    frames = write_frames(clean, [body(dx=d) for d in range(6)])
    docs = _documents(tmp_path, frames)
    v1 = docs["selection"].with_name("selection-v1.json")
    v1.write_text(json.dumps({"schema": "forge-frame-selection/v1", "sourceDirectory": str(clean.resolve()),
                              "start": 0, "endExclusive": 6, "fps": 12,
                              "sourceHashes": [sha(p) for p in frames], "status": "selected-needs-visual-review"}),
                  encoding="utf-8")
    meta = tmp_path / "work" / "pipeline-meta.json"
    meta.write_text(json.dumps({"video": str(tmp_path.resolve()), "matte": {"mode": "soft", "key": "magenta"}}),
                    encoding="utf-8")
    E.package(options(clean, tmp_path / "pkg", fps=None, selection=str(v1), registration=str(docs["registration"]),
                      pipeline_meta=str(meta)))
    roots = {str(tmp_path.resolve()), tmp_path.resolve().as_posix(), str(tmp_path)}
    for name in ("animation.json", "animation-qa.json", "provenance.json"):
        for text in _strings(read(tmp_path / "pkg" / name)):
            assert not _ABSOLUTE.match(text), (name, text)
            assert not any(root in text for root in roots), (name, text)


def test_stale_selection_refused(tmp_path):
    clean = tmp_path / "clean"
    frames = write_frames(clean, [body(dx=d) for d in range(4)])
    selection = selection_v2(tmp_path / "sel.json", frames, 0, 4, [0, 1, 2, 3], [83, 83, 84, 83])
    Image.fromarray(body(dx=3, colour=(90, 200, 90))).save(frames[2])
    with pytest.raises(ValueError, match="stale selection.*frame 2"):
        E.package(options(clean, tmp_path / "out", fps=None, selection=str(selection)))
    assert not (tmp_path / "out").exists()


def test_selection_v1_and_conflicts(tmp_path):
    clean = tmp_path / "clean"
    frames = write_frames(clean, [body(dx=d) for d in range(5)])
    v1 = tmp_path / "v1.json"
    v1.write_text(json.dumps({"schema": "forge-frame-selection/v1", "sourceDirectory": "clean", "start": 1,
                              "endExclusive": 4, "fps": 24, "sourceHashes": [sha(p) for p in frames[1:4]],
                              "status": "selected-needs-visual-review"}), encoding="utf-8")
    manifest = E.package(options(clean, tmp_path / "out", fps=None, selection=str(v1), loop=True))
    assert manifest["sourceIndices"] == [1, 2, 3] and manifest["fpsRational"] == "24/1"
    assert manifest["durationsMs"] == [42, 41, 42] and manifest["loopPolicy"] == "cycle"
    with pytest.raises(ValueError, match="conflicts with the selection fps"):
        E.package(options(clean, tmp_path / "out2", fps="12", selection=str(v1)))
    v2 = selection_v2(tmp_path / "v2.json", frames, 0, 4, [0, 1, 2, 3], [83, 83, 84, 83], loopPolicy="oneshot")
    with pytest.raises(ValueError, match="--loop conflicts with the oneshot"):
        E.package(options(clean, tmp_path / "out3", fps=None, selection=str(v2), loop=True))
    rejected = selection_v2(tmp_path / "v3.json", frames, 0, 4, [0, 1, 2, 3], [83, 83, 84, 83], status="rejected")
    with pytest.raises(E.QualityGateError, match="status is 'rejected'"):
        E.package(options(clean, tmp_path / "out4", fps=None, selection=str(rejected)))
    late = selection_v2(tmp_path / "v4.json", frames, 0, 4, [0, 1, 2, 3], [83, 83, 84, 83],
                        events=[{"name": "hit", "atMs": 400}])
    with pytest.raises(ValueError, match="hit at 400 ms lies outside the 333 ms clip"):
        E.package(options(clean, tmp_path / "out5", fps=None, selection=str(late)))


def test_pingpong_is_baked_into_one_cycle(tmp_path):
    clean = tmp_path / "clean"
    write_frames(clean, [body(dx=d) for d in range(4)])
    manifest = E.package(options(clean, tmp_path / "out", loop_policy="pingpong"))
    assert manifest["sourceIndices"] == [0, 1, 2, 3, 2, 1] and manifest["loopPolicy"] == "cycle"
    assert manifest["pingpongBaked"] is True and manifest["loop"] is True and manifest["frameCount"] == 6


def test_review_verdict_binding(tmp_path):
    clean = tmp_path / "clean"
    frames = write_frames(clean, [body(dx=d) for d in range(3)])
    digest = E.frames_digest([sha(p) for p in frames])
    verdict = {"schema": "video2dsprite.review_verdict.v1", "status": "accept_with_mask",
               "issues": [{"frame": 1, "box": [10, 10, 20, 20], "desc": "faint key glow on the hand"}],
               "evidence": [], "reviewedSha256": digest}
    review = tmp_path / "review.json"
    review.write_text(json.dumps(verdict), encoding="utf-8")
    manifest = E.package(options(clean, tmp_path / "ok", review=str(review)))
    assert manifest["reviewStatus"] == "accept_with_mask" and manifest["qa"]["status"] == "warn"
    review.write_text(json.dumps({**verdict, "reviewedSha256": "f" * 64}), encoding="utf-8")
    with pytest.raises(ValueError, match="stale review"):
        E.package(options(clean, tmp_path / "stale", review=str(review)))
    review.write_text(json.dumps({**verdict, "status": "fail_regenerate"}), encoding="utf-8")
    with pytest.raises(E.QualityGateError, match="fail_regenerate"):
        E.package(options(clean, tmp_path / "fail", review=str(review)))
    assert not any((tmp_path / name).exists() for name in ("stale", "fail"))


def test_registration_conflict_refused(tmp_path):
    clean = tmp_path / "clean"
    frames = write_frames(clean, [body(dx=d) for d in range(3)])
    job = _documents(tmp_path, frames)["registration"]
    with pytest.raises(ValueError, match="--source-anchor .* conflicts with the registration"):
        E.package(options(clean, tmp_path / "out", registration=str(job), source_anchor="24,40"))
    record = tmp_path / "registration-record.json"
    record.write_text(json.dumps({"mode": "fixed-envelope", "jobSha256": sha(job)}), encoding="utf-8")
    manifest = E.package(options(clean, tmp_path / "ok", registration=str(record)))
    assert manifest["registration"]["mode"] == "fixed-envelope"
    assert manifest["registration"]["registrationSha256"] == sha(record)


def test_timeline_helpers():
    timeline = E.constant_timeline(range(4), Fraction(12))
    assert timeline.durations == (83, 84, 83, 83) and timeline.seconds == Fraction(1, 3)
    assert E.duration_timeline([0, 1, 2, 3], [83, 84, 83, 83], Fraction(12)).rate == 12
    assert E.duration_timeline([0, 1, 2, 3], [83, 83, 84, 83], Fraction(12)).rate == Fraction(4000, 333)
    assert E.duration_timeline([0, 1, 2, 3], [100, 100, 100, 100]).rate == 10
    capped, positions = E.cap_timeline(E.constant_timeline(range(60), Fraction(125)), E.MAX_FPS)
    assert capped.seconds == Fraction(12, 25) and capped.rate <= 60 and len(positions) == 28
    assert positions[:4] == [0, 2, 4, 6] and sum(capped.durations) == 480
    assert E.bake_pingpong(E.constant_timeline([5, 6, 7], Fraction(10))).indices == (5, 6, 7, 6)
    assert E.parse_tiers("actor,custom:256@20") == (E.TierSpec("actor", 320, Fraction(24)),
                                                    E.TierSpec("custom", 256, Fraction(20)))
    with pytest.raises(ValueError, match="at most 60"):
        E.parse_tiers("fx:512@61")


def test_fps_cap_keeps_the_clip_length(tmp_path):
    clean = tmp_path / "clean"
    write_frames(clean, [body(dx=d % 4) for d in range(30)])
    manifest = E.package(options(clean, tmp_path / "out", fps="125"))
    assert manifest["fpsCapped"] is True and manifest["inputFps"] == "125/1"
    assert manifest["durationSeconds"] == 0.24 and sum(manifest["durationsMs"]) == 240
    assert manifest["frameCount"] == 14 and manifest["fps"] <= 60


def test_uneven_durations_need_png_only(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "capabilities", lambda: {"webm": True, "packed": True})
    clean = tmp_path / "clean"
    frames = write_frames(clean, [body(dx=d) for d in range(3)])
    selection = selection_v2(tmp_path / "sel.json", frames, 0, 3, [0, 1, 2], [40, 200, 40])
    manifest = E.package(options(clean, tmp_path / "png", fps=None, selection=str(selection)))
    assert manifest["durationsMs"] == [40, 200, 40] and manifest["durationSeconds"] == 0.28
    with pytest.raises(ValueError, match="constant frame rate"):
        E.package(options(clean, tmp_path / "video", fps=None, selection=str(selection), formats="png,packed"))


# --------------------------------------------------------------------------- B08-T3 transport

class FakeEncoders:
    """Stand-ins for the forge_av encoders that record their frames (no ffmpeg needed)."""

    def __init__(self, monkeypatch):
        self.calls, self.counts = [], {}
        monkeypatch.setattr(E, "capabilities", lambda: {"webm": True, "packed": True})
        monkeypatch.setattr(E, "_decoded_frame_count", lambda path, alpha: self.counts[Path(path).name])
        monkeypatch.setattr(E.forge_av, "ffmpeg_info", lambda: {"version": "test"})
        monkeypatch.setattr(E.forge_av, "encode_packed_alpha", self.packed)
        monkeypatch.setattr(E.forge_av, "encode_vp9_alpha", self.webm)

    def _write(self, kind, frames, out, fps, crf, keyint, extra):
        frames = [np.asarray(f) for f in frames]
        self.calls.append((kind, Path(out).name, frames, fps))
        Path(out).write_bytes(kind.encode())
        self.counts[Path(out).name] = len(frames)
        return {"file": Path(out).name, "bytes": len(kind), "sha256": hashlib.sha256(kind.encode()).hexdigest(),
                "crf": crf, "keyint": keyint, "keyframes": [0], "frameCount": len(frames), "fps": fps,
                "durationMs": float(len(frames) / Fraction(fps) * 1000), **extra}

    def packed(self, frames, out, fps, crf=18, *, keyint=None, max_fps=60, timeout=None):
        frames = list(frames)
        geometry = AV.packed_geometry(frames[0].shape[1], frames[0].shape[0])
        return self._write("packed", frames, out, fps, crf, keyint,
                           {"mimeType": "video/mp4", "codec": "h264", "profile": "main", **geometry,
                            "requiresCompositor": True})

    def webm(self, frames, out, fps, crf=28, *, keyint=None, max_fps=60, timeout=None):
        frames = list(frames)
        return self._write("webm", frames, out, fps, crf, keyint,
                           {"mimeType": "video/webm", "codec": "vp9", "alpha": "native-vp9", "pixFmt": "yuva420p",
                            "width": frames[0].shape[1], "height": frames[0].shape[0]})


def test_half_size_even_padding(tmp_path, monkeypatch):
    """Odd content: packed halves pad right/bottom to even; the encoder gets the unpadded content."""
    fake = FakeEncoders(monkeypatch)
    clean = tmp_path / "clean"
    write_frames(clean, [body(size=(15, 17), box=(2, 2, 12, 15), dx=d % 2) for d in range(4)])
    manifest = E.package(options(clean, tmp_path / "out", formats="png,webm,packed", loop=True))
    assert manifest["contentSize"] == [15, 17] and manifest["encodedSize"] == [16, 18]
    packed = manifest["packedAlpha"]
    assert (packed["width"], packed["height"], packed["halfWidth"], packed["halfHeight"]) == (15, 17, 16, 18)
    assert packed["encodedSize"] == [32, 18] and packed["fps"] == "12/1" and packed["keyint"] == 4
    sizes = {name: frames[0].shape[:2] for _, name, frames, _ in fake.calls}
    assert sizes == {"idle.webm": (18, 16), "idle-packed.mp4": (17, 15)}
    atlas = np.asarray(Image.open(tmp_path / "out" / "idle-atlas-00.png"))
    assert not atlas[17, :, 3].any() and not atlas[:18, 15, 3].any()
    tier = E.plan_tier(E.TierSpec("tiny", 9, Fraction(12)), manifest, E.constant_timeline(range(4), Fraction(12)),
                       "idle")
    assert (tier["width"], tier["height"], tier["halfWidth"], tier["halfHeight"]) == (8, 9, 8, 10)
    assert tier["encodedSize"] == [16, 10]
    assert_valid_contract(manifest, "video", "animation_v3", skill=SKILL)


def test_tier_geometry_in_source_coords(tmp_path, monkeypatch):
    """Tiers shrink the media only: the anchor maps to the same source point at every tier size."""
    fake = FakeEncoders(monkeypatch)
    clean = tmp_path / "clean"
    frames = []
    for d in range(4):
        frame = np.zeros((120, 160, 4), np.uint8)
        frame[30:90, 40 + d:100 + d] = (200, 160, 60, 255)
        frames.append(frame)
    write_frames(clean, frames)
    manifest = E.package(options(clean, tmp_path / "out", formats="png,packed", tiers="actor:32@12,prop:40@6",
                                 source_size="640,480", source_anchor="280,356", crop_union=True, max_side=48,
                                 loop=True))
    # union crop (40, 30)-(103, 90) of the 160x120 input is 63x60 px, i.e. 252x240 source units
    assert manifest["sourceSize"] == [640, 480] and manifest["sourceAnchor"] == [280.0, 356.0]
    assert manifest["sourceRect"] == [160.0, 120.0, 252.0, 240.0] and manifest["contentSize"] == [48, 46]
    rx, ry, rw, rh = manifest["sourceRect"]
    for tier in manifest["mobilePackedAlpha"]:
        ax, ay = tier["encodedAnchor"]
        assert rx + ax * rw / tier["width"] == pytest.approx(280.0)
        assert ry + ay * rh / tier["height"] == pytest.approx(356.0)
        assert [tier["sourceWidth"], tier["sourceHeight"]] == manifest["contentSize"]
        assert max(tier["width"], tier["height"]) <= tier["maxLongEdge"]
    actor, prop = manifest["mobilePackedAlpha"]
    assert (actor["width"], actor["height"], actor["fps"], actor["frameCount"]) == (32, 31, "12/1", 4)
    assert (prop["width"], prop["height"], prop["fps"], prop["frameCount"]) == (40, 38, "6/1", 2)
    assert prop["inputIndices"] == [0, 2]
    assert prop["durationMs"] == pytest.approx(manifest["durationSeconds"] * 1000)
    # the body (input x 40..100, centre 70 -> source x 280) lands where the source rect maps it
    actor_frames = next(frames for _, name, frames, _ in fake.calls if name == "idle-packed-actor.mp4")
    xs = np.nonzero((actor_frames[0][..., 3] > 127).any(axis=0))[0]
    body_x = (xs.min() + xs.max() + 1) / 2
    assert rx + body_x * rw / actor["width"] == pytest.approx(280.0, abs=rw / actor["width"])


def test_run_decodes_utf8():
    text = E.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write('caf\\u00e9 -> \\u2192'.encode())"])
    assert text == "café -> →"


def test_capabilities_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(E.shutil, "which", lambda name: None)
    caps = E.capabilities()
    assert (caps["webm"], caps["packed"], caps["png"]) == (False, False, True)


@ffmpeg_test
def test_duration_exact(tmp_path):
    """Rational transport rates: the files last exactly the manifest timeline, tiers included."""
    clean = tmp_path / "clean"
    write_frames(clean, [disc(k, 15) for k in range(15)])
    manifest = E.package(options(clean, tmp_path / "out", fps="24", loop=True, formats="png,webm,packed",
                                 tiers="prop:48@12"))
    out = tmp_path / "out"
    assert manifest["durationSeconds"] == 0.625 and sum(manifest["durationsMs"]) == 625
    assert AV.duration_ms(out / "idle-packed.mp4") == 625.0
    assert abs(AV.duration_ms(out / "idle.webm") - 625.0) <= 1.0
    prop = manifest["mobilePackedAlpha"][0]
    assert (prop["fps"], prop["frameCount"]) == ("56/5", 7)
    assert AV.duration_ms(out / prop["file"]) == 625.0
    assert manifest["packedAlpha"]["keyframes"] == [0] and manifest["webm"]["keyframes"] == [0]
    assert manifest["packedAlpha"]["keyint"] == 15 and prop["keyint"] == 7
    short = tmp_path / "short"
    write_frames(short, [disc(k, 4) for k in range(4)])
    manifest = E.package(options(short, tmp_path / "short-out", fps="12", formats="png,packed"))
    assert AV.duration_ms(tmp_path / "short-out" / "idle-packed.mp4") == pytest.approx(1000 / 3, abs=1e-3)
    assert manifest["packedAlpha"]["keyint"] is None


@ffmpeg_test
def test_v2_media_keys_preserved(tmp_path):
    clean = tmp_path / "clean"
    write_frames(clean, [disc(k, 6) for k in range(6)])
    manifest = E.package(options(clean, tmp_path / "out", loop=True, formats="png,webm,packed"))
    assert {"file", "bytes", "sha256", "mimeType", "alpha", "requiresAlphaPlaybackVerification",
            "fullDecodePassed"} <= set(manifest["webm"])
    assert {"file", "bytes", "sha256", "mimeType", "layout", "width", "height", "encodedSize", "requiresCompositor",
            "fullDecodePassed"} <= set(manifest["packedAlpha"])
    assert_valid_contract(manifest, "video", "animation_v3", skill=SKILL)


# --------------------------------------------------------------------------- B08-T4 verify

def _loop_reference(n=12, size=64, orbit=0.2):
    return [disc(k, n, size, orbit) for k in range(n)]


def _failed(result):
    return {c["id"].split(".", 1)[1] for c in result["checks"] if c["status"] == "fail"}


def test_verify_accepts_a_faithful_decode():
    reference = _loop_reference()
    result = E.evaluate_transport(reference, [f.copy() for f in reference], loop=True, label="packedAlpha")
    assert _failed(result) == set()
    assert result["metrics"]["seam"]["decodedSeamOverP95"] == result["metrics"]["seam"]["referenceSeamOverP95"]


def test_verify_catches_black_background_alpha():
    """A decoder that drops the alpha plane shows the clip on an opaque background."""
    reference = _loop_reference()
    decoded = [np.dstack([f[..., :3], np.full(f.shape[:2], 255, np.uint8)]) for f in reference]
    failed = _failed(E.evaluate_transport(reference, decoded, loop=True, label="webm"))
    assert {"transparent_median", "alpha_mae"} <= failed


def test_verify_catches_static_alpha():
    """Alpha frozen on the first frame while the reference moves."""
    reference = _loop_reference()
    decoded = [np.dstack([f[..., :3], reference[0][..., 3]]) for f in reference]
    result = E.evaluate_transport(reference, decoded, loop=True, label="packedAlpha")
    assert "dynamic_alpha" in _failed(result)
    dynamic = next(c for c in result["checks"] if c["id"].endswith("dynamic_alpha"))
    assert dynamic["value"]["decoded"] == 0.0 and dynamic["value"]["source"] > 0.1


def test_verify_catches_single_gop_seam_pop():
    """hd2d seam findings: codec drift through one long GOP pops at the wrap of a subtle ambient loop
    although every frame passes on its own (RGB error 11 <= 12)."""
    reference = _loop_reference(orbit=0.02)
    decoded = []
    for k, frame in enumerate(reference):
        drifted = frame.astype(np.int16)
        drifted[..., :3][frame[..., 3] > 0] += k
        decoded.append(np.clip(drifted, 0, 255).astype(np.uint8))
    result = E.evaluate_transport(reference, decoded, loop=True, label="packedAlpha")
    assert _failed(result) == {"decoded_seam"}
    seam = result["metrics"]["seam"]
    assert seam["decodedSeamOverP95"] > 1.3 and seam["referenceSeamOverP95"] < 1.1
    assert _failed(E.evaluate_transport(reference, decoded, loop=False, label="packedAlpha")) == set()


def test_verify_reports_frame_count_and_size():
    reference = _loop_reference(4)
    assert _failed(E.evaluate_transport(reference, reference[:3], loop=False)) == {"frame_count"}
    small = [f[:32, :32] for f in reference]
    assert _failed(E.evaluate_transport(reference, small, loop=False)) == {"frame_size"}


@ffmpeg_test
def test_verify_passes_good_package(tmp_path):
    clean = tmp_path / "clean"
    write_frames(clean, _loop_reference(12, 96))
    E.package(options(clean, tmp_path / "pkg", fps="12", loop=True, formats="png,webm,packed",
                      tiers="actor:64@12,prop:80@6"))
    result = run_cli([SCRIPT, "verify", "--package", tmp_path / "pkg"])
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "pass" and summary["transports"] == ["packedAlpha", "tier:actor", "tier:prop", "webm"]
    report = read(tmp_path / "pkg" / "verify-qa.json")
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
    assert proposed_errors(report, "verify_report_v1") == []
    assert report["schema"] == "video2dsprite.verify.v1" and report["status"] == "pass"
    assert report["inputs"][0] == {"path": "animation.json", "sha256": sha(tmp_path / "pkg" / "animation.json"),
                                   "bytes": (tmp_path / "pkg" / "animation.json").stat().st_size}
    assert {o["path"] for o in report["outputs"]} == {"idle.webm", "idle-packed.mp4", "idle-packed-actor.mp4",
                                                      "idle-packed-prop.mp4"}
    ids = {c["id"] for c in report["checks"]}
    assert {"webm.vp9_alpha_mode", "packedAlpha.moov_before_mdat", "packedAlpha.decoded_seam",
            "tier:prop.duration_ms", "webm.alpha_mae", "packedAlpha.rgb_mae"} <= ids
    validator = run_cli([script_path("video2dsprite", "validate_animation"), tmp_path / "pkg", "--require-verify"])
    assert validator.returncode == 0, validator.stderr
    again = run_cli([SCRIPT, "verify", "--package", tmp_path / "pkg"])
    assert again.returncode == 1 and "already exists" in again.stderr


@ffmpeg_test
def test_verify_fails_an_opaque_webm_and_writes_nothing(tmp_path):
    clean = tmp_path / "clean"
    write_frames(clean, _loop_reference(6, 48))
    E.package(options(clean, tmp_path / "pkg", fps="12", loop=True, formats="png,webm"))
    webm = tmp_path / "pkg" / "idle.webm"
    webm.unlink()
    AV.run([require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
            "color=c=0x3080c0:s=48x48:r=12", "-frames:v", "6", "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", str(webm)])
    manifest = read(tmp_path / "pkg" / "animation.json")
    manifest["webm"]["sha256"], manifest["webm"]["bytes"] = sha(webm), webm.stat().st_size
    (tmp_path / "pkg" / "animation.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = run_cli([SCRIPT, "verify", "--package", tmp_path / "pkg"])
    assert result.returncode == 1
    assert "webm.vp9_alpha_mode" in result.stderr and "webm.transparent_median" in result.stderr
    assert not (tmp_path / "pkg" / "verify-qa.json").exists()
    assert not list((tmp_path / "pkg").glob(".verify-qa.json*"))


# --------------------------------------------------------------------------- CLI conventions

def test_cli_help_cp1252():
    assert_cli_help(SKILL, "engine_export")
    for verb in ("package", "verify", "doctor"):
        for encoding in ("cp1252", "cp950"):
            result = run_cli([SCRIPT, verb, "--help"], encoding)
            assert result.returncode == 0 and result.stdout.strip() and result.stdout.isascii(), (verb, encoding)


def test_cli_refuses_existing_output(tmp_path):
    clean = tmp_path / "clean"
    write_frames(clean, walk_frames(3))
    existing = tmp_path / "out"
    existing.mkdir()
    (existing / "keep.txt").write_text("accepted", encoding="utf-8")
    result = run_cli([SCRIPT, "package", "--clean-dir", clean, "--output-dir", existing, "--fps", "12"])
    assert result.returncode == 1 and result.stderr.startswith("error: out-dir already exists")
    assert [p.name for p in existing.iterdir()] == ["keep.txt"]
    ok = run_cli([SCRIPT, "package", "--clean-dir", clean, "--output-dir", tmp_path / "pkg", "--fps", "12"])
    assert ok.returncode == 0, ok.stderr
    summary = json.loads(ok.stdout)
    assert ok.stdout.isascii() and len(ok.stdout.strip().splitlines()) == 1
    assert Path(summary["manifest"]).is_file() and summary["status"] == "needs-visual-review"
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    result = run_cli([SCRIPT, "verify", "--package", tmp_path / "pkg", "--report", report])
    assert result.returncode == 1 and "report already exists" in result.stderr
    assert report.read_text(encoding="utf-8") == "{}"


def test_cli_qc_failure_publishes_nothing(tmp_path, monkeypatch, capsys):
    clean = tmp_path / "clean"
    write_frames(clean, [body(dx=d, pocket=True) for d in range(3)])
    result = run_cli([SCRIPT, "package", "--clean-dir", clean, "--output-dir", tmp_path / "out", "--fps", "12"])
    assert result.returncode == 1 and result.stderr.startswith("error: key residue:")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clean"]
    pkg = tmp_path / "pkg"
    E.package(options(clean, pkg, allow_key_residue=True))
    failing = {"status": "fail", "checks": [{"id": "packedAlpha.alpha_mae", "status": "fail", "value": 9.0,
                                             "threshold": 3.5}], "transports": {}}
    monkeypatch.setattr(E, "verify_package", lambda folder: failing)
    assert E.main(["verify", "--package", str(pkg)]) == 1
    assert "packedAlpha.alpha_mae 9.0 (limit 3.5)" in capsys.readouterr().err
    assert not (pkg / "verify-qa.json").exists() and not list(pkg.glob(".verify-qa*"))
