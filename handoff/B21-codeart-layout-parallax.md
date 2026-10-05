# B21-codeart-layout-parallax: codeart2d layout_build (playable map_bundle.v2 with reachability and exact collision rectangles), parallax_build (periodic layers, sibling validator, 49-frame sweep) and ambient_bake (exact-period plate loops)

Branch `asf/B21-codeart-layout-parallax` (from `wip/asf-upgrade-20261005` @ 3f9252d). New files: [layout_build.py](../skills/codeart2d/scripts/layout_build.py), [parallax_build.py](../skills/codeart2d/scripts/parallax_build.py), [ambient_bake.py](../skills/codeart2d/scripts/ambient_bake.py), [examples/meadow-layout.json](../skills/codeart2d/examples/meadow-layout.json), [examples/parallax-gen.json](../skills/codeart2d/examples/parallax-gen.json), [examples/plate-effects.json](../skills/codeart2d/examples/plate-effects.json), [references/layouts-and-parallax.md](../skills/codeart2d/references/layouts-and-parallax.md), [tests/test_codeart2d_layout.py](../tests/test_codeart2d_layout.py) (27 tests), [tests/test_codeart2d_parallax.py](../tests/test_codeart2d_parallax.py) (20 tests).

## 1. CLIs

Run from the user's project root (`<skill-dir>` is the codeart2d folder, `${CLAUDE_SKILL_DIR}` in Claude Code):

    python "<skill-dir>/scripts/layout_build.py" --spec meadow-layout.json --output-dir out/map-v1 --seed 7 --preview --strict-qc
    python "<skill-dir>/scripts/layout_build.py" --spec meadow-layout.json --tiles out/tiles-v1/tileset-manifest.json --output-dir out/map-v2 --seed 7 --preview --strict-qc
    python "<skill-dir>/scripts/parallax_build.py" --spec parallax-gen.json --output-dir out/bg-v1 --validate --sweep-frames 49
    python "<skill-dir>/scripts/ambient_bake.py" --plate art/harbor-plate.png --spec plate-effects.json --output-dir out/harbor-ambient-v1 --preview --strict-qc

Flags:

- layout_build: `--spec`, `--output-dir`, `--tiles MANIFEST` (repeatable; tileset_v1 `wang_corner` or `flat`), `--seed` (overrides the spec), `--preview` (preview.png and debug.png), `--strict-qc`, `--rect-cell PX` (default half a tile), `--debug-scale 1-4`.
- parallax_build: `--spec`, `--output-dir`, `--validate` / `--no-validate` (default: run the sibling validate_parallax.py when it is found), `--validator PATH`, `--sweep-frames N` (default 49; 0 turns the sweep off), `--strict-qc`.
- ambient_bake: `--plate`, `--spec` (codeart2d.plate_effects.v1 or generate2dmap.stage.v1), `--output-dir`, `--frames`, `--fps`, `--preview` (review.png, preview.webp), `--plate-art-source code|host_image|api|existing|video|mixed` (default existing), `--strict-qc`.

All three follow plan Appendix D:

- `utf8_stdio()` first; work in `forge_core.staged_output`; an existing `--output-dir` is refused before any work.
- `--strict-qc`: a failed check exits 1 and publishes nothing. Without it the output is published with a failing QA envelope (status `fail`, exit 0), so the user can open debug.png.
- Errors are one ASCII `error: ...` line on stderr with exit 1.
- `--help` is ASCII under cp1252 and cp950; tested with `assert_cli_help`.

The one-line JSON summary reports:

| Tool | Keys |
|---|---|
| layout_build | `status`, `output`, `bundle` (map-bundle.json), `metadata` (codeart-meta.json), `qa` (layout-qa.json), `objects`, `solids`, `rects`, `portals`, `reachable_fraction` |
| parallax_build | `status`, `output`, `plan` (parallax-plan.json), `metadata` (codeart-meta.json), `qa` (parallax-qa.json), `layers`, `sweep_frames`, `validated` |
| ambient_bake | `status`, `output`, `metadata` (ambient-loop.json), `qa` (ambient-qa.json), `frames`, `period_ms`, `fps` (rational string) |

## 2. SKILL.md routing rows

codeart2d SKILL.md:

| Need | Route |
|---|---|
| A playable top-down map made from code (village, meadow, dungeon floor) with roads, props, exits, collision and a reachability gate | `scripts/layout_build.py --spec <layout.json> --output-dir <new> --preview --strict-qc` (add `--tiles <tileset manifest>` from autotile_build.py for real tiles); open debug.png; then generate2dmap `map_nav.py` and the engine exporters. Spec: `references/layouts-and-parallax.md` |
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
| `codeart2d/scripts/layout_build.py` | Builds a playable top-down map_bundle.v2 from a layout spec: vertex-grid terrain, spline roads, corner-Wang tiles, seeded prop variety, exact collision rectangles, exits with arrival spawns, preview and debug overlay | `tests/test_codeart2d_layout.py`: independent Appendix C BFS reaches every exit; rectangle union equals the blocked raster; seeded byte-identity, also without scipy; vendored map schema |
| `codeart2d/scripts/parallax_build.py` | Periodic code-art parallax layers, a parallax plan, the generate2dmap validator and a 49-frame camera sweep | `tests/test_codeart2d_parallax.py`: double-width render equals the tiled layer; loop step at most p95; validate_parallax passed; 49-frame sweep.webp |
| `codeart2d/scripts/ambient_bake.py` | Bakes ripple, shimmer, sway and glow loops inside feathered polygons of a plate | `tests/test_codeart2d_parallax.py`: frame N equals frame 0; pixels outside the polygons equal the plate in every frame |

## 4. CHANGELOG entries

- Added: `codeart2d/scripts/layout_build.py` (B21-T1). Builds a playable top-down map (generate2dmap.map_bundle.v2) from a code spec:
  - terrain painted on a vertex grid (ellipses with seeded wobble, rects, polygons, Catmull-Rom spline roads);
  - hygiene so every cell is drawable by the given corner-Wang tilesets, with saddles and lone vertices removed;
  - autotiling with interior variants that avoid repeating neighbours;
  - props from images, inline PixelSpecs, prop-pack v2 items or flat placeholders;
  - seeded scatter with weighted kinds, variants and mirroring; Poisson-disk spacing, density noise, clearance and near-bands; a same-look neighbour check;
  - exact terrain collision (merged vertex squares), footprint solids scaled once, and merged collision rectangles equal to the blocked raster;
  - Appendix C reachability (footprint ellipse at cell centres and move midpoints) for every exit, spawn and interaction;
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
- Changed: none. BREAKING: none (new tools; no default of an existing tool changes).
- Fixed: the battle-stage runtime's water displacement (`sin(q * 0.57 - phase * 0.37)`) does not repeat after one period. Baked loops use integer harmonics instead, with a regression test (plan B21-T3 source: Dusk battle-atmosphere.js L31-155).
- Fixed, in part: MAP-01, DOC-05 and F-07 (no engine-usable map data, no collision derived from data). Code-made maps now carry data-derived collision, reachability and portals; the exporters are B13 and B14.

## 5. Schema change requests

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
- B13 map_nav.py and map_bundle.py: run them on `layout_build` output in the e2e map pipeline (autotile_build, layout_build, map_bundle validate, map_nav, export_tiled/godot/ldtk, compose, scene preview). Consolidate the private BFS (section 6). Have layout_build optionally call `map_bundle.py validate`.
- B20 autotile_build.py output is the expected `--tiles` input. The layer name comes from the manifest's optional `id`, else the joined material names. Recommendations:
  - per-tile `collision` should agree with the vertex-square rule;
  - a three-material set removes the hygiene demotions that two two-material sets force.
- B12 parallax-backgrounds.md: mention parallax_build as the code route. B12's new validate_parallax.py (pivot, coverage default) should be re-run on parallax_build output at integration; the plan sets `require_canvas_coverage: true` on every layer and an explicit top-left pivot.
- B16 background-scenes.md and scene_motion: ambient_bake's `frames/` and `mask.png` can feed the scene-motion encode and decoded-file QA. ambient-loop.json carries durations, the rational fps and the mask.
- B17 build_scene_preview and map-runtime.mjs: draw props from `bundle.props[object.prop].image` with `anchor_px`, mirroring `flip_x` about the anchor; sort by `sortY`, x, id.

## 8. Known limitations and what is not proven

**Platform.** Run only on Windows 11 with Python 3.13.2, numpy 2.5.3, Pillow 12.3.0 and scipy 1.18.1, on a machine shared with other agents. Not run on Linux or macOS, nor at the Python 3.10, Pillow 10.1 and numpy 1.26 floors. PNG, WebP and debug-text bytes are deterministic per Pillow/zlib/FreeType build; JSON outputs are deterministic, also without scipy (tested).

**layout_build:**

- Tested with synthetic tilesets only: complete two-material corner-Wang sets whose tile quadrants take the corner colours. No autotile_build output existed in this worktree. blob47 and bevel tilesets are refused.
- Per-tile `collision` in a tileset is ignored: terrain collision comes from the vertex squares.
- Reachability is proven for one actor size (default radius `round(0.3 * tile)`, `ySquash` 1.0). Doors that open, jumping and moving objects are not simulated. Agreement with B13 map_nav and B17 map-runtime.mjs is not tested yet (integration parity test).
- Hygiene with two-material sets can cut a road that runs beside water. The reachability gate reports it, but nothing prevents it.
- Exits are map-edge portals only. Doors are interactions, and `material_map` and `nav` are not written; map_nav produces the navigation grid.
- Scatter clearance is measured from the anchor point, so canopies may overhang roads (intended for tall props). Without a tileset the ground is flat colour and the bundle and codeart-meta say `placeholder: true`.
- Performance: 640x400 px about 1.5 s with previews; 2048x1536 px with 1,200 props about 5 s.

**parallax_build:**

- Generated kinds are sky, ridge, clouds and foreground. Image layers are measured, never repaired.
- The sweep follows the camera from minimum to maximum at the widest zoom, at integer positions and with a top-left pivot. Sub-pixel scrolling, other pivots and engine culling are not simulated.
- Only the base version of validate_parallax.py was run (B12 changes it in parallel).

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
