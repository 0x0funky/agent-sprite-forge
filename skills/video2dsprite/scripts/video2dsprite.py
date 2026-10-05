#!/usr/bin/env python3
"""Postprocess generated or supplied video into registered 2D animation assets.

Verbs (deterministic only; no creative generation, no network):
  key-plan  -> pick the chroma key for a master before generation
  triage    -> raw-clip report: size, fps, frames, audio, border touches, key drift
  extract   -> ffmpeg frames from a clip (frame_000000.png, 0-based)
  clean     -> chroma-key frames (soft matte by default) plus matte-report.json
  sample    -> even-index frame sets + fixed-envelope feet/center registration
  process   -> extract + clean + sample in one shot, plus pipeline-meta.json
  package   -> engine_export package: animation.json 3.0, PNG atlas, alpha video, residue gate
  verify    -> engine_export verify: decode every encoded file against the atlas
  doctor    -> functional ffmpeg capability probe

Every verb that writes files takes a new --output-dir (--out-dir is the old
name), builds it in a staged directory beside it and publishes it only when the
run and its QA succeed, so a failed run leaves nothing behind.

Keying defaults to the soft matte with enclosed-pocket removal, auto despill
and alpha hysteresis; ``--matte binary --despill-mode off`` reproduces the
cfed170 keyer bit for bit (references/matte.md). Frames are decoded, estimated
and matted on up to four threads (``--workers``); the output bytes do not depend
on the thread count.

Generation is a separate provider step. This processor never makes API calls.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import time
from collections import deque
from collections.abc import Sequence as SequenceABC
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from itertools import islice
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_av as fa  # noqa: E402  (this skill's vendored copies; never a sibling skill's)
import forge_core as fc  # noqa: E402
import forge_matte as fm  # noqa: E402

TOOL_VERSION = fc.FORGE_PACKAGE_VERSION  # QA envelopes record the package version (D29)
RAW_DIR, CLEAN_DIR = "frames-raw", "frames-clean"
PIPELINE_META, MATTE_REPORT = "pipeline-meta.json", "matte-report.json"
TRIAGE_REPORT, TRIAGE_SHEET = "raw-triage.json", "triage-sheet.png"
MATTE_REPORT_SCHEMA = "video2dsprite.matte_report.v1"
TRIAGE_SCHEMA = "video2dsprite.raw_triage.v1"
PROFILE_SCHEMA = "video2dsprite.character_profile.v1"

MATTES = ("soft", "dominance", "binary")
KEY_MODES = ("auto", "always", "magenta", "none")
POCKET_MODES = ("auto", "remove", "keep")
TEMPORAL_MODES = ("auto", "off", "alpha")
DECODERS = ("default", "libvpx-vp9")

RING_SPILL_MAX = 0.01          # residue gate: outer-ring key share (report v2 P0-3; Forge cfed170 measured 0.70)
FLIPS_REFERENCE = 5.7          # sprite-gen default on the Ryo clip (report v2 table 4-B); one clip, so warn only
HYSTERESIS_BAND = (0.4, 0.6)
HYSTERESIS_MAX_STEP = 0.25
MAX_ERODE = 16
BORDER_MIN_PX = 4              # triage: subject pixels on the ring that flag a frame (Dusk qa-review-raw.py: > 3)
TRIAGE_THUMBS = 12
KEY_PLAN_CANDIDATES = ("magenta", "green", "blue")
KEY_WORKERS_MAX = 4               # threads that decode, estimate and matte frames (--workers)
PARALLEL_MATTE_BUDGET_MPX = 8.0   # frame megapixels matted at once: soft_matte peaks near 112 MB per megapixel

_HEX = re.compile(r"^#?([0-9a-f]{6})$")


def _log(message: str) -> None:
    """Progress and warnings go to stderr, so stdout carries only the one-line JSON summary."""
    print(fc.ascii_text(message), file=sys.stderr, flush=True)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_counts(text: str) -> list[int]:
    counts: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        n = int(part)
        if n < 1:
            raise ValueError(f"frame count must be >= 1, got {n}")
        counts.append(n)
    if not counts:
        raise ValueError("at least one frame count required")
    return counts


def sample_indices(n_total: int, n_want: int) -> list[int]:
    if n_total <= 0:
        return []
    if n_want >= n_total:
        return list(range(n_total))
    if n_want == 1:
        return [0]
    return [int(round(i * (n_total - 1) / (n_want - 1))) for i in range(n_want)]


def _round_key(values: Sequence[float]) -> list[int]:
    """Whole-number RGB (half-up) for keyColor fields; forge_matte reports float keys."""
    return [int(math.floor(float(v) + 0.5)) for v in values]


def _hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{v:02x}" for v in _round_key(rgb))


def _short_list(values: Sequence[int], limit: int = 8) -> str:
    shown = ", ".join(str(v) for v in values[:limit])
    return shown + (f" and {len(values) - limit} more" if len(values) > limit else "")


def _load_pixels(path: Path) -> tuple[np.ndarray, dict]:
    """A writable 8-bit straight RGBA array plus forge_core provenance (sha256, mode, conversion)."""
    image, info = fc.load_rgba(path)
    return np.array(image), info


class _FrameSequence(SequenceABC):
    """Frames on disk, read on demand: clip-level rules sample a clip without holding all of it."""

    def __init__(self, paths: Sequence[Path]):
        self.paths = list(paths)

    def __len__(self) -> int:
        return len(self.paths)

    def __getitem__(self, index: int) -> np.ndarray:
        return _load_pixels(self.paths[index])[0]


class FrameInputError(RuntimeError):
    """A refused frame input (no frames found or decoded); a RuntimeError as in cfed170, reported as error: ..."""


def frame_workers(requested: int | None, size: tuple[int, int], count: int) -> int:
    """Threads for the per-frame work: ``requested`` (0 or None: min(4, CPUs)), at most one per frame and no
    more than PARALLEL_MATTE_BUDGET_MPX megapixels of frames matted at once (4K frames key one at a time)."""
    cap = requested if requested else min(KEY_WORKERS_MAX, os.cpu_count() or 1)
    megapixels = max(size[0] * size[1] / 1e6, 1e-6)
    return max(1, min(int(cap), int(PARALLEL_MATTE_BUDGET_MPX // megapixels), count))


def ordered_map(function: Callable[[Any], Any], items: Sequence[Any], workers: int):
    """Yield ``function(item)`` for every item in order, at most ``workers`` calls at once on threads.

    The per-frame work (PNG decode, key estimate, matte, pockets) is pure and numpy releases the GIL, so
    threads give the bytes a sequential run gives, about 3x faster on four cores. At most 2 x ``workers``
    results wait; an error is raised in the caller and the calls not yet started are cancelled.
    """
    if workers <= 1 or len(items) <= 1:
        for item in items:
            yield function(item)
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="video2dsprite") as pool:
        iterator = iter(items)
        pending = deque(pool.submit(function, item) for item in islice(iterator, 2 * workers))
        try:
            while pending:
                result = pending.popleft().result()
                for item in islice(iterator, 1):
                    pending.append(pool.submit(function, item))
                yield result
        finally:
            for future in pending:
                future.cancel()


def _frame_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


# --------------------------------------------------------------------------- extraction

def extract_frames(video: Path, out_dir: Path, fps: float | str = 0.0, start: float = 0.0,
                   duration: float | None = None, decoder: str = "default", *,
                   alpha: str = "auto") -> list[Path]:
    """Decode ``video`` to ``out_dir/frame_000000.png`` and onwards (0-based, six digits) with forge_av.

    ``fps`` 0 keeps every source frame with its own timing; otherwise a number
    or "num/den". ``start``/``duration`` keep the frames timed in [start,
    start + duration) seconds (input-side seek). ``alpha`` auto keeps WebM
    alpha by decoding it with libvpx; ``decoder="libvpx-vp9"`` is the cfed170
    spelling of that. Frames an earlier run left in ``out_dir`` are removed.
    """
    video, out_dir = Path(video), Path(out_dir)
    if not video.is_file():
        raise FileNotFoundError(video)
    if decoder not in DECODERS:
        raise ValueError(f"decoder must be one of {', '.join(DECODERS)}")
    if decoder == "libvpx-vp9" and alpha == "off":
        raise ValueError("--decoder libvpx-vp9 keeps alpha; it cannot be combined with --alpha off")
    _ensure_dir(out_dir)
    for old in out_dir.glob("frame_*.png"):
        old.unlink()
    return fa.extract_frames(video, out_dir, fps=fps, start=start, duration=duration, alpha=alpha)


# --------------------------------------------------------------------------- legacy keyer

def chroma_key_rgba(im: Image.Image, dist: float = 55.0, despill: float = 0.0) -> Image.Image:
    """The cfed170 video keyer (``--matte binary``): forge_matte.legacy_border_flood_key, bit for bit.

    Magenta-ish pixels 4-connected to the canvas border become transparent;
    enclosed key holes stay opaque (``--pockets remove`` clears them).
    ``despill`` (0..1) corrects the first visible ring.
    """
    return fm.legacy_border_flood_key(im, dist, despill)


# --------------------------------------------------------------------------- registration

def content_bbox(im: Image.Image, alpha_min: int = 32) -> tuple[int, int, int, int] | None:
    arr = np.array(im.convert("RGBA"))
    mask = arr[:, :, 3] > alpha_min
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def normalize_sprite(
    im: Image.Image,
    cell: int = 128,
    body_height: int = 100,
    foot_y: int = 118,
    anchor: str = "feet",
) -> Image.Image:
    bb = content_bbox(im)
    canvas = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
    if not bb:
        return canvas
    crop = im.crop(bb)
    cw, ch = crop.size
    if ch <= 0 or cw <= 0:
        return canvas
    scale = body_height / float(ch)
    nw = max(1, int(round(cw * scale)))
    nh = max(1, int(round(ch * scale)))
    if nw > cell - 4:
        scale = (cell - 4) / float(cw)
        nw = max(1, int(round(cw * scale)))
        nh = max(1, int(round(ch * scale)))
    if nh > cell - 4:
        scale = (cell - 4) / float(ch)
        nw = max(1, int(round(cw * scale)))
        nh = max(1, int(round(ch * scale)))
    resized = crop.resize((nw, nh), Image.Resampling.LANCZOS)
    if anchor == "center":
        x = (cell - nw) // 2
        y = (cell - nh) // 2
    else:
        x = (cell - nw) // 2
        y = foot_y - nh
        if y < 0:
            y = 0
        if y + nh > cell:
            y = max(0, cell - nh)
    canvas.paste(resized, (x, y))
    return canvas


def fixed_envelope(sprites: Sequence[Image.Image], cell: int, body_height: int,
                   foot_y: int, anchor: str) -> tuple[list[Image.Image], dict]:
    """One scale and translation for the entire clip; motion stays authored."""
    if not sprites or cell < 4 or body_height < 1 or not 0 <= foot_y <= cell:
        raise ValueError("invalid frames, cell size, body height or foot line")
    size = sprites[0].size
    if any(im.size != size for im in sprites):
        raise ValueError("all frames must use one source canvas")
    boxes = [bb for im in sprites if (bb := im.getchannel("A").getbbox())]
    if not boxes:
        raise ValueError("all frames are empty")
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
           max(b[2] for b in boxes), max(b[3] for b in boxes))
    bw, bh = box[2] - box[0], box[3] - box[1]
    available_h = cell - 4 if anchor == "center" else min(cell - 4, foot_y)
    if available_h < 1:
        raise ValueError("foot-y leaves no space for the sprite")
    scale = min(body_height / bh, (cell - 4) / bw, available_h / bh)
    nw, nh = max(1, round(bw * scale)), max(1, round(bh * scale))
    x, y = (cell - nw) // 2, ((cell - nh) // 2 if anchor == "center" else foot_y - nh)
    frames = []
    for im in sprites:
        out = Image.new("RGBA", (cell, cell))
        # The shared crop retains deliberate changes in pose/height/contact.
        out.paste(im.crop(box).resize((nw, nh), Image.Resampling.LANCZOS), (x, y))
        frames.append(out)
    return frames, {"mode": "fixed-envelope", "sourceSize": list(size),
                    "unionRect": list(box), "scaleXY": [nw / bw, nh / bh],
                    "outputOffset": [x, y], "outputSize": [cell, cell]}


# --------------------------------------------------------------------------- matte settings

def _normalise_key(value: Any) -> str:
    """``auto``, a declared key name or ``#rrggbb`` (lower case); an RGB triple becomes ``#rrggbb``."""
    if isinstance(value, (list, tuple)):
        if len(value) != 3 or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255
                                      for v in value):
            raise ValueError(f"an RGB key needs three whole numbers 0..255, got {value!r}")
        return "#" + "".join(f"{v:02x}" for v in value)
    text = str(value).strip().lower()
    if text == "auto" or text in fm.DECLARED_KEYS:
        return text
    match = _HEX.match(text)
    if not match:
        raise ValueError(f"unknown key {value!r}; use auto, magenta, green, blue or #rrggbb")
    return "#" + match.group(1)


def _key_argument(text: str) -> str:
    try:
        return _normalise_key(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


class MatteSettings(NamedTuple):
    """How ``clean`` and ``process`` key a clip; references/matte.md explains every field.

    A NamedTuple rather than a dataclass: tests load this script by path without
    registering it in sys.modules, which dataclasses need for string annotations.
    """

    matte: str = "soft"
    key: str = "auto"
    key_mode: str = "auto"
    despill_mode: str = "auto"
    despill_radius: int = 1
    pockets: str = "auto"
    erode: int = 0
    unmix: bool | None = None
    protect_colors: tuple[str, ...] = ()
    protect_tol: float = 0.03
    temporal: str = "auto"
    local_background: int = 0
    params: dict | None = None
    dist: float = 55.0
    despill: float = 0.0
    strict: bool = False

    @classmethod
    def legacy(cls, dist: float = 55.0, key_mode: str = "auto", despill: float = 0.0) -> MatteSettings:
        """The cfed170 keyer: binary border flood with its first-ring despill strength and nothing else."""
        return cls(matte="binary", key="magenta", key_mode=key_mode, despill_mode="off", pockets="keep",
                   temporal="off", dist=dist, despill=despill)

    @property
    def remove_pockets(self) -> bool:
        return self.pockets == "remove" or (self.pockets == "auto" and self.matte != "binary")

    @property
    def use_unmix(self) -> bool:
        return self.matte == "soft" if self.unmix is None else self.unmix

    @property
    def use_temporal(self) -> bool:
        """auto: the alpha hysteresis for soft and dominance alpha; binary alpha has no in-band values."""
        return self.temporal == "alpha" or (self.temporal == "auto" and self.matte != "binary")

    def validate(self) -> None:
        for name, value, allowed in (("matte", self.matte, MATTES), ("key mode", self.key_mode, KEY_MODES),
                                     ("despill mode", self.despill_mode, fm.DESPILL_MODES),
                                     ("pockets", self.pockets, POCKET_MODES),
                                     ("temporal stability", self.temporal, TEMPORAL_MODES)):
            if value not in allowed:
                raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
        if _normalise_key(self.key) != self.key:
            raise ValueError(f"key {self.key!r} is not normalised; use auto, a key name or #rrggbb")
        if self.key_mode == "magenta" and self.key not in ("auto", "magenta"):
            raise ValueError("--key-mode magenta is the old spelling of 'always key magenta'; "
                             "use --key-mode always with another --key")
        if self.matte == "binary" and self.key not in ("auto", "magenta"):
            raise ValueError("--matte binary is the cfed170 magenta keyer; use --matte soft or "
                             "dominance for green, blue or #rrggbb keys")
        if self.unmix and self.matte != "soft":
            raise ValueError("--unmix belongs to the soft matte; the dominance alpha is a ramp, not a "
                             "coverage estimate, and binary alpha has nothing to un-mix")
        if self.local_background and self.matte != "soft":
            raise ValueError("--local-background belongs to the soft matte")
        if self.params is not None:
            if self.matte != "soft":
                raise ValueError("matte profile params are soft-matte KeyParams; the profile's mode is "
                                 f"{self.matte}")
            unknown = sorted(set(self.params) - set(fm.KeyParams().to_dict()))
            if unknown:
                raise ValueError(f"unknown KeyParams field(s) in the matte profile: {', '.join(unknown)}")
            fm.KeyParams(**{**fm.KeyParams().to_dict(), **self.params})
        if not (math.isfinite(self.despill) and 0 <= self.despill <= 1):
            raise ValueError("despill must be between 0 and 1")
        if not (math.isfinite(self.dist) and self.dist >= 0):
            raise ValueError("dist must be a finite distance >= 0")
        for name, value, limit in (("despill radius", self.despill_radius, 64), ("erode", self.erode, MAX_ERODE),
                                   ("local background", self.local_background, 64)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
                raise ValueError(f"{name} must be a whole number from 0 to {limit} px, got {value!r}")
        if not (math.isfinite(self.protect_tol) and self.protect_tol > 0):
            raise ValueError("protect tolerance must be a positive OKLab distance")
        if self.protect_colors:
            fm.protect_mask(np.zeros((1, 1, 3), np.uint8), list(self.protect_colors), self.protect_tol)


_PROFILE_FIELDS = (("matte", "mode", "--matte"), ("key", "key", "--key"), ("erode", "erode", "--erode"),
                   ("unmix", "unmix", "--unmix"), ("despill_mode", "despill", "--despill-mode"))


def load_matte_profile(path: Path) -> dict:
    """Read the ``matte`` block a character profile (video2dsprite.character_profile.v1) pins."""
    path = Path(path)
    try:
        document = fc.read_json(path, strict=True)  # UTF-8 with or without a BOM (D28)
    except ValueError as exc:
        raise ValueError(f"matte profile {path.name} is not valid JSON: {exc}") from None
    if not isinstance(document, dict) or document.get("schema") != PROFILE_SCHEMA:
        raise ValueError(f"matte profile {path.name} is not a {PROFILE_SCHEMA} document")
    matte = document.get("matte")
    if not isinstance(matte, dict) or any(name not in matte for _, name, _ in _PROFILE_FIELDS):
        raise ValueError(f"matte profile {path.name} needs matte.mode, key, erode, unmix and despill")
    erode = matte["erode"]
    if isinstance(erode, bool) or not isinstance(erode, (int, float)) or erode != int(erode) or erode < 0:
        raise ValueError(f"matte profile erode must be a whole number of px, got {erode!r}")
    if not isinstance(matte["unmix"], bool):
        raise ValueError("matte profile unmix must be true or false")
    params = matte.get("params")
    if params is not None and not isinstance(params, dict):
        raise ValueError("matte profile params must be a KeyParams object")
    pinned = {"matte": matte["mode"], "key": _normalise_key(matte["key"]), "erode": int(erode),
              "unmix": matte["unmix"], "despill_mode": matte["despill"]}
    return {"path": path, "sha256": fc.sha256_file(path), "id": document.get("id"), "pinned": pinned,
            "params": params}


def matte_settings(args: argparse.Namespace, *,
                   warn: Callable[[str], None] = _log) -> tuple[MatteSettings, dict | None]:
    """Resolve the keying options: explicit flags, then the --matte-profile pins, then the defaults.

    A flag that contradicts the profile wins but is warned about and recorded,
    because it makes this clip's matte differ from the character's other clips
    (game-opus55 pixelate.py L472-477).
    """
    explicit = {"matte": args.matte, "key": args.key, "erode": args.erode, "unmix": args.unmix,
                "despill_mode": args.despill_mode}
    values = {name: value for name, value in explicit.items() if value is not None}
    profile = None
    params = None
    if args.matte_profile:
        profile = load_matte_profile(args.matte_profile)
        deviations = []
        for attribute, _, flag in _PROFILE_FIELDS:
            pinned = profile["pinned"][attribute]
            if explicit[attribute] is None:
                values[attribute] = pinned
            elif explicit[attribute] != pinned:
                deviations.append({"option": flag, "profile": pinned, "used": explicit[attribute]})
                warn(f"warning: {flag} {explicit[attribute]} deviates from the matte profile ({pinned}); this "
                     "clip's matte will differ from the character's other clips")
        profile["deviations"] = deviations
        params = profile["params"]
    matte = values.get("matte", "soft")
    if matte != "binary":
        for flag, value in (("--dist", args.dist), ("--despill", args.despill)):
            if value is not None:
                raise ValueError(f"{flag} belongs to the cfed170 keyer (--matte binary); the {matte} matte "
                                 "cleans edges itself (see --despill-mode)")
    settings = MatteSettings(
        **values, key_mode=args.key_mode, despill_radius=args.despill_radius, pockets=args.pockets,
        protect_colors=tuple(_normalise_key(c) for c in args.protect_color or ()), protect_tol=args.protect_tol,
        temporal=args.temporal_stability, local_background=args.local_background, params=params,
        dist=55.0 if args.dist is None else args.dist, despill=0.0 if args.despill is None else args.despill,
        strict=args.strict)
    settings.validate()
    return settings, profile


# --------------------------------------------------------------------------- clip planning

def _local_key_dominance(rgb: np.ndarray, key_rgb: Sequence[float]) -> np.ndarray:
    """How far colours lean to the key's channels: min(key channels) - max(other channels)."""
    key = np.asarray(key_rgb, np.float32)
    high, low = np.flatnonzero(key > 127.5), np.flatnonzero(key <= 127.5)
    pixels = np.asarray(rgb, np.float32)
    return pixels[..., high].min(axis=-1) - pixels[..., low].max(axis=-1)


def _detect_key(frames: _FrameSequence, picks: Sequence[int]) -> tuple[str, dict]:
    """The declared key (magenta, green or blue) whose border-ring estimate fits the sampled frames best."""
    scores = {name: 0.0 for name in fm.DECLARED_KEYS}
    used = []
    for index in picks:
        pixels = frames[index]
        for name in fm.DECLARED_KEYS:
            _, info = fm.estimate_key(pixels, name)
            if info["use_native_alpha"]:
                break
            if info["valid"]:
                scores[name] += info["ring_share"]
        else:
            used.append(int(index))
    best = max(scores, key=scores.get)
    detection = {"frames": used, "ring_share": {name: round(score, 6) for name, score in scores.items()}}
    if scores[best] <= 0:
        return "", detection
    return best, detection


class _ClipPlan(NamedTuple):
    """Clip-wide decisions taken before any frame is keyed (_plan_clip)."""

    paths: list
    sha256: list
    declared: str
    keys: list
    passthrough: list
    estimates: list
    detection: dict | None
    decision: dict | None
    despill_applied: str
    params: fm.KeyParams | None
    warnings: list
    at_risk: list
    temporal_active: bool
    temporal_reason: str | None


def _estimate_frame(path: Path, declared: str, settings: MatteSettings) -> tuple:
    """Per-frame plan facts (thread-safe): sha256, shape, the border-ring key estimate and passthrough."""
    pixels, info = _load_pixels(path)
    key_rgb, estimate = fm.estimate_key(pixels, declared)
    if settings.key_mode == "none":
        skip = True
    elif settings.key_mode != "auto":
        skip = False
    elif settings.matte == "binary":
        skip = bool((pixels[..., 3] < 255).any())  # the cfed170 rule: any transparency keeps the frame
    else:
        skip = bool(estimate["use_native_alpha"])
    return info["sha256"], pixels.shape, key_rgb, estimate, skip


def _plan_clip(paths: list[Path], settings: MatteSettings, reference: np.ndarray | None,
               despill_sample: tuple[list, list] | None = None, workers: int = 1) -> _ClipPlan:
    """Decide everything clip-wide before keying: the key per frame, native alpha, despill and params.

    ``despill_sample`` is ``(frames, source_indices)`` from the whole source clip; the
    auto interior despill rule then judges the clip rather than a trimmed window.
    Frames are read and estimated on ``workers`` threads (same results).
    """
    frames = _FrameSequence(paths)
    warnings: list[str] = []
    declared, detection = settings.key, None
    if declared == "auto":
        if settings.matte == "binary" or settings.key_mode in ("none", "magenta"):
            declared = "magenta"
        else:
            declared, detection = _detect_key(frames, sample_indices(len(frames), 5))
            if not declared:
                if detection["frames"]:
                    raise ValueError("no magenta, green or blue backdrop on the border ring of the sampled frames; "
                                     "pass --key #rrggbb (triage reports the backdrop) or --key-mode none")
                declared = "magenta"  # every sampled frame has native alpha: nothing to key
    explicit = declared.startswith("#")
    explicit_rgb = np.array([int(declared[i:i + 2], 16) for i in (1, 3, 5)], np.float32) if explicit else None
    keys, passthrough, estimates, digests, shapes = [], [], [], [], set()
    estimate_one = functools.partial(_estimate_frame, declared=declared, settings=settings)
    with contextlib.closing(ordered_map(estimate_one, paths, workers)) as stream:  # threads end before we go on
        for digest, shape, key_rgb, estimate, skip in stream:
            digests.append(digest)
            shapes.add(shape)
            keys.append(explicit_rgb.copy() if explicit else key_rgb)
            passthrough.append(skip)
            estimates.append(None if explicit else estimate)
    keyable = [i for i, skip in enumerate(passthrough) if not skip]
    if not explicit:
        valid = [keys[i] for i in keyable if estimates[i]["valid"]]
        invalid = [i for i in keyable if not estimates[i]["valid"]]
        if keyable and not valid and settings.matte != "binary":
            raise ValueError(f"no frame shows a {declared} backdrop on its border ring; check --key "
                             "(triage reports the backdrop) or pass --key #rrggbb")
        if valid and invalid:
            median = np.median(np.stack(valid), axis=0).astype(np.float32)
            for i in invalid:
                keys[i] = median
            warnings.append(f"the {declared} key estimate failed on {len(invalid)} frame(s) ({_short_list(invalid)}); "
                            f"they use the clip median key {_hex(median)}")
    clip_key = (np.median(np.stack([keys[i] for i in keyable]), axis=0) if keyable
                else np.asarray(keys[0], np.float32))
    pinned = (settings.params or {}).get("interior_despill")
    decision = None
    if keyable and (settings.matte == "soft" or settings.despill_mode == "auto"):
        if despill_sample is not None:
            frames_sampled, source_indices = despill_sample
            decision = fm.auto_interior_despill(reference, frames_sampled, key=declared, sample=len(frames_sampled))
            decision["clip_sample_frames"] = [source_indices[p] for p in decision["clip_sample_frames"]]
            decision["sampled_from"] = "source clip"
        else:
            decision = fm.auto_interior_despill(reference, _FrameSequence([paths[i] for i in keyable]), key=declared)
            decision["clip_sample_frames"] = [keyable[p] for p in decision["clip_sample_frames"]]
            decision["sampled_from"] = "keyed frames"
    applied = settings.despill_mode
    if applied == "auto":
        if settings.matte == "soft" and pinned is not None:
            applied = "all" if pinned else "edge"
            if decision is not None:
                decision["pinned_by_profile"] = bool(pinned)
        else:
            applied = "all" if decision is None or decision["interior_despill"] else "edge"
    params = None
    if settings.matte == "soft" and keyable:
        values = {**fm.KeyParams().to_dict(), **(settings.params or {}), "interior_despill": applied == "all"}
        if applied == "off":
            values["r_c"] = 0  # no colour-clean band either
        if not settings.use_unmix:
            values["unmix_from"] = 1.0  # candidate colour only, no luma un-mix
        params = fm.KeyParams(**values)
    owns_material = decision is not None and max(decision.get("clip_median_share", 0.0),
                                                 decision.get("reference_share", 0.0)) > fm.KEY_MATERIAL_SHARE_MAX
    if settings.matte == "soft" and owns_material and not settings.protect_colors:
        warnings.append("the subject owns key-coloured material, so interior despill stays off; the video soft "
                        "matte also keys light colours near the key (for example #b43cc8 against magenta): review "
                        "the frames, pass --protect-color for design colours, or regenerate on the key that "
                        "key-plan recommends")
    at_risk = []
    master = None
    if settings.matte == "soft" and keyable and reference is not None:
        master = reference
        if not (reference[..., 3] < 255).any():  # an input still on the key: key it with the still parameters
            keyed, still = fm.key_still(reference, quality="soft", key=declared)
            master = None if still["quality"] == "native_alpha" else np.asarray(keyed)
    if master is not None and (master[..., 3] < 255).any():
        _, risk = fm.protect_design_colours(master, clip_key, params=params)
        rows = sorted(risk["colors"], key=lambda row: -row["px"])
        if settings.protect_colors and rows:  # colours the run already protects are not at risk
            colours = np.array([[[int(row["from"][i:i + 2], 16) for i in (1, 3, 5)] for row in rows]], np.uint8)
            protected = fm.protect_mask(colours, list(settings.protect_colors), settings.protect_tol)[0]
            rows = [row for row, kept in zip(rows, protected) if not kept]
        at_risk = rows[:8]
        if at_risk:
            warnings.append("design colours of the reference within the video matte's reach of the key: "
                            + ", ".join(f"{row['from']} ({row['px']} px)" for row in at_risk[:4])
                            + "; protect them with --protect-color or recolour them (suggestions in the matte report)")
    temporal_active, reason = settings.use_temporal, None
    if temporal_active and len(paths) < 2:
        temporal_active, reason = False, "a single frame has no neighbours"
    elif temporal_active and any(passthrough):
        temporal_active, reason = False, "native-alpha or unkeyed frames keep their own alpha"
    elif temporal_active and len(shapes) > 1:
        temporal_active, reason = False, "frames differ in size"
    return _ClipPlan(paths, digests, declared, keys, passthrough, estimates, detection, decision, applied, params,
                     warnings, at_risk, temporal_active, reason)


# --------------------------------------------------------------------------- keying

class _LocalAlphaHysteresis:
    """Streaming twin of forge_matte.temporal_alpha_hysteresis for 8-bit alpha (loop off).

    The library filters a whole clip held as float planes (about 0.5 GB for 145
    frames at 960^2); this keeps one frame of state and returns the same bytes
    (tests/test_video2dsprite_matte.py compares them). Each pixel has a coverage
    state that flips only when alpha leaves the (0.4, 0.6) band on the other
    side; while it holds, alpha moves at most ``max_step`` per frame; a decisive
    change passes at once and alpha 0 always passes.

    It works on the 8-bit grid in integers: the band edges are the library's own
    float32 comparisons tabulated per alpha value, and a held step is a whole
    number of levels (63 for 0.25), so every result equals the library's rounded
    float result (an exhaustive test over every previous value, state and alpha
    proves it) at about half the cost.
    """

    def __init__(self, band: tuple[float, float] = HYSTERESIS_BAND, max_step: float = HYSTERESIS_MAX_STEP):
        lo, hi = float(band[0]), float(band[1])
        if not 0.0 <= lo < hi <= 1.0 or not max_step > 0:
            raise ValueError("band needs 0 <= lo < hi <= 1 and max_step > 0.")
        self.lo, self.hi = lo, hi
        self.max_step = math.floor(max_step * 255 + 1e-6) / 255  # a held step stays on the 8-bit grid
        self._step = int(math.floor(max_step * 255 + 1e-6))
        levels = np.arange(256, dtype=np.float32) / np.float32(255.0)  # the library's float32 planes
        self._on, self._off = levels >= hi, levels <= lo
        self._previous: np.ndarray | None = None
        self._state: np.ndarray | None = None

    def push(self, alpha: np.ndarray) -> np.ndarray:
        """Filter the next uint8 alpha plane; the first frame passes unchanged."""
        source = np.asarray(alpha, np.uint8)
        if self._previous is None:
            self._previous, self._state = source.astype(np.int16), source >= 128  # plane >= 0.5
            return source.copy()
        decided = self._on[source] | (~self._off[source] & self._state)
        current = source.astype(np.int16)
        held = np.clip(current, self._previous - self._step, self._previous + self._step)
        result = np.where(decided == self._state, held, current)
        result[source == 0] = 0  # alpha 0 always passes: never invent coverage without colour
        self._previous, self._state = result, decided
        return result.astype(np.uint8)


def _absdiff(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """|first - second| of uint8 arrays without widening (max - min never wraps)."""
    return np.maximum(first, second) - np.minimum(first, second)


def _local_still_mask(previous_rgb: np.ndarray, current_rgb: np.ndarray, tolerance: int = 20) -> np.ndarray:
    """Pixels whose raw colour changed by at most ``tolerance`` (max channel): forge_matte.flip_count's motion rule."""
    return _absdiff(previous_rgb, current_rgb).max(-1) <= tolerance


def _local_pair_flips(previous_alpha: np.ndarray, current_alpha: np.ndarray, still: np.ndarray) -> int:
    """forge_matte.flip_count for one pair of 8-bit alpha planes: |a - b| / 255 > 0.25 is |a - b| >= 64.

    The library rebuilds the motion mask and float planes on every call; a clip
    counts flips twice per pair (before and after hysteresis), so the mask is
    built once (_local_still_mask). Equality with the library is tested.
    """
    return int(((_absdiff(previous_alpha, current_alpha) >= 64) & still).sum())


def _local_pair_flip_counts(previous_rgb: np.ndarray, current_rgb: np.ndarray, previous_before: np.ndarray,
                            before: np.ndarray, previous_after: np.ndarray, after: np.ndarray,
                            tolerance: int = 20) -> tuple[int, int]:
    """Flips of one frame pair before and after the hysteresis, as _local_pair_flips over _local_still_mask.

    A flip needs an alpha change of 64 or more AND a raw colour change of at most ``tolerance``; the colour
    test runs only at the pixels whose alpha changed (a thin rim), not over the whole frame. Equal counts.
    """
    changed_before = _absdiff(previous_before, before) >= 64
    changed_after = _absdiff(previous_after, after) >= 64
    rows, columns = np.nonzero(changed_before | changed_after)
    if rows.size == 0:
        return 0, 0
    still = _absdiff(previous_rgb[rows, columns], current_rgb[rows, columns]).max(-1) <= tolerance
    return int((changed_before[rows, columns] & still).sum()), int((changed_after[rows, columns] & still).sum())


def _visible_box(rgba: np.ndarray) -> tuple[int, int, int, int] | None:
    """Box of the alpha > 0 pixels: matte_qa and pocket removal read and change only these and their
    transparent or off-canvas neighbours, so running them on this crop is exact (tests prove it)."""
    return fc.subject_bbox(rgba[..., 3], 0)


def _local_matte_qa(rgba: np.ndarray, key_rgb: np.ndarray) -> dict:
    """forge_matte.matte_qa on the alpha > 0 crop: the same numbers, about twice as fast on video frames."""
    box = _visible_box(rgba)
    crop = np.zeros((1, 1, 4), np.uint8) if box is None else rgba[box[1]:box[3], box[0]:box[2]]
    return fm.matte_qa(crop, key_rgb)


def _local_remove_pockets(rgba: np.ndarray, key_rgb: np.ndarray) -> tuple[np.ndarray, int]:
    """forge_matte.remove_enclosed_pockets on the alpha > 0 crop, pasted back: the same pixels and count
    (a pocket is made of visible pixels, all inside the box)."""
    box = _visible_box(rgba)
    if box is None:
        return np.array(rgba, copy=True), 0
    x0, y0, x1, y1 = box
    cleaned, count = fm.remove_enclosed_pockets(rgba[y0:y1, x0:x1], key_rgb)
    out = np.array(rgba, copy=True)
    out[y0:y1, x0:x1] = cleaned
    return out, count


def _local_erode_alpha(rgba: np.ndarray, steps: int) -> np.ndarray:
    """Shrink the matte ``steps`` px: alpha becomes the minimum of its 4-neighbourhood ``steps`` times
    (edge pixels repeat outward), as game-opus55 pixelate.py _erode does against compression halos."""
    alpha = rgba[..., 3]
    for _ in range(steps):
        padded = np.pad(alpha, 1, mode="edge")
        alpha = np.minimum.reduce([padded[1:-1, 1:-1], padded[:-2, 1:-1], padded[2:, 1:-1],
                                   padded[1:-1, :-2], padded[1:-1, 2:]])
    out = rgba.copy()
    out[..., 3] = alpha
    out[alpha == 0, :3] = 0
    return out


def _key_frame(pixels: np.ndarray, key_rgb: np.ndarray, settings: MatteSettings, plan: _ClipPlan,
               guard: np.ndarray | None) -> tuple[np.ndarray, dict]:
    """Matte, enclosed-pocket removal and erosion for one RGBA frame (``guard``: protected pixels)."""
    stats = {"pockets_removed": 0, "protected_px": 0 if guard is None else int(guard.sum())}
    if settings.matte == "binary":
        keyed = np.array(fm.legacy_border_flood_key(pixels, settings.dist, settings.despill))
        if guard is not None:
            keyed[guard] = pixels[guard]
    elif settings.matte == "dominance":
        keyed = fm.dominance_matte(pixels, key_rgb, protect=guard)
    else:
        keyed = fm.soft_matte(pixels, plan.params, key_rgb, local_background=settings.local_background,
                              protect=guard)
    if settings.remove_pockets:
        cleaned, stats["pockets_removed"] = _local_remove_pockets(keyed, key_rgb)
        if guard is not None:
            cleaned[guard] = keyed[guard]
        keyed = cleaned
    if settings.erode:
        keyed = _local_erode_alpha(keyed, settings.erode)
    return keyed, stats


_QA_METHOD = ("forge_matte.matte_qa on every frame against that frame's key: opaque key px (alpha >= 128 within "
              "RGB 48 of the key), enclosed key-coloured regions of >= 16 px and key-hued px are counted after the "
              "matte, pockets, erosion and hysteresis but before any despill pass (which could hide residue as an "
              "opaque grey blob); the outer visible ring's share with key dominance > 20 and the semi-transparent "
              "share are measured on the published frame. Flips are pixels whose alpha changes by more than 0.25 "
              "while the raw colour changes by at most 20 (forge_matte.flip_count). Counts are sums over frames, "
              "fractions the worst frame.")
_FRAME_FIELDS = ("opaque_key_px", "outer_ring_spill_fraction", "semitransparent_fraction", "enclosed_key_pockets",
                 "key_hued_px", "visible_px")


def _key_estimate_summary(plan: _ClipPlan) -> dict | None:
    """The clip-wide key estimate: median of the valid per-frame border-ring estimates."""
    keyable = [i for i, skip in enumerate(plan.passthrough) if not skip]
    if plan.declared.startswith("#") or not keyable:
        return None
    infos = [plan.estimates[i] for i in keyable]
    valid = [np.asarray(info["key"], np.float32) for info in infos if info["valid"]]
    declared_rgb = np.asarray(infos[0]["declared_rgb"], np.float32)
    median = np.median(np.stack(valid), axis=0) if valid else declared_rgb
    summary = {
        "declared": plan.declared, "declared_rgb": infos[0]["declared_rgb"],
        "key": [round(float(v), 2) for v in median],
        "source": "border_median" if valid else "declared",
        "valid": len(valid) == len(infos),
        "ring_share": round(min(info["ring_share"] for info in infos), 6),
        "coverage": round(min(info["coverage"] for info in infos), 6),
        "distance_from_declared": round(float(np.linalg.norm(median - declared_rgb)), 2),
        "spread": round(max((float(np.linalg.norm(key - median)) for key in valid), default=0.0), 2),
        "frames_estimated": len(infos),
        "invalid_frames": [i for i in keyable if not plan.estimates[i]["valid"]],
    }
    if plan.detection is not None:
        summary["detected"] = plan.detection
    return summary


def _matte_report(settings: MatteSettings, plan: _ClipPlan, rows: list[dict], outputs: list[Path],
                  output_sha256: list[str], flips: tuple[float, float] | None, profile: dict | None,
                  base: Path, tool: str) -> dict:
    """matte_report.v1 for a keyed clip, as a QA envelope (method, notProven, checks, hashed files)."""
    keyable = [i for i, skip in enumerate(plan.passthrough) if not skip]
    clip_key = np.median(np.stack([plan.keys[i] for i in keyable]), axis=0) if keyable else plan.keys[0]
    totals = {name: int(sum(row[name] for row in rows))
              for name in ("opaque_key_px", "enclosed_key_pockets", "pockets_removed", "key_hued_px")}
    worst = {name: float(max(row[name] for row in rows))
             for name in ("outer_ring_spill_fraction", "semitransparent_fraction")}
    checks = [
        {"id": "opaque_key_px", "status": "pass" if totals["opaque_key_px"] == 0 else "fail",
         "value": totals["opaque_key_px"], "threshold": 0},
        {"id": "enclosed_key_pockets", "status": "pass" if totals["enclosed_key_pockets"] == 0 else "fail",
         "value": totals["enclosed_key_pockets"], "threshold": 0},
        {"id": "outer_ring_spill_fraction",
         "status": "pass" if worst["outer_ring_spill_fraction"] <= RING_SPILL_MAX else "fail",
         "value": round(worst["outer_ring_spill_fraction"], 6), "threshold": RING_SPILL_MAX},
    ]
    temporal = {"mode": "alpha" if settings.use_temporal else "off", "requested": settings.temporal,
                "band": list(HYSTERESIS_BAND), "max_step": HYSTERESIS_MAX_STEP, "loop": False,
                "active": plan.temporal_active}
    if plan.temporal_reason:
        temporal["skipped"] = plan.temporal_reason
    if flips is not None:
        temporal.update(flips_before=round(flips[0], 4), flips_after=round(flips[1], 4))
        checks.append({"id": "flips_per_frame_pair", "status": "pass" if flips[1] <= FLIPS_REFERENCE else "warn",
                       "value": round(flips[1], 4), "threshold": FLIPS_REFERENCE})
    statuses = {check["status"] for check in checks}
    status = ("fail" if "fail" in statuses else
              "warn" if "warn" in statuses or plan.warnings else "needs-visual-review")
    not_proven = [
        "Matte QA counts key residue only; edge quality, colour fidelity and identity need a visual review over "
        "light and dark backgrounds.",
        "The soft matte and these thresholds were tuned and verified on one clip (report v2 section 7); other keys, "
        "outline-free art and codecs are covered by synthetic tests only.",
    ]
    if settings.matte == "soft":
        not_proven.append("Light colours within the video matte's reach of the key (for example #b43cc8 against "
                          "magenta) are keyed out unless protected; clean residue numbers do not show they were kept.")
    if plan.temporal_active:
        not_proven.append("Alpha hysteresis lags in-band alpha changes by up to one frame.")
    report: dict[str, Any] = {
        "schema": MATTE_REPORT_SCHEMA,
        "mode": "none" if settings.key_mode == "none" else settings.matte,
        "key": _round_key(clip_key),
        "key_request": settings.key,
        "key_declared": plan.declared,
        "key_mode": settings.key_mode,
        "despill_mode": settings.despill_mode,
        "despill_applied": plan.despill_applied,
        "opaque_key_px": totals["opaque_key_px"],
        "outer_ring_spill_fraction": worst["outer_ring_spill_fraction"],
        "semitransparent_fraction": worst["semitransparent_fraction"],
        "enclosed_key_pockets": totals["enclosed_key_pockets"],
        "pockets": "remove" if settings.remove_pockets else "keep",
        "pockets_removed": totals["pockets_removed"],
        "key_hued_px": totals["key_hued_px"],
        "key_estimate": _key_estimate_summary(plan),
        "local_background": settings.local_background,
        "erode": settings.erode,
        "unmix": settings.use_unmix,
        "temporal": temporal,
        "frames_total": len(rows),
        "passthrough_frames": [row["index"] for row in rows if row["passthrough"]],
        "warnings": list(plan.warnings),
    }
    if flips is not None:
        report["flips_per_frame_pair"] = round(flips[1], 4)
    if plan.decision is not None:
        report["interior_despill"] = plan.decision
    if plan.params is not None:
        report["params"] = plan.params.to_dict()
    if settings.matte == "binary":
        report["legacy"] = {"dist": settings.dist, "despill": settings.despill}
    if settings.matte != "soft" and plan.despill_applied != "off":
        report["despill_radius"] = settings.despill_radius
    if settings.protect_colors:
        report["protect"] = {"colors": list(settings.protect_colors), "tol": settings.protect_tol,
                             "px": int(sum(row.get("protected_px", 0) for row in rows))}
    if plan.at_risk:
        report["design_colours_at_risk"] = plan.at_risk
    if profile is not None:
        report["profile"] = {"file": fc.file_ref(profile["path"], base, sha256=profile["sha256"]), "id": profile["id"],
                             "pinned": profile["pinned"], "deviations": profile["deviations"]}
        if profile["params"] is not None:
            report["profile"]["params"] = profile["params"]
    report["frames"] = rows
    report.update({
        "status": status,
        "method": _QA_METHOD,
        "notProven": not_proven,
        "checks": checks,
        "inputs": [fc.file_ref(path, base, sha256=digest) for path, digest in zip(plan.paths, plan.sha256)],
        "outputs": [fc.file_ref(path, base, sha256=digest) for path, digest in zip(outputs, output_sha256)],
        "tool": {"name": tool, "version": TOOL_VERSION},
    })
    return report


def sample_source_frames(video: Path, total: int, sample: int = 16) -> tuple[list, list] | None:
    """Up to ``sample`` evenly spaced frames of the whole clip (the spacing auto_interior_despill uses).

    ``process`` passes them to key_frames when it trims the clip, so the
    interior despill decision belongs to the clip, not to the trimmed window.
    """
    if total < 1:
        return None
    count, last = min(sample, total), total - 1
    picks = sorted({int(math.floor(k * last / max(1, count - 1) + 0.5)) for k in range(count)})
    wanted, frames, indices = set(picks), [], []
    stream = fa.iter_rgba(video)
    try:
        for index, frame in enumerate(stream):
            if index in wanted:
                frames.append(frame)
                indices.append(index)
            if index >= picks[-1]:
                break
    finally:
        stream.close()
    return (frames, indices) if frames else None


def _matte_one(index: int, paths: Sequence[Path], settings: MatteSettings, plan: _ClipPlan) -> tuple:
    """Decode and key one frame (thread-safe): pixels, keyed frame, stats and the protect guard."""
    pixels, _ = _load_pixels(paths[index])
    if plan.passthrough[index]:
        return pixels, pixels.copy(), {"pockets_removed": 0}, None
    guard = None
    if settings.protect_colors:
        guard = fm.protect_mask(pixels[..., :3], list(settings.protect_colors), settings.protect_tol)
    keyed, stats = _key_frame(pixels, plan.keys[index], settings, plan, guard)
    return pixels, keyed, stats, guard


def key_frames(raw_dir: Path, clean_dir: Path, settings: MatteSettings = MatteSettings(), *,
               reference: np.ndarray | None = None, profile: dict | None = None,
               despill_sample: tuple[list, list] | None = None, tool: str = "video2dsprite.py clean",
               log: Callable[[str], None] | None = None, workers: int | None = None) -> dict:
    """Key ``raw_dir/frame_*.png`` into ``clean_dir/clean_0000.png`` onwards and write matte-report.json.

    The clip is planned first (key per frame, native alpha, the auto interior
    despill rule), then keyed one frame at a time: matte, enclosed pockets,
    erosion, alpha hysteresis, residue QA, then the despill pass of the
    dominance and binary mattes, holding one frame plus the previous frame's
    alpha. ``reference`` (an RGBA master) joins the auto despill rule and names
    design colours the video matte would key out; ``despill_sample`` (frames
    and indices of the whole source clip, see sample_source_frames) lets that
    rule judge the clip, not a trimmed window. Returns the matte report
    (video2dsprite.matte_report.v1, a QA envelope); a ``clean_dir`` that
    already holds keyed frames is refused. ``workers`` threads decode,
    estimate and matte frames (None or 0: min(4, CPUs), see frame_workers);
    the hysteresis, QA and writing stay in order, so the bytes never depend
    on it.
    """
    settings.validate()
    raw_dir, clean_dir = Path(raw_dir), Path(clean_dir)
    paths = sorted(raw_dir.glob("frame_*.png"))
    if not paths:
        raise FrameInputError(f"no raw frames in {raw_dir}")
    _ensure_dir(clean_dir)
    if any(clean_dir.glob("clean_*.png")) or (clean_dir / MATTE_REPORT).exists():
        raise FileExistsError(f"{clean_dir} already holds keyed frames; key into a new directory")
    workers = frame_workers(workers, _frame_size(paths[0]), len(paths))
    plan = _plan_clip(paths, settings, reference, despill_sample, workers)
    if log:
        for warning in plan.warnings:
            log(f"warning: {warning}")
    hysteresis = _LocalAlphaHysteresis() if plan.temporal_active else None
    rows, outputs, digests = [], [], []
    flips_before = flips_after = 0.0
    pairs = 0
    previous = None
    post_despill = settings.matte != "soft" and plan.despill_applied != "off"
    matte_one = functools.partial(_matte_one, paths=paths, settings=settings, plan=plan)
    # closing() stops the threads before an error leaves key_frames (and staged_output removes the stage)
    with contextlib.closing(ordered_map(matte_one, range(len(paths)), workers)) as stream:
        for index, (pixels, keyed, stats, guard) in enumerate(stream):
            key_rgb = plan.keys[index]
            before = keyed[..., 3].copy()
            if hysteresis is not None:
                keyed[..., 3] = hysteresis.push(before)
                keyed[keyed[..., 3] == 0, :3] = 0
            if previous is not None and previous[0].shape == pixels[..., :3].shape:
                pair = _local_pair_flip_counts(previous[0], pixels[..., :3], previous[1], before, previous[2],
                                               keyed[..., 3])
                flips_before += pair[0]
                flips_after += pair[1]
                pairs += 1
            previous = (pixels[..., :3], before, keyed[..., 3].copy())
            # Residue is counted before the despill pass: neutralising a key-coloured pocket
            # would hide it as an opaque grey or black blob instead of removing it.
            qa = _local_matte_qa(keyed, key_rgb)
            if post_despill and not plan.passthrough[index]:
                keyed, despilled = fm.despill(keyed, plan.despill_applied, settings.despill_radius, protect=guard,
                                              key=key_rgb)
                stats["despill_px"] = despilled["changed_px"]
                final = _local_matte_qa(keyed, key_rgb)
                qa = {**qa, **{name: final[name] for name in ("outer_ring_spill_fraction", "semitransparent_fraction",
                                                              "visible_px")}}
            target = clean_dir / f"clean_{index:04d}.png"
            fc.save_png(keyed, target)
            outputs.append(target)
            digests.append(fc.sha256_file(target))
            rows.append({"index": index, "key": _round_key(key_rgb), "passthrough": plan.passthrough[index],
                         **{name: qa[name] for name in _FRAME_FIELDS}, **stats})
            if log and ((index + 1) % 25 == 0 or index + 1 == len(paths)):
                log(f"  keyed {index + 1}/{len(paths)}")
    flips = (flips_before / pairs, flips_after / pairs) if pairs else None
    report = _matte_report(settings, plan, rows, outputs, digests, flips, profile, clean_dir, tool)
    fc.write_json(clean_dir / MATTE_REPORT, report)
    return report


def clean_frames(
    raw_dir: Path,
    clean_dir: Path,
    dist: float = 55.0,
    key_mode: str = "auto",
    despill: float = 0.0,
) -> list[Path]:
    """cfed170 API: key ``raw_dir`` into ``clean_dir`` in place with the legacy binary keyer.

    The pixels equal the cfed170 output (frames with native alpha pass through,
    RGB zeroed under alpha 0). Keyed frames an earlier run left in
    ``clean_dir`` are removed first and matte-report.json is written beside
    the frames. The ``clean`` and ``process`` verbs use key_frames with the
    soft-matte defaults instead.
    """
    clean_dir = _ensure_dir(Path(clean_dir))
    for old in clean_dir.glob("clean_*.png"):
        old.unlink()
    (clean_dir / MATTE_REPORT).unlink(missing_ok=True)
    key_frames(raw_dir, clean_dir, MatteSettings.legacy(dist, key_mode, despill))
    return sorted(clean_dir.glob("clean_*.png"))


def _check_matte_gate(report: dict, settings: MatteSettings) -> None:
    """--strict refuses frames whose matte QA failed; otherwise they are published with a warning."""
    failed = [f"{check['id']} {check['value']} (limit {check['threshold']})"
              for check in report["checks"] if check["status"] == "fail"]
    if not failed:
        return
    message = "matte QA failed: " + "; ".join(failed)
    if settings.strict:
        raise ValueError(message + "; nothing was published (fix the key or protect colours, or drop --strict "
                                   "to publish the frames for review)")
    _log(f"warning: {message}; the frames are published for review (--strict refuses them)")


# --------------------------------------------------------------------------- sampling and previews

_LEGACY_PREVIEW_MS = ((40, 25), (20, 40), (12, 60), (0, 80))


def preview_durations(count: int, duration: float | None = None,
                      requested: int | None = None) -> tuple[list[int], int]:
    """Per-frame GIF delays in ms that sum exactly, plus the nominal per-frame ``gif_ms``.

    GIF stores centiseconds, so ``duration`` seconds becomes round(duration *
    100) centiseconds split with forge_core.frame_durations (at least 10 ms per
    frame) instead of one rounded delay repeated. Without a duration the cfed170
    preview speed applies (25, 40, 60 or 80 ms by the requested count), listed
    as the whole centiseconds GIF actually stores.
    """
    if count < 1:
        raise ValueError("a preview needs at least one frame")
    if duration is not None:
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("playback-duration must be positive and finite")
        centiseconds = int(math.floor(duration * 100 + 0.5))
        if centiseconds < count:
            raise ValueError(f"--playback-duration {duration:g} s is too short for {count} preview frames "
                             "(GIF frames last at least 10 ms)")
        return [10 * c for c in fc.frame_durations(centiseconds, count)], max(10, round(duration * 1000 / count))
    nominal = next(ms for floor, ms in _LEGACY_PREVIEW_MS if (requested or count) >= floor)
    return [max(10, nominal // 10 * 10)] * count, nominal


def build_exports(
    sprites: Sequence[Image.Image],
    out_sprite_dir: Path,
    tag: str,
    n_frames: int,
    gif_ms: int | None = None,
    *,
    durations_ms: Sequence[int] | None = None,
) -> dict:
    _ensure_dir(out_sprite_dir)
    sub = _ensure_dir(out_sprite_dir / tag) if tag else out_sprite_dir
    for old in sub.glob("sprite_*.png"):
        old.unlink()
    paths = []
    for i, sp in enumerate(sprites):
        p = sub / f"sprite_{i + 1:02d}.png"
        sp.save(p)
        paths.append(str(p))

    size = sprites[0].size[0]
    strip = Image.new("RGBA", (size * len(sprites), size), (0, 0, 0, 0))
    for i, sp in enumerate(sprites):
        strip.paste(sp, (i * size, 0))
    strip_path = out_sprite_dir / f"run-strip-{n_frames}.png"
    strip.save(strip_path)

    cols = 8 if n_frames >= 16 else 4
    rows = int(math.ceil(len(sprites) / cols))
    grid = Image.new("RGBA", (size * cols, size * rows), (0, 0, 0, 0))
    for i, sp in enumerate(sprites):
        r, c = divmod(i, cols)
        grid.paste(sp, (c * size, r * size))
    grid_path = out_sprite_dir / f"run-grid-{n_frames}.png"
    grid.save(grid_path)

    if durations_ms is None:
        if gif_ms is None:
            durations_ms, gif_ms = preview_durations(len(sprites), requested=n_frames)
        else:
            durations_ms = [max(10, int(gif_ms) // 10 * 10)] * len(sprites)
    elif gif_ms is None:
        gif_ms = max(10, round(sum(durations_ms) / len(durations_ms)))
    durations_ms = [int(ms) for ms in durations_ms]
    if len(durations_ms) != len(sprites):
        raise ValueError("durations_ms needs one duration per sprite")

    frames_gif = []
    for sp in sprites:
        bg = Image.new("RGBA", sp.size, (30, 30, 40, 255))
        bg.paste(sp, (0, 0), sp)
        frames_gif.append(bg.convert("P", palette=Image.ADAPTIVE, colors=255))
    gif_path = out_sprite_dir / f"run-preview-{n_frames}.gif"
    frames_gif[0].save(
        gif_path,
        save_all=True,
        append_images=frames_gif[1:],
        duration=durations_ms,
        loop=0,
        disposal=2,
    )

    # Legacy alias for 8-frame default
    if n_frames == 8:
        alias = out_sprite_dir / "run-preview.gif"
        shutil.copy2(gif_path, alias)

    return {
        "count": n_frames,
        "tag": tag,
        "sprites": paths,
        "strip": str(strip_path),
        "grid": str(grid_path),
        "gif": str(gif_path),
        "gif_ms": gif_ms,
        "gif_durations_ms": durations_ms,
    }


def sample_and_export(
    clean_dir: Path,
    out_dir: Path,
    frame_counts: Sequence[int],
    cell: int = 128,
    body_height: int = 100,
    foot_y: int = 118,
    anchor: str = "feet",
    registration: str = "fixed",
    duration: float | None = None,
) -> dict:
    """Sample even-index frame sets from ``clean_dir`` into ``out_dir/sprite``; paths are output-relative."""
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError("playback-duration must be positive and finite")
    cleans = sorted(Path(clean_dir).glob("clean_*.png"))
    if not cleans:
        raise FrameInputError(f"no cleaned frames in {clean_dir}")
    out_dir = Path(out_dir)
    sprite_dir = _ensure_dir(out_dir / "sprite")
    n_total = len(cleans)
    original = [fc.load_rgba(path)[0] for path in cleans]
    if registration == "legacy-per-frame":
        normalized = [normalize_sprite(im, cell, body_height, foot_y, anchor) for im in original]
        registration_info = {"mode": "legacy-per-frame", "warning": "Per-frame resize changes body scale and erases contact motion."}
    else:
        normalized, registration_info = fixed_envelope(original, cell, body_height, foot_y, anchor)
    results = []
    for n_want in frame_counts:
        idxs = sample_indices(n_total, n_want)
        sprites = [normalized[idx] for idx in idxs]
        durations, gif_ms = preview_durations(len(sprites), duration) if duration else (None, None)
        tag = f"x{n_want}" if n_want != 8 else ""
        # Always also write under xN for consistency when n!=8;
        # for 8, write both root sprites and optional x8.
        if n_want == 8:
            # root-level sprite_01..08 for backwards compat
            info = build_exports(sprites, sprite_dir, tag="", n_frames=n_want, gif_ms=gif_ms, durations_ms=durations)
            # also x8 folder
            build_exports(sprites, sprite_dir, tag="x8", n_frames=n_want, gif_ms=gif_ms, durations_ms=durations)
        else:
            info = build_exports(sprites, sprite_dir, tag=tag, n_frames=n_want, gif_ms=gif_ms, durations_ms=durations)
        info["sprites"] = [fc.manifest_path(Path(p), out_dir) for p in info["sprites"]]
        for name in ("strip", "grid", "gif"):
            info[name] = fc.manifest_path(Path(info[name]), out_dir)
        info["indices"] = idxs
        info["requestedCount"] = n_want
        info["count"] = len(sprites)
        results.append(info)
        _log(f"exported {len(sprites)} frames (requested {n_want}) -> {info['gif']}")
    return {"total_clean": n_total, "registration": registration_info,
            "previewTiming": "specified-duration" if duration else "legacy-preview-only",
            "sets": results}


def write_readme(out_dir: Path, meta: dict) -> None:
    entries = [(f"{RAW_DIR}/", "decoded source frames, frame_000000.png onwards (0-based)"),
               (f"{CLEAN_DIR}/", f"keyed RGBA frames, clean_0000.png onwards, plus {MATTE_REPORT}"),
               ("sprite/", "sampled fixed-envelope sprites, strips, grids and preview GIFs"),
               (PIPELINE_META, "run metadata" + ("; its matte block is the matte report" if "matte" in meta else ""))]
    lines = ["video2dsprite output (provider-independent postprocessing)",
             "=========================================================="]
    lines += [f"{name:<20}{text}" for name, text in entries
              if name == PIPELINE_META or (out_dir / name.rstrip("/")).exists()]
    lines += [
        "",
        "Generation is separate: native tools, generate2dmedia API, or a supplied clip.",
        "Fixed-envelope registration is the default. Inspect source contact drift",
        "and loop seams; dense frames do not prove a seamless game-ready cycle.",
        "Review keyed frames over light and dark backgrounds before packaging.",
        "",
    ]
    (out_dir / "README.txt").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- triage

def _border_subject(frame: np.ndarray, key_rgb: np.ndarray | None, ring: int) -> dict[str, int]:
    """Subject pixels on each border strip: alpha >= 16 on a transparent frame; on a keyed frame the pixels
    farther than 48 (RGB) from the key that lean less than half as far to its channels as the key (not haze)."""
    height, width = frame.shape[:2]
    strips = {"top": frame[:ring], "bottom": frame[height - ring:], "left": frame[:, :ring],
              "right": frame[:, width - ring:]}
    counts = {}
    for edge, strip in strips.items():
        if key_rgb is None:
            subject = strip[..., 3] >= fc.ALPHA_GEOMETRY_THRESHOLD
        else:
            colour = strip[..., :3].astype(np.float32)
            far = np.sqrt(((colour - key_rgb) ** 2).sum(-1)) > fm.KEY_TOLERANCE
            haze = _local_key_dominance(colour, key_rgb) >= 0.5 * float(_local_key_dominance(key_rgb, key_rgb))
            subject = far & ~haze
        counts[edge] = int(subject.sum())
    return counts


def _thumbnail(frame: np.ndarray, size: int = 240) -> Image.Image:
    backdrop = Image.new("RGBA", (frame.shape[1], frame.shape[0]), (128, 128, 128, 255))
    backdrop.alpha_composite(Image.fromarray(frame))
    thumb = backdrop.convert("RGB")
    thumb.thumbnail((size, size), Image.Resampling.LANCZOS)
    return thumb


def _contact_sheet(thumbs: dict[int, Image.Image], flagged: set[int], fps: Fraction,
                   columns: int = 4) -> Image.Image:
    """Overview of evenly spaced raw frames; border-touching frames are outlined and labelled."""
    order = sorted(thumbs)
    cell_w = max(thumb.width for thumb in thumbs.values())
    cell_h = max(thumb.height for thumb in thumbs.values())
    label, pad = 14, 6
    rows = math.ceil(len(order) / columns)
    sheet = Image.new("RGB", (columns * (cell_w + pad) + pad, rows * (cell_h + label + pad) + pad), (32, 36, 44))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for n, index in enumerate(order):
        x = pad + (n % columns) * (cell_w + pad)
        y = pad + (n // columns) * (cell_h + label + pad)
        thumb = thumbs[index]
        sheet.paste(thumb, (x, y + label))
        touching = index in flagged
        draw.text((x, y), f"f{index:04d} {float(index / fps):.2f}s{' BORDER' if touching else ''}",
                  fill=(255, 96, 96) if touching else (225, 228, 235), font=font)
        if touching:
            draw.rectangle([x, y + label, x + thumb.width - 1, y + label + thumb.height - 1],
                           outline=(255, 64, 64), width=2)
    return sheet


def triage_clip(video: Path, out_dir: Path, *, key: str = "auto", border_px: int = 1,
                border_min_px: int = BORDER_MIN_PX, log: Callable[[str], None] | None = None) -> dict:
    """Write raw-triage.json (video2dsprite.raw_triage.v1) and triage-sheet.png for a raw clip.

    Reports size, rational fps, decoded frames, audio and cover-art streams, the
    frames whose subject touches the border (a cropped limb or prop: regenerate
    the clip, never pad it) and the backdrop's drift from the declared key.
    """
    video, out_dir = Path(video), Path(out_dir)
    if not video.is_file():
        raise FileNotFoundError(video)
    if border_px < 1 or border_min_px < 1:
        raise ValueError("--border-px and --border-min-px must be at least 1")
    info = fa.probe(video)
    declared, detection = _normalise_key(key), None
    thumbs_at = set(sample_indices(info["nb_frames"] or 0, TRIAGE_THUMBS))
    touching, estimates, thumbs = [], [], {}
    count = native_frames = 0
    stream = fa.iter_rgba(video)
    try:
        for index, frame in enumerate(stream):
            count += 1
            if index in thumbs_at:
                thumbs[index] = _thumbnail(frame)
            key_rgb = None
            if (frame[..., 3] < 255).any():
                native_frames += 1
            else:
                if declared == "auto":
                    found, detection = _detect_key([frame], [0])
                    declared = found or "magenta"
                    detection["found"] = bool(found)
                key_rgb, estimate = fm.estimate_key(frame, declared)
                estimates.append((index, estimate))
            edges = _border_subject(frame, key_rgb, border_px)
            if sum(edges.values()) >= border_min_px:
                touching.append({"index": index, "px": sum(edges.values()),
                                 "edges": {edge: n for edge, n in edges.items() if n}})
            if log and (index + 1) % 50 == 0:
                log(f"  scanned {index + 1} frames")
    finally:
        stream.close()
    if not count:
        raise FrameInputError(f"no frames decoded from {video.name}")
    fps_text = info["fps_rational"]
    if fps_text is None and info["duration"]:
        fps_text = fc.rational_fps(count, max(1, int(round(info["duration"] * 1000))))
    if fps_text is None:
        raise ValueError(f"{video.name} reports no frame rate or duration")
    drift = None
    if estimates:
        valid = [np.asarray(estimate["key"], np.float32) for _, estimate in estimates if estimate["valid"]]
        declared_rgb = np.asarray(estimates[0][1]["declared_rgb"], np.float32)
        median = np.median(np.stack(valid), axis=0) if valid else declared_rgb
        drift = {"declared": declared, "declaredRgb": estimates[0][1]["declared_rgb"],
                 "estimated": _round_key(median),
                 "distance": round(float(np.linalg.norm(median - declared_rgb)), 2),
                 "spread": round(max((float(np.linalg.norm(key - median)) for key in valid), default=0.0), 2),
                 "framesEstimated": len(estimates),
                 "invalidFrames": [index for index, estimate in estimates if not estimate["valid"]]}
        if detection is not None:
            drift["detected"] = detection
    flagged = [row["index"] for row in touching]
    checks = [
        {"id": "border_touch_frames", "status": "warn" if flagged else "pass", "value": flagged, "threshold": []},
        {"id": "audio_streams", "status": "warn" if info["audio_streams"] else "pass",
         "value": info["audio_streams"], "threshold": 0},
        {"id": "frame_count", "status": "pass" if count == info["nb_frames"] else "warn",
         "value": {"decoded": count, "container": info["nb_frames"]}, "threshold": "decoded == container"},
    ]
    if drift is None:
        checks.append({"id": "key_drift", "status": "skipped", "value": None, "threshold": None})
    else:
        drifting = bool(drift["invalidFrames"]) or drift["spread"] > fm.KEY_TOLERANCE / 2
        checks.append({"id": "key_drift", "status": "warn" if drifting else "pass",
                       "value": {"distance": drift["distance"], "spread": drift["spread"],
                                 "invalidFrames": len(drift["invalidFrames"])},
                       "threshold": {"spread": fm.KEY_TOLERANCE / 2, "invalidFrames": 0}})
    statuses = {check["status"] for check in checks}
    outputs = []
    if thumbs:
        sheet = out_dir / TRIAGE_SHEET
        fc.save_png(_contact_sheet(thumbs, set(flagged), Fraction(fps_text)), sheet)
        outputs.append(fc.file_ref(sheet, out_dir))
    report = {
        "schema": TRIAGE_SCHEMA,
        "size": [info["width"], info["height"]],
        "fps": fps_text,
        "frames": count,
        "audioStreams": info["audio_streams"],
        "borderTouchFrames": flagged,
        "codec": info["codec"],
        "pixFmt": info["pix_fmt"],
        "duration": info["duration"],
        "hasAlpha": info["has_alpha"],
        "nativeAlphaFrames": native_frames,
        "attachedPics": info["attached_pics"],
        "audio": info["audio"],
        "nbFramesContainer": info["nb_frames"],
        "border": {"ringPx": border_px, "minPx": border_min_px, "touches": touching},
        "rule": "If the raw clip crops a limb, weapon or prop inside the action you need, regenerate it; "
                "padding cannot restore missing pixels.",
        "status": "fail" if "fail" in statuses else "warn" if "warn" in statuses else "needs-visual-review",
        "method": (f"ffprobe stream listing (forge_av.probe), then every decoded frame (forge_av.iter_rgba): subject "
                   f"pixels on the outer {border_px} px ring are alpha >= 16 on transparent frames, else farther than "
                   "48 RGB from the frame's border-ring key estimate (forge_matte.estimate_key) and leaning less than "
                   f"half as far to the key's channels; a frame touches when {border_min_px} or more ring pixels are "
                   "subject. Key drift compares the per-frame estimates with the declared key."),
        "notProven": [
            "A clean border does not prove the subject is complete, on-model or that its motion is usable.",
            "Border touches are judged by colour on the outer ring; key-coloured props or haze at the border can "
            "hide or fake a touch.",
            "Audio and cover art are only reported; extract and process drop them.",
        ],
        "checks": checks,
        "inputs": [fc.file_ref(video, out_dir)],
        "outputs": outputs,
        "tool": {"name": "video2dsprite.py triage", "version": TOOL_VERSION},
    }
    if drift is not None:  # a clip with native alpha has no backdrop key to drift
        report["keyDrift"] = drift
    if thumbs:
        report["overview"] = {"file": TRIAGE_SHEET, "frames": sorted(thumbs)}
    fc.write_json(out_dir / TRIAGE_REPORT, report)
    return report


# --------------------------------------------------------------------------- key plan

def _keyed_master(pixels: np.ndarray) -> tuple[np.ndarray | None, dict]:
    """The master with its backdrop removed, for listing design colours at risk (D33).

    A master with transparency is used as it is. An opaque master is keyed first, as ``clean
    --reference`` keys an opaque reference: on a magenta, green or blue backdrop (the declared key whose
    border-ring estimate is valid and holds at least half of the ring within 48 RGB) with
    forge_matte.key_still's soft matte;
    on another uniform backdrop (white, grey: at least half of the 8 px border ring within 48 RGB of its
    median) the subject is every pixel farther than 48 from that colour and more than 2 px inside it, as
    forge_matte.choose_key_color reads an opaque master. Otherwise (no uniform backdrop) None.
    """
    if (pixels[..., 3] < 255).any():
        return pixels, {"method": "alpha"}
    best = None
    for name in fm.DECLARED_KEYS:
        _, estimate = fm.estimate_key(pixels, name)
        if estimate["valid"] and estimate["ring_share"] >= 0.5 and (
                best is None or estimate["ring_share"] > best[1]["ring_share"]):
            best = (name, estimate)
    if best is not None:
        keyed, info = fm.key_still(pixels, quality="soft", key=best[0])
        if info["quality"] != "native_alpha":
            return np.asarray(keyed), {"method": "soft matte (forge_matte.key_still)", "backdrop": best[0],
                                       "backdrop_rgb": _round_key(best[1]["key"])}
    height, width = pixels.shape[:2]
    ring = min(8, max(1, height // 2), max(1, width // 2))
    border = np.ones((height, width), bool)
    border[ring:height - ring, ring:width - ring] = False
    samples = pixels[..., :3][border].astype(np.float32)
    backdrop = np.median(samples, axis=0)
    if np.mean(np.sqrt(((samples - backdrop) ** 2).sum(-1)) <= fm.KEY_TOLERANCE) < 0.5:
        return None, {"method": "none", "reason": "the master has no transparency and no uniform backdrop"}
    near = np.sqrt(((pixels[..., :3].astype(np.float32) - backdrop) ** 2).sum(-1)) <= fm.KEY_TOLERANCE
    inside = fc.distance_to(near, 2) > 2
    if not inside.any():
        return None, {"method": "none", "reason": "the master is all backdrop"}
    keyed = pixels.copy()
    keyed[~inside] = 0
    return keyed, {"method": "backdrop distance", "backdrop_rgb": _round_key(backdrop)}


def key_plan(master: Path, candidates: Sequence[str] = KEY_PLAN_CANDIDATES) -> dict:
    """Recommend the chroma key for an approved master before generation (Dusk prepare-actors.py L18).

    A key is rejected when more than 0.5% of the master's subject pixels lean
    to it by 40 or more (forge_matte.choose_key_color): a purple, violet or pink
    design rejects magenta. Also returns the background sentence for the
    video prompt and the design colours the video soft matte would still key
    out with the recommended key; an opaque master is keyed on its own backdrop
    first (D33, see _keyed_master), so its backdrop is never listed.
    """
    names = [_normalise_key(candidate) for candidate in candidates]
    if not names or "auto" in names:
        raise ValueError("--candidates lists magenta, green, blue or #rrggbb keys")
    image, info = fc.load_rgba(master)
    pixels = np.asarray(image)
    choice = fm.choose_key_color(pixels, names)
    name = choice["key"] if choice["key"] in fm.DECLARED_KEYS else "key"
    plan = {
        "master": {"path": Path(master).name, "sha256": info["sha256"]},
        "key": choice["key"], "hex": choice["hex"], "rgb": choice["rgb"], "status": choice["status"],
        "background_sentence": (f"Keep everything behind the subject a {choice['prompt_background']}: no floor, "
                                "contact shadow, horizon, reflection, background objects, text or "
                                f"{name}-coloured light."),
        "prompt_background": choice["prompt_background"],
        "process_key": choice["key"],
        "candidates": choice["candidates"],
        "rule": choice["rule"],
        "subject_px": choice["subject_px"],
    }
    subject, keying = _keyed_master(pixels)
    if subject is not None:
        _, risk = fm.protect_design_colours(subject, choice["rgb"])
        plan["design_colours_at_risk"] = sorted(risk["colors"], key=lambda row: -row["px"])[:8]
        plan["unprotectable_colours"] = risk["unprotectable"][:8]
    else:
        plan["design_colours_at_risk"] = None
        plan["note"] = f"{keying['reason']}; pass a keyed (RGBA) master to list design colours at risk"
    plan["master_keying"] = keying
    return plan


# --------------------------------------------------------------------------- commands

def _summary(**fields: Any) -> None:
    """The one-line ASCII JSON summary every verb prints on success."""
    print(json.dumps(fields, ensure_ascii=True))


def _published(path: Path) -> str:
    return str(Path(path).resolve())


def _load_reference(path: Path | None) -> np.ndarray | None:
    return None if path is None else _load_pixels(Path(path))[0]


def _fps_label(fps: str) -> str:
    rate = Fraction(str(fps).strip())
    return "passthrough" if rate == 0 else f"{rate.numerator}/{rate.denominator}"


def cmd_extract(args: argparse.Namespace) -> int:
    final = Path(args.output_dir)
    with fc.staged_output(final) as stage:
        frames = extract_frames(Path(args.video), stage, fps=args.fps, start=args.start, duration=args.duration,
                                decoder=args.decoder, alpha=args.alpha)
    _summary(output=_published(final), metadata=None, frames=len(frames), first=frames[0].name,
             last=frames[-1].name)
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    settings, profile = matte_settings(args)
    reference = _load_reference(args.reference)
    final = Path(args.output_dir)
    started = time.perf_counter()
    with fc.staged_output(final) as stage:
        report = key_frames(Path(args.raw_dir), stage, settings, reference=reference, profile=profile,
                            tool="video2dsprite.py clean", log=_log, workers=args.workers)
        _check_matte_gate(report, settings)
    _summary(output=_published(final), metadata=_published(final / MATTE_REPORT), frames=report["frames_total"],
             matte=report["mode"], key=report["key"], status=report["status"],
             flips_per_frame_pair=report.get("flips_per_frame_pair"),
             seconds_per_frame=round((time.perf_counter() - started) / report["frames_total"], 3))
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    counts = _parse_counts(args.frame_counts)
    clean_dir = Path(args.clean_dir)
    final = Path(args.output_dir)
    with fc.staged_output(final) as stage:
        meta = sample_and_export(clean_dir, stage, counts, cell=args.cell_size, body_height=args.body_height,
                                 foot_y=args.foot_y, anchor=args.anchor, registration=args.registration,
                                 duration=args.playback_duration)
        full = {"mode": "sample", "tool": {"name": "video2dsprite.py sample", "version": TOOL_VERSION},
                "clean_dir": fc.manifest_path(clean_dir, stage), **meta}
        if (clean_dir / MATTE_REPORT).is_file():
            full["matte_report"] = fc.file_ref(clean_dir / MATTE_REPORT, stage)
        fc.write_json(stage / PIPELINE_META, full)
        write_readme(stage, full)
    _summary(output=_published(final), metadata=_published(final / PIPELINE_META),
             sets=[{"count": s["count"], "gif": s["gif"]} for s in meta["sets"]])
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    settings, profile = matte_settings(args)
    counts = _parse_counts(args.frame_counts)
    video = Path(args.video)
    if not video.is_file():
        raise FileNotFoundError(video)
    reference = _load_reference(args.reference)
    final = Path(args.output_dir)
    with fc.staged_output(final) as stage:
        probe = fa.probe(video)
        _log(f"extract {video.name}")
        frames = extract_frames(video, stage / RAW_DIR, fps=args.fps, start=args.start, duration=args.duration,
                                decoder=args.decoder, alpha=args.alpha)
        trimmed = bool(args.start) or args.duration is not None
        wants_rule = settings.key_mode != "none" and (settings.matte == "soft" or settings.despill_mode == "auto")
        despill_sample = sample_source_frames(video, probe["nb_frames"] or 0) if trimmed and wants_rule else None
        _log(f"key {len(frames)} frames ({settings.matte} matte)")
        started = time.perf_counter()
        report = key_frames(stage / RAW_DIR, stage / CLEAN_DIR, settings, reference=reference, profile=profile,
                            despill_sample=despill_sample, tool="video2dsprite.py process", log=_log,
                            workers=args.workers)
        key_seconds = time.perf_counter() - started
        _check_matte_gate(report, settings)
        _log(f"sample counts={counts}")
        meta_sample = sample_and_export(
            clean_dir=stage / CLEAN_DIR,
            out_dir=stage,
            frame_counts=counts,
            cell=args.cell_size,
            body_height=args.body_height,
            foot_y=args.foot_y,
            anchor=args.anchor,
            registration=args.registration,
            duration=args.playback_duration,
        )
        outputs = {"frames_raw": RAW_DIR, "frames_clean": CLEAN_DIR, "matte_report": f"{CLEAN_DIR}/{MATTE_REPORT}",
                   "sprite": "sprite", "readme": "README.txt"}
        meta = {
            "skill": "video2dsprite",
            "platform": "provider-independent offline processor",
            "tool": {"name": "video2dsprite.py process", "version": TOOL_VERSION},
            "name": args.name,
            "video": fc.file_ref(video, stage),
            "probe": {name: probe[name] for name in ("codec", "width", "height", "pix_fmt", "fps_rational",
                                                     "nb_frames", "duration", "has_alpha", "audio_streams",
                                                     "attached_pics")},
            "extract": {"fps": _fps_label(args.fps), "start": args.start, "duration": args.duration,
                        "alpha": "auto" if args.decoder == "libvpx-vp9" else args.alpha,
                        "naming": "frame_000000.png onwards (0-based)"},
            "raw_frames": len(frames),
            "cell_size": args.cell_size,
            "body_height": args.body_height,
            "foot_y": args.foot_y,
            "anchor": args.anchor,
            "matte": {**{key: value for key, value in report.items() if key not in ("inputs", "outputs")},
                      "report_file": outputs["matte_report"]},
            **meta_sample,
            "outputs": outputs,
        }
        fc.write_json(stage / PIPELINE_META, meta)
        write_readme(stage, meta)
    _summary(output=_published(final), metadata=_published(final / PIPELINE_META),
             outputs={name: _published(final / relative) for name, relative in outputs.items()},
             frames=len(frames),
             matte={name: report.get(name) for name in ("mode", "status", "key", "opaque_key_px",
                                                        "enclosed_key_pockets", "flips_per_frame_pair")},
             sets=[{"count": s["count"], "gif": s["gif"]} for s in meta_sample["sets"]],
             key_seconds_per_frame=round(key_seconds / len(frames), 3))
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    final = Path(args.output_dir)
    with fc.staged_output(final) as stage:
        report = triage_clip(Path(args.video), stage, key=args.key or "auto", border_px=args.border_px,
                             border_min_px=args.border_min_px, log=_log)
        touching = report["borderTouchFrames"]
        if touching:
            message = (f"the subject touches the border in {len(touching)} frame(s) ({_short_list(touching)}): "
                       "a cropped limb or prop; regenerate the clip if it is inside the action you need, never pad it")
            if args.strict:
                raise ValueError(message + "; nothing was published")
            _log(f"warning: {message}")
    _summary(output=_published(final), metadata=_published(final / TRIAGE_REPORT),
             sheet=_published(final / TRIAGE_SHEET) if "overview" in report else None, status=report["status"],
             size=report["size"], fps=report["fps"], frames=report["frames"], audioStreams=report["audioStreams"],
             borderTouchFrames=len(touching),
             keyDistance=report["keyDrift"]["distance"] if "keyDrift" in report else None)
    return 0


def cmd_key_plan(args: argparse.Namespace) -> int:
    plan = key_plan(Path(args.master), [part.strip() for part in args.candidates.split(",") if part.strip()])
    if plan["status"] != "ok":
        message = (f"every candidate key fights the master's colours; {plan['key']} overlaps least "
                   "(protect or recolour the overlapping design colours)")
        if args.strict:
            raise ValueError(message)
        _log(f"warning: {message}")
    _summary(**plan)
    return 0


@functools.lru_cache(maxsize=1)
def engine_module():
    """The sibling engine_export.py (same skill), loaded once: package and verify are its verbs (D20)."""
    spec = importlib.util.spec_from_file_location("forge_engine_export", Path(__file__).with_name("engine_export.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_package(args: argparse.Namespace) -> int:
    """engine_export package (D20): every 3.0 flag, the residue gate and its legacy switch; the one-line
    summary names the output folder and the metadata file (animation.json)."""
    return engine_module().cmd_package(args)


def cmd_verify(args: argparse.Namespace) -> int:
    """engine_export verify (D20): decode every encoded file of a package against its atlas."""
    return engine_module().cmd_verify(args)


def cmd_doctor(args: argparse.Namespace) -> int:
    info = fa.ffmpeg_info()
    functional = info.get("functional", {})
    _summary(**info, webm=bool(functional.get("vp9_alpha")), packed=bool(functional.get("libx264")), png=True)
    return 0


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Video -> dense 2D sprite postprocessor (offline and deterministic; never generates).",
        epilog="Keying defaults: the soft matte with enclosed-pocket removal, auto despill and alpha "
               "hysteresis. The cfed170 keyer is --matte binary --despill-mode off. Outputs go to a new "
               "--output-dir and are published only when the run succeeds.")
    sub = p.add_subparsers(dest="command", required=True)

    def add_output(sp: argparse.ArgumentParser, what: str) -> None:
        sp.add_argument("--output-dir", "--out-dir", dest="output_dir", type=Path, required=True,
                        help=f"new directory for {what}; it must not exist (--out-dir is the old name)")

    def add_common_sample(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--frame-counts", default="8,16,24,48")
        sp.add_argument("--cell-size", type=int, default=128)
        sp.add_argument("--body-height", type=int, default=100)
        sp.add_argument("--foot-y", type=int, default=118)
        sp.add_argument("--anchor", choices=("feet", "center"), default="feet")
        sp.add_argument("--registration", choices=("fixed", "legacy-per-frame"), default="fixed")
        sp.add_argument("--playback-duration", type=float,
                        help="preview seconds, split into exact per-frame GIF delays; otherwise legacy preview timing")

    def add_trim(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--fps", default="0", help="0 = every source frame (passthrough); else a number or N/D")
        sp.add_argument("--start", type=float, default=0.0, help="trim start in source seconds")
        sp.add_argument("--duration", type=float, help="trim length in seconds; frames in [start, start+duration)")
        sp.add_argument("--alpha", choices=fa.ALPHA_MODES, default="auto",
                        help="auto keeps WebM alpha (decoded with libvpx), on requires it, off drops it")
        sp.add_argument("--decoder", choices=DECODERS, default="default",
                        help="old spelling: libvpx-vp9 equals --alpha auto")

    def add_matte(sp: argparse.ArgumentParser) -> None:
        group = sp.add_argument_group("keying (references/matte.md)")
        group.add_argument("--matte", choices=MATTES, help="soft (default), dominance or binary (the cfed170 keyer)")
        group.add_argument("--key", type=_key_argument,
                           help="auto (default: detect magenta, green or blue on the border ring), magenta, green, "
                                "blue (estimated per frame) or #rrggbb (used as given)")
        group.add_argument("--key-mode", choices=KEY_MODES, default="auto",
                           help="auto keeps frames with native alpha, always keys every frame, magenta is the old "
                                "spelling of always with the magenta key, none copies frames")
        group.add_argument("--despill-mode", choices=fm.DESPILL_MODES,
                           help="auto (default) decides interior despill once per clip; edge, all or off")
        group.add_argument("--despill-radius", type=int, default=1,
                           help="edge despill reach in px for the dominance and binary mattes (default 1)")
        group.add_argument("--pockets", choices=POCKET_MODES, default="auto",
                           help="remove enclosed key-coloured holes; auto removes them except with --matte binary")
        group.add_argument("--protect-color", action="append", metavar="HEX",
                           help="design colour that is never keyed or despilled (repeatable, #rrggbb)")
        group.add_argument("--protect-tol", type=float, default=0.03, help="OKLab distance for --protect-color")
        group.add_argument("--erode", type=int, help="shrink the matte by this many px (default 0)")
        group.add_argument("--unmix", action=argparse.BooleanOptionalAction,
                           help="soft matte only: un-mix edge luma from the key (default on); --no-unmix takes "
                                "edge colour from the neighbouring subject only")
        group.add_argument("--temporal-stability", choices=TEMPORAL_MODES, default="auto",
                           help="alpha damps matte flicker with an alpha hysteresis, off keys frames alone; auto "
                                "(default) is alpha for soft and dominance, off for binary")
        group.add_argument("--local-background", type=int, nargs="?", const=fm.LOCAL_BACKGROUND_RADIUS, default=0,
                           metavar="PX", help="soft matte against the local backdrop mean (default radius 12 px)")
        group.add_argument("--matte-profile", type=Path,
                           help="character profile JSON whose matte block pins mode, key, erode, unmix and despill")
        group.add_argument("--reference", type=Path,
                           help="approved master still: joins the auto despill rule and names design colours at risk")
        group.add_argument("--strict", action="store_true",
                           help="fail and publish nothing when matte QA finds key residue")
        group.add_argument("--workers", type=_workers_argument, default=0, metavar="N",
                           help="threads that decode and matte frames (default 0: min(4, CPUs); 1 = one at a "
                                "time); the output bytes do not depend on it")
        group.add_argument("--dist", type=float, help="binary only: flood key distance (default 55)")
        group.add_argument("--despill", type=float, help="binary only: first-ring despill strength 0..1")

    pt = sub.add_parser("triage", help="report on a raw clip before processing")
    pt.add_argument("--video", required=True, type=Path)
    add_output(pt, "raw-triage.json and triage-sheet.png")
    pt.add_argument("--key", type=_key_argument, help="declared key (default auto)")
    pt.add_argument("--border-px", type=int, default=1, help="width of the border ring that is checked")
    pt.add_argument("--border-min-px", type=int, default=BORDER_MIN_PX,
                    help="subject pixels on the ring that make a frame touch the border")
    pt.add_argument("--strict", action="store_true",
                    help="fail and publish nothing when the subject touches the border")
    pt.set_defaults(func=cmd_triage)

    pkp = sub.add_parser("key-plan", help="recommend the chroma key for a master before generation")
    pkp.add_argument("--master", required=True, type=Path, help="approved master still (RGBA preferred)")
    pkp.add_argument("--candidates", default=",".join(KEY_PLAN_CANDIDATES),
                     help="keys to try in order (default magenta,green,blue; #rrggbb allowed)")
    pkp.add_argument("--strict", action="store_true", help="exit 1 when every candidate fights the design")
    pkp.set_defaults(func=cmd_key_plan)

    pe = sub.add_parser("extract", help="ffmpeg extract frames")
    pe.add_argument("--video", required=True)
    add_output(pe, "frame_000000.png onwards")
    add_trim(pe)
    pe.set_defaults(func=cmd_extract)

    pc = sub.add_parser("clean", help="chroma-key raw frames")
    pc.add_argument("--raw-dir", required=True)
    add_output(pc, "clean_0000.png onwards and matte-report.json")
    add_matte(pc)
    pc.set_defaults(func=cmd_clean)

    ps = sub.add_parser("sample", help="sample cleaned frames into sprite sets")
    ps.add_argument("--clean-dir", required=True)
    add_output(ps, "sprite sets and pipeline-meta.json")
    add_common_sample(ps)
    ps.set_defaults(func=cmd_sample)

    pp = sub.add_parser("process", help="extract + clean + sample")
    pp.add_argument("--video", required=True)
    add_output(pp, "frames-raw, frames-clean, sprite and pipeline-meta.json")
    pp.add_argument("--name", default="clip")
    add_trim(pp)
    add_matte(pp)
    add_common_sample(pp)
    pp.set_defaults(func=cmd_process)

    engine = engine_module()
    pk = sub.add_parser("package", help="animation.json 3.0 package (engine_export package; same flags)",
                        description="engine_export.py package: PNG poster and atlas, optional WebM, packed MP4 and "
                                    "mobile tiers behind the key-residue gate (--allow-key-residue records an "
                                    "override). --out-dir is the old name of --output-dir.")
    engine.add_package_arguments(pk)
    pk.set_defaults(func=cmd_package)

    pv = sub.add_parser("verify", help="decode a package's video files against its atlas (engine_export verify)",
                        description="engine_export.py verify: decode every encoded file completely, compare it with "
                                    "the PNG atlas and write verify-qa.json only when every gate passes.")
    engine.add_verify_arguments(pv)
    pv.set_defaults(func=cmd_verify)

    pd = sub.add_parser("doctor", help="prove which ffmpeg encoders and decoders really work")
    pd.set_defaults(func=cmd_doctor)

    return p


def _workers_argument(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--workers needs a whole number >= 0, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"--workers needs a whole number >= 0, got {text!r}")
    return value


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileExistsError as exc:
        target = exc.filename or exc
        raise FileExistsError(f"{target} already exists; choose a new --output-dir (runs never overwrite)") from None


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors exit 2 (argparse); refused input and failed gates print ``error: ...`` and exit 1; anything
    unexpected prints ``error: internal error (...)`` and exits 1 (D26, D27; forge_core.run_cli)."""
    expected = fc.CLI_EXPECTED_ERRORS + (FrameInputError, fa.ForgeAVError, engine_module().PackageError)
    return fc.run_cli(_run, argv, expected=expected)


if __name__ == "__main__":
    raise SystemExit(main())
