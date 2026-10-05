"""Render a PixelSpec (codeart2d.pixelspec.v1) into checked 8-bit RGBA pixel-art frames.

Run from the project root; outputs go to a new folder inside the project:
  python "<skill-dir>/scripts/render_pixelspec.py" --spec slime.pixelspec.json --output-dir out/slime-v1
  python "<skill-dir>/scripts/render_pixelspec.py" --spec slime.pixelspec.json --output-dir out/slime-v1 --variants all --build-clips --preview-scale 6 --strict-qc

The spec is checked against references/schemas/codeart.schema.json#/$defs/pixelspec_v1
(with the vendored forge_schema evaluator, no jsonschema needed) and against the
cross-references a schema cannot express (pose, layer, frame and clip names, palette
characters, timing lengths) before anything is rendered.

Output folder (written to a stage beside it, checked, then published in one step):
  codeart-meta.json            art_source "code", spec sha256, palette, every output file
                               and a QA envelope covering every frame of every variant
  <variant>/frames/<name>.png   one 8-bit RGBA PNG per distinct frame; frames that render
                               the same pixels in every variant share one file
  <variant>/clips.json          --clips-manifest or --build-clips: manifest for
                               generate2dsprite's build_animation_clips.py
                               (animation_clips.v2; --clips-schema v1 for old builders)
  <variant>/bundle/             --build-clips: that builder's output for the variant
  preview-x<N>.png              --preview-scale N: variants x frames, integer nearest

Variants: "all" renders every named variant of the spec (the base palette when it has
none); "base" is the base palette; or name variants, comma-separated.
A frame that renders no pixel is not written. In clips, empty frames at the end of a
clip are dropped and their time is added to the last visible frame (build_animation_clips
needs visible and transparent pixels in every frame); an empty frame anywhere else is
an error. Code art goes to build_animation_clips, never to generate2dsprite.py process.
With --strict-qc nothing is published unless every frame has 0 partial-alpha and
0 off-palette pixels and, with an outline, 0 outline gaps and at most 10 L-corners.
Without it a failing result is still published for inspection, and the tool exits 1.
Walk and run clips (by clip or state name) also get half_cycle_duplicates: frame i vs
i+n/2 with silhouette IoU >= 0.95 warns (QA status warn, still published, exit 0)
unless --allow-duplicate-half-cycle records that the leg colours carry the stride.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import forge_schema  # noqa: E402  (standard library only: the vendored contract evaluator, D31)
try:
    import numpy as np
    import codeart_core
    import forge_core
except ImportError as _import_error:  # a clean machine: main() prints the pip command instead of a traceback
    _MISSING: ImportError | None = _import_error
else:
    _MISSING = None

TOOL_NAME = "render_pixelspec.py"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION if _MISSING is None else "0.4.0"  # D29: the package version
SKILL_DIR = SCRIPTS_DIR.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
DEFAULT_CLIPS_BUILDER = SKILL_DIR.parent / "generate2dsprite" / "scripts" / "build_animation_clips.py"
META_NAME = "codeart-meta.json"
BASE_VARIANT = "base"
CLIPS_V1 = "generate2dsprite.animation_clips.v1"
CLIPS_V2 = "generate2dsprite.animation_clips.v2"
CLIPS_SCHEMAS = {"v1": CLIPS_V1, "v2": CLIPS_V2}
# Clip fields that only the v2 clips manifest knows (plan Appendix B). Manifests are v2 by default (D11);
# --clips-schema v1 (for builders that predate the v2 reader) refuses clips that use any of them.
CLIP_V2_FIELDS = frozenset({"ticks", "tick_hz", "loop_policy", "events", "keys", "entry_frame", "stride_px_per_frame",
                            "cadence_ms", "speed_ref", "transitions", "hitstop_ticks", "role"})
BUILDER_TIMEOUT_S = 600
MAX_PREVIEW_SCALE = 64
MAX_IMAGE_PIXELS = 1 << 26  # 64 Mpx (256 MB as RGBA): the largest preview written
QA_METHOD = ("render_pixelspec.py: every published frame PNG is read back and measured with codeart_core.qa_pixels "
             "against the resolved palette of its variant (alpha census, exact palette lookup and, with an "
             "outline, 4-neighbour outline gaps and L-corners); each check is the worst frame against its gate "
             "in codeart_core.QA_PIXEL_GATES")
QA_NOT_PROVEN = (
    "Readability, silhouette and appeal at game scale; check them on a review sheet (pixel_qa.py --review).",
    "Motion, timing and spacing between frames; numbers do not judge animation quality.",
    "That the art matches the brief and the intent of the spec.",
)
GAIT_NAME = re.compile(r"(?<![a-z])(?:walk|run|jog|sprint|trot|gallop)(?:s|ing|ning)?(?![a-z])", re.I)
HALF_CYCLE_IOU_WARN = 0.95  # silhouette IoU of frame i vs i + n/2 in a walk or run clip (live validation 2026-10-06)
_WINDOWS_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])")


class QAFailure(Exception):
    """Strict QA failed; nothing is published."""


# ----------------------------------------------------------------------------- contract validation

def contract_errors(instance: Any, domain: str, definition: str) -> list[str]:
    """Violations of <domain>.schema.json#/$defs/<definition> among the skill's vendored contracts, as
    "$.json.path: message" lines (empty when valid). forge_schema (D31) evaluates exactly the Draft
    2020-12 keywords the contracts use and raises on any other, so a schema update it cannot read
    fails loudly instead of passing; it replaces this tool's former private _LocalContracts."""
    return forge_schema.schema_set(SCHEMA_DIR).contract_errors(instance, domain, definition)


# ----------------------------------------------------------------------------- spec loading and checks

def load_spec(path: Path) -> tuple[dict, bytes]:
    """Read a PixelSpec file strictly through forge_core.parse_json (D28): UTF-8 with an optional BOM,
    one JSON object, no duplicate keys, no NaN or Infinity."""
    if not path.is_file():
        raise codeart_core.CodeArtError(f"spec file not found: {path}")
    raw = path.read_bytes()
    try:
        spec = forge_core.parse_json(raw, strict=True)
    except UnicodeDecodeError:
        raise codeart_core.CodeArtError(f"{path.name} is not UTF-8 text") from None
    except json.JSONDecodeError as error:
        raise codeart_core.CodeArtError(f"{path.name} is not valid JSON: {error}") from None
    except ValueError as error:  # a duplicate key, NaN or a number that overflows to infinity
        raise codeart_core.CodeArtError(f"{path.name}: {error}; use unique keys and finite numbers") from None
    if not isinstance(spec, dict):
        raise codeart_core.CodeArtError(f"{path.name} must hold one JSON object (a PixelSpec)")
    return spec, raw


def _grid_chars(value: Any) -> set[str]:
    """Characters used by literal rows or run-length segments (a pose name uses none itself)."""
    if isinstance(value, Mapping):
        return _grid_chars(value.get("segments")) if "segments" in value else _grid_chars(value.get("rows"))
    if isinstance(value, list):
        return {char for row in value for char in row}
    return set()


def _segment_chars(rows: Any) -> set[str]:
    return {char for row in rows or () for char in re.findall(r"\d*(\D)", row)}


def validate_spec(spec: dict) -> None:
    """Refuse a spec that breaks pixelspec_v1 or one of the references a schema cannot express.

    The renderer is more lenient than the contract (integer RGB palette entries, a pose
    naming another pose, a pose with both rows and segments, truthy non-boolean flags), so
    the contract is checked first. Then: variant, pose, layer, frame and clip names exist;
    layer and frame names are unique; every character is in the palette; every rendered
    layer has pixels; clip timing lists match their frames; event and key positions fit.
    """
    errors = contract_errors(spec, "codeart", "pixelspec_v1")
    if errors:
        more = f" (and {len(errors) - 4} more)" if len(errors) > 4 else ""
        raise codeart_core.CodeArtError(
            "the spec does not follow codeart2d.pixelspec.v1 (references/schemas/codeart.schema.json): "
            + "; ".join(errors[:4]) + more)
    palette = spec["palette"]
    allowed = set(palette) | codeart_core.TRANSPARENT_CHARS
    for name, overrides in (spec.get("variants") or {}).items():
        unknown = sorted(set(overrides) - set(palette))
        if unknown:
            raise codeart_core.CodeArtError(f"variant {name!r} overrides {', '.join(map(repr, unknown))}, which the "
                                            "base palette does not define")

    def check_chars(chars: set[str], label: str) -> None:
        missing = sorted(chars - allowed)
        if missing:
            raise codeart_core.CodeArtError(f"{label} uses {', '.join(map(repr, missing))}, not in the palette "
                                            "('.' and space are transparent)")

    poses = spec.get("poses") or {}
    for name, pose in poses.items():
        segments = pose.get("segments") if isinstance(pose, Mapping) and "segments" in pose else None
        check_chars(_segment_chars(segments) if segments is not None else _grid_chars(pose), f"pose {name!r}")

    def check_source(source: Mapping, label: str) -> None:
        rows = source.get("rows")
        if isinstance(rows, str) and rows not in poses:
            raise codeart_core.CodeArtError(f"{label} rows name an unknown pose {rows!r}; poses: "
                                            f"{', '.join(map(repr, poses)) or 'none'}")
        check_chars(_grid_chars(rows if isinstance(rows, list) else None), label)
        check_chars(_segment_chars(source.get("segments")), label)

    layers = spec["layers"]
    layer_names = [layer["name"] for layer in layers]
    duplicates = sorted({name for name in layer_names if layer_names.count(name) > 1})
    if duplicates:
        raise codeart_core.CodeArtError(f"layer names must be unique; repeated: {', '.join(map(repr, duplicates))}")
    for layer in layers:
        check_source(layer, f"layer {layer['name']!r}")
    frames = spec.get("frames") or []
    names = [frame["name"] for frame in frames if "name" in frame]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise codeart_core.CodeArtError(f"frame names must be unique; repeated: {', '.join(map(repr, duplicates))}")
    for index, frame in enumerate(frames or [None]):
        frame = frame or {}
        label = f"frame {frame.get('name', index)!r}" if frames else "the base image (no frames)"
        overrides = frame.get("layers") or {}
        unknown = [name for name in overrides if name not in layer_names]
        if unknown:
            raise codeart_core.CodeArtError(f"{label} overrides unknown layer(s) {', '.join(map(repr, unknown))}")
        for layer in layers:
            override = overrides.get(layer["name"]) or {}
            check_source(override, f"{label} layer {layer['name']!r}")
            if override.get("hidden", layer.get("hidden", False)):
                continue
            if not any(key in source for source in (override, layer) for key in ("rows", "segments")):
                raise codeart_core.CodeArtError(f"{label} renders layer {layer['name']!r}, which has no rows or "
                                                "segments; give them in the layer or the frame, or set hidden")
    outline = spec.get("outline") or {}
    used = {outline.get("color")} | set((outline.get("map") or {}).keys()) | set((outline.get("map") or {}).values())
    missing = sorted(char for char in used - {None} if char not in palette)
    if missing:
        raise codeart_core.CodeArtError(f"the outline uses {', '.join(map(repr, missing))}, not in the palette")
    _check_clips(spec, frames)


def _check_clips(spec: Mapping, frames: Sequence[Mapping]) -> None:
    clips = spec.get("clips") or {}
    names = {frame["name"]: index for index, frame in enumerate(frames) if "name" in frame}
    for clip_name, clip in clips.items():
        label = f"clip {clip_name!r}"
        if not frames:
            raise codeart_core.CodeArtError(f"{label} needs frames, but the spec has none")
        count = len(clip["frames"])
        for reference in clip["frames"]:
            if isinstance(reference, str) and reference not in names:
                raise codeart_core.CodeArtError(f"{label} names an unknown frame {reference!r}")
            if isinstance(reference, int) and not 0 <= reference < len(frames):
                raise codeart_core.CodeArtError(f"{label} frame index {reference} is out of range "
                                                f"(0..{len(frames) - 1})")
        for key in ("duration_ms", "ticks"):
            if isinstance(clip.get(key), list) and len(clip[key]) != count:
                raise codeart_core.CodeArtError(f"{label} has {count} frame(s) but {len(clip[key])} {key} values")
        positions = [(f"events[{i}].at", event["at"]) for i, event in enumerate(clip.get("events") or ())]
        positions += [(f"keys.{key}", value) for key, value in (clip.get("keys") or {}).items()]
        if "entry_frame" in clip:
            positions.append(("entry_frame", clip["entry_frame"]))
        for field, position in positions:
            if position >= count:
                raise codeart_core.CodeArtError(f"{label} {field} is position {position}, past its {count} frame(s)")
        for transition in clip.get("transitions") or ():
            target = clips.get(transition["to"])
            if target is None:
                raise codeart_core.CodeArtError(f"{label} has a transition to unknown clip {transition['to']!r}")
            if transition.get("entry_frame", 0) >= len(target["frames"]):
                raise codeart_core.CodeArtError(f"{label} transition to {transition['to']!r} enters at position "
                                                f"{transition['entry_frame']}, past that clip's frames")
    states = spec.get("states")
    if states is not None:
        if not isinstance(states, Mapping) or not all(isinstance(target, str) and target in clips
                                                      for target in states.values()):
            raise codeart_core.CodeArtError("states must map state names to clip names of the spec")


# ----------------------------------------------------------------------------- rendering plan

def _local_safe_stem(name: str, used: set[str], fallback: str) -> str:
    """A file-system-safe, case-insensitively unique stem (Windows reserved names get a suffix)."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or fallback
    if _WINDOWS_RESERVED.fullmatch(stem.split(".", 1)[0]):
        stem += "_"
    candidate, counter = stem, 2
    while candidate.lower() in used:
        candidate, counter = f"{stem}-{counter}", counter + 1
    used.add(candidate.lower())
    return candidate


def select_variants(spec: Mapping, text: str) -> list[tuple[str, str | None]]:
    """(label, variant name or None for the base palette) per requested variant, in order."""
    named = list(spec.get("variants") or {})
    if text.strip() == "all":
        return [(name, name) for name in named] or [(BASE_VARIANT, None)]
    chosen: list[tuple[str, str | None]] = []
    for token in (part.strip() for part in text.split(",")):
        if not token:
            continue
        if token in named:
            item = (token, token)
        elif token == BASE_VARIANT:
            item = (BASE_VARIANT, None)
        else:
            raise codeart_core.CodeArtError(f"unknown variant {token!r}; this spec has: "
                                            f"{', '.join([BASE_VARIANT] + named)} (or use all)")
        if item not in chosen:
            chosen.append(item)
    if not chosen:
        raise codeart_core.CodeArtError("--variants needs 'all', 'base' or variant names")
    return chosen


def frame_plan(spec: Mapping, variants: Sequence[tuple[str, str | None]]) -> dict:
    """Render every frame in every variant and group frames that render identically.

    Returns {"frames": [{index, name, stem, ref}], "pixels": {(label, i): rgba}, "canonical":
    [i -> first frame with the same pixels in every variant], "empty": [bool per frame]}.
    """
    spec_frames = spec.get("frames") or []
    used: set[str] = set()
    if spec_frames:
        frames = [{"index": index, "name": frame.get("name"), "ref": frame,
                   "stem": _local_safe_stem(str(frame.get("name") or ""), used, f"frame-{index:03d}")}
                  for index, frame in enumerate(spec_frames)]
    else:
        name = str(spec.get("name") or "")
        frames = [{"index": None, "name": None, "ref": None, "stem": _local_safe_stem(name, used, "sprite")}]
    pixels: dict[tuple[str, int], np.ndarray] = {}
    keys, canonical, empty = {}, [], []
    for position, frame in enumerate(frames):
        signature = []
        visible = []
        for label, variant in variants:
            rgba = codeart_core.render_pixelspec(spec, frame["ref"], variant)
            pixels[(label, position)] = rgba
            signature.append(codeart_core.rgba_sha256(rgba))
            visible.append(bool(rgba[..., 3].any()))
        if any(visible) and not all(visible):
            shown = [label for (label, _), flag in zip(variants, visible) if flag]
            hidden = [label for (label, _), flag in zip(variants, visible) if not flag]
            raise codeart_core.CodeArtError(f"frame {frame['stem']!r} is visible in {', '.join(shown)} but empty in "
                                            f"{', '.join(hidden)}; variants should change colours, not visibility")
        empty.append(not any(visible))
        canonical.append(keys.setdefault(tuple(signature), position))
    if all(empty):
        raise codeart_core.CodeArtError("the spec renders no visible pixel in any frame")
    return {"frames": frames, "pixels": pixels, "canonical": canonical, "empty": empty}


# ----------------------------------------------------------------------------- QA

def _outline_colours(spec: Mapping, colours: Mapping[str, tuple]) -> list[tuple] | None:
    outline = spec.get("outline") or {}
    mode = outline.get("mode", "none")
    if mode == "none":
        return None
    chars = [outline["color"]] + (list((outline.get("map") or {}).values()) if mode == "selout" else [])
    return [colours[char] for char in dict.fromkeys(chars)]


def frame_qa(path: Path, colours: Mapping[str, tuple], outline: list | None) -> dict:
    """qa_pixels metrics of a published PNG, read back from disk."""
    image, _ = forge_core.load_rgba(path)
    return codeart_core.qa_pixels(np.asarray(image), list(colours.values()), outline)


def gait_clips(spec: Mapping) -> list[str]:
    """Clips whose own name, or a state that plays them, names a walk or run (walk_right, hero-run, ...)."""
    clips = spec.get("clips") or {}
    by_state = {target for state, target in (spec.get("states") or {}).items() if GAIT_NAME.search(str(state))}
    return [name for name in clips if GAIT_NAME.search(name) or name in by_state]


def half_cycle_check(spec: Mapping, plan: Mapping, allow: bool) -> dict | None:
    """Warn when a walk or run clip's frame i and frame i + n/2 have nearly the same silhouette.

    Side-view cycles swap the near and far legs every half cycle; when the two half-cycle frames
    share a silhouette (IoU >= HALF_CYCLE_IOU_WARN), the alternation rests on leg colours alone and
    often reads as a shuffle (the live Codex fox, IoU 0.97 and 0.95). Variants change colours only,
    so the first variant's alpha is measured. None when the spec has no walk or run clip."""
    names = gait_clips(spec)
    if not names:
        return None
    label = next(iter(key for key, _ in plan["pixels"]))
    lookup = {frame["name"]: position for position, frame in enumerate(plan["frames"]) if frame["name"] is not None}
    rows, flagged, worst = [], [], None
    for clip_name in names:
        positions = [ref if isinstance(ref, int) else lookup[ref] for ref in spec["clips"][clip_name]["frames"]]
        count, half = len(positions), len(positions) // 2
        row: dict[str, Any] = {"clip": clip_name, "frames": count, "pairs": []}
        if count < 4:
            row["note"] = "fewer than 4 frames: no half cycle to compare"
            rows.append(row)
            continue
        for index in range(half):
            first = plan["pixels"][(label, positions[index])][..., 3] > 0
            second = plan["pixels"][(label, positions[index + half])][..., 3] > 0
            union = int((first | second).sum())
            if not union:
                continue
            iou = round(float((first & second).sum()) / union, 4)
            row["pairs"].append([index, index + half, iou])
            worst = iou if worst is None else max(worst, iou)
            if iou >= HALF_CYCLE_IOU_WARN:
                flagged.append(f"{clip_name} {index}/{index + half} (IoU {iou:.2f})")
        rows.append(row)
    check: dict[str, Any] = {"id": "half_cycle_duplicates", "status": "pass", "value": worst,
                             "threshold": HALF_CYCLE_IOU_WARN, "detail": {"clips": rows, "variant": label}}
    if worst is None:
        check["status"] = "skipped"
    elif flagged:
        check["failing"] = flagged
        if allow:
            check["override"] = "--allow-duplicate-half-cycle"
        else:
            check["status"] = "warn"
            check["note"] = ("half-cycle frames share a silhouette, so the stride rests on leg colours alone: give the "
                             "near and far legs a clear value contrast, keep the arms or forelegs visible, look at the "
                             "review sheet, and pass --allow-duplicate-half-cycle only once it reads")
    return check


def qa_envelope(records: list[dict], inputs: list[dict], outputs: list[dict], has_outline: bool,
                extra_checks: Sequence[dict] = ()) -> dict:
    """A common qaEnvelope over every frame: one check per pixel gate, valued at the worst frame,
    plus ``extra_checks`` (the walk/run half-cycle check, which can warn but never fails)."""
    checks = []
    for name, limit in codeart_core.QA_PIXEL_GATES:
        values = [(record["metrics"][name], record["file"]) for record in records
                  if record["metrics"][name] is not None]
        if not values:
            checks.append({"id": name, "status": "skipped", "value": None, "threshold": limit})
            continue
        worst = max(value for value, _ in values)
        check = {"id": name, "status": "pass" if worst <= limit else "fail", "value": worst, "threshold": limit}
        failing = [file for value, file in values if value > limit]
        if failing:
            check["failing"] = failing
        checks.append(check)
    checks.extend(extra_checks)
    not_proven = list(QA_NOT_PROVEN)
    if not has_outline:
        not_proven.append("Outline continuity and L-corners: the spec has no outline, so they were not measured.")
    statuses = {check["status"] for check in checks}
    status = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    keep = ("visible", "partial_alpha", "off_palette", "colors", "orphans", "l_corners", "outline_gaps", "bbox")
    return {"status": status, "method": QA_METHOD, "notProven": not_proven, "checks": checks, "inputs": inputs,
            "outputs": outputs, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "frames": [{"file": record["file"], "variant": record["variant"], "frame": record["frame"],
                        **{key: record["metrics"][key] for key in keep}} for record in records]}


def _file_ref(path: Path, base: Path) -> dict:
    """fileRef relative to `base` (POSIX); a file on another drive is recorded by its name (forge_core.file_ref)."""
    return forge_core.file_ref(path, base)


# ----------------------------------------------------------------------------- clips

def clips_manifests(spec: Mapping, plan: Mapping, schema: str = "v2") -> tuple[dict, dict]:
    """The clips manifest shared by every variant (frame files are per variant) and a report.

    schema "v2" (the default, D11) writes generate2dsprite.animation_clips.v2, so events, ticks and
    the top-level sampling/pixel_art/art_source reach every builder; "v1" is for builders that
    predate the v2 reader and refuses clips that use v2-only fields.

    Clip frame references become indices into the manifest's frames, which list each
    distinct referenced frame once (repeated poses reuse one file). Empty frames at the
    end of a clip are dropped and their duration_ms / ticks are added to the last visible
    frame; event, key and entry positions past the new end move onto that frame.
    """
    frames = plan["frames"]
    canonical, empty = plan["canonical"], plan["empty"]
    names = {frame["name"]: position for position, frame in enumerate(frames) if frame["name"] is not None}
    resolved: dict[str, list[int]] = {}
    report: dict[str, dict] = {}
    for clip_name, clip in spec["clips"].items():
        positions = [reference if isinstance(reference, int) else names[reference] for reference in clip["frames"]]
        visible = [p for p, frame in enumerate(positions) if not empty[frame]]
        if not visible:
            raise codeart_core.CodeArtError(f"clip {clip_name!r}: every frame renders no visible pixel")
        last = visible[-1]
        gaps = [p for p in range(last + 1) if empty[positions[p]]]
        if gaps:
            raise codeart_core.CodeArtError(
                f"clip {clip_name!r}: frame {frames[positions[gaps[0]]]['stem']!r} at position {gaps[0]} renders no "
                "visible pixel; build_animation_clips needs visible pixels in every frame, and only empty frames at "
                "the end of a clip are merged into the frame before them")
        resolved[clip_name] = positions[:last + 1]
        report[clip_name] = {"frames": len(positions), "merged_empty_tail": len(positions) - last - 1}
    used = sorted({canonical[position] for positions in resolved.values() for position in positions})
    index_of = {position: index for index, position in enumerate(used)}
    # File stems are unique (case-insensitively) and stable, so they double as the manifest frame names.
    manifest_frames = [{"name": frames[p]["stem"], "file": f"frames/{frames[p]['stem']}.png"} for p in used]
    clips = {}
    for clip_name, clip in spec["clips"].items():
        kept = len(resolved[clip_name])
        out = {key: value for key, value in clip.items()}
        out["frames"] = [index_of[canonical[position]] for position in resolved[clip_name]]
        if report[clip_name]["merged_empty_tail"]:
            total = report[clip_name]["frames"]
            for key in ("duration_ms", "ticks"):
                if key in clip:
                    values = clip[key] if isinstance(clip[key], list) else [clip[key]] * total
                    out[key] = values[:kept - 1] + [sum(values[kept - 1:])]
            if "events" in clip:
                out["events"] = [{**event, "at": min(event["at"], kept - 1)} for event in clip["events"]]
            if "keys" in clip:
                out["keys"] = {key: min(value, kept - 1) for key, value in clip["keys"].items()}
            if "entry_frame" in clip:
                out["entry_frame"] = min(clip["entry_frame"], kept - 1)
        clips[clip_name] = out
    for out in clips.values():
        if "transitions" in out:
            out["transitions"] = [
                {**item, "entry_frame": min(item["entry_frame"], len(clips[item["to"]]["frames"]) - 1)}
                if "entry_frame" in item else item for item in out["transitions"]]
    needs_v2 = sorted(name for name, clip in clips.items()
                      if CLIP_V2_FIELDS & set(clip) or not {"duration_ms", "loop"} <= set(clip))
    if schema == "v1" and needs_v2:
        raise codeart_core.CodeArtError(f"clip(s) {', '.join(map(repr, needs_v2))} use animation_clips.v2 fields "
                                        "(ticks, loop_policy, events, keys, ...); drop --clips-schema v1")
    manifest: dict[str, Any] = {"schema": CLIPS_SCHEMAS[schema], "frames": manifest_frames,
                                "anchor_px": spec["anchor_px"], "clips": clips}
    if spec.get("states"):
        manifest["states"] = dict(spec["states"])
    manifest.update({"art_source": "code", "placeholder": False, "pixel_art": True, "sampling": "nearest"})
    return manifest, {"manifest_frames": used, "clips": report}


def check_builder_frames(spec: Mapping, plan: Mapping, used: Sequence[int], variants: Sequence[tuple]) -> None:
    """build_animation_clips needs one canvas, a root inside it and transparent + visible pixels per frame."""
    width, height = spec["canvas"]
    x, y = spec["anchor_px"]
    if not (0 <= x <= width and 0 <= y <= height):
        raise codeart_core.CodeArtError(f"anchor_px {spec['anchor_px']} must lie inside the {width}x{height} canvas "
                                        "(edges included) to export clips")
    for label, _ in variants:
        for position in used:
            alpha = plan["pixels"][(label, position)][..., 3]
            if alpha.all():
                raise codeart_core.CodeArtError(
                    f"frame {plan['frames'][position]['stem']!r} covers the whole canvas; build_animation_clips needs "
                    "transparent pixels in every frame, so leave a transparent margin or drop the clips options")


def build_clips(builder: Path, manifest: Path, bundle: Path, label: str) -> None:
    """Run generate2dsprite's build_animation_clips.py by path on one variant's manifest."""
    command = [sys.executable, str(builder), "--manifest", str(manifest), "--output-dir", str(bundle)]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                                timeout=BUILDER_TIMEOUT_S, env=environment, check=False)
    except subprocess.TimeoutExpired:
        raise codeart_core.CodeArtError(f"build_animation_clips timed out after {BUILDER_TIMEOUT_S} s "
                                        f"for variant {label!r}") from None
    if result.returncode != 0 or not (bundle / "animation-clips.json").is_file():
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise codeart_core.CodeArtError(f"build_animation_clips failed for variant {label!r} (exit "
                                        f"{result.returncode}): {detail[-1] if detail else 'no output'}")


# ----------------------------------------------------------------------------- command

def _local_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def run(args: argparse.Namespace) -> dict:
    spec_path = Path(args.spec)
    final = Path(args.output_dir)
    if os.path.lexists(final):
        raise FileExistsError(f"refusing to replace existing output {final}; choose a new --output-dir")
    if _local_inside(final, SKILL_DIR):
        raise codeart_core.CodeArtError("write outputs inside your project, not inside the codeart2d skill folder")
    if args.preview_scale is not None and not 1 <= args.preview_scale <= MAX_PREVIEW_SCALE:
        raise codeart_core.CodeArtError(f"--preview-scale must be an integer from 1 to {MAX_PREVIEW_SCALE}")
    want_clips = args.clips_manifest or args.build_clips
    builder = Path(args.clips_builder) if args.clips_builder else DEFAULT_CLIPS_BUILDER
    if args.build_clips and not builder.is_file():
        raise codeart_core.CodeArtError(f"build_animation_clips.py not found at {builder}; install the "
                                        "generate2dsprite skill beside codeart2d or pass --clips-builder")

    spec, raw = load_spec(spec_path)
    validate_spec(spec)
    if want_clips and not spec.get("clips"):
        raise codeart_core.CodeArtError("the spec has no clips to export; add clips or drop --clips-manifest and "
                                        "--build-clips")
    if want_clips and "anchor_px" not in spec:
        raise codeart_core.CodeArtError("exporting clips needs anchor_px, the shared canvas root (for example the "
                                        "bottom centre of the feet)")
    variants = select_variants(spec, args.variants)
    if args.preview_scale:
        frame_count = len(spec.get("frames") or [None])
        width, height = spec["canvas"][0] * frame_count, spec["canvas"][1] * len(variants)
        if width * height * args.preview_scale ** 2 > MAX_IMAGE_PIXELS:
            raise codeart_core.CodeArtError(f"a x{args.preview_scale} preview of {frame_count} frame(s) in "
                                            f"{len(variants)} variant(s) would exceed {MAX_IMAGE_PIXELS} pixels; "
                                            "lower --preview-scale")
    plan = frame_plan(spec, variants)
    manifest = clip_report = None
    if want_clips:
        manifest, clip_report = clips_manifests(spec, plan, args.clips_schema)
        check_builder_frames(spec, plan, clip_report["manifest_frames"], variants)

    used_dirs: set[str] = set()
    variant_dirs = {label: _local_safe_stem(label, used_dirs, "variant") for label, _ in variants}
    frames, canonical, empty = plan["frames"], plan["canonical"], plan["empty"]
    written = [position for position in range(len(frames)) if canonical[position] == position and not empty[position]]
    palette = codeart_core.parse_palette({"colors": spec["palette"], "variants": spec.get("variants") or {}})
    with forge_core.staged_output(final) as stage:
        records, outputs = [], []
        for label, variant in variants:
            colours = palette.resolve(variant)
            outline = _outline_colours(spec, colours)
            for position in written:
                target = stage / variant_dirs[label] / "frames" / f"{frames[position]['stem']}.png"
                codeart_core.save_png(plan["pixels"][(label, position)], target)
                outputs.append(target)
                records.append({"file": target.relative_to(stage).as_posix(), "variant": label,
                                "frame": frames[position]["name"] if frames[position]["name"] is not None
                                else frames[position]["stem"], "metrics": frame_qa(target, colours, outline)})
        inputs = [_file_ref(spec_path, stage)]
        frame_refs = [_file_ref(path, stage) for path in outputs]
        gait = half_cycle_check(spec, plan, args.allow_duplicate_half_cycle)
        qa = qa_envelope(records, inputs, frame_refs, spec.get("outline", {}).get("mode", "none") != "none",
                         [gait] if gait is not None else [])
        if args.strict_qc and qa["status"] == "fail":
            failing = [f"{check['id']} {check['value']} > {check['threshold']} in {', '.join(check['failing'][:3])}"
                       for check in qa["checks"] if check["status"] == "fail"]
            raise QAFailure("strict QC failed, nothing was published: " + "; ".join(failing))

        bundles = {}
        if manifest is not None:
            problems = contract_errors(manifest, "sprite", "clips_input")
            if problems:  # a defect of this tool, never of the spec
                raise RuntimeError("the clips manifest breaks clips_input: " + "; ".join(problems[:3]))
            for label, _ in variants:
                folder = stage / variant_dirs[label]
                manifest_path = folder / "clips.json"
                forge_core.write_json(manifest_path, manifest)
                outputs.append(manifest_path)
                if args.build_clips:
                    build_clips(builder, manifest_path, folder / "bundle", label)
                    bundles[label] = f"{variant_dirs[label]}/bundle"
        preview = None
        if args.preview_scale:
            rows = [np.concatenate([codeart_core.upscale_nearest(plan["pixels"][(label, position)], args.preview_scale)
                                    for position in written], axis=1) for label, _ in variants]
            preview = stage / f"preview-x{args.preview_scale}.png"
            codeart_core.save_png(np.concatenate(rows, axis=0), preview)
            outputs.append(preview)

        frame_table = []
        for position, frame in enumerate(frames):
            entry = {"index": frame["index"], "name": frame["name"]}
            if empty[position]:
                entry.update(file=None, empty=True)
            else:
                entry["file"] = f"frames/{frames[canonical[position]]['stem']}.png"
                if canonical[position] != position:
                    entry["reuses"] = frames[canonical[position]]["index"]
            frame_table.append(entry)
        details: dict[str, Any] = {
            "name": spec.get("name"), "schema": spec["schema"], "canvas": spec["canvas"],
            "anchor_px": spec.get("anchor_px"), "outline": spec.get("outline", {}).get("mode", "none"),
            "variants": {label: variant_dirs[label] for label, _ in variants}, "frames": frame_table,
        }
        if clip_report is not None:
            details["clips"] = {"manifest_schema": manifest["schema"], "manifests": {
                label: f"{variant_dirs[label]}/clips.json" for label, _ in variants}, "bundles": bundles,
                "per_clip": clip_report["clips"]}
        if preview is not None:
            details["preview"] = {"file": preview.name, "scale": args.preview_scale,
                                  "layout": "rows are variants, columns are distinct frames"}
        codeart_core.write_codeart_meta(
            stage / META_NAME, generator=TOOL_NAME, spec_sha256=forge_core.sha256_bytes(raw),
            renderer={"name": "codeart_core.render_pixelspec", "version": codeart_core.CODEART_CORE_API_VERSION},
            palette={"colors": spec["palette"], "variants": spec.get("variants") or {}}, outputs=outputs, qa=qa,
            extra={"pixelspec": details})
    final = final.parent.resolve() / final.name
    summary = {"output": str(final), "metadata": str(final / META_NAME), "qa": qa["status"],
               "variants": [label for label, _ in variants], "frames": len(written), "files": len(outputs),
               "failed_checks": [check["id"] for check in qa["checks"] if check["status"] == "fail"],
               "warned_checks": [check["id"] for check in qa["checks"] if check["status"] == "warn"]}
    if bundles:
        summary["bundles"] = [str(final / path) for path in bundles.values()]
    dropped = [frames[position]["stem"] for position in range(len(frames)) if empty[position]]
    if dropped:
        _warn(f"{len(dropped)} frame(s) render no pixel and were not written: {', '.join(dropped[:6])}")
    if gait is not None and gait["status"] == "warn":
        _warn(f"half_cycle_duplicates: {'; '.join(gait['failing'][:4])}: {gait['note']}")
    return summary  # a QA status fail (published because --strict-qc was not given) exits 1 in main (D26)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", required=True, help="PixelSpec JSON (schema codeart2d.pixelspec.v1)")
    parser.add_argument("--output-dir", required=True, help="new folder inside your project; never replaced")
    parser.add_argument("--variants", default="all",
                        help="all (default: every named variant, or the base palette), base, or names a,b")
    parser.add_argument("--clips-manifest", action="store_true",
                        help="write <variant>/clips.json for build_animation_clips (needs clips and anchor_px)")
    parser.add_argument("--build-clips", action="store_true",
                        help="also run build_animation_clips on each manifest into <variant>/bundle")
    parser.add_argument("--clips-builder", default=None,
                        help="path of build_animation_clips.py (default: the generate2dsprite skill beside this one)")
    parser.add_argument("--clips-schema", choices=tuple(CLIPS_SCHEMAS), default="v2",
                        help="clips manifest schema: v2 (default; events and ticks reach the builder) or v1 for "
                             "builders that predate the v2 reader")
    parser.add_argument("--preview-scale", type=int, default=None, metavar="N",
                        help="write preview-xN.png: every variant and distinct frame, integer nearest upscale")
    parser.add_argument("--strict-qc", action="store_true",
                        help="publish nothing unless every frame passes the pixel gates (warnings still publish)")
    parser.add_argument("--allow-duplicate-half-cycle", action="store_true",
                        help="walk/run clips: accept half-cycle frames (i, i+n/2) with silhouette IoU >= 0.95 after "
                             "you have checked that the near/far leg colours carry the stride (recorded as an override)")
    return parser


def _warn(message: str) -> None:
    print(f"warning: {forge_core.ascii_text(message)}", file=sys.stderr)


def _local_dependency_problem() -> str | None:
    """The pip command for missing numpy/Pillow, or why the skill's own modules did not import."""
    if _MISSING is None:
        return None
    import importlib.util

    missing = [name for name in ("numpy", "PIL") if importlib.util.find_spec(name) is None]
    if not missing:
        return f"error: cannot import the codeart2d libraries ({_MISSING}); reinstall the codeart2d skill"
    packages = " ".join("Pillow" if name == "PIL" else name for name in missing)
    interpreter = sys.executable.encode("ascii", "backslashreplace").decode("ascii")
    return (f"error: missing Python module(s): {', '.join(missing)}\n"
            f"install with: python -m pip install {packages}\n"
            f"(run it with the interpreter that runs this tool: {interpreter})")


def main(argv: Sequence[str] | None = None) -> int:
    if _MISSING is None:
        forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    problem = _local_dependency_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    try:
        summary = run(args)
    except KeyboardInterrupt:
        print("error: interrupted; nothing was published", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except (QAFailure, codeart_core.CodeArtError, ValueError, OSError) as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    except BaseException as error:  # noqa: BLE001  D27: never a traceback for the user, not even for a
        # BaseException such as a Rust panic from an extension; the stage is already removed
        print(f"error: internal error ({type(error).__name__}: {forge_core.ascii_text(str(error))})", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    if summary["qa"] == "fail":  # D26: published because --strict-qc was not given; the failed QA still exits 1
        print(forge_core.ascii_text(f"error: published with QA status fail: {', '.join(summary['failed_checks'])} "
                                    f"(see {summary['metadata']})"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
