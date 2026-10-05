// Unit tests for the video2dsprite runtime references (B09-T2, B09-T3).
// Run: node --test tests/js/forge-runtime.test.mjs (tests/test_runtime_js.py runs it under pytest).
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';
import {test} from 'node:test';
import {fileURLToPath} from 'node:url';

import * as R from '../../skills/video2dsprite/references/runtime/forge-runtime.mjs';
import * as P from '../../skills/video2dsprite/references/runtime/packed-alpha-webgl.mjs';
import {
  createPackedAlphaDrawable, drawAnchoredFrame,
} from '../../skills/video2dsprite/references/packed-alpha-runtime.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const fixture = name => JSON.parse(readFileSync(join(HERE, '..', 'fixtures', 'contracts', name), 'utf8'));
const sum = values => values.reduce((a, b) => a + b, 0);

// ----------------------------------------------------------------------------- timing

test('splitDurations sums exactly with half-up edges', () => {
  assert.deepEqual(R.splitDurations(1000, 3), [333, 334, 333]);
  assert.deepEqual(R.splitDurations(333, 4), [83, 84, 83, 83]);
  for (const [total, n] of [[1, 1], [7, 7], [1001, 30], [540, 4], [100000, 7]]) {
    const parts = R.splitDurations(total, n);
    assert.equal(sum(parts), total);
    assert.ok(Math.max(...parts) - Math.min(...parts) <= 1);
  }
  assert.throws(() => R.splitDurations(3, 4), RangeError);
  assert.throws(() => R.splitDurations(10.5, 2), TypeError);
});

test('frameAt sums exactly: every frame is shown for exactly its duration each cycle', () => {
  const durations = [83, 84, 83, 120, 1];
  const total = sum(durations);
  const shown = new Array(durations.length).fill(0);
  for (let t = 0; t < 2 * total; t++) shown[R.frameAt(durations, t, 'cycle').frame]++;
  assert.deepEqual(shown, durations.map(d => 2 * d));
  // Frames switch exactly on the cumulative edges, with fractional times too.
  let edge = 0;
  durations.forEach((duration, frame) => {
    assert.equal(R.frameAt(durations, edge, 'cycle').frame, frame);
    assert.equal(R.frameAt(durations, edge + duration - 1e-9, 'cycle').frame, frame);
    edge += duration;
  });
  const wrapped = R.frameAt(durations, total * 3 + 83, 'cycle');
  assert.deepEqual([wrapped.frame, wrapped.localMs, wrapped.cycle], [1, 0, 3]);
  assert.equal(R.frameAt(durations, -1, 'cycle').frame, durations.length - 1);
});

test('frameAt oneshot holds the last frame and pingpong mirrors without repeating ends', () => {
  const durations = [100, 100, 100, 100];
  assert.deepEqual(R.frameAt(durations, 399, 'oneshot'), {frame: 3, localMs: 99, cycle: 0, done: false});
  assert.deepEqual(R.frameAt(durations, 5000, 'once'), {frame: 3, localMs: 100, cycle: 0, done: true});
  const order = [];
  for (let t = 50; t < 1200; t += 100) order.push(R.frameAt(durations, t, 'pingpong').frame);
  assert.deepEqual(order, [0, 1, 2, 3, 2, 1, 0, 1, 2, 3, 2, 1]);
  assert.equal(R.frameAt([100], 250, 'pingpong').frame, 0);
  assert.throws(() => R.frameAt(durations, 0, 'sideways'), RangeError);
});

test('eventsCrossed fires each event exactly once per crossing', () => {
  const clip = R.normalizeClip({duration_ms: [100, 100, 100, 100], loop: true,
    events_ms: [{name: 'step_l', at_ms: 0, at: 0}, {name: 'step_r', at_ms: 200, at: 2}]});
  const fired = [];
  for (let t = 0; t < 1200; t += 16.7) {
    fired.push(...R.eventsCrossed(clip, t, Math.min(t + 16.7, 1200)).map(e => `${e.name}@${e.timeMs}`));
  }
  assert.deepEqual(fired, ['step_l@0', 'step_r@200', 'step_l@400', 'step_r@600', 'step_l@800', 'step_r@1000']);
  const once = R.normalizeClip({duration_ms: [100, 100], loop: false, events_ms: [{name: 'hit', at_ms: 100}]});
  assert.equal(R.eventsCrossed(once, 0, 1000).length, 1);
  assert.equal(R.eventsCrossed(once, 0, 100).length, 0);
  // An unexpanded pingpong (0 1 2 1 | 0 1 2 1 ...) repeats every 400 ms.
  const swing = R.normalizeClip({duration_ms: [100, 100, 100], loop: true, loop_policy: 'pingpong',
    events_ms: [{name: 'custom:apex', at_ms: 200}]}, {pingpongExpanded: false});
  assert.equal(swing.mode, 'pingpong');
  assert.deepEqual(R.eventsCrossed(swing, 0, 1000).map(e => e.timeMs), [200, 600]);
});

// ----------------------------------------------------------------------------- manifests

test('normalizeAnimation reads animation.json 2.0 with exact integer durations', () => {
  for (const name of ['video.animation_v3.legacy-2.0.json', 'video.animation_v3.legacy-2.0-media.json']) {
    const a = R.normalizeAnimation(fixture(name));
    assert.equal(a.schemaVersion, '2.0');
    assert.equal(a.frameCount, 4);
    assert.equal(a.totalMs, 333);
    assert.deepEqual(a.durationsMs, [83, 84, 83, 83]);
    assert.equal(a.mode, 'cycle');
    assert.equal(a.events.length, 0);
  }
  const media = R.normalizeAnimation(fixture('video.animation_v3.legacy-2.0-media.json'));
  // 2.0 has no halves: width/height already are the even half; the video is 2 * width wide.
  const packed = media.packedAlpha;
  assert.deepEqual([packed.halfWidth, packed.videoWidth, packed.videoHeight], [16, 32, 20]);
  assert.deepEqual(R.drawableRegion(media, media.packedAlpha), [0, 0, 15, 20]);
  assert.deepEqual(R.fallbackCell(media, 3), {page: 0, file: 'idle-atlas-00.png', x: 48, y: 0, width: 15, height: 20});
});

test('normalizeAnimation reads animation.json 3.0 timing, events, rational fps and packed halves', () => {
  const a = R.normalizeAnimation(fixture('video.animation_v3.valid-rational-fps.json'));
  assert.equal(a.schemaVersion, '3.0');
  assert.deepEqual(a.durationsMs, [33, 34, 33, 33]);
  assert.ok(Math.abs(a.fps - 30000 / 1001) < 1e-12);
  assert.deepEqual(a.events.map(e => [e.name, e.atMs, e.frame]), [['step_l', 0, 0], ['step_r', 67, 2]]);
  assert.deepEqual([a.packedAlpha.width, a.packedAlpha.halfWidth, a.packedAlpha.videoWidth], [24, 24, 48]);
  assert.equal(a.mobilePackedAlpha[0].tier, 'actor');
  assert.equal(R.pickPackedTransport(a, {mobile: true}).tier, 'actor');
  assert.equal(R.pickPackedTransport(a).file, 'idle_ntsc-packed.mp4');
  const short = {...fixture('video.animation_v3.valid-rational-fps.json'), durationsMs: [33]};
  assert.throws(() => R.normalizeAnimation(short), RangeError);
  assert.throws(() => R.normalizeAnimation({schemaVersion: '1.0'}), TypeError);
});

test('normalizeAnimation refuses packed geometry that would crop alpha in the wrong place', () => {
  // The contract example uses forge_av's geometry (D21): width/height are the logical frame, at most the halves.
  const example = fixture('video.animation_v3.valid.json');
  const a = R.normalizeAnimation(example);
  assert.deepEqual([a.packedAlpha.width, a.packedAlpha.halfWidth, a.packedAlpha.videoWidth], [24, 24, 48]);
  // A manifest that gives the video width as width (48 > halfWidth 24) would crop alpha in the wrong place.
  const physical = {...example, packedAlpha: {...example.packedAlpha, width: 48}};
  assert.throws(() => R.normalizeAnimation(physical), /packed alpha geometry/);
  assert.throws(() => R.packedGeometry({layout: 'rgb-top-alpha-bottom', width: 2, height: 2}), TypeError);
  assert.throws(() => R.packedGeometry({layout: R.PACKED_LAYOUT, width: 403, height: 407, halfWidth: 404,
    halfHeight: 408}, 806, 408), /808x408/);
  const g = R.packedGeometry({layout: R.PACKED_LAYOUT, width: 403, height: 407, halfWidth: 404, halfHeight: 408},
    808, 408);
  assert.deepEqual([g.width, g.halfWidth, g.videoWidth, g.videoHeight], [403, 404, 808, 408]);
});

test('normalizeClip reads built sprite clips v1/v2 and engine-export clips', () => {
  const v1 = fixture('sprite.animation_clips_v2.legacy-v1.json');
  const idle = R.normalizeClip(v1.clips.idle, {name: 'idle', manifest: v1});
  assert.deepEqual([idle.totalMs, idle.mode, idle.frameCount], [540, 'cycle', 4]);
  assert.equal(R.normalizeClip(v1.clips.pose, {manifest: v1}).mode, 'oneshot');
  const v2 = fixture('sprite.animation_clips_v2.valid.json');
  const walk = R.normalizeClip(v2.clips.walk, {name: 'walk', manifest: v2});
  assert.deepEqual(walk.events.map(e => [e.name, e.atMs, e.frame]), [['step_l', 0, 0], ['step_r', 200, 2]]);
  const exported = R.normalizeClip({frames: [4, 5, 6], duration_ms: [80, 60, 120], loop: false, loop_policy: 'oneshot',
    events_ms: [{name: 'tell', at_ms: 80, at: 1}, {name: 'hit', at_ms: 140, at: 2}],
    keys: {wind_start: 0, strike: 2}, transitions: [{to: 'idle', entry_frame: 1, dissolve_ms: 60, mode: 'dither'}],
    hitstop_ticks: 4, stride_world_units: 24});
  assert.deepEqual([exported.impactMs, exported.hitstopTicks, exported.loopDistance], [140, 4, 24]);
  assert.deepEqual(R.transitionHint(exported, 'idle'), {to: 'idle', entryFrame: 1, dissolveMs: 60, mode: 'dither'});
  assert.equal(R.transitionHint(exported, 'run'), null);
  assert.equal(R.normalizeClip(exported), exported);
  const late = {duration_ms: [100], loop: true, events_ms: [{name: 'hit', at_ms: 500}]};
  assert.throws(() => R.normalizeClip(late), RangeError);
  assert.throws(() => R.normalizeClip({duration_ms: [0, 100], loop: true}), TypeError);
});

test('anchoredRect places the anchor on the world point, optionally on whole pixels', () => {
  const a = R.normalizeAnimation(fixture('video.animation_v3.legacy-2.0-media.json'));
  // sourceRect [16, 16, 30, 40], sourceAnchor [32, 56]
  assert.deepEqual(R.anchoredRect(a, 100, 200, 2), {x: 68, y: 120, width: 60, height: 80});
  assert.deepEqual(R.anchoredRect({frame_size: [48, 48], anchor_px: [24.5, 44]}, 10.2, 20, 1, {snap: true}),
    {x: -14, y: -24, width: 48, height: 48});
  assert.equal(R.integerScale(270, 64), 4);
  assert.equal(R.integerScale(10, 64), 1);
  assert.equal(R.snapToGrid(10.26, 0.25), 10.25);
});

// ----------------------------------------------------------------------------- walking

test('walkPlayback and gaitFrame share one distance-driven phase', () => {
  const walk = R.normalizeClip({duration_ms: [100, 100, 100, 100], loop: true, stride_world_units: 40});
  for (const distance of [0, 5, 10, 19.9, 20, 39, 41, 1234.5]) {
    const video = R.walkPlayback(walk, distance, 40);
    const sheet = R.gaitFrame(distance, 40, 4);
    assert.ok(Math.abs(video.phase - sheet.phase) < 1e-12);
    assert.equal(video.frame, sheet.frame);
    assert.ok(Math.abs(video.phaseMs - video.phase * 400) < 1e-9);
  }
  // 40 units per 400 ms clip: walking 100 units/s needs 2.5x playback.
  assert.deepEqual(R.walkPlayback(walk, 0, 100).rate, 1);
  const fast = R.walkPlayback(walk, 0, 500);
  assert.deepEqual([fast.wantedRate, fast.rate, fast.rateLimited], [5, 4, true]);
  assert.equal(R.walkPlayback(R.normalizeClip([100, 100]), 3, 1), null);
  assert.equal(R.walkPlayback(R.normalizeClip([100, 100]), 3, 1, {stride: 10}).loopDistance, 10);
});

test('phase is frozen while the actor is blocked', () => {
  const walk = R.normalizeClip({duration_ms: [125, 125, 125, 125], loop: true, stride_world_units: 32});
  const meter = new R.TravelMeter(0, 0);
  const wallX = 50;
  const samples = [];
  for (let tick = 0; tick < 120; tick++) {
    const before = meter.distance;
    const intended = meter.x + 1.5;
    meter.moveTo(Math.min(intended, wallX), 0);     // collision resolves before the gait reads distance
    const speed = (meter.distance - before) * 60;
    samples.push({x: meter.x, ...R.walkPlayback(walk, meter.distance, speed)});
  }
  const blocked = samples.filter(s => s.x === wallX).slice(1);
  assert.ok(blocked.length > 50);
  const first = blocked[0];
  for (const sample of blocked) {
    assert.equal(sample.phase, first.phase);
    assert.equal(sample.phaseMs, first.phaseMs);
    assert.equal(sample.frame, first.frame);
    assert.equal(sample.paused, true);
  }
  assert.ok(samples.slice(0, 20).every(s => !s.paused));
  // Teleports do not add travel; ySquash walks on ground distance.
  meter.teleport(500, 500);
  assert.equal(meter.moveTo(500, 500), 0);
  const squashed = new R.TravelMeter(0, 0, {ySquash: 0.5});
  assert.equal(squashed.moveTo(0, 5), 10);
});

test('entry phase offsets start a gait on its entry frame', () => {
  const walk = R.normalizeClip({duration_ms: [100, 50, 150, 100], loop: true, stride_world_units: 80, entry_frame: 2});
  for (const distance of [0, 13.37, 79.99, 1000]) {
    const offset = R.phaseOffset(distance, walk.loopDistance, R.entryPhase(walk));
    const playback = R.walkPlayback(walk, distance, 0, {offset});
    assert.equal(playback.frame, 2);
    assert.ok(playback.phaseMs - 150 < 1e-3 && playback.phaseMs >= 150);
  }
  assert.throws(() => R.entryPhase(walk, 9), RangeError);
});

// ----------------------------------------------------------------------------- actions

test('mapActionTime lands the clip impact on each gameplay hit', () => {
  const attack = R.normalizeClip({duration_ms: [80, 60, 120, 200], loop: false,
    events_ms: [{name: 'impact', at_ms: 140, at: 2}]});
  for (const hits of [[300], [200, 520], [150, 420, 700, 880]]) {
    const timing = {durationMs: 1000, impactTimesMs: hits};
    hits.forEach((hitMs, index) => {
      const mapped = R.mapActionTime(attack, timing, hitMs);
      assert.equal(mapped.timeMs, 140);
      assert.equal(mapped.hit, index);
      assert.equal(mapped.frame, 2);
      assert.equal(mapped.impactAssumed, false);
    });
    // Monotonic inside each hit's span, never past the clip's last millisecond.
    let previous = -1, previousHit = 0;
    for (let t = 0; t < 1000; t += 5) {
      const mapped = R.mapActionTime(attack, timing, t);
      if (mapped.hit !== previousHit) { previous = -1; previousHit = mapped.hit; }
      assert.ok(mapped.timeMs >= previous && mapped.timeMs <= 459);
      previous = mapped.timeMs;
    }
  }
  assert.equal(R.mapActionTime(attack, {durationMs: 1000, impactTimesMs: [300]}, 1000), null);
  assert.equal(R.mapActionTime(attack, {durationMs: 1000, impactTimesMs: [300]}, -1), null);
  const unknown = R.mapActionTime(R.normalizeClip([100, 100]), {durationMs: 400, impactTimesMs: [200]}, 200);
  assert.deepEqual([unknown.timeMs, unknown.impactAssumed], [90, true]);
  assert.throws(() => R.mapActionTime(attack, {durationMs: 1000, impactTimesMs: [500, 200]}, 10), RangeError);
});

test('clipK and clipLoop stretch key poses over gameplay time', () => {
  const clip = R.normalizeClip({duration_ms: [50, 50, 50, 50, 50, 50], loop: false,
    keys: {wind_start: 0, wind_peak: 2, strike: 3, recover: 5}});
  assert.equal(R.clipK(clip, 'wind_start', 'wind_peak', 0), 0);
  assert.equal(R.clipK(clip, 'wind_start', 'wind_peak', 0.5), 1);
  assert.equal(R.clipK(clip, 'wind_start', 'wind_peak', 7), 2);
  assert.equal(R.clipK(clip, 'strike', 'recover', 0.75), 5);
  assert.equal(R.clipK([4, 9, 12], 0, 2, 0.5), 8);
  assert.equal(R.clipK([4, 9, 12], 1, 99, 1), 12);
  const loop = [0, 25, 50, 75, 100].map(t => R.clipLoop(clip, 'wind_start', 'wind_peak', t, 100));
  assert.deepEqual(loop, [0, 1, 2, 1, 0]);
  assert.throws(() => R.clipK(clip, 'parry', 'strike', 0), RangeError);
});

// ----------------------------------------------------------------------------- fixed step and hit-stop

test('FixedStepLoop steps once per 60 Hz frame despite jitter and interpolates at high refresh', () => {
  const loop = new R.FixedStepLoop({hz: 60});
  let now = 1000, steps = 0;
  loop.tick(now);
  const jitter = [0.4, -0.3, 0.6, -0.5, 0.2, -0.4];
  for (let frame = 0; frame < 600; frame++) {
    now += 1000 / 60 + jitter[frame % jitter.length];
    const result = loop.tick(now);
    assert.equal(result.steps, 1);
    steps += result.steps;
  }
  assert.equal(steps, 600);
  const high = new R.FixedStepLoop({hz: 60});
  let total = 0, alphas = new Set();
  for (let frame = 0; frame < 1440; frame++) {
    const result = high.advance(1000 / 144);
    total += result.steps;
    assert.ok(result.alpha >= 0 && result.alpha <= 1);
    alphas.add(result.alpha.toFixed(3));
  }
  assert.ok(Math.abs(total - 600) <= 1);
  assert.ok(alphas.size > 3);
});

test('FixedStepLoop drops a stall instead of spiralling', () => {
  const loop = new R.FixedStepLoop({hz: 60, maxSteps: 4, maxFrameMs: 250});
  const stall = loop.advance(5000);
  assert.equal(stall.steps, 4);
  assert.equal(stall.steps + stall.dropped, 15);
  assert.ok(stall.alpha < 1);
  assert.equal(loop.advance(16.6667).steps, 1);
  const calls = [];
  loop.reset();
  loop.run(0, () => calls.push('step'));
  loop.run(34, (stepMs, index) => calls.push(index));
  assert.deepEqual(calls, [0, 1]);
  assert.throws(() => new R.FixedStepLoop({snapMs: 20}), RangeError);
});

test('HitStopClock freezes the action clock while the world clock runs', () => {
  const clock = new R.HitStopClock();
  for (let i = 0; i < 10; i++) clock.tick();
  clock.hitStop(8);
  const advanced = [];
  for (let i = 0; i < 12; i++) {
    if (i === 3) clock.hitStop(3);                  // shorter overlapping request: longest wins, no stacking
    advanced.push(clock.tick());
  }
  assert.deepEqual(advanced, [false, false, false, false, false, false, false, false, true, true, true, true]);
  assert.deepEqual([clock.worldTicks, clock.actionTicks, clock.frozen], [22, 14, false]);
  assert.equal(clock.actionMs, 14 * 1000 / 60);
  assert.throws(() => clock.hitStop(-1), RangeError);
});

// ----------------------------------------------------------------------------- transitions

test('ditherDissolve switches each pixel once and covers the progress fraction', () => {
  for (let y = 0; y < 8; y++) {
    for (let x = 0; x < 8; x++) {
      let switched = false;
      for (let step = 0; step <= 32; step++) {
        const keepOld = R.ditherDissolve(step / 32, x, y);
        if (switched) assert.equal(keepOld, false);
        if (!keepOld) switched = true;
      }
      assert.equal(R.ditherDissolve(0, x, y), true);
      assert.equal(R.ditherDissolve(1, x, y), false);
    }
  }
  for (let level = 0; level <= 16; level++) {
    let gone = 0;
    for (let y = 0; y < 4; y++) for (let x = 0; x < 4; x++) gone += R.ditherDissolve(level / 16, x, y) ? 0 : 1;
    assert.equal(gone, level);
  }
  assert.equal(R.dissolveProgress(30, 60), 0.5);
  assert.equal(R.dissolveProgress(30, 0), 1);
});

test('ditherDissolvePixels never punches holes in the incoming pose; premultipliedMix keeps endpoints', () => {
  const w = 4, h = 4, from = new Uint8ClampedArray(w * h * 4), to = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i++) {
    to.set([10, 200, 30, 255], i * 4);
    if (i % 2 === 0) from.set([250, 20, 20, 255], i * 4);
  }
  for (const progress of [0, 0.3, 0.7, 1]) {
    const out = R.ditherDissolvePixels(from, to, w, h, progress);
    for (let i = 0; i < w * h; i++) assert.equal(out[i * 4 + 3], 255);
  }
  const mixStart = R.premultipliedMix(from, to, 0), mixEnd = R.premultipliedMix(from, to, 1);
  assert.deepEqual([...mixStart.slice(0, 8)], [...from.slice(0, 8)]);
  assert.deepEqual([...mixEnd], [...to]);
  const half = R.premultipliedMix(new Uint8ClampedArray([255, 0, 0, 255]), new Uint8ClampedArray([0, 0, 255, 0]), 0.5);
  assert.deepEqual([...half], [255, 0, 0, 128]);
});

// ----------------------------------------------------------------------------- packed alpha helpers

test('alpha snap removes codec noise at both ends only', () => {
  // Float levels a GPU may return for 8-bit 2 and 253 must snap; 3 and 252 must not.
  const wobbles = [-1e-7, 0, 1e-7];
  for (const level of [0, 1, 2]) for (const w of wobbles) assert.equal(P.alphaSnap(level / 255 + w), 0);
  for (const level of [253, 254, 255]) for (const w of wobbles) assert.equal(P.alphaSnap(level / 255 + w), 1);
  assert.equal(P.alphaSnap(3 / 255), 3 / 255);
  assert.equal(P.alphaSnap(252 / 255), 252 / 255);
  assert.deepEqual([0, 1, 2, 3, 128, 252, 253, 254, 255].map(a => P.alphaSnapByte(a)),
    [0, 0, 0, 3, 128, 252, 255, 255, 255]);
});

test('seam UV clamp keeps every bilinear tap inside the content of its half', () => {
  const cases = [[24, 24, 24, 24], [403, 407, 404, 408], [5, 3, 6, 4], [1, 1, 2, 2]];
  for (const [width, height, halfWidth, halfHeight] of cases) {
    const g = R.packedGeometry({layout: R.PACKED_LAYOUT, width, height, halfWidth, halfHeight});
    const textureWidth = 2 * halfWidth;
    for (let k = 0; k <= 200; k++) {
      const u = k / 200, v = 1 - k / 200;
      const {rgb, alpha} = P.packedSampleCoords(u, v, g);
      const rgbTexel = rgb[0] * textureWidth, alphaTexel = alpha[0] * textureWidth, row = rgb[1] * halfHeight;
      // A bilinear tap reads texels floor(x - 0.5) and floor(x - 0.5) + 1 (the second only with weight > 0).
      const eps = 1e-9;
      assert.ok(rgbTexel >= 0.5 - eps && rgbTexel <= width - 0.5 + eps, `rgb tap ${rgbTexel} leaves [0, ${width})`);
      assert.ok(alphaTexel >= halfWidth + 0.5 - eps && alphaTexel <= halfWidth + width - 0.5 + eps);
      assert.ok(row >= 0.5 - eps && row <= height - 0.5 + eps);
      assert.equal(alpha[1], rgb[1]);
      assert.ok(Math.abs(alpha[0] - rgb[0] - 0.5) < 1e-12);
    }
    // At output pixel centres the taps land exactly on texel centres (no filtering at 1:1).
    const centre = P.packedSampleCoords(1.5 / width, 0.5 / height, g);
    if (width > 1) assert.equal(centre.rgb[0] * textureWidth, 1.5);
  }
  assert.equal(P.seamClamp(-3, 10), 0.5);
  assert.equal(P.seamClamp(12, 10), 9.5);
});

function packedFrame(geometry, pixel) {
  const {videoWidth, videoHeight, width, height, halfWidth} = geometry;
  const data = new Uint8ClampedArray(videoWidth * videoHeight * 4).fill(37);   // padding is never black after coding
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const [r, g, b, a] = pixel(x, y);
      data.set([r, g, b, 255], (y * videoWidth + x) * 4);
      data.set([a, a, a, 255], (y * videoWidth + halfWidth + x) * 4);
    }
  }
  return data;
}

const ALPHA_LEVELS = [0, 1, 2, 3, 128, 252, 253, 255];
const samplePixel = (x, y) => [(x * 40 + 7) % 256, (y * 70 + 3) % 256, (x * y * 13) % 256,
  ALPHA_LEVELS[(x + 3 * y) % 8]];

test('composePackedPixels reads alpha from the right half and zeroes hidden colour', () => {
  const g = R.packedGeometry({layout: R.PACKED_LAYOUT, width: 5, height: 3, halfWidth: 6, halfHeight: 4});
  const src = packedFrame(g, samplePixel);
  const out = P.composePackedPixels(src, g.videoWidth, g, new Uint8ClampedArray(5 * 3 * 4));
  const raw = P.composePackedPixels(src, g.videoWidth, g, new Uint8ClampedArray(5 * 3 * 4), {snap: false});
  for (let y = 0; y < 3; y++) {
    for (let x = 0; x < 5; x++) {
      const [r, gg, b, a] = samplePixel(x, y), i = (y * 5 + x) * 4, snapped = P.alphaSnapByte(a);
      assert.deepEqual([...out.slice(i, i + 4)], snapped ? [r, gg, b, snapped] : [0, 0, 0, 0]);
      assert.deepEqual([...raw.slice(i, i + 4)], a ? [r, gg, b, a] : [0, 0, 0, 0]);
    }
  }
  assert.throws(() => P.composePackedPixels(src, 10, g, new Uint8ClampedArray(60)), RangeError);
});

test('shader sources implement the seam clamp, alpha snap and premultiplied output', () => {
  assert.match(P.FRAGMENT_SHADER, /clamp\(texel, vec2\(0\.5\), uContent - vec2\(0\.5\)\)/);
  assert.match(P.FRAGMENT_SHADER, /texel\.y \/ uHalf\.y/);
  assert.match(P.FRAGMENT_SHADER, /vec4\(rgb \* alpha, alpha\)/);
  assert.match(P.FRAGMENT_SHADER, /alpha < uSnap\.x/);
  assert.match(P.VERTEX_SHADER, /\(1\.0 - aPosition\.y\) \* 0\.5/);
  assert.ok(/^[\x00-\x7f]*$/.test(P.FRAGMENT_SHADER + P.VERTEX_SHADER));
});

// ----------------------------------------------------------------------------- compositor with fake canvases

class FakeContext2D {
  constructor(canvas) { this.canvas = canvas; this.calls = []; }
  clearRect(x, y, w, h) {
    for (let row = y; row < y + h && row < this.canvas.height; row++) {
      const start = row * this.canvas.width;
      this.canvas.pixels.fill(0, (start + x) * 4, (start + Math.min(x + w, this.canvas.width)) * 4);
    }
  }
  drawImage(source, ...args) {
    this.calls.push(['drawImage', source, ...args]);
    if (args.length !== 2 || !source.pixels) return;
    const sw = source.videoWidth ?? source.width, sh = source.videoHeight ?? source.height;
    for (let y = 0; y < sh; y++) {
      for (let x = 0; x < sw; x++) {
        const s = (y * sw + x) * 4, d = ((y + args[1]) * this.canvas.width + x + args[0]) * 4;
        this.canvas.pixels.set(source.pixels.subarray(s, s + 4), d);
      }
    }
  }
  getImageData(x, y, w, h) {
    const data = new Uint8ClampedArray(w * h * 4);
    for (let row = 0; row < h; row++) {
      const start = ((row + y) * this.canvas.width + x) * 4;
      data.set(this.canvas.pixels.subarray(start, start + w * 4), row * w * 4);
    }
    return {width: w, height: h, data};
  }
  createImageData(w, h) { return {width: w, height: h, data: new Uint8ClampedArray(w * h * 4)}; }
  putImageData(image, x, y) {
    for (let row = 0; row < image.height; row++) {
      this.canvas.pixels.set(image.data.subarray(row * image.width * 4, (row + 1) * image.width * 4),
        ((row + y) * this.canvas.width + x) * 4);
    }
  }
}

class FakeCanvas {
  constructor(width, height, webgl) {
    this._width = width; this._height = height; this.webgl = webgl; this.listeners = new Map(); this.gl = null;
    this.pixels = new Uint8ClampedArray(width * height * 4);
  }
  get width() { return this._width; }
  set width(value) { this._width = value; this.pixels = new Uint8ClampedArray(this._width * this._height * 4); }
  get height() { return this._height; }
  set height(value) { this._height = value; this.pixels = new Uint8ClampedArray(this._width * this._height * 4); }
  getContext(type) {
    if (type === '2d') return (this.ctx ??= new FakeContext2D(this));
    if (type === 'webgl' && this.webgl) return (this.gl ??= this.webgl(this));
    return null;
  }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  removeEventListener(type) { this.listeners.delete(type); }
  dispatch(type) { this.listeners.get(type)?.({type, preventDefault() { this.prevented = true; }}); }
}

function fakeWebGL(log, {compiles = true} = {}) {
  const constants = {NO_ERROR: 0, VERTEX_SHADER: 1, FRAGMENT_SHADER: 2, COMPILE_STATUS: 3, LINK_STATUS: 4,
    ARRAY_BUFFER: 5, STATIC_DRAW: 6, FLOAT: 7, TEXTURE0: 8, TEXTURE_2D: 9, TEXTURE_MIN_FILTER: 10,
    TEXTURE_MAG_FILTER: 11, TEXTURE_WRAP_S: 12, TEXTURE_WRAP_T: 13, LINEAR: 14, CLAMP_TO_EDGE: 15,
    UNPACK_FLIP_Y_WEBGL: 16, UNPACK_PREMULTIPLY_ALPHA_WEBGL: 17, BLEND: 18, RGBA: 19, UNSIGNED_BYTE: 20,
    COLOR_BUFFER_BIT: 21, TRIANGLES: 22};
  let lost = false, id = 0;
  return () => new Proxy({}, {
    get(_, name) {
      if (typeof name !== 'string') return undefined;
      if (name in constants) return constants[name];
      if (name === 'isContextLost') return () => lost;
      if (name === 'setLost') return value => { lost = value; };
      if (name === 'getShaderParameter') return () => compiles;
      if (name === 'getProgramParameter') return () => true;
      if (name === 'getShaderInfoLog') return () => 'ERROR: 0:1: fake failure';
      if (name === 'getError') return () => 0;
      if (name === 'getExtension') return () => null;
      if (name === 'getAttribLocation') return () => 0;
      if (name === 'getUniformLocation') return (_program, uniform) => ({uniform});
      if (name.startsWith('create')) return () => { log.push([name]); return {id: ++id}; };
      return (...args) => { log.push([name, ...args]); };
    },
  });
}

function fakeVideo(geometry, pixel = samplePixel) {
  return {readyState: 4, seeking: false, currentTime: 0, videoWidth: geometry.videoWidth,
    videoHeight: geometry.videoHeight, pixels: packedFrame(geometry, pixel)};
}

test('CPU fallback composes the exact straight-alpha frame into lease.drawable', () => {
  const canvases = [];
  const compositor = P.createPackedAlphaCompositor({webgl: false,
    createCanvas: (w, h) => { const c = new FakeCanvas(w, h); canvases.push(c); return c; }});
  const meta = {layout: R.PACKED_LAYOUT, width: 5, height: 3, halfWidth: 6, halfHeight: 4};
  const geometry = R.packedGeometry(meta);
  const video = fakeVideo(geometry);
  const lease = compositor.acquire(video, meta);
  assert.equal(lease.video, video);
  assert.notEqual(lease.drawable, video);
  assert.equal(lease.update(), true);
  assert.equal(lease.update(), false);              // same decoded frame: nothing to do
  video.currentTime = 0.04;
  assert.equal(lease.update(), true);
  assert.equal(lease.mode, 'cpu');
  const expected = P.composePackedPixels(video.pixels, geometry.videoWidth, geometry, new Uint8ClampedArray(60));
  assert.deepEqual([...lease.drawable.pixels], [...expected]);
  assert.equal(compositor.mode, 'cpu');
  lease.release();
  assert.equal(lease.update(true), false);
  assert.equal(compositor.getStats().activeLeases, 0);
});

test('one shared WebGL context serves every lease and reuses its texture', () => {
  const log = [], canvases = [];
  const compositor = P.createPackedAlphaCompositor({
    createCanvas: (w, h) => { const c = new FakeCanvas(w, h, fakeWebGL(log)); canvases.push(c); return c; }});
  const small = {layout: R.PACKED_LAYOUT, width: 5, height: 3, halfWidth: 6, halfHeight: 4};
  const large = {layout: R.PACKED_LAYOUT, width: 9, height: 7, halfWidth: 10, halfHeight: 8};
  const a = compositor.acquire(fakeVideo(R.packedGeometry(small)), small);
  const b = compositor.acquire(fakeVideo(R.packedGeometry(large)), large);
  assert.equal(compositor.mode, 'pending');
  assert.equal(a.update(), true);
  a.video.currentTime = 0.1;
  assert.equal(a.update(), true);
  assert.equal(b.update(), true);
  assert.equal(compositor.mode, 'webgl');
  const contexts = canvases.filter(c => c.gl);
  assert.equal(contexts.length, 1);
  const count = name => log.filter(entry => entry[0] === name).length;
  assert.equal(count('createProgram'), 1);
  assert.equal(count('createTexture'), 1);
  assert.equal(count('texImage2D'), 2);              // first upload, then the larger video
  assert.equal(count('texSubImage2D'), 1);
  const stats = compositor.getStats();
  assert.deepEqual(stats.scratchSize, [9, 7]);
  assert.equal(stats.webglFrames, 3);
  // Each lease copies the lower-left viewport of the shared drawing buffer into its own canvas.
  const copy = b.drawable.ctx.calls.find(call => call[0] === 'drawImage');
  assert.deepEqual(copy.slice(2), [0, 0, 9, 7, 0, 0, 9, 7]);
  assert.equal(copy[1], contexts[0]);
  assert.deepEqual(log.find(entry => entry[0] === 'viewport'), ['viewport', 0, 0, 5, 3]);
  compositor.destroy();
  assert.equal(log.filter(entry => entry[0] === 'deleteProgram').length, 1);
});

test('context loss falls back to the CPU path and restores the GPU path', () => {
  const log = [], canvases = [];
  const compositor = P.createPackedAlphaCompositor({
    createCanvas: (w, h) => { const c = new FakeCanvas(w, h, fakeWebGL(log)); canvases.push(c); return c; }});
  const meta = {layout: R.PACKED_LAYOUT, width: 5, height: 3, halfWidth: 6, halfHeight: 4};
  const geometry = R.packedGeometry(meta);
  const video = fakeVideo(geometry);
  const lease = compositor.acquire(video, meta);
  assert.equal(lease.update(), true);
  assert.equal(lease.mode, 'webgl');
  const glCanvas = canvases.find(c => c.gl);
  glCanvas.gl.setLost(true);
  glCanvas.dispatch('webglcontextlost');
  video.currentTime = 1;
  assert.equal(lease.update(), true);
  assert.equal(lease.mode, 'cpu');
  const expected = P.composePackedPixels(video.pixels, geometry.videoWidth, geometry, new Uint8ClampedArray(60));
  assert.deepEqual([...lease.drawable.pixels], [...expected]);
  glCanvas.gl.setLost(false);
  glCanvas.dispatch('webglcontextrestored');
  video.currentTime = 2;
  assert.equal(lease.update(), true);
  assert.equal(lease.mode, 'webgl');
  assert.equal(log.filter(entry => entry[0] === 'createProgram').length, 2);
  const stats = compositor.getStats();
  assert.deepEqual([stats.contextLosses, stats.contextRestores, stats.cpuFrames, stats.webglFrames], [1, 1, 1, 2]);
});

test('without WebGL, or with a shader that fails to compile, every frame takes the CPU path', () => {
  const meta = {layout: R.PACKED_LAYOUT, width: 5, height: 3, halfWidth: 6, halfHeight: 4};
  const geometry = R.packedGeometry(meta);
  for (const webgl of [undefined, fakeWebGL([], {compiles: false})]) {
    const compositor = P.createPackedAlphaCompositor({createCanvas: (w, h) => new FakeCanvas(w, h, webgl)});
    const video = fakeVideo(geometry);
    const lease = compositor.acquire(video, meta, {outline: true});
    assert.equal(lease.update(), true);
    assert.equal(lease.mode, 'cpu');
    assert.equal(compositor.mode, 'cpu');
    assert.equal(compositor.state, webgl ? 'failed' : 'unavailable');
    const expected = P.composePackedPixels(video.pixels, geometry.videoWidth, geometry, new Uint8ClampedArray(60));
    assert.deepEqual([...lease.drawable.pixels], [...expected]);
    const stats = compositor.getStats();
    assert.equal(stats.outlineSkipped, 1);                // the outline is drawn on the GPU path only
    if (webgl) assert.match(stats.errors[0], /failed to compile: ERROR: 0:1: fake failure/);
  }
});

test('a lease fails once, with a clear error, when the video does not match its metadata', () => {
  const errors = [];
  const compositor = P.createPackedAlphaCompositor({webgl: false, createCanvas: (w, h) => new FakeCanvas(w, h),
    onError: error => errors.push(error)});
  const meta = {layout: R.PACKED_LAYOUT, width: 5, height: 3, halfWidth: 6, halfHeight: 4};
  const video = {...fakeVideo(R.packedGeometry(meta)), videoWidth: 10};
  const lease = compositor.acquire(video, meta);
  assert.equal(lease.update(), false);
  assert.equal(lease.update(true), false);
  assert.equal(lease.failed(), true);
  assert.match(lease.error, /10x4; the manifest needs 12x4/);
  assert.equal(errors.length, 1);
  const notReady = compositor.acquire({...fakeVideo(R.packedGeometry(meta)), readyState: 1}, meta);
  assert.equal(notReady.update(), false);
  assert.equal(notReady.failed(), false);
  assert.throws(() => compositor.acquire(video, {...meta, layout: 'other'}), TypeError);
});

test('requestVideoFrameCallback drives recomposition and is cancelled on release', () => {
  const compositor = P.createPackedAlphaCompositor({webgl: false, createCanvas: (w, h) => new FakeCanvas(w, h)});
  const meta = {layout: R.PACKED_LAYOUT, width: 5, height: 3, halfWidth: 6, halfHeight: 4};
  const callbacks = [], cancelled = [];
  const video = {...fakeVideo(R.packedGeometry(meta)),
    requestVideoFrameCallback(callback) { callbacks.push(callback); return callbacks.length; },
    cancelVideoFrameCallback(handle) { cancelled.push(handle); }};
  const lease = compositor.acquire(video, meta);
  assert.equal(lease.update(), true);
  assert.equal(lease.update(), false);
  callbacks.at(-1)(0, {presentedFrames: 7});           // a new frame was presented at the same currentTime
  assert.equal(lease.update(), true);
  lease.release();
  assert.deepEqual(cancelled, [callbacks.length]);
});

test('createPackedAlphaDrawable keeps the old wrapper contract; drawAnchoredFrame honours regions', () => {
  const compositor = P.createPackedAlphaCompositor({webgl: false, createCanvas: (w, h) => new FakeCanvas(w, h)});
  const meta = {layout: R.PACKED_LAYOUT, width: 16, height: 20, encodedSize: [32, 20]};   // animation.json 2.0
  const output = createPackedAlphaDrawable(fakeVideo(R.packedGeometry(meta)), meta, {compositor});
  assert.equal(output.update(), true);
  assert.equal(output.drawable.width, 16);
  const broken = createPackedAlphaDrawable({...fakeVideo(R.packedGeometry(meta)), videoHeight: 21}, meta, {compositor});
  assert.throws(() => broken.update(), /32x21/);
  const clip = R.normalizeAnimation(fixture('video.animation_v3.legacy-2.0-media.json'));
  const calls = [];
  const ctx = {drawImage: (...args) => calls.push(args)};
  drawAnchoredFrame(ctx, output.drawable, clip, 100, 200, 2);
  assert.deepEqual(calls[0].slice(1), [0, 0, 15, 20, 68, 120, 60, 80]);
  drawAnchoredFrame(ctx, {drawable: output.drawable, region: [0, 0, 7.5, 10]}, clip, 100.4, 200.6, 2, {snap: true});
  assert.deepEqual(calls[1].slice(1), [0, 0, 7.5, 10, 68, 121, 60, 80]);
  output.release();
});
