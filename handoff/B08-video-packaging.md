# B08-video-packaging: engine_export 3.0 (residue gate, selection/registration-aware manifest, transport, verify) and validate_animation

Branch `asf/B08-video-packaging` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files:
[engine_export.py](../skills/video2dsprite/scripts/engine_export.py) (rewritten),
[validate_animation.py](../skills/video2dsprite/scripts/validate_animation.py) (new),
[pipeline.md](../skills/video2dsprite/references/pipeline.md) (rewritten),
[tests/test_engine_export_v3.py](../tests/test_engine_export_v3.py),
[tests/test_validate_animation.py](../tests/test_validate_animation.py),
[tests/test_engine_export.py](../tests/test_engine_export.py) (the 13 cfed170 tests A0 copied, decoupled from
video2dsprite.py), and 34 negative fixtures in [tests/fixtures/animation/negative/](../tests/fixtures/animation/negative/).

## 1. CLIs

Run from the user's project root; `<skill-dir>` is `${CLAUDE_SKILL_DIR}` in Claude Code.

    python "<skill-dir>/scripts/engine_export.py" package --clean-dir work/hero-walk/frames-clean --output-dir game/hero/walk --name walk --fps 12 --loop --formats png,webm,packed
    python "<skill-dir>/scripts/engine_export.py" package --clean-dir work/hero-walk/frames-clean --selection work/hero-walk/selection.json --registration work/hero-walk/registration.json --review work/hero-walk/review.json --pipeline-meta work/hero-walk/pipeline-meta.json --output-dir game/hero/walk --name walk --formats png,webm,packed --tiers actor
    python "<skill-dir>/scripts/engine_export.py" verify --package game/hero/walk
    python "<skill-dir>/scripts/engine_export.py" doctor
    python "<skill-dir>/scripts/validate_animation.py" game/hero --require-states idle,walk,attack --require-verify --static-sprite game/hero/hero.png --static-anchor 224,430

- `package` flags (also exposed as `engine_export.add_package_arguments(parser)` with the cfed170 dest names):
  `--clean-dir`, `--output-dir` (alias `--out-dir`), `--name`, `--fps` (number or `num/den`), `--selection`
  (forge-frame-selection v1/v2), `--registration` (registration_job.v1 or a record with `mode`), `--review`
  (review_verdict.v1), `--pipeline-meta`, `--source-size`, `--source-anchor`, `--max-side` (384), `--crop-union`,
  `--formats png,webm,packed`, `--tiers actor|prop|fx|name:EDGE@FPS,...`, `--budget-class`, `--loop`,
  `--loop-policy cycle|pingpong|oneshot`, `--key auto|magenta|green|blue|#rrggbb|none`, `--allow-key-residue`,
  `--pixel-art`, `--sampling`, `--resampler`, `--body-height-px`, `--shadow RX,RY[,OPACITY]`, `--display-scale`,
  `--cadence-ms`, `--stride-world-units`, `--speed-ref`, `--cycles`, `--terminal`, `--art-source`, `--placeholder`,
  `--crf-webm` (28), `--crf-packed` (18).
- `package` refuses an existing output folder, stages beside it and publishes only after every gate, encode and
  full decode passed. Its one-line JSON reports `output`, `manifest`, `qa`, `provenance`, `status`, `reviewStatus`,
  `frameCount`, `fps` (num/den), `durationMs` and `inputDigest`.
- `verify --package <dir> [--report <new file>]` writes `<dir>/verify-qa.json` only when every gate passes; on a
  failure it prints the failed checks and writes nothing. One-line JSON: `report`, `manifest`, `status`, `checks`,
  `transports`.
- `doctor` prints `capabilities()` (forge_av functional probes) as one JSON line.
- `validate_animation.py PATH... [--require-states A,B] [--static-sprite PNG | --static-size W,H] [--static-anchor X,Y]
  [--require-verify] [--require-review] [--report <new file>]`: one-line JSON (`status`, `packages`, `manifests`,
  `skipped`, `report`) on success; otherwise `error: <rule>: <package>: <message>` lines on stderr and exit 1, with
  no report written.
- `--help` of `engine_export.py` (and of `package`, `verify`, `doctor`) and of `validate_animation.py` is ASCII and
  exits 0 under cp1252 and cp950 (tested).

## 2. SKILL.md routing rows

video2dsprite:

| Need | Route |
|---|---|
| Ship a clip to a game (PNG atlas fallback, WebM, iPhone packed MP4, mobile tiers) | `scripts/engine_export.py package --clean-dir <frames-clean> --output-dir <new> --selection <selection.json> --formats png,webm,packed --tiers actor`; then `verify`, then `validate_animation.py` |
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
  refused (B08-T5, B08-T6).
- Changed: loops are encoded as one closed GOP per loop (keyint = frame count); transport rates are exact
  rationals capped at 60 fps; packed halves pad right/bottom to even pixels and `packedAlpha.width/height` is the
  logical content size with `halfWidth/halfHeight` the encoded halves (B08-T3; Dusk iOS transport).
- Changed: resizing uses forge_core.resample_rgba (premultiplied float) and rounds half up; encoders, probing and
  decoding use forge_av (functional probes, UTF-8 subprocess text, BT.709 tags, edge bleed); every encoded file is
  decoded completely before publication (B08-T3).
- Changed: `package` stages with forge_core.staged_output; output files are written with forge_core.save_png and
  write_json (deterministic, no metadata chunks).
- BREAKING: `package` fails on key residue (any opaque key pixel, or key spill on more than 1% of the outer ring);
  legacy switch `--allow-key-residue` (plan Appendix H row "video package"). `--key none` skips the gate for
  native-alpha footage.
- BREAKING: animation.json 3.0 (`registration` is an object, `qa` a QA envelope that still holds the 2.0
  diagnostics; every 2.0 key kept); no legacy switch (plan Appendix H). Runtimes must crop packed alpha at
  `halfWidth` (identical to 2.0 whenever the content size is even).
- Fixed: report v2 P0-3 (the forge-cycle and forge-cycle-31 packages shipped about 70-83% ring spill and 498
  opaque key px; both are now refused). report v2 P1-1 (`run()` decodes UTF-8 with replacement; console text
  ASCII). report v2 P1-7 (pipeline.md lists exact file names, inputs and single-line commands). hd2d seam-codec
  finding (a single-GOP loop pop is caught on the decoded file). engine_export.py:16-28 capability string matching
  (now forge_av functional probes).

## 5. Schema change requests

The documents B08 writes validate against the frozen schemas as they are (animation.json against
`video/animation_v3`, animation-qa.json and verify-qa.json against `common/qaEnvelope`, provenance.json against
`common/provenance`). These additions document them; `tests/test_engine_export_v3.py::proposed_errors` applies
exactly this diff in memory and validates real output against it. Producer B08; consumers B09, Z, games.

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

Optional fields B08 writes inside open objects (no diff needed, listed so they do not drift):

- `registration`: `legacyMode` (the 2.0 string `preserved-source-canvas` or `fixed-union-envelope`),
  `registrationSha256` (a non-job registration record), `sourceSize`, `sourceAnchor`, `action`.
- `qa` (and animation-qa.json): the 2.0 diagnostics (`loopRequested`, `frameCount`, `durationSeconds`,
  `blankFrames`, `edgeTouchFrames`, `boundsBottomSpanPx`, `boundsCenterXSpanPx`, `seamPremultipliedMAE`,
  `adjacentMedianMAE`, `seamToMedianRatio`, `adjacentMaxMAE`, `notes`) plus `keyResidue` (`key`, `framesMeasured`,
  `opaqueKeyPx`, `framesWithOpaqueKey`, `maxOuterRingSpillFraction`, `framesOverRingSpill`,
  `maxSemitransparentFraction`, `minSemitransparentFraction`, `enclosedKeyPockets`, `keyHuedPx`, `limits`,
  `thresholds`, `method`), `seamReport` (common/seamReport), `allowKeyResidue`, `keySource`.
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
  registration, review, pipelineMeta).

Input documents as B08 reads them (for B06, B07 and Z):

- frame_selection_v2: `sourceHashes` hold one sha256 per frame of `[start, endExclusive)` in the clean-dir order
  (a list with one hash per `sourceIndices` entry is accepted too); `durations_ms` are authoritative (a `fps`
  field is kept only when the durations are its forge_core.frame_durations rounding); a `status` starting with
  reject, fail or unusable is refused; optional `speedRef`, `cycles` and `terminal` are read when present.
- registration: a `video2dsprite.registration_job.v1`, or any record with `mode` (construction, fixed-envelope,
  preserved) and optional `jobSha256`, `padding`, `baseSize`/`baseAnchor` (or `master.size`/`master.anchor`),
  `sourceSize`, `sourceAnchor`.
- review_verdict_v1: `reviewedSha256` is the sha256 of the selection file, or the package `inputDigest` (sha256 of
  the newline-terminated list of frame sha256s in playback order).
- pipeline-meta.json: `matte.key` (name, `#rrggbb` or RGB triple, rounded) and `matte.mode` (`none` skips the
  residue gate).

## 6. Shared-helper promotion requests

- `engine_export._local_round_half_up(value)` (engine_export.py, "small helpers"): floor(value + 1/2), exact for
  Fractions. forge_core has the private `_round_half_up(float)`; promote a public `forge_core.round_half_up` that
  also accepts Fractions. Covered by `test_timeline_helpers` and the duration tests.
- Candidates for shared timing (forge_core), used by retime (B07) and the clip builder (B02) as well:
  `engine_export.constant_timeline`, `duration_timeline`, `cap_timeline` (at most N fps without changing the clip
  length, nearest frame start, ties to the later frame) and `bake_pingpong`. Tests: `test_timeline_helpers`,
  `test_fps_cap_keeps_the_clip_length`, `test_pingpong_is_baked_into_one_cycle`.
- Candidate for forge_av: `engine_export.evaluate_transport(reference, decoded, *, loop, label)` (decoded-file QA:
  alpha error and endpoints, visible RGB error, dynamic alpha, decoded seam against the source ratio). B16
  `scene_motion qa` needs the same seam rule on the decoded file. Tests: the four `test_verify_*` synthetic tests.

## 7. Cross-module links that Z must add

- video2dsprite.py (B05): its `package` subparser should call `engine_export.add_package_arguments(pk)` instead of
  its own ten `add_argument` lines (the dest names match), so `--selection`, `--registration`, `--review`,
  `--tiers`, `--key`, `--allow-key-residue` and the runtime hints reach `video2dsprite.py package`; add a `verify`
  verb that calls `engine_export.cmd_verify` (or document `engine_export.py verify`). Until then those flags exist
  only on `engine_export.py package`; the cfed170 namespace still works and gets the 3.0 defaults (residue gate on).
- B05 pipeline-meta.json: `package --key auto` reads `matte.key`/`matte.mode`; keep those names.
- B07 gait_loop/retime: video transports need durations within 1 ms of each other (holds as repeated
  `sourceIndices`); package refuses video formats otherwise and says so.
- B09 runtime export and packed-alpha-runtime.js: crop alpha at `halfWidth` and expect a `2 * halfWidth` video
  (pipeline.md says so); read `durationsMs`, `events`, `fpsRational`, `mobilePackedAlpha`, the registration object.
- Video SKILL.md: link references/pipeline.md; acceptance order package, verify, validate_animation; matte.md (B05)
  should link the residue gate section of pipeline.md; animation-review.md (B07) should say the selection file is
  what `--selection` and a review verdict bind.
- CHANGELOG (Z): the entries in section 4; Appendix H rows "video package" are implemented as stated.
- tests: `tests/test_video2dsprite.py` still holds the 13 originals B05 deletes (B05-T1); they pass against this
  engine_export, so the merge order does not matter.

## 8. Known limitations and what is not proven

- Thresholds: the residue gate (opaque key px 0, ring spill 1%) comes from report v2 on one clip; matte_qa counts
  key-dominant edge colours (crimson, pink, purple outlines) as spill, so such designs need `--key` with another key
  colour or `--allow-key-residue`. The verify gates are Dusk's iOS transport gates (one game) plus synthetic clips.
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
- Not covered by tests: `ffprobe-timestamps` has no negative fixture (forge_av's own tests cover non-monotonic DTS);
  a WebM container whose duration metadata disagrees with its packets.
