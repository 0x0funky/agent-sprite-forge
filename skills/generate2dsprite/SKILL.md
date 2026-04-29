---
name: generate2dsprite
description: "Generate and postprocess general 2D game assets and animation sheets: pixel-art sprites, clean HD map props, creatures, characters, NPCs, spells, projectiles, impacts, props, summons, and transparent GIF exports. Use when an agent (Codex, Claude Code, Cursor, or any CLI agent) should infer the asset plan from a natural-language request, match a reference or map art style, generate solid-magenta raw sheets via the agent's image-generation tool or the bundled `scripts/image_gen.py` fallback, and use the local processor only for chroma-key cleanup, frame extraction, alignment, QC, and transparent exports."
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
- `art_style`: pixel_art | clean_hd | pixel_inspired | retro_pixel | map_style | project-native
- `reference`: `none` | `attached_image` | `generated_image` | `local_file`
- `prompt`: the user's theme or visual direction
- `role`: only when the asset is clearly an NPC role
- `name`: optional output slug

Read [references/modes.md](references/modes.md) when the request is ambiguous.

## Agent Rules

- Decide the asset plan yourself. Do not force the user to spell out sheet size, frame count, or bundle structure when the request already implies them.
- Write the art prompt yourself. Do not default to the prompt-builder script.
- Generate every raw image with whichever image-generation tool the host agent provides. On Codex, that is built-in `image_gen`. On Claude Code, Cursor, or any other agent without a built-in image tool, shell out to `scripts/image_gen.py` (see "Image generation backends" below).
- When the user provides or implies a visual reference, use image-edit/reference semantics only after the reference image is visible in the conversation context. On Codex, call `view_image` for local paths. On other agents, surface the reference with the host agent's native file/image read tool, then pass `--reference <path>` to `scripts/image_gen.py`. Do not rely on a filesystem path in the prompt as the visual reference.
- Do not force pixel art when the asset is a map prop for `$generate2dmap` or when the user/project requests a different style. Match the map or reference style first.
- Use the script only as a deterministic processor: magenta cleanup, frame splitting, component filtering, scaling, alignment, QC metadata, transparent sheet export, and GIF export.
- Do not use scripts to generate the creative image prompt. If a legacy prompt-builder command exists, treat it as historical compatibility only, not the normal skill workflow.
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

Choose `art_style` before writing the prompt:

- Use `pixel_art` or `retro_pixel` for classic sprites, 16-bit RPG actors, and requests that explicitly ask for pixel art.
- Use `clean_hd` for map props or assets intended to match clean hand-painted HD maps.
- Use `pixel_inspired` only when the user wants a pixel-adjacent look without retro chunkiness.
- Use `map_style` or `project-native` when an existing map, game, or reference should define the style.

If a reference is involved:

- Make the reference visible first. On Codex, use `view_image` for local paths. On other agents (Claude Code, Cursor, generic CLI), use the host agent's native file/image read tool. For freshly generated references, rely on the image already shown in context.
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

### 3. Generate the raw image

Use the host agent's image-generation tool.

- **Codex**: call built-in `image_gen`. Find the raw PNG under `$CODEX_HOME/generated_images/...`, then copy or reference it from the working output folder.
- **Claude Code, Cursor, and other agents without a built-in image tool**: shell out to `scripts/image_gen.py`. Example:

  ```bash
  python scripts/image_gen.py \
      --prompt "<your prompt>" \
      --out work/raw-sheet.png \
      --size 1024x1024
  ```

  See "Image generation backends" below for backend selection and env vars.

In both cases, keep the original raw image on disk so it can be regenerated or QA'd later.

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

## Image generation backends

The skill works with any agent that can either (a) produce a PNG via a built-in image tool, or (b) run a Python script. The repo ships `scripts/image_gen.py` as a unified CLI for case (b).

Supported backends:

- `openai` (default) - calls the OpenAI Images API. Default model: `gpt-image-2`.
- `gemini` - calls Google Gemini 2.5 Flash Image.

Selection:

- `--backend auto` (default) picks the first backend whose API key is available.
- `SPRITE_FORGE_BACKEND=openai|gemini` overrides the default.
- `SPRITE_FORGE_MODEL=<id>` overrides the default model id.

Required env vars (only for the backend you actually use):

- `OPENAI_API_KEY` for `openai`.
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) for `gemini`.

Codex users do not need to install the optional SDKs because Codex's built-in `image_gen` handles generation directly.

## Resources

- `references/modes.md`: asset, action, bundle, and sheet selection
- `references/prompt-rules.md`: manual prompt patterns and containment rules
- `scripts/generate2dsprite.py`: postprocess primitive for cleanup, extraction, alignment, QC, and GIF export
- `../../scripts/image_gen.py`: agent-agnostic image generation wrapper for non-Codex hosts
- `../../scripts/view_image.py`: optional shim that reports image metadata for non-Codex hosts
