# Engine maps, room chunks and side-scroll layouts

Four tools turn map data into engine files or check it before art is made. Run them from the project root; outputs go to a new folder inside the project. In Claude Code `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.

```text
python "<skill-dir>/scripts/export_godot.py" --bundle maps/town/map-bundle.json --output-dir build/town-godot --name town
python "<skill-dir>/scripts/export_ldtk.py" --bundle maps/town/map-bundle.json --output-dir build/town-ldtk --name town
python "<skill-dir>/scripts/validate_chunks.py" --chunks maps/dungeon/room-chunks.json --output-dir maps/dungeon/qa/chunks-v1
python "<skill-dir>/scripts/validate_layout.py" --layout maps/levee/layout.json --output-dir maps/levee/qa/layout-v1
```

Every tool prints one ASCII JSON line with its status and paths, prints `error: ...` on stderr and exits 1 on failure (2 on a usage error), and refuses an existing `--output-dir`. The exporters publish nothing when the read-back QA fails; `--strict-qc` also refuses their warnings (things outside the world, rounded LDtk positions). The validators print only the summary without `--output-dir`; with it they publish the report and debug image even for a failing level (exit 1), unless `--strict-qc` is given. JSON inputs are read as UTF-8 with an optional BOM; NaN, Infinity and duplicate keys are refused.

## What is verified

| Output | Verified here | Not verified |
|---|---|---|
| Godot 4.3+ `.tres` TileSet and `.tscn` scene | Re-read by the exporter's Godot text parser; tiles (decoded `tile_map_data`), peering bits, physics polygons, prop anchors (with `flip_h`), collision shapes (solids, rects, solid footprints) and marker positions match the bundle | Opening the files in the Godot editor; painting with the terrain brush; runtime collision |
| LDtk 1.5.3 project | Required fields and types against a snapshot of the LDtk JSON schema; uids, iids, identifiers; tiles and entity positions read back | Opening the project in LDtk; auto-layer rules (none are written) |
| Room chunk report | Socket spans, stitching, chunk-graph and walk-grid reachability on the declared data | Door art, transitions, camera and loading |
| Layout report | Grammar rules and a parabolic jump model on the declared data | The real controller, coyote time, double jumps, ceilings, actor width |

Do not claim editor compatibility until a person has imported the files once in that editor and version.

## Bundle data the exporters read

The input is `generate2dmap.map_bundle.v2` (`references/schemas/map.schema.json#/$defs/map_bundle_v2`). Paths are relative to the bundle file; recorded `sha256` values must match.

- **Tiles layers**: `data` is rows (top first) of tileset tile indices, `-1` or `null` for empty, as inline JSON, a `.json` file, or a `.csv` file. The grid is `ceil(world / tile_size)` cells. A tile index `i` sits at atlas column `i % columns`, row `i // columns` of the tileset image.
- **Tilesets**: `generate2dmap.tileset.v1` manifests. `wang` is `[top_left, top_right, bottom_left, bottom_right]` material indices; `blob_mask` sets bits N, NE, E, SE, S, SW, W, NW. Tile `collision` shapes are in tile pixels and are the tile collision (integration decision D5); a tile without shapes whose `properties.walkable` is false blocks its whole cell.
- **Prop images**, first match wins, the one order every map tool uses (D6): `objects[].image` (optional `image_sha256`); the bundle's `props[prop]` (an inline item's `image`, relative to the bundle, or the accepted item of the prop pack named by `pack` and `label`); a prop pack whose accepted `label` equals `objects[].prop` (the bundle's optional `prop_packs: [{manifest, sha256}]`, then `--prop-pack FILE`, repeatable); then `objects[].occluder.source`. `anchor_px` must lie inside the image. `flip_x` mirrors the art around the anchor x.
- **Collision** is read through `scripts/forge_nav.py`, the one blocking set every map tool shares (D2): `collision.solids` and `rects`, the footprints of objects whose `solid` is not false (from the object, else its `props` item; scaled once by `scale`, not for `basis: "world_px"`; mirrored by `flip_x`), the per-tile collision of placed tiles and the blocking `material_map` classes. A bundle forge_nav cannot read (for example a material map pixel that matches no material) is refused.
- Layers are listed bottom first. The first `objects` layer holds every object.

## Godot 4.3+ mapping

| Bundle | Godot |
|---|---|
| tileset image | `TileSetAtlasSource` (`texture_region_size` = tile size), tile `x:y/0` per listed tile |
| Wang tileset (`wang` on every tile) | terrain set, mode Match Corners (1); terrains = materials; bits `top_left_corner`, `top_right_corner`, `bottom_left_corner`, `bottom_right_corner`; centre `terrain` = majority corner (ties: lower material index) |
| blob-47 tileset (`blob_mask` on every tile) | terrain set, mode Match Corners and Sides (0); centre and connected neighbours = the inside material (`--blob-inside`, else the manifest's `blob_inside`, else the last material); other neighbours = the other material, or unset with one material |
| tile `collision` | `physics_layer_0/polygon_N/points`, centred on the tile (rect 8x8 at 0,0 in a 16 px tile is -8,-8 .. 0,0); ellipses become 32-gons; a `walkable: false` tile without shapes gets its whole cell; shapes without area are dropped |
| tile `properties.walkable` | custom data layer 0 `walkable` (bool) |
| tiles layer | `TileMapLayer`, `tile_map_data` format 0 (x, y, source, atlas x, atlas y, alternative) |
| image layer | `Sprite2D`, `centered = false` at 0,0 |
| objects | y-sorted `Node2D` of `Sprite2D`: `position = (x, sortY)`, `offset` puts `anchor_px` on (x, y), `scale` from the object; with `flip_x`, `flip_h = true` and `offset.x = anchor_x - image width` (Godot mirrors the texture inside its own rect); metadata `anchor_world`, `anchor_px`, `prop`, `sort_y`, `footprint`, `solid`, `occlusion`, `flip_x` |
| collision solids and rects | `StaticBody2D` named `collision` with `CollisionShape2D` (rect, circle) or `CollisionPolygon2D`; walk regions as metadata `walk_regions` |
| solid object footprints | `footprint_<id>` children of the same `StaticBody2D`: the shape forge_nav blocks, already scaled once with offset, rotation and `flip_x` applied (rect and circle as `CollisionShape2D`, other ellipses as 32-gons, rotated rects as `CollisionPolygon2D`); metadata `object` |
| blocking `material_map` classes (`solid`, `one_way`, `liquid` and `hazard` unless walkable) | not exported: no Godot collision is made from the material map, so those pixels do not block in Godot; the report lists the classes in `notExported` (D2) |
| spawns, anchors | `Marker2D` (metadata `facing`, `slots`, `approach`) |
| portals | `Area2D` + `RectangleShape2D` / `CircleShape2D`; metadata `to`, `activation`, `travel_direction`, `radius`, `entrance_by_from`, `latch`, `requires_movement` |
| interactions | `Area2D` + `CircleShape2D` (radius = `reach`), or `Marker2D` without reach |

Paths inside the files are relative, so copy the whole output folder into the Godot project. `--texture-filter nearest` sets nearest filtering on the root for pixel art. Requires Godot 4.3 or later (`TileMapLayer`).

## LDtk 1.5.3 mapping

| Bundle | LDtk |
|---|---|
| tileset | tileset definition (`tileGridSize` = tile size; square tiles and an atlas that is a multiple of the tile are required); per-tile wang, blob, collision and walkable JSON in `customData` |
| tiles layer | Tiles layer with `gridTiles` (exact placement) |
| bottom image layer | level background (`bgPos: Unscaled`); other image layers are reported, not exported |
| objects | Entities layer; one entity definition per prop image and anchor, rendered from `assets/props-atlas.png`; pivot = `anchor_px` / image size, so `px` = (x, y) |
| spawns, portals, interactions, anchors | `Markers` Entities layer: `Spawn` (pivot bottom centre), `Portal` (rect, or a circle's bounding box with `Shape = circle`), `Interaction` (`Reach`), `Anchor` (`Slots`, `Approach` as JSON) |
| objects with `flip_x` | their own entity definition (`Prop_<prop>_flip_x`) drawn from a mirrored copy in the atlas, with the mirrored pivot (`(width - anchor_x) / width`), so `px` is still (x, y); field `FlipX` |
| collision, walk regions, solid footprints, material map classes, nav grid | not exported (listed in `notExported`); per-tile collision rides in tileset `customData` only |

LDtk positions are whole pixels: fractional bundle positions are rounded half up and reported as a warning (`--strict-qc` refuses them). Terrain is placed tiles, not IntGrid with auto-layer rules; make rules in LDtk if the level will be repainted there.

## Room chunks (`room_chunk_mode`)

`generate2dmap.room_chunk.v1`: chunks with `size` [w, h] px and sockets on N, E, S, W: `{offset, width, material}`, where `offset` runs along the edge from its top-left end. Optional walkability: `grid` rows of `.` (walkable) and `#` (blocked) with `cell` px per character (on the chunk or the file).

```json
{"schema": "generate2dmap.room_chunk.v1", "cell": 16,
 "chunks": [
  {"id": "hall", "size": [96, 64], "sockets": {"E": [{"offset": 16, "width": 32, "material": "floor"}]},
   "grid": ["######", "#.....", "#.....", "######"]},
  {"id": "corridor", "size": [96, 64], "grid": ["######", "......", "......", "######"],
   "sockets": {"W": [{"offset": 16, "width": 32, "material": "floor"}], "E": [{"offset": 16, "width": 32, "material": "floor"}]}},
  {"id": "vault", "size": [96, 64], "sockets": {"W": [{"offset": 16, "width": 32, "material": "floor"}]},
   "grid": ["######", ".....#", ".....#", "######"]}],
 "graph": [["hall", "corridor", "corridor", "vault"]]}
```

- `graph` as rows lays chunks on a grid (equal widths per column, equal heights per row, `null` for an empty cell). A chunk id used twice becomes the instances `corridor@0,1` and `corridor@0,2`.
- `graph` as edges, `{"from": "a", "to": "b", "side": "E", "offset": 0}`, puts b on a's east edge shifted `offset` px; each declared edge must have a compatible door. An empty graph checks a kit: every socket needs a partner of the same width on an opposite edge somewhere.
- Where two chunks touch, sockets must meet a socket with the same span and material; a mismatch fails. A socket touching nothing is a warning (it may be a world exit).
- Reachability: doors connect the chunk graph from `--start` (or the file's `start`, or the first chunk). With grids on every placed chunk, the grids are laid on one world grid and searched with `forge_nav.grid_bfs`, the grid search every map tool shares: 4-neighbour moves stay inside a chunk and cross into the next one only through the cells of a paired door. Every chunk and door must be reachable on foot; doors opening into walls fail; open edge cells without a socket warn.

`chunk-debug.png`: chunks in colour, blocked cells dark, reached cells green, unreachable cells orange; doors green (paired), red (mismatch) or yellow (dangling).

## Side-scroll layouts (`side_scroll_mode`)

`generate2dmap.layout.v1`: `segments` `[x0, x1, kind]` in world pixels (y down), `physics` `{jumpHeight, jumpDistance, maxSlopeDeg, stepUp, colliderSubstep}`, plus the ground surface: `surface` `[[x, y], ...]` (repeat an x for a vertical step) or a flat `groundY`.

```json
{"schema": "generate2dmap.layout.v1",
 "segments": [[0, 262, "ground"], [262, 346, "gap"], [346, 640, "ground"], [640, 830, "slope"]],
 "surface": [[0, 216], [640, 216], [700, 240], [770, 240], [830, 216]],
 "props": [{"id": "lantern", "x": 118, "y": 216, "w": 10},
           {"id": "plank", "kind": "deck", "x0": 280, "x1": 330, "y": 190, "thickness": 4}],
 "spawns": [{"id": "start", "x": 40, "y": 216}], "exits": [{"id": "gate", "x": 820}],
 "camera": {"viewHeight": 270, "travel": 80},
 "physics": {"jumpHeight": 72, "jumpDistance": 120, "maxSlopeDeg": 35, "stepUp": 8, "colliderSubstep": 3}}
```

- Kinds: ground (`floor`, `solid`, `crest`, `c`, `slope`, `s`), gap (`pit`, `void`, `g`), hazard (`water`, `lava`, `spikes`); map others with `"kinds": {"bridge": "ground"}`.
- Props: (x, y) is the middle of the base, `w` its width; `floating: true` for hanging or flying props. Decks (`kind` deck, platform, plank, bridge) are standable `{x0, x1, y, thickness}`, one-way.
- Segment bounds are whole world pixels (a column is one pixel; fractional bounds are refused). A gap is measured between the centres of its edge columns, so the widest gap a jump of `jumpDistance` crosses is `jumpDistance - 1` px (for `jumpDistance` 96, a 95 px gap crosses and a 96 px gap does not).
- Checks: segments contiguous; slopes steeper than `maxSlopeDeg` rise at most `stepUp` (draw taller ones as vertical ledges); ledges taller than `jumpHeight` warn; decks at least `minDeckThickness` (default `colliderSubstep + 1`, else 4: a 3-row deck lets a standing body sink through); every footprint column of a prop rests on ground or a deck within `groundTolerance` (1 px); spawns on a surface; the far side of every gap reachable (jump arc: apex `jumpHeight`, range `jumpDistance`, landing higher shortens it to `jumpDistance/2 * (1 + sqrt(1 - rise/jumpHeight))`); every spawn, exit and (without exits) the level end reachable from the first spawn; each arena (`arenas` `[{id, x0, x1, travel}]`, default the whole level) at least the view width plus camera travel at 16:9, 19.5:9 and each listed viewport.

`layout-debug.png` (side view): ground brown, steep faces magenta, gaps red at the bottom, hazards blue, decks wood (red when too thin), reachable surfaces green and unreachable ones red, the jump arc used over each gap cyan (red when it falls short), props grey (red floating, orange sunk), spawns green, exits blue.

## Report paths

`godot-export.json` and `ldtk-export.json` (`generate2dmap.engine_export.v1`) give every path relative to the report's own folder, except `assets[].source`, which is the source image relative to the bundle's folder (or only its name when the bundle sits on another drive). `notExported` lists every bundle field and every blocking material class the engine files do not carry.
