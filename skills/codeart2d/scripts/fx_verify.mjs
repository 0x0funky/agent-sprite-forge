#!/usr/bin/env node
// fx_verify.mjs: check an fx.v1 effect runtime module (codeart2d). Node 18+, no npm packages.
//
// Usage, from the project root:
//   node "<skill-dir>/scripts/fx_verify.mjs" out/fx-slash-v1/fx-runtime.mjs --report out/fx-slash-v1/fx-verify.json
//
// The module is imported with Math.random, Date.now, performance.now, timers and fetch trapped, then
// every effect is drawn at 60 Hz ticks, at its impact and around its lifetime into a recording 2D
// context that rejects non-finite arguments and tracks save/restore, transforms and every paint.
// Checks (references/fx-runtime-contract.md): forbidden_apis, exports, timing, spawn_at,
// no_randomness_or_clocks, no_global_writes, render_errors, finite_arguments, balanced_state,
// context_contract, transparent_outside, within_box, deterministic, visible_at_impact,
// fullscreen_flash; warnings: thin_strokes, reach, op_budget. Exit 0 when nothing fails (warnings allowed), 1 when a
// check fails or the module cannot be checked, 2 on a usage error (argparse style: the usage line, then
// "fx_verify.mjs: error: ..."). One ASCII JSON line on stdout; non-ASCII characters are \uXXXX escapes.
// The report (--report) is a common qaEnvelope.

import { createHash } from 'node:crypto';
import { readFileSync, realpathSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const TOOL = { name: 'codeart2d/fx_verify.mjs', version: '0.4.0' };  // the package version (D29)
export const FX_SCHEMA = 'codeart2d.fx.v1';
const TICK_MS = 1000 / 60;
const DEFAULTS = { width: 1280, height: 720, scale: 3, maxFullscreenTicks: 10, minStroke: 1, maxReach: 300,
  opBudget: 2000 };

// ----------------------------------------------------------------------------- static scan

const FORBIDDEN = [
  ['Math.random', /\bMath\s*\.\s*random\b/],
  ['Date', /\bDate\s*\.\s*now\b|\bnew\s+Date\b/],
  ['performance.now', /\bperformance\s*\.\s*now\b/],
  ['timers', /\b(?:setTimeout|setInterval|setImmediate|requestAnimationFrame|queueMicrotask)\b/],
  ['network', /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\b/],
  ['dynamic code', /\beval\s*\(|\bnew\s+Function\b|\bFunction\s*\(/],
  ['module loading', /\bimport\s*\(|\brequire\s*\(/],
  ['browser globals', /\b(?:document|window|navigator|localStorage|sessionStorage|indexedDB|location)\s*\./],
  ['process', /\bprocess\s*\./],
  ['global object', /\bglobalThis\b/],
  ['crypto randomness', /\bgetRandomValues\b|\brandomUUID\b/],
  ['raster I/O', /\b(?:drawImage|getImageData|putImageData|createImageData|createPattern|toDataURL)\b|\bnew\s+Image\b/],
  ['text drawing', /\b(?:fillText|strokeText)\b/],
];

/** Source with comments blanked (newlines kept), so findings point at real lines. */
export function stripComments(source) {
  let out = '';
  let i = 0;
  let quote = null;
  while (i < source.length) {
    const ch = source[i];
    const next = source[i + 1];
    if (quote) {
      out += ch;
      if (ch === '\\') { out += next ?? ''; i += 2; continue; }
      if (ch === quote) quote = null;
      i += 1;
    } else if (ch === '"' || ch === "'" || ch === '`') {
      quote = ch;
      out += ch;
      i += 1;
    } else if (ch === '/' && next === '/') {
      while (i < source.length && source[i] !== '\n') { out += ' '; i += 1; }
    } else if (ch === '/' && next === '*') {
      const end = source.indexOf('*/', i + 2);
      const stop = end < 0 ? source.length : end + 2;
      for (; i < stop; i += 1) out += source[i] === '\n' ? '\n' : ' ';
    } else {
      out += ch;
      i += 1;
    }
  }
  return out;
}

/** Forbidden constructs in a module's code (comments ignored): [{api, line, text}]. */
export function scanSource(source) {
  const code = stripComments(source);
  const lines = code.split('\n');
  const findings = [];
  lines.forEach((line, index) => {
    for (const [api, pattern] of FORBIDDEN) {
      if (pattern.test(line)) findings.push({ api, line: index + 1, text: source.split('\n')[index].trim().slice(0, 120) });
    }
  });
  return findings;
}

// ----------------------------------------------------------------------------- runtime traps

function installTraps(hits) {
  const saved = [];
  const trap = (owner, key, label) => {
    if (!owner) return;
    saved.push([owner, key, Object.getOwnPropertyDescriptor(owner, key)]);
    Object.defineProperty(owner, key, {
      configurable: true, writable: true,
      value: () => { hits.add(label); throw new Error(`${label} is forbidden in fx.v1 runtimes`); },
    });
  };
  trap(Math, 'random', 'Math.random');
  trap(Date, 'now', 'Date.now');
  trap(globalThis.performance, 'now', 'performance.now');
  trap(globalThis.crypto, 'getRandomValues', 'crypto.getRandomValues');
  for (const name of ['setTimeout', 'setInterval', 'setImmediate', 'fetch', 'requestAnimationFrame']) {
    trap(globalThis, name, name);
  }
  return () => {
    for (const [owner, key, descriptor] of saved.reverse()) {
      if (descriptor) Object.defineProperty(owner, key, descriptor);
      else delete owner[key];
    }
  };
}

// ----------------------------------------------------------------------------- recording context

const STATE_DEFAULTS = {
  globalAlpha: 1, globalCompositeOperation: 'source-over', fillStyle: '#000000', strokeStyle: '#000000',
  lineWidth: 1, lineCap: 'butt', lineJoin: 'miter', miterLimit: 10, lineDashOffset: 0, shadowBlur: 0,
  shadowColor: 'rgba(0, 0, 0, 0)', shadowOffsetX: 0, shadowOffsetY: 0, filter: 'none',
  imageSmoothingEnabled: true, font: '10px sans-serif', textAlign: 'start', textBaseline: 'alphabetic',
};
const FORBIDDEN_METHODS = new Set(['drawImage', 'getImageData', 'putImageData', 'createImageData', 'createPattern',
  'fillText', 'strokeText', 'clearRect']);
const PUBLIC_METHODS = new Set(['save', 'restore', 'translate', 'scale', 'rotate', 'transform', 'setTransform',
  'resetTransform', 'getTransform', 'beginPath', 'closePath', 'moveTo', 'lineTo', 'bezierCurveTo', 'quadraticCurveTo',
  'arc', 'arcTo', 'ellipse', 'rect', 'roundRect', 'fill', 'stroke', 'clip', 'fillRect', 'strokeRect', 'setLineDash',
  'getLineDash', 'isPointInPath', 'isPointInStroke', 'measureText', 'createLinearGradient', 'createRadialGradient',
  'createConicGradient']);

const NAMED_ALPHA = { transparent: 0 };

function styleAlpha(style) {
  if (typeof style !== 'string') return 1;
  const text = style.trim().toLowerCase();
  if (text in NAMED_ALPHA) return NAMED_ALPHA[text];
  const hex = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/.exec(text);
  if (hex) {
    const digits = hex[1];
    if (digits.length === 4) return parseInt(digits[3] + digits[3], 16) / 255;
    if (digits.length === 8) return parseInt(digits.slice(6), 16) / 255;
    return 1;
  }
  const rgba = /^rgba?\(([^)]*)\)$/.exec(text);
  if (rgba) {
    const parts = rgba[1].split(/[\s,/]+/).filter(Boolean);
    return parts.length === 4 ? Math.max(0, Math.min(1, parseFloat(parts[3]))) : 1;
  }
  return 1;
}

const emptyBox = () => [Infinity, Infinity, -Infinity, -Infinity];

function grow(box, x, y) {
  box[0] = Math.min(box[0], x); box[1] = Math.min(box[1], y);
  box[2] = Math.max(box[2], x); box[3] = Math.max(box[3], y);
}

/**
 * A Canvas 2D stand-in that records instead of drawing. calls is the full call log (determinism),
 * ops the paints with their device-space boxes, problems any contract violation seen.
 */
export class RecordingContext {
  constructor(width, height) {
    this.canvas = { width, height };
    this.state = { ...STATE_DEFAULTS, transform: [1, 0, 0, 1, 0, 0] };
    this.stack = [];
    this.path = emptyBox();
    this.calls = [];
    this.ops = [];
    this.problems = [];
    this.nonFinite = 0;
    this.gradients = 0;
  }

  apply(x, y) {
    const [a, b, c, d, e, f] = this.state.transform;
    return [a * x + c * y + e, b * x + d * y + f];
  }

  scales() {
    const [a, b, c, d] = this.state.transform;
    return [Math.hypot(a, c), Math.hypot(b, d), Math.sqrt(Math.abs(a * d - b * c))];
  }

  point(x, y) {
    const [px, py] = this.apply(x, y);
    grow(this.path, px, py);
  }

  round(x, y, r) {
    const [sx, sy] = this.scales();
    const [px, py] = this.apply(x, y);
    grow(this.path, px - r * sx, py - r * sy);
    grow(this.path, px + r * sx, py + r * sy);
  }

  paint(kind, box, extra = {}) {
    if (!(box[0] <= box[2])) return;
    const style = kind === 'stroke' ? this.state.strokeStyle : this.state.fillStyle;
    this.ops.push({ kind, box: box.slice(), alpha: this.state.globalAlpha * styleAlpha(style), ...extra });
  }

  save() { this.stack.push({ ...this.state, transform: this.state.transform.slice() }); }

  restore() {
    if (!this.stack.length) { this.problems.push('restore() without a matching save()'); return; }
    this.state = this.stack.pop();
  }

  translate(x, y) {
    const t = this.state.transform;
    t[4] += t[0] * x + t[2] * y;
    t[5] += t[1] * x + t[3] * y;
  }

  scale(x, y) {
    const t = this.state.transform;
    t[0] *= x; t[1] *= x; t[2] *= y; t[3] *= y;
  }

  rotate(angle) {
    const [a, b, c, d] = this.state.transform;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    this.state.transform.splice(0, 4, a * cos + c * sin, b * cos + d * sin, c * cos - a * sin, d * cos - b * sin);
  }

  transform(a2, b2, c2, d2, e2, f2) {
    const [a, b, c, d, e, f] = this.state.transform;
    this.state.transform = [a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
      a * e2 + c * f2 + e, b * e2 + d * f2 + f];
  }

  setTransform(a, b, c, d, e, f) {
    if (typeof a === 'object' && a !== null) ({ a, b, c, d, e, f } = a);
    this.state.transform = [a, b, c, d, e, f];
  }

  resetTransform() { this.state.transform = [1, 0, 0, 1, 0, 0]; }

  getTransform() {
    const [a, b, c, d, e, f] = this.state.transform;
    return { a, b, c, d, e, f };
  }

  beginPath() { this.path = emptyBox(); }

  closePath() {}

  moveTo(x, y) { this.point(x, y); }

  lineTo(x, y) { this.point(x, y); }

  bezierCurveTo(x1, y1, x2, y2, x, y) { this.point(x1, y1); this.point(x2, y2); this.point(x, y); }

  quadraticCurveTo(x1, y1, x, y) { this.point(x1, y1); this.point(x, y); }

  arc(x, y, r) {
    if (r < 0) this.problems.push('arc() with a negative radius');
    this.round(x, y, Math.abs(r));
  }

  arcTo(x1, y1, x2, y2, r) { this.point(x1, y1); this.point(x2, y2); this.round(x2, y2, Math.abs(r)); }

  ellipse(x, y, rx, ry) {
    if (rx < 0 || ry < 0) this.problems.push('ellipse() with a negative radius');
    this.round(x, y, Math.max(Math.abs(rx), Math.abs(ry)));
  }

  rect(x, y, w, h) { this.point(x, y); this.point(x + w, y); this.point(x, y + h); this.point(x + w, y + h); }

  roundRect(x, y, w, h) { this.rect(x, y, w, h); }

  fill() { this.paint('fill', this.path); }

  stroke() {
    const width = this.state.lineWidth * this.scales()[2];
    const box = this.path.slice();
    box[0] -= width / 2; box[1] -= width / 2; box[2] += width / 2; box[3] += width / 2;
    this.paint('stroke', box, { lineWidth: width });
  }

  clip() {}

  fillRect(x, y, w, h) {
    const saved = this.path;
    this.path = emptyBox();
    this.rect(x, y, w, h);
    this.paint('fill', this.path);
    this.path = saved;
  }

  strokeRect(x, y, w, h) {
    const saved = this.path;
    this.path = emptyBox();
    this.rect(x, y, w, h);
    this.stroke();
    this.path = saved;
  }

  setLineDash() {}

  getLineDash() { return []; }

  isPointInPath() { return false; }

  isPointInStroke() { return false; }

  measureText() { return { width: 0 }; }

  createLinearGradient() { return this.gradient(); }

  createRadialGradient() { return this.gradient(); }

  createConicGradient() { return this.gradient(); }

  gradient() {
    this.gradients += 1;
    return { id: `gradient${this.gradients}`, addColorStop: () => {} };
  }
}

function serialise(value) {
  if (typeof value === 'number' || typeof value === 'string' || typeof value === 'boolean') return value;
  if (Array.isArray(value)) return value.map(serialise);
  if (value && typeof value === 'object' && 'id' in value) return value.id;
  return String(value);
}

/** Proxy that logs every call and property write, refusing non-finite numbers and forbidden methods. */
function recordingProxy(ctx) {
  const checkNumbers = (label, values) => {
    for (const value of values.flat(2)) {
      if (typeof value === 'number' && !Number.isFinite(value)) {
        ctx.nonFinite += 1;  // ignored like a real canvas does; counted by finite_arguments
        return false;
      }
    }
    return true;
  };
  return new Proxy(ctx, {
    get(target, key) {
      if (key in STATE_DEFAULTS) return target.state[key];
      if (key === 'canvas') return target.canvas;
      if (typeof key !== 'string') return undefined;
      if (FORBIDDEN_METHODS.has(key)) {
        return () => { target.problems.push(`forbidden method ${key}()`); target.calls.push([key]); };
      }
      if (!PUBLIC_METHODS.has(key)) {
        target.problems.push(`unknown context member ${key}`);
        return undefined;
      }
      const value = target[key];
      return (...args) => {
        target.calls.push([key, ...args.map(serialise)]);
        if (!checkNumbers(`${key}()`, args)) return undefined;
        return value.apply(target, args);
      };
    },
    set(target, key, value) {
      target.calls.push(['=', key, serialise(value)]);
      if (!(key in STATE_DEFAULTS)) {
        target.problems.push(`write to unknown context property ${String(key)}`);
        return true;
      }
      if (typeof value === 'number' && !checkNumbers(String(key), [value])) return true;
      target.state[key] = value;
      return true;
    },
  });
}

// ----------------------------------------------------------------------------- verification

let instance = 0;

async function importFresh(url) {
  instance += 1;
  return import(`${url}?fx-verify-instance=${instance}`);
}

function check(id, passed, value, threshold, { warn = false } = {}) {
  const status = passed === null ? 'skipped' : passed ? 'pass' : warn ? 'warn' : 'fail';
  return { id, status, value, threshold };
}

function sampleTimes(effect) {
  const ticks = new Set();
  for (let tick = 0; tick * TICK_MS < effect.durationMs; tick += 1) ticks.add(tick * TICK_MS);
  const inside = new Set([...ticks, 0, effect.durationMs - 1, effect.durationMs - 0.001]);
  for (const t of [effect.impactMs - 1, effect.impactMs, effect.impactMs + 1]) {
    if (t >= 0 && t < effect.durationMs) inside.add(t);
  }
  const outside = [-1000, -1, -0.001, Number.NaN, Number.NEGATIVE_INFINITY];
  if (effect.loop) inside.add(effect.durationMs + effect.impactMs + 0.5);
  else outside.push(effect.durationMs, effect.durationMs + 0.001, effect.durationMs + 1, effect.durationMs + 1000);
  return { inside: [...inside].filter((t) => t >= 0).sort((a, b) => a - b), outside, ticks };
}

function draw(module, id, t, env, size) {
  const ctx = new RecordingContext(size.width, size.height);
  let error = null;
  try {
    module.render(id, recordingProxy(ctx), t, env);
  } catch (exc) {
    error = String(exc && exc.message ? exc.message : exc);
  }
  const balanced = ctx.stack.length === 0;
  const restored = Object.keys(STATE_DEFAULTS).every((key) => Object.is(ctx.state[key], STATE_DEFAULTS[key]))
    && ctx.state.transform.every((value, index) => value === [1, 0, 0, 1, 0, 0][index]);
  const digest = createHash('sha256').update(JSON.stringify(ctx.calls)).digest('hex');
  return { ctx, error, balanced, restored, digest };
}

function effectBox(module, env) {
  const [width, height] = module.canvas;
  const [ox, oy] = module.origin;
  const facing = env.facing === -1 ? -1 : 1;
  const xs = [0, width].map((u) => env.x + (u - ox) * env.scale * facing);
  const ys = [0, height].map((v) => env.y + (v - oy) * env.scale);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

/**
 * Verify an fx.v1 module file. Returns the report, a common qaEnvelope plus effects and findings.
 * options: width, height (viewport, default 1280x720), scale (env.scale, default 3), maxFullscreenTicks,
 * minStroke (device px), maxReach (device px), opBudget, reportDir (for the input path).
 */
export async function verifyFxModule(file, options = {}) {
  const settings = { ...DEFAULTS, ...options };
  const absolute = path.resolve(file);
  const source = readFileSync(absolute, 'utf8');
  const url = pathToFileURL(absolute).href;
  const checks = [];
  const findings = { forbidden: scanSource(source), traps: [], problems: [], outside: [], escapes: [],
    nondeterministic: [], errors: [], invisible: [] };
  checks.push(check('forbidden_apis', findings.forbidden.length === 0, findings.forbidden.map((f) => `${f.api}@${f.line}`),
    []));

  const hits = new Set();
  const restore = installTraps(hits);
  const globalsBefore = new Set(Object.getOwnPropertyNames(globalThis));
  let first;
  let second;
  let effects = [];
  try {
    try {
      first = await importFresh(url);
      second = await importFresh(url);
    } catch (exc) {
      throw new Error(`cannot import ${file}: ${exc && exc.message ? exc.message : exc}`);
    }
    const exportsOk = Array.isArray(first.effects) && typeof first.render === 'function'
      && typeof first.spawnAt === 'function' && first.FX_SCHEMA === FX_SCHEMA
      && Array.isArray(first.canvas) && first.canvas.length === 2 && Array.isArray(first.origin)
      && first.origin.length === 2 && [...first.canvas, ...first.origin].every(Number.isFinite);
    checks.push(check('exports', exportsOk, Object.keys(first).sort(),
      ['FX_SCHEMA', 'canvas', 'effects', 'origin', 'render', 'spawnAt']));
    if (!exportsOk) throw new Error('the module lacks the fx.v1 exports; see references/fx-runtime-contract.md');
    effects = first.effects;

    const ids = effects.map((effect) => effect.id);
    const timingProblems = [];
    if (new Set(ids).size !== ids.length) timingProblems.push('duplicate ids');
    for (const effect of effects) {
      if (typeof effect.id !== 'string' || !effect.id) timingProblems.push('an effect without an id');
      if (!Number.isInteger(effect.durationMs) || effect.durationMs < 1) timingProblems.push(`${effect.id}: durationMs`);
      const lastImpact = effect.loop ? effect.durationMs : effect.durationMs - 1;
      if (!Number.isInteger(effect.impactMs) || effect.impactMs < 0 || effect.impactMs > lastImpact) {
        timingProblems.push(`${effect.id}: impactMs`);
      }
    }
    checks.push(check('timing', timingProblems.length === 0, timingProblems, []));
    const spawnProblems = [];
    for (const effect of effects) {
      for (const hit of [0, 1000, 12345.5]) {
        if (first.spawnAt(effect.id, hit) !== hit - effect.impactMs) spawnProblems.push(`${effect.id}@${hit}`);
      }
    }
    checks.push(check('spawn_at', spawnProblems.length === 0, spawnProblems, 'spawnAt(id, hitAt) == hitAt - impactMs'));

    const size = { width: settings.width, height: settings.height };
    const baseEnv = { x: settings.width / 2, y: settings.height / 2, scale: settings.scale, facing: 1 };
    const envs = [baseEnv, { ...baseEnv, facing: -1 }, { ...baseEnv, scale: 1, seed: 7 }];
    let nonFinite = 0;
    let unbalanced = 0;
    let maxOps = 0;
    let thinnest = Infinity;
    let worstReach = 0;
    let fullscreenRun = 0;
    let fullscreenWorst = 0;
    for (const effect of effects) {
      const { inside, outside, ticks } = sampleTimes(effect);
      for (const env of envs) {
        fullscreenRun = 0;
        const box = effectBox(first, env);
        const tolerance = env.scale + 1;
        const digests = new Map();
        for (const t of inside) {
          const result = draw(first, effect.id, t, env, size);
          digests.set(t, result.digest);
          nonFinite += result.ctx.nonFinite;
          if (!result.balanced || !result.restored) unbalanced += 1;
          if (result.error) findings.errors.push(`${effect.id}@${t}: ${result.error}`);
          findings.problems.push(...result.ctx.problems.map((problem) => `${effect.id}@${t}: ${problem}`));
          maxOps = Math.max(maxOps, result.ctx.ops.length);
          for (const op of result.ctx.ops) {
            if (op.box[0] < box[0] - tolerance || op.box[1] < box[1] - tolerance || op.box[2] > box[2] + tolerance
                || op.box[3] > box[3] + tolerance) {
              findings.escapes.push(`${effect.id}@${Math.round(t)}`);
              break;
            }
          }
          for (const op of result.ctx.ops) if (op.kind === 'stroke') thinnest = Math.min(thinnest, op.lineWidth);
          if (env === baseEnv) {
            if (ticks.has(t)) {
              const viewport = settings.width * settings.height;
              const flash = result.ctx.ops.some((op) => op.kind === 'fill' && op.alpha >= 0.8
                && (Math.min(op.box[2], settings.width) - Math.max(op.box[0], 0))
                  * (Math.min(op.box[3], settings.height) - Math.max(op.box[1], 0)) >= 0.5 * viewport);
              fullscreenRun = flash ? fullscreenRun + 1 : 0;
              fullscreenWorst = Math.max(fullscreenWorst, fullscreenRun);
            }
            if (t === effect.impactMs) {
              if (!result.ctx.ops.length) findings.invisible.push(effect.id);
              for (const op of result.ctx.ops) {
                const cx = (op.box[0] + op.box[2]) / 2;
                const cy = (op.box[1] + op.box[3]) / 2;
                worstReach = Math.max(worstReach, Math.hypot(cx - env.x, cy - env.y));
              }
            }
          }
          const again = draw(first, effect.id, t, env, size);
          const fresh = draw(second, effect.id, t, env, size);
          if (again.digest !== result.digest || fresh.digest !== result.digest) {
            findings.nondeterministic.push(`${effect.id}@${t}`);
          }
        }
        for (const t of [...inside].reverse()) {
          if (draw(first, effect.id, t, env, size).digest !== digests.get(t)) {
            findings.nondeterministic.push(`${effect.id}@${t} (order)`);
          }
        }
        for (const t of outside) {
          const result = draw(first, effect.id, t, env, size);
          if (result.ctx.ops.length || result.error || !result.balanced) findings.outside.push(`${effect.id}@${t}`);
        }
      }
    }
    findings.traps = [...hits].sort();
    const newGlobals = Object.getOwnPropertyNames(globalThis).filter((name) => !globalsBefore.has(name));
    checks.push(check('no_randomness_or_clocks', hits.size === 0, findings.traps, []));
    checks.push(check('no_global_writes', newGlobals.length === 0, newGlobals, []));
    checks.push(check('render_errors', findings.errors.length === 0, findings.errors.slice(0, 8), []));
    checks.push(check('finite_arguments', nonFinite === 0, nonFinite, 0));
    checks.push(check('balanced_state', unbalanced === 0 && !findings.problems.some((p) => p.includes('restore')),
      unbalanced, 0));
    checks.push(check('context_contract', findings.problems.length === 0, [...new Set(findings.problems)].slice(0, 8),
      []));
    checks.push(check('transparent_outside', findings.outside.length === 0, findings.outside.slice(0, 8), []));
    checks.push(check('within_box', findings.escapes.length === 0, findings.escapes.slice(0, 8),
      'every paint inside the effect canvas box'));
    checks.push(check('deterministic', findings.nondeterministic.length === 0,
      [...new Set(findings.nondeterministic)].slice(0, 8), []));
    const oneShots = effects.filter((effect) => !effect.loop);
    checks.push(check('visible_at_impact', oneShots.length ? findings.invisible.length === 0 : null,
      findings.invisible, []));
    checks.push(check('fullscreen_flash', fullscreenWorst <= settings.maxFullscreenTicks, fullscreenWorst,
      settings.maxFullscreenTicks));
    checks.push(check('thin_strokes', thinnest === Infinity ? null : thinnest >= settings.minStroke,
      thinnest === Infinity ? null : Number(thinnest.toFixed(3)), settings.minStroke, { warn: true }));
    checks.push(check('reach', worstReach <= settings.maxReach, Number(worstReach.toFixed(3)), settings.maxReach,
      { warn: true }));
    checks.push(check('op_budget', maxOps <= settings.opBudget, maxOps, settings.opBudget, { warn: true }));
  } finally {
    restore();
  }

  const measured = new Set(checks.filter((item) => item.status !== 'skipped').map((item) => item.status));
  const status = measured.has('fail') ? 'fail' : measured.has('warn') ? 'warn' : 'pass';
  const reportDir = settings.reportDir ? path.resolve(settings.reportDir) : path.dirname(absolute);
  let relative = path.relative(reportDir, absolute).split(path.sep).join('/');
  if (!relative || path.isAbsolute(relative) || /^[A-Za-z]:/.test(relative)) relative = path.basename(absolute);
  return {
    status,
    method: 'fx_verify.mjs: static scan for forbidden APIs; import and draw under trapped Math.random, Date.now, '
      + 'performance.now, timers and fetch; each effect drawn at every 60 Hz tick, at impactMs +/- 1 and outside its '
      + `lifetime, for env scale ${settings.scale} facing +1 and -1 and scale 1, into a recording Canvas 2D context `
      + '(finite arguments, save/restore balance, restored state, device-space paint boxes); determinism by call-log '
      + 'sha256 across repeats, call order and a fresh module instance',
    notProven: [
      'Pixels: the recording context does not rasterize, so anti-aliasing, colour and readability on a real '
        + 'background are not proven; look at the effect in the game.',
      'Frame time on phones or browsers: op_budget is a count, not a benchmark.',
      'Equality with the baked frames: fx_build.py renders the same geometry, compared by tests, not by this tool.',
    ],
    checks,
    inputs: [{ path: relative, sha256: createHash('sha256').update(readFileSync(absolute)).digest('hex'),
      bytes: Buffer.byteLength(source, 'utf8') }],
    outputs: [],
    tool: TOOL,
    effects: effects.map((effect) => ({ id: effect.id, durationMs: effect.durationMs, impactMs: effect.impactMs,
      loop: Boolean(effect.loop) })),
    findings,
  };
}

// ----------------------------------------------------------------------------- CLI

const USAGE = 'usage: node fx_verify.mjs MODULE [--report FILE] [--scale N] [--width W] [--height H]';
const HELP = `${USAGE}

Check an fx.v1 effect runtime (codeart2d): no randomness, clocks or forbidden APIs; finite
arguments; balanced save/restore; nothing drawn outside the effect lifetime or canvas box;
deterministic; visible at impact; readability (full-screen flashes, thin strokes, reach).
Exit 0 when nothing fails, 1 when a check fails or the module cannot be checked, 2 on a
usage error. Prints one ASCII JSON line; --report writes a QA envelope.
`;

/**
 * Console text is ASCII (plan Appendix D): every character outside printable ASCII becomes a \uXXXX escape, the
 * way Python's json.dumps(ensure_ascii=True) writes non-ASCII text. A JSON line stays valid JSON that decodes to
 * the same strings (a project folder named in Chinese, say), and an error message stays on one line.
 */
export function asciiText(text) {
  return String(text).replace(/[^\x20-\x7e]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

function parseArgs(argv) {
  const options = {};
  let module = null;
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--help' || arg === '-h') return { help: true };
    if (['--report', '--scale', '--width', '--height'].includes(arg)) {
      const value = argv[i + 1];
      if (value === undefined) throw new Error(`${arg} needs a value`);
      i += 1;
      if (arg === '--report') options.report = value;
      else {
        const number = Number(value);
        if (!Number.isFinite(number) || number <= 0) throw new Error(`${arg} must be a positive number`);
        options[arg.slice(2)] = number;
      }
    } else if (arg.startsWith('-')) {
      throw new Error(`unknown option ${arg}`);
    } else if (module === null) {
      module = arg;
    } else {
      throw new Error(`unexpected argument ${arg}`);
    }
  }
  if (module === null) throw new Error('give the fx.v1 module to check (see --help)');
  return { module, options };
}

async function main(argv) {
  let parsed;
  try {
    parsed = parseArgs(argv);
  } catch (exc) {
    // D26: an argument error is a usage error, as argparse reports it: the usage line, "<prog>: error: ...", exit 2
    process.stderr.write(`${USAGE}\nfx_verify.mjs: error: ${asciiText(exc.message)}\n`);
    return 2;
  }
  if (parsed.help) {
    process.stdout.write(HELP);
    return 0;
  }
  const { module, options } = parsed;
  let report;
  try {
    report = await verifyFxModule(module, { ...options, reportDir: options.report ? path.dirname(options.report) : undefined });
    if (options.report) writeFileSync(options.report, `${JSON.stringify(report, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' });
  } catch (exc) {
    process.stderr.write(`error: ${asciiText(exc && exc.message ? exc.message : exc)}\n`);
    return 1;
  }
  const failed = report.checks.filter((item) => item.status === 'fail').map((item) => item.id);
  const warned = report.checks.filter((item) => item.status === 'warn').map((item) => item.id);
  const summary = JSON.stringify({ status: report.status, module: path.resolve(module),
    report: options.report ? path.resolve(options.report) : null, effects: report.effects.map((e) => e.id),
    failed, warned });
  process.stdout.write(`${asciiText(summary)}\n`);
  if (failed.length) {
    process.stderr.write(`error: fx.v1 verification failed: ${asciiText(failed.join(', '))}\n`);
    return 1;
  }
  return 0;
}

function invokedDirectly() {
  if (!process.argv[1]) return false;
  // Compare real paths: node resolves symlinks and junctions for import.meta.url but not for argv[1], so a skills
  // checkout linked into ~/.claude/skills or ~/.codex/skills would otherwise never run main() (a silent exit 0).
  const real = (value) => { try { return realpathSync.native(value); } catch { return path.resolve(value); } };
  const fold = (value) => (process.platform === 'win32' ? value.toLowerCase() : value);
  return fold(real(process.argv[1])) === fold(real(fileURLToPath(import.meta.url)));
}

if (invokedDirectly()) {
  process.exitCode = await main(process.argv.slice(2));
}
