#!/usr/bin/env python3
"""Build the motion mask of a masked environment loop from a motion plan (generate2dmap.motion_plan.v1).

The static plate stays sharp; only reviewed regions may show a provider clip's
motion. This tool rasterises the plan into an 8-bit mask over the plate:

* regions: ``polygon`` (points), ``rect`` (half-open box), ``luma_band`` (plate
  luma in [low, high], optionally limited to a box or polygon) and ``landmark``
  (an object's box or polygon, usually with a margin for its moving tips). A
  pixel belongs to a shape when its centre lies inside it. Inside the shape
  grown by ``margin`` the mask is 255 x ``strength``; it falls smoothly to 0
  over ``feather`` px outside, never across the region itself.
* protected: an exact-zero core (the shape grown by ``margin``) whose
  transition lies only outside it (``feather``, default --protect-feather), so
  no low-opacity motion leaks into a landmark: protectedMax is 0.
* --envelope-from CLIP measures where the clip really moves after the one fixed
  clip-to-plate transform. Moving pixels within --envelope-reach px of a region
  join that region, so its opaque core contains every moving pixel and the
  feather starts outside the motion. Motion inside a protected core or far from
  every region is excluded and reported: a mask cannot fix generated content.

Writes motion-mask.png (8-bit L), mask-overlay.png, mask-qa.json (a QA envelope
with protectedMax, coverage and mean opacity) and, with an envelope,
motion-envelope.png into a new --output-dir that is published only when
complete; a failed check publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
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
import forge_av  # noqa: E402  (this skill's vendored copies)
import forge_core  # noqa: E402


PLAN_SCHEMA = "generate2dmap.motion_plan.v1"
MASK_QA_SCHEMA = "generate2dmap.motion_mask_qa.v1"
TOOL = {"name": "build_motion_mask", "version": "1.0.0"}
REGION_KINDS = ("polygon", "rect", "luma_band", "landmark")
# flow (water, falls, mist, cloud, smoke) and flicker (fire, candles, light) only ever play forwards;
# sway (cloth, foliage, flags, hanging lamps) may play backwards, which pingpong loops need.
MOTION_CLASSES = ("flow", "flicker", "sway")
LOOP_POLICIES = ("forward-overlap", "pingpong")
FITS = ("contain", "cover")
DEFAULT_OVERLAP = 16
LUMA_WEIGHTS = np.array([0.299, 0.587, 0.114], np.float32)  # Rec. 601, as the hd2d cloud-band matte
MAX_FPS = 60

DEFAULT_PROTECT_FEATHER = 8.0
DEFAULT_ENVELOPE_THRESHOLD = 10.0
DEFAULT_ENVELOPE_REACH = 32.0
DEFAULT_MIN_AREA = 16
# Mean mask opacity below 7% hid the motion of a shipped scene (hd2d MOTION-v2: 6.96%).
DEFAULT_WARN_MEAN_OPACITY = 0.07

MOTION_TINT = np.array([13, 239, 217], np.float32)  # the Dusk mask-preview cyan
PROTECTED_TINT = np.array([230, 40, 40], np.float32)
EXCLUDED_TINT = np.array([255, 0, 255], np.float32)

MASK_NOT_PROVEN = [
    "Whether the regions are the right ones: the mask follows the plan; review the overlay at gameplay scale.",
    "Generated content: a mask cannot remove a hallucinated object, a ring on wet ground or a drifting landmark; "
    "regenerate the clip instead.",
    "The envelope is the clip's luma range per pixel after one fixed transform; motion below the threshold, "
    "or a provider camera drift the transform does not model, is not detected.",
    "Thresholds (mean opacity 0.07, envelope 10 luma levels) come from one project's scenes.",
]


# --------------------------------------------------------------------------- plan

def _number(value: Any, where: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{where} must be at least {minimum:g}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{where} must be at most {maximum:g}.")
    return float(value)


def _integer(value: Any, where: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be a whole number.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{where} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{where} must be at most {maximum}.")
    return value


def _points(value: Any, where: str) -> list[tuple[float, float]]:
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError(f"{where} must list at least three [x, y] points.")
    points = []
    for index, point in enumerate(value):
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError(f"{where}[{index}] must be [x, y].")
        points.append((_number(point[0], f"{where}[{index}]"), _number(point[1], f"{where}[{index}]")))
    return points


def _box(value: Any, where: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{where} must be a half-open box [x0, y0, x1, y1].")
    box = tuple(_number(item, where) for item in value)
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError(f"{where} needs x0 < x1 and y0 < y1.")
    return box  # type: ignore[return-value]


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object.")
    return value


def _unique_id(item: dict[str, Any], where: str, seen: set[str]) -> str:
    ident = item.get("id")
    if not isinstance(ident, str) or not ident:
        raise ValueError(f"{where}.id must be a non-empty string.")
    if ident in seen:
        raise ValueError(f"{where}.id {ident!r} is used twice.")
    seen.add(ident)
    return ident


def _region(item: Any, where: str, seen: set[str]) -> dict[str, Any]:
    item = _object(item, where)
    ident = _unique_id(item, where, seen)
    kind = item.get("kind")
    if kind not in REGION_KINDS:
        raise ValueError(f"{where}.kind must be one of {', '.join(REGION_KINDS)}.")
    motion = item.get("motion", "flow")
    if motion not in MOTION_CLASSES:
        raise ValueError(f"{where}.motion must be one of {', '.join(MOTION_CLASSES)} (default flow).")
    region = {
        "id": ident, "kind": kind, "motion": motion,
        "points": _points(item["points"], f"{where}.points") if "points" in item else None,
        "box": _box(item["box"], f"{where}.box") if "box" in item else None,
        "band": None,
        "margin": _number(item.get("margin", 0), f"{where}.margin", minimum=0),
        "feather": _number(item.get("feather"), f"{where}.feather", minimum=0),
        "strength": _number(item.get("strength"), f"{where}.strength", minimum=0, maximum=1),
    }
    if region["points"] is not None and region["box"] is not None:
        raise ValueError(f"{where} gives both points and box; use one.")
    if kind == "polygon" and region["points"] is None:
        raise ValueError(f"{where} is a polygon region and needs points.")
    if kind == "rect" and region["box"] is None:
        raise ValueError(f"{where} is a rect region and needs box.")
    if kind == "landmark" and region["points"] is None and region["box"] is None:
        raise ValueError(f"{where} is a landmark region and needs points or box.")
    if kind == "luma_band":
        band = item.get("band")
        if not isinstance(band, list) or len(band) != 2:
            raise ValueError(f"{where}.band must be [low, high] luma (0-255).")
        low = _number(band[0], f"{where}.band", minimum=0, maximum=255)
        high = _number(band[1], f"{where}.band", minimum=0, maximum=255)
        if low > high:
            raise ValueError(f"{where}.band must be ordered [low, high].")
        region["band"] = (low, high)
    return region


def _protected(item: Any, where: str, seen: set[str], default_feather: float) -> dict[str, Any]:
    item = _object(item, where)
    ident = _unique_id(item, where, seen)
    if ("polygon" in item) == ("box" in item):
        raise ValueError(f"{where} needs exactly one of polygon or box.")
    return {
        "id": ident,
        "points": _points(item["polygon"], f"{where}.polygon") if "polygon" in item else None,
        "box": _box(item["box"], f"{where}.box") if "box" in item else None,
        "margin": _number(item.get("margin", 0), f"{where}.margin", minimum=0),
        "feather": _number(item.get("feather", default_feather), f"{where}.feather", minimum=0),
    }


def normalize_plan(document: Any, *, protect_feather: float = DEFAULT_PROTECT_FEATHER) -> dict[str, Any]:
    """Validate a motion_plan.v1 document and return its normalised form.

    Checks what the JSON Schema checks plus the semantics a renderer needs
    (ordered boxes and bands, unique ids, overlap only with forward-overlap).
    Optional extensions read here: region ``motion`` (flow|flicker|sway,
    default flow), protected ``feather`` and ``registration`` ({fit} or
    {scale, offset}). Raises ValueError naming the offending field.
    """
    plan = _object(document, "plan")
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"plan.schema must be {PLAN_SCHEMA!r}.")
    size = plan.get("sourceSize")
    if not isinstance(size, list) or len(size) != 2:
        raise ValueError("plan.sourceSize must be [width, height].")
    source_size = (_integer(size[0], "sourceSize", minimum=1), _integer(size[1], "sourceSize", minimum=1))
    raw_regions = plan.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise ValueError("plan.regions must be a non-empty list.")
    region_ids: set[str] = set()
    regions = [_region(item, f"regions[{index}]", region_ids) for index, item in enumerate(raw_regions)]
    raw_protected = plan.get("protected", [])
    if not isinstance(raw_protected, list):
        raise ValueError("plan.protected must be a list.")
    protected_ids: set[str] = set()
    protected = [_protected(item, f"protected[{index}]", protected_ids, protect_feather)
                 for index, item in enumerate(raw_protected)]
    loop = _object(plan.get("loop"), "plan.loop")
    policy = loop.get("policy")
    if policy not in LOOP_POLICIES:
        raise ValueError(f"loop.policy must be one of {', '.join(LOOP_POLICIES)}.")
    if policy == "pingpong":
        overlap = _integer(loop.get("overlap", 0), "loop.overlap", minimum=0)
        if overlap:
            raise ValueError("loop.overlap belongs to forward-overlap; a pingpong loop has none.")
    else:
        overlap = _integer(loop.get("overlap", DEFAULT_OVERLAP), "loop.overlap", minimum=0)
    frame_range = None
    if "range" in loop:
        values = loop["range"]
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError("loop.range must be [start, endExclusive] source frames.")
        frame_range = (_integer(values[0], "loop.range", minimum=0), _integer(values[1], "loop.range", minimum=1))
        if frame_range[0] >= frame_range[1]:
            raise ValueError("loop.range must be [start, endExclusive] with start < endExclusive.")
    encode = _object(plan.get("encode", {}), "plan.encode")
    crf = _integer(encode["crf"], "encode.crf", minimum=1, maximum=51) if "crf" in encode else None
    keyint = _integer(encode["keyint"], "encode.keyint", minimum=1) if "keyint" in encode else None
    registration = _object(plan.get("registration", {}), "plan.registration")
    fit = registration.get("fit", "contain")
    if fit not in FITS:
        raise ValueError(f"registration.fit must be one of {', '.join(FITS)}.")
    scale = offset = None
    if "scale" in registration or "offset" in registration:
        scale = _number(registration.get("scale"), "registration.scale", minimum=1e-6)
        values = registration.get("offset")
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError("registration.offset must be [x, y] plate pixels.")
        offset = (_number(values[0], "registration.offset"), _number(values[1], "registration.offset"))
    return {
        "sourceSize": source_size, "regions": regions, "protected": protected,
        "loop": {"policy": policy, "overlap": overlap, "range": frame_range},
        "encode": {"crf": crf, "keyint": keyint},
        "registration": {"fit": "explicit" if scale is not None else fit, "scale": scale, "offset": offset},
    }


def load_plan(path: str | os.PathLike, **options: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read and normalise a motion plan file; returns (normalised plan, original document)."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{Path(path).name} is not valid JSON: {error}") from None
    return normalize_plan(document, **options), document


# --------------------------------------------------------------------------- rasterising and distances

def rasterize_box(box: Sequence[float], size: tuple[int, int]) -> np.ndarray:
    """Pixels whose centre lies in the half-open box [x0, x1) x [y0, y1)."""
    width, height = size
    mask = np.zeros((height, width), bool)
    x0, x1 = (min(width, max(0, math.ceil(value - 0.5))) for value in (box[0], box[2]))
    y0, y1 = (min(height, max(0, math.ceil(value - 0.5))) for value in (box[1], box[3]))
    mask[y0:y1, x0:x1] = True
    return mask


def rasterize_polygon(points: Sequence[Sequence[float]], size: tuple[int, int]) -> np.ndarray:
    """Pixels whose centre lies inside the polygon (even-odd rule; left edges in, right edges out).

    A scanline fill on pixel centres, so a rectangle polygon gives the same
    pixels as the half-open box with its corners, on every platform.
    """
    width, height = size
    mask = np.zeros((height, width), bool)
    xs = np.array([float(point[0]) for point in points])
    ys = np.array([float(point[1]) for point in points])
    top = max(0, math.floor(ys.min() - 0.5))
    bottom = min(height, math.ceil(ys.max() + 0.5))
    if top >= bottom:
        return mask
    x_next, y_next = np.roll(xs, -1), np.roll(ys, -1)
    low, high = np.minimum(ys, y_next), np.maximum(ys, y_next)
    centres = np.arange(top, bottom, dtype=np.float64) + 0.5
    crossing = (centres[:, None] >= low[None, :]) & (centres[:, None] < high[None, :])
    rows, edges = np.nonzero(crossing)
    if rows.size == 0:
        return mask
    slope = (x_next - xs)[edges] / (y_next - ys)[edges]
    x_cross = xs[edges] + (centres[rows] - ys[edges]) * slope
    columns = np.clip(np.ceil(x_cross - 0.5), 0, width).astype(np.int64)
    toggles = np.zeros((bottom - top, width + 1), np.int32)
    np.add.at(toggles, (rows, columns), 1)
    mask[top:bottom] = (np.cumsum(toggles, axis=1)[:, :width] & 1).astype(bool)
    return mask


def _ndimage() -> Any:
    """scipy.ndimage, or None when scipy is missing or FORGE_CORE_NO_SCIPY asks for the numpy path."""
    if os.environ.get("FORGE_CORE_NO_SCIPY", "") not in ("", "0"):
        return None
    try:
        from scipy import ndimage
    except ImportError:
        return None
    return ndimage


def _bounded_distance(mask: np.ndarray, radius: int) -> np.ndarray:
    """Exact squared Euclidean distances up to ``radius`` px (numpy only; larger values are > radius**2).

    Separable: the vertical distance to the nearest True pixel of each column,
    then the minimum of dx**2 + vertical**2 over |dx| <= radius.
    """
    height, width = mask.shape
    rows = np.arange(height)[:, None]
    far = height + radius + 2
    above = rows - np.maximum.accumulate(np.where(mask, rows, -far), axis=0)
    below = np.minimum.accumulate(np.where(mask, rows, height + far)[::-1], axis=0)[::-1] - rows
    vertical = np.minimum(np.minimum(above, below), radius + 1).astype(np.int64)
    squared = vertical * vertical
    best = squared.copy()
    for dx in range(1, min(radius, width - 1) + 1):
        cost = dx * dx
        np.minimum(best[:, dx:], squared[:, :-dx] + cost, out=best[:, dx:])
        np.minimum(best[:, :-dx], squared[:, dx:] + cost, out=best[:, :-dx])
    return best


def distance_from(mask: np.ndarray, limit: float) -> np.ndarray:
    """Euclidean distance (px, centre to centre) from every pixel to the nearest True pixel.

    Exact up to ``limit``; farther pixels are +inf. scipy.ndimage and the
    numpy fallback give identical values. Work is limited to the mask's
    bounding box grown by ``limit``.
    """
    mask = np.asarray(mask, bool)
    result = np.full(mask.shape, np.inf)
    rows, columns = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    if rows.size == 0:
        return result
    pad = int(math.floor(limit)) + 1
    y0, y1 = max(0, rows[0] - pad), min(mask.shape[0], rows[-1] + pad + 1)
    x0, x1 = max(0, columns[0] - pad), min(mask.shape[1], columns[-1] + pad + 1)
    crop = mask[y0:y1, x0:x1]
    ndimage = _ndimage()
    if ndimage is not None:
        distance = ndimage.distance_transform_edt(~crop)
    else:
        distance = np.sqrt(_bounded_distance(crop, int(math.floor(limit))).astype(np.float64))
    distance[distance > limit] = np.inf
    result[y0:y1, x0:x1] = distance
    return result


def _smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def ramp(distance: np.ndarray, margin: float, feather: float) -> np.ndarray:
    """1 within ``margin`` px of the shape, falling smoothly to 0 at ``margin + feather`` px (float32)."""
    value = (distance <= margin).astype(np.float32)
    if feather > 0:
        band = (distance > margin) & (distance < margin + feather)
        value[band] = 1.0 - _smoothstep((distance[band] - margin) / feather)
    return value


def plate_luma(rgb: np.ndarray) -> np.ndarray:
    """Rec. 601 luma (0-255, float32) after a 3x3 box blur, so single-pixel texture does not speckle a band."""
    luma = rgb.astype(np.float32) @ LUMA_WEIGHTS
    padded = np.pad(luma, 1, mode="edge")
    rows = padded[:-2] + padded[1:-1] + padded[2:]
    return (rows[:, :-2] + rows[:, 1:-1] + rows[:, 2:]) / 9.0


def drop_small(mask: np.ndarray, min_area: int) -> np.ndarray:
    """``mask`` without its 8-connected components smaller than ``min_area`` px."""
    if min_area <= 1 or not mask.any():
        return mask.copy()
    labels, count = forge_core.label_components(mask, 8)
    areas = np.bincount(labels.ravel(), minlength=count + 1)
    keep = areas >= min_area
    keep[0] = False
    return keep[labels]


def shape_mask(item: dict[str, Any], size: tuple[int, int]) -> np.ndarray:
    """The polygon or box of a region or protected entry; a luma band without either covers the plate."""
    if item.get("points") is not None:
        return rasterize_polygon(item["points"], size)
    if item.get("box") is not None:
        return rasterize_box(item["box"], size)
    return np.ones((size[1], size[0]), bool)


# --------------------------------------------------------------------------- clip and transform

def canonical_fps(value: Any) -> str:
    """A frame rate as an exact 'num/den' string (24 -> '24/1'); at most 60 fps."""
    try:
        rate = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"frame rate {value!r} must look like 24 or 30000/1001.") from None
    if rate <= 0:
        raise ValueError(f"frame rate {value!r} must be positive.")
    if rate > MAX_FPS:
        raise ValueError(f"frame rate {value} is above {MAX_FPS} fps; retime the clip first.")
    return f"{rate.numerator}/{rate.denominator}"


def _natural_key(path: Path) -> list[Any]:
    return [int(part) if index % 2 else part.lower() for index, part in enumerate(re.split(r"(\d+)", path.name))]


class Clip:
    """A provider clip: a video file decoded with forge_av, or a folder of PNG frames (natural order)."""

    def __init__(self, path: str | os.PathLike, fps: Any = None) -> None:
        self.path = Path(path)
        if self.path.is_dir():
            self.kind = "frames"
            self.files = sorted(self.path.glob("*.png"), key=_natural_key)
            if not self.files:
                raise ValueError(f"{self.path} holds no PNG frames.")
            with Image.open(self.files[0]) as first:
                self.size = first.size
            self.count = len(self.files)
            if fps is None:
                raise ValueError("a folder of frames needs --fps (for example 24 or 30000/1001).")
            self.fps = canonical_fps(fps)
        elif self.path.is_file():
            self.kind = "video"
            self.files = []
            info = forge_av.probe(self.path)
            self.size = (int(info["width"]), int(info["height"]))
            self.count = int(info["nb_frames"])
            rate = fps if fps is not None else info["fps_rational"]
            if rate is None:
                raise ValueError(f"cannot read the frame rate of {self.path.name}; pass --fps.")
            self.fps = canonical_fps(rate)
        else:
            raise FileNotFoundError(f"clip not found: {self.path}")

    def frames(self, start: int, stop: int) -> Iterator[tuple[int, np.ndarray]]:
        """Yield ``(index, rgb)`` for start <= index < stop; uint8 (H, W, 3), opaque frames only."""
        if self.kind == "frames":
            if stop > self.count:
                raise ValueError(f"{self.path.name} holds {self.count} frames; the range ends at {stop}.")
            for index in range(start, stop):
                image, _info = forge_core.load_rgba(self.files[index])
                yield index, self._checked(index, np.asarray(image))
            return
        stream = forge_av.iter_rgba(self.path, alpha="off")
        decoded = 0
        try:
            for index, frame in enumerate(stream):
                decoded = index + 1
                if index >= start:
                    yield index, self._checked(index, frame)
                if decoded >= stop:
                    break
        finally:
            stream.close()
        if decoded < stop:
            raise ValueError(f"{self.path.name} decodes to {decoded} frames; the range ends at {stop}.")

    def _checked(self, index: int, pixels: np.ndarray) -> np.ndarray:
        if (pixels.shape[1], pixels.shape[0]) != self.size:
            raise ValueError(f"clip frame {index} is {pixels.shape[1]}x{pixels.shape[0]}, not "
                             f"{self.size[0]}x{self.size[1]}.")
        if pixels.shape[2] == 4 and pixels[..., 3].min() < 255:
            raise ValueError(f"clip frame {index} has transparency; a background clip must be opaque.")
        return np.ascontiguousarray(pixels[..., :3])

    def fingerprint(self, base: Path) -> dict[str, Any]:
        """A fileRef for the clip; a frame folder is hashed as the sha256 of its frames' sha256 list."""
        if self.kind == "video":
            reference = file_ref(self.path, base)
        else:
            digests = "\n".join(forge_core.sha256_file(path) for path in self.files)
            reference = {"path": _relative(self.path, base), "sha256": forge_core.sha256_bytes(digests.encode())}
        return {**reference, "kind": self.kind, "frames": self.count, "size": list(self.size), "fps": self.fps}


@dataclass(frozen=True)
class Transform:
    """The one fixed clip-to-plate mapping: plate = clip * scale + offset (continuous pixel coordinates)."""
    scale: float
    offset: tuple[float, float]
    clip_size: tuple[int, int]
    plate_size: tuple[int, int]
    fit: str

    @property
    def footprint(self) -> tuple[float, float, float, float]:
        x0, y0 = self.offset
        return x0, y0, x0 + self.clip_size[0] * self.scale, y0 + self.clip_size[1] * self.scale

    @property
    def identity_paste(self) -> bool:
        return self.scale == 1.0 and all(float(value).is_integer() for value in self.offset)

    def describe(self) -> dict[str, Any]:
        return {"fit": self.fit, "scale": self.scale, "offset": list(self.offset),
                "clipSize": list(self.clip_size), "plateSize": list(self.plate_size),
                "footprint": list(self.footprint), "map": "plate = clip * scale + offset"}


def make_transform(clip_size: tuple[int, int], plate_size: tuple[int, int], *, fit: str = "contain",
                   scale: float | None = None, offset: tuple[float, float] | None = None) -> Transform:
    """``contain`` (default) scales the clip uniformly to fit inside the plate and centres it, which undoes a
    provider's centre crop; ``cover`` fills the plate; an explicit scale and offset win over both."""
    if scale is not None:
        return Transform(float(scale), (float(offset[0]), float(offset[1])), tuple(clip_size),
                         tuple(plate_size), "explicit")
    if fit not in FITS:
        raise ValueError(f"fit must be one of {', '.join(FITS)}.")
    (clip_w, clip_h), (plate_w, plate_h) = clip_size, plate_size
    pick = min if fit == "contain" else max
    factor = pick(plate_w / clip_w, plate_h / clip_h)
    return Transform(factor, ((plate_w - clip_w * factor) / 2, (plate_h - clip_h * factor) / 2),
                     tuple(clip_size), tuple(plate_size), fit)


def plan_transform(plan: dict[str, Any], clip_size: tuple[int, int], plate_size: tuple[int, int],
                   fit: str | None = None, explicit: Sequence[float] | None = None) -> Transform:
    """The transform from CLI overrides (``explicit`` = scale, x, y; or ``fit``), else the plan's registration."""
    if explicit is not None:
        return make_transform(clip_size, plate_size, scale=explicit[0], offset=(explicit[1], explicit[2]))
    registration = plan["registration"]
    if fit is None and registration["scale"] is not None:
        return make_transform(clip_size, plate_size, scale=registration["scale"], offset=registration["offset"])
    return make_transform(clip_size, plate_size, fit=fit or registration["fit"])


def align_frame(rgb: np.ndarray, transform: Transform) -> np.ndarray:
    """A clip frame resampled onto the plate grid (uint8 RGB, plate size; black outside the footprint).

    The frame is opaque, so Pillow's Lanczos resizes it directly (nothing to
    premultiply) with sub-pixel placement from the resize box; the clip's edge
    pixels are replicated so the border of the footprint never darkens. Only
    the plate window under the footprint is computed. A scale of 1 with
    whole-pixel offsets is an exact copy.
    """
    width, height = transform.plate_size
    out = np.zeros((height, width, 3), np.uint8)
    clip_h, clip_w = rgb.shape[:2]
    if transform.identity_paste:
        ox, oy = int(transform.offset[0]), int(transform.offset[1])
        src_x0, src_y0 = max(0, -ox), max(0, -oy)
        dst_x0, dst_y0 = max(0, ox), max(0, oy)
        w = min(clip_w - src_x0, width - dst_x0)
        h = min(clip_h - src_y0, height - dst_y0)
        if w > 0 and h > 0:
            out[dst_y0:dst_y0 + h, dst_x0:dst_x0 + w] = rgb[src_y0:src_y0 + h, src_x0:src_x0 + w]
        return out
    scale, (ox, oy) = transform.scale, transform.offset
    fx0, fy0, fx1, fy1 = transform.footprint
    x0, y0 = max(0, math.floor(fx0) - 1), max(0, math.floor(fy0) - 1)
    x1, y1 = min(width, math.ceil(fx1) + 1), min(height, math.ceil(fy1) + 1)
    if x0 >= x1 or y0 >= y1:
        return out
    box = ((x0 - ox) / scale, (y0 - oy) / scale, (x1 - ox) / scale, (y1 - oy) / scale)
    support = 3.0 * max(1.0, 1.0 / scale)  # Lanczos-3 reach in clip pixels
    pad = math.ceil(max(0.0, -box[0], -box[1], box[2] - clip_w, box[3] - clip_h) + support) + 1
    padded = Image.fromarray(np.pad(rgb, ((pad, pad), (pad, pad), (0, 0)), mode="edge"))
    window = padded.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS, box=tuple(value + pad for value in box))
    out[y0:y1, x0:x1] = np.asarray(window)
    return out


def coverage_map(transform: Transform, fade: float) -> np.ndarray:
    """Where the clip covers the plate (float32 0..1), fading over ``fade`` px from clip edges that lie inside it.

    Edges within half a pixel of the plate border need no fade; a real gap (a
    provider centre crop seen through ``contain``) fades so masked motion never
    shows a hard clip edge (Dusk build-scenes).
    """
    width, height = transform.plate_size
    x0, y0, x1, y1 = transform.footprint

    def axis(count: int, start: float, end: float) -> np.ndarray:
        centres = np.arange(count, dtype=np.float64) + 0.5
        value = ((centres >= start) & (centres < end)).astype(np.float64)
        if fade > 0 and start >= 0.5:
            value *= _smoothstep((centres - start) / fade)
        if fade > 0 and count - end >= 0.5:
            value *= _smoothstep((end - centres) / fade)
        return value

    return np.outer(axis(height, y0, y1), axis(width, x0, x1)).astype(np.float32)


def motion_envelope(clip: Clip, start: int, stop: int, transform: Transform, *,
                    threshold: float = DEFAULT_ENVELOPE_THRESHOLD,
                    min_area: int = DEFAULT_MIN_AREA) -> tuple[np.ndarray, dict[str, Any]]:
    """Pixels whose blurred luma range over the clip frames exceeds ``threshold`` (plate grid, bool).

    Pixels outside the clip footprint never count; components smaller than
    ``min_area`` px (codec speckle) are dropped.
    """
    low = high = None
    frames = 0
    for _index, rgb in clip.frames(start, stop):
        luma = plate_luma(align_frame(rgb, transform))
        low = luma if low is None else np.minimum(low, luma)
        high = luma if high is None else np.maximum(high, luma)
        frames += 1
    if low is None or frames < 2:
        raise ValueError("the envelope needs at least two clip frames.")
    covered = coverage_map(transform, 0.0) > 0
    moving = drop_small(((high - low) > threshold) & covered, min_area)
    return moving, {"frames": frames, "range": [start, stop], "threshold": threshold, "minArea": min_area,
                    "lumaRangeMax": round(float((high - low)[covered].max(initial=0.0)), 3)}


# --------------------------------------------------------------------------- the mask

@dataclass
class MaskBuild:
    mask: np.ndarray                      # uint8 (H, W)
    region_values: dict[str, np.ndarray]  # strength x ramp per region, before protection (float32)
    protection: np.ndarray                # 1 in protected cores, falling to 0 outside (float32)
    protected_cores: dict[str, np.ndarray]
    envelope: dict[str, Any] | None
    excluded_motion: np.ndarray | None    # moving pixels the mask refuses (bool) or None


def build_mask(plan: dict[str, Any], plate_rgb: np.ndarray, *, envelope: np.ndarray | None = None,
               envelope_reach: float = DEFAULT_ENVELOPE_REACH, luma_min_area: int = DEFAULT_MIN_AREA) -> MaskBuild:
    """Rasterise a normalised plan over the plate (see the module docstring for the rules)."""
    height, width = plate_rgb.shape[:2]
    size = (width, height)
    protection = np.zeros((height, width), np.float32)
    cores: dict[str, np.ndarray] = {}
    for item in plan["protected"]:
        distance = distance_from(shape_mask(item, size), item["margin"] + item["feather"])
        cores[item["id"]] = distance <= item["margin"]
        np.maximum(protection, ramp(distance, item["margin"], item["feather"]), out=protection)
    in_core = np.zeros((height, width), bool)
    for core in cores.values():
        in_core |= core
    luma = plate_luma(plate_rgb) if any(region["kind"] == "luma_band" for region in plan["regions"]) else None
    values: dict[str, np.ndarray] = {}
    admitted = np.zeros((height, width), bool)
    for region in plan["regions"]:
        shape = shape_mask(region, size)
        if region["kind"] == "luma_band":
            low, high = region["band"]
            shape = drop_small(shape & (luma >= low) & (luma <= high), luma_min_area)
        if envelope is not None and region["strength"] > 0:
            reach = distance_from(shape, envelope_reach) <= envelope_reach
            joined = envelope & reach & ~in_core
            admitted |= joined
            shape = shape | joined
        distance = distance_from(shape, region["margin"] + region["feather"])
        values[region["id"]] = ramp(distance, region["margin"], region["feather"]) * np.float32(region["strength"])
    combined = np.zeros((height, width), np.float32)
    for value in values.values():
        np.maximum(combined, value, out=combined)
    mask = np.floor(combined * (1.0 - protection) * 255.0 + 0.5).astype(np.uint8)
    mask[in_core] = 0
    stats = excluded = None
    if envelope is not None:
        excluded = envelope & ~admitted
        stats = {
            "movingPx": int(envelope.sum()),
            "admittedPx": int(admitted.sum()),
            "containedPx": int((admitted & (mask > 0)).sum()),
            "reducedByProtectionPx": int((admitted & (protection > 0)).sum()),
            "uncoveredPx": int((admitted & (mask == 0) & (protection == 0)).sum()),
            "excludedProtectedPx": int((envelope & in_core).sum()),
            "excludedOutsidePx": int((excluded & ~in_core).sum()),
            "reach": envelope_reach,
        }
    return MaskBuild(mask, values, protection, cores, stats, excluded)


def mask_statistics(build: MaskBuild) -> dict[str, Any]:
    """protectedMax, coverage and mean opacity of the finished mask, with per-region and per-core figures."""
    mask = build.mask
    union = np.zeros(mask.shape, bool)
    protected = []
    for ident, core in build.protected_cores.items():
        union |= core
        protected.append({"id": ident, "corePx": int(core.sum()), "max": int(mask[core].max(initial=0))})
    regions = []
    for ident, value in build.region_values.items():
        support = value > 0
        regions.append({"id": ident, "supportPx": int(support.sum()),
                        "visiblePx": int((support & (mask > 0)).sum()),
                        "meanOpacity": round(float(mask[support].mean() / 255.0), 6) if support.any() else 0.0})
    return {
        "protectedMax": int(mask[union].max(initial=0)),
        "coverage": round(float((mask > 0).mean()), 6),
        "coverageOver50": round(float((mask > 127).mean()), 6),
        "meanOpacity": round(float(mask.mean() / 255.0), 6),
        "regions": regions, "protected": protected,
    }


def mask_checks(statistics: dict[str, Any], envelope: dict[str, Any] | None,
                warn_mean_opacity: float = DEFAULT_WARN_MEAN_OPACITY) -> list[dict[str, Any]]:
    """QA checks of a built mask (protectedMax 0, non-empty, visible regions, mean opacity, envelope)."""
    checks = [
        {"id": "protected_max", "status": "pass" if statistics["protectedMax"] == 0 else "fail",
         "value": statistics["protectedMax"], "threshold": 0},
        {"id": "mask_not_empty", "status": "pass" if statistics["coverage"] > 0 else "fail",
         "value": statistics["coverage"], "threshold": "> 0"},
    ]
    hidden = [region["id"] for region in statistics["regions"] if region["visiblePx"] == 0]
    checks.append({"id": "regions_visible", "status": "warn" if hidden else "pass", "value": hidden,
                   "threshold": "every region shows at least one pixel"})
    checks.append({"id": "mean_opacity", "status": "warn" if statistics["meanOpacity"] < warn_mean_opacity else "pass",
                   "value": statistics["meanOpacity"], "threshold": warn_mean_opacity})
    if envelope is not None:
        checks.append({"id": "envelope_contained", "status": "pass" if envelope["uncoveredPx"] == 0 else "fail",
                       "value": envelope["uncoveredPx"], "threshold": 0})
        excluded = envelope["excludedProtectedPx"] + envelope["excludedOutsidePx"]
        checks.append({"id": "envelope_excluded", "status": "warn" if excluded else "pass",
                       "value": {"protected": envelope["excludedProtectedPx"],
                                 "outsideRegions": envelope["excludedOutsidePx"]}, "threshold": 0})
    return checks


def overall_status(checks: Sequence[dict[str, Any]]) -> str:
    """Overall QA status: fail beats warn beats pass."""
    statuses = {check["status"] for check in checks}
    return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"


def render_overlay(plate_rgb: np.ndarray, build: MaskBuild) -> Image.Image:
    """Plate tinted cyan by the mask, protected cores red, excluded moving pixels magenta."""
    out = plate_rgb.astype(np.float32)
    weight = (build.mask.astype(np.float32) / 255.0 * 0.55)[..., None]
    out = out * (1.0 - weight) + MOTION_TINT * weight
    for core in build.protected_cores.values():
        out[core] = out[core] * 0.5 + PROTECTED_TINT * 0.5
    if build.excluded_motion is not None:
        out[build.excluded_motion] = EXCLUDED_TINT
    return Image.fromarray(np.clip(np.floor(out + 0.5), 0, 255).astype(np.uint8))


# --------------------------------------------------------------------------- files

def _relative(path: Path, base: Path) -> str:
    """Manifest-relative POSIX path, or the bare file name when no relative route exists (another drive)."""
    relative = forge_core.portable_path(path, base)
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        return Path(path).name
    return relative


def file_ref(path: str | os.PathLike, base: str | os.PathLike, sha256: str | None = None) -> dict[str, Any]:
    """A common fileRef: manifest-relative path (or file name on another drive), sha256 and bytes."""
    path = Path(path)
    return {"path": _relative(path, Path(base)), "sha256": sha256 or forge_core.sha256_file(path),
            "bytes": path.stat().st_size}


def load_plate(path: str | os.PathLike, plan: dict[str, Any] | None = None) -> tuple[np.ndarray, dict[str, Any]]:
    """The plate as uint8 RGB; it must be opaque and match plan.sourceSize."""
    image, info = forge_core.load_rgba(path)
    pixels = np.asarray(image)
    if pixels[..., 3].min() < 255:
        raise ValueError(f"{Path(path).name} has transparent pixels; a scene plate must be opaque.")
    if plan is not None and tuple(image.size) != tuple(plan["sourceSize"]):
        raise ValueError(f"plate is {image.size[0]}x{image.size[1]} but plan.sourceSize is "
                         f"{plan['sourceSize'][0]}x{plan['sourceSize'][1]}.")
    return np.ascontiguousarray(pixels[..., :3]), info


def load_mask(path: str | os.PathLike, size: tuple[int, int]) -> np.ndarray:
    """An 8-bit mask PNG (L, or the alpha of LA/RGBA) of the plate's size, as uint8 (H, W)."""
    with Image.open(path) as image:
        image.load()
        if image.size != tuple(size):
            raise ValueError(f"mask is {image.size[0]}x{image.size[1]}; the plate is {size[0]}x{size[1]}.")
        if image.mode == "L":
            return np.asarray(image).copy()
        if image.mode in ("LA", "RGBA", "PA"):
            return np.asarray(image.convert("RGBA").getchannel("A")).copy()
        raise ValueError(f"mask mode {image.mode} is not supported; write an 8-bit L PNG.")


# --------------------------------------------------------------------------- command line

def _transform_option(text: str) -> tuple[float, float, float]:
    try:
        values = tuple(float(part) for part in text.split(","))
    except ValueError:
        values = ()
    if len(values) != 3 or not all(math.isfinite(value) for value in values) or values[0] <= 0:
        raise argparse.ArgumentTypeError("use SCALE,OFFSET_X,OFFSET_Y in plate pixels, e.g. 1.30625,0,0.25")
    return values  # type: ignore[return-value]


def add_registration_options(parser: argparse.ArgumentParser) -> None:
    """--fit / --transform / --fps, shared with scene_motion.py."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fit", choices=FITS,
                       help="Clip-to-plate fit: contain (default; undoes a provider centre crop) or cover. "
                            "Overrides plan.registration.")
    group.add_argument("--transform", type=_transform_option, metavar="S,X,Y",
                       help="Explicit fixed transform: plate = clip * S + (X, Y).")
    parser.add_argument("--fps", help="Frame rate of a frame-folder clip (24 or 30000/1001); "
                                      "overrides a video's own rate.")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.envelope_reach < 0 or args.envelope_threshold < 0 or args.protect_feather < 0:
        raise ValueError("--envelope-reach, --envelope-threshold and --protect-feather must be zero or more.")
    plan, _document = load_plan(args.plan, protect_feather=args.protect_feather)
    plate, plate_info = load_plate(args.plate, plan)
    size = (plate.shape[1], plate.shape[0])
    envelope = envelope_info = clip = None
    if args.envelope_from is not None:
        clip = Clip(args.envelope_from, args.fps)
        start, stop = plan["loop"]["range"] or (0, clip.count)
        transform = plan_transform(plan, clip.size, size, args.fit, args.transform)
        envelope, envelope_info = motion_envelope(clip, start, stop, transform, threshold=args.envelope_threshold,
                                                  min_area=args.envelope_min_area)
        envelope_info["transform"] = transform.describe()
    build = build_mask(plan, plate, envelope=envelope, envelope_reach=args.envelope_reach,
                       luma_min_area=args.luma_min_area)
    statistics = mask_statistics(build)
    checks = mask_checks(statistics, build.envelope, args.warn_mean_opacity)
    status = overall_status(checks)
    if status == "fail":
        failed = ", ".join(f"{check['id']}={check['value']}" for check in checks if check["status"] == "fail")
        raise ValueError(f"mask QA failed ({failed}); nothing was written.")
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        forge_core.save_png(Image.fromarray(build.mask), stage / "motion-mask.png")
        forge_core.save_png(render_overlay(plate, build), stage / "mask-overlay.png")
        outputs = ["motion-mask.png", "mask-overlay.png"]
        if envelope is not None:
            forge_core.save_png(Image.fromarray(envelope.astype(np.uint8) * 255), stage / "motion-envelope.png")
            outputs.append("motion-envelope.png")
        inputs = [file_ref(args.plan, final), file_ref(args.plate, final, plate_info["sha256"])]
        if clip is not None:
            inputs.append(clip.fingerprint(final))
        report = {
            "schema": MASK_QA_SCHEMA, "status": status,
            "method": "build_motion_mask: plan regions rasterised on pixel centres, grown by margin and feathered "
                      "outside by exact Euclidean distance; protected cores forced to 0 with their transition "
                      "outside; optional clip luma-range envelope admitted within reach of each region.",
            "notProven": list(MASK_NOT_PROVEN), "checks": checks, "inputs": inputs,
            "outputs": [file_ref(stage / name, stage) for name in outputs], "tool": dict(TOOL),
            "plateSize": list(size), **{key: statistics[key] for key in
                                        ("protectedMax", "coverage", "coverageOver50", "meanOpacity")},
            "regions": [{**item, "kind": region["kind"], "motion": region["motion"]}
                        for item, region in zip(statistics["regions"], plan["regions"])],
            "protected": statistics["protected"],
            "envelope": None if envelope_info is None else {**envelope_info, **build.envelope},
            "settings": {"protectFeather": args.protect_feather, "lumaMinArea": args.luma_min_area,
                         "luma": "Rec.601 (0.299, 0.587, 0.114) after a 3x3 box blur",
                         "warnMeanOpacity": args.warn_mean_opacity},
        }
        forge_core.write_json(stage / "mask-qa.json", report)
    warnings = [f"{check['id']}: {check['value']}" for check in checks if check["status"] == "warn"]
    return {"output_dir": str(final.resolve()), "mask": str((final / "motion-mask.png").resolve()),
            "overlay": str((final / "mask-overlay.png").resolve()),
            "metadata": str((final / "mask-qa.json").resolve()), "status": status,
            "protected_max": statistics["protectedMax"], "coverage": statistics["coverage"],
            "mean_opacity": statistics["meanOpacity"], "_warnings": warnings}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", type=Path, required=True, help="motion-plan.json (generate2dmap.motion_plan.v1).")
    parser.add_argument("--plate", type=Path, required=True, help="The accepted static plate (opaque PNG).")
    parser.add_argument("--output-dir", type=Path, required=True, help="New folder for the outputs; must not exist.")
    parser.add_argument("--envelope-from", type=Path, metavar="CLIP",
                        help="Provider clip (video file or folder of PNG frames) whose measured motion each "
                             "region must contain.")
    add_registration_options(parser)
    parser.add_argument("--envelope-threshold", type=float, default=DEFAULT_ENVELOPE_THRESHOLD,
                        help=f"Luma range (0-255) above which a pixel moves (default {DEFAULT_ENVELOPE_THRESHOLD:g}).")
    parser.add_argument("--envelope-reach", type=float, default=DEFAULT_ENVELOPE_REACH,
                        help=f"Moving pixels within this many px of a region join it "
                             f"(default {DEFAULT_ENVELOPE_REACH:g}).")
    parser.add_argument("--envelope-min-area", type=int, default=DEFAULT_MIN_AREA,
                        help=f"Ignore moving specks smaller than this many px (default {DEFAULT_MIN_AREA}).")
    parser.add_argument("--protect-feather", type=float, default=DEFAULT_PROTECT_FEATHER,
                        help=f"Transition width outside each protected core, unless the entry sets feather "
                             f"(default {DEFAULT_PROTECT_FEATHER:g} px).")
    parser.add_argument("--luma-min-area", type=int, default=DEFAULT_MIN_AREA,
                        help=f"Drop luma-band specks smaller than this many px (default {DEFAULT_MIN_AREA}).")
    parser.add_argument("--warn-mean-opacity", type=float, default=DEFAULT_WARN_MEAN_OPACITY,
                        help=f"Warn when the whole-plate mean opacity is below this "
                             f"(default {DEFAULT_WARN_MEAN_OPACITY:g}).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        summary = run(args)
    except (ValueError, OSError, forge_av.ForgeAVError, Image.DecompressionBombError) as error:
        print(f"error: {forge_core.ascii_text(str(error) or type(error).__name__)}", file=sys.stderr)
        return 1
    except Exception as error:  # never show a traceback to the user
        print(f"error: unexpected {type(error).__name__}: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
