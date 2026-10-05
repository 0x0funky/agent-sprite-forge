# Generation prompts

Compose a brief for the chosen tool's actual image and reference contract. Host image tools and an explicitly selected API adapter are both valid; the provider decides which sizes, alpha and reference options exist. Save the settings you requested and inspect the returned pixels separately: a prompt is a request, not a guarantee.

Ready-made phase lists, grid shapes and project presets are in [action-recipes.md](action-recipes.md).

## Prompt structure

Write these parts in order. Numbers beat adjectives: give the canvas, the palette and the scale as values.

1. **Asset and camera.** Side elevation, top-down, three-quarter or 2:1 isometric; facing; what the asset is for.
2. **Identity.** Name each attached reference and its role ("Reference 1 fixes identity, palette and anatomy; Reference 2 is layout only"). List the invariants: silhouette, head shape, costume colour groups, held equipment, light direction.
3. **Canvas and scale.** The logical canvas in game pixels and the body height, for example "a 48x64 logical-pixel canvas, body about 44 logical px tall, drawn at 8x so every logical pixel is an 8x8 block". State the grid ("8 poses in 2 rows x 4 columns, read left to right, top row first").
4. **Palette.** An explicit list with a colour cap, for example "use only these 12 colours: #1a1c2c, #5d275d, ...; no gradients outside them". Keep the list in a palette file (below) and paste it into every call.
5. **Style rules.** Outline, anti-aliasing and light (next section).
6. **Motion.** One line per phase, with NEAR/FAR limbs for side views and the allowed motion per phase.
7. **Registration and containment.** One canvas, one anatomical scale, one root; the whole motion envelope fits each cell's safe box.
8. **Background.** Real transparency if the tool supports it; otherwise one flat chroma colour absent from the subject. No text, labels, numbers, grid lines or painted checkerboard.

## Aspect, not pixels

A host image tool without a size parameter keeps the requested aspect and returns about 1,572,864 px (report v2, 3 of 3 calls, plus one 2:1 sheet): 3:2 gives 1536x1024, 16:9 gives 1672x941, 1:1 gives 1254x1254, 2:1 gives 1774x887. Pixel sizes written in the prompt were not obeyed. So:

- Ask for an aspect ("Request aspect 3:2"), never for pixel dimensions. Describe the grid and the logical canvas instead.
- Plan the grid for the predicted size: `plan_guide.py` lists layouts and their per-pose room for every standard aspect.
- Process the size that actually came back (process, `--pad-to-grid` or rounded slicing); never assume the requested one.
- An API route with a real size parameter may request exact sizes; still verify the returned image.

## Pixel art: logical pixels, palette, outline, anti-aliasing, light

For strict pixel art, describe the art at game size and let the model draw it enlarged:

- **Logical pixels.** "Every logical pixel is one solid square block at 8x; same block size everywhere; no half blocks." Image models only approximate this (the trial fox had 5.6-6.5 px blocks instead of 8), so reduce with integer nearest (`scale_frames.py --scale-from 1/8 --resampler nearest`) or a palette reducer, and inspect at game size.
- **Palette and cap.** Give the hex list and the cap ("at most 16 colours, including outline and highlights"). Save it as a `generate2dsprite.palette.v1` file (`colors[{hex, name?, reserved?}]`, `transparent_index`, `locked`, `source`; contract in [sprite.schema.json](schemas/sprite.schema.json) `palette_v1`) and reuse it for every action of the character.
- **Outline.** Say which: "1 logical px dark outline around the silhouette" or "selective outline: darker shade of the local colour, no black". Keep the outline colour in the palette.
- **Anti-aliasing.** "No anti-aliasing against the background; hard silhouette edges." Interior AA, if wanted, uses palette colours only.
- **Light.** One light direction for every frame ("light from the upper front-left; three tones per material: light, base, shadow"). Changing light between frames reads as flicker.
- **Readable at game size.** Ask for "readable at 48 px tall": big shapes, a clear face, 2-3 identity details; drop micro-texture that turns into noise.
- **No era labels by default.** Do not write 16-bit, retro or console-era style words unless the user asks for that look; they add dithering and noise. A general sprite brief says "clean pixel art" or matches the project.

## Identity: master first, turnaround first, character lock

- **Master first.** Generate and accept one master (neutral pose, final camera, intended scale) before any action sheet, then attach it to every later call as the identity reference. Identity drift is cheaper to prevent than to repair.
- **Turnaround first.** For a character that needs several directions, accept a turnaround (front, side, back; or the 8-direction set) at one scale before generating directional walks from it. For 2:1 isometric units use the two or four diagonal views the game needs.
- **Character lock.** Repeat the invariants in every call: "same head shape, ear size, eye, muzzle, costume colours, belt and boots, tail and outline weight in every cell; only limbs, tail sway and body height change".
- **Single-shot identity check.** When only one generation call is possible (no master), the sheet's frames are each a redraw. Check them before use: `sheet_qc.py frames` reports each frame's head ratio against the median frame (and a match residual for face or costume drift); give `--master` when a master exists. Regenerate or repair frames outside the tolerance instead of shipping a face that pops.

## NEAR and FAR limbs, never left and right

In a side view the model cannot see which leg is the character's left. Name limbs by depth:

- **NEAR** limbs are on the viewer's side: normal brightness, drawn in front of the body.
- **FAR** limbs are on the away side: one shade darker, partly hidden behind the body and the NEAR limbs.

Write the phase list with NEAR/FAR: "0 contact: NEAR leg reaches forward, heel on the ground; FAR leg trails. 4 contact: FAR leg forward, NEAR leg trails." Then say "frames 4-7 must not repeat frames 0-3: the leg drawn in front swaps". The trial fox had the NEAR leg forward in 8 of 8 frames, which plays as a limp. `sheet_qc.py frames --cycle run` checks the alternation from the NEAR/FAR shades (declare them with `--near-colors` and `--far-colors`). Front and back views can still say left and right foot, because both feet are visible.

## Body only, no trails

For playable characters, generate the body and held equipment only: no slash arc, weapon trail, muzzle flash, projectile, impact burst, aura, dust or ground plate. The body bbox then stays near the idle and run size and the feet stay on the shared root. Generate the effect as its own `fx`, `projectile` or `impact` sheet (or with codeart2d) and layer it at runtime. Reject a body action whose body is more than 10-15% smaller than idle because a wide effect forced it to shrink.

## Cell containment and recovery

Prompt for the **full motion envelope** inside each cell, not a large body that fills 80% of every pose. Leave a 15% safe margin on every side, keep one anatomical scale, and count the longest tail, widest stride, ears and held equipment. Say that no part may touch or cross a cell edge and that gutters and separators must not be drawn. These margins are a request, not evidence.

Check the raw sheet before slicing, then slice by ownership so nothing is cut:

    python "<skill-dir>/scripts/sheet_qc.py" spill --input raw/run-sheet.png --rows 2 --cols 4 --output-dir qc/run-spill

The report names each part that crosses a cell line, its owner cell, the pixels over the line and the overhang (the trial fox: 58 px of frame 3's tail over the line, 6 px overhang). A crossing part is cut or lands in the neighbour when a grid slicer runs. If the parts are complete in the raw sheet, slice by component ownership (`--sheet` in `sheet_qc.py frames` and `scale_frames.py`, or the assemble step with ownership slicing): every frame keeps its cell origin plus one shared padding, so registration is unchanged. If a part is missing or merged with a neighbour, regenerate that sheet with a smaller motion envelope. Do not enable a largest-component filter to hide a crossing: it deletes intended pieces such as sparks.

## 2:1 isometric

For isometric tactics units, props and tiles, say the projection with numbers: "2:1 dimetric isometric: ground lines rise 1 px for every 2 px across (26.57 degrees); a floor tile is a diamond twice as wide as tall; vertical edges stay vertical; no perspective". Give the footprint ("the unit stands on one 32x16 diamond; its root is the diamond centre") and the facing ("faces south-east toward the viewer"). Keep the light from one fixed screen direction for every facing. Register isometric units on the footprint centre, not the bbox bottom.

## Motion decisions that prevent recurring defects

- **Grounded idle:** restrained torso compression and secondary hair or clothing motion. For a massive boss, fixed feet and pelvis with chest and core pulses; no whole-body side-to-side slide.
- **Run and walk:** distinct contact, down, passing and flight (run) or up (walk) phases, then the same phases with NEAR and FAR swapped. Keep planned vertical body motion and flight; pinning every lowest pixel to one line makes skating or dead motion.
- **Attack, cast, shoot:** one body action per raw sheet; effects separate (body only, above).
- **Long quadruped or serpent:** keep the torso and root registered and every pose inside one shared silhouette envelope (central 70-72% of the cell); express bites and pounces through local articulation.
- **Jump, knockback, flying:** shared source root and motion-relative phases; not a fixed feet line.
- **Ground-contact fire:** one fixed ignition baseline at a fixed share of the cell height; moving flame tips; no baked floor plate or shadow.
- **Projectile and impact:** keep travel direction and origin; leave room for expansion and fade; do not normalise every phase to one silhouette size.

## Sheet, full frames, or video

Choose the useful phases and the detail per frame before the sheet size. A 2x2 suits a restrained four-pose loop; 2x3 or 3x3 adds phases; more cells mean fewer source pixels per pose. A 4x4 directional walk is one locomotion family; a hero's unrelated idle, run, attack and death are generated separately and packed later.

Individual full frames keep more detail but cost more calls and need stronger identity and registration checks: keep the same master and camera for every phase and never resize individual canvases. For fluid secondary motion, an accepted master followed by a short image-to-video sample is often better than more sheets; see [video-handoff.md](video-handoff.md). Video does not guarantee stable pixels, transparency, a loop seam or foot contact.

## Optional guides

- `make_layout_guide.py` draws cell geometry only.
- `make_anchor_layout.py` repeats an accepted master at the requested scale and root.
- `plan_guide.py` (opt-in) predicts the host size for each aspect, picks the grid with the most room per pose inside a 15% safe frame, and writes a guide with gutters, safe boxes, a ground line, root ticks and, for run and walk cycles, a NEAR (orange) / FAR (blue) leg skeleton whose leading leg alternates; it also writes an aspect-only prompt block. Its self-check runs the same leading-leg test as `sheet_qc.py`. It has not been A/B tested with an image model, so use it when sheets keep crossing cells or repeating the leading leg, not by default:

      python "<skill-dir>/scripts/plan_guide.py" --frames 8 --cycle run --facing right --output-dir guides/hero-run

Attach a guide as a real reference image and state: "Use this only for slots, scale, root and pose; do not reproduce its boxes, lines, colours, labels or stick figures." A grounded anchor template constrains a reference root, not every pose's visible feet; for flight, recoil, jumps or changing silhouettes use a motion envelope instead. A processing profile cannot fix inconsistent anatomy.

For map props, square packs suit compact rocks, crates and shrubs. Bridges, platforms, walls, doors, houses and large trees need explicit native dimensions, joins and collision geometry: route them to the map skill.

## After generation

Run the checks in this order, from the project root:

    python "<skill-dir>/scripts/sheet_qc.py" spill --input raw/run-sheet.png --rows 2 --cols 4 --output-dir qc/run-spill
    python "<skill-dir>/scripts/sheet_qc.py" frames --sheet raw/run-sheet.png --rows 2 --cols 4 --cycle run --game-pixel 8 --output-dir qc/run-frames
    python "<skill-dir>/scripts/scale_frames.py" --sheet raw/run-sheet.png --rows 2 --cols 4 --scale-from 1/8 --resampler nearest --root-lock torso-x --row-baseline --emit-clips --clip-name run --duration-ms 80 --output-dir out/run-game
    python "<skill-dir>/scripts/build_animation_clips.py" --manifest out/run-game/clips.json --output-dir out/run-clips

If the leading-leg test reports a duplicated half-cycle, regenerate only the second half (frames 4-7 of an 8-frame run), attaching the master, frames 0-3 and the guide. If one frame's head ratio is off, regenerate or repair that frame from the master. Torso drift and row-baseline steps are fixed by `scale_frames.py` (`--root-lock torso-x`, `--row-baseline`), never by moving pixels by hand. In Claude Code, `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.
