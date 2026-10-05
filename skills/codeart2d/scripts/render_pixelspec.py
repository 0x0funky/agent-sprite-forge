"""Render a PixelSpec (codeart2d.pixelspec.v1) into checked 8-bit RGBA pixel-art frames.

Run from the project root; outputs go to a new folder inside the project:
  python "<skill-dir>/scripts/render_pixelspec.py" --spec slime.pixelspec.json --output-dir out/slime-v1
  python "<skill-dir>/scripts/render_pixelspec.py" --spec slime.pixelspec.json --output-dir out/slime-v1 --variants all --build-clips --preview-scale 6 --strict-qc

The spec is checked against references/schemas/codeart.schema.json#/$defs/pixelspec_v1
and against the cross-references a schema cannot express (pose, layer, frame and clip
names, palette characters, timing lengths) before anything is rendered.

Output folder (written to a stage beside it, checked, then published in one step):
  codeart-meta.json            art_source "code", spec sha256, palette, every output file
                               and a QA envelope covering every frame of every variant
  <variant>/frames/<name>.png   one 8-bit RGBA PNG per distinct frame; frames that render
                               the same pixels in every variant share one file
  <variant>/clips.json          --clips-manifest or --build-clips: manifest for
                               generate2dsprite's build_animation_clips.py
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
"""

from __future__ import annotations

import argparse
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    import numpy as np
    import codeart_core
    import forge_core
except ImportError as _import_error:  # a clean machine: main() prints the pip command instead of a traceback
    _MISSING: ImportError | None = _import_error
else:
    _MISSING = None

TOOL_NAME = "render_pixelspec.py"
TOOL_VERSION = "0.4.0"
SKILL_DIR = SCRIPTS_DIR.parent
SCHEMA_DIR = SKILL_DIR / "references" / "schemas"
DEFAULT_CLIPS_BUILDER = SKILL_DIR.parent / "generate2dsprite" / "scripts" / "build_animation_clips.py"
META_NAME = "codeart-meta.json"
BASE_VARIANT = "base"
CLIPS_V1 = "generate2dsprite.animation_clips.v1"
CLIPS_V2 = "generate2dsprite.animation_clips.v2"
# Clip fields that only the v2 clips manifest knows (plan Appendix B); a spec clip using any
# of them is written to a v2 manifest, otherwise the v1 manifest every builder accepts.
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
_WINDOWS_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])")


class QAFailure(Exception):
    """Strict QA failed; nothing is published."""


# ----------------------------------------------------------------------------- contract validation
# A Draft 2020-12 validator for the keywords the vendored contracts use, so a spec is checked
# against references/schemas itself without needing jsonschema at run time. tests compare it
# with jsonschema on every contract fixture; an unsupported keyword raises instead of passing.

_ANNOTATIONS = frozenset({"$schema", "$id", "$defs", "$comment", "title", "description", "default", "examples",
                          "format", "deprecated", "readOnly", "writeOnly"})
_ASSERTIONS = frozenset({"$ref", "type", "enum", "const", "pattern", "minLength", "maxLength", "minimum", "maximum",
                         "exclusiveMinimum", "exclusiveMaximum", "required", "properties", "additionalProperties",
                         "propertyNames", "minProperties", "maxProperties", "items", "prefixItems", "minItems",
                         "maxItems", "contains", "allOf", "anyOf", "oneOf", "not", "if", "then", "else"})


def _brief(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=False)
    return text if len(text) <= 60 else text[:57] + "..."


def _depth(error: str) -> int:
    path = error.split(": ", 1)[0]
    return path.count(".") + path.count("[")


def _is_type(value: Any, name: Any) -> bool:
    if isinstance(name, list):
        return any(_is_type(value, item) for item in name)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if isinstance(value, bool):
        return False
    if name == "integer":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, (int, float))
    raise RuntimeError(f"contract validator: unknown type {name!r}")


def _json_equal(a: Any, b: Any) -> bool:
    """JSON equality: booleans never equal numbers; 1 equals 1.0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[key], b[key]) for key in a)
    return type(a) is type(b) and a == b


def _hint(schema: Mapping) -> str:
    """The first sentences of a schema's description, to explain an anyOf/oneOf failure."""
    text = str(schema.get("description") or "")
    if not text:
        return ""
    cut = text if len(text) <= 200 else text[:200].rsplit(". ", 1)[0] + "."
    return f" ({cut})"


class _LocalContracts:
    """The vendored *.schema.json documents of a skill, addressed by file stem (common, codeart, ...)."""

    def __init__(self, directory: Path) -> None:
        self.documents: dict[str, dict] = {}
        self.ids: dict[str, str] = {}
        for path in sorted(directory.glob("*.schema.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            self.documents[document["$id"]] = document
            self.ids[path.name[:-len(".schema.json")]] = document["$id"]
        if not self.ids:
            raise codeart_core.CodeArtError(f"no contract schemas in {directory}; reinstall the codeart2d skill")

    def errors(self, instance: Any, domain: str, definition: str) -> list[str]:
        """Violations of <domain>.schema.json#/$defs/<definition> as "$.json.path: message" lines."""
        if domain not in self.ids:
            raise codeart_core.CodeArtError(f"no {domain}.schema.json among the vendored contracts")
        found: list[str] = []
        self._check(instance, {"$ref": f"#/$defs/{definition}"}, self.ids[domain], "$", found)
        return found

    def _resolve(self, reference: str, base: str) -> tuple[Any, str]:
        url = urljoin(base, reference)
        document_url, _, fragment = url.partition("#")
        if document_url not in self.documents:
            raise RuntimeError(f"contract validator: cannot resolve $ref {reference!r} from {base}")
        node: Any = self.documents[document_url]
        for part in fragment.split("/")[1:] if fragment else []:
            part = part.replace("~1", "/").replace("~0", "~")
            node = node[int(part)] if isinstance(node, list) else node[part]
        return node, document_url

    def _errors(self, value: Any, schema: Any, base: str, path: str) -> list[str]:
        found: list[str] = []
        self._check(value, schema, base, path, found)
        return found

    def _check(self, value: Any, schema: Any, base: str, path: str, out: list[str]) -> None:
        if schema is True:
            return
        if schema is False:
            out.append(f"{path}: {_brief(value)} is not allowed here")
            return
        unknown = set(schema) - _ANNOTATIONS - _ASSERTIONS
        if unknown:
            raise RuntimeError(f"contract validator does not support {sorted(unknown)}")
        if "$ref" in schema:
            target, target_base = self._resolve(schema["$ref"], base)
            self._check(value, target, target_base, path, out)
        if "type" in schema and not _is_type(value, schema["type"]):
            out.append(f"{path}: {_brief(value)} is not of type {schema['type']!r}")
            return
        if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
            out.append(f"{path}: {_brief(value)} is not one of {_brief(schema['enum'])}")
        if "const" in schema and not _json_equal(value, schema["const"]):
            out.append(f"{path}: {_brief(schema['const'])} was expected")
        if isinstance(value, str):
            self._check_string(value, schema, path, out)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            self._check_number(value, schema, path, out)
        elif isinstance(value, dict):
            self._check_object(value, schema, base, path, out)
        elif isinstance(value, list):
            self._check_array(value, schema, base, path, out)
        for sub in schema.get("allOf", ()):
            self._check(value, sub, base, path, out)
        for keyword in ("anyOf", "oneOf"):
            if keyword not in schema:
                continue
            branches = [self._errors(value, sub, base, path) for sub in schema[keyword]]
            passing = sum(1 for branch in branches if not branch)
            if passing == 0:
                deepest = max((error for branch in branches for error in branch), key=_depth)
                out.append(deepest if _depth(deepest) > _depth(path + ": ") else
                           f"{path}: {_brief(value)} is not valid under any of the given schemas{_hint(schema)}")
            elif keyword == "oneOf" and passing > 1:
                out.append(f"{path}: {_brief(value)} is valid under more than one of the given schemas{_hint(schema)}")
        if "not" in schema and not self._errors(value, schema["not"], base, path):
            out.append(f"{path}: {_brief(value)} should not be valid under {_brief(schema['not'])}")
        if "if" in schema:
            branch = "then" if not self._errors(value, schema["if"], base, path) else "else"
            if branch in schema:
                self._check(value, schema[branch], base, path, out)

    @staticmethod
    def _check_string(value: str, schema: Mapping, path: str, out: list[str]) -> None:
        if "pattern" in schema and not _pattern(schema["pattern"]).search(value):
            out.append(f"{path}: {_brief(value)} does not match {schema['pattern']!r}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: {_brief(value)} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: {_brief(value)} is too long")

    @staticmethod
    def _check_number(value: float, schema: Mapping, path: str, out: list[str]) -> None:
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: {value} is less than the minimum of {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: {value} is greater than the maximum of {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append(f"{path}: {value} is less than or equal to the minimum of {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            out.append(f"{path}: {value} is greater than or equal to the maximum of {schema['exclusiveMaximum']}")

    def _check_object(self, value: dict, schema: Mapping, base: str, path: str, out: list[str]) -> None:
        for name in schema.get("required", ()):
            if name not in value:
                out.append(f"{path}: {name!r} is a required property")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                self._check(value[key], sub, base, f"{path}.{key}", out)
        extra = schema.get("additionalProperties", True)
        others = [key for key in value if key not in properties]
        if extra is False and others:
            out.append(f"{path}: additional properties are not allowed ({', '.join(map(repr, others))} unexpected)")
        elif isinstance(extra, dict):
            for key in others:
                self._check(value[key], extra, base, f"{path}.{key}", out)
        if "propertyNames" in schema:
            for key in value:
                problems = self._errors(key, schema["propertyNames"], base, path)
                if problems:
                    out.append(f"{path}: property name {key!r} is not allowed ({problems[0].split(': ', 1)[1]})")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            out.append(f"{path}: {_brief(value)} does not have enough properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            out.append(f"{path}: {_brief(value)} has too many properties")

    def _check_array(self, value: list, schema: Mapping, base: str, path: str, out: list[str]) -> None:
        prefix = schema.get("prefixItems", [])
        for index, sub in enumerate(prefix[:len(value)]):
            self._check(value[index], sub, base, f"{path}[{index}]", out)
        if "items" in schema:
            for index in range(len(prefix), len(value)):
                self._check(value[index], schema["items"], base, f"{path}[{index}]", out)
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: {_brief(value)} is too short")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: {_brief(value)} is too long")
        if "contains" in schema and not any(not self._errors(item, schema["contains"], base, path) for item in value):
            out.append(f"{path}: {_brief(value)} does not contain items matching the given schema")


@lru_cache(maxsize=None)
def _pattern(text: str) -> re.Pattern:
    return re.compile(text)


@lru_cache(maxsize=None)
def _local_contracts(directory: str = str(SCHEMA_DIR)) -> _LocalContracts:
    return _LocalContracts(Path(directory))


def _local_contract_errors(instance: Any, domain: str, definition: str) -> list[str]:
    """Violations of a vendored contract, as "$.json.path: message" lines (empty when valid)."""
    return _local_contracts().errors(instance, domain, definition)


# ----------------------------------------------------------------------------- spec loading and checks

def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise codeart_core.CodeArtError(f"duplicate key {key!r} in the spec JSON")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise codeart_core.CodeArtError(f"the spec contains {name}, which is not JSON; use finite numbers")


def load_spec(path: Path) -> tuple[dict, bytes]:
    """Read a PixelSpec file strictly: UTF-8 JSON object, no duplicate keys, no NaN or Infinity."""
    if not path.is_file():
        raise codeart_core.CodeArtError(f"spec file not found: {path}")
    raw = path.read_bytes()
    try:
        spec = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except UnicodeDecodeError:
        raise codeart_core.CodeArtError(f"{path.name} is not UTF-8 text") from None
    except json.JSONDecodeError as error:
        raise codeart_core.CodeArtError(f"{path.name} is not valid JSON: {error}") from None
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
    errors = _local_contract_errors(spec, "codeart", "pixelspec_v1")
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


def qa_envelope(records: list[dict], inputs: list[dict], outputs: list[dict], has_outline: bool) -> dict:
    """A common qaEnvelope over every frame: one check per pixel gate, valued at the worst frame."""
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
    not_proven = list(QA_NOT_PROVEN)
    if not has_outline:
        not_proven.append("Outline continuity and L-corners: the spec has no outline, so they were not measured.")
    status = "fail" if any(check["status"] == "fail" for check in checks) else "pass"
    keep = ("visible", "partial_alpha", "off_palette", "colors", "orphans", "l_corners", "outline_gaps", "bbox")
    return {"status": status, "method": QA_METHOD, "notProven": not_proven, "checks": checks, "inputs": inputs,
            "outputs": outputs, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "frames": [{"file": record["file"], "variant": record["variant"], "frame": record["frame"],
                        **{key: record["metrics"][key] for key in keep}} for record in records]}


def _local_file_ref(path: Path, base: Path) -> dict:
    """fileRef relative to `base` (POSIX); a file on another drive is recorded by its name."""
    try:
        relative = Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:  # Windows: another drive has no relative path
        relative = path.name
    return {"path": relative, "sha256": forge_core.sha256_file(path), "bytes": path.stat().st_size}


# ----------------------------------------------------------------------------- clips

def clips_manifests(spec: Mapping, plan: Mapping) -> tuple[dict, dict]:
    """The clips manifest shared by every variant (frame files are per variant) and a report.

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
    v2 = any(CLIP_V2_FIELDS & set(clip) or not {"duration_ms", "loop"} <= set(clip) for clip in clips.values())
    manifest: dict[str, Any] = {"schema": CLIPS_V2 if v2 else CLIPS_V1, "frames": manifest_frames,
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
        manifest, clip_report = clips_manifests(spec, plan)
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
        inputs = [_local_file_ref(spec_path, stage)]
        frame_refs = [_local_file_ref(path, stage) for path in outputs]
        qa = qa_envelope(records, inputs, frame_refs, spec.get("outline", {}).get("mode", "none") != "none")
        if args.strict_qc and qa["status"] != "pass":
            failing = [f"{check['id']} {check['value']} > {check['threshold']} in {', '.join(check['failing'][:3])}"
                       for check in qa["checks"] if check["status"] == "fail"]
            raise QAFailure("strict QC failed, nothing was published: " + "; ".join(failing))

        bundles = {}
        if manifest is not None:
            problems = _local_contract_errors(manifest, "sprite", "clips_input")
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
               "variants": [label for label, _ in variants], "frames": len(written), "files": len(outputs)}
    if bundles:
        summary["bundles"] = [str(final / path) for path in bundles.values()]
    dropped = [frames[position]["stem"] for position in range(len(frames)) if empty[position]]
    if dropped:
        _warn(f"{len(dropped)} frame(s) render no pixel and were not written: {', '.join(dropped[:6])}")
    if qa["status"] != "pass":
        failing = ", ".join(check["id"] for check in qa["checks"] if check["status"] == "fail")
        _warn(f"QA status {qa['status']} ({failing}); published because --strict-qc was not given")
    return summary


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
    parser.add_argument("--preview-scale", type=int, default=None, metavar="N",
                        help="write preview-xN.png: every variant and distinct frame, integer nearest upscale")
    parser.add_argument("--strict-qc", action="store_true",
                        help="publish nothing unless every frame passes the pixel gates")
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
    except (QAFailure, codeart_core.CodeArtError, ValueError, OSError) as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    except Exception as error:  # never a traceback for the user; the stage is already removed
        print(f"error: internal error ({type(error).__name__}): {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
