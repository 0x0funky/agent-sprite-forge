# B08-video-packaging: engine_export 3.0 (residue gate, selection/registration-aware manifest, transport, verify) and validate_animation

Branch `asf/B08-video-packaging` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files:
[engine_export.py](../skills/video2dsprite/scripts/engine_export.py) (rewritten),
[validate_animation.py](../skills/video2dsprite/scripts/validate_animation.py) (new),
[pipeline.md](../skills/video2dsprite/references/pipeline.md) (rewritten),
[tests/test_engine_export_v3.py](../tests/test_engine_export_v3.py),
[tests/test_validate_animation.py](../tests/test_validate_animation.py),
[tests/test_engine_export.py](../tests/test_engine_export.py) (the 13 cfed170 tests A0 copied, decoupled from
video2dsprite.py), and 39 negative fixtures in [tests/fixtures/animation/negative/](../tests/fixtures/animation/negative/).

Integration status (Phase 3, group video, branch `asf/int-g-video`): the review's three B08 blocking issues and the
D17, D18, D19, D21 and D26-D30 items are resolved; each resolved item below cites its decision ("per Dn").

## 1. CLIs

Run from the user's project root; `<skill-dir>` is `${CLAUDE_SKILL_DIR}` in Claude Code.

    python "<skill-dir>/scripts/engine_export.py" package --clean-dir work/hero-walk/frames-clean --output-dir game/hero/walk --name walk --fps 12 --loop --formats png,webm,packed
    python "<skill-dir>/scripts/engine_export.py" package --clean-dir work/hero-walk-reg/frames --selection work/hero-walk-loop/selection.json --registration work/hero-walk-reg/registration.json --review review.json --output-dir game/hero/walk --name walk --formats png,webm,packed --tiers actor
    python "<skill-dir>/scripts/engine_export.py" package --clean-dir work/hero-attack-reg/frames --selection work/hero-attack-ticks/selection.json --registration jobs/hero-attack/registration_job.json --action-padding 96,80,96,24 --output-dir game/hero/attack --name attack --formats png,webm,packed
    python "<skill-dir>/scripts/engine_export.py" verify --package game/hero/walk
    python "<skill-dir>/scripts/engine_export.py" doctor
    python "<skill-dir>/scripts/validate_animation.py" game/hero --require-states idle,walk,attack --require-verify --static-sprite game/hero/hero.png --static-anchor 224,430

- `package` flags (also exposed as `engine_export.add_package_arguments(parser)` with the cfed170 dest names;
  `video2dsprite.py package` and `video2dsprite.py verify` are these verbs, per D20):
  `--clean-dir`, `--output-dir` (alias `--out-dir`), `--name`, `--fps` (number or `num/den`), `--selection`
  (forge-frame-selection v1/v2), `--registration` (register_clip registration.json, best, or registration_job.v1),
  `--action-padding L,T,R,B` (with a job: the padding `register_clip.py apply --action-padding` used, per D21),
  `--review` (review_verdict.v1), `--pipeline-meta`, `--source-size`, `--source-anchor`, `--max-side` (384), `--crop-union`,
  `--formats png,webm,packed`, `--tiers actor|prop|fx|name:EDGE@FPS,...`, `--budget-class`, `--loop`,
  `--loop-policy cycle|pingpong|oneshot`, `--key auto|magenta|green|blue|#rrggbb|none`, `--allow-key-residue`,
  `--pixel-art`, `--sampling`, `--resampler`, `--body-height-px`, `--shadow RX,RY[,OPACITY]`, `--display-scale`,
  `--cadence-ms`, `--stride-world-units`, `--speed-ref`, `--cycles`, `--terminal`, `--art-source`, `--placeholder`,
  `--crf-webm` (28), `--crf-packed` (18).
- `--key auto` (default) takes the residue gate's key from `<clean-dir>/matte-report.json`, then the
  `--registration` keyColor, then `--pipeline-meta` `matte.key`, else magenta, and records `keySource`
  (`matte-report`, `registration`, `pipeline-meta`, `default`, or `flag` for `--key`) in QA, provenance params and
  the summary; a used matte report is a provenance input with role `matteReport` (per D17). A `matte-report.json`
  that is not a matte_report.v1 document is an error.
- The residue gate measures every frame on a copy with alpha <= 16 (forge_core.ALPHA_GEOMETRY_THRESHOLD) cleared and
  records the floor in `keyResidue.thresholds.alpha_floor` and `method`; a visible (alpha 17-255) key fringe still
  fails (per D18).
- Events may sit on the end edge (`0 <= atMs <= duration`) and name the last frame; `impactMs`/`holdMs` stay strictly
  inside (per D19).
- Video transports (webm, packed, tiers) expand uneven whole-tick durations into repeated frames at the selection's
  `tickHz` (with `ticks`), else its source fps, else 60 Hz (at most 60 fps, every authored edge within 1 ms, clip
  length kept); `tickExpansion` records it (per D21). Durations that are whole ticks of none of these package only as
  PNG, with an error that says so.
- `package` refuses an existing output folder, stages beside it and publishes only after every gate, encode and
  full decode passed. Its one-line JSON reports `output`, `metadata` (animation.json), `manifest`, `qa`,
  `provenance`, `status`, `reviewStatus`, `frameCount`, `fps` (num/den), `durationMs`, `keySource`, `inputDigest`
  and, when expanded, `tickExpansion` (per D20).
- `verify --package <dir> [--report <new file>]` writes `<dir>/verify-qa.json` only when every gate passes; on a
  failure it prints the failed checks and writes nothing (exit 1). One-line JSON: `output` and `metadata` (the
  report), `report`, `manifest`, `status`, `checks`, `transports`.
- `doctor` prints `capabilities()` (forge_av functional probes) as one JSON line.
- `validate_animation.py PATH... [--require-states A,B] [--static-sprite PNG | --static-size W,H] [--static-anchor X,Y]
  [--require-verify] [--require-review] [--report <new file>]`: one-line JSON (`status`, `output`/`metadata`/`report`
  (the report or null), `packages`, `manifests`, `skipped`) on success; otherwise `error: <rule>: <package>: <message>`
  lines on stderr and exit 1, with no report written. The `schema` rule runs the vendored forge_schema evaluator
  (never skipped, per D31); packed-geometry enforces `width <= halfWidth` and `height <= halfHeight` (per D21); the
  timing rule checks a `tickExpansion` against `sourceIndices` and `durationsMs`.
- Exit codes and errors (per D26, D27): usage errors exit 2 (argparse); refused inputs, failed gates and failed rules
  print `error: ...` and exit 1 with nothing published; anything unexpected prints `error: internal error (<Type>:
  <message>)` (forge_core.run_cli). JSON inputs are read with forge_core.read_json (BOM-tolerant, strict, per D28).
  QA envelopes and provenance record `tool.version` 0.4.0 (per D29); `engineExport` 3.0.0 stays in provenance params.
- `--help` of `engine_export.py` (and of `package`, `verify`, `doctor`) and of `validate_animation.py` is ASCII and
  exits 0 under cp1252 and cp950 (tested).

## 2. SKILL.md routing rows

video2dsprite:

| Need | Route |
|---|---|
| Ship a clip to a game (PNG atlas fallback, WebM, iPhone packed MP4, mobile tiers) | `scripts/engine_export.py package --clean-dir <apply's frames/> --output-dir <new> --selection <selection.json> --registration <registration.json> --formats png,webm,packed --tiers actor`; then `verify`, then `validate_animation.py` (`video2dsprite.py package`/`verify` are the same verbs) |
| Package refused for key residue | re-key the frames with the soft matte (video2dsprite clean); `--allow-key-residue` only ships them with a recorded override |
| Prove the encoded files decode like the atlas (alpha, colour, duration, loop seam) | `scripts/engine_export.py verify --package <dir>` |
| Check a character's clips before integration | `scripts/validate_animation.py <character folder> --require-states idle,walk,... --require-verify` |
| Check encoders | `scripts/engine_export.py doctor` |

Acceptance for the video SKILL.md: package, then verify, then validate_animation. Processing notes, ready to paste:

- animation.json 3.0 keeps every 2.0 key; timing is `durationsMs` (whole ms) with an exact `fpsRational` for video.
- Geometry stays in source units (`sourceSize`, `sourceAnchor`, `sourceRect`) at every media size and tier.
- On iPhone draw the compositor's `lease.drawable`, never the packed MP4 itself; show the PNG poster until a decoded
  frame exists.

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `video2dsprite/scripts/engine_export.py` | Packages frames as animation.json 3.0 with a PNG atlas, VP9-alpha WebM, packed-alpha MP4 and mobile tiers behind a key-residue gate; `verify` decodes every file against the atlas | tests/test_engine_export_v3.py, tests/test_engine_export.py |
| `video2dsprite/scripts/validate_animation.py` | Checks packages against the runtime contract (states, anchor, timing, events, poster, padding, ffprobe facts, static-sprite geometry, sha256-bound QA) | tests/test_validate_animation.py (34 negative fixtures) |

## 4. CHANGELOG entries

- Added: `engine_export.py` is a CLI with `package`, `verify` and `doctor` (B08-T3, T4).
- Added: animation.json 3.0, a superset of 2.0: `sourceIndices`, `durationsMs`, `fpsRational`, `loopPolicy`,
  `events`, `impactMs`, `holdMs`, `terminal`, `cadenceMs`, `strideWorldUnits`, `speedRef`, `cycles`, a registration
  object (`construction`, `fixed-envelope`, `preserved`, with `jobSha256`, `baseSize`, `baseAnchor`, `padding`),
  `sampling`, `pixelArt`, `bodyHeightPx`, `shadow`, `displayScale`, `budgetClass`, `mobilePackedAlpha`,
  `provenanceFile` (provenance.json), `artSource`; `--selection`, `--registration` and `--review` inputs bound by
  sha256 (B08-T2).
- Added: mobile packed tiers `--tiers actor:320@24,prop:384@12,fx:512@30` (or custom `name:EDGE@FPS`), scaled with
  premultiplied resampling and resampled in time without changing the clip length (B08-T3).
- Added: `verify` (alpha MAE/p99/max, transparent and opaque alpha medians, visible RGB error, dynamic alpha,
  codec, dimensions, packets, decode timestamps, duration, faststart, VP9 alpha tag, decoded loop seam) writing a
  hash-bound `verify-qa.json` (B08-T4).
- Added: `validate_animation.py` with 27 named rules and hash-bound QA checks: stale, partial or foreign QA is
  refused (B08-T5, B08-T6); every rule has a negative fixture (39), `schema` and `ffprobe-timestamps` included.
- Added: `--key auto` reads the key the matte used from `<clean-dir>/matte-report.json`, then the registration
  keyColor, then pipeline-meta, and records `keySource` (integration D17).
- Added: uneven whole-tick durations (retime `--ticks`, held frames) package as WebM and packed MP4 by repeating
  frames at the tick rate (`tickExpansion`, integration D21); `--action-padding` for registration jobs; a job's
  view box and `sourceSize`/`sourceAnchor` are honoured (integration D21).
- Changed: loops are encoded as one closed GOP per loop (keyint = frame count); transport rates are exact
  rationals capped at 60 fps; packed halves pad right/bottom to even pixels and `packedAlpha.width/height` is the
  logical content size with `halfWidth/halfHeight` the encoded halves (B08-T3; Dusk iOS transport).
- Changed: resizing uses forge_core.resample_rgba (premultiplied float) and rounds half up; encoders, probing and
  decoding use forge_av (functional probes, UTF-8 subprocess text, BT.709 tags, edge bleed); every encoded file is
  decoded completely before publication (B08-T3).
- Changed: `package` stages with forge_core.staged_output; output files are written with forge_core.save_png and
  write_json (deterministic, no metadata chunks).
- Changed: the residue gate ignores alpha <= 16 (invisible resampling halo) and records the floor (integration D18);
  events may sit on the clip's end edge (integration D19); validate_animation checks the schema with the vendored
  forge_schema (no optional jsonschema) and `width <= halfWidth` (integration D21, D31).
- Changed: summaries name `output` and `metadata` (integration D20); `error: internal error (...)` for unexpected
  failures (D27); BOM-tolerant JSON input (D28); QA `tool.version` is 0.4.0 (D29); forge_core.round_half_up and
  file_ref (D30). A whole selection `cycles` count stays an integer.
- BREAKING: `package` fails on key residue (any opaque key pixel, or key spill on more than 1% of the outer ring);
  legacy switch `--allow-key-residue` (plan Appendix H row "video package"). `--key none` skips the gate for
  native-alpha footage.
- BREAKING: animation.json 3.0 (`registration` is an object, `qa` a QA envelope that still holds the 2.0
  diagnostics; every 2.0 key kept); no legacy switch (plan Appendix H). Runtimes must crop packed alpha at
  `halfWidth` (identical to 2.0 whenever the content size is even).
- Fixed: registered frames (register_clip apply) were refused by the residue gate at default settings (5.9% ring
  spill from alpha 1-4 halo); green-keyed clips were measured against magenta (false refusals, missed green
  residue); tick-row selections with their `end` event could not be packaged (Wave B review, integration D17-D19).
- Fixed: report v2 P0-3 (the forge-cycle and forge-cycle-31 packages shipped about 70-83% ring spill and 498
  opaque key px; both are now refused). report v2 P1-1 (`run()` decodes UTF-8 with replacement; console text
  ASCII). report v2 P1-7 (pipeline.md lists exact file names, inputs and single-line commands). hd2d seam-codec
  finding (a single-GOP loop pop is caught on the decoded file). engine_export.py:16-28 capability string matching
  (now forge_av functional probes).

## 5. Schema change requests

Resolved: integration S1 applied this section to shared/schemas/video.schema.json (commit f3d7eb2, with the D17,
D18, D19 and D21 descriptions); the tests now validate real output with `assert_valid_contract` against the vendored
schemas (`animation_v3`, `package_provenance_v1`, `verify_report_v1`, `validation_report_v1`) and the in-memory
`proposed_errors` helper is gone. Kept for reference:

`shared/schemas/video.schema.json`, add to `$defs.animation_v3.properties`:

```json
"fpsRational": {"description": "Exact transport rate of the video files, num/den; fps keeps the 2.0 number.",
                "type": "string", "pattern": "^[1-9][0-9]*/[1-9][0-9]*$"},
"fpsCapped": {"description": "The clip was reduced to at most 60 fps without changing its length.", "type": "boolean"},
"inputFps": {"$ref": "common.schema.json#/$defs/fpsValue"},
"pingpongBaked": {"description": "A pingpong policy was baked into one cycle (0 1 2 3 2 1).", "type": "boolean"}
```

`shared/schemas/video.schema.json`, add to `$defs`:

```json
"package_provenance_v1": {
  "description": "provenance.json beside animation.json (engine_export.py package): common provenance plus the input digest a review verdict may bind.",
  "allOf": [{"$ref": "common.schema.json#/$defs/provenance"}],
  "type": "object",
  "required": ["schema", "inputDigest"],
  "properties": {
    "schema": {"const": "video2dsprite.provenance.v1"},
    "inputDigest": {"$ref": "common.schema.json#/$defs/sha256"},
    "inputDirectory": {"$ref": "common.schema.json#/$defs/relPath"}
  }
},
"verify_report_v1": {
  "description": "verify-qa.json (engine_export.py verify): a QA envelope over the encoded files of one package, bound to its animation.json by sha256.",
  "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
  "type": "object",
  "required": ["schema", "manifestSha256", "transports"],
  "properties": {
    "schema": {"const": "video2dsprite.verify.v1"},
    "manifestSha256": {"$ref": "common.schema.json#/$defs/sha256"},
    "transports": {"type": "object"},
    "thresholds": {"type": "object"}
  }
},
"validation_report_v1": {
  "description": "validate_animation.py --report: a QA envelope with one check per rule and package.",
  "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
  "type": "object",
  "required": ["schema"],
  "properties": {"schema": {"const": "video2dsprite.validation.v1"}}
}
```

New request (integration, optional): document `tickExpansion` in `$defs.animation_v3.properties`:

```json
"tickExpansion": {"description": "Uneven whole-tick durations expanded for video transports (engine_export, D21): each authored frame is repeated ticks[i] times at rate; sourceIndices and durationsMs are the expanded timeline.",
                  "type": "object", "required": ["rate", "ticks", "authoredSourceIndices", "authoredDurationsMs"],
                  "properties": {"rate": {"$ref": "common.schema.json#/$defs/fpsValue"},
                                 "ticks": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1}},
                                 "authoredSourceIndices": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                                 "authoredDurationsMs": {"$ref": "common.schema.json#/$defs/durationsMs"}}}
```

Optional fields B08 writes inside open objects (no diff needed, listed so they do not drift):

- `registration`: `legacyMode` (the 2.0 string `preserved-source-canvas` or `fixed-union-envelope`),
  `registrationSha256` (a non-job registration record), `sourceSize`, `sourceAnchor`, `action`.
- `qa` (and animation-qa.json): the 2.0 diagnostics (`loopRequested`, `frameCount`, `durationSeconds`,
  `blankFrames`, `edgeTouchFrames`, `boundsBottomSpanPx`, `boundsCenterXSpanPx`, `seamPremultipliedMAE`,
  `adjacentMedianMAE`, `seamToMedianRatio`, `adjacentMaxMAE`, `notes`) plus `keyResidue` (`key`, `framesMeasured`,
  `opaqueKeyPx`, `framesWithOpaqueKey`, `maxOuterRingSpillFraction`, `framesOverRingSpill`,
  `maxSemitransparentFraction`, `minSemitransparentFraction`, `enclosedKeyPockets`, `keyHuedPx`, `limits`,
  `thresholds` (with `alpha_floor` 16, per D18), `method`), `seamReport` (common/seamReport), `allowKeyResidue`,
  `keySource` (`matte-report`, `registration`, `pipeline-meta`, `default` or `flag`, per D17); a `tick_expansion`
  check when timing was expanded.
- `webm`: `codec`, `pixFmt`, `width`, `height`, `crf`, `keyint`, `keyframes`, `frameCount`, `fps`, `durationMs`,
  `requiresAlphaPlaybackVerification`, `fullDecodePassed`.
- `packedAlpha`: `codec`, `profile`, `encodedSize`, `requiresCompositor`, `crf`, `keyint`, `keyframes`, `closedGop`,
  `frameCount`, `durationMs`, `fullDecodePassed`.
- `mobilePackedAlpha[]`: `halfWidth`, `halfHeight`, `encodedSize`, `layout`, `encodedAnchor` (anchor in tier
  pixels), `maxLongEdge`, `maxFps`, `frameCount`, `durationMs`, `inputIndices` (positions in the main timeline),
  `resampler`, `bytes`, `mimeType`, `codec`, `profile`, `crf`, `keyint`, `keyframes`, `closedGop`,
  `requiresCompositor`, `fullDecodePassed`.
- provenance.json: `schema` (`video2dsprite.provenance.v1`), `inputDigest`, `inputDirectory` (only when a relative
  path exists), `libraries`, `selection`, `registration`, `review`; inputs carry a `role` (frame, selection,
  registration, review, pipelineMeta, matteReport); params add `actionPadding`, `tickExpansion` and `engineExport`.

Input documents as B08 reads them (for B06, B07 and Z):

- frame_selection_v2: `sourceHashes` hold one sha256 per frame of `[start, endExclusive)` in the clean-dir order
  (a list with one hash per `sourceIndices` entry is accepted too); `durations_ms` are authoritative (a `fps`
  field is kept only when the durations are its forge_core.frame_durations rounding); a `status` starting with
  reject, fail or unusable is refused; optional `speedRef`, `cycles` and `terminal` are read when present.
- registration: a `video2dsprite.registration_job.v1` (base canvas = its `sourceSize`/`sourceAnchor`, else
  `master.size`/`anchor` cut to `master.viewBox`; padding = `--action-padding` or the job's), or any record with
  `mode` (construction, fixed-envelope, preserved) and optional `jobSha256`, `padding`, `baseSize`/`baseAnchor` (or
  `master.size`/`master.anchor`), `sourceSize`, `sourceAnchor`; `keyColor` feeds `--key auto` (per D17, D21).
- matte-report.json in `--clean-dir`: `schema` video2dsprite.matte_report.v1, `key` (RGB) and `mode` (`none` skips
  the gate), per D17.
- review_verdict_v1: `reviewedSha256` is the sha256 of the selection file, or the package `inputDigest` (sha256 of
  the newline-terminated list of frame sha256s in playback order).
- pipeline-meta.json: `matte.key` (name, `#rrggbb` or RGB triple, rounded) and `matte.mode` (`none` skips the
  residue gate).

## 6. Shared-helper promotion requests

- Resolved (per D30): `engine_export._local_round_half_up` is gone; `engine_export.round_half_up` is
  forge_core.round_half_up (Fraction-exact), and packaged-file refs use forge_core.file_ref.
- Candidates for shared timing (forge_core), used by retime (B07) and the clip builder (B02) as well:
  `engine_export.constant_timeline`, `duration_timeline`, `cap_timeline` (at most N fps without changing the clip
  length, nearest frame start, ties to the later frame) and `bake_pingpong`. Tests: `test_timeline_helpers`,
  `test_fps_cap_keeps_the_clip_length`, `test_pingpong_is_baked_into_one_cycle`.
- Candidate for forge_av: `engine_export.evaluate_transport(reference, decoded, *, loop, label)` (decoded-file QA:
  alpha error and endpoints, visible RGB error, dynamic alpha, decoded seam against the source ratio). B16
  `scene_motion qa` needs the same seam rule on the decoded file. Tests: the four `test_verify_*` synthetic tests.

## 7. Cross-module links that Z must add

- Resolved (per D20): `video2dsprite.py package` uses `engine_export.add_package_arguments` and `cmd_package`, and
  `video2dsprite.py verify` uses `add_verify_arguments` and `cmd_verify`.
- Resolved (per D17): `package --key auto` reads `<clean-dir>/matte-report.json` first, then the registration
  keyColor, then pipeline-meta `matte.key`/`matte.mode`; B05 keeps those names.
- Resolved (per D21): uneven whole-tick durations from gait_loop/retime package as video by repeated frames; B07
  needs no holds-as-repeats option.
- B09 runtime export and packed-alpha-runtime.js: crop alpha at `halfWidth` and expect a `2 * halfWidth` video
  (pipeline.md says so); read `durationsMs`, `events`, `fpsRational`, `mobilePackedAlpha`, the registration object.
- Video SKILL.md: link references/pipeline.md; acceptance order package, verify, validate_animation; package the
  registered `frames/` with `--registration <registration.json>` (the key and the canvas come from it). matte.md
  (B05) now links pipeline.md for the gate; animation-review.md (B07) says how tick rows package; Z may still add
  that the selection file is what `--selection` and a review verdict bind.
- CHANGELOG (Z): the entries in section 4; Appendix H rows "video package" are implemented as stated.
- tests: the 13 cfed170 originals live only in tests/test_engine_export.py (B05-T1 done).
- shared/schemas (integration): the optional `tickExpansion` description of section 5.

## 8. Known limitations and what is not proven

- Thresholds: the residue gate (opaque key px 0, ring spill 1%, alpha floor 16) comes from report v2 on one clip
  and the Wave B review's registration measurements (registered frames: 5.9% ring spill at floor 0, 0.10% at
  floor 16; a real alpha-255 fringe stays at 100%); matte_qa counts key-dominant edge colours (crimson, pink, purple
  outlines) as spill, so such designs need a key the matte report or `--key` names, or `--allow-key-residue`. The
  verify gates are Dusk's iOS transport gates (one game) plus synthetic clips.
- Tick expansion tries the selection's tickHz (with `ticks`), its fps, then 60 Hz; durations from `retime --duration`
  or a three-key map that are whole ticks of none of these stay PNG-only (no wider rate search, so the transport
  rate is always one the selection names or the forge 60 Hz grid). An expanded atlas holds one cell per tick.
- The decoded-seam rule is relative: decoded seam / adjacent p95 may exceed max(1, source ratio) by 10%. It fails a
  pop the codec added (the hd2d single-GOP case: 1.47 on a subtle ambient loop) but reports a pop already in the
  source as `source_seam` warn, not as a transport failure. Large motion masks small codec pops (correctly).
- Playback: only offline ffmpeg decoding is checked; no browser, Safari, WebGL compositor or iPhone run.
- Platforms: Windows 11, Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, ffmpeg 8.0.1 (gyan full build). Exact container
  durations (625.0 ms, 950.0 ms) were measured on this ffmpeg build; WebM durations land within 1 ms. Linux, macOS,
  Python 3.10 and Pillow 10.1 were not run.
- Performance (this machine, shared with other agents): 64 Ryo frames of 960x960 (frames 40-103 of the evidence
  clip, `--allow-key-residue` because the cfed170 keyer left 3,526 opaque key px and 85% ring spill) package with
  webm, packed and the actor tier in about 25 s (matte_qa and the VP9 encode dominate; measuring matte_qa on each
  frame's alpha box is exact and 2.3x faster) and verify in about 14 s. Verify passed on that real clip: alpha
  MAE 0.14-0.35, visible RGB MAE 4.9-5.5, decoded seam ratios within 1% of the source. The cfed170 128-megapixel
  decode budget is kept, so all 145 frames at 960x960 need a selection or a smaller extract.
- Interpretations of the plan (deviations):
  - `fps` stays a number for 2.0 readers; the exact rate is the new `fpsRational`, and transport records carry
    forge_av's `num/den` strings.
  - "provenance moves to provenance.json": the full provenance is there; `inputFrames` (a 2.0 key: names, bytes,
    sha256) stays in animation.json because every 2.0 key is kept.
  - `--tiers` encodes every listed tier; `--budget-class` defaults to the tier when there is exactly one. The
    default table is `actor:320@24, prop:384@12, fx:512@30`; tiers resample time without changing the clip length
    (Dusk's capped FX changed 0.480 s to 0.4833 s; here the length is exact).
  - pingpong is baked into the frames (`pingpongBaked`) because video cannot play backwards; `loopPolicy` becomes
    `cycle`.
  - The residue gate measures the full-resolution source frames (stricter than the downscaled media).
  - `verify` writes its report into the package folder (a no-replace sidecar); `validate_animation` reads it there.
  - `validate_animation --require-states` matches the manifests' `name`.
- Not covered by tests: a WebM container whose duration metadata disagrees with its packets. (The
  `ffprobe-timestamps` and `schema` negative fixtures now exist, per D21.)
- Canonical chain (integration): key-plan, prepare_i2v_input, take, process, qc, register_clip apply, gait_loop
  select, retime `--ticks` with its end event, package png+webm+packed (+ actor tier), verify, validate_animation
  pass at default settings on a synthetic 64-frame 1280x720 take (44 s), and in `test_canonical_chain` on a
  320x180 canvas (about 20 s).
