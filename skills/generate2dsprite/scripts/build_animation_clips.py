"""Package registered, shared-canvas sprite frames into explicit animation clips (manifest v1 or v2).

Usage (run from the project root; each command is one line):
  python "<skill-dir>/scripts/build_animation_clips.py" --manifest clips.json --output-dir out/hero-clips
  python "<skill-dir>/scripts/build_animation_clips.py" --manifest clips.json --output-dir out/hero-clips --preview-scale 4 --preview-background checker --strict

Manifest paths are relative to the manifest file. anchor_px is one shared canvas
root (often a ground reference), never each frame's visible bottom. No frame is
cropped, aligned, normalized, keyed or regenerated: frame PNGs are copied byte for
byte and must be still 8-bit RGBA with transparent and visible pixels. Indexed PNG
is never builder input. Code-art frames come here directly (never through
generate2dsprite process); no pipeline-meta is needed.

A v1 manifest is still accepted and keeps every v1 output field unchanged:
{
  "schema": "generate2dsprite.animation_clips.v1",
  "frames": ["frame-1.png", "frame-2.png", "idle.png"],
  "anchor_px": [64, 112],
  "clips": {
    "run": {"frames": [0, 1], "duration_ms": [80, 100], "loop": true},
    "idle": {"frames": [2], "duration_ms": 400, "loop": true}
  },
  "states": {"moving": "run", "idle": "idle"}
}
Frame entries may be {"name": "run-1", "file": "frame-1.png"}; clip frames are
zero-based indices or unique names. Optional positive stride_world_units declares
travel per clip cycle; it is not measured from art.

"schema": "generate2dsprite.animation_clips.v2" adds, per clip: ticks (one value or
one per frame) at tick_hz (default 60) instead of duration_ms; loop_policy cycle,
pingpong (mirrored without repeating the end frames) or oneshot; events
[{"at": position, "name": ..., "data": ...}] named in, tell, hit, active_end,
cancel, chain, impact, hold, end, sfx, step_l, step_r or custom:<name>; keys
{wind_start, wind_peak, strike, recover, end}; entry_frame; stride_px_per_frame,
cadence_ms, speed_ref; transitions [{"to", "entry_frame", "dissolve_ms", "mode":
dither|premultiplied}]; hitstop_ticks; role player|enemy|npc|fx|prop. Top level:
sampling, pixel_art, palette_ref, art_source, placeholder, body_height_px and
shadow {rx, ry, opacity}.

Output: frames/ (byte copies), clips/clip-NN.webp (lossless; decoded timing, alpha
and visible RGB verified), contact-sheet.png, review/ (filmstrips on light, dark and
checker backgrounds with a ground line and tick labels, onion skins, a mirrored turn
test with anchor marks, A->B dissolve previews), source-manifest.json and
animation-clips.json with, per clip, events_ms, a tick-grid drift report (60 Hz by
default), holds and the loop seam. Lints: enemy telegraph (tell -> hit) under 28
ticks, uneven or vanishing ticks, near-duplicate holds. --strict turns any warning
into a failure and publishes nothing. Errors print "error: ..." and exit 1; success
prints one JSON line with the output and metadata paths.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from fractions import Fraction
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import forge_core  # noqa: E402  (this skill's vendored copy)

_SPEC = importlib.util.spec_from_file_location("_animation_clip_frame_utils", _HERE / "assemble_frames.py")
assert _SPEC and _SPEC.loader
FRAME_UTILS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(FRAME_UTILS)
require = FRAME_UTILS.require

SCHEMA_V1 = "generate2dsprite.animation_clips.v1"
SCHEMA_V2 = "generate2dsprite.animation_clips.v2"
SCHEMA = SCHEMA_V1  # a manifest without "schema" is v1
TOOL = {"name": "build_animation_clips", "version": "2.0"}
TICK_HZ = 60
TELEGRAPH_MIN_TICKS = 28  # 60 Hz ticks between an enemy's tell and its hit (game-opus55 combat-feel-v0)
NEAR_DUPLICATE_MAE = 1.5  # premultiplied RGBA MAE over the union of visible pixels, 0-255 units
MAX_WEBP_SIDE = 16383
MAX_REVIEW_PIXELS = 24_000_000
EVENT_NAMES = ("in", "tell", "hit", "active_end", "cancel", "chain", "impact", "hold", "end", "sfx",
               "step_l", "step_r")
KEY_NAMES = ("wind_start", "wind_peak", "strike", "recover", "end")
LOOP_POLICIES = ("cycle", "pingpong", "oneshot")
ROLES = ("player", "enemy", "npc", "fx", "prop")
TRANSITION_MODES = ("premultiplied", "dither")
ART_SOURCES = ("host_image", "api", "code", "existing", "video", "mixed")
SAMPLINGS = ("nearest", "linear")
BACKGROUNDS = ("light", "dark", "checker")
V2_CLIP_FIELDS = ("ticks", "tick_hz", "loop_policy", "events", "keys", "entry_frame", "stride_px_per_frame",
                  "cadence_ms", "speed_ref", "transitions", "hitstop_ticks", "role")
V2_TOP_FIELDS = ("sampling", "pixel_art", "palette_ref", "art_source", "placeholder", "body_height_px", "shadow")
_CUSTOM_EVENT = re.compile(r"custom:\S+")
_BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], np.float64)

SHEET_BASE = (28, 35, 44, 255)
TEXT = (232, 236, 239, 255)
ANCHOR_MARK = (255, 220, 80, 255)
GROUND_LINE = (235, 64, 64, 255)
PREVIOUS_TINT = (255, 72, 72)
NEXT_TINT = (72, 150, 255)
BACKGROUND_COLOURS = {"light": (236, 236, 228, 255), "dark": (24, 28, 36, 255)}
CHECKER_COLOURS = ((204, 204, 204, 255), (150, 150, 150, 255))


class _Parser(argparse.ArgumentParser):
    """argparse with the repository's error convention: 'error: ...' on stderr and exit status 1."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(1, f"error: {forge_core.ascii_text(message)}\n")


def _half_up(numerator: int, denominator: int) -> int:
    """round_half_up(numerator / denominator) for non-negative integers, exactly."""
    return (2 * numerator + denominator) // (2 * denominator)


def _finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _warning(code: str, message: str, **extra: object) -> dict:
    return {"code": code, "severity": "warning", "message": message, **extra}


# --------------------------------------------------------------------------- manifest and frames

def load_frame_png(path: Path, name: str) -> tuple[Image.Image, dict]:
    """Load one builder frame: a still 8-bit RGB(A) PNG; indexed and 16-bit PNGs are refused."""
    raw = path.read_bytes()
    require(len(raw) >= 33 and raw[:8] == FRAME_UTILS.PNG_SIGNATURE and raw[12:16] == b"IHDR",
            f"Frame {name} must be a PNG: {path.name}")
    bit_depth, colour_type = raw[24], raw[25]
    require(colour_type != 3, f"Frame {name} is an indexed (palette) PNG; the clip builder takes 8-bit RGBA PNGs "
                              "only. Convert it first (assemble_frames.py accepts palette PNGs and writes RGBA).")
    require(bit_depth == 8, f"Frame {name} must be an 8-bit PNG; got {bit_depth}-bit. Convert it to 8-bit RGBA.")
    with Image.open(io.BytesIO(raw)) as source:
        require(getattr(source, "n_frames", 1) == 1, f"Frame {name} must be a still PNG.")
        require(source.mode in {"RGB", "RGBA"}, f"Frame {name} mode must be RGBA; got {source.mode}.")
        source.load()
        frame = source.copy()
    return frame, {"file_sha256": FRAME_UTILS.digest(raw), "bytes": len(raw), "size": list(frame.size),
                   "mode": frame.mode}


def load_contract(path: Path) -> tuple[dict, int, list[Image.Image], list[np.ndarray], list[dict], list[Path]]:
    """Read the manifest (a UTF-8 BOM is accepted) and its frames:
    ``(contract, version, images, arrays, records, frame paths)``."""
    contract = json.loads(path.read_text(encoding="utf-8-sig"))
    require(isinstance(contract, dict), "Manifest must be a JSON object.")
    schema = contract.get("schema", SCHEMA_V1)
    require(schema in (SCHEMA_V1, SCHEMA_V2), f"Expected schema {SCHEMA_V1} or {SCHEMA_V2}.")
    entries = contract.get("frames")
    require(isinstance(entries, list) and bool(entries), "frames must be a nonempty list.")
    images, arrays, records, paths = [], [], [], []
    names = set()
    for index, entry in enumerate(entries):
        require(isinstance(entry, (str, dict)), f"Frame {index} must be a path or named file object.")
        if isinstance(entry, str):
            file, name = entry, Path(entry).stem
        else:
            require("anchor_px" not in entry and "anchorPx" not in entry,
                    "Per-frame anchors are not supported; declare one shared anchor_px.")
            file, name = entry.get("file"), entry.get("name")
        require(isinstance(file, str) and bool(file), f"Frame {index} needs a file path.")
        require(isinstance(name, str) and bool(name.strip()) and name not in names,
                f"Frame {index} needs a unique nonempty name; duplicate stems need explicit names.")
        names.add(name)
        source = (path.parent / file).resolve()
        image, provenance = load_frame_png(source, name)
        array = FRAME_UTILS.pixel_array(image)
        require(image.mode == "RGBA" and bool(np.any(array[..., 3] == 0)) and bool(np.any(array[..., 3] > 0)),
                f"Frame {name} must contain both transparent and visible pixels in RGBA; use extracted sprites.")
        images.append(image)
        arrays.append(array)
        paths.append(source)
        records.append({"index": index, "name": name, "source": provenance,
                        "rgba_pixel_sha256": FRAME_UTILS.digest(array.tobytes()),
                        "transparent_pixel_count": int(np.count_nonzero(array[..., 3] == 0)),
                        "visible_pixel_count": int(np.count_nonzero(array[..., 3] > 0))})
    require(len({image.size for image in images}) == 1, "All frames must have the same native canvas size.")
    anchor = contract.get("anchor_px")
    require(isinstance(anchor, list) and len(anchor) == 2 and all(_finite_number(value) for value in anchor),
            "anchor_px must be two finite numeric shared-canvas coordinates.")
    width, height = images[0].size
    require(0 <= anchor[0] <= width and 0 <= anchor[1] <= height,
            "Shared anchor_px must be inside or on the edge of the source canvas.")
    return contract, 2 if schema == SCHEMA_V2 else 1, images, arrays, records, paths


def resolve_top_level(contract: dict, manifest: Path, final: Path) -> tuple[dict, list[dict]]:
    """Validate the v2 top-level fields and return them as written to the output, plus lint."""
    fields: dict = {}
    lint: list[dict] = []
    if "sampling" in contract:
        require(contract["sampling"] in SAMPLINGS, "sampling must be nearest or linear.")
        fields["sampling"] = contract["sampling"]
    for flag in ("pixel_art", "placeholder"):
        if flag in contract:
            require(type(contract[flag]) is bool, f"{flag} must be true or false.")
            fields[flag] = contract[flag]
    if "art_source" in contract:
        require(contract["art_source"] in ART_SOURCES, f"art_source must be one of {', '.join(ART_SOURCES)}.")
        fields["art_source"] = contract["art_source"]
    if "body_height_px" in contract:
        require(_finite_number(contract["body_height_px"]) and contract["body_height_px"] > 0,
                "body_height_px must be a positive finite number.")
        fields["body_height_px"] = contract["body_height_px"]
    if "shadow" in contract:
        shadow = contract["shadow"]
        require(isinstance(shadow, dict) and all(_finite_number(shadow.get(axis)) and shadow[axis] >= 0
                                                 for axis in ("rx", "ry")),
                "shadow needs non-negative rx and ry.")
        require("opacity" not in shadow or (_finite_number(shadow["opacity"]) and 0 <= shadow["opacity"] <= 1),
                "shadow opacity must be between 0 and 1.")
        fields["shadow"] = shadow
    if "palette_ref" in contract:
        reference = contract["palette_ref"]
        require(isinstance(reference, str) and bool(reference), "palette_ref must be a manifest-relative path.")
        palette = (manifest.parent / reference).resolve()
        require(palette.is_file(), f"palette_ref {reference!r} was not found next to the manifest.")
        fields["palette_ref"] = FRAME_UTILS.manifest_path(palette, final)
        fields["palette_sha256"] = forge_core.sha256_file(palette)
    if fields.get("pixel_art") and fields.get("sampling") == "linear":
        lint.append(_warning("pixel_art_linear_sampling",
                             "pixel_art is true but sampling is linear; pixel art needs nearest sampling."))
    return fields, lint


# --------------------------------------------------------------------------- clips

def _frame_refs(name: str, references: object, names: dict[str, int], count: int) -> list[int]:
    require(isinstance(references, list) and bool(references), f"Clip {name} must contain frames.")
    indices = []
    for reference in references:
        if type(reference) is int:
            require(0 <= reference < count, f"Clip {name} has an out-of-range frame index.")
            indices.append(reference)
        elif isinstance(reference, str):
            require(reference in names, f"Clip {name} refers to unknown frame {reference!r}.")
            indices.append(names[reference])
        else:
            raise ValueError(f"Clip {name} frame references must be integer indices or names.")
    return indices


def _ms_durations(name: str, timing: object, count: int) -> list[int]:
    durations = [timing] * count if type(timing) is int else timing
    require(isinstance(durations, list) and len(durations) == count,
            f"Clip {name} duration_ms must be an integer or one integer per frame.")
    require(all(type(duration) is int and 0 < duration <= FRAME_UTILS.MAX_WEBP_DURATION for duration in durations),
            f"Clip {name} frame durations must be positive integer milliseconds.")
    return durations


def ticks_to_ms(ticks: list[int], hz: int) -> list[int]:
    """Integer ms per frame whose cumulative boundaries are the tick boundaries rounded half-up.

    The playhead never drifts more than 0.5 ms from the tick grid (forge_core.frame_durations
    generalised to unequal ticks): 5 ticks at 60 Hz give 83, 84, 83 ms.
    """
    edges, elapsed = [0], 0
    for count in ticks:
        elapsed += count
        edges.append(_half_up(elapsed * 1000, hz))
    return [end - start for start, end in zip(edges, edges[1:])]


def _timeline(indices: list[int], durations: list[int], loop: bool) -> dict:
    total = sum(durations)
    return {"frames": indices, "duration_ms": durations, "loop": loop, "total_duration_ms": total,
            "nominal_fps": 1000 / durations[0] if len(set(durations)) == 1 else None,
            "average_fps": len(indices) * 1000 / total}


def _stride(name: str, value: dict, clip: dict) -> None:
    if "stride_world_units" in value:
        stride = value["stride_world_units"]
        require(_finite_number(stride) and stride > 0,
                f"Clip {name} stride_world_units must be a positive finite number.")
        clip.update({"stride_world_units": stride, "stride_source": "user_declared",
                     "nominal_travel_speed_world_units_per_second": stride * 1000 / clip["total_duration_ms"]})


def _loop_policy(name: str, value: dict) -> tuple[bool, str]:
    if "loop" in value:
        require(type(value["loop"]) is bool, f"Clip {name} must declare loop as true or false.")
    if "loop_policy" in value:
        require(value["loop_policy"] in LOOP_POLICIES, f"Clip {name} loop_policy must be cycle, pingpong or oneshot.")
    require("loop" in value or "loop_policy" in value,
            f"Clip {name} must declare loop (true or false) or loop_policy (cycle, pingpong or oneshot).")
    policy = value.get("loop_policy") or ("cycle" if value["loop"] else "oneshot")
    if "loop" in value:
        require(value["loop"] == (policy != "oneshot"), f"Clip {name} loop {value['loop']} contradicts "
                                                          f"loop_policy {policy}.")
    return policy != "oneshot", policy


def playback_order(policy: str, count: int) -> list[int]:
    """Authored positions in playback order: pingpong mirrors without repeating either end frame."""
    if policy == "pingpong" and count > 2:
        return list(range(count)) + list(range(count - 2, 0, -1))
    return list(range(count))


def _valid_event_name(label: object) -> bool:
    return isinstance(label, str) and (label in EVENT_NAMES or _CUSTOM_EVENT.fullmatch(label) is not None)


def _events(name: str, raw: object, order: list[int], starts: list[int], hz: int) -> list[dict]:
    """Events at authored positions, resolved to every played occurrence in whole milliseconds."""
    require(isinstance(raw, list), f"Clip {name} events must be a list of {{at, name}} objects.")
    authored = len(set(order))
    events = []
    for number, event in enumerate(raw):
        require(isinstance(event, dict), f"Clip {name} event {number} must be an object with at and name.")
        at, label = event.get("at"), event.get("name")
        require(type(at) is int and 0 <= at < authored,
                f"Clip {name} event {number}: at must be a clip position from 0 to {authored - 1}; got {at!r}.")
        require(_valid_event_name(label), f"Clip {name} event {number}: unknown event name {label!r}; use "
                                          f"{', '.join(EVENT_NAMES)} or custom:<name>.")
        for position in (index for index, value in enumerate(order) if value == at):
            record = {"name": label, "at_ms": starts[position], "at": at, "position": position,
                      "at_tick": _half_up(starts[position] * hz, 1000)}
            if "data" in event:
                record["data"] = event["data"]
            events.append(record)
    events.sort(key=lambda record: (record["at_ms"], record["position"]))
    return events


def _keys(name: str, raw: object, authored: int, starts: list[int], lint: list[dict]) -> tuple[dict, dict]:
    require(isinstance(raw, dict), f"Clip {name} keys must be an object of clip positions.")
    known = {}
    for key, position in raw.items():
        if key not in KEY_NAMES:
            lint.append(_warning("unknown_key", f"Clip {name} key {key!r} is not one of {', '.join(KEY_NAMES)}; "
                                                "it was ignored.", clip=name))
            continue
        require(type(position) is int and 0 <= position < authored,
                f"Clip {name} key {key} must be a clip position from 0 to {authored - 1}.")
        known[key] = position
    present = [key for key in KEY_NAMES if key in known]
    require(all(known[first] <= known[second] for first, second in zip(present, present[1:])),
            f"Clip {name} keys must not decrease in the order {' <= '.join(KEY_NAMES)}.")
    return {key: known[key] for key in present}, {key: starts[known[key]] for key in present}


def resolve_clip_v2(name: str, value: dict, indices: list[int], tick_hz: int, lint: list[dict]) -> dict:
    """One v2 clip: timing from duration_ms or ticks, loop policy expansion, events, keys and hints."""
    count = len(indices)
    loop, policy = _loop_policy(name, value)
    order = playback_order(policy, count)
    require(("duration_ms" in value) != ("ticks" in value), f"Clip {name} needs duration_ms or ticks, not both.")
    timing: dict = {}
    if "ticks" in value:
        hz = value.get("tick_hz", TICK_HZ)
        require(type(hz) is int and hz >= 1, f"Clip {name} tick_hz must be a positive integer.")
        raw = value["ticks"]
        ticks = [raw] * count if type(raw) is int else raw
        require(isinstance(ticks, list) and len(ticks) == count and all(type(t) is int and t >= 1 for t in ticks),
                f"Clip {name} ticks must be a positive integer or one positive integer per frame.")
        durations = ticks_to_ms([ticks[position] for position in order], hz)
        require(all(duration >= 1 for duration in durations), f"Clip {name} has frames shorter than 1 ms at {hz} Hz.")
        timing = {"timing_source": "ticks", "ticks": ticks, "tick_hz": hz}
        authored_ms = ticks_to_ms(ticks, hz)
    else:
        authored_ms = _ms_durations(name, value["duration_ms"], count)
        durations = [authored_ms[position] for position in order]
        timing = {"timing_source": "duration_ms"}
    require(sum(durations) < (1 << 31), f"Clip {name} exceeds WebP timestamp limits.")
    clip = _timeline([indices[position] for position in order], durations, loop)
    clip["loop_policy"] = policy
    if order != list(range(count)):
        clip.update({"authored_frames": indices, "authored_duration_ms": authored_ms})
    clip.update(timing)
    if timing["timing_source"] == "ticks" and len(set(timing["ticks"])) == 1:
        rate = Fraction(timing["tick_hz"], timing["ticks"][0])
        clip["nominal_fps"] = rate.numerator / rate.denominator
        clip["fps_rational"] = f"{rate.numerator}/{rate.denominator}"
    elif clip["nominal_fps"] is not None:
        clip["fps_rational"] = forge_core.rational_fps(len(durations), sum(durations))
    _stride(name, value, clip)
    starts = np.concatenate([[0], np.cumsum(durations)[:-1]]).astype(int).tolist()
    clip["events_ms"] = _events(name, value.get("events", []), order, starts, tick_hz)
    if "keys" in value:
        clip["keys"], clip["keys_ms"] = _keys(name, value["keys"], count, starts, lint)
    if "entry_frame" in value:
        entry = value["entry_frame"]
        require(type(entry) is int and 0 <= entry < count,
                f"Clip {name} entry_frame must be a clip position from 0 to {count - 1}.")
        clip.update({"entry_frame": entry, "entry_ms": starts[entry]})
    for field in ("stride_px_per_frame", "cadence_ms", "speed_ref"):
        if field in value:
            require(_finite_number(value[field]) and value[field] > 0,
                    f"Clip {name} {field} must be a positive finite number.")
            clip[field] = value[field]
    if "stride_px_per_frame" in clip:
        clip["nominal_speed_px_per_second"] = clip["stride_px_per_frame"] * len(durations) * 1000 / sum(durations)
    if "speed_ref" in clip and "stride_world_units" in clip:
        clip["speed_ref_playback_rate"] = clip["speed_ref"] / clip["nominal_travel_speed_world_units_per_second"]
    if "hitstop_ticks" in value:
        hitstop = value["hitstop_ticks"]
        require(type(hitstop) is int and hitstop >= 0, f"Clip {name} hitstop_ticks must be a whole number of ticks.")
        hz = timing.get("tick_hz", TICK_HZ)
        clip.update({"hitstop_ticks": hitstop, "hitstop_ms": _half_up(hitstop * 1000, hz)})
        if hitstop and not any(event["name"] in ("hit", "impact") for event in clip["events_ms"]):
            lint.append(_warning("hitstop_without_hit", f"Clip {name} declares hitstop_ticks but no hit or impact "
                                                        "event to freeze on.", clip=name))
    if "role" in value:
        require(value["role"] in ROLES, f"Clip {name} role must be one of {', '.join(ROLES)}.")
        clip["role"] = value["role"]
    return clip


def _resolve_transitions(raw_clips: dict, clips: dict, tick_hz: int) -> None:
    for name, value in raw_clips.items():
        if "transitions" not in value:
            continue
        hints = value["transitions"]
        require(isinstance(hints, list), f"Clip {name} transitions must be a list of {{to, entry_frame, "
                                         "dissolve_ms, mode}} objects.")
        resolved = []
        for number, hint in enumerate(hints):
            require(isinstance(hint, dict), f"Clip {name} transition {number} must be an object.")
            target = hint.get("to")
            require(isinstance(target, str) and target in clips,
                    f"Clip {name} transition {number} must name an existing clip in to; got {target!r}.")
            destination = clips[target]
            authored = len(destination.get("authored_frames", destination["frames"]))
            entry = hint.get("entry_frame", destination.get("entry_frame", 0))
            require(type(entry) is int and 0 <= entry < authored,
                    f"Clip {name} transition {number}: entry_frame must be a position of {target} from 0 to "
                    f"{authored - 1}.")
            dissolve = hint.get("dissolve_ms", 0)
            require(type(dissolve) is int and dissolve >= 0,
                    f"Clip {name} transition {number}: dissolve_ms must be whole milliseconds.")
            mode = hint.get("mode", "premultiplied")
            require(mode in TRANSITION_MODES, f"Clip {name} transition {number}: mode must be dither or premultiplied.")
            resolved.append({"to": target, "entry_frame": entry, "entry_ms": sum(destination["duration_ms"][:entry]),
                             "dissolve_ms": dissolve, "dissolve_ticks": _half_up(dissolve * tick_hz, 1000),
                             "mode": mode})
        clips[name]["transition_hints"] = resolved


def resolve_clips(contract: dict, frame_records: list[dict], version: int = 1, *,
                  tick_hz: int = TICK_HZ) -> tuple[dict, dict, list[dict]]:
    """Validate the clips and states; returns ``(clips, states, lint)``.

    v1 clips keep their cfed170 meaning exactly (v2-only fields are ignored with a lint
    warning); v2 clips accept every optional field of the clips_input contract.
    """
    raw_clips = contract.get("clips")
    require(isinstance(raw_clips, dict) and bool(raw_clips), "clips must be a nonempty object.")
    names = {record["name"]: record["index"] for record in frame_records}
    clips: dict = {}
    lint: list[dict] = []
    for name, value in raw_clips.items():
        require(isinstance(name, str) and bool(name.strip()) and isinstance(value, dict),
                "Each clip needs a nonempty name and an object definition.")
        indices = _frame_refs(name, value.get("frames"), names, len(frame_records))
        if version == 2:
            clips[name] = resolve_clip_v2(name, value, indices, tick_hz, lint)
            continue
        durations = _ms_durations(name, value.get("duration_ms"), len(indices))
        require(sum(durations) < (1 << 31), f"Clip {name} exceeds WebP timestamp limits.")
        loop = value.get("loop")
        require(type(loop) is bool, f"Clip {name} must declare loop as true or false.")
        clips[name] = _timeline(indices, durations, loop)
        _stride(name, value, clips[name])
        ignored = [field for field in V2_CLIP_FIELDS if field in value]
        if ignored:
            lint.append(_warning("v2_field_ignored", f"Clip {name} uses v2 fields {', '.join(ignored)}; set schema "
                                                     f"{SCHEMA_V2} to apply them (ignored under v1).", clip=name))
    states = contract.get("states", {})
    require(isinstance(states, dict), "states must map state names to clip names.")
    for state, target in states.items():
        require(isinstance(state, str) and bool(state.strip()) and isinstance(target, str) and target in clips,
                f"State {state!r} must reference an existing clip.")
    if version == 2:
        _resolve_transitions(raw_clips, clips, tick_hz)
    return clips, states, lint


# --------------------------------------------------------------------------- timing and pose diagnostics

def tick_grid_report(durations: list[int], hz: int = TICK_HZ) -> dict:
    """How the integer-ms timeline lands on a fixed tick grid (60 Hz by default).

    Each frame boundary is rounded half-up to the nearest tick; ``max_drift_ms`` is the largest
    distance between a boundary and its tick, ``frame_ticks`` how many ticks each position is
    shown for. Uniform durations with uneven ticks play unevenly; a 0-tick position never shows.
    """
    edges = np.concatenate([[0], np.cumsum(durations)]).astype(int).tolist()
    ticks = [_half_up(edge * hz, 1000) for edge in edges]
    drift = max(abs(edge * hz - tick * 1000) / hz for edge, tick in zip(edges, ticks))
    frame_ticks = [end - start for start, end in zip(ticks, ticks[1:])]
    total = edges[-1]
    return {"hz": hz, "max_drift_ms": round(drift, 3), "frame_ticks": frame_ticks,
            "zero_tick_positions": [position for position, count in enumerate(frame_ticks) if count == 0],
            "even": len(set(frame_ticks)) == 1, "cycle_ticks": round(total * hz / 1000, 4),
            "cycle_aligned": (total * hz) % 1000 == 0,
            "method": "cumulative integer-ms frame boundaries rounded half-up to the nearest tick"}


def _premultiplied(array: np.ndarray) -> np.ndarray:
    pixels = array.astype(np.float32)
    pixels[..., :3] *= pixels[..., 3:] / 255.0
    return pixels


def pose_distance(first: np.ndarray, second: np.ndarray) -> float:
    """Premultiplied RGBA mean absolute difference (0-255) over the union of visible pixels."""
    union = (first[..., 3] > 0) | (second[..., 3] > 0)
    if not union.any():
        return 0.0
    return float(np.abs(_premultiplied(first)[union] - _premultiplied(second)[union]).mean())


def _coarse(array: np.ndarray, cells: int = 32) -> np.ndarray:
    """Block means of the premultiplied frame on a grid of at most ``cells`` x ``cells``."""
    height, width = array.shape[:2]
    block = max(1, math.ceil(max(height, width) / cells))
    padded = np.zeros((math.ceil(height / block) * block, math.ceil(width / block) * block, 4), np.float32)
    padded[:height, :width] = _premultiplied(array)
    return padded.reshape(padded.shape[0] // block, block, padded.shape[1] // block, block, 4).mean(axis=(1, 3)).ravel()


def near_duplicate_pairs(arrays: list[np.ndarray], threshold: float) -> dict[tuple[int, int], float]:
    """Frame pairs whose pose_distance is at most ``threshold``.

    Block means screen the pairs first: their mean absolute difference over the whole canvas
    never exceeds pose_distance, so the screen drops no true pair.
    """
    coarse = np.stack([_coarse(array) for array in arrays])
    pairs = {}
    for first in range(len(arrays) - 1):
        screen = np.abs(coarse[first + 1:] - coarse[first]).mean(axis=1)
        for second in (np.flatnonzero(screen <= threshold) + first + 1).tolist():
            distance = pose_distance(arrays[first], arrays[second])
            if distance <= threshold:
                pairs[(first, second)] = round(distance, 4)
    return pairs


def clip_holds(clip: dict, pairs: dict[tuple[int, int], float]) -> list[dict]:
    """Runs of consecutive positions that show the same pose: repeated indices or near-duplicates."""
    frames, durations = clip["frames"], clip["duration_ms"]
    holds: list[dict] = []
    run: dict | None = None
    for position in range(1, len(frames)):
        before, after = frames[position - 1], frames[position]
        if before == after:
            kind, distance = "repeat", 0.0
        elif (min(before, after), max(before, after)) in pairs:
            kind, distance = "near_duplicate", pairs[(min(before, after), max(before, after))]
        else:
            run = None
            continue
        if run is None:
            run = {"positions": [position - 1], "frames": [before], "kind": "repeat", "max_distance": 0.0}
            holds.append(run)
        run["positions"].append(position)
        run["frames"].append(after)
        run["max_distance"] = max(run["max_distance"], distance)
        if kind == "near_duplicate":
            run["kind"] = "near_duplicate"
    for hold in holds:
        hold["duration_ms"] = sum(durations[position] for position in hold["positions"])
    return holds


def telegraph_report(name: str, clip: dict) -> tuple[list[dict], list[dict]]:
    """Tell-to-hit spans of an enemy clip in 60 Hz ticks, and warnings under TELEGRAPH_MIN_TICKS."""
    events = clip.get("events_ms", [])
    tells = [event for event in events if event["name"] == "tell"]
    hits = [event for event in events if event["name"] == "hit"]
    spans: list[dict] = []
    if tells and hits:
        for tell in tells:
            later = [hit for hit in hits if hit["at_ms"] >= tell["at_ms"]]
            if later:
                hit_ms = later[0]["at_ms"]
            elif clip["loop"]:
                hit_ms = hits[0]["at_ms"] + clip["total_duration_ms"]
            else:
                continue
            spans.append({"tell_ms": tell["at_ms"], "hit_ms": hit_ms, "source": "events"})
    elif {"wind_start", "strike"} <= set(clip.get("keys_ms", {})):
        spans.append({"tell_ms": clip["keys_ms"]["wind_start"], "hit_ms": clip["keys_ms"]["strike"],
                      "source": "keys wind_start -> strike"})
    lint = []
    if hits and not tells:
        lint.append(_warning("telegraph_missing", f"Enemy clip {name} has a hit event but no tell event.", clip=name))
    for span in spans:
        span["ticks_60hz"] = round((span["hit_ms"] - span["tell_ms"]) * 60 / 1000, 3)
        if span["ticks_60hz"] < TELEGRAPH_MIN_TICKS:
            lint.append(_warning("telegraph_short",
                                 f"Enemy clip {name}: tell -> hit is {span['ticks_60hz']:g} ticks at 60 Hz "
                                 f"({span['hit_ms'] - span['tell_ms']} ms); players need at least "
                                 f"{TELEGRAPH_MIN_TICKS} ticks to react.", clip=name,
                                 value_ticks=span["ticks_60hz"], threshold_ticks=TELEGRAPH_MIN_TICKS))
    return spans, lint


def turn_slide_px(array: np.ndarray, anchor_x: float) -> float | None:
    """How far the stance moves when the frame is mirrored about the anchor x (0 = no turn slide)."""
    mask = forge_core.subject_mask(array[..., 3])
    if not mask.any():
        return None
    stance_x, _ = forge_core.anchor_from_mask(mask, "stance", band_rows=max(1, mask.shape[0] // 16))
    return round(abs(2.0 * (anchor_x - stance_x)), 3)


# --------------------------------------------------------------------------- previews and reviews

def review_font(size: int = 13):
    """``ImageFont.load_default(size=...)`` needs Pillow >= 10.1 with FreeType (S13); otherwise
    fall back to Pillow's fixed bitmap font so previews still render on Pillow 10.0."""
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, AttributeError, OSError, ImportError):
        return ImageFont.load_default()


def upscale(array: np.ndarray, scale: int) -> np.ndarray:
    """Integer nearest-neighbour upscale: every source pixel becomes a scale x scale block."""
    return array if scale == 1 else np.repeat(np.repeat(array, scale, axis=0), scale, axis=1)


def parse_backgrounds(value: str) -> list[str]:
    if value == "all":
        return list(BACKGROUNDS)
    if value in BACKGROUNDS:
        return [value]
    require(re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None,
            "--preview-background must be all, light, dark, checker or #rrggbb.")
    return [value.lower()]


def background_tile(kind: str, size: tuple[int, int], square: int) -> Image.Image:
    width, height = size
    if kind == "checker":
        ys, xs = np.mgrid[0:height, 0:width]
        light = ((xs // square + ys // square) % 2 == 0)[..., None]
        return Image.fromarray(np.where(light, CHECKER_COLOURS[0], CHECKER_COLOURS[1]).astype(np.uint8))
    if kind in BACKGROUND_COLOURS:
        return Image.new("RGBA", size, BACKGROUND_COLOURS[kind])
    return Image.new("RGBA", size, tuple(int(kind[i:i + 2], 16) for i in (1, 3, 5)) + (255,))


def _set_native_webp_loop(path: Path, count: int) -> None:
    # The existing timed-static helper emits loop=0; change only the ANIM loop
    # field for a single-play clip. Encoded image payloads are untouched.
    data = bytearray(path.read_bytes())
    position = 12
    while position + 8 <= len(data):
        length = int.from_bytes(data[position + 4:position + 8], "little")
        require(position + 8 + length <= len(data), "Truncated WebP chunk.")
        if data[position:position + 4] == b"ANIM":
            require(length == 6, "Unexpected WebP ANIM chunk.")
            data[position + 12:position + 14] = count.to_bytes(2, "little")
            path.write_bytes(data)
            return
        position += 8 + length + length % 2
    raise ValueError("Timed WebP has no ANIM chunk.")


def encode_clip(path: Path, images: list[Image.Image], arrays: list[np.ndarray], clip: dict, *,
                scale: int = 1) -> dict:
    """Write the clip's lossless WebP (frames upscaled ``scale`` times) and verify the decoded timeline.

    The default libwebp settings come first; a file that fails verification is re-encoded once
    with every frame a keyframe (see assemble_frames.save_animation), and fails the build if
    that copy does not verify either.
    """
    indices, durations = clip["frames"], clip["duration_ms"]
    if scale == 1:
        frames, expected = images, arrays
    else:
        expected = {index: upscale(arrays[index], scale) for index in set(indices)}
        frames = {index: Image.fromarray(array) for index, array in expected.items()}
    selected = [frames[index] for index in indices]
    loop_count = 0 if clip["loop"] else 1
    for all_keyframes in (False, True):
        try:
            decoded_records = _write_clip_webp(path, selected, expected, clip, loop_count, all_keyframes)
            break
        except ValueError:
            if all_keyframes:
                raise
    record = {"decoded_frames": decoded_records, "decoded_frame_count": len(decoded_records),
              "total_duration_ms": sum(durations), "native_loop_count": loop_count,
              "timeline_verified": True, "alpha_and_visible_rgb_exact": True,
              "hidden_rgb_guaranteed": False, "lossless": True,
              "file_sha256": FRAME_UTILS.digest(path.read_bytes())}
    if scale != 1:
        record["scale"] = scale
    if all_keyframes:
        record["all_keyframes"] = True
    return record


def _write_clip_webp(path: Path, selected: list[Image.Image], expected, clip: dict, loop_count: int,
                     all_keyframes: bool) -> list[dict]:
    """One encode-and-verify attempt; returns the decoded frame records or raises ValueError."""
    indices, durations = clip["frames"], clip["duration_ms"]
    FRAME_UTILS.save_animation(path, selected, durations, loop_count, all_keyframes=all_keyframes)
    with Image.open(path) as decoded:
        decoded.load()
        collapsed = decoded.n_frames == 1 and decoded.info.get("duration", 0) <= 0
    if collapsed:
        require(all(FRAME_UTILS.same_visible_pixels(expected[indices[0]], expected[index]) for index in indices),
                "Encoder collapsed visually distinct frames.")
        FRAME_UTILS._timed_static_webp(path, selected[0], sum(durations))
        if loop_count != 0:
            _set_native_webp_loop(path, loop_count)
    boundaries = np.cumsum(durations).tolist()
    elapsed = 0
    decoded_records = []
    with Image.open(path) as decoded:
        require(decoded.size == selected[0].size, "WebP preview changed the shared canvas size.")
        require(decoded.info.get("loop") == loop_count, "WebP loop policy differs from the clip.")
        for index in range(decoded.n_frames):
            decoded.seek(index)
            decoded.load()
            duration = decoded.info.get("duration")
            require(type(duration) is int and duration > 0, "WebP preview lost frame timing.")
            require(decoded.info.get("timestamp") == elapsed, "WebP timestamps are discontinuous.")
            end = elapsed + duration
            require(end <= sum(durations), "WebP playback exceeds the clip duration.")
            actual = FRAME_UTILS.pixel_array(decoded)
            position = elapsed
            matched = []
            while position < end:
                requested = bisect_right(boundaries, position)
                require(FRAME_UTILS.same_visible_pixels(actual, expected[indices[requested]]),
                        f"WebP changed alpha/visible RGB at clip position {requested}.")
                matched.append(requested)
                position = min(end, boundaries[requested])
            decoded_records.append({"index": index, "start_ms": elapsed, "duration_ms": duration,
                                    "clip_positions": matched})
            elapsed = end
    require(elapsed == sum(durations), "WebP total playback duration differs from the clip.")
    return decoded_records


def make_contact_sheet(path: Path, images: list[Image.Image], names: list[str], anchor: list, *,
                       scale: int = 1) -> None:
    width, height = images[0].size[0] * scale, images[0].size[1] * scale
    columns = min(4, math.ceil(math.sqrt(len(images))))
    rows = math.ceil(len(images) / columns)
    cell_width, cell_height = max(width, 180) + 16, height + 48
    canvas = Image.new("RGBA", (columns * cell_width, rows * cell_height + 30), (28, 35, 44, 255))
    draw = ImageDraw.Draw(canvas)
    font = review_font(13)
    draw.text((8, 7), "NATIVE CANVASES | + shared root | diagnostic preview" if scale == 1 else
              f"CANVASES x{scale} (nearest) | + shared root | diagnostic preview", font=font, fill=(232, 236, 239))
    for index, image in enumerate(images):
        col, row = index % columns, index // columns
        left = col * cell_width + (cell_width - width) // 2
        top = 30 + row * cell_height
        draw.rectangle((left, top, left + width - 1, top + height - 1), fill=(40, 50, 61))
        frame = image if scale == 1 else Image.fromarray(upscale(FRAME_UTILS.pixel_array(image), scale))
        canvas.alpha_composite(frame, (left, top))
        x, y = round(left + anchor[0] * scale), round(top + anchor[1] * scale)
        draw.line((x - 4, y, x + 4, y), fill=(255, 220, 80), width=1)
        draw.line((x, y - 4, x, y + 4), fill=(255, 220, 80), width=1)
        draw.text((col * cell_width + 8, top + height + 10), forge_core.ascii_text(f"{index}: {names[index][:22]}"),
                  font=font, fill=(232, 236, 239))
    forge_core.save_png(canvas.convert("RGB"), path)


def _tinted(array: np.ndarray, tint: tuple[int, int, int], opacity: float) -> Image.Image:
    ghost = np.zeros_like(array)
    ghost[..., :3] = tint
    ghost[..., 3] = np.floor(array[..., 3].astype(np.float64) * opacity + 0.5).astype(np.uint8)
    return Image.fromarray(ghost)


def dissolve(first: np.ndarray, second: np.ndarray, weight: float, mode: str) -> np.ndarray:
    """One A->B dissolve step drawn once (hd2d stop-transition-v3): a premultiplied mix, or an
    ordered 4x4 Bayer dither choosing A or B per source pixel. Neither dips in opacity."""
    if mode == "dither":
        height, width = first.shape[:2]
        threshold = np.tile((_BAYER4 + 0.5) / 16.0, (height // 4 + 1, width // 4 + 1))[:height, :width]
        return np.where((threshold < weight)[..., None], second, first)
    return FRAME_UTILS.blend_rgba(first, second, weight)


class ReviewRenderer:
    """Draws the review sheets: frames scaled by ``scale`` on the chosen backgrounds, with the
    shared root as a ground line and an anchor mark, and short ASCII labels."""

    PAD = 8
    LABEL = 32
    TITLE = 24
    MIN_CELL = 112

    def __init__(self, arrays: list[np.ndarray], anchor: list, scale: int, backgrounds: list[str]) -> None:
        self.arrays = arrays
        self.anchor = anchor
        self.scale = scale
        self.backgrounds = backgrounds
        self.height, self.width = arrays[0].shape[:2]
        self.font = review_font(12)
        self.square = max(4, 4 * scale)

    @property
    def frame_size(self) -> tuple[int, int]:
        return self.width * self.scale, self.height * self.scale

    def _scaled(self, array: np.ndarray) -> Image.Image:
        return Image.fromarray(upscale(array, self.scale))

    def _cell_size(self) -> tuple[int, int]:
        frame_w, frame_h = self.frame_size
        return max(frame_w, self.MIN_CELL) + 2 * self.PAD, frame_h + 2 * self.PAD + self.LABEL

    def _ground_and_anchor(self, draw: ImageDraw.ImageDraw, left: int, top: int) -> tuple[int, int]:
        frame_w, frame_h = self.frame_size
        x = left + round(self.anchor[0] * self.scale)
        y = top + round(self.anchor[1] * self.scale)
        draw.line((left, min(y, top + frame_h - 1), left + frame_w - 1, min(y, top + frame_h - 1)),
                  fill=GROUND_LINE, width=1)
        draw.line((x - 3, y, x + 3, y), fill=ANCHOR_MARK, width=1)
        draw.line((x, y - 3, x, y + 3), fill=ANCHOR_MARK, width=1)
        return x, y

    def _layout(self, count: int, bands: int) -> tuple[int, int, int, bool]:
        """Columns, rows and the positions kept so that ``bands`` bands of cells fit MAX_REVIEW_PIXELS."""
        cell_w, cell_h = self._cell_size()
        columns = max(1, min(count, 8))
        keep = count
        while keep > 1 and columns * cell_w * (self.TITLE + bands * (math.ceil(keep / columns) * cell_h + 16)) \
                > MAX_REVIEW_PIXELS:
            keep -= columns if keep > columns else 1
        return columns, math.ceil(keep / columns), keep, keep < count

    def _text(self, draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, limit: int) -> None:
        """ASCII labels only: the Pillow < 10.1 bitmap fallback font cannot encode other text."""
        draw.text(xy, forge_core.ascii_text(text)[:limit], font=self.font, fill=TEXT)

    def _sheet(self, width: int, height: int, title: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        """A review canvas at least as wide as its title, with the title drawn."""
        text = forge_core.ascii_text(title)[:150]
        width = max(width, math.ceil(self.font.getlength(text)) + 2 * self.PAD)
        canvas = Image.new("RGBA", (width, height), SHEET_BASE)
        draw = ImageDraw.Draw(canvas)
        draw.text((self.PAD, 6), text, font=self.font, fill=TEXT)
        return canvas, draw

    def filmstrip(self, path: Path, name: str, clip: dict, labels: list[str]) -> dict:
        positions = clip["frames"]
        backgrounds = list(self.backgrounds)
        columns, rows, keep, truncated = self._layout(len(positions), len(backgrounds))
        if truncated and len(backgrounds) > 1:
            backgrounds = backgrounds[:1]
            columns, rows, keep, truncated = self._layout(len(positions), 1)
        cell_w, cell_h = self._cell_size()
        band = rows * cell_h + 16
        grid = clip["tick_grid"]
        canvas, draw = self._sheet(columns * cell_w, self.TITLE + len(backgrounds) * band,
                                   f"{name}: {len(positions)} positions, {clip['total_duration_ms']} ms, "
                                   f"{clip['loop_policy']}, {grid['hz']} Hz ticks {grid['frame_ticks'][:keep]}")
        frame_w, frame_h = self.frame_size
        for band_index, kind in enumerate(backgrounds):
            band_top = self.TITLE + band_index * band
            self._text(draw, (self.PAD, band_top + 1), f"background: {kind}", 40)
            for position in range(keep):
                col, row = position % columns, position // columns
                left = col * cell_w + (cell_w - frame_w) // 2
                top = band_top + 16 + row * cell_h + self.PAD
                canvas.alpha_composite(background_tile(kind, self.frame_size, self.square), (left, top))
                canvas.alpha_composite(self._scaled(self.arrays[positions[position]]), (left, top))
                self._ground_and_anchor(draw, left, top)
                for line, text in enumerate(labels[position].split("\n")[:2]):
                    self._text(draw, (col * cell_w + self.PAD, top + frame_h + 4 + 13 * line), text, 24)
        forge_core.save_png(canvas.convert("RGB"), path)
        return {"backgrounds": backgrounds, "positions_shown": keep, "truncated": truncated}

    def onion(self, path: Path, name: str, clip: dict) -> dict:
        positions = clip["frames"]
        kind = "checker" if "checker" in self.backgrounds else self.backgrounds[0]
        columns, rows, keep, truncated = self._layout(len(positions), 1)
        cell_w, cell_h = self._cell_size()
        canvas, draw = self._sheet(columns * cell_w, self.TITLE + rows * cell_h + 16,
                                   f"{name}: onion skin, previous = red, next = blue, current on top")
        frame_w, frame_h = self.frame_size
        count = len(positions)
        for position in range(keep):
            col, row = position % columns, position // columns
            left = col * cell_w + (cell_w - frame_w) // 2
            top = self.TITLE + 16 + row * cell_h + self.PAD
            canvas.alpha_composite(background_tile(kind, self.frame_size, self.square), (left, top))
            for neighbour, tint in ((position - 1, PREVIOUS_TINT), (position + 1, NEXT_TINT)):
                if clip["loop"] or 0 <= neighbour < count:
                    ghost = upscale(self.arrays[positions[neighbour % count]], self.scale)
                    canvas.alpha_composite(_tinted(ghost, tint, 0.4), (left, top))
            canvas.alpha_composite(self._scaled(self.arrays[positions[position]]), (left, top))
            self._ground_and_anchor(draw, left, top)
            self._text(draw, (col * cell_w + self.PAD, top + frame_h + 4), f"p{position} f{positions[position]}", 24)
        forge_core.save_png(canvas.convert("RGB"), path)
        return {"background": kind, "positions_shown": keep, "truncated": truncated}

    def turn_test(self, path: Path, entries: list[tuple[str, int, float | None]]) -> dict:
        """Normal row over a row mirrored about the anchor x; a dotted anchor line crosses both rows."""
        frame_w, frame_h = self.frame_size
        anchor_x = self.anchor[0] * self.scale
        offset = round((2 * self.anchor[0] - self.width) * self.scale)
        span_left, span_right = min(0, offset), max(frame_w, offset + frame_w)
        cell_w = max(span_right - span_left, self.MIN_CELL) + 2 * self.PAD
        row_h = frame_h + 2 * self.PAD
        kind = "dark" if "dark" in self.backgrounds else self.backgrounds[0]  # contrast for the yellow marks
        canvas, draw = self._sheet(len(entries) * cell_w, self.TITLE + 2 * row_h + self.LABEL,
                                   "TURN TEST: top normal, bottom mirrored about the anchor x (yellow)")
        cells = []
        for column, (name, frame, slide) in enumerate(entries):
            left = column * cell_w + self.PAD - span_left + (cell_w - 2 * self.PAD - (span_right - span_left)) // 2
            screen_x = left + round(anchor_x)
            tops = [self.TITLE + self.PAD, self.TITLE + row_h + self.PAD]
            array = self.arrays[frame]
            for row, top in enumerate(tops):
                tile_left = left + min(0, offset)
                canvas.alpha_composite(background_tile(kind, (span_right - span_left, frame_h), self.square),
                                       (tile_left, top))
                image = array if row == 0 else np.ascontiguousarray(array[:, ::-1])
                canvas.alpha_composite(self._scaled(image), (left if row == 0 else left + offset, top))
                ground = top + round(self.anchor[1] * self.scale)
                draw.line((tile_left, min(ground, top + frame_h - 1), tile_left + span_right - span_left - 1,
                           min(ground, top + frame_h - 1)), fill=GROUND_LINE, width=1)
                for y in range(top, top + frame_h, 2):
                    canvas.putpixel((screen_x, y), ANCHOR_MARK)
            label = f"{name} f{frame}" + ("" if slide is None else f" slide {slide:g}px")
            self._text(draw, (column * cell_w + self.PAD, self.TITLE + 2 * row_h + 2), label, 28)
            cells.append({"clip": name, "frame": frame, "anchor_screen_x": screen_x,
                          "rows": [[tops[0], tops[0] + frame_h], [tops[1], tops[1] + frame_h]],
                          "mirrored_offset_px": offset, "turn_slide_px": slide})
        forge_core.save_png(canvas.convert("RGB"), path)
        return {"background": kind, "anchor_mark_rgb": list(ANCHOR_MARK[:3]), "cells": cells}

    def transition(self, path: Path, source: str, hint: dict, first: int, second: int) -> dict:
        samples = min(8, max(2, hint["dissolve_ticks"] + 1))
        weights = [step / (samples - 1) for step in range(samples)]
        kind = self.backgrounds[0]
        cell_w, cell_h = self._cell_size()
        frame_w, frame_h = self.frame_size
        canvas, draw = self._sheet(samples * cell_w, self.TITLE + cell_h + 8,
                                   f"{source} -> {hint['to']} ({hint['mode']}, {hint['dissolve_ms']} ms, "
                                   f"entry frame {hint['entry_frame']})")
        for column, weight in enumerate(weights):
            left = column * cell_w + (cell_w - frame_w) // 2
            top = self.TITLE + self.PAD
            mixed = dissolve(self.arrays[first], self.arrays[second], weight, hint["mode"])
            canvas.alpha_composite(background_tile(kind, self.frame_size, self.square), (left, top))
            canvas.alpha_composite(self._scaled(mixed), (left, top))
            self._ground_and_anchor(draw, left, top)
            self._text(draw, (column * cell_w + self.PAD, top + frame_h + 4),
                       f"{round(weight * hint['dissolve_ms'])}ms w{weight:.2f}", 24)
        forge_core.save_png(canvas.convert("RGB"), path)
        return {"from_frame": first, "to_frame": second, "weights": [round(weight, 4) for weight in weights],
                "background": kind}


def _position_labels(clip: dict) -> list[str]:
    events: dict[int, list[str]] = {}
    for event in clip.get("events_ms", []):
        events.setdefault(event["position"], []).append(event["name"])
    held = {position for hold in clip.get("holds", []) if hold["kind"] == "near_duplicate"
            for position in hold["positions"][1:]}
    labels = []
    for position, (frame, duration) in enumerate(zip(clip["frames"], clip["duration_ms"])):
        ticks = clip["tick_grid"]["frame_ticks"][position]
        second = " ".join(events.get(position, []) + (["HOLD"] if position in held else []))
        labels.append(f"p{position} f{frame} {duration}ms {ticks}t\n{second}")
    return labels


def render_reviews(stage: Path, clips: dict, arrays: list[np.ndarray], anchor: list, scale: int,
                   backgrounds: list[str]) -> tuple[dict, list[str]]:
    """Write review/ sheets; returns the manifest record and the written relative paths."""
    review = stage / "review"
    review.mkdir()
    renderer = ReviewRenderer(arrays, anchor, scale, backgrounds)
    record: dict = {"scale": scale, "backgrounds": backgrounds, "filmstrips": {}, "onion": {}, "transitions": []}
    written = []
    entries = []
    for clip_index, (name, clip) in enumerate(clips.items()):
        filmstrip = review / f"filmstrip-{clip_index:02d}.png"
        record["filmstrips"][name] = {"file": filmstrip.relative_to(stage).as_posix(),
                                      **renderer.filmstrip(filmstrip, name, clip, _position_labels(clip))}
        onion = review / f"onion-{clip_index:02d}.png"
        record["onion"][name] = {"file": onion.relative_to(stage).as_posix(), **renderer.onion(onion, name, clip)}
        written += [record["filmstrips"][name]["file"], record["onion"][name]["file"]]
        authored = clip.get("authored_frames", clip["frames"])
        frame = authored[clip.get("entry_frame", 0)]
        entries.append((name, frame, turn_slide_px(arrays[frame], anchor[0])))
        for hint_index, hint in enumerate(clip.get("transition_hints", [])):
            target = clips[hint["to"]]
            second = target.get("authored_frames", target["frames"])[hint["entry_frame"]]
            path = review / f"transition-{clip_index:02d}-{hint_index:02d}.png"
            record["transitions"].append({"file": path.relative_to(stage).as_posix(), "from": name, "to": hint["to"],
                                          **renderer.transition(path, name, hint, clip["frames"][-1], second)})
            written.append(record["transitions"][-1]["file"])
    turn = review / "turn-test.png"
    record["turn_test"] = {"file": turn.relative_to(stage).as_posix(), **renderer.turn_test(turn, entries)}
    written.append(record["turn_test"]["file"])
    return record, written


# --------------------------------------------------------------------------- QA and build

def _qa_envelope(result: dict, lint: list[dict], inputs: list[dict], outputs: list[dict]) -> dict:
    clips = result["clips"]
    codes = {item["code"] for item in lint}
    checks = [
        {"id": "png_byte_copy", "status": "pass", "value": len(result["frames"]), "threshold": None},
        {"id": "webp_timeline", "status": "pass", "value": len(clips), "threshold": None},
        {"id": "tick_grid", "status": "warn" if codes & {"uneven_ticks", "vanishing_frames"} else "pass",
         "value": max(clip["tick_grid"]["max_drift_ms"] for clip in clips.values()), "threshold": None},
    ]
    enemy = [clip for clip in clips.values() if clip.get("role") == "enemy"]
    spans = [span["ticks_60hz"] for clip in enemy for span in clip.get("telegraph", [])]
    checks.append({"id": "telegraph", "threshold": TELEGRAPH_MIN_TICKS,
                   "status": "skipped" if not enemy else ("warn" if codes & {"telegraph_short", "telegraph_missing"}
                                                          else "pass"),
                   "value": min(spans) if spans else None})
    holds = sum(1 for clip in clips.values() for hold in clip["holds"] if hold["kind"] == "near_duplicate")
    checks.append({"id": "near_duplicate_holds", "status": "warn" if holds else "pass", "value": holds,
                   "threshold": 0})
    seams = [clip["seam"]["wrap_ratio"] for clip in clips.values() if clip.get("seam")]
    checks.append({"id": "loop_seam", "status": "needs-visual-review" if seams else "skipped",
                   "value": max(seams) if seams else None, "threshold": None})
    other = [item for item in lint if item["code"] not in {"uneven_ticks", "vanishing_frames", "telegraph_short",
                                                           "telegraph_missing", "near_duplicate_hold"}]
    checks.append({"id": "manifest_lint", "status": "warn" if other else "pass", "value": len(other),
                   "threshold": 0})
    status = "warn" if any(check["status"] == "warn" for check in checks) else "needs-visual-review"
    return {
        "status": status,
        "method": ("Frame PNGs are byte copies (sha256 checked); each WebP preview is decoded and its timing, alpha "
                   "and visible RGB compared with the source frames; tick grid = cumulative integer-ms boundaries "
                   "rounded to the nearest tick; holds = repeated indices or premultiplied union-visible MAE <= "
                   f"{result['diagnostics']['near_duplicate_mae']}; seams = forge_core.seam_report with wrap_ratio "
                   "= seam / mean adjacent step; telegraph = tell -> hit in 60 Hz ticks."),
        "notProven": ["animation quality, gait, anatomy or identity",
                      "physical root stability or foot contact (anchor_px is declared, not measured)",
                      "runtime playback on a device (previews are verified files, not a game loop)",
                      "that near-duplicate thresholds suit every art style (heuristic)"],
        "checks": checks, "inputs": inputs, "outputs": outputs, "tool": dict(TOOL),
    }


def annotate_clips(clips: dict, records: list[dict], arrays: list[np.ndarray],
                   pairs: dict[tuple[int, int], float], tick_hz: int) -> list[dict]:
    """Add the per-clip diagnostics (tick grid, holds, loop seam, telegraph) and per-frame
    near-duplicates and holds in place; returns the lint warnings they raise."""
    lint: list[dict] = []
    for index, record in enumerate(records):
        record["near_duplicates"] = sorted(second if first == index else first
                                           for first, second in pairs if index in (first, second))
        record["holds"] = []
    for name, clip in clips.items():
        clip.setdefault("loop_policy", "cycle" if clip["loop"] else "oneshot")
        clip.setdefault("events_ms", [])
        grid = clip["tick_grid"] = tick_grid_report(clip["duration_ms"], tick_hz)
        if grid["zero_tick_positions"]:
            lint.append(_warning("vanishing_frames", f"Clip {name}: positions {grid['zero_tick_positions']} last less "
                                                     f"than half a tick at {tick_hz} Hz and never display.", clip=name))
        if len(set(clip["duration_ms"])) == 1 and not grid["even"]:
            lint.append(_warning("uneven_ticks", f"Clip {name}: {clip['duration_ms'][0]} ms frames show for "
                                                 f"{sorted(set(grid['frame_ticks']))} ticks at {tick_hz} Hz; even "
                                                 f"playback needs multiples of 1000/{tick_hz} ms (author ticks).",
                                 clip=name))
        clip["holds"] = clip_holds(clip, pairs)
        for hold in clip["holds"]:
            for frame in sorted(set(hold["frames"])):
                records[frame]["holds"].append({"clip": name, "positions": hold["positions"], "kind": hold["kind"]})
            if hold["kind"] == "near_duplicate":
                lint.append(_warning("near_duplicate_hold", f"Clip {name} holds near-identical frames "
                                                            f"{hold['frames']} at positions {hold['positions']} "
                                                            f"({hold['duration_ms']} ms); merge them into one frame "
                                                            "with the summed duration or redraw the pose.", clip=name))
        if clip["loop"] and len(clip["frames"]) >= 2:
            clip["seam"] = FRAME_UTILS.loop_seam([arrays[index] for index in clip["frames"]])
        if clip.get("role") == "enemy":
            clip["telegraph"], telegraph_lint = telegraph_report(name, clip)
            lint += telegraph_lint
    return lint


def build(manifest_path: Path, output_dir: Path, *, preview_scale: int = 1, preview_background: str = "all",
          reviews: bool = True, tick_hz: int = TICK_HZ, near_duplicate_mae: float = NEAR_DUPLICATE_MAE,
          strict: bool = False) -> dict:
    """Validate the manifest and frames, then publish the bundle to the new ``output_dir``.

    Everything is written into a stage beside ``output_dir`` and published only after the
    previews are decoded and verified (with ``strict``, only when no QA check warns); any
    failure leaves nothing behind. Returns the animation-clips.json document.
    """
    require(not os.path.lexists(output_dir), f"Refusing existing output directory: {output_dir}")
    require(type(preview_scale) is int and preview_scale >= 1, "--preview-scale must be a positive integer.")
    require(type(tick_hz) is int and tick_hz >= 1, "--tick-hz must be a positive integer.")
    require(_finite_number(near_duplicate_mae) and near_duplicate_mae >= 0,
            "--near-duplicate-mae must be zero or a positive number.")
    backgrounds = parse_backgrounds(preview_background)
    final = FRAME_UTILS.output_path(output_dir)
    source_manifest = manifest_path.read_bytes()
    contract, version, images, arrays, records, frame_paths = load_contract(manifest_path)
    width, height = images[0].size
    require(width * preview_scale <= MAX_WEBP_SIDE and height * preview_scale <= MAX_WEBP_SIDE,
            f"--preview-scale {preview_scale} makes {width * preview_scale}x{height * preview_scale} previews; "
            f"WebP allows at most {MAX_WEBP_SIDE} px per side.")
    clips, states, lint = resolve_clips(contract, records, version, tick_hz=tick_hz)
    top_level, top_lint = resolve_top_level(contract, manifest_path, final) if version == 2 else ({}, [])
    lint += top_lint
    if version == 1:
        ignored = [field for field in V2_TOP_FIELDS if field in contract]
        if ignored:
            lint.append(_warning("v2_field_ignored", f"The manifest uses v2 fields {', '.join(ignored)}; set schema "
                                                     f"{SCHEMA_V2} to apply them (ignored under v1)."))
    anchor = contract["anchor_px"]
    visible_hashes: dict[str, list[int]] = {}
    for index, array in enumerate(arrays):
        canonical = array.copy()
        canonical[canonical[..., 3] == 0, :3] = 0
        visible_hashes.setdefault(FRAME_UTILS.digest(canonical.tobytes()), []).append(index)
    duplicates = [group for group in visible_hashes.values() if len(group) > 1]
    pairs = near_duplicate_pairs(arrays, near_duplicate_mae)
    lint += annotate_clips(clips, records, arrays, pairs, tick_hz)
    with forge_core.staged_output(final) as stage:
        (stage / "frames").mkdir()
        (stage / "clips").mkdir()
        for index, record in enumerate(records):
            destination = stage / "frames" / f"frame-{index:02d}.png"
            shutil.copyfile(frame_paths[index], destination)
            require(FRAME_UTILS.digest(destination.read_bytes()) == record["source"]["file_sha256"],
                    "Source changed while copying; no output will be published.")
            record["source"] = {"path": FRAME_UTILS.manifest_path(frame_paths[index], final), **record["source"]}
            record["file"] = destination.relative_to(stage).as_posix()
        for clip_index, (name, clip) in enumerate(clips.items()):
            indices = clip["frames"]
            preview = stage / "clips" / f"clip-{clip_index:02d}.webp"
            clip["preview"] = {"file": preview.relative_to(stage).as_posix(),
                               **encode_clip(preview, images, arrays, clip, scale=preview_scale)}
            clip["transitions"] = [
                {"from_position": position, "to_position": position + 1,
                 **FRAME_UTILS.transition_metrics(arrays[indices[position]], arrays[indices[position + 1]])}
                for position in range(len(indices) - 1)
            ]
            clip["last_to_first"] = {"applies_to_playback": clip["loop"],
                                     **FRAME_UTILS.transition_metrics(arrays[indices[-1]], arrays[indices[0]])}
        contact = stage / "contact-sheet.png"
        make_contact_sheet(contact, images, [record["name"] for record in records], anchor, scale=preview_scale)
        review, review_files = render_reviews(stage, clips, arrays, anchor, preview_scale, backgrounds) \
            if reviews else (None, [])
        result = {
            "schema": SCHEMA_V2 if version == 2 else SCHEMA_V1,
            "source_manifest": {"path": FRAME_UTILS.manifest_path(manifest_path, final),
                                "file_sha256": FRAME_UTILS.digest(source_manifest), "copy": "source-manifest.json"},
            "frame_size": [width, height], "anchor_px": anchor,
            "anchor_semantics": "shared canvas/root origin, often a ground reference; not per-frame visible bottom",
            **top_level,
            "frames": records, "clips": clips, "states": states,
            "diagnostics": {"visible_pixel_duplicate_groups": duplicates, "visual_approval": False,
                            "note": "Pixel differences and duplicates do not establish animation quality or physical root stability.",
                            "near_duplicate_mae": near_duplicate_mae,
                            "near_duplicate_pairs": [[first, second, distance]
                                                     for (first, second), distance in sorted(pairs.items())],
                            "tick_hz": tick_hz, "lint": lint},
            "contact_sheet": {"file": "contact-sheet.png", "native_scale": preview_scale == 1, "scale": preview_scale,
                              "annotated_with_shared_root": True, "not_a_runtime_atlas": True,
                              "file_sha256": FRAME_UTILS.digest(contact.read_bytes())},
            "review": review,
            "processing": {"source_pngs_byte_identical": True, "resized": False, "cropped": False,
                           "per_frame_alignment": False, "chroma_keyed": False},
            "alpha_validation": "Every input has fully transparent and visible pixels; edge quality still requires visual review.",
        }
        inputs = [FRAME_UTILS.file_ref(manifest_path, final)] + [FRAME_UTILS.file_ref(path, final)
                                                                 for path in dict.fromkeys(frame_paths)]
        written = ([record["file"] for record in records] + [clip["preview"]["file"] for clip in clips.values()]
                   + ["contact-sheet.png"] + review_files)
        outputs = [{"path": name, "sha256": forge_core.sha256_file(stage / name), "bytes": (stage / name).stat().st_size}
                   for name in written]
        result["qa"] = _qa_envelope(result, lint, inputs, outputs)
        if strict:
            require(not lint, f"--strict: {len(lint)} QA warning(s): "
                              f"{'; '.join(item['message'] for item in lint[:3])}. Nothing was published.")
        (stage / "source-manifest.json").write_bytes(source_manifest)
        forge_core.write_json(stage / "animation-clips.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True, help="Clips manifest (v1 or v2 JSON).")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New output directory; never overwrite existing paths.")
    parser.add_argument("--preview-scale", type=int, default=1, metavar="N",
                        help="Integer nearest-neighbour scale of the WebP previews, contact sheet and reviews "
                             "(default 1).")
    parser.add_argument("--preview-background", default="all",
                        help="Review backgrounds: all (light, dark and checker; default), light, dark, checker "
                             "or #rrggbb.")
    parser.add_argument("--no-reviews", action="store_true", help="Skip the review/ sheets.")
    parser.add_argument("--tick-hz", type=int, default=TICK_HZ,
                        help=f"Runtime tick rate for the drift report and event ticks (default {TICK_HZ}).")
    parser.add_argument("--near-duplicate-mae", type=float, default=NEAR_DUPLICATE_MAE,
                        help="Hold threshold: premultiplied RGBA MAE over visible pixels, 0-255 "
                             f"(default {NEAR_DUPLICATE_MAE}).")
    parser.add_argument("--strict", action="store_true", help="Fail and publish nothing on any QA warning.")
    return parser


def main(argv: list[str] | None = None) -> int:
    forge_core.utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = build(args.manifest, args.output_dir, preview_scale=args.preview_scale,
                       preview_background=args.preview_background, reviews=not args.no_reviews,
                       tick_hz=args.tick_hz, near_duplicate_mae=args.near_duplicate_mae, strict=args.strict)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    final = FRAME_UTILS.output_path(args.output_dir)
    summary = {"status": "ok", "output": str(final), "metadata": str(final / "animation-clips.json"),
               "schema": result["schema"], "clips": len(result["clips"]), "frames": len(result["frames"]),
               "warnings": len(result["diagnostics"]["lint"]), "qa": result["qa"]["status"]}
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
