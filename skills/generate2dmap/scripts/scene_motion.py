#!/usr/bin/env python3
"""Masked environment motion on a static plate: build a registered loop, then QA the decoded file.

build: decodes the provider clip, maps every frame onto the plate with one fixed
transform (plan.registration, --fit or --transform), composites it only inside
the motion mask (outside the mask every pre-encode frame equals the plate
exactly), assembles the plan's loop and encodes it with closed GOPs aligned to
the loop (forge_av). Loop policies:

  forward-overlap  frames [a+K, b-K) then K blends of tail b-K+j with head a+j,
                   head weight smoothstep((j+1)/(K+1)): every layer only plays
                   forwards, so water never reverses (default K=16).
  pingpong         a..b-1 then b-2..a+1; only when every region is motion:
                   sway (cloth, foliage, flags). flow and flicker never reverse.

The encoded file is decoded again and gated. An aligned GOP is necessary but
not sufficient: every keyframe refreshes the picture and pops against the
drifted frames before it. When the in-mask seam fails, the build retries with
more bits at the wrap (x264 zones at a lower QP on each keyframe and the 8
frames before it: crf-8, then crf-12, then crf-14), then also a lower crf;
--ladder off keeps the first encode. Writes loop.mp4,
poster.png (decoded loop frame 0 over the plate, so the swap to the video
changes no pixel), motion-mask.png (the mask as applied: the input mask times
the clip coverage) and scene-motion.json into a new --output-dir; a failed
gate publishes nothing.

qa: measures a decoded loop as a runtime shows it over the plate
(decoded * mask + plate * (1 - mask)): the in-mask seam against the 95th
percentile adjacent step (fail above --max-seam-ratio), motion energy and mean
mask opacity (warn when motion is invisible), the leak ring just outside the
mask, protected-core and per-region stability (motionless regions are judged as
stills), the poster swap, frame count, GOP alignment and MP4 timestamps.
Writes loop-qa.json and seam-diff.png; exits 1 when the status is fail.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np
from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import build_motion_mask as motion_mask  # noqa: E402  (sibling script of this skill)
import forge_av  # noqa: E402  (this skill's vendored copies)
import forge_core  # noqa: E402


REPORT_SCHEMA = "generate2dmap.scene_motion.v1"
LOOP_QA_SCHEMA = "generate2dmap.scene_loop_qa.v1"
TOOL = {"name": "scene_motion", "version": "1.0.0"}
DEFAULT_CRF = 18
DEFAULT_EDGE_FADE = 24.0
WRAP_FRAMES = 8
# Rungs tried after a failed decoded seam: (crf drop, wrap QP drop) from the base crf. The wrap QP is the
# lever; a lower crf alone barely moves the seam. Real 1280x720 lake clip, crf 18: plain 3.37 x p95 (1.01 MB),
# wrap QP 10 1.13 (1.69 MB), wrap QP 6 0.73 (1.98 MB), wrap QP 4 0.57 (2.06 MB); crf 14 with wrap QP 10 1.12.
LADDER_STEPS = ((0, 8), (0, 12), (0, 14), (4, 16))
MIN_QP = 1  # H.264 Main has no lossless mode
STILL_FLOOR = 0.05      # a region whose decoded p95 step is below this (RGB 0-255) is judged as a still
STILL_TOLERANCE = 1.0   # ... and passes when it never drifts more than this from loop frame 0
SHIFT_RADIUS = 2
SHIFT_SAMPLES = 200_000


@dataclass(frozen=True)
class Thresholds:
    """Decoded-loop gates; the warn levels come from one project's scenes (hd2d, Dusk) and are provisional."""
    max_seam_ratio: float = 1.0       # in-mask decoded seam / adjacent p95 (plan B16-T3)
    warn_mean_opacity: float = motion_mask.DEFAULT_WARN_MEAN_OPACITY
    warn_motion_energy: float = 2.0   # max in-mask mean |frame - frame 0|, RGB 0-255
    warn_leak: float = 0.5            # mean decoded step in the ring outside the mask, RGB 0-255
    warn_protected: float = 0.5       # max mean |frame - frame 0| inside protected cores, RGB 0-255
    ring_px: int = 4

    def describe(self) -> dict[str, Any]:
        return {"maxSeamRatio": self.max_seam_ratio, "warnMeanOpacity": self.warn_mean_opacity,
                "warnMotionEnergy": self.warn_motion_energy, "warnLeak": self.warn_leak,
                "warnProtected": self.warn_protected, "ringPx": self.ring_px}


BUILD_NOT_PROVEN = [
    "Playback in a browser, engine or device: only an offline ffmpeg decode was measured; decoder throughput, "
    "memory and autoplay are not.",
    "Perceptual seamlessness: the seam ratio compares the wrap with the loop's own steps inside the mask; watch "
    "the loop at gameplay scale.",
    "Generated content: compositing cannot remove a hallucinated object, a ring on wet ground or a drifting "
    "landmark; review the clip and regenerate instead.",
    "Registration is one fixed transform; the shift estimate covers whole pixels in static areas of three frames.",
    "Thresholds (seam 1.0, mean opacity 0.07, motion energy 2.0, leak and protected drift 0.5) come from one "
    "project's loops.",
]
QA_NOT_PROVEN = [
    "Playback in a browser, engine or device: only an offline ffmpeg decode was measured.",
    "Perceptual seamlessness and content quality: numbers inside the mask, not a visual review.",
    "The runtime composite assumes decoded * mask + plate * (1 - mask); a runtime that plays the video full "
    "frame shows the leak ring and protected-core numbers instead.",
    "Thresholds come from one project's loops and are provisional.",
]


# --------------------------------------------------------------------------- loop assembly

def _smoothstep(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return value * value * (3.0 - 2.0 * value)


def loop_frames(policy: str, start: int, stop: int, overlap: int) -> list[list[tuple[int, float]]]:
    """Each loop frame as ``[(source index, weight), ...]`` for source frames [start, stop)."""
    count = stop - start
    if policy == "pingpong":
        if count < 2:
            raise ValueError("a pingpong loop needs at least two source frames.")
        order = list(range(start, stop)) + list(range(stop - 2, start, -1))
        return [[(index, 1.0)] for index in order]
    if overlap == 0:
        return [[(index, 1.0)] for index in range(start, stop)]
    if count < 2 * overlap + 2:
        raise ValueError(f"forward-overlap {overlap} needs at least {2 * overlap + 2} source frames; the range "
                         f"[{start}, {stop}) holds {count}. Lower loop.overlap or widen loop.range.")
    frames = [[(index, 1.0)] for index in range(start + overlap, stop - overlap)]
    for j in range(overlap):
        weight = _smoothstep((j + 1) / (overlap + 1))
        frames.append([(stop - overlap + j, 1.0 - weight), (start + j, weight)])
    return frames


def reversed_steps(frames: Sequence[Sequence[tuple[int, float]]]) -> int:
    """Steps (wrap included) where some layer plays backwards: source s + 1 is followed by source s."""
    count = 0
    for index, frame in enumerate(frames):
        current = {source for source, weight in frame if weight > 0}
        following = frames[(index + 1) % len(frames)]
        if any(source + 1 in current for source, weight in following if weight > 0):
            count += 1
    return count


def composite(frame: np.ndarray, plate: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """(plate * (255 - mask) + frame * mask) / 255 rounded half-up, in exact integers (uint8 RGB).

    Wherever the mask is 0 the result is the plate, bit for bit. The sum never
    exceeds 255 * 255 + 127, so uint16 holds it, and 255 being odd means no
    value lies exactly halfway between two levels.
    """
    weight = mask[..., None].astype(np.uint16)
    total = plate.astype(np.uint16) * (255 - weight)
    total += frame.astype(np.uint16) * weight
    total += 127
    return (total // 255).astype(np.uint8)


def bounding_window(mask: np.ndarray) -> tuple[slice, slice]:
    """(rows, columns) slices of the True pixels' bounding box (empty slices for an empty mask)."""
    rows, columns = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    if rows.size == 0:
        return slice(0, 0), slice(0, 0)
    return slice(int(rows[0]), int(rows[-1]) + 1), slice(int(columns[0]), int(columns[-1]) + 1)


def pad_even(rgb: np.ndarray) -> np.ndarray:
    """Repeat the last column/row so both dimensions are even (H.264 4:2:0); runtimes crop it away."""
    height, width = rgb.shape[:2]
    if width % 2 == 0 and height % 2 == 0:
        return rgb
    return np.pad(rgb, ((0, height % 2), (0, width % 2), (0, 0)), mode="edge")


def estimate_shift(aligned: np.ndarray, plate: np.ndarray, static: np.ndarray,
                   radius: int = SHIFT_RADIUS) -> dict[str, Any]:
    """Whole-pixel shift (dx, dy) that best matches the aligned clip frame to the plate on static pixels.

    plate(x, y) ~ aligned(x + dx, y + dy): the clip content sits (dx, dy) px
    away from the plate, so offset - (dx, dy) would register it. At most
    SHIFT_SAMPLES static pixels are compared (an even stride).
    """
    height, width = static.shape
    inner = np.zeros_like(static)
    inner[radius:height - radius, radius:width - radius] = static[radius:height - radius, radius:width - radius]
    ys, xs = np.nonzero(inner)
    if ys.size < 64:
        return {"status": "skipped", "reason": "fewer than 64 static pixels"}
    stride = max(1, math.ceil(ys.size / SHIFT_SAMPLES))
    ys, xs = ys[::stride], xs[::stride]
    a = aligned.astype(np.float32) @ motion_mask.LUMA_WEIGHTS
    target = plate[ys, xs].astype(np.float32) @ motion_mask.LUMA_WEIGHTS
    scores = {(dx, dy): float(np.abs(a[ys + dy, xs + dx] - target).mean())
              for dy in range(-radius, radius + 1) for dx in range(-radius, radius + 1)}
    best = min(scores, key=lambda key: (scores[key], abs(key[0]) + abs(key[1])))
    return {"status": "measured", "shift": list(best), "maeAtZero": round(scores[(0, 0)], 4),
            "maeAtBest": round(scores[best], 4), "pixels": int(ys.size)}


# --------------------------------------------------------------------------- encoding

def wrap_zones(count: int, keyint: int, wrap_frames: int, qp: int) -> str:
    """x264 zones giving QP ``qp`` to every keyframe and the ``wrap_frames`` frames before each GOP end."""
    marked = np.zeros(count, bool)
    for start in range(0, count, keyint):
        marked[start] = True
        end = min(count, start + keyint)
        marked[max(start + 1, end - wrap_frames):end] = True
    edges = np.flatnonzero(np.diff(np.concatenate([[0], marked.astype(np.int8), [0]])))
    return "/".join(f"{first},{last - 1},q={qp}" for first, last in zip(edges[::2], edges[1::2]))


def _local_encode_h264_loop(frames: Sequence[Any], out: Path, fps_rational: str, *, keyint: int | None, crf: int,
                            wrap_qp: int, wrap_frames: int = WRAP_FRAMES,
                            timeout: float = forge_av.DEFAULT_TIMEOUT) -> dict[str, Any]:
    """forge_av.encode_h264_loop plus x264 zones that spend more bits on both sides of every keyframe.

    A keyframe refreshes the picture; the frames before it have drifted, so the
    wrap pops. Coding the keyframe and the last ``wrap_frames`` frames of each
    GOP at QP ``wrap_qp`` shrinks the pop where it happens. The arguments mirror
    forge_av's own encoder through its helpers, so the two encodes differ only by
    the zones (promotion request: wrap_qp/wrap_frames on encode_h264_loop).
    """
    clip = forge_av._Clip(frames, fps_rational, forge_av.MAX_FPS)
    quality, gop = forge_av._crf(crf, 51), forge_av._keyint(keyint, clip.count, default=clip.count)
    wrap = forge_av._crf(wrap_qp, 51)
    width, height = clip.size
    if width % 2 or height % 2:
        raise ValueError(f"H.264 4:2:0 needs even dimensions, got {width}x{height}.")
    argv = [forge_av._tool("ffmpeg"), *forge_av._FFMPEG_GLOBAL, *forge_av._raw_input(clip, "rgb24"),
            "-filter_complex_threads", "1", "-filter_complex", f"[0:v]{forge_av._to_bt709('yuv420p')}[v]",
            "-map", "[v]", "-an", *forge_av._x264_args(quality, gop, True),
            "-x264-params", "zones=" + wrap_zones(clip.count, gop, wrap_frames, wrap),
            "-movflags", "+faststart", "-fps_mode", "passthrough", *forge_av._DETERMINISTIC_MUX]
    chunks = (forge_av._opaque_rgb(index, frame).tobytes() for index, frame in clip.frames())
    target, keyframes = forge_av._encode_file(out, "mp4", argv, chunks, clip.count, gop, timeout)
    return {**forge_av._digest(target), "mimeType": "video/mp4", "codec": "h264", "profile": "main",
            "width": width, "height": height, "crf": quality, "keyint": gop, "closedGop": True,
            "keyframes": keyframes, "wrapQp": wrap, "wrapFrames": wrap_frames, **clip.timing()}


def encode_rungs(crf: int, ladder: bool) -> list[dict[str, Any]]:
    """The encodes to try in order: the plan's crf, then a lower QP at the wrap, then also a lower crf."""
    rungs = [{"crf": crf, "wrapQp": None}]
    if ladder:
        for crf_drop, wrap_drop in LADDER_STEPS:
            rung = {"crf": max(MIN_QP, crf - crf_drop), "wrapQp": max(MIN_QP, crf - wrap_drop)}
            if rung not in rungs:
                rungs.append(rung)
    return rungs


def encode_loop(paths: Sequence[Path], out: Path, fps: str, keyint: int, rung: dict[str, Any]) -> dict[str, Any]:
    """One GOP-aligned closed-GOP H.264 encode of the loop frames (forge_av, plus wrap zones on later rungs)."""
    if rung["wrapQp"] is None:
        result = forge_av.encode_h264_loop(list(paths), out, fps, keyint=keyint, crf=rung["crf"])
        return {**result, "wrapQp": None, "wrapFrames": 0}
    return _local_encode_h264_loop(list(paths), out, fps, keyint=keyint, crf=rung["crf"], wrap_qp=rung["wrapQp"])


def keyframe_indices(path: Path, stream_index: int = 0) -> list[int]:
    """Presentation-order indices of the keyframe packets of one video stream (ffprobe)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise forge_av.ForgeAVError("ffprobe not found on PATH; install ffmpeg 5.1 or newer.")
    text = forge_av.run([ffprobe, "-v", "error", "-select_streams", str(stream_index),
                         "-show_entries", "packet=pts_time,flags", "-of", "json", str(path)])
    packets = json.loads(text or "{}").get("packets") or []

    def pts(packet: dict[str, Any]) -> float:
        try:
            return float(packet.get("pts_time"))
        except (TypeError, ValueError):
            return 0.0

    return [index for index, packet in enumerate(sorted(packets, key=pts)) if "K" in str(packet.get("flags", ""))]


# --------------------------------------------------------------------------- decoded QA

def _pixel_mae(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Per-pixel mean absolute RGB difference of two uint8 images (float32, 0-255)."""
    difference = np.maximum(a, b)
    difference -= np.minimum(a, b)
    return (difference[..., 0].astype(np.float32) + difference[..., 1] + difference[..., 2]) * np.float32(1 / 3)


def _weighted(values: np.ndarray, weights: np.ndarray, total: float) -> float:
    return float((values * weights).sum(dtype=np.float64) / total) if total > 0 else 0.0


@dataclass
class QAInputs:
    """Everything a decoded-loop QA needs besides the video; regions and cores come from the plan."""
    plate: np.ndarray                         # uint8 RGB (H, W)
    mask: np.ndarray                          # uint8 (H, W), the mask as the runtime applies it
    regions: dict[str, np.ndarray]            # region id -> support (bool)
    cores: dict[str, np.ndarray]              # protected id -> exact-zero core (bool)
    poster: np.ndarray | None = None
    expected_frames: int | None = None
    keyint: int | None = None


class _LoopMeter:
    """Accumulates every per-frame measurement while the runtime composites stream past once.

    All arrays are cropped to the analysis window (mask, leak ring and protected
    cores); outside it the runtime composite is the plate and nothing is measured.
    """

    def __init__(self, qa: QAInputs, thresholds: Thresholds) -> None:
        support = qa.mask > 0
        ring = (~support) & (motion_mask.distance_from(support, thresholds.ring_px) <= thresholds.ring_px)
        core = np.zeros(qa.mask.shape, bool)
        for value in qa.cores.values():
            core |= value
        self.window = bounding_window(support | ring | core)
        crop = self.window
        self.plate = qa.plate[crop]
        self.mask = qa.mask[crop]
        self.weight = self.mask.astype(np.float32) / 255.0
        self.weight_total = float(self.weight.sum(dtype=np.float64))
        self.ring, self.core = ring[crop], core[crop]
        self.regions = {ident: self.weight * value[crop] for ident, value in qa.regions.items()}
        self.region_totals = {ident: float(value.sum(dtype=np.float64)) for ident, value in self.regions.items()}
        self.first_comp = self.previous_comp = self.first_raw = self.previous_raw = None
        self.count = 0
        self.steps: list[float] = []
        self.region_steps: dict[str, list[float]] = {ident: [] for ident in self.regions}
        self.region_drift: dict[str, float] = {ident: 0.0 for ident in self.regions}
        self.energy: list[float] = []
        self.ring_steps: list[float] = []
        self.protected_drift = 0.0
        self.ring_static_error: float | None = None

    def add(self, raw: np.ndarray) -> np.ndarray:
        """Measure one decoded frame (already cropped to the window); returns its runtime composite."""
        comp = composite(raw, self.plate, self.mask)
        if self.first_comp is None:
            self.first_comp, self.first_raw = comp, raw
            if self.ring.any():
                self.ring_static_error = float(_pixel_mae(raw, self.plate)[self.ring].mean())
        else:
            step = _pixel_mae(comp, self.previous_comp)
            self.steps.append(_weighted(step, self.weight, self.weight_total))
            drift = _pixel_mae(comp, self.first_comp)
            self.energy.append(_weighted(drift, self.weight, self.weight_total))
            for ident, weights in self.regions.items():
                total = self.region_totals[ident]
                self.region_steps[ident].append(_weighted(step, weights, total))
                self.region_drift[ident] = max(self.region_drift[ident], _weighted(drift, weights, total))
            if self.ring.any():
                self.ring_steps.append(float(_pixel_mae(raw, self.previous_raw)[self.ring].mean()))
            if self.core.any():
                self.protected_drift = max(self.protected_drift,
                                           float(_pixel_mae(raw, self.first_raw)[self.core].mean()))
        self.previous_comp, self.previous_raw = comp, raw
        self.count += 1
        return comp

    def wrap_difference(self) -> np.ndarray:
        """|last - first| per pixel of the runtime composites (RGB 0-255, window crop): the picture of the wrap."""
        return _pixel_mae(self.previous_comp, self.first_comp)


def evaluate_video(video: Path, qa: QAInputs, thresholds: Thresholds) -> tuple[list[dict[str, Any]], dict[str, Any],
                                                                                np.ndarray]:
    """Decode ``video`` once and judge it as the runtime composites it over the plate.

    Returns (checks, metrics, seam map at plate size). The global seam is
    forge_core.seam_report of the runtime composites weighted by the mask; every
    other figure is a mean absolute RGB difference (0-255).
    """
    height, width = qa.plate.shape[:2]
    if not qa.mask.any():
        raise ValueError("the mask is empty; there is no motion to measure.")
    info = forge_av.probe(video)
    if not (width <= info["width"] <= width + 1 and height <= info["height"] <= height + 1):
        raise ValueError(f"{video.name} is {info['width']}x{info['height']}; the plate is {width}x{height} "
                         f"(the video may only add one padding column or row).")
    meter = _LoopMeter(qa, thresholds)
    rows, columns = meter.window

    def composites() -> Iterator[np.ndarray]:
        for frame in forge_av.iter_rgba(video, alpha="off"):
            yield meter.add(np.ascontiguousarray(frame[rows, columns, :3]))

    seam = forge_core.seam_report(composites(), mask=meter.mask)
    frames = meter.count
    keyframes = keyframe_indices(video, info["stream_index"])
    checks: list[dict[str, Any]] = []
    if qa.expected_frames is not None:
        checks.append({"id": "frame_count", "status": "pass" if frames == qa.expected_frames else "fail",
                       "value": frames, "threshold": qa.expected_frames})
    gop = keyframes[1] - keyframes[0] if len(keyframes) > 1 else frames
    aligned = bool(keyframes) and frames % gop == 0 and keyframes == list(range(0, frames, gop))
    if qa.keyint is not None:
        aligned = aligned and gop == qa.keyint
    checks.append({"id": "gop_aligned", "status": "pass" if aligned else "fail",
                   "value": {"keyframes": keyframes, "gop": gop, "frames": frames},
                   "threshold": "keyframes at 0, g, 2g, ... with g dividing the loop"
                                + (f" and g = {qa.keyint}" if qa.keyint is not None else "")})
    increasing = forge_av.timestamps_increasing(video)
    faststart = forge_av.moov_before_mdat(video) if "mp4" in str(info["format"] or "") else None
    checks.append({"id": "container",
                   "status": "fail" if not increasing else ("warn" if faststart is False else "pass"),
                   "value": {"timestampsIncreasing": increasing, "moovBeforeMdat": faststart},
                   "threshold": "increasing decode timestamps; moov before mdat for streaming"})
    ratio = seam["seam_over_p95"]
    checks.append({"id": "decoded_seam", "status": "pass" if ratio <= thresholds.max_seam_ratio else "fail",
                   "value": round(ratio, 6), "threshold": thresholds.max_seam_ratio})
    mean_opacity = float(qa.mask.mean() / 255.0)
    checks.append({"id": "mean_opacity", "status": "warn" if mean_opacity < thresholds.warn_mean_opacity else "pass",
                   "value": round(mean_opacity, 6), "threshold": thresholds.warn_mean_opacity})
    energy_max = max(meter.energy, default=0.0)
    checks.append({"id": "motion_energy", "status": "warn" if energy_max < thresholds.warn_motion_energy else "pass",
                   "value": round(energy_max, 6), "threshold": thresholds.warn_motion_energy})
    leak = float(np.mean(meter.ring_steps)) if meter.ring_steps else None
    checks.append({"id": "leak_ring", "status": "skipped" if leak is None else
                   ("warn" if leak > thresholds.warn_leak else "pass"),
                   "value": None if leak is None else round(leak, 6), "threshold": thresholds.warn_leak})
    if qa.cores:
        checks.append({"id": "protected_stability",
                       "status": "warn" if meter.protected_drift > thresholds.warn_protected else "pass",
                       "value": round(meter.protected_drift, 6), "threshold": thresholds.warn_protected})
    wrap = meter.wrap_difference()
    regions = []
    for ident, steps in meter.region_steps.items():
        weights, total = meter.regions[ident], meter.region_totals[ident]
        if total <= 0:
            regions.append({"id": ident, "mode": "hidden", "status": "warn"})
            continue
        region_seam = _weighted(wrap, weights, total)
        p95 = float(np.percentile(steps, 95))
        mode = "still" if p95 < STILL_FLOOR else "loop"
        region_ratio = region_seam / max(p95, 1e-6)
        stable = (meter.region_drift[ident] <= STILL_TOLERANCE if mode == "still"
                  else region_ratio <= thresholds.max_seam_ratio)
        regions.append({"id": ident, "mode": mode, "status": "pass" if stable else "warn",
                        "seam": round(region_seam, 6), "adjacentP95": round(p95, 6),
                        "seamOverP95": None if mode == "still" else round(region_ratio, 6),
                        "maxDrift": round(meter.region_drift[ident], 6)})
    if regions:
        unstable = [item["id"] for item in regions if item["status"] == "warn"]
        checks.append({"id": "region_stability", "status": "warn" if unstable else "pass",
                       "value": {"unstable": unstable,
                                 "stills": [item["id"] for item in regions if item["mode"] == "still"]},
                       "threshold": {"maxSeamRatio": thresholds.max_seam_ratio, "stillFloor": STILL_FLOOR,
                                     "stillTolerance": STILL_TOLERANCE}})
    p95_step = float(np.percentile(meter.steps, 95))
    poster = None
    if qa.poster is not None:
        swap = _weighted(_pixel_mae(qa.poster[rows, columns], meter.first_comp), meter.weight, meter.weight_total)
        outside = int(np.abs(qa.poster.astype(np.int16) - qa.plate.astype(np.int16))[qa.mask == 0].max(initial=0))
        poster = {"swapMAE": round(swap, 6), "outsideMaskMaxDelta": outside}
        checks.append({"id": "poster_swap", "status": "fail" if outside else ("warn" if swap > p95_step else "pass"),
                       "value": poster, "threshold": {"swapMAE": round(p95_step, 6), "outsideMaskMaxDelta": 0}})
    seam_map = np.zeros((height, width), np.float32)
    seam_map[rows, columns] = wrap * meter.weight
    metrics = {
        "frames": frames, "keyframes": keyframes, "gop": gop,
        "video": {"width": info["width"], "height": info["height"], "fps": info["fps_rational"],
                  "codec": info["codec"], "pixFmt": info["pix_fmt"]},
        "decodedSeam": _rounded(seam),
        "rgbSteps": {"median": round(float(np.median(meter.steps)), 6), "p95": round(p95_step, 6),
                     "max": round(max(meter.steps), 6)},
        "meanOpacity": round(mean_opacity, 6),
        "motionEnergy": {"max": round(energy_max, 6), "mean": round(float(np.mean(meter.energy)), 6)},
        "leakRing": {"ringPx": thresholds.ring_px, "pixels": int(meter.ring.sum()),
                     "meanStep": None if leak is None else round(leak, 6),
                     "maxStep": round(max(meter.ring_steps), 6) if meter.ring_steps else None,
                     "staticError": None if meter.ring_static_error is None else round(meter.ring_static_error, 6)},
        "protectedDrift": round(meter.protected_drift, 6) if qa.cores else None,
        "regions": regions, "poster": poster,
    }
    return checks, metrics, seam_map


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    return value


def seam_image(seam_map: np.ndarray) -> Image.Image:
    """The wrap difference x8 as an 8-bit grey image (white = large pop)."""
    return Image.fromarray(np.clip(np.floor(seam_map * 8.0 + 0.5), 0, 255).astype(np.uint8))


def plan_geometry(plan: dict[str, Any] | None, plate: np.ndarray) -> tuple[dict[str, np.ndarray],
                                                                            dict[str, np.ndarray]]:
    """Region supports and protected cores of a normalised plan (empty without one)."""
    if plan is None:
        return {}, {}
    build = motion_mask.build_mask(plan, plate)
    return {ident: value > 0 for ident, value in build.region_values.items()}, build.protected_cores


# --------------------------------------------------------------------------- build

@dataclass
class _Workspace:
    """Composited loop frames on disk (raw PPM, padded to even size), in loop order."""
    paths: list[Path]
    outside_max: int
    shifts: list[dict[str, Any]]


def _composite_loop(clip: motion_mask.Clip, sources: list[list[tuple[int, float]]], start: int, stop: int,
                    transform: motion_mask.Transform, plate: np.ndarray, mask: np.ndarray, coverage: np.ndarray,
                    work: Path) -> _Workspace:
    """Stream the clip once, compositing each source frame inside the mask and writing every loop frame."""
    rows, columns = bounding_window(mask > 0)
    mask_window, plate_window = mask[rows, columns], plate[rows, columns]
    outside = mask_window == 0
    static = (mask == 0) & (coverage >= 1.0)
    singles: dict[int, list[int]] = {}
    blends: list[int] = []
    for position, frame in enumerate(sources):
        if len(frame) == 1:
            singles.setdefault(frame[0][0], []).append(position)
        else:
            blends.append(position)
    pending = {source: sum(source in (s for s, _w in sources[p]) for p in blends)
               for source in {s for p in blends for s, _w in sources[p]}}
    probes = {start, (start + stop - 1) // 2, stop - 1}
    held: dict[int, np.ndarray] = {}
    paths: list[Path | None] = [None] * len(sources)
    outside_max = 0
    shifts = []

    def write(frame_window: np.ndarray, name: str) -> Path:
        nonlocal outside_max
        delta = np.abs(frame_window.astype(np.int16) - plate_window)[outside]
        outside_max = max(outside_max, int(delta.max(initial=0)))
        full = plate.copy()
        full[rows, columns] = frame_window
        path = work / name
        Image.fromarray(pad_even(full)).save(path)
        return path

    for index, rgb in clip.frames(start, stop):
        aligned = motion_mask.align_frame(rgb, transform)
        if index in probes:
            shifts.append({"frame": index, **estimate_shift(aligned, plate, static)})
        frame = composite(aligned[rows, columns], plate_window, mask_window)
        if index in singles:
            path = write(frame, f"src-{index:06d}.ppm")
            for position in singles[index]:
                paths[position] = path
        if index in pending:
            held[index] = frame
        for position in blends:
            if paths[position] is None and all(source in held for source, _w in sources[position]):
                (tail, _tail_weight), (head, head_weight) = sources[position]
                mixed = held[tail].astype(np.float32)
                mixed += (held[head].astype(np.float32) - mixed) * np.float32(head_weight)
                paths[position] = write(np.floor(mixed + 0.5).astype(np.uint8), f"blend-{position:06d}.ppm")
                for source in (tail, head):
                    pending[source] -= 1
                    if not pending[source]:
                        del held[source]
    if any(path is None for path in paths):
        raise ValueError("internal error: a loop frame was not composited.")
    return _Workspace(paths, outside_max, shifts)


def decoded_poster(video: Path, plate: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Loop frame 0 as the runtime shows it: the decoded first frame composited over the plate with the mask.

    Swapping from this poster to the playing video changes no pixel, and
    outside the mask the poster is the plate bit for bit.
    """
    height, width = plate.shape[:2]
    stream = forge_av.iter_rgba(video, alpha="off")
    try:
        first = next(stream)
    finally:
        stream.close()
    return composite(np.ascontiguousarray(first[:height, :width, :3]), plate, mask)


def _source_seam(paths: Sequence[Path], plate: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    """forge_core.seam_report of the lossless loop as the runtime composites it, weighted by the mask."""
    rows, columns = bounding_window(mask > 0)
    plate_window, mask_window = plate[rows, columns], mask[rows, columns]

    def frames() -> Iterator[np.ndarray]:
        for path in paths:
            with Image.open(path) as image:
                yield composite(np.asarray(image.convert("RGB"))[rows, columns], plate_window, mask_window)

    return forge_core.seam_report(frames(), mask=mask_window)


def _divisors(count: int) -> str:
    values = [value for value in range(1, count + 1) if count % value == 0]
    return ", ".join(map(str, values[:12])) + (", ..." if len(values) > 12 else "")


def _pre_encode_checks(loop: _Workspace, sources: list[list[tuple[int, float]]], plan: dict[str, Any],
                       source_seam: dict[str, Any], mask: np.ndarray, cores: dict[str, np.ndarray],
                       thresholds: Thresholds) -> list[dict[str, Any]]:
    reversed_count = reversed_steps(sources)
    forward_only = any(region["motion"] != "sway" for region in plan["regions"])
    union = np.zeros(mask.shape, bool)
    for core in cores.values():
        union |= core
    checks = [
        {"id": "outside_delta", "status": "pass" if loop.outside_max == 0 else "fail", "value": loop.outside_max,
         "threshold": 0},
        {"id": "forward_only", "status": "fail" if reversed_count and forward_only else "pass",
         "value": reversed_count, "threshold": "0 reversed steps unless every region is motion: sway"},
        {"id": "protected_max", "status": "pass" if not mask[union].any() else "fail",
         "value": int(mask[union].max(initial=0)), "threshold": 0},
        {"id": "source_seam", "status": "pass" if source_seam["seam_over_p95"] <= thresholds.max_seam_ratio
         else "fail", "value": round(source_seam["seam_over_p95"], 6), "threshold": thresholds.max_seam_ratio},
    ]
    measured = [item for item in loop.shifts if item["status"] == "measured"]
    moved = [item for item in measured if item["shift"] != [0, 0] and item["maeAtBest"] < 0.9 * item["maeAtZero"]]
    checks.append({"id": "registration", "status": "skipped" if not measured else ("warn" if moved else "pass"),
                   "value": [{"frame": item["frame"], "shift": item["shift"]} for item in measured],
                   "threshold": "best whole-pixel shift (0, 0) on static plate areas"})
    return checks


def cmd_build(args: argparse.Namespace) -> dict[str, Any]:
    thresholds = _thresholds(args)
    if args.edge_fade < 0:
        raise ValueError("--edge-fade must be zero or more pixels.")
    plan, document = motion_mask.load_plan(args.plan)
    plate, plate_info = motion_mask.load_plate(args.plate, plan)
    height, width = plate.shape[:2]
    clip = motion_mask.Clip(args.clip, args.fps)
    start, stop = plan["loop"]["range"] or (0, clip.count)
    if stop > clip.count:
        raise ValueError(f"loop.range ends at {stop} but the clip holds {clip.count} frames.")
    policy, overlap = plan["loop"]["policy"], plan["loop"]["overlap"]
    if policy == "pingpong":
        forward_only = [region["id"] for region in plan["regions"] if region["motion"] != "sway"]
        if forward_only:
            raise ValueError(f"pingpong plays frames backwards, and regions {', '.join(forward_only)} are flow or "
                             f"flicker (the default): water, fire and falling particles must never reverse. Use "
                             f"forward-overlap, or mark a region motion: sway after reviewing it.")
    sources = loop_frames(policy, start, stop, overlap)
    count = len(sources)
    if args.keyint is not None and args.keyint < 1:
        raise ValueError("--keyint must be at least 1.")
    keyint = args.keyint or plan["encode"]["keyint"] or count
    if count % keyint:
        raise ValueError(f"keyint {keyint} does not divide the {count}-frame loop; keyframes must land on the wrap. "
                         f"Use one of {_divisors(count)} or omit it for one GOP per loop.")
    crf = args.crf if args.crf is not None else (plan["encode"]["crf"] or DEFAULT_CRF)
    if not MIN_QP <= crf <= 51:
        raise ValueError("--crf must be between 1 and 51 (H.264 Main has no lossless mode).")
    transform = motion_mask.plan_transform(plan, clip.size, (width, height), args.fit, args.transform)
    coverage = motion_mask.coverage_map(transform, args.edge_fade)
    authored = motion_mask.load_mask(args.mask, (width, height))
    regions, cores = plan_geometry(plan, plate)
    leaks = {ident: int(authored[core].max(initial=0)) for ident, core in cores.items()}
    if any(leaks.values()):
        raise ValueError(f"the mask lets motion into protected cores {leaks}; rebuild it with build_motion_mask.py.")
    mask = np.floor(authored.astype(np.float32) * coverage + 0.5).astype(np.uint8)
    if not mask.any():
        raise ValueError("the mask is empty where the clip covers the plate; nothing would move.")
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        work = stage / ".frames"
        work.mkdir()
        loop = _composite_loop(clip, sources, start, stop, transform, plate, mask, coverage, work)
        source_seam = _source_seam(loop.paths, plate, mask)
        pre_checks = _pre_encode_checks(loop, sources, plan, source_seam, mask, cores, thresholds)
        broken = [check["id"] for check in pre_checks if check["status"] == "fail" and check["id"] != "source_seam"]
        if broken:
            raise ValueError(f"composited loop failed {', '.join(broken)}; nothing was written.")
        source_pops = source_seam["seam_over_p95"] > thresholds.max_seam_ratio
        if source_pops and not args.allow_seam_fail:
            raise ValueError(f"the loop already pops before encoding (in-mask seam {source_seam['seam_over_p95']:.3f}"
                             f" x p95 > {thresholds.max_seam_ratio:g}); move loop.range or change loop.overlap.")
        qa_inputs = QAInputs(plate, mask, regions, cores, None, count, keyint)
        attempts: list[dict[str, Any]] = []
        chosen = None
        # More bits cannot rescue a loop that pops before encoding, so it gets one encode.
        for number, rung in enumerate(encode_rungs(crf, args.ladder == "auto" and not source_pops)):
            folder = stage / f".attempt-{number}"
            folder.mkdir()
            result = encode_loop(loop.paths, folder / "loop.mp4", clip.fps, keyint, rung)
            checks, metrics, _seam_map = evaluate_video(folder / "loop.mp4", qa_inputs, thresholds)
            failed = [check["id"] for check in checks if check["status"] == "fail"]
            attempts.append({"crf": result["crf"], "wrapQp": result["wrapQp"], "wrapFrames": result["wrapFrames"],
                             "bytes": result["bytes"], "sha256": result["sha256"],
                             "decodedSeamOverP95": metrics["decodedSeam"]["seam_over_p95"],
                             "status": "fail" if failed else "pass"})
            broken = [ident for ident in failed if ident != "decoded_seam"]
            if broken:
                raise ValueError(f"decoded loop failed {', '.join(broken)}; nothing was written.")
            chosen = (folder, result, checks, metrics)
            if not failed:
                break
        folder, result, checks, metrics = chosen
        if attempts[-1]["status"] == "fail" and not args.allow_seam_fail:
            tried = "; ".join(f"crf {item['crf']} wrap {item['wrapQp']}: {item['decodedSeamOverP95']:.3f}"
                              for item in attempts)
            raise ValueError(f"the decoded loop pops at the wrap (in-mask seam / p95 above "
                             f"{thresholds.max_seam_ratio:g} after {len(attempts)} encode(s): {tried}). Regenerate "
                             f"with more motion, raise the mask strength, or pass --allow-seam-fail to keep it for "
                             f"review.")
        os.replace(folder / "loop.mp4", stage / "loop.mp4")
        forge_core.save_png(Image.fromarray(decoded_poster(stage / "loop.mp4", plate, mask)), stage / "poster.png")
        forge_core.save_png(Image.fromarray(mask), stage / "motion-mask.png")
        shutil.rmtree(work)
        for number in range(len(attempts)):
            shutil.rmtree(stage / f".attempt-{number}")
        all_checks = pre_checks + checks
        status = motion_mask.overall_status(all_checks)
        outputs = {name: motion_mask.file_ref(stage / name, stage)
                   for name in ("loop.mp4", "poster.png", "motion-mask.png")}
        plan_ref = motion_mask.file_ref(args.plan, final)
        plate_ref = motion_mask.file_ref(args.plate, final, plate_info["sha256"])
        clip_ref = clip.fingerprint(final)
        mask_ref = motion_mask.file_ref(args.mask, final)
        encoded = [int(result["width"]), int(result["height"])]
        fps = Fraction(result["fps"])
        report = {
            "schema": REPORT_SCHEMA, "tool": dict(TOOL),
            "plan": {**plan_ref, "document": document},
            "plate": {**plate_ref, "size": [width, height]},
            "clip": clip_ref, "maskSource": mask_ref,
            "registration": {**transform.describe(), "edgeFadePx": args.edge_fade,
                             "coverageMin": round(float(coverage[authored > 0].min(initial=1.0)), 6),
                             "shiftEstimates": loop.shifts},
            "loop": {"policy": policy, "overlap": overlap, "range": [start, stop], "frameCount": count,
                     "fps": result["fps"], "durationMs": round(count * 1000 / float(fps), 3),
                     "blend": "tail b-overlap+j and head a+j, head weight smoothstep((j + 1) / (overlap + 1))",
                     "reversedSteps": reversed_steps(sources),
                     "frames": [[[source, round(weight, 6)] for source, weight in frame] for frame in sources]},
            "composite": {"outsideMaskMaxDelta": loop.outside_max, "sourceSeam": _rounded(source_seam),
                          "formula": "plate + (clip - plate) * mask / 255, rounded half-up"},
            "video": {**outputs["loop.mp4"], "codec": result["codec"], "profile": result["profile"],
                      "encodedSize": encoded, "crop": [0, 0, width, height],
                      "padding": [0, 0, encoded[0] - width, encoded[1] - height],
                      "crf": result["crf"], "keyint": result["keyint"], "closedGop": result["closedGop"],
                      "keyframes": result["keyframes"], "wrapQp": result["wrapQp"],
                      "wrapFrames": result["wrapFrames"], "fps": result["fps"],
                      "decodedPixelsPerSecond": round(encoded[0] * encoded[1] * float(fps), 3)},
            "poster": {**outputs["poster.png"], "frame": 0,
                       "from": "decoded loop frame 0 composited over the plate with the mask"},
            "mask": outputs["motion-mask.png"],
            "encodeAttempts": attempts,
            "qa": {"status": status,
                   "method": "scene_motion build: composite only inside the mask (outside equal to the plate before "
                             "encoding), forward-only frame map, closed GOPs aligned to the loop, then the encoded "
                             "file decoded and composited over the plate with the mask: forge_core.seam_report "
                             "weighted by the mask plus RGB motion, leak and stability measures. The poster is "
                             "the decoded frame 0 over the plate.",
                   "notProven": list(BUILD_NOT_PROVEN), "checks": all_checks,
                   "inputs": [plan_ref, plate_ref, {"path": clip_ref["path"], "sha256": clip_ref["sha256"]}, mask_ref],
                   "outputs": list(outputs.values()), "tool": dict(TOOL)},
            "metrics": metrics, "thresholds": thresholds.describe(),
        }
        forge_core.write_json(stage / "scene-motion.json", report)
    warnings = [f"{check['id']}: {json.dumps(check['value'])}" for check in all_checks if check["status"] == "warn"]
    if attempts[-1]["status"] == "fail":
        warnings.insert(0, "decoded_seam failed and was kept by --allow-seam-fail; do not ship it as seamless.")
    return {"output_dir": str(final.resolve()), "video": str((final / "loop.mp4").resolve()),
            "poster": str((final / "poster.png").resolve()), "mask": str((final / "motion-mask.png").resolve()),
            "metadata": str((final / "scene-motion.json").resolve()), "frames": count, "fps": result["fps"],
            "crf": result["crf"], "wrap_qp": result["wrapQp"],
            "decoded_seam_over_p95": metrics["decodedSeam"]["seam_over_p95"], "qa_status": status,
            "_warnings": warnings}


# --------------------------------------------------------------------------- qa

def _from_report(report: dict[str, Any] | None, key: str, base: Path) -> Path | None:
    """A file named by a build report (paths are relative to the report's folder)."""
    value = (report or {}).get(key, {}).get("path")
    return (base / value).resolve() if isinstance(value, str) and value else None


def cmd_qa(args: argparse.Namespace) -> dict[str, Any]:
    thresholds = _thresholds(args)
    report = None
    base = Path.cwd()
    if args.report is not None:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("schema") != REPORT_SCHEMA:
            raise ValueError(f"{Path(args.report).name} is not a {REPORT_SCHEMA} report.")
        base = Path(args.report).resolve().parent
    video = args.video or _from_report(report, "video", base)
    mask_path = args.mask or _from_report(report, "mask", base)
    poster_path = args.poster or _from_report(report, "poster", base)
    if video is None or mask_path is None:
        raise ValueError("qa needs --video and --mask, or --report to find them.")
    plan = None
    if args.plan is not None:
        plan, _document = motion_mask.load_plan(args.plan)
    elif report is not None:
        plan = motion_mask.normalize_plan(report["plan"]["document"])
    plate, plate_info = motion_mask.load_plate(args.plate, plan)
    height, width = plate.shape[:2]
    mask = motion_mask.load_mask(mask_path, (width, height))
    regions, cores = plan_geometry(plan, plate)
    poster = None
    if poster_path is not None:
        image, _info = forge_core.load_rgba(poster_path)
        if image.size != (width, height):
            raise ValueError(f"poster is {image.size[0]}x{image.size[1]}; the plate is {width}x{height}.")
        poster = np.ascontiguousarray(np.asarray(image)[..., :3])
    expected = args.expect_frames or (report["loop"]["frameCount"] if report else None)
    keyint = args.keyint or (report["video"]["keyint"] if report else None)
    qa_inputs = QAInputs(plate, mask, regions, cores, poster, expected, keyint)
    checks, metrics, seam_map = evaluate_video(Path(video), qa_inputs, thresholds)
    status = motion_mask.overall_status(checks)
    if args.strict and status == "fail":
        failed = ", ".join(check["id"] for check in checks if check["status"] == "fail")
        raise ValueError(f"strict QA failed ({failed}); nothing was written.")
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        forge_core.save_png(seam_image(seam_map), stage / "seam-diff.png")
        inputs = [motion_mask.file_ref(video, final), motion_mask.file_ref(args.plate, final, plate_info["sha256"]),
                  motion_mask.file_ref(mask_path, final)]
        if poster_path is not None:
            inputs.append(motion_mask.file_ref(poster_path, final))
        document = {
            "schema": LOOP_QA_SCHEMA, "status": status,
            "method": "scene_motion qa: the video decoded once with ffmpeg and composited over the plate with the "
                      "mask (decoded * mask + plate * (1 - mask)); forge_core.seam_report weighted by the mask for "
                      "the wrap, mean absolute RGB differences for motion, leak, stability and poster swap; ffprobe "
                      "packets for keyframes.",
            "notProven": list(QA_NOT_PROVEN), "checks": checks, "inputs": inputs,
            "outputs": [motion_mask.file_ref(stage / "seam-diff.png", stage)], "tool": dict(TOOL),
            "metrics": metrics, "thresholds": thresholds.describe(),
        }
        forge_core.write_json(stage / "loop-qa.json", document)
    return {"output_dir": str(final.resolve()), "metadata": str((final / "loop-qa.json").resolve()),
            "seam_diff": str((final / "seam-diff.png").resolve()), "status": status,
            "decoded_seam_over_p95": metrics["decodedSeam"]["seam_over_p95"],
            "_warnings": [f"{check['id']}: {json.dumps(check['value'])}" for check in checks
                          if check["status"] == "warn"]}


# --------------------------------------------------------------------------- command line

def _thresholds(args: argparse.Namespace) -> Thresholds:
    values = Thresholds(args.max_seam_ratio, args.warn_mean_opacity, args.warn_motion_energy, args.warn_leak,
                        args.warn_protected, args.ring_px)
    numbers = (values.max_seam_ratio, values.warn_mean_opacity, values.warn_motion_energy, values.warn_leak,
               values.warn_protected)
    if not all(math.isfinite(value) and value >= 0 for value in numbers):
        raise ValueError("QA thresholds must be finite and zero or more.")
    if values.ring_px < 1:
        raise ValueError("--ring-px must be at least 1.")
    return values


def _threshold_options(parser: argparse.ArgumentParser) -> None:
    defaults = Thresholds()
    parser.add_argument("--max-seam-ratio", type=float, default=defaults.max_seam_ratio,
                        help=f"Fail when the decoded in-mask seam exceeds this many adjacent p95 steps "
                             f"(default {defaults.max_seam_ratio:g}).")
    parser.add_argument("--warn-mean-opacity", type=float, default=defaults.warn_mean_opacity,
                        help=f"Warn below this whole-plate mean mask opacity (default {defaults.warn_mean_opacity:g}).")
    parser.add_argument("--warn-motion-energy", type=float, default=defaults.warn_motion_energy,
                        help=f"Warn when in-mask motion never exceeds this mean RGB difference from frame 0 "
                             f"(default {defaults.warn_motion_energy:g}).")
    parser.add_argument("--warn-leak", type=float, default=defaults.warn_leak,
                        help=f"Warn when the decoded ring outside the mask changes by more than this per frame "
                             f"(default {defaults.warn_leak:g}).")
    parser.add_argument("--warn-protected", type=float, default=defaults.warn_protected,
                        help=f"Warn when protected cores drift more than this in the decoded video "
                             f"(default {defaults.warn_protected:g}).")
    parser.add_argument("--ring-px", type=int, default=defaults.ring_px,
                        help=f"Width of the leak ring outside the mask (default {defaults.ring_px}).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    build = verbs.add_parser("build", help="Composite, loop, encode and gate a masked scene loop.",
                             description="Composite the clip inside the mask on the plate, assemble the plan's "
                                         "loop, encode it GOP-aligned and gate the decoded file.")
    build.add_argument("--plan", type=Path, required=True, help="motion-plan.json (generate2dmap.motion_plan.v1).")
    build.add_argument("--plate", type=Path, required=True, help="The accepted static plate (opaque PNG).")
    build.add_argument("--clip", type=Path, required=True, help="Provider clip: video file or folder of PNG frames.")
    build.add_argument("--mask", type=Path, required=True, help="motion-mask.png from build_motion_mask.py.")
    build.add_argument("--output-dir", type=Path, required=True, help="New folder for the outputs; must not exist.")
    motion_mask.add_registration_options(build)
    build.add_argument("--crf", type=int, help=f"H.264 quality, lower is better (default plan.encode.crf or "
                                               f"{DEFAULT_CRF}).")
    build.add_argument("--keyint", type=int, help="GOP length; must divide the loop (default plan.encode.keyint or "
                                                  "one GOP per loop).")
    build.add_argument("--ladder", choices=("auto", "off"), default="auto",
                       help="auto (default): after a failed decoded seam, retry with a lower QP at the wrap "
                            "(crf-8, crf-12, crf-14), then also a lower crf. off: encode once.")
    build.add_argument("--allow-seam-fail", action="store_true",
                       help="Publish a loop whose seam still fails, marked fail, for review.")
    build.add_argument("--edge-fade", type=float, default=DEFAULT_EDGE_FADE,
                       help=f"Fade masked motion over this many px inside clip edges that do not reach the plate "
                            f"border (default {DEFAULT_EDGE_FADE:g}).")
    _threshold_options(build)

    qa = verbs.add_parser("qa", help="Measure a decoded scene loop over its plate.",
                          description="Decode a scene loop and measure it as the runtime composites it over the "
                                      "plate; writes loop-qa.json and seam-diff.png.")
    qa.add_argument("--plate", type=Path, required=True, help="The static plate the loop belongs to.")
    qa.add_argument("--output-dir", type=Path, required=True, help="New folder for the outputs; must not exist.")
    qa.add_argument("--report", type=Path, help="scene-motion.json from build: supplies the video, mask, poster, "
                                                "plan, frame count and keyint.")
    qa.add_argument("--video", type=Path, help="The loop video (default from --report).")
    qa.add_argument("--mask", type=Path, help="The mask the runtime applies (default from --report).")
    qa.add_argument("--plan", type=Path, help="motion-plan.json for per-region and protected-core checks.")
    qa.add_argument("--poster", type=Path, help="Poster shown before the video plays (default from --report).")
    qa.add_argument("--expect-frames", type=int, help="Frame count the loop must have.")
    qa.add_argument("--keyint", type=int, help="GOP length the loop must use.")
    qa.add_argument("--strict", action="store_true", help="Publish nothing when the QA status is fail.")
    _threshold_options(qa)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        summary = cmd_build(args) if args.verb == "build" else cmd_qa(args)
    except (ValueError, OSError, forge_av.ForgeAVError, Image.DecompressionBombError) as error:
        print(f"error: {forge_core.ascii_text(str(error) or type(error).__name__)}", file=sys.stderr)
        return 1
    except Exception as error:  # never show a traceback to the user
        print(f"error: unexpected {type(error).__name__}: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if args.verb == "qa" and summary["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
