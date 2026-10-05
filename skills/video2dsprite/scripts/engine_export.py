#!/usr/bin/env python3
"""Fixed-canvas animation packaging and transport verification (animation.json 3.0).

Verbs (``python engine_export.py <verb> --help``):

* ``package`` writes a new output directory: a PNG poster and atlas fallback (always),
  optional VP9-alpha WebM, packed-alpha MP4 (RGB left, alpha right, even halves) and
  mobile packed tiers, plus animation.json 3.0, animation-qa.json and provenance.json.
  Frames come from a clean RGBA directory, optionally cut and timed by a frame selection
  (forge-frame-selection v1/v2), placed by a registration job and accepted by a review
  verdict. A key-residue gate refuses frames with opaque key pixels or more than 1% key
  spill on the outer ring unless --allow-key-residue records an override. With --key auto
  the gate's key comes from <clean-dir>/matte-report.json, then the registration keyColor,
  then --pipeline-meta, then magenta (keySource records which). Uneven whole-tick durations
  (retime --ticks rows, held frames) are expanded into repeated frames for video transports.
* ``verify`` decodes every encoded file completely, compares it with the lossless atlas
  and publishes verify-qa.json beside the manifest; nothing is written when a gate fails.
* ``doctor`` prints the functional ffmpeg capabilities as one JSON line.

Geometry stays in source units: sourceSize, sourceAnchor and sourceRect never change with
the media size, tier or frame rate. Timing is whole milliseconds (durationsMs) played by an
exact rational transport rate of at most 60 fps. No generation, network access or game edits.
"""
from __future__ import annotations

import argparse
import bisect
import itertools
import json
import math
import os
import re
import shutil
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

import numpy as np
from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_av  # noqa: E402  (the skill's vendored copies)
import forge_core  # noqa: E402
import forge_matte  # noqa: E402

ENGINE_EXPORT_VERSION = "3.0.0"  # the animation.json 3.0 writer; QA envelopes record the package version (D29)
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION
SCHEMA_VERSION = "3.0"
TOOL_NAME = "engine_export.py"
FORMATS = ("png", "webm", "packed")
MAX_FPS = Fraction(forge_av.MAX_FPS)
MAX_INPUT_FPS = Fraction(1000)
DECODED_PIXEL_BUDGET = 128_000_000
MANIFEST_FILE, QA_FILE, PROVENANCE_FILE, VERIFY_FILE = (
    "animation.json", "animation-qa.json", "provenance.json", "verify-qa.json")
SELECTION_V1, SELECTION_V2 = "forge-frame-selection/v1", "forge-frame-selection/v2"
MATTE_REPORT_FILE, MATTE_REPORT_SCHEMA = "matte-report.json", "video2dsprite.matte_report.v1"
REGISTRATION_JOB = "video2dsprite.registration_job.v1"
REVIEW_VERDICT = "video2dsprite.review_verdict.v1"
PROVENANCE_SCHEMA = "video2dsprite.provenance.v1"
VERIFY_SCHEMA = "video2dsprite.verify.v1"
LOOP_POLICIES = ("cycle", "pingpong", "oneshot")
REGISTRATION_MODES = ("construction", "fixed-envelope", "preserved")
REVIEW_STATUSES = ("accepted", "accept_with_mask", "fail_regenerate")
ART_SOURCES = ("host_image", "api", "code", "existing", "video", "mixed")
EVENT_NAMES = frozenset({"in", "tell", "hit", "active_end", "cancel", "chain", "impact", "hold", "end",
                         "sfx", "step_l", "step_r"})
KEY_NAMES = {"magenta": (255, 0, 255), "green": (0, 255, 0), "blue": (0, 0, 255)}
# Mobile packed-alpha tiers: long edge in px and fps ceiling (Dusk 1.8.3 mobile budgets).
DEFAULT_TIERS: dict[str, tuple[int, Fraction]] = {
    "actor": (320, Fraction(24)), "prop": (384, Fraction(12)), "fx": (512, Fraction(30))}
# Key-residue gate (report v2 P0-3): any opaque key pixel, or key spill on more than 1% of the
# outer visible ring (cfed170 shipped about 70%), refuses the package.
RESIDUE_LIMITS = {"opaqueKeyPx": 0, "outerRingSpillFraction": 0.01}
# The gate measures a copy with alpha <= 16 cleared (D18): lanczos registration (forge_core.resample_rgba)
# leaves invisible alpha 1-4 halo pixels with invented key-leaning colours that are not residue. A real
# key fringe is visible (alpha 255 on the cfed170 frames) and still fails.
RESIDUE_ALPHA_FLOOR = forge_core.ALPHA_GEOMETRY_THRESHOLD
# Video transports play one constant rate: uneven whole-tick durations are expanded into repeated frames at
# the first of these rates whose tick grid holds every frame edge within this many ms (D21).
TICK_EDGE_TOLERANCE_MS = 1
DEFAULT_TICK_HZ = 60
# A loop's wrap may exceed the 95th-percentile adjacent step by 10% before it counts as a pop.
SEAM_TOLERANCE = 1.10
# verify gates: Dusk iOS packed-alpha transport (validate-packed-alpha.py) and hd2d seam findings.
VERIFY_LIMITS = {
    "alphaMAE": 3.5,             # worst-frame mean |alpha error| in 0-255 units
    "transparentMedian": 1.0,    # decoded alpha median where the reference alpha is 0
    "opaqueMedian": 253.0,       # decoded alpha median where the reference alpha is 255
    "rgbMAE": 12.0,              # worst-frame straight RGB error where the reference alpha > 16
    "durationToleranceMs": 2.0,  # container duration against the manifest timeline
    "seamTolerance": SEAM_TOLERANCE,
    "sourceAlphaMotion": 0.1,    # reference alpha that moves this much per frame pair ...
    "decodedAlphaMotion": 0.05,  # ... must still move this much after decoding
}
QA_SEVERITY = {"skipped": -1, "pass": 0, "needs-visual-review": 1, "warn": 2, "fail": 3}
_NAME = re.compile(r"[A-Za-z0-9_-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HEX = re.compile(r"#?([0-9a-fA-F]{6})")

PACKAGE_NOT_PROVEN = (
    "Visual identity, contact and gait quality: a low seam ratio or stable bounds do not prove a "
    "seamless cycle or a planted foot.",
    "Playback of the encoded files: run engine_export.py verify to decode them and compare them with "
    "the atlas, then validate_animation.py.",
    "Browser, Safari, WebGL or iPhone playback, decoder throughput and memory on target devices.",
    "Key-residue thresholds were set on one clip (report v2); unusual keys or costumes need a look.",
)
VERIFY_NOT_PROVEN = (
    "Playback in a browser, Safari, a WebGL compositor or on an iPhone: only offline ffmpeg decoding "
    "was checked.",
    "Perceptual quality, identity and motion correctness of the art itself.",
    "Decoder throughput, memory and power on target devices.",
    "Thresholds come from one shipped game's iOS transport (Dusk) and synthetic clips.",
)
RUNTIME_NOTES = (
    "Game logic owns hit timings, damage and root motion.",
    "Packed MP4 has no native alpha; reconstruct RGB and alpha before displaying.",
    "Keep sourceSize/sourceAnchor for world geometry when selecting smaller media.",
    "Use the PNG poster until a decoded first frame exists; preload only current scene actions.",
    "Packed halves are padded to even sizes: crop RGB at (0, 0, width, height) and alpha (red) at "
    "(halfWidth, 0, width, height).",
    "durationsMs is the timeline; fps/fpsRational is the constant rate of the video transports.",
)


class PackageError(RuntimeError):
    """A deliberate packaging failure (missing encoder, refused input); the CLI prints it as ``error: ...``."""


class QualityGateError(PackageError):
    """A strict QA gate refused the input; nothing was published."""


# Errors the CLI reports as plain ``error: <message>``; anything else is an internal error (D27).
CLI_ERRORS = forge_core.CLI_EXPECTED_ERRORS + (PackageError, forge_av.ForgeAVError)


# --------------------------------------------------------------------------- small helpers

def run(command: Sequence[str], timeout: float = forge_av.DEFAULT_TIMEOUT) -> str:
    """Run a tool and return its stdout decoded as UTF-8 with replacement (forge_av.run)."""
    return forge_av.run(command, timeout)


def capabilities() -> dict:
    """Functional encoder capabilities in the 2.0 doctor shape (forge_av.ffmpeg_info).

    ``webm`` means a VP9 frame with transparency survives encode and libvpx-vp9 decode;
    ``packed`` means libx264 really encodes. Without ffmpeg and ffprobe nothing is probed.
    """
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return {"ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"), "version": None,
                "webm": False, "packed": False, "png": True,
                "errors": {"ffmpeg": "ffmpeg 5.1+ and ffprobe are needed for webm and packed transports"},
                "engineExport": ENGINE_EXPORT_VERSION}
    info = forge_av.ffmpeg_info()
    return {"ffmpeg": info["ffmpeg"], "ffprobe": info["ffprobe"], "version": info["version"],
            "webm": info["functional"]["vp9_alpha"], "packed": info["functional"]["libx264"], "png": True,
            "errors": info["errors"], "engineExport": ENGINE_EXPORT_VERSION}


def pair(text: str | None, fallback: tuple, integer: bool = False) -> tuple:
    """Two finite comma-separated numbers (``W,H`` or ``X,Y``), or ``fallback``."""
    if text is None or text == "":
        values = tuple(fallback)
    else:
        try:
            values = tuple((int if integer else float)(v) for v in str(text).split(","))
        except ValueError:
            raise ValueError(f"expected two comma-separated {'integers' if integer else 'numbers'}, "
                             f"got {forge_core.ascii_text(str(text))!r}") from None
    if len(values) != 2 or not all(math.isfinite(v) for v in values):
        raise ValueError("expected two finite comma-separated coordinates")
    return values


def bbox(im: Image.Image) -> tuple | None:
    """Body bounds: pixels with alpha above forge_core.BODY_ALPHA_THRESHOLD."""
    return forge_core.subject_bbox(im, forge_core.BODY_ALPHA_THRESHOLD)


def digest(path: Path) -> dict:
    """The 2.0 media record of a packaged file: name, byte size and sha256."""
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": forge_core.sha256_file(path)}


round_half_up = forge_core.round_half_up  # floor(value + 1/2), exact for Fractions (D30)


def rational(rate: Fraction) -> str:
    """Exact ``"num/den"`` rate text (ffmpeg syntax, common fpsValue)."""
    return f"{rate.numerator}/{rate.denominator}"


def tier_text(spec: "TierSpec") -> str:
    """A tier as the --tiers syntax accepts it: ``name:EDGE@FPS``."""
    fps = str(spec.fps.numerator) if spec.fps.denominator == 1 else rational(spec.fps)
    return f"{spec.name}:{spec.edge}@{fps}"


def parse_rate(value: Any, name: str = "fps", maximum: Fraction = MAX_INPUT_FPS) -> Fraction:
    """A positive frame rate from a number or an exact ``"num/den"`` string."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Fraction)):
        raise ValueError(f"{name} must be a number or a 'num/den' string")
    try:
        rate = value if isinstance(value, Fraction) else Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"{name} must be positive and finite, like 24 or 30000/1001") from None
    if not 0 < rate <= maximum:
        raise ValueError(f"{name} must be positive, finite and at most {maximum}")
    return rate


def _finite(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _whole(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be a whole number >= {minimum}")
    return value


def _read_object(path: Path, label: str) -> dict:
    """A JSON object read as UTF-8 with or without a BOM (forge_core.read_json, D28; strict JSON)."""
    try:
        data = forge_core.read_json(path, strict=True)
    except FileNotFoundError:
        raise ValueError(f"{label} not found: {path.name}") from None
    except ValueError as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object")
    return data


def _input_ref(path: Path, role: str) -> dict:
    """fileRef of an input that lives outside the package: its file name (never an absolute path)."""
    return {"path": path.name, "sha256": forge_core.sha256_file(path), "bytes": path.stat().st_size, "role": role}


def relative_dir(target: Path, base: Path) -> str | None:
    """POSIX path from ``base`` to ``target``, or None when only an absolute one exists (another drive)."""
    try:
        text = Path(os.path.relpath(target.resolve(), base.resolve())).as_posix()
    except ValueError:
        return None
    return None if text.startswith("/") or re.match(r"[A-Za-z]:", text) else text


def frames_digest(hashes: Sequence[str]) -> str:
    """sha256 of the newline-terminated frame sha256 list in playback order (review binding)."""
    return forge_core.sha256_bytes("".join(f"{h}\n" for h in hashes).encode("ascii"))


def list_frames(clean_dir: Path) -> list[Path]:
    """Frames sorted by name: ``clean_*.png`` when present (video2dsprite clean), else every ``*.png``."""
    return sorted(clean_dir.glob("clean_*.png")) or sorted(clean_dir.glob("*.png"))


# --------------------------------------------------------------------------- options

class TierSpec(NamedTuple):
    """A mobile packed-alpha tier: name, maximum long edge (px) and fps ceiling."""
    name: str
    edge: int
    fps: Fraction


def parse_tiers(text: str | None) -> tuple[TierSpec, ...]:
    """``actor,prop:384@12,custom:256@20``: a default tier name or ``name:EDGE@FPS`` per entry."""
    if not text:
        return ()
    tiers: list[TierSpec] = []
    for part in (p.strip() for p in str(text).split(",")):
        if not part:
            continue
        name, _, spec = part.partition(":")
        if not _NAME.fullmatch(name):
            raise ValueError(f"tier name must use letters, digits, - or _: {forge_core.ascii_text(part)!r}")
        if not spec:
            if name not in DEFAULT_TIERS:
                raise ValueError(f"unknown tier {name!r}; use {', '.join(DEFAULT_TIERS)} or name:EDGE@FPS")
            edge, fps = DEFAULT_TIERS[name]
        else:
            edge_text, at, fps_text = spec.partition("@")
            if not at or not edge_text.isdigit():
                raise ValueError(f"tier {name!r} must look like {name}:320@24 (long edge @ fps)")
            edge, fps = int(edge_text), parse_rate(fps_text, f"tier {name} fps", MAX_FPS)
        if not 2 <= edge <= 4096:
            raise ValueError(f"tier {name!r} long edge must be 2..4096 px")
        if any(t.name == name for t in tiers):
            raise ValueError(f"tier {name!r} is listed twice")
        tiers.append(TierSpec(name, edge, fps))
    return tuple(tiers)


class PackageOptions(NamedTuple):
    """Every package option, with the cfed170 namespace (video2dsprite.py package) accepted as is."""
    clean_dir: Path
    out_dir: Path
    name: str = "clip"
    fps: Fraction | None = None
    source_size: tuple[int, int] | None = None
    source_anchor: tuple[float, float] | None = None
    max_side: int = 384
    crop_union: bool = False
    formats: frozenset[str] = frozenset({"png"})
    loop: bool = False
    loop_policy: str | None = None
    selection: Path | None = None
    registration: Path | None = None
    review: Path | None = None
    pipeline_meta: Path | None = None
    tiers: tuple[TierSpec, ...] = ()
    budget_class: str | None = None
    key: str = "auto"
    allow_key_residue: bool = False
    pixel_art: bool = False
    sampling: str | None = None
    resampler: str = "lanczos"
    body_height_px: float | None = None
    shadow: dict | None = None
    display_scale: float | None = None
    cadence_ms: float | None = None
    stride_world_units: float | None = None
    speed_ref: float | None = None
    cycles: float | None = None
    terminal: bool = False
    art_source: str = "video"
    placeholder: bool = False
    crf_webm: int = 28
    crf_packed: int = 18
    action_padding: tuple[int, int, int, int] | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "PackageOptions":
        get = lambda name, default=None: getattr(args, name, default)  # noqa: E731
        name = get("name") or "clip"
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("name must contain only letters, digits, hyphens and underscores")
        fps = get("fps")
        if isinstance(fps, float) and not math.isfinite(fps):
            raise ValueError("fps must be positive and finite")
        formats = {p.strip() for p in str(get("formats") or "png").split(",") if p.strip()}
        if not formats <= set(FORMATS):
            raise ValueError("formats supports png,webm,packed")
        max_side = get("max_side", 384)
        if isinstance(max_side, bool) or not isinstance(max_side, int) or not 2 <= max_side <= 4096:
            raise ValueError("max-side must be a whole number in 2..4096")
        pixel_art = bool(get("pixel_art", False))
        resampler = get("resampler") or ("nearest" if pixel_art else "lanczos")
        if resampler not in forge_core.RESAMPLERS:
            raise ValueError(f"resampler must be one of {', '.join(forge_core.RESAMPLERS)}")
        sampling = get("sampling") or ("nearest" if pixel_art else None)
        if sampling not in (None, "nearest", "linear"):
            raise ValueError("sampling must be nearest or linear")
        loop_policy = get("loop_policy")
        if loop_policy not in (None, *LOOP_POLICIES):
            raise ValueError(f"loop-policy must be one of {', '.join(LOOP_POLICIES)}")
        art_source = get("art_source") or "video"
        if art_source not in ART_SOURCES:
            raise ValueError(f"art-source must be one of {', '.join(ART_SOURCES)}")
        tiers = parse_tiers(get("tiers"))
        budget = get("budget_class") or (tiers[0].name if len(tiers) == 1 else None)
        if budget is not None and not _NAME.fullmatch(budget):
            raise ValueError("budget-class must use letters, digits, - or _")
        numbers = {}
        for attr, flag in (("body_height_px", "body-height-px"), ("display_scale", "display-scale"),
                           ("cadence_ms", "cadence-ms"), ("stride_world_units", "stride-world-units"),
                           ("speed_ref", "speed-ref"), ("cycles", "cycles")):
            value = get(attr)
            numbers[attr] = None if value is None else float(_finite(value, flag, positive=True))
        options = cls(
            clean_dir=Path(get("clean_dir")), out_dir=Path(get("out_dir")), name=name,
            fps=None if fps is None else parse_rate(fps),
            source_size=None if get("source_size") in (None, "") else pair(get("source_size"), (), True),
            source_anchor=None if get("source_anchor") in (None, "") else pair(get("source_anchor"), ()),
            max_side=max_side, crop_union=bool(get("crop_union", False)), formats=frozenset(formats | {"png"}),
            loop=bool(get("loop", False)), loop_policy=loop_policy,
            selection=_optional_path(get("selection")), registration=_optional_path(get("registration")),
            review=_optional_path(get("review")), pipeline_meta=_optional_path(get("pipeline_meta")),
            tiers=tiers, budget_class=budget, key=str(get("key") or "auto"),
            allow_key_residue=bool(get("allow_key_residue", False)), pixel_art=pixel_art, sampling=sampling,
            resampler=resampler, shadow=_parse_shadow(get("shadow")), terminal=bool(get("terminal", False)),
            art_source=art_source, placeholder=bool(get("placeholder", False)),
            crf_webm=_crf(get("crf_webm", 28), 63, "crf-webm"), crf_packed=_crf(get("crf_packed", 18), 51, "crf-packed"),
            action_padding=_parse_padding(get("action_padding")), **numbers)
        if options.source_size is not None and min(options.source_size) <= 0:
            raise ValueError("source-size must be positive")
        if options.pixel_art and options.resampler != "nearest":
            raise ValueError("--pixel-art resamples with nearest; drop --resampler or set it to nearest")
        if options.action_padding is not None and options.registration is None:
            raise ValueError("--action-padding applies to a --registration job (the padding register_clip apply "
                             "used); pass the job, or the registration.json apply wrote")
        return options

    @property
    def video_formats(self) -> frozenset[str]:
        return self.formats - {"png"}


def _optional_path(value: Any) -> Path | None:
    return None if value in (None, "") else Path(value)


def _parse_padding(value: Any) -> tuple[int, int, int, int] | None:
    """``L,T,R,B`` whole source px (register_clip apply --action-padding), or None."""
    if value in (None, ""):
        return None
    parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
    try:
        numbers = [int(str(part).strip()) for part in parts]
    except ValueError:
        raise ValueError(f"action-padding must be L,T,R,B whole px, got {forge_core.ascii_text(str(value))!r}") from None
    if len(numbers) != 4 or min(numbers) < 0:
        raise ValueError(f"action-padding must be four whole px >= 0 (L,T,R,B), got {numbers}")
    return tuple(numbers)  # type: ignore[return-value]


def _crf(value: Any, maximum: int, flag: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{flag} must be a whole number from 0 to {maximum}")
    return value


def _parse_shadow(text: Any) -> dict | None:
    if text in (None, ""):
        return None
    try:
        values = [float(v) for v in str(text).split(",")]
    except ValueError:
        raise ValueError("shadow must be RX,RY or RX,RY,OPACITY") from None
    if len(values) not in (2, 3) or not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError("shadow must be RX,RY or RX,RY,OPACITY with values >= 0")
    if len(values) == 3 and values[2] > 1:
        raise ValueError("shadow opacity must be 0..1")
    return dict(zip(("rx", "ry", "opacity"), values))


# --------------------------------------------------------------------------- timeline

class Timeline(NamedTuple):
    """Playback order of source frames with whole-millisecond durations.

    ``indices`` are 0-based positions in the clean-dir listing (repeats are holds);
    ``durations`` the integer milliseconds per entry; ``rate`` the exact constant frame rate of
    the video transports; ``seconds`` the exact clip length; ``constant`` whether frames are
    evenly spaced at ``rate`` (otherwise ``durations`` alone place them).
    """
    indices: tuple[int, ...]
    durations: tuple[int, ...]
    rate: Fraction
    seconds: Fraction
    constant: bool = True

    @property
    def total_ms(self) -> int:
        return sum(self.durations)

    @property
    def uniform(self) -> bool:
        """Durations differ by at most 1 ms, so one constant-rate video can carry them."""
        return max(self.durations) - min(self.durations) <= 1

    def starts(self) -> list[Fraction]:
        """Exact start time of each entry in seconds."""
        if self.constant:
            return [Fraction(i) / self.rate for i in range(len(self.indices))]
        edges, total = [], 0
        for duration in self.durations:
            edges.append(Fraction(total, 1000))
            total += duration
        return edges

    def frame_at(self, at_ms: int) -> int:
        """0-based entry shown at ``at_ms`` by a runtime that plays ``durations``."""
        edges = np.cumsum((0,) + self.durations[:-1])
        return int(np.searchsorted(edges, at_ms, side="right") - 1)


def constant_timeline(indices: Sequence[int], fps: Fraction) -> Timeline:
    """Evenly spaced frames at ``fps``; durations are frame_durations of the rounded length."""
    count = len(indices)
    seconds = Fraction(count) / fps
    total = max(count, round_half_up(seconds * 1000))
    return Timeline(tuple(indices), tuple(forge_core.frame_durations(total, count)), fps, seconds)


def duration_timeline(indices: Sequence[int], durations: Sequence[int], declared: Fraction | None = None) -> Timeline:
    """Frames with explicit durations; a declared fps is kept when the durations are its rounding."""
    count = len(indices)
    if declared is not None:
        expected = forge_core.frame_durations(max(count, round_half_up(Fraction(count) * 1000 / declared)), count)
        if list(durations) == expected:
            return Timeline(tuple(indices), tuple(durations), declared, Fraction(count) / declared)
    total = sum(durations)
    return Timeline(tuple(indices), tuple(durations), Fraction(1000 * count, total), Fraction(total, 1000),
                    constant=max(durations) - min(durations) <= 1)


def cap_timeline(timeline: Timeline, max_fps: Fraction) -> tuple[Timeline, list[int]]:
    """At most ``max_fps`` frames per second without changing the clip length.

    Returns the new timeline and, per new entry, its position in ``timeline``: the entry whose
    start is nearest to the new frame's start (ties go to the later frame, as in forge_av).
    """
    count = len(timeline.indices)
    if timeline.rate <= max_fps:
        return timeline, list(range(count))
    new_count = max(1, math.floor(timeline.seconds * max_fps))
    rate = Fraction(new_count) / timeline.seconds
    starts = timeline.starts()
    positions = []
    for k in range(new_count):
        t = Fraction(k) / rate
        i = max(0, bisect.bisect_right(starts, t) - 1)
        if i + 1 < count and starts[i + 1] - t <= t - starts[i]:
            i += 1
        positions.append(i)
    total = max(new_count, round_half_up(timeline.seconds * 1000))
    capped = Timeline(tuple(timeline.indices[p] for p in positions),
                      tuple(forge_core.frame_durations(total, new_count)), rate, timeline.seconds)
    return capped, positions


def bake_pingpong(timeline: Timeline) -> Timeline:
    """Forward then back without repeating the end frames (0 1 2 3 2 1), as one cycle."""
    count = len(timeline.indices)
    if count < 3:
        return timeline
    order = list(range(count)) + list(range(count - 2, 0, -1))
    indices = [timeline.indices[i] for i in order]
    if timeline.constant and timeline.uniform:
        return constant_timeline(indices, timeline.rate)
    return duration_timeline(indices, [timeline.durations[i] for i in order])


def expand_ticks(timeline: Timeline, rates: Iterable[Fraction]) -> tuple[Timeline, dict] | None:
    """Uneven whole-tick durations as repeated frames at one constant rate, for video transports (D21).

    A video plays one constant rate, so a frame that lasts ``k`` ticks becomes ``k`` copies of the
    frame at the tick rate. The first rate in ``rates`` (at most 60 fps) whose tick grid holds every
    authored frame edge within TICK_EDGE_TOLERANCE_MS (1 ms), and keeps the clip length to the
    millisecond, wins. Returns the expanded constant timeline and a record of the expansion
    (``rate``, ``ticks`` per authored frame, the authored ``sourceIndices`` and ``durationsMs``), or
    None when no rate fits (durations that are not whole ticks).
    """
    edges = list(itertools.accumulate(timeline.durations, initial=0))
    tried = []
    for rate in rates:
        if rate in tried or not 0 < rate <= MAX_FPS:
            continue
        tried.append(rate)
        marks = [round_half_up(Fraction(edge) * rate / 1000) for edge in edges]
        ticks = [b - a for a, b in zip(marks, marks[1:])]
        if min(ticks) < 1:
            continue
        indices = [index for index, count in zip(timeline.indices, ticks) for _ in range(count)]
        expanded = constant_timeline(indices, rate)
        if expanded.total_ms != edges[-1]:
            continue
        grid = list(itertools.accumulate(expanded.durations, initial=0))
        if any(abs(grid[mark] - edge) > TICK_EDGE_TOLERANCE_MS for mark, edge in zip(marks, edges)):
            continue
        return expanded, {"rate": rational(rate), "ticks": ticks, "authoredSourceIndices": list(timeline.indices),
                          "authoredDurationsMs": list(timeline.durations)}
    return None


# --------------------------------------------------------------------------- input documents

class _FrameHashes:
    """Lazy sha256 per clean-dir frame (selections only hash the frames they bind)."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self.paths, self._cache = list(paths), {}

    def __call__(self, index: int) -> str:
        if index not in self._cache:
            self._cache[index] = forge_core.sha256_file(self.paths[index])
        return self._cache[index]


def _event_list(raw: Any, total_ms: int, label: str) -> list[dict]:
    """Gameplay events in whole ms with ``0 <= atMs <= total_ms``: the end edge is allowed (D19), so an
    ``end`` event at the last tick edge (retime --ticks) names the last frame. impactMs/holdMs stay strict."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{label} events must be a list")
    events = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{label} events must be objects with name and atMs")
        name = item.get("name")
        if not isinstance(name, str) or not (name in EVENT_NAMES or re.fullmatch(r"custom:\S+", name)):
            raise ValueError(f"{label} event name {forge_core.ascii_text(str(name))!r} is not a known event "
                             "(in, tell, hit, active_end, cancel, chain, impact, hold, end, sfx, step_l, "
                             "step_r or custom:<name>)")
        at = _whole(item.get("atMs"), f"{label} event {name} atMs")
        if at > total_ms:
            raise ValueError(f"{label} event {name} at {at} ms lies outside the {total_ms} ms clip (events may sit "
                             f"at 0..{total_ms} ms, the end edge included)")
        event = {"name": name, "atMs": at}
        if "data" in item:
            event["data"] = item["data"]
        events.append(event)
    return events


def _instant(value: Any, total_ms: int, label: str) -> int | None:
    if value is None:
        return None
    at = _whole(value, label)
    if at >= total_ms:
        raise ValueError(f"{label} {at} ms lies outside the {total_ms} ms clip")
    return at


def load_selection(path: Path, count: int, frame_hash: Any, declared: Fraction | None) -> dict:
    """Read a forge-frame-selection v1/v2 bound by sha256 to the clean-dir frames.

    v1: ``[start, endExclusive)`` at ``fps``. v2: 0-based ``sourceIndices`` (repeats hold) with
    ``durations_ms``, ``loopPolicy``, ``events`` and optional ``impactMs``/``holdMs``/``cadenceMs``/
    ``strideWorldUnits``. ``sourceHashes`` hold one sha256 per frame of ``[start, endExclusive)``
    (a per-index list is accepted too). A mismatch means the selection is stale and is refused.
    """
    data = _read_object(path, "selection")
    schema = data.get("schema")
    if schema not in (SELECTION_V1, SELECTION_V2):
        raise ValueError(f"selection schema must be {SELECTION_V1} or {SELECTION_V2}")
    status = str(data.get("status", ""))
    if re.match(r"(?i)(reject|fail|unusable)", status):
        raise QualityGateError(f"selection status is {forge_core.ascii_text(status)!r}; choose another range")
    start = _whole(data.get("start"), "selection start")
    end = _whole(data.get("endExclusive"), "selection endExclusive", 1)
    if not start < end <= count:
        raise ValueError(f"selection [{start}, {end}) does not fit the {count} frames in clean-dir")
    hashes = data.get("sourceHashes")
    if not isinstance(hashes, list) or not hashes or not all(isinstance(h, str) and _SHA256.fullmatch(h)
                                                             for h in hashes):
        raise ValueError("selection sourceHashes must be a list of lowercase sha256 digests")
    fps = data.get("fps")
    rate = parse_rate(fps, "selection fps") if fps is not None else None
    if declared is not None and rate is not None and declared != rate:
        raise ValueError(f"--fps {rational(declared)} conflicts with the selection fps {rational(rate)}")
    rate = rate or declared
    if schema == SELECTION_V1:
        if rate is None:
            raise ValueError("a v1 selection needs fps")
        indices = list(range(start, end))
        timeline = constant_timeline(indices, rate)
        policy, raw_events = None, []
    else:
        indices = data.get("sourceIndices")
        durations = data.get("durations_ms")
        if not isinstance(indices, list) or not indices:
            raise ValueError("selection sourceIndices must be a non-empty list")
        for index in indices:
            if _whole(index, "selection sourceIndices entry") < start or index >= end:
                raise ValueError(f"selection sourceIndices entry {index} lies outside [{start}, {end})")
        if not isinstance(durations, list) or len(durations) != len(indices):
            raise ValueError("selection durations_ms needs one whole-millisecond duration per sourceIndices entry")
        for duration in durations:
            _whole(duration, "selection durations_ms entry", 1)
        policy = data.get("loopPolicy")
        if policy not in LOOP_POLICIES:
            raise ValueError(f"selection loopPolicy must be one of {', '.join(LOOP_POLICIES)}")
        timeline = duration_timeline(indices, durations, rate)
        if declared is not None and timeline.rate != declared:
            raise ValueError(f"--fps {rational(declared)} does not match the selection durations")
        raw_events = data.get("events")
        if not isinstance(raw_events, list):
            raise ValueError("a v2 selection needs an events list (it may be empty)")
    span = [frame_hash(i) for i in range(start, end)]
    if hashes != span and not (schema == SELECTION_V2 and hashes == [frame_hash(i) for i in indices]):
        if len(hashes) == len(span):
            where = f"first difference at frame {next(start + i for i, (a, b) in enumerate(zip(hashes, span)) if a != b)}"
        else:
            where = f"{len(hashes)} hashes for the {len(span)} frames of [{start}, {end})"
        raise ValueError(f"selection sourceHashes no longer match the clean-dir frames (stale selection); {where}; "
                         "reselect or package the original frames")
    total, digest_sha = timeline.total_ms, forge_core.sha256_file(path)
    extras = {}
    for key in ("cadenceMs", "strideWorldUnits", "speedRef", "cycles"):
        if data.get(key) is not None:
            value = _finite(data[key], f"selection {key}", positive=True)
            extras[key] = value if isinstance(value, int) else float(value)  # a whole cycle count stays an integer
    tick_hz = None
    if schema == SELECTION_V2 and data.get("ticks") is not None:
        ticks = data["ticks"]
        if not isinstance(ticks, list) or len(ticks) != len(timeline.indices):
            raise ValueError("selection ticks need one whole tick count per sourceIndices entry")
        for tick in ticks:
            _whole(tick, "selection ticks entry", 1)
        tick_hz = _whole(data.get("tickHz", DEFAULT_TICK_HZ), "selection tickHz", 1)
    return {
        "schema": schema, "sha256": digest_sha, "timeline": timeline, "loopPolicy": policy,
        "events": _event_list(raw_events, total, "selection"),
        "impactMs": _instant(data.get("impactMs"), total, "selection impactMs"),
        "holdMs": _instant(data.get("holdMs"), total, "selection holdMs"),
        "extras": extras, "terminal": data.get("terminal") is True, "fps": rate, "tickHz": tick_hz,
        "summary": {"schema": schema, "start": start, "endExclusive": end, "status": status or None,
                    "method": data.get("method"), "loopPolicy": policy, "sha256": digest_sha},
    }


def _point(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must be [x, y]")
    return [float(_finite(v, label)) for v in value]


def _size(value: Any, label: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must be [width, height]")
    return [_whole(v, label, 1) for v in value]


def _padding(value: Any, label: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{label} must be [left, top, right, bottom]")
    return [_whole(v, label) for v in value]


def _job_base(data: Mapping) -> tuple[list[int], list[float]]:
    """The base canvas of a registration_job.v1: the job's ``sourceSize``/``sourceAnchor`` (D21), else the
    master size and anchor, cut to ``master.viewBox`` for one view of a sheet (register_clip load_job)."""
    master = data.get("master")
    if not isinstance(master, dict):
        raise ValueError("registration job needs master {size, anchor}")
    size, anchor = _size(master.get("size"), "master.size"), _point(master.get("anchor"), "master.anchor")
    box = master.get("viewBox")
    if box is not None:
        whole = isinstance(box, list) and len(box) == 4 and all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in box)
        if not (whole and box[0] < box[2] <= size[0] and box[1] < box[3] <= size[1]):
            raise ValueError(f"registration job master.viewBox must be an integer box inside master.size, got {box!r}")
        size, anchor = [box[2] - box[0], box[3] - box[1]], [anchor[0] - box[0], anchor[1] - box[1]]
    if data.get("sourceSize") is not None:
        stated = _size(data["sourceSize"], "registration job sourceSize")
        if stated != size:
            raise ValueError(f"registration job sourceSize {stated} disagrees with master.size/viewBox {size}")
    if data.get("sourceAnchor") is not None:
        stated_anchor = _point(data["sourceAnchor"], "registration job sourceAnchor")
        if max(abs(a - b) for a, b in zip(stated_anchor, anchor)) > 1e-6:
            raise ValueError(f"registration job sourceAnchor {stated_anchor} disagrees with master.anchor/viewBox "
                             f"{anchor}")
        anchor = stated_anchor
    return size, anchor


def load_registration(path: Path, action_padding: Sequence[int] | None = None) -> dict:
    """The 3.0 registration block from a registration job or a register_clip registration record.

    A ``video2dsprite.registration_job.v1`` means registration by construction: the job's base canvas
    (its ``sourceSize``/``sourceAnchor``; else ``master.size``/``anchor``, cut to ``master.viewBox``) plus
    ``padding`` [left, top, right, bottom] is the source canvas, and the base anchor moves by (left, top).
    ``action_padding`` replaces the job's padding exactly as ``register_clip.py apply --action-padding``
    did (D21). Any other document must state ``mode`` (construction, fixed-envelope, preserved); its
    ``jobSha256``, ``padding``, ``baseSize``/``baseAnchor`` (or ``master.size``/``anchor``), ``sourceSize``
    and ``sourceAnchor`` are taken when present (register_clip's registration.json is the best input: it
    records the padding apply used).
    """
    data = _read_object(path, "registration")
    sha = forge_core.sha256_file(path)
    override = None if action_padding is None else _padding(list(action_padding), "--action-padding")
    if data.get("schema") == REGISTRATION_JOB:
        base_size, base_anchor = _job_base(data)
        padding = override or _padding(data.get("padding"), "registration padding")
        block = {"mode": "construction", "jobSha256": sha, "baseSize": base_size, "baseAnchor": base_anchor,
                 "padding": padding,
                 "sourceSize": [base_size[0] + padding[0] + padding[2], base_size[1] + padding[1] + padding[3]],
                 "sourceAnchor": [base_anchor[0] + padding[0], base_anchor[1] + padding[1]]}
        if isinstance(data.get("action"), str):
            block["action"] = data["action"]
        return block
    mode = data.get("mode")
    if mode not in REGISTRATION_MODES:
        raise ValueError(f"--registration needs a {REGISTRATION_JOB} document or a registration record with "
                         f"mode {'|'.join(REGISTRATION_MODES)}")
    if override is not None and (data.get("padding") is None or
                                 _padding(data["padding"], "registration padding") != override):
        raise ValueError(f"--action-padding {override} conflicts with the registration record (padding "
                         f"{data.get('padding')}); a registration.json already records the padding apply used")
    block: dict[str, Any] = {"mode": mode, "registrationSha256": sha}
    job = data.get("jobSha256")
    if job is not None:
        if not isinstance(job, str) or not _SHA256.fullmatch(job):
            raise ValueError("registration jobSha256 must be a lowercase sha256")
        block["jobSha256"] = job
    master = data.get("master") if isinstance(data.get("master"), dict) else {}
    base_size = data.get("baseSize", master.get("size"))
    base_anchor = data.get("baseAnchor", master.get("anchor"))
    if base_size is not None:
        block["baseSize"] = _size(base_size, "registration baseSize")
    if base_anchor is not None:
        block["baseAnchor"] = _point(base_anchor, "registration baseAnchor")
    if data.get("padding") is not None:
        block["padding"] = _padding(data["padding"], "registration padding")
    for key, parse in (("sourceSize", _size), ("sourceAnchor", _point)):
        if data.get(key) is not None:
            block[key] = parse(data[key], f"registration {key}")
    if "baseSize" in block and "padding" in block and "sourceSize" not in block:
        left, top, right, bottom = block["padding"]
        block["sourceSize"] = [block["baseSize"][0] + left + right, block["baseSize"][1] + top + bottom]
    if "baseAnchor" in block and "padding" in block and "sourceAnchor" not in block:
        block["sourceAnchor"] = [block["baseAnchor"][0] + block["padding"][0],
                                 block["baseAnchor"][1] + block["padding"][1]]
    return block


def load_review(path: Path, bindings: Mapping[str, str]) -> dict:
    """A review_verdict.v1 bound to what is packaged (``reviewedSha256`` must be one of ``bindings``).

    ``fail_regenerate`` refuses the package. A verdict for other bytes is stale and refused.
    """
    data = _read_object(path, "review verdict")
    if data.get("schema") != REVIEW_VERDICT:
        raise ValueError(f"review verdict schema must be {REVIEW_VERDICT}")
    status = data.get("status")
    if status not in REVIEW_STATUSES:
        raise ValueError(f"review status must be one of {', '.join(REVIEW_STATUSES)}")
    issues = data.get("issues")
    if not isinstance(issues, list) or not all(isinstance(i, dict) and isinstance(i.get("desc"), str)
                                               and i["desc"].strip() for i in issues):
        raise ValueError("review issues must be a list of {frame?, box?, desc}")
    if status != "accepted" and not issues:
        raise ValueError(f"a {status} verdict must name at least one issue")
    reviewed = data.get("reviewedSha256")
    if not isinstance(reviewed, str) or not _SHA256.fullmatch(reviewed):
        raise ValueError("review reviewedSha256 must be a lowercase sha256")
    if status == "fail_regenerate":
        first = forge_core.ascii_text(issues[0]["desc"])
        raise QualityGateError(f"review verdict is fail_regenerate ({first}); regenerate the clip instead of "
                               "packaging it")
    bound = [name for name, sha in bindings.items() if sha == reviewed]
    if not bound:
        expected = ", ".join(f"{name} {sha}" for name, sha in bindings.items())
        raise ValueError(f"review reviewedSha256 matches nothing being packaged (stale review); expected {expected}")
    return {"status": status, "reviewedSha256": reviewed, "boundTo": bound[0], "issues": len(issues),
            "sha256": forge_core.sha256_file(path)}


class KeyChoice(NamedTuple):
    """The residue gate's key (None skips the gate), where it came from (keySource) and the document."""
    key: tuple[int, int, int] | None
    source: str
    document: Path | None = None


def resolve_key(option: str, pipeline_meta: Path | None = None, *, clean_dir: Path | None = None,
                registration: Path | None = None) -> KeyChoice:
    """Key colour for the residue gate and where it came from (D17).

    ``none`` skips the gate (native-alpha footage); a key name, ``#rrggbb`` or an RGB triple is used as
    given (keySource ``flag``). ``auto`` takes the first of:

    1. ``<clean_dir>/matte-report.json`` (video2dsprite clean/process): its ``key`` (the clip key the
       matte used); mode ``none`` skips the gate (keySource ``matte-report``);
    2. the ``registration`` document's ``keyColor`` (register_clip registration.json or the job; for
       registered frames, whose folder holds no matte report) (``registration``);
    3. ``pipeline_meta`` ``matte.key``; mode ``none`` skips (``pipeline-meta``);
    4. magenta (``default``).

    A ``matte-report.json`` in ``clean_dir`` that is not a video2dsprite.matte_report.v1 document is an
    error: the gate would otherwise measure the wrong key silently.
    """
    text = option.strip().lower()
    if text == "none":
        return KeyChoice(None, "flag")
    if text != "auto":
        return KeyChoice(_key_rgb(text, "--key"), "flag")
    report = None if clean_dir is None else Path(clean_dir) / MATTE_REPORT_FILE
    if report is not None and report.is_file():
        data = _read_object(report, MATTE_REPORT_FILE)
        if data.get("schema") != MATTE_REPORT_SCHEMA:
            raise ValueError(f"{MATTE_REPORT_FILE} in clean-dir is not a {MATTE_REPORT_SCHEMA} document; pass --key "
                             "with the key the frames were keyed against")
        if data.get("mode") == "none":
            return KeyChoice(None, "matte-report", report)
        if data.get("key") is None:
            raise ValueError(f"{MATTE_REPORT_FILE} in clean-dir names no key; pass --key")
        return KeyChoice(_key_rgb(data["key"], f"{MATTE_REPORT_FILE} key"), "matte-report", report)
    if registration is not None:
        key_color = _read_object(registration, "registration").get("keyColor")
        if key_color is not None:
            return KeyChoice(_key_rgb(key_color, "registration keyColor"), "registration", registration)
    if pipeline_meta is not None:
        matte = _read_object(pipeline_meta, "pipeline-meta").get("matte")
        if isinstance(matte, dict):
            if matte.get("mode") == "none":
                return KeyChoice(None, "pipeline-meta", pipeline_meta)
            if matte.get("key") is not None:
                return KeyChoice(_key_rgb(matte["key"], "pipeline-meta matte.key"), "pipeline-meta", pipeline_meta)
    return KeyChoice(KEY_NAMES["magenta"], "default")


def _key_rgb(value: Any, label: str) -> tuple[int, int, int]:
    if isinstance(value, str):
        name = value.strip().lower()
        if name in KEY_NAMES:
            return KEY_NAMES[name]
        match = _HEX.fullmatch(name)
        if match:
            digits = match.group(1)
            return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        channels = [round_half_up(_finite(v, label)) for v in value]
        if all(0 <= c <= 255 for c in channels):
            return tuple(channels)  # type: ignore[return-value]
    raise ValueError(f"{label} must be magenta, green, blue, #rrggbb, an RGB triple or none")


# --------------------------------------------------------------------------- analysis

def analyze(frames: list[Image.Image], fps: float, loop: bool, *, key: Any = "magenta") -> dict:
    """Report diagnostics, never certify contact or identity from image bounds.

    The cfed170 bounds and seam diagnostics are unchanged. ``keyResidue`` adds matte_qa
    over every distinct frame (opaque key pixels, outer-ring spill, semitransparency, pockets);
    ``key=None`` skips it for footage that was never keyed. Pass or fail is the caller's gate.
    """
    boxes = [bbox(im) for im in frames]
    valid = [b for b in boxes if b]
    w, h = frames[0].size

    def premult(im):
        a = np.asarray(im.resize((min(w, 128), min(h, 128))), dtype=np.float32) / 255
        a[..., :3] *= a[..., 3:4]
        return a

    errors = []
    first = premult(frames[0])
    previous = first
    for im in frames[1:]:
        current = premult(im)
        errors.append(float(np.abs(current - previous).mean()))
        previous = current
    seam = float(np.abs(previous - first).mean())
    median = float(np.median(errors)) if errors else 0
    report = {
        "status": "needs-visual-review", "loopRequested": loop,
        "frameCount": len(frames), "durationSeconds": len(frames) / fps,
        "blankFrames": [i for i, b in enumerate(boxes) if not b],
        "edgeTouchFrames": [i for i, b in enumerate(boxes) if b and
                            (b[0] == 0 or b[1] == 0 or b[2] == w or b[3] == h)],
        "boundsBottomSpanPx": max(b[3] for b in valid) - min(b[3] for b in valid) if valid else None,
        "boundsCenterXSpanPx": max((b[0]+b[2])/2 for b in valid) - min((b[0]+b[2])/2 for b in valid) if valid else None,
        "seamPremultipliedMAE": round(seam, 6),
        "adjacentMedianMAE": round(median, 6),
        "seamToMedianRatio": round(seam/median, 3) if median > 1e-8 else None,
        "adjacentMaxMAE": round(max(errors, default=0), 6),
        "notes": ["Bounds bottom is a contact-drift proxy, not a tracked foot/root.",
                  "A similar first/last frame does not prove a seamless cycle or stable identity.",
                  "Airborne poses may legitimately change bottom position; review at runtime scale."],
    }
    if key is not None:
        report["keyResidue"] = key_residue(frames, key)
    return report


def key_residue(frames: Sequence[Any], key: Any) -> dict:
    """forge_matte.matte_qa over every distinct frame, aggregated for the residue gate (report v2 P0-3).

    Each frame is measured on a copy with alpha <= RESIDUE_ALPHA_FLOOR (16) cleared (D18): invisible
    resampling halo is not key residue, a visible key fringe is. Pixel counts are sums over distinct
    frames; fractions are the worst frame; frame lists are 0-based clip positions.
    """
    key_rgb = _key_rgb(key, "key") if not isinstance(key, tuple) else key
    measured: dict[int, dict] = {}
    per_position = []
    for frame in frames:
        qa = measured.get(id(frame))
        if qa is None:
            qa = measured[id(frame)] = forge_matte.matte_qa(_residue_view(frame), key_rgb)
        per_position.append(qa)
    distinct = list(measured.values())
    spill = [q["outer_ring_spill_fraction"] for q in per_position]
    semi = [q["semitransparent_fraction"] for q in per_position]
    return {
        "key": list(key_rgb),
        "framesMeasured": len(distinct),
        "opaqueKeyPx": int(sum(q["opaque_key_px"] for q in distinct)),
        "framesWithOpaqueKey": [i for i, q in enumerate(per_position) if q["opaque_key_px"] > 0],
        "maxOuterRingSpillFraction": round(max(spill), 6),
        "framesOverRingSpill": [i for i, v in enumerate(spill) if v > RESIDUE_LIMITS["outerRingSpillFraction"]],
        "maxSemitransparentFraction": round(max(semi), 6),
        "minSemitransparentFraction": round(min(semi), 6),
        "enclosedKeyPockets": int(sum(q["enclosed_key_pockets"] for q in distinct)),
        "keyHuedPx": int(sum(q["key_hued_px"] for q in distinct)),
        "limits": dict(RESIDUE_LIMITS),
        "thresholds": {**distinct[0]["thresholds"], "alpha_floor": RESIDUE_ALPHA_FLOOR},
        "method": (f"{distinct[0]['method']}; each frame measured on a copy with alpha <= {RESIDUE_ALPHA_FLOOR} "
                   "cleared, so invisible resampling halo is not counted (D18)"),
    }


def _visible_crop(frame: Any) -> np.ndarray:
    """The frame cropped to its alpha > 0 box. matte_qa reads only visible pixels and their
    transparent or off-canvas neighbours, so the crop gives identical numbers, faster."""
    pixels = np.asarray(frame)
    box = forge_core.subject_bbox(pixels, 0)
    if box is None:
        return np.zeros((1, 1, 4), np.uint8)
    return pixels[box[1]:box[3], box[0]:box[2]]


def _residue_view(frame: Any, floor: int = RESIDUE_ALPHA_FLOOR) -> np.ndarray:
    """What the residue gate measures (D18): the frame with alpha <= ``floor`` cleared, cropped to the
    box of what is left (matte_qa gives the same numbers on that crop as on the whole cleared frame)."""
    pixels = np.asarray(frame)
    box = forge_core.subject_bbox(pixels, floor)
    if box is None:
        return np.zeros((1, 1, 4), np.uint8)
    crop = pixels[box[1]:box[3], box[0]:box[2]].copy()
    crop[crop[..., 3] <= floor] = 0
    return crop


def residue_checks(residue: Mapping | None, allow: bool) -> list[dict]:
    """QA checks of the key-residue gate; failures become warn when the override is recorded."""
    if residue is None:
        return [{"id": name, "status": "skipped", "value": None, "threshold": None}
                for name in ("opaque_key_px", "outer_ring_spill_fraction", "semitransparent_fraction",
                             "enclosed_key_pockets")]
    failed = "warn" if allow else "fail"
    return [
        {"id": "opaque_key_px", "value": residue["opaqueKeyPx"], "threshold": RESIDUE_LIMITS["opaqueKeyPx"],
         "status": failed if residue["opaqueKeyPx"] > RESIDUE_LIMITS["opaqueKeyPx"] else "pass"},
        {"id": "outer_ring_spill_fraction", "value": residue["maxOuterRingSpillFraction"],
         "threshold": RESIDUE_LIMITS["outerRingSpillFraction"],
         "status": failed if residue["maxOuterRingSpillFraction"] > RESIDUE_LIMITS["outerRingSpillFraction"]
         else "pass"},
        {"id": "semitransparent_fraction", "status": "pass", "value": residue["maxSemitransparentFraction"],
         "threshold": None},
        {"id": "enclosed_key_pockets", "value": residue["enclosedKeyPockets"], "threshold": 0,
         "status": "warn" if residue["enclosedKeyPockets"] else "pass"},
    ]


# --------------------------------------------------------------------------- geometry

def prepare(frames: list[Image.Image], source_size: tuple, source_anchor: tuple,
            max_side: int, crop_union: bool, *, resampler: str = "lanczos") -> tuple[list[Image.Image], dict]:
    """One crop and one resize for every frame; even right/bottom transparent padding.

    The crop is the full input canvas, or with ``crop_union`` one envelope over every nonzero
    alpha pixel of the clip (never per frame). The resize keeps the longest side at most
    ``max_side`` (never upscales) on premultiplied channels (forge_core.resample_rgba);
    ``nearest`` only reduces by whole factors, for pixel art. Returns the frames and the 2.0
    geometry (sourceSize, sourceAnchor, inputFrameSize, sourceRect, encodedSize, contentSize,
    encodedAnchor, registration).
    """
    if not frames or not 2 <= max_side <= 4096 or min(source_size) <= 0:
        raise ValueError("nonempty frames, positive source size and max-side in 2..4096 required")
    input_size = frames[0].size
    if any(im.size != input_size for im in frames):
        raise ValueError("mixed source canvas dimensions are not supported")
    if abs(source_size[0]/source_size[1] - input_size[0]/input_size[1]) > 0.01:
        raise ValueError("source-size aspect differs from input canvas; register generated footage to the approved art canvas first")
    if not (0 <= source_anchor[0] <= source_size[0] and 0 <= source_anchor[1] <= source_size[1]):
        raise ValueError("source-anchor must be inside source-size")
    # Crop geometry includes every nonzero-alpha pixel, including faint FX.
    # Diagnostic body bounds use a threshold, but must never trim actual art.
    boxes = [bb for im in frames if (bb := im.getchannel("A").getbbox())]
    if not boxes:
        raise ValueError("all frames are transparent")
    rect = (0, 0, *input_size)
    if crop_union:
        # One envelope over the entire clip, never per-frame cropping.
        rect = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
    w, h = rect[2]-rect[0], rect[3]-rect[1]
    if resampler == "nearest":
        factor = max(1, math.ceil(max(w, h) / max_side))
        if w % factor or h % factor:
            raise ValueError(f"nearest (pixel-art) resampling reduces only by whole factors: {w}x{h} does not "
                             f"divide by {factor}; raise --max-side to {max(w, h)} or pre-scale the frames")
        scale, cw, ch = 1 / factor, w // factor, h // factor
    else:
        scale = min(1, max_side/max(w, h))
        cw, ch = max(1, round_half_up(w*scale)), max(1, round_half_up(h*scale))
    ew, eh = (cw+1)//2*2, (ch+1)//2*2
    prepared, cache = [], {}
    for im in frames:
        if id(im) not in cache:
            crop = im.crop(rect)
            if crop.size != (cw, ch):
                crop = forge_core.resample_rgba(crop, scale, resampler, out_size=(cw, ch))
            canvas = Image.new("RGBA", (ew, eh))
            # Plain paste copies straight alpha, unlike paste(image, mask=image).
            canvas.paste(crop, (0, 0))
            cache[id(im)] = canvas
        prepared.append(cache[id(im)])
    sx, sy = source_size[0]/input_size[0], source_size[1]/input_size[1]
    source_rect = [rect[0]*sx, rect[1]*sy, w*sx, h*sy]
    return prepared, {
        "sourceSize": list(source_size), "sourceAnchor": list(source_anchor),
        "inputFrameSize": list(input_size),
        "sourceRect": source_rect, "sourceRectFormat": "x,y,width,height",
        "encodedSize": [ew, eh], "contentSize": [cw, ch],
        "encodedAnchor": [(source_anchor[0]-source_rect[0])*cw/source_rect[2],
                          (source_anchor[1]-source_rect[1])*ch/source_rect[3]],
        "registration": "fixed-union-envelope" if crop_union else "preserved-source-canvas",
    }


def plan_tier(spec: TierSpec, geometry: Mapping, timeline: Timeline, name: str, *,
              resampler: str = "lanczos") -> dict:
    """Pixel size, packed halves and timing of one mobile tier; source geometry is untouched.

    The tier is the main content (``contentSize``) scaled so its long edge is at most
    ``spec.edge`` (never upscaled) and resampled in time to at most ``spec.fps`` without changing
    the clip length. Runtimes map tier pixels onto ``sourceRect`` exactly like the main media:
    ``encodedAnchor`` here is the source anchor in tier pixels.
    """
    cw, ch = geometry["contentSize"]
    if resampler == "nearest":
        factor = max(1, math.ceil(max(cw, ch) / spec.edge))
        if cw % factor or ch % factor:
            raise ValueError(f"tier {spec.name}: nearest (pixel-art) tiers reduce only by whole factors and "
                             f"{cw}x{ch} does not divide by {factor}")
        width, height = cw // factor, ch // factor
    else:
        ratio = min(Fraction(1), Fraction(spec.edge, max(cw, ch)))
        width, height = max(1, round_half_up(cw * ratio)), max(1, round_half_up(ch * ratio))
    packed = forge_av.packed_geometry(width, height)
    tier_timeline, positions = cap_timeline(timeline, spec.fps)
    ax, ay = geometry["encodedAnchor"]
    return {
        "tier": spec.name, "sourceWidth": cw, "sourceHeight": ch, "width": width, "height": height,
        "halfWidth": packed["halfWidth"], "halfHeight": packed["halfHeight"], "encodedSize": packed["encodedSize"],
        "layout": packed["layout"], "encodedAnchor": [ax * width / cw, ay * height / ch],
        "maxLongEdge": spec.edge, "maxFps": rational(spec.fps), "fps": rational(tier_timeline.rate),
        "frameCount": len(positions), "durationMs": float(tier_timeline.seconds * 1000),
        "inputIndices": positions, "resampler": resampler, "file": f"{name}-packed-{spec.name}.mp4",
    }


def tier_frames(content: Mapping[int, np.ndarray], indices: Sequence[int], tier: Mapping) -> list[np.ndarray]:
    """Tier frames in tier order: ``tier["inputIndices"]`` are positions in the main timeline,
    ``indices`` the source index at each position and ``content`` the main content frame of each
    source index, resampled (premultiplied) to the tier size."""
    size = (tier["width"], tier["height"])
    cache: dict[int, np.ndarray] = {}
    frames = []
    for position in tier["inputIndices"]:
        index = indices[position]
        if index not in cache:
            frame = content[index]
            if (frame.shape[1], frame.shape[0]) == size:
                cache[index] = frame
            else:
                scale = size[0] / frame.shape[1]
                resampler = tier.get("resampler", "lanczos")
                cache[index] = np.asarray(forge_core.resample_rgba(frame, scale, resampler, out_size=size))
        frames.append(cache[index])
    return frames


# --------------------------------------------------------------------------- encoding

class Transport(NamedTuple):
    """What encode() writes: one frame per timeline entry plus the requested formats and tiers."""
    name: str
    timeline: Timeline
    even: Mapping[int, np.ndarray]      # source index -> encodedSize RGBA (webm, atlas)
    content: Mapping[int, np.ndarray]   # source index -> contentSize RGBA (packed, tiers)
    loop: bool
    formats: frozenset[str]
    tiers: tuple[dict, ...] = ()
    crf_webm: int = 28
    crf_packed: int = 18


_MEDIA_KEYS = ("file", "bytes", "sha256", "mimeType", "codec", "profile", "alpha", "pixFmt", "layout", "width",
               "height", "halfWidth", "halfHeight", "encodedSize", "requiresCompositor", "crf", "keyint",
               "keyframes", "frameCount", "fps", "durationMs")


def _decoded_frame_count(path: Path, alpha: str) -> int:
    return sum(1 for _ in forge_av.iter_rgba(path, alpha=alpha))


def _encoded(result: Mapping, path: Path, alpha: str, expected: int, **extra: Any) -> dict:
    """Manifest record of an encoded file after a complete decode (truncation check)."""
    decoded = _decoded_frame_count(path, alpha)
    if decoded != expected:
        raise QualityGateError(f"{path.name} decodes to {decoded} frames, expected {expected}")
    record = {key: result[key] for key in _MEDIA_KEYS if key in result}
    record.update(extra)
    record["fullDecodePassed"] = True
    return record


def encode(transport: Transport, stage: Path) -> dict:
    """Encode the requested video transports into ``stage`` and return their manifest records.

    package() always calls it (a PNG-only package gets ``{}``). Loops get one closed GOP per
    loop (keyint = frame count) so the wrap is a keyframe boundary; rates are exact rationals.
    Every file is decoded completely afterwards.
    """
    files: dict[str, Any] = {}
    timeline, name = transport.timeline, transport.name
    count, fps = len(timeline.indices), rational(timeline.rate)
    keyint = count if transport.loop else None
    if "webm" in transport.formats:
        path = stage / f"{name}.webm"
        result = forge_av.encode_vp9_alpha([transport.even[i] for i in timeline.indices], path, fps,
                                           crf=transport.crf_webm, keyint=keyint)
        files["webm"] = _encoded(result, path, "on", count, requiresAlphaPlaybackVerification=True)
    if "packed" in transport.formats:
        path = stage / f"{name}-packed.mp4"
        result = forge_av.encode_packed_alpha([transport.content[i] for i in timeline.indices], path, fps,
                                              crf=transport.crf_packed, keyint=keyint)
        files["packedAlpha"] = _encoded(result, path, "off", count, closedGop=True)
    records = []
    for tier in transport.tiers:
        frames = tier_frames(transport.content, timeline.indices, tier)
        path = stage / tier["file"]
        result = forge_av.encode_packed_alpha(frames, path, tier["fps"], crf=transport.crf_packed,
                                              keyint=len(frames) if transport.loop else None,
                                              max_fps=Fraction(tier["maxFps"]))
        planned = {k: v for k, v in tier.items() if k not in ("file", "fps", "frameCount", "durationMs")}
        records.append({**planned, **_encoded(result, path, "off", len(frames), closedGop=True)})
    if records:
        files["mobilePackedAlpha"] = records
    return files


# --------------------------------------------------------------------------- package

class _Clip(NamedTuple):
    timeline: Timeline
    loop_policy: str
    events: list[dict]
    impact_ms: int | None
    hold_ms: int | None
    extras: dict
    selection: dict | None
    pingpong_baked: bool
    capped: bool
    input_rate: Fraction
    terminal: bool
    tick_expansion: dict | None = None


def _tick_rates(selection: Mapping | None) -> list[Fraction]:
    """Rates whose ticks may hold uneven durations: the selection's tick grid (retime --ticks), its source
    fps (gait_loop's held frames), then the 60 Hz forge tick grid (D11)."""
    rates = []
    if selection is not None:
        if selection.get("tickHz"):
            rates.append(Fraction(selection["tickHz"]))
        if selection.get("fps"):
            rates.append(Fraction(selection["fps"]))
    return rates + [Fraction(DEFAULT_TICK_HZ)]


def _clip(opts: PackageOptions, paths: Sequence[Path], frame_hash: _FrameHashes) -> _Clip:
    """Timeline, loop policy and timing metadata from --fps or --selection."""
    selection = None
    if opts.selection is not None:
        selection = load_selection(opts.selection, len(paths), frame_hash, opts.fps)
        timeline = selection["timeline"]
        policy = selection["loopPolicy"] or opts.loop_policy or ("cycle" if opts.loop else "oneshot")
        if opts.loop_policy and opts.loop_policy != policy:
            raise ValueError(f"--loop-policy {opts.loop_policy} conflicts with the selection loopPolicy {policy}")
        events, impact, hold = selection["events"], selection["impactMs"], selection["holdMs"]
        extras, terminal = dict(selection["extras"]), selection["terminal"] or opts.terminal
    else:
        if opts.fps is None:
            raise ValueError("fps is required unless --selection provides the timing")
        timeline = constant_timeline(range(len(paths)), opts.fps)
        policy = opts.loop_policy or ("cycle" if opts.loop else "oneshot")
        events, impact, hold, extras, terminal = [], None, None, {}, opts.terminal
    if opts.loop and policy == "oneshot":
        raise ValueError("--loop conflicts with the oneshot loop policy")
    for key, flag, value in (("cadenceMs", "--cadence-ms", opts.cadence_ms),
                             ("strideWorldUnits", "--stride-world-units", opts.stride_world_units),
                             ("speedRef", "--speed-ref", opts.speed_ref), ("cycles", "--cycles", opts.cycles)):
        if value is not None:
            if key in extras and abs(extras[key] - value) > 1e-9:
                raise ValueError(f"{flag} {value:g} conflicts with the selection {key} {extras[key]:g}")
            extras[key] = value
    baked = policy == "pingpong" and len(timeline.indices) >= 3
    if baked:
        timeline, policy = bake_pingpong(timeline), "cycle"
    expansion = None
    if (opts.video_formats or opts.tiers) and not timeline.uniform:
        rates = _tick_rates(selection)
        expanded = expand_ticks(timeline, rates)
        if expanded is None:
            shown = ", ".join(sorted({rational(rate) for rate in rates if rate <= MAX_FPS}))
            raise ValueError(f"webm/packed transports play one constant frame rate, but these durations (from "
                             f"{min(timeline.durations)} to {max(timeline.durations)} ms) are not whole ticks of "
                             f"{shown} fps; package --formats png (the atlas keeps uneven durations), or retime "
                             "onto a tick grid (retime.py --ticks) so every frame lasts whole ticks")
        timeline, expansion = expanded
    input_rate = timeline.rate
    timeline, _ = cap_timeline(timeline, MAX_FPS)
    return _Clip(timeline, policy, events, impact, hold, extras, selection and selection["summary"], baked,
                 timeline.rate != input_rate, input_rate, terminal, expansion)


def _load_unique(paths: Sequence[Path], indices: Iterable[int]) -> dict[int, Image.Image]:
    unique = sorted(set(indices))
    pixels = 0
    for index in unique:
        with Image.open(paths[index]) as image:
            pixels += image.size[0] * image.size[1]
    if pixels > DECODED_PIXEL_BUDGET:
        raise ValueError("decoded frame budget exceeds 128 million pixels; trim the clip or extract at lower fps/resolution")
    return {index: forge_core.load_rgba(paths[index])[0] for index in unique}


def _geometry_inputs(opts: PackageOptions, frame_size: tuple[int, int],
                     registration: Mapping | None) -> tuple[tuple, tuple]:
    size = opts.source_size or tuple((registration or {}).get("sourceSize") or frame_size)
    anchor = opts.source_anchor or tuple((registration or {}).get("sourceAnchor") or (size[0] / 2, size[1]))
    if registration:
        for key, flag, given in (("sourceSize", "--source-size", opts.source_size),
                                 ("sourceAnchor", "--source-anchor", opts.source_anchor)):
            if given is not None and key in registration and \
                    any(abs(a - b) > 1e-6 for a, b in zip(given, registration[key])):
                raise ValueError(f"{flag} {list(given)} conflicts with the registration {key} {registration[key]}")
    return tuple(int(v) for v in size), tuple(float(v) for v in anchor)


def _qa_status(checks: Sequence[Mapping]) -> str:
    graded = [c["status"] for c in checks if c["status"] != "skipped"]
    return max(graded, key=QA_SEVERITY.__getitem__) if graded else "needs-visual-review"


def _atlas(prepared: Sequence[Image.Image], stage: Path, name: str) -> tuple[list[dict], tuple[int, int]]:
    """PNG fallback pages below 4096 px per side (2.0 layout: up to 8 columns, row-major)."""
    width, height = prepared[0].size
    cols = min(8, max(1, 4096//width))
    rows = max(1, 4096//height)
    page_frames = cols*rows
    pages = []
    for start in range(0, len(prepared), page_frames):
        chunk = prepared[start:start+page_frames]
        actual_cols = min(cols, len(chunk))
        sheet = Image.new("RGBA", (width*actual_cols, height*math.ceil(len(chunk)/actual_cols)))
        for i, im in enumerate(chunk):
            sheet.paste(im, ((i % actual_cols)*width, (i//actual_cols)*height))
        path = stage / f"{name}-atlas-{len(pages):02d}.png"
        forge_core.save_png(sheet, path)
        pages.append({**digest(path), "firstFrame": start, "frameCount": len(chunk), "columns": actual_cols})
    return pages, (width, height)


def package(args: argparse.Namespace) -> dict:
    """Package frames into a new output directory and return its animation.json 3.0 manifest.

    ``args`` is the ``package`` namespace of this CLI or of video2dsprite.py, whose cfed170
    namespace lacks the 3.0 options (they take their defaults). Strict QA (key residue, stale
    selection or review, decode checks) raises before anything is published.
    """
    opts = PackageOptions.from_args(args)
    source, output = opts.clean_dir, opts.out_dir
    paths = list_frames(source)
    if not paths:
        raise ValueError("no PNG frames in clean-dir")
    if source.resolve() == output.resolve():
        raise ValueError("out-dir must differ from clean-dir")
    if os.path.lexists(output):
        raise ValueError("out-dir already exists; use a fresh destination to preserve accepted assets")
    if opts.video_formats or opts.tiers:
        caps = capabilities()
        for fmt in sorted(opts.video_formats | ({"packed"} if opts.tiers else set())):
            if not caps.get(fmt):
                raise PackageError(f"{fmt} needs ffmpeg with {'libx264' if fmt == 'packed' else 'libvpx-vp9'}; "
                                   "use --formats png for offline fallback")
    frame_hash = _FrameHashes(paths)
    clip = _clip(opts, paths, frame_hash)
    timeline = clip.timeline
    registration = load_registration(opts.registration, opts.action_padding) if opts.registration else None
    sequence_hashes = [frame_hash(i) for i in timeline.indices]
    input_digest = frames_digest(sequence_hashes)
    review = None
    if opts.review is not None:
        bindings = {"inputDigest": input_digest}
        if opts.selection is not None:
            bindings = {"selection": clip.selection["sha256"], **bindings}
        if clip.tick_expansion is not None:  # a review of the authored (unexpanded) frame list still binds
            authored = [frame_hash(i) for i in clip.tick_expansion["authoredSourceIndices"]]
            bindings["authoredDigest"] = frames_digest(authored)
        review = load_review(opts.review, bindings)
    key, key_source, key_document = resolve_key(opts.key, opts.pipeline_meta, clean_dir=source,
                                                registration=opts.registration)

    images = _load_unique(paths, timeline.indices)
    frame_size = next(iter(images.values())).size
    size, anchor = _geometry_inputs(opts, frame_size, registration)
    sequence = [images[i] for i in timeline.indices]
    prepared, geometry = prepare(sequence, size, anchor, opts.max_side, opts.crop_union, resampler=opts.resampler)
    diagnostics = analyze(sequence, float(timeline.rate), clip.loop_policy != "oneshot", key=key)
    residue = diagnostics.get("keyResidue")
    checks = residue_checks(residue, opts.allow_key_residue)
    failed = [c for c in checks if c["status"] == "fail"]
    if failed:
        frames_hit = sorted(set(residue["framesWithOpaqueKey"]) | set(residue["framesOverRingSpill"]))
        raise QualityGateError(
            f"key residue: {residue['opaqueKeyPx']} opaque key px, outer-ring spill "
            f"{residue['maxOuterRingSpillFraction']:.1%} (limit {RESIDUE_LIMITS['outerRingSpillFraction']:.0%}) "
            f"in clip frames {frames_hit[:12]}{' ...' if len(frames_hit) > 12 else ''}; re-key the frames "
            "(video2dsprite clean with the soft matte) or pass --allow-key-residue to ship them with a recorded override")

    loop = clip.loop_policy != "oneshot"
    first_position: dict[int, int] = {}
    for position, index in enumerate(timeline.indices):
        first_position.setdefault(index, position)
    even = {i: np.asarray(prepared[p]) for i, p in first_position.items()}
    cw, ch = geometry["contentSize"]
    content = {i: np.ascontiguousarray(frame[:ch, :cw]) for i, frame in even.items()}
    tiers = [plan_tier(spec, geometry, timeline, opts.name, resampler=opts.resampler) for spec in opts.tiers]
    seam = forge_core.seam_report([even[i] for i in timeline.indices]) if loop and len(timeline.indices) > 1 else None
    checks += _clip_checks(diagnostics, seam, clip, registration, review)

    with forge_core.staged_output(output) as stage:
        poster = stage / f"{opts.name}-poster.png"
        forge_core.save_png(prepared[0], poster)
        pages, cell = _atlas(prepared, stage, opts.name)
        transport = Transport(opts.name, timeline, even, content, loop, opts.video_formats, tiers,
                              opts.crf_webm, opts.crf_packed)
        files = encode(transport, stage)
        checks += [{"id": f"full_decode_{record['file']}", "status": "pass", "value": record["frameCount"],
                    "threshold": record["frameCount"]}
                   for record in [files.get("webm"), files.get("packedAlpha"), *files.get("mobilePackedAlpha", [])]
                   if record]
        input_refs = [_input_ref(paths[i], "frame") for i in sorted(images)]
        documents = [(opts.selection, "selection"), (opts.registration, "registration"), (opts.review, "review"),
                     (opts.pipeline_meta, "pipelineMeta"),
                     (key_document if key_source == "matte-report" else None, "matteReport")]
        input_refs += [_input_ref(p, role) for p, role in documents if p is not None]
        output_refs = [_file_ref(poster)] + [_file_ref(stage / page["file"]) for page in pages]
        output_refs += [_file_ref(stage / r["file"]) for r in
                        [files.get("webm"), files.get("packedAlpha"), *files.get("mobilePackedAlpha", [])] if r]
        qa = {
            "status": _qa_status(checks),
            "method": ("engine_export package: forge_matte.matte_qa key-residue gate on every packaged source frame "
                       f"(alpha <= {RESIDUE_ALPHA_FLOOR} cleared, D18; key from {key_source}, D17); body-bounds and "
                       "premultiplied seam diagnostics; selection, registration and review bound by sha256; forge_av "
                       "encoders verify packet counts and keyframes; every encoded file is decoded completely"),
            "notProven": list(PACKAGE_NOT_PROVEN),
            "checks": checks,
            "inputs": [{k: v for k, v in ref.items() if k != "role"} for ref in input_refs],
            "outputs": output_refs,
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            **{k: v for k, v in diagnostics.items() if k != "status"},
            "allowKeyResidue": opts.allow_key_residue,
            "keySource": key_source,
        }
        if seam is not None:
            qa["seamReport"] = _rounded(seam)
        manifest = _manifest(opts, clip, geometry, registration, review, qa, pages, cell, poster, files,
                             [paths[i] for i in sorted(images)], [frame_hash(i) for i in sorted(images)])
        provenance = _provenance(opts, clip, registration, review, input_refs, input_digest, key, key_source,
                                 files, output)
        forge_core.write_json(stage / PROVENANCE_FILE, provenance)
        forge_core.write_json(stage / QA_FILE, qa)
        forge_core.write_json(stage / MANIFEST_FILE, manifest)
    return manifest


def _file_ref(path: Path) -> dict:
    """fileRef of a packaged file, relative to the package folder it sits in (forge_core.file_ref, D30)."""
    return forge_core.file_ref(path, path.parent)


def _rounded(report: Mapping) -> dict:
    return {k: round(v, 6) if isinstance(v, float) else v for k, v in report.items()}


def _clip_checks(diagnostics: Mapping, seam: Mapping | None, clip: _Clip, registration: Mapping | None,
                 review: Mapping | None) -> list[dict]:
    """Diagnostic, binding and review checks of the package QA (the residue gate comes first)."""
    checks = [
        {"id": "blank_frames", "status": "warn" if diagnostics["blankFrames"] else "pass",
         "value": diagnostics["blankFrames"], "threshold": []},
        {"id": "edge_touch_frames", "status": "warn" if diagnostics["edgeTouchFrames"] else "pass",
         "value": diagnostics["edgeTouchFrames"], "threshold": []},
    ]
    if seam is not None:
        checks.append({"id": "loop_seam_over_p95", "value": round(seam["seam_over_p95"], 6),
                       "threshold": SEAM_TOLERANCE,
                       "status": "warn" if seam["seam_over_p95"] > SEAM_TOLERANCE else "pass"})
    checks.append({"id": "selection_hashes", "status": "pass" if clip.selection else "skipped",
                   "value": clip.selection["sha256"] if clip.selection else None, "threshold": None})
    checks.append({"id": "registration", "status": "pass" if registration else "skipped",
                   "value": registration["mode"] if registration else None, "threshold": None})
    checks.append({"id": "fps_cap", "status": "pass", "value": rational(clip.timeline.rate),
                   "threshold": rational(MAX_FPS)})
    if clip.tick_expansion is not None:
        expansion = clip.tick_expansion
        checks.append({"id": "tick_expansion", "status": "pass",
                       "value": {"rate": expansion["rate"], "authoredFrames": len(expansion["ticks"]),
                                 "frames": len(clip.timeline.indices)},
                       "threshold": {"edgeToleranceMs": TICK_EDGE_TOLERANCE_MS, "maxFps": rational(MAX_FPS)}})
    if review is None:
        checks.append({"id": "review_verdict", "status": "needs-visual-review", "value": None,
                       "threshold": "accepted"})
    else:
        checks.append({"id": "review_verdict", "status": "pass" if review["status"] == "accepted" else "warn",
                       "value": review["status"], "threshold": "accepted"})
    return checks


def _manifest(opts: PackageOptions, clip: _Clip, geometry: Mapping, registration: Mapping | None,
              review: Mapping | None, qa: Mapping, pages: list, cell: tuple, poster: Path, files: Mapping,
              inputs: Sequence[Path], input_hashes: Sequence[str]) -> dict:
    timeline = clip.timeline
    events = [{**event, "frame": timeline.frame_at(event["atMs"])} for event in clip.events]
    legacy_mode = geometry["registration"]
    block = dict(registration) if registration else {"mode": "fixed-envelope" if opts.crop_union else "preserved"}
    block["legacyMode"] = legacy_mode
    manifest: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION, "name": opts.name,
        **{k: v for k, v in geometry.items() if k != "registration"},
        "registration": block,
        "fps": float(timeline.rate),
        "fpsRational": rational(timeline.rate),
        "frameCount": len(timeline.indices), "durationSeconds": float(timeline.seconds),
        "loop": clip.loop_policy != "oneshot",
        "reviewStatus": review["status"] if review else "needs-visual-review",
        "hitEvents": [event for event in events if event["name"] == "hit"],
        "poster": digest(poster),
        "fallback": {"type": "png-atlas", "cellSize": list(cell), "pages": pages},
        **{key: files[key] for key in ("webm", "packedAlpha") if key in files},
        "qa": qa,
        "inputFrames": [{"file": p.name, "bytes": p.stat().st_size, "sha256": h} for p, h in zip(inputs, input_hashes)],
        "runtimeNotes": list(RUNTIME_NOTES),
        "sourceIndices": list(timeline.indices),
        "durationsMs": list(timeline.durations),
        "loopPolicy": clip.loop_policy,
        "events": events,
    }
    optional = {"impactMs": clip.impact_ms, "holdMs": clip.hold_ms, **clip.extras,
                "terminal": True if clip.terminal else None, "displayScale": opts.display_scale,
                "sampling": opts.sampling or ("nearest" if opts.pixel_art else "linear"),
                "pixelArt": opts.pixel_art, "bodyHeightPx": opts.body_height_px, "shadow": opts.shadow,
                "budgetClass": opts.budget_class}
    manifest.update({k: v for k, v in optional.items() if v is not None})
    if "mobilePackedAlpha" in files:
        manifest["mobilePackedAlpha"] = files["mobilePackedAlpha"]
    if clip.pingpong_baked:
        manifest["pingpongBaked"] = True
    if clip.tick_expansion is not None:
        manifest["tickExpansion"] = dict(clip.tick_expansion)
    if clip.capped:
        manifest["fpsCapped"], manifest["inputFps"] = True, rational(clip.input_rate)
    manifest.update({"provenanceFile": PROVENANCE_FILE, "artSource": opts.art_source,
                     "placeholder": opts.placeholder})
    return manifest


def _provenance(opts: PackageOptions, clip: _Clip, registration: Mapping | None, review: Mapping | None,
                input_refs: list, input_digest: str, key: Any, key_source: str, files: Mapping,
                output: Path) -> dict:
    params = {
        "name": opts.name, "formats": sorted(opts.formats), "maxSide": opts.max_side, "cropUnion": opts.crop_union,
        "fps": rational(opts.fps) if opts.fps else None, "loopPolicy": clip.loop_policy,
        "tiers": [tier_text(t) for t in opts.tiers], "resampler": opts.resampler,
        "pixelArt": opts.pixel_art, "key": list(key) if key else None, "keySource": key_source,
        "allowKeyResidue": opts.allow_key_residue, "crfWebm": opts.crf_webm, "crfPacked": opts.crf_packed,
        "sourceSize": list(opts.source_size) if opts.source_size else None,
        "sourceAnchor": list(opts.source_anchor) if opts.source_anchor else None,
        "actionPadding": list(opts.action_padding) if opts.action_padding else None,
        "tickExpansion": clip.tick_expansion["rate"] if clip.tick_expansion else None,
        "engineExport": ENGINE_EXPORT_VERSION,
    }
    record: dict[str, Any] = {
        "schema": PROVENANCE_SCHEMA, "tool": TOOL_NAME, "version": TOOL_VERSION, "params": params,
        "inputs": input_refs, "inputDigest": input_digest,
        "libraries": {"forge_core": forge_core.FORGE_CORE_API_VERSION, "forge_matte": forge_matte.FORGE_MATTE_API_VERSION,
                      "forge_av": forge_av.FORGE_AV_API_VERSION},
    }
    directory = relative_dir(opts.clean_dir, output)
    if directory is not None:
        record["inputDirectory"] = directory
    if files:
        info = forge_av.ffmpeg_info()
        record["renderer"] = {"name": "ffmpeg", "version": info["version"] or "unknown"}
    if clip.selection:
        record["selection"] = clip.selection
    if registration:
        record["registration"] = dict(registration)
    if review:
        record["review"] = dict(review)
    return record


# --------------------------------------------------------------------------- verify

def evaluate_transport(reference: Sequence[np.ndarray], decoded: Iterable[np.ndarray], *, loop: bool,
                       label: str = "media") -> dict:
    """Compare decoded RGBA frames with their lossless reference, frame by frame.

    Per frame: alpha mean/p99/max error, the decoded alpha median where the reference is
    transparent (0) or opaque (255), and straight RGB error where the reference alpha > 16
    (and on the 32..223 translucent edge). Over the clip: dynamic alpha (reference alpha that moves
    must still move) and, for loops, the decoded seam (forge_core.transition_mae last -> first)
    against the 95th-percentile adjacent step, allowing ``SEAM_TOLERANCE`` over
    max(1, reference ratio) so only a seam the codec added fails. Returns metrics and checks.
    """
    limits = VERIFY_LIMITS
    stats = []
    ref_motion, dec_motion, ref_steps, dec_steps = [], [], [], []
    first = previous = mismatch = None
    count = 0
    for index, frame in enumerate(decoded):
        count += 1
        if index >= len(reference) or mismatch is not None:
            continue
        ref = reference[index]
        frame = np.asarray(frame)
        if frame.shape != ref.shape:
            mismatch = [int(frame.shape[1]), int(frame.shape[0])]
            continue
        ref_alpha, dec_alpha = ref[..., 3].astype(np.int16), frame[..., 3].astype(np.int16)
        delta = np.abs(ref_alpha - dec_alpha)
        zeros, whites = ref_alpha == 0, ref_alpha == 255
        visible, edge = ref_alpha > 16, (ref_alpha >= 32) & (ref_alpha <= 223)
        colour = np.abs(ref[..., :3].astype(np.int16) - frame[..., :3].astype(np.int16))
        stats.append({
            "alphaMAE": float(delta.mean()), "alphaP99": float(np.percentile(delta, 99)), "alphaMax": int(delta.max()),
            "transparentMedian": float(np.median(dec_alpha[zeros])) if zeros.any() else None,
            "opaqueMedian": float(np.median(dec_alpha[whites])) if whites.any() else None,
            "rgbMAE": float(colour[visible].mean()) if visible.any() else None,
            "edgeRgbMAE": float(colour[edge].mean()) if edge.any() else None,
        })
        if previous is not None:
            ref_motion.append(float(np.abs(ref_alpha - previous[0][..., 3].astype(np.int16)).mean()))
            dec_motion.append(float(np.abs(dec_alpha - previous[1][..., 3].astype(np.int16)).mean()))
            if loop:
                ref_steps.append(forge_core.transition_mae(previous[0], ref))
                dec_steps.append(forge_core.transition_mae(previous[1], frame))
        else:
            first = (ref, frame)
        previous = (ref, frame)
    expected = len(reference)
    checks = [{"id": f"{label}.frame_count", "status": "pass" if count == expected else "fail",
               "value": count, "threshold": expected}]
    if mismatch is not None:
        size = [int(reference[0].shape[1]), int(reference[0].shape[0])]
        checks.append({"id": f"{label}.frame_size", "status": "fail", "value": mismatch, "threshold": size})
        return {"metrics": {"frames": count}, "checks": checks}
    if not stats:
        return {"metrics": {"frames": count}, "checks": checks}

    def worst(key: str, pick=max):
        values = [s[key] for s in stats if s[key] is not None]
        return pick(values) if values else None

    metrics = {"frames": count, "alphaMAE": worst("alphaMAE"), "alphaMAEMean": float(np.mean([s["alphaMAE"] for s in stats])),
               "alphaP99": worst("alphaP99"), "alphaMax": worst("alphaMax"),
               "transparentMedianMax": worst("transparentMedian"), "opaqueMedianMin": worst("opaqueMedian", min),
               "rgbMAE": worst("rgbMAE"), "edgeRgbMAE": worst("edgeRgbMAE"),
               "sourceAlphaMotionMax": max(ref_motion, default=0.0), "decodedAlphaMotionMax": max(dec_motion, default=0.0)}

    def gate(name: str, value: Any, threshold: float, ok: bool) -> dict:
        return {"id": f"{label}.{name}", "status": "skipped" if value is None else ("pass" if ok else "fail"),
                "value": None if value is None else round(float(value), 6), "threshold": threshold}

    checks += [
        gate("alpha_mae", metrics["alphaMAE"], limits["alphaMAE"], metrics["alphaMAE"] <= limits["alphaMAE"]),
        {"id": f"{label}.alpha_p99", "status": "pass", "value": metrics["alphaP99"], "threshold": None},
        {"id": f"{label}.alpha_max", "status": "pass", "value": metrics["alphaMax"], "threshold": None},
        gate("transparent_median", metrics["transparentMedianMax"], limits["transparentMedian"],
             metrics["transparentMedianMax"] is not None and metrics["transparentMedianMax"] <= limits["transparentMedian"]),
        gate("opaque_median", metrics["opaqueMedianMin"], limits["opaqueMedian"],
             metrics["opaqueMedianMin"] is not None and metrics["opaqueMedianMin"] >= limits["opaqueMedian"]),
        gate("rgb_mae", metrics["rgbMAE"], limits["rgbMAE"],
             metrics["rgbMAE"] is not None and metrics["rgbMAE"] <= limits["rgbMAE"]),
    ]
    moving = metrics["sourceAlphaMotionMax"] > limits["sourceAlphaMotion"]
    dynamic = not moving or metrics["decodedAlphaMotionMax"] > limits["decodedAlphaMotion"]
    checks.append({"id": f"{label}.dynamic_alpha", "status": "pass" if dynamic else "fail",
                   "value": {"source": round(metrics["sourceAlphaMotionMax"], 6),
                             "decoded": round(metrics["decodedAlphaMotionMax"], 6)},
                   "threshold": {"source": limits["sourceAlphaMotion"], "decoded": limits["decodedAlphaMotion"]}})
    if loop and ref_steps and first is not None and count == expected:
        ref_ratio = _seam_ratio(forge_core.transition_mae(previous[0], first[0]), ref_steps)
        dec_ratio = _seam_ratio(forge_core.transition_mae(previous[1], first[1]), dec_steps)
        limit = limits["seamTolerance"] * max(1.0, ref_ratio)
        metrics["seam"] = {"decodedSeamOverP95": round(dec_ratio, 6), "referenceSeamOverP95": round(ref_ratio, 6)}
        checks.append({"id": f"{label}.decoded_seam", "status": "pass" if dec_ratio <= limit else "fail",
                       "value": round(dec_ratio, 6), "threshold": round(limit, 6)})
        checks.append({"id": f"{label}.source_seam", "value": round(ref_ratio, 6), "threshold": limits["seamTolerance"],
                       "status": "warn" if ref_ratio > limits["seamTolerance"] else "pass"})
    return {"metrics": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in metrics.items()}, "checks": checks}


def _seam_ratio(seam: float, steps: Sequence[float]) -> float:
    return seam / max(float(np.percentile(steps, 95)), 1e-6)


def atlas_frames(package_dir: Path, manifest: Mapping) -> list[np.ndarray]:
    """The packaged frames (encodedSize RGBA) sliced from the PNG atlas pages, in clip order."""
    fallback = manifest.get("fallback") or {}
    cell_w, cell_h = fallback.get("cellSize") or (0, 0)
    frames: list[np.ndarray] = []
    for page in sorted(fallback.get("pages") or [], key=lambda p: p.get("firstFrame", 0)):
        if page.get("firstFrame") != len(frames):
            raise ValueError("atlas pages must cover the frames contiguously from frame 0")
        sheet = np.asarray(forge_core.load_rgba(package_dir / page["file"])[0])
        columns = page["columns"]
        for k in range(page["frameCount"]):
            row, col = divmod(k, columns)
            cell = sheet[row * cell_h:(row + 1) * cell_h, col * cell_w:(col + 1) * cell_w]
            if cell.shape[:2] != (cell_h, cell_w):
                raise ValueError(f"atlas page {page['file']} is too small for frame {len(frames)}")
            frames.append(np.ascontiguousarray(cell))
    if len(frames) != manifest.get("frameCount"):
        raise ValueError(f"atlas holds {len(frames)} frames, manifest frameCount is {manifest.get('frameCount')}")
    return frames


def package_media(manifest: Mapping) -> list[tuple[str, dict]]:
    """Every packaged file record with its role: poster, atlas pages, webm, packed and tiers."""
    records = [("poster", manifest.get("poster"))]
    records += [("atlas", page) for page in (manifest.get("fallback") or {}).get("pages") or []]
    records += [(key, manifest.get(key)) for key in ("webm", "packedAlpha") if manifest.get(key)]
    records += [(f"tier:{tier.get('tier')}", tier) for tier in manifest.get("mobilePackedAlpha") or []]
    return [(role, record) for role, record in records if isinstance(record, dict)]


def manifest_fps(manifest: Mapping) -> Fraction:
    """Exact transport rate of a manifest: ``fpsRational`` (3.0) or ``fps``."""
    return parse_rate(manifest.get("fpsRational") or manifest.get("fps"), "manifest fps")


def _container_checks(path: Path, label: str, *, codec: str, size: Sequence[int], frames: int,
                      duration_ms: float, alpha_tag: bool) -> list[dict]:
    info = forge_av.probe(path)
    measured = forge_av.duration_ms(path)
    packets = forge_av.packet_count(path)
    checks = [
        {"id": f"{label}.codec", "status": "pass" if info["codec"] == codec else "fail", "value": info["codec"],
         "threshold": codec},
        {"id": f"{label}.dimensions", "value": [info["width"], info["height"]], "threshold": list(size),
         "status": "pass" if [info["width"], info["height"]] == list(size) else "fail"},
        {"id": f"{label}.packets", "status": "pass" if packets == frames else "fail", "value": packets,
         "threshold": frames},
        {"id": f"{label}.timestamps_increasing", "status": "pass" if forge_av.timestamps_increasing(path) else "fail",
         "value": None, "threshold": None},
        {"id": f"{label}.duration_ms", "value": round(measured, 3), "threshold": round(duration_ms, 3),
         "status": "pass" if abs(measured - duration_ms) <= VERIFY_LIMITS["durationToleranceMs"] else "fail"},
    ]
    if alpha_tag:
        checks.append({"id": f"{label}.vp9_alpha_mode", "status": "pass" if info["vp9_alpha"] else "fail",
                       "value": info["alpha_mode"], "threshold": True})
    else:
        moov = forge_av.moov_before_mdat(path)
        checks.append({"id": f"{label}.moov_before_mdat", "status": "pass" if moov else "fail", "value": moov,
                       "threshold": True})
    return checks


def verify_package(package_dir: Path) -> dict:
    """Decode every encoded transport of a package fully and judge it against the atlas.

    Returns a qaEnvelope (``video2dsprite.verify.v1``) bound by sha256 to animation.json, the
    atlas and poster (inputs) and the encoded files (outputs). Gate failures give status fail;
    unusable packages raise ValueError.
    """
    manifest_path = package_dir / MANIFEST_FILE
    manifest = _read_object(manifest_path, "animation.json")
    if manifest.get("schemaVersion") not in ("2.0", "3.0"):
        raise ValueError("verify reads animation.json schemaVersion 3.0 (or 2.0)")
    media = package_media(manifest)
    checks, stale = [], []
    for role, record in media:
        path = package_dir / str(record.get("file", ""))
        if not path.is_file() or forge_core.sha256_file(path) != record.get("sha256"):
            stale.append(record.get("file"))
    checks.append({"id": "files_match_manifest", "status": "fail" if stale else "pass", "value": stale,
                   "threshold": []})
    encoded = [(role, record) for role, record in media if role not in ("poster", "atlas")]
    if not encoded:
        raise ValueError("verify needs an encoded transport; package with --formats webm and/or packed or --tiers")
    transports = {}
    if not stale:
        reference = atlas_frames(package_dir, manifest)
        count = manifest["frameCount"]
        fps = manifest_fps(manifest)
        duration = float(Fraction(count) / fps * 1000)
        loop = manifest.get("loop") is True
        for role, record in encoded:
            path = package_dir / record["file"]
            if role == "webm":
                frames_ref, frame_count, expected_ms = reference, count, duration
                decoded = forge_av.iter_rgba(path, alpha="auto")  # an opaque stream must fail, not raise
                container = _container_checks(path, "webm", codec="vp9", size=manifest["encodedSize"],
                                              frames=frame_count, duration_ms=expected_ms, alpha_tag=True)
            else:
                geometry = {**record, "halfWidth": record.get("halfWidth", record["width"]),
                            "halfHeight": record.get("halfHeight", record["height"])}
                if role == "packedAlpha":
                    width, height = record["width"], record["height"]
                    frames_ref, frame_count, expected_ms = [f[:height, :width] for f in reference], count, duration
                else:
                    width, height = record["sourceWidth"], record["sourceHeight"]
                    content = {i: f[:height, :width] for i, f in enumerate(reference)}
                    frames_ref = tier_frames(content, range(count), record)
                    frame_count = len(frames_ref)
                    expected_ms = float(Fraction(frame_count) / parse_rate(record["fps"], "tier fps") * 1000)
                decoded = (forge_av.unpack_packed_alpha(frame, geometry)
                           for frame in forge_av.iter_rgba(path, alpha="off"))
                container = _container_checks(path, role, codec="h264",
                                              size=[2 * geometry["halfWidth"], geometry["halfHeight"]],
                                              frames=frame_count, duration_ms=expected_ms, alpha_tag=False)
            result = evaluate_transport(frames_ref, decoded, loop=loop, label=role)
            transports[role] = {"file": record["file"], **result["metrics"]}
            checks += container + result["checks"]
    status = "fail" if any(c["status"] == "fail" for c in checks) else \
        ("warn" if any(c["status"] == "warn" for c in checks) else "pass")
    manifest_ref = _file_ref(manifest_path)
    inputs = [manifest_ref] + [_file_ref(package_dir / record["file"]) for role, record in media
                               if role in ("poster", "atlas") and (package_dir / record["file"]).is_file()]
    outputs = [_file_ref(package_dir / record["file"]) for _, record in encoded
               if (package_dir / record["file"]).is_file()]
    return {
        "schema": VERIFY_SCHEMA, "status": status,
        "method": ("engine_export verify: full ffmpeg decode of every encoded file (WebM alpha through "
                   "libvpx-vp9, packed halves recombined from the red channel) compared frame by frame with the "
                   "lossless PNG atlas; ffprobe codec, dimensions, packets, decode timestamps, duration, "
                   "faststart and VP9 alpha tag"),
        "notProven": list(VERIFY_NOT_PROVEN),
        "checks": checks, "inputs": inputs, "outputs": outputs,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "manifestSha256": manifest_ref["sha256"], "transports": transports, "thresholds": dict(VERIFY_LIMITS),
    }


# --------------------------------------------------------------------------- CLI

def add_package_arguments(parser: argparse.ArgumentParser) -> None:
    """The ``package`` options; dest names match the cfed170 namespace of video2dsprite.py package."""
    parser.add_argument("--clean-dir", required=True, help="RGBA PNG frames sorted by name (clean_*.png first)")
    parser.add_argument("--output-dir", "--out-dir", dest="out_dir", required=True,
                        help="new package directory; must not exist")
    parser.add_argument("--name", default="clip", help="file stem: letters, digits, - and _ (default clip)")
    parser.add_argument("--fps", help="constant playback rate, e.g. 12 or 30000/1001; optional with --selection")
    parser.add_argument("--selection", help="forge-frame-selection v1/v2 JSON: frames, durations, events, loop policy")
    parser.add_argument("--registration",
                        help="register_clip registration.json (best: it records the padding apply used) or the "
                             "registration_job.v1; also supplies the key for --key auto")
    parser.add_argument("--action-padding", metavar="L,T,R,B",
                        help="with a --registration job: the padding register_clip apply --action-padding used")
    parser.add_argument("--review", help="review_verdict.v1 JSON bound to the selection file or the input digest")
    parser.add_argument("--pipeline-meta", help="video2dsprite pipeline-meta.json; supplies the matte key")
    parser.add_argument("--source-size", help="original art geometry W,H; defaults to the registration or input frames")
    parser.add_argument("--source-anchor", help="original art anchor X,Y; defaults to the registration or bottom-center")
    parser.add_argument("--max-side", type=int, default=384, help="encoding cap in px, never upscales (default 384)")
    parser.add_argument("--crop-union", action="store_true", help="one shared alpha envelope for all frames")
    parser.add_argument("--formats", default="png", help="png,webm,packed; the PNG fallback is always written")
    parser.add_argument("--tiers", help="mobile packed tiers: actor, prop, fx or name:EDGE@FPS, comma separated")
    parser.add_argument("--budget-class", help="runtime budget class, e.g. actor, prop, fx (default: the one tier)")
    parser.add_argument("--loop", action="store_true", help="loop the clip (cycle policy); seam still needs review")
    parser.add_argument("--loop-policy", choices=LOOP_POLICIES, help="cycle, pingpong (baked into one cycle) or oneshot")
    parser.add_argument("--key", default="auto",
                        help="residue-gate key: auto (clean-dir matte-report.json, then the registration keyColor, "
                             "then --pipeline-meta, else magenta), magenta, green, blue, #rrggbb or none")
    parser.add_argument("--allow-key-residue", action="store_true",
                        help="ship frames that fail the key-residue gate; recorded in QA (legacy behaviour)")
    parser.add_argument("--pixel-art", action="store_true", help="nearest sampling and whole-factor reductions only")
    parser.add_argument("--sampling", choices=("nearest", "linear"), help="runtime texture sampling hint")
    parser.add_argument("--resampler", choices=forge_core.RESAMPLERS, help="resize filter (default lanczos)")
    parser.add_argument("--body-height-px", type=float, help="visible body height in source px")
    parser.add_argument("--shadow", help="ground shadow ellipse RX,RY[,OPACITY] in source px")
    parser.add_argument("--display-scale", type=float, help="runtime draw scale of source units")
    parser.add_argument("--cadence-ms", type=float, help="gait cycle length at the reference speed")
    parser.add_argument("--stride-world-units", type=float, help="world distance per gait cycle")
    parser.add_argument("--speed-ref", type=float, help="reference speed in world units per second")
    parser.add_argument("--cycles", type=float, help="gait cycles in one loop")
    parser.add_argument("--terminal", action="store_true", help="the clip ends its state and holds the last frame")
    parser.add_argument("--art-source", choices=ART_SOURCES, default="video", help="where the art came from")
    parser.add_argument("--placeholder", action="store_true", help="stand-in art, to be replaced")
    parser.add_argument("--crf-webm", type=int, default=28, help="VP9 quality, lower is better (default 28)")
    parser.add_argument("--crf-packed", type=int, default=18, help="H.264 quality, lower is better (default 18)")


def add_verify_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--package", required=True, help="package directory holding animation.json")
    parser.add_argument("--report", help=f"report path; must not exist (default <package>/{VERIFY_FILE})")


def cmd_package(args: argparse.Namespace) -> int:
    """``package``: one ASCII JSON line with the output folder and the metadata (animation.json) path (D20)."""
    manifest = package(args)
    output = Path(args.out_dir).resolve()
    provenance = forge_core.read_json(output / PROVENANCE_FILE)
    summary = {"output": str(output), "metadata": str(output / MANIFEST_FILE), "manifest": str(output / MANIFEST_FILE),
               "qa": str(output / QA_FILE), "provenance": str(output / PROVENANCE_FILE),
               "status": manifest["qa"]["status"], "reviewStatus": manifest["reviewStatus"],
               "frameCount": manifest["frameCount"], "fps": manifest["fpsRational"],
               "durationMs": sum(manifest["durationsMs"]), "keySource": manifest["qa"]["keySource"],
               "inputDigest": provenance["inputDigest"]}
    if "tickExpansion" in manifest:
        summary["tickExpansion"] = manifest["tickExpansion"]["rate"]
    print(json.dumps(summary, ensure_ascii=True))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """``verify``: writes the report only when every gate passes, so a published report never says fail (D26)."""
    package_dir = Path(args.package)
    report_path = Path(args.report) if args.report else package_dir / VERIFY_FILE
    if os.path.lexists(report_path):
        raise ValueError(f"report already exists: {report_path.name}; verify writes a new file")
    report = verify_package(package_dir)
    failed = [c for c in report["checks"] if c["status"] == "fail"]
    if failed:
        details = "; ".join(f"{c['id']} {c['value']} (limit {c['threshold']})" for c in failed[:8])
        raise QualityGateError(f"verify failed {len(failed)} check(s): {details}; nothing was written")
    stage = report_path.with_name(f".{report_path.name}.{os.getpid()}.tmp")
    try:
        forge_core.write_json(stage, report)
        forge_core.publish_file_no_replace(stage, report_path)
    finally:
        stage.unlink(missing_ok=True)
    written = str(report_path.resolve())
    print(json.dumps({"output": written, "metadata": written, "report": written,
                      "manifest": str((package_dir / MANIFEST_FILE).resolve()), "status": report["status"],
                      "checks": len(report["checks"]), "transports": sorted(report["transports"])}, ensure_ascii=True))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(json.dumps(capabilities(), ensure_ascii=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engine_export.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    pk = sub.add_parser("package", help="write a new animation package (animation.json 3.0)",
                        description="Package clean RGBA frames: PNG poster and atlas, optional WebM, packed MP4 and "
                                    "mobile tiers, animation.json 3.0, animation-qa.json and provenance.json.")
    add_package_arguments(pk)
    pk.set_defaults(func=cmd_package)
    vf = sub.add_parser("verify", help="decode every encoded file and compare it with the atlas",
                        description="Decode the WebM, packed MP4 and tier files completely, compare them with the "
                                    f"PNG atlas and write {VERIFY_FILE}; nothing is written when a gate fails.")
    add_verify_arguments(vf)
    vf.set_defaults(func=cmd_verify)
    dr = sub.add_parser("doctor", help="print functional ffmpeg capabilities as JSON")
    dr.set_defaults(func=cmd_doctor)
    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors exit 2 (argparse), refused inputs and failed gates print ``error: ...`` and exit 1, and
    anything unexpected prints ``error: internal error (...)`` and exits 1 (D26, D27; forge_core.run_cli)."""
    return forge_core.run_cli(_run, argv, expected=CLI_ERRORS)


if __name__ == "__main__":
    raise SystemExit(main())
