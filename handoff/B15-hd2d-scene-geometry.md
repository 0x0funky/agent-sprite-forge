# B15-hd2d-scene-geometry: HD-2D stage validation and aspect layout solver, plate layout guide and prompt block, light extraction with cookie and atmosphere checks, plate-variant locality check, plate and presentation references

Branch `asf/B15-hd2d-scene-geometry` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files:
[validate_stage.py](../skills/generate2dmap/scripts/validate_stage.py) (CLI and the stage library the other three import),
[scene_layout_guide.py](../skills/generate2dmap/scripts/scene_layout_guide.py),
[extract_scene_lights.py](../skills/generate2dmap/scripts/extract_scene_lights.py),
[edit_locality_check.py](../skills/generate2dmap/scripts/edit_locality_check.py),
[hd2d-plates.md](../skills/generate2dmap/references/hd2d-plates.md),
[hd2d-presentation.md](../skills/generate2dmap/references/hd2d-presentation.md),
[tests/test_hd2d_stage.py](../tests/test_hd2d_stage.py) (30 tests),
[tests/test_scene_lights.py](../tests/test_scene_lights.py) (30) and
[tests/test_edit_locality.py](../tests/test_edit_locality.py) (18).

## 1. CLIs

Commands run from the user's project root; `<skill-dir>` is `${CLAUDE_SKILL_DIR}` in Claude Code.

    python "<skill-dir>/scripts/scene_layout_guide.py" --stage plan/stage.json --output-dir plan/guide-v1
    python "<skill-dir>/scripts/scene_layout_guide.py" --stage plan/stage.json --output-dir plan/guide-v2 --forbid "fallen logs" --strict
    python "<skill-dir>/scripts/validate_stage.py" --stage scene/stage.json --output-dir scene/stage-qa-v1
    python "<skill-dir>/scripts/validate_stage.py" --stage scene/stage.json --output-dir scene/stage-qa-v2 --aspects 4:3,16:9,21:9,9:19.5 --ui panels.json --actor-height hero=0.16 --strict
    python "<skill-dir>/scripts/extract_scene_lights.py" extract --plate scene/plate.png --stage scene/stage.json --output-dir scene/lights-v1 --expect 3 --flicker 0.25:0.15
    python "<skill-dir>/scripts/extract_scene_lights.py" cookie --lights scene/lights-v1/lights.json --plate scene/plate.png --output-dir scene/lights-v2
    python "<skill-dir>/scripts/extract_scene_lights.py" atmosphere --atmosphere scene/atmosphere.json --output-dir scene/atmosphere-qa-v1
    python "<skill-dir>/scripts/edit_locality_check.py" --before scene/plate.png --after scene/plate-lit.png --stage scene/stage.json --protect-ground --edit-box 0.62,0.18,0.74,0.42 --output-dir scene/lit-check-v1
    python "<skill-dir>/scripts/edit_locality_check.py" --before scene/plate.png --after scene/plate-night-1280.png --resize stretch --protect-band 0.62:1 --output-dir scene/night-check-v1
    python "<skill-dir>/scripts/edit_locality_check.py" --before scene/plate.png --after scene/plate-night.png --conform scene/night-conform/conform.json --protect-band 0.62:1 --output-dir scene/night-check-v2

- `validate_stage.py` writes `stage-qa.json` (QA envelope, `generate2dmap.stage_qa.v1`, with every check and every solved layout), `stage-overlay.png` (plate with ground, band, regions, effects and labelled slots) and `layout-<aspect>.png` per aspect (default 4:3, 16:9, 21:9 and 9:19.5), plus `layout-<aspect>-boss.png` when the stage has a boss slot. The plate defaults to the stage's `plate`, relative to the stage file; a missing plate validates geometry only, with a warning.
- `scene_layout_guide.py` writes `guide.png` (sourceSize, flat colours, 10% grid, legend), `prompt-block.txt` (ASCII, whole percent) and `guide.json` (`generate2dmap.scene_guide.v1` with the percentages, forbidden list, bakedContent and the plan's QA). Same stage, same bytes.
- `extract_scene_lights.py` has three verbs: `extract` (plate to `lights.json` `generate2dmap.lights.v1`, `light-cookie.png` 512x288 RGB, `lights-overlay.png`, `lights-qa.json`), `cookie` (re-bakes an edited `lights.json`; overlay only with `--plate`) and `atmosphere` (`atmosphere-qa.json` for an `atmosphere.v1` file).
- `edit_locality_check.py` writes `locality-qa.json` (`generate2dmap.edit_locality.v1`) and `locality-diff.png` (changes red over the dimmed variant, protected regions orange or red, edit boxes green).
- Every `--help` (and each `extract_scene_lights.py` verb's) is ASCII and exits 0 under cp1252 and cp950 (tested).
- Success prints one ASCII JSON line: `output_dir`, `metadata` (the main JSON) and the tool's files (`overlay`, `renders`; `guide`, `prompt_block`; `report`, `cookie`, `overlay`, `lights`; `diff`), `status` (the envelope status) and `failed` (ids of failed checks).
- Exit codes: 0 for pass, warn and needs-visual-review; 1 when a check fails (the report is still published, so the overlays can be inspected); 1 with `error: ...` on stderr and nothing published for bad input, an existing `--output-dir`, or a failed check under `--strict`. argparse usage errors keep argparse's exit 2.
- All four refuse an existing `--output-dir` and publish through `forge_core.staged_output` only after QA.

## 2. SKILL.md routing rows

generate2dmap, route table:

| Need | Route |
|---|---|
| HD-2D battle, story or fixed-camera plate (actors stand on a painting) | read `references/hd2d-plates.md`: plan `stage.json`, then `scripts/scene_layout_guide.py`, generate the plate with `guide.png` attached and `prompt-block.txt` pasted, measure it, fix the stage, then `scripts/validate_stage.py` and look at every `layout-*.png` |
| Lights, cookie tint, shadows, depth blur, bloom and grade over a plate | read `references/hd2d-presentation.md`; `scripts/extract_scene_lights.py extract --stage ...`, curate `lights.json`, `cookie` to re-bake, `atmosphere` to check the atmosphere file |
| Night, lit or damaged variant of an accepted plate | `scripts/edit_locality_check.py --before master --after variant --stage stage.json --edit-box ...`; record a resize with `--conform` or `--resize`; regenerate on failure |
| Environment motion on a validated plate | after validate_stage: build_motion_mask, then scene_motion build and qa on the decoded file (B16); stage regions with `appliesTo` `motion` are its protected regions |

generate2dmap, tools table:

| Tool | Use |
|---|---|
| `scripts/scene_layout_guide.py` | Planned stage to a flat layout guide and a percent prompt block (band, ground outline, standing spots, landmarks, surfaces, forbidden-in-walk, bakedContent); deterministic |
| `scripts/validate_stage.py` | stage.v1 checks (feet and footprints on ground, off water and no-walk regions, band, polygons, effect overlaps, plate size) and a layout solver per aspect (cover/contain, HUD panels, scale ladder, foot-y order) with renders; also the stage library of the HD-2D tools |
| `scripts/extract_scene_lights.py` | Light candidates from a plate (contrast peaks, each measured against its own peak: centroid, glow hue, radius, strength) to lights.v1 with a 512x288 cookie and overlay; `cookie` re-bakes after curation; `atmosphere` checks atmosphere.v1 values and mote budgets |
| `scripts/edit_locality_check.py` | Plate variant vs master: protected regions and everything outside the edit boxes unchanged, through a recorded resize; diff image |

Processing-notes bullets (generate2dmap):

- Stage, slot, region and light coordinates are UV of the plate (0..1, y down); slots are foot roots. A protected region's `appliesTo` says what it blocks: `walk`, `motion`, `edit`; none means all three.
- A foot is standable when it and 8 points on its footprint ellipse (radius 0.01 of the plate width, height x0.58) lie on ground and off water and no-walk regions. A layout failure inside `reviewedAspectRange` fails the stage; outside it only warns.
- With a plate the best stage status is `needs-visual-review`: the numbers cannot see the painting.
- Light extraction proposes candidates; curate them on real art.

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `scene_layout_guide.py` | Turns a planned HD-2D stage into a layout guide image and a percent prompt block with the forbidden-in-walk list and bakedContent | tests/test_hd2d_stage.py: byte-identical reruns, prompt percentages (half-up), guide pixels, bakedContent and forbid options |
| `validate_stage.py` | Checks an HD-2D stage (slots, footprints, water, no-walk regions, band, polygons) and solves and renders the battle layout at 4:3, 16:9, 21:9 and 9:19.5 | tests/test_hd2d_stage.py: a slot on water fails; 4 renders per formation; every placed foot inside a ground polygon by an independent test; UI panels avoided; reviewed-range ends; documented commands parse |
| `extract_scene_lights.py` | Finds painted lights (centroid, glow hue, radius), bakes a 512x288 light cookie and overlay, re-bakes after curation, checks atmosphere.v1 | tests/test_scene_lights.py: 3 lanterns found as 3 lights within 4 px (measured 0.1 px or less); window band, sunlit panel, cool dot and off-hue glow rejected; floor glints rejected with --stage; cookie pools; atmosphere values |
| `edit_locality_check.py` | Proves a plate variant changed only where allowed, through a recorded resize | tests/test_edit_locality.py: band edit fails, local edit passes, relighting fails on MAE, a lamp going out elsewhere fails, bicubic resize passes |

## 4. CHANGELOG entries

- Added: `validate_stage.py`: stage.v1 contract and cross-field checks, footprint-aware standability (Appendix C ellipse), water and no-walk regions, playable band, effect overlaps, plate size; greedy layout solver at 4:3, 16:9, 21:9 and 9:19.5 (cover or contain projection with `objectPosition`, reserved HUD panels with a `battle` profile or `--ui`, scale ladder 1.0 to `--min-scale`, foot-y draw order, party-vs-enemies and party-vs-boss); overlays and renders (B15-T1).
- Added: `scene_layout_guide.py`: guide.png, prompt-block.txt with the band, ground outline and slots in whole percent, the always-visible window of the reviewed aspects, the forbidden-in-walk list and bakedContent; deterministic (B15-T2).
- Added: `extract_scene_lights.py extract|cookie|atmosphere`: light candidates as peaks of local contrast, each measured over its own half-peak region (a lamp stays apart from the lit wall around it, broad bright areas are dropped), glow-hue colour, stage-aware rejection of floor glints and water reflections, lights.v1, a 512x288 light cookie, overlays; cookie re-baking after curation; atmosphere.v1 checks with mote budgets (B15-T3).
- Added: `edit_locality_check.py`: plate-variant locality through identity, conform.json, cover or stretch transforms; protected stage regions, ground, bands and boxes; outside-edit rule with change components; exact mode (B15-T4).
- Added: references `hd2d-plates.md` (stage contract, route, prompt, variants) and `hd2d-presentation.md` (layer stack, cookie tint and flicker, shadows, tilt-shift, bloom and grade, atmosphere, orthographic ground mesh, rim outline) (B15-T5).
- Changed: none (all new tools). BREAKING: none. Fixed: none (B15 has no audit finding ids in the plan's coverage matrix).

## 5. Schema change requests

Producer: B15 (all four tools). Consumers: agents, games, B17's scene preview if it reads stages or lights, B13's map_bundle (it schema-checks `stage`, `lights` and `atmosphere` references). Apply to `shared/schemas/map.schema.json`, then run `python tools/vendor_sync.py --write` (map.schema.json is vendored into generate2dmap and codeart2d). The same fragments are applied in memory by `requested_errors()` in tests/test_hd2d_stage.py (`REQUESTED_MAP_DEFS`, `REQUESTED_STAGE_PROPERTIES`), tests/test_scene_lights.py (`REQUESTED_MAP_DEFS`, `REQUESTED_LIGHTS_PROPERTIES`, `REQUESTED_LIGHT_ITEM_PROPERTIES`) and tests/test_edit_locality.py (`REQUESTED_MAP_DEFS`); every document the tools write validates against them, and the stage, lights and atmosphere documents also validate against the frozen schema unchanged. Once applied, those helpers can become `forge_testutils.assert_valid_contract(..., "map", "<def>", skill="generate2dmap")`.

5.1 New `$defs` (merge into `/$defs`; nothing existing changes):

```json
{
  "stageLayout": {
    "description": "One solved battle layout of validate_stage.py: the plate projected onto a reference viewport (fit, scale, offset), the reserved UI panels and each actor's slot, chosen foot (viewport pixels and plate UV), box [x0, y0, x1, y1) in viewport pixels, scale-ladder factor (null when unplaced) and the foot-y draw order.",
    "type": "object",
    "required": [
      "aspect",
      "ratio",
      "formation",
      "render",
      "viewport",
      "scale",
      "actors",
      "drawOrder"
    ],
    "properties": {
      "aspect": {
        "type": "string",
        "minLength": 1
      },
      "ratio": {
        "type": "number",
        "exclusiveMinimum": 0
      },
      "formation": {
        "enum": [
          "party-vs-enemies",
          "party-vs-boss"
        ]
      },
      "render": {
        "$ref": "common.schema.json#/$defs/relPath"
      },
      "viewport": {
        "$ref": "common.schema.json#/$defs/size2"
      },
      "fit": {
        "enum": [
          "cover",
          "contain"
        ]
      },
      "scale": {
        "type": "number",
        "exclusiveMinimum": 0
      },
      "offset": {
        "$ref": "common.schema.json#/$defs/point2"
      },
      "visibleUv": {
        "$ref": "#/$defs/uvBox"
      },
      "ui": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "id",
            "box"
          ],
          "properties": {
            "id": {
              "type": "string",
              "minLength": 1
            },
            "box": {
              "$ref": "common.schema.json#/$defs/box"
            }
          }
        }
      },
      "actors": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "id",
            "role",
            "slot",
            "foot",
            "footUv",
            "box",
            "sizePx",
            "scale",
            "placed",
            "grounded"
          ],
          "properties": {
            "id": {
              "type": "string",
              "minLength": 1
            },
            "role": {
              "enum": [
                "hero",
                "enemy",
                "boss"
              ]
            },
            "slot": {
              "$ref": "#/$defs/uvPoint"
            },
            "desired": {
              "$ref": "common.schema.json#/$defs/point2"
            },
            "foot": {
              "$ref": "common.schema.json#/$defs/point2"
            },
            "footUv": {
              "$ref": "common.schema.json#/$defs/point2"
            },
            "box": {
              "$ref": "common.schema.json#/$defs/box"
            },
            "sizePx": {
              "type": "number",
              "exclusiveMinimum": 0
            },
            "scale": {
              "type": [
                "number",
                "null"
              ],
              "exclusiveMinimum": 0,
              "maximum": 1
            },
            "placed": {
              "type": "boolean"
            },
            "grounded": {
              "type": "boolean"
            },
            "movedPx": {
              "type": "number",
              "minimum": 0
            }
          }
        }
      },
      "drawOrder": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "overlaps": {
        "type": "array",
        "items": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "minItems": 2,
          "maxItems": 2
        }
      },
      "errors": {
        "type": "array",
        "items": {
          "type": "string"
        }
      }
    }
  },
  "stage_qa_v1": {
    "description": "validate_stage.py report (stage-qa.json): a QA envelope over a stage.v1 file plus the stage summary, the solver settings and every solved layout.",
    "allOf": [
      {
        "$ref": "common.schema.json#/$defs/qaEnvelope"
      }
    ],
    "type": "object",
    "required": [
      "schema",
      "stage",
      "settings",
      "layouts"
    ],
    "properties": {
      "schema": {
        "const": "generate2dmap.stage_qa.v1"
      },
      "stage": {
        "type": "object",
        "required": [
          "sourceSize",
          "fit",
          "reviewedAspectRange"
        ],
        "properties": {
          "sourceSize": {
            "$ref": "common.schema.json#/$defs/size2"
          },
          "fit": {
            "enum": [
              "cover",
              "contain"
            ]
          },
          "reviewedAspectRange": {
            "type": "array",
            "items": {
              "type": "number"
            },
            "minItems": 2,
            "maxItems": 2
          },
          "objectPosition": {
            "$ref": "#/$defs/uvPoint"
          },
          "slots": {
            "type": "integer",
            "minimum": 0
          }
        }
      },
      "settings": {
        "type": "object"
      },
      "layouts": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/stageLayout"
        }
      },
      "warnings": {
        "type": "array",
        "items": {
          "type": "string"
        }
      }
    }
  },
  "scene_guide_v1": {
    "description": "scene_layout_guide.py metadata (guide.json): the files written, the plan restated in whole percent of the canvas (x from the left, y from the top), the forbidden-in-walk list, bakedContent and a QA envelope of the plan's own consistency.",
    "type": "object",
    "required": [
      "schema",
      "stage",
      "canvas",
      "guide",
      "promptBlock",
      "percent",
      "forbiddenInWalk",
      "bakedContent",
      "qa"
    ],
    "properties": {
      "schema": {
        "const": "generate2dmap.scene_guide.v1"
      },
      "tool": {
        "$ref": "common.schema.json#/$defs/toolInfo"
      },
      "stage": {
        "$ref": "common.schema.json#/$defs/fileRef"
      },
      "canvas": {
        "$ref": "common.schema.json#/$defs/size2"
      },
      "guide": {
        "$ref": "common.schema.json#/$defs/fileRef"
      },
      "promptBlock": {
        "$ref": "common.schema.json#/$defs/fileRef"
      },
      "percent": {
        "type": "object",
        "required": [
          "ground",
          "slots"
        ],
        "properties": {
          "ground": {
            "type": "object",
            "required": [
              "box",
              "polygons"
            ],
            "properties": {
              "box": {
                "type": "array",
                "items": {
                  "type": "integer",
                  "minimum": 0,
                  "maximum": 100
                },
                "minItems": 4,
                "maxItems": 4
              },
              "polygons": {
                "type": "array",
                "items": {
                  "type": "array",
                  "items": {
                    "type": "array",
                    "items": {
                      "type": "integer",
                      "minimum": 0,
                      "maximum": 100
                    },
                    "minItems": 2,
                    "maxItems": 2
                  },
                  "minItems": 3
                }
              }
            }
          },
          "band": {
            "anyOf": [
              {
                "type": "null"
              },
              {
                "type": "object",
                "required": [
                  "y"
                ],
                "properties": {
                  "y": {
                    "type": "array",
                    "items": {
                      "type": "integer",
                      "minimum": 0,
                      "maximum": 100
                    },
                    "minItems": 2,
                    "maxItems": 2
                  },
                  "x": {
                    "anyOf": [
                      {
                        "type": "null"
                      },
                      {
                        "type": "array",
                        "items": {
                          "type": "integer",
                          "minimum": 0,
                          "maximum": 100
                        },
                        "minItems": 2,
                        "maxItems": 2
                      }
                    ]
                  }
                }
              }
            ]
          },
          "alwaysVisible": {
            "type": "array",
            "items": {
              "type": "integer",
              "minimum": 0,
              "maximum": 100
            },
            "minItems": 4,
            "maxItems": 4
          },
          "slots": {
            "type": "array",
            "items": {
              "type": "object",
              "required": [
                "id",
                "role",
                "x",
                "y"
              ],
              "properties": {
                "id": {
                  "type": "string",
                  "minLength": 1
                },
                "role": {
                  "enum": [
                    "hero",
                    "enemy",
                    "boss"
                  ]
                },
                "x": {
                  "type": "integer",
                  "minimum": 0,
                  "maximum": 100
                },
                "y": {
                  "type": "integer",
                  "minimum": 0,
                  "maximum": 100
                }
              }
            }
          },
          "approach": {
            "type": "array",
            "items": {
              "type": "object",
              "required": [
                "x",
                "y"
              ],
              "properties": {
                "x": {
                  "type": "integer",
                  "minimum": 0,
                  "maximum": 100
                },
                "y": {
                  "type": "integer",
                  "minimum": 0,
                  "maximum": 100
                }
              }
            }
          },
          "landmarks": {
            "type": "array",
            "items": {
              "type": "object",
              "required": [
                "id",
                "box"
              ],
              "properties": {
                "id": {
                  "type": "string"
                },
                "box": {
                  "type": "array",
                  "items": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100
                  },
                  "minItems": 4,
                  "maxItems": 4
                }
              }
            }
          },
          "surfaces": {
            "type": "array",
            "items": {
              "type": "object",
              "required": [
                "id",
                "kind",
                "box"
              ],
              "properties": {
                "id": {
                  "type": "string"
                },
                "kind": {
                  "enum": [
                    "ripple",
                    "shimmer",
                    "sway",
                    "glow"
                  ]
                },
                "box": {
                  "type": "array",
                  "items": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100
                  },
                  "minItems": 4,
                  "maxItems": 4
                }
              }
            }
          }
        }
      },
      "forbiddenInWalk": {
        "type": "array",
        "items": {
          "type": "string",
          "minLength": 1
        }
      },
      "bakedContent": {
        "type": "object"
      },
      "qa": {
        "$ref": "common.schema.json#/$defs/qaEnvelope"
      },
      "warnings": {
        "type": "array",
        "items": {
          "type": "string"
        }
      }
    }
  },
  "lights_qa_v1": {
    "description": "extract_scene_lights.py report (lights-qa.json): a QA envelope over the plate or the edited lights file, with the detector settings and statistics and each light's measured blob.",
    "allOf": [
      {
        "$ref": "common.schema.json#/$defs/qaEnvelope"
      }
    ],
    "type": "object",
    "required": [
      "schema"
    ],
    "properties": {
      "schema": {
        "const": "generate2dmap.lights_qa.v1"
      },
      "detector": {
        "type": "object"
      },
      "lights": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "id",
            "u",
            "v"
          ],
          "properties": {
            "id": {
              "type": "string"
            },
            "u": {
              "type": "number",
              "minimum": 0,
              "maximum": 1
            },
            "v": {
              "type": "number",
              "minimum": 0,
              "maximum": 1
            },
            "strength": {
              "type": "number",
              "minimum": 0,
              "maximum": 1
            },
            "corePx": {
              "type": "integer",
              "minimum": 0
            },
            "box": {
              "$ref": "common.schema.json#/$defs/box"
            },
            "touchesEdge": {
              "type": "boolean"
            }
          }
        }
      },
      "rejected": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "u",
            "v",
            "reason"
          ],
          "properties": {
            "u": {
              "type": "number"
            },
            "v": {
              "type": "number"
            },
            "strength": {
              "type": "number"
            },
            "reason": {
              "enum": [
                "glow hue",
                "on ground or water"
              ]
            }
          }
        }
      }
    }
  },
  "atmosphere_qa_v1": {
    "description": "extract_scene_lights.py atmosphere report (atmosphere-qa.json): a QA envelope over an atmosphere.v1 file with its mote, shaft and mist summary.",
    "allOf": [
      {
        "$ref": "common.schema.json#/$defs/qaEnvelope"
      }
    ],
    "type": "object",
    "required": [
      "schema",
      "summary"
    ],
    "properties": {
      "schema": {
        "const": "generate2dmap.atmosphere_qa.v1"
      },
      "summary": {
        "type": "object",
        "required": [
          "moteLayers",
          "motes",
          "mobileMotes",
          "shafts",
          "mist"
        ],
        "properties": {
          "moteLayers": {
            "type": "integer",
            "minimum": 0
          },
          "motes": {
            "type": "integer",
            "minimum": 0
          },
          "mobileMotes": {
            "type": "integer",
            "minimum": 0
          },
          "shafts": {
            "type": "integer",
            "minimum": 0
          },
          "mist": {
            "type": "boolean"
          }
        }
      }
    }
  },
  "edit_locality_v1": {
    "description": "edit_locality_check.py report (locality-qa.json): a QA envelope comparing a plate variant with its master through a recorded transform (out = (src - src_rect[0:2]) * scale); per protected region, outside the edit boxes and overall: changed pixels and fraction, mean absolute difference, p99 and the largest change components.",
    "allOf": [
      {
        "$ref": "common.schema.json#/$defs/qaEnvelope"
      }
    ],
    "type": "object",
    "required": [
      "schema",
      "transform",
      "comparison",
      "regions"
    ],
    "properties": {
      "schema": {
        "const": "generate2dmap.edit_locality.v1"
      },
      "transform": {
        "type": "object",
        "required": [
          "kind",
          "scale",
          "src_rect",
          "out_size"
        ],
        "properties": {
          "kind": {
            "enum": [
              "identity",
              "conform",
              "cover",
              "stretch"
            ]
          },
          "scale": {
            "type": "array",
            "items": {
              "type": "number",
              "exclusiveMinimum": 0
            },
            "minItems": 2,
            "maxItems": 2
          },
          "src_rect": {
            "$ref": "common.schema.json#/$defs/box"
          },
          "out_size": {
            "$ref": "common.schema.json#/$defs/size2"
          },
          "map": {
            "type": "string"
          }
        }
      },
      "comparison": {
        "type": "object",
        "required": [
          "space",
          "size",
          "blur",
          "pixelThreshold",
          "exact"
        ],
        "properties": {
          "space": {
            "enum": [
              "before",
              "after"
            ]
          },
          "size": {
            "$ref": "common.schema.json#/$defs/size2"
          },
          "blur": {
            "type": "integer",
            "minimum": 0
          },
          "pixelThreshold": {
            "type": "number",
            "minimum": 0
          },
          "exact": {
            "type": "boolean"
          }
        }
      },
      "regions": {
        "type": "array",
        "items": {
          "type": "object",
          "required": [
            "id",
            "origin",
            "kind",
            "uv",
            "status"
          ],
          "properties": {
            "id": {
              "type": "string",
              "minLength": 1
            },
            "origin": {
              "enum": [
                "stage",
                "cli"
              ]
            },
            "kind": {
              "enum": [
                "polygon",
                "box"
              ]
            },
            "uv": {
              "anyOf": [
                {
                  "$ref": "#/$defs/uvBox"
                },
                {
                  "$ref": "#/$defs/uvPolygon"
                }
              ]
            },
            "status": {
              "enum": [
                "pass",
                "fail",
                "skipped"
              ]
            }
          }
        }
      },
      "editBoxes": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/uvBox"
        }
      },
      "warnings": {
        "type": "array",
        "items": {
          "type": "string"
        }
      }
    }
  }
}
```

5.2 New optional property of `stage_v1` (pointer `/$defs/stage_v1/properties/objectPosition`), read by validate_stage, scene_layout_guide and edit_locality_check: where the projection window sits in the plate, like CSS `object-position`, default [0.5, 0.5]:

```json
{
  "objectPosition": {
    "$ref": "#/$defs/uvPoint"
  }
}
```

Please also extend the description of `/$defs/stage_v1/properties/protectedRegions/items/properties/appliesTo` with: "Uses: walk (no feet or slots), motion (environment motion keeps out), edit (plate variants must not change it); a region without appliesTo applies to all three; other strings are ignored by the B15 tools." The tools warn about unknown values; the schema keeps them open.

5.3 New optional properties of `lights_v1` (merge into `/$defs/lights_v1/properties`): the tool, the plate fileRef, `sourceSize`, `radiusUnit` (the radius is a fraction of the plate width; the frozen schema did not say), `ambient`, `cookieSize`, `cookieSha256`:

```json
{
  "tool": {
    "$ref": "common.schema.json#/$defs/toolInfo"
  },
  "plate": {
    "$ref": "common.schema.json#/$defs/fileRef"
  },
  "sourceSize": {
    "$ref": "common.schema.json#/$defs/size2"
  },
  "radiusUnit": {
    "enum": [
      "plate-width"
    ]
  },
  "ambient": {
    "$ref": "common.schema.json#/$defs/color"
  },
  "cookieSize": {
    "$ref": "common.schema.json#/$defs/size2"
  },
  "cookieSha256": {
    "$ref": "common.schema.json#/$defs/sha256"
  }
}
```

and into `/$defs/lights_v1/properties/lights/items/properties` (replacing the frozen `flicker`):

```json
{
  "id": {
    "type": "string",
    "minLength": 1
  },
  "strength": {
    "type": "number",
    "minimum": 0,
    "maximum": 1
  },
  "flicker": {
    "anyOf": [
      {
        "type": "number",
        "minimum": 0
      },
      {
        "type": "object",
        "required": [
          "hz"
        ],
        "properties": {
          "hz": {
            "type": "number",
            "exclusiveMinimum": 0
          },
          "depth": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "phase": {
            "type": "number"
          }
        }
      }
    ]
  }
}
```

The `flicker` object form gains `required: ["hz"]` and typed `hz`, `depth`, `phase`. This one is a tightening, not an addition: the frozen schema accepted any object. The only existing flicker object, the A0 fixture's `{"hz": 3, "depth": 0.2}`, still validates, and `extract_scene_lights.py cookie` already refuses an object without `hz`. If integration prefers additive-only changes, drop `required` and keep the parser stricter than the contract.

## 6. Shared-helper promotion requests

In skills/generate2dmap/scripts/validate_stage.py (imported by the other three B15 scripts as `vs`) unless noted:

- `_local_points_in_polygon(points, polygon) -> ndarray[bool]` (validate_stage.py:366): even-odd crossing test vectorised over points, the Appendix C walk-region rule and the reference layout's `inside()`. Proposed `forge_core.points_in_polygon`; B13's map_nav and B17's parity test need exactly this rule. Test: test_points_in_polygon_matches_plain_python.
- `_local_polygon_mask(polygon_px, size) -> ndarray[bool]` (:384) and `_local_box_mask(box_px, size)` (:400): pixel-centre rasters (half-open boxes), restricted to the polygon's bounding box. Proposed `forge_core.polygon_mask` / `box_mask`; B16's motion masks, B13's nav rasters and B11's `_local_shape_mask` are the same idea. Same test.
- `_local_box_mean(plane, radius)` (:410): summed-area-table box mean with replicated edges, float64. Proposed `forge_core.box_mean`. Used by light detection and the locality blur.
- `_local_dilate(mask, radius)` (:422): Chebyshev dilation from a summed-area table; forge_core already has the same as private `_dilate_square`, so making that public (`dilate_mask`) would let this go.
- `_local_max_filter(plane, radius)` (extract_scene_lights.py:111): square-window maximum as two separable passes of shifted maxima, numpy only. Proposed `forge_core.max_filter` (scipy's `maximum_filter` with mode `nearest` gives the same result). Peak detection.
- `_local_file_ref(path, base, sha256=None) -> dict` (:532): A1's portable-path rule (manifest-relative POSIX; only the file name on another drive). The same request as B11's `_local_file_ref`; proposed `forge_core.file_ref`.
- `_local_qa_envelope(checks, *, method, not_proven, inputs, outputs, tool, visual=False) -> dict` (:541): status fail > warn > needs-visual-review > pass, no createdAt. Proposed `forge_core.qa_envelope`; every Wave B module builds the same envelope.

## 7. Cross-module links that Z must add

- skills/generate2dmap/SKILL.md: the routing rows, tools rows and processing notes of section 2; link references/hd2d-plates.md and references/hd2d-presentation.md from the route table (HD-2D route per the plan: scene_layout_guide, plate, validate_stage, extract_scene_lights, then scene_motion).
- background-scenes.md (B16): point to hd2d-plates.md for stage geometry, protected regions and variants; a stage region with `appliesTo` `motion` becomes a motion-plan `protected` entry at `uv * sourceSize`. hd2d-plates.md already links background-scenes.md and names build_motion_mask and scene_motion in plain text.
- B16 build_motion_mask: could accept `--stage stage.json` and turn `appliesTo` `motion` regions into exact-zero protected cores, so the stage stays the one source of truth.
- B12 conform_background.py: edit_locality_check reads its conform.json (`transform.scale`, `src_rect`, `out_size`) with `--conform`; hd2d-plates.md names conform_background.py in plain text because it is not in this worktree. Once both land, link it.
- B13 map_bundle.py: stage, lights and atmosphere references are schema-checked there; point its docs at validate_stage.py and `extract_scene_lights.py atmosphere` for the semantic checks.
- B17 build_scene_preview: if it previews HD-2D plates, it can read the solved feet (`stage-qa.json` `layouts[].actors[].footUv`) and the cookie.
- Integration e2e (Appendix I pipeline 5, HD-2D): `python skills/generate2dmap/scripts/validate_stage.py --stage <synthetic stage> --output-dir <new dir>`; make_stage() and paint_plate() in tests/test_hd2d_stage.py are ready-made synthetic inputs.
- README tool table: section 3. CHANGELOG: section 4.

## 8. Known limitations and what is not proven

- The layout solver is the shipped battle layout's greedy grid search, made data-driven: 25 x 27 candidates per actor and ladder step, actors placed in slot order. It finds a valid foot, not the best formation, and can miss feasible slivers narrower than a grid step (a few pixels). Actor boxes are rectangles (default heights 0.2, 0.18, 0.4 of the plate, square, foot at 96%); depth-dependent actor scale, safe-area insets and the game's own layout code are not modelled. The `battle` HUD profile was measured from one shipped game at 1280x720 and 390x844.
- Real plates (four owner battle plates, 1672x941, read-only, not in the repo; run by hand): validate_stage placed every actor at all four aspects on the port plate, feet on the dry floor, and the renders matched a visual check. Light extraction with the defaults: the 32 strongest candidates of each plate contain all 11 lights annotated in that game's own stage config (1.2-9.3 px from the hand-placed annotation); on the port plate the 16 strongest were lamps, flames and lit windows on visual inspection, with a few wet-stone highlights. A first version that thresholded one global contrast mask found 5 of 11: lit walls chained the lamps into regions too large to keep, which is why each light is now measured against its own peak; `--min-brightness` 0.7 and `--merge` 0.012 were chosen on these plates (same recall, half the candidates). Window rows, wet-floor glints and reflections still rank among real lights, and a lamp no brighter than its surroundings is missed: the list needs curation (documented, with the `cookie` verb). The 4 px acceptance is proven on synthetic plates (measured 0.1 px or less); the thresholds are tuned on four plates of one game.
- Edit-locality thresholds (24 levels after a 3x3 blur, 0.2% changed, MAE 3, one component of 0.05% of the image) are synthetic: a bicubic-resized variant compared through a Lanczos resample gave MAE 0.14 and no changed pixel in the protected band, a global 8% darkening MAE 8.8, a 14 px lamp going out a 0.12% change caught by the component rule. Not calibrated on real image-model edits (JPEG blocks, colour drift, sub-pixel shifts).
- Prompt percentages are whole percent; the guide does not make a generator obey it.
- Fonts are Pillow's bundled default: PNG bytes repeat for one Pillow/FreeType/zlib build; across builds compare pixels.
- Not run: Linux, macOS, Python 3.10, Pillow 10.1, numpy 1.26. Run here: Windows 11, Python 3.13, Pillow 12.3, numpy 2.5, scipy 1.18; the B15 tests also pass with `FORGE_CORE_NO_SCIPY=1`; the four scripts and three test files parse with the Python 3.10 grammar.
- Deviations from the plan, with reasons:
  - extract_scene_lights.py has a third verb, `cookie`, because detected lights must be curated on real art and the cookie has to follow the edits; and `--stage`/`--keep-floor`, because floor glints and reflections were the main false positives on real plates.
  - validate_stage.py renders both formations: the stage's boss slot is an alternative to the enemies, so 4 renders per formation (8 with a boss slot). The 4-render acceptance holds per formation.
  - scene_layout_guide.py also writes guide.json, so the one-line summary has a metadata path and the plan's QA is recorded.
  - validate_stage.py doubles as the stage library of the other three scripts instead of a separate module, to stay within the plan's file list.
  - Decisions where the plan and schema were silent: `appliesTo` vocabulary (walk, motion, edit; none = all); light `radius` as a fraction of the plate width (`radiusUnit`); `objectPosition` read from the stage; reviewed-range comparisons with 0.1% slack (ranges are written rounded, 2.3333 for 21:9); with a plate the best stage status is needs-visual-review.
