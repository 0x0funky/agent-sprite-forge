# Scene preview: walk a map bundle in one HTML file

`scripts/build_scene_preview.py` turns a `map_bundle.v2` into `preview.html`, a single page that opens straight from disk: every image is an embedded data URI and the collision walker, [runtime/map-runtime.mjs](runtime/map-runtime.mjs), is inlined verbatim. A debug actor walks the bundle's collision with the keyboard or by clicking, objects and the actor are Y-sorted, and `window.__scene` exposes a snapshot and a route check. Use it to look at a playable map at world scale and to prove its routes before exporting it to an engine.

Run from the project root (`<skill-dir>` is this skill's folder; `${CLAUDE_SKILL_DIR}` in Claude Code). Validate the bundle and its reachability with map_bundle.py and map_nav.py first when they are available.

```bash
python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json --output-dir map/qa/preview
python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json --prop-pack assets/props/prop-pack.json --output-dir map/qa/preview-2 --verify --strict
```

The output folder must be new; the build is staged and published only after its checks pass. Success prints one JSON line with `status`, `output_dir`, `preview`, `metadata`, `bytes`, `verify` and `warnings`; every failure is one `error: ...` line with exit code 1 and leaves nothing behind.

## What the page reads

| Bundle field | Used for |
|---|---|
| `world.width`, `world.height` | Canvas size in world pixels (y down) |
| `layers` | Drawn in order. `image` layers sit at the world origin, one image pixel per world pixel. `tiles` layers are pre-rendered from their tileset manifest (`tilesets[].manifest`, `generate2dmap.tileset.v1`). The first `objects` layer is where objects and the actor are drawn; without one they go above every layer. |
| `objects` | Art at `(x, y)` with `anchor_px` on that point, size times `scale`; draw order `(sortY or y, x, id)`, the actor in front on ties. Footprints block (below). |
| `collision`, `material_map` | The walker's collision (below) |
| `spawns`, `portals`, `interactions`, `anchors` | Start points, exits, reach circles, approach points and slots |

Object art is looked up in this order: the object's `image` (with optional `image_sha256`), the accepted item of a prop pack whose `label` equals the object's `prop` (`--prop-pack`, then the bundle's `prop_packs[].manifest`), the object's `occluder.source`. An object with none is drawn as a magenta post and reported as a warning.

Tile data is a CSV file, a JSON file or an inline grid of 0-based tile indices into the tileset (tile `i` sits at column `i % columns`). A negative index, `null` or an empty CSV field is an empty cell; one comma ending a CSV row (Tiled style) is ignored. JSON may also be `{"width": w, "data": [flat list]}`.

Every relative path resolves from the bundle's folder (tileset images from their manifest's folder) and must be a POSIX path without a drive, scheme or leading slash. When a reference carries `sha256`, a mismatch stops the build.

Not drawn or not used: `stage`, `lights`, `atmosphere`, `camera`, `animatedParts`, occluder masks, alpha fades, tile collision shapes and tileset `walkable` flags. Walls belong in `collision` or `material_map`; the tile layers are pictures here.

## Collision and movement

The walker follows plan Appendix C, the rules map_nav.py uses. Field reference: [schemas/map.schema.json](schemas/map.schema.json) (`collision`, `solid`, `footprint`, `portal`).

- The actor footprint is an ellipse with `rx = actorRadius` and `ry = actorRadius * ySquash` (`ySquash` defaults to 1; HD-2D plates usually record 0.58). A position is valid when its centre and 8 points on that ellipse lie in a walk region outside its holes (in the world rectangle when there are no walk regions) and outside every solid.
- Solids are `collision.solids` (rect, ellipse, polygon), `collision.rects`, blocked `material_map` cells and object footprints. A footprint is scaled once by the object's `scale` (not at all for `basis: "world_px"`) and blocks unless the object says `solid: false` or the shape is `none`. Solids are never inflated by the actor radius again.
- `material_map` classes: `solid` blocks; `liquid` and `hazard` block unless `walkable: true`; `one_way` (a side-scroll rule) and `decor` never block the top-down walker. Colours match exactly; `index` materials need a palette or greyscale image.
- Paths come from a 4-neighbour grid search with `cell = max(1, round half up(actorRadius / 2))` px and are smoothed with `segmentClear` (samples at most `cell / 2` apart). The walker spends each tick's budget (`--speed` / 60) along the path without idle ticks at corners and never moves further than the budget. Keyboard moves slide along walls and stop when blocked.
- Exits: an `intent` portal fires within `radius` of its trigger when the input direction is within about 75 degrees of `travelDirection` (cosine above 0.25); a `crossing` portal (the default) fires while the actor is inside it and moving. Walking inward never exits. Arriving inside a portal's zone latches it until the actor leaves the zone, and a fired portal stays latched the same way. After an exit fires the page simulates the round trip: the actor returns through `entranceByFrom[<destination map>]` when that spawn is in this map.
- Interactions without `reach` use `6 x actorRadius`.

Validity is sampled, so a solid thinner than half a nav cell can slip between samples, and rotated shapes may differ by a rounding step from a Python engine at their exact edge. A planned path is walked as planned.

## Using the page

Click the map to focus it, then walk with WASD or the arrow keys, or click a point to walk there; E interacts with the nearest interaction in reach. **Debug** draws walk regions (green), holes (yellow), solids (red), object footprints (magenta), blocked material cells (blue), portals with their intent zone and travel arrow (orange), spawns, reach circles, anchors with slots and approach points, the current path and the 8 footprint samples (green free, red blocked). **Nav grid** adds the grid cells reachable from the actor (green), standable but cut off (yellow) and blocked (red). **Check routes** runs the route check; **Snapshot** shows the JSON below the map.

## window.__scene

| Member | Does |
|---|---|
| `ready`, `error`, `tick` | Load state, the first error and the simulation tick (60 per second) |
| `snapshot()` | JSON with `schema: "generate2dmap.scene_snapshot.v1"`: world, actor (position, validity, latched portals), `events`, `exitsFired`, `interactionsReached`, `drawOrder`, `assets` and `routes` |
| `traverseAll()` | The route check: from the spawns to every exit, interaction, approach point and slot with the same per-tick walker; each result says `ok`, `reason`, `ticks`, `distance`, `maxStep`, `zeroMotionTicks`, whether an exit `fired` and whether walking inward fired it; `portals` lists arrival spawns found outside their trigger |
| `reset(spawnId?)`, `teleport(x, y)`, `walkTo(x, y)` | Place or send the actor (placing it arms arrival latches) |
| `hold(key)`, `release(key)`, `step(n)`, `waitTicks(n)` | Drive the keyboard and the clock from a script |
| `isBlocked(x, y)`, `segmentClear(ax, ay, bx, by)`, `findPath(x, y)`, `canMove(dx, dy)` | Ask the collision query |
| `probeYSort()`, `setDebug(on)`, `setGrid(on)` | Pixel check of the draw order; overlay toggles |

For acceptance, run the route check (button, `window.__scene.traverseAll()` in the browser console, or `--verify`) and attach the snapshot JSON with the other QA files. A route result with `ok: false` names the problem: unreachable, walker blocked, exit did not fire, or zero-motion ticks.

## Checks and outputs

`preview-qa.json` is a QA envelope (`generate2dmap.scene_preview_qa.v1`): inputs and outputs with sha256, the checks below, warnings, and a `preview` block (bytes, runtime version and sha256, layers, draw order, art sources, counts, material summary). It has no time stamp, so the same inputs give the same bytes, as does `preview.html`.

- Always enforced: the page is at most `--max-bytes` (default 16,000,000), is ASCII, has one inline script holding the runtime, and nothing in it can load from outside (no URLs, no loading tags or attributes, no fetch or workers; data is escaped JSON and allow-listed base64 images).
- Warnings (fail with `--strict`): objects without art, image layers of another size than the world, tiles past the world, unclassified material pixels, duplicate object ids or prop labels, entrance spawns missing from the map.
- `--verify` opens the page in headless Chromium through node and the playwright npm package (install it in the project, or point `NODE_PATH` at its `node_modules`, and run `npx playwright install chromium`). It checks for page errors and network requests, that every image loads, that the keyboard moves the actor wherever the first step is free, the route check and a Y-sort pixel probe, then writes `scene-snapshot.json`, `preview-screen.png` and `preview-debug.png`. Without node, playwright or Chromium it prints `verify: SKIPPED (...)` and the build still succeeds. Failed browser checks publish with `status: "fail"` unless `--strict`.

## What the preview proves

- The bundle's art, tile layers and objects assemble at world scale with the ground-line draw order compose uses, and the files are the ones the bundle hashes name.
- With the route check: from the spawns, the walker reaches every exit, interaction, approach point and slot under the bundle's own collision data, without stalls or over-budget ticks; intent exits fire with their travel direction and not when walking inward; arrival spawns lie outside their trigger.
- The page is self-contained: the static scan, and with `--verify` the browser's request log, show nothing loaded from outside it.

## What it does not prove

- Engine behaviour: controllers, physics, acceleration and other speeds; Tiled, Godot or LDtk imports; mobile, touch and other browsers (only headless Chromium is run).
- Art: the actor is a marker sized from `actorRadius`, not character art; animation, lights, atmosphere, parallax, cut-aways and per-pixel occlusion are not shown. Look at the page and the screenshots before calling the art finished.
- Collision beyond its rules: validity is sampled; parity with map_nav.py is tested at integration, not by this tool; tile collision is ignored.
- Gameplay: dialogue, encounters, saves and anything after an exit fires.

See [layered-map-contract.md](layered-map-contract.md) for placement, depth and collision conventions.
