/*
 * map-runtime.mjs 1.0.0: collision query, navigation grid, walker and exits
 * for generate2dmap scenes (map_bundle.v2).
 *
 * One dependency-free ES module for browsers, Node 18+ and bundlers. Games can
 * import it as it is; build_scene_preview.py inlines it verbatim into
 * preview.html. It reads the bundle fields it needs (world, collision,
 * objects, portals, spawns, interactions, anchors) and never touches the DOM,
 * the network, a clock or a random source: the same calls give the same
 * results.
 *
 * Collision semantics (plan Appendix C, shared with map_nav.py). Coordinates
 * are world pixels, y down. The actor footprint is an ellipse with
 * rx = actorRadius and ry = actorRadius * ySquash (ySquash defaults to 1).
 *
 *   A point is FREE when it lies inside at least one walk region polygon and
 *   outside that region's holes (inside [0, width) x [0, height) when there
 *   are no walk regions), and outside every solid: collision.solids,
 *   collision.rects, object footprints and blocked material_map cells.
 *   A position is VALID (isBlocked() is false) when the point itself and the
 *   8 samples on its footprint ellipse are free. Solids are never inflated by
 *   the actor size: the samples already account for it.
 *
 * Parity rules, so a Python port (map_nav.py) decides every case the same way:
 *   rect          x <= px < x + w and y <= py < y + h (half-open)
 *   ellipse       nu*nu + nv*nv < 1 with nu = u / rx, nv = v / ry; rx or ry
 *                 <= 0 never blocks; rotate is degrees, clockwise on screen:
 *                 theta = rotate * PI / 180, u = dx*cos + dy*sin,
 *                 v = -dx*sin + dy*cos (no rotation math when rotate is 0)
 *   polygon       even-odd: edge (i, j) toggles when (yi > py) != (yj > py)
 *                 and px < xi + (py - yi) * (xj - xi) / (yj - yi)
 *   samples       P, then (x + r*ux, y + ry*uy) with ry = r * ySquash over
 *                 FOOTPRINT_DIRECTIONS (exact unit vectors, no cos/sin)
 *   lengths       sqrt(dx*dx + dy*dy), never hypot
 *   nav grid      cell = max(1, floor(r / 2 + 0.5)); cell (i, j) has its
 *                 centre at ((i + 0.5) * cell, (j + 0.5) * cell); 4-neighbour
 *                 moves between valid centres whose segment is clear
 *   segmentClear  n = max(1, ceil(len / (cell / 2))); samples
 *                 (ax + dx * k / n, ay + dy * k / n) for k = 0..n
 *   footprints    s = 1 for basis "world_px", else the instance scale;
 *                 w = width * s, h = depth * s, cx = x + ox * s,
 *                 cy = y + oy * s; ellipse rx = w / 2, ry = h / 2; an
 *                 unrotated rect is x0 = cx - w / 2, y0 = cy - h / 2 with
 *                 the rect rule above; a rotated rect is its 4-corner polygon
 *   material      cell (floor(px / cellWidth), floor(py / cellHeight)) of the
 *                 grid built from material_map: solid blocks, liquid and
 *                 hazard block unless walkable is true, one_way and decor
 *                 never block a top-down walker
 * Rotated shapes use Math.cos/Math.sin, which may differ from a C runtime's by
 * one ulp (V8 and the MSVC CRT disagree on sin(PI / 4)); points exactly on a
 * rotated edge can then fall on different sides. Everything else is exact.
 *
 * Validity is sampled, so a solid thinner than cell / 2 can slip between the
 * samples of a segment. Planned paths are therefore walked as planned:
 * findPath() flags its waypoints clear: true and advanceAlongPath() follows
 * those legs without re-testing each tick position; keyboard moves and
 * unflagged waypoints go through moveWithCollision().
 *
 * Exits: an "intent" portal fires when the actor is within `radius` of its
 * trigger (rect [x, y, w, h] or circle [cx, cy, r]) and the cosine between the
 * input intent and travelDirection is above 0.25. A "crossing" portal (the
 * default) fires while the actor is inside its trigger and, unless
 * requiresMovement is false, has a non-zero intent. Arriving inside a portal's
 * zone latches it (unless latch is false) and a portal that fired stays
 * latched; a latch releases when the actor leaves the zone.
 */

export const RUNTIME_VERSION = "1.0.0";
export const TICK_HZ = 60;
export const INTENT_MIN_COS = 0.25;
export const SNAPSHOT_SCHEMA = "generate2dmap.scene_snapshot.v1";

const EPS = 1e-9;
const SQRT_HALF = Math.sqrt(0.5);
const INDEX_BUCKET = 32;
const MAX_INDEX_CELLS = 1 << 22;
const SMOOTH_LOOKAHEAD = 64;
const ORIGIN_RINGS = 2;
const SNAP_RINGS = 4;

/** Unit directions of the 8 footprint samples: E, SE, S, SW, W, NW, N, NE (y down). */
export const FOOTPRINT_DIRECTIONS = Object.freeze([
  [1, 0], [SQRT_HALF, SQRT_HALF], [0, 1], [-SQRT_HALF, SQRT_HALF],
  [-1, 0], [-SQRT_HALF, -SQRT_HALF], [0, -1], [SQRT_HALF, -SQRT_HALF],
].map((direction) => Object.freeze(direction)));

const ACTIVATIONS = new Set(["crossing", "intent"]);
const BLOCKING_MATERIALS = new Set(["solid"]);
const WALK_BLOCKING_MATERIALS = new Set(["liquid", "hazard"]);
const FACINGS = {
  e: [1, 0], east: [1, 0], right: [1, 0], w: [-1, 0], west: [-1, 0], left: [-1, 0],
  s: [0, 1], south: [0, 1], down: [0, 1], n: [0, -1], north: [0, -1], up: [0, -1],
  ne: [SQRT_HALF, -SQRT_HALF], "north-east": [SQRT_HALF, -SQRT_HALF], northeast: [SQRT_HALF, -SQRT_HALF],
  nw: [-SQRT_HALF, -SQRT_HALF], "north-west": [-SQRT_HALF, -SQRT_HALF], northwest: [-SQRT_HALF, -SQRT_HALF],
  se: [SQRT_HALF, SQRT_HALF], "south-east": [SQRT_HALF, SQRT_HALF], southeast: [SQRT_HALF, SQRT_HALF],
  sw: [-SQRT_HALF, SQRT_HALF], "south-west": [-SQRT_HALF, SQRT_HALF], southwest: [-SQRT_HALF, SQRT_HALF],
};

// --------------------------------------------------------------------------- input checks

function finite(value, where) {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new TypeError(`${where} must be a finite number`);
  return value;
}

function nonNegative(value, where) {
  if (finite(value, where) < 0) throw new RangeError(`${where} must not be negative`);
  return value;
}

function text(value, where) {
  if (typeof value !== "string" || value.length === 0) throw new TypeError(`${where} must be a non-empty string`);
  return value;
}

/** A point as [x, y], {x, y} or {point: [x, y]}. */
function readPoint(value, where) {
  if (Array.isArray(value)) {
    if (value.length !== 2) throw new TypeError(`${where} must be [x, y]`);
    return [finite(value[0], `${where}[0]`), finite(value[1], `${where}[1]`)];
  }
  if (value && typeof value === "object") {
    if (value.point !== undefined) return readPoint(value.point, `${where}.point`);
    return [finite(value.x, `${where}.x`), finite(value.y, `${where}.y`)];
  }
  throw new TypeError(`${where} must be [x, y]`);
}

/** One point, or a list of points (approach may be either). */
function readPoints(value, where) {
  if (value === undefined || value === null) return [];
  if (Array.isArray(value) && value.length === 2 && typeof value[0] === "number") return [readPoint(value, where)];
  if (!Array.isArray(value)) return [readPoint(value, where)];
  return value.map((item, k) => readPoint(item, `${where}[${k}]`));
}

// --------------------------------------------------------------------------- shapes

function boundsOf(xs, ys) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (let k = 0; k < xs.length; k++) {
    if (xs[k] < minX) minX = xs[k];
    if (xs[k] > maxX) maxX = xs[k];
    if (ys[k] < minY) minY = ys[k];
    if (ys[k] > maxY) maxY = ys[k];
  }
  return {minX, minY, maxX, maxY};
}

function polygonShape(points, where, source) {
  if (!Array.isArray(points) || points.length < 3) throw new TypeError(`${where} must list at least 3 [x, y] points`);
  const xs = new Float64Array(points.length), ys = new Float64Array(points.length);
  points.forEach((item, k) => {
    const [x, y] = readPoint(item, `${where}[${k}]`);
    xs[k] = x;
    ys[k] = y;
  });
  return {kind: "polygon", xs, ys, ...boundsOf(xs, ys), source};
}

function rectShape(x, y, w, h, source) {
  const x1 = x + w, y1 = y + h;
  return {kind: "rect", x, y, x1, y1, minX: x, minY: y, maxX: x1, maxY: y1, source};
}

function ellipseShape(cx, cy, rx, ry, rotate, source) {
  const rotated = rotate !== 0;
  const theta = rotate * Math.PI / 180;
  const cos = rotated ? Math.cos(theta) : 1, sin = rotated ? Math.sin(theta) : 0;
  const ex = Math.sqrt(rx * cos * (rx * cos) + ry * sin * (ry * sin));
  const ey = Math.sqrt(rx * sin * (rx * sin) + ry * cos * (ry * cos));
  return {
    kind: "ellipse", cx, cy, rx, ry, rotate, rotated, cos, sin, empty: !(rx > 0 && ry > 0),
    minX: cx - ex, minY: cy - ey, maxX: cx + ex, maxY: cy + ey, source,
  };
}

/** Even-odd point-in-polygon test with the parity expression from the header. */
export function insidePolygon(xs, ys, px, py) {
  let inside = false;
  for (let i = 0, j = xs.length - 1; i < xs.length; j = i++) {
    const yi = ys[i], yj = ys[j];
    if ((yi > py) !== (yj > py)) {
      const xi = xs[i];
      if (px < xi + (py - yi) * (xs[j] - xi) / (yj - yi)) inside = !inside;
    }
  }
  return inside;
}

/** True when (px, py) lies inside a compiled solid (rect, ellipse or polygon). */
export function insideShape(shape, px, py) {
  if (shape.kind === "rect") return px >= shape.x && px < shape.x1 && py >= shape.y && py < shape.y1;
  if (shape.kind === "ellipse") {
    if (shape.empty) return false;
    const dx = px - shape.cx, dy = py - shape.cy;
    let u = dx, v = dy;
    if (shape.rotated) {
      u = dx * shape.cos + dy * shape.sin;
      v = -dx * shape.sin + dy * shape.cos;
    }
    const nu = u / shape.rx, nv = v / shape.ry;
    return nu * nu + nv * nv < 1;
  }
  return insidePolygon(shape.xs, shape.ys, px, py);
}

function compileSolid(solid, where) {
  if (!solid || typeof solid !== "object") throw new TypeError(`${where} must be an object`);
  if (solid.shape === "rect") {
    return rectShape(finite(solid.x, `${where}.x`), finite(solid.y, `${where}.y`),
      nonNegative(solid.w, `${where}.w`), nonNegative(solid.h, `${where}.h`), where);
  }
  if (solid.shape === "ellipse") {
    return ellipseShape(finite(solid.cx, `${where}.cx`), finite(solid.cy, `${where}.cy`),
      nonNegative(solid.rx, `${where}.rx`), nonNegative(solid.ry, `${where}.ry`),
      solid.rotate === undefined ? 0 : finite(solid.rotate, `${where}.rotate`), where);
  }
  if (solid.shape === "polygon") return polygonShape(solid.points, `${where}.points`, where);
  throw new TypeError(`${where}.shape must be rect, ellipse or polygon`);
}

/** The blocking solid of a placed object, or null (no footprint, shape none, or solid false). */
export function footprintSolid(object, where = "object") {
  const footprint = object.footprint;
  if (footprint === undefined || footprint === null || object.solid === false) return null;
  if (typeof footprint !== "object") throw new TypeError(`${where}.footprint must be an object`);
  if (footprint.shape === "none") return null;
  if (footprint.shape !== "ellipse" && footprint.shape !== "rect") {
    throw new TypeError(`${where}.footprint.shape must be ellipse, rect or none`);
  }
  const x = finite(object.x, `${where}.x`), y = finite(object.y, `${where}.y`);
  const scale = object.scale === undefined ? 1 : finite(object.scale, `${where}.scale`);
  if (!(scale > 0)) throw new RangeError(`${where}.scale must be positive`);
  const s = footprint.basis === "world_px" ? 1 : scale;
  const [ox, oy] = footprint.offset === undefined ? [0, 0] : readPoint(footprint.offset, `${where}.footprint.offset`);
  const w = nonNegative(footprint.width, `${where}.footprint.width`) * s;
  const h = nonNegative(footprint.depth, `${where}.footprint.depth`) * s;
  const cx = x + ox * s, cy = y + oy * s;
  const rotate = footprint.rotate === undefined ? 0 : finite(footprint.rotate, `${where}.footprint.rotate`);
  const source = `${where}.footprint`;
  if (footprint.shape === "ellipse") return ellipseShape(cx, cy, w / 2, h / 2, rotate, source);
  if (rotate === 0) return rectShape(cx - w / 2, cy - h / 2, w, h, source);
  const theta = rotate * Math.PI / 180, cos = Math.cos(theta), sin = Math.sin(theta), hw = w / 2, hh = h / 2;
  const corners = [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]]
    .map(([u, v]) => [cx + u * cos - v * sin, cy + u * sin + v * cos]);
  return polygonShape(corners, source, source);
}

// --------------------------------------------------------------------------- spatial index

function buildIndex(shapes) {
  const live = [];
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  shapes.forEach((shape, k) => {
    if (shape.empty) return;
    live.push(k);
    minX = Math.min(minX, shape.minX - 1);
    minY = Math.min(minY, shape.minY - 1);
    maxX = Math.max(maxX, shape.maxX + 1);
    maxY = Math.max(maxY, shape.maxY + 1);
  });
  if (!live.length) return null;
  let size = INDEX_BUCKET, x0, y0, cols, rows;
  for (;;) {
    x0 = Math.floor(minX / size);
    y0 = Math.floor(minY / size);
    cols = Math.floor(maxX / size) - x0 + 1;
    rows = Math.floor(maxY / size) - y0 + 1;
    if (cols * rows <= MAX_INDEX_CELLS) break;
    size *= 2;
  }
  const cells = new Array(cols * rows);
  for (const k of live) {
    const shape = shapes[k];
    const i0 = Math.floor((shape.minX - 1) / size) - x0, i1 = Math.floor((shape.maxX + 1) / size) - x0;
    const j0 = Math.floor((shape.minY - 1) / size) - y0, j1 = Math.floor((shape.maxY + 1) / size) - y0;
    for (let j = j0; j <= j1; j++) {
      for (let i = i0; i <= i1; i++) {
        const c = j * cols + i;
        (cells[c] || (cells[c] = [])).push(k);
      }
    }
  }
  return {size, x0, y0, cols, rows, cells};
}

function candidates(index, px, py) {
  if (index === null) return null;
  const i = Math.floor(px / index.size) - index.x0, j = Math.floor(py / index.size) - index.y0;
  if (!(i >= 0 && j >= 0 && i < index.cols && j < index.rows)) return null;
  return index.cells[j * index.cols + i] || null;
}

// --------------------------------------------------------------------------- material grid

function decodeBase64(data) {
  if (typeof atob === "function") {
    const binary = atob(data);
    const out = new Uint8Array(binary.length);
    for (let k = 0; k < binary.length; k++) out[k] = binary.charCodeAt(k);
    return out;
  }
  if (typeof globalThis.Buffer === "function") return new Uint8Array(globalThis.Buffer.from(data, "base64"));
  throw new Error("no base64 decoder available");
}

/**
 * Check a blocked-cell grid: {width, height, cellWidth, cellHeight, bits}, where
 * bits is a base64 string or a Uint8Array with bit k (LSB first) set for blocked
 * cell k = j * width + i. build_scene_preview.py writes this form.
 */
export function decodeMaterialGrid(grid) {
  if (!grid || typeof grid !== "object") throw new TypeError("materialGrid must be an object");
  const width = finite(grid.width, "materialGrid.width"), height = finite(grid.height, "materialGrid.height");
  if (!(Number.isInteger(width) && Number.isInteger(height) && width > 0 && height > 0)) {
    throw new RangeError("materialGrid width and height must be positive integers");
  }
  const cellWidth = finite(grid.cellWidth, "materialGrid.cellWidth"), cellHeight = finite(grid.cellHeight, "materialGrid.cellHeight");
  if (!(cellWidth > 0 && cellHeight > 0)) throw new RangeError("materialGrid cell sizes must be positive");
  const bytes = typeof grid.bits === "string" ? decodeBase64(grid.bits) : grid.bits;
  if (!(bytes instanceof Uint8Array) || bytes.length < Math.ceil(width * height / 8)) {
    throw new TypeError("materialGrid.bits must hold width * height bits");
  }
  return {width, height, cellWidth, cellHeight, bytes};
}

/** True when a material_map class blocks a top-down walker (Appendix C). */
export function materialBlocks(spec) {
  if (!spec || typeof spec !== "object") return false;
  if (BLOCKING_MATERIALS.has(spec.class)) return true;
  return WALK_BLOCKING_MATERIALS.has(spec.class) && spec.walkable !== true;
}

function materialColor(value, where) {
  if (Array.isArray(value) && value.length === 3) return value.map((channel, k) => finite(channel, `${where}[${k}]`));
  if (typeof value === "string" && /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(value)) {
    return [1, 3, 5].map((k) => parseInt(value.slice(k, k + 2), 16));
  }
  throw new TypeError(`${where} must be #rrggbb or [r, g, b]`);
}

/**
 * Build a material grid from decoded straight-alpha RGBA pixels (a canvas
 * getImageData result, for example) by exact colour match. Materials given by
 * palette `index` need the indices, which RGBA has lost: use the grid that
 * build_scene_preview.py embeds instead. Pixels with alpha 0 have no material.
 */
export function materialGridFromRGBA(rgba, width, height, materialMap, worldWidth, worldHeight) {
  const materials = materialMap && materialMap.materials;
  if (!materials || typeof materials !== "object") throw new TypeError("material_map.materials must be an object");
  const blocking = [];
  for (const [name, spec] of Object.entries(materials)) {
    if (!materialBlocks(spec)) continue;
    if (spec.color === undefined) throw new TypeError(`material ${name} has no color; index materials need the embedded grid`);
    blocking.push(materialColor(spec.color, `material_map.materials.${name}.color`));
  }
  const bytes = new Uint8Array(Math.ceil(width * height / 8));
  for (let k = 0; k < width * height; k++) {
    const o = k * 4;
    if (rgba[o + 3] === 0) continue;
    for (const [r, g, b] of blocking) {
      if (rgba[o] === r && rgba[o + 1] === g && rgba[o + 2] === b) {
        bytes[k >> 3] |= 1 << (k & 7);
        break;
      }
    }
  }
  return {width, height, cellWidth: worldWidth / width, cellHeight: worldHeight / height, bytes};
}

function materialBlocked(grid, px, py) {
  const i = Math.floor(px / grid.cellWidth), j = Math.floor(py / grid.cellHeight);
  if (!(i >= 0 && j >= 0 && i < grid.width && j < grid.height)) return false;
  const k = j * grid.width + i;
  return ((grid.bytes[k >> 3] >> (k & 7)) & 1) === 1;
}

// --------------------------------------------------------------------------- world

function compilePortal(portal, where) {
  if (!portal || typeof portal !== "object") throw new TypeError(`${where} must be an object`);
  const id = text(portal.id, `${where}.id`);
  const to = portal.to === undefined ? "" : String(portal.to);
  if ((portal.rect === undefined) === (portal.circle === undefined)) {
    throw new TypeError(`${where} needs exactly one of rect [x, y, w, h] or circle [cx, cy, r]`);
  }
  let shape;
  if (portal.rect !== undefined) {
    const r = portal.rect;
    if (!Array.isArray(r) || r.length !== 4) throw new TypeError(`${where}.rect must be [x, y, w, h]`);
    const x = finite(r[0], `${where}.rect[0]`), y = finite(r[1], `${where}.rect[1]`);
    const w = nonNegative(r[2], `${where}.rect[2]`), h = nonNegative(r[3], `${where}.rect[3]`);
    shape = {kind: "rect", x, y, x1: x + w, y1: y + h, cx: x + w / 2, cy: y + h / 2};
  } else {
    const c = portal.circle;
    if (!Array.isArray(c) || c.length !== 3) throw new TypeError(`${where}.circle must be [cx, cy, r]`);
    shape = {kind: "circle", cx: finite(c[0], `${where}.circle[0]`), cy: finite(c[1], `${where}.circle[1]`),
      r: nonNegative(c[2], `${where}.circle[2]`)};
  }
  const activation = portal.activation === undefined ? "crossing" : portal.activation;
  if (!ACTIVATIONS.has(activation)) throw new TypeError(`${where}.activation must be crossing or intent`);
  let dirX = 0, dirY = 0;
  if (portal.travelDirection !== undefined) {
    const [tx, ty] = readPoint(portal.travelDirection, `${where}.travelDirection`);
    const length = Math.sqrt(tx * tx + ty * ty);
    if (!(length > 0)) throw new RangeError(`${where}.travelDirection must not be zero`);
    dirX = tx / length;
    dirY = ty / length;
  }
  if (activation === "intent" && (portal.travelDirection === undefined || portal.radius === undefined)) {
    throw new TypeError(`${where}: intent portals need travelDirection and radius`);
  }
  const radius = portal.radius === undefined ? 0 : nonNegative(portal.radius, `${where}.radius`);
  const entranceByFrom = {};
  if (portal.entranceByFrom !== undefined) {
    if (!portal.entranceByFrom || typeof portal.entranceByFrom !== "object" || Array.isArray(portal.entranceByFrom)) {
      throw new TypeError(`${where}.entranceByFrom must map map ids to spawn ids`);
    }
    for (const [from, spawn] of Object.entries(portal.entranceByFrom)) {
      entranceByFrom[from] = text(spawn, `${where}.entranceByFrom.${from}`);
    }
  }
  const colon = to.indexOf(":");
  return {
    id, to, toMap: colon < 0 ? to : to.slice(0, colon), toSpawn: colon < 0 ? null : to.slice(colon + 1),
    activation, ...shape, dirX, dirY, hasDirection: portal.travelDirection !== undefined, radius,
    latch: portal.latch !== false, requiresMovement: portal.requiresMovement !== false, entranceByFrom,
  };
}

function facingVector(facing) {
  if (typeof facing === "number" && Number.isFinite(facing)) {
    const theta = facing * Math.PI / 180;
    return [Math.cos(theta), Math.sin(theta)];
  }
  if (typeof facing === "string") return FACINGS[facing.toLowerCase()] || null;
  return null;
}

function uniqueIds(items, what) {
  const byId = new Map();
  for (const item of items) {
    if (byId.has(item.id)) throw new TypeError(`duplicate ${what} id ${JSON.stringify(item.id)}`);
    byId.set(item.id, item);
  }
  return byId;
}

/** Cell size of the Appendix C navigation grid: max(1, round half up (r / 2)). */
export function navCellSize(actorRadius) {
  return Math.max(1, Math.floor(actorRadius / 2 + 0.5));
}

/**
 * Compile the map_bundle.v2 fields the runtime reads into a world object.
 * options.materialGrid is the blocked-cell grid of material_map (see
 * decodeMaterialGrid and materialGridFromRGBA); without it the material map is
 * ignored. Throws TypeError or RangeError on malformed data.
 */
export function createMapRuntime(bundle, options = {}) {
  if (!bundle || typeof bundle !== "object") throw new TypeError("bundle must be an object");
  const world = bundle.world || {};
  const width = finite(world.width, "world.width"), height = finite(world.height, "world.height");
  if (!(width > 0 && height > 0)) throw new RangeError("world.width and world.height must be positive");
  const collision = bundle.collision;
  if (!collision || typeof collision !== "object") throw new TypeError("collision must be an object");
  const actorRadius = nonNegative(collision.actorRadius, "collision.actorRadius");
  const ySquash = collision.ySquash === undefined ? 1 : finite(collision.ySquash, "collision.ySquash");
  if (!(ySquash > 0)) throw new RangeError("collision.ySquash must be positive");
  const cell = navCellSize(actorRadius);

  const walkRegions = (collision.walkRegions || []).map((region, k) => {
    const where = `collision.walkRegions[${k}]`;
    if (!region || typeof region !== "object") throw new TypeError(`${where} must be an object`);
    return {
      outer: polygonShape(region.polygon, `${where}.polygon`, where),
      holes: (region.holes || []).map((hole, h) => polygonShape(hole, `${where}.holes[${h}]`, where)),
    };
  });
  const solids = (collision.solids || []).map((solid, k) => compileSolid(solid, `collision.solids[${k}]`));
  (collision.rects || []).forEach((rect, k) => {
    const where = `collision.rects[${k}]`;
    if (!Array.isArray(rect) || rect.length !== 4) throw new TypeError(`${where} must be [x, y, w, h]`);
    solids.push(rectShape(finite(rect[0], `${where}[0]`), finite(rect[1], `${where}[1]`),
      nonNegative(rect[2], `${where}[2]`), nonNegative(rect[3], `${where}[3]`), where));
  });
  (bundle.objects || []).forEach((object, k) => {
    const shape = footprintSolid(object, `objects[${k}]`);
    if (shape) {
      shape.objectId = object.id;
      solids.push(shape);
    }
  });

  const portals = (bundle.portals || []).map((portal, k) => compilePortal(portal, `portals[${k}]`));
  const spawns = (bundle.spawns || []).map((spawn, k) => {
    const where = `spawns[${k}]`;
    return {id: text(spawn.id, `${where}.id`), x: finite(spawn.x, `${where}.x`), y: finite(spawn.y, `${where}.y`),
      facing: facingVector(spawn.facing)};
  });
  const interactions = (bundle.interactions || []).map((item, k) => {
    const where = `interactions[${k}]`;
    return {id: text(item.id, `${where}.id`), x: finite(item.x, `${where}.x`), y: finite(item.y, `${where}.y`),
      reach: item.reach === undefined ? 6 * actorRadius : nonNegative(item.reach, `${where}.reach`)};
  });
  const anchors = Object.entries(bundle.anchors || {}).map(([name, anchor]) => {
    const where = `anchors.${name}`;
    if (!anchor || typeof anchor !== "object") throw new TypeError(`${where} must be an object`);
    return {name, point: readPoint(anchor.point, `${where}.point`),
      slots: (anchor.slots || []).map((slot, k) => readPoint(slot, `${where}.slots[${k}]`)),
      approach: readPoints(anchor.approach, `${where}.approach`)};
  });

  return {
    version: RUNTIME_VERSION, width, height, actorRadius, ySquash, footprintRy: actorRadius * ySquash,
    cell, sampleSpacing: cell / 2, walkRegions, solids, index: buildIndex(solids),
    material: options.materialGrid ? decodeMaterialGrid(options.materialGrid) : null,
    portals, portalById: uniqueIds(portals, "portal"), spawns, spawnById: uniqueIds(spawns, "spawn"),
    interactions, interactionById: uniqueIds(interactions, "interaction"), anchors, nav: null,
  };
}

// --------------------------------------------------------------------------- collision query

function insideWalkArea(world, px, py) {
  if (world.walkRegions.length === 0) return px >= 0 && px < world.width && py >= 0 && py < world.height;
  for (const region of world.walkRegions) {
    const outer = region.outer;
    if (py < outer.minY || py >= outer.maxY) continue;
    if (!insidePolygon(outer.xs, outer.ys, px, py)) continue;
    if (!region.holes.some((hole) => insidePolygon(hole.xs, hole.ys, px, py))) return true;
  }
  return false;
}

/** The single-point test: walk area, solids and material grid (no footprint). */
export function pointFree(world, px, py) {
  if (!insideWalkArea(world, px, py)) return false;
  const near = candidates(world.index, px, py);
  if (near !== null) {
    for (const k of near) if (insideShape(world.solids[k], px, py)) return false;
  }
  return world.material === null || !materialBlocked(world.material, px, py);
}

/** The 9 points of the actor footprint test at (x, y): the centre, then E, SE, S, SW, W, NW, N, NE. */
export function footprintSamples(world, x, y) {
  const r = world.actorRadius, ry = world.footprintRy;
  return [[x, y], ...FOOTPRINT_DIRECTIONS.map(([ux, uy]) => [x + r * ux, y + ry * uy])];
}

/** Appendix C: true when the actor cannot stand at (x, y). */
export function isBlocked(world, x, y) {
  if (!pointFree(world, x, y)) return true;
  const r = world.actorRadius;
  if (r > 0) {
    const ry = world.footprintRy;
    for (const [ux, uy] of FOOTPRINT_DIRECTIONS) if (!pointFree(world, x + r * ux, y + ry * uy)) return true;
  }
  return false;
}

/** Appendix C: every sample along a -> b, at most cell / 2 apart (ends included), is a valid position. */
export function segmentClear(world, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const n = Math.max(1, Math.ceil(Math.sqrt(dx * dx + dy * dy) / world.sampleSpacing));
  for (let k = 0; k <= n; k++) if (isBlocked(world, ax + dx * k / n, ay + dy * k / n)) return false;
  return true;
}

// --------------------------------------------------------------------------- navigation grid

/** The Appendix C grid: {cell, cols, rows}, with validity cached lazily per cell centre. */
export function navGrid(world) {
  if (world.nav === null) {
    const cell = world.cell, cols = Math.ceil(world.width / cell), rows = Math.ceil(world.height / cell);
    world.nav = {cell, cols, rows, state: new Uint8Array(cols * rows), edges: new Uint8Array(cols * rows * 2)};
  }
  return world.nav;
}

/** Centre of cell k as [x, y]. */
export function cellCenter(nav, k) {
  const i = k % nav.cols, j = (k - i) / nav.cols;
  return [(i + 0.5) * nav.cell, (j + 0.5) * nav.cell];
}

/** True when the centre of cell k is a valid position (cached). */
export function cellValid(world, k) {
  const nav = navGrid(world);
  let state = nav.state[k];
  if (state === 0) {
    const [x, y] = cellCenter(nav, k);
    state = isBlocked(world, x, y) ? 2 : 1;
    nav.state[k] = state;
  }
  return state === 1;
}

// Edge k*2 joins cell k to its east neighbour, k*2+1 to its south neighbour.
function edgeClear(world, nav, a, b) {
  const lo = Math.min(a, b), slot = lo * 2 + (Math.abs(a - b) === 1 ? 0 : 1);
  let state = nav.edges[slot];
  if (state === 0) {
    const [ax, ay] = cellCenter(nav, a), [bx, by] = cellCenter(nav, b);
    state = segmentClear(world, ax, ay, bx, by) ? 1 : 2;
    nav.edges[slot] = state;
  }
  return state === 1;
}

function neighbours(nav, k) {
  const i = k % nav.cols, out = [];
  if (i + 1 < nav.cols) out.push(k + 1);
  if (i > 0) out.push(k - 1);
  if (k + nav.cols < nav.cols * nav.rows) out.push(k + nav.cols);
  if (k >= nav.cols) out.push(k - nav.cols);
  return out;
}

// Cells within `rings` of the cell holding (x, y), nearest centre first, filtered.
function nearbyCells(nav, x, y, rings, accept) {
  const ci = Math.floor(x / nav.cell), cj = Math.floor(y / nav.cell);
  let best = -1, bestDistance = Infinity;
  for (let j = cj - rings; j <= cj + rings; j++) {
    if (j < 0 || j >= nav.rows) continue;
    for (let i = ci - rings; i <= ci + rings; i++) {
      if (i < 0 || i >= nav.cols) continue;
      const k = j * nav.cols + i, cx = (i + 0.5) * nav.cell, cy = (j + 0.5) * nav.cell;
      const distance = (cx - x) * (cx - x) + (cy - y) * (cy - y);
      if (distance < bestDistance && accept(k, cx, cy)) {
        best = k;
        bestDistance = distance;
      }
    }
  }
  return best;
}

/** The grid cell a point starts from: the nearest valid centre (2 rings) reachable by a clear segment, or -1. */
export function originCell(world, x, y) {
  const nav = navGrid(world);
  return nearbyCells(nav, x, y, ORIGIN_RINGS, (k, cx, cy) => cellValid(world, k) && segmentClear(world, x, y, cx, cy));
}

/**
 * Breadth-first search over the grid from the point (x, y). Returns
 * {nav, start, dist, parent, reached}: dist[k] is the move count to cell k
 * (-1 when unreachable) and parent[k] its predecessor.
 */
export function flood(world, x, y) {
  const nav = navGrid(world), total = nav.cols * nav.rows;
  const dist = new Int32Array(total).fill(-1), parent = new Int32Array(total).fill(-1);
  const start = originCell(world, x, y);
  if (start < 0) return {nav, start, dist, parent, reached: 0};
  const queue = new Int32Array(total);
  let head = 0, tail = 0;
  dist[start] = 0;
  queue[tail++] = start;
  while (head < tail) {
    const k = queue[head++];
    for (const next of neighbours(nav, k)) {
      if (dist[next] >= 0 || !cellValid(world, next) || !edgeClear(world, nav, k, next)) continue;
      dist[next] = dist[k] + 1;
      parent[next] = k;
      queue[tail++] = next;
    }
  }
  return {nav, start, dist, parent, reached: tail};
}

function chain(field, goal) {
  const cells = [];
  for (let k = goal; k >= 0; k = field.parent[k]) cells.push(k);
  cells.reverse();
  return cells.map((k) => {
    const [x, y] = cellCenter(field.nav, k);
    return {x, y};
  });
}

/**
 * Greedy string pulling: from (x, y) jump to the farthest waypoint with a clear segment. The input
 * must already be a chain of clear segments starting at (x, y); every output waypoint is flagged
 * clear: true, meaning segmentClear() holds from the point before it.
 */
export function smoothPath(world, x, y, points) {
  const out = [];
  let hx = x, hy = y, i = 0;
  while (i < points.length) {
    let far = i;
    const limit = Math.min(points.length - 1, i + SMOOTH_LOOKAHEAD);
    while (far < limit && segmentClear(world, hx, hy, points[far + 1].x, points[far + 1].y)) far++;
    out.push({x: points[far].x, y: points[far].y, clear: true});
    hx = points[far].x;
    hy = points[far].y;
    i = far + 1;
  }
  return out;
}

/**
 * Waypoints from the flood origin to (gx, gy): the exact point when the actor
 * can stand there, otherwise the nearest reachable cell centre within
 * snapRings cells. Returns null when nothing is reachable.
 */
export function pathFromFlood(world, field, x, y, gx, gy, {snapRings = SNAP_RINGS} = {}) {
  if (field.start < 0) return null;
  const reachable = (k) => field.dist[k] >= 0;
  let goal = -1, exact = false;
  if (!isBlocked(world, gx, gy)) {
    goal = nearbyCells(field.nav, gx, gy, ORIGIN_RINGS, (k, cx, cy) => reachable(k) && segmentClear(world, cx, cy, gx, gy));
    exact = goal >= 0;
  }
  if (goal < 0) goal = nearbyCells(field.nav, gx, gy, snapRings, reachable);
  if (goal < 0) return null;
  const points = chain(field, goal);
  if (exact) points.push({x: gx, y: gy});
  return smoothPath(world, x, y, points);
}

/** Click-to-walk: waypoints from (sx, sy) towards (gx, gy), or null. */
export function findPath(world, sx, sy, gx, gy, options) {
  return pathFromFlood(world, flood(world, sx, sy), sx, sy, gx, gy, options);
}

// --------------------------------------------------------------------------- movement

/**
 * Move by (dx, dy) in parts no longer than cell / 2. A part that would leave
 * a valid position slides along x, then along y, else the walker stops there:
 * a blocked walker stays put and never moves further than asked.
 */
export function moveWithCollision(world, x, y, dx, dy) {
  const length = Math.sqrt(dx * dx + dy * dy);
  if (!(length > 0)) return {x, y};
  const parts = Math.max(1, Math.ceil(length / world.sampleSpacing));
  const sx = dx / parts, sy = dy / parts;
  for (let k = 0; k < parts; k++) {
    if (!isBlocked(world, x + sx, y + sy)) {
      x += sx;
      y += sy;
    } else if (sx !== 0 && !isBlocked(world, x + sx, y)) {
      x += sx;
    } else if (sy !== 0 && !isBlocked(world, x, y + sy)) {
      y += sy;
    } else {
      break;
    }
  }
  return {x, y};
}

/**
 * Spend one tick's movement budget along waypoints, carrying what is left at a
 * waypoint into the next segment, so reaching a corner never costs an idle
 * tick and the distance moved never exceeds the budget. A waypoint flagged
 * clear: true (findPath and smoothPath output) ends a segment that passed
 * segmentClear() when it was planned, so the walker follows it as planned;
 * other waypoints are approached with moveWithCollision(). The input path is
 * not changed; `path` in the result holds the waypoints still ahead.
 * Returns {x, y, travelled, dirX, dirY, wishX, wishY, blocked, path}.
 */
export function advanceAlongPath(world, start, path, budget) {
  let x = start.x, y = start.y, remaining = Math.max(0, budget), travelled = 0;
  let dirX = 0, dirY = 0, wishX = 0, wishY = 0, blocked = false, i = 0;
  // Each pass reaches a waypoint, spends the rest of the budget or slides; the
  // guard only bounds pathological slides that keep approaching a waypoint.
  for (let guard = 2 * path.length + 16; i < path.length && guard > 0; guard--) {
    const goal = path[i], gx = goal.x - x, gy = goal.y - y;
    const distance = Math.sqrt(gx * gx + gy * gy);
    if (distance <= EPS) {
      x = goal.x;
      y = goal.y;
      i++;
      continue;
    }
    wishX = gx / distance;
    wishY = gy / distance;
    if (remaining <= EPS) break;
    const amount = Math.min(distance, remaining);
    const tx = x + wishX * amount, ty = y + wishY * amount;
    const next = goal.clear === true ? {x: tx, y: ty} : moveWithCollision(world, x, y, wishX * amount, wishY * amount);
    const mx = next.x - x, my = next.y - y, moved = Math.sqrt(mx * mx + my * my);
    if (moved > EPS) {
      dirX = mx / moved;
      dirY = my / moved;
      travelled += moved;
    }
    remaining -= amount;
    if (amount === distance && Math.abs(next.x - tx) <= EPS && Math.abs(next.y - ty) <= EPS) {
      x = goal.x;
      y = goal.y;
      i++;
    } else {
      x = next.x;
      y = next.y;
    }
    if (moved <= EPS) {
      blocked = true;
      break;
    }
  }
  return {x, y, travelled, dirX, dirY, wishX, wishY, blocked, path: path.slice(i)};
}

// --------------------------------------------------------------------------- exits

/** Distance from (x, y) to a portal trigger; 0 inside it. */
export function portalDistance(portal, x, y) {
  if (portal.kind === "rect") {
    const dx = Math.max(portal.x - x, 0, x - portal.x1), dy = Math.max(portal.y - y, 0, y - portal.y1);
    return Math.sqrt(dx * dx + dy * dy);
  }
  const dx = x - portal.cx, dy = y - portal.cy;
  return Math.max(0, Math.sqrt(dx * dx + dy * dy) - portal.r);
}

/** True inside the trigger shape (rect half-open, circle open). */
export function insideTrigger(portal, x, y) {
  if (portal.kind === "rect") return x >= portal.x && x < portal.x1 && y >= portal.y && y < portal.y1;
  const dx = x - portal.cx, dy = y - portal.cy;
  return dx * dx + dy * dy < portal.r * portal.r;
}

/** The activation zone: within radius of the trigger (intent) or inside it (crossing). */
export function inPortalZone(portal, x, y) {
  return portal.activation === "intent" ? portalDistance(portal, x, y) <= portal.radius : insideTrigger(portal, x, y);
}

/**
 * The portal an input fires at (x, y), or null. intent is the input direction
 * (any length; zero means no input); latched is a Set of portal ids that may
 * not fire. Ties go to the nearest trigger, then to bundle order.
 */
export function exitCandidate(world, x, y, intentX, intentY, latched = null) {
  const length = Math.sqrt(intentX * intentX + intentY * intentY);
  let best = null, bestDistance = Infinity;
  for (const portal of world.portals) {
    if (latched !== null && latched.has(portal.id)) continue;
    let distance;
    if (portal.activation === "intent") {
      if (!(length > 0)) continue;
      distance = portalDistance(portal, x, y);
      if (distance > portal.radius) continue;
      if ((intentX * portal.dirX + intentY * portal.dirY) / length <= INTENT_MIN_COS) continue;
    } else {
      if (!insideTrigger(portal, x, y)) continue;
      if (portal.requiresMovement && !(length > 0)) continue;
      distance = 0;
    }
    if (distance < bestDistance) {
      best = portal;
      bestDistance = distance;
    }
  }
  return best;
}

// --------------------------------------------------------------------------- actor

/** A walker at (x, y), arrived there: portals whose zone holds it start latched. */
export function createActor(world, x, y, facing = null) {
  const actor = {x, y, facingX: facing ? facing[0] : 0, facingY: facing ? facing[1] : 1,
    latched: new Set(), path: null, travelled: 0};
  arrive(world, actor, x, y);
  return actor;
}

/** Place the actor (spawn, reset or return through an exit) and arm arrival latches. */
export function arrive(world, actor, x, y) {
  actor.x = x;
  actor.y = y;
  actor.path = null;
  actor.latched = new Set();
  for (const portal of world.portals) if (portal.latch && inPortalZone(portal, x, y)) actor.latched.add(portal.id);
  return actor.latched;
}

/** Release the latch of every portal whose zone the actor has left. */
export function releaseLatches(world, actor) {
  for (const id of [...actor.latched]) {
    const portal = world.portalById.get(id);
    if (!portal || !inPortalZone(portal, actor.x, actor.y)) actor.latched.delete(id);
  }
}

/**
 * One tick. intent {x, y} (keyboard; any length) moves budget pixels and
 * cancels a path; a null or zero intent follows actor.path. Then latches are
 * released, exits are checked and a portal that fires is latched until the
 * actor leaves its zone. Returns {travelled, intentX, intentY, blocked, arrived, fired}.
 */
export function stepActor(world, actor, intent, budget) {
  let intentX = 0, intentY = 0, travelled = 0, blocked = false, arrived = false;
  const length = intent ? Math.sqrt(intent.x * intent.x + intent.y * intent.y) : 0;
  if (length > 0) {
    actor.path = null;
    intentX = intent.x / length;
    intentY = intent.y / length;
    const next = moveWithCollision(world, actor.x, actor.y, intentX * budget, intentY * budget);
    const mx = next.x - actor.x, my = next.y - actor.y;
    travelled = Math.sqrt(mx * mx + my * my);
    blocked = travelled <= EPS;
    actor.x = next.x;
    actor.y = next.y;
  } else if (actor.path !== null && actor.path.length > 0) {
    const step = advanceAlongPath(world, actor, actor.path, budget);
    intentX = step.wishX;
    intentY = step.wishY;
    travelled = step.travelled;
    blocked = step.blocked;
    actor.x = step.x;
    actor.y = step.y;
    arrived = step.path.length === 0;
    actor.path = step.path.length > 0 && !blocked ? step.path : null;
  }
  if (intentX !== 0 || intentY !== 0) {
    actor.facingX = intentX;
    actor.facingY = intentY;
  }
  actor.travelled += travelled;
  releaseLatches(world, actor);
  const fired = exitCandidate(world, actor.x, actor.y, intentX, intentY, actor.latched);
  if (fired !== null) actor.latched.add(fired.id);
  return {travelled, intentX, intentY, blocked, arrived, fired};
}

/** The spawn an actor returns through after `portal` fired (entranceByFrom[toMap]), or null. */
export function returnSpawn(world, portal) {
  const id = portal.entranceByFrom[portal.toMap];
  return id === undefined ? null : world.spawnById.get(id) || null;
}

// --------------------------------------------------------------------------- draw order

/** Draw key of a placed object: [sortY or y, x, id, index]. */
export function drawKey(object, index) {
  return [object.sortY === undefined ? object.y : object.sortY, object.x, String(object.id), index];
}

/** Compare draw keys: sortY, then x, then id (UTF-16 order), then bundle order. */
export function compareDrawKeys(a, b) {
  if (a[0] !== b[0]) return a[0] - b[0];
  if (a[1] !== b[1]) return a[1] - b[1];
  if (a[2] !== b[2]) return a[2] < b[2] ? -1 : 1;
  return a[3] - b[3];
}

/** Objects in draw order (back to front). */
export function sortForDrawing(objects) {
  return objects.map((object, index) => ({object, key: drawKey(object, index)}))
    .sort((a, b) => compareDrawKeys(a.key, b.key)).map((item) => item.object);
}

/** Where the actor goes in a sorted list: after every object whose sort y is <= its feet. */
export function actorDrawIndex(sortedKeys, actorY) {
  let lo = 0, hi = sortedKeys.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sortedKeys[mid] <= actorY) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

// --------------------------------------------------------------------------- route check

function round3(value) {
  return Math.round(value * 1000) / 1000;
}

function targetsOf(world) {
  const targets = world.portals.map((portal) => ({kind: "exit", id: portal.id, portal}));
  for (const item of world.interactions) targets.push({kind: "interaction", id: item.id, x: item.x, y: item.y, reach: item.reach});
  for (const anchor of world.anchors) {
    anchor.approach.forEach(([x, y], k) => targets.push({kind: "approach", id: `${anchor.name}.approach[${k}]`, x, y}));
    anchor.slots.forEach(([x, y], k) => targets.push({kind: "slot", id: `${anchor.name}.slot[${k}]`, x, y}));
  }
  return targets;
}

// The reachable cell to walk to, or -1: inside an exit zone (nearest the trigger,
// then fewest moves), within reach of an interaction, or next to a standing point.
function goalCell(world, field, target) {
  const nav = field.nav, total = nav.cols * nav.rows;
  let best = -1, bestPrimary = Infinity, bestMoves = Infinity;
  const consider = (k, primary) => {
    if (primary < bestPrimary || (primary === bestPrimary && field.dist[k] < bestMoves)) {
      best = k;
      bestPrimary = primary;
      bestMoves = field.dist[k];
    }
  };
  if (target.kind === "exit") {
    const portal = target.portal;
    for (let k = 0; k < total; k++) {
      if (field.dist[k] < 0) continue;
      const [x, y] = cellCenter(nav, k);
      if (portal.activation === "intent") {
        const distance = portalDistance(portal, x, y);
        if (distance <= portal.radius) consider(k, distance);
      } else if (insideTrigger(portal, x, y)) {
        consider(k, 0);
      }
    }
    return best;
  }
  if (target.kind === "interaction") {
    for (let k = 0; k < total; k++) {
      if (field.dist[k] < 0) continue;
      const [x, y] = cellCenter(nav, k), dx = x - target.x, dy = y - target.y;
      if (Math.sqrt(dx * dx + dy * dy) <= target.reach) consider(k, 0);
    }
    return best;
  }
  if (isBlocked(world, target.x, target.y)) return -1;
  return nearbyCells(nav, target.x, target.y, ORIGIN_RINGS,
    (k, cx, cy) => field.dist[k] >= 0 && segmentClear(world, cx, cy, target.x, target.y));
}

// A reachable cell outside the portal's zone, fewest moves first (to leave an arrival latch).
function departureCell(field, portal) {
  let best = -1;
  for (let k = 0; k < field.dist.length; k++) {
    if (field.dist[k] < 0 || (best >= 0 && field.dist[k] >= field.dist[best])) continue;
    const [x, y] = cellCenter(field.nav, k);
    if (!inPortalZone(portal, x, y)) best = k;
  }
  return best;
}

function pathLength(x, y, path) {
  let length = 0;
  for (const point of path) {
    length += Math.sqrt((point.x - x) * (point.x - x) + (point.y - y) * (point.y - y));
    x = point.x;
    y = point.y;
  }
  return length;
}

// Follow actor.path tick by tick; onFire(portal) returns true to stop.
function walk(world, actor, budget, onFire, stats) {
  const limit = 2 * Math.ceil(pathLength(actor.x, actor.y, actor.path) / budget) + 120;
  for (let ticks = 0; actor.path !== null; ticks++) {
    if (ticks >= limit) {
      stats.limitHit = true;
      return false;
    }
    const step = stepActor(world, actor, null, budget);
    stats.ticks++;
    stats.distance += step.travelled;
    stats.maxStep = Math.max(stats.maxStep, step.travelled);
    if (step.travelled <= EPS && !step.arrived) stats.zeroMotionTicks++;
    if (step.blocked) {
      stats.blocked = true;
      return false;
    }
    if (step.fired !== null && onFire(step.fired)) return true;
  }
  return true;
}

// Hold one keyboard direction for `ticks` ticks; true when onFire stopped it.
function push(world, actor, dirX, dirY, budget, ticks, onFire) {
  for (let k = 0; k < ticks; k++) {
    const step = stepActor(world, actor, {x: dirX, y: dirY}, budget);
    if (step.fired !== null && onFire(step.fired)) return true;
  }
  return false;
}

function routeTo(world, floods, target, budget) {
  const result = {target: target.id, kind: target.kind, from: null, ok: false, reachable: false, reason: null,
    ticks: 0, distance: 0, maxStep: 0, zeroMotionTicks: 0, blocked: false, firedEnRoute: [], end: null};
  let spawn = null, field = null, goal = -1;
  for (const candidate of world.spawns) {
    const k = goalCell(world, floods.get(candidate.id), target);
    if (k >= 0) {
      spawn = candidate;
      field = floods.get(candidate.id);
      goal = k;
      break;
    }
  }
  if (spawn === null) {
    result.reason = world.spawns.length === 0 ? "no spawns" : "unreachable from every spawn";
    return result;
  }
  result.from = spawn.id;
  result.reachable = true;
  const exact = target.kind === "approach" || target.kind === "slot";
  const stats = {ticks: 0, distance: 0, maxStep: 0, zeroMotionTicks: 0, blocked: false, limitHit: false};
  const enRoute = new Set();
  let fired = false;
  const onFire = (portal) => {
    if (target.kind === "exit" && portal.id === target.id) {
      fired = true;
      return true;
    }
    enRoute.add(portal.id);
    return false;
  };
  const actor = createActor(world, spawn.x, spawn.y, spawn.facing);
  const reasons = [];
  let walking = true;
  if (target.kind === "exit" && actor.latched.has(target.id)) {
    // The spawn lies in this exit's zone and latched it: leave the zone first, as a player must.
    const away = departureCell(field, target.portal);
    if (away < 0) {
      reasons.push("the spawn latches this exit and no reachable cell lies outside its zone");
      walking = false;
    } else {
      actor.path = smoothPath(world, actor.x, actor.y, chain(field, away));
      walking = walk(world, actor, budget, onFire, stats);
      field = walking ? flood(world, actor.x, actor.y) : field;
      goal = walking ? goalCell(world, field, target) : -1;
      if (walking && goal < 0) {
        reasons.push("unreachable after leaving the arrival zone");
        walking = false;
      }
    }
  }
  const [gx, gy] = goal >= 0 ? cellCenter(field.nav, goal) : [actor.x, actor.y];
  if (walking) {
    const points = chain(field, goal);
    if (exact) points.push({x: target.x, y: target.y});
    actor.path = smoothPath(world, actor.x, actor.y, points);
    walk(world, actor, budget, onFire, stats);
  }
  if (target.kind === "exit") {
    const portal = target.portal;
    const pushTicks = Math.min(600, Math.ceil(portal.radius / budget) + Math.ceil(2 * world.cell / budget) + 4);
    if (walking && !fired && !stats.blocked && !stats.limitHit && portal.hasDirection) {
      push(world, actor, portal.dirX, portal.dirY, budget, pushTicks, onFire);
    }
    result.fired = fired;
    if (portal.activation === "intent" && goal >= 0) {
      // From the same spot, holding the opposite direction must never leave the map.
      const inward = createActor(world, gx, gy);
      inward.latched.clear();
      let inwardFired = false;
      push(world, inward, -portal.dirX, -portal.dirY, budget, pushTicks, (other) => {
        inwardFired = inwardFired || other.id === portal.id;
        return inwardFired;
      });
      result.inwardFired = inwardFired;
    }
  }
  Object.assign(result, {ticks: stats.ticks, distance: round3(stats.distance), maxStep: round3(stats.maxStep),
    zeroMotionTicks: stats.zeroMotionTicks, blocked: stats.blocked, firedEnRoute: [...enRoute],
    end: [round3(actor.x), round3(actor.y)]});
  const ex = actor.x - (exact || target.kind === "interaction" ? target.x : gx);
  const ey = actor.y - (exact || target.kind === "interaction" ? target.y : gy);
  const arrived = target.kind === "exit" ? fired
    : target.kind === "interaction" ? Math.sqrt(ex * ex + ey * ey) <= target.reach + EPS
      : Math.abs(ex) <= 1e-6 && Math.abs(ey) <= 1e-6;
  if (stats.blocked) reasons.push("walker blocked on the planned path");
  if (stats.limitHit) reasons.push("tick limit reached");
  if (stats.zeroMotionTicks > 0) reasons.push("zero-motion ticks");
  if (stats.maxStep > budget + EPS) reasons.push("movement budget exceeded");
  if (walking && !arrived) reasons.push(target.kind === "exit" ? "exit did not fire" : "did not arrive");
  if (result.inwardFired) reasons.push("walking inward fired the exit");
  result.ok = reasons.length === 0;
  result.reason = reasons.length ? reasons.join("; ") : null;
  return result;
}

/** Arrival spawns of each portal (entranceByFrom) must exist and lie outside the trigger. */
export function portalArrivals(world) {
  return world.portals.map((portal) => ({
    id: portal.id, activation: portal.activation, to: portal.to,
    arrivals: Object.entries(portal.entranceByFrom).map(([from, spawnId]) => {
      const spawn = world.spawnById.get(spawnId);
      return {from, spawn: spawnId, found: Boolean(spawn),
        insideTrigger: spawn ? insideTrigger(portal, spawn.x, spawn.y) : null,
        inZone: spawn ? inPortalZone(portal, spawn.x, spawn.y) : null,
        latchedOnArrival: spawn ? portal.latch && inPortalZone(portal, spawn.x, spawn.y) : null};
    }),
  }));
}

/**
 * Walk from the spawns to every exit, interaction, anchor approach point and
 * slot with the same stepActor() a game tick uses: each target is walked from
 * the first spawn that reaches it. Exits must fire with their travel direction
 * and must not fire when walking inward; the walk may never stall, idle or
 * exceed the per-tick budget. speed is in world px per second.
 */
export function traverseRoutes(world, {speed} = {}) {
  const pxPerSecond = finite(speed, "speed");
  if (!(pxPerSecond > 0)) throw new RangeError("speed must be positive");
  const budget = pxPerSecond / TICK_HZ;
  const floods = new Map(world.spawns.map((spawn) => [spawn.id, flood(world, spawn.x, spawn.y)]));
  const spawns = world.spawns.map((spawn) => ({id: spawn.id, valid: !isBlocked(world, spawn.x, spawn.y),
    reachableCells: floods.get(spawn.id).reached}));
  const results = targetsOf(world).map((target) => routeTo(world, floods, target, budget));
  const portals = portalArrivals(world);
  const arrivalsOk = portals.every((portal) => portal.arrivals.every((item) => item.found && !item.insideTrigger));
  return {
    ok: spawns.length > 0 && spawns.every((spawn) => spawn.valid) && results.every((item) => item.ok) && arrivalsOk,
    speed: pxPerSecond, budget: round3(budget), tickHz: TICK_HZ, cell: world.cell, spawns, results, portals,
  };
}

// --------------------------------------------------------------------------- snapshot

/** Plain JSON state of a world and actor; callers add their own fields. */
export function runtimeSnapshot(world, actor, extra = {}) {
  return {
    schema: SNAPSHOT_SCHEMA,
    runtime: {name: "map-runtime", version: RUNTIME_VERSION},
    world: {width: world.width, height: world.height, actorRadius: world.actorRadius, ySquash: world.ySquash,
      cell: world.cell, tickHz: TICK_HZ, solids: world.solids.length, portals: world.portals.length},
    actor: actor === null ? null : {
      x: round3(actor.x), y: round3(actor.y), valid: !isBlocked(world, actor.x, actor.y),
      facing: [round3(actor.facingX), round3(actor.facingY)], latched: [...actor.latched].sort(),
      travelled: round3(actor.travelled), pathLength: actor.path === null ? 0 : actor.path.length,
    },
    ...extra,
  };
}
