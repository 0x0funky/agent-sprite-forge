# B07-video-motion: loop selection (gait, idle, hover), stride and entry frame, action retiming, classified review candidates and v2 cuts

Branch `asf/B07-video-motion` (from `wip/asf-upgrade-20261005` @ 3f9252d). New tools:
[gait_loop.py](../skills/video2dsprite/scripts/gait_loop.py) (`select`, `measure-stride`; also the frame
selection v2 library), [retime.py](../skills/video2dsprite/scripts/retime.py), and the `select` verb plus
v2-aware `cut` in [animation_review.py](../skills/video2dsprite/scripts/animation_review.py). Reference:
[animation-review.md](../skills/video2dsprite/references/animation-review.md). Tests:
[test_gait_loop.py](../tests/test_gait_loop.py), [test_retime.py](../tests/test_retime.py),
[test_animation_review.py](../tests/test_animation_review.py); opt-in bench
[loop_bench.py](../tests/benchmarks/loop_bench.py).

Integration status (Phase 3, group video, branch `asf/int-g-video`): the review's B07 items are resolved: tick-row
`end` events at the clip edge package (D19, B08 side), uneven whole-tick durations package as video (D21, B08 side),
every CLI has the D27 catch-all, and D28-D30 are adopted. Resolved items below cite their decision.

## 1. CLIs

    python "<skill-dir>/scripts/gait_loop.py" select --frames-dir work/frames-clean --fps 24 --output-dir work/loop
    python "<skill-dir>/scripts/gait_loop.py" select --frames-dir work/frames-clean --fps 24 --kind idle --output-dir work/idle-loop
    python "<skill-dir>/scripts/gait_loop.py" select --frames-dir work/frames-clean --fps 24 --evaluate 64:79 --evaluate 57:88 --output-dir work/loop-check
    python "<skill-dir>/scripts/gait_loop.py" measure-stride --frames-dir work/frames-clean --fps 24 --selection work/loop/selection.json --rest-frame 0 --px-per-unit 32 --output-dir work/stride
    python "<skill-dir>/scripts/retime.py" --frames-dir work/frames-clean --fps 24 --kind attack --spans 12:36:3,36:40:2,40:51,52,56,64,70:89:2 --duration 950 --impact-source 44 --output-dir work/attack
    python "<skill-dir>/scripts/retime.py" --frames-dir work/fx-frames --fps 24 --kind fx --map 0.65s/1.65s@350/5.75s --duration 1400 --output-fps 40 --output-dir work/fx
    python "<skill-dir>/scripts/retime.py" --frames-dir work/frames-clean --fps 24 --kind attack --spans 0:15 --ticks 1,1,1,1,2,1,2,1,1,1,2,3,3,3,4 --impact-source 6 --event cancel@12t --output-dir work/attack-ticks
    python "<skill-dir>/scripts/retime.py" --frames-dir work/frames-clean --fps 24 --selection work/loop/selection.json --stride 1.65 --speed 3.65 --output-dir work/walk
    python "<skill-dir>/scripts/animation_review.py" select --frames-dir work/frames-clean --fps 24 --output-dir work/candidates
    python "<skill-dir>/scripts/animation_review.py" cut --frames-dir work/frames-clean --selection work/loop/selection.json --out-dir work/selected

- `--fps` is always the source frame rate of `--frames-dir` (a number or `N/D`). Frame numbers are 0-based
  positions in the sorted `*.png` list, the order `animation_review.py` already used; intervals are
  `[start, endExclusive)`.
- `gait_loop select`: `--kind gait|idle|hover` (default gait), `--state run|walk` (gait stride window 0.30-1.20 s
  or 0.50-1.60 s), `--policy auto|cycle|pingpong` (gait refuses pingpong), `--dedupe-cap` (default 1.2; 0 keeps
  every frame), `--drift-tolerance`, `--min-seconds`/`--max-seconds` (idle/hover loop length, 0.5-4 s),
  `--evaluate START:END` (repeatable), `--no-aids`. Writes `selection.json` (forge-frame-selection/v2),
  `loop-report.json` (with a QA envelope) and `aids/loop3x.gif`, `aids/seam.png`, `aids/onion.png`,
  `aids/timeline.png`. Summary keys: `output`, `selection`, `metadata`, `status`, `policy`, `start`,
  `endExclusive`, `frames`, `confidence`.
- `gait_loop measure-stride`: `--selection` (v1 or v2) or `--range A:B`; `--rest-frame K` or `--rest PNG`
  (same canvas); `--band-rows`, `--px-per-unit`, `--dedupe-cap`. Writes `stride.json`, and with `--selection` a
  copy of the selection with `stridePxPerFrame`, `entryFrame`, `cadenceMs` (+ `strideWorldUnits`, `speedRef`).
  Summary keys: `output`, `metadata`, `stridePxPerFrame`, `entryFrame`, `selection`.
- `retime.py`: frames from exactly one of `--selection`, `--spans`, `--range` (+ `--max-frames`), `--map`;
  timing from at most one of `--duration`, `--ticks` (`--tick-hz`, default 60), `--stride` + `--speed`
  (`--cycles`); `--impact-source`, `--hold-source`, `--event NAME@MS|NAME@Nt` (repeatable), `--policy`,
  `--kind` (required unless the selection records one). Writes `selection.json` and `retime-report.json`.
  Summary keys: `output`, `selection`, `metadata`, `frames`, `durationMs`, `policy`, `impactMs`.
- `animation_review select`: `--output-dir` (alias `--out-dir`), `--fps`, `--kind`, `--state`, `--min-cycle`,
  `--max-cycle`, `--limit`. Writes `selection.json` and `candidates.json`. `review` and v1 `cut` are unchanged;
  `cut --selection` now also takes v2 and writes a v2 selection rebased onto the cut folder.
- Every verb's `--help` is ASCII and works under cp1252 and cp950 (tested with `assert_cli_help`). Errors print
  `error: ...` to stderr and exit 1 (the existing `animation_review` verbs keep `SystemExit(1)`); outputs are
  staged with `forge_core.staged_output` and nothing is published on failure (no period, no valid window, a
  ping-pong walk, a reversed recovery, changed source frames, an existing output directory). Any other exception
  prints `error: internal error (<Type>: <message>)` instead of a traceback (forge_core.run_cli, per D27); usage
  errors exit 2 (D26). Selections are read with forge_core.read_json (BOM-tolerant, strict, per D28); QA envelopes
  record `tool.version` 0.4.0 (per D29). `animation_review review` and `cut` also print `metadata` (review.json or
  selection.json).
- Packaging (B08): a tick-row selection packages with its `end` event on the clip edge (per D19) and, for WebM and
  packed MP4, with each frame repeated for its ticks at `tickHz` (per D21); held frames merged by `select` repeat at
  the source fps. No holds-as-repeats option is needed here.

## 2. SKILL.md routing rows

video2dsprite:

| Need | Route |
|---|---|
| Pick a walk or run cycle from a clip | `scripts/gait_loop.py select`; play `aids/loop3x.gif`, then package with the selection |
| Breathing, idle sway or hover loop | `scripts/gait_loop.py select --kind idle` (or `hover`); pingpong only when nothing closes |
| See why candidate intervals are valid or not | `scripts/animation_review.py select`; read `candidates.json` |
| Time an attack, cast or FX: spans, impact, hold, ticks | `scripts/retime.py`; then package with its selection |
| Walk cadence, stride and a rest-like entry frame | `scripts/gait_loop.py measure-stride`; `scripts/retime.py --stride --speed` |
| Copy chosen frames byte for byte (holds, played order) | `scripts/animation_review.py cut --selection` |

Notes to paste: "Choose cycles with `gait_loop select`, never by eye alone, then confirm them by eye. Walks
never ping-pong; attacks never recover by playing frames backwards; retime actions to their impact and hold.
Walks ship cadence and stride."

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `gait_loop.py select` | Finds a gait cycle (harmonic period, half-period guard, unusable frames) or an idle/hover loop (closure, pingpong fallback), drift-aware, with review aids | `tests/test_gait_loop.py` (8 prototype runners, idle/hover/push-in/held frames); `tests/benchmarks/loop_bench.py` (opt-in report v2 clip) |
| `gait_loop.py measure-stride` | Stride per frame from the planted foot and the most rest-like entry frame | `tests/test_gait_loop.py` (4 px/frame treadmill) |
| `retime.py` | Spans, impact/hold, three-key time map, 60 Hz tick rows, cadence; integer ms; loop-policy rules | `tests/test_retime.py` |
| `animation_review.py select` / `cut` | Classified candidate intervals with reasons; byte-preserved cuts of v1 and v2 selections | `tests/test_animation_review.py` |

## 4. CHANGELOG entries

- Added: `gait_loop.py select` for walk/run cycles: harmonic least-squares period, half-period guard, per-frame
  alternation (frames below 1.10 are unusable), unified seam score, confidence, `forge-frame-selection/v2` with
  0-based indices, file names and hashes, a `forge_core` seam report, and review aids (3x loop GIF, seam close-up,
  onion skin, timeline) (B07-T1; report v2 P0-4).
- Added: `--kind idle|hover`: pose + 0.5 x velocity closure with non-maximum suppression and a pingpong fallback;
  near-duplicate holds dropped with exact timing (`--dedupe-cap`); a drift-aware usable range that ends loops
  before a push-in (B07-T2).
- Added: `animation_review.py select`: candidate intervals classified as valid 1-cycle, valid 2-cycle or rejected
  with reasons; `cut` accepts frame selection v1 and v2 (B07-T3; report v2 P0-4).
- Added: `retime.py`: spans, `--impact-source`/`--hold-source`, three-key `--map`, `--ticks` rows with
  in/hit/cancel/end events, `--policy` rules (walks never ping-pong, recoveries never reverse),
  cadence = 1000 x stride / speed, integer durations, frame selection v2 and a suggested contact frame (B07-T4).
- Added: `gait_loop.py measure-stride`: stride per frame from ground-band matching and the entry frame by
  stance-aligned silhouette XOR against the rest pose (B07-T5).
- Changed: `references/animation-review.md` covers classified candidates, the loop policy table, the early calm
  span, retiming and stride fields (B07-T6). `animation_review.py` calls `utf8_stdio()` and prints ASCII errors.
- Changed: fileRefs in selections and reports carry `bytes` (forge_core.file_ref, integration D30); `tool.version`
  is 0.4.0 (D29); unexpected errors read `error: internal error (...)` (D27).
- BREAKING: none. `review`, v1 `cut` and frame selection v1 behave as before.
- Fixed: report v2 P0-4: six of the eight old helper candidates on the report clip were invalid; they are now
  listed as rejected with their reasons, and an 8-frame single step is rejected even when its seam looks normal.

## 5. Schema change requests

Resolved: integration S1 applied this section to shared/schemas/video.schema.json (commit f3d7eb2), including
`videoEvent.tick` and the end-edge rule (per D19). Kept for reference:

Add to `/$defs/frame_selection_v2/properties` (producers: gait_loop select, measure-stride, retime,
animation_review select and cut; consumers: B08 package `--selection`, B09 runtime, B02 clip conversion):

```json
"sourceFiles": {"type": "array", "items": {"type": "string", "minLength": 1},
                "description": "File names of the source frames in [start, endExclusive), parallel to sourceHashes."},
"kind": {"enum": ["gait", "walk", "run", "idle", "hover", "attack", "cast", "hurt", "guard", "victory", "defeat", "fx", "other"]},
"cycles": {"type": "integer", "minimum": 1},
"confidence": {"type": "object", "properties": {"label": {"enum": ["high", "medium", "low"]},
                                                 "overall": {"type": "number", "minimum": 0, "maximum": 1}}},
"seam": {"$ref": "common.schema.json#/$defs/seamReport"},
"ticks": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1},
          "description": "Ticks per sourceIndex at tickHz; durations_ms are the drift-free tick edges in ms."},
"tickHz": {"type": "integer", "minimum": 1, "default": 60},
"speedRef": {"type": "number", "exclusiveMinimum": 0},
"stridePxPerFrame": {"type": "number", "exclusiveMinimum": 0},
"entryFrame": {"type": "integer", "minimum": 0, "description": "Position in sourceIndices to start from rest."},
"suggestedContact": {"type": "object", "required": ["outputFrame", "sourceIndex", "atMs", "method"],
                     "properties": {"outputFrame": {"type": "integer", "minimum": 0},
                                    "sourceIndex": {"type": "integer", "minimum": 0},
                                    "atMs": {"type": "integer", "minimum": 0}, "score": {"type": "number"},
                                    "method": {"type": "string", "minLength": 1}}},
"origin": {"type": "object", "description": "Set by animation_review cut: the selection this cut folder came from.",
           "properties": {"sourceDirectory": {"type": "string", "minLength": 1},
                          "start": {"type": "integer", "minimum": 0}, "endExclusive": {"type": "integer", "minimum": 1},
                          "sourceIndices": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                          "selection": {"$ref": "common.schema.json#/$defs/fileRef"},
                          "transforms": {"type": "string"}}}
```

Add to `/$defs/videoEvent/properties`: `"tick": {"type": "integer", "minimum": 0}` (retime writes it in tick mode).

Semantics for the `frame_selection_v2` description (no rule changes):

- `sourceHashes` and `sourceFiles` cover every frame of `[start, endExclusive)`, as in v1. The range may be wider
  than the played `sourceIndices` (a loop whose last frames were held duplicates keeps its window).
- `fps` is the source frame rate the indices were sampled at; `durations_ms` are the playback timing.
- `loopPolicy: pingpong` lists the forward frames only; players and packagers mirror them without duplicating
  the end frames (`indices + indices[-2:0:-1]`).
- `sourceDirectory` (v2) is relative to the selection file, or only the directory name when no relative path
  exists (another drive); readers then rely on the per-frame hashes.

The report documents `video2dsprite.loop_report.v1`, `video2dsprite.stride_report.v1`,
`video2dsprite.retime_report.v1` and `forge-animation-select/v1` are tool reports, not contracts; each embeds a
common `qaEnvelope` (validated in the tests). Promote them to `$defs` only if a consumer appears.

## 6. Shared-helper promotion requests

All in `skills/video2dsprite/scripts`; none is needed by another skill yet.

- Resolved (per D30): `gait_loop.round_half_up` is forge_core.round_half_up; `gait_loop.file_ref` and
  `directory_ref` are forge_core.file_ref and manifest_path (fileRefs now carry `bytes`).
- `gait_loop.kept_durations` (gait_loop.py:162) and `retime.scaled_edges` (retime.py:144): integer durations for
  weighted frames that sum exactly; a generalisation of `forge_core.frame_durations(total_ms, n)` as
  `frame_durations(total_ms, n, *, weights=None)`. Tests: `test_parse_fps_and_durations`,
  `test_durations_sum_to_duration`, `test_cadence_452ms_for_1_65_over_3_65`.
- `gait_loop.pairwise_l1(features)` (gait_loop.py:444): scipy `pdist` with a numpy fallback honouring
  `FORGE_CORE_NO_SCIPY`. Tests: `test_pairwise_l1_numpy_fallback_matches_scipy`.
- `gait_loop.qa_envelope(...)` (gait_loop.py:1713): builds a common `qaEnvelope` without `createdAt`; other
  modules write the same shape.
- `gait_loop.read_selection` / `verify_selection` / `build_selection` (gait_loop.py:1260-1366): the frame
  selection v1/v2 reader, binding check and writer. B08 can import them from its sibling `gait_loop.py` (same
  skill) instead of re-implementing them; they need no promotion.

## 7. Cross-module links that Z must add

- video2dsprite SKILL.md: replace the "Recurrence suggestions require visual phase/contact review" paragraph with
  the routing rows above and link references/animation-review.md for loops, retiming and stride.
- references/pipeline.md (B08): resolved for `--selection`, durations, pingpong baking, the end edge (D19) and tick
  expansion (D21). Still open for B08: `entryFrame` is not copied into animation.json 3.0 yet (`impactMs`, `holdMs`,
  `events`, `cadenceMs`, `strideWorldUnits`, `speedRef`, `cycles` are).
- B09 forge-runtime.mjs: `walkPlayback`/`gaitFrame` take `strideWorldUnits` (one cycle per stride) or
  `stridePxPerFrame`, `cadenceMs` and `entryFrame`; `mapActionTime` reads `impactMs` and the events.
- B02 build_animation_clips v2: a selection maps to clips_input as `frames` = sourceIndices (after a cut: 0..n-1),
  `duration_ms` = durations_ms (or `ticks`/`tick_hz`), `loop_policy`, `events[].at` = event `frame`,
  `entry_frame`, `stride_px_per_frame`, `cadence_ms`, `speed_ref`.
- generate2dsprite references/character-animation.md (B02) already points to animation_review for constant-rate
  frame sequences; add gait_loop select for cycles.
- CONTRIBUTING / CI (Z-T6): the opt-in bench is `python -m pytest tests/benchmarks/loop_bench.py -m bench` with
  `FORGE_BENCH_LOOP_FRAMES` set to the keyed frames of the report v2 clip.

## 8. Known limitations and what is not proven

- Bench result on the report v2 clip (`outputs/fresh-agent-20261005/grok-native/forge-process/frames-clean`,
  145 frames, 960x960, 24 fps), Windows, under load from other agents: period 15.64 frames, verdict full-stride,
  recommended 82-97 (16 frames, start 82), confidence high (0.96), frames 0-25 unusable and 26 usable, the six
  invalid helper candidates (12-22, 25-42, 75-82, 60-73, 95-117, 30-54) rejected with reasons, 79-94 valid
  1-cycle and 53-83 valid 2-cycle, 14.9-15.3 s for `select` including aids (prototype: 33 s without aids).
- Thresholds (stride windows, alternation 1.10, seam 1.0 step, wrap 0.5-2x, drift note 1.5 %) come from one real
  clip and eight synthetic runners. The idle closure limit (1.0 step), the drift tolerances (3 %, gait 5 %), the
  2 s drift median and the hold rule were set on synthetic clips only. No real idle, hover, push-in or 12 fps clip
  was measured; measure-stride and the contact suggestion are proven on synthetic clips only.
- On 12 fps content held at 24 fps a few frames can be marked unusable spuriously (parity of the duplicated
  frames in the cadence tests); the recommended loop is still valid. Sub-pixel registration did not remove it.
- Seam metrics: `seam` in the selection is `forge_core.seam_report` on full-resolution source frames inside the
  union of their visible pixels; the decoded-file seam is B08's gate. Window ranking uses the feature-space
  context ratio of the prototype.
- GIF aids quantise delays to 10 ms (running sum tracks durations_ms) and clamp to at least 20 ms.
- Run only on Windows 11, Python 3.13, numpy 2.5, Pillow 12.3, scipy 1.18. Python 3.10 / Pillow 10.1 / Linux /
  macOS not run; the code avoids newer APIs. Review-aid PNG/GIF bytes are deterministic per Pillow build.
- Test cost: test_gait_loop.py takes about 40 s here (the eight prototype runners and four idle clips are the
  acceptance cases); STD went from 674 to 731 passed tests.
- Deviations from the plan:
  - retime's `--fps` is the source rate everywhere (as in the other tools); the three-key map's output rate is
    `--output-fps` (the plan wrote "--map ... with --duration and --fps").
  - "Recoveries never reverse" applies to `--kind attack|cast` (pingpong refused; no backwards step after the
    impact, or anywhere without one). Reactions (`hurt` and others) may reverse, as the owner's Dusk hurt clips do;
    the Dusk handoff rule names attacks.
  - Hold detection keeps pixelate.py's cap and 0.4 relative rules but drops its absolute 0.35 floor, which would
    mark every frame of a slow idle as a hold.
  - Root drift inside a loop lowers confidence and adds a note instead of rejecting the window (the prototype
    only penalised it too); a drifting runner still gets a loop with a "register the clip" note.
  - Speed: the analysis box-filters crops from premultiplied 8-bit copies kept from the first decode (no second
    decode) and never upsamples the legs crop; every acceptance result is unchanged.
  - Frames without cadence evidence at the clip edges inherit the nearest measured frame's status, which makes
    frames 0-1 unusable on the report clip (the prototype left them unknown).
  - The bench reads `FORGE_BENCH_LOOP_FRAMES` (keyed frames; `FORGE_BENCH_CLIP` is used when it is a frames
    directory): the loop tools need keyed RGBA frames, not the source video.
  - `animation_review select` also lists gait_loop's best windows next to the recurrence candidates.
