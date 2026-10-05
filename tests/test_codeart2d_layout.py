"""codeart2d layout_build: vertex-grid terrain, corner-Wang tiles, seeded scatter, exact collision
rectangles, Appendix C reachability and the map_bundle.v2 it writes (plan B21-T1).

Everything is synthetic: the meadow example's props are inline PixelSpecs and the Wang tilesets are
built here from flat quadrant colours. Independent re-implementations of the Appendix C validity
test and of the blocked raster cross-check the tool instead of trusting its own QA.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
from pathlib import Path
import re

import numpy as np
from PIL import Image
import pytest

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, run_cli,
                             script_path)

SKILL = "codeart2d"
SCRIPT = script_path(SKILL, "layout_build")
MEADOW = SKILLS_DIR / SKILL / "examples" / "meadow-layout.json"
SCHEMAS = SKILLS_DIR / SKILL / "references" / "schemas"
layout_build = load_script(SKILL, "layout_build")

# --------------------------------------------------------------------------- proposed schema additions
# Handoff section 5 requests these additions; tests validate against the vendored schemas with them
# applied in memory (additionalProperties is already true, so the frozen schemas accept the documents).
PROPOSED_MAP_DEFS = {
    "vertex_grid_v1": {
        "description": "Terrain materials on the (W+1) x (H+1) vertex grid of a tile map (map_bundle_v2 "
                       "terrain.vertex_grid). data[row][column] indexes materials; tile (x, y) has corners "
                       "[data[y][x], data[y][x+1], data[y+1][x], data[y+1][x+1]] = [top_left, top_right, "
                       "bottom_left, bottom_right], the tileset_v1 wang order. Each vertex owns the tile-sized "
                       "square centred on it.",
        "type": "object",
        "required": ["schema", "size", "materials", "data"],
        "properties": {
            "schema": {"const": "generate2dmap.vertex_grid.v1"},
            "size": {"$ref": "common.schema.json#/$defs/size2"},
            "tile_size": {"anyOf": [{"type": "integer", "minimum": 1}, {"$ref": "common.schema.json#/$defs/size2"}]},
            "materials": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "base": {"type": "string", "minLength": 1},
            "data": {"type": "array", "minItems": 2,
                     "items": {"type": "array", "minItems": 2, "items": {"type": "integer", "minimum": 0}}},
        },
    },
}
PROPOSED_BUNDLE_PROPERTIES = {
    "id": {"type": "string", "minLength": 1},
    "props": {"type": "object", "additionalProperties": {"$ref": "#/$defs/propItem"}},
    "roads": {"type": "array", "items": {
        "type": "object", "required": ["id", "polyline"],
        "properties": {"id": {"type": "string", "minLength": 1}, "material": {"type": "string", "minLength": 1},
                       "width": {"type": "number", "exclusiveMinimum": 0},
                       "polyline": {"type": "array", "minItems": 2,
                                    "items": {"$ref": "common.schema.json#/$defs/point2"}}}}},
    "provenance": {"$ref": "common.schema.json#/$defs/provenance"},
}
PROPOSED_COLLISION_PROPERTIES = {"rectCell": {"type": "number", "exclusiveMinimum": 0}}
PROPOSED_OBJECT_PROPERTIES = {"kind": {"type": "string", "minLength": 1}, "flip_x": {"type": "boolean"},
                              "group": {"type": "string", "minLength": 1}}
PROPOSED_PORTAL_PROPERTIES = {"edge": {"enum": ["west", "east", "north", "south"]}}
PROPOSED_SOLID_PROPERTIES = {"material": {"type": "string", "minLength": 1}}
PROPOSED_CODEART_DEFS = {
    "layout_spec_v1": {
        "description": "Input of layout_build.py. size is in tiles; terrain shapes use tile (vertex) "
                       "coordinates and paint in order; objects, spawns, interactions, scatter regions and "
                       "clearances are world pixels.",
        "type": "object",
        "required": ["schema", "size", "materials"],
        "properties": {
            "schema": {"const": "codeart2d.layout_spec.v1"},
            "map_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$"},
            "size": {"$ref": "common.schema.json#/$defs/size2"},
            "tile_size": {"type": "integer", "minimum": 2},
            "seed": {"type": "integer", "minimum": 0},
            "materials": {"type": "object", "minProperties": 1, "additionalProperties": {
                "type": "object", "required": ["color"],
                "properties": {"color": {"$ref": "common.schema.json#/$defs/hexColor"},
                               "walkable": {"type": "boolean"}, "priority": {"type": "number"}}}},
            "base": {"type": "string", "minLength": 1},
            "terrain": {"type": "array", "items": {
                "type": "object", "required": ["shape", "material"],
                "properties": {"id": {"type": "string"}, "shape": {"enum": ["ellipse", "rect", "polygon", "road"]},
                               "material": {"type": "string"},
                               "center": {"$ref": "common.schema.json#/$defs/point2"},
                               "radius": {"anyOf": [{"type": "number", "exclusiveMinimum": 0},
                                                    {"$ref": "common.schema.json#/$defs/point2"}]},
                               "rotate": {"type": "number"}, "wobble": {"type": "number", "minimum": 0},
                               "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                               "points": {"type": "array", "items": {"$ref": "common.schema.json#/$defs/point2"}},
                               "width": {"type": "number", "exclusiveMinimum": 0}, "smooth": {"type": "boolean"}}}},
            "hygiene": {"type": "object", "properties": {"saddles": {"type": "boolean"},
                                                         "specks": {"type": "boolean"}}},
            "actor": {"type": "object", "properties": {"radius": {"type": "number", "exclusiveMinimum": 0},
                                                       "ySquash": {"type": "number", "exclusiveMinimum": 0}}},
            "prop_pack": {"$ref": "common.schema.json#/$defs/relPath"},
            "props": {"type": "object", "additionalProperties": {
                "type": "object",
                "properties": {"image": {"$ref": "common.schema.json#/$defs/relPath"},
                               "pixelspec": {"anyOf": [{"$ref": "#/$defs/pixelspec_v1"},
                                                       {"$ref": "common.schema.json#/$defs/relPath"}]},
                               "pack": {"type": "string"}, "variants": {"type": "array", "minItems": 1},
                               "anchor_px": {"$ref": "common.schema.json#/$defs/point2"},
                               "size": {"$ref": "common.schema.json#/$defs/size2"},
                               "color": {"$ref": "common.schema.json#/$defs/hexColor"},
                               "footprint": {"$ref": "map.schema.json#/$defs/footprint"},
                               "solid": {"type": "boolean"}, "occlusion": {"enum": ["low", "tall", "foreground"]},
                               "flip": {"type": "boolean"}, "scale": {"type": "number", "exclusiveMinimum": 0},
                               "interactions": {"type": "array", "items": {
                                   "type": "object", "required": ["name"],
                                   "properties": {"name": {"type": "string"},
                                                  "offset": {"$ref": "common.schema.json#/$defs/point2"},
                                                  "reach": {"type": "number", "exclusiveMinimum": 0}}}}}}},
            "objects": {"type": "array", "items": {
                "type": "object", "required": ["id", "prop", "x", "y"],
                "properties": {"id": {"type": "string"}, "prop": {"type": "string"}, "x": {"type": "number"},
                               "y": {"type": "number"}, "flip_x": {"type": "boolean"},
                               "scale": {"type": "number", "exclusiveMinimum": 0},
                               "variant": {"type": "integer", "minimum": 0}}}},
            "scatter": {"type": "array", "items": {
                "type": "object", "required": ["kinds", "count", "spacing"],
                "properties": {"id": {"type": "string"},
                               "kinds": {"type": "object", "minProperties": 1,
                                         "additionalProperties": {"type": "number", "exclusiveMinimum": 0}},
                               "count": {"type": "integer", "minimum": 0},
                               "spacing": {"type": "number", "exclusiveMinimum": 0},
                               "attempts": {"type": "integer", "minimum": 1},
                               "density": {"type": "object"}, "clearance": {"type": "object"},
                               "near": {"type": "object"},
                               "region": {"type": "array", "items": {"type": "number"},
                                          "minItems": 4, "maxItems": 4}}}},
            "exits": {"type": "array", "items": {
                "type": "object", "required": ["id", "edge", "to"],
                "properties": {"id": {"type": "string"}, "edge": {"enum": ["west", "east", "north", "south"]},
                               "road": {"type": "string"},
                               "span": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                               "to": {"type": "string", "minLength": 1},
                               "depth": {"type": "number", "exclusiveMinimum": 0},
                               "radius": {"type": "number", "minimum": 0}, "arrival": {"type": "string"}}}},
            "spawns": {"type": "array", "items": {"type": "object", "required": ["id", "x", "y"]}},
            "interactions": {"type": "array", "items": {"type": "object", "required": ["id", "x", "y"]}},
        },
    },
}


def _registry(patch):
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    schemas = {path.name.removesuffix(".schema.json"): json.loads(path.read_text(encoding="utf-8"))
               for path in sorted(SCHEMAS.glob("*.schema.json"))}
    schemas = copy.deepcopy(schemas)
    patch(schemas)
    registry = Registry().with_resources(
        (schema["$id"], DRAFT202012.create_resource(schema)) for schema in schemas.values())
    return schemas, registry


def apply_proposed(schemas):
    defs = schemas["map"]["$defs"]
    defs.update(copy.deepcopy(PROPOSED_MAP_DEFS))
    defs["map_bundle_v2"]["properties"].update(copy.deepcopy(PROPOSED_BUNDLE_PROPERTIES))
    defs["collision"]["properties"].update(copy.deepcopy(PROPOSED_COLLISION_PROPERTIES))
    defs["mapObject"]["properties"].update(copy.deepcopy(PROPOSED_OBJECT_PROPERTIES))
    defs["portal"]["properties"].update(copy.deepcopy(PROPOSED_PORTAL_PROPERTIES))
    defs["solid"]["properties"].update(copy.deepcopy(PROPOSED_SOLID_PROPERTIES))
    schemas["codeart"]["$defs"].update(copy.deepcopy(PROPOSED_CODEART_DEFS))


def assert_valid_proposed(instance, domain, name):
    from jsonschema import Draft202012Validator

    schemas, registry = _registry(apply_proposed)
    validator = Draft202012Validator({"$ref": f"{schemas[domain]['$id']}#/$defs/{name}"}, registry=registry)
    errors = [f"{error.json_path}: {error.message}" for error in validator.iter_errors(instance)]
    assert not errors, f"{domain}/{name} (with the proposed additions):\n  " + "\n  ".join(errors)


# --------------------------------------------------------------------------- independent checks

def contains(solid, xs, ys):
    """Closed point-in-solid test written independently of layout_build (plan Appendix C solids)."""
    if solid["shape"] == "rect":
        return ((xs >= solid["x"]) & (xs <= solid["x"] + solid["w"])
                & (ys >= solid["y"]) & (ys <= solid["y"] + solid["h"]))
    if solid["shape"] == "ellipse":
        angle = math.radians(solid.get("rotate", 0))
        dx, dy = xs - solid["cx"], ys - solid["cy"]
        u = dx * math.cos(angle) + dy * math.sin(angle)
        v = -dx * math.sin(angle) + dy * math.cos(angle)
        return (u / solid["rx"]) ** 2 + (v / solid["ry"]) ** 2 <= 1.0
    path = solid["points"]
    inside = np.zeros(np.broadcast(xs, ys).shape, bool)
    for (x0, y0), (x1, y1) in zip(path, path[1:] + path[:1]):
        if y0 != y1:
            inside ^= ((ys >= min(y0, y1)) & (ys < max(y0, y1))) & (xs < x0 + (ys - y0) * (x1 - x0) / (y1 - y0))
    return inside


def valid_points(xs, ys, bundle):
    """Plan Appendix C: the point and 8 samples of the footprint ellipse inside the map, outside every solid."""
    collision = bundle["collision"]
    radius, squash = collision["actorRadius"], collision.get("ySquash", 1.0)
    width, height = bundle["world"]["width"], bundle["world"]["height"]
    ok = np.ones(np.shape(xs), bool)
    offsets = [(0.0, 0.0)] + [(radius * math.cos(k * math.pi / 4), radius * squash * math.sin(k * math.pi / 4))
                              for k in range(8)]
    for dx, dy in offsets:
        sx, sy = xs + dx, ys + dy
        ok &= (sx >= 0) & (sx <= width) & (sy >= 0) & (sy <= height)
        for solid in collision["solids"]:
            ok &= ~contains(solid, sx, sy)
    return ok


def rect_distance(x, y, rect):
    rx, ry, rw, rh = rect
    return math.hypot(max(rx - x, 0.0, x - (rx + rw)), max(ry - y, 0.0, y - (ry + rh)))


def independent_reachability(bundle, start):
    """BFS over cell centres (cell = max(1, round_half_up(r / 2))) with the independent validity test."""
    from scipy import ndimage

    radius = bundle["collision"]["actorRadius"]
    cell = max(1, int(math.floor(radius / 2.0 + 0.5)))
    width, height = bundle["world"]["width"], bundle["world"]["height"]
    columns, rows = int(math.ceil(width / cell - 0.5)), int(math.ceil(height / cell - 0.5))
    xs, ys = np.meshgrid((np.arange(columns) + 0.5) * cell, (np.arange(rows) + 0.5) * cell)
    valid = valid_points(xs, ys, bundle)
    labels, _ = ndimage.label(valid)
    row, col = int(start[1] // cell), int(start[0] // cell)
    candidates = [(math.hypot((c + 0.5) * cell - start[0], (r + 0.5) * cell - start[1]), r, c)
                  for r in range(row - 2, row + 3) for c in range(col - 2, col + 3)
                  if 0 <= r < rows and 0 <= c < columns and valid[r, c]]
    assert candidates, "start point has no valid cell nearby"
    _, r0, c0 = min(candidates)
    return labels == labels[r0, c0], xs, ys


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def path_values(document):
    """Every path-like value of a JSON document (image, manifest, vertex_grid, path, report keys)."""
    if isinstance(document, dict):
        for key, value in document.items():
            if key in ("image", "manifest", "vertex_grid", "path", "report"):
                yield value
            yield from path_values(value)
    elif isinstance(document, list):
        for value in document:
            yield from path_values(value)


# --------------------------------------------------------------------------- fixtures

def write_spec(folder: Path, spec: dict, name: str = "layout.json") -> Path:
    path = folder / name
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


def build(spec_path: Path, out: Path, *extra: str):
    return run_cli([SCRIPT, "--spec", spec_path, "--output-dir", out, *extra], timeout=600)


def small_spec(**overrides) -> dict:
    spec = {
        "schema": "codeart2d.layout_spec.v1", "map_id": "test", "size": [20, 12], "tile_size": 16, "seed": 3,
        "materials": {"grass": {"color": "#5aa344"}, "path": {"color": "#c79d62"},
                      "water": {"color": "#2c62ab", "walkable": False}},
        "terrain": [{"id": "road", "shape": "road", "material": "path", "width": 1.7,
                     "points": [[-1, 6], [10, 6], [21, 6]]}],
        "props": {"tree": {"occlusion": "tall"}},
        "scatter": [{"id": "trees", "kinds": {"tree": 1}, "count": 12, "spacing": 20}],
        "exits": [{"id": "west", "edge": "west", "road": "road", "to": "a:from-test"},
                  {"id": "east", "edge": "east", "road": "road", "to": "b:from-test"}],
    }
    spec.update(overrides)
    return spec


def load_output(out: Path) -> dict:
    return {name: json.loads((out / name).read_text(encoding="utf-8"))
            for name in ("map-bundle.json", "terrain-vertices.json", "layout-qa.json", "codeart-meta.json")}


@pytest.fixture(scope="module")
def meadow(tmp_path_factory):
    """The shipped example, built twice with seed 7 and once with seed 8 (strict QC, previews)."""
    root = tmp_path_factory.mktemp("meadow")
    runs = {}
    for name, seed in (("a", "7"), ("b", "7"), ("c", "8")):
        result = build(MEADOW, root / name, "--seed", seed, "--preview", "--strict-qc")
        assert result.returncode == 0, result.stderr
        runs[name] = root / name
    return runs


# --------------------------------------------------------------------------- CLI conventions

def test_help_is_ascii_under_cp1252_and_cp950():
    assert_cli_help(SKILL, "layout_build")


def test_refuses_existing_output(tmp_path):
    out = tmp_path / "map"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    result = build(write_spec(tmp_path, small_spec()), out)
    assert result.returncode == 1
    assert result.stderr.startswith("error:") and "already exists" in result.stderr
    assert [p.name for p in out.iterdir()] == ["keep.txt"]


def test_strict_qc_failure_publishes_nothing(tmp_path):
    """A water wall cuts the east exit off: strict QC exits 1 and leaves no output or stage behind."""
    spec = small_spec(terrain=small_spec()["terrain"] + [
        {"id": "moat", "shape": "rect", "material": "water", "box": [14, -1, 15, 13]}])
    result = build(write_spec(tmp_path, spec), tmp_path / "map", "--strict-qc")
    assert result.returncode == 1
    assert "exits_reachable" in result.stderr and "east" in result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["layout.json"]
    # without --strict-qc the same map is published with a failing QA envelope, for the debug overlay
    result = build(write_spec(tmp_path, spec), tmp_path / "map", "--preview")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "fail"
    qa = json.loads((tmp_path / "map" / "layout-qa.json").read_text(encoding="utf-8"))
    failed = {check["id"]: check["value"] for check in qa["checks"] if check["status"] == "fail"}
    assert failed["exits_reachable"] == ["east"]
    assert (tmp_path / "map" / "debug.png").is_file()


def test_errors_are_one_line_and_ascii(tmp_path):
    for spec, needle in ((dict(small_spec(), schema="other"), "schema"),
                         (small_spec(exits=[{"id": "x", "edge": "west", "to": "a"}]), "road"),
                         (small_spec(terrain=[{"shape": "road", "material": "lava", "points": [[0, 0], [1, 1]]}]),
                          "unknown material")):
        result = build(write_spec(tmp_path, spec), tmp_path / "never")
        assert result.returncode == 1 and result.stderr.startswith("error:") and needle in result.stderr
        assert result.stderr.isascii() and len(result.stderr.strip().splitlines()) == 1
        assert not (tmp_path / "never").exists()


def test_reference_commands_are_single_line_and_use_real_flags():
    """B21-T4: every documented command is one line, names a shipped script and only uses its flags."""
    lines = (SKILLS_DIR / SKILL / "references" / "layouts-and-parallax.md").read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith('python "<skill-dir>/scripts/')]
    assert len(starts) == 4
    for index in starts:  # one line inside its own code block: no continuation, fence closes next
        assert lines[index - 1] == "```bash" and lines[index + 1] == "```"
        assert not lines[index].rstrip().endswith(("\\", "^", "`"))
    for command in (lines[index] for index in starts):
        name = re.match(r'python "<skill-dir>/scripts/([a-z_]+)\.py"', command).group(1)
        help_text = run_cli([script_path(SKILL, name), "--help"]).stdout
        for flag in re.findall(r"(--[a-z][a-z-]+)", command):
            assert flag in help_text, (name, flag)


# --------------------------------------------------------------------------- B21-T1 acceptance

def test_meadow_passes_and_every_exit_is_reachable(meadow):
    out = load_output(meadow["a"])
    bundle, qa = out["map-bundle.json"], out["layout-qa.json"]
    assert qa["status"] == "pass", [c for c in qa["checks"] if c["status"] != "pass"]
    assert {portal["id"] for portal in bundle["portals"]} == {"exit-west", "exit-east", "exit-north"}
    spawns = {spawn["id"]: spawn for spawn in bundle["spawns"]}
    reach, xs, ys = independent_reachability(bundle, (spawns["start"]["x"], spawns["start"]["y"]))
    for portal in bundle["portals"]:
        assert portal["activation"] == "intent" and portal["latch"] and portal["requiresMovement"]
        near = [rect_distance(x, y, portal["rect"]) <= portal["radius"]
                for x, y in zip(xs[reach].tolist(), ys[reach].tolist())]
        assert any(near), f"{portal['id']} is not reachable by the independent BFS"
        (arrival_id,) = portal["entranceByFrom"].values()
        arrival = spawns[arrival_id]
        assert rect_distance(arrival["x"], arrival["y"], portal["rect"]) > portal["radius"]
        assert bool(valid_points(np.array([arrival["x"]]), np.array([arrival["y"]]), bundle)[0])
        dx, dy = portal["travelDirection"]
        assert arrival["facing"] == {(-1, 0): "east", (1, 0): "west", (0, -1): "south", (0, 1): "north"}[(dx, dy)]
    # every interaction (the house doors) is within reach of the reachable area
    for item in bundle["interactions"]:
        assert np.any(np.hypot(xs[reach] - item["x"], ys[reach] - item["y"]) <= item["reach"]), item["id"]


def test_nav_is_never_more_permissive_than_appendix_c(tmp_path, meadow):
    """The tool also checks move midpoints, so its reachable cells are a subset of the independent BFS."""
    bundle = load_output(meadow["a"])["map-bundle.json"]
    reach, _, _ = independent_reachability(bundle, (300, 216))
    solids = layout_build.SolidSet(bundle["collision"]["solids"])
    walker = layout_build.Walker(solids, (bundle["world"]["width"], bundle["world"]["height"]),
                                 bundle["collision"]["actorRadius"], bundle["collision"]["ySquash"])
    lattice = walker.lattice()
    cells_valid = lattice[0::2, 0::2]
    xs, ys = np.meshgrid((np.arange(cells_valid.shape[1]) + 0.5) * walker.cell,
                         (np.arange(cells_valid.shape[0]) + 0.5) * walker.cell)
    np.testing.assert_array_equal(cells_valid, valid_points(xs, ys, bundle))
    reported = bundle["provenance"]["params"]["navigation"]
    assert reported["reachable_cells"] <= int(reach.sum())
    assert reported["cell"] == walker.cell == 3


def test_rect_union_equals_blocked_set(meadow):
    bundle = load_output(meadow["a"])["map-bundle.json"]
    collision = bundle["collision"]
    cell = collision["rectCell"]
    width, height = bundle["world"]["width"], bundle["world"]["height"]
    columns, rows = math.ceil(width / cell), math.ceil(height / cell)
    xs, ys = np.meshgrid((np.arange(columns) + 0.5) * cell, (np.arange(rows) + 0.5) * cell)
    blocked = np.zeros((rows, columns), bool)
    for solid in collision["solids"]:
        blocked |= contains(solid, xs, ys)
    cover = np.zeros((rows, columns), np.int32)
    for x, y, w, h in collision["rects"]:
        cover += (xs >= x) & (xs <= x + w) & (ys >= y) & (ys <= y + h)
    np.testing.assert_array_equal(cover > 0, blocked)
    assert cover.max() == 1, "rectangles overlap"
    assert sum(w * h for _, _, w, h in collision["rects"]) == int(blocked.sum()) * cell * cell
    assert len(collision["rects"]) < int(blocked.sum()) / 3, "rectangles were not merged"


def test_terrain_solids_are_the_exact_vertex_squares(meadow):
    out = load_output(meadow["a"])
    bundle, grid = out["map-bundle.json"], out["terrain-vertices.json"]
    data = np.array(grid["data"])
    tile = grid["tile_size"]
    water = grid["materials"].index("water")
    world_w, world_h = bundle["world"]["width"], bundle["world"]["height"]
    pixel_cols = np.floor((np.arange(world_w) + 0.5) / tile + 0.5).astype(int)
    pixel_rows = np.floor((np.arange(world_h) + 0.5) / tile + 0.5).astype(int)
    expected = data[np.ix_(pixel_rows, pixel_cols)] == water
    xs, ys = np.meshgrid(np.arange(world_w) + 0.5, np.arange(world_h) + 0.5)
    actual = np.zeros((world_h, world_w), bool)
    terrain = [solid for solid in bundle["collision"]["solids"] if solid["id"].startswith("terrain-")]
    assert terrain and all(solid["shape"] == "rect" and solid["material"] == "water" for solid in terrain)
    for solid in terrain:
        actual |= contains(solid, xs, ys)
    np.testing.assert_array_equal(actual, expected)


def test_output_is_seeded(meadow):
    names = ["map-bundle.json", "terrain-vertices.json", "layout-qa.json", "codeart-meta.json", "preview.png"]
    names += sorted(f"props/{p.name}" for p in (meadow["a"] / "props").iterdir())
    for name in names:
        assert (meadow["a"] / name).read_bytes() == (meadow["b"] / name).read_bytes(), name
    first = load_output(meadow["a"])["map-bundle.json"]["objects"]
    other = load_output(meadow["c"])["map-bundle.json"]["objects"]
    assert [(o["x"], o["y"]) for o in first] != [(o["x"], o["y"]) for o in other]
    houses = [o for o in first if o["prop"] == "house"]
    assert houses == [o for o in other if o["prop"] == "house"], "fixed objects must not move with the seed"


def test_bundle_and_sidecars_validate(meadow):
    out = load_output(meadow["a"])
    bundle = out["map-bundle.json"]
    assert_valid_contract(bundle, "map", "map_bundle_v2", skill=SKILL)
    assert_valid_proposed(bundle, "map", "map_bundle_v2")
    for item in bundle["props"].values():
        assert_valid_contract(item, "map", "propItem", skill=SKILL)
        image = meadow["a"] / item["image"]
        assert sha256_of(image) == item["sha256"]
    assert_valid_contract(out["layout-qa.json"], "common", "qaEnvelope", skill=SKILL)
    assert_valid_contract(out["codeart-meta.json"], "codeart", "codeart_meta_v1", skill=SKILL)
    assert_valid_proposed(out["terrain-vertices.json"], "map", "vertex_grid_v1")
    assert_valid_proposed(json.loads(MEADOW.read_text(encoding="utf-8")), "codeart", "layout_spec_v1")
    for ref in out["layout-qa.json"]["outputs"] + out["codeart-meta.json"]["outputs"]:
        assert sha256_of(meadow["a"] / ref["path"]) == ref["sha256"], ref["path"]
    assert bundle["art_source"] == "code" and bundle["placeholder"] is True  # flat-colour ground, no tileset
    assert out["codeart-meta.json"]["placeholder"] is True
    assert bundle["layers"][0] == {"name": "ground", "kind": "image", "image": "ground.png",
                                   "sha256": sha256_of(meadow["a"] / "ground.png")}
    assert not any(re.match(r"[A-Za-z]:|/|\\\\", str(value)) for value in path_values(bundle))


def test_prop_variety_mirroring_and_footprints(meadow):
    bundle = load_output(meadow["a"])["map-bundle.json"]
    scatter = {item["id"]: item for item in bundle["provenance"]["params"]["scatter"]}
    assert scatter["forest"]["placed"] == 70
    assert scatter["forest"]["same_look_neighbour_share"] <= layout_build.VARIETY_WARN_SHARE
    forest = [o for o in bundle["objects"] if o.get("group") == "forest"]
    assert {o["kind"] for o in forest} == {"tree", "pine"}
    assert {o["flip_x"] for o in forest} == {True, False}
    solids = {solid["id"]: solid for solid in bundle["collision"]["solids"]}
    for obj in bundle["objects"]:
        footprint = obj.get("footprint")
        if not obj["solid"]:
            assert obj["id"] not in solids
            continue
        solid = solids[obj["id"]]
        prop = bundle["props"][obj["prop"]]["footprint"]
        sign = -1 if obj["flip_x"] else 1
        assert footprint["offset"] == [sign * prop["offset"][0] if prop["offset"][0] else 0, prop["offset"][1]]
        if solid["shape"] == "ellipse":
            assert solid["cx"] == pytest.approx(obj["x"] + footprint["offset"][0] * obj["scale"])
            assert solid["rx"] == pytest.approx(footprint["width"] * obj["scale"] / 2)
            assert solid["ry"] == pytest.approx(footprint["depth"] * obj["scale"] / 2)
        else:
            assert solid["w"] == pytest.approx(footprint["width"] * obj["scale"])
    # ground-line draw order data: sortY is the anchor line
    assert all(obj["sortY"] == obj["y"] for obj in bundle["objects"])


def test_exit_spans_follow_the_road_at_the_edge(meadow):
    out = load_output(meadow["a"])
    bundle, grid = out["map-bundle.json"], out["terrain-vertices.json"]
    data = np.array(grid["data"])
    path = grid["materials"].index("path")
    tile = grid["tile_size"]
    portals = {portal["id"]: portal for portal in bundle["portals"]}
    x, y, w, h = portals["exit-west"]["rect"]
    assert x == 0 and w == tile / 2
    rows = [r for r in range(data.shape[0]) if data[r, 0] == path]
    assert y == (min(rows) - 0.5) * tile and y + h == (max(rows) + 0.5) * tile
    x, y, w, h = portals["exit-north"]["rect"]
    columns = [c for c in range(data.shape[1]) if data[0, c] == path]
    assert y == 0 and x == (min(columns) - 0.5) * tile and x + w == (max(columns) + 0.5) * tile


# --------------------------------------------------------------------------- tiles and hygiene

QUADRANT_COLOURS = {"grass": (90, 163, 68), "path": (199, 157, 98), "water": (44, 98, 171)}


def write_wang_tileset(folder: Path, materials, tile: int = 16, variants: int = 3) -> Path:
    """A complete corner-Wang tileset whose tile quadrants take their corner's colour (exact seams by
    construction); full tiles get interior variants marked by a centre dot."""
    images, tiles = [], []
    half = tile // 2
    for combo in itertools.product(range(len(materials)), repeat=4):
        art = np.zeros((tile, tile, 4), np.uint8)
        art[..., 3] = 255
        for (rows, cols), corner in zip(((slice(0, half), slice(0, half)), (slice(0, half), slice(half, tile)),
                                         (slice(half, tile), slice(0, half)), (slice(half, tile), slice(half, tile))),
                                        combo):
            art[rows, cols, :3] = QUADRANT_COLOURS[materials[corner]]
        count = variants if len(set(combo)) == 1 else 1
        for variant in range(count):
            image = art.copy()
            if variant:
                image[half - 1:half + 1, half - 1:half + 1, :3] = (10 * variant, 10 * variant, 10 * variant)
            tiles.append({"index": len(images), "wang": list(combo), "variant": variant})
            images.append(image)
    columns = 8
    rows = math.ceil(len(images) / columns)
    atlas = np.zeros((rows * tile, columns * tile, 4), np.uint8)
    for index, image in enumerate(images):
        r, c = divmod(index, columns)
        atlas[r * tile:(r + 1) * tile, c * tile:(c + 1) * tile] = image
    name = "-".join(materials)
    folder.mkdir(parents=True, exist_ok=True)
    Image.fromarray(atlas).save(folder / f"{name}.png")
    manifest = {"schema": "generate2dmap.tileset.v1", "image": f"{name}.png", "tile_size": tile, "columns": columns,
                "kind": "wang_corner", "materials": list(materials), "tiles": tiles, "seamless_verified": False}
    assert_valid_contract(manifest, "map", "tileset_v1", skill=SKILL)
    path = folder / f"{name}.tileset.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_two_wang_sets_autotile_hygiene_and_seams(tmp_path):
    """grass/path and grass/water sets cannot draw a cell with path and water corners: hygiene demotes
    the road next to the pond, every cell gets exactly one tile whose corners match the vertex grid,
    and the tiled ground equals the per-pixel render of the vertex grid outside variant dots."""
    tiles = [write_wang_tileset(tmp_path / "tiles", ["grass", "path"]),
             write_wang_tileset(tmp_path / "tiles", ["grass", "water"])]
    spec = small_spec(terrain=[
        {"id": "pond", "shape": "ellipse", "material": "water", "center": [10, 3.2], "radius": [3.2, 2.4]},
        {"id": "road", "shape": "road", "material": "path", "width": 1.7, "points": [[-1, 6], [10, 5.2], [21, 6]]}])
    out = tmp_path / "map"
    args = [arg for path in tiles for arg in ("--tiles", str(path))]
    result = build(write_spec(tmp_path, spec), out, *args, "--preview", "--strict-qc")
    assert result.returncode == 0, result.stderr
    bundle = json.loads((out / "map-bundle.json").read_text(encoding="utf-8"))
    grid = json.loads((out / "terrain-vertices.json").read_text(encoding="utf-8"))
    assert bundle["provenance"]["params"]["hygiene"]["demoted_vertices"] > 0
    assert_valid_contract(bundle, "map", "map_bundle_v2", skill=SKILL)
    data = np.array(grid["data"])
    corners = np.stack([data[:-1, :-1], data[:-1, 1:], data[1:, :-1], data[1:, 1:]], axis=-1)
    path, water = grid["materials"].index("path"), grid["materials"].index("water")
    assert not np.any(np.any(corners == path, -1) & np.any(corners == water, -1))
    layers = [layer for layer in bundle["layers"] if layer["kind"] == "tiles"]
    assert [layer["name"] for layer in layers] == ["ground", "ground-grass-water"]
    owner = np.zeros(data[:-1, :-1].shape, np.int32)
    ground = np.zeros((data.shape[0] * 16 - 16, data.shape[1] * 16 - 16, 4), np.uint8)
    for layer in layers:
        entry = next(t for t in bundle["tilesets"] if t["id"] == layer["tileset"])
        manifest = json.loads((out / entry["manifest"]).read_text(encoding="utf-8"))
        assert sha256_of(out / entry["manifest"]) == entry["sha256"]
        atlas = np.asarray(Image.open(out / Path(entry["manifest"]).parent / manifest["image"]).convert("RGBA"))
        by_index = {tile["index"]: tile for tile in manifest["tiles"]}
        indices = np.array(layer["data"])
        owner += indices >= 0
        for (y, x), index in np.ndenumerate(indices):
            if index < 0:
                continue
            wang = [grid["materials"].index(manifest["materials"][c]) for c in by_index[index]["wang"]]
            assert wang == corners[y, x].tolist()
            r, c = divmod(int(index), manifest["columns"])
            ground[y * 16:(y + 1) * 16, x * 16:(x + 1) * 16] = atlas[r * 16:(r + 1) * 16, c * 16:(c + 1) * 16]
    assert np.all(owner == 1)
    colours = np.array([QUADRANT_COLOURS[name] for name in grid["materials"]], np.uint8)
    pixel_cols = np.floor((np.arange(ground.shape[1]) + 0.5) / 16 + 0.5).astype(int)
    pixel_rows = np.floor((np.arange(ground.shape[0]) + 0.5) / 16 + 0.5).astype(int)
    expected = colours[data[np.ix_(pixel_rows, pixel_cols)]]
    dots = np.all(ground[..., :3] == ground[..., :3].min(axis=-1, keepdims=True), axis=-1) & (ground[..., 0] <= 20)
    assert np.array_equal(ground[..., :3][~dots], expected[~dots])
    preview = np.asarray(Image.open(out / "preview.png").convert("RGBA"))
    untouched = preview[..., 3] == 255
    assert untouched.mean() > 0.5
    variety = [np.array(layer["data"]) for layer in layers]
    full_grass = variety[0][(corners == grid["materials"].index("grass")).all(-1) & (variety[0] >= 0)]
    assert len(set(full_grass.tolist())) == 3, "interior variants are used"
    assert bundle["placeholder"] is True  # the tree kind is a placeholder sprite
    assert bundle["art_source"] == "mixed"


def test_hygiene_saddles_specks_and_drawability():
    spec = layout_build.Layout(
        spec_path=Path("x.json"), spec_bytes=b"", map_id="t", width=8, height=8, tile=16, seed=0,
        materials=[layout_build.Material("grass", 0, (0, 0, 0, 255), True, 0),
                   layout_build.Material("path", 1, (0, 0, 0, 255), True, 1),
                   layout_build.Material("water", 2, (0, 0, 0, 255), False, 2)],
        base=layout_build.Material("grass", 0, (0, 0, 0, 255), True, 0), terrain=[],
        hygiene={"saddles": True, "specks": True}, actor_radius=5, y_squash=1, kinds={}, objects=[], scatter=[],
        exits=[], spawns=[], interactions=[])
    grid = np.zeros((9, 9), np.int16)
    grid[1, 1] = grid[2, 2] = 1          # a diagonal path saddle
    grid[7, 1] = 1                       # a lone path vertex
    grid[5:7, 5] = 2                     # water ...
    grid[5, 6:8] = 1                     # ... touching a path: no tileset draws path and water together
    keys = {((a * 3 + b) * 3 + c) * 3 + d for a, b, c, d in itertools.product((0, 1), repeat=4)}
    keys |= {((a * 3 + b) * 3 + c) * 3 + d for a, b, c, d in itertools.product((0, 2), repeat=4)}
    report = layout_build.run_hygiene(grid, np.array(sorted(keys)), spec)
    assert report["converged"] and report["saddles_filled"] == 1 and report["demoted_vertices"] >= 1
    assert report["specks_removed"] >= 1
    assert grid[1, 2] == 1 and grid[1, 1] == 1 and grid[2, 2] == 1  # the saddle's top-right corner was filled
    assert grid[7, 1] == 0 and grid[5, 6] == 0  # the lone vertex and the path beside the water are gone
    assert (grid[5:7, 5] == 2).all()              # water outranks path, so it stays
    assert np.isin(layout_build._cell_keys(grid, 3), np.array(sorted(keys))).all()


def test_distance_fields_without_scipy_are_identical(monkeypatch):
    rng = np.random.default_rng(5)
    for trial in range(4):
        mask = rng.random((41, 67)) < 0.02 * (trial + 1)
        cap = 9.5
        monkeypatch.setenv("FORGE_CORE_NO_SCIPY", "1")
        fallback = layout_build._distance_field(mask, cap)
        monkeypatch.delenv("FORGE_CORE_NO_SCIPY")
        np.testing.assert_array_equal(fallback, layout_build._distance_field(mask, cap))
        ys, xs = np.nonzero(mask)
        grid_y, grid_x = np.mgrid[0:41, 0:67]
        brute = np.hypot(grid_x[..., None] - xs, grid_y[..., None] - ys).min(axis=-1)
        np.testing.assert_array_equal(np.where(brute <= cap, brute, np.inf), fallback)


def test_meadow_without_scipy_is_byte_identical(tmp_path, meadow):
    result = run_cli([SCRIPT, "--spec", MEADOW, "--output-dir", tmp_path / "noscipy", "--seed", "7", "--strict-qc"],
                     env={"FORGE_CORE_NO_SCIPY": "1"}, timeout=600)
    assert result.returncode == 0, result.stderr
    for name in ("map-bundle.json", "terrain-vertices.json"):
        assert (tmp_path / "noscipy" / name).read_bytes() == (meadow["a"] / name).read_bytes().replace(
            b'"preview": true', b'"preview": false'), name


@pytest.mark.parametrize("seed", range(6))
def test_run_rects_union_is_exact(seed):
    rng = np.random.default_rng(seed)
    mask = rng.random((37, 53)) < (0.15 + 0.1 * seed)
    mask[5:20, 10:30] = True
    rects = layout_build._run_rects(mask)
    cover = np.zeros(mask.shape, np.int32)
    for x, y, w, h in rects:
        cover[y:y + h, x:x + w] += 1
    np.testing.assert_array_equal(cover > 0, mask)
    assert cover.max() <= 1


def test_thin_gap_follows_actor_size(tmp_path):
    """A one-vertex gap in a water wall is 16 px wide: an actor of radius 5 passes, radius 9 cannot."""
    terrain = small_spec()["terrain"] + [
        {"id": "wall-top", "shape": "rect", "material": "water", "box": [12, -1, 12, 5]},
        {"id": "wall-bottom", "shape": "rect", "material": "water", "box": [12, 7, 12, 13]}]
    for radius, expected in ((5, 0), (9, 1)):
        spec = small_spec(terrain=terrain, actor={"radius": radius}, scatter=[])
        out = tmp_path / f"r{radius}"
        result = build(write_spec(tmp_path, spec, f"r{radius}.json"), out, "--strict-qc")
        assert result.returncode == expected, result.stderr


def test_unreachable_interaction_and_spawn_are_reported(tmp_path):
    spec = small_spec(
        terrain=small_spec()["terrain"] + [{"id": "lake", "shape": "rect", "material": "water", "box": [2, 1, 8, 4]}],
        spawns=[{"id": "start", "x": 160, "y": 96}, {"id": "island", "x": 80, "y": 40}],
        interactions=[{"id": "sign", "x": 300, "y": 96}, {"id": "drowned", "x": 80, "y": 40, "reach": 6}],
        scatter=[])
    out = tmp_path / "map"
    result = build(write_spec(tmp_path, spec), out)
    assert result.returncode == 0, result.stderr
    qa = json.loads((out / "layout-qa.json").read_text(encoding="utf-8"))
    checks = {check["id"]: check for check in qa["checks"]}
    assert checks["spawns_reachable"]["value"] == ["island"]
    assert checks["interactions_reachable"]["value"] == ["drowned"]
    assert qa["status"] == "fail"


def test_props_from_images_and_pixelspec_keep_anchors(tmp_path):
    sprite = np.zeros((12, 10, 4), np.uint8)
    sprite[2:11, 2:8] = (120, 80, 40, 255)
    Image.fromarray(sprite).save(tmp_path / "stump.png")
    pixelspec = {"schema": "codeart2d.pixelspec.v1", "canvas": [5, 4], "anchor_px": [2.5, 4],
                 "palette": {"r": "#aa3322"},
                 "layers": [{"name": "body", "rows": [".rrr.", "rrrrr", "rrrrr", ".rrr."]}]}
    spec = small_spec(props={
        "stump": {"image": "stump.png", "anchor_px": [5, 11], "occlusion": "low",
                  "footprint": {"shape": "rect", "width": 6, "depth": 3, "offset": [0, -1.5]}},
        "berry": {"pixelspec": pixelspec, "occlusion": "low", "flip": False}},
        objects=[{"id": "stump-1", "prop": "stump", "x": 100, "y": 60},
                 {"id": "berry-1", "prop": "berry", "x": 120, "y": 60}],
        scatter=[])
    out = tmp_path / "map"
    result = build(write_spec(tmp_path, spec), out, "--preview")
    assert result.returncode == 0, result.stderr
    bundle = json.loads((out / "map-bundle.json").read_text(encoding="utf-8"))
    assert bundle["props"]["stump"]["anchor_px"] == [5, 11] and bundle["props"]["stump"]["origin"] == "image"
    assert bundle["props"]["berry"]["anchor_px"] == [2.5, 4] and bundle["props"]["berry"]["origin"] == "pixelspec"
    np.testing.assert_array_equal(np.asarray(Image.open(out / "props" / "stump.png").convert("RGBA")), sprite)
    solids = {solid["id"]: solid for solid in bundle["collision"]["solids"]}
    assert solids["stump-1"] == {"id": "stump-1", "shape": "rect", "x": 97, "y": 57, "w": 6, "h": 3}
    preview = np.asarray(Image.open(out / "preview.png").convert("RGBA"))
    # sprite top-left = anchor position - anchor_px = (95, 49); its opaque block is rows 2-10, columns 2-7
    np.testing.assert_array_equal(preview[51:60, 97:103, :3], np.broadcast_to((120, 80, 40), (9, 6, 3)))
    assert not np.all(preview[49:51, 97:103, :3] == (120, 80, 40))
    qa = json.loads((out / "layout-qa.json").read_text(encoding="utf-8"))
    assert any(ref["path"].endswith("stump.png") for ref in qa["inputs"])
    assert bundle["art_source"] == "mixed"


def test_fixed_object_mirroring_moves_footprint_and_interaction(tmp_path):
    spec = small_spec(props={"house": {"occlusion": "tall", "size": [40, 36],
                                       "footprint": {"shape": "rect", "width": 30, "depth": 12, "offset": [4, -6]},
                                       "interactions": [{"name": "door", "offset": [6, 2], "reach": 10}]}},
                      objects=[{"id": "h", "prop": "house", "x": 160, "y": 90, "flip_x": True}], scatter=[])
    out = tmp_path / "map"
    assert build(write_spec(tmp_path, spec), out).returncode == 0
    bundle = json.loads((out / "map-bundle.json").read_text(encoding="utf-8"))
    solid = next(s for s in bundle["collision"]["solids"] if s["id"] == "h")
    assert solid == {"id": "h", "shape": "rect", "x": 160 - 4 - 15, "y": 90 - 6 - 6, "w": 30, "h": 12}
    door = next(item for item in bundle["interactions"] if item["id"] == "h.door")
    assert (door["x"], door["y"]) == (154, 92)
