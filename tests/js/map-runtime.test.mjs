// Node tests for skills/generate2dmap/references/runtime/map-runtime.mjs (B17-T1; forge_nav rules N1-N15).
// Run: node --test tests/js/map-runtime.test.mjs (tests/test_map_runtime_js.py runs it under pytest and
// compares the same queries with forge_nav itself).
import test from "node:test";
import assert from "node:assert/strict";

import {
  BLOCK, FOOTPRINT_DIRECTIONS, FREE, INTENT_MIN_COS, ONE_WAY, SNAPSHOT_SCHEMA, TICK_HZ, actorDrawIndex,
  advanceAlongPath, arrive, cellCenter, cellValid, createActor, createMapRuntime, decodeMaterialGrid, exitCandidate,
  exitTarget, findPath, flood, floodFrom, footprintSamples, footprintSolid, insidePolygon, isBlocked, isValid,
  joinCells, materialAt, materialBlocks, materialCode, materialGridFromRGBA, moveOpen, moveWithCollision, navCellSize,
  navGrid, objectSolid, onPolygonEdge, pointFree, pointTarget, portalArrivals, returnSpawn, routeStarts,
  runtimeSnapshot, segmentClear, segmentStatus, sortForDrawing, stepActor, traverseRoutes,
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

const open = (solids, extra = {}) => createMapRuntime({world: {width: 200, height: 200},
  collision: {actorRadius: 0, solids, ...extra}});

// ------------------------------------------------------------------- N3-N5: points

test("rect solids and collision.rects are closed sets (D1, N5)", () => {
  const world = open([{shape: "rect", x: 10, y: 10, w: 10, h: 10}], {rects: [[50, 50, 5, 5]]});
  assert.equal(pointFree(world, 10, 10), false);
  assert.equal(pointFree(world, 20, 15), false, "the right edge x = x + w is blocked");
  assert.equal(pointFree(world, 15, 20), false, "the bottom edge is blocked");
  assert.equal(pointFree(world, 20.001, 15), true);
  assert.equal(pointFree(world, 9.999, 15), true);
  assert.equal(pointFree(world, 55, 52), false, "collision.rects are exact closed blocking rectangles (D3)");
  assert.equal(pointFree(world, 55.001, 52), true);
});

test("ellipses are closed, rotate clockwise in degrees; shapes without area block nothing (N4, N5)", () => {
  const world = open([
    {shape: "ellipse", cx: 50, cy: 50, rx: 10, ry: 5},
    {shape: "ellipse", cx: 150, cy: 50, rx: 10, ry: 5, rotate: 90},
    {shape: "ellipse", cx: 50, cy: 150, rx: 0, ry: 5},
    {shape: "rect", x: 20, y: 80, w: 0, h: 10},
    {shape: "rect", x: 30, y: 80, w: -4, h: 10},
  ]);
  assert.equal(pointFree(world, 60, 50), false, "nu * nu + nv * nv <= 1");
  assert.equal(pointFree(world, 60.001, 50), true);
  assert.equal(pointFree(world, 50, 55), false);
  assert.equal(pointFree(world, 50, 55.001), true);
  assert.equal(pointFree(world, 150, 59.9), false, "rotated 90 degrees: the long axis points down");
  assert.equal(pointFree(world, 159.9, 50), true);
  assert.equal(pointFree(world, 50, 150), true, "rx 0 has no area");
  assert.equal(pointFree(world, 20, 85), true, "a zero-width rect blocks nothing, not even its line");
  assert.equal(world.solids.length, 2);
});

test("polygon solids are closed; walk regions use the even-odd rule with their own holes (N3, N5)", () => {
  const concave = [[0, 0], [30, 0], [30, 30], [20, 30], [20, 10], [10, 10], [10, 30], [0, 30]];
  const xs = Float64Array.from(concave, (p) => p[0]), ys = Float64Array.from(concave, (p) => p[1]);
  assert.equal(insidePolygon(xs, ys, 5, 20), true);
  assert.equal(insidePolygon(xs, ys, 15, 20), false, "the notch is outside");
  assert.equal(insidePolygon(xs, ys, 0, 0), true, "pnpoly: the left and top boundaries count as inside");
  assert.equal(insidePolygon(xs, ys, 30, 5), false, "pnpoly: the right boundary does not");
  assert.equal(onPolygonEdge(xs, ys, 30, 5), true);
  const solid = open([{shape: "polygon", points: concave}]);
  assert.equal(pointFree(solid, 30, 5), false, "a polygon solid's right edge is blocked");
  assert.equal(pointFree(solid, 15, 10), false, "and the notch's edge");
  assert.equal(pointFree(solid, 15, 20), true, "the notch interior is free");
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
  assert.throws(() => open([{shape: "polygon", points: [[0, 0], [5, 5], [10, 10]]}]), /zero area/);
});

test("without walk regions the walk area is the closed box [0, W] x [0, H] (N3)", () => {
  const world = createMapRuntime({world: {width: 50, height: 40}, collision: {actorRadius: 0}});
  assert.equal(pointFree(world, 0, 0), true);
  assert.equal(pointFree(world, 50, 40), true);
  assert.equal(pointFree(world, 50.001, 10), false);
  assert.equal(pointFree(world, 10, 40.001), false);
  assert.equal(pointFree(world, -0.001, 10), false);
  assert.equal(pointFree(world, NaN, 10), false, "NaN is never free");
});

test("the footprint is the point plus 8 samples on the ySquash ellipse, in forge_nav's order (N2, N9)", () => {
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
  assert.deepEqual(samples[2], [50 + 10 * Math.SQRT1_2, 57 + 5 * Math.SQRT1_2]);
  assert.deepEqual(samples[7], [50, 52], "north sample at y - r * ySquash");
  assert.equal(isBlocked(round, 50, 57), true, "the round footprint reaches y = 47");
  assert.equal(isBlocked(squashed, 50, 57.001), false, "the squashed footprint stops just below y = 50");
  assert.equal(isBlocked(squashed, 50, 55), true, "y = 50 is the rect's closed bottom edge");
  assert.equal(isValid(squashed, 50, 57.001), true);
});

// ------------------------------------------------------------------- N6: footprints

test("object footprints are scaled once; world_px is not scaled; solid false and none do not block (N6, D7)", () => {
  const object = (extra) => ({id: "tree", prop: "tree", x: 100, y: 100, anchor_px: [8, 30], scale: 2,
    footprint: {shape: "ellipse", width: 10, depth: 4, offset: [0, -2]}, ...extra});
  const make = (objects) => createMapRuntime({world: {width: 200, height: 200}, collision: {actorRadius: 0}, objects});
  const scaled = make([object()]);
  assert.equal(pointFree(scaled, 100, 96), false, "centre moves by offset * scale");
  assert.equal(pointFree(scaled, 110, 96), false, "rx = width * scale / 2 = 10, closed");
  assert.equal(pointFree(scaled, 110.1, 96), true);
  const world = make([object({footprint: {shape: "ellipse", width: 10, depth: 4, offset: [0, -2], basis: "world_px"}})]);
  assert.equal(pointFree(world, 104.9, 98), false, "world_px: rx = 5, offset not scaled");
  assert.equal(pointFree(world, 105.1, 98), true);
  const legacy = make([object({footprint: {shape: "ellipse", width: 10, depth: 4, offset: [0, -2], basis: "image_px"}})]);
  assert.equal(pointFree(legacy, 109.9, 96), false, "image_px is the legacy alias of prop_px: scaled");
  assert.throws(() => make([object({footprint: {shape: "ellipse", width: 1, depth: 1, basis: "metres"}})]), /basis/);
  assert.equal(pointFree(make([object({solid: false})]), 100, 96), true);
  assert.equal(pointFree(make([object({footprint: {shape: "none"}})]), 100, 96), true);
  const rect = make([object({footprint: {shape: "rect", width: 10, depth: 4}})]);
  assert.equal(pointFree(rect, 90, 96), false, "rect from cx - w / 2, closed");
  assert.equal(pointFree(rect, 110, 98), false, "its right edge is blocked too");
  assert.equal(pointFree(rect, 110.01, 98), true);
  const turned = make([object({footprint: {shape: "rect", width: 10, depth: 2, rotate: 90}})]);
  assert.equal(pointFree(turned, 100, 109), false, "a rotated rect becomes its corner polygon");
  assert.equal(pointFree(turned, 109, 100), true);
  const flat = make([object({footprint: {shape: "rect", width: 0, depth: 4}})]);
  assert.equal(flat.solids.length, 0, "a footprint without area blocks nothing");
});

test("flip_x mirrors the footprint around the anchor x: offset x and rotate change sign (D6, N6)", () => {
  const make = (flip) => createMapRuntime({world: {width: 200, height: 200}, collision: {actorRadius: 0}, objects: [
    {id: "o", prop: "p", x: 100, y: 100, anchor_px: [4, 8], flip_x: flip,
      footprint: {shape: "ellipse", width: 4, depth: 2, offset: [10, -3]}}]});
  assert.equal(pointFree(make(false), 110, 97), false);
  assert.equal(pointFree(make(false), 90, 97), true);
  assert.equal(pointFree(make(true), 90, 97), false, "the mirrored footprint sits at x - 10");
  assert.equal(pointFree(make(true), 110, 97), true);
  const plain = footprintSolid(0, 0, {shape: "rect", width: 4, depth: 2, offset: [3, 0], rotate: 30}, {flipX: true});
  const mirror = footprintSolid(0, 0, {shape: "rect", width: 4, depth: 2, offset: [-3, 0], rotate: -30});
  assert.deepEqual([...plain.xs], [...mirror.xs]);
  assert.deepEqual([...plain.ys], [...mirror.ys]);
  assert.throws(() => make("yes"), /flip_x must be true or false/);
});

test("footprints and solid come from the object, else from the props registry (N6)", () => {
  const bundle = (objects, props) => ({world: {width: 100, height: 100}, collision: {actorRadius: 0}, objects, props});
  const props = {
    tree: {image: "tree.png", footprint: {shape: "ellipse", width: 6, depth: 2}},
    rock: {image: "rock.png", solid: false, footprint: {shape: "rect", width: 6, depth: 2}},
  };
  const world = createMapRuntime(bundle([
    {id: "a", prop: "tree", x: 20, y: 20, anchor_px: [0, 0]},
    {id: "b", prop: "rock", x: 50, y: 20, anchor_px: [0, 0]},
    {id: "c", prop: "rock", x: 80, y: 20, anchor_px: [0, 0], solid: true},
    {id: "d", prop: "tree", x: 20, y: 60, anchor_px: [0, 0], footprint: null},
  ], props));
  assert.equal(pointFree(world, 22, 20), false, "the registry footprint");
  assert.equal(pointFree(world, 50, 20), true, "the registry's solid: false");
  assert.equal(pointFree(world, 80, 20), false, "the object's own solid flag wins");
  assert.equal(pointFree(world, 20, 60), true, "an explicit null footprint is no footprint");
  assert.equal(objectSolid({id: "x", x: 0, y: 0}, props.tree).kind, "ellipse");
  assert.throws(() => createMapRuntime(bundle([{id: "a", prop: "ghost", x: 1, y: 1, anchor_px: [0, 0]}], props)),
    /unknown prop "ghost"/);
  assert.throws(() => createMapRuntime(bundle([], {tree: {pack: "pack.json", label: "tree"}})), /resolve it/);
});

// ------------------------------------------------------------------- N8: materials

test("material classes: solid blocks, liquid and hazard unless walkable, decor never, one_way never at a point", () => {
  assert.equal(materialBlocks({class: "solid"}), true);
  assert.equal(materialBlocks({class: "solid", walkable: true}), true);
  assert.equal(materialBlocks({class: "liquid"}), true);
  assert.equal(materialBlocks({class: "hazard", walkable: true}), false);
  assert.equal(materialBlocks({class: "one_way"}), false);
  assert.equal(materialCode({class: "one_way"}), ONE_WAY);
  assert.equal(materialCode({class: "decor"}), FREE);
  assert.equal(materialCode({class: "liquid"}), BLOCK);
  assert.throws(() => materialCode({class: "lava"}), /not one of/);
  assert.throws(() => materialCode({class: "liquid", walkable: "yes"}), /true or false/);
  // 4 x 3 pixels over a 40 x 30 world; pixel (1, 1) is k = 5.
  const bits = btoa(String.fromCharCode(1 << 5, 0));
  const world = createMapRuntime({world: {width: 40, height: 30}, collision: {actorRadius: 0}},
    {materialGrid: {width: 4, height: 3, cellWidth: 10, cellHeight: 10, bits}});
  assert.equal(pointFree(world, 15, 15), false);
  assert.equal(pointFree(world, 10, 10), false);
  assert.equal(pointFree(world, 20, 15), true, "pixel squares are half-open: x = 20 reads the next pixel");
  assert.equal(pointFree(world, 9.999, 15), true);
  assert.throws(() => createMapRuntime({world: {width: 41, height: 30}, collision: {actorRadius: 0}},
    {materialGrid: {width: 4, height: 3, cellWidth: 10, cellHeight: 10, bits}}), /does not cover/);
  assert.throws(() => decodeMaterialGrid({width: 4, height: 3, cellWidth: 10, cellHeight: 7.5, bits}), /squares/);
});

test("materialGridFromRGBA classifies every opaque pixel by exact colour (N8)", () => {
  const rgba = new Uint8ClampedArray(4 * 3 * 4);
  rgba.set([10, 20, 200, 255], 5 * 4);
  rgba.set([200, 0, 0, 255], 6 * 4);
  rgba.set([10, 20, 200, 0], 7 * 4);
  rgba.set([255, 255, 0, 255], 8 * 4);
  const map = {materials: {water: {class: "liquid", color: "#0a14c8"}, flowers: {class: "decor", color: [200, 0, 0]},
    ledge: {class: "one_way", color: "#ffff00"}}};
  const grid = materialGridFromRGBA(rgba, 4, 3, map, 40, 30);
  assert.deepEqual([...grid.bits], [1 << 5, 0], "alpha 0 has no material; decor never blocks");
  assert.deepEqual([...grid.oneWay], [0, 1], "pixel 8 is one_way");
  const decoded = decodeMaterialGrid(grid);
  assert.deepEqual([decoded.codes[5], decoded.codes[6], decoded.codes[8]], [BLOCK, FREE, ONE_WAY]);
  assert.equal(decoded.hasOneWay, true);
  rgba.set([1, 2, 3, 255], 0);
  assert.throws(() => materialGridFromRGBA(rgba, 4, 3, map, 40, 30), /matches no material/);
  assert.throws(() => materialGridFromRGBA(new Uint8Array(48), 4, 3, map, 30, 30), /whole squares/);
  assert.throws(() => materialGridFromRGBA(new Uint8Array(48), 4, 3, {materials: {a: {class: "solid"}}}, 40, 30), /color/);
});

// ------------------------------------------------------------------- N10, N11: segments

test("segmentClear samples every cell / 2, then applies the thin-gap rule to the centre path (N10)", () => {
  // r = 0: cell 1, samples 0.5 apart.
  const make = (x, w) => open([{shape: "rect", x, y: 0, w, h: 100}]);
  assert.equal(segmentClear(make(10, 0.6), 5, 5, 15, 5), false, "a sample lands in the wall");
  const thin = make(10.1, 0.3);
  assert.equal(segmentClear(thin, 5, 5, 15, 5), false, "the thin-gap rule closes a wall between two samples");
  assert.equal(segmentClear(thin, 5, 5, 15, 5, {thinGap: false}), true, "the sampled rule alone misses it");
  assert.match(segmentStatus(thin, 5, 5, 15, 5), /thin-gap/);
  assert.equal(segmentClear(make(15, 1), 5, 5, 15, 5), false, "the end point is sampled");
  assert.equal(segmentClear(make(30, 1), 5, 5, 5, 5), true, "a zero-length segment checks its point");
  const corner = open([{shape: "rect", x: 10, y: 0, w: 5, h: 10}]);
  assert.equal(segmentClear(corner, 0, 0, 20, 20), true, "touching the corner (10, 10) at a single point is not a failure");
  assert.equal(segmentClear(corner, 0, 20, 20, 0), false, "this diagonal enters the rect after its corner");
});

function ledgeWorld() {
  // A side view: a one_way ledge row at pixel row 3 (y in [12, 16)) across the middle of the room.
  const width = 10, height = 8, rgba = new Uint8Array(width * height * 4);
  for (let k = 0; k < width * height; k++) rgba.set([64, 160, 64, 255], k * 4);
  for (let i = 2; i < 8; i++) rgba.set([224, 224, 0, 255], (3 * width + i) * 4);
  const map = {materials: {air: {class: "decor", color: "#40a040"}, ledge: {class: "one_way", color: "#e0e000"}}};
  return createMapRuntime({world: {width: 40, height: 32}, collision: {actorRadius: 0}},
    {materialGrid: materialGridFromRGBA(rgba, width, height, map, 40, 32)});
}

test("one_way blocks moving down onto it, never up or sideways, and never a point (N11)", () => {
  const world = ledgeWorld();
  assert.equal(materialAt(world, 20, 13), ONE_WAY);
  assert.equal(pointFree(world, 20, 13), true, "standing on one_way is allowed");
  assert.equal(segmentClear(world, 20, 6, 20, 20), false, "dropping through the ledge is blocked");
  assert.match(segmentStatus(world, 20, 6, 20, 13), /one_way/);
  assert.equal(segmentClear(world, 20, 20, 20, 6), true, "jumping up through it is allowed");
  assert.equal(segmentClear(world, 4, 13, 36, 13), true, "walking along it is allowed");
  assert.equal(segmentClear(world, 4, 6, 4, 20), true, "dropping beside it is allowed");
  const nav = navGrid(world), above = 11 * nav.cols + 20; // node (20.5, 11.5), just above the ledge
  assert.equal(moveOpen(world, above, above + nav.cols), false, "moving down onto the ledge");
  assert.equal(moveOpen(world, above + nav.cols, above), true, "moving up off it");
  assert.equal(moveOpen(world, above + nav.cols, above + 2 * nav.cols), true, "moving down inside it enters nothing");
  const field = flood(world, 20.5, 6.5);
  const [cx, cy] = cellCenter(nav, (20 * nav.cols) + 20);
  assert.ok(cy > 16 && cx === 20.5);
  assert.ok(field.dist[20 * nav.cols + 20] > 0, "the floor below is still reached around the ledge");
  const straight = Math.abs(20 - 6);
  assert.ok(field.dist[20 * nav.cols + 20] > straight, "but not straight down through it");
});

// ------------------------------------------------------------------- N12-N14: grid and reachability

test("the nav cell is max(1, round half up (r / 2)) (N12)", () => {
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
  assert.throws(() => navGrid(createMapRuntime({world: {width: 5000, height: 5000}, collision: {actorRadius: 0}})),
    /navigation grid would have/);
});

test("the grid search uses 4-neighbour moves whose segment is clear, so it cannot jump a thin wall (N13)", () => {
  // r = 0, cell 1: centres 10.5 and 11.5 are free, the wall [10.9, 11.1] covers the midpoint 11.0.
  const world = createMapRuntime({world: {width: 30, height: 10}, collision: {actorRadius: 0,
    solids: [{shape: "rect", x: 10.9, y: 0, w: 0.2, h: 10}]}});
  assert.equal(cellValid(world, 10), true);
  assert.equal(cellValid(world, 11), true);
  const field = flood(world, 2.5, 5.5);
  assert.ok(field.dist[3 * 30 + 5] >= 0);
  assert.equal(field.dist[3 * 30 + 20], -1, "the east side is unreachable");
  assert.equal(findPath(world, 2.5, 5.5, 20.5, 5.5), null);
  // A wall thinner than the midpoint spacing, between the samples 10.5, 11.0 and 11.5: only the thin-gap rule sees it.
  const sliver = createMapRuntime({world: {width: 30, height: 10}, collision: {actorRadius: 0,
    solids: [{shape: "rect", x: 11.1, y: 0, w: 0.2, h: 10}]}});
  assert.equal(flood(sliver, 2.5, 5.5).dist[3 * 30 + 20], -1);
});

test("a start joins every valid node within two cells that it reaches in a straight line (N14)", () => {
  const world = createMapRuntime({world: {width: 20, height: 20}, collision: {actorRadius: 0,
    solids: [{shape: "rect", x: 10, y: 0, w: 0.2, h: 20}]}});
  const joined = joinCells(world, 9.9, 5.5);
  assert.ok(joined.length > 1);
  const nav = navGrid(world);
  assert.ok(joined.every((k) => (k % nav.cols) <= 9), "never across the wall it would have to cross");
  assert.equal(joined[0], 5 * nav.cols + 9, "nearest first");
  assert.deepEqual(joinCells(world, 10.1, 5.5), [], "a point on the wall joins nothing");
  const field = floodFrom(world, [[2.5, 2.5], [15.5, 15.5]]);
  assert.ok(field.dist[2 * nav.cols + 2] === 0 && field.dist[15 * nav.cols + 15] === 0, "every start seeds the search");
  assert.equal(pointTarget(world, field, 10.1, 5).reason, "the actor cannot stand here (footprint blocked)");
  assert.equal(pointTarget(world, field, 3, 3).node >= 0, true);
});

test("crossing exits are reached inside the closed trigger, or by a clear move to its closest point (N14)", () => {
  const world = createMapRuntime({world: {width: 60, height: 30}, collision: {actorRadius: 2,
    solids: [{shape: "rect", x: 50, y: 0, w: 10, h: 30}]},
  portals: [{id: "door", rect: [56, 10, 4, 10], to: "next"}], spawns: [{id: "s", x: 10, y: 15}]});
  const field = flood(world, 10, 15);
  const answer = exitTarget(world, field, world.portals[0]);
  assert.equal(answer.node, -1, "the trigger sits inside a wall: no node can enter it");
  const wide = createMapRuntime({world: {width: 60, height: 30}, collision: {actorRadius: 2},
    portals: [{id: "door", rect: [55, 10, 5, 10], to: "next"}], spawns: [{id: "s", x: 10, y: 15}]});
  const inside = exitTarget(wide, flood(wide, 10, 15), wide.portals[0]);
  assert.ok(inside.node >= 0);
  assert.equal(inside.entry, null, "a valid node centre (x = 57.5) lies inside the closed trigger");
  // A 2 px trigger at the border: every node centre inside it is too close to the edge for the footprint,
  // but the node at x = 57.5 can step onto the trigger's closest point (58, y) in a clear straight line.
  const narrow = createMapRuntime({world: {width: 60, height: 30}, collision: {actorRadius: 2},
    portals: [{id: "door", rect: [58, 10, 2, 10], to: "next"}], spawns: [{id: "s", x: 10, y: 15}]});
  const entry = exitTarget(narrow, flood(narrow, 10, 15), narrow.portals[0]);
  assert.ok(entry.node >= 0);
  assert.equal(entry.entry[0], 58, "entered at the trigger's closest point");
  const report = traverseRoutes(narrow, {speed: 60});
  assert.equal(report.results[0].ok, true, report.results[0].reason);
  assert.equal(report.results[0].fired, true, "the walker steps onto the closed trigger and fires it");
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
  assert.ok(snapped && isBlocked(world, 100, 40), "a goal inside a solid snaps to a reachable node nearby");
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
  const stuck = moveWithCollision(world, 100, 40, 0, 0.5);
  assert.deepEqual(stuck, {x: 100, y: 40}, "inside a wall, a move to another invalid spot is refused");
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
  // r = 4 (cell 2, samples 1 px apart): a sliver [15.2, 15.4] x [0.5, 1.5] sits on the north sample's track
  // y = 1 while the centre walks along y = 5. The samples at x = 15 and 16 miss it and the centre path does
  // not cross it, so the plan passes (N10); a tick position at x = 15.3 would put the north sample inside it.
  const world = createMapRuntime({world: {width: 40, height: 12}, collision: {actorRadius: 4,
    solids: [{shape: "rect", x: 15.2, y: 0.5, w: 0.2, h: 1}]}});
  const path = findPath(world, 5, 5, 35, 5);
  assert.ok(path);
  assert.equal(isValid(world, 15.3, 5), false, "the north sample (15.3, 1) lies in the sliver");
  assert.equal(isValid(world, 15, 5) && isValid(world, 16, 5), true, "the samples around it are valid");
  assert.equal(moveWithCollision(world, 15.1, 5, 0.2, 0).x, 15.1, "a free step onto the sliver is refused");
  const actor = createActor(world, 5, 5);
  actor.path = path;
  let stalled = 0;
  walkPath(world, actor, 0.3, (step) => {
    if (step.travelled <= 1e-9 && !step.arrived) stalled++;
  });
  assert.equal(stalled, 0);
  assert.deepEqual([actor.x, actor.y], [35, 5]);
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

// ------------------------------------------------------------------- exits and latches (N15)

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

test("crossing portals fire inside the closed trigger with movement, or without it when requiresMovement is false", () => {
  const pad = (requiresMovement) => createMapRuntime(corridorBundle({portals: [
    {id: "pad", circle: [40, 100, 8], to: "cellar", ...(requiresMovement === undefined ? {} : {requiresMovement})},
  ]}));
  const world = pad(undefined);
  assert.equal(exitCandidate(world, 40, 100, 0, 0, null), null);
  assert.equal(exitCandidate(world, 40, 100, 0, -1, null).id, "pad");
  assert.equal(exitCandidate(world, 48, 100, 1, 0, null).id, "pad", "the circle is closed (N14 triggers)");
  assert.equal(exitCandidate(world, 48.01, 100, 1, 0, null), null);
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
    {from: "meadow", spawn: "east", found: true, insideTrigger: false, inZone: false, latchedOnArrival: false,
      valid: true, joined: true, bounceBack: []},
  ]}]);
  assert.deepEqual(traverseRoutes(corridor(), {speed: 112}), report, "deterministic");
});

test("an interaction without reach is a point target; arrivals given as points are starts (N14, map_nav)", () => {
  const world = corridor({
    interactions: [{id: "post", x: 60, y: 30}],
    portals: [{...corridorBundle().portals[0], entranceByFrom: {meadow: [150, 100]}}],
    spawns: [{id: "west", x: 30, y: 60}],
  });
  assert.deepEqual(routeStarts(world).map((start) => start.id), ["west", "exit-east<-meadow"]);
  const report = traverseRoutes(world, {speed: 112});
  assert.equal(report.ok, true, JSON.stringify(report.results.filter((item) => !item.ok)));
  const post = report.results.find((item) => item.target === "post");
  assert.deepEqual(post.end, [60, 30], "the walker stands on the point");
  assert.deepEqual(report.spawns[1], {id: "exit-east<-meadow", kind: "arrival", valid: true,
    reachableCells: report.spawns[1].reachableCells});
  assert.deepEqual(returnSpawn(world, world.portals[0]), {id: "exit-east<-meadow", x: 150, y: 100, facing: null});
  const blocked = traverseRoutes(corridor({interactions: [{id: "wall", x: 100, y: 40}]}), {speed: 112});
  const wall = blocked.results.find((item) => item.target === "wall");
  assert.equal(wall.reachable, false);
  assert.match(wall.reason, /cannot stand here/);
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
  assert.match(unreachable.reason, /^unreachable from every start \(no reachable node within the activation radius 10 px\)$/);
  const boxed = traverseRoutes(corridor({collision: {actorRadius: 6, solids: [
    {shape: "rect", x: 96, y: 0, w: 8, h: 80}, {shape: "rect", x: 170, y: 0, w: 30, h: 120},
  ]}}), {speed: 112});
  const sealed = boxed.results.find((item) => item.target === "exit-east");
  assert.equal(sealed.ok, false, "an exit whose zone holds no standable node is unreachable");
  assert.equal(sealed.reachable, false);
  const blockedSpawn = traverseRoutes(corridor({spawns: [{id: "wall", x: 100, y: 40}, {id: "west", x: 30, y: 60}]}),
    {speed: 112});
  assert.equal(blockedSpawn.ok, false);
  assert.deepEqual(blockedSpawn.spawns[0], {id: "wall", valid: false, reachableCells: 0});
  const bounce = corridor({spawns: [{id: "west", x: 30, y: 60}, {id: "east", x: 195, y: 60}]});
  const arrivals = portalArrivals(bounce);
  assert.equal(arrivals[0].arrivals[0].insideTrigger, true);
  assert.deepEqual(arrivals[0].arrivals[0].bounceBack, ["exit-east"]);
  assert.equal(traverseRoutes(bounce, {speed: 112}).ok, false, "an arrival inside a trigger bounces back");
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
  assert.equal(snapshot.world.oneWay, false);
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
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1},
      objects: [{id: "o", x: 1, y: 1, solid: "no", footprint: {shape: "rect", width: 1, depth: 1}}]}, /solid must be true or false/],
    [{world: {width: 10, height: 10}, collision: {actorRadius: 1},
      objects: [{id: "o", x: 1, y: 1, footprint: {shape: "rect", width: -1, depth: 1}}]}, /width must not be negative/],
  ];
  for (const [bundle, pattern] of cases) assert.throws(() => createMapRuntime(bundle), pattern);
  assert.throws(() => traverseRoutes(corridor(), {speed: 0}), /speed must be positive/);
  assert.throws(() => decodeMaterialGrid({width: 2, height: 2, cellWidth: 1, cellHeight: 1, bits: ""}), /bits/);
});
