"""Tests for generate2dmap extract_prop_pack.py (module B10-map-prop-pack, prop_pack.v2).

Audit finding and repro ids (2026-10-05 map audit) are named in the docstrings.
Fixtures are synthetic except the meadow crop, which real_fixture() checks against
tests/fixtures/real/PROVENANCE.json. The cfed170 extractor and the improved
fork's despill port are copied verbatim below as oracles, so the legacy-switch
tests keep their meaning after the rewrite.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import time
from collections import deque
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from forge_testutils import (assert_cli_help, assert_valid_contract, contract_validator, load_script,
                             make_magenta_sheet, real_fixture, run_cli, script_path, SKILLS_DIR)

PROPS = load_script("generate2dmap", "extract_prop_pack")
SCRIPT = script_path("generate2dmap", "extract_prop_pack")
forge_core = PROPS.forge_core
MAGENTA = (255, 0, 255)


# --------------------------------------------------------------------------- helpers

def run(root: Path, *argv: str) -> dict:
    """Run extract() in process and validate what it published."""
    manifest = PROPS.extract(PROPS.build_parser().parse_args(list(argv)))
    validate(manifest)
    return manifest


def validate(manifest: dict) -> None:
    """Every manifest validates against the vendored map contract; its QA block is a common qaEnvelope."""
    assert_valid_contract(manifest, "map", "prop_pack_v2", skill="generate2dmap")
    assert_valid_contract(manifest["qa"], "common", "qaEnvelope", skill="generate2dmap")


def pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA"))


def hidden_rgb_zeroed(array: np.ndarray) -> np.ndarray:
    """The published convention: RGB is 0 under alpha 0 (the cfed170 writer kept it)."""
    array = np.array(array)
    array[array[..., 3] == 0] = 0
    return array


def tinted(array: np.ndarray) -> int:
    """repro_21b: visible pixels whose magenta excess min(R, B) - G exceeds 12."""
    rgb = array[..., :3].astype(int)
    return int(((array[..., 3] > 0) & ((np.minimum(rgb[..., 0], rgb[..., 2]) - rgb[..., 1]) > 12)).sum())


def aa_chroma_sheet(rows: int = 2, cols: int = 2, cell: int = 64) -> Image.Image:
    """Opaque magenta sheet whose props have key-mixed anti-aliased edges, dark outlines and
    interior purple dots: drawn at 4x and reduced with Lanczos like a model sheet (MAP-03, MAP-09)."""
    scale, bodies = 4, [(60, 140, 60), (120, 85, 50), (90, 110, 130), (150, 120, 40)]
    big = Image.new("RGB", (cols * cell * scale, rows * cell * scale), MAGENTA)
    draw = ImageDraw.Draw(big)
    for index in range(rows * cols):
        row, col = divmod(index, cols)
        x0, y0, size = col * cell * scale, row * cell * scale, cell * scale
        inset = size * 18 // 100 + (index % 3) * scale
        draw.ellipse((x0 + inset, y0 + inset, x0 + size - inset, y0 + size - inset), fill=(20, 30, 25))
        draw.ellipse((x0 + inset + 6, y0 + inset + 6, x0 + size - inset - 6, y0 + size - inset - 6),
                     fill=bodies[index % len(bodies)])
        centre_x, centre_y = x0 + size // 2, y0 + size // 2
        draw.ellipse((centre_x - 10, centre_y - 10, centre_x + 10, centre_y + 10), fill=(120, 40, 140))
    return big.resize((cols * cell, rows * cell), Image.Resampling.LANCZOS)


def native_sheet(width: int, height: int, boxes: dict[tuple[int, int, int, int], tuple[int, ...]]) -> Image.Image:
    image = np.zeros((height, width, 4), np.uint8)
    for (x0, y0, x1, y1), colour in boxes.items():
        image[y0:y1, x0:x1] = colour
    return Image.fromarray(image)


def forest_replica() -> np.ndarray:
    """Synthetic stand-in for the 2026-10-05 forest prop atlas (report v2 3.3 and 5.5): a 1254^2
    native-alpha sheet whose tree, lantern and rock have the measured content boxes
    [85, 35, 605, 691), [788, 143, 1083, 671) and [48, 851, 598, 1185), plus alpha 1-4 haze
    and faint detached islands (alpha 10-17) like the host output."""
    size = 1254
    yy, xx = np.mgrid[0:size, 0:size]
    cx, cy = xx + 0.5, yy + 0.5
    rgba = np.zeros((size, size, 4), np.uint8)
    rng = np.random.default_rng(7)
    haze = rng.random((size, size)) < 0.03
    rgba[haze, :3] = rng.integers(0, 256, (int(haze.sum()), 3))
    rgba[haze, 3] = rng.integers(1, 5, int(haze.sum()))
    for x0, y0 in ((13, 15), (700, 760), (1180, 1200), (640, 40)):
        rgba[y0:y0 + 6, x0:x0 + 6] = (200, 200, 220, 10 + (x0 % 8))

    def paint(mask: np.ndarray, colour: tuple[int, int, int], alpha: int = 255) -> None:
        rgba[mask] = (*colour, alpha)

    canopy = ((cx - 345) / 260) ** 2 + ((cy - 300) / 265) ** 2 <= 1
    paint(canopy, (40, 110, 50))
    paint(canopy & (((cx - 345) / 258) ** 2 + ((cy - 300) / 263) ** 2 > 1), (60, 130, 70), 170)  # soft rim
    paint((xx >= 315) & (xx < 375) & (yy >= 480) & (yy < 691), (95, 65, 40))
    roof = (((cx - 935.5) / 147.5) ** 2 + ((cy - 175) / 32) ** 2 <= 1) & (cy < 175)
    body = (xx >= 870) & (xx < 1002) & (yy >= 175) & (yy < 330)
    glow = (xx >= 864) & (xx < 1008) & (yy >= 175) & (yy < 336) & ~body
    paint(glow, (250, 220, 120), 60)
    paint(roof, (70, 60, 60))
    paint(body, (240, 200, 90))
    paint((xx >= 925) & (xx < 947) & (yy >= 330) & (yy < 671), (50, 50, 55))
    paint((xx >= 900) & (xx < 972) & (yy >= 650) & (yy < 671), (60, 60, 65))
    paint(((cx - 323) / 275) ** 2 + ((cy - 1018) / 167) ** 2 <= 1, (120, 120, 115))
    return rgba


# --------------------------------------------------------------------------- cfed170 and fork oracles (verbatim)

def _cfed170_color_distance(rgb, target=MAGENTA):
    r, g, b = rgb
    tr, tg, tb = target
    return math.sqrt((r - tr) ** 2 + (g - tg) ** 2 + (b - tb) ** 2)


def _cfed170_remove_bg_magenta(img, threshold, edge_threshold):
    """skills/generate2dmap/scripts/extract_prop_pack.py:28-67 at cfed170."""
    img = img.convert("RGBA")
    pixels = img.load()
    width, height = img.size

    for x in range(width):
        for y in range(height):
            r, g, b, a = pixels[x, y]
            if a > 0 and _cfed170_color_distance((r, g, b)) < threshold:
                pixels[x, y] = (0, 0, 0, 0)

    visited = set()
    queue = deque()
    for x in range(width):
        queue.append((x, 0))
        queue.append((x, height - 1))
    for y in range(height):
        queue.append((0, y))
        queue.append((width - 1, y))

    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or x < 0 or x >= width or y < 0 or y >= height:
            continue
        visited.add((x, y))
        r, g, b, a = pixels[x, y]
        should_expand = a == 0
        if a > 0 and _cfed170_color_distance((r, g, b)) < edge_threshold:
            pixels[x, y] = (0, 0, 0, 0)
            should_expand = True
        if should_expand:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nxt = (x + dx, y + dy)
                    if nxt not in visited:
                        queue.append(nxt)

    return img


def _fork_despill_chroma_edges(img, radius=0):
    """plan/study-asf-improved/port/skills/generate2dmap/scripts/extract_prop_pack.py:77-98 (improved fork port)."""
    if type(radius) is not int or radius not in (0, 1, 2, 3):
        raise ValueError("Despill radius must be an integer from 0 to 3.")
    if radius == 0:
        return img
    pixels = np.array(img.convert("RGBA"))
    transparent = pixels[:, :, 3] == 0
    if not transparent.any():
        return img
    boundary = Image.fromarray(transparent.astype(np.uint8) * 255)
    nearby = np.asarray(boundary.filter(ImageFilter.MaxFilter(2 * radius + 1))) > 0
    red, green, blue = (pixels[:, :, channel].astype(np.int16) for channel in range(3))
    excess = np.minimum(red, blue) - green
    affected = nearby & (pixels[:, :, 3] > 0) & (excess > 12)
    pixels[:, :, 0][affected] = (red[affected] - excess[affected]).astype(np.uint8)
    pixels[:, :, 2][affected] = (blue[affected] - excess[affected]).astype(np.uint8)
    return Image.fromarray(pixels)


def _cfed170_trim_border(img, px):
    if px <= 0:
        return img
    width, height = img.size
    if width <= px * 2 or height <= px * 2:
        return img
    return img.crop((px, px, width - px, height - px))


def _cfed170_clean_edges(img, depth):
    if depth <= 0:
        return img
    pixels = img.load()
    width, height = img.size
    for d in range(depth):
        for x in range(width):
            for y in (d, height - 1 - d):
                if 0 <= y < height:
                    r, g, b, a = pixels[x, y]
                    if a > 0 and ((r < 40 and g < 40 and b < 40) or _cfed170_color_distance((r, g, b)) < 150):
                        pixels[x, y] = (0, 0, 0, 0)
        for y in range(height):
            for x in (d, width - 1 - d):
                if 0 <= x < width:
                    r, g, b, a = pixels[x, y]
                    if a > 0 and ((r < 40 and g < 40 and b < 40) or _cfed170_color_distance((r, g, b)) < 150):
                        pixels[x, y] = (0, 0, 0, 0)
    return img


def _cfed170_connected_components(img, min_area):
    alpha = img.getchannel("A")
    pixels = alpha.load()
    width, height = img.size
    visited = [[False] * width for _ in range(height)]
    components = []

    for y in range(height):
        for x in range(width):
            if pixels[x, y] == 0 or visited[y][x]:
                continue
            queue = deque([(x, y)])
            visited[y][x] = True
            coords = []
            min_x = max_x = x
            min_y = max_y = y
            touches_edge = x == 0 or y == 0 or x == width - 1 or y == height - 1

            while queue:
                cx, cy = queue.popleft()
                coords.append((cx, cy))
                min_x = min(min_x, cx)
                min_y = min(min_y, cy)
                max_x = max(max_x, cx)
                max_y = max(max_y, cy)
                if cx == 0 or cy == 0 or cx == width - 1 or cy == height - 1:
                    touches_edge = True
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < width and 0 <= ny < height and pixels[nx, ny] > 0 and not visited[ny][nx]:
                        visited[ny][nx] = True
                        queue.append((nx, ny))

            if len(coords) >= min_area:
                components.append({"area": len(coords), "bbox": (min_x, min_y, max_x + 1, max_y + 1),
                                   "touches_edge": touches_edge, "coords": coords})

    components.sort(key=lambda item: int(item["area"]), reverse=True)
    return components


def _cfed170_pad_bbox(bbox, padding, width, height):
    x0, y0, x1, y1 = bbox
    return (max(0, x0 - padding), max(0, y0 - padding), min(width, x1 + padding), min(height, y1 + padding))


def _cfed170_bbox_touches_edge(bbox, width, height, margin):
    if bbox is None:
        return False
    x0, y0, x1, y1 = bbox
    return x0 <= margin or y0 <= margin or x1 >= width - margin or y1 >= height - margin


def _cfed170_mask_to_component(img, component):
    selected = Image.new("RGBA", img.size, (0, 0, 0, 0))
    src = img.load()
    dst = selected.load()
    for x, y in component["coords"]:
        dst[x, y] = src[x, y]
    return selected


def _cfed170_extract_cell(cell, args):
    """extract_prop_pack.py:209-247 at cfed170."""
    alpha_bbox = lambda image: image.getchannel("A").getbbox()  # noqa: E731
    original_edge_touch = _cfed170_bbox_touches_edge(alpha_bbox(cell), cell.width, cell.height, args.edge_touch_margin)
    frame = _cfed170_trim_border(cell, args.trim_border)
    frame = _cfed170_clean_edges(frame, args.edge_clean_depth)
    components = _cfed170_connected_components(frame, args.min_component_area)
    selected_component = None
    bbox = alpha_bbox(frame)

    if args.component_mode == "largest" and components:
        selected_component = components[0]
        frame = _cfed170_mask_to_component(frame, selected_component)
        bbox = tuple(selected_component["bbox"])
    elif args.component_mode == "all" and components:
        selected = Image.new("RGBA", frame.size)
        for component in components:
            part = _cfed170_mask_to_component(frame, component)
            selected.alpha_composite(part)
        frame = selected
        bbox = alpha_bbox(frame)
    else:
        bbox = None

    padded_bbox = _cfed170_pad_bbox(bbox, args.component_padding, frame.width, frame.height) if bbox else None
    edge_touch = original_edge_touch or _cfed170_bbox_touches_edge(bbox, frame.width, frame.height,
                                                                    args.edge_touch_margin)
    prop = frame.crop(padded_bbox) if padded_bbox else None
    return prop, {"component_count": len(components), "crop_bbox": list(bbox) if bbox else None,
                  "padded_crop_bbox": list(padded_bbox) if padded_bbox else None, "edge_touch": edge_touch}


def _oracle_props(sheet: Image.Image, boxes, *, native: bool, despill: int, alpha_floor: int, **options):
    """The cfed170 extractor with the fork's despill: keyed sheet -> per-box (prop, info)."""
    cleaned = sheet.convert("RGBA") if native else _fork_despill_chroma_edges(
        _cfed170_remove_bg_magenta(sheet, 100, 150), despill)
    if alpha_floor:
        cleaned = cleaned.copy()
        cleaned.putalpha(cleaned.getchannel("A").point(lambda value: 0 if value <= alpha_floor else value))
    args = argparse.Namespace(**options)
    return [_cfed170_extract_cell(cleaned.crop(box), args) for box in boxes]


# --------------------------------------------------------------------------- B10-T1: keying and despill port

@pytest.mark.parametrize("radius", [0, 1, 2, 3])
def test_key_sheet_matches_the_fork_despill_port(radius):
    """B10-T1, MAP-03, MAP-12: forge_matte legacy key + edge despill equal the fork's prop-pack port."""
    sheet = aa_chroma_sheet(3, 3, 48)
    expected = np.asarray(_fork_despill_chroma_edges(_cfed170_remove_bg_magenta(sheet, 100, 150), radius)
                          .convert("RGBA"))
    actual, report = PROPS.key_sheet(np.asarray(sheet.convert("RGBA")), "chroma_key", 100, 150, radius)
    assert np.array_equal(actual, expected)
    assert report["radius"] == radius and report["margin"] == 12
    assert (report["changed_px"] > 0) == (radius > 0)


LEGACY_VARIANTS = {
    "defaults": dict(component_mode="largest", component_padding=8, min_component_area=100, trim_border=0,
                     edge_clean_depth=0, edge_touch_margin=0),
    "all-trim-clean": dict(component_mode="all", component_padding=3, min_component_area=4, trim_border=2,
                           edge_clean_depth=1, edge_touch_margin=0),
    "tight-margin": dict(component_mode="largest", component_padding=0, min_component_area=1, trim_border=0,
                         edge_clean_depth=0, edge_touch_margin=2),
}


@pytest.mark.parametrize("radius", [0, 2])
@pytest.mark.parametrize("variant", sorted(LEGACY_VARIANTS))
def test_legacy_switches_reproduce_the_fork_props(tmp_path, variant, radius):
    """plan Appendix H: --connectivity 4 (and --despill-radius 0) reproduce the cfed170 extractor with the
    fork's despill pixel for pixel; v1 keys keep their values, in cell coordinates (MAP-18)."""
    options = LEGACY_VARIANTS[variant]
    sheet = aa_chroma_sheet(2, 3, 56)
    sheet.save(tmp_path / "sheet.png")
    boxes = forge_core.rounded_grid_boxes(sheet.width, sheet.height, 2, 3)
    oracle = _oracle_props(sheet, boxes, native=False, despill=radius, alpha_floor=0, **options)
    flags = ["--component-mode", options["component_mode"], "--component-padding", str(options["component_padding"]),
             "--min-component-area", str(options["min_component_area"]), "--trim-border", str(options["trim_border"]),
             "--edge-clean-depth", str(options["edge_clean_depth"]), "--edge-touch-margin",
             str(options["edge_touch_margin"])]
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "2", "--cols", "3",
                   "--output-dir", str(tmp_path / "out"), "--connectivity", "4", "--despill-radius", str(radius),
                   *flags)
    items = {item["index"]: item for item in manifest["accepted"] + manifest["rejected"]}
    for index, (prop, info) in enumerate(oracle):
        item = items[index]
        if prop is None:
            assert item["status"] == "empty"
            continue
        assert np.array_equal(pixels(tmp_path / "out" / item["label"] / "prop.png"),
                              hidden_rgb_zeroed(np.asarray(prop)))
        trim = item["trim_applied"]
        assert item["crop_bbox"] == [value + trim for value in info["crop_bbox"]]
        assert item["padded_crop_bbox"] == [value + trim for value in info["padded_crop_bbox"]]
        assert item["component_count"] == info["component_count"]
        assert item["edge_touch"] == info["edge_touch"]
    assert manifest["despill_radius"] == radius and manifest["geometry"]["connectivity"] == 4


def test_legacy_native_alpha_with_floor_matches_cfed170(tmp_path):
    """Native alpha is never keyed; --alpha-floor keeps its cfed170 meaning (zero alpha <= N)."""
    sheet = native_sheet(48, 24, {(4, 4, 20, 20): (255, 0, 255, 128), (28, 6, 44, 22): (90, 60, 30, 255),
                                  (0, 0, 1, 1): (255, 0, 255, 3), (40, 0, 41, 1): (10, 10, 10, 200)})
    sheet.save(tmp_path / "sheet.png")
    boxes = [(0, 0, 24, 24), (24, 0, 48, 24)]
    options = dict(LEGACY_VARIANTS["defaults"], min_component_area=1)
    oracle = _oracle_props(sheet, boxes, native=True, despill=0, alpha_floor=4, **options)
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "2",
                   "--labels", "glass,crate", "--output-dir", str(tmp_path / "out"), "--alpha-floor", "4",
                   "--connectivity", "4", "--min-component-area", "1")
    assert manifest["background_mode"] == "native_alpha" and manifest["despill_radius"] == 0
    assert manifest["hygiene"]["mode"] == "floor" and manifest["alpha_floor_pixels_removed"] == 1
    for item, (prop, _) in zip(manifest["accepted"], oracle):
        assert np.array_equal(pixels(tmp_path / "out" / item["image"]), hidden_rgb_zeroed(np.asarray(prop)))


def test_remove_bg_magenta_wrapper_is_the_cfed170_keyer():
    """extract_platform_strip still imports remove_bg_magenta from this module (until B11 lands)."""
    sheet = aa_chroma_sheet(2, 2, 40).convert("RGBA")
    original = np.asarray(sheet).copy()
    keyed = PROPS.remove_bg_magenta(sheet, 100, 150)
    assert np.array_equal(np.asarray(keyed), np.asarray(_cfed170_remove_bg_magenta(sheet, 100, 150)))
    assert np.array_equal(np.asarray(sheet), original)


@pytest.mark.perf
def test_keying_a_1254_sheet_within_one_second(tmp_path):
    """B10-T1, MAP-13: a 1254^2 chroma sheet keys (legacy key + despill) in at most 1 s; the cfed170
    extractor took 12.6 s. The whole 4x4 extraction stays well inside a generous budget."""
    size = 1254
    yy, xx = np.mgrid[0:size, 0:size]
    sheet = np.zeros((size, size, 4), np.uint8)
    sheet[...] = (*MAGENTA, 255)
    for x0, y0, _, _ in forge_core.rounded_grid_boxes(size, size, 4, 4):
        sheet[((xx - x0 - 157) / 110.0) ** 2 + ((yy - y0 - 157) / 125.0) ** 2 <= 1, :3] = (60, 130, 50)
    PROPS.key_sheet(sheet, "chroma_key")  # warm-up: scipy import
    best = math.inf
    for _ in range(3):
        start = time.perf_counter()
        PROPS.key_sheet(sheet, "chroma_key")
        best = min(best, time.perf_counter() - start)
    assert best <= 1.0, f"keying took {best:.2f} s"
    Image.fromarray(sheet).save(tmp_path / "sheet.png")
    start = time.perf_counter()
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "4", "--cols", "4",
                   "--grid-rounding", "nearest", "--output-dir", str(tmp_path / "out"))
    elapsed = time.perf_counter() - start
    assert len(manifest["accepted"]) == 16
    assert elapsed <= 5.0, f"extraction took {elapsed:.2f} s"


# --------------------------------------------------------------------------- B10-T2: publication

def test_existing_output_directory_is_refused_repro_18(tmp_path):
    """repro_18, DOC-10, MAP-17: a second run never mixes its files into an earlier output."""
    native_sheet(64, 32, {(8, 8, 24, 24): (90, 60, 30, 255), (40, 8, 56, 24): (90, 60, 30, 255)}).save(
        tmp_path / "sheet.png")
    common = ["--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "2", "--output-dir",
              str(tmp_path / "props")]
    run(tmp_path, *common, "--labels", "barrel,crate")
    before = sorted(path.relative_to(tmp_path).as_posix() for path in (tmp_path / "props").rglob("*"))
    with pytest.raises(FileExistsError, match="existing output directory"):
        run(tmp_path, *common, "--labels", "rock,stump")
    assert sorted(path.relative_to(tmp_path).as_posix() for path in (tmp_path / "props").rglob("*")) == before
    assert not list(tmp_path.glob(".props.stage-*"))


def test_all_empty_run_fails_and_publishes_nothing(tmp_path):
    """B10-T2 probe: a run that accepts nothing exits 1 instead of publishing an empty pack."""
    native_sheet(32, 16, {(2, 2, 4, 4): (90, 60, 30, 255)}).save(tmp_path / "sheet.png")
    with pytest.raises(ValueError, match="No prop was accepted"):
        PROPS.extract(PROPS.build_parser().parse_args([
            "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "2", "--output-dir",
            str(tmp_path / "out"), "--min-component-area", "500", "--keep-empty"]))
    assert not (tmp_path / "out").exists()


def test_keep_empty_writes_an_explicit_placeholder_repro_05(tmp_path):
    """repro_05, MAP-20: an empty cell kept with --keep-empty is a placeholder whose size matches its file."""
    native_sheet(64, 32, {(8, 8, 24, 24): (40, 160, 40, 255)}).save(tmp_path / "sheet.png")
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "2",
                   "--labels", "bush,nothing", "--output-dir", str(tmp_path / "props"), "--keep-empty")
    bush, nothing = manifest["accepted"]
    assert bush["status"] == "accepted" and nothing["status"] == "placeholder"
    placeholder = pixels(tmp_path / "props" / nothing["image"])
    assert list(placeholder.shape[1::-1]) == nothing["output_size"] == [1, 1]
    assert not placeholder[..., 3].any()
    assert manifest["edge_touch_props"] == [] and manifest["rejected"] == []


def test_sidecar_manifest_is_removed_when_the_directory_publish_fails(tmp_path, monkeypatch):
    """Appendix D: a manifest outside the output dir is rolled back if the directory cannot be published."""
    native_sheet(16, 16, {(4, 4, 10, 12): (255, 0, 255, 128)}).save(tmp_path / "props.png")

    def refuse(stage, final):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(forge_core, "publish_directory_no_replace", refuse)
    with pytest.raises(OSError, match="simulated"):
        PROPS.extract(PROPS.build_parser().parse_args([
            "--input", str(tmp_path / "props.png"), "--rows", "1", "--cols", "1", "--output-dir",
            str(tmp_path / "out"), "--manifest", str(tmp_path / "meta" / "kit.json")]))
    assert not (tmp_path / "out").exists() and not (tmp_path / "meta" / "kit.json").exists()


# --------------------------------------------------------------------------- B10-T3: prop_pack.v2

def staff_cell(path: Path) -> int:
    """repro_04: a 20x20 body and a 14 px diagonal staff whose pixels touch only at corners."""
    cell = np.zeros((64, 64, 4), np.uint8)
    cell[30:50, 22:42] = (120, 80, 40, 255)
    for step in range(14):
        cell[29 - step, 42 + step] = (230, 230, 230, 255)
    Image.fromarray(cell).save(path)
    return int((cell[..., 3] > 0).sum())


@pytest.mark.parametrize("extra", [(), ("--component-mode", "all", "--min-component-area", "12"),
                                   ("--component-mode", "all", "--min-component-area", "1")])
def test_diagonal_pixel_strokes_survive_repro_04(tmp_path, extra):
    """repro_04, MAP-06: 8-connectivity keeps all 14 staff pixels in every documented mode."""
    source_px = staff_cell(tmp_path / "sheet.png")
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "1",
                   "--labels", "wizard-staff", "--output-dir", str(tmp_path / "out"), *extra)
    item = manifest["accepted"][0]
    assert int((pixels(tmp_path / "out" / item["image"])[..., 3] > 0).sum()) == source_px == 414
    assert item["component_count"] == 1 and item["dropped_components"] == 0 and item["dropped_area"] == 0


def test_four_connectivity_reports_what_it_drops(tmp_path):
    """MAP-06: the legacy rule still drops the staff, but the manifest and QA now say so."""
    staff_cell(tmp_path / "sheet.png")
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "1",
                   "--output-dir", str(tmp_path / "out"), "--connectivity", "4")
    item = manifest["accepted"][0]
    assert (item["kept_area"], item["dropped_components"], item["dropped_area"]) == (400, 14, 14)
    check = {check["id"]: check for check in manifest["qa"]["checks"]}["dropped_area"]
    assert check["status"] == "warn" and manifest["qa"]["status"] == "warn"
    with pytest.raises(ValueError, match="dropped more than"):
        run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "1",
            "--output-dir", str(tmp_path / "strict"), "--connectivity", "4", "--max-dropped-fraction", "0.01")
    assert not (tmp_path / "strict").exists()


@pytest.mark.parametrize("size,count", [(1024, 3), (1254, 4)])
def test_rounded_grid_accepts_model_sizes_repro_09(tmp_path, size, count):
    """repro_09, MAP-04: 1024^2 3x3 and 1254^2 4x4 sheets split with rounded cell edges (no pixel lost)."""
    boxes = forge_core.rounded_grid_boxes(size, size, count, count)
    sheet = Image.new("RGB", (size, size), MAGENTA)
    draw = ImageDraw.Draw(sheet)
    for x0, y0, x1, y1 in boxes:
        draw.ellipse((x0 + 40, y0 + 40, x1 - 40, y1 - 40), fill=(70, 120, 60))
    sheet.save(tmp_path / "sheet.png")
    common = ["--input", str(tmp_path / "sheet.png"), "--rows", str(count), "--cols", str(count)]
    with pytest.raises(ValueError, match="--grid-rounding nearest"):
        run(tmp_path, *common, "--output-dir", str(tmp_path / "exact"))
    manifest = run(tmp_path, *common, "--output-dir", str(tmp_path / "out"), "--grid-rounding", "nearest")
    assert [item["cell_box"] for item in manifest["accepted"]] == [list(box) for box in boxes]
    assert len(manifest["accepted"]) == count * count and manifest["rejected"] == []
    widths = {box[2] - box[0] for box in boxes}
    assert max(widths) - min(widths) <= 1 and manifest["geometry"]["grid_rounding"] == "nearest"


def test_anchor_puts_art_on_the_ground_repro_20(tmp_path):
    """repro_20, MAP-02: placing anchor_px on the ground point leaves 0 px between art and ground,
    even when padding is clamped by the cell edge (the old bottom-of-image rule floated 8 and 3 px)."""
    native_sheet(128, 64, {(20, 10, 44, 50): (40, 120, 50, 255), (84, 30, 108, 61): (120, 120, 120, 255)}).save(
        tmp_path / "sheet.png")
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "2",
                   "--labels", "tree,rock", "--output-dir", str(tmp_path / "props"))
    for item, ground in zip(manifest["accepted"], (50, 61)):
        art = pixels(tmp_path / "props" / item["image"])
        rows = np.flatnonzero(art[..., 3].any(axis=1))
        ax, ay = item["anchor_px"]
        placed_y = 100
        top = placed_y - ay  # where a compositor pastes the image so anchor_px lands on (x, 100)
        assert top + rows[-1] + 1 == placed_y  # float 0 px
        assert item["source_rect"][1] + ay == ground and float(ax).is_integer()
        assert item["padding"][3] == item["output_size"][1] - (rows[-1] + 1)
    assert manifest["accepted"][0]["padding"] == [8, 8, 8, 8]
    assert manifest["accepted"][1]["padding"] == [8, 8, 8, 3]


def test_anchor_modes_on_an_asymmetric_signpost(tmp_path):
    """The default stance anchor uses the bottom quarter of the art, so a signpost anchors on its post,
    not under its board; --anchor-mode bbox gives the box centre instead."""
    native_sheet(128, 112, {(30, 20, 40, 100): (110, 80, 50, 255), (30, 20, 90, 45): (140, 100, 60, 255)}).save(
        tmp_path / "sign.png")
    common = ["--input", str(tmp_path / "sign.png"), "--rows", "1", "--cols", "1", "--labels", "sign",
              "--component-padding", "0"]
    default = run(tmp_path, *common, "--output-dir", str(tmp_path / "stance"))
    boxed = run(tmp_path, *common, "--output-dir", str(tmp_path / "bbox"), "--anchor-mode", "bbox")
    assert default["accepted"][0]["anchor_px"] == [5, 80] and default["geometry"]["anchor_mode"] == "stance"
    assert boxed["accepted"][0]["anchor_px"] == [30, 80] and boxed["geometry"]["anchor_mode"] == "bbox"


def test_runs_are_byte_deterministic(tmp_path):
    """Appendix D: the same inputs give the same bytes (no timestamps, no absolute paths)."""
    aa_chroma_sheet(2, 2, 48).save(tmp_path / "sheet.png")
    for name in ("first", "second"):
        run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "2", "--cols", "2", "--output-dir",
            str(tmp_path / name), "--suggest-footprint", "ellipse", "--world-scale", "1/2")
    first = {path.relative_to(tmp_path / "first"): path.read_bytes() for path in (tmp_path / "first").rglob("*.*")}
    second = {path.relative_to(tmp_path / "second"): path.read_bytes() for path in (tmp_path / "second").rglob("*.*")}
    assert first == second and len(first) == 5


def test_trim_keeps_sheet_coordinates_repro_19(tmp_path):
    """repro_19, MAP-18: with --trim-border the boxes stay in cell and sheet coordinates."""
    native_sheet(128, 64, {(84, 20, 104, 40): (90, 60, 30, 255), (20, 20, 30, 30): (30, 90, 60, 255)}).save(
        tmp_path / "sheet.png")
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "2",
                   "--labels", "bush,crate", "--output-dir", str(tmp_path / "props"), "--trim-border", "4",
                   "--component-padding", "0", "--min-component-area", "1")
    crate = manifest["accepted"][1]
    assert crate["source_rect"] == [84, 20, 104, 40]  # the real art's sheet rectangle
    assert crate["trim_offset"] == [20, 20] and crate["crop_bbox"] == [20, 20, 40, 40]
    assert crate["trim_applied"] == 4 and manifest["trim_border"] == 4 and manifest["edge_clean_depth"] == 0


def test_cjk_labels_keep_display_names_repro_22(tmp_path):
    """repro_22, MAP-23: non-ASCII labels keep their text as display_name and get distinct ASCII folders."""
    native_sheet(64, 32, {(8, 8, 24, 24): (90, 60, 30, 255), (40, 8, 56, 24): (60, 60, 60, 255)}).save(
        tmp_path / "sheet.png")

    def labels(text: str) -> list[tuple[str, str]]:
        args = PROPS.build_parser().parse_args(["--input", "x", "--output-dir", "y", "--rows", "1", "--cols", "2",
                                                "--labels", text])
        return [(label.slug, label.display_name) for label in PROPS.parse_labels(args, 2)]

    assert labels("樹") == [("prop-1", "樹"), ("prop-2", "prop-2")]
    assert labels("樹,石頭") == [("prop-1", "樹"), ("prop-2", "石頭")]
    assert labels("oak 樹,oak 石頭") == [("oak-1", "oak 樹"), ("oak-2", "oak 石頭")]
    assert labels("樹，石頭") == labels("樹、石頭") == labels("樹,石頭")
    assert labels("Café,Ｔｒｅｅ") == [("cafe", "Café"), ("tree", "Ｔｒｅｅ")]
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "2",
                   "--labels", "樹,石頭", "--output-dir", str(tmp_path / "props"))
    assert [(item["label"], item["display_name"]) for item in manifest["accepted"]] == [
        ("prop-1", "樹"), ("prop-2", "石頭")]
    written = json.loads((tmp_path / "props" / "prop-pack.json").read_text(encoding="utf-8"))
    assert written["accepted"][1]["display_name"] == "石頭"
    with pytest.raises(ValueError, match="reserved Windows device name"):
        labels("crate,aux")  # repro_08


def test_manifest_v2_paths_hashes_and_contract(tmp_path):
    """MAP-24: manifest-relative POSIX paths with sha256; every v2 field validates; QA lists inputs/outputs."""
    image, boxes = make_magenta_sheet(2, 2, 64, fringe=True, return_boxes=True)
    image.save(tmp_path / "sheet.png")
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "2", "--cols", "2",
                   "--labels", "a,b,c,d", "--output-dir", str(tmp_path / "out"), "--manifest",
                   str(tmp_path / "meta" / "kit.json"), "--art-source", "host_image")
    written = json.loads((tmp_path / "meta" / "kit.json").read_text(encoding="utf-8"))
    validate(written)
    assert written["input"] == "../sheet.png" and written["art_source"] == "host_image"
    for item, box in zip(written["accepted"], boxes):
        image_path = (tmp_path / "meta" / item["image"]).resolve()
        assert item["image"].startswith("../out/") and image_path.is_file()
        assert item["sha256"] == forge_core.sha256_file(image_path)
        content = [item["source_rect"][0] + item["padding"][0], item["source_rect"][1] + item["padding"][1],
                   item["source_rect"][2] - item["padding"][2], item["source_rect"][3] - item["padding"][3]]
        # The 50/50 key-mixed ring is 160 RGB units from #FF00FF, so the legacy keyer keeps it as art;
        # the default despill neutralises it.
        assert content == [box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1] and item["edge_fringe_px"] == 0
    refs = {ref["path"]: ref["sha256"] for ref in written["qa"]["inputs"] + written["qa"]["outputs"]}
    assert refs["../sheet.png"] == written["source_sha256"] and len(refs) == 5
    assert all(not path.startswith("/") and ":" not in path for path in refs)
    assert manifest["accepted"] == written["accepted"]


@pytest.mark.skipif(os.name != "nt", reason="only Windows paths can lack a relative route (another drive)")
def test_paths_on_another_drive_are_stored_as_file_names():
    """MAP-24 with forge_core.portable_path's absolute fallback: a manifest never stores an absolute path."""
    assert PROPS._manifest_relative(Path("C:/art/sheet.png"), Path("D:/project/props")) == "sheet.png"
    assert PROPS._manifest_relative(Path("D:/art/sheet.png"), Path("D:/project/props")) == "../../art/sheet.png"


def test_keep_canvas_preserves_box_and_authored_anchor(tmp_path):
    """Probe (roadmap 2.3): exact code-art boxes keep their canvas and authored anchor instead of re-cropping."""
    sheet = np.zeros((52, 80, 4), np.uint8)
    sheet[1:44, 0:32] = (40, 120, 50, 255)        # a 32x44 tree whose top row is empty
    sheet[2:52, 32:80] = (150, 100, 60, 255)      # a 48x52 house whose top two rows are empty
    Image.fromarray(sheet).save(tmp_path / "sheet.png")
    (tmp_path / "boxes.json").write_text(json.dumps({"schema": "forge-crop-boxes/v1", "items": [
        {"id": "tree", "box": [0, 0, 32, 44], "anchor_px": [16, 44]},
        {"id": "house", "box": [32, 0, 80, 52], "anchor_px": [24, 52], "solid": True, "occlusion_class": "tall"},
    ]}), encoding="utf-8")
    common = ["--input", str(tmp_path / "sheet.png"), "--boxes-file", str(tmp_path / "boxes.json"),
              "--component-mode", "all", "--min-component-area", "1", "--component-padding", "0"]
    kept = run(tmp_path, *common, "--output-dir", str(tmp_path / "kept"), "--keep-canvas")
    assert [item["output_size"] for item in kept["accepted"]] == [[32, 44], [48, 52]]
    assert [item["anchor_px"] for item in kept["accepted"]] == [[16, 44], [24, 52]]
    assert all(item["anchor_source"] == "authored" for item in kept["accepted"])
    assert kept["accepted"][1]["solid"] is True and kept["accepted"][1]["occlusion_class"] == "tall"
    cropped = run(tmp_path, *common, "--output-dir", str(tmp_path / "cropped"))
    tree, house = cropped["accepted"]
    assert tree["output_size"] == [32, 43] and tree["anchor_px"] == [16, 43]  # same sheet point
    assert house["output_size"] == [48, 50] and house["anchor_px"] == [24, 50]


@pytest.mark.parametrize("document", [
    {"items": [{"id": "tree", "box": [0, 0, 12, 20]}, {"id": "rock", "box": [14, 14, 28, 24]}]},
    {"props": [{"label": "tree", "source_box": [0, 0, 12, 20]}, {"label": "rock", "source_box": [14, 14, 28, 24]}]},
])
def test_unified_and_legacy_box_files_agree(tmp_path, document):
    """DOC-18: the unified {"items": [{"id", "box"}]} file and the legacy props alias give the same pack."""
    native_sheet(30, 24, {(3, 3, 9, 17): (255, 0, 255, 128), (18, 18, 24, 21): (60, 120, 30, 255)}).save(
        tmp_path / "props.png")
    (tmp_path / "boxes.json").write_text(json.dumps(document), encoding="utf-8")
    manifest = run(tmp_path, "--input", str(tmp_path / "props.png"), "--boxes-file", str(tmp_path / "boxes.json"),
                   "--output-dir", str(tmp_path / "out"), "--component-padding", "0", "--reject-edge-touch")
    assert [item["label"] for item in manifest["accepted"]] == ["tree", "rock"]
    assert [item["output_size"] for item in manifest["accepted"]] == [[6, 14], [6, 3]]
    assert manifest["layout_mode"] == "explicit_boxes" and manifest["boxes_file"] == "../boxes.json"


@pytest.mark.parametrize("document,message", [
    ({"schema": "other/v1", "items": [{"id": "a", "box": [0, 0, 4, 4]}]}, "schema"),
    ({"items": [{"box": [0, 0, 4, 4]}]}, "nonempty string label"),
    ({"items": [{"id": "a", "box": [0, 0, 4.5, 4]}]}, "four integer"),
    ({"items": [{"id": "a", "box": [0, 0, 10, 10]}, {"id": "b", "box": [8, 8, 16, 16]}]}, "overlaps"),
    ({"items": [{"id": "a", "box": [12, 12, 17, 16]}]}, "inside the source canvas"),
    ({"items": [{"id": "a", "box": [0, 0, 8, 8], "anchor_px": [9, 2]}]}, "anchor_px"),
    ({"items": [{"id": "a", "box": [0, 0, 8, 8], "footprint": {"shape": "ellipse"}}]}, "footprint width"),
    ({"items": [{"id": "a", "box": [0, 0, 8, 8], "occlusion_class": "canopy"}]}, "occlusion_class"),
    ({"props": [{"label": "-", "source_box": [0, 0, 8, 8]}]}, "must name props"),
    ({"items": []}, "nonempty"),
])
def test_crop_boxes_are_validated(tmp_path, document, message):
    """DOC-18: unknown schemas, missing ids, fractional, overlapping or outside boxes and bad fields are refused."""
    path = tmp_path / "boxes.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        PROPS.read_crop_boxes(path, (16, 16))


def test_min_component_area_scales_with_the_cell(tmp_path):
    """Probe (roadmap 4.11): a 16 px coin (88 px) and a two-part sword (19 px) are no longer judged empty."""
    sheet = np.zeros((16, 32, 4), np.uint8)
    yy, xx = np.mgrid[0:16, 0:16]
    sheet[:, :16][((xx - 7.5) ** 2 + (yy - 7.5) ** 2) <= 28] = (230, 190, 40, 255)
    sheet[1:12, 23] = (200, 200, 210, 255)   # blade, 11 px
    sheet[11, 20:27] = (110, 70, 40, 255)    # guard, 6 more px
    sheet[13:15, 23] = (110, 70, 40, 255)    # pommel, a separate 2 px part
    Image.fromarray(sheet).save(tmp_path / "small.png")
    common = ["--input", str(tmp_path / "small.png"), "--rows", "1", "--cols", "2", "--labels", "coin,sword"]
    manifest = run(tmp_path, *common, "--output-dir", str(tmp_path / "auto"), "--component-mode", "all")
    assert [item["kept_area"] for item in manifest["accepted"]] == [88, 19]
    assert manifest["accepted"][1]["component_count"] == 2
    assert {item["min_component_area"] for item in manifest["accepted"]} == {1}
    assert manifest["min_component_area"] == "auto"
    with pytest.raises(ValueError, match="No prop was accepted"):
        run(tmp_path, *common, "--output-dir", str(tmp_path / "legacy"), "--min-component-area", "100")
    assert PROPS.auto_min_area(418 * 418) == 100 and PROPS.auto_min_area(1024 * 1024) == 100
    assert PROPS.auto_min_area(256 * 256) == 38 and PROPS.auto_min_area(16 * 16) == 1


def test_read_manifest_upgrades_v1_and_passes_v2_through(tmp_path):
    """v1 manifests stay readable: cfed170 items gain cell_box, source_rect, padding and a bbox anchor."""
    legacy = Path(__file__).parent / "fixtures" / "contracts" / "map.prop_pack_v2.legacy-v1.json"
    view = PROPS.read_manifest(legacy)
    rock = view["accepted"][0]
    assert view["schema_version"] == 1
    assert rock["source_rect"] == [4, 4, 60, 60] and rock["padding"] == [8, 8, 8, 8]
    assert rock["anchor_px"] == [28, 48] and rock["anchor_source"] == "derived-v1"
    placeholder = copy.deepcopy(json.loads(legacy.read_text(encoding="utf-8")))
    placeholder["accepted"][1]["output_size"] = [0, 0]
    (tmp_path / "v1.json").write_text(json.dumps(placeholder), encoding="utf-8")
    assert PROPS.read_manifest(tmp_path / "v1.json")["accepted"][1]["status"] == "placeholder"
    native_sheet(16, 16, {(4, 4, 10, 12): (255, 0, 255, 128)}).save(tmp_path / "props.png")
    run(tmp_path, "--input", str(tmp_path / "props.png"), "--rows", "1", "--cols", "1", "--output-dir",
        str(tmp_path / "out"))
    v2 = PROPS.read_manifest(tmp_path / "out" / "prop-pack.json")
    assert v2 == json.loads((tmp_path / "out" / "prop-pack.json").read_text(encoding="utf-8"))
    (tmp_path / "bad.json").write_text(json.dumps({"schema": "x", "accepted": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported"):
        PROPS.read_manifest(tmp_path / "bad.json")


def test_chroma_fixture_through_the_cli_map_09(tmp_path):
    """MAP-09: a chroma sheet with anti-aliased edges and purple details goes through the CLI subprocess;
    default despill removes the edge fringe and leaves interior purple untouched."""
    sheet = aa_chroma_sheet(2, 2, 64)
    sheet.save(tmp_path / "sheet.png")
    result = run_cli([SCRIPT, "--input", tmp_path / "sheet.png", "--rows", "2", "--cols", "2", "--labels",
                      "bush,stump,crate,lamp", "--output-dir", tmp_path / "props"])
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "ok" and summary["accepted"] == 4 and summary["qa"] == "pass"
    assert Path(summary["manifest"]) == (tmp_path / "props" / "prop-pack.json").resolve()
    manifest = json.loads(Path(summary["manifest"]).read_text(encoding="utf-8"))
    validate(manifest)
    assert manifest["background_mode"] == "chroma_key" and manifest["despill_radius"] == 1
    legacy = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "2", "--cols", "2", "--labels",
                 "bush,stump,crate,lamp", "--output-dir", str(tmp_path / "legacy"), "--despill-radius", "0")
    for item, old in zip(manifest["accepted"], legacy["accepted"]):
        new_art, old_art = pixels(tmp_path / "props" / item["image"]), pixels(tmp_path / "legacy" / old["image"])
        assert item["edge_fringe_px"] == 0 < old["edge_fringe_px"]
        assert np.array_equal(new_art[..., 3], old_art[..., 3])  # despill never touches alpha
        x = item["cell_box"][0] + 32 - item["source_rect"][0]  # the purple dot at the cell centre,
        y = item["cell_box"][1] + 32 - item["source_rect"][1]  # far from any transparent pixel
        centre = (slice(y - 2, y + 2), slice(x - 2, x + 2))
        assert np.array_equal(new_art[centre], old_art[centre]) and tinted(new_art[centre]) == 16
    assert legacy["qa"]["status"] == "warn"
    assert {check["id"]: check["status"] for check in legacy["qa"]["checks"]}["edge_fringe"] == "warn"


# --------------------------------------------------------------------------- B10-T4: defaults and new options

def test_meadow_crop_despill_counts_repro_21(tmp_path):
    """repro_21/21b, MAP-03 on the real meadow crop: wheat-grass keeps 2419 tinted px with the cfed170
    key, 111 at the new default radius 1 and 36 at radius 2; the default run accepts all six props."""
    path = real_fixture("meadow-prop-pack-crop.png")
    image, _ = forge_core.load_rgba(path)
    sheet = np.asarray(image)
    counts = [tinted(PROPS.key_sheet(sheet, "chroma_key", despill_radius=radius)[0][:418, :418])
              for radius in (0, 1, 2)]
    assert counts == [2419, 111, 36]
    manifest = run(tmp_path, "--input", str(path), "--rows", "2", "--cols", "3", "--labels",
                   "wheat-grass,mossy-boulder,tree,log,signpost,flower-bush", "--output-dir", str(tmp_path / "props"))
    assert len(manifest["accepted"]) == 6 and manifest["despill_radius"] == 1
    wheat = pixels(tmp_path / "props" / "wheat-grass" / "prop.png")
    assert tinted(wheat) <= 111
    fringe = {check["id"]: check for check in manifest["qa"]["checks"]}["edge_fringe"]
    assert fringe["status"] == "pass" and fringe["value"] <= 0.02


def run_forest(tmp_path: Path, name: str, *extra: str) -> dict:
    if not (tmp_path / "forest.png").exists():
        Image.fromarray(forest_replica()).save(tmp_path / "forest.png")
    return run(tmp_path, "--input", str(tmp_path / "forest.png"), "--auto-boxes", "--labels", "tree,lantern,rock",
               "--output-dir", str(tmp_path / name), *extra)


def test_forest_replica_hygiene_leaves_three_components_and_the_measured_canvas(tmp_path):
    """report v2 P1-4/P1-6: alpha hygiene leaves exactly 3 components and the tree canvas is 536x672;
    the auto box of the lantern equals its hand-measured box."""
    replica = forest_replica()
    cleaned, report = forge_core.alpha_hygiene(replica, "both")
    assert forge_core.label_components(np.asarray(cleaned)[..., 3] > 0)[1] == 3
    haze = (replica[..., 3] >= 1) & (replica[..., 3] <= 4)
    assert report["floor_px"] == int(haze.sum()) > 30000 and report["detached_components"] == 4
    manifest = run_forest(tmp_path, "props")
    tree, lantern, rock = manifest["accepted"]
    assert manifest["hygiene"]["mode"] == "both" and manifest["auto_boxes"]["unowned_px"] == 0
    assert tree["output_size"] == [536, 672] and tree["cell_box"] == [77, 27, 613, 699]
    assert lantern["cell_box"] == [780, 135, 1091, 679]
    measured = [lantern["source_rect"][0] + lantern["padding"][0], lantern["source_rect"][1] + lantern["padding"][1],
                lantern["source_rect"][2] - lantern["padding"][2], lantern["source_rect"][3] - lantern["padding"][3]]
    assert measured == [788, 143, 1083, 671]  # the hand-measured lantern box
    assert rock["cell_box"] == [40, 843, 606, 1193]
    assert all(item["component_count"] == 1 and item["dropped_area"] == 0 for item in manifest["accepted"])
    assert manifest["component_mode"] == "all" and manifest["qa"]["status"] == "pass"
    boxes = json.loads((tmp_path / "props" / "auto-boxes.json").read_text(encoding="utf-8"))
    validate_crop_boxes(boxes)
    assert [item["box"] for item in boxes["items"]] == [tree["cell_box"], lantern["cell_box"], rock["cell_box"]]
    rerun = run(tmp_path, "--input", str(tmp_path / "forest.png"), "--boxes-file",
                str(tmp_path / "props" / "auto-boxes.json"), "--output-dir", str(tmp_path / "rerun"),
                "--alpha-hygiene", "both", "--component-mode", "all", "--reject-edge-touch")
    assert [item["sha256"] for item in rerun["accepted"]] == [item["sha256"] for item in manifest["accepted"]]


def test_forest_replica_without_hygiene_inflates_boxes(tmp_path):
    """Without hygiene the alpha 1-4 haze joins the props (the 608x692 tree of the cold run)."""
    manifest = run_forest(tmp_path, "raw", "--alpha-hygiene", "none")
    tree = manifest["accepted"][0]
    assert tree["output_size"][0] > 536 and tree["output_size"][1] > 672
    assert manifest["auto_boxes"]["unowned_px"] > 0 and manifest["qa"]["status"] == "warn"


def test_world_scale_padding_makes_anchor_error_zero(tmp_path):
    """report v2 P1-6: integer-exact padding at world scale 3/8 gives anchor error 0.000 px."""
    manifest = run_forest(tmp_path, "world", "--world-scale", "3/8")
    scale = Fraction(3, 8)
    for item in manifest["accepted"]:
        width, height = item["output_size"]
        ax, ay = item["anchor_px"]
        assert (width * scale).denominator == (height * scale).denominator == 1
        assert (ax * scale).denominator == (ay * scale).denominator == 1
        assert item["world_size"] == [int(width * scale), int(height * scale)]
        assert item["world_anchor"] == [int(ax * scale), int(ay * scale)]
        x, y = 838, 550  # integer placement: left = x - world anchor is exact, so the anchor lands on (x, y)
        assert (x - item["world_anchor"][0]) + ax * scale == x and (y - item["world_anchor"][1]) + ay * scale == y
    plain = run_forest(tmp_path, "plain")
    lantern = plain["accepted"][1]
    shown = round(lantern["output_size"][0] * 3 / 8)  # what a renderer draws without exact padding
    error = abs(lantern["anchor_px"][0] * shown / lantern["output_size"][0] - lantern["anchor_px"][0] * 3 / 8)
    assert error > 0.1  # the 0.25-0.5 px drift of the cold run
    assert manifest["geometry"]["world_scale"] == "3/8"


def test_world_scale_parsing():
    assert PROPS.parse_world_scale("0.375") == Fraction(3, 8) == PROPS.parse_world_scale("3/8")
    for text in ("0", "-1", "abc", "1/0", "0.333"):
        with pytest.raises(argparse.ArgumentTypeError):
            PROPS.parse_world_scale(text)


def test_suggested_footprint_follows_the_support_band(tmp_path):
    """B10-T4 (Dusk world-data footprints): the suggestion spans the trunk, not the canopy; front edge on the ground."""
    sheet = np.zeros((64, 128, 4), np.uint8)
    yy, xx = np.mgrid[0:64, 0:64]
    sheet[:, :64][((xx - 32) / 26.0) ** 2 + ((yy - 24) / 18.0) ** 2 <= 1] = (40, 110, 50, 255)
    sheet[30:58, 27:37] = (95, 65, 40, 255)          # 10 px trunk down to row 57
    sheet[20:56, 70:120] = (150, 110, 70, 255)       # a 50 px crate
    Image.fromarray(sheet).save(tmp_path / "sheet.png")
    common = ["--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "2", "--labels", "tree,crate"]
    tree, crate = run(tmp_path, *common, "--output-dir", str(tmp_path / "ellipse"),
                      "--suggest-footprint", "ellipse")["accepted"]
    assert tree["footprint"]["shape"] == "ellipse" and tree["footprint"]["width"] == 10
    assert tree["footprint"]["depth"] == 5 and tree["footprint"]["offset"] == [0, -2.5]
    assert crate["footprint"]["width"] == 50 and crate["footprint"]["offset"] == [0, -12.5]
    rect = run(tmp_path, *common, "--output-dir", str(tmp_path / "rect"), "--suggest-footprint", "rect",
               "--footprint-depth-ratio", "0.25")["accepted"][1]["footprint"]
    assert rect["shape"] == "rect" and rect["depth"] == 12.5 and rect["suggested"] is True


def test_tall_props_in_a_square_grid_warn(tmp_path):
    """asf-improved strategy gate: aspect above 1.6 in a square grid is flagged, not silently accepted."""
    native_sheet(128, 64, {(27, 6, 37, 58): (40, 110, 50, 255), (72, 20, 120, 50): (120, 120, 115, 255)}).save(
        tmp_path / "sheet.png")
    manifest = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "2",
                   "--labels", "lamp,rock", "--output-dir", str(tmp_path / "props"))
    check = {check["id"]: check for check in manifest["qa"]["checks"]}["strategy_aspect"]
    assert check["status"] == "warn" and check["value"] == ["lamp 10x52"]
    assert manifest["qa"]["status"] == "warn" and any("square grid" in text for text in manifest["warnings"])
    (tmp_path / "boxes.json").write_text(json.dumps({"items": [{"id": "lamp", "box": [0, 0, 64, 64]}]}),
                                         encoding="utf-8")
    boxed = run(tmp_path, "--input", str(tmp_path / "sheet.png"), "--boxes-file", str(tmp_path / "boxes.json"),
                "--output-dir", str(tmp_path / "boxed"))
    assert {check["id"]: check["status"] for check in boxed["qa"]["checks"]}["strategy_aspect"] == "skipped"


def test_alpha_floor_is_an_alias_of_hygiene_floor(tmp_path):
    """B10-T4: --alpha-floor N means --alpha-hygiene floor at N; detached also drops faint islands."""
    sheet = native_sheet(32, 32, {(8, 8, 24, 24): (90, 60, 30, 255), (0, 0, 2, 2): (200, 200, 200, 3),
                                  (28, 28, 31, 31): (200, 200, 200, 12)})
    sheet.save(tmp_path / "sheet.png")
    common = ["--input", str(tmp_path / "sheet.png"), "--rows", "1", "--cols", "1", "--component-mode", "all",
              "--min-component-area", "1"]
    alias = run(tmp_path, *common, "--output-dir", str(tmp_path / "alias"), "--alpha-floor", "4")
    named = run(tmp_path, *common, "--output-dir", str(tmp_path / "named"), "--alpha-hygiene", "floor")
    assert alias["hygiene"] == named["hygiene"] and alias["alpha_floor_pixels_removed"] == 4
    assert alias["accepted"][0]["sha256"] == named["accepted"][0]["sha256"]
    assert alias["accepted"][0]["kept_area"] == 256 + 9
    both = run(tmp_path, *common, "--output-dir", str(tmp_path / "both"), "--alpha-hygiene", "both")
    assert both["hygiene"]["detached_px"] == 9 and both["accepted"][0]["kept_area"] == 256
    assert both["accepted"][0]["edge_touch"] is False


def test_auto_boxes_need_matching_labels(tmp_path):
    """--auto-boxes with the wrong number of labels fails and lists the boxes it found."""
    Image.fromarray(forest_replica()).save(tmp_path / "forest.png")
    with pytest.raises(ValueError, match=r"found 3 props but 2 labels.*\[77, 27, 613, 699\]"):
        run(tmp_path, "--input", str(tmp_path / "forest.png"), "--auto-boxes", "--labels", "tree,rock",
            "--output-dir", str(tmp_path / "out"))
    manifest = run(tmp_path, "--input", str(tmp_path / "forest.png"), "--auto-boxes", "--labels", "tree,-,rock",
                   "--output-dir", str(tmp_path / "skip"))
    assert [item["label"] for item in manifest["accepted"]] == ["tree", "rock"]
    assert manifest["rejected"][0]["status"] == "skipped-label"


# --------------------------------------------------------------------------- legacy behaviour kept from cfed170

def legacy_fixture(root: Path, edge: bool = False) -> None:
    image = Image.new("RGBA", (16, 16))
    for x in range(0 if edge else 4, 10):
        for y in range(4, 12):
            image.putpixel((x, y), (255, 0, 255, 128))
    image.save(root / "props.png")


def legacy_args(root: Path, *extra: str) -> list[str]:
    return ["--input", str(root / "props.png"), "--output-dir", str(root / "out"), "--rows", "1", "--cols", "1",
            "--labels", "purple-tree", "--min-component-area", "1", *extra]


def test_auto_preserves_native_purple_alpha(tmp_path):
    legacy_fixture(tmp_path)
    result = run(tmp_path, *legacy_args(tmp_path))
    assert result["background_mode"] == "native_alpha"
    with Image.open(tmp_path / "out/purple-tree/prop.png") as out:
        assert (255, 0, 255, 128) in [value for _, value in out.getcolors()]


def test_strict_edges_fail_before_images_publish_even_with_trim(tmp_path):
    legacy_fixture(tmp_path, edge=True)
    with pytest.raises(ValueError, match="cell edge"):
        PROPS.extract(PROPS.build_parser().parse_args(legacy_args(tmp_path, "--trim-border", "1",
                                                                  "--reject-edge-touch")))
    assert not (tmp_path / "out").exists()


def test_duplicate_normalized_labels_fail():
    args = PROPS.build_parser().parse_args(["--input", "x", "--output-dir", "y", "--rows", "1", "--cols", "2",
                                            "--labels", "Oak Tree,oak-tree"])
    with pytest.raises(ValueError, match="unique"):
        PROPS.parse_labels(args, 2)


def test_non_divisible_grid_fails():
    with pytest.raises(ValueError, match="exactly"):
        PROPS.grid_boxes(17, 16, 1, 2)


def test_all_mode_preserves_separate_parts_without_alpha_squaring(tmp_path):
    image = Image.new("RGBA", (12, 12))
    image.putpixel((3, 3), (200, 10, 200, 128))
    image.putpixel((8, 8), (200, 10, 200, 128))
    image.save(tmp_path / "props.png")
    result = run(tmp_path, *legacy_args(tmp_path, "--component-mode", "all"))
    assert result["accepted"][0]["component_count"] == 2
    with Image.open(tmp_path / "out/purple-tree/prop.png") as out:
        assert out.getchannel("A").histogram()[128] == 2


def test_inspected_boxes_recover_complete_tall_prop_without_resizing(tmp_path):
    image = Image.new("RGBA", (30, 24))
    for x in range(3, 9):
        for y in range(3, 17):
            image.putpixel((x, y), (255, 0, 255, 128))
    for x in range(18, 24):
        for y in range(18, 21):
            image.putpixel((x, y), (60, 120, 30, 255))
    image.save(tmp_path / "props.png")
    (tmp_path / "boxes.json").write_text(json.dumps({"props": [
        {"label": "tree", "source_box": [0, 0, 12, 20]},
        {"label": "rock", "source_box": [14, 14, 28, 24]},
    ]}), encoding="utf-8")
    result = run(tmp_path, "--input", str(tmp_path / "props.png"), "--boxes-file", str(tmp_path / "boxes.json"),
                 "--output-dir", str(tmp_path / "out"), "--background-mode", "native_alpha",
                 "--min-component-area", "1", "--component-padding", "0", "--reject-edge-touch")
    assert result["layout_mode"] == "explicit_boxes"
    assert result["source_size"] == [30, 24]
    assert result["accepted"][0]["source_box"] == [0, 0, 12, 20]
    assert result["accepted"][0]["grid"] is None
    with Image.open(tmp_path / "out/tree/prop.png") as extracted:
        assert extracted.size == (6, 14)
        assert extracted.getchannel("A").histogram()[128] == 84
        assert extracted.getpixel((0, 0)) == (255, 0, 255, 128)


def test_alpha_floor_is_explicit_and_preserves_source_and_visible_alpha(tmp_path):
    legacy_fixture(tmp_path)
    with Image.open(tmp_path / "props.png") as opened:
        image = opened.copy()
    image.putpixel((0, 0), (255, 0, 255, 1))
    image.save(tmp_path / "props.png")
    original = (tmp_path / "props.png").read_bytes()
    with pytest.raises(ValueError, match="cell edge"):
        PROPS.extract(PROPS.build_parser().parse_args(legacy_args(tmp_path, "--reject-edge-touch")))
    result = run(tmp_path, *legacy_args(tmp_path, "--alpha-floor", "4", "--reject-edge-touch"))
    assert result["alpha_floor_pixels_removed"] == 1
    assert (tmp_path / "props.png").read_bytes() == original
    with Image.open(tmp_path / "out/purple-tree/prop.png") as out:
        assert out.getchannel("A").histogram()[128] == 48
        assert (255, 0, 255, 128) in [value for _, value in out.getcolors()]


def test_small_alpha_floor_does_not_hide_real_edge_clipping(tmp_path):
    legacy_fixture(tmp_path, edge=True)
    with pytest.raises(ValueError, match="cell edge"):
        PROPS.extract(PROPS.build_parser().parse_args(legacy_args(tmp_path, "--alpha-floor", "4",
                                                                  "--reject-edge-touch")))
    assert not (tmp_path / "out").exists()


def test_boxes_reject_grid_flags_instead_of_guessing(tmp_path):
    with pytest.raises(ValueError, match="cannot be combined"):
        PROPS.extract(PROPS.build_parser().parse_args(legacy_args(tmp_path, "--boxes-file", "boxes.json")))


def test_crop_spec_cannot_be_overwritten_by_manifest(tmp_path):
    legacy_fixture(tmp_path)
    path = tmp_path / "boxes.json"
    path.write_text(json.dumps({"props": [{"label": "tree", "source_box": [0, 0, 16, 16]}]}), encoding="utf-8")
    original = path.read_bytes()
    args = PROPS.build_parser().parse_args([
        "--input", str(tmp_path / "props.png"), "--boxes-file", str(path),
        "--output-dir", str(tmp_path / "out"), "--manifest", str(path), "--min-component-area", "1"])
    with pytest.raises(ValueError, match="aliases"):
        PROPS.extract(args)
    assert path.read_bytes() == original
    assert not (tmp_path / "out").exists()


def test_manifest_inside_output_must_not_take_an_image_path(tmp_path):
    legacy_fixture(tmp_path)
    with pytest.raises(ValueError, match="distinct"):
        PROPS.extract(PROPS.build_parser().parse_args(legacy_args(
            tmp_path, "--manifest", str(tmp_path / "out" / "purple-tree" / "prop.png"))))
    assert not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- CLI conventions (Appendix D/E)

def test_cli_help_is_ascii_under_cp1252_and_cp950():
    assert_cli_help("generate2dmap", "extract_prop_pack")


def test_cli_refuses_an_existing_output_dir(tmp_path):
    legacy_fixture(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep.txt").write_text("mine", encoding="utf-8")
    result = run_cli([SCRIPT, *legacy_args(tmp_path)], "cp1252")
    assert result.returncode == 1 and result.stderr.startswith("error: Refusing to replace existing output")
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == ["keep.txt"]


def test_cli_qc_failure_publishes_nothing(tmp_path):
    legacy_fixture(tmp_path, edge=True)
    result = run_cli([SCRIPT, *legacy_args(tmp_path, "--reject-edge-touch")])
    assert result.returncode == 1 and "error: Accepted props touch a cell edge" in result.stderr
    assert result.stdout == "" and sorted(path.name for path in tmp_path.iterdir()) == ["props.png"]


def test_cli_all_empty_exits_1_and_usage_errors_exit_1(tmp_path):
    native_sheet(32, 16, {(2, 2, 4, 4): (90, 60, 30, 255)}).save(tmp_path / "props.png")
    result = run_cli([SCRIPT, "--input", tmp_path / "props.png", "--rows", "1", "--cols", "2", "--output-dir",
                      tmp_path / "out", "--min-component-area", "500"])
    assert result.returncode == 1 and "error: No prop was accepted" in result.stderr
    assert not (tmp_path / "out").exists()
    usage = run_cli([SCRIPT, "--input", tmp_path / "props.png"])
    assert usage.returncode == 1 and "error: the following arguments are required: --output-dir" in usage.stderr


def test_cli_summary_is_one_ascii_line_with_cjk_labels(tmp_path):
    native_sheet(64, 32, {(8, 8, 24, 24): (90, 60, 30, 255), (40, 8, 56, 24): (60, 60, 60, 255)}).save(
        tmp_path / "sheet.png")
    result = run_cli([SCRIPT, "--input", tmp_path / "sheet.png", "--rows", "1", "--cols", "2", "--labels",
                      "樹,石頭", "--output-dir", tmp_path / "props"], "cp1252")
    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii() and len(result.stdout.strip().splitlines()) == 1
    assert json.loads(result.stdout)["accepted"] == 2


# --------------------------------------------------------------------------- proposed crop-box contract

CROP_BOXES_DEF = {  # handoff/B10-map-prop-pack.md section 5, request 1 (common.schema.json /$defs/cropBoxes)
    "description": "forge-crop-boxes/v1: measured crop boxes in sheet pixels (DOC-18). extract_prop_pack reads it "
                   "with --boxes-file and writes it as auto-boxes.json; assemble_frames --crop-boxes should read it "
                   "too. Readers also accept the legacy {props: [{label, source_box}]} object and a bare list of boxes.",
    "type": "object",
    "required": ["items"],
    "properties": {
        "schema": {"const": "forge-crop-boxes/v1"},
        "source": {"$ref": "#/$defs/fileRef"},
        "items": {"type": "array", "minItems": 1, "items": {
            "type": "object",
            "required": ["id", "box"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "box": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 4, "maxItems": 4},
                "display_name": {"type": "string", "minLength": 1},
                "anchor_px": {"$ref": "#/$defs/point2"},
            },
        }},
    },
}


def validate_crop_boxes(document: dict) -> None:
    """Validate against the cropBoxes def requested in handoff/B10-map-prop-pack.md section 5,
    applied in memory to the vendored common.schema.json."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    common = json.loads((SKILLS_DIR / "generate2dmap" / "references" / "schemas" / "common.schema.json")
                        .read_text(encoding="utf-8"))
    common["$defs"]["cropBoxes"] = CROP_BOXES_DEF
    registry = Registry().with_resource(common["$id"], DRAFT202012.create_resource(common))
    validator = Draft202012Validator({"$ref": f"{common['$id']}#/$defs/cropBoxes"}, registry=registry)
    errors = [error.message for error in validator.iter_errors(document)]
    assert not errors, errors
    assert contract_validator("common", "fileRef", skill="generate2dmap").is_valid(document["source"])
