"""Assemble full-frame PNGs into lossless frames, an atlas, a timed WebP and animation.json.

Usage (run from the project root; each command is one line):
  python "<skill-dir>/scripts/assemble_frames.py" --input a.png b.png c.png --output-dir out/loop
  python "<skill-dir>/scripts/assemble_frames.py" --sheet sheet.png --rows 2 --cols 4 --output-dir out/anim
  python "<skill-dir>/scripts/assemble_frames.py" --sheet sheet.png --rows 2 --cols 4 --key chroma --slice ownership --output-dir out/anim
  python "<skill-dir>/scripts/assemble_frames.py" --input f0.png f1.png f2.png f3.png f4.png f5.png --loop-overlap 2 --ambient --output-dir out/water

Inputs are still PNGs of any mode (palette with tRNS, grey, grey+alpha, 1/2/4-bit,
16-bit): they are expanded to 8-bit without loss (16-bit keeps the high byte; the
conversion is recorded). 8-bit RGB stays RGB. Nothing is resized, aligned or
regenerated, and by default (--key none) pixels stay exact, including RGB hidden
under alpha 0. --key chroma keys a uniform backdrop with the shared keyer
(forge_matte.key_still) before slicing; keyed frames have RGB zeroed under alpha 0.

Sheets: --rows/--cols cut a grid whose cells must divide the sheet; --crop-boxes
takes {"items": [{"id": "walk-0", "box": [x0, y0, x1, y1]}, ...]}, a legacy list
of boxes, or a prop-pack {"props": [{"label", "source_box"}]} file. Boxes are
[left, top, right, bottom) with exclusive right/bottom edges. On a sheet with a
transparent background, 8-connected subject components (alpha above
--spill-threshold) that cross their cell are reported before slicing, and the run
fails unless --allow-spill. --slice ownership instead keeps every component whole
in the cell that owns most of it, on one shared padded canvas: frame pixel (u, v)
is sheet pixel (u - pad_left + cell x0, v - pad_top + cell y0) for every frame.

Loops: adjacent steps and the wrap seam are measured (wrap_ratio = seam / mean
step; about 1 is even, well above 1 a pop at the wrap, near 0 a held duplicate).
--static-regions names boxes that should stay still; --static-region-search N
finds each box's drift by normalized cross-correlation within +-N px.
--loop-overlap K crossfades the last K played frames into the first K with a
smoothstep curve (premultiplied); it is for ambient loops only (water, fire, mist,
foliage), requires --ambient and must never be used on characters.

JSON options accept inline JSON or a path to a JSON file (UTF-8, a byte-order mark is
accepted). Usage errors exit 2 (argparse); every other error prints one "error: ..."
line, publishes nothing and exits 1; success prints one JSON line with the output and
metadata paths. Numerical diagnostics are not visual approval of motion or of a
seamless loop.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import struct
import sys

import numpy as np
from PIL import Image, features

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_matte  # noqa: E402


MAX_WEBP_DURATION = (1 << 24) - 1
SCHEMA = "generate2dsprite.full_frames.v2"
TOOL = {"name": "assemble_frames", "version": forge_core.FORGE_PACKAGE_VERSION}  # the package version (D29)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SPILL_THRESHOLD = forge_core.ALPHA_GEOMETRY_THRESHOLD  # subject pixels: alpha above this
SPILL_MIN_AREA = 4          # components smaller than this are specks: reported, never a spill
ATTACH_RADIUS = 6           # px: faint edge pixels join the owner of the nearest subject pixel
BACKGROUND_MIN_SHARE = 0.01  # spill checks need a transparent background (alpha <= threshold)
DRIFT_NCC_MARGIN = 0.02     # a static region drifted when its best shift beats no shift by this
SLICE_MODES = ("grid", "ownership")
KEY_MODES = ("none", "chroma")
_LUMA = np.array([0.299, 0.587, 0.114])
# The shared fileRef helpers (D30); the names stay public here because build_animation_clips uses them.
manifest_path = forge_core.manifest_path
file_ref = forge_core.file_ref


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_option(value: str | None) -> object | None:
    """Inline JSON, or the path of a JSON file; a UTF-8 BOM (Windows PowerShell 5.1 writes one) is accepted (D28)."""
    if value is None:
        return None
    if value.lstrip().startswith(("[", "{")):
        return forge_core.parse_json(value)
    try:
        return forge_core.read_json(value)
    except ValueError as error:
        raise ValueError(f"{Path(value).name} is not valid JSON: {error}") from None


def validate_box(value: object, size: tuple[int, int], label: str) -> tuple[int, int, int, int]:
    require(isinstance(value, list) and len(value) == 4, f"{label} must have four coordinates.")
    require(all(type(v) is int for v in value), f"{label} coordinates must be integers.")
    left, top, right, bottom = value
    require(0 <= left < right <= size[0] and 0 <= top < bottom <= size[1],
            f"{label} is empty or outside the image: {value}; image size is {size}.")
    return left, top, right, bottom


def output_path(path: Path) -> Path:
    """The absolute output directory, resolved the way forge_core.staged_output resolves it."""
    return path.parent.resolve() / path.name


# --------------------------------------------------------------------------- loading and keying

def load_png(path: Path) -> tuple[Image.Image, dict[str, object]]:
    """Load a still PNG of any mode without loss (S23): 8-bit RGB stays RGB, every other mode
    (palette with tRNS, grey, grey+alpha, 1/2/4-bit, 16-bit) becomes 8-bit RGBA via forge_core.load_rgba."""
    with open(path, "rb") as stream:
        head = stream.read(16)
    require(head[:8] == PNG_SIGNATURE and head[12:16] == b"IHDR", f"Input must be a PNG: {path}")
    image, info = forge_core.load_rgba(path)
    if info["source_mode"] == "RGB" and image.getchannel("A").getextrema() == (255, 255):
        image = image.convert("RGB")
    return image, {
        "file_sha256": info["sha256"], "bytes": info["bytes"], "size": info["size"], "mode": image.mode,
        "source_mode": info["source_mode"], "bit_depth": info["bit_depth"], "conversion": info["conversion"],
    }


def key_image(image: Image.Image, args: argparse.Namespace,
              source_name: str = "the input") -> tuple[Image.Image, dict[str, object]]:
    """Key a uniform backdrop with forge_matte.key_still; RGB under alpha 0 is zeroed.

    Keying was asked for explicitly, so an opaque image whose border shows no backdrop of the
    declared key is refused instead of being published unkeyed (sprite review B02).
    """
    try:
        keyed, info = forge_matte.key_still(image.convert("RGBA"), quality=args.key_quality, key=args.key_color,
                                            resampler_hint="nearest" if args.pixel_art else "lanczos")
    except ValueError as error:
        if "hard keying" not in str(error):
            raise
        raise ValueError(f"{error} The binary key used by --pixel-art (or --key-quality hard) needs a magenta "
                         "backdrop; pass --key-quality dominance or soft for a green or blue key.") from error
    estimate = info.get("key_estimate")
    if estimate and not estimate["valid"] and not estimate["native_alpha"]:
        raise ValueError(f"--key chroma found no {estimate['declared']} backdrop on the border of {source_name} "
                         f"({estimate['reason']}); nothing was keyed. Pass --key-color green, blue or #rrggbb for "
                         "another backdrop, or drop --key chroma for complete frames.")
    pixels = np.array(keyed)
    pixels[pixels[..., 3] == 0] = 0
    qa = {key: value for key, value in info["qa"].items() if key != "key"}
    record = {
        "quality": info["quality"], "requested_quality": info["requested_quality"],
        "key_rgb": [forge_core.round_half_up(float(value)) for value in info["key"]],
        "key_estimate": estimate, "qa": qa,
    }
    for name in ("interior_despill", "key_material_share", "params"):
        if name in info:
            record[name] = info[name]
    return Image.fromarray(pixels), record


# --------------------------------------------------------------------------- boxes

def parse_crop_boxes(raw: object, size: tuple[int, int]) -> tuple[list[tuple[str, tuple[int, int, int, int]]], str]:
    """Read crop boxes in the unified form or a legacy alias (DOC-18).

    Unified: {"items": [{"id": "walk-0", "box": [x0, y0, x1, y1]}, ...]}; ``id`` is optional.
    Legacy: a bare list of boxes (assemble_frames) or {"props": [{"label", "source_box"}]}
    (extract_prop_pack --boxes-file). Returns ``[(id, box)]`` and the format name.
    """
    if isinstance(raw, list):
        entries = [(f"crop-{index:02d}", box) for index, box in enumerate(raw)]
        form = "legacy-list"
    elif isinstance(raw, dict) and "items" in raw:
        items = raw["items"]
        require(isinstance(items, list), "--crop-boxes items must be a list of {id, box} objects.")
        entries = []
        for index, item in enumerate(items):
            require(isinstance(item, dict) and "box" in item, f"crop item {index} must be an object with a box.")
            entries.append((item.get("id", f"crop-{index:02d}"), item["box"]))
        form = "items"
    elif isinstance(raw, dict) and "props" in raw:
        props = raw["props"]
        require(isinstance(props, list), "--crop-boxes props must be a list of {label, source_box} objects.")
        entries = []
        for index, item in enumerate(props):
            require(isinstance(item, dict) and "source_box" in item,
                    f"crop prop {index} must be an object with a source_box.")
            entries.append((item.get("label", f"crop-{index:02d}"), item["source_box"]))
        form = "legacy-props"
    else:
        raise ValueError('--crop-boxes must be {"items": [{"id", "box"}]}, a list of boxes, '
                         'or a prop-pack {"props": [{"label", "source_box"}]} object.')
    require(bool(entries), "--crop-boxes must be a nonempty JSON list.")
    seen: set[str] = set()
    boxes = []
    for index, (item_id, box) in enumerate(entries):
        require(isinstance(item_id, str) and bool(item_id.strip()) and item_id not in seen,
                f"crop {index} needs a unique nonempty id.")
        seen.add(item_id)
        boxes.append((item_id, validate_box(box, size, f"crop {index}")))
    return boxes, form


def sheet_boxes(args: argparse.Namespace, size: tuple[int, int]) -> tuple[list[tuple[str, tuple[int, int, int, int]]], dict]:
    if args.crop_boxes is not None:
        require(args.rows is None and args.cols is None, "Use --crop-boxes or --rows/--cols, not both.")
        boxes, form = parse_crop_boxes(json_option(args.crop_boxes), size)
        return boxes, {"source": "crop_boxes", "crop_box_format": form}
    require(type(args.rows) is int and type(args.cols) is int and args.rows > 0 and args.cols > 0,
            "--sheet requires positive --rows and --cols, or explicit --crop-boxes.")
    width, height = size
    divisible = width % args.cols == 0 and height % args.rows == 0
    require(divisible or args.slice == "ownership",
            f"Sheet dimensions {width}x{height} are not divisible by the grid ({args.rows} rows x {args.cols} "
            "columns); supply reviewed --crop-boxes instead, or use --slice ownership (rounded cells on one "
            "shared padded canvas).")
    cells = forge_core.rounded_grid_boxes(width, height, args.rows, args.cols)
    boxes = [(f"r{index // args.cols}c{index % args.cols}", box) for index, box in enumerate(cells)]
    return boxes, {"source": "grid", "rows": args.rows, "cols": args.cols,
                   "rounding": "exact" if divisible else "half-up (forge_core.rounded_grid_boxes)"}


# --------------------------------------------------------------------------- cross-cell spill and ownership

def _component_boxes(labels: np.ndarray, count: int) -> np.ndarray:
    """``(count + 1, 4)`` int array of component bboxes ``(x0, y0, x1, y1)``; row 0 is unused."""
    height, width = labels.shape
    ys, xs = np.nonzero(labels)
    ids = labels[ys, xs]
    boxes = np.zeros((count + 1, 4), np.int64)
    boxes[:, 0], boxes[:, 1] = width, height
    np.minimum.at(boxes[:, 0], ids, xs)
    np.minimum.at(boxes[:, 1], ids, ys)
    np.maximum.at(boxes[:, 2], ids, xs + 1)
    np.maximum.at(boxes[:, 3], ids, ys + 1)
    return boxes


def _box_counts(labels: np.ndarray, count: int, boxes: list[tuple[int, int, int, int]]) -> np.ndarray:
    """``(len(boxes), count + 1)`` pixel counts of each component inside each box."""
    return np.stack([np.bincount(labels[y0:y1, x0:x1].ravel(), minlength=count + 1)
                     for x0, y0, x1, y1 in boxes])


def background_share(alpha: np.ndarray, threshold: int) -> float:
    return float((alpha <= threshold).mean())


def spill_report(alpha: np.ndarray, boxes: list[tuple[str, tuple[int, int, int, int]]], *,
                 threshold: int = SPILL_THRESHOLD, min_area: int = SPILL_MIN_AREA) -> dict:
    """Find subject components that a plain cut of ``boxes`` would split (report v2 P1-3).

    Components are 8-connected ``alpha > threshold`` (forge_core.label_components). Each one
    is owned by the box holding most of its pixels (ties: the first box). A component of at
    least ``min_area`` px with pixels outside its owner box would be cut: that is a spill.
    Components outside every box are reported as unboxed; smaller ones count as specks.
    """
    labels, count = forge_core.label_components(alpha > threshold, 8)
    report: dict = {"threshold": threshold, "min_area": min_area, "connectivity": 8, "components": int(count),
                    "crossing": [], "specks_crossing": 0, "unboxed_components": 0, "unboxed_px": 0}
    if count == 0:
        report["verdict"] = "clean"
        return report
    ids = [item_id for item_id, _ in boxes]
    areas = np.bincount(labels.ravel(), minlength=count + 1)
    counts = _box_counts(labels, count, [box for _, box in boxes])
    owner = counts.argmax(axis=0)
    inside = counts[owner, np.arange(count + 1)]
    covered = np.zeros(alpha.shape, bool)
    for _, (x0, y0, x1, y1) in boxes:
        covered[y0:y1, x0:x1] = True
    outside_all = np.bincount(labels[~covered].ravel(), minlength=count + 1)
    bboxes = _component_boxes(labels, count)
    for label in range(1, count + 1):
        if counts[:, label].max() == 0:
            report["unboxed_components"] += 1
            report["unboxed_px"] += int(areas[label])
            continue
        over = int(areas[label] - inside[label])
        if over == 0:
            continue
        if areas[label] < min_area:
            report["specks_crossing"] += 1
            continue
        box = boxes[int(owner[label])][1]
        x0, y0, x1, y1 = (int(value) for value in bboxes[label])
        report["crossing"].append({
            "label": label, "area": int(areas[label]), "bbox": [x0, y0, x1, y1],
            "owner": int(owner[label]), "owner_id": ids[int(owner[label])],
            "pixels_over": over,
            "overhang_px": max(box[0] - x0, x1 - box[2], box[1] - y0, y1 - box[3], 0),
            "pixels_per_box": {ids[index]: int(counts[index, label])
                               for index in np.flatnonzero(counts[:, label]).tolist()},
            "pixels_outside_every_box": int(outside_all[label]),
        })
    report["verdict"] = "spill" if report["crossing"] else "clean"
    return report


def ownership_slice(rgba: np.ndarray, boxes: list[tuple[str, tuple[int, int, int, int]]], *,
                    threshold: int = SPILL_THRESHOLD, min_area: int = SPILL_MIN_AREA,
                    attach_radius: int = ATTACH_RADIUS) -> tuple[list[np.ndarray], dict, list[dict]]:
    """Cut a sheet so that no subject component is split (report v2 P1-3, game-opus55 fix_sheet).

    forge_core.ownership_slice with the frame-assembly policy of D14 (haze ``keep``):
    components of ``alpha > threshold`` with at least ``min_area`` px go whole to the box
    holding most of their pixels; other visible pixels (faint edges, specks) join the owner
    they reach within ``attach_radius`` px through visible pixels (ties: left, right, above,
    below, then the diagonals); the rest stay in the first box that contains them, and pixels
    outside every box are dropped. Every frame is placed on one shared canvas at its box
    origin plus one shared padding, so the frames keep the sheet's registration. Returns
    ``(frames, report, per-frame records)``.
    """
    frames, shared = forge_core.ownership_slice(rgba, boxes=[box for _, box in boxes], alpha_threshold=threshold,
                                                min_area=min_area, haze="keep", attach_radius=attach_radius)
    report = {key: shared[key] for key in ("mode", "threshold", "min_area", "attach_radius", "connectivity", "canvas",
                                           "padding", "registration", "attached_px", "cell_fallback_px",
                                           "dropped_px")}
    report.update({"haze": shared["haze"], "hidden_rgb": "zeroed (only visible pixels are copied)",
                   "method": f"forge_core.ownership_slice (D14): {shared['method']}"})
    records = [{key: frame[key] for key in ("sheet_origin", "owned_components", "pixels_from_outside_cell")}
               for frame in shared["frames"]]
    return frames, report, records


# --------------------------------------------------------------------------- frames and metrics

def parse_sequence(value: str | None, frame_count: int) -> list[int]:
    if value is None:
        return list(range(frame_count))
    try:
        indices = [int(token.strip()) for token in value.split(",")]
    except ValueError as error:
        raise ValueError("--sequence must be comma-separated integer frame indices.") from error
    require(bool(indices) and all(0 <= index < frame_count for index in indices),
            f"Sequence indices must be between 0 and {frame_count - 1}.")
    return indices


def pixel_array(frame: Image.Image) -> np.ndarray:
    return np.asarray(frame.convert("RGBA"), dtype=np.uint8)


def same_visible_pixels(left: np.ndarray, right: np.ndarray) -> bool:
    if left.shape != right.shape or not np.array_equal(left[..., 3], right[..., 3]):
        return False
    return np.array_equal(left[..., :3][left[..., 3] > 0], right[..., :3][right[..., 3] > 0])


def transition_metrics(left: np.ndarray, right: np.ndarray) -> dict[str, float | int]:
    # Alpha-weighted RGB ignores meaningless hidden colors, while alpha is also
    # reported separately. For opaque frames this is ordinary full-frame RGB MAE.
    left_rgb = left[..., :3].astype(np.float32) * (left[..., 3:4].astype(np.float32) / 255)
    right_rgb = right[..., :3].astype(np.float32) * (right[..., 3:4].astype(np.float32) / 255)
    delta = np.abs(left_rgb - right_rgb)
    alpha_delta = np.abs(left[..., 3].astype(np.int16) - right[..., 3].astype(np.int16))
    return {
        "premultiplied_rgb_mae": round(float(delta.mean()), 6),
        "alpha_mae": round(float(alpha_delta.mean()), 6),
        "changed_visible_pixels": int(np.count_nonzero(np.any(delta > 0, axis=2) | (alpha_delta > 0))),
    }


def loop_seam(played: list[np.ndarray]) -> dict | None:
    """forge_core.seam_report of a played loop, plus ``adjacent_mean`` and ``wrap_ratio``.

    ``wrap_ratio`` is the seam over the mean adjacent step, the seam ratio of the Dusk scene
    loops: about 1 for an even loop, well above 1 for a pop at the wrap, near 0 for a held
    duplicate at the wrap. A --loop-overlap crossfade lands below 1, because its blended steps
    are larger than ordinary ones. The median and p95 ratios of seam_report stay beside it.
    """
    if len(played) < 2:
        return None
    report = dict(forge_core.seam_report(played))
    mean = float(np.mean([forge_core.transition_mae(first, second) for first, second in zip(played, played[1:])]))
    report.update({"adjacent_mean": mean, "wrap_ratio": report["seam"] / max(mean, 1e-6)})
    return {name: (round(value, 6) if isinstance(value, float) else value) for name, value in report.items()}


def smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def blend_rgba(first: np.ndarray, second: np.ndarray, weight: float) -> np.ndarray:
    """``(1 - weight) * first + weight * second`` in premultiplied space, rounded half-up to straight RGBA."""
    a = first.astype(np.float64)
    b = second.astype(np.float64)
    alpha_a, alpha_b = a[..., 3:] / 255.0, b[..., 3:] / 255.0
    alpha = alpha_a * (1.0 - weight) + alpha_b * weight
    colour = a[..., :3] * alpha_a * (1.0 - weight) + b[..., :3] * alpha_b * weight
    rgb = np.divide(colour, alpha, out=np.zeros_like(colour), where=alpha > 0)
    out = np.dstack([np.floor(np.clip(rgb, 0, 255) + 0.5), np.floor(alpha[..., 0] * 255.0 + 0.5)]).astype(np.uint8)
    out[out[..., 3] == 0] = 0
    return out


def loop_overlap(pixels: list[np.ndarray], sequence: list[int], overlap: int) -> tuple[list[int], list[tuple[np.ndarray, dict]], dict]:
    """Forward-overlap crossfade for ambient loops (Dusk build-scenes.py L56, with a smoothstep curve).

    Plays positions ``overlap .. n - overlap - 1`` of ``sequence`` unchanged, then ``overlap``
    frames that blend position ``n - overlap + j`` into position ``j`` with weight
    ``smoothstep((j + 1) / overlap)``. The last weight is 1, so the loop ends on position
    ``overlap - 1`` and wraps to position ``overlap``: both joins are ordinary steps.
    Returns the new sequence (derived frames are numbered after the existing ones), the
    derived frames with their records, and a report.
    """
    count = len(sequence)
    require(overlap >= 2, "--loop-overlap needs at least 2 frames to crossfade.")
    require(2 * overlap < count,
            f"--loop-overlap {overlap} needs at least {2 * overlap + 1} played positions; the sequence has {count}.")
    derived: list[tuple[np.ndarray, dict]] = []
    tail = []
    for step in range(overlap):
        weight = smoothstep((step + 1) / overlap)
        leaving, entering = sequence[count - overlap + step], sequence[step]
        if weight >= 1.0:
            tail.append(entering)
            continue
        blended = blend_rgba(pixels[leaving], pixels[entering], weight)
        tail.append(len(pixels) + len(derived))
        derived.append((blended, {"blend_of": [leaving, entering], "weight": round(weight, 6),
                                  "curve": "smoothstep", "space": "premultiplied RGBA"}))
    new_sequence = sequence[overlap:count - overlap] + tail
    report = {"frames": overlap, "curve": "smoothstep((j + 1) / K)", "played_before": list(sequence),
              "played_after": new_sequence, "ambient_only": True,
              "note": "Blended frames are new pixels; never crossfade characters (ghosted limbs)."}
    return new_sequence, derived, report


def premultiplied_luma(array: np.ndarray) -> np.ndarray:
    return (array[..., :3].astype(np.float64) @ _LUMA) * (array[..., 3].astype(np.float64) / 255.0)


def _window_sums(values: np.ndarray, height: int, width: int) -> np.ndarray:
    table = np.pad(values, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return table[height:, width:] - table[:-height, width:] - table[height:, :-width] + table[:-height, :-width]


def ncc_drift(reference: np.ndarray, frame: np.ndarray, box: tuple[int, int, int, int], radius: int) -> dict:
    """Integer shift (dx, dy) within +-radius that best re-registers ``box`` of ``reference`` in ``frame``.

    Zero-mean normalized cross-correlation of premultiplied luma: the content of the box
    moved by (dx, dy) when ``frame[y + dy, x + dx]`` matches ``reference[y, x]``. Shifts that
    would leave the image are skipped. Ties prefer the smallest shift.
    """
    left, top, right, bottom = box
    height, width = frame.shape
    template = reference[top:bottom, left:right]
    centred = template - template.mean()
    energy = float((centred * centred).sum())
    if energy <= 1e-9:
        return {"shift": None, "ncc": None, "ncc_at_zero": None, "drift_px": None, "drifted": False,
                "reason": "flat region: no texture to register"}
    dx0, dx1 = max(-radius, -left), min(radius, width - right)
    dy0, dy1 = max(-radius, -top), min(radius, height - bottom)
    area = frame[top + dy0:bottom + dy1, left + dx0:right + dx1]
    box_h, box_w = template.shape
    windows = np.lib.stride_tricks.sliding_window_view(area, (box_h, box_w))
    numerator = np.einsum("ijhw,hw->ij", windows, centred)
    sums = _window_sums(area, box_h, box_w)
    variance = np.maximum(_window_sums(area * area, box_h, box_w) - sums * sums / (box_h * box_w), 0.0)
    denominator = np.sqrt(variance * energy)
    scores = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 1e-9)
    best = float(scores.max())
    candidates = np.argwhere(scores >= best - 1e-9)
    dy, dx = min(((int(row) + dy0, int(col) + dx0) for row, col in candidates),
                 key=lambda shift: (abs(shift[0]) + abs(shift[1]), abs(shift[0]), shift[0], shift[1]))
    at_zero = float(scores[-dy0, -dx0])
    return {"shift": [dx, dy], "ncc": round(best, 6), "ncc_at_zero": round(at_zero, 6),
            "drift_px": max(abs(dx), abs(dy)), "drifted": (dx, dy) != (0, 0) and best - at_zero >= DRIFT_NCC_MARGIN}


def static_region_reports(pixels: list[np.ndarray], regions: dict[str, tuple[int, int, int, int]],
                          reference: int, search: int) -> dict:
    """Per-region change against the reference frame, plus NCC drift when ``search`` > 0."""
    lumas = [premultiplied_luma(array) for array in pixels] if search else []
    reports = {}
    for name, (left, top, right, bottom) in regions.items():
        crops = [array[top:bottom, left:right] for array in pixels]
        rows = [{"frame": index, **transition_metrics(crops[reference], crop)} for index, crop in enumerate(crops)]
        report = {"box": [left, top, right, bottom], "reference_frame": reference,
                  "against_reference": rows, "registration_performed": False}
        if search:
            for row, luma in zip(rows, lumas):
                row["drift"] = ncc_drift(lumas[reference], luma, (left, top, right, bottom), search)
            drifting = [row["frame"] for row in rows if row["drift"]["drifted"]]
            report.update({"search_px": search, "method": "zero-mean NCC of premultiplied luma, integer shifts",
                           "max_drift_px": max((row["drift"]["drift_px"] or 0) for row in rows),
                           "drifting_frames": drifting})
        reports[name] = report
    return reports


# --------------------------------------------------------------------------- WebP

def _riff_chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) % 2 else b"")


def _timed_static_webp(path: Path, frame: Image.Image, total_duration: int) -> None:
    """Retain ANIM timing when libwebp collapses all identical frames to a still.

    A single ANMF is valid animated WebP. Full-canvas no-blend frames retain the
    original pixels; no artificial pixel changes are used to defeat merging.
    """
    buffer = io.BytesIO()
    frame.save(buffer, format="WEBP", lossless=True, exact=True, quality=75, method=3)
    data = buffer.getvalue()
    require(data[:4] == b"RIFF" and data[8:12] == b"WEBP", "Invalid static WebP encoder output.")
    image_chunks = []
    offset = 12
    while offset + 8 <= len(data):
        tag = data[offset:offset + 4]
        length = int.from_bytes(data[offset + 4:offset + 8], "little")
        end = offset + 8 + length + length % 2
        require(end <= len(data), "Truncated static WebP chunk.")
        if tag in {b"VP8L", b"VP8 ", b"ALPH"}:
            image_chunks.append(data[offset:end])
        offset = end
    require(bool(image_chunks), "Static WebP has no image payload.")
    u24 = lambda value: value.to_bytes(3, "little")
    width, height = frame.size
    has_alpha = frame.mode == "RGBA" and frame.getchannel("A").getextrema()[0] < 255
    flags = 0x02 | (0x10 if has_alpha else 0)
    parts = [_riff_chunk(b"VP8X", bytes([flags, 0, 0, 0]) + u24(width - 1) + u24(height - 1)),
             _riff_chunk(b"ANIM", b"\0" * 6)]
    remaining = total_duration
    while remaining:
        duration = min(remaining, MAX_WEBP_DURATION)
        header = b"\0" * 6 + u24(width - 1) + u24(height - 1) + u24(duration) + b"\x02"
        parts.append(_riff_chunk(b"ANMF", header + b"".join(image_chunks)))
        remaining -= duration
    body = b"WEBP" + b"".join(parts)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def validate_webp(path: Path, pixels: list[np.ndarray], sequence: list[int], duration: int) -> dict[str, object]:
    expected_total = len(sequence) * duration
    elapsed = 0
    decoded_records = []
    with Image.open(path) as decoded:
        require(decoded.size == (pixels[0].shape[1], pixels[0].shape[0]), "WebP dimensions changed.")
        require(decoded.info.get("loop") == 0, "WebP infinite-loop flag is missing.")
        for index in range(decoded.n_frames):
            decoded.seek(index)
            decoded.load()
            actual_duration = decoded.info.get("duration")
            require(type(actual_duration) is int and actual_duration > 0, "WebP has no positive frame duration.")
            require(decoded.info.get("timestamp") == elapsed, "WebP timestamps are discontinuous.")
            end = elapsed + actual_duration
            require(end <= expected_total, "WebP timeline exceeds the requested duration.")
            actual = pixel_array(decoded)
            position = elapsed
            matching_positions = []
            while position < end:
                requested_position = position // duration
                require(same_visible_pixels(actual, pixels[sequence[requested_position]]),
                        f"WebP alpha/visible RGB mismatch at requested sequence position {requested_position}.")
                matching_positions.append(requested_position)
                position = min(end, (requested_position + 1) * duration)
            decoded_records.append({
                "index": index, "timestamp_ms": elapsed, "duration_ms": actual_duration,
                "sequence_positions": matching_positions,
            })
            elapsed = end
    require(elapsed == expected_total, f"WebP duration {elapsed} differs from requested {expected_total} ms.")
    return {
        "decoded_frame_count": len(decoded_records), "decoded_frames": decoded_records,
        "total_duration_ms": elapsed, "loop": 0,
        "alpha_and_visible_rgb_exact": True, "timeline_verified": True,
        "hidden_rgb_guaranteed": False,
    }


def save_animation(path: Path, images: list[Image.Image], duration: int | list[int], loop: int, *,
                   all_keyframes: bool = False) -> None:
    """Write a lossless animated WebP with Pillow.

    ``all_keyframes`` (kmin = kmax = 1) is the fallback for a libwebp 1.6 animation-encoder
    defect: when it crops a fully opaque first frame out of a transparent canvas whose hidden
    RGB is black, it drops the VP8X alpha flag and decoders paint the canvas opaque black.
    Callers try the default first (cfed170 bytes) and re-encode only when decoding fails.
    """
    options = {"kmin": 1, "kmax": 1} if all_keyframes else {}
    images[0].save(path, format="WEBP", save_all=True, append_images=images[1:], duration=duration, loop=loop,
                   lossless=True, quality=75, method=3, allow_mixed=False, **options)


def encode_webp(path: Path, frames: list[Image.Image], pixels: list[np.ndarray], sequence: list[int], duration: int) -> dict[str, object]:
    selected = [frames[index] for index in sequence]
    for all_keyframes in (False, True):
        try:
            save_animation(path, selected, duration, 0, all_keyframes=all_keyframes)
            with Image.open(path) as decoded:
                decoded.load()
                collapsed_to_still = decoded.n_frames == 1 and (
                    decoded.info.get("duration", 0) <= 0 or decoded.info.get("loop") != 0
                )
            if collapsed_to_still:
                require(all(same_visible_pixels(pixels[sequence[0]], pixels[index]) for index in sequence),
                        "Encoder collapsed distinct visible frames into one still.")
                _timed_static_webp(path, selected[0], len(sequence) * duration)
            result = validate_webp(path, pixels, sequence, duration)
            break
        except ValueError:
            if all_keyframes:
                raise
    result.update({"file": path.name, "file_sha256": digest(path.read_bytes()), "bytes": path.stat().st_size,
                   "lossless": True, "quality": 75, "method": 3,
                   "timed_static_container": collapsed_to_still, "all_keyframes": all_keyframes})
    return result


# --------------------------------------------------------------------------- assembly

def _check_options(args: argparse.Namespace) -> None:
    require(type(args.duration) is int and 0 < args.duration <= MAX_WEBP_DURATION,
            f"Duration must be between 1 and {MAX_WEBP_DURATION} integer milliseconds.")
    if args.input:
        require(args.rows is None and args.cols is None and args.crop_boxes is None,
                "--rows, --cols and --crop-boxes require --sheet.")
        require(args.slice == "grid", "--slice ownership needs --sheet.")
    require(0 <= args.spill_threshold < 255, "--spill-threshold must be between 0 and 254.")
    require(args.spill_min_area >= 1, "--spill-min-area must be at least 1 px.")
    require(args.attach_radius >= 0, "--attach-radius must be zero or positive.")
    require(args.static_region_search >= 0, "--static-region-search must be zero or a positive radius in px.")
    require(args.static_region_search == 0 or args.static_regions is not None,
            "--static-region-search needs --static-regions.")
    require(args.loop_overlap >= 0, "--loop-overlap must be zero (off) or a frame count.")
    require(args.loop_overlap == 0 or args.ambient,
            "--loop-overlap crossfades frames and is for ambient loops only (water, fire, mist, foliage); "
            "pass --ambient to confirm. Never crossfade characters.")


def _load_sources(args: argparse.Namespace, final: Path) -> tuple[list[Image.Image], list[dict]]:
    paths = list(args.input) if args.input else [args.sheet]
    images, records = [], []
    for path in paths:
        image, record = load_png(path)
        record = {"path": manifest_path(path, final), **record}
        if args.key == "chroma":
            image, record["key"] = key_image(image, args, Path(path).name)
            record["mode"] = image.mode
        images.append(image)
        records.append(record)
    return images, records


def _slice_sheet(sheet: Image.Image, args: argparse.Namespace) -> tuple[list[Image.Image], list[dict], dict, dict | None]:
    """Cut the sheet by grid or crop boxes after the spill check, or by ownership."""
    boxes, layout = sheet_boxes(args, sheet.size)
    rgba = pixel_array(sheet)
    alpha = rgba[..., 3]
    share = background_share(alpha, args.spill_threshold)
    if args.slice == "ownership":
        require(share >= BACKGROUND_MIN_SHARE,
                "--slice ownership needs a transparent background to separate subjects; key the sheet with "
                "--key chroma or use --slice grid.")
        spill = spill_report(alpha, boxes, threshold=args.spill_threshold, min_area=args.spill_min_area)
        spill.update({"status": "resolved" if spill["verdict"] == "spill" else "clean",
                      "background_share": round(share, 6),
                      "note": "--slice ownership keeps each crossing component whole in its owner cell."})
        arrays, report, extra = ownership_slice(rgba, boxes, threshold=args.spill_threshold,
                                                min_area=args.spill_min_area, attach_radius=args.attach_radius)
        frames = [Image.fromarray(array) for array in arrays]
        origins = [{"source_index": 0, "crop_box": list(box), "crop_id": item_id, **record}
                   for (item_id, box), record in zip(boxes, extra)]
        return frames, origins, {**layout, **report}, spill
    if share < BACKGROUND_MIN_SHARE:
        spill = {"status": "skipped", "background_share": round(share, 6),
                 "reason": "the sheet has no transparent background to separate subjects (opaque full frames); "
                           "key a chroma sheet with --key chroma to check it."}
    else:
        spill = spill_report(alpha, boxes, threshold=args.spill_threshold, min_area=args.spill_min_area)
        spill["background_share"] = round(share, 6)
        if spill["verdict"] == "spill" and not args.allow_spill:
            worst = max(spill["crossing"], key=lambda item: item["pixels_over"])
            raise ValueError(
                f"Cross-cell spill: {len(spill['crossing'])} subject component(s) cross their cell. The largest "
                f"overflow is component {worst['label']} ({worst['area']} px, bbox {worst['bbox']}) owned by "
                f"{worst['owner_id']}, with {worst['pixels_over']} px outside it (overhang {worst['overhang_px']} px). "
                "Use --slice ownership to keep components whole, regenerate with wider cells, or pass "
                "--allow-spill to cut them anyway.")
        spill["status"] = "allowed" if spill["verdict"] == "spill" else "clean"
    frames = [sheet.crop(box) for _, box in boxes]
    origins = [{"source_index": 0, "crop_box": list(box), "crop_id": item_id} for item_id, box in boxes]
    return frames, origins, {**layout, "mode": "grid"}, spill


def _qa_envelope(manifest: dict, sources: list[Path], final: Path, stage: Path, outputs: list[str]) -> dict:
    checks = [
        {"id": "png_rgba_exact", "status": "pass", "value": True, "threshold": None},
        {"id": "atlas_rgba_exact", "status": "pass", "value": True, "threshold": None},
        {"id": "webp_timeline", "status": "pass", "value": manifest["webp"]["total_duration_ms"], "threshold": None},
    ]
    spill = manifest["spill_check"]
    if spill is None:
        checks.append({"id": "cross_cell_spill", "status": "skipped", "value": None, "threshold": None})
    else:
        status = {"clean": "pass", "resolved": "pass", "allowed": "warn", "skipped": "skipped"}[spill["status"]]
        over = sum(item["pixels_over"] for item in spill.get("crossing", []))
        checks.append({"id": "cross_cell_spill", "status": status, "value": over, "threshold": 0})
    keys = [record["key"] for record in manifest["sources"] if "key" in record]
    if keys:
        residue = sum(key["qa"]["opaque_key_px"] for key in keys)
        checks.append({"id": "chroma_key_residue", "status": "pass" if residue == 0 else "warn",
                       "value": residue, "threshold": 0})
        estimates = [key["key_estimate"] for key in keys if key["key_estimate"]]
        missing = sum(1 for estimate in estimates if not estimate["valid"] and not estimate["use_native_alpha"])
        checks.append({"id": "chroma_key_backdrop", "status": "warn" if missing else "pass", "value": missing,
                       "threshold": 0})
    seam = manifest["loop_seam"]
    checks.append({"id": "loop_seam", "status": "needs-visual-review" if seam else "skipped",
                   "value": None if seam is None else seam["wrap_ratio"], "threshold": None})
    searched = [region for region in manifest["static_regions"].values() if "search_px" in region]
    if searched:
        drifting = sorted({frame for region in searched for frame in region["drifting_frames"]})
        checks.append({"id": "static_region_drift", "status": "warn" if drifting else "pass",
                       "value": max(region["max_drift_px"] for region in searched), "threshold": 0})
    status = "warn" if any(check["status"] == "warn" for check in checks) else "needs-visual-review"
    return {
        "status": status,
        "method": ("Lossless packaging: frame PNGs and the atlas are decoded and compared pixel for pixel; the "
                   "WebP timeline and its visible RGBA are decoded and verified. Spill: 8-connected components of "
                   "alpha above the threshold that leave their owner cell. Seams: forge_core.seam_report of the "
                   "played sequence (premultiplied RGBA MAE); wrap_ratio = seam / mean adjacent step. Drift: NCC "
                   "of premultiplied luma."),
        "notProven": ["visual quality, identity or anatomy of any frame",
                      "smooth motion or a seamless loop (seam ratios are numeric diagnostics only)",
                      "that the crop boxes, grid or sequence match the intended phases",
                      "that RGB hidden under alpha 0 survives the WebP preview"],
        "checks": checks,
        "inputs": [file_ref(path, final) for path in sources],
        "outputs": [{"path": name, "sha256": forge_core.sha256_file(stage / name),
                     "bytes": (stage / name).stat().st_size} for name in outputs],
        "tool": dict(TOOL),
    }


def assemble(args: argparse.Namespace) -> dict[str, object]:
    if os.path.lexists(args.output_dir):
        raise FileExistsError(f"Refusing existing output: {args.output_dir}")
    _check_options(args)
    require(features.check("webp"), "Pillow requires WebP support.")
    final = output_path(args.output_dir)
    images, sources = _load_sources(args, final)
    spill = None
    layout: dict = {"source": "inputs", "mode": "inputs"}
    if args.input:
        frames = images
        origins = [{"source_index": index} for index in range(len(images))]
    else:
        frames, origins, layout, spill = _slice_sheet(images[0], args)
    require(bool(frames), "At least one input frame is required.")
    require(len({frame.size for frame in frames}) == 1,
            f"Frame dimensions differ: {[frame.size for frame in frames]}; no resizing is performed.")
    source_count = len(frames)
    sequence = parse_sequence(args.sequence, source_count)
    pixels = [pixel_array(frame) for frame in frames]
    overlap = None
    if args.loop_overlap:
        sequence, derived, overlap = loop_overlap(pixels, sequence, args.loop_overlap)
        overlap["seam_before"] = loop_seam([pixels[index] for index in overlap["played_before"]])
        rgb_only = all(frame.mode == "RGB" for frame in frames)
        for array, record in derived:
            pixels.append(array)
            frames.append(Image.fromarray(np.ascontiguousarray(array[..., :3]) if rgb_only else array))
            origins.append({"derived": record})
    require(len(sequence) * args.duration < (1 << 31), "Total duration exceeds the WebP encoder timestamp range.")
    manifest = json_option(args.manifest)
    require(manifest is None or isinstance(manifest, dict), "--manifest must be a JSON object.")
    regions = json_option(args.static_regions)
    require(regions is None or isinstance(regions, dict), "--static-regions must be a JSON object of named boxes.")
    region_boxes = {name: validate_box(box, frames[0].size, f"region {name}")
                    for name, box in (regions or {}).items()}
    source_paths = list(args.input) if args.input else [args.sheet]
    with forge_core.staged_output(final) as stage:
        frame_dir = stage / "frames"
        frame_dir.mkdir()
        records = []
        for index, frame in enumerate(frames):
            path = frame_dir / f"frame-{index:02d}.png"
            forge_core.save_png(frame, path, zero_transparent_rgb=False)
            with Image.open(path) as decoded:
                require(np.array_equal(pixel_array(decoded), pixels[index]), "PNG pixel roundtrip failed.")
            records.append({
                "index": index, "file": path.relative_to(stage).as_posix(), **origins[index],
                "size": list(frame.size), "mode": frame.mode,
                "file_sha256": digest(path.read_bytes()), "rgba_pixel_sha256": digest(pixels[index].tobytes()),
            })
        columns = math.ceil(math.sqrt(len(frames)))
        rows = math.ceil(len(frames) / columns)
        width, height = frames[0].size
        atlas = Image.new("RGBA", (columns * width, rows * height))
        for index, frame in enumerate(frames):
            atlas.paste(frame, ((index % columns) * width, (index // columns) * height))
        atlas_path = stage / "atlas.png"
        forge_core.save_png(atlas, atlas_path, zero_transparent_rgb=False)
        with Image.open(atlas_path) as decoded:
            for index, expected in enumerate(pixels):
                x, y = (index % columns) * width, (index // columns) * height
                require(np.array_equal(pixel_array(decoded.crop((x, y, x + width, y + height))), expected),
                        "Atlas pixel roundtrip failed.")
        webp = encode_webp(stage / "animation.webp", frames, pixels, sequence, args.duration)
        transitions = [{"from_sequence_position": position,
                        "to_sequence_position": (position + 1) % len(sequence),
                        "from_frame": index, "to_frame": sequence[(position + 1) % len(sequence)],
                        "wrap": position == len(sequence) - 1,
                        **transition_metrics(pixels[index], pixels[sequence[(position + 1) % len(sequence)]])}
                       for position, index in enumerate(sequence)]
        seam = loop_seam([pixels[index] for index in sequence])
        if overlap is not None:
            overlap["seam_after"] = seam
        result = {
            "schema": SCHEMA,
            "frame_size": [width, height], "source_frame_count": source_count,
            "sources": sources, "frames": records, "sequence": sequence,
            "duration_ms": args.duration, "total_duration_ms": len(sequence) * args.duration, "loop": 0,
            "atlas": {"file": "atlas.png", "size": list(atlas.size), "rows": rows, "cols": columns,
                      "file_sha256": digest(atlas_path.read_bytes()), "rgba_pixels_exact": True},
            "webp": webp, "transitions": transitions,
            "loop_seam": seam, "loop_overlap": overlap,
            "static_regions": static_region_reports(pixels, region_boxes, sequence[0], args.static_region_search),
            "key": {"mode": args.key} if args.key == "none" else {
                "mode": "chroma", "key_color": args.key_color, "requested_quality": args.key_quality,
                "pixel_art": bool(args.pixel_art), "per_source": "sources[].key"},
            "slicing": layout, "spill_check": spill,
            "provenance": {"user_manifest": manifest, "user_assertions_verified": False},
            "validation": {"png_rgba_exact": True, "atlas_rgba_exact": True,
                           "webp_timeline_verified": True, "visual_approval": False,
                           "note": "Numerical diagnostics only; no claim of seamless motion or visual continuity."},
            "processing": {"resized": False, "aligned": False, "chroma_keyed": args.key == "chroma",
                           "masked": layout.get("mode") == "ownership", "blended": overlap is not None},
        }
        outputs = [record["file"] for record in records] + ["atlas.png", "animation.webp"]
        result["qa"] = _qa_envelope(result, source_paths, final, stage, outputs)
        if args.strict:
            warned = [check["id"] for check in result["qa"]["checks"] if check["status"] == "warn"]
            require(not warned, f"--strict: QA warnings {', '.join(warned)}; nothing was published.")
        forge_core.write_json(stage / "animation.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", nargs="+", type=Path, help="Ordered still PNG files (one frame each).")
    source.add_argument("--sheet", type=Path, help="One still PNG containing the frames.")
    parser.add_argument("--rows", type=int, help="Grid rows of --sheet.")
    parser.add_argument("--cols", type=int, help="Grid columns of --sheet.")
    parser.add_argument("--crop-boxes", help='Crop boxes of --sheet: {"items": [{"id", "box"}]} (or a legacy list '
                                             "of boxes, or a prop-pack props file), inline or as a JSON path.")
    parser.add_argument("--sequence", help="Comma-separated zero-based frame indices to play; default input order.")
    parser.add_argument("--duration", type=int, default=250, help="Duration in milliseconds of each played position.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory; existing paths are refused.")
    parser.add_argument("--manifest", help="User-authored provenance JSON object, or path to that JSON.")
    parser.add_argument("--static-regions", help="JSON {name: [left, top, right, bottom]} of regions that should "
                                                 "stay still, or path to that JSON.")
    parser.add_argument("--static-region-search", type=int, default=0, metavar="N",
                        help="Search +-N px for each static region's drift (NCC); 0 (default) only diffs in place.")
    parser.add_argument("--key", choices=KEY_MODES, default="none",
                        help="none (default): pixels stay exact; chroma: key a uniform backdrop before slicing.")
    parser.add_argument("--key-color", default="auto",
                        help="Backdrop for --key chroma: auto (estimated magenta), magenta, green, blue or #rrggbb.")
    parser.add_argument("--key-quality", choices=forge_matte.KEY_QUALITIES, default="auto",
                        help="Keyer for --key chroma: auto (soft edges; hard binary alpha with --pixel-art), soft, "
                             "hard (legacy magenta keyer) or dominance.")
    parser.add_argument("--pixel-art", action="store_true",
                        help="Pixel art: --key-quality auto keeps binary alpha.")
    parser.add_argument("--slice", choices=SLICE_MODES, default="grid",
                        help="grid (default): plain cuts after the spill check; ownership: keep every subject "
                             "component whole in the cell that owns most of it (shared padded canvas).")
    parser.add_argument("--allow-spill", action="store_true",
                        help="Cut a sheet even when subject components cross their cell (recorded as a warning).")
    parser.add_argument("--spill-threshold", type=int, default=SPILL_THRESHOLD,
                        help=f"Subject pixels for spill and ownership: alpha above this (default {SPILL_THRESHOLD}).")
    parser.add_argument("--spill-min-area", type=int, default=SPILL_MIN_AREA,
                        help=f"Smaller components are specks, never a spill (default {SPILL_MIN_AREA} px).")
    parser.add_argument("--attach-radius", type=int, default=ATTACH_RADIUS,
                        help=f"Ownership slicing: faint pixels within this many px join their subject "
                             f"(default {ATTACH_RADIUS}).")
    parser.add_argument("--loop-overlap", type=int, default=0, metavar="K",
                        help="Crossfade the last K played frames into the first K (smoothstep); ambient loops "
                             "only, needs --ambient. The loop gets K fewer positions.")
    parser.add_argument("--ambient", action="store_true",
                        help="Declare an ambient loop (water, fire, mist, foliage), never a character.")
    parser.add_argument("--strict", action="store_true",
                        help="Fail and publish nothing when any QA check warns.")
    return parser


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = assemble(args)
    final = output_path(args.output_dir)
    summary = {"status": "ok", "output": str(final), "metadata": str(final / "animation.json"),
               "frames": len(result["frames"]), "sequence": len(result["sequence"]),
               "total_duration_ms": result["total_duration_ms"], "qa": result["qa"]["status"]}
    print(json.dumps(summary, ensure_ascii=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry (D26, D27): usage errors exit 2 (argparse); every other failure prints one
    ``error: ...`` line and exits 1 (an unexpected one as ``error: internal error (...)``)."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
