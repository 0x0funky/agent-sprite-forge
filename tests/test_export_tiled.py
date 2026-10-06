"""export_tiled.py (plan B13-T3): Tiled 1.10 TMJ with TSX tilesets (corner wangset, blob-47 as a
two-colour mixed wangset, per-tile collision), a prop image collection with anchors and sortY,
layers ground/decoration/props/collision/interactions, the --embedded-variant for Phaser, and
the round trip: re-rendering the written files with the built-in reader or pytiled-parser
differs from the bundle's reference render by 0 px."""
from __future__ import annotations

import hashlib
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import assert_cli_help, assert_valid_contract, load_script, run_cli, script_path
from test_map_bundle import (BLOB_MASKS, SKILL, assert_patched_valid, d6_bundle, demo, edit, prop_image, save,
                             wang_atlas)

et = load_script(SKILL, "export_tiled")
mb = et.map_bundle
TOOL = script_path(SKILL, "export_tiled")


def export(tmp_path: Path, *extra, bundle: Path | None = None, name: str = "tiled") -> Path:
    path = bundle or demo(tmp_path)
    out = tmp_path / name
    run = run_cli([TOOL, "export", "--bundle", path, "--output-dir", out, *extra])
    assert run.returncode == 0, run.stderr
    return out


def reference(tmp_path: Path) -> np.ndarray:
    return mb.render_map(mb.load_bundle(tmp_path / "map" / "map-bundle.json"))


def pytiled_render(tmj: Path, size: tuple[int, int]) -> np.ndarray:
    """An independent re-render: pytiled-parser's model of the files, composited here."""
    tp = pytest.importorskip("pytiled_parser")
    tiled = tp.parse_map(tmj)
    tw, th = tiled.tile_size.width, tiled.tile_size.height
    canvas = Image.new("RGBA", (tiled.map_size.width * tw, tiled.map_size.height * th))

    def tile_image(gid: int) -> Image.Image:
        first = max(g for g in tiled.tilesets if g <= gid)
        tileset, local = tiled.tilesets[first], gid - first
        if tileset.image is not None:
            atlas = Image.open(tmj.parent / tileset.image).convert("RGBA")
            col, row = local % tileset.columns, local // tileset.columns
            return atlas.crop((col * tw, row * th, (col + 1) * tw, (row + 1) * th))
        return Image.open(tmj.parent / tileset.tiles[local].image).convert("RGBA")

    def paste(image: Image.Image, left: int, top: int) -> None:
        crop = image.crop((max(0, -left), max(0, -top), image.width, image.height))
        canvas.alpha_composite(crop, (max(0, left), max(0, top)))

    for layer in tiled.layers:
        if not layer.visible:
            continue
        if isinstance(layer, tp.TileLayer):
            for y, row in enumerate(layer.data):
                for x, gid in enumerate(row):
                    if gid:
                        paste(tile_image(gid), x * tw, y * th)
        elif isinstance(layer, tp.ImageLayer):
            paste(Image.open(tmj.parent / layer.image).convert("RGBA"), int(layer.offset.x), int(layer.offset.y))
        elif isinstance(layer, tp.ObjectLayer):
            assert layer.draw_order == "index"
            for obj in layer.tiled_objects:
                if not hasattr(obj, "gid"):
                    continue
                image = tile_image(obj.gid & 0x0FFFFFFF)
                if obj.gid & 0x80000000:  # Tiled's horizontal flip flag (a flip_x object, D6)
                    image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                width, height = math.floor(obj.size.width + 0.5), math.floor(obj.size.height + 0.5)
                if (width, height) != image.size:
                    image = image.resize((width, height), Image.Resampling.NEAREST)
                paste(image, math.floor(obj.coordinates.x + 0.5), math.floor(obj.coordinates.y - obj.size.height + 0.5))
    return np.asarray(canvas)[: size[1], : size[0]]


def tsx(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def properties(element: ET.Element) -> dict:
    return {p.get("name"): (p.get("type", "string"), p.get("value")) for p in element.findall("properties/property")}


# --------------------------------------------------------------------------- B13-T3 acceptance

def test_rerender_builtin_zero_px(tmp_path):
    out = export(tmp_path, "--embedded-variant")
    expected = reference(tmp_path)
    preview = np.asarray(Image.open(out / "preview.png").convert("RGBA"))
    assert np.array_equal(preview, expected)
    for name in ("map.tmj", "map.embedded.tmj"):
        rendered = et.render_tiled(et.read_tiled_map(out / name), (192, 128))
        assert et.differing_pixels(rendered, expected) == 0, name
    report = json.loads((out / "export-report.json").read_text(encoding="utf-8"))
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
    assert_patched_valid(report, "tiled_export_v1")
    assert report["status"] == "pass" and [c["id"] for c in report["checks"]] == [
        "tsx_well_formed", "rerender_map_tmj", "rerender_map_embedded_tmj"]
    assert all(c["status"] == "pass" and c["value"] in (0, []) for c in report["checks"])
    assert any("Tiled GUI not verified" in item for item in report["notProven"])
    files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert files == {"map.tmj", "map.embedded.tmj", "terrain.tsx", "path.tsx", "props.tsx", "preview.png",
                     "export-report.json", "images/terrain.png", "images/path.png", "images/backdrop.png",
                     "images/tree.png", "images/rock.png", "images/bush.png"}
    refs = {ref["path"]: ref["sha256"] for ref in report["outputs"]}
    published = files - {"export-report.json"}
    assert refs == {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in published}
    copied = (out / "images" / "terrain.png").read_bytes()
    assert copied == (tmp_path / "map" / "tiles" / "terrain.png").read_bytes()  # images are copied byte for byte


def test_rerender_pytiled_zero_px(tmp_path):
    pytest.importorskip("pytiled_parser")
    out = export(tmp_path, "--embedded-variant")
    expected = reference(tmp_path)
    for name in ("map.tmj", "map.embedded.tmj"):
        assert et.differing_pixels(pytiled_render(out / name, (192, 128)), expected) == 0, name
        run = run_cli([TOOL, "verify", "--map", out / name, "--bundle", tmp_path / "map" / "map-bundle.json",
                       "--reader", "pytiled"])
        assert run.returncode == 0 and json.loads(run.stdout)["differingPixels"] == 0, run.stderr


# --------------------------------------------------------------------------- tilesets

def test_terrain_tsx_corner_wangset_and_collision(tmp_path):
    root = tsx(export(tmp_path) / "terrain.tsx")
    assert (root.get("tilecount"), root.get("columns"), root.get("tilewidth")) == ("16", "4", "16")
    assert root.find("image").get("source") == "images/terrain.png"
    (wangset,) = root.findall("wangsets/wangset")
    assert wangset.get("type") == "corner"
    assert [c.get("name") for c in wangset.findall("wangcolor")] == ["water", "grass"]
    wangids = {int(t.get("tileid")): [int(v) for v in t.get("wangid").split(",")] for t in wangset.findall("wangtile")}
    assert len(wangids) == 16
    for index, wangid in wangids.items():
        tl, tr, bl, br = (index >> 3) & 1, (index >> 2) & 1, (index >> 1) & 1, index & 1
        # Tiled order: top, top-right, right, bottom-right, bottom, bottom-left, left, top-left; colours 1-based
        assert wangid == [0, tr + 1, 0, br + 1, 0, bl + 1, 0, tl + 1]
    tiles = {int(t.get("id")): t for t in root.findall("tile")}
    water = tiles[0].findall("objectgroup/object")
    assert sorted((o.get("x"), o.get("y"), o.get("width"), o.get("height")) for o in water) == [
        ("0", "0", "8", "8"), ("0", "8", "8", "8"), ("8", "0", "8", "8"), ("8", "8", "8", "8")]
    assert properties(tiles[0])["walkable"] == ("bool", "false")
    assert properties(tiles[15])["walkable"] == ("bool", "true")
    assert tiles[15].find("objectgroup") is None
    assert properties(tiles[6])["wang"] == ("string", "0,1,1,0")


def test_tsx_tile_collision_keeps_only_what_forge_nav_blocks(tmp_path):
    """D2, N4 (review r2, finding 7): the TSX gets exactly the tile shapes forge_nav keeps; a zero-size rect or
    ellipse blocks nothing there, so it is not written as a solid either. A tile whose only shapes have no area
    keeps no collision even with walkable false (forge_nav adds the whole cell only to a tile without shapes),
    and a bundle with a zero-area tile polygon (the bow tie) no longer validates, so it is not exported."""
    path = demo(tmp_path, hashes=False)
    terrain = path.parent / "tiles" / "terrain.tileset.json"
    edit(terrain, lambda doc: doc["tiles"][1]["collision"].extend([{"shape": "rect", "x": 9, "y": 9, "w": 0, "h": 3},
                                                                   {"shape": "ellipse", "cx": 4, "cy": 4, "rx": 2,
                                                                    "ry": 0}]))
    edit(terrain, lambda doc: doc["tiles"][15].update(collision=[{"shape": "rect", "x": 1, "y": 1, "w": 5, "h": 0}],
                                                      properties={"walkable": False}))
    tiles = {int(t.get("id")): t for t in tsx(export(tmp_path, bundle=path) / "terrain.tsx").findall("tile")}
    kept = [(o.get("x"), o.get("y"), o.get("width"), o.get("height")) for o in tiles[1].findall("objectgroup/object")]
    assert len(kept) == 3 and ("9", "9", "0", "3") not in kept and all(o.get("ellipse") is None for o in
                                                                       tiles[1].findall("objectgroup/object"))
    assert tiles[15].find("objectgroup") is None
    blocking = mb.blocking_set(mb.load_bundle(path))  # forge_nav's tile collision: no ellipse, no zero-size rect
    assert not [s for s in blocking.tiles
                if s["shape"] == "ellipse" or (s["shape"] == "rect" and (s["w"] <= 0 or s["h"] <= 0))]
    edit(terrain, lambda doc: doc["tiles"][2].update(collision=[{"shape": "polygon",
                                                                 "points": [[2, 2], [14, 14], [14, 2], [2, 14]]}]))
    refused = run_cli([TOOL, "export", "--bundle", path, "--output-dir", tmp_path / "bow-tie"])
    assert refused.returncode == 1 and "does not validate" in refused.stderr and "polygon has zero area" in refused.stderr
    assert not (tmp_path / "bow-tie").exists()


def test_blob_tsx_is_a_two_colour_mixed_wangset(tmp_path):
    root = tsx(export(tmp_path) / "path.tsx")
    assert (root.get("tilecount"), root.get("columns")) == ("48", "8")
    (wangset,) = root.findall("wangsets/wangset")
    assert wangset.get("type") == "mixed" and [c.get("name") for c in wangset.findall("wangcolor")] == ["grass", "path"]
    wangids = {int(t.get("tileid")): [int(v) for v in t.get("wangid").split(",")] for t in wangset.findall("wangtile")}
    assert len(wangids) == 47
    for index, mask in enumerate(BLOB_MASKS):
        assert wangids[index] == [2 if mask >> bit & 1 else 1 for bit in range(8)]
    assert wangids[BLOB_MASKS.index(0)] == [1] * 8  # an isolated tile is all "outside", never all-zero
    assert wangids[BLOB_MASKS.index(255)] == [2] * 8
    assert et.corner_wangid([0, 1, 0, 1]) == [0, 2, 0, 2, 0, 1, 0, 1]
    assert et.blob_wangid(0b10000011) == [2, 2, 1, 1, 1, 1, 1, 2]


def test_props_collection_and_prop_objects(tmp_path):
    out = export(tmp_path)
    root = tsx(out / "props.tsx")
    assert (root.get("columns"), root.get("objectalignment"), root.get("tilecount")) == ("0", "bottomleft", "3")
    tiles = {properties(t)["prop"][1]: t for t in root.findall("tile")}
    assert set(tiles) == {"bush", "rock", "tree"}
    tree = tiles["tree"]
    assert tree.get("type") == "prop" and tree.find("image").get("source") == "images/tree.png"
    assert (properties(tree)["anchorX"], properties(tree)["anchorY"]) == (("float", "12"), ("float", "30"))
    footprint = tree.find("objectgroup/object")
    assert footprint.find("ellipse") is not None
    assert [float(footprint.get(k)) for k in ("x", "y", "width", "height")] == [8.0, 28.0, 8.0, 4.0]
    assert tiles["rock"].find("objectgroup/object/polygon") is not None  # the rotated footprint
    assert tiles["bush"].find("objectgroup") is None

    tmj = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    layer = next(l for l in tmj["layers"] if l["name"] == "props")
    assert layer["type"] == "objectgroup" and layer["draworder"] == "index"
    objects = layer["objects"]
    assert [o["name"] for o in objects] == ["tree-2", "rock-1", "bush-1", "bush-2", "tree-1"]  # sortY, x, id
    firstgid = next(ts["firstgid"] for ts in tmj["tilesets"] if ts["source"] == "props.tsx")
    tree2 = objects[0]
    assert tree2["gid"] - firstgid == [properties(t)["prop"][1] for t in root.findall("tile")].index("tree")
    # anchor (12, 30) of a 24 x 32 image at scale 2 lands on (156, 40): bottom-left (132, 44), 48 x 64
    assert (tree2["x"], tree2["y"], tree2["width"], tree2["height"]) == (132.0, 44.0, 48.0, 64.0)
    props = {p["name"]: p["value"] for p in tree2["properties"]}
    assert props["sortY"] == 40.0 and (props["anchorWorldX"], props["anchorWorldY"]) == (156.0, 40.0)
    assert props["scale"] == 2.0 and props["solid"] is True and props["occlusion"] == "tall"


def test_layers_collision_and_interactions(tmp_path):
    tmj = json.loads((export(tmp_path) / "map.tmj").read_text(encoding="utf-8"))
    assert [(l["name"], l["type"]) for l in tmj["layers"]] == [
        ("backdrop", "imagelayer"), ("ground", "tilelayer"), ("decoration", "tilelayer"), ("props", "objectgroup"),
        ("collision", "objectgroup"), ("interactions", "objectgroup")]
    assert (tmj["width"], tmj["height"], tmj["tilewidth"], tmj["orientation"]) == (12, 8, 16, "orthogonal")
    ground = tmj["layers"][1]["data"]
    assert len(ground) == 96 and min(ground) >= 1 and max(ground) <= 16  # terrain gids 1..16
    decoration = tmj["layers"][2]["data"]
    assert decoration.count(0) == 96 - 12 and all(17 <= g < 17 + 48 for g in decoration if g)
    ids = [o["id"] for l in tmj["layers"] if l["type"] == "objectgroup" for o in l["objects"]]
    assert len(ids) == len(set(ids)) and tmj["nextobjectid"] == max(ids) + 1 and tmj["nextlayerid"] == 7

    collision = tmj["layers"][4]
    assert collision["visible"] is False
    props = {p["name"]: p["value"] for p in collision["properties"]}
    assert (props["actorRadius"], props["ySquash"], props["navCell"]) == (4.0, 1.0, 2)
    kinds = {(o["type"], o["name"]): o for o in collision["objects"]}
    assert ("solid", "collision.solids[0]") in kinds and kinds[("solid", "well-base")]["ellipse"] is True
    rect = kinds[("solid", "collision.rects[0]")]
    assert (rect["width"], rect["height"]) == (20, 8)
    tree2 = kinds[("solid", "tree-2")]
    assert tree2["ellipse"] and (tree2["x"], tree2["y"], tree2["width"], tree2["height"]) == (148.0, 36.0, 16.0, 8.0)
    assert "polygon" in kinds[("solid", "rock-1")]
    lava = kinds[("hazard", "lava")]
    assert (lava["x"], lava["y"], lava["width"], lava["height"]) == (160, 112, 32, 16)
    assert {p["name"]: p["value"] for p in kinds[("liquid", "pool")]["properties"]}["walkable"] is True
    assert not any(kind == "decor" for kind, _ in kinds)

    interactions = {(o["type"], o["name"]): o for o in tmj["layers"][5]["objects"]}
    start = interactions[("spawn", "start")]
    assert start["point"] and (start["x"], start["y"]) == (24, 70) and start["properties"][0]["value"] == "east"
    exit_east = interactions[("portal", "exit-east")]
    values = {p["name"]: p["value"] for p in exit_east["properties"]}
    assert (exit_east["x"], exit_east["y"], exit_east["width"], exit_east["height"]) == (184, 48, 8, 32)
    assert values["to"] == "road:arrive-west" and values["activation"] == "intent" and values["radius"] == 6.0
    assert (values["travelDirectionX"], values["travelDirectionY"], values["latch"]) == (1.0, 0.0, True)
    assert json.loads(values["entranceByFrom"]) == {"road": "arrive-east"}
    cellar = interactions[("portal", "cellar")]
    assert cellar["ellipse"] and (cellar["x"], cellar["y"], cellar["width"]) == (67.0, 59.0, 10.0)
    well = interactions[("anchor", "well")]
    anchor = {p["name"]: p["value"] for p in well["properties"]}
    assert json.loads(anchor["slots"]) == [[86.0, 44.0], [106.0, 44.0]]
    assert json.loads(anchor["approach"]) == [[96.0, 44.0]]
    assert {p["name"]: p["value"] for p in interactions[("interaction", "sign")]["properties"]} == {"reach": 10.0}


def test_embedded_variant_inlines_tilesets(tmp_path):
    out = export(tmp_path, "--embedded-variant")
    external = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    embedded = json.loads((out / "map.embedded.tmj").read_text(encoding="utf-8"))
    assert [ts["source"] for ts in external["tilesets"]] == ["terrain.tsx", "path.tsx", "props.tsx"]
    assert all("source" not in ts for ts in embedded["tilesets"])
    firstgids = [ts["firstgid"] for ts in embedded["tilesets"]]
    assert firstgids == [ts["firstgid"] for ts in external["tilesets"]] == [1, 17, 65]
    terrain = embedded["tilesets"][0]
    assert terrain["image"] == "images/terrain.png" and terrain["wangsets"][0]["type"] == "corner"
    assert embedded["layers"] == external["layers"]
    for name, entry in zip(["terrain.tsx", "path.tsx", "props.tsx"], embedded["tilesets"]):
        root = tsx(out / name)
        assert root.get("name") == entry["name"] and int(root.get("tilecount")) == entry["tilecount"]


def test_export_is_deterministic(tmp_path):
    first = export(tmp_path, "--embedded-variant", name="a")
    second = export(tmp_path, "--embedded-variant", bundle=tmp_path / "map" / "map-bundle.json", name="b")
    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
    for rel in files:
        assert (first / rel).read_bytes() == (second / rel).read_bytes(), rel


def test_rotated_ellipse_keeps_its_centre():
    solid = {"shape": "ellipse", "cx": 50.0, "cy": 40.0, "rx": 12.0, "ry": 5.0, "rotate": 30.0}
    obj = et.tiled_shape(solid, 1, "solid")
    theta = math.radians(obj["rotation"])
    centre_local = np.array([obj["width"] / 2, obj["height"] / 2])
    rotation = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    assert np.allclose(np.array([obj["x"], obj["y"]]) + rotation @ centre_local, [50, 40])
    assert obj["ellipse"] and (obj["width"], obj["height"]) == (24.0, 10.0)


def test_image_plate_and_v1_bundles_export(tmp_path):
    plate = tmp_path / "plate"
    save(np.dstack([np.full((50, 70, 3), 90, np.uint8), np.full((50, 70), 255, np.uint8)]), plate / "plate.png")
    save(prop_image(10, 14, (200, 100, 50), 9), plate / "lamp.png")
    doc = {"schema": "generate2dmap.map_bundle.v2", "world": {"width": 70, "height": 50, "unit": "px"},
           "layers": [{"name": "plate", "kind": "image", "image": "plate.png"}, {"name": "props", "kind": "objects"}],
           "props": {"lamp": {"image": "lamp.png", "anchor_px": [5, 13]}},
           "objects": [{"id": "lamp-1", "prop": "lamp", "x": 33.5, "y": 30.25, "scale": 1.5, "anchor_px": [5, 13]}],
           "collision": {"actorRadius": 3, "ySquash": 0.58}}
    (plate / "plate-bundle.json").write_text(json.dumps(doc), encoding="utf-8")
    out = export(tmp_path, bundle=plate / "plate-bundle.json", name="plate-tiled")
    tmj = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    assert (tmj["width"], tmj["height"], tmj["tilewidth"]) == (5, 4, 16)  # 16 px default grid covering 70 x 50
    assert {p["name"]: p["value"] for p in tmj["properties"]}["mapId"] == "plate"
    expected = mb.render_map(mb.load_bundle(plate / "plate-bundle.json"))
    assert et.differing_pixels(et.render_tiled(et.read_tiled_map(out / "map.tmj"), (70, 50)), expected) == 0

    legacy = tmp_path / "legacy"
    save(wang_atlas(), legacy / "terrain.png")
    tiles = [{"index": i, "wang": [(i >> 3) & 1, (i >> 2) & 1, (i >> 1) & 1, i & 1]} for i in range(16)]
    (legacy / "map-bundle.json").write_text(json.dumps({
        "schema": "generate2dmap.map_bundle.v1", "tile_size": 16,
        "tilesets": [{"id": "terrain", "image": "terrain.png", "kind": "wang_corner", "materials": ["water", "grass"],
                      "tiles": tiles}],
        "layers": [{"name": "ground", "kind": "tiles", "data": [[15, 0], [6, 9]]}]}), encoding="utf-8")
    out = export(tmp_path, bundle=legacy / "map-bundle.json", name="legacy-tiled")
    assert tsx(out / "terrain.tsx").find("wangsets/wangset").get("type") == "corner"


def test_regions_cell_blockers_and_name_clashes(tmp_path):
    """Walk regions and holes reach the collision layer; a walkable-false tile without shapes gets a
    full-cell rect; a rotated ellipse keeps its rotation in TSX; two props both named prop.png get
    distinct copies; a v1 tiles layer without a tileset exports as empty cells."""
    root = tmp_path / "custom"
    save(wang_atlas(), root / "atlas.png")
    tiles = [{"index": 0, "properties": {"walkable": False}},
             {"index": 1, "collision": [{"shape": "ellipse", "cx": 8, "cy": 8, "rx": 4, "ry": 2, "rotate": 30}]}]
    tileset = {"schema": "generate2dmap.tileset.v1", "image": "atlas.png", "tile_size": 16, "columns": 4,
               "kind": "flat", "materials": ["stone"], "seamless_verified": False, "tiles": tiles}
    (root / "set.json").write_text(json.dumps(tileset), encoding="utf-8")
    for folder, colour in (("oak", (40, 120, 50)), ("pine", (30, 90, 60))):
        save(prop_image(12, 16, colour, 4), root / folder / "prop.png")
    region = {"polygon": [[0, 0], [64, 0], [64, 48], [0, 48]], "holes": [[[20, 20], [30, 20], [25, 30]]]}
    doc = {"schema": "generate2dmap.map_bundle.v2", "tile_size": 16, "world": {"width": 64, "height": 48, "unit": "px"},
           "tilesets": [{"id": "stone", "manifest": "set.json"}],
           "layers": [{"name": "ground", "kind": "tiles", "data": [[1, 0, 1, 2], [2, 2, 2, 2], [1, 1, 1, 1]]},
                      {"name": "props", "kind": "objects"}],
           "props": {"oak": {"image": "oak/prop.png", "anchor_px": [6, 15]},
                     "pine": {"image": "pine/prop.png", "anchor_px": [6, 15]}},
           "objects": [{"id": "oak-1", "prop": "oak", "x": 10, "y": 40, "anchor_px": [6, 15]},
                       {"id": "pine-1", "prop": "pine", "x": 50, "y": 40, "anchor_px": [6, 15]}],
           "collision": {"actorRadius": 3, "walkRegions": [region]}}
    (root / "map-bundle.json").write_text(json.dumps(doc), encoding="utf-8")
    out = export(tmp_path, bundle=root / "map-bundle.json", name="custom-tiled")
    assert sorted(p.name for p in (out / "images").iterdir()) == ["atlas.png", "prop-2.png", "prop.png"]
    root_tsx = tsx(out / "stone.tsx")
    cells = {int(t.get("id")): t for t in root_tsx.findall("tile")}
    full = cells[0].find("objectgroup/object")
    assert [full.get(k) for k in ("x", "y", "width", "height")] == ["0", "0", "16", "16"]
    rotated = cells[1].find("objectgroup/object")
    assert rotated.get("rotation") == "30" and rotated.find("ellipse") is not None
    tmj = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    collision = next(layer for layer in tmj["layers"] if layer["name"] == "collision")
    kinds = [(o["type"], o["name"]) for o in collision["objects"]]
    assert ("walkRegion", "region-0") in kinds and ("walkHole", "region-0-hole-0") in kinds
    hole = next(o for o in collision["objects"] if o["type"] == "walkHole")
    assert [(p["x"] + hole["x"], p["y"] + hole["y"]) for p in hole["polygon"]] == [(20, 20), (30, 20), (25, 30)]

    legacy = tmp_path / "legacy"
    (legacy / "layers").mkdir(parents=True)
    (legacy / "layers" / "g.csv").write_text("0,1\n2,3\n", encoding="utf-8")
    (legacy / "map-bundle.json").write_text(json.dumps({
        "schema": "generate2dmap.map_bundle.v1", "tile_size": 8,
        "layers": [{"name": "g", "kind": "tiles", "data": "layers/g.csv"}]}), encoding="utf-8")
    out = export(tmp_path, bundle=legacy / "map-bundle.json", name="legacy-plain")
    tmj = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    assert tmj["layers"][0]["data"] == [0, 0, 0, 0] and tmj["tilesets"] == []


def test_topdown_order_is_not_the_ground_line(tmp_path):
    """A sign anchored high (image bottom 40, sortY 20) and a crate in front of it (bottom 31,
    sortY 30): the ground line draws the crate over the sign, Tiled's topdown order (image
    bottom) the reverse. The export therefore stores objects sorted, with draworder index."""
    root = tmp_path / "order"
    save(np.dstack([np.full((30, 10, 3), (200, 40, 40), np.uint8), np.full((30, 10), 255, np.uint8)]),
         root / "sign.png")
    save(np.dstack([np.full((10, 10, 3), (40, 40, 200), np.uint8), np.full((10, 10), 255, np.uint8)]),
         root / "crate.png")
    doc = {"schema": "generate2dmap.map_bundle.v2", "world": {"width": 40, "height": 40, "unit": "px"},
           "layers": [{"name": "props", "kind": "objects"}],
           "props": {"sign": {"image": "sign.png", "anchor_px": [5, 10]},
                     "crate": {"image": "crate.png", "anchor_px": [5, 9]}},
           "objects": [{"id": "sign", "prop": "sign", "x": 20, "y": 20, "anchor_px": [5, 10]},
                       {"id": "crate", "prop": "crate", "x": 22, "y": 30, "anchor_px": [5, 9]}],
           "collision": {"actorRadius": 2}}
    (root / "map-bundle.json").write_text(json.dumps(doc), encoding="utf-8")
    out = export(tmp_path, bundle=root / "map-bundle.json", name="order-tiled")
    expected = mb.render_map(mb.load_bundle(root / "map-bundle.json"))
    assert tuple(expected[25, 20, :3]) == (40, 40, 200)  # the crate is in front
    tmj = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    assert [o["name"] for o in tmj["layers"][0]["objects"]] == ["sign", "crate"]
    tmj["layers"][0]["draworder"] = "topdown"
    (out / "topdown.tmj").write_text(json.dumps(tmj), encoding="utf-8")
    topdown = et.render_tiled(et.read_tiled_map(out / "topdown.tmj"), (40, 40))
    assert tuple(topdown[25, 20, :3]) == (200, 40, 40) and et.differing_pixels(topdown, expected) == 80


def test_d6_art_and_flip_x_round_trip(tmp_path):
    """D6 in the export: an object's own image and the registry art become separate prop tiles, a
    flip_x object is a tile object with Tiled's horizontal flip flag placed by its mirrored anchor,
    and the files re-render at 0 px with the built-in reader and pytiled-parser. Without a registry,
    prop_packs labels and occluder sources supply the art. Footprints are forge_nav's (D6, D7)."""
    path = d6_bundle(tmp_path / "registry")
    out = export(tmp_path, "--embedded-variant", bundle=path, name="registry-tiled")
    expected = mb.render_map(mb.load_bundle(path))
    for name in ("map.tmj", "map.embedded.tmj"):
        assert et.differing_pixels(et.render_tiled(et.read_tiled_map(out / name), (64, 48)), expected) == 0, name
    tmj = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    objects = {o["name"]: o for o in tmj["layers"][0]["objects"]}
    assert objects["post-2"]["gid"] & 0x80000000 and not objects["post-1"]["gid"] & 0x80000000
    assert objects["post-2"]["gid"] & 0x0FFFFFFF == objects["post-1"]["gid"]
    assert (objects["post-1"]["x"], objects["post-2"]["x"]) == (8.0, 26.0)
    assert {p["name"]: p["value"] for p in objects["post-2"]["properties"]}["flipX"] is True
    tiles = {(properties(t)["prop"][1], t.find("image").get("source")): t for t in tsx(out / "props.tsx").findall("tile")}
    assert set(tiles) == {("post", "images/post.png"), ("post", "images/lamp.png")}
    lamp = tiles[("post", "images/lamp.png")]
    assert (properties(lamp)["anchorX"], properties(lamp)["anchorY"]) == (("float", "4"), ("float", "7"))
    assert lamp.find("objectgroup") is None and "solid" not in properties(lamp)
    collision = {o["name"]: o for o in tmj["layers"][1]["objects"]}
    assert (collision["post-1"]["x"], collision["post-2"]["x"]) == (9.5, 26.5)  # the footprint mirrors too
    assert "lamp-1" not in collision

    path = d6_bundle(tmp_path / "packs", registry=False)
    out = export(tmp_path, bundle=path, name="packs-tiled")
    expected = mb.render_map(mb.load_bundle(path))
    assert et.differing_pixels(et.render_tiled(et.read_tiled_map(out / "map.tmj"), (64, 48)), expected) == 0
    tmj = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    assert [o["name"] for o in tmj["layers"][0]["objects"]] == ["crate-1", "stone-1"]  # ghost has no art
    tiles = {properties(t)["prop"][1]: t.find("image").get("source") for t in tsx(out / "props.tsx").findall("tile")}
    assert tiles == {"crate": "images/prop.png", "stone": "images/stone.png"}


def test_flip_x_rerenders_with_pytiled(tmp_path):
    """The horizontal flip flag of a flip_x object, read by an independent parser (pytiled-parser)."""
    pytest.importorskip("pytiled_parser")
    path = d6_bundle(tmp_path / "registry")
    out = export(tmp_path, "--embedded-variant", bundle=path, name="registry-tiled")
    expected = mb.render_map(mb.load_bundle(path))
    for name in ("map.tmj", "map.embedded.tmj"):
        assert et.differing_pixels(pytiled_render(out / name, (64, 48)), expected) == 0, name
        run = run_cli([TOOL, "verify", "--map", out / name, "--bundle", path, "--reader", "pytiled"])
        assert run.returncode == 0, run.stderr


def test_reader_rules(tmp_path):
    out = export(tmp_path)
    expected = reference(tmp_path)
    flipped = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    flipped["layers"][1]["data"][0] |= 0x80000000
    (out / "flipped.tmj").write_text(json.dumps(flipped), encoding="utf-8")
    with pytest.raises(mb.BundleError, match="flip flags"):
        et.render_tiled(et.read_tiled_map(out / "flipped.tmj"), (192, 128))
    flipped = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    flipped["layers"][3]["objects"][0]["gid"] |= 0x40000000  # vertical flip of a tile object: refused
    (out / "vertical.tmj").write_text(json.dumps(flipped), encoding="utf-8")
    with pytest.raises(mb.BundleError, match="vertical, diagonal or hexagonal flip flags"):
        et.render_tiled(et.read_tiled_map(out / "vertical.tmj"), (192, 128))
    (out / "not-a-tileset.tsx").write_text('<?xml version="1.0"?><map/>', encoding="utf-8")
    broken = json.loads((out / "map.tmj").read_text(encoding="utf-8"))
    broken["tilesets"][0]["source"] = "not-a-tileset.tsx"
    (out / "broken.tmj").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(mb.BundleError, match="is not a TSX tileset"):
        et.read_tiled_map(out / "broken.tmj")
    assert et.differing_pixels(expected, expected[:10]) == 192 * 128


def test_verify_detects_changes(tmp_path):
    out = export(tmp_path)
    bundle = tmp_path / "map" / "map-bundle.json"
    assert run_cli([TOOL, "verify", "--map", out / "map.tmj", "--bundle", bundle]).returncode == 0
    edit(out / "map.tmj", lambda doc: doc["layers"][1]["data"].__setitem__(0, 1))
    run = run_cli([TOOL, "verify", "--map", out / "map.tmj", "--bundle", bundle])
    assert run.returncode == 1 and "pixel(s) differ" in run.stderr
    edit(out / "map.tmj", lambda doc: doc["layers"][1]["data"].__setitem__(0, 999))
    run = run_cli([TOOL, "verify", "--map", out / "map.tmj", "--bundle", bundle])
    assert run.returncode == 1 and "gid 999 belongs to no tileset" in run.stderr


def test_reserved_layer_names(tmp_path):
    path = edit(demo(tmp_path, hashes=False), lambda doc: doc["layers"][2].update(name="collision"))
    run = run_cli([TOOL, "export", "--bundle", path, "--output-dir", tmp_path / "out"])
    assert run.returncode == 1 and "reserved for the Tiled export" in run.stderr and not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- CLI conventions

def test_cli_help_cp1252():
    assert_cli_help(SKILL, "export_tiled")
    for verb in ("export", "verify"):
        result = run_cli([TOOL, verb, "--help"], "cp1252")
        assert result.returncode == 0 and result.stdout.isascii()


def test_cli_refuses_existing_output(tmp_path):
    path = demo(tmp_path)
    out = tmp_path / "taken"
    out.mkdir()
    run = run_cli([TOOL, "export", "--bundle", path, "--output-dir", out])
    assert run.returncode == 1 and run.stderr.startswith("error: refusing to replace existing output")
    assert list(out.iterdir()) == []


def test_cli_failure_publishes_nothing(tmp_path, monkeypatch, capsys):
    path = demo(tmp_path)
    out = tmp_path / "result" / "tiled"

    def wrong_render(tiled, size):
        image = mb.render_map(mb.load_bundle(path)).copy()
        image[0, 0] = (1, 2, 3, 4)
        return image

    monkeypatch.setattr(et, "render_tiled", wrong_render)
    assert et.main(["export", "--bundle", str(path), "--output-dir", str(out), "--embedded-variant"]) == 1
    captured = capsys.readouterr()
    assert "rerender_map_tmj=1" in captured.err and "nothing was published" in captured.err
    assert not out.exists() and list((tmp_path / "result").iterdir()) == []

    (tmp_path / "map" / "props" / "tree.png").unlink()
    run = run_cli([TOOL, "export", "--bundle", path, "--output-dir", out])
    assert run.returncode == 1 and "does not validate" in run.stderr and not out.exists()
