#!/usr/bin/env python3
"""Scale and register the frames of one action on one canvas (generate2dsprite.scale_frames.v1).

One scale for the whole set, one shared root (anchor_px) and only whole-pixel
shifts per frame: poses keep their bob, flight and stride while the body stops
swimming between frames. The canvas grows to hold every frame and nothing is
ever clamped: a frame that would leave a fixed canvas is an error (exit 1,
nothing published).

  scale   --scale-from neutral|union with --target-body-px, or a number such as
          1/8. --resampler nearest needs an integer factor (pixel art); box and
          lanczos resample premultiplied colour (HD art).
  root    --anchor stance|feet|bbox, measured on the --neutral frame; y is the
          bottom edge of its lowest row (the ground line).
  locks   --root-lock torso-x|stance (horizontal), --row-baseline (one y shift
          per sheet row, keeps bob and flight), --lock feet|x|hip (per-frame
          pins for grounded actions). Shifts are whole output pixels;
          --shift-quantum 8 keeps whole game pixels while working at 8x size.
  canvas  automatic (every frame fits plus --margin), or --canvas W,H with
          --anchor-px X,Y (or --profile from an earlier run), grown by
          --action-padding L,T,R,B.

Writes frames/<name>.png, scale-frames.json (the scale, canvas, root and every
shift) and, with --emit-clips, clips.json for build_animation_clips.py: a
generate2dsprite.animation_clips.v2 manifest timed in --ticks at --tick-hz (D11;
6 ticks at 60 Hz by default, or exact --duration-ms), with pixel_art and nearest
sampling for --resampler nearest.

Usage errors exit 2 (argparse); every other error prints one "error: ..." line,
publishes nothing and exits 1.

Run from the project root, for example:
  python "<skill-dir>/scripts/scale_frames.py" --sheet raw/run-sheet.png --rows 2 --cols 4 --scale-from 1/8 --resampler nearest --root-lock torso-x --row-baseline --emit-clips --ticks 5 --output-dir out/run-game
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)
import sheet_qc  # noqa: E402  (sibling: input loading, ownership slicing, torso measure)

TOOL = {"name": "scale_frames.py", "version": forge_core.FORGE_PACKAGE_VERSION}  # the package version (D29)
SCHEMA = "generate2dsprite.scale_frames.v1"
CLIPS_SCHEMA = "generate2dsprite.animation_clips.v2"  # D11: ticks on the tick grid
DEFAULT_TICKS = 6  # 100 ms at 60 Hz, the old default duration
RECORD_NAME = "scale-frames.json"
ANCHORS = ("stance", "feet", "bbox")
ROOT_LOCKS = ("torso-x", "stance", "none")
LOCKS = ("feet", "x", "hip")
SCALE_SOURCES = ("neutral", "union")


class ScaleError(ValueError):
    """A user-facing problem, printed as 'error: ...'."""


# --------------------------------------------------------------------------- parsing

def parse_scale(text: str) -> str | float:
    """'neutral', 'union', or a positive number such as 0.125 or 1/8."""
    value = text.strip().lower()
    if value in SCALE_SOURCES:
        return value
    try:
        number = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        raise argparse.ArgumentTypeError(f"use neutral, union or a number such as 0.125 or 1/8; got {text}") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"the scale must be positive; got {text}")
    return number


def parse_ints(text: str, count: int, name: str, *, minimum: int = 0) -> list[int]:
    """'48,64' -> [48, 64]; a single value is repeated when ``count`` is 4 (padding)."""
    try:
        values = [int(part) for part in text.split(",")]
    except ValueError:
        raise ScaleError(f"{name} takes {count} comma-separated integers; got {text!r}.") from None
    if count == 4 and len(values) == 1:
        values = values * 4
    if len(values) != count or any(value < minimum for value in values):
        raise ScaleError(f"{name} takes {count} comma-separated integers >= {minimum}; got {text!r}.")
    return values


def parse_point(text: str, name: str) -> list[float]:
    try:
        values = [float(part) for part in text.split(",")]
    except ValueError:
        raise ScaleError(f"{name} takes x,y; got {text!r}.") from None
    if len(values) != 2 or not all(math.isfinite(value) for value in values):
        raise ScaleError(f"{name} takes two finite numbers x,y; got {text!r}.")
    return values


def parse_locks(values: Sequence[str] | None) -> list[str]:
    locks: list[str] = []
    for value in values or ():
        for part in value.split(","):
            part = part.strip()
            if part not in LOCKS:
                raise ScaleError(f"--lock takes {', '.join(LOCKS)}; got {part!r}.")
            if part not in locks:
                locks.append(part)
    return locks


# --------------------------------------------------------------------------- measurement

def hip_x(mask: np.ndarray, ground: int, height: int) -> float | None:
    """Body root x from the hip band (ground - 0.55 H .. ground - 0.25 H of the neutral frame): per row,
    the centre of the widest run, then the median (thin blades, poles and scarf tips are never the
    widest run)."""
    first, last = max(0, int(ground - 0.55 * height)), max(0, int(ground - 0.25 * height))
    centres = []
    for row in mask[first:last]:
        padded = np.concatenate(([False], row, [False])).astype(np.int8)
        edges = np.flatnonzero(np.diff(padded))
        if edges.size == 0:
            continue
        starts, ends = edges[0::2], edges[1::2]
        widest = int(np.argmax(ends - starts))
        centres.append((starts[widest] + ends[widest]) / 2.0)
    return float(np.median(centres)) if centres else None


def measure(frame: np.ndarray, *, threshold: int, torso_colors: Sequence[Sequence[int]], torso_band: tuple,
            tolerance: float) -> dict:
    """Source-space geometry of one frame: extent, ground, body height, stance/feet/bbox points and
    the horizontal features the locks can pin."""
    mask = forge_core.subject_mask(frame[..., 3], threshold)
    box = forge_core.subject_bbox(frame[..., 3], threshold)
    body_box = forge_core.subject_bbox(frame[..., 3], forge_core.BODY_ALPHA_THRESHOLD)
    if box is None or body_box is None:
        raise ScaleError("A frame has no subject pixels; check the key and --alpha-threshold.")
    ground = forge_core.ground_row(mask)
    height = ground - box[1]
    band = max(2, forge_core.round_half_up(0.04 * height))
    stance = forge_core.anchor_from_mask(mask, "stance", band_rows=band)
    feet = forge_core.anchor_from_mask(mask, "feet", band_rows=band)
    bbox_point = forge_core.anchor_from_mask(mask, "bbox")
    torso, method = sheet_qc.torso_x(frame, colors=torso_colors, tolerance=tolerance, band=torso_band)
    xs = np.nonzero(mask)[1]
    return {
        "bbox": [int(value) for value in box], "ground": int(ground), "height": int(height),
        "body_height": int(body_box[3] - body_box[1]), "body_top": int(body_box[1]), "body_ground": int(body_box[3]),
        "support_band_rows": band,
        "points": {"stance": [stance[0], stance[1]], "feet": [feet[0], feet[1]], "bbox": [bbox_point[0], bbox_point[1]]},
        "torso_x": torso, "torso_method": method, "centroid_x": float(xs.mean()) + 0.5,
        "visible_px": int((frame[..., 3] > 0).sum()),
    }


# --------------------------------------------------------------------------- planning

def resolve_scale(scale_from: str | float, target_body_px: float | None, measures: Sequence[dict],
                  reference: int) -> float:
    if isinstance(scale_from, float):
        if target_body_px is not None:
            raise ScaleError("--target-body-px applies to --scale-from neutral or union, not a fixed number.")
        return scale_from
    if target_body_px is None or not target_body_px > 0:
        raise ScaleError(f"--scale-from {scale_from} needs --target-body-px (body height in output pixels).")
    if scale_from == "neutral":
        height = measures[reference]["body_height"]
    else:
        height = max(item["body_ground"] for item in measures) - min(item["body_top"] for item in measures)
    return float(target_body_px) / height


def check_resampler(scale: float, resampler: str, target_body_px: float | None, height: int) -> None:
    """Nearest keeps pixel art only at integer factors (S06); suggest the nearest ones otherwise."""
    if resampler != "nearest":
        return
    try:
        forge_core.integer_scale(scale if scale >= 1 else 1.0 / scale, tol=1e-6)
    except ValueError:
        if scale < 1:
            low, high = math.floor(1.0 / scale), math.ceil(1.0 / scale)
            options = ", ".join(f"1/{factor} ({height / factor:.1f} px)" for factor in sorted({low, high}) if factor)
        else:
            low, high = math.floor(scale), math.ceil(scale)
            options = ", ".join(f"{factor} ({height * factor:.1f} px)" for factor in sorted({low, high}) if factor)
        hint = f" for a {target_body_px} px body" if target_body_px else ""
        raise ScaleError(f"--resampler nearest needs an integer factor N or 1/N, but the scale is {scale:.6g}{hint}; "
                         f"use --scale-from with one of: {options}, or --resampler box/lanczos for HD art.") from None


def plan_shifts(measures: Sequence[dict], *, reference: int, scale: float, root_lock: str, locks: Sequence[str],
                row_baseline: bool, row_size: int, quantum: int) -> tuple[list[list[int]], dict]:
    """Whole-pixel output shifts [dx, dy] per frame, against the reference frame.

    Horizontal: --root-lock torso-x|stance, or --lock x (centroid) | hip.
    Vertical: --row-baseline (each sheet row's ground to the reference row's)
    or --lock feet (each frame's ground to the reference ground).
    """
    horizontal = [mode for mode in (root_lock if root_lock != "none" else None,
                                    "x" if "x" in locks else None, "hip" if "hip" in locks else None) if mode]
    if len(horizontal) > 1:
        raise ScaleError(f"Choose one horizontal lock; got {', '.join(horizontal)}.")
    if row_baseline and "feet" in locks:
        raise ScaleError("--row-baseline and --lock feet both set y; choose one.")
    feature = {"torso-x": "torso_x", "stance": "stance", "x": "centroid_x", "hip": "hip_x"}
    horizontal_mode = horizontal[0] if horizontal else None
    values: list[float | None] = []
    if horizontal_mode:
        key = feature[horizontal_mode]
        values = [item["points"]["stance"][0] if key == "stance" else item[key] for item in measures]
        missing = [index for index, value in enumerate(values) if value is None]
        if missing:
            raise ScaleError(f"No {horizontal_mode} measure in frame(s) {missing}; "
                             "check --torso-colors/--torso-band or choose another lock.")
    rows = [index // row_size for index in range(len(measures))]
    row_ground: dict[int, int] = {}
    for index, item in enumerate(measures):
        row_ground[rows[index]] = max(row_ground.get(rows[index], 0), item["ground"])

    def quantise(value: float) -> int:
        return quantum * forge_core.round_half_up(value / quantum)

    shifts = []
    for index, item in enumerate(measures):
        dx = quantise((values[reference] - values[index]) * scale) if horizontal_mode else 0
        if row_baseline:
            dy = quantise((row_ground[rows[reference]] - row_ground[rows[index]]) * scale)
        elif "feet" in locks:
            dy = quantise((measures[reference]["ground"] - item["ground"]) * scale)
        else:
            dy = 0
        shifts.append([int(dx), int(dy)])
    info = {"horizontal": horizontal_mode, "vertical": "row-baseline" if row_baseline else ("feet" if "feet" in locks else None),
            "row_grounds": {str(row): ground for row, ground in sorted(row_ground.items())},
            "feature_values": None if not horizontal_mode else [round(float(value), 2) for value in values]}
    return shifts, info


# --------------------------------------------------------------------------- rendering

def _slack(scale: float, resampler: str) -> int:
    """Output pixels a resampling kernel can reach past the mapped box."""
    return 2 if resampler == "nearest" else 4 + math.ceil(3.0 * max(1.0, scale))


def render_set(frames: Sequence[np.ndarray], measures: Sequence[dict], shifts: Sequence[Sequence[int]], *,
               scale: float, resampler: str, anchor_src: Sequence[float]) -> tuple[list[np.ndarray], tuple[int, int]]:
    """Render every frame on one work canvas big enough for all of them; return frames and the anchor there."""
    slack = _slack(scale, resampler)
    lefts, tops, rights, bottoms = [], [], [], []
    for item, (dx, dy) in zip(measures, shifts):
        x0, y0, x1, y1 = item["bbox_any"]
        lefts.append(math.floor((x0 - anchor_src[0]) * scale) + dx - slack)
        tops.append(math.floor((y0 - anchor_src[1]) * scale) + dy - slack)
        rights.append(math.ceil((x1 - anchor_src[0]) * scale) + dx + slack)
        bottoms.append(math.ceil((y1 - anchor_src[1]) * scale) + dy + slack)
    origin = (min(lefts), min(tops))
    size = (max(rights) - origin[0], max(bottoms) - origin[1])
    anchor = (-origin[0], -origin[1])
    rendered = []
    for frame, (dx, dy) in zip(frames, shifts):
        image = forge_core.resample_rgba(Image.fromarray(frame, "RGBA"), scale, resampler, anchor_src=anchor_src,
                                         anchor_dst=(anchor[0] + dx, anchor[1] + dy), out_size=size)
        rendered.append(np.asarray(image))
    return rendered, anchor


def _union_box(frames: Sequence[np.ndarray]) -> tuple[int, int, int, int]:
    boxes = [forge_core.subject_bbox(frame[..., 3], 0) for frame in frames]
    boxes = [box for box in boxes if box is not None]
    if not boxes:
        raise ScaleError("Every frame vanished at this scale; use a larger scale.")
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _transitions(frames: Sequence[np.ndarray], loop: bool) -> list[float]:
    steps = [(index, index + 1) for index in range(len(frames) - 1)] + ([(len(frames) - 1, 0)] if loop else [])
    return [round(forge_core.transition_mae(frames[a], frames[b]), 3) for a, b in steps]


def turn_slide(frame: np.ndarray, anchor_x: float, threshold: int, band: int) -> float | None:
    """Feet movement, in output pixels, when an output frame is mirrored about anchor_x:
    2 x |stance x - anchor x| measured on the output pixels (0 for a turn in place)."""
    mask = forge_core.subject_mask(frame[..., 3], threshold)
    if not mask.any():
        return None
    stance_x, _ = forge_core.anchor_from_mask(mask, "stance", band_rows=max(1, band))
    return round(2.0 * abs(stance_x - anchor_x), 3)


def render_review(frames: Sequence[np.ndarray], anchor: Sequence[float], columns: int) -> Image.Image:
    """Frames at an integer zoom on a light background with the shared root (red cross) and ground line."""
    height, width = frames[0].shape[:2]
    zoom = max(1, 192 // max(height, 1))
    tile_w, tile_h = width * zoom, height * zoom
    columns = max(1, min(columns, len(frames)))
    rows = math.ceil(len(frames) / columns)
    sheet = Image.new("RGB", (columns * (tile_w + 6) + 6, rows * (tile_h + 6) + 6), (236, 232, 222))
    draw = ImageDraw.Draw(sheet)
    for index, frame in enumerate(frames):
        left = 6 + (index % columns) * (tile_w + 6)
        top = 6 + (index // columns) * (tile_h + 6)
        tile = Image.fromarray(frame, "RGBA").resize((tile_w, tile_h), Image.Resampling.NEAREST)
        draw.rectangle((left, top, left + tile_w - 1, top + tile_h - 1), outline=(205, 200, 190))
        sheet.paste(tile, (left, top), tile)
        ax, ay = left + anchor[0] * zoom, top + anchor[1] * zoom
        draw.line((left, ay, left + tile_w - 1, ay), fill=(40, 90, 255))
        draw.line((ax - 4, ay, ax + 4, ay), fill=(230, 30, 40), width=2)
        draw.line((ax, ay - 4, ax, ay + 4), fill=(230, 30, 40), width=2)
    return sheet


# --------------------------------------------------------------------------- CLI

def load_profile(path: Path) -> dict:
    try:
        profile = forge_core.read_json(path)  # UTF-8 with or without a BOM (D28)
    except (OSError, ValueError) as error:
        raise ScaleError(f"Cannot read --profile {path}: {error}") from None
    if not isinstance(profile, dict) or profile.get("schema") != SCHEMA:
        raise ScaleError(f"--profile must be a {RECORD_NAME} written by this tool ({SCHEMA}).")
    return {"scale": float(profile["scale"]), "resampler": profile.get("resampler", "lanczos"),
            "canvas": profile.get("base_canvas") or profile["canvas"],
            "anchor_px": profile.get("base_anchor_px") or profile["anchor_px"]}


def _frame_names(sources: Sequence[dict]) -> list[str]:
    names: list[str] = []
    for index, source in enumerate(sources):
        stem = f"frame-{index:02d}" if source["cell"] is not None else Path(source["file"]).stem
        name, counter = stem, 1
        while name in names:
            name, counter = f"{stem}-{counter}", counter + 1
        names.append(name)
    return names


def _per_frame(text: str, count: int, flag: str) -> int | list[int]:
    """One positive integer, or one per frame, for ``flag`` (``--ticks`` or ``--duration-ms``)."""
    try:
        values = [int(part) for part in text.split(",")]
    except ValueError:
        raise ScaleError(f"{flag} takes one integer or one per frame; got {text!r}.") from None
    if any(value < 1 for value in values) or len(values) not in (1, count):
        raise ScaleError(f"{flag} takes one positive integer or {count} of them; got {text!r}.")
    return values[0] if len(values) == 1 else values


def clip_timing(args: argparse.Namespace, count: int) -> dict:
    """The emitted clip's timing (D11): ticks at tick_hz, or exact integer ms when --duration-ms is given."""
    if args.tick_hz < 1:
        raise ScaleError(f"--tick-hz must be a positive integer; got {args.tick_hz}.")
    if args.duration_ms is not None:
        return {"duration_ms": _per_frame(args.duration_ms, count, "--duration-ms")}
    ticks = DEFAULT_TICKS if args.ticks is None else _per_frame(args.ticks, count, "--ticks")
    return {"ticks": ticks, "tick_hz": args.tick_hz}


def run(args: argparse.Namespace) -> dict:
    frames, sources, input_info, row_size = sheet_qc.load_frames(args)
    count = len(frames)
    if len({frame.shape for frame in frames}) != 1:
        raise ScaleError("All frames must share one canvas size; slice a sheet with --sheet or pad them first.")
    if not 0 <= args.neutral < count:
        raise ScaleError(f"--neutral must be a frame index between 0 and {count - 1}.")
    locks = parse_locks(args.lock)
    torso_colors = sheet_qc.parse_colors(args.torso_colors)
    torso_band = sheet_qc.parse_fraction_pair(args.torso_band, "--torso-band")
    timing = clip_timing(args, count) if args.emit_clips else None
    if args.shift_quantum < 1:
        raise ScaleError("--shift-quantum must be a positive integer.")
    profile = load_profile(Path(args.profile)) if args.profile else None
    if profile:
        clashes = [flag for flag, given in (("--scale-from", args.scale_from), ("--canvas", args.canvas),
                                            ("--anchor-px", args.anchor_px), ("--target-body-px", args.target_body_px))
                   if given is not None]
        if args.resampler is not None and args.resampler != profile["resampler"]:
            clashes.append("--resampler")
        if clashes:
            raise ScaleError(f"--profile already fixes the scale, resampler, canvas and root; drop {', '.join(clashes)}.")
    if (args.canvas is None) != (args.anchor_px is None):
        raise ScaleError("--canvas and --anchor-px go together.")
    measures = [measure(frame, threshold=args.alpha_threshold, torso_colors=torso_colors, torso_band=torso_band,
                        tolerance=args.colour_tol) for frame in frames]
    reference = measures[args.neutral]
    for frame, item in zip(frames, measures):
        item["bbox_any"] = forge_core.subject_bbox(frame[..., 3], 0)
        item["hip_x"] = hip_x(forge_core.subject_mask(frame[..., 3], args.alpha_threshold), reference["ground"],
                              reference["height"])
    scale = profile["scale"] if profile else resolve_scale(
        args.scale_from if args.scale_from is not None else "neutral", args.target_body_px, measures, args.neutral)
    resampler = profile["resampler"] if profile else (args.resampler or "lanczos")
    check_resampler(scale, resampler, args.target_body_px, reference["body_height"])
    anchor_src = reference["points"][args.anchor]
    shifts, lock_info = plan_shifts(measures, reference=args.neutral, scale=scale, root_lock=args.root_lock,
                                    locks=locks, row_baseline=args.row_baseline, row_size=row_size,
                                    quantum=args.shift_quantum)
    after, work_anchor = render_set(frames, measures, shifts, scale=scale, resampler=resampler, anchor_src=anchor_src)
    union = _union_box(after)
    padding = parse_ints(args.action_padding, 4, "--action-padding")
    if profile or args.canvas is not None:
        base_canvas = profile["canvas"] if profile else parse_ints(args.canvas, 2, "--canvas", minimum=1)
        base_anchor = profile["anchor_px"] if profile else parse_point(args.anchor_px, "--anchor-px")
        if any(value != int(value) for value in base_anchor):
            raise ScaleError("--anchor-px must be whole pixels so every frame keeps the same pixel grid.")
        base_anchor = [int(value) for value in base_anchor]
        origin = (work_anchor[0] - base_anchor[0] - padding[0], work_anchor[1] - base_anchor[1] - padding[1])
        canvas = [base_canvas[0] + padding[0] + padding[2], base_canvas[1] + padding[1] + padding[3]]
        needed = [max(0, work_anchor[0] - base_anchor[0] - union[0]), max(0, work_anchor[1] - base_anchor[1] - union[1]),
                  max(0, union[2] - (work_anchor[0] - base_anchor[0] + base_canvas[0])),
                  max(0, union[3] - (work_anchor[1] - base_anchor[1] + base_canvas[1]))]
        overflow = [needed[side] - padding[side] for side in range(4)]
        if any(value > 0 for value in overflow):
            sides = ", ".join(f"{name} {value} px" for name, value in zip(("left", "top", "right", "bottom"), overflow)
                              if value > 0)
            frames_out = [index for index, frame in enumerate(after)
                          if _leaves(frame, origin, canvas)]
            raise ScaleError(f"frame(s) {frames_out} leave the {canvas[0]}x{canvas[1]} canvas by {sides}; frames are "
                             f"never clamped: grow it with --action-padding {','.join(str(v) for v in needed)} "
                             "or use a smaller scale.")
        anchor_px = [base_anchor[0] + padding[0], base_anchor[1] + padding[1]]
    else:
        base_canvas = base_anchor = needed = None
        margin = args.margin
        origin = (union[0] - margin - padding[0], union[1] - margin - padding[1])
        canvas = [union[2] - union[0] + 2 * margin + padding[0] + padding[2],
                  union[3] - union[1] + 2 * margin + padding[1] + padding[3]]
        anchor_px = [work_anchor[0] - origin[0], work_anchor[1] - origin[1]]
    outputs = [_crop(frame, origin, canvas) for frame in after]
    lost = [frame_in[..., 3].astype(bool).sum() - frame_out[..., 3].astype(bool).sum()
            for frame_in, frame_out in zip(after, outputs)]
    if any(lost):
        raise ScaleError(f"Internal check failed: cropping would drop {max(lost)} visible pixels.")
    if args.emit_clips:
        blank = [index for index, frame in enumerate(outputs)
                 if not ((frame[..., 3] == 0).any() and (frame[..., 3] > 0).any())]
        if blank:
            raise ScaleError(f"Frame(s) {blank} need both transparent and visible pixels for clips.json; "
                             "add --margin or --action-padding.")

    zero = [[0, 0]] * count
    before, before_anchor = render_set(frames, measures, zero, scale=scale, resampler=resampler, anchor_src=anchor_src)
    loop = args.loop
    compare_after, compare_before = _common_crop(after, work_anchor, before, before_anchor)
    transitions = {"pairs": [[index, (index + 1) % count] for index in range(count - (0 if loop else 1))],
                   "before": _transitions(compare_before, loop), "after": _transitions(compare_after, loop),
                   "metric": "forge_core.transition_mae (premultiplied RGBA) on one shared crop"}
    band = max(1, forge_core.round_half_up(reference["support_band_rows"] * scale))
    slides = [turn_slide(frame, anchor_px[0], args.alpha_threshold, band) for frame in outputs]
    # The same measure before pixel sampling: where each frame's source stance lands against the root.
    design_slides = [round(2.0 * abs((item["points"]["stance"][0] - anchor_src[0]) * scale + dx), 3)
                     for item, (dx, _dy) in zip(measures, shifts)]

    output_dir = Path(args.output_dir)
    names = _frame_names(sources)
    with forge_core.staged_output(output_dir) as stage:
        (stage / "frames").mkdir()
        frame_records = []
        for index, (frame, name) in enumerate(zip(outputs, names)):
            path = stage / "frames" / f"{name}.png"
            forge_core.save_png(frame, path)
            item = measures[index]
            frame_records.append({
                "source": forge_core.file_ref(Path(sources[index]["file"]), stage),
                "cell": sources[index]["cell"],
                "output": forge_core.file_ref(path, stage),
                "shift": shifts[index],
                "output_bbox": list(forge_core.subject_bbox(frame[..., 3], 0) or []),
                "turn_slide_px": design_slides[index],
                "turn_slide_output_px": slides[index],
                "measure": {"bbox": item["bbox"], "ground": item["ground"], "body_height": item["body_height"],
                            "stance_x": round(item["points"]["stance"][0], 2), "torso_x": None if item["torso_x"] is None
                            else round(item["torso_x"], 2), "centroid_x": round(item["centroid_x"], 2),
                            "hip_x": None if item["hip_x"] is None else round(item["hip_x"], 2)},
            })
        review = stage / "scale-review.png"
        forge_core.save_png(render_review(outputs, anchor_px, row_size), review)
        checks = [
            sheet_qc.check("no_clipping", "pass", 0, 0, note="every visible source pixel is on the canvas"),
            sheet_qc.check("integer_nearest", "pass" if resampler == "nearest" else "skipped",
                           scale if resampler == "nearest" else None, "N or 1/N"),
            sheet_qc.check("registration", "pass", lock_info, None,
                           note="per-frame whole-pixel shifts against the reference frame"),
            sheet_qc.check("turn_slide", "pass", {"design": max(design_slides),
                                                  "output": max((slide for slide in slides if slide is not None),
                                                                default=None)}, None,
                           note="2 x |stance x - anchor x| in output px, before (design) and after pixel sampling "
                                "(output); 0 means a mirrored turn keeps the feet in place"),
        ]
        record: dict[str, Any] = {
            "schema": SCHEMA, "scale": scale,
            "scale_from": "profile" if profile else (str(Fraction(scale).limit_denominator(4096))
                                                     if isinstance(args.scale_from, float)
                                                     else args.scale_from or "neutral"),
            "target_body_px": args.target_body_px, "resampler": resampler,
            "canvas": canvas, "anchor_px": anchor_px, "padding": padding,
            "base_canvas": base_canvas, "base_anchor_px": base_anchor, "needed_padding": needed,
            "margin": None if base_canvas else args.margin,
            "anchor_mode": args.anchor, "source_anchor": [round(float(value), 3) for value in anchor_src],
            "reference_frame": args.neutral, "root_lock": args.root_lock, "lock": locks,
            "row_baseline": bool(args.row_baseline), "shift_quantum": args.shift_quantum,
            "alpha_threshold": args.alpha_threshold, "frames_per_row": row_size,
            "torso": {"colors": [sheet_qc.hex_color(color) for color in torso_colors], "band": list(torso_band),
                      "method": reference["torso_method"]},
            "frames": frame_records, "transitions": transitions,
            "seam": {"before": forge_core.seam_report(compare_before), "after": forge_core.seam_report(compare_after)}
            if loop and count > 1 else None,
            "input": input_info, "profile": forge_core.file_ref(Path(args.profile), stage) if args.profile else None,
            "qa": {"status": "pass",
                   "method": "one pinned resample per frame (forge_core.resample_rgba) on a work canvas holding every "
                             "frame, then one crop; the crop is checked to keep every visible pixel",
                   "notProven": ["That the locked feature (torso, stance, hip) is the right root for this action.",
                                 "Pose quality and smoothness: review scale-review.png and play the clip."],
                   "checks": checks, "inputs": [], "outputs": [forge_core.file_ref(review, stage)], "tool": dict(TOOL)},
            "tool": dict(TOOL),
        }
        record["qa"]["inputs"] = [ref for ref in {item["source"]["path"]: item["source"] for item in frame_records}.values()]
        if args.emit_clips:
            clip = {"frames": list(range(count)), **timing, "loop_policy": "cycle" if loop else "oneshot"}
            clips = {"schema": CLIPS_SCHEMA, "frames": [f"frames/{name}.png" for name in names], "anchor_px": anchor_px,
                     "clips": {args.clip_name: clip}}
            if resampler == "nearest":  # integer nearest frames are pixel art: the runtime must sample them nearest
                clips.update({"pixel_art": True, "sampling": "nearest"})
            forge_core.write_json(stage / "clips.json", clips)
            record["clips"] = "clips.json"
        forge_core.write_json(stage / RECORD_NAME, record)
    summary = {"status": "pass", "output": str(output_dir), "metadata": str(output_dir / RECORD_NAME),
               "scale": scale, "canvas": canvas, "anchor_px": anchor_px, "shifts": shifts}
    if args.emit_clips:
        summary["clips"] = str(output_dir / "clips.json")
    return summary


def _leaves(frame: np.ndarray, origin: Sequence[int], canvas: Sequence[int]) -> bool:
    visible = frame[..., 3] > 0
    inside = np.zeros_like(visible)
    x0, y0 = max(0, origin[0]), max(0, origin[1])
    inside[y0:origin[1] + canvas[1], x0:origin[0] + canvas[0]] = True
    return bool((visible & ~inside).any())


def _crop(frame: np.ndarray, origin: Sequence[int], canvas: Sequence[int]) -> np.ndarray:
    """Cut the canvas out of a work frame, padding with transparency where it reaches past the work canvas."""
    out = np.zeros((canvas[1], canvas[0], 4), np.uint8)
    src_x0, src_y0 = max(0, origin[0]), max(0, origin[1])
    src_x1 = min(frame.shape[1], origin[0] + canvas[0])
    src_y1 = min(frame.shape[0], origin[1] + canvas[1])
    if src_x0 < src_x1 and src_y0 < src_y1:
        out[src_y0 - origin[1]:src_y1 - origin[1], src_x0 - origin[0]:src_x1 - origin[0]] = \
            frame[src_y0:src_y1, src_x0:src_x1]
    return out


def _common_crop(after: Sequence[np.ndarray], after_anchor: Sequence[int], before: Sequence[np.ndarray],
                 before_anchor: Sequence[int]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Both sets on one anchor-aligned crop covering everything visible in either, for fair comparisons."""
    a_box, b_box = _union_box(after), _union_box(before)
    left = min(a_box[0] - after_anchor[0], b_box[0] - before_anchor[0])
    top = min(a_box[1] - after_anchor[1], b_box[1] - before_anchor[1])
    right = max(a_box[2] - after_anchor[0], b_box[2] - before_anchor[0])
    bottom = max(a_box[3] - after_anchor[1], b_box[3] - before_anchor[1])
    size = [right - left, bottom - top]
    return ([_crop(frame, (after_anchor[0] + left, after_anchor[1] + top), size) for frame in after],
            [_crop(frame, (before_anchor[0] + left, before_anchor[1] + top), size) for frame in before])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sheet", help="raw sheet, sliced by component ownership (needs --rows/--cols)")
    source.add_argument("--frames", nargs="+", help="frame PNGs that share one canvas, in playback order")
    parser.add_argument("--rows", type=sheet_qc._positive_int)
    parser.add_argument("--cols", type=sheet_qc._positive_int)
    parser.add_argument("--count", type=sheet_qc._positive_int, default=None,
                        help="with --sheet: use the first N cells in reading order; later cells may be empty")
    parser.add_argument("--frames-per-row", type=sheet_qc._positive_int, default=None,
                        help="with --frames: frames per sheet row, for --row-baseline (default all in one row)")
    parser.add_argument("--output-dir", required=True, help="new directory for frames/, scale-frames.json, clips.json")
    parser.add_argument("--key", type=sheet_qc._key, default="auto",
                        help="backdrop of opaque inputs: auto, none, magenta, green, blue or #rrggbb")
    parser.add_argument("--scale-from", type=parse_scale, default=None,
                        help="neutral (default) or union with --target-body-px, or a number such as 1/8")
    parser.add_argument("--target-body-px", type=float, default=None,
                        help="body height in output pixels for --scale-from neutral|union")
    parser.add_argument("--neutral", type=int, default=0, help="reference frame for scale, root and locks (default 0)")
    parser.add_argument("--resampler", choices=forge_core.RESAMPLERS, default=None,
                        help="nearest (integer factors, pixel art), box or lanczos (default lanczos)")
    parser.add_argument("--anchor", choices=ANCHORS, default="stance",
                        help="root measured on the neutral frame (default stance: mirrors without foot slide)")
    parser.add_argument("--root-lock", choices=ROOT_LOCKS, default="none",
                        help="horizontal lock of every frame to the neutral frame (default none)")
    parser.add_argument("--lock", action="append", default=None,
                        help="per-frame pins, repeatable or comma-separated: feet (y), x (centroid), hip")
    parser.add_argument("--row-baseline", action="store_true",
                        help="one y shift per sheet row so each row's ground line matches the neutral row")
    parser.add_argument("--shift-quantum", type=int, default=1,
                        help="round every shift to this many output pixels (8 = whole game px at 8x size)")
    parser.add_argument("--torso-colors", help="torso costume colours for --root-lock torso-x, #rrggbb,...")
    parser.add_argument("--torso-band", default="0.40,0.60",
                        help="torso rows as fractions of the body height from the top (default 0.40,0.60)")
    parser.add_argument("--colour-tol", type=float, default=30.0, help="RGB distance for --torso-colors (default 30)")
    parser.add_argument("--canvas", help="fixed canvas W,H in output pixels (with --anchor-px)")
    parser.add_argument("--anchor-px", help="root X,Y on the fixed canvas, whole pixels")
    parser.add_argument("--profile", help="scale-frames.json of an earlier action: reuse its scale, resampler, "
                                          "canvas and root")
    parser.add_argument("--action-padding", default="0",
                        help="grow the canvas by L,T,R,B pixels (or one value for all sides) for this action")
    parser.add_argument("--margin", type=int, default=1, help="transparent margin of an automatic canvas (default 1)")
    parser.add_argument("--alpha-threshold", type=sheet_qc._alpha, default=forge_core.ALPHA_GEOMETRY_THRESHOLD,
                        help="geometry ignores alpha at or below this (default 16)")
    parser.add_argument("--loop", action=argparse.BooleanOptionalAction, default=True,
                        help="the action loops: count the last->first seam, clips loop (default on)")
    parser.add_argument("--emit-clips", action="store_true", help="also write clips.json for build_animation_clips.py")
    parser.add_argument("--clip-name", default="action", help="clip name in clips.json (default action)")
    timing = parser.add_mutually_exclusive_group()
    timing.add_argument("--ticks", default=None,
                        help=f"frame duration for clips.json in ticks at --tick-hz: one integer or one per frame "
                             f"(default {DEFAULT_TICKS}, 100 ms at 60 Hz)")
    timing.add_argument("--duration-ms", default=None,
                        help="exact frame duration for clips.json in integer ms instead of ticks: one integer or one "
                             "per frame (uneven on a 60 Hz loop unless a multiple of 1000/60 ms)")
    parser.add_argument("--tick-hz", type=int, default=60, help="tick rate of --ticks (default 60)")
    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.margin < 0:
        raise ScaleError("--margin must be 0 or more.")
    summary = run(args)
    print(json.dumps(summary, ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry (D26, D27): usage errors exit 2 (argparse); every other failure prints one
    ``error: ...`` line and exits 1."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
