# HD-2D backgrounds and selected environmental motion

| Need | Route |
|---|---|
| Fixed battle/title/story | Sharp complete still; optional locked-camera opaque video |
| Living details in a detailed scene | Static high-resolution base plus selected water/fire/cloth/leaf clips |
| Reusable tree/lantern/flag | Registered prop plus transparent/packed-alpha motion and poster |
| Large exploration map | Stable terrain/geometry with a small nearby active-motion set |
| Sparks, dust, glints/trails | Native particles/shaders where useful; no video per spark |

Image-to-video adds organic continuity but can warp identity, geometry and pixels. It is an additional route, not a universal replacement for sprites, tiles or code effects.

## Battle composition and motion routes

Reserve a continuous dry standing plane and enough depth for both parties. Support bands belong in metadata/debug views. Feet must not land in water/cliffs/distant scenery; leave space for commands/portraits and strong effects. Fixed complete paintings may contain buildings/trees; independent gameplay actors remain separate.

**Full-frame sequence:** if requested, generate individual complete frames using the same accepted master. Specify observable phases (flame leans/rises/settles; cloth changes pose), preserve camera/landmarks and inspect loop wrap. Four full-size outputs retain more detail than four cells of a same-size 2×2 image. Prompt dimensions are not guaranteed dimensions. Repeated texture shimmer is not meaningful motion; referencing only the previous generated frame accumulates drift.

**Full-frame image-to-video:** useful for fixed battle/menu scenes when movement and decode size justify it. Lock camera and architecture; define motion amplitude. A looping prompt does not guarantee a seamless loop. Inspect/trim it; ping-pong suits swaying but reverses fire, falling particles or flowing water unnaturally.

**Selected local video:** keep the accepted full-resolution scene sharp and animate only registered regions. Include context and space around moving tips. Preserve world/source anchors and inspect mask/crop boundaries, exposure drift and halos against the base. Hard crop boundaries through moving water often reveal seams.

Use [$video2dsprite](../../video2dsprite/SKILL.md) for conversion, alpha/packed-alpha and clip manifests; use [$generate2dmedia](../../generate2dmedia/SKILL.md) for selected APIs. Ordinary MP4 is not transparent video, and a filename cannot establish codec support.

## Observable motion prompt

```text
Animate the attached accepted [scene/prop], preserving framing, palette, scale,
geometry and support point. Locked camera, no pan/zoom. Only [parts] move:
[direction, amplitude, pacing]. Keep [trunk/roots, stonework, bridge, horizon]
stationary. Preserve identity and art style. No new objects, text, cuts or morphing.
```

Say what moves rather than only “subtle.” A tree's leaf tips sway while roots stay planted; do not breathe the whole silhouette. Water moves while shore/bridge edges stay fixed. Request a loop if supported, then verify it.

## Runtime and mobile

Preserve `sourceSize` and `sourceAnchor` across still/video variants. Source frame, visible crop and encoded packed size are different values. Keep collision/depth baseline on stable ground contact, not changing alpha bounds.

Show a poster immediately. Swap only after a drawable frame is ready with matching crop/anchor; a filename load or resolved `play()` promise does not prove visible pixels. Recover to a poster/valid frame after autoplay/decoder/alpha failure. Test actual supported browsers over light/dark scenes, including iPhone Safari/Chrome.

Budget **concurrent decoders, decoded pixels/sec and GPU uploads**, not only download bytes. Identical instances can share decoded texture/clock when synchronized phases are acceptable. Pause/release offscreen and prior-map clips, prefetch likely neighbors, cap creation and prioritize actors over decorations. Low quality should reduce optional motion/resolution, not break geometry or display black rectangles.

Declare desktop/mobile profiles with active count, resolution/fps, poster policy and priority. No universal count guarantees mobile performance. Measure load, movement, transition and peak battle on target devices; distinguish observed results from inferred savings.

Compare animated/static at gameplay scale. Verify meaningful motion, registration, loop seam, crisp static detail, actor readability, poster alignment, error recovery and previous-scene cleanup. Keep originals/provenance. A returned video remains an intermediate asset until integration checks pass.
