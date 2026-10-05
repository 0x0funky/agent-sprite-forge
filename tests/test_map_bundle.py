"""map_bundle.py (plan B13-T1): map_bundle.v2 validation with v1 still readable, the
standard-library JSON Schema evaluator, files and sha256, cross-field rules, the reference
render, world solids, the hash verb and the CLI conventions.

write_demo_bundle() builds the synthetic, deterministic bundle that test_map_nav.py and
test_export_tiled.py also use: a wang-corner terrain tileset, a blob-47 path tileset, an
image layer, props with footprints (scaled and rotated), collision, a material map,
portals (intent and crossing), spawns, an anchor with slots and interactions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (
    FIXTURES_DIR, SKILLS_DIR, assert_cli_help, assert_valid_contract, contract_errors, load_script, run_cli,
    script_path,
)

SKILL = "generate2dmap"
mb = load_script(SKILL, "map_bundle")
TOOL = script_path(SKILL, "map_bundle")
SCHEMA_DIR = SKILLS_DIR / SKILL / "references" / "schemas"
T = 16  # demo tile size
COLS, ROWS = 12, 8  # demo map in tiles: 192 x 128 px
GRASS, WATER = (90, 163, 68), (44, 98, 171)
PATH_RGB = (199, 157, 98)


# --------------------------------------------------------------------------- the demo bundle

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(image: Image.Image | np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    (image if isinstance(image, Image.Image) else Image.fromarray(np.asarray(image, np.uint8))).save(path)
    return path


def wang_atlas() -> np.ndarray:
    """16 opaque corner tiles, index = tl*8 + tr*4 + bl*2 + br (1 = grass, 0 = water), 4 per row."""
    atlas = np.zeros((4 * T, 4 * T, 4), np.uint8)
    yy, xx = np.mgrid[0:T, 0:T]
    for index in range(16):
        corners = [(index >> 3) & 1, (index >> 2) & 1, (index >> 1) & 1, index & 1]
        tile = np.zeros((T, T, 4), np.uint8)
        for q, (ys, xs) in enumerate(((slice(0, 8), slice(0, 8)), (slice(0, 8), slice(8, 16)),
                                      (slice(8, 16), slice(0, 8)), (slice(8, 16), slice(8, 16)))):
            tile[ys, xs, :3] = GRASS if corners[q] else WATER
        tile[..., :3] = np.clip(tile[..., :3].astype(int) + ((xx * 3 + yy * 5 + index) % 7)[..., None] * 3, 0, 255)
        tile[..., 3] = 255
        row, col = divmod(index, 4)
        atlas[row * T:(row + 1) * T, col * T:(col + 1) * T] = tile
    return atlas


def canonical_blob(mask: int) -> int:
    for diagonal, first, second in ((1, 0, 2), (3, 4, 2), (5, 4, 6), (7, 0, 6)):
        if not (mask >> first & 1 and mask >> second & 1):
            mask &= ~(1 << diagonal)
    return mask


BLOB_MASKS = sorted({canonical_blob(m) for m in range(256)})  # the 47 canonical masks


def blob_atlas() -> np.ndarray:
    """47 transparent path tiles (8 per row): a centre square, an arm per edge, filled diagonals."""
    atlas = np.zeros((6 * T, 8 * T, 4), np.uint8)
    parts = {0: (slice(0, 4), slice(4, 12)), 2: (slice(4, 12), slice(12, 16)), 4: (slice(12, 16), slice(4, 12)),
             6: (slice(4, 12), slice(0, 4)), 1: (slice(0, 4), slice(12, 16)), 3: (slice(12, 16), slice(12, 16)),
             5: (slice(12, 16), slice(0, 4)), 7: (slice(0, 4), slice(0, 4))}
    for k, mask in enumerate(BLOB_MASKS):
        tile = np.zeros((T, T, 4), np.uint8)
        tile[4:12, 4:12] = (*PATH_RGB, 255)
        for bit, (ys, xs) in parts.items():
            if mask >> bit & 1:
                tile[ys, xs] = (*PATH_RGB, 255) if bit % 2 == 0 else (170, 130, 80, 255)
        row, col = divmod(k, 8)
        atlas[row * T:(row + 1) * T, col * T:(col + 1) * T] = tile
    return atlas


def prop_image(width: int, height: int, colour: tuple[int, int, int], seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.zeros((height, width, 4), np.uint8)
    image[2:, 2:width - 2, :3] = colour
    image[2:, 2:width - 2, 3] = 255
    noise = rng.integers(0, 30, (height - 2, width - 4, 1))
    image[2:, 2:width - 2, :3] = np.clip(image[2:, 2:width - 2, :3] + noise, 0, 255)
    return image


def vertex_grid() -> np.ndarray:
    grid = np.ones((ROWS + 1, COLS + 1), np.int64)
    grid[1:3, 1:4] = 0  # a pond in the north-west
    return grid


def path_cells() -> np.ndarray:
    cells = np.zeros((ROWS, COLS), bool)
    cells[5, 1:11] = True
    cells[3:6, 6] = True
    return cells


def write_demo_bundle(root: Path, *, hashes: bool = True, name: str = "map-bundle.json") -> Path:
    """Write the synthetic demo map (192 x 128 px, 16 px tiles) and return its bundle path."""
    root.mkdir(parents=True, exist_ok=True)
    terrain_png = save(wang_atlas(), root / "tiles" / "terrain.png")
    tiles = []
    for index in range(16):
        tl, tr, bl, br = (index >> 3) & 1, (index >> 2) & 1, (index >> 1) & 1, index & 1
        quadrants = [(0, 0), (8, 0), (0, 8), (8, 8)]
        collision = [{"shape": "rect", "x": x, "y": y, "w": 8, "h": 8}
                     for corner, (x, y) in zip((tl, tr, bl, br), quadrants) if corner == 0]
        tiles.append({"index": index, "wang": [tl, tr, bl, br], "collision": collision,
                      "properties": {"walkable": not collision}})
    terrain = {"schema": "generate2dmap.tileset.v1", "image": "terrain.png", "sha256": sha(terrain_png),
               "tile_size": T, "columns": 4, "kind": "wang_corner", "materials": ["water", "grass"],
               "tiles": tiles, "seamless_verified": False}
    (root / "tiles" / "terrain.tileset.json").write_text(json.dumps(terrain, indent=1), encoding="utf-8")
    path_png = save(blob_atlas(), root / "tiles" / "path.png")
    path_set = {"schema": "generate2dmap.tileset.v1", "image": "path.png", "sha256": sha(path_png),
                "tile_size": [T, T], "columns": 8, "kind": "blob47", "materials": ["grass", "path"],
                "tiles": [{"index": k, "blob_mask": mask, "properties": {"walkable": True}}
                          for k, mask in enumerate(BLOB_MASKS)], "seamless_verified": False}
    (root / "tiles" / "path.tileset.json").write_text(json.dumps(path_set, indent=1), encoding="utf-8")

    vertices = vertex_grid()
    ground = [[int(vertices[y, x] * 8 + vertices[y, x + 1] * 4 + vertices[y + 1, x] * 2 + vertices[y + 1, x + 1])
               for x in range(COLS)] for y in range(ROWS)]
    (root / "layers").mkdir(exist_ok=True)
    (root / "layers" / "ground.csv").write_text("\n".join(",".join(map(str, row)) for row in ground) + "\n",
                                                encoding="utf-8")
    cells = np.pad(path_cells(), 1)
    decoration = []
    for y in range(ROWS):
        row = []
        for x in range(COLS):
            if not cells[y + 1, x + 1]:
                row.append(-1)
                continue
            neighbours = [cells[y, x + 1], cells[y, x + 2], cells[y + 1, x + 2], cells[y + 2, x + 2],
                          cells[y + 2, x + 1], cells[y + 2, x], cells[y + 1, x], cells[y, x]]
            mask = canonical_blob(sum(1 << bit for bit, on in enumerate(neighbours) if on))
            row.append(BLOB_MASKS.index(mask))
        decoration.append(row)
    (root / "terrain-vertices.json").write_text(json.dumps(vertices.tolist()), encoding="utf-8")

    backdrop = np.zeros((ROWS * T, COLS * T, 4), np.uint8)
    backdrop[..., 0] = np.linspace(20, 120, COLS * T, dtype=np.uint8)[None, :]
    backdrop[..., 3] = 255
    save(backdrop, root / "art" / "backdrop.png")
    save(prop_image(24, 32, (40, 120, 50), 1), root / "props" / "tree.png")
    save(prop_image(16, 12, (120, 120, 130), 2), root / "props" / "rock.png")
    save(prop_image(14, 10, (70, 150, 60), 3), root / "props" / "bush.png")
    materials = np.zeros((ROWS, COLS, 4), np.uint8)
    materials[...] = (0, 128, 0, 255)
    materials[7, 10:12] = (200, 30, 30, 255)  # lava in the south-east corner
    materials[0, 6] = (30, 30, 200, 255)  # a shallow, walkable pool
    save(materials, root / "materials.png")

    bundle = {
        "schema": "generate2dmap.map_bundle.v2",
        "id": "meadow",
        "tile_size": [T, T],
        "world": {"width": COLS * T, "height": ROWS * T, "unit": "px"},
        "terrain": {"vertex_grid": "terrain-vertices.json", "materials": ["water", "grass"]},
        "tilesets": [{"id": "terrain", "manifest": "tiles/terrain.tileset.json"},
                     {"id": "path", "manifest": "tiles/path.tileset.json"}],
        "layers": [{"name": "backdrop", "kind": "image", "image": "art/backdrop.png"},
                   {"name": "ground", "kind": "tiles", "data": "layers/ground.csv", "tileset": "terrain"},
                   {"name": "decoration", "kind": "tiles", "data": decoration, "tileset": "path"},
                   {"name": "props", "kind": "objects"}],
        "props": {
            "tree": {"image": "props/tree.png", "anchor_px": [12, 30],
                     "footprint": {"shape": "ellipse", "width": 8, "depth": 4}, "solid": True,
                     "occlusion_class": "tall", "occupant_policy": "y_sort"},
            "rock": {"image": "props/rock.png", "anchor_px": [8, 11],
                     "footprint": {"shape": "rect", "width": 10, "depth": 6, "rotate": 30}, "solid": True,
                     "occlusion_class": "low"},
            "bush": {"image": "props/bush.png", "anchor_px": [7, 9], "solid": False, "occlusion_class": "low"},
        },
        "objects": [
            {"id": "tree-1", "prop": "tree", "x": 40, "y": 112, "anchor_px": [12, 30], "sortY": 112},
            {"id": "tree-2", "prop": "tree", "x": 156, "y": 40, "scale": 2, "anchor_px": [12, 30], "sortY": 40,
             "occluder": {"alphaThreshold": 16, "source": "props/tree.png"}},
            {"id": "rock-1", "prop": "rock", "x": 112, "y": 62, "anchor_px": [8, 11]},
            {"id": "bush-1", "prop": "bush", "x": 128, "y": 104, "anchor_px": [7, 9]},
            {"id": "bush-2", "prop": "bush", "x": 132, "y": 104, "anchor_px": [7, 9]},
        ],
        "collision": {
            "actorRadius": 4,
            "ySquash": 1.0,
            "solids": [{"shape": "rect", "x": 120, "y": 0, "w": 6, "h": 30},
                       {"shape": "ellipse", "cx": 96, "cy": 26, "rx": 6, "ry": 4, "id": "well-base"}],
            "rects": [[0, 120, 20, 8]],
        },
        "material_map": {"image": "materials.png", "materials": {
            "meadow": {"class": "decor", "color": "#008000"},
            "lava": {"class": "hazard", "color": [200, 30, 30]},
            "pool": {"class": "liquid", "color": "#1e1ec8", "walkable": True},
        }},
        "portals": [
            {"id": "exit-east", "rect": [184, 48, 8, 32], "to": "road:arrive-west", "activation": "intent",
             "travelDirection": [1, 0], "radius": 6, "entranceByFrom": {"road": "arrive-east"}, "latch": True,
             "requiresMovement": True},
            {"id": "cellar", "circle": [72, 64, 5], "to": "meadow:start", "activation": "crossing"},
        ],
        "spawns": [{"id": "start", "x": 24, "y": 70, "facing": "east"},
                   {"id": "arrive-east", "x": 170, "y": 64, "facing": "west"}],
        "anchors": {"well": {"point": [96, 26], "facing": "north", "slots": [[86, 44], {"x": 106, "y": 44}],
                             "approach": [96, 44]}},
        "interactions": [{"id": "well", "x": 96, "y": 26, "reach": 20},
                         {"id": "sign", "x": 64, "y": 100, "reach": 10}],
        "camera": {"bounds": [0, 0, COLS * T, ROWS * T]},
        "qa": {},
        "art_source": "code",
    }
    path = root / name
    path.write_text(json.dumps(bundle, indent=1), encoding="utf-8")
    if hashes:
        doc = mb._local_read_json(path)
        for container, key, _, sha_key in mb.path_fields(doc):
            if sha_key:
                container[sha_key] = sha(root / container[key])
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- schema additions (handoff section 5)

SCHEMA_PATCH = {
    "defs": {
        "bundleProp": {
            "description": "A prop of the bundle's props registry: an image (relative to the bundle) with its anchor, "
                           "or a prop_pack_v2 item (pack + label, its paths relative to that pack); fields given here "
                           "override the pack item. sha256 is the image's.",
            "type": "object",
            "properties": {
                "image": {"$ref": "common.schema.json#/$defs/relPath"},
                "sha256": {"$ref": "common.schema.json#/$defs/sha256"},
                "pack": {"$ref": "common.schema.json#/$defs/relPath"},
                "label": {"type": "string", "minLength": 1},
                "anchor_px": {"$ref": "common.schema.json#/$defs/point2"},
                "footprint": {"$ref": "#/$defs/footprint"},
                "solid": {"type": "boolean"},
                "contact": True,
                "occlusion_class": {"enum": ["low", "tall", "foreground"]},
                "occupant_policy": {"enum": ["y_sort", "rear_shift_and_fade", "static_front", "static_back"]},
            },
            "anyOf": [{"required": ["image"]}, {"required": ["pack", "label"]}],
        },
        "nav_grid_v1": {
            "description": "map_nav.py navigation grid (plan Appendix C). Node (col, row) sits at ((col + 0.5) * "
                           "cell, (row + 0.5) * cell). moves has one character per node: '#' for a blocked node, "
                           "else a hex digit of its open moves (E=1, S=2 (+y), W=4, N=8). reachable marks nodes "
                           "reached from the spawns and arrivals. blockedRects are disjoint [x, y, w, h] rectangles "
                           "in world px whose union is exactly the blocked nodes.",
            "type": "object",
            "required": ["schema", "map", "world", "actor", "cell", "cols", "rows", "moves", "reachable",
                         "blockedRects"],
            "properties": {
                "schema": {"const": "generate2dmap.nav_grid.v1"},
                "map": {"type": "string", "minLength": 1},
                "bundle": {"$ref": "common.schema.json#/$defs/fileRef"},
                "world": {"type": "object", "required": ["width", "height"],
                          "properties": {"width": {"type": "number", "exclusiveMinimum": 0},
                                         "height": {"type": "number", "exclusiveMinimum": 0}}},
                "actor": {"type": "object", "required": ["radius", "ySquash", "samples"],
                          "properties": {"radius": {"type": "number", "minimum": 0},
                                         "ySquash": {"type": "number", "exclusiveMinimum": 0},
                                         "samples": {"type": "array", "minItems": 9, "maxItems": 9,
                                                     "items": {"$ref": "common.schema.json#/$defs/point2"}}}},
                "cell": {"type": "integer", "minimum": 1},
                "cols": {"type": "integer", "minimum": 1},
                "rows": {"type": "integer", "minimum": 1},
                "moves": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^[0-9a-f#]+$"}},
                "reachable": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^[01]+$"}},
                "blockedRects": {"type": "array", "items": {"$ref": "common.schema.json#/$defs/rectXYWH"}},
            },
        },
        "map_bundle_report_v1": {
            "description": "map_bundle.py validate report: a QA envelope plus the bundle facts and every problem.",
            "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
            "required": ["schema", "problems"],
            "properties": {"schema": {"const": "generate2dmap.map_bundle_report.v1"}, "problems": {"type": "array"}},
        },
        "nav_report_v1": {
            "description": "map_nav.py check report: a QA envelope plus starts, targets, links, thin gaps and pockets.",
            "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
            "required": ["schema", "targets"],
            "properties": {"schema": {"const": "generate2dmap.nav_report.v1"}, "targets": {"type": "array"}},
        },
        "tiled_export_v1": {
            "description": "export_tiled.py export report: a QA envelope plus the Tiled tilesets and layers written.",
            "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
            "required": ["schema", "tiled"],
            "properties": {"schema": {"const": "generate2dmap.tiled_export.v1"}, "tiled": {"type": "object"}},
        },
    },
    "properties": {
        "/$defs/map_bundle_v2": {
            "id": {"type": "string", "minLength": 1, "pattern": "^[^:]+$",
                   "description": "Map id used by portal targets (map:spawn). Default: the bundle file name without "
                                  ".json and a .map-bundle, -bundle or .bundle suffix; a file named map-bundle.json "
                                  "takes its folder name."},
            "props": {"type": "object", "additionalProperties": {"$ref": "#/$defs/bundleProp"}},
        },
        "/$defs/map_bundle_v2/properties/terrain": {"sha256": {"$ref": "common.schema.json#/$defs/sha256"}},
        "/$defs/map_bundle_v2/properties/material_map": {"sha256": {"$ref": "common.schema.json#/$defs/sha256"}},
        "/$defs/map_bundle_v2/properties/nav": {"sha256": {"$ref": "common.schema.json#/$defs/sha256"}},
        "/$defs/mapLayer": {"offset": {"$ref": "common.schema.json#/$defs/point2"}},
        "/$defs/mapObject": {
            "layer": {"type": "string", "minLength": 1},
            "occupant_policy": {"enum": ["y_sort", "rear_shift_and_fade", "static_front", "static_back"]},
        },
        "/$defs/mapObject/properties/occluder": {"sha256": {"$ref": "common.schema.json#/$defs/sha256"}},
        "/$defs/portal": {"reciprocal": {"type": "boolean"}},
    },
}


def patched_map_schema() -> dict:
    """The vendored map schema with SCHEMA_PATCH applied (what handoff section 5 asks for)."""
    schema = json.loads((SCHEMA_DIR / "map.schema.json").read_text(encoding="utf-8"))
    schema["$defs"].update(copy.deepcopy(SCHEMA_PATCH["defs"]))
    for pointer, additions in SCHEMA_PATCH["properties"].items():
        node = schema
        for part in pointer.split("/")[1:]:
            node = node[part]
        assert not set(additions) & set(node.setdefault("properties", {})), f"{pointer} already defines {additions}"
        node["properties"].update(copy.deepcopy(additions))
    return schema


def patched_errors(instance, name: str) -> list[str]:
    """Validate against the vendored map schema with SCHEMA_PATCH applied in memory."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    common = json.loads((SCHEMA_DIR / "common.schema.json").read_text(encoding="utf-8"))
    patched = patched_map_schema()
    registry = Registry().with_resources((s["$id"], DRAFT202012.create_resource(s)) for s in (common, patched))
    validator = Draft202012Validator({"$ref": f"{patched['$id']}#/$defs/{name}"}, registry=registry)
    return [f"{e.json_path}: {e.message}" for e in validator.iter_errors(instance)]


def assert_patched_valid(instance, name: str) -> None:
    errors = patched_errors(instance, name)
    assert not errors, "\n".join(errors)


def test_schema_patch_is_valid_and_additive():
    from jsonschema import Draft202012Validator

    patched = patched_map_schema()
    Draft202012Validator.check_schema(patched)
    vendored = json.loads((SCHEMA_DIR / "map.schema.json").read_text(encoding="utf-8"))
    assert set(vendored["$defs"]) < set(patched["$defs"])
    assert all(definition.get("description") for definition in SCHEMA_PATCH["defs"].values())
    for path in sorted(FIXTURES_DIR.glob("contracts/map.*.json")):  # every valid A0 map fixture stays valid
        _, name, kind = path.name[:-5].split(".", 2)
        if kind != "invalid":
            assert not patched_errors(json.loads(path.read_text(encoding="utf-8")), name), path.name
    assert patched_errors({"schema": "generate2dmap.map_bundle.v1", "id": "a:b"}, "map_bundle_v2")
    assert patched_errors({"schema": "generate2dmap.map_bundle.v1", "props": {"x": {"label": "y"}}}, "map_bundle_v2")


# --------------------------------------------------------------------------- helpers

def demo(tmp_path: Path, **kwargs) -> Path:
    return write_demo_bundle(tmp_path / "map", **kwargs)


def edit(path: Path, change) -> Path:
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def errors_of(bundle) -> list[str]:
    return [f"{p.path}: {p.message}" for p in bundle.errors]


# --------------------------------------------------------------------------- B13-T1 acceptance

def test_v2_fixture_valid(tmp_path):
    path = demo(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert_valid_contract(raw, "map", "map_bundle_v2", skill=SKILL)
    assert_patched_valid(raw, "map_bundle_v2")
    for name in ("terrain", "path"):
        tileset = json.loads((path.parent / "tiles" / f"{name}.tileset.json").read_text(encoding="utf-8"))
        assert_valid_contract(tileset, "map", "tileset_v1", skill=SKILL)

    bundle = mb.load_bundle(path, require_sha256=True)
    assert bundle.problems == [], [p.as_dict() for p in bundle.problems]
    assert bundle.readable and bundle.version == 2 and bundle.id == "meadow"
    assert (bundle.width, bundle.height, bundle.tile_w, bundle.tile_h) == (192, 128, 16, 16)
    assert [layer.kind for layer in bundle.layers] == ["image", "tiles", "tiles", "objects"]
    assert bundle.tilesets["terrain"].kind == "wang_corner" and bundle.tilesets["path"].tile_count == 48
    assert len(bundle.tilesets["path"].tiles) == 47
    assert [o.id for o in bundle.objects] == ["tree-1", "tree-2", "rock-1", "bush-1", "bush-2"]
    tree2 = bundle.objects[1]
    assert (tree2.scale, tree2.solid, tree2.sort_y, tree2.layer, tree2.occlusion) == (2.0, True, 40.0, "props", "tall")
    assert bundle.objects[3].solid is False  # bush: no footprint, solid false in the registry
    assert bundle.material.scale == 16 and bundle.material.names == ["meadow", "lava", "pool"]
    exit_east = bundle.portals[0]
    assert (exit_east.activation, exit_east.travel, exit_east.radius) == ("intent", (1.0, 0.0), 6.0)
    assert exit_east.latch is True
    assert exit_east.entrances == {"road": (170.0, 64.0)} and exit_east.to_map == "road"
    assert bundle.portals[1].circle == (72.0, 64.0, 5.0) and bundle.portals[1].to_target == "start"
    assert bundle.anchors["well"]["slots"] == [(86.0, 44.0), (106.0, 44.0)]
    assert bundle.anchors["well"]["approach"] == [(96.0, 44.0)]
    names = sorted(entry["file"].relative_to(path.parent).as_posix() for entry in bundle.files)
    assert names == ["art/backdrop.png", "layers/ground.csv", "materials.png", "props/bush.png", "props/rock.png",
                     "props/tree.png", "terrain-vertices.json", "tiles/path.png", "tiles/path.tileset.json",
                     "tiles/terrain.png", "tiles/terrain.tileset.json"]

    report = tmp_path / "out" / "validate-report.json"
    result = run_cli([TOOL, "validate", "--bundle", path, "--report", report, "--require-sha256"], "cp1252")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "pass" and summary["errors"] == 0 and summary["metadata"] == str(report.resolve())
    document = json.loads(report.read_text(encoding="utf-8"))
    assert_valid_contract(document, "common", "qaEnvelope", skill=SKILL)
    assert_patched_valid(document, "map_bundle_report_v1")
    assert document["status"] == "pass" and all(check["status"] == "pass" for check in document["checks"])
    refs = {ref["path"]: ref["sha256"] for ref in document["inputs"]}
    assert refs["../map/map-bundle.json"] == sha(path)
    assert refs["../map/tiles/terrain.png"] == sha(path.parent / "tiles" / "terrain.png")


def test_v1_bundle_readable(tmp_path):
    root = tmp_path / "legacy"
    (root / "layers").mkdir(parents=True)
    legacy = json.loads((FIXTURES_DIR / "contracts" / "map.map_bundle_v2.legacy-v1.json").read_text(encoding="utf-8"))
    (root / "layers" / "ground.csv").write_text("0,1,2\n3,4,5\n", encoding="utf-8")
    path = root / "map-bundle.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    bundle = mb.load_bundle(path)
    assert bundle.errors == [] and bundle.readable and bundle.version == 1
    assert (bundle.width, bundle.height) == (48.0, 32.0)  # inferred from 3 x 2 tiles of 16 px
    assert bundle.id == "legacy" and bundle.collision is None
    messages = " ".join(p.message for p in bundle.warnings)
    for text in ("world inferred", "must name its tileset", "map_nav needs collision"):
        assert text in messages
    result = run_cli([TOOL, "validate", "--bundle", path])
    assert result.returncode == 0 and json.loads(result.stdout)["version"] == 1

    # the roadmap 4.10 draft: inline tileset, {type, rx, ry} footprint, no world, collision or props
    save(wang_atlas(), root / "tiles" / "terrain.png")
    draft = {"schema": "generate2dmap.map_bundle.v1", "tile_size": [16, 16],
             "tilesets": [{"id": "grass-path", "image": "tiles/terrain.png", "kind": "wang_corner",
                           "materials": ["water", "grass"],
                           "tiles": [{"index": i, "wang": [(i >> 3) & 1, (i >> 2) & 1, (i >> 1) & 1, i & 1]}
                                     for i in range(16)]}],
             "layers": [{"name": "ground", "kind": "tiles", "data": [[15, 15, 0], [15, 6, 15]]}],
             "objects": [{"id": "tree-12", "prop": "tree", "x": 20, "y": 20, "anchor_px": [16, 39],
                          "footprint": {"type": "ellipse", "rx": 5, "ry": 3}, "sortY": 20, "occlusion": "tall"}],
             "portals": [{"id": "exit-east", "rect": [40, 0, 8, 16], "to": "route-1:exit-west"}],
             "spawns": [{"id": "spawn", "x": 8, "y": 8}]}
    draft_path = root / "draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    bundle = mb.load_bundle(draft_path)
    assert bundle.errors == [], errors_of(bundle)
    assert bundle.id == "draft" and bundle.tilesets["grass-path"].manifest is None
    assert bundle.layers[0].tileset == "grass-path"
    assert bundle.objects[0].footprint == {"shape": "ellipse", "width": 10, "depth": 6, "offset": [0, 0]}
    rendered = mb.render_map(bundle)
    assert rendered.shape == (32, 48, 4)
    assert np.array_equal(rendered[:16, 32:48], wang_atlas()[:16, :16])  # tile 0 at column 2


def test_missing_file_or_sha_mismatch_fails(tmp_path):
    path = demo(tmp_path)
    (path.parent / "props" / "rock.png").unlink()
    bundle = mb.load_bundle(path)
    assert any(p.code == "file" and "props/rock.png" in p.message for p in bundle.errors), errors_of(bundle)
    result = run_cli([TOOL, "validate", "--bundle", path])
    assert result.returncode == 1 and "file not found: props/rock.png" in result.stderr

    path = demo(tmp_path / "second")
    edit(path, lambda doc: doc["layers"][1].update(sha256="0" * 64))
    bundle = mb.load_bundle(path)
    assert [p.code for p in bundle.errors] == ["sha256"] and "layers/ground.csv" in bundle.errors[0].message
    result = run_cli([TOOL, "validate", "--bundle", path, "--report", tmp_path / "report.json"])
    assert result.returncode == 1 and "sha256 mismatch" in result.stderr
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))  # the failing report is kept
    assert report["status"] == "fail" and {c["id"]: c["status"] for c in report["checks"]}["sha256"] == "fail"
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)

    # an image inside a tileset manifest is checked against the manifest's own sha256
    path = demo(tmp_path / "third")
    save(np.zeros((64, 64, 4), np.uint8), path.parent / "tiles" / "terrain.png")
    assert any("tiles/terrain.tileset.json" in p.path and p.code == "sha256" for p in mb.load_bundle(path).errors)

    path = demo(tmp_path / "unhashed", hashes=False)
    assert mb.load_bundle(path).errors == []
    assert {p.code for p in mb.load_bundle(path, require_sha256=True).errors} == {"sha256"}


# --------------------------------------------------------------------------- the schema evaluator

def _apply_case(document, case):
    if "document" in case:
        return copy.deepcopy(case["document"])
    mutated = copy.deepcopy(document)
    pointer = case["set"] if "set" in case else case["remove"]
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
    parent = mutated
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
    if "set" in case:
        parent[key] = copy.deepcopy(case["value"])
    else:
        del parent[key]
    return mutated


def _mutations(document, rng: random.Random, count: int):
    """Random single-field mutations: wrong types, out-of-range numbers, removed keys."""
    pointers = []

    def walk(node, pointer):
        pointers.append(pointer)
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, pointer + [key])
        elif isinstance(node, list):
            for index, value in enumerate(node[:3]):
                walk(value, pointer + [index])

    walk(document, [])
    replacements = [None, True, -1, 0, 1.5, "", "x:y", "/abs", [], {}, [1, 2], "#zz", 1e9]
    for _ in range(count):
        mutated = copy.deepcopy(document)
        pointer = rng.choice(pointers[1:])
        parent = mutated
        for part in pointer[:-1]:
            parent = parent[part]
        if isinstance(parent, dict) and rng.random() < 0.3:
            del parent[pointer[-1]]
        else:
            parent[pointer[-1]] = copy.deepcopy(rng.choice(replacements))
        yield mutated


def test_schema_evaluator_matches_jsonschema(tmp_path):
    cases = 0
    for path in sorted(FIXTURES_DIR.glob("contracts/*.json")):
        domain, name, kind = path.name[:-5].split(".", 2)
        if domain not in ("map", "common") or kind == "invalid":
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        documents = [document]
        invalid = path.with_name(f"{domain}.{name}.invalid.json")
        if kind == "valid" and invalid.exists():
            documents += [_apply_case(document, case)
                          for case in json.loads(invalid.read_text(encoding="utf-8"))["cases"]]
        for doc in documents:
            ours = mb.SCHEMAS.errors(doc, f"{domain}.schema.json#/$defs/{name}")
            theirs = contract_errors(doc, domain, name, skill=SKILL)
            assert bool(ours) == bool(theirs), (path.name, ours[:3], theirs[:3])
            cases += 1
    raw = json.loads(demo(tmp_path).read_text(encoding="utf-8"))
    for mutated in _mutations(raw, random.Random(13), 300):
        ours = mb.SCHEMAS.errors(mutated, "map.schema.json#/$defs/map_bundle_v2")
        theirs = contract_errors(mutated, "map", "map_bundle_v2", skill=SKILL)
        assert bool(ours) == bool(theirs), (ours[:3], theirs[:3])
        cases += 1
    assert cases > 450


def test_schema_evaluator_covers_every_keyword():
    """Every keyword in the schemas this skill vendors, and in all shared schemas (so the
    evaluator can move to forge_core), is implemented or an annotation."""
    known = mb._ASSERTIONS | mb._ANNOTATIONS

    def keywords(node, out, in_properties=False):
        if isinstance(node, dict):
            for key, value in node.items():
                if not in_properties:
                    out.add(key)
                keywords(value, out, key in ("properties", "$defs") and not in_properties)
        elif isinstance(node, list):
            for value in node:
                keywords(value, out)

    for folder in (SKILLS_DIR / SKILL / "references" / "schemas", SKILLS_DIR.parent / "shared" / "schemas"):
        for path in sorted(folder.glob("*.schema.json")):
            found: set[str] = set()
            keywords(json.loads(path.read_text(encoding="utf-8")), found)
            assert found <= known, (path, found - known)
    with pytest.raises(ValueError, match="unsupported"):
        mb._LocalSchemaSet(FIXTURES_DIR)._check(1, {"uniqueItems": True}, "x", "$", [])


def test_schema_evaluator_on_synthetic_schemas(tmp_path):
    """Keywords the map schemas use rarely or not at all (false schemas, exclusiveMaximum, maxLength,
    min/maxProperties, else, array and object consts, cross-file $ref) agree with jsonschema too."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    base = "https://example.invalid/schemas/"
    pair = {"type": "array", "prefixItems": [{"const": [1, {"a": 2}]}], "items": False, "maxItems": 2}
    other = {"$id": base + "other.schema.json", "$defs": {"pair": pair}}
    main = {"$id": base + "main.schema.json", "$defs": {"thing": {
        "type": "object", "minProperties": 1, "maxProperties": 3, "propertyNames": {"maxLength": 4},
        "properties": {"n": {"type": "number", "exclusiveMaximum": 5}, "s": {"type": "string", "maxLength": 3},
                       "p": {"$ref": "other.schema.json#/$defs/pair"}, "k": {"enum": [1, "1", True, None, [1]]}},
        "if": {"required": ["n"]}, "then": {"required": ["s"]}, "else": {"not": {"required": ["s"]}},
        "additionalProperties": False}}}
    for schema in (other, main):
        (tmp_path / schema["$id"].rsplit("/", 1)[1]).write_text(json.dumps(schema), encoding="utf-8")
    ours = mb._LocalSchemaSet(tmp_path)
    registry = Registry().with_resources((s["$id"], DRAFT202012.create_resource(s)) for s in (other, main))
    theirs = Draft202012Validator({"$ref": base + "main.schema.json#/$defs/thing"}, registry=registry)
    instances = [{"n": 4, "s": "abc"}, {"n": 5, "s": "a"}, {"n": 1}, {"s": "a"}, {}, {"n": 1, "s": "a", "k": 1, "p": 2},
                 {"n": 1, "s": "abcd"}, {"long": 1}, {"p": [[1, {"a": 2}]]}, {"p": [[1, {"a": 2.0}], 3]},
                 {"p": [[1, {"a": True}]]}, {"k": 1.0}, {"k": "1"}, {"k": False}, {"k": None}, {"k": [1.0]}, {"k": [2]},
                 {"n": True}, {"n": 4, "s": "abc", "extra": 1}, []]
    for instance in instances:
        expected = theirs.is_valid(instance)
        assert (not ours.errors(instance, "main.schema.json#/$defs/thing")) == expected, instance
    assert mb._json_equal([1, {"a": 2.0}], [1, {"a": 2}]) and not mb._json_equal([True], [1])


def test_corrupt_files_are_reported_not_raised(tmp_path):
    path = demo(tmp_path, hashes=False)
    root = path.parent
    (root / "props" / "rock.png").write_bytes(b"not a png")
    (root / "tiles" / "path.png").write_bytes(b"\x89PNG broken")
    (root / "terrain-vertices.json").write_text("[[1, 1], [1", encoding="utf-8")
    (root / "layers" / "ground.csv").write_text("1,2,x\n", encoding="utf-8")
    messages = errors_of(mb.load_bundle(path))
    assert any(m.startswith("$.props.rock.image: cannot open props/rock.png") for m in messages)
    assert any("tiles/path.tileset.json" in m and "cannot open path.png" in m for m in messages)
    assert any(m.startswith("$.terrain.vertex_grid: cannot read JSON") for m in messages)
    assert any(m.startswith("$.layers[1].data: cannot read tile data layers/ground.csv") for m in messages)
    listed = tmp_path / "list.json"
    listed.write_text("[]", encoding="utf-8")
    assert [p.message for p in mb.load_bundle(listed).errors] == ["a map bundle must be a JSON object"]


def test_portal_geometry():
    rect = mb.Portal(id="r", rect=(10.0, 20.0, 4.0, 6.0), circle=None, to_map="m", to_target=None,
                     activation="crossing", travel=None, radius=0.0, entrances={}, entrance_refs={}, latch=True,
                     requires_movement=True, reciprocal=True)
    circle = mb.Portal(id="c", rect=None, circle=(0.0, 0.0, 5.0), to_map="m", to_target=None, activation="intent",
                       travel=(1.0, 0.0), radius=3.0, entrances={}, entrance_refs={}, latch=True,
                       requires_movement=True, reciprocal=True)
    assert rect.bounds() == (10.0, 20.0, 14.0, 26.0) and circle.bounds() == (-5.0, -5.0, 5.0, 5.0)
    assert rect.distance(12, 22) == 0 and rect.distance(17, 30) == 5.0 and rect.closest_point(17, 30) == (14.0, 26.0)
    assert circle.distance(3, 4) == 0 and circle.distance(6, 8) == 5.0
    assert circle.closest_point(6, 8) == (3.0, 4.0) and circle.closest_point(1, 1) == (1, 1)


def test_v1_footprint_forms():
    doc, notes = mb.upgrade_v1({"schema": "generate2dmap.map_bundle.v1", "objects": [
        {"id": "a", "footprint": {"type": "ellipse", "cx": 2, "cy": -1, "rx": 5, "ry": 3}},
        {"id": "b", "footprint": {"x": -6, "y": -4, "w": 12, "h": 4}},
        {"id": "c", "footprint": {"shape": "rect", "width": 3, "depth": 2}}]})
    assert doc["objects"][0]["footprint"] == {"shape": "ellipse", "width": 10, "depth": 6, "offset": [2, -1]}
    assert doc["objects"][1]["footprint"] == {"shape": "rect", "width": 12, "depth": 4, "offset": [0.0, -2.0]}
    assert doc["objects"][2]["footprint"] == {"shape": "rect", "width": 3, "depth": 2} and len(notes) == 2


def test_strict_json_reader(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema": "generate2dmap.map_bundle.v2", "schema": "again"}', encoding="utf-8")
    with pytest.raises(mb.BundleError, match="duplicate key"):
        mb.load_bundle(path)
    path.write_text('{"x": NaN}', encoding="utf-8")
    with pytest.raises(mb.BundleError, match="NaN"):
        mb._local_read_json(path)
    path.write_text('﻿{"schema": "generate2dmap.map_bundle.v3"}', encoding="utf-8")
    assert [p.path for p in mb.load_bundle(path).errors] == ["$.schema"]
    result = run_cli([TOOL, "validate", "--bundle", tmp_path / "missing.json"])
    assert result.returncode == 1 and result.stderr.startswith("error: cannot read JSON")
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------- cross-field rules

def _set(pointer: str, value):
    def change(doc):
        parts = pointer.split("/")[1:]
        node = doc
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        node[int(parts[-1]) if isinstance(node, list) else parts[-1]] = value
    return change


RULES = [
    ("duplicate object id", _set("/objects/1/id", "tree-1"), "$.objects[1].id", "duplicate object id"),
    ("duplicate spawn id", _set("/spawns/1/id", "start"), "$.spawns[1].id", "duplicate spawn id"),
    ("duplicate portal id", _set("/portals/1/id", "exit-east"), "$.portals[1].id", "duplicate portal id"),
    ("duplicate interaction id", _set("/interactions/1/id", "well"), "$.interactions[1].id", "duplicate interaction"),
    ("duplicate layer name", _set("/layers/2/name", "ground"), "$.layers[2].name", "duplicate layer name"),
    ("unknown tileset", _set("/layers/1/tileset", "nope"), "$.layers[1].tileset", "unknown tileset"),
    ("tile index outside the atlas", _set("/layers/2/data/5/1", 48), "$.layers[2].data", "exceed the 48 tiles"),
    ("negative tile index", _set("/layers/2/data/5/1", -2), "$.layers[2].data", "below -1"),
    ("ragged tile rows", _set("/layers/2/data/0", [1]), "$.layers[2].data", "equally long rows"),
    ("tiles do not cover the world", _set("/world/width", 200), "$.layers[1].data", "the world is 200x128"),
    ("vertex grid size", _set("/tile_size", 32), "$.terrain.vertex_grid", "the map needs"),
    ("unknown prop", _set("/objects/0/prop", "statue"), "$.objects[0].prop", "unknown prop 'statue'"),
    ("object layer is not an objects layer", _set("/objects/0/layer", "ground"), "$.objects[0].layer",
     "not an objects layer"),
    ("same-map portal to an unknown spawn", _set("/portals/1/to", "meadow:nowhere"), "$.portals[1].to",
     "no spawn or anchor 'nowhere'"),
    ("same-map portal without arrival", _set("/portals/1/to", "meadow"), "$.portals[1].to", "must name its arrival"),
    ("portal to without a map", _set("/portals/1/to", ":start"), "$.portals[1].to", "map id"),
    ("entrance names an unknown spawn", _set("/portals/0/entranceByFrom/road", "ghost"),
     "$.portals[0].entranceByFrom.road", "neither a spawn"),
    ("zero travel direction", _set("/portals/0/travelDirection", [0, 0]), "$.portals[0].travelDirection",
     "must not be [0, 0]"),
    ("reciprocal must be boolean", _set("/portals/0/reciprocal", "no"), "$.portals[0].reciprocal", "true or false"),
    ("spawn outside the world", _set("/spawns/0/x", 500), "$.spawns[0]", "outside the 192x128 world"),
    ("malformed slot", _set("/anchors/well/slots/0", "here"), "$.anchors.well.slots[0]", "a slot is"),
    ("map id with a colon", _set("/id", "a:b"), "$.id", "without ':'"),
    ("material colour clash", _set("/material_map/materials/lava/color", "#008000"), "$.material_map.materials",
     "colors must be unique"),
    ("prop footprint without size", _set("/props/tree/footprint", {"shape": "ellipse"}), "$.props.tree.footprint",
     "required property"),
    ("schema enum", _set("/portals/0/activation", "teleport"), "$.portals[0].activation", "is not one of"),
    ("prop image path is absolute", _set("/props/tree/image", "/art/tree.png"), "$.props.tree.image",
     "is not a relative POSIX path"),
    ("prop without image", _set("/props/tree", {"anchor_px": [1, 1]}), "$.props.tree", "a prop needs an image"),
    ("props registry is a list", _set("/props", []), "$.props", "props must be an object"),
    ("prop anchor is malformed", _set("/props/tree/anchor_px", "bottom"), "$.props.tree.anchor_px", "must be [x, y]"),
    ("prop solid is not boolean", _set("/props/tree/solid", "yes"), "$.props.tree.solid", "true or false"),
    ("duplicate tileset id", _set("/tilesets/1/id", "terrain"), "$.tilesets[1].id", "duplicate tileset id"),
    ("zero-area walk region", _set("/collision/walkRegions", [{"polygon": [[0, 0], [10, 0], [20, 0]]}]),
     "$.collision.walkRegions[0].polygon", "zero area"),
    ("material without colour", _set("/material_map/materials/lava", {"class": "hazard"}), "$.material_map.materials",
     "needs a color"),
    ("image layer offset is malformed", _set("/layers/0/offset", [1]), "$.layers[0].offset", "must be [x, y]"),
]


@pytest.mark.parametrize("change, where, text", [rule[1:] for rule in RULES], ids=[rule[0] for rule in RULES])
def test_cross_field_rules(tmp_path, change, where, text):
    path = edit(demo(tmp_path, hashes=False), change)
    bundle = mb.load_bundle(path)
    assert any(p.path == where and text in p.message for p in bundle.errors), errors_of(bundle)


def test_file_level_rules(tmp_path):
    path = demo(tmp_path, hashes=False)
    root = path.parent
    rows = [[int(v) for v in line.split(",")] for line in (root / "layers" / "ground.csv").read_text().split()]
    (root / "layers" / "ground.json").write_text(json.dumps({"data": rows}), encoding="utf-8")
    edit(path, lambda doc: doc["layers"][1].update(data="layers/ground.json"))
    bundle = mb.load_bundle(path)
    assert bundle.errors == [] and bundle.layers[1].grid.tolist() == rows  # JSON tile data, {"data": rows} form

    (root / "terrain-vertices.json").write_text(json.dumps([[5] * 13] * 9), encoding="utf-8")
    edit(root / "tiles" / "terrain.tileset.json", lambda doc: doc["tiles"][0].update(index=99))
    (root / "stage.json").write_text(json.dumps({"schema": "generate2dmap.stage.v1"}), encoding="utf-8")
    edit(path, lambda doc: doc.update(stage="stage.json", nav={"cell": 5, "grid": "qa/nav-grid.json"}))
    messages = errors_of(mb.load_bundle(path))
    assert any("vertex values must index terrain.materials (0..1)" in m for m in messages)
    assert any("tile 99 is outside the 4x4 atlas" in m for m in messages)
    assert any(m.startswith("$.stage: stage.json: $: 'sourceSize' is a required property") for m in messages)
    assert any(m.startswith("$.nav.grid: file not found: qa/nav-grid.json") for m in messages)
    assert any("nav.cell 5 differs" in p.message for p in mb.load_bundle(path).warnings)

    edit(root / "tiles" / "terrain.tileset.json", lambda doc: doc.update(kind="hexagonal"))
    schema_errors = [p for p in mb.load_bundle(path).errors if p.code == "schema"]
    assert any(p.path == "$.tilesets[0].manifest" and "hexagonal" in p.message for p in schema_errors)


def test_validation_warnings(tmp_path):
    path = demo(tmp_path, hashes=False)

    def change(doc):
        doc["collision"].update(actorRadius=0, walkRegions=[{"polygon": [[-5, 0], [192, 0], [192, 128], [0, 128]],
                                                             "holes": [[[150, 10], [250, 10], [200, 50]]]}],
                                solids=[{"shape": "rect", "x": 5, "y": 5, "w": 0, "h": 3}], rects=[[1, 1, 0, 0]])
        doc["anchors"]["start"] = {"point": [10, 10]}
        doc["objects"][0].update(x=500, anchor_px=[90, 90])
        doc["material_map"]["materials"]["lava"].update(**{"class": "solid", "walkable": True})
        doc["camera"]["bounds"] = [0, 0, 400, 128]
        edit(path.parent / "tiles" / "path.tileset.json", lambda tiles: tiles["tiles"][1].update(
            blob_mask=tiles["tiles"][0]["blob_mask"],
            collision=[{"shape": "rect", "x": 10, "y": 10, "w": 20, "h": 4}]))

    edit(path, change)
    bundle = mb.load_bundle(path)
    assert bundle.errors == [], errors_of(bundle)
    warned = {p.path: p.message for p in bundle.warnings}
    assert "treats the actor as a point" in warned["$.collision.actorRadius"]
    assert "extends outside the world" in warned["$.collision.walkRegions"]
    assert "hole extends outside its region" in warned["$.collision.walkRegions[0].holes[0]"]
    assert "zero-size solid" in warned["$.collision.solids[0]"] and "zero-size rect" in warned["$.collision.rects[0]"]
    assert "shares its name with a spawn" in warned["$.anchors.start"]
    assert "anchored outside the world" in warned["$.objects[0]"]
    assert "lies outside the 24x32 prop image" in warned["$.objects[0].anchor_px"]
    assert "never walkable" in warned["$.material_map.materials.lava"]
    assert "extend outside the world" in warned["$.camera.bounds"]
    assert any("repeats the topology" in m for m in warned.values())
    assert any("collision shape extends outside the tile" in m for m in warned.values())


def test_tile_collision_shapes_are_translated(tmp_path):
    root = tmp_path / "tiles"
    save(wang_atlas(), root / "atlas.png")
    shapes = {0: [{"shape": "ellipse", "cx": 8, "cy": 8, "rx": 3, "ry": 2, "rotate": 45}],
              1: [{"shape": "polygon", "points": [[0, 0], [16, 0], [0, 16]]}],
              2: [{"shape": "rect", "x": 0.5, "y": 0, "w": 4, "h": 4}],
              3: [{"shape": "rect", "x": 12, "y": 12, "w": 8, "h": 8}]}  # reaches into the next cell
    tileset = {"schema": "generate2dmap.tileset.v1", "image": "atlas.png", "tile_size": 16, "columns": 4,
               "kind": "flat", "materials": ["stone"], "seamless_verified": False,
               "tiles": [{"index": i, "collision": s} for i, s in shapes.items()]}
    (root / "set.json").write_text(json.dumps(tileset), encoding="utf-8")
    doc = {"schema": "generate2dmap.map_bundle.v2", "tile_size": 16, "world": {"width": 32, "height": 32, "unit": "px"},
           "tilesets": [{"id": "stone", "manifest": "set.json"}], "collision": {"actorRadius": 2},
           "layers": [{"name": "ground", "kind": "tiles", "data": [[0, 1], [2, 3]]}]}
    bundle = mb.bundle_from_document(doc, root / "bundle.json")
    assert bundle.errors == [], errors_of(bundle)
    solids = mb.world_solids(bundle)
    by_shape = {}
    for solid in solids:
        by_shape.setdefault(solid["shape"], []).append(solid)
    assert by_shape["ellipse"] == [{"shape": "ellipse", "cx": 8, "cy": 8, "rx": 3, "ry": 2, "rotate": 45,
                                    "source": "tiles:ground"}]
    assert by_shape["polygon"][0]["points"] == [[16, 0], [32, 0], [16, 16]]  # tile 1 sits at x 16
    loose = [s for s in by_shape["rect"] if s["x"] == 0.5]
    assert loose == [{"shape": "rect", "x": 0.5, "y": 16, "w": 4, "h": 4, "source": "tiles:ground"}]
    assert {"shape": "rect", "x": 28, "y": 28, "w": 8, "h": 8, "source": "tiles:ground"} in by_shape["rect"]
    merged = [s for s in by_shape["rect"] if s["x"] == 28 and s["w"] == 4]  # the in-cell part, merged
    assert merged == [{"shape": "rect", "x": 28, "y": 28, "w": 4, "h": 4, "source": "tiles:ground"}]


def test_v1_world_from_an_image_layer(tmp_path):
    save(np.full((30, 50, 4), 255, np.uint8), tmp_path / "plate.png")
    doc = {"schema": "generate2dmap.map_bundle.v1",
           "layers": [{"name": "plate", "kind": "image", "image": "plate.png"}]}
    bundle = mb.bundle_from_document(doc, tmp_path / "plate.json")
    assert bundle.errors == [] and (bundle.width, bundle.height) == (50.0, 30.0)
    assert any("world inferred from image layer 'plate'" in p.message for p in bundle.warnings)
    empty = mb.bundle_from_document({"schema": "generate2dmap.map_bundle.v1", "layers": []}, tmp_path / "empty.json")
    assert [p.path for p in empty.errors] == ["$.world"] and not empty.readable


def test_tileset_rules(tmp_path):
    path = demo(tmp_path, hashes=False)
    manifest = path.parent / "tiles" / "path.tileset.json"
    edit(manifest, lambda doc: doc["tiles"][3].update(blob_mask=2))  # NE without N and E
    edit(manifest, lambda doc: doc["tiles"][4].update(index=3))
    terrain = path.parent / "tiles" / "terrain.tileset.json"
    edit(terrain, lambda doc: doc["tiles"][15].update(wang=[1, 1, 1, 2]))
    edit(terrain, lambda doc: doc.update(columns=3))
    messages = errors_of(mb.load_bundle(path))
    assert any("blob_mask 2 sets NE without N and E" in m for m in messages)
    assert any("duplicate tile index 3" in m for m in messages)
    assert any("atlas 64x64 is not 3 columns" in m for m in messages)
    edit(terrain, lambda doc: doc.update(columns=4))
    assert any("names a material beyond" in m for m in errors_of(mb.load_bundle(path)))


def test_material_map_rules(tmp_path):
    path = demo(tmp_path, hashes=False)
    image = np.asarray(Image.open(path.parent / "materials.png")).copy()
    image[3, 3] = (1, 2, 3, 255)
    save(image, path.parent / "materials.png")
    assert any("1 pixel(s) match no material (first at x=3, y=3)" in m for m in errors_of(mb.load_bundle(path)))
    save(np.zeros((8, 13, 4), np.uint8), path.parent / "materials.png")
    assert any("whole squares" in m for m in errors_of(mb.load_bundle(path)))
    # palette or grey images match materials by index
    save(np.where(np.arange(12)[None, :] > 9, 1, 0).repeat(8, 0).astype(np.uint8), path.parent / "materials.png")
    edit(path, lambda doc: doc["material_map"].update(materials={"meadow": {"class": "decor", "index": 0},
                                                                 "wall": {"class": "solid", "index": 1}}))
    bundle = mb.load_bundle(path)
    assert bundle.errors == [], errors_of(bundle)
    codes = bundle.material.class_codes()
    assert codes.shape == (8, 12) and (codes[:, 10:] == mb.BLOCK).all() and (codes[:, :10] == mb.FREE).all()


def test_warnings_do_not_fail(tmp_path):
    path = demo(tmp_path, hashes=False)
    edit(path, lambda doc: (doc.update(extra_field=1), doc["objects"][0].update(occlusion="canopy"),
                            doc["portals"][0].update(latch=False)))
    bundle = mb.load_bundle(path)
    assert bundle.errors == []
    messages = " ".join(p.message for p in bundle.warnings)
    assert "unknown top-level field(s) ignored: extra_field" in messages
    assert "'canopy' is not one of" in messages and "unlatched exit" in messages
    result = run_cli([TOOL, "validate", "--bundle", path])
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "warn" and "warning:" in result.stderr


def test_hd2d_stage_without_ysquash_warns(tmp_path):
    stage = {"schema": "generate2dmap.stage.v1", "sourceSize": [100, 60], "fit": "cover",
             "reviewedAspectRange": [1.3, 2.0], "groundPolygons": [[[0, 0.5], [1, 0.5], [1, 1], [0, 1]]],
             "slots": {"hero": [[0.3, 0.8]], "enemy": [[0.7, 0.8]]}}
    doc = {"schema": "generate2dmap.map_bundle.v2", "world": {"width": 100, "height": 60, "unit": "px"},
           "layers": [], "collision": {"actorRadius": 5}, "stage": stage}
    bundle = mb.bundle_from_document(doc, tmp_path / "plate.json")
    assert bundle.errors == [] and any("ySquash about 0.58" in p.message for p in bundle.warnings)
    doc["collision"]["ySquash"] = 0.58
    assert mb.bundle_from_document(doc, tmp_path / "plate.json").warnings == []


def test_props_from_a_prop_pack(tmp_path):
    path = demo(tmp_path, hashes=False)
    pack_dir = path.parent / "pack"
    save(prop_image(10, 20, (90, 60, 40), 7), pack_dir / "lamp" / "prop.png")
    pack = {"schema": "generate2dmap.prop_pack.v2", "accepted": [{
        "label": "lamp", "display_name": "Lamp", "image": "lamp/prop.png",
        "sha256": sha(pack_dir / "lamp" / "prop.png"),
        "source_rect": [0, 0, 10, 20], "cell_box": [0, 0, 10, 20], "padding": [0, 0, 0, 0], "anchor_px": [5, 19],
        "footprint": {"shape": "ellipse", "width": 4, "depth": 2}, "solid": True, "dropped_components": 0,
        "dropped_area": 0, "status": "accepted"}], "rejected": []}
    (pack_dir / "prop-pack.json").write_text(json.dumps(pack), encoding="utf-8")
    edit(path, lambda doc: (doc["props"].update(lamp={"pack": "pack/prop-pack.json", "label": "lamp"}),
                            doc["objects"].append({"id": "lamp-1", "prop": "lamp", "x": 80, "y": 90,
                                                   "anchor_px": [5, 19]})))
    bundle = mb.load_bundle(path)
    assert bundle.errors == [], errors_of(bundle)
    lamp = bundle.props["lamp"]
    assert lamp.size == (10, 20) and lamp.anchor_px == (5.0, 19.0)
    assert lamp.image == (pack_dir / "lamp" / "prop.png").resolve()
    assert bundle.objects[-1].solid is True and bundle.objects[-1].footprint["width"] == 4
    assert_patched_valid(json.loads(path.read_text(encoding="utf-8")), "map_bundle_v2")
    edit(path, lambda doc: doc["props"]["lamp"].update(label="lantern"))
    assert any("no accepted item 'lantern'" in m for m in errors_of(mb.load_bundle(path)))


# --------------------------------------------------------------------------- geometry and rendering

def test_world_solids_scale_footprints_once(tmp_path):
    bundle = mb.load_bundle(demo(tmp_path))
    solids = {s["source"]: s for s in mb.world_solids(bundle) if s["source"].startswith("object:")}
    assert set(solids) == {"object:tree-1", "object:tree-2", "object:rock-1"}  # bushes are not solid
    assert solids["object:tree-1"] == {"shape": "ellipse", "cx": 40.0, "cy": 112.0, "rx": 4.0, "ry": 2.0,
                                       "rotate": 0.0, "source": "object:tree-1"}
    assert (solids["object:tree-2"]["rx"], solids["object:tree-2"]["ry"]) == (8.0, 4.0)  # scale 2, applied once
    rock = np.array(solids["object:rock-1"]["points"])
    assert rock.shape == (4, 2) and np.allclose(rock.mean(axis=0), (112, 62))
    edges = np.linalg.norm(np.roll(rock, -1, axis=0) - rock, axis=1)
    assert np.allclose(sorted(edges), [6, 6, 10, 10])
    dx, dy = rock[1] - rock[0]
    assert np.isclose(np.degrees(np.arctan2(dy, dx)), 30)  # clockwise on screen (y down)


def test_tile_solids_merge_quadrants(tmp_path):
    path = demo(tmp_path, hashes=False)
    bundle = mb.load_bundle(path)
    tile_rects = [s for s in mb.world_solids(bundle) if s["source"] == "tiles:ground"]
    assert all(s["shape"] == "rect" for s in tile_rects) and len(tile_rects) == 1
    mask = np.zeros((128, 192), bool)
    for s in tile_rects:
        mask[s["y"]:s["y"] + s["h"], s["x"]:s["x"] + s["w"]] = True
    expected = np.zeros((128, 192), bool)
    expected[8:40, 8:56] = True  # water vertices (1..3, 1..2): +-8 px around each
    assert np.array_equal(mask, expected)
    # a tile marked walkable false with no shapes blocks its whole cell
    grid = bundle.layers[2].grid
    index = int(np.bincount(grid[grid >= 0]).argmax())
    edit(path.parent / "tiles" / "path.tileset.json",
         lambda doc: doc["tiles"][index].update(properties={"walkable": False}))
    placed = mb.load_bundle(path)
    solids = [s for s in mb.world_solids(placed) if s["source"] == "tiles:decoration"]
    assert sum(s["w"] * s["h"] for s in solids) == int((grid == index).sum()) * T * T


def test_render_map_layers_and_draw_order(tmp_path):
    bundle = mb.load_bundle(demo(tmp_path))
    image = mb.render_map(bundle)
    assert image.shape == (128, 192, 4) and image.dtype == np.uint8
    ground = mb.tile_layer_rgba(bundle, bundle.layers[1])
    assert np.array_equal(image[0:16, 176:192], ground[0:16, 176:192])  # plain grass, nothing on top
    # tree-2: 24x32 at scale 2 anchored (12, 30) at (156, 40) -> drawn from (132, -20), clipped at the top
    tree = np.asarray(Image.open(bundle.props["tree"].image).convert("RGBA").resize((48, 64), Image.NEAREST))
    assert np.array_equal(image[0:44, 136:176], tree[20:64, 4:44])
    assert [o.id for o in mb.draw_order(bundle.objects)] == ["tree-2", "rock-1", "bush-1", "bush-2", "tree-1"]
    assert mb.raster_box(*mb.object_placement(bundle.objects[3], (14, 10))) == (121, 95, 14, 10)


def test_merge_rects_is_exact():
    rng = np.random.default_rng(5)
    for density in (0.0, 0.2, 0.5, 0.8, 1.0):
        mask = rng.random((23, 31)) < density
        cover = np.zeros(mask.shape, int)
        for x, y, w, h in mb.merge_rects(mask):
            cover[y:y + h, x:x + w] += 1
        assert np.array_equal(cover, mask.astype(int))
    assert mb.merge_rects(np.ones((5, 7), bool)) == [(0, 0, 7, 5)]
    assert (mb.nav_cell(5), mb.nav_cell(3), mb.nav_cell(1), mb.nav_cell(0)) == (3, 2, 1, 1)  # half up


# --------------------------------------------------------------------------- the hash verb and CLI conventions

def test_hash_fills_every_sha256(tmp_path):
    path = demo(tmp_path, hashes=False)
    out = tmp_path / "elsewhere" / "hashed.json"
    result = run_cli([TOOL, "hash", "--bundle", path, "--output", out])
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["output"] == str(out.resolve()) and summary["files_hashed"] == 10
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["tilesets"][0]["manifest"] == "../map/tiles/terrain.tileset.json"  # rebased to the new folder
    assert doc["terrain"]["sha256"] == sha(path.parent / "terrain-vertices.json")
    bundle = mb.load_bundle(out, require_sha256=True)
    assert bundle.errors == [] and bundle.id == "meadow"
    assert_valid_contract(doc, "map", "map_bundle_v2", skill=SKILL)
    assert_patched_valid(doc, "map_bundle_v2")


def test_cli_help_cp1252():
    assert_cli_help(SKILL, "map_bundle")
    for verb in ("validate", "hash"):
        result = run_cli([TOOL, verb, "--help"], "cp1252")
        assert result.returncode == 0 and result.stdout.isascii()


def test_cli_refuses_existing_output(tmp_path):
    path = demo(tmp_path)
    existing = tmp_path / "taken.json"
    existing.write_text("keep", encoding="utf-8")
    for argv in (["hash", "--bundle", path, "--output", existing],
                 ["validate", "--bundle", path, "--report", existing]):
        result = run_cli([TOOL, *argv])
        assert result.returncode == 1 and result.stderr.startswith("error:"), result.stderr
        assert existing.read_text(encoding="utf-8") == "keep"


def test_cli_failure_publishes_nothing(tmp_path):
    path = demo(tmp_path, hashes=False)
    (path.parent / "layers" / "ground.csv").unlink()
    out = tmp_path / "out" / "hashed.json"
    result = run_cli([TOOL, "hash", "--bundle", path, "--output", out])
    assert result.returncode == 1 and "nothing was written" in result.stderr and "Traceback" not in result.stderr
    assert not (tmp_path / "out").exists()
    assert sorted(p.name for p in path.parent.iterdir() if p.is_file()) == [
        "map-bundle.json", "materials.png", "terrain-vertices.json"]


# --------------------------------------------------------------------------- the reference docs (B13-T4)

DOCS = ("layered-map-contract.md", "map-strategies.md", "map-presets.md")


def _doc_blocks(name: str, language: str) -> list[str]:
    text = (SKILLS_DIR / SKILL / "references" / name).read_text(encoding="utf-8")
    parts = text.split("```")
    return [parts[i].split("\n", 1)[1] for i in range(1, len(parts), 2) if parts[i].startswith(language + "\n")]


def test_reference_docs_examples_validate():
    contract = [json.loads(block) for block in _doc_blocks("layered-map-contract.md", "json")]
    placements, gameplay = contract
    assert_valid_contract(placements, "map", "placements_v2", skill=SKILL)
    bundle = {"schema": "generate2dmap.map_bundle.v2", "world": {"width": 960, "height": 640, "unit": "px"},
              "layers": [{"name": "props", "kind": "objects"}], **gameplay}
    assert_patched_valid(bundle, "map_bundle_v2")
    assert gameplay["props"]["tree"]["occupant_policy"] in mb.OCCUPANT_POLICIES
    (portal,) = [json.loads(block) for block in _doc_blocks("map-strategies.md", "json")]
    assert_valid_contract(portal, "map", "portal", skill=SKILL)
    for snippet in (json.loads(block) for block in _doc_blocks("map-presets.md", "json")):
        assert_patched_valid({"schema": "generate2dmap.map_bundle.v2", "layers": [], **snippet}, "map_bundle_v2")


def test_reference_docs_commands_and_links():
    import re
    import shlex

    parsers = {name: load_script(SKILL, name).build_parser() for name in ("map_bundle", "map_nav", "export_tiled")}
    commands = 0
    for name in DOCS:
        text = (SKILLS_DIR / SKILL / "references" / name).read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#]+)\)", text):
            assert (SKILLS_DIR / SKILL / "references" / target).resolve().is_file(), (name, target)
        lines = [line for block in _doc_blocks(name, "bash") for line in block.splitlines() if line.strip()]
        lines += re.findall(r"`(python \"<skill-dir>/scripts/[^`]+)`", text)
        for line in lines:
            assert not line.rstrip().endswith("\\"), line  # single-line commands only
            argv = shlex.split(line)
            assert argv[0] == "python" and argv[1].startswith("<skill-dir>/scripts/"), line
            script = argv[1].split("/")[-1]
            assert (SKILLS_DIR / SKILL / "scripts" / script).is_file(), line
            if script[:-3] in parsers:
                parsers[script[:-3]].parse_args(argv[2:])  # flags and verbs exist
            commands += 1
    assert commands >= 12
