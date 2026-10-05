"""codeart2d PixelSpec route: render_pixelspec.py (plan B18-T1), pixel_qa.py (B18-T3) and the
PixelSpec examples (B18-T4).

Every input is synthetic or a code-authored example of this skill (skills/codeart2d/examples);
tests/fixtures/codeart/slime.pixelspec.json is the design prototype spec (legacy schema id).
--build-clips runs the real generate2dsprite build_animation_clips.py by path.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from forge_testutils import (
    FIXTURES_DIR, SKILLS_DIR, assert_cli_help, assert_valid_contract, contract_errors, load_script, run_cli,
    script_path,
)

RENDER = load_script("codeart2d", "render_pixelspec")
QA = load_script("codeart2d", "pixel_qa")
core = RENDER.codeart_core
EXAMPLES = SKILLS_DIR / "codeart2d" / "examples"
SLIME = EXAMPLES / "slime.pixelspec.json"
WALKER = EXAMPLES / "walker16x24.pixelspec.json"
LEGACY_SLIME = FIXTURES_DIR / "codeart" / "slime.pixelspec.json"
CONTRACTS_DIR = FIXTURES_DIR / "contracts"
SLIME_OUTLINE = "#1a1c2c"


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_spec(folder: Path, spec: dict, name: str = "art.pixelspec.json") -> Path:
    path = folder / name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def render(capsys, *argv) -> tuple[int, str, str]:
    """render_pixelspec.main in-process: (exit code, stdout, stderr)."""
    code = RENDER.main([str(item) for item in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def pixel_qa(capsys, *argv) -> tuple[int, str, str]:
    code = QA.main([str(item) for item in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def summary_of(out: str) -> dict:
    lines = out.strip().splitlines()
    assert len(lines) == 1 and lines[0].isascii(), out
    return json.loads(lines[0])


def png_header(path: Path) -> tuple[int, int]:
    """(bit depth, colour type) from the IHDR chunk."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    return data[24], data[25]


def rgba(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA")).copy()


def small_spec(**changes) -> dict:
    """A 12x10 two-layer spec with a solid outline and one clip."""
    spec = {
        "schema": "codeart2d.pixelspec.v1", "name": "gem", "canvas": [12, 10], "anchor_px": [6, 9],
        "palette": {"k": "#101018", "a": "#d04040", "b": "#4060d0"},
        "variants": {"ruby": {}, "sapphire": {"a": "#3050c0"}},
        "outline": {"mode": "solid", "color": "k"},
        "poses": {"gem": ["aaa", "aba", "aaa"], "wide": ["aaaa", "abba", "aaaa"]},
        "layers": [{"name": "body", "origin": [4, 4], "rows": "gem"}],
        "frames": [{"name": "a"}, {"name": "b", "layers": {"body": {"rows": "wide", "dx": -1}}}],
        "clips": {"spin": {"frames": ["a", "b"], "duration_ms": [100, 120], "loop": True}},
    }
    spec.update(changes)
    return spec


def ref_path(path: Path, base: Path) -> str:
    """The fileRef path rule: relative POSIX, or the file name when the file is on another drive."""
    try:
        return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:
        return path.name


def stage_leftovers(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir() if ".stage-" in path.name)


# ----------------------------------------------------------------------------- render_pixelspec CLI basics

def test_render_pixelspec_help_is_ascii_under_cp1252_and_cp950():
    assert_cli_help("codeart2d", "render_pixelspec")


def test_render_pixelspec_refuses_an_existing_output_dir(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    code, stdout, stderr = render(capsys, "--spec", SLIME, "--output-dir", out)
    assert code == 1 and stdout == "" and stderr.startswith("error: refusing to replace existing output")
    assert list(out.iterdir()) == []


def test_render_pixelspec_strict_qc_failure_publishes_nothing(tmp_path, capsys):
    """A semi-transparent palette colour gives partial alpha: --strict-qc must leave nothing behind."""
    spec = small_spec(palette={"k": "#101018", "a": "#d0404080", "b": "#4060d0"})
    path = write_spec(tmp_path, spec)
    code, stdout, stderr = render(capsys, "--spec", path, "--output-dir", tmp_path / "out", "--strict-qc",
                                  "--build-clips")
    assert code == 1 and stdout == ""
    assert "strict QC failed" in stderr and "partial_alpha" in stderr and "Traceback" not in stderr
    assert not (tmp_path / "out").exists() and stage_leftovers(tmp_path) == []
    # Without --strict-qc the same spec publishes and records the failure, and the failed QA exits 1 (D26).
    code, stdout, stderr = render(capsys, "--spec", path, "--output-dir", tmp_path / "loose")
    summary = summary_of(stdout)
    assert code == 1 and summary["qa"] == "fail" and summary["failed_checks"] == ["partial_alpha"]
    assert stderr.startswith("error: published with QA status fail: partial_alpha (see ") and stderr.isascii()
    meta = read_json(tmp_path / "loose" / "codeart-meta.json")
    assert meta["qa"]["status"] == "fail"
    assert {check["id"]: check["status"] for check in meta["qa"]["checks"]}["partial_alpha"] == "fail"


def test_render_pixelspec_refuses_outputs_inside_the_skill(capsys):
    code, _, stderr = render(capsys, "--spec", SLIME, "--output-dir", EXAMPLES / "never-created")
    assert code == 1 and "not inside the codeart2d skill folder" in stderr
    assert not (EXAMPLES / "never-created").exists()


@pytest.mark.parametrize("tool", ["render_pixelspec", "pixel_qa"])
def test_main_reports_any_base_exception_as_one_internal_error_line(tmp_path, capsys, monkeypatch, tool):
    """r2-conventions F1, defence in depth: the hand-written mains turn any BaseException (a Rust panic from an
    extension, which derives from BaseException) into one 'internal error' line with exit 1; SystemExit still
    passes through."""
    module, call = (RENDER, render) if tool == "render_pixelspec" else (QA, pixel_qa)
    argv = (["--spec", SLIME, "--output-dir", tmp_path / "out"] if tool == "render_pixelspec"
            else ["--input", tmp_path / "frame.png"])

    class PanicException(BaseException):
        pass

    def panics(args):
        raise PanicException("range start index 2048 out of range for slice of length 256")

    monkeypatch.setattr(module, "run", panics)
    code, stdout, stderr = call(capsys, *argv)
    assert (code, stdout) == (1, "")
    assert stderr == ("error: internal error (PanicException: range start index 2048 out of range for slice of "
                      "length 256)\n")

    def exits(args):
        raise SystemExit(5)

    monkeypatch.setattr(module, "run", exits)
    with pytest.raises(SystemExit) as exited:
        module.main([str(item) for item in argv])
    assert exited.value.code == 5
    assert not (tmp_path / "out").exists()


# ----------------------------------------------------------------------------- B18-T1 acceptance

def test_slime_example_builds_four_frames_in_three_variants(tmp_path, capsys):
    """Acceptance B18-T1: 4 frames x 3 variants build; 0 partial alpha, 0 off-palette; poses reused by index."""
    out = tmp_path / "slime-v1"
    code, stdout, stderr = render(capsys, "--spec", SLIME, "--output-dir", out, "--variants", "all",
                                  "--clips-manifest", "--build-clips", "--preview-scale", "6", "--strict-qc")
    assert code == 0, stderr
    summary = summary_of(stdout)
    assert summary["qa"] == "pass" and summary["variants"] == ["green", "blue", "red"] and summary["frames"] == 3
    assert summary["metadata"] == str(out.resolve() / "codeart-meta.json")
    spec = read_json(SLIME)
    meta = read_json(out / "codeart-meta.json")
    assert_valid_contract(meta, "codeart", "codeart_meta_v1", skill="codeart2d")
    assert meta["art_source"] == "code" and meta["disclosure"] == core.DISCLOSURE
    assert meta["spec_sha256"] == RENDER.forge_core.sha256_file(SLIME)
    qa = meta["qa"]
    assert qa["status"] == "pass" and qa["tool"] == {"name": "render_pixelspec.py", "version": "0.4.0"}
    checks = {check["id"]: check for check in qa["checks"]}
    assert checks["partial_alpha"]["value"] == 0 and checks["off_palette"]["value"] == 0
    assert checks["outline_gaps"]["value"] == 0 and checks["l_corners"]["value"] <= 10
    assert len(qa["frames"]) == 9 and qa["inputs"][0]["sha256"] == meta["spec_sha256"]
    pngs = sorted(out.glob("*/frames/*.png"))
    assert [path.relative_to(out).as_posix() for path in pngs] == sorted(
        f"{variant}/frames/{name}.png" for variant in ("green", "blue", "red")
        for name in ("rest", "squash", "stretch"))
    for path in pngs:
        assert png_header(path) == (8, 6), "8-bit RGBA"
        pixels = rgba(path)
        assert set(np.unique(pixels[..., 3]).tolist()) == {0, 255}
        variant = path.parent.parent.name
        allowed = {tuple(colour[:3]) for colour in core.parse_palette(
            {"colors": spec["palette"], "variants": spec["variants"]}).resolve(variant).values()}
        assert {tuple(pixel) for pixel in pixels[pixels[..., 3] > 0][:, :3].tolist()} <= allowed
    # Each written frame equals the library render; rest-2 reuses rest by index (no duplicate file).
    for variant in ("green", "blue", "red"):
        for name in ("rest", "squash", "stretch"):
            assert np.array_equal(rgba(out / variant / "frames" / f"{name}.png"),
                                  core.render_pixelspec(spec, name, variant))
        manifest = read_json(out / variant / "clips.json")
        assert_valid_contract(manifest, "sprite", "clips_input", skill="codeart2d")
        assert manifest["schema"] == "generate2dsprite.animation_clips.v2"  # D11: v2 by default
        assert [frame["file"] for frame in manifest["frames"]] == [
            "frames/rest.png", "frames/squash.png", "frames/stretch.png"]
        assert manifest["clips"]["idle"] == {"frames": [0, 1, 0, 2], "duration_ms": [180, 120, 140, 160], "loop": True}
        assert manifest["anchor_px"] == [16, 31] and manifest["art_source"] == "code"
        assert manifest["sampling"] == "nearest" and manifest["pixel_art"] is True
        built = read_json(out / variant / "bundle" / "animation-clips.json")
        assert_valid_contract(built, "sprite", "animation_clips_v2", skill="codeart2d")
        assert built["sampling"] == "nearest" and built["art_source"] == "code"  # the codeart review's D11 finding
        assert built["clips"]["idle"]["frames"] == [0, 1, 0, 2]
        assert built["clips"]["idle"]["duration_ms"] == [180, 120, 140, 160]
        assert built["anchor_px"] == [16, 31]
    frames = meta["pixelspec"]["frames"]
    assert frames[2] == {"index": 2, "name": "rest-2", "file": "frames/rest.png", "reuses": 0}
    assert meta["pixelspec"]["clips"]["bundles"] == {name: f"{name}/bundle" for name in ("green", "blue", "red")}
    preview = rgba(out / "preview-x6.png")
    assert preview.shape == (3 * 32 * 6, 3 * 32 * 6, 4)
    assert np.array_equal(preview[:192, :192], core.upscale_nearest(core.render_pixelspec(spec, "rest", "green"), 6))
    listed = {item["path"] for item in meta["outputs"]}
    assert listed == {path.relative_to(out).as_posix() for path in pngs} | {
        f"{name}/clips.json" for name in ("green", "blue", "red")} | {"preview-x6.png"}
    assert stage_leftovers(tmp_path) == []


def test_render_is_byte_identical_across_runs(tmp_path, capsys):
    """Acceptance B18-T1: the same spec gives the same bytes (frames, manifests, preview, meta)."""
    spec = tmp_path / "slime.pixelspec.json"
    spec.write_bytes(SLIME.read_bytes())
    for run in ("run1", "run2"):
        code, _, stderr = render(capsys, "--spec", spec, "--output-dir", tmp_path / run, "--clips-manifest",
                                 "--preview-scale", "2")
        assert code == 0, stderr
    first = sorted(path.relative_to(tmp_path / "run1") for path in (tmp_path / "run1").rglob("*") if path.is_file())
    second = sorted(path.relative_to(tmp_path / "run2") for path in (tmp_path / "run2").rglob("*") if path.is_file())
    assert first == second and len(first) == 14
    for relative in first:
        assert (tmp_path / "run1" / relative).read_bytes() == (tmp_path / "run2" / relative).read_bytes(), relative


def test_walker_example_builds_four_direction_walk_clips(tmp_path, capsys):
    """B18-T4: the 16x24 four-direction walk builds in every variant; stand frames are reused by index."""
    out = tmp_path / "walker"
    code, stdout, stderr = render(capsys, "--spec", WALKER, "--output-dir", out, "--build-clips", "--strict-qc")
    assert code == 0, stderr
    spec = read_json(WALKER)
    meta = read_json(out / "codeart-meta.json")
    assert_valid_contract(meta, "codeart", "codeart_meta_v1", skill="codeart2d")
    assert summary_of(stdout)["frames"] == 12 and meta["pixelspec"]["canvas"] == [16, 24]
    for variant in spec["variants"]:
        manifest = read_json(out / variant / "clips.json")
        assert len(manifest["frames"]) == 12 and set(manifest["clips"]) == {
            "walk_down", "walk_up", "walk_right", "walk_left"}
        for clip in manifest["clips"].values():
            first, step_a, again, step_b = clip["frames"]
            assert first == again and len({first, step_a, step_b}) == 3
        built = read_json(out / variant / "bundle" / "animation-clips.json")
        assert set(built["clips"]) == set(manifest["clips"])
    left = core.render_pixelspec(spec, "left-stand", "forest")
    right = core.render_pixelspec(spec, "right-stand", "forest")
    assert np.array_equal(left, right[:, ::-1]), "left is the mirrored right"
    bottoms = [core.qa_pixels(core.render_pixelspec(spec, frame["name"], "forest"))["bbox"][3]
               for frame in spec["frames"]]
    assert set(bottoms) == {spec["anchor_px"][1]}, "every frame stands on the shared root"


def _gait_check(meta: dict) -> dict | None:
    return next((check for check in meta["qa"]["checks"] if check["id"] == "half_cycle_duplicates"), None)


def test_walk_and_run_clips_warn_on_duplicate_half_cycles(tmp_path, capsys):
    """Live validation 2026-10-06: the Codex fox run had half-cycle frames 0/4 and 3/7 at IoU 0.97 and 0.95,
    and the pixel gates passed it. Walk/run clips now warn (published, exit 0, even with --strict-qc) unless
    --allow-duplicate-half-cycle records that the leg colours carry the stride."""
    out = tmp_path / "warned"
    code, stdout, stderr = render(capsys, "--spec", WALKER, "--output-dir", out, "--strict-qc")
    assert code == 0, stderr
    line = summary_of(stdout)
    assert line["qa"] == "warn" and line["warned_checks"] == ["half_cycle_duplicates"] and line["failed_checks"] == []
    assert "warning: half_cycle_duplicates: walk_down 0/2 (IoU 1.00)" in stderr and "value contrast" in stderr
    meta = read_json(out / "codeart-meta.json")
    assert_valid_contract(meta, "codeart", "codeart_meta_v1", skill="codeart2d")
    check = _gait_check(meta)
    assert check["status"] == "warn" and check["value"] == 1.0 and check["threshold"] == 0.95
    assert {row["clip"] for row in check["detail"]["clips"]} == {"walk_down", "walk_up", "walk_right", "walk_left"}
    assert all(len(row["pairs"]) == 2 for row in check["detail"]["clips"])

    allowed = tmp_path / "allowed"
    code, stdout, stderr = render(capsys, "--spec", WALKER, "--output-dir", allowed, "--allow-duplicate-half-cycle")
    assert code == 0 and summary_of(stdout)["qa"] == "pass" and "half_cycle" not in stderr
    check = _gait_check(read_json(allowed / "codeart-meta.json"))
    assert check["status"] == "pass" and check["override"] == "--allow-duplicate-half-cycle" and check["failing"]


def test_half_cycle_check_only_judges_walk_and_run_clips(tmp_path, capsys):
    poses = {"p0": ["aa..", "aa..", "aa.."], "p1": [".aa.", ".aa.", ".aa."], "p2": ["..aa", "..aa", "..aa"],
             "p3": ["aaaa", "a..a", "a..a"]}
    frames = [{"name": f"f{i}", "layers": {"body": {"rows": f"p{i}"}}} for i in range(4)]
    clip = {"frames": ["f0", "f1", "f2", "f3"], "duration_ms": 100, "loop": True}
    spec = small_spec(poses=poses, frames=frames, outline={"mode": "none"},
                      layers=[{"name": "body", "origin": [4, 4], "rows": "p0"}])
    spec["clips"] = {"hero-run": clip}
    code, stdout, stderr = render(capsys, "--spec", write_spec(tmp_path, spec), "--output-dir", tmp_path / "run")
    assert code == 0 and summary_of(stdout)["qa"] == "pass", stderr
    check = _gait_check(read_json(tmp_path / "run" / "codeart-meta.json"))
    assert check["status"] == "pass" and check["value"] < 0.95 and check["detail"]["clips"][0]["clip"] == "hero-run"
    # a state named for a walk marks its clip; a spin clip with the same frames is never judged
    spec["clips"] = {"cycle": clip, "spin": dict(clip, frames=["f0", "f1", "f0", "f1"])}
    spec["states"] = {"walking": "cycle"}
    code, stdout, _ = render(capsys, "--spec", write_spec(tmp_path, spec, "b.json"), "--output-dir", tmp_path / "b")
    check = _gait_check(read_json(tmp_path / "b" / "codeart-meta.json"))
    assert code == 0 and [row["clip"] for row in check["detail"]["clips"]] == ["cycle"]
    del spec["states"]
    code, stdout, _ = render(capsys, "--spec", write_spec(tmp_path, spec, "c.json"), "--output-dir", tmp_path / "c")
    assert code == 0 and _gait_check(read_json(tmp_path / "c" / "codeart-meta.json")) is None
    assert RENDER.GAIT_NAME.search("walk_right") and RENDER.GAIT_NAME.search("Running")
    assert not RENDER.GAIT_NAME.search("rune") and not RENDER.GAIT_NAME.search("crawler")


def test_legacy_schema_alias_still_renders(tmp_path, capsys):
    code, _, stderr = render(capsys, "--spec", LEGACY_SLIME, "--output-dir", tmp_path / "legacy", "--variants", "red")
    assert code == 0, stderr
    meta = read_json(tmp_path / "legacy" / "codeart-meta.json")
    assert meta["pixelspec"]["schema"] == "codeart.pixelspec.v1" and meta["pixelspec"]["variants"] == {"red": "red"}


def test_variant_selection(tmp_path, capsys):
    code, stdout, stderr = render(capsys, "--spec", SLIME, "--output-dir", tmp_path / "pick", "--variants",
                                  "red, blue,red")
    assert code == 0, stderr
    assert summary_of(stdout)["variants"] == ["red", "blue"]
    assert sorted(path.name for path in (tmp_path / "pick").iterdir()) == ["blue", "codeart-meta.json", "red"]
    code, _, stderr = render(capsys, "--spec", SLIME, "--output-dir", tmp_path / "base", "--variants", "base")
    assert code == 0, stderr
    assert np.array_equal(rgba(tmp_path / "base" / "base" / "frames" / "rest.png"),
                          core.render_pixelspec(read_json(SLIME), "rest", None))
    code, _, stderr = render(capsys, "--spec", SLIME, "--output-dir", tmp_path / "bad", "--variants", "gold")
    assert code == 1 and "unknown variant 'gold'" in stderr and "green, blue, red" in stderr
    assert not (tmp_path / "bad").exists()


def test_spec_without_frames_renders_its_base_layers(tmp_path, capsys):
    spec = small_spec()
    for key in ("frames", "clips", "variants"):
        del spec[key]
    code, stdout, stderr = render(capsys, "--spec", write_spec(tmp_path, spec), "--output-dir", tmp_path / "out")
    assert code == 0, stderr
    assert summary_of(stdout)["variants"] == ["base"]
    assert np.array_equal(rgba(tmp_path / "out" / "base" / "frames" / "gem.png"), core.render_pixelspec(spec))
    code, _, stderr = render(capsys, "--spec", write_spec(tmp_path, spec, "b.json"), "--output-dir",
                             tmp_path / "clips", "--clips-manifest")
    assert code == 1 and "no clips to export" in stderr


# ----------------------------------------------------------------------------- spec validation

def _segments_and_rows(spec):
    spec["poses"]["gem"] = {"rows": ["aaa"], "segments": ["3a"]}


@pytest.mark.parametrize("change, path_in_error, renderer_accepts", [
    (lambda s: s["palette"].update(a=[208, 64, 64]), "$.palette.a", True),
    (lambda s: s["poses"].update(alias="gem"), "$.poses.alias", True),
    (_segments_and_rows, "$.poses.gem", True),
    (lambda s: s["layers"][0].update(mirror=1), "$.layers[0].mirror", True),
    (lambda s: s["frames"][0].update(flip_x="yes"), "$.frames[0].flip_x", True),
    (lambda s: s.update(schema="codeart.pixelspec.v2"), "$.schema", False),
    (lambda s: s["palette"].update({".": "#ffffff"}), "property name '.'", False),
    (lambda s: s.update(outline={"mode": "solid"}), "'color' is a required property", False),
    (lambda s: s["clips"]["spin"].update(frames=[]), "$.clips.spin.frames", True),
], ids=["integer-rgb-colour", "pose-alias", "rows-and-segments", "truthy-mirror", "string-flip", "schema-id",
        "reserved-palette-key", "outline-without-colour", "empty-clip"])
def test_specs_outside_the_contract_are_refused_before_rendering(tmp_path, capsys, change, path_in_error,
                                                                renderer_accepts):
    """The A3 renderer is more lenient than pixelspec_v1 (A0 handoff section 5): the CLI checks the contract first."""
    spec = small_spec()
    change(spec)
    if renderer_accepts:
        core.render_pixelspec(spec, 0, "ruby")  # the library alone would render it
    code, stdout, stderr = render(capsys, "--spec", write_spec(tmp_path, spec), "--output-dir", tmp_path / "out")
    assert code == 1 and stdout == ""
    assert stderr.startswith("error: the spec does not follow codeart2d.pixelspec.v1") and path_in_error in stderr
    assert not (tmp_path / "out").exists() and stage_leftovers(tmp_path) == []


@pytest.mark.parametrize("change, message", [
    (lambda s: s["layers"][0].update(rows="gems"), "unknown pose 'gems'"),
    (lambda s: s["frames"][1]["layers"].update(cape={"dx": 1}), "unknown layer(s) 'cape'"),
    (lambda s: s["clips"]["spin"].update(frames=["a", "c"]), "unknown frame 'c'"),
    (lambda s: s["clips"]["spin"].update(frames=[0, 5], duration_ms=100), "index 5 is out of range"),
    (lambda s: s["frames"][1].update(name="a"), "frame names must be unique"),
    (lambda s: s["poses"].update(unused=["aq"]), "pose 'unused' uses 'q'"),
    (lambda s: s["clips"]["spin"].update(duration_ms=[100]), "2 frame(s) but 1 duration_ms"),
    (lambda s: s["variants"].update(gold={"z": "#ffd700"}), "variant 'gold' overrides 'z'"),
    (lambda s: s["clips"]["spin"].update(events=[{"at": 2, "name": "hit"}]), "events[0].at is position 2"),
    (lambda s: s["clips"]["spin"].update(transitions=[{"to": "idle"}]), "unknown clip 'idle'"),
    (lambda s: s["layers"].append({"name": "cape", "z": 1}), "renders layer 'cape', which has no rows"),
    (lambda s: s["layers"].append({"name": "body"}), "layer names must be unique"),
    (lambda s: s.update(states={"idle": "spin", "run": "dash"}), "states must map"),
], ids=["unknown-pose", "unknown-layer", "unknown-frame-name", "frame-index", "duplicate-frame-name",
        "unused-pose-char", "duration-count", "variant-key", "event-position", "transition-target",
        "layer-without-rows", "duplicate-layer", "states"])
def test_cross_references_a_schema_cannot_express_are_checked(tmp_path, capsys, change, message):
    spec = small_spec()
    change(spec)
    assert contract_errors(spec, "codeart", "pixelspec_v1", skill="codeart2d") == [], "schema-valid on purpose"
    code, _, stderr = render(capsys, "--spec", write_spec(tmp_path, spec), "--output-dir", tmp_path / "out")
    assert code == 1 and message in stderr, stderr
    assert not (tmp_path / "out").exists()


def test_pixels_outside_the_canvas_fail_and_publish_nothing(tmp_path, capsys):
    """Roadmap P0-5: a PixelSpec pixel outside the canvas is an error, never a crop."""
    spec = small_spec()
    spec["frames"][1]["layers"]["body"]["dx"] = 6
    code, stdout, stderr = render(capsys, "--spec", write_spec(tmp_path, spec), "--output-dir", tmp_path / "out")
    assert code == 1 and stdout == "" and "overflows the 12x10 canvas" in stderr
    spec = small_spec(layers=[{"name": "body", "origin": [0, 4], "rows": "gem"}])
    code, _, stderr = render(capsys, "--spec", write_spec(tmp_path, spec, "edge.json"), "--output-dir", tmp_path / "out")
    assert code == 1 and "outline would overflow the canvas" in stderr
    assert not (tmp_path / "out").exists() and stage_leftovers(tmp_path) == []


@pytest.mark.parametrize("text, message", [
    ('{"schema": "codeart2d.pixelspec.v1", "schema": "x"}', "duplicate key 'schema'"),
    ('{"canvas": [NaN, 3]}', "NaN"),
    ("[1, 2]", "one JSON object"),
    ("{nope", "not valid JSON"),
])
def test_spec_files_are_read_strictly(tmp_path, capsys, text, message):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    code, _, stderr = render(capsys, "--spec", path, "--output-dir", tmp_path / "out")
    assert code == 1 and message in stderr


# ----------------------------------------------------------------------------- clips handoff

def fx_spec() -> dict:
    """A burst that fades out: frames 2 and 3 hide the only layer, so they render no pixel."""
    hidden = {"layers": {"spark": {"hidden": True}}}
    return small_spec(
        poses={"small": ["a"], "big": [".a.", "aba", ".a."]},
        layers=[{"name": "spark", "origin": [5, 4], "rows": "big"}],
        frames=[{"name": "pop", "layers": {"spark": {"rows": "small", "dx": 1, "dy": 1}}}, {"name": "burst"},
                {"name": "fade", **hidden}, {"name": "gone", **hidden}],
        clips={"hit": {"frames": ["pop", "burst", "fade", "gone"], "duration_ms": [50, 60, 70, 80], "loop": False,
                       "events": [{"at": 1, "name": "hit"}, {"at": 3, "name": "end"}]},
               "flash": {"frames": [1, 0], "duration_ms": 40, "loop": True}})


def test_empty_tail_frames_are_merged_into_the_last_visible_frame(tmp_path, capsys):
    out = tmp_path / "fx"
    code, stdout, stderr = render(capsys, "--spec", write_spec(tmp_path, fx_spec()), "--output-dir", out,
                                  "--variants", "ruby", "--clips-manifest")
    assert code == 0, stderr
    assert "2 frame(s) render no pixel and were not written: fade, gone" in stderr
    manifest = read_json(out / "ruby" / "clips.json")
    assert manifest["schema"] == "generate2dsprite.animation_clips.v2", "events are a v2 clip field"
    assert_valid_contract(manifest, "sprite", "clips_input", skill="codeart2d")
    hit = manifest["clips"]["hit"]
    assert hit["frames"] == [0, 1] and hit["duration_ms"] == [50, 210] and hit["loop"] is False
    assert hit["events"] == [{"at": 1, "name": "hit"}, {"at": 1, "name": "end"}]
    assert manifest["clips"]["flash"] == {"frames": [1, 0], "duration_ms": 40, "loop": True}
    assert sorted(path.name for path in (out / "ruby" / "frames").iterdir()) == ["burst.png", "pop.png"]
    meta = read_json(out / "codeart-meta.json")
    assert meta["pixelspec"]["clips"]["per_clip"]["hit"] == {"frames": 4, "merged_empty_tail": 2}
    assert meta["pixelspec"]["frames"][2] == {"index": 2, "name": "fade", "file": None, "empty": True}


def test_empty_tail_merge_feeds_the_v1_builder(tmp_path, capsys):
    """Without v2-only fields the merged clip stays a v1 manifest that the current builder accepts."""
    spec = fx_spec()
    del spec["clips"]["hit"]["events"]
    out = tmp_path / "fx"
    code, _, stderr = render(capsys, "--spec", write_spec(tmp_path, spec), "--output-dir", out, "--variants",
                             "sapphire", "--build-clips", "--clips-schema", "v1")
    assert code == 0, stderr
    assert read_json(out / "sapphire" / "clips.json")["schema"] == "generate2dsprite.animation_clips.v1"
    built = read_json(out / "sapphire" / "bundle" / "animation-clips.json")
    assert built["clips"]["hit"]["frames"] == [0, 1] and built["clips"]["hit"]["duration_ms"] == [50, 210]


def test_ticks_merge_and_v2_fields_pass_through(tmp_path, capsys):
    spec = fx_spec()
    spec["clips"]["hit"] = {"frames": ["pop", "burst", "fade"], "ticks": 3, "tick_hz": 60, "loop_policy": "oneshot",
                            "keys": {"strike": 1, "end": 2}, "role": "fx"}
    code, _, stderr = render(capsys, "--spec", write_spec(tmp_path, spec), "--output-dir", tmp_path / "out",
                             "--clips-manifest")
    assert code == 0, stderr
    hit = read_json(tmp_path / "out" / "ruby" / "clips.json")["clips"]["hit"]
    assert hit == {"frames": [0, 1], "ticks": [3, 6], "tick_hz": 60, "loop_policy": "oneshot",
                   "keys": {"strike": 1, "end": 1}, "role": "fx"}


def test_an_empty_frame_inside_a_clip_is_refused(tmp_path, capsys):
    spec = fx_spec()
    spec["clips"]["hit"]["frames"] = ["pop", "fade", "burst", "gone"]
    code, _, stderr = render(capsys, "--spec", write_spec(tmp_path, spec), "--output-dir", tmp_path / "out",
                             "--clips-manifest")
    assert code == 1 and "'fade' at position 1 renders no" in stderr and "only empty frames at the end" in stderr
    assert not (tmp_path / "out").exists()


def test_a_frame_without_transparent_pixels_is_refused_for_clips(tmp_path, capsys):
    spec = small_spec(canvas=[3, 3], anchor_px=[1, 3], outline={"mode": "none"},
                      layers=[{"name": "body", "origin": [0, 0], "rows": "gem"}],
                      frames=[{"name": "a"}], clips={"still": {"frames": [0], "duration_ms": 100, "loop": True}})
    path = write_spec(tmp_path, spec)
    code, _, stderr = render(capsys, "--spec", path, "--output-dir", tmp_path / "out", "--clips-manifest")
    assert code == 1 and "covers the whole canvas" in stderr
    code, _, stderr = render(capsys, "--spec", path, "--output-dir", tmp_path / "frames-only")
    assert code == 0, stderr


def test_clips_need_an_anchor(tmp_path, capsys):
    spec = small_spec()
    del spec["anchor_px"]
    code, _, stderr = render(capsys, "--spec", write_spec(tmp_path, spec), "--output-dir", tmp_path / "out",
                             "--build-clips")
    assert code == 1 and "needs anchor_px" in stderr
    spec["anchor_px"] = [6, 11]
    code, _, stderr = render(capsys, "--spec", write_spec(tmp_path, spec, "b.json"), "--output-dir",
                             tmp_path / "out", "--clips-manifest")
    assert code == 1 and "inside the 12x10 canvas" in stderr


def test_a_builder_failure_publishes_nothing(tmp_path, capsys):
    builder = tmp_path / "fake_builder.py"
    builder.write_text("import sys\nprint('Clip export failed: boom', file=sys.stderr)\nraise SystemExit(2)\n",
                       encoding="utf-8")
    code, stdout, stderr = render(capsys, "--spec", SLIME, "--output-dir", tmp_path / "out", "--build-clips",
                                  "--clips-builder", builder)
    assert code == 1 and stdout == ""
    assert "build_animation_clips failed for variant 'green' (exit 2): Clip export failed: boom" in stderr
    assert not (tmp_path / "out").exists() and stage_leftovers(tmp_path) == []
    code, _, stderr = render(capsys, "--spec", SLIME, "--output-dir", tmp_path / "out", "--build-clips",
                             "--clips-builder", tmp_path / "missing.py")
    assert code == 1 and "build_animation_clips.py not found" in stderr


def test_the_default_builder_is_the_sibling_generate2dsprite_script():
    assert RENDER.DEFAULT_CLIPS_BUILDER == script_path("generate2dsprite", "build_animation_clips")


# ----------------------------------------------------------------------------- contract validator

_KEYWORDS = RENDER.forge_schema.KEYWORDS  # D31: the vendored forge_schema evaluates the contracts now


def _keywords(node, found: set) -> set:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for sub in value.values():
                    _keywords(sub, found)
            elif key not in ("enum", "const", "required", "default", "examples"):
                found.add(key)
                _keywords(value, found)
            else:
                found.add(key)
    elif isinstance(node, list):
        for item in node:
            _keywords(item, found)
    return found


def test_validator_supports_every_keyword_of_the_vendored_schemas():
    schemas = SKILLS_DIR / "codeart2d" / "references" / "schemas"
    used = set()
    for path in schemas.glob("*.schema.json"):
        _keywords(read_json(path), used)
    assert used and used <= _KEYWORDS, sorted(used - _KEYWORDS)


def _mutate(document, case):
    if "document" in case:
        return copy.deepcopy(case["document"])
    result = copy.deepcopy(document)
    pointer = case["set"] if "set" in case else case["remove"]
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    parent = result
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
    if "set" in case:
        parent[key] = copy.deepcopy(case["value"])
    else:
        del parent[key]
    return result


def _fixture_documents():
    """(domain, def, document, label) for every contract fixture of the schemas codeart2d vendors."""
    for path in sorted(CONTRACTS_DIR.glob("*.json")):
        domain, name, kind = path.name[:-len(".json")].split(".", 2)
        if domain not in ("codeart", "common", "sprite", "map"):
            continue
        if kind == "invalid":
            valid = read_json(CONTRACTS_DIR / f"{domain}.{name}.valid.json")
            for case in read_json(path)["cases"]:
                yield domain, name, _mutate(valid, case), f"{path.name}: {case['why']}"
        else:
            yield domain, name, read_json(path), path.name


def test_validator_agrees_with_jsonschema_on_every_vendored_contract_fixture():
    pytest.importorskip("jsonschema")
    checked, disagreements = 0, []
    for domain, name, document, label in _fixture_documents():
        expected = not contract_errors(document, domain, name, skill="codeart2d")
        actual = not RENDER.contract_errors(document, domain, name)
        checked += 1
        if expected != actual:
            disagreements.append(f"{label}: jsonschema {'accepts' if expected else 'rejects'}")
    assert checked > 250 and not disagreements, disagreements


def test_validator_agrees_with_jsonschema_on_pixelspec_mutations():
    pytest.importorskip("jsonschema")
    base = read_json(WALKER)
    mutations = {
        "ok": lambda s: None, "short-colour": lambda s: s["palette"].update(k="#123"),
        "colour-without-hash": lambda s: s["palette"].update(k="22182c"),
        "bad-colour": lambda s: s["palette"].update(k="#12345"), "float-canvas": lambda s: s.update(canvas=[16.0, 24]),
        "zero-canvas": lambda s: s.update(canvas=[0, 24]), "bool-canvas": lambda s: s.update(canvas=[True, 24]),
        "float-z": lambda s: s["layers"][0].update(z=0.5),
        "string-origin": lambda s: s["layers"][0].update(origin="0,0"),
        "fraction-dx": lambda s: s["frames"][1]["layers"]["head"].update(dy=0.5),
        "segments-zero-run": lambda s: s["poses"].update(x={"segments": ["0k"]}),
        "segments-ok": lambda s: s["poses"].update(x={"segments": ["2k3.k"]}),
        "selout-no-map": lambda s: s.update(outline={"mode": "selout", "color": "k"}),
        "selout-ok": lambda s: s.update(outline={"mode": "selout", "color": "k", "map": {"t": "T"}}),
        "outline-two-chars": lambda s: s.update(outline={"mode": "solid", "color": "kk"}),
        "clip-ticks": lambda s: s["clips"]["walk_up"].update(ticks=[9, 9, 9, 9], loop_policy="pingpong"),
        "clip-bad-event": lambda s: s["clips"]["walk_up"].update(events=[{"at": 0, "name": "boom"}]),
        "clip-custom-event": lambda s: s["clips"]["walk_up"].update(events=[{"at": 0, "name": "custom:boom"}]),
        "clip-no-timing": lambda s: s["clips"]["walk_up"].pop("duration_ms"),
        "anchor-string": lambda s: s.update(anchor_px="8,23"),
        "variant-space-key": lambda s: s["variants"].update(v={" ": "#fff"}),
        "layers-empty": lambda s: s.update(layers=[]), "frame-not-object": lambda s: s["frames"].append("x"),
    }
    for label, change in mutations.items():
        spec = copy.deepcopy(base)
        change(spec)
        expected = contract_errors(spec, "codeart", "pixelspec_v1", skill="codeart2d")
        actual = RENDER.contract_errors(spec, "codeart", "pixelspec_v1")
        assert bool(expected) == bool(actual), (label, expected, actual)
        assert (label in ("ok", "short-colour", "colour-without-hash", "float-canvas", "float-z", "segments-ok",
                          "selout-ok", "clip-ticks", "clip-custom-event")) == (not actual), (label, actual)


# ----------------------------------------------------------------------------- pixel_qa

def slime_frames(folder: Path, variant: str = "blue", scale: int = 1) -> list[Path]:
    spec = read_json(SLIME)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in ("rest", "squash", "stretch"):
        path = folder / f"{name}.png"
        core.save_png(core.upscale_nearest(core.render_pixelspec(spec, name, variant), scale), path)
        paths.append(path)
    return paths


def test_pixel_qa_help_is_ascii_under_cp1252_and_cp950():
    assert_cli_help("codeart2d", "pixel_qa")


def test_pixel_qa_refuses_existing_report_or_review(tmp_path, capsys):
    frames = slime_frames(tmp_path / "frames")
    for flag in ("--report", "--review"):
        existing = tmp_path / f"existing{flag}.out"
        existing.write_text("keep", encoding="utf-8")
        code, _, stderr = pixel_qa(capsys, "--input", *frames, flag, existing)
        assert code == 1 and "refusing to replace existing file" in stderr
        assert existing.read_text(encoding="utf-8") == "keep"


def test_pixel_qa_strict_fails_on_off_palette_pixels_and_writes_nothing(tmp_path, capsys):
    """Acceptance B18-T3: --strict fails on off-palette pixels; no partial output on failure."""
    frames = slime_frames(tmp_path / "frames")
    pixels = rgba(frames[1])
    y, x = np.argwhere(pixels[..., 3] == 255)[10]
    pixels[y, x, :3] = (255, 0, 255)
    core.save_png(pixels, frames[1])
    report, review = tmp_path / "qa.json", tmp_path / "review.png"
    code, stdout, stderr = pixel_qa(capsys, "--input", str(tmp_path / "frames" / "*.png"), "--palette", SLIME,
                                    "--variant", "blue", "--strict", "--report", report, "--review", review)
    assert code == 1 and stdout == ""
    assert "strict QA failed" in stderr and "off_palette 1 (limit 0) in squash.png" in stderr
    assert not report.exists() and not review.exists()
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]
    # Without --strict the report is written, and the failed QA still exits 1 (D26).
    code, stdout, stderr = pixel_qa(capsys, "--input", *frames, "--palette", SLIME, "--variant", "blue",
                                    "--report", report)
    assert code == 1 and summary_of(stdout)["failed"] == ["off_palette"]
    assert stderr.startswith("error: QA failed: off_palette (report written)") and "--strict" in stderr
    assert read_json(report)["status"] == "fail"


def test_pixel_qa_writes_a_review_sheet_and_a_qa_envelope(tmp_path, capsys):
    """Acceptance B18-T3: a review sheet is written; the report is a common QA envelope."""
    frames = slime_frames(tmp_path / "art" / "frames")
    report, review = tmp_path / "qa" / "qa.json", tmp_path / "qa" / "review.png"
    code, stdout, stderr = pixel_qa(
        capsys, "--input", str(tmp_path / "art" / "frames" / "*.png"), "--palette", SLIME, "--variant", "blue",
        "--outline", "k", "--anchor", "16,31", "--review", review, "--scales", "1,2,4", "--bg", "light,dark,checker",
        "--onion", "--strict", "--report", report)
    assert code == 0, stderr
    summary = summary_of(stdout)
    assert summary["status"] == "pass" and summary["review"] == str(review.resolve())
    data = read_json(report)
    assert_valid_contract(data, "common", "qaEnvelope", skill="codeart2d")
    assert data["tool"] == {"name": "pixel_qa.py", "version": "0.4.0"}
    assert [item["path"] for item in data["inputs"]] == [
        "../art/frames/rest.png", "../art/frames/squash.png", "../art/frames/stretch.png",
        ref_path(SLIME, report.parent)]
    assert data["outputs"] == [{"path": "review.png", "sha256": RENDER.forge_core.sha256_file(review),
                                "bytes": review.stat().st_size}]
    checks = {check["id"]: check for check in data["checks"]}
    assert {name: checks[name]["status"] for name in ("partial_alpha", "off_palette", "outline_gaps", "l_corners",
                                                      "anchor", "same_size", "loop_seam")} == dict.fromkeys(
        ("partial_alpha", "off_palette", "outline_gaps", "l_corners", "anchor", "same_size", "loop_seam"), "pass")
    assert [frame["ground_gap_px"] for frame in data["frames"]] == [0, 0, 0]
    expected = core.review_sheet([rgba(path) for path in frames], scales=(1, 2, 4), onion=True,
                                 palette=read_json(SLIME)["palette"], qa=[{}] * 3, anchor=(16, 31))
    with Image.open(review) as image:
        assert image.mode == "RGBA" and image.size == expected.size


def test_pixel_qa_variant_palette_and_meta_palette(tmp_path, capsys):
    frames = slime_frames(tmp_path / "frames", "red")
    code, stdout, _ = pixel_qa(capsys, "--input", *frames, "--palette", SLIME, "--variant", "red", "--strict")
    assert code == 0 and summary_of(stdout)["status"] == "pass"
    code, _, stderr = pixel_qa(capsys, "--input", *frames, "--palette", SLIME, "--strict")
    assert code == 1 and "off_palette" in stderr, "the base palette has no red-variant colours"
    code, _, stderr = render(capsys, "--spec", SLIME, "--output-dir", tmp_path / "out", "--variants", "red")
    assert code == 0, stderr
    code, stdout, stderr = pixel_qa(capsys, "--input", str(tmp_path / "out" / "red" / "frames" / "*.png"),
                                    "--palette", tmp_path / "out" / "codeart-meta.json", "--variant", "red", "--strict")
    assert code == 0, stderr
    code, _, stderr = pixel_qa(capsys, "--input", *frames, "--variant", "red")
    assert code == 1 and "--variant needs --palette" in stderr
    # Outline names resolve in the checked variant: a variant may recolour the outline.
    recoloured = tmp_path / "recoloured.json"
    spec = read_json(SLIME)
    spec["variants"]["red"]["k"] = "#300818"
    recoloured.write_text(json.dumps(spec), encoding="utf-8")
    red = core.render_pixelspec(spec, "rest", "red")
    core.save_png(red, tmp_path / "red-outline.png")
    code, stdout, stderr = pixel_qa(capsys, "--input", tmp_path / "red-outline.png", "--palette", recoloured,
                                    "--variant", "red", "--outline", "k", "--strict")
    assert code == 0, stderr
    assert summary_of(stdout)["status"] == "pass"


def test_pixel_qa_detects_the_pixel_grid_of_upscaled_art(tmp_path, capsys):
    frames = slime_frames(tmp_path / "x6", scale=6)
    report = tmp_path / "grid.json"
    code, _, stderr = pixel_qa(capsys, "--input", *frames, "--detect-grid", "--strict", "--report", report)
    assert code == 0, stderr
    grid = {check["id"]: check for check in read_json(report)["checks"]}["grid"]
    assert grid["status"] == "pass" and grid["value"]["periods"] == [6] and grid["value"]["min_uniformity"] == 1.0
    native = slime_frames(tmp_path / "x1")
    code, stdout, _ = pixel_qa(capsys, "--input", *native, "--detect-grid", "--strict")
    assert code == 0, "warnings never fail --strict"
    assert summary_of(stdout)["warned"] == ["grid"]


def test_pixel_qa_expands_globs_in_natural_order(tmp_path, capsys):
    source = slime_frames(tmp_path / "src")
    folder = tmp_path / "frames"
    folder.mkdir()
    for index, number in enumerate((10, 2, 1)):
        (folder / f"frame-{number}.png").write_bytes(source[index].read_bytes())
    report = tmp_path / "qa.json"
    code, _, stderr = pixel_qa(capsys, "--input", str(folder / "frame-*.png"), str(folder / "frame-2.png"),
                               "--report", report)
    assert code == 0, stderr
    assert [frame["file"] for frame in read_json(report)["frames"]] == [
        "frames/frame-1.png", "frames/frame-2.png", "frames/frame-10.png"]
    code, _, stderr = pixel_qa(capsys, "--input", str(folder / "nothing-*.png"))
    assert code == 1 and "no files match" in stderr


def test_pixel_qa_anchor_outline_and_loop_seam_checks(tmp_path, capsys):
    frames = slime_frames(tmp_path / "frames")
    code, stdout, stderr = pixel_qa(capsys, "--input", *frames, "--anchor", "40,31", "--strict")
    assert code == 1 and "anchor" in stderr
    code, stdout, _ = pixel_qa(capsys, "--input", *frames, "--outline", SLIME_OUTLINE + ",#000000")
    assert code == 0 and summary_of(stdout)["status"] == "pass"
    code, _, stderr = pixel_qa(capsys, "--input", *frames, "--outline", "q")
    assert code == 1 and "neither a #hex colour nor a palette colour name" in stderr
    # A loop that drifts 1 px per frame jumps back 5 px at the wrap: a warning (never a strict
    # failure), and no check at all for one-shots.
    rest = core.render_pixelspec(read_json(SLIME), "rest", "blue")
    drift = []
    for shift in range(6):
        drift.append(tmp_path / "drift" / f"drift-{shift}.png")
        core.save_png(np.roll(rest, shift, axis=1), drift[-1])
    code, stdout, _ = pixel_qa(capsys, "--input", *drift, "--onion", "--strict")
    assert code == 0 and summary_of(stdout)["warned"] == ["loop_seam"]
    code, stdout, _ = pixel_qa(capsys, "--input", *drift, "--onion", "--once")
    assert summary_of(stdout)["warned"] == []
    code, stdout, _ = pixel_qa(capsys, "--input", *frames, "--onion")
    assert summary_of(stdout)["warned"] == [], "the slime idle loop does not pop"


def test_pixel_qa_reads_any_png_mode(tmp_path, capsys):
    """Indexed PNG frames (engine exports) load as exact RGBA for QA."""
    frame = core.render_pixelspec(read_json(SLIME), "rest", "green")
    colours, index = np.unique(frame.reshape(-1, 4), axis=0, return_inverse=True)
    image = Image.fromarray(index.reshape(frame.shape[:2]).astype(np.uint8), mode="P")
    image.putpalette(colours[:, :3].astype(np.uint8).tobytes())
    path = tmp_path / "indexed.png"
    image.save(path, transparency=bytes(colours[:, 3].astype(np.uint8)))
    assert np.array_equal(rgba(path), frame)
    code, stdout, stderr = pixel_qa(capsys, "--input", path, "--palette", SLIME, "--variant", "green", "--strict")
    assert code == 0, stderr


# ----------------------------------------------------------------------------- clean machines

BLOCKER = """import runpy, sys
for name in sys.argv[1].split(","):
    sys.modules[name] = None
script = sys.argv[2]
sys.argv = [script, *sys.argv[3:]]
runpy.run_path(script, run_name="__main__")
"""


@pytest.mark.parametrize("tool, argv", [
    ("render_pixelspec", ["--spec", "x.json", "--output-dir", "out"]),
    ("pixel_qa", ["--input", "x.png"]),
])
def test_missing_numpy_prints_the_pip_command_not_a_traceback(tmp_path, tool, argv):
    blocker = tmp_path / "blocker.py"
    blocker.write_text(BLOCKER, encoding="utf-8")
    result = run_cli([blocker, "numpy", script_path("codeart2d", tool), *argv], "cp1252", cwd=tmp_path)
    assert result.returncode == 1 and "Traceback" not in result.stderr
    assert "missing Python module(s): numpy" in result.stderr and "python -m pip install numpy" in result.stderr
    help_result = run_cli([blocker, "numpy", script_path("codeart2d", tool), "--help"], "cp1252", cwd=tmp_path)
    assert help_result.returncode == 0 and "usage:" in help_result.stdout, "help works without numpy"


def test_oversized_previews_and_review_sheets_are_refused(tmp_path, capsys):
    """A huge integer upscale fails fast with a hint instead of allocating gigabytes."""
    code, _, stderr = render(capsys, "--spec", WALKER, "--output-dir", tmp_path / "out", "--preview-scale", "64")
    assert code == 1 and "lower --preview-scale" in stderr and not (tmp_path / "out").exists()
    frames = slime_frames(tmp_path / "frames")
    code, _, stderr = pixel_qa(capsys, "--input", *frames, "--review", tmp_path / "review.png", "--scales", "128")
    assert code == 1 and "smaller --scales" in stderr and not (tmp_path / "review.png").exists()


def test_clips_schema_v1_refuses_v2_fields_and_the_validator_is_forge_schema(tmp_path, capsys):
    """D11: --clips-schema v1 is only for builders that predate the v2 reader, so clips that need v2
    fields are refused instead of silently losing them. D31: no private evaluator is left."""
    code, _, stderr = render(capsys, "--spec", write_spec(tmp_path, fx_spec()), "--output-dir", tmp_path / "out",
                             "--variants", "ruby", "--clips-manifest", "--clips-schema", "v1")
    assert code == 1 and "'hit'" in stderr and "drop --clips-schema v1" in stderr
    assert not (tmp_path / "out").exists()
    assert not hasattr(RENDER, "_LocalContracts") and not hasattr(RENDER, "_local_contract_errors")
    assert RENDER.contract_errors({"schema": "codeart2d.pixelspec.v1"}, "codeart", "pixelspec_v1")
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + SLIME.read_bytes())
    code, _, stderr = render(capsys, "--spec", bom, "--output-dir", tmp_path / "bom-out")
    assert code == 0, stderr  # D28: a spec saved with a UTF-8 BOM is read
