# B11-map-terrain-platform: terrain overlays, iso and hex tiles, Wang rows and seam-checked fills; platform kits with middle variants, surface QC and normalised seams

Branch `asf/B11-map-terrain-platform` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files:
[extract_terrain_tiles.py](../skills/generate2dmap/scripts/extract_terrain_tiles.py),
[extract_platform_strip.py](../skills/generate2dmap/scripts/extract_platform_strip.py),
[side-scroll-scenes.md](../skills/generate2dmap/references/side-scroll-scenes.md),
[tests/test_generate2dmap_terrain.py](../tests/test_generate2dmap_terrain.py) (61 tests) and
[tests/test_extract_platform_strip.py](../tests/test_extract_platform_strip.py) (52 tests, including the
improved fork's 16).

## 1. CLIs

Commands run from the user's project root; `<skill-dir>` is `${CLAUDE_SKILL_DIR}` in Claude Code.

    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/terrain.png --output-dir art/terrain-v1 --rows 2 --cols 3 --terrain-row grass=0 --terrain-row dirt=1 --tile-size 64 --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/terrain.png --output-dir art/terrain-v2 --rows 2 --cols 3 --terrain-row grass=0 --terrain-row dirt=1 --tile-size 64 --edge-policy seamless --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/iso.png --output-dir art/iso-v1 --rows 1 --cols 4 --terrain-row grass=0 --shape iso-diamond --tile-width 128 --background-mode shape_fill --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/hex.png --output-dir art/hex-v1 --rows 1 --cols 4 --terrain-row sand=0 --shape hex-pointy --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/tufts.png --output-dir art/tufts-v1 --rows 1 --cols 4 --terrain-row tufts=0 --layer overlay --shape rect --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/shore.png --output-dir art/shore-v1 --rows 3 --cols 14 --terrain-row water=0 --terrain-row shore=1 --terrain-row grass=2 --wang shore=water/grass:0001,0010,0011,0100,0101,0110,0111,1000,1001,1010,1011,1100,1101,1110 --edge-policy seamless --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/terrain.png --output-dir art/terrain-legacy --rows 2 --cols 3 --terrain-row grass=0 --terrain-row dirt=1 --emit-runtime-defaults
    python "<skill-dir>/scripts/extract_platform_strip.py" --input art/platform.png --spec art/platform.json --output-dir art/platform-v1 --background-mode chroma_key --decoration-band-px 3 --max-seam-ratio 1.25 --strict-qc

- `--help` of both tools is ASCII and exits 0 under cp1252 and cp950 (`assert_cli_help` in both test files).
- Success prints one ASCII JSON line: `output` (the published directory), `manifest` (`terrain-bundle.json` or the
  `--manifest` path; `platform-strip.json`), `schema`, `status` (the QA envelope status) and `qc` (terrain: `passed`,
  `warnings`; platform: `passed`, `structural_passed`, `issues`, `warnings`).
- Runtime errors print `error: <message>` to stderr and exit 1 with nothing published (missing file, existing output,
  QC failure under `--strict-qc`, bad values). argparse usage errors (unknown flag, malformed `--wang`) keep argparse's
  exit 2.
- Both tools refuse an existing `--output-dir`, work in `forge_core.staged_output` and publish only after QC. The terrain
  `--manifest` outside the output directory is a sidecar published with `publish_file_no_replace` and removed again if
  the directory publish fails.

## 2. SKILL.md routing rows

generate2dmap, route table:

| Need | Route |
|---|---|
| Opaque terrain fills from an atlas (rows are terrains, columns variants) | `scripts/extract_terrain_tiles.py ... --strict-qc`; add `--edge-policy seamless` only for art meant to tile, which then must wrap |
| Iso-diamond or hex terrain tiles | `scripts/extract_terrain_tiles.py --shape iso-diamond`, `hex-pointy` or `hex-flat` with `--tile-width`/`--tile-height`; transparent corners (native_alpha) or `--background-mode shape_fill` for a flat corner colour |
| Transparent overlays (tufts, decals, transition overlays) | `scripts/extract_terrain_tiles.py --layer overlay` (native alpha or `--background-mode chroma_key`) |
| Wang corner transition rows | `scripts/extract_terrain_tiles.py --wang NAME=A/B:MASKS` (TL TR BL BR per column); name fill rows after the materials so coverage and joins include them |
| Platform caps and middle variants | read `references/side-scroll-scenes.md`, then `scripts/extract_platform_strip.py --strict-qc` |

generate2dmap, tools table (replaces the two current rows):

| Tool | Use |
|---|---|
| `scripts/extract_terrain_tiles.py` | Terrain fills, RGBA overlays, rect, iso-diamond and hex tiles, Wang rows; border-frame, footprint and (seamless policy) wrap and Wang seam QC; runtime numbers only when given |
| `scripts/extract_platform_strip.py` | Exact left cap, middle-variant and right cap crops; collision band, per-column surface and normalised seam QC; no bbox fitting |

Processing-notes bullets (generate2dmap):

- Terrain runtime and material numbers (`world_size`, `surface_y`, `roughness`, `emission_energy`, `engine_target`) are written only when given; `--emit-runtime-defaults` restores the v1 values and lists them in `runtime_defaults_applied`.
- `--edge-policy seamless` resizes fills as one period of an endless repeat and checks every wrap; image tiles never get `seamless_verified: true` (that needs an exact proof, as codeart2d autotiles have).
- A platform's declared surface row must be solid in every collision column, and the art may rise above it only by `--surface-tolerance-px` plus a declared `--decoration-band-px`.

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `extract_terrain_tiles.py` | Slices a terrain atlas into fills, overlays, rect, iso-diamond or hex tiles and Wang transition rows with border, footprint and seam QC; no hidden runtime defaults | tests/test_generate2dmap_terrain.py: repro_10/10b (wrap resize within 1/255), repro_11, repro_12, repro_13 (100% of diamond pixels) |
| `extract_platform_strip.py` | Cuts exact platform caps and middle variants, measures the surface per column and every join with a normalised seam ratio | tests/test_extract_platform_strip.py: the fork's 16 tests, repro_14, repro_15, repro_16 |

## 4. CHANGELOG entries

- Added: terrain `--shape rect|iso-diamond|hex-pointy|hex-flat` with `--tile-width`/`--tile-height`, `--layer overlay` for RGBA overlays, `--background-mode auto|opaque|native_alpha|chroma_key|shape_fill` (`--fill-tolerance`, `--despill-radius`, `--threshold`, `--edge-threshold`), footprint coverage and spill QC (`--min-shape-coverage`, `--max-shape-spill`) (B11-T2; MAP-11).
- Added: terrain border-frame check for drawn grid lines and gutters along opposite tile edges (`--max-border-delta`, 1 disables) (B11-T2; MAP-10).
- Added: terrain `--wang NAME=A/B:MASKS`: Wang corner masks per transition row, `wang_coverage`, and under the seamless policy a seam check of every legal Wang join, including fills named after the materials (B11-T2).
- Added: terrain `--grid-rounding nearest` (rounded cell edges, per-cell `source_box`) and CJK terrain names kept as `display_name` with `terrain-<row>` file stems (B11-T2; MAP-04, MAP-23).
- Added: platform middle variants (1-16 interchangeable middles; every join measured; preview cycles them), per-column surface measurement (`--surface-tolerance-px`, `--decoration-band-px`), normalised seam ratio per join with an opt-in gate (`--max-seam-ratio`, which also flags duplicated edges), palette and grey inputs, and the improved fork's `--despill-radius 0..3` (B11-T1, B11-T3; MAP-07, MAP-14, MAP-19).
- Added: QA envelopes (method, notProven, checks, input and output sha256) in `terrain-bundle.json` and `platform-strip.json` (B11-T2, B11-T3).
- Added: side-scroll-scenes.md: stage plan template, prompt templates, platform QC flag table, render bands, camera notes, stage checklist and the layout validator pointer (B11-T4).
- Changed: `--edge-policy seamless` now changes processing: fills are Lanczos-resized wrap-aware (one period of an endless repeat) and every fill must wrap within `--max-seam-ratio` (default 1.25) without a duplicated edge. `isolated` keeps the old pixels exactly (B11-T2; MAP-15).
- Changed: both tools use the shared core: staged no-clobber publication (forge_core), the vectorised bit-identical legacy keyer and edge despill (forge_matte); the platform tool no longer loads extract_prop_pack.py through importlib (B11-T1; MAP-12, F-03).
- Changed: manifests are `generate2dmap.terrain_tile_bundle.v2` and `generate2dmap.platform_strip.v2`: manifest-relative paths (the platform tool stored absolute paths), `source` and `prompt` are fileRefs, the terrain `edge_policy` moved to `processing`; tiles and pieces are RGBA PNGs with RGB zeroed under alpha 0 (terrain tiles were RGB) (B11-T1, B11-T2).
- BREAKING: terrain runtime 3D fields are omitted unless given; legacy switch `--emit-runtime-defaults` (plan Appendix H, row "terrain").
- BREAKING: the terrain tool refuses an existing output directory (stale tiles were mixed into a new manifest); use a new folder, as for prop packs (Appendix H, prop-pack row).
- Fixed: DOC-14, MAP-16 (hidden runtime defaults), MAP-10 (gutters passed strict QC), MAP-11 (iso atlases rejected or cropped), MAP-15 (edge-clamped resize up to 19/255 off on periodic tiles), MAP-04 (no grid rounding), MAP-23 (CJK names rejected), DOC-10 (stale outputs), MAP-07 (art above the declared surface passed), MAP-14 (raw-equality seam metric failed true joins and passed duplicated edges), MAP-19 (palette PNGs rejected), MAP-12 (helper copies and importlib hack), F-03 (WSL publication), MAP-24 (absolute paths in the platform manifest).

## 5. Schema change requests

Add three `$defs` to `shared/schemas/map.schema.json` (pointers `/$defs/edgeSeam`, `/$defs/platform_strip_v2`,
`/$defs/terrain_tile_bundle_v2`), then run `python tools/vendor_sync.py --write` (map.schema.json is vendored into
generate2dmap and codeart2d). Nothing existing changes. Producers: B11's two tools; consumers: agents and games, and
any later converter to `tileset_v1` (section 7). The same fragments are `PROPOSED_PLATFORM_DEFS` in
tests/test_extract_platform_strip.py and `PROPOSED_TERRAIN_DEFS` in tests/test_generate2dmap_terrain.py; both test
files validate every manifest they produce against the vendored map.schema.json with these `$defs` applied in memory
(`proposed_map_errors`). Once applied, those helpers can be replaced by `forge_testutils.assert_valid_contract(...,
"map", "platform_strip_v2", skill="generate2dmap")` and the same for `terrain_tile_bundle_v2`.

Fragment to merge into `$defs`:

```json
{
  "edgeSeam": {
    "description": "Normalised seam of one join or wrap: common seamReport plus seam_ratio, near_median and verdict. seam_ratio is the larger of seam / adjacent_max and the worst 4-row window of the join over the worst window of the interior steps near it: about 1 or less looks like the art, well above 1 is a seam. duplicate_edge is a join much flatter than its neighbourhood (a stutter).",
    "allOf": [
      {
        "$ref": "common.schema.json#/$defs/seamReport"
      }
    ],
    "required": [
      "seam_ratio",
      "near_median",
      "verdict"
    ],
    "properties": {
      "seam_ratio": {
        "type": "number",
        "minimum": 0
      },
      "near_median": {
        "type": "number",
        "minimum": 0
      },
      "verdict": {
        "enum": [
          "continuous",
          "seam",
          "duplicate_edge"
        ]
      }
    }
  },
  "platform_strip_v2": {
    "description": "platform-strip.json from extract_platform_strip.py: exact rectangular crops of a left cap, one or more interchangeable middle variants and a right cap, published with their sha256. Collision is declared metadata: collision_rect_px is [x, y, width, height] in piece pixels, collision_span_px a half-open column range. surface records the art's top per collision column against the declared surface. Every join is measured (seam: edgeSeam). v1 manifests (schema generate2dmap.platform_strip.v1) stored absolute source paths and had no qa block.",
    "type": "object",
    "required": [
      "schema",
      "source",
      "spec",
      "processing",
      "surface_y_px",
      "collision_depth_px",
      "pieces",
      "joins",
      "preview",
      "qc",
      "qa"
    ],
    "properties": {
      "schema": {
        "const": "generate2dmap.platform_strip.v2"
      },
      "source": {
        "allOf": [
          {
            "$ref": "common.schema.json#/$defs/fileRef"
          }
        ],
        "required": [
          "size",
          "mode"
        ],
        "properties": {
          "size": {
            "$ref": "common.schema.json#/$defs/size2"
          },
          "mode": {
            "type": "string",
            "minLength": 1
          },
          "bit_depth": {
            "type": "integer",
            "minimum": 1
          },
          "conversion": {
            "type": "string"
          }
        }
      },
      "spec": {
        "allOf": [
          {
            "$ref": "common.schema.json#/$defs/fileRef"
          }
        ],
        "required": [
          "content"
        ],
        "properties": {
          "content": {
            "type": "object"
          }
        }
      },
      "processing": {
        "type": "object",
        "required": [
          "background_mode",
          "geometry",
          "resized"
        ],
        "properties": {
          "background_mode": {
            "enum": [
              "chroma_key",
              "native_alpha",
              "opaque"
            ]
          },
          "despill_radius": {
            "type": "integer",
            "minimum": 0,
            "maximum": 3
          },
          "geometry": {
            "const": "explicit_native_rectangles"
          },
          "resized": {
            "const": false
          },
          "trimmed": {
            "const": false
          },
          "aligned": {
            "const": false
          }
        }
      },
      "surface_y_px": {
        "type": "integer",
        "minimum": 0
      },
      "collision_depth_px": {
        "type": "integer",
        "minimum": 1
      },
      "pieces": {
        "type": "array",
        "minItems": 3,
        "items": {
          "type": "object",
          "required": [
            "id",
            "role",
            "source_box",
            "size",
            "collision_span_px",
            "path",
            "sha256",
            "anchor_px",
            "collision_rect_px",
            "coverage",
            "surface"
          ],
          "properties": {
            "id": {
              "type": "string",
              "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]*$"
            },
            "role": {
              "enum": [
                "left_cap",
                "middle",
                "right_cap"
              ]
            },
            "source_box": {
              "$ref": "common.schema.json#/$defs/box"
            },
            "size": {
              "$ref": "common.schema.json#/$defs/size2"
            },
            "collision_span_px": {
              "type": "array",
              "items": {
                "type": "integer",
                "minimum": 0
              },
              "minItems": 2,
              "maxItems": 2
            },
            "path": {
              "$ref": "common.schema.json#/$defs/relPath"
            },
            "sha256": {
              "$ref": "common.schema.json#/$defs/sha256"
            },
            "anchor_px": {
              "$ref": "common.schema.json#/$defs/point2"
            },
            "collision_rect_px": {
              "$ref": "common.schema.json#/$defs/rectXYWH"
            },
            "surface_y_source_px": {
              "type": "integer",
              "minimum": 0
            },
            "coverage": {
              "type": "object",
              "required": [
                "solid_fraction_by_column",
                "minimum_column_fraction",
                "insufficient_columns",
                "missing_surface_columns",
                "non_solid_band_pixels"
              ],
              "properties": {
                "solid_fraction_by_column": {
                  "type": "array",
                  "items": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1
                  }
                },
                "minimum_column_fraction": {
                  "type": "number",
                  "minimum": 0,
                  "maximum": 1
                },
                "insufficient_columns": {
                  "type": "array",
                  "items": {
                    "type": "integer",
                    "minimum": 0
                  }
                },
                "missing_surface_columns": {
                  "type": "array",
                  "items": {
                    "type": "integer",
                    "minimum": 0
                  }
                },
                "non_solid_band_pixels": {
                  "type": "integer",
                  "minimum": 0
                }
              }
            },
            "surface": {
              "type": "object",
              "required": [
                "declared_y_px",
                "measured",
                "max_rise_px",
                "columns_above_tolerance",
                "decoration_band_px"
              ],
              "properties": {
                "declared_y_px": {
                  "type": "integer",
                  "minimum": 0
                },
                "measured": {
                  "type": "boolean"
                },
                "measured_y_px_by_column": {
                  "type": [
                    "array",
                    "null"
                  ],
                  "items": {
                    "type": [
                      "integer",
                      "null"
                    ],
                    "minimum": 0
                  }
                },
                "max_rise_px": {
                  "type": "integer",
                  "minimum": 0
                },
                "columns_above_tolerance": {
                  "type": "array",
                  "items": {
                    "type": "integer",
                    "minimum": 0
                  }
                },
                "decoration_band_px": {
                  "type": "integer",
                  "minimum": 0
                },
                "decoration_px": {
                  "type": "integer",
                  "minimum": 0
                }
              }
            }
          }
        }
      },
      "joins": {
        "type": "array",
        "minItems": 3,
        "items": {
          "type": "object",
          "required": [
            "join",
            "left",
            "right",
            "full_edge",
            "contact_band",
            "seam"
          ],
          "properties": {
            "join": {
              "enum": [
                "left_cap->middle",
                "middle->middle",
                "middle->right_cap"
              ]
            },
            "left": {
              "type": "string"
            },
            "right": {
              "type": "string"
            },
            "full_edge": {
              "type": "object"
            },
            "contact_band": {
              "type": "object"
            },
            "seam": {
              "$ref": "#/$defs/edgeSeam"
            }
          }
        }
      },
      "preview": {
        "type": "object",
        "required": [
          "path",
          "size",
          "sha256",
          "placements"
        ],
        "properties": {
          "path": {
            "$ref": "common.schema.json#/$defs/relPath"
          },
          "size": {
            "$ref": "common.schema.json#/$defs/size2"
          },
          "sha256": {
            "$ref": "common.schema.json#/$defs/sha256"
          },
          "placements": {
            "type": "array",
            "minItems": 5
          }
        }
      },
      "qc": {
        "type": "object",
        "required": [
          "passed",
          "structural_passed",
          "issues",
          "warnings"
        ],
        "properties": {
          "passed": {
            "type": "boolean"
          },
          "structural_passed": {
            "type": "boolean"
          },
          "issues": {
            "type": "array",
            "items": {
              "type": "string"
            }
          },
          "warnings": {
            "type": "array",
            "items": {
              "type": "string"
            }
          },
          "max_seam_ratio": {
            "type": [
              "number",
              "null"
            ],
            "exclusiveMinimum": 0
          }
        }
      },
      "qa": {
        "$ref": "common.schema.json#/$defs/qaEnvelope"
      }
    }
  },
  "terrain_tile_bundle_v2": {
    "description": "terrain-bundle.json from extract_terrain_tiles.py. Each atlas row is one terrain: a fill or a Wang corner transition row (variants carry wang [TL, TR, BL, BR] material indices into materials). Tiles are square, rect, iso-diamond or hex, base or overlay; tile.footprint is the polygon in tile pixels. runtime and material numbers appear only when given, or with --emit-runtime-defaults (listed in runtime_defaults_applied); v1 bundles (generate2dmap.terrain_tile_bundle.v1) always wrote them. seamless_verified stays false: image tiles have no exact seam proof; the seamless policy records wrap and Wang seams (edgeSeam).",
    "type": "object",
    "required": [
      "schema",
      "source",
      "grid",
      "tile",
      "terrains",
      "processing",
      "qc",
      "qa"
    ],
    "properties": {
      "schema": {
        "const": "generate2dmap.terrain_tile_bundle.v2"
      },
      "source": {
        "allOf": [
          {
            "$ref": "common.schema.json#/$defs/fileRef"
          }
        ],
        "required": [
          "size",
          "mode"
        ],
        "properties": {
          "size": {
            "$ref": "common.schema.json#/$defs/size2"
          },
          "mode": {
            "type": "string",
            "minLength": 1
          },
          "bit_depth": {
            "type": "integer",
            "minimum": 1
          },
          "conversion": {
            "type": "string"
          }
        }
      },
      "prompt": {
        "$ref": "common.schema.json#/$defs/fileRef"
      },
      "grid": {
        "type": "object",
        "required": [
          "rows",
          "cols",
          "rounding",
          "source_cell_size",
          "output_tile_size"
        ],
        "properties": {
          "rows": {
            "type": "integer",
            "minimum": 1
          },
          "cols": {
            "type": "integer",
            "minimum": 1
          },
          "rounding": {
            "enum": [
              "exact",
              "nearest"
            ]
          },
          "source_cell_size": {
            "$ref": "common.schema.json#/$defs/size2"
          },
          "source_cell_size_max": {
            "$ref": "common.schema.json#/$defs/size2"
          },
          "output_tile_size": {
            "$ref": "common.schema.json#/$defs/size2"
          }
        }
      },
      "tile": {
        "type": "object",
        "required": [
          "shape",
          "layer",
          "footprint"
        ],
        "properties": {
          "shape": {
            "enum": [
              "square",
              "rect",
              "iso-diamond",
              "hex-pointy",
              "hex-flat"
            ]
          },
          "layer": {
            "enum": [
              "base",
              "overlay"
            ]
          },
          "footprint": {
            "$ref": "common.schema.json#/$defs/polygon"
          }
        }
      },
      "terrains": {
        "type": "object",
        "minProperties": 1,
        "propertyNames": {
          "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"
        },
        "additionalProperties": {
          "type": "object",
          "required": [
            "display_name",
            "row",
            "kind",
            "variants",
            "variant_difference_min"
          ],
          "properties": {
            "display_name": {
              "type": "string",
              "minLength": 1
            },
            "row": {
              "type": "integer",
              "minimum": 0
            },
            "kind": {
              "enum": [
                "fill",
                "wang_corner"
              ]
            },
            "materials": {
              "type": "array",
              "minItems": 2,
              "maxItems": 9,
              "items": {
                "type": "string",
                "minLength": 1
              }
            },
            "variant_difference_min": {
              "type": "number",
              "minimum": 0,
              "maximum": 1
            },
            "material": {
              "type": "object",
              "properties": {
                "roughness": {
                  "type": "number",
                  "minimum": 0,
                  "maximum": 1
                },
                "emission_energy": {
                  "type": "number",
                  "minimum": 0
                }
              }
            },
            "wang_coverage": {
              "type": "object",
              "required": [
                "masks",
                "of",
                "complete"
              ]
            },
            "wang_seams": {
              "type": "object",
              "required": [
                "legal_joins",
                "failed"
              ]
            },
            "cross_variant_seams": {
              "type": "object"
            },
            "variants": {
              "type": "array",
              "minItems": 1,
              "items": {
                "type": "object",
                "required": [
                  "path",
                  "sha256",
                  "source_cell",
                  "source_box",
                  "resized",
                  "mean_luminance",
                  "contrast"
                ],
                "properties": {
                  "path": {
                    "$ref": "common.schema.json#/$defs/relPath"
                  },
                  "sha256": {
                    "$ref": "common.schema.json#/$defs/sha256"
                  },
                  "source_cell": {
                    "type": "array",
                    "items": {
                      "type": "integer",
                      "minimum": 0
                    },
                    "minItems": 2,
                    "maxItems": 2
                  },
                  "source_box": {
                    "$ref": "common.schema.json#/$defs/box"
                  },
                  "crop_box": {
                    "$ref": "common.schema.json#/$defs/box"
                  },
                  "resized": {
                    "type": "boolean"
                  },
                  "mean_luminance": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1
                  },
                  "contrast": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1
                  },
                  "visible_fraction": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1
                  },
                  "wang": {
                    "type": "array",
                    "items": {
                      "type": "integer",
                      "minimum": 0,
                      "maximum": 8
                    },
                    "minItems": 4,
                    "maxItems": 4
                  },
                  "variant": {
                    "type": "integer",
                    "minimum": 0
                  },
                  "border": {
                    "type": "object",
                    "required": [
                      "checked",
                      "frame",
                      "frames"
                    ]
                  },
                  "shape": {
                    "type": "object",
                    "required": [
                      "footprint_px",
                      "coverage",
                      "spill_px",
                      "spill_fraction"
                    ]
                  },
                  "shape_fill": {
                    "type": "object"
                  },
                  "wrap": {
                    "type": "object",
                    "required": [
                      "x",
                      "y"
                    ],
                    "properties": {
                      "x": {
                        "$ref": "#/$defs/edgeSeam"
                      },
                      "y": {
                        "$ref": "#/$defs/edgeSeam"
                      }
                    }
                  }
                }
              }
            }
          },
          "if": {
            "properties": {
              "kind": {
                "const": "wang_corner"
              }
            },
            "required": [
              "kind"
            ]
          },
          "then": {
            "required": [
              "materials"
            ],
            "properties": {
              "variants": {
                "items": {
                  "required": [
                    "wang"
                  ]
                }
              }
            }
          }
        }
      },
      "runtime": {
        "type": "object",
        "properties": {
          "engine_target": {
            "type": "string",
            "minLength": 1
          },
          "world_size": {
            "type": "number",
            "exclusiveMinimum": 0
          },
          "surface_y": {
            "type": "number"
          },
          "edge_policy": {
            "enum": [
              "isolated",
              "seamless"
            ]
          }
        }
      },
      "runtime_defaults_applied": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "processing": {
        "type": "object",
        "required": [
          "background_mode",
          "resampler",
          "edge_policy",
          "grid_rounding"
        ],
        "properties": {
          "background_mode": {
            "enum": [
              "opaque",
              "native_alpha",
              "chroma_key",
              "shape_fill"
            ]
          },
          "resampler": {
            "enum": [
              "nearest",
              "lanczos"
            ]
          },
          "edge_policy": {
            "enum": [
              "isolated",
              "seamless"
            ]
          },
          "grid_rounding": {
            "enum": [
              "exact",
              "nearest"
            ]
          },
          "wrap_aware_resize": {
            "type": "boolean"
          }
        }
      },
      "qc": {
        "type": "object",
        "required": [
          "warnings",
          "passed",
          "seamless_verified"
        ],
        "properties": {
          "warnings": {
            "type": "array",
            "items": {
              "type": "string"
            }
          },
          "passed": {
            "type": "boolean"
          },
          "seamless_verified": {
            "const": false
          }
        }
      },
      "qa": {
        "$ref": "common.schema.json#/$defs/qaEnvelope"
      }
    }
  }
}
```

Fixtures for tests/test_contracts.py (every `$def` needs `map.<def>.valid.json` and `map.<def>.invalid.json`; all of the
following were checked against the patched schema, including each case's expected error). The valid documents are
real tool output for tiny inputs (paths `../strip.png`, `../atlas.png` point at the inputs beside the output folder).

`tests/fixtures/contracts/map.edgeSeam.valid.json`:

```json
{
  "seam": 5.625,
  "adjacent_median": 5.625,
  "adjacent_p95": 5.625,
  "adjacent_max": 5.625,
  "seam_over_median": 1.0,
  "seam_over_p95": 1.0,
  "seam_ratio": 1.0,
  "near_median": 5.625,
  "verdict": "continuous",
  "method": "premultiplied RGBA column steps (0-255); join vs interior steps within 8 columns per side, whole edge and worst 4-row window"
}
```

`tests/fixtures/contracts/map.edgeSeam.invalid.json`:

```json
{
  "cases": [
    {
      "set": "/verdict",
      "value": "ok",
      "why": "verdict is continuous, seam or duplicate_edge",
      "error": "'ok' is not one of"
    },
    {
      "remove": "/seam_ratio",
      "why": "the normalised ratio is required",
      "error": "'seam_ratio' is a required property"
    },
    {
      "set": "/seam",
      "value": -1,
      "why": "a step cannot be negative",
      "error": "-1 is less than the minimum of 0"
    }
  ]
}
```

`tests/fixtures/contracts/map.platform_strip_v2.invalid.json`:

```json
{
  "cases": [
    {
      "set": "/schema",
      "value": "generate2dmap.platform_strip.v1",
      "why": "v1 manifests have their own id",
      "error": "'generate2dmap.platform_strip.v2' was expected"
    },
    {
      "set": "/source/path",
      "value": "D:/art/strip.png",
      "why": "paths are manifest-relative POSIX",
      "error": "does not match"
    },
    {
      "set": "/processing/resized",
      "value": true,
      "why": "pieces are exact crops",
      "error": "False was expected"
    },
    {
      "set": "/qa/checks/0/status",
      "value": "fail",
      "why": "a pass envelope cannot hold a failed check"
    },
    {
      "set": "/joins/0/join",
      "value": "cap->cap",
      "why": "joins are cap-middle, middle-middle or middle-cap",
      "error": "is not one of"
    },
    {
      "remove": "/qa",
      "why": "every manifest carries its QA envelope",
      "error": "'qa' is a required property"
    }
  ]
}
```

`tests/fixtures/contracts/map.terrain_tile_bundle_v2.invalid.json`:

```json
{
  "cases": [
    {
      "set": "/schema",
      "value": "generate2dmap.terrain_tile_bundle.v1",
      "why": "v1 bundles have their own id",
      "error": "'generate2dmap.terrain_tile_bundle.v2' was expected"
    },
    {
      "set": "/qc/seamless_verified",
      "value": true,
      "why": "image tiles have no exact seam proof",
      "error": "False was expected"
    },
    {
      "set": "/terrains/grass/kind",
      "value": "wang_corner",
      "why": "a Wang row needs materials and per-tile masks",
      "error": "'materials' is a required property"
    },
    {
      "set": "/tile/shape",
      "value": "octagon",
      "why": "shapes are square, rect, iso-diamond, hex-pointy, hex-flat",
      "error": "is not one of"
    },
    {
      "set": "/terrains/grass/variants/0/path",
      "value": "C:/x/grass-1.png",
      "why": "paths are manifest-relative",
      "error": "does not match"
    }
  ]
}
```

`tests/fixtures/contracts/map.terrain_tile_bundle_v2.valid.json` (one line):

```json
{"schema":"generate2dmap.terrain_tile_bundle.v2","source":{"path":"../atlas.png","sha256":"dbdb5a5cfc59dd2ef2dd2e31e91262f8ab0e2dd1befc6114f52170007d1e3b65","bytes":268,"size":[8,8],"mode":"RGB","bit_depth":8,"conversion":"RGB -> RGBA"},"grid":{"rows":1,"cols":1,"rounding":"exact","source_cell_size":[8,8],"output_tile_size":[8,8]},"tile":{"shape":"square","layer":"base","footprint":[[0.0,0.0],[8.0,0.0],[8.0,8.0],[0.0,8.0]]},"terrains":{"grass":{"display_name":"grass","row":0,"kind":"fill","variants":[{"path":"grass-1.png","source_cell":[0,0],"source_box":[0,0,8,8],"resized":false,"mean_luminance":0.50337,"contrast":0.101985,"border":{"checked":true,"frame":false,"frames":[],"max_delta":0.0},"sha256":"47d034afa9ab584d1d5177a248d0c3a2cef12e79470260a786c24d8365c393e9"}],"variant_difference_min":1.0}},"processing":{"background_mode":"opaque","background_mode_requested":"auto","cell_shape":"square","resampler":"lanczos","grid_rounding":"exact","edge_policy":"isolated","wrap_aware_resize":false},"qc":{"min_contrast":0.035,"min_variant_difference":0.025,"max_border_delta":0.1,"min_shape_coverage":0.97,"max_shape_spill":0.03,"max_seam_ratio":null,"warnings":[],"passed":true,"seamless_verified":false},"qa":{"status":"pass","method":"Grid slices of the atlas, background handled per --background-mode, resized to the tile size (Lanczos or nearest; wrap-aware for the seamless policy). Luminance contrast and mean thumbnail differences; drawn frames along opposite edges; footprint coverage and spill for iso/hex base tiles; for the seamless policy, premultiplied wrap and Wang-join steps normalised by the art's own steps near each edge.","notProven":["visual quality, style match and readability at game zoom","that each variant reads as the named terrain","seamlessness: no exact seam proof exists for image tiles (seamless_verified stays false)"],"checks":[{"id":"contrast","status":"pass","value":0.101985,"threshold":0.035},{"id":"variant_difference","status":"pass","value":1.0,"threshold":0.025},{"id":"border_frame","status":"pass","value":0,"threshold":0.1},{"id":"shape_coverage","status":"skipped","value":null,"threshold":0.97},{"id":"shape_spill","status":"skipped","value":null,"threshold":0.03},{"id":"wrap_seams","status":"skipped","value":null,"threshold":null},{"id":"wang_seams","status":"skipped","value":null,"threshold":null}],"inputs":[{"path":"../atlas.png","sha256":"dbdb5a5cfc59dd2ef2dd2e31e91262f8ab0e2dd1befc6114f52170007d1e3b65","bytes":268}],"outputs":[{"path":"grass-1.png","sha256":"47d034afa9ab584d1d5177a248d0c3a2cef12e79470260a786c24d8365c393e9","bytes":332}],"tool":{"name":"extract_terrain_tiles.py","version":"2.0"}}}
```

`tests/fixtures/contracts/map.platform_strip_v2.valid.json` (one line):

```json
{"schema":"generate2dmap.platform_strip.v2","source":{"path":"../strip.png","sha256":"fc487b9dd4f0992fa72d970977e73983cf4d94c1caba41347f8c6c7c43307486","bytes":90,"size":[12,4],"mode":"RGBA","bit_depth":8,"conversion":"none"},"spec":{"path":"../strip.json","sha256":"fe35d2a52811e16d1227f7e8202ce57392a098f253ad744808a3fd3e04eba320","bytes":248,"content":{"surface_y_px":1,"collision_depth_px":2,"pieces":[{"id":"left","role":"left_cap","source_box":[0,0,4,4]},{"id":"mid","role":"middle","source_box":[4,0,8,4]},{"id":"right","role":"right_cap","source_box":[8,0,12,4]}]}},"processing":{"background_mode":"native_alpha","despill_radius":0,"geometry":"explicit_native_rectangles","resized":false,"trimmed":false,"aligned":false,"transparent_rgb":"zeroed"},"surface_y_px":1,"collision_depth_px":2,"coordinate_contract":"Origin at crop top-left; +x right, +y down. Collision rect is [x,y,width,height]. At uniform scale s, place crop at (collision_left-s*collision_span_px[0], collision_top-s*surface_y_px). anchor_px is the visual left/top-surface anchor, not necessarily the collision-left when a cap has outer padding. Middle variants are interchangeable: any middle may follow the left cap, any other middle or itself, and precede the right cap. Collision is declared metadata; alpha coverage does not infer physical geometry.","pieces":[{"id":"left","role":"left_cap","source_box":[0,0,4,4],"size":[4,4],"collision_span_px":[0,4],"path":"left.png","anchor_px":[0,1],"collision_rect_px":[0,1,4,2],"surface_y_source_px":1,"coverage":{"solid_fraction_by_column":[1.0,1.0,1.0,1.0],"minimum_column_fraction":1.0,"insufficient_columns":[],"missing_surface_columns":[],"non_solid_band_pixels":0},"surface":{"declared_y_px":1,"measured":true,"measured_y_px_by_column":[1,1,1,1],"max_rise_px":0,"columns_above_tolerance":[],"decoration_band_px":0,"decoration_px":0},"sha256":"c862de1dde068bf8b62520fd2c99da85e697071692551a5319f2e9c8a1ea2a1a"},{"id":"mid","role":"middle","source_box":[4,0,8,4],"size":[4,4],"collision_span_px":[0,4],"path":"mid.png","anchor_px":[0,1],"collision_rect_px":[0,1,4,2],"surface_y_source_px":1,"coverage":{"solid_fraction_by_column":[1.0,1.0,1.0,1.0],"minimum_column_fraction":1.0,"insufficient_columns":[],"missing_surface_columns":[],"non_solid_band_pixels":0},"surface":{"declared_y_px":1,"measured":true,"measured_y_px_by_column":[1,1,1,1],"max_rise_px":0,"columns_above_tolerance":[],"decoration_band_px":0,"decoration_px":0},"sha256":"c862de1dde068bf8b62520fd2c99da85e697071692551a5319f2e9c8a1ea2a1a"},{"id":"right","role":"right_cap","source_box":[8,0,12,4],"size":[4,4],"collision_span_px":[0,4],"path":"right.png","anchor_px":[0,1],"collision_rect_px":[0,1,4,2],"surface_y_source_px":1,"coverage":{"solid_fraction_by_column":[1.0,1.0,1.0,1.0],"minimum_column_fraction":1.0,"insufficient_columns":[],"missing_surface_columns":[],"non_solid_band_pixels":0},"surface":{"declared_y_px":1,"measured":true,"measured_y_px_by_column":[1,1,1,1],"max_rise_px":0,"columns_above_tolerance":[],"decoration_band_px":0,"decoration_px":0},"sha256":"c862de1dde068bf8b62520fd2c99da85e697071692551a5319f2e9c8a1ea2a1a"}],"joins":[{"join":"left_cap->middle","left":"left","right":"mid","full_edge":{"alpha_mae":0.0,"visible_rgb_mae":10.0,"jointly_visible_rows":3,"premultiplied_rgb_mae":7.5},"contact_band":{"alpha_mae":0.0,"visible_rgb_mae":10.0,"jointly_visible_rows":2,"premultiplied_rgb_mae":10.0,"y_range_px":[1,3],"solid_contact_by_row":[true,true],"solid_contact_fraction":1.0},"seam":{"seam":5.625,"adjacent_median":5.625,"adjacent_p95":5.625,"adjacent_max":5.625,"seam_over_median":1.0,"seam_over_p95":1.0,"seam_ratio":1.0,"near_median":5.625,"verdict":"continuous","method":"premultiplied RGBA column steps (0-255); join vs interior steps within 8 columns per side, whole edge and worst 4-row window"}},{"join":"middle->middle","left":"mid","right":"mid","full_edge":{"alpha_mae":0.0,"visible_rgb_mae":10.0,"jointly_visible_rows":3,"premultiplied_rgb_mae":7.5},"contact_band":{"alpha_mae":0.0,"visible_rgb_mae":10.0,"jointly_visible_rows":2,"premultiplied_rgb_mae":10.0,"y_range_px":[1,3],"solid_contact_by_row":[true,true],"solid_contact_fraction":1.0},"seam":{"seam":5.625,"adjacent_median":5.625,"adjacent_p95":5.625,"adjacent_max":5.625,"seam_over_median":1.0,"seam_over_p95":1.0,"seam_ratio":1.0,"near_median":5.625,"verdict":"continuous","method":"premultiplied RGBA column steps (0-255); join vs interior steps within 8 columns per side, whole edge and worst 4-row window"}},{"join":"middle->right_cap","left":"mid","right":"right","full_edge":{"alpha_mae":0.0,"visible_rgb_mae":10.0,"jointly_visible_rows":3,"premultiplied_rgb_mae":7.5},"contact_band":{"alpha_mae":0.0,"visible_rgb_mae":10.0,"jointly_visible_rows":2,"premultiplied_rgb_mae":10.0,"y_range_px":[1,3],"solid_contact_by_row":[true,true],"solid_contact_fraction":1.0},"seam":{"seam":5.625,"adjacent_median":5.625,"adjacent_p95":5.625,"adjacent_max":5.625,"seam_over_median":1.0,"seam_over_p95":1.0,"seam_ratio":1.0,"near_median":5.625,"verdict":"continuous","method":"premultiplied RGBA column steps (0-255); join vs interior steps within 8 columns per side, whole edge and worst 4-row window"}}],"qc":{"passed":true,"structural_passed":true,"issues":[],"warnings":[],"solid_alpha_threshold":255,"min_column_coverage":1.0,"surface_tolerance_px":0,"decoration_band_px":0,"max_seam_ratio":null,"max_seam_rgb_mae":null,"max_seam_alpha_mae":null,"seam_metrics_note":"seam.seam_ratio compares each join with the interior steps near it: about 1 or less looks like the art, well above 1 is a seam, a join much flatter than its neighbours duplicates an edge. Raw edge MAE values are 0..255 diagnostics; no seam is repaired and no aesthetic seamlessness is certified."},"preview":{"path":"strip-preview.png","size":[20,4],"sha256":"243e03b67b699f766a4b4c5a08b7fa07b019d0addb48780a2abec7426b880004","placements":[{"role":"left_cap","id":"left","left":0,"top":0,"size":[4,4]},{"role":"middle","id":"mid","left":4,"top":0,"size":[4,4]},{"role":"middle","id":"mid","left":8,"top":0,"size":[4,4]},{"role":"middle","id":"mid","left":12,"top":0,"size":[4,4]},{"role":"right_cap","id":"right","left":16,"top":0,"size":[4,4]}]},"qa":{"status":"pass","method":"Exact rectangular crops of the keyed source. Collision band coverage and a solid declared surface row per collision column; the first solid row above it (outside the decoration band) against the surface tolerance; every join (left cap, each middle variant, right cap) by premultiplied column steps normalised by the art's own steps near the join.","notProven":["artistic continuity, lighting or style match at joins (only pixel steps are measured)","runtime behaviour: crossing every join, one-way platforms, slopes and pixel snapping","that the declared collision depth matches the intended physics","chroma keying quality beyond the legacy keyer rules"],"checks":[{"id":"collision_band_coverage","status":"pass","value":{"left":1.0,"mid":1.0,"right":1.0},"threshold":1.0},{"id":"surface_row_solid","status":"pass","value":0,"threshold":0},{"id":"surface_rise_px","status":"pass","value":0,"threshold":0},{"id":"seam_ratio","status":"pass","value":1.0,"threshold":1.25},{"id":"duplicate_edges","status":"pass","value":0,"threshold":0},{"id":"seam_rgb_mae","status":"skipped","value":10.0,"threshold":null},{"id":"seam_alpha_mae","status":"skipped","value":0.0,"threshold":null}],"inputs":[{"path":"../strip.png","sha256":"fc487b9dd4f0992fa72d970977e73983cf4d94c1caba41347f8c6c7c43307486","bytes":90},{"path":"../strip.json","sha256":"fe35d2a52811e16d1227f7e8202ce57392a098f253ad744808a3fd3e04eba320","bytes":248}],"outputs":[{"path":"left.png","sha256":"c862de1dde068bf8b62520fd2c99da85e697071692551a5319f2e9c8a1ea2a1a","bytes":89},{"path":"mid.png","sha256":"c862de1dde068bf8b62520fd2c99da85e697071692551a5319f2e9c8a1ea2a1a","bytes":89},{"path":"right.png","sha256":"c862de1dde068bf8b62520fd2c99da85e697071692551a5319f2e9c8a1ea2a1a","bytes":89},{"path":"strip-preview.png","sha256":"243e03b67b699f766a4b4c5a08b7fa07b019d0addb48780a2abec7426b880004","bytes":91}],"tool":{"name":"extract_platform_strip.py","version":"2.0"}}}
```

Optional fields the producers write beyond what the fragments require (all allowed, objects stay open):
terrain `processing.background_mode_requested`, `keyer`, `threshold`, `edge_threshold`, `despill_radius`,
`despill_changed_px`, `cell_shape`, `wrap_aware_resize`; `qc.min_contrast`, `min_variant_difference`,
`max_border_delta`, `min_shape_coverage`, `max_shape_spill`, `max_seam_ratio`; per terrain `wang_coverage`
(`masks`, `of`, `complete`, `missing_count`, `missing`), `wang_seams` (`legal_joins`, `failed[]`),
`cross_variant_seams` (`pairs`, `max_seam_ratio`, `not_continuous[]`, `note`); per variant `crop_box`,
`shape_fill` (`fill_rgb`, `corner_fill_share`, `keyed_px`), `border.max_delta`. Platform `processing.keyer`,
`threshold`, `edge_threshold`, `despill_changed_px`, `transparent_rgb`; `coordinate_contract`; per piece
`surface.reason` when not measured; per join `full_edge` and `contact_band` (the fork's metrics); `qc` thresholds and
`seam_metrics_note`; preview placements carry `id`.

## 6. Shared-helper promotion requests

- `_local_edge_seam_report(left, right, *, left_start=0, right_stop=None, gate=None) -> dict`
  (skills/generate2dmap/scripts/extract_platform_strip.py:171 and extract_terrain_tiles.py:444; the two copies,
  with `_premultiplied` and `_window_max` above them, are byte-identical). Proposed home: forge_core, next to
  `seam_report`, as `edge_seam_report(left, right, *, left_start=0, right_stop=None, gate=None)`; it returns a
  common `seamReport` plus `seam_ratio`, `near_median` and `verdict`. It is the spatial counterpart of
  `seam_report` (MAP-14 asks for one normalised seam metric shared by terrain, platform and parallax; B12's
  validate_parallax could use it for repeat seams). Tests: SeamRatioTests in tests/test_extract_platform_strip.py,
  SeamlessTests and WangTests in tests/test_generate2dmap_terrain.py. Calibration is in section 8.
- `_local_wrap_resize(image, size) -> Image` (extract_terrain_tiles.py:331): Lanczos resize of a periodic opaque
  tile as one period of an endless repeat (wrap padding plus Pillow's `box`). Proposed: a `wrap=True` option of
  `forge_core.resample_rgba` for the unanchored path (premultiplied planes padded with `np.pad(mode="wrap")`).
  Tests: test_repro_10_wrap_aware_resize_matches_the_reference, test_repro_10b_bricks_wrap_within_one_level.
- `_local_shape_mask(shape, width, height, grow=0.0) -> ndarray[bool]` (extract_terrain_tiles.py:244) with
  `shape_polygon`: pixel-centre raster of rect, iso-diamond and hex footprints, an exact plane partition at
  `grow=0`. Proposed: `forge_core.footprint_mask(polygon, width, height, grow=0.0)`; iso and hex map exporters
  (B13, B14) and codeart2d autotiles need the same rule. Tests: test_hex_footprints_partition_the_plane,
  test_iso_diamond_partitions_the_plane.
- `_local_file_ref(path, base, sha256, size) -> dict` (extract_platform_strip.py:251, extract_terrain_tiles.py:499):
  A1's rule as code (manifest-relative POSIX path; on another drive the file name, never an absolute path).
  Proposed: `forge_core.file_ref(path, base, *, sha256=None)`. Test: test_input_on_another_drive_records_the_file_name.

## 7. Cross-module links that Z must add

- skills/generate2dmap/SKILL.md: the routing and tools rows of section 2 (the current tools rows still say "opaque
  square terrain variants" and "contact-band and repeat-preview checks").
- map-strategies.md (B13): its terrain example is the old multi-line `python scripts/extract_terrain_tiles.py ...`;
  replace it with a single-line command from section 1 and mention shapes, overlays, Wang rows and the seamless
  policy.
- side-scroll-scenes.md names the side-scroll layout validator `validate_layout.py` in plain text (B14 creates it);
  once it lands, link `../scripts/validate_layout.py` usage and engine-maps.md from section 1 of side-scroll-scenes.md.
- parallax-backgrounds.md and validate_parallax (B12): use the same normalised seam definition (section 6) for
  repeated layers, so terrain, platform and parallax report one metric.
- export_tiled / export_godot (B13, B14) read `tileset_v1` (one tilesheet image). A terrain bundle with Wang rows has
  the corner masks (`wang`, `materials`) but one PNG per tile; a small converter (pack the tiles, write `tileset_v1`
  with `kind: wang_corner` and `seamless_verified: false`) would let image-model terrain reach those exporters.
- CHANGELOG (Z): section 4; README tool table: section 3.

## 8. Known limitations and what is not proven

- Seam ratio thresholds are synthetic. `seam_ratio = max(seam / local max step, worst 4-row window of the join /
  worst window of the local steps)`, with local steps within 8 columns of the join. On 400 synthetic trials
  (smooth periodic noise of several spectra, iid pixel noise) seamless joins reached at most 1.19; the 5th
  percentile of unrelated joins was 1.38 for smooth noise and 1.23 for iid noise offset by 25 levels. Sine and brick strips give 1.06 and 1.00; a duplicated
  column gives 0.05. Not calibrated on real image-model art; 1.25 is a default, not a proof. Each tile is also
  judged against its own sharpest nearby step, so a seam is invisible to the metric next to an equally sharp
  feature.
- `--edge-policy seamless` verifies each fill against itself and legal Wang joins; joins between different variants
  of a fill row are reported (`cross_variant_seams`) but not gated, so variants meant for random mixing need a
  look at that list. Iso, hex, overlay and crop-square tiles cannot use the seamless policy (no iso-periodic wrap).
- The border check uses luminance only: a frame of another hue at the same luminance, or a gradual vignette, is not
  found. Art that legitimately repeats a line on both opposite edges (bricks aligned to the tile grid, mortar on two
  edges) is reported as a frame; `--max-border-delta 1` turns the check off. Tiles smaller than 8 px are not judged.
- Iso-diamond and hex tiles are flat footprints inscribed in the tile; block tiles with visible sides spill outside
  the footprint and belong in `--layer overlay`. The pixel-centre footprint partitions the plane exactly for the
  2:1 diamond and the tested hex sizes (28x32 pointy, 32x28 flat); other hex sizes can have pixel centres exactly on
  an edge, which then count for both neighbours.
- `shape_fill` keys every pixel near the corner colour that touches the corners: art whose edge has that colour (a
  black outline on black fill) loses it. Use native alpha or a magenta key for such art.
- Platform surface QC reads alpha: `opaque` pieces cannot be measured and record `measured: false` (the fork test
  that runs opaque art with painted rows above the surface depends on that). The declared surface row must still be
  solid in every collision column, so a tolerance only allows art rising above it, never dipping below.
- MAP-19 names P, PA and LA input. PNG has no palette+alpha mode, so PA never comes from a PNG; P with tRNS, LA and L
  with a tRNS key are tested. repro_15's own file (`quantize()` then `transparency=0`) makes the brown platform index
  transparent, so it can only show acceptance; a palette whose transparent index is the background passes strict QC.
- Deviations from the improved fork's tests (three, marked "Port:" in their docstrings): the schema id is
  `generate2dmap.platform_strip.v2` (manifest paths, variants and QA changed meaning); the exact-bytes comparison zeroes
  RGB under alpha 0 first (plan Appendix D; the fork kept hidden RGB such as (199, 20, 80, 0)); the publish race is
  injected into `forge_core.publish_directory_no_replace`, which `staged_output` calls, instead of a private
  module-level copy. The adjusted race test also asserts that no stage directory is left behind.
- Deviation: `--despill-radius` with `native_alpha` or `opaque` is ignored and recorded as 0 (the fork's semantics),
  not an error.
- Deviation: RGBA terrain tiles (overlays, iso, hex) resize through `forge_core.resample_rgba` (premultiplied; box
  filter for reductions of 2x or more); opaque fills keep the old Pillow Lanczos in RGB, so `isolated` output pixels
  are unchanged. Terrain PNGs are now RGBA (were RGB) with identical colour values.
- The terrain tool keeps `--manifest` (legacy); a sidecar must be on the same drive as the output directory so tile
  paths stay relative.
- Not run: Linux, macOS, Python 3.10, Pillow 10.1 and numpy 1.26 floors (this machine: Windows 11, Python 3.13,
  Pillow 12.3, numpy 2.5, scipy 1.18; the module tests also pass with `FORGE_CORE_NO_SCIPY=1`). No engine import of
  iso, hex or Wang tiles was tried, and no real image-model atlas was used: every fixture is synthetic, mirroring the
  map-audit repros 10-16 and the study-asf-improved despill validation.
