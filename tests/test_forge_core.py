"""Tests for shared/forge_core.py (module A1-forge-core).

Audit finding and repro ids (2026-10-05) are named in the docstrings. All
fixtures are synthetic except the fox sheet, which is used only when A0's
provenance-tracked copy exists.
"""
from __future__ import annotations

import errno
import functools
import hashlib
import importlib.util
import json
import math
import os
import struct
import subprocess
import sys
import time
import zlib
from collections import deque
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "shared" / "forge_core.py"
VENDORED_SKILLS = ("generate2dsprite", "generate2dmap", "video2dsprite", "codeart2d")
FOX_FIXTURE = ROOT / "tests" / "fixtures" / "real" / "raw-fox-run-v1.png"


def _local_load_forge_core():
    """Load the canonical module by path (tests/forge_testutils.py arrives with A0)."""
    spec = importlib.util.spec_from_file_location("forge_core_under_test", CANONICAL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fc = _local_load_forge_core()


@pytest.fixture(params=["scipy", "numpy"])
def backend(request, monkeypatch):
    """Run a test once with scipy.ndimage and once with the forced numpy fallback."""
    if request.param == "numpy":
        monkeypatch.setenv("FORGE_CORE_NO_SCIPY", "1")
    else:
        pytest.importorskip("scipy")
        monkeypatch.delenv("FORGE_CORE_NO_SCIPY", raising=False)
    return request.param


# --------------------------------------------------------------------------- A1-T1 publishing

def _make_tree(stage: Path) -> None:
    (stage / "frames").mkdir()
    (stage / "frames" / "f0.png").write_bytes(b"frame-0")
    (stage / "pipeline-meta.json").write_text('{"ok": true}\n', encoding="utf-8")


def _snapshot(directory: Path) -> list[tuple[str, bytes | None]]:
    return sorted((path.relative_to(directory).as_posix(), path.read_bytes() if path.is_file() else None)
                  for path in directory.rglob("*"))


def _unsupported(code: int):
    def rename(source, target):
        raise OSError(code, os.strerror(code), str(target))
    return rename


@pytest.mark.parametrize("code", [errno.EINVAL, errno.ENOTSUP, errno.ENOSYS])
def test_publish_falls_back_on_einval(tmp_path, monkeypatch, code):
    """F-03: WSL drvfs rejects renameat2(RENAME_NOREPLACE) with EINVAL; publish must still succeed."""
    stage = tmp_path / ".out.stage-test"
    stage.mkdir()
    _make_tree(stage)
    expected = _snapshot(stage)
    monkeypatch.setattr(fc, "_rename_noreplace", _unsupported(code))
    final = tmp_path / "out"
    fc.publish_directory_no_replace(stage, final)
    assert _snapshot(final) == expected
    assert not stage.exists()


def test_publish_propagates_other_rename_errors(tmp_path, monkeypatch):
    stage = tmp_path / ".out.stage-test"
    stage.mkdir()
    _make_tree(stage)
    expected = _snapshot(stage)
    monkeypatch.setattr(fc, "_rename_noreplace", _unsupported(errno.EACCES))
    with pytest.raises(PermissionError):
        fc.publish_directory_no_replace(stage, tmp_path / "out")
    assert _snapshot(stage) == expected and not (tmp_path / "out").exists()


def test_publish_refuses_existing_and_racing_empty_dir(tmp_path, monkeypatch):
    """F-03: an existing output, even an empty directory created by a racing process, is never replaced."""
    stage = tmp_path / ".out.stage-test"
    stage.mkdir()
    _make_tree(stage)
    expected = _snapshot(stage)
    final = tmp_path / "out"
    final.mkdir()
    occupied = tmp_path / "occupied"
    occupied.write_text("theirs", encoding="utf-8")
    for target in (final, occupied):
        with pytest.raises(FileExistsError):
            fc.publish_directory_no_replace(stage, target)
    # The destination appears after the existence check: both publish paths must still refuse it.
    monkeypatch.setattr(fc.os.path, "lexists", lambda path: False)
    with pytest.raises(FileExistsError):
        fc.publish_directory_no_replace(stage, final)
    monkeypatch.setattr(fc, "_rename_noreplace", _unsupported(errno.EINVAL))
    with pytest.raises(FileExistsError):
        fc.publish_directory_no_replace(stage, final)
    monkeypatch.undo()
    assert _snapshot(stage) == expected
    assert list(final.iterdir()) == []
    assert occupied.read_text(encoding="utf-8") == "theirs"


def test_publish_fallback_restores_stage_on_failure(tmp_path, monkeypatch):
    stage = tmp_path / ".out.stage-test"
    stage.mkdir()
    _make_tree(stage)
    expected = _snapshot(stage)
    monkeypatch.setattr(fc, "_rename_noreplace", _unsupported(errno.EINVAL))
    real_rmdir = os.rmdir

    def failing_rmdir(path):
        if Path(path) == stage:
            raise OSError(errno.EBUSY, "busy", str(path))
        real_rmdir(path)

    monkeypatch.setattr(fc.os, "rmdir", failing_rmdir)
    with pytest.raises(OSError):
        fc.publish_directory_no_replace(stage, tmp_path / "out")
    monkeypatch.undo()
    assert _snapshot(stage) == expected
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("links", ["available", "oserror", "missing"])
def test_file_publish_without_hardlinks(tmp_path, monkeypatch, links):
    """F-03 sidecars: FAT/exFAT and some WSL or network mounts have no hard links; fall back to open('xb')."""
    source = tmp_path / "stage" / "scale-profile.json"
    source.parent.mkdir()
    source.write_bytes(b'{"version": 2}\n')
    if links == "oserror":
        monkeypatch.setattr(fc.os, "link", _unsupported(errno.EPERM))
    elif links == "missing":
        monkeypatch.delattr(fc.os, "link")
    target = tmp_path / "out" / "scale-profile.json"
    fc.publish_file_no_replace(source, target)
    assert target.read_bytes() == b'{"version": 2}\n'
    assert [path.name for path in target.parent.iterdir()] == ["scale-profile.json"]
    source.write_bytes(b"newer")
    with pytest.raises(FileExistsError):
        fc.publish_file_no_replace(source, target)
    assert target.read_bytes() == b'{"version": 2}\n'
    assert [path.name for path in target.parent.iterdir()] == ["scale-profile.json"]


def test_staged_output_cleans_on_exception(tmp_path):
    """Appendix D / roadmap P0-2: a strict-QC failure (any exception) publishes nothing and leaves no stage."""
    final = tmp_path / "out"
    with pytest.raises(RuntimeError, match="strict QC failed"):
        with fc.staged_output(final) as stage:
            (stage / "idle-1.png").write_bytes(b"partial")
            raise RuntimeError("strict QC failed")
    assert list(tmp_path.iterdir()) == []
    # A destination that appears while the job runs is left alone and the stage is removed.
    with pytest.raises(FileExistsError):
        with fc.staged_output(final) as stage:
            (stage / "ours.txt").write_text("ours", encoding="utf-8")
            final.mkdir()
            (final / "theirs.txt").write_text("theirs", encoding="utf-8")
    assert [path.name for path in tmp_path.iterdir()] == ["out"]
    assert [path.name for path in final.iterdir()] == ["theirs.txt"]


def test_staged_output_publishes_with_stable_relative_paths(tmp_path):
    source = tmp_path / "inputs" / "sheet.png"
    source.parent.mkdir()
    source.write_bytes(b"png")
    final = tmp_path / "runs" / "out"
    with fc.staged_output(final, prefix="sprite-") as stage:
        assert stage.parent == final.parent.resolve()
        assert stage.name.startswith(".sprite-out.stage-")
        fc.write_json(stage / "manifest.json", {"source": fc.portable_path(source, stage)})
    manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "../../inputs/sheet.png"
    assert (final / manifest["source"]).resolve() == source.resolve()
    assert [path.name for path in final.parent.iterdir()] == ["out"]
    with pytest.raises(FileExistsError):
        with fc.staged_output(final):
            pass


# --------------------------------------------------------------------------- A1-T2 image I/O

def _png16(path: Path, samples: np.ndarray, colour_type: int) -> None:
    """Hand-encode a 16-bit PNG (Pillow cannot write 16-bit RGB(A)); repro r10b."""
    height, width = samples.shape[:2]
    rows = samples.reshape(height, width, -1).astype(">u2")
    raw = b"".join(b"\0" + rows[y].tobytes() for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 16, colour_type, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw))
                     + chunk(b"IEND", b""))


def _palette_image(indices: np.ndarray, colours: list[tuple[int, int, int]]) -> Image.Image:
    """Palette image with a full 256-entry palette, so Pillow keeps 8 bits unless ``bits`` is given."""
    image = Image.frombytes("P", (indices.shape[1], indices.shape[0]), indices.astype(np.uint8).tobytes())
    image.putpalette([value for colour in colours for value in colour] + [0] * (3 * (256 - len(colours))))
    return image


INDICES = np.array([[0, 1, 2, 3], [3, 2, 1, 0]], np.uint8)
PALETTE = [(0, 0, 0), (255, 0, 0), (0, 200, 50), (20, 40, 250)]
PALETTE_ALPHA = [0, 128, 255, 255]


def _rgba_of_palette(alphas: list[int]) -> np.ndarray:
    lookup = np.array([colour + (alpha,) for colour, alpha in zip(PALETTE, alphas)], np.uint8)
    return lookup[INDICES]


def _case_rgba8(path: Path):
    pixels = np.array([[[10, 20, 30, 255], [99, 98, 97, 0]], [[1, 2, 3, 4], [250, 0, 250, 128]]], np.uint8)
    Image.fromarray(pixels).save(path)
    return pixels, "RGBA", 8, "none"


def _case_rgb8(path: Path):
    rgb = np.array([[[10, 20, 30], [40, 50, 60]]], np.uint8)
    Image.fromarray(rgb).save(path)
    return np.dstack([rgb, np.full((1, 2), 255, np.uint8)]), "RGB", 8, "RGB -> RGBA"


def _case_rgb_trns(path: Path):
    rgb = np.array([[[10, 20, 30], [4, 5, 6]]], np.uint8)
    Image.fromarray(rgb).save(path, transparency=(4, 5, 6))
    return np.array([[[10, 20, 30, 255], [4, 5, 6, 0]]], np.uint8), "RGB", 8, "RGB+tRNS -> RGBA"


def _case_palette_index_trns(path: Path):
    _palette_image(INDICES, PALETTE).save(path, transparency=0)
    return _rgba_of_palette([0, 255, 255, 255]), "P", 8, "P+tRNS -> RGBA"


def _case_palette_alpha_table(path: Path):
    _palette_image(INDICES, PALETTE).save(path, transparency=bytes(PALETTE_ALPHA))
    return _rgba_of_palette(PALETTE_ALPHA), "P", 8, "P+tRNS -> RGBA"


def _case_palette_2bit(path: Path):
    _palette_image(INDICES, PALETTE).save(path, transparency=0, bits=2)
    return _rgba_of_palette([0, 255, 255, 255]), "P", 2, "P (2-bit)+tRNS -> RGBA"


def _case_palette_4bit(path: Path):
    _palette_image(INDICES, PALETTE).save(path, bits=4)
    return _rgba_of_palette([255, 255, 255, 255]), "P", 4, "P (4-bit) -> RGBA"


def _case_grey(path: Path):
    grey = np.array([[0, 100, 200]], np.uint8)
    Image.fromarray(grey).save(path)
    return np.dstack([grey, grey, grey, np.full_like(grey, 255)]), "L", 8, "L -> RGBA"


def _case_grey_trns(path: Path):
    grey = np.array([[0, 100, 200]], np.uint8)
    Image.fromarray(grey).save(path, transparency=100)
    return np.dstack([grey, grey, grey, np.array([[255, 0, 255]], np.uint8)]), "L", 8, "L+tRNS -> RGBA"


def _case_grey_alpha(path: Path):
    pair = np.array([[[0, 0], [90, 128], [255, 255]]], np.uint8)
    Image.fromarray(pair).save(path)
    grey, alpha = pair[..., 0], pair[..., 1]
    return np.dstack([grey, grey, grey, alpha]), "LA", 8, "LA -> RGBA"


def _case_bilevel(path: Path):
    Image.fromarray(np.array([[0, 255, 0, 255]], np.uint8)).convert("1").save(path)
    grey = np.array([[0, 255, 0, 255]], np.uint8)
    return np.dstack([grey, grey, grey, np.full_like(grey, 255)]), "1", 1, "1 -> RGBA"


def _case_grey16_ramp(path: Path):
    """S16 / r10: a 16-bit grey PNG became pure white; the ramp must come back as its high bytes."""
    ramp = (np.arange(256, dtype=np.uint16) * 257).reshape(1, 256)
    values = np.vstack([ramp, np.full((1, 256), 8000, np.uint16)])
    _png16(path, values, 0)
    grey = (values >> 8).astype(np.uint8)
    assert grey[0].tolist() == list(range(256)) and grey[1, 0] == 31
    return np.dstack([grey, grey, grey, np.full_like(grey, 255)]), None, 16, ">> 8"


def _case_rgba16(path: Path):
    """r10b: 16-bit RGBA keeps its colours (high byte)."""
    samples = np.array([[[0x2A80, 0x5AFF, 0x9600, 0xFFFF], [0x00FF, 0x0100, 0xFF00, 0x7FFF]]], np.uint16)
    _png16(path, samples, 6)
    return (samples >> 8).astype(np.uint8), "RGBA", 16, ">> 8"


def _case_rgb16(path: Path):
    samples = np.array([[[0x2A80, 0x5AFF, 0x9600], [0x00FF, 0x0100, 0xFF00]]], np.uint16)
    _png16(path, samples, 2)
    rgb = (samples >> 8).astype(np.uint8)
    return np.dstack([rgb, np.full((1, 2), 255, np.uint8)]), "RGB", 16, ">> 8"


def _case_grey_alpha16(path: Path):
    samples = np.array([[[0x2A80, 0xFFFF], [0x9600, 0x7FFF]]], np.uint16)
    _png16(path, samples, 4)
    grey, alpha = (samples[..., 0] >> 8).astype(np.uint8), (samples[..., 1] >> 8).astype(np.uint8)
    return np.dstack([grey, grey, grey, alpha]), None, 16, ">> 8"


def _case_cmyk(path: Path):
    cmyk = np.array([[[0, 255, 255, 0], [0, 0, 0, 255], [0, 0, 0, 0]]], np.uint8)
    Image.frombytes("CMYK", (3, 1), cmyk.tobytes()).save(path.with_suffix(".tif"))
    path.with_suffix(".tif").replace(path)
    return np.array([[[255, 0, 0, 255], [0, 0, 0, 255], [255, 255, 255, 255]]], np.uint8), "CMYK", 8, "CMYK"


LOAD_CASES = {
    "rgba8": _case_rgba8, "rgb8": _case_rgb8, "rgb_trns": _case_rgb_trns,
    "palette_index_trns": _case_palette_index_trns, "palette_alpha_table": _case_palette_alpha_table,
    "palette_2bit": _case_palette_2bit, "palette_4bit": _case_palette_4bit,
    "grey": _case_grey, "grey_trns": _case_grey_trns, "grey_alpha": _case_grey_alpha, "bilevel": _case_bilevel,
    "grey16_ramp": _case_grey16_ramp, "rgba16": _case_rgba16, "rgb16": _case_rgb16,
    "grey_alpha16": _case_grey_alpha16, "cmyk": _case_cmyk,
}


@pytest.mark.parametrize("case", sorted(LOAD_CASES))
def test_load_rgba_matrix(tmp_path, case):
    """S23 / S16 / MAP-19: palette, low-bit, grey, 16-bit and CMYK inputs load as exact 8-bit RGBA."""
    path = tmp_path / f"{case}.png"
    expected, source_mode, bit_depth, conversion = LOAD_CASES[case](path)
    image, info = fc.load_rgba(path)
    assert image.mode == "RGBA"
    assert np.array_equal(np.asarray(image), expected)
    assert info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info["bytes"] == path.stat().st_size
    assert info["size"] == [expected.shape[1], expected.shape[0]]
    assert info["bit_depth"] == bit_depth
    assert info["frames"] == 1
    assert info["path"] == path.resolve().as_posix()
    if source_mode is not None:
        assert info["source_mode"] == source_mode
    assert conversion in info["conversion"]


def test_animated_rejected(tmp_path):
    """S16 / r10: animated PNG and GIF used to load frame 0 silently."""
    first = Image.new("RGBA", (8, 8), (40, 90, 150, 255))
    second = Image.new("RGBA", (8, 8), (200, 40, 40, 255))
    apng, gif = tmp_path / "walk.png", tmp_path / "walk.gif"
    first.save(apng, save_all=True, append_images=[second], duration=100)
    first.convert("RGB").save(gif, save_all=True, append_images=[second.convert("RGB")], duration=100)
    for path in (apng, gif):
        with pytest.raises(ValueError, match="animated"):
            fc.load_rgba(path)
    image, info = fc.load_rgba(apng, allow_animated=True)
    assert info["frames"] == 2 and "frame 0 of 2" in info["conversion"]
    assert np.asarray(image)[0, 0].tolist() == [40, 90, 150, 255]


def _png_chunks(data: bytes) -> list[bytes]:
    tags, offset = [], 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        tags.append(data[offset + 4:offset + 8])
        offset += 12 + length
    return tags


def test_save_png_deterministic_without_metadata(tmp_path):
    from PIL import PngImagePlugin

    pixels = np.array([[[10, 20, 30, 255], [99, 98, 97, 0]], [[1, 2, 3, 4], [250, 0, 250, 128]]], np.uint8)
    text = PngImagePlugin.PngInfo()
    text.add_text("Comment", "generator notes")
    source = tmp_path / "source.png"
    Image.fromarray(pixels).save(source, pnginfo=text, icc_profile=b"not-a-real-profile")
    with Image.open(source) as loaded:
        loaded.load()
        assert "icc_profile" in loaded.info
        fc.save_png(loaded, tmp_path / "a.png")
    fc.save_png(pixels, tmp_path / "b.png")
    first, second = (tmp_path / "a.png").read_bytes(), (tmp_path / "b.png").read_bytes()
    assert first == second
    assert _png_chunks(first) == [b"IHDR", b"IDAT", b"IEND"]
    saved = np.asarray(Image.open(tmp_path / "a.png"))
    assert saved[0, 1].tolist() == [0, 0, 0, 0]
    assert np.array_equal(saved[pixels[..., 3] > 0], pixels[pixels[..., 3] > 0])
    fc.save_png(pixels, tmp_path / "raw.png", zero_transparent_rgb=False)
    assert np.array_equal(np.asarray(Image.open(tmp_path / "raw.png")), pixels)
    with pytest.raises(ValueError, match="mode P"):
        fc.save_png(Image.new("P", (2, 2)), tmp_path / "indexed.png")


# --------------------------------------------------------------------------- A1-T3 components

def _bfs_labels(mask: np.ndarray, connectivity: int) -> tuple[np.ndarray, int]:
    """Reference labelling: breadth-first search seeded in raster order."""
    height, width = mask.shape
    steps = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if connectivity == 8:
        steps += [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    grid = mask.tolist()
    labels = [[0] * width for _ in range(height)]
    count = 0
    for y in range(height):
        for x in range(width):
            if grid[y][x] and not labels[y][x]:
                count += 1
                labels[y][x] = count
                queue = deque([(y, x)])
                while queue:
                    cy, cx = queue.popleft()
                    for dy, dx in steps:
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < height and 0 <= nx < width and grid[ny][nx] and not labels[ny][nx]:
                            labels[ny][nx] = count
                            queue.append((ny, nx))
    return np.array(labels, np.int32).reshape(height, width), count


@functools.lru_cache(maxsize=1)
def _reference_masks() -> list[tuple[np.ndarray, dict[int, tuple[np.ndarray, int]]]]:
    rng = np.random.default_rng(20261005)
    cases = []
    for _ in range(400):
        shape = (int(rng.integers(1, 41)), int(rng.integers(1, 41)))
        mask = rng.random(shape) < rng.uniform(0.15, 0.75)
        cases.append((mask, {connectivity: _bfs_labels(mask, connectivity) for connectivity in (4, 8)}))
    return cases


def test_components_match_reference_bfs(backend):
    """S03 / S04 / MAP-06: 400 random masks x {4, 8}-connectivity match a BFS exactly, labels included."""
    for index, (mask, references) in enumerate(_reference_masks()):
        for connectivity, (expected, count) in references.items():
            labels, found = fc.label_components(mask, connectivity)
            assert found == count, (index, connectivity)
            assert labels.dtype == np.int32 and np.array_equal(labels, expected), (index, connectivity)
            if index % 20:
                continue
            height, width = mask.shape
            areas = np.bincount(expected.ravel(), minlength=count + 1)
            components = fc.connected_components(mask, connectivity=connectivity, with_masks=True)
            assert [item["label"] for item in components] == sorted(range(1, count + 1),
                                                                      key=lambda label: -areas[label])
            for item in components:
                ys, xs = np.nonzero(expected == item["label"])
                box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
                assert item["area"] == areas[item["label"]] and item["bbox"] == box
                assert item["touches_edge"] == (box[0] == 0 or box[1] == 0 or box[2] == width or box[3] == height)
                assert np.array_equal(item["mask"], expected[box[1]:box[3], box[0]:box[2]] == item["label"])


def test_diagonal_stroke_is_one_component_8conn(backend):
    """S04 / r01, MAP-06 / repro_04: 1-px diagonal strokes stay whole under 8-connectivity."""
    sword = np.zeros((40, 40), np.uint8)
    sword[10:20, 10:20] = 255
    for step in range(11):
        sword[20 + step, 20 + step] = 255
    largest = fc.connected_components(sword)[0]
    assert len(fc.connected_components(sword)) == 1 and largest["area"] == 111
    assert len(fc.connected_components(sword, connectivity=4)) == 12
    spear = np.zeros((40, 40), np.uint8)
    spear[2:8, 2:8] = 255
    for step in range(15):
        spear[5 + step, 20 + step] = 255
    areas = [item["area"] for item in fc.connected_components(spear, min_area=2)]
    assert areas == [36, 15]
    assert [item["area"] for item in fc.connected_components(spear, min_area=2, connectivity=4)] == [36]


@functools.lru_cache(maxsize=1)
def _sheet_alpha(size: int = 2048) -> np.ndarray:
    """A 4x4 sprite sheet: elliptical bodies with holes and diagonal weapons, plus 0.2% speckle."""
    rng = np.random.default_rng(7)
    cell = size // 4
    yy, xx = np.mgrid[:cell, :cell] - cell // 2
    alpha = np.zeros((size, size), np.uint8)
    for row in range(4):
        for col in range(4):
            rx, ry = (int(value) for value in rng.integers(cell // 6, cell // 3, 2))
            body = (xx / rx) ** 2 + (yy / ry) ** 2 < 1
            hole = (xx / (rx / 3)) ** 2 + (yy / (ry / 3)) ** 2 < 1
            view = alpha[row * cell:(row + 1) * cell, col * cell:(col + 1) * cell]
            view[body & ~hole] = 255
            steps = np.arange(min(cell // 3, cell // 2 - rx))
            view[cell // 2 + steps, cell // 2 + rx + steps] = 255
    alpha[rng.random((size, size)) < 0.002] = 3
    alpha.setflags(write=False)
    return alpha


@pytest.mark.perf
def test_components_perf_2048(backend):
    """S03 / MAP-13: a 2048^2 sheet labels in <= 0.5 s with scipy, <= 3 s with the numpy fallback."""
    budget = 0.5 if backend == "scipy" else 3.0
    alpha = _sheet_alpha()
    noise = np.random.default_rng(1).random((2048, 2048)) < 0.5
    for run in (lambda: fc.connected_components(alpha), lambda: fc.label_components(noise)):
        started = time.perf_counter()
        run()
        assert time.perf_counter() - started <= budget


def test_subject_mask_and_bbox_threshold_semantics():
    """S01: geometry counts alpha > threshold; threshold 0 is the legacy alpha > 0 rule."""
    alpha = np.array([[0, 1, 16, 17], [0, 0, 32, 33]], np.uint8)
    assert fc.subject_mask(alpha).tolist() == [[False, False, False, True], [False, False, True, True]]
    assert fc.subject_mask(alpha, 0).sum() == 5
    assert fc.subject_mask(alpha, fc.BODY_ALPHA_THRESHOLD).tolist() == [[False] * 4, [False, False, False, True]]
    assert fc.subject_bbox(alpha) == (2, 0, 4, 2)
    assert fc.subject_bbox(alpha, 0) == (1, 0, 4, 2)
    assert fc.subject_bbox(np.zeros((3, 3), np.uint8)) is None
    rgba = np.zeros((2, 4, 4), np.uint8)
    rgba[..., 3] = alpha
    assert fc.subject_bbox(Image.fromarray(rgba)) == (2, 0, 4, 2)
    boolean = np.zeros((3, 3), bool)
    boolean[1, 1] = True
    assert fc.subject_bbox(boolean) == (1, 1, 2, 2)
    assert (fc.ALPHA_GEOMETRY_THRESHOLD, fc.BODY_ALPHA_THRESHOLD, fc.FORGE_CORE_API_VERSION) == (16, 32, "1")


# --------------------------------------------------------------------------- A1-T4 hygiene

@pytest.mark.skipif(not FOX_FIXTURE.exists(), reason="fox fixture arrives with A0-contracts (A0-T7)")
def test_hygiene_on_fox_fixture():
    """DOC-04: the host fox sheet carries 67,907 alpha 1-4 haze pixels; floor 4 removes exactly those."""
    image, _ = fc.load_rgba(FOX_FIXTURE)
    before = np.asarray(image)
    haze = (before[..., 3] >= 1) & (before[..., 3] <= 4)
    assert int(haze.sum()) == 67_907
    cleaned, report = fc.alpha_hygiene(image, mode="floor", floor=4)
    after = np.asarray(cleaned)
    assert report["floor_px"] == 67_907
    assert int(((after[..., 3] >= 1) & (after[..., 3] <= 4)).sum()) == 0
    assert np.array_equal(after[~haze], before[~haze])
    _, both = fc.alpha_hygiene(image, mode="both")
    assert both["floor_px"] == 67_907 and both["max_removed_alpha"] < 32


def _hygiene_scene() -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Body with a soft edge, a faint glow 2 px off it, a far faint island, a far solid speck and haze."""
    rng = np.random.default_rng(4)
    parts = {name: np.zeros((64, 64), bool) for name in ("body", "edge", "glow", "island", "speck")}
    parts["edge"][19:46, 19:46] = True
    parts["body"][20:45, 20:45] = True
    parts["edge"] &= ~parts["body"]
    parts["glow"][17, 24:41] = True  # row 18 stays empty: not 8-connected, Chebyshev distance 2
    parts["island"][3:6, 3:6] = True
    parts["speck"][58:60, 58:60] = True
    occupied = np.zeros((64, 64), bool)
    for mask in parts.values():
        occupied |= mask
    parts["haze"] = (rng.random((64, 64)) < 0.08) & ~occupied
    rgba = np.zeros((64, 64, 4), np.uint8)
    rgba[parts["body"]] = (200, 80, 40, 255)
    rgba[parts["edge"]] = (200, 80, 40, 64)
    rgba[parts["glow"]] = (255, 240, 200, 20)
    rgba[parts["island"]] = (255, 240, 200, 20)
    rgba[parts["speck"]] = (30, 30, 30, 200)
    count = int(parts["haze"].sum())
    rgba[parts["haze"], :3] = rng.integers(0, 256, (count, 3))
    rgba[parts["haze"], 3] = rng.integers(1, 5, count)
    return rgba, parts


def test_hygiene_preserves_visible_pixels():
    """report v2 P1-4: floor and detached removal touch only haze and faint islands, never visible art."""
    rgba, parts = _hygiene_scene()
    floored, report = fc.alpha_hygiene(rgba, mode="floor", floor=4)
    floored = np.asarray(floored)
    assert report["floor_px"] == int(parts["haze"].sum()) and report["detached_px"] == 0
    assert np.array_equal(floored[~parts["haze"]], rgba[~parts["haze"]])
    assert not floored[parts["haze"]].any()

    cleaned, report = fc.alpha_hygiene(rgba, mode="both", floor=4, solid_min=32, attach_radius=2)
    cleaned = np.asarray(cleaned)
    removed = parts["haze"] | parts["island"]
    assert report["detached_components"] == 1 and report["detached_px"] == 9
    assert report["max_removed_alpha"] == 20
    assert np.array_equal(cleaned[~removed], rgba[~removed])
    assert not cleaned[removed].any()

    # A faint glow one pixel off the art is kept only while it lies within attach_radius of solid pixels.
    _, tight = fc.alpha_hygiene(rgba, mode="both", attach_radius=0)
    assert tight["detached_px"] == 9 + int(parts["glow"].sum())
    untouched, report = fc.alpha_hygiene(rgba, mode="none")
    assert np.array_equal(np.asarray(untouched), rgba) and report["floor_px"] == report["detached_px"] == 0
    with pytest.raises(ValueError):
        fc.alpha_hygiene(rgba, mode="aggressive")


# --------------------------------------------------------------------------- A1-T5 anchors and grids

def _walk_frame(phase: int) -> np.ndarray:
    """A 300x200 walker: the planted foot always ends on row 239 while the other foot lifts."""
    mask = np.zeros((300, 200), bool)
    mask[60:180, 80:120] = True  # torso
    mask[180:240, 82:96] = True  # planted leg
    mask[230:240, 70:96] = True  # planted foot, bottom row 239
    lift = 8 * phase
    mask[180:240 - lift, 104:118] = True  # swinging leg
    mask[230 - lift:240 - lift, 104:130] = True  # swinging foot
    return mask


def test_ground_row_bottom_edge():
    """S14 / r20: the ground is the bottom edge of the lowest stable row, 239 -> 240, with no jitter."""
    frames = [_walk_frame(phase) for phase in range(4)]
    assert [fc.ground_row(mask) for mask in frames] == [240, 240, 240, 240]
    assert {fc.anchor_from_mask(mask, mode)[1] for mask in frames
            for mode in ("feet", "stance", "bbox")} == {240.0}
    legacy = [float(np.percentile(np.nonzero(mask)[0], 98)) for mask in frames]
    assert max(legacy) < 240 and len(set(legacy)) > 1
    tipped = frames[0].copy()
    tipped[240:246, 150] = True
    assert fc.ground_row(tipped) == 246
    assert fc.ground_row(tipped, min_run=2) == 240
    with pytest.raises(ValueError):
        fc.ground_row(np.zeros((4, 4), bool))


def _caped_character() -> np.ndarray:
    mask = np.zeros((80, 60), bool)
    mask[20:50, 20:41] = True  # torso
    mask[50:70, 22:27] = True  # left leg
    mask[50:70, 33:38] = True  # right leg
    mask[22:46, 6:20] = True  # cape trailing on one side, above the support band
    return mask


def test_stance_anchor_zero_turn_slide():
    """study exp_anchor_turnslide: a stance anchor mirrors with the feet, so turning slides 0 px."""
    mask = _caped_character()
    mirrored = mask[:, ::-1]
    width = mask.shape[1]
    feet = (22 + 38) / 2
    slides = {}
    for mode in fc.ANCHOR_MODES:
        x, _ = fc.anchor_from_mask(mask, mode)
        x_mirrored, _ = fc.anchor_from_mask(mirrored, mode)
        slides[mode] = abs((feet - x) - ((width - feet) - x_mirrored))
    assert slides["stance"] == 0.0
    assert slides["bbox"] > 2 and slides["center"] > 2 and slides["centroid"] > 2


def test_anchor_modes_and_subject_bbox():
    """S15 / r14: a spear held low drags the feet anchor unless it is restricted to the subject box."""
    cell = np.zeros((128, 128), bool)
    cell[30:110, 4:24] = True
    assert fc.anchor_from_mask(cell, "feet") == (14.0, 110.0)
    assert fc.anchor_from_mask(cell, "stance") == (14.0, 110.0)
    assert fc.anchor_from_mask(cell, "bbox") == (14.0, 110.0)
    assert fc.anchor_from_mask(cell, "center") == (14.0, 70.0)
    assert fc.anchor_from_mask(cell, "centroid") == (14.0, 70.0)
    cell[100:103, 24:112] = True
    assert fc.anchor_from_mask(cell, "feet")[0] > 24
    assert fc.anchor_from_mask(cell, "feet", subject_bbox=(4, 30, 24, 110)) == (14.0, 110.0)
    with pytest.raises(ValueError):
        fc.anchor_from_mask(cell, "hips")
    with pytest.raises(ValueError):
        fc.anchor_from_mask(np.zeros((5, 5), bool))


def test_rounded_grid_boxes_1254_4x4():
    """MAP-04 / DOC-03 / repro_09: a 1254^2 host sheet slices 4x4 with no dropped or repeated pixel."""
    boxes = fc.rounded_grid_boxes(1254, 1254, 4, 4)
    assert len(boxes) == 16
    assert boxes[0] == (0, 0, 314, 314) and boxes[-1] == (941, 941, 1254, 1254)
    assert [box[2] - box[0] for box in boxes[:4]] == [314, 313, 314, 313]
    assert [box[3] - box[1] for box in boxes[::4]] == [314, 313, 314, 313]
    coverage = np.zeros((1254, 1254), np.int32)
    for x0, y0, x1, y1 in boxes:
        coverage[y0:y1, x0:x1] += 1
    assert (coverage == 1).all()
    assert fc.rounded_grid_boxes(1024, 512, 2, 4) == [
        (col * 256, row * 256, (col + 1) * 256, (row + 1) * 256) for row in range(2) for col in range(4)]
    assert {box[2] - box[0] for box in fc.rounded_grid_boxes(1024, 1024, 3, 3)} == {341, 342}
    with pytest.raises(ValueError):
        fc.rounded_grid_boxes(3, 3, 4, 4)


def test_pad_to_grid_records_offset():
    """DOC-03: lossless padding instead of rejecting a sheet whose size does not divide."""
    sheet = Image.new("RGBA", (1254, 941), (255, 0, 255, 255))
    sheet.putpixel((0, 0), (1, 2, 3, 255))
    padded, offset = fc.pad_to_grid(sheet, 2, 4, fill=(255, 0, 255, 255))
    assert padded.size == (1256, 942) and offset == (1, 0)
    assert padded.getpixel((1, 0)) == (1, 2, 3, 255) and padded.getpixel((0, 0)) == (255, 0, 255, 255)
    assert padded.crop((1, 0, 1255, 941)).tobytes() == sheet.tobytes()
    same, offset = fc.pad_to_grid(sheet.crop((0, 0, 1252, 940)), 2, 4)
    assert same.size == (1252, 940) and offset == (0, 0)


def test_integer_scale():
    """S06: nearest-neighbour scaling is only defined for whole-pixel factors."""
    assert fc.integer_scale(3) == 3 and fc.integer_scale(2.0000001) == 2
    for bad in (2.5, 0.5, 0, -2, math.nan, math.inf):
        with pytest.raises(ValueError):
            fc.integer_scale(bad)


# --------------------------------------------------------------------------- A1-T6 resampling

def _striped_character() -> np.ndarray:
    """64x64 RGBA character with 2-px stripes (r07), feet on row 57, transparent margins."""
    rgba = np.zeros((64, 64, 4), np.uint8)
    yy, xx = np.mgrid[:64, :64]
    body = (xx >= 22) & (xx < 42) & (yy >= 20) & (yy < 58)
    rgba[body, 0] = np.where((xx[body] // 2) % 2 == 0, 200, 40)
    rgba[body, 1] = np.where((yy[body] // 3) % 2 == 0, 160, 60)
    rgba[body, 2] = 90
    rgba[body, 3] = 255
    return rgba


def _embed(frame: np.ndarray, size: tuple[int, int], offset: tuple[int, int]) -> np.ndarray:
    canvas = np.zeros((size[1], size[0], 4), np.uint8)
    canvas[offset[1]:offset[1] + frame.shape[0], offset[0]:offset[0] + frame.shape[1]] = frame
    return canvas


def test_same_rest_frame_identical_through_two_clips():
    """S05 / r07: the rest pose cropped differently by two clips resamples to identical bytes."""
    rest = _striped_character()
    raised = rest.copy()
    raised[2:20, 10:16] = (220, 220, 60, 255)  # an arm raised above-left widens the clip's bbox
    clip_a = [_embed(rest, (90, 80), (13, 9)), _embed(raised, (90, 80), (13, 9))]
    clip_b = [rest[3:, 7:], rest[3:, 7:]]
    for resampler, scale in (("lanczos", 0.41), ("box", 0.25), ("lanczos", 1.7)):
        outputs = []
        for frame in (clip_a[0], clip_b[0], clip_a[1]):
            anchor = fc.anchor_from_mask(fc.subject_mask(frame[..., 3]), "stance")
            outputs.append(np.asarray(fc.resample_rgba(frame, scale, resampler, anchor_src=anchor,
                                                       anchor_dst=(32, 56), out_size=(64, 64))))
        assert outputs[0].tobytes() == outputs[1].tobytes(), resampler
        # The static lower body does not shimmer when another part of the pose moves.
        assert outputs[0][50:, :, 3].any()
        assert np.array_equal(outputs[0][50:], outputs[2][50:]), resampler


def _disk(colour: tuple[int, int, int]) -> np.ndarray:
    """The Dusk premult_check fixture: a hard-edged disk of radius 20 that resampling makes soft."""
    yy, xx = np.mgrid[:64, :64]
    rgba = np.zeros((64, 64, 4), np.uint8)
    rgba[(xx - 32) ** 2 + (yy - 32) ** 2 < 20 ** 2] = colour + (255,)
    return rgba


def test_no_double_premultiply():
    """Dusk premult_check / game-opus55 resize_rgba: soft edges keep their colour, neither darker nor brighter."""
    colour = (120, 90, 60)
    disk = _disk(colour)
    for resampler, scale in (("box", 23 / 64), ("lanczos", 23 / 64), ("lanczos", 40 / 64), ("lanczos", 1.5)):
        out = np.asarray(fc.resample_rgba(disk, scale, resampler)).astype(float)
        soft = (out[..., 3] > 20) & (out[..., 3] < 235)
        assert soft.sum() > 20
        assert np.abs(out[out[..., 3] > 20][:, :3] - colour).max() <= 2, (resampler, scale)
    # Controls: straight per-channel scaling darkens edges; premultiplying before Pillow's RGBA resize brightens them.
    size = (23, 23)
    straight = np.dstack([np.asarray(Image.fromarray(disk[..., c]).resize(size, Image.Resampling.LANCZOS))
                          for c in range(4)]).astype(float)
    soft = (straight[..., 3] > 20) & (straight[..., 3] < 235)
    assert straight[soft][:, 0].mean() < colour[0] - 20
    twice = disk.astype(float)
    twice[..., :3] *= twice[..., 3:] / 255
    doubled = np.asarray(Image.fromarray(twice.astype(np.uint8)).resize(size, Image.Resampling.LANCZOS)).astype(float)
    alpha = np.maximum(doubled[..., 3:] / 255, 1e-6)
    soft = (doubled[..., 3] > 20) & (doubled[..., 3] < 235)
    assert (doubled[..., :3] / alpha)[soft][:, 0].mean() > colour[0] + 5
    with pytest.raises(ValueError, match="premultiplied"):
        fc.resample_rgba(Image.fromarray(disk).convert("RGBa"), 0.5)


def test_nearest_rejects_fractional():
    """S06 / r08: nearest only at whole factors; reductions sample logical-pixel centres exactly."""
    rng = np.random.default_rng(7)
    palette = rng.integers(0, 256, (8, 4), dtype=np.uint8)
    palette[:, 3] = 255
    logical = palette[rng.integers(0, 8, (12, 10))]
    for bad in (2.5, 1.7, 0.4, 2 / 3):
        with pytest.raises(ValueError):
            fc.resample_rgba(logical, bad, "nearest")
    big = np.asarray(fc.resample_rgba(logical, 8, "nearest"))
    assert np.array_equal(big, np.repeat(np.repeat(logical, 8, axis=0), 8, axis=1))
    assert np.array_equal(np.asarray(fc.resample_rgba(big, 1 / 8, "nearest")), logical)
    # A logical grid that starts at an offset is recovered by pinning its corner.
    shifted = _embed(big, (100, 120), (5, 3))
    recovered = fc.resample_rgba(shifted, 1 / 8, "nearest", anchor_src=(5, 3), anchor_dst=(0, 0), out_size=(10, 12))
    assert np.array_equal(np.asarray(recovered), logical)
    with pytest.raises(ValueError, match="divisible"):
        fc.resample_rgba(shifted, 1 / 8, "nearest")


def test_lanczos_uses_box_filter_for_large_reductions():
    rgba = _striped_character()
    for scale in (0.5, 0.25):
        assert fc.resample_rgba(rgba, scale, "lanczos").tobytes() == fc.resample_rgba(rgba, scale, "box").tobytes()
    assert fc.resample_rgba(rgba, 0.75, "lanczos").tobytes() != fc.resample_rgba(rgba, 0.75, "box").tobytes()
    opaque = fc.resample_rgba(np.full((10, 10, 4), 255, np.uint8), 0.7)
    assert opaque.size == (7, 7) and np.asarray(opaque)[..., 3].min() == 255


# --------------------------------------------------------------------------- A1-T7 timing and seams

def test_frame_durations_sum_exact():
    """video2dsprite.py:386 rounding drifted; durations must sum exactly with <= 0.5 ms playhead error."""
    for count in range(1, 61):
        totals = np.arange(count, 5001)
        durations = np.array([fc.frame_durations(int(total), count) for total in totals], np.int64)
        assert durations.shape == (totals.size, count)
        assert np.array_equal(durations.sum(axis=1), totals)
        assert durations.min() >= 1 and (durations.max(axis=1) - durations.min(axis=1)).max() <= 1
        # Playhead after frame i is within 0.5 ms of i * total / count: |2n*elapsed - 2i*total| <= n.
        elapsed = np.cumsum(durations, axis=1)
        ideal = 2 * np.arange(1, count + 1)[None, :] * totals[:, None]
        assert np.abs(2 * count * elapsed - ideal).max() <= count
    for count in range(2, 61):
        with pytest.raises(ValueError):
            fc.frame_durations(count - 1, count)
    assert fc.frame_durations(1000, 7) == [143, 143, 143, 142, 143, 143, 143]
    with pytest.raises(TypeError):
        fc.frame_durations(1000.0, 7)


def test_rational_fps():
    assert fc.rational_fps(8, 452) == "2000/113"
    assert fc.rational_fps(16, 1000) == "16/1"
    assert fc.rational_fps(24, 1001) == "24000/1001"
    with pytest.raises(ValueError):
        fc.rational_fps(0, 1000)


def _orbit_frames(positions: list[tuple[float, float]]) -> list[np.ndarray]:
    yy, xx = np.mgrid[:48, :64]
    frames = []
    for x, y in positions:
        alpha = np.clip(255 * (4.5 - np.hypot(xx - x, yy - y)), 0, 255)
        frame = np.zeros((48, 64, 4), np.uint8)
        frame[..., :3] = (230, 120, 40)
        frame[..., 3] = np.floor(alpha + 0.5).astype(np.uint8)
        frames.append(frame)
    return frames


def test_seam_ratio_flags_non_cyclic():
    """MAP-14 / repro_16: normalised seam metric separates seamless loops, wrap pops and held duplicates."""
    circle = [(32 + 10 * math.cos(2 * math.pi * k / 16), 24 + 10 * math.sin(2 * math.pi * k / 16)) for k in range(16)]
    cyclic = fc.seam_report(_orbit_frames(circle))
    assert 0.8 <= cyclic["seam_over_median"] <= 1.25 and cyclic["seam_over_p95"] <= 1.1
    line = [(10.0 + k, 24.0) for k in range(16)]
    sweep = fc.seam_report(_orbit_frames(line))
    assert sweep["seam_over_p95"] > 4 and sweep["frames"] == 16
    held = fc.seam_report(_orbit_frames(circle + circle[:1]))
    assert held["seam"] == 0 and held["seam_over_median"] == 0
    assert set(cyclic) >= {"seam", "adjacent_median", "adjacent_p95", "seam_over_median", "seam_over_p95"}
    frames = _orbit_frames(line)
    region = np.zeros((48, 64), bool)
    region[:, :20] = True
    masked = fc.seam_report(frames, mask=region)
    assert masked["seam"] == fc.transition_mae(frames[-1], frames[0], mask=region)
    with pytest.raises(ValueError):
        fc.seam_report(frames[:1])


def test_transition_mae_is_premultiplied():
    a = np.zeros((2, 2, 4), np.uint8)
    b = a.copy()
    b[0, 0, :3] = (255, 255, 255)  # colour hidden under alpha 0 does not count
    assert fc.transition_mae(a, b) == 0.0
    a[1, 1] = (200, 0, 0, 128)
    b[1, 1] = (0, 0, 0, 128)
    assert fc.transition_mae(a, b) == pytest.approx(200 * 128 / 255 / 4 / 4)
    weights = np.zeros((2, 2), np.uint8)
    weights[1, 1] = 255
    assert fc.transition_mae(a, b, mask=weights) == pytest.approx(200 * 128 / 255 / 4)


# --------------------------------------------------------------------------- A1-T8 utilities

def test_utf8_stdio_cp1252():
    """F-14 / report v2 P1-1: a cp1252 console must not turn a finished run into UnicodeEncodeError."""
    script = ("import sys; sys.path.insert(0, sys.argv[1]); import forge_core; SETUP"
              "print('\\u2192 \\u65e5\\u672c caf\\u00e9'); "
              "print(forge_core.ascii_text('a \\u2192 b \\u2014 c \\u00d7 d \\u00b7 e'))")
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONDONTWRITEBYTECODE="1")

    def run(setup: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", script.replace("SETUP", setup), str(CANONICAL.parent)],
                              capture_output=True, env=env, timeout=120)

    fixed = run("forge_core.utf8_stdio(); ")
    assert fixed.returncode == 0, fixed.stderr.decode("utf-8", "replace")
    assert fixed.stdout.decode("utf-8").splitlines() == ["\u2192 \u65e5\u672c caf\u00e9", "a -> b - c x d . e"]
    broken = run("")
    assert broken.returncode != 0 and b"UnicodeEncodeError" in broken.stderr


def test_ascii_text():
    assert fc.ascii_text("4\u00d74 cells \u2192 frames \u2014 ok \u00b7 done") == "4x4 cells -> frames - ok . done"
    assert fc.ascii_text("\u6e2c\u8a66") == "\\u6e2c\\u8a66"
    assert fc.ascii_text("plain") == "plain"


def test_require_modules_message(capsys):
    fc.require_modules(["json", "math"])
    assert capsys.readouterr().err == ""
    with pytest.raises(SystemExit) as excinfo:
        fc.require_modules(["json", "forge_missing_module_for_tests", "PIL.forge_missing_plugin"])
    assert excinfo.value.code == 1
    message = capsys.readouterr().err
    assert message.startswith("error: missing Python module(s): forge_missing_module_for_tests, PIL.forge_missing_plugin")
    assert "python -m pip install forge_missing_module_for_tests Pillow" in message
    assert message.isascii()


def test_write_json_and_hashes(tmp_path):
    """MAP-24: manifests hold UTF-8 JSON with manifest-relative POSIX paths and sha256 values."""
    target = tmp_path / "manifest.json"
    data = {"label": "\u72d0\u72f8", "area": np.int64(111), "scale": np.float32(0.5), "box": np.arange(4)}
    fc.write_json(target, data)
    assert target.read_bytes() == (
        '{\n  "label": "\u72d0\u72f8",\n  "area": 111,\n  "scale": 0.5,\n  "box": [\n    0,\n    1,\n'
        '    2,\n    3\n  ]\n}\n').encode("utf-8")
    with pytest.raises(FileExistsError):
        fc.write_json(target, {"replaced": True})
    fc.write_json(target, {"replaced": True}, no_clobber=False)
    assert json.loads(target.read_text(encoding="utf-8")) == {"replaced": True}
    for bad in ({"value": float("nan")}, {"path": tmp_path}):
        with pytest.raises((ValueError, TypeError)):
            fc.write_json(tmp_path / "bad.json", bad)
    assert not (tmp_path / "bad.json").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["manifest.json"]
    assert fc.sha256_file(target) == hashlib.sha256(target.read_bytes()).hexdigest()
    assert fc.sha256_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()
    assert fc.portable_path(tmp_path / "frames" / "f0.png", tmp_path) == "frames/f0.png"
    assert fc.portable_path(tmp_path / "f0.png", tmp_path / "bundle") == "../f0.png"


# --------------------------------------------------------------------------- A1-T9 vendoring

def test_vendored_copies_match_shared():
    """S11 / MAP-12: one canonical core, byte-identical in every skill that uses it."""
    canonical = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    for skill in VENDORED_SKILLS:
        copy = ROOT / "skills" / skill / "scripts" / "forge_core.py"
        assert copy.exists(), copy
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == canonical, copy
