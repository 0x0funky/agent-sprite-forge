---
name: generate2dmap
description: "Generate and revise production-oriented 2D game maps with built-in image generation as the default visual asset source, choosing a visual model, runtime object model, collision model, art direction, and engine/export target. Use when Codex needs to create or integrate RPG maps, monster-taming maps, tactical arenas, battle backgrounds, side-scroller/parallax scenes, tilemaps, layered raster maps, clean HD hand-painted maps, pixel-inspired maps, prop packs, collision zones, walkable areas, or map previews."
---

# Generate2dmap

## Overview

Build the smallest playable map bundle that satisfies the game. Start by choosing a user-facing `map_mode`, then map it to the lower-level pipeline axes. Do not treat a map as only one image unless the user explicitly asks for a flat visual background.

1. `map_mode`: `tile_mode` | `scene_mode` | `side_scroll_mode` | `grid_mode` | `room_chunk_mode` | `baked_scene_mode`
2. `visual_model`: `baked_raster` | `layered_raster` | `tilemap` | `layered_tilemap` | `parallax_layers`
3. `runtime_object_model`: `none` | `separate_props` | `platform_objects` | `y_sorted_props` | `interactive_scene_objects` | `foreground_occluders` | `scene_hooks`
4. `collision_model`: `none` | `coarse_shapes` | `precise_shapes` | `tile_collision` | `polygon_walkmesh` | `trigger_zones`
5. `engine_target`: `raw_canvas` | `Phaser` | `Tiled_JSON` | `LDtk` | `Godot_TileMap` | `Unity_Tilemap` | project-native

Use user-specified parameters when present. When the user does not specify them, infer the lightest playable pipeline from the existing game, camera, collision needs, map scale, and editing needs.

For requests that imply a playable game map, level, stage, room, prototype, or engine scene, do not ship a single baked image as the runtime map unless the user explicitly asks for a flat background only. A baked image may be a background, reference, or preview artifact, but the playable deliverable must expose gameplay geometry and objects as separate layers, props, tile/object data, collision, zones, or engine-native scene nodes.

This skill is for scenes and maps. Do not generate character, enemy, boss, projectile, NPC, player, or animation sprite assets as map deliverables. The map may include scene hooks such as player spawns, actor spawn marker metadata, patrol/encounter zones, arena entrances, gates, exits, and camera triggers, but actor artwork, projectiles, and animations belong in `$generate2dsprite`.

Read [references/map-strategies.md](references/map-strategies.md) when the pipeline choice is not obvious. Read [references/layered-map-contract.md](references/layered-map-contract.md) before implementing a layered raster map. Read [references/prop-pack-contract.md](references/prop-pack-contract.md) before batching generated props into a sheet.

## Map Modes

Use `map_mode` as the first decision. It is a product-level preset that chooses the initial pipeline axes and expected deliverables:

- `tile_mode`: editable tile/grid maps for RPGs, monster-taming games, platformers, tactical maps, factory games, and engines/editors that already use tiles. Default axes: `tilemap` or `layered_tilemap` + `interactive_scene_objects + scene_hooks` + `tile_collision + trigger_zones`.
- `scene_mode`: base map plus separate props for tower defense, survivors-like arenas, cozy demos, top-down adventure scenes, and visual showcase maps. Default axes: `layered_raster` + `separate_props` or `y_sorted_props + interactive_scene_objects + scene_hooks` + `precise_shapes + trigger_zones`.
- `side_scroll_mode`: parallax side-scroller stages for action platformers, runners, Metroidvania rooms, side-view shooters, and beat-em-up stages. Default axes: `parallax_layers` + `platform_objects + interactive_scene_objects + foreground_occluders + scene_hooks` + `precise_shapes`.
- `grid_mode`: rule-heavy grid scenes for tactical RPGs, factory/automation games, board/card battlers, build grids, and terrain-cost maps. Default axes: `layered_tilemap` or `tilemap` + `interactive_scene_objects + scene_hooks` + `tile_collision` or grid metadata.
- `room_chunk_mode`: modular rooms/chunks for roguelikes, Metroidvania rooms, dungeon rooms, and procedural level assembly. Default axes: `layered_tilemap` or `parallax_layers` or `layered_raster` + object layers + exits/connection metadata + collision.
- `baked_scene_mode`: fixed battle backgrounds, title/menu screens, boss-room concept art, visual novel scenes, point-and-click backgrounds, or other explicitly flat/non-editable scenes. Default axes: `baked_raster` + `none` or `coarse_shapes`.

When the mode and lower-level axes disagree, the mode's playable/editable contract wins. For example, `side_scroll_mode` always needs separate collision and platform/object data even if it also produces a beautiful full-width preview image.

## Genre Routing

When the user gives a genre instead of a technical map mode, choose the mode conservatively:

- Pokemon-like / monster-taming RPG / top-down RPG town or route -> `tile_mode` with optional separate props, encounter zones, exits, NPC spawn markers, and collision.
- Tower defense / Kingdom Rush-like -> `scene_mode` with path metadata, build slots, props, collision/blockers, spawn/exit hooks, and optional engine scene scaffold.
- Survivors-like / arena survival -> `scene_mode` or `tile_mode` depending on map scale; keep obstacles sparse, define spawn rings/zones, camera bounds, and collision separately.
- Mega Man-like / side-view action platformer / runner / Metroidvania side room -> `side_scroll_mode`.
- Beat-em-up / brawler -> `side_scroll_mode` with a walkable belt polygon instead of jump-platform geometry; use parallax/background depth plus props, enemy wave zones, and camera bounds.
- Tactical RPG / strategy grid / factory automation / board-like game -> `grid_mode`.
- Roguelike dungeon / modular Metroidvania / procedural room assembly -> `room_chunk_mode`.
- Visual novel, title screen, point-and-click, boss arena concept, or non-playable showcase -> `baked_scene_mode` unless gameplay/editability is requested.

## Image Generation First

This skill is image-generation-first for visual assets. Use built-in `image_gen` as the default creative art source for base maps, in-world reference mockups, dressed references, stage references, prop sheets, prop sprites, tileset art, parallax layers, battle backgrounds, and other visible map assets.

The agent must write the creative image prompts itself. Do not use scripts to generate creative prompts or to procedurally draw final visual art. Scripts may assemble, slice, chroma-key, crop, validate, compose previews, emit JSON metadata, and wire image-generated assets into engine-native files such as Godot `.tscn` scenes.

Save every manually written image-generation prompt next to the generated asset as `<asset>.prompt.txt` or in an explicit manifest field. Do not leave accepted generated assets with empty prompt metadata when the run creates new visual assets.

Only use procedural drawing or scripted placeholder art when the user explicitly asks for placeholders, test fixtures, debug maps, or engine scaffolding without final art. If using an engine target such as `Godot_TileMap`, generate or reuse the visual tileset art first, then use scripts/code only to build tile layers, collision, zones, and scene wiring.

## Visual Reference Handoff

When generating an in-world reference mockup from an existing generated base/background, the prior image must be treated as an active visual reference, not just a file path or loose style hint:

1. Save the base/background image first.
2. Immediately before the next `image_gen` call, make that exact image visible in conversation context. If it is a local file, call `view_image` on the saved file.
3. In the next `image_gen` prompt, explicitly say to use the visible image immediately above as the visual reference.
4. Describe concrete features from the viewed image that must be preserved, such as camera framing, horizon, road or water shapes, terrain boundaries, entrance/exit direction, major silhouettes, empty pads, and landmark positions.
5. Generate an in-world reference mockup, not an annotated diagram. Do not draw circles, arrows, outlines, labels, numbers, UI callouts, text, captions, legends, highlighted boxes, highlighted zones, measurement lines, or explanatory overlays.
6. Render proposed visible gameplay objects as natural game-world objects or subtle in-world blockout geometry. Do not draw non-visual metadata such as spawn points, triggers, camera bounds, or patrol hints; write those later as structured scene-hook metadata.
7. Keep reference mockups sparse enough to drive final asset production. Unless the user explicitly asks for a dense concept sheet, include at most 9 distinct visible runtime prop/object candidates in the mockup. Repeated instances of the same platform, lamp, crate, hazard, pickup, or gate count as one candidate and can be repeated later in placement metadata.

Do not rely on a path string, filename, or generic wording like "based on the map" as the reference handoff. If the base/background is not visible in context, stop and make it visible before generating the dressed reference or stage reference.

## Layer Separation Contract

For any playable or editable layered map, the first generated base/background/foundation image must not bake in objects that the runtime should control separately. This applies across perspectives and styles: top-down RPG maps, monster-taming maps, tactical arenas, tower-defense lanes, side-view platformers, parallax stages, tile/editor workflows, clean HD, pixel-inspired, and retro pixel art.

The base/background/foundation layer may contain only stable non-interactive foundation art:

- top-down or 3/4 maps: ground material, paths, roads, water, cliffs, low terrain markings, floor patterns, and terrain boundaries
- tactical or tower-defense maps: ground, lanes, roads, build pads, lane markings, terrain zones, and non-interactive floor detail
- side-view stages: sky, far/mid scenery, distant buildings, distant terrain silhouettes, atmosphere, and non-colliding depth
- tilemaps: tileset art and tile layers arranged as editable engine data, not a flattened full-scene background

The base/background/foundation layer must not contain runtime-controlled objects unless the user explicitly asked for a single baked image:

- tall props, buildings, trees, rocks, crates, signs, doors, gates, pickups, chests, checkpoints, hazards, traps, turrets, tower objects, ladders, foreground occluders, destructibles, actors, enemies, NPCs, bosses, player characters, UI, labels, or any object that needs collision, interaction, replacement, reuse, y-sorting, animation, engine editing, or independent render order

If a generated base/background already contains those runtime objects, do not use it as the runtime base. Regenerate a cleaner foundation-only base or demote that image to a concept/reference artifact. The next in-world reference mockup is where proposed objects may appear, and the final runtime must still use separate generated props, platform objects, object layers, tile layers, collision, zones, and scene-hook metadata as appropriate.

## Parameter Contract

User-facing parameters may be stated in natural language:

- `map_mode`: tile_mode | scene_mode | side_scroll_mode | grid_mode | room_chunk_mode | baked_scene_mode
- `map_kind`: overworld | town | dungeon | shrine | arena | battle_bg | side_scroller | side_view_action | platformer | metroidvania | brawler | tower_defense | survivors_like | tactical | factory | card_board | room_chunk
- `visual_model`: baked raster | layered raster | tilemap | layered tilemap | parallax
- `size`: pixel dimensions, tile dimensions, or camera-relative size
- `stage_canvas`: exact pixel dimensions and aspect ratio for side-scroll/parallax layers, references, and previews
- `stage_segment_count`: number of camera-width chunks for a side-scroll stage
- `perspective`: top-down | 3/4 top-down | side-view | isometric-like
- `art_style`: clean_hd | pixel_inspired | retro_pixel | hand_painted | project-native
- `visual_asset_source`: image_gen | existing_assets | procedural_placeholder
- `collision_precision`: none | coarse | precise | tile | walkmesh
- `platform_strategy`: platform_rects_with_shared_tiles | platform_strip | tilemap | custom_terrain_chunks
- `prop_generation`: none | one_by_one | prop_pack_2x2 | prop_pack_3x3 | prop_pack_4x4 | platform_strip_1x3 | platform_strip_1x4 | custom_wide_pack
- `output_format`: PNG only | layered preview | manifest JSON | engine-native map data

When unspecified:

- Use `image_gen` as the visual asset source.
- Infer `map_mode` from genre and editing needs before selecting lower-level axes.
- Use `tile_mode` for Pokemon-like, top-down RPG, monster-taming, editor/grid-perfect, or tilemap requests.
- Use `scene_mode` for tower defense, survivors-like, cozy/top-down showcase maps, and base-map-plus-props requests.
- Use `side_scroll_mode` for side-scrollers, platformers, runners, side-view action, brawlers, Metroidvania side rooms, Mega Man-like, Castlevania-like, Contra-like, and parallax background requests.
- For `side_scroll_mode`, choose a canonical `stage_canvas` before image generation. Use the project camera/viewport aspect when available; otherwise default to a 16:9 side-scroller canvas such as `1536x864`. All primary parallax plates, stage references, and previews must preserve this same size/aspect.
- For playable `side_scroll_mode`, choose `stage_segment_count` before image generation. Default to 2 camera-width segments for a normal playable scrolling level. Use 1 only for explicit one-screen rooms, boss arenas, title-like scenes, or fixed battle rooms; use 3 or more only when the user asks for a longer stage or the existing game already has that scope.
- For playable `side_scroll_mode`, default `platform_strategy` to `platform_rects_with_shared_tiles`: write platform rectangles or engine-native platform objects as the gameplay source of truth, then skin them with a shared generated platform tile/strip library. Do not rely on a generated background or generic prop pack for platform shape.
- Use `grid_mode` for tactical RPGs, factory/automation maps, board/card battlers, build grids, and terrain-cost maps.
- Use `room_chunk_mode` for modular rooms, roguelike rooms, procedural room assembly, or Metroidvania room-chunk planning.
- Use `baked_scene_mode` only for non-playable visual scenes or explicitly flat images.
- Use `baked_raster + coarse_shapes` only for battle backgrounds, title/menu scenes, cutscenes, decorative backdrops, non-playable previews, or when the user explicitly asks for a single flat image.
- Use `layered_raster + y_sorted_props + precise_shapes` for top-down RPG exploration with tall props, occlusion, interactables, or reusable props; the base must be foundation-only and the props/interactables must remain separate.
- Use `tilemap` or `layered_tilemap` only when the engine/editor already uses tiles or the user asks for editable tiles; do not flatten gameplay objects into one background image.
- Use `parallax_layers + platform_objects + interactive_scene_objects + scene_hooks + precise_shapes` for playable side-view scrolling stages, platformers, runners, shooters, and horizontal action scenes; the parallax/background image is scenery-only and is not the runtime map by itself.
- Use square prop packs only when 4 or more compact small/medium static props share one style and fit comfortably inside equal square cells.
- Use one-by-one, platform strips, tile/object layers, or custom wide packs for hero props, buildings, gates, irregular large props, wide/tall props, platforms, terrain chunks, bridges, walls, ladders, long hazards, animated props, or props needing strong identity or collision alignment.
- For side-scroll platformers, treat platforms, floors, ledges, bridges, walls, slopes, ladders, long hazards, doors, gates, checkpoints, and exits as structural stage objects, not decorative props. Compact prop packs are for optional dressing and pickups only.
- Use `clean_hd` for generated exploration maps unless the project or user asks for pixel art. This means clean hand-painted top-down 2D RPG game map, HD game asset style, sharp readable terrain shapes, low texture noise, and no chunky pixels.
- Use `pixel_inspired` only when the user wants a pixel-adjacent look without retro chunkiness.
- Use `retro_pixel` only when the user explicitly asks for 16-bit, retro JRPG, or classic pixel-art maps.

## Workflow

1. Inspect the target game.
   - Find camera size, map dimensions, coordinate system, render order, asset loading, collision support, zone data, and existing map formats.
   - Preserve the engine's existing style and data contracts.

2. Choose the pipeline axes.
   - Choose `map_mode` first. Use the genre routing table when the user describes a game type instead of a technical map format.
   - Select `visual_model`, `runtime_object_model`, `collision_model`, and `engine_target`.
   - If the request is for a playable map, stage, level, room, prototype, or game scene, choose a pipeline with explicit runtime objects. Do not downgrade to `baked_raster` unless the user asked for a background-only image.
   - If the request implies a playable side-view scrolling/action stage, such as a side-scroller, platformer, runner, shooter, brawler, scrolling combat stage, Megaman-like stage, Castlevania-like stage, or Contra-like stage, lock the map pipeline to `parallax_layers + platform_objects + interactive_scene_objects + scene_hooks + precise_shapes` unless the engine already requires a tilemap.
   - Select `art_style`. Prefer readable gameplay shapes over decorative texture density.
   - Select `visual_asset_source`. Default to `image_gen`; use `existing_assets` only when the project already has suitable art; use `procedural_placeholder` only when explicitly requested.
   - Treat `hybrid` as a result of combining axes, not as a primary category.

3. Produce assets.
   - Write the creative prompts manually and use built-in `image_gen` for visible map art unless the user explicitly chose existing assets or procedural placeholders.
   - For baked raster maps, generate one background with built-in `image_gen`, or edit/use an existing image when supplied, then add optional collision/zones metadata.
   - For playable or editable layered maps, generate a foundation-only base/background first. The base must not contain runtime-controlled props, interactables, hazards, doors, gates, pickups, actors, or foreground occluders. If it does, regenerate or demote it to a reference artifact.
   - For layered raster maps, generate a ground-only/foundation-only base map first. Then perform the visual reference handoff and generate an in-world dressed reference mockup from the visible base before making final props and placements.
   - For tilemaps, generate or reuse tileset art first, then follow the engine/editor format for layers, objects, collision, and scene files. Do not script-draw the tileset as the final art source, and do not flatten object layers into a single runtime image.
   - For `grid_mode`, generate or reuse grid/tileset visual art first, then write cell metadata such as walkable/buildable flags, move cost, terrain effects, resource nodes, and object layers.
   - For `room_chunk_mode`, define chunk dimensions, exits, connection sockets, collision contract, and spawn/trigger metadata before final art assembly. Chunks must be reusable and validated at their seams.
   - For playable side-view scrolling/action stages, define the canonical `stage_canvas`, `stage_segment_count`, `stage_length`, and `platform_strategy` before generating art. Do not ask image generation for one ultra-wide full level. Generate per-segment or loopable scenery-only parallax plates first: `sky`, `far_bg`, `mid_bg`, `near_bg`, and optional `foreground_overlay`. Every primary parallax layer must use the same pixel dimensions, aspect ratio, camera framing, horizon line, and top-left anchor as the `stage_canvas`; do not accept mismatched image sizes that require guesswork to stack. Do not treat one full-width background image as a complete `side_scroll_mode` background stack unless the user explicitly asks for a flat/non-parallax background. These parallax passes must not contain playable foreground platforms, walkable floors, terrain chunks, hazards, pickups, doors, gates, checkpoints, crates, fences, spikes, or other runtime objects. Then perform the visual reference handoff and generate an in-world stage reference mockup that visually places up to 9 distinct intended platform/object candidates before generating final separate scene objects and metadata.
   - If a side-view background already contains collidable-looking foreground geometry, walkable floors, or reusable gameplay props, reject it as a runtime background and regenerate a cleaner scenery-only background before continuing.
   - Treat the reference mockup as a checkpoint, not a deliverable. Do not stop after generating it. After the relevant `dressed-reference` or `stage-reference` exists, inspect it and continue into the post-reference object production gate.
   - Do not present a rerunnable script that creates the whole art pack as the main solution unless the user asked for procedural placeholder art.

4. Build metadata.
   - Store prop placement, player spawns, actor spawn marker metadata, interactable scene objects, blockers, walk bounds, encounter zones, exits, camera bounds, and triggers as structured data.
   - For `grid_mode`, store grid dimensions, cell size, tile ids, terrain types, walkable/buildable flags, movement cost, collision, resource nodes, and object/entity slots.
   - For `room_chunk_mode`, store chunk id, size, entrances/exits, connection sockets, collision, spawn markers, camera bounds, and validation hints for seam alignment.
   - For `side_scroll_mode`, store `stage_canvas`, `stage_segment_count`, `stage_length`, segment ids, parallax layer source size, display size, anchor, render order, scroll factors, loop/repeat policy, shared platform/object library ids, camera bounds, platform collision, hazards, exits, checkpoints, and actor spawn marker metadata.
   - Keep collision independent from pixels unless the target engine explicitly uses tile collision.

5. Validate and preview.
   - Compose a flattened preview for layered maps.
   - Validate image sizes, alpha channels, prop pack extraction metadata, JSON parseability, and critical walkability points when collision matters.
   - For `side_scroll_mode`, reject or normalize mismatched primary parallax layer sizes before runtime integration. The stage reference and QA preview must match `stage_canvas` exactly. Deterministic resizing/cropping/padding is allowed only as a normalization step on generated art, not as a way to invent missing art.

## Prop Generation Rules

Use `$generate2dsprite` for reusable transparent props and visible scene objects, but the agent must write the prop prompt itself using the selected map `art_style`. Do not use a script to generate the creative prompt. For `clean_hd` maps, explicitly request clean hand-painted HD 2D game assets and explicitly forbid pixel art. For `pixel_inspired`, request clean modern pixel-art-inspired props without retro chunkiness. For `retro_pixel`, request 16-bit or retro JRPG pixel art.

Before any prop/object image generation, classify each visible runtime object from the reference mockup:

- `compact_prop`: small/medium, roughly square or vertical, decorative or simple blocker, no exact alignment requirement
- `wide_or_long_object`: expected aspect ratio wider than about `1.6:1`, such as platforms, floor pieces, bridges, wall runs, fence rows, long traps, long signs, pipes, rails, ledges, or roads
- `tall_or_large_object`: expected aspect ratio taller than about `1.6:1` or visually dominant, such as large trees, gates, towers, buildings, banners, doors, statues, or boss-room props
- `collision_bearing_object`: must line up with collision, walkable edges, build pads, doors, checkpoints, gates, hazards, or engine editor handles
- `tileset_or_strip_piece`: should repeat seamlessly or assemble from left/middle/right caps, corners, slopes, tops, sides, or tile pieces

Generation strategy is determined by that classification:

- Only `compact_prop` objects may use square `prop_pack_2x2`, `prop_pack_3x3`, or `prop_pack_4x4`.
- Do not put `wide_or_long_object`, `tall_or_large_object`, `collision_bearing_object`, or `tileset_or_strip_piece` into square prop packs.
- Use `one_by_one` for important, large, tall, irregular, identity-sensitive, or collision-aligned objects.
- Use `platform_strip_1x3` or `platform_strip_1x4` for repeatable floors/platforms: left cap, middle repeat, right cap, plus optional corner/slope/end variant.
- Use `custom_wide_pack` only for several similar wide objects that share one category and can use wide cells such as `768x256`, `1024x384`, or another explicit non-square cell size.
- Never mix compact decorative props with platforms, terrain chunks, gates, doors, hazards, or other collision-critical objects in the same generated sheet.
- If a square pack fails because a wide/tall object touches an edge, do not retry the same square pack with looser QC. Reclassify that object and regenerate it one-by-one, as a platform strip, as a custom wide pack, or as tile/object-layer art.

Choose the generation shape deliberately:

- `one_by_one`: safest for large, important, animated, or irregular props.
- `prop_pack_2x2`: 4 related compact props, safest square batch size.
- `prop_pack_3x3`: 9 compact small/medium props, good quality/time tradeoff.
- `prop_pack_4x4`: 16 very simple compact small props; fastest but most likely to drift or touch edges.
- `platform_strip_1x3`: repeatable non-actor platform/floor strip with left cap, middle repeat, and right cap.
- `platform_strip_1x4`: repeatable non-actor platform/floor strip with left cap, middle repeat, right cap, and one extra slope/corner/end variant. This is not an animation-frame format and must not be used for characters, enemies, creatures, NPCs, summons, or animated body assets.
- `custom_wide_pack`: several related wide objects using explicit wide cells, not square cells.

Prop packs save image-generation calls and prompt overhead, but reduce per-prop control. Use square prop packs for rocks, shrubs, barrels, small signs, lamps, crates, floor ornaments, plants, and repeated compact environmental props. Do not use square prop packs for buildings, gates, trees with wide canopies, bridges, platforms, floors, walls, ladders, long fences, long hazards, character-like statues, hero objects, or anything that must be pixel-perfect or collision-aligned.

For layered maps with generated props, prefer this in-world reference mockup pipeline:

1. Generate `assets/map/<name>-base.png` as ground-only terrain.
2. Make the base image visible in conversation context. If the base is a local file, use `view_image` immediately before calling built-in `image_gen`; do not rely on a path string as the reference.
3. In the dressed-reference prompt, explicitly say: use the visible base image immediately above as the visual reference, preserve its camera/framing/dimensions/terrain/road/water/boundaries, and generate an in-world dressed reference mockup.
4. The dressed reference must show proposed props as natural game-world objects placed on the base. It must not contain circles, arrows, outlines, labels, text, callouts, legends, highlighted boxes, or other annotation graphics.
5. The dressed reference should contain at most 9 distinct visible prop/object candidates unless the user explicitly asks for more. Prefer the objects that will become final generated props, collision blockers, interactables, or occluders.
6. Generate `assets/map/<name>-dressed-reference.png` from the visible base. Treat this as a reference mockup, not the final runtime map.
7. Generate one-by-one props or a prop pack based on the dressed reference.
8. Place extracted props over the original base and compose a flattened preview.
9. Validate that base, dressed reference, and preview dimensions match.

Use `scripts/extract_prop_pack.py` after generating a solid-magenta prop sheet. If the sheet has antialiased magenta fringe, run the imagegen chroma-key helper with soft matte and despill before extraction, then extract from the alpha-cleaned sheet. Use `scripts/compose_layered_preview.py` to verify placement over the base map.

## Post-Reference Object Production Gate

An in-world reference mockup is never the final deliverable by itself. After generating `dressed-reference` or `stage-reference`, continue with:

1. Make both images visible in conversation context before any object/prop generation:
   - the original `base` or `background`
   - the generated `dressed-reference` or `stage-reference` mockup
2. If either image is a local file, call `view_image` on it immediately before writing object lists or object/prop image prompts. Do not rely on file paths alone.
3. Create a concrete object list from the visible reference mockup while cross-checking the original base/background: object id, type, approximate position, approximate size, render layer, collision role, and asset strategy.
   - If the reference contains more than 9 distinct visible runtime object candidates, reduce the generated asset list to the 9 most gameplay-relevant candidates first, then represent extra repeats or low-value decorations through placement metadata or a later asset pass.
   - Classify every object before generation. Compact decorative props may be batched; wide/long, tall/large, collision-bearing, and tileset/strip objects must use one-by-one, strip, custom wide pack, tile/object-layer, or engine-native strategies.
4. For each visible runtime object, choose exactly one asset strategy:
   - generate a separate transparent asset with `$generate2dsprite` or direct `image_gen`
   - extract it from a generated prop/object pack
   - represent it as a tile/object layer if the engine/editor pipeline is tile-based
5. For every object/prop image prompt, explicitly state that the visible original base/background and visible reference mockup above are the visual context. The generated asset must match the original map style and correspond to an object visible in the reference mockup.
6. Generate or define the final platforms, terrain chunks, props, hazards, pickups, doors, gates, checkpoints, exits, foreground occluders, and other visible scene objects. Do not skip this step just because the reference mockup already contains them visually.
7. Write placement metadata such as `data/<name>-props.json`, `data/<name>-objects.json`, engine-native object layers, or tile/object data.
8. Write collision, zones, scene hooks, camera bounds, and exits as structured metadata.
9. Compose a QA preview from the original base/background plus final runtime objects.

Reference-only output is incomplete for any playable map, layered map with props, side-view stage, engine scene, or request that asks for separate props/editable objects. Only stop at a reference mockup if the user explicitly asks for a reference-only concept image.

For prop packs or object packs generated after a reference mockup, the prompt must be derived from the visible reference mockup and original base/background, not from memory or filenames. It should list the exact objects being generated and preserve the art style, lighting, perspective, and scale cues from the original base/background.

## Side-Scroll Stage Segments

Playable side-scroll stages should be planned as camera-width segments, not as one huge generated map image. This keeps image generation readable while allowing the runtime level to be longer than a single picture.

Default contract:

- Use one `stage_canvas` for every segment, primary parallax plate, stage reference, stage preview, and normalization target.
- Use `stage_segment_count: 2` by default for a normal playable platformer or action side-scroller. Use `1` only for explicit one-screen rooms, boss arenas, fixed battle rooms, or background-only requests.
- Compute `stage_length` from the engine camera width and segment count, or from the existing project coordinate system when available.
- Name segment files predictably, such as `segment-01`, `segment-02`, and keep each segment aligned to the same top-left camera frame.
- Generate either per-segment parallax plates or loopable parallax plates, but record the choice in metadata. Do not mix image sizes or aspect ratios across segments.
- Create one shared platform/object art library for the full stage. Do not generate a fresh unrelated prop pack for every segment unless the user explicitly asks for biome changes.
- Compose a per-segment QA preview and, when practical, a stitched stage overview preview from the same shared object metadata.

For platformer-style side-scroll stages, the runtime geometry must come from explicit platform/object metadata or engine-native nodes:

- `platforms`: rectangles, tile spans, slopes, one-way ledges, moving platform handles, or engine-native platform nodes.
- `terrain_chunks`: optional large reusable solid pieces when rectangles are not enough.
- `hazards`: spikes, lasers, lava strips, pits, saws, traps, and other collision-critical objects.
- `interactives`: doors, gates, terminals, switches, checkpoints, pickups, exits, and destructibles.
- `scene_hooks`: player spawn, actor spawn markers, camera bounds, lock zones, arena triggers, exit links, and checkpoint ids.

The art for structural geometry should skin the metadata, not define it. A good platformer stage can use simple collision rectangles with repeated platform cap/middle sprites, just like a classic tile or strip workflow. This is preferable to asking image generation to produce a complete foreground platform layout in one baked image.

## Side-Scroll Shared Platform Library

For `side_scroll_mode`, generate or select a shared structural platform library before decorative props:

- `platform_strip_1x3`: left cap, seamless middle repeat, right cap.
- `platform_strip_1x4`: left cap, seamless middle repeat, right cap, plus one slope/corner/broken/end variant.
- `platform_tile_set`: square or rectangular tiles for top edge, underside, wall face, corner, and optional slope when the target engine/editor is tile-based.
- `custom_terrain_chunk`: one-by-one or custom wide assets for important unique terrain chunks.

Use the platform library across all stage segments. Platform objects in metadata should reference the shared library by id and declare display width, display height, anchor, repeat policy, collision rectangle, and render layer.

Do not put platform strips, floors, ledges, bridges, ladders, walls, slopes, long spike rows, or terrain chunks into ordinary square prop packs. Square prop packs are for compact decorative props and simple pickups. Structural objects need strips, tiles, custom wide cells, one-by-one assets, or engine-native tile/object layers.

## Playable Stage Reference Rules

For playable side-view scrolling/action maps, an in-world stage reference mockup is mandatory before generating final scene objects or scene metadata. This applies across art styles and game styles, including pixel art, clean HD, side-scrollers, platformers, runners, shooters, brawlers, scrolling combat stages, and Megaman-like or Castlevania-like stages:

0. Choose and record one `stage_canvas`, for example `1536x864` for a default 16:9 HD side-scroller when the project has no explicit camera size. Choose and record `stage_segment_count` before art generation. All primary parallax layers, stage references, and stage previews must share this exact size unless a layer is explicitly marked as a repeatable strip.
   - For normal playable platformer/action stages, default to `stage_segment_count: 2`. This means at least two camera-width segments that share one platform/object art library.
   - Use `stage_segment_count: 1` only for explicit one-screen rooms, boss arenas, fixed battle rooms, or background-only requests.
   - Do not make a longer level by asking for one ultra-wide generated painting. Keep each generated segment at `stage_canvas` size and assemble the runtime stage from segments plus object metadata.
1. Generate named parallax scenery layers as separate runtime images: `assets/map/<name>-sky.png`, `assets/map/<name>-far-bg.png`, `assets/map/<name>-mid-bg.png`, `assets/map/<name>-near-bg.png`, and optional `assets/map/<name>-foreground-overlay.png`.
- These layers are scenery only, not playable foreground. They may contain sky, clouds, mountains, distant buildings, distant castle walls, silhouettes, atmosphere, and non-colliding far depth.
- Do not collapse these layers into only `assets/map/<name>-background.png` for a playable `side_scroll_mode` stage. A single scenery background is allowed only when the user explicitly requests a flat/non-parallax background; in that case still continue with stage reference, separate objects, collision, camera bounds, and QA preview.
   - Each primary layer prompt must specify the same target canvas size/aspect ratio, same camera framing, same horizon height, and same top-left aligned composition. If image generation returns different sizes, regenerate or normalize them to `stage_canvas` before using them together.
   - Repeatable strips and foreground/object sprites may have different source dimensions, but they must declare display size, anchor point, repeat axis, and scale in metadata. They are not substitutes for the primary parallax plates.
   - It must not contain walkable floors, platform tops, terrain chunks, spike traps, pickups, crates, doors, gates, checkpoints, ladders, near fences, near stone walls, enemies, player characters, UI, labels, or any object that should later be edited, collided with, reused, or layered independently.
   - Keep the playable foreground lane visually open or neutral so separate platform/object layers can stack clearly over it.
2. Make the background visible in conversation context. If it is a local file, use `view_image` immediately before calling built-in `image_gen`; do not rely on a path string as the reference.
3. In the stage-reference prompt, explicitly say: use the visible background image immediately above as the visual reference, preserve exact camera/framing/dimensions/horizon/depth/entrances/exit direction, and generate an in-world stage reference mockup.
4. Generate `assets/map/<name>-stage-reference.png` from the visible background.
5. In the stage reference, visually place the intended scene layout as natural game-world objects or subtle blockout geometry: platforms or walkable lanes, terrain chunks, foreground occluders, hazards, pickups, doors, checkpoints, gates, and exits.
   - Use at most 9 distinct visible runtime object candidates in the stage reference unless the user explicitly asks for a larger object pass. Repeated placements of the same platform, terrain chunk, hazard, pickup, checkpoint, door, gate, or occluder count as one candidate and should be repeated later in metadata.
   - Prioritize objects that the final game must render or collide with separately. Avoid filling the mockup with many small decorative foreground props that will not become reusable assets.
   - For multi-segment stages, each segment may have its own stage reference, but all references must reuse the same structural platform/object library and camera frame. If time or cost requires only one reference, make it the style/layout reference for the shared object library and write separate segment placement metadata.
6. Do not draw spawn markers, actor markers, arena trigger zones, camera bounds, arrows, labels, circles, outlines, numbered callouts, text, legends, or UI overlays in the reference image. Record player spawn, actor spawn markers, arena triggers, camera bounds, and exit links later as scene-hook metadata.
7. Use the stage reference to decide object identities, sizes, coordinates, render order, collision shapes, and camera bounds.
8. Continue through the post-reference object production gate: generate or define final platforms, terrain chunks, hazards, pickups, doors, checkpoints, foreground occluders, and other visible scene objects as separate assets, tile layers, or object layers. Create segment placement metadata that reuses the shared platform/object library across the full `stage_length`. Compose the final runtime preview from the original background plus these separate runtime objects.

The stage reference is an in-world reference mockup. Do not ship it as the runtime map, do not infer collision from its pixels, and do not cut platform objects out of the baked reference image. If a platform must be reusable or collidable, generate it as a separate platform object, terrain chunk, tile, or engine-native object.

If the generated background already has obvious foreground gameplay pieces baked into it, do not use it as `background` in runtime data. Regenerate the scenery-only background or demote that image to a concept/reference artifact.

Scene hooks are metadata only. Do not generate enemy, boss, NPC, player, projectile, or animation sprites inside `generate2dmap`; call `$generate2dsprite` separately when the game needs those assets.

If a playable side-view scrolling/action run has already generated a background but has not generated `assets/map/<name>-stage-reference.png`, pause the platform/props pipeline and generate the stage reference next. Background plus props is not enough evidence that the level layout is coherent.

## Expected Deliverables

For a baked raster map:

- `assets/map/<name>.png`
- optional `<name>.prompt.txt`
- optional `data/<name>-collision.json` or `data/<name>-zones.json`
- code changes that load/use the image

Use this deliverable only for non-playable backgrounds or explicitly requested flat images. If actors must move through the scene, collide with level geometry, jump on platforms, collect items, trigger doors, or edit the level later, upgrade to a layered, parallax-stage, tilemap, or engine-native deliverable.

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

For a playable side-view scrolling/action stage:

- image-generated parallax scenery layers such as `assets/map/<name>-sky.png`, `assets/map/<name>-far-bg.png`, `assets/map/<name>-mid-bg.png`, `assets/map/<name>-near-bg.png`, and optional `assets/map/<name>-foreground-overlay.png`
- one recorded `stage_canvas` shared by the primary parallax layers, `stage-reference`, and `stage-preview`
- one recorded `stage_segment_count` and `stage_length`; default to at least 2 camera-width segments for a normal playable scrolling level
- per-segment parallax layer metadata or loopable parallax metadata, with consistent dimensions and anchors
- `assets/map/<name>-background.prompt.txt` and prompt files/manifests for other generated visual assets
- `assets/map/<name>-stage-reference.png` as an in-world reference mockup for platform/object placement
- a shared platform/object art library, such as platform strips, platform tiles, terrain chunks, hazards, doors, pickups, checkpoints, gates, and exits
- separate image-generated platform, terrain-chunk, foreground-occluder, hazard, door, pickup, checkpoint, gate, and exit sprites when these are visible scene objects
- `data/<name>-objects.json` or engine-native object layers for platforms, terrain chunks, hazards, pickups, doors, checkpoints, gates, exits, and foreground occluders, grouped by segment when the stage has more than one segment
- `data/<name>-scene-hooks.json` or engine-native metadata for player spawns, actor spawn marker metadata, encounter/arena triggers, camera bounds, and exit links
- `data/<name>-collision.json` with explicit platform/solid geometry independent from the background pixels
- `assets/map/<name>-stage-preview.png` composed from the background plus objects for QA only
- code or scene changes that load the background, render object layers, and use the collision/object data as runtime gameplay data

Do not accept a single generated side-view action/platformer stage image plus collision rectangles as the final playable map. Do not accept a single camera-width scenery image as a complete scrolling level unless the user explicitly asked for a one-screen room. The stage must expose platforms or walkable lanes, hazards, doors, pickups, checkpoints, gates, exits, scene hooks, and camera bounds as separate runtime objects, tile/object layers, or metadata. Runtime `background` fields must point to the scenery-only background or parallax layer, never to `stage-reference` or `stage-preview`; previews are QA artifacts only.

For `grid_mode`:

- image-generated or user-supplied tileset/grid art
- grid dimensions, cell size, and map data in project-native JSON, Tiled JSON, LDtk, Godot TileMap, Unity Tilemap, or equivalent
- cell metadata for walkable/buildable, movement cost, terrain effects, resources, collision, and placement rules
- object layers for units, buildings, machines, cards/board slots, exits, spawns, and triggers
- a QA preview that can show optional debug grid/collision overlays

For `room_chunk_mode`:

- reusable chunk art or tile/object layers
- chunk metadata with `chunk_id`, size, entrances/exits, connection sockets, spawn markers, blockers, hazards, and camera bounds
- collision and seam validation metadata
- a chunk preview and, when multiple chunks exist, an assembled layout preview

For `scene_mode`:

- foundation-only `assets/map/<name>-base.png`
- in-world `assets/map/<name>-dressed-reference.png`
- separate props/interactables/blockers from one-by-one assets or compact prop packs
- placement, collision, zones, exits, camera bounds, and scene-hook metadata
- a QA preview composed from the base plus final runtime objects

For a prop pack:

- raw generated sheet with solid `#FF00FF` background
- extracted `assets/props/<prop>/prop.png` files
- `prop-pack.json` extraction manifest
- no `edge_touch` entries for accepted props

## Validation

Always validate what the chosen pipeline requires:

- map files exist and have expected dimensions
- prompt files or prompt manifest fields exist for generated visible assets
- transparent props contain alpha
- prop pack manifests parse and accepted props do not touch cell edges
- placement JSON parses and referenced prop files exist
- collision/zones JSON parses when present
- critical spawn, path, entrance, blocker, and zone points behave as expected
- playable/editable layered maps use a foundation-only base/background and do not bake runtime-controlled props, interactables, hazards, doors, gates, pickups, actors, foreground occluders, or reusable scene objects into the base
- playable stages have explicit runtime objects or metadata for every gameplay-relevant platform or walkable lane, blocker, hazard, door, pickup, checkpoint, gate, exit, player spawn, actor spawn marker, encounter/arena trigger, and camera bound
- playable side-view backgrounds are scenery-only and do not contain baked-in foreground gameplay platforms, hazards, pickups, doors, gates, checkpoints, or other reusable runtime objects
- `side_scroll_mode` primary parallax layers, stage references, and stage previews match the recorded `stage_canvas`; any repeatable strips or differently sized foreground sprites declare display size, anchor, scale, and repeat policy
- `side_scroll_mode` records `stage_segment_count`, `stage_length`, segment ids, and whether parallax plates are per-segment or loopable
- normal playable `side_scroll_mode` levels have at least 2 camera-width segments unless the user requested a one-screen room, boss arena, fixed battle room, or background-only asset
- `side_scroll_mode` parallax layers have explicit render order, scroll factors, dimensions, loop/repeat policy, and are not used as collision sources
- `side_scroll_mode` platforms and terrain use explicit platform/object metadata plus a shared platform strip, tile, or terrain-chunk library; they are not taken from a generic square prop pack
- `grid_mode` outputs include grid dimensions, cell size, cell metadata, object layers, and validation of critical walkable/buildable cells
- `room_chunk_mode` outputs include chunk dimensions, exits/connection sockets, seam validation, collision, and at least one assembled or per-chunk preview
- stage-reference maps preserve the background dimensions and their object plan matches the final object/collision metadata
- stage-reference and dressed-reference mockups contain no more than 9 distinct visible runtime prop/object candidates unless the user explicitly requested a larger pass
- reference mockups are followed by final props/objects, placement metadata, collision/scene-hook metadata, and a QA preview unless the user explicitly requested reference-only output
- flattened preview looks coherent at the game's camera size
