"""CLI tests for skills/generate2dsprite/scripts/pixel_reduce.py (module B04-palette-pixel, task B04-T4).

Synthetic upscales throughout, plus the provenance-tracked fox sheet (real
image-model "pixel art" with 16k+ colours and no grid; roadmap P2-3).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from forge_testutils import assert_cli_help, assert_valid_contract, load_script, real_fixture, run_cli, script_path

TOOL = script_path("generate2dsprite", "pixel_reduce")
fp = load_script("generate2dsprite", "forge_palette")
fc = load_script("generate2dsprite", "forge_core")


def reduce(*args):
    return run_cli([TOOL, *map(str, args)])


def ok(*args) -> dict:
    result = reduce(*args)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].isascii() and result.stderr == ""
    return json.loads(lines[0])


def fails(*args) -> str:
    result = reduce(*args)
    assert result.returncode == 1, (result.stdout, result.stderr)
    lines = result.stderr.strip().splitlines()
    assert result.stdout == "" and len(lines) == 1 and lines[0].startswith("error: "), result.stderr
    return lines[0]


def nothing_published(parent: Path, name: str) -> bool:
    return not (parent / name).exists() and not list(parent.glob(f".{name}.stage-*"))


def logical_art() -> np.ndarray:
    """20 x 16 logical sprite: transparency, three regions and two single-pixel details."""
    art = np.zeros((16, 20, 4), np.uint8)
    art[2:14, 3:17] = (200, 80, 40, 255)
    art[4:8, 5:9] = (30, 30, 60, 255)
    art[9:12, 10:16] = (240, 220, 120, 255)
    art[2, 3] = (20, 140, 60, 255)
    art[13, 16] = (90, 90, 200, 255)
    return art


def upscale(art: np.ndarray, scale: int, pad=((2, 1), (3, 2))) -> np.ndarray:
    return np.pad(np.repeat(np.repeat(art, scale, axis=0), scale, axis=1), tuple(pad) + ((0, 0),))


def save(path: Path, pixels: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fc.save_png(pixels, path)
    return path


def rgba(path: Path) -> np.ndarray:
    return np.asarray(fc.load_rgba(path)[0])


def qa_of(folder: Path, *inputs: Path) -> dict:
    """The validated QA document; outputs are checked by path, inputs by content (an input on another
    drive is recorded by file name only, as the fileRef contract requires)."""
    document = json.loads((folder / "pixel-reduce-qa.json").read_text(encoding="utf-8"))
    assert_valid_contract(document, "common", "qaEnvelope", skill="generate2dsprite")
    for reference in document["outputs"]:
        assert reference["sha256"] == fc.sha256_file(folder / reference["path"])
    known = {fc.sha256_file(path) for path in inputs}
    for reference in document["inputs"]:
        assert not Path(reference["path"]).is_absolute() and ":" not in reference["path"]
        assert not inputs or reference["sha256"] in known
    return document


def statuses(document: dict) -> dict[str, str]:
    return {item["id"]: item["status"] for item in document["checks"]}


# --------------------------------------------------------------------------- the three standard CLI tests

def test_help_works_on_narrow_consoles():
    assert_cli_help("generate2dsprite", "pixel_reduce")


def test_refuses_an_existing_output_dir(tmp_path):
    source = save(tmp_path / "hero.png", upscale(logical_art(), 6))
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep.txt").write_text("mine", encoding="utf-8")
    assert "already exists" in fails("--input", source, "--output-dir", tmp_path / "out")
    assert [path.name for path in (tmp_path / "out").iterdir()] == ["keep.txt"]
    assert not list(tmp_path.glob(".out.stage-*"))


def test_no_clean_grid_publishes_nothing(tmp_path):
    painted = np.random.default_rng(3).integers(0, 256, (90, 120, 4)).astype(np.uint8)
    painted[..., 3] = 255
    source = save(tmp_path / "painted.png", painted)
    message = fails("--input", source, "--output-dir", tmp_path / "out")
    assert "no clean grid in painted.png" in message and "--force" in message
    assert nothing_published(tmp_path, "out")
    forced = fails("--input", source, "--force", "--period", "6", "--strict", "--output-dir", tmp_path / "out")
    assert "QA warned under --strict" in forced and "grid_clean" in forced
    assert nothing_published(tmp_path, "out")


# --------------------------------------------------------------------------- recovery

def test_synthetic_6x_image_recovers_exactly(tmp_path):
    """B04-T4: a nearest 6x upscale (offset by a 3, 2 px phase) reduces to its logical pixels exactly."""
    art = logical_art()
    source = save(tmp_path / "hero.png", upscale(art, 6))
    summary = ok("--input", source, "--upscale", "6", "--indexed", "--output-dir", tmp_path / "out")
    assert summary["status"] == "pass"
    assert summary["images"] == [{"file": "hero.png", "period": 6, "logical_size": [20, 16], "forced": False}]
    assert np.array_equal(rgba(tmp_path / "out" / "hero.png"), art)
    assert np.array_equal(rgba(tmp_path / "out" / "hero@6x.png"), np.repeat(np.repeat(art, 6, 0), 6, 1))
    assert np.array_equal(rgba(tmp_path / "out" / "indexed" / "hero.png"), art)
    palette = json.loads((tmp_path / "out" / "palette.json").read_text(encoding="utf-8"))
    assert_valid_contract(palette, "sprite", "palette_v1", skill="generate2dsprite")
    assert len(palette["colors"]) == 5, "art with few colours keeps exactly those colours"
    qa = qa_of(tmp_path / "out", source)
    assert qa["status"] == "pass" and set(statuses(qa).values()) == {"pass"}
    assert qa["images"]["hero.png"]["grid"]["phase"] == [3, 2]
    assert qa["images"]["hero.png"]["mixed_blocks"] == 0


@pytest.mark.parametrize("method", ["mode", "box", "center"])
def test_methods_recover_noisy_upscales_with_a_palette(tmp_path, method):
    art = logical_art()
    clean = upscale(art, 5, pad=((0, 0), (0, 0)))
    rng = np.random.default_rng(8)
    noisy = clean.astype(np.int16) + rng.integers(-10, 11, clean.shape)
    noisy[..., 3] = clean[..., 3]
    noisy = np.clip(noisy, 0, 255).astype(np.uint8)
    noisy[noisy[..., 3] == 0] = 0
    source = save(tmp_path / "noisy.png", noisy)
    fp.write_palette(fp.build_palette(art, 8), tmp_path / "palette.json")
    summary = ok("--input", source, "--palette", tmp_path / "palette.json", "--method", method,
                 "--output-dir", tmp_path / "out")
    assert summary["images"][0]["period"] == 5
    assert np.array_equal(rgba(tmp_path / "out" / "noisy.png"), art)
    assert not (tmp_path / "out" / "palette.json").exists(), "a given palette is used, never rewritten"
    assert qa_of(tmp_path / "out")["palette"]["built"] is False


def test_several_inputs_get_their_own_grid_and_one_palette(tmp_path):
    art = logical_art()
    big = save(tmp_path / "in" / "a.png", upscale(art, 6))
    small = save(tmp_path / "in" / "b.png", upscale(np.roll(art, 2, axis=1), 4, pad=((0, 0), (0, 0))))
    summary = ok("--input", big, small, "--output-dir", tmp_path / "out")
    assert [item["period"] for item in summary["images"]] == [6, 4]
    assert np.array_equal(rgba(tmp_path / "out" / "a.png"), art)
    assert np.array_equal(rgba(tmp_path / "out" / "b.png"), np.roll(art, 2, axis=1))


# --------------------------------------------------------------------------- the real fixture

def test_fox_fixture_reports_no_clean_grid(tmp_path):
    """B04-T4 / roadmap P2-3: the image-model fox sheet has no clean grid and is refused."""
    fox = real_fixture("raw-fox-run-v1.png")
    message = fails("--input", fox, "--output-dir", tmp_path / "out")
    assert "no clean grid in raw-fox-run-v1.png" in message
    score = float(message.split("score ")[1].split(" ")[0])
    assert score < 0.1
    assert nothing_published(tmp_path, "out")


def test_force_reduces_the_fox_with_a_warning(tmp_path):
    fox = real_fixture("raw-fox-run-v1.png")
    summary = ok("--input", fox, "--force", "--period", "8", "--output-dir", tmp_path / "out")
    assert summary["status"] == "warn"
    assert summary["images"] == [{"file": "raw-fox-run-v1.png", "period": 8, "logical_size": [192, 128],
                                  "forced": True}]
    qa = qa_of(tmp_path / "out", fox)
    assert statuses(qa)["grid_clean"] == "warn" and statuses(qa)["output_partial_alpha_px"] == "pass"
    assert any("forced" in note for note in qa["notProven"])
    logical = rgba(tmp_path / "out" / "raw-fox-run-v1.png")
    palette = fp.read_palette(tmp_path / "out" / "palette.json")
    assert len(palette) == 32 and fp.fit_report(logical, palette)["off_palette_px"] == 0
    assert set(np.unique(logical[..., 3]).tolist()) <= {0, 255}


# --------------------------------------------------------------------------- QC details

def test_a_given_period_that_detection_disagrees_with_warns(tmp_path):
    """Sparse art can score well at a wrong period; detection is consulted and the run warns."""
    source = save(tmp_path / "hero.png", upscale(logical_art(), 6))
    summary = ok("--input", source, "--period", "4", "--output-dir", tmp_path / "wrong")
    assert summary["status"] == "warn"
    check = next(item for item in qa_of(tmp_path / "wrong")["checks"] if item["id"] == "period_agrees")
    assert check["status"] == "warn" and check["value"] == {"given": 4, "detected": 6, "detected_score": 1.0}
    assert ok("--input", source, "--period", "6", "--output-dir", tmp_path / "right")["status"] == "pass"
    assert "period_agrees" in fails("--input", source, "--period", "4", "--strict", "--output-dir", tmp_path / "bad")
    assert nothing_published(tmp_path, "bad")


def test_visible_pixels_outside_whole_blocks_are_reported(tmp_path):
    image = upscale(logical_art(), 6)
    image[0:2, 40:50] = (200, 80, 40, 255)  # inside the 2 px partial strip above the first whole block row
    source = save(tmp_path / "edge.png", image)
    summary = ok("--input", source, "--output-dir", tmp_path / "out")
    assert summary["status"] == "warn"
    check = next(item for item in qa_of(tmp_path / "out")["checks"] if item["id"] == "lost_visible_px")
    assert check["status"] == "warn" and check["value"] == 20


def test_flat_images_and_bad_arguments(tmp_path):
    flat = np.zeros((30, 30, 4), np.uint8)
    flat[...] = (50, 60, 70, 255)
    source = save(tmp_path / "flat.png", flat)
    assert "no clean grid" in fails("--input", source, "--output-dir", tmp_path / "out")
    assert "no colour edges" in fails("--input", source, "--force", "--output-dir", tmp_path / "out")
    forced = ok("--input", source, "--force", "--period", "3", "--output-dir", tmp_path / "forced")
    assert forced["images"][0]["logical_size"] == [10, 10]
    assert "--phase needs --period" in fails("--input", source, "--phase", "1", "1", "--output-dir", tmp_path / "out")
    assert "smaller than one" in fails("--input", source, "--force", "--period", "31", "--output-dir", tmp_path / "out")
    result = reduce("--input", source, "--period", "1", "--output-dir", tmp_path / "out")
    assert result.returncode == 2 and "period" in result.stderr
    assert nothing_published(tmp_path, "out")
