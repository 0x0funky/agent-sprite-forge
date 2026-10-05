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

Use project-native schemas first. Raw Canvas, Three.js, Phaser, Tiled, LDtk, Godot and Unity are options, not dependencies. Keep visual models (`baked_raster`, `layered_raster`, `tilemap`, `layered_tilemap`, `parallax_layers`) distinct from object and collision models.

## Layout and portals

Plan entry, visible destination/landmark, meaningful route and optional reward pockets when gameplay needs them. Give corridors controller clearance. A village can have no encounter zone and more dialogue/door hooks. Source detail density follows display size/zoom; avoid stretching one picture across the entire world.

Prefer an existing portal schema; otherwise record these concepts:

```json
{
  "id": "forest-east",
  "trigger": {"shape": "rect", "x": 1880, "y": 410, "w": 40, "h": 120},
  "destinationMap": "ruins", "destinationPortal": "ruins-west",
  "arrival": {"x": 100, "y": 468, "facing": "east"},
  "activation": "crossing", "rearm": "leave-trigger"
}
```

Values are illustrative world units. Place arrivals outside the return trigger and inflated blockers; rearm only after leaving so held movement cannot bounce between maps. Preserve facing/movement and settle the camera according to transition design. Prefetch static floor and critical actor media near the exit; optional loops can arrive later over aligned posters. Nearby cues may fade in softly instead of bouncing permanently.

Check both directions, unequal corridor widths/elevations, blocked arrivals and loading failure. Old scene media must not appear over new geometry. A chunk boundary is not complete until both movement and rendering transitions work.

## Opaque terrain variants

```bash
python scripts/extract_terrain_tiles.py --input terrain.png --output-dir terrain-tiles \
  --rows 2 --cols 3 --terrain-row grass=0 --terrain-row stone=1 \
  --tile-size 128 --resampler nearest --prompt terrain.prompt.txt --strict-qc
```

Rows must each be assigned exactly once. Square cells are required unless `--cell-shape crop-square` explicitly authorizes centered cropping. Use nearest for pixel art, Lanczos for smooth materials. The helper rejects transparency and invalid numeric values; strict QC runs before image writes. It records portable paths, dimensions, material/runtime hints and contrast/variant differences, not native autotile rules.

`--edge-policy seamless` records intent only; `seamless_verified` remains false. Inspect repeated tiles/neighbors before acceptance. Contrast/variance are diagnostics rather than artistic approval. Runtime world size, height and `--engine-target` are explicit project values; default engine target is `project-native`.
