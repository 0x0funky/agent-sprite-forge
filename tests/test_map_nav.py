"""map_nav.py (plan B13-T2): plan Appendix C collision rasterised from bundle data, material
classes, merged blocked rectangles, BFS reachability from spawns and arrivals to every
interaction, exit, slot and approach point, the thin-gap rule, portal checks (inside, arrival
outside the trigger, reciprocal), footprints scaled once, nav-grid.json and the debug PNG."""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import assert_cli_help, assert_valid_contract, load_script, run_cli, script_path
from test_map_bundle import SKILL, assert_patched_valid, demo, save

nav = load_script(SKILL, "map_nav")
mb = nav.map_bundle  # the module object map_nav itself uses
TOOL = script_path(SKILL, "map_nav")


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


def checked(path: Path, links=()) -> nav.NavResult:
    bundle = mb.load_bundle(path)
    assert bundle.errors == [], [p.as_dict() for p in bundle.errors]
    return nav.check_bundle(bundle, [mb.load_bundle(p) for p in links])


def check_status(result: nav.NavResult) -> dict:
    return {check["id"]: check["status"] for check in result.checks}


ROOM_WALLS = [{"shape": "rect", "x": x, "y": y, "w": w, "h": h}
              for x, y, w, h in ((100, 20, 50, 4), (100, 76, 50, 4), (100, 20, 4, 60), (146, 20, 4, 60))]


def sampled_only_reachable(model: nav.CollisionModel, start: tuple[float, float]) -> np.ndarray:
    """Reachability with plan Appendix C sampling only (nodes and move midpoints), no thin-gap rule."""
    c = model.cell
    xs = (np.arange(math.ceil(model.width / c)) + 0.5) * c
    ys = (np.arange(math.ceil(model.height / c)) + 0.5) * c
    valid = model.valid_lattice(xs, ys)
    h_ok = valid[:, :-1] & valid[:, 1:] & model.valid_lattice((np.arange(xs.size - 1) + 1.0) * c, ys)
    v_ok = valid[:-1] & valid[1:] & model.valid_lattice(xs, (np.arange(ys.size - 1) + 1.0) * c)
    moves = np.zeros(valid.shape, np.uint8)
    moves[:, :-1] |= np.where(h_ok, nav.MOVE_E, 0).astype(np.uint8)
    moves[:, 1:] |= np.where(h_ok, nav.MOVE_W, 0).astype(np.uint8)
    moves[:-1] |= np.where(v_ok, nav.MOVE_S, 0).astype(np.uint8)
    moves[1:] |= np.where(v_ok, nav.MOVE_N, 0).astype(np.uint8)
    row, col = math.floor(start[1] / c), math.floor(start[0] / c)
    return nav.reachable_mask(valid, [(row, col)], moves)


# --------------------------------------------------------------------------- B13-T2 acceptance

def test_merged_rects_union_equals_blocked(tmp_path):
    rng = np.random.default_rng(7)
    for shape, density in (((1, 1), 1.0), ((17, 29), 0.1), ((40, 33), 0.5), ((25, 64), 0.9)):
        mask = rng.random(shape) < density
        if shape[0] > 12 and shape[1] > 20:
            mask[5:12, 3:20] = True  # one solid block among the noise
        cover = np.zeros(shape, np.int64)
        for x, y, w, h in nav.merge_rects(mask):
            assert w > 0 and h > 0 and 0 <= x and x + w <= shape[1] and 0 <= y and y + h <= shape[0]
            cover[y:y + h, x:x + w] += 1
        assert np.array_equal(cover, mask.astype(np.int64))  # exact union, no overlap

    path = demo(tmp_path)
    out = tmp_path / "nav"
    assert run_cli([TOOL, "check", "--bundle", path, "--output-dir", out]).returncode == 0
    grid = json.loads((out / "nav-grid.json").read_text(encoding="utf-8"))
    blocked = np.array([[ch == "#" for ch in row] for row in grid["moves"]])
    union = np.zeros_like(blocked)
    cell = grid["cell"]
    for x, y, w, h in grid["blockedRects"]:
        assert x % cell == 0 and y % cell == 0 and w % cell == 0 and h % cell == 0
        assert not union[y // cell:(y + h) // cell, x // cell:(x + w) // cell].any()
        union[y // cell:(y + h) // cell, x // cell:(x + w) // cell] = True
    assert np.array_equal(union, blocked) and blocked.any()
    report = json.loads((out / "nav-report.json").read_text(encoding="utf-8"))
    assert {c["id"]: c for c in report["checks"]}["blocked_rects_union"]["status"] == "pass"


def test_unreachable_door_fails(tmp_path):
    door = {"id": "cellar-door", "rect": [120, 45, 10, 10], "to": "cellar:arrive", "activation": "crossing"}
    fields = {"spawns": [{"id": "start", "x": 20, "y": 50}], "portals": [door],
              "interactions": [{"id": "door-bell", "x": 125, "y": 30, "reach": 6}]}
    walls = {"actorRadius": 4, "solids": ROOM_WALLS}
    path = write_bundle(tmp_path / "town", collision=walls, **fields)
    result = checked(path)
    assert result.status == "fail" and check_status(result)["targets_reachable"] == "fail"
    unreachable = {t.id for t in result.targets if not t.reachable}
    assert unreachable == {"cellar-door", "door-bell"}

    out = tmp_path / "nav"
    run = run_cli([TOOL, "check", "--bundle", path, "--output-dir", out])
    assert run.returncode == 1 and "exit 'cellar-door' is unreachable" in run.stderr
    assert "nothing was published" in run.stderr and not out.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no stage left behind

    run = run_cli([TOOL, "check", "--bundle", path, "--output-dir", out, "--publish-on-fail"])
    assert run.returncode == 1 and json.loads(run.stdout)["status"] == "fail"
    report = json.loads((out / "nav-report.json").read_text(encoding="utf-8"))
    assert {c["id"]: c for c in report["checks"]}["targets_reachable"]["value"]["unreachable"] == ["door-bell",
                                                                                                    "cellar-door"]
    debug = np.asarray(Image.open(out / "nav-debug.png").convert("RGB"))
    assert (debug[50, 125] == (255, 40, 220)).all()  # the magenta cross on the door

    opened = write_bundle(tmp_path / "open", collision={"actorRadius": 4, "solids": ROOM_WALLS[1:]}, **fields)
    assert checked(opened).status in ("pass", "warn")  # the same door becomes reachable once a wall goes


def test_bounce_back_arrival_fails(tmp_path):
    exit_east = {"id": "exit-east", "rect": [150, 30, 10, 40], "to": "road", "activation": "intent",
                 "travelDirection": [1, 0], "radius": 6, "entranceByFrom": {"road": "arrive-east"}}
    spawns = [{"id": "start", "x": 20, "y": 50}, {"id": "arrive-east", "x": 154, "y": 50}]  # inside its own trigger
    path = write_bundle(tmp_path / "meadow", spawns=spawns, portals=[exit_east])
    result = checked(path)
    assert check_status(result)["arrivals_outside_triggers"] == "fail" and result.status == "fail"
    assert any("inside the trigger of portal 'exit-east' (bounce-back)" in p.message for p in result.problems)
    spawns[1]["x"] = 140
    assert check_status(checked(write_bundle(tmp_path / "fixed", spawns=spawns, portals=[exit_east])))[
        "arrivals_outside_triggers"] == "pass"

    # across maps: the arrival in the destination lies inside the destination's own return trigger
    meadow = write_bundle(tmp_path / "meadow2", id="meadow", spawns=spawns,
                          portals=[dict(exit_east, to="road:arrive-west")])
    road_exit = {"id": "exit-west", "rect": [0, 30, 10, 40], "to": "meadow:arrive-east", "activation": "intent",
                 "travelDirection": [-1, 0], "radius": 6, "entranceByFrom": {"meadow": "arrive-west"}}
    road = write_bundle(tmp_path / "road", id="road", spawns=[{"id": "arrive-west", "x": 6, "y": 50}],
                        portals=[road_exit])
    linked = checked(meadow, [road])
    assert check_status(linked)["reciprocal_links"] == "fail"
    assert any("arrival in 'road' lies inside the trigger of portal 'exit-west' (bounce-back)" in p.message
               for p in linked.problems)


def test_thin_gap_not_jumpable(tmp_path):
    """Two walk regions 0.5 px apart: no footprint sample of any node or move midpoint lands in
    the gap (cell 3, radius 6), so sampled collision alone would join them; the thin-gap rule
    (the actor's centre path checked exactly) keeps them apart. A 0.5 px wall behaves the same."""
    regions = [{"polygon": [[0, 0], [32, 0], [32, 30], [0, 30]]},
               {"polygon": [[32.5, 0], [66, 0], [66, 30], [32.5, 30]]}]
    fields = {"world": {"width": 66, "height": 30, "unit": "px"}, "spawns": [{"id": "start", "x": 10, "y": 15}],
              "interactions": [{"id": "far", "x": 55, "y": 15, "reach": 3}]}
    wall = {"actorRadius": 6, "solids": [{"shape": "rect", "x": 32, "y": -1, "w": 0.5, "h": 32}]}
    for name, collision in (("gap", {"actorRadius": 6, "walkRegions": regions}), ("wall", wall)):
        path = write_bundle(tmp_path / name, collision=collision, **fields)
        result = checked(path)
        model = result.model
        assert model.cell == 3
        far = (math.floor(15 / 3), math.floor(55 / 3))
        assert sampled_only_reachable(model, (10, 15))[far], "sampling alone must jump the gap for this test to matter"
        assert result.distance[far] == -1 and result.status == "fail"
        assert {t.id for t in result.targets if not t.reachable} == {"far"}
        assert check_status(result)["thin_gaps"] == "warn" and len(result.grid.thin_gaps) == 6  # one per walkable row
        reason = model.segment_status((31.5, 16.5), (34.5, 16.5))
        assert reason is not None and "thin-gap rule" in reason
        assert model.valid(31.5, 16.5) and model.valid(33.0, 16.5) and model.valid(34.5, 16.5)
        assert model.segment_clear((31.5, 16.5), (34.5, 16.5), thin_gap=False)  # plan Appendix C sampling alone
        query = [TOOL, "query", "--bundle", path, "--segment", "31.5,16.5,34.5,16.5"]
        answers = [json.loads(run_cli(query + flag).stdout) for flag in ([], ["--sampled-only"])]
        assert [a["segments"][0]["clear"] for a in answers] == [False, True]


# --------------------------------------------------------------------------- Appendix C semantics

def test_footprint_samples_and_cell():
    offsets = nav.footprint_offsets(5, 0.58)
    s = 0.7071067811865476
    expected = [[0, 0], [5, 0], [5 * s, 5 * 0.58 * s], [0, 5 * 0.58], [-5 * s, 5 * 0.58 * s], [-5, 0],
                [-5 * s, -5 * 0.58 * s], [0, -5 * 0.58], [5 * s, -5 * 0.58 * s]]
    assert offsets.tolist() == expected  # exact doubles: rx * SQRT1_2 and (r * ySquash) * SQRT1_2
    assert [nav.nav_cell(r) for r in (0, 1, 2.9, 3, 4, 5, 6, 7)] == [1, 1, 1, 2, 2, 3, 3, 4]


def test_point_validity_semantics():
    model = nav.CollisionModel(100, 60, 5, solids=[{"shape": "rect", "x": 50, "y": 0, "w": 10, "h": 60}])
    assert model.valid(5, 5) and not model.valid(4.9, 30)  # the world box is closed
    assert not model.valid(45, 30) and model.valid(44.99, 30)  # closed solid, actor size added once by sampling
    assert model.blocked(50, 30) and model.blocked(60, 30) and not model.blocked(60.01, 30)

    rotated = nav.CollisionModel(60, 60, 0, solids=[{"shape": "ellipse", "cx": 30, "cy": 30, "rx": 10, "ry": 2,
                                                     "rotate": 90}])
    assert rotated.blocked(30, 39) and not rotated.blocked(33, 30)  # long axis turned to vertical

    hole = [[40, 20], [60, 20], [60, 40], [40, 40]]
    region = nav.CollisionModel(100, 60, 0, regions=[(np.array([[0, 0], [100, 0], [100, 60], [0, 60]], float),
                                                      [np.array(hole, float)])])
    assert not region.area_ok(50, 30) and region.area_ok(10, 30)
    assert region.area_ok(0, 30) and not region.area_ok(100, 30)  # even-odd crossing test: left edge in, right out
    assert region.area_ok(10, 0) and not region.area_ok(10, 60)

    wall = [{"shape": "rect", "x": 0, "y": 0, "w": 100, "h": 20}]
    squashed = nav.CollisionModel(100, 60, 10, 0.58, solids=wall)
    round_actor = nav.CollisionModel(100, 60, 10, 1.0, solids=wall)
    assert squashed.valid(50, 26) and not round_actor.valid(50, 26)  # HD-2D footprints are flatter (ry = 5.8)


def test_footprints_scaled_once(tmp_path):
    save(np.full((8, 8, 4), 255, np.uint8), tmp_path / "solo" / "post.png")
    props = {"post": {"image": "post.png", "anchor_px": [4, 7],
                      "footprint": {"shape": "ellipse", "width": 4, "depth": 2}}}
    objects = [{"id": "post-1", "prop": "post", "x": 50, "y": 50, "scale": 3, "anchor_px": [4, 7]}]
    path = write_bundle(tmp_path / "solo", collision={"actorRadius": 2}, props=props, objects=objects,
                        layers=[{"name": "props", "kind": "objects"}], spawns=[{"id": "start", "x": 10, "y": 10}])
    model = checked(path).model
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
    result = checked(path)
    model = result.model
    assert not model.valid(35, 5) and not model.valid(2, 35) and model.valid(12, 35) and not model.valid(22, 35)
    assert model.valid(5, 20) and model.valid(15, 11)  # decor, and standing on the ledge
    assert "one_way" in model.segment_status((20, 5), (20, 20))  # dropping onto the ledge from above
    assert model.segment_clear((20, 20), (20, 5))  # jumping up through it
    assert model.segment_clear((3, 11), (16, 11))  # walking along it
    below = (math.floor(20.5), math.floor(20.5))  # node at (20.5, 20.5)
    assert result.distance[below] == -1 and result.grid.one_way_blocked > 0
    from_below = checked(write_bundle(tmp_path / "cave2", world={"width": 40, "height": 40, "unit": "px"},
                                      collision={"actorRadius": 1},
                                      material_map={"image": "../cave/materials.png", "materials": materials},
                                      spawns=[{"id": "low", "x": 15, "y": 25}]))
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
        codes = rng.choice([mb.FREE, mb.BLOCK, mb.ONE_WAY], size=(6, 8), p=[0.8, 0.1, 0.1]).astype(np.uint8)
        model = nav.CollisionModel(width, height, rng.uniform(1.5, 4.5), float(rng.choice([1.0, 0.58])),
                                   regions=[(outline, [hole])], solids=solids, material_codes=codes, material_scale=9)
        grid = nav.build_grid(model)
        nodes = np.argwhere(grid.valid)
        assert np.array_equal(grid.valid[nodes[:, 0], nodes[:, 1]],
                              model.valid(grid.xs[nodes[:, 1]], grid.ys[nodes[:, 0]]))
        checked_moves = 0
        for r, c in nodes[rng.permutation(len(nodes))[:250]]:
            here = (grid.xs[c], grid.ys[r])
            for bit, dr, dc in ((nav.MOVE_E, 0, 1), (nav.MOVE_S, 1, 0), (nav.MOVE_W, 0, -1), (nav.MOVE_N, -1, 0)):
                rr, cc = r + dr, c + dc
                if not (0 <= rr < grid.rows and 0 <= cc < grid.cols) or not grid.valid[rr, cc]:
                    assert not grid.moves[r, c] & bit
                    continue
                expected = model.segment_clear(here, (grid.xs[cc], grid.ys[rr]))
                assert bool(grid.moves[r, c] & bit) == expected, (seed, r, c, bit)
                checked_moves += 1
        assert checked_moves > 200


def test_oversized_grid_is_refused_before_allocating():
    with pytest.raises(mb.BundleError, match="split the map into chunks"):
        nav.build_grid(nav.CollisionModel(100_000, 100_000, 1))


def test_grid_bfs_public_api():
    passable = np.ones((5, 6), bool)
    passable[:4, 3] = False
    distance = nav.grid_bfs(passable, [(0, 0), (9, 9), (0, 3)])  # off-grid and blocked starts are ignored
    assert distance[0, 0] == 0 and distance[4, 3] == 4 + 3 and distance[0, 5] == 7 + 4 + 2  # round the wall
    assert np.array_equal(nav.reachable_mask(passable, [(0, 0)]), passable)
    corridor = np.ones((1, 4), bool)
    one_way = np.array([[nav.MOVE_E, nav.MOVE_E, nav.MOVE_E | nav.MOVE_N | nav.MOVE_S, nav.MOVE_W | nav.MOVE_E]],
                       np.uint8)
    assert nav.grid_bfs(corridor, [(0, 0)], one_way).tolist() == [[0, 1, 2, 3]]
    assert nav.grid_bfs(corridor, [(0, 3)], one_way).tolist() == [[-1, -1, 1, 0]]  # off-grid bits are ignored
    assert nav.moves_from_mask(np.array([[True, True], [False, True]])).tolist() == [[1, 4 | 2], [0, 8]]


# --------------------------------------------------------------------------- portals, anchors and links

def test_portal_checks(tmp_path):
    spawns = [{"id": "start", "x": 20, "y": 50}, {"id": "arrive-east", "x": 140, "y": 50}]
    outside = {"id": "exit-east", "rect": [155, 30, 10, 40], "to": "road", "activation": "intent",
               "travelDirection": [1, 0], "radius": 6, "entranceByFrom": {"road": "arrive-east"}}
    result = checked(write_bundle(tmp_path / "a", spawns=spawns, portals=[outside]))
    assert check_status(result)["portals_inside_world"] == "fail"
    assert check_status(result)["reciprocal_links"] == "skipped"

    meadow_exit = dict(outside, rect=[150, 30, 10, 40], to="road:arrive-west")
    road_spawns = [{"id": "arrive-west", "x": 20, "y": 50}]
    road_exit = {"id": "exit-west", "rect": [0, 30, 10, 40], "to": "meadow", "activation": "intent",
                 "travelDirection": [-1, 0], "radius": 6, "entranceByFrom": {"meadow": "arrive-west"}}
    meadow = write_bundle(tmp_path / "meadow", spawns=spawns, portals=[meadow_exit])
    road = write_bundle(tmp_path / "road", spawns=road_spawns, portals=[road_exit])
    linked = checked(meadow, [road])
    assert check_status(linked)["reciprocal_links"] == "pass" and linked.status in ("pass", "warn")
    assert linked.links == [{"portal": "exit-east", "to": "road", "arrival": [20.0, 50.0],
                             "returnPortals": ["exit-west"], "status": "pass"}]

    lonely_road = write_bundle(tmp_path / "road2", id="road", spawns=road_spawns)
    one_way = checked(meadow, [lonely_road])
    assert check_status(one_way)["reciprocal_links"] == "fail"
    assert any("has no portal back to 'meadow'" in p.message for p in one_way.problems)
    allowed = write_bundle(tmp_path / "meadow3", id="meadow", spawns=spawns,
                           portals=[dict(meadow_exit, reciprocal=False)])
    assert check_status(checked(allowed, [lonely_road]))["reciprocal_links"] == "pass"

    out = tmp_path / "out"
    run = run_cli([TOOL, "check", "--bundle", meadow, "--link", road, "--output-dir", out])
    assert run.returncode == 0, run.stderr
    report = json.loads((out / "nav-report.json").read_text(encoding="utf-8"))
    assert report["links"][0]["status"] == "pass"
    clash = run_cli([TOOL, "check", "--bundle", meadow, "--link", meadow, "--output-dir", tmp_path / "out2"])
    assert clash.returncode == 1 and "map ids must be unique" in clash.stderr


def test_slots_approach_and_reach(tmp_path):
    anchors = {"well": {"point": [80, 50], "slots": [[70, 62], [80, 52]], "approach": [80, 66]}}
    solids = [{"shape": "ellipse", "cx": 80, "cy": 50, "rx": 6, "ry": 4}]
    path = write_bundle(tmp_path / "plaza", collision={"actorRadius": 4, "solids": solids}, anchors=anchors,
                        spawns=[{"id": "start", "x": 20, "y": 50}],
                        interactions=[{"id": "well", "x": 80, "y": 50, "reach": 12},
                                      {"id": "well-too-close", "x": 80, "y": 50, "reach": 5},
                                      {"id": "pebble", "x": 30, "y": 30}])
    result = checked(path)
    status = {t.id: (t.reachable, t.reason) for t in result.targets}
    assert status["well/slots[0]"][0] and status["well/approach[0]"][0] and status["well"][0] and status["pebble"][0]
    assert status["well/slots[1]"] == (False, "the actor cannot stand here (footprint blocked)")
    assert status["well-too-close"] == (False, "no reachable node within reach 5 px")


def test_intent_exit_reachable_where_crossing_is_not(tmp_path):
    base = {"world": {"width": 100, "height": 60, "unit": "px"}, "collision": {"actorRadius": 10},
            "spawns": [{"id": "start", "x": 20, "y": 30}]}
    crossing = {"id": "edge", "rect": [96, 20, 4, 20], "to": "next", "activation": "crossing"}
    result = checked(write_bundle(tmp_path / "crossing", portals=[crossing], **base))
    assert [t.reason for t in result.targets] == ["the actor's centre cannot enter the trigger from any reachable node"]
    intent = dict(crossing, activation="intent", travelDirection=[1, 0], radius=10)
    assert [t.reachable for t in checked(write_bundle(tmp_path / "intent", portals=[intent], **base)).targets] == [True]
    near = dict(intent, radius=8)  # the nearest valid node (x 87.5) is 8.5 px from the trigger
    assert [t.reachable for t in checked(write_bundle(tmp_path / "near", portals=[near], **base)).targets] == [False]


def test_start_and_arrival_problems(tmp_path):
    solids = [{"shape": "rect", "x": 60, "y": 40, "w": 20, "h": 20}]
    portal = {"id": "gate", "rect": [150, 40, 10, 20], "to": "road", "activation": "intent", "travelDirection": [1, 0],
              "radius": 6, "entranceByFrom": {"road": [130, 50], "hill": [70, 50]}}
    result = checked(write_bundle(tmp_path / "a", collision={"actorRadius": 4, "solids": solids},
                                  spawns=[{"id": "start", "x": 20, "y": 50}, {"id": "stuck", "x": 70, "y": 45}],
                                  portals=[portal]))
    starts = {s.id: (s.kind, s.reachable, s.reason) for s in result.starts}
    assert starts["stuck"] == ("spawn", False, "not a valid actor position")
    assert starts["gate<-road"] == ("arrival", True, None) and starts["gate<-hill"][1] is False
    statuses = check_status(result)
    assert statuses["starts_valid"] == "fail" and statuses["arrivals_outside_triggers"] == "fail"
    message = "arrival from 'hill' at portal 'gate' is not a valid actor position"
    assert any(message in p.message for p in result.problems)
    empty = checked(write_bundle(tmp_path / "b"))
    assert check_status(empty)["starts_valid"] == "fail"
    assert any("no spawns or portal arrivals" in p.message for p in empty.problems)


def test_off_grid_target(tmp_path):
    """A corridor 0.5 px wider than the actor: (40, 14.25) is a valid position, but no grid node fits."""
    walls = [{"shape": "rect", "x": 0, "y": 0, "w": 160, "h": 10},
             {"shape": "rect", "x": 0, "y": 18.5, "w": 160, "h": 81.5}]
    result = checked(write_bundle(tmp_path / "corridor", collision={"actorRadius": 4, "solids": walls},
                                  spawns=[{"id": "start", "x": 40, "y": 14.25}],
                                  interactions=[{"id": "mark", "x": 60, "y": 14.25}]))
    assert result.model.valid(40, 14.25) and not result.grid.valid.any()
    assert result.starts[0].reason == "cannot reach any grid node"
    assert result.targets[0].reason.startswith("valid but off the grid (no valid node within two cells")


def test_crossing_exit_through_its_closest_point(tmp_path):
    """A trigger 0.5 px wide between node centres (x 41 and 43) is entered by a straight move."""
    door = {"id": "slot", "rect": [41.5, 40, 0.5, 20], "to": "cellar", "activation": "crossing"}
    result = checked(write_bundle(tmp_path / "slot", spawns=[{"id": "start", "x": 20, "y": 50}], portals=[door]))
    (target,) = result.targets
    assert target.reachable and target.node == (41.5, 41.0) and target.steps is not None  # nearest row first


def test_link_arrival_through_the_return_portal(tmp_path):
    spawns = [{"id": "start", "x": 20, "y": 50}, {"id": "arrive-east", "x": 130, "y": 50}]
    to_road = {"id": "exit-east", "rect": [150, 30, 10, 40], "to": "road", "activation": "intent",
               "travelDirection": [1, 0], "radius": 6, "entranceByFrom": {"road": "arrive-east"}}
    meadow = write_bundle(tmp_path / "meadow", spawns=spawns, portals=[to_road])
    back = {"id": "exit-west", "rect": [0, 30, 10, 40], "to": "meadow:arrive-east", "activation": "intent",
            "travelDirection": [-1, 0], "radius": 6, "entranceByFrom": {"meadow": [30, 50]}}
    road = write_bundle(tmp_path / "road", spawns=[{"id": "start", "x": 80, "y": 50}], portals=[back])
    linked = checked(meadow, [road])
    assert linked.links[0]["arrival"] == [30.0, 50.0] and check_status(linked)["reciprocal_links"] == "pass"
    silent = write_bundle(tmp_path / "road2", id="road", spawns=[{"id": "start", "x": 80, "y": 50}],
                          portals=[{k: v for k, v in back.items() if k != "entranceByFrom"}])
    assert any("names no arrival for travellers from 'meadow'" in p.message for p in checked(meadow, [silent]).problems)
    ghost = write_bundle(tmp_path / "meadow2", id="meadow", spawns=spawns, portals=[dict(to_road, to="road:ghost")])
    assert any("has no spawn or anchor 'ghost'" in p.message for p in checked(ghost, [road]).problems)


def test_thin_one_way_strip_is_caught_by_the_centre_path():
    codes = np.zeros((40, 40), np.uint8)
    codes[10, :] = mb.ONE_WAY  # a 1 px ledge; samples 1.5 px apart can step over it
    model = nav.CollisionModel(40, 40, 6, material_codes=codes, material_scale=1)
    assert model.segment_status((20, 8.2), (20, 11.2)) == "centre path enters one_way material from above"
    assert model.segment_clear((20, 8.2), (20, 11.2), thin_gap=False)  # the sampled rule alone misses it
    assert model.segment_clear((20, 11.2), (20, 8.2))  # upward is open


def test_segments_along_region_edges():
    """Even-odd boundaries: the top edge of a region is inside, the bottom edge outside; a segment
    lying on an edge (the collinear case of the breakpoint search) follows the point rule."""
    square = (np.array([[10, 10], [30, 10], [30, 30], [10, 30]], float), [])
    point_actor = nav.CollisionModel(40, 40, 0, regions=[square])
    assert point_actor.segment_clear((12, 10), (28, 10))
    assert not point_actor.segment_clear((12, 30), (28, 30))
    breaks = point_actor.segment_breaks((5, 10), (35, 10))
    assert {0.0, 1.0} <= set(breaks.tolist()) and np.isclose(breaks, 5 / 30).any() and np.isclose(breaks, 25 / 30).any()


# --------------------------------------------------------------------------- outputs and CLI

def test_nav_outputs_and_contracts(tmp_path):
    path = demo(tmp_path)
    outputs = []
    for name in ("first", "second"):
        out = tmp_path / name
        run = run_cli([TOOL, "check", "--bundle", path, "--output-dir", out], "cp1252")
        assert run.returncode == 0, run.stderr
        summary = json.loads(run.stdout)
        assert summary["status"] == "warn" and summary["unreachable"] == 0
        assert summary["metadata"] == str((out / "nav-report.json").resolve())
        outputs.append({p.name: p.read_bytes() for p in out.iterdir()})
    assert outputs[0] == outputs[1] and set(outputs[0]) == {"nav-grid.json", "nav-report.json", "nav-debug.png"}

    grid = json.loads(outputs[0]["nav-grid.json"])
    assert_patched_valid(grid, "nav_grid_v1")
    report = json.loads(outputs[0]["nav-report.json"])
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
    assert_patched_valid(report, "nav_report_v1")
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert report["status"] == "warn" and statuses["unreachable_walkable_area"] == "warn"
    refs = {ref["path"]: ref["sha256"] for ref in report["outputs"]}
    assert refs == {name: hashlib.sha256(outputs[0][name]).hexdigest() for name in ("nav-grid.json", "nav-debug.png")}
    assert {ref["path"] for ref in report["inputs"]} >= {"../map/map-bundle.json", "../map/tiles/terrain.png"}

    result = checked(path)
    decoded = np.array([[int(ch, 16) if ch != "#" else -1 for ch in row] for row in grid["moves"]])
    assert np.array_equal(decoded >= 0, result.grid.valid)
    assert np.array_equal(np.where(result.grid.valid, result.grid.moves.astype(int), -1), decoded)
    assert np.array_equal(np.array([[ch == "1" for ch in row] for row in grid["reachable"]]), result.distance >= 0)
    assert (grid["cell"], grid["cols"], grid["rows"]) == (2, 96, 64) and len(grid["actor"]["samples"]) == 9
    assert Image.open(tmp_path / "first" / "nav-debug.png").size == (192, 128)


def test_cli_help_cp1252():
    assert_cli_help(SKILL, "map_nav")
    for verb in ("check", "query"):
        result = run_cli([TOOL, verb, "--help"], "cp1252")
        assert result.returncode == 0 and result.stdout.isascii()


def test_cli_refuses_existing_output(tmp_path):
    path = demo(tmp_path)
    out = tmp_path / "taken"
    out.mkdir()
    (out / "keep.txt").write_text("keep", encoding="utf-8")
    run = run_cli([TOOL, "check", "--bundle", path, "--output-dir", out])
    assert run.returncode == 1 and run.stderr.startswith("error: refusing to replace existing output")
    assert [p.name for p in out.iterdir()] == ["keep.txt"]


def test_cli_failure_publishes_nothing(tmp_path):
    path = write_bundle(tmp_path / "town", collision={"actorRadius": 4, "solids": ROOM_WALLS},
                        spawns=[{"id": "start", "x": 20, "y": 50}],
                        interactions=[{"id": "inside", "x": 125, "y": 50, "reach": 4}])
    out = tmp_path / "result" / "nav"
    run = run_cli([TOOL, "check", "--bundle", path, "--output-dir", out])
    assert run.returncode == 1 and "nothing was published" in run.stderr and "Traceback" not in run.stderr
    assert not out.exists() and list((tmp_path / "result").iterdir()) == []

    broken = write_bundle(tmp_path / "broken", spawns=[{"id": "start", "x": 20, "y": 50}],
                          portals=[{"id": "p", "rect": [0, 0, 5, 5], "to": "broken:nowhere"}])
    run = run_cli([TOOL, "check", "--bundle", broken, "--output-dir", tmp_path / "never"])
    assert run.returncode == 1 and "does not validate" in run.stderr and not (tmp_path / "never").exists()


def test_query_cli_and_v1_refusal(tmp_path):
    path = write_bundle(tmp_path / "q", collision={"actorRadius": 4, "solids": [{"shape": "rect", "x": 50, "y": 0,
                                                                                 "w": 4, "h": 100}]},
                        spawns=[{"id": "start", "x": 20, "y": 50}])
    run = run_cli([TOOL, "query", "--bundle", path, "--point", "20,50", "--point", "48,50",
                   "--segment", "20,50,40,50", "--segment", "20,50,80,50"])
    assert run.returncode == 0, run.stderr
    answer = json.loads(run.stdout)
    assert [p["valid"] for p in answer["points"]] == [True, False] and answer["cell"] == 2
    assert [s["clear"] for s in answer["segments"]] == [True, False]
    assert "blocked" in answer["segments"][1]["reason"]
    assert run_cli([TOOL, "query", "--bundle", path, "--point", "1,2,3"]).returncode == 2  # argparse usage error

    legacy = tmp_path / "legacy"
    (legacy / "layers").mkdir(parents=True)
    (legacy / "layers" / "g.csv").write_text("0,0\n0,0\n", encoding="utf-8")
    legacy_doc = {"schema": "generate2dmap.map_bundle.v1", "tile_size": 8,
                  "layers": [{"name": "g", "kind": "tiles", "data": "layers/g.csv"}]}
    (legacy / "map-bundle.json").write_text(json.dumps(legacy_doc), encoding="utf-8")
    run = run_cli([TOOL, "check", "--bundle", legacy / "map-bundle.json", "--output-dir", tmp_path / "x"])
    assert run.returncode == 1 and "no collision block" in run.stderr


@pytest.mark.perf
def test_large_map_builds_quickly():
    """A 1024 x 768 map (actor radius 6: 87,552 nodes) with 300 blockers, a holed walk region and
    a material map builds and searches in well under the budget."""
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
    codes = rng.choice([mb.FREE, mb.BLOCK, mb.ONE_WAY], size=(48, 64), p=[0.9, 0.08, 0.02]).astype(np.uint8)
    started = time.perf_counter()
    model = nav.CollisionModel(1024, 768, 6, regions=[(outline, [hole])], solids=solids, material_codes=codes,
                               material_scale=16)
    grid = nav.build_grid(model)
    distance = nav.grid_bfs(grid.valid, [tuple(np.argwhere(grid.valid)[0])], grid.moves)
    elapsed = time.perf_counter() - started
    assert grid.valid.shape == (256, 342) and (distance >= 0).sum() > 1000
    assert elapsed < 20, f"{elapsed:.2f} s"
