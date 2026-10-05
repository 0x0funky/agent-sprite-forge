"""Export built sprite clips to engine formats: Aseprite JSON, Godot 4 SpriteFrames and Sprite3D.

Reads animation-clips.json written by build_animation_clips.py (schema
generate2dsprite.animation_clips.v1 or .v2) and the frames it lists, then
writes a new output directory:

  aseprite/<name>.json + .png        Aseprite-style JSON array atlas: one frameTag
                                     per clip, an "anchor" slice with the pivot,
                                     per-frame durations and events in the tag
                                     user data; Phaser, PixiJS and the Unity/Godot
                                     Aseprite-JSON importers read this layout
  godot/<name>.tres + .tscn + PNGs   Godot 4 SpriteFrames (relative_duration =
                                     ms x speed / 1000) and an AnimatedSprite2D
                                     scene carrying the anchor offset and filter
  godot-sprite3d/                    AnimatedSprite3D scene over per-frame PNGs,
                                     plus generate2dsprite.godot_sprite3d.v1
                                     contracts whose frame paths are relative (S19)
  engine-export.json                 files, per-clip timing and events, the engine
                                     mapping table and the QA envelope

Every output is parsed back and compared with the source frames (exact RGBA),
the integer durations, loops, events and the shared anchor before the
directory is published; a failed round trip leaves nothing behind. Imports
into Aseprite, Godot, Phaser, PixiJS or Unity are NOT verified by this tool.

Positions follow sprite.schema.json builtClip (D12): frames and duration_ms are
the played timeline (a pingpong clip arrives expanded, its authored order in
authored_frames); events_ms[].at_ms is authoritative, position is the index
into the played frames (taken when given, otherwise found from at_ms on the
duration edges, and checked against at_ms) and at is the authored position.
ticks, keys and entry_frame stay authored positions; Godot plays the authored
ticks mapped onto the played frames. Transition hints are read from
transition_hints, falling back to legacy transitions items that name a target
clip with to (D13), and are written as transition_hints.

Usage (from the project root):
  python export_engine.py --clips out/hero/animation-clips.json --target all --output-dir out/hero-engine
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)

TOOL_NAME = "export_engine.py"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # QA envelopes record the package version (D29)
SOURCE_SCHEMAS = ("generate2dsprite.animation_clips.v1", "generate2dsprite.animation_clips.v2")
EXPORT_SCHEMA = "generate2dsprite.engine_export.v1"
SPRITE3D_SCHEMA = "generate2dsprite.godot_sprite3d.v1"
SPRITE3D_BUNDLE_SCHEMA = "generate2dsprite.godot_sprite3d_bundle.v1"
TARGETS = ("aseprite-json", "godot-spriteframes", "godot-sprite3d")
ATLAS_TARGETS = ("aseprite-json", "godot-spriteframes")  # godot-sprite3d uses per-frame textures
TARGET_DIRS = {"aseprite-json": "aseprite", "godot-spriteframes": "godot", "godot-sprite3d": "godot-sprite3d"}
MAX_ATLAS_SIZE = 4096
GODOT_DEFAULT_PIXEL_SIZE = 0.01
# Godot enums: CanvasItem.TextureFilter (2D); BaseMaterial3D.TextureFilter and BillboardMode (3D).
GODOT_FILTER_2D = {"nearest": 1, "linear": 2}
GODOT_FILTER_3D = {"nearest": 0, "linear": 3}
GODOT_BILLBOARD = {"disabled": 0, "enabled": 1, "fixed-y": 2}
# Passed through to engine-export.json as the builder wrote them. ticks, keys and entry_frame are
# authored positions (D12), indices into authored_frames; keys_ms, entry_ms and hitstop_ms are their times.
CLIP_EXTRAS = ("keys", "keys_ms", "entry_frame", "entry_ms", "stride_world_units", "stride_px_per_frame",
               "cadence_ms", "speed_ref", "hitstop_ticks", "hitstop_ms", "role", "tick_grid", "ticks", "tick_hz",
               "authored_frames")
TOP_LEVEL_EXTRAS = ("sampling", "pixel_art", "body_height_px", "art_source", "placeholder", "shadow")
# common.schema.json eventName.
EVENT_NAMES = ("in", "tell", "hit", "active_end", "cancel", "chain", "impact", "hold", "end", "sfx", "step_l", "step_r")
CUSTOM_EVENT = re.compile(r"custom:\S+")
TRANSITION_MODES = ("dither", "premultiplied")
ROUND_TRIP_MS_TOLERANCE = 1e-6
TICK_MS_TOLERANCE = 1.0
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
ACTION_INVALID = re.compile(r"[^a-z0-9_-]+")


class ExportError(ValueError):
    """A user-facing failure: bad input, an impossible layout or a failed round trip."""


class NoVisibleSubject(ExportError):
    """The reference clip has no pixel above BODY_ALPHA_THRESHOLD (faint FX), so no subject height."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ExportError(message)


def _is_int(value: object, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _event_name(value: object) -> bool:
    return isinstance(value, str) and (value in EVENT_NAMES or CUSTOM_EVENT.fullmatch(value) is not None)


# --------------------------------------------------------------------------- source model

@dataclass
class Frame:
    index: int
    path: Path
    pixels: np.ndarray            # canonical RGBA: RGB zeroed where alpha is 0
    key: str                      # sha256 of the canonical pixels (atlas de-duplication)
    hash_verified: bool


@dataclass
class Clip:
    name: str
    frames: list[int]             # source frame index per played position (pingpong arrives expanded)
    durations: list[int]          # integer ms per played position
    loop: bool
    loop_policy: str
    events: list[dict]            # {name, at_ms, at (authored), position (played), data?}
    extras: dict                  # keys, entry_frame, stride..., hitstop_ticks, role, ticks, tick_hz, ...
    hints: list[dict]             # transition hints {to, entry_frame?, dissolve_ms?, mode?, ...}
    order: list[int] = field(default_factory=list)  # authored position per played position (D12)

    def __post_init__(self) -> None:
        if not self.order:
            self.order = list(range(len(self.frames)))

    @property
    def total_ms(self) -> int:
        return sum(self.durations)

    @property
    def authored_count(self) -> int:
        return max(self.order) + 1


@dataclass
class Sprite:
    manifest: Path
    schema: str
    frame_size: tuple[int, int]
    anchor: tuple[float, float]
    frame_count: int              # frame records in the manifest, used or not
    frames: dict[int, Frame]      # only the frames some clip uses
    clips: list[Clip]
    states: dict
    top: dict                     # TOP_LEVEL_EXTRAS present in the manifest

    @property
    def default_clip(self) -> str:
        """The idle state's clip when there is one, else the first clip (Godot autoplay, bundle default)."""
        idle = self.states.get("idle")
        return idle if idle in {clip.name for clip in self.clips} else self.clips[0].name


def _manifest_file(manifest_dir: Path, text: object, label: str) -> Path:
    require(isinstance(text, str) and text.strip() != "", f"{label} needs a manifest-relative file path")
    require("\\" not in text and re.match(r"^(/|[A-Za-z][A-Za-z0-9+.-]*:)", text) is None,
            f"{label} must be a manifest-relative POSIX path, got {text!r}")
    path = manifest_dir / text
    require(path.is_file(), f"{label} file does not exist: {text}")
    return path


def _load_frame(manifest_dir: Path, record: dict, size: tuple[int, int]) -> Frame:
    position = record["index"]
    path = _manifest_file(manifest_dir, record.get("file"), f"frame {position}")
    image, _info = forge_core.load_rgba(path)
    require(image.size == size, f"frame {position} is {image.size[0]}x{image.size[1]}, expected {size[0]}x{size[1]}")
    raw = np.asarray(image, dtype=np.uint8)
    expected = record.get("rgba_pixel_sha256")
    if expected is not None:
        require(forge_core.sha256_bytes(raw.tobytes()) == expected,
                f"frame {position} ({record['file']}) no longer matches the rgba_pixel_sha256 recorded when the "
                "clips were built; rebuild the clips")
    pixels = raw.copy()
    pixels[pixels[..., 3] == 0] = 0
    return Frame(position, path, pixels, forge_core.sha256_bytes(pixels.tobytes()), expected is not None)


def played_order(policy: str, count: int) -> list[int]:
    """Authored positions in playback order: pingpong mirrors without repeating either end frame
    (build_animation_clips.playback_order)."""
    if policy == "pingpong" and count > 2:
        return list(range(count)) + list(range(count - 2, 0, -1))
    return list(range(count))


def _clip_order(name: str, value: dict, frames: list[int], policy: str) -> list[int]:
    """The authored position of every played position (D12). A built pingpong clip lists its played
    frames and keeps the authored ones in authored_frames; every other clip plays its authored order."""
    authored = value.get("authored_frames")
    if authored is None:
        return list(range(len(frames)))
    require(isinstance(authored, list) and len(authored) > 0 and all(_is_int(i) for i in authored),
            f"clip {name} authored_frames must list frame indices")
    order = played_order(policy, len(authored))
    require([authored[position] for position in order] == frames,
            f"clip {name} frames are not the {policy} playback of its authored_frames")
    return order


def _clip_events(name: str, value: object, durations: list[int], order: list[int]) -> list[dict]:
    """events_ms per D12: at_ms is authoritative; position, the index into the played frames, is taken
    when given and otherwise found from at_ms on the duration edges (an event at the end edge names the
    last frame); at is the authored position. A given position or at must agree with at_ms."""
    if value is None:
        return []
    require(isinstance(value, list), f"clip {name} events_ms must be a list")
    edges = np.cumsum([0, *durations])
    total = int(edges[-1])
    events = []
    for index, event in enumerate(value):
        require(isinstance(event, dict) and _event_name(event.get("name")),
                f"clip {name} event {index} needs a name from common/eventName "
                f"({', '.join(EVENT_NAMES)} or custom:<name>)")
        label = event["name"]
        at_ms = event.get("at_ms")
        require(_is_int(at_ms) and at_ms <= total,
                f"clip {name} event {label} needs integer at_ms inside the clip (0..{total})")
        position = min(len(durations) - 1, int(np.searchsorted(edges, at_ms, side="right")) - 1)
        given = event.get("position")
        require(given is None or (_is_int(given) and given == position),
                f"clip {name} event {label}: position {given!r} disagrees with at_ms {at_ms}, which falls in "
                f"played frame {position}")
        at = event.get("at")
        require(at is None or (_is_int(at) and at == order[position]),
                f"clip {name} event {label}: at {at!r} disagrees with at_ms {at_ms}, which shows authored "
                f"position {order[position]} (at is the authored position, position the played one)")
        item = {"name": label, "at_ms": at_ms, "at": order[position], "position": position}
        if "data" in event:
            item["data"] = event["data"]
        events.append(item)
    return sorted(events, key=lambda item: (item["at_ms"], item["position"]))


def _clip_hints(name: str, value: dict) -> list[dict]:
    """Transition hints (D13): transition_hints when present, else the legacy transitions items that
    name a target clip with to (built clips keep transitions for the frame-to-frame metrics)."""
    raw = value.get("transition_hints")
    if raw is None:
        raw = [item for item in value.get("transitions") or [] if isinstance(item, dict)
               and isinstance(item.get("to"), str)]
    require(isinstance(raw, list), f"clip {name} transition_hints must be a list")
    hints = []
    for index, hint in enumerate(raw):
        require(isinstance(hint, dict) and isinstance(hint.get("to"), str) and hint["to"] != "",
                f"clip {name} transition hint {index} must name its target clip in to")
        for key in ("entry_frame", "dissolve_ms"):
            require(key not in hint or _is_int(hint[key]),
                    f"clip {name} transition hint {index}: {key} must be a whole number >= 0")
        require(hint.get("mode", "dither") in TRANSITION_MODES,
                f"clip {name} transition hint {index}: mode must be dither or premultiplied")
        hints.append(dict(hint))
    return hints


def _load_clip(name: str, value: object, frame_count: int) -> Clip:
    require(name.strip() != "" and name.isprintable(), f"invalid clip name {name!r}")
    require(isinstance(value, dict), f"clip {name} must be an object")
    frames = value.get("frames")
    require(isinstance(frames, list) and len(frames) > 0 and all(_is_int(i) and i < frame_count for i in frames),
            f"clip {name} frames must be resolved frame indices")
    durations = value.get("duration_ms")
    require(isinstance(durations, list) and len(durations) == len(frames) and all(_is_int(d, 1) for d in durations),
            f"clip {name} duration_ms must list one positive integer per frame")
    require(isinstance(value.get("loop"), bool), f"clip {name} must declare loop")
    policy = value.get("loop_policy") or ("cycle" if value["loop"] else "oneshot")
    require(policy in ("cycle", "pingpong", "oneshot"), f"clip {name} has unknown loop_policy {policy!r}")
    order = _clip_order(name, value, list(frames), policy)
    extras = {key: value[key] for key in CLIP_EXTRAS if key in value}
    events = _clip_events(name, value.get("events_ms"), durations, order)
    return Clip(name, list(frames), list(durations), value["loop"], policy, events, extras, _clip_hints(name, value),
                order)


def _check_references(clips: list[Clip], states: object) -> dict:
    """states map names to clips of this manifest; hints name a clip and enter it at one of its authored
    positions (entry_frame is authored, D12)."""
    by_name = {clip.name: clip for clip in clips}
    if states is None:
        states = {}
    require(isinstance(states, dict) and all(isinstance(state, str) and state != "" and isinstance(target, str)
                                             and target in by_name for state, target in states.items()),
            "states must map state names to clip names of this manifest")
    for clip in clips:
        for hint in clip.hints:
            target = by_name.get(hint["to"])
            require(target is not None,
                    f"clip {clip.name} has a transition hint to {hint['to']!r}, which is not a clip")
            entry = hint.get("entry_frame", 0)
            require(entry < target.authored_count,
                    f"clip {clip.name} transition hint to {target.name}: entry_frame {entry} is not one of its "
                    f"{target.authored_count} authored positions")
    return dict(states)


def load_clips(path: Path) -> Sprite:
    """Read and verify a built animation-clips.json and every frame its clips use."""
    require(path.is_file(), f"clips manifest not found: {path}")
    try:
        data = forge_core.read_json(path, strict=True)  # BOM-tolerant (D28); no NaN, Infinity or duplicate keys
    except ValueError as error:
        raise ExportError(f"{path.name} is not usable JSON: {error}") from None
    require(isinstance(data, dict), "the clips manifest must be a JSON object")
    require(data.get("schema") in SOURCE_SCHEMAS,
            f"unsupported schema {data.get('schema')!r}; expected animation-clips.json v1 or v2")
    records = data.get("frames")
    require(isinstance(records, list) and len(records) > 0, "frames must be a non-empty list")
    if "frame_size" not in data or any(not isinstance(item, dict) or "index" not in item for item in records):
        raise ExportError("this is a clips input manifest, not a built one; run build_animation_clips.py "
                          "--manifest <clips.json> --output-dir <dir> and export its animation-clips.json")
    require(all(record["index"] == position for position, record in enumerate(records)),
            "frame records must be listed in index order")
    size = data["frame_size"]
    require(isinstance(size, list) and len(size) == 2 and all(_is_int(v, 1) for v in size),
            "frame_size must be two positive integers")
    anchor = data.get("anchor_px")
    require(isinstance(anchor, list) and len(anchor) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in anchor)
            and 0 <= anchor[0] <= size[0] and 0 <= anchor[1] <= size[1],
            "anchor_px must be two finite numbers inside the frame")
    raw_clips = data.get("clips")
    require(isinstance(raw_clips, dict) and len(raw_clips) > 0, "clips must be a non-empty object")
    clips = [_load_clip(name, value, len(records)) for name, value in raw_clips.items()]
    states = _check_references(clips, data.get("states"))
    used = sorted({index for clip in clips for index in clip.frames})
    frames = {index: _load_frame(path.parent, records[index], (size[0], size[1])) for index in used}
    top = {key: data[key] for key in TOP_LEVEL_EXTRAS if key in data}
    return Sprite(path, data["schema"], (size[0], size[1]), (float(anchor[0]), float(anchor[1])), len(records),
                  frames, clips, states, top)


# --------------------------------------------------------------------------- atlas packing

@dataclass
class Page:
    index: int
    cells: dict[str, tuple[int, int]] = field(default_factory=dict)   # frame key -> frame rect origin
    clips: list[Clip] = field(default_factory=list)
    size: tuple[int, int] = (0, 0)
    pixels: np.ndarray | None = None


def plan_pages(sprite: Sprite, max_size: int, padding: int, extrude: int) -> list[Page]:
    """Pack whole clips into pages of at most max_size px; identical frames share one cell.

    Cells form a grid: `padding` transparent px between and around them, and
    `extrude` px of replicated edge pixels around each frame rect.
    """
    width, height = sprite.frame_size
    block_w, block_h = width + 2 * extrude, height + 2 * extrude
    max_columns = (max_size - padding) // (block_w + padding)
    max_rows = (max_size - padding) // (block_h + padding)
    require(max_columns >= 1 and max_rows >= 1,
            f"a {width}x{height} frame with padding {padding} and extrude {extrude} does not fit a {max_size} px atlas")
    capacity = max_columns * max_rows
    pages = [Page(0)]
    for clip in sprite.clips:
        wanted = list(dict.fromkeys(sprite.frames[i].key for i in clip.frames))
        require(len(wanted) <= capacity,
                f"clip {clip.name} needs {len(wanted)} distinct frames but a {max_size} px page holds {capacity}; "
                "split the clip or export smaller frames")
        if len(pages[-1].cells) + len(set(wanted) - set(pages[-1].cells)) > capacity:
            pages.append(Page(len(pages)))
        for key in wanted:
            pages[-1].cells.setdefault(key, (0, 0))
        pages[-1].clips.append(clip)
    pixels_of = {frame.key: frame.pixels for frame in sprite.frames.values()}
    for page in pages:
        columns = min(max_columns, math.ceil(math.sqrt(len(page.cells))))
        if math.ceil(len(page.cells) / columns) > max_rows:
            columns = max_columns
        rows = math.ceil(len(page.cells) / columns)
        page.size = (padding + columns * (block_w + padding), padding + rows * (block_h + padding))
        page.pixels = np.zeros((page.size[1], page.size[0], 4), np.uint8)
        for slot, key in enumerate(page.cells):
            x = padding + (slot % columns) * (block_w + padding)
            y = padding + (slot // columns) * (block_h + padding)
            page.pixels[y:y + block_h, x:x + block_w] = np.pad(
                pixels_of[key], ((extrude, extrude), (extrude, extrude), (0, 0)), mode="edge")
            page.cells[key] = (x + extrude, y + extrude)
    return pages


# --------------------------------------------------------------------------- timing, scale and mapping

def played_ticks(clip: Clip) -> list[int] | None:
    """The clip's ticks per played frame, or None. Built clips keep ticks per authored position (D12): a
    pingpong clip of 3 authored poses has 3 ticks and 4 played frames, mapped through the playback order."""
    ticks = clip.extras.get("ticks")
    if _is_int(ticks, 1):
        return [ticks] * len(clip.durations)
    if isinstance(ticks, list) and len(ticks) == clip.authored_count and all(_is_int(t, 1) for t in ticks):
        return [ticks[position] for position in clip.order]
    return None


def godot_timing(clip: Clip, fixed_fps: float | None) -> dict:
    """Godot speed (fps) and per-frame relative durations, with relative_duration = ms x speed / 1000.

    A fixed --godot-fps applies to every clip. Otherwise a clip that carries
    exact integer ticks at tick_hz (consistent with its ms) plays at the tick
    rate with relative durations = its ticks mapped onto the played frames;
    every other clip uses its shortest frame as 1.0.
    """
    if fixed_fps:
        return {"speed": fixed_fps, "relative_durations": [d * fixed_fps / 1000 for d in clip.durations],
                "basis": "fixed fps"}
    ticks, hz = played_ticks(clip), clip.extras.get("tick_hz", 60)
    if (ticks is not None and _is_int(hz, 1)
            and all(abs(t * 1000 / hz - d) <= TICK_MS_TOLERANCE for t, d in zip(ticks, clip.durations))):
        return {"speed": float(hz), "relative_durations": [float(t) for t in ticks], "basis": f"ticks at {hz} Hz"}
    base = min(clip.durations)
    return {"speed": 1000 / base, "relative_durations": [d / base for d in clip.durations],
            "basis": "shortest frame = 1.0"}


def resolve_sampling(choice: str, top: dict) -> str:
    if choice != "auto":
        return choice
    if top.get("sampling") in ("nearest", "linear"):
        return top["sampling"]
    return "nearest" if top.get("pixel_art") is True else "linear"


def reference_subject_height(sprite: Sprite, clip_name: str | None, override: float | None) -> tuple[float, str]:
    """Subject height in source px: the override, else the named clip's median visible height, else
    body_height_px, else the default clip's median visible height (alpha above BODY_ALPHA_THRESHOLD)."""
    if override is not None:
        return float(override), "--subject-height-px"
    clips = {clip.name: clip for clip in sprite.clips}
    require(clip_name is None or clip_name in clips, f"--reference-clip {clip_name!r} is not a clip of this manifest")
    body = sprite.top.get("body_height_px")
    declared = isinstance(body, (int, float)) and not isinstance(body, bool) and math.isfinite(body) and body > 0
    if clip_name is None and declared:
        return float(body), "body_height_px"
    reference = clips[clip_name or sprite.default_clip]
    heights = [box[3] - box[1] for index in dict.fromkeys(reference.frames)
               if (box := forge_core.subject_bbox(sprite.frames[index].pixels, forge_core.BODY_ALPHA_THRESHOLD))]
    if not heights:
        raise NoVisibleSubject(f"clip {reference.name} has no visible subject to measure; pass --subject-height-px")
    return float(np.median(heights)), f"median visible height of clip {reference.name}"


def engine_mapping(sprite: Sprite, sampling: str, pixel_size: float, ppu: float, pitch_deg: float | None) -> dict:
    """Where the shared anchor_px goes in each engine (the mapping table of engine-export.md)."""
    (width, height), (ax, ay) = sprite.frame_size, sprite.anchor
    compensation = {"applies_to": "yaw-only (fixed-y) billboards seen by a pitched camera",
                    "formula": "scale_y = 1 / cos(camera_pitch), applied about the anchor"}
    if pitch_deg is not None:
        compensation["camera_pitch_deg"] = pitch_deg
        compensation["scale_y"] = 1 / math.cos(math.radians(pitch_deg))
    return {
        "anchor_px": [ax, ay],
        "frame_size": [width, height],
        "anchor_normalized_top_left": [ax / width, ay / height],
        "sampling": sampling,
        "godot": {
            "animated_sprite_2d": {"centered": False, "offset": [-ax, -ay],
                                   "texture_filter": GODOT_FILTER_2D[sampling]},
            "animated_sprite_3d": {"centered": True, "offset": [width / 2 - ax, ay - height / 2],
                                   "pixel_size": pixel_size, "texture_filter": GODOT_FILTER_3D[sampling]},
        },
        "unity": {"pivot": [ax / width, 1 - ay / height], "pixels_per_unit": ppu,
                  "filter_mode": "Point" if sampling == "nearest" else "Bilinear"},
        "phaser": {"origin": [ax / width, ay / height], "pixel_art": sampling == "nearest"},
        "pixi": {"anchor": [ax / width, ay / height], "scale_mode": sampling},
        "pitch_compensation": compensation,
    }


# --------------------------------------------------------------------------- Godot text resources

def _gd_number(value: float) -> str:
    value = float(value)
    require(math.isfinite(value), "Godot resources cannot store NaN or infinity")
    if value.is_integer() and abs(value) < 1e15:
        return f"{value:.1f}"
    text = repr(value)
    return text if "e" not in text else f"{value:.12f}".rstrip("0")


def _gd_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _gd_node_name(text: str) -> str:
    return re.sub(r'[.:@/"%\s]+', "_", text).strip("_") or "Sprite"


def spriteframes_text(textures: list[tuple[str, str]], regions: list[tuple[str, str, tuple[int, int, int, int]]],
                      animations: list[dict]) -> str:
    """A Godot 4 SpriteFrames .tres.

    textures: (id, path relative to the .tres); regions: AtlasTexture sub-resources
    (id, texture id, (x, y, w, h)); animations: {name, loop, speed, frames:
    [(texture reference, relative duration)]}.
    """
    lines = [f'[gd_resource type="SpriteFrames" load_steps={len(textures) + len(regions) + 1} format=3]', ""]
    lines += [f'[ext_resource type="Texture2D" path={_gd_string(path)} id={_gd_string(ident)}]'
              for ident, path in textures]
    lines.append("")
    for ident, texture, (x, y, w, h) in regions:
        lines += [f"[sub_resource type=\"AtlasTexture\" id={_gd_string(ident)}]", f'atlas = ExtResource("{texture}")',
                  f"region = Rect2({x}, {y}, {w}, {h})", "filter_clip = true", ""]
    blocks = []
    for animation in animations:
        frames = ", ".join(f'{{\n"duration": {_gd_number(duration)},\n"texture": {reference}\n}}'
                           for reference, duration in animation["frames"])
        blocks.append(f'{{\n"frames": [{frames}],\n"loop": {"true" if animation["loop"] else "false"},\n'
                      f'"name": &{_gd_string(animation["name"])},\n"speed": {_gd_number(animation["speed"])}\n}}')
    lines += ["[resource]", "animations = [" + ", ".join(blocks) + "]"]
    return "\n".join(lines) + "\n"


def scene_text(node_type: str, node_name: str, frames_path: str, properties: dict[str, str]) -> str:
    """A one-node Godot 4 scene whose node uses the SpriteFrames at frames_path (relative to the scene)."""
    lines = ["[gd_scene load_steps=2 format=3]", "",
             f'[ext_resource type="SpriteFrames" path={_gd_string(frames_path)} id="1_frames"]', "",
             f'[node name={_gd_string(node_name)} type="{node_type}"]']
    lines += [f"{key} = {value}" for key, value in properties.items()]
    return "\n".join(lines) + "\n"


_GD_TOKEN = re.compile(r'\s*(?:(?P<string>&?"(?:[^"\\]|\\.)*")|(?P<number>-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)'
                       r'|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<punct>[\[\]{}(),:=]))', re.S)
_GD_TRAILING_SPACE = re.compile(r"\s*\Z")
_GD_HEADER = re.compile(r"^\[(?P<tag>\w+)(?P<attributes>[^\]\n]*)\]\s*$", re.M)
_GD_ATTRIBUTE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|-?\d+)')


class _GodotParser:
    """Recursive-descent reader for the Godot 4 text-resource syntax this tool writes (the round trip)."""

    def __init__(self, text: str):
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while (match := _GD_TOKEN.match(text, position)) is not None:
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            position = match.end()
        require(_GD_TRAILING_SPACE.match(text, position) is not None,
                f"unexpected text in a Godot resource: {text[position:position + 20].strip()!r}")
        self.index = 0

    def peek(self) -> str | None:
        return self.tokens[self.index][1] if self.index < len(self.tokens) else None

    def take(self, expected: str | None = None) -> tuple[str, str]:
        require(self.index < len(self.tokens) and expected in (None, self.tokens[self.index][1]),
                f"malformed Godot resource: expected {expected!r} at token {self.index}")
        self.index += 1
        return self.tokens[self.index - 1]

    def items(self, closer: str, read) -> None:
        while self.peek() not in (closer, None):
            read()
            if self.peek() == ",":
                self.take(",")
        self.take(closer)

    def value(self) -> Any:
        kind, text = self.take()
        if kind == "string":
            body = json.loads(text.lstrip("&"))
            return ("StringName", body) if text.startswith("&") else body
        if kind == "number":
            return float(text) if any(c in text for c in ".eE") else int(text)
        if kind == "name":
            if text in ("true", "false", "null"):
                return {"true": True, "false": False, "null": None}[text]
            arguments: list[Any] = []
            self.take("(")
            self.items(")", lambda: arguments.append(self.value()))
            return (text, arguments)
        if text == "[":
            array: list[Any] = []
            self.items("]", lambda: array.append(self.value()))
            return array
        require(text == "{", f"malformed Godot resource: unexpected {text!r}")
        mapping: dict[Any, Any] = {}

        def entry() -> None:
            key = self.value()
            self.take(":")
            mapping[key] = self.value()
        self.items("}", entry)
        return mapping


def parse_godot_resource(text: str) -> list[dict]:
    """Sections of a .tres/.tscn as [{tag, attributes, properties}], with values parsed."""
    matches = list(_GD_HEADER.finditer(text))
    require(len(matches) > 0 and text[:matches[0].start()].strip() == "", "a Godot resource must start with a [header]")
    sections = []
    for number, match in enumerate(matches):
        attributes = {key: json.loads(value) if value.startswith('"') else int(value)
                      for key, value in _GD_ATTRIBUTE.findall(match.group("attributes"))}
        end = matches[number + 1].start() if number + 1 < len(matches) else len(text)
        parser = _GodotParser(text[match.end():end])
        properties = {}
        while parser.peek() is not None:
            key = parser.take()[1]
            parser.take("=")
            properties[key] = parser.value()
        sections.append({"tag": match.group("tag"), "attributes": attributes, "properties": properties})
    return sections


# --------------------------------------------------------------------------- the export job

@dataclass
class Job:
    sprite: Sprite
    stage: Path
    name: str
    targets: list[str]
    pages: list[Page]
    sampling: str
    fixed_fps: float | None
    billboard: str
    scale: dict | None            # pixel_size, subject_height_px, world_height, source, subject_height_source
    files: list[Path] = field(default_factory=list)
    clip_info: dict[str, dict] = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)

    def check(self, check_id: str, status: str, value: Any = None, threshold: Any = None) -> None:
        self.checks.append({"id": check_id, "status": status, "value": value, "threshold": threshold})

    def info(self, clip: Clip) -> dict:
        return self.clip_info.setdefault(clip.name, {})

    def write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as stream:
            stream.write(text.encode("utf-8"))
        self.files.append(path)

    def write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        forge_core.write_json(path, data)
        self.files.append(path)

    def write_png(self, path: Path, pixels: np.ndarray) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        require(not path.exists(), f"refusing to overwrite {path.name}")
        forge_core.save_png(pixels, path)
        self.files.append(path)

    def page_file(self, page: Page, suffix: str) -> str:
        return f"{self.name}{suffix}" if len(self.pages) == 1 else f"{self.name}-{page.index:02d}{suffix}"


def action_names(clips: list[Clip]) -> dict[str, str]:
    """Sprite3D action names match [a-z0-9][a-z0-9_-]* (generate2dsprite build-godot-bundle) and stay unique."""
    names, taken = {}, set()
    for clip in clips:
        base = ACTION_INVALID.sub("-", clip.name.lower()).strip("-_") or "clip"
        candidate, suffix = base, 2
        while candidate in taken:
            candidate, suffix = f"{base}-{suffix}", suffix + 1
        taken.add(candidate)
        names[clip.name] = candidate
    return names


def write_aseprite(job: Job) -> list[Path]:
    """One JSON + PNG per page. Frames are the clips' timelines in clip order, named "0", "1", ... ({frame})."""
    sprite, folder = job.sprite, job.stage / TARGET_DIRS["aseprite-json"]
    width, height = sprite.frame_size
    pivot = {"x": forge_core.round_half_up(sprite.anchor[0]), "y": forge_core.round_half_up(sprite.anchor[1])}
    written = []
    for page in job.pages:
        image_name, json_name = job.page_file(page, ".png"), job.page_file(page, ".json")
        job.write_png(folder / image_name, page.pixels)
        frames, tags, animations = [], [], {}
        for clip in page.clips:
            start = len(frames)
            for index, duration in zip(clip.frames, clip.durations):
                x, y = page.cells[sprite.frames[index].key]
                frames.append({"filename": str(len(frames)), "frame": {"x": x, "y": y, "w": width, "h": height},
                               "rotated": False, "trimmed": False,
                               "spriteSourceSize": {"x": 0, "y": 0, "w": width, "h": height},
                               "sourceSize": {"w": width, "h": height}, "duration": duration})
            tag = {"name": clip.name, "from": start, "to": len(frames) - 1, "direction": "forward",
                   "color": "#000000ff"}
            if not clip.loop:
                tag["repeat"] = "1"
            data = {}
            if clip.events:  # an Aseprite frame index is a played position (D12)
                data["events"] = [{"name": e["name"], "frame": start + e["position"], "at_ms": e["at_ms"]}
                                  for e in clip.events]
            if clip.loop_policy != ("cycle" if clip.loop else "oneshot"):
                data["loop_policy"] = clip.loop_policy
            if data:
                tag["data"] = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
            tags.append(tag)
            animations[clip.name] = [str(i) for i in range(start, len(frames))]
            job.info(clip)["aseprite"] = {"file": json_name, "from": start, "to": len(frames) - 1}
        job.write_json(folder / json_name, {
            "frames": frames,
            "meta": {
                "app": f"agent-sprite-forge {TOOL_NAME}", "version": TOOL_VERSION, "image": image_name,
                "format": "RGBA8888", "size": {"w": page.size[0], "h": page.size[1]}, "scale": "1",
                "frameTags": tags,
                "layers": [{"name": "sprite", "opacity": 255, "blendMode": "normal"}],
                "slices": [{"name": "anchor", "color": "#0000ffff",
                            "data": json.dumps({"anchor_px": list(sprite.anchor)}, separators=(",", ":")),
                            "keys": [{"frame": 0, "bounds": {"x": 0, "y": 0, "w": width, "h": height},
                                      "pivot": pivot}]}],
            },
            "animations": animations,
        })
        written.append(folder / json_name)
    return written


def write_godot_spriteframes(job: Job) -> Path:
    """SpriteFrames over the atlas pages (AtlasTexture regions) plus an AnimatedSprite2D scene."""
    sprite, folder = job.sprite, job.stage / TARGET_DIRS["godot-spriteframes"]
    width, height = sprite.frame_size
    textures, regions, region_ids = [], [], {}
    for page in job.pages:
        texture_id, file_name = f"{page.index + 1}_atlas{page.index:02d}", f"{job.name}-atlas-{page.index:02d}.png"
        job.write_png(folder / file_name, page.pixels)
        textures.append((texture_id, file_name))
        for slot, (key, (x, y)) in enumerate(page.cells.items()):
            region_ids[(page.index, key)] = f"AtlasTexture_p{page.index}c{slot}"
            regions.append((region_ids[(page.index, key)], texture_id, (x, y, width, height)))
    animations = []
    for page in job.pages:
        for clip in page.clips:
            timing = godot_timing(clip, job.fixed_fps)
            animations.append({"name": clip.name, "loop": clip.loop, "speed": timing["speed"], "frames": [
                (f'SubResource("{region_ids[(page.index, sprite.frames[i].key)]}")', duration)
                for i, duration in zip(clip.frames, timing["relative_durations"])]})
            job.info(clip)["godot"] = {"speed": timing["speed"], "basis": timing["basis"],
                                       "relative_durations": timing["relative_durations"]}
    frames_path = folder / f"{job.name}.tres"
    job.write_text(frames_path, spriteframes_text(textures, regions, animations))
    ax, ay = sprite.anchor
    node = _gd_node_name(job.name)
    job.write_text(folder / f"{job.name}.tscn", scene_text("AnimatedSprite2D", node, frames_path.name, {
        "texture_filter": str(GODOT_FILTER_2D[job.sampling]),
        "sprite_frames": 'ExtResource("1_frames")',
        "animation": "&" + _gd_string(sprite.default_clip),
        "autoplay": _gd_string(sprite.default_clip),
        "centered": "false",
        "offset": f"Vector2({_gd_number(-ax)}, {_gd_number(-ay)})",
    }))
    return frames_path


def write_godot_sprite3d(job: Job) -> Path:
    """AnimatedSprite3D scene over per-frame textures (no atlas mip bleeding) and v1 Sprite3D contracts.

    Contract frame paths are relative to the contract file (S19); generate2dsprite
    build-godot-bundle reads the per-action contracts and bundle.json lists them.
    """
    sprite, folder, scale = job.sprite, job.stage / TARGET_DIRS["godot-sprite3d"], job.scale
    (width, height), (ax, ay) = sprite.frame_size, sprite.anchor
    digits = max(3, len(str(max(sprite.frames))))
    files, digests, textures, texture_ids = {}, {}, [], {}
    for index in sorted(sprite.frames):
        files[index] = f"frames/frame-{index:0{digits}d}.png"
        job.write_png(folder / files[index], sprite.frames[index].pixels)
        digests[index] = forge_core.sha256_file(folder / files[index])
        texture_ids[index] = f"{len(textures) + 1}_frame{index}"
        textures.append((texture_ids[index], files[index]))
    offset = [width / 2 - ax, ay - height / 2]
    actions, bundle_actions, animations = action_names(sprite.clips), {}, []
    for clip in sprite.clips:
        timing = godot_timing(clip, job.fixed_fps)
        animations.append({"name": clip.name, "loop": clip.loop, "speed": timing["speed"], "frames": [
            (f'ExtResource("{texture_ids[i]}")', duration)
            for i, duration in zip(clip.frames, timing["relative_durations"])]})
        uniform = len(set(clip.durations)) == 1
        contract_name = f"action-{actions[clip.name]}.json"
        job.write_json(folder / contract_name, {
            "schema": SPRITE3D_SCHEMA,
            "clip": clip.name,
            "frame_size": [width, height],
            "output_origin": [ax, ay],
            "sprite3d_offset": offset,
            "reference_subject_height_px": scale["subject_height_px"],
            "world_height": scale["world_height"],
            "recommended_pixel_size": scale["pixel_size"],
            "rendered_subject_height_world": scale["subject_height_px"] * scale["pixel_size"],
            "scale_source": scale["source"],
            "billboard": job.billboard,
            "texture_filter": job.sampling,
            # v1 readers know one duration; durations_ms is exact per frame.
            "duration_ms": (clip.durations[0] if uniform
                            else forge_core.round_half_up(clip.total_ms / len(clip.durations))),
            "fps": len(clip.durations) * 1000 / clip.total_ms,
            "frames": [files[i] for i in clip.frames],
            "frame_sha256": [digests[i] for i in clip.frames],
            "durations_ms": clip.durations,
            "loop": clip.loop,
            "loop_policy": clip.loop_policy,
            "events_ms": clip.events,
        })
        bundle_actions[actions[clip.name]] = {"contract": contract_name, "loop": clip.loop, "clip": clip.name}
        job.info(clip)["sprite3d"] = {"action": actions[clip.name], "contract": contract_name,
                                      "speed": timing["speed"], "basis": timing["basis"]}
    frames_path = folder / f"{job.name}.tres"
    job.write_text(frames_path, spriteframes_text(textures, [], animations))
    node = _gd_node_name(job.name)
    job.write_text(folder / f"{job.name}.tscn", scene_text("AnimatedSprite3D", node, frames_path.name, {
        "offset": f"Vector2({_gd_number(offset[0])}, {_gd_number(offset[1])})",
        "pixel_size": _gd_number(scale["pixel_size"]),
        "billboard": str(GODOT_BILLBOARD[job.billboard]),
        "texture_filter": str(GODOT_FILTER_3D[job.sampling]),
        "sprite_frames": 'ExtResource("1_frames")',
        "animation": "&" + _gd_string(sprite.default_clip),
        "autoplay": _gd_string(sprite.default_clip),
    }))
    job.write_json(folder / "bundle.json", {
        "schema": SPRITE3D_BUNDLE_SCHEMA,
        "default_action": actions[sprite.default_clip],
        "world_height": scale["world_height"],
        "world_height_max_drift": 0.0,
        "pixel_size": scale["pixel_size"],
        "pixel_size_max_drift": 0.0,
        "actions": bundle_actions,
    })
    return frames_path


# --------------------------------------------------------------------------- round trips

def verify_aseprite(job: Job, json_paths: list[Path]) -> None:
    """Parse every Aseprite JSON and atlas back; compare frames, pixels, durations, tags, events and pivot."""
    sprite = job.sprite
    width, height = sprite.frame_size
    pivot_error, total = 0.0, 0
    for page, json_path in zip(job.pages, json_paths):
        document = json.loads(json_path.read_text(encoding="utf-8"))
        meta, frames = document["meta"], document["frames"]
        atlas = np.asarray(forge_core.load_rgba(json_path.parent / meta["image"])[0])
        require([meta["size"]["w"], meta["size"]["h"]] == [atlas.shape[1], atlas.shape[0]],
                f"{json_path.name}: meta.size differs from the atlas")
        expected = [(index, duration) for clip in page.clips for index, duration in zip(clip.frames, clip.durations)]
        require(len(frames) == len(expected), f"{json_path.name}: {len(frames)} frames, expected {len(expected)}")
        for position, (entry, (index, duration)) in enumerate(zip(frames, expected)):
            rect = entry["frame"]
            require(entry["filename"] == str(position) and entry["duration"] == duration
                    and (rect["w"], rect["h"]) == (width, height), f"{json_path.name}: frame {position} differs")
            require(np.array_equal(atlas[rect["y"]:rect["y"] + height, rect["x"]:rect["x"] + width],
                                   sprite.frames[index].pixels),
                    f"{json_path.name}: frame {position} pixels differ from source frame {index}")
        require(len(meta["frameTags"]) == len(page.clips), f"{json_path.name}: tag count differs")
        start = 0
        for tag, clip in zip(meta["frameTags"], page.clips):
            require(tag["name"] == clip.name and tag["from"] == start and tag["to"] == start + len(clip.frames) - 1
                    and (tag.get("repeat") == "1") == (not clip.loop),
                    f"{json_path.name}: tag {tag['name']} does not match clip {clip.name}")
            events = json.loads(tag["data"]).get("events", []) if "data" in tag else []
            require([(e["name"], e["frame"] - start, e["at_ms"]) for e in events]
                    == [(e["name"], e["position"], e["at_ms"]) for e in clip.events],
                    f"{json_path.name}: events of {clip.name} differ")
            # Independent of the writer: each event's frame is the one shown at its at_ms.
            edges = np.cumsum([0, *[entry["duration"] for entry in frames[start:start + len(clip.frames)]]])
            for event in events:
                shown = min(len(clip.frames) - 1, int(np.searchsorted(edges, event["at_ms"], side="right")) - 1)
                require(event["frame"] - start == shown,
                        f"{json_path.name}: event {event['name']} of {clip.name} sits on frame {event['frame']}, "
                        f"but frame {start + shown} is shown at {event['at_ms']} ms")
            require(document["animations"][clip.name] == [str(i) for i in range(start, tag["to"] + 1)],
                    f"{json_path.name}: animations.{clip.name} differs")
            start += len(clip.frames)
        key = meta["slices"][0]["keys"][0]
        require(key["bounds"] == {"x": 0, "y": 0, "w": width, "h": height}, f"{json_path.name}: slice bounds differ")
        pivot_error = max(pivot_error, abs(key["pivot"]["x"] - sprite.anchor[0]),
                          abs(key["pivot"]["y"] - sprite.anchor[1]))
        require(pivot_error <= 0.5, f"{json_path.name}: the slice pivot is more than 0.5 px from anchor_px")
        total += len(frames)
    job.check("aseprite_roundtrip", "pass", {"pages": len(json_paths), "frames": total})
    # Aseprite pivots are whole pixels; a fractional anchor_px is rounded half up (exact value in the slice data).
    job.check("aseprite_pivot_rounding_px", "pass" if pivot_error == 0 else "warn", pivot_error, 0)


def _texture_pixels(sections: list[dict], folder: Path) -> dict[str, np.ndarray]:
    """Pixels each ExtResource/SubResource id of a parsed SpriteFrames shows."""
    images = {}
    for section in sections:
        if section["tag"] == "ext_resource":
            path = folder / section["attributes"]["path"]
            require(path.is_file(), f"{section['attributes']['path']} referenced by the SpriteFrames does not exist")
            images[section["attributes"]["id"]] = np.asarray(forge_core.load_rgba(path)[0])
    pixels = {f"ExtResource:{ident}": image for ident, image in images.items()}
    for section in sections:
        if section["tag"] == "sub_resource":
            (_, (atlas_id,)), (_, rect) = section["properties"]["atlas"], section["properties"]["region"]
            x, y, w, h = (int(v) for v in rect)
            pixels[f"SubResource:{section['attributes']['id']}"] = images[atlas_id][y:y + h, x:x + w]
    return pixels


def verify_spriteframes(job: Job, tres: Path, label: str) -> None:
    """Parse a SpriteFrames .tres back: names, loops, the pixels of every frame, and ms = duration / speed.

    Durations derived from ms must give back the source ms (to 1e-6 ms); clips
    timed by exact ticks may differ by the builder's tick rounding (at most 1 ms).
    """
    sprite = job.sprite
    sections = parse_godot_resource(tres.read_text(encoding="utf-8"))
    header = sections[0]
    require(header["tag"] == "gd_resource" and header["attributes"].get("type") == "SpriteFrames"
            and header["attributes"].get("load_steps") == len(sections) - 1,
            f"{tres.name}: header or load_steps is wrong")
    pixels = _texture_pixels(sections, tres.parent)
    animations = next(s for s in sections if s["tag"] == "resource")["properties"]["animations"]
    require([a["name"] for a in animations] == [("StringName", c.name) for c in sprite.clips],
            f"{tres.name}: animation names differ from the clips")
    worst, threshold = 0.0, ROUND_TRIP_MS_TOLERANCE
    for animation, clip in zip(animations, sprite.clips):
        require(animation["loop"] is clip.loop and len(animation["frames"]) == len(clip.frames),
                f"{tres.name}: animation {clip.name} loop or frame count differs")
        ticks = godot_timing(clip, job.fixed_fps)["basis"].startswith("ticks")
        limit = TICK_MS_TOLERANCE if ticks else ROUND_TRIP_MS_TOLERANCE
        threshold = max(threshold, limit)
        for frame, index, duration in zip(animation["frames"], clip.frames, clip.durations):
            kind, (ident,) = frame["texture"]
            require(np.array_equal(pixels[f"{kind}:{ident}"], sprite.frames[index].pixels),
                    f"{tres.name}: animation {clip.name} shows the wrong pixels for frame {index}")
            error = abs(frame["duration"] / animation["speed"] * 1000 - duration)
            require(error <= limit, f"{tres.name}: animation {clip.name} drifts {error:.6f} ms from the source")
            worst = max(worst, error)
    job.check(f"{label}_roundtrip", "pass", {"animations": len(animations)})
    job.check(f"{label}_duration_error_ms", "pass", worst, threshold)


def verify_scene(scene: Path, node_type: str, expected: dict[str, Any]) -> None:
    """Parse a .tscn back and compare its node's properties."""
    sections = parse_godot_resource(scene.read_text(encoding="utf-8"))
    node = next((s for s in sections if s["tag"] == "node"), None)
    require(node is not None and node["attributes"].get("type") == node_type, f"{scene.name}: no {node_type} node")
    resource = next(s for s in sections if s["tag"] == "ext_resource")
    require((scene.parent / resource["attributes"]["path"]).is_file(), f"{scene.name}: the SpriteFrames path is broken")
    for key, value in expected.items():
        actual = node["properties"].get(key)
        if isinstance(value, tuple) and value[0] == "Vector2":
            require(isinstance(actual, tuple) and actual[0] == "Vector2"
                    and all(abs(a - b) < 1e-9 for a, b in zip(actual[1], value[1])), f"{scene.name}: {key} is {actual}")
        elif isinstance(value, float):
            require(isinstance(actual, float) and abs(actual - value) <= 1e-12 * max(1.0, abs(value)),
                    f"{scene.name}: {key} is {actual}, expected {value}")
        else:
            require(actual == value, f"{scene.name}: {key} is {actual!r}, expected {value!r}")


def verify_sprite3d(job: Job, folder: Path) -> None:
    """Every contract frame path resolves relative to its contract (S19) with matching sha256 and pixels."""
    sprite, scale = job.sprite, job.scale
    bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
    actions = action_names(sprite.clips)
    require(bundle["schema"] == SPRITE3D_BUNDLE_SCHEMA and set(bundle["actions"]) == set(actions.values())
            and bundle["default_action"] == actions[sprite.default_clip], "bundle.json does not match the clips")
    width, height = sprite.frame_size
    offset = [width / 2 - sprite.anchor[0], sprite.anchor[1] - height / 2]
    for clip in sprite.clips:
        contract_path = folder / bundle["actions"][actions[clip.name]]["contract"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        require(contract["schema"] == SPRITE3D_SCHEMA and contract["sprite3d_offset"] == offset
                and contract["recommended_pixel_size"] == scale["pixel_size"]
                and contract["durations_ms"] == clip.durations and len(contract["frames"]) == len(clip.frames),
                f"{contract_path.name}: geometry, scale or timing differs")
        for relative, digest, index in zip(contract["frames"], contract["frame_sha256"], clip.frames):
            require(not Path(relative).is_absolute() and "\\" not in relative and ":" not in relative,
                    f"{contract_path.name}: frame path {relative!r} is not relative")
            frame_path = contract_path.parent / relative
            require(frame_path.is_file() and forge_core.sha256_file(frame_path) == digest,
                    f"{contract_path.name}: {relative} is missing or changed")
            require(np.array_equal(np.asarray(forge_core.load_rgba(frame_path)[0]), sprite.frames[index].pixels),
                    f"{contract_path.name}: {relative} pixels differ from source frame {index}")
    job.check("sprite3d_paths_relative", "pass", {"contracts": len(sprite.clips)})


# --------------------------------------------------------------------------- export

def _default_name(output_dir: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", Path(output_dir).name).strip("-_") or "sprite"


def _scale(args: argparse.Namespace, sprite: Sprite, sprite3d: bool) -> dict | None:
    """The Sprite3D pixel size and the subject height it comes from.

    Only the godot-sprite3d target and --world-height need the subject height. Without them, art with no
    pixel above BODY_ALPHA_THRESHOLD (faint FX) gets no scale block, and the mapping table uses
    --pixel-size or Godot's default pixel size."""
    try:
        subject, subject_source = reference_subject_height(sprite, args.reference_clip, args.subject_height_px)
    except NoVisibleSubject:
        if sprite3d or args.world_height is not None:
            raise
        return None
    if args.pixel_size is not None:
        pixel_size, source = args.pixel_size, "--pixel-size"
    elif args.world_height is not None:
        pixel_size, source = args.world_height / subject, "--world-height"
    else:
        pixel_size, source = GODOT_DEFAULT_PIXEL_SIZE, "Godot default pixel size"
    return {"pixel_size": pixel_size, "subject_height_px": subject, "world_height": subject * pixel_size,
            "source": source, "subject_height_source": subject_source}


def export(args: argparse.Namespace) -> dict:
    """Load, write every requested target into a stage, round-trip it, then publish the directory."""
    sprite = load_clips(args.clips.resolve())
    name = args.name or _default_name(args.output_dir)
    require(NAME_PATTERN.match(name) is not None, "--name may use only letters, digits, hyphens and underscores")
    targets = list(TARGETS) if args.target == "all" else [args.target]
    sampling = resolve_sampling(args.sampling, sprite.top)
    scale = _scale(args, sprite, "godot-sprite3d" in targets)
    # Atlas pages exist only for the atlas targets; godot-sprite3d draws per-frame textures.
    atlas = any(target in ATLAS_TARGETS for target in targets)
    pages = plan_pages(sprite, args.max_atlas_size, args.padding, args.extrude) if atlas else []
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        job = Job(sprite, stage, name, targets, pages, sampling, args.godot_fps, args.billboard, scale)
        width, height = sprite.frame_size
        (ax, ay), default = sprite.anchor, sprite.default_clip
        if "aseprite-json" in targets:
            verify_aseprite(job, write_aseprite(job))
        if "godot-spriteframes" in targets:
            tres = write_godot_spriteframes(job)
            verify_spriteframes(job, tres, "godot_spriteframes")
            verify_scene(tres.with_suffix(".tscn"), "AnimatedSprite2D", {
                "texture_filter": GODOT_FILTER_2D[sampling], "centered": False, "offset": ("Vector2", [-ax, -ay]),
                "animation": ("StringName", default), "autoplay": default})
        if "godot-sprite3d" in targets:
            tres = write_godot_sprite3d(job)
            verify_spriteframes(job, tres, "godot_sprite3d_frames")
            verify_scene(tres.with_suffix(".tscn"), "AnimatedSprite3D", {
                "texture_filter": GODOT_FILTER_3D[sampling], "billboard": GODOT_BILLBOARD[args.billboard],
                "pixel_size": scale["pixel_size"], "offset": ("Vector2", [width / 2 - ax, ay - height / 2]),
                "animation": ("StringName", default)})
            verify_sprite3d(job, tres.parent)
        if job.pages:
            max_side = max(max(page.size) for page in job.pages)
            require(max_side <= args.max_atlas_size, f"atlas side {max_side} exceeds {args.max_atlas_size}")
            job.check("atlas_max_side_px", "pass", max_side, args.max_atlas_size)
        else:
            job.check("atlas_max_side_px", "skipped", None, args.max_atlas_size)
        verified = sum(frame.hash_verified for frame in sprite.frames.values())
        job.check("source_frame_hashes", "pass" if verified == len(sprite.frames) else "skipped",
                  {"verified": verified, "frames": len(sprite.frames)})
        document = export_document(job, final, args)
        forge_core.write_json(stage / "engine-export.json", document)
    return {"status": document["qa"]["status"], "output": str(final.resolve()),
            "metadata": str(final.resolve() / "engine-export.json"), "targets": targets,
            "clips": len(sprite.clips), "frames": len(sprite.frames), "pages": len(job.pages)}


def export_document(job: Job, final: Path, args: argparse.Namespace) -> dict:
    """engine-export.json: what was written, per-clip timing and events, the mapping table and the QA envelope."""
    sprite = job.sprite
    clips = {}
    for clip in sprite.clips:
        # page: the clip's atlas page; 0 when no atlas target was exported (atlas.pages is then empty).
        entry = {"frames": clip.frames, "duration_ms": clip.durations, "total_duration_ms": clip.total_ms,
                 "loop": clip.loop, "loop_policy": clip.loop_policy, "events_ms": clip.events,
                 "page": next((page.index for page in job.pages if clip in page.clips), 0)}
        if clip.hints:
            entry["transition_hints"] = clip.hints
        entry.update(clip.extras)
        entry.update(job.clip_info.get(clip.name, {}))
        clips[clip.name] = entry
    outputs = [{"path": path.relative_to(job.stage).as_posix(), "sha256": forge_core.sha256_file(path),
                "bytes": path.stat().st_size} for path in job.files]
    # A file on another drive records only its name (forge_core.file_ref): never an absolute path.
    inputs = [forge_core.file_ref(sprite.manifest, final)]
    inputs += [forge_core.file_ref(sprite.frames[i].path, final) for i in sorted(sprite.frames)]
    warned = any(check["status"] == "warn" for check in job.checks)
    pixel_size = job.scale["pixel_size"] if job.scale else (args.pixel_size or GODOT_DEFAULT_PIXEL_SIZE)
    ppu = args.ppu if args.ppu is not None else 1 / pixel_size
    return {
        "schema": EXPORT_SCHEMA,
        "name": job.name,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "source": {**inputs[0], "schema": sprite.schema},
        "targets": job.targets,
        "frame_size": list(sprite.frame_size),
        "anchor_px": list(sprite.anchor),
        "sampling": job.sampling,
        **{key: sprite.top[key] for key in ("pixel_art", "art_source", "placeholder", "shadow") if key in sprite.top},
        "default_clip": sprite.default_clip,
        "atlas": {"max_size": args.max_atlas_size, "padding": args.padding, "extrude": args.extrude,
                  "pages": [{"index": page.index, "size": list(page.size), "cells": len(page.cells),
                             "clips": [clip.name for clip in page.clips]} for page in job.pages]},
        **({"scale": job.scale} if job.scale else {}),
        "clips": clips,
        "states": sprite.states,
        "unused_frames": sorted(set(range(sprite.frame_count)) - set(sprite.frames)),
        "mapping": engine_mapping(sprite, job.sampling, pixel_size, ppu, args.camera_pitch_deg),
        "files": outputs,
        "qa": {
            "status": "warn" if warned else "pass",
            "method": ("round trip: every written atlas, Aseprite JSON, SpriteFrames .tres, scene .tscn and Sprite3D "
                       "contract was parsed back and compared with the source frames (exact RGBA), integer durations, "
                       "loop flags, events and the shared anchor"),
            "notProven": [
                "Imports into the Aseprite, Godot 4, Phaser, PixiJS and Unity editors or runtimes were not run; "
                "only parse-level round trips were checked.",
                "Engine import settings (filter, compression, mipmaps) are not written; set them as "
                "engine-export.md describes.",
                "Animation quality, timing feel and silhouette consistency are not judged.",
            ],
            "checks": job.checks,
            "inputs": inputs,
            "outputs": [{"path": item["path"], "sha256": item["sha256"]} for item in outputs],
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        },
    }


# --------------------------------------------------------------------------- CLI

def _positive_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def _atlas_size(text: str) -> int:
    value = int(text)
    if not 16 <= value <= MAX_ATLAS_SIZE:
        raise argparse.ArgumentTypeError(f"must be between 16 and {MAX_ATLAS_SIZE}")
    return value


def _small_int(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 16:
        raise argparse.ArgumentTypeError("must be between 0 and 16")
    return value


def _godot_fps(text: str) -> float | None:
    return None if text == "auto" else _positive_float(text)


def _pitch(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or not 0 <= value < 90:
        raise argparse.ArgumentTypeError("must be in [0, 90) degrees")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export built sprite clips (animation-clips.json v1/v2) to an Aseprite JSON atlas and Godot 4 "
                    "SpriteFrames/Sprite3D. Every output is parsed back and checked before the directory is "
                    "published; engine imports are not verified by this tool.",
        epilog="Example: python export_engine.py --clips out/hero/animation-clips.json --target all "
               "--output-dir out/hero-engine")
    parser.add_argument("--clips", type=Path, required=True,
                        help="animation-clips.json written by build_animation_clips.py (v1 or v2)")
    parser.add_argument("--target", choices=(*TARGETS, "all"), default="all", help="what to export (default: all)")
    parser.add_argument("--output-dir", type=Path, required=True, help="new output directory; never overwritten")
    parser.add_argument("--name", help="base file name (default: the output directory name)")
    parser.add_argument("--max-atlas-size", type=_atlas_size, default=MAX_ATLAS_SIZE,
                        help="largest atlas side in px, at most 4096 (default 4096); whole clips move to extra pages")
    parser.add_argument("--padding", type=_small_int, default=2, help="transparent px between atlas cells (default 2)")
    parser.add_argument("--extrude", type=_small_int, default=1,
                        help="px of replicated edge around each frame against filtering bleed (default 1)")
    parser.add_argument("--sampling", choices=("auto", "nearest", "linear"), default="auto",
                        help="texture filter; auto reads the manifest sampling or pixel_art, else linear")
    parser.add_argument("--godot-fps", type=_godot_fps, default=None, metavar="auto|FPS",
                        help="Godot animation speed; auto uses a clip's ticks when present, else its shortest frame")
    scale = parser.add_mutually_exclusive_group()
    scale.add_argument("--world-height", type=_positive_float,
                       help="Godot world units for the reference subject height (sets the Sprite3D pixel size)")
    scale.add_argument("--pixel-size", type=_positive_float, help="explicit Godot Sprite3D pixel size (default 0.01)")
    parser.add_argument("--subject-height-px", type=_positive_float,
                        help="reference subject height in source px (default: body_height_px, else measured)")
    parser.add_argument("--reference-clip",
                        help="clip measured for the subject height (default: the idle state's clip)")
    parser.add_argument("--billboard", choices=tuple(GODOT_BILLBOARD), default="enabled",
                        help="Sprite3D billboard mode (default: enabled)")
    parser.add_argument("--ppu", type=_positive_float,
                        help="Unity pixels per unit for the mapping table (default: 1 / pixel size)")
    parser.add_argument("--camera-pitch-deg", type=_pitch,
                        help="camera pitch for the fixed-y billboard compensation example in the mapping table")
    return parser


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(export(args), ensure_ascii=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Usage errors exit 2 (argparse); bad input, an impossible layout or a failed round trip print
    ``error: <message>`` and exit 1; anything unexpected prints ``error: internal error (...)`` (D26, D27)."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
