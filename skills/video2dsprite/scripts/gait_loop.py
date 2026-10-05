#!/usr/bin/env python3
"""Choose a loop from keyed RGBA frames: gait cycles (walk, run), idle and hover loops.

  select          gait: harmonic least-squares period, half-period guard, per-frame
                  alternation, unified seam score and confidence. idle/hover: pose +
                  0.5 x velocity closure with non-maximum suppression and a pingpong
                  fallback. Both: drift-aware usable range and near-duplicate holds.
                  Writes selection.json (forge-frame-selection/v2), loop-report.json
                  and review aids (3x loop GIF, seam close-up, onion skin, timeline).
  measure-stride  stride per frame from ground-band matching against the root motion,
                  and the most rest-like entry frame (stance-aligned silhouette XOR).

Frame indices are 0-based positions in the lexically sorted *.png list (the order
animation_review.py uses); every interval is [start, endExclusive). Inputs are never
modified. Pixel evidence cannot certify foot contact, identity or art, so every
selection stays 'selected-needs-visual-review'.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import forge_core  # noqa: E402  (this skill's vendored copy)

GAIT_LOOP_VERSION = "1"
SELECTION_V1 = "forge-frame-selection/v1"
SELECTION_V2 = "forge-frame-selection/v2"
SELECTED_STATUS = "selected-needs-visual-review"
LOOP_POLICIES = ("cycle", "pingpong", "oneshot")
KINDS = ("gait", "idle", "hover")
STATES = ("run", "walk")
MAX_FRAMES = 600

# Gait period analysis, ported from the report v2 prototype loop_select.py. The thresholds
# were set on one real clip and eight synthetic runners (references/animation-review.md).
STATE_STRIDE_SECONDS = {"run": (0.30, 1.20), "walk": (0.50, 1.60)}
GAIT_FLOOR_SECONDS = {"run": 0.35, "walk": 0.60}  # a "stride" shorter than this is one step
DEPTH_TOL = 0.15             # smallest lag-profile minimum within 15 % of the deepest = repeat period
ABS_TOL_STEPS = 0.05         # ... or within 0.05 adjacent steps of it (near-exact repeats)
PERIODICITY_MIN = 0.15       # the repeat must dip >= 15 % below the profile mean
HARMONIC_TOL = 0.25          # a k*T minimum within 25 % of P(T) joins the least-squares fit
STEADY_TOL = 0.15            # local period within 15 % of T = steady gait
TWO_STEP_FRACTION = 0.60     # paired test: mirror > same on most steady frames
TWO_STEP_RATIO = 1.25        # ... and median mirror/same >= 1.25
LOCAL_TWO_STEP_MIN = 1.10    # per-frame mirror/same below this = legs not alternating there
STEP_PROMINENCE_MIN = 0.20   # silhouette dip at T/2 must sit >= 0.2 steps below its neighbouring peaks
RATIO_EPS = 0.10             # distances below 10 % of a median step are noise
CONTEXT_RADIUS = 2
LEGS_FRACTION = 0.45
GLOBAL_SIDE = {"gait": 96, "idle": 160, "hover": 160}
LEGS_WIDTH = 160

# Window classification.
SEAM_MAX_STEPS = 1.0         # seam (context repeat) error above one ordinary step: rejected
WRAP_MAX_FACTOR = 2.0        # wrap step outside [1/2, 2] x the source step: stall or snap
DRIFT_NOTE_PCT = 1.5         # root drift per loop worth a note (percent of body height)
CLOSURE_MAX_STEPS = 1.0      # idle/hover: pose + 0.5 x velocity closure above one step does not loop
AMPLITUDE_MIN_STEPS = 2.0    # idle/hover: a window must move at least two ordinary steps
TOP_WINDOWS = 5

# Holds (near-duplicate frames) and drift.
DEDUPE_CAP = 1.2             # mean |step| (0-255) at or below which a frame may repeat its predecessor
HOLD_RELATIVE = 0.4          # ... and below 0.4 x its neighbours' steps and 0.4 x the clip's p75 step
DRIFT_TOLERANCE = 0.03       # idle/hover range ends when scale or position drifts 3 % (of body height)
GAIT_DRIFT_TOLERANCE = 0.05  # gait clips change shape every stride: scale only, smoothed per stride
DRIFT_SMOOTH_SECONDS = 2.0   # idle/hover drift signals: running median over 2 s

# Kept reduced copies (pose features without a second decode).
REDUCED_SIDE = 480
REDUCED_BUDGET_BYTES = 256 * 1024 * 1024

# measure-stride.
BAND_FRACTION = 0.04         # ground band height as a fraction of the body height
BAND_MATCH_MAX = 0.30        # band mismatch / band mass above this: the pair is not a rigid ground shift
MIN_STRIDE = (0.1, 0.002)    # a gait moves the ground at least max(0.1 px, 0.2 % of body height) per frame

REVIEW_FIRST = ("Play the 3x loop GIF at source fps and inspect the seam pair; pixel metrics cannot certify "
                "foot contact, hand or prop identity drift or the art itself.")
NOT_PROVEN = [
    "foot contact, sliding and ground penetration",
    "hand, hair, scarf or prop identity drift between frames",
    "art quality, anatomy and facing",
    "left/right alternation beyond the pixel evidence reported here",
]


# --------------------------------------------------------------------------- small helpers

def round_half_up(value: float | Fraction) -> int:
    """floor(value + 1/2), the rounding forge_core uses everywhere (never banker's rounding)."""
    if isinstance(value, Fraction):
        return int(math.floor(value + Fraction(1, 2)))
    return int(math.floor(value + 0.5))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def parse_fps(text: Any) -> Fraction:
    """Frame rate from a number or an exact ``N/D`` string (``24``, ``12.5``, ``24000/1001``)."""
    if isinstance(text, bool):
        raise ValueError("fps must be a positive number or N/D, not a boolean")
    try:
        if isinstance(text, (int, Fraction)):
            value = Fraction(text)
        elif isinstance(text, float):
            if not math.isfinite(text):
                raise ValueError
            value = Fraction(str(text))
        else:
            raw = str(text).strip()
            if "/" in raw:
                numerator, denominator = raw.split("/", 1)
                value = Fraction(int(numerator), int(denominator))
            else:
                value = Fraction(raw)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"fps must be a positive number or N/D, got {text!r}") from None
    if not 0 < value <= 240:
        raise ValueError(f"fps must be in (0, 240], got {text!r}")
    return value


def fps_text(fps: Fraction) -> str:
    """Exact rational fps as selections store it (common.schema fpsValue)."""
    return f"{fps.numerator}/{fps.denominator}"


def parse_interval(text: str) -> tuple[int, int]:
    """``start:endExclusive`` (0-based) to a pair of ints."""
    start, sep, end = str(text).partition(":")
    try:
        if not sep:
            raise ValueError
        a, b = int(start), int(end)
    except ValueError:
        raise ValueError(f"interval must be start:endExclusive, got {text!r}") from None
    if not 0 <= a < b:
        raise ValueError(f"interval needs 0 <= start < endExclusive, got {text!r}")
    return a, b


def source_durations(count: int, fps: Fraction) -> list[int]:
    """Integer ms for ``count`` source frames at ``fps``; they sum to round(count * 1000 / fps)."""
    return forge_core.frame_durations(round_half_up(Fraction(1000 * count) / fps), count)


def kept_durations(start: int, end: int, kept: Sequence[int], fps: Fraction) -> list[int]:
    """Durations of the kept frames of [start, end) when held duplicates are dropped.

    Each kept frame lasts until the next kept frame, so the window keeps its exact source
    length; the edges are the forge_core.frame_durations edges of the whole window.
    """
    length = end - start
    total = round_half_up(Fraction(1000 * length) / fps)
    if total < length:
        raise ValueError(f"{length} frames at {fps_text(fps)} fps need at least 1 ms each")
    edges = [(2 * k * total + length) // (2 * length) for k in range(length + 1)]
    positions = [index - start for index in kept] + [length]
    return [edges[b] - edges[a] for a, b in zip(positions, positions[1:])]


def _ranges(values: Iterable[int]) -> str:
    """``[3, 4, 5, 9]`` -> ``"3-5, 9"`` for messages."""
    values = sorted(set(int(v) for v in values))
    parts, start = [], None
    for index, value in enumerate(values):
        if start is None:
            start = value
        if index + 1 == len(values) or values[index + 1] != value + 1:
            parts.append(str(start) if start == value else f"{start}-{value}")
            start = None
    return ", ".join(parts)


def jsonable(value: Any, digits: int = 6) -> Any:
    """numpy-free, NaN-free JSON values with floats rounded so reports are byte-stable."""
    if isinstance(value, dict):
        return {str(k): jsonable(v, digits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v, digits) for v in value]
    if isinstance(value, np.ndarray):
        return [jsonable(v, digits) for v in value.tolist()]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return round(number, digits) if math.isfinite(number) else None
    if isinstance(value, Fraction):
        return fps_text(value)
    return value


def _relative_or_name(path: Path, base: Path) -> str:
    """forge_core.portable_path, or only the name when there is no relative route (another
    drive): manifests never store absolute paths; hashes bind the content instead."""
    rel = forge_core.portable_path(path, base)
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        return Path(path).resolve().name
    return rel


def directory_ref(directory: Path, base: Path) -> str:
    """Manifest-relative POSIX path of ``directory`` seen from ``base`` (its name off-drive)."""
    return _relative_or_name(directory, base)


def file_ref(path: Path, base: Path, sha256: str | None = None) -> dict:
    """common.schema fileRef: manifest-relative path (the bare name off-drive) plus sha256."""
    return {"path": _relative_or_name(path, base), "sha256": sha256 or forge_core.sha256_file(path)}


def resolve_directory(value: str, selection_path: Path, frames_dir: Path) -> bool:
    """True when a selection's ``sourceDirectory`` names ``frames_dir``.

    v1 stores an absolute path; v2 a path relative to the selection file, or only the
    directory name when the frames sat on another drive (the hashes are the binding).
    """
    target = Path(frames_dir).resolve()
    candidate = Path(value)
    resolved = candidate if candidate.is_absolute() else Path(selection_path).resolve().parent / candidate
    if resolved.resolve() == target:
        return True
    return "/" not in value and "\\" not in value and value == target.name


def write_selection_files(stage: Path, documents: dict[str, Any]) -> None:
    for name, document in documents.items():
        forge_core.write_json(stage / name, jsonable(document))


# --------------------------------------------------------------------------- frames and features

def frame_paths(directory: Path, minimum: int = 2) -> list[Path]:
    """Sorted ``*.png`` files: the same list and order as animation_review.sources()."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"frames directory not found: {directory}")
    paths = sorted(directory.glob("*.png"))
    if len(paths) < minimum:
        raise ValueError(f"need at least {minimum} PNG frames in {directory}, found {len(paths)}")
    if len(paths) > MAX_FRAMES:
        raise ValueError(f"at most {MAX_FRAMES} frames are supported, found {len(paths)}")
    return paths


@dataclass
class Clip:
    """Per-frame hashes and body statistics (alpha > 32) of a frame directory.

    ``reduced`` optionally keeps every frame premultiplied ('RGBa') and reduced by the
    integer ``reduction`` factor, so the pose features need no second decode.
    """
    directory: Path
    paths: list[Path]
    hashes: list[str]
    size: tuple[int, int]
    fps: Fraction
    boxes: np.ndarray
    cx: np.ndarray
    ground: np.ndarray
    area: np.ndarray
    reduced: list[Image.Image] | None = None
    reduction: int = 1
    names: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.names = [path.name for path in self.paths]

    @property
    def count(self) -> int:
        return len(self.paths)

    @property
    def heights(self) -> np.ndarray:
        return self.boxes[:, 3] - self.boxes[:, 1]

    def rgba(self, index: int) -> np.ndarray:
        """Frame ``index`` as an RGBA array, refusing a file that changed since it was hashed."""
        image, info = forge_core.load_rgba(self.paths[index])
        if info["sha256"] != self.hashes[index]:
            raise ValueError(f"source frame {self.paths[index].name} changed during the run; nothing published")
        return np.asarray(image)


def _reduction_factor(size: tuple[int, int], count: int) -> int:
    """Integer reduction that keeps the kept copies at most 480 px on a side and 256 MB in all."""
    width, height = size
    factor = max(1, math.ceil(max(width, height) / REDUCED_SIDE))
    while count * math.ceil(width / factor) * math.ceil(height / factor) * 4 > REDUCED_BUDGET_BYTES:
        factor += 1
    return factor


def load_clip(directory: Path, fps: Fraction, minimum: int = 2, *, allow_blank: bool = False,
              keep_reduced: bool = False) -> Clip:
    """Load every frame once: hashes, one shared canvas and body statistics.

    A frame without pixels above alpha 32 is an error unless ``allow_blank`` (fading FX),
    in which case its statistics are NaN. ``keep_reduced`` keeps premultiplied reduced
    copies for extract_features().
    """
    paths = frame_paths(directory, minimum)
    n = len(paths)
    boxes = np.full((n, 4), np.nan)
    cx, ground, area = (np.full(n, np.nan) for _ in range(3))
    hashes, size, factor = [], None, 1
    reduced: list[Image.Image] | None = [] if keep_reduced else None
    for index, path in enumerate(paths):
        image, info = forge_core.load_rgba(path)
        if size is None:
            size = image.size
            factor = _reduction_factor(size, n)
        if image.size != size:
            raise ValueError(f"frames must share one canvas: {path.name} is {image.size[0]}x{image.size[1]}, "
                             f"{paths[0].name} is {size[0]}x{size[1]}")
        hashes.append(info["sha256"])
        if reduced is not None:
            premultiplied = image.convert("RGBa")
            reduced.append(premultiplied.reduce(factor) if factor > 1 else premultiplied)
        ys, xs = np.nonzero(np.asarray(image.getchannel("A")) > forge_core.BODY_ALPHA_THRESHOLD)
        if not len(xs):
            if allow_blank:
                continue
            raise ValueError(f"blank frame {path.name}: no pixel above alpha {forge_core.BODY_ALPHA_THRESHOLD}")
        boxes[index] = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        cx[index] = xs.mean() + 0.5
        ground[index] = np.percentile(ys, 99.0) + 1.0  # robust ground line: bottom edge of the p99 row
        area[index] = len(xs)
    return Clip(Path(directory), paths, hashes, size, fps, boxes, cx, ground, area, reduced, factor)


def linear_trend(values: np.ndarray) -> tuple[np.ndarray, float]:
    t = np.arange(len(values), dtype=float)
    coefficients = np.polyfit(t, values, 1)
    return np.polyval(coefficients, t), float(coefficients[0])


def _crop(pixels: np.ndarray, box: Sequence[int]) -> np.ndarray:
    """Crop with transparent padding outside the canvas."""
    x0, y0, x1, y1 = (int(v) for v in box)
    h, w = pixels.shape[:2]
    out = np.zeros((y1 - y0, x1 - x0) + pixels.shape[2:], pixels.dtype)
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = pixels[sy0:sy1, sx0:sx1]
    return out


def _gradient(planes: np.ndarray) -> np.ndarray:
    luma = 0.299 * planes[..., 0] + 0.587 * planes[..., 1] + 0.114 * planes[..., 2]
    gy, gx = np.gradient(luma)
    return np.hypot(gx, gy)


def extract_features(clip: Clip, kind: str, register: bool) -> dict:
    """Premultiplied pose features on one union crop (drift-registered for gait).

    gait: whole body (max side 96 px) + legs (lower 45 %, at most 160 px wide and never
    upsampled) + legs luminance gradient, weighted 1:2:1, plus an alpha-only silhouette
    row; idle/hover: whole body at max side 160 px + its gradient. Rows are scaled so that
    their L1 distance is the weighted mean absolute difference; premultiplication keeps
    invisible RGB out of every distance. Crops are box-filtered from the clip's reduced
    premultiplied copies. ``steps255`` (for hold detection) compares unshifted crops: the
    whole-pixel registration shift may step between two identical frames.
    """
    if clip.reduced is None:
        raise ValueError("extract_features needs load_clip(..., keep_reduced=True)")
    n, f = clip.count, clip.reduction
    if register:
        trend_x, slope_x = linear_trend(clip.cx)
        trend_g, slope_g = linear_trend(clip.ground)
        sx = np.rint(trend_x - trend_x.mean()).astype(int)
        sy = np.rint(trend_g - trend_g.mean()).astype(int)
    else:
        slope_x = slope_g = 0.0
        sx = sy = np.zeros(n, int)
    b = clip.boxes
    if not np.isfinite(b).all(axis=1).any():
        raise ValueError("every frame is blank: no pixel above alpha 32")
    x0, y0 = int(np.nanmin(b[:, 0] - sx)), int(np.nanmin(b[:, 1] - sy))
    x1, y1 = int(np.nanmax(b[:, 2] - sx)), int(np.nanmax(b[:, 3] - sy))
    mx, my = round_half_up(0.02 * (x1 - x0)), round_half_up(0.02 * (y1 - y0))
    union = (x0 - mx, y0 - my, x1 + mx, y1 + my)
    uw, uh = union[2] - union[0], union[3] - union[1]
    scale = GLOBAL_SIDE[kind] / max(uw, uh)
    gsize = (max(8, round_half_up(uw * scale)), max(8, round_half_up(uh * scale)))
    legs_top = round_half_up(uh * (1 - LEGS_FRACTION))
    legs_width = min(LEGS_WIDTH, max(8, round_half_up(uw / f)))
    lsize = (legs_width, max(8, round_half_up((uh - legs_top) * legs_width / uw)))
    # Pad the reduced copies so every shifted union box lies inside them.
    width, height = clip.size
    reach = max(0, -(union[0] + int(sx.min())), -(union[1] + int(sy.min())),
                union[2] + int(sx.max()) - width, union[3] + int(sy.max()) - height)
    pad = math.ceil(reach / f) + 1
    full, silhouette, previous, steps255 = [], [], None, []
    for index, frame in enumerate(clip.reduced):
        canvas = Image.new("RGBa", (frame.width + 2 * pad, frame.height + 2 * pad))
        canvas.paste(frame, (pad, pad))
        bx0, by0 = (union[0] + sx[index]) / f + pad, (union[1] + sy[index]) / f + pad
        bx1, by1 = (union[2] + sx[index]) / f + pad, (union[3] + sy[index]) / f + pad
        g = np.asarray(canvas.resize(gsize, Image.Resampling.BOX, box=(bx0, by0, bx1, by1)), np.float32) / 255.0
        still = g if not register else np.asarray(canvas.resize(
            gsize, Image.Resampling.BOX, box=(union[0] / f + pad, union[1] / f + pad, union[2] / f + pad,
                                              union[3] / f + pad)), np.float32) / 255.0
        if previous is not None:
            steps255.append(float(np.abs(still - previous).mean()) * 255.0)
        previous = still
        if kind == "gait":
            legs = np.asarray(canvas.resize(lsize, Image.Resampling.BOX, box=(bx0, by0 + legs_top / f, bx1, by1)),
                              np.float32) / 255.0
            grad = _gradient(legs)
            full.append(np.concatenate([g.ravel() / (4 * g.size), legs.ravel() * (2 / (4 * legs.size)),
                                        grad.ravel() / (4 * grad.size)]))
            silhouette.append(np.concatenate([g[..., 3].ravel() / (3 * g[..., 3].size),
                                              legs[..., 3].ravel() * (2 / (3 * legs[..., 3].size))]))
        else:
            grad = _gradient(g)
            full.append(np.concatenate([g.ravel() / (2 * g.size), grad.ravel() / (2 * grad.size)]))
    return {"full": np.stack(full).astype(np.float64),
            "silhouette": np.stack(silhouette).astype(np.float64) if silhouette else None,
            "steps255": np.asarray(steps255), "union": list(union), "global_size": list(gsize),
            "legs_size": list(lsize) if kind == "gait" else None,
            "legs_top_px": legs_top if kind == "gait" else None, "registration": bool(register),
            "reduction": f, "drift": {"cx_slope_px_per_frame": slope_x, "ground_slope_px_per_frame": slope_g}}


def pairwise_l1(features: np.ndarray) -> np.ndarray:
    """Symmetric matrix of summed absolute differences (scipy pdist, or a numpy fallback;
    ``FORGE_CORE_NO_SCIPY=1`` forces the fallback as in forge_core)."""
    n = len(features)
    if os.environ.get("FORGE_CORE_NO_SCIPY", "") in ("", "0"):
        try:
            from scipy.spatial.distance import pdist, squareform
        except ImportError:
            pass
        else:
            return squareform(pdist(features, "cityblock"))
    distances = np.zeros((n, n))
    for i in range(n - 1):
        distances[i, i + 1:] = np.abs(features[i + 1:] - features[i]).sum(axis=1)
    return distances + distances.T


def detect_holds(steps255: np.ndarray, cap: float) -> np.ndarray:
    """Frames that only repeat their predecessor (12 or 8 fps content inside a 24 fps clip).

    Frame i is a hold when its step from i-1 is at most ``cap`` (mean absolute 0-255
    difference of the premultiplied pose crop), below 0.4 x the larger neighbouring step
    and below 0.4 x the clip's 75th-percentile step. ``cap`` 0 disables the detection.
    """
    n = len(steps255) + 1
    holds = np.zeros(n, bool)
    if cap <= 0 or len(steps255) < 2:
        return holds
    padded = np.concatenate([[0.0], steps255, [0.0]])
    neighbours = np.maximum(padded[:-2], padded[2:])
    typical = float(np.percentile(steps255, 75))
    holds[1:] = (steps255 <= cap) & (steps255 < HOLD_RELATIVE * neighbours) & (steps255 < HOLD_RELATIVE * typical)
    return holds


def _moving_median(values: np.ndarray, window: int) -> np.ndarray:
    window = max(1, int(window) | 1)
    if window == 1 or len(values) < 2:
        return values.astype(float)
    padded = np.pad(values.astype(float), window // 2, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)


def drift_range(clip: Clip, kind: str, reference: tuple[int, int], smooth: int, tolerance: float) -> dict:
    """Drift-aware usable range: from the reference span until the body drifts.

    Scale is sqrt(area) relative to the reference span (a push-in grows it); idle also
    tracks the ground line and centroid x, hover only the centroid x (its vertical bob is
    motion, not drift). Signals are median-smoothed over ``smooth`` frames, and the range
    ends at the first frame after the reference whose drift exceeds ``tolerance`` (a scale
    fraction, or that fraction of the body height for positions).
    """
    n = clip.count
    r0 = max(0, min(n - 1, int(reference[0])))
    r1 = max(r0 + 1, min(n, int(reference[1])))
    scale = _moving_median(np.sqrt(clip.area), smooth)
    body = float(np.median(clip.heights[r0:r1]))
    signals = {"scale": (scale / float(np.median(scale[r0:r1])) - 1.0) / tolerance}
    if kind == "idle":
        ground = _moving_median(clip.ground, smooth)
        signals["ground"] = (ground - float(np.median(ground[r0:r1]))) / (tolerance * body)
    if kind in ("idle", "hover"):
        cx = _moving_median(clip.cx, smooth)
        signals["x"] = (cx - float(np.median(cx[r0:r1]))) / (tolerance * body)
    deviation = np.max(np.abs(np.stack(list(signals.values()))), axis=0)
    beyond = np.nonzero(deviation[r1:] > 1.0)[0]
    end = int(r1 + beyond[0]) if len(beyond) else n
    reason = None
    if end < n:
        name = max(signals, key=lambda key: abs(signals[key][end]))
        value = signals[name][end] * tolerance * 100
        reason = (f"scale {value:+.1f}% at frame {end}" if name == "scale"
                  else f"{name} drift {value:+.1f}% of body height at frame {end}")
    return {"start": 0, "endExclusive": end, "reference": [r0, r1], "smoothFrames": max(1, int(smooth) | 1),
            "tolerance": tolerance, "signals": sorted(signals), "reason": reason,
            "maxDeviation": float(deviation.max()) * tolerance, "_deviation": deviation}


def moving_steps(steps: np.ndarray, holds: np.ndarray) -> np.ndarray:
    """Steps that carry motion: not into a held duplicate and not exactly zero (a duplicate
    frame counts as no evidence even when hold detection is off)."""
    return steps[~holds & (steps > 1e-12)]


# --------------------------------------------------------------------------- gait period

def lag_profile(D: np.ndarray, max_lag: int, rows: np.ndarray | None = None) -> np.ndarray:
    """P(L) = mean D[j, j + L] over rows j (all frames by default)."""
    n = len(D)
    profile = np.full(max_lag + 2, np.nan)
    for lag in range(1, min(max_lag + 1, n - 1) + 1):
        j = np.arange(n - lag) if rows is None else rows[rows + lag < n]
        if len(j):
            profile[lag] = float(D[j, j + lag].mean())
    return profile


def local_minima(P: np.ndarray, lo: int, hi: int) -> list[int]:
    return [L for L in range(max(2, lo), min(hi, len(P) - 2) + 1)
            if np.isfinite(P[L - 1]) and np.isfinite(P[L + 1]) and P[L] <= P[L - 1] and P[L] <= P[L + 1]]


def parabola(P: np.ndarray, L: int) -> tuple[float, float]:
    """Sub-frame vertex (lag, value) of the parabola through L-1, L, L+1."""
    a, b, c = P[L - 1], P[L], P[L + 1]
    den = a - 2 * b + c
    if not np.isfinite(den) or den <= 1e-12:
        return float(L), float(b)
    d = 0.5 * (a - c) / den
    return float(L + d), float(b - 0.25 * (a - c) * d)


def global_period(P: np.ndarray, lo: int, hi: int) -> dict:
    """Repeat period T: the shortest deep minimum, refined by least squares over T, 2T, 3T."""
    mins = local_minima(P, lo, hi)
    if not mins:
        return {"status": "no-period", "why": f"no local minimum of the lag profile in [{lo}, {hi}] frames"}
    deepest = min(P[L] for L in mins)
    tol = max(1e-6, ABS_TOL_STEPS * float(P[1]))
    L1 = min(L for L in mins if P[L] <= deepest * (1 + DEPTH_TOL) + tol)
    depth = 1 - P[L1] / float(np.nanmean(P[lo:hi + 1]))
    if depth < PERIODICITY_MIN:
        return {"status": "no-period",
                "why": (f"the lag profile is flat: the best repeat at {L1} frames is only {100 * depth:.0f}% below "
                        f"the profile mean (need {100 * PERIODICITY_MIN:.0f}%); the motion does not repeat")}
    T1, _ = parabola(P, L1)
    harmonics = [{"k": 1, "lag": L1, "lag_subframe": T1, "P": float(P[L1])}]
    for k in (2, 3):
        candidates = [L for L in local_minima(P, math.floor(k * T1 - 2), math.ceil(k * T1 + 2))
                      if L + 1 < len(P) and np.isfinite(P[L + 1])]
        if candidates:
            Lk = min(candidates, key=lambda L: P[L])
            if P[Lk] <= P[L1] * (1 + HARMONIC_TOL):
                harmonics.append({"k": k, "lag": Lk, "lag_subframe": parabola(P, Lk)[0], "P": float(P[Lk])})
    ks = np.array([h["k"] for h in harmonics], float)
    lags = np.array([h["lag_subframe"] for h in harmonics], float)
    return {"status": "ok", "local_minima": [[L, float(P[L])] for L in mins], "L1": L1,
            "harmonics": harmonics, "T": float((ks * lags).sum() / (ks * ks).sum())}


def _refine_minimum(profile: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """Per row of a profile over lags lo-1..hi+1: the argmin lag in [lo, hi], parabola-refined;
    NaN when the minimum sits on the search edge."""
    best = np.argmin(profile[:, 1:-1], axis=1)
    lag = lo + best
    rows = np.arange(len(profile))
    a, b, c = profile[rows, best], profile[rows, best + 1], profile[rows, best + 2]
    den = a - 2 * b + c
    with np.errstate(divide="ignore", invalid="ignore"):
        shift = np.where(den > 1e-12, 0.5 * (a - c) / den, 0.0)
    return np.where((lag > lo) & (lag < hi), lag + shift, np.nan)


def local_period(D: np.ndarray, T: float, radius: int = CONTEXT_RADIUS, search: float = 4) -> tuple[np.ndarray, np.ndarray]:
    """Best lag near T per frame, averaged over frames t-radius..t+radius.

    Returns the local period (NaN where unmeasurable or where the best lag sits on the
    search edge, i.e. cadence far from T) and the mask of measurable frames.
    """
    n = len(D)
    out = np.full(n, np.nan)
    measurable = np.zeros(n, bool)
    lo, hi = max(3, math.floor(T - search)), math.ceil(T + search)
    rows = np.arange(radius, n - radius - hi - 1)
    if not len(rows):
        return out, measurable
    profile = np.zeros((len(rows), hi - lo + 3))
    for column, lag in enumerate(range(lo - 1, hi + 2)):
        diagonal = np.diagonal(D, lag)
        profile[:, column] = sum(diagonal[rows + k] for k in range(-radius, radius + 1)) / (2 * radius + 1)
    out[rows] = _refine_minimum(profile, lo, hi)
    measurable[rows] = True
    return out, measurable


def _stride_lags(T: float) -> tuple[range, range]:
    same = range(math.floor(T - 1.5), math.ceil(T + 1.5) + 1)
    half = range(max(1, math.floor(T / 2 - 1)), math.ceil(T / 2 + 1) + 1)
    return same, half


def paired_half_test(D: np.ndarray, T: float, frames: np.ndarray, eps: float) -> dict:
    """Frames one step apart (T/2) against frames one stride apart (T), paired per frame."""
    n = len(D)
    same_l, half_l = _stride_lags(T)
    frames = np.asarray([t for t in frames if t + max(same_l) < n], int)
    if not len(frames):
        return {"pairs": 0}
    same = np.min([D[frames, frames + L] for L in same_l], axis=0)
    mirror = np.min([D[frames, frames + L] for L in half_l], axis=0)
    ratio = (mirror + eps) / (same + eps)
    return {"pairs": int(len(ratio)), "fraction_mirror_farther": float(np.mean(mirror > same)),
            "median_ratio": float(np.median(ratio)), "p10_ratio": float(np.percentile(ratio, 10)),
            "same_lags": [min(same_l), max(same_l)], "mirror_lags": [min(half_l), max(half_l)]}


def per_frame_two_step(D: np.ndarray, T: float, eps: float) -> np.ndarray:
    """mirror(t) / same(t) per frame with +-2 frames of context; NaN where the stride runs past the clip."""
    n = len(D)
    same_l, half_l = _stride_lags(T)
    span = n - max(same_l)
    ratio = np.full(n, np.nan)
    if span <= 0:
        return ratio
    t = np.arange(span)
    lo, hi = np.maximum(0, t - CONTEXT_RADIUS), np.minimum(span - 1, t + CONTEXT_RADIUS)

    def context(lag: int) -> np.ndarray:
        cumulative = np.concatenate([[0.0], np.cumsum(np.diagonal(D, lag)[:span])])
        return (cumulative[hi + 1] - cumulative[lo]) / (hi - lo + 1)

    mirror = np.min([context(L) for L in half_l], axis=0)
    same = np.min([context(L) for L in same_l], axis=0)
    ratio[:span] = (mirror + eps) / (same + eps)
    return ratio


def step_lags(D_sil: np.ndarray, T: float, search: int = 4) -> np.ndarray:
    """Per frame: sub-frame lag of the best silhouette match near T/2, the duration of the step
    that starts there (a mirrored pose repeats the silhouette, not the colours)."""
    n = len(D_sil)
    half = round_half_up(T / 2)
    lo, hi = max(3, half - search), half + search
    out = np.full(n, np.nan)
    rows = np.arange(max(0, n - hi - 1))
    if not len(rows):
        return out
    profile = np.stack([D_sil[rows, rows + L] for L in range(lo - 1, hi + 2)], axis=1)
    out[rows] = _refine_minimum(profile, lo, hi)
    return out


def analyse_period(D: np.ndarray, D_sil: np.ndarray, lo: int, hi: int, fps: Fraction, state: str,
                   step_eps: float) -> dict:
    """Stride period, half-period guard and per-frame usability of a gait clip."""
    n = len(D)
    rate = float(fps)
    max_lag = min(n - 3, 3 * hi + 3)
    P_all = lag_profile(D, max_lag)
    period = global_period(P_all, lo, hi)
    if period["status"] != "ok":
        return period
    lp, _ = local_period(D, period["T"])
    rows = np.nonzero(np.isfinite(lp) & (np.abs(lp - period["T"]) <= STEADY_TOL * period["T"]))[0]
    # Re-estimate on the steady segment: an irregular start must not set the period.
    P = lag_profile(D, max_lag, rows) if len(rows) >= 2 * hi else P_all
    refined = global_period(P, lo, hi)
    if refined["status"] == "ok":
        period = refined
    T = period["T"]
    lp, measurable = local_period(D, T)
    lp_ok = np.isfinite(lp) & (np.abs(lp - T) <= STEADY_TOL * T)
    rows = np.nonzero(lp_ok)[0]
    test_rows = rows if len(rows) else np.arange(n)
    t_full = paired_half_test(D, T, test_rows, step_eps)
    two_step = (t_full.get("fraction_mirror_farther", 0) >= TWO_STEP_FRACTION
                and t_full.get("median_ratio", 0) >= TWO_STEP_RATIO)
    # A stride holds two mirrored steps, so the silhouette dips again near T/2. Without that
    # dip, T itself may be one step of a gait whose legs look alike.
    Q = lag_profile(D_sil, max_lag, rows if len(rows) >= 2 * hi else None)
    half = [L for L in range(max(2, math.floor(T / 2 - 1.5)), math.ceil(T / 2 + 1.5) + 1) if L + 1 < len(Q)]
    hmin = min(half, key=lambda L: Q[L])
    is_min = bool(Q[hmin] <= Q[hmin - 1] and Q[hmin] <= Q[hmin + 1])
    peaks = (np.nanmax(Q[1:hmin + 1]), np.nanmax(Q[hmin:round_half_up(T) + 1]))
    prominence = float((min(peaks) - Q[hmin]) / Q[1]) if Q[1] > 0 else 0.0
    step_structure = is_min and prominence >= STEP_PROMINENCE_MIN
    floor_s = GAIT_FLOOR_SECONDS[state]
    if T / rate < floor_s:
        verdict = "one-step-double-it"
        why = f"T = {T / rate:.3f} s is under the {state} gait floor {floor_s} s: T is one step, so one stride is 2T"
    elif step_structure and two_step:
        verdict = "full-stride"
        why = (f"the silhouette dips at the half stride (lag {hmin}, prominence {prominence:.2f} steps), so T holds "
               f"two mirrored steps, and frames one step apart (lags {t_full['mirror_lags']}) differ from frames "
               f"one stride apart (lags {t_full['same_lags']}) on {100 * t_full['fraction_mirror_farther']:.0f}% "
               f"of steady frames (median x{t_full['median_ratio']:.2f}): the steps are distinguishable, so a "
               f"T window is one full stride and a T/2 window would loop one leg")
    elif step_structure:
        verdict = "full-stride-legs-alike"
        why = ("T holds two mirrored steps that look alike in pixels: a T window is one stride; a human should "
               "still confirm left/right alternation")
    else:
        verdict = "ambiguous"
        why = ("no step structure inside T (the silhouette does not dip at T/2): T may itself be one step of a "
               "gait whose legs look alike, so one stride is taken as 2T; confirm left/right alternation")
    S = T if verdict in ("full-stride", "full-stride-legs-alike") else 2 * T
    stride_test = paired_half_test(D, S, test_rows, step_eps)
    distinct_at_S = (stride_test.get("fraction_mirror_farther", 0) >= TWO_STEP_FRACTION
                     and stride_test.get("median_ratio", 0) >= TWO_STEP_RATIO)
    ratio = per_frame_two_step(D, S, step_eps)
    # Unusable: a measurable frame whose cadence is off (or whose best lag sat on the search
    # edge), and, when the clip shows distinct steps at S, a frame whose legs do not alternate.
    bad = measurable & ~lp_ok
    if distinct_at_S:
        bad |= np.isfinite(ratio) & (ratio < LOCAL_TWO_STEP_MIN)
    # Frames at the clip edges have no cadence evidence: they inherit the nearest measured frame.
    measured = np.nonzero(measurable)[0]
    if len(measured):
        bad[:measured[0]] = bad[measured[0]]
        bad[measured[-1] + 1:] = bad[measured[-1]]
    usable_rows = np.nonzero(~bad & np.isfinite(lp))[0]
    prof_mean = float(np.nanmean(P[lo:hi + 1]))
    rT = round_half_up(T)
    PT = parabola(P, rT)[1] if 2 <= rT < len(P) - 1 else float(P[rT])
    return {"status": "ok", **period, "T_frames": T, "T_seconds": T / rate, "S_frames": S, "S_seconds": S / rate,
            "T_from_whole_clip": global_period(P_all, lo, hi).get("T"),
            "steady_frames": [int(usable_rows.min()), int(usable_rows.max())] if len(usable_rows) else None,
            "steady_count": int(len(usable_rows)),
            "stride_two_step_test": {**stride_test, "distinct_steps": bool(distinct_at_S)},
            "half_period_test": t_full,
            "step_structure": {"silhouette_half_lag": hmin, "is_local_min": is_min,
                               "prominence_in_steps": prominence, "required": STEP_PROMINENCE_MIN,
                               "present": bool(step_structure)},
            "periodicity": 1 - PT / prof_mean if prof_mean > 0 else 0.0,
            "P_over_step_at_T": PT / float(P[1]) if P[1] > 0 else None,
            "verdict": verdict, "why": why,
            "unusable_frames": [int(i) for i in np.nonzero(bad)[0]],
            "_local_period": lp, "_two_step_ratio": ratio, "_step_lags": step_lags(D_sil, S)}


# --------------------------------------------------------------------------- analysis

@dataclass
class Analysis:
    """Everything select, classification and the review aids need."""
    clip: Clip
    kind: str
    state: str | None
    features: dict
    D: np.ndarray
    D_sil: np.ndarray | None
    DV: np.ndarray | None
    holds: np.ndarray
    step_median: float
    body_height: float
    usable: dict
    period: dict | None = None
    unusable: list[int] = field(default_factory=list)
    lengths: dict = field(default_factory=dict)

    @property
    def steps(self) -> np.ndarray:
        return np.diagonal(self.D, 1)


def analyse(frames_dir: Path, fps: Any, kind: str = "gait", state: str = "run", *,
            dedupe_cap: float = DEDUPE_CAP, drift_tolerance: float | None = None,
            min_seconds: float = 0.5, max_seconds: float = 4.0) -> Analysis:
    """Analyse a frame directory; ValueError when no loop analysis is possible."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if kind == "gait" and state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    if not math.isfinite(dedupe_cap) or dedupe_cap < 0:
        raise ValueError("--dedupe-cap must be a finite number >= 0 (0 disables hold detection)")
    if drift_tolerance is not None and not 0 < drift_tolerance < 1:
        raise ValueError("--drift-tolerance must be in (0, 1)")
    if not 0 < min_seconds <= max_seconds:
        raise ValueError("idle/hover loop bounds need 0 < --min-seconds <= --max-seconds")
    rate = parse_fps(fps)
    clip = load_clip(Path(frames_dir), rate, minimum=12 if kind == "gait" else 8, keep_reduced=True)
    n = clip.count
    feats = extract_features(clip, kind, register=kind == "gait")
    clip.reduced = None  # features done: release the reduced copies
    full = feats.pop("full")
    D = pairwise_l1(full)
    silhouette = feats.pop("silhouette")
    D_sil = pairwise_l1(silhouette) if silhouette is not None else None
    DV = pairwise_l1(np.diff(full, axis=0)) if kind != "gait" else None
    del full
    holds = detect_holds(feats["steps255"], dedupe_cap)
    moving = moving_steps(np.diagonal(D, 1), holds[1:])
    if not len(moving):
        raise ValueError("static clip: consecutive frames are identical, nothing moves to loop")
    step_median = float(np.median(moving))
    body = float(np.median(clip.heights))
    rate_f = float(rate)
    if kind == "gait":
        smin, smax = STATE_STRIDE_SECONDS[state]
        lo, hi = max(4, round_half_up(smin * rate_f)), min(n // 2, round_half_up(smax * rate_f))
        if hi < lo + 2:
            raise ValueError(f"{n} frames at {fps_text(rate)} fps are too few to find a {state} stride of "
                             f"{smin}-{smax} s; supply a longer clip")
        period = analyse_period(D, D_sil, lo, hi, rate, state, RATIO_EPS * step_median)
        if period["status"] != "ok":
            raise ValueError(f"no gait period: {period['why']}")
        stride = max(1, round_half_up(period["S_frames"]))
        steady = period["steady_frames"] or [0, n - 1]
        usable = drift_range(clip, kind, (steady[0], steady[0] + stride), stride,
                             GAIT_DRIFT_TOLERANCE if drift_tolerance is None else drift_tolerance)
        return Analysis(clip, kind, state, feats, D, D_sil, None, holds, step_median, body, usable,
                        period=period, unusable=period["unusable_frames"],
                        lengths={"strideWindowFrames": [lo, hi]})
    # A 2 s running median removes breathing and sway cycles up to 2 s but follows a
    # monotonic ramp such as a push-in without delay.
    usable = drift_range(clip, kind, (0, max(3, round_half_up(0.5 * rate_f))),
                         max(3, round_half_up(DRIFT_SMOOTH_SECONDS * rate_f)),
                         DRIFT_TOLERANCE if drift_tolerance is None else drift_tolerance)
    min_len = max(4, round_half_up(min_seconds * rate_f))
    max_len = max(min_len, round_half_up(max_seconds * rate_f))
    return Analysis(clip, kind, None, feats, D, D_sil, DV, holds, step_median, body, usable,
                    lengths={"minFrames": min_len, "maxFrames": max_len})


# --------------------------------------------------------------------------- gait windows

def phase_tolerance(strides: int, stride_frames: float) -> float:
    """Allowed |frames - strides x local period|: 1 frame (1.5 for two strides) or 7 % per stride."""
    return max(0.5 + 0.5 * strides, 0.07 * strides * stride_frames)


def _window_drift(a: Analysis, s: int, e: int) -> dict:
    clip = a.clip
    if e >= clip.count:
        return {"root_dx_px_per_loop": None, "ground_dy_px_per_loop": None, "root_drift_pct_body": None}
    dx = float(clip.cx[e] - clip.cx[s])
    dy = float(clip.ground[e] - clip.ground[s])
    return {"root_dx_px_per_loop": dx, "ground_dy_px_per_loop": dy,
            "root_drift_pct_body": 100 * math.hypot(dx, dy) / a.body_height}


def gait_window_metrics(a: Analysis, s: int, L: int) -> dict:
    """Seam, wrap, stride coverage, drift and step timing of the loop window [s, s + L)."""
    D, per, clip = a.D, a.period, a.clip
    n, e = len(D), s + L
    T, S = per["T_frames"], per["S_frames"]
    steps = a.steps
    inner = steps[s:e - 1]
    moving = moving_steps(inner, a.holds[s + 1:e])
    step = float(np.median(moving)) if len(moving) else a.step_median
    wrap = float(D[e - 1, s])
    lp = per["_local_period"][max(0, s - CONTEXT_RADIUS):e]
    T_loc = float(np.nanmedian(lp)) * (S / T) if np.isfinite(lp).any() else S
    phase_step = float(D[e - 1, e]) if e < n else step
    # Same-phase references: a step i -> i+1 against the same step one or two periods away.
    Tr = max(1, round_half_up(T))
    index = np.arange(s, e - 1)
    references = []
    for k in (-2, -1, 1, 2):
        j = index + k * Tr
        valid = (j >= 0) & (j + 1 < n)
        references.append(np.where(valid, steps[np.clip(j, 0, n - 2)], np.nan))
    glitch = None
    if len(index):
        stacked = np.stack(references)
        counted = np.isfinite(stacked).sum(axis=0)
        reference = np.sort(np.where(np.isfinite(stacked), stacked, np.inf), axis=0)
        # median of the finite references per column (NaN when a column has none)
        lower = reference[np.maximum(0, (counted - 1) // 2), np.arange(len(index))]
        upper = reference[np.maximum(0, counted // 2), np.arange(len(index))]
        median = np.where(counted > 0, (lower + upper) / 2, np.nan)
        ratios = steps[index] / np.maximum(1e-12, median)
        glitch = float(np.nanmax(ratios)) if np.isfinite(ratios).any() else None
    context = [float(D[s + k, e + k]) for k in range(-CONTEXT_RADIUS, CONTEXT_RADIUS + 1) if s + k >= 0 and e + k < n]
    k_strides = max(1, round_half_up(L / S))
    metrics = {"start": s, "endExclusive": e, "lastInclusive": e - 1, "frames": L, "seconds": L / float(clip.fps),
               "files": [clip.names[s], clip.names[e - 1]], "step_median": step,
               "max_step_over_median": float(inner.max()) / step if len(inner) else None,
               "max_step_over_same_phase": glitch,
               "wrap_ratio": wrap / max(1e-12, phase_step), "wrap_ratio_vs_median_step": wrap / step,
               "seam_jump_ratio": float(D[s, e]) / step if e < n else None,
               "context_repeat_ratio": float(np.mean(context)) / step if context else None,
               "context_pairs": len(context),
               "strides_global": L / S, "strides": k_strides, "local_period": T_loc,
               "phase_error_frames_local": L - k_strides * T_loc,
               "tempo_vs_source_pct_global": (k_strides * S / L - 1) * 100,
               "tempo_vs_source_pct_local": (k_strides * T_loc / L - 1) * 100}
    # Duplicate phases: each frame's nearest non-neighbour inside the window. In one stride that
    # is the mirrored step (about T/2 away); a window holding a phase twice pairs frames T apart.
    block = D[s:e, s:e].copy()
    gap = np.abs(np.arange(L)[:, None] - np.arange(L)[None, :])
    block[gap < 3] = np.inf
    rows = np.isfinite(block).any(axis=1)
    if rows.any():
        nearest = np.abs(np.argmin(block[rows], axis=1) - np.nonzero(rows)[0])
        metrics["nn_lag_median"] = float(np.median(nearest))
        metrics["duplicate_phase_fraction"] = (float(np.mean(np.abs(nearest - T_loc) <= 1.5))
                                               if L >= T_loc - 1.5 else 0.0)
    else:
        metrics["nn_lag_median"] = metrics["duplicate_phase_fraction"] = None
    metrics.update(_window_drift(a, s, e))
    metrics["covers"] = ("one full stride (both steps)" if k_strides == 1 and abs(metrics["phase_error_frames_local"]) <= 1.0
                         else "two strides (each phase twice)" if k_strides == 2 and abs(L - 2 * T_loc) <= 1.5
                         else f"{L / T_loc:.2f} strides (partial or irregular)")
    # Step durations: chain the per-frame step lag from the first frame through the window.
    h = per["_step_lags"]
    finite = h[np.isfinite(h)]
    fallback = float(np.median(finite)) if len(finite) else S / 2
    chain, t = [], float(s)
    while t < e - 0.5 and len(chain) < 2 * k_strides + 1:
        j = round_half_up(t)
        value = float(h[j]) if j < len(h) and np.isfinite(h[j]) else fallback
        chain.append(value)
        t += value
    chain = chain[:2 * k_strides]
    metrics["step_durations_est"] = chain
    complete = len(chain) == 2 * k_strides
    metrics["steps_sum_vs_frames"] = (sum(chain) - L) if complete else None
    metrics["step_asymmetry"] = (float(np.max(np.abs(np.array(chain) - np.mean(chain))) / np.mean(chain))
                                 if complete else None)
    metrics["unusable_frames_touched"] = [t for t in a.unusable if s - CONTEXT_RADIUS <= t <= e + CONTEXT_RADIUS]
    ratio = per["_two_step_ratio"][s:e]
    metrics["two_step_ratio_min_in_window"] = float(np.nanmin(ratio)) if np.isfinite(ratio).any() else None
    metrics["holds_inside"] = [int(i) for i in np.nonzero(a.holds[s + 1:e])[0] + s + 1]
    metrics["score"] = ((metrics["context_repeat_ratio"] if metrics["context_repeat_ratio"] is not None else 9.0)
                        + 0.5 * abs(math.log(max(1e-3, metrics["wrap_ratio"])))
                        + (metrics["root_drift_pct_body"] or 0.0) / 10
                        + (metrics["step_asymmetry"] if metrics["step_asymmetry"] is not None else 0.25))
    return metrics


def classify_gait(a: Analysis, m: dict) -> dict:
    """valid-1-cycle, valid-2-cycle or rejected, with every reason and non-blocking notes."""
    s, e, L = m["start"], m["endExclusive"], m["frames"]
    S = a.period["S_frames"]
    reasons, notes = [], []
    inside = [t for t in a.unusable if s <= t < e]
    if inside:
        reasons.append(f"includes unusable frames {_ranges(inside)} (irregular cadence or legs not alternating)")
    if e > a.usable["endExclusive"]:
        reasons.append(f"runs past the drift-free range ending at frame {a.usable['endExclusive']} "
                       f"({a.usable['reason']})")
    k = m["strides"]
    if k > 2:
        reasons.append(f"{L} frames hold {L / S:.2f} strides: ship one stride (about {round_half_up(S)} frames) "
                       f"or two (about {round_half_up(2 * S)})")
    elif abs(m["phase_error_frames_local"]) > phase_tolerance(k, S):
        if k == 1 and abs(L - S / 2) <= phase_tolerance(1, S):
            reasons.append(f"half stride: {L} frames hold one step of the {S:.2f}-frame stride, so the loop "
                           f"would repeat one leg")
        else:
            reasons.append(f"partial stride: {L} frames are {L / m['local_period']:.2f} local strides; a loop "
                           f"needs about {round_half_up(S)} frames (one stride) or {round_half_up(2 * S)} (two)")
    context = m["context_repeat_ratio"]
    if context is not None and context > SEAM_MAX_STEPS:
        reasons.append(f"seam error {context:.2f} steps: repeating at this length is worse than an ordinary "
                       f"frame step")
    wrap = m["wrap_ratio"]
    if not 1 / WRAP_MAX_FACTOR <= wrap <= WRAP_MAX_FACTOR:
        reasons.append(f"wrap step {wrap:.2f}x the source step at that phase: a {'stall' if wrap < 1 else 'snap'} "
                       f"at the seam")
    drift = m["root_drift_pct_body"]
    if drift is not None and drift > DRIFT_NOTE_PCT:
        notes.append(f"root drifts {drift:.2f}% of body height per loop: lock the root (register the clip) "
                     f"before packaging")
    if m["step_asymmetry"] is not None and m["step_asymmetry"] > 0.25:
        notes.append(f"the two steps differ in duration by {100 * m['step_asymmetry']:.0f}% (limp-like timing)")
    if m["holds_inside"]:
        notes.append(f"held duplicate frames {_ranges(m['holds_inside'])} are dropped; their time goes to the "
                     f"frame they repeat")
    label = "rejected" if reasons else ("valid-1-cycle" if k == 1 else "valid-2-cycle")
    return {"class": label, "reasons": reasons, "notes": notes}


def _nms_by_start(rows: list[dict], keep: int) -> list[dict]:
    picked = []
    for row in rows:
        if all(abs(row["start"] - other["start"]) >= 3 for other in picked):
            picked.append(row)
        if len(picked) == keep:
            break
    return picked


def search_gait(a: Analysis) -> dict:
    """Best valid one-stride and two-stride windows (lowest unified seam score, starts 3+ apart)."""
    n, S = len(a.D), a.period["S_frames"]
    lo = a.lengths["strideWindowFrames"][0]
    rS, r2S = round_half_up(S), round_half_up(2 * S)
    lengths = {1: sorted({L for L in (rS - 1, rS, rS + 1) if L >= max(3, lo)}),
               2: sorted({L for L in (r2S - 1, r2S, r2S + 1) if L >= 3})}
    bad = np.zeros(n + 2 * CONTEXT_RADIUS + 1, bool)
    bad[np.asarray(a.unusable, int) + CONTEXT_RADIUS] = True
    found = {1: [], 2: []}
    for k, options in lengths.items():
        for L in options:
            for s in range(0, min(n - L - CONTEXT_RADIUS, a.usable["endExclusive"] - L + 1)):
                if bad[s:s + L + 2 * CONTEXT_RADIUS + 1].any():  # unusable within the seam context
                    continue
                metrics = gait_window_metrics(a, s, L)
                verdict = classify_gait(a, metrics)
                if verdict["class"] == f"valid-{k}-cycle":
                    found[k].append({**metrics, "classification": verdict})
    for k in found:
        found[k].sort(key=lambda row: (row["score"], row["start"], row["frames"]))
        found[k] = _nms_by_start(found[k], TOP_WINDOWS)
    return {"oneCycle": found[1], "twoCycle": found[2]}


def confidence_gait(a: Analysis, best: dict) -> dict:
    """Weakest of periodicity, two-step evidence, seam, wrap, drift, smoothness and step timing."""
    per = a.period
    test = per["stride_two_step_test"]
    if test.get("distinct_steps"):
        two = _clamp((test["median_ratio"] - 1.0) / 0.5) * _clamp((test["fraction_mirror_farther"] - 0.5) / 0.4)
    else:
        two = 0.35 if per["verdict"] == "ambiguous" else 0.5
    smooth = best["max_step_over_same_phase"] or best["max_step_over_median"] or 1.0
    components = {
        "periodicity": _clamp((per["periodicity"] - 0.10) / 0.30),
        "two_step_evidence": two,
        "seam": _clamp(1.5 - best["context_repeat_ratio"]) if best["context_repeat_ratio"] is not None else 0.0,
        "wrap_step": _clamp(1 - abs(math.log(max(1e-3, best["wrap_ratio"]))) / 0.7),
        "root_drift": _clamp(1 - (best["root_drift_pct_body"] or 0) / 3.0),
        "smoothness": _clamp((2.5 - smooth) / 1.0),
        "step_timing": _clamp(1 - (best["step_asymmetry"] - 0.05) / 0.20) if best["step_asymmetry"] is not None else 0.5,
    }
    review = [REVIEW_FIRST]
    if components["two_step_evidence"] < 0.5:
        review.append("Left/right alternation is not shown by pixels: confirm both steps appear, or ship the "
                      "2-stride window.")
    if components["seam"] < 0.5 or components["wrap_step"] < 0.5:
        review.append("The seam is weaker than an ordinary step: check the wrap for a stall or snap.")
    if components["root_drift"] < 0.5:
        review.append("The root drifts within one loop: register the clip or choose another start.")
    if components["step_timing"] < 0.5:
        review.append("The two steps differ in duration (limp-like timing): compare another start.")
    if components["smoothness"] < 0.5:
        review.append("A frame-to-frame jump inside the window is much larger than usual: look for a glitch frame.")
    return _confidence(components, review)


def _confidence(components: dict, review: list[str]) -> dict:
    overall = min(components.values())
    label = "high" if overall >= 0.7 else "medium" if overall >= 0.4 else "low"
    return {"components": components, "overall": overall, "label": label,
            "review_priority": {"high": "spot-check", "medium": "review", "low": "manual-selection"}[label],
            "review": review}


# --------------------------------------------------------------------------- idle and hover windows

def ambient_window_metrics(a: Analysis, s: int, L: int) -> dict:
    """Closure (pose + 0.5 x velocity), amplitude and wrap of the loop window [s, s + L).

    The loop plays e-1 -> s where the source plays e-1 -> e, so a seamless loop needs
    F[s] = F[e] (pose) and F[s+1] - F[s] = F[e+1] - F[e] (velocity), in ordinary steps.
    """
    D, DV, n, e = a.D, a.DV, len(a.D), s + L
    step = a.step_median
    pose = float(D[s, e]) / step if e < n else None
    velocity = float(DV[s, e]) / step if e < n - 1 else None
    closure = pose + 0.5 * velocity if pose is not None and velocity is not None else None
    metrics = {"start": s, "endExclusive": e, "lastInclusive": e - 1, "frames": L,
               "seconds": L / float(a.clip.fps), "files": [a.clip.names[s], a.clip.names[e - 1]],
               "closure_pose_steps": pose, "closure_velocity_steps": velocity, "closure_steps": closure,
               "amplitude_steps": float(D[s, s:e].max()) / step, "wrap_steps": float(D[e - 1, s]) / step,
               "holds_inside": [int(i) for i in np.nonzero(a.holds[s + 1:e])[0] + s + 1]}
    metrics.update(_window_drift(a, s, e))
    metrics["score"] = closure if closure is not None else float("inf")
    return metrics


def classify_ambient(a: Analysis, m: dict) -> dict:
    """valid-cycle or rejected for an idle/hover loop window."""
    reasons, notes = [], []
    if m["endExclusive"] > a.usable["endExclusive"]:
        reasons.append(f"runs past the drift-free range ending at frame {a.usable['endExclusive']} "
                       f"({a.usable['reason']})")
    if m["frames"] < a.lengths["minFrames"]:
        reasons.append(f"{m['frames']} frames is shorter than the minimum loop of {a.lengths['minFrames']} frames")
    if m["closure_steps"] is None:
        reasons.append("reaches the clip end: no successor frames to verify the closure")
    elif m["closure_steps"] > CLOSURE_MAX_STEPS:
        reasons.append(f"does not close: the wrap misses the next source pose by {m['closure_steps']:.2f} steps "
                       f"(pose + 0.5 x velocity)")
    if m["amplitude_steps"] < AMPLITUDE_MIN_STEPS:
        reasons.append(f"static: the window moves only {m['amplitude_steps']:.2f} ordinary steps")
    if m["root_drift_pct_body"] is not None and m["root_drift_pct_body"] > DRIFT_NOTE_PCT:
        notes.append(f"the body drifts {m['root_drift_pct_body']:.2f}% of its height per loop")
    if m["holds_inside"]:
        notes.append(f"held duplicate frames {_ranges(m['holds_inside'])} are dropped; their time goes to the "
                     f"frame they repeat")
    return {"class": "rejected" if reasons else "valid-cycle", "reasons": reasons, "notes": notes}


def _overlap(a: dict, b: dict) -> float:
    inter = max(0, min(a["endExclusive"], b["endExclusive"]) - max(a["start"], b["start"]))
    union = max(a["endExclusive"], b["endExclusive"]) - min(a["start"], b["start"])
    return inter / union


def search_ambient(a: Analysis) -> list[dict]:
    """Closing idle/hover windows, best closure first, after non-maximum suppression (IoU > 0.5)."""
    D, DV, n = a.D, a.DV, len(a.D)
    end = min(a.usable["endExclusive"], n)
    # amplitude[s, l] = max(D[s, s:s + l + 1]): running maximum along each row from the diagonal.
    shifted = np.zeros((n, n))
    for s in range(n):
        shifted[s, :n - s] = D[s, s:]
    running = np.maximum.accumulate(shifted, axis=1)
    rows = []
    for L in range(a.lengths["minFrames"], a.lengths["maxFrames"] + 1):
        starts = np.arange(0, end - L - 1)  # e + 1 <= end - 1: successor frames inside the usable range
        if not len(starts):
            continue
        closure = (D[starts, starts + L] + 0.5 * DV[starts, starts + L]) / a.step_median
        amplitude = running[starts, L - 1] / a.step_median
        for s in starts[(closure <= CLOSURE_MAX_STEPS) & (amplitude >= AMPLITUDE_MIN_STEPS)]:
            rows.append((float(closure[s - starts[0]]), int(s), int(L)))
    rows.sort()
    picked: list[dict] = []
    for closure, s, L in rows:
        window = {"start": s, "endExclusive": s + L}
        if all(_overlap(window, other) <= 0.5 for other in picked):
            metrics = ambient_window_metrics(a, s, L)
            picked.append({**metrics, "classification": classify_ambient(a, metrics)})
        if len(picked) == TOP_WINDOWS:
            break
    return picked


def pingpong_span(a: Analysis) -> dict:
    """The early calm span for a forward/reverse loop: from the usable start to the most
    extreme pose (largest distance from the first frame) within --max-seconds."""
    n = len(a.D)
    s = a.usable["start"]
    end = min(a.usable["endExclusive"], n, s + a.lengths["maxFrames"])
    first_last = s + a.lengths["minFrames"] - 1
    if end <= first_last:
        raise ValueError(f"the drift-free range [{s}, {a.usable['endExclusive']}) is shorter than the minimum "
                         f"loop of {a.lengths['minFrames']} frames ({a.usable['reason']})")
    last = first_last + int(np.argmax(a.D[s, first_last:end]))
    e = last + 1
    metrics = {"start": s, "endExclusive": e, "lastInclusive": last, "frames": e - s,
               "seconds": (e - s) / float(a.clip.fps), "files": [a.clip.names[s], a.clip.names[last]],
               "amplitude_steps": float(a.D[s, last]) / a.step_median,
               "turnaround_steps": float(a.D[last - 1, last]) / a.step_median,
               "playback_frames": 2 * (e - s) - 2,
               "holds_inside": [int(i) for i in np.nonzero(a.holds[s + 1:e])[0] + s + 1]}
    metrics.update(_window_drift(a, s, e))
    return metrics


def confidence_ambient(a: Analysis, best: dict, policy: str) -> dict:
    review = [REVIEW_FIRST]
    if policy == "cycle":
        components = {"closure": _clamp((1.5 - best["closure_steps"]) / 1.0),
                      "amplitude": _clamp((best["amplitude_steps"] - 1.0) / 3.0)}
    else:
        components = {"turnaround": _clamp((1.5 - best["turnaround_steps"]) / 1.0),
                      "amplitude": _clamp((best["amplitude_steps"] - 1.0) / 3.0),
                      "no_closing_cycle": 0.5}
        review.append(f"No window closes as a cycle: pingpong plays frames {best['start']}-{best['lastInclusive']} "
                      f"forward then back. Use it only for breathing or sway, never for walks or strikes.")
    if a.usable["endExclusive"] < a.clip.count:
        review.append(f"Frames from {a.usable['endExclusive']} on drift ({a.usable['reason']}) and were not used.")
    return _confidence(components, review)


# --------------------------------------------------------------------------- recommendation

def kept_indices(a: Analysis, s: int, e: int) -> list[int]:
    """Frames of [s, e) without held duplicates (the first frame is always kept)."""
    return [s] + [i for i in range(s + 1, e) if not a.holds[i]]


def classify_window(a: Analysis, s: int, e: int) -> dict:
    """Metrics plus classification for any caller-supplied window."""
    if not 0 <= s < e <= len(a.D):
        raise ValueError(f"window {s}:{e} is outside the clip of {len(a.D)} frames")
    if a.kind == "gait":
        metrics = gait_window_metrics(a, s, e - s)
        verdict = classify_gait(a, metrics)
    else:
        metrics = ambient_window_metrics(a, s, e - s)
        verdict = classify_ambient(a, metrics)
    return {**metrics, "classification": verdict}


def recommend(a: Analysis, policy: str = "auto") -> dict:
    """The loop to ship: the best valid window, or the pingpong span for idle/hover.

    gait never pingpongs (a mirrored gait moonwalks). Raises ValueError, listing why, when
    nothing qualifies.
    """
    if a.kind == "gait":
        if policy == "pingpong":
            raise ValueError("walks never ping-pong: a mirrored gait moonwalks; gait loops are full cycles")
        found = search_gait(a)
        best = found["oneCycle"][0] if found["oneCycle"] else (found["twoCycle"][0] if found["twoCycle"] else None)
        if best is None:
            raise ValueError("no loop window: every one- and two-stride window includes unusable frames, runs past "
                             "the drift-free range or fails the seam checks; review the clip manually")
        return {"policy": "cycle", "window": best, "cycles": best["strides"], "candidates": found,
                "confidence": confidence_gait(a, best)}
    found = search_ambient(a)
    if policy in ("auto", "cycle") and found:
        best = found[0]
        return {"policy": "cycle", "window": best, "cycles": 1, "candidates": {"closing": found},
                "confidence": confidence_ambient(a, best, "cycle")}
    if policy == "cycle":
        raise ValueError(f"no {a.kind} window closes within {CLOSURE_MAX_STEPS} steps (pose + 0.5 x velocity); "
                         f"use --policy auto for the pingpong fallback")
    best = pingpong_span(a)
    return {"policy": "pingpong", "window": best, "cycles": 1, "candidates": {"closing": found},
            "confidence": confidence_ambient(a, best, "pingpong")}


def sequence_seam(clip: Clip, indices: Sequence[int]) -> dict | None:
    """forge_core.seam_report of the played sequence on full-resolution source frames, inside
    the union of their visible pixels (so the empty canvas does not dilute it)."""
    if len(indices) < 2:
        return None
    cache: dict[int, np.ndarray] = {}
    for index in indices:
        if index not in cache:
            cache[index] = clip.rgba(index)
    mask = np.zeros(clip.size[::-1], bool)
    for pixels in cache.values():
        mask |= pixels[..., 3] > 0
    report = forge_core.seam_report((cache[i] for i in indices), mask=mask if mask.any() else None)
    report["mask"] = "union of visible pixels"
    return report


def build_selection(clip: Clip, out_dir: Path, indices: Sequence[int], durations: Sequence[int], *,
                    policy: str, method: str, review: str, kind: str | None = None,
                    events: Sequence[dict] = (), extra: dict | None = None,
                    window: tuple[int, int] | None = None) -> dict:
    """A forge-frame-selection/v2 document for frames of ``clip`` (paths relative to ``out_dir``).

    sourceHashes and sourceFiles describe every source frame in [start, endExclusive), like
    v1; sourceIndices are the played frames in order (repeats are holds), with one integer
    duration each. ``window`` is the analysed range when it is wider than the played frames
    (a loop whose last frames were held duplicates). fps is the source frame rate the
    indices were sampled at.
    """
    if not indices or len(indices) != len(durations):
        raise ValueError("a selection needs one duration per selected frame")
    if policy not in LOOP_POLICIES:
        raise ValueError(f"loop policy must be one of {', '.join(LOOP_POLICIES)}")
    start, end = window or (min(indices), max(indices) + 1)
    if not (0 <= start <= min(indices) and max(indices) < end <= clip.count):
        raise ValueError("the selection window must contain every selected frame")
    document = {"schema": SELECTION_V2, "sourceDirectory": directory_ref(clip.directory, out_dir),
                "sourceHashes": clip.hashes[start:end], "sourceFiles": clip.names[start:end],
                "start": int(start), "endExclusive": int(end), "sourceIndices": [int(i) for i in indices],
                "durations_ms": [int(d) for d in durations], "fps": fps_text(clip.fps), "loopPolicy": policy,
                "events": list(events), "status": SELECTED_STATUS, "reviewReason": review, "method": method}
    if kind:
        document["kind"] = kind
    document.update(extra or {})
    return document


def read_selection(path: Path) -> dict:
    """Read a forge-frame-selection v1 or v2 document and normalise it.

    Returns the raw document under ``raw`` plus ``indices`` (played source frames),
    ``durations`` (ms; from fps for v1), ``fps`` (Fraction or None) and ``policy``.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"selection {Path(path).name} is not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise ValueError("selection must be a JSON object")
    schema = data.get("schema")
    if schema not in (SELECTION_V1, SELECTION_V2):
        raise ValueError(f"selection schema must be {SELECTION_V1} or {SELECTION_V2}, got {schema!r}")
    if not isinstance(data.get("sourceDirectory"), str) or not data["sourceDirectory"].strip():
        raise ValueError("selection sourceDirectory must be a nonempty path string")
    start, end = data.get("start"), data.get("endExclusive")
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError("selection start/endExclusive must be integers with 0 <= start < endExclusive")
    hashes = data.get("sourceHashes")
    if (not isinstance(hashes, list) or len(hashes) != end - start
            or any(not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
                   for h in hashes)):
        raise ValueError("selection sourceHashes must hold one lowercase SHA-256 per frame in [start, endExclusive)")
    files = data.get("sourceFiles")
    if files is not None and (not isinstance(files, list) or len(files) != end - start
                              or any(not isinstance(name, str) for name in files)):
        raise ValueError("selection sourceFiles must name each frame in [start, endExclusive)")
    if schema == SELECTION_V1:
        fps = data.get("fps")
        if isinstance(fps, bool) or not isinstance(fps, (int, float)):
            raise ValueError("a v1 selection needs a numeric fps")
        rate = parse_fps(fps)
        return {"raw": data, "schema": schema, "start": start, "endExclusive": end, "hashes": hashes,
                "files": files, "indices": list(range(start, end)), "durations": source_durations(end - start, rate),
                "fps": rate, "policy": None}
    indices, durations = data.get("sourceIndices"), data.get("durations_ms")
    if (not isinstance(indices, list) or not indices
            or any(type(i) is not int or not start <= i < end for i in indices)):
        raise ValueError("selection sourceIndices must be integers inside [start, endExclusive)")
    if (not isinstance(durations, list) or len(durations) != len(indices)
            or any(type(d) is not int or d < 1 for d in durations)):
        raise ValueError("selection durations_ms must hold one integer >= 1 ms per source index")
    policy = data.get("loopPolicy")
    if policy not in LOOP_POLICIES:
        raise ValueError(f"selection loopPolicy must be one of {', '.join(LOOP_POLICIES)}")
    if not isinstance(data.get("events"), list):
        raise ValueError("selection events must be a list")
    rate = parse_fps(data["fps"]) if data.get("fps") is not None else None
    return {"raw": data, "schema": schema, "start": start, "endExclusive": end, "hashes": hashes, "files": files,
            "indices": indices, "durations": durations, "fps": rate, "policy": policy}


def verify_selection(selection: dict, selection_path: Path, frames_dir: Path, clip: Clip | None = None) -> list[Path]:
    """Check that a selection names ``frames_dir`` and that every frame it covers is unchanged
    (against ``clip``'s hashes when the clip is already loaded)."""
    if not resolve_directory(selection["raw"]["sourceDirectory"], selection_path, frames_dir):
        raise ValueError("selection schema/source directory mismatch: it was made for another frames directory")
    paths = clip.paths if clip is not None else frame_paths(frames_dir, 1)
    start, end = selection["start"], selection["endExclusive"]
    if end > len(paths):
        raise ValueError(f"selection reaches frame {end - 1} but {frames_dir} has {len(paths)} frames")
    if selection["files"] is not None and [p.name for p in paths[start:end]] != selection["files"]:
        raise ValueError("selection sourceFiles no longer match the frame names at those positions")
    hashes = clip.hashes[start:end] if clip is not None else [forge_core.sha256_file(p) for p in paths[start:end]]
    if hashes != selection["hashes"]:
        raise ValueError("selection hashes no longer match source frames")
    return paths


# --------------------------------------------------------------------------- review aids

_BACKGROUND = (238, 232, 220)
_PANEL = (24, 27, 32)


def _font() -> ImageFont.ImageFont:
    return ImageFont.load_default()


def _aid_box(clip: Clip, indices: Iterable[int]) -> tuple[int, int, int, int]:
    """Union of the body boxes of ``indices`` with a 6 % margin (clamped to the canvas)."""
    boxes = clip.boxes[[i for i in indices if np.isfinite(clip.boxes[i]).all()]]
    if not len(boxes):
        return 0, 0, clip.size[0], clip.size[1]
    x0, y0 = boxes[:, 0].min(), boxes[:, 1].min()
    x1, y1 = boxes[:, 2].max(), boxes[:, 3].max()
    mx, my = 0.06 * (x1 - x0) + 2, 0.06 * (y1 - y0) + 2
    return (max(0, int(x0 - mx)), max(0, int(y0 - my)),
            min(clip.size[0], int(math.ceil(x1 + mx))), min(clip.size[1], int(math.ceil(y1 + my))))


def _tile(pixels: np.ndarray, box: Sequence[int], width: int) -> Image.Image:
    """Frame crop composited on a light background and scaled to ``width`` (nearest when enlarged 2x+)."""
    crop = Image.fromarray(_crop(pixels, box))
    base = Image.new("RGBA", crop.size, _BACKGROUND + (255,))
    base.alpha_composite(crop)
    height = max(1, round_half_up(crop.height * width / crop.width))
    method = Image.Resampling.NEAREST if width >= 2 * crop.width else Image.Resampling.LANCZOS
    return base.convert("RGB").resize((width, height), method)


def _gif_delays(durations: Sequence[int]) -> list[int]:
    """GIF delays (10 ms units) whose running sum tracks the exact ms timeline; at least 20 ms."""
    cumulative = np.concatenate([[0], np.cumsum(durations)])
    ticks = [round_half_up(value / 10) for value in cumulative]
    return [max(20, 10 * (b - a)) for a, b in zip(ticks, ticks[1:])]


def aid_loop_gif(clip: Clip, indices: Sequence[int], durations: Sequence[int], label: str, out: Path,
                 passes: int = 3) -> dict:
    box = _aid_box(clip, indices)
    width = 240 if len(indices) <= 32 else 160
    tiles = {i: _tile(clip.rgba(i), box, width) for i in sorted(set(indices))}
    font, frames = _font(), []
    for repeat in range(passes):
        for position, index in enumerate(indices):
            tile = tiles[index]
            frame = Image.new("RGB", (width, tile.height + 30), _PANEL)
            frame.paste(tile, (0, 0))
            draw = ImageDraw.Draw(frame)
            if position == 0:
                draw.rectangle((0, 0, width - 1, 4), fill=(230, 90, 40))  # wrap marker
            draw.text((4, tile.height + 2), label, fill=(255, 255, 255), font=font)
            draw.text((4, tile.height + 15), f"src {index} {clip.names[index]}  {position + 1}/{len(indices)} "
                                              f"pass {repeat + 1}/{passes}", fill=(210, 210, 210), font=font)
            frames.append(frame)
    delays = _gif_delays(list(durations) * passes)
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=delays, loop=0, disposal=1,
                   optimize=False)
    return {"frames": len(frames), "delays_ms_total": int(sum(delays)), "exact_ms_total": int(sum(durations)) * passes}


def _heat(a: np.ndarray, b: np.ndarray, box: Sequence[int], size: tuple[int, int]) -> Image.Image:
    def composite(pixels):
        rgba = _crop(pixels, box).astype(np.float32) / 255
        return rgba[..., :3] * rgba[..., 3:] + np.array(_BACKGROUND, np.float32) / 255 * (1 - rgba[..., 3:])
    diff = (np.abs(composite(a) - composite(b)).max(axis=-1) * 255).astype(np.uint8)
    heat = np.stack([diff, (diff * 0.3).astype(np.uint8), 255 - diff], axis=-1)
    return Image.fromarray(heat).resize(size, Image.Resampling.LANCZOS)


def aid_seam(clip: Clip, panels: Sequence[tuple[str, int]], successor: int | None, text: Sequence[str],
             title: str, out: Path) -> None:
    """Seam close-up: the wrap pair, the true successor, |first - successor| and a 2x lower-body zoom."""
    indices = [i for _, i in panels] + ([successor] if successor is not None else [])
    box = _aid_box(clip, indices)
    width = 220
    pixels = {i: clip.rgba(i) for i in indices}
    tiles = [(name, i, _tile(pixels[i], box, width)) for name, i in panels]
    if successor is not None:
        tiles.append(("source plays next", successor, _tile(pixels[successor], box, width)))
    th = tiles[0][2].height
    lower = (box[0], box[1] + round_half_up(0.55 * (box[3] - box[1])), box[2], box[3])
    zoom = [_tile(pixels[i], lower, 2 * width) for _, i in panels[:2]]
    columns = len(tiles) + (1 if successor is not None else 0)
    zoom_top = 26 + th + 22 + 18
    sheet = Image.new("RGB", (max(columns * width, 4 * width + 10), zoom_top + zoom[0].height + 10 + 16 * len(text) + 8),
                      _PANEL)
    draw, font = ImageDraw.Draw(sheet), _font()
    draw.text((6, 6), title, fill=(255, 255, 255), font=font)
    for column, (name, index, tile) in enumerate(tiles):
        sheet.paste(tile, (column * width, 26))
        draw.text((column * width + 4, 26 + th + 4), f"{name}: {index} {clip.names[index]}", fill=(230, 230, 230),
                  font=font)
    if successor is not None:
        first = panels[1][1]
        sheet.paste(_heat(pixels[first], pixels[successor], box, (width, th)), (len(tiles) * width, 26))
        draw.text((len(tiles) * width + 4, 26 + th + 4), "|loop first - source next|", fill=(230, 230, 230), font=font)
    for column, (image, (name, index)) in enumerate(zip(zoom, panels[:2])):
        sheet.paste(image, (column * (2 * width + 10), zoom_top))
        draw.text((column * (2 * width + 10) + 4, zoom_top - 16), f"lower body 2x: {name} {index}",
                  fill=(230, 230, 230), font=font)
    y = zoom_top + zoom[0].height + 10
    for line in text:
        draw.text((6, y), line, fill=(255, 255, 255), font=font)
        y += 16
    forge_core.save_png(sheet, out)


def _silhouette(pixels: np.ndarray, color: tuple[int, int, int]) -> Image.Image:
    out = np.zeros(pixels.shape[:2] + (4,), np.uint8)
    out[..., :3] = color
    out[..., 3] = (pixels[..., 3].astype(np.float32) * 0.55).astype(np.uint8)
    return Image.fromarray(out)


def aid_onion(clip: Clip, pairs: Sequence[tuple[str, int, int]], title: str, out: Path) -> None:
    """Onion skins (red = first index, cyan = second) of each pair, side by side."""
    box = _aid_box(clip, [i for _, a, b in pairs for i in (a, b)])
    width = 240
    panels = []
    for name, first, second in pairs:
        a, b = clip.rgba(first), clip.rgba(second)
        base = Image.new("RGBA", (a.shape[1], a.shape[0]), (250, 250, 250, 255))
        base.alpha_composite(_silhouette(a, (220, 40, 40)))
        base.alpha_composite(_silhouette(b, (0, 170, 200)))
        panels.append((f"{name}: {first} red + {second} cyan", _tile(np.asarray(base), box, width)))
    th = panels[0][1].height
    sheet = Image.new("RGB", (len(panels) * (width + 10), th + 50), _PANEL)
    draw, font = ImageDraw.Draw(sheet), _font()
    draw.text((6, 6), title, fill=(255, 255, 255), font=font)
    for column, (name, tile) in enumerate(panels):
        sheet.paste(tile, (column * (width + 10), 24))
        draw.text((column * (width + 10) + 4, 24 + th + 4), name, fill=(230, 230, 230), font=font)
    forge_core.save_png(sheet, out)


def aid_timeline(a: Analysis, window: dict, out: Path) -> None:
    """Per-frame chart: unusable frames, holds, the drift limit, the chosen window and the
    cadence evidence (gait: local period and two-step ratio; idle/hover: distance from the
    window start and the drift deviation)."""
    n = len(a.D)
    step_px = max(4, min(10, 1200 // n))
    width, height, left = n * step_px + 80, 300, 60
    sheet = Image.new("RGB", (width, height), (250, 250, 248))
    draw, font = ImageDraw.Draw(sheet), _font()
    x = lambda frame: left + frame * step_px  # noqa: E731
    draw.rectangle((x(window["start"]), 20, x(window["endExclusive"]) - 1, 240), fill=(214, 238, 214))
    for frame in a.unusable:
        draw.rectangle((x(frame), 20, x(frame + 1) - 1, 240), fill=(244, 204, 204))
    for frame in np.nonzero(a.holds)[0]:
        draw.rectangle((x(int(frame)), 232, x(int(frame) + 1) - 1, 240), fill=(120, 120, 120))
    if a.usable["endExclusive"] < n:
        draw.line((x(a.usable["endExclusive"]), 16, x(a.usable["endExclusive"]), 244), fill=(230, 120, 0), width=2)
    if a.kind == "gait":
        series = [("local period (frames)", a.period["_local_period"], (40, 80, 200), a.period["S_frames"]),
                  ("two-step ratio", a.period["_two_step_ratio"], (180, 60, 160), LOCAL_TWO_STEP_MIN)]
    else:
        series = [("distance from window start (steps)", a.D[window["start"]] / a.step_median, (40, 80, 200), None),
                  ("drift / tolerance", a.usable["_deviation"], (180, 60, 160), 1.0)]
    for row, (name, values, color, reference) in enumerate(series):
        top, bottom = 30 + row * 105, 120 + row * 105
        finite = np.asarray(values, float)
        good = finite[np.isfinite(finite)]
        if not len(good):
            continue
        lo_v = min(float(good.min()), reference if reference is not None else float(good.min()))
        hi_v = max(float(good.max()), reference if reference is not None else float(good.max()))
        span = hi_v - lo_v or 1.0
        y = lambda v: bottom - (v - lo_v) / span * (bottom - top)  # noqa: E731
        if reference is not None:
            draw.line((left, y(reference), x(n), y(reference)), fill=(150, 150, 150))
        for i in range(n - 1):  # consecutive measured frames only: gaps stay gaps
            if i + 1 < len(finite) and math.isfinite(finite[i]) and math.isfinite(finite[i + 1]):
                draw.line(((x(i) + step_px / 2, y(finite[i])), (x(i + 1) + step_px / 2, y(finite[i + 1]))),
                          fill=color, width=2)
        draw.text((left + 4, top - 10), f"{name}: {lo_v:.2f}..{hi_v:.2f}", fill=color, font=font)
    for frame in range(0, n + 1, 10):
        draw.line((x(frame), 244, x(frame), 250), fill=(60, 60, 60))
        draw.text((x(frame) - 6, 252), str(frame), fill=(60, 60, 60), font=font)
    draw.text((4, 270), "green: chosen window  red: unusable  grey: held duplicates  orange: drift limit",
              fill=(40, 40, 40), font=font)
    draw.text((4, 4), f"{a.kind} timeline, {n} frames at {fps_text(a.clip.fps)} fps", fill=(40, 40, 40), font=font)
    forge_core.save_png(sheet, out)


def write_aids(a: Analysis, rec: dict, indices: Sequence[int], durations: Sequence[int], directory: Path) -> dict:
    """3x loop GIF, seam close-up, onion skin and timeline for the recommendation."""
    directory.mkdir()
    w = rec["window"]
    s, e, n = w["start"], w["endExclusive"], a.clip.count
    label = f"{rec['policy']} {s}..{e - 1} ({e - s} f)"
    played = list(indices) + list(indices[-2:0:-1]) if rec["policy"] == "pingpong" else list(indices)
    played_durations = list(durations) + list(durations[-2:0:-1]) if rec["policy"] == "pingpong" else list(durations)
    gif = aid_loop_gif(a.clip, played, played_durations, label, directory / "loop3x.gif")
    if rec["policy"] == "pingpong":
        last = w["lastInclusive"]
        panels = [("before turn", max(s, last - 1)), ("turnaround", last)]
        successor = last + 1 if last + 1 < n else None
        pairs = [("turn", max(s, last - 1), last), ("source next", last, successor if successor is not None else last)]
        text = [f"amplitude {w['amplitude_steps']:.2f} steps, turnaround speed {w['turnaround_steps']:.2f} steps"]
    else:
        panels = [("last in loop", e - 1), ("loop plays first", s)]
        successor = e if e < n else None
        pairs = [("loop wrap", e - 1, s)] + ([("source", e - 1, e), ("first vs next", s, e)] if successor else [])
        key = "context_repeat_ratio" if a.kind == "gait" else "closure_steps"
        value = w.get(key)
        text = [f"{key} {value:.2f} steps" if value is not None else f"{key} n/a",
                f"wrap {w.get('wrap_ratio', w.get('wrap_steps')):.2f}"]
    aid_seam(a.clip, panels, successor, text, label, directory / "seam.png")
    aid_onion(a.clip, pairs, label, directory / "onion.png")
    aid_timeline(a, w, directory / "timeline.png")
    return {"loop3x": "aids/loop3x.gif", "seam": "aids/seam.png", "onion": "aids/onion.png",
            "timeline": "aids/timeline.png", "gif": gif,
            "gifTiming": "GIF delays are 10 ms units whose running sum tracks durations_ms (minimum 20 ms)"}


# --------------------------------------------------------------------------- stride and entry frame

def _band_shift(a_band: np.ndarray, b_band: np.ndarray, max_shift: int) -> tuple[float, float] | None:
    """Horizontal motion (px, positive = right) that best maps band A onto band B, and the
    mismatch relative to their mass; None when either band is empty."""
    mass = float(a_band.sum() + b_band.sum())
    if a_band.sum() < 2 or b_band.sum() < 2:
        return None
    columns = np.nonzero(a_band.any(axis=0) | b_band.any(axis=0))[0]
    x0 = max(0, int(columns[0]) - max_shift)
    x1 = min(a_band.shape[1], int(columns[-1]) + 1 + max_shift)
    a, b = a_band[:, x0:x1], b_band[:, x0:x1]
    padded = np.pad(b, ((0, 0), (max_shift, max_shift)))
    windows = np.lib.stride_tricks.sliding_window_view(padded, a.shape[1], axis=1)  # (rows, 2M+1, W)
    # cost[j] compares A[x] with B[x + d], d = j - M: content that moved right by m matches at d = m.
    cost = np.abs(windows - a[:, None, :]).sum(axis=(0, 2))
    best = int(np.argmin(cost))
    d = float(best - max_shift)
    if 0 < best < len(cost) - 1:
        den = cost[best - 1] - 2 * cost[best] + cost[best + 1]
        if den > 1e-12:
            d += 0.5 * (cost[best - 1] - cost[best + 1]) / den
    return d, float(cost[best]) / mass


def measure_stride(clip: Clip, start: int, end: int, holds: np.ndarray, *, band_rows: int | None = None,
                   max_shift: int | None = None) -> dict:
    """Stride per source frame from the ground band of [start, end).

    The band is the rows just above the floor (the 90th percentile of the per-frame ground
    rows), where only planted feet appear. For each pair of consecutive distinct frames the
    band of the next frame is matched against the band of the previous one; the planted
    foot moves with the ground. Stride per frame = |median ground motion - root motion|,
    with the root motion the slope of the body centroid, so a treadmill clip and a clip
    that walks across the canvas measure the same.
    """
    masks = {}
    rows = []
    for index in range(start, end):
        mask = clip.rgba(index)[..., 3] > forge_core.BODY_ALPHA_THRESHOLD
        masks[index] = mask
        if mask.any():
            rows.append(forge_core.ground_row(mask))
    if not rows:
        raise ValueError("no body pixels in the measured frames")
    floor = round_half_up(float(np.percentile(rows, 90)))
    body = float(np.nanmedian(clip.heights[start:end]))
    band = band_rows or max(2, round_half_up(BAND_FRACTION * body))
    shift_limit = max_shift or max(8, round_half_up(0.25 * body))
    distinct = [i for i in range(start, end) if i == start or not holds[i]]
    pairs = []
    for first, second in zip(distinct, distinct[1:]):
        a = masks[first][max(0, floor - band):floor].astype(np.float32)
        b = masks[second][max(0, floor - band):floor].astype(np.float32)
        result = _band_shift(a, b, shift_limit)
        if result is None:
            pairs.append({"from": first, "to": second, "status": "no-contact"})
            continue
        motion, mismatch = result
        gap = second - first
        pairs.append({"from": first, "to": second, "motionPxPerFrame": float(motion) / gap, "mismatch": mismatch,
                      "status": "used" if mismatch <= BAND_MATCH_MAX else "not-rigid"})
    rigid = np.array([p["motionPxPerFrame"] for p in pairs if p["status"] == "used"])
    if len(rigid):
        # A pair where the planted foot changes can still look rigid (one foot lands where the
        # other was): keep the pairs that agree with the median ground motion.
        centre = float(np.median(rigid))
        for pair in pairs:
            if pair["status"] == "used" and abs(pair["motionPxPerFrame"] - centre) > max(0.5, 0.25 * abs(centre)):
                pair["status"] = "outlier"
    used = np.array([p["motionPxPerFrame"] for p in pairs if p["status"] == "used"])
    if len(used) < 2:
        raise ValueError(f"only {len(used)} frame pairs show a planted foot in the ground band "
                         f"(rows {floor - band}-{floor - 1}); measure-stride needs a gait with ground contact")
    finite = np.isfinite(clip.cx[start:end])
    root = float(np.polyfit(np.arange(start, end)[finite], clip.cx[start:end][finite], 1)[0]) if finite.sum() >= 2 else 0.0
    ground_motion = float(np.median(used))
    stride = abs(ground_motion - root)
    floor_px = max(MIN_STRIDE[0], MIN_STRIDE[1] * body)
    if stride < floor_px:
        raise ValueError(f"the planted foot moves {stride:.3f} px per frame against the body (minimum "
                         f"{floor_px:.2f}): no stride to measure (an in-place idle, or the ground band misses "
                         f"the feet; try --band-rows)")
    return {"stridePxPerFrame": stride, "groundMotionPxPerFrame": ground_motion, "rootMotionPxPerFrame": root,
            "direction": ("ground moves left (the subject faces right)" if ground_motion - root < 0
                          else "ground moves right (the subject faces left)"),
            "pairsUsed": int(len(used)), "pairsTotal": len(pairs),
            "spreadPx": float(np.percentile(used, 75) - np.percentile(used, 25)),
            "band": {"rows": [floor - band, floor], "floorRow": floor, "heightPx": band},
            "method": ("planted-foot band matching: per pair of distinct frames, the horizontal shift that best maps "
                       "the alpha band above the floor onto the next frame (sub-pixel parabola), median over rigid "
                       "pairs that agree with the median, minus the centroid slope"),
            "pairs": pairs}


def entry_frame(clip: Clip, indices: Sequence[int], rest: np.ndarray, band_rows: int) -> dict:
    """The loop frame most like the rest pose: stance anchors aligned, then silhouette XOR.

    ``rest`` is the rest-pose mask and ``indices`` the played frames (repeats allowed; the
    first position wins a tie). Each frame's stance anchor (forge_core anchor_from_mask
    'stance') is moved onto the rest anchor by whole pixels; the XOR count over the rest
    area ranks the frames (0 = identical silhouette).
    """
    rest_anchor = forge_core.anchor_from_mask(rest, "stance", band_rows)
    rest_area = float(rest.sum())
    h, w = rest.shape
    measured: dict[int, dict] = {}
    for index in dict.fromkeys(indices):
        mask = clip.rgba(index)[..., 3] > forge_core.BODY_ALPHA_THRESHOLD
        if not mask.any():
            measured[index] = {"xorRatio": None}
            continue
        anchor = forge_core.anchor_from_mask(mask, "stance", band_rows)
        dx, dy = round_half_up(rest_anchor[0] - anchor[0]), round_half_up(rest_anchor[1] - anchor[1])
        moved = np.zeros_like(rest)
        ys, xs = np.nonzero(mask)
        ys, xs = ys + dy, xs + dx
        keep = (ys >= 0) & (ys < h) & (xs >= 0) & (xs < w)
        moved[ys[keep], xs[keep]] = True
        lost = int((~keep).sum())
        measured[index] = {"shift": [dx, dy], "xorRatio": (float((moved ^ rest).sum()) + lost) / rest_area}
    scores = [{"outputFrame": position, "sourceIndex": int(index), **measured[index]}
              for position, index in enumerate(indices)]
    ranked = [score for score in scores if score["xorRatio"] is not None]
    if not ranked:
        raise ValueError("no loop frame has body pixels to compare with the rest pose")
    best = min(ranked, key=lambda score: (score["xorRatio"], score["outputFrame"]))
    return {"outputFrame": best["outputFrame"], "sourceIndex": best["sourceIndex"], "xorRatio": best["xorRatio"],
            "method": "stance-aligned silhouette XOR against the rest pose (alpha > 32), whole-pixel alignment",
            "perFrame": scores}


# --------------------------------------------------------------------------- CLI

def qa_envelope(status: str, method: str, checks: list[dict], inputs: list[dict], outputs: list[dict], *,
        tool: dict | None = None) -> dict:
    """A common.schema qaEnvelope (no createdAt, so reruns stay byte-identical)."""
    return {"status": status, "method": method, "notProven": list(NOT_PROVEN), "checks": checks,
            "inputs": inputs, "outputs": outputs,
            "tool": tool or {"name": "gait_loop", "version": GAIT_LOOP_VERSION}}


def frame_refs(clip: Clip, base: Path, indices: Iterable[int]) -> list[dict]:
    return [file_ref(clip.paths[i], base, clip.hashes[i]) for i in sorted(set(indices))]


def _strip_private(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_private(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_strip_private(v) for v in value]
    return value


def cmd_select(args: argparse.Namespace) -> dict:
    out = Path(args.output_dir)
    analysis = analyse(args.frames_dir, args.fps, args.kind, args.state, dedupe_cap=args.dedupe_cap,
                       drift_tolerance=args.drift_tolerance, min_seconds=args.min_seconds,
                       max_seconds=args.max_seconds)
    clip = analysis.clip
    rec = recommend(analysis, args.policy)
    w = rec["window"]
    indices = kept_indices(analysis, w["start"], w["endExclusive"])
    durations = kept_durations(w["start"], w["endExclusive"], indices, clip.fps)
    evaluated = [classify_window(analysis, s, e) for s, e in (parse_interval(item) for item in args.evaluate)]
    played = indices + indices[-2:0:-1] if rec["policy"] == "pingpong" else indices
    seam = sequence_seam(clip, played)
    confidence = rec["confidence"]
    if analysis.kind == "gait":
        per = analysis.period
        method = (f"gait_loop select ({args.state}): harmonic least-squares period {per['T_frames']:.2f} frames, "
                  f"{per['verdict']} half-period guard, unified seam score")
    else:
        method = (f"gait_loop select ({analysis.kind}): pose + 0.5 x velocity closure with non-maximum suppression"
                  + (", pingpong fallback over the early calm span" if rec["policy"] == "pingpong" else ""))
    with forge_core.staged_output(out) as stage:
        selection = build_selection(
            clip, stage, indices, durations, policy=rec["policy"], method=method,
            review=" ".join(confidence["review"]), kind=analysis.kind, window=(w["start"], w["endExclusive"]),
            extra={"cycles": rec["cycles"], "confidence": {"label": confidence["label"],
                                                           "overall": confidence["overall"]},
                   "seam": seam})
        aids = None if args.no_aids else write_aids(analysis, rec, indices, durations, stage / "aids")
        write_selection_files(stage, {"selection.json": selection})
        checks = [{"id": "loop-found", "status": "pass", "value": [w["start"], w["endExclusive"]]},
                  {"id": "confidence", "status": "pass" if confidence["overall"] >= 0.4 else "warn",
                   "value": confidence["overall"], "threshold": 0.4},
                  {"id": "drift-free-range", "status": "pass", "value": analysis.usable["endExclusive"]},
                  {"id": "visual-review", "status": "needs-visual-review", "value": None}]
        if seam is not None:
            checks.insert(1, {"id": "seam_over_p95", "status": "pass" if seam["seam_over_p95"] <= 1.0 else "warn",
                              "value": seam["seam_over_p95"], "threshold": 1.0})
        if analysis.kind == "gait":
            checks.insert(1, {"id": "half-period-guard", "status": "pass", "value": analysis.period["verdict"]})
        outputs = [file_ref(stage / "selection.json", stage)]
        if aids:
            outputs += [file_ref(stage / aids[key], stage) for key in ("loop3x", "seam", "onion", "timeline")]
        report = {
            "schema": "video2dsprite.loop_report.v1",
            "tool": {"name": "gait_loop", "version": GAIT_LOOP_VERSION},
            "frames": {"directory": directory_ref(clip.directory, stage), "count": clip.count,
                       "first": clip.names[0], "last": clip.names[-1], "fps": fps_text(clip.fps),
                       "canvas": list(clip.size)},
            "indexing": "0-based positions in the sorted *.png list; intervals are [start, endExclusive)",
            "kind": analysis.kind, "state": analysis.state,
            "analysis": {**{key: value for key, value in analysis.features.items() if key != "steps255"},
                         "lengths": analysis.lengths, "stepMedian": analysis.step_median,
                         "bodyHeightMedianPx": analysis.body_height},
            "usableRange": _strip_private(analysis.usable),
            "holds": [int(i) for i in np.nonzero(analysis.holds)[0]],
            "unusableFrames": analysis.unusable,
            "period": _strip_private(analysis.period) if analysis.period else None,
            "recommendation": {"policy": rec["policy"], "cycles": rec["cycles"], "window": rec["window"],
                               "sourceIndices": indices, "durations_ms": durations, "confidence": confidence,
                               "seam": seam},
            "candidates": rec["candidates"],
            "evaluated": evaluated,
            "aids": aids,
            "qa": qa_envelope("needs-visual-review", method, checks,
                      frame_refs(clip, stage, range(clip.count)), outputs),
        }
        write_selection_files(stage, {"loop-report.json": report})
    return {"output": str(out), "selection": str(out / "selection.json"), "metadata": str(out / "loop-report.json"),
            "status": SELECTED_STATUS, "policy": rec["policy"], "start": w["start"],
            "endExclusive": w["endExclusive"], "frames": len(indices), "confidence": confidence["label"]}


def cmd_measure_stride(args: argparse.Namespace) -> dict:
    out = Path(args.output_dir)
    rate = parse_fps(args.fps)
    if args.band_rows is not None and args.band_rows < 1:
        raise ValueError("--band-rows must be at least 1")
    if args.px_per_unit is not None and not (math.isfinite(args.px_per_unit) and args.px_per_unit > 0):
        raise ValueError("--px-per-unit must be a positive number")
    clip = load_clip(Path(args.frames_dir), rate, 2, allow_blank=True, keep_reduced=True)
    selection = None
    if args.selection:
        selection = read_selection(args.selection)
        verify_selection(selection, args.selection, args.frames_dir, clip)
        start, end = selection["start"], selection["endExclusive"]
        loop = list(selection["indices"])
        cycles = selection["raw"].get("cycles", 1)
        if type(cycles) is not int or cycles < 1:
            raise ValueError("selection cycles must be a whole number >= 1")
    else:
        start, end = parse_interval(args.range) if args.range else (0, clip.count)
        if end > clip.count:
            raise ValueError(f"--range ends after the last frame ({clip.count} frames)")
        loop = list(range(start, end))
        cycles = 1
    if end - start < 3:
        raise ValueError("measure-stride needs at least 3 frames")
    feats = extract_features(clip, "gait", register=False)
    clip.reduced = None
    holds = detect_holds(feats["steps255"], args.dedupe_cap)
    stride = measure_stride(clip, start, end, holds, band_rows=args.band_rows)
    if args.rest:
        rest_image, rest_info = forge_core.load_rgba(args.rest)
        if rest_image.size != clip.size:
            raise ValueError(f"--rest is {rest_image.size[0]}x{rest_image.size[1]}; the frames are "
                             f"{clip.size[0]}x{clip.size[1]} (use the same registered canvas)")
        rest = np.asarray(rest_image.getchannel("A")) > forge_core.BODY_ALPHA_THRESHOLD
        rest_ref = {"file": Path(args.rest).name, "sha256": rest_info["sha256"]}
    else:
        if not 0 <= args.rest_frame < clip.count:
            raise ValueError(f"--rest-frame must be in [0, {clip.count})")
        rest = clip.rgba(args.rest_frame)[..., 3] > forge_core.BODY_ALPHA_THRESHOLD
        rest_ref = {"frame": args.rest_frame, "file": clip.names[args.rest_frame]}
    if not rest.any():
        raise ValueError("the rest pose has no body pixels")
    entry = entry_frame(clip, loop, rest, stride["band"]["heightPx"])
    entry["rest"] = rest_ref
    cycle_frames = (end - start) / cycles
    cycle = {"frames": cycle_frames, "cycles": cycles, "lengthPx": stride["stridePxPerFrame"] * cycle_frames,
             "cadenceMs": float(sum(selection["durations"])) / cycles if selection else
             float(Fraction(1000) * Fraction(cycle_frames) / rate)}
    if args.px_per_unit:
        cycle["strideWorldUnits"] = cycle["lengthPx"] / args.px_per_unit
        cycle["speedRef"] = cycle["strideWorldUnits"] * 1000 / cycle["cadenceMs"]
    with forge_core.staged_output(out) as stage:
        outputs = []
        if selection is not None:
            raw = dict(selection["raw"])
            if raw["schema"] == SELECTION_V1:
                raw = build_selection(clip, stage, selection["indices"], selection["durations"], policy="cycle",
                                      method="gait_loop measure-stride (v1 selection converted to v2)",
                                      review=REVIEW_FIRST, kind="gait")
            else:
                raw["sourceDirectory"] = directory_ref(clip.directory, stage)
            raw.update({"stridePxPerFrame": stride["stridePxPerFrame"], "entryFrame": entry["outputFrame"],
                        "cadenceMs": cycle["cadenceMs"]})
            if "strideWorldUnits" in cycle:
                raw.update({"strideWorldUnits": cycle["strideWorldUnits"], "speedRef": cycle["speedRef"]})
            write_selection_files(stage, {"selection.json": raw})
            outputs.append(file_ref(stage / "selection.json", stage))
        checks = [{"id": "stride-pairs", "status": "pass", "value": stride["pairsUsed"], "threshold": 2},
                  {"id": "stride-spread-px", "status": "pass" if stride["spreadPx"] <= 0.25 * max(1.0, stride["stridePxPerFrame"]) else "warn",
                   "value": stride["spreadPx"]},
                  {"id": "entry-frame", "status": "needs-visual-review", "value": entry["outputFrame"]}]
        report = {"schema": "video2dsprite.stride_report.v1",
                  "tool": {"name": "gait_loop", "version": GAIT_LOOP_VERSION},
                  "frames": {"directory": directory_ref(clip.directory, stage), "range": [start, end],
                             "fps": fps_text(rate), "holds": [i for i in range(start + 1, end) if holds[i]]},
                  "stride": stride, "cycle": cycle, "entryFrame": entry,
                  "qa": qa_envelope("needs-visual-review", stride["method"], checks,
                            frame_refs(clip, stage, range(start, end)), outputs)}
        write_selection_files(stage, {"stride.json": report})
    summary = {"output": str(out), "metadata": str(out / "stride.json"),
               "stridePxPerFrame": round(stride["stridePxPerFrame"], 3), "entryFrame": entry["outputFrame"]}
    if selection is not None:
        summary["selection"] = str(out / "selection.json")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select", help="choose a gait, idle or hover loop and write a v2 frame selection",
                            description="Choose a loop and write selection.json, loop-report.json and aids/.")
    select.add_argument("--frames-dir", type=Path, required=True, help="keyed RGBA PNG frames on one canvas")
    select.add_argument("--fps", required=True, help="source frame rate: a number or N/D (24, 24000/1001)")
    select.add_argument("--output-dir", type=Path, required=True, help="new directory; refused if it exists")
    select.add_argument("--kind", choices=KINDS, default="gait",
                        help="gait = walk/run cycle; idle/hover = closure loop with pingpong fallback (default gait)")
    select.add_argument("--state", choices=STATES, default="run",
                        help="gait stride window: run 0.30-1.20 s, walk 0.50-1.60 s (default run)")
    select.add_argument("--policy", choices=("auto", "cycle", "pingpong"), default="auto",
                        help="idle/hover: auto = cycle when a window closes, else pingpong; gait is always cycle")
    select.add_argument("--dedupe-cap", type=float, default=DEDUPE_CAP,
                        help=f"max mean step (0-255) of a held duplicate frame; 0 keeps every frame (default {DEDUPE_CAP})")
    select.add_argument("--drift-tolerance", type=float,
                        help=f"usable range ends past this drift fraction (default {DRIFT_TOLERANCE} idle/hover, "
                             f"{GAIT_DRIFT_TOLERANCE} gait scale)")
    select.add_argument("--min-seconds", type=float, default=0.5, help="idle/hover: shortest loop (default 0.5)")
    select.add_argument("--max-seconds", type=float, default=4.0, help="idle/hover: longest loop (default 4)")
    select.add_argument("--evaluate", action="append", default=[], metavar="START:END",
                        help="also classify this 0-based [start, endExclusive) window (repeatable)")
    select.add_argument("--no-aids", action="store_true", help="skip the GIF and PNG review aids")
    stride = sub.add_parser("measure-stride", help="stride per frame and the most rest-like entry frame",
                            description="Measure stride per frame and the entry frame; writes stride.json "
                                        "(and selection.json with stride fields when --selection is given).")
    stride.add_argument("--frames-dir", type=Path, required=True)
    stride.add_argument("--fps", required=True, help="source frame rate: a number or N/D")
    stride.add_argument("--output-dir", type=Path, required=True, help="new directory; refused if it exists")
    which = stride.add_mutually_exclusive_group()
    which.add_argument("--selection", type=Path, help="frame selection (v1 or v2) of the loop to measure")
    which.add_argument("--range", help="0-based start:endExclusive to measure (default: every frame)")
    rest = stride.add_mutually_exclusive_group()
    rest.add_argument("--rest-frame", type=int, default=0, help="frame index of the rest pose (default 0)")
    rest.add_argument("--rest", type=Path, help="rest-pose PNG on the same canvas (for example the idle frame)")
    stride.add_argument("--band-rows", type=int, help="ground band height in px (default 4%% of body height)")
    stride.add_argument("--px-per-unit", type=float, help="source px per world unit: adds strideWorldUnits and speedRef")
    stride.add_argument("--dedupe-cap", type=float, default=DEDUPE_CAP, help="held-duplicate cap as for select")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = cmd_select(args) if args.command == "select" else cmd_measure_stride(args)
    except (ValueError, OSError) as error:
        print(forge_core.ascii_text(f"error: {error}"), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
