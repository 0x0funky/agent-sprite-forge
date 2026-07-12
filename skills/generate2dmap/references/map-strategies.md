# Map Pipeline Selection

Choose maps by first selecting a product-level `map_mode`, then mapping that mode to pipeline axes. Avoid treating `hybrid` as a top-level strategy; most real 2D maps are hybrid combinations of visual art, objects, and collision metadata.

## Core Map Modes

Use these modes as the first decision layer:

- `tile_mode`: editable tile/grid maps. Use for Pokemon-like routes, top-down RPG towns, monster-taming exploration, platformer tilemaps, tactical maps, factory maps, and projects that already use Tiled, LDtk, Godot TileMap, Unity Tilemap, or Phaser tilemaps.
- `scene_mode`: foundation/base map plus separate props. Use for tower defense, survivors-like arenas, cozy/top-down showcase maps, visual adventure scenes, and base-map-plus-props requests.
- `side_scroll_mode`: parallax side-scroller scenes. Use for Mega Man-like, action platformer, Metroidvania side rooms, runners, side-view shooters, and brawlers.
- `grid_mode`: rule-heavy grid scenes. Use for tactical RPGs, factory/automation games, board/card battlers, build grids, terrain-cost maps, and resource maps.
- `room_chunk_mode`: modular room/chunk generation. Use for roguelike rooms, dungeon chunks, procedural level assembly, and Metroidvania room networks.
- `baked_scene_mode`: fixed visual backgrounds. Use only for title screens, visual novel backgrounds, point-and-click scenes, boss arena concept art, non-playable showcase images, or explicit flat-image requests.

Modes are not final file formats. After choosing a mode, still define `visual_model`, `runtime_object_model`, `collision_model`, and `engine_target`.

## Genre Routing Table

| User asks for | Default mode | Notes |
| --- | --- | --- |
| Pokemon-like, monster-taming RPG, top-down RPG route/town | `tile_mode` | Add props, encounter zones, exits, NPC/actor spawn markers, and collision. |
| Tower defense, Kingdom Rush-like | `scene_mode` | Add path metadata, build slots, spawn/exit hooks, blockers, and optional engine scene scaffold. |
| Survivors-like arena | `scene_mode` or `tile_mode` | Use sparse blockers, spawn zones/rings, camera bounds, and props. |
| Mega Man-like, side-view action, platformer, runner | `side_scroll_mode` | Use parallax layers plus platform/object/collision metadata. |
| Metroidvania | `side_scroll_mode` or `room_chunk_mode` | Use room/chunk exits and camera bounds; use tilemap when the engine expects grid collision. |
| Beat-em-up / brawler | `side_scroll_mode` | Use parallax/background depth plus a walkable belt polygon, enemy wave zones, and props. |
| Tactical RPG, grid strategy | `grid_mode` | Store terrain, move cost, defense/effects, unit slots, and collision. |
| Factory / automation | `grid_mode` | Store buildable cells, resource nodes, machine slots, belts/conveyors, and item lanes. |
| Card/board battler or UI-heavy game | `grid_mode` | Store board slots, UI zones, interaction regions, and background art. |
| Roguelike room, procedural dungeon, modular rooms | `room_chunk_mode` | Store chunk sockets, exits, collision, spawn markers, and seam validation. |
| Visual novel, title screen, fixed battle background | `baked_scene_mode` | Use only when no runtime editing/collision is needed. |

## Playable Map Default

When the user asks for a playable game map, level, stage, room, prototype, or engine scene, the runtime map must not be only a flattened generated image. A single baked image can be used as a background, in-world reference mockup, or QA preview, but playable output needs explicit runtime structure:

- top-down maps: ground/base layer plus separate props, object placement, collision, zones, exits, and spawn data
- side-view scrolling/action stages: background/parallax layers plus an in-world stage reference mockup, platform objects or walkable lanes, terrain chunks, foreground occluders, hazards, doors, pickups, checkpoints, scene hooks, camera bounds, and collision
- tile/editor workflows: generated or supplied tileset art plus tile layers, object layers, collision, zones, and engine-native scene/map data

If the request mentions "game", "playable", "prototype", "level", "stage", "side-view action", "side-scroller", "platformer", "Megaman-like", "RPG exploration", "tower defense", or engine integration, start from the nearest playable preset below instead of `baked_raster`.

## Mode Deliverable Contracts

### `tile_mode`

Deliver tileset art, map data, tile layers, object layers, collision, exits, and a preview. Good output formats include Tiled JSON, LDtk, Godot TileMap, Unity Tilemap, Phaser tilemaps, or project-native JSON.

Use `generate2dsprite` only when reusable transparent props, NPCs, animated objects, or non-tile scene objects are actually needed. A pure terrain map can stay tileset + map data + collision metadata.

### `scene_mode`

Deliver a foundation-only base map, an in-world dressed reference, final separate props/interactables/blockers, placement metadata, collision/zones/exits/camera bounds, and a composed QA preview.

This is the default for beautiful top-down demos, tower defense scenes, survivors-like arenas, and base-map-plus-props workflows.

### `side_scroll_mode`

Deliver scenery-only parallax layers plus separate playable foreground objects. Typical visual layers:

- `sky`
- `far_bg`
- `mid_bg`
- `near_bg`
- optional `foreground_overlay`

Then deliver platform tiles/objects, terrain chunks, hazards, doors, checkpoints, pickups, exits, camera bounds, scroll factors, collision, scene hooks, and a QA preview. Parallax layers create depth; they are not collision sources.

Choose one `stage_canvas` before generation. The primary parallax plates, stage reference, and QA preview must share the same pixel dimensions, aspect ratio, camera framing, horizon, and top-left anchor. Default to the project camera aspect ratio; when unknown, use a 16:9 side-scroller canvas such as `1536x864`.

For playable side-scrollers, also choose `stage_segment_count` before generation. Default to 2 camera-width segments for a normal playable level. Use 1 only for explicit one-screen rooms, boss arenas, fixed battle rooms, or background-only requests. Avoid one ultra-wide generated map image; use same-sized segments, loopable parallax plates, or per-segment parallax plates plus shared object metadata.

For platformers and side-view action games, treat platforms as structural runtime geometry. The stable pattern is explicit platform rectangles or engine-native platform objects skinned by a shared platform strip/tile library. Use compact prop packs for decoration and small pickups, not for the main floors, ledges, bridges, walls, ladders, slopes, or long hazards.

For brawlers, use the same mode but replace jump-platform geometry with a walkable belt polygon, foreground/background props, enemy wave zones, and camera locks.

### `grid_mode`

Deliver grid dimensions, cell size, tiles/cells, terrain metadata, walkable/buildable flags, movement cost, resource or terrain effects, collision, object layers, and a preview with optional debug overlay.

Prioritize validation over beauty. The map must be readable by game logic.

### `room_chunk_mode`

Deliver reusable room/chunk art or tile/object layers, chunk dimensions, exits, connection sockets, collision, spawn markers, camera bounds, and seam validation. If multiple chunks exist, also deliver an assembled layout preview.

### `baked_scene_mode`

Deliver a fixed image plus optional coarse collision/zones only. Do not use this mode for editable or playable maps unless the user explicitly requests a flat background.

## Visual Asset Source

Default to built-in image generation for visual assets. Base maps, in-world reference mockups, dressed references, stage references, prop sheets, prop sprites, tileset art, parallax layers, and battle backgrounds should come from `image_gen` unless the user supplies existing art or explicitly asks for procedural placeholders.

Scripts may slice, assemble, chroma-key, validate, compose previews, create metadata, and emit engine files. They must not replace image generation as the creative art source for final map visuals. Engine outputs such as Godot `.tscn`, Tiled JSON, LDtk data, or Unity placement data should wire up image-generated or user-supplied assets.

## In-World Reference Mockups

Use an in-world reference mockup whenever object placement must be visually coherent but the final runtime needs separate objects.

- Top-down layered maps use `assets/map/<name>-dressed-reference.png`: base map plus proposed props rendered as natural game-world objects.
- Side-view scrolling/action stages use `assets/map/<name>-stage-reference.png`: background/parallax base plus proposed platforms or walkable lanes, hazards, pickups, doors, checkpoints, gates, and exits rendered as natural game-world objects or subtle in-world blockout geometry.
- Reference mockups must preserve exact camera, framing, dimensions, terrain/background, entrances, exits, and collision-relevant boundaries from the base image.
- Reference mockups should include at most 9 distinct visible runtime prop/object candidates unless the user explicitly asks for a larger pass. Repeated placements of the same object count as one candidate and should be repeated later in placement metadata.
- Reference mockups are planning artifacts only. Do not ship them as runtime maps, infer collision from their pixels, or cut final platform/prop assets out of them.
- Final output must still use separate props/platform objects, scene-object metadata, collision, zones, scene hooks, tile/object layers, or engine-native nodes.
- Character, enemy, boss, projectile, player, NPC, and animation sprites are outside the map deliverable. Store actor spawn markers and encounter/arena hooks as metadata only, then use `$generate2dsprite` if those actor assets are needed.
- Do not create annotated diagrams. Reference mockups must not contain circles, arrows, outlines, labels, numbers, UI callouts, text, captions, legends, highlighted boxes, highlighted zones, measurement lines, or explanatory overlays.
- Do not stop after the reference mockup. Reference-only output is incomplete unless the user explicitly asked for a reference-only concept image.

## Visual Reference Handoff

Reference mockups must be generated from the actual visible base/background image:

1. Save the base/background image first.
2. Immediately before the reference-mockup `image_gen` call, make the exact image visible in conversation context. For local files, call `view_image` on the saved image.
3. The next image prompt must explicitly say to use the visible image immediately above as the visual reference.
4. The prompt must name concrete features from the viewed image to preserve: camera framing, dimensions, horizon, terrain boundaries, road/water shapes, entrances, exits, major silhouettes, and landmark positions.
5. The prompt must ask for an in-world reference mockup, not an annotated planning diagram.
6. The prompt should render only visible scene objects: props, platforms, terrain chunks, hazards, gates, pickups, checkpoints, doors, exits, foreground occluders, or subtle blockout geometry.
7. Non-visual data such as player spawns, actor spawn markers, camera bounds, patrol hints, and encounter/arena triggers must be written later as scene-hook metadata, not drawn into the image.

Do not rely on filenames, paths, or vague phrasing such as "based on this map". If the image is not visible in context, stop and make it visible before generating the dressed reference or stage reference.

## Layer Separation Contract

For any playable or editable layered map, the first generated base/background/foundation image must not bake in objects that the runtime should control separately. This applies across top-down RPG maps, monster-taming maps, tactical arenas, tower-defense lanes, side-view platformers, parallax stages, tile/editor workflows, clean HD, pixel-inspired, and retro pixel art.

Allowed in the base/background/foundation layer:

- top-down or 3/4 maps: ground material, paths, roads, water, cliffs, low terrain markings, floor patterns, and terrain boundaries
- tactical or tower-defense maps: ground, lanes, roads, build pads, lane markings, terrain zones, and non-interactive floor detail
- side-view stages: sky, far/mid scenery, distant buildings, distant terrain silhouettes, atmosphere, and non-colliding depth
- tilemaps: tileset art and editable tile layers, not a flattened full-scene background

Not allowed in the runtime base/background/foundation layer unless the user explicitly asks for a single baked image:

- tall props, buildings, trees, rocks, crates, signs, doors, gates, pickups, chests, checkpoints, hazards, traps, turrets, tower objects, ladders, foreground occluders, destructibles, actors, enemies, NPCs, bosses, player characters, UI, labels, or any object that needs collision, interaction, replacement, reuse, y-sorting, animation, engine editing, or independent render order

If a generated base/background contains runtime-controlled objects, regenerate a cleaner foundation-only base or demote that image to a concept/reference artifact. Proposed objects belong in the in-world reference mockup, then in final separate props, platform objects, object layers, tile layers, collision, zones, and scene-hook metadata.

## Side-Scroll Parallax Contract

`side_scroll_mode` uses parallax background as a core stage-building method. It should produce a layered depth stack, not one crowded full-stage painting.

Typical layer responsibilities:

- `sky`: sky, moon/sun, far atmosphere; scroll factor near `0.0` to `0.1`.
- `far_bg`: mountains, skyline, far castle/factory silhouettes; slow scroll factor.
- `mid_bg`: readable landmarks and large distant structures; medium scroll factor.
- `near_bg`: near non-colliding scenery behind gameplay objects; faster scroll factor but still not collision.
- `foreground_overlay`: optional fog, chains, pipes, silhouettes, smoke, or framing elements that render above actors but do not define gameplay collision.

Generate parallax layers as scenery-only art. Platforms, walkable floors, ladders, hazards, gates, doors, pickups, checkpoints, and collision-critical props belong in platform/object/tile layers, not in parallax backgrounds.

All primary parallax plates must use the same `stage_canvas`. If image generation returns inconsistent dimensions, regenerate or normalize the generated layer before runtime use. Do not rely on the engine to guess scaling between mismatched layer sizes. Repeatable strips may have different source widths only when metadata records display size, anchor, scale, repeat axis, and loop policy.

The final side-scroller should feel deeper than a single flat image: distant layers move slowly, near layers move faster, and gameplay objects stay on their own runtime layer.

## Side-Scroll Stage Segment Contract

A side-scroll stage should be long because its runtime object layout is long, not because image generation produced one oversized panoramic picture.

Default structure:

- `stage_canvas`: one camera-sized canvas shared by primary parallax plates, stage references, and previews.
- `stage_segment_count`: 2 by default for a normal playable side-scroller.
- `stage_length`: camera width multiplied by segment count, or the existing engine world width when present.
- `segments`: predictable ids such as `segment-01` and `segment-02`, each with its own placement ranges and optional parallax plates.
- `shared_platform_library`: platform strips, platform tiles, or terrain chunks used across all segments.
- `shared_object_library`: hazards, gates, doors, checkpoints, pickups, exits, and compact decorative props used across all segments.
- `objects`: platform placements, terrain placements, hazards, interactives, foreground occluders, and exits grouped by segment or world coordinate range.
- `scene_hooks`: player spawn, actor spawn markers, camera bounds, locks, arena triggers, checkpoint ids, and exit links.

This mirrors a common platformer implementation: the runtime draws a background, then repeats/skins platform rectangles or tile spans from a small library. It is more controllable than asking image generation for a finished foreground layout and trying to infer collision from the pixels.

## Side-View Background Separation

For playable side-view scrolling/action stages, the general layer separation contract becomes stricter: the background is scenery-only. It should be a far/mid depth plate that separate runtime objects can stack over cleanly.

Allowed in the background:

- sky, clouds, mountains, distant city/castle silhouettes, far walls, smoke, weather, atmospheric depth, and non-colliding distant landmarks
- optional separate parallax midground/foreground layers when they are not gameplay geometry

Not allowed in the runtime background:

- walkable floors, platform tops, terrain chunks, ladders, spike traps, pickups, crates, doors, gates, checkpoints, near fences, near walls, foreground barricades, enemies, player characters, UI, labels, or any object that should be edited, collided with, reused, or rendered independently

If a generated side-view background contains obvious foreground gameplay geometry, reject it as a runtime background and regenerate a cleaner scenery-only background. Do not set flattened `stage-reference` or `stage-preview` images as the runtime background.

## Post-Reference Object Production

After a dressed reference or stage reference exists, continue into final runtime production:

1. Make both the original base/background and the dressed/stage reference mockup visible in conversation context. For local files, call `view_image` on both images immediately before object-list extraction or object/prop generation.
2. Create a concrete object list from the visible reference mockup while cross-checking the original base/background: object id, type, approximate position, approximate size, render layer, collision role, and asset strategy.
3. For each visible runtime object, generate a separate transparent asset, extract it from a generated pack, or represent it as a tile/object layer when the engine/editor pipeline is tile-based.
4. Every object/prop image prompt must explicitly state that the visible original base/background and visible reference mockup above are the visual context. The generated asset must match the original map style and correspond to an object visible in the reference mockup.
5. Generate or define the final props, platforms, terrain chunks, hazards, pickups, doors, gates, checkpoints, exits, foreground occluders, and other visible scene objects. Do not rely on the reference image as the runtime art for these objects.
6. Write placement metadata, object layers, collision data, scene hooks, camera bounds, exits, and zones.
7. Compose a QA preview from the original base/background plus the final runtime objects.

For playable maps, layered maps with props, side-view stages, engine scenes, and requests for separate/editable props, stopping after the reference mockup is a failed/incomplete run.

For prop packs or object packs generated after a reference mockup, derive the object list and prompt from the visible reference mockup and original base/background. Do not generate generic props from memory or filenames.

## Visual Model

### `baked_raster`

Use when:

- the scene is static, decorative, fixed-screen, or visual-first
- the game needs a battle background, title scene, menu backdrop, cutscene, or quick prototype
- collision is absent or can be represented by a few invisible shapes
- the user explicitly asks for a single flat image or background

Deliver one image generated or edited through image generation, plus optional collision/zones metadata.

Do not use this as the final runtime map for platformers, RPG exploration, tower defense, or any scene where props, platforms, hazards, exits, or interactables must be edited, collided with, reused, or rendered independently.

### `layered_raster`

Use when:

- a hand-painted or generated base map is best, but tall objects need collision, occlusion, interaction, reuse, or later editing
- the scene is an RPG town, shrine, dungeon room, field, interior, or monster-taming exploration map
- y-sorted actors should walk in front of and behind props

Deliver an image-generated ground-only base image, separate image-generated props, placement metadata, collision/zones metadata, and a flattened preview.

The base image must be foundation-only: terrain, roads, water, floor markings, and boundaries are allowed; tall props, buildings, trees, signs, doors, chests, pickups, actors, hazards, and occluders must be separate assets or object/tile layers.

### `tilemap`

Use when:

- the engine/editor already uses Tiled, LDtk, Phaser tilemaps, Godot TileMap, Unity Tilemap, or similar tooling
- the user asks for tiles, tilesets, tile collision, autotiling, or editable grid-perfect maps
- procedural generation, large maps, or editor workflows matter

Deliver image-generated or user-supplied tileset images, engine-native map data, tile layers, object layers, and tile/object collision. Do not script-draw the tileset as final art unless the user explicitly asked for procedural placeholders.

Do not flatten tile layers, object layers, collision-relevant props, pickups, doors, hazards, or interactables into one runtime background image.

### `layered_tilemap`

Use when:

- the game needs multiple tile layers such as ground, decor, walls, overhead, and foreground
- actors need to pass under selected tile layers
- collision and triggers are tile/object-layer driven

Deliver image-generated or user-supplied tileset art, layered tile data, and a render-order contract.

### `parallax_layers`

Use when:

- the map is a side-scroller, platformer, runner, shooter, side-view brawler, scrolling action stage, or scrolling backdrop
- background depth matters more than top-down collision

Deliver image-generated background, midground, foreground, and scroll-speed metadata.

For a playable side-view scrolling/action stage, parallax layers are only the scenery. Generate an in-world stage reference mockup from the visible background using the visual reference handoff, then continue through post-reference object production. The playable stage still needs separate runtime objects for platforms or walkable lanes, terrain chunks, hazards, pickups, doors, checkpoints, gates, exits, scene hooks, camera bounds, and explicit collision.

The runtime background for this preset must be scenery-only. Put collidable foreground geometry and reusable gameplay objects into `platform_objects`, tile/object layers, or engine-native nodes instead of baking them into the background image.

For `side_scroll_mode`, use named parallax layers (`sky`, `far_bg`, `mid_bg`, `near_bg`, optional `foreground_overlay`) plus explicit scroll factors, shared `stage_canvas`, and loop/repeat policy. Do not treat a single scenery background as a complete side-scroller background stack unless the user explicitly asks for a flat/non-parallax background.

## Runtime Object Model

- `none`: the map is just a background or tile layers.
- `separate_props`: props are independent sprites but do not require y-sort.
- `platform_objects`: platforms, walkable lanes, terrain chunks, walls, hazards, foreground blockers, and other collidable stage geometry are independent runtime objects with placement and collision data.
- `y_sorted_props`: props and actors sort by base `y`; use for top-down RPG scenes.
- `interactive_scene_objects`: doors, pickups, switches, checkpoints, gates, destructibles, signs, exits, and other non-character scene objects with interaction or state.
- `foreground_occluders`: selected overlays always draw over actors.
- `scene_hooks`: metadata-only markers such as player spawn, actor spawn markers, encounter zones, patrol hints, arena triggers, camera bounds, exit links, and checkpoint ids. These do not require generated actor art.

Use the simplest model that can express collision and occlusion correctly.

## Collision Model

- `none`: visual-only maps and simple backgrounds.
- `coarse_shapes`: a few rectangles/ellipses for fixed arenas or decorative maps.
- `precise_shapes`: explicit blockers and walk bounds for layered RPG maps.
- `tile_collision`: collision stored per tile or tile layer.
- `polygon_walkmesh`: irregular walkable regions or constrained path maps.
- `trigger_zones`: encounter/rest/exit/dialogue areas; often combined with another collision model.

Do not infer collision from prop PNG bounds automatically. Use explicit blockers for prop bases and explicit walkable zones for navigation.

## Engine Target

- `raw_canvas`: use PNG assets, JSON metadata, and project-specific render code.
- `Phaser`: prefer atlas/tilemap JSON when the project already uses Phaser loaders; visual assets still come from image generation or existing art.
- `Tiled_JSON`: produce Tiled-compatible tilesets, layers, objects, and custom properties around image-generated or existing tileset art.
- `LDtk`: produce or adapt to LDtk entity/layer concepts if the project uses LDtk, while preserving image-generated or existing art as the visual source.
- `Godot_TileMap`: produce tile layers and scene metadata matching Godot's structure after generating or selecting the visual tileset art.
- `Unity_Tilemap`: produce tileset/sprite assets and placement data for Unity workflows after generating or selecting the visual art.
- project-native: preserve existing schema when a game already has one.

## Presets

### Fixed Battle Background

- `visual_model`: `baked_raster`
- `runtime_object_model`: `none`
- `collision_model`: `none` or `coarse_shapes`
- Typical deliverables: one PNG, optional zones.

### RPG Exploration Scene

- `visual_model`: `layered_raster`
- `runtime_object_model`: `y_sorted_props`
- `collision_model`: `precise_shapes + trigger_zones`
- Typical deliverables: base map, prop images, placement JSON, collision JSON, preview.

### Monster Grassland

- `visual_model`: `layered_raster`
- `runtime_object_model`: `y_sorted_props + interactive_scene_objects + scene_hooks`
- `collision_model`: `precise_shapes + trigger_zones`
- Good prop-pack candidates: rocks, shrubs, flowers, signs, small logs.

### Tile-Based Dungeon

- `visual_model`: `layered_tilemap`
- `runtime_object_model`: `interactive_scene_objects + scene_hooks`
- `collision_model`: `tile_collision + trigger_zones`
- Use only when the engine/editor supports tilemaps.

### Side-View Scrolling Stage

- `visual_model`: `parallax_layers`
- `runtime_object_model`: `platform_objects + interactive_scene_objects + scene_hooks + foreground_occluders`
- `collision_model`: `precise_shapes` or engine-native platform/object collision
- Typical deliverables: shared `stage_canvas`, `stage_segment_count`, `stage_length`, separate parallax layers (`sky`, `far_bg`, `mid_bg`, `near_bg`, optional `foreground_overlay`) matching that canvas, scroll factors, in-world stage reference mockup, shared platform/object art library, separate platform/terrain sprites, foreground pieces, hazards, pickups, doors, checkpoints, gates, exits, segment placement metadata, scene-hook metadata, camera bounds, collision metadata, and a stage preview.

### Side-View Action / Platformer Stage

- `visual_model`: `parallax_layers` or `layered_tilemap` if the engine/editor already uses tiles
- `runtime_object_model`: `platform_objects + interactive_scene_objects + scene_hooks + foreground_occluders`
- `collision_model`: `precise_shapes` or engine-native platform/object collision
- Applies to Megaman-like, Castlevania-like, Contra-like, side-view action, runner, shooter, and brawler stages across pixel art, clean HD, and project-native styles.
- Required deliverables: shared `stage_canvas`, background/parallax art that matches that canvas, in-world stage reference mockup, separate platform or terrain-chunk sprites, hazard sprites, scene object placement data, scene-hook metadata, pickups/doors/checkpoints/gates when present, collision data, camera bounds, and a QA preview.
- Default stage length: 2 camera-width segments unless the user asks for a one-screen room or boss arena.
- Platform strategy: explicit platform metadata skinned by shared platform strips, platform tiles, or terrain chunks. Do not use ordinary square prop packs for floors, bridges, ledges, walls, ladders, or slopes.
- The stage reference should plan no more than 9 distinct visible object candidates unless the user requests a larger pass. Use repeats in metadata rather than asking the image model to invent many unrelated props at once.
- Anti-pattern: one generated full-stage PNG plus collision rectangles. That is a background with hitboxes, not a playable stage.

## Escalation Heuristic

Start with the smallest playable bundle that works:

1. non-playable background: `baked_scene_mode`
2. beautiful top-down or tower-defense demo: `scene_mode`
3. editable top-down/platform/grid map: `tile_mode`
4. playable side-view scrolling/action stage: `side_scroll_mode`
5. tactical/factory/board rules-first scene: `grid_mode`
6. procedural/modular room assembly: `room_chunk_mode`
