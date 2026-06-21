---
name: generate2dsprite
description: "Generate and postprocess general 2D game assets and animation sheets: pixel-art sprites, clean HD map props, creatures, characters, NPCs, spells, projectiles, impacts, props, summons, and transparent GIF exports. Use when Codex should infer the asset plan from a natural-language request, match a reference or map art style, call built-in `image_gen` for solid-magenta raw sheets, and use the local processor only for chroma-key cleanup, frame extraction, alignment, QC, and transparent exports."
---

# Generate2dsprite

Use this skill for self-contained 2D sprite or animation assets.

When a larger game or playable prototype needs sprites, use this skill for the visible sprite assets and keep runtime/game assembly separate. Do not replace requested sprite assets with code-drawn placeholders.

## Parameters

Infer these from the user request:

- `asset_type`: `player` | `npc` | `creature` | `character` | `spell` | `projectile` | `impact` | `prop` | `summon` | `fx`
- `action`: `single` | `idle` | `cast` | `attack` | `shoot` | `jump` | `hurt` | `combat` | `walk` | `run` | `hover` | `charge` | `projectile` | `impact` | `explode` | `death`
- `view`: `topdown` | `side` | `3/4`
- `sheet`: `auto` | `2x2` | `2x3` | `2x4` | `3x3` | `3x4` | `4x4` | `5x5` | `custom_grid` | `strip_1x3` | `strip_1x4`
- `frames`: `auto` or explicit count
- `bundle`: `single_asset` | `unit_bundle` | `spell_bundle` | `combat_bundle` | `line_bundle` | `hero_action_bundle` | `engine_atlas`
- `effect_policy`: `all` | `largest`
- `anchor`: `center` | `bottom` | `feet`
- `margin`: `tight` | `normal` | `safe`
- `art_style`: pixel_art | clean_hd | pixel_inspired | retro_pixel | map_style | project-native
- `reference`: `none` | `attached_image` | `generated_image` | `local_file`
- `layout_guide`: `none` | `optional` | `recommended`
- `prompt`: the user's theme or visual direction
- `role`: only when the asset is clearly an NPC role
- `name`: optional output slug

Read [references/modes.md](references/modes.md) when the request is ambiguous.

## Agent Rules

- Decide the asset plan yourself. Do not force the user to spell out sheet size, frame count, or bundle structure when the request already implies them.
- Do not pack unrelated actions into one raw generated sheet just to satisfy a `4x4`, `5x5`, or custom engine atlas. A raw generated sheet should represent one action family, one continuous sequence, one canonical directional locomotion sheet, or one prop/asset pack.
- For controllable heroes, main characters, and high-value player assets with multiple actions, generate separate per-action grid sheets first, QC each action, then deterministically assemble the engine-required atlas only after the grids pass visual review.
- For controllable heroes, main characters, and high-value player body actions, default attack/shoot/cast body sheets to body-only. Do not include large slash arcs, muzzle flashes, projectiles, impact bursts, detached dust, long trails, or wide detached FX in the body sheet. Generate those as separate `fx`, `projectile`, or `impact` sheets and layer them in the game.
- Only include wide attack FX in the same raw body sheet when the target runtime explicitly supports wider per-action cells plus per-action origin/anchor metadata. Otherwise, a wide FX bbox will force the body to shrink inside the fixed cell.
- Write the art prompt yourself. Do not default to the prompt-builder script.
- Use built-in `image_gen` for every raw image.
- Do not create raw sprite art with Three.js, Canvas, SVG, HTML/CSS drawing, PIL shape drawing, procedural geometry, placeholder primitives, or code-rendered screenshots. Runtime code may display finished generated assets, and scripts may make layout guides or postprocess generated images, but requested sprite art must originate from built-in `image_gen`.
- When the user provides or implies a visual reference, use built-in image edit/reference semantics only after the reference image is visible in the conversation context. If the reference is a local file, call `view_image` first; do not rely on a filesystem path in the prompt as the visual reference.
- Do not force pixel art when the asset is a map prop for `$generate2dmap` or when the user/project requests a different style. Match the map or reference style first.
- Use the script only as a deterministic processor: magenta cleanup, frame splitting, component filtering, scaling, alignment, QC metadata, transparent sheet export, and GIF export.
- Do not use scripts to generate the creative image prompt. If a legacy prompt-builder command exists, treat it as historical compatibility only, not the normal skill workflow.
- Layout guides are allowed only as deterministic geometry references for image generation. They may show slot count, spacing, centering, and safe padding, but must never define the creative art direction.
- Treat script flags as execution primitives chosen by the agent, not user-facing hardcoded workflow.
- If a generated sheet touches cell edges, drifts in scale, or breaks a projectile / impact loop, either reprocess with better primitive settings or regenerate the raw sheet.
- Do not use raw single-row sheets such as `1x4`, `1x6`, `1x8`, or `1xN` for characters, players, controllable heroes, creatures, NPCs, enemies, summons, animated props, or any asset where a body/subject must stay centered. Single-row raw generation is too likely to drift horizontally and crop inconsistently.
- For animated body assets, use a multi-row grid by default: 4 frames -> `2x2`, 6 frames -> `2x3`, 8 frames -> `2x4`, 9 frames -> `3x3`, 12 frames -> `3x4` or `4x3`, 16 frames -> `4x4`.
- If a game engine needs a final single-row strip or mixed atlas, first generate and QC the action as a multi-row grid, then assemble the delivery strip/atlas deterministically.
- In every animated body grid prompt, require the subject body to stay centered in each cell, full body inside the central 60% to 70% safe area, consistent scale across cells, stable feet/bottom anchor line when applicable, and no limbs, weapons, hair, capes, dust, muzzle flashes, or detached FX crossing cell edges.
- For hero attack body prompts, explicitly require body height and body scale to match the accepted idle/run sheets, stable feet/bottom anchor, weapon kept close enough to avoid widening the body bbox, and no detached slash arc or screen-space attack effect.
- For map prop packs, classify props before choosing a grid. Square `2x2`, `3x3`, and `4x4` packs are only for compact props. Do not put platforms, floors, bridges, walls, ladders, gates, doors, long hazards, wide/tall props, collision-bearing objects, or tileset/strip pieces into square prop packs; use one-by-one, `1x3`/`1x4` strips, custom wide cells, or a tileset-like atlas instead.
- Keep the solid `#FF00FF` background rule unless the user explicitly wants a different processing workflow.

## Workflow

### 1. Infer the asset plan

Pick the smallest useful output.

Examples:

- controllable hero with four directions -> `player` + `player_sheet`
- side-view controllable hero with idle/run/shoot/jump -> `player` + `hero_action_bundle`
  - idle grid sheet, usually `2x2` for 4 frames
  - run grid sheet, usually `2x2` or `2x3` depending on needed frame count
  - shoot grid sheet with body/weapon only, usually `2x2`
  - jump grid sheet, usually `2x2`
  - projectile / muzzle flash as separate assets when needed
  - optional assembled engine atlas after per-action QC
- side-view controllable hero with melee attack -> `player` + `hero_action_bundle`
  - attack body grid sheet, usually `2x2` or `2x3`, body-only
  - slash arc / weapon trail as a separate `fx` sheet when the attack needs a wide visual effect
  - impact spark as a separate `impact` sheet when hits need feedback
- healer overworld NPC -> `npc` + `single_asset` or `unit_bundle`
- large boss idle loop -> `creature` + `idle` + `3x3`
- wizard throwing a magic orb -> `spell_bundle`
  - caster cast sheet
  - projectile loop
  - impact burst
- monster line request -> `line_bundle`
  - plan 1-3 forms
  - per form, make the sheets the request actually needs

### 2. Write the prompt manually

Use [references/prompt-rules.md](references/prompt-rules.md).

Choose `art_style` before writing the prompt:

- Use `pixel_art` or `retro_pixel` for classic sprites, 16-bit RPG actors, and requests that explicitly ask for pixel art.
- Use `clean_hd` for map props or assets intended to match clean hand-painted HD maps.
- Use `pixel_inspired` only when the user wants a pixel-adjacent look without retro chunkiness.
- Use `map_style` or `project-native` when an existing map, game, or reference should define the style.

If a reference is involved:

- Make the reference visible first. For local paths, use `view_image`; for freshly generated references, rely on the image already shown in context.
- State the reference role explicitly: preserve identity/style, create an animation sheet for the same subject, create an evolution/variant, or derive a matching prop/FX.
- Preserve the stable identity markers from the reference: silhouette, palette, face/eye features, costume marks, major accessories, and material language.
- Let only the requested action or evolution change. Do not redesign the subject unless the user asks.
- Still require exact sheet shape, solid magenta background, frame containment, and same scale across frames.

Keep the strict parts:

- solid `#FF00FF` background
- exact sheet shape
- same character or asset identity across frames
- same bounding box and pixel scale across frames
- explicit containment: nothing may cross cell edges

Mixed-action atlas guardrail:

- Do not ask `image_gen` to generate unrelated action rows in one raw sheet, such as `row 1 idle, row 2 run, row 3 shoot, row 4 jump`, for a controllable hero or main character.
- Do not ask `image_gen` to generate raw single-row action strips such as `1x4 idle`, `1x4 run`, `1x4 shoot`, or `1x4 jump` for a controllable hero, character, creature, NPC, enemy, summon, or animated prop.
- If an engine needs a combined `4x4`, `5x5`, custom atlas, or row-strip delivery format, generate the action grids separately, process and QC them separately, then assemble the delivery atlas deterministically.
- Exceptions are canonical directional locomotion sheets, one continuous long action sequence, prop packs, tileset-like atlases, and low-stakes compact enemy combat sheets. These still need one coherent prompt and visual QC.
- Keep projectile, muzzle flash, impact, dust trails, and detached FX in separate sheets unless they are intentionally part of the same action silhouette and remain tightly attached.
- For controllable heroes and main characters, "tightly attached" is not enough when the effect makes the action bbox much wider or taller than idle/run. Split wide slash arcs, muzzle flashes, long weapon trails, dust clouds, and impact bursts into separate FX sheets by default.

Animated body grid guardrail:

- `1x4` and other raw single-row sheets are not valid defaults for animated bodies. This includes players, controllable heroes, creatures, NPCs, enemies, summons, animated props, and body-attached combat actions.
- Use `2x2` for 4-frame body actions. This is the default for idle, short attack, shoot body, jump, hurt, hover, and compact side-view walk/run actions.
- Use `2x3` for 6-frame body actions such as cast, attack, summon, run, charge, or transformation.
- Use `2x4`, `3x3`, `3x4`, or `4x4` for longer body actions. Prefer a compact grid over a long row.
- For 4-direction top-down walk, `4x4` can remain a raw generation shape because it is a canonical directional locomotion sheet, not four unrelated action rows.
- If final runtime needs a row strip, assemble it after QC from the processed multi-row grid frames.
- Keep the character centered in every cell. The body centerline should stay near the cell center, feet/bottom anchor should stay on the same y-position when visible, and the subject should occupy only the central safe area with generous magenta padding.
- For attack, shoot, cast, charge, and other body actions, the body height should stay close to the accepted idle/run body height. If a fixed-cell runtime is being used, reject body-action output when the body appears more than about 10-15% smaller than idle/run, even if `edge_touch_frames` is empty.

Map prop pack guardrail:

- Use square `2x2`, `3x3`, and `4x4` raw prop packs only for compact props such as rocks, shrubs, barrels, crates, lamps, small signs, pots, debris, and small ornaments.
- Do not use square prop packs for wide or collision-critical map objects: floors, platforms, ledges, terrain chunks, bridges, wall runs, ladders, roads, rails, pipes, long spike traps, gates, doors, buildings, large trees, checkpoints, exits, or build pads.
- Use one-by-one generation for unique, large, important, tall, irregular, or collision-aligned props.
- Use `1x3` or `1x4` strips for repeatable platform/floor assets, with left cap, middle repeat, right cap, and optional slope/corner/end variant.
- Use custom wide cells for multiple similar wide objects. The grid must state explicit non-square cell dimensions and must not mix compact props with platform/terrain objects.
- If a square prop pack fails due to edge touch or bad cropping, do not solve it by relaxing QC. Reclassify the object and regenerate with a more suitable sheet shape.

If a layout guide is useful, generate one before calling built-in `image_gen`:

```bash
python scripts/make_layout_guide.py \
  --rows <rows> \
  --cols <cols> \
  --cell-width 384 \
  --cell-height 384 \
  --output <run-dir>/references/<rows>x<cols>-layout-guide.png
```

Then make the guide visible in the conversation context and tell `image_gen` to use it only for invisible slot count, spacing, centering, and safe padding. The output must not reproduce guide boxes, safe-area rectangles, center marks, labels, borders, or guide background.

Use layout guides deliberately:

- recommended for `prop_pack_3x3`, `prop_pack_4x4`, tileset-like atlases, fixed multi-row animation grids, and non-directional 16-frame action sequences such as casting, summoning, charging, death, or transformation
- optional for `3x3` large idle and high-value showcase loops when previous generations drift in scale or spacing
- not the default for `4x4` four-direction walk sheets, because the guide can make directional poses too conservative; use it only after an unguided run fails layout or edge safety

### 3. Generate the raw image

Use built-in `image_gen`.

Do not use Three.js, Canvas, SVG, HTML/CSS, PIL drawing, or other code-generated art as the raw sprite source. These are acceptable only for runtime display, debug overlays, deterministic layout guides, or postprocessing already-generated images.

After generation:

- resolve the raw image to a stable local file path before postprocessing
- prefer an explicit file path returned by `image_gen` when one is available
- otherwise find the raw PNG under `$CODEX_HOME/generated_images/...` or `~/.codex/generated_images/...`, and only accept files that correspond to the current generation call, not a stale latest file
- if `image_gen` does not return a stable file path and the generated-images directories do not contain the current output, inspect the current Codex session's `image_generation_call.result` for base64 image data (`b64_json`, a `data:image/...;base64,...` URL, or raw base64), immediately decode it into the current run's `generated_images/` directory, and use that decoded file path
- keep the original generated image in place

This fallback is especially important on Windows/Codex Desktop sessions, where `image_gen` may display the generated image in chat without writing a current PNG into the expected generated-images directory.

Use the helper when the current result must be decoded:

```bash
python scripts/save_imagegen_result.py \
  --input <current-codex-session.jsonl-or-image-generation-result.json> \
  --output-dir <run-dir>/generated_images
```

When given a session JSONL, the helper uses the latest `image_generation_call.result` in that file and ignores unrelated data URLs from other tool output. If the result is only available as raw base64 or a data URL, pass it on stdin instead of `--input`. Do not call the local processor with a guessed `/mnt/data/...` path or an older generated image. If neither a current file nor base64 image bytes are available, stop and report that the raw image bytes are inaccessible.

### 4. Postprocess locally

Run `scripts/generate2dsprite.py process` on the raw image.

The processor is intentionally low-level. The agent chooses:

- `rows` / `cols`
- `fit_scale`
- `align`
- `shared_scale`
- `component_mode`
- `component_padding`
- `edge_touch` rejection strategy

Use the processor to gather QC metadata, not to make aesthetic decisions for you.

For hero action bundles, process each action grid as its own sheet before any final atlas assembly. Use `component_mode=largest` for body-only hero grids. Use `component_mode=all` only for projectile, impact, aura, slash FX, or intentionally detached FX sheets, not for fixed-cell hero body attacks that need stable body scale.

### 5. QC the result

Check:

- did any frame touch the cell edge
- did any frame resize differently than intended
- did detached effects become noise
- does the sheet still read as one coherent animation
- for hero/player body actions, does the body height match the accepted idle/run scale within roughly 10-15%
- for fixed-cell runtimes, did a wide weapon trail or FX arc shrink the body inside the cell

If not, rerun with different processor settings or regenerate the raw sheet.

### 6. Return the right bundle

For a single sheet, expect:

- `raw-sheet.png`
- `raw-sheet-clean.png`
- `sheet-transparent.png`
- frame PNGs
- `animation.gif`
- `prompt-used.txt`
- `pipeline-meta.json`

For `player_sheet`, expect:

- transparent 4x4 sheet
- 16 frame PNGs
- direction strips
- 4 direction GIFs

For `spell_bundle` or `unit_bundle`, create one folder per asset in the bundle.

For `hero_action_bundle`, expect:

- one raw and processed sheet per action
- per-action frame PNGs and GIFs for visual QC
- separate projectile / muzzle / slash / impact assets when the hero shoots, casts, or uses wide melee effects
- optional assembled `engine-atlas-transparent.png` only after per-action QC passes

## Defaults

- `idle`
  - small or medium actor -> `2x2`
  - large creature or boss -> `3x3`
- `cast` -> prefer `2x3`
- `projectile` -> prefer `2x2` for short animated loops; use row strips only when the engine specifically requires a strip, and assemble that strip after QC when practical
- `impact` / `explode` -> prefer `2x2`
- `walk`
  - topdown actor -> `4x4` for four-direction walk
  - side-view asset -> `2x2`
- controllable hero or main player with multiple actions -> `hero_action_bundle`
  - generate one action per raw multi-row grid sheet, not as a raw `1x4` strip
  - attack/shoot/cast body sheets are body-only by default; wide slash arcs, muzzle flashes, projectiles, trails, dust, and hit impacts are separate FX/projectile/impact sheets
  - default 4-frame action grid is `2x2`
  - use `2x3` for 6-frame actions and `2x4`, `3x3`, `3x4`, or `4x4` for longer actions
  - do not generate a mixed-action raw `4x4`, `5x5`, or custom atlas
  - assemble the final atlas only as a deterministic delivery step if the game requires it
- `4x4`, `5x5`, and custom grids
  - use as raw generation only for one coherent long action sequence, canonical directional locomotion, prop packs, or tileset-like atlases
  - use as delivery atlases for mixed actions only after separate action sheets pass QC
- use `shared_scale` by default for any multi-frame asset where frame-to-frame consistency matters
- use `largest` component mode for hero/player body grids; use `all` for separate FX/projectile/impact sheets

## Resources

- `references/modes.md`: asset, action, bundle, and sheet selection
- `references/prompt-rules.md`: manual prompt patterns and containment rules
- `scripts/generate2dsprite.py`: postprocess primitive for cleanup, extraction, alignment, QC, and GIF export
