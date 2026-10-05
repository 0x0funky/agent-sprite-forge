#!/usr/bin/env python3
"""Bake periodic ambient motion into frames of a static plate (codeart2d; no image model).

Effects live in feathered polygons: ripple (rows or columns slide, water), shimmer
(heat haze), sway (displacement growing towards the top of the region, foliage)
and glow (a pulsing colour blend). Every effect completes a whole number of
cycles in the loop, and phases are computed from integer frame arithmetic, so
frame N equals frame 0 exactly (the loop has an exact period). The feather lies
inside each polygon and protected regions are forced to zero, so every pixel
outside the effect polygons is bit-identical to the plate in every frame.

Inputs: a plate PNG and an effects spec, either codeart2d.plate_effects.v1 or a
generate2dmap.stage.v1 document (its effects and protectedRegions, in UV).
Outputs (staged, published only when complete): frames/frame_000000.png ...
(0-based), mask.png (8-bit motion mask), ambient-loop.json (timing, effects,
hashes and provenance), ambient-qa.json (QA envelope) and, with --preview,
review.png and preview.webp. No codeart-meta.json is written: the motion is
code-drawn but the plate's pixels usually are not (--plate-art-source records
where they came from).

Run from the project root:
  python "<skill-dir>/scripts/ambient_bake.py" --plate art/harbor-plate.png --spec plate-effects.json --output-dir out/harbor-ambient-v1 --preview --strict-qc
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

try:
    import numpy as np
    from PIL import Image, UnidentifiedImageError, features
except ImportError as _missing:  # an environment problem: report it without a traceback
    sys.stderr.write(f"error: missing Python module {_missing.name}; install with: "
                     "python -m pip install numpy Pillow scipy\n")
    raise SystemExit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codeart_core  # noqa: E402
import forge_core  # noqa: E402


TOOL = "ambient_bake"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # D29
SPEC_SCHEMA = "codeart2d.plate_effects.v1"
STAGE_SCHEMA = "generate2dmap.stage.v1"
LOOP_SCHEMA = "codeart2d.ambient_loop.v1"
KINDS = ("ripple", "shimmer", "sway", "glow")
MAX_PERIOD_MS = 60_000
MAX_FRAMES = 600
PREVIEW_MAX_WIDTH = 960  # review.png and preview.webp use frames subsampled to at most this width


class AmbientError(ValueError):
    """The effects spec, the plate or a parameter is invalid."""


class AmbientQAError(ValueError):
    """Strict QC failed; nothing is published."""


# ----------------------------------------------------------------------------- helpers

def _num(value: float) -> int | float:
    rounded = round(float(value), 4)
    return int(rounded) if rounded == int(rounded) else rounded


def _mapping(value: Any, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise AmbientError(f"{label} must be a JSON object")
    return value


def _finite(value: Any, label: str, *, minimum: float | None = None, maximum: float | None = None,
            positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AmbientError(f"{label} must be a finite number")
    if positive and value <= 0:
        raise AmbientError(f"{label} must be greater than 0")
    if minimum is not None and value < minimum:
        raise AmbientError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise AmbientError(f"{label} must be at most {maximum}")
    return float(value)


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AmbientError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise AmbientError(f"{label} must be at least {minimum}")
    return int(value)


def _file_ref(path: Path, base: Path) -> dict:
    """forge_core.file_ref: a manifest-relative POSIX path, or the file name across drives (D30)."""
    return forge_core.file_ref(path, base)


def _inside_distance(inside: np.ndarray, cap: float) -> np.ndarray:
    """Euclidean distance (px) from each True pixel of `inside` to the nearest False pixel, exact up to
    `cap` and infinity beyond it (and everywhere when there is no False pixel); 0 on False pixels.
    The array border is not an edge, so a region that runs off the image keeps moving up to the edge.
    codeart_core.distance_field of the complement: the skill's one Euclidean capped distance."""
    return codeart_core.distance_field(~inside, cap)


# ----------------------------------------------------------------------------- spec

@dataclass
class Effect:
    id: str
    kind: str
    polygon: list[tuple[float, float]]
    period_ms: int
    amplitude: float
    wavelength: float
    axis: str
    feather: float
    opacity: float
    colour: tuple[int, int, int, int] | None
    cycles: int = 0
    weight: np.ndarray | None = None  # feather weight in the effect box
    box: tuple[int, int, int, int] = (0, 0, 0, 0)  # x0, y0, x1, y1 (half-open)
    top: float = 0.0
    bottom: float = 0.0
    # the pixels with nonzero weight: centres, weights and their positions in the loop's moving set
    xs: np.ndarray | None = None
    ys: np.ndarray | None = None
    weights: np.ndarray | None = None
    slots: np.ndarray | None = None


@dataclass
class Loop:
    spec_path: Path
    spec_bytes: bytes
    plate_path: Path
    plate: np.ndarray
    effects: list[Effect]
    protected: list[dict]
    period_ms: int
    frames: int
    sampling: str
    source_schema: str
    moving: np.ndarray | None = None  # flat indices of every pixel some effect may change, sorted


def _period_ms(value: Any, label: str) -> int:
    seconds = _finite(value, label, positive=True)
    milliseconds = round(seconds * 1000.0)
    if abs(seconds * 1000.0 - milliseconds) > 1e-6 or milliseconds < 1:
        raise AmbientError(f"{label} must be a whole number of milliseconds (got {seconds} s)")
    return int(milliseconds)


def _polygon(value: Any, label: str, scale: tuple[float, float]) -> list[tuple[float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        raise AmbientError(f"{label} must list at least three [x, y] points")
    points = []
    for index, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise AmbientError(f"{label}[{index}] must be [x, y]")
        points.append((_finite(point[0], label) * scale[0], _finite(point[1], label) * scale[1]))
    return points


def load_loop(spec_path: Path, plate_override: Path | None, frames_override: int | None,
              fps_override: float | None) -> Loop:
    try:
        raw = spec_path.read_bytes()
    except FileNotFoundError:
        raise AmbientError(f"spec not found: {spec_path}") from None
    try:
        data = _mapping(forge_core.parse_json(raw), "spec")  # UTF-8 with an optional BOM (D28)
    except AmbientError:
        raise
    except ValueError as error:  # JSONDecodeError and UnicodeDecodeError
        raise AmbientError(f"spec {spec_path.name} is not valid UTF-8 JSON: {error}") from None
    schema = data.get("schema")
    if schema not in (SPEC_SCHEMA, STAGE_SCHEMA):
        raise AmbientError(f"spec schema must be {SPEC_SCHEMA!r} or {STAGE_SCHEMA!r} (got {schema!r})")
    plate_value = plate_override or (spec_path.resolve().parent / data["plate"] if isinstance(data.get("plate"), str)
                                     else None)
    if plate_value is None:
        raise AmbientError("give the plate with --plate (or a plate path in the spec)")
    plate_path = Path(plate_value).resolve()
    try:
        image, _ = forge_core.load_rgba(plate_path)
    except FileNotFoundError:
        raise AmbientError(f"plate not found: {plate_path}") from None
    except UnidentifiedImageError:
        raise AmbientError(f"plate is not a readable image: {plate_path}") from None
    plate = np.asarray(image).copy()
    height, width = plate.shape[:2]
    if schema == STAGE_SCHEMA:
        units, raw_effects, raw_protected = "uv", data.get("effects") or [], data.get("protectedRegions") or []
        source = data.get("sourceSize")
        if isinstance(source, list) and len(source) == 2 and all(isinstance(v, int) and v > 0 for v in source):
            if abs(source[0] / source[1] - width / height) > 0.01 * (width / height):
                raise AmbientError(f"plate is {width}x{height} but the stage was reviewed at "
                                   f"{source[0]}x{source[1]}; use the plate the stage describes")
        sampling, period_value, frames_value, fps_value = "bilinear", None, None, None
    else:
        units = data.get("units", "uv")
        raw_effects, raw_protected = data.get("effects"), data.get("protected") or []
        sampling = data.get("sampling", "bilinear")
        period_value, frames_value, fps_value = data.get("period_ms"), data.get("frames"), data.get("fps")
    if units not in ("uv", "px"):
        raise AmbientError("units must be uv or px")
    if sampling not in ("bilinear", "nearest"):
        raise AmbientError("sampling must be bilinear or nearest")
    scale = (float(width), float(height)) if units == "uv" else (1.0, 1.0)
    if not isinstance(raw_effects, list) or not raw_effects:
        raise AmbientError("effects must be a nonempty list")
    effects, ids = [], set()
    loop_hint = _integer(period_value, "period_ms", minimum=1) if period_value is not None else None
    for index, item in enumerate(raw_effects):
        label = f"effects[{index}]"
        item = _mapping(item, label)
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise AmbientError(f"{label}.id must be a unique nonempty string")
        ids.add(identity)
        kind = item.get("kind")
        if kind not in KINDS:
            raise AmbientError(f"{label}.kind must be one of {', '.join(KINDS)}")
        if "period" in item:
            period = _period_ms(item["period"], f"{label}.period")
        elif loop_hint is not None:
            period = loop_hint
        else:
            raise AmbientError(f"{label}.period (seconds) is required when the spec has no period_ms")
        axis = item.get("axis", "y" if kind == "shimmer" else "x")
        if axis not in ("x", "y"):
            raise AmbientError(f"{label}.axis must be x or y")
        if kind == "glow":
            amplitude = _finite(item.get("amplitude", 0.35), f"{label}.amplitude", minimum=0, maximum=1)
            try:
                colour = codeart_core.hex_to_rgba(item.get("color", "#ffe9a8"))
            except codeart_core.CodeArtError as error:
                raise AmbientError(f"{label}.color: {error}") from None
        else:
            amplitude = _finite(item.get("amplitude", 1.5), f"{label}.amplitude", minimum=0, maximum=64)
            colour = None
        effects.append(Effect(
            identity, kind, _polygon(item.get("polygon"), f"{label}.polygon", scale), period, amplitude,
            _finite(item.get("wavelength", 48), f"{label}.wavelength", positive=True), axis,
            _finite(item.get("feather", 8), f"{label}.feather", minimum=0),
            _finite(item.get("opacity", 1.0), f"{label}.opacity", minimum=0, maximum=1), colour))
    protected = []
    for index, item in enumerate(raw_protected):
        label = f"protected[{index}]"
        item = _mapping(item, label)
        if "polygon" in item:
            protected.append({"id": str(item.get("id", index)), "polygon": _polygon(item["polygon"], label, scale)})
        elif "box" in item:
            box = item["box"]
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                raise AmbientError(f"{label}.box must be [x0, y0, x1, y1]")
            x0, y0, x1, y1 = (_finite(v, label) for v in box)
            protected.append({"id": str(item.get("id", index)), "polygon": [
                (x0 * scale[0], y0 * scale[1]), (x1 * scale[0], y0 * scale[1]),
                (x1 * scale[0], y1 * scale[1]), (x0 * scale[0], y1 * scale[1])]})
        else:
            raise AmbientError(f"{label} needs a polygon or a box")
    period_ms = loop_hint if loop_hint is not None else math.lcm(*(effect.period_ms for effect in effects))
    if period_ms > MAX_PERIOD_MS:
        raise AmbientError(f"the loop would last {period_ms} ms (the common multiple of the effect periods); keep "
                           f"it at most {MAX_PERIOD_MS} ms by choosing periods that divide one another")
    for effect in effects:
        if period_ms % effect.period_ms:
            raise AmbientError(f"effect {effect.id}: period {effect.period_ms} ms does not divide the loop "
                               f"period {period_ms} ms, so the loop could not repeat exactly")
        effect.cycles = period_ms // effect.period_ms
    if frames_override is not None:
        frames = frames_override
    elif fps_override is not None or (fps_value is not None and frames_value is None):
        fps = fps_override if fps_override is not None else _finite(fps_value, "fps", positive=True)
        frames = int(math.floor(period_ms * fps / 1000.0 + 0.5))
    elif frames_value is not None:
        frames = _integer(frames_value, "frames", minimum=2)
    else:
        frames = int(math.floor(period_ms * 12 / 1000.0 + 0.5))
    if not 2 <= frames <= MAX_FRAMES:
        raise AmbientError(f"the loop needs 2 to {MAX_FRAMES} frames (got {frames})")
    if frames > period_ms:
        raise AmbientError(f"{frames} frames cannot share {period_ms} ms with at least 1 ms each")
    for effect in effects:
        if effect.cycles * 2 > frames:
            raise AmbientError(f"effect {effect.id}: {effect.cycles} cycles need at least {effect.cycles * 2} frames "
                               "to be seen (Nyquist); add frames or lengthen its period")
    return Loop(spec_path, raw, plate_path, plate, effects, protected, period_ms, frames, sampling, schema)


# ----------------------------------------------------------------------------- effects

def prepare(loop: Loop) -> np.ndarray:
    """Feather weights per effect (inside the polygon, minus protected regions) and their union."""
    height, width = loop.plate.shape[:2]
    blocked = np.zeros((height, width), bool)
    ys_all, xs_all = np.mgrid[0:height, 0:width] + 0.5
    for region in loop.protected:
        blocked |= codeart_core.inside_polygon(xs_all, ys_all, region["polygon"])
    union = np.zeros((height, width), np.float64)
    for effect in loop.effects:
        xs = [p[0] for p in effect.polygon]
        ys = [p[1] for p in effect.polygon]
        margin = int(math.ceil(effect.feather)) + 1  # room for outside pixels around the polygon
        x0, y0 = max(0, int(math.floor(min(xs))) - margin), max(0, int(math.floor(min(ys))) - margin)
        x1, y1 = min(width, int(math.ceil(max(xs))) + margin), min(height, int(math.ceil(max(ys))) + margin)
        if max(xs) <= 0 or max(ys) <= 0 or min(xs) >= width or min(ys) >= height or x0 >= x1 or y0 >= y1:
            raise AmbientError(f"effect {effect.id}: its polygon lies outside the {width}x{height} plate")
        inside = codeart_core.inside_polygon(xs_all[y0:y1, x0:x1], ys_all[y0:y1, x0:x1], effect.polygon)
        if effect.feather > 0:
            t = np.clip(_inside_distance(inside, effect.feather) / effect.feather, 0.0, 1.0)
            weight = t * t * (3.0 - 2.0 * t)
        else:
            weight = inside.astype(np.float64)
        weight[blocked[y0:y1, x0:x1]] = 0.0
        weight *= effect.opacity
        if not (weight > 0).any():
            raise AmbientError(f"effect {effect.id}: no pixel of its polygon is free to move (empty, protected "
                               "or narrower than the feather)")
        effect.weight, effect.box = weight, (x0, y0, x1, y1)
        effect.top, effect.bottom = min(ys), max(ys)
        union[y0:y1, x0:x1] = np.maximum(union[y0:y1, x0:x1], weight)
        rows, cols = np.nonzero(weight > 0)
        effect.xs, effect.ys = cols + x0 + 0.5, rows + y0 + 0.5
        effect.weights = weight[rows, cols].astype(np.float32)[:, None]
        effect.slots = (rows + y0) * width + (cols + x0)  # flat indices until the moving set is known
    loop.moving = np.unique(np.concatenate([effect.slots for effect in loop.effects]))
    for effect in loop.effects:
        effect.slots = np.searchsorted(loop.moving, effect.slots)
    return union


def displacement(effect: Effect, phase: float, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Displacement in px along the effect axis; every temporal term is an integer multiple of the
    phase, so the field repeats exactly after each of the effect's cycles."""
    k = 2.0 * math.pi / effect.wavelength
    amplitude = effect.amplitude
    along = ys if effect.axis == "x" else xs  # ripples slide rows (axis x) or columns (axis y)
    if effect.kind == "ripple":
        return amplitude * (0.72 * np.sin(k * along - phase) + 0.28 * np.sin(0.57 * k * along - 2.0 * phase + 1.3))
    if effect.kind == "shimmer":
        across = xs if effect.axis == "x" else ys
        return amplitude * (0.6 * np.sin(k * along - phase)
                            + 0.4 * np.sin(k * (across / 1.7 + along / 0.6) - 3.0 * phase))
    height = max(1e-6, effect.bottom - effect.top)  # sway: anchored at the bottom of the region
    lift = np.clip((effect.bottom - ys) / height, 0.0, 1.0)
    across = xs if effect.axis == "x" else ys
    return amplitude * lift * (0.8 * np.sin(phase + k * across) + 0.2 * np.sin(2.0 * phase + 2.0 * k * across + 0.7))


def _premultiplied(rgba: np.ndarray) -> np.ndarray:
    """Premultiplied float32 RGBA (0-255); float32 keeps every 8-bit value and product exact enough
    that untouched or unshifted pixels round back to their own bytes."""
    out = rgba.astype(np.float32)
    out[..., :3] *= out[..., 3:] / np.float32(255.0)
    return out


def _sample(premultiplied: np.ndarray, sx: np.ndarray, sy: np.ndarray, mode: str) -> np.ndarray:
    """Sample a premultiplied plate at pixel-centre coordinates (clamped to the image); (n, 4)."""
    height, width = premultiplied.shape[:2]
    flat = premultiplied.reshape(-1, 4)
    fx, fy = np.clip(sx - 0.5, 0, width - 1), np.clip(sy - 0.5, 0, height - 1)
    if mode == "nearest":
        return flat.take(np.floor(fy + 0.5).astype(np.int64) * width + np.floor(fx + 0.5).astype(np.int64), axis=0)
    x0, y0 = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
    step_x = (x0 < width - 1).astype(np.int64)
    step_y = np.where(y0 < height - 1, width, 0)
    tx, ty = (fx - x0).astype(np.float32)[:, None], (fy - y0).astype(np.float32)[:, None]
    base = y0 * width + x0
    top = flat.take(base, axis=0)
    top += (flat.take(base + step_x, axis=0) - top) * tx
    bottom = flat.take(base + step_y, axis=0)
    bottom += (flat.take(base + step_y + step_x, axis=0) - bottom) * tx
    top += (bottom - top) * ty
    return top


def render_frame(loop: Loop, index: int, premultiplied: np.ndarray) -> np.ndarray:
    """Frame `index` (any integer; index N gives the same phases as index 0).

    Effects are applied in spec order to the pixels they may move; each samples the plate itself
    and blends over the frame so far by its feather weight. Every other pixel keeps the plate's
    exact bytes."""
    flat = premultiplied.reshape(-1, 4)
    work = flat[loop.moving]
    for effect in loop.effects:
        phase = 2.0 * math.pi * ((index * effect.cycles) % loop.frames) / loop.frames
        current = work[effect.slots]
        if effect.kind == "glow":
            pulse = np.float32(effect.amplitude * (0.5 + 0.5 * math.sin(phase)))
            colour = np.array(effect.colour, np.float32)
            colour[:3] *= colour[3] / np.float32(255.0)
            target = current + (colour - current) * pulse
        else:
            shift = displacement(effect, phase, effect.xs, effect.ys)
            sx, sy = (effect.xs - shift, effect.ys) if effect.axis == "x" else (effect.xs, effect.ys - shift)
            target = _sample(premultiplied, sx, sy, loop.sampling)
        current += (target - current) * effect.weights
        work[effect.slots] = current
    alpha = work[:, 3:]
    np.divide(work[:, :3] * np.float32(255.0), alpha, out=work[:, :3], where=alpha > 0)
    result = np.floor(work + np.float32(0.5)).clip(0, 255).astype(np.uint8)
    result[result[:, 3] == 0, :3] = 0
    frame = loop.plate.copy()
    frame.reshape(-1, 4)[loop.moving] = result
    return frame


def review(frames: Sequence[np.ndarray], mask: np.ndarray, count: int = 4) -> np.ndarray:
    """A strip of `count` evenly spaced frames plus the motion mask, each scaled to fit 640 px wide."""
    factor = max(1, int(math.ceil(frames[0].shape[1] / 640)))
    picks = [frames[(i * len(frames)) // count] for i in range(count)]
    mask_rgba = np.dstack([mask, mask, mask, np.full(mask.shape, 255, np.uint8)])
    tiles = [image[::factor, ::factor] for image in picks + [mask_rgba]]
    th, tw = tiles[0].shape[:2]
    sheet = np.zeros((th + 4, len(tiles) * (tw + 4) + 4, 4), np.uint8)
    sheet[..., :3], sheet[..., 3] = 40, 255
    for number, tile in enumerate(tiles):
        x = 4 + number * (tw + 4)
        sheet[2:2 + tile.shape[0], x:x + tile.shape[1]] = tile
    return sheet


# ----------------------------------------------------------------------------- build

def build(args: argparse.Namespace) -> dict:
    loop = load_loop(Path(args.spec), Path(args.plate) if args.plate else None, args.frames, args.fps)
    union = prepare(loop)
    premultiplied = _premultiplied(loop.plate)
    outside = union <= 0
    durations = forge_core.frame_durations(loop.period_ms, loop.frames)
    preview_step = max(1, int(math.ceil(loop.plate.shape[1] / PREVIEW_MAX_WIDTH)))
    with forge_core.staged_output(Path(args.output_dir)) as stage:
        (stage / "frames").mkdir()
        outputs: list[Path] = []
        frame_refs = []
        thumbnails: list[np.ndarray] = []
        first: np.ndarray | None = None
        mismatch_outside = 0
        moved = {effect.id: False for effect in loop.effects}
        for index in range(loop.frames):  # frames stream to disk; only the first stays in memory
            frame = render_frame(loop, index, premultiplied)
            mismatch_outside += int(np.count_nonzero(np.any(frame != loop.plate, axis=-1) & outside))
            if first is None:
                first = frame
            for effect in loop.effects:
                x0, y0, x1, y1 = effect.box
                region = effect.weight > 0
                moved[effect.id] |= bool(np.any(frame[y0:y1, x0:x1][region] != first[y0:y1, x0:x1][region]))
            path = stage / "frames" / f"frame_{index:06d}.png"
            forge_core.save_png(frame, path)
            outputs.append(path)
            frame_refs.append({"path": f"frames/{path.name}", "sha256": forge_core.sha256_file(path)})
            if args.preview:
                thumbnails.append(frame[::preview_step, ::preview_step].copy())
        period_mismatch = int(np.count_nonzero(np.any(render_frame(loop, loop.frames, premultiplied) != first,
                                                      axis=-1)))
        still = [identity for identity, flag in moved.items() if not flag]
        # the seam inside the motion mask: each saved frame's moving pixels as a (n, 1, 4) image
        seam = forge_core.seam_report(
            np.asarray(forge_core.load_rgba(stage / ref["path"])[0]).reshape(-1, 4)[loop.moving][:, None, :]
            for ref in frame_refs)
        checks = [
            {"id": "exact_period", "status": "pass" if period_mismatch == 0 else "fail", "value": period_mismatch,
             "threshold": 0},
            {"id": "outside_unchanged", "status": "pass" if mismatch_outside == 0 else "fail",
             "value": mismatch_outside, "threshold": 0},
            {"id": "motion_present", "status": "pass" if not still else "fail", "value": still, "threshold": []},
        ]
        status = "fail" if any(check["status"] == "fail" for check in checks) else "pass"
        if args.strict_qc and status == "fail":  # raising inside the stage publishes nothing
            failed = "; ".join(f"{c['id']}={c['value']}" for c in checks if c["status"] == "fail")
            raise AmbientQAError(f"ambient QA failed ({failed}); nothing was published")
        mask_image = np.floor(union * 255.0 + 0.5).clip(0, 255).astype(np.uint8)
        forge_core.save_png(mask_image, stage / "mask.png")
        outputs.append(stage / "mask.png")
        if args.preview:
            codeart_core.save_png(review(thumbnails, mask_image[::preview_step, ::preview_step]), stage / "review.png")
            outputs.append(stage / "review.png")
            if features.check("webp"):
                pictures = [Image.fromarray(frame) for frame in thumbnails]
                pictures[0].save(stage / "preview.webp", save_all=True, append_images=pictures[1:],
                                 duration=durations, loop=0, lossless=True)
                outputs.append(stage / "preview.webp")
        plate_ref = _file_ref(loop.plate_path, stage)
        document = {
            "schema": LOOP_SCHEMA, "plate": plate_ref, "size": [int(loop.plate.shape[1]), int(loop.plate.shape[0])],
            "period_ms": loop.period_ms, "frame_count": loop.frames, "durations_ms": durations,
            "fps": forge_core.rational_fps(loop.frames, loop.period_ms),
            "loop": {"policy": "cycle", "exact": period_mismatch == 0}, "sampling": loop.sampling,
            "frames": frame_refs, "mask": {"path": "mask.png", "sha256": forge_core.sha256_file(stage / "mask.png")},
            "effects": [{"id": e.id, "kind": e.kind, "cycles": e.cycles, "period_ms": e.period_ms,
                         "amplitude": _num(e.amplitude), "wavelength": _num(e.wavelength), "axis": e.axis,
                         "feather": _num(e.feather), "opacity": _num(e.opacity),
                         "polygon": [[_num(x), _num(y)] for x, y in e.polygon], "box": list(e.box),
                         **({"color": codeart_core.rgba_to_hex(e.colour)} if e.colour else {})}
                        for e in loop.effects],
            "protected": [{"id": region["id"], "polygon": [[_num(x), _num(y)] for x, y in region["polygon"]]}
                          for region in loop.protected],
            "source_schema": loop.source_schema,
            "art_source": "code" if args.plate_art_source == "code" else "mixed",
            "plate_art_source": args.plate_art_source, "motion_source": "code",
            "disclosure": ("motion code-drawn, no image model; plate pixels from "
                           + ("code" if args.plate_art_source == "code" else args.plate_art_source.replace("_", " "))),
            "provenance": {"tool": TOOL, "version": TOOL_VERSION,
                           "params": {"frames": loop.frames, "period_ms": loop.period_ms, "sampling": loop.sampling,
                                      "strict_qc": bool(args.strict_qc), "preview": bool(args.preview)},
                           "inputs": [_file_ref(loop.spec_path.resolve(), stage), plate_ref]},
        }
        forge_core.write_json(stage / "ambient-loop.json", document)
        outputs.insert(0, stage / "ambient-loop.json")
        envelope = {
            "status": status,
            "method": ("ambient_bake: integer-cycle phases ((i * cycles) mod N) / N, frame N re-rendered and compared "
                       "with frame 0; every frame compared with the plate outside the union of the effect weights; "
                       "per effect, any change inside its region across the loop; forge_core.seam_report inside "
                       "the motion mask as a diagnostic"),
            "notProven": [
                "That the motion looks natural at game size: open review.png or preview.webp.",
                "Encoded video: the frames are lossless PNG; a video encode (scene_motion in generate2dmap) needs "
                "its own decoded-file QA.",
                "Displacement samples the plate itself: content near a region's edge may be pulled in from outside "
                "the region (the feather hides the seam; it does not prevent the borrowing).",
            ],
            "checks": checks,
            "inputs": [_file_ref(loop.spec_path.resolve(), stage), plate_ref],
            "outputs": [_file_ref(path, stage) for path in outputs],
            "tool": {"name": TOOL, "version": TOOL_VERSION},
            "seam": seam,
        }
        forge_core.write_json(stage / "ambient-qa.json", envelope)
    final = Path(args.output_dir).resolve()
    return {"status": status, "output": final.as_posix(), "metadata": (final / "ambient-loop.json").as_posix(),
            "qa": (final / "ambient-qa.json").as_posix(), "frames": loop.frames, "period_ms": loop.period_ms,
            "fps": forge_core.rational_fps(loop.frames, loop.period_ms),
            "failed_checks": [check["id"] for check in checks if check["status"] == "fail"]}


def _frames_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 2:
        raise argparse.ArgumentTypeError("must be at least 2")
    return value


def _fps_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bake periodic ambient motion (ripple, shimmer, sway, glow) inside feathered polygons of a "
                    "static plate into an exactly repeating PNG frame loop. Pixels outside the polygons never "
                    "change. No image model is used.",
        epilog="Example: python ambient_bake.py --plate plate.png --spec plate-effects.json --output-dir "
               "out/plate-ambient-v1 --preview --strict-qc")
    parser.add_argument("--plate", help="the static plate PNG (default: the spec's plate path)")
    parser.add_argument("--spec", required=True,
                        help="codeart2d.plate_effects.v1 JSON, or a generate2dmap.stage.v1 document with effects")
    parser.add_argument("--output-dir", required=True, help="new folder to create; an existing path is refused")
    parser.add_argument("--frames", type=_frames_arg, help="frames in the loop, at least 2 (overrides the spec)")
    parser.add_argument("--fps", type=_fps_arg, help="frames per second; frames = period_ms * fps / 1000, rounded")
    parser.add_argument("--preview", action="store_true", help="also write review.png and preview.webp")
    parser.add_argument("--plate-art-source", default="existing",
                        choices=("code", "host_image", "api", "existing", "video", "mixed"),
                        help="where the plate's pixels came from, recorded in ambient-loop.json (default: existing)")
    parser.add_argument("--strict-qc", action="store_true",
                        help="exit 1 and publish nothing when a QA check fails (inexact period, a changed pixel "
                             "outside the polygons, an effect that moves nothing)")
    return parser.parse_args(argv)


def _run(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = parse_args(argv)  # usage errors (a bad --frames or --fps too) exit 2 with argparse's message (D26)
    if os.path.lexists(args.output_dir):
        print(f"error: output directory already exists: {forge_core.ascii_text(str(args.output_dir))}",
              file=sys.stderr)
        return 1
    try:
        summary = build(args)
    except (AmbientError, AmbientQAError, codeart_core.CodeArtError, OSError, ValueError) as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    if summary["status"] == "fail":  # D26: published for inspection, and the failed QA still exits 1
        print(forge_core.ascii_text(f"error: published with QA status fail: {', '.join(summary['failed_checks'])} "
                                    f"(see {summary['qa']})"), file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI behind forge_core.run_cli (D26/D27): usage errors exit 2 from argparse, expected errors print
    one "error: ..." line and exit 1, and anything unexpected is one "error: internal error (Type: message)"
    line with exit 1, also when main() is called in-process."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
