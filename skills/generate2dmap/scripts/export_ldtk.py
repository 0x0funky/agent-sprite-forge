#!/usr/bin/env python3
"""Export a map bundle (generate2dmap.map_bundle.v2) to an LDtk project (JSON 1.5.3).

Writes into a new --output-dir:

  <name>.ldtk          one level in a Free world layout:
                         - a Tiles layer per bundle tiles layer (exact tile placement;
                           each tileset's per-tile wang / blob / collision / walkable
                           data rides in the tileset's customData as JSON),
                         - an Entities layer of props (one entity definition per prop
                           image and anchor; the pivot is the prop's anchor, so an
                           entity's px is the bundle's (x, y)),
                         - a Markers Entities layer: Spawn, Portal, Interaction, Anchor,
                         - the bottom image layer as the level background.
  assets/...           byte-identical tileset and background copies, plus
                       props-atlas.png (the prop images packed on the tile grid)
  ldtk-export.json     what was written, what was not, and the QA

Before anything is published the project is checked against a snapshot of the
LDtk 1.5.3 JSON required fields and types, its uids, iids and identifiers are
checked, and tiles and entity positions are read back and compared with the
bundle. LDtk positions are whole pixels: fractional bundle positions are rounded
half up and reported (--strict-qc refuses them). Verified at parse level only:
the LDtk editor is not run by this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)
from export_godot import (BundleInfo, ImageInfo, _local_file_ref, _local_not_exported,  # noqa: E402
                          _local_read_bundle, _local_safe_name, _local_world_warnings)


REPORT_SCHEMA = "generate2dmap.engine_export.v1"
TOOL = {"name": "export_ldtk", "version": "1.0"}
LDTK_VERSION = "1.5.3"
IID_NAMESPACE = uuid.UUID("6f1c2a52-9a43-4f7e-8d0e-6b2f3f7f0b14")
DEFAULT_GRID = 16
LEVEL_BG = "#696A79"
NOT_PROVEN = [
    "Parse-level only: the project is checked against this tool's snapshot of the LDtk 1.5.3 JSON required fields "
    "and read back by this tool; the LDtk editor was not run.",
    "Terrain is exported as placed tiles, not as IntGrid plus auto-layer rules; re-painting terrain in LDtk needs "
    "rules made there.",
    "Collision shapes, walk regions, the material map and the nav grid stay in the map bundle; per-tile collision "
    "rides in tileset customData only.",
]

# LDtk 1.5.3 JSON (https://ldtk.io/json, schema https://ldtk.io/files/JSON_SCHEMA.json): the fields this
# exporter must write for each type, with their JSON types ("?" = nullable). Hand-transcribed snapshot; the
# project is checked against it before anything is published.
LDTK_REQUIRED: dict[str, dict[str, str]] = {
    "Project": {
        "iid": "str", "jsonVersion": "str", "appBuildId": "num", "nextUid": "int", "identifierStyle": "str",
        "toc": "list", "worldLayout": "str?", "worldGridWidth": "int?", "worldGridHeight": "int?",
        "defaultLevelWidth": "int?", "defaultLevelHeight": "int?", "defaultPivotX": "num", "defaultPivotY": "num",
        "defaultGridSize": "int", "defaultEntityWidth": "int", "defaultEntityHeight": "int", "bgColor": "str",
        "defaultLevelBgColor": "str", "minifyJson": "bool", "externalLevels": "bool", "exportTiled": "bool",
        "simplifiedExport": "bool", "imageExportMode": "str", "exportLevelBg": "bool", "pngFilePattern": "str?",
        "backupOnSave": "bool", "backupLimit": "int", "backupRelPath": "str?", "levelNamePattern": "str",
        "tutorialDesc": "str?", "customCommands": "list", "flags": "list", "defs": "dict", "levels": "list",
        "worlds": "list", "dummyWorldIid": "str",
    },
    "Definitions": {"layers": "list", "entities": "list", "tilesets": "list", "enums": "list",
                    "externalEnums": "list", "levelFields": "list"},
    "TilesetDef": {
        "__cWid": "int", "__cHei": "int", "identifier": "str", "uid": "int", "relPath": "str?", "embedAtlas": "str?",
        "pxWid": "int", "pxHei": "int", "tileGridSize": "int", "spacing": "int", "padding": "int", "tags": "list",
        "tagsSourceEnumUid": "int?", "enumTags": "list", "customData": "list", "savedSelections": "list",
    },
    "LayerDef": {
        "__type": "str", "identifier": "str", "type": "str", "uid": "int", "doc": "str?", "uiColor": "str?",
        "gridSize": "int", "guideGridWid": "int", "guideGridHei": "int", "displayOpacity": "num",
        "inactiveOpacity": "num", "hideInList": "bool", "hideFieldsWhenInactive": "bool",
        "canSelectWhenInactive": "bool", "renderInWorldView": "bool", "pxOffsetX": "int", "pxOffsetY": "int",
        "parallaxFactorX": "num", "parallaxFactorY": "num", "parallaxScaling": "bool", "requiredTags": "list",
        "excludedTags": "list", "autoTilesKilledByOtherLayerUid": "int?", "uiFilterTags": "list",
        "useAsyncRender": "bool", "intGridValues": "list", "intGridValuesGroups": "list", "autoRuleGroups": "list",
        "autoSourceLayerDefUid": "int?", "tilesetDefUid": "int?", "tilePivotX": "num", "tilePivotY": "num",
        "biomeFieldUid": "int?",
    },
    "EntityDef": {
        "identifier": "str", "uid": "int", "tags": "list", "exportToToc": "bool", "allowOutOfBounds": "bool",
        "doc": "str?", "width": "int", "height": "int", "resizableX": "bool", "resizableY": "bool",
        "minWidth": "int?", "maxWidth": "int?", "minHeight": "int?", "maxHeight": "int?", "keepAspectRatio": "bool",
        "tileOpacity": "num", "fillOpacity": "num", "lineOpacity": "num", "hollow": "bool", "color": "str",
        "renderMode": "str", "showName": "bool", "tilesetId": "int?", "tileRenderMode": "str", "tileRect": "dict?",
        "uiTileRect": "dict?", "nineSliceBorders": "list", "maxCount": "int", "limitScope": "str",
        "limitBehavior": "str", "pivotX": "num", "pivotY": "num", "fieldDefs": "list",
    },
    "FieldDef": {
        "identifier": "str", "doc": "str?", "__type": "str", "uid": "int", "type": "str", "isArray": "bool",
        "canBeNull": "bool", "arrayMinLength": "int?", "arrayMaxLength": "int?", "editorDisplayMode": "str",
        "editorDisplayScale": "num", "editorDisplayPos": "str", "editorLinkStyle": "str",
        "editorDisplayColor": "str?", "editorAlwaysShow": "bool", "editorShowInWorld": "bool",
        "editorCutLongValues": "bool", "editorTextSuffix": "str?", "editorTextPrefix": "str?",
        "useForSmartColor": "bool", "exportToToc": "bool", "searchable": "bool", "min": "num?", "max": "num?",
        "regex": "str?", "acceptFileTypes": "list?", "defaultOverride": "dict?", "textLanguageMode": "str?",
        "symmetricalRef": "bool", "autoChainRef": "bool", "allowOutOfLevelRef": "bool", "allowedRefs": "str",
        "allowedRefsEntityUid": "int?", "allowedRefTags": "list", "tilesetUid": "int?",
    },
    "Level": {
        "identifier": "str", "iid": "str", "uid": "int", "worldX": "int", "worldY": "int", "worldDepth": "int",
        "pxWid": "int", "pxHei": "int", "__bgColor": "str", "bgColor": "str?", "useAutoIdentifier": "bool",
        "bgRelPath": "str?", "bgPos": "str?", "bgPivotX": "num", "bgPivotY": "num", "__smartColor": "str",
        "__bgPos": "dict?", "externalRelPath": "str?", "fieldInstances": "list", "layerInstances": "list?",
        "__neighbours": "list",
    },
    "LayerInstance": {
        "__identifier": "str", "__type": "str", "__cWid": "int", "__cHei": "int", "__gridSize": "int",
        "__opacity": "num", "__pxTotalOffsetX": "int", "__pxTotalOffsetY": "int", "__tilesetDefUid": "int?",
        "__tilesetRelPath": "str?", "iid": "str", "levelId": "int", "layerDefUid": "int", "pxOffsetX": "int",
        "pxOffsetY": "int", "visible": "bool", "optionalRules": "list", "intGridCsv": "list",
        "autoLayerTiles": "list", "seed": "int", "overrideTilesetUid": "int?", "gridTiles": "list",
        "entityInstances": "list",
    },
    "TileInstance": {"px": "list", "src": "list", "f": "int", "t": "int", "d": "list", "a": "num"},
    "EntityInstance": {
        "__identifier": "str", "__grid": "list", "__pivot": "list", "__tags": "list", "__tile": "dict?",
        "__smartColor": "str", "__worldX": "int?", "__worldY": "int?", "iid": "str", "width": "int",
        "height": "int", "defUid": "int", "px": "list", "fieldInstances": "list",
    },
    "FieldInstance": {"__identifier": "str", "__type": "str", "__value": "any", "__tile": "dict?", "defUid": "int",
                      "realEditorValues": "list"},
    "TilesetRect": {"tilesetUid": "int", "x": "int", "y": "int", "w": "int", "h": "int"},
}
LDTK_ENUMS: dict[tuple[str, str], tuple[str, ...]] = {
    ("Project", "identifierStyle"): ("Capitalize", "Uppercase", "Lowercase", "Free"),
    ("Project", "worldLayout"): ("Free", "GridVania", "LinearHorizontal", "LinearVertical"),
    ("Project", "imageExportMode"): ("None", "OneImagePerLayer", "OneImagePerLevel", "LayersAndLevels"),
    ("LayerDef", "type"): ("IntGrid", "Entities", "Tiles", "AutoLayer"),
    ("LayerInstance", "__type"): ("IntGrid", "Entities", "Tiles", "AutoLayer"),
    ("EntityDef", "renderMode"): ("Rectangle", "Ellipse", "Tile", "Cross"),
    ("EntityDef", "tileRenderMode"): ("Cover", "FitInside", "Repeat", "Stretch", "FullSizeCropped",
                                      "FullSizeUncropped", "NineSlice"),
    ("EntityDef", "limitScope"): ("PerLayer", "PerLevel", "PerWorld"),
    ("EntityDef", "limitBehavior"): ("DiscardOldOnes", "PreventAdding", "MoveLastOne"),
    ("FieldDef", "allowedRefs"): ("Any", "OnlySame", "OnlyTags", "OnlySpecificEntity"),
    ("Level", "bgPos"): ("Unscaled", "Contain", "Cover", "CoverDirty", "Repeat"),
}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FIELD_TYPES = {"str": ("F_String", "String", "V_String"), "float": ("F_Float", "Float", "V_Float"),
               "bool": ("F_Bool", "Bool", "V_Bool"), "int": ("F_Int", "Int", "V_Int")}


# --------------------------------------------------------------------------- spec snapshot check

def _type_ok(value: Any, tag: str) -> bool:
    if tag.endswith("?"):
        return value is None or _type_ok(value, tag[:-1])
    return {"str": lambda v: isinstance(v, str), "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "num": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "bool": lambda v: isinstance(v, bool), "list": lambda v: isinstance(v, list),
            "dict": lambda v: isinstance(v, dict), "any": lambda v: True}[tag](value)


def _check_type(errors: list[str], where: str, kind: str, item: Any) -> None:
    if not isinstance(item, dict):
        errors.append(f"{where}: {kind} must be an object")
        return
    for key, tag in LDTK_REQUIRED[kind].items():
        if key not in item:
            errors.append(f"{where}: {kind} misses required field {key!r}")
        elif not _type_ok(item[key], tag):
            errors.append(f"{where}.{key}: expected {tag}, got {type(item[key]).__name__}")
        elif (kind, key) in LDTK_ENUMS and item[key] is not None and item[key] not in LDTK_ENUMS[(kind, key)]:
            errors.append(f"{where}.{key}: {item[key]!r} is not one of {LDTK_ENUMS[(kind, key)]}")


def ldtk_spec_errors(project: Any) -> list[str]:
    """Missing or mistyped required fields of every LDtk object in the project (empty when it complies)."""
    errors: list[str] = []
    _check_type(errors, "$", "Project", project)
    if errors:
        return errors
    defs = project["defs"]
    _check_type(errors, "$.defs", "Definitions", defs)
    for index, tileset in enumerate(defs.get("tilesets") or []):
        _check_type(errors, f"$.defs.tilesets[{index}]", "TilesetDef", tileset)
    for index, layer in enumerate(defs.get("layers") or []):
        _check_type(errors, f"$.defs.layers[{index}]", "LayerDef", layer)
    for index, entity in enumerate(defs.get("entities") or []):
        where = f"$.defs.entities[{index}]"
        _check_type(errors, where, "EntityDef", entity)
        for key in ("tileRect", "uiTileRect"):
            if isinstance(entity, dict) and isinstance(entity.get(key), dict):
                _check_type(errors, f"{where}.{key}", "TilesetRect", entity[key])
        for number, field in enumerate((entity or {}).get("fieldDefs") or []):
            _check_type(errors, f"{where}.fieldDefs[{number}]", "FieldDef", field)
    for index, level in enumerate(project.get("levels") or []):
        where = f"$.levels[{index}]"
        _check_type(errors, where, "Level", level)
        for number, layer in enumerate((level or {}).get("layerInstances") or []):
            lwhere = f"{where}.layerInstances[{number}]"
            _check_type(errors, lwhere, "LayerInstance", layer)
            for t, tile in enumerate((layer or {}).get("gridTiles") or []):
                _check_type(errors, f"{lwhere}.gridTiles[{t}]", "TileInstance", tile)
            for e, entity in enumerate((layer or {}).get("entityInstances") or []):
                ewhere = f"{lwhere}.entityInstances[{e}]"
                _check_type(errors, ewhere, "EntityInstance", entity)
                if isinstance(entity, dict) and isinstance(entity.get("__tile"), dict):
                    _check_type(errors, f"{ewhere}.__tile", "TilesetRect", entity["__tile"])
                for f, field in enumerate((entity or {}).get("fieldInstances") or []):
                    _check_type(errors, f"{ewhere}.fieldInstances[{f}]", "FieldInstance", field)
    return errors


def ldtk_id_errors(project: dict[str, Any]) -> list[str]:
    """uids and iids unique, identifiers valid and unique per kind, references resolve."""
    errors = []
    defs = project["defs"]
    uids: dict[int, str] = {}

    def claim(uid: int, what: str) -> None:
        if uid in uids:
            errors.append(f"uid {uid} is used by {uids[uid]} and {what}")
        uids[uid] = what

    for kind in ("tilesets", "layers", "entities"):
        names = set()
        for item in defs[kind]:
            claim(item["uid"], f"{kind} {item['identifier']}")
            if not IDENTIFIER.match(item["identifier"]) or item["identifier"] in names:
                errors.append(f"{kind} identifier {item['identifier']!r} is invalid or repeated")
            names.add(item["identifier"])
            for field in item.get("fieldDefs", []):
                claim(field["uid"], f"field {item['identifier']}.{field['identifier']}")
    for level in project["levels"]:
        claim(level["uid"], f"level {level['identifier']}")
    if project["nextUid"] <= max(uids, default=0):
        errors.append(f"nextUid {project['nextUid']} is not above the largest uid {max(uids)}")
    tilesets = {item["uid"] for item in defs["tilesets"]}
    layers = {item["uid"]: item for item in defs["layers"]}
    entities = {item["uid"]: item for item in defs["entities"]}
    iids = [project["iid"], project["dummyWorldIid"]]
    for item in defs["layers"]:
        if item["tilesetDefUid"] is not None and item["tilesetDefUid"] not in tilesets:
            errors.append(f"layer {item['identifier']} points at missing tileset {item['tilesetDefUid']}")
    for item in defs["entities"]:
        if item["tilesetId"] is not None and item["tilesetId"] not in tilesets:
            errors.append(f"entity {item['identifier']} points at missing tileset {item['tilesetId']}")
    for level in project["levels"]:
        iids.append(level["iid"])
        if [layer["layerDefUid"] for layer in level["layerInstances"]] != [item["uid"] for item in defs["layers"]]:
            errors.append(f"level {level['identifier']}: layer instances do not follow defs.layers")
        for layer in level["layerInstances"]:
            iids.append(layer["iid"])
            if layer["layerDefUid"] not in layers:
                errors.append(f"layer instance {layer['__identifier']} has no definition")
            for entity in layer["entityInstances"]:
                iids.append(entity["iid"])
                definition = entities.get(entity["defUid"])
                if definition is None:
                    errors.append(f"entity {entity['__identifier']} has no definition")
                    continue
                field_uids = [field["uid"] for field in definition["fieldDefs"]]
                if [field["defUid"] for field in entity["fieldInstances"]] != field_uids:
                    errors.append(f"entity {entity['iid']}: field instances do not follow its definition")
    if len(set(iids)) != len(iids):
        errors.append("iids are not unique")
    return errors


# --------------------------------------------------------------------------- building blocks

class _Uids:
    def __init__(self) -> None:
        self.next = 1

    def take(self) -> int:
        self.next += 1
        return self.next - 1


def _iid(*parts: Any) -> str:
    return str(uuid.uuid5(IID_NAMESPACE, "\x1f".join(str(part) for part in parts)))


def ldtk_identifier(text: str, taken: set[str], fallback: str = "Item") -> str:
    """A unique LDtk identifier in the Capitalize style: [A-Za-z_][A-Za-z0-9_]*, first letter upper case."""
    stem = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", str(text))).strip("_")
    if not stem:
        stem = f"{fallback}_{hashlib.sha256(str(text).encode('utf-8')).hexdigest()[:6]}"
    if stem[0].isdigit():
        stem = f"{fallback}_{stem}"
    stem = stem[0].upper() + stem[1:]
    name, number = stem, 2
    while name in taken:
        name, number = f"{stem}_{number}", number + 1
    taken.add(name)
    return name


def _color(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "#" + "".join(f"{64 + value % 160:02X}" for value in digest[:3])


def _round(value: float) -> int:
    return int(math.floor(value + 0.5))


def _field_def(uids: _Uids, identifier: str, kind: str, *, nullable: bool = True) -> dict[str, Any]:
    internal, public, _ = FIELD_TYPES[kind]
    return {
        "identifier": identifier, "doc": None, "__type": public, "uid": uids.take(), "type": internal,
        "isArray": False, "canBeNull": nullable and kind != "bool", "arrayMinLength": None, "arrayMaxLength": None,
        "editorDisplayMode": "Hidden", "editorDisplayScale": 1, "editorDisplayPos": "Above",
        "editorLinkStyle": "StraightArrow", "editorDisplayColor": None, "editorAlwaysShow": False,
        "editorShowInWorld": True, "editorCutLongValues": True, "editorTextSuffix": None, "editorTextPrefix": None,
        "useForSmartColor": False, "exportToToc": False, "searchable": False, "min": None, "max": None,
        "regex": None, "acceptFileTypes": None, "defaultOverride": None, "textLanguageMode": None,
        "symmetricalRef": False, "autoChainRef": True, "allowOutOfLevelRef": True, "allowedRefs": "OnlySame",
        "allowedRefsEntityUid": None, "allowedRefTags": [], "tilesetUid": None,
    }


def _field_instance(definition: dict[str, Any], value: Any) -> dict[str, Any]:
    kind = {"F_String": "str", "F_Float": "float", "F_Bool": "bool", "F_Int": "int"}[definition["type"]]
    if value is None:
        public = False if kind == "bool" else None
        editor: list[Any] = []
    else:
        public = {"str": str, "float": float, "bool": bool, "int": int}[kind](value)
        editor = [{"id": FIELD_TYPES[kind][2], "params": [public]}]
    return {"__identifier": definition["identifier"], "__type": definition["__type"], "__value": public,
            "__tile": None, "defUid": definition["uid"], "realEditorValues": editor}


def _entity_def(uids: _Uids, identifier: str, *, width: int, height: int, pivot: tuple[float, float],
                color: str, render: str = "Rectangle", fields: Sequence[tuple[str, str]] = (),
                tile: dict[str, Any] | None = None, resizable: bool = False) -> dict[str, Any]:
    return {
        "identifier": identifier, "uid": uids.take(), "tags": [], "exportToToc": False, "allowOutOfBounds": False,
        "doc": None, "width": width, "height": height, "resizableX": resizable, "resizableY": resizable,
        "minWidth": None, "maxWidth": None, "minHeight": None, "maxHeight": None, "keepAspectRatio": tile is not None,
        "tileOpacity": 1, "fillOpacity": 0.08 if tile is None else 0, "lineOpacity": 1 if tile is None else 0,
        "hollow": False, "color": color, "renderMode": render if tile is None else "Tile", "showName": tile is None,
        "tilesetId": None if tile is None else tile["tilesetUid"],
        "tileRenderMode": "FitInside" if tile is None else "Stretch", "tileRect": tile, "uiTileRect": None,
        "nineSliceBorders": [], "maxCount": 0, "limitScope": "PerLevel", "limitBehavior": "MoveLastOne",
        "pivotX": pivot[0], "pivotY": pivot[1],
        "fieldDefs": [_field_def(uids, name, kind) for name, kind in fields],
    }


def _layer_def(uids: _Uids, identifier: str, kind: str, grid: int, tileset_uid: int | None = None) -> dict[str, Any]:
    return {
        "__type": kind, "identifier": identifier, "type": kind, "uid": uids.take(), "doc": None, "uiColor": None,
        "gridSize": grid, "guideGridWid": 0, "guideGridHei": 0, "displayOpacity": 1, "inactiveOpacity": 0.6,
        "hideInList": False, "hideFieldsWhenInactive": True, "canSelectWhenInactive": True,
        "renderInWorldView": True, "pxOffsetX": 0, "pxOffsetY": 0, "parallaxFactorX": 0, "parallaxFactorY": 0,
        "parallaxScaling": True, "requiredTags": [], "excludedTags": [], "autoTilesKilledByOtherLayerUid": None,
        "uiFilterTags": [], "useAsyncRender": False, "intGridValues": [], "intGridValuesGroups": [],
        "autoRuleGroups": [], "autoSourceLayerDefUid": None, "tilesetDefUid": tileset_uid, "tilePivotX": 0,
        "tilePivotY": 0, "biomeFieldUid": None,
    }


def _tileset_def(uids: _Uids, identifier: str, rel_path: str, size: tuple[int, int], grid: int,
                 custom: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "__cWid": size[0] // grid, "__cHei": size[1] // grid, "identifier": identifier, "uid": uids.take(),
        "relPath": rel_path, "embedAtlas": None, "pxWid": size[0], "pxHei": size[1], "tileGridSize": grid,
        "spacing": 0, "padding": 0, "tags": [], "tagsSourceEnumUid": None, "enumTags": [], "customData": custom,
        "savedSelections": [],
    }


def _entity(definition: dict[str, Any], px: tuple[int, int], size: tuple[int, int], grid: int,
            values: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        "__identifier": definition["identifier"], "__grid": [px[0] // grid, px[1] // grid],
        "__pivot": [definition["pivotX"], definition["pivotY"]], "__tags": [],
        "__tile": definition["tileRect"], "__smartColor": definition["color"], "iid": _iid("entity", key),
        "width": size[0], "height": size[1], "defUid": definition["uid"], "px": [px[0], px[1]],
        "fieldInstances": [_field_instance(field, values.get(field["identifier"]))
                           for field in definition["fieldDefs"]],
        "__worldX": px[0], "__worldY": px[1],
    }


def _layer_instance(definition: dict[str, Any], level: dict[str, Any], *, tiles: list[dict[str, Any]] | None = None,
                    entities: list[dict[str, Any]] | None = None, tileset: dict[str, Any] | None = None,
                    key: str) -> dict[str, Any]:
    grid = definition["gridSize"]
    return {
        "__identifier": definition["identifier"], "__type": definition["type"],
        "__cWid": math.ceil(level["pxWid"] / grid), "__cHei": math.ceil(level["pxHei"] / grid), "__gridSize": grid,
        "__opacity": 1, "__pxTotalOffsetX": 0, "__pxTotalOffsetY": 0,
        "__tilesetDefUid": None if tileset is None else tileset["uid"],
        "__tilesetRelPath": None if tileset is None else tileset["relPath"],
        "iid": _iid("layer", key), "levelId": level["uid"], "layerDefUid": definition["uid"], "pxOffsetX": 0,
        "pxOffsetY": 0, "visible": True, "optionalRules": [], "intGridCsv": [], "autoLayerTiles": [],
        "seed": int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:7], 16), "overrideTilesetUid": None,
        "gridTiles": tiles or [], "entityInstances": entities or [],
    }


def pack_props(images: list[tuple[str, ImageInfo]], grid: int) -> tuple[np.ndarray, dict[str, tuple[int, int]]]:
    """Shelf-pack distinct prop images on the tile grid; returns the atlas and sha256 -> top-left."""
    aligned = [(sha, info, math.ceil(info.size[0] / grid) * grid, math.ceil(info.size[1] / grid) * grid)
               for sha, info in images]
    area = sum(w * h for _, _, w, h in aligned)
    width = max([w for _, _, w, _ in aligned] + [math.ceil(math.sqrt(area) / grid) * grid])
    positions, x, y, row = {}, 0, 0, 0
    for sha, _, w, h in aligned:
        if x + w > width:
            x, y, row = 0, y + row, 0
        positions[sha] = (x, y)
        x, row = x + w, max(row, h)
    atlas = np.zeros((y + row, width, 4), np.uint8)
    for sha, info, _, _ in aligned:
        image, _ = forge_core.load_rgba(info.path)
        px, py = positions[sha]
        atlas[py:py + info.size[1], px:px + info.size[0]] = np.asarray(image)
    return atlas, positions


# --------------------------------------------------------------------------- export

def build_project(bundle: BundleInfo, name: str, stage: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The LDtk project dict plus what QA needs to read it back."""
    if bundle.tile_size is not None and bundle.tile_size[0] != bundle.tile_size[1]:
        raise ValueError(f"LDtk layers need square tiles; the bundle grid is {bundle.tile_size[0]}x"
                         f"{bundle.tile_size[1]}.")
    grid = bundle.tile_size[0] if bundle.tile_size else DEFAULT_GRID
    uids = _Uids()
    names: dict[str, set[str]] = {"tilesets": set(), "layers": set(), "entities": set(), "levels": set()}
    files: set[str] = set()
    assets, warnings = [], []

    def copy(role: str, ident: str, info: ImageInfo, folder: str) -> str:
        relative = f"assets/{folder}/{_local_safe_name(ident, files)}.png"
        (stage / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(info.path, stage / relative)
        assets.append({"role": role, "id": ident, "path": relative, "sha256": info.sha256,
                       "source": _local_file_ref(info.path, bundle.path.parent, info.sha256)["path"]})
        return relative

    tileset_defs: dict[str, dict[str, Any]] = {}
    for layer in bundle.layers:
        if layer["kind"] != "tiles" or layer["tiles"].tileset.id in tileset_defs:
            continue
        tileset = layer["tiles"].tileset
        if tileset.tile_size != (grid, grid):
            raise ValueError(f"tileset {tileset.id} has {tileset.tile_size[0]}x{tileset.tile_size[1]} tiles; the "
                             f"LDtk layer grid is {grid}.")
        if tileset.image_size[0] % grid or tileset.image_size[1] % grid:
            raise ValueError(f"tileset {tileset.id}: the {tileset.image_size[0]}x{tileset.image_size[1]} atlas must be "
                             f"a multiple of the {grid} px tile for LDtk tile ids.")
        relative = copy("tileset", tileset.id, ImageInfo(tileset.image_path, tileset.image_sha256, tileset.image_size),
                        "tilesets")
        columns = tileset.image_size[0] // grid
        custom = []
        for index in sorted(tileset.tiles):
            tile = {key: value for key, value in tileset.tiles[index].items() if key != "index"}
            ax, ay = tileset.atlas(index)
            custom.append({"tileId": ax + ay * columns,
                           "data": json.dumps({"index": index, **tile}, sort_keys=True, separators=(",", ":"))})
        tileset_defs[tileset.id] = _tileset_def(uids, ldtk_identifier(tileset.id, names["tilesets"], "Tileset"),
                                                relative, tileset.image_size, grid, custom)
    distinct: dict[str, ImageInfo] = {}
    for item in bundle.objects:
        distinct.setdefault(item["_image"].sha256, item["_image"])
    props_tileset = None
    atlas_positions: dict[str, tuple[int, int]] = {}
    if distinct:
        atlas, atlas_positions = pack_props(list(distinct.items()), grid)
        (stage / "assets").mkdir(exist_ok=True)
        forge_core.save_png(atlas, stage / "assets" / "props-atlas.png")
        props_tileset = _tileset_def(uids, ldtk_identifier("Props", names["tilesets"]), "assets/props-atlas.png",
                                     (atlas.shape[1], atlas.shape[0]), grid, [])
    background = None
    for position, layer in enumerate(bundle.layers):
        if layer["kind"] != "image":
            continue
        if position == 0 and background is None:
            background = (copy("layer", layer["name"], layer["image"], "layers"), layer["image"])
        else:
            warnings.append(f"image layer {layer['name']} is not the bottom layer; LDtk has one level background, "
                            f"so it was not exported")

    entity_defs: list[dict[str, Any]] = []
    marker_defs = {
        "spawn": _entity_def(uids, ldtk_identifier("Spawn", names["entities"]), width=grid, height=grid,
                             pivot=(0.5, 1.0), color="#3FBF6A", fields=[("BundleId", "str"), ("Facing", "str")]),
        "portal": _entity_def(uids, ldtk_identifier("Portal", names["entities"]), width=grid, height=grid,
                              pivot=(0.0, 0.0), color="#3F7FDF", resizable=True,
                              fields=[("BundleId", "str"), ("To", "str"), ("Shape", "str"), ("Activation", "str"),
                                      ("TravelX", "float"), ("TravelY", "float"), ("Radius", "float"),
                                      ("Latch", "bool"), ("RequiresMovement", "bool"), ("EntranceByFrom", "str")]),
        "interaction": _entity_def(uids, ldtk_identifier("Interaction", names["entities"]), width=grid, height=grid,
                                   pivot=(0.5, 0.5), color="#DFAF3F", render="Ellipse",
                                   fields=[("BundleId", "str"), ("Reach", "float")]),
        "anchor": _entity_def(uids, ldtk_identifier("Anchor", names["entities"]), width=grid, height=grid,
                              pivot=(0.5, 0.5), color="#BF5FBF", render="Cross",
                              fields=[("BundleId", "str"), ("Facing", "str"), ("Slots", "str"), ("Approach", "str")]),
    }
    entity_defs += marker_defs.values()
    prop_defs: dict[tuple[str, str, float, float], dict[str, Any]] = {}
    for item in bundle.objects:
        info = item["_image"]
        key = (str(item.get("prop") or item["id"]), info.sha256, float(item["anchor_px"][0]),
               float(item["anchor_px"][1]))
        if key in prop_defs:
            continue
        ax, ay = atlas_positions[info.sha256]
        rect = {"tilesetUid": props_tileset["uid"], "x": ax, "y": ay, "w": info.size[0], "h": info.size[1]}
        prop_defs[key] = _entity_def(
            uids, ldtk_identifier(f"Prop_{key[0]}", names["entities"], "Prop"), width=info.size[0],
            height=info.size[1], pivot=(key[2] / info.size[0], key[3] / info.size[1]), color=_color(key[0]),
            fields=[("BundleId", "str"), ("Prop", "str"), ("Scale", "float"), ("SortY", "float"), ("Solid", "bool"),
                    ("Occlusion", "str"), ("Footprint", "str")], tile=rect, resizable=True)
    entity_defs += prop_defs.values()

    level = {"identifier": ldtk_identifier(name, names["levels"], "Level"), "iid": _iid("level", bundle.sha256, name),
             "uid": uids.take(), "worldX": 0, "worldY": 0, "worldDepth": 0,
             "pxWid": math.ceil(bundle.world[0]), "pxHei": math.ceil(bundle.world[1]), "__bgColor": LEVEL_BG,
             "bgColor": None, "useAutoIdentifier": False, "bgRelPath": None, "bgPos": None, "bgPivotX": 0.0,
             "bgPivotY": 0.0, "__smartColor": "#ADADB5", "__bgPos": None, "externalRelPath": None,
             "fieldInstances": [], "layerInstances": [], "__neighbours": []}
    if background is not None:
        relative, info = background
        level.update(bgRelPath=relative, bgPos="Unscaled",
                     __bgPos={"topLeftPx": [0, 0], "scale": [1, 1],
                              "cropRect": [0, 0, min(info.size[0], level["pxWid"]),
                                           min(info.size[1], level["pxHei"])]})

    expected_entities: dict[str, dict[str, Any]] = {}
    expected_tiles: dict[str, dict[str, Any]] = {}
    rounding: list[str] = []

    def place(kind: str, ident: str, x: float, y: float) -> tuple[int, int]:
        px = (_round(x), _round(y))
        if abs(px[0] - x) > 1e-9 or abs(px[1] - y) > 1e-9:
            rounding.append(f"{kind} {ident} ({x:g}, {y:g}) rounded to ({px[0]}, {px[1]})")
        return px

    markers = []
    for spawn in bundle.data.get("spawns") or []:
        px = place("spawn", spawn["id"], spawn["x"], spawn["y"])
        key = f"spawn:{spawn['id']}"
        markers.append(_entity(marker_defs["spawn"], px, (grid, grid), grid,
                               {"BundleId": spawn["id"], "Facing": None if "facing" not in spawn
                                else str(spawn["facing"])}, key))
        expected_entities[key] = {"x": spawn["x"], "y": spawn["y"], "iid": markers[-1]["iid"]}
    for portal in bundle.data.get("portals") or []:
        if "rect" in portal:
            x, y, w, h = portal["rect"]
            shape = "rect"
        else:
            cx, cy, radius = portal["circle"]
            x, y, w, h, shape = cx - radius, cy - radius, 2 * radius, 2 * radius, "circle"
        px = place("portal", portal["id"], x, y)
        size = (max(1, _round(w)), max(1, _round(h)))
        travel = portal.get("travelDirection") or [None, None]
        key = f"portal:{portal['id']}"
        markers.append(_entity(marker_defs["portal"], px, size, grid, {
            "BundleId": portal["id"], "To": portal["to"], "Shape": shape, "Activation": portal.get("activation"),
            "TravelX": travel[0], "TravelY": travel[1], "Radius": portal.get("radius"),
            "Latch": portal.get("latch"), "RequiresMovement": portal.get("requiresMovement"),
            "EntranceByFrom": None if "entranceByFrom" not in portal else
            json.dumps(portal["entranceByFrom"], sort_keys=True, ensure_ascii=False)}, key))
        expected_entities[key] = {"x": x, "y": y, "w": w, "h": h, "iid": markers[-1]["iid"]}
    for item in bundle.data.get("interactions") or []:
        px = place("interaction", item["id"], item["x"], item["y"])
        key = f"interaction:{item['id']}"
        markers.append(_entity(marker_defs["interaction"], px, (grid, grid), grid,
                               {"BundleId": item["id"], "Reach": item.get("reach")}, key))
        expected_entities[key] = {"x": item["x"], "y": item["y"], "iid": markers[-1]["iid"]}
    for anchor_name, anchor in (bundle.data.get("anchors") or {}).items():
        x, y = anchor["point"]
        px = place("anchor", anchor_name, x, y)
        key = f"anchor:{anchor_name}"
        markers.append(_entity(marker_defs["anchor"], px, (grid, grid), grid, {
            "BundleId": anchor_name, "Facing": None if "facing" not in anchor else str(anchor["facing"]),
            "Slots": None if "slots" not in anchor else json.dumps(anchor["slots"]),
            "Approach": None if "approach" not in anchor else json.dumps(anchor["approach"])}, key))
        expected_entities[key] = {"x": x, "y": y, "iid": markers[-1]["iid"]}
    markers_entry = (_layer_def(uids, ldtk_identifier("Markers", names["layers"]), "Entities", grid),
                     {"entities": markers, "key": "markers"})

    def objects_entry(layer_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        instances = []
        for item in bundle.objects:
            info = item["_image"]
            definition = prop_defs[(str(item.get("prop") or item["id"]), info.sha256, float(item["anchor_px"][0]),
                                    float(item["anchor_px"][1]))]
            scale = float(item.get("scale", 1))
            px = place("object", item["id"], item["x"], item["y"])
            key = f"object:{item['id']}"
            size = (max(1, _round(info.size[0] * scale)), max(1, _round(info.size[1] * scale)))
            instances.append(_entity(definition, px, size, grid, {
                "BundleId": item["id"], "Prop": item.get("prop"), "Scale": scale, "SortY": item.get("sortY"),
                "Solid": item.get("solid"), "Occlusion": item.get("occlusion"),
                "Footprint": None if "footprint" not in item else json.dumps(item["footprint"], sort_keys=True)},
                key))
            expected_entities[key] = {"x": item["x"], "y": item["y"], "iid": instances[-1]["iid"]}
        definition = _layer_def(uids, ldtk_identifier(layer_name, names["layers"]), "Entities", grid)
        return definition, {"entities": instances, "key": f"layer:{layer_name}"}

    def tiles_entry(layer: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        info = layer["tiles"]
        tileset_def = tileset_defs[info.tileset.id]
        level_columns = math.ceil(level["pxWid"] / grid)
        tiles = []
        for row, col in zip(*np.nonzero(info.grid >= 0)):
            ax, ay = info.tileset.atlas(int(info.grid[row, col]))
            tiles.append({"px": [int(col) * grid, int(row) * grid], "src": [ax * grid, ay * grid], "f": 0,
                          "t": ax + ay * tileset_def["__cWid"], "d": [int(col) + int(row) * level_columns], "a": 1})
        definition = _layer_def(uids, ldtk_identifier(layer["name"], names["layers"]), "Tiles", grid,
                                tileset_def["uid"])
        expected_tiles[definition["identifier"]] = {"grid": info.grid, "tileset": info.tileset}
        return definition, {"tiles": tiles, "tileset": tileset_def, "key": f"layer:{layer['name']}"}

    bottom_up: list[tuple[dict[str, Any], dict[str, Any]]] = []
    objects_done = False
    for layer in bundle.layers:
        if layer["kind"] == "tiles":
            bottom_up.append(tiles_entry(layer))
        elif layer["kind"] == "objects" and not objects_done:
            bottom_up.append(objects_entry(layer["name"]))
            objects_done = True
    if bundle.objects and not objects_done:
        bottom_up.append(objects_entry("Objects"))
    # bundle layers are listed bottom-up; LDtk lists the top layer first, with Markers on top of everything
    ordered = [markers_entry] + bottom_up[::-1]
    level["layerInstances"] = [_layer_instance(definition, level, tiles=extra.get("tiles"),
                                               entities=extra.get("entities"), tileset=extra.get("tileset"),
                                               key=extra["key"]) for definition, extra in ordered]
    tilesets = list(tileset_defs.values()) + ([props_tileset] if props_tileset else [])
    project = {
        "__header__": {"fileType": "LDtk Project JSON", "app": "LDtk", "doc": "https://ldtk.io/json",
                       "schema": "https://ldtk.io/files/JSON_SCHEMA.json", "appAuthor": "Sebastien 'deepnight' Benard",
                       "appVersion": LDTK_VERSION, "url": "https://ldtk.io"},
        "iid": _iid("project", bundle.sha256, name), "jsonVersion": LDTK_VERSION, "appBuildId": 0,
        "nextUid": uids.next, "identifierStyle": "Capitalize", "toc": [], "worldLayout": "Free",
        "worldGridWidth": 256, "worldGridHeight": 256, "defaultLevelWidth": level["pxWid"],
        "defaultLevelHeight": level["pxHei"], "defaultPivotX": 0, "defaultPivotY": 0, "defaultGridSize": grid,
        "defaultEntityWidth": grid, "defaultEntityHeight": grid, "bgColor": "#40465B",
        "defaultLevelBgColor": LEVEL_BG, "minifyJson": False, "externalLevels": False, "exportTiled": False,
        "simplifiedExport": False, "imageExportMode": "None", "exportLevelBg": True, "pngFilePattern": None,
        "backupOnSave": False, "backupLimit": 10, "backupRelPath": None, "levelNamePattern": "Level_%idx",
        "tutorialDesc": None, "customCommands": [], "flags": [],
        "defs": {"layers": [definition for definition, _ in ordered], "entities": entity_defs, "tilesets": tilesets,
                 "enums": [], "externalEnums": [], "levelFields": []},
        "levels": [level], "worlds": [], "dummyWorldIid": _iid("world", bundle.sha256, name),
    }
    context = {"grid": grid, "expected_entities": expected_entities, "expected_tiles": expected_tiles,
               "rounding": rounding, "assets": assets, "warnings": warnings, "atlas_positions": atlas_positions,
               "distinct_props": distinct, "props_tileset": props_tileset}
    return project, context


# --------------------------------------------------------------------------- QA (read back)

def _check(ident: str, status: str, value: Any = None, threshold: Any = None) -> dict[str, Any]:
    return {"id": ident, "status": status, "value": value, "threshold": threshold}


def qa_project(project: dict[str, Any], context: dict[str, Any], stage: Path) -> list[dict[str, Any]]:
    checks = []
    spec = ldtk_spec_errors(project)
    checks.append(_check("ldtk_required_fields", "fail" if spec else "pass", spec[:20],
                         {"ldtk_json": LDTK_VERSION, "types": len(LDTK_REQUIRED)}))
    ids = ldtk_id_errors(project) if not spec else ["skipped: required fields missing"]
    checks.append(_check("ldtk_ids", "fail" if ids else "pass", ids[:20]))
    level = project["levels"][0]
    grid = context["grid"]
    tile_problems = []
    tilesets = {item["uid"]: item for item in project["defs"]["tilesets"]}
    for layer in level["layerInstances"]:
        if layer["__type"] != "Tiles":
            continue
        expected = context["expected_tiles"].get(layer["__identifier"])
        tileset = tilesets.get(layer["__tilesetDefUid"])
        if expected is None or tileset is None:
            tile_problems.append(f"layer {layer['__identifier']} has no bundle layer or tileset")
            continue
        got = np.full(expected["grid"].shape, -1, np.int64)
        for tile in layer["gridTiles"]:
            col, row = tile["px"][0] // grid, tile["px"][1] // grid
            ax, ay = tile["src"][0] // grid, tile["src"][1] // grid
            if tile["t"] != ax + ay * tileset["__cWid"] or tile["d"] != [col + row * layer["__cWid"]]:
                tile_problems.append(f"layer {layer['__identifier']} tile at {tile['px']} has inconsistent t or d")
            got[row, col] = ay * expected["tileset"].columns + ax
        if not np.array_equal(got, expected["grid"]):
            tile_problems.append(f"layer {layer['__identifier']}: {int((got != expected['grid']).sum())} cells "
                                 f"differ from the bundle")
    checks.append(_check("tiles_roundtrip", "fail" if tile_problems else "pass", tile_problems))
    found = {entity["iid"]: entity for layer in level["layerInstances"] for entity in layer["entityInstances"]}
    worst, mismatched = 0.0, []
    entities = {item["uid"]: item for item in project["defs"]["entities"]}
    for ident, expected in context["expected_entities"].items():
        entity = found.get(expected["iid"])
        if entity is None:
            mismatched.append(f"{ident} is missing")
            continue
        error = max(abs(entity["px"][0] - expected["x"]), abs(entity["px"][1] - expected["y"]))
        if "w" in expected:
            error = max(error, abs(entity["width"] - expected["w"]), abs(entity["height"] - expected["h"]))
        if error > 0.5 + 1e-9 or entity["__worldX"] != entity["px"][0] or entity["__worldY"] != entity["px"][1] \
                or entities[entity["defUid"]]["identifier"] != entity["__identifier"]:
            mismatched.append(f"{ident}: LDtk px {entity['px']} vs bundle ({expected['x']:g}, {expected['y']:g})")
        worst = max(worst, error)
    status = "fail" if mismatched else ("warn" if context["rounding"] else "pass")
    checks.append(_check("entity_positions", status, mismatched or context["rounding"],
                         {"max_rounding_px": 0.5, "measured_px": round(worst, 6)}))
    changed = [record["path"] for record in context["assets"]
               if forge_core.sha256_file(stage / record["path"]) != record["sha256"]]
    if context["distinct_props"]:
        atlas = np.asarray(Image.open(stage / "assets" / "props-atlas.png").convert("RGBA"))
        for sha, info in context["distinct_props"].items():
            x, y = context["atlas_positions"][sha]
            source = np.asarray(forge_core.load_rgba(info.path)[0])
            region = atlas[y:y + info.size[1], x:x + info.size[0]]
            if not (np.array_equal(region[..., 3], source[..., 3])
                    and np.array_equal(region[..., :3][source[..., 3] > 0], source[..., :3][source[..., 3] > 0])):
                changed.append(f"props-atlas.png region of {info.path.name}")
    checks.append(_check("assets_identical", "fail" if changed else "pass", changed))
    return checks


def export(args: argparse.Namespace) -> dict[str, Any]:
    bundle = _local_read_bundle(Path(args.bundle), [Path(path) for path in args.prop_pack or []])
    name = _local_safe_name(args.name, set(), "map")
    outside = _local_world_warnings(bundle)
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        project, context = build_project(bundle, name, stage)
        project_path = stage / f"{name}.ldtk"
        forge_core.write_json(project_path, project)
        reread = json.loads(project_path.read_text(encoding="utf-8"))
        checks = qa_project(reread, context, stage)
        checks.append(_check("objects_in_world", "warn" if outside else "pass", outside,
                             {"world": list(bundle.world)}))
        failed = [check["id"] for check in checks if check["status"] == "fail"]
        if failed:
            raise ValueError(f"QA failed ({', '.join(failed)}): " + "; ".join(
                str(item) for check in checks if check["status"] == "fail" for item in (check["value"] or [])[:2]))
        warned = [check["id"] for check in checks if check["status"] == "warn"]
        if args.strict_qc and warned:
            raise ValueError(f"strict QC failed ({', '.join(warned)}); nothing was written.")
        outputs = [_local_file_ref(project_path, stage)]
        if context["distinct_props"]:
            outputs.append(_local_file_ref(stage / "assets" / "props-atlas.png", stage))
        outputs += [_local_file_ref(stage / record["path"], stage, record["sha256"]) for record in context["assets"]]
        status = "warn" if warned else "pass"
        level = project["levels"][0]
        not_exported = _local_not_exported(bundle)
        if bundle.data.get("collision"):
            not_exported.append("collision: walk regions and solids stay in the map bundle (per-tile collision "
                                "is in tileset customData)")
        report = {
            "schema": REPORT_SCHEMA, "tool": dict(TOOL),
            "engine": {"name": "ldtk", "target": LDTK_VERSION, "format": "json",
                       "verified": "parse-level (required-field snapshot and read-back by this tool); LDtk editor "
                                   "not run"},
            "bundle": _local_file_ref(bundle.path, final, bundle.sha256),
            "files": {"project": f"{name}.ldtk", "props_atlas": "assets/props-atlas.png"
                      if context["distinct_props"] else None},
            "assets": context["assets"],
            "tilesets": [{"id": item["identifier"], "uid": item["uid"], "relPath": item["relPath"],
                          "tile": item["tileGridSize"], "custom_tiles": len(item["customData"])}
                         for item in project["defs"]["tilesets"]],
            "layers": [{"identifier": layer["__identifier"], "type": layer["__type"],
                        "tiles": len(layer["gridTiles"]), "entities": len(layer["entityInstances"])}
                       for layer in level["layerInstances"]],
            "level": {"identifier": level["identifier"], "size": [level["pxWid"], level["pxHei"]],
                      "background": level["bgRelPath"]},
            "notExported": not_exported,
            "warnings": context["warnings"],
            "qa": {"status": status,
                   "method": "export_ldtk: built the project, wrote it, re-read the JSON, checked it against the "
                             f"LDtk {LDTK_VERSION} required-field snapshot, uid/iid/identifier rules, decoded every "
                             "grid tile and compared entity positions and sizes with the bundle.",
                   "notProven": list(NOT_PROVEN), "checks": checks,
                   "inputs": [_local_file_ref(bundle.path, final, bundle.sha256)], "outputs": outputs,
                   "tool": dict(TOOL)},
        }
        forge_core.write_json(stage / "ldtk-export.json", report)
    return {"output_dir": str(final.resolve()), "project": str((final / f"{name}.ldtk").resolve()),
            "metadata": str((final / "ldtk-export.json").resolve()), "status": status,
            "entities": sum(len(layer["entityInstances"]) for layer in level["layerInstances"]),
            "tiles": sum(len(layer["gridTiles"]) for layer in level["layerInstances"]),
            "_warnings": context["warnings"] + context["rounding"] + outside}


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="map_bundle.v2 JSON.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New folder for the LDtk files; must not exist.")
    parser.add_argument("--name", default="map", help="Project file stem and level identifier (default map).")
    parser.add_argument("--prop-pack", type=Path, action="append",
                        help="prop-pack.json whose accepted labels supply prop images (repeatable).")
    parser.add_argument("--strict-qc", action="store_true",
                        help="Publish nothing when a QA check warns (rounded positions, things outside the world).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        summary = export(args)
    except (ValueError, OSError, Image.DecompressionBombError) as error:
        print(f"error: {forge_core.ascii_text(str(error) or type(error).__name__)}", file=sys.stderr)
        return 1
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
