# B03-sprite-authoring-qc: generation-side QC (sheet_qc spill/frames), registration by scale_frames (one scale, root locks, padding, never clamp), opt-in plan_guide, prompt rules and action recipes

Branch `asf/B03-sprite-authoring-qc` (from `wip/asf-upgrade-20261005` @ 3f9252d). New files: [sheet_qc.py](../skills/generate2dsprite/scripts/sheet_qc.py), [scale_frames.py](../skills/generate2dsprite/scripts/scale_frames.py), [plan_guide.py](../skills/generate2dsprite/scripts/plan_guide.py), [action-recipes.md](../skills/generate2dsprite/references/action-recipes.md), [tests/test_sheet_qc.py](../tests/test_sheet_qc.py), [tests/test_scale_frames.py](../tests/test_scale_frames.py), [tests/test_plan_guide.py](../tests/test_plan_guide.py). Rewritten (owned): [prompt-rules.md](../skills/generate2dsprite/references/prompt-rules.md). The three scripts import only this skill's vendored `forge_core` (and `forge_matte` for chroma inputs); `scale_frames.py` and `plan_guide.py` import `sheet_qc.py` as a sibling.

## 1. CLIs

Run from the user's project root; `<skill-dir>` is `${CLAUDE_SKILL_DIR}` in Claude Code.

    python "<skill-dir>/scripts/sheet_qc.py" spill --input raw/run-sheet.png --rows 2 --cols 4 --output-dir qc/run-spill
    python "<skill-dir>/scripts/sheet_qc.py" frames --sheet raw/run-sheet.png --rows 2 --cols 4 --cycle run --game-pixel 8 --near-colors "#d15823,#4b2418" --far-colors "#a03c19,#39180f" --output-dir qc/run-frames
    python "<skill-dir>/scripts/sheet_qc.py" frames --frames out/walk/frame-0.png out/walk/frame-1.png out/walk/frame-2.png out/walk/frame-3.png --cycle walk --master art/hero-master.png --output-dir qc/walk-frames
    python "<skill-dir>/scripts/scale_frames.py" --sheet raw/run-sheet.png --rows 2 --cols 4 --scale-from 1/8 --resampler nearest --root-lock torso-x --row-baseline --emit-clips --clip-name run --duration-ms 80 --output-dir out/run-game
    python "<skill-dir>/scripts/scale_frames.py" --frames out/attack/f0.png out/attack/f1.png out/attack/f2.png --profile out/run-game/scale-frames.json --action-padding 0,4,6,0 --no-loop --emit-clips --clip-name attack --output-dir out/attack-game
    python "<skill-dir>/scripts/plan_guide.py" --frames 8 --cycle run --facing right --output-dir guides/hero-run

- `sheet_qc.py spill` writes `sheet-qc.json` (a `generate2dsprite.sheet_qc.v1` QA envelope, `check: spill`) and `spill-overlay.png`. Checks: `cross_cell_components` and `cross_cell_visible` (fail), `sheet_edge` (fail), `empty_cells` (fail; `--expect N`), `boundary_band`, `safe_frame`, `faint_specks` (warn). Solid parts are 8-connected `alpha > 127` (`--alpha-threshold`), the visible edge `alpha > 16` (`--edge-threshold`); opaque chroma sheets are keyed with `forge_matte.key_still` (`--key auto|none|magenta|green|blue|#rrggbb`).
- `sheet_qc.py frames` writes `sheet-qc.json` (`check: frames`) and `frames-review.png`. `--sheet` slices by component ownership (nominal cell origin plus one shared padding; nothing is cut; `--count N` uses the first N cells of a sheet with spare cells); `--frames` takes frames on one canvas (`--frames-per-row` for row baselines). Checks: `identity_head_ratio` (fail outside +-5%), `identity_residual`, `near_duplicates`, `phase_coverage` (warn), `leg_alternation` (fail on a duplicated half-cycle; `--cycle run|walk`), `torso_drift`, `row_baseline`, `seam` (warn). NEAR/FAR leads come from `--near-colors/--far-colors`, or without them from the "FAR one shade darker" lightness convention (auto mode leaves ambiguous frames `unclear`). `--torso-colors` gives the costume colours for the torso x; without them the trunk geometry is used. `--game-pixel N` reports drift and baselines in game pixels.
- `scale_frames.py` writes `frames/<name>.png`, `scale-frames.json` (`generate2dsprite.scale_frames.v1`: scale, resampler, canvas, `anchor_px`, padding and every per-frame shift) and `scale-review.png`; with `--emit-clips`, `clips.json` (`generate2dsprite.animation_clips.v1`, which `build_animation_clips.py` builds today and B02's v2 builder still reads). Scale: `--scale-from neutral|union --target-body-px H` or a number (`1/8`); `--resampler nearest` needs an integer factor and suggests the nearest ones otherwise. Root: `--anchor stance|feet|bbox` on the `--neutral` frame. Locks: `--root-lock torso-x|stance|none`, `--row-baseline`, `--lock feet|x|hip` (one horizontal and one vertical lock at most), `--shift-quantum N`. Canvas: automatic (`--margin`), or `--canvas W,H --anchor-px X,Y` / `--profile earlier/scale-frames.json`, grown by `--action-padding L,T,R,B`; a frame that would leave it is an error naming the padding it needs (frames are never clamped).
- `plan_guide.py` writes `guide.png`, `guide.svg`, `guide-annotated.png`, `prompt.txt` and `sheet-plan.json` (new document `generate2dsprite.sheet_plan.v1`, section 5). It predicts the host size for an aspect (budget 1,572,864 px), ranks grids by envelope height inside the `--safe 0.15` frame, draws gutters, safe boxes, ground lines, root ticks and (with `--cycle run|walk`) a NEAR/FAR skeleton, and refuses to publish when its self-check fails (`--min-envelope-px`, skeleton outside its safe box, ground contact, leading-leg alternation).
- All three: argparse; a new `--output-dir` is staged, checked and published atomically (`forge_core.staged_output`); an existing output is refused; `--strict` (sheet_qc) and every scale_frames/plan_guide error publish nothing. Every failure, including argparse usage errors, is one `error: ...` line on stderr and exit 1 (no traceback; `FORGE_DEBUG=1` re-raises for developers). `--help` is ASCII and exits 0 under cp1252 and cp950 (tested with `assert_cli_help`). Success prints one ASCII JSON line: `status`, `output`, `metadata` (the JSON path), plus `check`/`failed`/`warnings` (sheet_qc), `scale`/`canvas`/`anchor_px`/`shifts`/`clips` (scale_frames) or `aspect`/`predicted_size`/`grid`/`guide` (plan_guide). sheet_qc exits 0 when it publishes a failing report; the summary's `status` and `failed` say so.

## 2. SKILL.md routing rows

generate2dsprite:

| Need | Route |
|---|---|
| Check a raw generated sheet before slicing | `scripts/sheet_qc.py spill`; crossing parts are fixed by regenerating with more margin or by ownership slicing, never by a largest-component filter |
| Check an action's identity, NEAR/FAR alternation, drift, baselines and seams | `scripts/sheet_qc.py frames --cycle run` (or walk); a duplicated half-cycle means regenerating only the second half |
| Scale an action to game size on one canvas and root, without clamping | `scripts/scale_frames.py --root-lock torso-x --row-baseline --emit-clips`; then `scripts/build_animation_clips.py --manifest <out>/clips.json` |
| Give later actions the same scale, canvas and root | `scripts/scale_frames.py --profile <first action>/scale-frames.json --action-padding L,T,R,B` |
| Plan a sheet for a host image tool (aspect, grid, guide, prompt block) | `scripts/plan_guide.py` (opt-in, not A/B tested); attach `guide.png`, paste `prompt.txt` |
| Write or fix a sprite prompt | `references/prompt-rules.md`; phase lists, grids and presets in `references/action-recipes.md` |
| Single-shot prototype (one generation call, no master) | generate one sheet, then `sheet_qc.py spill` and `sheet_qc.py frames` (identity check against the median frame), `scale_frames.py`; label the result a prototype |

Pipeline sentence for the SKILL.md body: "After generation run `sheet_qc.py spill`, then `sheet_qc.py frames`, then `scale_frames.py`, then `build_animation_clips.py`. Code-art frames skip these and go straight to `build_animation_clips.py`."

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `sheet_qc.py spill` | Finds parts that cross a cell line in a raw sheet before slicing (owner cell, pixels over the line, overhang), art cut by the sheet edge, empty cells, the boundary band, the safe frame and alpha haze | `tests/test_sheet_qc.py`: fox sheet frame 3's tail 58 px over the line, 6 px overhang, in under 1.5 s |
| `sheet_qc.py frames` | Checks one action's frames: head ratio, near-duplicates, phase coverage, NEAR/FAR leading-leg alternation, torso drift and row baselines in game pixels, seam ranking | `tests/test_sheet_qc.py`: fox 8/8 NEAR leg leading, head ratio 0.94, drift 6.6 game px; same-leg walk fails, alternating walk passes |
| `scale_frames.py` | One scale, one root and whole-pixel registration locks for an action; the canvas grows and nothing is clamped; writes the shifts and a clips manifest | `tests/test_scale_frames.py`: fox 3->4 transition 26.54 -> 8.10 with no pixel lost; stance turn slide 0; clamp case exits 1; clips.json builds |
| `plan_guide.py` | Predicts the host image size for an aspect, plans rows, columns and gutters with a 15% safe frame, draws a NEAR/FAR pose guide and an aspect-only prompt (opt-in) | `tests/test_plan_guide.py`: 1536x1024, 1672x941, 1254x1254; self-check passes the alternation test |

## 4. CHANGELOG entries

- Added: `sheet_qc.py spill` cross-cell spill check on raw sheets (owner cell, pixels over the line, overhang, boundary band, safe frame, faint specks), sheet_qc.v1 report and overlay (B03-T1; report v2 P1-3, 3.2.1-3.2.2).
- Added: `sheet_qc.py frames`: head-ratio identity check against the median frame or a master, best-shift near-duplicates, phase coverage, NEAR/FAR leading-leg alternation over (i, i+n/2), torso drift and row baselines in game pixels, seam ranking; ownership slicing of raw sheets (B03-T2; report v2 3.2.3-3.2.5).
- Added: `scale_frames.py`: one scale (`--scale-from neutral|union|N`, `--target-body-px`), stance/feet/bbox root, `--root-lock torso-x|stance`, `--row-baseline`, `--lock feet|x|hip`, `--action-padding`, `--profile`, integer-only nearest for pixel art and premultiplied box/lanczos for HD art, a processing record of every shift and `--emit-clips`. It replaces task-local scripts such as the trial's `prepare_game_scale.py` (B03-T3; report v2 P1-5, 3.2.4).
- Added: `plan_guide.py`, an opt-in sheet planner and pose guide with a self-check (B03-T4; report v2 3.3.1, 5.4; roadmap P2-2).
- Added: `references/action-recipes.md`: the fork's phase recipes (idle, run and walk with NEAR/FAR, attack, cast, hurt, combat, projectile, impact, four-direction sheets, 3x3 idle, 16-frame single actions, atlas guardrail, anchor sheets, guide fit, bundles) and YAML presets for a JRPG walker, a platform hero, an isometric tactics unit and a tower-defense tower (B03-T5; roadmap P2-8).
- Changed: `references/prompt-rules.md`: numeric canvas, palette list with a colour cap and the palette.v1 file, logical pixels, outline, anti-aliasing and light rules, master and turnaround first, character lock, single-shot identity check, NEAR/FAR limbs, aspect instead of pixels, body only, 2:1 isometric, and the post-generation check order (B03-T5; DOC-11, DOC-12, DOC-16, report v2 P1-7).
- BREAKING: none. The tools are new; the prompt docs only change guidance.
- Fixed: report v2 P1-3 (a tail crossing the cell line was invisible until frames were cut), report v2 3.2.3 and jev walk_phase_check (a same-leg run or walk passed strict QC), report v2 P1-5 (no CLI for one-scale registration: agents wrote `prepare_game_scale.py`), DOC-16 in the sprite prompt docs (no franchise names, no era style by default), DOC-11 and DOC-12 (palette and logical-pixel contract in prompts).

## 5. Schema change requests

All three requests are additive. `tests/test_plan_guide.py::test_requested_schema_additions_accept_every_b03_producer` applies them in memory (`requested_validator`, a no-op once merged) and validates the output of all four producers; the constants there (`SHEET_PLAN_V1`, `SHEET_QC_V1_PROPERTIES`, `SCALE_FRAMES_V1_PROPERTIES`, `SCALE_FRAMES_V1_FRAME_PROPERTIES`) are the source of the JSON below. Producer: B03. Consumers: agents and Z docs; B02 and B09 read the clips manifest, not these.

**5.1 New `$defs.sheet_plan_v1` in `shared/schemas/sprite.schema.json`** (document id `generate2dsprite.sheet_plan.v1`, written by `plan_guide.py` as `sheet-plan.json`). The frozen schema has no definition for it.

```json
{
  "description": "plan_guide.py output (opt-in generation aid, not A/B tested with an image model): the host size predicted for an aspect-only request, the chosen grid and the candidates, the guide's cells (safe box, ground line, root) and gait phases, and the guide's self-check as a QA envelope.",
  "type": "object",
  "required": ["schema", "predicted_size", "aspect", "layout", "qa"],
  "properties": {
    "schema": {"const": "generate2dsprite.sheet_plan.v1"},
    "request": {"type": "object"},
    "predicted_size": {"$ref": "common.schema.json#/$defs/size2"},
    "aspect": {"type": "string", "pattern": "^[1-9][0-9]*:[1-9][0-9]*$"},
    "layout": {
      "type": "object",
      "required": ["rows", "cols", "cell", "safe_box", "envelope_px"],
      "properties": {
        "rows": {"type": "integer", "minimum": 1},
        "cols": {"type": "integer", "minimum": 1},
        "empty_cells": {"type": "integer", "minimum": 0},
        "cell": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 2, "maxItems": 2},
        "safe_box": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 2, "maxItems": 2},
        "envelope_px": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 2, "maxItems": 2},
        "divides_exactly": {"type": "boolean"}
      }
    },
    "candidates": {"type": "array", "items": {"type": "object", "required": ["aspect", "rows", "cols"]}},
    "cells": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["cell", "box", "safe_box", "ground_y", "root_x"],
        "properties": {
          "cell": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2},
          "box": {"$ref": "common.schema.json#/$defs/box"},
          "safe_box": {"$ref": "common.schema.json#/$defs/box"},
          "gutter_px": {"type": "integer", "minimum": 1},
          "ground_y": {"type": "number"},
          "root_x": {"type": "number"},
          "used": {"type": "boolean"},
          "phase": {"type": "string"},
          "pose_bounds": {"$ref": "common.schema.json#/$defs/box"},
          "inside_safe_box": {"type": "boolean"},
          "toe_ahead": {"enum": ["near", "far"]}
        }
      }
    },
    "phases": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["index", "name", "half", "near", "far", "contact_leg", "ground"],
        "properties": {
          "index": {"type": "integer", "minimum": 0},
          "name": {"type": "string", "minLength": 1},
          "half": {"enum": [0, 1]},
          "near": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
          "far": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
          "lift": {"type": "number"},
          "contact_leg": {"enum": ["near", "far"]},
          "ground": {
            "type": "object",
            "required": ["near", "far"],
            "properties": {"near": {"type": "boolean"}, "far": {"type": "boolean"}}
          }
        }
      }
    },
    "prompt": {"$ref": "common.schema.json#/$defs/relPath"},
    "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
    "tool": {"$ref": "common.schema.json#/$defs/toolInfo"}
  }
}
```

**5.2 Document the optional fields of `sheet_qc_v1`**: merge into `$defs.sheet_qc_v1.properties` (`cells` replaces the bare `{"type": "array", "items": {"type": "object"}}`; every item stays open).

```json
{
  "image": {
    "type": "object",
    "description": "spill: input facts (sha256, size, source mode, bit depth, conversion) and the key used for an opaque chroma sheet"
  },
  "input": {"type": "object", "description": "frames: input facts and the ownership slicing record"},
  "params": {"type": "object"},
  "crossing_components": {
    "type": "array",
    "items": {
      "type": "object",
      "required": ["label", "area", "bbox", "owner_cell", "pixels_over_line", "overhang_px"],
      "properties": {
        "bbox": {"$ref": "common.schema.json#/$defs/box"},
        "owner_cell": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2},
        "pixels_over_line": {"type": "integer", "minimum": 0},
        "overhang_px": {"type": "integer", "minimum": 0},
        "pixels_per_cell": {"type": "object", "additionalProperties": {"type": "integer"}}
      }
    }
  },
  "visible_crossing_components": {"type": "array", "items": {"type": "object"}},
  "boundary_band": {
    "type": "array",
    "items": {
      "type": "object",
      "required": ["axis", "at", "solid_px"],
      "properties": {
        "axis": {"enum": ["x", "y"]},
        "at": {"type": "integer"},
        "solid_px": {"type": "integer", "minimum": 0}
      }
    }
  },
  "specks": {
    "type": "object",
    "properties": {
      "floor_px": {"type": "integer", "minimum": 0},
      "detached_px": {"type": "integer", "minimum": 0},
      "detached_components": {"type": "integer", "minimum": 0}
    }
  },
  "identity": {"type": "object"},
  "near_duplicates": {"type": "object"},
  "half_cycle": {"type": ["object", "null"]},
  "alternation": {
    "type": "object",
    "properties": {
      "verdict": {"type": "string"},
      "near_leading": {"type": "integer", "minimum": 0},
      "far_leading": {"type": "integer", "minimum": 0},
      "unclear": {"type": "integer", "minimum": 0}
    }
  },
  "torso": {"type": "object"},
  "row_baselines": {"type": "object", "additionalProperties": {"type": "integer"}},
  "seam_ranking": {
    "type": "array",
    "items": {
      "type": "object",
      "required": ["from", "to", "mae", "over_median"],
      "properties": {
        "from": {"type": "integer", "minimum": 0},
        "to": {"type": "integer", "minimum": 0},
        "mae": {"type": "number", "minimum": 0},
        "over_median": {"type": "number", "minimum": 0}
      }
    }
  },
  "seam": {"anyOf": [{"$ref": "common.schema.json#/$defs/seamReport"}, {"type": "null"}]},
  "cells": {
    "type": "array",
    "items": {
      "type": "object",
      "properties": {
        "cell": {
          "anyOf": [
            {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2},
            {"type": "null"}
          ]
        },
        "index": {"type": "integer", "minimum": 0},
        "frame": {"type": "integer", "minimum": 0},
        "box": {"$ref": "common.schema.json#/$defs/box"},
        "pixels_over_line": {"type": "integer", "minimum": 0},
        "overhang_px": {"type": "integer", "minimum": 0},
        "lead": {"type": "object", "properties": {"lead": {"enum": ["near", "far", "unclear"]}}}
      }
    }
  }
}
```

Note for A0's contract text: `cells[].cell` is `[row, col]` (as in the A0 fixture); `frames` reports also give `frame` (index) and, for `--frames` input, `cell: null` and the file name in `source`.

**5.3 Document the optional fields of `scale_frames_v1`**: merge into `$defs.scale_frames_v1.properties`, and the second block into `$defs.scale_frames_v1.properties.frames.items.properties`.

```json
{
  "scale_from": {"type": "string", "description": "neutral, union, profile, or the fixed scale as a fraction"},
  "target_body_px": {"type": ["number", "null"]},
  "base_canvas": {"anyOf": [{"$ref": "common.schema.json#/$defs/size2"}, {"type": "null"}]},
  "base_anchor_px": {"anyOf": [{"$ref": "common.schema.json#/$defs/point2"}, {"type": "null"}]},
  "needed_padding": {"anyOf": [{"$ref": "common.schema.json#/$defs/padding4"}, {"type": "null"}]},
  "margin": {"type": ["integer", "null"], "minimum": 0},
  "anchor_mode": {"enum": ["stance", "feet", "bbox"]},
  "source_anchor": {"$ref": "common.schema.json#/$defs/point2"},
  "reference_frame": {"type": "integer", "minimum": 0},
  "lock": {"type": "array", "items": {"enum": ["feet", "x", "hip"]}},
  "row_baseline": {"type": "boolean"},
  "shift_quantum": {"type": "integer", "minimum": 1},
  "alpha_threshold": {"type": "integer", "minimum": 0, "maximum": 254},
  "frames_per_row": {"type": "integer", "minimum": 1},
  "torso": {"type": "object"},
  "transitions": {"type": "object", "required": ["pairs", "before", "after"]},
  "seam": {
    "anyOf": [
      {"type": "null"},
      {
        "type": "object",
        "required": ["before", "after"],
        "properties": {
          "before": {"$ref": "common.schema.json#/$defs/seamReport"},
          "after": {"$ref": "common.schema.json#/$defs/seamReport"}
        }
      }
    ]
  },
  "input": {"type": "object"},
  "profile": {"anyOf": [{"$ref": "common.schema.json#/$defs/fileRef"}, {"type": "null"}]},
  "clips": {"$ref": "common.schema.json#/$defs/relPath"},
  "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
  "tool": {"$ref": "common.schema.json#/$defs/toolInfo"}
}
```

```json
{
  "cell": {
    "anyOf": [
      {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2},
      {"type": "null"}
    ]
  },
  "output_bbox": {"$ref": "common.schema.json#/$defs/box"},
  "turn_slide_px": {"type": "number", "minimum": 0},
  "turn_slide_output_px": {"type": ["number", "null"], "minimum": 0},
  "measure": {"type": "object"}
}
```

Semantics to state in the contract text: `scale_frames_v1.frames[].source` is the sheet itself for `--sheet` input (same fileRef for every frame, with `cell` naming the cell); `shift` is in output pixels relative to the neutral frame's placement; `turn_slide_px` is 2 x |stance x - anchor x| before pixel sampling and `turn_slide_output_px` the same measured on the output pixels; `transitions` are `forge_core.transition_mae` before (no locks) and after on one shared crop.

## 6. Shared-helper promotion requests

- `sheet_qc.ownership_slice(rgba, boxes, *, threshold=127, min_area=64, attach_radius=6) -> (frames, info)` and its helper `_attach_soft(owner, soft, radius)` (skills/generate2dsprite/scripts/sheet_qc.py, "ownership slicing" section): slicing that keeps every part on one registered canvas (byte-identical to the report v2 prototype on the fox; vectorised, about 0.25 s for a 1536x1024 sheet). B02 adds `assemble_frames --slice ownership` independently; one shared implementation in `forge_core` (extending the "Anchors and grids" API) would stop the two from drifting. Tests: `test_ownership_slice_keeps_every_part_on_one_registered_canvas`, `test_ownership_slice_reproduces_report_v2_fox_frames`.
- `sheet_qc.file_ref(path, base) -> dict` (sheet_qc.py, "small helpers"): a common `fileRef` `{path, sha256, bytes}` relative to the manifest directory that falls back to the file name when `forge_core.portable_path` returns an absolute path (another drive), as A1's section 5 requires. Every Wave B manifest writer needs it; proposed as `forge_core.file_ref(path, base)`. Tested through every report's `inputs`/`outputs`.
- `sheet_qc.fail(error) -> int` and `sheet_qc.ArgumentParser` (sheet_qc.py, "small helpers"): the CLI error convention (one `error: ...` line, exit 1, argparse usage errors included; `FORGE_DEBUG=1` re-raises). Proposed as `forge_core.cli_error(error)` and `forge_core.ArgumentParser`. Tested by the clean-error tests of all three CLIs.
- `sheet_qc._dilate(mask, radius)` duplicates the private `forge_core._dilate_square`; a public `forge_core.dilate_square(mask, radius)` would let it go.
- `sheet_qc.oklab_lightness(rgb)` should become `forge_palette.to_oklab(rgb)[..., 0]` once B04's `forge_palette` is vendored into generate2dsprite (B04 owns OKLab).

## 7. Cross-module links that Z must add

- generate2dsprite SKILL.md: the routing rows of section 2; link references/action-recipes.md and references/prompt-rules.md; the single-shot prototype route; the pipeline sentence (sheet_qc spill, sheet_qc frames, scale_frames, build_animation_clips).
- processing.md (B01): before `process`, run `sheet_qc.py spill` on raw sheets; for registered animation use `scale_frames.py` (one scale, whole-pixel locks) instead of per-frame fitting; mention `--alpha-hygiene both` as the fix for the `faint_specks` warning.
- frames-and-clips.md and animation-planning.md (B02, files not in this worktree): `scale_frames.py --emit-clips` writes the clips manifest; the NEAR/FAR phase tables match action-recipes.md; `assemble_frames --slice ownership` and `sheet_qc.ownership_slice` should share one implementation (section 6).
- character-animation.md: point its phase table to the NEAR/FAR wording in prompt-rules.md (its owner is B02).
- modes.md (B01): the four-direction row order (down, left, right, up) matches action-recipes.md's player sheet.
- palette_tool.py (B04): prompt-rules.md tells agents to save the palette as `generate2dsprite.palette.v1`; link B04's palette_tool command there once it exists.
- export_engine.py (B09) consumes clips built from `scale_frames.py` output; its docs can name `scale-frames.json` as the registration record.
- README tool table: the rows of section 3; requirements unchanged (numpy, Pillow, scipy optional).
- The integration e2e "still" pipeline (plan Appendix I) can use: fox fixture, `sheet_qc.py spill`, `scale_frames.py --sheet ... --emit-clips`, `build_animation_clips.py`, `export_engine all`.

## 8. Known limitations and what is not proven

- Real-art evidence is one sheet: the fox fixture (sha256 b9f14eaa...). Its numbers reproduce report v2 exactly: spill 58 px over the line with a 6 px overhang (77 px and 7 px at the visible edge), boundary band 24/0/246/0 px, 67,907 haze px and 7 detached specks; frames head ratio 0.94 for frame 6, 8/8 NEAR leading, torso drift 6.63 game px, row baseline step 1.12 game px, 3->4 seam 1.59x the median step. Ownership slicing is byte-identical to the prototype's `own_frames.npy`. Everything else is synthetic.
- The 8/8 NEAR figure and the 6.6 px drift use the fox's declared colours (rounded centres of the report v2 k-means palette: NEAR `#d15823,#4b2418`, FAR `#a03c19,#39180f`, tunic `#255e5e,#152e2d`). Without colours, auto mode finds 7 NEAR leading and leaves the narrow frame 5 unclear (the verdict is the same: duplicated half-cycle), and the trunk-geometry torso gives 6.24 game px. Auto NEAR/FAR relies on the "FAR one shade darker" convention that prompt-rules.md asks for; art that shades the other way needs declared colours.
- The head ratio is a template match of the top 35% of the body (`--head-frac`) at about 32 px template height, scales 0.90-1.10 in 0.02 steps. On the fox other bands and resolutions give 0.90-0.96 for frame 6; the default reproduces the prototype's 0.94. `--head-frac 0.35` suits chibi proportions; realistic proportions need about 0.15.
- Acceptance metric for scale_frames: report v2's premultiplied-RGB mean difference normalised by the 396x512 source canvas (the test's `report_v2_mae`): fox 3->4 26.54 before, 8.10 after at source size (`--scale-from 1 --shift-quantum 8`, identical to the prototype), 7.99 at game size (1/8 nearest). `scale-frames.json` records `forge_core.transition_mae` (premultiplied RGBA) on a shared crop instead, which reads 42.29 -> 13.30 for the same step; both fall by about 69%.
- Stance turn slide is exactly 0 at the neutral frame and at scale 1; at reduced scales whole output-pixel shifts leave up to 0.5 px of the stance offset, so `turn_slide_px` can reach 1 px (tested at 1/2: 0 and 1). A bbox anchor on the same synthetic figure slides 26 px (the tail pulls the bbox centre 13 px behind the feet).
- plan_guide's host-size rule comes from 3 calls of one host tool plus one 2:1 sheet; other tools may differ. The guide is not A/B tested with an image model (report v2 P2-4 is deferred), so the docs keep it opt-in. Its walk keyframes are hand-tuned for a readable guide, not measured gait data.
- `--key auto` keying of opaque chroma sheets was tested on synthetic magenta sheets only; QC numbers on soft-keyed edges depend on forge_matte's soft matte.
- Only Windows 11 with Python 3.13, numpy 2.5, Pillow 12.3 and scipy 1.18 was run. Python 3.10, Pillow 10.1, Linux and macOS were not; the sources avoid newer syntax and APIs.
- Performance on this machine (shared with other agents): spill on the 1536x1024 fox about 0.4 s in-process and 1.1 s as a CLI including interpreter start-up (the perf test budget is 1.5 s in-process); ownership slicing about 0.25 s; the frames analysis 0.9-1.2 s in-process; scale_frames 1.5-2.4 s as a CLI.
- Deviations from the plan text, with reasons:
  - `--emit-clips` writes `generate2dsprite.animation_clips.v1` (the plan says "clips.json" without a version): the builder in this base reads only v1, and B02's v2 builder keeps v1 readers, so the manifest builds before and after integration. Switch to v2 (with `pixel_art`, `sampling`, `body_height_px`) after B02 lands if wanted.
  - `--root-lock` and `--lock` overlap (both can set x): they are one horizontal lock (torso-x, stance, x or hip) and one vertical lock (row-baseline or feet); combining two of the same axis is an error.
  - Locks pin every frame to the `--neutral` frame (default 0), not to the median as in the report v2 prototype; a constant offset does not change any transition, and the root then belongs to a known pose.
  - `sheet_qc.py frames` defaults to `--cycle none` (identity, duplicates, drift, baselines and seams only); the phase and alternation checks need `--cycle run|walk`, because attacks and idles have no half-cycles.
  - `sheet_qc` exits 0 when it publishes a failing report (the report is the deliverable); `--strict` turns a failed check into exit 1 with nothing published, which is the plan's "strict-QC failure leaves nothing behind".
  - New optional features beyond the plan: `scale_frames.py --profile` (reuse an earlier action's scale, canvas and root) and `--shift-quantum`; `sheet_qc.py spill --expect` and `--count` for `--sheet` input in `sheet_qc.py frames` and `scale_frames.py` (sheets with spare cells); a new document type `sheet_plan.v1` (section 5.1).
