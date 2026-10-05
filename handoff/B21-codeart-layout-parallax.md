# B21-codeart-layout-parallax: codeart2d layout_build (playable map_bundle.v2 with reachability and exact collision rectangles), parallax_build (periodic layers, sibling validator, 49-frame sweep) and ambient_bake (exact-period plate loops)

Branch `asf/B21-codeart-layout-parallax` (from `wip/asf-upgrade-20261005` @ 3f9252d). New files: [layout_build.py](../skills/codeart2d/scripts/layout_build.py), [parallax_build.py](../skills/codeart2d/scripts/parallax_build.py), [ambient_bake.py](../skills/codeart2d/scripts/ambient_bake.py), [examples/meadow-layout.json](../skills/codeart2d/examples/meadow-layout.json), [examples/parallax-gen.json](../skills/codeart2d/examples/parallax-gen.json), [examples/plate-effects.json](../skills/codeart2d/examples/plate-effects.json), [references/layouts-and-parallax.md](../skills/codeart2d/references/layouts-and-parallax.md), [tests/test_codeart2d_layout.py](../tests/test_codeart2d_layout.py) (31 collected after Phase 3), [tests/test_codeart2d_parallax.py](../tests/test_codeart2d_parallax.py) (23 collected after Phase 3).

## Phase 3 integration status (branch asf/int-g-codeart)

Resolved in the codeart group fix pass (integration decisions cited as Dn):

- **D3/D4, reachability through forge_nav.** The private Appendix C code (`SolidSet`, `Walker`, `navigate`, `blocked_raster`, `object_solid`) is deleted. layout_build builds the bundle's collision-relevant document in the stage, reads it back with the vendored `forge_nav.blocking_set_from_document` (exactly what map_nav, the exporters and the runtime read) and runs `forge_nav.build_grid` / `navigate` / `exit_target` / `reach_target` / `point_target` on the full D2 blocking set, collision.rects included. The codeart review's meadow comparison, rerun on `examples/meadow-layout.json --seed 7`: layout_build reports 23,349 valid and 23,304 reachable nodes (cell 3); forge_nav reading the published bundle gets the same; B13's map_nav.py run by path on the same bundle gets the identical node set (23,349 valid, 23,304 reachable). Before: layout_build 23,830 vs map_nav 23,347. Test: `test_forge_nav_reads_the_bundle_exactly_as_layout_build_proved_it`.
- **D3, rects.** `collision.rects` are `forge_core.merge_rects` of a rectCell raster: the cells whose centre an exact blocker covers (forge_nav N5 closed sets). `_run_rects` is deleted. rectCell defaults to the tile collision grid with `--tiles` (an autotile_build set's `collision_cell`, so the rects are exact on its tile collision) and to half a tile without (exact on the vertex squares); only prop footprints are approximated. collision.rects bytes changed accordingly.
- **D5, tile collision.** With tilesets the bundle's terrain collision is the placed tiles' own `tiles[].collision` (N7): no vertex-square solids are written. A tileset that states no collision for a tile gets the vertex-square rule (each corner's quadrant) written into the bundle's copy, so every reader still sees one blocking set. A new `tile_collision` check warns when a full tile's stated collision contradicts the layout material's `walkable`. The copied manifest drops `qa` (relPath or fileRef); the QA file joins `provenance.inputs` by hash.
- **D6/D7, footprints.** Objects now carry the prop footprint as authored (prop pixels, `basis: prop_px`, unmirrored) with `flip_x` and `scale`, so readers mirror and scale it once (forge_nav N6). Before, layout_build wrote it already mirrored, which a D6 reader mirrored again. Object footprints are no longer duplicated into `collision.solids` (each blocker is written once; terrain solids stay there for maps without tilesets).
- **D8.** The props registry items validate as `bundleProp` (and `propItem`); the bundle, vertex grid, layout spec, parallax spec, plate effects and ambient loop validate against the real vendored schemas. The in-memory `PROPOSED_*` schema patches in the tests are gone.
- **D26.** `--seed` and `--rect-cell` (layout_build), `--frames` and `--fps` (ambient_bake) and `--sweep-frames` (parallax_build) are argparse usage errors: `usage: ...`, exit 2. An existing `--output-dir` stays a runtime error (exit 1).
- **D27.** `main()` runs through `forge_core.run_cli` in all three tools (in-process callers too): anything unexpected is one `error: internal error (Type: message)` line, exit 1. The codeart reviewer's `fuzz_b21.py` reports 0 problems.
- **D28.** Specs, tileset manifests and prop packs are read through `forge_core.parse_json` / `read_json` (a UTF-8 BOM is accepted).
- **D29.** QA envelopes, provenance and codeart-meta renderer carry `tool.version` `0.4.0` (`forge_core.FORGE_PACKAGE_VERSION`).
- **D30.** `forge_core.file_ref`, `round_half_up` and `merge_rects` replace the private copies. `_local_capped_edt` (layout_build and ambient_bake) and `_inside_polygon` (both) are consolidated into one codeart_core copy each, `codeart_core.distance_field` and `codeart_core.inside_polygon` (forge_core's `distance_to` is the capped Chebyshev distance, so the exact Euclidean one stays in the codeart2d library; its values equal the former copies with and without scipy, and ambient_bake outputs are byte-identical). `_local_write_codeart_meta` is deleted: codeart_core 1.1 takes `placeholder=`.
- **Codeart review, non-blocking.** Scatter `count` and `attempts` are capped at 250,000 per group (the 1e7 case grew to 26 GB); parallax_build refuses canvases above 16.7 Mpx (the minimum-zoom composite included) and sweeps above 128 Mpx (viewport [100000, 100000] reached 10 GB); pixel-art image layers need a whole-number scale (nearest neighbour at a fractional factor gave uneven pixels); the dangling tileset `qa` on copy (D5).
- **Not done:** spawn `facing` and ridge `role` stay free strings. The map schema allows any non-empty facing string or a number, and validate_parallax.py treats roles by prefix, so a closed list in layout_build or parallax_build would refuse documents the contracts accept; it needs a contract decision first.

## 1. CLIs

Run from the user's project root (`<skill-dir>` is the codeart2d folder, `${CLAUDE_SKILL_DIR}` in Claude Code):

    python "<skill-dir>/scripts/layout_build.py" --spec meadow-layout.json --output-dir out/map-v1 --seed 7 --preview --strict-qc
    python "<skill-dir>/scripts/layout_build.py" --spec meadow-layout.json --tiles out/tiles-v1/tileset-manifest.json --output-dir out/map-v2 --seed 7 --preview --strict-qc
    python "<skill-dir>/scripts/parallax_build.py" --spec parallax-gen.json --output-dir out/bg-v1 --validate --sweep-frames 49
    python "<skill-dir>/scripts/ambient_bake.py" --plate art/harbor-plate.png --spec plate-effects.json --output-dir out/harbor-ambient-v1 --preview --strict-qc

Flags:

- layout_build: `--spec`, `--output-dir`, `--tiles MANIFEST` (repeatable; tileset_v1 `wang_corner` or `flat`; its per-tile collision is the terrain collision), `--seed` (overrides the spec), `--preview` (preview.png and debug.png), `--strict-qc`, `--rect-cell PX` (default: the tile collision grid with `--tiles`, else half a tile), `--debug-scale 1-4`.
- parallax_build: `--spec`, `--output-dir`, `--validate` / `--no-validate` (default: run the sibling validate_parallax.py when it is found), `--validator PATH`, `--sweep-frames N` (default 49; 0 turns the sweep off), `--strict-qc`.
- ambient_bake: `--plate`, `--spec` (codeart2d.plate_effects.v1 or generate2dmap.stage.v1), `--output-dir`, `--frames`, `--fps`, `--preview` (review.png, preview.webp), `--plate-art-source code|host_image|api|existing|video|mixed` (default existing), `--strict-qc`.

All three follow plan Appendix D:

- `utf8_stdio()` first; work in `forge_core.staged_output`; an existing `--output-dir` is refused before any work.
- `--strict-qc`: a failed check exits 1 and publishes nothing. Without it the output is published with a failing QA envelope (status `fail`, exit 0), so the user can open debug.png.
- Errors are one ASCII `error: ...` line on stderr with exit 1 (D27: through `forge_core.run_cli`, an unexpected failure too); argument errors are argparse usage errors with exit 2 (D26).
- `--help` is ASCII under cp1252 and cp950; tested with `assert_cli_help`.

The one-line JSON summary reports:

| Tool | Keys |
|---|---|
| layout_build | `status`, `output`, `bundle` (map-bundle.json), `metadata` (codeart-meta.json), `qa` (layout-qa.json), `objects`, `solids` (exact blockers: terrain solids, footprints, tile collision), `rects`, `portals`, `valid_cells`, `reachable_fraction` |
| parallax_build | `status`, `output`, `plan` (parallax-plan.json), `metadata` (codeart-meta.json), `qa` (parallax-qa.json), `layers`, `sweep_frames`, `validated` |
| ambient_bake | `status`, `output`, `metadata` (ambient-loop.json), `qa` (ambient-qa.json), `frames`, `period_ms`, `fps` (rational string) |

## 2. SKILL.md routing rows

codeart2d SKILL.md:

| Need | Route |
|---|---|
| A playable top-down map made from code (village, meadow, dungeon floor) with roads, props, exits, collision and a reachability gate | `scripts/layout_build.py --spec <layout.json> --output-dir <new> --preview --strict-qc` (add `--tiles <tileset manifest>` from autotile_build.py for real tiles and tile collision); open debug.png; then generate2dmap `map_nav.py` (same forge_nav rules, same answer) and the engine exporters. Spec: `references/layouts-and-parallax.md` |
| A stylised parallax background (dithered sky, ridges, clouds, foreground grass) | `scripts/parallax_build.py --spec <parallax.json> --output-dir <new> --validate --sweep-frames 49`; review sweep-sheet.png |
| Ambient motion on a static plate (water ripples, heat haze, swaying reeds, lantern glow) | `scripts/ambient_bake.py --plate <png> --spec <effects.json or stage.json> --output-dir <new> --preview --strict-qc`; review review.png; encode with generate2dmap scene motion |

generate2dmap SKILL.md (plan note for Z):

| Need | Route |
|---|---|
| A code-made playable map | codeart2d `layout_build.py`, then `map_nav.py` (reachability and navigation), then `export_tiled.py` / Godot / LDtk |
| A stylised (non-painterly) parallax | codeart2d `parallax_build.py` (it validates with this skill's `validate_parallax.py`) |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `codeart2d/scripts/layout_build.py` | Builds a playable top-down map_bundle.v2 from a layout spec: vertex-grid terrain, spline roads, corner-Wang tiles whose own collision is the terrain collision, seeded prop variety, collision rectangles, exits with arrival spawns, a reachability gate run with the shared forge_nav rules, preview and debug overlay | `tests/test_codeart2d_layout.py`: forge_nav and an independent Appendix C check agree node for node with the tool's report; every exit reachable; rectangle union equals the blocked raster; autotile_build tile collision used as is; seeded byte-identity, also without scipy; the real vendored map schema |
| `codeart2d/scripts/parallax_build.py` | Periodic code-art parallax layers, a parallax plan, the generate2dmap validator and a 49-frame camera sweep | `tests/test_codeart2d_parallax.py`: double-width render equals the tiled layer; loop step at most p95; validate_parallax passed; 49-frame sweep.webp |
| `codeart2d/scripts/ambient_bake.py` | Bakes ripple, shimmer, sway and glow loops inside feathered polygons of a plate | `tests/test_codeart2d_parallax.py`: frame N equals frame 0; pixels outside the polygons equal the plate in every frame |

## 4. CHANGELOG entries

- Added: `codeart2d/scripts/layout_build.py` (B21-T1). Builds a playable top-down map (generate2dmap.map_bundle.v2) from a code spec:
  - terrain painted on a vertex grid (ellipses with seeded wobble, rects, polygons, Catmull-Rom spline roads);
  - hygiene so every cell is drawable by the given corner-Wang tilesets, with saddles and lone vertices removed;
  - autotiling with interior variants that avoid repeating neighbours;
  - props from images, inline PixelSpecs, prop-pack v2 items or flat placeholders;
  - seeded scatter with weighted kinds, variants and mirroring; Poisson-disk spacing, density noise, clearance and near-bands; a same-look neighbour check;
  - one blocking set for every reader: the placed tiles' own collision (or merged vertex squares without tilesets), prop footprints stored as authored with flip_x and scale, and merged collision rectangles equal to the blocked raster at the tile collision grid;
  - reachability with the shared forge_nav rules (the code map_nav.py runs) on the bundle's full blocking set, for every exit, spawn and interaction;
  - intent exits with latched triggers and arrival spawns outside them;
  - preview and debug overlay; QA envelope and codeart-meta.
- Added: `codeart2d/scripts/parallax_build.py` (B21-T2):
  - sky, ridge, clouds and foreground layers, periodic by construction and proven by a double-width render;
  - the layer origin rolled to the calmest column, so the loop step is at most p95 of the column steps;
  - canvases sized for every camera and zoom extreme, and image layers;
  - the plan is checked by generate2dmap `validate_parallax.py`;
  - a 49-frame camera sweep (sheet and animated WebP).
- Added: `codeart2d/scripts/ambient_bake.py` (B21-T3):
  - ripple, shimmer, sway and glow in feathered polygons, from a plate-effects spec or a generate2dmap stage.v1 document;
  - integer temporal harmonics, so frame N equals frame 0 exactly;
  - protected regions, and pixels outside the polygons unchanged;
  - integer-ms durations, rational fps and a motion mask.
- Added: `codeart2d/references/layouts-and-parallax.md` and three examples (meadow layout, parallax, plate effects) (B21-T4).
- Changed: none. BREAKING: none (new tools; no default of an existing tool changes). Within this release, against the Wave B module: collision.rects are re-merged by forge_core.merge_rects at the tile collision grid; object footprints are stored unmirrored and no longer repeated in collision.solids; argument errors exit 2.
- Fixed: the battle-stage runtime's water displacement (`sin(q * 0.57 - phase * 0.37)`) does not repeat after one period. Baked loops use integer harmonics instead, with a regression test (plan B21-T3 source: Dusk battle-atmosphere.js L31-155).
- Fixed, in part: MAP-01, DOC-05 and F-07 (no engine-usable map data, no collision derived from data). Code-made maps now carry data-derived collision, reachability and portals; the exporters are B13 and B14.

## 5. Schema change requests

**Status: applied.** S1 (commit f3d7eb2) put every request below into `shared/schemas` (map: vertex_grid_v1, bundleProp per D8, roads, id with the `^[^:]+$` pattern, provenance, collision.rectCell, mapObject kind/flip_x/group/image, portal edge, solid material; codeart: layout_spec_v1, parallax_spec_v1, plate_effects_v1, ambient_loop_v1). The tests validate against the real vendored schemas now. One follow-up for the schema owner: the `mapObject` description says an object's own footprint "is written as placed (layout_build writes it already mirrored)"; per D6 and forge_nav N6 every reader mirrors the object footprint for flip_x, so layout_build now writes it unmirrored and the sentence should say that readers mirror an object's footprint too. The JSON below is kept as the record of what was requested.

Every document B21 writes already validates against the frozen vendored schemas (`additionalProperties` is true):

- map-bundle.json against `map_bundle_v2`, and each `props` entry against `propItem`;
- layout-qa.json, parallax-qa.json and ambient-qa.json against `common/qaEnvelope`;
- both codeart-meta.json files against `codeart_meta_v1`;
- parallax-plan.json against `parallax_plan`.

The requests below document the optional fields and the new documents. The tests validate against the vendored schemas with these diffs applied in memory: `PROPOSED_*` constants in [tests/test_codeart2d_layout.py](../tests/test_codeart2d_layout.py) and [tests/test_codeart2d_parallax.py](../tests/test_codeart2d_parallax.py). The JSON below is generated from those constants and is identical to them. Producer: B21. Consumers: B13 (map_bundle.py, map_nav.py, export_tiled.py), B14, B17, agents.

Why each one:

- **`vertex_grid_v1`** describes the file that `map_bundle_v2.terrain.vertex_grid` points to; the schema leaves its format open today.
- **`props`** binds each object's `prop` id to its image, anchor, footprint and sha256. Without it, export_tiled and the scene preview cannot find prop images in a bundle.
- **`roads`** gives the centre lines in px, for NPC paths and debug overlays.
- **`id`** is the map id that portals' `to` and `entranceByFrom` refer to.
- **`provenance`** is the common provenance object.
- **`collision.rectCell`** is the raster cell of `collision.rects`; the rectangles are exact on it.
- **Object `kind`, `flip_x` and `group`**: the scatter group, and mirroring about the anchor. The footprint in the bundle is already mirrored, and `anchor_px` stays the unmirrored image anchor.
- **Portal `edge`** names the map edge of the exit.
- **Terrain solid `material`** names the blocking material.
- **The codeart.schema.json `$defs`** describe the three new input specs and the ambient loop document.

Semantics to record in the map contract (B13's layered-map-contract.md):

- `entranceByFrom` is `{destination map id: arrival spawn id}`.
- Portal `to` is `map` or `map:spawn`.
- Footprints are centred at `anchor_px + offset`, `width` across, `depth` along the ground.
- Solids test closed: a sample on a solid's boundary is blocked.

Other optional fields written:

| Document | Extra fields |
|---|---|
| parallax-plan.json layers | `sha256`, `kind` (`camera.pivot` is written as `top-left`) |
| parallax-qa.json | `seams` (seamReport per repeating layer), `sweep`, `layers` (size, rolled_px, pads) |
| ambient-qa.json | `seam` (seamReport inside the motion mask) |
| layout-qa.json | nothing extra |
| map-bundle.json `qa` | `{status, report, checks: {id: status}, reachability}`; `qa` is an open object |
| validate-parallax.json | the sibling's own report (generate2dmap.parallax_validation.v1), with `layers[].image` reduced to the file name (it printed absolute stage paths) |

#### map.schema.json, new `/$defs/vertex_grid_v1`

```json
{
  "description": "Terrain materials on the (W+1) x (H+1) vertex grid of a tile map (map_bundle_v2 terrain.vertex_grid). data[row][column] indexes materials; tile (x, y) has corners [data[y][x], data[y][x+1], data[y+1][x], data[y+1][x+1]] = [top_left, top_right, bottom_left, bottom_right], the tileset_v1 wang order. Each vertex owns the tile-sized square centred on it.",
  "type": "object",
  "required": ["schema", "size", "materials", "data"],
  "properties": {
    "schema": {"const": "generate2dmap.vertex_grid.v1"},
    "size": {"$ref": "common.schema.json#/$defs/size2"},
    "tile_size": {"anyOf": [{"type": "integer", "minimum": 1}, {"$ref": "common.schema.json#/$defs/size2"}]},
    "materials": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
    "base": {"type": "string", "minLength": 1},
    "data": {
      "type": "array",
      "minItems": 2,
      "items": {"type": "array", "minItems": 2, "items": {"type": "integer", "minimum": 0}}
    }
  }
}
```

#### map.schema.json, merge into `/$defs/map_bundle_v2/properties`

```json
{
  "id": {"type": "string", "minLength": 1},
  "props": {"type": "object", "additionalProperties": {"$ref": "#/$defs/propItem"}},
  "roads": {
    "type": "array",
    "items": {
      "type": "object",
      "required": ["id", "polyline"],
      "properties": {
        "id": {"type": "string", "minLength": 1},
        "material": {"type": "string", "minLength": 1},
        "width": {"type": "number", "exclusiveMinimum": 0},
        "polyline": {"type": "array", "minItems": 2, "items": {"$ref": "common.schema.json#/$defs/point2"}}
      }
    }
  },
  "provenance": {"$ref": "common.schema.json#/$defs/provenance"}
}
```

#### map.schema.json, merge into `/$defs/collision/properties`

```json
{"rectCell": {"type": "number", "exclusiveMinimum": 0}}
```

#### map.schema.json, merge into `/$defs/mapObject/properties`

```json
{
  "kind": {"type": "string", "minLength": 1},
  "flip_x": {"type": "boolean"},
  "group": {"type": "string", "minLength": 1}
}
```

#### map.schema.json, merge into `/$defs/portal/properties`

```json
{"edge": {"enum": ["west", "east", "north", "south"]}}
```

#### map.schema.json, merge into `/$defs/solid/properties`

```json
{"material": {"type": "string", "minLength": 1}}
```

#### codeart.schema.json, new `/$defs/layout_spec_v1`

```json
{
  "description": "Input of layout_build.py. size is in tiles; terrain shapes use tile (vertex) coordinates and paint in order; objects, spawns, interactions, scatter regions and clearances are world pixels.",
  "type": "object",
  "required": ["schema", "size", "materials"],
  "properties": {
    "schema": {"const": "codeart2d.layout_spec.v1"},
    "map_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$"},
    "size": {"$ref": "common.schema.json#/$defs/size2"},
    "tile_size": {"type": "integer", "minimum": 2},
    "seed": {"type": "integer", "minimum": 0},
    "materials": {
      "type": "object",
      "minProperties": 1,
      "additionalProperties": {
        "type": "object",
        "required": ["color"],
        "properties": {
          "color": {"$ref": "common.schema.json#/$defs/hexColor"},
          "walkable": {"type": "boolean"},
          "priority": {"type": "number"}
        }
      }
    },
    "base": {"type": "string", "minLength": 1},
    "terrain": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["shape", "material"],
        "properties": {
          "id": {"type": "string"},
          "shape": {"enum": ["ellipse", "rect", "polygon", "road"]},
          "material": {"type": "string"},
          "center": {"$ref": "common.schema.json#/$defs/point2"},
          "radius": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"$ref": "common.schema.json#/$defs/point2"}]},
          "rotate": {"type": "number"},
          "wobble": {"type": "number", "minimum": 0},
          "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
          "points": {"type": "array", "items": {"$ref": "common.schema.json#/$defs/point2"}},
          "width": {"type": "number", "exclusiveMinimum": 0},
          "smooth": {"type": "boolean"}
        }
      }
    },
    "hygiene": {"type": "object", "properties": {"saddles": {"type": "boolean"}, "specks": {"type": "boolean"}}},
    "actor": {
      "type": "object",
      "properties": {
        "radius": {"type": "number", "exclusiveMinimum": 0},
        "ySquash": {"type": "number", "exclusiveMinimum": 0}
      }
    },
    "prop_pack": {"$ref": "common.schema.json#/$defs/relPath"},
    "props": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "properties": {
          "image": {"$ref": "common.schema.json#/$defs/relPath"},
          "pixelspec": {"anyOf": [{"$ref": "#/$defs/pixelspec_v1"}, {"$ref": "common.schema.json#/$defs/relPath"}]},
          "pack": {"type": "string"},
          "variants": {"type": "array", "minItems": 1},
          "anchor_px": {"$ref": "common.schema.json#/$defs/point2"},
          "size": {"$ref": "common.schema.json#/$defs/size2"},
          "color": {"$ref": "common.schema.json#/$defs/hexColor"},
          "footprint": {"$ref": "map.schema.json#/$defs/footprint"},
          "solid": {"type": "boolean"},
          "occlusion": {"enum": ["low", "tall", "foreground"]},
          "flip": {"type": "boolean"},
          "scale": {"type": "number", "exclusiveMinimum": 0},
          "interactions": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["name"],
              "properties": {
                "name": {"type": "string"},
                "offset": {"$ref": "common.schema.json#/$defs/point2"},
                "reach": {"type": "number", "exclusiveMinimum": 0}
              }
            }
          }
        }
      }
    },
    "objects": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "prop", "x", "y"],
        "properties": {
          "id": {"type": "string"},
          "prop": {"type": "string"},
          "x": {"type": "number"},
          "y": {"type": "number"},
          "flip_x": {"type": "boolean"},
          "scale": {"type": "number", "exclusiveMinimum": 0},
          "variant": {"type": "integer", "minimum": 0}
        }
      }
    },
    "scatter": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["kinds", "count", "spacing"],
        "properties": {
          "id": {"type": "string"},
          "kinds": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {"type": "number", "exclusiveMinimum": 0}
          },
          "count": {"type": "integer", "minimum": 0},
          "spacing": {"type": "number", "exclusiveMinimum": 0},
          "attempts": {"type": "integer", "minimum": 1},
          "density": {"type": "object"},
          "clearance": {"type": "object"},
          "near": {"type": "object"},
          "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}
        }
      }
    },
    "exits": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "edge", "to"],
        "properties": {
          "id": {"type": "string"},
          "edge": {"enum": ["west", "east", "north", "south"]},
          "road": {"type": "string"},
          "span": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
          "to": {"type": "string", "minLength": 1},
          "depth": {"type": "number", "exclusiveMinimum": 0},
          "radius": {"type": "number", "minimum": 0},
          "arrival": {"type": "string"}
        }
      }
    },
    "spawns": {"type": "array", "items": {"type": "object", "required": ["id", "x", "y"]}},
    "interactions": {"type": "array", "items": {"type": "object", "required": ["id", "x", "y"]}}
  }
}
```

#### codeart.schema.json, new `/$defs/parallax_spec_v1`

```json
{
  "description": "Input of parallax_build.py: viewport and camera envelope as in parallax_plan, and layers back to front. Generated kinds (sky, ridge, clouds, foreground) are periodic in their period; image layers bring their own PNG.",
  "type": "object",
  "required": ["schema", "viewport", "layers"],
  "properties": {
    "schema": {"const": "codeart2d.parallax_spec.v1"},
    "viewport": {"$ref": "common.schema.json#/$defs/size2"},
    "camera": {
      "type": "object",
      "properties": {
        "x": {"$ref": "common.schema.json#/$defs/point2"},
        "y": {"$ref": "common.schema.json#/$defs/point2"},
        "zoom": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number", "exclusiveMinimum": 0}}
      }
    },
    "seed": {"type": "integer", "minimum": 0},
    "pixel_art": {"type": "boolean"},
    "sweep_frames": {"type": "integer", "minimum": 0},
    "layers": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["id", "kind"],
        "properties": {
          "id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"},
          "kind": {"enum": ["sky", "ridge", "clouds", "foreground", "image"]},
          "role": {"type": "string", "minLength": 1},
          "scroll": {"anyOf": [{"type": "number"}, {"$ref": "common.schema.json#/$defs/point2"}]},
          "repeat": {"type": "array", "items": {"type": "boolean"}, "minItems": 2, "maxItems": 2},
          "period": {"type": "integer", "minimum": 8},
          "seed": {"type": "integer", "minimum": 0},
          "colors": {"type": "array", "minItems": 1, "items": {"$ref": "common.schema.json#/$defs/hexColor"}},
          "fill": {"$ref": "common.schema.json#/$defs/hexColor"},
          "rim": {"$ref": "common.schema.json#/$defs/hexColor"},
          "shade": {"$ref": "common.schema.json#/$defs/hexColor"},
          "image": {"$ref": "common.schema.json#/$defs/relPath"},
          "alpha": {"enum": ["opaque", "transparent"]}
        },
        "if": {"properties": {"kind": {"const": "image"}}, "required": ["kind"]},
        "then": {"required": ["image", "role", "scroll"]}
      }
    }
  }
}
```

#### codeart.schema.json, new `/$defs/plate_effects_v1`

```json
{
  "description": "Input of ambient_bake.py: effects in feathered polygons of a static plate. Every effect period must divide the loop period_ms (default: their least common multiple).",
  "type": "object",
  "required": ["schema", "effects"],
  "properties": {
    "schema": {"const": "codeart2d.plate_effects.v1"},
    "plate": {"$ref": "common.schema.json#/$defs/relPath"},
    "units": {"enum": ["uv", "px"]},
    "period_ms": {"type": "integer", "minimum": 1},
    "frames": {"type": "integer", "minimum": 2},
    "fps": {"type": "number", "exclusiveMinimum": 0},
    "sampling": {"enum": ["bilinear", "nearest"]},
    "effects": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["id", "kind", "polygon"],
        "properties": {
          "id": {"type": "string", "minLength": 1},
          "kind": {"enum": ["ripple", "shimmer", "sway", "glow"]},
          "polygon": {"$ref": "common.schema.json#/$defs/polygon"},
          "period": {"type": "number", "exclusiveMinimum": 0},
          "amplitude": {"type": "number", "minimum": 0},
          "wavelength": {"type": "number", "exclusiveMinimum": 0},
          "axis": {"enum": ["x", "y"]},
          "feather": {"type": "number", "minimum": 0},
          "opacity": {"type": "number", "minimum": 0, "maximum": 1},
          "color": {"$ref": "common.schema.json#/$defs/hexColor"}
        }
      }
    },
    "protected": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "id": {"type": "string"},
          "polygon": {"$ref": "common.schema.json#/$defs/polygon"},
          "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}
        },
        "oneOf": [{"required": ["polygon"]}, {"required": ["box"]}]
      }
    }
  }
}
```

#### codeart.schema.json, new `/$defs/ambient_loop_v1`

```json
{
  "description": "ambient-loop.json from ambient_bake.py: a cycle loop of frame_count PNG frames lasting period_ms; durations_ms sum to period_ms; frame N would equal frame 0. art_source is code only when the plate itself is code art.",
  "type": "object",
  "required": [
    "schema",
    "plate",
    "size",
    "period_ms",
    "frame_count",
    "durations_ms",
    "fps",
    "frames",
    "mask",
    "effects",
    "art_source"
  ],
  "properties": {
    "schema": {"const": "codeart2d.ambient_loop.v1"},
    "plate": {"$ref": "common.schema.json#/$defs/fileRef"},
    "size": {"$ref": "common.schema.json#/$defs/size2"},
    "period_ms": {"type": "integer", "minimum": 1},
    "frame_count": {"type": "integer", "minimum": 2},
    "durations_ms": {"$ref": "common.schema.json#/$defs/durationsMs"},
    "fps": {"$ref": "common.schema.json#/$defs/fpsValue"},
    "loop": {"type": "object", "properties": {"policy": {"const": "cycle"}, "exact": {"type": "boolean"}}},
    "sampling": {"enum": ["bilinear", "nearest"]},
    "frames": {"type": "array", "minItems": 2, "items": {"$ref": "common.schema.json#/$defs/fileRef"}},
    "mask": {"$ref": "common.schema.json#/$defs/fileRef"},
    "effects": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["id", "kind", "cycles", "polygon"],
        "properties": {
          "id": {"type": "string"},
          "kind": {"enum": ["ripple", "shimmer", "sway", "glow"]},
          "cycles": {"type": "integer", "minimum": 1},
          "polygon": {"$ref": "common.schema.json#/$defs/polygon"}
        }
      }
    },
    "art_source": {"$ref": "common.schema.json#/$defs/artSource"},
    "plate_art_source": {"$ref": "common.schema.json#/$defs/artSource"},
    "motion_source": {"const": "code"},
    "provenance": {"$ref": "common.schema.json#/$defs/provenance"}
  }
}
```

## 6. Shared-helper promotion requests

**Status (Phase 3):** `_run_rects` became `forge_core.merge_rects` (D30); `SolidSet`, `Walker` and `navigate` became `forge_nav` (D4); `_local_write_codeart_meta` became `codeart_core.write_codeart_meta(placeholder=)` (D30); `_local_capped_edt` and `_inside_polygon` are one copy each in codeart_core (`distance_field`, `inside_polygon`), because D30's forge_core `distance_to` is Chebyshev. Open for the integrator: promote the Euclidean capped distance into forge_core if another skill needs it (S2 asked the same question). The original requests follow.

- `_local_capped_edt(target, cap)`: [layout_build.py:284](../skills/codeart2d/scripts/layout_build.py) and the identical [ambient_bake.py:150](../skills/codeart2d/scripts/ambient_bake.py).
  - Exact Euclidean distance to the nearest True cell where it is at most `cap`, infinity beyond. It is the numpy fallback for `scipy.ndimage.distance_transform_edt`, and gives bit-identical values up to the cap.
  - Callers: `_distance_field` (layout_build.py:263) and `_local_inside_distance` (ambient_bake.py:126).
  - Proposed home: forge_core, as `distance_to(mask, *, cap=None, inside=False) -> ndarray` (a forge_core API addition, keyword-only extras), honouring `FORGE_CORE_NO_SCIPY` like `label_components`.
  - Tests: `test_distance_fields_without_scipy_are_identical`, `test_feather_distance_without_scipy_is_identical` and `test_meadow_without_scipy_is_byte_identical` (a whole map built without scipy is byte-identical).
- `_run_rects(mask)`, [layout_build.py:239](../skills/codeart2d/scripts/layout_build.py): disjoint rectangles whose union is exactly a boolean mask (row runs merged downwards). B13 map_nav needs the same "rectangles whose union equals the blocked set".
  - Proposed: forge_core `merge_rects(mask) -> list[(x, y, w, h)]`.
  - Tests: `test_run_rects_union_is_exact` (6 seeds) and `test_rect_union_equals_blocked_set`.
- `_inside_polygon(xs, ys, polygon)`: even-odd test, in both layout_build.py and ambient_bake.py. Proposed: forge_core `points_in_polygon`.
- `SolidSet`, `Walker` and `navigate` (layout_build.py:1381, 1484, 1563): the private Appendix C BFS the plan asked for while B13 map_nav is built in parallel. Integration should make layout_build call map_nav's implementation (or move one copy into forge_core) and add the JS parity test. Semantics:
  - closed solid tests;
  - map bounds `[0, W] x [0, H]` inclusive;
  - 8 samples at k * 45 degrees on the actor ellipse;
  - cell centres at `(i + 0.5) * cell`, `cell = max(1, round_half_up(r / 2))`;
  - 4-neighbour moves also require the move midpoint to be valid, which is segmentClear at `cell / 2` spacing.
- `_local_write_codeart_meta(path, *, placeholder, **kwargs)`, [layout_build.py:1802](../skills/codeart2d/scripts/layout_build.py): `codeart_core.write_codeart_meta` always writes `placeholder: false` and refuses it as an extra field.
  - The helper writes through the library to a scratch name and stores the result with `placeholder: true` when the map uses flat-colour ground or placeholder props.
  - Request to the codeart_core owner: add a keyword-only `placeholder: bool = False` to `write_codeart_meta`, then delete the helper.

## 7. Cross-module links that Z must add

- codeart2d SKILL.md: link `references/layouts-and-parallax.md`, the three example files and `references/schemas/map.schema.json`; add the routing rows of section 2.
- generate2dmap SKILL.md: "code-made playable maps go through codeart2d layout_build.py, then map_nav.py"; "stylised parallax uses codeart2d parallax_build.py" (plan notes for Z).
- B13 map-strategies.md: spline roads, prop scatter and footprints should point to codeart2d layouts-and-parallax.md. layered-map-contract.md: add the optional bundle fields and semantics of section 5.
- B13 map_nav.py and map_bundle.py: run them on `layout_build` output in the e2e map pipeline (autotile_build, layout_build, map_bundle validate, map_nav, export_tiled/godot/ldtk, compose, scene preview). The private BFS is consolidated (forge_nav, D4); map_nav already finds the same nodes on the meadow. Optional: have layout_build call `map_bundle.py validate`.
- B20 autotile_build.py output is the expected `--tiles` input. The layer name comes from the manifest's optional `id`, else the joined material names. Its per-tile `collision` is the bundle's terrain collision (D5); a three-material set removes the hygiene demotions that two two-material sets force.
- B12 parallax-backgrounds.md: mention parallax_build as the code route. B12's new validate_parallax.py (pivot, coverage default) should be re-run on parallax_build output at integration; the plan sets `require_canvas_coverage: true` on every layer and an explicit top-left pivot.
- B16 background-scenes.md and scene_motion: ambient_bake's `frames/` and `mask.png` can feed the scene-motion encode and decoded-file QA. ambient-loop.json carries durations, the rational fps and the mask.
- B17 build_scene_preview and map-runtime.mjs: draw props from `bundle.props[object.prop].image` with `anchor_px`, mirroring `flip_x` about the anchor; sort by `sortY`, x, id. Object footprints are stored unmirrored (D6): mirror them for flip_x, as forge_nav N6 does; terrain collision of tiled bundles is the placed tiles' `collision` (D5).

## 8. Known limitations and what is not proven

**Platform.** Run only on Windows 11 with Python 3.13.2, numpy 2.5.3, Pillow 12.3.0 and scipy 1.18.1, on a machine shared with other agents. Not run on Linux or macOS, nor at the Python 3.10, Pillow 10.1 and numpy 1.26 floors. PNG, WebP and debug-text bytes are deterministic per Pillow/zlib/FreeType build; JSON outputs are deterministic, also without scipy (tested).

**layout_build:**

- Tested with synthetic tilesets and with real autotile_build output (a two-material Wang-16 set; the codeart review also ran a three-material set). blob47 and bevel tilesets are refused.
- With tilesets, the terrain collision is the tiles' own `collision` (D5). For a tileset without collision data the bundle's copy gets the vertex-square rule; a full tile whose stated collision contradicts the layout material's `walkable` only warns (`tile_collision`).
- Reachability is proven for one actor size (default radius `round(0.3 * tile)`, `ySquash` 1.0) with forge_nav (D4); map_nav agrees node for node on the meadow. Doors that open, jumping and moving objects are not simulated. B17's map-runtime.mjs agreement is the integration parity test's job.
- Hygiene with two-material sets can cut a road that runs beside water. The reachability gate reports it, but nothing prevents it.
- Exits are map-edge portals only. Doors are interactions, and `material_map` and `nav` are not written; map_nav produces the navigation grid.
- Scatter clearance is measured from the anchor point, so canopies may overhang roads (intended for tall props). Without a tileset the ground is flat colour and the bundle and codeart-meta say `placeholder: true`.
- Performance: 640x400 px about 1.7 s with previews. collision.rects at a fine tile collision grid (rectCell 4 or less) on a 4096 px map hold up to 16.7 million raster cells (the cap); the forge_nav grid is capped at 2^24 nodes.
- Scatter groups are capped at 250,000 candidate points (and requested props) each.

**parallax_build:**

- Generated kinds are sky, ridge, clouds and foreground. Image layers are measured, never repaired; pixel-art image layers need a whole-number scale.
- Canvases are capped at 16.7 Mpx (the composite at the minimum zoom included) and a sweep at 128 Mpx in all.
- The sweep follows the camera from minimum to maximum at the widest zoom, at integer positions and with a top-left pivot. Sub-pixel scrolling, other pivots and engine culling are not simulated.
- validate_parallax.py is B12's; the integrated version passes on the example. If B12's group changes its report format (edge_seam_report verdicts), parallax_build stores the new report as given.

**ambient_bake:**

- Displacement samples the plate itself, so pixels next to a region edge can be borrowed from outside it; the feather hides the seam.
- Math is float32: at most 1 level of 255 differs from float64, on rounding ties.
- A stage's `appliesTo` is ignored (protection applies to every effect). Loops are at most 60 s and 600 frames.
- No video is encoded.
- About 33 s for a 1672x941 plate x 48 frames on the loaded test machine.

**Deviations from the plan:**

1. **No codeart-meta.json from ambient_bake.** Its fixed disclosure "code-drawn, no image model" would be false for an image-model plate. ambient-loop.json records `plate_art_source`, `motion_source: code` and an honest disclosure instead.
2. **`--strict-qc` is opt-in for layout_build.** The roadmap says any unreachable exit fails. Without the flag the map is still published with QA status `fail`, so debug.png can be inspected; strict QC refuses to publish. This follows the repo's existing strict-QC pattern.
3. **The "sibling validator" is called as a subprocess by path** (allowed: calling a sibling CLI) and is skipped, recorded as such, when generate2dmap is not installed beside codeart2d.
4. **Tests are split:** ambient_bake's tests live in `tests/test_codeart2d_parallax.py`, because the plan lists no third test file.
