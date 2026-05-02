---
name: generate2dmap
description: "Generate and revise production-oriented 2D game maps with image generation as the default visual asset source, choosing a visual model, runtime object model, collision model, art direction, and engine/export target. Use when an agent (Codex, Claude Code, Cursor, or any CLI agent) needs to create or integrate RPG maps, monster-taming maps, tactical arenas, battle backgrounds, side-scroller/parallax scenes, tilemaps, layered raster maps, clean HD hand-painted maps, pixel-inspired maps, prop packs, collision zones, walkable areas, or map previews."
---

# Generate2dmap

## Overview

Build the smallest map bundle that satisfies the game. Decide the map as a pipeline, not as a single strategy label:

1. `visual_model`: `baked_raster` | `layered_raster` | `tilemap` | `layered_tilemap` | `parallax_layers`
2. `runtime_object_model`: `none` | `separate_props` | `y_sorted_props` | `interactive_entities` | `foreground_occluders`
3. `collision_model`: `none` | `coarse_shapes` | `precise_shapes` | `tile_collision` | `polygon_walkmesh` | `trigger_zones`
4. `engine_target`: `raw_canvas` | `Phaser` | `Tiled_JSON` | `LDtk` | `Godot_TileMap` | `Unity_Tilemap` | project-native

Use user-specified parameters when present. When the user does not specify them, infer the lightest pipeline from the existing game, camera, collision needs, map scale, and editing needs.

Read [references/map-strategies.md](references/map-strategies.md) when the pipeline choice is not obvious. Read [references/layered-map-contract.md](references/layered-map-contract.md) before implementing a layered raster map. Read [references/prop-pack-contract.md](references/prop-pack-contract.md) before batching generated props into a sheet.

## Image Generation First

This skill is image-generation-first for visual assets. Use the host agent's image-generation tool as the default creative art source for base maps, dressed references, prop sheets, prop sprites, tileset art, parallax layers, battle backgrounds, and other visible map assets.

- **Codex**: built-in `image_gen`.
- **Claude Code, Cursor, and other agents without a built-in image tool**: shell out to `scripts/image_gen.py` (see the sibling `generate2dsprite` SKILL.md "Image generation backends" section for backend selection and env vars).

The agent must write the creative image prompts itself. Do not use scripts to generate creative prompts or to procedurally draw final visual art. Scripts may assemble, slice, chroma-key, crop, validate, compose previews, emit JSON metadata, and wire image-generated assets into engine-native files such as Godot `.tscn` scenes.

Only use procedural drawing or scripted placeholder art when the user explicitly asks for placeholders, test fixtures, debug maps, or engine scaffolding without final art. If using an engine target such as `Godot_TileMap`, generate or reuse the visual tileset art first, then use scripts/code only to build tile layers, collision, zones, and scene wiring.

## Parameter Contract

User-facing parameters may be stated in natural language:

- `map_kind`: overworld | town | dungeon | shrine | arena | battle_bg | side_scroller | tactical
- `visual_model`: baked raster | layered raster | tilemap | layered tilemap | parallax
- `size`: pixel dimensions, tile dimensions, or camera-relative size
- `perspective`: top-down | 3/4 top-down | side-view | isometric-like
- `art_style`: clean_hd | pixel_inspired | retro_pixel | hand_painted | project-native
- `visual_asset_source`: image_gen | existing_assets | procedural_placeholder
- `collision_precision`: none | coarse | precise | tile | walkmesh
- `prop_generation`: none | one_by_one | prop_pack_2x2 | prop_pack_3x3 | prop_pack_4x4
- `output_format`: PNG only | layered preview | manifest JSON | engine-native map data

When unspecified:

- Use `image_gen` as the visual asset source.
- Use `baked_raster + coarse_shapes` for battle backgrounds, title/menu scenes, cutscenes, and fixed arenas.
- Use `layered_raster + y_sorted_props + precise_shapes` for top-down RPG exploration with tall props, occlusion, interactables, or reusable props.
- Use `tilemap` or `layered_tilemap` only when the engine/editor already uses tiles or the user asks for editable tiles.
- Use `parallax_layers` for side-scrollers and scrolling backgrounds.
- Use prop packs when 4 or more small/medium static props share one style and can fit into equal cells.
- Use one-by-one prop generation for hero props, buildings, gates, irregular large props, animated props, or props needing strong identity.
- Use `clean_hd` for generated exploration maps unless the project or user asks for pixel art. This means clean hand-painted top-down 2D RPG game map, HD game asset style, sharp readable terrain shapes, low texture noise, and no chunky pixels.
- Use `pixel_inspired` only when the user wants a pixel-adjacent look without retro chunkiness.
- Use `retro_pixel` only when the user explicitly asks for 16-bit, retro JRPG, or classic pixel-art maps.

## Workflow

1. Inspect the target game.
   - Find camera size, map dimensions, coordinate system, render order, asset loading, collision support, zone data, and existing map formats.
   - Preserve the engine's existing style and data contracts.

2. Choose the pipeline axes.
   - Select `visual_model`, `runtime_object_model`, `collision_model`, and `engine_target`.
   - Select `art_style`. Prefer readable gameplay shapes over decorative texture density.
   - Select `visual_asset_source`. Default to `image_gen`; use `existing_assets` only when the project already has suitable art; use `procedural_placeholder` only when explicitly requested.
   - Treat `hybrid` as a result of combining axes, not as a primary category.

3. Produce assets.
   - Write the creative prompts manually and use the host agent's image-generation tool for visible map art unless the user explicitly chose existing assets or procedural placeholders. On Codex this is built-in `image_gen`; on Claude Code, Cursor, or other agents without a built-in image tool, run `scripts/image_gen.py` (see "Image generation backends" in the `generate2dsprite` SKILL.md).
   - For baked raster maps, generate one background with the host agent's image tool, or edit/use an existing image when supplied, then add optional collision/zones metadata.
   - For layered raster maps, generate a ground-only base map first. Then show that base image in context and generate a dressed reference from the visible base before making final props and placements.
   - For tilemaps, generate or reuse tileset art first, then follow the engine/editor format for layers, objects, collision, and scene files. Do not script-draw the tileset as the final art source.
   - For parallax scenes, generate background/midground/foreground visual layers first, then produce scroll metadata.
   - Do not present a rerunnable script that creates the whole art pack as the main solution unless the user asked for procedural placeholder art.

4. Build metadata.
   - Store prop placement, actor spawn points, interactables, blockers, walk bounds, encounter zones, exits, and triggers as structured data.
   - Keep collision independent from pixels unless the target engine explicitly uses tile collision.

5. Validate and preview.
   - Compose a flattened preview for layered maps.
   - Validate image sizes, alpha channels, prop pack extraction metadata, JSON parseability, and critical walkability points when collision matters.

## Prop Generation Rules

Use `$generate2dsprite` for reusable transparent props, but the agent must write the prop prompt itself using the selected map `art_style`. Do not use a script to generate the creative prompt. For `clean_hd` maps, explicitly request clean hand-painted HD 2D game assets and explicitly forbid pixel art. For `pixel_inspired`, request clean modern pixel-art-inspired props without retro chunkiness. For `retro_pixel`, request 16-bit or retro JRPG pixel art.

Choose the generation shape deliberately:

- `one_by_one`: safest for large, important, animated, or irregular props.
- `prop_pack_2x2`: 4 related props, safest batch size.
- `prop_pack_3x3`: 9 small/medium props, good quality/time tradeoff.
- `prop_pack_4x4`: 16 very simple small props; fastest but most likely to drift or touch edges.

Prop packs save image-generation calls and prompt overhead, but reduce per-prop control. Use them for rocks, shrubs, barrels, small signs, lamps, crates, floor ornaments, plants, and repeated environmental props. Do not use prop packs for buildings, gates, trees with wide canopies, character-like statues, hero objects, or anything that must be pixel-perfect.

For layered maps with generated props, prefer this reference pipeline:

1. Generate `assets/map/<name>-base.png` as ground-only terrain.
2. Make the base image visible in conversation context. On Codex, use `view_image` for local paths. On other agents, surface the file with the host agent's native file/image read tool, then pass `--reference <path>` to `scripts/image_gen.py`. Do not rely on a path string in the prompt as the reference.
3. Generate `assets/map/<name>-dressed-reference.png` from the visible base, preserving camera, terrain, size, road/water shapes, anchor pads, and boundaries. Treat this as a planning/reference image, not the final runtime map.
4. Generate one-by-one props or a prop pack based on the dressed reference.
5. Place extracted props over the original base and compose a flattened preview.
6. Validate that base, dressed reference, and preview dimensions match.

Use `scripts/extract_prop_pack.py` after generating a solid-magenta prop sheet. If the sheet has antialiased magenta fringe, run the imagegen chroma-key helper with soft matte and despill before extraction, then extract from the alpha-cleaned sheet. Use `scripts/compose_layered_preview.py` to verify placement over the base map.

## Expected Deliverables

For a baked raster map:

- `assets/map/<name>.png`
- optional `<name>.prompt.txt`
- optional `data/<name>-collision.json` or `data/<name>-zones.json`
- code changes that load/use the image

For a layered raster map:

- `assets/map/<name>-base.png`
- `assets/map/<name>-base.prompt.txt`
- optional `assets/map/<name>-dressed-reference.png` for prop planning
- `assets/props/<prop>/prop.png` folders, from one-by-one props or extracted prop packs
- `data/<name>-props.json` placement metadata
- `data/<name>-collision.json` and/or `data/<name>-zones.json` when gameplay needs them
- `assets/map/<name>-layered-preview.png`
- code changes that load the base, props, y-sorted renderables, collision, and zones

For a tilemap or layered tilemap:

- image-generated or user-supplied `assets/tilesets/<name>.png`
- optional tile slicing/atlas metadata
- engine-native tile layer data such as Tiled JSON, LDtk data, Godot TileMap scene data, Unity tile placement data, or project-native JSON
- object layers for spawns, exits, interactables, blockers, and zones
- a flattened preview assembled from the visual tileset and layer data
- no script-drawn final tileset art unless the user explicitly asked for procedural placeholders

For a prop pack:

- raw generated sheet with solid `#FF00FF` background
- extracted `assets/props/<prop>/prop.png` files
- `prop-pack.json` extraction manifest
- no `edge_touch` entries for accepted props

## Validation

Always validate what the chosen pipeline requires:

- map files exist and have expected dimensions
- transparent props contain alpha
- prop pack manifests parse and accepted props do not touch cell edges
- placement JSON parses and referenced prop files exist
- collision/zones JSON parses when present
- critical spawn, path, entrance, blocker, and zone points behave as expected
- flattened preview looks coherent at the game's camera size
