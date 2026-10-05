#!/usr/bin/env python3
"""Validate packaged animations against the runtime contract (animation.json 3.0; 2.0 read-only).

Pass package folders, animation.json files or a parent folder whose subfolders hold packages.
Every failure names its rule:

  schema           animation_v3 JSON Schema (needs jsonschema; skipped without it)
  paths            no absolute path or URL anywhere; packaged files stay inside the package
  files            every packaged file exists with the recorded sha256 and size
  timing           frameCount, sourceIndices, durationsMs, durationSeconds, fps and atlas pages agree
  anchor           sourceAnchor lies inside sourceSize
  impact-hold      impactMs and holdMs lie inside the clip
  events           event times lie inside the clip and name the frame shown at that time
  loop-flag        loop is an explicit boolean that matches loopPolicy
  states           --require-states names exist among the packages
  poster           the poster has the encoded size and equals the first atlas frame
  padding-embed    action padding embeds the unscaled base canvas at a whole-pixel offset
  packed-geometry  packed halves are the logical size padded right/bottom to even pixels
  tiers            mobile tiers scale the content without upscaling, at most 60 fps
  static-sprite    --static-sprite/--static-size and --static-anchor match sourceSize/sourceAnchor
  ffprobe-dimensions, ffprobe-timestamps, ffprobe-duration (2 ms), ffprobe-packets, vp9-alpha
                   container facts of every video against the manifest (skipped without ffprobe)
  qa-incomplete, qa-inconsistent, qa-failed, qa-stale, qa-partial, qa-foreign
                   hash-bound QA: the manifest qa and verify-qa.json state method and notProven,
                   agree with their checks and bind exactly the current files by sha256
  verify           --require-verify: a verify-qa.json for this animation.json
  review           --require-review: reviewStatus accepted or accept_with_mask

Prints one JSON line on success. Otherwise prints "error: <rule>: <package>: <message>" lines
and exits 1. --report writes a QA envelope, only when every check passes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple, Sequence

import numpy as np

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import engine_export  # noqa: E402  (same skill)
import forge_av  # noqa: E402  (the skill's vendored copies)
import forge_core  # noqa: E402

VALIDATOR_VERSION = "1.0.0"
VALIDATION_SCHEMA = "video2dsprite.validation.v1"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "references" / "schemas"
RULES = ("schema", "paths", "files", "timing", "anchor", "impact-hold", "events", "loop-flag", "states", "poster",
         "padding-embed", "packed-geometry", "tiers", "static-sprite", "ffprobe-dimensions", "ffprobe-timestamps",
         "ffprobe-duration", "ffprobe-packets", "vp9-alpha", "qa-incomplete", "qa-inconsistent", "qa-failed",
         "qa-stale", "qa-partial", "qa-foreign", "verify", "review")
FFPROBE_RULES = ("ffprobe-dimensions", "ffprobe-timestamps", "ffprobe-duration", "ffprobe-packets", "vp9-alpha")
DURATION_TOLERANCE_MS = 2.0
ANCHOR_TOLERANCE_PX = 0.01
QA_STATUSES = ("pass", "fail", "warn", "needs-visual-review")
QA_CHECK_STATUSES = QA_STATUSES + ("skipped",)
QA_KEYS = ("status", "method", "notProven", "checks", "inputs", "outputs", "tool")
_ABSOLUTE = re.compile(r"(?:[A-Za-z]:[\\/]|[\\/]|[A-Za-z][A-Za-z0-9+.-]*://)")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class Finding(NamedTuple):
    """One rule outcome for one package (``status`` fail or skipped)."""
    rule: str
    package: str
    message: str
    status: str = "fail"


class Options(NamedTuple):
    require_states: tuple[str, ...] = ()
    static_size: tuple[int, int] | None = None
    static_anchor: tuple[float, float] | None = None
    require_verify: bool = False
    require_review: bool = False


# --------------------------------------------------------------------------- discovery

def discover(paths: Sequence[Path]) -> list[Path]:
    """animation.json files named by ``paths``: files, package folders or folders of packages."""
    found: list[Path] = []
    for path in paths:
        if path.is_file():
            found.append(path)
        elif (path / engine_export.MANIFEST_FILE).is_file():
            found.append(path / engine_export.MANIFEST_FILE)
        elif path.is_dir():
            children = sorted(path.glob(f"*/{engine_export.MANIFEST_FILE}"))
            if not children:
                raise ValueError(f"no animation.json in {path.name} or its subfolders")
            found += children
        else:
            raise ValueError(f"not found: {path.name}")
    return list(dict.fromkeys(p.resolve() for p in found))


# --------------------------------------------------------------------------- helpers

def _strings(value: Any, pointer: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield pointer or "/", value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(item, f"{pointer}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{pointer}/{index}")


def _package_path(text: Any) -> str | None:
    """What is wrong with a packaged file name, or None for a plain relative path inside the package."""
    if not isinstance(text, str) or not text:
        return "is not a file name"
    if _ABSOLUTE.match(text) or "\\" in text:
        return "is an absolute or non-POSIX path"
    if ".." in text.split("/"):
        return "leaves the package folder"
    return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _durations(manifest: Mapping) -> list[int] | None:
    durations = manifest.get("durationsMs")
    if isinstance(durations, list) and durations and all(_whole(d) and d >= 1 for d in durations):
        return durations
    return None


def _duration_ms(manifest: Mapping) -> float | None:
    durations = _durations(manifest)
    if durations is not None:
        return float(sum(durations))
    seconds = manifest.get("durationSeconds")
    return seconds * 1000 if _number(seconds) else None


def _ms(total: float | None) -> str:
    return "unknown-length" if total is None else f"{total:g} ms"


def _frame_at(durations: Sequence[int], at_ms: int) -> int:
    edges = np.cumsum([0, *durations[:-1]])
    return int(np.searchsorted(edges, at_ms, side="right") - 1)


def _label(path: Path) -> str:
    try:
        name = json.loads(path.read_text(encoding="utf-8")).get("name")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        name = None
    return str(name) if isinstance(name, str) and name else path.parent.name


def schema_validator():
    """Draft 2020-12 validator of video.schema.json#/$defs/animation_v3, or None without jsonschema."""
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry
        from referencing.jsonschema import DRAFT202012
    except ImportError:
        return None
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCHEMA_DIR.glob("*.schema.json"))]
    registry = Registry().with_resources((s["$id"], DRAFT202012.create_resource(s)) for s in schemas)
    video = next(s for s in schemas if s["$id"].endswith("/video.schema.json"))
    return Draft202012Validator({"$ref": f"{video['$id']}#/$defs/animation_v3"}, registry=registry)


def qa_errors(qa: Any, *, expected_outputs: Mapping[str, str], current: Mapping[str, str | None],
              expected_inputs: Mapping[str, str] | None = None,
              accept: Sequence[str] = ("pass", "warn", "needs-visual-review")) -> list[tuple[str, str]]:
    """Hash-bound QA rules for one QA envelope, as (rule, message) pairs.

    ``expected_outputs`` maps every file the QA must cover to its manifest sha256, ``current``
    to the sha256 of its bytes now (None when missing). The envelope must state method,
    notProven, checks, inputs, outputs and tool (qa-incomplete), keep a pass free of failed
    checks (qa-inconsistent), have a status in ``accept`` (qa-failed) and bind exactly the
    expected files with their current hashes (qa-partial, qa-foreign, qa-stale). Every
    ``expected_inputs`` entry must also be among its inputs with that sha256.
    """
    if not isinstance(qa, Mapping):
        return [("qa-incomplete", "QA is not an object")]
    errors: list[tuple[str, str]] = []
    missing = [key for key in QA_KEYS if key not in qa]
    if missing:
        errors.append(("qa-incomplete", f"QA lacks {', '.join(missing)}"))
    if "method" in qa and not (isinstance(qa["method"], str) and qa["method"].strip()):
        errors.append(("qa-incomplete", "QA method must say how the files were judged"))
    if "notProven" in qa and not (isinstance(qa["notProven"], list) and qa["notProven"]
                                  and all(isinstance(t, str) and t.strip() for t in qa["notProven"])):
        errors.append(("qa-incomplete", "QA notProven must list what the checks do not prove"))
    tool = qa.get("tool")
    if "tool" in qa and not (isinstance(tool, Mapping)
                             and all(isinstance(tool.get(k), str) and tool[k] for k in ("name", "version"))):
        errors.append(("qa-incomplete", "QA tool must give a name and a version"))
    for key in ("inputs", "outputs"):
        if key in qa and not (isinstance(qa[key], list) and all(
                isinstance(r, Mapping) and isinstance(r.get("path"), str)
                and isinstance(r.get("sha256"), str) and _SHA256.fullmatch(r["sha256"]) for r in qa[key])):
            errors.append(("qa-incomplete", f"QA {key} must be fileRefs {{path, sha256}}"))
    checks = qa.get("checks")
    if "checks" in qa and not (isinstance(checks, list) and all(
            isinstance(c, Mapping) and isinstance(c.get("id"), str) and c.get("status") in QA_CHECK_STATUSES
            for c in checks)):
        errors.append(("qa-inconsistent", "QA checks must be {id, status} records with a known status"))
        checks = []
    status = qa.get("status")
    if "status" in qa:
        if status not in QA_STATUSES:
            errors.append(("qa-inconsistent", f"QA status {status!r} is not a QA status"))
        elif status == "pass" and any(c.get("status") == "fail" for c in checks or []):
            errors.append(("qa-inconsistent", "QA status is pass but a check failed"))
        elif status not in accept:
            errors.append(("qa-failed", f"QA status is {status}"))
    outputs: dict[str, str] = {}
    for record in qa.get("outputs") if isinstance(qa.get("outputs"), list) else []:
        if isinstance(record, Mapping) and isinstance(record.get("path"), str):
            if record["path"] in outputs:
                errors.append(("qa-foreign", f"QA lists {record['path']} twice"))
            outputs[record["path"]] = str(record.get("sha256"))
    for name, sha in expected_outputs.items():
        if name not in outputs:
            errors.append(("qa-partial", f"QA does not cover {name}"))
        elif outputs[name] != sha or outputs[name] != current.get(name):
            errors.append(("qa-stale", f"QA judged other bytes of {name} (its sha256 differs from the file now)"))
    for name in outputs:
        if name not in expected_outputs:
            errors.append(("qa-foreign", f"QA covers {name}, which this package does not ship"))
    if expected_inputs:
        inputs = {r.get("path"): r.get("sha256") for r in qa.get("inputs") or [] if isinstance(r, Mapping)}
        for name, sha in expected_inputs.items():
            if name not in inputs:
                errors.append(("qa-partial", f"QA does not bind its input {name}"))
            elif inputs[name] != sha:
                errors.append(("qa-stale", f"QA was made for another {name} (its sha256 differs)"))
    return errors


# --------------------------------------------------------------------------- per-package rules

class _Context:
    """One package being validated: its folder, manifest and findings."""

    def __init__(self, manifest_path: Path, manifest: dict) -> None:
        self.path, self.folder, self.manifest = manifest_path, manifest_path.parent, manifest
        self.label = str(manifest.get("name") or manifest_path.parent.name)
        self.v3 = manifest.get("schemaVersion") == "3.0"
        self.findings: list[Finding] = []
        self.unsafe: set[str] = set()
        self._sha: dict[str, str | None] = {}

    def fail(self, rule: str, message: str, status: str = "fail") -> None:
        self.findings.append(Finding(rule, self.label, forge_core.ascii_text(message), status))

    def sha(self, name: str) -> str | None:
        """sha256 of a packaged file now; None when it is missing or its name is unsafe."""
        if name not in self._sha:
            path = self.folder / name
            self._sha[name] = None if name in self.unsafe or not path.is_file() else forge_core.sha256_file(path)
        return self._sha[name]


def _check_schema(ctx: _Context, validator: Any) -> None:
    if validator is None:
        ctx.fail("schema", "jsonschema is not installed (python -m pip install -r requirements-dev.txt)", "skipped")
        return
    for error in sorted(validator.iter_errors(ctx.manifest), key=lambda e: list(map(str, e.absolute_path)))[:12]:
        ctx.fail("schema", f"{error.json_path}: {error.message}")


def _check_paths(ctx: _Context) -> None:
    for pointer, text in _strings(ctx.manifest):
        if _ABSOLUTE.match(text):
            ctx.fail("paths", f"{pointer} holds an absolute path or URL: {text[:60]}")
    named = [(role, record.get("file")) for role, record in engine_export.package_media(ctx.manifest)]
    if "provenanceFile" in ctx.manifest:
        named.append(("provenanceFile", ctx.manifest["provenanceFile"]))
    for role, name in named:
        problem = _package_path(name)
        if problem:
            ctx.unsafe.add(str(name))
            ctx.fail("paths", f"{role} file {str(name)[:60]} {problem}")


def _check_files(ctx: _Context) -> None:
    for role, record in engine_export.package_media(ctx.manifest):
        name = str(record.get("file"))
        if name in ctx.unsafe:
            continue
        path = ctx.folder / name
        if not path.is_file():
            ctx.fail("files", f"{role} file {name} is missing")
        elif ctx.sha(name) != record.get("sha256"):
            ctx.fail("files", f"{role} file {name} changed: its sha256 differs from the manifest")
        elif "bytes" in record and path.stat().st_size != record["bytes"]:
            ctx.fail("files", f"{role} file {name} is {path.stat().st_size} bytes, the manifest says {record['bytes']}")
    provenance = ctx.manifest.get("provenanceFile")
    if isinstance(provenance, str) and provenance not in ctx.unsafe and not (ctx.folder / provenance).is_file():
        ctx.fail("files", f"provenanceFile {provenance} is missing")


def _check_timing(ctx: _Context) -> None:
    m = ctx.manifest
    count = m.get("frameCount")
    if not _whole(count) or count < 1:
        ctx.fail("timing", "frameCount must be a whole number >= 1")
        return
    if ctx.v3:
        indices, durations = m.get("sourceIndices"), _durations(m)
        if not isinstance(indices, list) or len(indices) != count:
            ctx.fail("timing", f"sourceIndices must hold one entry per frame ({count})")
        if durations is None or len(durations) != count:
            ctx.fail("timing", f"durationsMs must hold one whole-millisecond duration per frame ({count})")
        elif _number(m.get("durationSeconds")) and abs(sum(durations) - m["durationSeconds"] * 1000) > 1:
            ctx.fail("timing", f"durationsMs sum to {sum(durations)} ms but durationSeconds is {m['durationSeconds']}")
    try:
        fps = engine_export.manifest_fps(m)
    except ValueError as exc:
        ctx.fail("timing", str(exc))
        fps = None
    if fps is not None and _number(m.get("durationSeconds")) and abs(float(count / fps) - m["durationSeconds"]) > 0.001:
        ctx.fail("timing", f"frameCount / fps is {float(count / fps):.6f} s but durationSeconds is "
                           f"{m['durationSeconds']}")
    pages = (m.get("fallback") or {}).get("pages") or []
    covered = 0
    for page in sorted(pages, key=lambda p: p.get("firstFrame", 0) if isinstance(p, dict) else 0):
        if not (isinstance(page, dict) and page.get("firstFrame") == covered and _whole(page.get("frameCount"))
                and page["frameCount"] >= 1 and _whole(page.get("columns")) and page["columns"] >= 1):
            ctx.fail("timing", "atlas pages must cover the frames contiguously from frame 0")
            return
        covered += page["frameCount"]
    if covered != count:
        ctx.fail("timing", f"atlas pages hold {covered} frames, frameCount is {count}")


def _check_anchor(ctx: _Context) -> None:
    size, anchor = ctx.manifest.get("sourceSize"), ctx.manifest.get("sourceAnchor")
    if not (isinstance(size, list) and len(size) == 2 and all(_number(v) and v > 0 for v in size)
            and isinstance(anchor, list) and len(anchor) == 2 and all(_number(v) for v in anchor)):
        ctx.fail("anchor", "sourceSize and sourceAnchor must be two finite numbers each")
    elif not (0 <= anchor[0] <= size[0] and 0 <= anchor[1] <= size[1]):
        ctx.fail("anchor", f"sourceAnchor {anchor} lies outside sourceSize {size}")


def _check_instants(ctx: _Context) -> None:
    total = _duration_ms(ctx.manifest)
    for key in ("impactMs", "holdMs"):
        if key in ctx.manifest:
            value = ctx.manifest[key]
            if not _whole(value) or total is None or not 0 <= value < total:
                ctx.fail("impact-hold", f"{key} {value} must be a whole millisecond inside the {_ms(total)} clip")


def _check_events(ctx: _Context) -> None:
    events = ctx.manifest.get("events")
    if events is None:
        return
    if not isinstance(events, list):
        ctx.fail("events", "events must be a list")
        return
    total, durations = _duration_ms(ctx.manifest), _durations(ctx.manifest)
    for index, event in enumerate(events):
        at = event.get("atMs") if isinstance(event, dict) else None
        if not _whole(at) or total is None or not 0 <= at < total:
            ctx.fail("events", f"event {index} at {at} ms lies outside the {_ms(total)} clip")
        elif durations is not None and "frame" in event and event["frame"] != _frame_at(durations, at):
            ctx.fail("events", f"event {index} at {at} ms names frame {event['frame']}, but frame "
                               f"{_frame_at(durations, at)} is shown then")


def _check_loop(ctx: _Context) -> None:
    loop = ctx.manifest.get("loop")
    if not isinstance(loop, bool):
        ctx.fail("loop-flag", "loop must be an explicit true or false")
    elif ctx.v3:
        policy = ctx.manifest.get("loopPolicy")
        if policy not in engine_export.LOOP_POLICIES:
            ctx.fail("loop-flag", f"loopPolicy must be one of {', '.join(engine_export.LOOP_POLICIES)}")
        elif (policy != "oneshot") != loop:
            ctx.fail("loop-flag", f"loop {str(loop).lower()} contradicts loopPolicy {policy}")


def _check_poster(ctx: _Context) -> None:
    poster = ctx.manifest.get("poster")
    if not isinstance(poster, dict) or str(poster.get("file")) in ctx.unsafe:
        return
    path = ctx.folder / str(poster.get("file"))
    if not path.is_file():
        return  # the files rule reports it
    image = np.asarray(forge_core.load_rgba(path)[0])
    size = ctx.manifest.get("encodedSize")
    if [image.shape[1], image.shape[0]] != size:
        ctx.fail("poster", f"poster is {image.shape[1]}x{image.shape[0]}, the encoded canvas is {size}")
        return
    try:
        first = engine_export.atlas_frames(ctx.folder, ctx.manifest)[0]
    except (ValueError, OSError, KeyError, IndexError, TypeError):
        return  # the timing or files rule reports an unreadable atlas
    if not np.array_equal(image, first):
        ctx.fail("poster", "poster differs from the first atlas frame")


def _check_padding(ctx: _Context) -> None:
    block = ctx.manifest.get("registration")
    if not isinstance(block, dict) or "baseSize" not in block or "baseAnchor" not in block:
        return
    size, anchor = ctx.manifest.get("sourceSize"), ctx.manifest.get("sourceAnchor")
    base, base_anchor, padding = block["baseSize"], block["baseAnchor"], block.get("padding")
    try:
        offset = [anchor[i] - base_anchor[i] for i in range(2)]
        embedded = all(offset[i] >= 0 and abs(offset[i] - round(offset[i])) < 1e-7 and offset[i] + base[i] <= size[i]
                       for i in range(2))
    except (TypeError, IndexError, KeyError):
        ctx.fail("padding-embed", "registration baseSize/baseAnchor and sourceSize/sourceAnchor must be pairs")
        return
    if not embedded:
        ctx.fail("padding-embed", f"base canvas {base} with anchor {base_anchor} does not sit at a whole-pixel "
                                  f"offset inside {size} (anchor {anchor})")
    elif padding is not None and not (
            isinstance(padding, list) and len(padding) == 4 and padding[0] == round(offset[0])
            and padding[1] == round(offset[1]) and padding[0] + base[0] + padding[2] == size[0]
            and padding[1] + base[1] + padding[3] == size[1]):
        ctx.fail("padding-embed", f"padding {padding} does not turn base {base} at anchor {base_anchor} into "
                                  f"{size} at anchor {anchor}")


def _halves(record: Mapping, label: str, ctx: _Context, rule: str) -> bool:
    width, height = record.get("width"), record.get("height")
    if not (_whole(width) and _whole(height) and width >= 1 and height >= 1):
        ctx.fail(rule, f"{label} width and height must be whole pixels")
        return False
    half_w, half_h = record.get("halfWidth", width), record.get("halfHeight", height)
    for name, half, logical in (("halfWidth", half_w, width), ("halfHeight", half_h, height)):
        if not _whole(half) or half % 2 or not logical <= half <= logical + 1:
            ctx.fail(rule, f"{label} {name} {half} must be {logical} padded right/bottom to an even size")
            return False
    if "encodedSize" in record and record["encodedSize"] != [2 * half_w, half_h]:
        ctx.fail(rule, f"{label} encodedSize {record['encodedSize']} must be [{2 * half_w}, {half_h}]")
    if record.get("layout", forge_av.PACKED_LAYOUT) != forge_av.PACKED_LAYOUT:
        ctx.fail(rule, f"{label} layout must be {forge_av.PACKED_LAYOUT}")
    return True


def _check_packed(ctx: _Context) -> None:
    packed = ctx.manifest.get("packedAlpha")
    if isinstance(packed, dict) and _halves(packed, "packedAlpha", ctx, "packed-geometry") and ctx.v3 \
            and [packed["width"], packed["height"]] != ctx.manifest.get("contentSize"):
        ctx.fail("packed-geometry", f"packedAlpha {packed['width']}x{packed['height']} must be contentSize "
                                    f"{ctx.manifest.get('contentSize')}")


def _check_tiers(ctx: _Context) -> None:
    tiers = ctx.manifest.get("mobilePackedAlpha")
    if not isinstance(tiers, list):
        return
    content, names = ctx.manifest.get("contentSize"), set()
    for tier in (t for t in tiers if isinstance(t, dict)):
        label = f"tier {tier.get('tier')}"
        if tier.get("tier") in names:
            ctx.fail("tiers", f"{label} is listed twice")
        names.add(tier.get("tier"))
        if not _halves(tier, label, ctx, "tiers"):
            continue
        source = [tier.get("sourceWidth"), tier.get("sourceHeight")]
        if content is not None and source != content:
            ctx.fail("tiers", f"{label} sourceWidth/sourceHeight {source} must be contentSize {content}")
        elif not all(_whole(v) and v >= 1 for v in source) or tier["width"] > source[0] or tier["height"] > source[1]:
            ctx.fail("tiers", f"{label} {tier['width']}x{tier['height']} must not upscale {source}")
        elif abs(tier["width"] * source[1] - tier["height"] * source[0]) > max(source):
            ctx.fail("tiers", f"{label} {tier['width']}x{tier['height']} changes the aspect of {source}")
        try:
            fps = engine_export.parse_rate(tier.get("fps"), f"{label} fps", engine_export.MAX_FPS)
            if "maxFps" in tier and fps > engine_export.parse_rate(tier["maxFps"], f"{label} maxFps"):
                ctx.fail("tiers", f"{label} fps {tier['fps']} exceeds its maxFps {tier['maxFps']}")
        except ValueError as exc:
            ctx.fail("tiers", str(exc))


def _check_static(ctx: _Context, options: Options) -> None:
    if options.static_size is None:
        return
    size, anchor = ctx.manifest.get("sourceSize"), ctx.manifest.get("sourceAnchor")
    if list(options.static_size) != size:
        ctx.fail("static-sprite", f"sourceSize {size} differs from the static sprite canvas {list(options.static_size)}")
    if not (isinstance(anchor, list) and len(anchor) == 2 and all(
            _number(a) and abs(a - b) <= ANCHOR_TOLERANCE_PX for a, b in zip(anchor, options.static_anchor))):
        ctx.fail("static-sprite", f"sourceAnchor {anchor} differs from the static sprite root "
                                  f"{list(options.static_anchor)} by more than {ANCHOR_TOLERANCE_PX} px")


def _videos(ctx: _Context) -> list[tuple[str, dict, list[int], int | None, float | None]]:
    """(role, record, container size, packets, duration ms) for every video the manifest names."""
    m = ctx.manifest
    main_ms = m["durationSeconds"] * 1000 if _number(m.get("durationSeconds")) else None
    count = m.get("frameCount") if _whole(m.get("frameCount")) else None
    videos = []
    if isinstance(m.get("webm"), dict) and isinstance(m.get("encodedSize"), list):
        videos.append(("webm", m["webm"], list(m["encodedSize"]), count, main_ms))
    packed = [("packedAlpha", m.get("packedAlpha"))]
    packed += [(f"tier:{t.get('tier')}", t) for t in m.get("mobilePackedAlpha") or [] if isinstance(t, dict)]
    for role, record in packed:
        if not isinstance(record, dict) or not _whole(record.get("width")) or not _whole(record.get("height")):
            continue
        size = [2 * record.get("halfWidth", record["width"]), record.get("halfHeight", record["height"])]
        if role == "packedAlpha":
            videos.append((role, record, size, count, main_ms))
            continue
        frames = record.get("frameCount") if _whole(record.get("frameCount")) else None
        duration = float(record["durationMs"]) if _number(record.get("durationMs")) else None
        videos.append((role, record, size, frames, duration))
    return videos


def _check_ffprobe(ctx: _Context, available: bool) -> None:
    videos = [v for v in _videos(ctx) if str(v[1].get("file")) not in ctx.unsafe
              and (ctx.folder / str(v[1].get("file"))).is_file()]
    if not videos:
        return
    if not available:
        for rule in FFPROBE_RULES:
            ctx.fail(rule, "ffprobe is not on PATH; container facts were not checked", "skipped")
        return
    for role, record, size, packets, duration in videos:
        path = ctx.folder / record["file"]
        try:
            info = forge_av.probe(path)
        except (forge_av.ForgeAVError, ValueError) as exc:
            ctx.fail("ffprobe-dimensions", f"{role} {record['file']} cannot be probed: {exc}")
            continue
        if [info["width"], info["height"]] != size:
            ctx.fail("ffprobe-dimensions", f"{role} {record['file']} is {info['width']}x{info['height']}, "
                                           f"the manifest implies {size[0]}x{size[1]}")
        if not forge_av.timestamps_increasing(path):
            ctx.fail("ffprobe-timestamps", f"{role} {record['file']} decode timestamps do not strictly increase")
        if duration is not None:
            measured = forge_av.duration_ms(path)
            if abs(measured - duration) > DURATION_TOLERANCE_MS:
                ctx.fail("ffprobe-duration", f"{role} {record['file']} lasts {measured:.3f} ms, the manifest says "
                                             f"{duration:.3f} ms (tolerance {DURATION_TOLERANCE_MS:g} ms)")
        if packets is not None and forge_av.packet_count(path) != packets:
            ctx.fail("ffprobe-packets", f"{role} {record['file']} holds {forge_av.packet_count(path)} packets "
                                        f"for {packets} frames")
        if role == "webm" and not info["vp9_alpha"]:
            ctx.fail("vp9-alpha", f"webm {record['file']} is {info['codec']} without the VP9 AlphaMode tag; "
                                  "it would play on black")


def _check_qa(ctx: _Context, options: Options) -> None:
    media = engine_export.package_media(ctx.manifest)
    if ctx.v3:
        expected = {str(r.get("file")): str(r.get("sha256")) for _, r in media}
        for rule, message in qa_errors(ctx.manifest.get("qa"), expected_outputs=expected,
                                       current={name: ctx.sha(name) for name in expected}):
            ctx.fail(rule, f"animation.json qa: {message}")
    else:
        ctx.fail("qa-incomplete", "animation.json 2.0 QA is not hash-bound; repackage for 3.0", "skipped")
    report_path = ctx.folder / engine_export.VERIFY_FILE
    if not report_path.is_file():
        if options.require_verify:
            ctx.fail("verify", f"no {engine_export.VERIFY_FILE}; run engine_export.py verify --package <folder>")
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        ctx.fail("qa-incomplete", f"{engine_export.VERIFY_FILE} is not valid JSON: {exc}")
        return
    if not isinstance(report, Mapping) or report.get("schema") != engine_export.VERIFY_SCHEMA:
        ctx.fail("qa-incomplete", f"{engine_export.VERIFY_FILE} must have schema {engine_export.VERIFY_SCHEMA}")
        return
    encoded = {str(r.get("file")): str(r.get("sha256")) for role, r in media if role not in ("poster", "atlas")}
    for rule, message in qa_errors(report, expected_outputs=encoded, current={name: ctx.sha(name) for name in encoded},
                                   expected_inputs={engine_export.MANIFEST_FILE: forge_core.sha256_file(ctx.path)},
                                   accept=("pass", "warn")):
        ctx.fail(rule, f"{engine_export.VERIFY_FILE}: {message}")


def validate_manifest(path: Path, options: Options = Options(), *, validator: Any = None,
                      ffprobe: bool | None = None) -> list[Finding]:
    """Every per-package rule for one animation.json (``states`` is checked across packages)."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [Finding("schema", path.parent.name, forge_core.ascii_text(f"unreadable animation.json: {exc}"))]
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") not in ("2.0", "3.0"):
        return [Finding("schema", path.parent.name, "animation.json must be an object with schemaVersion 3.0 or 2.0")]
    ctx = _Context(path, manifest)
    _check_schema(ctx, validator)
    _check_paths(ctx)
    _check_files(ctx)
    _check_timing(ctx)
    _check_anchor(ctx)
    _check_instants(ctx)
    _check_events(ctx)
    _check_loop(ctx)
    _check_poster(ctx)
    _check_padding(ctx)
    _check_packed(ctx)
    _check_tiers(ctx)
    _check_static(ctx, options)
    _check_ffprobe(ctx, shutil.which("ffprobe") is not None if ffprobe is None else ffprobe)
    _check_qa(ctx, options)
    if options.require_review and manifest.get("reviewStatus") not in ("accepted", "accept_with_mask"):
        ctx.fail("review", f"reviewStatus is {manifest.get('reviewStatus')}; package with --review <verdict>")
    return ctx.findings


def validate(paths: Sequence[Path], options: Options = Options(), *, ffprobe: bool | None = None) -> dict:
    """Validate every package named by ``paths``: ``{"manifests", "labels", "findings", "failed"}``."""
    manifests = discover(paths)
    validator = schema_validator()
    findings: list[Finding] = []
    for path in manifests:
        findings += validate_manifest(path, options, validator=validator, ffprobe=ffprobe)
    labels = [_label(path) for path in manifests]
    missing = [state for state in options.require_states if state not in labels]
    if missing:
        findings.append(Finding("states", "*", f"missing required states: {', '.join(missing)} "
                                               f"(have {', '.join(sorted(labels))})"))
    return {"manifests": manifests, "labels": labels, "findings": findings,
            "failed": [f for f in findings if f.status == "fail"]}


# --------------------------------------------------------------------------- CLI

def validation_report(result: Mapping, report_path: Path) -> dict:
    """QA envelope of a passing validation: one check per rule and package."""
    base = report_path.resolve().parent
    checks = []
    for label in result["labels"]:
        for rule in RULES:
            hits = [f for f in result["findings"] if f.rule == rule and f.package in (label, "*")]
            checks.append({"id": f"{label}.{rule}", "status": "skipped" if hits else "pass", "value": None,
                           "threshold": None})
    inputs = [{"path": engine_export.relative_dir(p, base) or p.name, "sha256": forge_core.sha256_file(p),
               "bytes": p.stat().st_size} for p in result["manifests"]]
    return {
        "schema": VALIDATION_SCHEMA, "status": "pass",
        "method": "validate_animation.py: animation_v3 schema, cross-field timing, geometry and event rules, ffprobe "
                  "container facts and sha256-bound QA of every package",
        "notProven": ["Visual quality, identity and motion of the art.",
                      "Playback in a browser, Safari, WebGL or on a device."],
        "checks": checks, "inputs": inputs, "outputs": [],
        "tool": {"name": "validate_animation.py", "version": VALIDATOR_VERSION},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="validate_animation.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="package folders, animation.json files or folders of packages")
    parser.add_argument("--require-states", help="comma-separated clip names that must exist, e.g. idle,walk,attack")
    parser.add_argument("--static-sprite", help="static world sprite PNG whose canvas must equal sourceSize")
    parser.add_argument("--static-size", help="static world sprite canvas W,H (instead of --static-sprite)")
    parser.add_argument("--static-anchor", help="static world sprite root X,Y; goes with a static sprite or size")
    parser.add_argument("--require-verify", action="store_true", help="fail without a fresh verify-qa.json")
    parser.add_argument("--require-review", action="store_true", help="fail unless a review verdict accepted the clip")
    parser.add_argument("--report", help="write a validation QA envelope here (must not exist; only on success)")
    return parser


def options_from_args(args: argparse.Namespace) -> Options:
    if args.static_sprite and args.static_size:
        raise ValueError("use --static-sprite or --static-size, not both")
    static_size = None
    if args.static_sprite:
        static_size = forge_core.load_rgba(args.static_sprite)[0].size
    elif args.static_size:
        static_size = engine_export.pair(args.static_size, (), True)
    if (static_size is None) != (args.static_anchor is None):
        raise ValueError("--static-anchor goes with --static-sprite or --static-size")
    states = tuple(s.strip() for s in (args.require_states or "").split(",") if s.strip())
    return Options(states, None if static_size is None else (int(static_size[0]), int(static_size[1])),
                   None if args.static_anchor is None else engine_export.pair(args.static_anchor, ()),
                   args.require_verify, args.require_review)


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        report_path = Path(args.report) if args.report else None
        if report_path is not None and os.path.lexists(report_path):
            raise ValueError(f"report already exists: {report_path.name}")
        result = validate([Path(p) for p in args.paths], options_from_args(args))
        if result["failed"]:
            for finding in result["failed"]:
                print(f"error: {finding.rule}: {finding.package}: {finding.message}", file=sys.stderr)
            return 1
        if report_path is not None:
            stage = report_path.with_name(f".{report_path.name}.{os.getpid()}.tmp")
            try:
                forge_core.write_json(stage, validation_report(result, report_path))
                forge_core.publish_file_no_replace(stage, report_path)
            finally:
                stage.unlink(missing_ok=True)
        print(json.dumps({"status": "pass", "packages": result["labels"],
                          "manifests": [str(p) for p in result["manifests"]],
                          "skipped": sorted({f.rule for f in result["findings"] if f.status == "skipped"}),
                          "report": str(report_path.resolve()) if report_path else None}))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI surface: tracebacks are never user-facing
        print("error: " + forge_core.ascii_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
