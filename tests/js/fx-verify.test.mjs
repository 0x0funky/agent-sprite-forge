// node --test tests/js/fx-verify.test.mjs
// fx_verify.mjs on the fx.v1 runtime template (must pass) and on mutants that each break one rule of
// references/fx-runtime-contract.md: each must fail exactly its own checks (a trapped Math.random also makes
// the render throw; a skipped non-finite translate draws off target). Pure Node, no packages.

import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  RecordingContext, TOOL, asciiText, scanSource, stripComments, verifyFxModule,
} from '../../skills/codeart2d/scripts/fx_verify.mjs';

const TEMPLATE = fileURLToPath(new URL('../../skills/codeart2d/references/runtime/fx-template.mjs', import.meta.url));
// Mutants match multi-line snippets of the template: normalise CRLF first, so a checkout with
// core.autocrlf=true (the template arrives as CRLF) builds the same mutants as an LF checkout (D32).
const SOURCE = readFileSync(TEMPLATE, 'utf8').replace(/\r\n/g, '\n');
const WORK = mkdtempSync(path.join(tmpdir(), 'fx-verify-'));
after(() => rmSync(WORK, { recursive: true, force: true }));

function mutant(name, edits) {
  let text = SOURCE;
  for (const [from, to] of edits) {
    assert.ok(text.includes(from), `mutant ${name}: the template no longer contains ${from}`);
    text = text.replace(from, to);
  }
  const file = path.join(WORK, `${name}.mjs`);
  writeFileSync(file, text, 'utf8');
  return file;
}

const statuses = (report) => Object.fromEntries(report.checks.map((item) => [item.id, item.status]));
const failed = (report) => report.checks.filter((item) => item.status === 'fail').map((item) => item.id).sort();

test('the runtime template passes every check', async () => {
  const report = await verifyFxModule(TEMPLATE);
  assert.equal(report.status, 'pass', JSON.stringify(report.checks.filter((c) => c.status !== 'pass')));
  for (const key of ['status', 'method', 'notProven', 'checks', 'inputs', 'outputs', 'tool']) assert.ok(key in report);
  assert.equal(report.inputs[0].path, 'fx-template.mjs');
  assert.match(report.inputs[0].sha256, /^[0-9a-f]{64}$/);
  assert.deepEqual(report.effects.map((e) => e.id), ['slash']);
});

const MUTANTS = {
  random: [[['const angle = (p.angle + (hash01(first, i) - 0.5) * p.spread) * DEG;',
    'const angle = (p.angle + (Math.random() - 0.5) * p.spread) * DEG;']],
  ['forbidden_apis', 'no_randomness_or_clocks', 'render_errors', 'visible_at_impact']],
  clock: [[['const local = localTime(effect, t);\n  if (local === null) return false;\n  const list',
    'const local = localTime(effect, t);\n  if (local === null) return false;\n  const stamp = Date.now();\n  const list']],
  ['forbidden_apis', 'no_randomness_or_clocks', 'render_errors', 'visible_at_impact']],
  unbalanced: [[['  } finally {\n    ctx.restore();\n  }', '  } finally {\n    ctx.globalAlpha = 1;\n  }']],
    ['balanced_state']],
  late: [[['return t < effect.durationMs ? t : null;', 'return t % effect.durationMs;']], ['transparent_outside']],
  nan: [[['ctx.translate(finiteOr(env.x, 0), finiteOr(env.y, 0));', 'ctx.translate(Number.NaN, finiteOr(env.y, 0));']],
    ['finite_arguments', 'within_box']],
  stateful: [[['const BY_ID = new Map(', 'let drawCount = 0;\nconst BY_ID = new Map('],
    ['ctx.translate(-origin[0], -origin[1]);', 'ctx.translate(-origin[0] + (drawCount++ % 2), -origin[1]);']],
  ['deterministic']],
  escape: [[['ctx.translate(finiteOr(env.x, 0), finiteOr(env.y, 0));',
    'ctx.translate(finiteOr(env.x, 0) + 4000, finiteOr(env.y, 0));']], ['within_box']],
  throws: [[['const list = buildShapes(effect, local, seedOf(effect, env));',
    'if (local > 200) throw new Error("late frame");\n  const list = buildShapes(effect, local, seedOf(effect, env));']],
  ['render_errors']],
  flash: [[['    list.forEach((shape) => drawShape(ctx, shape, shape.color, 0));',
    "    list.forEach((shape) => drawShape(ctx, shape, shape.color, 0));\n    ctx.setTransform(1, 0, 0, 1, 0, 0);\n"
    + "    ctx.fillStyle = '#ffffff';\n    ctx.fillRect(0, 0, ctx.canvas.width, ctx.canvas.height);"]],
  ['fullscreen_flash', 'within_box']],
  drawImage: [[['    list.forEach((shape) => drawShape(ctx, shape, shape.color, 0));',
    '    list.forEach((shape) => drawShape(ctx, shape, shape.color, 0));\n    ctx.drawImage(null, 0, 0);']],
  ['context_contract', 'forbidden_apis']],
};

for (const [name, [edits, expected]] of Object.entries(MUTANTS)) {
  test(`mutant ${name} fails ${expected.join(' and ')}`, async () => {
    const report = await verifyFxModule(mutant(name, edits));
    assert.equal(report.status, 'fail');
    assert.deepEqual(failed(report), [...expected].sort());
  });
}

test('a module that writes a global fails no_global_writes', async () => {
  const file = mutant('global', [['const DEG = Math.PI / 180;', 'const DEG = Math.PI / 180;\nglobalThis.fxLeak = 1;']]);
  try {
    const report = await verifyFxModule(file);
    assert.equal(statuses(report).no_global_writes, 'fail');
    assert.equal(statuses(report).forbidden_apis, 'fail');
  } finally {
    delete globalThis.fxLeak;
  }
});

test('traps are removed after verification', async () => {
  await verifyFxModule(mutant('random-again', MUTANTS.random[0]));
  assert.equal(typeof Math.random(), 'number');
  assert.equal(typeof Date.now(), 'number');
  assert.equal(globalThis.requestAnimationFrame, undefined);
});

test('the static scan ignores comments but not code', () => {
  assert.deepEqual(scanSource('// Math.random()\nconst a = 1; /* fetch(url) */\n'), []);
  const findings = scanSource('const a = 1;\nconst b = Math.random();\nsetTimeout(f, 1);\n');
  assert.deepEqual(findings.map((f) => [f.api, f.line]), [['Math.random', 2], ['timers', 3]]);
  assert.equal(stripComments('a /* x\ny */ b // z\n"//q"'), 'a     \n     b     \n"//q"');
});

test('the recording context tracks transforms and paint boxes', () => {
  const ctx = new RecordingContext(100, 100);
  ctx.translate(10, 5);
  ctx.scale(2, 3);
  ctx.rotate(Math.PI / 2);
  const [x, y] = ctx.apply(1, 0);
  assert.ok(Math.abs(x - 10) < 1e-9 && Math.abs(y - 8) < 1e-9);
  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.beginPath();
  ctx.arc(20, 30, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
  assert.deepEqual(ctx.ops[0].box, [16, 26, 24, 34]);
  assert.deepEqual(ctx.state.transform.map((v) => Math.round(v * 1e9) / 1e9), [0, 3, -2, 0, 10, 5]);
});

test('console lines are ASCII: non-ASCII becomes JSON \\u escapes that decode back (Appendix D)', () => {
  const summary = JSON.stringify({ module: 'C:\\Users\\\u6797\\fx \u6e2c\u8a66\\fx-runtime.mjs', effects: ['\u00fc', '\u{1F525}'] });
  const line = asciiText(summary);
  assert.match(line, /^[\x20-\x7e]*$/);
  assert.ok(line.includes('\\u6797') && line.includes('\\u6e2c\\u8a66') && line.includes('\\ud83d\\udd25'));
  assert.deepEqual(JSON.parse(line), JSON.parse(summary));
  assert.equal(asciiText('plain ASCII, kept as is'), 'plain ASCII, kept as is');
  assert.equal(asciiText('two\nlines\u007f'), 'two\\u000alines\\u007f');  // an error message stays one line
});

test('the QA envelope names the package version (D29)', () => {
  assert.deepEqual(TOOL, { name: 'codeart2d/fx_verify.mjs', version: '0.4.0' });
});
