---
name: generate2dsprite
description: "Generate and postprocess general 2D pixel-art assets and animation sheets: creatures, characters, NPCs, spells, projectiles, impacts, props, summons, and transparent GIF exports. Use when Codex should infer the asset plan from a natural-language request, call built-in `image_gen` for solid-magenta raw sheets, and use the local processor only for chroma-key cleanup, frame extraction, alignment, QC, and transparent exports."
---

# Generate2dsprite

Use this skill for self-contained 2D sprite or animation assets.

If the user wants a whole playable content pack, map, story, slideshow, or pack assembly, use `generate2dgamepack`.

## Parameters

Infer these from the user request:

- `asset_type`: `player` | `npc` | `creature` | `character` | `spell` | `projectile` | `impact` | `prop` | `summon` | `fx`
- `action`: `single` | `idle` | `cast` | `attack` | `hurt` | `combat` | `walk` | `run` | `hover` | `charge` | `projectile` | `impact` | `explode` | `death`
- `view`: `topdown` | `side` | `3/4`
- `sheet`: `auto` | `1x4` | `2x2` | `2x3` | `3x3` | `4x4`
- `frames`: `auto` or explicit count
- `bundle`: `single_asset` | `unit_bundle` | `spell_bundle` | `combat_bundle` | `line_bundle`
- `effect_policy`: `all` | `largest`
- `anchor`: `center` | `bottom` | `feet`
- `margin`: `tight` | `normal` | `safe`
- `prompt`: the user's theme or visual direction
- `role`: only when the asset is clearly an NPC role
- `name`: optional output slug

Read [references/modes.md](references/modes.md) when the request is ambiguous.

## Agent Rules

- Decide the asset plan yourself. Do not force the user to spell out sheet size, frame count, or bundle structure when the request already implies them.
- Write the art prompt yourself. Do not default to the prompt-builder script.
- Use built-in `image_gen` for every raw image.
- Use the script only as a deterministic processor: magenta cleanup, frame splitting, component filtering, scaling, alignment, QC metadata, transparent sheet export, and GIF export.
- Treat script flags as execution primitives chosen by the agent, not user-facing hardcoded workflow.
- If a generated sheet touches cell edges, drifts in scale, or breaks a projectile / impact loop, either reprocess with better primitive settings or regenerate the raw sheet.
- Keep the solid `#FF00FF` background rule unless the user explicitly wants a different processing workflow.

## Workflow

### 1. Infer the asset plan

Pick the smallest useful output.

Examples:

- controllable hero with four directions -> `player` + `player_sheet`
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

Keep the strict parts:

- solid `#FF00FF` background
- exact sheet shape
- same character or asset identity across frames
- same bounding box and pixel scale across frames
- explicit containment: nothing may cross cell edges

### 3. Generate the raw image

Use built-in `image_gen`.

After generation:

- find the raw PNG under `$CODEX_HOME/generated_images/...`
- copy or reference it from the working output folder
- keep the original generated image in place

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

### 5. QC the result

Check:

- did any frame touch the cell edge
- did any frame resize differently than intended
- did detached effects become noise
- does the sheet still read as one coherent animation

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

## Defaults

- `idle`
  - small or medium actor -> `2x2`
  - large creature or boss -> `3x3`
- `cast` -> prefer `2x3`
- `projectile` -> prefer `1x4`
- `impact` / `explode` -> prefer `2x2`
- `walk`
  - topdown actor -> `4x4` for four-direction walk
  - side-view asset -> `2x2`
- use `shared_scale` by default for any multi-frame asset where frame-to-frame consistency matters
- use `largest` component mode when detached sparkles or edge debris make the main body unstable

## Resources

- `references/modes.md`: asset, action, bundle, and sheet selection
- `references/prompt-rules.md`: manual prompt patterns and containment rules
- `scripts/generate2dsprite.py`: postprocess primitive for cleanup, extraction, alignment, QC, and GIF export
