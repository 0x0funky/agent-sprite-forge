#!/usr/bin/env python3
"""Export a map bundle (generate2dmap.map_bundle.v2) to Godot 4.3+ text resources.

Writes into a new --output-dir:

  <name>.tileset.tres   TileSet: one TileSetAtlasSource per bundle tileset, a terrain
                        set per Wang (Match Corners) or blob-47 (Match Corners and
                        Sides) tileset with its peering bits, physics polygons from
                        each tile's collision shapes and a `walkable` custom data layer
  <name>.tscn           Node2D scene: bundle layers in draw order (TileMapLayer per
                        tiles layer, Sprite2D per image layer, a y-sorted Node2D of
                        Sprite2D props whose origin is the prop's ground anchor),
                        StaticBody2D collision, Marker2D spawns and anchors, Area2D
                        portals and interactions, all carrying their bundle data as
                        metadata
  assets/...            byte-identical copies of every image the files use
  godot-export.json     what was written, what was not, and the parse-back QA

Paths inside the files are relative to the files, so the folder can be copied
anywhere inside a Godot project. Every file is parsed back before publishing:
tiles, peering bits, physics polygons, prop anchors, collision shapes and
markers must round-trip. Verified at parse level only: the Godot editor import
is not run by this tool.

Collision is the D2 blocking set read by forge_nav (scripts/forge_nav.py):
collision.solids and rects and the footprints of solid objects (scaled once,
basis and flip_x applied) become shapes under the collision StaticBody2D; tile
collision becomes TileSet physics polygons (a walkable: false tile without
shapes blocks its whole cell). Material-map classes have no Godot form here:
the blocking ones are listed in the report's notExported.

Prop images are found in the D6 order: objects[].image, the bundle's
props[prop] (an inline item, or a prop pack named by pack + label), prop packs
by label (the bundle's prop_packs, then --prop-pack), occluder.source. flip_x
props become Sprite2D with flip_h, mirrored around their anchor.
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import io
import json
import math
import re
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_nav  # noqa: E402  (this skill's vendored copy: the D2 blocking set, rules N1-N15)


BUNDLE_SCHEMAS = ("generate2dmap.map_bundle.v2", "generate2dmap.map_bundle.v1")
TILESET_SCHEMA = "generate2dmap.tileset.v1"
PROP_PACK_SCHEMAS = (None, "generate2dmap.prop_pack.v2")
REPORT_SCHEMA = "generate2dmap.engine_export.v1"
TOOL = {"name": "export_godot", "version": forge_core.FORGE_PACKAGE_VERSION}
GODOT_TARGET = "4.3+"
GODOT_FORMAT = 3
TERRAIN_MODES = {"match_corners_and_sides": 0, "match_corners": 1, "match_sides": 2}
WANG_BITS = ("top_left_corner", "top_right_corner", "bottom_left_corner", "bottom_right_corner")
BLOB_BITS = ("top_side", "top_right_corner", "right_side", "bottom_right_corner",
             "bottom_side", "bottom_left_corner", "left_side", "top_left_corner")  # N, NE, E, SE, S, SW, W, NW
VARIANT_BOOL = 1
TEXTURE_FILTERS = {"inherit": None, "nearest": 1, "linear": 2}
ELLIPSE_SEGMENTS = 32
NODE_NAME_FORBIDDEN = re.compile(r'[.:@/"%\\]')
EPSILON = 1e-6
NOT_PROVEN = [
    "Parse-level only: the files are re-read by this tool's own reader; the Godot editor import was not run.",
    "Terrain peering bits follow the documented Wang/blob mapping; painting with Godot's terrain brush is not "
    "verified (the centre terrain of a mixed Wang tile is its majority corner material).",
    "Non-circular ellipse solids and footprints become 32-sided polygons inscribed in the ellipse; walk regions ride "
    "as metadata, and the material map and nav grid are not exported (see notExported).",
    "Collision shapes are the forge_nav blocking set at export time; Godot's physics (body shapes, margins, one-way "
    "collision) is not simulated here.",
]


# --------------------------------------------------------------------------- map_bundle.v2 reading
# Shared with export_ldtk.py. Integration moves this reader into map_bundle.py (B13).

@dataclass
class TilesetInfo:
    id: str
    manifest_path: Path
    image_path: Path
    image_sha256: str
    image_size: tuple[int, int]
    tile_size: tuple[int, int]
    columns: int
    kind: str
    materials: list[str]
    tiles: dict[int, dict[str, Any]]
    manifest: dict[str, Any]

    def atlas(self, index: int) -> tuple[int, int]:
        return index % self.columns, index // self.columns


@dataclass
class TileLayerInfo:
    name: str
    tileset: TilesetInfo
    grid: np.ndarray  # int64 rows x cols; -1 is empty


@dataclass
class ImageInfo:
    path: Path
    sha256: str
    size: tuple[int, int]


@dataclass
class BundleInfo:
    path: Path
    sha256: str
    data: dict[str, Any]
    schema: str
    world: tuple[float, float]
    tile_size: tuple[int, int] | None
    tilesets: dict[str, TilesetInfo]
    layers: list[dict[str, Any]]  # {"name", "kind", "tiles": TileLayerInfo | None, "image": ImageInfo | None}
    objects: list[dict[str, Any]]  # bundle objects plus "_image": ImageInfo
    blocking: Any = None  # forge_nav.BlockingSet (D2)
    warnings: list[str] = field(default_factory=list)


def _local_rel_path(base: Path, value: Any, what: str) -> Path:
    """Resolve a manifest-relative POSIX path (common relPath) and require the file."""
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value \
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        raise ValueError(f"{what} must be a relative POSIX path (got {value!r}).")
    path = base / value
    if not path.is_file():
        raise ValueError(f"{what} {value!r} does not exist (looked in {forge_core.ascii_text(str(base))}).")
    return path


def _local_check_sha(path: Path, expected: Any, what: str) -> str:
    actual = forge_core.sha256_file(path)
    if expected is not None and actual != expected:
        raise ValueError(f"{what} {path.name} sha256 {actual[:12]}... does not match the recorded "
                         f"{str(expected)[:12]}...")
    return actual


def _local_png_info(path: Path, expected_sha: Any, what: str) -> ImageInfo:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{what} {path.name} must be a PNG file.")
    sha = forge_core.sha256_bytes(raw)
    if expected_sha is not None and sha != expected_sha:
        raise ValueError(f"{what} {path.name} sha256 {sha[:12]}... does not match the recorded "
                         f"{str(expected_sha)[:12]}...")
    with Image.open(io.BytesIO(raw)) as image:
        size = image.size
    return ImageInfo(path, sha, size)


def _size2(value: Any, what: str) -> tuple[int, int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value, value
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) and not isinstance(v, bool) and v >= 1
                                                           for v in value):
        return value[0], value[1]
    raise ValueError(f"{what} must be a whole number of pixels or [width, height].")


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number.")
    return float(value)


def _point(value: Any, what: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{what} must be [x, y].")
    return _number(value[0], f"{what}[0]"), _number(value[1], f"{what}[1]")


def _ident(item: Any, what: str) -> str:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        raise ValueError(f"{what} needs a non-empty string id.")
    return item["id"]


def _local_check_markers(data: dict[str, Any]) -> None:
    """Spawns, interactions, portals, anchors and collision need the fields the exporters read."""
    for kind in ("spawns", "interactions"):
        seen: set[str] = set()
        for position, item in enumerate(data.get(kind) or []):
            ident = _ident(item, f"{kind}[{position}]")
            if ident in seen:
                raise ValueError(f"{kind} id {ident!r} is used twice.")
            seen.add(ident)
            _number(item.get("x"), f"{kind[:-1]} {ident} x")
            _number(item.get("y"), f"{kind[:-1]} {ident} y")
            if "reach" in item and _number(item["reach"], f"interaction {ident} reach") < 0:
                raise ValueError(f"interaction {ident}: reach must not be negative.")
    seen = set()
    for position, portal in enumerate(data.get("portals") or []):
        ident = _ident(portal, f"portals[{position}]")
        if ident in seen:
            raise ValueError(f"portal id {ident!r} is used twice.")
        seen.add(ident)
        if not isinstance(portal.get("to"), str) or not portal["to"]:
            raise ValueError(f"portal {ident} needs a destination in to.")
        if ("rect" in portal) == ("circle" in portal):
            raise ValueError(f"portal {ident} needs exactly one of rect [x, y, w, h] or circle [cx, cy, r].")
        shape = portal.get("rect", portal.get("circle"))
        if not isinstance(shape, list) or len(shape) != (4 if "rect" in portal else 3):
            raise ValueError(f"portal {ident}: {'rect' if 'rect' in portal else 'circle'} has the wrong length.")
        values = [_number(value, f"portal {ident} shape") for value in shape]
        if min(values[2:]) < 0:
            raise ValueError(f"portal {ident}: sizes must not be negative.")
        if "travelDirection" in portal:
            _point(portal["travelDirection"], f"portal {ident} travelDirection")
    anchors = data.get("anchors") or {}
    if not isinstance(anchors, dict):
        raise ValueError("anchors must map names to {point, ...}.")
    for name, anchor in anchors.items():
        if not isinstance(anchor, dict):
            raise ValueError(f"anchor {name} must be an object with a point.")
        _point(anchor.get("point"), f"anchor {name} point")
    collision = data.get("collision")
    if collision is not None:
        if not isinstance(collision, dict):
            raise ValueError("collision must be an object.")
        for position, region in enumerate(collision.get("walkRegions") or []):
            polygons = [region.get("polygon")] + list(region.get("holes") or []) if isinstance(region, dict) else [None]
            for polygon in polygons:
                if not isinstance(polygon, list) or len(polygon) < 3:
                    raise ValueError(f"collision.walkRegions[{position}] polygons need at least three points.")
                for point in polygon:
                    _point(point, f"collision.walkRegions[{position}] point")
        for position, rect in enumerate(collision.get("rects") or []):
            if not isinstance(rect, list) or len(rect) != 4:
                raise ValueError(f"collision.rects[{position}] must be [x, y, w, h].")
        for position, solid in enumerate(collision.get("solids") or []):
            if not isinstance(solid, dict):
                raise ValueError(f"collision.solids[{position}] must be an object with a shape.")
            _shape_polygon(solid, f"collision.solids[{position}]")


def _local_load_tileset(path: Path, expected_sha: Any, ident: str) -> TilesetInfo:
    """Read a tileset.v1 manifest and its atlas image; tile indices must fit the atlas."""
    _local_check_sha(path, expected_sha, f"tileset {ident} manifest")
    manifest = _local_read_json(path, f"tileset {ident} manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") != TILESET_SCHEMA:
        raise ValueError(f"tileset {ident}: {path.name} must have schema {TILESET_SCHEMA!r}.")
    tile_size = _size2(manifest.get("tile_size"), f"tileset {ident} tile_size")
    columns = manifest.get("columns")
    if isinstance(columns, bool) or not isinstance(columns, int) or columns < 1:
        raise ValueError(f"tileset {ident}: columns must be a whole number, at least 1.")
    image = _local_png_info(_local_rel_path(path.parent, manifest.get("image"), f"tileset {ident} image"),
                            manifest.get("sha256"), f"tileset {ident} image")
    if image.size[0] < columns * tile_size[0]:
        raise ValueError(f"tileset {ident}: image is {image.size[0]} px wide, less than {columns} columns of "
                         f"{tile_size[0]} px.")
    rows = image.size[1] // tile_size[1]
    materials = manifest.get("materials")
    if not isinstance(materials, list) or not materials or not all(isinstance(m, str) and m for m in materials):
        raise ValueError(f"tileset {ident}: materials must be a non-empty list of names.")
    kind = manifest.get("kind")
    if kind not in ("wang_corner", "blob47", "flat", "bevel"):
        raise ValueError(f"tileset {ident}: kind must be wang_corner, blob47, flat or bevel.")
    tiles: dict[int, dict[str, Any]] = {}
    for position, tile in enumerate(manifest.get("tiles") or []):
        index = tile.get("index") if isinstance(tile, dict) else None
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError(f"tileset {ident}: tiles[{position}] needs a whole-number index.")
        if index in tiles:
            raise ValueError(f"tileset {ident}: tile index {index} is listed twice.")
        if index // columns >= rows:
            raise ValueError(f"tileset {ident}: tile {index} lies below the {image.size[0]}x{image.size[1]} atlas.")
        wang = tile.get("wang")
        if wang is not None and (not isinstance(wang, list) or len(wang) != 4
                                 or not all(isinstance(v, int) and 0 <= v < len(materials) for v in wang)):
            raise ValueError(f"tileset {ident}: tile {index} wang must be four material indices "
                             f"below {len(materials)}.")
        blob = tile.get("blob_mask")
        if blob is not None and (isinstance(blob, bool) or not isinstance(blob, int) or not 0 <= blob <= 255):
            raise ValueError(f"tileset {ident}: tile {index} blob_mask must be 0..255.")
        tiles[index] = tile
    if not tiles:
        raise ValueError(f"tileset {ident}: tiles must list at least one tile.")
    return TilesetInfo(ident, path, image.path, image.sha256, image.size, tile_size, columns, kind, list(materials),
                       tiles, manifest)


def _local_layer_grid(bundle_dir: Path, layer: dict[str, Any], shape: tuple[int, int]) -> np.ndarray:
    """A tiles layer's index grid (rows x cols, -1 empty) from inline rows, a CSV or a JSON file."""
    name = layer["name"]
    data = layer.get("data")
    if isinstance(data, str):
        path = _local_rel_path(bundle_dir, data, f"layer {name} data")
        _local_check_sha(path, layer.get("sha256"), f"layer {name} data")
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".csv":
            values: Any = [[cell.strip() for cell in line.split(",") if cell.strip() != ""]
                           for line in text.splitlines() if line.strip()]
            try:
                values = [[int(cell) for cell in row] for row in values]
            except ValueError as error:
                raise ValueError(f"layer {name}: {path.name} must hold whole tile indices ({error}).") from None
        else:
            values = _local_read_json(path, f"layer {name} data")
            if isinstance(values, dict):
                values = values.get("data")
    else:
        values = data
    rows, cols = shape
    if not isinstance(values, list) or not values:
        raise ValueError(f"layer {name}: data must be rows of tile indices.")
    if all(isinstance(row, list) for row in values):
        if {len(row) for row in values} != {cols} or len(values) != rows:
            raise ValueError(f"layer {name}: data is {len(values)} rows of {sorted({len(r) for r in values})} "
                             f"cells; the {shape[1]}x{shape[0]} tile world needs {rows} rows of {cols}.")
        flat = [cell for row in values for cell in row]
    else:
        if len(values) != rows * cols:
            raise ValueError(f"layer {name}: flat data has {len(values)} cells; the world needs {rows}x{cols}.")
        flat = values
    grid = np.empty(len(flat), np.int64)
    for position, cell in enumerate(flat):
        if cell is None:
            grid[position] = -1
        elif isinstance(cell, bool) or not isinstance(cell, int) or cell < -1:
            raise ValueError(f"layer {name}: cell {position} must be a tile index, -1 or null (got {cell!r}).")
        else:
            grid[position] = cell
    return grid.reshape(rows, cols)


def _local_read_json(path: Path, what: str) -> Any:
    """Strict JSON (D28): UTF-8 with an optional BOM, no NaN, Infinity or duplicate keys, as forge_nav reads it."""
    try:
        return forge_core.read_json(path, strict=True)
    except ValueError as error:
        raise ValueError(f"{what}: {path.name} is not valid JSON ({error}).") from None


def _local_prop_pack_images(paths: list[Path]) -> dict[str, tuple[Path, str | None]]:
    """label -> (image path, recorded sha256) from prop-pack manifests (v1 or v2); the first pack naming a label wins."""
    found: dict[str, tuple[Path, str | None]] = {}
    for path in paths:
        manifest = _local_read_json(path, "prop pack")
        if not isinstance(manifest, dict) or manifest.get("schema") not in PROP_PACK_SCHEMAS \
                or not isinstance(manifest.get("accepted"), list):
            raise ValueError(f"{path.name} is not a prop pack manifest (accepted list, schema v1 or v2).")
        for item in manifest["accepted"]:
            if isinstance(item, dict) and isinstance(item.get("label"), str) and item.get("image"):
                image = _local_rel_path(path.parent, item["image"], f"prop {item['label']} image")
                found.setdefault(item["label"], (image, item.get("sha256")))
    return found


def _local_prop_registry(base: Path, data: dict[str, Any]) -> dict[str, tuple[Path, str | None] | None]:
    """The bundle's props registry as art (D6 step 2): name -> (image, recorded sha256). An inline item's image is
    relative to the bundle; a pack + label item's image comes from that prop pack's accepted item, relative to the
    pack, unless the entry gives its own image. None when the item names no image."""
    registry = data.get("props")
    if registry is None:
        return {}
    if not isinstance(registry, dict):
        raise ValueError("props must map prop names to items (an image, or a pack plus a label).")
    found: dict[str, tuple[Path, str | None] | None] = {}
    for name, entry in registry.items():
        if not isinstance(entry, dict):
            raise ValueError(f"props[{name!r}] must be an object.")
        if isinstance(entry.get("image"), str):
            found[name] = (_local_rel_path(base, entry["image"], f"props[{name!r}] image"), entry.get("sha256"))
        elif "pack" in entry:
            pack = _local_rel_path(base, entry["pack"], f"props[{name!r}] pack")
            manifest = _local_read_json(pack, f"props[{name!r}] pack")
            items = [item for item in (manifest.get("accepted") or [] if isinstance(manifest, dict) else [])
                     if isinstance(item, dict) and item.get("label") == entry.get("label")]
            if not items:
                raise ValueError(f"props[{name!r}]: prop pack {entry['pack']} has no accepted item "
                                 f"{entry.get('label')!r}.")
            image = items[0].get("image")
            found[name] = (None if not isinstance(image, str) else
                           (_local_rel_path(pack.parent, image, f"props[{name!r}] pack image"), items[0].get("sha256")))
        else:
            found[name] = None
    return found


def _local_blocking_set(path: Path, data: dict[str, Any]) -> Any:
    """The D2 blocking set through forge_nav (the reader every map tool shares, D4). A bundle without a collision
    block still has footprints, tile collision and materials: they are read with an actor radius of 0."""
    document = data if isinstance(data.get("collision"), dict) else {**data, "collision": {"actorRadius": 0}}
    try:
        return forge_nav.blocking_set_from_document(document, path.parent)
    except forge_nav.NavError as error:
        raise ValueError(f"collision (forge_nav): {error}") from None


def _local_read_bundle(path: Path, prop_packs: Sequence[Path] = ()) -> BundleInfo:
    """Read a map_bundle.v2 file and everything it references, checking paths and recorded sha256."""
    raw = path.read_bytes()
    try:
        data = forge_core.parse_json(raw, strict=True)
    except ValueError as error:
        raise ValueError(f"{path.name} is not valid JSON ({error}).") from None
    if not isinstance(data, dict) or data.get("schema") not in BUNDLE_SCHEMAS:
        raise ValueError(f"{path.name} must be a map bundle (schema {BUNDLE_SCHEMAS[0]!r}).")
    base = path.parent
    world_data = data.get("world")
    if not isinstance(world_data, dict):
        raise ValueError("the bundle needs world {width, height, unit: px}.")
    world = (_number(world_data.get("width"), "world.width"), _number(world_data.get("height"), "world.height"))
    if min(world) <= 0:
        raise ValueError("world width and height must be positive.")
    tile_size = _size2(data["tile_size"], "tile_size") if "tile_size" in data else None
    _local_check_markers(data)
    tilesets: dict[str, TilesetInfo] = {}
    for position, entry in enumerate(data.get("tilesets") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ValueError(f"tilesets[{position}] needs an id and a manifest.")
        if entry["id"] in tilesets:
            raise ValueError(f"tileset id {entry['id']!r} is used twice.")
        manifest = _local_rel_path(base, entry.get("manifest"), f"tileset {entry['id']} manifest")
        tilesets[entry["id"]] = _local_load_tileset(manifest, entry.get("sha256"), entry["id"])
    layers: list[dict[str, Any]] = []
    names: set[str] = set()
    for position, layer in enumerate(data.get("layers") or []):
        if not isinstance(layer, dict) or not isinstance(layer.get("name"), str) or not layer["name"]:
            raise ValueError(f"layers[{position}] needs a name and a kind.")
        if layer["name"] in names:
            raise ValueError(f"layer name {layer['name']!r} is used twice.")
        names.add(layer["name"])
        kind = layer.get("kind")
        entry: dict[str, Any] = {"name": layer["name"], "kind": kind, "tiles": None, "image": None}
        if kind == "tiles":
            if tile_size is None:
                raise ValueError("a bundle with a tiles layer needs tile_size.")
            ident = layer.get("tileset") or (next(iter(tilesets)) if len(tilesets) == 1 else None)
            if ident not in tilesets:
                raise ValueError(f"layer {layer['name']}: tileset {layer.get('tileset')!r} is not in tilesets "
                                 f"(name it when the bundle has several).")
            tileset = tilesets[ident]
            shape = (math.ceil(world[1] / tile_size[1]), math.ceil(world[0] / tile_size[0]))
            grid = _local_layer_grid(base, layer, shape)
            unknown = sorted(set(np.unique(grid[grid >= 0]).tolist()) - set(tileset.tiles))
            if unknown:
                raise ValueError(f"layer {layer['name']} uses tile indices {unknown[:8]} that tileset {ident} does "
                                 f"not define.")
            entry["tiles"] = TileLayerInfo(layer["name"], tileset, grid)
        elif kind == "image":
            image = _local_rel_path(base, layer.get("image"), f"layer {layer['name']} image")
            entry["image"] = _local_png_info(image, layer.get("sha256"), f"layer {layer['name']} image")
        elif kind != "objects":
            raise ValueError(f"layer {layer['name']}: kind must be tiles, image or objects.")
        layers.append(entry)
    pack_paths = []  # D6 step 3: the bundle's prop_packs, then --prop-pack
    for position, entry in enumerate(data.get("prop_packs") or []):
        manifest = _local_rel_path(base, entry.get("manifest") if isinstance(entry, dict) else entry,
                                   f"prop_packs[{position}]")
        if isinstance(entry, dict):
            _local_check_sha(manifest, entry.get("sha256"), f"prop_packs[{position}]")
        pack_paths.append(manifest)
    pack_paths += list(prop_packs)
    packs = _local_prop_pack_images(pack_paths)
    registry = _local_prop_registry(base, data)
    objects = []
    seen: set[str] = set()
    for position, item in enumerate(data.get("objects") or []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError(f"objects[{position}] needs an id.")
        if item["id"] in seen:
            raise ValueError(f"object id {item['id']!r} is used twice.")
        seen.add(item["id"])
        prop = item.get("prop")
        _number(item.get("x"), f"object {item['id']} x")
        _number(item.get("y"), f"object {item['id']} y")
        _point(item.get("anchor_px"), f"object {item['id']} anchor_px")
        scale = _number(item.get("scale", 1), f"object {item['id']} scale")
        if scale <= 0:
            raise ValueError(f"object {item['id']}: scale must be positive.")
        if "sortY" in item:
            _number(item["sortY"], f"object {item['id']} sortY")
        if not isinstance(item.get("flip_x", False), bool):
            raise ValueError(f"object {item['id']}: flip_x must be true or false.")
        if isinstance(item.get("image"), str):
            image = _local_png_info(_local_rel_path(base, item["image"], f"object {item['id']} image"),
                                    item.get("image_sha256"), f"object {item['id']} image")
        elif registry.get(prop) is not None:
            image = _local_png_info(registry[prop][0], registry[prop][1], f"props[{prop!r}] image")
        elif prop in packs:
            image = _local_png_info(packs[prop][0], packs[prop][1], f"prop {prop} image")
        elif isinstance(item.get("occluder"), dict) and isinstance(item["occluder"].get("source"), str):
            image = _local_png_info(_local_rel_path(base, item["occluder"]["source"], f"object {item['id']} occluder"),
                                    None, f"object {item['id']} occluder")
        else:
            raise ValueError(f"object {item['id']}: no image for prop {prop!r}; set objects[].image, give the "
                             f"bundle's props[{prop!r}] an image, list a prop pack in prop_packs, or pass --prop-pack.")
        ax, ay = item["anchor_px"]
        if not (0 <= ax <= image.size[0] and 0 <= ay <= image.size[1]):
            raise ValueError(f"object {item['id']}: anchor_px {item['anchor_px']} lies outside its "
                             f"{image.size[0]}x{image.size[1]} image {image.path.name}.")
        objects.append({**item, "_image": image})
    return BundleInfo(path, forge_core.sha256_bytes(raw), data, data["schema"], world, tile_size, tilesets, layers,
                      objects, _local_blocking_set(path, data))


def _local_safe_name(text: str, taken: set[str], fallback: str = "item") -> str:
    """An ASCII file/identifier stem made unique within `taken` (deterministic suffixes)."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", str(text)).strip("-")
    if not stem:  # nothing ASCII left (e.g. a CJK label): keep names apart with a hash of the original
        stem = f"{fallback}-{hashlib.sha256(str(text).encode('utf-8')).hexdigest()[:6]}"
    name, number = stem, 2
    while name.lower() in taken:
        name, number = f"{stem}-{number}", number + 1
    taken.add(name.lower())
    return name


def _local_used_tilesets(bundle: BundleInfo) -> list[str]:
    """Tileset ids in the order tiles layers first use them."""
    used: list[str] = []
    for layer in bundle.layers:
        if layer["kind"] == "tiles" and layer["tiles"].tileset.id not in used:
            used.append(layer["tiles"].tileset.id)
    return used


def _local_not_exported(bundle: BundleInfo) -> list[str]:
    """Bundle fields an engine file does not represent, so the report can say so."""
    data = bundle.data
    notes = [f"tileset {ident}: not used by any tiles layer" for ident in bundle.tilesets
             if ident not in _local_used_tilesets(bundle)]
    for key in ("terrain", "material_map", "nav", "camera", "stage", "atmosphere", "lights"):
        if key in data:
            notes.append(f"{key}: not exported (stays in the map bundle)")
    classes = bundle.blocking.blocking_material_classes if bundle.blocking is not None else []
    if classes:  # D2: an exporter that cannot represent a blocking class lists it
        notes.append(f"material_map blocking classes {', '.join(classes)}: no engine collision is made from the "
                     f"material map, so those pixels do not block in the engine (forge_nav and the preview block them)")
    if any(item.get("animatedParts") for item in data.get("objects") or [] if isinstance(item, dict)):
        notes.append("objects[].animatedParts: not exported")
    return notes


def _local_world_warnings(bundle: BundleInfo) -> list[str]:
    """Placed things outside the world rectangle (a strict-QC failure)."""
    width, height = bundle.world
    outside = []

    def check(kind: str, ident: str, x: float, y: float) -> None:
        if not (-EPSILON <= x <= width + EPSILON and -EPSILON <= y <= height + EPSILON):
            outside.append(f"{kind} {ident} at ({x:g}, {y:g}) lies outside the {width:g}x{height:g} world")

    for item in bundle.objects:
        check("object", item["id"], item["x"], item["y"])
    for item in bundle.data.get("spawns") or []:
        check("spawn", str(item.get("id")), item["x"], item["y"])
    for item in bundle.data.get("interactions") or []:
        check("interaction", str(item.get("id")), item["x"], item["y"])
    for portal in bundle.data.get("portals") or []:
        if "rect" in portal:
            x, y, w, h = portal["rect"]
            check("portal", str(portal.get("id")), x + w / 2, y + h / 2)
        elif "circle" in portal:
            check("portal", str(portal.get("id")), portal["circle"][0], portal["circle"][1])
    return outside


# --------------------------------------------------------------------------- Godot text values

@dataclass(frozen=True)
class GdCall:
    """A Godot constructor literal such as Vector2(1, 2) or ExtResource("1_tileset")."""
    name: str
    args: tuple[Any, ...]


def _gd_number(value: float | int, *, standalone: bool = True) -> str:
    """Godot spelling: ints as ints; a standalone float keeps a '.0' (as Godot writes it) so it stays a
    float, while constructor components such as Vector2(40, 60) are written bare."""
    if isinstance(value, bool):
        raise TypeError("booleans are not numbers here")
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Godot files cannot hold NaN or infinity here.")
    text = "0" if abs(value) < 5e-7 else (str(int(value)) if value == int(value)
                                          else f"{value:.6f}".rstrip("0").rstrip("."))
    return text + ".0" if standalone and "." not in text else text


def _gd_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def gd_value(value: Any) -> str:
    """Godot text-resource literal for a Python value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, np.integer, np.floating)):
        return _gd_number(value)
    if isinstance(value, str):
        return _gd_string(value)
    if isinstance(value, GdCall):
        if value.name == "PackedByteArray":  # large tile_map_data: plain ints, no per-item dispatch
            return f"PackedByteArray({', '.join(map(str, value.args))})"
        return f"{value.name}({', '.join(_gd_arg(arg) for arg in value.args)})"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(gd_value(item) for item in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{" + ", ".join(f"{_gd_string(str(key))}: {gd_value(item)}" for key, item in value.items()) + "}"
    raise TypeError(f"cannot write {type(value).__name__} into a Godot file")


def _gd_arg(value: Any) -> str:
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        return _gd_number(value, standalone=False)
    return gd_value(value)


def vec2(x: float, y: float) -> GdCall:
    return GdCall("Vector2", (float(x), float(y)))


def vec2i(x: int, y: int) -> GdCall:
    return GdCall("Vector2i", (int(x), int(y)))


def packed_vec2(points: Sequence[Sequence[float]]) -> GdCall:
    return GdCall("PackedVector2Array", tuple(float(c) for point in points for c in point))


def _json_to_gd(value: Any) -> Any:
    """Bundle JSON as Godot metadata: [x, y] number pairs become Vector2, the rest stays Array/Dictionary."""
    if isinstance(value, list):
        if len(value) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            return vec2(*value)
        return [_json_to_gd(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_to_gd(item) for key, item in value.items()}
    return value


class GdWriter:
    """Builds a Godot 4 text resource (.tres) or scene (.tscn) with deterministic ids."""

    def __init__(self, kind: str, resource_type: str | None = None) -> None:
        self.kind = kind  # "gd_resource" or "gd_scene"
        self.resource_type = resource_type
        self.ext: list[tuple[str, str, str]] = []  # (type, path, id)
        self.sub: list[tuple[str, str, list[tuple[str, Any]]]] = []
        self.body: list[str] = []

    def ext_resource(self, kind: str, path: str) -> GdCall:
        for existing_kind, existing_path, ident in self.ext:
            if (existing_kind, existing_path) == (kind, path):
                return GdCall("ExtResource", (ident,))
        stem = re.sub(r"[^A-Za-z0-9_]+", "_", Path(path).stem).strip("_") or "res"
        ident = f"{len(self.ext) + 1}_{stem}"
        self.ext.append((kind, path, ident))
        return GdCall("ExtResource", (ident,))

    def sub_resource(self, kind: str, properties: list[tuple[str, Any]]) -> GdCall:
        ident = f"{kind}_{len(self.sub)}"
        self.sub.append((kind, ident, properties))
        return GdCall("SubResource", (ident,))

    def section(self, header: str, properties: list[tuple[str, Any]]) -> None:
        self.body.append(header + "\n" + "".join(f"{key} = {gd_value(value)}\n" for key, value in properties))

    def text(self) -> str:
        steps = len(self.ext) + len(self.sub) + 1
        head = f"[{self.kind}"
        if self.resource_type:
            head += f' type="{self.resource_type}"'
        if steps > 1:
            head += f" load_steps={steps}"
        head += f" format={GODOT_FORMAT}]\n"
        parts = [head]
        if self.ext:
            parts.append("".join(f'[ext_resource type="{kind}" path={_gd_string(path)} id="{ident}"]\n'
                                 for kind, path, ident in self.ext))
        for kind, ident, properties in self.sub:
            parts.append(f'[sub_resource type="{kind}" id="{ident}"]\n'
                         + "".join(f"{key} = {gd_value(value)}\n" for key, value in properties))
        parts += self.body
        return "\n".join(parts)


# --------------------------------------------------------------------------- Godot text reader (QA)

@dataclass
class GdSection:
    tag: str
    fields: dict[str, Any]
    properties: dict[str, Any]


class _GdParser:
    _NUMBER = re.compile(r"[-+]?(?:inf|nan|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
    _IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    _KEY = re.compile(r"[^\s=\[]+")

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def error(self, message: str) -> ValueError:
        line = self.text.count("\n", 0, self.pos) + 1
        return ValueError(f"Godot text line {line}: {message}")

    def skip(self) -> None:
        while self.pos < len(self.text):
            if self.text[self.pos] in " \t\r\n":
                self.pos += 1
            elif self.text[self.pos] == ";":
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end
            else:
                return

    def expect(self, char: str) -> None:
        self.skip()
        if not self.text.startswith(char, self.pos):
            raise self.error(f"expected {char!r}")
        self.pos += len(char)

    def string(self) -> str:
        assert self.text[self.pos] == '"'
        self.pos += 1
        out = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                escape = self.text[self.pos + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(escape, escape))
                self.pos += 2
            elif char == '"':
                self.pos += 1
                return "".join(out)
            else:
                out.append(char)
                self.pos += 1
        raise self.error("unterminated string")

    def value(self) -> Any:
        self.skip()
        char = self.text[self.pos:self.pos + 1]
        if char in ("&", "^") and self.text[self.pos + 1:self.pos + 2] == '"':
            self.pos += 1
            return self.string()
        if char == '"':
            return self.string()
        if char == "[":
            self.pos += 1
            items = []
            self.skip()
            while not self.text.startswith("]", self.pos):
                items.append(self.value())
                self.skip()
                if self.text.startswith(",", self.pos):
                    self.pos += 1
                    self.skip()
            self.pos += 1
            return items
        if char == "{":
            self.pos += 1
            items: dict[Any, Any] = {}
            self.skip()
            while not self.text.startswith("}", self.pos):
                key = self.value()
                self.expect(":")
                items[key] = self.value()
                self.skip()
                if self.text.startswith(",", self.pos):
                    self.pos += 1
                    self.skip()
            self.pos += 1
            return items
        number = self._NUMBER.match(self.text, self.pos)
        if number and not self._IDENT.match(self.text, self.pos):
            self.pos = number.end()
            token = number.group(0)
            return float(token) if any(c in token for c in ".eEn") else int(token)
        ident = self._IDENT.match(self.text, self.pos)
        if not ident:
            raise self.error(f"unexpected {char!r}")
        self.pos = ident.end()
        name = ident.group(0)
        if name in ("true", "false"):
            return name == "true"
        if name == "null":
            return None
        if name in ("inf", "nan"):
            return float(name)
        self.expect("(")
        self.skip()
        if name.startswith("Packed") and name.endswith("Array") and not self.text.startswith('"', self.pos):
            return self._packed_numbers(name)
        args = []
        while not self.text.startswith(")", self.pos):
            args.append(self.value())
            self.skip()
            if self.text.startswith(",", self.pos):
                self.pos += 1
                self.skip()
        self.pos += 1
        return GdCall(name, tuple(args))

    def _packed_numbers(self, name: str) -> GdCall:
        """Fast path for numeric packed arrays (tile_map_data can hold millions of bytes)."""
        end = self.text.find(")", self.pos)
        if end < 0:
            raise self.error(f"unterminated {name}")
        body = self.text[self.pos:end].strip()
        self.pos = end + 1
        values = []
        for token in body.split(",") if body else []:
            token = token.strip()
            try:
                values.append(int(token))
            except ValueError:
                try:
                    values.append(float(token))
                except ValueError:
                    raise self.error(f"{name} holds {token!r}, not a number") from None
        return GdCall(name, tuple(values))

    def header(self) -> tuple[str, dict[str, Any]]:
        self.expect("[")
        tag = self._IDENT.match(self.text, self.pos)
        if not tag:
            raise self.error("expected a section name")
        self.pos = tag.end()
        fields = {}
        while True:
            self.skip()
            if self.text.startswith("]", self.pos):
                self.pos += 1
                return tag.group(0), fields
            key = self._IDENT.match(self.text, self.pos)
            if not key:
                raise self.error("expected a header field")
            self.pos = key.end()
            self.expect("=")
            fields[key.group(0)] = self.value()

    def sections(self) -> list[GdSection]:
        out: list[GdSection] = []
        self.skip()
        while self.pos < len(self.text):
            tag, fields = self.header()
            properties: dict[str, Any] = {}
            while True:
                self.skip()
                if self.pos >= len(self.text) or self.text[self.pos] == "[":
                    break
                key = self._KEY.match(self.text, self.pos)
                if not key:
                    raise self.error("expected a property")
                self.pos = key.end()
                self.expect("=")
                if key.group(0) in properties:
                    raise self.error(f"property {key.group(0)} is set twice")
                properties[key.group(0)] = self.value()
            out.append(GdSection(tag, fields, properties))
        return out


def parse_godot_text(text: str) -> list[GdSection]:
    """Parse a Godot 4 .tres/.tscn into sections (header tag, header fields, properties)."""
    return _GdParser(text).sections()


def decode_tile_map_data(data: Sequence[int]) -> list[tuple[int, int, int, int, int, int]]:
    """TileMapLayer.tile_map_data: uint16 format 0, then (x, y, source, atlas x, atlas y, alternative) per cell."""
    raw = bytes(data)
    if len(raw) < 2 or (len(raw) - 2) % 12:
        raise ValueError("tile_map_data must be 2 + 12*n bytes.")
    if struct.unpack_from("<H", raw, 0)[0] != 0:
        raise ValueError("tile_map_data format must be 0.")
    return [struct.unpack_from("<hhHHHH", raw, 2 + 12 * i) for i in range((len(raw) - 2) // 12)]


# --------------------------------------------------------------------------- TileSet

def _material_color(name: str) -> GdCall:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    red, green, blue = colorsys.hsv_to_rgb(digest[0] / 255, 0.55, 0.85)
    return GdCall("Color", (round(red, 3), round(green, 3), round(blue, 3), 1.0))


def terrain_mode(tileset: TilesetInfo) -> str | None:
    tiles = tileset.tiles.values()
    if all("wang" in tile for tile in tiles):
        return "match_corners"
    if all("blob_mask" in tile for tile in tiles):
        return "match_corners_and_sides"
    return None


def blob_inside(tileset: TilesetInfo, requested: str | None) -> int:
    """Material index of the blob's own (inside) terrain: --blob-inside, else the manifest's optional
    blob_inside, else the last material."""
    requested = requested if requested is not None else tileset.manifest.get("blob_inside")
    if requested is None:
        return len(tileset.materials) - 1
    if requested not in tileset.materials:
        raise ValueError(f"blob inside material {requested!r} is not a material of tileset {tileset.id} "
                         f"({', '.join(tileset.materials)}).")
    return tileset.materials.index(requested)


def expected_terrain(tileset: TilesetInfo, tile: dict[str, Any], inside: int | None) -> dict[str, int] | None:
    """terrain (centre) and peering bits for one tile, or None when the tileset has no terrain data."""
    mode = terrain_mode(tileset)
    if mode == "match_corners":
        corners = tile["wang"]
        counts = np.bincount(corners, minlength=len(tileset.materials))
        bits = {name: int(value) for name, value in zip(WANG_BITS, corners)}
        return {"terrain": int(np.argmax(counts)), **bits}
    if mode == "match_corners_and_sides":
        mask = tile["blob_mask"]
        outside = next((i for i in range(len(tileset.materials)) if i != inside), None)
        bits = {}
        for bit, name in enumerate(BLOB_BITS):
            value = inside if mask >> bit & 1 else outside
            if value is not None:
                bits[name] = int(value)
        return {"terrain": int(inside), **bits}
    return None


def _shape_polygon(shape: dict[str, Any], what: str) -> list[tuple[float, float]]:
    """A solid (rect, ellipse or polygon) as polygon points in the same pixel space."""
    kind = shape.get("shape")
    if kind == "rect":
        x, y = _number(shape.get("x"), f"{what} x"), _number(shape.get("y"), f"{what} y")
        w, h = _number(shape.get("w"), f"{what} w"), _number(shape.get("h"), f"{what} h")
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    if kind == "ellipse":
        cx, cy = _number(shape.get("cx"), f"{what} cx"), _number(shape.get("cy"), f"{what} cy")
        rx, ry = _number(shape.get("rx"), f"{what} rx"), _number(shape.get("ry"), f"{what} ry")
        angle = math.radians(_number(shape.get("rotate", 0), f"{what} rotate"))
        points = []
        for step in range(ELLIPSE_SEGMENTS):
            t = 2 * math.pi * step / ELLIPSE_SEGMENTS
            px, py = rx * math.cos(t), ry * math.sin(t)
            points.append((cx + px * math.cos(angle) - py * math.sin(angle),
                           cy + px * math.sin(angle) + py * math.cos(angle)))
        return points
    if kind == "polygon":
        points = shape.get("points")
        if not isinstance(points, list) or len(points) < 3:
            raise ValueError(f"{what} polygon needs at least three points.")
        return [_point(point, f"{what} point") for point in points]
    raise ValueError(f"{what} shape must be rect, ellipse or polygon.")


def _has_area(points: list[tuple[float, float]]) -> bool:
    """N4: a polygon whose shoelace sum is exactly 0 blocks nothing (rects and ellipses of zero size included)."""
    xs, ys = [x for x, _ in points], [y for _, y in points]
    return sum(x * y for x, y in zip(xs, ys[1:] + ys[:1])) - sum(y * x for x, y in zip(xs[1:] + xs[:1], ys)) != 0


def expected_physics(tileset: TilesetInfo, tile: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Tile collision as Godot polygons, tile pixels shifted so the tile centre is the origin (N7): the tile's
    collision shapes, or its whole cell when it has none and properties.walkable is false."""
    half_w, half_h = tileset.tile_size[0] / 2, tileset.tile_size[1] / 2
    shapes = list(tile.get("collision") or [])
    flags = tile.get("properties")
    if not shapes and isinstance(flags, dict) and flags.get("walkable") is False:
        shapes = [{"shape": "rect", "x": 0, "y": 0, "w": tileset.tile_size[0], "h": tileset.tile_size[1]}]
    polygons = []
    for number, shape in enumerate(shapes):
        points = _shape_polygon(shape, f"tileset {tileset.id} tile {tile['index']} collision[{number}]")
        if _has_area(points):
            polygons.append([(x - half_w, y - half_h) for x, y in points])
    return polygons


@dataclass
class TilesetPlan:
    source_id: int
    tileset: TilesetInfo
    texture_path: str
    terrain_set: int | None
    mode: str | None
    inside: int | None


def build_tileset(plans: list[TilesetPlan], tile_size: tuple[int, int]) -> str:
    writer = GdWriter("gd_resource", "TileSet")
    physics = any(expected_physics(plan.tileset, tile) for plan in plans for tile in plan.tileset.tiles.values())
    walkable = any(isinstance(tile.get("properties"), dict) and "walkable" in tile["properties"]
                   for plan in plans for tile in plan.tileset.tiles.values())
    sources = []
    for plan in plans:
        tileset = plan.tileset
        properties: list[tuple[str, Any]] = [
            ("resource_name", tileset.id),
            ("texture", writer.ext_resource("Texture2D", plan.texture_path)),
            ("texture_region_size", vec2i(*tileset.tile_size)),
        ]
        for index in sorted(tileset.tiles):
            tile = tileset.tiles[index]
            ax, ay = tileset.atlas(index)
            key = f"{ax}:{ay}/0"
            properties.append((key, 0))
            terrain = expected_terrain(tileset, tile, plan.inside) if plan.terrain_set is not None else None
            if terrain is not None:
                properties.append((f"{key}/terrain_set", plan.terrain_set))
                properties.append((f"{key}/terrain", terrain["terrain"]))
                properties += [(f"{key}/terrains_peering_bit/{name}", value)
                               for name, value in terrain.items() if name != "terrain"]
            for number, polygon in enumerate(expected_physics(tileset, tile)):
                properties.append((f"{key}/physics_layer_0/polygon_{number}/points", packed_vec2(polygon)))
            flags = tile.get("properties")
            if walkable and isinstance(flags, dict) and isinstance(flags.get("walkable"), bool):
                properties.append((f"{key}/custom_data_0", flags["walkable"]))
        sources.append((plan.source_id, writer.sub_resource("TileSetAtlasSource", properties)))
    resource: list[tuple[str, Any]] = [("tile_size", vec2i(*tile_size))]
    if physics:
        resource.append(("physics_layer_0/collision_layer", 1))
    for plan in plans:
        if plan.terrain_set is None:
            continue
        prefix = f"terrain_set_{plan.terrain_set}"
        resource.append((f"{prefix}/mode", TERRAIN_MODES[plan.mode]))
        for number, material in enumerate(plan.tileset.materials):
            resource.append((f"{prefix}/terrain_{number}/name", material))
            resource.append((f"{prefix}/terrain_{number}/color", _material_color(material)))
    if walkable:
        resource += [("custom_data_layer_0/name", "walkable"), ("custom_data_layer_0/type", VARIANT_BOOL)]
    resource += [(f"sources/{source_id}", sub) for source_id, sub in sources]
    writer.section("[resource]", resource)
    return writer.text()


# --------------------------------------------------------------------------- scene

def _node_name(text: str, taken: set[str]) -> str:
    stem = NODE_NAME_FORBIDDEN.sub("_", str(text)).strip() or "node"
    name, number = stem, 2
    while name in taken:
        name, number = f"{stem}_{number}", number + 1
    taken.add(name)
    return name


def encode_tile_map_data(cells: list[tuple[int, int, int, int, int, int]]) -> list[int]:
    raw = bytearray(struct.pack("<H", 0))
    for cell in sorted(cells, key=lambda item: (item[1], item[0])):
        raw += struct.pack("<hhHHHH", *cell)
    return list(raw)


def layer_cells(layer: TileLayerInfo, source_id: int) -> list[tuple[int, int, int, int, int, int]]:
    rows, cols = np.nonzero(layer.grid >= 0)
    cells = []
    for row, col in zip(rows.tolist(), cols.tolist()):
        ax, ay = layer.tileset.atlas(int(layer.grid[row, col]))
        cells.append((col, row, source_id, ax, ay, 0))
    return cells


def prop_transform(item: dict[str, Any]) -> dict[str, Any]:
    """Sprite2D placement: the node origin sits on (x, sortY) so Godot's y-sort follows sortY; offset puts the
    image so the prop's anchor_px lands on (x, y). With flip_x (D6) the sprite sets flip_h, which mirrors the
    texture inside its own rect, so anchor column ax lands at offset.x + width - ax: offset.x = ax - width."""
    x, y = float(item["x"]), float(item["y"])
    ax, ay = (float(v) for v in item["anchor_px"])
    scale = float(item.get("scale", 1))
    sort_y = float(item.get("sortY", y))
    flip = item.get("flip_x") is True
    offset_x = ax - item["_image"].size[0] if flip else -ax
    return {"position": (x, sort_y), "offset": (offset_x, (y - sort_y) / scale - ay), "scale": scale, "flip": flip}


def _shape_node(writer: GdWriter, shape: dict[str, Any], what: str) -> tuple[str, list[tuple[str, Any]], tuple]:
    """(node type, properties, expected geometry for the parse-back QA) for one solid in world pixels."""
    kind = shape.get("shape")
    if kind == "rect":
        x, y = _number(shape.get("x"), f"{what} x"), _number(shape.get("y"), f"{what} y")
        w, h = _number(shape.get("w"), f"{what} w"), _number(shape.get("h"), f"{what} h")
        sub = writer.sub_resource("RectangleShape2D", [("size", vec2(w, h))])
        centre = (x + w / 2, y + h / 2)
        return "CollisionShape2D", [("position", vec2(*centre)), ("shape", sub)], ("rect", centre, (w, h))
    if kind == "ellipse" and abs(float(shape.get("rx", 0)) - float(shape.get("ry", -1))) <= EPSILON:
        radius = _number(shape.get("rx"), f"{what} rx")
        centre = (_number(shape.get("cx"), f"{what} cx"), _number(shape.get("cy"), f"{what} cy"))
        sub = writer.sub_resource("CircleShape2D", [("radius", radius)])
        return "CollisionShape2D", [("position", vec2(*centre)), ("shape", sub)], ("circle", centre, radius)
    points = _shape_polygon(shape, what)
    return "CollisionPolygon2D", [("polygon", packed_vec2(points))], ("polygon", tuple(points))


def build_scene(bundle: BundleInfo, name: str, plans: dict[str, TilesetPlan], tileset_path: str | None,
                asset_paths: dict[str, str], texture_filter: str) -> tuple[str, dict[str, Any]]:
    """The .tscn text and a description of what each node holds (for QA and the report)."""
    writer = GdWriter("gd_scene")
    root = _node_name(name, set())
    root_props: list[tuple[str, Any]] = []
    if TEXTURE_FILTERS[texture_filter] is not None:
        root_props.append(("texture_filter", TEXTURE_FILTERS[texture_filter]))
    root_props.append(("metadata/map_bundle_sha256", bundle.sha256))
    nodes: list[tuple[str, str, str | None, list[tuple[str, Any]]]] = [(root, "Node2D", None, root_props)]
    top: set[str] = set()
    layout: dict[str, Any] = {"layers": [], "props": {}, "markers": {}, "portals": {}, "interactions": {},
                              "solids": 0, "footprints": 0, "collision": {}}
    tileset_ref = writer.ext_resource("TileSet", tileset_path) if tileset_path else None
    objects_done = False

    def add_props(layer_name: str) -> None:
        props_node = _node_name(layer_name, top)
        nodes.append((props_node, "Node2D", ".", [("y_sort_enabled", True)]))
        taken: set[str] = set()
        for item in bundle.objects:
            child = _node_name(item["id"], taken)
            placed = prop_transform(item)
            properties: list[tuple[str, Any]] = [("position", vec2(*placed["position"]))]
            if abs(placed["scale"] - 1) > EPSILON:
                properties.append(("scale", vec2(placed["scale"], placed["scale"])))
            properties += [("texture", writer.ext_resource("Texture2D", asset_paths[item["_image"].sha256])),
                           ("centered", False), ("offset", vec2(*placed["offset"]))]
            if placed["flip"]:
                properties.append(("flip_h", True))
            properties += [
                           ("metadata/bundle_id", item["id"]), ("metadata/prop", str(item.get("prop", ""))),
                           ("metadata/anchor_world", vec2(item["x"], item["y"])),
                           ("metadata/anchor_px", vec2(*item["anchor_px"]))]
            if "sortY" in item:
                properties.append(("metadata/sort_y", float(item["sortY"])))
            for key, meta in (("footprint", "footprint"), ("solid", "solid"), ("occlusion", "occlusion"),
                              ("contact", "contact"), ("flip_x", "flip_x")):
                if key in item:
                    properties.append((f"metadata/{meta}", _json_to_gd(item[key])))
            nodes.append((child, "Sprite2D", props_node, properties))
            layout["props"][item["id"]] = {"node": f"{props_node}/{child}", **placed,
                                           "anchor": (float(item["x"]), float(item["y"])),
                                           "anchor_px": tuple(float(v) for v in item["anchor_px"]),
                                           "width": item["_image"].size[0],
                                           "texture": asset_paths[item["_image"].sha256]}
        layout["layers"].append({"name": layer_name, "kind": "objects", "node": props_node,
                                 "count": len(bundle.objects)})

    for layer in bundle.layers:
        if layer["kind"] == "tiles":
            info: TileLayerInfo = layer["tiles"]
            plan = plans[info.tileset.id]
            cells = layer_cells(info, plan.source_id)
            node = _node_name(layer["name"], top)
            nodes.append((node, "TileMapLayer", ".", [("tile_map_data", GdCall("PackedByteArray",
                                                                                  tuple(encode_tile_map_data(cells)))),
                                                       ("tile_set", tileset_ref)]))
            layout["layers"].append({"name": layer["name"], "kind": "tiles", "node": node, "cells": cells})
        elif layer["kind"] == "image":
            node = _node_name(layer["name"], top)
            texture = writer.ext_resource("Texture2D", asset_paths[layer["image"].sha256])
            nodes.append((node, "Sprite2D", ".", [("texture", texture), ("centered", False)]))
            layout["layers"].append({"name": layer["name"], "kind": "image", "node": node,
                                     "texture": asset_paths[layer["image"].sha256]})
        elif not objects_done:
            add_props(layer["name"])
            objects_done = True
    if bundle.objects and not objects_done:
        add_props("props")

    collision = bundle.data.get("collision") or {}
    footprints = bundle.blocking.footprints if bundle.blocking is not None else []
    if collision or footprints:
        body = _node_name("collision", top)
        meta: list[tuple[str, Any]] = []
        if collision:
            meta += [("metadata/actor_radius", float(collision.get("actorRadius", 0))),
                     ("metadata/y_squash", float(collision.get("ySquash", 1.0)))]
        if collision.get("walkRegions"):
            meta.append(("metadata/walk_regions", [
                {"polygon": packed_vec2([_point(p, "walk region point") for p in region["polygon"]]),
                 "holes": [packed_vec2([_point(p, "walk region hole point") for p in hole])
                           for hole in region.get("holes") or []]}
                for region in collision["walkRegions"]]))
        nodes.append((body, "StaticBody2D", ".", meta))
        taken: set[str] = set()
        shapes = [(solid.get("id") or f"solid_{number}", solid)
                  for number, solid in enumerate(collision.get("solids") or [])]
        for number, rect in enumerate(collision.get("rects") or []):
            x, y, w, h = (_number(v, f"collision.rects[{number}]") for v in rect)
            shapes.append((f"rect_{number}", {"shape": "rect", "x": x, "y": y, "w": w, "h": h}))
        for ident, solid in shapes:
            if solid.get("shape") == "rect" and (float(solid.get("w", 0)) <= 0 or float(solid.get("h", 0)) <= 0):
                bundle.warnings.append(f"collision {ident} has zero size and was skipped")
                continue
            kind, properties, expected = _shape_node(writer, solid, f"collision {ident}")
            child = _node_name(ident, taken)
            nodes.append((child, kind, body, properties))
            layout["collision"][f"{body}/{child}"] = expected
            layout["solids"] += 1
        # D33: the footprints of solid objects, as forge_nav reads them: scaled once by the instance scale (basis
        # world_px is not), offset and rotation applied, mirrored by flip_x (N6)
        for solid in footprints:
            ident = solid["source"].split(":", 1)[1]
            kind, properties, expected = _shape_node(writer, solid, f"object {ident} footprint")
            child = _node_name(f"footprint_{ident}", taken)
            nodes.append((child, kind, body, properties + [("metadata/object", ident)]))
            layout["collision"][f"{body}/{child}"] = expected
            layout["footprints"] += 1

    spawns, anchors = bundle.data.get("spawns") or [], bundle.data.get("anchors") or {}
    if spawns or anchors:
        group = _node_name("markers", top)
        nodes.append((group, "Node2D", ".", []))
        taken = set()
        for spawn in spawns:
            child = _node_name(spawn["id"], taken)
            properties = [("position", vec2(spawn["x"], spawn["y"])), ("metadata/kind", "spawn")]
            if "facing" in spawn:
                properties.append(("metadata/facing", spawn["facing"]))
            nodes.append((child, "Marker2D", group, properties))
            layout["markers"][f"spawn:{spawn['id']}"] = {"node": f"{group}/{child}",
                                                         "position": (float(spawn["x"]), float(spawn["y"]))}
        for anchor_name, anchor in anchors.items():
            child = _node_name(anchor_name, taken)
            point = _point(anchor.get("point"), f"anchor {anchor_name} point")
            properties = [("position", vec2(*point)), ("metadata/kind", "anchor")]
            for key, meta_key in (("facing", "facing"), ("slots", "slots"), ("approach", "approach")):
                if key in anchor:
                    properties.append((f"metadata/{meta_key}", _json_to_gd(anchor[key])))
            nodes.append((child, "Marker2D", group, properties))
            layout["markers"][f"anchor:{anchor_name}"] = {"node": f"{group}/{child}", "position": point}

    portals = bundle.data.get("portals") or []
    if portals:
        group = _node_name("portals", top)
        nodes.append((group, "Node2D", ".", []))
        taken = set()
        for portal in portals:
            child = _node_name(portal["id"], taken)
            if "rect" in portal:
                x, y, w, h = (_number(v, f"portal {portal['id']} rect") for v in portal["rect"])
                position = (x + w / 2, y + h / 2)
                shape = writer.sub_resource("RectangleShape2D", [("size", vec2(w, h))])
            else:
                cx, cy, radius = (_number(v, f"portal {portal['id']} circle") for v in portal["circle"])
                position = (cx, cy)
                shape = writer.sub_resource("CircleShape2D", [("radius", radius)])
            properties = [("position", vec2(*position)), ("metadata/kind", "portal"), ("metadata/to", portal["to"])]
            for key, meta_key in (("activation", "activation"), ("travelDirection", "travel_direction"),
                                  ("radius", "radius"), ("entranceByFrom", "entrance_by_from"), ("latch", "latch"),
                                  ("requiresMovement", "requires_movement")):
                if key in portal:
                    properties.append((f"metadata/{meta_key}", _json_to_gd(portal[key])))
            nodes.append((child, "Area2D", group, properties))
            nodes.append(("shape", "CollisionShape2D", f"{group}/{child}", [("shape", shape)]))
            layout["portals"][portal["id"]] = {"node": f"{group}/{child}", "position": position}

    interactions = bundle.data.get("interactions") or []
    if interactions:
        group = _node_name("interactions", top)
        nodes.append((group, "Node2D", ".", []))
        taken = set()
        for item in interactions:
            child = _node_name(item["id"], taken)
            properties = [("position", vec2(item["x"], item["y"])), ("metadata/kind", "interaction")]
            reach = float(item.get("reach", 0))
            if reach > 0:
                properties.append(("metadata/reach", reach))
                nodes.append((child, "Area2D", group, properties))
                shape = writer.sub_resource("CircleShape2D", [("radius", reach)])
                nodes.append(("reach", "CollisionShape2D", f"{group}/{child}", [("shape", shape)]))
            else:
                nodes.append((child, "Marker2D", group, properties))
            layout["interactions"][item["id"]] = {"node": f"{group}/{child}",
                                                  "position": (float(item["x"]), float(item["y"]))}

    for node, node_type, parent, properties in nodes:
        header = f"[node name={_gd_string(node)} type=\"{node_type}\""
        if parent is not None:
            header += f" parent={_gd_string(parent)}"
        writer.section(header + "]", properties)
    return writer.text(), layout


# --------------------------------------------------------------------------- QA (parse back)

def _check(ident: str, status: str, value: Any = None, threshold: Any = None) -> dict[str, Any]:
    return {"id": ident, "status": status, "value": value, "threshold": threshold}


def _resources(sections: list[GdSection]) -> tuple[dict[str, GdSection], dict[str, GdSection]]:
    ext = {section.fields["id"]: section for section in sections if section.tag == "ext_resource"}
    sub = {section.fields["id"]: section for section in sections if section.tag == "sub_resource"}
    return ext, sub


def _references(value: Any) -> list[GdCall]:
    if isinstance(value, GdCall):
        found = [value] if value.name in ("ExtResource", "SubResource") else []
        return found + [ref for arg in value.args for ref in _references(arg)]
    if isinstance(value, list):
        return [ref for item in value for ref in _references(item)]
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _references(item)]
    return []


def _dangling(sections: list[GdSection], stage: Path, base: Path) -> list[str]:
    ext, sub = _resources(sections)
    problems = []
    for section in sections:
        for key, value in section.properties.items():
            for ref in _references(value):
                table = ext if ref.name == "ExtResource" else sub
                if ref.args[0] not in table:
                    problems.append(f"{key} refers to missing {ref.name}({ref.args[0]!r})")
    for ident, section in ext.items():
        if not (base / section.fields["path"]).is_file():
            problems.append(f"ext_resource {ident} path {section.fields['path']!r} does not exist")
    return problems


def qa_tileset(text: str, plans: list[TilesetPlan], stage: Path) -> list[dict[str, Any]]:
    sections = parse_godot_text(text)
    problems = []
    if sections[0].tag != "gd_resource" or sections[0].fields.get("type") != "TileSet":
        problems.append("the file is not a TileSet resource")
    problems += _dangling(sections, stage, stage)
    ext, sub = _resources(sections)
    resource = next((section for section in sections if section.tag == "resource"), None)
    terrain_mismatch, physics_mismatch = [], []
    for plan in plans:
        ref = resource.properties.get(f"sources/{plan.source_id}") if resource else None
        source = sub.get(ref.args[0]) if isinstance(ref, GdCall) else None
        if source is None:
            problems.append(f"source {plan.source_id} ({plan.tileset.id}) is missing")
            continue
        texture = ext.get(source.properties.get("texture", GdCall("", ("",))).args[0])
        if texture is None or forge_core.sha256_file(stage / texture.fields["path"]) != plan.tileset.image_sha256:
            problems.append(f"source {plan.tileset.id} texture does not match the tileset image")
        for index, tile in sorted(plan.tileset.tiles.items()):
            ax, ay = plan.tileset.atlas(index)
            key = f"{ax}:{ay}/0"
            if source.properties.get(key) != 0:
                problems.append(f"{plan.tileset.id} tile {index} ({key}) was not created")
                continue
            expected = expected_terrain(plan.tileset, tile, plan.inside) if plan.terrain_set is not None else None
            got = {name: value for name, value in source.properties.items()
                   if name.startswith(f"{key}/terrains_peering_bit/")}
            got = {name.rsplit("/", 1)[1]: value for name, value in got.items()}
            if expected is None:
                if got or f"{key}/terrain_set" in source.properties:
                    terrain_mismatch.append(f"{plan.tileset.id} tile {index} has terrain data it should not")
            else:
                want_bits = {name: value for name, value in expected.items() if name != "terrain"}
                if source.properties.get(f"{key}/terrain_set") != plan.terrain_set \
                        or source.properties.get(f"{key}/terrain") != expected["terrain"] or got != want_bits:
                    terrain_mismatch.append(f"{plan.tileset.id} tile {index}: terrain {got} != {want_bits}")
            polygons = expected_physics(plan.tileset, tile)
            for number, polygon in enumerate(polygons):
                value = source.properties.get(f"{key}/physics_layer_0/polygon_{number}/points")
                flat = [round(c, 4) for point in polygon for c in point]
                if not isinstance(value, GdCall) or [round(float(c), 4) for c in value.args] != flat:
                    physics_mismatch.append(f"{plan.tileset.id} tile {index} polygon {number} differs")
            if f"{key}/physics_layer_0/polygon_{len(polygons)}/points" in source.properties:
                physics_mismatch.append(f"{plan.tileset.id} tile {index} has an extra polygon")
    return [_check("tileset_parses", "fail" if problems else "pass", problems),
            _check("peering_bits_roundtrip", "fail" if terrain_mismatch else "pass", terrain_mismatch),
            _check("physics_polygons_roundtrip", "fail" if physics_mismatch else "pass", physics_mismatch)]


def _collision_mismatch(node: GdSection | None, subs: dict[str, GdSection], expected: tuple) -> str | None:
    """Why a parsed collision node differs from the shape it was written from, or None."""
    if node is None:
        return "node is missing"
    kind = expected[0]
    if kind == "polygon":
        polygon = node.properties.get("polygon")
        flat = [c for point in expected[1] for c in point]
        if node.fields.get("type") != "CollisionPolygon2D" or not isinstance(polygon, GdCall) \
                or len(polygon.args) != len(flat) or max((abs(float(a) - b) for a, b in zip(polygon.args, flat)),
                                                         default=0) > 1e-3:
            return "polygon points differ"
        return None
    position, ref = node.properties.get("position"), node.properties.get("shape")
    shape = subs.get(ref.args[0]) if isinstance(ref, GdCall) else None
    if node.fields.get("type") != "CollisionShape2D" or not isinstance(position, GdCall) or shape is None \
            or max(abs(float(position.args[0]) - expected[1][0]), abs(float(position.args[1]) - expected[1][1])) > 1e-3:
        return "position or shape differs"
    if kind == "rect":
        size = shape.properties.get("size")
        if shape.fields.get("type") != "RectangleShape2D" or not isinstance(size, GdCall) \
                or max(abs(float(size.args[0]) - expected[2][0]), abs(float(size.args[1]) - expected[2][1])) > 1e-3:
            return "rectangle size differs"
    elif shape.fields.get("type") != "CircleShape2D" or abs(float(shape.properties.get("radius", -1)) - expected[2]) > 1e-3:
        return "circle radius differs"
    return None


def qa_scene(text: str, layout: dict[str, Any], stage: Path) -> list[dict[str, Any]]:
    sections = parse_godot_text(text)
    problems = []
    if sections[0].tag != "gd_scene":
        problems.append("the file is not a scene")
    problems += _dangling(sections, stage, stage)
    nodes: dict[str, GdSection] = {}
    for section in sections:
        if section.tag != "node":
            continue
        parent = section.fields.get("parent")
        name = section.fields["name"]
        path = "." if parent is None else (name if parent == "." else f"{parent}/{name}")
        if parent is not None and parent != "." and parent not in nodes:
            problems.append(f"node {name} comes before its parent {parent}")
        if path in nodes:
            problems.append(f"node path {path} is used twice")
        nodes[path] = section
    ext, _ = _resources(sections)
    tile_problems = []
    for layer in layout["layers"]:
        if layer["kind"] != "tiles":
            continue
        node = nodes.get(layer["node"])
        data = node.properties.get("tile_map_data") if node else None
        if not isinstance(data, GdCall) or data.name != "PackedByteArray":
            tile_problems.append(f"layer {layer['name']} has no tile_map_data")
            continue
        decoded = sorted(decode_tile_map_data(data.args))
        if decoded != sorted(layer["cells"]):
            tile_problems.append(f"layer {layer['name']}: {len(decoded)} decoded cells differ from the "
                                 f"{len(layer['cells'])} bundle cells")
    prop_problems = []
    for ident, placed in layout["props"].items():
        node = nodes.get(placed["node"])
        if node is None:
            prop_problems.append(f"prop {ident} node is missing")
            continue
        position = node.properties.get("position")
        offset = node.properties.get("offset")
        scale = node.properties.get("scale", GdCall("Vector2", (1.0, 1.0)))
        texture = ext.get(node.properties.get("texture", GdCall("", ("",))).args[0])
        if not all(isinstance(v, GdCall) for v in (position, offset, scale)) or texture is None:
            prop_problems.append(f"prop {ident} misses position, offset, scale or texture")
            continue
        sx = float(scale.args[0])
        with Image.open(stage / texture.fields["path"]) as image:
            size = image.size
        flipped = node.properties.get("flip_h") is True
        if flipped != placed["flip"]:
            prop_problems.append(f"prop {ident}: flip_h is {flipped}, the bundle's flip_x is {placed['flip']}")
        top_left = (position.args[0] + sx * offset.args[0], position.args[1] + sx * offset.args[1])
        column = size[0] - placed["anchor_px"][0] if flipped else placed["anchor_px"][0]  # flip_h mirrors in place
        anchor = (top_left[0] + sx * column, top_left[1] + sx * placed["anchor_px"][1])
        if max(abs(anchor[0] - placed["anchor"][0]), abs(anchor[1] - placed["anchor"][1])) > 1e-3 \
                or node.properties.get("centered") is not False:
            prop_problems.append(f"prop {ident}: anchor lands at {anchor}, bundle says {placed['anchor']}")
        if not (0 <= placed["anchor_px"][0] <= size[0] and 0 <= placed["anchor_px"][1] <= size[1]):
            prop_problems.append(f"prop {ident}: anchor_px {placed['anchor_px']} lies outside its {size} image")
    _, sub_resources = _resources(sections)
    collision_problems = []
    for path, expected in layout["collision"].items():
        node = nodes.get(path)
        problem = _collision_mismatch(node, sub_resources, expected)
        if problem:
            collision_problems.append(f"{path}: {problem}")
    marker_problems = []
    for group in ("markers", "portals", "interactions"):
        for ident, placed in layout[group].items():
            node = nodes.get(placed["node"])
            position = node.properties.get("position") if node else None
            if not isinstance(position, GdCall) or \
                    max(abs(position.args[0] - placed["position"][0]), abs(position.args[1] - placed["position"][1])) \
                    > 1e-3:
                marker_problems.append(f"{group} {ident} position differs from the bundle")
    return [_check("scene_parses", "fail" if problems else "pass", problems),
            _check("tile_map_data_roundtrip", "fail" if tile_problems else "pass", tile_problems),
            _check("prop_anchors_roundtrip", "fail" if prop_problems else "pass", prop_problems,
                   {"max_error_px": 1e-3}),
            _check("collision_shapes_roundtrip", "fail" if collision_problems else "pass", collision_problems,
                   {"shapes": len(layout["collision"]), "max_error_px": 1e-3}),
            _check("markers_roundtrip", "fail" if marker_problems else "pass", marker_problems,
                   {"max_error_px": 1e-3})]


# --------------------------------------------------------------------------- export

def copy_assets(bundle: BundleInfo, stage: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Copy every image the files use once (by content) into assets/; return sha256 -> stage-relative path."""
    paths: dict[str, str] = {}
    records = []
    taken: set[str] = set()

    def add(role: str, ident: str, info: ImageInfo, folder: str) -> None:
        if info.sha256 in paths:
            return
        relative = f"assets/{folder}/{_local_safe_name(ident, taken)}.png"
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(info.path, target)
        paths[info.sha256] = relative
        records.append({"role": role, "id": ident, "path": relative, "sha256": info.sha256,
                        "source": forge_core.file_ref(info.path, bundle.path.parent, sha256=info.sha256)["path"]})

    for ident in _local_used_tilesets(bundle):
        tileset = bundle.tilesets[ident]
        add("tileset", ident, ImageInfo(tileset.image_path, tileset.image_sha256, tileset.image_size), "tilesets")
    for layer in bundle.layers:
        if layer["kind"] == "image":
            add("layer", layer["name"], layer["image"], "layers")
    for item in bundle.objects:
        add("prop", str(item.get("prop") or item["id"]), item["_image"], "props")
    return paths, records


def export(args: argparse.Namespace) -> dict[str, Any]:
    bundle = _local_read_bundle(Path(args.bundle), [Path(path) for path in args.prop_pack or []])
    name = _local_safe_name(args.name, set(), "map")
    used = _local_used_tilesets(bundle)
    for ident in used:
        tileset = bundle.tilesets[ident]
        if tileset.tile_size != bundle.tile_size:
            raise ValueError(f"tileset {ident} has {tileset.tile_size[0]}x{tileset.tile_size[1]} tiles but the bundle "
                             f"grid is {bundle.tile_size[0]}x{bundle.tile_size[1]}; Godot needs one cell size.")
    warnings = list(bundle.warnings)
    outside = _local_world_warnings(bundle)
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        asset_paths, assets = copy_assets(bundle, stage)
        plans, terrain_sets = [], 0
        for source_id, ident in enumerate(used):
            tileset = bundle.tilesets[ident]
            mode = terrain_mode(tileset)
            if mode is None:
                warnings.append(f"tileset {ident} ({tileset.kind}) has no complete wang or blob_mask data; "
                                f"no terrain set was made")
            inside = blob_inside(tileset, args.blob_inside) if mode == "match_corners_and_sides" else None
            plans.append(TilesetPlan(source_id, tileset, asset_paths[tileset.image_sha256],
                                     terrain_sets if mode else None, mode, inside))
            terrain_sets += 1 if mode else 0
        tileset_name = f"{name}.tileset.tres" if plans else None
        checks = []
        if plans:
            tileset_text = build_tileset(plans, bundle.tile_size)
            (stage / tileset_name).write_text(tileset_text, encoding="utf-8", newline="\n")
            checks += qa_tileset(tileset_text, plans, stage)
        scene_text, layout = build_scene(bundle, name, {plan.tileset.id: plan for plan in plans}, tileset_name,
                                         asset_paths, args.texture_filter)
        warnings += [message for message in bundle.warnings + outside if message not in warnings]
        (stage / f"{name}.tscn").write_text(scene_text, encoding="utf-8", newline="\n")
        checks += qa_scene(scene_text, layout, stage)
        changed = [record["path"] for record in assets
                   if forge_core.sha256_file(stage / record["path"]) != record["sha256"]]
        checks.append(_check("assets_identical", "fail" if changed else "pass", changed))
        checks.append(_check("objects_in_world", "warn" if outside else "pass", outside,
                             {"world": list(bundle.world)}))
        failed = [check["id"] for check in checks if check["status"] == "fail"]
        if failed:
            raise ValueError(f"parse-back QA failed ({', '.join(failed)}): "
                             + "; ".join(str(item) for check in checks if check["status"] == "fail"
                                         for item in (check["value"] or [])[:2]))
        if args.strict_qc and outside:
            raise ValueError(f"strict QC failed (objects_in_world): {outside[0]}; nothing was written.")
        outputs = [forge_core.file_ref(stage / f"{name}.tscn", stage)]
        if tileset_name:
            outputs.append(forge_core.file_ref(stage / tileset_name, stage))
        outputs += [forge_core.file_ref(stage / record["path"], stage, sha256=record["sha256"]) for record in assets]
        status = "warn" if any(check["status"] == "warn" for check in checks) else "pass"
        report = {
            "schema": REPORT_SCHEMA, "tool": dict(TOOL),
            "engine": {"name": "godot", "target": GODOT_TARGET, "format": GODOT_FORMAT,
                       "verified": "parse-level (files re-read by this tool); Godot editor import not run"},
            "bundle": forge_core.file_ref(bundle.path, final, sha256=bundle.sha256),
            "files": {"scene": f"{name}.tscn", "tileset": tileset_name},
            "assets": assets,
            "tilesets": [{"id": plan.tileset.id, "source_id": plan.source_id, "terrain_set": plan.terrain_set,
                          "terrain_mode": plan.mode, "inside_material": None if plan.inside is None else
                          plan.tileset.materials[plan.inside], "tiles": len(plan.tileset.tiles),
                          "physics_polygons": sum(len(expected_physics(plan.tileset, tile))
                                                  for tile in plan.tileset.tiles.values())} for plan in plans],
            "layers": [{key: value for key, value in layer.items() if key != "cells"}
                       | ({"cells": len(layer["cells"])} if "cells" in layer else {}) for layer in layout["layers"]],
            "counts": {"objects": len(bundle.objects), "solids": layout["solids"], "footprints": layout["footprints"],
                       "markers": len(layout["markers"]), "portals": len(layout["portals"]),
                       "interactions": len(layout["interactions"])},
            "notExported": _local_not_exported(bundle),
            "warnings": warnings,
            "qa": {"status": status,
                   "method": "export_godot: read collision through forge_nav (the D2 blocking set), wrote the "
                             "TileSet and scene, re-read both with a Godot text-resource parser and compared tiles "
                             "(decoded tile_map_data), terrain peering bits, physics polygons, prop anchors (flip_h "
                             "included), collision shapes (solids, rects, solid footprints) and marker positions "
                             "with the bundle.",
                   "notProven": list(NOT_PROVEN), "checks": checks,
                   "inputs": [forge_core.file_ref(bundle.path, final, sha256=bundle.sha256)], "outputs": outputs,
                   "tool": dict(TOOL)},
        }
        forge_core.write_json(stage / "godot-export.json", report)
    return {"output_dir": str(final.resolve()), "scene": str((final / f"{name}.tscn").resolve()),
            "tileset": None if tileset_name is None else str((final / tileset_name).resolve()),
            "metadata": str((final / "godot-export.json").resolve()), "status": status,
            "tiles_layers": sum(1 for layer in layout["layers"] if layer["kind"] == "tiles"),
            "objects": len(bundle.objects), "_warnings": warnings}


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="map_bundle.v2 JSON.")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New folder for the Godot files; must not exist.")
    parser.add_argument("--name", default="map", help="Scene and TileSet file stem and root node name (default map).")
    parser.add_argument("--prop-pack", type=Path, action="append",
                        help="prop-pack.json whose accepted labels supply prop images (repeatable).")
    parser.add_argument("--blob-inside", help="Material that is the inside of blob-47 tilesets (default: the last).")
    parser.add_argument("--texture-filter", choices=tuple(TEXTURE_FILTERS), default="inherit",
                        help="Root CanvasItem texture filter: inherit the project setting (default), nearest for "
                             "pixel art, or linear.")
    parser.add_argument("--strict-qc", action="store_true",
                        help="Publish nothing when a QA check warns (for example a prop outside the world).")
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = export(args)
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 when published, 1 on an error (nothing published), 2 on a usage error (D26, D27)."""
    return forge_core.run_cli(_main, argv)


if __name__ == "__main__":
    raise SystemExit(main())
