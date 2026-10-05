#!/usr/bin/env python3
"""Validate and read playable map bundles (generate2dmap.map_bundle.v2; v1 stays readable).

A map bundle keeps a playable map as data: world size, tile layers, placed props with
ground footprints, collision, portals, spawns, anchors and interactions. map_nav.py
(collision, reachability, portal checks) and export_tiled.py (Tiled TMJ/TSX) read bundles
through load_bundle() and draw them with render_map().

Collision is not modelled here. The blocking set every map tool shares (integration
decisions D1-D7) comes from the vendored forge_nav.py: world_solids(), footprint_solid(),
tile_solids(), nav_cell() and MaterialMap.class_codes() delegate to it, and merge_rects()
is forge_core's. The contract is evaluated by the vendored forge_schema.py (D31), JSON is
read by forge_core.read_json (strict, a BOM tolerated, D28).

Object art follows one lookup order in every reader (D6): objects[].image, then the props
registry (props[prop]), then the bundle's prop_packs by label, then objects[].occluder.source.
objects[].flip_x mirrors the art around the anchor x (and forge_nav mirrors the footprint).

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
import forge_nav  # noqa: E402  (the shared collision rule book, D4)
import forge_schema  # noqa: E402  (the shared contract evaluator, D31)

TOOL_NAME = "map_bundle.py"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # D29: QA envelopes carry the package version
SCHEMA_V1 = "generate2dmap.map_bundle.v1"
SCHEMA_V2 = "generate2dmap.map_bundle.v2"
TILESET_SCHEMA = "generate2dmap.tileset.v1"
REPORT_SCHEMA = "generate2dmap.map_bundle_report.v1"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "references" / "schemas"

OCCLUSION_CLASSES = ("low", "tall", "foreground")
OCCUPANT_POLICIES = ("y_sort", "rear_shift_and_fade", "static_front", "static_back")
FREE, BLOCK, ONE_WAY = forge_nav.FREE, forge_nav.BLOCK, forge_nav.ONE_WAY
EMPTY_TILE = -1
BLOB_BITS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
ART_SOURCES = ("object", "props", "prop_packs", "occluder")  # the D6 lookup order
# A blob diagonal counts only when both adjacent edges are set (plan Appendix B, tile).
_BLOB_DIAGONALS = ((1, 0, 2), (3, 4, 2), (5, 4, 6), (7, 0, 6))
_REL_PATH = re.compile(r"^(?!/)(?![A-Za-z][A-Za-z0-9+.-]*:)[^\\]+$")
_KNOWN_TOP_LEVEL = frozenset({
    "schema", "id", "name", "tile_size", "world", "terrain", "tilesets", "layers", "props", "prop_packs", "objects",
    "collision", "material_map", "nav", "portals", "spawns", "anchors", "interactions", "roads", "camera", "stage",
    "atmosphere", "lights", "qa", "provenance", "art_source", "placeholder",
})


class BundleError(ValueError):
    """A bundle (or a file it needs) cannot be read at all."""


# --------------------------------------------------------------------------- JSON and the contract

def read_json(path: str | os.PathLike) -> Any:
    """Strict JSON through forge_core.read_json(strict=True) (D28): UTF-8 with an optional BOM, no
    NaN, Infinity or overflowing numbers, no duplicate keys. Raises BundleError naming the file."""
    try:
        return forge_core.read_json(path, strict=True)
    except (OSError, ValueError) as error:
        raise BundleError(f"cannot read JSON {Path(path).name}: {error}") from error


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _brief(value: Any, limit: int = 60) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit - 3] + "..."


json_path = forge_schema.json_path  # jsonschema's JSONPath form, shared with the schema errors

SCHEMAS = forge_schema.schema_set(SCHEMA_DIR)  # the vendored map and common schemas (D31)

# Plain words for the bundle schema's alternatives (anyOf / oneOf), whose generic message names
# none of the allowed forms; keyed by the JSONPath of the failing value.
_KEY = r"(\.[A-Za-z][A-Za-z0-9_]*|\['(?:[^'\\]|\\.)*'\])"  # one object key: .name or ['odd key']
_ALTERNATIVES = (
    (re.compile(r"^\$\.tile_size$"), "tile_size is a whole number of pixels or [width, height]"),
    (re.compile(r"^\$\.props" + _KEY + "$"), "a prop needs an image (or pack + label)"),
    (re.compile(r"^\$\.portals\[\d+\]$"), "a portal needs exactly one trigger: rect [x, y, w, h] or circle [cx, cy, r]"),
    (re.compile(r"^\$\.layers\[\d+\]\.data$"), "tiles layer data is a relative path to a CSV or JSON file, or a list "
                                               "of rows"),
    (re.compile(r"^\$\.(spawns\[\d+\]|anchors" + _KEY + r")\.facing$"), "facing is a direction name or an angle"),
    (re.compile(r"^\$\.anchors" + _KEY + r"\.approach$"), "approach is a point [x, y] or a list of points"),
    (re.compile(r"^\$\.(stage|atmosphere|lights)$"), "give a relative path to the file or the document inline"),
)
_NO_ALTERNATIVE = (" is not valid under any of the given schemas", " is valid under each of ")


def schema_problem(message: str) -> tuple[str, str]:
    """(JSON path, text) of one forge_schema error line; an unmatched alternative gets plain words."""
    path, _, text = message.partition(": ")
    for marker in _NO_ALTERNATIVE:
        if marker in text:
            for pattern, plain in _ALTERNATIVES:
                if pattern.match(path):
                    return path, f"{plain}; got {text.split(marker, 1)[0]}"
    return path, text


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
    """A placed prop: (x, y) is where the prop's anchor_px (the unmirrored image anchor) lands,
    in world pixels. image is its art found by the D6 lookup order (art names the step:
    object, props, prop_packs or occluder), drawn mirrored around the anchor x when flip_x."""
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
    flip_x: bool = False
    image: Path | None = None
    image_size: tuple[int, int] | None = None
    art: str | None = None


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
        """(h, w) uint8: FREE, BLOCK or ONE_WAY per material pixel (forge_nav rule N8)."""
        return forge_nav.material_codes(self.index, self.classes, self.walkable)


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
    _blocking: forge_nav.BlockingSet | None = field(default=None, repr=False)
    _art: dict = field(default_factory=dict, repr=False)

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def art_rgba(self, path: Path) -> Image.Image:
        """An art image as 8-bit straight RGBA, loaded once."""
        if path not in self._art:
            self._art[path] = forge_core.load_rgba(path)[0]
        return self._art[path]

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
    for i, entry in enumerate(_as_list(doc.get("prop_packs"))):
        if isinstance(entry, dict) and isinstance(entry.get("manifest"), str):
            yield entry, "manifest", f"$.prop_packs[{i}].manifest", "sha256"
    for i, obj in enumerate(_as_list(doc.get("objects"))):
        if isinstance(obj, dict) and isinstance(obj.get("image"), str):
            yield obj, "image", f"$.objects[{i}].image", "image_sha256"
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

def nav_cell(actor_radius: float) -> int:
    """Navigation grid cell: max(1, round(r / 2)) with half-up rounding (forge_nav rule N12)."""
    return forge_nav.nav_cell(actor_radius)


def merge_rects(mask: Any) -> list[tuple[int, int, int, int]]:
    """Cover a boolean grid with disjoint axis-aligned rectangles (x, y, w, h) in cells whose
    union is exactly the True cells: forge_core.merge_rects, the one cover every tool shares (D30)."""
    return forge_core.merge_rects(mask)


def object_placement(obj: MapObject, image_size: tuple[int, int]) -> tuple[float, float, float, float]:
    """(left, bottom, width, height) of an object's drawn image in world pixels.

    This is the Tiled tile-object convention (x, y = bottom-left): the image is scaled by
    obj.scale and its anchor_px lands on (obj.x, obj.y). With flip_x the image is drawn
    mirrored around the anchor x (D6), so the mirrored anchor, image width - anchor_px[0],
    is the one that lands on obj.x.
    """
    s = obj.scale
    width, height = image_size[0] * s, image_size[1] * s
    anchor_x = image_size[0] - obj.anchor_px[0] if obj.flip_x else obj.anchor_px[0]
    left = obj.x - anchor_x * s
    bottom = obj.y - obj.anchor_px[1] * s + height
    return left, bottom, width, height


def raster_box(left: float, bottom: float, width: float, height: float) -> tuple[int, int, int, int]:
    """Whole-pixel (x0, y0, w, h) for a drawn image, rounding half up."""
    return (forge_core.round_half_up(left), forge_core.round_half_up(bottom - height),
            forge_core.round_half_up(width), forge_core.round_half_up(height))


def footprint_solid(obj: MapObject) -> dict | None:
    """The object's ground footprint as a world solid (forge_nav rule N6), or None.

    Footprints are measured in prop-image pixels (basis prop_px, or its legacy alias
    image_px) and scaled exactly once by obj.scale; basis world_px is never scaled (D7).
    flip_x mirrors the footprint around the anchor x (D6). A rotated rect becomes a polygon.
    Actor size is never added here.
    """
    if not obj.solid or not obj.footprint:
        return None
    return forge_nav.footprint_solid(obj.x, obj.y, obj.footprint, scale=obj.scale, flip_x=obj.flip_x,
                                     source=f"object:{obj.id}")


def tile_solids(bundle: Bundle) -> list[dict]:
    """World solids from the collision of every placed tile (forge_nav rule N7).

    Each tile's collision shapes are moved to the tile's position; a tile whose
    properties say walkable false and that has no shapes blocks its whole cell.
    Axis-aligned rects with whole-pixel corners are unioned per layer and re-merged
    (solids are closed sets, so this does not change which points are blocked).
    """
    layers = []
    for layer in bundle.layers:
        tileset = bundle.tilesets.get(layer.tileset or "")
        if layer.kind == "tiles" and layer.grid is not None and tileset is not None:
            layers.append(forge_nav.TileLayer(layer.name, layer.grid, tileset.tile_w, tileset.tile_h, tileset.tiles,
                                              tileset.tile_count))
    return forge_nav.tile_solids(layers)


def blocking_set(bundle: Bundle) -> forge_nav.BlockingSet:
    """The D2 blocking set of a readable bundle, read by forge_nav from the same document (cached).

    Raises forge_nav.NavError (a ValueError) when collision cannot be read, for example
    without a collision block."""
    if bundle._blocking is None:
        bundle._blocking = forge_nav.blocking_set_from_document(bundle.raw, bundle.base_dir)
    return bundle._blocking


def world_solids(bundle: Bundle) -> list[dict]:
    """Every blocking shape of the map in world pixels (the D2 set of forge_nav, rule N4):
    collision solids and rects, solid object footprints (scaled once) and tile collision.
    Each carries a "source". A bundle without a collision block has no solids or rects;
    its footprints and tile collision follow the same rules (N6, N7)."""
    if bundle.collision is None:
        footprints = [solid for solid in (footprint_solid(obj) for obj in bundle.objects) if solid is not None]
        return footprints + tile_solids(bundle)
    return blocking_set(bundle).solids


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
        self._pack_labels: dict[str, tuple[int, Path, dict]] | None = None
        self._pack_art: dict[tuple[int, str], tuple[Path | None, tuple[int, int] | None]] = {}

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
            self.error(*schema_problem(message), "schema")
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
                doc = read_json(manifest)
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
                    rows = read_json(path)
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
            grid = read_json(path)
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
            expected = (forge_core.round_half_up(b.height / b.tile_h) + 1,
                        forge_core.round_half_up(b.width / b.tile_w) + 1)
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
            size = None if image is None else self.image_size(f"{where}.image", image, merged["image"])
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
            manifest = read_json(pack)
        except BundleError as error:
            self.error(f"{where}.pack", str(error))
            return None, self.b.base_dir
        for message in SCHEMAS.errors(manifest, "map.schema.json#/$defs/prop_pack_v2"):
            self.error(f"{where}.pack", f"{entry['pack']}: {message}", "schema")
            return None, self.b.base_dir
        label = entry.get("label")
        for item in manifest.get("accepted", []):
            if item.get("label") == label:
                self.placeholder_warning(f"{where}.label", entry["pack"], item)
                return dict(item), pack.parent
        self.error(f"{where}.label", f"prop pack {entry['pack']} has no accepted item {label!r}")
        return None, self.b.base_dir

    def placeholder_warning(self, where: str, pack: str, item: dict) -> None:
        if item.get("status") == "placeholder":
            self.warn(where, f"{pack} item {item.get('label')!r} is a placeholder (status placeholder, an "
                             "--keep-empty stand-in), not extracted art")

    def image_size(self, where: str, path: Path, rel: str) -> tuple[int, int] | None:
        try:
            with Image.open(path) as handle:
                return handle.size
        except OSError as error:
            self.error(where, f"cannot open {rel}: {error}")
            return None

    def prop_pack_index(self) -> dict[str, tuple[int, Path, dict]]:
        """label -> (prop_packs index, manifest path, accepted item) over the bundle's prop_packs
        (D6 step 3); the first manifest listing a label wins. Read once."""
        if self._pack_labels is not None:
            return self._pack_labels
        self._pack_labels = {}
        for i, entry in enumerate(_as_list(self.b.doc.get("prop_packs"))):
            where = f"$.prop_packs[{i}].manifest"
            path = self.file(where, entry["manifest"], entry.get("sha256"), self.b.base_dir)
            if path is None:
                continue
            try:
                manifest = read_json(path)
            except BundleError as error:
                self.error(where, str(error))
                continue
            messages = SCHEMAS.errors(manifest, "map.schema.json#/$defs/prop_pack_v2")
            for message in messages:
                self.error(where, f"{entry['manifest']}: {message}", "schema")
            if messages:
                continue
            for item in manifest["accepted"]:
                if isinstance(item.get("label"), str) and isinstance(item.get("image"), str):
                    self._pack_labels.setdefault(item["label"], (i, path, item))
        return self._pack_labels

    def object_art(self, where: str, entry: dict, prop: Prop | None) -> tuple[Path | None, tuple | None, str | None]:
        """(image, size, step) of an object's art by the D6 lookup order: objects[].image, the
        props registry, prop_packs by label, then occluder.source. The first step the object
        names decides; a missing file there is an error, not a reason to look further."""
        b = self.b
        if "image" in entry:
            path = self.file(f"{where}.image", entry["image"], entry.get("image_sha256"), b.base_dir)
            size = None if path is None else self.image_size(f"{where}.image", path, entry["image"])
            return path, size, "object"
        if prop is not None:
            return prop.image, prop.size, "props"
        found = self.prop_pack_index().get(entry["prop"])
        if found is not None:
            index, manifest, item = found
            self.placeholder_warning(f"{where}.prop", b.doc["prop_packs"][index]["manifest"], item)
            key = (index, entry["prop"])
            if key not in self._pack_art:
                at = f"$.prop_packs[{index}] {entry['prop']!r}.image"
                path = self.file(at, item["image"], item.get("sha256"), manifest.parent)
                self._pack_art[key] = (path, None if path is None else self.image_size(at, path, item["image"]))
            path, size = self._pack_art[key]
            return path, size, "prop_packs"
        occluder = entry.get("occluder")
        if occluder is not None:  # checked as a file by objects()
            path = (b.base_dir / occluder["source"]).resolve()
            if path.is_file():
                return path, self.image_size(f"{where}.occluder.source", path, occluder["source"]), "occluder"
        return None, None, None

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
        if entries and not object_layers:
            self.warn("$.layers", "objects are not drawn: the bundle has no objects layer")
        artless: list[str] = []
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
            image, size, art = self.object_art(where, entry, prop)
            obj = MapObject(id=entry["id"], prop=entry["prop"], x=float(entry["x"]), y=float(entry["y"]),
                            scale=float(entry.get("scale", 1)), anchor_px=_point(entry["anchor_px"]),
                            footprint=footprint, solid=bool(solid), sort_y=float(entry.get("sortY", entry["y"])),
                            layer=layer, occlusion=occlusion, occupant_policy=policy,
                            flip_x=entry.get("flip_x", False) is True, image=image, image_size=size, art=art)
            if art is None:
                artless.append(obj.id)
            if ("footprint" not in entry and prop is not None and prop.footprint is not None
                    and prop.anchor_px is not None and obj.anchor_px != prop.anchor_px):
                dx, dy = obj.anchor_px[0] - prop.anchor_px[0], obj.anchor_px[1] - prop.anchor_px[1]
                self.warn(f"{where}.anchor_px", f"anchor_px {list(obj.anchor_px)} differs from prop {prop.id!r} "
                                                f"anchor_px {list(prop.anchor_px)}: the prop's footprint is relative to "
                                                f"its own anchor, so collision moves against the art by "
                                                f"({dx:g}, {dy:g}) prop px; give the object its own footprint or the "
                                                "prop's anchor")
            if size is not None:
                ax, ay = obj.anchor_px
                if not (0 <= ax <= size[0] and 0 <= ay <= size[1]):
                    self.warn(f"{where}.anchor_px", f"anchor {list(obj.anchor_px)} lies outside the "
                                                    f"{size[0]}x{size[1]} {'prop ' if art == 'props' else ''}image")
            if not (0 <= obj.x <= b.width and 0 <= obj.y <= b.height):
                self.warn(where, f"object {obj.id!r} is anchored outside the world")
            b.objects.append(obj)
        if artless:
            listed = ", ".join(repr(ident) for ident in artless[:8]) + (", ..." if len(artless) > 8 else "")
            self.warn("$.objects", f"{len(artless)} object(s) have no art (no image, props entry, prop_packs label or "
                                   f"occluder source), so they are not drawn or exported as tile objects: {listed}")

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
                    document = read_json(path)
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
    return bundle_from_document(read_json(path), path, check_hashes=check_hashes, require_sha256=require_sha256)


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
    objects layers in draw_order() with each object's art (the D6 lookup order, mirrored
    around the anchor x when flip_x) placed by object_placement() and drawn by draw_image().
    It proves what the data says, not how an engine filters or sorts at run time.
    """
    canvas = Image.new("RGBA", canvas_size(bundle), (0, 0, 0, 0))
    for layer in bundle.layers:
        if layer.kind == "image" and layer.image is not None:
            image = forge_core.load_rgba(layer.image)[0]
            composite(canvas, image, forge_core.round_half_up(layer.offset[0]),
                      forge_core.round_half_up(layer.offset[1]))
        elif layer.kind == "tiles" and layer.grid is not None and layer.tileset in bundle.tilesets:
            composite(canvas, Image.fromarray(tile_layer_rgba(bundle, layer)), 0, 0)
        elif layer.kind == "objects":
            for obj in draw_order([o for o in bundle.objects if o.layer == layer.name]):
                if obj.image is not None and obj.image_size is not None:
                    draw_image(canvas, object_image(bundle, obj), *object_placement(obj, obj.image_size))
    return np.asarray(canvas)


def object_image(bundle: Bundle, obj: MapObject) -> Image.Image:
    """An object's art as drawn: the D6 image, mirrored left to right when flip_x (D6)."""
    image = bundle.art_rgba(obj.image)
    return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT) if obj.flip_x else image


# --------------------------------------------------------------------------- reports

def bundle_inputs(bundle: Bundle, base: Path) -> list[dict]:
    """fileRefs (forge_core.file_ref, D30) of the bundle and every file it references, relative to ``base``."""
    refs = [forge_core.file_ref(bundle.path, base)]
    for entry in bundle.files:
        refs.append(forge_core.file_ref(entry["file"], base, sha256=entry["sha256"]))
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


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def main(argv: list[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27): usage errors exit 2, expected errors print
    'error: <message>' and exit 1, anything else 'error: internal error (<Type>: <message>)'."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
