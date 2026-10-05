# Scene preview: walk a map bundle in one HTML file

`scripts/build_scene_preview.py` turns a `map_bundle.v2` into `preview.html`, a single page that opens straight from disk: every image is an embedded data URI and the collision walker, [runtime/map-runtime.mjs](runtime/map-runtime.mjs), is inlined verbatim. A debug actor walks the bundle's collision with the keyboard or by clicking, objects and the actor are Y-sorted, and `window.__scene` exposes a snapshot and a route check. Use it to look at a playable map at world scale and to prove its routes before exporting it to an engine.

Run from the project root (`<skill-dir>` is this skill's folder; `${CLAUDE_SKILL_DIR}` in Claude Code). Validate the bundle with map_bundle.py first when it is available; the preview reads collision through the same rules map_nav.py proves reachability with.

```bash
python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json --output-dir map/qa/preview
python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json --prop-pack assets/props/prop-pack.json --output-dir map/qa/preview-2 --verify --strict
```

The output folder must be new; the build is staged and published only after its checks pass. Success prints one JSON line with `status`, `output_dir`, `preview`, `metadata`, `bytes`, `verify`, `warnings` and `failed` (the ids of failed checks). Exit codes: 0 when the published report passes or warns; 1 when it was published with status `fail` (a failed `--verify` without `--strict`), or when nothing was published (one `error: ...` line, nothing left behind); 2 on a usage error.

## What the page reads

| Bundle field | Used for |
|---|---|
| `world.width`, `world.height` | Canvas size in world pixels (y down) |
| `layers` | Drawn in order. `image` layers sit at the world origin, one image pixel per world pixel. `tiles` layers are pre-rendered from their tileset manifest (`tilesets[].manifest`, `generate2dmap.tileset.v1`). The first `objects` layer is where objects and the actor are drawn; without one they go above every layer. |
| `objects`, `props`, `prop_packs` | Art at `(x, y)` with `anchor_px` on that point, size times `scale`, mirrored around the anchor with `flip_x`; draw order `(sortY or y, x, id)`, the actor in front on ties. Footprints block (below). |
| `collision`, `material_map`, tile collision | The walker's collision (below) |
| `spawns`, `portals`, `interactions`, `anchors` | Start points, exits, reach circles, approach points and slots |

Object art is looked up in the one order every map tool uses (integration decision D6): the object's `image` (with optional `image_sha256`); the bundle's `props[prop]` (an inline item's `image`, relative to the bundle, or the accepted item of the prop pack named by `pack` and `label`); the accepted item of a prop pack whose `label` equals the object's `prop` (the bundle's `prop_packs[].manifest`, then `--prop-pack`); the object's `occluder.source`. An object with none is drawn as a magenta post and reported as a warning. An object without `anchor_px` takes its props item's `anchor_px`, else the bottom centre of its image (warned).

Tile data is a CSV file, a JSON file or an inline grid of 0-based tile indices into the tileset (tile `i` sits at column `i % columns`); `-1` or `null` is an empty cell, and the grid covers the world exactly. JSON may also be `{"width": w, "data": [flat list]}`.

Every relative path resolves from the bundle's folder (tileset images from their manifest's folder, pack items from their pack's folder) and must be a POSIX path without a drive, scheme or leading slash. When a reference carries `sha256`, a mismatch stops the build. JSON files are read as UTF-8 with an optional BOM; NaN, Infinity and duplicate keys are refused.

Not drawn or not used: `stage`, `lights`, `atmosphere`, `camera`, `animatedParts`, occluder masks and alpha fades.

## Collision and movement

Collision is one blocking set for every map tool (integration decisions D1 and D2): the page gets it from `scripts/forge_nav.py`, the rule book N1-N15 that map_nav.py, the engine exporters and layout_build share, and map-runtime.mjs mirrors those rules rule for rule (its header lists them). Field reference: [schemas/map.schema.json](schemas/map.schema.json) (`collision`, `solid`, `footprint`, `tile`, `portal`).

- The actor footprint is an ellipse with `rx = actorRadius` and `ry = actorRadius * ySquash` (`ySquash` defaults to 1; HD-2D plates usually record 0.58). A position is valid when its centre and 8 points on that ellipse lie in the walk area and on no blocker. Without walk regions the walk area is the closed world box `[0, width] x [0, height]`; with them, a point is inside when some region contains it and none of that region's holes do (the even-odd rule).
- Blockers are closed sets: a point on the boundary is blocked (rect `x <= px <= x + w`, ellipse `<= 1`, polygon interior or edge). They are `collision.solids`, `collision.rects`, the footprints of objects whose `solid` is not false, the collision of placed tiles and blocking `material_map` pixels. A shape without area blocks nothing.
- Footprints come from the object, else from its `props` item (pack items included). They are scaled once by the object's `scale`, not at all for `basis: "world_px"` (`prop_px` is the default; `image_px` is its legacy alias), and `flip_x` mirrors them around the anchor. They are never inflated by the actor radius again.
- Tile collision: each placed tile's `tiles[].collision` shapes (tile pixels) are moved to its cell, and a tile without shapes whose `properties.walkable` is false blocks its whole cell. The page receives them as world solids (drawn orange with **Debug**).
- `material_map` pixels are squares of a whole number of world px and every opaque pixel must match one material (colour, or `index` for a palette or greyscale image), else the build stops. `solid` blocks; `liquid` and `hazard` block unless `walkable: true`; `decor` never blocks. `one_way` blocks moving down onto it (from above) and never a point; the rule applies to every map, so a top-down map should not use it.
- `segmentClear(a, b)`: footprint samples at most `cell / 2` apart are valid, the one_way rule holds and the actor's centre stays in the walk area and off every blocker along the whole segment (the thin-gap rule), so no wall or gap thinner than the samples is jumped. A single touching point is not a crossing: a path that only touches a vertex, a rect corner or an ellipse stays open (pieces of 1e-9 px or less between boundary cuts are rounding slivers, N10).
- Paths come from a 4-neighbour grid search with `cell = max(1, round half up(actorRadius / 2))` px; a move between neighbouring cell centres is open when `segmentClear` holds. A start joins every valid cell within two cells that it reaches in a straight line. Paths are smoothed with `segmentClear`. The walker spends each tick's budget (`--speed` / 60) along the path without idle ticks at corners and never moves further than the budget. Keyboard moves slide along walls and stop when blocked.
- Exits: an `intent` portal fires within `radius` of its trigger when the input direction is within about 75 degrees of `travelDirection` (cosine above 0.25); a `crossing` portal (the default) fires while the actor is inside its closed trigger and moving. Walking inward never exits. Arriving inside a portal's zone latches it until the actor leaves the zone, and a fired portal stays latched the same way. After an exit fires the page simulates the round trip: the actor returns through `entranceByFrom[<destination map>]` (a spawn of this map or an `[x, y]` point).
- Interactions with `reach` are reached from any reachable cell centre within `reach`; interactions without it are point targets like slots and approach points: the actor must stand on them. The page counts one nav cell around them as in reach for the E key.

Footprint samples are taken every half nav cell, so a solid thinner than that can lie between the samples of the footprint's rim (the centre path is exact). Rotated shapes may differ by one rounding step of sin and cos from Python at their exact edge. A planned path is walked as planned.

## Using the page

Click the map to focus it, then walk with WASD or the arrow keys, or click a point to walk there; E interacts with the nearest interaction in reach. **Debug** draws walk regions (green), holes (yellow), solids (red), tile collision (orange), object footprints (magenta), blocking material pixels (blue) and one_way pixels (yellow), portals with their intent zone and travel arrow (orange), spawns, reach circles, anchors with slots and approach points, the current path and the 9 footprint samples (green free, red blocked). **Nav grid** adds the grid cells reachable from the actor (green), standable but cut off (yellow) and blocked (red). **Check routes** runs the route check; **Snapshot** shows the JSON below the map.

## window.__scene

| Member | Does |
|---|---|
| `ready`, `error`, `tick` | Load state, the first error and the simulation tick (60 per second) |
| `snapshot()` | JSON with `schema: "generate2dmap.scene_snapshot.v1"`: world, actor (position, validity, latched portals), `events`, `exitsFired`, `interactionsReached`, `drawOrder`, `assets` and `routes` |
| `traverseAll()` | The route check: from the starts (spawns, then arrival points given as `[x, y]`) to every exit, interaction, approach point and slot with the same per-tick walker; reachability follows forge_nav (N14) and each result says `ok`, `reason`, `ticks`, `distance`, `maxStep`, `zeroMotionTicks`, whether an exit `fired` and whether walking inward fired it; `portals` lists each arrival with `found`, `valid`, `joined` and `bounceBack` (the triggers that hold it) |
| `reset(spawnId?)`, `teleport(x, y)`, `walkTo(x, y)` | Place or send the actor (placing it arms arrival latches) |
| `hold(key)`, `release(key)`, `step(n)`, `waitTicks(n)` | Drive the keyboard and the clock from a script |
| `isBlocked(x, y)`, `segmentClear(ax, ay, bx, by)`, `findPath(x, y)`, `canMove(dx, dy)` | Ask the collision query |
| `probeYSort()`, `setDebug(on)`, `setGrid(on)` | Pixel check of the draw order; overlay toggles |

For acceptance, run the route check (button, `window.__scene.traverseAll()` in the browser console, or `--verify`) and attach the snapshot JSON with the other QA files. A route result with `ok: false` names the problem: unreachable (with forge_nav's reason), walker blocked, exit did not fire, or zero-motion ticks.

## Using map-runtime.mjs in a game

The page's walker is [runtime/map-runtime.mjs](runtime/map-runtime.mjs), a dependency-free ES module a browser or node game can import as it is (`createMapRuntime`, `isValid`, `segmentClear`, `findPath`, `stepActor`, `traverseRoutes`). It reads no files, so two parts of the blocking set must reach it resolved: the collision of placed tiles and the material map. The preview passes them in the page; a game gets them from `map_nav.py check`, whose `nav-grid.json` holds them as `runtimeInputs`:

```js
import {createMapRuntime, findPath} from "./map-runtime.mjs";
const world = createMapRuntime(bundle, navGrid.runtimeInputs); // {tileSolids, materialGrid}
```

`createMapRuntime` throws a `TypeError` for a bundle with a `tiles` layer when neither `options.tileSolids` is given nor `collision.tilesResolved` is true (the tile solids already added to `collision.solids`), and for a `material_map` without `options.materialGrid` unless `options.ignoreMaterialMap` is true. `materialGridFromRGBA` builds the grid from a decoded colour image when the material map is not palette-indexed. A pack-based `props` entry (`pack` + `label`) must be resolved into an inline item first, as the preview does.

## Checks and outputs

`preview-qa.json` is a QA envelope (`generate2dmap.scene_preview_qa.v1`): inputs and outputs with sha256, the checks below, warnings, and a `preview` block (bytes, runtime version and sha256, layers, draw order, art sources, counts, the material summary and the collision counts: collision solids, rects, footprints, tile solids and the blocking material classes). It has no time stamp, so the same inputs give the same bytes, as does `preview.html`.

- Always enforced: the page is at most `--max-bytes` (default 16,000,000), is ASCII, has one inline script holding the runtime, and nothing in it can load from outside (no URLs, no loading tags or attributes, no fetch or workers; data is escaped JSON and allow-listed base64 images). The bundle's collision must read through forge_nav.
- Warnings (fail with `--strict`): objects without art, image layers of another size than the world, tiles past the world, duplicate object ids or prop labels, entrance spawns missing from the map.
- `--verify` opens the page in headless Chromium through node and the playwright npm package (install it in the project, or point `NODE_PATH` at its `node_modules`, and run `npx playwright install chromium`). It checks for page errors and network requests, that every image loads, that the keyboard moves the actor wherever the first step is free, the route check and a Y-sort pixel probe, then writes `scene-snapshot.json`, `preview-screen.png` and `preview-debug.png`. Without node, playwright or Chromium it prints `verify: SKIPPED (...)` and the build still succeeds. Failed browser checks publish with `status: "fail"` and exit 1 unless `--strict` (then nothing is published).

## What the preview proves

- The bundle's art, tile layers and objects assemble at world scale with the ground-line draw order compose uses, and the files are the ones the bundle hashes name.
- With the route check: from the starts, the walker reaches every exit, interaction, approach point and slot under the same blocking set forge_nav and map_nav read, without stalls or over-budget ticks; intent exits fire with their travel direction and not when walking inward; arrivals stand outside every trigger.
- The page is self-contained: the static scan, and with `--verify` the browser's request log, show nothing loaded from outside it.

## What it does not prove

- Engine behaviour: controllers, physics, acceleration and other speeds; Tiled, Godot or LDtk imports; mobile, touch and other browsers (only headless Chromium is run).
- Art: the actor is a marker sized from `actorRadius`, not character art; animation, lights, atmosphere, parallax, cut-aways and per-pixel occlusion are not shown. Look at the page and the screenshots before calling the art finished.
- Collision beyond its rules: footprint samples are half a nav cell apart; JS and Python agreement is tested on synthetic fixtures (`tests/test_map_runtime_js.py`, `tests/test_collision_parity.py`), not on each map.
- Gameplay: dialogue, encounters, saves and anything after an exit fires.

See [layered-map-contract.md](layered-map-contract.md) for placement, depth and collision conventions.
