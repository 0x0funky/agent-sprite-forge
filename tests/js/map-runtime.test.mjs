// Node tests for skills/generate2dmap/references/runtime/map-runtime.mjs (B17-T1).
// Run: node --test tests/js/map-runtime.test.mjs (tests/test_map_runtime_js.py runs it under pytest).
import test from "node:test";
import assert from "node:assert/strict";

import {
  FOOTPRINT_DIRECTIONS, INTENT_MIN_COS, SNAPSHOT_SCHEMA, TICK_HZ, actorDrawIndex, advanceAlongPath, arrive,
  cellCenter, cellValid, createActor, createMapRuntime, decodeMaterialGrid, exitCandidate, findPath, flood,
  footprintSamples, insidePolygon, isBlocked, materialBlocks, materialGridFromRGBA, moveWithCollision, navCellSize,
  navGrid, pointFree, portalArrivals, returnSpawn, runtimeSnapshot, segmentClear, sortForDrawing, stepActor,
  traverseRoutes,
} from "../../skills/generate2dmap/references/runtime/map-runtime.mjs";

const BUDGET = 112 / TICK_HZ;

// 200 x 120 room split by a wall from the top down to y = 80; the gap below it
// joins the west and east halves. One intent exit on the east edge.
function corridorBundle(overrides = {}) {
  return {
    schema: "generate2dmap.map_bundle.v2",
    world: {width: 200, height: 120, unit: "px"},
    layers: [{name: "props", kind: "objects"}],
    collision: {actorRadius: 6, ySquash: 1, solids: [{shape: "rect", x: 96, y: 0, w: 8, h: 80}]},
    spawns: [{id: "west", x: 30, y: 60, facing: "east"}, {id: "east", x: 160, y: 60, facing: "west"}],
    portals: [{
      id: "exit-east", rect: [192, 40, 8, 40], to: "meadow:west", activation: "intent",
      travelDirection: [1, 0], radius: 10, entranceByFrom: {meadow: "east"}, latch: true, requiresMovement: true,
    }],
    interactions: [{id: "sign", x: 60, y: 30, reach: 16}],
    anchors: {well: {point: [150, 30], slots: [[140, 44], {x: 160, y: 44}], approach: [150, 50]}},
    ...overrides,
  };
}

function corridor(overrides) {
  return createMapRuntime(corridorBundle(overrides));
}

function walkPath(world, actor, budget, onStep) {
  for (let tick = 0; actor.path !== null && tick < 10000; tick++) onStep(stepActor(world, actor, null, budget), tick);
}

// ------------------------------------------------------------------- Appendix C point and footprint semantics

test("rect solids are half-open and collision.rects block like rect solids", () => {
  const world = createMapRuntime({
    world: {width: 100, height: 100},
    collision: {actorRadius: 0, solids: [{shape: "rect", x: 10, y: 10, w: 10, h: 10}], rects: [[50, 50, 5, 5]]},
  });
  assert.equal(pointFree(world, 10, 10), false);
  assert.equal(pointFree(world, 19.999, 19.999), false);
  assert.equal(pointFree(world, 20, 15), true);
  assert.equal(pointFree(world, 15, 20), true);
  assert.equal(pointFree(world, 9.999, 15), true);
  assert.equal(pointFree(world, 52, 52), false);
  assert.equal(pointFree(world, 55, 52), true);
});

test("ellipse solids have an open boundary, zero radii never block, rotation is clockwise in degrees", () => {
  const world = createMapRuntime({
    world: {width: 200, height: 200},
    collision: {actorRadius: 0, solids: [
      {shape: "ellipse", cx: 50, cy: 50, rx: 10, ry: 5},
      {shape: "ellipse", cx: 150, cy: 50, rx: 10, ry: 5, rotate: 90},
      {shape: "ellipse", cx: 50, cy: 150, rx: 0, ry: 5},
    ]},
  });
  assert.equal(pointFree(world, 59.999, 50), false);
  assert.equal(pointFree(world, 60, 50), true);
  assert.equal(pointFree(world, 50, 54.999), false);
  assert.equal(pointFree(world, 50, 55), true);
  assert.equal(pointFree(world, 150, 59.9), false, "rotated 90 degrees: the long axis points down");
  assert.equal(pointFree(world, 159.9, 50), true);
  assert.equal(pointFree(world, 50, 150), true, "rx 0 has no interior");
});

test("polygons use the even-odd rule; walk regions are a union and holes belong to their region", () => {
  const concave = [[0, 0], [30, 0], [30, 30], [20, 30], [20, 10], [10, 10], [10, 30], [0, 30]];
  const xs = Float64Array.from(concave, (p) => p[0]), ys = Float64Array.from(concave, (p) => p[1]);
  assert.equal(insidePolygon(xs, ys, 5, 20), true);
  assert.equal(insidePolygon(xs, ys, 15, 20), false, "the notch is outside");
  assert.equal(insidePolygon(xs, ys, 0, 0), true, "the polygon is half-open: top-left edge in");
  assert.equal(insidePolygon(xs, ys, 30, 5), false, "right edge out");
  const world = createMapRuntime({
    world: {width: 100, height: 100},
    collision: {actorRadius: 0, walkRegions: [
      {polygon: [[0, 0], [60, 0], [60, 60], [0, 60]], holes: [[[20, 20], [40, 20], [40, 40], [20, 40]]]},
      {polygon: [[30, 30], [90, 30], [90, 90], [30, 90]]},
    ]},
  });
  assert.equal(pointFree(world, 10, 10), true);
  assert.equal(pointFree(world, 25, 25), false, "inside region A's hole and outside region B");
  assert.equal(pointFree(world, 35, 35), true, "inside region A's hole but inside region B");
  assert.equal(pointFree(world, 95, 95), false, "outside every region, although inside the map");
});

test("without walk regions the map bounds are [0, width) x [0, height)", () => {
  const world = createMapRuntime({world: {width: 50, height: 40}, collision: {actorRadius: 0}});
  assert.equal(pointFree(world, 0, 0), true);
  assert.equal(pointFree(world, 49.999, 39.999), true);
  assert.equal(pointFree(world, 50, 10), false);
  assert.equal(pointFree(world, 10, 40), false);
  assert.equal(pointFree(world, -0.001, 10), false);
  assert.equal(pointFree(world, NaN, 10), false, "NaN is never free");
});

test("the footprint is the point plus 8 exact samples on the ySquash ellipse", () => {
  assert.equal(FOOTPRINT_DIRECTIONS.length, 8);
  for (const [ux, uy] of FOOTPRINT_DIRECTIONS) assert.ok(Math.abs(ux * ux + uy * uy - 1) < 1e-15);
  const bundle = (ySquash) => ({
    world: {width: 100, height: 100},
    collision: {actorRadius: 10, ySquash, solids: [{shape: "rect", x: 0, y: 0, w: 100, h: 50}]},
  });
  const round = createMapRuntime(bundle(1)), squashed = createMapRuntime(bundle(0.5));
  const samples = footprintSamples(squashed, 50, 57);
  assert.deepEqual(samples[0], [50, 57], "the centre comes first");
  assert.deepEqual(samples[1], [60, 57], "then east, clockwise on screen");
  assert.deepEqual(samples[2], [50 + 10 * Math.sqrt(0.5), 57 + 5 * Math.sqrt(0.5)]);
  assert.deepEqual(samples[7], [50, 52], "north sample at y - r * ySquash");
  assert.equal(isBlocked(round, 50, 57), true, "the round footprint reaches y = 47");
  assert.equal(isBlocked(squashed, 50, 57), false, "the squashed footprint stops at y = 52");
  assert.equal(isBlocked(squashed, 50, 54.9), true);
});

test("object footprints are scaled once by the instance scale; world_px, solid false and none do not scale or block", () => {
  const object = (extra) => ({id: "tree", prop: "tree", x: 100, y: 100, anchor_px: [8, 30], scale: 2,
    footprint: {shape: "ellipse", width: 10, depth: 4, offset: [0, -2]}, ...extra});
  const make = (objects) => createMapRuntime({world: {width: 200, height: 200}, collision: {actorRadius: 0}, objects});
  const scaled = make([object()]);
  assert.equal(pointFree(scaled, 100, 96), false, "centre moves by offset * scale");
  assert.equal(pointFree(scaled, 109.9, 96), false, "rx = width * scale / 2 = 10");
  assert.equal(pointFree(scaled, 110.1, 96), true);
  const world = make([object({footprint: {shape: "ellipse", width: 10, depth: 4, offset: [0, -2], basis: "world_px"}})]);
  assert.equal(pointFree(world, 104.9, 98), false, "world_px: rx = 5, offset not scaled");
  assert.equal(pointFree(world, 105.1, 98), true);
  assert.equal(pointFree(make([object({solid: false})]), 100, 96), true);
  assert.equal(pointFree(make([object({footprint: {shape: "none"}})]), 100, 96), true);
  const rect = make([object({footprint: {shape: "rect", width: 10, depth: 4}})]);
  assert.equal(pointFree(rect, 90, 96), false, "rect from cx - w / 2, half-open");
  assert.equal(pointFree(rect, 110, 98), true);
  const turned = make([object({footprint: {shape: "rect", width: 10, depth: 2, rotate: 90}})]);
  assert.equal(pointFree(turned, 100, 109), false, "a rotated rect becomes its corner polygon");
  assert.equal(pointFree(turned, 109, 100), true);
});

test("material grids block solid cells, and liquid or hazard cells unless walkable", () => {
  assert.equal(materialBlocks({class: "solid"}), true);
  assert.equal(materialBlocks({class: "solid", walkable: true}), true);
  assert.equal(materialBlocks({class: "liquid"}), true);
  assert.equal(materialBlocks({class: "hazard", walkable: true}), false);
  assert.equal(materialBlocks({class: "one_way"}), false, "one_way only stops side-scroll bodies from above");
  assert.equal(materialBlocks({class: "decor"}), false);
  // 4 x 3 cells over a 40 x 30 world; cell (1, 1) is k = 5.
  const bits = btoa(String.fromCharCode(1 << 5, 0));
  const world = createMapRuntime({world: {width: 40, height: 30}, collision: {actorRadius: 0}},
    {materialGrid: {width: 4, height: 3, cellWidth: 10, cellHeight: 10, bits}});
  assert.equal(pointFree(world, 15, 15), false);
  assert.equal(pointFree(world, 10, 10), false);
  assert.equal(pointFree(world, 20, 15), true);
  assert.equal(pointFree(world, 9.999, 15), true);
  const rgba = new Uint8ClampedArray(4 * 3 * 4);
  rgba.set([10, 20, 200, 255], 5 * 4);
  rgba.set([200, 0, 0, 255], 6 * 4);
  rgba.set([10, 20, 200, 0], 7 * 4);
  const fromPixels = materialGridFromRGBA(rgba, 4, 3, {materials: {
    water: {class: "liquid", color: "#0a14c8"}, flowers: {class: "decor", color: [200, 0, 0]},
  }}, 40, 30);
  assert.deepEqual([...fromPixels.bytes], [1 << 5, 0], "alpha 0 has no material; decor never blocks");
  assert.deepEqual(decodeMaterialGrid({...fromPixels, bits: fromPixels.bytes}).bytes, fromPixels.bytes);
});

test("the nav cell is max(1, round half up (r / 2))", () => {
  assert.equal(navCellSize(0), 1);
  assert.equal(navCellSize(1), 1);
  assert.equal(navCellSize(3), 2);
  assert.equal(navCellSize(4), 2);
  assert.equal(navCellSize(5), 3, "2.5 rounds up, not to even");
  assert.equal(navCellSize(6.9), 3);
  assert.equal(navCellSize(7), 4);
  const world = corridor();
  const nav = navGrid(world);
  assert.deepEqual([nav.cell, nav.cols, nav.rows], [3, 67, 40]);
  assert.deepEqual(cellCenter(nav, 67 + 2), [7.5, 4.5]);
});

test("segmentClear samples every cell / 2 including both ends", () => {
  // r = 0: cell 1, samples 0.5 apart. A wall covering x in [10, 10.6) is hit by the sample at 10.0 or 10.5.
  const make = (x, w) => createMapRuntime({world: {width: 40, height: 20}, collision: {actorRadius: 0,
    solids: [{shape: "rect", x, y: 0, w, h: 20}]}});
  assert.equal(segmentClear(make(10, 0.6), 5, 5, 15, 5), false);
  assert.equal(segmentClear(make(10.1, 0.3), 5, 5, 15, 5), true, "thinner than the spacing and between samples");
  assert.equal(segmentClear(make(15, 1), 5, 5, 15, 5), false, "the end point is sampled");
  assert.equal(segmentClear(make(30, 1), 5, 5, 5, 5), true, "a zero-length segment checks its point");
});

// ------------------------------------------------------------------- navigation

test("the grid search uses 4-neighbour moves whose segment is clear, so it cannot jump a thin wall", () => {
  // r = 0, cell 1: centres 10.5 and 11.5 are free, the wall [10.9, 11.1) covers the midpoint 11.0.
  const world = createMapRuntime({world: {width: 30, height: 10}, collision: {actorRadius: 0,
    solids: [{shape: "rect", x: 10.9, y: 0, w: 0.2, h: 10}]}});
  assert.equal(cellValid(world, 10), true);
  assert.equal(cellValid(world, 11), true);
  const field = flood(world, 2.5, 5.5);
  assert.ok(field.dist[3 * 30 + 5] >= 0);
  assert.equal(field.dist[3 * 30 + 20], -1, "the east side is unreachable");
  assert.equal(findPath(world, 2.5, 5.5, 20.5, 5.5), null);
});

test("findPath routes around the wall and every leg is clear", () => {
  const world = corridor();
  const path = findPath(world, 30, 60, 160, 60);
  assert.ok(path && path.length >= 2);
  assert.deepEqual(path.at(-1), {x: 160, y: 60, clear: true}, "a standable goal is reached exactly");
  assert.ok(path.every((point) => point.clear === true), "planned waypoints are flagged clear");
  let x = 30, y = 60;
  for (const point of path) {
    assert.equal(segmentClear(world, x, y, point.x, point.y), true);
    x = point.x;
    y = point.y;
  }
  assert.ok(path.some((point) => point.y >= 80), "the route passes the gap below the wall");
  const snapped = findPath(world, 30, 60, 100, 40);
  assert.ok(snapped && isBlocked(world, 100, 40), "a goal inside a solid snaps to a reachable cell nearby");
  assert.equal(isBlocked(world, snapped.at(-1).x, snapped.at(-1).y), false);
});

// ------------------------------------------------------------------- walker (acceptance: B17-T1)

test("a blocked walker stays put", () => {
  const world = corridor();
  assert.equal(isBlocked(world, 89.5, 40), false);
  for (const budget of [BUDGET, 5, 40]) {
    assert.deepEqual(moveWithCollision(world, 89.5, 40, budget, 0), {x: 89.5, y: 40});
    const actor = createActor(world, 89.5, 40);
    for (let tick = 0; tick < 30; tick++) {
      const step = stepActor(world, actor, {x: 1, y: 0}, budget);
      assert.equal(step.travelled, 0);
      assert.equal(step.blocked, true);
      assert.deepEqual([actor.x, actor.y], [89.5, 40]);
    }
  }
  const slide = moveWithCollision(world, 89.5, 40, 2, 2);
  assert.equal(slide.x, 89.5, "a diagonal push into the wall slides along it");
  assert.ok(slide.y > 40);
  const pinned = createActor(world, 30, 60);
  pinned.path = [{x: 100, y: 40}];
  const step = stepActor(world, pinned, null, BUDGET);
  assert.ok(step.travelled > 0, "following a path into a wall moves until the wall");
});

// Distance from (x, y) to the polyline start -> path[0] -> path[1] ...
function polylineDistance(x, y, start, path) {
  let best = Infinity, ax = start.x, ay = start.y;
  for (const point of path) {
    const dx = point.x - ax, dy = point.y - ay, length = dx * dx + dy * dy;
    const t = length ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / length)) : 0;
    best = Math.min(best, Math.hypot(ax + t * dx - x, ay + t * dy - y));
    ax = point.x;
    ay = point.y;
  }
  return best;
}

test("the movement budget is never exceeded and there are no zero-motion ticks", () => {
  const world = corridor();
  for (const [gx, gy] of [[160, 60], [150, 50], [60, 30], [186, 60], [30, 100]]) {
    for (const budget of [BUDGET, 0.37, 3]) {
      const actor = createActor(world, 30, 60);
      actor.path = findPath(world, 30, 60, gx, gy);
      assert.ok(actor.path, `path to ${gx},${gy}`);
      const planned = actor.path, goal = planned.at(-1);
      let ticks = 0;
      walkPath(world, actor, budget, (step) => {
        ticks++;
        assert.ok(step.travelled <= budget + 1e-12, `travelled ${step.travelled} > budget ${budget}`);
        assert.ok(step.travelled > 1e-9 || step.arrived, "no idle tick before arrival");
        assert.equal(step.blocked, false);
        assert.ok(polylineDistance(actor.x, actor.y, {x: 30, y: 60}, planned) < 1e-6, "the walker stays on the plan");
      });
      assert.equal(actor.path, null);
      assert.ok(Math.abs(actor.x - goal.x) < 1e-6 && Math.abs(actor.y - goal.y) < 1e-6);
      assert.equal(isBlocked(world, actor.x, actor.y), false, "it stops on a valid position");
      assert.ok(ticks > 0);
    }
  }
});

test("a planned path is walked as planned even where tick positions fall between its samples", () => {
  // r = 0, cell 1, samples 0.5 apart: a 0.3 px wall between the samples at 5.0 and 5.5 is
  // invisible to segmentClear (the Appendix C sampling limit), so the plan crosses it. The walker
  // follows the plan; re-checking each tick position would stall it inside the plan instead.
  const world = createMapRuntime({world: {width: 12, height: 12}, collision: {actorRadius: 0,
    solids: [{shape: "rect", x: 5.1, y: 0, w: 0.3, h: 12}]}});
  const path = findPath(world, 0.5, 5.5, 10.5, 5.5);
  assert.ok(path);
  assert.equal(moveWithCollision(world, 4.9, 5.5, 0.3, 0).x, 4.9, "a free step onto the wall is refused");
  const actor = createActor(world, 0.5, 5.5);
  actor.path = path;
  let stalled = 0;
  walkPath(world, actor, 0.3, (step) => {
    if (step.travelled <= 1e-9 && !step.arrived) stalled++;
  });
  assert.equal(stalled, 0);
  assert.deepEqual([actor.x, actor.y], [10.5, 5.5]);
});

test("advanceAlongPath carries the budget across waypoints without changing the input path", () => {
  const world = corridor();
  const start = {x: 30, y: 60};
  const corner = [{x: 31, y: 60}, {x: 31, y: 70}];
  const step = advanceAlongPath(world, start, corner, 3);
  assert.ok(Math.abs(step.x - 31) < 1e-9 && Math.abs(step.y - 62) < 1e-9);
  assert.ok(Math.abs(step.travelled - 3) < 1e-9);
  assert.equal(step.path.length, 1);
  assert.equal(corner.length, 2, "the input path is untouched");
  assert.equal(advanceAlongPath(world, start, [{...start}, {...start}, {x: 40, y: 60}], 2).travelled, 2);
  const short = advanceAlongPath(world, start, [{x: 30.2, y: 60}], 5);
  assert.equal(short.path.length, 0);
  assert.ok(Math.abs(short.x - 30.2) < 1e-12);
  assert.equal(advanceAlongPath(world, short, short.path, 5).travelled, 0);
});

// ------------------------------------------------------------------- exits and latches

test("walking inward never exits; outward intent within the radius does, even against the border", () => {
  const world = corridor();
  const portal = world.portalById.get("exit-east");
  const inside = createActor(world, 186, 60);
  assert.deepEqual([...inside.latched], ["exit-east"], "arriving 6 px from the trigger latches it");
  for (const intent of [{x: -1, y: 0}, {x: 0, y: 1}, {x: 0, y: -1}, {x: 0, y: 0}, {x: -1, y: 3}]) {
    assert.equal(exitCandidate(world, 186, 60, intent.x, intent.y, null), null, JSON.stringify(intent));
  }
  const walker = createActor(world, 186, 60);
  walker.latched.clear();
  for (let tick = 0; tick < 60; tick++) assert.equal(stepActor(world, walker, {x: -1, y: 0}, BUDGET).fired, null);
  assert.ok(walker.x < 120, "the walker really walked inward, up to the wall");
  assert.equal(exitCandidate(world, 186, 60, 1, 0, null), portal);
  assert.equal(exitCandidate(world, 186, 60, 1, 3.8, null), portal, "cos = 0.254 > 0.25");
  assert.equal(exitCandidate(world, 186, 60, 1, 3.9, null), null, "cos = 0.248 <= 0.25");
  assert.equal(INTENT_MIN_COS, 0.25);
  assert.equal(exitCandidate(world, 180, 60, 1, 0, null), null, "12 px away is outside radius 10");
  const border = createActor(world, 193.9, 60);
  border.latched.clear();
  const fired = stepActor(world, border, {x: 1, y: 0}, BUDGET);
  assert.equal(fired.travelled, 0, "pinned against the map border");
  assert.equal(fired.fired, portal);
});

test("an arrival inside the zone is latched until the actor departs; a fired exit latches too", () => {
  const world = corridor({spawns: [{id: "west", x: 30, y: 60}, {id: "east", x: 185, y: 60}]});
  const portal = world.portalById.get("exit-east");
  const actor = createActor(world, 185, 60);
  assert.deepEqual([...actor.latched], ["exit-east"]);
  for (let tick = 0; tick < 30; tick++) assert.equal(stepActor(world, actor, {x: 1, y: 0}, BUDGET).fired, null);
  let left = false;
  for (let tick = 0; tick < 120 && !left; tick++) {
    stepActor(world, actor, {x: -1, y: 0}, BUDGET);
    left = !actor.latched.has("exit-east");
  }
  assert.ok(left, "walking out of the zone releases the latch");
  let fires = 0;
  for (let tick = 0; tick < 240; tick++) if (stepActor(world, actor, {x: 1, y: 0}, BUDGET).fired === portal) fires++;
  assert.equal(fires, 1, "holding the key fires once, then the exit stays latched");
  assert.equal(returnSpawn(world, portal).id, "east");
  arrive(world, actor, 30, 60);
  assert.deepEqual([...actor.latched], []);
  const unlatched = createMapRuntime(corridorBundle({portals: [{...corridorBundle().portals[0], latch: false}]}));
  assert.equal(createActor(unlatched, 185, 60).latched.size, 0, "latch false: arrival does not latch");
});

test("crossing portals fire inside the trigger with movement, or without it when requiresMovement is false", () => {
  const pad = (requiresMovement) => createMapRuntime(corridorBundle({portals: [
    {id: "pad", circle: [40, 100, 8], to: "cellar", requiresMovement},
  ]}));
  const world = pad(undefined);
  assert.equal(exitCandidate(world, 40, 100, 0, 0, null), null);
  assert.equal(exitCandidate(world, 40, 100, 0, -1, null).id, "pad");
  assert.equal(exitCandidate(world, 48, 100, 1, 0, null), null, "the circle is open");
  assert.equal(exitCandidate(pad(false), 40, 100, 0, 0, null).id, "pad");
  const actor = createActor(world, 40, 80);
  actor.path = findPath(world, 40, 80, 40, 100);
  let fired = null;
  walkPath(world, actor, BUDGET, (step) => {
    fired = fired || step.fired;
  });
  assert.equal(fired && fired.id, "pad", "walking into the trigger fires it");
});

// ------------------------------------------------------------------- route check

test("traverseRoutes reaches every exit, interaction, approach point and slot", () => {
  const world = corridor();
  const report = traverseRoutes(world, {speed: 112});
  assert.equal(report.ok, true, JSON.stringify(report.results.filter((item) => !item.ok)));
  assert.deepEqual(report.results.map((item) => [item.kind, item.target]), [
    ["exit", "exit-east"], ["interaction", "sign"], ["approach", "well.approach[0]"],
    ["slot", "well.slot[0]"], ["slot", "well.slot[1]"],
  ]);
  const exit = report.results[0];
  assert.equal(exit.fired, true);
  assert.equal(exit.inwardFired, false);
  for (const item of report.results) {
    assert.equal(item.zeroMotionTicks, 0);
    assert.ok(item.maxStep <= report.budget + 1e-3);
  }
  assert.deepEqual(report.results[2].end, [150, 50]);
  assert.deepEqual(report.portals, [{id: "exit-east", activation: "intent", to: "meadow:west", arrivals: [
    {from: "meadow", spawn: "east", found: true, insideTrigger: false, inZone: false, latchedOnArrival: false},
  ]}]);
  assert.deepEqual(traverseRoutes(corridor(), {speed: 112}), report, "deterministic");
});

test("traverseRoutes leaves an arrival latch before using the exit", () => {
  const world = corridor({spawns: [{id: "door", x: 185, y: 60}]});
  const report = traverseRoutes(world, {speed: 90});
  const exit = report.results.find((item) => item.target === "exit-east");
  assert.equal(exit.from, "door");
  assert.equal(exit.ok, true, exit.reason);
});

test("traverseRoutes reports unreachable targets, inward exits, blocked spawns and bad arrivals", () => {
  const walled = corridor({collision: {actorRadius: 6, solids: [{shape: "rect", x: 96, y: 0, w: 8, h: 120}]}});
  const cut = traverseRoutes(walled, {speed: 112});
  assert.equal(cut.ok, true, "every target is reachable from some spawn");
  assert.equal(cut.results.find((item) => item.target === "sign").from, "west");
  const exit = cut.results.find((item) => item.target === "exit-east");
  assert.equal(exit.from, "east", "the west spawn cannot reach it, the east spawn can");
  assert.equal(exit.ok, true);
  const lonely = traverseRoutes(corridor({collision: {actorRadius: 6, solids: [{shape: "rect", x: 96, y: 0, w: 8, h: 120}]},
    spawns: [{id: "west", x: 30, y: 60}]}), {speed: 112});
  const unreachable = lonely.results.find((item) => item.target === "exit-east");
  assert.equal(unreachable.reachable, false);
  assert.equal(unreachable.reason, "unreachable from every spawn");
  const boxed = traverseRoutes(corridor({collision: {actorRadius: 6, solids: [
    {shape: "rect", x: 96, y: 0, w: 8, h: 80}, {shape: "rect", x: 170, y: 0, w: 30, h: 120},
  ]}}), {speed: 112});
  const sealed = boxed.results.find((item) => item.target === "exit-east");
  assert.equal(sealed.ok, false, "an exit whose zone holds no standable cell is unreachable");
  assert.equal(sealed.reachable, false);
  const blockedSpawn = traverseRoutes(corridor({spawns: [{id: "wall", x: 100, y: 40}, {id: "west", x: 30, y: 60}]}),
    {speed: 112});
  assert.equal(blockedSpawn.ok, false);
  assert.deepEqual(blockedSpawn.spawns[0], {id: "wall", valid: false, reachableCells: 0});
  const bounce = corridor({spawns: [{id: "west", x: 30, y: 60}, {id: "east", x: 195, y: 60}]});
  const arrivals = portalArrivals(bounce);
  assert.equal(arrivals[0].arrivals[0].insideTrigger, true);
  assert.equal(traverseRoutes(bounce, {speed: 112}).ok, false, "an arrival spawn inside its own trigger fails");
  const missing = portalArrivals(corridor({spawns: [{id: "west", x: 30, y: 60}]}));
  assert.equal(missing[0].arrivals[0].found, false);
});

// ------------------------------------------------------------------- draw order, snapshot, validation

test("draw order is (sortY or y, x, id, bundle order) and the actor goes in front on ties", () => {
  const objects = [
    {id: "b", x: 10, y: 50}, {id: "a", x: 10, y: 50}, {id: "c", x: 5, y: 50},
    {id: "tall", x: 0, y: 40, sortY: 60}, {id: "low", x: 0, y: 20},
  ];
  assert.deepEqual(sortForDrawing(objects).map((object) => object.id), ["low", "c", "a", "b", "tall"]);
  const keys = [20, 50, 50, 50, 60];
  assert.equal(actorDrawIndex(keys, 50), 4);
  assert.equal(actorDrawIndex(keys, 49.9), 1);
  assert.equal(actorDrawIndex(keys, 99), 5);
  assert.equal(actorDrawIndex([], 1), 0);
});

test("runtimeSnapshot is plain JSON with the schema id", () => {
  const world = corridor();
  const actor = createActor(world, 30, 60, [1, 0]);
  const snapshot = runtimeSnapshot(world, actor, {tick: 3});
  assert.equal(snapshot.schema, SNAPSHOT_SCHEMA);
  assert.deepEqual(snapshot.actor, {x: 30, y: 60, valid: true, facing: [1, 0], latched: [], travelled: 0, pathLength: 0});
  assert.equal(snapshot.world.cell, 3);
  assert.equal(snapshot.tick, 3);
  assert.deepEqual(JSON.parse(JSON.stringify(snapshot)), snapshot);
});

test("malformed bundles fail with the field path", () => {
  const cases = [
    [{collision: {actorRadius: 1}}, /world.width/],
    [{world: {width: 10, height: 10}}, /collision must be an object/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: NaN}}, /actorRadius must be a finite number/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1, ySquash: 0}}, /ySquash must be positive/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1, walkRegions: [{polygon: [[0, 0], [1, 1]]}]}},
      /walkRegions\[0\].polygon must list at least 3/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1, solids: [{shape: "circle"}]}}, /solids\[0\].shape/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1},
      portals: [{id: "p", to: "x", rect: [0, 0, 1, 1], circle: [0, 0, 1]}]}, /exactly one of rect/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1},
      portals: [{id: "p", to: "x", rect: [0, 0, 1, 1], activation: "intent", travelDirection: [1, 0]}]},
      /need travelDirection and radius/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1},
      portals: [{id: "p", to: "x", rect: [0, 0, 1, 1], travelDirection: [0, 0]}]}, /must not be zero/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1},
      portals: [{id: "p", to: "x", rect: [0, 0, 1, 1]}, {id: "p", to: "y", circle: [5, 5, 1]}]}, /duplicate portal id/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1},
      objects: [{id: "o", x: 1, y: 1, scale: 0, footprint: {shape: "rect", width: 1, depth: 1}}]}, /scale must be positive/],
  ];
  for (const [bundle, pattern] of cases) assert.throws(() => createMapRuntime(bundle), pattern);
  assert.throws(() => traverseRoutes(corridor(), {speed: 0}), /speed must be positive/);
  assert.throws(() => decodeMaterialGrid({width: 2, height: 2, cellWidth: 1, cellHeight: 1, bits: ""}), /bits/);
});
