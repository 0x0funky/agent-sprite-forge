# Layouts, parallax and ambient loops

Three codeart2d tools make map data and stylised backgrounds as code. None of them calls an image model.

| Tool | Makes | Proves |
|---|---|---|
| [layout_build.py](../scripts/layout_build.py) | A playable top-down map: `map-bundle.json` (map_bundle.v2), terrain, props, collision, exits, spawns, previews | Every exit, spawn and interaction is reachable for the actor footprint, judged by the shared forge_nav rules on the bundle's full blocking set; the collision rectangles cover exactly the blocked cells |
| [parallax_build.py](../scripts/parallax_build.py) | Periodic parallax layers, a parallax plan and a camera sweep | Each repeating layer is exactly periodic and its loop step is at most the p95 of its own column steps; the plan passes the generate2dmap validator |
| [ambient_bake.py](../scripts/ambient_bake.py) | A frame loop of ripples, haze, swaying foliage or glows on a static plate | The loop has an exact period; pixels outside the effect polygons never change |

Run every command from your project root. `<skill-dir>` is the codeart2d folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs go to a new `--output-dir` inside your project: the tool refuses an existing path, works in a hidden stage folder beside it and publishes only a complete result. With `--strict-qc` a failed check exits 1 and leaves nothing behind; without it the output is published with a failing QA envelope, so you can open the debug images, and the tool still exits 1 after its summary line with `error: published with QA status fail: <check ids> (see <qa file>)`. Exit 0 means pass or warn. Errors print one `error: ...` line and exit 1; a wrong argument is a usage error (argparse's `usage: ...` and exit 2). Success prints one JSON line with the output, metadata and QA paths and `failed_checks`. Spec files may be saved with a UTF-8 BOM.

## Playable maps: layout_build.py

```bash
python "<skill-dir>/scripts/layout_build.py" --spec meadow-layout.json --output-dir out/map-v1 --seed 7 --preview --strict-qc
```

With a corner-Wang tileset (for example from autotile_build.py; repeat `--tiles` for several sets):

```bash
python "<skill-dir>/scripts/layout_build.py" --spec meadow-layout.json --tiles out/tiles-v1/tileset-manifest.json --output-dir out/map-v2 --seed 7 --preview --strict-qc
```

Start from [examples/meadow-layout.json](../examples/meadow-layout.json): a 40x25-tile village with a pond, a village green, three spline roads leaving the map, three houses with doors, and seeded forest, hedgerow, shore and flower scatter. Its props are inline PixelSpecs, so the map needs no image files.

### Pipeline

1. **Terrain.** Materials are painted on the (W+1) x (H+1) vertex grid in spec order. Tile (x, y) takes the corners `[top_left, top_right, bottom_left, bottom_right]` = vertices (x, y), (x+1, y), (x, y+1), (x+1, y+1), the tileset_v1 `wang` order. Each vertex owns the tile-sized square centred on it.
2. **Hygiene**, repeated until nothing changes:
   - a cell no tileset can draw (for example path and water corners with only grass/path and grass/water sets) loses its lowest-priority non-base material;
   - a diagonal-only pair of corners (a saddle) gets its top corner filled;
   - a lone vertex with no same-material neighbour becomes the base.
   `hygiene.saddles` and `hygiene.specks` switch the last two off.
3. **Tiles.** Every cell gets exactly one tile, from the first tileset that has its corner tuple. Interior variants are chosen by a seeded hash and avoid repeating the left and upper neighbour. With several tilesets the bundle gets one tiles layer per set (`ground`, `ground-<id>`), each cell in exactly one of them; the tilesets are copied into `tilesets/<id>/`. A copied manifest drops its `qa` reference (the QA file stays with the tileset; the bundle's provenance lists it). Without `--tiles` the ground is a flat-colour image (`ground.png`) and the bundle says `placeholder: true`.
4. **Props.** Fixed `objects` first, then each `scatter` group (see below).
5. **Collision.** One blocking set, the same for every reader of the bundle (generate2dmap map_nav.py and the engine exporters, the scene preview and its runtime):
   - **Terrain.** With tilesets, each placed tile blocks with its own `collision` shapes (a tile with no shapes whose `properties.walkable` is false blocks whole). autotile_build tilesets carry these shapes, measured from the art. A tileset that has none gets the vertex-square rule of your materials (each corner owns its quadrant) written into the bundle's copy. Without tilesets, non-walkable terrain becomes exact rectangle solids in `collision.solids` (the merged vertex squares, per material).
   - **Props.** Each object keeps its prop's footprint as authored (in prop pixels, `basis: prop_px`) with `flip_x` and `scale`; readers mirror and scale it once.
   - **`collision.rects`.** Disjoint rectangles whose union is exactly a raster of `rectCell` px cells: the cells whose centre a blocker covers. `rectCell` defaults to the tile collision grid with tilesets (an autotile_build set's `collision_cell`) and to half a tile without, so the rects are exact on the terrain and approximate only prop footprints. They are part of the blocking set too.
6. **Reachability**, with the shared forge_nav rules (the code generate2dmap's map_nav.py runs) on the blocking set read back from the bundle. A point is valid when it and 8 samples of the actor ellipse (`rx = r`, `ry = r * ySquash`) lie inside the map and outside every blocker (a boundary counts as blocked). Validity is tested at grid nodes (`cell = max(1, round_half_up(r / 2))`); a 4-neighbour move is open when the straight segment between the two nodes stays clear, thin walls between samples included. Every exit, spawn and interaction must be reachable from the first spawn (or, without spawns, from the first exit's arrival point).
7. **Output**, staged and published together (see below).

### Spec (`codeart2d.layout_spec.v1`)

Terrain shapes use tile (vertex) coordinates; everything else is in world pixels (`tile * tiles`). Required: `schema`, `size`, `materials`, and at least one spawn or exit.

| Field | Meaning |
|---|---|
| `size` | `[W, H]` in tiles. `tile_size` defaults to 16 and must match the tilesets. |
| `seed` | Scatter, ellipse wobble and tile variants. `--seed` overrides it. |
| `materials` | `{name: {color, walkable, priority}}` in order. The first is the base unless `base` names another walkable material. A higher `priority` wins when hygiene must remove a material. |
| `terrain` | Paint operations, in order. `ellipse {center, radius, rotate?, wobble?}`, `rect {box: [x0, y0, x1, y1]}`, `polygon {points}`, `road {points, width, smooth}`. A vertex is painted when its point lies inside the shape (boundary included); a road paints vertices within `width / 2` of its Catmull-Rom centre line. Run roads past the map edge where exits go. |
| `actor` | `{radius, ySquash}`: the footprint judged by the reachability gate (defaults `round(0.3 * tile)` and 1.0). |
| `props` | Prop kinds: `{image}`, `{pixelspec}` (inline or a path), `{pack}` (a label in `prop_pack`, a prop-pack v2 manifest) or nothing (a flat placeholder sprite of `size` and `color`). Optional `variants` (a list of those sources), `anchor_px` (default bottom centre), `footprint`, `solid`, `occlusion` (`low`, `tall`, `foreground`), `flip` (scatter may mirror it, default true), `scale` and `interactions` (`{name, offset, reach}` relative to the anchor). Kind names name the prop images (`props/<kind>.png`, `props/<kind>-v<n>.png`), so they must differ in more than letter case (`rock` and `Rock` are refused: one file on Windows and macOS). |
| `objects` | Fixed placements `{id, prop, x, y, flip_x?, scale?, variant?}`; (x, y) is where the anchor lands. |
| `scatter` | Seeded groups, see below. |
| `exits` | `{id, edge, road \| span, to, depth?, radius?, arrival?}`, see below. |
| `spawns`, `interactions` | `{id, x, y, facing?}` and `{id, x, y, reach?}`. |

**Footprints** are in the prop's own pixels: the centre is `anchor_px + offset`, `width` runs across and `depth` along the ground, `rotate` is in degrees. The bundle stores them as authored; every reader scales them once by the instance scale, mirrors them with the sprite when `flip_x` is set, and never inflates them for the actor: the reachability gate accounts for the actor's size. Size the footprint to the trunk, base or walls, not to the whole silhouette, so canopies and roofs do not wall off paths.

**Scatter groups** `{id, kinds: {kind: weight}, count, spacing, density?, clearance?, near?, region?, attempts?}`:

- Candidates are drawn uniformly in `region` (`attempts` of them, default `40 * count`; `count` and `attempts` are at most 250,000 per group). They are kept when the value noise (`density.scale`, `octaves`, plus `edge_bonus` within `edge_distance` of the map edge) reaches `density.threshold`, and when every clearance holds. Clearances are distances in px from `road` material, `blocked` terrain, fixed `object` sprites, `exit` approaches, `spawn` and interaction points, and the map `edge`. Each `near` band `{field: [min, max]}` keeps candidates within that distance of the field.
- Accepted props keep Poisson-disk spacing from every earlier scatter prop.
- **Variety.** A look is (kind, variant, mirror). Looks are drawn by weight, and every same-group neighbour within two spacings penalises its own look, so nearby props rarely repeat one. The `prop_variety` check warns when more than 35% of a group's props have an identical-looking nearest neighbour.
- Frame paths with low props in a `near.road` band and keep tall ones back with a larger `road` clearance.

**Exits.** An exit with `road` spans the run of that road's material along the `edge` where its centre line meets the edge, plus half a tile at each end; `span: [a, b]` gives the tiles along the edge directly. The trigger rectangle is `depth` px deep (default half a tile) on the edge. Each portal is written with `activation: intent`, the outward `travelDirection`, `radius` (default the actor radius), `latch` and `requiresMovement`. Each exit also gets an arrival spawn (`from-<exit id>` unless `arrival` names it), placed on a reachable cell just outside the trigger and facing into the map. `entranceByFrom` maps the destination map (the part of `to` before `:`) to that spawn.

### Outputs

- `map-bundle.json`: generate2dmap.map_bundle.v2 with `world`, `tile_size`, `terrain` (the vertex grid), `tilesets`, `layers` (tiles or the ground image, then `props` objects), `objects`, `collision`, `portals`, `spawns`, `interactions`, `camera.bounds`, `art_source`, `placeholder`, a `qa` summary and `provenance` (including where the tile collision came from and the navigation numbers). It also writes `id`, `props` (the props registry, one bundleProp per prop image), `roads` (centre lines in px), `collision.rectCell`, per object `kind`, `flip_x` and `group`, per portal `edge`, and per terrain solid `material`.
- `terrain-vertices.json`: `generate2dmap.vertex_grid.v1` (`size`, `materials`, `data[row][column]`).
- `props/<prop>.png`, `tilesets/<id>/...` and, without tiles, `ground.png`.
- `preview.png` and `debug.png` with `--preview`. In `debug.png`, dark means too tight for the actor and magenta means walkable but unreachable. It also draws the blockers (blue terrain and tile collision, yellow prop footprints), the merged rectangles (red), exits with their radius (cyan), interactions (yellow) and spawns (white).
- `layout-qa.json`: the QA envelope. `codeart-meta.json`: art_source code, with `placeholder` set honestly.

| Check | Fails or warns when |
|---|---|
| `tiles_drawable` | a cell has no tile (with tilesets) |
| `tile_collision` (warn) | a full tile of a material blocks differently from that material's `walkable` in your spec (the tileset's collision still wins) |
| `rect_union_equals_blocked`, `rects_disjoint` | the merged rectangles do not reproduce the blocked raster exactly |
| `spawns_reachable`, `exits_reachable`, `interactions_reachable` | a target is outside the first spawn's component |
| `arrivals_outside_triggers` | an exit has no reachable cell just outside its trigger |
| `enclosed_pockets` (warn) | walkable areas of 8 or more cells cannot be reached |
| `prop_variety`, `scatter_filled`, `hygiene_converged` (warn) | repeated looks, fewer props than requested, or hygiene still changing after 10 passes |

The terrain, tile and footprint shapes are exact; an engine that collides with `collision.rects` alone gets the `rectCell` approximation of prop footprints, and the reachability proof covers both. generate2dmap's map_nav.py reads the bundle with the same rules and finds the same valid and reachable nodes; run it for the navigation grid, then use export_tiled.py and the Godot or LDtk exporters for engines.

## Parallax backgrounds: parallax_build.py

```bash
python "<skill-dir>/scripts/parallax_build.py" --spec parallax-gen.json --output-dir out/bg-v1 --validate --sweep-frames 49
```

Start from [examples/parallax-gen.json](../examples/parallax-gen.json): a dithered dusk sky with stars, pink clouds, a far ridge, a forested mid ridge and a grassy foreground, all pixel art.

### Spec (`codeart2d.parallax_spec.v1`)

`viewport [w, h]`, `camera {x, y, zoom}` (ranges `[min, max]`, as in the parallax plan), `seed`, `pixel_art` (default true; false renders at 4x and box-reduces for soft edges) and `sweep_frames` (default 49). A canvas holds at most 16.7 million pixels (the composite at the minimum zoom included) and a sweep at most 128 million in all, so a huge viewport or a tiny zoom is refused before anything is drawn. `layers` run back to front:

| Kind | Parameters |
|---|---|
| `sky` | `colors` (gradient stops, top to bottom), `bands` (default 8 for pixel art), `dither` (4x4 ordered dither), `stars {count, color, band: [top, bottom]}` (fractions of the viewport), `sun {x, y, radius, color}`. Opaque; static unless it scrolls. |
| `ridge` | `base_y`, `amplitude`, `cells` (noise cells per period), `octaves`, `fill`, `rim` and `rim_px`, `trees {count, height: [min, max], width_ratio, tiers, color}` |
| `clouds` | `count`, `y_range`, `size` (puff radius range), `puffs`, `fill`, `shade` |
| `foreground` | like `ridge`, plus `grass {count, height, color}`; role `foreground` |
| `image` | your own PNG: `image`, `role`, `scroll`, `repeat`, `alpha`, `scale` (a whole number for pixel art, which is scaled by nearest neighbour), `anchor_px`, `offset`, `require_canvas_coverage` |

Every non-sky layer needs `scroll` (its camera factor: about 0.1 to 0.3 far, 0.5 mid, 1.0 for the play layer, above 1 for foreground). Generated layers repeat horizontally with `period` px (default: the viewport width). Image layers repeat only when `repeat` says so. A layer's `id` names its PNG (`<id>.png`), so ids must differ in more than letter case (`far` and `FAR` are refused).

### What it proves

- **Exact periodicity.** Ridge lines use a wrapping noise lattice, and trees, clouds, stars and grass are drawn at periodic distances. Each repeating layer is rendered again twice as wide and must equal itself tiled twice: the `periodic:<layer>` check counts differing pixels, which must be 0.
- **Calm origin and loop step.** The layer is rolled so that its wrap falls on its smallest column step. `loop_step:<layer>` is forge_core.seam_report over the columns, and the wrap step must be at most the p95 of the column steps. Image layers are measured too, never repaired.
- **Coverage.** Canvases are sized from the camera envelope: taller for a vertical camera range or a zoom below 1, wider for a non-repeating layer. The plan keeps the viewport alignment through `offset`, uses one uniform scale, a top-left `camera.pivot` and `require_canvas_coverage: true`.
- **Validator.** The sibling generate2dmap validate_parallax.py checks the plan: alpha, one opaque sky, and coverage at every camera and zoom extreme. Its report is written to `validate-parallax.json` with plan-relative image names. `--validate` requires the validator. By default it runs when generate2dmap is installed beside codeart2d; `--validator PATH` points elsewhere; `--no-validate` skips it, and the check is then marked skipped.
- **Sweep.** `sweep_frames` frames run from the camera minimum to its maximum, at the widest zoom and integer pixel positions. They are written as `sweep-sheet.png` and an animated `sweep.webp`. Checks: no transparent pixel in any frame, and every coverage-required layer covers every frame.

Outputs: one PNG per layer, `parallax-plan.json` (map parallax_plan, with each layer's `sha256` and `kind`), `validate-parallax.json`, the sweep, `parallax-qa.json` and `codeart-meta.json`. The engine must use the same transform: `screenTopLeft = (offset - anchor_px * scale - camera * scroll_factor) * zoom`. Check sub-pixel scrolling, culling and other camera transforms in the engine.

## Ambient loops on plates: ambient_bake.py

```bash
python "<skill-dir>/scripts/ambient_bake.py" --plate art/harbor-plate.png --spec plate-effects.json --output-dir out/harbor-ambient-v1 --preview --strict-qc
```

Start from [examples/plate-effects.json](../examples/plate-effects.json). Its polygons are UV fractions, so it runs on any plate. It has rippling harbour water, swaying reeds, chimney haze and a pulsing lantern, plus a protected boat.

### Spec

Either `codeart2d.plate_effects.v1` or a generate2dmap.stage.v1 document; the stage's `effects` and `protectedRegions` are used, in UV, and its `sourceSize` must match the plate's aspect.

- `units` (`uv` or `px`), `period_ms`, `frames` or `fps` (default 12 fps), `sampling` (`bilinear`; `nearest` with `feather: 0` keeps the plate's exact colours).
- `effects[{id, kind, polygon, period, amplitude, wavelength, axis, feather, opacity, color}]`:
  - `ripple`: rows (axis `x`) or columns (axis `y`) slide in a travelling wave, like the water strips of a battle-stage runtime;
  - `shimmer`: heat haze;
  - `sway`: displacement that grows from the bottom of the region to its top;
  - `glow`: blends towards `color` by up to `amplitude` (0 to 1).
- `protected[{id, polygon | box}]`: regions that never move. A stage's `appliesTo` is ignored: protection applies to every effect.

Every effect period must divide the loop period, which defaults to their least common multiple, at most 60 s. All temporal terms are integer multiples of the phase, and phases come from `((frame * cycles) mod N) / N`, so frame N is frame 0. A runtime term such as `sin(q * 0.57 - phase * 0.37)` does not repeat after one period: the loop pops at its seam. The feather is a smoothstep of the distance inside the polygon; the image border does not count as an edge.

### Outputs and checks

- `frames/frame_000000.png` and on (0-based); `mask.png`, the 8-bit motion mask, to use as a scene-motion mask.
- `ambient-loop.json` (`codeart2d.ambient_loop.v1`): `period_ms`, `frame_count`, integer `durations_ms` that sum exactly to the period, the rational `fps`, frame and mask hashes, the resolved effects, `art_source` and provenance.
- `ambient-qa.json`, plus `review.png` and `preview.webp` with `--preview`.

| Check | Meaning |
|---|---|
| `exact_period` | frame N, rendered again, equals frame 0 byte for byte |
| `outside_unchanged` | no pixel outside the effect polygons (or inside a protected region) differs from the plate, in any frame |
| `motion_present` | each effect changes at least one pixel during the loop; a flat-colour area cannot ripple |

The motion is code-drawn, but the plate usually is not, so no `codeart-meta.json` is written. `--plate-art-source` (default `existing`) records where the plate came from, and `art_source` is `mixed` unless the plate is code art. The QA envelope also carries forge_core.seam_report inside the mask as a diagnostic. To ship a video, encode the frames with the generate2dmap scene-motion tools; the encode needs its own decoded-file QA.

## When a check fails

| Symptom | Fix |
|---|---|
| `exits_reachable` lists an exit | Open `debug.png`: magenta areas are walkable but cut off. Widen the road, lower scatter density near it, or raise `clearance.exit`, `road` or `spawn`. |
| `exit X: road ... does not reach the edge` | Extend the road's points past the edge (for example x = -1) and keep its width at least 1.5 tiles. |
| `arrivals_outside_triggers` | The ground just inside the exit is blocked; clear it or move the exit's `span`. |
| `scatter_filled` warns | Lower `density.threshold` or the clearances, raise `attempts`, or set a `region`. |
| `loop_step:<layer>` fails on an image layer | The image's left and right edges do not meet; make it tile, or set `repeat: [false, false]` and size it to cover the camera range. |
| `motion_present` fails | The region is flat colour or fully protected; move the polygon onto textured art or raise `amplitude`. |
