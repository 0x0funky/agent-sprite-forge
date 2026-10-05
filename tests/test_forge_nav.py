"""shared/forge_nav.py: the one collision and navigation rule book (integration D1-D7).

Proof that forge_nav behaves exactly like B13's map_nav.py (loaded by path, the reference):
- map_nav's own scenarios (tests/test_map_nav.py and the geometry tests of
  tests/test_map_bundle.py) run against forge_nav;
- a seeded randomized differential test compares validity, segmentClear (with and without
  the thin-gap rule), the navigation grid, BFS reachability and the target checks of both
  on many generated bundles, read from disk by map_bundle.py and by forge_nav's own reader.

forge_nav differs from map_nav in four places, and the tests pin each difference exactly:
polygon solids are closed (D1; map_nav's even-odd test left a polygon's right and bottom
edges free), footprint basis world_px is not scaled (D7), flip_x mirrors the footprint (D6),
and shapes without area block nothing wherever they come from (N4; map_nav dropped them only
from collision.solids and collision.rects).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import REPO_ROOT, load_script, load_shared
from test_map_bundle import save, write_demo_bundle

SKILL = "generate2dmap"
fn = load_shared("forge_nav")
nav = load_script(SKILL, "map_nav")  # B13's map_nav.py, loaded by path: the reference implementation
mb = nav.map_bundle  # the map_bundle module map_nav itself uses
_vendored = nav.forge_nav  # the forge_nav copy map_nav imports


class PreD1Model(_vendored.CollisionModel):
    """B13's map_nav model as built before integration, frozen here as the reference these tests pin
    forge_nav's model differences against (D1): polygon solids use the even-odd test alone (a point on
    a polygon solid's right or bottom edge was free) and shapes without area still block. Everything
    else is forge_nav's code. It lived in map_nav.py as map_nav.CollisionModel until the integration
    pass moved it here (handoff B13, section 6); map_nav.CollisionModel is now forge_nav's model."""

    def __init__(self, width: float, height: float, radius: float, y_squash: float = 1.0, regions=(),
                 solids=(), material_codes=None, material_scale: int = 1) -> None:
        super().__init__(width, height, radius, y_squash, regions, (), material_codes, material_scale)
        self.solids = [_vendored._Polygon(solid["points"]) if solid["shape"] == "polygon"  # open, even-odd only
                       else _vendored._shape(solid) for solid in solids]
        self.solid_sources = [solid.get("source", "") for solid in solids]

    @classmethod
    def from_bundle(cls, bundle) -> "PreD1Model":
        if bundle.collision is None:
            raise mb.BundleError("the bundle has no collision block (map_nav needs collision.actorRadius)")
        material = bundle.material
        return cls(bundle.width, bundle.height, bundle.collision.actor_radius, bundle.collision.y_squash,
                   bundle.collision.regions, mb.world_solids(bundle),
                   None if material is None else material.class_codes(), 1 if material is None else material.scale)


def write_bundle(folder: Path, name: str = "map-bundle.json", **fields) -> Path:
    """A file-free v2 bundle: world 160 x 100, actor radius 4, plus the given fields."""
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"schema": "generate2dmap.map_bundle.v2", "id": folder.name,
           "world": {"width": 160, "height": 100, "unit": "px"}, "layers": [],
           "collision": {"actorRadius": 4}}
    doc.update(fields)
    path = folder / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def both(path: Path):
    """(map_nav model, forge_nav model, map_bundle Bundle, forge_nav BlockingSet) of one bundle file."""
    bundle = mb.load_bundle(path)
    assert bundle.errors == [], [p.as_dict() for p in bundle.errors]
    blocking = fn.read_blocking_set(path)
    return PreD1Model.from_bundle(bundle), blocking.model(), bundle, blocking


def model_of(path: Path):
    return fn.read_blocking_set(path).model()


NO_ENTRY = "the actor's centre cannot enter the trigger from any reachable node"
ROOM_WALLS = [{"shape": "rect", "x": x, "y": y, "w": w, "h": h}
              for x, y, w, h in ((100, 20, 50, 4), (100, 76, 50, 4), (100, 20, 4, 60), (146, 20, 4, 60))]


def sampled_only_reachable(model, start: tuple[float, float]) -> np.ndarray:
    """Reachability with the sampled rule only (nodes and move midpoints), no thin-gap rule."""
    c = model.cell
    xs = (np.arange(math.ceil(model.width / c)) + 0.5) * c
    ys = (np.arange(math.ceil(model.height / c)) + 0.5) * c
    valid = model.valid_lattice(xs, ys)
    h_ok = valid[:, :-1] & valid[:, 1:] & model.valid_lattice((np.arange(xs.size - 1) + 1.0) * c, ys)
    v_ok = valid[:-1] & valid[1:] & model.valid_lattice(xs, (np.arange(ys.size - 1) + 1.0) * c)
    moves = np.zeros(valid.shape, np.uint8)
    moves[:, :-1] |= np.where(h_ok, fn.MOVE_E, 0).astype(np.uint8)
    moves[:, 1:] |= np.where(h_ok, fn.MOVE_W, 0).astype(np.uint8)
    moves[:-1] |= np.where(v_ok, fn.MOVE_S, 0).astype(np.uint8)
    moves[1:] |= np.where(v_ok, fn.MOVE_N, 0).astype(np.uint8)
    row, col = math.floor(start[1] / c), math.floor(start[0] / c)
    return fn.reachable_mask(valid, [(row, col)], moves)


def assert_moves_equal_segment_clear(model, grid, rng, count: int) -> int:
    """Every sampled grid move is open exactly when segmentClear holds between the node centres."""
    nodes = np.argwhere(grid.valid)
    checked = 0
    for r, c in nodes[rng.permutation(len(nodes))[:count]]:
        here = (grid.xs[c], grid.ys[r])
        for bit, dr, dc in ((fn.MOVE_E, 0, 1), (fn.MOVE_S, 1, 0), (fn.MOVE_W, 0, -1), (fn.MOVE_N, -1, 0)):
            rr, cc = r + dr, c + dc
            if not (0 <= rr < grid.rows and 0 <= cc < grid.cols) or not grid.valid[rr, cc]:
                assert not grid.moves[r, c] & bit
                continue
            assert bool(grid.moves[r, c] & bit) == model.segment_clear(here, (grid.xs[cc], grid.ys[rr])), (r, c, bit)
            checked += 1
    return checked


# --------------------------------------------------------------------------- map_nav's scenarios on forge_nav

def test_footprint_samples_and_cell():
    offsets = fn.footprint_offsets(5, 0.58)
    s = 0.7071067811865476
    expected = [[0, 0], [5, 0], [5 * s, 5 * 0.58 * s], [0, 5 * 0.58], [-5 * s, 5 * 0.58 * s], [-5, 0],
                [-5 * s, -5 * 0.58 * s], [0, -5 * 0.58], [5 * s, -5 * 0.58 * s]]
    assert offsets.tolist() == expected  # exact doubles: rx * SQRT1_2 and (r * ySquash) * SQRT1_2
    assert [fn.nav_cell(r) for r in (0, 1, 2.9, 3, 4, 5, 6, 7)] == [1, 1, 1, 2, 2, 3, 3, 4]
    assert (fn.nav_cell(5), fn.nav_cell(3), fn.nav_cell(1), fn.nav_cell(0)) == (3, 2, 1, 1)  # half up
    assert fn.round_half_up(2.5) == 3 and fn.round_half_up(-2.5) == -2


def test_point_validity_semantics():
    model = fn.CollisionModel(100, 60, 5, solids=[{"shape": "rect", "x": 50, "y": 0, "w": 10, "h": 60}])
    assert model.valid(5, 5) and not model.valid(4.9, 30)  # the world box is closed
    assert not model.valid(45, 30) and model.valid(44.99, 30)  # closed solid, actor size added once by sampling
    assert model.blocked(50, 30) and model.blocked(60, 30) and not model.blocked(60.01, 30)

    rotated = fn.CollisionModel(60, 60, 0, solids=[{"shape": "ellipse", "cx": 30, "cy": 30, "rx": 10, "ry": 2,
                                                    "rotate": 90}])
    assert rotated.blocked(30, 39) and not rotated.blocked(33, 30)  # long axis turned to vertical
    circle = fn.CollisionModel(60, 60, 0, solids=[{"shape": "ellipse", "cx": 40, "cy": 20, "rx": 5, "ry": 5}])
    assert circle.blocked(45, 20) and circle.blocked(40, 25) and not circle.blocked(45.000001, 20)  # <= 1

    hole = [[40, 20], [60, 20], [60, 40], [40, 40]]
    region = fn.CollisionModel(100, 60, 0, regions=[(np.array([[0, 0], [100, 0], [100, 60], [0, 60]], float),
                                                     [np.array(hole, float)])])
    assert not region.area_ok(50, 30) and region.area_ok(10, 30)
    assert region.area_ok(0, 30) and not region.area_ok(100, 30)  # even-odd crossing test: left edge in, right out
    assert region.area_ok(10, 0) and not region.area_ok(10, 60)
    assert not region.area_ok(40, 30) and region.area_ok(60, 30)  # a hole's left edge is not walkable, its right is

    wall = [{"shape": "rect", "x": 0, "y": 0, "w": 100, "h": 20}]
    squashed = fn.CollisionModel(100, 60, 10, 0.58, solids=wall)
    round_actor = fn.CollisionModel(100, 60, 10, 1.0, solids=wall)
    assert squashed.valid(50, 26) and not round_actor.valid(50, 26)  # HD-2D footprints are flatter (ry = 5.8)
    xs, ys = [50, 50], [26, 24]
    assert np.array_equal(round_actor.with_actor(10, 0.58).valid(xs, ys), squashed.valid(xs, ys))


def test_footprints_scaled_once(tmp_path):
    save(np.full((8, 8, 4), 255, np.uint8), tmp_path / "solo" / "post.png")
    props = {"post": {"image": "post.png", "anchor_px": [4, 7],
                      "footprint": {"shape": "ellipse", "width": 4, "depth": 2}}}
    objects = [{"id": "post-1", "prop": "post", "x": 50, "y": 50, "scale": 3, "anchor_px": [4, 7]}]
    path = write_bundle(tmp_path / "solo", collision={"actorRadius": 2}, props=props, objects=objects,
                        layers=[{"name": "props", "kind": "objects"}], spawns=[{"id": "start", "x": 10, "y": 10}])
    model = model_of(path)
    # world footprint rx = 3 * 4 / 2 = 6 (scaled once); the actor adds its radius 2 by sampling, not by inflation
    assert model.valid(58.01, 50) and not model.valid(57.99, 50)
    assert model.valid(50, 50 + 3 + 2 + 0.01) and not model.valid(50, 50 + 3 + 2 - 0.01)


def test_material_classes(tmp_path):
    folder = tmp_path / "cave"
    codes = np.zeros((40, 40, 4), np.uint8)
    codes[...] = (0, 0, 0, 255)  # decor
    codes[10:12, :] = (255, 255, 0, 255)  # one_way ledge
    codes[0:10, 30:40] = (128, 128, 128, 255)  # rock
    codes[30:40, 0:5] = (0, 0, 255, 255)  # deep water
    codes[30:40, 10:15] = (0, 255, 255, 255)  # shallow water, walkable
    codes[30:40, 20:25] = (255, 0, 0, 255)  # lava
    save(codes, folder / "materials.png")
    materials = {"floor": {"class": "decor", "color": "#000000"}, "ledge": {"class": "one_way", "color": "#ffff00"},
                 "rock": {"class": "solid", "color": "#808080"}, "deep": {"class": "liquid", "color": "#0000ff"},
                 "shallow": {"class": "liquid", "color": "#00ffff", "walkable": True},
                 "lava": {"class": "hazard", "color": "#ff0000"}}
    path = write_bundle(folder, world={"width": 40, "height": 40, "unit": "px"}, collision={"actorRadius": 1},
                        material_map={"image": "materials.png", "materials": materials},
                        spawns=[{"id": "top", "x": 15, "y": 4}])
    blocking = fn.read_blocking_set(path)
    model = blocking.model()
    assert blocking.blocking_material_classes == ["solid", "one_way", "liquid", "hazard"]
    assert {m["name"]: m["code"] for m in blocking.materials} == {
        "floor": fn.FREE, "ledge": fn.ONE_WAY, "rock": fn.BLOCK, "deep": fn.BLOCK, "shallow": fn.FREE, "lava": fn.BLOCK}
    assert not model.valid(35, 5) and not model.valid(2, 35) and model.valid(12, 35) and not model.valid(22, 35)
    assert model.valid(5, 20) and model.valid(15, 11)  # decor, and standing on the ledge
    assert "one_way" in model.segment_status((20, 5), (20, 20))  # dropping onto the ledge from above
    assert model.segment_clear((20, 20), (20, 5))  # jumping up through it
    assert model.segment_clear((3, 11), (16, 11))  # walking along it
    found = fn.navigate(model, [(15, 4)])
    below = (math.floor(20.5), math.floor(20.5))  # node at (20.5, 20.5)
    assert found.distance[below] == -1 and found.grid.one_way_blocked > 0
    from_below = fn.navigate(model, [(15, 25)])
    assert from_below.distance[4, 15] >= 0  # upward through the ledge is open


def test_grid_edges_equal_segment_clear():
    """Every BFS move is exactly segmentClear between the two node centres (both directions,
    one_way included), and the lattice fast path agrees with the point predicates."""
    rng = np.random.default_rng(11)
    for seed in range(4):
        width, height = 72.0, 54.0
        solids = []
        for _ in range(9):
            kind = rng.integers(3)
            x, y = rng.uniform(0, width), rng.uniform(0, height)
            if kind == 0:
                solids.append({"shape": "rect", "x": x, "y": y, "w": rng.uniform(0.3, 12), "h": rng.uniform(0.3, 12)})
            elif kind == 1:
                solids.append({"shape": "ellipse", "cx": x, "cy": y, "rx": rng.uniform(1, 8), "ry": rng.uniform(1, 8),
                               "rotate": float(rng.uniform(0, 180))})
            else:
                points = np.array([x, y]) + rng.uniform(-8, 8, (5, 2))
                solids.append({"shape": "polygon", "points": points.tolist()})
        outline = np.array([[2, 1], [70, 3], [69, 52], [36, 40], [1, 50]], float)
        hole = np.array([[30, 10], [40, 12], [34, 20]], float)
        codes = rng.choice([fn.FREE, fn.BLOCK, fn.ONE_WAY], size=(6, 8), p=[0.8, 0.1, 0.1]).astype(np.uint8)
        model = fn.CollisionModel(width, height, rng.uniform(1.5, 4.5), float(rng.choice([1.0, 0.58])),
                                  regions=[(outline, [hole])], solids=solids, material_codes=codes, material_scale=9)
        grid = fn.build_grid(model)
        nodes = np.argwhere(grid.valid)
        assert np.array_equal(grid.valid[nodes[:, 0], nodes[:, 1]],
                              model.valid(grid.xs[nodes[:, 1]], grid.ys[nodes[:, 0]]))
        assert assert_moves_equal_segment_clear(model, grid, rng, 250) > 200


def test_oversized_grid_is_refused_before_allocating():
    with pytest.raises(fn.NavError, match="split the map into chunks"):
        fn.build_grid(fn.CollisionModel(100_000, 100_000, 1))


def test_grid_bfs_public_api():
    passable = np.ones((5, 6), bool)
    passable[:4, 3] = False
    distance = fn.grid_bfs(passable, [(0, 0), (9, 9), (0, 3)])  # off-grid and blocked starts are ignored
    assert distance[0, 0] == 0 and distance[4, 3] == 4 + 3 and distance[0, 5] == 7 + 4 + 2  # round the wall
    assert np.array_equal(fn.reachable_mask(passable, [(0, 0)]), passable)
    corridor = np.ones((1, 4), bool)
    one_way = np.array([[fn.MOVE_E, fn.MOVE_E, fn.MOVE_E | fn.MOVE_N | fn.MOVE_S, fn.MOVE_W | fn.MOVE_E]], np.uint8)
    assert fn.grid_bfs(corridor, [(0, 0)], one_way).tolist() == [[0, 1, 2, 3]]
    assert fn.grid_bfs(corridor, [(0, 3)], one_way).tolist() == [[-1, -1, 1, 0]]  # off-grid bits are ignored
    assert fn.moves_from_mask(np.array([[True, True], [False, True]])).tolist() == [[1, 4 | 2], [0, 8]]
    with pytest.raises(ValueError, match="2-D"):
        fn.grid_bfs(np.ones(4, bool), [(0, 0)])


def test_thin_gap_not_jumpable(tmp_path):
    """Two walk regions 0.5 px apart: no footprint sample of any node or move midpoint lands in
    the gap (cell 3, radius 6), so sampled collision alone would join them; the thin-gap rule
    (the actor's centre path checked exactly) keeps them apart. A 0.5 px wall behaves the same."""
    regions = [{"polygon": [[0, 0], [32, 0], [32, 30], [0, 30]]},
               {"polygon": [[32.5, 0], [66, 0], [66, 30], [32.5, 30]]}]
    fields = {"world": {"width": 66, "height": 30, "unit": "px"}, "spawns": [{"id": "start", "x": 10, "y": 15}]}
    wall = {"actorRadius": 6, "solids": [{"shape": "rect", "x": 32, "y": -1, "w": 0.5, "h": 32}]}
    for name, collision in (("gap", {"actorRadius": 6, "walkRegions": regions}), ("wall", wall)):
        model = model_of(write_bundle(tmp_path / name, collision=collision, **fields))
        assert model.cell == 3
        far = (math.floor(15 / 3), math.floor(55 / 3))
        assert sampled_only_reachable(model, (10, 15))[far], "sampling alone must jump the gap for this test to matter"
        found = fn.navigate(model, [(10, 15)])
        assert found.distance[far] == -1
        assert found.reach_target((55, 15), 3).reason == "no reachable node within reach 3 px"
        assert len(found.grid.thin_gaps) == 6  # one per walkable row
        reason = model.segment_status((31.5, 16.5), (34.5, 16.5))
        assert reason is not None and "thin-gap rule" in reason
        assert model.valid(31.5, 16.5) and model.valid(33.0, 16.5) and model.valid(34.5, 16.5)
        assert model.segment_clear((31.5, 16.5), (34.5, 16.5), thin_gap=False)  # the sampled rule alone


def test_unreachable_door_fails(tmp_path):
    door = fn.Trigger(rect=(120, 45, 10, 10))
    found = fn.navigate(model_of(write_bundle(tmp_path / "town", collision={"actorRadius": 4, "solids": ROOM_WALLS})),
                        [(20, 50)])
    exit_reach, bell = found.exit_target(door, "crossing"), found.reach_target((125, 30), 6)
    assert not exit_reach.reachable and exit_reach.reason == NO_ENTRY
    assert not bell.reachable and bell.reason == "no reachable node within reach 6 px"
    opened = fn.navigate(model_of(write_bundle(tmp_path / "open", collision={"actorRadius": 4,
                                                                            "solids": ROOM_WALLS[1:]})), [(20, 50)])
    assert opened.exit_target(door, "crossing").reachable and opened.reach_target((125, 30), 6).reachable


def test_slots_approach_and_reach(tmp_path):
    solids = [{"shape": "ellipse", "cx": 80, "cy": 50, "rx": 6, "ry": 4}]
    found = fn.navigate(model_of(write_bundle(tmp_path / "plaza", collision={"actorRadius": 4, "solids": solids})),
                        [(20, 50)])
    assert found.point_target((70, 62)).reachable and found.point_target((80, 66)).reachable  # slot, approach
    assert found.reach_target((80, 50), 12).reachable and found.point_target((30, 30)).reachable
    assert found.point_target((80, 52)).reason == "the actor cannot stand here (footprint blocked)"
    assert found.reach_target((80, 50), 5).reason == "no reachable node within reach 5 px"


def test_intent_exit_reachable_where_crossing_is_not(tmp_path):
    model = model_of(write_bundle(tmp_path / "edge", world={"width": 100, "height": 60, "unit": "px"},
                                  collision={"actorRadius": 10}))
    found = fn.navigate(model, [(20, 30)])
    trigger = fn.Trigger.from_portal({"id": "edge", "rect": [96, 20, 4, 20]})
    assert found.exit_target(trigger, "crossing").reason == NO_ENTRY
    assert found.exit_target(trigger, "intent", 10).reachable
    near = found.exit_target(trigger, "intent", 8)  # the nearest valid node (x 87.5) is 8.5 px from the trigger
    assert not near.reachable and near.reason == "no reachable node within the activation radius 8 px"


def test_start_and_arrival_problems(tmp_path):
    solids = [{"shape": "rect", "x": 60, "y": 40, "w": 20, "h": 20}]
    model = model_of(write_bundle(tmp_path / "a", collision={"actorRadius": 4, "solids": solids}))
    found = fn.navigate(model, [(20, 50), (70, 45), (130, 50), (70, 50)])
    assert [(s.reachable, s.steps, s.reason) for s in found.starts] == [
        (True, 0, None), (False, None, "not a valid actor position"), (True, 0, None),
        (False, None, "not a valid actor position")]
    assert found.starts[0].node == (19.0, 49.0) and found.starts[0].cell == (24, 9)  # nearest joined node
    assert set(found.seeds) >= {found.starts[0].cell, found.starts[2].cell}
    assert fn.navigate(model, []).distance.max() == -1  # no starts: nothing is reached


def test_off_grid_target(tmp_path):
    """A corridor 0.5 px wider than the actor: (40, 14.25) is a valid position, but no grid node fits."""
    walls = [{"shape": "rect", "x": 0, "y": 0, "w": 160, "h": 10},
             {"shape": "rect", "x": 0, "y": 18.5, "w": 160, "h": 81.5}]
    model = model_of(write_bundle(tmp_path / "corridor", collision={"actorRadius": 4, "solids": walls}))
    found = fn.navigate(model, [(40, 14.25)])
    assert model.valid(40, 14.25) and not found.grid.valid.any()
    assert found.starts[0].reason == "cannot reach any grid node"
    assert found.point_target((60, 14.25)).reason.startswith("valid but off the grid (no valid node within two cells")


def test_crossing_exit_through_its_closest_point(tmp_path):
    """A trigger 0.5 px wide between node centres (x 41 and 43) is entered by a straight move."""
    found = fn.navigate(model_of(write_bundle(tmp_path / "slot")), [(20, 50)])
    target = found.exit_target(fn.Trigger(rect=(41.5, 40, 0.5, 20)), "crossing")
    assert target.reachable and target.node == (41.5, 41.0) and target.steps is not None  # nearest row first


def test_thin_one_way_strip_is_caught_by_the_centre_path():
    codes = np.zeros((40, 40), np.uint8)
    codes[10, :] = fn.ONE_WAY  # a 1 px ledge; samples 1.5 px apart can step over it
    model = fn.CollisionModel(40, 40, 6, material_codes=codes, material_scale=1)
    assert model.segment_status((20, 8.2), (20, 11.2)) == "centre path enters one_way material from above"
    assert model.segment_clear((20, 8.2), (20, 11.2), thin_gap=False)  # the sampled rule alone misses it
    assert model.segment_clear((20, 11.2), (20, 8.2))  # upward is open


def test_segments_along_region_edges():
    """Even-odd boundaries: the top edge of a region is inside, the bottom edge outside; a segment
    lying on an edge (the collinear case of the breakpoint search) follows the point rule."""
    square = (np.array([[10, 10], [30, 10], [30, 30], [10, 30]], float), [])
    point_actor = fn.CollisionModel(40, 40, 0, regions=[square])
    assert point_actor.segment_clear((12, 10), (28, 10))
    assert not point_actor.segment_clear((12, 30), (28, 30))
    breaks = point_actor.segment_breaks((5, 10), (35, 10))
    assert {0.0, 1.0} <= set(breaks.tolist()) and np.isclose(breaks, 5 / 30).any() and np.isclose(breaks, 25 / 30).any()


def test_merge_rects_is_exact():
    rng = np.random.default_rng(7)
    for shape, density in (((1, 1), 1.0), ((17, 29), 0.1), ((40, 33), 0.5), ((25, 64), 0.9), ((23, 31), 0.0)):
        mask = rng.random(shape) < density
        if shape[0] > 12 and shape[1] > 20:
            mask[5:12, 3:20] = True  # one solid block among the noise
        cover = np.zeros(shape, np.int64)
        for x, y, w, h in fn.merge_rects(mask):
            assert w > 0 and h > 0 and 0 <= x and x + w <= shape[1] and 0 <= y and y + h <= shape[0]
            cover[y:y + h, x:x + w] += 1
        assert np.array_equal(cover, mask.astype(np.int64))  # exact union, no overlap
        assert fn.merge_rects(mask) == nav.merge_rects(mask)  # the same greedy cover as map_bundle
    assert fn.merge_rects(np.ones((5, 7), bool)) == [(0, 0, 7, 5)]
    with pytest.raises(ValueError, match="2-D"):
        fn.merge_rects(np.ones(3, bool))


def test_merge_rects_is_forge_cores_cover(tmp_path):
    """D30: forge_nav.merge_rects is the sibling forge_core.merge_rects (one cover for every map
    tool, rectangle for rectangle B13's); a stale sibling without it is refused by name."""
    core = load_shared("forge_core")
    rng = np.random.default_rng(30)
    for shape, density in (((1, 1), 1.0), ((9, 40), 0.5), ((33, 17), 0.8), ((6, 6), 0.0)):
        mask = rng.random(shape) < density
        assert fn.merge_rects(mask) == core.merge_rects(mask) == nav.merge_rects(mask)
    (tmp_path / "forge_nav.py").write_bytes((REPO_ROOT / "shared" / "forge_nav.py").read_bytes())
    (tmp_path / "forge_core.py").write_text('FORGE_CORE_API_VERSION = "1"\n', encoding="utf-8")
    done = subprocess.run([sys.executable, "-c", "import forge_nav; forge_nav.merge_rects([[True]])"],
                          capture_output=True, text=True, cwd=str(tmp_path),
                          env={**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode != 0 and "NavError: merge_rects needs forge_core.merge_rects" in done.stderr


# --------------------------------------------------------------------------- the blocking set (map_bundle's tests)

def test_world_solids_scale_footprints_once(tmp_path):
    blocking = fn.read_blocking_set(write_demo_bundle(tmp_path / "map"))
    solids = {s["source"]: s for s in blocking.footprints}
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
    assert [s["source"] for s in blocking.collision_solids] == ["collision.solids[0]", "well-base"]
    assert blocking.rects == [{"shape": "rect", "x": 0, "y": 120, "w": 20, "h": 8, "source": "collision.rects[0]"}]


def test_tile_solids_merge_quadrants(tmp_path):
    path = write_demo_bundle(tmp_path / "map", hashes=False)
    tile_rects = [s for s in fn.read_blocking_set(path).tiles if s["source"] == "tiles:ground"]
    assert all(s["shape"] == "rect" for s in tile_rects) and len(tile_rects) == 1
    mask = np.zeros((128, 192), bool)
    for s in tile_rects:
        mask[s["y"]:s["y"] + s["h"], s["x"]:s["x"] + s["w"]] = True
    expected = np.zeros((128, 192), bool)
    expected[8:40, 8:56] = True  # water vertices (1..3, 1..2): +-8 px around each
    assert np.array_equal(mask, expected)
    # a tile marked walkable false with no shapes blocks its whole cell
    doc = json.loads(path.read_text(encoding="utf-8"))
    grid = np.array(doc["layers"][2]["data"])
    index = int(np.bincount(grid[grid >= 0]).argmax())
    manifest = path.parent / "tiles" / "path.tileset.json"
    tiles = json.loads(manifest.read_text(encoding="utf-8"))
    tiles["tiles"][index]["properties"] = {"walkable": False}
    manifest.write_text(json.dumps(tiles), encoding="utf-8")
    solids = [s for s in fn.read_blocking_set(path).tiles if s["source"] == "tiles:decoration"]
    assert sum(s["w"] * s["h"] for s in solids) == int((grid == index).sum()) * 16 * 16


def test_tile_collision_shapes_are_translated(tmp_path):
    root = tmp_path / "tiles"
    save(np.zeros((32, 64, 4), np.uint8), root / "atlas.png")
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
    solids = fn.blocking_set_from_document(doc, root).tiles
    assert solids == mb.world_solids(mb.bundle_from_document(doc, root / "bundle.json"))  # map_bundle's own list
    by_shape: dict = {}
    for solid in solids:
        by_shape.setdefault(solid["shape"], []).append(solid)
    assert by_shape["ellipse"] == [{"shape": "ellipse", "cx": 8, "cy": 8, "rx": 3, "ry": 2, "rotate": 45,
                                    "source": "tiles:ground"}]
    assert by_shape["polygon"][0]["points"] == [[16, 0], [32, 0], [16, 16]]  # tile 1 sits at x 16
    assert [s for s in by_shape["rect"] if s["x"] == 0.5] == [{"shape": "rect", "x": 0.5, "y": 16, "w": 4, "h": 4,
                                                               "source": "tiles:ground"}]
    assert {"shape": "rect", "x": 28, "y": 28, "w": 8, "h": 8, "source": "tiles:ground"} in by_shape["rect"]
    merged = [s for s in by_shape["rect"] if s["x"] == 28 and s["w"] == 4]  # the in-cell part, merged
    assert merged == [{"shape": "rect", "x": 28, "y": 28, "w": 4, "h": 4, "source": "tiles:ground"}]
    layer = fn.TileLayer("direct", np.array([[0, 1], [2, 3]]), 16, 16, {i: {"collision": s} for i, s in shapes.items()})
    assert [dict(s, source="tiles:ground") for s in fn.tile_solids([layer])] == solids  # the same from parsed data


def test_material_map_by_index(tmp_path):
    path = write_demo_bundle(tmp_path / "map", hashes=False)
    save(np.where(np.arange(12)[None, :] > 9, 1, 0).repeat(8, 0).astype(np.uint8), path.parent / "materials.png")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["material_map"]["materials"] = {"meadow": {"class": "decor", "index": 0},
                                        "wall": {"class": "solid", "index": 1}}
    path.write_text(json.dumps(doc), encoding="utf-8")
    codes = fn.read_blocking_set(path).material_codes
    assert codes.shape == (8, 12) and (codes[:, 10:] == fn.BLOCK).all() and (codes[:, :10] == fn.FREE).all()
    assert np.array_equal(codes, mb.load_bundle(path).material.class_codes())


def test_v1_footprint_forms():
    doc = fn.upgrade_v1_footprints({"schema": "generate2dmap.map_bundle.v1", "objects": [
        {"id": "a", "footprint": {"type": "ellipse", "cx": 2, "cy": -1, "rx": 5, "ry": 3}},
        {"id": "b", "footprint": {"x": -6, "y": -4, "w": 12, "h": 4}},
        {"id": "c", "footprint": {"shape": "rect", "width": 3, "depth": 2}}]})
    assert doc["objects"][0]["footprint"] == {"shape": "ellipse", "width": 10, "depth": 6, "offset": [2, -1]}
    assert doc["objects"][1]["footprint"] == {"shape": "rect", "width": 12, "depth": 4, "offset": [0.0, -2.0]}
    assert doc["objects"][2]["footprint"] == {"shape": "rect", "width": 3, "depth": 2}
    assert doc == mb.upgrade_v1({"schema": "generate2dmap.map_bundle.v1", "objects": [
        {"id": "a", "footprint": {"type": "ellipse", "cx": 2, "cy": -1, "rx": 5, "ry": 3}},
        {"id": "b", "footprint": {"x": -6, "y": -4, "w": 12, "h": 4}},
        {"id": "c", "footprint": {"shape": "rect", "width": 3, "depth": 2}}]})[0]


# --------------------------------------------------------------------------- the randomized differential test

def _polygon(rng, cx: float, cy: float, radius: float, count: int, integer: bool) -> list[list[float]]:
    """A star-shaped polygon with non-zero area: whole numbers in the integer family, generic doubles otherwise."""
    while True:
        angles = np.sort(rng.uniform(0, 2 * np.pi, count))
        radii = rng.uniform(0.35, 1.0, count) * radius
        points = np.stack([cx + radii * np.cos(angles), cy + radii * np.sin(angles)], axis=1)
        if integer:
            points = np.round(points)
        x, y = points[:, 0], points[:, 1]
        if abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) > 1.0:
            return [[int(a), int(b)] if integer else [float(a), float(b)] for a, b in points]


def _footprint(rng, integer: bool) -> dict:
    shape = str(rng.choice(["ellipse", "rect", "rect", "none"]))
    if shape == "none":
        return {"shape": "none"}
    footprint = {"shape": shape, "width": int(rng.integers(2, 13)) if integer else float(rng.uniform(1, 14)),
                 "depth": int(rng.integers(1, 8)) if integer else float(rng.uniform(1, 8))}
    if rng.random() < 0.6:
        footprint["offset"] = ([int(v) for v in rng.integers(-3, 4, 2)] if integer
                               else [float(v) for v in rng.uniform(-3, 3, 2)])
    if rng.random() < 0.5:
        footprint["rotate"] = int(rng.choice([0, 30, 90])) if integer else float(rng.uniform(-60, 60))
    if rng.random() < 0.4:
        footprint["basis"] = "prop_px"  # scales like map_nav's footprints (image_px: see the basis test)
    return footprint


def random_bundle(root: Path, seed: int, *, integer: bool = False, version: int = 2) -> Path:
    """A map bundle that map_bundle.py validates without errors, with every D2 blocker: walk
    regions with holes, solids (rect, rotated ellipse, polygon), collision rects, props inline
    and from a prop pack with footprints (ellipse, rect, rotated rect, offsets, scales), tile
    collision (whole-pixel and fractional rects, rects past the cell, ellipses, polygons,
    walkable false), CSV, JSON and inline tile data, and a colour or index material map with
    every class. version 1 writes the v1 forms (inline tileset, v1 footprints, no world)."""
    rng = np.random.default_rng(seed)
    root.mkdir(parents=True, exist_ok=True)
    tile = int(rng.choice([8, 16]))
    cols, rows = int(rng.integers(5, 10)), int(rng.integers(4, 8))
    width, height = cols * tile, rows * tile

    def coord(lo: float, hi: float):
        return int(rng.integers(math.floor(lo), math.floor(hi) + 1)) if integer else float(rng.uniform(lo, hi))

    collision: dict = {"actorRadius": float(rng.choice([0, 1, 1.5, 2, 3, 4, 5])) if integer
                       else float(rng.uniform(0.8, 7.5)),
                       "ySquash": float(rng.choice([1.0, 0.58, 0.75]))}
    roll = rng.random()
    if roll < 0.35:
        outline = _polygon(rng, width / 2, height / 2, 0.8 * max(width, height), 8, integer)
        holes = [_polygon(rng, coord(0.3 * width, 0.7 * width), coord(0.3 * height, 0.7 * height), 8, 5, integer)]
        regions = [{"polygon": outline, "holes": holes}]
        if rng.random() < 0.5:
            regions.append({"polygon": _polygon(rng, coord(0, width), coord(0, height), 14, 4, integer)})
        collision["walkRegions"] = regions
    elif roll < 0.6:  # two regions with a gap thinner than the sample spacing, one of them holed
        split = coord(width / 3, 2 * width / 3)
        gap = 0.5 if integer else float(rng.uniform(0.1, 0.45))
        hole = _polygon(rng, coord(split + 6, width - 6), coord(6, height - 6), 5, 4, integer)
        collision["walkRegions"] = [
            {"polygon": [[0, 0], [split, 0], [split, height], [0, height]]},
            {"polygon": [[split + gap, 0], [width, 0], [width, height], [split + gap, height]], "holes": [hole]}]
    solids = []
    for k in range(int(rng.integers(3, 9))):
        kind = str(rng.choice(["rect", "ellipse", "polygon"]))
        if kind == "rect":
            solid = {"shape": "rect", "x": coord(-4, width), "y": coord(-4, height), "w": coord(1, 14),
                     "h": coord(1, 14)}
        elif kind == "ellipse":
            solid = {"shape": "ellipse", "cx": coord(0, width), "cy": coord(0, height), "rx": coord(1, 9),
                     "ry": coord(1, 9)}
            if rng.random() < 0.5:
                solid["rotate"] = int(rng.choice([0, 45, 90])) if integer else float(rng.uniform(0, 180))
        else:
            solid = {"shape": "polygon",
                     "points": _polygon(rng, coord(0, width), coord(0, height), 9, int(rng.integers(3, 7)), integer)}
        if k == 0:
            solid["id"] = "named-solid"
        solids.append(solid)
    for _ in range(int(rng.integers(1, 4))):  # walls and slivers thinner than the sample spacing (thin-gap rule)
        thin = 0.5 if integer else float(rng.uniform(0.05, 0.45))
        x, y, length = coord(2, width - 2), coord(-2, height / 2), coord(height / 3, height + 2)
        if rng.random() < 0.5:
            solids.append({"shape": "rect", "x": x, "y": y, "w": thin, "h": length})
        else:
            square = [[x, y], [x + thin, y], [x + thin, y + length], [x, y + length]]
            skewed = [[x, y], [x + thin, y + 0.3], [x + thin, y + length], [x, y + length - 0.2]]
            solids.append({"shape": "polygon", "points": square if integer else skewed})
    collision["solids"] = solids
    collision["rects"] = [[coord(0, width), coord(0, height), coord(1, 10), coord(1, 10)]
                          for _ in range(int(rng.integers(0, 4)))]

    # tileset: 8 tiles (4 x 2) of tile x tile px
    atlas = np.zeros((2 * tile, 4 * tile, 4), np.uint8)
    atlas[..., 1:] = 200
    save(atlas, root / "tiles" / "atlas.png")
    tiles = []
    for index in range(8):  # tiles 0-3 are open floor, 4 is walkable: false, 5-7 carry collision shapes
        entry: dict = {"index": index}
        if index == 4:
            entry["properties"] = {"walkable": False}
        elif rng.random() < 0.3:
            entry["properties"] = {"walkable": index < 4 or bool(rng.random() < 0.5)}
        shapes = []
        for _ in range(int(rng.integers(1, 4)) if index >= 5 else 0):
            kind = str(rng.choice(["rect", "rect", "outside", "fraction", "ellipse", "polygon"]))
            if kind == "rect":
                x, y = int(rng.integers(0, tile - 1)), int(rng.integers(0, tile - 1))
                shapes.append({"shape": "rect", "x": x, "y": y, "w": int(rng.integers(1, tile - x + 1)),
                               "h": int(rng.integers(1, tile - y + 1))})
            elif kind == "outside":
                shapes.append({"shape": "rect", "x": tile - 2, "y": int(rng.integers(0, tile - 1)), "w": 5, "h": 3})
            elif kind == "fraction" and not integer:
                x, y = float(rng.uniform(0, tile / 2)), float(rng.uniform(0, tile / 2))
                shapes.append({"shape": "rect", "x": x, "y": y, "w": float(rng.uniform(1, tile / 2)),
                               "h": float(rng.uniform(1, tile / 2))})
            elif kind == "ellipse":
                shapes.append({"shape": "ellipse", "cx": coord(2, tile - 2), "cy": coord(2, tile - 2),
                               "rx": coord(1, tile / 3), "ry": coord(1, tile / 3)})
            elif kind == "polygon":
                shapes.append({"shape": "polygon",
                               "points": _polygon(rng, tile / 2, tile / 2, tile / 2, int(rng.integers(3, 6)), integer)})
        if shapes:
            entry["collision"] = shapes
        tiles.append(entry)
    ground = rng.choice(np.arange(-1, 8), size=(rows, cols), p=[0.25, 0.15, 0.15, 0.15, 0.14, 0.04, 0.04, 0.04, 0.04])
    (root / "layers").mkdir(exist_ok=True)
    (root / "layers" / "ground.csv").write_text("\n".join(",".join(map(str, row)) for row in ground) + "\n",
                                                encoding="utf-8")
    deco = rng.integers(5, 8, (rows, cols))
    deco[rng.random((rows, cols)) < 0.85] = -1
    deco_rows = [[None if v < 0 and rng.random() < 0.5 else int(v) for v in row] for row in deco]

    # props: inline images and a v1 prop pack (no schema key)
    save(np.full((16, 12, 4), 255, np.uint8), root / "props" / "post.png")
    save(np.full((10, 14, 4), 255, np.uint8), root / "pack" / "crate.png")
    pack_item = {"label": "crate", "display_name": "Crate", "image": "crate.png", "anchor_px": [7, 9],
                 "footprint": _footprint(rng, integer), "solid": bool(rng.random() < 0.8)}
    (root / "pack" / "prop-pack.json").write_text(json.dumps({"accepted": [pack_item], "rejected": []}),
                                                  encoding="utf-8")
    props = {"post": {"image": "props/post.png", "anchor_px": [6, 15], "footprint": _footprint(rng, integer)},
             "crate": {"pack": "pack/prop-pack.json", "label": "crate"},
             "rock": {"image": "props/post.png", "anchor_px": [6, 15], "footprint": _footprint(rng, integer),
                      "solid": True}}
    if rng.random() < 0.5:
        props["post"]["solid"] = bool(rng.random() < 0.5)
    objects = []
    for k in range(int(rng.integers(3, 8))):
        obj = {"id": f"o{k}", "prop": str(rng.choice(sorted(props))), "x": coord(0, width), "y": coord(0, height),
               "anchor_px": [6, 15]}
        if rng.random() < 0.6:
            obj["scale"] = float(rng.choice([0.5, 1, 2, 3])) if integer else float(rng.uniform(0.4, 3))
        if rng.random() < 0.25:
            obj["footprint"] = _footprint(rng, integer)
        if rng.random() < 0.2:
            obj["solid"] = bool(rng.random() < 0.5)
        if rng.random() < 0.2:
            obj["flip_x"] = False  # D6: false mirrors nothing
        objects.append(obj)

    doc: dict = {"schema": "generate2dmap.map_bundle.v2", "id": f"m{seed}", "tile_size": tile,
                 "world": {"width": width, "height": height, "unit": "px"},
                 "tilesets": [{"id": "ts", "manifest": "tiles/set.json"}],
                 "layers": [{"name": "ground", "kind": "tiles", "data": "layers/ground.csv", "tileset": "ts"},
                            {"name": "deco", "kind": "tiles", "data": deco_rows, "tileset": "ts"},
                            {"name": "props", "kind": "objects"}],
                 "props": props, "objects": objects, "collision": collision,
                 "spawns": [{"id": f"s{k}", "x": coord(0, width), "y": coord(0, height)} for k in range(3)]}
    if rng.random() < 0.5:
        (root / "layers" / "deco.json").write_text(json.dumps({"data": deco_rows}), encoding="utf-8")
        doc["layers"][1]["data"] = "layers/deco.json"
    manifest = {"schema": "generate2dmap.tileset.v1", "image": "atlas.png", "tile_size": tile, "columns": 4,
                "kind": "flat", "materials": ["stone"], "tiles": tiles, "seamless_verified": False}
    (root / "tiles" / "set.json").write_text(json.dumps(manifest), encoding="utf-8")

    if rng.random() < 0.7:
        scale = int(rng.choice([1, 1, 2, tile // 2, tile]))
        shape = (height // scale, width // scale)
        names = ["floor", "rock", "deep", "shallow", "lava", "warm", "ledge"]
        classes = ["decor", "solid", "liquid", "liquid", "hazard", "hazard", "one_way"]
        walkable = [None, None, False, True, None, True, None]
        weights = np.array([24, 1, 1, 2, 1, 2, 2], float)
        index = rng.choice(len(names), size=shape, p=weights / weights.sum())
        for _ in range(int(rng.integers(0, 3))):  # one-pixel ledges and walls: thin at scale 1
            line = int(rng.integers(0, shape[0]))
            index[line, int(rng.integers(0, shape[1] // 2)):] = int(rng.choice([6, 6, 1]))
        materials = {}
        if rng.random() < 0.5:  # colour image, some transparent pixels without material
            colours = [(0, 0, 0), (128, 128, 128), (0, 0, 255), (0, 255, 255), (255, 0, 0), (255, 128, 0),
                       (255, 255, 0)]
            image = np.zeros(shape + (4,), np.uint8)
            image[..., :3] = np.array(colours, np.uint8)[index]
            image[..., 3] = np.where(rng.random(shape) < 0.1, 0, 255)
            save(image, root / "materials.png")
            for k, name in enumerate(names):
                materials[name] = {"class": classes[k], "color": "#%02x%02x%02x" % colours[k]}
        else:  # a grey image matched by index
            save(index.astype(np.uint8), root / "materials.png")
            for k, name in enumerate(names):
                materials[name] = {"class": classes[k], "index": k}
        for k, name in enumerate(names):
            if walkable[k] is not None:
                materials[name]["walkable"] = walkable[k]
        doc["material_map"] = {"image": "materials.png", "materials": materials}

    if version == 1:
        doc["schema"] = "generate2dmap.map_bundle.v1"
        del doc["world"], doc["props"], doc["id"]
        doc["tilesets"] = [{"id": "ts", "image": "tiles/atlas.png", "tiles": tiles, "columns": 4}]
        doc["layers"] = [{"name": "ground", "kind": "tiles", "data": "layers/ground.csv"},
                         {"name": "props", "kind": "objects"}]
        for obj in doc["objects"]:
            obj.pop("footprint", None)
            obj.pop("solid", None)
            roll = rng.random()
            if roll < 0.35:
                obj["footprint"] = {"type": "ellipse", "rx": coord(1, 6), "ry": coord(1, 4), "cx": coord(-2, 2)}
            elif roll < 0.7:
                obj["footprint"] = {"x": coord(-6, 0), "y": coord(-4, 0), "w": coord(2, 10), "h": coord(1, 6)}
    path = root / "map-bundle.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def polygon_edge_hits(blocking, xs, ys) -> np.ndarray:
    """Points on an edge of a polygon solid: the only place D1's closed polygons differ from map_nav."""
    hit = np.zeros(np.broadcast_shapes(np.shape(xs), np.shape(ys)), bool)
    for solid in blocking.solids:
        if solid["shape"] == "polygon":
            hit |= fn.on_polygon_edge(xs, ys, np.asarray(solid["points"], float))
    return hit


def probe_points(rng, blocking, cell: int) -> tuple[np.ndarray, np.ndarray]:
    """Random points plus every kind of boundary point: rect sides and corners, ellipse extremes,
    polygon vertices and edge midpoints, the world box, material pixel corners and grid lattices."""
    width, height = blocking.width, blocking.height
    points = [rng.uniform([-4, -4], [width + 4, height + 4], (600, 2))]
    for solid in blocking.solids:
        if solid["shape"] == "rect":
            x, y, w, h = solid["x"], solid["y"], solid["w"], solid["h"]
            points.append([[x, y], [x + w, y + h], [x + w / 2, y], [x + w / 2, y + h], [x, y + h / 2],
                           [x + w, y + h / 2], [x + w, y], [x, y + h]])
        elif solid["shape"] == "ellipse":
            cx, cy, rx, ry = solid["cx"], solid["cy"], solid["rx"], solid["ry"]
            points.append([[cx + rx, cy], [cx - rx, cy], [cx, cy + ry], [cx, cy - ry], [cx, cy]])
        else:
            vertices = np.asarray(solid["points"], float)
            points += [vertices, (vertices + np.roll(vertices, 1, axis=0)) / 2]
    for polygon, holes in blocking.regions:
        for ring in (polygon, *holes):
            points += [ring, (ring + np.roll(ring, 1, axis=0)) / 2]
    edge = rng.uniform(0, 1, 40)
    points.append(np.stack([np.zeros(40), edge * height], 1))
    points.append(np.stack([np.full(40, width), edge * height], 1))
    points.append(np.stack([edge * width, np.full(40, height)], 1))
    if blocking.material_codes is not None:
        s = blocking.material_scale
        mx = rng.integers(0, blocking.material_codes.shape[1] + 1, 60) * s
        my = rng.integers(0, blocking.material_codes.shape[0] + 1, 60) * s
        points.append(np.stack([mx, my], 1))
    half = rng.integers(0, 2 * math.ceil(max(width, height) / cell) + 2, (300, 2)) * (cell / 2)
    points.append(half)
    every = np.concatenate([np.asarray(p, float).reshape(-1, 2) for p in points])
    return every[:, 0], every[:, 1]


def assert_points_agree(m_ref, m_new, blocking, xs, ys) -> int:
    """forge_nav equals map_nav on every point, except that a point on a polygon solid's edge is
    blocked (D1); returns how many probes touched such an edge."""
    hits = polygon_edge_hits(blocking, xs, ys)
    assert np.array_equal(m_new.blocked(xs, ys), m_ref.blocked(xs, ys) | hits)
    assert np.array_equal(m_new.area_ok(xs, ys), m_ref.area_ok(xs, ys))
    assert np.array_equal(m_new.one_way_bits(xs, ys), m_ref.one_way_bits(xs, ys))
    sx, sy = m_new._samples(xs, ys)
    sample_hits = polygon_edge_hits(blocking, sx, sy).any(axis=-1)
    assert np.array_equal(m_new.valid(xs, ys), m_ref.valid(xs, ys) & ~sample_hits)
    return int(hits.sum() + sample_hits.sum())


def random_segments(rng, model, count: int) -> list[tuple[tuple, tuple]]:
    """Segments that reach the interesting rules: mostly between valid positions (near and far,
    any direction, axis-aligned) so the one_way and thin-gap rules decide, plus grid moves
    between node centres, zero-length segments and arbitrary ones."""
    width, height, cell = model.width, model.height, model.cell
    candidates = rng.uniform([0, 0], [width, height], (4000, 2))
    valid = candidates[model.valid(candidates[:, 0], candidates[:, 1])]
    pool = valid if len(valid) >= 2 else candidates

    def pick() -> tuple[float, float]:
        x, y = pool[int(rng.integers(len(pool)))]
        return float(x), float(y)

    segments = []
    for k in range(count):
        mode = k % 6
        a = pick()
        if mode in (0, 1):  # valid to valid, anywhere
            b = pick()
        elif mode == 2:  # short, any direction
            angle, length = rng.uniform(0, 2 * np.pi), rng.uniform(0, 4 * cell)
            b = (a[0] + float(length * np.cos(angle)), a[1] + float(length * np.sin(angle)))
        elif mode == 3:  # axis-aligned
            b = (a[0], float(rng.uniform(0, height))) if rng.random() < 0.5 else (float(rng.uniform(0, width)), a[1])
        elif mode == 4:  # a grid move between node centres, or a zero-length segment
            col, row = math.floor(a[0] / cell), math.floor(a[1] / cell)
            a = ((col + 0.5) * cell, (row + 0.5) * cell)
            step = int(rng.choice([-1, 1])) * cell
            b = a if rng.random() < 0.1 else (a[0] + step, a[1]) if rng.random() < 0.5 else (a[0], a[1] + step)
        else:  # anywhere at all
            a = (float(rng.uniform(-2, width + 2)), float(rng.uniform(-2, height + 2)))
            b = (float(rng.uniform(-2, width + 2)), float(rng.uniform(-2, height + 2)))
        segments.append((a, b))
    return segments


def compare_reachability(rng, m_ref, m_new, bundle) -> None:
    """Starts, BFS distances and every kind of target check equal map_nav's (map_nav.check_bundle's steps)."""
    g_ref = nav.build_grid(m_ref)
    g_new = fn.build_grid(m_new)
    assert np.array_equal(g_ref.valid, g_new.valid) and np.array_equal(g_ref.moves, g_new.moves)
    assert g_ref.thin_gaps == g_new.thin_gaps and g_ref.one_way_blocked == g_new.one_way_blocked
    starts = list(bundle.spawns.values())
    valid_nodes = np.argwhere(g_new.valid)
    for r, c in valid_nodes[rng.permutation(len(valid_nodes))[:2]]:
        starts.append((float(g_new.xs[c] + rng.uniform(-1, 1)), float(g_new.ys[r] + rng.uniform(-1, 1))))
    seeds = []
    for point in starts:
        seeds.extend(nav.attach(m_ref, g_ref, point))
    distance = nav.grid_bfs(g_ref.valid, seeds, g_ref.moves)
    found = fn.navigate(m_new, starts, g_new)
    assert np.array_equal(distance, found.distance) and found.seeds == seeds

    def same(target, reach) -> None:
        assert (target.reachable, target.steps, target.node, target.reason) == \
            (reach.reachable, reach.steps, reach.node, reach.reason or None), target

    width, height = m_new.width, m_new.height
    candidates = rng.uniform([0, 0], [width, height], (2000, 2))
    standable = candidates[m_ref.valid(candidates[:, 0], candidates[:, 1])]
    for k in range(14):
        if k % 2 and len(standable):  # half the targets where the actor can stand
            point = tuple(float(v) for v in standable[int(rng.integers(len(standable)))])
        else:
            point = (float(rng.uniform(0, width)), float(rng.uniform(0, height)))
        target = nav.Target("slot", "t", point)
        nav.check_point_target(m_ref, g_ref, distance, target)
        same(target, found.point_target(point))
        reach = float(rng.uniform(0, 3 * m_new.cell + 4))
        target = nav.Target("interaction", "t", point)
        nav.check_reach_target(g_ref, distance, target, reach)
        same(target, found.reach_target(point, reach))
    for k in range(10):
        x, y = float(rng.uniform(0, width)), float(rng.uniform(0, height))
        if k % 2:
            rect, circle = (x, y, float(rng.uniform(0.2, 12)), float(rng.uniform(0.2, 12))), None
        else:
            rect, circle = None, (x, y, float(rng.uniform(0.5, 8)))
        activation, radius = ("intent", float(rng.uniform(0, 8))) if k % 3 == 0 else ("crossing", 0.0)
        portal = mb.Portal(id="p", rect=rect, circle=circle, to_map="m", to_target=None, activation=activation,
                           travel=(1.0, 0.0), radius=radius, entrances={}, entrance_refs={}, latch=True,
                           requires_movement=True, reciprocal=True)
        target = nav.Target("exit", "p", (0.0, 0.0))
        nav.check_exit(m_ref, g_ref, distance, portal, target)
        same(target, found.exit_target(fn.Trigger(rect=rect, circle=circle), activation, radius))


def _check_parity(tmp_path: Path, seed: int, integer: bool, version: int = 2) -> int:
    rng = np.random.default_rng(1000 + seed)
    path = random_bundle(tmp_path / f"b{seed}-{int(integer)}-{version}", seed, integer=integer, version=version)
    m_ref, m_new, bundle, blocking = both(path)
    assert blocking.solids == mb.world_solids(bundle)  # the D2 builder: the same shapes, in map_nav's order
    assert (blocking.width, blocking.height) == (bundle.width, bundle.height)
    assert (m_new.radius, m_new.y_squash, m_new.cell) == (m_ref.radius, m_ref.y_squash, m_ref.cell)
    if bundle.material is None:
        assert blocking.material_codes is None
    else:
        assert np.array_equal(blocking.material_codes, bundle.material.class_codes())
        assert blocking.material_scale == bundle.material.scale
    xs, ys = probe_points(rng, blocking, m_new.cell)
    touched = assert_points_agree(m_ref, m_new, blocking, xs, ys)
    if integer:
        grid = fn.build_grid(m_new)  # D1's closed polygons keep grid moves equal to segmentClear
        assert_moves_equal_segment_clear(m_new, grid, rng, 90)
        return touched
    for a, b in random_segments(rng, m_ref, 100):
        for thin in (True, False):
            assert m_ref.segment_status(a, b, thin_gap=thin) == m_new.segment_status(a, b, thin_gap=thin), (a, b, thin)
    compare_reachability(rng, m_ref, m_new, bundle)
    return touched


@pytest.mark.parametrize("seed", range(20))
def test_differential_generic_bundles(tmp_path, seed):
    """Generic doubles: no probe or sample lands exactly on a polygon edge, so forge_nav must equal
    map_nav bit for bit: points, segments (both rules), the grid, BFS and every target check."""
    _check_parity(tmp_path, seed, integer=False)


def test_differential_whole_number_bundles(tmp_path):
    """Whole-number geometry puts many probes exactly on boundaries: rects, ellipses, regions,
    holes, the world box and material pixels agree exactly; points on a polygon solid's edge are
    blocked only by forge_nav (D1), and that difference is exercised."""
    touched = sum(_check_parity(tmp_path, seed, integer=True) for seed in range(100, 112))
    assert touched > 50


@pytest.mark.parametrize("seed", range(200, 204))
def test_differential_v1_bundles(tmp_path, seed):
    """map_bundle.v1 documents (inline tileset, v1 footprint forms, world inferred from the tiles)."""
    _check_parity(tmp_path, seed, integer=False, version=1)


def test_vendored_copies_behave_like_the_canonical(tmp_path):
    path = write_demo_bundle(tmp_path / "map")
    expected = fn.read_blocking_set(path)
    for skill in ("generate2dmap", "codeart2d"):
        copy = load_script(skill, "forge_nav")
        assert copy.FORGE_NAV_API_VERSION == fn.FORGE_NAV_API_VERSION
        blocking = copy.read_blocking_set(path)
        assert blocking.solids == expected.solids and np.array_equal(blocking.material_codes, expected.material_codes)
        assert np.array_equal(copy.build_grid(blocking.model()).moves, fn.build_grid(expected.model()).moves)
    assert (REPO_ROOT / "skills" / "generate2dmap" / "scripts" / "forge_nav.py").read_bytes().replace(b"\r\n", b"\n") \
        == (REPO_ROOT / "shared" / "forge_nav.py").read_bytes().replace(b"\r\n", b"\n")


# --------------------------------------------------------------------------- D1: closed polygon solids

def test_polygon_solids_are_closed():
    """D1: a point on any edge of a polygon solid is blocked; map_nav's even-odd test left the
    right and bottom edges free. Walk regions keep the even-odd rule (no seams between regions)."""
    square = [[10, 10], [30, 10], [30, 30], [10, 30]]
    new = fn.CollisionModel(40, 40, 0, solids=[{"shape": "polygon", "points": square}])
    old = PreD1Model(40, 40, 0, solids=[{"shape": "polygon", "points": square}])
    edges = ([10, 20], [30, 20], [20, 10], [20, 30], [10, 10], [30, 30], [30, 10], [10, 30])
    xs, ys = np.array(edges, float).T
    assert new.blocked(xs, ys).all()
    assert old.blocked(xs, ys).tolist() == [True, False, True, False, True, False, False, False]
    assert not new.blocked(30.000001, 20) and not new.blocked(20, 9.999999) and new.blocked(29.999999, 29.999999)
    slanted = [[0, 0], [8, 4], [0, 8]]  # a point exactly on the slanted edge (4, 2) counts too
    assert fn.CollisionModel(10, 10, 0, solids=[{"shape": "polygon", "points": slanted}]).blocked(4, 2)
    rect_polygon = fn.CollisionModel(40, 40, 0, solids=[{"shape": "rect", "x": 10, "y": 10, "w": 20, "h": 20}])
    assert np.array_equal(rect_polygon.blocked(xs, ys), new.blocked(xs, ys))  # the same closed set as the rect
    region = fn.CollisionModel(40, 40, 0, regions=[(np.array(square, float), [])])
    assert region.area_ok(xs, ys).tolist() == [True, False, True, False, True, False, False, False]


def test_shapes_without_area_block_nothing():
    """N4: zero-size rects and ellipses and zero-area polygons block nothing, wherever they come
    from; map_nav dropped them only from collision.solids and rects, so a zero-width footprint
    or tile rect blocked the line it spans (and closed polygons would block a flat polygon's edges)."""
    line_rect = {"shape": "rect", "x": 10, "y": 0, "w": 0, "h": 20}
    flat = {"shape": "polygon", "points": [[0, 5], [10, 5], [20, 5]]}
    dot = {"shape": "ellipse", "cx": 5, "cy": 5, "rx": 0, "ry": 3}
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # and no division by a zero radius
        model = fn.CollisionModel(20, 20, 0, solids=[line_rect, flat, dot])
        assert model.solids == [] and not model.blocked([10, 5, 5, 15], [10, 5, 6, 5]).any()
    assert PreD1Model(20, 20, 0, solids=[line_rect]).blocked(10, 10)  # map_nav blocked the line
    assert fn.footprint_solid(10, 10, {"shape": "rect", "width": 0, "depth": 4}) is None
    assert fn.footprint_solid(10, 10, {"shape": "ellipse", "width": 3, "depth": 0}, scale=2) is None
    layer = fn.TileLayer("g", np.array([[0, 0]]), 16, 16, {0: {"collision": [
        {"shape": "rect", "x": 0.5, "y": 0, "w": 0, "h": 4}, {"shape": "rect", "x": 2, "y": 2, "w": 0, "h": 3},
        {"shape": "polygon", "points": [[0, 0], [8, 8], [16, 16]]},
        {"shape": "ellipse", "cx": 4, "cy": 4, "rx": 2, "ry": 0}]}})
    assert fn.tile_solids([layer]) == []


def test_a_tangent_ellipse_is_one_touching_point():
    """N10, found by the integration pass on layout_build's meadow example: a footprint ellipse whose top
    touches a grid row exactly (cy - ry = 58.5). The quadratic's discriminant is rounding noise (+3.9e-16 one
    way, -4.4e-16 the other), which made segmentClear block the move west to east but not east to west, and
    differ from the grid. A tangent is a single touching point in both directions, in segment_clear and in
    build_grid alike, while a line 1e-6 px inside the ellipse still crosses it."""
    tangent = {"shape": "ellipse", "cx": 8.0, "cy": 60.25, "rx": 3.0, "ry": 1.75, "rotate": 0.0}
    a, b = (7.5, 58.5), (10.5, 58.5)
    for model in (fn.CollisionModel(40, 80, 5, solids=[tangent]), PreD1Model(40, 80, 5, solids=[tangent])):
        assert model.cell == 3 and model.valid(*a) and model.valid(*b)
        assert model.segment_clear(a, b) and model.segment_clear(b, a)
    grid = fn.build_grid(fn.CollisionModel(40, 80, 5, solids=[tangent]))
    assert (grid.xs[2], grid.xs[3], grid.ys[19]) == (7.5, 10.5, 58.5)
    assert grid.moves[19, 2] & fn.MOVE_E and grid.moves[19, 3] & fn.MOVE_W
    inside = fn.CollisionModel(40, 80, 5, solids=[{**tangent, "cy": 60.25 - 1e-6}])  # a real chord of ~5e-3 px
    assert not inside.segment_clear(a, b) and not inside.segment_clear(b, a)
    assert "thin-gap rule" in inside.segment_status(a, b)
    inside_grid = fn.build_grid(inside)
    assert not inside_grid.moves[19, 2] & fn.MOVE_E and not inside_grid.moves[19, 3] & fn.MOVE_W


@pytest.mark.parametrize("axis", ["h", "v"])
def test_closed_polygon_edges_on_grid_lines_block_moves(axis):
    """A polygon solid whose edge lies on a node row (or column) between two node centres: neither
    node nor the move's midpoint touches it, but the centre path runs along the edge, so the
    thin-gap rule closes the move, in segment_status and in the grid alike."""
    flat = [[3.6, 1.0], [3.9, 1.0], [3.9, 2.5], [3.6, 2.5]]  # bottom edge on y = 2.5, x 3.6 .. 3.9
    points = flat if axis == "h" else [[y, x] for x, y in flat]
    model = fn.CollisionModel(8, 8, 0, solids=[{"shape": "polygon", "points": points}])
    a, b = ((3.5, 2.5), (4.5, 2.5)) if axis == "h" else ((2.5, 3.5), (2.5, 4.5))
    assert model.valid(*a) and model.valid(*b) and model.valid((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    assert model.segment_clear(a, b, thin_gap=False)
    assert "thin-gap rule" in model.segment_status(a, b)
    reference = PreD1Model(8, 8, 0, solids=[{"shape": "polygon", "points": points}])
    assert reference.segment_clear(a, b)  # map_nav: the bottom edge was outside the polygon
    grid = fn.build_grid(model)
    row, col = (2, 3) if axis == "h" else (3, 2)
    bit = fn.MOVE_E if axis == "h" else fn.MOVE_S
    assert grid.valid[row, col] and not grid.moves[row, col] & bit
    assert assert_moves_equal_segment_clear(model, grid, np.random.default_rng(3), 64) > 100


TOUCHING_VERTICES = [  # (kind, triangle, move): the first vertex lies on the move's node line; each broke the old rule
    ("solid", [[10.5, 6.75], [10.81, 6.29], [10.74, 7.2]], ((10.5, 6.5), (10.5, 7.5))),
    ("solid", [[16.25, 9.5], [15.41, 9.81], [16.61, 9.77]], ((15.5, 9.5), (16.5, 9.5))),
    ("solid", [[20.5, 24.25], [20.83, 23.43], [20.85, 24.6]], ((20.5, 23.5), (20.5, 24.5))),
    ("hole", [[30.5, 6.25], [30.66, 5.42], [30.85, 6.9]], ((30.5, 5.5), (30.5, 6.5))),
    ("hole", [[40.5, 25.75], [40.57, 25.27], [40.6, 25.95]], ((40.5, 25.5), (40.5, 26.5))),
]


def test_a_vertex_on_a_node_line_is_one_touching_point(tmp_path):
    """N10 (review r1, finding 1): the two edges at a vertex cut a segment through it 1 ulp apart; the sliver's
    midpoint rounded onto the closed vertex, so segment_status blocked a move the grid's fast path opened, and
    map_nav check proved a corridor the runtime could not walk. A piece of 1e-9 px or less is skipped: the touch
    is open in segment_status, the grid and map_nav alike, while a vertex 1e-6 px across the line still blocks."""
    path = write_bundle(tmp_path / "vertex", world={"width": 3, "height": 10, "unit": "px"},
                        collision={"actorRadius": 0, "rects": [[0, 0, 1, 10], [2, 0, 1, 10]],
                                   "solids": [{"shape": "polygon", "points": [[1.5, 4.75], [1.35, 4.1], [1.26, 5.29]]}]},
                        spawns=[{"id": "start", "x": 1.5, "y": 0.5}], interactions=[{"id": "chest", "x": 1.5, "y": 9.5}])
    model = model_of(path)
    cuts = model.segment_breaks((1.5, 4.5), (1.5, 5.5))
    assert len(cuts) == 4 and 0 < cuts[2] - cuts[1] <= 1e-9  # the vertex's two cuts, 1 ulp apart
    assert model.segment_status((1.5, 4.5), (1.5, 5.5)) is None and model.segment_clear((1.5, 5.5), (1.5, 4.5))
    grid = fn.build_grid(model)
    assert grid.moves[4, 1] & fn.MOVE_S and grid.moves[5, 1] & fn.MOVE_N
    assert fn.navigate(model, [(1.5, 0.5)], grid).point_target((1.5, 9.5)).reachable
    result = nav.check_bundle(mb.load_bundle(path))
    assert result.status == "pass" and all(target.reachable for target in result.targets)
    for kind, triangle, (a, b) in TOUCHING_VERTICES:
        outline = np.array([[0, 0], [48, 0], [48, 32], [0, 32]], float)
        model = (fn.CollisionModel(48, 32, 0, solids=[{"shape": "polygon", "points": triangle}]) if kind == "solid"
                 else fn.CollisionModel(48, 32, 0, regions=[(outline, [np.array(triangle, float)])]))
        assert model.segment_status(a, b) is None and model.segment_clear(b, a), (kind, triangle)
        grid = fn.build_grid(model)
        (row, col), bit = grid.node_of(*a), fn.MOVE_S if a[0] == b[0] else fn.MOVE_E
        assert grid.moves[row, col] & bit, (kind, triangle)
        assert assert_moves_equal_segment_clear(model, grid, np.random.default_rng(7), 400) > 1000
        poke = [[triangle[0][0] + (0.0 if a[0] != b[0] else 1e-6 * (1 if triangle[1][0] < a[0] else -1)),
                 triangle[0][1] + (0.0 if a[0] == b[0] else 1e-6 * (1 if triangle[1][1] < a[1] else -1))],
                *triangle[1:]]  # the vertex 1e-6 px across the line: a real crossing of the centre path
        crossing = (fn.CollisionModel(48, 32, 0, solids=[{"shape": "polygon", "points": poke}]) if kind == "solid"
                    else fn.CollisionModel(48, 32, 0, regions=[(outline, [np.array(poke, float)])]))
        assert not crossing.segment_clear(a, b), (kind, poke)
        assert not fn.build_grid(crossing).moves[row, col] & bit, (kind, poke)


def test_whole_number_polygons_keep_grid_moves_equal_to_segment_clear():
    rng = np.random.default_rng(29)
    for _ in range(4):
        solids = [{"shape": "polygon", "points": _polygon(rng, rng.integers(4, 36), rng.integers(4, 28), 7,
                                                          int(rng.integers(3, 7)), True)} for _ in range(6)]
        solids.append({"shape": "polygon", "points": [[10, 7], [20, 7], [20, 9], [10, 9]]})  # edges on node lines
        model = fn.CollisionModel(40, 32, float(rng.choice([0, 1, 2])), solids=solids)
        grid = fn.build_grid(model)
        assert assert_moves_equal_segment_clear(model, grid, rng, 260) > 200


# --------------------------------------------------------------------------- D6 flip_x and D7 basis

def _with_object(tmp_path: Path, name: str, obj_fields: dict, footprint: dict) -> Path:
    save(np.full((16, 12, 4), 255, np.uint8), tmp_path / name / "post.png")
    props = {"post": {"image": "post.png", "anchor_px": [6, 15], "footprint": footprint, "solid": True}}
    obj = {"id": "p1", "prop": "post", "x": 60.25, "y": 40.5, "anchor_px": [6, 15], **obj_fields}
    return write_bundle(tmp_path / name, props=props, objects=[obj], layers=[{"name": "props", "kind": "objects"}])


@pytest.mark.parametrize("footprint", [
    {"shape": "ellipse", "width": 9, "depth": 4, "offset": [3.5, -1.25], "rotate": 20},
    {"shape": "rect", "width": 9, "depth": 4, "offset": [3.5, -1.25]},
    {"shape": "rect", "width": 9, "depth": 4, "offset": [-2, 1], "rotate": -35},
], ids=["rotated-ellipse", "rect", "rotated-rect"])
def test_flip_x_mirrors_the_footprint_around_the_anchor(tmp_path, footprint):
    """D6: flip_x negates the footprint's x offset and its rotation before scaling, which is the
    footprint map_nav builds from a pre-mirrored prop (oracle), and mirrors the shape about x."""
    flipped = fn.read_blocking_set(_with_object(tmp_path, "flip", {"scale": 2, "flip_x": True}, footprint))
    mirrored = dict(footprint, offset=[-footprint["offset"][0], footprint["offset"][1]])
    if "rotate" in footprint:
        mirrored["rotate"] = -footprint["rotate"]
    oracle = mb.world_solids(mb.load_bundle(_with_object(tmp_path, "oracle", {"scale": 2}, mirrored)))
    assert flipped.footprints == oracle
    plain = fn.read_blocking_set(_with_object(tmp_path, "plain", {"scale": 2, "flip_x": False}, footprint))
    assert plain.footprints == mb.world_solids(mb.load_bundle(_with_object(tmp_path, "ref", {"scale": 2}, footprint)))
    ys = np.linspace(30, 50, 81)
    for dx in np.linspace(0.25, 14, 56):  # the blocked set is the mirror image about x = 60.25
        left = flipped.model().blocked(np.full(81, 60.25 - dx), ys)
        right = plain.model().blocked(np.full(81, 60.25 + dx), ys)
        assert np.array_equal(left, right) or footprint.get("rotate"), dx  # exact unless sin/cos round apart
        assert (left != right).sum() <= 2, dx


def test_footprint_basis_world_px_is_not_scaled(tmp_path):
    """D7: a world_px footprint keeps its size and offset at any instance scale (map_nav's footprint
    at scale 1 is the oracle); prop_px and the legacy image_px are scaled once; others are refused."""
    footprint = {"shape": "rect", "width": 9, "depth": 4, "offset": [3, -1], "rotate": 15}
    world = fn.read_blocking_set(_with_object(tmp_path, "world", {"scale": 3},
                                              dict(footprint, basis="world_px")))
    oracle = mb.world_solids(mb.load_bundle(_with_object(tmp_path, "oracle", {"scale": 1}, footprint)))
    assert world.footprints == oracle
    scaled = mb.world_solids(mb.load_bundle(_with_object(tmp_path, "scaled", {"scale": 3}, footprint)))
    for basis in ("prop_px", "image_px"):
        assert fn.read_blocking_set(_with_object(tmp_path, basis, {"scale": 3},
                                                 dict(footprint, basis=basis))).footprints == scaled
    assert fn.footprint_solid(10, 20, {"shape": "ellipse", "width": 4, "depth": 2, "basis": "world_px"},
                              scale=5, source="x") == {"shape": "ellipse", "cx": 10, "cy": 20, "rx": 2.0, "ry": 1.0,
                                                       "rotate": 0.0, "source": "x"}
    with pytest.raises(fn.NavError, match="basis 'screen_px'"):
        fn.read_blocking_set(_with_object(tmp_path, "bad", {}, dict(footprint, basis="screen_px")))


def test_object_solid_resolution():
    """N6: the object's footprint and solid win over its prop's; without solid a footprint of
    shape ellipse or rect blocks; shape none never does; solid false never does."""
    prop = {"footprint": {"shape": "ellipse", "width": 4, "depth": 2}, "solid": None}
    base = {"id": "a", "x": 10, "y": 10}
    assert fn.object_solid(base, prop)["shape"] == "ellipse"
    assert fn.object_solid(dict(base, solid=False), prop) is None
    assert fn.object_solid(base, dict(prop, solid=False)) is None
    assert fn.object_solid(dict(base, solid=True), dict(prop, solid=False))["shape"] == "ellipse"
    assert fn.object_solid(dict(base, footprint={"shape": "none"}), prop) is None
    assert fn.object_solid(dict(base, footprint={"shape": "rect", "width": 2, "depth": 2}), prop)["shape"] == "rect"
    assert fn.object_solid(base, None) is None
    with pytest.raises(fn.NavError, match="flip_x must be true or false"):
        fn.object_solid(dict(base, flip_x="yes"), prop)
    with pytest.raises(fn.NavError, match="scale must be greater than 0"):
        fn.object_solid(dict(base, scale=0), prop)


# --------------------------------------------------------------------------- the reader

def test_reader_refuses_what_map_nav_refuses(tmp_path):
    folder = tmp_path / "bad"
    cases = [
        ({"collision": None}, "no collision block"),
        ({"schema": "generate2dmap.map_bundle.v3"}, "schema"),
        ({"collision": {"actorRadius": 4, "walkRegions": [{"polygon": [[0, 0], [10, 0], [20, 0]]}]}}, "zero area"),
        ({"collision": {"actorRadius": -1}}, "actorRadius must be at least 0"),
        ({"props": {"a": {"image": "a.png"}}, "objects": [{"id": "o", "prop": "b", "x": 1, "y": 1,
                                                           "anchor_px": [0, 0]}]}, "unknown prop 'b'"),
        ({"material_map": {"image": "missing.png", "materials": {}}}, "file not found: missing.png"),
        ({"tile_size": 16, "tilesets": [{"id": "t", "manifest": "/abs.json"}]}, "relative POSIX path"),
        ({"world": {"width": 160, "height": 100, "unit": "px"}, "tile_size": 16,
          "layers": [{"name": "g", "kind": "tiles", "data": [[-1, -1]], "tileset": "t"}]}, "unknown tileset"),
    ]
    for change, message in cases:
        doc = {"schema": "generate2dmap.map_bundle.v2", "world": {"width": 160, "height": 100, "unit": "px"},
               "layers": [], "collision": {"actorRadius": 4}}
        doc.update(change)
        if doc.get("collision") is None:
            del doc["collision"]
        with pytest.raises(fn.NavError, match=message):
            fn.blocking_set_from_document(doc, folder)
    path = tmp_path / "bom.json"
    path.write_text('{"schema": "generate2dmap.map_bundle.v2", "world": {"width": 8, "height": 8, "unit": "px"},'
                    ' "collision": {"actorRadius": 1}}', encoding="utf-8-sig")
    assert fn.read_blocking_set(path).width == 8.0  # D28: a BOM is tolerated
    path.write_text('{"schema": "generate2dmap.map_bundle.v2", "schema": "again"}', encoding="utf-8")
    with pytest.raises(fn.NavError, match="duplicate key"):
        fn.read_blocking_set(path)
    path.write_text('{"x": NaN}', encoding="utf-8")
    with pytest.raises(fn.NavError, match="NaN"):
        fn.read_json(path)
    with pytest.raises(fn.NavError, match="cannot read JSON"):
        fn.read_json(tmp_path / "missing.json")


def test_reader_material_and_tile_errors(tmp_path):
    path = write_demo_bundle(tmp_path / "map", hashes=False)
    image = np.asarray(Image.open(path.parent / "materials.png")).copy()
    image[3, 3] = (1, 2, 3, 255)
    save(image, path.parent / "materials.png")
    with pytest.raises(fn.NavError, match=r"1 pixel\(s\) match no material \(first at x=3, y=3\)"):
        fn.read_blocking_set(path)
    save(np.zeros((8, 13, 4), np.uint8), path.parent / "materials.png")
    with pytest.raises(fn.NavError, match="whole squares"):
        fn.read_blocking_set(path)
    path = write_demo_bundle(tmp_path / "map2", hashes=False)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["world"]["width"] = 200
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(fn.NavError, match="the world is 200x128"):
        fn.read_blocking_set(path)


def test_props_from_a_prop_pack_supply_footprints(tmp_path):
    path = write_demo_bundle(tmp_path / "map", hashes=False)
    pack_dir = path.parent / "pack"
    save(np.full((20, 10, 4), 255, np.uint8), pack_dir / "lamp" / "prop.png")
    pack = {"accepted": [{"label": "lamp", "image": "lamp/prop.png", "anchor_px": [5, 19],
                          "footprint": {"shape": "ellipse", "width": 4, "depth": 2}, "solid": True}], "rejected": []}
    (pack_dir / "prop-pack.json").write_text(json.dumps(pack), encoding="utf-8")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["props"]["lamp"] = {"pack": "pack/prop-pack.json", "label": "lamp"}
    doc["objects"].append({"id": "lamp-1", "prop": "lamp", "x": 80, "y": 90, "anchor_px": [5, 19], "scale": 1.5})
    path.write_text(json.dumps(doc), encoding="utf-8")
    blocking = fn.read_blocking_set(path)
    lamp = [s for s in blocking.footprints if s["source"] == "object:lamp-1"]
    assert lamp == [{"shape": "ellipse", "cx": 80.0, "cy": 90.0, "rx": 3.0, "ry": 1.5, "rotate": 0.0,
                     "source": "object:lamp-1"}]
    assert blocking.solids == mb.world_solids(mb.load_bundle(path))
    doc["props"]["lamp"]["label"] = "lantern"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(fn.NavError, match="no accepted item 'lantern'"):
        fn.read_blocking_set(path)


def test_trigger_geometry():
    rect, circle = fn.Trigger(rect=(10.0, 20.0, 4.0, 6.0)), fn.Trigger(circle=(0.0, 0.0, 5.0))
    assert rect.bounds() == (10.0, 20.0, 14.0, 26.0) and circle.bounds() == (-5.0, -5.0, 5.0, 5.0)
    assert rect.distance(12, 22) == 0 and rect.distance(17, 30) == 5.0 and rect.closest_point(17, 30) == (14.0, 26.0)
    assert circle.distance(3, 4) == 0 and circle.distance(6, 8) == 5.0
    assert circle.closest_point(6, 8) == (3.0, 4.0) and circle.closest_point(1, 1) == (1, 1)
    with pytest.raises(fn.NavError, match="exactly one of"):
        fn.Trigger()
    with pytest.raises(fn.NavError, match="needs rect"):
        fn.Trigger.from_portal({"id": "p"})


def test_rule_book_is_in_the_module_docstring():
    """The docstring is the reference map-runtime.mjs mirrors and layered-map-contract.md quotes."""
    doc = fn.__doc__
    for rule in ("N1 ", "N2 ", "N3 ", "N4 ", "N5 ", "N6 ", "N7 ", "N8 ", "N9 ", "N10 ", "N11 ", "N12 ", "N13 ",
                 "N14 ", "N15 "):
        assert f"\n{rule}" in doc, rule
    for phrase in ("closed box 0 <= x <= W and 0 <= y <= H", "nu * nu + nv * nv <= 1", "x <= px <= x + w",
                   "even-odd", "holes", "world_px", "image_px", "flip_x", "ox = -ox and rot = -rot",
                   "walkable is false", "one_way", "decor never blocks", "liquid", "hazard", "0.7071067811865476",
                   "max(1, floor(r / 2 + 0.5))", "dx * k first, then", "D1", "D2", "D3", "D5", "D6", "D7"):
        assert phrase in doc, phrase
    assert doc.isascii()


@pytest.mark.perf
def test_large_map_builds_quickly():
    """A 1024 x 768 map (actor radius 6: 87,552 nodes) with 300 blockers, a holed walk region and
    a material map builds and searches in well under the budget (map_nav's own perf scenario)."""
    rng = np.random.default_rng(3)
    solids = []
    for k in range(300):
        x, y = rng.uniform(0, 1024), rng.uniform(0, 768)
        if k % 3 == 0:
            solids.append({"shape": "rect", "x": x, "y": y, "w": rng.uniform(2, 30), "h": rng.uniform(2, 30)})
        elif k % 3 == 1:
            solids.append({"shape": "ellipse", "cx": x, "cy": y, "rx": rng.uniform(2, 15), "ry": rng.uniform(2, 15),
                           "rotate": float(rng.uniform(0, 90))})
        else:
            solids.append({"shape": "polygon", "points": (np.array([x, y]) + rng.uniform(-15, 15, (6, 2))).tolist()})
    outline = np.array([[0, 0], [1024, 0], [1024, 768], [0, 768]], float)
    hole = np.array([[400, 300], [600, 300], [600, 450], [400, 450]], float)
    codes = rng.choice([fn.FREE, fn.BLOCK, fn.ONE_WAY], size=(48, 64), p=[0.9, 0.08, 0.02]).astype(np.uint8)
    started = time.perf_counter()
    model = fn.CollisionModel(1024, 768, 6, regions=[(outline, [hole])], solids=solids, material_codes=codes,
                              material_scale=16)
    found = fn.navigate(model, [(float(x), float(y)) for x, y in rng.uniform(100, 700, (5, 2))])
    elapsed = time.perf_counter() - started
    assert found.grid.valid.shape == (256, 342) and found.reachable.sum() > 1000
    assert elapsed < 20, f"{elapsed:.2f} s"
