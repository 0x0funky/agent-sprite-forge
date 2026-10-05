# Map strategies and connected worlds

| Route | Good fit | Runtime data |
|---|---|---|
| Complete authored picture | Battle, story/hotspots, stable room with walkmesh | Actor staging or explicit walkmesh/hotspots |
| Layered scene | Handcrafted RPG region/town | Base, props, anchors, collision, depth and interactions |
| Tile/layer map | Large repeated terrain, grid movement, editor/autotile | Tileset, native layers, collision and hooks |
| Connected chunks | Streaming, distinct locations, modular worlds | Shared kit, bounds, reciprocal sockets and arrival rules |
| Parallax scene | Side-view depth and scrolling | Independent imagery, transforms, alpha and coverage |
| Rules grid | Tactical/factory/building | Cells, costs, occupancy/build flags and terrain effects |

Do not route every RPG to tiles. A wide authored forest floor with separate trees and explicit blockers can be playable. A fixed battle painting need not become dozens of props. A painted cliff, blocker, foreground canopy and portal are separate concerns even where their pixels overlap.

Use project-native schemas first. Raw Canvas, Three.js, Phaser, Tiled, LDtk, Godot and Unity are options, not dependencies. Keep visual models (`baked_raster`, `layered_raster`, `tilemap`, `layered_tilemap`, `parallax_layers`) distinct from object and collision models. When the project has no schema of its own, store the playable map as a map bundle ([layered-map-contract.md](layered-map-contract.md)) and pick a preset from [map-presets.md](map-presets.md).

## Data route for playable maps

1. Lay out the world: size, tile size if any, walk regions or blocked tiles, spawns, exits and landmarks.
2. Produce the art: a painted base, a tileset (autotile sets below) or both, and props with anchors and footprints ([prop-pack-contract.md](prop-pack-contract.md)).
3. Write the map bundle and hash it: `python "<skill-dir>/scripts/map_bundle.py" hash --bundle map/draft-bundle.json --output map/map-bundle.json`
4. Validate: `python "<skill-dir>/scripts/map_bundle.py" validate --bundle map/map-bundle.json`
5. Prove navigation: `python "<skill-dir>/scripts/map_nav.py" check --bundle map/map-bundle.json --output-dir map/qa/nav`
6. Export for the engine: `python "<skill-dir>/scripts/export_tiled.py" export --bundle map/map-bundle.json --output-dir map/tiled --embedded-variant`

## Composition rules

- Plan an entry, a visible destination or landmark, a meaningful route and optional reward pockets when gameplay needs them. Landmarks should be visible from the entrances.
- Give every corridor, door and bridge controller clearance: more than `2 * actorRadius + cell` px, where `cell = max(1, round(actorRadius / 2))`. Narrower openings may be passable in a runtime but cannot be proven on the navigation grid.
- Keep walls and gaps at least one cell thick. `map_nav.py` never lets the actor's centre cross a thinner wall or gap (the thin-gap rule) and lists every place where that rule closed a move; a runtime without the rule could slip through there.
- Unreachable walkable pockets are reported; keep them only when they are intentional (fenced gardens, scenery islands).
- Scatter props with spacing (Poisson-disk style), keep their footprints plus clearance off the routes, vary density with noise, and frame map edges with denser cover. Source detail density follows display size and zoom; avoid stretching one picture across the entire world.
- A village can have no encounter zone and more dialogue or door hooks.

## Autotile sets

Terrain lives on a vertex grid one larger than the tiles in each direction (`terrain.vertex_grid` lists material indices; `terrain.materials` names them). Each tile is chosen by the materials on its four corners, so neighbours agree on their shared edges by construction.

- **Wang corner (16 tiles per material pair).** tileset.v1 `kind: wang_corner`; each tile's `wang` is `[top_left, top_right, bottom_left, bottom_right]` material indices. `export_tiled.py` writes a Tiled corner wangset (colours are the materials, 1-based).
- **Blob-47 (one material over a background).** tileset.v1 `kind: blob47`; `blob_mask` sets bit i for neighbour i of N, NE, E, SE, S, SW, W, NW, and a diagonal counts only when both of its edges are set, which leaves 47 canonical masks. `export_tiled.py` writes it as a two-colour mixed wangset (outside, blob) so an isolated tile never has an all-zero id.
- **Three materials.** Two separate two-material sets cannot put a road directly against water; the layout then needs a buffer of the base material, which can also cut a route off. Use a three-material set (81 corner combinations) or keep the buffer on purpose and re-check reachability.
- **Variants.** Change only the tile interior; the outer ring stays identical so seams cannot appear. Claim `seamless_verified: true` only with a `seam_proof` (for example: a tiled map compared with a global render, `mismatches: 0`).
- **Collision per tile.** The tileset's per-tile `collision` is the one source of tile collision (D5): `map_nav.py`, the exported TSX tilesets and the runtime all read it. Shapes are in tile pixels; `properties.walkable: false` without shapes blocks the whole cell. Water quadrants per corner give exact shorelines.

## Ground-line sorting (sortY)

Top-down depth follows the ground contact. Each object's `sortY` is the world y of its anchor unless authored otherwise; ties break by x and then id, and actors sort by their feet. Author `sortY` only for deliberate exceptions, such as a bridge rail sorted with the far bank. Objects whose `occupant_policy` is `static_front` or `static_back` leave the sort: the runtime draws them after or before every actor (render order rule 3 in [layered-map-contract.md](layered-map-contract.md)). Tiled's `topdown` order uses the image bottom instead, so exported maps keep the objects pre-sorted and carry `sortY` for the runtime.

## Layout and portals

Portals are data in the map bundle. Prefer an existing project portal schema; otherwise use this one:

```json
{
  "id": "forest-east",
  "rect": [1880, 410, 40, 120],
  "to": "ruins:arrive-west",
  "activation": "intent",
  "travelDirection": [1, 0],
  "radius": 12,
  "entranceByFrom": {"ruins": "arrive-east"},
  "latch": true,
  "requiresMovement": true
}
```

- `rect` is `[x, y, w, h]`; a circle trigger is `circle: [cx, cy, r]` instead.
- `to` is `map-id:spawn` (or an anchor name) in the destination map. `entranceByFrom` maps a source map id to the spawn id (or `[x, y]`) where travellers coming back from that map arrive here, next to this portal but outside its trigger.
- `crossing` fires when the actor's centre enters the trigger. `intent` fires within `radius` px of the trigger while the movement intent points along `travelDirection` (normalised dot product above 0.25), so an exit at a clamped map border still works and walking inward never exits.
- `latch` keeps the trigger inactive after an arrival until the actor has left it; `requiresMovement` means standing still never fires. Values are world pixels.
- A deliberately one-way exit (a pit, a slide) sets `"reciprocal": false`.

Place arrivals outside the return trigger and its approach so held movement cannot bounce between maps. Preserve facing and movement and settle the camera according to the transition design. Prefetch static floor and critical actor media near the exit; optional loops can arrive later over aligned posters. Nearby cues may fade in softly instead of bouncing permanently.

Check both directions, unequal corridor widths and elevations, blocked arrivals and loading failure; old scene media must not appear over new geometry. `map_nav.py check` verifies triggers inside the world, every exit reachable and arrivals outside every trigger; pass the neighbouring maps to check the links themselves:

```bash
python "<skill-dir>/scripts/map_nav.py" check --bundle maps/forest/map-bundle.json --link maps/ruins/map-bundle.json --output-dir maps/forest/qa-nav
```

## Connected chunks and sockets

Room chunks share one kit and meet at sockets. Each chunk edge (N, E, S, W) lists sockets `{offset, width, material}` (room_chunk.v1 in [schemas/map.schema.json](schemas/map.schema.json)); two edges connect when the sockets face each other with the same width and material at mirrored offsets. Reachability must hold across the stitched graph, not only inside each chunk; the chunk validator (`validate_chunks.py`, see [engine-maps.md](engine-maps.md)) checks sockets and stitched reachability, and each chunk's own bundle goes through `map_nav.py`. A chunk boundary is not complete until both movement and rendering transitions work.

## Roads and bridges

- **Spline roads.** Draw roads as Catmull-Rom splines through control points on the vertex grid and stamp the road material within a radius that keeps the clearance above. Remove diagonal-only saddles and lone single-vertex specks afterwards, and keep path vertices one vertex away from water unless a three-material set exists.
- **Bridges.** The deck is a walk region (or a walkable material) laid over water; rails and posts are solids or exact `collision.rects` (D3); both bank ends must overlap the land's walk area so the actor can step on and off. Draw the deck below actors, sort rails by their ground line, and mark arch fronts `static_front` (drawn after actors by the runtime). Check a bridge with `map_nav.py`: both banks reachable, no thin gap at the joins.

## Kitbash

Build variety from a small, consistent kit: one camera, light direction and pixel density for every piece; anchors at the ground contact; footprints per piece. Vary instance `scale` modestly (footprints in `prop_px` follow, scaled once; `world_px` footprints do not), mirror with an object's `flip_x` only where the lighting allows (the art and its footprint mirror around the anchor, D6), recolour through the palette, and assemble modular fences or walls on the grid. Never rotate lit sprites; rotate footprints only for top-down props whose art is rotation-neutral. Record every instance as a bundle object so collision and sorting stay data; an object may carry its own `image` for a one-off variant ([layered-map-contract.md](layered-map-contract.md) has the art lookup order).

## Terrain tiles from an atlas

```bash
python "<skill-dir>/scripts/extract_terrain_tiles.py" --input terrain.png --output-dir terrain-tiles --rows 2 --cols 3 --terrain-row grass=0 --terrain-row stone=1 --tile-size 128 --resampler nearest --prompt terrain.prompt.txt --strict-qc
python "<skill-dir>/scripts/extract_terrain_tiles.py" --input shore.png --output-dir shore-tiles --rows 3 --cols 4 --terrain-row water=0 --terrain-row shore=1 --terrain-row grass=2 --wang shore=water/grass:0001,0011,0111,0110 --edge-policy seamless --strict-qc
```

Each atlas row is one terrain and each column a variant; every row is assigned exactly once. Square cells are required unless `--cell-shape crop-square` explicitly authorizes centered cropping; `--grid-rounding nearest` accepts an atlas that does not divide evenly. Use nearest for pixel art, Lanczos for smooth materials. Base fills are opaque; `--layer overlay` cuts RGBA overlays (tufts, decals, transitions), `--shape rect|iso-diamond|hex-pointy|hex-flat` other tile shapes, and `--wang NAME=A/B:MASKS` marks a Wang corner transition row (one TL TR BL BR mask per column). Strict QC runs before anything is published, into a new output folder.

`--edge-policy seamless` changes the processing: fills are resized wrap-aware, every fill must wrap and every legal Wang join must continue within `--max-seam-ratio` (default 1.25). The ratio is the one seam metric of every edge tool (`forge_core.edge_seam_report`, D9): the join step against the art's own steps near it. Only the verdicts `seam` and `duplicate_edge` fail; `continuous`, `flat` (a plain colour) and `too_small` do not. `seamless_verified` stays false because a statistic is not a proof: inspect repeated tiles and neighbours before acceptance. Contrast and variance are diagnostics rather than artistic approval. Runtime numbers (`--engine-target`, `--runtime-world-size`, `--surface-y`, `--roughness`, `--emission`) are written only when given; `--emit-runtime-defaults` restores the v1 defaults. The manifest (`terrain-bundle.json`, `generate2dmap.terrain_tile_bundle.v2`) records portable paths, hashes and the QA; the tileset with its autotile and collision data (above) is a separate step.
