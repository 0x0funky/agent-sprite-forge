"""codeart2d layout_build: vertex-grid terrain, corner-Wang tiles, seeded scatter, collision rectangles,
reachability with the shared forge_nav rules and the map_bundle.v2 it writes (plan B21-T1; integration
decisions D2-D8).

Everything is synthetic: the meadow example's props are inline PixelSpecs, the Wang tilesets are built
here from flat quadrant colours or by autotile_build from a small material spec. An independent
re-implementation of the Appendix C validity test and of the D2 blocking set (collision solids and
rects, object footprints mirrored and scaled per D6/D7) cross-checks the tool and forge_nav instead of
trusting either one's own QA.
"""
from __future__ import annotations

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
layout_build = load_script(SKILL, "layout_build")
forge_nav = load_script(SKILL, "forge_nav")
codeart_core = load_script(SKILL, "codeart_core")


# --------------------------------------------------------------------------- independent checks

def contains(solid, xs, ys):
    """Closed point-in-solid test written independently of layout_build and forge_nav (D1 solids)."""
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


def placed_footprint(obj, prop):
    """D6/D7, written here independently: the object's (else the prop's) footprint in prop pixels,
    mirrored around the anchor for flip_x, scaled once by the object scale; None when it does not block."""
    footprint = obj.get("footprint", prop.get("footprint"))
    solid = obj.get("solid", prop.get("solid"))
    if not footprint or footprint["shape"] not in ("ellipse", "rect") or solid is False:
        return None
    assert footprint.get("basis", "prop_px") == "prop_px"
    k = obj.get("scale", 1)
    ox, oy = footprint.get("offset", [0, 0])
    rotate = footprint.get("rotate", 0)
    if obj.get("flip_x"):
        ox, rotate = -ox, -rotate
    cx, cy, width, depth = obj["x"] + k * ox, obj["y"] + k * oy, k * footprint["width"], k * footprint["depth"]
    if footprint["shape"] == "ellipse":
        return {"shape": "ellipse", "cx": cx, "cy": cy, "rx": width / 2, "ry": depth / 2, "rotate": rotate}
    assert rotate == 0, "the examples here use axis-aligned rect footprints"
    return {"shape": "rect", "x": cx - width / 2, "y": cy - depth / 2, "w": width, "h": depth}


def blockers(bundle, *, rects=True):
    """The D2 blocking set of a bundle without tilesets: collision.solids, collision.rects and the
    footprints of solid objects (nothing else is written by layout_build when it has no tileset)."""
    collision = bundle["collision"]
    found = list(collision.get("solids", []))
    if rects:
        found += [{"shape": "rect", "x": x, "y": y, "w": w, "h": h} for x, y, w, h in collision.get("rects", [])]
    for obj in bundle["objects"]:
        solid = placed_footprint(obj, bundle["props"][obj["prop"]])
        if solid is not None:
            found.append(solid)
    return found


def valid_points(xs, ys, bundle, *, rects=True):
    """Plan Appendix C: the point and 8 samples of the footprint ellipse inside the map, outside every
    blocker. The samples are the rule book's (forge_nav N2: the diagonal uses Math.SQRT1_2)."""
    collision = bundle["collision"]
    radius, squash = collision["actorRadius"], collision.get("ySquash", 1.0)
    width, height = bundle["world"]["width"], bundle["world"]["height"]
    shapes = blockers(bundle, rects=rects)
    ok = np.ones(np.shape(xs), bool)
    rx, ry, root_half = radius, radius * squash, 0.7071067811865476
    dx, dy = rx * root_half, ry * root_half
    offsets = [(0.0, 0.0), (rx, 0.0), (dx, dy), (0.0, ry), (-dx, dy), (-rx, 0.0), (-dx, -dy), (0.0, -ry), (dx, -dy)]
    for dx, dy in offsets:
        sx, sy = xs + dx, ys + dy
        ok &= (sx >= 0) & (sx <= width) & (sy >= 0) & (sy <= height)
        for solid in shapes:
            ok &= ~contains(solid, sx, sy)
    return ok


def rect_distance(x, y, rect):
    rx, ry, rw, rh = rect
    return math.hypot(max(rx - x, 0.0, x - (rx + rw)), max(ry - y, 0.0, y - (ry + rh)))


def node_lattice(bundle):
    radius = bundle["collision"]["actorRadius"]
    cell = max(1, int(math.floor(radius / 2.0 + 0.5)))
    width, height = bundle["world"]["width"], bundle["world"]["height"]
    columns, rows = max(1, math.ceil(width / cell)), max(1, math.ceil(height / cell))  # forge_nav N12
    xs, ys = np.meshgrid((np.arange(columns) + 0.5) * cell, (np.arange(rows) + 0.5) * cell)
    return cell, xs, ys


def independent_reachability(bundle, start):
    """4-neighbour components of the valid node centres (no segment rule): a superset of forge_nav's reach."""
    from scipy import ndimage

    cell, xs, ys = node_lattice(bundle)
    valid = valid_points(xs, ys, bundle)
    labels, _ = ndimage.label(valid)
    row, col = int(start[1] // cell), int(start[0] // cell)
    candidates = [(math.hypot(xs[r, c] - start[0], ys[r, c] - start[1]), r, c)
                  for r in range(row - 2, row + 3) for c in range(col - 2, col + 3)
                  if 0 <= r < valid.shape[0] and 0 <= c < valid.shape[1] and valid[r, c]]
    assert candidates, "start point has no valid node nearby"
    _, r0, c0 = min(candidates)
    return labels == labels[r0, c0], valid, xs, ys


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
                          "unknown material"),
                         (small_spec(scatter=[{"id": "huge", "kinds": {"tree": 1}, "count": 10_000_000,
                                               "spacing": 20}]), "limited to")):
        result = build(write_spec(tmp_path, spec), tmp_path / "never")
        assert result.returncode == 1 and result.stderr.startswith("error:") and needle in result.stderr
        assert result.stderr.isascii() and len(result.stderr.strip().splitlines()) == 1
        assert not (tmp_path / "never").exists()


def test_usage_errors_exit_2_and_malformed_specs_never_trace_back(tmp_path):
    """D26: argument errors are argparse usage errors (exit 2); D27: malformed input is one error line."""
    spec = write_spec(tmp_path, small_spec())
    for extra in (["--seed", "-1"], ["--seed", "x"], ["--rect-cell", "0"], ["--rect-cell", "nan"]):
        result = build(spec, tmp_path / "never", *extra)
        assert result.returncode == 2 and "usage:" in result.stderr and "error:" in result.stderr, extra
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(small_spec(scatter=[])).encode("utf-8"))
    assert build(bom, tmp_path / "bom").returncode == 0, "D28: a spec with a UTF-8 BOM is read"
    for index, text in enumerate(['[1, 2]', '{"schema": "codeart2d.layout_spec.v1", "size": "big"}',
                                  json.dumps(small_spec(props={"tree": {"footprint": [1, 2]}})),
                                  json.dumps(small_spec(objects=[{"id": "o", "prop": "tree", "x": "1", "y": 0}])),
                                  json.dumps(small_spec(spawns=[{"id": "s", "x": None, "y": 0}])), "{", "\xff"]):
        path = tmp_path / f"bad-{index}.json"
        path.write_bytes(text.encode("latin-1"))
        result = build(path, tmp_path / f"out-{index}")
        assert result.returncode == 1 and "Traceback" not in result.stderr, (index, result.stderr)
        assert result.stderr.startswith("error:") and "internal error" not in result.stderr, (index, result.stderr)


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
    reach, _, xs, ys = independent_reachability(bundle, (spawns["start"]["x"], spawns["start"]["y"]))
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


def test_forge_nav_reads_the_bundle_exactly_as_layout_build_proved_it(meadow):
    """D3/D4, the codeart review's meadow comparison rerun: layout_build's proof is forge_nav on the full
    blocking set, so a consumer reading the published bundle (map_nav, the runtime, the exporters) gets
    the same valid nodes and the same reachable nodes. The independent Appendix C test agrees node for
    node, and forge_nav never reaches more than the 4-neighbour components of the valid nodes."""
    bundle_path = meadow["a"] / "map-bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    reported = bundle["provenance"]["params"]["navigation"]
    blocking = forge_nav.read_blocking_set(bundle_path)
    assert len(blocking.rects) == len(bundle["collision"]["rects"]) and blocking.tiles == []
    assert len(blocking.footprints) == sum(1 for obj in bundle["objects"] if obj["solid"] and "footprint" in obj)
    model = blocking.model()
    grid = forge_nav.build_grid(model)
    start = next(spawn for spawn in bundle["spawns"] if spawn["id"] == reported["origin"] == "start")
    nav = forge_nav.navigate(model, [(start["x"], start["y"])], grid)
    assert int(grid.valid.sum()) == reported["valid_cells"] == bundle["qa"]["reachability"]["valid_cells"]
    assert int(nav.reachable.sum()) == reported["reachable_cells"]
    assert grid.cell == reported["cell"] == 3
    reach, valid, _, _ = independent_reachability(bundle, (start["x"], start["y"]))
    np.testing.assert_array_equal(grid.valid, valid)
    assert not np.any(nav.reachable & ~reach)
    assert reported["reachable_fraction"] >= 0.99


def test_rect_union_equals_blocked_set(meadow):
    """D3: collision.rects cover exactly the rectCell cells whose centre an exact blocker covers (terrain
    solids and object footprints), are disjoint and merged; they are exact on the vertex squares."""
    bundle = load_output(meadow["a"])["map-bundle.json"]
    collision = bundle["collision"]
    cell = collision["rectCell"]
    assert cell == bundle["tile_size"] / 2 == bundle["provenance"]["params"]["rect_cell"]
    width, height = bundle["world"]["width"], bundle["world"]["height"]
    columns, rows = math.ceil(width / cell), math.ceil(height / cell)
    xs, ys = np.meshgrid((np.arange(columns) + 0.5) * cell, (np.arange(rows) + 0.5) * cell)
    blocked = np.zeros((rows, columns), bool)
    for solid in blockers(bundle, rects=False):
        blocked |= contains(solid, xs, ys)
    cover = np.zeros((rows, columns), np.int32)
    for x, y, w, h in collision["rects"]:
        cover += (xs >= x) & (xs <= x + w) & (ys >= y) & (ys <= y + h)
    np.testing.assert_array_equal(cover > 0, blocked)
    assert cover.max() == 1, "rectangles overlap"
    assert sum(w * h for _, _, w, h in collision["rects"]) == int(blocked.sum()) * cell * cell
    assert len(collision["rects"]) < int(blocked.sum()) / 3, "rectangles were not merged"
    # on the terrain the rects add nothing: every terrain pixel they cover is a vertex-square pixel
    px, py = np.meshgrid(np.arange(width) + 0.5, np.arange(height) + 0.5)
    terrain = np.zeros((height, width), bool)
    for solid in collision["solids"]:
        terrain |= contains(solid, px, py)
    footprint = np.zeros((height, width), bool)
    for solid in blockers(bundle, rects=False)[len(collision["solids"]):]:
        footprint |= contains(solid, px, py)
    rect_px = np.zeros((height, width), bool)
    for x, y, w, h in collision["rects"]:
        rect_px |= (px >= x) & (px <= x + w) & (py >= y) & (py <= y + h)
    assert np.all(terrain <= rect_px)
    extra = rect_px & ~terrain & ~footprint
    near_footprint = layout_build.forge_core.dilate_square(footprint, int(cell))
    assert extra.any() and np.all(extra <= near_footprint), "the rects approximate the footprints, cell by cell"


@pytest.mark.parametrize("seed", range(6))
def test_raster_rects_union_is_exact(seed):
    """The rects are forge_core.merge_rects of the raster: disjoint, and their union is the raster."""
    rng = np.random.default_rng(seed)
    mask = rng.random((37, 53)) < (0.15 + 0.1 * seed)
    mask[5:20, 10:30] = True
    cell = 4.0
    rects = layout_build.raster_rects(mask, (53 * 4 - 2, 37 * 4), cell)  # the last column is clipped
    coverage = layout_build.rect_coverage(rects, mask.shape, cell)
    np.testing.assert_array_equal(coverage > 0, mask)
    assert coverage.max() <= 1
    assert all(x + w <= 53 * 4 - 2 for x, _, w, _ in rects)


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
    terrain = bundle["collision"]["solids"]
    assert terrain and all(solid["id"].startswith("terrain-") and solid["shape"] == "rect"
                           and solid["material"] == "water" for solid in terrain)
    for solid in terrain:
        actual |= contains(solid, xs, ys)
    np.testing.assert_array_equal(actual, expected)


def test_output_is_seeded(meadow):
    names = ["map-bundle.json", "terrain-vertices.json", "layout-qa.json", "codeart-meta.json", "preview.png",
             "debug.png"]
    names += sorted(f"props/{p.name}" for p in (meadow["a"] / "props").iterdir())
    for name in names:
        assert (meadow["a"] / name).read_bytes() == (meadow["b"] / name).read_bytes(), name
    first = load_output(meadow["a"])["map-bundle.json"]["objects"]
    other = load_output(meadow["c"])["map-bundle.json"]["objects"]
    assert [(o["x"], o["y"]) for o in first] != [(o["x"], o["y"]) for o in other]
    houses = [o for o in first if o["prop"] == "house"]
    assert houses == [o for o in other if o["prop"] == "house"], "fixed objects must not move with the seed"


def test_bundle_and_sidecars_validate(meadow):
    """D8: the bundle, its props registry (bundleProp, a propItem superset), the vertex grid, the spec and
    the sidecars validate against the real vendored schemas (no in-memory additions any more)."""
    out = load_output(meadow["a"])
    bundle = out["map-bundle.json"]
    assert_valid_contract(bundle, "map", "map_bundle_v2", skill=SKILL)
    for item in bundle["props"].values():
        assert_valid_contract(item, "map", "bundleProp", skill=SKILL)
        assert_valid_contract(item, "map", "propItem", skill=SKILL)
        image = meadow["a"] / item["image"]
        assert sha256_of(image) == item["sha256"]
    assert_valid_contract(out["layout-qa.json"], "common", "qaEnvelope", skill=SKILL)
    assert_valid_contract(out["codeart-meta.json"], "codeart", "codeart_meta_v1", skill=SKILL)
    assert_valid_contract(out["terrain-vertices.json"], "map", "vertex_grid_v1", skill=SKILL)
    assert_valid_contract(json.loads(MEADOW.read_text(encoding="utf-8")), "codeart", "layout_spec_v1", skill=SKILL)
    for ref in out["layout-qa.json"]["outputs"] + out["codeart-meta.json"]["outputs"]:
        assert sha256_of(meadow["a"] / ref["path"]) == ref["sha256"], ref["path"]
    assert bundle["art_source"] == "code" and bundle["placeholder"] is True  # flat-colour ground, no tileset
    assert out["codeart-meta.json"]["placeholder"] is True
    assert out["layout-qa.json"]["tool"] == {"name": "layout_build", "version": "0.4.0"}  # D29
    assert bundle["provenance"]["version"] == "0.4.0"
    assert bundle["layers"][0] == {"name": "ground", "kind": "image", "image": "ground.png",
                                   "sha256": sha256_of(meadow["a"] / "ground.png")}
    assert re.fullmatch(r"[^:]+", bundle["id"])
    assert not any(re.match(r"[A-Za-z]:|/|\\\\", str(value)) for value in path_values(bundle))


def test_prop_variety_mirroring_and_footprints(meadow):
    """D6/D7: objects carry the prop's footprint unmirrored (basis prop_px) with flip_x and scale; every
    reader mirrors and scales it once. collision.solids hold only terrain, so nothing blocks twice."""
    bundle = load_output(meadow["a"])["map-bundle.json"]
    scatter = {item["id"]: item for item in bundle["provenance"]["params"]["scatter"]}
    assert scatter["forest"]["placed"] == 70
    assert scatter["forest"]["same_look_neighbour_share"] <= layout_build.VARIETY_WARN_SHARE
    forest = [o for o in bundle["objects"] if o.get("group") == "forest"]
    assert {o["kind"] for o in forest} == {"tree", "pine"}
    assert {o["flip_x"] for o in forest} == {True, False}
    ids = {obj["id"] for obj in bundle["objects"]}
    assert not ids & {solid.get("id") for solid in bundle["collision"]["solids"]}
    for obj in bundle["objects"]:
        prop = bundle["props"][obj["prop"]]
        if prop["footprint"]["shape"] == "none":
            assert "footprint" not in obj and not obj["solid"]
            continue
        assert obj["footprint"] == prop["footprint"] and obj["footprint"]["basis"] == "prop_px"
        mine = placed_footprint(obj, prop)
        theirs = forge_nav.object_solid(obj, prop)
        for key in ("cx", "cy", "rx", "ry", "x", "y", "w", "h"):
            if key in mine:
                assert theirs[key] == pytest.approx(mine[key], abs=1e-12), (obj["id"], key)
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
    construction); full tiles get interior variants marked by a centre dot. It states no collision."""
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
                "kind": "wang_corner", "materials": list(materials), "tiles": tiles, "seamless_verified": False,
                "qa": "missing-qa.json"}
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
        assert "qa" not in manifest  # D5: the source's qa reference does not travel with the copy
        assert_valid_contract(manifest, "map", "tileset_v1", skill=SKILL)
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


def test_tiles_without_collision_get_the_vertex_square_rule_in_the_copy(tmp_path):
    """D5 fallback: a tileset that states no per-tile collision gets each non-walkable corner's quadrant
    written into the bundle's copy. The bundle then has no terrain solids, forge_nav's tile collision is
    exactly the vertex squares, and the rects are exact on them."""
    tiles = write_wang_tileset(tmp_path / "tiles", ["grass", "water"], variants=1)
    spec = small_spec(terrain=[{"id": "pond", "shape": "ellipse", "material": "water", "center": [10, 6],
                                "radius": [3.5, 2.5]}],
                      exits=[{"id": "west", "edge": "west", "span": [4, 8], "to": "a"}], scatter=[],
                      materials={"grass": {"color": "#5aa344"}, "water": {"color": "#2c62ab", "walkable": False}})
    out = tmp_path / "map"
    result = build(write_spec(tmp_path, spec), out, "--tiles", tiles, "--strict-qc")
    assert result.returncode == 0, result.stderr
    bundle = json.loads((out / "map-bundle.json").read_text(encoding="utf-8"))
    assert bundle["collision"]["solids"] == []
    report = bundle["provenance"]["params"]["tile_collision"]
    assert report == {"grass-water": {"source": "derived", "stated": 0, "derived": 16, "conflicts": []}}
    qa = json.loads((out / "layout-qa.json").read_text(encoding="utf-8"))
    assert any("state no per-tile collision" in text for text in qa["notProven"])
    manifest = json.loads((out / bundle["tilesets"][0]["manifest"]).read_text(encoding="utf-8"))
    full = next(tile for tile in manifest["tiles"] if tile["wang"] == [1, 1, 1, 1])
    assert full["collision"] == [{"shape": "rect", "x": 0, "y": 0, "w": 16, "h": 16}]
    assert full["properties"] == {"walkable": False}
    blocking = forge_nav.read_blocking_set(out / "map-bundle.json")
    grid = json.loads((out / "terrain-vertices.json").read_text(encoding="utf-8"))
    data = np.array(grid["data"])
    world_w, world_h = bundle["world"]["width"], bundle["world"]["height"]
    pixel_cols = np.floor((np.arange(world_w) + 0.5) / 16 + 0.5).astype(int)
    pixel_rows = np.floor((np.arange(world_h) + 0.5) / 16 + 0.5).astype(int)
    expected = data[np.ix_(pixel_rows, pixel_cols)] == grid["materials"].index("water")
    xs, ys = np.meshgrid(np.arange(world_w) + 0.5, np.arange(world_h) + 0.5)
    actual = np.zeros((world_h, world_w), bool)
    for solid in blocking.tiles:
        actual |= contains(solid, xs, ys)
    np.testing.assert_array_equal(actual, expected)
    assert bundle["collision"]["rectCell"] == 8 and not blocking.footprints
    rect_px = np.zeros((world_h, world_w), bool)
    for x, y, w, h in bundle["collision"]["rects"]:
        rect_px |= (xs >= x) & (xs <= x + w) & (ys >= y) & (ys <= y + h)
    np.testing.assert_array_equal(rect_px, expected)


SHORE_SPEC = {
    "schema": "codeart2d.material_spec.v1", "tile_size": 16, "variants": 1, "seed": 3,
    "materials": {
        "water": {"ramp": ["#1d3d73", "#2c62ab", "#4b98dc", "#a5dcf5"], "walkable": False,
                  "texture": {"base": 1}},
        "grass": {"ramp": ["#23502f", "#377a3b", "#5aa344", "#8cc657"], "texture": {"base": 2}},
    },
    "sets": [{"kind": "wang_corner", "materials": ["water", "grass"]}],
}


def test_autotile_collision_is_the_bundle_collision(tmp_path):
    """D5 with real autotile_build output: the placed tiles' own collision (blended shore at the set's
    collision_cell) is the terrain collision for forge_nav, the runtime and export_tiled alike. No vertex
    square is written, rectCell is the collision cell, the rects reproduce the tile collision exactly,
    and the copied manifest drops its qa fileRef while the QA file joins the provenance inputs."""
    spec_path = tmp_path / "shore.material.json"
    spec_path.write_text(json.dumps(SHORE_SPEC), encoding="utf-8")
    tiles_out = tmp_path / "tiles"
    made = run_cli([script_path(SKILL, "autotile_build"), "--material-spec", spec_path, "--output-dir", tiles_out,
                    "--preview-map", "none", "--strict-qc"], timeout=600)
    assert made.returncode == 0, made.stderr
    (manifest_path,) = sorted(tiles_out.glob("*.tileset.json"))
    source = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert source["qa"]["path"] == "autotile-qa.json"
    layout = small_spec(materials={"grass": {"color": "#5aa344"}, "water": {"color": "#2c62ab", "walkable": False}},
                        terrain=[{"id": "pond", "shape": "ellipse", "material": "water", "center": [10, 6],
                                  "radius": [4.2, 3.1], "wobble": 0.5}],
                        exits=[{"id": "west", "edge": "west", "span": [4, 8], "to": "a"},
                               {"id": "east", "edge": "east", "span": [4, 8], "to": "b"}], scatter=[],
                        spawns=[{"id": "start", "x": 30, "y": 96}])
    out = tmp_path / "map"
    result = build(write_spec(tmp_path, layout), out, "--tiles", manifest_path, "--preview", "--strict-qc")
    assert result.returncode == 0, result.stderr
    bundle = json.loads((out / "map-bundle.json").read_text(encoding="utf-8"))
    assert_valid_contract(bundle, "map", "map_bundle_v2", skill=SKILL)
    assert bundle["collision"]["solids"] == []
    report = bundle["provenance"]["params"]["tile_collision"]
    (entry,) = report.values()
    assert entry["source"] == "manifest" and entry["conflicts"] == [] and entry["derived"] == 0
    copy = json.loads((out / bundle["tilesets"][0]["manifest"]).read_text(encoding="utf-8"))
    assert "qa" not in copy and copy["tiles"] == source["tiles"]
    inputs = {Path(ref["path"]).name for ref in bundle["provenance"]["inputs"]}
    assert {"autotile-qa.json", manifest_path.name} <= inputs
    cell = bundle["collision"]["rectCell"]
    assert cell == 4  # autotile_build's collision_cell (tile // 4)
    blocking = forge_nav.read_blocking_set(out / "map-bundle.json")
    world_w, world_h = bundle["world"]["width"], bundle["world"]["height"]
    xs, ys = np.meshgrid(np.arange(world_w) + 0.5, np.arange(world_h) + 0.5)
    tile_px = np.zeros((world_h, world_w), bool)
    layer = next(layer for layer in bundle["layers"] if layer["kind"] == "tiles")
    by_index = {tile["index"]: tile for tile in copy["tiles"]}
    for (row, col), index in np.ndenumerate(np.array(layer["data"])):
        for shape in by_index[int(index)]["collision"]:
            tile_px[row * 16 + shape["y"]:row * 16 + shape["y"] + shape["h"],
                    col * 16 + shape["x"]:col * 16 + shape["x"] + shape["w"]] = True
    forge_px = np.zeros((world_h, world_w), bool)
    for solid in blocking.tiles:
        forge_px |= contains(solid, xs, ys)
    np.testing.assert_array_equal(forge_px, tile_px)
    rect_px = np.zeros((world_h, world_w), bool)
    for x, y, w, h in bundle["collision"]["rects"]:
        rect_px |= (xs >= x) & (xs <= x + w) & (ys >= y) & (ys <= y + h)
    np.testing.assert_array_equal(rect_px, tile_px)
    assert tile_px.any() and not tile_px.all()


def test_tile_collision_that_contradicts_the_layout_warns(tmp_path):
    """The tileset stays authoritative (D5), but a full water tile that does not block while the layout
    calls water non-walkable is reported as a warning."""
    tiles = write_wang_tileset(tmp_path / "tiles", ["grass", "water"], variants=1)
    manifest = json.loads(tiles.read_text(encoding="utf-8"))
    for tile in manifest["tiles"]:
        tile["collision"] = []  # states that nothing blocks, water included
    tiles.write_text(json.dumps(manifest), encoding="utf-8")
    spec = small_spec(materials={"grass": {"color": "#5aa344"}, "water": {"color": "#2c62ab", "walkable": False}},
                      terrain=[{"id": "pond", "shape": "ellipse", "material": "water", "center": [10, 6],
                                "radius": [3.5, 2.5]}],
                      exits=[{"id": "west", "edge": "west", "span": [4, 8], "to": "a"}], scatter=[])
    out = tmp_path / "map"
    assert build(write_spec(tmp_path, spec), out, "--tiles", tiles).returncode == 0
    qa = json.loads((out / "layout-qa.json").read_text(encoding="utf-8"))
    check = next(check for check in qa["checks"] if check["id"] == "tile_collision")
    assert check["status"] == "warn" and qa["status"] == "warn"
    (conflict,) = check["value"]["grass-water"]["conflicts"]
    assert "'water' is not walkable" in conflict and "blocks none" in conflict
    assert forge_nav.read_blocking_set(out / "map-bundle.json").tiles == []
    bundle = json.loads((out / "map-bundle.json").read_text(encoding="utf-8"))
    assert bundle["collision"]["rectCell"] == 8, "never coarser than the half-tile grid of a map without tiles"


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


def test_scatter_uses_the_shared_distance_field():
    """The clearance fields come from codeart_core.distance_field (the one Euclidean capped distance of
    the skill; layout_build and ambient_bake no longer carry their own copies)."""
    assert layout_build.codeart_core.distance_field is not None
    assert not any(hasattr(layout_build, name) for name in
                   ("_local_capped_edt", "_distance_field", "_run_rects", "SolidSet", "Walker", "navigate"))


def test_meadow_without_scipy_is_byte_identical(tmp_path, meadow):
    result = run_cli([SCRIPT, "--spec", MEADOW, "--output-dir", tmp_path / "noscipy", "--seed", "7", "--strict-qc"],
                     env={"FORGE_CORE_NO_SCIPY": "1"}, timeout=600)
    assert result.returncode == 0, result.stderr
    for name in ("map-bundle.json", "terrain-vertices.json"):
        assert (tmp_path / "noscipy" / name).read_bytes() == (meadow["a"] / name).read_bytes().replace(
            b'"preview": true', b'"preview": false'), name


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
    stump = next(obj for obj in bundle["objects"] if obj["id"] == "stump-1")
    assert forge_nav.object_solid(stump, bundle["props"]["stump"]) == {
        "shape": "rect", "x": 97, "y": 57, "w": 6, "h": 3, "source": "object:stump-1"}
    preview = np.asarray(Image.open(out / "preview.png").convert("RGBA"))
    # sprite top-left = anchor position - anchor_px = (95, 49); its opaque block is rows 2-10, columns 2-7
    np.testing.assert_array_equal(preview[51:60, 97:103, :3], np.broadcast_to((120, 80, 40), (9, 6, 3)))
    assert not np.all(preview[49:51, 97:103, :3] == (120, 80, 40))
    qa = json.loads((out / "layout-qa.json").read_text(encoding="utf-8"))
    assert any(ref["path"].endswith("stump.png") for ref in qa["inputs"])
    assert bundle["art_source"] == "mixed"


def test_fixed_object_mirroring_moves_footprint_and_interaction(tmp_path):
    """D6: the object stores the prop's footprint (offset [4, -6]) and flip_x; the reader mirrors it, so
    the blocking rect sits left of the anchor. The door interaction is mirrored by layout_build itself."""
    spec = small_spec(props={"house": {"occlusion": "tall", "size": [40, 36],
                                       "footprint": {"shape": "rect", "width": 30, "depth": 12, "offset": [4, -6]},
                                       "interactions": [{"name": "door", "offset": [6, 2], "reach": 10}]}},
                      objects=[{"id": "h", "prop": "house", "x": 160, "y": 90, "flip_x": True}], scatter=[])
    out = tmp_path / "map"
    assert build(write_spec(tmp_path, spec), out).returncode == 0
    bundle = json.loads((out / "map-bundle.json").read_text(encoding="utf-8"))
    house = next(obj for obj in bundle["objects"] if obj["id"] == "h")
    assert house["flip_x"] is True and house["footprint"]["offset"] == [4, -6]
    (solid,) = forge_nav.read_blocking_set(out / "map-bundle.json").footprints
    assert solid == {"shape": "rect", "x": 160 - 4 - 15, "y": 90 - 6 - 6, "w": 30, "h": 12, "source": "object:h"}
    assert placed_footprint(house, bundle["props"]["house"]) == {k: v for k, v in solid.items() if k != "source"}
    door = next(item for item in bundle["interactions"] if item["id"] == "h.door")
    assert (door["x"], door["y"]) == (154, 92)
