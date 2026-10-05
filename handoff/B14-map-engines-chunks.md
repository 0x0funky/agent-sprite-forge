# B14-map-engines-chunks: Godot 4 and LDtk map exporters, room-chunk socket validator, side-scroll layout grammar validator

Branch `asf/B14-map-engines-chunks` (from `wip/asf-upgrade-20261005` @ 3f9252d), integrated on `asf/integration` and revised in the Phase 3 group pass `asf/int-g-map-scene`. Files:
[export_godot.py](../skills/generate2dmap/scripts/export_godot.py),
[export_ldtk.py](../skills/generate2dmap/scripts/export_ldtk.py),
[validate_chunks.py](../skills/generate2dmap/scripts/validate_chunks.py),
[validate_layout.py](../skills/generate2dmap/scripts/validate_layout.py),
[engine-maps.md](../skills/generate2dmap/references/engine-maps.md) and the tests
[test_export_godot.py](../tests/test_export_godot.py) (21 tests),
[test_export_ldtk.py](../tests/test_export_ldtk.py) (13),
[test_validate_chunks.py](../tests/test_validate_chunks.py) (19) and
[test_validate_layout.py](../tests/test_validate_layout.py) (19).

Plan acceptance: B14-T1 `test_export_godot.py::ExportGodotTests::test_tres_parses_and_peering_bits` and
`::test_tile_map_data_roundtrip`; B14-T2 `test_export_ldtk.py::ExportLdtkTests::test_ldtk_required_fields` and
`::test_entities_positions_match_bundle`; B14-T3 `test_validate_chunks.py::...::test_width_mismatch_fails`,
`::test_two_by_two_layout_passes`, `::test_two_by_two_edge_graph_passes`; B14-T4
`test_validate_layout.py::...::test_three_row_deck_fails`, `::test_unjumpable_gap_fails`,
`::test_floating_prop_fails`; B14-T5 engine-maps.md (single-line commands; its two JSON examples are run by
`test_reference_doc_example_passes` in the validator tests).

## Phase 3 integration (what changed, and why)

All resolved items cite the integration decisions (`.tmp/claude-audit-20261005/plan/planner/integration-decisions.md`).

- **Solid footprints in Godot (D33, D2).** export_godot reads the D2 blocking set through `scripts/forge_nav.py` (`blocking_set_from_document`; a bundle without a collision block is read with actor radius 0) and writes every solid object footprint as a `footprint_<id>` child of the `collision` StaticBody2D: exactly the shape forge_nav blocks, scaled once by the instance scale (basis `world_px` not scaled), with offset, rotation and `flip_x` applied (rect and circle as CollisionShape2D, other ellipses as 32-gons, rotated rects as CollisionPolygon2D). The parse-back QA gains `collision_shapes_roundtrip`, which re-reads every collision shape (solids, rects, footprints) and compares position, size, radius or polygon points; `counts.footprints` reports them. Tile physics follow N7: a `walkable: false` tile without shapes gets its whole cell, shapes without area are dropped. Blocking material classes cannot be represented and are listed in `notExported` (D2).
- **sortY (D33).** `sortY` is validated as a finite number, so a schema-invalid `sortY: null` is a clean `error:` line (exit 1) instead of a TypeError traceback (the review's blocking item; tested).
- **Private BFS deleted (D4).** validate_chunks's stitched walkability search (`_local_grid_reachability` with `_UnionFind`) is now one world grid searched by `forge_nav.grid_bfs`: 4-neighbour moves stay inside a chunk and cross into the next one only at the cells of a paired door; behaviour is unchanged (every existing test passes; a new test pins the door rule and the single grid search). The chunk-graph traversals (placement propagation in `place_edges`, `graph_reachability` over paired doors) and validate_layout's span graph (`reach_from`, a directed graph of walks, drops and jump arcs in a side view) stay: they are graph searches, not 4-neighbour collision grids, and forge_nav has no general graph search.
- **Prop art and flip_x (D6).** Both exporters resolve art in the D6 order: `objects[].image`, the bundle's `props[prop]` (an inline item's image, relative to the bundle, or the prop pack named by `pack` + `label`), prop packs by label (the bundle's `prop_packs`, then `--prop-pack`), `occluder.source`. Godot: `flip_x` props are Sprite2D with `flip_h = true` and `offset.x = anchor_x - width` (Godot mirrors the texture inside its own rect), checked by the parse-back QA. LDtk: a `flip_x` prop gets its own entity definition (`Prop_<prop>_flip_x`) with a mirrored atlas copy and the mirrored pivot, plus a `FlipX` field; the atlas QA compares the mirrored region.
- **B21 bundles export.** The review's repro (B21 layout_build small_spec: 12 objects, 8 with `flip_x`, art only in `props{label}`) now exports: Godot exit 0, 8 `flip_h` sprites, 12 footprint shapes, every QA check passing; LDtk exit 0 (positions rounded half up and warned). Tested synthetically in both test files.
- **Conventions (D26-D30).** Usage errors exit 2 (argparse); every `main()` runs through `forge_core.run_cli` (one `error:` line, never a traceback, in-process calls too); JSON inputs are read with `forge_core.read_json(strict=True)` / `parse_json(strict=True)`; `tool.version` is `0.4.0`; `forge_core.file_ref` replaced the three `_local_file_ref` copies.
- **Tests on the real schemas (D33).** `REQUESTED_MAP_DEFS` / `REQUESTED_MAP_PATCHES` are gone; `requested_contract_errors` now validates against the vendored generate2dmap schemas. The engine fixture's material map is valid (a 16 px water square with a colour), so forge_nav, the preview and map_nav can read it.
- **Reviewer non-blocking items.** Documented in engine-maps.md: whole-pixel segment bounds, the gap measured between edge-column centres (`jumpDistance - 1` is the widest crossable gap), `assets[].source` relative to the bundle's folder (also in the schema description). Not changed: validate_layout's memory growth with level width (section 8).

## 1. CLIs

All commands run from the user's project root; outputs go to a new folder inside the project. In Claude Code notes `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.

    python "<skill-dir>/scripts/export_godot.py" --bundle maps/town/map-bundle.json --output-dir build/town-godot --name town
    python "<skill-dir>/scripts/export_godot.py" --bundle maps/town/map-bundle.json --output-dir build/town-godot --name town --prop-pack maps/town/props/prop-pack.json --texture-filter nearest --strict-qc
    python "<skill-dir>/scripts/export_godot.py" --bundle maps/cave/map-bundle.json --output-dir build/cave-godot --blob-inside dirt
    python "<skill-dir>/scripts/export_ldtk.py" --bundle maps/town/map-bundle.json --output-dir build/town-ldtk --name town
    python "<skill-dir>/scripts/export_ldtk.py" --bundle maps/town/map-bundle.json --output-dir build/town-ldtk --name town --prop-pack maps/town/props/prop-pack.json --strict-qc
    python "<skill-dir>/scripts/validate_chunks.py" --chunks maps/dungeon/room-chunks.json
    python "<skill-dir>/scripts/validate_chunks.py" --chunks maps/dungeon/room-chunks.json --start hall:40,40 --output-dir maps/dungeon/qa/chunks-v1 --strict-qc
    python "<skill-dir>/scripts/validate_layout.py" --layout maps/levee/layout.json
    python "<skill-dir>/scripts/validate_layout.py" --layout maps/levee/layout.json --output-dir maps/levee/qa/layout-v1 --strict-qc

- `--help` prints the module docstring (what is written, the checks, the conventions) and the options, ASCII only, exit 0 under cp1252 and cp950 (tested with `assert_cli_help`).
- Errors: one `error: ...` line on stderr, exit 1, never a traceback (malformed bundles, markers, chunk files and layouts are reported by field; a bundle forge_nav cannot read says `error: collision (forge_nav): ...`). Usage errors: argparse's exit 2. An existing `--output-dir` is refused.
- export_godot: writes `<name>.tileset.tres` (only when the bundle has a tiles layer), `<name>.tscn`, `assets/{tilesets,layers,props}/*.png` (byte-identical copies of the images the files use) and `godot-export.json` (`generate2dmap.engine_export.v1`: files, assets with source paths and sha256, tilesets with terrain mode, layers, counts including `footprints`, `notExported`, warnings, QA envelope). Before publishing, both files are re-read with the tool's Godot text parser; a failed round-trip check (tiles, peering bits, physics polygons, prop anchors with flip_h, collision shapes, markers, asset bytes) publishes nothing. `--strict-qc` also refuses warnings (objects, spawns, portals or interactions outside the world). Summary keys: `output_dir`, `scene`, `tileset`, `metadata`, `status`, `tiles_layers`, `objects`.
- export_ldtk: writes `<name>.ldtk` (LDtk JSON 1.5.3, one level, Free layout), `assets/tilesets/*.png`, `assets/layers/*.png` (the level background), `assets/props-atlas.png` (mirrored copies for `flip_x` props) and `ldtk-export.json` (same schema). QA: required fields and types against the embedded LDtk 1.5.3 snapshot (`LDTK_REQUIRED`), uid/iid/identifier rules, decoded grid tiles vs the bundle, entity positions and sizes vs the bundle, asset bytes and atlas pixels (mirrored for `flip_x`). Fractional positions are rounded half up and warned; `--strict-qc` refuses them. Summary keys: `output_dir`, `project`, `metadata`, `status`, `entities`, `tiles`.
- validate_chunks / validate_layout: without `--output-dir` they only print the summary; with it they publish `chunk-report.json` + `chunk-debug.png` / `layout-report.json` + `layout-debug.png` (also when a check fails, so the debug image can be inspected) and exit 1 on any failed check; `--strict-qc` publishes nothing when a check fails. Summary keys: `status`, `failed` (check ids), `problems` (first failure messages), `warnings` (first warning messages), plus `mode`, `chunks`, `placed` (chunks) or `spans` (layout), and `output_dir`, `report`, `metadata`, `debug` when written.

## 2. SKILL.md routing rows

For `skills/generate2dmap/SKILL.md` (routing table):

| Need | Route |
|---|---|
| Godot 4 project files from a playable map | `scripts/export_godot.py --bundle map-bundle.json --output-dir <new>`; copy the folder into the Godot project; mapping and limits in `references/engine-maps.md` |
| LDtk project from a playable map | `scripts/export_ldtk.py --bundle map-bundle.json --output-dir <new>`; open `<name>.ldtk` in LDtk 1.5.3+ (not verified here) |
| `room_chunk_mode`: check door sockets, stitching and reachability of room chunks | `scripts/validate_chunks.py --chunks room-chunks.json --output-dir <new>` (room_chunk.v1; optional walk grids); fix every failed socket before art |
| `side_scroll_mode`: check a level grammar against the jump and slope limits | `scripts/validate_layout.py --layout layout.json --output-dir <new>` (layout.v1); inspect layout-debug.png |

Processing-tools table rows:

| Script | Purpose / limits |
|---|---|
| `scripts/export_godot.py` | map_bundle.v2 to Godot 4.3+ `.tres` TileSet (terrain peering bits from Wang/blob data, physics polygons) and `.tscn` (TileMapLayer, y-sorted Sprite2D props on their anchors with flip_h, collision with solid footprints, markers); collision is the forge_nav blocking set, material classes listed as not exported; parse-level verified, editor import not verified |
| `scripts/export_ldtk.py` | map_bundle.v2 to an LDtk 1.5.3 project (tilesets, Tiles layers, prop/spawn/portal/interaction/anchor entities with mirrored flip_x props, level background); no collision layer (listed in notExported); required fields checked against a snapshot, LDtk not run |
| `scripts/validate_chunks.py` | room_chunk.v1: socket spans and materials on shared edges, layout rows or edge graph, chunk-graph reachability, walk-grid reachability with forge_nav's grid search, debug image |
| `scripts/validate_layout.py` | layout.v1: segments, slopes and ledges, deck thickness, prop support, gaps vs the jump arc, arena width at 16:9 and 19.5:9, reachability, side-view debug image |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `generate2dmap/scripts/export_godot.py` | Exports a map bundle to a Godot 4.3+ TileSet and TileMapLayer scene with props, collision (solid footprints included) and markers (exported data; editor import not verified) | `tests/test_export_godot.py`: independent regex/struct read-back of peering bits, physics polygons and `tile_map_data`; anchors with flip_h; footprint shapes compared with forge_nav; determinism |
| `generate2dmap/scripts/export_ldtk.py` | Exports a map bundle to an LDtk 1.5.3 project (editor import not verified) | `tests/test_export_ldtk.py`: required-field snapshot plus an independent loader-field list, tiles and entity positions read back, mirrored flip_x atlas entries |
| `generate2dmap/scripts/validate_chunks.py` | Checks room-chunk door sockets, stitching and reachability | `tests/test_validate_chunks.py`: width mismatch fails, 2x2 layouts pass, doors into walls and cut corridors fail, walks cross chunks only through doors |
| `generate2dmap/scripts/validate_layout.py` | Checks a side-scroll level against jump, slope, deck and arena rules | `tests/test_validate_layout.py`: 3-row deck, unjumpable gap and floating prop fail; parabola numbers |

## 4. CHANGELOG entries

- Added: `export_godot.py`, Godot 4.3+ export of map_bundle.v2: TileSetAtlasSource per tileset; terrain sets in Match Corners mode (Wang) or Match Corners and Sides mode (blob-47) with peering bits; physics polygons from tile collision shapes (a walkable: false tile without shapes blocks its cell); a `walkable` custom data layer; TileMapLayer per tiles layer; Sprite2D props whose origin sits on (x, sortY) with the anchor offset, flip_h for `flip_x`; StaticBody2D collision with collision.solids, rects and the footprints of solid objects (the forge_nav blocking set); Marker2D and Area2D markers; parse-back QA before publishing, collision shapes included (B14-T1; roadmap P2-1; MAP-01, F-07; D2, D6, D33).
- Added: `export_ldtk.py`, LDtk 1.5.3 export: tileset definitions with per-tile data in customData, Tiles layers, Entities for props (pivot = anchor; mirrored atlas entries for `flip_x`), spawns, portals, interactions and anchors, the bottom image layer as the level background, and a required-field check against an embedded LDtk JSON snapshot (B14-T2; roadmap P2-1; MAP-01, F-07; D6).
- Added: both exporters find prop art in the one D6 order every map tool uses (object image, the bundle's props registry, prop packs, occluder source), so layout_build bundles export as they are.
- Added: `validate_chunks.py` for room_chunk.v1: socket bounds and overlaps, layout rows (with chunk reuse as instances) or edge graphs, exact socket pairing (offset, width, material) across shared edges, dangling sockets, chunk-graph reachability, and walkability-grid reachability through doors (forge_nav's grid search) with a debug image (B14-T3; roadmap P2-6; room_chunk_mode; D4).
- Added: `validate_layout.py` for layout.v1: contiguous segments, slope and step limits, deck thickness (a 3-row deck fails by default), prop support per footprint column, gaps against a parabolic jump arc, arena width at 16:9 and 19.5:9 plus listed viewports, reachability from the first spawn, side-view debug image (B14-T4; owner side-scroller rules).
- Added: `references/engine-maps.md`: Godot and LDtk mappings, what is parse-level verified versus not verified in an editor, what each export does not carry, room chunk and layout authoring (B14-T5; roadmap 6.1 #7).
- Changed: none. BREAKING: none (new tools only).
- Fixed: MAP-01, DOC-05, F-07 in part: playable maps now export to Godot and LDtk data from the bundle (editor import is still not verified, so README claims stay "exported data").

## 5. Schema change requests

Status: applied by the shared stage S1 (commit f3d7eb2) in `shared/schemas/map.schema.json` and vendored: the new $defs `engine_export_v1`, `chunk_validation_v1`, `layout_validation_v1`, and the patches to `layout_v1`, `room_chunk_v1`, `tileset_v1.blob_inside`, `mapObject.image`/`image_sha256` and `map_bundle_v2.prop_packs` (merged with B17's per D8; `props`, `flip_x` and the D6 order come from D8 and B21). `engine_export_v1.assets[].source` is described as relative to the bundle's folder. The tests validate against the vendored schemas (D33).

Optional fields the tools now write beyond the listed properties (every object involved is open; recorded per Appendix B): `engine_export_v1.counts.footprints`; the QA check `collision_shapes_roundtrip` (threshold `{shapes, max_error_px}`); in `godot-export.json` `notExported` an entry `material_map blocking classes ...` when the map has blocking material classes; LDtk entity field `FlipX` (engine data, no schema).

## 6. Shared-helper promotion requests

- `_local_file_ref`: resolved; `forge_core.file_ref` is used in all four tools (D30).
- `_local_grid_reachability` with `_UnionFind`: resolved; validate_chunks uses `forge_nav.grid_bfs` and `moves_from_mask` (D4).
- Still requested, optional: `_local_safe_name(text, taken, fallback="item") -> str` (export_godot.py): ASCII file stem, unique case-insensitively within `taken`, with a short sha256 suffix when nothing ASCII is left (CJK labels). Candidate for forge_core next to B10's label slugging.
- Still requested, for the map-core group (B13): move the map_bundle.v2 reader of export_godot.py (`_local_read_bundle`, `_local_load_tileset`, `_local_layer_grid`, `_local_prop_pack_images`, `_local_prop_registry`, `_local_read_json`, `_local_check_markers`, `_local_rel_path`, `_local_check_sha`, `_local_png_info`, `_local_used_tilesets`, `_local_not_exported`, `_local_world_warnings`; export_ldtk.py imports them) into `map_bundle.py`, so export_tiled, export_godot, export_ldtk and build_scene_preview share one reader and one D6 art resolver. Collision is already shared (forge_nav).
- Optional, unchanged: Godot text helpers in export_godot.py (`gd_value`, `GdWriter`, `parse_godot_text`, `decode_tile_map_data`, `encode_tile_map_data`) could become `shared/forge_godot.py` if B09's export_engine should share one Godot `.tres` writer.

## 7. Cross-module links that Z must add

- generate2dmap SKILL.md: the section 2 rows; link references/engine-maps.md under engine targets, `room_chunk_mode` and `side_scroll_mode`. Notes for Z from the plan: "Map SKILL.md tools: export_godot, export_ldtk, validate_chunks (room_chunk_mode), validate_layout (side_scroll_mode). No editor-compatibility claims until manual import."
- map-strategies.md (B13 owns): the "chunk sockets" part should point to engine-maps.md, section "Room chunks".
- side-scroll-scenes.md (B11 owns): its "layout validator pointer" (B11-T4) should point to engine-maps.md, section "Side-scroll layouts", and validate_layout.py.
- layered-map-contract.md and export_tiled.py (B13): the same D6 prop-art order, the same tiles-layer data convention (rows of tileset indices, -1/null empty) and the forge_nav blocking set, so Tiled, Godot and LDtk exports of one bundle agree; list unsupported blocking material classes in Tiled's notExported too (D2).
- B20 autotile_build: write `blob_inside` on blob47 tilesets (or keep the inside material last) and keep `wang` as [tl, tr, bl, br]; export_godot maps both into Godot terrain sets.
- Integration e2e pipeline 4 (Map): after `export_tiled`, run `export_godot.py --bundle <bundle> --output-dir <new>` and `export_ldtk.py --bundle <bundle> --output-dir <new>` (both refuse existing folders) on the layout_build bundle.
- README (Z): the Godot section stays "exported data; editor import not verified" (MAP-01, DOC-05, F-07); name LDtk the same way. engine-maps.md lists what each export does not carry.

## 8. Known limitations and what is not proven

- No engine is installed here. Godot and LDtk outputs are verified at parse level only: re-read by the exporters' own readers and, in the tests, by independent regexes, `struct` decoding and an independent list of loader fields. Nobody has opened them in Godot or LDtk; do not claim editor compatibility until a manual import.
- Godot format facts: the TileSetAtlasSource tile keys (`x:y/0 = 0`), `sources/N`, and the TileMapLayer `tile_map_data` layout (uint16 format 0, then int16 x, int16 y, uint16 source, atlas x, atlas y, alternative per cell) were checked against a scene saved by Godot 4.5 on this machine. The terrain property names, physics (`physics_layer_0/polygon_N/points`, `physics_layer_0/collision_layer`) and custom data (`custom_data_layer_0/name|type`, `custom_data_0`) are from the Godot 4 source as remembered, not checked against a saved file. `flip_h` with `centered = false` mirrors the texture inside its own rect (Godot's canvas renderer flips a negative-width rect in place); the offset formula `anchor_x - width` follows from that and is checked by the parse-back QA, not in the editor. Layers are written before `sources/N` in the TileSet resource on purpose.
- Format targets: Godot 4.3+ (TileMapLayer), text format 3 with integer-list PackedByteArray (readable by 4.3 to 4.5); the legacy 4.0-4.2 TileMap node is not written. LDtk JSON 1.5.3; `appBuildId` is 0; the required-field snapshot `LDTK_REQUIRED` was hand-transcribed without network access, so a field LDtk requires but the snapshot misses would go unnoticed.
- Terrain semantics are a documented mapping, not editor-proven: a mixed Wang tile's centre terrain is its majority corner (ties: the lower material index); blob-47 neighbours that are not connected get the other material, or no bit with one material.
- Deviation (B14-T2): LDtk terrain is exported as Tiles layers (exact placement), not IntGrid plus auto-layer rules; per-tile wang/blob/collision/walkable data rides in tileset customData.
- Not exported (and listed in each report's `notExported`): to Godot, the material map (its blocking classes named, D2), nav grid, terrain vertex grid, camera, stage, atmosphere, lights and animatedParts (walk regions ride as `metadata/walk_regions`); to LDtk, additionally walk regions, solids, rects and solid footprints (no collision layer), and image layers other than the bottom one. Godot's one_way collision is not used for one_way material (listed as not exported).
- Non-circular ellipse solids and footprints become 32-gons inscribed in the ellipse, so they block slightly less than forge_nav's exact ellipse.
- The exporters refuse what forge_nav refuses (D2): Tiled-style CSV tile data with a trailing comma, a tiles layer that does not cover the world, a material map with an unclassified opaque pixel or a fractional scale, a material without a colour (or index for a palette image), a footprint basis other than prop_px, world_px or image_px.
- Double mirroring (cross-module, not fixed here): forge_nav mirrors any footprint of a `flip_x` object, including the object's own, while B21 layout_build writes the object's own footprint already mirrored and the `mapObject` description says such footprints are written as placed. On asymmetric footprints the Godot footprint shapes then sit on the unmirrored side. B21's current footprints are symmetric. The exporters follow forge_nav (D2); the integrator decides which side changes.
- LDtk positions and sizes are whole pixels: fractional bundle values are rounded half up (warned; `--strict-qc` refuses).
- validate_layout's model: the actor is a point at its feet (no collider width, head room or ceilings); one parabola (apex jumpHeight, range jumpDistance) with full air control; decks are one-way and never block an arc; a slope steeper than maxSlopeDeg that rises above stepUp fails; camera travel defaults to 0; arena checks at aspect ratios need `camera.viewHeight` or a [w, h] viewport. Segment bounds are whole pixels; the widest crossable gap is `jumpDistance - 1` (gaps are measured between edge-column centres). Jump candidates are sampled (at most 64 columns per span and 32 arcs per span pair), so an exotic route can be missed (a false failure, never a false pass). Memory grows with level width (per-column arrays and a full-resolution side view before downscaling); a 1e9 px gap segment took 4m44s in the review, with no cap.
- validate_chunks: walk grids are top-down 4-neighbour; side-view rooms should use validate_layout. Sockets are axis-aligned edge spans; rotated or mirrored chunk variants are not generated. Reuse of one chunk id works in layout rows (instances `id@row,col`), not in edge graphs. Grid reachability runs only when every placed chunk has a grid.
- Measured (Windows 11, Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1): a 256x256-tile bundle with 2000 props (rect and ellipse footprints, some rotated) exported to Godot in 2.7 s and to LDtk in 2.8 s after this pass (1.6 s and 1.9 s before it; the difference is forge_nav's blocking-set read and, for Godot, 2001 footprint shapes and their parse-back check); before this pass, a 10x10 chunk grid with 40x30-cell walk grids validated in 0.9 s and a 16,000 px level with 40 gaps and 120 decks in 0.7 s (not re-measured). No perf tests.
- Not run on Linux, macOS, Python 3.10 or Pillow 10.1.
- Test structure: `tests/test_export_ldtk.py` and `tests/test_scene_preview.py` import `build_bundle` from `tests/test_export_godot.py` (one synthetic engine bundle).
