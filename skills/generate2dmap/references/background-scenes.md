# HD-2D backgrounds and selected environmental motion

| Need | Route |
|---|---|
| Fixed battle/title/story | Sharp complete still; optional masked motion on the plate |
| Living details in a detailed scene | Static high-resolution plate plus masked water/fire/cloth/leaf motion from one clip |
| Reusable tree/lantern/flag | Registered prop plus transparent/packed-alpha motion and poster |
| Large exploration map | Stable terrain/geometry with a small nearby active-motion set |
| Sparks, dust, glints/trails | Native particles/shaders where useful; no video per spark |

Image-to-video adds organic continuity but can warp identity, geometry and pixels. It is an additional route, not a universal replacement for sprites, tiles or code effects.

## Battle composition and motion routes

Reserve a continuous dry standing plane and enough depth for both parties. Support bands belong in metadata/debug views. Feet must not land in water/cliffs/distant scenery; leave space for commands/portraits and strong effects. Fixed complete paintings may contain buildings/trees; independent gameplay actors remain separate.

- **Masked motion (default for detailed plates):** the accepted plate stays sharp; only reviewed regions show the clip's motion, composited with one fixed transform. Workflow below.
- **Full-frame sequence:** individual complete frames from the same accepted master, with observable phases (flame leans/rises/settles; cloth changes pose). Four full-size outputs keep more detail than four cells of one image. Repeated texture shimmer is not meaningful motion; referencing only the previous generated frame accumulates drift. Package with `assemble_frames.py` (see Full-frame loops).
- **Full-frame image-to-video:** for fixed battle/menu scenes when movement and decode size justify it. Lock camera and architecture. A looping prompt does not guarantee a seamless loop. Run it through the masked route with one region covering the plate and protected cores on landmarks, so the same loop policy and decoded QA apply.

Use [$video2dsprite](../../video2dsprite/SKILL.md) for transparent actor/prop clips and packed alpha; use [$generate2dmedia](../../generate2dmedia/SKILL.md) for a selected video API. Ordinary MP4 is not transparent video, and a filename cannot establish codec support.

## Masked motion on a static plate

Commands run from the project root; `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs go into new folders inside the project.

1. Accept the plate (opaque PNG). Write `motion-plan.json` (`generate2dmap.motion_plan.v1`, [schema](schemas/map.schema.json)) in plate pixels:

```json
{"schema": "generate2dmap.motion_plan.v1", "sourceSize": [1672, 941],
 "regions": [{"id": "lake", "kind": "polygon", "points": [[252, 448], [1154, 434], [1671, 470], [1671, 530], [443, 569]], "feather": 8, "strength": 1, "motion": "flow"},
             {"id": "pennant", "kind": "landmark", "box": [1443, 94, 1596, 346], "margin": 10, "feather": 3, "strength": 1, "motion": "sway"},
             {"id": "clouds", "kind": "luma_band", "band": [153, 255], "box": [0, 0, 1672, 460], "feather": 6, "strength": 0.8, "motion": "flow"}],
 "protected": [{"id": "bridge", "box": [1299, 341, 1387, 385], "margin": 26, "feather": 14}, {"id": "ground", "box": [0, 580, 1672, 941]}],
 "loop": {"policy": "forward-overlap", "overlap": 16, "range": [4, 140]},
 "encode": {"crf": 18}}
```

   - Kinds: `polygon` (points), `rect` (half-open box), `luma_band` (plate luma in [low, high], optionally inside a box or polygon), `landmark` (an object's box or polygon; `margin` covers tips that move beyond the painted object).
   - A region is `255 x strength` inside the shape grown by `margin` and fades to 0 over `feather` px outside it. Protected cores (shape grown by `margin`) are exactly 0, with their transition (`feather`, default 8) outside the core.
   - `motion`: `flow` (water, falls, mist, clouds, smoke; the default) and `flicker` (fire, candles) only play forwards; `sway` (cloth, foliage, flags, hanging lamps) may reverse.
   - Optional `registration`: `{"fit": "contain"}` (default; undoes a provider centre crop), `{"fit": "cover"}`, or `{"scale": s, "offset": [x, y]}` meaning plate = clip x s + offset.
2. Generate one clip from the accepted plate (prompt below). Review it before any masking (next section).
3. Build the mask; the envelope grows each region to contain every pixel the clip really moves, and reports motion inside protected cores or far from every region:

```bash
python "<skill-dir>/scripts/build_motion_mask.py" --plan scenes/m01/motion-plan.json --plate scenes/m01/plate.png --envelope-from scenes/m01/clip.mp4 --output-dir scenes/m01/mask
```

   Look at `mask-overlay.png` (cyan: motion, red: protected cores, magenta: excluded motion) and `mask-qa.json` (`protectedMax` is 0; `meanOpacity` below 0.07 warns that motion will be invisible).
4. Build the loop. Frames are composited only inside the mask (outside it every pre-encode frame equals the plate), looped by policy, encoded with closed GOPs aligned to the loop, decoded again and gated:

```bash
python "<skill-dir>/scripts/scene_motion.py" build --plan scenes/m01/motion-plan.json --plate scenes/m01/plate.png --clip scenes/m01/clip.mp4 --mask scenes/m01/mask/motion-mask.png --output-dir scenes/m01/loop
```

   Outputs: `loop.mp4` (plate size, padded by one row or column when a dimension is odd), `poster.png` (decoded loop frame 0 over the plate, so swapping to the video changes no pixel), `motion-mask.png` (the mask as applied, including fades where the clip does not reach the plate edge) and `scene-motion.json` (transform, frame map, encode attempts, QA). A frame folder works as `--clip` with `--fps 24`.
5. QA the decoded file again whenever it is re-encoded, copied into a game or compared with another build:

```bash
python "<skill-dir>/scripts/scene_motion.py" qa --report scenes/m01/loop/scene-motion.json --plate scenes/m01/plate.png --output-dir scenes/m01/loop-qa
```

### Loop policy

| Policy | Loop frames from source [a, b) | Use for | Never for |
|---|---|---|---|
| `forward-overlap`, K = `overlap` (default 16) | b - a - K: frames a+K..b-K-1, then K crossfades of tail b-K+j into head a+j | water, falls, mist, clouds, fire, particles | (none) |
| `pingpong` | 2 (b - a) - 2: a..b-1 then b-2..a+1 | sway reviewed as reversible: cloth, leaves, flags | water, fire, falling or flowing things (refused unless every region is `sway`) |
| `forward-overlap` with `overlap` 0 | b - a, played as is | a clip that already loops natively | provider clips without a native loop |

Choose `range` to skip the provider's settle-in and fade-out frames (one shipped scene used [4, 140) of a 145-frame clip). The crossfade never reverses a layer; its ghosting is short (16 frames is 0.67 s at 24 fps). If `source_seam` fails, the wrap already pops before encoding: move `range` or change `overlap`.

## Clip prompt: amplitude and negatives

```text
Animate the attached accepted scene, preserving framing, palette, scale, geometry and every
landmark. Locked camera, no pan/zoom. Only [regions] move: [what, direction, clearly visible
amplitude, pacing]. Keep [stonework, bridge, roofs, ground, horizon] perfectly still. No new
objects, no rings or splashes on wet ground or puddles, no fire outside the lamps, no text, cuts
or morphing.
```

- Say what moves and how much. Prompts full of "very fine", "tiny" and "extremely slow" produced motion nobody could see; a mask covering 18.5% of the frame at 6.96% mean opacity hid the rest.
- Name the negatives that generators add: expanding rings on wet ground, drifting boat bows, extra flags, fire outside lanterns, recoloured cloud blocks.
- Moving tips need room: give a flag or tree crown a `landmark` margin, and measure the real reach with `--envelope-from`.

## Masks cannot fix content

A mask only chooses where the clip shows. Review the clip first and record a verdict (`video2dsprite.review_verdict.v1`: `accepted`, `accept_with_mask` or `fail_regenerate`, with frame, box and description per issue):

- `accept_with_mask`: the problem lies outside every region (an extra flag on a static roof); the plate covers it. `build_motion_mask` reports such motion as excluded.
- `fail_regenerate`: the problem is inside a moving region (rings on the wet ground, a hallucinated boat in the lake, a landmark that drifts). Regenerate the clip; do not shrink the mask until the motion disappears.
- Motion inside a protected core is always excluded (the core stays the plate), and a `registration` warning means the clip is shifted against the plate; fix the transform (`--transform` or `registration`) instead of feathering the seam.

## Decoded QA

Seams are measured on the decoded runtime file, inside the mask, against the loop's own steps. A shipped HD-2D crossfade loop was seamless before encoding, but encoded as one GOP at crf 18 it popped at the wrap: decoded seam 3.90 against an adjacent p95 of 1.76 inside the mask. An aligned closed GOP is necessary but not enough: every keyframe refreshes the picture and pops against the drifted frames before it. When the gate fails, `build` retries with more bits on both sides of the wrap (`wrapQp`: x264 zones at QP crf-8, then crf-12, then crf-14 on each keyframe and the 8 frames before it), then also lowers the crf, and keeps the first encode that passes. The wrap QP is what matters; a lower crf alone barely moves the seam. Measured on 1280x720, 120-frame masked loops: a synthetic drift loop went from 1.35 x p95 at crf 18 (0.93 MB) to 0.68 with wrap QP 10 (1.65 MB); a real lake clip went from 3.37 at crf 18 (1.01 MB) to 1.13 with wrap QP 10 (1.69 MB) and 0.73 with wrap QP 6 (1.98 MB), while crf 14 with wrap QP 10 still gave 1.12. `--ladder off` encodes once; `--allow-seam-fail` keeps a failing loop for review, marked fail.

| Check | Meaning | Gate |
|---|---|---|
| `decoded_seam` | wrap step / 95th-percentile adjacent step, mask-weighted, of decoded x mask + plate x (1 - mask) | fail above 1.0 |
| `frame_count`, `gop_aligned`, `container` | frames as planned; keyframes at 0, g, 2g with g dividing the loop; increasing timestamps, faststart | fail |
| `mean_opacity`, `motion_energy` | whole-plate mean mask opacity; largest in-mask mean RGB change from frame 0 | warn below 0.07 / 2.0 |
| `leak_ring` | decoded change per frame in a 4 px ring outside the mask (what full-frame playback shows) | warn above 0.5 |
| `protected_stability` | decoded drift inside protected cores | warn above 0.5 |
| `region_stability` | each region's own seam ratio; a motionless region is judged as a still (drift at most 1.0) | warn |
| `poster_swap` | poster against decoded frame 0 inside the mask; poster equal to the plate outside it | warn above the p95 step / fail |
| `outside_delta`, `forward_only`, `protected_max`, `source_seam`, `registration` | build only: pre-encode frames equal the plate outside the mask, no reversed layer for flow/flicker, zero cores, pre-encode seam, whole-pixel shift (within 2 px) on static areas | fail / fail / fail / fail / warn |

Never call a loop seamless without these numbers: quote `decoded_seam`, the frame count, fps, keyint and bytes. The thresholds come from one project's scenes and are provisional; offline decoding is not browser or device playback.

## Full-frame loops (generated frames)

For a few complete generated frames (flame phases, a waving banner), package them with the generate2dsprite assembler instead of a video. `--sequence` sets the played order (a mirrored order suits sway only) and `--static-regions` takes a JSON file of named boxes that must not move, such as `{"gate": [610, 220, 860, 540]}`:

```bash
python "<generate2dsprite-dir>/scripts/assemble_frames.py" --input scenes/shrine/f0.png scenes/shrine/f1.png scenes/shrine/f2.png scenes/shrine/f3.png --sequence 0,1,2,3,2,1 --duration 160 --static-regions scenes/shrine/static-regions.json --output-dir scenes/shrine/full-frame-loop
```

The assembler does not register, key or mask the frames; its static-region numbers are diagnostics, not proof of a seamless loop. Inspect the wrap and the landmarks at gameplay scale.

## Runtime and mobile

Preserve `sourceSize` and `sourceAnchor` across still/video variants. Source frame, visible crop and encoded size are different values: crop the decoded loop to `video.crop` (the padding row or column is not art).

- **Masked overlay (recommended):** draw the plate, then the video with `motion-mask.png` as alpha. Static pixels stay the exact plate; the QA measures this composite.
- **Full-frame playback:** play `loop.mp4` alone when the engine cannot mask; outside the mask it shows the plate as encoded, which is what `leak_ring` and `protected_stability` measure.

Show `poster.png` immediately. Swap only after a drawable frame is ready with matching crop/anchor; a filename load or resolved `play()` promise does not prove visible pixels. Recover to the poster after autoplay/decoder failure. Test actual supported browsers over light/dark scenes, including iPhone Safari/Chrome.

Budget **concurrent decoders, decoded pixels/sec and GPU uploads**, not only download bytes: `scene-motion.json` records `bytes` and `decodedPixelsPerSecond` per loop. For scale, one shipped set was five 1672x941 battle loops, 137 frames at 24 fps (5.7 s) each: 5,337,448 bytes of video and 179,455 bytes of masks. Pause/release offscreen and prior-map clips, prefetch likely neighbours, cap creation and prioritise actors over decorations. Low quality should drop optional motion (show the poster), not break geometry or show black rectangles.

Declare desktop/mobile profiles with active count, resolution/fps, poster policy and priority. No universal count guarantees mobile performance. Measure load, movement, transition and peak battle on target devices; separate observed results from inferred savings. Keep originals, prompts, review verdicts and reports; a returned video remains an intermediate asset until the decoded QA and an in-engine check pass.
