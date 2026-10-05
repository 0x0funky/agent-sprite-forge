# B16-hd2d-scene-motion: masked environment motion on static plates (build_motion_mask, scene_motion build and decoded-file qa, background-scenes.md)

Branch `asf/B16-hd2d-scene-motion`, from `wip/asf-upgrade-20261005` @ 3f9252d, integrated on `asf/integration` and revised in the Phase 3 group pass `asf/int-g-map-scene`. Files:

- [build_motion_mask.py](../skills/generate2dmap/scripts/build_motion_mask.py) (B16-T1).
- [scene_motion.py](../skills/generate2dmap/scripts/scene_motion.py): `build` (B16-T2) and `qa` (B16-T3).
- [background-scenes.md](../skills/generate2dmap/references/background-scenes.md), rewritten (B16-T4).
- [tests/test_motion_mask.py](../tests/test_motion_mask.py) (23 tests) and [tests/test_scene_motion.py](../tests/test_scene_motion.py) (29 tests; 21 carry the `ffmpeg` marker and skip without ffmpeg/ffprobe).

## Phase 3 integration (what changed, and why)

The Wave B review (group map-engines-scene) merged B16 without blocking items; this pass applied the conventions and the cheap non-blocking items. Resolved items cite the integration decisions.

- **Exit codes (D26, D27).** `scene_motion.py build --allow-seam-fail` still publishes a loop whose decoded seam fails (status `fail`, for review), and now exits 1 after publishing, with `error: the loop was published with status fail ...` on stderr, like every tool whose published report fails. Both tools' `main(argv)` run through `forge_core.run_cli` with `forge_av.ForgeAVError` as an expected error (one `error:` line, never a traceback, in-process calls too).
- **Conventions (D28-D30).** Plan and build-report JSON is read with `forge_core.read_json(strict=True)` (BOM tolerated; NaN, Infinity and duplicate keys refused); `tool.version` is `0.4.0`; `build_motion_mask.file_ref` is `forge_core.file_ref`, and a frame folder's path uses `forge_core.manifest_path`.
- **Tests on the real schemas (D33).** `requested_errors` in both test files validates against the vendored generate2dmap schemas; the in-memory fragments are gone. S1 expressed `motion_plan_v1.registration`'s scale/offset dependency with `allOf` + `if`/`then` instead of `dependentRequired` (same meaning).
- **Reviewer non-blocking items (review only, no Dn, except D26).** The temporary disk the build needs (full-plate PPM work frames, about 330 MB for a 136-frame 1280x720 loop) is stated in background-scenes.md and section 8. The forge_av promotion of the wrap-QP encoder (section 6.1) is still pending in A5's shared module; the GOP-alignment deviation is unchanged and documented. `--allow-seam-fail` now follows D26 (above).

## 1. CLIs

Run from the project root; `<skill-dir>` is the generate2dmap folder (`${CLAUDE_SKILL_DIR}` in Claude Code):

    python "<skill-dir>/scripts/build_motion_mask.py" --plan scenes/m01/motion-plan.json --plate scenes/m01/plate.png --output-dir scenes/m01/mask
    python "<skill-dir>/scripts/build_motion_mask.py" --plan scenes/m01/motion-plan.json --plate scenes/m01/plate.png --envelope-from scenes/m01/clip.mp4 --output-dir scenes/m01/mask
    python "<skill-dir>/scripts/scene_motion.py" build --plan scenes/m01/motion-plan.json --plate scenes/m01/plate.png --clip scenes/m01/clip.mp4 --mask scenes/m01/mask/motion-mask.png --output-dir scenes/m01/loop
    python "<skill-dir>/scripts/scene_motion.py" build --plan scenes/m01/motion-plan.json --plate scenes/m01/plate.png --clip scenes/m01/frames --fps 24 --mask scenes/m01/mask/motion-mask.png --output-dir scenes/m01/loop
    python "<skill-dir>/scripts/scene_motion.py" qa --report scenes/m01/loop/scene-motion.json --plate scenes/m01/plate.png --output-dir scenes/m01/loop-qa
    python "<skill-dir>/scripts/scene_motion.py" qa --video game/assets/m01-loop.mp4 --mask game/assets/m01-mask.png --plate scenes/m01/plate.png --plan scenes/m01/motion-plan.json --expect-frames 120 --output-dir scenes/m01/shipped-qa

`build_motion_mask.py` (plan `generate2dmap.motion_plan.v1`; the plate is an opaque PNG of `sourceSize`):

| Flag | Meaning |
|---|---|
| `--plan`, `--plate`, `--output-dir` | Required; the output folder must be new. |
| `--envelope-from CLIP` | Video file (forge_av decode) or folder of PNG frames. Over `loop.range`, pixels whose blurred luma range exceeds the threshold count as moving; those within `--envelope-reach` px of a region (and outside protected cores) join it. |
| `--fit contain\|cover`, `--transform S,X,Y`, `--fps` | Clip-to-plate registration (overrides `plan.registration`; plate = clip x S + (X, Y)); `--fps` for frame folders. |
| `--envelope-threshold 10`, `--envelope-reach 32`, `--envelope-min-area 16` | Luma levels, px, px. |
| `--protect-feather 8` | Transition outside each protected core unless the entry sets `feather`. |
| `--luma-min-area 16`, `--warn-mean-opacity 0.07` | Luma-band speck removal; mean-opacity warning. |

Writes `motion-mask.png` (8-bit L), `mask-overlay.png` (cyan motion, red protected cores, magenta excluded motion), `mask-qa.json` (`generate2dmap.motion_mask_qa.v1`: a QA envelope plus `protectedMax`, `coverage`, `coverageOver50`, `meanOpacity`, per-region and per-core figures and envelope counts) and, with an envelope, `motion-envelope.png`. A failed check (protectedMax not 0, an empty mask, an admitted moving pixel left at 0) exits 1 and publishes nothing; warnings (mean opacity below 0.07, a hidden region, excluded motion) publish with status `warn`. Summary keys: `output_dir`, `mask`, `overlay`, `metadata`, `status`, `protected_max`, `coverage`, `mean_opacity`.

`scene_motion.py build`:

| Flag | Meaning |
|---|---|
| `--plan`, `--plate`, `--clip`, `--mask`, `--output-dir` | Required. `--clip` is a video or a PNG frame folder; `--mask` is the reviewed `motion-mask.png`. |
| `--fit`, `--transform`, `--fps` | As above. |
| `--crf N`, `--keyint N` | Default `plan.encode`, else crf 18 and one GOP per loop; crf 1-51; keyint must divide the loop length. |
| `--ladder auto\|off` | auto: after a failed decoded seam, retry with wrap QP crf-8, then crf-12, then crf-14 (on each keyframe and the 8 frames before it), then crf-4 with wrap QP crf-16 (QP at least 1). |
| `--allow-seam-fail` | Publish a loop whose seam still fails, marked `fail`, for review; exit 1 after publishing (D26). |
| `--edge-fade 24` | Fade inside clip edges that do not reach the plate border. |
| `--max-seam-ratio 1.0`, `--warn-mean-opacity 0.07`, `--warn-motion-energy 2.0`, `--warn-leak 0.5`, `--warn-protected 0.5`, `--ring-px 4` | Decoded-QA gates (shared with `qa`). |

Writes `loop.mp4` (H.264 Main, closed GOPs aligned to the loop, BT.709, faststart; plate size padded right/bottom to even dimensions by edge repeat), `poster.png` (decoded loop frame 0 composited over the plate, so the poster-to-video swap changes no pixel), `motion-mask.png` (input mask x clip coverage) and `scene-motion.json` (`generate2dmap.scene_motion.v1`: plan copy, transform and shift estimates, frame map, pre-encode seam, video record, every encode attempt, QA envelope, metrics). Exit 1 with nothing published for: an invalid plan, plate, clip or mask; pingpong with a `flow`/`flicker` region; a keyint that does not divide the loop; a mask above 0 in a protected core; a pre-encode seam above the gate; a decoded frame-count, GOP or timestamp failure; or a decoded seam that still fails after the ladder (unless `--allow-seam-fail`). Summary keys: `output_dir`, `video`, `poster`, `mask`, `metadata`, `frames`, `fps`, `crf`, `wrap_qp`, `decoded_seam_over_p95`, `qa_status`.

`scene_motion.py qa`:

| Flag | Meaning |
|---|---|
| `--plate`, `--output-dir` | Required. |
| `--report` | A build's `scene-motion.json`: supplies video, mask, poster, plan, frame count and keyint. |
| `--video`, `--mask`, `--plan`, `--poster`, `--expect-frames`, `--keyint` | Explicit inputs for a loop from elsewhere (the video may add one padding row or column). |
| `--strict` | Publish nothing when the status is `fail`. |
| thresholds | As for `build`. |

Writes `loop-qa.json` (`generate2dmap.scene_loop_qa.v1`) and `seam-diff.png` (wrap difference x8 inside the mask). Exits 1 when the status is `fail` (the report is still published unless `--strict`). Summary keys: `output_dir`, `metadata`, `seam_diff`, `status`, `decoded_seam_over_p95`.

Both tools: errors print `error: ...` to stderr with exit 1 (an unexpected exception prints `error: internal error (<Type>: <message>)`, D27), usage errors exit 2, warnings print `warning: ...`; success prints one ASCII JSON line. A build published with status `fail` (`--allow-seam-fail`) exits 1 (D26). `--help` (and `build --help`, `qa --help`) is ASCII and exits 0 under cp1252 and cp950 (tested).

## 2. SKILL.md routing rows

generate2dmap, "Route by the scene":

| Need | Read |
|---|---|
| Animate water, fire, mist or cloth inside a still HD-2D plate | [background-scenes.md](references/background-scenes.md) "Masked motion on a static plate": write motion-plan.json, run `scripts/build_motion_mask.py --envelope-from`, review mask-overlay.png, run `scripts/scene_motion.py build`, then `scene_motion.py qa` on the decoded file |

generate2dmap, "Processing tools":

| Script | Purpose / limits |
|---|---|
| `scripts/build_motion_mask.py` | motion_plan.v1 regions (polygon, rect, luma band, landmark) on pixel centres; exact-zero protected cores with transitions outside them; `--envelope-from` grows regions to every pixel the clip moves and reports excluded motion; cannot judge or fix generated content |
| `scripts/scene_motion.py` | `build`: one fixed transform, composite only inside the mask, forward-overlap or sway-only pingpong, GOP-aligned H.264 gated on the decoded seam with a wrap-quality ladder; `qa`: decoded seam, motion energy, leak ring, region and protected stability, poster swap; no browser or device playback proof |

Acceptance bullet: "For masked scene loops, quote the `scene_motion.py qa` numbers (decoded_seam, frames, fps, keyint, bytes); never call a loop seamless without them."

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `generate2dmap/scripts/build_motion_mask.py` | Builds the motion mask of an HD-2D plate from a motion plan: feathered regions, exact-zero protected cores and a measured clip envelope | `tests/test_motion_mask.py` |
| `generate2dmap/scripts/scene_motion.py` | Composites a provider clip inside the mask on the plate, loops it forwards (or sway-only pingpong), encodes GOP-aligned H.264 and gates the decoded in-mask seam; `qa` measures any scene loop | `tests/test_scene_motion.py` (ffmpeg) |

Requirements row: `scene_motion.py` needs ffmpeg 5.1+ with libx264 (forge_av); `build_motion_mask.py` needs ffmpeg only for `--envelope-from` with a video file (frame folders need none). scipy is used when present; the numpy fallback gives identical masks.

## 4. CHANGELOG entries

- Added: `build_motion_mask.py` (B16-T1): polygon, rect, luma-band and landmark regions from `motion_plan.v1`, rasterised on pixel centres (platform-independent); margins and feathers by exact Euclidean distance, feathering only outside each region and outside each exact-zero protected core (protectedMax 0); `--envelope-from` measures the clip's motion after the fixed transform so that the opaque core contains every moving pixel, and reports motion inside protected cores or away from every region; `mask-qa.json` with coverage and mean opacity (warns below 0.07).
- Added: `scene_motion.py build` (B16-T2): one fixed clip-to-plate transform (contain, cover or explicit, with an edge fade where the clip does not reach the plate and a whole-pixel shift check on static areas); compositing only inside the mask, with the outside exactly equal to the plate before encoding; forward-overlap loops (default 16 frames, smoothstep crossfade, no layer ever reversed) or pingpong for regions marked `sway`; closed-GOP H.264 aligned to the loop through forge_av; a decoded-seam gate with an automatic wrap-quality ladder; outputs `loop.mp4`, `poster.png`, `motion-mask.png`, `scene-motion.json`.
- Added: `scene_motion.py qa` (B16-T3): decoded-file QA of a scene loop as the runtime composites it: in-mask seam over the adjacent p95 (fail above 1.0), motion energy and mean mask opacity (warn when motion is invisible), the leak ring outside the mask, region stability with a still-region fallback, protected-core drift, poster swap, frame count, GOP alignment and timestamps.
- Changed: `generate2dmap/references/background-scenes.md` (B16-T4): masked-motion workflow with single-line commands, loop policy table, amplitude and negatives for clip prompts, "masks cannot fix content" with review verdicts, decoded QA table, the full-frame loop workflow (`assemble_frames.py --sequence --static-regions`) and decoder budgets.
- BREAKING: none (new tools; the reference keeps its routes and runtime guidance).
- Fixed: Appendix J assigns no finding id to B16. Evidence addressed: a crossfade loop, seamless before encoding, that popped at the wrap after a single-GOP encode (decoded seam 2.2x p95 in the mask; seam-codec-findings.json); motion hidden by a diluted mask (6.96% mean opacity; MOTION-v2.md); a pennant cut off by a too-small mask (repack-flag-v2.py); and feathers that reached into protected landmarks (protect-environment-landmarks.py).

## 5. Schema change requests

Status: applied by the shared stage S1 (commit f3d7eb2) in `shared/schemas/map.schema.json` and vendored; `motion_plan_v1.registration`'s `dependentRequired` was written as `allOf` + `if`/`then` (same meaning). Both test files now validate every document the tools write against the vendored schemas (D33). The fragments below are kept as the record of what was requested.

Producer: B16 (`build_motion_mask.py`, `scene_motion.py`). Consumers: agents, Z docs and the integration e2e (Appendix I, pipeline 5).

1. Optional plan fields read by both tools (`motion_plan_v1` stays open; these document and type them).

`$defs.motionRegion.properties.motion` (default `flow`; pingpong is refused unless every region is `sway`):

```json
{"enum": ["flow", "flicker", "sway"]}
```

`$defs.motion_plan_v1.properties.protected.items.properties.feather` (transition width outside the exact-zero core; default 8):

```json
{"type": "number", "minimum": 0}
```

`$defs.motion_plan_v1.properties.registration` (plate = clip x scale + offset; an explicit scale and offset win over fit):

```json
{
  "type": "object",
  "properties": {
    "fit": {"enum": ["contain", "cover"]},
    "scale": {"type": "number", "exclusiveMinimum": 0},
    "offset": {"$ref": "common.schema.json#/$defs/point2"}
  },
  "dependentRequired": {"scale": ["offset"], "offset": ["scale"]}
}
```

2. New `$defs.motion_mask_qa_v1` (`mask-qa.json`, id `generate2dmap.motion_mask_qa.v1`):

```json
{
  "description": "build_motion_mask.py mask-qa.json: a QA envelope plus the figures of the published mask. A mask is published only when its protected cores are exactly 0.",
  "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
  "type": "object",
  "required": [
    "schema",
    "plateSize",
    "protectedMax",
    "coverage",
    "coverageOver50",
    "meanOpacity",
    "regions",
    "protected"
  ],
  "properties": {
    "schema": {"const": "generate2dmap.motion_mask_qa.v1"},
    "plateSize": {"$ref": "common.schema.json#/$defs/size2"},
    "protectedMax": {"const": 0},
    "coverage": {"type": "number", "minimum": 0, "maximum": 1},
    "coverageOver50": {"type": "number", "minimum": 0, "maximum": 1},
    "meanOpacity": {"type": "number", "minimum": 0, "maximum": 1},
    "regions": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "kind", "motion", "supportPx", "visiblePx", "meanOpacity"],
        "properties": {
          "id": {"type": "string", "minLength": 1},
          "kind": {"enum": ["polygon", "rect", "luma_band", "landmark"]},
          "motion": {"enum": ["flow", "flicker", "sway"]},
          "supportPx": {"type": "integer", "minimum": 0},
          "visiblePx": {"type": "integer", "minimum": 0},
          "meanOpacity": {"type": "number", "minimum": 0, "maximum": 1}
        }
      }
    },
    "protected": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "corePx", "max"],
        "properties": {
          "id": {"type": "string", "minLength": 1},
          "corePx": {"type": "integer", "minimum": 0},
          "max": {"const": 0}
        }
      }
    },
    "envelope": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "object",
          "required": [
            "movingPx",
            "admittedPx",
            "containedPx",
            "uncoveredPx",
            "excludedProtectedPx",
            "excludedOutsidePx",
            "reach",
            "threshold",
            "frames",
            "transform"
          ],
          "properties": {
            "movingPx": {"type": "integer", "minimum": 0},
            "admittedPx": {"type": "integer", "minimum": 0},
            "containedPx": {"type": "integer", "minimum": 0},
            "reducedByProtectionPx": {"type": "integer", "minimum": 0},
            "excludedProtectedPx": {"type": "integer", "minimum": 0},
            "excludedOutsidePx": {"type": "integer", "minimum": 0},
            "frames": {"type": "integer", "minimum": 0},
            "uncoveredPx": {"const": 0},
            "reach": {"type": "number", "minimum": 0},
            "threshold": {"type": "number", "minimum": 0}
          }
        }
      ]
    },
    "settings": {"type": "object"}
  }
}
```

3. New `$defs.scene_motion_v1` (`scene-motion.json`, id `generate2dmap.scene_motion.v1`):

```json
{
  "description": "scene_motion.py build: scene-motion.json, the record of a masked environment loop on a plate.",
  "type": "object",
  "required": [
    "schema",
    "plan",
    "plate",
    "clip",
    "registration",
    "loop",
    "composite",
    "video",
    "poster",
    "mask",
    "encodeAttempts",
    "qa"
  ],
  "properties": {
    "schema": {"const": "generate2dmap.scene_motion.v1"},
    "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
    "plan": {
      "allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}],
      "properties": {"document": {"$ref": "#/$defs/motion_plan_v1"}}
    },
    "plate": {
      "allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}],
      "required": ["size"],
      "properties": {"size": {"$ref": "common.schema.json#/$defs/size2"}}
    },
    "clip": {
      "allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}],
      "required": ["kind", "frames", "size", "fps"],
      "properties": {
        "kind": {"enum": ["video", "frames"]},
        "frames": {"type": "integer", "minimum": 1},
        "size": {"$ref": "common.schema.json#/$defs/size2"},
        "fps": {"$ref": "common.schema.json#/$defs/fpsValue"}
      }
    },
    "maskSource": {"$ref": "common.schema.json#/$defs/fileRef"},
    "registration": {
      "type": "object",
      "required": ["fit", "scale", "offset", "footprint"],
      "properties": {
        "fit": {"enum": ["contain", "cover", "explicit"]},
        "scale": {"type": "number", "exclusiveMinimum": 0},
        "offset": {"$ref": "common.schema.json#/$defs/point2"},
        "footprint": {"$ref": "common.schema.json#/$defs/box"},
        "edgeFadePx": {"type": "number", "minimum": 0},
        "coverageMin": {"type": "number", "minimum": 0, "maximum": 1},
        "shiftEstimates": {"type": "array"}
      }
    },
    "loop": {
      "type": "object",
      "required": [
        "policy",
        "overlap",
        "range",
        "frameCount",
        "fps",
        "reversedSteps",
        "frames"
      ],
      "properties": {
        "policy": {"enum": ["forward-overlap", "pingpong"]},
        "overlap": {"type": "integer", "minimum": 0},
        "range": {
          "type": "array",
          "items": {"type": "integer", "minimum": 0},
          "minItems": 2,
          "maxItems": 2
        },
        "frameCount": {"type": "integer", "minimum": 2},
        "fps": {"$ref": "common.schema.json#/$defs/fpsValue"},
        "durationMs": {"type": "number", "exclusiveMinimum": 0},
        "reversedSteps": {"type": "integer", "minimum": 0},
        "frames": {
          "type": "array",
          "minItems": 2,
          "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
              "type": "array",
              "prefixItems": [
                {"type": "integer", "minimum": 0},
                {"type": "number", "minimum": 0, "maximum": 1}
              ],
              "minItems": 2,
              "maxItems": 2
            }
          }
        }
      }
    },
    "composite": {
      "type": "object",
      "required": ["outsideMaskMaxDelta", "sourceSeam"],
      "properties": {
        "outsideMaskMaxDelta": {"const": 0},
        "sourceSeam": {"$ref": "common.schema.json#/$defs/seamReport"}
      }
    },
    "video": {
      "allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}],
      "required": [
        "codec",
        "encodedSize",
        "crop",
        "crf",
        "keyint",
        "closedGop",
        "keyframes",
        "fps"
      ],
      "properties": {
        "codec": {"const": "h264"},
        "encodedSize": {"$ref": "common.schema.json#/$defs/size2"},
        "crop": {"$ref": "common.schema.json#/$defs/box"},
        "padding": {"$ref": "common.schema.json#/$defs/padding4"},
        "crf": {"type": "integer", "minimum": 1, "maximum": 51},
        "keyint": {"type": "integer", "minimum": 1},
        "closedGop": {"const": true},
        "keyframes": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "wrapQp": {"type": ["integer", "null"], "minimum": 1, "maximum": 51},
        "wrapFrames": {"type": "integer", "minimum": 0},
        "fps": {"$ref": "common.schema.json#/$defs/fpsValue"},
        "decodedPixelsPerSecond": {"type": "number", "exclusiveMinimum": 0}
      }
    },
    "poster": {"$ref": "common.schema.json#/$defs/fileRef"},
    "mask": {"$ref": "common.schema.json#/$defs/fileRef"},
    "encodeAttempts": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["crf", "wrapQp", "bytes", "sha256", "decodedSeamOverP95", "status"],
        "properties": {
          "status": {"enum": ["pass", "fail"]},
          "sha256": {"$ref": "common.schema.json#/$defs/sha256"}
        }
      }
    },
    "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
    "metrics": {"type": "object"},
    "thresholds": {"type": "object"}
  }
}
```

4. New `$defs.scene_loop_qa_v1` (`loop-qa.json`, id `generate2dmap.scene_loop_qa.v1`):

```json
{
  "description": "scene_motion.py qa: loop-qa.json, a QA envelope of a decoded scene loop with its metrics.",
  "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
  "type": "object",
  "required": ["schema", "metrics", "thresholds"],
  "properties": {
    "schema": {"const": "generate2dmap.scene_loop_qa.v1"},
    "metrics": {
      "type": "object",
      "required": [
        "frames",
        "keyframes",
        "decodedSeam",
        "meanOpacity",
        "motionEnergy",
        "leakRing",
        "regions"
      ],
      "properties": {"decodedSeam": {"$ref": "common.schema.json#/$defs/seamReport"}}
    },
    "thresholds": {"type": "object"}
  }
}
```

5. A note, with no diff requested: `motion_plan_v1.encode.crf` allows 0-63 (the VP9 range). Scene loops are H.264 Main, so the tools accept crf 1-51 and refuse 0 (Main has no lossless mode). Narrow it to `{"minimum": 1, "maximum": 51}` only if no VP9 scene encoder is planned.

Optional fields the producers write beyond these fragments (objects are open; listed so they do not drift):

- `mask-qa.json`: `envelope.range`, `envelope.minArea`, `envelope.lumaRangeMax`, `envelope.transform` (`fit`, `scale`, `offset`, `clipSize`, `plateSize`, `footprint`, `map`), `settings.{protectFeather, lumaMinArea, luma, warnMeanOpacity}`, and `kind`, `frames`, `size`, `fps` on the clip input.
- `scene-motion.json`: `registration.{clipSize, plateSize, map}`, `registration.shiftEstimates[{frame, status, shift, maeAtZero, maeAtBest, pixels | reason}]`, `loop.blend`, `composite.formula`, `video.{bytes, profile}`, `metrics` (the `loop-qa.json` metrics object) and `thresholds`.
- `loop-qa.json`: `metrics.{gop, video, rgbSteps, protectedDrift, poster}`.

## 6. Shared-helper promotion requests

1. forge_av (A5): add keyword-only `wrap_qp: int | None = None, wrap_frames: int = 8` to `encode_h264_loop`, from `scene_motion._local_encode_h264_loop` (scene_motion.py:219) and `scene_motion.wrap_zones(count, keyint, wrap_frames, qp) -> str` (scene_motion.py:208). With `wrap_qp` set, it appends `-x264-params zones=...`: QP `wrap_qp` on every keyframe and on the last `wrap_frames` frames of every GOP (merged into runs), and records `wrapQp` and `wrapFrames` in the result. Reason: an aligned closed GOP still pops at the wrap, because the keyframe refresh meets frames that drifted through the GOP. Spending bits there fixes the pop for about 70% of the bytes of a global crf drop. Measured on 120-frame masked loops (decoded in-mask seam / p95, bytes). Two are synthetic drift loops; the third is a real provider clip (the shipped M02 lake scene's native 1280x720 clip, frames [4, 140), overlap 16, with its own mask, mean opacity 0.115; plate and mask resized to the clip; run by hand outside the suite, reading the owner's files). The wrap QP is the lever: lowering the global crf alone barely moves the seam, which is why the ladder lowers the wrap QP before the crf.

   | Encode | synthetic 1672x940, opacity 0.13 | synthetic 1280x720, 0.28 | real M02 clip, 0.115 |
   |---|---|---|---|
   | crf 18, one GOP (rung 0) | 1.42, 0.91 MB | 1.35, 0.93 MB | 3.37, 1.01 MB |
   | crf 14, no zones | 1.10, 1.37 MB | (not run) | (not run) |
   | crf 10, no zones | 0.91, 2.38 MB | (not run) | (not run) |
   | crf 18, wrap QP 10 over 8 frames (rung 1) | 0.74, 1.67 MB | 0.68, 1.65 MB | 1.13, 1.69 MB |
   | crf 18, wrap QP 10 over 16 frames | (not run) | (not run) | 1.11, 1.96 MB |
   | crf 14, wrap QP 10 | (not run) | (not run) | 1.12, 2.11 MB |
   | crf 18, wrap QP 6 (rung 2) | (not run) | (not run) | 0.73, 1.98 MB |
   | crf 14, wrap QP 6 | (not run) | (not run) | 0.72, 2.40 MB |
   | crf 18, wrap QP 4 (rung 3) | (not run) | (not run) | 0.57, 2.06 MB |

   Until it is promoted, the local helper mirrors `encode_h264_loop` through twelve forge_av private names (`_Clip`, `_crf`, `_keyint`, `_tool`, `_FFMPEG_GLOBAL`, `_raw_input`, `_to_bt709`, `_x264_args`, `_encode_file`, `_opaque_rgb`, `_digest`, `_DETERMINISTIC_MUX`), so the two encodes differ only by the zones. Renaming any of them breaks it, and tests/test_scene_motion.py catches that. Tests to move with it: `LoopAssemblyTests.test_wrap_zones_cover_every_keyframe_and_the_frames_before_it` and `BuildTests.test_single_gop_encode_pops_and_more_bits_at_the_wrap_pass`.
2. forge_av: a public `keyframes(path) -> list[int]` (presentation-order keyframe indices of the main video stream), from `scene_motion.keyframe_indices(path, stream_index)` (scene_motion.py:267). forge_av already has the private `_packets` and `_keyframes`.
3. forge_core (A1), optional: `distance_from(mask, limit)` (build_motion_mask.py:345, with `_bounded_distance` at :324), an exact Euclidean distance up to `limit` with scipy or an identical numpy path (`FORGE_CORE_NO_SCIPY=1` honoured); and `rasterize_polygon(points, size)` and `rasterize_box(box, size)` (build_motion_mask.py:283 and :273), a pixel-centre, half-open, even-odd scanline fill that is platform-independent. Walk polygons, footprints and feathers in B11, B13, B15, B17 and B21 could share them. Tests: `RasterTests` in tests/test_motion_mask.py.
4. forge_core, optional: `composite(frame, plate, mask)` (scene_motion.py:151), an exact integer mask composite, `(plate * (255 - m) + frame * m + 127) // 255` in uint16, equal to the plate bit for bit wherever the mask is 0.

## 7. Cross-module links that Z must add

- generate2dmap SKILL.md: add the rows of section 2, link background-scenes.md from the HD-2D route, and point production step 5 ("Add selected motion") to the masked-motion workflow.
- B15 (hd2d-plates.md, hd2d-presentation.md): link background-scenes.md "Masked motion on a static plate". `stage_v1.effects[]` (UV polygons; ripple, shimmer, sway, glow) maps onto motion-plan regions by multiplying UV by `sourceSize`, with ripple and shimmer as `flow`, sway as `sway` and glow as `flicker`. There is no converter yet.
- B17 (scene preview) and B09 (forge-runtime.mjs): the recommended runtime is a masked overlay. Draw the plate, then `loop.mp4` cropped to `video.crop` with `motion-mask.png` as alpha, and show `poster.png` until a decoded frame is drawable.
- B02: background-scenes.md uses `assemble_frames.py --sequence` and `--static-regions`, which exist before and after B02. Once B02 lands, Z may also mention `--static-region-search N` and `--loop-overlap K --ambient` there.
- B07/B08: background-scenes.md asks for `video2dsprite.review_verdict.v1` verdicts on scene clips (accepted, accept_with_mask, fail_regenerate).
- Integration e2e (Appendix I, pipeline 5): run `build_motion_mask.py --envelope-from`, then `scene_motion.py build`, then `scene_motion.py qa --report`. In tests/test_scene_motion.py, `drifting_scene()` builds a small synthetic scene that exercises the ladder in about 2 s.
- A5 docs (pipeline.md, Z): scene loops are the first adopter of `encode_h264_loop`; its "gate on the decoded file" advice is implemented here.

## 8. Known limitations and what is not proven

- Deviation from the B16-T3 acceptance "a GOP-aligned encode passes": alignment alone does not pass. A5 predicted this and the build reproduces it. The plain one-GOP crf 18 encode (rung 0) fails at 1.35-1.66x p95 on the synthetic scenes and at 3.37x on the real M02 clip; the build passes only after a wrap-quality rung (rung 1, wrap QP 10, on the synthetic scenes; rung 2, wrap QP 6, on the real clip). The tests therefore prove that a single-GOP crossfade fails (rung 0, `--ladder off`) and that the build's GOP-aligned encode with wrap quality passes.
- The suite's QA evidence is synthetic (sub-pixel drifting texture in a water band, generated in the tests); it has no real provider clip, because no video fixture with provenance exists. Real footage was checked by hand only, reading the owner's files and writing to a scratch folder: `qa` on the shipped M02 lake loop (a single-GOP crossfade, 1280x720, 120 frames) reports `decoded_seam` 2.04 x p95 (fail; the study measured 2.2 inside the mask), mean opacity 0.1146 (the study's 0.1146), motion energy 7.1 and `leak_ring` 1.96 (warn: that file was never composited, so the plate moves outside the mask). `build` on the same native clip gives a pre-encode seam of 0.25 and passes at rung 2 (crf 18, wrap QP 6: 0.73, 1.98 MB). `qa --report` on that output passes every check (leak ring 0.002, poster swap 0.0). The build also flags a 1 px vertical registration shift, which is real: the plate was stretched to 1280x720 here, while the provider input had been scaled uniformly to 1280x721 and cropped. The thresholds rest on one project's loops and on these synthetic cases: seam 1.0, mean opacity 0.07, motion energy 2.0, leak ring and protected drift 0.5, still floor 0.05 and tolerance 1.0, envelope 10 luma levels.
- x264 output differs between builds, so seam numbers vary by machine; the tests assert outcomes with wide margins (rung 0 above 1.6, rung 1 near 0.6). Only Windows 11 was run, with Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1 and ffmpeg 8.0.1 (gyan full build). The new files parse with the Python 3.10 grammar. Linux, macOS, Python 3.10 and Pillow 10.1 were not run.
- The wrap-quality encode depends on forge_av private names until request 6.1 is applied.
- `--allow-seam-fail` publishes a failing loop for review and exits 1 (D26); scripts that chain the build must treat exit 1 with a published folder as "kept for review", not as "nothing written".
- Registration is one fixed uniform transform. The shift check searches whole-pixel translations within 2 px on the static areas of three frames (a larger shift is flagged but its size is clipped); provider zoom or rotation drift is not modelled. The envelope uses luma only, so a hue change at constant luma is not detected.
- Per-region QA uses each region's plan geometry, not the envelope-grown shape. Motion admitted by the envelope counts in the global figures only.
- The QA composite is decoded x mask + plate x (1 - mask), the masked-overlay runtime. Full-frame playback is covered only by the leak ring and the protected-core drift. Playback in browsers, engines, Safari and on iPhone, decoder throughput and memory are not proven.
- Costs on this machine for a 1280x720 plate with a 136-frame clip (a 120-frame loop): the mask with an envelope takes about 14 s, the build about 53 s (two encodes, each decoded and measured) and qa about 11 s. The build writes uncompressed PPM work frames into its stage, about width x height x 3 bytes per loop frame (roughly 570 MB for 121 frames at 1672x941), and deletes them before publishing.
- Scene loops are opaque H.264 only; the mask ships separately. There is no VP9 or packed-alpha scene output.
- `scene_motion.py build` takes the reviewed mask (`--mask`) instead of building one from the plan, so the overlay review cannot be skipped by accident. The plan is still read for the loop, the protected cores and the regions.
