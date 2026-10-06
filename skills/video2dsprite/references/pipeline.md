# Processing, packaging and runtime contract

The processor makes no network calls. Generation is a separate step (host tools, the sibling
`generate2dmedia` adapter, or a clip the user supplies). Run every command from the project
root; `<skill-dir>` is this skill's folder (in Claude Code: `${CLAUDE_SKILL_DIR}`). Outputs go
into the project, never into the skill folder.

## Files, in pipeline order

```text
work/<clip>/                       video2dsprite.py process --output-dir work/<clip> (a new folder)
  frames-raw/frame_000000.png ...  decoded source frames, 0-based, original timing
  frames-clean/clean_0000.png ...  keyed straight-alpha RGBA frames (the package input without registration)
  frames-clean/matte-report.json   matte report: the key the matte used (package --key auto reads it)
  sprite/                          sampled comparison sprites, strips and preview GIFs
  pipeline-meta.json               processing record; its "matte" block repeats the matte report
  README.txt
work/<clip>-reg/                   register_clip.py apply --output-dir (a new folder)
  frames/frame_000000.png ...      registered frames on the master canvas: the package input
  registration.json                registration record (package --registration; it names keyColor)
  review-contact.png
work/<clip>-loop/selection.json    gait_loop.py select --output-dir (a new folder); retime.py and
                                   animation_review.py select write selection.json the same way
review.json                        review verdict (video2dsprite.review_verdict.v1, optional)
game/<character>/<state>/          engine_export.py package --output-dir (a new folder)
  animation.json                   runtime manifest, schemaVersion 3.0
  animation-qa.json                the manifest's QA envelope as its own file
  provenance.json                  tool, version, parameters and sha256 of every input
  <name>-poster.png                first frame on the encoded canvas
  <name>-atlas-00.png ...          PNG fallback pages, at most 4096 px per side
  <name>.webm                      VP9 with native alpha (--formats webm)
  <name>-packed.mp4                H.264, RGB left and alpha right (--formats packed)
  <name>-packed-<tier>.mp4         mobile packed tiers (--tiers)
  verify-qa.json                   engine_export.py verify, written only when it passes
```

Every tool writes into a new folder and refuses an existing one, so each step gets its own
folder. The selection comes from the motion tools (gait_loop select, retime, or
animation_review select); run them on apply's `frames/`. The registration file is
register_clip's `registration.json` (best: it records the padding apply used) or the
prepare_i2v_input job (`video2dsprite.registration_job.v1`). Keying is explained in
[matte.md](matte.md). Contracts for every file are in
[schemas/video.schema.json](schemas/video.schema.json) and
[schemas/common.schema.json](schemas/common.schema.json).

## Package

The smallest call packages every clean frame at a fixed rate (the key comes from
`frames-clean/matte-report.json`):

    python "<skill-dir>/scripts/engine_export.py" package --clean-dir work/hero-walk/frames-clean --output-dir game/hero/walk --name walk --fps 12 --loop --formats png,webm,packed

The canonical call packages registered frames with their selection, registration and review,
plus a mobile tier:

    python "<skill-dir>/scripts/engine_export.py" package --clean-dir work/hero-walk-reg/frames --selection work/hero-walk-loop/selection.json --registration work/hero-walk-reg/registration.json --review review.json --output-dir game/hero/walk --name walk --formats png,webm,packed --tiers actor --body-height-px 300 --speed-ref 3.65

`python "<skill-dir>/scripts/video2dsprite.py" package ...` and `video2dsprite.py verify ...`
are the same verbs with the same flags (`--out-dir` stays the old name of `--output-dir`).

Inputs and rules:

- `--clean-dir` frames are read sorted by name (`clean_*.png` when present, otherwise every
  PNG); they must share one canvas. At most 128 million decoded pixels per package.
- Timing comes from `--fps` (`12`, `24000/1001`) or from `--selection`. A v1 selection packages
  `[start, endExclusive)` at its `fps`; a v2 selection supplies 0-based `sourceIndices`
  (repeats are holds), `durations_ms`, `loopPolicy`, `events`, `impactMs`, `holdMs`,
  `cadenceMs` and `strideWorldUnits`. Its `sourceHashes` must still match the frames, or the
  selection is stale and refused.
- Events may sit anywhere from 0 to the clip's end edge (`atMs` equal to the duration, as the
  `end` event of `retime.py --ticks`); an event on the end edge names the last frame.
  `impactMs` and `holdMs` must lie strictly inside the clip.
- `--loop` (or `--loop-policy cycle`) loops; `--loop-policy pingpong` is baked into one cycle
  (`0 1 2 3 2 1`, recorded as `pingpongBaked`); `oneshot` plays once.
- Rates above 60 fps are reduced to at most 60 fps without changing the clip length
  (`fpsCapped`, `inputFps`).
- Video transports play one constant rate. Uneven durations that are whole ticks (`retime.py
  --ticks` rows at the selection's `tickHz`, or held frames that `gait_loop` merged at the
  source fps) are expanded into repeated frames at that rate, at most 60 fps, keeping every
  frame edge within 1 ms and the clip length to the millisecond; `tickExpansion` records the
  authored frames and ticks. A PNG-only package keeps the uneven durations. Durations that are
  whole ticks of no such rate package only as PNG; retime them onto a tick grid
  (`retime.py --ticks`).
- `--registration`: register_clip's `registration.json` carries the padded canvas
  (`sourceSize`, `sourceAnchor`), the base canvas and the padding apply used. A registration
  job means registration by construction: the job's base canvas (its `sourceSize` and
  `sourceAnchor`; for a `--view-box` job that is the view, not the whole sheet) plus `padding`
  [left, top, right, bottom] is `sourceSize`, and the base anchor moves by (left, top). If
  `register_clip.py apply` ran with `--action-padding`, pass the same `--action-padding` with
  the job (or pass registration.json). Conflicting `--source-size`/`--source-anchor` values
  are refused.
- `--review`: the verdict's `reviewedSha256` must be the sha256 of the selection file, or the
  `inputDigest` that package prints (sha256 of the newline-terminated list of frame sha256s in
  playback order). `accepted` gives reviewStatus accepted and lets QA pass;
  `accept_with_mask` gives a warn; `fail_regenerate` refuses the package.
- Key-residue gate: every packaged frame is measured with forge_matte.matte_qa against the key,
  on a copy with alpha <= 16 cleared (registration's resampling leaves invisible alpha 1-4
  halo with invented key-leaning colours; a visible key fringe still counts). `--key auto`
  takes the key from `<clean-dir>/matte-report.json`, then from the `--registration` keyColor
  (registered frames), then from `--pipeline-meta`, else magenta, and records `keySource`;
  `--key green` (or `#rrggbb`) forces one, `--key none` skips the gate for native-alpha
  footage. Any opaque key pixel, or key-coloured spill on more than 1% of the outer ring,
  refuses the package and publishes nothing. `--allow-key-residue` ships it anyway and records
  the override (QA status warn). Re-keying with the soft matte is the fix.
- Resizing is one crop (full canvas, or `--crop-union` over the whole clip) and one resize on
  premultiplied channels to at most `--max-side` px; it never upscales. `--pixel-art` samples
  nearest and only reduces by whole factors.
- The output folder must not exist. Everything is written to a staging folder beside it and
  published only after every encode and decode check passed.

The command prints one JSON line with the output folder, the metadata file (animation.json,
also as `manifest`), the QA and provenance paths, the QA status, reviewStatus, frame count,
rational fps, duration, keySource and inputDigest.

## animation.json 3.0

Every 2.0 key is kept (`fps` stays a number, `registration` becomes an object, `qa` becomes a
QA envelope that still carries the 2.0 diagnostics). 3.0 adds:

| Field | Meaning |
|---|---|
| `fpsRational` | exact transport rate, `"num/den"` |
| `sourceIndices` | 0-based source frame per packaged frame |
| `durationsMs` | whole milliseconds per frame; the timeline |
| `loopPolicy` | `cycle` or `oneshot` (pingpong is baked into a cycle) |
| `events` | `{name, atMs, frame, data?}`; `frame` is the frame shown at `atMs` (the last frame on the end edge) |
| `impactMs`, `holdMs` | gameplay instants inside the clip |
| `terminal` | the clip ends its state and holds the last frame |
| `cadenceMs`, `strideWorldUnits`, `speedRef`, `cycles` | walk phase from distance travelled |
| `registration` | `{mode: construction, fixed-envelope or preserved, jobSha256?, baseSize?, baseAnchor?, padding?, legacyMode}` |
| `sampling`, `pixelArt`, `bodyHeightPx`, `shadow`, `displayScale`, `budgetClass` | runtime hints |
| `packedAlpha` | `layout`, `width`, `height` (content), `halfWidth`, `halfHeight` (even halves), `fps`, `keyframes`, `fullDecodePassed` |
| `mobilePackedAlpha` | one record per tier (below) |
| `fpsCapped`, `inputFps`, `pingpongBaked` | present when they apply |
| `tickExpansion` | `{rate, ticks, authoredSourceIndices, authoredDurationsMs}`: uneven whole-tick timing repeated at one rate for video |
| `provenanceFile`, `artSource`, `placeholder` | provenance.json, `video`, false |

`hitEvents` (2.0) lists the `hit` events. File names in the manifest are bare names inside the
package; no absolute path is ever written. Inputs outside the package are recorded in
provenance.json by file name and sha256.

## Geometry

`sourceSize`, `sourceAnchor` and `sourceRect` (`[x, y, width, height]`) are approved art
units and never change with the media size, a tier or the frame rate. `contentSize` is the
media pixels of `sourceRect`; `encodedSize` adds the even right/bottom padding. For world anchor
`(x, y)` and scale `s`, draw the `contentSize` pixels of any transport to:

```text
left   = x + (sourceRect.x - sourceAnchor.x) * s
top    = y + (sourceRect.y - sourceAnchor.y) * s
width  = sourceRect.width * s
height = sourceRect.height * s
```

Example: art 448x448 with anchor [224, 430], media 320x320 and an actor tier. Geometry stays
448x448 and [224, 430] at every size, so contact and scale match across devices. Collision
and hitboxes are gameplay data; never derive them from animation alpha.

## Transports and mobile tiers

- PNG fallback: always written; pages record `firstFrame`, `frameCount` and `columns`.
- WebM: VP9 with an alpha plane (decode with libvpx-vp9; native decoders drop alpha).
- Packed MP4: H.264 Main, BT.709 limited range, faststart. RGB is on the left, alpha (red
  channel) on the right; each half is the content padded right/bottom to even pixels
  (`width` 403 gives `halfWidth` 404). Crop RGB at `(0, 0, width, height)` and alpha at
  `(halfWidth, 0, width, height)`.
- Loops are encoded as one closed GOP per loop (keyint = frame count), so the wrap is a
  keyframe boundary. Alignment is necessary but not sufficient; `verify` measures the seam.
- `--tiers` adds packed files sized for phones. Default tiers (Dusk 1.8.3 mobile budgets):

| Tier | Long edge | Max fps |
|---|---|---|
| `actor` | 320 px | 24 |
| `prop` | 384 px | 12 |
| `fx` | 512 px | 30 |

  Custom tiers are `name:EDGE@FPS` (`actor:256@20`). A tier scales the packaged content
  (premultiplied, never upscaled) and resamples time to at most its fps without changing the
  clip length. Each record has `tier`, `sourceWidth`/`sourceHeight` (the content it was scaled
  from), `width`/`height`, `halfWidth`/`halfHeight`, `fps`, `frameCount`, `durationMs`,
  `inputIndices` (positions in the main timeline), `encodedAnchor` (the anchor in tier pixels),
  `file` and `sha256`. Draw a tier with the same source geometry as the main media.

## Verify

    python "<skill-dir>/scripts/engine_export.py" verify --package game/hero/walk

(or `video2dsprite.py verify --package ...`). Decodes every encoded file completely and compares
each frame with the lossless atlas:

| Check | Gate |
|---|---|
| alpha mean absolute error, worst frame | at most 3.5 / 255 (p99 and max are reported) |
| decoded alpha where the source is transparent | median at most 1 |
| decoded alpha where the source is opaque | median at least 253 |
| straight RGB error where source alpha > 16 | at most 12 / 255 |
| dynamic alpha | source alpha that moves must still move after decoding |
| codec, dimensions, packets, decode timestamps | match the manifest |
| duration | within 2 ms of the manifest |
| packed MP4 | `moov` before `mdat` (faststart) |
| WebM | VP9 AlphaMode tag present |
| loops: decoded seam / adjacent p95 | at most 1.10 x max(1, source ratio) |

It writes `verify-qa.json` (a QA envelope bound by sha256 to animation.json, the atlas, the
poster and every encoded file) only when every gate passes; otherwise it prints the failed
checks and writes nothing. A failing seam on a subtle loop usually needs a lower `--crf-packed`
or `--crf-webm`, not a different GOP.

## Validate

    python "<skill-dir>/scripts/validate_animation.py" game/hero --require-states idle,walk,attack --require-verify --static-sprite game/hero/hero.png --static-anchor 224,430

Accepts package folders, animation.json files or a folder of packages. Each failure names its
rule: schema (the vendored animation_v3 schema, always checked), paths, files, timing (with a
`tickExpansion` that must repeat into `sourceIndices`), anchor, impact-hold, events (end edge
allowed), loop-flag, states, poster, padding-embed, packed-geometry (logical `width`/`height`
never above `halfWidth`/`halfHeight`), tiers, static-sprite, ffprobe-dimensions,
ffprobe-timestamps, ffprobe-duration (2 ms), ffprobe-packets, vp9-alpha, the hash-bound QA rules
(qa-incomplete, qa-inconsistent, qa-failed, qa-stale, qa-partial, qa-foreign), verify and
review. Every rule has a negative fixture in tests/fixtures/animation/negative/. QA that misses
a file, lists a foreign one, or judged other bytes than the files now on disk is refused, so
edit nothing after packaging: repackage. `--report <new file>` writes a QA envelope when every
rule passes. Acceptance order: package, then verify, then validate_animation.

## iOS transport and runtime

- Browsers with working VP9 alpha play the WebM. iPhone and iPad play the packed MP4 through a
  compositor; a packed MP4 drawn as a plain video shows the grey alpha half beside the art.
  `canPlayType()` or a playing video does not prove the alpha is right; test on a device.
- One shared WebGL compositor rebuilds RGBA from the two halves (alpha from the right half's
  red channel) into a canvas. A decoder pool hands out leases: `lease.video` is the element to
  play, pause and seek; `lease.drawable` is what to draw (the video on the native path, the
  rebuilt canvas on the packed path); `lease.frameVersion()` changes when a new frame is ready.
  Always draw `lease.drawable`, never the packed video itself.
- Draw the PNG poster until the first decoded frame exists, and fall back to the PNG atlas when
  decoding or compositing fails; never leave a black rectangle or an endless loading state.
- [packed-alpha-runtime.js](packed-alpha-runtime.js) is a CPU Canvas2D reference for one clip
  (correctness, not throughput). Under the 3.0 contract it must crop alpha at `halfWidth` and
  expect a `2 * halfWidth` wide video.
- The application owns play/pause, seeking, unloading (`removeAttribute('src'); load()`), CORS
  for cross-origin pixel reads and visibility. Avoid `file://` pages for video textures.

## Decoder budgets

Measured on one shipped game (Dusk 1.8.3); starting points to re-measure, not guarantees:

- at most 6 live video resources while exploring on a phone, controllable actors first;
- at most 3 packed MP4 downloads at a time, cancelable;
- device pixel ratio capped at 1.25, world drawing budgeted at 30 fps (logic keeps its rate);
- distant or over-budget props show their poster; one decoder is shared by identical clips;
- release world video when a battle starts; pause and release on tab switches.

Compressed bytes do not predict decoder, memory or GPU cost. Preload only the current scene's
actions.

## Honest scope

- Proven offline: file hashes, container facts, complete decodes, alpha and colour error
  against the lossless atlas, durations, loop seams on the decoded file, and the manifest
  contract.
- Not proven: Safari, WebGL or iPhone playback, autoplay rules, decoder throughput, memory and
  thermal behaviour, and the visual quality, identity and motion of the art. A similar first
  and last frame does not prove a seamless gait; review the clip at runtime scale.
- The key-residue thresholds were set on one clip; the verify gates come from one game's iOS
  transport and synthetic clips. Game code decides when an attack lands; never trigger damage
  from video `ended` or decoded frame counts.
- Exit codes: usage errors exit 2; a refused input or failed gate prints `error: ...` and exits
  1 with nothing published; an unexpected failure prints `error: internal error (...)`.
