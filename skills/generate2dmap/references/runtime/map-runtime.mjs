/*
 * map-runtime.mjs 1.1.0: collision query, navigation grid, walker and exits
 * for generate2dmap scenes (map_bundle.v2).
 *
 * One dependency-free ES module for browsers, Node 18+ and bundlers. Games can
 * import it as it is; build_scene_preview.py inlines it verbatim into
 * preview.html. It reads the bundle fields it needs (world, collision,
 * objects, props, portals, spawns, interactions, anchors) and never touches
 * the DOM, the network, a clock or a random source: the same calls give the
 * same results.
 *
 * COLLISION mirrors forge_nav (shared/forge_nav.py, vendored as
 * scripts/forge_nav.py) rule for rule: the rule book N1-N15 in its docstring
 * (integration decisions D1-D7) is the specification, and every formula
 * below keeps its operation order, so the answers are bit-identical except
 * for the sine and cosine of a rotation (V8 and a C runtime can differ by one
 * ulp, for example at sin(pi / 4); points exactly on a rotated edge may then
 * fall on different sides). In short:
 *   N1  world px, y down; theta = rotate * (PI / 180), no trigonometry at 0
 *   N2  9 samples: P, then (rx,0) (dx,dy) (0,ry) (-dx,dy) (-rx,0) (-dx,-dy)
 *       (0,-ry) (dx,-dy) with rx = r, ry = r * ySquash, dx = rx * SQRT1_2
 *   N3  walk area: no regions = the closed box [0, W] x [0, H]; otherwise
 *       inside some region (even-odd, edge a = p[i], b = p[i - 1], skipped
 *       when a.y == b.y, toggle when (a.y > y) != (b.y > y) and
 *       x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x) and none of its holes
 *   N4  blockers: collision.solids, collision.rects, footprints of solid
 *       objects, placed tiles' collision (build_scene_preview converts it into
 *       collision.solids, since this module reads no files) and blocking
 *       material pixels; shapes without area are dropped
 *   N5  solids are closed: rect x <= px <= x + w; ellipse nu*nu + nv*nv <= 1;
 *       polygon interior or on an edge
 *   N6  footprints: from the object, else from bundle.props[prop] (resolved
 *       inline items; pack + label entries must be resolved first); basis
 *       world_px is not scaled, prop_px and image_px are scaled once by the
 *       instance scale; flip_x mirrors offset x and rotate
 *   N7  tile collision: each placed tile's shapes moved to its cell, and a
 *       tile without shapes whose walkable is false blocks its cell; forge_nav
 *       reads the tileset files, so build_scene_preview passes the result in
 *       as collision.solids (their source names the layer), closed like
 *       every solid (N5)
 *   N8  material grid: square pixels of s = W / width world px (a whole
 *       number), half-open (right and bottom edges belong to the next pixel);
 *       solid blocks; liquid and hazard block unless walkable; decor never;
 *       one_way never blocks a point (N11)
 *   N9  valid = all 9 samples in the walk area and on no blocker
 *   N10 segmentClear: n = max(1, ceil(len / (cell / 2))) samples
 *       a + d * k / n are valid; the one_way rule holds; and (thin-gap rule)
 *       the centre stays in the walk area and off every blocker on the whole
 *       segment, tested at the midpoint of every piece between boundary cuts
 *   N11 one_way blocks moving down (+y) onto it: no footprint sample may step
 *       from another pixel onto one_way between consecutive samples, and the
 *       centre path may not enter it
 *   N12 cell = max(1, floor(r / 2 + 0.5)); node centres (i + 0.5) * cell;
 *       at most 2^24 nodes
 *   N13 4-neighbour moves, open when segmentClear holds between the centres
 *   N14 a point joins every valid node within two cells (of its clamped
 *       cell) that it reaches by segmentClear, nearest first; starts seed the
 *       search with every joined node; point targets must be valid and join a
 *       reached node; reach targets need a reached centre with
 *       dx*dx + dy*dy <= reach*reach; triggers are closed; intent exits need a
 *       reached node within radius; crossing exits a reached node inside the
 *       trigger, else one within two cells whose straight move to the
 *       trigger's closest point is clear
 *   N15 runtime duties (not in forge_nav): intent fires when the normalised
 *       dot(intent, travelDirection) > 0.25 within radius; crossing fires
 *       inside the trigger with movement unless requiresMovement is false;
 *       latches (default true) hold until the actor leaves the zone
 *
 * Planned paths are walked as planned: findPath() flags its waypoints
 * clear: true (each leg passed segmentClear when it was planned) and
 * advanceAlongPath() follows those legs without re-testing tick positions;
 * keyboard moves go through moveWithCollision().
 */

export const RUNTIME_VERSION = "1.1.0";
export const TICK_HZ = 60;
export const INTENT_MIN_COS = 0.25;
export const SNAPSHOT_SCHEMA = "generate2dmap.scene_snapshot.v1";
export const FREE = 0;
export const BLOCK = 1;
export const ONE_WAY = 2;
export const MAX_GRID_NODES = 1 << 24;
export const MATERIAL_CLASSES = Object.freeze(["solid", "one_way", "liquid", "hazard", "decor"]);
export const FOOTPRINT_BASES = Object.freeze(["prop_px", "world_px", "image_px"]);

const EPS = 1e-9;
const SQRT1_2 = 0.7071067811865476; // Math.SQRT1_2, forge_nav.SQRT1_2
const DEG = Math.PI / 180; // math.radians multiplies by this exact double
const INDEX_BUCKET = 32;
const MAX_INDEX_CELLS = 1 << 22;
const SMOOTH_LOOKAHEAD = 64;
const JOIN_RINGS = 2;
const SNAP_RINGS = 4;
const STAND_REASON = "the actor cannot stand here (footprint blocked)";

/** Unit directions of the 8 footprint samples after the centre: E, SE, S, SW, W, NW, N, NE (y down). */
export const FOOTPRINT_DIRECTIONS = Object.freeze([
  [1, 0], [SQRT1_2, SQRT1_2], [0, 1], [-SQRT1_2, SQRT1_2],
  [-1, 0], [-SQRT1_2, -SQRT1_2], [0, -1], [SQRT1_2, -SQRT1_2],
].map((direction) => Object.freeze(direction)));

const ACTIVATIONS = new Set(["crossing", "intent"]);
const FACINGS = {
  e: [1, 0], east: [1, 0], right: [1, 0], w: [-1, 0], west: [-1, 0], left: [-1, 0],
  s: [0, 1], south: [0, 1], down: [0, 1], n: [0, -1], north: [0, -1], up: [0, -1],
  ne: [SQRT1_2, -SQRT1_2], "north-east": [SQRT1_2, -SQRT1_2], northeast: [SQRT1_2, -SQRT1_2],
  nw: [-SQRT1_2, -SQRT1_2], "north-west": [-SQRT1_2, -SQRT1_2], northwest: [-SQRT1_2, -SQRT1_2],
  se: [SQRT1_2, SQRT1_2], "south-east": [SQRT1_2, SQRT1_2], southeast: [SQRT1_2, SQRT1_2],
  sw: [-SQRT1_2, SQRT1_2], "south-west": [-SQRT1_2, SQRT1_2], southwest: [-SQRT1_2, SQRT1_2],
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

function flag(value, where) {
  if (typeof value !== "boolean") throw new TypeError(`${where} must be true or false`);
  return value;
}

function plainObject(value, where) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(`${where} must be an object`);
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

// --------------------------------------------------------------------------- shapes (N3, N5)

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

/** Twice the signed shoelace area: sum(x[i] * y[i + 1]) - sum(y[i] * x[i + 1]) (N4). */
function shoelace(xs, ys) {
  let a = 0, b = 0;
  for (let i = 0; i < xs.length; i++) {
    const j = i + 1 === xs.length ? 0 : i + 1;
    a += xs[i] * ys[j];
    b += ys[i] * xs[j];
  }
  return a - b;
}

function polygonShape(points, where, source, closed) {
  if (!Array.isArray(points) || points.length < 3) throw new TypeError(`${where} must list at least 3 [x, y] points`);
  const xs = new Float64Array(points.length), ys = new Float64Array(points.length);
  points.forEach((item, k) => {
    const [x, y] = readPoint(item, `${where}[${k}]`);
    xs[k] = x;
    ys[k] = y;
  });
  return {kind: "polygon", xs, ys, closed, area2: shoelace(xs, ys), ...boundsOf(xs, ys), source};
}

function rectShape(x, y, w, h, source) {
  const x1 = x + w, y1 = y + h;
  return {kind: "rect", x, y, x1, y1, minX: x, minY: y, maxX: x1, maxY: y1, empty: !(w > 0 && h > 0), source};
}

function ellipseShape(cx, cy, rx, ry, rotate, source) {
  const turned = rotate !== 0;
  const theta = rotate * DEG;
  const cos = turned ? Math.cos(theta) : 1, sin = turned ? Math.sin(theta) : 0;
  const local = !(sin === 0 && cos === 1); // forge_nav evaluates the rotation unless c = 1 and s = 0 exactly
  const ex = Math.sqrt(rx * cos * (rx * cos) + ry * sin * (ry * sin));
  const ey = Math.sqrt(rx * sin * (rx * sin) + ry * cos * (ry * cos));
  const pad = 1e-7 + 1e-9 * (ex + ey);
  return {
    kind: "ellipse", cx, cy, rx, ry, rotate, local, cos, sin, empty: !(rx > 0 && ry > 0),
    minX: cx - ex - pad, minY: cy - ey - pad, maxX: cx + ex + pad, maxY: cy + ey + pad, source,
  };
}

/** N3 even-odd crossing test (pnpoly), in forge_nav's operation order. */
export function insidePolygon(xs, ys, px, py) {
  let inside = false;
  const n = xs.length;
  for (let i = 0; i < n; i++) {
    const j = i === 0 ? n - 1 : i - 1;
    const ax = xs[i], ay = ys[i], bx = xs[j], by = ys[j];
    if (ay === by) continue;
    if ((ay > py) !== (by > py) && px < (bx - ax) * (py - ay) / (by - ay) + ax) inside = !inside;
  }
  return inside;
}

/** N5: the point lies on an edge a = p[i], b = p[i - 1] (exact for axis-aligned edges and vertices). */
export function onPolygonEdge(xs, ys, px, py) {
  const n = xs.length;
  for (let i = 0; i < n; i++) {
    const j = i === 0 ? n - 1 : i - 1;
    const ax = xs[i], ay = ys[i], bx = xs[j], by = ys[j];
    if (px >= Math.min(ax, bx) && px <= Math.max(ax, bx) && py >= Math.min(ay, by) && py <= Math.max(ay, by)
        && (bx - ax) * (py - ay) - (by - ay) * (px - ax) === 0) return true;
  }
  return false;
}

/** True when (px, py) lies in a compiled solid; solids are closed sets (N5). */
export function insideShape(shape, px, py) {
  if (shape.kind === "rect") return px >= shape.x && px <= shape.x1 && py >= shape.y && py <= shape.y1;
  if (shape.kind === "ellipse") {
    const ex = px - shape.cx, ey = py - shape.cy;
    let u = ex, v = ey;
    if (shape.local) {
      u = ex * shape.cos + ey * shape.sin;
      v = ey * shape.cos - ex * shape.sin;
    }
    const nu = u / shape.rx, nv = v / shape.ry;
    return nu * nu + nv * nv <= 1;
  }
  return insidePolygon(shape.xs, shape.ys, px, py) || (shape.closed && onPolygonEdge(shape.xs, shape.ys, px, py));
}

function compileSolid(solid, where) {
  plainObject(solid, where);
  const source = typeof solid.source === "string" ? solid.source : (typeof solid.id === "string" ? solid.id : where);
  if (solid.shape === "rect") {
    return rectShape(finite(solid.x, `${where}.x`), finite(solid.y, `${where}.y`),
      finite(solid.w, `${where}.w`), finite(solid.h, `${where}.h`), source);
  }
  if (solid.shape === "ellipse") {
    return ellipseShape(finite(solid.cx, `${where}.cx`), finite(solid.cy, `${where}.cy`),
      finite(solid.rx, `${where}.rx`), finite(solid.ry, `${where}.ry`),
      solid.rotate === undefined ? 0 : finite(solid.rotate, `${where}.rotate`), source);
  }
  if (solid.shape === "polygon") {
    const shape = polygonShape(solid.points, `${where}.points`, source, true);
    if (shape.area2 === 0) throw new RangeError(`${where}.points: polygon has zero area`);
    return shape;
  }
  throw new TypeError(`${where}.shape must be rect, ellipse or polygon`);
}

/** N4: a shape without area blocks nothing. */
function hasArea(shape) {
  return shape.kind === "polygon" ? shape.area2 !== 0 : !shape.empty;
}

/**
 * The world solid of a footprint anchored at (x, y) (N6), as a compiled shape, or null for shape none,
 * a missing footprint or one without area. Basis world_px is not scaled; prop_px (the default) and its
 * legacy alias image_px are scaled once by `scale`; flipX mirrors the footprint around x.
 */
export function footprintSolid(x, y, footprint, {scale = 1, flipX = false, source = "footprint"} = {}) {
  if (!footprint || (footprint.shape !== "ellipse" && footprint.shape !== "rect")) return null;
  const basis = footprint.basis === undefined ? "prop_px" : footprint.basis;
  if (!FOOTPRINT_BASES.includes(basis)) {
    throw new TypeError(`${source}: footprint basis ${JSON.stringify(basis)} is not one of ${FOOTPRINT_BASES.join(", ")}`);
  }
  const k = basis === "world_px" ? 1 : scale;
  const offset = footprint.offset || [0, 0];
  let ox = offset[0];
  const oy = offset[1];
  let rotate = Number(footprint.rotate || 0);
  if (flipX) {
    if (ox) ox = -ox;
    if (rotate) rotate = -rotate;
  }
  const cx = x + k * ox, cy = y + k * oy;
  const width = k * footprint.width, depth = k * footprint.depth;
  if (width <= 0 || depth <= 0) return null;
  if (footprint.shape === "ellipse") return ellipseShape(cx, cy, width / 2, depth / 2, rotate, source);
  if (rotate === 0) return rectShape(cx - width / 2, cy - depth / 2, width, depth, source);
  const theta = rotate * DEG, cos = Math.cos(theta), sin = Math.sin(theta);
  const corners = [[-width / 2, -depth / 2], [width / 2, -depth / 2], [width / 2, depth / 2], [-width / 2, depth / 2]]
    .map(([u, v]) => [cx + u * cos - v * sin, cy + u * sin + v * cos]);
  return polygonShape(corners, source, source, true);
}

/**
 * The blocking footprint of one bundle object (N6) or null. `prop` is the object's resolved
 * props-registry item, if any: the object's own footprint and solid win, else the item's. Without a
 * solid flag either way the object is solid exactly when its footprint is an ellipse or a rect.
 */
export function objectSolid(object, prop = null, where = "object") {
  const footprint = object.footprint !== undefined ? object.footprint : (prop ? prop.footprint : undefined);
  if (footprint !== undefined && footprint !== null && (typeof footprint !== "object" || Array.isArray(footprint))) {
    throw new TypeError(`${where}: footprint must be an object`);
  }
  let solid = object.solid !== undefined ? object.solid
    : (prop && prop.solid !== undefined && prop.solid !== null ? prop.solid : null);
  if (solid === null) solid = Boolean(footprint) && (footprint.shape === "ellipse" || footprint.shape === "rect");
  if (!flag(solid, `${where}.solid`) || !footprint || (footprint.shape !== "ellipse" && footprint.shape !== "rect")) {
    return null;
  }
  for (const key of ["width", "depth"]) nonNegative(footprint[key], `${where}.footprint.${key}`);
  if (footprint.offset !== undefined && footprint.offset !== null) {
    if (!Array.isArray(footprint.offset) || footprint.offset.length !== 2) {
      throw new TypeError(`${where}.footprint.offset must be [x, y]`);
    }
    footprint.offset.forEach((value, k) => finite(value, `${where}.footprint.offset[${k}]`));
  }
  if (footprint.rotate !== undefined) finite(footprint.rotate, `${where}.footprint.rotate`);
  const x = finite(object.x, `${where}.x`), y = finite(object.y, `${where}.y`);
  const scale = object.scale === undefined ? 1 : finite(object.scale, `${where}.scale`);
  if (!(scale > 0)) throw new RangeError(`${where}.scale must be positive`);
  const flipX = object.flip_x === undefined ? false : flag(object.flip_x, `${where}.flip_x`);
  return footprintSolid(x, y, footprint, {scale, flipX, source: `object:${object.id}`});
}

/** The props registry of a bundle (N6): name -> inline item; pack + label entries must be resolved first. */
function propsRegistry(bundle) {
  const registry = bundle.props;
  const found = new Map();
  if (registry === undefined || registry === null) return found;
  plainObject(registry, "props");
  for (const [name, entry] of Object.entries(registry)) {
    const where = `props.${name}`;
    plainObject(entry, where);
    if (entry.pack !== undefined) {
      throw new TypeError(`${where} names a prop pack (pack + label); resolve it into an inline item first `
        + "(build_scene_preview.py does)");
    }
    if (entry.solid !== undefined && entry.solid !== null) flag(entry.solid, `${where}.solid`);
    found.set(name, entry);
  }
  return found;
}

// --------------------------------------------------------------------------- spatial indexes

function bucketIndex(items) {
  const live = [];
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  items.forEach((item, k) => {
    live.push(k);
    minX = Math.min(minX, item.minX - 1);
    minY = Math.min(minY, item.minY - 1);
    maxX = Math.max(maxX, item.maxX + 1);
    maxY = Math.max(maxY, item.maxY + 1);
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
    const item = items[k];
    const i0 = Math.floor((item.minX - 1) / size) - x0, i1 = Math.floor((item.maxX + 1) / size) - x0;
    const j0 = Math.floor((item.minY - 1) / size) - y0, j1 = Math.floor((item.maxY + 1) / size) - y0;
    for (let j = j0; j <= j1; j++) {
      for (let i = i0; i <= i1; i++) {
        const c = j * cols + i;
        (cells[c] || (cells[c] = [])).push(k);
      }
    }
  }
  return {size, x0, y0, cols, rows, cells, stamp: new Uint32Array(items.length), mark: 0};
}

function candidates(index, px, py) {
  if (index === null) return null;
  const i = Math.floor(px / index.size) - index.x0, j = Math.floor(py / index.size) - index.y0;
  if (!(i >= 0 && j >= 0 && i < index.cols && j < index.rows)) return null;
  return index.cells[j * index.cols + i] || null;
}

// Every indexed item whose buckets meet the box, once.
function itemsInBox(index, minX, minY, maxX, maxY, visit) {
  if (index === null) return;
  index.mark = (index.mark + 1) >>> 0;
  if (index.mark === 0) {
    index.stamp.fill(0);
    index.mark = 1;
  }
  const i0 = Math.max(0, Math.floor(minX / index.size) - index.x0), i1 = Math.min(index.cols - 1, Math.floor(maxX / index.size) - index.x0);
  const j0 = Math.max(0, Math.floor(minY / index.size) - index.y0), j1 = Math.min(index.rows - 1, Math.floor(maxY / index.size) - index.y0);
  for (let j = j0; j <= j1; j++) {
    for (let i = i0; i <= i1; i++) {
      const bucket = index.cells[j * index.cols + i];
      if (!bucket) continue;
      for (const k of bucket) {
        if (index.stamp[k] === index.mark) continue;
        index.stamp[k] = index.mark;
        visit(k);
      }
    }
  }
}

// Boundary elements for the thin-gap cuts of N10: polygon edges, rect sides, the world box, ellipses.
function boundaryElements(world) {
  const elements = [];
  const edge = (ax, ay, bx, by) => elements.push({kind: "edge", ax, ay, bx, by, minX: Math.min(ax, bx),
    minY: Math.min(ay, by), maxX: Math.max(ax, bx), maxY: Math.max(ay, by)});
  const polygon = (shape) => {
    for (let i = 0; i < shape.xs.length; i++) {
      const j = i === 0 ? shape.xs.length - 1 : i - 1;
      edge(shape.xs[i], shape.ys[i], shape.xs[j], shape.ys[j]);
    }
  };
  const rect = (x0, y0, x1, y1) => {
    edge(x0, y0, x1, y0);
    edge(x1, y0, x1, y1);
    edge(x1, y1, x0, y1);
    edge(x0, y1, x0, y0);
  };
  for (const region of world.walkRegions) {
    polygon(region.outer);
    region.holes.forEach(polygon);
  }
  if (!world.walkRegions.length) rect(0, 0, world.width, world.height);
  for (const shape of world.solids) {
    if (shape.kind === "polygon") polygon(shape);
    else if (shape.kind === "rect") rect(shape.x, shape.y, shape.x1, shape.y1);
    else elements.push({kind: "ellipse", shape, minX: shape.minX, minY: shape.minY, maxX: shape.maxX, maxY: shape.maxY});
  }
  return elements;
}

// Parameters t in (0, 1) where p + t * d meets the edge a -> b (forge_nav _segment_edge_params).
function edgeCuts(px, py, dx, dy, element, out) {
  const ex = element.bx - element.ax, ey = element.by - element.ay;
  const wx = element.ax - px, wy = element.ay - py;
  const denom = dx * ey - dy * ex;
  if (denom !== 0) {
    const t = (wx * ey - wy * ex) / denom, u = (wx * dy - wy * dx) / denom;
    if (u >= 0 && u <= 1 && t > 0 && t < 1) out.push(t);
    return;
  }
  const length = dx * dx + dy * dy;
  if (wx * dy - wy * dx === 0 && length > 0) { // collinear overlap: the edge's ends split the segment
    for (const t of [(wx * dx + wy * dy) / length, ((element.bx - px) * dx + (element.by - py) * dy) / length]) {
      if (t > 0 && t < 1) out.push(t);
    }
  }
}

// Both roots of |p + t d| on an ellipse boundary (forge_nav _Ellipse._roots).
function ellipseCuts(px, py, dx, dy, shape, out) {
  let u0 = px - shape.cx, v0 = py - shape.cy;
  if (shape.local) {
    const ex = u0, ey = v0;
    u0 = ex * shape.cos + ey * shape.sin;
    v0 = ey * shape.cos - ex * shape.sin;
  }
  const du = dx * shape.cos + dy * shape.sin, dv = dy * shape.cos - dx * shape.sin;
  const rx2 = shape.rx * shape.rx, ry2 = shape.ry * shape.ry;
  const a = (du / shape.rx) * (du / shape.rx) + (dv / shape.ry) * (dv / shape.ry);
  const b = 2 * (u0 * du / rx2 + v0 * dv / ry2);
  const c = (u0 / shape.rx) * (u0 / shape.rx) + (v0 / shape.ry) * (v0 / shape.ry) - 1;
  const disc = b * b - 4 * a * c;
  if (!(disc >= 0) || !(a > 0)) return;
  const root = Math.sqrt(disc);
  for (const t of [(-b - root) / (2 * a), (-b + root) / (2 * a)]) if (t > 0 && t < 1) out.push(t);
}

// Material pixel edges crossed by the segment (forge_nav _Materials.seg_breaks).
function materialCuts(material, px, py, dx, dy, out) {
  const s = material.scale;
  for (const [start, delta, limit] of [[px, dx, material.width], [py, dy, material.height]]) {
    if (!delta) continue;
    const lo = Math.min(start, start + delta), hi = Math.max(start, start + delta);
    const first = Math.max(0, Math.ceil(lo / s)), last = Math.min(limit, Math.floor(hi / s));
    for (let m = first; m <= last; m++) {
      const t = (m * s - start) / delta;
      if (t > 0 && t < 1) out.push(t);
    }
  }
}

// --------------------------------------------------------------------------- material grid (N8)

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

function bitPlane(value, count, where) {
  const bytes = typeof value === "string" ? decodeBase64(value) : value;
  if (!(bytes instanceof Uint8Array) || bytes.length < Math.ceil(count / 8)) {
    throw new TypeError(`${where} must hold width * height bits`);
  }
  return bytes;
}

/**
 * Decode a material grid: {width, height, cellWidth, cellHeight, bits, oneWay?}. bits (base64 or
 * Uint8Array, bit k LSB first for pixel k = j * width + i) marks BLOCK pixels, the optional oneWay plane
 * ONE_WAY pixels; build_scene_preview.py writes this form. Pixels are squares of a whole number of world
 * px (N8): cellWidth must equal cellHeight. Returns {width, height, scale, codes, hasOneWay}.
 */
export function decodeMaterialGrid(grid) {
  plainObject(grid, "materialGrid");
  const width = finite(grid.width, "materialGrid.width"), height = finite(grid.height, "materialGrid.height");
  if (!(Number.isInteger(width) && Number.isInteger(height) && width > 0 && height > 0)) {
    throw new RangeError("materialGrid width and height must be positive integers");
  }
  const cellWidth = finite(grid.cellWidth, "materialGrid.cellWidth"), cellHeight = finite(grid.cellHeight, "materialGrid.cellHeight");
  if (!(Number.isInteger(cellWidth) && cellWidth >= 1 && cellHeight === cellWidth)) {
    throw new RangeError("materialGrid pixels must be squares of a whole number of world px (cellWidth = cellHeight >= 1)");
  }
  const count = width * height;
  const blocked = bitPlane(grid.bits, count, "materialGrid.bits");
  const oneWay = grid.oneWay === undefined || grid.oneWay === null ? null : bitPlane(grid.oneWay, count, "materialGrid.oneWay");
  const codes = new Uint8Array(count);
  let hasOneWay = false;
  for (let k = 0; k < count; k++) {
    const block = (blocked[k >> 3] >> (k & 7)) & 1, up = oneWay === null ? 0 : (oneWay[k >> 3] >> (k & 7)) & 1;
    if (block && up) throw new RangeError(`materialGrid pixel ${k} is both blocking and one_way`);
    codes[k] = block ? BLOCK : up ? ONE_WAY : FREE;
    hasOneWay = hasOneWay || up === 1;
  }
  return {width, height, scale: cellWidth, codes, hasOneWay};
}

/** The code of a material_map class (N8): BLOCK, ONE_WAY or FREE. */
export function materialCode(spec) {
  plainObject(spec, "material");
  if (!MATERIAL_CLASSES.includes(spec.class)) throw new TypeError(`material class ${JSON.stringify(spec.class)} is not one of ${MATERIAL_CLASSES.join(", ")}`);
  const walkable = spec.walkable === undefined ? false : flag(spec.walkable, "material walkable");
  if (spec.class === "solid" || ((spec.class === "liquid" || spec.class === "hazard") && !walkable)) return BLOCK;
  return spec.class === "one_way" ? ONE_WAY : FREE;
}

/** True when a material class blocks a point (N8): solid; liquid and hazard unless walkable. one_way never does. */
export function materialBlocks(spec) {
  return materialCode(spec) === BLOCK;
}

function materialColor(value, where) {
  if (typeof value === "string" && /^#[0-9a-fA-F]{6}/.test(value)) return [1, 3, 5].map((k) => parseInt(value.slice(k, k + 2), 16));
  if (Array.isArray(value) && value.length >= 3 && value.slice(0, 3).every((v) => Number.isInteger(v))) return value.slice(0, 3);
  throw new TypeError(`${where} must be #rrggbb or [r, g, b]`);
}

function bitsOf(mask) {
  const bytes = new Uint8Array(Math.ceil(mask.length / 8));
  for (let k = 0; k < mask.length; k++) if (mask[k]) bytes[k >> 3] |= 1 << (k & 7);
  return bytes;
}

/**
 * The material grid of decoded straight-alpha RGBA pixels (a canvas getImageData result, for example)
 * by exact colour (N8): alpha 0 is no material; any other pixel must match exactly one material colour.
 * Materials given by palette `index` need the indices, which RGBA has lost: use the grid that
 * build_scene_preview.py embeds instead.
 */
export function materialGridFromRGBA(rgba, width, height, materialMap, worldWidth, worldHeight) {
  const materials = materialMap && materialMap.materials;
  plainObject(materials, "material_map.materials");
  const s = worldWidth / width;
  if (!(Number.isInteger(s) && s >= 1 && s * height === worldHeight)) {
    throw new RangeError(`material_map: ${width}x${height} px does not divide the ${worldWidth}x${worldHeight} world into whole squares`);
  }
  const entries = Object.entries(materials).map(([name, spec]) => {
    const where = `material_map.materials.${name}`;
    plainObject(spec, where);
    if (spec.color === undefined) throw new TypeError(`${where}: every material needs a color (index materials need the embedded grid)`);
    return {name, code: materialCode(spec), rgb: materialColor(spec.color, `${where}.color`)};
  });
  const keys = new Set(entries.map((entry) => entry.rgb.join(",")));
  if (keys.size !== entries.length) throw new RangeError("material_map.materials: material colors must be unique");
  const blocked = new Uint8Array(width * height), oneWay = new Uint8Array(width * height);
  for (let k = 0; k < width * height; k++) {
    const o = k * 4;
    if (rgba[o + 3] === 0) continue;
    const entry = entries.find(({rgb}) => rgba[o] === rgb[0] && rgba[o + 1] === rgb[1] && rgba[o + 2] === rgb[2]);
    if (!entry) throw new RangeError(`material_map: pixel (${k % width}, ${Math.floor(k / width)}) matches no material`);
    blocked[k] = entry.code === BLOCK ? 1 : 0;
    oneWay[k] = entry.code === ONE_WAY ? 1 : 0;
  }
  return {width, height, cellWidth: s, cellHeight: s, bits: bitsOf(blocked), oneWay: bitsOf(oneWay)};
}

/** The material code under (px, py): FREE outside the image (N8). */
export function materialAt(world, px, py) {
  const material = world.material;
  if (material === null) return FREE;
  const mx = Math.floor(px / material.scale), my = Math.floor(py / material.scale);
  if (!(mx >= 0 && mx < material.width && my >= 0 && my < material.height)) return FREE;
  return material.codes[my * material.width + mx];
}

// --------------------------------------------------------------------------- world

function compilePortal(portal, where) {
  plainObject(portal, where);
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
    plainObject(portal.entranceByFrom, `${where}.entranceByFrom`);
    for (const [from, entry] of Object.entries(portal.entranceByFrom)) {
      entranceByFrom[from] = typeof entry === "string" ? text(entry, `${where}.entranceByFrom.${from}`)
        : readPoint(entry, `${where}.entranceByFrom.${from}`);
    }
  }
  const colon = to.indexOf(":");
  return {
    id, to, toMap: colon < 0 ? to : to.slice(0, colon), toSpawn: colon < 0 ? null : to.slice(colon + 1),
    activation, ...shape, dirX, dirY, hasDirection: portal.travelDirection !== undefined, radius,
    latch: portal.latch === undefined ? true : flag(portal.latch, `${where}.latch`),
    requiresMovement: portal.requiresMovement === undefined ? true : flag(portal.requiresMovement, `${where}.requiresMovement`),
    entranceByFrom,
  };
}

function facingVector(facing) {
  if (typeof facing === "number" && Number.isFinite(facing)) {
    const theta = facing * DEG;
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

/** Cell size of the navigation grid (N12): max(1, round half up (r / 2)). */
export function navCellSize(actorRadius) {
  return Math.max(1, Math.floor(actorRadius / 2 + 0.5));
}

/** The 9 sample offsets of N2, as [x, y] pairs, centre first. */
export function footprintOffsets(radius, ySquash = 1) {
  const rx = radius, ry = rx * ySquash;
  const dx = rx * SQRT1_2, dy = ry * SQRT1_2;
  return [[0, 0], [rx, 0], [dx, dy], [0, ry], [-dx, dy], [-rx, 0], [-dx, -dy], [0, -ry], [dx, -dy]];
}

/**
 * Compile the map_bundle.v2 fields the runtime reads into a world object (N2-N8).
 * options.materialGrid is the material grid of material_map (see decodeMaterialGrid and
 * materialGridFromRGBA); without it the material map is not applied. Throws TypeError or RangeError on
 * malformed data, as forge_nav raises NavError.
 */
export function createMapRuntime(bundle, options = {}) {
  plainObject(bundle, "bundle");
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
    plainObject(region, where);
    const zeroArea = (shape, at) => {
      if (shape.area2 === 0) throw new RangeError(`${at}: polygon has zero area`);
      return shape;
    };
    return {
      outer: zeroArea(polygonShape(region.polygon, `${where}.polygon`, where, false), `${where}.polygon`),
      holes: (region.holes || []).map((hole, h) => zeroArea(polygonShape(hole, `${where}.holes[${h}]`, where, false),
        `${where}.holes[${h}]`)),
    };
  });
  const solids = [];
  (collision.solids || []).forEach((solid, k) => {
    const shape = compileSolid(solid, `collision.solids[${k}]`);
    if (hasArea(shape)) solids.push(shape);
  });
  (collision.rects || []).forEach((rect, k) => {
    const where = `collision.rects[${k}]`;
    if (!Array.isArray(rect) || rect.length !== 4) throw new TypeError(`${where} must be [x, y, w, h]`);
    const shape = rectShape(finite(rect[0], `${where}[0]`), finite(rect[1], `${where}[1]`),
      finite(rect[2], `${where}[2]`), finite(rect[3], `${where}[3]`), where);
    if (hasArea(shape)) solids.push(shape);
  });
  const props = propsRegistry(bundle);
  (bundle.objects || []).forEach((object, k) => {
    const where = `objects[${k}]`;
    plainObject(object, where);
    text(object.id, `${where}.id`);
    const prop = props.get(object.prop);
    if (props.size > 0 && prop === undefined) throw new TypeError(`${where} (${object.id}): unknown prop ${JSON.stringify(object.prop)}`);
    const shape = objectSolid(object, prop || null, where);
    if (shape && hasArea(shape)) {
      shape.objectId = object.id;
      solids.push(shape);
    }
  });

  const material = options.materialGrid ? decodeMaterialGrid(options.materialGrid) : null;
  if (material !== null && (material.width * material.scale !== width || material.height * material.scale !== height)) {
    throw new RangeError(`material grid ${material.width}x${material.height} of ${material.scale} px squares does not cover `
      + `the ${width}x${height} world`);
  }
  const portals = (bundle.portals || []).map((portal, k) => compilePortal(portal, `portals[${k}]`));
  const spawns = (bundle.spawns || []).map((spawn, k) => {
    const where = `spawns[${k}]`;
    plainObject(spawn, where);
    return {id: text(spawn.id, `${where}.id`), x: finite(spawn.x, `${where}.x`), y: finite(spawn.y, `${where}.y`),
      facing: facingVector(spawn.facing)};
  });
  const interactions = (bundle.interactions || []).map((item, k) => {
    const where = `interactions[${k}]`;
    plainObject(item, where);
    return {id: text(item.id, `${where}.id`), x: finite(item.x, `${where}.x`), y: finite(item.y, `${where}.y`),
      reach: item.reach === undefined ? null : nonNegative(item.reach, `${where}.reach`)};
  });
  const anchors = Object.entries(bundle.anchors || {}).map(([name, anchor]) => {
    const where = `anchors.${name}`;
    plainObject(anchor, where);
    return {name, point: readPoint(anchor.point, `${where}.point`),
      slots: (anchor.slots || []).map((slot, k) => readPoint(slot, `${where}.slots[${k}]`)),
      approach: readPoints(anchor.approach, `${where}.approach`)};
  });
  // Arrivals given as points are starts too (map_nav check: spawns and arrival points).
  const arrivals = [];
  for (const portal of portals) {
    for (const [from, entry] of Object.entries(portal.entranceByFrom)) {
      if (Array.isArray(entry)) arrivals.push({id: `${portal.id}<-${from}`, x: entry[0], y: entry[1], facing: null, arrival: true});
    }
  }

  const compiled = {
    version: RUNTIME_VERSION, width, height, actorRadius, ySquash, footprintRy: actorRadius * ySquash,
    offsets: footprintOffsets(actorRadius, ySquash), cell, sampleSpacing: cell / 2, walkRegions, solids,
    index: bucketIndex(solids), material, hasOneWay: material !== null && material.hasOneWay,
    portals, portalById: uniqueIds(portals, "portal"), spawns, spawnById: uniqueIds(spawns, "spawn"), arrivals,
    interactions, interactionById: uniqueIds(interactions, "interaction"), anchors, nav: null,
  };
  compiled.boundaries = bucketIndex(compiled.boundaryList = boundaryElements(compiled));
  return compiled;
}

// --------------------------------------------------------------------------- collision query (N3-N11)

/** In the walk area (N3). */
export function inWalkArea(world, px, py) {
  if (world.walkRegions.length === 0) return px >= 0 && px <= world.width && py >= 0 && py <= world.height;
  for (const region of world.walkRegions) {
    const outer = region.outer;
    if (!(py >= outer.minY && py < outer.maxY)) continue; // pnpoly is false outside [minY, maxY)
    if (!insidePolygon(outer.xs, outer.ys, px, py)) continue;
    if (!region.holes.some((hole) => insidePolygon(hole.xs, hole.ys, px, py))) return true;
  }
  return false;
}

/** On a blocker: a solid or a blocking material pixel (N4). */
export function blockedAt(world, px, py) {
  const near = candidates(world.index, px, py);
  if (near !== null) {
    for (const k of near) if (insideShape(world.solids[k], px, py)) return true;
  }
  return world.material !== null && materialAt(world, px, py) === BLOCK;
}

/** One sample (forge_nav centre_ok): in the walk area and on no blocker. */
export function pointFree(world, px, py) {
  return inWalkArea(world, px, py) && !blockedAt(world, px, py);
}

/** The 9 samples of the actor footprint at (x, y) (N2): the centre, then E, SE, S, SW, W, NW, N, NE. */
export function footprintSamples(world, x, y) {
  return world.offsets.map(([ox, oy]) => [x + ox, y + oy]);
}

/** N9: true when the actor can stand at (x, y). */
export function isValid(world, x, y) {
  for (const [ox, oy] of world.offsets) if (!pointFree(world, x + ox, y + oy)) return false;
  return true;
}

/** True when the actor cannot stand at (x, y). */
export function isBlocked(world, x, y) {
  return !isValid(world, x, y);
}

// Bit k set where footprint sample k lies on one_way.
function oneWayBits(world, x, y) {
  let bits = 0;
  world.offsets.forEach(([ox, oy], k) => {
    if (materialAt(world, x + ox, y + oy) === ONE_WAY) bits |= 1 << k;
  });
  return bits;
}

/** Sorted cut parameters in [0, 1] where any boundary may cross a -> b (0 and 1 included; N10). */
export function segmentBreaks(world, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, cuts = [0, 1];
  if (dx !== 0 || dy !== 0) {
    const pad = 1e-7;
    itemsInBox(world.boundaries, Math.min(ax, bx) - pad, Math.min(ay, by) - pad, Math.max(ax, bx) + pad,
      Math.max(ay, by) + pad, (k) => {
        const element = world.boundaryList[k];
        if (element.kind === "edge") edgeCuts(ax, ay, dx, dy, element, cuts);
        else ellipseCuts(ax, ay, dx, dy, element.shape, cuts);
      });
    if (world.material !== null) materialCuts(world.material, ax, ay, dx, dy, cuts);
  }
  cuts.sort((p, q) => p - q);
  return cuts.filter((t, k) => k === 0 || t !== cuts[k - 1]);
}

/**
 * Why the actor cannot move straight from a to b (forge_nav segment_status, N10/N11), or null when it
 * can. thinGap false tests only the sampled rule and the sampled one_way rule.
 */
export function segmentStatus(world, ax, ay, bx, by, {thinGap = true} = {}) {
  const dx = bx - ax, dy = by - ay;
  const n = Math.max(1, Math.ceil(Math.sqrt(dx * dx + dy * dy) / (world.cell / 2)));
  for (let k = 0; k <= n; k++) {
    const px = ax + dx * k / n, py = ay + dy * k / n;
    if (!isValid(world, px, py)) return `actor footprint blocked at (${px}, ${py})`;
  }
  const downward = by > ay && world.hasOneWay;
  if (downward) {
    let before = oneWayBits(world, ax, ay);
    for (let k = 1; k <= n; k++) {
      const bits = oneWayBits(world, ax + dx * k / n, ay + dy * k / n);
      if ((bits & ~before) !== 0) return "one_way material blocks moving down onto it";
      before = bits;
    }
  }
  if (!thinGap) return null;
  const ts = segmentBreaks(world, ax, ay, bx, by);
  let code = downward ? materialAt(world, ax, ay) : FREE;
  for (let i = 1; i < ts.length; i++) {
    const mid = (ts[i - 1] + ts[i]) / 2, mx = ax + dx * mid, my = ay + dy * mid;
    if (!pointFree(world, mx, my)) return `centre path leaves the walk area or crosses a blocker near (${mx}, ${my}) (thin-gap rule)`;
    if (downward) {
      const next = materialAt(world, mx, my);
      if (next === ONE_WAY && code !== ONE_WAY) return "centre path enters one_way material from above";
      code = next;
    }
  }
  if (downward && materialAt(world, bx, by) === ONE_WAY && code !== ONE_WAY) return "centre path enters one_way material from above";
  return null;
}

/** N10: the actor can move straight from a to b. */
export function segmentClear(world, ax, ay, bx, by, options) {
  return segmentStatus(world, ax, ay, bx, by, options) === null;
}

// --------------------------------------------------------------------------- navigation grid (N12, N13)

/** The navigation grid: {cell, cols, rows} with lazily cached node validity and moves. */
export function navGrid(world) {
  if (world.nav === null) {
    const cell = world.cell;
    const cols = Math.max(1, Math.ceil(world.width / cell)), rows = Math.max(1, Math.ceil(world.height / cell));
    if (cols * rows > MAX_GRID_NODES) {
      throw new RangeError(`the navigation grid would have ${cols * rows} nodes (cell ${cell} px for actorRadius `
        + `${world.actorRadius} on a ${world.width}x${world.height} world; the limit is ${MAX_GRID_NODES}): split the map `
        + "into chunks or check the actor radius");
    }
    const total = cols * rows;
    world.nav = {cell, cols, rows, state: new Uint8Array(total), east: new Uint8Array(total), south: new Uint8Array(total),
      north: new Uint8Array(total)};
  }
  return world.nav;
}

/** Centre of node k as [x, y]. */
export function cellCenter(nav, k) {
  const i = k % nav.cols, j = (k - i) / nav.cols;
  return [(i + 0.5) * nav.cell, (j + 0.5) * nav.cell];
}

/** True when node k's centre is a valid position (cached). */
export function cellValid(world, k) {
  const nav = navGrid(world);
  let state = nav.state[k];
  if (state === 0) {
    const [x, y] = cellCenter(nav, k);
    state = isValid(world, x, y) ? 1 : 2;
    nav.state[k] = state;
  }
  return state === 1;
}

/** N13: the 4-neighbour move from node k to node next is open (cached; S and N differ through one_way). */
export function moveOpen(world, k, next) {
  const nav = navGrid(world);
  if (!cellValid(world, k) || !cellValid(world, next)) return false;
  let plane, slot, from, to;
  if (next === k + 1 || next === k - 1) {
    plane = nav.east;
    slot = Math.min(k, next);
    from = slot;
    to = slot + 1;
  } else if (next === k + nav.cols) {
    plane = nav.south;
    slot = k;
    from = k;
    to = next;
  } else {
    plane = nav.north;
    slot = next;
    from = k;
    to = next;
  }
  let state = plane[slot];
  if (state === 0) {
    const [ax, ay] = cellCenter(nav, from), [bx, by] = cellCenter(nav, to);
    state = segmentClear(world, ax, ay, bx, by) ? 1 : 2;
    plane[slot] = state;
  }
  return state === 1;
}

function neighbours(nav, k) {
  const i = k % nav.cols, out = [];
  if (i + 1 < nav.cols) out.push(k + 1);
  if (k + nav.cols < nav.cols * nav.rows) out.push(k + nav.cols);
  if (i > 0) out.push(k - 1);
  if (k >= nav.cols) out.push(k - nav.cols);
  return out;
}

function clampedCell(nav, x, y) {
  const col = Math.min(Math.max(Math.floor(x / nav.cell), 0), nav.cols - 1);
  const row = Math.min(Math.max(Math.floor(y / nav.cell), 0), nav.rows - 1);
  return [row, col];
}

/**
 * N14 join: the valid nodes within two cells of the (clamped) cell holding (x, y) that a valid point
 * reaches by segmentClear, nearest first (squared distance, then row, then column). Empty when the
 * point is not a valid position.
 */
export function joinCells(world, x, y) {
  if (!isValid(world, x, y)) return [];
  const nav = navGrid(world), [row, col] = clampedCell(nav, x, y), found = [];
  for (let r = Math.max(0, row - JOIN_RINGS); r < Math.min(nav.rows, row + JOIN_RINGS + 1); r++) {
    for (let c = Math.max(0, col - JOIN_RINGS); c < Math.min(nav.cols, col + JOIN_RINGS + 1); c++) {
      const k = r * nav.cols + c;
      if (!cellValid(world, k)) continue;
      const cx = (c + 0.5) * nav.cell, cy = (r + 0.5) * nav.cell;
      found.push([(cx - x) * (cx - x) + (cy - y) * (cy - y), r, c, k, cx, cy]);
    }
  }
  found.sort((p, q) => p[0] - q[0] || p[1] - q[1] || p[2] - q[2]);
  return found.filter(([, , , , cx, cy]) => segmentClear(world, x, y, cx, cy)).map((item) => item[3]);
}

/** The first node (x, y) joins, or -1. */
export function originCell(world, x, y) {
  const joined = joinCells(world, x, y);
  return joined.length ? joined[0] : -1;
}

/**
 * Breadth-first search over open moves from every node the points join (N14 starts). Returns
 * {nav, start, seeds, dist, parent, reached}: dist[k] is the move count to node k (-1 when unreachable),
 * parent[k] its predecessor and start the first joined node (-1 when none).
 */
export function floodFrom(world, points) {
  const nav = navGrid(world), total = nav.cols * nav.rows;
  const dist = new Int32Array(total).fill(-1), parent = new Int32Array(total).fill(-1);
  const queue = new Int32Array(total), seeds = [];
  let tail = 0;
  for (const [x, y] of points) {
    for (const k of joinCells(world, x, y)) {
      seeds.push(k);
      if (dist[k] < 0) {
        dist[k] = 0;
        queue[tail++] = k;
      }
    }
  }
  for (let head = 0; head < tail; head++) {
    const k = queue[head];
    for (const next of neighbours(nav, k)) {
      if (dist[next] >= 0 || !moveOpen(world, k, next)) continue;
      dist[next] = dist[k] + 1;
      parent[next] = k;
      queue[tail++] = next;
    }
  }
  return {nav, start: seeds.length ? seeds[0] : -1, seeds, dist, parent, reached: tail};
}

/** floodFrom one point. */
export function flood(world, x, y) {
  return floodFrom(world, [[x, y]]);
}

// The best reached node of a list: fewest moves, then lowest row, then lowest column (N14).
function bestReached(field, nodes) {
  let best = -1;
  for (const k of nodes) {
    if (field.dist[k] < 0) continue;
    if (best < 0 || field.dist[k] < field.dist[best] || (field.dist[k] === field.dist[best] && k < best)) best = k;
  }
  return best;
}

// Nodes of the window around a box (forge_nav _nodes_within), in row-major order.
function nodesWithin(nav, x0, y0, x1, y1) {
  const c0 = Math.max(0, Math.floor(x0 / nav.cell) - 1), c1 = Math.min(nav.cols, Math.floor(x1 / nav.cell) + 2);
  const r0 = Math.max(0, Math.floor(y0 / nav.cell) - 1), r1 = Math.min(nav.rows, Math.floor(y1 / nav.cell) + 2);
  const out = [];
  for (let r = r0; r < r1; r++) for (let c = c0; c < c1; c++) out.push(r * nav.cols + c);
  return out;
}

/** N14 point target: the actor must stand at (x, y), joined to a reached node. {node, reason}. */
export function pointTarget(world, field, x, y) {
  if (!isValid(world, x, y)) return {node: -1, reason: STAND_REASON};
  const nodes = joinCells(world, x, y);
  const node = bestReached(field, nodes);
  return {node, reason: node >= 0 ? null : nodes.length ? "no reachable node within two cells in a clear straight line"
    : "valid but off the grid (no valid node within two cells in a clear straight line)"};
}

/** N14 reach target: a reached node centre with dx * dx + dy * dy <= reach * reach. {node, reason}. */
export function reachTarget(world, field, x, y, reach) {
  const nav = field.nav;
  const close = nodesWithin(nav, x - reach, y - reach, x + reach, y + reach).filter((k) => {
    const [cx, cy] = cellCenter(nav, k), dx = cx - x, dy = cy - y;
    return dx * dx + dy * dy <= reach * reach;
  });
  const node = bestReached(field, close);
  return {node, reason: node >= 0 ? null : `no reachable node within reach ${reach} px`};
}

/** Distance from (x, y) to a portal trigger: 0 inside the closed rect or circle (N14). */
export function portalDistance(portal, x, y) {
  if (portal.kind === "rect") {
    const dx = Math.max(portal.x - x, 0, x - portal.x1), dy = Math.max(portal.y - y, 0, y - portal.y1);
    return Math.sqrt(dx * dx + dy * dy);
  }
  const dx = x - portal.cx, dy = y - portal.cy;
  return Math.max(Math.sqrt(dx * dx + dy * dy) - portal.r, 0);
}

/** The trigger point closest to (x, y) (forge_nav Trigger.closest_point). */
export function closestTriggerPoint(portal, x, y) {
  if (portal.kind === "rect") return [Math.min(Math.max(x, portal.x), portal.x1), Math.min(Math.max(y, portal.y), portal.y1)];
  const d = Math.hypot(x - portal.cx, y - portal.cy);
  if (d <= portal.r) return [x, y];
  return [portal.cx + (x - portal.cx) * portal.r / d, portal.cy + (y - portal.cy) * portal.r / d];
}

/**
 * N14 exit: intent needs a reached node within radius of the trigger; crossing a reached node inside it,
 * else (nearest first, ties in row-major order) a reached node within two cells whose straight move to the
 * trigger's closest point is clear. {node, entry, reason}: entry is that closest point, or null.
 */
export function exitTarget(world, field, portal) {
  const nav = field.nav;
  const box = portal.kind === "rect" ? [portal.x, portal.y, portal.x1, portal.y1]
    : [portal.cx - portal.r, portal.cy - portal.r, portal.cx + portal.r, portal.cy + portal.r];
  const margin = portal.activation === "intent" ? portal.radius : 2 * nav.cell;
  const window = nodesWithin(nav, box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)
    .map((k) => {
      const [cx, cy] = cellCenter(nav, k);
      return {k, cx, cy, gap: portalDistance(portal, cx, cy)};
    });
  if (portal.activation === "intent") {
    const node = bestReached(field, window.filter((item) => item.gap <= portal.radius).map((item) => item.k));
    return {node, entry: null, reason: node >= 0 ? null : `no reachable node within the activation radius ${portal.radius} px`};
  }
  const inside = bestReached(field, window.filter((item) => item.gap === 0).map((item) => item.k));
  if (inside >= 0) return {node: inside, entry: null, reason: null};
  const ordered = window.map((item, order) => ({...item, order})).sort((p, q) => p.gap - q.gap || p.order - q.order);
  for (const item of ordered) {
    if (field.dist[item.k] < 0 || !(item.gap <= margin)) continue;
    const [ex, ey] = closestTriggerPoint(portal, item.cx, item.cy);
    if (isValid(world, ex, ey) && segmentClear(world, item.cx, item.cy, ex, ey)) return {node: item.k, entry: [ex, ey], reason: null};
  }
  return {node: -1, entry: null, reason: "the actor's centre cannot enter the trigger from any reachable node"};
}

// --------------------------------------------------------------------------- paths

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

// The reached node nearest (x, y) within `rings` cells that passes `accept`, or -1.
function nearbyReached(field, x, y, rings, accept) {
  const nav = field.nav, [row, col] = clampedCell(nav, x, y);
  let best = -1, bestDistance = Infinity;
  for (let r = Math.max(0, row - rings); r < Math.min(nav.rows, row + rings + 1); r++) {
    for (let c = Math.max(0, col - rings); c < Math.min(nav.cols, col + rings + 1); c++) {
      const k = r * nav.cols + c, cx = (c + 0.5) * nav.cell, cy = (r + 0.5) * nav.cell;
      const distance = (cx - x) * (cx - x) + (cy - y) * (cy - y);
      if (field.dist[k] >= 0 && distance < bestDistance && accept(k, cx, cy)) {
        best = k;
        bestDistance = distance;
      }
    }
  }
  return best;
}

/**
 * Waypoints from the flood origin to (gx, gy): the exact point when the actor can stand there and a
 * reached node within two cells walks onto it, otherwise the nearest reachable node centre within
 * snapRings cells. Returns null when nothing is reachable.
 */
export function pathFromFlood(world, field, x, y, gx, gy, {snapRings = SNAP_RINGS} = {}) {
  if (field.start < 0) return null;
  let goal = -1, exact = false;
  if (isValid(world, gx, gy)) {
    goal = nearbyReached(field, gx, gy, JOIN_RINGS, (k, cx, cy) => segmentClear(world, cx, cy, gx, gy));
    exact = goal >= 0;
  }
  if (goal < 0) goal = nearbyReached(field, gx, gy, snapRings, () => true);
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

// One keyboard part: segmentClear from a valid position; from an invalid one (a teleport into a wall),
// any valid end, so the actor can walk out.
function partClear(world, x, y, nx, ny) {
  return isValid(world, x, y) ? segmentClear(world, x, y, nx, ny) : isValid(world, nx, ny);
}

/**
 * Move by (dx, dy) in parts no longer than cell / 2. A part that would break segmentClear slides along
 * x, then along y, else the walker stops there: a blocked walker stays put and never moves further than
 * asked.
 */
export function moveWithCollision(world, x, y, dx, dy) {
  const length = Math.sqrt(dx * dx + dy * dy);
  if (!(length > 0)) return {x, y};
  const parts = Math.max(1, Math.ceil(length / world.sampleSpacing));
  const sx = dx / parts, sy = dy / parts;
  for (let k = 0; k < parts; k++) {
    if (partClear(world, x, y, x + sx, y + sy)) {
      x += sx;
      y += sy;
    } else if (sx !== 0 && partClear(world, x, y, x + sx, y)) {
      x += sx;
    } else if (sy !== 0 && partClear(world, x, y, x, y + sy)) {
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

// --------------------------------------------------------------------------- exits (N15, runtime duties)

/** True inside the closed trigger (portalDistance 0). */
export function insideTrigger(portal, x, y) {
  return portalDistance(portal, x, y) === 0;
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

/**
 * Where an actor returns after `portal` fired: entranceByFrom[toMap], a spawn of this map or a point, as
 * {id, x, y, facing}; null when there is none.
 */
export function returnSpawn(world, portal) {
  const entry = portal.entranceByFrom[portal.toMap];
  if (entry === undefined) return null;
  if (Array.isArray(entry)) return {id: `${portal.id}<-${portal.toMap}`, x: entry[0], y: entry[1], facing: null};
  return world.spawnById.get(entry) || null;
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

/** Spawns, then arrivals given as points: the starts of the route check (map_nav check). */
export function routeStarts(world) {
  return [...world.spawns, ...world.arrivals];
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

// The N14 answer for one target on one flood: {node, entry, exact, reason}.
function targetGoal(world, field, target) {
  if (target.kind === "exit") {
    const portal = target.portal;
    if (portal.activation === "intent") {
      // The verdict is N14's; the walk goes to the reached node nearest the trigger (then fewest moves).
      const answer = exitTarget(world, field, portal);
      if (answer.node < 0) return answer;
      let best = -1, bestGap = Infinity;
      for (let k = 0; k < field.dist.length; k++) {
        if (field.dist[k] < 0) continue;
        const [x, y] = cellCenter(field.nav, k), gap = portalDistance(portal, x, y);
        if (gap <= portal.radius && (gap < bestGap || (gap === bestGap && field.dist[k] < field.dist[best]))) {
          best = k;
          bestGap = gap;
        }
      }
      return {node: best, entry: null, reason: null};
    }
    return exitTarget(world, field, portal);
  }
  if (target.kind === "interaction" && target.reach !== null) return reachTarget(world, field, target.x, target.y, target.reach);
  const answer = pointTarget(world, field, target.x, target.y);
  return {...answer, exact: true};
}

// A reachable node outside the portal's zone, fewest moves first (to leave an arrival latch).
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

function routeTo(world, starts, floods, target, budget) {
  const result = {target: target.id, kind: target.kind, from: null, ok: false, reachable: false, reason: null,
    ticks: 0, distance: 0, maxStep: 0, zeroMotionTicks: 0, blocked: false, firedEnRoute: [], end: null};
  let start = null, field = null, goal = null, why = null;
  for (const candidate of starts) {
    const answer = targetGoal(world, floods.get(candidate.id), target);
    if (answer.node >= 0) {
      start = candidate;
      field = floods.get(candidate.id);
      goal = answer;
      break;
    }
    why = why || answer.reason;
  }
  if (start === null) {
    result.reason = starts.length === 0 ? "no spawns" : `unreachable from every start${why ? ` (${why})` : ""}`;
    return result;
  }
  result.from = start.id;
  result.reachable = true;
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
  const actor = createActor(world, start.x, start.y, start.facing);
  const reasons = [];
  let walking = true;
  if (target.kind === "exit" && actor.latched.has(target.id)) {
    // The start lies in this exit's zone and latched it: leave the zone first, as a player must.
    const away = departureCell(field, target.portal);
    if (away < 0) {
      reasons.push("the start latches this exit and no reachable node lies outside its zone");
      walking = false;
    } else {
      actor.path = smoothPath(world, actor.x, actor.y, chain(field, away));
      walking = walk(world, actor, budget, onFire, stats);
      field = walking ? flood(world, actor.x, actor.y) : field;
      goal = walking ? targetGoal(world, field, target) : null;
      if (walking && goal.node < 0) {
        reasons.push("unreachable after leaving the arrival zone");
        walking = false;
      }
    }
  }
  const node = goal !== null && goal.node >= 0 ? goal.node : -1;
  const [gx, gy] = node >= 0 ? cellCenter(field.nav, node) : [actor.x, actor.y];
  const finalPoint = goal !== null && goal.exact ? [target.x, target.y] : goal !== null && goal.entry ? goal.entry : null;
  if (walking && finalPoint !== null && !segmentClear(world, gx, gy, finalPoint[0], finalPoint[1])) {
    reasons.push("the last step from the grid onto the point is blocked (one_way)");
    walking = false;
  }
  if (walking) {
    const points = chain(field, node);
    if (finalPoint !== null) points.push({x: finalPoint[0], y: finalPoint[1]});
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
    if (portal.activation === "intent" && node >= 0) {
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
  const exact = goal !== null && goal.exact;
  const ex = actor.x - (exact || target.kind === "interaction" ? target.x : gx);
  const ey = actor.y - (exact || target.kind === "interaction" ? target.y : gy);
  const arrived = target.kind === "exit" ? fired
    : target.kind === "interaction" && target.reach !== null ? Math.sqrt(ex * ex + ey * ey) <= target.reach + EPS
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

/**
 * The arrivals of each portal (entranceByFrom: a spawn id or a point): found, valid, joined to the grid
 * and outside every trigger (bounceBack lists the closed triggers that hold it, as map_nav checks).
 */
export function portalArrivals(world) {
  return world.portals.map((portal) => ({
    id: portal.id, activation: portal.activation, to: portal.to,
    arrivals: Object.entries(portal.entranceByFrom).map(([from, entry]) => {
      const point = Array.isArray(entry) ? {x: entry[0], y: entry[1]} : world.spawnById.get(entry);
      const label = Array.isArray(entry) ? `${portal.id}<-${from}` : entry;
      if (!point) return {from, spawn: label, found: false, insideTrigger: null, inZone: null, latchedOnArrival: null};
      return {from, spawn: label, found: true, ...(Array.isArray(entry) ? {point: [entry[0], entry[1]]} : {}),
        insideTrigger: insideTrigger(portal, point.x, point.y), inZone: inPortalZone(portal, point.x, point.y),
        latchedOnArrival: portal.latch && inPortalZone(portal, point.x, point.y),
        valid: isValid(world, point.x, point.y), joined: joinCells(world, point.x, point.y).length > 0,
        bounceBack: world.portals.filter((other) => insideTrigger(other, point.x, point.y)).map((other) => other.id)};
    }),
  }));
}

/**
 * Walk from the starts (spawns, then arrival points) to every exit, interaction, anchor approach point
 * and slot with the same stepActor() a game tick uses: each target is walked from the first start that
 * reaches it (N14 decides reachability). Exits must fire with their travel direction and must not fire
 * when walking inward; the walk may never stall, idle or exceed the per-tick budget. speed is in world px
 * per second.
 */
export function traverseRoutes(world, {speed} = {}) {
  const pxPerSecond = finite(speed, "speed");
  if (!(pxPerSecond > 0)) throw new RangeError("speed must be positive");
  const budget = pxPerSecond / TICK_HZ;
  const starts = routeStarts(world);
  const floods = new Map(starts.map((start) => [start.id, flood(world, start.x, start.y)]));
  const spawns = starts.map((start) => ({id: start.id, ...(start.arrival ? {kind: "arrival"} : {}),
    valid: isValid(world, start.x, start.y), reachableCells: floods.get(start.id).reached}));
  const results = targetsOf(world).map((target) => routeTo(world, starts, floods, target, budget));
  const portals = portalArrivals(world);
  const arrivalsOk = portals.every((portal) => portal.arrivals.every((item) => item.found && item.valid && item.joined
    && item.bounceBack.length === 0));
  return {
    ok: spawns.length > 0 && spawns.every((spawn) => spawn.valid && spawn.reachableCells > 0)
      && results.every((item) => item.ok) && arrivalsOk,
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
      cell: world.cell, tickHz: TICK_HZ, solids: world.solids.length, portals: world.portals.length,
      oneWay: world.hasOneWay},
    actor: actor === null ? null : {
      x: round3(actor.x), y: round3(actor.y), valid: isValid(world, actor.x, actor.y),
      facing: [round3(actor.facingX), round3(actor.facingY)], latched: [...actor.latched].sort(),
      travelled: round3(actor.travelled), pathLength: actor.path === null ? 0 : actor.path.length,
    },
    ...extra,
  };
}
