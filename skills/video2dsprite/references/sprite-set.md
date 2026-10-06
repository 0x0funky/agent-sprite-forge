# Sprite sets: one approved still in, a whole game-ready set out

`scripts/sprite_set.py` turns one approved master still into every animation a character needs
(idle, walk, run, attack, jump, hurt, and cast, guard, victory, defeat on request), one
image-to-video clip per action, with the game-opus55 take loop: numeric gates, automatic retakes
with prompt fixes, a contact sheet the agent must look at, and an explicit accept. It calls the
other video2dsprite tools by path; it never draws, keys or registers anything itself except its
gates and a stand-in finisher.

## Commands

Run each as one line from the project root. `<skill-dir>` is this skill's folder.

    python "<skill-dir>/scripts/sprite_set.py" plan --master art/hero/master.json --output-dir sets/hero
    python "<skill-dir>/scripts/sprite_set.py" run --plan sets/hero/set_plan.json
    python "<skill-dir>/scripts/sprite_set.py" review --plan sets/hero/set_plan.json
    python "<skill-dir>/scripts/sprite_set.py" accept --plan sets/hero/set_plan.json --action idle --take 1
    python "<skill-dir>/scripts/sprite_set.py" retake --plan sets/hero/set_plan.json --action attack --fix weapon-shape
    python "<skill-dir>/scripts/sprite_set.py" report --plan sets/hero/set_plan.json

| Verb | Flags | Writes |
|---|---|---|
| `plan` | `--master` (generate2dsprite.master.v1), `--output-dir` (new), `--actions` (default `idle,walk,run,attack,jump,hurt`), `--views side[,front,back]` with `--view-master VIEW=FILE`, `--finish hd\|pixel` (default the master's), `--target-height`, `--fps` (fractions allowed: `12.5`, `25/2`) or `--frame-ms 80`, `--formats png,webm,packed`, `--tiers`, `--duration 6`, `--resolution 720p`, `--max-takes 3`, `--jobs 3`, `--pin-last auto\|on\|off`, `--gen-timeout`, `--style`, `--pronoun`, `--palette` or `--colors N` (pixel), `--outline selective\|dark\|none` (pixel), `--canvas WxH`, `--canvas-anchor X,Y`, `--no-colour-lock`, `--oneshot-timing auto\|source`, `--oneshot-ms attack=700,...`, `--matte-profile`, `--library` | the set folder: `master/<view>/` byte copies, `set_plan.json` |
| `run` | `--plan`, `--actions`, `--max-takes`, `--jobs`, `--stagger 2`, `--media-cli`, `--timeout` | `actions/<id>/job/`, `actions/<id>/takes/tNN/...`, `set_state.json`, `takes.jsonl` |
| `review` | `--plan`, `--peer <other set>` | `review/<id>/tNN-sheet.png`, `review/<id>/tNN-final.png`, `review/lineup-NNN.png`, `review/review-NNN.json` |
| `accept` | `--plan`, `--action`, `--take`, `--window START:END`, `--note` | the acceptance in `set_state.json` |
| `retake` | `--plan`, `--action`, `--fix <clause id or text>` (repeatable) | a new round and the fix in `set_state.json`, a `retake-requested` line in `takes.jsonl` |
| `report` | `--plan`, `--output` | `reports/report-NNN.json` |

Every verb prints one ASCII JSON line. Errors print `error: ...` and exit 1; usage errors exit 2;
`run` exits 3 when it waits for a clip because no generation route exists. `run` exits 1 when an
action is still incomplete (failed, retake pending, post-processing error) after printing its
summary line.

## The master

`master.json` comes from generate2dsprite (`generate2dsprite.master.v1`): `name`,
`identity_recap` (the long identity phrase every motion prompt repeats), `facing`
(left, right, front, back), `finish` (hd or pixel), `class` (hero, mob, boss, spirit, ...),
`framing` {canvas, subject_height_px, top_margin_px}, `key`, `files` (the padded master PNG and
its sha256), `references`, `transform` and `route`. `plan` checks the PNG's sha256, keys an
opaque master on its flat key exactly like prepare_i2v_input (soft matte), measures the subject
box and the stance anchor, and copies both files into the set. `run` refuses to continue if
either copy changed.

## What plan decides per action

| Action | Kind | Canvas | End frame pinned | Lock | Cut |
|---|---|---|---|---|---|
| idle | loop | square (the master framing) | yes | feet | gait_loop select --kind idle (hover for floating classes) |
| walk | loop | square | no | feet | gait_loop select --kind gait --state walk |
| run | loop | square | no | none (keeps the flight bob) | gait_loop select --kind gait --state run |
| attack | one-shot | 16:9, root led 6% away from the facing | yes | feet | auto retime to 0.7 s, the hit (largest reach) at 40% held 80 ms, cancel when the pose is half back |
| jump | one-shot | 3:4 with 34% headroom | no | feet (the engine moves the body) | auto retime to 0.9 s, custom:takeoff at 22% and custom:land at 72% |
| hurt | one-shot | square | no | feet | auto retime to 0.5 s, the recoil peak at 30% held 80 ms |
| cast, guard | one-shot | 16:9, square | no | feet | auto retime to 0.9 s / 0.7 s, the peak at 45% / 35% |
| victory, defeat | one-shot ending in a held pose | 3:4, 16:9 | no | feet, none | retime ticks at the clip's speed |

The square canvas keeps the master's own framing (scale 1, so the provider input is the padded
master itself). The tall and wide canvases keep the master's feet margin; tall shrinks the subject
until 34% of the height is free above the head. Each placement is one recorded transform:
`prepare_i2v_input.py prepare --canvas --scale --root --anchor --action-padding`, where the action
padding covers the whole video footprint on the master canvas, so `register_clip.py apply` never
clips motion. "Pinned" passes the action's `input.png` (the master as placed on that canvas) as
`--last-frame`; when the route refuses or ignores a last frame the take records
`pinnedLastFrame: false` and why.

Grok and similar models stretch a one-shot over the whole clip: the live Aria attack held its
thrust 1.7 s, the jump took 5.3 s and the hurt held its hunch 2.2 s. Every one-shot entry carries
a `timing` block (`ONESHOT_TIMING`: `{"mode": "auto", "durationMs": 700, "keys": {"hit": {"at":
0.4, "holdMs": 80}}}` for attacks), editable in `set_plan.json`; `--oneshot-ms attack=600` sets the
lengths at plan time and `--oneshot-timing source` (or `"mode": "source"`) keeps the clip's speed.

Floating classes (spirit, ghost, hover, flying) get the hover idle template, no feet lock and a
looser feet gate. `plan` also writes the take-1 prompt and its lint into every action entry, so the
agent can read and edit `motion`, `negatives` and `gates` in `set_plan.json` before `run`.

## The run, per action

1. **Input.** `prepare_i2v_input.py prepare` writes `actions/<id>/job/` (input.png, the keyed
   master.png, registration_job.json).
2. **Prompt.** [motion-prompts.md](motion-prompts.md): `The same <identity_recap>, facing <X> the
   entire time and never turning around, <template>`, the same start and end pose, the action's
   negatives, every fix clause collected so far, then the camera lock, the flat key background and
   "keep the exact <style> look". Written to `takes/tNN/prompt.txt`.
3. **Generate.** `python "<skills>/generate2dmedia/scripts/route_media.py" video --prompt-file P
   --reference input.png [--last-frame input.png] --duration 6 --resolution 720p --out-dir
   takes/tNN/media`, up to `--jobs` clips at once. `--media-cli` replaces the script;
   `FORGE_ROUTE_MEDIA_FAKE` (a script path) replaces it in tests. Exit 3 means no route: the action
   waits, and a clip saved into `takes/tNN/media/` (any .mp4, .webm, .mov) is adopted on the next
   run, so a host with its own image-to-video tool can feed the set by hand. A clip already in
   that folder is never generated again.
4. **Key.** `video2dsprite.py process --matte soft --reference job/master.png --key <key>` (plus
   `--matte-profile` when the plan has one, so every clip of the character is keyed the same way).
5. **Gates.** `takes/tNN/qc.json` (below).
6. **Retake.** A failed gate adds its fix clauses (the gatefix map in motion-prompts.md) to the
   action, and the next take carries every clause collected so far, up to `--max-takes` takes per
   round. When the round is spent, the take with the fewest failures and the longest usable
   window is kept: its usable frames are registered with `--range` and the choice is logged
   (`best-window` in takes.jsonl). No usable window marks the action `failed`.
7. **Register.** `register_clip.py apply --lock feet` (per the table) with the master's rest pose
   as frame 0.
8. **Cut.** Loops: `gait_loop.py select`; with `--fps` below the clip's rate the loop window is
   resampled onto the tick grid with `retime.py` (60 Hz when a frame is a whole number of ticks,
   else the rate's own fraction: `--frame-ms 80` plays exactly 80 ms frames on a 25 Hz grid).
   One-shots (timing `auto`): `retime.py --auto-oneshot --range 0:N --duration <ms>` over the
   registered frames. The motion energy (mean premultiplied change between frames) finds the onset,
   the moving spans and the settle (the pose back within 15% of its excursion); static holds inside
   the action are cut to their first two frames and their last; the keys from the QC landmarks
   (`--key hit=<impact>@0.4+80`, `peak=<peak frame>@0.3+80`, `takeoff=<f>@0.22`, `land=<f>@0.72`)
   pin those source frames to fractions of the length (a key with a hold is a hit-stop), the spans
   between keys are compressed evenly and sampled at the plan's fps (else the clip's). Events: `in`,
   `hit` (attacks; `impactMs`), `custom:<key>`, `cancel` mapped through the same warp, `end`. Timing
   `source`, or a retime that fails, keeps the old cut: `retime.py --range` over the motion window
   (onset - 2 to settle + 3) at the clip's speed.
9. **Finish.** `finish_frames.py hd|pixel --frames registered/frames --output-dir finished
   --target-height N [--scale-ref FILE] [--palette FILE | --colors N] [--outline M] [--canvas WxH
   [--canvas-anchor X,Y]] [--colour-lock job/master.png] [--loop-policy oneshot]`. The
   scale-reference action (idle when planned) finishes first; every other action passes its finish
   record as `--scale-ref`, so one character keeps one body height. The colour lock is on by default
   (the action's keyed job master; `plan --no-colour-lock` turns it off). A pixel set without
   `--palette` learns `--colors` (default 32) colours on the scale-reference action and passes that
   `palette.json` to every other action, so the whole character shares one palette. One-shots are
   finished with `--loop-policy oneshot`. Until finish_frames.py is installed the private stand-in runs (same arguments):
   rest pose = frame 0, scale = target / rest height (or the reference scale times sqrt of the
   rest-area ratio, clamped to 3%), grid pinned to the stance anchor, premultiplied box resample;
   hd keeps full colour with the alpha floor, pixel binarises alpha at 0.5 and maps every frame to
   one median-cut palette (or `--palette`) without dither. `FORGE_FINISH_FRAMES=standin` forces the
   stand-in; a path names another finisher.
10. **Package and verify.** The cut's selection is rebound 1:1 to the finished frames (new hashes,
    same indices, durations and events), then `engine_export.py package --crop-union --max-side
    <no resize> --source-size/--source-anchor <finished canvas and anchor> --body-height-px N
    --formats png,webm,packed` (pixel adds `--pixel-art --sampling nearest`; a plan `--canvas` drops
    `--crop-union`, so the atlas keeps the requested cell) and `engine_export.py verify`. A failed verify repackages once with `--crf-webm 18 --crf-packed 12`.

Default target heights: hd 256 px, pixel 80 px, a boss twice that (a pixel outline is included).
`--fps` defaults to the clip's own rate for hd and 12 for pixel; `--frame-ms 80` (or `--fps 12.5`)
gives exact 80 ms frames.

## Numeric gates

Measured on the keyed frames reduced to about 112 px of body height, against the master as the job
placed it in the video. Thresholds live in each action's `gates` in set_plan.json. A `fail` rejects
the take and adds its fix clauses; a `warn` keeps the take and is printed on its review sheet
(`warnings` in qc.json).

| Gate | Measures | Default |
|---|---|---|
| area | opaque area / the master's | fail: median outside 0.72-1.32 or more than 10% of frames outside |
| feet | ground line drift / body height (jump: the calm start and end only; run: the half-second rolling median, so the flight phase is not drift) | fail: over 5% (run 10%, floating 8%) in more than 3 frames |
| identity | frame 0 against the master; then the head against the master and frame 0 at scales 0.9, 1.0, 1.1 | fail: frame 0 under 0.8, or more than half the frames under 0.30 (the design is lost); warn: more than 2 frames under 0.5 |
| zoom | loops: rolling 1 s median of the body scale; one-shots: end span against frame 0 | fail: loops over 10%, one-shots over 8% (scale and height agree) |
| turn | the mirrored master head matches better than the master head and the silhouette agrees | fail: 3 or more frames in a row |
| edge | subject pixels within 2 px of the video border (full resolution) | fail: any frame |
| background | backdrop pixels farther than 48 from the frame's own key estimate | fail: over 0.4% in more than 2 frames |
| extra | opaque area detached from the body beyond the master's own | fail: over 1.5% of the body in more than 2 frames |
| motion | peak pose change from frame 0 | fail: under the action's minimum (idle 1%, walk 5%, attack 3%) |
| end-pose | one-shots that return to rest: last frames' silhouette IoU with frame 0 | fail: under 0.8 |
| colour | per frame, the share of the body whose hue no master colour at that height has (colour_lock positional colours: chroma over 0.05 and 0.06 or more from every master colour of the band, OKLab a/b); the largest region drift | fail: over 2% of the body in more than 15% of the frames (key bleed, purple or green boots, a cyan blade); warn: region drift over 0.03 in 40% of the frames (the finish's colour lock undoes drift) |
| timing | one-shots: seconds from the motion's onset to its settle and the longest static hold inside it | over 2 s of motion or a 0.5 s hold (jump 2.5 s; cast and guard 1.2 s holds): warn while the set retimes one-shots, fail with timing `source` |

Keying, registration (`register_clip` rest-pose QA) and loop failures (`gait_loop` finds no
cycle) count as the gates `keying`, `registration`/`edge` and `loop`. The usable window is the
longest run of frames that pass every per-frame gate (at least 1 s for loops; one-shots must
contain the whole motion).

### Calibration (live run 2026-10-06)

The gates were recalibrated on the 42 real takes of the Aria and fox sets, labelled by the
reviewer's decisions (accepted, or a retake asked for a kept take) and, for the takes the old gates
rejected before anyone could decide, by their review sheets against the reviewer's checklist and
the defects the reviewer named in the next round. 18 takes are bad (colour bleed, turns, edge cuts,
a doubled or smeared blade, too little motion, specks), 24 are good; slow timing alone is not a
reject label (the reviewer accepted slow one-shots, and the set now retimes them).

| | rejected | precision | recall | good takes rejected |
|---|---|---|---|---|
| before (hard identity floor 0.5 against frame 0) | 31 | 0.42 | 0.72 | 18 of 24 |
| after (the gates above) | 9 | 1.00 | 0.50 | 0 of 24 |

The old identity gate rejected 29 takes, most of them for a head that turned or bobbed: the six
bled runs it rejected are now rejected by the colour gate, for the right reason. What the numbers
still miss (a bent or doubled blade, specks, a weak hurt, mild colour flicker) is what the review
sheets are for; every one-shot was flagged slow by the timing gate and retimed. The fix clause of
a feet failure never asks a walk, run or jump to keep its feet planted: those get
`feet-ground-line` (the motion-prompts.md clause) instead.

## Review and accept

Numbers cannot see identity, facing, a weapon that changed shape, text or a covered face. After
`run`, `review` writes one contact sheet per take (the input, evenly spaced frames and the first
frame of every failing run, each labelled with its frame number and red gate letters), a strip of
the finished frames of each chosen take on its feet line, and a cast line-up of every action's
finished rest frame at x1 and x3 (with `--peer` sets for the whole cast). The agent must open and
look at every sheet (Read in Claude Code, view_image in Codex) and then:

- `accept --action X --take N` records the approval (refused unless the latest review lists that
  take's sheet unchanged). Accepting another take, or a failed take with `--window START:END`,
  makes it the chosen take; the next `run` finishes and packages it.
- `retake --action X --fix <id>` rejects the action semantically: a new round starts with the
  clause (an id from the library, such as `weapon-shape`, `no-text`, `no-flares`,
  `keep-colours`, or free text) and the next `run` generates a new take.

## Files and schemas

All JSON written by the set holds paths relative to the set folder; absolute paths never appear.

- `set_plan.json` (`video2dsprite.sprite_set_plan.v1`): name, class, key, style, pronoun, views,
  `masters` {view: json and png fileRefs, identityRecap, facing, finish, class, size, subjectBox,
  anchor, area, framing}, `generation` {duration, resolution, maxTakes, jobs, pinLastFrame,
  timeoutSeconds}, `finish` {mode, targetHeight, scaleRefAction, palette, colors, outline, canvas,
  canvasAnchor, colourLock}, `package` {formats, tiers, fps (a number, 12.5 allowed), frameMs},
  `matteProfile` (fileRef or null), `library` {name, sha256}, `clauses`, `gatefix`, `estimate`
  {clips, maxClips, maxVideoSeconds}, `actions` [{id, action, view, kind, loopKind, gaitState,
  prepareAction, retimeKind, placement {preset, canvas, scale, root, anchor, subjectBox, margin,
  padding, headroom}, pinLastFrame, lock, airborne, hitTick, returnsToRest, motion, negatives,
  gates, timing (one-shots: {mode auto|source, durationMs, keys {name: {at, holdMs}}}), prompt,
  lint}].
- `set_state.json` (`video2dsprite.sprite_set_state.v1`, replaced atomically after every step):
  plan {path, sha256}, runs, review {latest, at}, `actions` {id: {status (pending, generating,
  retake, no-route, chosen, done, done-warn, failed, post-error), round, fixes [{id, text, source
  auto|agent, take, round}], job {dir, job, sha256, input, referenceScale, keyColor, padding},
  takes [{take, round, dir, status (open, generated, no-route, error, kept, rejected), prompt
  {path, sha256}, fixes, lint, route, pinnedLastFrame, clip {path, sha256}, qc {file, status,
  failures, reasons, usableWindow, usableFrames, frames, motion, badFrames, gates}, failures,
  reasons, fixNext, stages {keyed, registered[-wA-B], motion, timed, finished, selection, package,
  package-r2, verify: each {dir, seconds, ...tool summary}}}], chosen {take, window, tag, reason},
  accepted {take, window, note, at, review, sheet}, output {take, tag, package, manifest, verify},
  message}}.
- `takes.jsonl` (`video2dsprite.sprite_set_take.v1`, append-only): one line per decided take
  (kept, rejected, error), per best-window choice and per retake request, with failures, reasons
  with frame numbers, badFrames per gate, usableWindow, the fixes used and the fixes added next.
- `takes/tNN/qc.json` (`video2dsprite.sprite_set_qc.v1`): status, failures, reasons, warnings,
  gates {id: {status, value, threshold, frames}}, usableWindow, motion {onset, settle, peak,
  peakFrame, impact, cancel, takeoff, land}, reference, perFrame {area, feet, lift, pose, iou0,
  extra, reach, direct, mirror, ncc, nccMaster, nccMirror, nccScaled, bg, foreign, drift, step,
  edge, usable, bad}.
- `review/review-NNN.json` (`video2dsprite.sprite_set_review.v1`) and
  `reports/report-NNN.json` (`video2dsprite.sprite_set_report.v1`: status, finish, package,
  accepted, needsReview, and per action the route, canvas, chosen and accepted take, fixes, every
  take with its QC numbers, `loop` or `timing`, `finish` and `outputs` {package, manifest, qa,
  provenance, poster, atlas, webm, packed, tiers, verify, registration}).

## Limits

- Front and back views need their own approved stills (`--view-master`); the sprite-gen trick of
  redrawing a mid-step still before a front or back walk is not implemented, and the turn gate is
  off for those views.
- The gates measure geometry, not taste. A clip that passes every number can still drift in
  design; that is what the review is for.
- Accepting a different take of the scale-reference action does not refinish the other actions;
  their rest heights already match through registration by construction.
- Parallel generation (`--jobs`) only overlaps the media calls; keying, gates and packaging run
  one at a time.
