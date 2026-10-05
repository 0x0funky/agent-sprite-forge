#!/usr/bin/env python3
"""Export a map bundle to Tiled 1.10 JSON (TMJ) with TSX tilesets, and verify an export.

Verbs:
  export  write map.tmj, one TSX per bundle tileset (corner wangset for wang_corner tiles,
          a two-colour mixed wangset for blob-47 tiles, per-tile collision and properties),
          props.tsx (an image-collection tileset: each prop's art with its anchor and footprint),
          copies of every image under images/, preview.png (the bundle's reference render)
          and export-report.json. --embedded-variant adds map.embedded.tmj with the tilesets
          inlined, for Phaser and other loaders that cannot read .tsx. Before anything is
          published the written files are read back with the built-in reader and re-rendered;
          the export is refused unless that render equals the reference render pixel for pixel.
  verify  re-render an exported map with the built-in reader, or with pytiled-parser when it
          is installed, and compare it with a bundle's reference render.

Tiled layers: the bundle's layers in order (tiles layers such as ground and decoration,
image layers, objects layers as prop tile objects with anchor and sortY properties, stored
in draw order with draworder "index" because Tiled's topdown order sorts by image bottom,
not by the ground line), then "collision" (hidden: walk regions, then the bundle's blocking
set as forge_nav reads it (D2): solids, collision rects, solid footprints scaled once (basis
world_px never, D7) and mirrored with flip_x (D6), then the material areas) and
"interactions" (spawns, portals, interactions, anchors). Tile collision lives in the TSX
tilesets (D5). Object art follows the D6 lookup order (objects[].image, props registry,
prop_packs by label, occluder.source); a flip_x object is a tile object with Tiled's
horizontal flip flag. The Tiled GUI, its terrain brushes and Phaser are not verified here.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)
import map_bundle  # noqa: E402
from map_bundle import BundleError  # noqa: E402

TOOL_NAME = "export_tiled.py"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # D29
TILED_FORMAT = "1.10"
TILED_APP = "1.11.0"
REPORT_SCHEMA = "generate2dmap.tiled_export.v1"
RESERVED_LAYERS = ("collision", "interactions")
FLIP_FLAGS = 0xF0000000
FLIPPED_HORIZONTALLY = 0x80000000  # Tiled's gid flag: the tile object is drawn mirrored left to right
WANG_COLOURS = ("#5aa344", "#3b5dc9", "#c79d62", "#9a9a9a", "#d04e4e", "#8e5bd0", "#e0c040", "#40c0c0")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ExportError(BundleError):
    """The export failed its checks; nothing was published."""


# --------------------------------------------------------------------------- Tiled building blocks

def tiled_property(name: str, value: Any) -> dict:
    """One Tiled custom property; lists and objects become JSON strings."""
    if isinstance(value, bool):
        kind = "bool"
    elif isinstance(value, int):
        kind = "int"
    elif isinstance(value, float):
        kind = "float"
    else:
        kind = "string"
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {"name": name, "type": kind, "value": value}


def _properties(pairs: list[tuple[str, Any]]) -> list[dict]:
    return [tiled_property(name, value) for name, value in pairs if value is not None]


def tiled_shape(solid: dict, object_id: int, kind: str, name: str = "", properties: list[dict] | None = None) -> dict:
    """A bundle solid (rect, ellipse or polygon) as a Tiled object. Tiled rotates rectangles
    and ellipses clockwise about their top-left corner, so a rotated ellipse's corner is
    moved to keep its centre."""
    obj: dict[str, Any] = {"id": object_id, "name": name, "type": kind, "rotation": 0, "visible": True}
    if solid["shape"] == "rect":
        obj.update(x=solid["x"], y=solid["y"], width=solid["w"], height=solid["h"])
    elif solid["shape"] == "ellipse":
        rx, ry, rotate = solid["rx"], solid["ry"], float(solid.get("rotate", 0) or 0)
        x, y = solid["cx"] - rx, solid["cy"] - ry
        if rotate:
            theta = math.radians(rotate)
            x = solid["cx"] - (rx * math.cos(theta) - ry * math.sin(theta))
            y = solid["cy"] - (rx * math.sin(theta) + ry * math.cos(theta))
        obj.update(x=x, y=y, width=2 * rx, height=2 * ry, rotation=rotate, ellipse=True)
    else:
        x0, y0 = solid["points"][0]
        obj.update(x=x0, y=y0, width=0, height=0,
                   polygon=[{"x": px - x0, "y": py - y0} for px, py in solid["points"]])
    if properties:
        obj["properties"] = properties
    return obj


def object_group(layer_id: int, name: str, objects: list[dict], *, visible: bool = True,
                 properties: list[dict] | None = None) -> dict:
    group = {"draworder": "index", "id": layer_id, "name": name, "objects": objects, "opacity": 1,
             "type": "objectgroup", "visible": visible, "x": 0, "y": 0}
    if properties:
        group["properties"] = properties
    return group


def _num(value: Any) -> str:
    """Numbers in TSX attributes: integers without a decimal point, floats with repr()."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _properties_xml(properties: list[dict], indent: str) -> list[str]:
    if not properties:
        return []
    lines = [f"{indent}<properties>"]
    for prop in properties:
        kind = "" if prop["type"] == "string" else f' type="{prop["type"]}"'
        value = _num(prop["value"]) if prop["type"] != "string" else prop["value"]
        lines.append(f'{indent} <property name={quoteattr(prop["name"])}{kind} value={quoteattr(str(value))}/>')
    lines.append(f"{indent}</properties>")
    return lines


def _object_xml(obj: dict, indent: str) -> list[str]:
    attrs = f'id="{obj["id"]}"'
    if obj.get("name"):
        attrs += f' name={quoteattr(obj["name"])}'
    if obj.get("type"):
        attrs += f' type={quoteattr(obj["type"])}'
    attrs += f' x="{_num(obj["x"])}" y="{_num(obj["y"])}"'
    if "polygon" not in obj:
        attrs += f' width="{_num(obj["width"])}" height="{_num(obj["height"])}"'
    if obj.get("rotation"):
        attrs += f' rotation="{_num(obj["rotation"])}"'
    children = _properties_xml(obj.get("properties", []), indent + " ")
    if obj.get("ellipse"):
        children.append(f"{indent} <ellipse/>")
    if "polygon" in obj:
        points = " ".join(f'{_num(p["x"])},{_num(p["y"])}' for p in obj["polygon"])
        children.append(f'{indent} <polygon points="{points}"/>')
    if not children:
        return [f"{indent}<object {attrs}/>"]
    return [f"{indent}<object {attrs}>", *children, f"{indent}</object>"]


def tileset_xml(tileset: dict) -> str:
    """The TSX text of a tileset given in Tiled's JSON tileset form."""
    attrs = (f'version="{TILED_FORMAT}" tiledversion="{TILED_APP}" name={quoteattr(tileset["name"])} '
             f'tilewidth="{tileset["tilewidth"]}" tileheight="{tileset["tileheight"]}" '
             f'tilecount="{tileset["tilecount"]}" columns="{tileset["columns"]}"')
    if "objectalignment" in tileset:
        attrs += f' objectalignment="{tileset["objectalignment"]}"'
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f"<tileset {attrs}>"]
    if "grid" in tileset:
        grid = tileset["grid"]
        lines.append(f' <grid orientation="{grid["orientation"]}" width="{grid["width"]}" height="{grid["height"]}"/>')
    lines += _properties_xml(tileset.get("properties", []), " ")
    if "image" in tileset:
        lines.append(f' <image source={quoteattr(tileset["image"])} width="{tileset["imagewidth"]}" '
                     f'height="{tileset["imageheight"]}"/>')
    for tile in tileset.get("tiles", []):
        tile_attrs = f'id="{tile["id"]}"' + (f' type={quoteattr(tile["type"])}' if tile.get("type") else "")
        lines.append(f" <tile {tile_attrs}>")
        lines += _properties_xml(tile.get("properties", []), "  ")
        if "image" in tile:
            lines.append(f'  <image source={quoteattr(tile["image"])} width="{tile["imagewidth"]}" '
                         f'height="{tile["imageheight"]}"/>')
        if "objectgroup" in tile:
            lines.append('  <objectgroup draworder="index" id="2">')
            for obj in tile["objectgroup"]["objects"]:
                lines += _object_xml(obj, "   ")
            lines.append("  </objectgroup>")
        lines.append(" </tile>")
    if tileset.get("wangsets"):
        lines.append(" <wangsets>")
        for wangset in tileset["wangsets"]:
            lines.append(f'  <wangset name={quoteattr(wangset["name"])} type="{wangset["type"]}" '
                         f'tile="{wangset["tile"]}">')
            for colour in wangset["colors"]:
                lines.append(f'   <wangcolor name={quoteattr(colour["name"])} color="{colour["color"]}" '
                             f'tile="{colour["tile"]}" probability="{_num(colour["probability"])}"/>')
            for wangtile in wangset["wangtiles"]:
                wangid = ",".join(str(v) for v in wangtile["wangid"])
                lines.append(f'   <wangtile tileid="{wangtile["tileid"]}" wangid="{wangid}"/>')
            lines.append("  </wangset>")
        lines.append(" </wangsets>")
    lines.append("</tileset>")
    return "\n".join(lines) + "\n"


def corner_wangid(wang: list[int]) -> list[int]:
    """tileset_v1 wang [top_left, top_right, bottom_left, bottom_right] (material indices) as a
    Tiled corner wangid: top, top-right, right, bottom-right, bottom, bottom-left, left,
    top-left with 1-based colours (0 = unused edge)."""
    top_left, top_right, bottom_left, bottom_right = wang
    return [0, top_right + 1, 0, bottom_right + 1, 0, bottom_left + 1, 0, top_left + 1]


def blob_wangid(mask: int) -> list[int]:
    """tileset_v1 blob_mask (bit i = neighbour i of N, NE, E, SE, S, SW, W, NW, the order of a
    Tiled wangid) as a two-colour mixed wangid: 2 where the neighbour belongs to the blob,
    1 (the outside colour) elsewhere. Two colours keep an isolated tile from an all-zero id."""
    return [2 if mask >> bit & 1 else 1 for bit in range(8)]


# --------------------------------------------------------------------------- export

@dataclass
class _Copied:
    rel: str
    size: tuple[int, int]


class _Exporter:
    def __init__(self, bundle: map_bundle.Bundle, stage: Path) -> None:
        self.bundle = bundle
        self.stage = stage
        self.images: dict[Path, _Copied] = {}
        self.names: set[str] = set()
        self.next_object_id = 1
        self.next_layer_id = 1
        self.tilesets: list[tuple[str, dict, int]] = []  # (tsx file name, JSON tileset, firstgid)
        self.firstgid: dict[str, int] = {}
        self.prop_gid: dict[tuple[str, str], int] = {}  # (prop id, art path) -> gid

    # -- helpers

    def unique(self, stem: str, suffix: str) -> str:
        base = _SAFE.sub("_", stem).strip("._") or "file"
        name, n = f"{base}{suffix}", 2
        while name.lower() in self.names:
            name, n = f"{base}-{n}{suffix}", n + 1
        self.names.add(name.lower())
        return name

    def copy_image(self, source: Path) -> _Copied:
        """Copy an image under images/ once, keeping its bytes and extension."""
        if source not in self.images:
            folder = self.stage / "images"
            folder.mkdir(exist_ok=True)
            name = self.unique(source.stem, source.suffix.lower())
            shutil.copyfile(source, folder / name)
            with Image.open(source) as handle:
                size = handle.size
            self.images[source] = _Copied(f"images/{name}", size)
        return self.images[source]

    def object_id(self) -> int:
        self.next_object_id += 1
        return self.next_object_id - 1

    def layer_id(self) -> int:
        self.next_layer_id += 1
        return self.next_layer_id - 1

    # -- tilesets

    def terrain_tileset(self, tileset: map_bundle.Tileset) -> dict:
        copied = self.copy_image(tileset.image)
        tiles = []
        corner, mixed = [], []
        for index in sorted(tileset.tiles):
            entry = tileset.tiles[index]
            pairs: list[tuple[str, Any]] = [("walkable", (entry.get("properties") or {}).get("walkable"))]
            extra = sorted((entry.get("properties") or {}).items())
            pairs += [(key, value) for key, value in extra if key != "walkable"]
            pairs += [("variant", entry.get("variant")), ("blobMask", entry.get("blob_mask")),
                      ("wang", None if "wang" not in entry else ",".join(str(v) for v in entry["wang"]))]
            tile: dict[str, Any] = {"id": index}
            properties = _properties(pairs)
            if properties:
                tile["properties"] = properties
            shapes = list(entry.get("collision") or [])
            if not shapes and (entry.get("properties") or {}).get("walkable") is False:
                shapes = [{"shape": "rect", "x": 0, "y": 0, "w": tileset.tile_w, "h": tileset.tile_h}]
            if shapes:
                objects = [tiled_shape(shape, k + 1, "solid") for k, shape in enumerate(shapes)]
                tile["objectgroup"] = object_group(2, "", objects)
            if len(tile) > 1:
                tiles.append(tile)
            if "wang" in entry:
                corner.append({"tileid": index, "wangid": corner_wangid(entry["wang"])})
            if "blob_mask" in entry:
                mixed.append({"tileid": index, "wangid": blob_wangid(int(entry["blob_mask"]))})
        wangsets = []
        materials = tileset.materials
        if corner:
            wangsets.append({"name": tileset.id, "type": "corner", "tile": -1,
                             "colors": [_colour(name, i) for i, name in enumerate(materials)], "wangtiles": corner})
        if mixed:
            outside = materials[0] if len(materials) > 1 else "outside"
            wangsets.append({"name": f"{tileset.id}-blob", "type": "mixed", "tile": -1,
                             "colors": [_colour(outside, 0), _colour(materials[-1], 1)], "wangtiles": mixed})
        result: dict[str, Any] = {
            "name": tileset.id, "type": "tileset", "version": TILED_FORMAT, "tiledversion": TILED_APP,
            "tilewidth": tileset.tile_w, "tileheight": tileset.tile_h, "tilecount": tileset.tile_count,
            "columns": tileset.columns, "margin": 0, "spacing": 0, "image": copied.rel,
            "imagewidth": copied.size[0], "imageheight": copied.size[1],
            "properties": _properties([("kind", tileset.kind), ("materials", tileset.materials),
                                       ("seamlessVerified", bool(tileset.doc.get("seamless_verified")))]),
        }
        if tiles:
            result["tiles"] = tiles
        if wangsets:
            result["wangsets"] = wangsets
        return result

    def props_tileset(self, keys: list[tuple[str, str]]) -> dict:
        """One image tile per (prop id, art path) the objects draw (the D6 art lookup), with its anchor
        and, for art from the props registry, the prop's footprint, solid and occlusion fields."""
        tiles = []
        sizes = []
        for k, (prop_id, art) in enumerate(keys):
            art_path = Path(art)
            prop = self.bundle.props.get(prop_id)
            first = next(obj for obj in self.bundle.objects if obj.prop == prop_id and obj.image == art_path)
            copied = self.copy_image(art_path)
            sizes.append(first.image_size)
            registry_art = prop is not None and prop.image == art_path
            if registry_art:
                anchor = prop.anchor_px or (prop.size[0] / 2, float(prop.size[1]))
            else:
                anchor = first.anchor_px
            tile: dict[str, Any] = {
                "id": k, "type": "prop", "image": copied.rel, "imagewidth": copied.size[0],
                "imageheight": copied.size[1],
                "properties": _properties([("prop", prop_id), ("anchorX", float(anchor[0])),
                                           ("anchorY", float(anchor[1])),
                                           ("solid", prop.solid if registry_art else None),
                                           ("occlusionClass", prop.occlusion_class if registry_art else None),
                                           ("occupantPolicy", prop.occupant_policy if registry_art else None)]),
            }
            if registry_art:
                unit = map_bundle.MapObject(id=prop_id, prop=prop_id, x=float(anchor[0]), y=float(anchor[1]),
                                            scale=1.0, anchor_px=anchor, footprint=prop.footprint, solid=True,
                                            sort_y=0.0, layer=None, occlusion=None, occupant_policy=None)
                footprint = map_bundle.footprint_solid(unit)
                if footprint is not None:
                    tile["objectgroup"] = object_group(2, "", [tiled_shape(footprint, 1, "footprint", "footprint")])
            tiles.append(tile)
        return {"name": "props", "type": "tileset", "version": TILED_FORMAT, "tiledversion": TILED_APP,
                "tilewidth": max(size[0] for size in sizes), "tileheight": max(size[1] for size in sizes),
                "tilecount": len(keys), "columns": 0, "margin": 0, "spacing": 0, "objectalignment": "bottomleft",
                "grid": {"orientation": "orthogonal", "width": 1, "height": 1}, "tiles": tiles}

    def add_tileset(self, file_stem: str, tileset: dict) -> int:
        firstgid = 1 + sum(ts["tilecount"] for _, ts, _ in self.tilesets)
        self.tilesets.append((self.unique(file_stem, ".tsx"), tileset, firstgid))
        return firstgid

    # -- layers

    def tile_layer(self, layer: map_bundle.Layer) -> dict:
        grid = layer.grid
        firstgid = self.firstgid.get(layer.tileset or "")
        tileset = self.bundle.tilesets.get(layer.tileset or "")
        if firstgid is None or tileset is None:
            data = np.zeros(grid.shape, np.int64)
        else:
            usable = (grid >= 0) & (grid < tileset.tile_count)
            data = np.where(usable, grid + firstgid, 0)
        return {"data": [int(v) for v in data.ravel()], "height": int(grid.shape[0]), "id": self.layer_id(),
                "name": layer.name, "opacity": 1, "type": "tilelayer", "visible": True,
                "width": int(grid.shape[1]), "x": 0, "y": 0}

    def image_layer(self, layer: map_bundle.Layer) -> dict:
        copied = self.copy_image(layer.image)
        return {"id": self.layer_id(), "image": copied.rel, "imageheight": copied.size[1], "imagewidth": copied.size[0],
                "name": layer.name, "offsetx": layer.offset[0], "offsety": layer.offset[1], "opacity": 1,
                "repeatx": False, "repeaty": False, "type": "imagelayer", "visible": True, "x": 0, "y": 0}

    def objects_layer(self, layer: map_bundle.Layer) -> dict:
        objects = []
        for obj in map_bundle.draw_order([o for o in self.bundle.objects if o.layer == layer.name]):
            key = (obj.prop, str(obj.image))
            if obj.image is None or obj.image_size is None or key not in self.prop_gid:
                continue
            left, bottom, width, height = map_bundle.object_placement(obj, obj.image_size)
            gid = self.prop_gid[key] | (FLIPPED_HORIZONTALLY if obj.flip_x else 0)
            objects.append({
                "gid": gid, "height": height, "id": self.object_id(), "name": obj.id,
                "properties": _properties([("prop", obj.prop), ("anchorWorldX", obj.x), ("anchorWorldY", obj.y),
                                           ("sortY", obj.sort_y), ("scale", obj.scale), ("solid", obj.solid),
                                           ("occlusion", obj.occlusion), ("occupantPolicy", obj.occupant_policy),
                                           ("flipX", True if obj.flip_x else None)]),
                "rotation": 0, "type": "prop", "visible": True, "width": width, "x": left, "y": bottom})
        return object_group(self.layer_id(), layer.name, objects,
                            properties=_properties([("sortBy", "sortY, then x, then id (ground line)")]))

    def collision_layer(self) -> dict:
        """Walk regions, then the D2 blocking set as forge_nav reads the bundle (solids, rects and
        solid footprints; tile collision stays in the TSX tilesets, D5), then the material areas."""
        bundle, objects = self.bundle, []
        collision = bundle.collision
        if collision is not None:
            blocking = map_bundle.blocking_set(bundle)
            for i, (polygon, holes) in enumerate(collision.regions):
                region = {"shape": "polygon", "points": polygon.tolist()}
                objects.append(tiled_shape(region, self.object_id(), "walkRegion", f"region-{i}"))
                for k, hole in enumerate(holes):
                    objects.append(tiled_shape({"shape": "polygon", "points": hole.tolist()}, self.object_id(),
                                               "walkHole", f"region-{i}-hole-{k}", _properties([("region", i)])))
            for solid in blocking.collision_solids + blocking.rects:
                objects.append(tiled_shape(solid, self.object_id(), "solid", solid["source"],
                                           _properties([("source", solid["source"])])))
            footprints = blocking.footprints
        else:  # no collision block, so no forge_nav model: the footprints follow the same rule (N6)
            footprints = [solid for solid in map(map_bundle.footprint_solid, bundle.objects) if solid is not None]
        for solid in footprints:
            objects.append(tiled_shape(solid, self.object_id(), "solid", solid["source"].split(":", 1)[1],
                                       _properties([("source", solid["source"])])))
        material = bundle.material
        if material is not None:
            for i, (name, klass, walkable) in enumerate(zip(material.names, material.classes, material.walkable)):
                if klass == "decor":
                    continue
                for x, y, w, h in map_bundle.merge_rects(material.index == i):
                    s = material.scale
                    objects.append(tiled_shape({"shape": "rect", "x": x * s, "y": y * s, "w": w * s, "h": h * s},
                                               self.object_id(), klass, name,
                                               _properties([("material", name), ("walkable", walkable)])))
        properties = []
        if collision is not None:
            properties = _properties([("actorRadius", collision.actor_radius), ("ySquash", collision.y_squash),
                                      ("navCell", map_bundle.nav_cell(collision.actor_radius)),
                                      ("semantics", "plan Appendix C; tile collision lives in the tilesets")])
        return object_group(self.layer_id(), "collision", objects, visible=False, properties=properties)

    def interactions_layer(self) -> dict:
        bundle, objects = self.bundle, []
        for spawn in bundle.doc.get("spawns", []):
            objects.append({"height": 0, "id": self.object_id(), "name": spawn["id"], "point": True,
                            "properties": _properties([("facing", spawn.get("facing"))]), "rotation": 0,
                            "type": "spawn", "visible": True, "width": 0, "x": spawn["x"], "y": spawn["y"]})
        for portal, raw in zip(bundle.portals, bundle.doc.get("portals", [])):
            if portal.rect is not None:
                x, y, w, h = portal.rect
                shape = {"shape": "rect", "x": x, "y": y, "w": w, "h": h}
            else:
                cx, cy, r = portal.circle
                shape = {"shape": "ellipse", "cx": cx, "cy": cy, "rx": r, "ry": r}
            travel = portal.travel or (None, None)
            pairs = [("to", raw["to"]), ("activation", portal.activation),
                     ("radius", portal.radius if portal.activation == "intent" else None),
                     ("travelDirectionX", travel[0]), ("travelDirectionY", travel[1]), ("latch", portal.latch),
                     ("requiresMovement", portal.requires_movement), ("entranceByFrom", raw.get("entranceByFrom"))]
            objects.append(tiled_shape(shape, self.object_id(), "portal", portal.id, _properties(pairs)))
        for entry in bundle.interactions:
            objects.append({"height": 0, "id": self.object_id(), "name": entry["id"], "point": True,
                            "properties": _properties([("reach", entry["reach"])]), "rotation": 0,
                            "type": "interaction", "visible": True, "width": 0, "x": entry["x"], "y": entry["y"]})
        for name, anchor in bundle.anchors.items():
            pairs = [("facing", anchor["facing"]), ("slots", [list(p) for p in anchor["slots"]] or None),
                     ("approach", [list(p) for p in anchor["approach"]] or None)]
            objects.append({"height": 0, "id": self.object_id(), "name": name, "point": True,
                            "properties": _properties(pairs), "rotation": 0, "type": "anchor", "visible": True,
                            "width": 0, "x": anchor["point"][0], "y": anchor["point"][1]})
        return object_group(self.layer_id(), "interactions", objects)

    # -- the map

    def build(self) -> tuple[dict, dict]:
        """Write images and return (map with external tilesets, map with embedded tilesets)."""
        bundle = self.bundle
        clash = [layer.name for layer in bundle.layers if layer.name in RESERVED_LAYERS]
        if clash:
            raise ExportError(f"layer name(s) {clash} are reserved for the Tiled export; rename them in the bundle")
        for tileset in bundle.tilesets.values():
            self.firstgid[tileset.id] = self.add_tileset(tileset.id, self.terrain_tileset(tileset))
        used = sorted({(obj.prop, str(obj.image)) for obj in bundle.objects
                       if obj.image is not None and obj.image_size is not None})
        if used:
            firstgid = self.add_tileset("props", self.props_tileset(used))
            self.prop_gid = {key: firstgid + k for k, key in enumerate(used)}
        layers = []
        for layer in bundle.layers:
            if layer.kind == "tiles" and layer.grid is not None:
                layers.append(self.tile_layer(layer))
            elif layer.kind == "image" and layer.image is not None:
                layers.append(self.image_layer(layer))
            elif layer.kind == "objects":
                layers.append(self.objects_layer(layer))
        layers += [self.collision_layer(), self.interactions_layer()]
        tile_w, tile_h = bundle.tile_w or 16, bundle.tile_h or 16
        world_w, world_h = map_bundle.canvas_size(bundle)
        pairs = [("mapId", bundle.id), ("bundleSchema", bundle.raw.get("schema")), ("worldWidth", bundle.width),
                 ("worldHeight", bundle.height), ("generator", f"generate2dmap {TOOL_NAME} {TOOL_VERSION}")]
        if bundle.collision is not None:
            pairs += [("actorRadius", bundle.collision.actor_radius), ("ySquash", bundle.collision.y_squash)]
        base = {"compressionlevel": -1, "height": math.ceil(world_h / tile_h), "infinite": False, "layers": layers,
                "nextlayerid": self.next_layer_id, "nextobjectid": self.next_object_id, "orientation": "orthogonal",
                "properties": _properties(pairs), "renderorder": "right-down", "tiledversion": TILED_APP,
                "tileheight": tile_h, "tilesets": [], "tilewidth": tile_w, "type": "map", "version": TILED_FORMAT,
                "width": math.ceil(world_w / tile_w)}
        external = dict(base, tilesets=[{"firstgid": gid, "source": name} for name, _, gid in self.tilesets])
        embedded = dict(base, tilesets=[{"firstgid": gid, **tileset} for _, tileset, gid in self.tilesets])
        return external, embedded


def _colour(name: str, index: int) -> dict:
    return {"name": name, "color": WANG_COLOURS[index % len(WANG_COLOURS)], "tile": -1, "probability": 1}


# --------------------------------------------------------------------------- the built-in reader

def read_tiled_map(tmj: str | os.PathLike) -> dict:
    """Read a TMJ map and its tilesets (external TSX or embedded JSON) into plain dicts:
    {"map": TMJ document, "tilesets": [{"firstgid", "base", "columns", "tilewidth",
    "tileheight", "tilecount", "image", "tiles": {id: {"image", ...}}}, ...]} sorted by firstgid."""
    tmj = Path(tmj).resolve()
    document = map_bundle.read_json(tmj)
    tilesets = []
    for entry in document["tilesets"]:
        if "source" in entry:
            path = (tmj.parent / entry["source"]).resolve()
            tileset, base = _read_tsx(path), path.parent
        else:
            tileset, base = entry, tmj.parent
        tilesets.append({"firstgid": int(entry["firstgid"]), "base": base, "columns": int(tileset["columns"]),
                         "tilewidth": int(tileset["tilewidth"]), "tileheight": int(tileset["tileheight"]),
                         "tilecount": int(tileset["tilecount"]), "image": tileset.get("image"),
                         "tiles": {int(t["id"]): t for t in tileset.get("tiles", [])}})
    return {"path": tmj, "map": document, "tilesets": sorted(tilesets, key=lambda ts: ts["firstgid"])}


def _read_tsx(path: Path) -> dict:
    root = ET.parse(path).getroot()
    if root.tag != "tileset":
        raise BundleError(f"{path.name} is not a TSX tileset")
    keys = ("name", "tilewidth", "tileheight", "tilecount", "columns")
    tileset: dict[str, Any] = {key: root.get(key) for key in keys}
    image = root.find("image")
    if image is not None:
        tileset["image"] = image.get("source")
    tiles = []
    for tile in root.findall("tile"):
        entry: dict[str, Any] = {"id": tile.get("id")}
        tile_image = tile.find("image")
        if tile_image is not None:
            entry.update(image=tile_image.get("source"), imagewidth=int(tile_image.get("width")),
                         imageheight=int(tile_image.get("height")))
        tiles.append(entry)
    tileset["tiles"] = tiles
    return tileset


class _GidImages:
    """Tile images by gid, loaded once (atlas cells cropped, collection images whole)."""

    def __init__(self, tiled: dict) -> None:
        self.tilesets = tiled["tilesets"]
        self._atlases: dict[int, np.ndarray] = {}
        self._images: dict[int, Image.Image] = {}

    def owner(self, gid: int) -> dict:
        if gid & FLIP_FLAGS:
            raise BundleError(f"gid {gid} uses flip flags, which this reader supports only as the horizontal "
                              "flip of a tile object")
        candidates = [ts for ts in self.tilesets if ts["firstgid"] <= gid]
        if not candidates or gid - candidates[-1]["firstgid"] >= candidates[-1]["tilecount"]:
            raise BundleError(f"gid {gid} belongs to no tileset")
        return candidates[-1]

    def atlas_stack(self, tileset: dict) -> np.ndarray:
        key = tileset["firstgid"]
        if key not in self._atlases:
            atlas = np.asarray(forge_core.load_rgba(tileset["base"] / tileset["image"])[0])
            tw, th, cols = tileset["tilewidth"], tileset["tileheight"], tileset["columns"]
            rows = tileset["tilecount"] // cols
            cells = atlas[: rows * th, : cols * tw].reshape(rows, th, cols, tw, 4).transpose(0, 2, 1, 3, 4)
            stack = cells.reshape(rows * cols, th, tw, 4)
            self._atlases[key] = np.concatenate([stack, np.zeros((1, th, tw, 4), np.uint8)])
        return self._atlases[key]

    def image(self, gid: int) -> Image.Image:
        if gid not in self._images:
            tileset = self.owner(gid)
            local = gid - tileset["firstgid"]
            if tileset.get("image"):
                self._images[gid] = Image.fromarray(self.atlas_stack(tileset)[local])
            else:
                self._images[gid] = forge_core.load_rgba(tileset["base"] / tileset["tiles"][local]["image"])[0]
        return self._images[gid]


def _object_gid(raw: int) -> tuple[int, bool]:
    """(gid, mirrored) of a tile object's gid: the horizontal flip flag (a flip_x object, D6) is
    supported; vertical, diagonal and hexagonal flags are refused."""
    if raw & (FLIP_FLAGS & ~FLIPPED_HORIZONTALLY):
        raise BundleError(f"gid {raw} uses vertical, diagonal or hexagonal flip flags, which this reader does not "
                          "support")
    return raw & ~FLIP_FLAGS, bool(raw & FLIPPED_HORIZONTALLY)


def _mirrored(image: Image.Image, mirrored: bool) -> Image.Image:
    return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT) if mirrored else image


def render_tiled(tiled: dict, size: tuple[int, int]) -> np.ndarray:
    """Re-render a map read by read_tiled_map(): visible tile, image and tile-object layers,
    objects in draworder (index = file order, topdown = by y), tile objects drawn at
    (round(x), round(y - height)) scaled to (round(width), round(height)) with nearest neighbour,
    mirrored left to right when the gid carries the horizontal flip flag."""
    document = tiled["map"]
    gids = _GidImages(tiled)
    tw, th = int(document["tilewidth"]), int(document["tileheight"])
    canvas = Image.new("RGBA", (document["width"] * tw, document["height"] * th), (0, 0, 0, 0))
    for layer in document["layers"]:
        if not layer.get("visible", True):
            continue
        if layer["type"] == "tilelayer":
            data = np.asarray(layer["data"], np.int64).reshape(layer["height"], layer["width"])
            layer_image = np.zeros((layer["height"] * th, layer["width"] * tw, 4), np.uint8)
            for tileset in tiled["tilesets"]:
                local = data - tileset["firstgid"]
                mine = (data > 0) & (local >= 0) & (local < tileset["tilecount"])
                if not mine.any():
                    continue
                if not tileset.get("image") or (tileset["tilewidth"], tileset["tileheight"]) != (tw, th):
                    raise BundleError("tile layers must use atlas tilesets of the map's tile size")
                stack = gids.atlas_stack(tileset)
                index = np.where(mine, local, tileset["tilecount"])
                cells = stack[index].transpose(0, 2, 1, 3, 4).reshape(layer_image.shape)
                layer_image = np.where(np.repeat(np.repeat(mine, th, 0), tw, 1)[..., None], cells, layer_image)
            for gid in np.unique(data[data > 0]):
                gids.owner(int(gid))  # rejects gids outside every tileset and flip flags
            map_bundle.composite(canvas, Image.fromarray(layer_image), 0, 0)
        elif layer["type"] == "imagelayer":
            image = forge_core.load_rgba(tiled["path"].parent / layer["image"])[0]
            offset_x, offset_y = layer.get("offsetx", 0), layer.get("offsety", 0)
            map_bundle.composite(canvas, image, forge_core.round_half_up(offset_x), forge_core.round_half_up(offset_y))
        elif layer["type"] == "objectgroup":
            objects = [o for o in layer["objects"] if o.get("gid") and o.get("visible", True)]
            if layer.get("draworder", "topdown") == "topdown":
                objects = sorted(objects, key=lambda o: o["y"])
            for obj in objects:
                gid, mirrored = _object_gid(int(obj["gid"]))
                map_bundle.draw_image(canvas, _mirrored(gids.image(gid), mirrored), obj["x"], obj["y"], obj["width"],
                                      obj["height"])
    return np.asarray(canvas)[: size[1], : size[0]]


def render_pytiled(tmj: str | os.PathLike, size: tuple[int, int]) -> np.ndarray:
    """The same re-render from pytiled-parser's model of the files (an independent parser)."""
    forge_core.require_modules(["pytiled_parser"])
    import pytiled_parser as tp

    tmj = Path(tmj).resolve()
    tiled_map = tp.parse_map(tmj)
    tw, th = tiled_map.tile_size.width, tiled_map.tile_size.height
    canvas = Image.new("RGBA", (tiled_map.map_size.width * tw, tiled_map.map_size.height * th), (0, 0, 0, 0))
    firstgids = sorted(tiled_map.tilesets)
    cache: dict[int, Image.Image] = {}

    def tile_image(gid: int) -> Image.Image:
        if gid not in cache:
            first = max(g for g in firstgids if g <= gid)
            tileset, local = tiled_map.tilesets[first], gid - first
            if tileset.image is not None:
                atlas = forge_core.load_rgba(tmj.parent / tileset.image)[0]
                col, row = local % tileset.columns, local // tileset.columns
                cache[gid] = atlas.crop((col * tileset.tile_width, row * tileset.tile_height,
                                         (col + 1) * tileset.tile_width, (row + 1) * tileset.tile_height))
            else:
                cache[gid] = forge_core.load_rgba(tmj.parent / tileset.tiles[local].image)[0]
        return cache[gid]

    for layer in tiled_map.layers:
        if not layer.visible:
            continue
        if isinstance(layer, tp.TileLayer):
            for y, row in enumerate(layer.data):
                for x, gid in enumerate(row):
                    if gid:
                        map_bundle.composite(canvas, tile_image(gid), x * tw, y * th)
        elif isinstance(layer, tp.ImageLayer):
            image = forge_core.load_rgba(tmj.parent / layer.image)[0]
            offset = layer.offset
            map_bundle.composite(canvas, image, forge_core.round_half_up(offset.x), forge_core.round_half_up(offset.y))
        elif isinstance(layer, tp.ObjectLayer):
            objects = [o for o in layer.tiled_objects if isinstance(o, tp.tiled_object.Tile) and o.visible]
            if layer.draw_order == "topdown":
                objects = sorted(objects, key=lambda o: o.coordinates.y)
            for obj in objects:
                gid, mirrored = _object_gid(int(obj.gid))
                map_bundle.draw_image(canvas, _mirrored(tile_image(gid), mirrored), obj.coordinates.x,
                                      obj.coordinates.y, obj.size.width, obj.size.height)
    return np.asarray(canvas)[: size[1], : size[0]]


def differing_pixels(a: np.ndarray, b: np.ndarray) -> int:
    if a.shape != b.shape:
        return int(max(a.shape[0] * a.shape[1], b.shape[0] * b.shape[1]))
    return int(np.any(a != b, axis=-1).sum())


# --------------------------------------------------------------------------- CLI

def _checked_bundle(path: Path) -> map_bundle.Bundle:
    bundle = map_bundle.load_bundle(path)
    if bundle.errors or not bundle.readable:
        map_bundle.print_problems(bundle.errors)
        raise BundleError(f"{path.name} does not validate; run map_bundle.py validate first (nothing was written)")
    return bundle


def export_bundle(bundle: map_bundle.Bundle, stage: Path, embedded_variant: bool) -> dict:
    """Write the Tiled export of ``bundle`` into ``stage`` and return its QA report; the
    report's status is "fail" when a TSX is malformed or a re-render differs from the
    bundle's reference render."""
    reference = map_bundle.render_map(bundle)
    size = (reference.shape[1], reference.shape[0])
    exporter = _Exporter(bundle, stage)
    external, embedded = exporter.build()
    for name, tileset, _ in exporter.tilesets:
        (stage / name).write_text(tileset_xml(tileset), encoding="utf-8", newline="\n")
    maps = {"map.tmj": external}
    if embedded_variant:
        maps["map.embedded.tmj"] = embedded
    for name, document in maps.items():
        forge_core.write_json(stage / name, document)
    forge_core.save_png(reference, stage / "preview.png")
    malformed = []
    for name, _, _ in exporter.tilesets:
        try:
            ET.parse(stage / name)
        except ET.ParseError as error:
            malformed.append(f"{name}: {error}")
    checks = [{"id": "tsx_well_formed", "status": "fail" if malformed else "pass", "value": malformed,
               "threshold": []}]
    for name in maps:
        differ = differing_pixels(render_tiled(read_tiled_map(stage / name), size), reference)
        checks.append({"id": f"rerender_{name.replace('.', '_')}", "status": "fail" if differ else "pass",
                       "value": differ, "threshold": 0})
    outputs = sorted(p for p in stage.rglob("*") if p.is_file())
    status = "fail" if any(check["status"] == "fail" for check in checks) else "pass"
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "method": ("TMJ and TSX written from the bundle, read back with the built-in reader (TSX via ElementTree) "
                   "and re-rendered (tile layers, image layers, prop tile objects in draw order); the re-render "
                   "must equal render_map(bundle) pixel for pixel (preview.png)"),
        "notProven": [
            "Tiled GUI not verified: opening the map, wang/terrain brushes and the mixed blob wangset in the editor",
            "Phaser or another engine rendering map.embedded.tmj the same way",
            "runtime depth sorting by the sortY property (Tiled's own topdown order sorts by image bottom)",
        ],
        "checks": checks,
        "inputs": map_bundle.bundle_inputs(bundle, stage),
        "outputs": [forge_core.file_ref(path, stage) for path in outputs],
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "tiled": {"format": TILED_FORMAT, "app": TILED_APP,
                  "tilesets": [{"file": name, "name": ts["name"], "firstgid": gid, "tilecount": ts["tilecount"]}
                               for name, ts, gid in exporter.tilesets],
                  "layers": [layer["name"] for layer in external["layers"]]},
    }


def cmd_export(args: argparse.Namespace) -> int:
    final = args.output_dir.resolve()
    if final.exists() or final.is_symlink():
        raise BundleError(f"refusing to replace existing output: {final}")
    bundle = _checked_bundle(args.bundle)
    with forge_core.staged_output(final) as stage:
        report = export_bundle(bundle, stage, args.embedded_variant)
        forge_core.write_json(stage / "export-report.json", report)
        if report["status"] == "fail":
            failed = [f"{c['id']}={c['value']}" for c in report["checks"] if c["status"] == "fail"]
            raise ExportError(f"export checks failed ({', '.join(failed)}); nothing was published")
    print(json.dumps({"status": "pass", "output": str(final), "metadata": str(final / "export-report.json"),
                      "map": str(final / "map.tmj"),
                      "embedded": str(final / "map.embedded.tmj") if args.embedded_variant else None,
                      "tilesets": len(report["tiled"]["tilesets"]), "layers": report["tiled"]["layers"],
                      "differingPixels": 0}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_tiled.py",
        description="Export a generate2dmap map bundle to Tiled 1.10 JSON (TMJ) with TSX tilesets, verified by "
                    "re-rendering. The Tiled GUI and terrain brushes are not verified.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    export = verbs.add_parser("export", help="write map.tmj, TSX tilesets, images and a report into a new folder",
                              description="Export a bundle to Tiled. Nothing is published unless the re-render of the "
                                          "written files equals the bundle's reference render.")
    export.add_argument("--bundle", required=True, type=Path, help="map bundle JSON")
    export.add_argument("--output-dir", required=True, type=Path, help="new folder for the export (must not exist)")
    export.add_argument("--embedded-variant", action="store_true",
                        help="also write map.embedded.tmj with the tilesets inlined (Phaser cannot read .tsx)")
    export.set_defaults(func=cmd_export)
    verify = verbs.add_parser("verify", help="re-render an exported map and compare it with a bundle",
                              description="Re-render a TMJ map and compare it pixel for pixel with the bundle's "
                                          "reference render. Exit 1 on any difference.")
    verify.add_argument("--map", required=True, type=Path, help="exported map.tmj or map.embedded.tmj")
    verify.add_argument("--bundle", required=True, type=Path, help="map bundle JSON the map was exported from")
    verify.add_argument("--reader", choices=("builtin", "pytiled"), default="builtin",
                        help="builtin (standard library) or pytiled (needs pip install pytiled-parser)")
    verify.set_defaults(func=cmd_verify)
    return parser


def cmd_verify(args: argparse.Namespace) -> int:
    bundle = _checked_bundle(args.bundle)
    reference = map_bundle.render_map(bundle)
    size = (reference.shape[1], reference.shape[0])
    if args.reader == "pytiled":
        rendered = render_pytiled(args.map, size)
    else:
        rendered = render_tiled(read_tiled_map(args.map), size)
    differ = differing_pixels(rendered, reference)
    if differ:
        print(f"error: {differ} pixel(s) differ from the bundle's reference render", file=sys.stderr)
        return 1
    print(json.dumps({"status": "pass", "map": str(Path(args.map).resolve()), "bundle": str(bundle.path),
                      "reader": args.reader, "differingPixels": 0}))
    return 0


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def main(argv: list[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27)."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
