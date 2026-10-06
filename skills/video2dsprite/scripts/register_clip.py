#!/usr/bin/env python3
"""Register image-to-video clips by construction, and judge raw takes.

  apply             Map keyed frames (for example video2dsprite process frames-clean)
                    onto the master canvas with the one fixed transform recorded by
                    prepare_i2v_input.py: premultiplied resampling, action padding that
                    grows the canvas and shifts the anchor (never shrinks the body),
                    optional feet/x/hip locks in whole output pixels, and an actor or
                    fx profile (edge fade, dissolve tail, displayScale), then the alpha
                    hygiene floor (alpha <= 4 cleared: invisible resampling halo). Writes
                    frames/, registration.json and review-contact.png to a new
                    --output-dir after registration QA.
  profile           Write a character profile (video2dsprite.character_profile.v1) from
                    registered clips; later clips inherit it with apply
                    --character-profile.
  validate-profile  Check registered clips against a profile and report every clip's
                    rest-pose foot line (nativeFootBottomRange).
  qc                Judge a raw take against its job: landmark and identity NCC
                    (36 px patches, +-15 px search), camera drift and scale in the
                    calm spans, edge key purity. Appends one take.v1 line to
                    takes.jsonl (an audit log: kept and rejected takes exit 0;
                    --strict exits 1 on a rejection, the line still written).
  palette-repair    Remap off-palette flashes inside given regions to colours sampled
                    from the master. Alpha never changes; writes an audit report.

Registration is by construction, not by bounding box: every frame of a clip uses
the same scale and translation, so pose, height and contact changes survive.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_matte  # noqa: E402
import prepare_i2v_input as prep  # noqa: E402  (sibling script: action templates, placement, key helpers)

TOOL_NAME = "register_clip"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # QA envelopes record the package version (D29)
REGISTRATION_SCHEMA = "video2dsprite.registration.v1"
PROFILE_SCHEMA = "video2dsprite.character_profile.v1"
TAKE_SCHEMA = "video2dsprite.take.v1"
PALETTE_REPAIR_SCHEMA = "video2dsprite.palette_repair.v1"
REGISTRATION_FILE = "registration.json"
PALETTE_REPAIR_FILE = "palette-repair.json"

FITS = ("auto", "stretch", "cover", "contain")
LOCKS = ("feet", "x", "hip")
PROFILES = ("actor", "fx")
THRESHOLD = forge_core.ALPHA_GEOMETRY_THRESHOLD   # subject pixels: alpha > 16 (edge and overflow checks)
CONTOUR = 127   # body measurements compared with the crisp master use the 50% alpha contour,
                # which resampling blur leaves in place (alpha > 16 grows a resampled body by ~1 px a side)

DEFAULT_MAX_ANCHOR_ERROR = 2.0    # output px between the rest frame and the master (same measurement)
DEFAULT_MAX_SCALE_ERROR = 0.03    # amber-quay measured 2.2-4.8% body-scale pumping as visible
DEFAULT_GROUND_MIN_RUN = 3        # px a row needs to count as the ground line (ignores specks)
LOCK_MARGIN = 0.06                # locked frames are registered once with this much room (share of the canvas)
FX_EDGE_FADE = 0.085              # Dusk FX: 46 px boundary falloff on a 540 px canvas
FX_FADE_IN_FRAMES = 2             # Dusk FX: transparent first frame, 50 ms appearance
FX_DISSOLVE_TAIL = 0.2            # Dusk FX: final 20% alpha dissolve, at least 4 frames
FX_DISSOLVE_MIN_FRAMES = 4

QC_PATCH = 36                     # Dusk qa-registration.py: 36 px zero-mean NCC patches
QC_SEARCH = 15                    # ... searched +-15 px
QC_SUBJECT_DISTANCE = 64.0        # RGB distance from the estimated key that counts as subject in raw frames
QC_CHANGE_LEVELS = 16             # luma change that counts as motion, above codec noise
DEFAULT_MAX_SCALE_CHANGE = 0.03
DEFAULT_MAX_DRIFT = 2.0           # source px
DEFAULT_MIN_LANDMARK_NCC = 0.6
DEFAULT_MIN_IDENTITY_NCC = 0.5
DEFAULT_MAX_EDGE_IMPURE = 0.0005  # share of the border band
HYGIENE_FLOOR = 4                 # D18: registered frames lose alpha <= 4 (forge_core.alpha_hygiene floor)
MATTE_MODES = ("soft", "dominance", "binary")


class RegistrationQAError(ValueError):
    """Registration QA failed; nothing was published."""


# --------------------------------------------------------------------------- small helpers

def _ascii(text: Any) -> str:
    return forge_core.ascii_text(str(text))


_round_half_up = forge_core.round_half_up  # floor(value + 1/2), never banker's rounding (D30)


def _natural_key(path: Path) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def list_frames(directory: str | Path, pattern: str = "*.png") -> list[Path]:
    folder = Path(directory)
    if not folder.is_dir():
        raise ValueError(f"Frames folder not found: {folder}")
    files = sorted((path for path in folder.glob(pattern) if path.is_file()), key=_natural_key)
    if not files:
        raise ValueError(f"No frames matching {pattern} in {folder}")
    return files


def ref_path(path: str | Path, base: str | Path) -> str:
    """Manifest-relative POSIX path, or just the file name when no relative path exists
    (another drive): manifests never hold absolute paths (forge_core.manifest_path, D30)."""
    return forge_core.manifest_path(path, base)


def _file_ref(path: Path, base: Path, digest: str | None = None) -> dict[str, Any]:
    """A common fileRef {path, sha256, bytes} (forge_core.file_ref, D30)."""
    return forge_core.file_ref(path, base, sha256=digest)


def _load_pixels(path: Path) -> tuple[np.ndarray, str]:
    image, info = forge_core.load_rgba(path)
    return np.asarray(image), info["sha256"]


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return None
    columns = np.flatnonzero(mask.any(axis=0))
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


def _box_sum(values: np.ndarray, height: int, width: int) -> np.ndarray:
    """Sums of every height x width window (valid positions only), via a summed-area table."""
    table = np.zeros((values.shape[0] + 1, values.shape[1] + 1), np.float64)
    table[1:, 1:] = values.astype(np.float64).cumsum(0).cumsum(1)
    return table[height:, width:] - table[:-height, width:] - table[height:, :-width] + table[:-height, :-width]


def parse_range(text: str) -> tuple[int, int | None]:
    match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d*)\s*", str(text))
    if not match:
        raise argparse.ArgumentTypeError(f"--range needs START:END (0-based, end exclusive); got {text!r}")
    start, end = int(match.group(1)), (int(match.group(2)) if match.group(2) else None)
    if end is not None and end <= start:
        raise argparse.ArgumentTypeError(f"--range end must be after start; got {text!r}")
    return start, end


def parse_frames_list(text: str) -> list[int]:
    """Comma-separated frame indices; 'none' is the empty list."""
    if str(text).strip().lower() == "none":
        return []
    try:
        values = sorted({int(part) for part in str(text).split(",") if part.strip()})
    except ValueError:
        raise argparse.ArgumentTypeError(f"--rest-frames needs none or comma-separated indices; got {text!r}") from None
    if not values or values[0] < 0:
        raise argparse.ArgumentTypeError(f"--rest-frames needs none or indices >= 0; got {text!r}")
    return values


def parse_locks(text: str) -> list[str]:
    locks = [part.strip().lower() for part in str(text).split(",") if part.strip()]
    if not locks or any(lock not in LOCKS for lock in locks):
        raise argparse.ArgumentTypeError(f"--lock takes {', '.join(LOCKS)} (comma-separated); got {text!r}")
    if "x" in locks and "hip" in locks:
        raise argparse.ArgumentTypeError("--lock x and hip both pin x; pick one")
    return sorted(set(locks), key=LOCKS.index)


def parse_hue(text: str) -> tuple[float, float]:
    low, high = prep.parse_numbers(text, 2, "a hue range H0,H1")
    if not (0 <= low <= 360 and 0 <= high <= 360):
        raise argparse.ArgumentTypeError(f"hues are degrees 0-360; got {text!r}")
    return low, high


# --------------------------------------------------------------------------- the job

@dataclass(frozen=True)
class Job:
    """registration_job.v1 plus the geometry derived from it."""

    path: Path
    sha256: str
    data: dict[str, Any]
    master_path: Path
    master_sha256: str
    master_size: tuple[int, int]
    view_box: tuple[int, int, int, int] | None
    source_size: tuple[int, int]
    source_anchor: tuple[float, float]
    canvas: tuple[int, int]
    scale: float
    offset: tuple[float, float]
    key: str
    key_rgb: tuple[int, int, int]
    padding: tuple[int, int, int, int]
    action: str
    returns_to_rest: bool
    pixel_art: bool
    anchor_mode: str
    margin: float


def _pair(value: Any, name: str, *, positive: bool = False, integer: bool = False) -> tuple:
    ok = isinstance(value, list) and len(value) == 2 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in value)
    if ok and integer:
        ok = all(float(item).is_integer() for item in value)
    if ok and positive:
        ok = all(item > 0 for item in value)
    if not ok:
        raise ValueError(f"The job's {name} must be two {'positive ' if positive else ''}"
                         f"{'integers' if integer else 'numbers'}; got {value!r}")
    return (int(value[0]), int(value[1])) if integer else (float(value[0]), float(value[1]))


def _job_key(value: Any) -> str:
    """The job's keyColor as a key name or #rrggbb (the key actually used, never auto)."""
    if isinstance(value, list) and len(value) == 3 and all(isinstance(v, int) and 0 <= v <= 255 for v in value):
        return "#" + "".join(f"{int(v):02x}" for v in value)
    if isinstance(value, str) and value.strip().lower() != "auto":
        try:
            return prep.normalise_key(value)
        except argparse.ArgumentTypeError:
            pass
    raise ValueError(f"The job's keyColor must be the key used: magenta, green, blue, #rrggbb or [r, g, b]; "
                     f"got {value!r}")


def load_job(path: str | Path) -> Job:
    """Read a registration_job.v1 (prepare_i2v_input output or hand-written with the
    required fields only) and derive the source canvas, anchor and transform."""
    path = Path(path)
    raw = path.read_bytes()
    try:
        data = forge_core.parse_json(raw)  # UTF-8 with or without a BOM (D28)
    except ValueError:
        raise ValueError(f"{path.name} is not valid JSON") from None
    if not isinstance(data, dict) or data.get("schema") != prep.JOB_SCHEMA:
        raise ValueError(f"{path.name} is not a {prep.JOB_SCHEMA} document")
    master = data.get("master")
    if not (isinstance(master, dict) and isinstance(master.get("path"), str)
            and isinstance(master.get("sha256"), str)):
        raise ValueError("The job needs master.path and master.sha256")
    master_size = _pair(master.get("size"), "master.size", positive=True, integer=True)
    master_anchor = _pair(master.get("anchor"), "master.anchor")
    view_box = None
    origin = (0.0, 0.0)
    source_size = master_size
    if master.get("viewBox") is not None:
        box = master["viewBox"]
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, int) for v in box)
                and 0 <= box[0] < box[2] <= master_size[0] and 0 <= box[1] < box[3] <= master_size[1]):
            raise ValueError(f"The job's master.viewBox must be an integer box inside the master; got {box!r}")
        view_box = tuple(box)
        origin = (float(box[0]), float(box[1]))
        source_size = (box[2] - box[0], box[3] - box[1])
    source_anchor = (master_anchor[0] - origin[0], master_anchor[1] - origin[1])
    if "sourceSize" in data and _pair(data["sourceSize"], "sourceSize", positive=True, integer=True) != source_size:
        raise ValueError("The job's sourceSize disagrees with master.size/viewBox")
    if "sourceAnchor" in data:
        stated = _pair(data["sourceAnchor"], "sourceAnchor")
        if max(abs(stated[0] - source_anchor[0]), abs(stated[1] - source_anchor[1])) > 1e-6:
            raise ValueError("The job's sourceAnchor disagrees with master.anchor and viewBox")
    canvas = _pair(data.get("referenceCanvas"), "referenceCanvas", positive=True, integer=True)
    scale = data.get("referenceScale")
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not math.isfinite(scale) or scale <= 0:
        raise ValueError(f"The job's referenceScale must be a positive number; got {scale!r}")
    offset = _pair(data.get("referenceOffset"), "referenceOffset")
    padding = data.get("padding")
    if not (isinstance(padding, list) and len(padding) == 4 and all(isinstance(v, int) and v >= 0 for v in padding)):
        raise ValueError(f"The job's padding must be four integers >= 0; got {padding!r}")
    key = _job_key(data.get("keyColor"))
    action = str(data.get("action") or "")
    if not action:
        raise ValueError("The job needs an action")
    template = prep.ACTIONS.get(action)
    returns = data.get("returnsToRest")
    returns = bool(returns) if isinstance(returns, bool) else bool(template and template.returns_to_rest)
    master_path = (path.parent / master["path"]).resolve()
    margin = data.get("margin", prep.DEFAULT_MARGIN)
    return Job(path=path.resolve(), sha256=forge_core.sha256_bytes(raw), data=data, master_path=master_path,
               master_sha256=master["sha256"], master_size=master_size, view_box=view_box,
               source_size=source_size, source_anchor=source_anchor, canvas=canvas, scale=float(scale),
               offset=offset, key=key, key_rgb=prep.key_rgb(key), padding=tuple(padding), action=action,
               returns_to_rest=returns, pixel_art=bool(data.get("pixelArt", False)),
               anchor_mode=str(data.get("anchorMode") or "stance"),
               margin=float(margin) if isinstance(margin, (int, float)) else float(prep.DEFAULT_MARGIN))


def load_master_view(job: Job) -> np.ndarray:
    """The master (or its view box) as RGBA, after checking it is the file the job was made from."""
    if not job.master_path.is_file():
        raise ValueError(f"The job's master {job.data['master']['path']} is not next to {job.path.name}")
    pixels, digest = _load_pixels(job.master_path)
    if digest != job.master_sha256:
        raise ValueError(f"{job.master_path.name} changed since the job was prepared (sha256 mismatch)")
    if job.view_box:
        x0, y0, x1, y1 = job.view_box
        pixels = pixels[y0:y1, x0:x1]
    return np.ascontiguousarray(pixels)


def measure_mode(job: Job) -> str:
    """The anchor measurement used for rest checks (explicit anchors are compared in stance mode)."""
    return job.anchor_mode if job.anchor_mode in forge_core.ANCHOR_MODES else "stance"


# --------------------------------------------------------------------------- the transform

@dataclass(frozen=True)
class VideoFit:
    """How the provider canvas maps onto the returned video: video = k * reference + c."""

    requested: str
    applied: str
    size: tuple[int, int]
    k: tuple[float, float]
    c: tuple[float, float]


def video_fit(canvas: Sequence[int], size: Sequence[int], fit: str) -> VideoFit:
    """stretch: separate x/y factors (sx = W/1280, sy = H/720). cover: one factor, centred
    crop (Grok's 1264x720 from 1280x720 is a fixed centre crop). contain: one factor,
    centred letterbox. auto: one factor when the aspect ratios match (960x960 from a square
    input), otherwise cover."""
    (ref_w, ref_h), (width, height) = canvas, size
    ratio_x, ratio_y = width / ref_w, height / ref_h
    same_aspect = width * ref_h == height * ref_w
    applied = ("uniform" if same_aspect else "cover") if fit == "auto" else fit
    if applied == "stretch":
        return VideoFit(fit, applied, (width, height), (ratio_x, ratio_y), (0.0, 0.0))
    factor = ratio_x if applied == "uniform" else (max if applied == "cover" else min)(ratio_x, ratio_y)
    centre = ((width - factor * ref_w) / 2, (height - factor * ref_h) / 2)
    return VideoFit(fit, applied, (width, height), (factor, factor), centre)


def anchor_in_video(job: Job, fit: VideoFit) -> tuple[float, float]:
    """Video position of the source anchor: k * (offset + scale * anchor) + c."""
    ax, ay = job.source_anchor
    return (fit.k[0] * (job.offset[0] + job.scale * ax) + fit.c[0],
            fit.k[1] * (job.offset[1] + job.scale * ay) + fit.c[1])


def output_scale(job: Job, fit: VideoFit) -> tuple[float, float]:
    """Output px per video px on each axis."""
    return 1.0 / (fit.k[0] * job.scale), 1.0 / (fit.k[1] * job.scale)


def video_to_output(job: Job, fit: VideoFit, padding: Sequence[int], point: Sequence[float],
                    shift: Sequence[int] = (0, 0)) -> tuple[float, float]:
    sx, sy = output_scale(job, fit)
    vx, vy = anchor_in_video(job, fit)
    return (job.source_anchor[0] + padding[0] + shift[0] + (point[0] - vx) * sx,
            job.source_anchor[1] + padding[1] + shift[1] + (point[1] - vy) * sy)


def _local_unpremultiply(planes: Sequence[np.ndarray]) -> np.ndarray:
    """forge_core's unpremultiply: colour divided by the unclipped alpha, RGB zeroed under alpha 0."""
    alpha = planes[3]
    alpha8 = np.floor(np.clip(alpha, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    premultiplied = np.clip(np.stack(planes[:3], axis=-1), 0.0, None)
    rgb = np.clip(np.floor(premultiplied / np.maximum(alpha, 1e-6)[..., None] + 0.5), 0, 255).astype(np.uint8)
    rgb[alpha8 == 0] = 0
    return np.dstack([rgb, alpha8])


_LOCAL_FILTERS = {"box": (Image.Resampling.BOX, 0.5), "lanczos": (Image.Resampling.LANCZOS, 3.0)}


def _local_resample_anisotropic(pixels: np.ndarray, scale_xy: Sequence[float], resampler: str,
                                anchor_src: Sequence[float], anchor_dst: Sequence[float],
                                out_size: Sequence[int]) -> np.ndarray:
    """forge_core.resample_rgba's anchor-pinned path with separate x and y scales.

    Needed only for --fit stretch with a provider that changed the aspect ratio
    (handoff section 6 asks to promote it as resample_rgba(scale=(sx, sy))). Same
    premultiplied float planes, the same box filter for reductions of 2x or more and
    the same padded canvas, so Pillow never renormalises a clipped window.
    """
    if resampler == "nearest":
        raise ValueError("nearest needs one integer scale; a stretched fit has two scales (use lanczos or box)")
    scale_x, scale_y = float(scale_xy[0]), float(scale_xy[1])
    resample_filter, support = _LOCAL_FILTERS["box" if resampler == "box" or min(scale_x, scale_y) <= 0.5
                                              else resampler]
    pad_x = math.ceil(support * max(1.0, 1.0 / scale_x)) + 2
    pad_y = math.ceil(support * max(1.0, 1.0 / scale_y)) + 2
    base_x, base_y = math.floor(anchor_src[0]), math.floor(anchor_src[1])
    left = anchor_src[0] - base_x - anchor_dst[0] / scale_x
    top = anchor_src[1] - base_y - anchor_dst[1] / scale_y
    span_x, span_y = out_size[0] / scale_x, out_size[1] / scale_y
    col0, row0 = math.floor(left) - pad_x, math.floor(top) - pad_y
    col1, row1 = math.ceil(left + span_x) + pad_x, math.ceil(top + span_y) + pad_y
    canvas = np.zeros((row1 - row0, col1 - col0, 4), np.float32)
    height, width = pixels.shape[:2]
    src_x0, src_x1 = max(0, base_x + col0), min(width, base_x + col1)
    src_y0, src_y1 = max(0, base_y + row0), min(height, base_y + row1)
    if src_x0 < src_x1 and src_y0 < src_y1:
        region = pixels[src_y0:src_y1, src_x0:src_x1].astype(np.float32)
        alpha = region[..., 3:] / 255.0
        cy, cx = src_y0 - (base_y + row0), src_x0 - (base_x + col0)
        canvas[cy:cy + region.shape[0], cx:cx + region.shape[1], :3] = region[..., :3] * alpha
        canvas[cy:cy + region.shape[0], cx:cx + region.shape[1], 3] = alpha[..., 0]
    box = (left - col0, top - row0, left - col0 + span_x, top - row0 + span_y)
    planes = [np.asarray(Image.fromarray(np.ascontiguousarray(canvas[..., channel])).resize(
        tuple(int(v) for v in out_size), resample_filter, box=box)) for channel in range(4)]
    return _local_unpremultiply(planes)


def register_frame(pixels: np.ndarray, job: Job, fit: VideoFit, padding: Sequence[int],
                   out_size: Sequence[int], resampler: str, shift: Sequence[int] = (0, 0)) -> np.ndarray:
    """One frame through the clip's single inverse transform: the job anchor lands exactly
    on (anchor + padding + shift) with the exact scale (premultiplied, grid pinned)."""
    scale_x, scale_y = output_scale(job, fit)
    source = anchor_in_video(job, fit)
    target = (job.source_anchor[0] + padding[0] + shift[0], job.source_anchor[1] + padding[1] + shift[1])
    if scale_x == scale_y:
        return np.asarray(forge_core.resample_rgba(pixels, scale_x, resampler, anchor_src=source, anchor_dst=target,
                                                   out_size=tuple(out_size)))
    return _local_resample_anisotropic(pixels, (scale_x, scale_y), resampler, source, target, out_size)


def place_in_video(view: np.ndarray, job: Job, fit: VideoFit) -> np.ndarray:
    """The master as the provider was asked to reproduce it, in video px (perfect matte)."""
    scale = (fit.k[0] * job.scale, fit.k[1] * job.scale)
    target = (fit.k[0] * job.offset[0] + fit.c[0], fit.k[1] * job.offset[1] + fit.c[1])
    if scale[0] == scale[1]:
        integral = all(float(value).is_integer() for value in (scale[0], *target))
        resampler = "nearest" if job.pixel_art and integral else "lanczos"
        return np.asarray(forge_core.resample_rgba(view, scale[0], resampler, anchor_src=(0.0, 0.0),
                                                   anchor_dst=target, out_size=fit.size))
    return _local_resample_anisotropic(view, scale, "lanczos", (0.0, 0.0), target, fit.size)


# --------------------------------------------------------------------------- silhouette measurements

@dataclass(frozen=True)
class Silhouette:
    """The body inside the 50% alpha contour."""

    mask: np.ndarray
    box: tuple[int, int, int, int]
    ground: int        # bottom edge of the lowest row with >= min_run body px
    centroid_x: float
    area: int

    @property
    def height(self) -> int:
        return self.ground - self.box[1]


def silhouette(alpha: np.ndarray, min_run: int) -> Silhouette | None:
    mask = alpha > CONTOUR
    box = _bbox(mask)
    if box is None:
        return None
    try:
        ground = forge_core.ground_row(mask, min_run=min_run)
    except ValueError:
        ground = box[3]
    columns = np.nonzero(mask)[1]
    return Silhouette(mask, box, ground, float(columns.mean()) + 0.5, int(columns.size))


def hip_x(mask: np.ndarray, ground: float, height: float) -> float | None:
    """Body root x (game-opus55 _hip): per row of the hip band (0.55 to 0.25 of the rest height
    above the ground line), the centre of the widest run; blades and scarf tips are never it."""
    first = max(0, _round_half_up(ground - 0.55 * height))
    last = min(mask.shape[0], _round_half_up(ground - 0.25 * height))
    centres = []
    for row in mask[first:last]:
        edges = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
        if starts.size:
            widest = int(np.argmax(ends - starts))
            centres.append((starts[widest] + ends[widest]) / 2)
    return float(np.median(centres)) if centres else None


def anchor_band(height: int) -> int:
    return max(2, _round_half_up(0.04 * height))


# --------------------------------------------------------------------------- profiles

def _identity_from_job(job: Job) -> dict[str, Any]:
    return {"masterSha256": job.master_sha256, "sourceSize": list(job.source_size),
            "sourceAnchor": list(job.source_anchor), "scale": job.scale, "anchorMode": job.anchor_mode}


def _identity_from_registration(doc: dict[str, Any]) -> dict[str, Any]:
    return {"masterSha256": doc["master"]["sha256"], "sourceSize": doc["baseSize"], "sourceAnchor": doc["baseAnchor"],
            "scale": doc["referenceScale"], "anchorMode": doc["anchorMode"]}


def _identity_from_profile(doc: dict[str, Any]) -> dict[str, Any]:
    registration = doc["registration"]
    return {"masterSha256": registration.get("masterSha256"), "sourceSize": doc["sourceSize"],
            "sourceAnchor": doc["sourceAnchor"], "scale": registration["scale"],
            "anchorMode": registration["anchorMode"]}


def identity_mismatches(expected: dict[str, Any], actual: dict[str, Any], scale_tolerance: float) -> list[str]:
    """Differences that would make two clips of one character disagree (master, canvas,
    anchor, reference scale, anchor mode)."""
    problems = []
    shas = expected.get("masterSha256"), actual.get("masterSha256")
    if all(shas) and shas[0] != shas[1]:
        problems.append("master differs (sha256 mismatch)")
    if list(expected["sourceSize"]) != list(actual["sourceSize"]):
        problems.append(f"sourceSize {list(actual['sourceSize'])} != {list(expected['sourceSize'])}")
    if max(abs(float(a) - float(b)) for a, b in zip(expected["sourceAnchor"], actual["sourceAnchor"])) > 0.01:
        problems.append(f"sourceAnchor {list(actual['sourceAnchor'])} != {list(expected['sourceAnchor'])}")
    reference = float(expected["scale"])
    if abs(float(actual["scale"]) - reference) > scale_tolerance * reference:
        problems.append(f"referenceScale {float(actual['scale']):g} != {reference:g} (tolerance "
                        f"{scale_tolerance:.1%}; a different scale changes the generated detail and motion size)")
    if expected["anchorMode"] != actual["anchorMode"]:
        problems.append(f"anchorMode {actual['anchorMode']} != {expected['anchorMode']}")
    return problems


def _load_document(path: str | Path, schema: str) -> dict[str, Any]:
    path = Path(path)
    try:
        data = forge_core.read_json(path)  # UTF-8 with or without a BOM (D28)
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read {path.name}: {error}") from None
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise ValueError(f"{path.name} is not a {schema} document")
    return data


def load_registration(path: str | Path) -> dict[str, Any]:
    data = _load_document(path, REGISTRATION_SCHEMA)
    for field in ("master", "baseSize", "baseAnchor", "referenceScale", "anchorMode", "clip", "padding",
                  "sourceSize", "sourceAnchor", "mode"):
        if field not in data:
            raise ValueError(f"{Path(path).name} lacks {field}")
    return data


def load_profile(path: str | Path) -> dict[str, Any]:
    data = _load_document(path, PROFILE_SCHEMA)
    if not isinstance(data.get("registration"), dict) or "scale" not in data["registration"]:
        raise ValueError(f"{Path(path).name} lacks registration.scale")
    for field in ("sourceSize", "sourceAnchor", "clips"):
        if field not in data:
            raise ValueError(f"{Path(path).name} lacks {field}")
    return data


# --------------------------------------------------------------------------- apply

def _fx_edge_fade(job: Job, fit: VideoFit, padding: Sequence[int], out_size: Sequence[int],
                  fade_px: float) -> tuple[np.ndarray, list[float]]:
    """Smoothstep falloff toward the edge of what the provider could draw: the output canvas
    clipped to the video footprint, so escaping energy dissipates before any hard edge."""
    x0, y0 = video_to_output(job, fit, padding, (0.0, 0.0))
    x1, y1 = video_to_output(job, fit, padding, fit.size)
    rect = [max(0.0, x0), max(0.0, y0), min(float(out_size[0]), x1), min(float(out_size[1]), y1)]
    xs = np.arange(out_size[0]) + 0.5
    ys = np.arange(out_size[1]) + 0.5
    # distance of each pixel centre from the edge, minus half a pixel: the outermost pixels are 0
    dx = np.minimum(xs - rect[0], rect[2] - xs) - 0.5
    dy = np.minimum(ys - rect[1], rect[3] - ys) - 0.5
    distance = np.minimum(dy[:, None], dx[None, :])
    ramp = np.clip(distance / max(fade_px, 1e-6), 0.0, 1.0)
    return ramp * ramp * (3.0 - 2.0 * ramp), [round(value, 4) for value in rect]


def _fx_time_fade(index: int, count: int, fade_in: int, tail: int) -> float:
    """Dusk FX alpha envelope: min(1, i / fade_in, (n - 1 - i) / tail)."""
    factor = 1.0
    if fade_in > 0:
        factor = min(factor, index / fade_in)
    if tail > 0:
        factor = min(factor, (count - 1 - index) / tail)
    return max(0.0, factor)


def _scale_alpha(pixels: np.ndarray, factor: Any) -> np.ndarray:
    out = pixels.copy()
    out[..., 3] = np.floor(pixels[..., 3].astype(np.float64) * factor + 0.5).astype(np.uint8)
    out[out[..., 3] == 0] = 0
    return out


def _contact_sheet(samples: Sequence[np.ndarray], padding: Sequence[int], base_size: Sequence[int],
                   anchor: Sequence[float], tile: int = 160, columns: int = 6) -> Image.Image:
    """Review sheet: sampled frames on slate with the base canvas and the anchor drawn."""
    height, width = samples[0].shape[:2]
    factor = min(1.0, tile / max(width, height))
    tile_w, tile_h = max(1, _round_half_up(width * factor)), max(1, _round_half_up(height * factor))
    rows = math.ceil(len(samples) / columns)
    count = min(columns, len(samples))
    sheet = Image.new("RGB", (count * (tile_w + 8) + 8, rows * (tile_h + 8) + 8), (24, 30, 38))
    for index, frame in enumerate(samples):
        small = (forge_core.resample_rgba(frame, factor, "box", out_size=(tile_w, tile_h)) if factor < 1
                 else Image.fromarray(frame))
        cell = Image.new("RGB", (tile_w, tile_h), (58, 66, 76))
        cell.paste(small, (0, 0), small)
        draw = ImageDraw.Draw(cell)
        left, top = padding[0] * factor, padding[1] * factor
        draw.rectangle([left, top, left + base_size[0] * factor - 1, top + base_size[1] * factor - 1],
                       outline=(150, 150, 150))
        ax, ay = anchor[0] * factor, anchor[1] * factor
        draw.line([ax - 5, ay, ax + 5, ay], fill=(0, 230, 255))
        draw.line([ax, ay - 5, ax, ay + 5], fill=(0, 230, 255))
        row, column = divmod(index, columns)
        sheet.paste(cell, (8 + column * (tile_w + 8), 8 + row * (tile_h + 8)))
    return sheet


def _frame_list_text(indices: Sequence[int], limit: int = 12) -> str:
    shown = ", ".join(str(index) for index in indices[:limit])
    return shown + (f" and {len(indices) - limit} more" if len(indices) > limit else "")


@dataclass(frozen=True)
class ApplyPlan:
    """Everything apply needs before it touches a frame."""

    job: Job
    view: np.ndarray
    all_files: list[Path]
    files: list[Path]
    start: int
    padding: tuple[int, int, int, int]
    out_size: tuple[int, int]
    out_anchor: tuple[float, float]
    fit: VideoFit
    resampler: str
    min_run: int
    rest_frames: list[int]
    fx: bool


def _plan_apply(args: argparse.Namespace, character: dict[str, Any] | None) -> ApplyPlan:
    job = load_job(args.job)
    if character:
        problems = identity_mismatches(_identity_from_profile(character), _identity_from_job(job), args.scale_tolerance)
        if problems:
            raise ValueError(f"Profile mismatch with {Path(args.character_profile).name} "
                             f"(id {character.get('id')}): " + "; ".join(problems))
    inherited = (character or {}).get("registration", {})
    fit_name = args.fit or inherited.get("fit") or "auto"
    resampler = args.resampler or inherited.get("resampler") or "lanczos"
    min_run = args.ground_min_run or inherited.get("groundMinRun") or DEFAULT_GROUND_MIN_RUN
    if fit_name not in FITS or resampler not in forge_core.RESAMPLERS:
        raise ValueError(f"The profile's fit {fit_name!r} or resampler {resampler!r} is unknown")
    fx = args.profile == "fx"
    if fx and args.lock:
        raise ValueError("--lock pins a body to the ground; it does not apply to --profile fx")
    view = load_master_view(job)
    all_files = list_frames(args.frames, args.pattern)
    total = len(all_files)
    start, end = args.range if args.range else (0, None)
    end = total if end is None else min(end, total)
    if start >= end:
        raise ValueError(f"--range selects no frames out of {total}")
    files = all_files[start:end]
    rest_frames = ([] if fx else [0]) if args.rest_frames is None else args.rest_frames
    if rest_frames and rest_frames[-1] >= total:
        raise ValueError(f"--rest-frames {rest_frames} exceed the {total} frames in {Path(args.frames).name}")
    padding = tuple(args.action_padding) if args.action_padding else job.padding
    if fx and not args.action_padding and not any(padding):
        padding = prep.default_padding(prep.ACTIONS["fx"], job.source_size)
    width, height = job.source_size
    out_size = (width + padding[0] + padding[2], height + padding[1] + padding[3])
    out_anchor = (job.source_anchor[0] + padding[0], job.source_anchor[1] + padding[1])
    first, _ = _load_pixels(files[0])
    if not fx and int(first[..., 3].min()) == 255:
        raise ValueError(f"{files[0].name} is opaque: register keyed RGBA frames "
                         "(video2dsprite process frames-clean), not raw video frames")
    fit = video_fit(job.canvas, (first.shape[1], first.shape[0]), fit_name)
    return ApplyPlan(job, view, all_files, files, start, padding, out_size, out_anchor, fit, resampler, int(min_run),
                     rest_frames, fx)


def _load_frame(plan: ApplyPlan, path: Path) -> tuple[np.ndarray, str]:
    pixels, digest = _load_pixels(path)
    if (pixels.shape[1], pixels.shape[0]) != plan.fit.size:
        raise ValueError(f"{path.name} is {pixels.shape[1]}x{pixels.shape[0]}; the clip's frames are "
                         f"{plan.fit.size[0]}x{plan.fit.size[1]}")
    return pixels, digest


def _rest_pass(plan: ApplyPlan, mode: str, band: int) -> list[dict[str, Any]]:
    """Register the rest frames (source indices, inside or outside --range) without locks and
    measure them (anchor, foot line, x, hip)."""
    measures = []
    for index in plan.rest_frames:
        pixels, _ = _load_frame(plan, plan.all_files[index])
        registered = register_frame(pixels, plan.job, plan.fit, plan.padding, plan.out_size, plan.resampler)
        sil = silhouette(registered[..., 3], plan.min_run)
        if sil is None:
            continue
        measures.append({"frame": index, "anchor": forge_core.anchor_from_mask(sil.mask, mode, band_rows=band),
                         "ground": sil.ground, "height": sil.height, "area": sil.area, "x": sil.centroid_x,
                         "hip": hip_x(sil.mask, sil.ground, sil.height)})
    return measures


def _lock_targets(rest: list[dict[str, Any]], master: Silhouette, master_hip: float | None,
                  padding: Sequence[int]) -> dict[str, Any]:
    """Locks pin every frame to the rest pose as registered (the same measurement, so resampling
    blur adds no bias); without rest frames, to the master."""
    if rest:
        hips = [item["hip"] for item in rest if item["hip"] is not None]
        return {"source": "rest", "ground": _round_half_up(float(np.median([item["ground"] for item in rest]))),
                "x": float(np.median([item["x"] for item in rest])),
                "hip": float(np.median(hips)) if hips else None,
                "height": float(np.median([item["height"] for item in rest]))}
    return {"source": "master", "ground": master.ground + padding[1], "x": master.centroid_x + padding[0],
            "hip": None if master_hip is None else master_hip + padding[0], "height": float(master.height)}


def _lock_shift(sil: Silhouette, locks: Sequence[str], targets: dict[str, Any],
                lock_max: int | None) -> tuple[tuple[int, int], bool]:
    """Whole-output-pixel shift that pins the foot line (feet) and x (centroid or hip band);
    whole pixels keep the sampling phase identical in every frame (game-opus55)."""
    dx = dy = 0
    if "feet" in locks:
        dy = targets["ground"] - sil.ground
    if "x" in locks:
        dx = _round_half_up(targets["x"] - sil.centroid_x)
    elif "hip" in locks:
        hip = hip_x(sil.mask, targets["ground"], targets["height"])
        dx = 0 if hip is None else _round_half_up(targets["hip"] - hip)
    clipped = lock_max is not None and max(abs(dx), abs(dy)) > lock_max
    if clipped:
        dx, dy = int(np.clip(dx, -lock_max, lock_max)), int(np.clip(dy, -lock_max, lock_max))
    return (int(dx), int(dy)), clipped


def _register_locked(pixels: np.ndarray, plan: ApplyPlan, locks: Sequence[str], targets: dict[str, Any],
                     lock_max: int | None, margin: int) -> tuple[np.ndarray, int | None, tuple[int, int], bool]:
    """Register one frame and apply the locks.

    With locks the frame is registered once onto the canvas grown by ``margin`` along the
    locked axes, measured there, and the output canvas is cropped at the whole-pixel lock
    shift (the pixels a shifted registration gives); a shift beyond the margin is registered
    again. Returns the frame, its unshifted foot line in canvas coordinates (None when
    empty), the shift and whether --lock-max clipped it.
    """
    width, height = plan.out_size
    if not locks:
        registered = register_frame(pixels, plan.job, plan.fit, plan.padding, plan.out_size, plan.resampler)
        sil = silhouette(registered[..., 3], plan.min_run)
        return registered, None if sil is None else sil.ground, (0, 0), False
    mx = margin if ("x" in locks or "hip" in locks) else 0
    my = margin if "feet" in locks else 0
    grown = (plan.padding[0] + mx, plan.padding[1] + my, plan.padding[2] + mx, plan.padding[3] + my)
    wide = register_frame(pixels, plan.job, plan.fit, grown, (width + 2 * mx, height + 2 * my), plan.resampler)
    sil = silhouette(wide[..., 3], plan.min_run)
    if sil is None:
        return wide[my:my + height, mx:mx + width].copy(), None, (0, 0), False
    moved = {"ground": targets["ground"] + my, "x": targets["x"] + mx, "height": targets["height"],
             "hip": None if targets["hip"] is None else targets["hip"] + mx}
    (dx, dy), clipped = _lock_shift(sil, locks, moved, lock_max)
    if abs(dx) <= mx and abs(dy) <= my:
        registered = wide[my - dy:my - dy + height, mx - dx:mx - dx + width].copy()
    else:
        registered = register_frame(pixels, plan.job, plan.fit, plan.padding, plan.out_size, plan.resampler, (dx, dy))
    return registered, sil.ground - my, (dx, dy), clipped


def _overflow(plan: ApplyPlan, box: Sequence[int], shift: Sequence[int]) -> list[float]:
    """How far (output px) a source subject box reaches past each side of the output canvas."""
    x0, y0 = video_to_output(plan.job, plan.fit, plan.padding, box[:2], shift)
    x1, y1 = video_to_output(plan.job, plan.fit, plan.padding, box[2:], shift)
    return [-x0, -y0, x1 - plan.out_size[0], y1 - plan.out_size[1]]


def cmd_apply(args: argparse.Namespace) -> dict[str, Any]:
    character = load_profile(args.character_profile) if args.character_profile else None
    plan = _plan_apply(args, character)
    job, padding, out_size, count = plan.job, plan.padding, plan.out_size, len(plan.files)
    master_sil = silhouette(plan.view[..., 3], plan.min_run)
    if master_sil is None:
        raise ValueError("The master has no visible pixels (alpha > 16)")
    band = anchor_band(master_sil.height)
    mode = measure_mode(job)
    master_anchor = forge_core.anchor_from_mask(master_sil.mask, mode, band_rows=band)
    master_hip = hip_x(master_sil.mask, master_sil.ground, master_sil.height)
    rest_measures = _rest_pass(plan, mode, band)
    locks = args.lock or []
    targets = _lock_targets(rest_measures, master_sil, master_hip, padding)
    if "hip" in locks and targets["hip"] is None:
        raise ValueError("The rest pose has no hip band to lock to")
    fade = None
    fade_rect: list[float] = []
    fade_px, tail = 0.0, 0
    if plan.fx:
        fade_px = args.edge_fade if args.edge_fade is not None else max(1.0, FX_EDGE_FADE * min(out_size))
        fade, fade_rect = _fx_edge_fade(job, plan.fit, padding, out_size, fade_px)
        tail = max(FX_DISSOLVE_MIN_FRAMES, _round_half_up(args.dissolve_tail * count)) if args.dissolve_tail > 0 else 0

    lock_margin = args.lock_max if args.lock_max is not None else max(8, _round_half_up(LOCK_MARGIN * max(out_size)))
    output = Path(args.output_dir)
    final = output.parent.resolve() / output.name
    contact = sorted({_round_half_up(i * (count - 1) / max(1, min(12, count) - 1)) for i in range(min(12, count))})
    samples: list[np.ndarray] = []
    records: list[dict[str, Any]] = []
    video_edge, overflow_frames, empty_frames, clipped_locks = [], [], [], []
    needed = [0.0, 0.0, 0.0, 0.0]
    hygiene = {"floor_px": 0, "max_removed_alpha": 0, "frames": []}
    with forge_core.staged_output(output) as stage:
        (stage / "frames").mkdir()
        for index, path in enumerate(plan.files):
            pixels, digest = _load_frame(plan, path)
            source_box = forge_core.subject_bbox(pixels[..., 3])
            if source_box is not None and (min(source_box[:2]) == 0 or source_box[2] == plan.fit.size[0]
                                           or source_box[3] == plan.fit.size[1]):
                video_edge.append(index)
            registered, ground_line, shift, clipped = _register_locked(pixels, plan, locks, targets, args.lock_max,
                                                                        lock_margin)
            native = None if ground_line is None else ground_line - padding[1]
            if ground_line is None:
                empty_frames.append(index)
            if clipped:
                clipped_locks.append(index)
            if source_box is not None:
                over = _overflow(plan, source_box, shift)
                if max(over) > 0.5:
                    overflow_frames.append(index)
                    needed = [max(need, value) for need, value in zip(needed, over)]
            if plan.fx:
                registered = _scale_alpha(registered, fade * _fx_time_fade(index, count, args.fade_in_frames, tail))
            # D18: lanczos resampling (forge_core.resample_rgba) rings just outside edges and unpremultiplying
            # alpha 1-4 invents saturated, often key-leaning colours; they are invisible but read as key spill.
            cleaned, floor = forge_core.alpha_hygiene(registered, mode="floor", floor=HYGIENE_FLOOR)
            if floor["floor_px"]:
                registered = np.asarray(cleaned)
                hygiene["floor_px"] += floor["floor_px"]
                hygiene["max_removed_alpha"] = max(hygiene["max_removed_alpha"], floor["max_removed_alpha"])
                hygiene["frames"].append(index)
            name = f"frames/frame_{index:06d}.png"
            forge_core.save_png(registered, stage / name)
            records.append({"file": name, "sha256": forge_core.sha256_file(stage / name),
                            "sourceIndex": plan.start + index, "sourceFile": path.name, "sourceSha256": digest,
                            "shift": list(shift), "nativeFootBottom": native})
            if index in contact:
                samples.append(registered)
        checks, failures = _apply_checks(plan.fx, args, video_edge, overflow_frames, needed, padding, empty_frames,
                                         clipped_locks, plan.rest_frames, rest_measures, master_anchor, master_sil)
        if failures:
            raise RegistrationQAError("registration QA failed, nothing was written: " + "; ".join(failures))
        forge_core.save_png(np.asarray(_contact_sheet(samples, padding, job.source_size, plan.out_anchor)),
                            stage / "review-contact.png")
        document = _registration_document(args, plan, final, records, checks, master_sil, master_hip, master_anchor,
                                          band, rest_measures, targets, contact, character)
        document["hygiene"] = {"mode": "floor", "floor": HYGIENE_FLOOR, "floor_px": hygiene["floor_px"],
                               "max_removed_alpha": hygiene["max_removed_alpha"],
                               "frames_changed": len(hygiene["frames"]),
                               "method": "forge_core.alpha_hygiene(mode='floor', floor=4) after resampling, locks and "
                                         "fx fades: pixels with 0 < alpha <= 4 become (0, 0, 0, 0) (D18)"}
        if plan.fx:
            document["fx"] = {"edgeFadePx": fade_px, "fadeRect": fade_rect, "fadeInFrames": args.fade_in_frames,
                              "dissolveTailFrames": tail}
        forge_core.write_json(stage / REGISTRATION_FILE, document)
    warnings = [f"{check['id']}: {check['value']}" for check in checks if check["status"] == "warn"]
    for warning in warnings:
        print("warning: " + _ascii(warning), file=sys.stderr)
    return {"output": str(final), "metadata": str(final / REGISTRATION_FILE), "frames": count,
            "sourceSize": list(out_size), "sourceAnchor": list(plan.out_anchor), "fitApplied": plan.fit.applied,
            "qa": "needs-visual-review", "warnings": len(warnings)}


def _registration_document(args: argparse.Namespace, plan: ApplyPlan, final: Path, records: list[dict[str, Any]],
                           checks: list[dict[str, Any]], master_sil: Silhouette, master_hip: float | None,
                           master_anchor: Sequence[float], band: int, rest_measures: list[dict[str, Any]],
                           targets: dict[str, Any], contact: list[int],
                           character: dict[str, Any] | None) -> dict[str, Any]:
    """registration.json (video2dsprite.registration.v1; schema requested in the B06 handoff)."""
    job, padding, fit = plan.job, plan.padding, plan.fit
    rest = None
    if rest_measures:
        anchor = [float(np.median([item["anchor"][axis] for item in rest_measures])) for axis in (0, 1)]
        expected = [master_anchor[0] + padding[0], master_anchor[1] + padding[1]]
        rest = {"frames": [item["frame"] for item in rest_measures],
                "nativeFootBottom": float(np.median([item["ground"] for item in rest_measures])) - padding[1],
                "anchor": anchor, "anchorExpected": expected,
                "anchorError": [round(anchor[axis] - expected[axis], 4) for axis in (0, 1)],
                "heightRatio": float(np.median([item["height"] for item in rest_measures])) / master_sil.height,
                "areaRatio": float(np.median([item["area"] for item in rest_measures])) / master_sil.area}
    feet = [record["nativeFootBottom"] for record in records if record["nativeFootBottom"] is not None]
    scale_x, scale_y = output_scale(job, fit)
    anchor_video = anchor_in_video(job, fit)
    display_scale = args.display_scale
    if display_scale is None and plan.fx:
        display_scale = max(plan.out_size) / max(job.source_size)
    master_ref = {"path": ref_path(job.master_path, final), "sha256": job.master_sha256}
    document: dict[str, Any] = {
        "schema": REGISTRATION_SCHEMA,
        "mode": "construction",
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "clip": args.clip or job.action,
        "action": job.action,
        "profile": args.profile,
        "jobSha256": job.sha256,
        "job": {"path": ref_path(job.path, final), "sha256": job.sha256},
        "master": {**master_ref, "size": list(job.master_size), "anchor": job.data["master"]["anchor"],
                   **({"viewBox": list(job.view_box)} if job.view_box else {})},
        "anchorMode": job.anchor_mode,
        "keyColor": job.key,
        "referenceCanvas": list(job.canvas),
        "referenceScale": job.scale,
        "referenceOffset": list(job.offset),
        "baseSize": list(job.source_size),
        "baseAnchor": list(job.source_anchor),
        "padding": list(padding),
        "sourceSize": list(plan.out_size),
        "sourceAnchor": list(plan.out_anchor),
        "masterGeometry": {"groundLine": master_sil.ground, "top": master_sil.box[1], "height": master_sil.height,
                           "centroidX": master_sil.centroid_x, "hipX": master_hip, "anchor": list(master_anchor),
                           "anchorBandRows": band},
        "video": {"size": list(fit.size), "frameCount": len(plan.all_files),
                  "range": [plan.start, plan.start + len(records)],
                  "frameDirectory": ref_path(Path(args.frames).resolve(), final)},
        "transform": {
            "fit": fit.requested, "fitApplied": fit.applied, "videoScale": list(fit.k), "videoOffset": list(fit.c),
            "anchorVideo": list(anchor_video), "anchorOutput": list(plan.out_anchor),
            "inverse": {"scale": [scale_x, scale_y], "offset": [plan.out_anchor[0] - anchor_video[0] * scale_x,
                                                                 plan.out_anchor[1] - anchor_video[1] * scale_y]},
            "resampler": plan.resampler,
            "note": "output = video * inverse.scale + inverse.offset + shift; one transform for every frame",
        },
        "lock": {"modes": args.lock or [], "maxShift": args.lock_max, "groundMinRun": plan.min_run,
                 "targets": targets},
        "rest": rest,
        "nativeFootBottomRange": [min(feet), max(feet)] if feet else None,
        "frames": records,
        "review": {"path": "review-contact.png", "frames": contact},
        "qa": {
            "status": "needs-visual-review",
            "method": "register_clip apply v1: one inverse transform from registration_job.v1 (registration by "
                      "construction), forge_core.resample_rgba anchor-pinned premultiplied resampling, then the "
                      "alpha hygiene floor (alpha <= 4 cleared, D18); subject = alpha > 16; edge, overflow and "
                      "rest-pose checks on the source and registered alpha",
            "notProven": ["identity, contact and pose quality (look at review-contact.png and the frames)",
                          "matte quality of the input frames", "loop seams and timing (gait_loop, retime)"],
            "checks": checks,
            "inputs": [_file_ref(job.path, final, job.sha256), master_ref,
                       *({"path": ref_path(path, final), "sha256": record["sourceSha256"]}
                         for path, record in zip(plan.files, records))],
            "outputs": [{"path": record["file"], "sha256": record["sha256"]} for record in records],
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        },
    }
    if display_scale is not None:
        document["displayScale"] = display_scale
    if character:
        document["characterProfile"] = {"path": ref_path(Path(args.character_profile).resolve(), final),
                                        "sha256": forge_core.sha256_file(args.character_profile),
                                        "id": character.get("id")}
    return document


def _apply_checks(fx: bool, args: argparse.Namespace, video_edge: list[int], overflow_frames: list[int],
                  needed: list[float], padding: Sequence[int], empty_frames: list[int], clipped_locks: list[int],
                  rest_frames: list[int], rest_measures: list[dict[str, Any]], master_anchor: Sequence[float],
                  master_sil: Silhouette) -> tuple[list[dict], list[str]]:
    """Registration QA checks and the failure messages (empty when everything passes)."""
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    soft = fx or args.allow_edge_touch
    status = "warn" if soft else "fail"
    checks.append({"id": "video-edge-touch", "status": status if video_edge else "pass", "value": video_edge,
                   "threshold": 0})
    if video_edge and not soft:
        failures.append(f"video-edge-touch: the subject touches the video frame border in frames "
                        f"{_frame_list_text(video_edge)}; the generator cut it, so regenerate the take (padding cannot "
                        "restore it)")
    extra = [int(math.ceil(value)) if value > 0.5 else 0 for value in needed]
    grown = [base + more for base, more in zip(padding, extra)]
    checks.append({"id": "canvas-overflow", "status": status if overflow_frames else "pass",
                   "value": {"frames": overflow_frames, "neededPadding": extra}, "threshold": 0.5})
    if overflow_frames and not soft:
        failures.append(f"canvas-overflow: the subject leaves the {padding} padded canvas in frames "
                        f"{_frame_list_text(overflow_frames)}; grow the canvas with --action-padding "
                        f"{','.join(str(v) for v in grown)} (never shrink the body)")
    checks.append({"id": "empty-frames", "status": "warn" if empty_frames and not fx else "pass",
                   "value": empty_frames, "threshold": 0})
    if clipped_locks:
        checks.append({"id": "lock-max", "status": "warn", "value": clipped_locks, "threshold": args.lock_max})
    if not rest_frames:
        checks.append({"id": "rest-anchor-error", "status": "skipped", "value": None, "threshold": None})
        checks.append({"id": "rest-scale-error", "status": "skipped", "value": None, "threshold": None})
        return checks, failures
    if not rest_measures:
        checks.append({"id": "rest-anchor-error", "status": "fail", "value": "rest frames are empty",
                       "threshold": args.max_anchor_error})
        failures.append(f"rest-anchor-error: rest frames {rest_frames} have no subject")
        return checks, failures
    expected = (master_anchor[0] + padding[0], master_anchor[1] + padding[1])
    error = max(max(abs(item["anchor"][axis] - expected[axis]) for axis in (0, 1)) for item in rest_measures)
    ok = error <= args.max_anchor_error
    checks.append({"id": "rest-anchor-error", "status": "pass" if ok else "fail", "value": round(error, 4),
                   "threshold": args.max_anchor_error})
    if not ok:
        failures.append(f"rest-anchor-error: the rest pose sits {error:.2f} px from the master's anchor (limit "
                        f"{args.max_anchor_error:g}); the provider changed the framing: check --fit, run qc, or "
                        "regenerate")
    ratio = max(abs(item["height"] / master_sil.height - 1.0) for item in rest_measures)
    ok = ratio <= args.max_scale_error
    checks.append({"id": "rest-scale-error", "status": "pass" if ok else "fail", "value": round(ratio, 5),
                   "threshold": args.max_scale_error})
    if not ok:
        failures.append(f"rest-scale-error: the rest pose height differs from the master by {ratio:.1%} (limit "
                        f"{args.max_scale_error:.1%}); the provider rescaled the take: check --fit or regenerate")
    return checks, failures


# --------------------------------------------------------------------------- profile and validate-profile

def profile_matte(mode: str, key: str, erode: int, unmix: bool | None, despill: str) -> dict[str, Any]:
    """The ``matte`` block video2dsprite --matte-profile reads (D21): ``unmix`` defaults to the soft matte
    (the dominance alpha is a ramp and binary alpha has nothing to un-mix, so only soft may un-mix) and
    ``erode`` is a whole number of px."""
    if mode not in MATTE_MODES:
        raise ValueError(f"--matte-mode must be one of {', '.join(MATTE_MODES)}")
    if unmix and mode != "soft":
        raise ValueError(f"--unmix belongs to the soft matte; a {mode} profile pins unmix false")
    if isinstance(erode, bool) or not isinstance(erode, int) or erode < 0:
        raise ValueError(f"--erode must be a whole number of px >= 0, got {erode!r}")
    return {"mode": mode, "key": key, "erode": erode, "unmix": (mode == "soft") if unmix is None else bool(unmix),
            "despill": despill}


def cmd_profile(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"Refusing to replace existing output: {output}")
    matte = profile_matte(args.matte_mode, args.key or None, args.erode, args.unmix, args.despill_mode)
    registrations = [(Path(path), load_registration(path)) for path in args.registration]
    reference = registrations[0][1]
    expected = _identity_from_registration(reference)
    clips: dict[str, Any] = {}
    for path, doc in registrations:
        problems = identity_mismatches(expected, _identity_from_registration(doc), args.scale_tolerance)
        if problems:
            raise ValueError(f"{path.name} does not match {registrations[0][0].name}: " + "; ".join(problems))
        if doc["clip"] in clips:
            raise ValueError(f"Two registrations name clip {doc['clip']!r}; pass apply --clip to tell them apart")
        clips[doc["clip"]] = {
            "padding": doc["padding"], "sourceSize": doc["sourceSize"], "sourceAnchor": doc["sourceAnchor"],
            "registration": {"path": ref_path(path.resolve(), output.parent.resolve()),
                             "sha256": forge_core.sha256_file(path)},
            "jobSha256": doc.get("jobSha256"),
            "nativeFootBottom": (doc.get("rest") or {}).get("nativeFootBottom"),
        }
    feet = [clip["nativeFootBottom"] for clip in clips.values() if clip["nativeFootBottom"] is not None]
    geometry = reference.get("masterGeometry", {})
    profile: dict[str, Any] = {
        "schema": PROFILE_SCHEMA,
        "id": args.id,
        "sourceSize": reference["baseSize"],
        "sourceAnchor": reference["baseAnchor"],
        "registration": {
            "scale": reference["referenceScale"], "anchorMode": reference["anchorMode"],
            "masterSha256": reference["master"]["sha256"], "referenceCanvas": reference.get("referenceCanvas"),
            "fit": reference.get("transform", {}).get("fit"),
            "resampler": reference.get("transform", {}).get("resampler"),
            "groundMinRun": reference.get("lock", {}).get("groundMinRun"),
            "restFootBottom": geometry.get("groundLine"),
        },
        "matte": {**matte, "key": matte["key"] or reference.get("keyColor", "magenta")},
        "clips": clips,
        "nativeFootBottomRange": [min(feet), max(feet)] if feet else None,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
    }
    if args.world_scale is not None:
        profile["worldScale"] = args.world_scale
    output.parent.mkdir(parents=True, exist_ok=True)
    forge_core.write_json(output, profile, no_clobber=True)
    final = output.resolve()
    return {"output": str(final), "metadata": str(final), "id": args.id, "clips": sorted(clips),
            "nativeFootBottomRange": profile["nativeFootBottomRange"]}


def cmd_validate_profile(args: argparse.Namespace) -> dict[str, Any]:
    profile = load_profile(args.profile)
    expected = _identity_from_profile(profile)
    rest_line = profile["registration"].get("restFootBottom")
    results = []
    failed = False
    for path in args.registration:
        doc = load_registration(path)
        issues = identity_mismatches(expected, _identity_from_registration(doc), args.scale_tolerance)
        if doc.get("mode") != "construction":
            issues.append(f"mode {doc.get('mode')} is not construction")
        known = profile["clips"].get(doc["clip"])
        if known:
            for field in ("padding", "sourceSize"):
                if list(known[field]) != list(doc[field]):
                    issues.append(f"{field} {doc[field]} != profile clip {known[field]}")
            if max(abs(float(a) - float(b)) for a, b in zip(known["sourceAnchor"], doc["sourceAnchor"])) > 0.01:
                issues.append(f"sourceAnchor {doc['sourceAnchor']} != profile clip {known['sourceAnchor']}")
        foot = (doc.get("rest") or {}).get("nativeFootBottom")
        if foot is not None and rest_line is not None and abs(foot - rest_line) > args.foot_tolerance:
            issues.append(f"rest foot line {foot:g} is {foot - rest_line:+g} px from the profile's {rest_line:g} "
                          f"(tolerance {args.foot_tolerance:g})")
        failed |= bool(issues)
        results.append({"clip": doc["clip"], "registration": Path(path).name, "nativeFootBottom": foot,
                        "clipFootBottomRange": doc.get("nativeFootBottomRange"), "issues": issues})
    feet = [item["nativeFootBottom"] for item in results if item["nativeFootBottom"] is not None]
    foot_range = [min(feet), max(feet)] if feet else None
    spread_ok = foot_range is None or foot_range[1] - foot_range[0] <= args.foot_tolerance
    summary = {"status": "pass" if not failed and spread_ok else "fail", "profile": profile.get("id"),
               "clips": len(results), "nativeFootBottomRange": foot_range,
               "restFootBottom": rest_line, "results": results}
    if summary["status"] == "fail":
        print(json.dumps(summary))
        reasons = [f"{item['clip']}: " + "; ".join(item["issues"]) for item in results if item["issues"]]
        if not spread_ok:
            reasons.append(f"nativeFootBottomRange {foot_range} spreads more than {args.foot_tolerance:g} px")
        raise ValueError("profile validation failed: " + " | ".join(reasons))
    return summary


# --------------------------------------------------------------------------- qc

def _luma(pixels: np.ndarray) -> np.ndarray:
    return pixels[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)


def ncc_search(template: np.ndarray, image: np.ndarray, x0: int, y0: int, search: int) -> dict[str, Any] | None:
    """Best zero-mean NCC of ``template`` (top-left at x0, y0) within +-search px in ``image``."""
    height, width = template.shape
    top, left = y0 - search, x0 - search
    if top < 0 or left < 0 or top + height + 2 * search > image.shape[0] or left + width + 2 * search > image.shape[1]:
        return None
    region = image[top:top + height + 2 * search, left:left + width + 2 * search].astype(np.float64)
    centred = template.astype(np.float64) - template.mean()
    norm = math.sqrt(float((centred * centred).sum()))
    if norm < 1e-6:
        return None
    windows = np.lib.stride_tricks.sliding_window_view(region, (height, width))
    numerator = np.einsum("ijkl,kl->ij", windows, centred)
    count = height * width
    sums = _box_sum(region, height, width)
    squares = _box_sum(region * region, height, width)
    spread = np.sqrt(np.maximum(squares - sums * sums / count, 0.0))
    scores = numerator / np.maximum(spread * norm, 1e-9)
    row, column = np.unravel_index(int(np.argmax(scores)), scores.shape)
    return {"ncc": float(scores[row, column]), "dx": int(column) - search, "dy": int(row) - search}


def pick_landmarks(luma: np.ndarray, subject: np.ndarray, count: int, patch: int, search: int) -> list[tuple[int, int]]:
    """Up to ``count`` well-textured patch corners (top-left) on the subject: Shi-Tomasi
    response over the patch, at least a quarter subject coverage, greedy spacing of one patch."""
    gradient_y, gradient_x = np.gradient(luma.astype(np.float64))
    sxx = _box_sum(gradient_x * gradient_x, patch, patch)
    syy = _box_sum(gradient_y * gradient_y, patch, patch)
    sxy = _box_sum(gradient_x * gradient_y, patch, patch)
    response = (sxx + syy) / 2 - np.sqrt(((sxx - syy) / 2) ** 2 + sxy ** 2)
    coverage = _box_sum(subject.astype(np.float64), patch, patch) / (patch * patch)
    score = np.where(coverage >= 0.25, response, -np.inf)
    score[:search, :] = -np.inf
    score[:, :search] = -np.inf
    score[score.shape[0] - search:, :] = -np.inf
    score[:, score.shape[1] - search:] = -np.inf
    picks: list[tuple[int, int]] = []
    for _ in range(count):
        flat = int(np.argmax(score))
        row, column = divmod(flat, score.shape[1])
        if not np.isfinite(score[row, column]) or score[row, column] <= 1e-6:
            break
        picks.append((column, row))
        score[max(0, row - patch + 1):row + patch, max(0, column - patch + 1):column + patch] = -np.inf
    return picks


def _similarity_scale(points: Sequence[tuple[float, float]], moved: Sequence[tuple[float, float]]) -> float | None:
    if len(points) < 3:
        return None
    source = np.asarray(points, np.float64)
    target = np.asarray(moved, np.float64)
    source_c, target_c = source - source.mean(0), target - target.mean(0)
    denominator = float((source_c * source_c).sum())
    return float((source_c * target_c).sum() / denominator) if denominator > 1e-9 else None


def _band_mask(height: int, width: int, band: int) -> np.ndarray:
    mask = np.zeros((height, width), bool)
    mask[:band] = mask[-band:] = True
    mask[:, :band] = mask[:, -band:] = True
    return mask


def _decimate(image: np.ndarray, target: int = 240) -> tuple[np.ndarray, int]:
    """Every n-th pixel so the short side is about ``target`` px (key estimates, motion measures)."""
    step = max(1, min(image.shape[:2]) // target)
    return image[::step, ::step], step


def _frame_key(pixels: np.ndarray, job: Job, band: int) -> np.ndarray:
    """The take's backdrop key from the border ring of a decimated frame (I2V keys drift)."""
    small, step = _decimate(pixels[..., :3])
    key, _info = forge_matte.estimate_key(small, job.key, ring=max(1, band // step))
    return key


def _subject_mask(pixels: np.ndarray, key: np.ndarray, keyed: bool) -> np.ndarray:
    """Keyed frames: alpha > 16. Raw frames: RGB distance from the key above 64."""
    if keyed:
        return pixels[..., 3] > THRESHOLD
    offset = pixels[..., :3].astype(np.float32) - key
    return np.sqrt((offset * offset).sum(-1)) > QC_SUBJECT_DISTANCE


def _subject_stats(mask: np.ndarray, band: int) -> dict[str, Any] | None:
    box = _bbox(mask)
    if box is None:
        return None
    return {"area": int(mask.sum()), "box": box,
            "anchor": forge_core.anchor_from_mask(mask, "stance", band_rows=band)}


def _take_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    ids = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                ids.add(str(json.loads(line).get("take")))
            except (ValueError, AttributeError):
                raise ValueError(f"{path.name} line {number} is not a JSON object") from None
    return ids


def cmd_qc(args: argparse.Namespace) -> dict[str, Any]:
    job = load_job(args.job)
    view = load_master_view(job)
    takes = Path(args.takes) if args.takes else job.path.parent / "takes.jsonl"
    take_id = args.take or (Path(args.video).stem if args.video else Path(args.frames).resolve().name)
    if take_id in _take_ids(takes):
        raise ValueError(f"Take {take_id!r} is already recorded in {takes.name}; pass a new --take id")
    with tempfile.TemporaryDirectory(prefix="register-clip-qc-") as scratch:
        if args.video:
            import forge_av  # optional: needs ffmpeg
            fps = _fps_value(forge_av.probe(args.video)["fps_rational"])
            files = forge_av.extract_frames(args.video, Path(scratch) / "frames", alpha="off")
            source = {"kind": "video", "name": Path(args.video).name, "sha256": forge_core.sha256_file(args.video)}
        else:
            files = list_frames(args.frames, args.pattern)
            fps = args.fps
            source = {"kind": "frames", "name": Path(args.frames).resolve().name}
        line = _judge_take(job, view, files, fps, take_id, source, takes, args)
    takes.parent.mkdir(parents=True, exist_ok=True)
    with open(takes, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n")
    summary = {"take": take_id, "status": line["status"], "reasons": line["reasons"],
               "warnings": line["warnings"], "takes": str(takes.resolve()), "metadata": str(takes.resolve())}
    if line["status"] == "rejected" and args.strict:
        print(json.dumps(summary))
        raise ValueError(f"take {take_id} rejected: " + " | ".join(line["reasons"]))
    return summary


def _plain(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _fps_value(text: str) -> float:
    numerator, _, denominator = str(text).partition("/")
    return float(numerator) / float(denominator or 1)


@dataclass(frozen=True)
class TakeReference:
    """The master as the job placed it, seen in the take's video px."""

    fit: VideoFit
    luma: np.ndarray
    stats: dict[str, Any]
    stance_band: int
    landmarks: list[tuple[int, int]]
    identity_box: list[int] | None
    identity_step: int
    identity_template: np.ndarray | None


def _identity_box(job: Job, fit: VideoFit, mask: np.ndarray, box: Sequence[int], size: Sequence[int],
                  requested: Sequence[int] | None) -> list[int] | None:
    """--identity-box (master px) in video px, or the head: the top 35% of the subject, as wide as its
    top 20% plus a margin."""
    if requested:
        x0, y0 = (int(math.floor(v)) for v in master_to_video(job, fit, requested[:2]))
        x1, y1 = (int(math.ceil(v)) for v in master_to_video(job, fit, requested[2:]))
    else:
        bx0, by0, bx1, by1 = box
        height = by1 - by0
        crown = np.flatnonzero(mask[by0:by0 + max(1, _round_half_up(0.2 * height))].any(axis=0))
        margin = _round_half_up(0.1 * (crown[-1] + 1 - crown[0]))
        x0, x1 = max(bx0, crown[0] - margin), min(bx1, crown[-1] + 1 + margin)
        y0, y1 = by0, by0 + max(QC_PATCH // 2, _round_half_up(0.35 * height))
    x0, y0 = max(QC_SEARCH, x0), max(QC_SEARCH, y0)
    x1, y1 = min(size[0] - QC_SEARCH, x1), min(size[1] - QC_SEARCH, y1)
    return [x0, y0, x1, y1] if x1 - x0 >= 8 and y1 - y0 >= 8 else None


def _take_reference(job: Job, view: np.ndarray, first: np.ndarray, args: argparse.Namespace) -> TakeReference:
    size = (first.shape[1], first.shape[0])
    fit = video_fit(job.canvas, size, args.fit or "auto")
    placed = place_in_video(view, job, fit)
    rgb = prep.composite_on_key(placed, job.key_rgb)
    luma = _luma(rgb)
    keyed = int(first[..., 3].min()) < 255
    # the reference subject follows the same rule as the frames, so their areas compare without bias
    mask = placed[..., 3] > THRESHOLD if keyed else _subject_mask(rgb, np.asarray(job.key_rgb, np.float32), False)
    box = _bbox(mask)
    if box is None:
        raise ValueError("The master does not land inside the video frame; check the job and --fit")
    stance_band = anchor_band(box[3] - box[1])
    identity = _identity_box(job, fit, mask, box, size, args.identity_box)
    step, template = 1, None
    if identity:
        step = max(1, math.ceil(max(identity[2] - identity[0], identity[3] - identity[1]) / 96))
        template = _block_mean(luma[identity[1]:identity[3], identity[0]:identity[2]], step)
    return TakeReference(fit, luma, _subject_stats(mask, stance_band), stance_band,
                         pick_landmarks(luma, mask, args.landmarks, QC_PATCH, QC_SEARCH), identity, step, template)


def _block_mean(image: np.ndarray, step: int) -> np.ndarray:
    """Mean of step x step blocks (the trailing partial block is dropped)."""
    if step == 1:
        return image
    height, width = (image.shape[0] // step) * step, (image.shape[1] // step) * step
    return image[:height, :width].reshape(height // step, step, width // step, step).mean(axis=(1, 3))


def _sample_scores(reference: TakeReference, luma: np.ndarray, min_ncc: float) -> tuple[dict, dict | None]:
    """Landmark matches (with a similarity-fit scale) and the identity NCC of one sampled frame."""
    matches = [ncc_search(reference.luma[y:y + QC_PATCH, x:x + QC_PATCH], luma, x, y, QC_SEARCH)
               for x, y in reference.landmarks]
    good = [(point, match) for point, match in zip(reference.landmarks, matches)
            if match and match["ncc"] >= min_ncc and abs(match["dx"]) < QC_SEARCH and abs(match["dy"]) < QC_SEARCH]
    half = QC_PATCH / 2
    scale = _similarity_scale([(x + half, y + half) for (x, y), _ in good],
                              [(x + half + m["dx"], y + half + m["dy"]) for (x, y), m in good])
    values = [match["ncc"] for match in matches if match]
    landmarks = {"matched": len(good), "medianNcc": round(float(np.median(values)), 4) if values else None,
                 "scale": None if scale is None else round(scale, 5)}
    identity = None
    if reference.identity_template is not None:
        step = reference.identity_step
        x0, y0 = reference.identity_box[:2]
        search = max(1, math.ceil(QC_SEARCH / step))
        image = _block_mean(luma[y0 % step:, x0 % step:], step)
        match = ncc_search(reference.identity_template, image, x0 // step, y0 // step, search)
        identity = {"ncc": None if match is None else round(match["ncc"], 4),
                    "dx": None if match is None else match["dx"] * step,
                    "dy": None if match is None else match["dy"] * step}
    return landmarks, identity


def _judge_take(job: Job, view: np.ndarray, files: Sequence[Path], fps: float, take_id: str,
                source: dict[str, Any], takes: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Score one raw take; the take.v1 line (with a QA envelope) is returned, not written."""
    first, first_digest = _load_pixels(files[0])
    size = (first.shape[1], first.shape[0])
    keyed = int(first[..., 3].min()) < 255
    reference = _take_reference(job, view, first, args)
    fit = reference.fit
    count = len(files)
    to_source = (1.0 / (fit.k[0] * job.scale), 1.0 / (fit.k[1] * job.scale))
    band = args.edge_band or max(4, int(math.floor(job.margin * min(fit.k) * 0.6)))
    band = min(band, max(1, min(size) // 4))
    band_mask = _band_mask(size[1], size[0], band)
    band_px = int(band_mask.sum())
    calm = max(1, _round_half_up(prep.CALM_SPAN_S * fps))
    start_span = list(range(min(count, calm)))
    end_span = list(range(max(0, count - calm), count)) if job.returns_to_rest and count > calm else []
    samples = sorted({_round_half_up(i * (count - 1) / max(1, min(args.samples, count) - 1))
                      for i in range(min(args.samples, count))})

    edge_frames: list[dict[str, Any]] = []
    key_drift: list[float] = []
    changes: list[float] = []
    poses: dict[int, dict[str, float]] = {}
    landmark_rows: list[dict[str, Any]] = []
    identity_rows: list[dict[str, Any]] = []
    digests: list[str] = []
    base = None
    for index, path in enumerate(files):
        pixels, digest = (first, first_digest) if index == 0 else _load_pixels(path)
        digests.append(digest)
        if (pixels.shape[1], pixels.shape[0]) != size:
            raise ValueError(f"{path.name} is {pixels.shape[1]}x{pixels.shape[0]}; the take is {size[0]}x{size[1]}")
        key = np.asarray(job.key_rgb, np.float32) if keyed else _frame_key(pixels, job, band)
        key_drift.append(float(np.sqrt(((key - np.asarray(job.key_rgb, np.float32)) ** 2).sum())))
        if keyed:
            impure = int((pixels[..., 3][band_mask] > THRESHOLD).sum())
        else:
            ring = pixels[..., :3][band_mask].astype(np.float32) - key
            impure = int(((ring * ring).sum(-1) > forge_matte.KEY_TOLERANCE ** 2).sum())
        if impure > args.max_edge_impure * band_px:
            edge_frames.append({"frame": index, "impurePx": impure, "fraction": round(impure / band_px, 6)})
        small = _luma(_decimate(pixels)[0])
        base = small if base is None else base
        changes.append(float((np.abs(small - base) > QC_CHANGE_LEVELS).mean()))
        if index == 0 or index in end_span:
            stats = _subject_stats(_subject_mask(pixels, key, keyed), reference.stance_band)
            if stats:
                poses[index] = {"scale": math.sqrt(stats["area"] / reference.stats["area"]),
                                "dx": (stats["anchor"][0] - reference.stats["anchor"][0]) * to_source[0],
                                "dy": (stats["anchor"][1] - reference.stats["anchor"][1]) * to_source[1]}
        if index in samples:
            marks, identity = _sample_scores(reference, _luma(pixels), args.min_landmark_ncc)
            landmark_rows.append({"frame": index, **marks})
            if identity:
                identity_rows.append({"frame": index, **identity})
    if source["kind"] == "video":
        inputs = [{"path": source["name"], "sha256": source["sha256"]}]
    else:
        inputs = [{"path": ref_path(path, takes.resolve().parent), "sha256": digest}
                  for path, digest in zip(files, digests)]
    return _take_line(job, reference, take_id, source, takes, args, size, fps, count, band, start_span, end_span,
                      edge_frames, key_drift, changes, poses, landmark_rows, identity_rows, inputs)


def _take_line(job: Job, reference: TakeReference, take_id: str, source: dict[str, Any], takes: Path,
               args: argparse.Namespace, size: Sequence[int], fps: float, count: int, band: int,
               start_span: list[int], end_span: list[int], edge_frames: list[dict[str, Any]], key_drift: list[float],
               changes: list[float], poses: dict[int, dict[str, float]], landmark_rows: list[dict[str, Any]],
               identity_rows: list[dict[str, Any]], inputs: list[dict[str, str]]) -> dict[str, Any]:
    """Gates, warnings and the take.v1 line; ``inputs`` are the take's video or frame file refs."""
    start = poses.get(0)
    end = [poses[index] for index in end_span if index in poses]
    end_scale = float(np.median([pose["scale"] for pose in end])) if end else None
    end_drift = None
    if end:
        end_drift = math.hypot(float(np.median([pose["dx"] for pose in end])),
                               float(np.median([pose["dy"] for pose in end])))
    start_drift = math.hypot(start["dx"], start["dy"]) if start else None
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    warnings: list[str] = []

    def gate(check_id: str, ok: bool | None, value: Any, threshold: Any, reason: str) -> None:
        checks.append({"id": check_id, "status": "skipped" if ok is None else ("pass" if ok else "fail"),
                       "value": value, "threshold": threshold})
        if ok is False:
            reasons.append(reason)

    worst = max(edge_frames, key=lambda item: item["fraction"]) if edge_frames else {"fraction": 0.0, "frame": None}
    gate("edge-key-purity", not edge_frames, [item["frame"] for item in edge_frames], args.max_edge_impure,
         f"edge key purity: {len(edge_frames)} frame(s) have non-key pixels in the {band} px border band (worst "
         f"{worst['fraction']:.2%} at frame {worst['frame']}); the subject or an effect leaves the work region or the "
         "background is not flat: regenerate, don't pad")
    first_row = landmark_rows[0] if landmark_rows and landmark_rows[0]["frame"] == 0 else None
    landmarks_ok = None if not reference.landmarks or first_row is None else (
        first_row["matched"] >= 0.5 * len(reference.landmarks))
    gate("landmarks-first-frame", landmarks_ok,
         None if first_row is None else f"{first_row['matched']}/{len(reference.landmarks)}", 0.5,
         f"only {first_row['matched'] if first_row else 0}/{len(reference.landmarks)} landmarks match the reference in "
         f"frame 0 (NCC >= {args.min_landmark_ncc:g}): the take does not start from the supplied image")
    gate("camera-scale-start", None if start is None else abs(start["scale"] - 1) <= args.max_scale_change,
         None if start is None else round(start["scale"], 5), args.max_scale_change,
         f"camera scale {start['scale'] if start else 0:.3f} in frame 0: the take does not start at the reference "
         "scale; check --fit (provider crop or stretch) or regenerate")
    gate("camera-drift-start", None if start_drift is None else start_drift <= args.max_drift,
         None if start_drift is None else round(start_drift, 4), args.max_drift,
         f"frame 0 sits {start_drift or 0:.2f} source px from the reference position: check --fit or regenerate")
    gate("camera-scale-end", None if end_scale is None else abs(end_scale - 1) <= args.max_scale_change,
         None if end_scale is None else round(end_scale, 5), args.max_scale_change,
         f"camera scale {end_scale or 0:.3f} in the end calm span (frames {end_span[0] if end_span else 0}-"
         f"{end_span[-1] if end_span else 0}): push-in or pull-out; regenerate with 'locked camera, fixed scale'")
    calm_identity = [row["ncc"] for row in identity_rows
                     if (row["frame"] == 0 or row["frame"] in end_span) and row["ncc"] is not None]
    identity_min = min(calm_identity) if calm_identity else None
    gate("identity-calm", None if identity_min is None else identity_min >= args.min_identity_ncc,
         identity_min, args.min_identity_ncc,
         f"identity NCC {identity_min or 0:.2f} below {args.min_identity_ncc:g} in a calm frame: the head region "
         "changed or is covered")

    # calm spans: the share of pixels changed from frame 0, relative to the clip's busiest 10% of frames
    busiest = float(np.percentile(changes, 90))
    calm_motion = {}
    for name, span in (("start", start_span), ("end", end_span)):
        if span and busiest > 1e-3:
            calm_motion[name] = round(float(np.median([changes[index] for index in span])) / busiest, 4)
            moving = calm_motion[name] > 0.5
            checks.append({"id": f"calm-{name}", "status": "warn" if moving else "pass",
                           "value": calm_motion[name], "threshold": 0.5})
            if moving:
                warnings.append(f"the {name} calm span is not at the reference pose (it changes "
                                f"{calm_motion[name]:.2f}x as much as the busiest frames): "
                                + ("the take starts moving at once; frame 0 is the only rest frame" if name == "start"
                                   else "the take does not return to the reference pose"))
    if end_drift is not None and end_drift > args.max_drift:
        warnings.append(f"the end calm span sits {end_drift:.2f} source px from the reference: "
                        "consider apply --lock feet")
    all_identity = [row["ncc"] for row in identity_rows if row["ncc"] is not None]
    metrics = {
        "frames": count, "fps": fps, "size": list(size), "fitApplied": reference.fit.applied,
        "calm": {"start": [start_span[0], start_span[-1]] if start_span else None,
                 "end": [end_span[0], end_span[-1]] if end_span else None, "motionRatio": calm_motion},
        "cameraScale": {"start": None if start is None else start["scale"], "end": end_scale,
                        "landmarkFirst": first_row["scale"] if first_row else None,
                        "landmarkLast": landmark_rows[-1]["scale"] if landmark_rows else None},
        "cameraDriftSourcePx": {"start": start_drift, "end": end_drift},
        "landmarks": {"count": len(reference.landmarks), "patch": QC_PATCH, "search": QC_SEARCH,
                      "minNcc": args.min_landmark_ncc, "perFrame": landmark_rows},
        "identity": {"box": reference.identity_box, "minCalm": identity_min,
                     "minAll": min(all_identity) if all_identity else None, "perFrame": identity_rows},
        "edge": {"bandPx": band, "impureFrames": edge_frames},
        "keyDrift": {"median": float(np.median(key_drift)), "max": float(max(key_drift))},
        "changedShareFromFrame0": {"p90": busiest, "levels": QC_CHANGE_LEVELS},
    }
    metrics = json.loads(json.dumps(metrics, default=_plain))  # numpy scalars -> JSON numbers
    job_ref = {"path": ref_path(job.path, takes.resolve().parent), "sha256": job.sha256}
    return {
        "schema": TAKE_SCHEMA,
        "take": take_id,
        "status": "rejected" if reasons else "kept",
        "reasons": reasons,
        "warnings": warnings,
        "metrics": metrics,
        "job": job_ref,
        "action": job.action,
        "source": {**source, "frames": count, "size": list(size), "fps": fps},
        "qa": {
            "status": "fail" if reasons else "needs-visual-review",
            "method": "register_clip qc v1: frame 0 and the end calm span against the master as the job placed it; "
                      "36 px zero-mean NCC landmarks and head-region identity, +-15 px search; subject = RGB "
                      "distance > 64 from the take's border key (alpha > 16 for keyed frames); scale = sqrt of the "
                      "subject area ratio, drift = stance anchor shift",
            "notProven": ["mid-clip identity and anatomy (watch the take)",
                          "camera motion that returns to the reference before the end calm span",
                          "camera motion in takes that end in a new pose (victory, defeat, fx)"],
            "checks": checks,
            "inputs": [job_ref, *inputs],
            "outputs": [],
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        },
    }


def master_to_video(job: Job, fit: VideoFit, point: Sequence[float]) -> tuple[float, float]:
    """A master (sheet) px point in video px."""
    origin = job.view_box[:2] if job.view_box else (0, 0)
    px, py = point[0] - origin[0], point[1] - origin[1]
    return (fit.k[0] * (job.offset[0] + job.scale * px) + fit.c[0],
            fit.k[1] * (job.offset[1] + job.scale * py) + fit.c[1])


# --------------------------------------------------------------------------- palette-repair

def hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Hue in degrees [0, 360), saturation and value in [0, 1] of uint8 RGB."""
    colour = rgb.astype(np.float64)
    high, low = colour.max(-1), colour.min(-1)
    delta = high - low
    safe = np.where(delta > 0, delta, 1.0)
    red, green, blue = colour[..., 0], colour[..., 1], colour[..., 2]
    hue = np.where(high == red, ((green - blue) / safe) % 6.0,
                   np.where(high == green, (blue - red) / safe + 2.0, (red - green) / safe + 4.0)) * 60.0
    hue = np.where(delta > 0, hue, 0.0)
    saturation = np.where(high > 0, delta / np.where(high > 0, high, 1.0), 0.0)
    return hue, saturation, high / 255.0


def hue_selection(rgb: np.ndarray, hue_range: Sequence[float], min_saturation: float,
                  min_value: float) -> np.ndarray:
    hue, saturation, value = hsv(rgb)
    low, high = hue_range
    in_band = (hue >= low) & (hue <= high) if low <= high else (hue >= low) | (hue <= high)
    return in_band & (saturation >= min_saturation) & (value >= min_value)


def build_palette(master: np.ndarray, box: Sequence[int] | None, hue_range: Sequence[float],
                  min_saturation: float, min_value: float, size: int) -> np.ndarray:
    """The ``size`` most frequent opaque master colours outside the repaired hue band."""
    pixels = master if box is None else master[box[1]:box[3], box[0]:box[2]]
    opaque = pixels[..., 3] > 245
    allowed = opaque & ~hue_selection(pixels[..., :3], hue_range, min_saturation, min_value)
    colours, counts = np.unique(pixels[allowed][:, :3], axis=0, return_counts=True)
    if colours.size == 0:
        raise ValueError("The palette source has no opaque colours outside the repaired hue band")
    order = np.argsort(-counts, kind="stable")[:size]
    return colours[order]


def nearest_colours(colours: np.ndarray, palette: np.ndarray, chunk: int = 65536) -> np.ndarray:
    pal = palette.astype(np.int32)
    out = np.empty((colours.shape[0], 3), np.uint8)
    for begin in range(0, colours.shape[0], chunk):
        part = colours[begin:begin + chunk].astype(np.int32)
        distance = ((part[:, None, :] - pal[None, :, :]) ** 2).sum(-1)
        out[begin:begin + chunk] = palette[np.argmin(distance, axis=1)]
    return out


def cmd_palette_repair(args: argparse.Namespace) -> dict[str, Any]:
    files = list_frames(args.frames, args.pattern)
    if args.job:
        job = load_job(args.job)
        master = load_master_view(job)
        palette_source = job.master_path
    else:
        image, _info = forge_core.load_rgba(args.master)
        master = np.asarray(image)
        palette_source = Path(args.master)
    palette = build_palette(master, args.palette_box, args.hue, args.min_saturation, args.min_value, args.palette_size)
    output = Path(args.output_dir)
    final = output.parent.resolve() / output.name
    first, _ = _load_pixels(files[0])
    height, width = first.shape[:2]
    region = np.zeros((height, width), bool)
    for x0, y0, x1, y1 in args.region:
        region[max(0, y0):min(height, y1), max(0, x0):min(width, x1)] = True
    if not region.any():
        raise ValueError("--region lies outside the frames")
    union = np.zeros((height, width), bool)
    rows, inputs, outputs = [], [], []
    worst = 0.0
    with forge_core.staged_output(output) as stage:
        frames_dir = stage / "frames"
        frames_dir.mkdir()
        for path in files:
            pixels, digest = _load_pixels(path)
            if pixels.shape[:2] != (height, width):
                raise ValueError(f"{path.name} is {pixels.shape[1]}x{pixels.shape[0]}; the frames are {width}x{height}")
            selected = (region & (pixels[..., 3] >= args.min_alpha)
                        & hue_selection(pixels[..., :3], args.hue, args.min_saturation, args.min_value))
            visible = int((region & (pixels[..., 3] > 0)).sum())
            fraction = int(selected.sum()) / visible if visible else 0.0
            worst = max(worst, fraction)
            if fraction > args.max_changed_fraction:
                raise RegistrationQAError(
                    f"palette-repair QA failed, nothing was written: {fraction:.1%} of the visible region pixels in "
                    f"{path.name} match the rule (limit {args.max_changed_fraction:.0%}); narrow --hue or --region")
            repaired = pixels.copy()
            if selected.any():
                repaired[selected, :3] = nearest_colours(pixels[selected, :3], palette)
            if not np.array_equal(repaired[..., 3], pixels[..., 3]):
                raise RegistrationQAError("palette-repair changed alpha; nothing was written")
            union |= selected
            forge_core.save_png(repaired, frames_dir / path.name)
            after = forge_core.sha256_file(frames_dir / path.name)
            rows.append({"file": f"frames/{path.name}", "changedPx": int(selected.sum()), "regionVisiblePx": visible,
                         "beforeSha256": digest, "afterSha256": after})
            inputs.append({"path": ref_path(path, final), "sha256": digest})
            outputs.append({"path": f"frames/{path.name}", "sha256": after})
        forge_core.save_png((union * 255).astype(np.uint8), stage / "changed-mask.png")
        total = sum(row["changedPx"] for row in rows)
        report = {
            "schema": PALETTE_REPAIR_SCHEMA,
            "status": "needs-visual-review",
            "method": "register_clip palette-repair v1: inside the regions, opaque pixels (alpha >= min-alpha) whose "
                      "HSV hue is in the band (with saturation and value floors) take the nearest RGB colour of the "
                      "palette sampled from the master; alpha and every pixel outside the regions are unchanged",
            "notProven": ["that the replacement colours read correctly (compare frames before and after)",
                          "pixels of the flash outside the regions or below the alpha floor"],
            "checks": [
                {"id": "alpha-unchanged", "status": "pass", "value": True, "threshold": True},
                {"id": "changed-fraction", "status": "pass", "value": round(worst, 6),
                 "threshold": args.max_changed_fraction},
                {"id": "region-limited", "status": "pass", "value": 0, "threshold": 0},
            ],
            "inputs": inputs,
            "outputs": outputs,
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "rule": {"hueRange": list(args.hue), "minSaturation": args.min_saturation, "minValue": args.min_value,
                     "minAlpha": args.min_alpha},
            "regions": [list(box) for box in args.region],
            "paletteSource": {"path": ref_path(palette_source, final), "sha256": forge_core.sha256_file(palette_source),
                              "box": None if args.palette_box is None else list(args.palette_box)},
            "palette": ["#" + "".join(f"{int(v):02x}" for v in colour) for colour in palette],
            "frames": rows,
            "totalChangedPx": total,
            "alphaUnchanged": True,
            "changedMask": "changed-mask.png",
        }
        forge_core.write_json(stage / PALETTE_REPAIR_FILE, report)
    return {"output": str(final), "metadata": str(final / PALETTE_REPAIR_FILE), "frames": len(rows),
            "totalChangedPx": total, "palette": len(palette)}


# --------------------------------------------------------------------------- CLI

def _positive(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"needs a positive number; got {text!r}")
    return value


def _fraction(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise argparse.ArgumentTypeError(f"needs a number from 0 to 1; got {text!r}")
    return value


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"needs an integer >= 0; got {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    pa = sub.add_parser("apply", help="register keyed frames with the job's one fixed transform")
    pa.add_argument("--job", required=True, help="registration_job.json from prepare_i2v_input.py")
    pa.add_argument("--frames", required=True, help="folder of keyed RGBA frames at the video's size (natural order)")
    pa.add_argument("--pattern", default="*.png", help="frame file pattern (default *.png)")
    pa.add_argument("--output-dir", required=True, help="new folder for frames/ and registration.json")
    pa.add_argument("--range", type=parse_range, help="START:END frames to register (0-based, end exclusive)")
    pa.add_argument("--action-padding", type=prep.parse_padding,
                    help="L,T,R,B source px; grows the canvas and shifts the anchor (default: the job's padding)")
    pa.add_argument("--fit", choices=FITS, help="provider canvas to video: auto (default), stretch, cover, contain")
    pa.add_argument("--resampler", choices=forge_core.RESAMPLERS,
                    help="lanczos (default; box for 2x+ reductions), box, or nearest (exact 1/N pixel art only)")
    pa.add_argument("--lock", type=parse_locks, help="feet, x or hip (comma list): whole-pixel shifts to the master")
    pa.add_argument("--lock-max", type=_non_negative_int, help="largest lock shift in output px (default unlimited)")
    pa.add_argument("--ground-min-run", type=_non_negative_int,
                    help="px a row needs to count as the ground line (default 3)")
    pa.add_argument("--rest-frames", type=parse_frames_list,
                    help="source frames (0-based, before --range) that repeat the reference pose (default 0; "
                         "fx: none); 'none' skips the rest checks")
    pa.add_argument("--max-anchor-error", type=_positive, default=DEFAULT_MAX_ANCHOR_ERROR,
                    help="rest-pose anchor error limit in output px (default 2)")
    pa.add_argument("--max-scale-error", type=_positive, default=DEFAULT_MAX_SCALE_ERROR,
                    help="rest-pose height error limit (default 0.03)")
    pa.add_argument("--allow-edge-touch", action="store_true",
                    help="downgrade video-edge-touch and canvas-overflow to warnings (recorded)")
    pa.add_argument("--profile", choices=PROFILES, default="actor", help="actor (default) or fx")
    pa.add_argument("--edge-fade", type=_positive,
                    help="fx: boundary falloff in output px (default 8.5%% of the canvas)")
    pa.add_argument("--fade-in-frames", type=_non_negative_int, default=FX_FADE_IN_FRAMES,
                    help="fx: frames of appearance (default 2: the first frame is transparent)")
    pa.add_argument("--dissolve-tail", type=_fraction, default=FX_DISSOLVE_TAIL,
                    help="fx: share of the frames that dissolve to transparent at the end (default 0.2, >= 4 frames)")
    pa.add_argument("--display-scale", type=_positive,
                    help="runtime draw scale to record (fx default: padded canvas / base canvas, longest side)")
    pa.add_argument("--clip", help="clip name for profiles (default: the job's action)")
    pa.add_argument("--character-profile", help="character_profile.json to inherit; a mismatch fails the run")
    pa.add_argument("--scale-tolerance", type=_fraction, default=0.005,
                    help="relative referenceScale tolerance against the profile (default 0.005)")
    pa.set_defaults(func=cmd_apply)

    pr = sub.add_parser("profile", help="write a character profile from registered clips")
    pr.add_argument("--registration", action="append", required=True,
                    help="registration.json of a clip (repeat; the first one is the reference)")
    pr.add_argument("--id", required=True, help="character id")
    pr.add_argument("--output", required=True, help="new profile JSON file")
    pr.add_argument("--world-scale", type=_positive, help="world units per source px")
    pr.add_argument("--matte-mode", choices=MATTE_MODES, default="soft")
    pr.add_argument("--key", type=prep.normalise_key, help="matte key (default: the clips' key)")
    pr.add_argument("--erode", type=_non_negative_int, default=0, help="matte erosion in whole px (default 0)")
    pr.add_argument("--unmix", action=argparse.BooleanOptionalAction,
                    help="soft matte only: un-mix edge colours (default: on for soft, off for dominance and binary)")
    pr.add_argument("--despill-mode", choices=forge_matte.DESPILL_MODES, default="auto")
    pr.add_argument("--scale-tolerance", type=_fraction, default=0.005)
    pr.set_defaults(func=cmd_profile)

    pv = sub.add_parser("validate-profile", help="check registered clips against a character profile")
    pv.add_argument("--profile", required=True, help="character_profile.json")
    pv.add_argument("--registration", action="append", required=True, help="registration.json (repeat)")
    pv.add_argument("--foot-tolerance", type=float, default=2.0, help="rest foot line tolerance in px (default 2)")
    pv.add_argument("--scale-tolerance", type=_fraction, default=0.005)
    pv.set_defaults(func=cmd_validate_profile)

    pq = sub.add_parser("qc", help="judge a raw take and append a take.v1 line to takes.jsonl")
    pq.add_argument("--job", required=True, help="registration_job.json the take was generated from")
    source = pq.add_mutually_exclusive_group(required=True)
    source.add_argument("--frames", help="folder of the take's raw frames (natural order)")
    source.add_argument("--video", help="the take's video file (needs ffmpeg)")
    pq.add_argument("--pattern", default="*.png", help="frame file pattern (default *.png)")
    pq.add_argument("--take", help="take id (default: the video or folder name)")
    pq.add_argument("--takes", help="takes.jsonl to append to (default: next to the job)")
    pq.add_argument("--fps", type=_positive, default=24.0, help="frame rate of --frames (default 24)")
    pq.add_argument("--fit", choices=FITS, help="provider canvas to video (default auto)")
    pq.add_argument("--samples", type=int, default=9,
                    help="frames sampled for NCC (default 9, first and last included)")
    pq.add_argument("--landmarks", type=int, default=12, help="landmark patches (default 12)")
    pq.add_argument("--identity-box", type=prep.parse_box, help="X0,Y0,X1,Y1 identity region in master px "
                                                                "(default: the top 35%% of the subject)")
    pq.add_argument("--edge-band", type=int, help="border band in video px (default 60%% of the job margin)")
    pq.add_argument("--max-edge-impure", type=_fraction, default=DEFAULT_MAX_EDGE_IMPURE,
                    help="share of the border band allowed off-key (default 0.0005)")
    pq.add_argument("--max-scale-change", type=_positive, default=DEFAULT_MAX_SCALE_CHANGE,
                    help="calm-span camera scale limit (default 0.03)")
    pq.add_argument("--max-drift", type=_positive, default=DEFAULT_MAX_DRIFT,
                    help="start-span drift limit in source px (default 2)")
    pq.add_argument("--min-landmark-ncc", type=_fraction, default=DEFAULT_MIN_LANDMARK_NCC)
    pq.add_argument("--min-identity-ncc", type=_fraction, default=DEFAULT_MIN_IDENTITY_NCC)
    pq.add_argument("--strict", action="store_true",
                    help="exit 1 when the take is rejected (the line is still written)")
    pq.set_defaults(func=cmd_qc)

    pp = sub.add_parser("palette-repair", help="remap off-palette flashes inside regions to master colours")
    pp.add_argument("--frames", required=True, help="folder of registered RGBA frames")
    pp.add_argument("--pattern", default="*.png", help="frame file pattern (default *.png)")
    palette = pp.add_mutually_exclusive_group(required=True)
    palette.add_argument("--master", help="palette source image (RGBA)")
    palette.add_argument("--job", help="registration_job.json: use its master")
    pp.add_argument("--region", type=prep.parse_box, action="append", required=True,
                    help="X0,Y0,X1,Y1 frame px to repair (half-open; repeat for several)")
    pp.add_argument("--hue", type=parse_hue, required=True,
                    help="H0,H1 hue band in degrees to replace (wraps if H0 > H1)")
    pp.add_argument("--min-saturation", type=_fraction, default=0.3)
    pp.add_argument("--min-value", type=_fraction, default=0.1)
    pp.add_argument("--min-alpha", type=int, default=200, help="only pixels at least this opaque (default 200)")
    pp.add_argument("--palette-box", type=prep.parse_box, help="X0,Y0,X1,Y1 master px to sample (default: all)")
    pp.add_argument("--palette-size", type=int, default=128, help="most frequent master colours kept (default 128)")
    pp.add_argument("--max-changed-fraction", type=_fraction, default=0.6,
                    help="QA limit: share of a frame's visible region pixels the rule may change (default 0.6)")
    pp.add_argument("--output-dir", required=True, help="new folder for the repaired frames and the report")
    pp.set_defaults(func=cmd_palette_repair)
    return parser


def _finish_args(args: argparse.Namespace) -> None:
    if args.command == "qc" and (args.samples < 2 or args.landmarks < 1):
        raise ValueError("--samples needs at least 2 and --landmarks at least 1")
    if args.command == "palette-repair" and not (1 <= args.palette_size <= 4096 and 0 <= args.min_alpha <= 255):
        raise ValueError("--palette-size must be 1-4096 and --min-alpha 0-255")


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    """Parse argv and run the command; returns the JSON summary (raises on errors)."""
    args = build_parser().parse_args(argv)
    _finish_args(args)
    return args.func(args)


def _run(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(run(argv), ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors exit 2; refused input, failed registration QA, a failed profile validation and a take
    rejected under --strict exit 1 with ``error: ...``; anything unexpected prints ``error: internal error
    (...)`` (D26, D27; forge_core.run_cli). qc is an append-only verdict log, not a verify tool: a rejected
    take is recorded and exits 0 unless --strict."""
    import forge_av  # qc --video decodes with ffmpeg: its errors are refused input, not internal errors
    return forge_core.run_cli(_run, argv, expected=forge_core.CLI_EXPECTED_ERRORS + (forge_av.ForgeAVError,))


if __name__ == "__main__":
    raise SystemExit(main())
