#!/usr/bin/env python3
"""Finish registered clips into game-ready frames: HD (the default) or pixel art.

  hd             The default finish. One scale per clip from the rest pose: --target-height
                 (output px of the rest-pose body; frame 0 or --rest-frame) or --scale-ref
                 (a sibling clip's finish.json: its scale x sqrt(rest-area ratio), clamped
                 to +-3 percent). Premultiplied box (area) downscale with
                 forge_core.resample_rgba on a grid pinned to the anchor (the stance midpoint
                 on the feet line), then forge_core.alpha_hygiene. Never nearest, never an
                 upscale. --display-sizes 1x,2x re-derives every size from the source frames.
  pixel          Bold pixel art from the same registration: a crisp downscale (each output
                 pixel takes the dominant of two OKLab colour clusters of its 4x4 sub-pixels,
                 so edges do not blur into in-between colours and thin dark lines and eyes
                 survive), a light contrast and saturation lift, one OKLab palette of at
                 most --colors (default 32) colours (--palette, or built from the clip and
                 spaced so no two colours are closer than --shade-gap, OKLab dE 0.04), no
                 dither, temporal hysteresis (forge_palette.quantize_sequence), lone-pixel
                 cleanup that keeps eyes and highlights, speck cleanup, binary alpha at 0.5
                 and a 1 px outline (--outline selective|dark|none). --downscale box
                 --contrast 1 --saturation 1 --outline none is the plain v1 finish.
                 --indexed-sheet adds an indexed PNG sheet.
  --canvas WxH   (hd and pixel) every frame on a fixed cell, the anchor at one point
                 (--canvas-anchor X,Y, or centred over the feet); a clip too big for the
                 cell is finished smaller so it fits (warned).
  --colour-lock  (hd and pixel) MASTER.png or rest: keeps every design colour on the
                 master's colours in OKLab (colour_lock.py): region hue shifts are undone,
                 near colours are pulled softly toward their master colour (lightness kept,
                 no posterising) and still pixels are smoothed over time.
  palette build  One OKLab k-means++ palette across every action of a character or cast,
                 with reserved fixed colours; deterministic for a --seed.
  lineup         A cast line-up at x1 and x3 that checks the size rules (body height from
                 the rest pose; spirits by opaque area) and writes a JSON report beside it.

Input frames are registered straight-alpha RGBA frames (register_clip apply: its frames/
folder or its output folder). Finished frames keep the source file names, so a gait_loop
or retime selection made on the registered frames still applies to them. hd and pixel
write a new --output-dir (frames/, frames-<k>x/, review-contact.png, finish.json and, in
pixel mode, palette.json); palette build and lineup write new files. Nothing is ever
replaced, and a failed run publishes nothing. Success prints one ASCII JSON line; errors
print "error: ..." and exit 1 (a lineup whose rules fail still writes its report, then
exits 1); usage errors exit 2. Library use: finish_clip(), build_cast_palette(), lineup().
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import forge_core  # noqa: E402  (this skill's vendored copies)
import forge_palette as fp  # noqa: E402
import colour_lock as colour_lock_module  # noqa: E402  (sibling: the colour lock)

TOOL_NAME = "finish_frames"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # QA envelopes record the package version (D29)
FINISH_SCHEMA = "video2dsprite.finish.v1"
LINEUP_SCHEMA = "video2dsprite.lineup.v1"
FINISH_FILE = "finish.json"
CONTACT_FILE = "review-contact.png"
PALETTE_FILE = "palette.json"
SHEET_FILE = "sheet-indexed.png"

MODES = ("hd", "pixel")
ANCHORS = ("feet", "center")
LOOP_POLICIES = ("cycle", "pingpong", "oneshot")

CONTOUR = 127               # body = alpha > 127, the 50% contour (pixelate.py: alpha > 0.5)
REST_MIN_RUN = 6            # source px a row needs to count for the top and feet lines (pixelate.py _sil)
OUTPUT_MIN_RUN = 2          # the same rule on finished frames (line-ups, measured rest height)
STANCE_BAND = 0.02          # ground-contact band: 2% of the rest height (12 rows on ~600 px bodies)
HYGIENE_FLOOR = 4           # alpha <= 4 is invisible resampling halo (D18); also the canvas crop threshold
SCALE_REF_CLAMP = 0.03      # the scale-ref area correction is clamped to +-3% (pixelate.py)
SCALE_REF_WARN = 0.01       # a correction beyond 1% warns: is the rest frame really the base still?
DEFAULT_COLORS = 255        # palette build: 255 colours + transparent index 255 (game-opus55: 47 fixed + 208 learned)
PIXEL_COLORS = 32           # pixel finish without --palette: a 45-90 px sprite reads as pixel art with 16-32 colours
PIXEL_ALPHA = 0.5           # binary alpha threshold of the pixel finish
PIXEL_ORPHANS = 1           # lone-pixel cleanup passes of the plain (v1) pixel finish (forge_palette.cleanup_orphans)
DOWNSCALES = ("crisp", "box")
OUTLINES = ("selective", "dark", "none")
SUPERSAMPLE = 4             # crisp downscale: 4x4 sub-pixels per output pixel (box-reduced from the source)
CRISP_ITERATIONS = 4        # Lloyd iterations of the two-cluster split per output pixel
DETAIL_GAP = 0.15           # OKLab L gap that makes the darker cluster a detail (a line, an eye, a seam) ...
DETAIL_SHARE = 0.35         # ... which then wins from 35% of the pixel's coverage instead of 50%
HOLD_SHARE = 0.30           # crisp hysteresis: the previous frame's cluster stays while it holds 30% of the pixel
HOLD_DELTA_E = 0.06         # ... and is within OKLab dE 0.06 of the previous choice
LIFT_CONTRAST = 1.10        # pixel lift: OKLab lightness spread around the rest pose's mean lightness
LIFT_SATURATION = 1.15      # pixel lift: OKLab chroma scale
SHADE_GAP = 0.04            # bold pixel finish: no two learned palette colours closer than this OKLab dE (flat
                            # colour clusters; k-means alone put 8 oranges 0.03 apart on the 2026-10-06 fox)
FEATURE_DELTA_L = 0.22      # a lone pixel this much lighter or darker than every 4-neighbour is a feature (kept)
PIXEL_CLEAN_PASSES = 2      # lone-pixel passes of the bold finish
OUTLINE_MAX_L = 0.34        # an outline colour is at most this light (OKLab L) ...
OUTLINE_DROP = 0.22         # ... and at least this much darker than the colour it outlines
OUTLINE_PX = 1
PIXEL_MIN_REDUCTION = 4.0   # a pixel finish reducing less than 4x warns (pixel art wants 1/8 to 1/12)
STILL_ALPHA = 13            # source alpha change below 13/255 (5%) counts as still in the flicker QA
REST_HEIGHT_TOLERANCE = 1.5  # output px between the measured and the nominal rest height before a warning
SET_HEIGHT_TOLERANCE = 1.0  # output px spread of the rest heights of one character's clips before a warning
CONTACT_FRAMES = 12
SHEET_MAX_WIDTH = 4096
PALETTE_FRAMES_PER_SET = 12     # palette build: frames sampled per set (evenly spaced)
PALETTE_SAMPLES_PER_SET = 22000  # palette build: opaque pixels sampled per set (pixelate.py budget)
DEFAULT_RULES = "hero:1.0,mob:<=1.0,boss:~2.0,spirit:~1.0"
DEFAULT_AREA_ROLES = ("spirit",)
EXACT_TOLERANCE = 0.02
ABOUT_TOLERANCE = 0.10
BACKGROUND = (46, 46, 56)
GROUND_COLOUR = (120, 120, 140)
REFERENCE_COLOUR = (200, 170, 60)
TEXT_COLOUR = (220, 220, 228)

HD_METHOD = ("finish_frames hd v1: one scale per clip from the rest pose (target height / rest height at the 50% "
             "alpha contour, rows with >= 6 px; or scale-ref x sqrt(rest-area ratio) clamped to +-3%), "
             "forge_core.resample_rgba box (area) on premultiplied float channels with the output grid pinned to the "
             "anchor (stance midpoint on the feet line, or the alpha centroid), forge_core.alpha_hygiene(both, floor "
             "4), canvas = union of alpha > 4 plus 1 px; never nearest, never an upscale; display sizes re-derived "
             "from the source frames")
PIXEL_METHOD = ("finish_frames pixel v1 (port of game-opus55 pixelate.py): the hd registration and box downscale, "
                "then one OKLab palette (given, or forge_palette.build_palette k-means++ on the finished frames), "
                "forge_palette.quantize_sequence with temporal hysteresis (index kept within the margin, opacity "
                "kept inside alpha 0.4-0.6), cleanup_orphans (1 pass) and cleanup_alpha per frame, binary alpha at "
                "0.5, no dither; flicker counted per frame pair against per-frame nearest quantizing")
BOLD_METHOD = ("finish_frames pixel v2 (bold): the hd registration; alpha from the box downscale (the hd silhouette); "
               "colour from a 4x4 supersampled box downscale split per output pixel into two OKLab clusters (4 Lloyd "
               "steps), the heavier cluster winning, a cluster 0.15 L darker from 35% (lines and eyes survive), the "
               "previous frame's cluster kept from 30% (no 50/50 flicker); OKLab lift (lightness spread around the "
               "rest pose's mean, chroma scale); one palette (given, or k-means++ on the lifted frames, then spaced: "
               "the closest pair of learned colours merged, usage-weighted, until no two are closer than the shade "
               "gap, OKLab dE 0.04 by default, the darkest, the lightest and reserved colours kept in place); "
               "forge_palette.quantize_sequence hysteresis and cleanup_alpha; lone pixels merged into their "
               "neighbours' majority unless 0.22 L lighter or darker than every neighbour (features kept), 2 passes; "
               "a 1 px outline outside the 4-connected silhouette in the palette's dark shade of the darkest "
               "neighbour (selective) or its darkest colour (dark); the target height includes the outline and the "
               "feet anchor moves to the outline's bottom edge; no dither, binary alpha at 0.5")


class FinishError(ValueError):
    """A user-facing refusal: one ``error: ...`` line, exit 1, nothing published."""


# --------------------------------------------------------------------------- small helpers

def _natural_key(path: Path) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def _ceil(value: float) -> int:
    """Ceiling that ignores float noise just above an integer (23.000000001 -> 23)."""
    return int(math.ceil(value - 1e-9))


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return None
    columns = np.flatnonzero(mask.any(axis=0))
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


def _check(check_id: str, value: Any, threshold: Any, ok: bool | None, *, warn: bool = False,
           **extra: Any) -> dict[str, Any]:
    """A qaCheck; ``ok=None`` is an ungated measurement (skipped), a failed ``warn`` check warns."""
    status = "skipped" if ok is None else "pass" if ok else "warn" if warn else "fail"
    return {"id": check_id, "status": status, "value": value, "threshold": threshold, **extra}


def _envelope(checks: list[dict[str, Any]], method: str, not_proven: Sequence[str],
              inputs: Sequence[dict[str, Any]], outputs: Sequence[dict[str, Any]], *,
              passed: str = "needs-visual-review") -> dict[str, Any]:
    """A common qaEnvelope; the status is the worst check, ``passed`` when nothing failed or warned."""
    statuses = {item["status"] for item in checks}
    status = "fail" if "fail" in statuses else "warn" if "warn" in statuses else passed
    return {"status": status, "method": method, "notProven": list(not_proven), "checks": checks,
            "inputs": list(inputs), "outputs": list(outputs), "tool": {"name": TOOL_NAME, "version": TOOL_VERSION}}


def _load(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    image, info = forge_core.load_rgba(path)
    return np.asarray(image, dtype=np.uint8), info


def _frame_files(folder: Path, pattern: str = "*.png") -> list[Path]:
    return sorted((path for path in folder.glob(pattern) if path.is_file()), key=_natural_key)


def _factor_label(factor: float) -> str:
    return f"{factor:g}x"


def _size_dir(factor: float) -> str:
    return "frames" if factor == 1.0 else f"frames-{factor:g}x"


def parse_display_sizes(value: Any) -> list[float]:
    """``"1x,2x"`` (or numbers) -> factors, 1.0 first, duplicates dropped; every factor is positive."""
    items = value.split(",") if isinstance(value, str) else list(value)
    factors = [1.0]
    for item in items:
        text = str(item).strip().lower()
        if not text:
            continue
        try:
            factor = float(text[:-1] if text.endswith("x") else text)
        except ValueError:
            raise FinishError(f"bad display size {item!r}; use factors such as 1x,2x") from None
        if not math.isfinite(factor) or factor <= 0:
            raise FinishError(f"display size {item!r} must be a positive factor such as 2x")
        if factor not in factors:
            factors.append(factor)
    return factors


def _union(frames: Sequence[np.ndarray], threshold: int) -> tuple[int, int, int, int] | None:
    mask = np.zeros(frames[0].shape[:2], bool)
    for frame in frames:
        mask |= frame[..., 3] > threshold
    return _bbox(mask)


def _pack(rgb: np.ndarray) -> np.ndarray:
    """``(..., 3)`` uint8 colours as 24-bit integers."""
    channels = np.asarray(rgb).astype(np.uint32)
    return (channels[..., 0] << 16) | (channels[..., 1] << 8) | channels[..., 2]


# --------------------------------------------------------------------------- inputs

def resolve_frames(frames_dir: str | os.PathLike, pattern: str = "*.png") -> tuple[Path, list[Path], Path | None]:
    """The frames folder (a register_clip output folder means its frames/), its frames in natural
    order and the registration.json beside them, if any. Finished frames are refused."""
    folder = Path(frames_dir)
    if not folder.is_dir():
        raise FinishError(f"frames folder not found: {folder}")
    if (folder / "frames").is_dir():
        folder = folder / "frames"
    for manifest in (folder / FINISH_FILE, folder.parent / FINISH_FILE):
        if manifest.is_file():
            raise FinishError(f"{folder} holds finished frames ({manifest.name}); finish from the registered frames: "
                              "a finished sprite is never rescaled")
    files = _frame_files(folder, pattern)
    if not files:
        raise FinishError(f"no frames matching {pattern} in {folder}")
    registration = next((candidate for candidate in (folder / "registration.json",
                                                      folder.parent / "registration.json")
                         if candidate.is_file()), None)
    return folder, files, registration


# --------------------------------------------------------------------------- body, scale and grid

@dataclass(frozen=True)
class Body:
    """A body inside the 50% alpha contour: the rows with at least ``min_run`` px set the top and feet lines."""

    top: int
    ground: int
    area: int
    stance_x: float                 # ground-contact midpoint (continuous px)
    centroid: tuple[float, float]   # mean pixel centre (continuous px)

    @property
    def height(self) -> int:
        return self.ground - self.top


def measure_body(alpha: np.ndarray, min_run: int = REST_MIN_RUN) -> Body | None:
    """pixelate.py ``_sil``: area, feet line (under the lowest row with >= min_run px, so specks do not
    count), top, centroid and the stance midpoint of the ground-contact band above the feet line."""
    mask = np.asarray(alpha) > CONTOUR
    counts = mask.sum(axis=1)
    rows = np.flatnonzero(counts >= min_run)
    if rows.size == 0:
        rows = np.flatnonzero(counts > 0)
        if rows.size == 0:
            return None
    top, ground = int(rows[0]), int(rows[-1]) + 1
    band = max(2, forge_core.round_half_up(STANCE_BAND * (ground - top)))
    contact = np.flatnonzero(mask[max(0, ground - band):ground].any(axis=0))
    ys, xs = np.nonzero(mask)
    return Body(top, ground, int(xs.size), (int(contact[0]) + int(contact[-1]) + 1) / 2,
                (float(xs.mean()) + 0.5, float(ys.mean()) + 0.5))


@dataclass(frozen=True)
class ScalePlan:
    scale: float
    source: str                     # "target-height" or "scale-ref"
    target_height: float            # rest-pose body height in output px at the base (1x) size
    reference: dict[str, Any] | None
    reference_path: Path | None
    checks: tuple[dict[str, Any], ...]


def load_finish(value: Any) -> tuple[dict[str, Any], Path | None]:
    """A finish.json document from a mapping, a finish.json path or a finish output folder."""
    if isinstance(value, Mapping):
        document, path = dict(value), None
    else:
        path = Path(value)
        if path.is_dir():
            path = path / FINISH_FILE
        if not path.is_file():
            raise FinishError(f"finish.json not found: {path}")
        try:
            document = forge_core.read_json(path)
        except ValueError as error:
            raise FinishError(f"{path.name} is not valid JSON: {error}") from None
    if not isinstance(document, dict) or document.get("schema") != FINISH_SCHEMA:
        raise FinishError(f"not a {FINISH_SCHEMA} document: {path or 'mapping'}")
    return document, path


def plan_scale(body: Body, target_height: float | None, scale_ref: Any, *, area_norm: bool = True,
               character: str | None = None) -> ScalePlan:
    """target height / rest height, or (pixelate.py prep_vframes) the reference clip's scale times
    sqrt(reference rest area / this rest area), clamped to +-3%; ``area_norm=False`` takes it as is."""
    if (target_height is None) == (scale_ref is None):
        raise FinishError("give a target height or a scale reference (exactly one of them)")
    if target_height is not None:
        target = float(target_height)
        if not math.isfinite(target) or target <= 0:
            raise FinishError(f"target height must be a positive number of output px, got {target_height!r}")
        return ScalePlan(target / body.height, "target-height", target, None, None, ())
    document, path = load_finish(scale_ref)
    try:
        ref_scale = float(document["scale"])
        ref_area = float(document["rest"]["areaPx"])
    except (KeyError, TypeError, ValueError):
        raise FinishError("the scale reference lacks scale or rest.areaPx; re-finish it with this tool") from None
    if not (math.isfinite(ref_scale) and ref_scale > 0 and math.isfinite(ref_area) and ref_area > 0):
        raise FinishError("the scale reference has a non-positive scale or rest area")
    ref_character = document.get("character")
    if character and ref_character and str(character) != str(ref_character):
        raise FinishError(f"the scale reference belongs to character {ref_character!r}, not {character!r}; "
                          "scale-ref inherits a scale within one character")
    correction = math.sqrt(ref_area / body.area)
    applied = min(1.0 + SCALE_REF_CLAMP, max(1.0 - SCALE_REF_CLAMP, correction)) if area_norm else 1.0
    scale = ref_scale * applied
    checks = []
    if area_norm:
        checks.append(_check("scale-ref-area", round(correction - 1.0, 5), SCALE_REF_WARN,
                             abs(correction - 1.0) <= SCALE_REF_WARN, warn=True,
                             note="sqrt(reference rest area / rest area) - 1; beyond 1% the rest frame may not be "
                                  "the base still"))
        checks.append(_check("scale-ref-clamp", round(correction, 5), [1.0 - SCALE_REF_CLAMP, 1.0 + SCALE_REF_CLAMP],
                             applied == correction, warn=True))
    reference = {"clip": document.get("clip"), "character": ref_character, "scale": ref_scale,
                 "restAreaPx": ref_area, "areaRatio": round(body.area / ref_area, 6),
                 "correction": round(correction, 6), "applied": round(applied, 6),
                 "clamped": bool(area_norm and applied != correction), "areaNorm": bool(area_norm)}
    return ScalePlan(scale, "scale-ref", body.height * scale, reference, path, tuple(checks))


@dataclass(frozen=True)
class Grid:
    """One display size: the output grid pinned so the source anchor lands on ``anchor`` exactly."""

    factor: float
    scale: float
    size: tuple[int, int]           # full canvas covering the whole source frame
    anchor: tuple[int, int]


def full_grid(source_size: Sequence[int], anchor_src: Sequence[float], scale: float, factor: float) -> Grid:
    """pixelate.py's canvas rule (ax = ceil((fx - x0) * sc) + 1, ...) over the whole source frame; the
    finished canvas is cropped from it later, so every frame shares one grid phase."""
    fx, fy = anchor_src
    width, height = source_size
    ax, ay = _ceil(fx * scale) + 1, _ceil(fy * scale) + 1
    return Grid(factor, scale, (ax + max(0, _ceil((width - fx) * scale)) + 1,
                                ay + max(0, _ceil((height - fy) * scale)) + 1), (ax, ay))


def reduce_frame(pixels: np.ndarray, box: tuple[int, int, int, int] | None, anchor_src: Sequence[float],
                 grid: Grid) -> np.ndarray:
    """One source frame on ``grid``: forge_core.resample_rgba box (area) on premultiplied channels, the
    source anchor landing exactly on the grid anchor. The frame is first cut to ``box`` (its visible
    pixels) with the anchor shifted by the same whole pixels, which resample_rgba guarantees gives
    byte-identical output for less work."""
    if box is None:
        return np.zeros((grid.size[1], grid.size[0], 4), np.uint8)
    x0, y0, x1, y1 = box
    return np.asarray(forge_core.resample_rgba(pixels[y0:y1, x0:x1], grid.scale, "box",
                                               anchor_src=(anchor_src[0] - x0, anchor_src[1] - y0),
                                               anchor_dst=grid.anchor, out_size=grid.size), np.uint8)


# --------------------------------------------------------------------------- flicker and pictures

def flicker(states: Sequence[np.ndarray], sources: Sequence[np.ndarray], *, transparent: int | None,
            loop: bool) -> dict[str, Any] | None:
    """Mean counts per consecutive frame pair (plus last -> first for a cycle).

    ``alphaFlipsPerPair``: pixels whose opacity toggles (pixel: binary alpha; hd: the 50% contour).
    ``indexFlipsPerPair``: pixels opaque in both frames whose palette index changes (pixel only).
    ``noiseFlipsPerPair``: either change where the finished source barely moved (OKLab dE < 0.02 and
    alpha within 5%): pure flicker. ``edgeFlipsPerPair``: the opacity toggles among those, the
    silhouette-edge flicker. Real motion counts in the first two, never in the last two.
    """
    count = len(states)
    if count < 2:
        return None
    pairs = [(index - 1, index) for index in range(1, count)]
    if loop and count > 2:
        pairs.append((count - 1, 0))
    labs = [fp.to_oklab(frame[..., :3]) for frame in sources]
    alphas = [frame[..., 3].astype(np.int16) for frame in sources]
    still_limit = np.float32(fp.STILL_DELTA_E ** 2)
    alpha_total = index_total = noise_total = edge_total = 0
    for first, second in pairs:
        a, b = states[first], states[second]
        if transparent is None:
            opaque_a, opaque_b = a > CONTOUR, b > CONTOUR
            changed = np.zeros(a.shape, bool)
        else:
            opaque_a, opaque_b = a != transparent, b != transparent
            changed = opaque_a & opaque_b & (a != b)
        toggled = opaque_a ^ opaque_b
        still = ((((labs[second] - labs[first]) ** 2).sum(-1) < still_limit)
                 & (np.abs(alphas[second] - alphas[first]) < STILL_ALPHA))
        alpha_total += int(toggled.sum())
        index_total += int(changed.sum())
        noise_total += int((still & (toggled | changed)).sum())
        edge_total += int((still & toggled).sum())
    total = len(pairs)
    return {"pairs": total, "alphaFlipsPerPair": round(alpha_total / total, 3),
            "indexFlipsPerPair": None if transparent is None else round(index_total / total, 3),
            "noiseFlipsPerPair": round(noise_total / total, 3), "edgeFlipsPerPair": round(edge_total / total, 3)}


def _zoom(frame: np.ndarray, factor: int) -> np.ndarray:
    """Integer nearest zoom of a REVIEW picture (never of a delivered frame)."""
    return frame if factor == 1 else np.repeat(np.repeat(frame, factor, axis=0), factor, axis=1)


def _composite(canvas: np.ndarray, sprite: np.ndarray, x: int, y: int) -> None:
    """Straight-alpha ``sprite`` over the float RGB ``canvas`` at (x, y), clipped to the canvas."""
    height, width = sprite.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(canvas.shape[1], x + width), min(canvas.shape[0], y + height)
    if x0 >= x1 or y0 >= y1:
        return
    part = sprite[y0 - y:y1 - y, x0 - x:x1 - x]
    alpha = part[..., 3:4].astype(np.float32) / 255.0
    region = canvas[y0:y1, x0:x1]
    region *= 1.0 - alpha
    region += part[..., :3].astype(np.float32) * alpha


def _rgb8(canvas: np.ndarray) -> np.ndarray:
    return np.clip(np.floor(canvas + 0.5), 0, 255).astype(np.uint8)


def contact_sheet(frames: Sequence[np.ndarray]) -> np.ndarray:
    """Frames side by side at x1 and, when it fits in 4096 px, x3 (else x2): nearest review zoom."""
    height, width = frames[0].shape[:2]
    gap = 4

    def row_width(zoom: int) -> int:
        return gap + len(frames) * (width * zoom + gap)

    zooms = [1] + [zoom for zoom in (3, 2) if row_width(zoom) <= SHEET_MAX_WIDTH][:1]
    canvas = np.empty((gap + sum(height * zoom + gap for zoom in zooms), max(map(row_width, zooms)), 3), np.float32)
    canvas[...] = BACKGROUND
    y = gap
    for zoom in zooms:
        x = gap
        for frame in frames:
            _composite(canvas, _zoom(frame, zoom), x, y)
            x += width * zoom + gap
        y += height * zoom + gap
    return _rgb8(canvas)


def _contact_indices(count: int) -> list[int]:
    shown = min(CONTACT_FRAMES, count)
    return sorted({forge_core.round_half_up(i * (count - 1) / max(1, shown - 1)) for i in range(shown)})


# --------------------------------------------------------------------------- finish

def _resolve_palette(palette: Any) -> tuple[fp.Palette, Path | None]:
    try:
        if isinstance(palette, (str, os.PathLike)):
            return fp.read_palette(palette), Path(palette)
        return fp.as_palette(palette), None
    except fp.PaletteError as error:
        raise FinishError(str(error)) from None


def _quantize(frames: list[np.ndarray], pal: fp.Palette, loop_policy: str, margin: float,
              orphans: int = PIXEL_ORPHANS) -> tuple[list[np.ndarray], list[dict[str, int]], list[np.ndarray]]:
    """Hysteresis index maps with their per-frame stats, and the per-frame nearest baseline maps."""
    common = {"margin": margin, "alpha_threshold": PIXEL_ALPHA, "orphans": orphans, "despeckle": True}
    maps, stats = fp.quantize_sequence(frames, pal, loop=loop_policy == "cycle", pingpong=loop_policy == "pingpong",
                                       return_stats=True, **common)
    baseline = [fp.quantize_sequence([frame], pal, loop=False, **common)[0] for frame in frames]
    return maps[:len(frames)], stats, baseline


def _indexed_sheet(maps: Sequence[np.ndarray], slot: int) -> tuple[np.ndarray, int, int]:
    height, width = maps[0].shape
    columns = max(1, min(len(maps), SHEET_MAX_WIDTH // width))
    rows = (len(maps) + columns - 1) // columns
    sheet = np.full((rows * height, columns * width), slot, np.uint8)
    for index, frame in enumerate(maps):
        row, column = divmod(index, columns)
        sheet[row * height:(row + 1) * height, column * width:(column + 1) * width] = frame
    return sheet, columns, rows


# --------------------------------------------------------------------------- bold pixel art

def supersampled(grid: Grid, factor: int = SUPERSAMPLE) -> Grid:
    """``grid`` with factor x factor sub-pixels per output pixel, pinned to the same anchor: output pixel
    (x, y) covers exactly the sub-pixels [x*f, (x+1)*f) x [y*f, (y+1)*f)."""
    return Grid(grid.factor, grid.scale * factor, (grid.size[0] * factor, grid.size[1] * factor),
                (grid.anchor[0] * factor, grid.anchor[1] * factor))


@dataclass
class Clusters:
    """Two OKLab colour clusters per output pixel (crisp downscale): centres and the dark one's share."""

    dark: np.ndarray        # (H, W, 3) OKLab
    light: np.ndarray       # (H, W, 3) OKLab
    share: np.ndarray       # (H, W) share of the pixel's coverage held by the dark cluster


def crisp_clusters(sub: np.ndarray, factor: int = SUPERSAMPLE) -> Clusters:
    """Split the factor x factor sub-pixels of every output pixel into two OKLab clusters (alpha-weighted
    Lloyd steps from the darkest and the lightest solid sub-pixel). Only the visible window is computed."""
    height, width = sub.shape[0] // factor, sub.shape[1] // factor
    dark = np.zeros((height, width, 3), np.float32)
    light = np.zeros((height, width, 3), np.float32)
    share = np.zeros((height, width), np.float32)
    box = _bbox(sub[..., 3] > 0)
    if box is None:
        return Clusters(dark, light, share)
    x0, y0 = box[0] // factor, box[1] // factor
    x1, y1 = min(width, -(-box[2] // factor)), min(height, -(-box[3] // factor))
    window = sub[y0 * factor:y1 * factor, x0 * factor:x1 * factor]
    rows, columns = y1 - y0, x1 - x0
    blocks = window.reshape(rows, factor, columns, factor, 4).transpose(0, 2, 1, 3, 4).reshape(
        rows, columns, factor * factor, 4)
    weight = blocks[..., 3].astype(np.float64) / 255.0
    lab = fp.to_oklab(blocks[..., :3]).astype(np.float64)
    lightness = lab[..., 0]
    solid = weight >= 0.5
    any_solid = solid.any(-1, keepdims=True)
    usable = np.where(any_solid, solid, weight > 0)
    low = np.argmin(np.where(usable, lightness, np.inf), axis=-1)
    high = np.argmax(np.where(usable, lightness, -np.inf), axis=-1)

    def centre(index: np.ndarray) -> np.ndarray:
        return np.take_along_axis(lab, index[..., None, None].repeat(3, -1), axis=-2)[..., 0, :]

    first, second = centre(low), centre(high)
    first_weight = second_weight = None
    for _ in range(CRISP_ITERATIONS):
        to_first = ((lab - first[..., None, :]) ** 2).sum(-1)
        to_second = ((lab - second[..., None, :]) ** 2).sum(-1)
        second_weight = weight * (to_second < to_first)
        first_weight = weight - second_weight
        sums = first_weight.sum(-1), second_weight.sum(-1)
        first = np.where(sums[0][..., None] > 0, (lab * first_weight[..., None]).sum(-2)
                         / np.maximum(sums[0], 1e-12)[..., None], first)
        second = np.where(sums[1][..., None] > 0, (lab * second_weight[..., None]).sum(-2)
                          / np.maximum(sums[1], 1e-12)[..., None], second)
    s_first, s_second = first_weight.sum(-1), second_weight.sum(-1)
    swap = second[..., 0] < first[..., 0]
    dark[y0:y1, x0:x1] = np.where(swap[..., None], second, first)
    light[y0:y1, x0:x1] = np.where(swap[..., None], first, second)
    total = np.maximum(s_first + s_second, 1e-12)
    share[y0:y1, x0:x1] = np.where(swap, s_second, s_first) / total
    return Clusters(dark, light, share)


def crisp_choice(clusters: Clusters, previous: np.ndarray | None) -> np.ndarray:
    """The OKLab colour of every output pixel: the heavier cluster; a darker cluster (DETAIL_GAP) from
    DETAIL_SHARE, so lines and eyes survive; and the previous frame's cluster while it holds HOLD_SHARE."""
    dark, light, share = clusters.dark, clusters.light, clusters.share
    gap = light[..., 0] - dark[..., 0]
    pick_dark = share >= np.where(gap > DETAIL_GAP, DETAIL_SHARE, 0.5)
    if previous is not None:
        to_dark = ((dark - previous) ** 2).sum(-1)
        to_light = ((light - previous) ** 2).sum(-1)
        limit = HOLD_DELTA_E ** 2
        hold_dark = (to_dark < limit) & (to_dark < to_light) & (share >= HOLD_SHARE)
        hold_light = (to_light < limit) & (to_light < to_dark) & (1.0 - share >= HOLD_SHARE)
        pick_dark = np.where(hold_dark, True, np.where(hold_light, False, pick_dark))
    return np.where(pick_dark[..., None], dark, light).astype(np.float32)


def lift_frame(frame: np.ndarray, contrast: float, saturation: float, pivot: float) -> np.ndarray:
    """OKLab lift of a straight-alpha RGBA frame: lightness spread around ``pivot`` by ``contrast``, chroma
    scaled by ``saturation`` (1, 1 is the identity); alpha and invisible pixels are untouched."""
    if contrast == 1.0 and saturation == 1.0:
        return frame
    out = frame.copy()
    visible = frame[..., 3] > 0
    if not visible.any():
        return out
    lab = fp.to_oklab(frame[..., :3][visible]).astype(np.float64)
    lab[:, 0] = np.clip(pivot + (lab[:, 0] - pivot) * contrast, 0.0, 1.0)
    lab[:, 1:] *= saturation
    out[..., :3][visible] = fp.from_oklab(lab)
    return out


def space_palette(pal: fp.Palette, frames: Sequence[np.ndarray], gap: float = SHADE_GAP) -> tuple[fp.Palette, int]:
    """``pal`` with no two colours closer than ``gap`` (OKLab dE), and the number of colours merged away.

    k-means spends its colours where the pixels are: a fur that fills half the sprite gets eight oranges 0.03
    apart, and at 4x those near-identical shades read as speckle instead of shading. The closest pair merges
    first, into its usage-weighted OKLab mean (usage: the opaque pixels of ``frames`` nearest to each colour),
    until every pair is at least ``gap`` apart, so each material keeps a few clearly distinct tones. Reserved
    colours never move (a learned colour close to one merges into it) and the darkest and the lightest colour
    keep their place (the outline and the highlights); untouched colours keep their exact RGB. Learned colours
    stay darkest first after the reserved ones, as build_palette orders them. A palette whose transparent index
    is a colour slot is returned unchanged."""
    count = len(pal)
    slot = pal.transparent_index
    if not gap > 0 or count < 3 or (slot is not None and slot < count):
        return pal, 0
    lab = pal.lab.astype(np.float64).copy()
    weights = np.full(count, 1e-6)
    opaque = [np.asarray(frame)[..., :3][np.asarray(frame)[..., 3] >= 128] for frame in frames]
    opaque = [part for part in opaque if len(part)]
    if opaque:
        weights += np.bincount(fp.nearest_index(np.concatenate(opaque), pal), minlength=count)[:count]
    fixed = np.array(pal.reserved, bool)
    alive = np.ones(count, bool)
    rgb = {i: tuple(int(v) for v in pal.colors[i]) for i in range(count)}
    merged = 0
    while alive.sum() > 2:
        index = np.flatnonzero(alive)
        sub = lab[index]
        distance = np.sqrt(((sub[:, None, :] - sub[None, :, :]) ** 2).sum(-1))
        np.fill_diagonal(distance, np.inf)
        distance[np.ix_(fixed[index], fixed[index])] = np.inf     # two reserved colours never merge
        first, second = np.unravel_index(int(np.argmin(distance)), distance.shape)
        if not distance[first, second] < gap:
            break
        keep, drop = int(index[first]), int(index[second])
        extremes = {int(index[np.argmin(sub[:, 0])]), int(index[np.argmax(sub[:, 0])])}
        if fixed[drop] or (drop in extremes and not fixed[keep]):
            keep, drop = drop, keep
        if not (fixed[keep] or keep in extremes):
            mean = (lab[keep] * weights[keep] + lab[drop] * weights[drop]) / (weights[keep] + weights[drop])
            rgb[keep] = tuple(int(v) for v in fp.from_oklab(mean[None])[0])
            lab[keep] = fp.to_oklab(np.array([rgb[keep]], np.uint8))[0]   # measured as stored: 8-bit RGB
        weights[keep] += weights[drop]
        alive[drop] = False
        merged += 1
    if not merged:
        return pal, 0
    reserved = [i for i in range(count) if alive[i] and fixed[i]]
    learned = [i for i in range(count) if alive[i] and not fixed[i]]
    learned.sort(key=lambda i: (float(lab[i][0]), rgb[i]))
    colours = [tuple(pal.colors[i]) for i in reserved]
    for i in learned:
        if rgb[i] not in colours:
            colours.append(rgb[i])
    kept = len(colours) - len(reserved)
    return pal.replace(colors=tuple(colours), names=tuple(pal.names[i] for i in reserved) + (None,) * kept,
                       reserved=(True,) * len(reserved) + (False,) * kept,
                       source=f"{pal.source}; spaced to OKLab dE >= {gap:g} ({count - len(colours)} merged)"), \
        count - len(colours)


def clean_lone_pixels(idx: np.ndarray, pal: fp.Palette, passes: int = PIXEL_CLEAN_PASSES) -> tuple[np.ndarray, int]:
    """Lone pixels (no 4-neighbour of their own index) take the neighbours' majority index, as
    forge_palette.cleanup_orphans, except features: a pixel FEATURE_DELTA_L lighter or darker than every
    4-neighbour (an eye, a highlight) stays. Transparent pixels never change and never spread."""
    slot = pal.transparent_index
    lightness = np.full(256, np.nan)
    lightness[:len(pal)] = pal.lab[:, 0]
    data = np.array(idx, copy=True)
    height, width = data.shape
    fixed = 0
    for _ in range(int(passes)):
        padded = np.pad(data, 1, mode="edge")
        up, down = padded[0:height, 1:width + 1], padded[2:height + 2, 1:width + 1]
        left, right = padded[1:height + 1, 0:width], padded[1:height + 1, 2:width + 2]
        lonely = (up != data) & (down != data) & (left != data) & (right != data)
        majority = np.where((up == down) | (up == left) | (up == right), up,
                            np.where((down == left) | (down == right), down, np.where(left == right, left, data)))
        own = lightness[data]
        gaps = np.abs(np.stack([lightness[up], lightness[down], lightness[left], lightness[right]]) - own[None])
        gaps = np.where(np.isnan(gaps), np.inf, gaps)   # a transparent neighbour never vetoes a feature
        feature = gaps.min(axis=0) >= FEATURE_DELTA_L
        fix = lonely & (majority != data) & (majority != slot) & (data != slot) & ~feature
        fixed += int(fix.sum())
        data = np.where(fix, majority, data)
    return data, fixed


def outline_shades(pal: fp.Palette) -> np.ndarray:
    """Per palette index, the index that outlines it: the palette's dark shade of the same hue (OKLab L at
    most min(L - OUTLINE_DROP, OUTLINE_MAX_L), nearest to 45% of its lightness and 80% of its chroma, hue
    weighted 3x), else the darkest colour. The transparent index maps to itself."""
    lab = pal.lab.astype(np.float64)
    darkest = int(np.argmin(lab[:, 0]))
    table = np.full(256, darkest, np.int64)
    for index, (lightness, a, b) in enumerate(lab):
        limit = min(lightness - OUTLINE_DROP, OUTLINE_MAX_L)
        candidates = np.flatnonzero(lab[:, 0] <= limit)
        if candidates.size:
            target = np.array([min(0.45 * lightness, limit), 0.8 * a, 0.8 * b])
            distance = (((lab[candidates] - target) ** 2) * np.array([1.0, 3.0, 3.0])).sum(-1)
            table[index] = int(candidates[np.argmin(distance)])
    if pal.transparent_index is not None:
        table[pal.transparent_index] = pal.transparent_index
    return table


def add_outline(idx: np.ndarray, pal: fp.Palette, mode: str, shades: np.ndarray | None = None
                ) -> tuple[np.ndarray, int]:
    """A 1 px outline on the transparent pixels 4-adjacent to the sprite: ``selective`` paints each one in
    the dark shade (outline_shades) of the darkest of its 8 neighbours, ``dark`` in the palette's darkest
    colour. Needs a transparent border of 1 px; returns the map and the number of outline pixels."""
    slot = pal.transparent_index
    if mode == "none":
        return idx, 0
    height, width = idx.shape
    lightness = np.full(256, np.inf)
    lightness[:len(pal)] = pal.lab[:, 0]
    padded = np.pad(idx, 1, constant_values=slot)
    ring = np.zeros(idx.shape, bool)
    best = np.full(idx.shape, slot, idx.dtype)
    best_l = np.full(idx.shape, np.inf)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            neighbour = padded[1 + dy:1 + dy + height, 1 + dx:1 + dx + width]
            if abs(dy) + abs(dx) == 1:
                ring |= neighbour != slot
            darker = lightness[neighbour] < best_l
            best = np.where(darker, neighbour, best)
            best_l = np.where(darker, lightness[neighbour], best_l)
    ring &= idx == slot
    out = idx.copy()
    if mode == "dark":
        out[ring] = int(np.argmin(pal.lab[:, 0]))
    else:
        table = outline_shades(pal) if shades is None else shades
        out[ring] = table[best[ring]].astype(idx.dtype)
    return out, int(ring.sum())


def parse_canvas(value: Any) -> tuple[int, int] | None:
    """``"48x64"`` (or a pair) -> (48, 64); None stays None."""
    if value is None:
        return None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)\s*[xX,]\s*(\d+)\s*", value)
        if not match:
            raise FinishError(f"canvas {value!r} must be WIDTHxHEIGHT, such as 48x64")
        width, height = int(match.group(1)), int(match.group(2))
    else:
        try:
            width, height = (int(item) for item in value)
        except (TypeError, ValueError):
            raise FinishError(f"canvas {value!r} must be a width and a height") from None
    if width < 4 or height < 4:
        raise FinishError(f"canvas {width}x{height} is too small (at least 4x4)")
    return width, height


def parse_point(value: Any, name: str = "canvas anchor") -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*", value)
        if not match:
            raise FinishError(f"{name} {value!r} must be X,Y in whole output px")
        return int(match.group(1)), int(match.group(2))
    try:
        x, y = (int(item) for item in value)
    except (TypeError, ValueError):
        raise FinishError(f"{name} {value!r} must be X,Y in whole output px") from None
    return x, y


def canvas_window(content: tuple[int, int, int, int], anchor: tuple[int, int], canvas: tuple[int, int],
                  canvas_anchor: tuple[int, int] | None) -> tuple[tuple[int, int, int, int] | None, tuple[int, int],
                                                                   float]:
    """Where a ``canvas`` cell sits on a full grid: ((x0, y0, x1, y1) on the grid or None when the content
    does not fit, the anchor inside the cell, the scale factor that would make it fit).

    ``content`` is the union box of everything drawn (outline included) on the grid and ``anchor`` the grid
    anchor. Without ``canvas_anchor`` the anchor goes to the cell's centre column and one px above its bottom
    edge, then moves the least that keeps the content inside the cell."""
    width, height = canvas
    left, right = anchor[0] - content[0], content[2] - anchor[0]
    top, bottom = anchor[1] - content[1], content[3] - anchor[1]
    if canvas_anchor is None:
        fits = left + right <= width and top + bottom <= height
        x = min(max(width // 2, left), width - right)
        y = min(max(height - 1, top), height - bottom)
    else:
        x, y = canvas_anchor
        fits = left <= x and right <= width - x and top <= y and bottom <= height - y
    if not fits:
        if canvas_anchor is None:
            factors = [width / max(1, left + right), height / max(1, top + bottom)]
        else:
            factors = [x / left if left > 0 else math.inf, (width - x) / right if right > 0 else math.inf,
                       y / top if top > 0 else math.inf, (height - y) / bottom if bottom > 0 else math.inf]
        return None, (x, y), max(0.0, min(factors))
    return (anchor[0] - x, anchor[1] - y, anchor[0] - x + width, anchor[1] - y + height), (x, y), 1.0


def cut_window(frame: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    """``frame[y0:y1, x0:x1]`` with transparent padding where the box leaves the frame."""
    x0, y0, x1, y1 = box
    out = np.zeros((y1 - y0, x1 - x0) + frame.shape[2:], frame.dtype)
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(frame.shape[1], x1), min(frame.shape[0], y1)
    if sx0 < sx1 and sy0 < sy1:
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = frame[sy0:sy1, sx0:sx1]
    return out


@dataclass
class _Pass:
    """One pass over the source frames: per display size the box frames, the cleaned frames and, for the
    crisp pixel downscale, the crisp frames (cleaned alpha, cluster colour)."""

    small: dict[float, list[np.ndarray]]
    cleaned: dict[float, list[np.ndarray]]
    crisp: dict[float, list[np.ndarray]] | None
    hygiene: dict[float, dict[str, int]]
    sources: list[dict[str, Any]]


def _source_pass(files: Sequence[Path], rest_frame: int, rest: tuple[np.ndarray, dict[str, Any]],
                 source_size: tuple[int, int], anchor_src: Sequence[float], grids: Sequence[Grid], crisp: bool,
                 seed_frame: int | None) -> _Pass:
    """Every display size is derived from the source frame, never from another size. ``seed_frame`` (cycles:
    the last frame, pingpong: frame 1) seeds the crisp hysteresis of frame 0."""
    small: dict[float, list[np.ndarray]] = {grid.factor: [] for grid in grids}
    cleaned: dict[float, list[np.ndarray]] = {grid.factor: [] for grid in grids}
    crisp_frames: dict[float, list[np.ndarray]] | None = {grid.factor: [] for grid in grids} if crisp else None
    hygiene = {grid.factor: {"floor_px": 0, "detached_px": 0, "detached_components": 0} for grid in grids}
    previous: dict[float, np.ndarray | None] = {grid.factor: None for grid in grids}
    subgrids = {grid.factor: supersampled(grid) for grid in grids}

    def load(index: int) -> tuple[np.ndarray, dict[str, Any]]:
        pixels, info = rest if index == rest_frame else _load(files[index])
        if (int(pixels.shape[1]), int(pixels.shape[0])) != source_size:
            raise FinishError(f"{files[index].name} is {pixels.shape[1]}x{pixels.shape[0]}, not {source_size[0]}x"
                              f"{source_size[1]}: registered frames share one canvas")
        return pixels, info

    if crisp and seed_frame is not None and len(files) > 1:
        pixels, _info = load(seed_frame)
        visible = _bbox(pixels[..., 3] > 0)
        for grid in grids:
            sub = reduce_frame(pixels, visible, anchor_src, subgrids[grid.factor])
            previous[grid.factor] = crisp_choice(crisp_clusters(sub), None)
        del pixels
    sources: list[dict[str, Any]] = []
    for index, path in enumerate(files):
        pixels, info = load(index)
        sources.append({"sourceFile": path.name, "sourceSha256": info["sha256"]})
        visible = _bbox(pixels[..., 3] > 0)
        for grid in grids:
            reduced = reduce_frame(pixels, visible, anchor_src, grid)
            clean_image, report = forge_core.alpha_hygiene(reduced, mode="both", floor=HYGIENE_FLOOR)
            clean = np.asarray(clean_image, np.uint8)
            small[grid.factor].append(reduced)
            cleaned[grid.factor].append(clean)
            for key in hygiene[grid.factor]:
                hygiene[grid.factor][key] += int(report[key])
            if crisp_frames is not None:
                sub = reduce_frame(pixels, visible, anchor_src, subgrids[grid.factor])
                choice = crisp_choice(crisp_clusters(sub), previous[grid.factor])
                previous[grid.factor] = choice
                frame = np.zeros_like(clean)
                frame[..., :3] = fp.from_oklab(choice)
                frame[..., 3] = clean[..., 3]
                frame[frame[..., 3] == 0] = 0
                crisp_frames[grid.factor].append(frame)
        del pixels
    return _Pass(small, cleaned, crisp_frames, hygiene, sources)


def _source_extents(files: Sequence[Path], anchor_src: Sequence[float]) -> tuple[float, float, float, float]:
    """(left, right, top, bottom) source px of the union of every frame's visible pixels (alpha above the hygiene
    floor) around the anchor."""
    box = None
    for path in files:
        with Image.open(path) as image:
            frame_box = image.convert("RGBA").getchannel("A").point(lambda v: 255 if v > HYGIENE_FLOOR else 0).getbbox()
        if frame_box:
            box = frame_box if box is None else (min(box[0], frame_box[0]), min(box[1], frame_box[1]),
                                                 max(box[2], frame_box[2]), max(box[3], frame_box[3]))
    if box is None:
        raise FinishError("every source frame is empty")
    fx, fy = anchor_src
    return max(0.0, fx - box[0]), max(0.0, box[2] - fx), max(0.0, fy - box[1]), max(0.0, box[3] - fy)


def fit_scale(extents: tuple[float, float, float, float], scale: float, canvas: tuple[int, int],
              canvas_anchor: tuple[int, int] | None, fixed: int) -> float:
    """The largest scale (at most ``scale``) at which content of source ``extents`` (left, right, top,
    bottom around the anchor) plus ``fixed`` output px on every side fits the canvas (the anchor free, or
    at ``canvas_anchor``)."""
    left, right, top, bottom = extents
    width, height = canvas
    if canvas_anchor is None:
        limits = [(width - 2 * fixed) / max(left + right, 1e-9), (height - 2 * fixed) / max(top + bottom, 1e-9)]
    else:
        x, y = canvas_anchor
        limits = [(x - fixed) / left if left > 0 else math.inf, (width - x - fixed) / right if right > 0 else math.inf,
                  (y - fixed) / top if top > 0 else math.inf, (height - y - fixed) / bottom if bottom > 0 else math.inf]
    return max(0.0, min([scale] + limits))


def _master_for_lock(colour_lock: Any, rest_pixels: np.ndarray) -> tuple[Any, dict[str, Any] | None]:
    """colour_lock.master_colours of ``rest`` (the rest frame), an RGBA image path or an RGBA array."""
    try:
        if isinstance(colour_lock, str) and colour_lock.strip().lower() == "rest":
            return colour_lock_module.master_colours(rest_pixels), {"source": "rest"}
        if isinstance(colour_lock, (str, os.PathLike)):
            path = Path(colour_lock)
            if not path.is_file():
                raise FinishError(f"--colour-lock master not found: {path}")
            return colour_lock_module.master_colours(path), {"path": path}
        return colour_lock_module.master_colours(np.asarray(colour_lock)), {"source": "array"}
    except colour_lock_module.ColourLockError as error:
        raise FinishError(str(error)) from None


def finish_clip(frames_dir: str | os.PathLike, out_dir: str | os.PathLike, *, mode: str = "hd",
                target_height: float | None, scale_ref: Any = None, palette: Any = None, rest_frame: int = 0,
                anchor: str = "feet", display_sizes: Any = (1.0,), colors: int | None = None,
                reserve: Sequence[str] = (), seed: int = 0, loop_policy: str = "cycle",
                margin: float = fp.HYSTERESIS_MARGIN, area_norm: bool = True, indexed_sheet: bool = False,
                pattern: str = "*.png", clip: str | None = None, character: str | None = None,
                role: str | None = None, strict: bool = False, downscale: str | None = None,
                contrast: float | None = None, saturation: float | None = None, outline: str | None = None,
                canvas: Any = None, canvas_anchor: Any = None, colour_lock: Any = None,
                lock_strength: float = 1.0, shade_gap: float | None = None) -> dict[str, Any]:
    """Finish one registered clip into ``out_dir`` (new; staged, published only after QA).

    ``mode`` is ``hd`` (default) or ``pixel``. Exactly one of ``target_height`` (output px of the
    rest-pose body at 1x; with a pixel outline the outline is included) and ``scale_ref`` (a sibling
    clip's finish.json, its folder or the loaded document) sets the scale. ``palette`` (pixel only) is a
    palette file, a forge_palette.Palette or a palette mapping; without it ``colors`` (default 32) colours
    are learned from the finished frames, ``reserve`` colours first. Pixel only: ``downscale`` (crisp or
    box), ``contrast`` and ``saturation`` (the lift; 1 is off), ``outline`` (selective, dark or none) and
    ``shade_gap`` (a learned palette keeps no two colours closer than this OKLab dE; 0 is off); the
    defaults are the bold finish. ``canvas`` (WxH) and ``canvas_anchor`` (X,Y) put every frame on a fixed
    cell. ``colour_lock`` (a keyed master image or "rest") locks the design colours (colour_lock.py).
    Returns the summary the CLI prints (paths, scale, palette colours, QA).
    """
    if mode not in MODES:
        raise FinishError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
    if anchor not in ANCHORS:
        raise FinishError(f"anchor must be one of {', '.join(ANCHORS)}, got {anchor!r}")
    if loop_policy not in LOOP_POLICIES:
        raise FinishError(f"loop policy must be one of {', '.join(LOOP_POLICIES)}, got {loop_policy!r}")
    pixel_only = (palette, colors, downscale, contrast, saturation, outline, shade_gap)
    if mode == "hd" and (any(value is not None for value in pixel_only) or reserve or indexed_sheet):
        raise FinishError("palette, colors, reserve, indexed sheets, downscale, contrast, saturation, outline and the "
                          "shade gap belong to the pixel finish")
    if palette is not None and (colors is not None or reserve):
        raise FinishError("colors and reserve build a palette; drop them when a palette is given")
    if colors is not None and (isinstance(colors, bool) or not 2 <= int(colors) <= 255):
        raise FinishError("colors must be 2..255 (the transparent index needs a free slot)")
    if not (math.isfinite(float(margin)) and float(margin) >= 0):
        raise FinishError("the hysteresis margin must be >= 0")
    if mode == "pixel":
        downscale = "crisp" if downscale is None else str(downscale)
        outline = "selective" if outline is None else str(outline)
        contrast = LIFT_CONTRAST if contrast is None else float(contrast)
        saturation = LIFT_SATURATION if saturation is None else float(saturation)
        if downscale not in DOWNSCALES:
            raise FinishError(f"downscale must be one of {', '.join(DOWNSCALES)}, got {downscale!r}")
        if outline not in OUTLINES:
            raise FinishError(f"outline must be one of {', '.join(OUTLINES)}, got {outline!r}")
        for name, value in (("contrast", contrast), ("saturation", saturation)):
            if not (math.isfinite(value) and 0.5 <= value <= 2.0):
                raise FinishError(f"{name} must be 0.5..2 (1 is no change), got {value!r}")
        shade_gap = SHADE_GAP if shade_gap is None else float(shade_gap)
        if not (math.isfinite(shade_gap) and 0.0 <= shade_gap <= 0.3):
            raise FinishError(f"the shade gap must be 0..0.3 (OKLab dE; 0 is off), got {shade_gap!r}")
    if not 0.0 <= float(lock_strength) <= 1.0:
        raise FinishError("the colour-lock strength must be 0..1")
    cell = parse_canvas(canvas)
    cell_anchor = parse_point(canvas_anchor)
    if cell_anchor is not None and cell is None:
        raise FinishError("a canvas anchor needs a canvas (--canvas WxH)")
    if cell is not None and cell_anchor is not None and not (0 <= cell_anchor[0] <= cell[0]
                                                              and 0 <= cell_anchor[1] <= cell[1]):
        raise FinishError(f"canvas anchor {cell_anchor} lies outside the {cell[0]}x{cell[1]} canvas")
    outline_px = OUTLINE_PX if mode == "pixel" and outline != "none" else 0
    crisp = mode == "pixel" and downscale == "crisp"
    bold = mode == "pixel" and (crisp or outline_px or contrast != 1.0 or saturation != 1.0)
    factors = parse_display_sizes(display_sizes)
    folder, files, registration = resolve_frames(frames_dir, pattern)
    if isinstance(rest_frame, bool) or not 0 <= int(rest_frame) < len(files):
        raise FinishError(f"rest frame {rest_frame} is outside the clip (0..{len(files) - 1})")
    rest_frame = int(rest_frame)
    out = Path(out_dir)
    if out.name in ("", ".", ".."):
        raise FinishError(f"the output folder needs a name: {out}")
    final = out.parent.resolve() / out.name
    if os.path.lexists(final):
        raise FileExistsError(errno.EEXIST, "Refusing to replace existing output", str(final))

    rest_pixels, rest_info = _load(files[rest_frame])
    if (rest_pixels[..., 3] == 255).all():
        raise FinishError(f"{files[rest_frame].name} has no transparency: key the clip (video2dsprite.py process or "
                          "clean) and register it (register_clip.py apply) before finishing")
    body = measure_body(rest_pixels[..., 3])
    if body is None:
        raise FinishError(f"rest frame {rest_frame} ({files[rest_frame].name}) has no body above 50% alpha")
    anchor_src = (body.stance_x, float(body.ground)) if anchor == "feet" else body.centroid
    body_target = None
    if target_height is not None:
        body_target = float(target_height) - 2 * outline_px
        if not body_target > 0:
            raise FinishError(f"target height {target_height} leaves no body inside a {outline_px} px outline")
    plan = plan_scale(body, body_target, scale_ref, area_norm=area_norm, character=character)
    checks: list[dict[str, Any]] = list(plan.checks)
    lock_master, lock_source = (None, None) if colour_lock is None else _master_for_lock(colour_lock, rest_pixels)
    source_size = (int(rest_pixels.shape[1]), int(rest_pixels.shape[0]))
    extents = _source_extents(files, anchor_src) if cell is not None else None
    if cell is not None:
        fitted = fit_scale(extents, plan.scale, cell, cell_anchor, outline_px + 1)
        if fitted < plan.scale * (1.0 - 1e-9):
            fitted *= 0.995
            checks.append(_check("canvas-fit", {"targetHeight": round(plan.target_height + 2 * outline_px, 3),
                                                "fittedHeight": round(body.height * fitted + 2 * outline_px, 3)},
                                 list(cell), False, warn=True,
                                 note="the clip did not fit the canvas at the target; it was finished smaller"))
            plan = ScalePlan(fitted, plan.source, body.height * fitted, plan.reference, plan.reference_path,
                             plan.checks)
    for factor in factors:
        scale = plan.scale * factor
        if scale > 1.0 + 1e-9:
            what = "the target" if factor == 1.0 else f"display size {_factor_label(factor)}"
            raise FinishError(f"{what} would upscale: scale {scale:.4f} > 1 ({plan.target_height * factor:.1f} px "
                              f"from a {body.height} px rest pose); finishing never upscales, so lower the target "
                              "or regenerate the source larger")
    seed_frame = {"cycle": len(files) - 1, "pingpong": 1}.get(loop_policy) if len(files) > 1 else None
    rest = (rest_pixels, rest_info)
    sizes: list[dict[str, Any]] = []
    for attempt in range(3):
        grids = [full_grid(source_size, anchor_src, plan.scale * factor, factor) for factor in factors]
        passed = _source_pass(files, rest_frame, rest, source_size, anchor_src, grids, crisp, seed_frame)
        # Crop each size to the union of its visible pixels (plus the outline) plus 1 px, or place it on the
        # canvas; the anchor moves with the crop (and to the outline's bottom edge for the feet anchor).
        sizes, retry = [], None
        for grid in grids:
            box = _union(passed.cleaned[grid.factor], HYGIENE_FLOOR)
            if box is None:
                raise FinishError("every frame is empty at the finished size; check the input frames and the target")
            content = (box[0] - outline_px, box[1] - outline_px, box[2] + outline_px, box[3] + outline_px)
            pin = (grid.anchor[0], grid.anchor[1] + (outline_px if anchor == "feet" else 0))
            if cell is None:
                window = (content[0] - 1, content[1] - 1, content[2] + 1, content[3] + 1)
                if not outline_px:   # the v1 rule: the crop never leaves the full grid
                    window = (max(0, window[0]), max(0, window[1]), min(grid.size[0], window[2]),
                              min(grid.size[1], window[3]))
            else:
                size_cell = (max(4, int(round(cell[0] * grid.factor))), max(4, int(round(cell[1] * grid.factor))))
                size_anchor = None if cell_anchor is None else (int(round(cell_anchor[0] * grid.factor)),
                                                                int(round(cell_anchor[1] * grid.factor)))
                window, _, factor_needed = canvas_window(content, pin, size_cell, size_anchor)
                if window is None:
                    retry = factor_needed
                    break
            for store in (passed.small, passed.cleaned) + ((passed.crisp,) if passed.crisp is not None else ()):
                store[grid.factor] = [cut_window(frame, window) for frame in store[grid.factor]]
            sizes.append({"factor": grid.factor, "label": _factor_label(grid.factor), "dir": _size_dir(grid.factor),
                          "scale": grid.scale, "size": [window[2] - window[0], window[3] - window[1]],
                          "anchor": [pin[0] - window[0], pin[1] - window[1]],
                          "targetHeight": plan.target_height * grid.factor + 2 * outline_px})
        if retry is None:
            break
        shrink = max(0.5, min(0.98, retry * 0.98))
        if not any(item["id"] == "canvas-fit" for item in checks):
            checks.append(_check("canvas-fit", {"targetHeight": round(plan.target_height + 2 * outline_px, 3)},
                                 list(cell), False, warn=True,
                                 note="the clip did not fit the canvas at the target; it was finished smaller"))
        plan = ScalePlan(plan.scale * shrink, plan.source, plan.target_height * shrink, plan.reference,
                         plan.reference_path, plan.checks)
    else:
        raise FinishError(f"the clip does not fit the {cell[0]}x{cell[1]} canvas; give a bigger canvas")
    for item in checks:
        if item["id"] == "canvas-fit":
            item["value"]["fittedHeight"] = round(plan.target_height + 2 * outline_px, 3)
    small, cleaned, hygiene, sources = passed.small, passed.cleaned, passed.hygiene, passed.sources

    # The colour lock (hd: the cleaned frames; pixel: the colours that get quantized).
    lock_doc = None
    if lock_master is not None:
        lock_doc = {"strength": float(lock_strength), "method": colour_lock_module.METHOD, "sizes": {}}
        for entry in sizes:
            factor = entry["factor"]
            store = cleaned if passed.crisp is None else passed.crisp
            before = colour_lock_module.measure(store[factor], lock_master, loop=loop_policy == "cycle") \
                if factor == 1.0 else None
            store[factor], stats = colour_lock_module.lock_frames(store[factor], lock_master,
                                                                  strength=float(lock_strength),
                                                                  loop=loop_policy == "cycle")
            item = {"stats": stats}
            if before is not None:
                after = colour_lock_module.measure(store[factor], lock_master, loop=loop_policy == "cycle")
                item.update(before={key: before[key] for key in ("hueFlipsPerPair", "regionSpreadMean",
                                                                 "regionSpreadMax", "foreignShare")},
                            after={key: after[key] for key in ("hueFlipsPerPair", "regionSpreadMean",
                                                               "regionSpreadMax", "foreignShare")})
            lock_doc["sizes"][entry["label"]] = item

    pal: fp.Palette | None = None
    palette_path: Path | None = None
    built = False
    shade_merged = 0
    quantized: dict[float, list[np.ndarray]] = {}
    pivot = None
    if mode == "pixel":
        colour_source = passed.crisp if passed.crisp is not None else cleaned
        rest_colours = colour_source[1.0][rest_frame]
        solid = rest_colours[..., 3] > CONTOUR
        pivot = float(fp.to_oklab(rest_colours[..., :3][solid])[:, 0].mean()) if solid.any() else 0.5
        for entry in sizes:
            quantized[entry["factor"]] = [lift_frame(frame, contrast, saturation, pivot)
                                          for frame in colour_source[entry["factor"]]]
        if palette is not None:
            pal, palette_path = _resolve_palette(palette)
        else:
            count = PIXEL_COLORS if colors is None else int(colors)
            try:
                pal = fp.build_palette(quantized[1.0], count, reserved=list(reserve) or None, seed=int(seed),
                                       alpha_threshold=128)
            except fp.PaletteError as error:
                raise FinishError(str(error)) from None
            pal = pal.replace(source=f"finish_frames pixel: OKLab k-means++ of {len(files)} finished frame(s), "
                                     f"{count} colours, seed {int(seed)}")
            built = True
            if bold and shade_gap:   # flat colour clusters: no near-identical shades (a given palette is kept as is)
                pal, shade_merged = space_palette(pal, quantized[1.0], shade_gap)
        if pal.transparent_index is None:
            raise FinishError("the pixel finish needs a palette with a transparent index (at most 255 colours)")

    count = len(files)
    outputs: dict[float, list[np.ndarray]] = {}
    size_qa: dict[float, dict[str, Any]] = {}
    shades = outline_shades(pal) if pal is not None and outline_px and outline == "selective" else None
    for entry in sizes:
        factor = entry["factor"]
        if mode == "hd":
            outputs[factor] = cleaned[factor]
            size_qa[factor] = {
                "flicker": flicker([frame[..., 3] for frame in cleaned[factor]], small[factor], transparent=None,
                                   loop=loop_policy == "cycle"),
                "specksRemoved": hygiene[factor]["detached_px"],
                "hygiene": dict(hygiene[factor], mode="both", floor=HYGIENE_FLOOR)}
        else:
            frames_in = quantized[factor]
            maps, stats, baseline = _quantize(frames_in, pal, loop_policy, float(margin),
                                              orphans=0 if bold else PIXEL_ORPHANS)
            lone = sum(item["orphans_fixed_px"] for item in stats)
            outlined_px = 0
            if bold:   # the flicker is counted before the outline, which only follows the silhouette
                maps = [clean_lone_pixels(frame, pal) for frame in maps]
                lone += sum(fixed for _, fixed in maps)
                maps = [frame for frame, _ in maps]
                baseline = [clean_lone_pixels(frame, pal)[0] for frame in baseline]
            held = flicker(maps, frames_in, transparent=pal.transparent_index, loop=loop_policy == "cycle")
            plain = flicker(baseline, frames_in, transparent=pal.transparent_index, loop=loop_policy == "cycle")
            if outline_px:
                outlined = [add_outline(frame, pal, outline, shades) for frame in maps]
                outlined_px = sum(ring for _, ring in outlined)
                maps = [frame for frame, _ in outlined]
            outputs[factor] = [fp.render_indices(frame, pal) for frame in maps]
            reduction = None
            if held is not None and plain is not None and plain["noiseFlipsPerPair"]:
                reduction = round(1.0 - held["noiseFlipsPerPair"] / plain["noiseFlipsPerPair"], 4)
            per_frame_colours = [int(np.unique(_pack(frame[..., :3][frame[..., 3] > 0])).size)
                                 for frame in outputs[factor]]
            size_qa[factor] = {"flicker": held, "perFrameNearest": plain, "noiseFlipReduction": reduction,
                               "specksRemoved": int(sum(item["despeckled_px"] for item in stats)),
                               "lonePixelsFixed": int(lone),
                               "heldIndexPx": int(sum(item["held_index_px"] for item in stats)),
                               "heldAlphaPx": int(sum(item["held_alpha_px"] for item in stats)),
                               "outlinePx": int(outlined_px), "coloursPerFrameMax": max(per_frame_colours),
                               "maps": maps}
        alpha_values = np.unique(np.concatenate([np.unique(frame[..., 3]) for frame in outputs[factor]]))
        size_qa[factor]["alphaBinary"] = bool(np.isin(alpha_values, (0, 255)).all())
        size_qa[factor]["emptyFrames"] = [index for index, frame in enumerate(outputs[factor])
                                          if not (frame[..., 3] > CONTOUR).any()]

    base = outputs[1.0]
    nominal = plan.target_height + 2 * outline_px
    measured = measure_body(base[rest_frame][..., 3], OUTPUT_MIN_RUN)
    rest_output = {"heightPx": round(nominal, 4), "areaPx": round(body.area * plan.scale ** 2, 3),
                   "bodyHeightPx": round(plan.target_height, 4), "outlinePx": outline_px,
                   "measuredHeightPx": None if measured is None else measured.height,
                   "measuredAreaPx": None if measured is None else measured.area}
    error = None if measured is None else abs(measured.height - nominal)
    checks.append(_check("no-upscale", round(max(entry["scale"] for entry in sizes), 6), 1.0, True))
    checks.append(_check("rest-height", None if error is None else round(error, 3), REST_HEIGHT_TOLERANCE,
                         error is not None and error <= REST_HEIGHT_TOLERANCE, warn=True,
                         note="|measured - nominal| rest-pose body height at 1x (outline included), output px"))
    qa1 = size_qa[1.0]
    checks.append(_check("empty-frames", qa1["emptyFrames"], 0, not qa1["emptyFrames"], warn=True))
    checks.append(_check("flicker", qa1["flicker"], None, None))
    if lock_doc is not None:
        checks.append(_check("colour-lock", lock_doc["sizes"].get("1x"), None, None))
    if mode == "hd":
        checks.append(_check("alpha-hygiene", qa1["hygiene"], None, None))
    else:
        reduction_factor = 1.0 / plan.scale
        checks.append(_check("pixel-reduction", round(reduction_factor, 3), PIXEL_MIN_REDUCTION,
                             reduction_factor >= PIXEL_MIN_REDUCTION, warn=True,
                             note="source px per output px; pixel art comes from about 8-12x"))
        checks.append(_check("alpha-binary", all(size_qa[f]["alphaBinary"] for f in size_qa), True,
                             all(size_qa[f]["alphaBinary"] for f in size_qa)))
        used = np.unique(np.concatenate([_pack(frame[..., :3][frame[..., 3] > 0]) for frame in base]))
        off = int((~np.isin(used, _pack(pal.rgb))).sum())
        checks.append(_check("palette-fit", {"colorsUsed": int(used.size), "paletteColors": len(pal),
                                             "offPaletteColors": off,
                                             "coloursPerFrameMax": qa1["coloursPerFrameMax"]}, 0,
                             off == 0 and used.size <= len(pal) and qa1["coloursPerFrameMax"] <= len(pal)))
        if qa1["perFrameNearest"] is not None:
            held, plain = qa1["flicker"]["noiseFlipsPerPair"], qa1["perFrameNearest"]["noiseFlipsPerPair"]
            checks.append(_check("hysteresis", {"noiseFlipsPerPair": held, "perFrameNearest": plain,
                                                "reduction": qa1["noiseFlipReduction"]}, "<= per-frame nearest",
                                 held <= plain, warn=True))

    clip_name = clip or final.name
    not_proven = ["identity, pose and readability at the finished size (look at review-contact.png and the frames)",
                  "that the target height suits the game camera and the rest of the cast (run lineup)",
                  "loop seams and timing (gait_loop, retime); finished frames keep the source names"]
    if mode == "pixel":
        not_proven += ["hysteresis follows file order: the seam pair of a loop window chosen later is not held",
                       "the palette's fit to other clips of the cast (build one shared palette with palette build)"]
    else:
        not_proven += ["engine blending: draw with premultiplied alpha (RGB under alpha 0 is zeroed)"]
    if lock_doc is not None:
        not_proven.append("that every locked colour belongs to the master's design part it matched (look at the "
                          "frames: the lock only moves chroma toward colours found at the same body height)")

    with forge_core.staged_output(final) as stage:
        written: list[Path] = []
        records: list[dict[str, Any]] = []
        for entry in sizes:
            factor = entry["factor"]
            (stage / entry["dir"]).mkdir()
            for index, (frame, path) in enumerate(zip(outputs[factor], files)):
                target = stage / entry["dir"] / path.name
                forge_core.save_png(frame, target)
                written.append(target)
                if factor == 1.0:
                    records.append({"file": f"{entry['dir']}/{path.name}", "sha256": forge_core.sha256_file(target),
                                    **sources[index], "opaquePx": int((frame[..., 3] > CONTOUR).sum())})
        shown = _contact_indices(count)
        forge_core.save_png(contact_sheet([base[index] for index in shown]), stage / CONTACT_FILE)
        written.append(stage / CONTACT_FILE)
        palette_doc = None
        if mode == "pixel":
            if palette_path is None:
                fp.write_palette(pal, stage / PALETTE_FILE, "json")
                written.append(stage / PALETTE_FILE)
                palette_ref = forge_core.file_ref(stage / PALETTE_FILE, stage)
            else:
                palette_ref = forge_core.file_ref(palette_path, stage)
            palette_doc = {**palette_ref, "colors": len(pal), "built": built,
                           "reserved": int(sum(pal.reserved)), "transparentIndex": pal.transparent_index}
        sheet_doc = None
        if mode == "pixel" and indexed_sheet:
            sheet, columns, rows = _indexed_sheet(qa1["maps"], pal.transparent_index)
            fp.save_indexed_png(sheet, pal, stage / SHEET_FILE, pal.transparent_index)
            written.append(stage / SHEET_FILE)
            sheet_doc = {**forge_core.file_ref(stage / SHEET_FILE, stage), "columns": columns, "rows": rows,
                         "frameSize": list(sizes[0]["size"]), "frames": count,
                         "transparentIndex": pal.transparent_index}
        inputs = [{"path": forge_core.manifest_path(path, stage), "sha256": source["sourceSha256"]}
                  for path, source in zip(files, sources)]
        if registration is not None:
            inputs.append(forge_core.file_ref(registration, stage))
        if plan.reference_path is not None:
            inputs.append(forge_core.file_ref(plan.reference_path, stage))
        if palette_path is not None:
            inputs.append(forge_core.file_ref(palette_path, stage))
        if lock_source is not None and lock_source.get("path") is not None:
            lock_ref = forge_core.file_ref(lock_source["path"], stage)
            inputs.append(lock_ref)
            lock_doc["master"] = lock_ref
        elif lock_doc is not None:
            lock_doc["master"] = {"source": lock_source.get("source")}
        output_refs = [forge_core.file_ref(path, stage) for path in written]
        failed = [item["id"] for item in checks if item["status"] == "fail"]
        warned = [item["id"] for item in checks if item["status"] == "warn"]
        if failed:
            raise FinishError(f"finish QA failed ({', '.join(failed)}); nothing was published")
        if strict and warned:
            raise FinishError(f"finish QA warned under --strict ({', '.join(warned)}); nothing was published")
        method = HD_METHOD if mode == "hd" else (BOLD_METHOD if bold else PIXEL_METHOD)
        if lock_doc is not None:
            method += "; then " + colour_lock_module.METHOD
        qa = _envelope(checks, method, not_proven, inputs, output_refs)
        display = []
        for entry in sizes:
            item = {key: entry[key] for key in ("label", "dir", "scale", "size", "anchor", "targetHeight")}
            item["flicker"] = size_qa[entry["factor"]]["flicker"]
            item["alphaBinary"] = size_qa[entry["factor"]]["alphaBinary"]
            display.append(item)
        document = {
            "schema": FINISH_SCHEMA,
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "mode": mode,
            "clip": clip_name,
            "character": character,
            "role": role,
            "source": {"frames": forge_core.manifest_path(folder, stage), "count": count,
                       "size": list(source_size), "pattern": pattern,
                       "registration": None if registration is None else forge_core.file_ref(registration, stage)},
            "rest": {"frame": rest_frame, "topPx": body.top, "feetPx": body.ground, "heightPx": body.height,
                     "areaPx": body.area, "stanceX": body.stance_x, "centroid": list(body.centroid),
                     "minRun": REST_MIN_RUN},
            "anchorMode": anchor,
            "anchorSource": list(anchor_src),
            "scale": plan.scale,
            "scaleSource": plan.source,
            "targetHeight": nominal,
            "scaleRef": None if plan.reference is None else {
                **({} if plan.reference_path is None else forge_core.file_ref(plan.reference_path, stage)),
                **plan.reference},
            "size": list(sizes[0]["size"]),
            "anchor": list(sizes[0]["anchor"]),
            "canvas": None if cell is None else {"size": list(cell), "anchor": list(sizes[0]["anchor"]),
                                                 "requestedAnchor": None if cell_anchor is None else list(cell_anchor)},
            "engine": {"sourceSize": list(sizes[0]["size"]), "sourceAnchor": list(sizes[0]["anchor"]),
                       "sampling": "nearest" if mode == "pixel" else "linear", "pixelArt": mode == "pixel",
                       "note": "engine_export.py package --clean-dir <out>/frames --source-size W,H --source-anchor "
                               "X,Y (no --registration; add --pixel-art --sampling nearest for pixel)"},
            "resampler": "box",
            "restOutput": rest_output,
            "displaySizes": display,
            "palette": palette_doc,
            "pixel": None if mode != "pixel" else {
                "alphaThreshold": PIXEL_ALPHA, "dither": False, "loopPolicy": loop_policy, "margin": float(margin),
                "alphaBand": list(fp.ALPHA_BAND), "orphans": PIXEL_CLEAN_PASSES if bold else PIXEL_ORPHANS,
                "despeckle": True, "downscale": downscale, "supersample": SUPERSAMPLE if crisp else 1,
                "lift": {"contrast": contrast, "saturation": saturation, "pivotL": None if pivot is None
                         else round(pivot, 5)},
                "outline": outline, "outlinePx": qa1["outlinePx"], "featureDeltaL": FEATURE_DELTA_L if bold else None,
                "shadeGap": shade_gap if (built and bold) else None, "shadeMerged": shade_merged,
                "coloursPerFrameMax": qa1["coloursPerFrameMax"],
                "noiseFlipReduction": qa1["noiseFlipReduction"], "perFrameNearest": qa1["perFrameNearest"],
                "heldIndexPx": qa1["heldIndexPx"], "heldAlphaPx": qa1["heldAlphaPx"],
                "lonePixelsFixed": qa1["lonePixelsFixed"]},
            "colourLock": lock_doc,
            "indexedSheet": sheet_doc,
            "loopPolicy": loop_policy,
            "frames": records,
            "flicker": qa1["flicker"],
            "specksRemoved": qa1["specksRemoved"],
            "alphaBinary": qa1["alphaBinary"],
            "review": {"path": CONTACT_FILE, "frames": shown},
            "qa": qa,
        }
        forge_core.write_json(stage / FINISH_FILE, document)
    for warning in warned:
        print(forge_core.ascii_text(f"warning: {warning}"), file=sys.stderr)
    flips = {"alpha": None, "index": None, "noise": None, "edge": None}
    if qa1["flicker"] is not None:
        flips = {"alpha": qa1["flicker"]["alphaFlipsPerPair"], "index": qa1["flicker"]["indexFlipsPerPair"],
                 "noise": qa1["flicker"]["noiseFlipsPerPair"], "edge": qa1["flicker"]["edgeFlipsPerPair"]}
    if mode == "pixel":
        plain = qa1["perFrameNearest"]
        flips["noisePerFrameNearest"] = None if plain is None else plain["noiseFlipsPerPair"]
        flips["edgePerFrameNearest"] = None if plain is None else plain["edgeFlipsPerPair"]
        flips["noiseReduction"] = qa1["noiseFlipReduction"]
    summary_qa = {"status": qa["status"], "flipsPerPair": flips, "specksRemoved": qa1["specksRemoved"],
                  "alphaBinary": qa1["alphaBinary"], "warnings": warned}
    if mode == "pixel":
        summary_qa["lonePixelsFixed"] = qa1["lonePixelsFixed"]
        summary_qa["coloursPerFrameMax"] = qa1["coloursPerFrameMax"]
        summary_qa["outlinePx"] = qa1["outlinePx"]
    if lock_doc is not None:
        summary_qa["colourLock"] = lock_doc["sizes"].get("1x")
    return {
        "status": qa["status"], "output": str(final), "metadata": str(final / FINISH_FILE), "mode": mode,
        "frames": count, "targetHeight": round(nominal, 4), "scale": round(plan.scale, 8),
        "scaleSource": plan.source, "size": list(sizes[0]["size"]), "anchor": list(sizes[0]["anchor"]),
        "displaySizes": [{"label": entry["label"], "dir": str(final / entry["dir"]), "size": entry["size"],
                          "anchor": entry["anchor"]} for entry in sizes],
        "paletteColors": None if pal is None else len(pal),
        "palette": None if pal is None else str(palette_path.resolve() if palette_path else final / PALETTE_FILE),
        "contact": str(final / CONTACT_FILE),
        "indexedSheet": None if sheet_doc is None else str(final / SHEET_FILE),
        "qa": summary_qa,
    }


# --------------------------------------------------------------------------- palette build

def _sample_set(files: Sequence[Path], frames_per_set: int, samples_per_set: int,
                rng: np.random.Generator) -> tuple[np.ndarray, int]:
    """Opaque colours (alpha >= 128) of up to ``frames_per_set`` evenly spaced frames, at most
    ``samples_per_set`` in total, drawn without replacement by ``rng``."""
    if len(files) > frames_per_set:
        picks = np.unique(np.floor(np.linspace(0, len(files) - 1, frames_per_set) + 0.5).astype(np.int64))
        files = [files[int(index)] for index in picks]
    budget = max(1, samples_per_set // len(files))
    parts = []
    for path in files:
        pixels = _load(path)[0]
        opaque = pixels[..., :3][pixels[..., 3] >= 128]
        if len(opaque) > budget:
            opaque = opaque[np.sort(rng.choice(len(opaque), budget, replace=False))]
        parts.append(opaque.reshape(-1, 3))
    return np.concatenate(parts) if parts else np.zeros((0, 3), np.uint8), len(files)


def build_cast_palette(frame_dirs: Sequence[str | os.PathLike], colors: int = DEFAULT_COLORS, *,
                       reserve: Sequence[str] = (), seed: int = 0, frames_per_set: int = PALETTE_FRAMES_PER_SET,
                       samples_per_set: int = PALETTE_SAMPLES_PER_SET, name: str | None = None
                       ) -> tuple[fp.Palette, dict[str, Any]]:
    """One OKLab k-means++ palette over every set (an action folder, a register_clip or finish output).

    Each set gives the same pixel budget (pixelate.py: about 22000 per asset), so long clips do not
    outweigh short ones. ``reserve`` colours keep indices 0..r-1; index 255 stays transparent, so
    ``colors`` is 2..255. The result depends only on the frames, their order and ``seed``.
    """
    if isinstance(colors, bool) or not 2 <= int(colors) <= 255:
        raise FinishError("--colors must be 2..255 (index 255 stays transparent)")
    if frames_per_set < 1 or samples_per_set < 1:
        raise FinishError("frames and samples per set must be at least 1")
    rng = np.random.default_rng(int(seed))
    parts, sets = [], []
    for raw in frame_dirs:
        folder = Path(raw)
        if not folder.is_dir():
            raise FinishError(f"frames folder not found: {folder}")
        if (folder / "frames").is_dir():
            folder = folder / "frames"
        files = _frame_files(folder)
        if not files:
            raise FinishError(f"no PNG frames in {folder}")
        samples, used = _sample_set(files, frames_per_set, samples_per_set, rng)
        parts.append(samples)
        sets.append({"folder": str(folder), "frames": len(files), "framesSampled": used, "samples": int(len(samples))})
    samples = np.concatenate(parts) if parts else np.zeros((0, 3), np.uint8)
    if not len(samples):
        raise FinishError("no opaque pixels (alpha >= 128) to learn a palette from")
    try:
        palette = fp.build_palette(samples, int(colors), reserved=list(reserve) or None, seed=int(seed))
    except fp.PaletteError as error:
        raise FinishError(str(error)) from None
    palette = palette.replace(name=name or palette.name,
                              source=f"finish_frames palette build: OKLab k-means++ over {len(sets)} set(s), "
                                     f"{int(colors)} colours, seed {int(seed)}")
    fit = fp.fit_report(samples[None, ...], palette)
    stats = {"sets": sets, "samples": int(len(samples)), "uniqueColors": int(np.unique(_pack(samples)).size),
             "fit": {"meanDeltaE": fit["mean_delta_e"], "p95DeltaE": fit["p95_delta_e"],
                     "maxDeltaE": fit["max_delta_e"]}}
    return palette, stats


def _stage_dir(final: Path) -> Path:
    final.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{final.name}.stage-", dir=final.parent))


def _publish_files(pairs: Sequence[tuple[Path, Path]]) -> None:
    """Publish staged files without replacing anything; roll back the ones published when a later one fails."""
    done: list[Path] = []
    try:
        for staged, final in pairs:
            forge_core.publish_file_no_replace(staged, final)
            done.append(final)
    except BaseException:
        for final in done:
            final.unlink(missing_ok=True)
        raise


def cmd_palette_build(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    out = Path(args.out)
    fmt = out.suffix.lower().lstrip(".")
    if fmt not in fp.PALETTE_FORMATS:
        raise FinishError(f"--out must end in .json, .gpl, .hex or .pal, got {out.name}")
    final = out.parent.resolve() / out.name
    if os.path.lexists(final):
        raise FileExistsError(errno.EEXIST, "Refusing to replace existing file", str(final))
    palette, stats = build_cast_palette(args.frames, args.colors, reserve=args.reserve, seed=args.seed,
                                        frames_per_set=args.frames_per_set, samples_per_set=args.samples_per_set,
                                        name=args.name)
    stage = _stage_dir(final)
    try:
        fp.write_palette(palette, stage / final.name, fmt)
        _publish_files([(stage / final.name, final)])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    summary = {
        "status": "pass", "output": str(final), "format": fmt, "colors": len(palette), "requested": args.colors,
        "reserved": int(sum(palette.reserved)), "transparentIndex": palette.transparent_index, "seed": args.seed,
        "sets": len(stats["sets"]), "framesSampled": sum(item["framesSampled"] for item in stats["sets"]),
        "samples": stats["samples"], "uniqueColors": stats["uniqueColors"], "fit": stats["fit"],
        "qa": {"method": "finish_frames palette build v1: per set up to 12 evenly spaced frames and 22000 opaque "
                         "pixels (alpha >= 128, seeded draw), forge_palette.build_palette weighted k-means++ in OKLab "
                         "with the reserved colours fixed first; fit = OKLab dE of the samples to the palette",
               "notProven": ["the fit to frames that were not sampled",
                             "the fit at the finished size when full-size frames were sampled (sample hd finishes at "
                             "the pixel target height for the closest fit)"]},
    }
    return summary, 0


# --------------------------------------------------------------------------- lineup

def parse_rules(text: str) -> list[dict[str, Any]]:
    """``hero:1.0,mob:<=1.0,boss:~2.0`` -> rules; a bare value means equal (within the exact tolerance)."""
    rules, seen = [], set()
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(<=|>=|~|=)?\s*([0-9]*\.?[0-9]+)", item)
        if not match:
            raise FinishError(f"bad rule {item!r}; use ROLE:VALUE, ROLE:<=VALUE, ROLE:>=VALUE or ROLE:~VALUE")
        role, value = match.group(1).lower(), float(match.group(3))
        if value <= 0 or role in seen:
            raise FinishError(f"rule {item!r} needs a positive value and a role named once")
        seen.add(role)
        rules.append({"role": role, "op": match.group(2) or "=", "value": value})
    if not rules:
        raise FinishError("--rules lists no rule")
    return rules


def _rule_text(rule: Mapping[str, Any]) -> str:
    return f"{'' if rule['op'] == '=' else rule['op']}{rule['value']:g}"


def rule_holds(rule: Mapping[str, Any], ratio: float, reference_px: float, *, exact: float = EXACT_TOLERANCE,
               about: float = ABOUT_TOLERANCE) -> bool:
    """``=``: within ``exact`` (and always within 1 px); ``~``: within ``about``; ``<=``/``>=``: whole px."""
    value, op = float(rule["value"]), rule["op"]
    pixel = 1.0 / max(reference_px, 1e-9)
    if op == "=":
        return abs(ratio - value) <= max(exact * value, pixel) + 1e-12
    if op == "~":
        return abs(ratio - value) <= about * value + 1e-12
    if op == "<=":
        return ratio <= value + 0.5 * pixel
    return ratio >= value - 0.5 * pixel


def _set_spec(text: str) -> tuple[str | None, Path]:
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*)=(.+)", str(text))
    if match and not Path(text).exists():
        return match.group(1).lower(), Path(match.group(2))
    return None, Path(text)


def _finished_clip(folder: Path) -> dict[str, Any]:
    document, path = load_finish(folder)
    try:
        rest = int(document["rest"]["frame"])
        frame_file = folder / document["frames"][rest]["file"]
        nominal_h = float(document["restOutput"]["heightPx"])
        nominal_a = float(document["restOutput"]["areaPx"])
        anchor = [float(value) for value in document["anchor"]]
    except (KeyError, IndexError, TypeError, ValueError):
        raise FinishError(f"{path} lacks rest, frames, restOutput or anchor; re-finish the clip") from None
    pixels, info = _load(frame_file)
    measured = measure_body(pixels[..., 3], OUTPUT_MIN_RUN)
    if measured is None:
        raise FinishError(f"{frame_file} (the rest frame) is empty")
    return {"name": str(document.get("clip") or folder.name), "role": document.get("role"),
            "character": document.get("character"), "finish": path, "frame": frame_file, "frameSha256": info["sha256"],
            "pixels": pixels, "anchor": anchor, "body": measured, "heightPx": nominal_h, "areaPx": nominal_a,
            "mode": document.get("mode")}


def _plain_clip(folder: Path) -> dict[str, Any]:
    files = _frame_files(folder / "frames" if (folder / "frames").is_dir() else folder)
    if not files:
        raise FinishError(f"{folder} has no finish.json, finished clips or PNG frames")
    pixels, info = _load(files[0])
    measured = measure_body(pixels[..., 3], OUTPUT_MIN_RUN)
    if measured is None:
        raise FinishError(f"{files[0]} (frame 0, the rest pose) is empty")
    return {"name": folder.name, "role": None, "character": None, "finish": None, "frame": files[0],
            "frameSha256": info["sha256"], "pixels": pixels, "anchor": [measured.stance_x, float(measured.ground)],
            "body": measured, "heightPx": float(measured.height), "areaPx": float(measured.area), "mode": None}


def load_set(role: str | None, folder: Path) -> dict[str, Any]:
    """A cast member: one finished clip, a folder of finished clips (one character; an idle clip
    represents it), or a plain frames folder whose frame 0 is the rest pose."""
    if not folder.is_dir():
        raise FinishError(f"set folder not found: {folder}")
    if (folder / FINISH_FILE).is_file():
        clips = [_finished_clip(folder)]
    elif folder.name.startswith("frames") and (folder.parent / FINISH_FILE).is_file():
        clips = [_finished_clip(folder.parent)]
    else:
        children = sorted((child for child in folder.iterdir() if child.is_dir() and (child / FINISH_FILE).is_file()),
                          key=_natural_key)
        clips = [_finished_clip(child) for child in children] or [_plain_clip(folder)]
    roles = {str(clip["role"]).lower() for clip in clips if clip["role"]}
    if role is None:
        if len(roles) > 1:
            raise FinishError(f"{folder}: its clips disagree on the role ({', '.join(sorted(roles))}); "
                              f"pass ROLE={folder}")
        role = roles.pop() if roles else None
    represent = next((clip for clip in clips if "idle" in clip["name"].lower()), clips[0])
    heights = [clip["heightPx"] for clip in clips]
    return {"name": folder.parent.name if folder.name == "frames" else folder.name,
            "role": role, "folder": folder, "clips": clips, "represent": represent,
            "heightPx": represent["heightPx"], "areaPx": represent["areaPx"],
            "heightSpread": round(max(heights) - min(heights), 4)}


def render_lineup(sets: Sequence[Mapping[str, Any]], reference_height: float) -> np.ndarray:
    """Every set's rest frame on one ground line at x1 (with labels) and x3 (nearest review zoom),
    with the ground line and the reference body height (dotted) drawn across each strip."""
    font = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    gap, margin, line_h = 8, 8, 12
    items = []
    for entry in sets:
        clip = entry["represent"]
        pixels, anchor_x = clip["pixels"], clip["anchor"][0]
        size = f"h {entry['heightPx']:.1f}" if entry["metric"] == "height" else f"a {entry['areaPx']:.0f}"
        lines = [entry["name"], f"{entry['role'] or '-'} {entry['ratio']:.2f}x", size]
        text_w = max(int(math.ceil(probe.textlength(line, font=font))) for line in lines)
        reach = int(math.ceil(max(anchor_x, pixels.shape[1] - anchor_x)))
        items.append({"pixels": pixels, "ground": clip["body"].ground, "anchor_x": anchor_x, "lines": lines,
                      "slot": max(2 * reach, text_w) + gap})
    slots = sum(item["slot"] for item in items)
    above = max(max(item["ground"] for item in items), int(math.ceil(reference_height)) + 2)
    below = max(item["pixels"].shape[0] - item["ground"] for item in items)
    label_top = margin + above + below + 4
    strip1_h = label_top + 3 * line_h + margin
    strip3_h = margin + 3 * (above + below) + margin
    canvas = np.empty((strip1_h + strip3_h, 2 * margin + 3 * slots, 3), np.float32)
    canvas[...] = BACKGROUND
    for zoom, top in ((1, 0), (3, strip1_h)):
        ground_y = top + margin + above * zoom
        canvas[ground_y, margin:margin + slots * zoom] = GROUND_COLOUR
        canvas[ground_y - int(round(reference_height * zoom)), margin:margin + slots * zoom:4] = REFERENCE_COLOUR
        x = margin
        for item in items:
            centre = x + item["slot"] * zoom / 2
            _composite(canvas, _zoom(item["pixels"], zoom), int(round(centre - item["anchor_x"] * zoom)),
                       ground_y - item["ground"] * zoom)
            x += item["slot"] * zoom
    picture = Image.fromarray(_rgb8(canvas))
    draw = ImageDraw.Draw(picture)
    x = margin
    for item in items:
        for row, line in enumerate(item["lines"]):
            width = probe.textlength(line, font=font)
            draw.text((x + (item["slot"] - width) / 2, label_top + row * line_h), line, fill=TEXT_COLOUR, font=font)
        x += item["slot"]
    return np.asarray(picture)


def lineup(set_specs: Sequence[str | tuple[str | None, str | os.PathLike]], out: str | os.PathLike, *,
           rules: str = DEFAULT_RULES, reference_role: str = "hero", area_roles: Sequence[str] = DEFAULT_AREA_ROLES,
           exact: float = EXACT_TOLERANCE, about: float = ABOUT_TOLERANCE,
           report: str | os.PathLike | None = None) -> dict[str, Any]:
    """Check the cast size rules and write the line-up PNG (x1 and x3) and its JSON report (new files).

    ``set_specs`` items are ``[ROLE=]DIR`` strings or ``(role, dir)`` pairs. The reference is the first
    set whose role is ``reference_role``. Height rules compare rest-pose body heights; ``area_roles``
    (spirits) compare sqrt(opaque area). Returns the summary; its status is ``fail`` when a rule fails.
    """
    out_path = Path(out)
    if out_path.suffix.lower() != ".png":
        raise FinishError(f"--out must be a .png file, got {out_path.name}")
    final_png = out_path.parent.resolve() / out_path.name
    final_json = (Path(report).parent.resolve() / Path(report).name) if report else final_png.with_suffix(".json")
    for final in (final_png, final_json):
        if os.path.lexists(final):
            raise FileExistsError(errno.EEXIST, "Refusing to replace existing file", str(final))
    parsed = parse_rules(rules)
    by_role = {rule["role"]: rule for rule in parsed}
    area_set = {str(role).lower() for role in area_roles}
    if not 0 <= exact < 1 or not 0 <= about < 1:
        raise FinishError("tolerances must be 0 <= t < 1")
    sets = []
    for spec in set_specs:
        role, folder = (spec[0], Path(spec[1])) if isinstance(spec, tuple) else _set_spec(spec)
        sets.append(load_set(None if role is None else str(role).lower(), folder))
    if not sets:
        raise FinishError("lineup needs at least one set")
    names: dict[str, int] = {}
    for entry in sets:
        base = entry["name"]
        names[base] = names.get(base, 0) + 1
        if names[base] > 1:
            entry["name"] = f"{base}-{names[base]}"
    reference = next((entry for entry in sets if entry["role"] == reference_role.lower()), None)
    if reference is None:
        raise FinishError(f"lineup needs a set with role {reference_role} (the reference for the size rules); "
                          f"tag one with {reference_role}=DIR or finish it with --role {reference_role}")
    ref_h, ref_a = reference["heightPx"], reference["areaPx"]
    base = final_json.parent
    checks, rows, inputs = [], [], []
    for entry in sets:
        metric = "area" if entry["role"] in area_set else "height"
        ratio = entry["heightPx"] / ref_h if metric == "height" else math.sqrt(entry["areaPx"] / ref_a)
        entry["metric"], entry["ratio"] = metric, ratio
        rule = by_role.get(entry["role"]) if entry["role"] else None
        extra = {"metric": metric} if rule is not None else {"metric": metric, "note": "no rule for this role"}
        rule_check = _check(f"rule:{entry['name']}", round(ratio, 4),
                            None if rule is None else f"{entry['role']}:{_rule_text(rule)}",
                            None if rule is None else rule_holds(rule, ratio, ref_h if metric == "height"
                                                                 else math.sqrt(ref_a), exact=exact, about=about),
                            **extra)
        checks.append(rule_check)
        if len(entry["clips"]) > 1:
            spread = entry["heightSpread"]
            checks.append(_check(f"one-height:{entry['name']}", spread, SET_HEIGHT_TOLERANCE,
                                 spread <= SET_HEIGHT_TOLERANCE, warn=True,
                                 note="rest-pose body heights of one character's clips, output px"))
        clip_rows = []
        for clip in entry["clips"]:
            if clip["finish"] is not None:
                inputs.append(forge_core.file_ref(clip["finish"], base))
            inputs.append({"path": forge_core.manifest_path(clip["frame"], base), "sha256": clip["frameSha256"]})
            clip_rows.append({"name": clip["name"], "mode": clip["mode"], "heightPx": clip["heightPx"],
                              "areaPx": clip["areaPx"], "measuredHeightPx": clip["body"].height,
                              "measuredAreaPx": clip["body"].area,
                              "restFrame": forge_core.manifest_path(clip["frame"], base)})
        rows.append({"name": entry["name"], "role": entry["role"], "folder": forge_core.manifest_path(entry["folder"],
                                                                                                    base),
                     "metric": metric, "ratio": round(ratio, 6), "rule": None if rule is None else _rule_text(rule),
                     "status": rule_check["status"], "heightPx": entry["heightPx"], "areaPx": entry["areaPx"],
                     "heightSpread": entry["heightSpread"], "clips": clip_rows})
    picture = render_lineup(sets, ref_h)
    stage = _stage_dir(final_png)
    try:
        forge_core.save_png(picture, stage / final_png.name)
        png_ref = {"path": forge_core.manifest_path(final_png, base),
                   "sha256": forge_core.sha256_file(stage / final_png.name),
                   "bytes": (stage / final_png.name).stat().st_size}
        qa = _envelope(checks, "finish_frames lineup v1: rest-pose body heights from finish.json (rest height x scale; "
                               "frame 0 measured at the 50% contour for plain folders), area roles by sqrt(opaque "
                               "area); ratios against the first reference-role set; '=' within the exact tolerance "
                               "or 1 px, '~' within the about tolerance, '<=' and '>=' to the half pixel; picture at "
                               "x1 and x3 (nearest review zoom) on one ground line",
                       ["art and silhouette readability side by side (look at the picture)",
                        "sizes in other poses than the rest pose"], inputs, [png_ref], passed="pass")
        document = {"schema": LINEUP_SCHEMA, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
                    "image": png_ref["path"], "scales": [1, 3],
                    "reference": {"set": reference["name"], "role": reference["role"], "heightPx": ref_h,
                                  "areaPx": ref_a},
                    "rules": [dict(rule, metric="area" if rule["role"] in area_set else "height") for rule in parsed],
                    "tolerances": {"exact": exact, "about": about}, "sets": rows, "qa": qa}
        forge_core.write_json(stage / final_json.name, document)
        _publish_files([(stage / final_png.name, final_png), (stage / final_json.name, final_json)])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"status": qa["status"], "output": str(final_png), "metadata": str(final_json), "sets": len(sets),
            "reference": reference["name"],
            "failed": [{"set": row["name"], "role": row["role"], "metric": row["metric"],
                        "ratio": round(row["ratio"], 4), "rule": row["rule"]} for row in rows if row["status"] == "fail"],
            "warnings": [item["id"] for item in checks if item["status"] == "warn"],
            "ratios": {row["name"]: round(row["ratio"], 4) for row in rows}}


# --------------------------------------------------------------------------- CLI

def _positive(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"needs a positive number; got {text!r}")
    return value


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"needs an integer >= 0; got {text!r}")
    return value


def _colour(text: str) -> str:
    try:
        return fp.hex_color(fp.parse_color(text))
    except fp.PaletteError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _common_finish_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--frames", required=True,
                        help="registered RGBA frames: a folder, or a register_clip output folder (its frames/)")
    parser.add_argument("--output-dir", required=True, help="new folder for the finished set (must not exist)")
    scale = parser.add_mutually_exclusive_group(required=True)
    scale.add_argument("--target-height", type=_positive,
                       help="output px of the rest-pose body at 1x (pixel art: about 1/8 to 1/12 of the source)")
    scale.add_argument("--scale-ref",
                       help="finish.json (or its folder) of a sibling clip of the same character: its scale x "
                            "sqrt(rest-area ratio), clamped to +-3%%")
    parser.add_argument("--no-area-norm", action="store_true",
                        help="take the --scale-ref scale as is (a clip whose rest frame is not the base still)")
    parser.add_argument("--rest-frame", type=_non_negative_int, default=0,
                        help="0-based frame holding the rest pose (default 0, the master still)")
    parser.add_argument("--anchor", choices=ANCHORS, default="feet",
                        help="grid pin: feet = stance midpoint on the feet line (default), center = alpha centroid "
                             "(floating subjects)")
    parser.add_argument("--display-sizes", default="1x",
                        help="comma list such as 1x,2x; each size is re-derived from the source frames into "
                             "frames-<k>x/ (1x is frames/); never an upscale")
    parser.add_argument("--loop-policy", choices=LOOP_POLICIES, default="cycle",
                        help="cycle (default): frame 0 follows the last frame; pingpong; oneshot (flicker QA and "
                             "pixel hysteresis seeding)")
    parser.add_argument("--pattern", default="*.png", help="frame file pattern (default *.png)")
    parser.add_argument("--clip", help="clip name for the manifest (default: the output folder name)")
    parser.add_argument("--character", help="character id; --scale-ref refuses another character's clip")
    parser.add_argument("--role", help="cast role for lineup, such as hero, mob, boss or spirit")
    parser.add_argument("--strict", action="store_true", help="treat QA warnings as failures (nothing published)")
    parser.add_argument("--canvas", help="fixed cell WIDTHxHEIGHT (1x output px) for every frame, such as 48x64; a clip "
                                         "too big for it is finished smaller (canvas-fit warning)")
    parser.add_argument("--canvas-anchor", help="X,Y of the anchor inside the --canvas cell (default: the centre "
                                                "column, 1 px above the bottom edge, moved the least to fit)")
    parser.add_argument("--colour-lock", metavar="MASTER",
                        help="keyed master still (RGBA PNG), or 'rest' for the rest frame: lock the design colours "
                             "to it in OKLab (chroma only, lightness kept; colour_lock.py)")
    parser.add_argument("--lock-strength", type=float, default=1.0, help="colour-lock strength 0..1 (default 1)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finish_frames.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    hd = sub.add_parser("hd", help="the default finish: premultiplied box downscale, clean alpha",
                        description="HD finish: rest-pose scale, premultiplied box (area) downscale on a grid pinned "
                                    "to the anchor, alpha hygiene. Never nearest, never an upscale.")
    _common_finish_args(hd)
    hd.set_defaults(func=cmd_finish, mode="hd")

    pixel = sub.add_parser("pixel", help="pixel-art finish: shared OKLab palette, hysteresis, binary alpha",
                           description="Pixel finish (game-opus55 pixelate.py): the hd registration and box "
                                       "downscale, one OKLab palette, no dither, temporal hysteresis, lone-pixel and "
                                       "speck cleanup, binary alpha at 0.5.")
    _common_finish_args(pixel)
    pixel.add_argument("--palette", help="palette file (.json, .gpl, .hex, .pal); shared by every clip of the cast")
    pixel.add_argument("--colors", type=int,
                       help="without --palette: learn this many colours from the clip (2..255, default "
                            f"{PIXEL_COLORS}); every frame then uses at most that many")
    pixel.add_argument("--reserve", nargs="+", action="extend", default=[], type=_colour, metavar="#RRGGBB",
                       help="without --palette: fixed colours kept first in the learned palette")
    pixel.add_argument("--seed", type=int, default=0, help="k-means++ seed of a learned palette (default 0)")
    pixel.add_argument("--margin", type=float, default=fp.HYSTERESIS_MARGIN,
                       help="hysteresis margin, squared OKLab distance (default 0.0004 = dE 0.02)")
    pixel.add_argument("--indexed-sheet", action="store_true",
                       help="also write sheet-indexed.png (index 255 transparent) of the 1x frames")
    pixel.add_argument("--downscale", choices=DOWNSCALES, default="crisp",
                       help="crisp (default): each output pixel takes the dominant colour cluster of its 4x4 "
                            "sub-pixels (no blurred in-between colours; lines and eyes survive); box: the plain "
                            "area average")
    pixel.add_argument("--contrast", type=float, default=LIFT_CONTRAST,
                       help=f"OKLab lightness spread before quantizing (default {LIFT_CONTRAST:g}; 1 = off)")
    pixel.add_argument("--saturation", type=float, default=LIFT_SATURATION,
                       help=f"OKLab chroma scale before quantizing (default {LIFT_SATURATION:g}; 1 = off)")
    pixel.add_argument("--outline", choices=OUTLINES, default="selective",
                       help="1 px outline outside the silhouette: selective (default; the palette's dark shade of "
                            "the darkest neighbour), dark (the darkest palette colour) or none; the target height "
                            "includes it")
    pixel.add_argument("--shade-gap", type=float, default=SHADE_GAP,
                       help=f"a palette learned from the clip keeps no two colours closer than this OKLab dE: the "
                            f"closest pair merges first, so each material keeps a few distinct tones instead of "
                            f"near-identical shades that read as speckle (default {SHADE_GAP:g}; 0 = off; a "
                            f"--palette is used as given)")
    pixel.set_defaults(func=cmd_finish, mode="pixel")

    palette = sub.add_parser("palette", help="palettes for the pixel finish (palette build)",
                             description="Palettes for the pixel finish.")
    palette_sub = palette.add_subparsers(dest="palette_command", required=True, metavar="COMMAND")
    build = palette_sub.add_parser("build", help="one OKLab k-means++ palette across a character or cast",
                                   description="Learn one OKLab k-means++ palette across every action of a "
                                               "character or cast, reserved colours first; deterministic per seed.")
    build.add_argument("--frames", nargs="+", action="extend", required=True, metavar="DIR",
                       help="frame folders (actions); register_clip and finish output folders use their frames/")
    build.add_argument("--colors", type=int, default=DEFAULT_COLORS,
                       help="palette size including reserved colours, 2..255 (default 255; index 255 is transparent)")
    build.add_argument("--reserve", nargs="+", action="extend", default=[], type=_colour, metavar="#RRGGBB",
                       help="fixed colours kept at indices 0.. (engine, UI or outline colours)")
    build.add_argument("--seed", type=int, default=0, help="k-means++ and sampling seed (default 0)")
    build.add_argument("--frames-per-set", type=int, default=PALETTE_FRAMES_PER_SET,
                       help=f"frames sampled per folder, evenly spaced (default {PALETTE_FRAMES_PER_SET})")
    build.add_argument("--samples-per-set", type=int, default=PALETTE_SAMPLES_PER_SET,
                       help=f"opaque pixels sampled per folder (default {PALETTE_SAMPLES_PER_SET})")
    build.add_argument("--name", help="palette name")
    build.add_argument("--out", required=True, help="new palette file: .json (palette.v1), .gpl, .hex or .pal")
    build.set_defaults(func=cmd_palette_build)

    line = sub.add_parser("lineup", help="cast line-up at x1 and x3 with the size rules checked",
                          description="Cast line-up at x1 and x3 on one ground line; checks the size rules (body "
                                      "height from the rest pose; area roles by opaque area) and writes a JSON "
                                      "report beside the PNG. A failed rule still writes both, then exits 1.")
    line.add_argument("--sets", nargs="+", action="extend", required=True, metavar="[ROLE=]DIR",
                      help="finished clips, folders of one character's finished clips, or frame folders; ROLE= "
                           "overrides the role recorded by finish --role")
    line.add_argument("--out", required=True, help="new .png file; the report is the same name with .json")
    line.add_argument("--report", help="report path instead of <out>.json (must not exist)")
    line.add_argument("--rules", default=DEFAULT_RULES,
                      help=f"ROLE:VALUE (equal), ROLE:<=VALUE, ROLE:>=VALUE or ROLE:~VALUE (about), as ratios to "
                           f"the reference (default {DEFAULT_RULES})")
    line.add_argument("--reference-role", default="hero", help="role of the reference set (default hero)")
    line.add_argument("--area-roles", default=",".join(DEFAULT_AREA_ROLES),
                      help="roles compared by sqrt(opaque area) instead of height (default spirit)")
    line.add_argument("--exact-tolerance", type=float, default=EXACT_TOLERANCE,
                      help="relative tolerance of '=' rules (default 0.02; 1 px always passes)")
    line.add_argument("--about-tolerance", type=float, default=ABOUT_TOLERANCE,
                      help="relative tolerance of '~' rules (default 0.10)")
    line.set_defaults(func=cmd_lineup)
    return parser


def cmd_finish(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    pixel = args.mode == "pixel"
    summary = finish_clip(
        args.frames, args.output_dir, mode=args.mode, target_height=args.target_height, scale_ref=args.scale_ref,
        palette=getattr(args, "palette", None) if pixel else None, rest_frame=args.rest_frame, anchor=args.anchor,
        display_sizes=args.display_sizes, colors=getattr(args, "colors", None) if pixel else None,
        reserve=getattr(args, "reserve", []) if pixel else (), seed=getattr(args, "seed", 0),
        loop_policy=args.loop_policy, margin=getattr(args, "margin", fp.HYSTERESIS_MARGIN),
        area_norm=not args.no_area_norm, indexed_sheet=getattr(args, "indexed_sheet", False), pattern=args.pattern,
        clip=args.clip, character=args.character, role=args.role, strict=args.strict,
        downscale=args.downscale if pixel else None, contrast=args.contrast if pixel else None,
        saturation=args.saturation if pixel else None, outline=args.outline if pixel else None,
        canvas=args.canvas, canvas_anchor=args.canvas_anchor, colour_lock=args.colour_lock,
        lock_strength=args.lock_strength, shade_gap=args.shade_gap if pixel else None)
    return summary, 0


def cmd_lineup(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    roles = [item.strip() for item in args.area_roles.split(",") if item.strip()]
    summary = lineup(args.sets, args.out, rules=args.rules, reference_role=args.reference_role, area_roles=roles,
                     exact=args.exact_tolerance, about=args.about_tolerance, report=args.report)
    for warning in summary["warnings"]:
        print(forge_core.ascii_text(f"warning: {warning}"), file=sys.stderr)
    if summary["status"] == "fail":
        for item in summary["failed"]:
            print(forge_core.ascii_text(f"error: lineup rule failed: {item['set']} ({item['role']}) is "
                                        f"{item['ratio']:g}x the reference by {item['metric']}, rule "
                                        f"{item['role']}:{item['rule']}; see {summary['metadata']}"), file=sys.stderr)
        return summary, 1
    return summary, 0


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary, status = args.func(args)
    except FileExistsError as error:
        if not getattr(error, "filename", None):
            raise
        raise FinishError(f"output already exists, choose a new path: {error.filename}") from None
    print(json.dumps(summary, ensure_ascii=True))
    return status


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors exit 2; refused input, failed QA and failed lineup rules exit 1 with ``error: ...``;
    anything unexpected prints ``error: internal error (...)`` (D26, D27; forge_core.run_cli)."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
