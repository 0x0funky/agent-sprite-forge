"""Tests for export_godot.py: Godot 4.3+ TileSet .tres and TileMapLayer .tscn from a map bundle (B14-T1).

No engine is installed here, so every check is structural: the files are parsed back
(independent regexes and struct decoding where it matters) and compared with the bundle.
Collision is the forge_nav blocking set (D2, D33); prop art follows the D6 lookup order.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from forge_testutils import (assert_cli_help, assert_valid_contract, contract_errors, load_script, run_cli,
                             script_path)


GODOT = load_script("generate2dmap", "export_godot")
NAV = load_script("generate2dmap", "forge_nav")
SCRIPT = script_path("generate2dmap", "export_godot")
SKILL = "generate2dmap"
T = 16


def requested_contract_errors(document, name):
    """Errors against the vendored generate2dmap schemas, which hold this module's section 5 requests (D33)."""
    return contract_errors(document, "map", name, skill=SKILL)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def wang_corners(index):
    return [(index >> 3) & 1, (index >> 2) & 1, (index >> 1) & 1, index & 1]


def build_bundle(root, *, object_x=40.0, extra_objects=(), blob=False):
    """A deterministic water/grass Wang-16 map (10x6 tiles), two props from a prop pack, collision and markers.

    Returns (bundle path, tile grid). Tile 0 is all water and fully solid; each water quadrant of a
    mixed tile is one 8x8 collision rect.
    """
    root = Path(root)
    (root / "tiles").mkdir(parents=True)
    atlas = np.zeros((4 * T, 4 * T, 4), np.uint8)
    tiles = []
    for index in range(16):
        corners = wang_corners(index)
        ax, ay = index % 4, index // 4
        quads = list(zip(corners, ((0, 0), (8, 0), (0, 8), (8, 8))))  # (x0, y0) of tl, tr, bl, br
        for grass, (x0, y0) in quads:
            atlas[ay * T + y0:ay * T + y0 + 8, ax * T + x0:ax * T + x0 + 8] = \
                (60, 160 + index, 70, 255) if grass else (50, 90, 200 + index, 255)
        tile = {"index": index, "variant": 0, "properties": {"walkable": index == 15}}
        if blob:
            tile["blob_mask"] = [0, 1, 4, 5, 16, 17, 20, 21, 64, 65, 68, 69, 80, 81, 84, 255][index]
        else:
            tile["wang"] = corners
        solids = [{"shape": "rect", "x": x0, "y": y0, "w": 8, "h": 8} for grass, (x0, y0) in quads if not grass]
        if solids:
            tile["collision"] = solids
        tiles.append(tile)
    Image.fromarray(atlas).save(root / "tiles" / "terrain.png")
    tileset = {"schema": "generate2dmap.tileset.v1", "image": "terrain.png",
               "sha256": _sha(root / "tiles" / "terrain.png"),
               "tile_size": T, "columns": 4, "kind": "blob47" if blob else "wang_corner",
               "materials": ["grass", "dirt"] if blob else ["water", "grass"], "tiles": tiles,
               "seamless_verified": False}
    (root / "tiles" / "terrain.tileset.json").write_text(json.dumps(tileset), encoding="utf-8")
    rng = np.random.default_rng(7)
    vertex = (rng.random((7, 11)) > 0.4).astype(int)
    grid = vertex[:-1, :-1] * 8 + vertex[:-1, 1:] * 4 + vertex[1:, :-1] * 2 + vertex[1:, 1:]
    grid[0, 0] = -1
    (root / "layers").mkdir()
    (root / "layers" / "ground.csv").write_text("\n".join(",".join(map(str, row)) for row in grid) + "\n",
                                                encoding="utf-8")
    for label, size, anchor in (("tree", (24, 40), (12, 39)), ("rock", (14, 10), (7, 10))):
        (root / "props" / label).mkdir(parents=True)
        pixels = np.zeros((size[1], size[0], 4), np.uint8)
        pixels[1:-1, 1:-1] = (30 + size[0], 120, 40, 255)
        Image.fromarray(pixels).save(root / "props" / label / "prop.png")
    pack = {"schema": "generate2dmap.prop_pack.v2", "rejected": [], "accepted": [
        {"label": label, "image": f"{label}/prop.png", "sha256": _sha(root / "props" / label / "prop.png"),
         "anchor_px": anchor} for label, anchor in (("tree", [12, 39]), ("rock", [7, 10]))]}
    (root / "props" / "prop-pack.json").write_text(json.dumps(pack), encoding="utf-8")
    Image.new("RGBA", (160, 96), (200, 220, 180, 255)).save(root / "base.png")
    # material map: 16 px squares (N8); the top-left cell is deep water, every other pixel has no material
    materials = np.zeros((6, 10, 4), np.uint8)
    materials[0, 0] = (50, 100, 200, 255)
    Image.fromarray(materials).save(root / "materials.png")
    objects = [
        {"id": "tree-1", "prop": "tree", "x": object_x, "y": 60, "anchor_px": [12, 39], "sortY": 60, "solid": True,
         "footprint": {"shape": "ellipse", "width": 8, "depth": 4}},
        {"id": "tree-2", "prop": "tree", "x": 100, "y": 50, "anchor_px": [12, 39], "scale": 2, "sortY": 46},
        {"id": "rock-1", "prop": "rock", "x": 130, "y": 80, "anchor_px": [7, 10]},
        *extra_objects]
    bundle = {
        "schema": "generate2dmap.map_bundle.v2", "tile_size": [T, T],
        "world": {"width": 160, "height": 96, "unit": "px"},
        "tilesets": [{"id": "terrain", "manifest": "tiles/terrain.tileset.json",
                      "sha256": _sha(root / "tiles" / "terrain.tileset.json")}],
        "prop_packs": [{"manifest": "props/prop-pack.json", "sha256": _sha(root / "props" / "prop-pack.json")}],
        "layers": [{"name": "base", "kind": "image", "image": "base.png"},
                   {"name": "ground", "kind": "tiles", "data": "layers/ground.csv", "tileset": "terrain",
                    "sha256": _sha(root / "layers" / "ground.csv")},
                   {"name": "objects", "kind": "objects"}],
        "objects": objects,
        "collision": {"actorRadius": 4, "ySquash": 1.0,
                      "walkRegions": [{"polygon": [[0, 0], [160, 0], [160, 96], [0, 96]]}],
                      "solids": [{"shape": "rect", "x": 36, "y": 58, "w": 8, "h": 4},
                                 {"shape": "ellipse", "cx": 100, "cy": 50, "rx": 6, "ry": 3},
                                 {"shape": "ellipse", "cx": 20, "cy": 20, "rx": 5, "ry": 5},
                                 {"shape": "polygon", "points": [[120, 70], [140, 70], [130, 82]]}],
                      "rects": [[0, 0, 160, 4]]},
        "portals": [{"id": "exit-east", "rect": [150, 40, 10, 16], "to": "route-1:spawn-west", "activation": "intent",
                     "travelDirection": [1, 0], "radius": 12, "entranceByFrom": {"route-1": "spawn-east"},
                     "latch": True, "requiresMovement": True},
                    {"id": "well-hole", "circle": [80, 48, 6], "to": "cave:spawn"}],
        "spawns": [{"id": "spawn-west", "x": 12, "y": 48, "facing": "east"}],
        "anchors": {"well": {"point": [80, 40], "facing": "south", "slots": [[76, 44], [84, 44]],
                             "approach": [80, 46]}},
        "interactions": [{"id": "well", "x": 80, "y": 40, "reach": 10}, {"id": "sign", "x": 30, "y": 30}],
        "material_map": {"image": "materials.png", "materials": {"water": {"class": "liquid", "color": "#3264c8"}}},
        "art_source": "code",
    }
    path = root / "map-bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return path, grid


def export(bundle, out, *extra):
    return run_cli([SCRIPT, "--bundle", bundle, "--output-dir", out, "--name", "town", *extra])


def tres_tiles(text):
    """Independent reader: {(x, y): {"terrain_set", "terrain", bits..., "polygons": {n: [floats]}}}."""
    tiles = {}
    for x, y, key, value in re.findall(r"^(\d+):(\d+)/0/([\w/]+) = (.+)$", text, re.M):
        entry = tiles.setdefault((int(x), int(y)), {"bits": {}, "polygons": {}})
        if key.startswith("terrains_peering_bit/"):
            entry["bits"][key.split("/", 1)[1]] = int(value)
        elif key in ("terrain_set", "terrain"):
            entry[key] = int(value)
        elif match := re.fullmatch(r"physics_layer_0/polygon_(\d+)/points", key):
            entry["polygons"][int(match.group(1))] = [float(v) for v in
                                                      re.fullmatch(r"PackedVector2Array\((.*)\)", value)
                                                      .group(1).split(",")]
        elif key == "custom_data_0":
            entry["walkable"] = value == "true"
    return tiles


def tile_layer_cells(scene_text, node):
    """Independent decoder of a TileMapLayer's tile_map_data: {(x, y): (source, atlas_x, atlas_y, alt)}."""
    block = re.search(rf'\[node name="{node}" type="TileMapLayer" parent="\."\]\n(.*?)(?:\n\n|\Z)', scene_text, re.S)
    data = bytes(int(v) for v in re.search(r"tile_map_data = PackedByteArray\(([^)]*)\)", block.group(1))
                 .group(1).split(","))
    assert struct.unpack_from("<H", data, 0)[0] == 0 and (len(data) - 2) % 12 == 0
    cells = {}
    for offset in range(2, len(data), 12):
        x, y, source, ax, ay, alt = struct.unpack_from("<hhHHHH", data, offset)
        cells[(x, y)] = (source, ax, ay, alt)
    return cells


class ExportGodotTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.bundle, self.grid = build_bundle(self.root / "map")
        self.out = self.root / "godot"

    def tearDown(self):
        self._temporary.cleanup()

    def run_export(self, *extra, out=None, bundle=None):
        result = export(bundle or self.bundle, out or self.out, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_fixture_bundle_and_tileset_satisfy_the_contracts(self):
        bundle = json.loads(self.bundle.read_text(encoding="utf-8"))
        assert_valid_contract(bundle, "map", "map_bundle_v2", skill=SKILL)
        manifest = json.loads((self.root / "map" / "tiles" / "terrain.tileset.json").read_text(encoding="utf-8"))
        assert_valid_contract(manifest, "map", "tileset_v1", skill=SKILL)
        bundle["objects"][2].update(image="props/rock/prop.png", image_sha256="0" * 64)
        self.assertEqual(requested_contract_errors(bundle, "map_bundle_v2"), [])
        self.assertEqual(requested_contract_errors(dict(manifest, blob_inside="grass"), "tileset_v1"), [])
        bundle["prop_packs"] = [{"sha256": "0" * 64}]
        self.assertTrue(requested_contract_errors(bundle, "map_bundle_v2"))
        bundle["prop_packs"] = []
        bundle["objects"][0]["sortY"] = None
        self.assertTrue(requested_contract_errors(bundle, "map_bundle_v2"), "sortY is a number")

    def test_tres_parses_and_peering_bits(self):
        summary = self.run_export()
        text = (self.out / "town.tileset.tres").read_text(encoding="utf-8")
        self.assertTrue(text.startswith('[gd_resource type="TileSet" load_steps=3 format=3]\n'))
        self.assertIn('[ext_resource type="Texture2D" path="assets/tilesets/terrain.png"', text)
        self.assertIn("terrain_set_0/mode = 1\n", text)  # TERRAIN_MODE_MATCH_CORNERS
        self.assertIn('terrain_set_0/terrain_0/name = "water"\n', text)
        self.assertIn('terrain_set_0/terrain_1/name = "grass"\n', text)
        self.assertLess(text.index("terrain_set_0/mode"), text.index("sources/0 = "))  # layers before sources
        tiles = tres_tiles(text)
        self.assertEqual(len(tiles), 16)
        for index in range(16):
            tl, tr, bl, br = wang_corners(index)
            entry = tiles[(index % 4, index // 4)]
            self.assertEqual(entry["terrain_set"], 0)
            self.assertEqual(entry["bits"], {"top_left_corner": tl, "top_right_corner": tr,
                                             "bottom_left_corner": bl, "bottom_right_corner": br}, index)
            self.assertEqual(entry["terrain"], 1 if tl + tr + bl + br > 2 else 0, index)
            self.assertEqual(entry["walkable"], index == 15)
            self.assertEqual(len(entry["polygons"]), 4 - (tl + tr + bl + br))
        # tile 0 is all water: four 8x8 quads around the tile centre
        self.assertEqual(tiles[(0, 0)]["polygons"][0], [-8, -8, 0, -8, 0, 0, -8, 0])
        self.assertEqual(tiles[(0, 0)]["polygons"][3], [0, 0, 8, 0, 8, 8, 0, 8])
        self.assertEqual(sorted(GODOT.parse_godot_text(text)[0].fields), ["format", "load_steps", "type"])
        report = json.loads((self.out / "godot-export.json").read_text(encoding="utf-8"))
        self.assertEqual({check["id"]: check["status"] for check in report["qa"]["checks"]}["peering_bits_roundtrip"],
                         "pass")
        self.assertEqual(report["tilesets"][0]["terrain_mode"], "match_corners")
        self.assertEqual(summary["status"], "pass")

    def test_tile_map_data_roundtrip(self):
        self.run_export()
        scene = (self.out / "town.tscn").read_text(encoding="utf-8")
        cells = tile_layer_cells(scene, "ground")
        expected = {(col, row): (0, int(value) % 4, int(value) // 4, 0)
                    for (row, col), value in np.ndenumerate(self.grid) if value >= 0}
        self.assertEqual(cells, expected)
        self.assertNotIn((0, 0), cells)  # -1 is an empty cell
        self.assertIn('tile_set = ExtResource("1_town_tileset")', scene)

    def test_layers_keep_bundle_draw_order(self):
        self.run_export()
        sections = GODOT.parse_godot_text((self.out / "town.tscn").read_text(encoding="utf-8"))
        children = [(s.fields["name"], s.fields["type"]) for s in sections
                    if s.tag == "node" and s.fields.get("parent") == "."]
        self.assertEqual(children[:3], [("base", "Sprite2D"), ("ground", "TileMapLayer"), ("objects", "Node2D")])
        self.assertEqual([name for name, _ in children[3:]], ["collision", "markers", "portals", "interactions"])

    def test_props_are_sprite2d_with_anchor_offsets(self):
        self.run_export()
        sections = GODOT.parse_godot_text((self.out / "town.tscn").read_text(encoding="utf-8"))
        ext = {s.fields["id"]: s.fields["path"] for s in sections if s.tag == "ext_resource"}
        nodes = {s.fields["name"]: s for s in sections if s.tag == "node" and s.fields.get("parent") == "objects"}
        objects_node = next(s for s in sections if s.tag == "node" and s.fields["name"] == "objects")
        self.assertIs(objects_node.properties["y_sort_enabled"], True)
        bundle = json.loads(self.bundle.read_text(encoding="utf-8"))
        for item in bundle["objects"]:
            node = nodes[item["id"]]
            self.assertEqual(node.fields["type"], "Sprite2D")
            self.assertIs(node.properties["centered"], False)
            position, offset = node.properties["position"].args, node.properties["offset"].args
            scale = node.properties.get("scale", GODOT.GdCall("Vector2", (1, 1))).args[0]
            anchor = (position[0] + scale * (offset[0] + item["anchor_px"][0]),
                      position[1] + scale * (offset[1] + item["anchor_px"][1]))
            self.assertAlmostEqual(anchor[0], item["x"], places=6)
            self.assertAlmostEqual(anchor[1], item["y"], places=6)
            self.assertAlmostEqual(position[1], item.get("sortY", item["y"]), places=6)  # y-sort follows sortY
            texture = self.out / ext[node.properties["texture"].args[0]]
            self.assertEqual(_sha(texture), _sha(self.root / "map" / "props" / item["prop"] / "prop.png"))

    def test_collision_markers_portals_and_interactions(self):
        self.run_export()
        sections = GODOT.parse_godot_text((self.out / "town.tscn").read_text(encoding="utf-8"))
        nodes = {(s.fields.get("parent"), s.fields["name"]): s for s in sections if s.tag == "node"}
        subs = {s.fields["id"]: s for s in sections if s.tag == "sub_resource"}
        collision = [s for (parent, _), s in nodes.items() if parent == "collision"]
        self.assertEqual([s.fields["type"] for s in collision],
                         ["CollisionShape2D", "CollisionPolygon2D", "CollisionShape2D", "CollisionPolygon2D",
                          "CollisionShape2D", "CollisionPolygon2D"])
        footprint = nodes[("collision", "footprint_tree-1")]  # D33: tree-1 is solid with an 8 x 4 ellipse footprint
        self.assertEqual(footprint.properties["metadata/object"], "tree-1")
        points = np.array(footprint.properties["polygon"].args, float).reshape(-1, 2)
        self.assertEqual(len(points), GODOT.ELLIPSE_SEGMENTS)
        np.testing.assert_allclose((points[:, 0] - 40) ** 2 / 16 + (points[:, 1] - 60) ** 2 / 4, 1, atol=1e-5)
        rect = nodes[("collision", "solid_0")]
        self.assertEqual(rect.properties["position"].args, (40, 60))
        self.assertEqual(subs[rect.properties["shape"].args[0]].properties["size"].args, (8, 4))
        self.assertEqual(len(nodes[("collision", "solid_1")].properties["polygon"].args), 2 * GODOT.ELLIPSE_SEGMENTS)
        self.assertEqual(subs[nodes[("collision", "solid_2")].properties["shape"].args[0]].properties["radius"], 5.0)
        spawn = nodes[("markers", "spawn-west")]
        self.assertEqual((spawn.fields["type"], spawn.properties["position"].args), ("Marker2D", (12, 48)))
        self.assertEqual(spawn.properties["metadata/facing"], "east")
        portal = nodes[("portals", "exit-east")]
        self.assertEqual(portal.properties["position"].args, (155, 48))
        self.assertEqual(portal.properties["metadata/entrance_by_from"], {"route-1": "spawn-east"})
        shape = nodes[("portals/exit-east", "shape")]
        self.assertEqual(subs[shape.properties["shape"].args[0]].properties["size"].args, (10, 16))
        circle = nodes[("portals/well-hole", "shape")]
        self.assertEqual(subs[circle.properties["shape"].args[0]].properties["radius"], 6.0)
        self.assertEqual(nodes[("interactions", "well")].fields["type"], "Area2D")
        self.assertEqual(nodes[("interactions", "sign")].fields["type"], "Marker2D")
        walk = nodes[(".", "collision")].properties["metadata/walk_regions"][0]["polygon"]
        self.assertEqual(walk.args, (0, 0, 160, 0, 160, 96, 0, 96))

    def test_blob47_tileset_uses_corners_and_sides(self):
        bundle, _ = build_bundle(self.root / "blob", blob=True)
        self.run_export("--blob-inside", "dirt", out=self.root / "blob-godot", bundle=bundle)
        text = (self.root / "blob-godot" / "town.tileset.tres").read_text(encoding="utf-8")
        self.assertIn("terrain_set_0/mode = 0\n", text)  # TERRAIN_MODE_MATCH_CORNERS_AND_SIDES
        tiles = tres_tiles(text)
        full = tiles[(3, 3)]  # blob_mask 255: every neighbour inside
        self.assertEqual(full["terrain"], 1)
        self.assertEqual(set(full["bits"].values()), {1})
        self.assertEqual(len(full["bits"]), 8)
        lone = tiles[(0, 0)]  # blob_mask 0: every neighbour is the other material
        self.assertEqual(set(lone["bits"].values()), {0})
        east = tiles[(2, 0)]  # blob_mask 4: only E
        self.assertEqual(east["bits"]["right_side"], 1)
        self.assertEqual(east["bits"]["top_side"], 0)

    def test_deterministic_output(self):
        self.run_export()
        self.run_export(out=self.root / "again")
        first = sorted(path.relative_to(self.out) for path in self.out.rglob("*") if path.is_file())
        second = sorted(path.relative_to(self.root / "again") for path in (self.root / "again").rglob("*")
                        if path.is_file())
        self.assertEqual(first, second)
        for relative in first:
            self.assertEqual((self.out / relative).read_bytes(), (self.root / "again" / relative).read_bytes(),
                             relative)

    def test_report_satisfies_the_requested_contract(self):
        self.run_export()
        report = json.loads((self.out / "godot-export.json").read_text(encoding="utf-8"))
        self.assertEqual(requested_contract_errors(report, "engine_export_v1"), [])
        assert_valid_contract(report["qa"], "common", "qaEnvelope", skill=SKILL)
        self.assertNotIn(":/", json.dumps(report))  # no absolute paths
        self.assertIn("material_map: not exported (stays in the map bundle)", report["notExported"])

    def test_input_errors_exit_1_and_write_nothing(self):
        bundle = json.loads(self.bundle.read_text(encoding="utf-8"))
        cases = {
            "sha": lambda b: b["tilesets"][0].update(sha256="0" * 64),
            "tile": lambda b: (b["layers"][1].update(data=[[99] * 10] * 6), b["layers"][1].pop("sha256")),
            "image": lambda b: b.update(prop_packs=[]),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                broken = json.loads(json.dumps(bundle))
                mutate(broken)
                path = self.root / "map" / f"broken-{name}.json"
                path.write_text(json.dumps(broken), encoding="utf-8")
                result = export(path, self.root / f"out-{name}")
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.startswith("error: "), result.stderr)
                self.assertFalse((self.root / f"out-{name}").exists())

    def scene_nodes(self, out=None):
        sections = GODOT.parse_godot_text(((out or self.out) / "town.tscn").read_text(encoding="utf-8"))
        nodes = {(s.fields.get("parent"), s.fields["name"]): s for s in sections if s.tag == "node"}
        subs = {s.fields["id"]: s for s in sections if s.tag == "sub_resource"}
        return nodes, subs

    def test_solid_footprints_become_collision_shapes(self):
        """D33: every solid footprint is a CollisionShape2D or CollisionPolygon2D under the collision body, scaled once
        by the instance scale (basis world_px is not), with offset, rotation and flip_x applied, exactly the shape
        forge_nav blocks; the parse-back QA covers them."""
        root = self.root / "feet"
        extra = [
            {"id": "pot", "prop": "rock", "x": 30, "y": 70, "anchor_px": [7, 10],
             "footprint": {"shape": "ellipse", "width": 6, "depth": 6}},
            {"id": "crate", "prop": "rock", "x": 60, "y": 80, "anchor_px": [7, 10], "scale": 2,
             "footprint": {"shape": "rect", "width": 6, "depth": 4, "offset": [1, -2]}},
            {"id": "log", "prop": "rock", "x": 90, "y": 30, "anchor_px": [7, 10],
             "footprint": {"shape": "rect", "width": 10, "depth": 3, "rotate": 30}},
            {"id": "slab", "prop": "rock", "x": 110, "y": 30, "anchor_px": [7, 10], "scale": 3,
             "footprint": {"shape": "rect", "width": 8, "depth": 2, "basis": "world_px"}},
            {"id": "mirror", "prop": "rock", "x": 140, "y": 30, "anchor_px": [7, 10], "flip_x": True,
             "footprint": {"shape": "rect", "width": 4, "depth": 2, "offset": [5, 0]}},
            {"id": "ghost", "prop": "rock", "x": 60, "y": 20, "anchor_px": [7, 10], "solid": False,
             "footprint": {"shape": "ellipse", "width": 6, "depth": 6}},
        ]
        bundle, _ = build_bundle(root, extra_objects=extra)
        self.run_export(bundle=bundle, out=self.root / "feet-godot")
        nodes, subs = self.scene_nodes(self.root / "feet-godot")
        footprints = {name.removeprefix("footprint_"): node for (parent, name), node in nodes.items()
                      if parent == "collision" and name.startswith("footprint_")}
        expected = {solid["source"].split(":", 1)[1]: solid for solid in NAV.read_blocking_set(bundle).footprints}
        self.assertEqual(sorted(footprints), sorted(expected))
        self.assertNotIn("ghost", footprints, "solid: false blocks nothing")
        circle = footprints["pot"]
        self.assertEqual(circle.properties["position"].args, (30, 70))
        self.assertEqual(subs[circle.properties["shape"].args[0]].properties["radius"], 3.0)
        crate = footprints["crate"]  # scaled once: 12 x 8 at (60 + 2, 80 - 4)
        self.assertEqual(crate.properties["position"].args, (62, 76))
        self.assertEqual(subs[crate.properties["shape"].args[0]].properties["size"].args, (12, 8))
        slab = footprints["slab"]  # world_px is not scaled
        self.assertEqual(subs[slab.properties["shape"].args[0]].properties["size"].args, (8, 2))
        mirror = footprints["mirror"]  # flip_x mirrors the offset around the anchor x
        self.assertEqual(mirror.properties["position"].args, (135, 30))
        log = np.array(footprints["log"].properties["polygon"].args, float).reshape(-1, 2)
        np.testing.assert_allclose(log, expected["log"]["points"], atol=1e-5)
        report = json.loads((self.root / "feet-godot" / "godot-export.json").read_text(encoding="utf-8"))
        checks = {check["id"]: check for check in report["qa"]["checks"]}
        self.assertEqual(checks["collision_shapes_roundtrip"]["status"], "pass")
        self.assertEqual(checks["collision_shapes_roundtrip"]["threshold"]["shapes"], 5 + len(expected), "4 solids, 1 rect")
        self.assertEqual(report["counts"]["footprints"], len(expected))

    def test_collision_shapes_are_the_blocking_sets_without_zero_area_shapes(self):
        """D2, N4 (review r2, finding 8): an ellipse with rx 0 and a rect with w 0 block nothing; forge_nav, the
        runtime and export_tiled drop them, and so does export_godot now (it wrote the ellipse as a 32-point
        CollisionPolygon2D of zero area). The collision body holds exactly forge_nav's collision.solids and rects."""
        data = json.loads(self.bundle.read_text(encoding="utf-8"))
        data["collision"]["solids"].append({"shape": "ellipse", "cx": 48, "cy": 16, "rx": 0, "ry": 5, "id": "sliver"})
        data["collision"]["rects"].append([10, 10, 0, 4])
        self.bundle.write_text(json.dumps(data), encoding="utf-8")
        result = export(self.bundle, self.out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("warning: collision sliver has no area and was skipped", result.stderr)
        self.assertIn("warning: collision rect_1 has no area and was skipped", result.stderr)
        nodes, subs = self.scene_nodes(self.out)
        shapes = {name: node for (parent, name), node in nodes.items()
                  if parent == "collision" and not name.startswith("footprint_")}
        blocking = NAV.read_blocking_set(self.bundle)
        self.assertEqual(sorted(shapes), ["rect_0", "solid_0", "solid_1", "solid_2", "solid_3"])
        self.assertEqual(len(shapes), len(blocking.collision_solids) + len(blocking.rects))
        for node in shapes.values():
            if node.fields["type"] == "CollisionPolygon2D":
                points = np.array(node.properties["polygon"].args, float).reshape(-1, 2)
                area = np.dot(points[:, 0], np.roll(points[:, 1], -1)) - np.dot(points[:, 1], np.roll(points[:, 0], -1))
                self.assertGreater(abs(area), 0, node.fields["name"])

    def test_collision_qa_catches_a_shape_that_does_not_round_trip(self):
        self.run_export()
        report = json.loads((self.out / "godot-export.json").read_text(encoding="utf-8"))
        assets = {item["sha256"]: item["path"] for item in report["assets"]}
        bundle = GODOT._local_read_bundle(self.bundle)
        terrain = bundle.tilesets["terrain"]
        plans = {"terrain": GODOT.TilesetPlan(0, terrain, assets[terrain.image_sha256], 0, "match_corners", None)}
        text, layout = GODOT.build_scene(bundle, "town", plans, "town.tileset.tres", assets, "inherit")
        self.assertEqual(text, (self.out / "town.tscn").read_text(encoding="utf-8"), "the same scene as the export")
        self.assertEqual({c["id"]: c["status"] for c in GODOT.qa_scene(text, layout, self.out)}
                         ["collision_shapes_roundtrip"], "pass")
        broken = text.replace("size = Vector2(8, 4)", "size = Vector2(8, 5)", 1)
        self.assertNotEqual(broken, text)
        checks = {check["id"]: check for check in GODOT.qa_scene(broken, layout, self.out)}
        self.assertEqual(checks["collision_shapes_roundtrip"]["status"], "fail")
        self.assertIn("rectangle size differs", " ".join(checks["collision_shapes_roundtrip"]["value"]))

    def test_props_registry_art_and_flip_x(self):
        """D6: a layout_build-style bundle (props{label} with an inline image, objects with flip_x and no prop_packs)
        exports; a flipped prop is a Sprite2D with flip_h whose mirrored anchor still lands on (x, y)."""
        data = json.loads(self.bundle.read_text(encoding="utf-8"))
        data.pop("prop_packs")
        data["props"] = {"tree": {"label": "tree", "image": "props/tree/prop.png", "anchor_px": [12, 39],
                                  "sha256": _sha(self.root / "map" / "props" / "tree" / "prop.png"),
                                  "footprint": {"shape": "ellipse", "width": 6, "depth": 3, "offset": [2, 0]}},
                         "rock": {"pack": "props/prop-pack.json", "label": "rock"}}
        data["objects"][1]["flip_x"] = True
        path = self.root / "map" / "registry.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert_valid_contract(data, "map", "map_bundle_v2", skill=SKILL)
        summary = self.run_export(bundle=path, out=self.root / "registry-godot")
        self.assertEqual(summary["status"], "pass")
        nodes, _ = self.scene_nodes(self.root / "registry-godot")
        tree = nodes[("objects", "tree-2")]
        self.assertIs(tree.properties["flip_h"], True)
        self.assertNotIn("flip_h", nodes[("objects", "tree-1")].properties)
        position, offset = tree.properties["position"].args, tree.properties["offset"].args
        self.assertEqual(offset[0], 12 - 24, "flip_h mirrors the texture in place: offset.x = anchor_x - width")
        scale = tree.properties["scale"].args[0]
        self.assertAlmostEqual(position[0] + scale * (offset[0] + 24 - 12), 100, places=6, msg="the anchor lands on x")
        report = json.loads((self.root / "registry-godot" / "godot-export.json").read_text(encoding="utf-8"))
        self.assertEqual({check["id"]: check["status"] for check in report["qa"]["checks"]}["prop_anchors_roundtrip"],
                         "pass")
        footprints = {name for (parent, name) in nodes if parent == "collision" and name.startswith("footprint_")}
        self.assertEqual(footprints, {"footprint_tree-1", "footprint_tree-2"}, "tree-2 takes the registry footprint")

    def test_sort_y_must_be_a_number(self):
        """The Wave B review's traceback: a schema-invalid sortY: null is a clean error (exit 1, nothing written)."""
        data = json.loads(self.bundle.read_text(encoding="utf-8"))
        data["objects"][0]["sortY"] = None
        path = self.root / "map" / "null-sort.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = export(path, self.root / "null-sort")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "error: object tree-1 sortY must be a finite number.")
        self.assertFalse((self.root / "null-sort").exists())

    def test_walkable_false_tiles_without_shapes_block_their_cell(self):
        """N7: a tile with no collision shapes whose properties.walkable is false gets a whole-cell physics polygon."""
        manifest_path = self.root / "map" / "tiles" / "terrain.tileset.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["tiles"][15]["properties"]["walkable"] = False
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        data = json.loads(self.bundle.read_text(encoding="utf-8"))
        data["tilesets"][0]["sha256"] = _sha(manifest_path)
        self.bundle.write_text(json.dumps(data), encoding="utf-8")
        self.run_export()
        tiles = tres_tiles((self.out / "town.tileset.tres").read_text(encoding="utf-8"))
        self.assertEqual(tiles[(3, 3)]["polygons"], {0: [-8, -8, 8, -8, 8, 8, -8, 8]})

    def test_blocking_material_classes_are_listed_as_not_exported(self):
        """D2: a class the exporter cannot represent is listed in notExported, never dropped silently."""
        self.run_export()
        report = json.loads((self.out / "godot-export.json").read_text(encoding="utf-8"))
        notes = [note for note in report["notExported"] if note.startswith("material_map blocking classes")]
        self.assertEqual(len(notes), 1)
        self.assertIn("liquid", notes[0])

    def test_bundles_forge_nav_refuses_are_refused(self):
        data = json.loads(self.bundle.read_text(encoding="utf-8"))
        data["material_map"]["materials"]["water"].pop("color")
        path = self.root / "map" / "no-colour.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = export(path, self.root / "no-colour")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: collision (forge_nav): material_map.materials: every material needs a color", result.stderr)
        self.assertFalse((self.root / "no-colour").exists())

    def test_usage_errors_exit_2(self):
        result = export(self.bundle, self.out, "--texture-filter", "blurry")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertFalse(self.out.exists())

    def test_a_tile_csv_with_a_utf8_bom_is_read(self):
        """D28 (review r1, finding 2): a tiles layer CSV saved with a UTF-8 BOM (Excel "CSV UTF-8", PowerShell 5.1
        Out-File) exports exactly like the plain file, as map_bundle, map_nav, export_tiled and the preview read it."""
        plain = self.run_export()
        csv = self.root / "map" / "layers" / "ground.csv"
        csv.write_bytes(b"\xef\xbb\xbf" + csv.read_bytes())
        data = json.loads(self.bundle.read_text(encoding="utf-8"))
        data["layers"][1]["sha256"] = _sha(csv)
        self.bundle.write_text(json.dumps(data), encoding="utf-8")
        bom = self.run_export(out=self.root / "godot-bom")
        self.assertEqual(bom["status"], plain["status"])
        self.assertEqual(tile_layer_cells((self.root / "godot-bom" / "town.tscn").read_text(encoding="utf-8"), "ground"),
                         tile_layer_cells((self.out / "town.tscn").read_text(encoding="utf-8"), "ground"))

    def test_help_works_under_cp1252_and_cp950(self):
        assert_cli_help(SKILL, "export_godot")  # cp1252 and cp950

    def test_refuses_an_existing_output_dir(self):
        self.out.mkdir()
        result = export(self.bundle, self.out)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_strict_qc_failure_publishes_nothing(self):
        bundle, _ = build_bundle(self.root / "outside", extra_objects=[
            {"id": "lost", "prop": "rock", "x": 400, "y": 20, "anchor_px": [7, 10]}])
        lenient = export(bundle, self.root / "lenient")
        self.assertEqual(lenient.returncode, 0, lenient.stderr)
        self.assertIn("warning: object lost", lenient.stderr)
        strict = export(bundle, self.root / "strict", "--strict-qc")
        self.assertEqual(strict.returncode, 1)
        self.assertIn("objects_in_world", strict.stderr)
        self.assertFalse((self.root / "strict").exists())
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.startswith(".strict")], [])


if __name__ == "__main__":
    unittest.main()
