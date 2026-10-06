// fx.v1 canvas runtime (codeart2d): code-drawn game effects, deterministic, Canvas 2D only.
// Contract: references/fx-runtime-contract.md. fx_build.py --export-runtime copies this file and
// replaces the data block with a normalised codeart2d.fx.v1 spec, so the geometry here is the same
// geometry the baked frames show. The demo data keeps the template itself runnable and verifiable.
//
// Use (t is ms since the effect spawned; spawn it at hitAt - impactMs so the hit lands on time):
//   import { render, spawnAt } from './fx-runtime.mjs';
//   const start = spawnAt('slash', hitAtMs);
//   render('slash', ctx, nowMs - start, { x: hitX, y: hitY, scale: 3, facing: 1 });
// No randomness, no clocks, no I/O: the same (id, t, env) always draws the same thing, and nothing is
// drawn before t = 0, after durationMs (one-shots) or outside the effect's canvas box.

const DATA = /*FX-DATA*/{"schema":"codeart2d.fx.v1","canvas":[64,64],"origin":[32.0,34.0],"palette":{"white":"#ffffff","gold":"#ffcd75","ember":"#ef7d57","red":"#b13e53","ice":"#a6f0ff","cyan":"#41a6f6","blue":"#3b5dc9","dust-hi":"#d8c8aa","dust-mid":"#a8957a","dust-lo":"#6f6050","ink":"#1a1c2c"},"effects":[{"id":"slash","durationMs":400,"impactMs":150,"loop":false,"seed":7,"frameMs":50,"outline":null,"primitives":[{"type":"slash","center":[30.0,32.0],"radius":18.0,"from":-150.0,"to":30.0,"width":8.0,"squash":1.0,"trail":0.9,"startMs":0.0,"endMs":150.0,"fadeMs":250.0,"colors":["#ffffff","#ffcd75","#ef7d57","#b13e53"]},{"type":"flash","origin":[45.5,41.0],"atMs":150.0,"lifeMs":100.0,"radius":3.0,"rays":4,"rayLength":8.0,"rotation":45.0,"colors":["#ffffff","#ffcd75","#ef7d57"]},{"type":"sparks","origin":[45.5,41.0],"atMs":150.0,"count":8,"angle":20.0,"spread":130.0,"speed":55.0,"lifeMs":220.0,"length":4.0,"width":1.5,"gravity":60.0,"colors":["#ffffff","#ffcd75","#ef7d57"]}],"events":[]}]}/*END-FX-DATA*/;

export const FX_SCHEMA = 'codeart2d.fx.v1';
export const canvas = Object.freeze([DATA.canvas[0], DATA.canvas[1]]);
export const origin = Object.freeze([DATA.origin[0], DATA.origin[1]]);
export const effects = Object.freeze(DATA.effects.map((effect) => Object.freeze({
  id: effect.id, durationMs: effect.durationMs, impactMs: effect.impactMs, loop: effect.loop,
})));
const BY_ID = new Map(DATA.effects.map((effect) => [effect.id, effect]));

const DEG = Math.PI / 180;
const TAU = 2 * Math.PI;

// 32-bit integer mix, bit-identical to fx_build.hash01: a value in [0, 1) for (seed, index).
function hash01(seed, index) {
  let h = (seed | 0) ^ Math.imul((index + 1) | 0, 0x9E3779B1);
  h = Math.imul(h ^ (h >>> 16), 0x85EBCA6B);
  h = Math.imul(h ^ (h >>> 13), 0xC2B2AE35);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

function stream(seed, primitive, variable) {
  return (seed ^ Math.imul(primitive + 1, 0x27D4EB2D) ^ Math.imul(variable, 0x165667B1)) >>> 0;
}

const clamp01 = (u) => (u <= 0 ? 0 : u >= 1 ? 1 : u);

function easeOut(u) {
  const r = 1 - clamp01(u);
  return 1 - r * r * r;
}

function easeIn(u) {
  const c = clamp01(u);
  return c * c * c;
}

const rampColour = (colors, q) => colors[Math.min(colors.length - 1, Math.floor(q * colors.length))];

// Triangle wave, period 1 (0, 1, 0, -1 at quarters): constant speed keeps loop steps even.
const triangle = (x) => 4 * Math.abs((((x - 0.25) % 1) + 1) % 1 - 0.5) - 1;

const PRIMITIVES = {
  slash(p, t, seed, index, out) {
    if (t < p.startMs) return;
    const head = easeOut((t - p.startMs) / Math.max(1, p.endMs - p.startMs));
    const colors = p.colors;
    const layers = Math.min(3, colors.length);
    let tail;
    let thin;
    let shift;
    if (t <= p.endMs) {
      tail = Math.max(0, head - p.trail);
      thin = 1;
      shift = 0;
    } else {
      const e = easeIn((t - p.endMs) / Math.max(1, p.fadeMs));
      const base = Math.max(0, 1 - p.trail);
      tail = base + (1 - base) * e;
      thin = 1 - 0.7 * e;
      shift = Math.min(colors.length - layers, Math.floor(e * (colors.length - layers + 1)));
    }
    if (head - tail <= 0.002) return;
    const start = p.from * DEG;
    const end = p.to * DEG;
    const tailAngle = start + (end - start) * tail;
    const headAngle = start + (end - start) * head;
    const segments = Math.max(6, Math.ceil(Math.abs(headAngle - tailAngle) * p.radius / 1.5));
    const [cx, cy] = p.center;
    for (let layer = 0; layer < layers; layer++) {
      const factor = 1 - layer / layers;
      const outer = [];
      const inner = [];
      for (let i = 0; i <= segments; i++) {
        const u = i / segments;
        const angle = tailAngle + (headAngle - tailAngle) * u;
        const half = p.width * factor * thin * Math.sin(Math.PI * u * u) / 2;
        const c = Math.cos(angle);
        const s = Math.sin(angle);
        outer.push([cx + (p.radius + half) * c, cy + (p.radius + half) * s * p.squash]);
        inner.push([cx + (p.radius - half) * c, cy + (p.radius - half) * s * p.squash]);
      }
      out.push({ kind: 'poly', points: outer.concat(inner.reverse()), color: colors[shift + layers - 1 - layer] });
    }
  },

  sparks(p, t, seed, index, out) {
    const tau = t - p.atMs;
    if (tau < 0) return;
    const first = stream(seed, index, 0);
    const second = stream(seed, index, 1);
    const third = stream(seed, index, 2);
    const [ox, oy] = p.origin;
    for (let i = 0; i < p.count; i++) {
      const life = p.lifeMs * (0.6 + 0.4 * hash01(third, i));
      if (tau >= life) continue;
      const q = tau / life;
      const angle = (p.angle + (hash01(first, i) - 0.5) * p.spread) * DEG;
      const speed = p.speed * (0.5 + 0.5 * hash01(second, i));
      const distance = speed * life / 1000 * easeOut(q);
      const seconds = tau / 1000;
      const c = Math.cos(angle);
      const s = Math.sin(angle);
      const hx = ox + c * distance;
      const hy = oy + s * distance + 0.5 * p.gravity * seconds * seconds;
      const length = p.length * (1 - q);
      out.push({ kind: 'capsule', x1: hx - c * length, y1: hy - s * length, x2: hx, y2: hy, w: p.width,
        color: rampColour(p.colors, q) });
    }
  },

  ring(p, t, seed, index, out) {
    const tau = t - p.atMs;
    if (tau < 0 || tau >= p.lifeMs) return;
    const q = tau / p.lifeMs;
    const [r0, r1] = p.radius;
    const [w0, w1] = p.width;
    const radius = r0 + (r1 - r0) * easeOut(q);
    const width = w0 + (w1 - w0) * q;
    const outer = radius + width / 2;
    const inner = Math.max(0, radius - width / 2);
    out.push({ kind: 'ring', cx: p.origin[0], cy: p.origin[1], rx: outer, ry: outer * p.squash, irx: inner,
      iry: inner * p.squash, color: rampColour(p.colors, q) });
  },

  flash(p, t, seed, index, out) {
    const tau = t - p.atMs;
    if (tau < 0 || tau >= p.lifeMs) return;
    const q = tau / p.lifeMs;
    const colors = p.colors;
    const count = colors.length;
    const coreRadius = p.radius * (1 - q);
    const ray = p.rayLength * (1 - 0.5 * q);
    const inner = Math.max(coreRadius * 0.5, 0.75);
    const [ox, oy] = p.origin;
    const points = [];
    for (let k = 0; k < 2 * p.rays; k++) {
      const angle = (p.rotation + k * 180 / p.rays) * DEG;
      const reach = k % 2 === 0 ? ray : inner;
      points.push([ox + reach * Math.cos(angle), oy + reach * Math.sin(angle)]);
    }
    const star = count > 1 ? colors[Math.min(count - 1, 1 + Math.floor(q * (count - 1)))] : colors[0];
    out.push({ kind: 'poly', points, color: star });
    if (coreRadius >= 0.5) out.push({ kind: 'disc', cx: ox, cy: oy, r: coreRadius, color: rampColour(colors, q) });
  },

  dust(p, t, seed, index, out) {
    const first = stream(seed, index, 0);
    const second = stream(seed, index, 1);
    const third = stream(seed, index, 2);
    const [ox, oy] = p.origin;
    const [r0, r1] = p.radius;
    for (let i = 0; i < p.count; i++) {
      const a = hash01(first, i);
      const b = hash01(second, i);
      const c = hash01(third, i);
      const delay = 0.25 * p.lifeMs * i / p.count;
      const life = p.lifeMs * (0.7 + 0.3 * c);
      const tau = t - p.atMs - delay;
      if (tau < 0 || tau >= life) continue;
      const q = tau / life;
      const e = easeOut(q);
      const side = a < 0.5 ? -1 : 1;
      const x = ox + (a - 0.5) * p.spread + side * p.drift * e;
      const y = oy - p.rise * e * (0.6 + 0.4 * b);
      out.push({ kind: 'disc', cx: x, cy: y, r: r0 + (r1 - r0) * e, color: rampColour(p.colors, q) });
    }
  },

  projectile(p, t, seed, index, out) {
    const phase = (t % p.periodMs) / p.periodMs;
    const colors = p.colors;
    const glow = colors[Math.min(1, colors.length - 1)];
    const angle = p.angle * DEG;
    const c = Math.cos(angle);
    const s = Math.sin(angle);
    const [ox, oy] = p.origin;
    const radius = p.radius * (1 + 0.15 * triangle(phase));
    const trail = p.trail * (1 + 0.1 * triangle(2 * phase));
    const left = [];
    const right = [];
    for (let i = 0; i < 9; i++) {
      const u = i / 8;
      const half = radius * (1 - u) * 0.9;
      const x = ox - c * trail * u;
      const y = oy - s * trail * u;
      left.push([x - s * half, y + c * half]);
      right.push([x + s * half, y - c * half]);
    }
    out.push({ kind: 'poly', points: left.concat(right.reverse()), color: colors[colors.length - 1] });
    out.push({ kind: 'disc', cx: ox, cy: oy, r: radius * 1.35, color: glow });
    out.push({ kind: 'disc', cx: ox, cy: oy, r: radius, color: colors[0] });
    for (let k = 0; k < p.orbiters; k++) {
      const orbit = TAU * (k / p.orbiters + phase);
      out.push({ kind: 'disc', cx: ox + Math.cos(orbit) * radius * 1.9, cy: oy + Math.sin(orbit) * radius * 1.9,
        r: 0.9, color: glow });
    }
  },
};

function effectOf(id) {
  const effect = BY_ID.get(id);
  if (!effect) throw new RangeError(`unknown fx id ${String(id)}`);
  return effect;
}

// Effect-local time, or null when nothing may be drawn (before spawn, after a one-shot ends).
function localTime(effect, t) {
  if (!(t >= 0) || t === Infinity) return null;
  if (effect.loop) return t % effect.durationMs;
  return t < effect.durationMs ? t : null;
}

function seedOf(effect, env) {
  const extra = env && Number.isInteger(env.seed) ? env.seed : 0;
  return (effect.seed ^ extra) >>> 0;
}

function buildShapes(effect, t, seed) {
  const out = [];
  effect.primitives.forEach((primitive, index) => PRIMITIVES[primitive.type](primitive, t, seed, index, out));
  return out;
}

/** Time to spawn an effect so its impact lands at hitAtMs: spawnAt = hitAt - impactMs. */
export function spawnAt(id, hitAtMs) {
  return hitAtMs - effectOf(id).impactMs;
}

/** The shapes drawn at time t, in effect canvas coordinates (for tools and tests). */
export function shapes(id, t, env) {
  const effect = effectOf(id);
  const local = localTime(effect, t);
  return local === null ? [] : buildShapes(effect, local, seedOf(effect, env));
}

function finiteOr(value, fallback) {
  return Number.isFinite(value) ? value : fallback;
}

function drawShape(ctx, shape, colour, grow) {
  ctx.fillStyle = colour;
  if (shape.kind === 'poly') {
    ctx.beginPath();
    shape.points.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.fill();
    if (grow) {
      ctx.strokeStyle = colour;
      ctx.lineWidth = 2 * grow;
      ctx.lineJoin = 'round';
      ctx.stroke();
    }
  } else if (shape.kind === 'disc') {
    ctx.beginPath();
    ctx.arc(shape.cx, shape.cy, shape.r + grow, 0, TAU);
    ctx.fill();
  } else if (shape.kind === 'ring') {
    ctx.beginPath();
    ctx.ellipse(shape.cx, shape.cy, shape.rx + grow, shape.ry + grow, 0, 0, TAU);
    const irx = shape.irx - grow;
    const iry = shape.iry - grow;
    if (irx > 0 && iry > 0) {
      ctx.moveTo(shape.cx + irx, shape.cy);
      ctx.ellipse(shape.cx, shape.cy, irx, iry, 0, 0, TAU, true);
    }
    ctx.fill('evenodd');
  } else {
    const width = shape.w + 2 * grow;
    if (Math.hypot(shape.x2 - shape.x1, shape.y2 - shape.y1) < 1e-6) {
      ctx.beginPath();
      ctx.arc(shape.x2, shape.y2, width / 2, 0, TAU);
      ctx.fill();
    } else {
      ctx.strokeStyle = colour;
      ctx.lineWidth = width;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(shape.x1, shape.y1);
      ctx.lineTo(shape.x2, shape.y2);
      ctx.stroke();
    }
  }
}

/**
 * Draw effect `id` at time t (ms since spawn). env: {x, y} where the effect origin goes,
 * scale (default 1), facing (1 or -1 mirrors), seed (integer, varies particles per instance).
 * Draws nothing and returns false outside the effect's lifetime; leaves ctx state unchanged.
 */
export function render(id, ctx, t, env = {}) {
  const effect = effectOf(id);
  const local = localTime(effect, t);
  if (local === null) return false;
  const list = buildShapes(effect, local, seedOf(effect, env));
  if (!list.length) return false;
  const scale = finiteOr(env.scale, 1);
  ctx.save();
  try {
    ctx.translate(finiteOr(env.x, 0), finiteOr(env.y, 0));
    ctx.scale(env.facing === -1 ? -scale : scale, scale);
    ctx.translate(-origin[0], -origin[1]);
    if (effect.outline) list.forEach((shape) => drawShape(ctx, shape, effect.outline, 1));
    list.forEach((shape) => drawShape(ctx, shape, shape.color, 0));
  } finally {
    ctx.restore();
  }
  return true;
}
