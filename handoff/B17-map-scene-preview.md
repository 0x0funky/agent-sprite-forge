# B17-map-scene-preview: single-file playable scene preview (build_scene_preview.py) and the shared JS collision query (map-runtime.mjs)

Branch `asf/B17-map-scene-preview` (from `wip/asf-upgrade-20261005` @ 3f9252d), integrated on `asf/integration` and revised in the Phase 3 group pass `asf/int-g-map-scene`. Files:
[build_scene_preview.py](../skills/generate2dmap/scripts/build_scene_preview.py) (B17-T2),
[map-runtime.mjs](../skills/generate2dmap/references/runtime/map-runtime.mjs) (B17-T1),
[scene-preview.md](../skills/generate2dmap/references/scene-preview.md) (B17-T3),
[tests/test_scene_preview.py](../tests/test_scene_preview.py) (40 tests, 1 skipped unless Playwright is resolvable),
[tests/test_map_runtime_js.py](../tests/test_map_runtime_js.py) (10 tests, node marker),
[tests/js/map-runtime.test.mjs](../tests/js/map-runtime.test.mjs) (31 node:test cases).

## Phase 3 integration (what changed, and why)

All resolved items cite the integration decisions (`.tmp/claude-audit-20261005/plan/planner/integration-decisions.md`).

- **One collision model (D1, D2, D33).** map-runtime.mjs 1.1.0 mirrors `scripts/forge_nav.py` (shared/forge_nav.py, rules N1-N15) rule for rule: solids are closed sets (rect `x <= px <= x + w`, ellipse `<= 1`, polygon interior or edge), the walk area without walk regions is the closed box `[0, W] x [0, H]`, shapes without area are dropped, rotations use `rotate * (PI / 180)`, footprints come from the object else from the resolved `props` registry (basis checked, `flip_x` mirrors them), the material grid carries BLOCK and ONE_WAY planes on whole-pixel squares, `segmentClear` adds the one_way rule and the thin-gap rule, grid moves are directed (S and N differ through one_way), a start joins every valid node within two cells, and route verdicts follow N14 (interactions without `reach` are point targets; triggers are closed; crossing exits may be entered at the trigger's closest point). The 18 private resolutions of the Wave B handoff are superseded by forge_nav's rules.
- **Tile collision (D5, D33).** build_scene_preview reads collision through `forge_nav.blocking_set_from_document` and converts the placed tiles' per-tile collision (tileset_v1 `tiles[].collision`; a `walkable: false` tile without shapes blocks its cell) into world solids in `scene.bundle.collision.solids` (source `tiles:<layer>`), so the runtime needs no file access. Material maps are classified by forge_nav (an unclassified opaque pixel or a fractional scale now refuses the bundle, as map_nav does).
- **Prop art and flip_x (D6).** Lookup order: `objects[].image`, the bundle's `props[prop]` (inline or pack + label), prop packs by label (the bundle's `prop_packs`, then `--prop-pack`), `occluder.source`. `flip_x` objects are drawn mirrored around their anchor (also in the Y-sort probe) and their footprints mirror with them. The report's `objectArt` gains the `props` source.
- **Exit codes (D26, D27).** A failed `--verify` without `--strict` publishes the report with status `fail` and exits 1 (stderr names the failed checks; the summary gains `failed`). Usage errors use argparse's exit 2. `main()` runs through `forge_core.run_cli` (in-process calls too), so `Image.DecompressionBombError` and any unexpected exception print one `error:` line.
- **Conventions (D28-D30).** Bundle, pack, tileset and tile-data JSON is read with `forge_core.parse_json(strict=True)` (BOM tolerated; NaN, Infinity, duplicate keys refused); `tool.version` is `0.4.0`; `forge_core.file_ref` replaced `_local_file_ref`.
- **Reviewer fixes (Wave B review, group map-engines-scene).** Blocking: the route check ignored tile collision (D33, D5; fixed above; on B14's fixture the page and forge_nav now both report spawn-west blocked and `sign`, `well.slot[0]`, `well.slot[1]` unreachable, tested). Non-blocking: `--verify` exit code (D26); DecompressionBombError traceback (D27); tile indices below -1 are now refused (`-1` or `null` is empty; D2, as forge_nav reads tiles); the material map without colour or index is refused consistently with forge_nav (D2).
- **Evidence (D1, D2, D6).** The reviewer's parity probe (11 boundary points) now agrees with forge_nav on every point (it disagreed on 6). The reviewer's B21 small_spec repro: build_scene_preview passes with all 12 props drawn from `props` (8 mirrored) and a passing route check.

## 1. CLIs

Run from the user's project root; `<skill-dir>` is the generate2dmap folder (`${CLAUDE_SKILL_DIR}` in Claude Code). One verb, so no subcommands.

    python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json --output-dir map/qa/preview
    python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json --prop-pack assets/props/prop-pack.json --output-dir map/qa/preview-2 --verify --strict
    python "<skill-dir>/scripts/build_scene_preview.py" --bundle scene/stage-bundle.json --spawn hero --speed 240 --zoom 1 --output-dir scene/qa/preview

- Flags: `--bundle` (required), `--output-dir` (required, must not exist), `--prop-pack MANIFEST` (repeatable), `--spawn ID`, `--speed PX_PER_S` (default 12 x actorRadius, at least 60), `--actor-height PX` (default 4.5 x actorRadius, at least 12), `--zoom 1-8` (default the largest of 1-4 that keeps the world within 1280 px), `--title`, `--max-bytes` (default 16000000), `--verify`, `--verify-timeout` (120 s), `--strict`.
- Outputs in the new folder: `preview.html` and `preview-qa.json`; with a successful `--verify` also `scene-snapshot.json`, `preview-screen.png` and `preview-debug.png`. Work happens in `forge_core.staged_output`; a failed gate leaves nothing behind.
- Success prints one ASCII JSON line: `status` (pass, warn or fail), `output_dir`, `preview`, `metadata` (preview-qa.json), `bytes`, `verify` (not-requested, SKIPPED, pass, fail), `warnings` (count) and `failed` (ids of failed checks).
- Exit codes (D26, D27): 0 pass or warn; 1 when the report was published with status `fail` (a failed `--verify` without `--strict`) or when nothing was published (one `error: ...` line; a failed QA gate says `error: QA failed, nothing published: ...`; a bundle forge_nav cannot read says `error: collision (forge_nav): ...`); 2 on a usage error (argparse).
- `--verify` without node, the playwright npm package or its Chromium prints `verify: SKIPPED (<reason>)` on stderr and still exits 0 (even with `--strict`). Playwright is found by node's normal resolution from the current directory plus `NODE_PATH`.
- `--help` is ASCII and exits 0 under cp1252 and cp950 (tested); its epilog holds two single-line examples.
- Library entry points (same skill): `build_preview(args)`, `clean_collision`, `clean_portals`, `compile_objects`, `PropRegistry`, `PropPacks`, `compile_layers`, `render_tile_layer`, `read_blocking_set`, `tile_solids_for_runtime`, `compile_material_grid`, `script_json`, `external_references`.

Runtime (a JavaScript module, not a CLI): `import {createMapRuntime, isBlocked, segmentClear, findPath, stepActor} from "./map-runtime.mjs";`. Exports: RUNTIME_VERSION ("1.1.0"), TICK_HZ, INTENT_MIN_COS, SNAPSHOT_SCHEMA, FREE, BLOCK, ONE_WAY, MAX_GRID_NODES, MATERIAL_CLASSES, FOOTPRINT_BASES, FOOTPRINT_DIRECTIONS; geometry `insidePolygon`, `onPolygonEdge`, `insideShape`, `footprintSolid(x, y, footprint, {scale, flipX, source})` (forge_nav `footprint_solid`), `objectSolid(object, prop, where)` (forge_nav `object_solid`), `footprintOffsets`; materials `decodeMaterialGrid`, `materialCode`, `materialBlocks`, `materialGridFromRGBA`, `materialAt`; the world `createMapRuntime`, `navCellSize`; queries `inWalkArea`, `blockedAt`, `pointFree` (forge_nav `centre_ok`), `footprintSamples`, `isValid`, `isBlocked`, `segmentBreaks`, `segmentStatus`, `segmentClear(world, ax, ay, bx, by, {thinGap})`; grid `navGrid`, `cellCenter`, `cellValid`, `moveOpen`, `joinCells`, `originCell`, `floodFrom`, `flood`; N14 targets `pointTarget`, `reachTarget`, `portalDistance`, `closestTriggerPoint`, `exitTarget`; paths and walking `smoothPath`, `pathFromFlood`, `findPath`, `moveWithCollision`, `advanceAlongPath`; exits `insideTrigger`, `inPortalZone`, `exitCandidate`, `createActor`, `arrive`, `releaseLatches`, `stepActor`, `returnSpawn`; drawing `drawKey`, `compareDrawKeys`, `sortForDrawing`, `actorDrawIndex`; route check `routeStarts`, `portalArrivals`, `traverseRoutes`, `runtimeSnapshot`. Changed in 1.1.0: `footprintSolid` takes `(x, y, footprint, options)` (the object form is `objectSolid`); `decodeMaterialGrid` returns `{width, height, scale, codes, hasOneWay}`; `materialGridFromRGBA` returns `bits` and `oneWay` planes and refuses unmatched pixels and fractional scales; `returnSpawn` also returns point arrivals.

## 2. SKILL.md routing rows

generate2dmap, routing table:

| Need | Route |
|---|---|
| Walk a playable map, check that every exit, interaction and approach point is reachable, and keep the evidence | `scripts/build_scene_preview.py --bundle <map_bundle.json> --output-dir <new>`; open preview.html from disk, press Check routes (or add `--verify`), attach the `window.__scene` snapshot or scene-snapshot.json; read `references/scene-preview.md` |
| Collision, click-to-walk paths and exits in a web game that agree with the map tools | import `references/runtime/map-runtime.mjs` (`createMapRuntime`, `isBlocked`, `segmentClear`, `findPath`, `stepActor`); it mirrors `scripts/forge_nav.py` rules N1-N15 (its header lists them); pass tile collision as `collision.solids` and resolve `props` pack items first (build_scene_preview does both) |

generate2dmap, processing tools table:

| Script | Purpose / limits |
|---|---|
| `scripts/build_scene_preview.py` | One self-contained preview.html (data-URI art, inlined map-runtime.mjs): Y-sorted objects (D6 art lookup, `flip_x` mirrored) and a debug actor walking the forge_nav blocking set (solids, rects, footprints, tile collision, material classes incl. one_way), debug overlay, route check, `window.__scene` snapshot; optional headless-Chromium `--verify` (exit 1 when it fails); a debug marker, not character art; no engine import |

Acceptance bullet (replaces "Traverse routes, portals and camera extremes when integrating"): "Before handing over a playable map, run the route check in build_scene_preview (button, `window.__scene.traverseAll()` or `--verify`) and attach the snapshot; every route must say `ok: true`. The route check and map_nav check read the same forge_nav blocking set."

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `build_scene_preview.py` | Builds one self-contained, deterministic preview.html (at most 16 MB, no external URLs) where a debug actor walks the bundle's collision (the forge_nav blocking set, tile collision included), with a debug overlay, route check and `window.__scene` snapshot | `tests/test_scene_preview.py` (byte-identical rebuild, tile-layer pixels, tile collision as world solids, draw order, D6 art lookup and flip_x, material BLOCK and one_way planes, the route check agreeing with forge_nav on B14's fixture, hostile-data escaping, external-reference scan, exit codes; opt-in headless Chromium run) |
| `references/runtime/map-runtime.mjs` | Shared JS collision query that mirrors forge_nav (rules N1-N15), grid path finder, budget-carrying walker and intent exits with arrival latches | `tests/js/map-runtime.test.mjs` under node (31 cases: closed shapes, one_way, thin gaps, footprints and flip_x, N14 targets, blocked walker stays put, budget never exceeded, no zero-motion ticks, inward never exits) and `tests/test_map_runtime_js.py` (reference comparison with forge_nav on 5 fixtures: every lattice point, random segments with and without the thin-gap rule, grid nodes and moves, BFS distances and N14 target verdicts) |

## 4. CHANGELOG entries

- Added: `references/runtime/map-runtime.mjs` 1.1.0, a dependency-free ES module that mirrors forge_nav's collision rules N1-N15 rule for rule (closed solids, the closed world box, walk regions with holes, collision rects, object footprints scaled once with basis and flip_x, the props registry, material classes with one_way, the thin-gap rule), a 4-neighbour grid search with directed moves, path smoothing, `advanceAlongPath` that carries the movement budget across waypoints, keyboard moves that slide and stop, intent and crossing exits with arrival latches, a route check (`traverseRoutes`, N14 verdicts) and `runtimeSnapshot` (B17-T1; D1, D2).
- Added: `scripts/build_scene_preview.py`: a single-file preview.html with data-URI art, tile layers pre-rendered from tileset.v1 manifests, collision read through forge_nav (tile collision converted into world solids, material classes into BLOCK and one_way planes), D6 prop-art lookup with mirrored `flip_x` props, ground-line Y-sort `(sortY, x, id)` with the actor in front on ties, debug overlay, click-to-walk, `window.__scene` (snapshot, route check, Y-sort probe, scripted input), a QA envelope, a 16,000,000-byte gate, a scan for anything that loads from outside the page, and `--verify` in headless Chromium through playwright (SKIPPED when missing; a failed run exits 1) (B17-T2; D2, D5, D6, D26, D33).
- Added: `references/scene-preview.md`, what the preview proves and what it does not (B17-T3).
- Changed: none. BREAKING: none (new files only).
- Fixed: none with audit ids (plan Appendix J assigns none to B17); the Wave B review's blocking finding (the route check ignored tile collision) is fixed before release.

## 5. Schema change requests

Status: applied by the shared stage S1 (commit f3d7eb2) in `shared/schemas/map.schema.json` and vendored, with the `prop_packs` and `mapObject.image` fragments merged with B14's per D8 (B17's `prop_packs` superset with optional `id`; the D6 lookup order is in the descriptions). The tests validate against the vendored schemas (`assert_valid_contract` and `contract_errors`, D33); `tests/test_scene_preview.py::SCHEMA_PATCH` is gone.

Optional fields this module now writes beyond the listed properties (every object involved is open, so no schema change is needed; recorded per Appendix B):

- `scene_snapshot_v1.world.oneWay` (boolean: the map has one_way material).
- `routeCheck.spawns[]`: arrivals given as `[x, y]` points are listed after the spawns with `kind: "arrival"` and the id `<portal id><-<source map>`.
- `routeCheck.portals[].arrivals[]`: `valid`, `joined` (the arrival reaches a grid node), `bounceBack` (ids of every portal whose closed trigger holds it) and, for point arrivals, `point`; for point arrivals `spawn` holds the arrival id.
- `scene_preview_qa_v1.preview.collision` `{collisionSolids, rects, footprints, tileSolids, blockingMaterialClasses}`; `preview.materialMap` now `{size, cell, blockedCells, oneWayCells, materials[{name, class, walkable, blocks, oneWay}]}` (the unclassified count is gone: unclassified pixels refuse the bundle); `preview.objectArt.props`.
- Summary line: `failed`.

## 6. Shared-helper promotion requests

- `_local_file_ref`: resolved; `forge_core.file_ref` is used (D30).
- `material_blocks` / `compile_material_grid`: resolved; material classification is forge_nav's (`blocking_set_from_document`, `material_codes`), D2/D4.
- The `AppendixC` test oracle: resolved; tests/test_map_runtime_js.py compares with forge_nav itself (D4).
- Still requested, into forge_core (A1 owns `shared/forge_core.py`): `_local_png_bytes(pixels) -> bytes` at build_scene_preview.py (`forge_core.save_png` into memory: same encoder settings, RGB zeroed under alpha 0, no metadata), proposed `forge_core.png_bytes(img)` with `save_png` written on top of it. Tested by `PageTests.test_tile_layers_are_rendered_from_the_tileset_exactly` and `test_output_is_byte_identical_across_runs`.
- Requested for the map-core group (B13, `map_bundle.py`): one D6 art resolver (`objects[].image`, `props[prop]` inline or pack + label, `prop_packs` by label, `occluder.source`) that build_scene_preview, export_godot/export_ldtk and export_tiled can import; today build_scene_preview and export_godot each implement the same order.

## 7. Cross-module links that Z must add

- generate2dmap SKILL.md (Z): the routing and tools rows of section 2; link references/scene-preview.md; add the acceptance bullet.
- layered-map-contract.md (B13): quote forge_nav's rules N1-N15 and name map-runtime.mjs as their JS mirror; link scene-preview.md for walking and route evidence; document `objects[].image`, `props`, `prop_packs`, the D6 order, footprints that block unless `solid: false`, and that one_way blocks downward moves on every map (N11).
- Integration, parity (plan Appendix I, the next stage): tests/test_map_runtime_js.py is the harness (5 fixtures, forge_nav as the reference); extend it to the 3+ e2e fixtures of D4 and keep the rotated-edge exemption.
- Integration, e2e map pipeline: `... -> compose --debug-overlay -> build_scene_preview` (no `--verify` in CI unless Playwright is installed); assert `status` pass and run the route check in node with the pattern of `ContractTests.test_runtime_snapshot_and_route_check_match_the_requested_contract`.
- B21 layout_build: it writes `props{label}` and `flip_x`, which the preview now reads (D6). See the double-mirroring note in section 8.
- B12 compose: both tools sort by `(sortY or ground line, x, id)`; keep it that way.
- CONTRIBUTING (Z): `tests/js/*.test.mjs` run under `node --test`; pytest collects them through tests/test_map_runtime_js.py (pytest.ini already skips tests/js).
- README requirements (Z): `--verify` is optional and needs node plus `npm install playwright` and `npx playwright install chromium`.

## 8. Known limitations and what is not proven

- Collision rules are forge_nav's (N1-N15 in shared/forge_nav.py). Arithmetic matches bit for bit except sin and cos of a rotation: V8's `Math.sin(PI / 4)` is 0.7071067811865475 and the Windows C runtime's is ...476, so a point exactly on a rotated edge can fall on different sides. The reference comparison exempts only points within 1e-9 of rotated edges (asserted under 0.2%); thin-gap cut positions are accurate, not bit-identical (forge_nav N10 allows this).
- one_way blocks moving down onto it on every map, as forge_nav's N11 says (bundles carry no view mode), although D2 reads "one_way blocks from above only, side-scroll only". A top-down map should not use one_way. Whether one_way should be inert on top-down maps (which needs a view mode in the bundle) is an open integrator question (raised by S3); the runtime, the preview and the exporters follow forge_nav until it changes.
- `prop_packs` are art only: they do not supply footprints (forge_nav N6). The `props` registry does.
- Double mirroring (cross-module, not fixed here): forge_nav mirrors any footprint of a `flip_x` object, including the object's own; B21 layout_build writes the object's own footprint already mirrored, and the map_bundle_v2 `mapObject` description (S1) says an object's own footprint is written as placed. On asymmetric footprints (offset x or rotate not 0) the preview, the runtime and forge_nav therefore mirror B21's footprints back. B21's current footprints are symmetric, so no shipped output is affected. The preview and runtime follow forge_nav (D2: one blocking set); if forge_nav changes to mirror only registry footprints, `objectSolid` in map-runtime.mjs must change with it.
- Footprint samples lie half a nav cell apart, so a solid thinner than that can fall between the samples of the footprint's rim (the centre path is exact). Planned paths are walked as planned (waypoints flagged `clear: true`); keyboard moves and unflagged waypoints are collision-checked with `segmentClear` per part.
- The preview refuses what forge_nav refuses: Tiled-style CSV tile data with a trailing comma, a tiles layer that does not cover the world, a material map with an unclassified opaque pixel or a fractional scale, a zero-area collision polygon. map_nav and map_bundle refuse the same.
- `--verify` was run here only with playwright 1.62.1 and Chromium 151.0.7922.34 from a local Codex runtime (`NODE_PATH`, no network), node 22.15, on Windows 11: `test_verify_walks_the_page_in_headless_chromium` passed after this pass; in STD that test skips (no resolvable playwright); the SKIPPED paths and the D26 exit code are tested (a failing browser run is simulated). Firefox, WebKit, mobile and touch are not run.
- Speed (node 22.15, this machine, runtime 1.1.0 with the thin-gap rule): a 640 x 480 map with r = 6 (34,240 cells, 500 solids: rects, rotated rects, rotated ellipses and triangles) flooded from a cold runtime in about 90 ms (115 ms on the first, unwarmed run; 2 ms once the cell and move caches are filled); the thin-gap cuts are found through a spatial index of boundary edges, so each grid move tests only the edges near it. `actorRadius` 0 makes a 1-pixel grid; grids above 2^24 nodes are refused (split the map).
- 16 MB is read as 16,000,000 bytes (stricter than MiB). Images are embedded as their original bytes (PNG, JPEG, WebP, GIF); other formats are re-encoded to PNG.
- Platforms: Windows 11, Python 3.13, Pillow 12.3, numpy 2.5, node 22. macOS, Linux, Python 3.10 and Pillow 10.1 were not run.
- Not proven: engine imports; character art, animation, lights and per-pixel occlusion (whole-object Y-sort only); gameplay after an exit fires.
