#!/usr/bin/env python3
"""Validate and read playable map bundles (generate2dmap.map_bundle.v2; v1 stays readable).

A map bundle keeps a playable map as data: world size, tile layers, placed props with
ground footprints, collision, portals, spawns, anchors and interactions. map_nav.py
(collision, reachability, portal checks) and export_tiled.py (Tiled TMJ/TSX) read bundles
through load_bundle() and draw them with render_map().

Verbs:
  validate  check the contract (the vendored references/schemas/map.schema.json, evaluated
            here without third-party packages), every referenced file and its sha256, and
            the cross-field rules a schema cannot express (tile indices, wang and blob data,
            portal targets, unique ids, material colours, ...).
  hash      write a copy of the bundle with the sha256 of every referenced file filled in.

Paths inside a bundle are POSIX paths relative to the bundle file.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)

TOOL_NAME = "map_bundle.py"
TOOL_VERSION = "1.0.0"
SCHEMA_V1 = "generate2dmap.map_bundle.v1"
SCHEMA_V2 = "generate2dmap.map_bundle.v2"
TILESET_SCHEMA = "generate2dmap.tileset.v1"
REPORT_SCHEMA = "generate2dmap.map_bundle_report.v1"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "references" / "schemas"

OCCLUSION_CLASSES = ("low", "tall", "foreground")
OCCUPANT_POLICIES = ("y_sort", "rear_shift_and_fade", "static_front", "static_back")
FREE, BLOCK, ONE_WAY = 0, 1, 2
EMPTY_TILE = -1
BLOB_BITS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
# A blob diagonal counts only when both adjacent edges are set (plan Appendix B, tile).
_BLOB_DIAGONALS = ((1, 0, 2), (3, 4, 2), (5, 4, 6), (7, 0, 6))
_REL_PATH = re.compile(r"^(?!/)(?![A-Za-z][A-Za-z0-9+.-]*:)[^\\]+$")
_KNOWN_TOP_LEVEL = frozenset({
    "schema", "id", "tile_size", "world", "terrain", "tilesets", "layers", "props", "objects", "collision",
    "material_map", "nav", "portals", "spawns", "anchors", "interactions", "camera", "stage", "atmosphere",
    "lights", "qa", "art_source", "placeholder",
})


class BundleError(ValueError):
    """A bundle (or a file it needs) cannot be read at all."""


# --------------------------------------------------------------------------- JSON and the schema evaluator

def _local_read_json(path: str | os.PathLike) -> Any:
    """Read strict JSON: UTF-8 (a BOM is tolerated), no NaN or Infinity, no duplicate keys."""
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def no_constants(name: str) -> Any:
        raise ValueError(f"{name} is not valid JSON")

    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=no_constants)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise BundleError(f"cannot read JSON {Path(path).name}: {error}") from error


_ASSERTIONS = frozenset({
    "$ref", "type", "const", "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "items", "prefixItems", "contains",
    "required", "properties", "additionalProperties", "propertyNames", "minProperties", "maxProperties",
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
})
_ANNOTATIONS = frozenset({
    "$schema", "$id", "$comment", "$defs", "title", "description", "default", "examples", "format",
    "deprecated", "readOnly", "writeOnly",
})
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_is_type(value, item) for item in expected)
    checks = {
        "null": lambda v: v is None,
        "boolean": lambda v: isinstance(v, bool),
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "number": _is_number,
        "integer": lambda v: _is_number(v) and (isinstance(v, int) or float(v).is_integer()),
    }
    if expected not in checks:
        raise ValueError(f"unknown JSON Schema type {expected!r}")
    return checks[expected](value)


def _json_equal(a: Any, b: Any) -> bool:
    """JSON equality: 1 == 1.0, but booleans never equal numbers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[key], b[key]) for key in a)
    return type(a) is type(b) and a == b


def _brief(value: Any, limit: int = 60) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def json_path(base: str, key: str | int) -> str:
    """Extend a JSONPath like jsonschema's json_path: $.a.b[0] or $['odd key']."""
    if isinstance(key, int):
        return f"{base}[{key}]"
    return f"{base}.{key}" if _IDENTIFIER.match(key) else f"{base}[{json.dumps(key, ensure_ascii=False)}]"


class _LocalSchemaSet:
    """The JSON Schemas of one folder with a small Draft 2020-12 evaluator (standard library only).

    It implements exactly the assertion keywords the vendored schemas use (the test suite
    compares its verdicts with the jsonschema package); any other assertion keyword raises
    ValueError rather than being skipped. "format" is an annotation, as in jsonschema's
    default validator. Error lines read "$.json.path: message".
    """

    def __init__(self, directory: str | os.PathLike) -> None:
        self.directory = Path(directory)
        self._documents: dict[str, Any] = {}
        self._patterns: dict[str, re.Pattern[str]] = {}

    def document(self, name: str) -> Any:
        if name not in self._documents:
            path = self.directory / name
            try:
                self._documents[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError(f"cannot load schema {path}: {error}") from error
        return self._documents[name]

    def errors(self, instance: Any, ref: str) -> list[str]:
        """Return every violation of ``ref`` (for example ``map.schema.json#/$defs/map_bundle_v2``)."""
        name, schema = self._resolve(ref, "")
        out: list[str] = []
        self._check(instance, schema, name, "$", out)
        return out

    def _resolve(self, ref: str, current: str) -> tuple[str, Any]:
        name, _, pointer = ref.partition("#")
        name = name.rsplit("/", 1)[-1] if name else current
        node = self.document(name)
        for part in [p for p in pointer.split("/") if p]:
            part = part.replace("~1", "/").replace("~0", "~")
            node = node[int(part)] if isinstance(node, list) else node[part]
        return name, node

    def _sub_errors(self, value: Any, schema: Any, doc: str, path: str) -> list[str]:
        out: list[str] = []
        self._check(value, schema, doc, path, out)
        return out

    def _check(self, value: Any, schema: Any, doc: str, path: str, out: list[str]) -> None:
        if schema is True:
            return
        if schema is False:
            out.append(f"{path}: no value is allowed here")
            return
        unknown = set(schema) - _ASSERTIONS - _ANNOTATIONS
        if unknown:
            raise ValueError(f"unsupported JSON Schema keyword(s) {sorted(unknown)} in {doc}")
        if "$ref" in schema:
            target_doc, target = self._resolve(schema["$ref"], doc)
            self._check(value, target, target_doc, path, out)
        if "type" in schema and not _is_type(value, schema["type"]):
            out.append(f"{path}: {_brief(value)} is not of type {_brief(schema['type'])}")
            return
        if "const" in schema and not _json_equal(value, schema["const"]):
            out.append(f"{path}: {_brief(schema['const'])} was expected, got {_brief(value)}")
        if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
            out.append(f"{path}: {_brief(value)} is not one of {_brief(schema['enum'])}")
        if _is_number(value):
            self._check_number(value, schema, path, out)
        elif isinstance(value, str):
            self._check_string(value, schema, path, out)
        elif isinstance(value, list):
            self._check_array(value, schema, doc, path, out)
        elif isinstance(value, dict):
            self._check_object(value, schema, doc, path, out)
        for sub in schema.get("allOf", ()):
            self._check(value, sub, doc, path, out)
        for keyword in ("anyOf", "oneOf"):
            if keyword not in schema:
                continue
            results = [self._sub_errors(value, sub, doc, path) for sub in schema[keyword]]
            passing = sum(1 for result in results if not result)
            if passing == 0:
                closest = min(results, key=len)
                out.append(f"{path}: matches none of the allowed forms ({keyword}); closest: {closest[0]}")
            elif keyword == "oneOf" and passing > 1:
                out.append(f"{path}: matches {passing} of the mutually exclusive forms (oneOf)")
        if "not" in schema and not self._sub_errors(value, schema["not"], doc, path):
            out.append(f"{path}: matches a form that is not allowed (not)")
        if "if" in schema:
            branch = "then" if not self._sub_errors(value, schema["if"], doc, path) else "else"
            if branch in schema:
                self._check(value, schema[branch], doc, path, out)

    @staticmethod
    def _check_number(value: float, schema: dict, path: str, out: list[str]) -> None:
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: {value} is less than the minimum of {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: {value} is greater than the maximum of {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append(f"{path}: {value} is less than or equal to the minimum of {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            out.append(f"{path}: {value} is greater than or equal to the maximum of {schema['exclusiveMaximum']}")

    def _check_string(self, value: str, schema: dict, path: str, out: list[str]) -> None:
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: {_brief(value)} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: {_brief(value)} is too long")
        if "pattern" in schema:
            pattern = self._patterns.setdefault(schema["pattern"], re.compile(schema["pattern"]))
            if not pattern.search(value):
                out.append(f"{path}: {_brief(value)} does not match {schema['pattern']!r}")

    def _check_array(self, value: list, schema: dict, doc: str, path: str, out: list[str]) -> None:
        prefix = schema.get("prefixItems", [])
        for index, (item, sub) in enumerate(zip(value, prefix)):
            self._check(item, sub, doc, json_path(path, index), out)
        if "items" in schema:
            for index in range(len(prefix), len(value)):
                self._check(value[index], schema["items"], doc, json_path(path, index), out)
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: needs at least {schema['minItems']} item(s), has {len(value)}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: allows at most {schema['maxItems']} item(s), has {len(value)}")
        if "contains" in schema and not any(not self._sub_errors(item, schema["contains"], doc, path)
                                            for item in value):
            out.append(f"{path}: no item matches the required form (contains)")

    def _check_object(self, value: dict, schema: dict, doc: str, path: str, out: list[str]) -> None:
        for key in schema.get("required", ()):
            if key not in value:
                out.append(f"{path}: {key!r} is a required property")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                self._check(value[key], sub, doc, json_path(path, key), out)
        if "additionalProperties" in schema:
            for key in value:
                if key not in properties:
                    self._check(value[key], schema["additionalProperties"], doc, json_path(path, key), out)
        if "propertyNames" in schema:
            for key in value:
                self._check(key, schema["propertyNames"], doc, f"{json_path(path, key)} (name)", out)
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            out.append(f"{path}: needs at least {schema['minProperties']} propert(ies)")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            out.append(f"{path}: allows at most {schema['maxProperties']} propert(ies)")


SCHEMAS = _LocalSchemaSet(SCHEMA_DIR)


# --------------------------------------------------------------------------- data model

@dataclass
class Problem:
    severity: str  # "error" or "warning"
    path: str
    message: str
    code: str = ""  # "schema", "file", "sha256" or "" for cross-field rules

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "path": self.path, "message": self.message, "code": self.code or "rule"}


@dataclass
class Tileset:
    """A tileset_v1 manifest (or a v1 inline tileset) with its atlas image."""
    id: str
    doc: dict
    image: Path
    image_size: tuple[int, int]
    tile_w: int
    tile_h: int
    columns: int
    rows: int
    kind: str
    materials: list[str]
    tiles: dict[int, dict]
    manifest: Path | None = None
    _pixels: np.ndarray | None = field(default=None, repr=False)

    @property
    def tile_count(self) -> int:
        return self.columns * self.rows

    def pixels(self) -> np.ndarray:
        if self._pixels is None:
            self._pixels = np.asarray(forge_core.load_rgba(self.image)[0])
        return self._pixels

    def tile_stack(self) -> np.ndarray:
        """(tile_count + 1, tile_h, tile_w, 4): every atlas cell in index order plus a transparent tile last."""
        atlas = self.pixels()[: self.rows * self.tile_h, : self.columns * self.tile_w]
        stack = atlas.reshape(self.rows, self.tile_h, self.columns, self.tile_w, 4).transpose(0, 2, 1, 3, 4)
        stack = stack.reshape(self.tile_count, self.tile_h, self.tile_w, 4)
        return np.concatenate([stack, np.zeros((1, self.tile_h, self.tile_w, 4), np.uint8)])


@dataclass
class Layer:
    index: int
    name: str
    kind: str  # tiles, image or objects
    grid: np.ndarray | None = None  # tiles: (rows, cols) tileset-local indices, EMPTY_TILE when empty
    tileset: str | None = None
    image: Path | None = None
    image_size: tuple[int, int] | None = None
    offset: tuple[float, float] = (0.0, 0.0)


@dataclass
class Prop:
    id: str
    image: Path | None
    size: tuple[int, int] | None
    anchor_px: tuple[float, float] | None
    footprint: dict | None
    solid: bool | None
    occlusion_class: str | None
    occupant_policy: str | None
    sha256: str | None
    _pixels: Image.Image | None = field(default=None, repr=False)

    def rgba(self) -> Image.Image:
        if self._pixels is None:
            self._pixels = forge_core.load_rgba(self.image)[0]
        return self._pixels


@dataclass
class MapObject:
    """A placed prop: (x, y) is where the prop's anchor_px lands, in world pixels."""
    id: str
    prop: str
    x: float
    y: float
    scale: float
    anchor_px: tuple[float, float]
    footprint: dict | None
    solid: bool
    sort_y: float
    layer: str | None
    occlusion: str | None
    occupant_policy: str | None


@dataclass
class Portal:
    id: str
    rect: tuple[float, float, float, float] | None
    circle: tuple[float, float, float] | None
    to_map: str
    to_target: str | None
    activation: str
    travel: tuple[float, float] | None
    radius: float
    entrances: dict[str, tuple[float, float]]
    entrance_refs: dict[str, str | None]
    latch: bool
    requires_movement: bool
    reciprocal: bool

    def bounds(self) -> tuple[float, float, float, float]:
        if self.rect is not None:
            x, y, w, h = self.rect
            return x, y, x + w, y + h
        cx, cy, r = self.circle
        return cx - r, cy - r, cx + r, cy + r

    def distance(self, x: Any, y: Any) -> Any:
        """Distance from points to the closed trigger area (0 inside)."""
        if self.rect is not None:
            x0, y0, x1, y1 = self.bounds()
            dx = np.maximum(np.maximum(x0 - x, 0.0), x - x1)
            dy = np.maximum(np.maximum(y0 - y, 0.0), y - y1)
            return np.sqrt(dx * dx + dy * dy)
        cx, cy, r = self.circle
        return np.maximum(np.sqrt((x - cx) * (x - cx) + (y - cy) * (y - cy)) - r, 0.0)

    def closest_point(self, x: float, y: float) -> tuple[float, float]:
        if self.rect is not None:
            x0, y0, x1, y1 = self.bounds()
            return min(max(x, x0), x1), min(max(y, y0), y1)
        cx, cy, r = self.circle
        d = math.hypot(x - cx, y - cy)
        if d <= r:
            return x, y
        return cx + (x - cx) * r / d, cy + (y - cy) * r / d


@dataclass
class MaterialMap:
    image: Path
    scale: int
    index: np.ndarray  # (h, w) material index per pixel, -1 for none
    names: list[str]
    classes: list[str]
    walkable: list[bool]

    def class_codes(self) -> np.ndarray:
        """(h, w) uint8: FREE, BLOCK or ONE_WAY per material pixel (plan Appendix C)."""
        codes = np.zeros(len(self.names) + 1, np.uint8)  # the extra last entry is "no material"
        for i, (klass, walkable) in enumerate(zip(self.classes, self.walkable)):
            if klass == "solid" or (klass in ("liquid", "hazard") and not walkable):
                codes[i] = BLOCK
            elif klass == "one_way":
                codes[i] = ONE_WAY
        return codes[self.index]


@dataclass
class Collision:
    actor_radius: float
    y_squash: float
    regions: list[tuple[np.ndarray, list[np.ndarray]]]
    solids: list[dict]  # collision.solids and collision.rects, normalised, each with a "source"


@dataclass
class Bundle:
    path: Path
    raw: Any
    doc: dict = field(default_factory=dict)  # the v2-shaped document (v1 upgraded in memory)
    version: int = 0
    id: str = ""
    width: float = 0.0
    height: float = 0.0
    tile_w: int | None = None
    tile_h: int | None = None
    tilesets: dict[str, Tileset] = field(default_factory=dict)
    layers: list[Layer] = field(default_factory=list)
    props: dict[str, Prop] = field(default_factory=dict)
    objects: list[MapObject] = field(default_factory=list)
    collision: Collision | None = None
    material: MaterialMap | None = None
    portals: list[Portal] = field(default_factory=list)
    spawns: dict[str, tuple[float, float]] = field(default_factory=dict)
    anchors: dict[str, dict] = field(default_factory=dict)
    interactions: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    readable: bool = False

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == "error"]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == "warning"]

    def point_of(self, name: str) -> tuple[float, float] | None:
        """Position of a spawn id, or else of an anchor name."""
        if name in self.spawns:
            return self.spawns[name]
        anchor = self.anchors.get(name)
        return anchor["point"] if anchor else None


# --------------------------------------------------------------------------- path fields

def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def path_fields(doc: Any) -> Iterator[tuple[dict, str, str, str | None]]:
    """Yield (container, key, JSON path, sha256 key or None) for every file path of a bundle document.

    A path's sha256 lives beside it in the same object under "sha256" (props: the image's).
    """
    if not isinstance(doc, dict):
        return
    terrain = doc.get("terrain")
    if isinstance(terrain, dict) and isinstance(terrain.get("vertex_grid"), str):
        yield terrain, "vertex_grid", "$.terrain.vertex_grid", "sha256"
    for i, entry in enumerate(_as_list(doc.get("tilesets"))):
        if isinstance(entry, dict):
            for key in ("manifest", "image"):
                if isinstance(entry.get(key), str):
                    yield entry, key, f"$.tilesets[{i}].{key}", "sha256"
                    break
    for i, layer in enumerate(_as_list(doc.get("layers"))):
        if isinstance(layer, dict):
            for key in ("data", "image"):
                if isinstance(layer.get(key), str):
                    yield layer, key, f"$.layers[{i}].{key}", "sha256"
                    break
    props = doc.get("props")
    if isinstance(props, dict):
        for name, entry in props.items():
            if isinstance(entry, dict):
                base = json_path("$.props", name)
                if isinstance(entry.get("image"), str):
                    yield entry, "image", f"{base}.image", "sha256"
                if isinstance(entry.get("pack"), str):
                    yield entry, "pack", f"{base}.pack", None
    for i, obj in enumerate(_as_list(doc.get("objects"))):
        occluder = obj.get("occluder") if isinstance(obj, dict) else None
        if isinstance(occluder, dict) and isinstance(occluder.get("source"), str):
            yield occluder, "source", f"$.objects[{i}].occluder.source", "sha256"
    for key, sub in (("material_map", "image"), ("nav", "grid")):
        block = doc.get(key)
        if isinstance(block, dict) and isinstance(block.get(sub), str):
            yield block, sub, f"$.{key}.{sub}", "sha256"
    for key in ("stage", "atmosphere", "lights"):
        if isinstance(doc.get(key), str):
            yield doc, key, f"$.{key}", None


# --------------------------------------------------------------------------- geometry helpers

def _local_round_half_up(value: float) -> int:
    """floor(value + 0.5): the forge rounding rule (never banker's rounding)."""
    return math.floor(value + 0.5)


def nav_cell(actor_radius: float) -> int:
    """Navigation grid cell (plan Appendix C): max(1, round(r / 2)) with half-up rounding."""
    return max(1, _local_round_half_up(actor_radius / 2))


def merge_rects(mask: Any) -> list[tuple[int, int, int, int]]:
    """Cover a boolean grid with disjoint axis-aligned rectangles (x, y, w, h) in cells.

    Greedy: rows are scanned top to bottom; each maximal run of uncovered True cells in a
    row becomes a rectangle that grows downward while the whole run stays True and
    uncovered. The rectangles are disjoint and their union is exactly the True cells.
    """
    blocked = np.asarray(mask, bool)
    if blocked.ndim != 2:
        raise ValueError("merge_rects needs a 2-D mask")
    rows = blocked.shape[0]
    used = np.zeros_like(blocked)
    rects: list[tuple[int, int, int, int]] = []
    for y in range(rows):
        free = blocked[y] & ~used[y]
        if not free.any():
            continue
        edges = np.flatnonzero(np.diff(np.concatenate(([False], free, [False])).astype(np.int8)))
        for x0, x1 in zip(edges[0::2], edges[1::2]):
            y1 = y + 1
            while y1 < rows and blocked[y1, x0:x1].all() and not used[y1, x0:x1].any():
                y1 += 1
            used[y:y1, x0:x1] = True
            rects.append((int(x0), int(y), int(x1 - x0), int(y1 - y)))
    return rects


def object_placement(obj: MapObject, image_size: tuple[int, int]) -> tuple[float, float, float, float]:
    """(left, bottom, width, height) of an object's drawn image in world pixels.

    This is the Tiled tile-object convention (x, y = bottom-left): the image is scaled by
    obj.scale and its anchor_px lands on (obj.x, obj.y).
    """
    s = obj.scale
    width, height = image_size[0] * s, image_size[1] * s
    left = obj.x - obj.anchor_px[0] * s
    bottom = obj.y - obj.anchor_px[1] * s + height
    return left, bottom, width, height


def raster_box(left: float, bottom: float, width: float, height: float) -> tuple[int, int, int, int]:
    """Whole-pixel (x0, y0, w, h) for a drawn image, rounding half up."""
    return (_local_round_half_up(left), _local_round_half_up(bottom - height), _local_round_half_up(width),
            _local_round_half_up(height))


def footprint_solid(obj: MapObject) -> dict | None:
    """The object's ground footprint as a world solid, scaled exactly once by obj.scale.

    Footprints are measured in prop-image pixels: width across, depth along the ground,
    offset [dx, dy] from anchor_px, rotate in degrees (clockwise, y down). A rotated rect
    becomes a polygon. Actor size is never added here (plan Appendix C).
    """
    fp = obj.footprint
    if not obj.solid or not fp or fp.get("shape") not in ("ellipse", "rect"):
        return None
    s = obj.scale
    offset = fp.get("offset") or [0, 0]
    cx, cy = obj.x + s * offset[0], obj.y + s * offset[1]
    width, depth = s * fp["width"], s * fp["depth"]
    rotate = float(fp.get("rotate", 0) or 0)
    source = f"object:{obj.id}"
    if fp["shape"] == "ellipse":
        return {"shape": "ellipse", "cx": cx, "cy": cy, "rx": width / 2, "ry": depth / 2, "rotate": rotate,
                "source": source}
    if rotate == 0:
        return {"shape": "rect", "x": cx - width / 2, "y": cy - depth / 2, "w": width, "h": depth, "source": source}
    theta = math.radians(rotate)
    cos, sin = math.cos(theta), math.sin(theta)
    corners = [(-width / 2, -depth / 2), (width / 2, -depth / 2), (width / 2, depth / 2), (-width / 2, depth / 2)]
    points = [[cx + u * cos - v * sin, cy + u * sin + v * cos] for u, v in corners]
    return {"shape": "polygon", "points": points, "source": source}


def tile_solids(bundle: Bundle) -> list[dict]:
    """World solids from the collision of every placed tile.

    Each tile's collision shapes are moved to the tile's position; a tile whose
    properties say walkable false and that has no shapes blocks its whole cell.
    Axis-aligned rects with whole-pixel corners are unioned per layer and re-merged
    (solids are closed sets, so this does not change which points are blocked).
    """
    solids: list[dict] = []
    for layer in bundle.layers:
        tileset = bundle.tilesets.get(layer.tileset or "")
        if layer.kind != "tiles" or layer.grid is None or tileset is None:
            continue
        tw, th = tileset.tile_w, tileset.tile_h
        tile_masks = np.zeros((tileset.tile_count + 1, th, tw), bool)
        loose: dict[int, list[dict]] = {}
        for index, tile in tileset.tiles.items():
            if not 0 <= index < tileset.tile_count:
                continue
            shapes = tile.get("collision") or []
            if not shapes and (tile.get("properties") or {}).get("walkable") is False:
                shapes = [{"shape": "rect", "x": 0, "y": 0, "w": tw, "h": th}]
            for shape in shapes:
                box = _integral_rect(shape)
                if box is not None:
                    x0, y0, x1, y1 = (min(max(v, 0), limit) for v, limit in zip(box, (tw, th, tw, th)))
                    tile_masks[index, y0:y1, x0:x1] = True
                    if box != (x0, y0, x1, y1):  # the part outside the cell stays an exact solid
                        loose.setdefault(index, []).append(shape)
                else:
                    loose.setdefault(index, []).append(shape)
        grid = np.where(layer.grid >= 0, layer.grid, tileset.tile_count)
        rows, cols = grid.shape
        mask = tile_masks[grid].transpose(0, 2, 1, 3).reshape(rows * th, cols * tw)
        for x, y, w, h in merge_rects(mask):
            solids.append({"shape": "rect", "x": x, "y": y, "w": w, "h": h, "source": f"tiles:{layer.name}"})
        for index, shapes in sorted(loose.items()):
            for row, col in zip(*np.nonzero(layer.grid == index)):
                for shape in shapes:
                    solids.append(_translate_solid(shape, col * tw, row * th, f"tiles:{layer.name}"))
    return solids


def _integral_rect(shape: dict) -> tuple[int, int, int, int] | None:
    if shape.get("shape") != "rect":
        return None
    values = [shape.get(key) for key in ("x", "y", "w", "h")]
    if not all(_is_number(v) and float(v).is_integer() for v in values) or values[2] <= 0 or values[3] <= 0:
        return None
    x, y, w, h = (int(v) for v in values)
    return x, y, x + w, y + h


def _translate_solid(shape: dict, dx: float, dy: float, source: str) -> dict:
    moved = {key: value for key, value in shape.items() if key != "id"}
    if shape["shape"] == "rect":
        moved.update(x=shape["x"] + dx, y=shape["y"] + dy)
    elif shape["shape"] == "ellipse":
        moved.update(cx=shape["cx"] + dx, cy=shape["cy"] + dy)
    else:
        moved["points"] = [[px + dx, py + dy] for px, py in shape["points"]]
    moved["source"] = source
    return moved


def world_solids(bundle: Bundle) -> list[dict]:
    """Every blocking shape of the map in world pixels: collision solids and rects, solid
    object footprints (scaled once) and tile collision. Each carries a "source"."""
    solids = list(bundle.collision.solids) if bundle.collision else []
    for obj in bundle.objects:
        solid = footprint_solid(obj)
        if solid is not None:
            solids.append(solid)
    solids.extend(tile_solids(bundle))
    return solids


# --------------------------------------------------------------------------- loading

def _point(value: Any) -> tuple[float, float] | None:
    """[x, y], {"x", "y"} or {"point": [x, y]} as a float pair; None when malformed."""
    if isinstance(value, dict):
        value = value.get("point", [value.get("x"), value.get("y")])
    if isinstance(value, list) and len(value) == 2 and all(_is_number(v) for v in value):
        return float(value[0]), float(value[1])
    return None


def _default_map_id(path: Path) -> str:
    stem = path.name[:-5] if path.name.lower().endswith(".json") else path.stem
    for suffix in (".map-bundle", ".map_bundle", "-map-bundle", "_map_bundle", ".bundle", "-bundle", "_bundle"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if stem.lower() in ("map-bundle", "map_bundle", "bundle", "map") and path.parent.name:
        return path.parent.name
    return stem


def upgrade_v1(raw: dict) -> tuple[dict, list[str]]:
    """Map a generate2dmap.map_bundle.v1 document onto the v2 field names, in memory.

    v1 never had a frozen schema; this reader accepts the roadmap 4.10 draft: inline
    tilesets {id, image, kind, tiles, columns?}, object footprints {type|shape, rx, ry}
    or {x, y, w, h} relative to the anchor, and no world, collision or props. The schema
    key stays v1, so the v2-only requirements (world, layers, collision) do not apply.
    """
    doc = copy.deepcopy(raw)
    notes: list[str] = []
    for obj in _as_list(doc.get("objects")):
        fp = obj.get("footprint") if isinstance(obj, dict) else None
        if not isinstance(fp, dict) or "shape" in fp and "width" in fp:
            continue
        kind = fp.get("shape", fp.get("type"))
        if kind == "ellipse" and _is_number(fp.get("rx")) and _is_number(fp.get("ry")):
            obj["footprint"] = {"shape": "ellipse", "width": 2 * fp["rx"], "depth": 2 * fp["ry"],
                                "offset": [fp.get("cx", 0), fp.get("cy", 0)]}
        elif kind in ("rect", None) and all(_is_number(fp.get(k)) for k in ("w", "h")):
            obj["footprint"] = {"shape": "rect", "width": fp["w"], "depth": fp["h"],
                                "offset": [fp.get("x", -fp["w"] / 2) + fp["w"] / 2,
                                           fp.get("y", -fp["h"] / 2) + fp["h"] / 2]}
        else:
            continue
        notes.append(f"object {obj.get('id')!r}: v1 footprint converted to width/depth")
    return doc, notes


class _Loader:
    def __init__(self, bundle: Bundle, check_hashes: bool, require_sha256: bool) -> None:
        self.b = bundle
        self.check_hashes = check_hashes
        self.require_sha256 = require_sha256
        self._hashes: dict[Path, str] = {}
        self._inline_tilesets: dict[str, tuple[dict, str]] = {}

    def error(self, path: str, message: str, code: str = "") -> None:
        self.b.problems.append(Problem("error", path, message, code))

    def warn(self, path: str, message: str, code: str = "") -> None:
        self.b.problems.append(Problem("warning", path, message, code))

    # -- files

    def file(self, json_field: str, rel: Any, declared: Any, base_dir: Path) -> Path | None:
        """Resolve a relative path, check that the file exists and that its sha256 matches."""
        if not isinstance(rel, str) or not _REL_PATH.match(rel):
            self.error(json_field, f"{_brief(rel)} is not a relative POSIX path", "file")
            return None
        path = (base_dir / rel).resolve()
        if not path.is_file():
            self.error(json_field, f"file not found: {rel}", "file")
            return None
        if self.check_hashes:
            digest = self._hashes.get(path)
            if digest is None:
                digest = self._hashes[path] = forge_core.sha256_file(path)
            if declared is not None and declared != digest:
                self.error(json_field, f"sha256 mismatch for {rel}: recorded {declared}, file has {digest}", "sha256")
            elif declared is None and self.require_sha256:
                self.error(json_field, f"no sha256 recorded for {rel}", "sha256")
            if not any(entry["file"] == path for entry in self.b.files):
                self.b.files.append({"field": json_field, "file": path, "sha256": digest})
        return path

    # -- top level

    def run(self) -> None:
        b, raw = self.b, self.b.raw
        if not isinstance(raw, dict):
            self.error("$", "a map bundle must be a JSON object")
            return
        schema = raw.get("schema")
        if schema not in (SCHEMA_V1, SCHEMA_V2):
            self.error("$.schema", f"{_brief(schema)} is not {SCHEMA_V2} (or {SCHEMA_V1})")
            return
        b.version = 2 if schema == SCHEMA_V2 else 1
        doc = raw
        if b.version == 1:
            doc, notes = upgrade_v1(raw)
            for note in notes:
                self.warn("$.objects", note)
            for i, entry in enumerate(_as_list(doc.get("tilesets"))):
                if isinstance(entry, dict) and "manifest" not in entry and isinstance(entry.get("id"), str):
                    self._inline_tilesets[entry["id"]] = (entry, f"$.tilesets[{i}]")
            doc = dict(doc, tilesets=[e for e in _as_list(doc.get("tilesets"))
                                      if not (isinstance(e, dict) and "manifest" not in e)])
        b.doc = doc
        for message in SCHEMAS.errors(doc, "map.schema.json#/$defs/map_bundle_v2"):
            path, _, text = message.partition(": ")
            self.error(path, text, "schema")
        if b.errors:
            return
        self.identity()
        self.world()
        self.tilesets()
        self.layers()
        if not b.width or not b.height:
            return
        self.terrain()
        self.props()
        self.objects()
        self.collision()
        self.material_map()
        self.spawns_anchors_interactions()
        self.portals()
        self.extras()
        b.readable = True

    def identity(self) -> None:
        b = self.b
        map_id = b.doc.get("id")
        if map_id is None:
            b.id = _default_map_id(b.path)
        elif isinstance(map_id, str) and map_id and ":" not in map_id:
            b.id = map_id
        else:
            self.error("$.id", "map id must be a non-empty string without ':' (portals use map:target)")
            b.id = _default_map_id(b.path)
        unknown = sorted(set(b.doc) - _KNOWN_TOP_LEVEL)
        if unknown:
            self.warn("$", f"unknown top-level field(s) ignored: {', '.join(unknown)}")
        size = b.doc.get("tile_size")
        if size is not None:
            b.tile_w, b.tile_h = (size, size) if isinstance(size, int) else (int(size[0]), int(size[1]))

    def world(self) -> None:
        b = self.b
        world = b.doc.get("world")
        if world is not None:
            b.width, b.height = float(world["width"]), float(world["height"])

    # -- tilesets and layers

    def tilesets(self) -> None:
        seen: set[str] = set()
        for i, entry in enumerate(self.b.doc.get("tilesets", [])):
            where = f"$.tilesets[{i}]"
            if entry["id"] in seen:
                self.error(f"{where}.id", f"duplicate tileset id {entry['id']!r}")
                continue
            seen.add(entry["id"])
            manifest = self.file(f"{where}.manifest", entry["manifest"], entry.get("sha256"), self.b.base_dir)
            if manifest is None:
                continue
            try:
                doc = _local_read_json(manifest)
            except BundleError as error:
                self.error(f"{where}.manifest", str(error))
                continue
            messages = SCHEMAS.errors(doc, "map.schema.json#/$defs/tileset_v1")
            for message in messages:
                self.error(f"{where}.manifest", f"{entry['manifest']}: {message}", "schema")
            if not messages:
                self.tileset_from_doc(entry["id"], doc, manifest.parent, f"{where} ({entry['manifest']})", manifest)
        for tileset_id, (entry, where) in self._inline_tilesets.items():
            if tileset_id in seen:
                self.error(f"{where}.id", f"duplicate tileset id {tileset_id!r}")
                continue
            seen.add(tileset_id)
            self.inline_tileset(tileset_id, entry, where)

    def inline_tileset(self, tileset_id: str, entry: dict, where: str) -> None:
        """A v1 inline tileset {id, image, kind?, tiles?, columns?, materials?}."""
        b = self.b
        if b.tile_w is None or not isinstance(entry.get("image"), str):
            self.error(where, "a v1 inline tileset needs an image and the bundle's tile_size")
            return
        image = self.file(f"{where}.image", entry["image"], entry.get("sha256"), b.base_dir)
        if image is None:
            return
        with Image.open(image) as handle:
            width = handle.size[0]
        columns = entry.get("columns", max(1, width // b.tile_w))
        doc = {"schema": TILESET_SCHEMA, "image": entry["image"], "tile_size": [b.tile_w, b.tile_h],
               "columns": columns, "kind": entry.get("kind", "flat"),
               "materials": entry.get("materials") or ["default"],
               "tiles": entry.get("tiles") or [{"index": 0}], "seamless_verified": False}
        if entry.get("sha256") is not None:
            doc["sha256"] = entry["sha256"]
        messages = SCHEMAS.errors(doc, "map.schema.json#/$defs/tileset_v1")
        for message in messages:
            self.error(where, f"v1 inline tileset: {message}", "schema")
        if not messages:
            self.tileset_from_doc(tileset_id, doc, b.base_dir, where, None)

    def tileset_from_doc(self, tileset_id: str, doc: dict, base_dir: Path, where: str, manifest: Path | None) -> None:
        image = self.file(f"{where}.image", doc["image"], doc.get("sha256"), base_dir)
        if image is None:
            return
        try:
            with Image.open(image) as handle:
                size = handle.size
        except OSError as error:
            self.error(f"{where}.image", f"cannot open {doc['image']}: {error}")
            return
        tile = doc["tile_size"]
        tw, th = (tile, tile) if isinstance(tile, int) else (int(tile[0]), int(tile[1]))
        columns = int(doc["columns"])
        if size[0] != columns * tw or size[1] % th or size[1] == 0:
            self.error(f"{where}.image", f"atlas {size[0]}x{size[1]} is not {columns} columns of {tw}x{th} tiles")
            return
        if self.b.tile_w is not None and (tw, th) != (self.b.tile_w, self.b.tile_h):
            self.error(f"{where}.tile_size",
                       f"tileset tiles are {tw}x{th}, the bundle's are {self.b.tile_w}x{self.b.tile_h}")
        rows = size[1] // th
        materials = list(doc["materials"])
        tiles: dict[int, dict] = {}
        keys: dict[tuple, int] = {}
        for k, entry in enumerate(doc["tiles"]):
            at = f"{where}.tiles[{k}]"
            index = int(entry["index"])
            if index in tiles:
                self.error(f"{at}.index", f"duplicate tile index {index}")
                continue
            if index >= columns * rows:
                self.error(f"{at}.index", f"tile {index} is outside the {columns}x{rows} atlas")
                continue
            tiles[index] = entry
            if "wang" in entry and any(value >= len(materials) for value in entry["wang"]):
                self.error(f"{at}.wang", f"wang {entry['wang']} names a material beyond {materials}")
            if "blob_mask" in entry:
                mask = int(entry["blob_mask"])
                for diagonal, first, second in _BLOB_DIAGONALS:
                    if mask >> diagonal & 1 and not (mask >> first & 1 and mask >> second & 1):
                        self.error(f"{at}.blob_mask", f"blob_mask {mask} sets {BLOB_BITS[diagonal]} without "
                                                      f"{BLOB_BITS[first]} and {BLOB_BITS[second]} (not canonical)")
                        break
            key = (tuple(entry.get("wang", ())), entry.get("blob_mask"), entry.get("variant", 0))
            if (entry.get("wang") is not None or entry.get("blob_mask") is not None) and key in keys:
                self.warn(f"{at}", f"tile {index} repeats the topology and variant of tile {keys[key]}")
            keys.setdefault(key, index)
            for s, shape in enumerate(entry.get("collision") or []):
                box = _solid_bounds(shape)
                if box and (box[0] < 0 or box[1] < 0 or box[2] > tw or box[3] > th):
                    self.warn(f"{at}.collision[{s}]", "collision shape extends outside the tile")
        self.b.tilesets[tileset_id] = Tileset(
            id=tileset_id, doc=doc, image=image, image_size=size, tile_w=tw, tile_h=th, columns=columns,
            rows=rows, kind=doc["kind"], materials=materials, tiles=tiles, manifest=manifest)

    def layers(self) -> None:
        b = self.b
        names: set[str] = set()
        for i, entry in enumerate(b.doc.get("layers", [])):
            where = f"$.layers[{i}]"
            if entry["name"] in names:
                self.error(f"{where}.name", f"duplicate layer name {entry['name']!r}")
            names.add(entry["name"])
            layer = Layer(index=i, name=entry["name"], kind=entry["kind"])
            if layer.kind == "tiles":
                layer.grid = self.tile_grid(where, entry)
                layer.tileset = self.layer_tileset(where, entry)
                if layer.grid is not None and layer.tileset in b.tilesets:
                    self.check_tile_values(where, layer, b.tilesets[layer.tileset])
            elif layer.kind == "image":
                layer.image = self.file(f"{where}.image", entry["image"], entry.get("sha256"), b.base_dir)
                offset = _point(entry.get("offset", [0, 0]))
                if offset is None:
                    self.error(f"{where}.offset", "offset must be [x, y]")
                else:
                    layer.offset = offset
                if layer.image is not None:
                    try:
                        with Image.open(layer.image) as handle:
                            layer.image_size = handle.size
                    except OSError as error:
                        self.error(f"{where}.image", f"cannot open {entry['image']}: {error}")
            b.layers.append(layer)
        self.layer_extent()

    def tile_grid(self, where: str, entry: dict) -> np.ndarray | None:
        data = entry["data"]
        rows: Any = data
        if isinstance(data, str):
            path = self.file(f"{where}.data", data, entry.get("sha256"), self.b.base_dir)
            if path is None:
                return None
            try:
                if path.suffix.lower() == ".csv":
                    lines = path.read_text(encoding="utf-8-sig").splitlines()
                    rows = [[int(cell) for cell in line.split(",")] for line in lines if line.strip()]
                else:
                    rows = _local_read_json(path)
                    rows = rows.get("data") if isinstance(rows, dict) else rows
            except (ValueError, BundleError) as error:
                self.error(f"{where}.data", f"cannot read tile data {data}: {error}")
                return None
        if (not isinstance(rows, list) or not rows or not all(isinstance(row, list) and row for row in rows)
                or len({len(row) for row in rows}) != 1):
            self.error(f"{where}.data", "tile data must be a non-empty list of equally long rows")
            return None
        if not all(isinstance(v, int) and not isinstance(v, bool) or v is None for row in rows for v in row):
            self.error(f"{where}.data", "tile data holds whole tile indices (-1 or null for empty)")
            return None
        grid = np.array([[EMPTY_TILE if v is None else v for v in row] for row in rows], np.int64)
        if (grid < EMPTY_TILE).any():
            self.error(f"{where}.data", f"tile indices below {EMPTY_TILE}")
            return None
        return grid

    def layer_tileset(self, where: str, entry: dict) -> str | None:
        declared = {e["id"] for e in self.b.doc.get("tilesets", [])} | set(self._inline_tilesets)
        name = entry.get("tileset")
        if name is None:
            if len(declared) == 1:
                return next(iter(declared))
            (self.warn if self.b.version == 1 else self.error)(
                f"{where}.tileset", "a tiles layer must name its tileset when the bundle has none or several")
            return None
        if name not in declared:
            self.error(f"{where}.tileset", f"unknown tileset {name!r}")
        return name

    def check_tile_values(self, where: str, layer: Layer, tileset: Tileset) -> None:
        used = np.unique(layer.grid[layer.grid >= 0])
        outside = used[used >= tileset.tile_count]
        if outside.size:
            self.error(f"{where}.data", f"tile indices {outside[:8].tolist()} exceed the {tileset.tile_count} tiles "
                                        f"of tileset {tileset.id!r}")
        if tileset.kind in ("wang_corner", "blob47"):
            missing = [int(v) for v in used if v < tileset.tile_count and int(v) not in tileset.tiles]
            if missing:
                self.warn(f"{where}.data", f"tiles {missing[:8]} of {tileset.kind} tileset {tileset.id!r} have no "
                                           "tiles[] entry (no topology or collision)")

    def layer_extent(self) -> None:
        """Tile layers must cover the world exactly; v1 infers the world from them."""
        b = self.b
        grids = [layer for layer in b.layers if layer.kind == "tiles" and layer.grid is not None]
        if grids and b.tile_w is None:
            self.error("$.tile_size", "tiles layers need tile_size")
            return
        for layer in grids:
            rows, cols = layer.grid.shape
            extent = (cols * (b.tile_w or 0), rows * (b.tile_h or 0))
            if not b.width and b.tile_w:
                b.width, b.height = float(extent[0]), float(extent[1])
                self.warn("$.world", f"world inferred from tiles layer {layer.name!r}: {extent[0]}x{extent[1]} px")
            if extent != (b.width, b.height):
                self.error(f"$.layers[{layer.index}].data", f"{cols}x{rows} tiles of {b.tile_w}x{b.tile_h} px cover "
                                                           f"{extent[0]}x{extent[1]} px, the world is "
                                                           f"{b.width:g}x{b.height:g}")
        if not b.width:
            images = [layer for layer in b.layers if layer.image_size]
            if images:
                b.width, b.height = (float(v) for v in images[0].image_size)
                self.warn("$.world", f"world inferred from image layer {images[0].name!r}")
            else:
                self.error("$.world", "no world size: give world {width, height, unit: px}")
        for layer in b.layers:
            if layer.image_size and (layer.offset[0] > 0 or layer.offset[1] > 0
                                     or layer.offset[0] + layer.image_size[0] < b.width
                                     or layer.offset[1] + layer.image_size[1] < b.height):
                self.warn(f"$.layers[{layer.index}].image", f"image layer {layer.name!r} does not cover the world")

    def terrain(self) -> None:
        """terrain.vertex_grid: a JSON grid of material indices, one more row and column than the tiles."""
        b = self.b
        block = b.doc.get("terrain")
        if block is None:
            return
        path = self.file("$.terrain.vertex_grid", block["vertex_grid"], block.get("sha256"), b.base_dir)
        if path is None:
            return
        try:
            grid = _local_read_json(path)
            grid = grid.get("data") if isinstance(grid, dict) else grid
            array = np.asarray(grid)
        except (BundleError, ValueError) as error:
            self.error("$.terrain.vertex_grid", str(error))
            return
        if array.ndim != 2 or array.dtype.kind not in "iu":
            self.error("$.terrain.vertex_grid", "the vertex grid must be a list of equally long rows of integers")
            return
        if array.min() < 0 or array.max() >= len(block["materials"]):
            self.error("$.terrain.vertex_grid",
                       f"vertex values must index terrain.materials (0..{len(block['materials']) - 1})")
        if b.tile_w:
            expected = (_local_round_half_up(b.height / b.tile_h) + 1, _local_round_half_up(b.width / b.tile_w) + 1)
            if array.shape != expected:
                self.error("$.terrain.vertex_grid", f"vertex grid is {array.shape[1]}x{array.shape[0]}, the map needs "
                                                    f"{expected[1]}x{expected[0]} (one more than the tiles each way)")

    # -- props and objects

    def props(self) -> None:
        b = self.b
        registry = b.doc.get("props")
        if registry is None:
            return
        if not isinstance(registry, dict):
            self.error("$.props", "props must be an object of prop id -> {image | pack + label, ...}")
            return
        for name, entry in registry.items():
            where = json_path("$.props", name)
            if not isinstance(entry, dict):
                self.error(where, "a prop entry must be an object")
                continue
            item: dict = {}
            base = b.base_dir
            if "pack" in entry:
                item, base = self.pack_item(where, entry)
                if item is None:
                    continue
            merged = {**item, **{k: v for k, v in entry.items() if k not in ("pack", "label")}}
            if "image" not in merged:
                self.error(where, "a prop needs an image (or pack + label)")
                continue
            image_base = b.base_dir if "image" in entry else base
            image = self.file(f"{where}.image", merged["image"], merged.get("sha256"), image_base)
            size = None
            if image is not None:
                try:
                    with Image.open(image) as handle:
                        size = handle.size
                except OSError as error:
                    self.error(f"{where}.image", f"cannot open {merged['image']}: {error}")
            prop = Prop(id=name, image=image, size=size, anchor_px=_point(merged.get("anchor_px")),
                        footprint=merged.get("footprint"), solid=merged.get("solid"),
                        occlusion_class=merged.get("occlusion_class"), occupant_policy=merged.get("occupant_policy"),
                        sha256=merged.get("sha256"))
            self.check_prop(where, prop, merged)
            b.props[name] = prop

    def pack_item(self, where: str, entry: dict) -> tuple[dict | None, Path]:
        """The accepted prop_pack_v2 (or v1) item named by entry["label"], with its manifest folder."""
        pack = self.file(f"{where}.pack", entry["pack"], None, self.b.base_dir)
        if pack is None:
            return None, self.b.base_dir
        try:
            manifest = _local_read_json(pack)
        except BundleError as error:
            self.error(f"{where}.pack", str(error))
            return None, self.b.base_dir
        for message in SCHEMAS.errors(manifest, "map.schema.json#/$defs/prop_pack_v2"):
            self.error(f"{where}.pack", f"{entry['pack']}: {message}", "schema")
            return None, self.b.base_dir
        label = entry.get("label")
        for item in manifest.get("accepted", []):
            if item.get("label") == label:
                return dict(item), pack.parent
        self.error(f"{where}.label", f"prop pack {entry['pack']} has no accepted item {label!r}")
        return None, self.b.base_dir

    def check_prop(self, where: str, prop: Prop, merged: dict) -> None:
        if merged.get("anchor_px") is not None and prop.anchor_px is None:
            self.error(f"{where}.anchor_px", "anchor_px must be [x, y]")
        if prop.footprint is not None:
            for message in SCHEMAS.errors(prop.footprint, "map.schema.json#/$defs/footprint"):
                path, _, text = message.partition(": ")
                self.error(where + ".footprint" + path[1:], text, "schema")
        if prop.solid is not None and not isinstance(prop.solid, bool):
            self.error(f"{where}.solid", "solid must be true or false")
        if prop.occlusion_class is not None and prop.occlusion_class not in OCCLUSION_CLASSES:
            self.warn(f"{where}.occlusion_class", f"{prop.occlusion_class!r} is not one of {OCCLUSION_CLASSES}")
        if prop.occupant_policy is not None and prop.occupant_policy not in OCCUPANT_POLICIES:
            self.warn(f"{where}.occupant_policy", f"{prop.occupant_policy!r} is not one of {OCCUPANT_POLICIES}")

    def objects(self) -> None:
        b = self.b
        object_layers = [layer.name for layer in b.layers if layer.kind == "objects"]
        seen: set[str] = set()
        entries = b.doc.get("objects", [])
        if entries and not b.props:
            self.warn("$.props", "no props registry: objects cannot be drawn or exported with images")
        if entries and not object_layers:
            self.warn("$.layers", "objects are not drawn: the bundle has no objects layer")
        for i, entry in enumerate(entries):
            where = f"$.objects[{i}]"
            if entry["id"] in seen:
                self.error(f"{where}.id", f"duplicate object id {entry['id']!r}")
            seen.add(entry["id"])
            prop = b.props.get(entry["prop"])
            if b.props and prop is None:
                self.error(f"{where}.prop", f"unknown prop {entry['prop']!r}")
            footprint = entry.get("footprint", prop.footprint if prop else None)
            solid = entry.get("solid", prop.solid if prop and prop.solid is not None else None)
            if solid is None:
                solid = bool(footprint) and footprint.get("shape") in ("ellipse", "rect")
            layer = entry.get("layer", object_layers[0] if object_layers else None)
            if layer is not None and layer not in object_layers:
                self.error(f"{where}.layer", f"{layer!r} is not an objects layer")
            occlusion = entry.get("occlusion", entry.get("occlusion_class", prop.occlusion_class if prop else None))
            if occlusion is not None and occlusion not in OCCLUSION_CLASSES:
                self.warn(f"{where}.occlusion", f"{occlusion!r} is not one of {OCCLUSION_CLASSES}")
            policy = entry.get("occupant_policy", prop.occupant_policy if prop else None)
            if policy is not None and policy not in OCCUPANT_POLICIES:
                self.warn(f"{where}.occupant_policy", f"{policy!r} is not one of {OCCUPANT_POLICIES}")
            occluder = entry.get("occluder")
            if occluder is not None:
                self.file(f"{where}.occluder.source", occluder["source"], occluder.get("sha256"), b.base_dir)
            obj = MapObject(id=entry["id"], prop=entry["prop"], x=float(entry["x"]), y=float(entry["y"]),
                            scale=float(entry.get("scale", 1)), anchor_px=_point(entry["anchor_px"]),
                            footprint=footprint, solid=bool(solid), sort_y=float(entry.get("sortY", entry["y"])),
                            layer=layer, occlusion=occlusion, occupant_policy=policy)
            if prop is not None and prop.size is not None:
                ax, ay = obj.anchor_px
                if not (0 <= ax <= prop.size[0] and 0 <= ay <= prop.size[1]):
                    self.warn(f"{where}.anchor_px", f"anchor {list(obj.anchor_px)} lies outside the "
                                                    f"{prop.size[0]}x{prop.size[1]} prop image")
            if not (0 <= obj.x <= b.width and 0 <= obj.y <= b.height):
                self.warn(where, f"object {obj.id!r} is anchored outside the world")
            b.objects.append(obj)

    # -- collision and materials

    def collision(self) -> None:
        b = self.b
        block = b.doc.get("collision")
        if block is None:
            if b.version == 1:
                self.warn("$.collision", "v1 bundle without collision: map_nav needs collision.actorRadius")
            return
        regions = []
        for i, region in enumerate(block.get("walkRegions", [])):
            polygon = self.polygon(f"$.collision.walkRegions[{i}].polygon", region["polygon"])
            holes = [self.polygon(f"$.collision.walkRegions[{i}].holes[{k}]", hole)
                     for k, hole in enumerate(region.get("holes", []))]
            if polygon is None or any(hole is None for hole in holes):
                continue
            for k, hole in enumerate(holes):
                x0, y0 = polygon.min(axis=0)
                x1, y1 = polygon.max(axis=0)
                if (hole < (x0, y0)).any() or (hole > (x1, y1)).any():
                    self.warn(f"$.collision.walkRegions[{i}].holes[{k}]", "hole extends outside its region")
            regions.append((polygon, holes))
        solids = []
        for i, shape in enumerate(block.get("solids", [])):
            normalised = dict(shape, source=shape.get("id", f"collision.solids[{i}]"))
            if shape["shape"] == "polygon" and self.polygon(f"$.collision.solids[{i}].points", shape["points"]) is None:
                continue
            if _solid_bounds(normalised) is None:
                self.warn(f"$.collision.solids[{i}]", "zero-size solid blocks nothing")
                continue
            solids.append(normalised)
        for i, (x, y, w, h) in enumerate(block.get("rects", [])):
            if w <= 0 or h <= 0:
                self.warn(f"$.collision.rects[{i}]", "zero-size rect blocks nothing")
                continue
            solids.append({"shape": "rect", "x": x, "y": y, "w": w, "h": h, "source": f"collision.rects[{i}]"})
        radius = float(block["actorRadius"])
        if radius == 0:
            self.warn("$.collision.actorRadius", "actorRadius 0 treats the actor as a point")
        if "ySquash" not in block and b.doc.get("stage") is not None:
            self.warn("$.collision", "HD-2D plates usually flatten the actor footprint (ySquash about 0.58); "
                                     "ySquash defaults to 1.0, so set it explicitly")
        b.collision = Collision(actor_radius=radius, y_squash=float(block.get("ySquash", 1.0)),
                                regions=regions, solids=solids)
        for region, _ in regions:
            if (region[:, 0] < 0).any() or (region[:, 1] < 0).any() or (region[:, 0] > b.width).any() \
                    or (region[:, 1] > b.height).any():
                self.warn("$.collision.walkRegions", "a walk region extends outside the world")
                break

    def polygon(self, where: str, points: list) -> np.ndarray | None:
        array = np.asarray(points, np.float64)
        x, y = array[:, 0], array[:, 1]
        area = 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
        if not np.isfinite(array).all() or area == 0:
            self.error(where, "polygon has zero area")
            return None
        return array

    def material_map(self) -> None:
        b = self.b
        block = b.doc.get("material_map")
        if block is None:
            return
        path = self.file("$.material_map.image", block["image"], block.get("sha256"), b.base_dir)
        if path is None:
            return
        names = list(block["materials"])
        entries = [block["materials"][name] for name in names]
        try:
            with Image.open(path) as handle:
                mode, size = handle.mode, handle.size
                by_index = mode in ("P", "L") and all("index" in entry for entry in entries)
                raw = np.asarray(handle) if by_index else None
        except OSError as error:
            self.error("$.material_map.image", f"cannot open {block['image']}: {error}")
            return
        scale = b.width / size[0]
        if not scale.is_integer() or scale < 1 or scale * size[1] != b.height:
            self.error("$.material_map.image", f"{size[0]}x{size[1]} px does not divide the "
                                               f"{b.width:g}x{b.height:g} world into whole squares")
            return
        index = np.full((size[1], size[0]), -1, np.int16)
        if by_index:
            values = [entry["index"] for entry in entries]
            if len(set(values)) != len(values):
                self.error("$.material_map.materials", "material indices must be unique")
                return
            for i, value in enumerate(values):
                index[raw == value] = i
            unmatched = int((index < 0).sum())
        else:
            if not all("color" in entry for entry in entries):
                self.error("$.material_map.materials", "every material needs a color (or an index for a P/L image)")
                return
            colours = [_rgb(entry["color"]) for entry in entries]
            if len(set(colours)) != len(colours):
                self.error("$.material_map.materials", "material colors must be unique")
                return
            rgba = np.asarray(forge_core.load_rgba(path)[0])
            for i, colour in enumerate(colours):
                index[(rgba[..., 3] > 0) & np.all(rgba[..., :3] == colour, axis=-1)] = i
            unmatched = int(((index < 0) & (rgba[..., 3] > 0)).sum())
        if unmatched:
            first = np.argwhere(index < 0)[0] if by_index else np.argwhere((index < 0) & (rgba[..., 3] > 0))[0]
            self.error("$.material_map.image", f"{unmatched} pixel(s) match no material (first at x={first[1]}, "
                                               f"y={first[0]})")
            return
        for name, entry in zip(names, entries):
            if entry["class"] == "solid" and entry.get("walkable") is True:
                self.warn(json_path("$.material_map.materials", name), "solid material is never walkable")
        b.material = MaterialMap(image=path, scale=int(scale), index=index, names=names,
                                 classes=[entry["class"] for entry in entries],
                                 walkable=[bool(entry.get("walkable", False)) for entry in entries])

    # -- points of interest

    def spawns_anchors_interactions(self) -> None:
        b = self.b
        for i, spawn in enumerate(b.doc.get("spawns", [])):
            where = f"$.spawns[{i}]"
            if spawn["id"] in b.spawns:
                self.error(f"{where}.id", f"duplicate spawn id {spawn['id']!r}")
            b.spawns[spawn["id"]] = (float(spawn["x"]), float(spawn["y"]))
            self.inside_world(where, b.spawns[spawn["id"]])
        for name, anchor in b.doc.get("anchors", {}).items():
            where = json_path("$.anchors", name)
            slots = []
            for k, slot in enumerate(anchor.get("slots", [])):
                point = _point(slot)
                if point is None:
                    self.error(f"{where}.slots[{k}]", "a slot is [x, y], {x, y} or {point: [x, y]}")
                else:
                    slots.append(point)
            approach = anchor.get("approach")
            if approach is None:
                approaches = []
            elif _point(approach) is not None:
                approaches = [_point(approach)]
            else:
                approaches = [_point(p) for p in approach]
            if name in b.spawns:
                self.warn(where, f"anchor {name!r} shares its name with a spawn; portal targets resolve to the spawn")
            b.anchors[name] = {"point": _point(anchor["point"]), "facing": anchor.get("facing"),
                               "slots": slots, "approach": approaches}
        ids: set[str] = set()
        for i, entry in enumerate(b.doc.get("interactions", [])):
            where = f"$.interactions[{i}]"
            if entry["id"] in ids:
                self.error(f"{where}.id", f"duplicate interaction id {entry['id']!r}")
            ids.add(entry["id"])
            reach = entry.get("reach")
            b.interactions.append({"id": entry["id"], "x": float(entry["x"]), "y": float(entry["y"]),
                                   "reach": None if reach is None else float(reach)})
            self.inside_world(where, (float(entry["x"]), float(entry["y"])))

    def inside_world(self, where: str, point: tuple[float, float]) -> None:
        if not (0 <= point[0] <= self.b.width and 0 <= point[1] <= self.b.height):
            self.error(where, f"point {list(point)} lies outside the {self.b.width:g}x{self.b.height:g} world")

    def portals(self) -> None:
        b = self.b
        ids: set[str] = set()
        for i, entry in enumerate(b.doc.get("portals", [])):
            where = f"$.portals[{i}]"
            if entry["id"] in ids:
                self.error(f"{where}.id", f"duplicate portal id {entry['id']!r}")
            ids.add(entry["id"])
            to_map, _, to_target = entry["to"].partition(":")
            if not to_map:
                self.error(f"{where}.to", "to is <map id> or <map id>:<spawn or anchor>")
            if to_map == b.id and not to_target:
                self.error(f"{where}.to", "a portal to the same map must name its arrival spawn or anchor")
            if to_map == b.id and to_target and b.point_of(to_target) is None:
                self.error(f"{where}.to", f"no spawn or anchor {to_target!r} in this map")
            travel = entry.get("travelDirection")
            if travel is not None and travel[0] == 0 and travel[1] == 0:
                self.error(f"{where}.travelDirection", "travelDirection must not be [0, 0]")
            entrances: dict[str, tuple[float, float]] = {}
            refs: dict[str, str | None] = {}
            for source, value in entry.get("entranceByFrom", {}).items():
                at = json_path(f"{where}.entranceByFrom", source)
                point = b.point_of(value) if isinstance(value, str) else _point(value)
                if point is None:
                    self.error(at, f"{_brief(value)} is neither a spawn or anchor of this map nor a point [x, y]")
                    continue
                entrances[source] = point
                refs[source] = value if isinstance(value, str) else None
            for key in ("latch", "requiresMovement", "reciprocal"):
                if key in entry and not isinstance(entry[key], bool):
                    self.error(f"{where}.{key}", f"{key} must be true or false")
            portal = Portal(
                id=entry["id"], rect=tuple(float(v) for v in entry["rect"]) if "rect" in entry else None,
                circle=tuple(float(v) for v in entry["circle"]) if "circle" in entry else None,
                to_map=to_map, to_target=to_target or None, activation=entry.get("activation", "crossing"),
                travel=None if travel is None else (float(travel[0]), float(travel[1])),
                radius=float(entry.get("radius", 0)), entrances=entrances, entrance_refs=refs,
                latch=entry.get("latch", True) is not False,
                requires_movement=entry.get("requiresMovement", True) is not False,
                reciprocal=entry.get("reciprocal", True) is not False)
            if not portal.latch:
                self.warn(f"{where}.latch", "an unlatched exit can fire again on arrival (plan Appendix C latches it)")
            b.portals.append(portal)

    def extras(self) -> None:
        b = self.b
        nav = b.doc.get("nav")
        if nav is not None:
            if b.collision is not None and nav["cell"] != nav_cell(b.collision.actor_radius):
                self.warn("$.nav.cell", f"nav.cell {nav['cell']} differs from max(1, round(actorRadius/2)) = "
                                        f"{nav_cell(b.collision.actor_radius)}, which map_nav uses")
            if "grid" in nav:
                self.file("$.nav.grid", nav["grid"], nav.get("sha256"), b.base_dir)
        camera = b.doc.get("camera") or {}
        if "bounds" in camera:
            x, y, w, h = camera["bounds"]
            if x < 0 or y < 0 or x + w > b.width or y + h > b.height:
                self.warn("$.camera.bounds", "camera bounds extend outside the world")
        for key in ("stage", "atmosphere", "lights"):
            value = b.doc.get(key)
            if isinstance(value, str):
                path = self.file(f"$.{key}", value, None, b.base_dir)
                if path is None:
                    continue
                try:
                    document = _local_read_json(path)
                except BundleError as error:
                    self.error(f"$.{key}", str(error))
                    continue
                for message in SCHEMAS.errors(document, f"map.schema.json#/$defs/{key}_v1"):
                    self.error(f"$.{key}", f"{value}: {message}", "schema")


def _rgb(colour: Any) -> tuple[int, int, int]:
    if isinstance(colour, str):
        return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)
    return int(colour[0]), int(colour[1]), int(colour[2])


def _solid_bounds(shape: dict) -> tuple[float, float, float, float] | None:
    """Axis-aligned bounds of a solid; None when it has no area (it then blocks nothing)."""
    kind = shape.get("shape")
    if kind == "rect":
        if shape["w"] <= 0 or shape["h"] <= 0:
            return None
        return shape["x"], shape["y"], shape["x"] + shape["w"], shape["y"] + shape["h"]
    if kind == "ellipse":
        rx, ry = shape["rx"], shape["ry"]
        if rx <= 0 or ry <= 0:
            return None
        theta = math.radians(float(shape.get("rotate", 0) or 0))
        half_w = math.hypot(rx * math.cos(theta), ry * math.sin(theta))
        half_h = math.hypot(rx * math.sin(theta), ry * math.cos(theta))
        return shape["cx"] - half_w, shape["cy"] - half_h, shape["cx"] + half_w, shape["cy"] + half_h
    points = np.asarray(shape["points"], np.float64)
    return float(points[:, 0].min()), float(points[:, 1].min()), float(points[:, 0].max()), float(points[:, 1].max())


def bundle_from_document(raw: Any, path: str | os.PathLike, *, check_hashes: bool = True,
                         require_sha256: bool = False) -> Bundle:
    """Check a bundle document as if it were stored at ``path`` (its relative paths resolve
    from path's folder; its default map id comes from path's name)."""
    bundle = Bundle(path=Path(path).resolve(), raw=raw)
    _Loader(bundle, check_hashes, require_sha256).run()
    return bundle


def load_bundle(path: str | os.PathLike, *, check_hashes: bool = True, require_sha256: bool = False) -> Bundle:
    """Read and check a map bundle. Data problems are collected in bundle.problems; only an
    unreadable bundle file raises BundleError. bundle.readable is False when errors stopped
    the reader before the whole bundle was understood."""
    return bundle_from_document(_local_read_json(path), path, check_hashes=check_hashes, require_sha256=require_sha256)


# --------------------------------------------------------------------------- rendering

def canvas_size(bundle: Bundle) -> tuple[int, int]:
    return math.ceil(bundle.width), math.ceil(bundle.height)


def composite(canvas: Image.Image, image: Image.Image, x0: int, y0: int) -> None:
    """Alpha-composite ``image`` onto ``canvas`` with its top-left at (x0, y0), clipped to the canvas."""
    sx, sy = max(0, -x0), max(0, -y0)
    dx, dy = max(0, x0), max(0, y0)
    width = min(image.width - sx, canvas.width - dx)
    height = min(image.height - sy, canvas.height - dy)
    if width <= 0 or height <= 0:
        return
    region = image if (sx, sy, width, height) == (0, 0, image.width, image.height) else \
        image.crop((sx, sy, sx + width, sy + height))
    canvas.alpha_composite(region, dest=(dx, dy))


def draw_image(canvas: Image.Image, image: Image.Image, left: float, bottom: float, width: float,
               height: float) -> None:
    """Draw ``image`` with its bottom-left corner at (left, bottom), scaled with nearest
    neighbour to width x height; the box is rounded by raster_box()."""
    x0, y0, w, h = raster_box(left, bottom, width, height)
    if w <= 0 or h <= 0:
        return
    if (w, h) != image.size:
        image = image.resize((w, h), Image.Resampling.NEAREST)
    composite(canvas, image, x0, y0)


def tile_layer_rgba(bundle: Bundle, layer: Layer) -> np.ndarray:
    """A whole tiles layer as one straight-alpha RGBA array (empty cells transparent)."""
    tileset = bundle.tilesets[layer.tileset]
    stack = tileset.tile_stack()
    grid = np.where((layer.grid >= 0) & (layer.grid < tileset.tile_count), layer.grid, tileset.tile_count)
    rows, cols = grid.shape
    tiles = stack[grid]  # (rows, cols, th, tw, 4)
    return tiles.transpose(0, 2, 1, 3, 4).reshape(rows * tileset.tile_h, cols * tileset.tile_w, 4)


def draw_order(objects: list[MapObject]) -> list[MapObject]:
    """Ground-line draw order: sortY, then x, then id (B12's compose tie rule)."""
    return sorted(objects, key=lambda obj: (obj.sort_y, obj.x, obj.id))


def render_map(bundle: Bundle) -> np.ndarray:
    """Reference render of a bundle, world-sized straight-alpha RGBA (H, W, 4) uint8.

    Layers draw in bundle order: image layers at their offset, tiles layers cell by cell,
    objects layers in draw_order() with each prop image placed by object_placement() and
    drawn by draw_image(). It proves what the data says, not how an engine filters or
    sorts at run time.
    """
    canvas = Image.new("RGBA", canvas_size(bundle), (0, 0, 0, 0))
    for layer in bundle.layers:
        if layer.kind == "image" and layer.image is not None:
            image = forge_core.load_rgba(layer.image)[0]
            composite(canvas, image, _local_round_half_up(layer.offset[0]), _local_round_half_up(layer.offset[1]))
        elif layer.kind == "tiles" and layer.grid is not None and layer.tileset in bundle.tilesets:
            composite(canvas, Image.fromarray(tile_layer_rgba(bundle, layer)), 0, 0)
        elif layer.kind == "objects":
            for obj in draw_order([o for o in bundle.objects if o.layer == layer.name]):
                prop = bundle.props.get(obj.prop)
                if prop is not None and prop.image is not None and prop.size is not None:
                    draw_image(canvas, prop.rgba(), *object_placement(obj, prop.size))
    return np.asarray(canvas)


# --------------------------------------------------------------------------- reports

def _local_file_ref(path: Path, base: Path, digest: str | None = None) -> dict:
    """A common fileRef relative to ``base``; a file on another drive records its name only."""
    rel = forge_core.portable_path(path, base)
    if not _REL_PATH.match(rel):
        rel = path.name
    return {"path": rel, "sha256": digest or forge_core.sha256_file(path), "bytes": path.stat().st_size}


def bundle_inputs(bundle: Bundle, base: Path) -> list[dict]:
    refs = [_local_file_ref(bundle.path, base)]
    for entry in bundle.files:
        refs.append(_local_file_ref(entry["file"], base, entry["sha256"]))
    return refs


_CHECK_SECTIONS = (
    ("tilesets", ("$.tilesets",)),
    ("layers", ("$.layers", "$.world", "$.tile_size", "$.terrain")),
    ("props_and_objects", ("$.props", "$.objects")),
    ("collision", ("$.collision",)),
    ("material_map", ("$.material_map",)),
    ("portals", ("$.portals",)),
    ("spawns_anchors_interactions", ("$.spawns", "$.anchors", "$.interactions")),
)


def validation_report(bundle: Bundle, base: Path) -> dict:
    """The validate verb's QA envelope (common qaEnvelope plus bundle facts and problems)."""
    def check(check_id: str, problems: list[Problem]) -> dict:
        errors = sum(p.severity == "error" for p in problems)
        status = "fail" if errors else "warn" if problems else "pass"
        return {"id": check_id, "status": status, "value": {"errors": errors, "warnings": len(problems) - errors},
                "threshold": {"errors": 0}}

    by_code = {code: [p for p in bundle.problems if p.code == code] for code in ("schema", "file", "sha256")}
    rules = [p for p in bundle.problems if p.code not in by_code]
    checks = [check("schema", by_code["schema"]), check("files", by_code["file"]),
              check("sha256", by_code["sha256"])]
    for check_id, prefixes in _CHECK_SECTIONS:
        checks.append(check(check_id, [p for p in rules if p.path.startswith(prefixes)]))
    sectioned = tuple(prefix for _, prefixes in _CHECK_SECTIONS for prefix in prefixes)
    checks.append(check("document", [p for p in rules if not p.path.startswith(sectioned)]))
    status = "fail" if bundle.errors else "warn" if bundle.warnings else "pass"
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "method": ("JSON Schema map.schema.json#/$defs/map_bundle_v2 (vendored copy, built-in Draft 2020-12 "
                   "evaluator), file existence and sha256 of every referenced file, and cross-field rules: "
                   "unique ids, tile indices against tilesets, wang/blob topology, material colours, "
                   "portal targets and arrivals, slots and approach points"),
        "notProven": [
            "reachability, portal arrivals and collision geometry (run map_nav.py check)",
            "that collision and footprints match the painted art",
            "engine or editor import (Tiled GUI, Godot, LDtk not verified)",
        ],
        "checks": checks,
        "inputs": bundle_inputs(bundle, base),
        "outputs": [],
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "bundle": {"id": bundle.id, "version": bundle.version, "world": [bundle.width, bundle.height],
                   "counts": {"tilesets": len(bundle.tilesets), "layers": len(bundle.layers),
                              "props": len(bundle.props), "objects": len(bundle.objects),
                              "portals": len(bundle.portals), "spawns": len(bundle.spawns),
                              "anchors": len(bundle.anchors), "interactions": len(bundle.interactions)}},
        "problems": [p.as_dict() for p in bundle.problems],
    }


# --------------------------------------------------------------------------- CLI

def print_problems(problems: list[Problem]) -> None:
    for problem in problems:
        print(forge_core.ascii_text(f"{problem.severity}: {problem.path}: {problem.message}"), file=sys.stderr)


def _publish_json(data: Any, target: Path) -> None:
    """Write JSON beside ``target`` and publish it without replacing anything."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(handle)
    try:
        forge_core.write_json(temporary, data, no_clobber=False)
        forge_core.publish_file_no_replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def cmd_validate(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.bundle, require_sha256=args.require_sha256)
    print_problems(bundle.problems)
    report_path = None
    if args.report is not None:
        report_path = args.report.resolve()
        _publish_json(validation_report(bundle, report_path.parent), report_path)
    if bundle.errors:
        note = f"; report: {report_path}" if report_path else ""
        print(forge_core.ascii_text(f"error: map bundle has {len(bundle.errors)} error(s){note}"), file=sys.stderr)
        return 1
    print(json.dumps({"status": "warn" if bundle.warnings else "pass", "bundle": str(bundle.path),
                      "id": forge_core.ascii_text(bundle.id), "version": bundle.version,
                      "errors": 0, "warnings": len(bundle.warnings),
                      "metadata": None if report_path is None else str(report_path)}))
    return 0


def cmd_hash(args: argparse.Namespace) -> int:
    target = args.output.resolve()
    if os.path.lexists(target):
        raise BundleError(f"refusing to replace existing output: {target}")
    source = load_bundle(args.bundle, check_hashes=False)
    if source.errors:
        print_problems(source.errors)
        raise BundleError("fix the bundle before hashing it (nothing was written)")
    doc = copy.deepcopy(source.raw)
    hashed = 0
    for container, key, where, sha_key in path_fields(doc):
        resolved = (source.base_dir / container[key]).resolve()
        rel = forge_core.portable_path(resolved, target.parent)
        if not _REL_PATH.match(rel):
            raise BundleError(f"{where}: {container[key]} cannot be reached by a relative path from {target.parent}")
        container[key] = rel
        if sha_key is not None:
            container[sha_key] = forge_core.sha256_file(resolved)
            hashed += 1
    check = bundle_from_document(doc, target)
    if check.errors:
        print_problems(check.errors)
        raise BundleError("the hashed bundle does not validate (nothing was written)")
    _publish_json(doc, target)
    print(json.dumps({"status": "pass", "output": str(target), "metadata": str(target), "files_hashed": hashed}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="map_bundle.py",
        description="Validate and read generate2dmap.map_bundle.v2 playable-map bundles (v1 stays readable).")
    verbs = parser.add_subparsers(dest="verb", required=True)
    validate = verbs.add_parser("validate", help="check the contract, files, sha256 and cross-field rules",
                                description="Check a map bundle: JSON Schema, files and sha256, cross-field rules. "
                                            "Exit 1 on any error; warnings go to stderr.")
    validate.add_argument("--bundle", required=True, type=Path, help="map bundle JSON")
    validate.add_argument("--report", type=Path, help="also write the QA report JSON here (must not exist)")
    validate.add_argument("--require-sha256", action="store_true", help="every referenced file must record its sha256")
    validate.set_defaults(func=cmd_validate)
    hasher = verbs.add_parser("hash", help="write a copy with the sha256 of every referenced file",
                              description="Write a new bundle file with the sha256 of every referenced file filled "
                                          "in and paths rebased to its folder. Refuses an existing output.")
    hasher.add_argument("--bundle", required=True, type=Path, help="map bundle JSON")
    hasher.add_argument("--output", required=True, type=Path, help="new bundle JSON to write (must not exist)")
    hasher.set_defaults(func=cmd_hash)
    return parser


def _local_run_cli(parser: argparse.ArgumentParser, argv: list[str] | None) -> int:
    """Shared CLI driver: UTF-8 console, argparse, and 'error: ...' instead of a traceback."""
    forge_core.utf8_stdio()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (BundleError, OSError, ValueError) as error:
        print(forge_core.ascii_text(f"error: {error}"), file=sys.stderr)
    except Exception as error:  # an internal bug: still no traceback for the user
        print(forge_core.ascii_text(f"error: internal error ({type(error).__name__}): {error}"), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    return _local_run_cli(build_parser(), argv)


if __name__ == "__main__":
    raise SystemExit(main())
