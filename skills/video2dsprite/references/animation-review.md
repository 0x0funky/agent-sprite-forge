# Review frames, choose loops and retime actions

Five offline tools work on common-canvas RGBA PNGs (cleaned video frames or prepared
sheet frames). None of them generates, interpolates, aligns feet or alters source files.
Frame numbers are 0-based positions in the sorted `*.png` list, and every interval is
`[start, endExclusive)`: `82:98` is frames 82 to 97, 16 frames.

| Need | Tool |
|---|---|
| Scrub frames on light/dark/checker backgrounds | `animation_review.py review` |
| Pick a walk/run cycle, an idle or a hover loop | `gait_loop.py select` |
| See why candidate intervals are valid or not | `animation_review.py select` |
| Time an action: spans, impact, hold, ticks, cadence | `retime.py` |
| Stride per frame and a rest-like entry frame | `gait_loop.py measure-stride` |
| Copy the chosen frames byte for byte | `animation_review.py cut` |

Choose cycles with `gait_loop select`, never by eye alone, then confirm them by eye:
pixel metrics cannot certify foot contact, hand or prop identity, or the art. Every
selection stays `selected-needs-visual-review`.

Commands below run from your project root; `<skill-dir>` is this skill's folder
(`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs go to new folders in your project.

## Scrub the source

```bash
python "<skill-dir>/scripts/animation_review.py" review --frames-dir work/frames-clean --out-dir work/review --fps 24 --min-cycle 8 --max-cycle 36
```

Open `work/review/index.html`. Play at normal speed, then pause, step and scrub. Switch
backgrounds to see key spill. Set the ground annotation from the source art and compare
contact with intended flight. Its candidate intervals are raw recurrence hints: they can
be a half stride, a stride and a half, or a run of glitched frames. Use the classified
list below before trusting one.

## Choose a loop

```bash
python "<skill-dir>/scripts/gait_loop.py" select --frames-dir work/frames-clean --fps 24 --output-dir work/loop
python "<skill-dir>/scripts/gait_loop.py" select --frames-dir work/frames-clean --fps 24 --kind idle --output-dir work/idle-loop
```

`--kind gait` (default; `--state run` or `walk` sets the stride window) finds the stride
period by harmonic least squares and applies the half-period guard: a stride holds two
mirrored steps, so a loop of half the period repeats one leg. Frames whose cadence is
irregular, or whose legs stop alternating (mirror/same ratio below 1.10), are unusable;
on the report v2 clip that is frames 0-25, a leg-identity glitch. The loop with the
best unified seam score among valid one-stride windows is chosen.

`--kind idle` or `hover` scores each window by pose + 0.5 x velocity closure (the loop
restarts where the source would continue, moving the same way), keeps the best windows
after non-maximum suppression, and falls back to pingpong when nothing closes.

The output folder holds:

- `selection.json`: `forge-frame-selection/v2` with `sourceIndices`, `durations_ms`,
  `loopPolicy`, file names, hashes, the confidence and a `forge_core` seam report.
- `loop-report.json`: period, verdict, unusable frames, holds, the drift-free range,
  the best one- and two-stride windows and a QA envelope.
- `aids/loop3x.gif` (the loop three times at source timing, wrap marked),
  `aids/seam.png` (last, first and the true successor with a difference map),
  `aids/onion.png` (wrap onion skins) and `aids/timeline.png` (cadence evidence per frame).

`--evaluate START:END` (repeatable) classifies any window you are considering.

### Classified candidates

```bash
python "<skill-dir>/scripts/animation_review.py" select --frames-dir work/frames-clean --fps 24 --output-dir work/candidates --min-cycle 8 --max-cycle 36
```

`candidates.json` lists the review helper's recurrence candidates and gait_loop's best
windows as `valid-1-cycle`, `valid-2-cycle` (`valid-cycle` for idle/hover) or `rejected`
with every reason:

| Reason | Meaning |
|---|---|
| includes unusable frames | irregular cadence, or the legs do not alternate there |
| half stride | one step only: the loop would repeat one leg |
| partial stride | not a whole number of strides (for example 1.5) |
| more than two strides | ship one stride, or two when you want variation |
| seam error | repeating at this length is worse than an ordinary frame step |
| wrap step | a stall or snap at the seam |
| does not close | idle/hover: the wrap misses the next source pose |
| runs past the drift-free range | the window uses frames after the body drifted |

Notes are not rejections: root drift per loop (register the clip), uneven step timing
and dropped held duplicates.

## Loop policies

| Motion | Policy | Why |
|---|---|---|
| Walk, run | `cycle`, a full stride (both steps) | pingpong moonwalks: the feet slide backwards |
| Breathing, sway, hover bob | `cycle` when a window closes, else `pingpong` | reversal reads as natural for soft motion |
| Attack, cast | `oneshot`, recovery played forward | a reversed strike is not a recovery |
| Hurt, guard, victory, defeat | `oneshot` | a reaction may return to its stance by reversal |

Pingpong selections list the forward frames only; players mirror them without
duplicating the end frames (`indices + indices[-2:0:-1]`).

## Early calm span and drift

Image-to-video clips start at the approved still and tend to drift later: the camera
pushes in, the head turns to the viewer, the body slides. `select` tracks the body scale
(and, for idle, the ground line and centroid) with a 2 s running median and ends the
usable range when it drifts 3 % from the first half second (gait: scale 5 %, per
stride). Loops and the pingpong span stay inside that early calm span.
`--drift-tolerance` changes the limit.

## Held duplicates

Many generated clips hold each pose for two or three 24 fps frames (12 or 8 fps
content). A frame whose step from its predecessor is at most `--dedupe-cap` (default
1.2 on a 0-255 scale) and well below its neighbours' steps is a hold. Holds are dropped
from `sourceIndices`; their time goes to the frame they repeat, so the loop keeps its
exact length. `--dedupe-cap 0` keeps every frame. The durations stay whole source frames,
so a WebM or packed package repeats the held frame at the source fps again.

## Retime actions

```bash
python "<skill-dir>/scripts/retime.py" --frames-dir work/frames-clean --fps 24 --kind attack --spans 12:36:3,36:40:2,40:51,52,56,64,70:89:2 --duration 950 --impact-source 44 --output-dir work/attack
python "<skill-dir>/scripts/retime.py" --frames-dir work/fx-frames --fps 24 --kind fx --map 0.65s/1.65s@350/5.75s --duration 1400 --output-fps 40 --output-dir work/fx
python "<skill-dir>/scripts/retime.py" --frames-dir work/frames-clean --fps 24 --kind attack --spans 0:15 --ticks 1,1,1,1,2,1,2,1,1,1,2,3,3,3,4 --impact-source 6 --event cancel@12t --output-dir work/attack-ticks
python "<skill-dir>/scripts/retime.py" --frames-dir work/frames-clean --fps 24 --selection work/loop/selection.json --stride 1.65 --speed 3.65 --output-dir work/walk
```

- `--spans`: `a:b` (end exclusive), `a:b:step` (a negative step plays backwards), `k`,
  or `k*n` (frame k held n times). Compress a long hold, keep the strike frames dense.
- `--impact-source` / `--hold-source`: `impactMs` / `holdMs` start the output frame whose
  source frame is nearest (the earlier one on ties).
- `--map START/PEAK@MS/END` with `--duration` and `--output-fps`: a three-key time map;
  the frame that starts at MS shows the PEAK source frame. Keys are source frames, or
  seconds with an `s` suffix.
- `--ticks`: a total or one count per frame on a 60 Hz grid (`--tick-hz`). Writes `ticks`,
  `tickHz` and the `in`, `hit` and `end` events; add `cancel@12t` and others with `--event`.
  The `end` event sits on the clip's end edge (its `atMs` is the duration) and packages as
  the last frame. Tick rows give uneven durations: `engine_export.py package` keeps them in
  a PNG-only package and, for WebM and packed MP4, repeats each frame for its ticks at
  `tickHz` (`tickExpansion` in animation.json; see pipeline.md).
- `--stride` and `--speed` (walks): cadence = 1000 x stride / speed ms per cycle, for
  example 1.65 units at 3.65 units/s = 452 ms; writes `cadenceMs`, `strideWorldUnits` and
  `speedRef`.

Durations are integer ms that sum exactly to the clip length. Walks never ping-pong or
play backwards; attacks and casts never recover backwards after the impact. Each run
also suggests a contact frame (`suggestedContact`): the widest ground contact for a gait,
the largest reach for an action. It is a review hint, not a decision.

## Stride and entry frame

```bash
python "<skill-dir>/scripts/gait_loop.py" measure-stride --frames-dir work/frames-clean --fps 24 --selection work/loop/selection.json --rest-frame 0 --px-per-unit 32 --output-dir work/stride
```

The planted foot moves with the ground, so the alpha band just above the floor is
matched between consecutive frames; stride per frame is the median ground motion minus
the body's own drift (`stridePxPerFrame`, in source pixels). The entry frame is the loop
frame whose silhouette, aligned at the stance, differs least from the rest pose: start
the walk there when leaving idle. With `--selection`, a copy of the selection gains
`stridePxPerFrame`, `entryFrame`, `cadenceMs` and, with `--px-per-unit`,
`strideWorldUnits` and `speedRef`. Drive the walk phase by distance travelled: one full
cycle per stride length (`strideWorldUnits`, or `stridePxPerFrame` x the cycle's source
frames in source pixels), so the feet never slide when the speed changes.

## Export the frames

```bash
python "<skill-dir>/scripts/animation_review.py" cut --frames-dir work/frames-clean --selection work/loop/selection.json --out-dir work/selected
```

`cut` accepts v1 selections from the HTML reviewer and v2 selections from `select`,
`gait_loop` and `retime`. It verifies the directory binding and every frame hash, then
copies the frames byte for byte in played order (holds and reversals repeat the same
file) and writes a v2 selection rebased onto the cut folder, with the original indices
under `origin`. `cut --start 12 --end 28 --fps 24` still exports an interval without a
selection. Neither selection nor export turns `needs-visual-review` into an approval.

## Limits

The gait thresholds (stride windows, the 1.10 alternation ratio, seam and wrap limits)
were set on one real clip and eight synthetic runners; the idle closure limit and the
drift tolerances on synthetic clips only. Expect to override them on unusual footage, and
report what you changed. No RIFE dependency is required. If you interpolate separately,
disclose the derived frames, keep the originals, and inspect hands, silhouette,
pixel-grid stability and contacts: more samples cannot recover a missing authored pose.
