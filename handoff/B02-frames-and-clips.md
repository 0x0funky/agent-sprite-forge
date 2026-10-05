# B02-frames-and-clips: assemble_frames and build_animation_clips v2 (lossless inputs, spill-safe slicing, loop tools, clips v2 timing, events and transitions, reviews, docs split)

Branch `asf/B02-frames-and-clips`, from `wip/asf-upgrade-20261005` @ 3f9252d. Commits:

1. T1: the fork's 29 tests, ported unchanged and passing against the unmodified scripts.
2. T2 to T5: the scripts and tests.
3. T6: the docs and this handoff.

Files changed:

- Scripts: [assemble_frames.py](../skills/generate2dsprite/scripts/assemble_frames.py) and [build_animation_clips.py](../skills/generate2dsprite/scripts/build_animation_clips.py).
- References: [character-animation.md](../skills/generate2dsprite/references/character-animation.md), now an index, and the new [frames-and-clips.md](../skills/generate2dsprite/references/frames-and-clips.md), [animation-planning.md](../skills/generate2dsprite/references/animation-planning.md) and [runtime-integration.md](../skills/generate2dsprite/references/runtime-integration.md).
- Tests: [tests/test_assemble_frames.py](../tests/test_assemble_frames.py) and [tests/test_build_animation_clips.py](../tests/test_build_animation_clips.py).

## 1. CLIs

Run from the user's project root; `<skill-dir>` is the generate2dsprite folder (`${CLAUDE_SKILL_DIR}` in Claude Code):

    python "<skill-dir>/scripts/build_animation_clips.py" --manifest output/hero/clips.json --output-dir output/hero/clips
    python "<skill-dir>/scripts/build_animation_clips.py" --manifest output/hero/clips.json --output-dir output/hero/clips-review --preview-scale 4 --preview-background checker --strict
    python "<skill-dir>/scripts/assemble_frames.py" --input output/scene/f0.png output/scene/f1.png output/scene/f2.png output/scene/f3.png --duration 250 --output-dir output/scene/loop
    python "<skill-dir>/scripts/assemble_frames.py" --sheet output/fox/raw-sheet.png --rows 2 --cols 4 --slice ownership --output-dir output/fox/frames
    python "<skill-dir>/scripts/assemble_frames.py" --sheet output/props/sheet.png --crop-boxes output/props/boxes.json --key chroma --output-dir output/props/frames
    python "<skill-dir>/scripts/assemble_frames.py" --input output/water/w0.png output/water/w1.png output/water/w2.png output/water/w3.png output/water/w4.png output/water/w5.png --loop-overlap 2 --ambient --output-dir output/water/loop
    python "<skill-dir>/scripts/assemble_frames.py" --input output/scene/s0.png output/scene/s1.png output/scene/s2.png --static-regions output/scene/regions.json --static-region-search 8 --output-dir output/scene/check

`build_animation_clips.py` takes these flags:

| Flag | Meaning |
|---|---|
| `--manifest` | The v1 or v2 clips manifest. |
| `--output-dir` | A new directory. |
| `--preview-scale N` | Integer nearest-neighbour scale for the WebP previews, the contact sheet and the reviews; frames stay native. |
| `--preview-background` | `all`, `light`, `dark`, `checker` or `#rrggbb`. |
| `--no-reviews` | Skip the review sheets. |
| `--tick-hz` | The tick grid of the drift report; default 60. |
| `--near-duplicate-mae` | The hold threshold; default 1.5. |
| `--strict` | Any lint fails the build and publishes nothing. |

`assemble_frames.py` takes these flags:

| Flag | Meaning |
|---|---|
| `--input` or `--sheet` | The source frames. |
| `--rows`/`--cols` or `--crop-boxes` | How to cut a sheet. |
| `--sequence`, `--duration` | Played order and per-position duration. |
| `--key none\|chroma`, `--key-color`, `--key-quality`, `--pixel-art` | Optional chroma keying. |
| `--slice grid\|ownership`, `--allow-spill`, `--spill-threshold`, `--spill-min-area`, `--attach-radius` | The spill check and ownership slicing. |
| `--static-regions`, `--static-region-search N` | Areas that must stay still, and their drift search. |
| `--loop-overlap K`, `--ambient` | The ambient-only crossfade. |
| `--manifest` | User provenance. |
| `--strict` | Any QA warning fails the run. |

Behaviour of both CLIs:

- `--help` is ASCII and exits 0 under cp1252 and cp950 (`CliTests.test_help_works_under_cp1252_and_cp950`).
- Errors, including argparse usage errors, print `error: ...` to stderr and exit 1.
- Success prints one JSON line:
  - `build_animation_clips`: `status`, `output`, `metadata` (absolute path of `animation-clips.json`), `schema`, `clips`, `frames`, `warnings`, `qa`.
  - `assemble_frames`: `status`, `output`, `metadata` (absolute path of `animation.json`), `frames`, `sequence`, `total_duration_ms`, `qa`.

Python API:

- `build_animation_clips.build(manifest_path, output_dir, *, preview_scale=1, preview_background="all", reviews=True, tick_hz=60, near_duplicate_mae=1.5, strict=False) -> dict`. The positional form `build(manifest, output_dir)` is unchanged, as B01's tests call it.
- `assemble_frames.assemble(args)` and `build_parser()` are unchanged.
- Reusable helpers: `ticks_to_ms`, `tick_grid_report`, `playback_order`, `telegraph_report`, `clip_holds`, `near_duplicate_pairs`, `dissolve` and `review_font` in build_animation_clips; `parse_crop_boxes`, `spill_report`, `ownership_slice`, `loop_overlap`, `loop_seam`, `blend_rgba`, `ncc_drift`, `save_animation` and `manifest_path` in assemble_frames.

## 2. SKILL.md routing rows

generate2dsprite:

| Need | Route |
|---|---|
| Registered frames on one canvas (processed, scaled or code-art) that need clips, timing, events or transitions | `scripts/build_animation_clips.py --manifest clips.json --output-dir <new>`; a v2 manifest adds ticks, loop_policy, events, keys, entry_frame, transitions, hitstop_ticks and role. Read the lints and `review/`, then export. |
| Complete frames, a scene loop or a sheet of whole images | `scripts/assemble_frames.py`. Use `--key chroma` for a chroma sheet, `--slice ownership` when a tail or weapon crosses its cell, and `--loop-overlap K --ambient` for ambient loops only (never characters). |
| Code-art frames (codeart2d) | Straight to `scripts/build_animation_clips.py`, never through `generate2dsprite.py process`. |
| Plan poses, NEAR/FAR legs, timing and telegraphs | `references/animation-planning.md` |
| Play clips in a game (distance-driven gait, entry frames, dissolves, hit-stop, time-warp, pixel snapping) | `references/runtime-integration.md` |

The reference list should name character-animation.md (index), animation-planning.md, frames-and-clips.md and runtime-integration.md.

generate2dmap:

| Need | Route |
|---|---|
| Animated full-frame scene loop | The generate2dsprite skill's `scripts/assemble_frames.py`. Use `--static-regions` with `--static-region-search N` to prove landmarks and hotspots stay still, and `--loop-overlap K --ambient` to remove a pop at the wrap. |

codeart2d:

| Need | Route |
|---|---|
| Package rendered frames as clips | Call the generate2dsprite skill's `scripts/build_animation_clips.py` by path with a v2 manifest: `"art_source": "code"`, `"pixel_art": true`, `"sampling": "nearest"`, timing in `ticks`. Fold empty FX tail frames first; the builder refuses fully transparent frames. |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `build_animation_clips.py` | Packages registered frames into clips: v1/v2 manifests, ticks on a 60 Hz grid, events, keys, transitions, hit-stop, verified lossless WebP previews, review sheets (filmstrips, onion skins, turn test, dissolves) and lints (enemy telegraph, uneven ticks, near-duplicate holds) | `tests/test_build_animation_clips.py`: cfed170 golden output, animation_clips_v2 validation, documented manifests |
| `assemble_frames.py` | Packs complete frames losslessly. It reads any PNG mode, keys chroma optionally, reads one crop-box schema, checks cross-cell spill and slices by ownership, and measures loop seams, landmark drift and the ambient crossfade | `tests/test_assemble_frames.py`: fox fixture spill (77 px tail kept whole), full_frames_v2 validation |

## 4. CHANGELOG entries

Added:

- Clips v2 (B02-T4):
  - Ticks at `tick_hz` with drift-free integer ms.
  - `loop_policy`; pingpong mirrors without repeating the end frames.
  - `events` resolved to `events_ms` with ticks.
  - `keys`, `entry_frame`, `stride_px_per_frame`, `cadence_ms`, `speed_ref`, transition hints, `hitstop_ticks`, `role`.
  - Top-level `sampling`, `pixel_art`, `palette_ref` (hashed), `art_source`, `placeholder`, `body_height_px`, `shadow`.
  - A 60 Hz tick-grid drift report per clip; holds and near-duplicates; loop seams with `wrap_ratio`; a `qa` envelope.
  - Lints: enemy telegraph under 28 ticks, uneven or vanishing ticks, near-duplicate holds, hit-stop without a hit.
- Reviews (B02-T5):
  - `--preview-scale N`, `--preview-background`.
  - Filmstrips on light, dark and checker backgrounds with a ground line and tick labels.
  - Onion skins, a mirrored turn test with anchor marks and `turn_slide_px`, and A-to-B dissolve previews (premultiplied or dither, drawn once).
  - `--strict`.
- assemble_frames (B02-T2, B02-T3):
  - `--key chroma` through forge_matte.key_still (DOC-03).
  - One crop-box schema `{"items": [{"id", "box"}]}` plus the legacy list and prop-pack forms (DOC-18).
  - A cross-cell spill check and `--slice ownership` (report v2 P1-3).
  - `wrap_ratio`, `--static-region-search N` (NCC drift), and `--loop-overlap K --ambient` (smoothstep crossfade, ambient loops only).
  - A `qa` envelope.
- References frames-and-clips.md, animation-planning.md (NEAR/FAR phases, ticks, telegraphs) and runtime-integration.md (B02-T6; DOC-19, report v2 P1-7).

Changed:

- Both tools publish through forge_core.staged_output (F-03).
- animation-clips.json records manifest-relative source paths (absolute before). A v1 manifest keeps its v1 schema and every v1 value; the new diagnostics are extra keys.
- assemble_frames animation.json is `generate2dsprite.full_frames.v2` with manifest-relative source paths.
- Both tools accept a UTF-8 BOM in JSON inputs.
- character-animation.md is now an index of the three references.

BREAKING (plan Appendix H):

- `assemble_frames --sheet` fails on cross-cell spill. Legacy switch: `--allow-spill`. Better: `--slice ownership`.
- Both CLIs print one JSON line (`output`, `metadata`) instead of the bare output path, and exit 1 instead of 2 on errors. This is the Appendix D convention; there is no legacy switch.
- assemble_frames animation.json drops the absolute `output_directory`, and `sources[].path` is relative. There is no legacy switch; the folder holding animation.json is the output.

Fixed:

- S23: palette (with tRNS), 1, 2 and 4-bit, grey and 16-bit PNG input in assemble_frames.
- S13: the review font falls back on Pillow 10.0.
- F-03: WSL publishing.
- Report v2 P1-3: the fox frame-3 tail was cut silently; now it is reported, or kept whole.
- DOC-03 and DOC-18.
- WebP previews: libwebp 1.6 drops the VP8X alpha flag when it crops a fully opaque first frame, so cfed170 refused valid frames. A failed verification now re-encodes once with every frame a keyframe.

## 5. Schema change requests

All requests are additive. Every A0 contract fixture still validates (`V1CompatibilityTests.test_contract_fixtures_still_validate_with_the_proposal`), and the tests validate the B02 outputs against these fragments in memory (`proposed_validator` and `patched_validator`). Producer: B02. Consumers: B09 export_engine, B18 and B19 (`--build-clips`), and the e2e smoke.

**A. `shared/schemas/common.schema.json`, add to `/$defs`** (DOC-18: one crop-box form for every tool; B10 may adopt it for `--boxes-file`):

```json
{
  "cropBoxes": {
    "description": "Crop boxes of a sheet, one form for every tool (DOC-18): items with an optional id and a half-open [x0, y0, x1, y1) box in sheet pixels. Tools keep reading the legacy forms: a bare list of boxes (assemble_frames --crop-boxes) and {props: [{label, source_box}]} (extract_prop_pack --boxes-file).",
    "type": "object",
    "required": ["items"],
    "properties": {
      "schema": {"const": "forge-crop-boxes/v1"},
      "items": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "object",
          "required": ["box"],
          "properties": {
            "id": {"type": "string", "minLength": 1, "pattern": "\\S"},
            "box": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 4, "maxItems": 4}
          }
        }
      }
    }
  }
}
```

**B. `shared/schemas/sprite.schema.json`, add to `/$defs`** (assemble_frames animation.json had no contract; v1 documents stay valid):

```json
{
  "full_frames_v2": {
    "description": "animation.json written by assemble_frames.py: complete frames packed without resizing or alignment, an atlas, a decoded and verified lossless WebP, and loop diagnostics. generate2dsprite.full_frames.v1 documents stay valid.",
    "type": "object",
    "required": [
      "schema",
      "frame_size",
      "source_frame_count",
      "sources",
      "frames",
      "sequence",
      "duration_ms",
      "total_duration_ms",
      "atlas",
      "webp",
      "transitions"
    ],
    "properties": {
      "schema": {"enum": ["generate2dsprite.full_frames.v1", "generate2dsprite.full_frames.v2"]},
      "frame_size": {"$ref": "common.schema.json#/$defs/size2"},
      "source_frame_count": {"type": "integer", "minimum": 1},
      "sources": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "object",
          "required": ["file_sha256", "size"],
          "properties": {
            "path": {"type": "string", "minLength": 1},
            "file_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
            "bytes": {"type": "integer", "minimum": 0},
            "size": {"$ref": "common.schema.json#/$defs/size2"},
            "mode": {"enum": ["RGB", "RGBA"]},
            "source_mode": {"type": "string"},
            "bit_depth": {"enum": [1, 2, 4, 8, 16]},
            "conversion": {"type": "string"},
            "key": {
              "type": "object",
              "required": ["quality", "key_rgb"],
              "properties": {
                "quality": {"type": "string"},
                "key_rgb": {"$ref": "common.schema.json#/$defs/rgb"},
                "qa": {"type": "object"}
              }
            }
          }
        }
      },
      "frames": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "object",
          "required": ["index", "file", "size", "mode", "rgba_pixel_sha256"],
          "properties": {
            "index": {"type": "integer", "minimum": 0},
            "file": {"$ref": "common.schema.json#/$defs/relPath"},
            "source_index": {"type": "integer", "minimum": 0},
            "crop_box": {"$ref": "common.schema.json#/$defs/box"},
            "crop_id": {"type": "string", "minLength": 1},
            "sheet_origin": {"$ref": "common.schema.json#/$defs/point2"},
            "derived": {
              "type": "object",
              "required": ["blend_of", "weight"],
              "properties": {
                "blend_of": {
                  "type": "array",
                  "items": {"type": "integer", "minimum": 0},
                  "minItems": 2,
                  "maxItems": 2
                },
                "weight": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
                "curve": {"type": "string"}
              }
            },
            "size": {"$ref": "common.schema.json#/$defs/size2"},
            "mode": {"enum": ["RGB", "RGBA"]},
            "file_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
            "rgba_pixel_sha256": {"$ref": "common.schema.json#/$defs/sha256"}
          }
        }
      },
      "sequence": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 0}},
      "duration_ms": {"type": "integer", "minimum": 1},
      "total_duration_ms": {"type": "integer", "minimum": 1},
      "loop": {"const": 0},
      "atlas": {
        "type": "object",
        "required": ["file", "size", "rows", "cols"],
        "properties": {
          "file": {"$ref": "common.schema.json#/$defs/relPath"},
          "size": {"$ref": "common.schema.json#/$defs/size2"},
          "rows": {"type": "integer", "minimum": 1},
          "cols": {"type": "integer", "minimum": 1},
          "file_sha256": {"$ref": "common.schema.json#/$defs/sha256"}
        }
      },
      "webp": {
        "type": "object",
        "required": ["file", "total_duration_ms", "timeline_verified"],
        "properties": {
          "file": {"$ref": "common.schema.json#/$defs/relPath"},
          "total_duration_ms": {"type": "integer", "minimum": 1},
          "timeline_verified": {"const": true},
          "file_sha256": {"$ref": "common.schema.json#/$defs/sha256"}
        }
      },
      "transitions": {
        "type": "array",
        "items": {"type": "object", "required": ["from_frame", "to_frame", "wrap"]}
      },
      "loop_seam": {
        "anyOf": [
          {"type": "null"},
          {
            "allOf": [
              {"$ref": "common.schema.json#/$defs/seamReport"},
              {
                "type": "object",
                "required": ["adjacent_mean", "wrap_ratio"],
                "properties": {
                  "adjacent_mean": {"type": "number", "minimum": 0},
                  "wrap_ratio": {"type": "number", "minimum": 0}
                }
              }
            ]
          }
        ]
      },
      "loop_overlap": {
        "anyOf": [
          {"type": "null"},
          {
            "type": "object",
            "required": ["frames", "played_before", "played_after"],
            "properties": {
              "frames": {"type": "integer", "minimum": 2},
              "played_before": {"type": "array", "items": {"type": "integer", "minimum": 0}},
              "played_after": {"type": "array", "items": {"type": "integer", "minimum": 0}}
            }
          }
        ]
      },
      "static_regions": {
        "type": "object",
        "additionalProperties": {
          "type": "object",
          "required": ["box", "reference_frame", "against_reference"],
          "properties": {"box": {"$ref": "common.schema.json#/$defs/box"}}
        }
      },
      "key": {"type": "object", "required": ["mode"], "properties": {"mode": {"enum": ["none", "chroma"]}}},
      "slicing": {
        "type": "object",
        "required": ["mode"],
        "properties": {
          "mode": {"enum": ["inputs", "grid", "ownership"]},
          "padding": {"$ref": "common.schema.json#/$defs/padding4"},
          "canvas": {"$ref": "common.schema.json#/$defs/size2"}
        }
      },
      "spill_check": {
        "anyOf": [
          {"type": "null"},
          {
            "type": "object",
            "required": ["status"],
            "properties": {
              "status": {"enum": ["clean", "resolved", "allowed", "skipped"]},
              "crossing": {
                "type": "array",
                "items": {"type": "object", "required": ["owner_id", "pixels_over", "bbox"]}
              }
            }
          }
        ]
      },
      "provenance": {"type": "object"},
      "validation": {"type": "object"},
      "processing": {"type": "object"},
      "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
    },
    "if": {"properties": {"schema": {"const": "generate2dsprite.full_frames.v2"}}, "required": ["schema"]},
    "then": {"required": ["loop_seam", "key", "slicing", "spill_check", "qa"]}
  }
}
```

**C. `shared/schemas/sprite.schema.json`, add to `/$defs/builtClip/properties`** (documents the optional per-clip fields of the v2 builder; objects are already open):

```json
{
  "authored_frames": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 0}},
  "authored_duration_ms": {"$ref": "common.schema.json#/$defs/durationsMs"},
  "timing_source": {"enum": ["duration_ms", "ticks"]},
  "ticks": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1}},
  "tick_hz": {"type": "integer", "minimum": 1},
  "fps_rational": {"type": "string", "pattern": "^[1-9][0-9]*/[1-9][0-9]*$"},
  "stride_world_units": {"type": "number", "exclusiveMinimum": 0},
  "stride_source": {"const": "user_declared"},
  "nominal_travel_speed_world_units_per_second": {"type": "number", "exclusiveMinimum": 0},
  "stride_px_per_frame": {"type": "number", "exclusiveMinimum": 0},
  "nominal_speed_px_per_second": {"type": "number", "exclusiveMinimum": 0},
  "cadence_ms": {"type": "number", "exclusiveMinimum": 0},
  "speed_ref": {"type": "number", "exclusiveMinimum": 0},
  "speed_ref_playback_rate": {"type": "number", "exclusiveMinimum": 0},
  "keys": {"$ref": "#/$defs/clipSpec/properties/keys"},
  "keys_ms": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
  "entry_frame": {"type": "integer", "minimum": 0},
  "entry_ms": {"type": "integer", "minimum": 0},
  "hitstop_ticks": {"type": "integer", "minimum": 0},
  "hitstop_ms": {"type": "integer", "minimum": 0},
  "role": {"enum": ["player", "enemy", "npc", "fx", "prop"]},
  "transition_hints": {
    "type": "array",
    "items": {
      "type": "object",
      "required": ["to", "entry_frame", "dissolve_ms", "mode"],
      "properties": {
        "to": {"type": "string", "minLength": 1},
        "entry_frame": {"type": "integer", "minimum": 0},
        "entry_ms": {"type": "integer", "minimum": 0},
        "dissolve_ms": {"type": "integer", "minimum": 0},
        "dissolve_ticks": {"type": "integer", "minimum": 0},
        "mode": {"enum": ["dither", "premultiplied"]}
      }
    }
  },
  "holds": {
    "type": "array",
    "items": {
      "type": "object",
      "required": ["positions", "frames", "kind", "duration_ms"],
      "properties": {
        "positions": {"type": "array", "minItems": 2, "items": {"type": "integer", "minimum": 0}},
        "frames": {"type": "array", "minItems": 2, "items": {"type": "integer", "minimum": 0}},
        "kind": {"enum": ["repeat", "near_duplicate"]},
        "max_distance": {"type": "number", "minimum": 0},
        "duration_ms": {"type": "integer", "minimum": 1}
      }
    }
  },
  "seam": {
    "allOf": [
      {"$ref": "common.schema.json#/$defs/seamReport"},
      {
        "type": "object",
        "required": ["adjacent_mean", "wrap_ratio"],
        "properties": {
          "adjacent_mean": {"type": "number", "minimum": 0},
          "wrap_ratio": {"type": "number", "minimum": 0}
        }
      }
    ]
  },
  "telegraph": {
    "type": "array",
    "items": {
      "type": "object",
      "required": ["tell_ms", "hit_ms", "ticks_60hz", "source"],
      "properties": {
        "tell_ms": {"type": "integer", "minimum": 0},
        "hit_ms": {"type": "integer", "minimum": 0},
        "ticks_60hz": {"type": "number", "minimum": 0},
        "source": {"type": "string"}
      }
    }
  }
}
```

**D. Add to `/$defs/builtClip/properties/events_ms/items/properties`:**

```json
{"position": {"type": "integer", "minimum": 0}, "at_tick": {"type": "integer", "minimum": 0}}
```

**E. Add to `/$defs/builtClip/properties/tick_grid/properties`:**

```json
{
  "frame_ticks": {"type": "array", "items": {"type": "integer", "minimum": 0}},
  "zero_tick_positions": {"type": "array", "items": {"type": "integer", "minimum": 0}},
  "even": {"type": "boolean"},
  "cycle_ticks": {"type": "number", "minimum": 0},
  "cycle_aligned": {"type": "boolean"},
  "method": {"type": "string"}
}
```

**F. Add to `/$defs/animation_clips_v2/properties`:**

```json
{
  "sampling": {"$ref": "common.schema.json#/$defs/sampling"},
  "pixel_art": {"type": "boolean"},
  "palette_ref": {"$ref": "common.schema.json#/$defs/relPath"},
  "palette_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
  "body_height_px": {"type": "number", "exclusiveMinimum": 0},
  "shadow": {"$ref": "common.schema.json#/$defs/shadow"},
  "review": {
    "anyOf": [
      {"type": "null"},
      {
        "type": "object",
        "required": ["scale", "backgrounds", "turn_test"],
        "properties": {
          "scale": {"type": "integer", "minimum": 1},
          "backgrounds": {"type": "array", "minItems": 1, "items": {"type": "string"}},
          "turn_test": {"type": "object", "required": ["file", "cells"]}
        }
      }
    ]
  },
  "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
}
```

**G. Add to `/$defs/animation_clips_v2/properties/frames/items/properties`:**

```json
{
  "source": {
    "type": "object",
    "required": ["file_sha256"],
    "properties": {
      "path": {"type": "string", "minLength": 1},
      "file_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
      "bytes": {"type": "integer", "minimum": 0},
      "size": {"$ref": "common.schema.json#/$defs/size2"},
      "mode": {"const": "RGBA"}
    }
  },
  "transparent_pixel_count": {"type": "integer", "minimum": 0},
  "visible_pixel_count": {"type": "integer", "minimum": 0}
}
```

The schema does not cover these fields; record them for Z:

- **`transitions`.** In built clips, `transitions` keeps its v1 meaning: adjacent-frame metrics `{from_position, to_position, premultiplied_rgb_mae, alpha_mae, changed_visible_pixels}`, which v1 consumers read. The v2 input hints `{to, entry_frame, dissolve_ms, mode}` are written as `transition_hints` with `entry_ms` and `dissolve_ticks`. The builtClip `transitions` description should say so.
- **Pingpong clips.** `frames` and `duration_ms` hold the played order, so v1 players loop it correctly. The authored order is in `authored_frames` and `authored_duration_ms`.
- **Other built output.** `diagnostics` gains `lint` (`{code, severity, message, clip?}`), `near_duplicate_mae`, `near_duplicate_pairs` and `tick_hz`. `source_manifest` gains `copy`. `contact_sheet` gains `scale`. `preview` may hold `scale` and `all_keyframes`.
- **Other assemble output.** `webp.all_keyframes`. Frames may carry `crop_id`, `sheet_origin`, `owned_components`, `pixels_from_outside_cell` (ownership) and `derived` (overlap blends).

## 6. Shared-helper promotion requests

| Helper (file) | Proposed home | Why and tests |
|---|---|---|
| `manifest_path(path, base) -> str` (assemble_frames.py) | forge_core, beside `portable_path` | `portable_path` returns an absolute path for another drive, and every manifest writer must then fall back to the file name. Tests: v1 golden paths, `test_rectangular_odd_dimensions...` |
| `blend_rgba(first, second, weight) -> ndarray` (assemble_frames.py) | forge_core | A premultiplied mix with half-up rounding, used by the loop crossfade and the dissolve preview. B16 forward-overlap loops and B09's runtime dissolve need the same numbers. Tests: `test_loop_overlap_reduces_seam_ratio_below_1`, `test_transition_dissolve_preview` |
| `loop_seam(frames) -> dict` (assemble_frames.py) | forge_core.seam_report | Add `adjacent_mean` and `seam_over_mean` (the Dusk seam ratio, published here as `wrap_ratio`). With the median, a crossfaded loop always lands near 1; with the mean it drops from 11.9 to 0.71 in the test. |
| `ticks_to_ms(ticks, hz) -> list[int]` and `tick_grid_report(durations, hz) -> dict` (build_animation_clips.py) | forge_core, beside `frame_durations` | `frame_durations` generalised to unequal ticks, and the grid report. B07 retime and B09 frameAt need the same rounding. Tests: `test_ticks_become_drift_free_integer_ms`, `test_tick_grid_report` |
| `ncc_drift(reference, frame, box, radius) -> dict` (assemble_frames.py) | forge_core | Landmark and identity NCC: B06 register_clip qc searches plus or minus 15 px with the same metric. Test: `test_static_region_shift_detected` |
| `save_animation(..., all_keyframes)` plus the verify-and-retry loop (assemble_frames.py) | forge_core, if another Pillow animated-WebP writer appears | The libwebp 1.6 alpha-flag workaround. Tests: `WebpEncoderTests`, `PreviewEncoderTests` |
| `review_font(size)` (build_animation_clips.py) | forge_core | The S13 fallback for every tool that draws labels (B03 plan_guide, B07 review sheets, B18 pixel_qa). Test: `test_font_fallback_pillow_10_0` |
| `_Parser` (both scripts) | forge_core | An argparse subclass whose usage errors print `error: ...` and exit 1, as Appendix D requires of every CLI |

## 7. Cross-module links that Z must add

- Sprite SKILL.md:
  - The routing rows of section 2.
  - Links to the four references (character-animation.md, animation-planning.md, frames-and-clips.md, runtime-integration.md).
  - Registered frames go to build_animation_clips (v2). Full frames go to assemble_frames (`--slice ownership`; `--loop-overlap` for ambient loops only).
- processing.md (B01) should replace its "Registered animation clips" and "Whole-frame packaging" sections with a plain pointer to frames-and-clips.md (B01-T8 plans this).
- prompt-rules.md (B03) should use the NEAR/FAR wording and link animation-planning.md for phase tables and the 28-tick telegraph.
- B03 `scale_frames --emit-clips` should write `"schema": "generate2dsprite.animation_clips.v2"` with ticks, so the tick-grid report stays clean.
- B18 and B19 `--build-clips` should call `build_animation_clips.py` by path. They should parse its one-line JSON summary (`metadata`), not a bare path, and pass a v2 manifest with `art_source: code`. The builder still refuses indexed PNGs and fully transparent frames.
- B09 export_engine reads `animation-clips.json` as follows:
  - For pingpong clips, `frames` and `duration_ms` are already the played order.
  - Hints are in `transition_hints`; `transitions` holds frame-step metrics.
  - `events_ms` lists every played occurrence.
  - `entry_ms`, `keys_ms` and `hitstop_ms` are precomputed.
- B09 forge-runtime.mjs should implement what runtime-integration.md describes:
  - The ditherDissolve as one draw with a 4x4 Bayer threshold, matching `build_animation_clips.dissolve`.
  - `mapActionTime` as the piecewise-linear time-warp.
  - Hit-stop as a frozen simulation clock beside a running presentation clock.
- B07 animation-review.md should link runtime-integration.md for distance-driven gait. Its loop policy table should match animation-planning.md: pingpong for sway, cycle for gaits, oneshot for actions.
- B10 prop pack could read the `cropBoxes` form (DOC-18 covers both tools).
- generate2dmap background-scenes.md (B16) should mention `assemble_frames --static-region-search` for landmark stability and `--loop-overlap K --ambient` for plate loops.
- README requirements: assemble_frames needs Pillow with WebP; the review font fallback keeps Pillow 10.0 working.

## 8. Known limitations and what is not proven

**Thresholds** come from synthetic tests, one real sheet (the fox fixture) and one game's tuning:

- Spill: alpha above 16, components of at least 4 px.
- Ownership attach radius: 6 px.
- Near-duplicate holds: union-visible premultiplied mean difference of at most 1.5.
- NCC drift margin: 0.02.
- Telegraph: 28 ticks, from game-opus55 combat-feel-v0.md.

None is validated on a corpus.

**Visual output** has not been reviewed by a person; the agent inspected a few sheets. The review sheets are aids, and numeric QA approves no motion.

**Platforms.** Only Windows 11 was run, with Python 3.13.2, Pillow 12.3.0, libwebp 1.6.0, numpy 2.5.3 and scipy 1.18.1. Pillow 10.0 is simulated by monkeypatching `ImageFont.load_default`; 10.0 and 10.1 themselves were not run. The WebP alpha-flag workaround was observed with libwebp 1.6.0; other versions may never need the retry, and the tests accept either path.

**v1 compatibility** is proven against values recorded from the cfed170 builder on this machine:

- Frames and WebP previews are byte-identical.
- The contact sheet is pixel-identical; its PNG bytes differ because it now uses forge_core.save_png.
- Paths are now manifest-relative.

Deviations from the plan, with reasons:

1. **`wrap_ratio`** is the seam over the **mean** adjacent step (the Dusk metric the plan cites: 3.50 to 0.73), not forge_core's median ratio. With the median, a crossfaded loop always lands near 1.0, so "below 1" would not be provable. Both ratios are recorded.
2. **build_animation_clips still refuses indexed and 16-bit PNGs**, with a conversion hint. Appendix D says indexed PNG is never builder input, and frames are copied byte for byte. S23 is fixed in assemble_frames, which converts such inputs to RGBA.
3. **`--key none` keeps RGB under alpha 0 exactly** in assemble_frames' frames and atlas. The fork's test requires it, and the tool's contract is lossless. Keyed frames and ownership-sliced frames have it zeroed (Appendix D).
4. **A v1 manifest keeps `"schema": "generate2dsprite.animation_clips.v1"`** in the output, with extra diagnostic keys, rather than becoming v2. That keeps v1 consumers that check the schema working. v2 fields in a v1 manifest are ignored with a lint warning.
5. **Ownership slicing also accepts sheets that do not divide by the grid**, using rounded cells; grid slicing still refuses them. The frames share one canvas, so registration stays exact.
6. **`--loop-overlap` needs `--ambient`.** This is how "never characters" is enforced; the tool cannot tell a character from water. K is at least 2 and below half the played positions.

**Hit-stop, time-warp and walk phase** are documented for runtimes and resolved into manifest fields. B09 implements them; this module does not test a runtime.
