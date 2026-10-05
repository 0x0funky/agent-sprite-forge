# FX language: code-drawn game effects

Use `scripts/fx_build.py` for slashes, sparks, impact rings, hit flashes, dust and projectile loops: the strongest code-art category, because timing, alpha, palette and resolution are all exact. Claude writes one `codeart2d.fx.v1` JSON spec; the script bakes frames with an exact palette, a hit event on the impact frame, a clips manifest and, on request, the fx.v1 runtime module that draws the same effect live on a canvas (see [fx-runtime-contract.md](fx-runtime-contract.md)). Code-drawn, no image model: say so when you deliver.

Realistic fire, smoke and water belong to the video route. The worked example is `examples/slash.fx.json` (slash, impact, dust and orb effects).

## Commands

```text
python "<skill-dir>/scripts/fx_build.py" --spec slash.fx.json --output-dir out/fx-slash-v1 --route pixel --build-clips --export-runtime --strict-qc
python "<skill-dir>/scripts/fx_build.py" --spec slash.fx.json --output-dir out/fx-hd --route vector --zoom 4 --effects slash,impact
node "<skill-dir>/scripts/fx_verify.mjs" out/fx-slash-v1/fx-runtime.mjs --report out/fx-slash-v1/fx-verify-again.json
```

In Claude Code `<skill-dir>` is `${CLAUDE_SKILL_DIR}`. On success one JSON line names the output folder, `codeart-meta.json`, `fx-report.json` and the runtime module. Errors print `error: ...` and exit 1 (an unexpected failure too, as one `error: internal error (...)` line); a wrong argument is a usage error with exit 2. `--output-dir` must not exist, and `--strict-qc` publishes nothing when a check fails; without it a failing result is published for inspection and the script still exits 1, with `error: published with QA status fail: <check ids> (see fx-report.json)` (exit 0 means pass or warn). clips.json is `animation_clips.v2` by default, so the hit events reach the compiled clips; `routes`, when given, is a list of route names.

| Flag | Meaning |
|---|---|
| `--route pixel` (default) | Exact palette, alpha 0 or 255: `codeart_core.pixel_finish` per run of same-coloured shapes, optional 1 px outline. |
| `--route vector` | The same shapes anti-aliased at an integer `--zoom` (default 4). |
| `--effects a,b` | Bake only these effects. |
| `--build-clips` | Run the sibling `generate2dsprite/scripts/build_animation_clips.py` by path into `compiled-clips/`. |
| `--export-runtime` | Also write `fx-runtime.mjs` and, when node is on PATH, check it with fx_verify.mjs into `fx-verify.json` (`--no-verify` skips that). |
| `--margin 1`, `--seam-range 0.8,1.25` | Transparent margin in canvas pixels; loop seam band (warning only for FX). |
| `--clips-schema v1\|v2`, `--ss`, `--coverage`, `--backend` | As in rig_animate.py. |

## The spec

```json
{"schema": "codeart2d.fx.v1", "canvas": [64, 64], "origin": [32, 34],
 "palette": {"white": "#ffffff", "gold": "#ffcd75", "ember": "#ef7d57", "red": "#b13e53", "ink": "#1a1c2c"},
 "effects": [{"id": "slash", "durationMs": 400, "impactMs": 150, "seed": 7,
   "ramps": {"blade": ["white", "gold", "ember", "red"]},
   "primitives": [
     {"type": "slash", "ramp": "blade", "center": [30, 32], "radius": 18, "from": -150, "to": 30, "width": 8, "trail": 0.9},
     {"type": "sparks", "ramp": "blade", "origin": [45.5, 41], "count": 8, "angle": 20, "spread": 130, "speed": 55}]}]}
```

- `canvas` [w, h] (default [48, 48]) is shared by every effect of the spec, so one clips manifest holds them all. `origin` (default: the canvas centre) is the effect's anchor: `anchor_px` in clips.json and the point the runtime places at `env.x, env.y`.
- `palette` is required: a `{name: "#rrggbb"}` object, a list, or a palette file path. Every colour must be opaque and every shape colour must come from it, by name or exact hex.
- Effect fields: `id` (ASCII; names the frames, so ids must differ in more than letter case: `slash` and `SLASH` are refused), `durationMs`, `impactMs` (the gameplay hit, below the duration for one-shots), `seed` (0 to 2^31-1), `frameMs` (default 50 = 3 ticks at 60 Hz), `loop` (default false), `outline` (a palette colour for a 1 px dark outline, recommended for busy backgrounds), `ramps` (`{name: [colours]}`, brightest first), `primitives`, and `events` (`[{atMs, name}]`, extra clip events).
- Primitive fields: `type`, then `ramp` (a ramp name) or `colors` (a colour list, brightest first); without either, the effect's first ramp or else the palette order. Unknown parameters are refused. Times accept `"impact"`.

## The six presets

| Type | Draws | Parameters (defaults; S = the smaller canvas side) |
|---|---|---|
| `slash` | A crescent swept along an arc; its head reaches `to` exactly at `endMs`, so **the arc ends on the hit tick**, then the tail catches up and the blade thins. Up to three nested layers: darkest outside, brightest core. | `center` (origin), `radius` 0.35S, `from` -150, `to` 30 (degrees, clockwise on screen), `width` 0.11S (min 2), `squash` 1 (ellipse y scale), `trail` 0.5 (how far the tail lags, as a fraction of the sweep), `startMs` 0, `endMs` impactMs, `fadeMs` (the rest). |
| `sparks` | A seeded burst of streaks that fly out, shorten and cool along the ramp. | `origin`, `atMs` impact, `count` 10, `angle` -90, `spread` 360, `speed` 1.6S px/s, `lifeMs` 250, `length` 0.1S, `width` 1.5, `gravity` 0 px/s^2. |
| `ring` | An expanding ellipse ring that thins and cools. | `origin`, `atMs` impact, `lifeMs` 250, `radius` [0.08S, 0.42S], `width` [0.08S, 1], `squash` 1 (0.5 for a ground ring). |
| `flash` | A star burst with a hot core that shrinks fast. | `origin`, `atMs` impact, `lifeMs` 120, `radius` 0.12S, `rays` 4, `rayLength` 0.3S, `rotation` 0. |
| `dust` | Staggered puffs that rise, drift outward, grow and darken. | `origin`, `atMs` 0, `count` 6, `spread` 0.4S, `rise` 0.2S, `drift` 0.15S, `radius` [0.04S, 0.1S], `lifeMs` duration. |
| `projectile` | An in-flight loop: pulsing core, glow, tapered trail and orbiting sparkles. The game moves the projectile; spawn an impact effect when it hits. | `origin`, `angle` 0 (travel direction), `radius` 0.12S, `trail` 0.4S, `orbiters` 3, `periodMs` duration (a looping effect needs a whole number of periods). |

Particles use a deterministic 32-bit hash of the seed, the primitive index and the particle index; the runtime computes the same hash bit for bit, so the same seed always gives the same burst. Dust puffs start in a fixed stagger (the first at `atMs`, so the effect is visible on its hit), and the projectile pulses with a triangle wave whose constant speed keeps loop steps even.

## Timing

- Frames are about `frameMs` long and are cut so one frame starts exactly at `impactMs`: the **hit frame**. clips.json gives that frame the event `{"at": hit, "name": "hit", "data": {"impactMs": ...}}` and the frames before it add up to impactMs, so a builder's `events_ms` puts the hit on the gameplay hit.
- Each frame shows the effect at the middle of its time on screen. The frame before the hit shows the arc still sweeping; the hit frame shows it complete.
- Fully transparent frames at the end are dropped and their time is merged into the last kept frame (the clip still lasts `durationMs`; build_animation_clips refuses empty frames). An empty frame before that is an error: start a primitive earlier.
- For 60 Hz games, `impactMs` on the 3-tick grid (multiples of 50 ms) lands the hit on a tick; `fx-report.json` records `hit_on_60hz_tick` for each effect.
- Looping effects have no hit frame; their seam is reported as a warning only, because smooth pixel loops with varying speed can exceed the band without a visible pop.

## Visual language

- **Read in one frame.** Each effect needs one clear silhouette at game scale: a crescent, a burst, a ring. Check the review sheet at 1x before adding detail.
- **The hit lands on the impact frame.** Anticipation (the sweep) happens before `impactMs`; the flash, sparks and ring start on it. Spawn runtime effects at `hitAt - impactMs`.
- **Hot to cold.** Ramps run brightest first: young particles are white or gold, old ones ember and red, then gone. Never fade with alpha in pixel art; shrink and cool instead.
- **Contrast.** Effects drawn over busy art read better with a 1 px dark `outline`; at phone scale keep strokes at least 2 device px (the runtime verifier warns below 1).
- **Stay local and short.** Keep the key read near the action, never cover the screen with an opaque fill for more than 10 frames at 60 Hz, and keep everything inside the canvas (the margin check fails otherwise).

## Outputs and QA

| Path | Content |
|---|---|
| `frames/<effect>-NN.png` | 8-bit RGBA frames; identical frames stored once. |
| `clips.json` | One clip per effect: `duration_ms`, `loop`, `loop_policy`, `role: fx`, `events` with the hit; `anchor_px` = origin; `sampling`, `pixel_art`, `art_source: code`. |
| `fx-report.json` | `codeart2d.fx_report.v1`: per effect the frames (start, duration, sample time, QA metrics, margin), durations, hit frame, dropped tail, events, loop seam; the slash arc progress per frame; the build and runtime records; the QA envelope. |
| `codeart-meta.json` | `art_source: code`, disclosure, `spec_sha256`, renderer, palette, outputs and the QA envelope. |
| `review/<effect>.png` | Review sheet with onion row and palette swatches. |
| `fx-runtime.mjs`, `fx-verify.json` | The fx.v1 runtime and its verification report (`--export-runtime`). |

Checks: `partial_alpha` and `off_palette` 0 in every pixel frame; `outline_gaps` 0 for outlined effects; `l_corners` (warning above 10); `arc_on_hit` (every slash completes exactly on its hit frame); `seam:<effect>` (warning); `margins`; `timing` (each clip sums to durationMs); `build_clips`; `runtime_verify`. Not proven: readability on the real background and the feel of the hit in play.
