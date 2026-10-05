# B13-map-bundle-nav-tiled: map_bundle.v2 validator, map_nav collision and reachability from data, Tiled export, map contract docs

Branch `asf/B13-map-bundle-nav-tiled` (from `wip/asf-upgrade-20261005` @ 3f9252d). New tools in generate2dmap: [map_bundle.py](../skills/generate2dmap/scripts/map_bundle.py), [map_nav.py](../skills/generate2dmap/scripts/map_nav.py), [export_tiled.py](../skills/generate2dmap/scripts/export_tiled.py). Tests: [test_map_bundle.py](../tests/test_map_bundle.py), [test_map_nav.py](../tests/test_map_nav.py), [test_export_tiled.py](../tests/test_export_tiled.py). Docs: [layered-map-contract.md](../skills/generate2dmap/references/layered-map-contract.md), [map-strategies.md](../skills/generate2dmap/references/map-strategies.md), [map-presets.md](../skills/generate2dmap/references/map-presets.md) (new).

The three scripts use only numpy, Pillow and the skill's vendored forge_core (scipy and jsonschema are not needed at run time; pytiled-parser is optional for `export_tiled.py verify --reader pytiled`).

## 1. CLIs

Run from the project root (`<skill-dir>` is `${CLAUDE_SKILL_DIR}` in Claude Code):

    python "<skill-dir>/scripts/map_bundle.py" validate --bundle map/map-bundle.json
    python "<skill-dir>/scripts/map_bundle.py" validate --bundle map/map-bundle.json --report map/qa/bundle-report.json --require-sha256
    python "<skill-dir>/scripts/map_bundle.py" hash --bundle map/draft-bundle.json --output map/map-bundle.json
    python "<skill-dir>/scripts/map_nav.py" check --bundle map/map-bundle.json --output-dir map/qa/nav
    python "<skill-dir>/scripts/map_nav.py" check --bundle maps/forest/map-bundle.json --link maps/ruins/map-bundle.json --output-dir maps/forest/qa-nav
    python "<skill-dir>/scripts/map_nav.py" query --bundle map/map-bundle.json --point 120,80 --segment 120,80,180,80
    python "<skill-dir>/scripts/export_tiled.py" export --bundle map/map-bundle.json --output-dir map/tiled --embedded-variant
    python "<skill-dir>/scripts/export_tiled.py" verify --map map/tiled/map.tmj --bundle map/map-bundle.json --reader pytiled

- `map_bundle.py validate`: JSON Schema (the vendored `references/schemas/map.schema.json`, evaluated by a built-in Draft 2020-12 evaluator, so no jsonschema at run time), every referenced file and its sha256 (tileset manifests check their own atlas sha256; prop-pack items theirs), and cross-field rules (unique ids per kind, tile indices against the atlas, wang values against materials, canonical blob masks, tiles covering the world exactly, vertex grid one larger than the tiles, unknown props and object layers, material colours or palette indices unique and covering every pixel, portal `to` targets, `entranceByFrom` spawns or points, zero travel directions, slots and approach points, points inside the world). Problems go to stderr as `error: <json path>: <message>` / `warning: ...`; exit 1 on any error. `--report` writes a QA envelope (also when the bundle fails; it refuses an existing file). Success prints `{"status", "bundle", "id", "version", "errors", "warnings", "metadata"}`.
- `map_bundle.py hash`: writes a new bundle with the sha256 of every referenced file filled in and paths rebased to the output folder; validates the result in memory first and publishes nothing when the source has errors (for example a missing file) or the output exists. Prints `{"status", "output", "metadata", "files_hashed"}`.
- `map_nav.py check`: builds the navigation grid (plan Appendix C, section 8 below), runs BFS from every spawn and portal arrival to every interaction, exit, anchor slot and approach point, checks portal triggers (inside the world), arrivals (outside every trigger, valid, on the grid) and, with `--link` (repeatable), the links between maps in both directions. Publishes `nav-grid.json`, `nav-report.json` (QA envelope) and `nav-debug.png` into a new `--output-dir`; any failed check exits 1 and publishes nothing unless `--publish-on-fail` (then it publishes and still exits 1). `--debug-scale N` (1-8) upscales the PNG. Prints `{"status", "output", "metadata", "grid", "debug", "map", "cell", "targets", "unreachable", "thinGaps"}`.
- `map_nav.py query`: prints `{"map", "cell", "points": [{"point", "valid"}], "segments": [{"from", "to", "clear", "reason"}]}`; `--sampled-only` tests segments with the Appendix C samples only (no thin-gap rule), for runtime parity work.
- `export_tiled.py export`: writes `map.tmj`, one `<tileset>.tsx` per bundle tileset, `props.tsx`, `images/` (byte copies), `preview.png` (the bundle's reference render) and `export-report.json`; `--embedded-variant` adds `map.embedded.tmj` (tilesets inlined, for Phaser). Before publishing it re-reads the written TMJ/TSX with the built-in reader and requires a 0 px difference from the reference render; otherwise nothing is published. Prints `{"status", "output", "metadata", "map", "embedded", "tilesets", "layers", "differingPixels"}`.
- `export_tiled.py verify`: re-renders an exported map with the built-in reader or with pytiled-parser (`--reader pytiled`; without it installed the tool prints `python -m pip install pytiled-parser` and exits 1) and compares it with the bundle's reference render.
- `--help` of every verb is ASCII and works under cp1252 and cp950 (tested). All console text goes through `forge_core.ascii_text`; no traceback reaches the user (`error: internal error (<type>): ...` at worst).

## 2. SKILL.md routing rows

generate2dmap (routing table and the Processing tools table):

| Need | Route |
|---|---|
| Check a playable map's data: files, sha256, ids, tiles, portals, slots | `scripts/map_bundle.py validate`; then `scripts/map_nav.py check` |
| Fill in the sha256 of every file a hand-written bundle references | `scripts/map_bundle.py hash` |
| Prove every interaction, exit, slot and approach point is reachable; arrivals outside triggers; links between maps | `scripts/map_nav.py check` (`--link` each neighbouring map); review `nav-debug.png` |
| Test whether a spot or a straight move is walkable | `scripts/map_nav.py query` |
| Hand a map to Tiled or Phaser | `scripts/export_tiled.py export` (`--embedded-variant` for Phaser); optionally `verify --reader pytiled` |
| Collision, occlusion classes, occupant policy, render order, QA checklist | [references/layered-map-contract.md](../skills/generate2dmap/references/layered-map-contract.md) |
| Autotile sets, sortY, portals, chunk sockets, roads and bridges, kitbash | [references/map-strategies.md](../skills/generate2dmap/references/map-strategies.md) |
| Starting values per genre (actor radius, ySquash, tiles, exits) and engine targets | [references/map-presets.md](../skills/generate2dmap/references/map-presets.md) |

Acceptance bullet for the map SKILL.md: "A playable map is accepted only after `map_bundle.py validate` and `map_nav.py check` pass; `map_nav.py` proves reachability from data, not that collision matches the painted art."

codeart2d (for B21's route, once it lands): `| Code-made playable map | layout_build.py writes map_bundle.v2; then generate2dmap map_bundle.py validate, map_nav.py check, export_tiled.py export |` (calling the sibling CLIs by path).

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `generate2dmap/scripts/map_bundle.py` | Validates playable map bundles (map_bundle.v2; v1 readable): contract, files and sha256, cross-field rules; fills in sha256 | `tests/test_map_bundle.py` (evaluator agrees with jsonschema on 450+ documents) |
| `generate2dmap/scripts/map_nav.py` | Collision and navigation from data (plan Appendix C plus a thin-gap rule): reachability of every interaction, exit, slot and approach point, portal arrivals, links between maps, `nav-grid.json`, debug PNG | `tests/test_map_nav.py` (grid moves equal segmentClear on random maps; merged rects are exact) |
| `generate2dmap/scripts/export_tiled.py` | Tiled 1.10 TMJ/TSX: corner wangsets, blob-47 as a two-colour mixed wangset, per-tile collision, prop image collection with anchors and sortY, collision and interaction layers, Phaser embedded variant | `tests/test_export_tiled.py` (re-render 0 px with the built-in reader and with pytiled-parser 2.2.9); Tiled GUI not verified |

README Godot section (MAP-01, DOC-05, F-07): "Map data is exported for Tiled (verified by re-rendering the exported files) and, through the engine exporters, for Godot 4 and LDtk; editor imports are not verified."

## 4. CHANGELOG entries

- Added: `map_bundle.py validate` and `hash` for generate2dmap.map_bundle.v2 playable-map bundles (v1 readable), with a standard-library JSON Schema evaluator for the vendored schemas (B13-T1; MAP-01, DOC-05, F-07).
- Added: `map_nav.py check` and `query`: plan Appendix C collision rasterised from bundle data (walk regions, solids, collision rects, object footprints scaled once, tile collision, material classes including one_way), a navigation grid of `max(1, round(r/2))` px, BFS reachability from spawns and arrivals to every interaction, exit, slot and approach point, merged blocked rectangles, the thin-gap rule, portal checks (inside, arrival outside the trigger, reciprocal links with `--link`), `nav-grid.json` and `nav-debug.png` (B13-T2).
- Added: `export_tiled.py export` and `verify`: Tiled TMJ with TSX tilesets (corner wangset, blob-47 two-colour mixed wangset, per-tile collision), a prop image collection with anchor and sortY properties, layers ground/decoration/props/collision/interactions, `--embedded-variant` for Phaser, verified by re-rendering at 0 px difference (B13-T3).
- Added: `references/map-presets.md` (genre presets and engine targets, data only) (B13-T4).
- Changed: `layered-map-contract.md` rewritten: one definition of `occlusion_class` and `occupant_policy`, render order, collision JSON, image versus collision versus occlusion, footprints scaled once, no alpha-fade occlusion, QA checklist; `map-strategies.md` adds the data route, composition rules, autotile sets, sortY, portals as bundle data, chunk sockets, roads and bridges and kitbash. All commands are single-line (B13-T4).
- BREAKING: none (new tools). The placement example in layered-map-contract.md no longer shows `occupantPolicy: {"mode": "fade-when-hidden"}` or `occlusionClass: "canopy"`; gameplay metadata moved to the bundle.
- Fixed: DOC-20 (`rear_shift_and_fade` and the other occupant policies are defined once, the example uses the enum); MAP-01, DOC-05, F-07 (engine-usable map data: a validated bundle, collision from data, reachability and a verified Tiled export; Godot and LDtk follow in the engine exporters).

## 5. Schema change requests

All additions are optional and additive (every valid A0 map fixture stays valid; tested by `test_schema_patch_is_valid_and_additive`). The tests validate every document these tools write against `shared/schemas` as vendored, and against the vendored map schema with exactly this patch applied in memory (`SCHEMA_PATCH` in [test_map_bundle.py](../tests/test_map_bundle.py), the single source of the JSON below). Producers: B13 (all), B21 layout_build (bundles). Consumers: B13, B14 (Godot/LDtk exporters), B17 (scene preview), B21.

New `$defs`, added to `/$defs` of `shared/schemas/map.schema.json`:

```json
"bundleProp": {
  "description": "A prop of the bundle's props registry: an image (relative to the bundle) with its anchor, or a prop_pack_v2 item (pack + label, its paths relative to that pack); fields given here override the pack item. sha256 is the image's.",
  "type": "object",
  "properties": {
    "image": {"$ref": "common.schema.json#/$defs/relPath"},
    "sha256": {"$ref": "common.schema.json#/$defs/sha256"},
    "pack": {"$ref": "common.schema.json#/$defs/relPath"},
    "label": {"type": "string", "minLength": 1},
    "anchor_px": {"$ref": "common.schema.json#/$defs/point2"},
    "footprint": {"$ref": "#/$defs/footprint"},
    "solid": {"type": "boolean"},
    "contact": true,
    "occlusion_class": {"enum": ["low", "tall", "foreground"]},
    "occupant_policy": {"enum": ["y_sort", "rear_shift_and_fade", "static_front", "static_back"]}
  },
  "anyOf": [{"required": ["image"]}, {"required": ["pack", "label"]}]
},
"nav_grid_v1": {
  "description": "map_nav.py navigation grid (plan Appendix C). Node (col, row) sits at ((col + 0.5) * cell, (row + 0.5) * cell). moves has one character per node: '#' for a blocked node, else a hex digit of its open moves (E=1, S=2 (+y), W=4, N=8). reachable marks nodes reached from the spawns and arrivals. blockedRects are disjoint [x, y, w, h] rectangles in world px whose union is exactly the blocked nodes.",
  "type": "object",
  "required": ["schema", "map", "world", "actor", "cell", "cols", "rows", "moves", "reachable", "blockedRects"],
  "properties": {
    "schema": {"const": "generate2dmap.nav_grid.v1"},
    "map": {"type": "string", "minLength": 1},
    "bundle": {"$ref": "common.schema.json#/$defs/fileRef"},
    "world": {"type": "object", "required": ["width", "height"], "properties": {"width": {"type": "number", "exclusiveMinimum": 0}, "height": {"type": "number", "exclusiveMinimum": 0}}},
    "actor": {"type": "object", "required": ["radius", "ySquash", "samples"], "properties": {"radius": {"type": "number", "minimum": 0}, "ySquash": {"type": "number", "exclusiveMinimum": 0}, "samples": {"type": "array", "minItems": 9, "maxItems": 9, "items": {"$ref": "common.schema.json#/$defs/point2"}}}},
    "cell": {"type": "integer", "minimum": 1},
    "cols": {"type": "integer", "minimum": 1},
    "rows": {"type": "integer", "minimum": 1},
    "moves": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^[0-9a-f#]+$"}},
    "reachable": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^[01]+$"}},
    "blockedRects": {"type": "array", "items": {"$ref": "common.schema.json#/$defs/rectXYWH"}}
  }
},
"map_bundle_report_v1": {
  "description": "map_bundle.py validate report: a QA envelope plus the bundle facts and every problem.",
  "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
  "required": ["schema", "problems"],
  "properties": {
    "schema": {"const": "generate2dmap.map_bundle_report.v1"},
    "problems": {"type": "array"}
  }
},
"nav_report_v1": {
  "description": "map_nav.py check report: a QA envelope plus starts, targets, links, thin gaps and pockets.",
  "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
  "required": ["schema", "targets"],
  "properties": {
    "schema": {"const": "generate2dmap.nav_report.v1"},
    "targets": {"type": "array"}
  }
},
"tiled_export_v1": {
  "description": "export_tiled.py export report: a QA envelope plus the Tiled tilesets and layers written.",
  "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
  "required": ["schema", "tiled"],
  "properties": {
    "schema": {"const": "generate2dmap.tiled_export.v1"},
    "tiled": {"type": "object"}
  }
}
```

New optional properties, merged into the `properties` object at each JSON pointer of `shared/schemas/map.schema.json`:

| JSON pointer | Properties to add |
|---|---|
| `/$defs/map_bundle_v2` | `{"id": {"type": "string", "minLength": 1, "pattern": "^[^:]+$", "description": "Map id used by portal targets (map:spawn). Default: the bundle file name without .json and a .map-bundle, -bundle or .bundle suffix; a file named map-bundle.json takes its folder name."}, "props": {"type": "object", "additionalProperties": {"$ref": "#/$defs/bundleProp"}}}` |
| `/$defs/map_bundle_v2/properties/terrain` | `{"sha256": {"$ref": "common.schema.json#/$defs/sha256"}}` |
| `/$defs/map_bundle_v2/properties/material_map` | `{"sha256": {"$ref": "common.schema.json#/$defs/sha256"}}` |
| `/$defs/map_bundle_v2/properties/nav` | `{"sha256": {"$ref": "common.schema.json#/$defs/sha256"}}` |
| `/$defs/mapLayer` | `{"offset": {"$ref": "common.schema.json#/$defs/point2"}}` |
| `/$defs/mapObject` | `{"layer": {"type": "string", "minLength": 1}, "occupant_policy": {"enum": ["y_sort", "rear_shift_and_fade", "static_front", "static_back"]}}` |
| `/$defs/mapObject/properties/occluder` | `{"sha256": {"$ref": "common.schema.json#/$defs/sha256"}}` |
| `/$defs/portal` | `{"reciprocal": {"type": "boolean"}}` |

Also requested (descriptions only, no rule change):

- `map_bundle_v2.description`: replace "generate2dmap.map_bundle.v1 is still read (only its schema key is checked here)" with "generate2dmap.map_bundle.v1 (the roadmap 4.10 draft: inline tilesets {id, image, kind, tiles}, footprints {type, rx, ry} or {x, y, w, h}, no world, collision or props) is still read: map_bundle.py maps it onto these fields in memory." The schema's property rules already apply to v1 documents; the reader moves inline tilesets aside before checking.
- `mapLayer.description`: "tiles layers hold tileset-local tile indices (0-based atlas positions, row-major by columns; -1 or null = empty) as rows of a CSV file, a JSON file (a list of rows, or {data: rows}) or an inline list of rows; one tileset per layer (optional when the bundle has exactly one); the grid must cover the world exactly."
- `portal.description`: append "to is <map id> or <map id>:<spawn or anchor>; entranceByFrom maps a source map id to the spawn id (or [x, y]) where travellers coming back from that map arrive in this map."

Fields the producers add that the frozen schema allows but does not list: the props registry and `id` above; `objects[].layer`, `objects[].occupant_policy`, `layers[].offset` (image layers), `portals[].reciprocal`, and `sha256` beside `terrain.vertex_grid`, `material_map.image`, `nav.grid` and `objects[].occluder.source`. Reports carry `schema` ids `generate2dmap.map_bundle_report.v1`, `generate2dmap.nav_report.v1` and `generate2dmap.tiled_export.v1` plus fields beyond the QA envelope (`bundle`, `problems`; `map`, `cell`, `starts`, `targets`, `links`, `thinGaps`, `unreachablePockets`, `bundleNav`; `tiled`). Problem entries are `{severity, path, message, code: schema|file|sha256|rule}`.

## 6. Shared-helper promotion requests

Private helpers in [map_bundle.py](../skills/generate2dmap/scripts/map_bundle.py) that belong in `forge_core` (generic, needed by other validators such as B14 validate_chunks/validate_layout, B15 validate_stage, B08 validate_animation and codeart2d's layout_build):

- `_LocalSchemaSet(directory)` with `.errors(instance, ref) -> list[str]` (plus `json_path`, `_is_type`, `_json_equal`): a Draft 2020-12 evaluator for exactly the keywords the shared schemas use (type, const, enum, numeric and string bounds, pattern, items, prefixItems, contains, required, properties, additionalProperties, propertyNames, min/maxProperties, allOf, anyOf, oneOf, not, if/then/else, local and cross-file $ref; format is an annotation); any other assertion keyword raises ValueError instead of being skipped. Proposed: `forge_core.schema_errors(instance, ref, schema_dir) -> list[str]` with error lines `$.json.path: message`. Tests: `test_schema_evaluator_matches_jsonschema` (450+ documents, same verdict as jsonschema), `test_schema_evaluator_covers_every_keyword` (all six shared schemas).
- `_local_read_json(path)`: strict JSON (UTF-8 with an optional BOM, no NaN or Infinity, no duplicate keys) raising BundleError; proposed `forge_core.read_json(path)` raising ValueError. Test: `test_strict_json_reader`.
- `_local_file_ref(path, base, digest=None)`: a common fileRef relative to `base`, recording only the file name when no relative path exists (the A1 rule for other drives). Proposed `forge_core.file_ref`.
- `_local_run_cli(parser, argv)`: `utf8_stdio()`, argparse, then `error: <message>` and exit 1 for expected errors and `error: internal error (<type>): ...` for anything else, never a traceback. Proposed `forge_core.run_cli`.
- `_local_round_half_up(value)`: forge_core already has the private `_round_half_up`; make it public.
- `merge_rects(mask)` (public in map_bundle, re-exported by map_nav): the greedy exact rectangle cover (disjoint, union equals the True cells). codeart2d's layout_build (B21, "the rect union equals the blocked set") lives in another skill and cannot import it; proposed `forge_core.merge_rects`. Tests: `test_merge_rects_is_exact`, `test_merged_rects_union_equals_blocked`.

Not for forge_core: `map_nav.grid_bfs(passable, starts, moves=None) -> distances` (with `reachable_mask`, `moves_from_mask` and `MOVE_E/S/W/N`) is the generate2dmap reachability that the integration step "consolidate B14's BFS into map_nav" should call (same skill, sibling import).

## 7. Cross-module links that Z must add

- generate2dmap SKILL.md: the routing rows of section 2; link layered-map-contract.md, map-strategies.md and map-presets.md (all exist); add the three scripts to the Processing tools table.
- Integration, B14: replace the private grid BFS of validate_chunks.py and validate_layout.py with `map_nav.grid_bfs(passable, [(row, col), ...], moves=None)` (directed moves via the `moves` bits when needed). export_godot.py and export_ldtk.py should read bundles through `map_bundle.load_bundle()` and reuse `object_placement`, `draw_order`, `world_solids` and `tile_layer_rgba` so all exporters agree with export_tiled and map_nav. engine-maps.md should link map-presets.md (engine targets table) and say the Tiled export is verified by re-rendering.
- Integration, B17: implement map-runtime.mjs from the collision contract in section 8 (formulas and evaluation order are exact); the JS/Python parity test can use `map_nav.py query` (with and without `--sampled-only`) on three fixtures: `write_demo_bundle()` from tests/test_map_bundle.py, the thin-gap bundle of `test_thin_gap_not_jumpable`, and the material/one_way bundle of `test_material_classes`. build_scene_preview should draw with `map_bundle.draw_order()` and `object_placement()`, and scene-preview.md should link layered-map-contract.md.
- Integration, B21: layout_build writes map_bundle.v2 with the section 5 fields (props registry, `id`), then calls `map_bundle.py validate` and `map_nav.py check` by path; its tilesets follow tileset.v1 as export_tiled reads it (wang `[top_left, top_right, bottom_left, bottom_right]`; blob mask bits N, NE, E, SE, S, SW, W, NW, canonical; for blob47 the outside colour is `materials[0]` and the blob material `materials[-1]`).
- Integration, B20: autotile_build's tileset.v1 is consumed by export_tiled exactly as above; the codeart2d tiles-and-maps.md should point to export_tiled for Tiled output.
- B10: a bundle prop can be `{"pack": "<prop-pack.json>", "label": "<label>"}`; prop-pack-contract.md should mention that map bundles read accepted items' `image`, `sha256`, `anchor_px`, `footprint`, `solid`, `occlusion_class` and `occupant_policy`, and should link layered-map-contract.md for the enum definitions.
- B12: the layered-map-contract.md placement example uses placements.v2 with `anchor: "px"`; if compose's default becomes `anchor: "manifest"`, Z may simplify the example (the command line already uses only the existing flags). parallax-backgrounds.md and side-scroll-scenes.md (B11) can link map-presets.md.
- A0/Z: add `pytiled-parser>=2.2,<3` to requirements-dev.txt so `test_rerender_pytiled_zero_px` runs in CI instead of skipping (it is installed locally, 2.2.9); it stays optional at run time.
- CHANGELOG and README per sections 3 and 4.

## 8. Known limitations and what is not proven

### Collision contract as implemented (for map-runtime.mjs parity)

Every ambiguity of plan Appendix C resolved by map_nav.py (and `map_bundle.world_solids()`), in evaluation order where it matters:

1. Coordinates are world px, y down. Rotations are degrees, clockwise on screen.
2. Footprint samples, in this order: the centre `(0, 0)`, then `(rx, 0)`, `(dx, dy)`, `(0, ry)`, `(-dx, dy)`, `(-rx, 0)`, `(-dx, -dy)`, `(0, -ry)`, `(dx, -dy)` with `rx = r`, `ry = r * ySquash`, `dx = rx * SQRT1_2`, `dy = ry * SQRT1_2` and `SQRT1_2 = 0.7071067811865476` (Math.SQRT1_2). A sample is `P.x + offset.x`, `P.y + offset.y`. `ySquash` defaults to 1.0 (the schema default); a bundle with a `stage` but no explicit `ySquash` gets a warning, not a silent 0.58.
3. "Inside at least one walk region" is decided per sample: some region contains the sample and none of that region's holes does. Point-in-polygon is the even-odd crossing test: for each edge `a = poly[i]`, `b = poly[i - 1]` (i = 0 pairs with the last vertex), a crossing is `(a.y > y) != (b.y > y) && x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x`, evaluated in that order (horizontal edges never cross). Left and top boundaries are inside, right and bottom outside, so regions sharing an edge leave no seam. Without walk regions the area is the closed box `0 <= x <= W`, `0 <= y <= H`.
4. Solids are closed sets: rect `x <= px <= x + w && y <= py <= y + h`; ellipse `(u / rx) * (u / rx) + (v / ry) * (v / ry) <= 1` with `u = dx * cos + dy * sin`, `v = dy * cos - dx * sin` (`dx = px - cx`; no rotation means `u = dx`, `v = dy` exactly); polygon solids use the even-odd test of item 3. Zero-size rects and ellipses block nothing (validation warns). Closed sets make rectangle merging exact, which the tile-collision merge relies on. (The reference games used an open `< 1` ellipse test; boundary-only differences.)
5. Blockers are: `collision.solids`; `collision.rects` (physical rect solids, NOT the navigation blocked set); solid object footprints; tile collision; material pixels of class `solid`, or `liquid`/`hazard` without `walkable: true`. `decor` never blocks; `one_way` never blocks a point (item 10). A `solid` material with `walkable: true` still blocks (warning).
6. Object footprints (prop pixels) are scaled once: centre `(x + scale * offset[0], y + scale * offset[1])`; ellipse `rx = (scale * width) / 2`, `ry = (scale * depth) / 2`, `rotate` kept; rect `(cx - w / 2, cy - h / 2, w, h)` with `w = scale * width`, `h = scale * depth`; a rotated rect becomes the polygon of its corners `(-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)` rotated by `(u cos - v sin, u sin + v cos)`. An object is solid when its `solid` (or its prop's) says so, otherwise when it has an ellipse or rect footprint. Never inflated by the actor radius.
7. Tile collision: each placed tile's `collision` shapes (tile px) move by the tile's origin; `properties.walkable: false` without shapes blocks the whole cell; integral rects are unioned per layer and re-merged with `merge_rects` (same closed set, fewer shapes).
8. Material map: pixel `(floor(x / s), floor(y / s))` where `s = world width / image width` (a whole number, same for both axes); outside the image there is no material. Colour images match `color` exactly (alpha-0 pixels are "no material"); P or L images match `index` when every material has one; every other pixel is a validation error.
9. Grid: `cell = max(1, floor(r / 2 + 0.5))` (Math.round for r >= 0); `cols = ceil(W / cell)`, `rows = ceil(H / cell)`; node `(col, row)` at `((col + 0.5) * cell, (row + 0.5) * cell)`. Grids above 16,777,216 nodes are refused with advice to split the map.
10. segmentClear(a, b): `n = max(1, ceil(sqrt(dx * dx + dy * dy) / (cell / 2)))`, samples `a.x + (b.x - a.x) * k / n` (multiply, then divide, then add) for k = 0..n, each valid (all 9 footprint samples). If `b.y > a.y`: no footprint sample may go from another material onto `one_way` between consecutive samples (one_way "blocks from above only"); upward and sideways moves pass. Then the thin-gap rule below.
11. Thin-gap rule (task B13-T2; not spelled out in Appendix C): the actor's centre must stay in the walk area and off every blocker for the whole segment. The segment is split at every parameter where any region, hole or solid boundary or material pixel edge can cross it, and each piece's midpoint is tested with the single-sample rule; for a downward move the centre may also not pass from another material onto one_way. An isolated tangent contact counts as clear. `segment_status(a, b, thin_gap=False)` and `query --sampled-only` give the Appendix C sampled rule alone.
12. BFS: 4-neighbour moves between valid nodes; a move is open exactly when segmentClear holds between the two node centres (with n = 2, so the move's midpoint is sampled too; this is stricter than "validity at cell centres" and guarantees a grid path is walkable by a segmentClear runtime). S and N moves can differ only through one_way. `test_grid_edges_equal_segment_clear` checks this on random maps.
13. Starts: every spawn, plus portal arrivals given as points (arrivals given as spawn ids are spawns). A start joins the grid at every valid node within two cells that it reaches by segmentClear; a start that is invalid or joins nothing is an error.
14. Targets: an interaction with `reach` needs a reachable node centre within `reach` of its point (`dx * dx + dy * dy <= reach * reach`); without `reach` it is a point target (the actor must stand there: valid, and joined to a reachable node as in item 13). Anchor slots and approach points are point targets; the anchor's own point is not a target. A `crossing` exit needs a reachable node inside the closed trigger, or a reachable node within two cells that reaches the trigger's closest point by segmentClear; an `intent` exit needs a reachable node within `radius` of the closed trigger area.
15. Portals: triggers must lie inside the closed world box. Every arrival (each `entranceByFrom` value, and the spawn of a same-map `to`) must lie outside every trigger of its map (distance from the closed trigger area > 0), be valid and join the grid. `to` is `<map id>` or `<map id>:<spawn or anchor>` (a spawn wins over an anchor of the same name). With `--link`, each portal's destination arrival (the named spawn, or else the destination portal back whose `entranceByFrom` names this map) must exist, lie outside the destination's triggers, be valid and join its grid, and the destination must have a portal back unless `reciprocal: false`. Without `--link`, reciprocity is reported as `skipped` with the destinations listed; a portal with no `entranceByFrom` for its own destination is a warning.
16. Runtime-only rules not modelled by map_nav: intent firing (normalised `dot(intent, travelDirection) > 0.25`), `latch` (default true; false warns) and `requiresMovement` (default true).

### Not proven or not covered

- The Tiled GUI is not verified: opening the files, the corner wangsets and the two-colour mixed blob wangset with terrain brushes. Phaser rendering of `map.embedded.tmj` and engine imports are not run. The 0 px proof covers tile layers, image layers and prop tile objects in file order, by the built-in reader and by pytiled-parser 2.2.9 (local, optional; the test skips without it).
- Runtime depth sorting by the `sortY` property is a runtime duty; Tiled's own `topdown` order sorts by image bottom, so the export stores objects pre-sorted with `draworder: index`.
- JS parity is not tested here (B17 and integration). Platformer reachability (jump arcs) is not modelled; map_nav checks top-down movement only.
- Grid resolution: positions closer than one cell to a blocker are not sampled; a corridor not wider than `2 * actorRadius + cell` may be walkable but unprovable; a valid target with no valid node within two cells reports "off the grid". Unreachable walkable pockets are reported as warnings (they can be intentional; per-sample region membership can create thin strips of valid nodes straddling a region gap).
- Collision data is never compared with the painted art; nav-debug.png is for a human or agent to look at.
- v1: map_bundle.v1 never had a frozen schema; this reader accepts the A0 legacy fixture shape and the roadmap 4.10 draft (inline tilesets, `{type, rx, ry}` and `{x, y, w, h}` footprints, no world, collision or props). Anything else in a v1 document is checked against the v2 property rules.
- Ran on Windows 11 only (Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1, jsonschema 4.26.0); Python 3.10, Pillow 10.1, Linux and macOS were not run (the code avoids newer APIs). PNG bytes are deterministic for one Pillow/zlib build.
- Performance (this machine, shared with other agents): a 1024 x 768 world with r = 6 (87,552 nodes), 300 mixed blockers, a holed walk region and a material map builds and searches in 0.6 to 1.4 s (perf test budget 20 s); the demo map loads and checks in 0.1 to 0.5 s. `segment_status` costs about 0.4 ms per call, so thousands of point targets add seconds.
- Deviations from the plan: (1) BFS moves require segmentClear between node centres (item 12), stricter than validity at the centres alone; (2) the thin-gap rule is an exact centre-path test rather than a sampling rule (item 11); (3) extra verbs `map_bundle.py hash`, `map_nav.py query` and `export_tiled.py verify`; (4) new document types `nav_grid_v1` and three report ids, proposed in section 5; (5) `--publish-on-fail` lets a failed navigation check publish its diagnostics (exit code stays 1); (6) Tiled maps without tile layers use a 16 px grid covering the world; (7) a bundle props registry and map `id` were needed so objects have images and portals have map ids (section 5).
