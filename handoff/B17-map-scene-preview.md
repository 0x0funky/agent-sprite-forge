# B17-map-scene-preview: single-file playable scene preview (build_scene_preview.py) and the shared JS collision query (map-runtime.mjs)

Branch `asf/B17-map-scene-preview` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files:
[build_scene_preview.py](../skills/generate2dmap/scripts/build_scene_preview.py) (B17-T2),
[map-runtime.mjs](../skills/generate2dmap/references/runtime/map-runtime.mjs) (B17-T1),
[scene-preview.md](../skills/generate2dmap/references/scene-preview.md) (B17-T3),
[tests/test_scene_preview.py](../tests/test_scene_preview.py) (30 tests, 1 skipped unless Playwright is resolvable),
[tests/test_map_runtime_js.py](../tests/test_map_runtime_js.py) (6 tests, node marker),
[tests/js/map-runtime.test.mjs](../tests/js/map-runtime.test.mjs) (24 node:test cases).

build_scene_preview uses forge_core's `utf8_stdio`, `ascii_text`, `load_rgba`, `staged_output`, `portable_path`, `sha256_bytes`, `sha256_file` and `write_json`. map-runtime.mjs has no dependencies and no DOM, network, clock or random calls.

## 1. CLIs

Run from the user's project root; `<skill-dir>` is the generate2dmap folder (`${CLAUDE_SKILL_DIR}` in Claude Code). One verb, so no subcommands.

    python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json --output-dir map/qa/preview
    python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json --prop-pack assets/props/prop-pack.json --output-dir map/qa/preview-2 --verify --strict
    python "<skill-dir>/scripts/build_scene_preview.py" --bundle scene/stage-bundle.json --spawn hero --speed 240 --zoom 1 --output-dir scene/qa/preview

- Flags: `--bundle` (required), `--output-dir` (required, must not exist), `--prop-pack MANIFEST` (repeatable), `--spawn ID`, `--speed PX_PER_S` (default 12 x actorRadius, at least 60), `--actor-height PX` (default 4.5 x actorRadius, at least 12), `--zoom 1-8` (default the largest of 1-4 that keeps the world within 1280 px), `--title`, `--max-bytes` (default 16000000), `--verify`, `--verify-timeout` (120 s), `--strict`.
- Outputs in the new folder: `preview.html` and `preview-qa.json`; with a successful `--verify` also `scene-snapshot.json`, `preview-screen.png` and `preview-debug.png`. Work happens in `forge_core.staged_output`; a failed gate leaves nothing behind.
- Success prints one ASCII JSON line: `status` (pass, warn or fail), `output_dir`, `preview`, `metadata` (preview-qa.json), `bytes`, `verify` (not-requested, SKIPPED, pass, fail) and `warnings` (count). Errors are one `error: ...` line on stderr with exit 1, usage errors included; a failed QA gate says `error: QA failed, nothing published: ...`.
- `--verify` without node, the playwright npm package or its Chromium prints `verify: SKIPPED (<reason>)` on stderr and still exits 0 (even with `--strict`). Playwright is found by node's normal resolution from the current directory plus `NODE_PATH`.
- `--help` is ASCII and exits 0 under cp1252 and cp950 (tested); its epilog holds two single-line examples.
- Library entry points (same skill): `build_preview(args)`, `clean_collision`, `clean_portals`, `compile_objects`, `compile_layers`, `render_tile_layer`, `compile_material_grid`, `script_json`, `external_references`.

Runtime (a JavaScript module, not a CLI): `import {createMapRuntime, isBlocked, segmentClear, findPath, stepActor} from "./map-runtime.mjs";`. Exports: RUNTIME_VERSION, TICK_HZ, INTENT_MIN_COS, SNAPSHOT_SCHEMA, FOOTPRINT_DIRECTIONS, createMapRuntime, pointFree, isBlocked, footprintSamples, segmentClear, insidePolygon, insideShape, footprintSolid, decodeMaterialGrid, materialBlocks, materialGridFromRGBA, navCellSize, navGrid, cellCenter, cellValid, originCell, flood, smoothPath, pathFromFlood, findPath, moveWithCollision, advanceAlongPath, portalDistance, insideTrigger, inPortalZone, exitCandidate, createActor, arrive, releaseLatches, stepActor, returnSpawn, drawKey, compareDrawKeys, sortForDrawing, actorDrawIndex, portalArrivals, traverseRoutes, runtimeSnapshot.

## 2. SKILL.md routing rows

generate2dmap, routing table:

| Need | Route |
|---|---|
| Walk a playable map, check that every exit, interaction and approach point is reachable, and keep the evidence | `scripts/build_scene_preview.py --bundle <map_bundle.json> --output-dir <new>`; open preview.html from disk, press Check routes (or add `--verify`), attach the `window.__scene` snapshot or scene-snapshot.json; read `references/scene-preview.md` |
| Collision, click-to-walk paths and exits in a web game that agree with the map tools | import `references/runtime/map-runtime.mjs` (`createMapRuntime`, `isBlocked`, `segmentClear`, `findPath`, `stepActor`); header comment states the Appendix C rules |

generate2dmap, processing tools table:

| Script | Purpose / limits |
|---|---|
| `scripts/build_scene_preview.py` | One self-contained preview.html (data-URI art, inlined map-runtime.mjs): Y-sorted objects and a debug actor walking the bundle's Appendix C collision, debug overlay, route check, `window.__scene` snapshot; optional headless-Chromium `--verify`; a debug marker, not character art; no engine import |

Acceptance bullet (replaces "Traverse routes, portals and camera extremes when integrating"): "Before handing over a playable map, run the route check in build_scene_preview (button, `window.__scene.traverseAll()` or `--verify`) and attach the snapshot; every route must say `ok: true`."

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `build_scene_preview.py` | Builds one self-contained, deterministic preview.html (at most 16 MB, no external URLs) where a debug actor walks the bundle's collision, with a debug overlay, route check and `window.__scene` snapshot | `tests/test_scene_preview.py` (byte-identical rebuild, tile-layer pixels, draw order, material grid, hostile-data escaping, external-reference scan, CLI conventions; opt-in headless Chromium run) |
| `references/runtime/map-runtime.mjs` | Shared JS collision query, grid path finder, budget-carrying walker and intent exits with arrival latches (plan Appendix C) | `tests/js/map-runtime.test.mjs` under node (blocked walker stays put, budget never exceeded, no zero-motion ticks, inward never exits) and `tests/test_map_runtime_js.py` (differential check against a numpy reading of Appendix C on 4 fixtures, about 480,000 point queries) |

## 4. CHANGELOG entries

- Added: `references/runtime/map-runtime.mjs` 1.0.0, a dependency-free ES module: Appendix C validity (walk regions with holes, rect/ellipse/polygon solids, collision rects, object footprints scaled once, material classes), `segmentClear`, a 4-neighbour grid search with clear edges, path smoothing, `advanceAlongPath` that carries the movement budget across waypoints, keyboard moves that slide and stop, intent and crossing exits with arrival latches, a route check (`traverseRoutes`) and `runtimeSnapshot` (B17-T1).
- Added: `scripts/build_scene_preview.py`: a single-file preview.html with data-URI art, tile layers pre-rendered from tileset.v1 manifests, ground-line Y-sort `(sortY, x, id)` with the actor in front on ties, debug overlay, click-to-walk, `window.__scene` (snapshot, route check, Y-sort probe, scripted input), a QA envelope, a 16,000,000-byte gate, a scan for anything that loads from outside the page, and `--verify` in headless Chromium through playwright (SKIPPED when missing) (B17-T2).
- Added: `references/scene-preview.md`, what the preview proves and what it does not (B17-T3).
- Changed: none.
- BREAKING: none (new files only).
- Fixed: none; plan Appendix J assigns no audit finding ids to B17.

## 5. Schema change requests

All additions go to `shared/schemas/map.schema.json` (vendored into generate2dmap and codeart2d). They are additive: every existing fixture stays valid. `tests/test_scene_preview.py::SCHEMA_PATCH` applies exactly this object in memory (`patched_errors`) and the tests validate the bundles, reports and snapshots against it; integration can then use `assert_valid_contract(..., "map", "scene_snapshot_v1")` and drop the helper. The object maps a JSON pointer inside map.schema.json to the fragment to insert there (none of the pointers exist today):

```json
{
  "/$defs/mapObject/properties/image": {"$ref": "common.schema.json#/$defs/relPath", "description": "The object's art when it is not found through its prop label; drawn with anchor_px at (x, y)."},
  "/$defs/mapObject/properties/image_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
  "/$defs/map_bundle_v2/properties/name": {"type": "string", "minLength": 1},
  "/$defs/map_bundle_v2/properties/prop_packs": {"description": "Prop-pack manifests whose accepted labels name the objects' prop.", "type": "array", "items": {"type": "object", "required": ["manifest"], "properties": {"id": {"type": "string", "minLength": 1}, "manifest": {"$ref": "common.schema.json#/$defs/relPath"}, "sha256": {"$ref": "common.schema.json#/$defs/sha256"}}}},
  "/$defs/routeCheck": {"description": "map-runtime.mjs traverseRoutes(): every exit, interaction, approach point and slot walked from the first spawn that reaches it, with the per-tick walker.", "type": "object", "required": ["ok", "speed", "budget", "tickHz", "cell", "spawns", "results", "portals"], "properties": {"ok": {"type": "boolean"}, "speed": {"type": "number", "exclusiveMinimum": 0}, "budget": {"type": "number", "exclusiveMinimum": 0}, "tickHz": {"type": "integer", "minimum": 1}, "cell": {"type": "integer", "minimum": 1}, "spawns": {"type": "array", "items": {"type": "object", "required": ["id", "valid", "reachableCells"], "properties": {"id": {"type": "string"}, "valid": {"type": "boolean"}, "reachableCells": {"type": "integer", "minimum": 0}}}}, "results": {"type": "array", "items": {"type": "object", "required": ["target", "kind", "from", "ok", "reachable", "reason"], "properties": {"target": {"type": "string", "minLength": 1}, "kind": {"enum": ["exit", "interaction", "approach", "slot"]}, "from": {"type": ["string", "null"]}, "ok": {"type": "boolean"}, "reachable": {"type": "boolean"}, "reason": {"type": ["string", "null"]}, "ticks": {"type": "integer", "minimum": 0}, "distance": {"type": "number", "minimum": 0}, "maxStep": {"type": "number", "minimum": 0}, "zeroMotionTicks": {"type": "integer", "minimum": 0}, "blocked": {"type": "boolean"}, "fired": {"type": "boolean"}, "inwardFired": {"type": "boolean"}, "firedEnRoute": {"type": "array", "items": {"type": "string"}}, "end": {"anyOf": [{"type": "null"}, {"$ref": "common.schema.json#/$defs/point2"}]}}}}, "portals": {"type": "array", "items": {"type": "object", "required": ["id", "activation", "arrivals"], "properties": {"id": {"type": "string"}, "activation": {"enum": ["crossing", "intent"]}, "to": {"type": "string"}, "arrivals": {"type": "array", "items": {"type": "object", "required": ["from", "spawn", "found"], "properties": {"from": {"type": "string"}, "spawn": {"type": "string"}, "found": {"type": "boolean"}, "insideTrigger": {"type": ["boolean", "null"]}, "inZone": {"type": ["boolean", "null"]}, "latchedOnArrival": {"type": ["boolean", "null"]}}}}}}}}},
  "/$defs/scene_snapshot_v1": {"description": "window.__scene.snapshot() of a build_scene_preview page, scene-snapshot.json from --verify, or map-runtime.mjs runtimeSnapshot(): world, actor, events and the route check when it ran.", "type": "object", "required": ["schema", "runtime", "world", "actor"], "properties": {"schema": {"const": "generate2dmap.scene_snapshot.v1"}, "runtime": {"$ref": "common.schema.json#/$defs/toolInfo"}, "world": {"type": "object", "required": ["width", "height", "actorRadius", "ySquash", "cell", "tickHz"], "properties": {"width": {"type": "number", "exclusiveMinimum": 0}, "height": {"type": "number", "exclusiveMinimum": 0}, "actorRadius": {"type": "number", "minimum": 0}, "ySquash": {"type": "number", "exclusiveMinimum": 0}, "cell": {"type": "integer", "minimum": 1}, "tickHz": {"type": "integer", "minimum": 1}}}, "actor": {"anyOf": [{"type": "null"}, {"type": "object", "required": ["x", "y", "valid", "latched"], "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "valid": {"type": "boolean"}, "facing": {"$ref": "common.schema.json#/$defs/point2"}, "latched": {"type": "array", "items": {"type": "string"}}, "travelled": {"type": "number", "minimum": 0}, "pathLength": {"type": "integer", "minimum": 0}}}]}, "ready": {"type": "boolean"}, "error": {"type": ["string", "null"]}, "tick": {"type": "integer", "minimum": 0}, "source": {"type": "object", "properties": {"file": {"type": "string"}, "sha256": {"$ref": "common.schema.json#/$defs/sha256"}, "schema": {"type": "string"}}}, "events": {"type": "array", "items": {"type": "object", "required": ["tick", "type"], "properties": {"tick": {"type": "integer", "minimum": 0}, "type": {"enum": ["arrive", "exit", "reach", "interact", "teleport", "routes"]}}}}, "exitsFired": {"type": "array", "items": {"type": "string"}}, "interactionsReached": {"type": "array", "items": {"type": "string"}}, "drawOrder": {"type": "array", "items": {"type": "string"}}, "routes": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/routeCheck"}]}}},
  "/$defs/scene_preview_qa_v1": {"description": "preview-qa.json of build_scene_preview.py: a QA envelope plus what the page contains.", "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}], "required": ["schema", "preview", "warnings", "verify"], "properties": {"schema": {"const": "generate2dmap.scene_preview_qa.v1"}, "preview": {"type": "object", "required": ["file", "bytes", "sha256", "runtime", "world", "start", "drawOrder", "layers"], "properties": {"file": {"$ref": "common.schema.json#/$defs/relPath"}, "bytes": {"type": "integer", "minimum": 1}, "sha256": {"$ref": "common.schema.json#/$defs/sha256"}, "runtime": {"type": "object", "required": ["version", "sha256"], "properties": {"sha256": {"$ref": "common.schema.json#/$defs/sha256"}}}, "world": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}, "drawOrder": {"type": "array", "items": {"type": "string"}}, "layers": {"type": "array", "items": {"type": "object", "required": ["name", "source"], "properties": {"source": {"enum": ["image", "tiles", "objects"]}}}}}}, "warnings": {"type": "array", "items": {"type": "string"}}, "verify": {"type": "object", "required": ["status"], "properties": {"status": {"enum": ["not-requested", "SKIPPED", "pass", "fail"]}}}}}
}
```

Reasons, producers and consumers:

- `mapObject.image`, `mapObject.image_sha256`: an object's art when no prop pack names it. The schema gave objects only a `prop` label and `occluder.source`, so neither the preview nor export_tiled (B13-T3, "prop image collection") had a defined way to find a prop's picture. Producers: agents, B21 layout_build. Consumers: B17, B13 export_tiled, B14 exporters.
- `map_bundle_v2.prop_packs[{id?, manifest, sha256?}]`: prop-pack manifests whose accepted `label` names `objects[].prop`. Producers: agents, B21. Consumers: B17 (`--prop-pack` adds more at the command line), B13, B14.
- `map_bundle_v2.name`: a display name; the preview uses it as the page title (it also accepts `title`). Producers: agents, B21.
- `routeCheck`, `scene_snapshot_v1`: the `window.__scene.snapshot()` / `scene-snapshot.json` document and the `traverseRoutes()` result inside it. Producer: B17 (page, runtime, `--verify`). Consumers: agents attaching evidence (Map SKILL.md acceptance), Z docs, the integration e2e test.
- `scene_preview_qa_v1`: preview-qa.json, a common `qaEnvelope` plus `schema`, `preview`, `warnings` and `verify`. Producer: B17. Consumers: agents, Z.

Optional fields B17 reads that already validate (objects are open) and that B13's validator should know: `objects[].occluder.source` doubles as the art fallback; `footprint.basis` `"world_px"` means the footprint is not scaled by `scale` (any other value, including `image_px` and the `prop_px` of the prop_pack_v2 example, is scaled); spawns' `facing` (a compass word or degrees) only orients the marker.

## 6. Shared-helper promotion requests

Into forge_core (A1 owns `shared/forge_core.py`):

- `_local_file_ref(path, base_dir, sha256=None) -> dict` at build_scene_preview.py:874. Same request as B12's: `forge_core.portable_path` returns an absolute POSIX path across drives, which a manifest must not store, so the helper falls back to the file name. Proposed `forge_core.file_ref(path, base_dir, sha256=None) -> {"path", "sha256", "bytes"}`. Tested by `ContractTests.test_report_is_a_qa_envelope_and_a_scene_preview_qa` (relative input paths, output sha256).
- `_local_png_bytes(pixels) -> bytes` at build_scene_preview.py:885: `forge_core.save_png` into memory (same encoder settings, RGB zeroed under alpha 0, no metadata), for embedding a rendered image without a temporary file. Proposed `forge_core.png_bytes(img) -> bytes`, with `save_png` written on top of it. Tested by `PageTests.test_tile_layers_are_rendered_from_the_tileset_exactly` and `test_output_is_byte_identical_across_runs`.

Into map_nav.py (B13, same skill; not forge_core):

- `material_blocks(material_class, walkable)` (build_scene_preview.py:1402) and the classification inside `compile_material_grid` (build_scene_preview.py:1407: exact RGB on alpha > 0, palette or grey `index`, ambiguous matches refused, `cellWidth = world.width / image.width`): map_nav's material rasterisation and the preview must classify pixels the same way. Keep one implementation (map_nav's, if it exists by then) and let build_scene_preview import it from the same scripts folder. Tests: `PageTests.test_material_map_becomes_a_blocked_cell_grid`, `test_palette_material_maps_match_by_index`, `AppendixCParityTests.test_material_grid_from_pixels_matches_the_preview_builder`.
- `AppendixC` (tests/test_map_runtime_js.py:111) is a test oracle, not a helper: integration replaces it with map_nav's validity query to get the planned JS/Python parity test (section 7).

## 7. Cross-module links that Z must add

- generate2dmap SKILL.md (Z): the routing and tools rows of section 2; link references/scene-preview.md; add the acceptance bullet.
- layered-map-contract.md (B13): link scene-preview.md for walking and route evidence; document `objects[].image`, `prop_packs`, footprints that block unless `solid: false`, and point to the Appendix C rules in the map-runtime.mjs header (the one place they are written as code). map-strategies.md (B13) may name the preview as the check after export_tiled.
- Integration, parity (plan Appendix I): replace `AppendixC` in tests/test_map_runtime_js.py with map_nav.py's query and keep the four fixtures and the lattice; exempt only points within 1e-9 of a rotated shape's edge (`near_rotated_edge`, see section 8). Also compare reachability: `flood()` cell sets against map_nav's BFS on the same fixtures.
- Integration, e2e map pipeline: `... -> compose --debug-overlay -> build_scene_preview` (no `--verify` in CI unless Playwright is installed); assert `status` pass and run the route check in node with the snapshot test pattern of `ContractTests.test_runtime_snapshot_and_route_check_match_the_requested_contract`.
- Integration: apply section 5 and run `tools/vendor_sync.py --write`.
- B21 layout_build: write art references (`objects[].image` or `prop_packs`) so previews show the props, and keep walls in `collision` (rects or solids): Appendix C does not read tile collision.
- B12 compose: both tools sort by `(sortY or ground line, x, id)`; keep it that way (B12 section 7 asked for this).
- CONTRIBUTING (Z): `tests/js/*.test.mjs` run under `node --test`; pytest collects them through tests/test_map_runtime_js.py (pytest.ini already skips tests/js).
- README requirements (Z): `--verify` is optional and needs node plus `npm install playwright` and `npx playwright install chromium`.

## 8. Known limitations and what is not proven

Appendix C ambiguities and how they were resolved (B13's map_nav must decide the same way for parity; every rule is also written in the map-runtime.mjs header):

1. Walk regions are a union; a region's holes cut only that region, so a point in region A's hole but inside region B is walkable.
2. Without walk regions the bounds are half-open: `0 <= x < width`, `0 <= y < height`.
3. Rect solids and `collision.rects` are half-open (`x <= px < x + w`). `collision.rects` are blocked geometry (merged blocked cells), tested like rect solids and never inflated by the actor radius.
4. Ellipses block strictly inside (`nu*nu + nv*nv < 1`); `rx` or `ry` of 0 never blocks; `rotate` is degrees, clockwise on screen, `theta = rotate * PI / 180` (not a precomputed degree factor).
5. Polygons (solids, walk regions, holes) use the even-odd crossing rule with `px < xi + (py - yi) * (xj - xi) / (yj - yi)`.
6. The 8 footprint samples sit at exact unit directions E, SE, S, SW, W, NW, N, NE (`sqrt(0.5)` on diagonals, no cos/sin), at `(x + r*ux, y + (r*ySquash)*uy)`; `ySquash` defaults to 1 even for plates (producers write 0.58).
7. Lengths are `sqrt(dx*dx + dy*dy)`, never `hypot` (engines differ in the last bit).
8. `cell = max(1, floor(r / 2 + 0.5))`: Appendix C's round is half up. Python's `round()` would give 2 for r = 5; this gives 3.
9. The grid covers the world rectangle (`ceil(width / cell)` columns); cell centres are `((i + 0.5) * cell, (j + 0.5) * cell)`; a bundle's `nav.cell` is not used.
10. Grid moves need both centres valid and `segmentClear` between them (the midpoint is sampled). Appendix C only says "4-neighbour moves"; without the edge test the search jumps walls thinner than a cell (the thin-gap rule of B13-T2).
11. `segmentClear`: `n = max(1, ceil(len / (cell / 2)))`, samples `a + d * k / n` for k = 0..n, ends included.
12. A footprint blocks unless the object has `solid: false` or shape `none` (the same default as B12's compose audit). `basis: "world_px"` is not scaled; every other basis is scaled by `scale`, offset included. An unrotated rect footprint is the half-open rect from `cx - w / 2`; a rotated one is its corner polygon.
13. Materials: `solid` blocks even with `walkable: true`; `one_way` never blocks the top-down walker (its "from above" rule is side-scroll physics, B14); colours match exact RGB where alpha > 0; `index` reads palette or grey images; a pixel matching two materials is an error; the grid cell is `world.width / image.width` by `world.height / image.height` (unequal sizes warn).
14. Exits: the 0.25 test is a cosine (both vectors normalised); "within radius" is the distance from the actor to the trigger shape (0 inside it); `activation` defaults to crossing; crossing fires while inside the trigger (rect half-open, circle open) with a non-zero intent unless `requiresMovement` is false (it has no effect on intent exits, which need input anyway); `latch` defaults to true and latches on arrival inside the portal's zone (radius zone for intent, trigger for crossing); a fired portal stays latched until the actor leaves the zone, whatever `latch` says; ties go to the nearest trigger, then bundle order.
15. `entranceByFrom` maps a source map id to a spawn in this map; after an exit fires the page returns the actor through `entranceByFrom[<map id before ':' in to>]` when that spawn exists (arrival latches apply), else it stays latched where it fired.
16. An interaction without `reach` uses `6 x actorRadius`.
17. Route check: a target passes when one spawn reaches it (the union, as a BFS from all spawns would); a spawn that latches its own exit walks out of the zone first; `anchors[].approach` may be one point or a list; slots may be `[x, y]`, `{x, y}` or `{point}`.
18. Tile `collision` shapes and `properties.walkable` in tileset manifests are not read: Appendix C lists walk regions, solids, footprints and material classes only.

Measured behaviour and limits:

- Rotated shapes: V8's `Math.sin(PI / 4)` is 0.7071067811865475 and the Windows C runtime's `math.sin` gives ...476, so JS and CPython can put a point exactly on a rotated edge on different sides. On the four fixtures (119,808 lattice points each) the only differences were 4 point queries and 12 footprint queries, all on the boundary of a 45-degree rotated circle; everything else matched bit for bit. The parity test exempts points within 1e-9 of rotated edges and asserts they are under 0.2 percent. CPython on Linux (glibc) can differ from CPython on Windows the same way.
- Validity is sampled, so a solid thinner than `cell / 2` can be crossed by a segment, and a tick position between two samples can fail the point test. Planned paths are therefore walked as planned (waypoints flagged `clear: true`); without that, a random 1920 x 1080 plate with 60 props stalled the walker on 1 of 11 routes. Keyboard moves and unflagged waypoints are always collision-checked.
- `--verify` was run here only with playwright 1.62.1 and Chromium 151 from a local Codex runtime (`NODE_PATH`), on Windows 11: all six browser checks pass on the test scene. In STD that test skips (no resolvable playwright); the SKIPPED paths are tested. Firefox, WebKit, mobile and touch are not run.
- Speed (node 22, this machine): a 640 x 480 map with r = 6 (34,240 cells, 500 solids) floods in about 50 ms and runs its route check in about 40 ms; a 1920 x 1080 plate with r = 24 and 60 props in about 40 ms and 20 ms. `actorRadius` 0 makes a 1-pixel grid: a 4096 x 4096 world would hold 16.7 million cells.
- 16 MB is read as 16,000,000 bytes (stricter than MiB). Images are embedded as their original bytes (PNG, JPEG, WebP, GIF); other formats are re-encoded to PNG.
- Platforms: Windows 11, Python 3.13, Pillow 12.3, numpy 2.5, node 22.15. macOS, Linux, Python 3.10 and Pillow 10.1 were not run.
- Not proven: parity with map_nav.py (B13 had no commits when this module was built, so the oracle encodes this module's reading of Appendix C); engine imports; character art, animation, lights and per-pixel occlusion (whole-object Y-sort only); gameplay after an exit fires.
- Deviations from the plan text, all additive: the page simulates the round trip through `entranceByFrom`; `--verify` also checks network requests, asset loading, keyboard movement, the route check and a Y-sort pixel probe, and writes the snapshot and two screenshots; three schema additions and two new document types (section 5) give the snapshot and report a contract; `--prop-pack` and the bundle's `prop_packs` resolve prop art.
