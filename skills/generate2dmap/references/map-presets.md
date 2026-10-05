# Map presets and engine targets

Starting values for common game shapes, written as map-bundle data (see [layered-map-contract.md](layered-map-contract.md) and [schemas/map.schema.json](schemas/map.schema.json)). They are data only: no tool reads a preset name, and every value is a starting point to adjust to the project's actual camera, actor size and engine. Measure the real actor before fixing `actorRadius`.

Rules of thumb used below:

- `actorRadius` is about 0.2 of the actor's displayed height in world pixels (a 64 px tall actor gets about 12); on a tile map about a third of a tile.
- `ySquash` is 1.0 for top-down maps and about 0.58 for HD-2D plates seen at a low camera angle.
- The navigation cell follows from the radius: `max(1, round(actorRadius / 2))` px. Corridors must be wider than `2 * actorRadius + cell` px.
- Exits at map borders use `activation: intent` with a `radius` of one to two actor radii; doors inside the map can use `crossing`.

## Genre presets

| Preset | Map mode | World and tiles | Collision | Portals and points | Tools after art |
|---|---|---|---|---|---|
| Tile RPG town or field | `tile_mode` | 16 or 32 px tiles; wang-corner terrain plus blob-47 paths | tile collision plus prop footprints; no walk regions | intent exits at the borders; spawns per entrance; anchors for shops and wells | map_bundle, map_nav, export_tiled |
| Painted exploration scene | `scene_mode` | one painted plate as an image layer; optional tile overlay | walk-region polygons traced on the plate; trunk and base footprints | intent exits along paths; interactions with `reach`; anchors with `approach` | map_bundle, map_nav, compose preview |
| HD-2D plate | `scene_mode` | painted plate, actors at plate scale | walk regions on the floor band; `ySquash` 0.58 | slots for party members; stage data in `stage` | map_bundle, map_nav, the HD-2D stage tools |
| Dungeon rooms | `room_chunk_mode` | fixed-size rooms (for example 15 x 11 tiles) | tile collision; door gaps match the sockets | crossing doors between rooms; one bundle per room | map_nav per room, the chunk validator across rooms |
| Tactics board | `grid_mode` | one material pixel per tile (`material_map` scale = tile size) | material classes per tile (`liquid`, `hazard`, `solid`); small radius so every free tile is a valid node | tall dressing uses `rear_shift_and_fade` | map_bundle, map_nav, export_tiled |
| Brawler belt | `side_scroll_mode` | a long plate; the belt is one walk-region polygon | belt polygon only; `ySquash` about 0.6 | intent exits at the belt ends | map_bundle, map_nav |
| Platformer | `side_scroll_mode` | platform kit tiles | `one_way` platforms in the material map | exits and checkpoints | map_bundle for data; jump and gap checks belong to the side-scroll layout validator, not to map_nav |
| Story or point-and-click scene | `baked_scene_mode` | one complete picture | a walk region for the floor | interactions with `reach`, anchors whose `approach` is where the actor walks before acting | map_bundle, map_nav |

`map_nav.py` checks top-down movement. Platformer reachability depends on jump arcs, which it does not model; for those maps it still checks the data, the `one_way` direction and the exits.

### Snippets

Tile RPG field (16 px tiles, actor about 16 px tall on screen):

```json
{"tile_size": [16, 16], "world": {"width": 640, "height": 480, "unit": "px"},
 "collision": {"actorRadius": 5, "ySquash": 1.0},
 "portals": [{"id": "exit-east", "rect": [632, 192, 8, 48], "to": "route-1:arrive-west", "activation": "intent",
              "travelDirection": [1, 0], "radius": 8, "entranceByFrom": {"route-1": "arrive-east"}}]}
```

HD-2D plate (1920 x 1080 plate, actor about 140 px tall):

```json
{"world": {"width": 1920, "height": 1080, "unit": "px"},
 "layers": [{"name": "plate", "kind": "image", "image": "plate.png"}, {"name": "props", "kind": "objects"}],
 "collision": {"actorRadius": 28, "ySquash": 0.58,
               "walkRegions": [{"polygon": [[120, 760], [1800, 760], [1880, 1040], [40, 1040]]}]}}
```

Tactics board (12 x 10 tiles of 32 px; materials one pixel per tile):

```json
{"tile_size": 32, "world": {"width": 384, "height": 320, "unit": "px"},
 "collision": {"actorRadius": 6},
 "material_map": {"image": "board-materials.png", "materials": {
   "floor": {"class": "decor", "color": "#000000"}, "pit": {"class": "hazard", "color": "#ff0000"},
   "shallow": {"class": "liquid", "color": "#00ffff", "walkable": true}, "pillar": {"class": "solid", "color": "#808080"}}}}
```

## Engine targets

| Target | Hand over | Produced by | What is verified |
|---|---|---|---|
| Tiled 1.10 or later | `map.tmj`, one `.tsx` per tileset, `props.tsx`, `images/` | `export_tiled.py export` | the written files re-render to the bundle's reference render with 0 px difference (built-in reader; pytiled-parser when installed); Tiled GUI not verified, including terrain brushes on the exported wangsets |
| Phaser 3 | `map.embedded.tmj` (tilesets inlined) and `images/` | `export_tiled.py export --embedded-variant` | the same re-render check; Phaser itself is not run by the tests |
| Godot 4 | TileSet and TileMapLayer resources | the Godot exporter of the engine-exporters module, once it lands | parse level only; editor import not verified |
| LDtk | an `.ldtk` project | the LDtk exporter of the same module | required fields only; editor not verified |
| Custom engine (Canvas, Three.js, Pixi, others) | `map-bundle.json` and `nav-grid.json` | `map_bundle.py`, `map_nav.py` | collision and reachability follow plan Appendix C; the shared JavaScript collision query comes with the scene-preview module |
| Unity | a Tiled export through a third-party importer, or a reader of `map-bundle.json` | not provided here | not verified |

`nav-grid.json` (from `map_nav.py check`) lists one character per grid node: `#` for a blocked node, otherwise a hex digit of its open moves (east 1, south 2, west 4, north 8; south is +y). Node `(col, row)` sits at `((col + 0.5) * cell, (row + 0.5) * cell)`. A runtime can path-find on it directly; `blockedRects` lists the blocked nodes as disjoint rectangles in world pixels.

```bash
python "<skill-dir>/scripts/map_nav.py" check --bundle map/map-bundle.json --output-dir map/qa/nav
python "<skill-dir>/scripts/export_tiled.py" export --bundle map/map-bundle.json --output-dir map/tiled --embedded-variant
python "<skill-dir>/scripts/export_tiled.py" verify --map map/tiled/map.tmj --bundle map/map-bundle.json --reader pytiled
```

The `--reader pytiled` check needs `python -m pip install pytiled-parser`; without it the tool prints that command and exits.
