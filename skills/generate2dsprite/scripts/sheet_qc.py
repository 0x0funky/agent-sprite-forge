#!/usr/bin/env python3
"""Generation-side QC for sprite sheets and frame sets (generate2dsprite.sheet_qc.v1).

Two checks. Each writes a QA envelope (sheet-qc.json) and a review image into a
new --output-dir:

  spill   Run on the RAW sheet before anything is sliced. Finds solid parts that
          cross a cell line (owner cell, pixels over the line, overhang), art cut
          by the sheet edge, empty cells, art inside the band around each cell
          line, cells outside the safe frame and faint detached specks.
  frames  Run on the frames of one action: a sheet sliced by component
          ownership (--sheet), or frame files on one canvas (--frames). Checks
          identity (head ratio against the median frame), near-duplicates and
          phase coverage, leading-leg alternation from NEAR/FAR colour groups,
          torso drift and row baselines in game pixels, and ranks the seams.

Run from the project root, for example:
  python "<skill-dir>/scripts/sheet_qc.py" spill --input raw/run-sheet.png --rows 2 --cols 4 --output-dir qc/run-spill
  python "<skill-dir>/scripts/sheet_qc.py" frames --sheet raw/run-sheet.png --rows 2 --cols 4 --cycle run --game-pixel 8 --output-dir qc/run-frames

The report is published even when a check fails; the run then exits 1 (D26:
a published report whose status is fail exits 1; pass and warn exit 0). With
--strict a failed check exits 1 and publishes nothing. Usage errors exit 2
(argparse); other errors print one "error: ..." line and exit 1.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_palette  # noqa: E402  (this skill's vendored copy: OKLab, D16)

TOOL = {"name": "sheet_qc.py", "version": forge_core.FORGE_PACKAGE_VERSION}  # the package version (D29)
SCHEMA = "generate2dsprite.sheet_qc.v1"
REPORT_NAME = "sheet-qc.json"
SOLID_THRESHOLD = 127  # alpha > 127 (>= 128): the solid body; faint glow never joins two subjects
KEYS = ("auto", "none", "magenta", "green", "blue")
CYCLES = ("run", "walk", "none")
FACINGS = ("right", "left")
HEAD_SCALES = tuple(round(0.90 + 0.02 * step, 2) for step in range(11))
PHASE_SAMPLE_BAND = 0.18  # bottom share of the body used for stride width (report v2 boots band)
AUTO_FEET_BAND = 0.18  # bottom share used by the colour-free NEAR/FAR test
AUTO_MIN_DELTA_L = 0.015  # OKLab lightness gap that counts as "one shade darker"
_SEVERITY = {"pass": 0, "needs-visual-review": 1, "warn": 2, "fail": 3}
_HEX = re.compile(r"#?([0-9a-fA-F]{6})")


class QcError(ValueError):
    """A user-facing input problem, printed as 'error: ...'."""


# --------------------------------------------------------------------------- small helpers

def worst_status(statuses: Sequence[str]) -> str:
    """Overall verdict: the most severe status, ignoring skipped checks (pass when none ran)."""
    ranked = [status for status in statuses if status in _SEVERITY]
    return max(ranked, key=_SEVERITY.__getitem__) if ranked else "pass"


def check(check_id: str, status: str, value: Any = None, threshold: Any = None, **extra: Any) -> dict:
    """One qaCheck entry."""
    item = {"id": check_id, "status": status, "value": value, "threshold": threshold}
    item.update(extra)
    return item


def parse_colors(text: str | None) -> list[tuple[int, int, int]]:
    """'#rrggbb,#rrggbb' (the '#' is optional) -> RGB tuples; empty for None."""
    if text is None or not text.strip():
        return []
    colors = []
    for part in text.split(","):
        match = _HEX.fullmatch(part.strip())
        if not match:
            raise QcError(f"Colours are #rrggbb values separated by commas; got {part.strip()!r}.")
        value = match.group(1)
        colors.append((int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)))
    return colors


def parse_fraction_pair(text: str, name: str) -> tuple[float, float]:
    """'0.40,0.60' -> (0.40, 0.60) with 0 <= a < b <= 1."""
    try:
        low, high = (float(part) for part in text.split(","))
    except ValueError:
        raise QcError(f"{name} takes two fractions such as 0.40,0.60; got {text!r}.") from None
    if not 0.0 <= low < high <= 1.0:
        raise QcError(f"{name} needs 0 <= start < end <= 1; got {text!r}.")
    return low, high


def hex_color(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{int(round(float(value))):02x}" for value in rgb[:3])


def _round(value: float, digits: int = 2) -> float:
    return float(round(float(value), digits))


# --------------------------------------------------------------------------- input

def load_input(path: Path, key: str = "auto") -> tuple[np.ndarray, dict]:
    """Load a sheet or frame as 8-bit RGBA, keying an opaque chroma backdrop when needed.

    ``key``: ``auto`` keeps real transparency and keys an opaque image's magenta
    backdrop; ``none`` never keys; ``magenta``, ``green``, ``blue`` or ``#rrggbb``
    keys that backdrop with the shared soft keyer (forge_matte.key_still).
    """
    image, info = forge_core.load_rgba(path)
    pixels = np.asarray(image)
    has_alpha = bool((pixels[..., 3] < 255).any())
    keyed = None
    if key != "none" and not (key == "auto" and has_alpha):
        import forge_matte  # vendored beside this file; only chroma inputs need it

        declared = "magenta" if key == "auto" else key
        result, key_info = forge_matte.key_still(pixels, quality="soft", key=declared, resampler_hint="lanczos")
        pixels = np.asarray(result)
        keyed = {"key": declared, "quality": key_info["quality"],
                 "key_rgb": [int(round(float(value))) for value in key_info["key"]],
                 "opaque_key_px": int(key_info["qa"]["opaque_key_px"])}
    if not (pixels[..., 3] < 255).any():
        raise QcError(f"{path.name} has no transparent pixels; pass --key with its backdrop colour "
                      "(magenta, green, blue or #rrggbb).")
    record = {"sha256": info["sha256"], "size": info["size"], "source_mode": info["source_mode"],
              "bit_depth": info["bit_depth"], "conversion": info["conversion"], "keyed": keyed}
    return np.ascontiguousarray(pixels), record


def _cell_map(height: int, width: int, boxes: Sequence[Sequence[int]]) -> np.ndarray:
    """Index of the grid cell under every pixel (row-major cell order)."""
    cell_map = np.empty((height, width), np.int32)
    for index, (x0, y0, x1, y1) in enumerate(boxes):
        cell_map[y0:y1, x0:x1] = index
    return cell_map


def _components(mask: np.ndarray, cell_map: np.ndarray, cells: int) -> dict:
    """8-connected components of ``mask`` with their area, bbox and per-cell pixel counts."""
    labels, count = forge_core.label_components(mask, 8)
    ys, xs = np.nonzero(labels)
    ids = labels[ys, xs]
    counts = np.bincount(ids.astype(np.int64) * cells + cell_map[ys, xs], minlength=(count + 1) * cells)
    counts = counts.reshape(count + 1, cells)
    x0 = np.full(count + 1, cell_map.shape[1], np.int64)
    y0 = np.full(count + 1, cell_map.shape[0], np.int64)
    x1 = np.zeros(count + 1, np.int64)
    y1 = np.zeros(count + 1, np.int64)
    np.minimum.at(x0, ids, xs)
    np.minimum.at(y0, ids, ys)
    np.maximum.at(x1, ids, xs + 1)
    np.maximum.at(y1, ids, ys + 1)
    return {"labels": labels, "count": count, "counts": counts, "area": counts.sum(axis=1),
            "bbox": np.stack([x0, y0, x1, y1], axis=1)}


def _cell_rc(index: int, cols: int) -> list[int]:
    return [index // cols, index % cols]


# --------------------------------------------------------------------------- spill

def _crossings(table: dict, boxes: Sequence[Sequence[int]], cols: int, min_area: int,
               size: tuple[int, int]) -> list[dict]:
    """Every component of at least ``min_area`` px with its owner cell and how far it leaves that cell."""
    width, height = size
    records = []
    for label in range(1, table["count"] + 1):
        area = int(table["area"][label])
        if area < min_area:
            continue
        counts = table["counts"][label]
        owner = int(np.argmax(counts))  # ties keep the lowest cell index
        x0, y0, x1, y1 = (int(value) for value in table["bbox"][label])
        bx0, by0, bx1, by1 = boxes[owner]
        sides = {"left": max(0, bx0 - x0), "top": max(0, by0 - y0),
                 "right": max(0, x1 - bx1), "bottom": max(0, y1 - by1)}
        records.append({
            "label": label, "area": area, "bbox": [x0, y0, x1, y1],
            "owner_cell": _cell_rc(owner, cols), "owner_index": owner,
            "pixels_per_cell": {str(index): int(value) for index, value in enumerate(counts) if value},
            "pixels_over_line": area - int(counts[owner]),
            "overhang_px": max(sides.values()), "overhang_sides": sides,
            "touches_sheet_edge": x0 == 0 or y0 == 0 or x1 == width or y1 == height,
        })
    return records


def analyse_spill(rgba: np.ndarray, rows: int, cols: int, *, threshold: int = SOLID_THRESHOLD,
                  edge_threshold: int = forge_core.ALPHA_GEOMETRY_THRESHOLD, min_area: int = 64,
                  band: int | None = None, safe_margin: float = 0.15, expect: int | None = None) -> dict:
    """Cross-cell spill analysis of a raw sheet (report v2 P1-3, 3.2.1-3.2.2).

    Components are 8-connected at ``alpha > threshold`` (solid) and again at
    ``alpha > edge_threshold`` (visible edge). A component belongs to the cell
    holding most of its pixels; its pixels in any other cell are "over the
    line" and its overhang is how far it leaves that cell. Returns checks,
    per-cell records and private arrays (keys starting with '_') for the overlay.
    """
    height, width = rgba.shape[:2]
    if rows < 1 or cols < 1:
        raise QcError("--rows and --cols must be positive.")
    if width < cols or height < rows:
        raise QcError(f"A {width}x{height} sheet cannot hold {rows} rows x {cols} columns.")
    cells = rows * cols
    expect = cells if expect is None else expect
    if not 1 <= expect <= cells:
        raise QcError(f"--expect must be between 1 and {cells} (rows x cols); got {expect}.")
    if not 0.0 <= safe_margin < 0.5:
        raise QcError(f"--safe-margin is a fraction of the cell in [0, 0.5); got {safe_margin}.")
    boxes = [tuple(int(v) for v in box) for box in forge_core.rounded_grid_boxes(width, height, rows, cols)]
    cell_w = min(box[2] - box[0] for box in boxes)
    cell_h = min(box[3] - box[1] for box in boxes)
    band = max(1, int(round(0.02 * min(cell_w, cell_h)))) if band is None else band
    alpha = rgba[..., 3]
    cell_map = _cell_map(height, width, boxes)
    solid = _components(alpha > threshold, cell_map, cells)
    visible = _components(alpha > edge_threshold, cell_map, cells)
    solid_records = _crossings(solid, boxes, cols, min_area, (width, height))
    visible_records = _crossings(visible, boxes, cols, min_area, (width, height))
    crossing = [record for record in solid_records if record["pixels_over_line"] > 0]
    visible_crossing = [record for record in visible_records if record["pixels_over_line"] > 0]

    cell_records = []
    for index, (x0, y0, x1, y1) in enumerate(boxes):
        owned = [record for record in solid_records if record["owner_index"] == index]
        owned_visible = [record for record in visible_records if record["owner_index"] == index]
        record: dict[str, Any] = {
            "cell": _cell_rc(index, cols), "index": index, "box": [x0, y0, x1, y1],
            "owned_components": len(owned), "owned_area": int(sum(item["area"] for item in owned)),
            "components_over_line": sum(1 for item in owned if item["pixels_over_line"]),
            "pixels_over_line": int(sum(item["pixels_over_line"] for item in owned)),
            "overhang_px": int(max((item["overhang_px"] for item in owned), default=0)),
            "visible_pixels_over_line": int(sum(item["pixels_over_line"] for item in owned_visible)),
            "visible_overhang_px": int(max((item["overhang_px"] for item in owned_visible), default=0)),
            "intruders": [{"label": item["label"], "owner_cell": item["owner_cell"],
                           "pixels_in_this_cell": int(item["pixels_per_cell"].get(str(index), 0))}
                          for item in solid_records
                          if item["owner_index"] != index and item["pixels_per_cell"].get(str(index))],
            "expected": index < expect,
        }
        if owned:
            box = [min(item["bbox"][0] for item in owned), min(item["bbox"][1] for item in owned),
                   max(item["bbox"][2] for item in owned), max(item["bbox"][3] for item in owned)]
            cw, ch = x1 - x0, y1 - y0
            margins = {"left": box[0] - x0, "top": box[1] - y0, "right": x1 - box[2], "bottom": y1 - box[3]}
            record.update({
                "owned_bbox": box,
                "margins_px": margins,
                "margins_frac": {side: _round(value / (cw if side in ("left", "right") else ch), 4)
                                 for side, value in margins.items()},
                "cut_by_cell_edge": any(value < 0 for value in margins.values()),
                "inside_safe_frame": all(value >= safe_margin * (cw if side in ("left", "right") else ch)
                                         for side, value in margins.items()),
            })
        cell_records.append(record)

    lines = [("x", box[2]) for box in boxes[:cols - 1]] + [("y", boxes[row * cols][3]) for row in range(rows - 1)]
    solid_mask = alpha > threshold
    band_counts = []
    for axis, position in lines:
        low, high = max(0, position - band), position + band
        region = solid_mask[:, low:high] if axis == "x" else solid_mask[low:high, :]
        band_counts.append({"axis": axis, "at": int(position), "solid_px": int(region.sum())})

    _, hygiene = forge_core.alpha_hygiene(rgba, mode="both")
    soft = alpha[(alpha > 0) & (alpha <= threshold)]
    histogram = {"1-4": int(((soft >= 1) & (soft <= 4)).sum()), "5-15": int(((soft >= 5) & (soft <= 15)).sum()),
                 "16-63": int(((soft >= 16) & (soft <= 63)).sum()), "64-127": int(((soft >= 64) & (soft <= 127)).sum())}
    specks = {"floor_px": hygiene["floor_px"], "detached_px": hygiene["detached_px"],
              "detached_components": hygiene["detached_components"],
              "max_removed_alpha": hygiene["max_removed_alpha"], "soft_alpha_histogram": histogram}

    edge_cut = [record for record in solid_records if record["touches_sheet_edge"]]
    empty = [record["cell"] for record in cell_records if record["expected"] and not record["owned_components"]]
    outside_safe = [record["cell"] for record in cell_records if record.get("inside_safe_frame") is False]
    band_max = max((item["solid_px"] for item in band_counts), default=0)
    checks = [
        check("cross_cell_components", "fail" if crossing else "pass", len(crossing), 0,
              cells=[record["owner_cell"] for record in crossing],
              note="solid parts crossing a cell line are cut or misassigned when the sheet is sliced"),
        check("cross_cell_visible", "fail" if visible_crossing else "pass", len(visible_crossing), 0,
              note=f"the same test on the visible edge (alpha > {edge_threshold})"),
        check("sheet_edge", "fail" if edge_cut else "pass", len(edge_cut), 0,
              note="art touching the sheet border was cut by the image edge"),
        check("empty_cells", "fail" if empty else "pass", empty, [],
              note=f"the first {expect} cells must each hold a subject"),
        check("boundary_band", "warn" if band_max else "pass", band_max, 0,
              note=f"solid pixels within {band} px of a cell line; a different crop or rounding can cut them"),
        check("safe_frame", "warn" if outside_safe else "pass", len(outside_safe), 0, cells=outside_safe,
              note=f"owned art closer than {safe_margin:.0%} of the cell to its edge; ask for more margin next time"),
        check("faint_specks", "warn" if hygiene["detached_components"] or hygiene["floor_px"] else "pass",
              {"floor_px": hygiene["floor_px"], "detached_components": hygiene["detached_components"]}, 0,
              note="alpha haze and detached specks; process removes them with --alpha-hygiene both"),
    ]
    return {
        "grid": {"rows": rows, "cols": cols, "cell_boxes": [list(box) for box in boxes],
                 "rounding": "half-up (forge_core.rounded_grid_boxes)"},
        "params": {"alpha_threshold": threshold, "edge_threshold": edge_threshold, "min_area": min_area,
                   "band_px": band, "safe_margin": safe_margin, "expect": expect, "connectivity": 8},
        "checks": checks,
        "cells": cell_records,
        "crossing_components": [{key: value for key, value in record.items() if key != "owner_index"}
                                for record in crossing],
        "visible_crossing_components": [{key: value for key, value in record.items() if key != "owner_index"}
                                        for record in visible_crossing],
        "boundary_band": band_counts,
        "specks": specks,
        "_labels": solid["labels"], "_cell_map": cell_map, "_crossing": crossing,
    }


def render_spill_overlay(rgba: np.ndarray, analysis: dict) -> Image.Image:
    """The sheet over mid grey with cell lines (blue), safe frames (cyan), pixels over a line (red)
    and the boxes of crossing parts (red)."""
    alpha = rgba[..., 3:4].astype(np.float32) / 255.0
    base = rgba[..., :3].astype(np.float32) * alpha + 128.0 * (1.0 - alpha)
    canvas = np.clip(base + 0.5, 0, 255).astype(np.uint8)
    labels, cell_map = analysis["_labels"], analysis["_cell_map"]
    for record in analysis["_crossing"]:
        over = (labels == record["label"]) & (cell_map != record["owner_index"])
        canvas[over] = (255, 32, 48)
    image = Image.fromarray(canvas, "RGB")
    draw = ImageDraw.Draw(image)
    margin = analysis["params"]["safe_margin"]
    for x0, y0, x1, y1 in analysis["grid"]["cell_boxes"]:
        mx, my = margin * (x1 - x0), margin * (y1 - y0)
        draw.rectangle((x0 + mx, y0 + my, x1 - 1 - mx, y1 - 1 - my), outline=(0, 200, 230), width=1)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(40, 90, 255), width=2)
    font = ImageFont.load_default()
    for record in analysis["_crossing"]:
        x0, y0, x1, y1 = record["bbox"]
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(255, 32, 48), width=2)
        label = f"{record['pixels_over_line']} px over the line, overhang {record['overhang_px']} px"
        draw.text((x0 + 4, y0 + 4), label, fill=(255, 32, 48), font=font)
    for record in analysis["cells"]:
        if record["expected"] and not record["owned_components"]:
            x0, y0, x1, y1 = record["box"]
            draw.line((x0, y0, x1 - 1, y1 - 1), fill=(255, 32, 48), width=3)
            draw.line((x0, y1 - 1, x1 - 1, y0), fill=(255, 32, 48), width=3)
    return image


# --------------------------------------------------------------------------- ownership slicing

def ownership_slice(rgba: np.ndarray, boxes: Sequence[Sequence[int]], *, threshold: int = SOLID_THRESHOLD,
                    min_area: int = 64, attach_radius: int = 6,
                    count: int | None = None) -> tuple[list[np.ndarray], dict]:
    """Slice a sheet so no part is cut: every frame keeps its nominal cell origin plus one shared padding.

    forge_core.ownership_slice with the spill-QC policy of D14 (haze ``drop``):
    solid components (``alpha > threshold``, at least ``min_area`` px) belong to
    the cell holding most of their pixels; softer pixels join the owner they
    reach within ``attach_radius`` steps through soft pixels (report v2
    prototype order: left, right, above, below, diagonals). Pixels nobody owns
    (detached haze) are dropped and counted. Every frame gets the same canvas,
    so the registration of the grid is unchanged (report v2 5.4). Only the
    first ``count`` cells (default all) are returned; later cells may be empty,
    but each returned cell must hold a subject.
    """
    cells = len(boxes)
    count = cells if count is None else count
    if not 1 <= count <= cells:
        raise QcError(f"--count must be between 1 and {cells} (rows x cols); got {count}.")
    frames, shared = forge_core.ownership_slice(rgba, boxes=boxes, alpha_threshold=threshold, min_area=min_area,
                                                haze="drop", attach_radius=attach_radius, count=count)
    if shared["empty_cells"]:
        cols = len({box[0] for box in boxes})
        raise QcError(f"Cell {_cell_rc(shared['empty_cells'][0], cols)} holds no subject; check --rows/--cols, "
                      "--count or the key.")
    info = {"method": "ownership (forge_core.ownership_slice, haze drop): solid components to the cell holding most "
                      "of their pixels, soft pixels to the owner they reach through soft pixels; nominal cell origin "
                      "plus one shared padding",
            "haze": shared["haze"]}
    info.update({key: shared[key] for key in ("threshold", "min_area", "attach_radius", "cells_used",
                                              "unused_cells_px", "padding", "canvas", "frame_origins_in_sheet",
                                              "dropped_px", "dropped_max_alpha")})
    return frames, info


# --------------------------------------------------------------------------- per-frame measurements

def _extent(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, top, x1, ground) of a mask; ground is the bottom edge of the lowest row."""
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return None
    columns = np.flatnonzero(mask.any(axis=0))
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


def _band_rows(extent: tuple[int, int, int, int], start: float, end: float) -> tuple[int, int]:
    """Rows [a, b) of a band given as fractions of the body height from the top."""
    _x0, top, _x1, ground = extent
    height = ground - top
    first = top + int(math.floor(start * height + 0.5))
    last = top + int(math.floor(end * height + 0.5))
    return first, max(first + 1, last)


def _color_distance(rgb: np.ndarray, colors: Sequence[Sequence[int]]) -> np.ndarray:
    """Euclidean RGB distance of every pixel to the nearest of ``colors``."""
    palette = np.asarray(colors, np.float32)
    pixels = np.asarray(rgb, np.float32)[..., None, :3]
    return np.sqrt(((pixels - palette) ** 2).sum(axis=-1)).min(axis=-1)


def torso_x(frame: np.ndarray, *, colors: Sequence[Sequence[int]] = (), tolerance: float = 30.0,
            band: tuple[float, float] = (0.40, 0.60), threshold: int = SOLID_THRESHOLD) -> tuple[float | None, str]:
    """Horizontal torso position of one frame (continuous x), and the method used.

    With ``colors`` (the torso's costume colours) it is the centroid of the
    largest 8-connected group of solid pixels within ``tolerance`` of them, as
    in report v2 (tunic colour group). Otherwise it is the centroid of the
    "trunk": solid pixels of the columns filled in at least 80% of the torso
    band's rows (``band`` = fractions of the body height from the top), which
    ignores swinging arms and a tail. Returns (None, reason) when it finds nothing.
    """
    solid = frame[..., 3] > threshold
    extent = _extent(solid)
    if extent is None:
        return None, "empty frame"
    if colors:
        mask = solid & (_color_distance(frame[..., :3], colors) <= tolerance)
        components = forge_core.connected_components(mask, with_masks=True)
        if not components:
            return None, "no pixel near the torso colours"
        largest = components[0]
        xs = np.nonzero(largest["mask"])[1]
        return float(xs.mean()) + largest["bbox"][0] + 0.5, "colour"
    first, last = _band_rows(extent, *band)
    rows = solid[first:last]
    coverage = rows.mean(axis=0)
    columns = np.flatnonzero(coverage >= 0.8)
    if columns.size == 0:
        columns = np.flatnonzero(coverage >= coverage.max() * 0.8) if coverage.max() > 0 else columns
    if columns.size == 0:
        return None, "empty torso band"
    breaks = np.flatnonzero(np.diff(columns) > 1)
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [columns.size - 1]])
    widest = int(np.argmax(columns[ends] - columns[starts]))
    run = columns[starts[widest]:ends[widest] + 1]
    weights = rows[:, run].sum(axis=0)
    return float((run * weights).sum() / weights.sum()) + 0.5, "trunk"


def _two_means(xs: np.ndarray, iterations: int = 30) -> tuple[np.ndarray, np.ndarray]:
    """1-D 2-means on x (report v2 boots split): (assignments, centres)."""
    centres = np.array([np.percentile(xs, 20), np.percentile(xs, 80)], np.float64)
    assign = np.zeros(xs.shape, np.int64)
    for _ in range(iterations):
        assign = np.abs(xs[:, None] - centres[None]).argmin(axis=1)
        updated = np.array([xs[assign == k].mean() if np.any(assign == k) else centres[k] for k in range(2)])
        if np.allclose(updated, centres):
            break
        centres = updated
    return assign, centres


def leg_lead(frame: np.ndarray, *, facing: str = "right", near: Sequence[Sequence[int]] = (),
             far: Sequence[Sequence[int]] = (), tolerance: float = 30.0, band: float = 0.30,
             game_pixel: float = 1.0, threshold: int = SOLID_THRESHOLD) -> dict:
    """Which leg leads (is ahead in the facing direction): NEAR, FAR or unclear.

    Colour mode (``near`` and ``far`` given): in the bottom ``band`` of the body,
    pixels within ``tolerance`` of a NEAR colour and closer to it than to any FAR
    colour are NEAR (and the other way round); the lead is the group whose
    centroid is ahead. Auto mode: the feet band (bottom 18%) is split into two
    feet by 2-means on x and the brighter foot (OKLab lightness of its
    mid-tones, outline excluded) is NEAR, the drawing convention of
    prompt-rules.md ("FAR limbs one shade darker").
    """
    sign = 1.0 if facing == "right" else -1.0
    solid = frame[..., 3] > threshold
    extent = _extent(solid)
    if extent is None:
        return {"lead": "unclear", "reason": "empty frame"}
    height = extent[3] - extent[1]
    if near and far:
        first = extent[3] - max(1, int(math.floor(band * height + 0.5)))
        ys, xs = np.nonzero(solid[first:extent[3]])
        rgb = frame[ys + first, xs, :3]
        to_near, to_far = _color_distance(rgb, near), _color_distance(rgb, far)
        is_near = (to_near <= tolerance) & (to_near < to_far)
        is_far = (to_far <= tolerance) & (to_far < to_near)
        minimum = max(20, int(0.002 * int(solid.sum())))
        result = {"mode": "colour", "near_px": int(is_near.sum()), "far_px": int(is_far.sum())}
        if result["near_px"] < minimum or result["far_px"] < minimum:
            return {**result, "lead": "unclear", "reason": f"fewer than {minimum} NEAR or FAR pixels in the leg band"}
        near_x, far_x = float(xs[is_near].mean()) + 0.5, float(xs[is_far].mean()) + 0.5
        ahead = (near_x - far_x) * sign
        result.update({"near_x": _round(near_x, 1), "far_x": _round(far_x, 1),
                       "separation_game_px": _round(abs(near_x - far_x) / game_pixel)})
        if abs(near_x - far_x) < game_pixel:
            return {**result, "lead": "unclear", "reason": "NEAR and FAR centroids less than 1 game px apart"}
        return {**result, "lead": "near" if ahead > 0 else "far"}
    first = extent[3] - max(1, int(math.floor(AUTO_FEET_BAND * height + 0.5)))
    ys, xs = np.nonzero(solid[first:extent[3]])
    if xs.size < 20:
        return {"mode": "auto-luminance", "lead": "unclear", "reason": "feet band nearly empty"}
    assign, centres = _two_means(xs.astype(np.float64))
    front = int(np.argmax(centres * sign))
    back = 1 - front
    lightness = forge_palette.to_oklab(frame[ys + first, xs, :3])[..., 0]  # OKLab L (D16)

    def midtone(selection: np.ndarray) -> float:
        values = lightness[selection]
        return float(np.median(values[values >= np.percentile(values, 35)]))

    if not (np.any(assign == front) and np.any(assign == back)):
        return {"mode": "auto-luminance", "lead": "unclear", "reason": "one foot only"}
    delta = midtone(assign == front) - midtone(assign == back)
    return {"mode": "auto-luminance", "front_minus_back_L": _round(delta, 4),
            "separation_game_px": _round(abs(centres[0] - centres[1]) / game_pixel),
            "lead": "near" if delta > 0 else "far", "_delta": delta,
            "_separation": float(abs(centres[0] - centres[1]))}


def alternation_verdict(leads: Sequence[dict], *, game_pixel: float = 1.0) -> dict:
    """Compare the lead of frame i with frame i + n/2 (report v2 3.2.3; jev walk_phase_check).

    A cycle alternates when the other leg leads half a cycle later. Every
    comparable pair having the same lead means a duplicated half-cycle.
    """
    count = len(leads)
    leads = [dict(item) for item in leads]
    if leads and leads[0].get("mode") == "auto-luminance":
        separations = [item.get("_separation", 0.0) for item in leads]
        needed = max(2.0 * game_pixel, 0.25 * max(separations, default=0.0))
        for item in leads:
            if item["lead"] == "unclear":
                continue
            if item.get("_separation", 0.0) < needed:
                item.update(lead="unclear", reason=f"feet less than {needed / game_pixel:.1f} game px apart")
            elif abs(item.get("_delta", 0.0)) < AUTO_MIN_DELTA_L:
                item.update(lead="unclear", reason="the feet differ by less than one shade")
    for item in leads:
        item.pop("_delta", None)
        item.pop("_separation", None)
    half = count // 2
    pairs = [[index, index + half, leads[index]["lead"] == leads[index + half]["lead"]]
             for index in range(half)
             if leads[index]["lead"] != "unclear" and leads[index + half]["lead"] != "unclear"]
    if not pairs:
        verdict, status = "unknown: no pair of half-cycle frames shows a clear NEAR/FAR lead", "needs-visual-review"
    elif all(same for _, _, same in pairs):
        verdict, status = "duplicated half-cycle: the same leg leads in both halves", "fail"
    elif not any(same for _, _, same in pairs):
        verdict, status = "alternates", "pass"
    else:
        verdict, status = "mixed: some half-cycle pairs repeat the lead", "warn"
    return {"leads": leads, "pairs": pairs, "verdict": verdict, "status": status,
            "near_leading": sum(1 for item in leads if item["lead"] == "near"),
            "far_leading": sum(1 for item in leads if item["lead"] == "far"),
            "unclear": sum(1 for item in leads if item["lead"] == "unclear")}


def _reduce(frame: np.ndarray, factor: int) -> np.ndarray:
    """Box-reduce an RGBA frame by an integer factor (Pillow area filter), as float32."""
    if factor <= 1:
        return frame.astype(np.float32)
    height, width = frame.shape[0] // factor, frame.shape[1] // factor
    image = Image.fromarray(np.ascontiguousarray(frame[:height * factor, :width * factor]), "RGBA")
    return np.asarray(image.resize((width, height), Image.Resampling.BOX), dtype=np.float32)


def _premultiplied_rgb(pixels: np.ndarray) -> np.ndarray:
    return pixels[..., :3] * (pixels[..., 3:4] / 255.0)


def _head_geometry(frame: np.ndarray, head_frac: float, threshold: int) -> dict | None:
    """Head band box (top ``head_frac`` of the body) and its crown centroid (top 30% of the band)."""
    solid = frame[..., 3] > threshold
    extent = _extent(solid)
    if extent is None:
        return None
    first, last = _band_rows(extent, 0.0, head_frac)
    band = solid[first:last]
    columns = np.flatnonzero(band.any(axis=0))
    crown_rows = max(1, int(round(0.3 * (last - first))))
    crown_xs = np.nonzero(band[:crown_rows])[1]
    return {"box": (int(columns[0]), first, int(columns[-1]) + 1, last),
            "crown_x": float(crown_xs.mean()) + 0.5 if crown_xs.size else (columns[0] + columns[-1] + 1) / 2}


def _match_window(padded_pm: np.ndarray, padded_solid: np.ndarray, pad: int, size: tuple[int, int],
                  template_pm: np.ndarray, template_solid: np.ndarray, top: int, left: int,
                  reach: int) -> tuple[float, int, int]:
    """Best masked premultiplied-RGB MAE of a template placed at (top, left) +- reach px.

    The score averages |frame - template| over the union of both solid masks
    (report v2 head_scale_fast). Placements leaving the frame are skipped.
    Returns (score, top, left) of the first best placement in raster order.
    """
    height, width = template_solid.shape
    rows_total, cols_total = size
    ry, rx = top - reach + pad, left - reach + pad
    region_pm = padded_pm[ry:ry + height + 2 * reach, rx:rx + width + 2 * reach]
    region_solid = padded_solid[ry:ry + height + 2 * reach, rx:rx + width + 2 * reach]
    windows_pm = sliding_window_view(region_pm, (height, width), axis=(0, 1))
    windows_solid = sliding_window_view(region_solid, (height, width), axis=(0, 1))
    union = windows_solid | template_solid
    difference = np.abs(windows_pm - template_pm.transpose(2, 0, 1)[None, None]).mean(axis=2)
    score = (difference * union).sum(axis=(-1, -2)) / np.maximum(union.sum(axis=(-1, -2)), 1)
    offsets = np.arange(-reach, reach + 1)
    tops, lefts = top + offsets[:, None], left + offsets[None, :]
    valid = (tops >= 0) & (lefts >= 0) & (tops + height <= rows_total) & (lefts + width <= cols_total)
    score = np.where(valid, score, np.inf)
    row, column = np.unravel_index(int(np.argmin(score)), score.shape)
    return float(score[row, column]), int(tops[row, 0]), int(lefts[0, column])


def head_ratios(frames: Sequence[np.ndarray], *, reference: int = 0, master: np.ndarray | None = None,
                head_frac: float = 0.35, threshold: int = SOLID_THRESHOLD) -> dict:
    """Head scale of every frame, matched against one head template (report v2 3.2.5).

    The template is the head band (top ``head_frac`` of the body) of the
    ``reference`` frame, or of ``master``. Frames are box-reduced so the
    template is about 32 px tall. The template is aligned at full size by its
    crown (the top 30% of the band) and slid +-15% to find the head; then
    every scale 0.90..1.10 (step 0.02) is tried +-3 px around that spot. The
    best premultiplied-RGB match gives the frame's head scale. ``head_ratio``
    divides by the median scale, so a typical head is 1.00 whatever the
    reference; ``residual`` is the mean absolute premultiplied-RGB difference
    of the best match (identity drift).
    """
    geometry = [_head_geometry(frame, head_frac, threshold) for frame in frames]
    template_source = master if master is not None else frames[reference]
    template_geometry = _head_geometry(template_source, head_frac, threshold)
    if template_geometry is None or any(item is None for item in geometry):
        raise QcError("Every frame (and the master) needs solid pixels to measure the head.")
    tx0, ty0, tx1, ty1 = template_geometry["box"]
    factor = max(1, int(round((ty1 - ty0) / 32.0)))
    template = _reduce(template_source, factor)[ty0 // factor:ty1 // factor, tx0 // factor:tx1 // factor]
    if min(template.shape[:2]) < 4:
        raise QcError("The head band is too small to match; use larger frames or a larger --head-frac.")
    template_u8 = np.clip(template + 0.5, 0, 255).astype(np.uint8)
    crown_offset = (tx0 - template_geometry["crown_x"]) / factor  # template left edge relative to the crown
    base_scale = 1.0
    if master is not None:  # bring a master drawn at another size to the frames' scale first
        widths = [item["box"][2] - item["box"][0] for item in geometry]
        base_scale = float(np.median(widths)) / max(1, tx1 - tx0)
    templates = []
    for step in HEAD_SCALES:
        scale = round(base_scale * step, 4)
        width = max(4, int(round(template_u8.shape[1] * scale)))
        height = max(4, int(round(template_u8.shape[0] * scale)))
        scaled = np.asarray(Image.fromarray(template_u8, "RGBA").resize((width, height), Image.Resampling.BOX),
                            dtype=np.float32)
        templates.append((scale, _premultiplied_rgb(scaled), scaled[..., 3] >= 128))
    unit = templates[HEAD_SCALES.index(1.0)]
    search = max(3, int(round(0.15 * max(unit[2].shape))))
    pad = search + 3 + max(max(item[2].shape) for item in templates)
    results = []
    for index, frame in enumerate(frames):
        reduced = _reduce(frame, factor)
        size = reduced.shape[:2]
        padded_pm = np.pad(_premultiplied_rgb(reduced), ((pad, pad), (pad, pad), (0, 0)))
        padded_solid = np.pad(reduced[..., 3] >= 128, pad)
        crown_x = geometry[index]["crown_x"] / factor
        top = geometry[index]["box"][1] // factor
        unit_left = int(math.floor(crown_x + crown_offset * unit[0] + 0.5))
        score, found_top, found_left = _match_window(padded_pm, padded_solid, pad, size, unit[1], unit[2], top,
                                                     unit_left, search)
        if not math.isfinite(score):
            raise QcError(f"The head template does not fit inside frame {index}; check --head-frac.")
        best = (math.inf, 1.0)
        for scale, template_pm, template_solid in templates:
            left = int(math.floor(crown_x + crown_offset * scale + 0.5)) + found_left - unit_left
            score, _top, _left = _match_window(padded_pm, padded_solid, pad, size, template_pm, template_solid,
                                               found_top, left, 3)
            if score < best[0]:
                best = (score, scale)
        results.append({"frame": index, "scale": best[1], "residual": _round(best[0]),
                        "head_box": list(geometry[index]["box"])})
    median_scale = float(np.median([item["scale"] for item in results]))
    for item in results:
        item["head_ratio"] = _round(item["scale"] / median_scale, 3)
        item["scale"] = _round(item["scale"], 4)
    others = [item["residual"] for item in results if master is not None or item["frame"] != reference]
    median_residual = float(np.median(others)) if others else 0.0
    for item in results:
        is_template = master is None and item["frame"] == reference
        item["template"] = is_template
        item["residual_over_median"] = None if is_template or median_residual <= 0 else _round(
            item["residual"] / median_residual)
    return {"frames": results, "reduction": factor, "median_scale": _round(median_scale, 4),
            "median_residual": _round(median_residual), "scales": list(HEAD_SCALES),
            "template": "master" if master is not None else f"frame {reference}"}


def _best_shift_iou(first: np.ndarray, second: np.ndarray, max_shift: int) -> tuple[float, int, int]:
    """Best IoU of two masks over integer shifts of ``second`` within +-max_shift (report v2 near-duplicates).

    Returns (iou, dx, dy); ties keep the first shift in raster order (dy, then dx).
    """
    height, width = first.shape
    windows = sliding_window_view(np.pad(second, max_shift), (height, width))[::-1, ::-1]
    intersection = np.count_nonzero(windows & first, axis=(2, 3))
    union = int(first.sum()) + np.count_nonzero(windows, axis=(2, 3)) - intersection
    iou = np.where(union > 0, intersection / np.maximum(union, 1), 1.0)
    row, column = np.unravel_index(int(np.argmax(iou)), iou.shape)
    return float(iou[row, column]), int(column) - max_shift, int(row) - max_shift


def _color_histogram(frame: np.ndarray, threshold: int) -> np.ndarray:
    """Normalised 512-bin RGB histogram (8 levels per channel) of the solid pixels."""
    pixels = frame[frame[..., 3] > threshold][:, :3].astype(np.int64) // 32
    counts = np.bincount(pixels[:, 0] * 64 + pixels[:, 1] * 8 + pixels[:, 2], minlength=512).astype(np.float64)
    return counts / max(1.0, counts.sum())


# --------------------------------------------------------------------------- frames

def analyse_frames(frames: Sequence[np.ndarray], *, cycle: str = "none", facing: str = "right",
                   game_pixel: float = 1.0, row_size: int | None = None, loop: bool | None = None,
                   reference: int = 0, master: np.ndarray | None = None, head_frac: float = 0.35,
                   torso_colors: Sequence[Sequence[int]] = (), torso_band: tuple[float, float] = (0.40, 0.60),
                   near_colors: Sequence[Sequence[int]] = (), far_colors: Sequence[Sequence[int]] = (),
                   color_tolerance: float = 30.0, leg_band: float = 0.30, max_head_dev: float = 0.05,
                   max_residual_ratio: float = 1.5, near_dup_iou: float = 0.95, max_drift: float = 2.0,
                   max_baseline_step: float = 0.5, max_seam_ratio: float = 1.5,
                   threshold: int = SOLID_THRESHOLD) -> dict:
    """All frame-set checks (report v2 3.2.3-3.2.5) on frames sharing one canvas and registration."""
    count = len(frames)
    if count < 2:
        raise QcError("frames needs at least two frames.")
    if len({frame.shape for frame in frames}) != 1:
        raise QcError("All frames must share one canvas size; slice the sheet with --sheet or pad them first.")
    if cycle not in CYCLES or facing not in FACINGS:
        raise QcError(f"--cycle is one of {', '.join(CYCLES)} and --facing one of {', '.join(FACINGS)}.")
    if game_pixel <= 0 or not math.isfinite(game_pixel):
        raise QcError("--game-pixel must be a positive number of source pixels.")
    if not 0 <= reference < count:
        raise QcError(f"--reference-frame must be between 0 and {count - 1}.")
    if bool(near_colors) != bool(far_colors):
        raise QcError("Give both --near-colors and --far-colors, or neither (auto NEAR/FAR from lightness).")
    if not 0.05 <= head_frac <= 0.8 or not 0.05 <= leg_band <= 0.8:
        raise QcError("--head-frac and --leg-band are fractions of the body height between 0.05 and 0.8.")
    row_size = count if not row_size else row_size
    loop = cycle != "none" if loop is None else loop
    solids = [frame[..., 3] > threshold for frame in frames]
    extents = [_extent(mask) for mask in solids]
    empty = [index for index, extent in enumerate(extents) if extent is None]
    if empty:
        raise QcError(f"Frame(s) {empty} have no solid pixels (alpha > {threshold}).")
    records = []
    for index, (frame, mask, extent) in enumerate(zip(frames, solids, extents)):
        x0, top, x1, ground = extent
        records.append({"frame": index, "row": index // row_size, "bbox": [x0, top, x1, ground],
                        "height_px": ground - top, "width_px": x1 - x0, "solid_px": int(mask.sum()),
                        "ground": ground})
    checks: list[dict] = []

    # identity: head ratio against the median frame (or the master's head)
    heads = head_ratios(frames, reference=reference, master=master, head_frac=head_frac, threshold=threshold)
    histograms = [_color_histogram(frame, threshold) for frame in frames]
    median_histogram = np.median(np.stack(histograms), axis=0)
    median_histogram /= max(1e-12, median_histogram.sum())
    for record, head, histogram in zip(records, heads["frames"], histograms):
        record["head"] = {key: head[key] for key in ("head_ratio", "scale", "residual", "residual_over_median",
                                                     "template", "head_box")}
        record["colour_l1_to_median"] = _round(float(np.abs(histogram - median_histogram).sum()), 4)
    off_ratio = [head["frame"] for head in heads["frames"] if abs(head["head_ratio"] - 1.0) > max_head_dev]
    checks.append(check("identity_head_ratio", "fail" if off_ratio else "pass",
                        {str(head["frame"]): head["head_ratio"] for head in heads["frames"]},
                        [round(1 - max_head_dev, 3), round(1 + max_head_dev, 3)], frames=off_ratio,
                        note=f"head scale against the median frame ({heads['template']} template)"))
    drifted = [head["frame"] for head in heads["frames"]
               if head["residual_over_median"] is not None and head["residual_over_median"] > max_residual_ratio]
    checks.append(check("identity_residual", "warn" if drifted else "pass",
                        {str(head["frame"]): head["residual_over_median"] for head in heads["frames"]},
                        max_residual_ratio, frames=drifted,
                        note="head match residual over the median: face or costume drift"))

    # near-duplicates (best-shift silhouette IoU within +-3 game px, at most +-6 sampled steps) and the
    # half-cycle pairs
    step = max(heads["reduction"], math.ceil(3.0 * game_pixel / 6.0))
    small = [mask[::step, ::step] for mask in solids]
    max_shift = max(1, int(round(3.0 * game_pixel / step)))
    pairs = []
    for first in range(count):
        for second in range(first + 1, count):
            iou, dx, dy = _best_shift_iou(small[first], small[second], max_shift)
            pairs.append({"pair": [first, second], "iou_best_shift": _round(iou, 4),
                          "best_shift_src_px": [dx * step, dy * step],
                          "colour_l1": _round(float(np.abs(histograms[first] - histograms[second]).sum()), 4)})
    duplicates = [item["pair"] for item in pairs if item["iou_best_shift"] >= near_dup_iou and item["colour_l1"] <= 0.15]
    checks.append(check("near_duplicates", "warn" if duplicates else "pass", duplicates, near_dup_iou,
                        note="pairs with best-shift silhouette IoU at or above the threshold and similar colours"))
    by_pair = {tuple(item["pair"]): item for item in pairs}
    adjacent = [by_pair[(index, index + 1)]["iou_best_shift"] for index in range(count - 1)]
    if loop:
        adjacent.append(by_pair[(0, count - 1)]["iou_best_shift"])
    half_cycle = None
    if cycle != "none" and count % 2 == 0 and count >= 4:
        half = count // 2
        half_cycle = {"pairs": [{"pair": [index, index + half],
                                 "iou_best_shift": by_pair[(index, index + half)]["iou_best_shift"]}
                                for index in range(half)],
                      "adjacent_iou_median": _round(float(np.median(adjacent)), 4)}
        half_cycle["half_cycle_iou_median"] = _round(float(np.median(
            [item["iou_best_shift"] for item in half_cycle["pairs"]])), 4)

    # phases: ground clearance and stride width (report v2 run_phase)
    row_ground = {}
    for record in records:
        row_ground[record["row"]] = max(row_ground.get(record["row"], 0), record["ground"])
    stride = []
    for mask, extent in zip(solids, extents):
        first = extent[3] - max(1, int(math.floor(PHASE_SAMPLE_BAND * (extent[3] - extent[1]) + 0.5)))
        columns = np.flatnonzero(mask[first:extent[3]].any(axis=0))
        stride.append(int(columns[-1] - columns[0] + 1))
    widest = max(stride)
    for record, width in zip(records, stride):
        clearance = row_ground[record["row"]] - record["ground"]
        ratio = width / widest
        if clearance > game_pixel:
            label = "flight"
        elif ratio >= 0.75:
            label = "contact"
        elif ratio <= 0.55:
            label = "narrow"
        else:
            label = "mid"
        record["phase"] = {"label": label, "clearance_game_px": _round(clearance / game_pixel),
                           "stride_ratio": _round(ratio, 3)}
    if cycle == "none" or count % 2 or count < 4:
        checks.append(check("phase_coverage", "skipped", None, None,
                            note="needs --cycle run or walk and an even frame count of at least 4"))
    else:
        half = count // 2
        halves = [[records[index]["phase"]["label"] for index in range(start, start + half)] for start in (0, half)]
        problems = []
        for number, labels in enumerate(halves):
            if "contact" not in labels:
                problems.append(f"half {number} has no wide-stride contact frame")
            if cycle == "run" and "flight" not in labels:
                problems.append(f"half {number} has no flight frame")
            if cycle == "walk" and "flight" in labels:
                problems.append(f"half {number} leaves the ground; a walk keeps one foot down")
        checks.append(check("phase_coverage", "warn" if problems else "pass", halves, None, problems=problems,
                            note="labels from ground clearance and stride width; they cannot judge the pose itself"))

    # leading-leg alternation from NEAR/FAR colour groups (or the lightness convention)
    leads = [leg_lead(frame, facing=facing, near=near_colors, far=far_colors, tolerance=color_tolerance,
                      band=leg_band, game_pixel=game_pixel, threshold=threshold) for frame in frames]
    alternation = alternation_verdict(leads, game_pixel=game_pixel)
    for record, lead in zip(records, alternation["leads"]):
        record["lead"] = lead
    if cycle == "none" or count % 2 or count < 4:
        checks.append(check("leg_alternation", "skipped", None, None,
                            note="needs --cycle run or walk and an even frame count of at least 4"))
    else:
        checks.append(check("leg_alternation", alternation["status"],
                            [item["lead"] for item in alternation["leads"]], "lead differs between i and i+n/2",
                            verdict=alternation["verdict"], pairs=alternation["pairs"],
                            mode="colour" if near_colors else "auto-luminance",
                            near_leading=alternation["near_leading"], unclear=alternation["unclear"],
                            half_cycle=half_cycle,
                            fix=None if alternation["status"] == "pass" else
                            f"regenerate frames {count // 2}-{count - 1} with NEAR and FAR swapped (attach frames "
                            f"0-{count // 2 - 1} and the guide)"))

    # torso drift and row baselines, in game pixels
    torso = [torso_x(frame, colors=torso_colors, tolerance=color_tolerance, band=torso_band, threshold=threshold)
             for frame in frames]
    missing = [index for index, (value, _method) in enumerate(torso) if value is None]
    for record, (value, method) in zip(records, torso):
        record["torso_x"] = None if value is None else _round(value, 1)
        record["torso_method"] = method
    if missing:
        checks.append(check("torso_drift", "needs-visual-review", None, max_drift, frames=missing,
                            note="no torso found in these frames; check --torso-colors"))
        drift = None
    else:
        values = np.array([value for value, _ in torso])
        drift = float(values.max() - values.min()) / game_pixel
        checks.append(check("torso_drift", "warn" if drift > max_drift else "pass", _round(drift), max_drift,
                            method=torso[0][1], median_x=_round(float(np.median(values)), 1),
                            note="range of the torso x in game px; scale_frames --root-lock torso-x removes it"))
    rows_present = sorted(row_ground)
    baselines = {str(row): row_ground[row] for row in rows_present}
    step_size = (max(row_ground.values()) - min(row_ground.values())) / game_pixel
    checks.append(check("row_baseline", "warn" if step_size > max_baseline_step else "pass", _round(step_size),
                        max_baseline_step, baselines=baselines,
                        note="ground line of each sheet row; scale_frames --row-baseline aligns them"))

    # seam ranking (forge_core.transition_mae, premultiplied RGBA)
    steps = [(index, index + 1) for index in range(count - 1)] + ([(count - 1, 0)] if loop else [])
    transitions = [{"from": a, "to": b, "mae": _round(forge_core.transition_mae(frames[a], frames[b]))}
                   for a, b in steps]
    median_mae = float(np.median([item["mae"] for item in transitions]))
    for item in transitions:
        item["over_median"] = _round(item["mae"] / max(median_mae, 1e-6))
    ranking = sorted(transitions, key=lambda item: (-item["mae"], item["from"]))
    rough = [[item["from"], item["to"]] for item in ranking if item["over_median"] > max_seam_ratio]
    checks.append(check("seam", "warn" if rough else "pass",
                        _round(max(item["over_median"] for item in transitions)), max_seam_ratio, transitions=rough,
                        note="largest frame-to-frame change over the median step; review these joins first"))
    seam = forge_core.seam_report(frames) if loop else None
    return {
        "params": {"cycle": cycle, "facing": facing, "game_pixel": game_pixel, "frames_per_row": row_size,
                   "loop": loop, "alpha_threshold": threshold, "reference_frame": reference,
                   "head_frac": head_frac, "torso_band": list(torso_band),
                   "torso_colors": [hex_color(color) for color in torso_colors],
                   "near_colors": [hex_color(color) for color in near_colors],
                   "far_colors": [hex_color(color) for color in far_colors], "colour_tolerance": color_tolerance,
                   "leg_band": leg_band},
        "checks": checks,
        "cells": records,
        "identity": {"median_scale": heads["median_scale"], "median_residual": heads["median_residual"],
                     "reduction": heads["reduction"], "template": heads["template"], "scales": heads["scales"]},
        "near_duplicates": {"pairs": sorted(pairs, key=lambda item: -item["iou_best_shift"]),
                            "max_shift_src_px": max_shift * step},
        "half_cycle": half_cycle,
        "alternation": {key: alternation[key] for key in ("verdict", "pairs", "near_leading", "far_leading", "unclear")},
        "torso": {"drift_game_px": None if drift is None else _round(drift)},
        "row_baselines": baselines,
        "seam_ranking": ranking,
        "seam": seam,
    }


def render_frames_review(frames: Sequence[np.ndarray], analysis: dict, *, columns: int) -> Image.Image:
    """Frames on a light background with the ground line (blue), torso x (magenta), head band (green),
    NEAR/FAR centroids (orange/blue dots) and a label per frame."""
    height, width = frames[0].shape[:2]
    scale = min(1.0, 192.0 / height)
    tile_w, tile_h = max(1, int(round(width * scale))), max(1, int(round(height * scale)))
    label_h = 30
    columns = max(1, min(columns, len(frames)))
    rows = math.ceil(len(frames) / columns)
    sheet = Image.new("RGB", (columns * (tile_w + 8) + 8, rows * (tile_h + label_h + 8) + 8), (236, 232, 222))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (frame, record) in enumerate(zip(frames, analysis["cells"])):
        left = 8 + (index % columns) * (tile_w + 8)
        top = 8 + (index // columns) * (tile_h + label_h + 8)
        tile = forge_core.resample_rgba(Image.fromarray(frame, "RGBA"), scale, "box",
                                        out_size=(tile_w, tile_h)) if scale < 1 else Image.fromarray(frame, "RGBA")
        sheet.paste(tile, (left, top), tile)
        ground = top + record["ground"] * scale
        draw.line((left, ground, left + tile_w, ground), fill=(40, 90, 255), width=1)
        if record.get("torso_x") is not None:
            x = left + record["torso_x"] * scale
            draw.line((x, top, x, top + tile_h), fill=(220, 0, 200), width=1)
        hx0, hy0, hx1, hy1 = record["head"]["head_box"]
        draw.rectangle((left + hx0 * scale, top + hy0 * scale, left + hx1 * scale, top + hy1 * scale),
                       outline=(0, 160, 60), width=1)
        lead = record["lead"]
        for key, color in (("near_x", (255, 122, 0)), ("far_x", (32, 80, 160))):
            if key in lead:
                x, y = left + lead[key] * scale, ground - 4
                draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color)
        text = f"{index} {record['phase']['label']} lead={lead['lead']} head={record['head']['head_ratio']:.2f}"
        draw.text((left, top + tile_h + 4), forge_core.ascii_text(text), fill=(20, 20, 20), font=font)
    return sheet


# --------------------------------------------------------------------------- report and CLI

SPILL_METHOD = ("8-connected components at alpha > {threshold} (solid) and > {edge} (visible edge) on the raw sheet; "
                "a component belongs to the cell holding most of its pixels; cells are forge_core.rounded_grid_boxes; "
                "specks are forge_core.alpha_hygiene('both') counts")
SPILL_NOT_PROVEN = [
    "Whether an overhang is intended art such as a tail or weapon trail.",
    "Whether a cell's pose, anatomy or identity is correct; run sheet_qc.py frames and look at the frames.",
    "That a different crop or slicing method would be safe: only the nominal grid was measured.",
]
FRAMES_METHOD = ("solid alpha > {threshold}; head ratio by multi-scale template match of the head band against the "
                 "median frame; best-shift silhouette IoU; phases from ground clearance and stride width; NEAR/FAR lead "
                 "from {lead}; torso x from {torso}; seams from forge_core.transition_mae (premultiplied RGBA)")
FRAMES_NOT_PROVEN = [
    "Pose quality, anatomy, appeal and readability at game scale: look at frames-review.png and the game-size frames.",
    "That the head band holds only the head: arms raised into it change the match.",
    "NEAR/FAR leads in auto mode rely on the 'FAR limbs one shade darker' convention; declare the colours to be sure.",
    "Phase labels come from feet clearance and stride width, not from the pose.",
]


def _envelope(kind: str, analysis: dict, *, inputs: list[dict], outputs: list[dict], method: str,
              not_proven: list[str], extra: dict) -> dict:
    checks = analysis["checks"]
    document = {
        "schema": SCHEMA, "check": kind, "status": worst_status([item["status"] for item in checks]),
        "method": method, "notProven": not_proven, "checks": checks, "inputs": inputs, "outputs": outputs,
        "tool": dict(TOOL),
    }
    document.update(extra)
    document.update({key: value for key, value in analysis.items() if not key.startswith("_") and key != "checks"})
    return document


def _summary(document: dict, output_dir: Path, report: Path) -> dict:
    return {"status": document["status"], "check": document["check"], "output": str(output_dir),
            "metadata": str(report),
            "failed": [item["id"] for item in document["checks"] if item["status"] == "fail"],
            "warnings": [item["id"] for item in document["checks"] if item["status"] in ("warn", "needs-visual-review")]}


def _strict_failure(document: dict) -> str:
    failed = [item for item in document["checks"] if item["status"] == "fail"]
    details = "; ".join(f"{item['id']}={json.dumps(item['value'], ensure_ascii=True)}" for item in failed)
    return f"{document['check']} QC failed ({details}); nothing was published (drop --strict to keep the report)"


def cmd_spill(args: argparse.Namespace) -> dict:
    source = Path(args.input)
    pixels, info = load_input(source, args.key)
    analysis = analyse_spill(pixels, args.rows, args.cols, threshold=args.alpha_threshold,
                             edge_threshold=args.edge_threshold, min_area=args.min_area, band=args.band,
                             safe_margin=args.safe_margin, expect=args.expect)
    output_dir = Path(args.output_dir)
    with forge_core.staged_output(output_dir) as stage:
        overlay = stage / "spill-overlay.png"
        forge_core.save_png(render_spill_overlay(pixels, analysis), overlay)
        method = SPILL_METHOD.format(threshold=args.alpha_threshold, edge=args.edge_threshold)
        document = _envelope("spill", analysis, inputs=[forge_core.file_ref(source, stage)],
                             outputs=[forge_core.file_ref(overlay, stage)], method=method,
                             not_proven=list(SPILL_NOT_PROVEN), extra={"image": info})
        if args.strict and document["status"] == "fail":
            raise QcError(_strict_failure(document))
        forge_core.write_json(stage / REPORT_NAME, document)
    return _summary(document, output_dir, output_dir / REPORT_NAME)


def load_frames(args: argparse.Namespace) -> tuple[list[np.ndarray], list[dict], dict, int]:
    """Frames from --sheet (ownership slicing) or --frames, with their source records."""
    if args.sheet:
        if not args.rows or not args.cols:
            raise QcError("--sheet needs --rows and --cols.")
        sheet = Path(args.sheet)
        pixels, info = load_input(sheet, args.key)
        height, width = pixels.shape[:2]
        if width < args.cols or height < args.rows:
            raise QcError(f"A {width}x{height} sheet cannot hold {args.rows} rows x {args.cols} columns.")
        boxes = forge_core.rounded_grid_boxes(width, height, args.rows, args.cols)
        frames, slicing = ownership_slice(pixels, boxes, count=args.count)
        sources = [{"file": sheet, "cell": [index // args.cols, index % args.cols]} for index in range(len(frames))]
        return frames, sources, {"sheet": info, "slicing": slicing}, args.cols
    if not args.frames:
        raise QcError("Give --sheet with --rows/--cols, or --frames.")
    frames, sources, infos = [], [], []
    for name in args.frames:
        path = Path(name)
        pixels, info = load_input(path, args.key)
        frames.append(pixels)
        sources.append({"file": path, "cell": None})
        infos.append(info)
    return frames, sources, {"frames": infos}, args.frames_per_row or len(frames)


def cmd_frames(args: argparse.Namespace) -> dict:
    frames, sources, input_info, row_size = load_frames(args)
    master = load_input(Path(args.master), args.key)[0] if args.master else None
    near, far = parse_colors(args.near_colors), parse_colors(args.far_colors)
    torso_colors = parse_colors(args.torso_colors)
    analysis = analyse_frames(
        frames, cycle=args.cycle, facing=args.facing, game_pixel=args.game_pixel, row_size=row_size,
        loop=args.loop, reference=args.reference_frame, master=master, head_frac=args.head_frac,
        torso_colors=torso_colors, torso_band=parse_fraction_pair(args.torso_band, "--torso-band"),
        near_colors=near, far_colors=far, color_tolerance=args.colour_tol, leg_band=args.leg_band,
        max_head_dev=args.max_head_dev, near_dup_iou=args.near_dup_iou, max_drift=args.max_drift,
        max_baseline_step=args.max_baseline_step, max_seam_ratio=args.max_seam_ratio,
        threshold=args.alpha_threshold)
    for record, source in zip(analysis["cells"], sources):
        record["cell"] = source["cell"]
        record["source"] = source["file"].name
    output_dir = Path(args.output_dir)
    with forge_core.staged_output(output_dir) as stage:
        review = stage / "frames-review.png"
        forge_core.save_png(render_frames_review(frames, analysis, columns=row_size), review)
        unique = list(dict.fromkeys(source["file"] for source in sources))
        inputs = [forge_core.file_ref(path, stage) for path in unique]
        if args.master:
            inputs.append(forge_core.file_ref(Path(args.master), stage))
        method = FRAMES_METHOD.format(
            threshold=args.alpha_threshold,
            lead="the declared NEAR/FAR colours" if near else "the lightness of the two feet (FAR one shade darker)",
            torso="the declared torso colours" if torso_colors else "the trunk columns of the torso band")
        grid_rows = math.ceil(len(frames) / row_size)
        document = _envelope("frames", analysis, inputs=inputs, outputs=[forge_core.file_ref(review, stage)],
                             method=method, not_proven=list(FRAMES_NOT_PROVEN),
                             extra={"grid": {"rows": grid_rows, "cols": row_size}, "input": input_info})
        if args.strict and document["status"] == "fail":
            raise QcError(_strict_failure(document))
        forge_core.write_json(stage / REPORT_NAME, document)
    return _summary(document, output_dir, output_dir / REPORT_NAME)


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer; got {text}")
    return value


def _alpha(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 254:
        raise argparse.ArgumentTypeError(f"must be an alpha level 0-254; got {text}")
    return value


def _key(text: str) -> str:
    value = text.strip().lower()
    if value in KEYS or _HEX.fullmatch(value):
        return value if value in KEYS else "#" + value.lstrip("#")
    raise argparse.ArgumentTypeError(f"use auto, none, magenta, green, blue or #rrggbb; got {text}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--output-dir", required=True, help="new directory for sheet-qc.json and the review image")
        sub.add_argument("--key", type=_key, default="auto",
                         help="backdrop of opaque inputs: auto (keep real alpha, else key magenta), none, magenta, "
                              "green, blue or #rrggbb")
        sub.add_argument("--alpha-threshold", type=_alpha, default=SOLID_THRESHOLD,
                         help="solid pixels have alpha above this (default 127, i.e. >= 128)")
        sub.add_argument("--strict", action="store_true",
                         help="publish nothing when a check fails (a failing report exits 1 either way)")

    spill = commands.add_parser("spill", help="cross-cell spill on the raw sheet, before slicing",
                                description="Cross-cell spill, sheet-edge cuts, empty cells, boundary band, safe frame "
                                            "and faint specks of a raw sheet (sheet_qc.v1).")
    spill.add_argument("--input", required=True, help="the raw generated sheet (PNG)")
    spill.add_argument("--rows", type=_positive_int, required=True)
    spill.add_argument("--cols", type=_positive_int, required=True)
    common(spill)
    spill.add_argument("--edge-threshold", type=_alpha, default=forge_core.ALPHA_GEOMETRY_THRESHOLD,
                       help="visible edge for the second crossing test (default 16)")
    spill.add_argument("--min-area", type=_positive_int, default=64,
                       help="smaller solid components count as specks, not parts (default 64 px)")
    spill.add_argument("--band", type=_positive_int, default=None,
                       help="half-width of the band around each cell line in px (default 2%% of the cell)")
    spill.add_argument("--safe-margin", type=float, default=0.15,
                       help="safe frame inside each cell, as a fraction of the cell (default 0.15)")
    spill.add_argument("--expect", type=_positive_int, default=None,
                       help="number of cells that must hold a subject, in reading order (default all)")
    spill.set_defaults(handler=cmd_spill)

    frames = commands.add_parser("frames", help="identity, duplicates, phases, NEAR/FAR alternation, drift, seams",
                                 description="Frame-set checks on one action (sheet_qc.v1). Use --sheet to slice a raw "
                                             "sheet by component ownership, or --frames for frames on one canvas.")
    source = frames.add_mutually_exclusive_group(required=True)
    source.add_argument("--sheet", help="raw sheet, sliced by component ownership (needs --rows/--cols)")
    source.add_argument("--frames", nargs="+", help="frame PNGs that share one canvas, in playback order")
    frames.add_argument("--rows", type=_positive_int)
    frames.add_argument("--cols", type=_positive_int)
    frames.add_argument("--count", type=_positive_int, default=None,
                        help="with --sheet: use the first N cells in reading order; later cells may be empty")
    frames.add_argument("--frames-per-row", type=_positive_int, default=None,
                        help="with --frames: frames per sheet row, for row baselines (default all in one row)")
    common(frames)
    frames.add_argument("--cycle", choices=CYCLES, default="none",
                        help="run or walk enables the phase and leading-leg checks (default none)")
    frames.add_argument("--facing", choices=FACINGS, default="right", help="direction the character faces")
    frames.add_argument("--loop", action=argparse.BooleanOptionalAction, default=None,
                        help="count the last->first seam (default: on for run/walk)")
    frames.add_argument("--game-pixel", type=float, default=1.0,
                        help="source pixels per game pixel, e.g. 8 for an 8x pixel-art sheet (default 1)")
    frames.add_argument("--master", help="accepted master image whose head is the identity template")
    frames.add_argument("--reference-frame", type=int, default=0, help="head template frame without --master")
    frames.add_argument("--head-frac", type=float, default=0.35,
                        help="share of the body height, from the top, that holds the head (default 0.35)")
    frames.add_argument("--torso-colors", help="costume colours of the torso, #rrggbb,... (default: trunk geometry)")
    frames.add_argument("--torso-band", default="0.40,0.60",
                        help="torso rows as fractions of the body height from the top (default 0.40,0.60)")
    frames.add_argument("--near-colors", help="NEAR (viewer-side) limb colours, #rrggbb,...")
    frames.add_argument("--far-colors", help="FAR (away-side) limb colours, #rrggbb,...")
    frames.add_argument("--colour-tol", type=float, default=30.0, help="RGB distance for colour groups (default 30)")
    frames.add_argument("--leg-band", type=float, default=0.30,
                        help="bottom share of the body searched for NEAR/FAR colours (default 0.30)")
    frames.add_argument("--max-head-dev", type=float, default=0.05, help="allowed head ratio deviation (default 0.05)")
    frames.add_argument("--near-dup-iou", type=float, default=0.95, help="near-duplicate IoU (default 0.95)")
    frames.add_argument("--max-drift", type=float, default=2.0, help="allowed torso drift in game px (default 2)")
    frames.add_argument("--max-baseline-step", type=float, default=0.5,
                        help="allowed ground-line difference between sheet rows in game px (default 0.5)")
    frames.add_argument("--max-seam-ratio", type=float, default=1.5,
                        help="flag transitions above this multiple of the median step (default 1.5)")
    frames.set_defaults(handler=cmd_frames)
    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = args.handler(args)
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if summary["status"] == "fail" else 0  # D26: a published failing report exits 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry (D26, D27): usage errors exit 2 (argparse); a published report whose status is fail
    exits 1; every other failure prints one ``error: ...`` line and exits 1."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
