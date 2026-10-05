# B14-map-engines-chunks: Godot 4 and LDtk map exporters, room-chunk socket validator, side-scroll layout grammar validator

Branch `asf/B14-map-engines-chunks` (from `wip/asf-upgrade-20261005` @ 3f9252d). New files:
[export_godot.py](../skills/generate2dmap/scripts/export_godot.py),
[export_ldtk.py](../skills/generate2dmap/scripts/export_ldtk.py),
[validate_chunks.py](../skills/generate2dmap/scripts/validate_chunks.py),
[validate_layout.py](../skills/generate2dmap/scripts/validate_layout.py),
[engine-maps.md](../skills/generate2dmap/references/engine-maps.md) and the tests
[test_export_godot.py](../tests/test_export_godot.py) (13 tests),
[test_export_ldtk.py](../tests/test_export_ldtk.py) (11),
[test_validate_chunks.py](../tests/test_validate_chunks.py) (18) and
[test_validate_layout.py](../tests/test_validate_layout.py) (19). No existing file was changed.

Plan acceptance: B14-T1 `test_export_godot.py::ExportGodotTests::test_tres_parses_and_peering_bits` and
`::test_tile_map_data_roundtrip`; B14-T2 `test_export_ldtk.py::ExportLdtkTests::test_ldtk_required_fields` and
`::test_entities_positions_match_bundle`; B14-T3 `test_validate_chunks.py::...::test_width_mismatch_fails`,
`::test_two_by_two_layout_passes`, `::test_two_by_two_edge_graph_passes`; B14-T4
`test_validate_layout.py::...::test_three_row_deck_fails`, `::test_unjumpable_gap_fails`,
`::test_floating_prop_fails`; B14-T5 engine-maps.md (single-line commands; its two JSON examples are run by
`test_reference_doc_example_passes` in the validator tests).

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
- Errors: one `error: ...` line on stderr, exit 1, never a traceback (malformed bundles, markers, chunk files and layouts are reported by field). An existing `--output-dir` is refused.
- export_godot: writes `<name>.tileset.tres` (only when the bundle has a tiles layer), `<name>.tscn`, `assets/{tilesets,layers,props}/*.png` (byte-identical copies of the images the files use) and `godot-export.json` (`generate2dmap.engine_export.v1`: files, assets with source paths and sha256, tilesets with terrain mode, layers, counts, `notExported`, warnings, QA envelope). Before publishing, both files are re-read with the tool's Godot text parser; a failed round-trip check (tiles, peering bits, physics polygons, prop anchors, markers, asset bytes) publishes nothing. `--strict-qc` also refuses warnings (objects, spawns, portals or interactions outside the world). Summary keys: `output_dir`, `scene`, `tileset`, `metadata`, `status`, `tiles_layers`, `objects`.
- export_ldtk: writes `<name>.ldtk` (LDtk JSON 1.5.3, one level, Free layout), `assets/tilesets/*.png`, `assets/layers/*.png` (the level background), `assets/props-atlas.png` and `ldtk-export.json` (same schema). QA: required fields and types against the embedded LDtk 1.5.3 snapshot (`LDTK_REQUIRED`), uid/iid/identifier rules, decoded grid tiles vs the bundle, entity positions and sizes vs the bundle, asset bytes and atlas pixels. Fractional positions are rounded half up and warned; `--strict-qc` refuses them. Summary keys: `output_dir`, `project`, `metadata`, `status`, `entities`, `tiles`.
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
| `scripts/export_godot.py` | map_bundle.v2 to Godot 4.3+ `.tres` TileSet (terrain peering bits from Wang/blob data, physics polygons) and `.tscn` (TileMapLayer, y-sorted Sprite2D props on their anchors, collision, markers); parse-level verified, editor import not verified |
| `scripts/export_ldtk.py` | map_bundle.v2 to an LDtk 1.5.3 project (tilesets, Tiles layers, prop/spawn/portal/interaction/anchor entities, level background); required fields checked against a snapshot, LDtk not run |
| `scripts/validate_chunks.py` | room_chunk.v1: socket spans and materials on shared edges, layout rows or edge graph, chunk-graph and walk-grid reachability, debug image |
| `scripts/validate_layout.py` | layout.v1: segments, slopes and ledges, deck thickness, prop support, gaps vs the jump arc, arena width at 16:9 and 19.5:9, reachability, side-view debug image |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `generate2dmap/scripts/export_godot.py` | Exports a map bundle to a Godot 4.3+ TileSet and TileMapLayer scene with props, collision and markers (exported data; editor import not verified) | `tests/test_export_godot.py`: independent regex/struct read-back of peering bits, physics polygons and `tile_map_data`; anchors; determinism |
| `generate2dmap/scripts/export_ldtk.py` | Exports a map bundle to an LDtk 1.5.3 project (editor import not verified) | `tests/test_export_ldtk.py`: required-field snapshot plus an independent loader-field list, tiles and entity positions read back |
| `generate2dmap/scripts/validate_chunks.py` | Checks room-chunk door sockets, stitching and reachability | `tests/test_validate_chunks.py`: width mismatch fails, 2x2 layouts pass, doors into walls and cut corridors fail |
| `generate2dmap/scripts/validate_layout.py` | Checks a side-scroll level against jump, slope, deck and arena rules | `tests/test_validate_layout.py`: 3-row deck, unjumpable gap and floating prop fail; parabola numbers |

## 4. CHANGELOG entries

- Added: `export_godot.py`, Godot 4.3+ export of map_bundle.v2: TileSetAtlasSource per tileset; terrain sets in Match Corners mode (Wang) or Match Corners and Sides mode (blob-47) with peering bits; physics polygons from tile collision shapes; a `walkable` custom data layer; TileMapLayer per tiles layer; Sprite2D props whose origin sits on (x, sortY) with the anchor offset; StaticBody2D collision; Marker2D and Area2D markers; parse-back QA before publishing (B14-T1; roadmap P2-1; MAP-01, F-07).
- Added: `export_ldtk.py`, LDtk 1.5.3 export: tileset definitions with per-tile data in customData, Tiles layers, Entities for props (pivot = anchor), spawns, portals, interactions and anchors, the bottom image layer as the level background, and a required-field check against an embedded LDtk JSON snapshot (B14-T2; roadmap P2-1; MAP-01, F-07).
- Added: `validate_chunks.py` for room_chunk.v1: socket bounds and overlaps, layout rows (with chunk reuse as instances) or edge graphs, exact socket pairing (offset, width, material) across shared edges, dangling sockets, chunk-graph reachability, and walkability-grid reachability through doors with a debug image (B14-T3; roadmap P2-6; room_chunk_mode).
- Added: `validate_layout.py` for layout.v1: contiguous segments, slope and step limits, deck thickness (a 3-row deck fails by default), prop support per footprint column, gaps against a parabolic jump arc, arena width at 16:9 and 19.5:9 plus listed viewports, reachability from the first spawn, side-view debug image (B14-T4; owner side-scroller rules).
- Added: `references/engine-maps.md`: Godot and LDtk mappings, what is parse-level verified versus not verified in an editor, room chunk and layout authoring (B14-T5; roadmap 6.1 #7).
- Changed: none. BREAKING: none (new tools only).
- Fixed: MAP-01, DOC-05, F-07 in part: playable maps now export to Godot and LDtk data from the bundle (editor import is still not verified, so README claims stay "exported data").

## 5. Schema change requests

All against `shared/schemas/map.schema.json` (vendored into generate2dmap and codeart2d), additive. The tests apply exactly these fragments in memory and validate every report the tools write, plus the test fixtures and the engine-maps.md examples they read, (`REQUESTED_MAP_DEFS` and `REQUESTED_MAP_PATCHES` in the four test files; `requested_contract_errors`). `NEWDEF name` adds `$defs/name`; `PATCH pointer` sets the value at that JSON pointer (new properties, or replacing an untyped `{"type": "object"}` / `{"type": ["object", "array"]}` item schema). Existing valid fixtures stay valid.

Producers and consumers: `engine_export_v1` written by export_godot.py and export_ldtk.py, read by agents and Z docs; `chunk_validation_v1` and `layout_validation_v1` written by the validators, read by agents. The input patches document fields the B14 tools read: layout_v1 (agent-written, read by validate_layout), room_chunk_v1 (agent-written, read by validate_chunks), map_bundle_v2 `prop_packs` / `mapObject.image` (written by B21 layout_build or agents, read by B14 exporters and ideally B13 export_tiled), tileset_v1 `blob_inside` (written by B20 autotile_build, read by export_godot).

Also amend the `mapLayer` description (text only): "tiles layers: `data` is rows, top first, of the tileset's tile `index` values, `-1` or `null` for an empty cell; the grid is ceil(world / tile_size) cells; a tile index i sits at atlas column i % columns, row i // columns."

```text
NEWDEF engine_export_v1
{
  "description": "export_godot.py and export_ldtk.py report: the engine files written from one map bundle, the copied assets, what the engine files do not carry, and the parse-back QA.",
  "type": "object",
  "required": ["schema", "tool", "engine", "bundle", "files", "assets", "notExported", "warnings", "qa"],
  "properties": {
    "schema": {"const": "generate2dmap.engine_export.v1"},
    "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
    "engine": {
      "type": "object",
      "required": ["name", "target", "verified"],
      "properties": {
        "name": {"enum": ["godot", "ldtk"]},
        "target": {"type": "string", "minLength": 1},
        "format": {"type": ["integer", "string"]},
        "verified": {"type": "string", "minLength": 1}
      }
    },
    "bundle": {"$ref": "common.schema.json#/$defs/fileRef"},
    "files": {
      "type": "object",
      "additionalProperties": {"anyOf": [{"$ref": "common.schema.json#/$defs/relPath"}, {"type": "null"}]}
    },
    "assets": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["role", "id", "path", "sha256"],
        "properties": {
          "role": {"enum": ["tileset", "layer", "prop"]},
          "id": {"type": "string", "minLength": 1},
          "path": {"$ref": "common.schema.json#/$defs/relPath"},
          "sha256": {"$ref": "common.schema.json#/$defs/sha256"},
          "source": {"$ref": "common.schema.json#/$defs/relPath"}
        }
      }
    },
    "tilesets": {"type": "array", "items": {"type": "object"}},
    "layers": {"type": "array", "items": {"type": "object"}},
    "counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
    "level": {"type": "object"},
    "notExported": {"type": "array", "items": {"type": "string"}},
    "warnings": {"type": "array", "items": {"type": "string"}},
    "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
  }
}
NEWDEF chunk_validation_v1
{
  "description": "validate_chunks.py report: placements, socket pairing per shared edge, chunk-graph and walkability-grid reachability, and the QA envelope.",
  "type": "object",
  "required": ["schema", "tool", "status", "mode", "placements", "sockets", "qa"],
  "properties": {
    "schema": {"const": "generate2dmap.chunk_validation.v1"},
    "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
    "status": {"enum": ["pass", "warn", "fail"]},
    "mode": {"enum": ["rows", "edges", "kit"]},
    "start": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "object",
          "required": ["chunk"],
          "properties": {
            "chunk": {"type": "string", "minLength": 1},
            "point": {"anyOf": [{"type": "null"}, {"$ref": "common.schema.json#/$defs/point2"}]}
          }
        }
      ]
    },
    "unused": {"type": "array", "items": {"type": "string"}},
    "unplaced": {"type": "array", "items": {"type": "string"}},
    "placements": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "rect"],
        "properties": {
          "id": {"type": "string", "minLength": 1},
          "rect": {"$ref": "common.schema.json#/$defs/rectXYWH"}
        }
      }
    },
    "contacts": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["a", "side", "b", "start", "end"],
        "properties": {
          "a": {"type": "string"},
          "b": {"type": "string"},
          "side": {"enum": ["E", "S"]},
          "b_side": {"enum": ["W", "N"]},
          "start": {"type": "number"},
          "end": {"type": "number"}
        }
      }
    },
    "sockets": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["chunk", "side", "index", "offset", "width", "status"],
        "properties": {
          "chunk": {"type": "string"},
          "side": {"enum": ["N", "E", "S", "W"]},
          "index": {"type": "integer", "minimum": 0},
          "offset": {"type": "number"},
          "width": {"type": "number"},
          "material": {"type": ["string", "null"]},
          "status": {"enum": ["paired", "mismatch", "straddle", "dangling", "unplaced"]},
          "world_span": {
            "anyOf": [
              {"type": "null"},
              {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
            ]
          },
          "partner": {"anyOf": [{"type": "null"}, {"type": "object"}]}
        }
      }
    },
    "reachability": {"anyOf": [{"type": "null"}, {"type": "object"}]},
    "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
  }
}
NEWDEF layout_validation_v1
{
  "description": "validate_layout.py report: standable spans, ledges and steep faces, gaps with their jump numbers, prop support, arena widths, reachability, and the QA envelope.",
  "type": "object",
  "required": ["schema", "tool", "status", "extent", "physics", "spans", "gaps", "props", "qa"],
  "properties": {
    "schema": {"const": "generate2dmap.layout_validation.v1"},
    "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
    "status": {"enum": ["pass", "warn", "fail"]},
    "extent": {
      "type": "object",
      "required": ["x0", "x1"],
      "properties": {"x0": {"type": "integer"}, "x1": {"type": "integer"}}
    },
    "physics": {
      "type": "object",
      "required": [
        "jumpHeight",
        "jumpDistance",
        "maxSlopeDeg",
        "stepUp",
        "minDeckThickness",
        "groundTolerance"
      ]
    },
    "spans": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "kind", "x0", "x1", "reachable"],
        "properties": {
          "id": {"type": "string"},
          "kind": {"enum": ["ground", "deck"]},
          "x0": {"type": "integer"},
          "x1": {"type": "integer"},
          "top_min": {"type": "number"},
          "top_max": {"type": "number"},
          "reachable": {"type": "boolean"}
        }
      }
    },
    "pieces": {
      "type": "array",
      "items": {"type": "object", "required": ["kind"], "properties": {"kind": {"enum": ["ledge", "steep"]}}}
    },
    "gaps": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["x0", "x1", "width", "kind", "crossable"],
        "properties": {
          "kind": {"enum": ["gap", "hazard"]},
          "crossable": {"type": ["boolean", "null"]},
          "direct": {
            "anyOf": [
              {"type": "null"},
              {"type": "object", "required": ["distance", "rise", "reach", "feasible"]}
            ]
          }
        }
      }
    },
    "props": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "x", "y", "status"],
        "properties": {"status": {"enum": ["supported", "floating", "sunk", "allowed"]}}
      }
    },
    "arenas": {
      "type": "array",
      "items": {"type": "object", "required": ["arena", "viewport", "needed", "status"]}
    },
    "reachability": {"type": "object"},
    "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
  }
}
PATCH /$defs/map_bundle_v2/properties/prop_packs
{
  "description": "Prop pack manifests whose accepted labels give objects[].prop its image (after objects[].image, before occluder.source).",
  "type": "array",
  "items": {
    "type": "object",
    "required": ["manifest"],
    "properties": {
      "manifest": {"$ref": "common.schema.json#/$defs/relPath"},
      "sha256": {"$ref": "common.schema.json#/$defs/sha256"}
    }
  }
}
PATCH /$defs/mapObject/properties/image
{"$ref": "common.schema.json#/$defs/relPath", "description": "The prop's image (relative to the bundle)."}
PATCH /$defs/mapObject/properties/image_sha256
{"$ref": "common.schema.json#/$defs/sha256"}
PATCH /$defs/tileset_v1/properties/blob_inside
{
  "description": "blob47: the material that is the blob's inside (default: the last material).",
  "type": "string",
  "minLength": 1
}
PATCH /$defs/room_chunk_v1/properties/cell
{"description": "Pixels per grid character (chunks may override).", "type": "integer", "minimum": 1}
PATCH /$defs/room_chunk_v1/properties/start
{
  "description": "Start chunk (or chunk instance such as corridor@0,1), optionally with a point in its pixels.",
  "anyOf": [
    {"type": "string", "minLength": 1},
    {
      "type": "object",
      "required": ["chunk"],
      "properties": {
        "chunk": {"type": "string", "minLength": 1},
        "x": {"type": "number"},
        "y": {"type": "number"}
      }
    }
  ]
}
PATCH /$defs/room_chunk_v1/properties/chunks/items/properties/grid
{
  "description": "Walkability rows, top first: '.' walkable, '#' blocked; size = grid size x cell.",
  "type": "array",
  "minItems": 1,
  "items": {"type": "string", "pattern": "^[.#]+$"}
}
PATCH /$defs/room_chunk_v1/properties/chunks/items/properties/cell
{"type": "integer", "minimum": 1}
PATCH /$defs/room_chunk_v1/properties/graph/items
{
  "description": "Either layout rows of chunk ids (null for an empty cell; a repeated id is one instance per cell) or edges: b sits on a's side, shifted offset px along it.",
  "anyOf": [
    {"type": "array", "items": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]}},
    {
      "type": "object",
      "required": ["from", "to", "side"],
      "properties": {
        "from": {"type": "string", "minLength": 1},
        "to": {"type": "string", "minLength": 1},
        "side": {"enum": ["N", "E", "S", "W"]},
        "offset": {"type": "number"}
      }
    }
  ]
}
PATCH /$defs/layout_v1/properties/surface
{
  "description": "Ground surface polyline [[x, y], ...] in world pixels (y down); x never decreases and a repeated x is a vertical step. Give surface or groundY.",
  "type": "array",
  "minItems": 2,
  "items": {"$ref": "common.schema.json#/$defs/point2"}
}
PATCH /$defs/layout_v1/properties/groundY
{"description": "Flat ground height when there is no surface.", "type": "number"}
PATCH /$defs/layout_v1/properties/kinds
{
  "description": "Extra segment kinds and the class each one maps to.",
  "type": "object",
  "additionalProperties": {"enum": ["ground", "gap", "hazard"]}
}
PATCH /$defs/layout_v1/properties/props/items
{
  "description": "A prop: (x, y) is the middle of its base and w its width; floating opts out of the support check. Kinds deck, platform, plank and bridge are one-way standable surfaces {x0, x1, y, thickness} (or x and w).",
  "type": "object",
  "properties": {
    "id": {"type": "string", "minLength": 1},
    "kind": {"type": "string"},
    "x": {"type": "number"},
    "y": {"type": "number"},
    "w": {"type": "number", "minimum": 0},
    "x0": {"type": "number"},
    "x1": {"type": "number"},
    "thickness": {"type": "number", "minimum": 0},
    "floating": {"type": "boolean"}
  }
}
PATCH /$defs/layout_v1/properties/spawns/items
{
  "type": "object",
  "required": ["x", "y"],
  "properties": {"id": {"type": "string", "minLength": 1}, "x": {"type": "number"}, "y": {"type": "number"}}
}
PATCH /$defs/layout_v1/properties/exits
{
  "description": "Places that must be reachable from the first spawn (default: the level end).",
  "type": "array",
  "items": {
    "type": "object",
    "required": ["x"],
    "properties": {"id": {"type": "string", "minLength": 1}, "x": {"type": "number"}, "y": {"type": "number"}}
  }
}
PATCH /$defs/layout_v1/properties/arenas
{
  "description": "Ranges that must be at least the view width plus camera travel (default: the level).",
  "type": "array",
  "items": {
    "type": "object",
    "required": ["x0", "x1"],
    "properties": {
      "id": {"type": "string", "minLength": 1},
      "x0": {"type": "number"},
      "x1": {"type": "number"},
      "travel": {"type": "number", "minimum": 0}
    }
  }
}
PATCH /$defs/layout_v1/properties/camera
{
  "type": "object",
  "properties": {
    "viewHeight": {"type": "number", "exclusiveMinimum": 0},
    "travel": {"type": "number", "minimum": 0}
  }
}
PATCH /$defs/layout_v1/properties/physics/properties/minDeckThickness
{"type": "number", "minimum": 0}
PATCH /$defs/layout_v1/properties/physics/properties/groundTolerance
{"type": "number", "minimum": 0}
PATCH /$defs/layout_v1/not
{"required": ["surface", "groundY"]}
```

Optional fields the B14 tools read that the patches above document: layout_v1 `surface`, `groundY`, `kinds`, `exits`, `arenas`, `camera.viewHeight`, `camera.travel`, `physics.minDeckThickness`, `physics.groundTolerance`, deck props (`kind`, `x0`, `x1`, `thickness`), prop `w` and `floating`; room_chunk_v1 `cell`, `start`, chunk `grid` and `cell`, graph rows with `null` and repeated ids, edge `offset`; map_bundle_v2 `prop_packs`, `objects[].image`, `objects[].image_sha256`; tileset_v1 `blob_inside`. The report documents also carry fields beyond the `required` lists (for example `tilesets`, `layers`, `counts`, `level`, `pieces`, `arenas`, `reachability`); the NEWDEF fragments list them.

## 6. Shared-helper promotion requests

- `_local_file_ref(path, base_dir, sha256=None) -> dict` ([export_godot.py:458](../skills/generate2dmap/scripts/export_godot.py#L458); the same helper without `sha256` at [validate_chunks.py:875](../skills/generate2dmap/scripts/validate_chunks.py#L875) and [validate_layout.py:798](../skills/generate2dmap/scripts/validate_layout.py#L798); B12's conform_background has another copy): a common `fileRef` with the manifest-relative POSIX path, or only the file name when `forge_core.portable_path` finds no relative route (another drive). Promote to forge_core as `file_ref(path, base_dir, sha256=None)` next to `portable_path` (Appendix A, hashing and paths); tests: same-drive relative path, another drive gives the basename, sha256 passthrough.
- `_local_safe_name(text, taken, fallback="item") -> str` ([export_godot.py:446](../skills/generate2dmap/scripts/export_godot.py#L446)): ASCII file stem, unique case-insensitively within `taken`, with a short sha256 suffix when nothing ASCII is left (CJK labels). Candidate for forge_core next to B10's label slugging; optional.
- `_local_grid_reachability(...)` ([validate_chunks.py:525](../skills/generate2dmap/scripts/validate_chunks.py#L525)) with `_UnionFind` ([validate_chunks.py:490](../skills/generate2dmap/scripts/validate_chunks.py#L490)): per-chunk 4-connected components (`forge_core.label_components(connectivity=4)`) joined through paired doors. Per plan B14-T3, integration consolidates it into B13 map_nav's grid search (not forge_*).
- The map_bundle.v2 reader in export_godot.py: `_local_read_bundle` (:349), `_local_load_tileset` (:244), `_local_layer_grid` (:290), `_local_prop_pack_images` (:334), `_local_check_markers` (:186), `_local_rel_path` (:127), `_local_check_sha` (:138), `_local_png_info` (:146), `_local_used_tilesets` (:466), `_local_not_exported` (:475), `_local_world_warnings` (:488). export_ldtk.py imports them from export_godot.py. Move into B13's map_bundle.py at integration (one reader for export_tiled, export_godot, export_ldtk and map_nav); this is not a forge_* change.
- Godot text helpers in export_godot.py: `gd_value` (:540), `GdWriter` (:592), `parse_godot_text` (:818), `decode_tile_map_data` (:823), `encode_tile_map_data` (:987). B09's export_engine also writes Godot `.tres` (SpriteFrames) in generate2dsprite; if both should share one writer/reader, add `shared/forge_godot.py` vendored into generate2dsprite and generate2dmap. Optional; nothing in Appendix A covers it today.

## 7. Cross-module links that Z must add

- generate2dmap SKILL.md: the section 2 rows; link references/engine-maps.md under engine targets, `room_chunk_mode` and `side_scroll_mode`. Notes for Z from the plan: "Map SKILL.md tools: export_godot, export_ldtk, validate_chunks (room_chunk_mode), validate_layout (side_scroll_mode). No editor-compatibility claims until manual import."
- map-strategies.md (B13 owns): the "chunk sockets" part should point to engine-maps.md, section "Room chunks".
- side-scroll-scenes.md (B11 owns): its "layout validator pointer" (B11-T4) should point to engine-maps.md, section "Side-scroll layouts", and validate_layout.py.
- layered-map-contract.md and export_tiled.py (B13): use the same prop-image resolution (objects[].image, then prop packs by label via prop_packs or a CLI flag, then occluder.source) and the same tiles-layer data convention (rows of tileset indices, -1/null empty) so Tiled, Godot and LDtk exports of one bundle agree.
- B20 autotile_build: write `blob_inside` on blob47 tilesets (or keep the inside material last) and keep `wang` as [tl, tr, bl, br]; export_godot maps both into Godot terrain sets.
- B21 layout_build: write `objects[].image` (with `image_sha256`) or a `prop_packs` list, so the exporters find prop images without a CLI flag.
- Integration e2e pipeline 4 (Map): after `export_tiled`, run `export_godot.py --bundle <bundle> --output-dir <new>` and `export_ldtk.py --bundle <bundle> --output-dir <new>` (both refuse existing folders); consolidate `_local_grid_reachability` into map_nav and the bundle reader into map_bundle.py (section 6).
- README (Z): the Godot section stays "exported data; editor import not verified" (MAP-01, DOC-05, F-07); name LDtk the same way. engine-maps.md lists what each export does not carry.

## 8. Known limitations and what is not proven

- No engine is installed here. Godot and LDtk outputs are verified at parse level only: re-read by the exporters' own readers and, in the tests, by independent regexes, `struct` decoding and an independent list of loader fields. Nobody has opened them in Godot or LDtk; do not claim editor compatibility until a manual import.
- Godot format facts: the TileSetAtlasSource tile keys (`x:y/0 = 0`), `sources/N`, and the TileMapLayer `tile_map_data` layout (uint16 format 0, then int16 x, int16 y, uint16 source, atlas x, atlas y, alternative per cell) were checked against a scene saved by Godot 4.5 on this machine. The terrain property names (`terrain_set`, `terrain`, `terrains_peering_bit/<bit>`, `terrain_set_N/mode`, `terrain_set_N/terrain_M/name|color`), physics (`physics_layer_0/polygon_N/points`, `physics_layer_0/collision_layer`) and custom data (`custom_data_layer_0/name|type`, `custom_data_0`) are from the Godot 4 source as remembered, not checked against a saved file. Layers are written before `sources/N` in the TileSet resource on purpose (Godot resizes tile data when a source is attached).
- Format targets: Godot 4.3+ (TileMapLayer), text format 3 with integer-list PackedByteArray (readable by 4.3 to 4.5); the legacy 4.0-4.2 TileMap node is not written. LDtk JSON 1.5.3; `appBuildId` is 0 (not built by LDtk); the required-field snapshot `LDTK_REQUIRED` was hand-transcribed from the LDtk 1.5.3 JSON documentation without network access, so a field LDtk requires but the snapshot misses would go unnoticed.
- Terrain semantics are a documented mapping, not editor-proven: a mixed Wang tile's centre terrain is its majority corner (ties: the lower material index); blob-47 neighbours that are not connected get the other material, or no bit with one material. Painting with Godot's terrain brush or LDtk rules is not verified.
- Deviation (B14-T2): LDtk terrain is exported as Tiles layers (exact placement), not IntGrid plus auto-layer rules. The plan allowed either; corner-Wang tiles need dual-grid rules that cannot be checked without LDtk. Per-tile wang/blob/collision/walkable data rides in tileset customData.
- Not exported (and listed in each report's `notExported`): to Godot, the material map, nav grid, terrain vertex grid, camera, stage, atmosphere, lights and animatedParts (walk regions ride as `metadata/walk_regions`); to LDtk, additionally the collision shapes and walk regions, and image layers other than the bottom one.
- LDtk positions and sizes are whole pixels: fractional bundle values are rounded half up (warned; `--strict-qc` refuses).
- validate_layout's model: the actor is a point at its feet (no collider width, head room or ceilings); one parabola (apex jumpHeight, range jumpDistance) with full air control; decks are one-way and never block an arc; a slope steeper than maxSlopeDeg that rises above stepUp fails (draw it as a vertical ledge); camera travel defaults to 0 when not given; arena checks at aspect ratios need `camera.viewHeight` or a [w, h] viewport, else they are skipped (reported). Jump candidates are sampled (at most 64 columns per span and 32 arcs per span pair), so an exotic route can be missed (a false failure, never a false pass).
- validate_chunks: walk grids are top-down 4-neighbour; side-view rooms should use validate_layout. Sockets are axis-aligned edge spans; rotated or mirrored chunk variants are not generated. Reuse of one chunk id works in layout rows (instances `id@row,col`), not in edge graphs. Grid reachability runs only when every placed chunk has a grid (otherwise reported as skipped).
- Measured on this machine (Windows 11, Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1; shared with other agents): a 256x256-tile bundle with 2000 props exports to Godot in 1.6 s and to LDtk in 1.9 s; a 10x10 chunk grid with 40x30-cell walk grids validates in 0.9 s; a 16,000 px level with 40 gaps and 120 decks in 0.7 s. One run each; no perf tests were added.
- Not run on Linux, macOS, Python 3.10 or Pillow 10.1. The sources avoid newer APIs (dict union `|` needs 3.9+, fine for the 3.10 floor).
- Test structure: `tests/test_export_ldtk.py` imports `build_bundle` and `requested_contract_errors` from `tests/test_export_godot.py` (one synthetic bundle and one copy of the requested schema helper).
