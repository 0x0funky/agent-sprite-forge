"""CLI tests for skills/generate2dsprite/scripts/palette_tool.py (module B04-palette-pixel, task B04-T3).

Every image is synthetic. Documents are validated against the generate2dsprite
vendored schemas; palette_luts_v1 and the palette_lock_v1 additions are B04's
schema change requests (handoff/B04-palette-pixel.md section 5), applied here in
memory exactly as filed.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from forge_testutils import (assert_cli_help, assert_valid_contract, contract_errors, load_script, run_cli,
                             script_path)

TOOL = script_path("generate2dsprite", "palette_tool")
fp = load_script("generate2dsprite", "forge_palette")
fc = load_script("generate2dsprite", "forge_core")
SUBCOMMANDS = ("build", "apply", "lock", "luts", "variants", "quantize-seq")

# --------------------------------------------------------------------------- B04 schema change requests (section 5)
# Integrated into shared/schemas/sprite.schema.json: palette_lock_v1 colors and transparent_index, and the new
# palette_luts_v1. Validated against the vendored generate2dsprite copy.

def amended_errors(instance, name: str) -> list[str]:
    """Errors against the vendored sprite schema, which holds B04's section 5 requests."""
    return contract_errors(instance, "sprite", name, skill="generate2dsprite")


# --------------------------------------------------------------------------- helpers

def tool(*args, encoding: str | None = None):
    return run_cli([TOOL, *map(str, args)], encoding)


def ok(*args) -> dict:
    result = tool(*args)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].isascii(), result.stdout
    assert result.stderr == ""
    return json.loads(lines[0])


def fails(*args) -> str:
    result = tool(*args)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stdout == ""
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("error: ") and "Traceback" not in result.stderr, result.stderr
    return lines[0]


def nothing_published(parent: Path, name: str) -> bool:
    return not (parent / name).exists() and not list(parent.glob(f".{name}.stage-*"))


def gradient_frame(start, stop, *, shift: int = 0, size=(24, 40)) -> np.ndarray:
    """An outlined horizontal colour ramp on transparency (binary alpha)."""
    height, width = size
    ramp = np.clip((np.arange(width) - shift) / (width - 1), 0.0, 1.0)[None, :, None]
    start, stop = np.array(start, np.float64), np.array(stop, np.float64)
    frame = np.zeros((height, width, 4), np.uint8)
    frame[3:-3, 2:-2, :3] = np.floor(start + (stop - start) * ramp + 0.5)[:, 2:-2]
    frame[3:-3, 2:-2, 3] = 255
    frame[3, 2:-2, :3] = frame[-4, 2:-2, :3] = (24, 20, 36)
    return frame


def write_frames(folder: Path, frames, prefix: str = "walk") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for index, frame in enumerate(frames):
        fc.save_png(frame, folder / f"{prefix}_{index}.png")
    return folder


def hero_frames(folder: Path) -> Path:
    return write_frames(folder, [gradient_frame((40, 30, 90), (230, 200, 120), shift=s) for s in (0, 2, 4)])


def rgba(path: Path) -> np.ndarray:
    return np.asarray(fc.load_rgba(path)[0])


def check_file_refs(document: dict, folder: Path) -> None:
    for reference in document["inputs"] + document["outputs"]:
        target = (folder / reference["path"]).resolve()
        assert target.is_file(), reference
        assert reference["sha256"] == fc.sha256_file(target)


# --------------------------------------------------------------------------- the three standard CLI tests

def test_help_works_on_narrow_consoles():
    assert_cli_help("generate2dsprite", "palette_tool")
    for command in SUBCOMMANDS:
        result = tool(command, "--help", encoding="cp1252")
        assert result.returncode == 0, result.stderr
        assert result.stdout.isascii() and "--output-dir" in result.stdout


@pytest.mark.parametrize("command", SUBCOMMANDS)
def test_refuses_an_existing_output_dir(tmp_path, command):
    frames = hero_frames(tmp_path / "frames")
    palette = tmp_path / "palette.json"
    fp.write_palette(fp.build_palette(rgba(frames / "walk_0.png"), 6), palette)
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep.txt").write_text("mine", encoding="utf-8")
    arguments = {"build": ["--input", frames],
                 "apply": ["--palette", palette, "--input", frames],
                 "lock": ["--palette", palette, "--source", frames],
                 "luts": ["--palette", palette],
                 "variants": ["--palette", palette, "--input", frames, "--quantize"],
                 "quantize-seq": ["--palette", palette, "--input", frames]}[command]
    message = fails(command, *arguments, "--output-dir", output)
    assert "already exists" in message
    assert [path.name for path in output.iterdir()] == ["keep.txt"]
    assert (output / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert not list(tmp_path.glob(".out.stage-*"))


@pytest.mark.parametrize("command", ["build", "apply", "lock", "quantize-seq"])
def test_strict_qc_failure_publishes_nothing(tmp_path, command):
    frames = hero_frames(tmp_path / "frames")
    palette = tmp_path / "palette.json"
    fp.write_palette(fp.Palette(((0, 0, 0), (255, 255, 255))), palette)
    arguments = {"build": ["--input", frames, "--colors", "2", "--max-delta-e", "0.001"],
                 "apply": ["--palette", palette, "--input", frames, "--max-delta-e", "0.001"],
                 "lock": ["--palette", palette, "--source", frames, "--strict"],
                 "quantize-seq": ["--palette", palette, "--input", frames, "--max-delta-e", "0.001"]}[command]
    message = fails(command, *arguments, "--output-dir", tmp_path / "out")
    assert "nothing was published" in message
    assert nothing_published(tmp_path, "out")


def test_errors_are_one_line_without_traceback(tmp_path):
    frames = hero_frames(tmp_path / "frames")
    assert "palette file not found" in fails("apply", "--palette", tmp_path / "nope.json", "--input", frames,
                                             "--output-dir", tmp_path / "out")
    assert "not found" in fails("build", "--input", tmp_path / "missing", "--output-dir", tmp_path / "out")
    assert "--colors must be 1..256" in fails("build", "--input", frames, "--colors", "300",
                                              "--output-dir", tmp_path / "out")
    assert nothing_published(tmp_path, "out")


# --------------------------------------------------------------------------- build / apply / lock

def test_cli_build_apply_lock_roundtrip(tmp_path):
    """B04-T3: build -> apply -> lock -> grow -> apply --lock reproduces the first outputs byte for byte."""
    frames = hero_frames(tmp_path / "frames")
    names = sorted(path.name for path in frames.iterdir())
    built = ok("build", "--input", frames, "--colors", "6", "--name", "hero", "--output-dir", tmp_path / "pal")
    assert built["status"] == "pass" and built["colors"] == 6
    assert set(built["files"]) == {"palette.json", "palette.gpl", "palette.hex", "swatches.png"}
    palette_doc = json.loads((tmp_path / "pal" / "palette.json").read_text(encoding="utf-8"))
    assert_valid_contract(palette_doc, "sprite", "palette_v1", skill="generate2dsprite")
    assert palette_doc["name"] == "hero" and palette_doc["transparent_index"] == 255 and not palette_doc["locked"]
    assert "palette_tool build" in palette_doc["source"]
    assert not any(mark in palette_doc["source"] for mark in ("/", "\\", str(tmp_path))), "no paths in source"
    qa = json.loads((tmp_path / "pal" / "palette-qa.json").read_text(encoding="utf-8"))
    assert_valid_contract(qa, "common", "qaEnvelope", skill="generate2dsprite")
    check_file_refs(qa, tmp_path / "pal")
    assert set(qa["fit"]) == {f"../frames/{name}" for name in names}

    applied = ok("apply", "--palette", tmp_path / "pal" / "palette.json", "--input", frames, "--indexed",
                 "--output-dir", tmp_path / "out1")
    assert applied["frames"] == 3 and applied["indexed"]
    palette = fp.read_palette(tmp_path / "pal" / "palette.json")
    for name in names:
        out = tmp_path / "out1" / name
        report = fp.fit_report(out, palette)
        assert report["off_palette_px"] == 0 and report["partial_alpha_px"] == 0
        assert np.array_equal(rgba(tmp_path / "out1" / "indexed" / name), rgba(out))
    apply_qa = json.loads((tmp_path / "out1" / "apply-qa.json").read_text(encoding="utf-8"))
    assert_valid_contract(apply_qa, "common", "qaEnvelope", skill="generate2dsprite")
    check_file_refs(apply_qa, tmp_path / "out1")

    locked = ok("lock", "--palette", tmp_path / "pal" / "palette.json", "--source", frames,
                "--output-dir", tmp_path / "lock")
    assert locked["sources"] == 3 and locked["status"] == "warn", "raw sources are recorded with their fit"
    lock_palette = json.loads((tmp_path / "lock" / "palette.json").read_text(encoding="utf-8"))
    assert lock_palette["locked"] is True and lock_palette["colors"] == palette_doc["colors"]
    record = json.loads((tmp_path / "lock" / "palette-lock.json").read_text(encoding="utf-8"))
    assert_valid_contract(record, "sprite", "palette_lock_v1", skill="generate2dsprite")
    assert amended_errors(record, "palette_lock_v1") == []
    assert record["palette"]["sha256"] == fc.sha256_file(tmp_path / "lock" / "palette.json")
    assert [entry["path"] for entry in record["sources"]] == [f"../frames/{name}" for name in names]
    assert [entry["sha256"] for entry in record["sources"]] == [fc.sha256_file(frames / n) for n in names]

    newcomer = write_frames(tmp_path / "newcomer", [gradient_frame((90, 60, 100), (180, 140, 110))], "enemy")
    grown = ok("build", "--base", tmp_path / "lock" / "palette.json", "--input", newcomer, "--colors", "10",
               "--output-dir", tmp_path / "pal2")
    grown_doc = json.loads((tmp_path / "pal2" / "palette.json").read_text(encoding="utf-8"))
    assert [c["hex"] for c in grown_doc["colors"][:6]] == [c["hex"] for c in palette_doc["colors"]]
    assert all(entry.get("reserved") for entry in grown_doc["colors"][:6]) and grown["colors"] > 6

    again = ok("apply", "--palette", tmp_path / "pal2" / "palette.json", "--lock",
               tmp_path / "lock" / "palette-lock.json", "--input", frames, "--output-dir", tmp_path / "out2")
    assert again["locked_inputs"] == 3
    for name in names:
        assert (tmp_path / "out2" / name).read_bytes() == (tmp_path / "out1" / name).read_bytes()
    ok("apply", "--palette", tmp_path / "pal2" / "palette.json", "--input", frames, "--output-dir", tmp_path / "out3")
    assert any((tmp_path / "out3" / name).read_bytes() != (tmp_path / "out1" / name).read_bytes() for name in names), \
        "without --lock the grown palette changes the hero's pixels"
    changed = fp.read_palette(tmp_path / "pal2" / "palette.json").replace(
        colors=((1, 2, 3),) + fp.read_palette(tmp_path / "pal2" / "palette.json").colors[1:])
    fp.write_palette(changed, tmp_path / "moved.json")
    assert "does not extend" in fails("apply", "--palette", tmp_path / "moved.json", "--lock",
                                      tmp_path / "lock" / "palette-lock.json", "--input", frames,
                                      "--output-dir", tmp_path / "out4")


def test_build_is_byte_deterministic_and_balances_inputs(tmp_path):
    frames = hero_frames(tmp_path / "frames")
    small = write_frames(tmp_path / "small", [gradient_frame((20, 160, 60), (200, 255, 200), size=(14, 18))], "gem")
    first = ok("build", "--input", frames, small, "--colors", "8", "--output-dir", tmp_path / "a")
    ok("build", "--input", frames, small, "--colors", "8", "--output-dir", tmp_path / "b")
    for name in first["files"] + ["palette-qa.json"]:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    ok("build", "--input", frames, small, "--colors", "8", "--balance", "--output-dir", tmp_path / "c")
    greens = []
    for folder in ("a", "c"):
        lab = fp.to_oklab(fp.read_palette(tmp_path / folder / "palette.json").rgb).astype(np.float64)
        greens.append(int((lab[:, 1] < -0.05).sum()))
    assert greens[1] > greens[0], "--balance gives the small green gem a fair share of colours"


def test_build_reserve_formats_and_transparent_index(tmp_path):
    frames = hero_frames(tmp_path / "frames")
    built = ok("build", "--input", frames, "--colors", "6", "--reserve", "#181424", "#ffffff",
               "--formats", "gpl,pal", "--transparent-index", "6", "--output-dir", tmp_path / "pal")
    assert set(built["files"]) == {"palette.json", "palette.gpl", "palette.pal", "swatches.png"}
    palette = fp.read_palette(tmp_path / "pal" / "palette.json")
    assert palette.colors[:2] == ((24, 20, 36), (255, 255, 255)) and palette.reserved[:2] == (True, True)
    assert palette.transparent_index == 6
    assert fp.read_palette(tmp_path / "pal" / "palette.pal").colors == palette.colors
    ok("apply", "--palette", tmp_path / "pal" / "palette.json", "--input", frames, "--indexed",
       "--output-dir", tmp_path / "out")
    indexed = (tmp_path / "out" / "indexed" / "walk_0.png").read_bytes()
    assert indexed[24] == 4 and len(indexed) < 600, "a compact slot keeps the PLTE at 7 entries (4-bit)"
    assert "would hide colour" in fails("build", "--input", frames, "--colors", "6", "--transparent-index", "2",
                                        "--output-dir", tmp_path / "bad")
    assert "cannot move" in fails("build", "--base", tmp_path / "pal" / "palette.json", "--input", frames,
                                  "--colors", "6", "--transparent-index", "255", "--output-dir", tmp_path / "bad")
    assert "unknown palette format" in fails("build", "--input", frames, "--formats", "aco",
                                             "--output-dir", tmp_path / "bad")
    assert nothing_published(tmp_path, "bad")


def test_apply_keep_alpha_orphans_and_lock_strict(tmp_path):
    frame = gradient_frame((40, 30, 90), (230, 200, 120))
    frame[10, 10, 3] = 90                      # a partial-alpha pixel
    frames = write_frames(tmp_path / "frames", [frame])
    palette = tmp_path / "palette.json"
    fp.write_palette(fp.build_palette(frame, 5, alpha_threshold=1), palette)
    ok("apply", "--palette", palette, "--input", frames, "--keep-alpha", "--output-dir", tmp_path / "soft")
    kept = rgba(tmp_path / "soft" / "walk_0.png")
    assert kept[10, 10, 3] == 90 and fp.fit_report(kept, fp.read_palette(palette))["off_palette_px"] == 0
    qa = json.loads((tmp_path / "soft" / "apply-qa.json").read_text(encoding="utf-8"))
    assert {c["id"]: c["status"] for c in qa["checks"]}["output_partial_alpha_px"] == "skipped"
    assert "--indexed needs binary alpha" in fails("apply", "--palette", palette, "--input", frames, "--keep-alpha",
                                                   "--indexed", "--output-dir", tmp_path / "bad")
    ok("apply", "--palette", palette, "--input", frames, "--orphans", "1", "--output-dir", tmp_path / "hard")
    assert rgba(tmp_path / "hard" / "walk_0.png")[10, 10, 3] == 0
    ok("lock", "--palette", palette, "--source", tmp_path / "hard", "--strict", "--output-dir", tmp_path / "lock")
    record = json.loads((tmp_path / "lock" / "palette-lock.json").read_text(encoding="utf-8"))
    assert record["sources"][0]["fit"]["off_palette_px"] == 0 and record["sources"][0]["fit"]["max_delta_e"] == 0.0


# --------------------------------------------------------------------------- luts and variants

def _on_palette_sprite():
    palette = fp.Palette(((24, 20, 36), (200, 120, 60), (240, 220, 180), (90, 160, 80)))
    sprite = np.zeros((10, 12, 4), np.uint8)
    sprite[2:9, 2:10] = (24, 20, 36, 255)
    sprite[3:8, 3:9] = (200, 120, 60, 255)
    sprite[4:6, 4:7] = (240, 220, 180, 255)
    sprite[7, 8] = (90, 160, 80, 255)
    return palette, sprite


def test_variants_bake(tmp_path):
    """B04-T3: hitflash, frozen, silhouette and skin variants keep alpha and follow the LUT rows exactly."""
    palette, sprite = _on_palette_sprite()
    fp.write_palette(palette, tmp_path / "palette.json")
    frames = write_frames(tmp_path / "frames", [sprite, np.roll(sprite, 1, axis=1)])
    (tmp_path / "skin-blue.json").write_text(json.dumps({"#c8783c": "#3c78c8", "3": "#e0e0e0"}), encoding="utf-8")
    baked = ok("variants", "--palette", tmp_path / "palette.json", "--input", frames,
               "--skin-map", tmp_path / "skin-blue.json", "--output-dir", tmp_path / "var")
    assert baked["variants"] == ["hitflash", "frozen", "silhouette", "skin-skin-blue"] and baked["frames"] == 2
    visible = sprite[..., 3] > 0
    flash = rgba(tmp_path / "var" / "hitflash" / "walk_0.png")
    assert np.array_equal(flash[..., 3], sprite[..., 3])
    assert (flash[visible][:, :3] == 255).all() and (flash[~visible] == 0).all()
    shadow = rgba(tmp_path / "var" / "silhouette" / "walk_0.png")
    assert (shadow[visible][:, :3] == 0).all() and (shadow[visible][:, 3] == 255).all()
    skin = rgba(tmp_path / "var" / "skin-skin-blue" / "walk_0.png")
    assert skin[4, 3, :3].tolist() == [60, 120, 200] and skin[7, 8, :3].tolist() == [224, 224, 224]
    assert skin[2, 2, :3].tolist() == [24, 20, 36] and skin[5, 5, :3].tolist() == [240, 220, 180]
    frozen = rgba(tmp_path / "var" / "frozen" / "walk_0.png")
    tint = fp.variant_colors(palette, "frozen")
    index = fp.nearest_index(sprite, palette)
    assert np.array_equal(frozen[visible][:, :3], tint[index][visible])
    assert len({tuple(c) for c in frozen[visible][:, :3]}) == 4, "one frozen colour per palette colour"
    qa = json.loads((tmp_path / "var" / "variants-qa.json").read_text(encoding="utf-8"))
    assert_valid_contract(qa, "common", "qaEnvelope", skill="generate2dsprite")
    check_file_refs(qa, tmp_path / "var")
    assert qa["status"] == "pass" and set(qa["luts"]) == set(baked["variants"])
    ok("luts", "--palette", tmp_path / "palette.json", "--skin-map", tmp_path / "skin-blue.json",
       "--output-dir", tmp_path / "luts")
    texture = rgba(tmp_path / "luts" / "luts.png")
    rows = json.loads((tmp_path / "luts" / "luts.json").read_text(encoding="utf-8"))["rows"]
    for row, name in enumerate(rows[1:], start=1):
        baked_frame = rgba(tmp_path / "var" / name / "walk_0.png")
        assert np.array_equal(baked_frame[visible][:, :3], texture[row][index][visible][:, :3]), name
    snapped = ok("variants", "--palette", tmp_path / "palette.json", "--input", frames, "--variant", "frozen",
                 "--snap", "--output-dir", tmp_path / "snapped")
    assert snapped["variants"] == ["frozen"]
    assert fp.fit_report(tmp_path / "snapped" / "frozen" / "walk_0.png", palette)["off_palette_px"] == 0
    raw = write_frames(tmp_path / "raw", [gradient_frame((40, 30, 90), (230, 200, 120))])
    assert "not on the palette" in fails("variants", "--palette", tmp_path / "palette.json", "--input", raw,
                                         "--output-dir", tmp_path / "bad")
    ok("variants", "--palette", tmp_path / "palette.json", "--input", raw, "--quantize", "--variant", "hitflash",
       "--output-dir", tmp_path / "quantized")
    assert "at least one" in fails("variants", "--palette", tmp_path / "palette.json", "--input", frames,
                                   "--variant", "--output-dir", tmp_path / "bad")
    assert nothing_published(tmp_path, "bad")


def test_luts_document_and_textures(tmp_path):
    palette, _ = _on_palette_sprite()
    fp.write_palette(palette.replace(transparent_index=1), tmp_path / "palette.json")
    summary = ok("luts", "--palette", tmp_path / "palette.json", "--flash-color", "#fff0f0",
                 "--output-dir", tmp_path / "luts")
    assert summary["rows"] == ["identity", "hitflash", "frozen", "silhouette"] and summary["palettize"]
    document = json.loads((tmp_path / "luts" / "luts.json").read_text(encoding="utf-8"))
    assert amended_errors(document, "palette_luts_v1") == []
    assert document["palette"]["path"] == "../palette.json"
    assert document["palette"]["sha256"] == fc.sha256_file(tmp_path / "palette.json")
    assert document["image"]["sha256"] == fc.sha256_file(tmp_path / "luts" / "luts.png")
    assert document["luts"]["hitflash"]["colors"] == ["#fff0f0", "#00000000", "#fff0f0", "#fff0f0"]
    texture = rgba(tmp_path / "luts" / "luts.png")
    assert texture.shape == (4, 256, 4) and texture[:, 1, 3].max() == 0 and texture[:, 4:, 3].max() == 0
    strip = rgba(tmp_path / "luts" / "palettize-32.png")
    assert strip.shape == (32, 1024, 4)
    assert {tuple(c) for c in strip[..., :3].reshape(-1, 3)} <= set(palette.colors) - {palette.colors[1]}
    ok("luts", "--palette", tmp_path / "palette.json", "--variant", "--palettize-size", "0",
       "--output-dir", tmp_path / "plain")
    assert sorted(path.name for path in (tmp_path / "plain").iterdir()) == ["luts.json", "luts.png"]
    broken = dict(document, luts={"identity": {"kind": "sparkle", "colors": ["#000000"]}})
    assert amended_errors(broken, "palette_luts_v1"), "the requested schema rejects unknown LUT kinds"


# --------------------------------------------------------------------------- quantize-seq

def noisy_frames(folder: Path, count: int = 8, seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:40, 0:40].astype(np.float64)
    radius = np.hypot(x - 19.5, (y - 19.5) * 1.15)
    shade = np.clip((x + y) / 80.0, 0.0, 1.0)
    base = np.zeros((40, 40, 4))
    base[..., 0], base[..., 1], base[..., 2] = 60 + 170 * shade, 90 + 110 * (1 - shade), 140 + 60 * np.sin(x / 5)
    base[..., 3] = np.clip((16.8 - radius) * 0.8 + 0.5, 0.0, 1.0) * 255
    frames = []
    for _ in range(count):
        frame = base.copy()
        frame[..., :3] += rng.normal(0.0, 3.0, frame[..., :3].shape)
        frame[..., 3] += rng.normal(0.0, 10.0, frame[..., 3].shape) * (frame[..., 3] > 0)
        frames.append(np.clip(np.floor(frame + 0.5), 0, 255).astype(np.uint8))
    return write_frames(folder, frames, "frame")


def test_quantize_seq_cli_pingpong_and_report(tmp_path):
    frames = noisy_frames(tmp_path / "frames")
    summary = ok("quantize-seq", "--input", frames, "--colors", "16", "--loop-policy", "pingpong", "--indexed",
                 "--output-dir", tmp_path / "clip")
    assert summary["frames"] == 8 and summary["loop_policy"] == "pingpong"
    assert summary["noise_flip_reduction"] >= 0.4
    names = sorted((p.name for p in (tmp_path / "clip").glob("frame_*.png")), key=lambda n: int(n[6:-4]))
    assert names == [f"frame_{i}.png" for i in range(8)], "forward frames only; the clip plays them back"
    palette = fp.read_palette(tmp_path / "clip" / "palette.json")
    for name in names:
        report = fp.fit_report(tmp_path / "clip" / name, palette)
        assert report["off_palette_px"] == 0 and report["partial_alpha_px"] == 0
        assert np.array_equal(rgba(tmp_path / "clip" / "indexed" / name), rgba(tmp_path / "clip" / name))
    qa = json.loads((tmp_path / "clip" / "quantize-qa.json").read_text(encoding="utf-8"))
    assert_valid_contract(qa, "common", "qaEnvelope", skill="generate2dsprite")
    check_file_refs(qa, tmp_path / "clip")
    assert qa["loop_policy"] == "pingpong" and any("pingpong" in note for note in qa["notProven"])
    assert qa["flips"]["hysteresis"]["noise_flip"] < qa["flips"]["per_frame_nearest"]["noise_flip"]
    expected = fp.quantize_sequence([rgba(frames / f"frame_{i}.png") for i in range(8)], palette, pingpong=True)
    for index, name in zip(expected, names):
        assert np.array_equal(rgba(tmp_path / "clip" / name), fp.render_indices(index, palette))


def test_quantize_seq_lock_must_cover_the_whole_clip(tmp_path):
    frames = noisy_frames(tmp_path / "frames", count=3)
    palette = tmp_path / "palette.json"
    fp.write_palette(fp.build_palette([rgba(path) for path in sorted(frames.iterdir())], 8), palette)
    ok("lock", "--palette", palette, "--source", frames / "frame_0.png", "--output-dir", tmp_path / "lock")
    message = fails("quantize-seq", "--palette", tmp_path / "lock" / "palette.json", "--lock",
                    tmp_path / "lock" / "palette-lock.json", "--input", frames, "--output-dir", tmp_path / "bad")
    assert "mixes locked and unlocked frames" in message
    ok("lock", "--palette", palette, "--source", frames, "--output-dir", tmp_path / "lock_all")
    summary = ok("quantize-seq", "--palette", tmp_path / "lock_all" / "palette.json", "--lock",
                 tmp_path / "lock_all" / "palette-lock.json", "--input", frames, "--loop-policy", "oneshot",
                 "--output-dir", tmp_path / "clip")
    qa = json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))
    assert qa["locked"] is True and qa["loop_policy"] == "oneshot"
    assert "alpha-band needs" in fails("quantize-seq", "--palette", palette, "--input", frames, "--alpha-band",
                                       "0.7", "0.2", "--output-dir", tmp_path / "bad")
    assert nothing_published(tmp_path, "bad")


# --------------------------------------------------------------------------- integration conventions (D26-D29)

def test_cli_conventions_after_integration(tmp_path):
    """D26: usage errors exit 2; D27: a bug is one ``error: internal error (...)`` line with exit 1; D28: a JSON
    skin map written with a UTF-8 BOM (Windows PowerShell 5.1) is read; D29: QA envelopes record the package
    version; D30: fileRefs come from forge_core."""
    import contextlib
    import io
    from unittest import mock

    palette, sprite = _on_palette_sprite()
    fp.write_palette(palette, tmp_path / "palette.json")
    frames = write_frames(tmp_path / "frames", [sprite])
    (tmp_path / "skin-bom.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"#c8783c": "#3c78c8"}).encode("utf-8"))
    baked = ok("variants", "--palette", tmp_path / "palette.json", "--input", frames, "--variant", "hitflash",
               "--skin-map", tmp_path / "skin-bom.json", "--output-dir", tmp_path / "var")
    assert baked["variants"] == ["hitflash", "skin-skin-bom"]
    qa = json.loads((tmp_path / "var" / "variants-qa.json").read_text(encoding="utf-8"))
    assert qa["tool"] == {"name": "palette_tool", "version": fc.FORGE_PACKAGE_VERSION} == {
        "name": "palette_tool", "version": "0.4.0"}
    check_file_refs(qa, tmp_path / "var")
    (tmp_path / "broken.json").write_text("{broken", encoding="utf-8")
    assert fails("variants", "--palette", tmp_path / "palette.json", "--input", frames, "--skin-map",
                 tmp_path / "broken.json", "--output-dir", tmp_path / "bad").startswith(
        "error: skin map broken.json is not valid JSON")
    usage = tool("build", "--input", frames, "--colors", "many", "--output-dir", tmp_path / "usage")
    assert usage.returncode == 2 and usage.stderr.startswith("usage:") and "error: argument --colors" in usage.stderr
    module = load_script("generate2dsprite", "palette_tool")
    stderr = io.StringIO()
    with mock.patch.dict(module.COMMANDS, {"build": mock.Mock(side_effect=KeyError("boom"))}), \
         contextlib.redirect_stderr(stderr):
        status = module.main(["build", "--input", str(frames), "--colors", "4", "--output-dir", str(tmp_path / "x")])
    assert (status, stderr.getvalue()) == (1, "error: internal error (KeyError: 'boom')\n")
    assert nothing_published(tmp_path, "bad") and nothing_published(tmp_path, "usage") and nothing_published(tmp_path, "x")
