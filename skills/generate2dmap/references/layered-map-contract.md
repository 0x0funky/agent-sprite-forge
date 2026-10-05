# Layered RPG map contract

A playable layered map is data, not a picture: a map bundle (`generate2dmap.map_bundle.v2`, see [schemas/map.schema.json](schemas/map.schema.json)) records the base art, tile layers, placed props, collision, portals, spawns, anchors and interactions. Three tools close the loop:

```bash
python "<skill-dir>/scripts/map_bundle.py" validate --bundle map/map-bundle.json --report map/qa/bundle-report.json
python "<skill-dir>/scripts/map_nav.py" check --bundle map/map-bundle.json --output-dir map/qa/nav
python "<skill-dir>/scripts/export_tiled.py" export --bundle map/map-bundle.json --output-dir map/tiled --embedded-variant
```

Commands run from the project root; outputs stay in the project. In Claude Code `<skill-dir>` is `${CLAUDE_SKILL_DIR}`. Each `--output-dir` must be new; a failed check publishes nothing.

## Geometry and depth

Stable foundations can include terrain, roads and distant scenery. Keep independently animated or interactable props and walk-behind roofs or canopies separate. Buildings can use a base and door plus a roof occluder; trees need a small trunk footprint, never a collider covering the canopy.

Record source size and support anchor, display scale and position, ground footprint, interaction reach and state, depth baseline and optional motion source separately. Actor feet collide; the full sprite rectangle does not. Bridges need a walkable deck and reachable banks, not just art; canopies must not hide the approaches.

## Generation

Use known camera, style and terrain geometry. A separate concept is useful only when composition or identity remains unresolved. Show local references and attach their pixels; name fixed landmarks, path widths, light and the requested layer's content.

```text
Create the static foundation for this [camera/style] exploration scene.
Use the attached accepted master for palette, horizon and light direction.
Preserve [road, river, bridge anchor, entrances] from the layout. Terrain and
stable scenery only; leave [areas] for separate trees, doors and canopies.
Keep the travel corridor readable at [gameplay scale]. No UI, labels or actors.
```

A fixed scene without independent behavior can remain a complete picture. Do not remove distant scenery merely because it depicts buildings or trees.

## Image, collision and occlusion are separate

| Concern | Data in the bundle | Measured in | Rule |
|---|---|---|---|
| Image | prop `image`, `anchor_px`, object `x`, `y`, `scale` | prop pixels | the prop is drawn so `anchor_px` lands on (`x`, `y`) |
| Collision | prop or object `footprint` `{shape, width, depth, offset, rotate}`, `solid` | prop pixels, scaled once | trunk, base or legs only; canopies, leaves and flags never block |
| Occlusion | `occlusion_class`, `occupant_policy`, `occluder {alphaThreshold, source}` | alpha of the occluder image | opaque pixels hide an actor; transparent corners never do |

Footprints are scaled exactly once: a footprint of `width` 8 on an object with `scale` 2 blocks a 16 px wide ellipse, centred at `x + scale * offset[0]`, `y + scale * offset[1]`. Never add the actor radius to a footprint or a solid; the collision rule already samples the actor's own footprint (see Collision). A rotated rect footprint becomes a polygon; `rotate` is in degrees, clockwise on screen (y down).

No alpha-fade occlusion: scenery stays fully opaque with alpha-tested cutouts. When an opaque occluder pixel actually covers an actor, draw a faint actor-shaped cue over it; do not fade or hide whole trees, and never trigger on the image rectangle. The only fade allowed is the board-only `rear_shift_and_fade` policy below.

## Occlusion class and occupant policy

These two enums are defined here once; prop packs (`occlusion_class`, `occupant_policy` in prop_pack.v2) and map bundles use the same values.

| `occlusion_class` | Meaning | Typical policy |
|---|---|---|
| `low` | below an actor's chest: rocks, stumps, crates, flower beds | `y_sort` or `static_back` |
| `tall` | can hide a standing actor: trees, lamps, columns, statues | `y_sort` plus the occluder cue |
| `foreground` | overhangs the walkway near the camera: eaves, arches, canopies | `static_front` |

| `occupant_policy` | Use it for | Runtime behaviour |
|---|---|---|
| `y_sort` (default) | free-roaming exploration | drawn with actors in ground-line order (render order below) |
| `rear_shift_and_fade` | fixed boards and grids only (tactics, isometric boards) where an occupant stands on a cell with tall walkable dressing | while the cell is occupied or selected, shift the dressing toward the cell's rear edge and fade only its tall part, with hysteresis; the ground texture never fades |
| `static_front` | foreground occluders | always drawn after actors |
| `static_back` | floor decals, rugs, shadows, low clutter | always drawn before actors |

Any other value is reported by `map_bundle.py validate` as a warning.

## Render order

1. Image and tile layers in bundle `layers` order (for example `ground`, then `decoration`).
2. Each objects layer: props and actors together, sorted by `sortY` (the ground line: the anchor's world y unless authored otherwise), then x, then id. Actors sort by their feet.
3. `static_front` and `foreground` props after every actor; `static_back` props before them.
4. Effects, UI and debug overlays.

Tiled's own `topdown` object order sorts by the image bottom, which differs from the ground line whenever the anchor is not on the image's bottom row. `export_tiled.py` therefore writes prop objects already sorted with `draworder: index` and a `sortY` property; engines should sort by `sortY` at run time. Platformers usually need stable render bands rather than sort changes while jumping (see [side-scroll-scenes.md](side-scroll-scenes.md)).

## Placement for a composed preview

`compose_layered_preview.py` draws props, objects, actors and foreground over a base for review. Placements name the image, the world position and the anchor; gameplay metadata (footprint, occlusion, policy) lives in the map bundle, not in the preview file.

```json
{
  "schema": "generate2dmap.placements.v2",
  "props": [{"id": "tree-01", "image": "../props/tree/prop.png", "x": 420, "y": 512,
             "anchor": "px", "anchorPx": [128, 370], "sortY": 512, "layer": "props"}],
  "actors": [{"id": "player-preview", "image": "../actors/player.png", "x": 460, "y": 530,
              "anchor": "px", "anchorPx": [32, 62], "layer": "actors"}],
  "foreground": []
}
```

```bash
python "<skill-dir>/scripts/compose_layered_preview.py" --base map-base.png --placements map-placements.json --output assembled.png --resampler nearest --report assembled-report.json
```

`anchorPx` is in source pixels. Foreground draws last. Inspect at gameplay scale with an actor in front of, behind and beside tall props. A PNG verifies composition, not gameplay, occlusion cues or video readiness.

## Collision JSON

The same object, as gameplay data in the map bundle:

```json
{
  "props": {"tree": {"image": "props/tree/prop.png", "anchor_px": [128, 370],
                     "footprint": {"shape": "ellipse", "width": 32, "depth": 18}, "solid": true,
                     "occlusion_class": "tall", "occupant_policy": "y_sort"}},
  "objects": [{"id": "tree-01", "prop": "tree", "x": 420, "y": 512, "scale": 0.5, "anchor_px": [128, 370],
               "sortY": 512, "occlusion": "tall",
               "occluder": {"alphaThreshold": 16, "source": "props/tree/prop.png"}}],
  "collision": {
    "actorRadius": 6, "ySquash": 1.0,
    "walkRegions": [{"polygon": [[0, 0], [960, 0], [960, 640], [0, 640]],
                     "holes": [[[300, 200], [340, 200], [340, 240], [300, 240]]]}],
    "solids": [{"shape": "rect", "x": 100, "y": 100, "w": 32, "h": 16},
               {"shape": "ellipse", "cx": 600, "cy": 300, "rx": 16, "ry": 9, "rotate": 20},
               {"shape": "polygon", "points": [[200, 300], [240, 300], [220, 330]]}],
    "rects": [[0, 620, 960, 20]]
  },
  "material_map": {"image": "materials.png", "materials": {
    "grass": {"class": "decor", "color": "#4e9640"}, "water": {"class": "liquid", "color": "#2c62ab"},
    "shallows": {"class": "liquid", "color": "#5a8fd0", "walkable": true}, "ledge": {"class": "one_way", "color": "#c79d62"}}}
}
```

Semantics (shared by `map_nav.py` and the runtime collision query):

- World pixels, y down. The actor footprint is an ellipse `rx = actorRadius`, `ry = actorRadius * ySquash`; `ySquash` is 1.0 for top-down tiles and about 0.58 for HD-2D plates.
- A position is valid when its centre and 8 points on that ellipse all lie inside a walk region (and outside its holes), or inside the world when there are no regions, and outside every blocker: `solids`, `rects` (merged blocked rectangles, treated as rect solids), solid object footprints, tile collision from the tilesets, and blocking material pixels.
- Material classes: `solid` blocks; `liquid` and `hazard` block unless `walkable: true`; `decor` never blocks; `one_way` blocks only moves that drop onto it from above. Every material-map pixel must match exactly one material colour (or palette index); the image is the world size or divides it into whole squares.
- Navigation uses a grid of `max(1, round(actorRadius / 2))` px. A move between neighbouring cells is open when every sample along it is valid and the actor's centre never crosses a blocker or leaves the walk area, so walls and gaps thinner than a cell are never jumped. A corridor must be wider than `2 * actorRadius + cell` px to be sure to show up on the grid.

## QA checklist

- `map_bundle.py validate`: contract, files and sha256, unique ids, tile indices, wang and blob data, portal targets, slots.
- `map_nav.py check`: every interaction, exit, anchor slot and approach point reachable from the spawns and arrivals; arrivals outside every trigger; triggers inside the world; links in both directions with `--link other-map.json`. Review `nav-debug.png`: red cells blocked, green reachable, yellow walkable but unreachable, orange thin gaps.
- No collider is the size of a canopy or a whole image; every solid prop has a footprint; footprints are scaled once.
- Tall props on walkways have an occlusion class and an occupant policy; the occluder cue works over opaque pixels only.
- Compose a preview with a representative actor at gameplay scale.
- Export with `export_tiled.py` when the target loads Tiled data; the export proves the re-rendered files match the bundle, not that the Tiled editor or an engine imports them (Tiled GUI not verified).
- Walk the routes in the actual runtime: in front of and behind props, around corners, over bridges and through portals both ways.

Strategy-level guidance (routes, portals, autotiles, chunks, roads and bridges) is in [map-strategies.md](map-strategies.md); genre presets and engine targets are in [map-presets.md](map-presets.md); prop extraction and anchors are in [prop-pack-contract.md](prop-pack-contract.md).
