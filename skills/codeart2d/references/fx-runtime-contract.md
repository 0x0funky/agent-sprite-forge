# fx.v1 runtime contract

An fx.v1 runtime is one dependency-free ES module that draws code-drawn game effects on a Canvas 2D context at any time, deterministically. `fx_build.py --export-runtime` writes it as `fx-runtime.mjs` from a `codeart2d.fx.v1` spec by filling the data block of [runtime/fx-template.mjs](runtime/fx-template.mjs), so the runtime draws exactly the geometry the baked frames show. A hand-written runtime may implement the same contract; `scripts/fx_verify.mjs` checks either kind. See [fx-language.md](fx-language.md) for the spec and the presets.

Use baked frames for pixel-art games (exact palette, alpha 0 or 255, nearest sampling). Use the runtime when effects must follow gameplay timing at any frame rate, mirror per attacker, vary per hit, or draw at HD scale (canvas paths are anti-aliased).

## Exports

| Export | Meaning |
|---|---|
| `FX_SCHEMA` | `'codeart2d.fx.v1'`. |
| `canvas` | `[w, h]`: the effect box in effect pixels. Nothing is drawn outside it. |
| `origin` | `[x, y]`: the anchor inside the box, placed at `env.x, env.y`. |
| `effects` | Frozen `[{id, durationMs, impactMs, loop}]`. |
| `spawnAt(id, hitAtMs)` | `hitAtMs - impactMs`: when to start the effect so its impact lands on the hit. |
| `render(id, ctx, t, env)` | Draws effect `id` at `t` ms since spawn; returns true when it drew something. |
| `shapes(id, t, env)` | The shape list `render` draws, in effect pixels (for tools and tests). |

## Timing: spawnAt = hitAt - impactMs

`t` is milliseconds since the effect spawned, on the game's own clock. A one-shot draws for `0 <= t < durationMs` and nothing outside; a loop draws for every `t >= 0` (it wraps at `durationMs`).

The gameplay hit decides the timing. A slash with `impactMs` 150 whose blade connects at 1000 ms spawns at `spawnAt('slash', 1000)` = 850 ms and is drawn with `t = now - 850`: the arc completes exactly when the hit registers, and the sparks start then. If the hit moves (a combo speeds up, an attack is cancelled), recompute the spawn from the new hit time; never stretch `t`. During hit-stop, pause `t` with the rest of the game.

Baked clips follow the same rule: the frame that starts at `impactMs` is the hit frame and carries the `hit` event, so `events_ms` from build_animation_clips puts it on the hit.

## Placement: env

`render(id, ctx, t, {x, y, scale, facing, seed})`:

- `x, y`: where the effect's `origin` goes, in the context's current coordinates (usually the hit point or the projectile position).
- `scale` (default 1): effect pixels to context pixels, e.g. 3 in a game that draws 3x pixel art.
- `facing` (1 or -1): -1 mirrors the effect horizontally around its origin (a left-facing attacker).
- `seed` (integer, default 0): varies particle bursts per instance, deterministically. 0 draws the baked look.

The runtime leaves `ctx` exactly as it found it (one balanced save/restore around all drawing).

## Rules

A conforming runtime:

1. Uses no randomness and no clock: no `Math.random`, `Date`, `performance.now`, timers or `crypto` randomness. Particles come from a seeded integer hash.
2. Does no I/O and touches no globals: no `fetch`, workers, `document`, `window`, `globalThis` writes, dynamic `import()`, `eval` or `Function`.
3. Draws only paths with Canvas 2D: no `drawImage`, image data, text or `clearRect`.
4. Passes only finite numbers to the context, and never a negative radius.
5. Balances `save`/`restore` and leaves every context property and the transform unchanged.
6. Draws nothing before `t = 0`, nothing at or after `durationMs` for one-shots, and nothing outside its canvas box.
7. Is deterministic: the same `(id, t, env)` produces the same calls, whatever was drawn before, in a fresh module instance too.
8. Is visible at its impact: a one-shot draws something at `t = impactMs`.
9. Stays readable: no opaque fill over half the viewport for more than 10 ticks at 60 Hz; strokes of at least 1 device px (2 px or more reads better on phones); the key read near the anchor.

## Verifying

```text
node "<skill-dir>/scripts/fx_verify.mjs" out/fx-slash-v1/fx-runtime.mjs --report out/fx-slash-v1/fx-verify-2.json
```

Node 18 or newer, no packages. The verifier scans the source (comments ignored), imports the module twice with `Math.random`, `Date.now`, `performance.now`, `crypto.getRandomValues`, timers and `fetch` trapped, and draws every effect at every 60 Hz tick, at `impactMs - 1`, `impactMs`, `impactMs + 1`, at the end and outside the lifetime (negative, NaN, infinite and late times), for `env` scale 3 facing +1, facing -1 and scale 1 with a seed, into a recording 2D context. It prints one ASCII JSON line `{status, module, report, effects, failed, warned}` (non-ASCII characters in paths are `\uXXXX` escapes, as in the Python tools) and exits 1 when any check fails or the module cannot be checked. A wrong argument is a usage error: the usage line, `fx_verify.mjs: error: ...` and exit 2. `--report` writes a common QA envelope (never over an existing file) whose `tool.version` is the package version; `--scale`, `--width` and `--height` change the reference viewport (default 1280x720, scale 3). The script also runs when the skill folder is reached through a symlink or junction.

| Check | Fails when |
|---|---|
| `forbidden_apis` | The source names a forbidden API (rules 1-3). |
| `exports`, `timing`, `spawn_at` | An export is missing; ids, durations or impacts are invalid; `spawnAt` is not `hitAt - impactMs`. |
| `no_randomness_or_clocks`, `no_global_writes` | A trapped API was called; a global appeared. |
| `render_errors`, `finite_arguments` | `render` threw; a non-finite number reached the context. |
| `balanced_state`, `context_contract` | Save/restore or state is unbalanced; an unknown or forbidden context member was used. |
| `transparent_outside`, `within_box` | Something was drawn outside the lifetime or outside the canvas box (1 px plus the scale of tolerance). |
| `deterministic` | Two draws of the same sample differ (repeat, reversed order, fresh instance). |
| `visible_at_impact` | A one-shot draws nothing at `impactMs`. |
| `fullscreen_flash` | An opaque fill covers half the viewport for more than 10 consecutive ticks. |
| `thin_strokes`, `reach`, `op_budget` (warnings) | A stroke is under 1 device px; the impact is drawn more than 300 px from the anchor; more than 2000 paints in one call. |

Not proven: pixels (the recording context does not rasterize), frame time on real devices, and equality with the baked frames (the tests compare `shapes()` with fx_build.py's geometry).

## Embedding

```js
import { render, spawnAt, effects } from './fx-runtime.mjs';

const active = [];
function onHit(effectId, hitAtMs, x, y, facing, hitId) {
  active.push({ effectId, start: spawnAt(effectId, hitAtMs), x, y, facing, seed: hitId });
}
function drawEffects(ctx, nowMs) {
  for (const fx of active) render(fx.effectId, ctx, nowMs - fx.start, { x: fx.x, y: fx.y, scale: 3, facing: fx.facing, seed: fx.seed });
  const ends = Object.fromEntries(effects.map((e) => [e.id, e.durationMs]));
  for (let i = active.length - 1; i >= 0; i -= 1) if (nowMs - active[i].start >= ends[active[i].effectId]) active.splice(i, 1);
}
```

Register the effect when the attack starts: the attack's own timeline knows when its hit lands, so `spawnAt` is usually at or after the attack start and the sweep plays before the hit. When the game learns of a hit only as it happens, use an effect with `impactMs` 0 (an impact burst) rather than a sweep.
