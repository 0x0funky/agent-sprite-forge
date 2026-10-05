"""validate_animation.py (B08-T5, T6): the animation contract and hash-bound QA.

Negative fixtures live in tests/fixtures/animation/negative/<name>.json:
``{"rule", "why", "base", "args", "ops", "alsoFails"}``. Each is applied to a fresh copy of a
package built here with engine_export (``base`` "png", or "video" with webm, packed and one tier,
which needs ffmpeg) and must fail exactly on ``rule`` plus the rules in ``alsoFails``. Ops:
``set``/``remove``/``append`` (a JSON pointer into animation.json), ``png`` (write a transparent
PNG of ``size``), ``touch`` (append a byte), ``encode`` (replace a video: ``kind`` packed or
opaque-webm with ``size``, ``frames`` and ``fps``) and ``duplicate-timestamp`` (remux a video so
packet ``packet`` repeats the previous packet's timestamp, with ffmpeg's setts filter). Replaced
files are re-hashed in the manifest and its QA, so only the named rule sees them. Every rule has a
negative fixture, ``schema`` and ``ffprobe-timestamps`` included (D21).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (FIXTURES_DIR, assert_cli_help, assert_valid_contract, load_script, require_ffmpeg,
                             run_cli, script_path)

E = load_script("video2dsprite", "engine_export")
V = load_script("video2dsprite", "validate_animation")
AV = load_script("video2dsprite", "forge_av")
SCRIPT = script_path("video2dsprite", "validate_animation")
NEGATIVE = sorted((FIXTURES_DIR / "animation" / "negative").glob("*.json"))


def body(dx: int, size: int = 48) -> np.ndarray:
    frame = np.zeros((size, size, 4), np.uint8)
    frame[10:40, 14 + dx:34 + dx] = (20, 30, 40, 255)
    frame[11:39, 15 + dx:33 + dx] = (40, 160, 210, 255)
    return frame


def disc(k: int, n: int, width: int = 48, height: int = 48) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width] + 0.5
    cx, cy = width / 2 + 8 * np.cos(2 * np.pi * k / n), height / 2 + 8 * np.sin(2 * np.pi * k / n)
    alpha = np.clip(9 - np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) + 0.5, 0, 1)
    frame = np.zeros((height, width, 4), np.uint8)
    frame[..., 0], frame[..., 1], frame[..., 2] = 200, 150, 60
    frame[..., 3] = (alpha * 255 + 0.5).astype(np.uint8)
    frame[frame[..., 3] == 0] = 0
    return frame


def write_frames(folder: Path, frames) -> list[Path]:
    folder.mkdir(parents=True)
    paths = []
    for i, frame in enumerate(frames):
        paths.append(folder / f"clean_{i:04d}.png")
        Image.fromarray(frame).save(paths[-1])
    return paths


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path: Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def package(clean: Path, out: Path, **kw) -> dict:
    values = dict(clean_dir=str(clean), out_dir=str(out), name="idle", fps="12", formats="png")
    values.update(kw)
    return E.package(argparse.Namespace(**values))


@pytest.fixture(scope="module")
def png_base(tmp_path_factory) -> Path:
    """5 frames, 100 ms each, step_l at 0 ms and hit at 250 ms, impact 250 ms, hold 400 ms, cycle."""
    root = tmp_path_factory.mktemp("png-base")
    frames = write_frames(root / "frames-clean", [body(dx) for dx in (0, 1, 2, 1, 0)])
    selection = root / "selection.json"
    write(selection, {"schema": "forge-frame-selection/v2", "sourceDirectory": "frames-clean",
                      "sourceHashes": [sha(p) for p in frames], "start": 0, "endExclusive": 5,
                      "sourceIndices": [0, 1, 2, 3, 4], "durations_ms": [100] * 5, "loopPolicy": "cycle",
                      "events": [{"name": "step_l", "atMs": 0}, {"name": "hit", "atMs": 250}], "impactMs": 250,
                      "holdMs": 400, "status": "selected-needs-visual-review", "method": "test"})
    package(root / "frames-clean", root / "idle", fps=None, selection=str(selection))
    return root / "idle"


@pytest.fixture(scope="module")
def video_base(tmp_path_factory) -> Path:
    """8 frames of 48x48 at 12 fps, loop, png + webm + packed and tier actor:32@12."""
    require_ffmpeg()
    caps = E.capabilities()
    if not (caps["webm"] and caps["packed"]):
        pytest.skip(f"ffmpeg lacks a working VP9-alpha or libx264 path: {caps['errors']}")
    root = tmp_path_factory.mktemp("video-base")
    write_frames(root / "frames-clean", [disc(k, 8) for k in range(8)])
    package(root / "frames-clean", root / "idle", loop=True, formats="png,webm,packed", tiers="actor:32@12")
    return root / "idle"


def copy(base: Path, tmp_path: Path) -> Path:
    target = tmp_path / "pkg" / "idle"
    shutil.copytree(base, target)
    return target


def failed_rules(paths, *args: str, **kw) -> set[str]:
    parsed = V.build_parser().parse_args([str(p) for p in paths] + list(args))
    return {f.rule for f in V.validate([Path(p) for p in paths], V.options_from_args(parsed), **kw)["failed"]}


# --------------------------------------------------------------------------- fixture ops

def _pointer(path: str) -> list:
    return [int(p) if p.isdigit() else p for p in path.lstrip("/").split("/")]


def _parent(document, path: str):
    keys = _pointer(path)
    for key in keys[:-1]:
        document = document[key]
    return document, keys[-1]


def _rehash(manifest: dict, folder: Path, name: str) -> None:
    for _, record in E.package_media(manifest):
        if record.get("file") == name:
            record["sha256"], record["bytes"] = sha(folder / name), (folder / name).stat().st_size
    for record in manifest["qa"]["outputs"]:
        if record["path"] == name:
            record["sha256"], record["bytes"] = sha(folder / name), (folder / name).stat().st_size


def _duplicate_timestamp(target: Path, packet: int) -> None:
    """Remux ``target`` so packet ``packet`` carries the previous packet's timestamp (non-increasing DTS)."""
    original = target.with_name(f"original-{target.name}")
    target.rename(original)
    AV.run([require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(original), "-map", "0:v:0",
            "-c", "copy", "-bsf:v", rf"setts=ts=if(eq(N\,{packet})\,PREV_OUTPTS\,TS)", str(target)])
    original.unlink()


def _encode(target: Path, op: dict) -> None:
    width, height = op["size"]
    target.unlink()
    if op["kind"] == "packed":
        AV.encode_packed_alpha([disc(k, op["frames"], width, height) for k in range(op["frames"])], target, op["fps"])
    else:
        AV.run([require_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
                f"color=c=0x3080c0:s={width}x{height}:r={op['fps']}", "-frames:v", str(op["frames"]),
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", str(target)])


def apply_ops(folder: Path, ops: list[dict]) -> None:
    manifest = read(folder / "animation.json")
    for op in ops:
        kind = op["op"]
        if kind == "set":
            parent, key = _parent(manifest, op["path"])
            parent[key] = op["value"]
        elif kind == "remove":
            parent, key = _parent(manifest, op["path"])
            del parent[key]
        elif kind == "append":
            parent, key = _parent(manifest, op["path"])
            parent[key].append(op["value"])
        elif kind == "png":
            Image.new("RGBA", tuple(op["size"])).save(folder / op["file"])
            _rehash(manifest, folder, op["file"])
        elif kind == "touch":
            with open(folder / op["file"], "ab") as stream:
                stream.write(b"\0")
        elif kind == "encode":
            _encode(folder / op["file"], op)
            _rehash(manifest, folder, op["file"])
        elif kind == "duplicate-timestamp":
            _duplicate_timestamp(folder / op["file"], op["packet"])
            _rehash(manifest, folder, op["file"])
        else:
            raise AssertionError(f"unknown fixture op {kind}")
    write(folder / "animation.json", manifest)


def _negative_cases():
    for path in NEGATIVE:
        spec = read(path)
        marks = [pytest.mark.ffmpeg] if spec["base"] == "video" else []
        yield pytest.param(path, marks=marks, id=path.stem)


@pytest.mark.parametrize("fixture", list(_negative_cases()))
def test_negative_fixture_fails_on_its_rule(fixture, request, tmp_path):
    """B08-T5: every negative fixture fails on its named rule (and only the rules it declares)."""
    spec = read(fixture)
    assert spec["rule"] in V.RULES and set(spec["alsoFails"]) <= set(V.RULES), fixture.name
    base = request.getfixturevalue(f"{spec['base']}_base")
    folder = copy(base, tmp_path)
    apply_ops(folder, spec["ops"])
    assert failed_rules([folder], *spec["args"]) == {spec["rule"], *spec["alsoFails"]}, spec["why"]


def test_negative_fixtures_cover_the_rules():
    """D21: every rule, schema and ffprobe-timestamps included, has a negative fixture."""
    covered = {read(path)["rule"] for path in NEGATIVE}
    assert covered >= set(V.RULES)
    assert len(NEGATIVE) == len({path.stem for path in NEGATIVE}) >= 39


# --------------------------------------------------------------------------- positive cases

def test_valid_package_passes_and_reports(png_base, tmp_path):
    folder = copy(png_base, tmp_path)
    assert failed_rules([folder], "--require-states", "idle", "--static-size", "48,48",
                        "--static-anchor", "24,48") == set()
    report = tmp_path / "validation.json"
    result = run_cli([SCRIPT, folder, "--report", report])
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "pass" and summary["packages"] == ["idle"] and result.stdout.isascii()
    data = read(report)
    assert_valid_contract(data, "common", "qaEnvelope", skill="video2dsprite")
    assert_valid_contract(data, "video", "validation_report_v1", skill="video2dsprite")
    assert summary["output"] == summary["metadata"] == summary["report"] == str(report.resolve())
    assert data["inputs"][0]["path"] == "pkg/idle/animation.json"
    assert {c["id"] for c in data["checks"]} >= {"idle.anchor", "idle.qa-stale", "idle.poster"}


def test_static_sprite_png_and_parent_folder(png_base, tmp_path):
    folder = copy(png_base, tmp_path)
    shutil.copytree(folder, tmp_path / "pkg" / "walk")
    manifest = read(tmp_path / "pkg" / "walk" / "animation.json")
    manifest["name"] = "walk"
    write(tmp_path / "pkg" / "walk" / "animation.json", manifest)
    sprite = tmp_path / "hero.png"
    Image.new("RGBA", (48, 48)).save(sprite)
    assert failed_rules([tmp_path / "pkg"], "--require-states", "idle,walk", "--static-sprite", str(sprite),
                        "--static-anchor", "24,48") == set()
    assert failed_rules([tmp_path / "pkg"], "--static-sprite", str(sprite), "--static-anchor", "24.02,48") == \
        {"static-sprite"}


def test_v2_manifest_is_read(png_base, tmp_path):
    """animation.json 2.0 stays readable: its QA is reported as not hash-bound, never as a failure."""
    folder = copy(png_base, tmp_path)
    manifest = read(folder / "animation.json")
    v2 = {key: manifest[key] for key in ("name", "sourceSize", "sourceAnchor", "inputFrameSize", "sourceRect",
                                         "sourceRectFormat", "encodedSize", "contentSize", "encodedAnchor", "fps",
                                         "frameCount", "durationSeconds", "loop", "reviewStatus", "hitEvents",
                                         "poster", "fallback", "inputFrames", "runtimeNotes")}
    v2.update(schemaVersion="2.0", registration="preserved-source-canvas", fps=10.0,
              qa={"status": "needs-visual-review", "seamPremultipliedMAE": 0.0})
    write(folder / "animation.json", v2)
    result = V.validate([folder])
    assert result["failed"] == []
    assert {f.rule for f in result["findings"]} == {"qa-incomplete"}


# --------------------------------------------------------------------------- B08-T6 hash-bound QA

def _verify_report(folder: Path) -> dict:
    """A minimal valid verify-qa.json for a PNG-only package (no encoded files to bind)."""
    return {"schema": "video2dsprite.verify.v1", "status": "pass", "method": "test", "notProven": ["playback"],
            "checks": [], "inputs": [{"path": "animation.json", "sha256": sha(folder / "animation.json")}],
            "outputs": [], "tool": {"name": "engine_export.py", "version": E.ENGINE_EXPORT_VERSION}}


def _edit_manifest(folder: Path, change) -> None:
    manifest = read(folder / "animation.json")
    change(manifest)
    write(folder / "animation.json", manifest)


MUTANTS = {
    "packaged file edited after QA": (lambda f: open(f / "idle-atlas-00.png", "ab").write(b"\0"), "qa-stale"),
    "QA output hash rewritten": (lambda f: _edit_manifest(f, lambda m: m["qa"]["outputs"][0].update(sha256="0" * 64)),
                                 "qa-stale"),
    "QA misses a packaged file": (lambda f: _edit_manifest(f, lambda m: m["qa"]["outputs"].pop(0)), "qa-partial"),
    "QA covers a file the package does not ship":
        (lambda f: _edit_manifest(f, lambda m: m["qa"]["outputs"].append({"path": "old-atlas.png",
                                                                          "sha256": "1" * 64})), "qa-foreign"),
    "pass status over a failed check":
        (lambda f: _edit_manifest(f, lambda m: (m["qa"].update(status="pass"),
                                                m["qa"]["checks"][0].update(status="fail"))), "qa-inconsistent"),
    "verify report of an older animation.json":
        (lambda f: (write(f / "verify-qa.json", _verify_report(f)),
                    _edit_manifest(f, lambda m: m.update(displayScale=2.0))), "qa-stale"),
}


@pytest.mark.parametrize("mutant", list(MUTANTS))
def test_stale_qa_rejected(mutant, png_base, tmp_path):
    """B08-T6: six mutants of the hash-bound QA; each is refused by its rule (Dusk qa-release-gates)."""
    folder = copy(png_base, tmp_path)
    mutate, rule = MUTANTS[mutant]
    assert failed_rules([folder], "--require-verify") == {"verify"}
    write(folder / "verify-qa.json", _verify_report(folder))
    assert failed_rules([folder], "--require-verify") == set()
    (folder / "verify-qa.json").unlink()
    mutate(folder)
    assert rule in failed_rules([folder])


def test_qa_errors_names_each_problem():
    expected = {"a.png": "a" * 64, "b.png": "b" * 64}
    good = {"status": "pass", "method": "m", "notProven": ["x"], "checks": [{"id": "c", "status": "pass"}],
            "inputs": [], "outputs": [{"path": "a.png", "sha256": "a" * 64}, {"path": "b.png", "sha256": "b" * 64}],
            "tool": {"name": "t", "version": "1"}}
    assert V.qa_errors(good, expected_outputs=expected, current=expected) == []
    stale = V.qa_errors(good, expected_outputs=expected, current={"a.png": "a" * 64, "b.png": None})
    assert [rule for rule, _ in stale] == ["qa-stale"]
    duplicate = {**good, "outputs": good["outputs"] + [{"path": "a.png", "sha256": "a" * 64}]}
    assert [rule for rule, _ in V.qa_errors(duplicate, expected_outputs=expected, current=expected)] == ["qa-foreign"]
    warned = {**good, "status": "needs-visual-review"}
    assert [r for r, _ in V.qa_errors(warned, expected_outputs=expected, current=expected,
                                      accept=("pass", "warn"))] == ["qa-failed"]
    bound = V.qa_errors(good, expected_outputs=expected, current=expected, expected_inputs={"animation.json": "c" * 64})
    assert [rule for rule, _ in bound] == ["qa-partial"]


@pytest.mark.ffmpeg
def test_real_verify_report_goes_stale(video_base, tmp_path):
    folder = copy(video_base, tmp_path)
    result = run_cli([script_path("video2dsprite", "engine_export"), "verify", "--package", folder])
    assert result.returncode == 0, result.stderr
    assert failed_rules([folder], "--require-verify") == set()
    with open(folder / "idle-packed.mp4", "ab") as stream:
        stream.write(b"\0")
    assert failed_rules([folder], "--require-verify") >= {"files", "qa-stale"}


# --------------------------------------------------------------------------- CLI conventions

def test_cli_help_cp1252():
    assert_cli_help("video2dsprite", "validate_animation")


def test_cli_refuses_existing_report(png_base, tmp_path):
    folder = copy(png_base, tmp_path)
    report = tmp_path / "report.json"
    report.write_text("keep", encoding="utf-8")
    result = run_cli([SCRIPT, folder, "--report", report])
    assert result.returncode == 1 and result.stderr.startswith("error: report already exists")
    assert report.read_text(encoding="utf-8") == "keep"


def test_cli_failure_writes_no_report(png_base, tmp_path):
    folder = copy(png_base, tmp_path)
    report = tmp_path / "report.json"
    result = run_cli([SCRIPT, folder, "--require-review", "--report", report])
    assert result.returncode == 1
    assert result.stderr.splitlines() == ["error: review: idle: reviewStatus is needs-visual-review; package with "
                                          "--review <verdict>"]
    assert not report.exists() and not list(tmp_path.glob(".report.json*"))
    missing = run_cli([SCRIPT, tmp_path / "nowhere"])
    assert missing.returncode == 1 and missing.stderr.startswith("error: not found")
