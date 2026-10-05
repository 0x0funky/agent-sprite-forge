#!/usr/bin/env python3
"""Slice a terrain atlas into validated, project-ready tiles.

Each atlas row is one terrain (a fill or a Wang corner transition row); each column is one variant.
Tiles are opaque square or rect fills, or RGBA overlays and rect, iso-diamond or hex tiles. Runtime
and material numbers are written only when given. QC checks contrast, variant similarity, drawn
border frames and shape coverage; --edge-policy seamless adds a wrap-aware resize and normalised
wrap and Wang seam checks. The output directory must be new: work is staged beside it and
published only after QC, so a failed run leaves nothing behind.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_matte  # noqa: E402


SCHEMA = "generate2dmap.terrain_tile_bundle.v2"
TOOL_NAME = "extract_terrain_tiles.py"
TOOL_VERSION = "2.0"
MANIFEST_NAME = "terrain-bundle.json"
SHAPES = ("square", "rect", "iso-diamond", "hex-pointy", "hex-flat")
LAYERS = ("base", "overlay")
BACKGROUND_MODES = ("auto", "opaque", "native_alpha", "chroma_key", "shape_fill")
LEGACY_RUNTIME_DEFAULTS = {"engine_target": "project-native", "world_size": 0.94, "surface_y": 0.011}
LEGACY_MATERIAL_DEFAULTS = {"roughness": 0.88, "emission_energy": 0.0}
LUMA = np.array([0.299, 0.587, 0.114])
LANCZOS_SUPPORT = 3.0

BORDER_DEPTHS = (1, 2, 3)  # outer lines tested for a drawn frame or gutter (MAP-10)
BORDER_REFERENCE = 3       # inner lines each band is compared with
BORDER_LINE_SHARE = 0.8    # share of the edge that must differ before it counts as a line
FILL_FLAT_SHARE = 0.9      # share of corner pixels near the fill colour for shape_fill

# Normalised seam ratio (MAP-14); same definition as extract_platform_strip.py.
SEAM_LOCAL_STEPS = 8
SEAM_WINDOW_ROWS = 4
SEAM_NEAR_STEPS = 3
SEAM_FLOOR = 1.0
DUPLICATE_RATIO = 0.25
DUPLICATE_FLOOR = 2.0
NOMINAL_SEAM_RATIO = 1.25
EPSILON = 1e-6


# --------------------------------------------------------------------------- arguments

def parse_row(value: str) -> tuple[str, int]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Use TERRAIN=ROW, for example plain=0.")
    name, row_text = value.split("=", 1)
    try:
        row = int(row_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Terrain row must be an integer.") from exc
    if not name.strip():
        raise argparse.ArgumentTypeError("Terrain names must not be empty.")
    return name.strip(), row


def parse_float_map(value: str) -> tuple[str, float]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Use TERRAIN=VALUE, for example fire=1.2.")
    name, number = value.split("=", 1)
    try:
        return name.strip(), float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Mapped value must be numeric.") from exc


def parse_wang(value: str) -> tuple[str, list[str], list[tuple[int, int, int, int]]]:
    """``NAME=MATERIAL/MATERIAL:MASK,MASK,...``; a mask lists corner materials TL, TR, BL, BR."""
    usage = "Use NAME=MATERIAL/MATERIAL:MASK,MASK,..., for example shore=water/grass:0001,0011,0111."
    if "=" not in value or ":" not in value.split("=", 1)[1]:
        raise argparse.ArgumentTypeError(usage)
    name, rest = value.split("=", 1)
    materials_text, masks_text = rest.split(":", 1)
    materials = [material.strip() for material in materials_text.split("/")]
    if not name.strip() or not 2 <= len(materials) <= 9 or not all(materials) or len(set(materials)) != len(materials):
        raise argparse.ArgumentTypeError("A Wang row names 2 to 9 distinct materials separated by '/'. " + usage)
    masks = []
    for token in masks_text.split(","):
        token = token.strip()
        if not re.fullmatch(r"[0-9]{4}", token):
            raise argparse.ArgumentTypeError(
                f"Wang mask {token!r} must be 4 digits: top-left, top-right, bottom-left, bottom-right material.")
        corners = tuple(int(digit) for digit in token)
        if max(corners) >= len(materials):
            raise argparse.ArgumentTypeError(f"Wang mask {token} uses material {max(corners)}, but only "
                                             f"{len(materials)} materials are listed.")
        masks.append(corners)
    return name.strip(), materials, masks


def _stem(name: str) -> str:
    """Lower-case ASCII letters and digits joined by '-'; empty for a name without any (CJK)."""
    return re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")


def terrain_id(name: str, row: int) -> str:
    """ASCII file stem of a terrain; names without ASCII letters or digits (CJK) become terrain-<row>."""
    return _stem(name) or f"terrain-{row}"


def validate_options(args: argparse.Namespace) -> None:
    if args.rows <= 0 or args.cols <= 0:
        raise ValueError("rows and cols must be positive integers.")
    for name in ("tile_size", "tile_width", "tile_height"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            raise ValueError(f"{name.replace('_', '-')} must be positive.")
    finite = ("min_contrast", "min_variant_difference", "max_border_delta", "min_shape_coverage",
              "max_shape_spill", "max_seam_ratio", "fill_tolerance")
    for name in finite + ("runtime_world_size", "surface_y", "roughness"):
        value = getattr(args, name)
        if value is not None and not math.isfinite(value):
            raise ValueError(f"{name.replace('_', '-')} must be finite.")
    if args.runtime_world_size is not None and args.runtime_world_size <= 0:
        raise ValueError("runtime-world-size must be positive.")
    if args.roughness is not None and not 0 <= args.roughness <= 1:
        raise ValueError("roughness must be in [0,1].")
    for name in ("min_contrast", "min_variant_difference", "max_border_delta", "min_shape_coverage", "max_shape_spill"):
        if not 0 <= getattr(args, name) <= 1:
            raise ValueError("QC thresholds must be in [0,1].")
    if args.max_seam_ratio <= 0:
        raise ValueError("max-seam-ratio must be positive.")
    if not 0 <= args.fill_tolerance <= 442:
        raise ValueError("fill-tolerance must be between 0 and 442 (RGB Euclidean distance).")
    for name in ("threshold", "edge_threshold"):
        if not 0 <= getattr(args, name) <= 442:
            raise ValueError(f"{name.replace('_', '-')} must be between 0 and 442 (RGB Euclidean distance).")
    rect = args.shape in ("square", "rect")
    if args.shape == "square" and (args.tile_width is not None or args.tile_height is not None):
        raise ValueError("--tile-width/--tile-height are for rect, iso-diamond and hex tiles; use --tile-size.")
    if args.shape != "square" and (args.tile_size is not None or args.cell_shape != "square"):
        raise ValueError(f"--tile-size and --cell-shape crop-square are for square tiles; {args.shape} tiles use "
                         "--tile-width and --tile-height.")
    if args.edge_policy == "seamless" and not (rect and args.layer == "base" and args.cell_shape == "square"):
        raise ValueError("--edge-policy seamless applies to uncropped base square or rect tiles; iso-diamond, "
                         "hex, overlay and crop-square tiles cannot be wrap-verified.")


def resolve_background_mode(args: argparse.Namespace) -> str:
    rect = args.shape in ("square", "rect")
    mode = args.background_mode
    if mode == "auto":
        mode = "opaque" if args.layer == "base" and rect else "native_alpha"
    if args.layer == "base" and rect and mode != "opaque":
        raise ValueError("Base square and rect tiles are opaque fills; use --layer overlay for transparent tiles.")
    if mode == "opaque" and not (args.layer == "base" and rect):
        raise ValueError(f"--background-mode opaque cannot make {args.shape} {args.layer} tiles: use native_alpha "
                         "(real transparency), chroma_key (magenta background) or shape_fill (flat corner colour "
                         "around iso-diamond or hex cells).")
    if mode == "shape_fill" and rect:
        raise ValueError("shape_fill keys the flat fill outside an iso-diamond or hex footprint; square and rect "
                         "cells have none.")
    return mode


def resolve_terrains(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = [row for _, row in args.terrain_row]
    terrains = [{"id": terrain_id(name, row), "display_name": name, "row": row} for name, row in args.terrain_row]
    if len({terrain["id"] for terrain in terrains}) != len(terrains):
        raise ValueError("Terrain names may only be mapped once (names are compared as file stems).")
    if len(rows) != args.rows or set(rows) != set(range(args.rows)):
        raise ValueError(f"Terrain rows must cover every row from 0 to {args.rows - 1} exactly once.")
    return sorted(terrains, key=lambda terrain: terrain["row"])


def _lookup(terrains: list[dict[str, Any]], name: str, what: str) -> dict[str, Any]:
    stem = _stem(name)
    for terrain in terrains:
        if name in (terrain["display_name"], terrain["id"]) or (stem and stem == terrain["id"]):
            return terrain
    known = ", ".join(terrain["display_name"] for terrain in terrains)
    raise ValueError(f"{what} references unknown terrain {name!r}; terrains are: {known}.")


def resolve_wang(args: argparse.Namespace, terrains: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for name, materials, masks in args.wang:
        terrain = _lookup(terrains, name, "--wang")
        if terrain["id"] in rows:
            raise ValueError(f"Wang masks for {terrain['display_name']} are given twice.")
        if len(masks) != args.cols:
            raise ValueError(f"--wang {name} lists {len(masks)} masks; the atlas has {args.cols} columns.")
        rows[terrain["id"]] = {"materials": materials, "masks": masks}
    return rows


def resolve_emissions(args: argparse.Namespace, terrains: list[dict[str, Any]]) -> dict[str, float]:
    emissions: dict[str, float] = {}
    for name, value in args.emission:
        if not math.isfinite(value) or value < 0:
            raise ValueError("Emission values must be finite and nonnegative.")
        identity = _lookup(terrains, name, "Emission")["id"]
        if identity in emissions:
            raise ValueError("Emission names may only be mapped once.")
        emissions[identity] = value
    return emissions


# --------------------------------------------------------------------------- geometry

def grid_cells(width: int, height: int, rows: int, cols: int, rounding: str) -> dict[tuple[int, int], tuple]:
    if rounding == "exact":
        if width % cols or height % rows:
            raise ValueError(f"Atlas {width}x{height} is not evenly divisible by {cols} columns x {rows} rows; "
                             "pass --grid-rounding nearest to slice with rounded cell edges (cells differ by at "
                             "most 1 px, no pixel lost).")
        cell_w, cell_h = width // cols, height // rows
        boxes = [(c * cell_w, r * cell_h, (c + 1) * cell_w, (r + 1) * cell_h) for r in range(rows) for c in range(cols)]
    else:
        boxes = forge_core.rounded_grid_boxes(width, height, rows, cols)
    return {(index // cols, index % cols): tuple(int(v) for v in box) for index, box in enumerate(boxes)}


def shape_polygon(shape: str, width: float, height: float) -> list[tuple[float, float]]:
    """Footprint corners in tile pixels (y down), clockwise on screen."""
    w, h = float(width), float(height)
    if shape == "iso-diamond":
        return [(w / 2, 0.0), (w, h / 2), (w / 2, h), (0.0, h / 2)]
    if shape == "hex-pointy":
        return [(w / 2, 0.0), (w, h / 4), (w, 3 * h / 4), (w / 2, h), (0.0, 3 * h / 4), (0.0, h / 4)]
    if shape == "hex-flat":
        return [(w / 4, 0.0), (3 * w / 4, 0.0), (w, h / 2), (3 * w / 4, h), (w / 4, h), (0.0, h / 2)]
    return [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]


def _local_shape_mask(shape: str, width: int, height: int, grow: float = 0.0) -> np.ndarray:
    """Pixels whose centre lies inside the footprint grown by ``grow`` px (exact plane partition at 0)."""
    polygon = shape_polygon(shape, width, height)
    ys, xs = np.mgrid[0:height, 0:width] + 0.5
    inside = np.ones((height, width), bool)
    for (x0, y0), (x1, y1) in zip(polygon, polygon[1:] + polygon[:1]):
        length = math.hypot(x1 - x0, y1 - y0)
        signed = ((x1 - x0) * (ys - y0) - (y1 - y0) * (xs - x0)) / length  # >= 0 inside (clockwise on screen)
        inside &= signed >= -grow - 1e-9
    return inside


def resolve_output_size(args: argparse.Namespace, cells: dict[tuple[int, int], tuple]) -> tuple[int, int]:
    widths = [box[2] - box[0] for box in cells.values()]
    heights = [box[3] - box[1] for box in cells.values()]
    if args.shape == "square":
        uneven = max(abs(w - h) for w, h in zip(widths, heights))
        if uneven and args.cell_shape != "crop-square" and not (args.grid_rounding == "nearest" and uneven <= 1):
            raise ValueError("Non-square source cells require explicit --cell-shape crop-square; cropping loses "
                             "pixels. For rect, iso-diamond or hex tiles use --shape with --tile-width and "
                             "--tile-height instead.")
        side = args.tile_size or min(min(widths), min(heights))
        return side, side
    cell_w, cell_h = min(widths), min(heights)
    if args.tile_width is None and args.tile_height is None:
        return cell_w, cell_h
    width = args.tile_width or max(1, math.floor(args.tile_height * cell_w / cell_h + 0.5))
    height = args.tile_height or max(1, math.floor(args.tile_width * cell_h / cell_w + 0.5))
    stretch = abs((width / cell_w) / (height / cell_h) - 1.0)
    if stretch > 1.0 / min(cell_w, cell_h) + 0.01:
        raise ValueError(f"A {width}x{height} tile would stretch the {cell_w}x{cell_h} cells by {stretch:.1%}; "
                         "keep the cell aspect ratio.")
    return width, height


# --------------------------------------------------------------------------- background and resize

def prepare_atlas(atlas: np.ndarray, args: argparse.Namespace, mode: str) -> tuple[np.ndarray, dict[str, Any]]:
    alpha = atlas[..., 3]
    report: dict[str, Any] = {}
    if mode in ("opaque", "shape_fill") and alpha.min() != 255:
        if mode == "opaque":
            raise ValueError("Terrain atlas must be opaque; transparency cannot be discarded. Transparent tiles "
                             "need --layer overlay, or --shape iso-diamond/hex with --background-mode native_alpha.")
        raise ValueError("shape_fill keys a flat opaque corner colour, but this atlas has transparency: use "
                         "--background-mode native_alpha.")
    if mode == "native_alpha" and alpha.min() == 255:
        raise ValueError("native_alpha needs real transparency, but this atlas is fully opaque: use --background-mode "
                         "shape_fill (flat corner colour around iso-diamond or hex cells) or chroma_key (magenta).")
    if mode != "chroma_key":
        return atlas, report
    keyed = np.array(forge_matte.legacy_hard_key(atlas, args.threshold, args.edge_threshold))
    report.update({"keyer": "forge_matte.legacy_hard_key", "threshold": args.threshold,
                   "edge_threshold": args.edge_threshold, "despill_radius": args.despill_radius})
    if args.despill_radius:
        keyed, despill = forge_matte.despill(keyed, "edge", args.despill_radius)
        report["despill_changed_px"] = despill["changed_px"]
    return keyed, report


def _key_shape_fill(cell: np.ndarray, shape: str, tolerance: float) -> tuple[np.ndarray, dict[str, Any]]:
    """Make the flat fill around an iso-diamond or hex footprint transparent (MAP-11).

    The fill colour is the median of the cell corners well outside the footprint. Pixels within
    ``tolerance`` of it that connect (8-way, through such pixels) to those corners become
    transparent; everything else, including art that overhangs the footprint, stays opaque.
    """
    height, width = cell.shape[:2]
    corners = ~_local_shape_mask(shape, width, height, grow=2.0)
    if corners.sum() < 4:
        raise ValueError(f"A {width}x{height} {shape} cell leaves no corner fill to sample.")
    rgb = cell[..., :3].astype(np.float64)
    fill = np.median(rgb[corners], axis=0)
    near = np.sqrt(((rgb - fill) ** 2).sum(axis=2)) <= tolerance
    share = float(near[corners].mean())
    if share < FILL_FLAT_SHARE:
        raise ValueError(f"The corners outside the {shape} footprint are not one flat colour ({share:.0%} within "
                         f"{tolerance:g} of the median); use native_alpha or chroma_key, or raise --fill-tolerance.")
    labels, _count = forge_core.label_components(near, 8)
    touching = np.unique(labels[corners & near])
    background = np.isin(labels, touching[touching > 0])
    keyed = cell.copy()
    keyed[background] = 0
    return keyed, {"fill_rgb": [int(math.floor(v + 0.5)) for v in fill], "corner_fill_share": round(share, 6),
                   "keyed_px": int(background.sum())}


def _local_wrap_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Lanczos-resize a periodic tile as one period of an endless repeat (MAP-15).

    The tile is padded with its own opposite edges, wider than the filter reach, and Pillow
    resizes only the original box, so filter windows wrap instead of clamping at the edges.
    """
    width, height = image.size
    pad_x = math.ceil(LANCZOS_SUPPORT * max(1.0, width / size[0])) + 2
    pad_y = math.ceil(LANCZOS_SUPPORT * max(1.0, height / size[1])) + 2
    padded = np.pad(np.asarray(image), ((pad_y, pad_y), (pad_x, pad_x), (0, 0)), mode="wrap")
    return Image.fromarray(padded).resize(size, Image.Resampling.LANCZOS,
                                          box=(pad_x, pad_y, pad_x + width, pad_y + height))


def resize_tile(cell: np.ndarray, size: tuple[int, int], *, resampler: str, opaque: bool, wrap: bool) -> np.ndarray:
    """Opaque fills resize in RGB exactly as before (wrap-aware when seamless); RGBA tiles premultiplied."""
    if (cell.shape[1], cell.shape[0]) == size:
        return cell
    image = Image.fromarray(cell)
    if resampler == "nearest":
        return np.array(image.resize(size, Image.Resampling.NEAREST))
    if opaque:
        rgb = image.convert("RGB")
        resized = _local_wrap_resize(rgb, size) if wrap else rgb.resize(size, Image.Resampling.LANCZOS)
        return np.array(resized.convert("RGBA"))
    return np.array(forge_core.resample_rgba(image, size[0] / image.width, "lanczos", out_size=size))


# --------------------------------------------------------------------------- measurements

def tile_metrics(pixels: np.ndarray) -> dict[str, float]:
    """Luminance mean and contrast (0..1): of the whole opaque tile, else of its visible pixels."""
    luminance = Image.fromarray(np.ascontiguousarray(pixels[..., :3])).convert("L")
    alpha = pixels[..., 3]
    if alpha.min() == 255:
        stat = ImageStat.Stat(luminance)
        return {"mean_luminance": round(stat.mean[0] / 255.0, 6), "contrast": round(stat.stddev[0] / 255.0, 6)}
    visible = forge_core.subject_mask(alpha)
    if not visible.any():
        return {"mean_luminance": 0.0, "contrast": 0.0, "visible_fraction": 0.0}
    stat = ImageStat.Stat(luminance, Image.fromarray(visible.astype(np.uint8) * 255))
    return {"mean_luminance": round(stat.mean[0] / 255.0, 6), "contrast": round(stat.stddev[0] / 255.0, 6),
            "visible_fraction": round(float(visible.mean()), 6)}


def _premultiplied_rgb(pixels: np.ndarray) -> Image.Image:
    if pixels[..., 3].min() == 255:
        return Image.fromarray(np.ascontiguousarray(pixels[..., :3]))
    weighted = pixels[..., :3].astype(np.float64) * pixels[..., 3:].astype(np.float64) / 255.0
    return Image.fromarray(np.floor(weighted + 0.5).astype(np.uint8))


def normalized_difference(left: np.ndarray, right: np.ndarray) -> float:
    """Mean absolute difference (0..1) of 64x64 Lanczos thumbnails of the premultiplied colour."""
    size = (64, 64)
    left_small = _premultiplied_rgb(left).resize(size, Image.Resampling.LANCZOS)
    right_small = _premultiplied_rgb(right).resize(size, Image.Resampling.LANCZOS)
    difference = ImageChops.difference(left_small, right_small)
    return round(statistics.fmean(ImageStat.Stat(difference).mean) / 255.0, 6)


def border_frame(pixels: np.ndarray, threshold: float) -> dict[str, Any]:
    """Detect drawn grid lines, gutters or frames along opposite tile edges (MAP-10).

    For depths of 1-3 px, each edge band is compared with the 3 lines just inside it, along the
    whole edge. A frame is a band that is lighter (or darker) than its neighbours by more than
    ``threshold`` (luminance 0..1) along at least 80% of BOTH opposite edges: that line repeats on
    every tile boundary of a map. Art that crosses one edge, or texture that wraps, does not.
    """
    luminance = pixels[..., :3].astype(np.float64) @ LUMA / 255.0
    frames = []
    checked = False
    for axis, plane in (("x", luminance), ("y", luminance.T)):
        size = plane.shape[1]
        for depth in BORDER_DEPTHS:
            if 2 * (depth + BORDER_REFERENCE) > size:
                break
            checked = True
            first = plane[:, :depth].mean(axis=1) - plane[:, depth:depth + BORDER_REFERENCE].mean(axis=1)
            last = (plane[:, size - depth:].mean(axis=1)
                    - plane[:, size - depth - BORDER_REFERENCE:size - depth].mean(axis=1))
            for sign, tone in ((1.0, "lighter"), (-1.0, "darker")):
                if ((sign * first > threshold).mean() >= BORDER_LINE_SHARE
                        and (sign * last > threshold).mean() >= BORDER_LINE_SHARE):
                    delta = min(float(np.median(np.abs(first))), float(np.median(np.abs(last))))
                    frames.append({"axis": axis, "depth_px": depth, "tone": tone, "delta": round(delta, 6)})
    return {"checked": checked, "frame": bool(frames), "frames": frames,
            "max_delta": max((frame["delta"] for frame in frames), default=0.0)}


def shape_qc(pixels: np.ndarray, footprint: np.ndarray) -> dict[str, Any]:
    visible = forge_core.subject_mask(pixels[..., 3])
    area = int(footprint.sum())
    spill = int((visible & ~footprint).sum())
    return {"footprint_px": area, "coverage": round(int((visible & footprint).sum()) / area, 6),
            "spill_px": spill, "spill_fraction": round(spill / area, 6)}


def _premultiplied(pixels: np.ndarray) -> np.ndarray:
    values = pixels.astype(np.float64)
    values[..., :3] *= values[..., 3:] / 255.0
    return values


def _window_max(values: np.ndarray, window: int) -> float:
    """Largest mean over ``window`` consecutive rows of any column of ``values`` (rows x columns)."""
    if values.size == 0:
        return 0.0
    window = min(window, values.shape[0])
    sums = np.cumsum(np.pad(values, ((1, 0), (0, 0))), axis=0)
    return float(((sums[window:] - sums[:-window]) / window).max())


def _local_edge_seam_report(left: np.ndarray, right: np.ndarray, *, left_start: int = 0,
                            right_stop: int | None = None, gate: float | None = None) -> dict[str, Any]:
    """Normalised seam of the join ``left[:, -1] | right[:, 0]`` (MAP-14).

    ``seam`` is the premultiplied RGBA step across the join; ``adjacent_*`` describe the interior
    steps within SEAM_LOCAL_STEPS columns of it, on both sides, never using columns of ``left``
    before ``left_start`` or of ``right`` from ``right_stop`` on (outer cap padding).
    ``seam_ratio`` is the larger of seam / adjacent_max and worst SEAM_WINDOW_ROWS-row window of
    the join / worst window of any of those steps: about 1 or less for a join that looks like the
    art, well above 1 for a seam. A join much flatter than its neighbourhood is a duplicated edge.
    The output follows common seamReport plus ``seam_ratio``, ``near_median`` and ``verdict``.
    """
    a = _premultiplied(left[:, left_start:][:, -(SEAM_LOCAL_STEPS + 1):])  # only the columns near the join
    b = _premultiplied(right[:, :right_stop][:, :SEAM_LOCAL_STEPS + 1])
    join_rows = np.abs(a[:, -1] - b[:, 0]).mean(axis=1)
    left_steps = np.abs(np.diff(a, axis=1)).mean(axis=2)
    right_steps = np.abs(np.diff(b, axis=1)).mean(axis=2)
    local = np.concatenate([left_steps, right_steps], axis=1)
    means = local.mean(axis=0) if local.size else np.zeros(1)
    near = np.concatenate([left_steps[:, -SEAM_NEAR_STEPS:].mean(axis=0) if left_steps.size else np.zeros(0),
                           right_steps[:, :SEAM_NEAR_STEPS].mean(axis=0) if right_steps.size else np.zeros(0)])
    seam = float(join_rows.mean())
    median, p95, peak = float(np.median(means)), float(np.percentile(means, 95)), float(means.max())
    near_median = float(np.median(near)) if near.size else 0.0
    ratio = max(seam / max(peak, EPSILON),
                _window_max(join_rows[:, None], SEAM_WINDOW_ROWS) / max(_window_max(local, SEAM_WINDOW_ROWS), EPSILON))
    if near_median >= DUPLICATE_FLOOR and seam < DUPLICATE_RATIO * near_median:
        verdict = "duplicate_edge"
    elif seam > SEAM_FLOOR and ratio > (gate if gate is not None else NOMINAL_SEAM_RATIO):
        verdict = "seam"
    else:
        verdict = "continuous"
    return {
        "seam": round(seam, 6), "adjacent_median": round(median, 6), "adjacent_p95": round(p95, 6),
        "adjacent_max": round(peak, 6), "seam_over_median": round(seam / max(median, EPSILON), 6),
        "seam_over_p95": round(seam / max(p95, EPSILON), 6), "seam_ratio": round(ratio, 6),
        "near_median": round(near_median, 6), "verdict": verdict,
        "method": (f"premultiplied RGBA column steps (0-255); join vs interior steps within {SEAM_LOCAL_STEPS} "
                   f"columns per side, whole edge and worst {SEAM_WINDOW_ROWS}-row window"),
    }


def _transposed(pixels: np.ndarray) -> np.ndarray:
    return pixels.transpose(1, 0, 2)


def wang_legal(left: tuple[int, ...], right: tuple[int, ...], axis: str) -> bool:
    """Corners [TL, TR, BL, BR] agree along the shared edge: right of ``left`` (x) or below it (y)."""
    if axis == "x":
        return left[1] == right[0] and left[3] == right[2]
    return left[2] == right[0] and left[3] == right[1]


# --------------------------------------------------------------------------- QA helpers

def _local_file_ref(path: Path, base: Path, sha256: str, size: int) -> dict[str, Any]:
    """fileRef with a manifest-relative POSIX path; another drive records the file name only."""
    relative = forge_core.portable_path(path, base)
    if relative.startswith("/") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", relative):
        relative = Path(path).name
    return {"path": relative, "sha256": sha256, "bytes": size}


def _check(identifier: str, status: str, value: Any, threshold: Any) -> dict[str, Any]:
    return {"id": identifier, "status": status, "value": value, "threshold": threshold}


def _same_file(first: Path, second: Path) -> bool:
    if os.path.normcase(str(first.resolve())) == os.path.normcase(str(second.resolve())):
        return True
    return first.exists() and second.exists() and first.samefile(second)


# --------------------------------------------------------------------------- extraction

def extract(args: argparse.Namespace) -> dict[str, Any]:
    validate_options(args)
    mode = resolve_background_mode(args)
    output = Path(os.path.abspath(args.output_dir))
    manifest_path = Path(os.path.abspath(args.manifest)) if args.manifest else output / MANIFEST_NAME
    inputs = [Path(args.input)] + ([Path(args.prompt)] if args.prompt else [])
    if any(_same_file(manifest_path, path) for path in inputs):
        raise ValueError("Output path aliases an input file.")
    if os.path.lexists(output):
        raise FileExistsError(f"Output directory already exists: {output}")
    inside = output in manifest_path.parents
    if not inside and os.path.lexists(manifest_path):
        raise FileExistsError(f"Manifest already exists: {manifest_path}")
    if os.path.splitdrive(str(manifest_path))[0].lower() != os.path.splitdrive(str(output))[0].lower():
        raise ValueError("Keep --manifest on the same drive as --output-dir so tile paths stay relative.")

    terrains = resolve_terrains(args)
    tile_paths = {output / f"{terrain['id']}-{col + 1}.png" for terrain in terrains for col in range(args.cols)}
    if manifest_path in tile_paths:
        raise ValueError("Manifest and image output paths must be distinct.")
    wang = resolve_wang(args, terrains)
    emissions = resolve_emissions(args, terrains)
    source, info = forge_core.load_rgba(args.input)
    atlas = np.array(source)
    cells = grid_cells(source.width, source.height, args.rows, args.cols, args.grid_rounding)
    size = resolve_output_size(args, cells)
    prepared, background = prepare_atlas(atlas, args, mode)
    opaque_fill = args.layer == "base" and args.shape in ("square", "rect")
    seamless = args.edge_policy == "seamless"
    footprint = _local_shape_mask(args.shape, *size)
    manifest_dir = manifest_path.parent

    tiles: dict[str, list[np.ndarray]] = {}
    terrain_payload: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for terrain in terrains:
        identity, row = terrain["id"], terrain["row"]
        transition = wang.get(identity)
        variants: list[dict[str, Any]] = []
        row_tiles: list[np.ndarray] = []
        for col in range(args.cols):
            box = cells[(row, col)]
            cell = prepared[box[1]:box[3], box[0]:box[2]]
            variant: dict[str, Any] = {"path": "", "source_cell": [row, col], "source_box": list(box)}
            if args.shape == "square" and cell.shape[0] != cell.shape[1] and args.cell_shape == "crop-square":
                side = min(cell.shape[:2])
                x0, y0 = (cell.shape[1] - side) // 2, (cell.shape[0] - side) // 2
                cell = cell[y0:y0 + side, x0:x0 + side]
                variant["crop_box"] = [box[0] + x0, box[1] + y0, box[0] + x0 + side, box[1] + y0 + side]
            if mode == "shape_fill":
                cell, variant["shape_fill"] = _key_shape_fill(cell, args.shape, args.fill_tolerance)
            variant["resized"] = (cell.shape[1], cell.shape[0]) != size
            pixels = resize_tile(cell, size, resampler=args.resampler, opaque=opaque_fill,
                                 wrap=seamless).copy()
            pixels[pixels[..., 3] == 0] = 0
            name = f"{identity}-{col + 1}"
            variant.update(tile_metrics(pixels))
            if variant.get("visible_fraction") == 0.0:
                warnings.append(f"{name} is empty: no visible pixels.")
            elif variant["contrast"] < args.min_contrast:
                warnings.append(f"{name} contrast {variant['contrast']:.4f} is below {args.min_contrast:.4f}.")
            if transition:
                mask = transition["masks"][col]
                variant["wang"] = list(mask)
                variant["variant"] = transition["masks"][:col].count(mask)
            if opaque_fill:
                variant["border"] = border_frame(pixels, args.max_border_delta)
                for frame in variant["border"]["frames"][:1]:
                    sides = "left/right" if frame["axis"] == "x" else "top/bottom"
                    warnings.append(f"{name} has a {frame['tone']} border frame on its {sides} edges (luminance "
                                    f"delta {frame['delta']:.3f} > {args.max_border_delta:.3f}); a map would show "
                                    "grid lines.")
            elif args.layer == "base":
                variant["shape"] = shape_qc(pixels, footprint)
                if variant["shape"]["coverage"] < args.min_shape_coverage:
                    warnings.append(f"{name} covers {variant['shape']['coverage']:.1%} of its {args.shape} footprint "
                                    f"(minimum {args.min_shape_coverage:.1%}).")
                if variant["shape"]["spill_fraction"] > args.max_shape_spill:
                    warnings.append(f"{name} spills {variant['shape']['spill_px']} visible px outside its "
                                    f"{args.shape} footprint (maximum {args.max_shape_spill:.1%} of its area).")
            variants.append(variant)
            row_tiles.append(pixels)

        pair_differences: list[float] = []
        for left_index in range(len(row_tiles)):
            for right_index in range(left_index + 1, len(row_tiles)):
                if transition and transition["masks"][left_index] != transition["masks"][right_index]:
                    continue  # different Wang corners are meant to differ
                value = normalized_difference(row_tiles[left_index], row_tiles[right_index])
                pair_differences.append(value)
                if value < args.min_variant_difference:
                    warnings.append(f"{terrain['display_name']} variants {left_index + 1} and {right_index + 1} are "
                                    f"too similar ({value:.4f}).")
        entry: dict[str, Any] = {"display_name": terrain["display_name"], "row": row,
                                 "kind": "wang_corner" if transition else "fill", "variants": variants,
                                 "variant_difference_min": min(pair_differences) if pair_differences else 1.0}
        if transition:
            entry["materials"] = transition["materials"]
        terrain_payload[identity] = entry
        tiles[identity] = row_tiles

    _wang_coverage(terrains, wang, terrain_payload)
    if seamless:
        warnings.extend(_seam_checks(terrains, wang, terrain_payload, tiles, args.max_seam_ratio))
    if args.strict_qc and warnings:
        raise ValueError("Terrain atlas QC failed:\n- " + "\n- ".join(warnings))

    runtime, material, defaults_applied = _runtime_fields(args, emissions)
    for identity, entry in terrain_payload.items():
        values = dict(material)
        if identity in emissions:
            values["emission_energy"] = emissions[identity]
        if values:
            entry["material"] = values

    source_ref = _local_file_ref(Path(args.input), manifest_dir, info["sha256"], info["bytes"])
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "source": {**source_ref, "size": info["size"], "mode": info["source_mode"], "bit_depth": info["bit_depth"],
                   "conversion": info["conversion"]},
    }
    prompt_ref = None
    if args.prompt:
        prompt_bytes = Path(args.prompt).read_bytes()
        prompt_ref = _local_file_ref(Path(args.prompt), manifest_dir, forge_core.sha256_bytes(prompt_bytes),
                                     len(prompt_bytes))
        payload["prompt"] = prompt_ref
    widths = sorted({box[2] - box[0] for box in cells.values()})
    heights = sorted({box[3] - box[1] for box in cells.values()})
    payload["grid"] = {"rows": args.rows, "cols": args.cols, "rounding": args.grid_rounding,
                       "source_cell_size": [widths[0], heights[0]], "output_tile_size": list(size)}
    if len(widths) > 1 or len(heights) > 1:
        payload["grid"]["source_cell_size_max"] = [widths[-1], heights[-1]]
    payload["tile"] = {"shape": args.shape, "layer": args.layer,
                       "footprint": [[round(x, 6), round(y, 6)] for x, y in shape_polygon(args.shape, *size)]}
    payload["terrains"] = terrain_payload
    if runtime:
        payload["runtime"] = runtime
    if defaults_applied:
        payload["runtime_defaults_applied"] = defaults_applied
    payload["processing"] = {"background_mode": mode, "background_mode_requested": args.background_mode,
                             **background, "cell_shape": args.cell_shape, "resampler": args.resampler,
                             "grid_rounding": args.grid_rounding, "edge_policy": args.edge_policy,
                             "wrap_aware_resize": seamless and args.resampler == "lanczos"}
    payload["qc"] = {
        "min_contrast": args.min_contrast,
        "min_variant_difference": args.min_variant_difference,
        "max_border_delta": args.max_border_delta,
        "min_shape_coverage": args.min_shape_coverage,
        "max_shape_spill": args.max_shape_spill,
        "max_seam_ratio": args.max_seam_ratio if seamless else None,
        "warnings": warnings,
        "passed": not warnings,
        "seamless_verified": False,
    }

    sidecar_published = False
    try:
        with forge_core.staged_output(output) as stage:
            outputs = []
            for identity, entry in terrain_payload.items():
                for col, variant in enumerate(entry["variants"]):
                    filename = f"{identity}-{col + 1}.png"
                    forge_core.save_png(tiles[identity][col], stage / filename)
                    variant["path"] = forge_core.portable_path(output / filename, manifest_dir)
                    variant["sha256"] = forge_core.sha256_file(stage / filename)
                    outputs.append({"path": variant["path"], "sha256": variant["sha256"],
                                    "bytes": (stage / filename).stat().st_size})
            payload["qa"] = _qa_envelope(args, terrain_payload, warnings,
                                         [source_ref] + ([prompt_ref] if prompt_ref else []), outputs)
            if inside:
                staged_manifest = stage / manifest_path.relative_to(output)
                staged_manifest.parent.mkdir(parents=True, exist_ok=True)
                forge_core.write_json(staged_manifest, payload)
            else:  # a sidecar: published first, removed again if the directory cannot be published
                temporary = stage / ".terrain-bundle.sidecar"
                forge_core.write_json(temporary, payload)
                forge_core.publish_file_no_replace(temporary, manifest_path)
                sidecar_published = True
                temporary.unlink()
    except BaseException:
        if sidecar_published:
            manifest_path.unlink(missing_ok=True)
        raise
    return payload


def _wang_coverage(terrains: list[dict[str, Any]], wang: dict[str, dict[str, Any]],
                   payload: dict[str, dict[str, Any]]) -> None:
    """Record which corner combinations each Wang row (plus fills named after its materials) provides."""
    for identity, transition in wang.items():
        materials = transition["materials"]
        present = {tuple(mask) for mask in transition["masks"]}
        for index, material in enumerate(materials):
            if _material_fill(terrains, wang, material) is not None:
                present.add((index,) * 4)
        missing = ["".join(map(str, mask)) for mask in np.ndindex(*(len(materials),) * 4) if mask not in present]
        coverage: dict[str, Any] = {"masks": len(present), "of": len(materials) ** 4, "complete": not missing,
                                    "missing_count": len(missing)}
        if len(missing) <= 81:
            coverage["missing"] = missing
        payload[identity]["wang_coverage"] = coverage


def _material_fill(terrains: list[dict[str, Any]], wang: dict[str, dict[str, Any]], material: str) -> str | None:
    for terrain in terrains:
        if terrain["id"] not in wang and material in (terrain["display_name"], terrain["id"]):
            return terrain["id"]
    return None


def _seam_checks(terrains: list[dict[str, Any]], wang: dict[str, dict[str, Any]],
                 payload: dict[str, dict[str, Any]], tiles: dict[str, list[np.ndarray]], gate: float) -> list[str]:
    """Seamless policy: fills must wrap and Wang tiles must join where their corners agree."""
    warnings: list[str] = []

    def failed(report: dict[str, Any]) -> bool:
        return report["verdict"] != "continuous"

    def describe(report: dict[str, Any]) -> str:
        if report["verdict"] == "duplicate_edge":
            return f"duplicates its edge (step {report['seam']:.2f} vs {report['near_median']:.2f} nearby)"
        return f"has a seam (ratio {report['seam_ratio']:.2f} > {gate:g})"

    for terrain in terrains:
        identity = terrain["id"]
        if identity in wang:
            continue
        row_tiles, cross = tiles[identity], []
        for col, (variant, pixels) in enumerate(zip(payload[identity]["variants"], row_tiles)):
            variant["wrap"] = {"x": _local_edge_seam_report(pixels, pixels, gate=gate),
                               "y": _local_edge_seam_report(_transposed(pixels), _transposed(pixels), gate=gate)}
            for axis, report in variant["wrap"].items():
                if failed(report):
                    edges = "left/right" if axis == "x" else "top/bottom"
                    warnings.append(f"{identity}-{col + 1} {describe(report)} where its {edges} edges wrap.")
        for first in range(len(row_tiles)):
            for second in range(len(row_tiles)):
                if first != second:
                    for axis, (a, b) in (("x", (row_tiles[first], row_tiles[second])),
                                         ("y", (_transposed(row_tiles[first]), _transposed(row_tiles[second])))):
                        report = _local_edge_seam_report(a, b, gate=gate)
                        cross.append({"left": first + 1, "right": second + 1, "axis": axis,
                                      "seam_ratio": report["seam_ratio"], "verdict": report["verdict"]})
        if cross:
            payload[identity]["cross_variant_seams"] = {
                "pairs": len(cross), "max_seam_ratio": max(item["seam_ratio"] for item in cross),
                "not_continuous": [item for item in cross if item["verdict"] != "continuous"],
                "note": "diagnostic only: variants are verified to wrap with themselves, not with each other"}

    for identity, transition in wang.items():
        members = [(f"{identity}-{col + 1}", tuple(mask), pixels, True)
                   for col, (mask, pixels) in enumerate(zip(transition["masks"], tiles[identity]))]
        for index, material in enumerate(transition["materials"]):
            fill = _material_fill(terrains, wang, material)
            if fill is not None:
                members += [(f"{fill}-{col + 1}", (index,) * 4, pixels, False)
                            for col, pixels in enumerate(tiles[fill])]
        checked, failures = 0, []
        for left_name, left_mask, left_pixels, left_wang in members:
            for right_name, right_mask, right_pixels, right_wang in members:
                if not (left_wang or right_wang):
                    continue  # fill-to-fill joins belong to the fill rows
                for axis in ("x", "y"):
                    if not wang_legal(left_mask, right_mask, axis):
                        continue
                    a, b = (left_pixels, right_pixels) if axis == "x" else (_transposed(left_pixels),
                                                                           _transposed(right_pixels))
                    report = _local_edge_seam_report(a, b, gate=gate)
                    checked += 1
                    if failed(report):
                        failures.append({"left": left_name, "right": right_name, "axis": axis,
                                         "seam_ratio": report["seam_ratio"], "verdict": report["verdict"]})
                        where = "right of" if axis == "x" else "below"
                        warnings.append(f"Wang join {right_name} {where} {left_name} {describe(report)}.")
        payload[identity]["wang_seams"] = {"legal_joins": checked, "failed": failures}
    return warnings


def _runtime_fields(args: argparse.Namespace, emissions: dict[str, float]) -> tuple[dict, dict, list[str]]:
    """Runtime and shared material numbers the caller gave (MAP-16, DOC-14).

    --emit-runtime-defaults restores the v1 values for whatever was not given and lists them in
    ``applied``. Per-terrain --emission values override the shared emission default.
    """
    runtime = {key: value for key, value in (("engine_target", args.engine_target),
                                             ("world_size", args.runtime_world_size),
                                             ("surface_y", args.surface_y)) if value is not None}
    material = {"roughness": args.roughness} if args.roughness is not None else {}
    applied: list[str] = []
    if args.emit_runtime_defaults:
        for key, value in LEGACY_RUNTIME_DEFAULTS.items():
            if key not in runtime:
                runtime[key] = value
                applied.append(f"runtime.{key}")
        runtime["edge_policy"] = args.edge_policy
        if "roughness" not in material:
            material["roughness"] = LEGACY_MATERIAL_DEFAULTS["roughness"]
            applied.append("material.roughness")
        material["emission_energy"] = LEGACY_MATERIAL_DEFAULTS["emission_energy"]
        if len(emissions) < len(args.terrain_row):
            applied.append("material.emission_energy")
    return runtime, material, applied


def _qa_envelope(args: argparse.Namespace, terrains: dict[str, dict[str, Any]], warnings: list[str],
                 inputs: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    variants = [variant for entry in terrains.values() for variant in entry["variants"]]
    contrast = min(variant["contrast"] for variant in variants)
    difference = min(entry["variant_difference_min"] for entry in terrains.values())
    checks = [
        _check("contrast", "pass" if contrast >= args.min_contrast else "fail", contrast, args.min_contrast),
        _check("variant_difference", "pass" if difference >= args.min_variant_difference else "fail", difference,
               args.min_variant_difference),
    ]
    borders = [variant["border"] for variant in variants if "border" in variant]
    if borders and any(border["checked"] for border in borders):
        framed = sum(border["frame"] for border in borders)
        checks.append(_check("border_frame", "fail" if framed else "pass", framed, args.max_border_delta))
    else:
        checks.append(_check("border_frame", "skipped", None, args.max_border_delta))
    shapes = [variant["shape"] for variant in variants if "shape" in variant]
    if shapes:
        coverage = min(shape["coverage"] for shape in shapes)
        spill = max(shape["spill_fraction"] for shape in shapes)
        checks.append(_check("shape_coverage", "pass" if coverage >= args.min_shape_coverage else "fail", coverage,
                             args.min_shape_coverage))
        checks.append(_check("shape_spill", "pass" if spill <= args.max_shape_spill else "fail", spill,
                             args.max_shape_spill))
    else:
        checks.append(_check("shape_coverage", "skipped", None, args.min_shape_coverage))
        checks.append(_check("shape_spill", "skipped", None, args.max_shape_spill))
    wraps = [report for variant in variants for report in variant.get("wrap", {}).values()]
    if wraps:
        bad = sum(report["verdict"] != "continuous" for report in wraps)
        checks.append(_check("wrap_seams", "fail" if bad else "pass", max(r["seam_ratio"] for r in wraps),
                             args.max_seam_ratio))
    else:
        checks.append(_check("wrap_seams", "skipped", None, None))
    wang_reports = [entry["wang_seams"] for entry in terrains.values() if "wang_seams" in entry]
    if wang_reports:
        bad = sum(len(report["failed"]) for report in wang_reports)
        checks.append(_check("wang_seams", "fail" if bad else "pass", bad, args.max_seam_ratio))
    else:
        checks.append(_check("wang_seams", "skipped", None, None))
    status = "fail" if warnings or any(check["status"] == "fail" for check in checks) else "pass"
    not_proven = [
        "visual quality, style match and readability at game zoom",
        "that each variant reads as the named terrain",
        "seamlessness: no exact seam proof exists for image tiles (seamless_verified stays false)",
    ]
    if args.edge_policy == "seamless":
        not_proven.append("joins between different variants of a fill row (measured as diagnostics only)")
    if args.shape not in ("square", "rect"):
        not_proven.append(f"engine placement of {args.shape} tiles (footprints are data; no engine import ran)")
    return {
        "status": status,
        "method": "Grid slices of the atlas, background handled per --background-mode, resized to the tile size "
                  "(Lanczos or nearest; wrap-aware for the seamless policy). Luminance contrast and mean thumbnail "
                  "differences; drawn frames along opposite edges; footprint coverage and spill for iso/hex base "
                  "tiles; for the seamless policy, premultiplied wrap and Wang-join steps normalised by the art's "
                  "own steps near each edge.",
        "notProven": not_proven,
        "checks": checks,
        "inputs": inputs,
        "outputs": outputs,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
    }


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True,
                        help="Terrain atlas: RGB(A), palette PNG with transparency, grey or grey+alpha.")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New directory only; tiles and terrain-bundle.json are published after QC.")
    parser.add_argument("--rows", type=int, required=True, help="Atlas rows: one terrain per row.")
    parser.add_argument("--cols", type=int, required=True, help="Atlas columns: one variant per column.")
    parser.add_argument("--terrain-row", action="append", type=parse_row, required=True, metavar="NAME=ROW",
                        help="Map every atlas row to a terrain name, for example grass=0. Non-ASCII names are kept "
                             "as display names; their files are named terrain-<row>-<n>.png.")
    parser.add_argument("--wang", action="append", type=parse_wang, default=[],
                        metavar="NAME=MAT/MAT:MASKS",
                        help="Mark a row as Wang corner transitions: materials and one 4-digit corner mask per "
                             "column (top-left, top-right, bottom-left, bottom-right), for example "
                             "shore=water/grass:0001,0011,0111.")
    shape = parser.add_argument_group("tile shape")
    shape.add_argument("--shape", choices=SHAPES, default="square",
                       help="Tile footprint. iso-diamond and hex tiles keep the cell aspect ratio.")
    shape.add_argument("--layer", choices=LAYERS, default="base",
                       help="base: opaque fills (or iso/hex tiles opaque inside their footprint); overlay: RGBA "
                            "tiles drawn over a base (decals, tufts, transitions).")
    shape.add_argument("--tile-size", type=int, help="Square output size (default: the smallest cell side).")
    shape.add_argument("--tile-width", type=int, help="Rect/iso/hex output width (default: the cell width).")
    shape.add_argument("--tile-height", type=int, help="Rect/iso/hex output height (default: the cell height).")
    shape.add_argument("--cell-shape", choices=("square", "crop-square"), default="square",
                       help="Square tiles only: crop-square centre-crops non-square cells (loses pixels).")
    shape.add_argument("--grid-rounding", choices=("exact", "nearest"), default="exact",
                       help="exact needs an evenly divisible atlas; nearest rounds cell edges (cells differ by at "
                            "most 1 px) and records every cell's source_box.")
    shape.add_argument("--resampler", choices=("nearest", "lanczos"), default="lanczos",
                       help="nearest for pixel art; lanczos resizes opaque fills in RGB and RGBA tiles "
                            "premultiplied (box filter for 2x or larger reductions).")
    background = parser.add_argument_group("background")
    background.add_argument("--background-mode", choices=BACKGROUND_MODES, default="auto",
                            help="auto: opaque for base square/rect fills, native_alpha otherwise. shape_fill keys "
                                 "the flat colour around iso-diamond or hex cells; chroma_key removes magenta.")
    background.add_argument("--fill-tolerance", type=float, default=32.0,
                            help="shape_fill: RGB distance from the corner colour that still counts as fill.")
    background.add_argument("--threshold", type=int, default=100, help="Chroma: clear pixels this close to #FF00FF.")
    background.add_argument("--edge-threshold", type=int, default=150,
                            help="Chroma: clear border-connected pixels this close to #FF00FF.")
    background.add_argument("--despill-radius", type=int, choices=range(4), default=0,
                            help="Chroma only: remove magenta excess within this many px of transparency (0-3).")
    runtime = parser.add_argument_group("runtime metadata (written only when given)")
    runtime.add_argument("--engine-target", help="Free-text engine or renderer the tiles are meant for.")
    runtime.add_argument("--runtime-world-size", type=float, help="World units per tile in the target engine.")
    runtime.add_argument("--surface-y", type=float, help="Height offset of the walk surface in world units.")
    runtime.add_argument("--roughness", type=float, help="Shared material roughness, 0..1.")
    runtime.add_argument("--emission", action="append", type=parse_float_map, default=[], metavar="NAME=VALUE",
                         help="Emission energy of one terrain, for example lava=1.2 (repeatable).")
    runtime.add_argument("--emit-runtime-defaults", action="store_true",
                         help="Legacy switch: also write the v1 defaults (world size 0.94, surface y 0.011, "
                              "roughness 0.88, emission 0, engine target project-native).")
    qc = parser.add_argument_group("quality control")
    qc.add_argument("--edge-policy", choices=("isolated", "seamless"), default="isolated",
                    help="seamless: wrap-aware Lanczos resize, and every fill must wrap and every Wang join "
                         "must continue within --max-seam-ratio. isolated makes no tiling claim.")
    qc.add_argument("--max-seam-ratio", type=float, default=1.25,
                    help="Seamless policy: largest join step relative to the art's own steps near the edge.")
    qc.add_argument("--min-contrast", type=float, default=0.035,
                    help="Lowest luminance standard deviation (0..1) of a tile's visible pixels.")
    qc.add_argument("--min-variant-difference", type=float, default=0.025,
                    help="Lowest mean thumbnail difference (0..1) between variants of a row (same Wang mask).")
    qc.add_argument("--max-border-delta", type=float, default=0.1,
                    help="Base square/rect fills: luminance step (0..1) that marks a drawn frame or gutter along "
                         "opposite edges; 1 disables the check.")
    qc.add_argument("--min-shape-coverage", type=float, default=0.97,
                    help="Base iso/hex tiles: share of the footprint that must be visible.")
    qc.add_argument("--max-shape-spill", type=float, default=0.03,
                    help="Base iso/hex tiles: visible px outside the footprint, as a share of its area.")
    qc.add_argument("--strict-qc", action="store_true", help="Fail without publishing when any QC warning remains.")
    parser.add_argument("--manifest", type=Path,
                        help="Manifest path (default <output-dir>/terrain-bundle.json); outside the output "
                             "directory it is published next to it without replacing anything.")
    parser.add_argument("--prompt", type=Path, help="Prompt file to record (path and sha256).")
    return parser


def main(argv: list[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        payload = extract(args)
    except (OSError, ValueError) as exc:
        message = f"file not found: {exc.filename}" if isinstance(exc, FileNotFoundError) and exc.filename else exc
        print(f"error: {forge_core.ascii_text(str(message))}", file=sys.stderr)
        return 1
    output = Path(args.output_dir).absolute()
    manifest = Path(args.manifest).absolute() if args.manifest else output / MANIFEST_NAME
    summary = {"output": str(output), "manifest": str(manifest), "schema": payload["schema"],
               "status": payload["qa"]["status"],
               "qc": {"passed": payload["qc"]["passed"], "warnings": payload["qc"]["warnings"]}}
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
