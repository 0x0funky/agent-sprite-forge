/**
 * forge-runtime.mjs: DOM-free playback helpers for Agent Sprite Forge animations.
 *
 * Reads animation.json 2.0 and 3.0 (video2dsprite engine_export), the clips of
 * animation-clips.json v1/v2 (generate2dsprite build_animation_clips) and the
 * clips of engine-export.json (generate2dsprite export_engine). Times are
 * milliseconds, frame positions are 0-based and ranges are [start, endExclusive).
 *
 * Nothing here touches the DOM, timers or randomness: the game owns its clock and
 * passes times in, so every function is deterministic and testable under Node
 * (node --test). Copy this file next to packed-alpha-webgl.mjs; neither has
 * dependencies.
 *
 * Contents:
 *   timing        splitDurations, parseFps, frameAt, eventsCrossed
 *   manifests     normalizeClip, normalizeAnimation, packedGeometry, pickPackedTransport,
 *                 drawableRegion, fallbackCell, anchoredRect
 *   walking       gaitPhase, gaitFrame, phaseOffset, entryPhase, walkPlayback, TravelMeter
 *   actions       mapActionTime (time-warp onto gameplay hits), clipK, clipLoop
 *   loop          FixedStepLoop (fixed 60 Hz steps + interpolation alpha), lerp
 *   hit-stop      HitStopClock (world clock keeps running, action clock freezes)
 *   transitions   transitionHint, ditherDissolve, ditherThreshold, dissolveProgress,
 *                 ditherDissolvePixels, premultipliedMix
 *   pixels        integerScale, snapToGrid
 */

export const RUNTIME_VERSION = '1';
export const TICK_HZ = 60;
export const PACKED_LAYOUT = 'rgb-left-alpha-right';

const NORMALIZED = new WeakSet();
const LOOP_POLICIES = new Set(['cycle', 'pingpong', 'oneshot']);
const MODES = new Map([['cycle', 'cycle'], ['loop', 'cycle'], ['oneshot', 'oneshot'], ['once', 'oneshot'],
  ['pingpong', 'pingpong']]);

export const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
export const lerp = (a, b, t) => a + (b - a) * t;
const mod = (value, size) => ((value % size) + size) % size;
const isNumber = value => typeof value === 'number' && Number.isFinite(value);
const isPositive = value => isNumber(value) && value > 0;
const isIndex = value => Number.isInteger(value) && value >= 0;

function pair(value, label, test = isNumber) {
  if (!Array.isArray(value) || value.length !== 2 || !value.every(test)) {
    throw new TypeError(`${label} must be two numbers`);
  }
  return [value[0], value[1]];
}

// --------------------------------------------------------------------------- timing

/**
 * Split totalMs into count integer durations that sum exactly. Frame i ends at
 * round_half_up((i + 1) * totalMs / count), the rule of forge_core.frame_durations,
 * so the playhead never drifts more than 0.5 ms.
 */
export function splitDurations(totalMs, count) {
  if (!Number.isInteger(totalMs) || !Number.isInteger(count) || count < 1) {
    throw new TypeError('splitDurations needs an integer total and a positive integer count');
  }
  if (totalMs < count) throw new RangeError(`${totalMs} ms cannot hold ${count} frames of at least 1 ms`);
  const edges = [];
  for (let i = 0; i <= count; i++) edges.push(Math.floor((2 * i * totalMs + count) / (2 * count)));
  return edges.slice(1).map((end, i) => end - edges[i]);
}

/** A frame rate as a number: a positive number or an exact "num/den" string such as "30000/1001". */
export function parseFps(value) {
  if (isPositive(value)) return value;
  const match = typeof value === 'string' ? /^([1-9][0-9]*)\/([1-9][0-9]*)$/.exec(value) : null;
  if (!match) throw new TypeError(`Invalid fps ${JSON.stringify(value)}; use a positive number or "num/den"`);
  return Number(match[1]) / Number(match[2]);
}

function buildTimeline(durationsMs) {
  if (!Array.isArray(durationsMs) || !durationsMs.length || !durationsMs.every(isPositive)) {
    throw new TypeError('durationsMs must be a non-empty list of positive milliseconds');
  }
  const edges = [0];
  for (const duration of durationsMs) edges.push(edges[edges.length - 1] + duration);
  return {durationsMs: Object.freeze([...durationsMs]), edges: Object.freeze(edges), totalMs: edges[edges.length - 1]};
}

function asTimeline(source) {
  if (Array.isArray(source)) return buildTimeline(source);
  if (source && Array.isArray(source.edges) && Array.isArray(source.durationsMs) && isPositive(source.totalMs)) {
    return source;
  }
  return normalizeClip(source);
}

function locate(edges, timeMs) {
  // Largest i with edges[i] <= timeMs, for timeMs in [0, total).
  let low = 0, high = edges.length - 2;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (edges[middle] <= timeMs) low = middle; else high = middle - 1;
  }
  return low;
}

function mirrored(timeline) {
  const n = timeline.durationsMs.length;
  if (n < 2) return {timeline, positions: [0]};
  const positions = [];
  for (let i = 0; i < n; i++) positions.push(i);
  for (let i = n - 2; i > 0; i--) positions.push(i);
  return {timeline: buildTimeline(positions.map(i => timeline.durationsMs[i])), positions};
}

/**
 * The frame shown at timeMs.
 *
 * source: a list of durations, a normalized clip or anything normalizeClip reads.
 * mode: 'cycle' (alias 'loop'), 'oneshot' (alias 'once': holds the last frame) or
 * 'pingpong' (mirrors the list without repeating its end frames). Built clips and
 * animation.json already list pingpong clips expanded, so their mode is 'cycle';
 * the default is the clip's own mode.
 *
 * Returns {frame, localMs, cycle, done}: the 0-based frame position, the time
 * inside that frame, the completed cycles and whether a oneshot has ended.
 * Integer durations sum exactly, so frame i is shown for exactly durationsMs[i]
 * of every cycle.
 */
export function frameAt(source, timeMs, mode) {
  if (!isNumber(timeMs)) throw new TypeError('timeMs must be a finite number');
  const base = asTimeline(source);
  const resolved = MODES.get(mode ?? source?.mode ?? 'cycle');
  if (!resolved) throw new RangeError(`Unknown playback mode ${JSON.stringify(mode)}`);
  const {timeline, positions} = resolved === 'pingpong' ? mirrored(base) : {timeline: base, positions: null};
  const total = timeline.totalMs, last = timeline.durationsMs.length - 1;
  let cycle = 0, local = timeMs, done = false;
  if (resolved === 'oneshot') {
    if (timeMs >= total) {
      return {frame: last, localMs: timeline.durationsMs[last], cycle: 0, done: true};
    }
    local = Math.max(0, timeMs);
  } else {
    cycle = Math.floor(timeMs / total);
    local = timeMs - cycle * total;
    if (local >= total) { local -= total; cycle += 1; }
  }
  const position = locate(timeline.edges, local);
  return {frame: positions ? positions[position] : position, localMs: local - timeline.edges[position], cycle, done};
}

/**
 * Events whose time falls in [fromMs, toMs) of continuous playback, in order.
 * Call it every update with the previous and the new clip time; an event fires
 * exactly once per crossing, cycles included. In a oneshot clip only the
 * first pass counts; an unexpanded pingpong clip fires its events on the
 * forward pass of each mirrored cycle. Each result is
 * {name, atMs, frame, data, cycle, timeMs}.
 */
export function eventsCrossed(clip, fromMs, toMs) {
  const c = normalizeClip(clip);
  if (!isNumber(fromMs) || !isNumber(toMs)) throw new TypeError('fromMs and toMs must be finite numbers');
  if (toMs <= fromMs || !c.events.length) return [];
  const last = c.durationsMs.length - 1;
  const period = c.mode === 'pingpong' && last > 0 ? 2 * c.totalMs - c.durationsMs[0] - c.durationsMs[last] : c.totalMs;
  const result = [];
  const cycles = c.mode === 'oneshot' ? [0, 0] : [Math.floor(fromMs / period), Math.floor(toMs / period)];
  for (let k = cycles[0]; k <= cycles[1]; k++) {
    for (const event of c.events) {
      const timeMs = k * period + event.atMs;
      if (timeMs >= fromMs && timeMs < toMs) result.push({...event, cycle: k, timeMs});
    }
  }
  return result;
}

// --------------------------------------------------------------------------- manifests

/**
 * Events with the frame each one fires on. The time (atMs / at_ms) is authoritative: the frame is
 * the played frame shown at that time (an event at the end edge names the last frame). A given
 * played index (frame in animation.json, position in built and exported sprite clips) must agree
 * with the time. A sprite clip's `at` is its AUTHORED position (sprite.schema builtClip, D12): in a
 * pingpong clip played 0 1 2 1, an event authored at 1 fires on frames 1 and 3, so `at` is never
 * read as a frame.
 */
function normalizeEvents(events, timeline, label) {
  if (events === undefined || events === null) return [];
  if (!Array.isArray(events)) throw new TypeError(`${label} events must be a list`);
  return events.map((event, index) => {
    const atMs = event?.atMs ?? event?.at_ms;
    if (typeof event?.name !== 'string' || !event.name || !isNumber(atMs) || atMs < 0 || atMs > timeline.totalMs) {
      throw new RangeError(`${label} event ${index} needs a name and a time inside the clip`);
    }
    const frame = frameAt(timeline, atMs, 'oneshot').frame;
    const given = event.frame ?? event.position;
    if (given !== undefined && given !== frame) {
      throw new RangeError(`${label} event ${index} (${event.name}) names frame ${given}, but frame ${frame} `
        + `is shown at ${atMs} ms`);
    }
    const result = {name: event.name, atMs, frame};
    if (event.data !== undefined) result.data = event.data;
    return Object.freeze(result);
  }).sort((a, b) => a.atMs - b.atMs || a.frame - b.frame);
}

function resolveImpact(explicit, events) {
  if (isNumber(explicit)) return explicit;
  const event = events.find(e => e.name === 'impact') ?? events.find(e => e.name === 'hit');
  return event ? event.atMs : undefined;
}

function transitionHints(value) {
  if (!Array.isArray(value)) return [];
  return value.filter(item => item && typeof item.to === 'string' && item.to).map(item => Object.freeze({
    to: item.to,
    entryFrame: item.entry_frame ?? item.entryFrame ?? 0,
    dissolveMs: item.dissolve_ms ?? item.dissolveMs ?? 0,
    mode: item.mode ?? 'dither',
  }));
}

function finishClip(fields) {
  const clip = Object.freeze(fields);
  NORMALIZED.add(clip);
  return clip;
}

function modeFor(loop, loopPolicy, pingpongExpanded) {
  if (loopPolicy === 'oneshot' || loop === false) return 'oneshot';
  if (loopPolicy === 'pingpong' && !pingpongExpanded) return 'pingpong';
  return 'cycle';
}

/**
 * One clip as a frozen, uniform record.
 *
 * Reads, by shape:
 *   - animation.json 2.0 or 3.0 (has schemaVersion; see normalizeAnimation);
 *   - a clip of animation-clips.json v1/v2 or of engine-export.json
 *     (duration_ms, loop, loop_policy, events_ms, keys, entry_frame,
 *     transition_hints, hitstop_ticks, stride_world_units, ...);
 *   - a camelCase record with durationsMs (and optional loop, loopPolicy, events, ...);
 *   - a bare list of durations.
 * options: {name, manifest (the enclosing animation-clips.json, for sampling and
 * pixel_art), pingpongExpanded (default true: manifests list pingpong expanded)}.
 *
 * Sprite clips list the PLAYED frames: a pingpong clip arrives expanded
 * (0 1 2 3 2 1). Events fire on the frame shown at their time (see
 * normalizeEvents). keys, entry_frame and a hint's entry_frame are authored
 * positions (D12); the played order starts with the authored forward pass, so
 * authored position p is first shown as frame p and they index the timeline
 * directly. Transition hints come from transition_hints; a clip without them
 * falls back to transitions items that name a target clip with `to` (D13: built
 * clips keep transitions for the frame-to-frame metrics).
 *
 * loopDistance is the travel in world units for one pass of the whole clip:
 * stride_world_units for sprite clips (declared per clip cycle) and
 * strideWorldUnits x cycles for animation.json (declared per gait cycle).
 */
export function normalizeClip(input, options = {}) {
  if (NORMALIZED.has(input)) return input;
  if (Array.isArray(input)) {
    const timeline = buildTimeline(input);
    return finishClip({kind: 'timeline', name: options.name ?? null, frameCount: input.length, ...timeline,
      loop: true, loopPolicy: 'cycle', mode: 'cycle', events: Object.freeze([]), keys: null,
      transitions: Object.freeze([])});
  }
  if (!input || typeof input !== 'object') throw new TypeError('A clip must be an object or a list of durations');
  if (input.schemaVersion !== undefined) return normalizeAnimation(input, options);
  const pingpongExpanded = options.pingpongExpanded ?? true;
  const snake = input.duration_ms !== undefined;
  let durations = snake ? input.duration_ms : input.durationsMs;
  const frames = snake ? input.frames : (input.frames ?? input.sourceIndices);
  if (Number.isInteger(durations) && Array.isArray(frames)) durations = frames.map(() => durations);
  const label = `Clip ${options.name ?? input.name ?? ''}`.trim();
  const timeline = buildTimeline(durations);
  const loopPolicy = input.loop_policy ?? input.loopPolicy ?? (input.loop === false ? 'oneshot' : 'cycle');
  if (!LOOP_POLICIES.has(loopPolicy)) throw new RangeError(`${label} has an unknown loop policy ${loopPolicy}`);
  const loop = typeof input.loop === 'boolean' ? input.loop : loopPolicy !== 'oneshot';
  const events = normalizeEvents(snake ? (input.events_ms ?? input.events) : input.events, timeline, label);
  const manifest = options.manifest ?? {};
  const stride = input.stride_world_units ?? input.strideWorldUnits;
  const cycles = input.cycles ?? 1;
  const loopDistance = isPositive(stride) ? (snake ? stride : stride * cycles) : undefined;
  return finishClip({
    kind: snake ? 'sprite-clip' : 'record',
    name: options.name ?? input.name ?? null,
    frameCount: timeline.durationsMs.length,
    ...timeline,
    loop, loopPolicy, mode: modeFor(loop, loopPolicy, pingpongExpanded),
    events: Object.freeze(events),
    impactMs: resolveImpact(input.impactMs ?? input.impact_ms, events),
    holdMs: input.holdMs ?? input.hold_ms,
    terminal: input.terminal,
    keys: input.keys ?? null,
    entryFrame: input.entry_frame ?? input.entryFrame ?? 0,
    // D13: the hints first; legacy transitions items count only when they name a target with `to`.
    transitions: Object.freeze(transitionHints(input.transition_hints ?? input.transitionHints
      ?? input.transitions)),
    hitstopTicks: input.hitstop_ticks ?? input.hitstopTicks ?? 0,
    loopDistance,
    strideWorldUnits: isPositive(stride) ? stride : undefined,
    cycles,
    stridePxPerFrame: input.stride_px_per_frame ?? input.stridePxPerFrame,
    cadenceMs: input.cadence_ms ?? input.cadenceMs,
    speedRef: input.speed_ref ?? input.speedRef,
    frames: Array.isArray(frames) ? Object.freeze([...frames]) : null,
    sampling: input.sampling ?? manifest.sampling ?? (manifest.pixel_art === true ? 'nearest' : undefined),
    pixelArt: input.pixelArt ?? manifest.pixel_art,
  });
}

/**
 * Normalize a packed-alpha transport record (animation.json packedAlpha or a
 * mobilePackedAlpha tier).
 *
 * width/height are the logical frame; halfWidth/halfHeight the physical size of
 * each encoded half (forge_av.packed_geometry: 403 -> 404). animation.json 2.0
 * has no halves: its width/height already are the (even) half size. A tier's
 * sourceWidth/sourceHeight is the size it was scaled down from. When the decoded
 * video size is given it must equal (2 * halfWidth, halfHeight).
 */
export function packedGeometry(meta, videoWidth, videoHeight) {
  if (!meta || meta.layout !== PACKED_LAYOUT) {
    throw new TypeError(`Unsupported packed alpha layout ${JSON.stringify(meta?.layout)}; expected ${PACKED_LAYOUT}`);
  }
  const width = meta.width, height = meta.height;
  const halfWidth = meta.halfWidth ?? width, halfHeight = meta.halfHeight ?? height;
  const positive = value => Number.isInteger(value) && value >= 1;
  if (![width, height, halfWidth, halfHeight].every(positive) || halfWidth < width || halfHeight < height) {
    throw new RangeError('Invalid packed alpha geometry: need integer width <= halfWidth and height <= halfHeight');
  }
  if (videoWidth !== undefined && (videoWidth !== 2 * halfWidth || videoHeight !== halfHeight)) {
    throw new RangeError(`Packed alpha video is ${videoWidth}x${videoHeight}; `
      + `the manifest needs ${2 * halfWidth}x${halfHeight}`);
  }
  const geometry = {
    layout: PACKED_LAYOUT, width, height, halfWidth, halfHeight,
    videoWidth: 2 * halfWidth, videoHeight: halfHeight,
    sourceWidth: positive(meta.sourceWidth) ? meta.sourceWidth : width,
    sourceHeight: positive(meta.sourceHeight) ? meta.sourceHeight : height,
  };
  if (typeof meta.file === 'string') geometry.file = meta.file;
  if (meta.fps !== undefined && meta.fps !== null) geometry.fps = parseFps(meta.fps);
  if (typeof meta.tier === 'string') geometry.tier = meta.tier;
  return Object.freeze(geometry);
}

/**
 * Read animation.json 2.0 or 3.0 into a normalized clip plus its geometry and
 * transports. 2.0 has no per-frame durations: they are split exactly from
 * frameCount / fps with splitDurations. 3.0 keeps every 2.0 key and adds
 * durationsMs, loopPolicy, events, impactMs, holdMs, cycles, strideWorldUnits,
 * packedAlpha halves and mobilePackedAlpha tiers.
 */
export function normalizeAnimation(manifest, options = {}) {
  if (NORMALIZED.has(manifest)) return manifest;
  const version = manifest?.schemaVersion;
  if (version !== '2.0' && version !== '3.0') {
    throw new TypeError(`Unsupported animation.json schemaVersion ${JSON.stringify(version)}; expected "2.0" or "3.0"`);
  }
  const frameCount = manifest.frameCount;
  if (!Number.isInteger(frameCount) || frameCount < 1) throw new RangeError('frameCount must be a positive integer');
  const fps = parseFps(manifest.fps);
  let durations = manifest.durationsMs;
  if (version === '2.0' || !Array.isArray(durations)) {
    durations = splitDurations(Math.max(frameCount, Math.round(frameCount * 1000 / fps)), frameCount);
  }
  if (durations.length !== frameCount) {
    throw new RangeError(`durationsMs has ${durations.length} entries for ${frameCount} frames`);
  }
  const timeline = buildTimeline(durations);
  const loop = manifest.loop !== false;
  const loopPolicy = manifest.loopPolicy ?? (loop ? 'cycle' : 'oneshot');
  if (!LOOP_POLICIES.has(loopPolicy)) throw new RangeError(`Unknown loopPolicy ${loopPolicy}`);
  const label = `animation ${manifest.name ?? ''}`.trim();
  const rawEvents = version === '3.0' ? manifest.events
    : (manifest.hitEvents ?? []).filter(event => event && typeof event === 'object' && 'atMs' in event);
  const events = normalizeEvents(rawEvents, timeline, label);
  const stride = manifest.strideWorldUnits, cycles = isPositive(manifest.cycles) ? manifest.cycles : 1;
  const sourceSize = pair(manifest.sourceSize, 'sourceSize', isPositive);
  const contentSize = manifest.contentSize ? pair(manifest.contentSize, 'contentSize', isPositive) : null;
  const tiers = Array.isArray(manifest.mobilePackedAlpha) ? manifest.mobilePackedAlpha.map(t => packedGeometry({
    layout: PACKED_LAYOUT, ...t})) : [];
  return finishClip({
    kind: `animation-${version}`,
    name: options.name ?? manifest.name ?? null,
    schemaVersion: version,
    frameCount,
    ...timeline,
    fps,
    loop, loopPolicy, mode: modeFor(loop, loopPolicy, options.pingpongExpanded ?? true),
    events: Object.freeze(events),
    impactMs: resolveImpact(manifest.impactMs, events),
    holdMs: manifest.holdMs,
    terminal: manifest.terminal,
    keys: manifest.keys ?? null,
    entryFrame: manifest.entryFrame ?? 0,
    transitions: Object.freeze(transitionHints(manifest.transitionHints ?? manifest.transitions)),
    hitstopTicks: manifest.hitstopTicks ?? 0,
    loopDistance: isPositive(stride) ? stride * cycles : undefined,
    strideWorldUnits: isPositive(stride) ? stride : undefined,
    cycles,
    cadenceMs: manifest.cadenceMs,
    speedRef: manifest.speedRef,
    frames: Array.isArray(manifest.sourceIndices) ? Object.freeze([...manifest.sourceIndices]) : null,
    sampling: manifest.sampling ?? (manifest.pixelArt === true ? 'nearest' : undefined),
    pixelArt: manifest.pixelArt,
    sourceSize,
    sourceAnchor: pair(manifest.sourceAnchor, 'sourceAnchor'),
    sourceRect: Array.isArray(manifest.sourceRect) ? [...manifest.sourceRect] : [0, 0, ...sourceSize],
    contentSize,
    encodedSize: manifest.encodedSize ? pair(manifest.encodedSize, 'encodedSize', isPositive) : null,
    encodedAnchor: manifest.encodedAnchor ? pair(manifest.encodedAnchor, 'encodedAnchor') : null,
    displayScale: isPositive(manifest.displayScale) ? manifest.displayScale : 1,
    registration: manifest.registration,
    bodyHeightPx: manifest.bodyHeightPx,
    shadow: manifest.shadow,
    poster: manifest.poster ?? null,
    fallback: manifest.fallback ?? null,
    webm: manifest.webm ?? null,
    packedAlpha: manifest.packedAlpha ? packedGeometry(manifest.packedAlpha) : null,
    mobilePackedAlpha: Object.freeze(tiers),
    budgetClass: manifest.budgetClass,
  });
}

/**
 * Choose the packed transport: a mobilePackedAlpha tier when `mobile` is true
 * (the tier named `tier`, else the one matching budgetClass, else the first),
 * otherwise packedAlpha. Returns null when the manifest has none.
 */
export function pickPackedTransport(animation, {mobile = false, tier} = {}) {
  const a = normalizeAnimation(animation);
  if (mobile && a.mobilePackedAlpha.length) {
    const wanted = tier ?? a.budgetClass;
    return a.mobilePackedAlpha.find(t => t.tier === wanted) ?? a.mobilePackedAlpha[0];
  }
  return a.packedAlpha ?? a.mobilePackedAlpha[0] ?? null;
}

/**
 * The rectangle [x, y, w, h] of a transport's drawable that maps onto
 * sourceRect: contentSize, scaled for a tier that was resized from its
 * sourceWidth/sourceHeight.
 */
export function drawableRegion(animation, geometry) {
  const a = normalizeAnimation(animation);
  const [cw, ch] = a.contentSize ?? [geometry.width, geometry.height];
  const sx = geometry.width / geometry.sourceWidth, sy = geometry.height / geometry.sourceHeight;
  return [0, 0, cw * sx, ch * sy];
}

/**
 * Where frame `frame` sits in the PNG fallback atlas of animation.json:
 * {page, file, x, y, width, height}, the content part of its cell.
 */
export function fallbackCell(animation, frame) {
  const a = normalizeAnimation(animation);
  const fallback = a.fallback;
  if (!fallback || !Array.isArray(fallback.pages)) throw new TypeError('animation.json has no PNG fallback');
  const page = fallback.pages.findIndex(p => frame >= p.firstFrame && frame < p.firstFrame + p.frameCount);
  if (page < 0) throw new RangeError(`Frame ${frame} is in no fallback page`);
  const record = fallback.pages[page], [cellWidth, cellHeight] = fallback.cellSize;
  const offset = frame - record.firstFrame;
  const [width, height] = a.contentSize ?? fallback.cellSize;
  return {page, file: record.file, x: (offset % record.columns) * cellWidth,
    y: Math.floor(offset / record.columns) * cellHeight, width, height};
}

function geometryOf(source) {
  if (source?.sourceAnchor) {
    const size = source.sourceSize;
    return {rect: source.sourceRect ?? [0, 0, size[0], size[1]], anchor: source.sourceAnchor};
  }
  if (source?.anchor_px && source?.frame_size) {
    return {rect: [0, 0, source.frame_size[0], source.frame_size[1]], anchor: source.anchor_px};
  }
  throw new TypeError('Geometry needs sourceRect/sourceAnchor (animation.json) or frame_size/anchor_px (clips)');
}

/**
 * Destination rectangle {x, y, width, height} that draws a frame with its anchor
 * (sourceAnchor, or anchor_px of sprite clips) at world point (x, y). With
 * snap, the corner lands on whole pixels; use an integer scale for pixel art.
 */
export function anchoredRect(source, x, y, scale = 1, {snap = false} = {}) {
  const {rect: [rx, ry, rw, rh], anchor: [ax, ay]} = geometryOf(source);
  const left = x + (rx - ax) * scale, top = y + (ry - ay) * scale;
  return snap
    ? {x: Math.round(left), y: Math.round(top), width: Math.round(rw * scale), height: Math.round(rh * scale)}
    : {x: left, y: top, width: rw * scale, height: rh * scale};
}

// --------------------------------------------------------------------------- walking

/** Gait phase in [0, 1) from distance travelled: one cycle per loopDistance world units. */
export function gaitPhase(distance, loopDistance, offset = 0) {
  if (!isNumber(distance) || !isPositive(loopDistance) || !isNumber(offset)) {
    throw new TypeError('gaitPhase needs a finite distance, a positive loopDistance and a finite offset');
  }
  return mod(distance / loopDistance + offset, 1);
}

/**
 * Frame of an evenly timed gait from distance travelled (a sprite-sheet walk):
 * {phase, frame}. `stride` is the travel of one full cycle of `frames` frames.
 */
export function gaitFrame(distance, stride, frames, offset = 0) {
  if (!Number.isInteger(frames) || frames < 1) throw new TypeError('frames must be a positive integer');
  const phase = gaitPhase(distance, stride, offset);
  return {phase, frame: Math.min(frames - 1, Math.floor(phase * frames))};
}

/** Offset that makes gaitPhase(distance, loopDistance, offset) equal targetPhase. */
export function phaseOffset(distance, loopDistance, targetPhase = 0) {
  return mod(targetPhase - distance / loopDistance, 1);
}

/** Phase at which frame `entryFrame` starts (nudged inside it), for entering a gait on that frame. */
export function entryPhase(clip, entryFrame = undefined) {
  const c = normalizeClip(clip);
  const frame = entryFrame ?? c.entryFrame ?? 0;
  if (!isIndex(frame) || frame >= c.frameCount) throw new RangeError(`Entry frame ${frame} is outside the clip`);
  return (c.edges[frame] + Math.min(1e-6, c.durationsMs[frame] / 2)) / c.totalMs;
}

/**
 * Distance-driven walk for a timed clip (video or sprite). The phase comes from
 * the distance actually travelled, never the wall clock, so a blocked actor
 * keeps its pose: feed the displacement after collision (TravelMeter).
 *
 * Returns null when the clip has no travel (no stride and no option), else
 * {phase, phaseMs, frame, loopDistance, wantedRate, rate, rateLimited, paused}.
 * For video: seek to phaseMs when it drifts and set playbackRate = rate; pause
 * while `paused` (speed below minRate). gaitFrame shares the same phase.
 *
 * options: {offset, loopDistance, stride (per gait cycle when the clip has none),
 * minRate (0.05), maxRate (4)}.
 */
export function walkPlayback(clip, distance, speed, options = {}) {
  const c = normalizeClip(clip);
  const loopDistance = options.loopDistance ?? c.loopDistance
    ?? (isPositive(options.stride) ? options.stride * (c.cycles ?? 1) : undefined);
  if (!isPositive(loopDistance)) return null;
  const phase = gaitPhase(distance, loopDistance, options.offset ?? 0);
  const phaseMs = phase * c.totalMs;
  const minRate = options.minRate ?? 0.05, maxRate = options.maxRate ?? 4;
  const wantedRate = Math.max(0, isNumber(speed) ? speed : 0) / loopDistance * c.totalMs / 1000;
  return {
    phase, phaseMs, frame: frameAt(c, phaseMs, 'cycle').frame, loopDistance, wantedRate,
    rate: clamp(wantedRate, minRate, maxRate), rateLimited: wantedRate > maxRate, paused: wantedRate < minRate,
  };
}

/**
 * Accumulates the distance actually travelled. Call moveTo with the position
 * after collision; teleport moves without adding travel. ySquash divides
 * screen-space y so a foreshortened world (HD-2D plates, ySquash 0.58) walks on
 * ground distance.
 */
export class TravelMeter {
  constructor(x = 0, y = 0, {ySquash = 1} = {}) {
    if (!isPositive(ySquash)) throw new RangeError('ySquash must be positive');
    this.x = x;
    this.y = y;
    this.ySquash = ySquash;
    this.distance = 0;
  }

  moveTo(x, y) {
    const step = Math.hypot(x - this.x, (y - this.y) / this.ySquash);
    this.x = x;
    this.y = y;
    this.distance += step;
    return step;
  }

  teleport(x, y) {
    this.x = x;
    this.y = y;
  }
}

// --------------------------------------------------------------------------- actions

/**
 * Time-warp an action clip onto gameplay hit times (multi-hit attacks).
 *
 * timing: {durationMs, impactTimesMs: [ascending gameplay hit times]}. Each hit
 * owns the span between the midpoints to its neighbours; before the hit the
 * clip plays 0 -> impactMs, after it impactMs -> the clip's last millisecond, so
 * the clip's impact frame lands exactly on every hit. The clip's impact comes
 * from impactMs, else its first impact/hit event, else 45% of its length
 * (reported as impactAssumed).
 *
 * Returns {timeMs, rate, hit, frame, impactMs, impactAssumed} or null when
 * elapsedMs is outside [0, timing.durationMs). rate is clip ms per gameplay ms.
 */
export function mapActionTime(clip, timing, elapsedMs) {
  const c = normalizeClip(clip);
  const hits = timing?.impactTimesMs, durationMs = timing?.durationMs;
  if (!Array.isArray(hits) || !hits.length || !hits.every(isNumber) || !isPositive(durationMs)) {
    throw new TypeError('timing needs durationMs and a non-empty impactTimesMs list');
  }
  for (let i = 1; i < hits.length; i++) if (hits[i] < hits[i - 1]) throw new RangeError('impactTimesMs must ascend');
  if (!isNumber(elapsedMs) || elapsedMs < 0 || elapsedMs >= durationMs) return null;
  const last = Math.max(0, c.totalMs - 1);
  const impactAssumed = !isNumber(c.impactMs);
  const impactMs = clamp(impactAssumed ? Math.round(c.totalMs * 0.45) : c.impactMs, 0, last);
  let i = 0;
  while (i + 1 < hits.length && elapsedMs >= (hits[i] + hits[i + 1]) / 2) i++;
  const start = i ? (hits[i - 1] + hits[i]) / 2 : 0;
  const end = i === hits.length - 1 ? durationMs : (hits[i] + hits[i + 1]) / 2;
  const before = elapsedMs < hits[i];
  const span = Math.max(1, before ? hits[i] - start : end - hits[i]);
  const sourceSpan = before ? impactMs : last - impactMs;
  const timeMs = clamp((before ? 0 : impactMs) + (elapsedMs - (before ? start : hits[i])) / span * sourceSpan, 0, last);
  return {timeMs, rate: sourceSpan / span, hit: i, frame: frameAt(c, timeMs, 'oneshot').frame, impactMs, impactAssumed};
}

function keyPosition(source, key) {
  const keys = Array.isArray(source) ? source : (source && 'keys' in source ? source.keys : source);
  if (typeof key === 'number') {
    if (Array.isArray(keys)) return keys[clamp(Math.trunc(key), 0, keys.length - 1)];
    return key;
  }
  const value = keys?.[key];
  if (!isIndex(value)) throw new RangeError(`Unknown key pose ${JSON.stringify(key)}`);
  return value;
}

/**
 * Frame position between two key poses: round(a + (b - a) * u) for u in [0, 1].
 * Keys are names from a clip's keys (wind_start, wind_peak, strike, recover,
 * end), indices into a key list, or plain frame positions. Stretch an action's
 * key poses over gameplay ticks instead of playing fixed frame times.
 */
export function clipK(source, k0, k1, u) {
  const a = keyPosition(source, k0), b = keyPosition(source, k1);
  return Math.round(a + (b - a) * clamp(isNumber(u) ? u : 0, 0, 1));
}

/** Ping-pong between two key poses with the given period (a breathing idle or a charge loop). */
export function clipLoop(source, k0, k1, t, period) {
  if (!isPositive(period) || !isNumber(t)) throw new TypeError('clipLoop needs a finite time and a positive period');
  const q = mod(t, period) / period;
  return clipK(source, k0, k1, q < 0.5 ? q * 2 : 2 - q * 2);
}

// --------------------------------------------------------------------------- fixed step

/**
 * Fixed-timestep driver with render interpolation.
 *
 * advance(dtMs) (or tick(nowMs), which measures dt itself) returns
 * {steps, dropped, alpha}: run the simulation `steps` times, then render
 * lerp(previous, current, alpha). Frames shorter than one step by at most
 * snapMs still step, so a 60 Hz display does not alternate 0 and 2 steps; the
 * debt is repaid on later frames, so simulated time tracks real time. dt is
 * capped at maxFrameMs and at most maxSteps run per frame; the rest of a long
 * stall is dropped (counted, never replayed), which prevents a spiral.
 */
export class FixedStepLoop {
  constructor({hz = TICK_HZ, maxSteps = 4, maxFrameMs = 250, snapMs = 2} = {}) {
    if (!isPositive(hz) || !Number.isInteger(maxSteps) || maxSteps < 1 || !isPositive(maxFrameMs)
        || !isNumber(snapMs) || snapMs < 0 || snapMs >= 1000 / hz) {
      throw new RangeError('FixedStepLoop needs hz > 0, maxSteps >= 1, maxFrameMs > 0 and 0 <= snapMs < one step');
    }
    this.hz = hz;
    this.stepMs = 1000 / hz;
    this.maxSteps = maxSteps;
    this.maxFrameMs = maxFrameMs;
    this.snapMs = snapMs;
    this.reset();
  }

  reset() {
    this.accumulator = 0;
    this.lastTime = null;
    this.totalSteps = 0;
    this.totalDropped = 0;
  }

  advance(dtMs) {
    this.accumulator += clamp(isNumber(dtMs) ? dtMs : 0, 0, this.maxFrameMs);
    let steps = 0, dropped = 0;
    while (steps < this.maxSteps && this.accumulator + this.snapMs >= this.stepMs) {
      this.accumulator -= this.stepMs;
      steps++;
    }
    if (this.accumulator >= this.stepMs) {
      dropped = Math.floor(this.accumulator / this.stepMs);
      this.accumulator -= dropped * this.stepMs;
    }
    this.totalSteps += steps;
    this.totalDropped += dropped;
    return {steps, dropped, alpha: clamp(this.accumulator / this.stepMs, 0, 1)};
  }

  tick(nowMs) {
    const dt = this.lastTime === null ? 0 : nowMs - this.lastTime;
    this.lastTime = nowMs;
    return this.advance(dt);
  }

  /** tick(nowMs), calling step(stepMs, index) once per simulation step. */
  run(nowMs, step) {
    const result = this.tick(nowMs);
    for (let i = 0; i < result.steps; i++) step(this.stepMs, i);
    return result;
  }
}

// --------------------------------------------------------------------------- hit-stop

/**
 * Two clocks per entity (or per scene): the world clock always advances (shake,
 * particles, UI), the action clock freezes during hit-stop, so the strike pose
 * holds on screen. hitStop(n) freezes the next n ticks; overlapping requests
 * keep the longest remaining freeze and never stack. Call tick() once per
 * fixed step; it returns true when the action clock advanced.
 */
export class HitStopClock {
  constructor({hz = TICK_HZ} = {}) {
    if (!isPositive(hz)) throw new RangeError('hz must be positive');
    this.hz = hz;
    this.reset();
  }

  reset() {
    this.worldTicks = 0;
    this.actionTicks = 0;
    this.remaining = 0;
  }

  hitStop(ticks) {
    if (!isNumber(ticks) || ticks < 0) throw new RangeError('hit-stop ticks must be a non-negative number');
    this.remaining = Math.max(this.remaining, Math.floor(ticks));
    return this.remaining;
  }

  tick() {
    this.worldTicks++;
    if (this.remaining > 0) {
      this.remaining--;
      return false;
    }
    this.actionTicks++;
    return true;
  }

  get frozen() {
    return this.remaining > 0;
  }

  get worldMs() {
    return this.worldTicks * 1000 / this.hz;
  }

  get actionMs() {
    return this.actionTicks * 1000 / this.hz;
  }
}

// --------------------------------------------------------------------------- transitions

/** The clip's hint for switching to `to`: {to, entryFrame, dissolveMs, mode}, or null. */
export function transitionHint(clip, to) {
  return normalizeClip(clip).transitions.find(hint => hint.to === to) ?? null;
}

/** 4x4 ordered-dither (Bayer) ranks, row-major. */
export const BAYER4 = Object.freeze([0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5]);

/** Dither threshold of sprite pixel (x, y), in (0, 1). Use sprite-local pixels so the pattern moves with the sprite. */
export function ditherThreshold(x, y) {
  return (BAYER4[((y & 3) << 2) | (x & 3)] + 0.5) / 16;
}

/**
 * Ordered-dither dissolve: true while the outgoing pose is still drawn at
 * sprite pixel (x, y), for progress in [0, 1]. Each pixel switches once and
 * stays switched, so the dissolve never flickers; draw the incoming pose
 * underneath.
 */
export function ditherDissolve(progress, x, y) {
  return clamp(isNumber(progress) ? progress : 1, 0, 1) < ditherThreshold(x, y);
}

/** Progress in [0, 1] of a dissolve of dissolveMs after elapsedMs (1 when there is no dissolve). */
export function dissolveProgress(elapsedMs, dissolveMs) {
  if (!isPositive(dissolveMs)) return 1;
  return clamp(elapsedMs / dissolveMs, 0, 1);
}

/**
 * CPU dither dissolve of two straight-alpha RGBA frames of one size (ImageData
 * data or any byte array): the outgoing frame's opaque pixels stay where
 * ditherDissolve keeps them, the incoming frame shows everywhere else.
 */
export function ditherDissolvePixels(from, to, width, height, progress,
  out = new Uint8ClampedArray(width * height * 4)) {
  const size = width * height * 4;
  if (from.length < size || to.length < size || out.length < size) throw new RangeError('Pixel arrays are too small');
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = (y * width + x) * 4;
      const source = from[i + 3] > 0 && ditherDissolve(progress, x, y) ? from : to;
      out[i] = source[i];
      out[i + 1] = source[i + 1];
      out[i + 2] = source[i + 2];
      out[i + 3] = source[i + 3];
    }
  }
  return out;
}

/**
 * Premultiplied cross-fade of two straight-alpha RGBA frames (transition mode
 * 'premultiplied'): mixing in premultiplied space never darkens edges or shows
 * the floor through a half-faded pair. Output is straight alpha.
 */
export function premultipliedMix(from, to, t, out = new Uint8ClampedArray(from.length)) {
  if (to.length !== from.length || out.length < from.length) throw new RangeError('Pixel arrays differ in size');
  const k = clamp(isNumber(t) ? t : 0, 0, 1);
  for (let i = 0; i < from.length; i += 4) {
    const af = from[i + 3] / 255, at = to[i + 3] / 255, a = af + (at - af) * k;
    for (let c = 0; c < 3; c++) {
      const premultiplied = from[i + c] * af + (to[i + c] * at - from[i + c] * af) * k;
      out[i + c] = a > 0 ? Math.round(premultiplied / a) : 0;
    }
    out[i + 3] = Math.round(a * 255);
  }
  return out;
}

// --------------------------------------------------------------------------- pixels

/**
 * Largest whole-number scale (at least `minimum`) that fits `size` into `available`;
 * pixel art stays crisp only at integer scales.
 */
export function integerScale(available, size, minimum = 1) {
  if (!isPositive(available) || !isPositive(size)) throw new TypeError('integerScale needs positive sizes');
  return Math.max(minimum, Math.floor(available / size + 1e-9));
}

/** Snap a coordinate to a grid of `step` (1 = whole screen pixels; 1 / scale = art pixels). */
export function snapToGrid(value, step = 1) {
  if (!isPositive(step)) throw new TypeError('step must be positive');
  return Math.round(value / step) * step;
}
