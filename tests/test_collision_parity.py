"""JS/Python collision parity on three map fixtures (integration decision D4; plan Appendix I, Phase 3 e2e).

forge_nav (shared/forge_nav.py, vendored into generate2dmap) decides walkability for map_nav, layout_build, the
compose audit and the engine exporters; map-runtime.mjs mirrors it rule for rule (N1-N15) and is what the
playable preview and a game walk with. This test proves that the two give the same answer to every question,
bit for bit, on three bundles written to disk:

1. a top-down tile map whose collision comes from per-tile shapes (tileset_v1 tiles[].collision, D5): whole-pixel
   rects that merge, a rect that reaches out of its cell, a fractional rect, an ellipse, polygons, thin walls and
   a tile that blocks through walkable: false, on two tiles layers, plus prop footprints, collision.rects, exits
   and anchors;
2. an HD-2D plate (ySquash 0.58) with two overlapping walk regions and their holes, ellipse, polygon and rect
   solids, prop footprints (prop_px and world_px basis, scale, flip_x, solid false) and a material map of six
   materials in five classes;
3. a side-scroll room whose palette material map (matched by index) holds one_way ledges, solid ground,
   hazards and a wadeable pool, with a slope polygon and a hanging wall.

Python reads the bundle file with forge_nav.read_blocking_set (the D2 blocking set). JavaScript gets exactly what
a player gets: build_scene_preview builds the playable page from the same bundle (tile collision converted into
world solids, material classes into bit planes) and the page's SCENE object is handed to map-runtime.mjs under
node. Compared with no tolerance and no exempt point:

- validity, the centre test (pointFree / centre_ok) and the nine-sample footprint test (isValid / valid), on a
  quarter-pixel lattice with a margin outside the world, which holds every shape edge and material pixel edge
  of these fixtures, plus the vertices and edge points of every solid, walk region and hole and the extreme
  points of every ellipse, each also shifted so that a footprint sample lands on it;
- segmentClear with and without the thin-gap rule on random segments, short vertical drops and climbs (one_way),
  segments along every rect and polygon edge, and segments that graze every ellipse;
- reachability: every grid node, every open move, the BFS distances from the spawns and arrival points, and the
  node and entry point each interaction, anchor slot, approach point and exit is reached at (N14).

Material maps are integer-scale with every pixel classified (D4). Rotations stay at 0: the sine and cosine of a
rotation come from the platform's libm and may differ by one ulp (forge_nav N1); rotated shapes are covered,
with that one exemption, by tests/test_map_runtime_js.py.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import SKILLS_DIR, load_script, require_node, run_cli, script_path

pytestmark = [pytest.mark.node, pytest.mark.e2e]

NAV = load_script("generate2dmap", "forge_nav")
RUNTIME = SKILLS_DIR / "generate2dmap" / "references" / "runtime" / "map-runtime.mjs"
PREVIEW = script_path("generate2dmap", "build_scene_preview")
BUNDLE = "generate2dmap.map_bundle.v2"
STEP = 0.25  # lattice spacing: every coordinate of the fixtures is a multiple of it
MARGIN = 4.0  # lattice points this far outside the world are tested too


# --------------------------------------------------------------------------- small writers

def write_png(path: Path, pixels: np.ndarray) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.ascontiguousarray(pixels.astype(np.uint8))).save(path)
    return path.name


def prop_art(path: Path, width: int, height: int, colour: tuple[int, int, int]) -> None:
    """A small opaque sprite with a transparent border column, so anchors and flips are visible in the page."""
    pixels = np.zeros((height, width, 4), np.uint8)
    pixels[1:, 1:-1] = (*colour, 255)
    write_png(path, pixels)


def material_image(path: Path, labels: np.ndarray, materials: dict[str, dict]) -> None:
    """An opaque RGBA image whose every pixel is one material's colour (D4: every pixel classified)."""
    names = list(materials)
    palette = np.array([[int(materials[n]["color"][k:k + 2], 16) for k in (1, 3, 5)] + [255] for n in names], np.uint8)
    write_png(path, palette[labels])


def write_bundle(root: Path, bundle: dict) -> Path:
    path = root / "map-bundle.json"
    path.write_text(json.dumps(bundle, indent=1), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- fixture 1: top-down tile map

TILE = 16
TILESET_TILES = [
    {"index": 0},  # floor
    {"index": 1, "collision": [{"shape": "rect", "x": 0, "y": 0, "w": 16, "h": 16}]},  # wall: merged
    {"index": 2, "collision": [{"shape": "rect", "x": 0, "y": 8, "w": 16, "h": 8}]},  # low wall: merged
    {"index": 3, "collision": [{"shape": "ellipse", "cx": 8, "cy": 8, "rx": 5, "ry": 4}]},  # pillar: kept per tile
    {"index": 4, "collision": [{"shape": "polygon", "points": [[0, 16], [16, 0], [16, 16]]}]},  # slanted wall
    {"index": 5, "collision": [{"shape": "rect", "x": 12, "y": 4, "w": 8, "h": 8}]},  # reaches into the next cell
    {"index": 6, "collision": [{"shape": "rect", "x": 2.5, "y": 2.5, "w": 11, "h": 11}]},  # fractional: kept
    {"index": 7, "properties": {"walkable": False}},  # water: no shapes, the whole cell blocks
    {"index": 8, "collision": [{"shape": "rect", "x": 0, "y": 0, "w": 16, "h": 1},  # two thin walls
                               {"shape": "rect", "x": 0, "y": 15, "w": 16, "h": 1}]},
    {"index": 9, "collision": [{"shape": "polygon",  # a concave L
                                "points": [[0, 0], [6, 0], [6, 10], [16, 10], [16, 16], [0, 16]]}]},
    {"index": 10, "properties": {"walkable": True}},  # floor variant
    {"index": 11, "collision": [{"shape": "ellipse", "cx": 0, "cy": 8, "rx": 4.5, "ry": 3}]},  # crosses the cell edge
]
GROUND = [
    "111111111111",
    "100000a00001",
    "10300000a301",
    "100a00400001",
    "1000077700a1",
    "1a0000000001",
    "100090002001",
    "1003000a0031",
    "10000a000001",
    "111110011111",
]
DECOR = [
    "............",
    "..5.........",
    "........8...",
    ".....b......",
    "............",
    "..6.....9...",
    "............",
    "....8...b...",
    "..........5.",
    "............",
]


def _tile_rows(rows: list[str]) -> list[list[int]]:
    return [[-1 if ch == "." else int(ch, 16) for ch in row] for row in rows]


def tile_map(root: Path) -> Path:
    """Fixture 1: a 12x10-tile dungeon room (192x160 px) whose walls are tiles."""
    atlas = np.zeros((3 * TILE, 4 * TILE, 4), np.uint8)
    for index in range(12):
        row, col = divmod(index, 4)
        atlas[row * TILE:(row + 1) * TILE, col * TILE:(col + 1) * TILE] = (40 + 17 * index, 90, 200 - 13 * index, 255)
    tiles_dir = root / "tiles"
    write_png(tiles_dir / "dungeon.png", atlas)
    (tiles_dir / "dungeon.tileset.json").write_text(json.dumps({
        "schema": "generate2dmap.tileset.v1", "id": "dungeon", "image": "dungeon.png", "tile_size": TILE, "columns": 4,
        "tilecount": 12, "kind": "flat", "materials": ["stone"], "tiles": TILESET_TILES, "seamless_verified": False,
    }, indent=1), encoding="utf-8")
    prop_art(root / "props" / "crate.png", 12, 14, (150, 100, 50))
    prop_art(root / "props" / "barrel.png", 10, 14, (120, 70, 40))
    bundle = {
        "schema": BUNDLE, "id": "dungeon", "tile_size": TILE, "world": {"width": 192, "height": 160, "unit": "px"},
        "tilesets": [{"id": "dungeon", "manifest": "tiles/dungeon.tileset.json"}],
        "layers": [{"name": "ground", "kind": "tiles", "tileset": "dungeon", "data": _tile_rows(GROUND)},
                   {"name": "decor", "kind": "tiles", "tileset": "dungeon", "data": _tile_rows(DECOR)},
                   {"name": "props", "kind": "objects"}],
        "props": {
            "crate": {"image": "props/crate.png", "anchor_px": [6, 13],
                      "footprint": {"shape": "rect", "width": 10, "depth": 5, "offset": [0, -2]}},
            "barrel": {"image": "props/barrel.png", "anchor_px": [5, 13],
                       "footprint": {"shape": "ellipse", "width": 8, "depth": 4, "offset": [0.5, -1.5]}},
        },
        "objects": [
            {"id": "crate-1", "prop": "crate", "x": 56, "y": 40, "anchor_px": [6, 13], "layer": "props"},
            {"id": "crate-2", "prop": "crate", "x": 120.5, "y": 120, "anchor_px": [6, 13], "scale": 1.5,
             "flip_x": True, "layer": "props"},
            {"id": "barrel-1", "prop": "barrel", "x": 152, "y": 56.5, "anchor_px": [5, 13], "layer": "props"},
            {"id": "barrel-2", "prop": "barrel", "x": 100, "y": 100, "anchor_px": [5, 13], "solid": False,
             "layer": "props"},
        ],
        "collision": {"actorRadius": 5, "rects": [[88, 24, 0.5, 20], [120, 104, 12, 4.5], [44, 52, 0.25, 30]],
                      "solids": [{"shape": "ellipse", "cx": 72, "cy": 132, "rx": 6, "ry": 3.5, "id": "puddle"}]},
        "spawns": [{"id": "start", "x": 40, "y": 120}, {"id": "east", "x": 168, "y": 96}],
        "interactions": [{"id": "chest", "x": 72, "y": 24, "reach": 10}, {"id": "lever", "x": 104, "y": 72},
                         {"id": "sealed", "x": 120, "y": 72, "reach": 3}],
        "anchors": {"altar": {"point": [152, 136], "slots": [[148, 128], [160, 128]], "approach": [[152, 120]]}},
        "portals": [
            {"id": "door", "rect": [80, 152, 32, 8], "to": "hall:from-dungeon", "activation": "intent",
             "travelDirection": [0, 1], "radius": 6, "entranceByFrom": {"hall": [96, 136]}},
            {"id": "stairs", "circle": [168, 40, 6], "to": "tower", "activation": "crossing"},
        ],
    }
    return write_bundle(root, bundle)


# --------------------------------------------------------------------------- fixture 2: HD-2D plate

PLATE_MATERIALS = {
    "grass": {"class": "decor", "color": "#4a8a3c"},
    "sand": {"class": "decor", "color": "#c8b070"},
    "rock": {"class": "solid", "color": "#606068"},
    "water": {"class": "liquid", "color": "#2a5aa8"},
    "shallows": {"class": "liquid", "color": "#4a8ad0", "walkable": True},
    "embers": {"class": "hazard", "color": "#d05020"},
}


PLATE_CLEAR = [(30, 122), (236, 80), (240, 108), (150, 100), (186, 124), (205, 130), (215, 125), (190, 110)]


def hd2d_plate(root: Path) -> Path:
    """Fixture 2: a 256x144 battle plate with walk regions, holes, solids, props and a material map."""
    rng = np.random.default_rng(29)
    labels = np.zeros((36, 64), np.int64)  # 4 world px per material pixel
    labels[rng.random(labels.shape) < 0.25] = 1  # sand speckle (decor)
    labels[20:26, 6:14] = 3  # a pond
    labels[19:27, 5:6] = 4  # its shallow rim
    labels[30:33, 44:52] = 4
    labels[8:11, 30:34] = 2  # rocks
    labels[24, 40:43] = 2
    labels[28:30, 22:27] = 5  # embers
    pebbles = rng.random(labels.shape) < 0.01  # scattered pebbles that block, kept off the spawns and targets
    for x, y in PLATE_CLEAR:
        pebbles[max(0, y // 4 - 3):y // 4 + 4, max(0, x // 4 - 3):x // 4 + 4] = False
    labels[pebbles] = 2
    material_image(root / "materials.png", labels, PLATE_MATERIALS)
    prop_art(root / "props" / "lantern.png", 8, 20, (230, 200, 90))
    prop_art(root / "props" / "banner.png", 12, 24, (180, 40, 60))
    prop_art(root / "props" / "pack" / "statue.png", 14, 26, (150, 150, 160))
    (root / "props" / "pack" / "prop-pack.json").write_text(json.dumps({
        "schema": "generate2dmap.prop_pack.v2", "rejected": [],
        "accepted": [{"label": "statue", "image": "statue.png", "anchor_px": [7, 25], "status": "accepted",
                      "solid": False, "footprint": {"shape": "rect", "width": 10, "depth": 5, "offset": [0, -2]}}],
    }), encoding="utf-8")
    bundle = {
        "schema": BUNDLE, "id": "plate", "world": {"width": 256, "height": 144, "unit": "px"},
        "layers": [{"name": "props", "kind": "objects"}],
        "material_map": {"image": "materials.png", "materials": PLATE_MATERIALS},
        "props": {
            "lantern": {"image": "props/lantern.png", "anchor_px": [4, 19],
                        "footprint": {"shape": "ellipse", "width": 6, "depth": 3, "offset": [1, -1]}},
            "banner": {"image": "props/banner.png", "anchor_px": [6, 23], "solid": False,
                       "footprint": {"shape": "rect", "width": 8, "depth": 4}},
            "statue": {"pack": "props/pack/prop-pack.json", "label": "statue", "solid": True},  # overrides the pack
        },
        "objects": [
            {"id": "lantern-a", "prop": "lantern", "x": 60, "y": 120, "anchor_px": [4, 19], "scale": 1.5,
             "layer": "props"},
            {"id": "lantern-b", "prop": "lantern", "x": 190.25, "y": 82, "anchor_px": [4, 19], "flip_x": True,
             "layer": "props"},
            {"id": "crate", "prop": "lantern", "x": 130, "y": 126, "anchor_px": [4, 19], "scale": 2,
             "footprint": {"shape": "rect", "width": 9, "depth": 4.5, "offset": [-0.5, -1], "basis": "world_px"},
             "layer": "props"},
            {"id": "banner-a", "prop": "banner", "x": 96, "y": 112, "anchor_px": [6, 23], "layer": "props"},
            {"id": "banner-b", "prop": "banner", "x": 226, "y": 132, "anchor_px": [6, 23], "solid": True,
             "layer": "props"},
            {"id": "post", "prop": "lantern", "x": 30, "y": 70, "anchor_px": [4, 19], "footprint": {"shape": "none"},
             "layer": "props"},
            {"id": "statue", "prop": "statue", "x": 110, "y": 76, "anchor_px": [7, 25], "flip_x": True,
             "layer": "props"},
        ],
        "collision": {
            "actorRadius": 6, "ySquash": 0.58,
            "walkRegions": [
                {"polygon": [[8, 60], [180, 52], [200, 96], [120, 140], [10, 136]],
                 "holes": [[[40, 80], [70, 80], [70, 100], [40, 100]], [[100, 100], [120, 90], [130, 115]]]},
                {"polygon": [[170, 70], [250, 64], [252, 140], [176, 140]],
                 "holes": [[[200, 100], [230, 100], [230, 120], [200, 120]]]},
            ],
            "solids": [
                {"shape": "ellipse", "cx": 90, "cy": 70, "rx": 10, "ry": 5, "id": "boulder"},
                {"shape": "ellipse", "cx": 215.5, "cy": 90, "rx": 7.5, "ry": 4},
                {"shape": "polygon", "points": [[140, 70], [160, 66], [150, 80], [162, 92], [138, 90]], "id": "ruin"},
                {"shape": "rect", "x": 20, "y": 110, "w": 12.5, "h": 6},
            ],
            "rects": [[176, 120, 6, 0.75]],
        },
        "spawns": [{"id": "party", "x": 30, "y": 122}, {"id": "reserve", "x": 236, "y": 80}],
        "interactions": [{"id": "shrine", "x": 150, "y": 100, "reach": 12}, {"id": "well", "x": 55, "y": 90},
                         {"id": "ledge", "x": 186, "y": 124}],
        "anchors": {"boss": {"point": [215, 128], "slots": [[205, 130], [225, 130]],
                             "approach": [[215, 125], [190, 110], [215, 112]]}},
        "portals": [
            {"id": "east-gate", "rect": [244, 90, 8, 30], "to": "camp", "activation": "intent",
             "travelDirection": [1, 0], "radius": 8, "entranceByFrom": {"camp": [240, 108]}},
            {"id": "west-path", "circle": [16, 96, 8], "to": "road:from-plate", "activation": "crossing"},
        ],
    }
    return write_bundle(root, bundle)


# --------------------------------------------------------------------------- fixture 3: side-scroll room

ROOM_MATERIALS = {  # matched by palette index: the room's material map is a P-mode image (N8)
    "air": {"class": "decor", "index": 0},
    "ground": {"class": "solid", "index": 1},
    "ledge": {"class": "one_way", "index": 2},
    "spikes": {"class": "hazard", "index": 3},
    "pool": {"class": "liquid", "index": 4, "walkable": True},
}


def side_scroll(root: Path) -> Path:
    """Fixture 3: a 256x128 side view: solid floor, one_way ledges at three heights, spikes and a pool."""
    labels = np.zeros((32, 64), np.int64)  # 4 world px per material pixel
    labels[28:, :] = 1  # floor
    labels[:, :2] = 1  # walls
    labels[:, 62:] = 1
    labels[21, 8:22] = 2  # ledges
    labels[15, 20:36] = 2
    labels[9, 34:50] = 2
    labels[21, 44:58] = 2
    labels[27, 26:32] = 3  # spikes on the floor
    labels[24:28, 50:56] = 4  # a pool the actor may wade
    labels[14:21, 52:54] = 1  # a pillar under a ledge
    image = Image.fromarray(labels.astype(np.uint8))  # L; putpalette makes it a P image of these indices
    image.putpalette([32, 40, 56, 112, 88, 56, 192, 160, 64, 192, 48, 48, 48, 96, 160] + [0] * (3 * 251))
    root.mkdir(parents=True, exist_ok=True)
    image.save(root / "materials.png")
    bundle = {
        "schema": BUNDLE, "id": "room", "world": {"width": 256, "height": 128, "unit": "px"},
        "material_map": {"image": "materials.png", "materials": ROOM_MATERIALS},
        "collision": {
            "actorRadius": 4,
            "solids": [{"shape": "polygon", "points": [[96, 112], [124, 96], [124, 112]], "id": "slope"},
                       {"shape": "rect", "x": 172, "y": 40, "w": 4, "h": 24, "id": "hanging-wall"}],
        },
        "spawns": [{"id": "floor", "x": 24, "y": 104}, {"id": "top", "x": 160, "y": 28}],
        "interactions": [{"id": "low-ledge", "x": 40, "y": 76}, {"id": "mid-ledge", "x": 110, "y": 52},
                         {"id": "high-ledge", "x": 168, "y": 30, "reach": 6}, {"id": "pool", "x": 212, "y": 104}],
        "anchors": {"chest": {"point": [200, 76], "slots": [[204, 76]], "approach": [[192, 76]]}},
        "portals": [{"id": "exit", "rect": [240, 88, 8, 24], "to": "next", "activation": "intent",
                     "travelDirection": [1, 0], "radius": 6}],
    }
    return write_bundle(root, bundle)


FIXTURES = {"tile-map": tile_map, "hd2d-plate": hd2d_plate, "side-scroll": side_scroll}


# --------------------------------------------------------------------------- the two sides

def scene_of(page: Path) -> dict:
    """The SCENE object build_scene_preview embeds: the runtime bundle and the material grid."""
    match = re.search(r"const SCENE = (\{.*?\});\n", page.read_text(encoding="ascii"), re.S)
    if match is None:
        raise AssertionError(f"no SCENE in {page}")
    return json.loads(match.group(1))


_JS = r"""
import * as rt from @@RUNTIME@@;
import {readFileSync} from "node:fs";
const job = JSON.parse(readFileSync(0, "utf8"));
const world = rt.createMapRuntime(job.scene.bundle, {materialGrid: job.scene.materialGrid});
const {x0, y0, step, nx, ny} = job.lattice;
const free = new Uint8Array(nx * ny), valid = new Uint8Array(nx * ny);
for (let j = 0; j < ny; j++) {
  const y = y0 + j * step;
  for (let i = 0; i < nx; i++) {
    const x = x0 + i * step;
    free[j * nx + i] = rt.pointFree(world, x, y) ? 1 : 0;
    valid[j * nx + i] = rt.isValid(world, x, y) ? 1 : 0;
  }
}
const bits = (array) => Buffer.from(array).toString("base64");
const pointFree = job.points.map(([x, y]) => (rt.pointFree(world, x, y) ? 1 : 0));
const pointValid = job.points.map(([x, y]) => (rt.isValid(world, x, y) ? 1 : 0));
const clear = job.segments.map(([ax, ay, bx, by]) => (rt.segmentClear(world, ax, ay, bx, by) ? 1 : 0));
const sampled = job.segments.map(([ax, ay, bx, by]) =>
  (rt.segmentClear(world, ax, ay, bx, by, {thinGap: false}) ? 1 : 0));
const nav = rt.navGrid(world), total = nav.cols * nav.rows;
const nodes = new Uint8Array(total), moves = new Uint8Array(total);
for (let k = 0; k < total; k++) {
  nodes[k] = rt.cellValid(world, k) ? 1 : 0;
  const i = k % nav.cols;
  let m = 0;
  if (i + 1 < nav.cols && rt.moveOpen(world, k, k + 1)) m |= 1;
  if (k + nav.cols < total && rt.moveOpen(world, k, k + nav.cols)) m |= 2;
  if (i > 0 && rt.moveOpen(world, k, k - 1)) m |= 4;
  if (k >= nav.cols && rt.moveOpen(world, k, k - nav.cols)) m |= 8;
  moves[k] = m;
}
const field = rt.floodFrom(world, rt.routeStarts(world).map((s) => [s.x, s.y]));
const targets = {};
for (const item of world.interactions) {
  const answer = item.reach === null ? rt.pointTarget(world, field, item.x, item.y)
    : rt.reachTarget(world, field, item.x, item.y, item.reach);
  targets[`interaction:${item.id}`] = [answer.node, null];
}
for (const anchor of world.anchors) {
  anchor.slots.forEach(([x, y], k) => {
    targets[`slot:${anchor.name}/${k}`] = [rt.pointTarget(world, field, x, y).node, null];
  });
  anchor.approach.forEach(([x, y], k) => {
    targets[`approach:${anchor.name}/${k}`] = [rt.pointTarget(world, field, x, y).node, null];
  });
}
for (const portal of world.portals) {
  const answer = rt.exitTarget(world, field, portal);
  targets[`exit:${portal.id}`] = [answer.node, answer.entry];
}
process.stdout.write(JSON.stringify({free: bits(free), valid: bits(valid), pointFree, pointValid, clear, sampled,
  cell: world.cell, cols: nav.cols, rows: nav.rows, nodes: bits(nodes), moves: bits(moves),
  dist: Buffer.from(field.dist.buffer).toString("base64"), seeds: field.seeds, targets}));
"""


def run_runtime(scene: dict, lattice: dict, points: list, segments: list) -> dict:
    node = require_node()
    code = _JS.replace("@@RUNTIME@@", json.dumps(RUNTIME.as_uri()))
    payload = json.dumps({"scene": scene, "lattice": lattice, "points": points, "segments": segments})
    completed = subprocess.run([node, "--input-type=module", "-e", code], input=payload, capture_output=True,
                               encoding="utf-8", errors="replace", timeout=900, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"map-runtime.mjs query failed:\n{completed.stderr[-3000:]}")
    return json.loads(completed.stdout)


def unbits(text: str, count: int) -> np.ndarray:
    array = np.frombuffer(base64.b64decode(text), np.uint8)
    assert array.size == count, (array.size, count)
    return array


def python_targets(model, grid, bundle: dict) -> tuple[dict, object]:
    """forge_nav's N14 answers (node index row * cols + col, and the entry point of a crossing exit)."""
    starts = [(s["x"], s["y"]) for s in bundle.get("spawns", [])]
    for portal in bundle.get("portals", []):
        starts += [tuple(p) for p in (portal.get("entranceByFrom") or {}).values() if isinstance(p, list)]
    navigation = NAV.navigate(model, starts, grid)

    def node(reach) -> int:
        return reach.cell[0] * grid.cols + reach.cell[1] if reach.reachable else -1

    targets = {}
    for item in bundle.get("interactions", []):
        point = (item["x"], item["y"])
        reach = (navigation.reach_target(point, item["reach"]) if "reach" in item
                 else navigation.point_target(point))
        targets[f"interaction:{item['id']}"] = [node(reach), None]
    for name, anchor in (bundle.get("anchors") or {}).items():
        for k, slot in enumerate(anchor.get("slots") or []):
            targets[f"slot:{name}/{k}"] = [node(navigation.point_target(slot)), None]
        for k, point in enumerate(anchor.get("approach") or []):
            targets[f"approach:{name}/{k}"] = [node(navigation.point_target(point)), None]
    for portal in bundle.get("portals", []):
        activation = portal.get("activation", "crossing")
        reach = navigation.exit_target(NAV.Trigger.from_portal(portal), activation, portal.get("radius", 0))
        entry = None
        if reach.reachable and activation != "intent" and reach.node != (float(grid.xs[reach.cell[1]]),
                                                                         float(grid.ys[reach.cell[0]])):
            entry = [float(reach.node[0]), float(reach.node[1])]
        targets[f"exit:{portal['id']}"] = [node(reach), entry]
    return targets, navigation


# --------------------------------------------------------------------------- probes

def _edge_points(polygon) -> list[tuple[float, float]]:
    points = []
    for (ax, ay), (bx, by) in zip(polygon, list(polygon[1:]) + list(polygon[:1])):
        for t in (0.0, 0.125, 0.25, 0.5, 0.75):
            points.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return points


def boundary_points(blocking, model) -> list[list[float]]:
    """Vertices, edge points and ellipse extremes of every blocker, walk region and hole and of the world box,
    each also moved by minus every footprint offset, so that one of the nine samples lands on it (up to the
    rounding of p - o + o, which both sides share)."""
    base: list[tuple[float, float]] = []
    w, h = blocking.width, blocking.height
    base += _edge_points([(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)])
    for polygon, holes in blocking.regions:
        for ring in [polygon, *holes]:
            base += _edge_points([tuple(map(float, p)) for p in ring])
    for solid in blocking.solids:
        if solid["shape"] == "rect":
            x, y, sw, sh = (float(solid[k]) for k in ("x", "y", "w", "h"))
            base += _edge_points([(x, y), (x + sw, y), (x + sw, y + sh), (x, y + sh)])
        elif solid["shape"] == "ellipse":
            cx, cy, rx, ry = (float(solid[k]) for k in ("cx", "cy", "rx", "ry"))
            base += [(cx + rx, cy), (cx - rx, cy), (cx, cy + ry), (cx, cy - ry), (cx, cy)]
            base += [(cx + rx * np.cos(t), cy + ry * np.sin(t)) for t in np.linspace(0, 2 * np.pi, 16, endpoint=False)]
        else:
            base += _edge_points([tuple(map(float, p)) for p in solid["points"]])
    points = []
    for x, y in base:
        points.append([float(x), float(y)])
        for ox, oy in model.offsets[1:]:
            points.append([float(x - ox), float(y - oy)])
    return points


def probe_segments(blocking, model, seed: int) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    w, h = blocking.width, blocking.height
    ends = np.round(rng.uniform([-2, -2, -2, -2], [w + 2, h + 2, w + 2, h + 2], (500, 4)) * 4) / 4
    segments = ends.tolist()
    starts = np.round(rng.uniform([0, 0], [w, h], (300, 2)) * 2) / 2
    for (x, y), length in zip(starts, rng.uniform(1, 14, 300)):  # vertical drops and climbs (one_way)
        segments.append([float(x), float(y), float(x), float(y + length * rng.choice([-1, 1]))])
    for solid in blocking.solids:
        if solid["shape"] == "rect":
            x, y, sw, sh = (float(solid[k]) for k in ("x", "y", "w", "h"))
            corners = [(x, y), (x + sw, y), (x + sw, y + sh), (x, y + sh)]
        elif solid["shape"] == "polygon":
            corners = [tuple(map(float, p)) for p in solid["points"]]
        else:  # a horizontal line that touches the ellipse at its top and bottom, and one through its centre
            cx, cy, rx, ry = (float(solid[k]) for k in ("cx", "cy", "rx", "ry"))
            for yy in (cy - ry, cy + ry, cy):
                segments.append([cx - rx - 6, yy, cx + rx + 6, yy])
            segments.append([cx, cy - ry - 6, cx, cy + ry + 6])
            continue
        for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
            segments.append([ax, ay, bx, by])  # along the edge
            segments.append([ax - (by - ay) * 0.5, ay + (bx - ax) * 0.5, ax, ay])  # ends on a vertex
    for polygon, holes in blocking.regions:
        for ring in [polygon, *holes]:
            corners = [tuple(map(float, p)) for p in ring]
            for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
                segments.append([ax, ay, bx, by])
    return segments


# --------------------------------------------------------------------------- the test

class CollisionParityTests(unittest.TestCase):
    """forge_nav and map-runtime.mjs agree exactly (D1, D2, D4) on what build_scene_preview hands the runtime."""

    def compare(self, name: str) -> dict:
        require_node()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_path = FIXTURES[name](root / "map")
            preview = root / "preview"
            built = run_cli([PREVIEW, "--bundle", bundle_path, "--output-dir", preview], "cp1252", timeout=300)
            self.assertEqual(built.returncode, 0, built.stderr)
            summary = json.loads(built.stdout)
            self.assertEqual(summary["status"], "pass", f"{name}: preview QA {summary}")
            scene = scene_of(preview / "preview.html")
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            blocking = NAV.read_blocking_set(bundle_path)
        model = blocking.model()
        w, h = blocking.width, blocking.height
        nx, ny = int((w + 2 * MARGIN) / STEP) + 1, int((h + 2 * MARGIN) / STEP) + 1
        lattice = {"x0": -MARGIN, "y0": -MARGIN, "step": STEP, "nx": nx, "ny": ny}
        points = boundary_points(blocking, model)
        segments = probe_segments(blocking, model, seed=len(name))
        result = run_runtime(scene, lattice, points, segments)

        xs = -MARGIN + np.arange(nx) * STEP
        ys = -MARGIN + np.arange(ny) * STEP
        gx, gy = np.meshgrid(xs, ys)
        px, py = gx.ravel(), gy.ravel()
        for key, expected in (("free", model.centre_ok(px, py)), ("valid", model.valid(px, py))):
            got = unbits(result[key], nx * ny).astype(bool)
            bad = np.flatnonzero(got != expected)
            self.assertEqual(bad.size, 0, f"{name}: lattice {key} differs at {bad.size} points, e.g. "
                                          f"{np.column_stack([px[bad[:5]], py[bad[:5]]]).tolist()}")
        bx, by = np.array(points).T
        for key, expected in (("pointFree", model.centre_ok(bx, by)), ("pointValid", model.valid(bx, by))):
            bad = np.flatnonzero(np.array(result[key], bool) != expected)
            self.assertEqual(bad.size, 0, f"{name}: boundary {key} differs at {[points[k] for k in bad[:5]]}")
        for key, thin in (("clear", True), ("sampled", False)):
            expected = [int(model.segment_clear(s[:2], s[2:], thin_gap=thin)) for s in segments]
            bad = [s for s, a, b in zip(segments, result[key], expected) if a != b]
            self.assertEqual(bad, [], f"{name}: segmentClear(thinGap={thin}) differs on {len(bad)} segments")
            self.assertTrue(0 < sum(expected) < len(expected), "both clear and blocked segments are probed")

        grid = NAV.build_grid(model)
        self.assertEqual((result["cell"], result["rows"], result["cols"]), (grid.cell, grid.rows, grid.cols))
        total = grid.rows * grid.cols
        np.testing.assert_array_equal(unbits(result["nodes"], total).reshape(grid.rows, grid.cols).astype(bool),
                                      grid.valid, f"{name}: grid nodes")
        np.testing.assert_array_equal(unbits(result["moves"], total).reshape(grid.rows, grid.cols), grid.moves,
                                      f"{name}: open moves")
        targets, navigation = python_targets(model, grid, bundle)
        dist = np.frombuffer(base64.b64decode(result["dist"]), np.int32).reshape(grid.rows, grid.cols)
        np.testing.assert_array_equal(dist, navigation.distance, f"{name}: BFS distances")
        self.assertEqual(result["seeds"], [r * grid.cols + c for r, c in navigation.seeds], f"{name}: BFS seeds")
        self.assertEqual(result["targets"], targets, f"{name}: N14 targets (node, entry)")
        return {"blocking": blocking, "grid": grid, "navigation": navigation, "targets": targets,
                "valid": model.valid(px, py), "segments": len(segments), "points": len(points)}

    def test_top_down_tile_map_with_per_tile_collision(self):
        facts = self.compare("tile-map")
        sources = {solid["source"] for solid in facts["blocking"].solids}
        self.assertIn("tiles:ground", sources)
        self.assertIn("tiles:decor", sources)
        self.assertTrue({"object:crate-1", "object:crate-2", "object:barrel-1"} <= sources)
        self.assertNotIn("object:barrel-2", sources, "solid: false never blocks")
        kinds = {solid["shape"] for solid in facts["blocking"].tiles}
        self.assertEqual(kinds, {"rect", "ellipse", "polygon"}, "merged rects and kept shapes")
        self.assertTrue(facts["grid"].thin_gaps, "a wall thinner than half a cell is closed by the thin-gap rule")
        self.assertTrue(all(start.reachable for start in facts["navigation"].starts))
        unreachable = {key for key, (node, _) in facts["targets"].items() if node < 0}
        self.assertEqual(unreachable, {"interaction:lever", "interaction:sealed"}, "both stand in the water tiles")

    def test_hd2d_plate_with_walk_regions_solids_and_materials(self):
        facts = self.compare("hd2d-plate")
        blocking = facts["blocking"]
        self.assertEqual(blocking.y_squash, 0.58)
        self.assertEqual(blocking.blocking_material_classes, ["solid", "liquid", "hazard"])
        self.assertEqual(blocking.material_scale, 4)
        sources = {solid["source"] for solid in blocking.solids}
        self.assertTrue({"object:lantern-a", "object:lantern-b", "object:crate", "object:banner-b",
                         "object:statue"} <= sources, "the statue's footprint comes from its prop pack")
        self.assertFalse({"object:banner-a", "object:post"} & sources)
        self.assertTrue(all(start.reachable for start in facts["navigation"].starts))
        unreachable = {key for key, (node, _) in facts["targets"].items() if node < 0}
        self.assertEqual(unreachable, {"interaction:well", "approach:boss/2", "slot:boss/1"},
                         "two points inside holes and a slot on a solid prop's footprint")

    def test_side_scroll_room_with_one_way_ledges(self):
        facts = self.compare("side-scroll")
        self.assertEqual(facts["blocking"].blocking_material_classes, ["solid", "one_way", "hazard"])
        self.assertGreater(facts["grid"].one_way_blocked, 0, "moves down onto a ledge are refused")
        unreachable = {key for key, (node, _) in facts["targets"].items() if node < 0}
        self.assertEqual(unreachable, {"slot:chest/0"}, "every ledge is reached from below; the slot's footprint "
                                                        "touches the pillar")


if __name__ == "__main__":
    unittest.main()
