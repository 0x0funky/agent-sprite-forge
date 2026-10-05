---
name: generate2dsprite
description: Create reference-consistent game characters, creatures, props and FX as static assets, sprite sheets or explicit animation clips. Plan motion, generate artwork with the available image tool or selected API, and package alpha, scale, anchors and timing for a game runtime. Route fluid image-to-video animation to video2dsprite.
---

# Generate 2D Sprite

Create the smallest useful asset bundle for the requested game. Match its camera, art style and actual display size. Use [generate2dmap](../generate2dmap/SKILL.md) for scene geometry and structural props. Game assembly is separate from asset generation; keep authored collisions and combat timing independent of the artwork.

## Choose the representation first

| Need | Route |
|---|---|
| Static master, prop, strict pixel animation or a few deliberate poses | Generate an image or coherent per-action sheet, then this skill's processor. |
| Already registered PNG frames with intentional flight, recoil or bob | `build_animation_clips.py`; preserves source canvases and explicit timing. |
| Fluid breathing, hair/cape motion, creature attacks, animated trees or other living set pieces | Accept one master here, then [video2dsprite](../video2dsprite/SKILL.md). See [video handoff](references/video-handoff.md). |
| Four full-resolution scene phases or complete rectangular frames | `assemble_frames.py`; no keying, silhouette crop or per-frame alignment. Plan geometry with the map skill. |
| Slashes, hit sparks, light bursts needing exact reactive timing | Separate FX layer. Generated sheets/video or runtime particles can serve the requested look; do not bake screen-wide FX into a character body. |

Read [prompt-rules.md](references/prompt-rules.md) for generation, [character-animation.md](references/character-animation.md) for motion and clip contracts, and [processing.md](references/processing.md) for commands/QC. [modes.md](references/modes.md) covers legacy names and bundle choices. Read only relevant references.

## Establish one asset contract

Record identity/reference, camera/facing, style, intended game size, source dimensions, action phases, loop versus one-shot, full motion envelope, shared root and output format. For a bundle, keep an accepted master, one source-to-display scale and compatible action canvases. `3x3` is nine cells, not a guarantee of smoothness: pose spacing, timing, loop seam and ground contact matter more than filling a grid.

Choose alpha and sampling explicitly:

- `native_alpha`: preserve real transparency and color, including purple. Inspect light and dark composites; painted checkerboards are not alpha.
- `chroma_key`: use uniform flat magenta only when clean keying is needed. Avoid magenta in the subject. Inspect halos; optional despill is a color heuristic, not alpha reconstruction.
- `opaque`: retain complete opaque artwork. Use the full-frame assembler for backgrounds so sprite fitting cannot alter the composition.
- `nearest` for a verified pixel grid; `lanczos` for smooth HD art. High-resolution pixel-like painting is not automatically clean 32/64/96-pixel art. Review at gameplay size before expanding a bundle.

A shared root is not a per-frame alpha-bbox bottom. Preserve intentional run flight, jump, breathing and recoil. Feet fitting is a repair tool for genuinely grounded poses, not a universal animation step.

## Generate using real capabilities

Use the host's available image-generation tool, composing its image skill when available. Honor an explicitly selected provider. For direct OpenAI/xAI API generation, use [generate2dmedia](../generate2dmedia/SKILL.md); its commands are dry-run until `--execute`, and a subscription is not evidence of API access or API credit. Verify the active tool/model/options; do not claim a model version or returned resolution from prompt text.

Write the art prompt directly. Inspect local references with the host's image viewer, then attach them through the real reference-image input. A filesystem path written in prose is not a visual reference. Keep the accepted master in every separate generation; a prior frame may supplement it. Describe absolute motion phases rather than accumulating edits that drift identity.

Generate one action family at a time for important playable characters. Compact multi-row sheets usually contain actors better; requested/proven strips, directional locomotion and coherent long actions remain valid. Final atlas shape is packaging. Keep body plus held equipment separate from wide slashes, trails, projectiles and hit bursts so FX bounds cannot shrink the body.

Code can make layout guides, process pixels, display assets and implement requested procedural FX. It must not quietly substitute placeholder art for requested generated characters. If a tool is unavailable, preserve a prepared prompt/contract and report the missing capability rather than pretending generation happened.

## Process and review

Preserve raw outputs, prompts/reference roles and measured dimensions. Prefer direct registered-frame packaging when the frames already share a canvas/root. For normalization, select the processor's background, resampler, component policy, scale and anchor deliberately; see [processing.md](references/processing.md).

Create one scale profile from an accepted grounded reference action and reuse it for compatible body actions. It prevents processor magnification changes, not model anatomy drift. `preserve` still translates subjects to a detected anchor; do not use it on motion that needs those offsets.

Check actual game-size playback: silhouette and identity, coherent contact/flight, loop seam or one-shot recovery, correct alpha over light/dark, complete extremities, scale across actions and runtime root/collider placement. Numeric QC detects symptoms and does not approve anatomy or motion. Failed strict QC must not leave an apparently finished bundle; scripts publish to a new directory only after checks pass. Retain the last accepted bundle when trying a revision.

Deliver accepted source art, runtime frames/clips or video contract, preview, prompt/provenance and QC with any remaining limitations. Do not promise that video is always smaller or faster than a sheet: decoded surfaces, concurrent decoders and device alpha support also determine the budget.
