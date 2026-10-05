---
name: video2dsprite
description: Animate approved 2D characters, creatures, props or local scene details through image-to-video, then package fixed-canvas frames, alpha video and engine metadata. Supports supplied video, available native tools or the optional generate2dmedia API route. Use for video-derived game animation and transparency/anchor repair; use generate2dsprite for authored discrete pose sheets.
---

# Video-derived 2D animation

An approved still establishes identity and geometry; a video model supplies motion;
deterministic processing prepares it for the engine. This is an optional asset path,
not a promise that video is smaller, seamless, pixel-perfect or cheaper than sheets.

## Choose the route

- **Already have a clip:** process it offline with the bundled scripts. No provider/account required.
- **Native image-to-video tool available:** use its actual supported request schema; retain input, prompt and result.
- **API generation:** use [generate2dmedia](../generate2dmedia/SKILL.md) for provider checks, dry-run, execution and resumable jobs. Codex/Claude can use this route; the processor is not Grok-only.
- **Neither available:** prepare the motion brief and process any supplied clip. Report the missing generation capability; never invent a successful generation.

Use [prompt-rules.md](references/prompt-rules.md) for character/prop/local-background briefs.
Use [pipeline.md](references/pipeline.md) for CLI details, metadata and browser integration.

## Geometry and motion contract

Record `sourceSize` and `sourceAnchor` from the approved art before generation.
Keep camera, body scale, facing and root position consistent. One clip contains one
action; the game owns translation, hit timing, damage and state transitions.

Review contact/identity drift before accepting a clip. Use one constant crop,
scale and translation across a clip; do not crop/resize every frame to its current
alpha bounds. That pumps body size, removes crouches and pins airborne feet.
Do not freeze the original still's alpha over moving RGB: it clips moving leaves,
hair and weapons. Capture dynamic alpha, or retain an opaque local patch + mask.

For idle/walk, trim an actual cycle and inspect the seam. For attacks, export a
one-shot and return to idle in the engine; don't ping-pong an impact. Matching
first/last generation references can help but cannot guarantee intermediate motion
or a seamless loop. More sampled frames do not fix drifting geometry.

Use [animation-review.md](references/animation-review.md) for the local frame
scrubber, light/dark alpha review, candidate intervals and byte-preserved cuts.
Recurrence suggestions require visual phase/contact review; they do not certify a loop.

## Process and package

`Pillow` and `numpy` are required. ffmpeg is optional for supplied PNG frames and
required for decode/video encoding. Inspect installed encoders:

```bash
python skills/video2dsprite/scripts/video2dsprite.py doctor
```

For an opaque magenta-key clip, select a reviewed interval. This example decodes
2 seconds at 12fps; adjust to the actual motion, not a universal production default.

```bash
python skills/video2dsprite/scripts/video2dsprite.py process --video motion.mp4 --out-dir work --start 1 --duration 2 --fps 12 --key-mode magenta --frame-counts 12,24 --playback-duration 2
python skills/video2dsprite/scripts/video2dsprite.py package --clean-dir work/frames-clean --out-dir assets/hero-idle --name hero-idle --fps 12 --source-size 448,448 --source-anchor 224,430 --max-side 320 --formats png,webm,packed --loop
```

The geometry numbers must come from this asset. Default packaging keeps the source
canvas; optional `--crop-union` stores a shared `sourceRect`. Output encoding size
may shrink while `sourceSize`/`sourceAnchor` remain original art coordinates.

`extract`, `clean`, `sample`, `process` remain supported. `sample`/`process` now use
one fixed envelope; `--registration legacy-per-frame` exists only for reproducing
old output. Package from **clean source frames**, not normalized sample cells.
`--key-mode auto` preserves existing alpha; `none` skips keying. Transparent VP9
WebM extraction needs `--decoder libvpx-vp9` because some ffmpeg decoders drop alpha.
If a magenta fringe remains, `--despill 0.5` optionally reduces magenta only at the
first visible edge; review intentional violet edge colors before accepting it.
`package` requires a fresh output directory and publishes only after successful encoding.
For detailed pixel sheets or careful contact registration use `generate2dsprite`.

## Runtime and acceptance

Deliver `animation.json`, `animation-qa.json`, poster, paged PNG fallback and the
requested video transports. Packed H.264 stores RGB left / grayscale alpha right;
it is **not a natively transparent MP4**. A working Canvas2D reconstruction helper
and anchor-aware draw function are in [packed-alpha-runtime.js](references/packed-alpha-runtime.js).

Show the poster until the first decoded frame. Use the reconstructed canvas as the
drawable, not the packed source video. Pause/release invisible assets; share decoders
for identical clips, cap resolution/fps/active videos for the target device, and
serve over HTTP with correct media headers. Verify actual iPhone playback and
frame time; `canPlayType()` and successful desktop decode do not prove alpha or FPS.

Inspect at game scale: silhouette/face stability, feet/root contact, moving alpha
edges over light/dark backgrounds, loop seam, action readability and transitions.
The report measures frame differences and bounding-box drift; it always retains
`needs-visual-review` rather than falsely certifying identity/contact/seam quality.
Compare actual package bytes and runtime decode cost before replacing sheets.
Integrate into the game only when that is within the user's requested scope.
