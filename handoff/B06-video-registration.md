# B06-video-registration: registration by construction (prepare_i2v_input, register_clip apply/profile/validate-profile/qc/palette-repair) and the video prompt contract

Branch `asf/B06-video-registration` (from `wip/asf-upgrade-20261005` @ 3f9252d). New tools
[prepare_i2v_input.py](../skills/video2dsprite/scripts/prepare_i2v_input.py) and
[register_clip.py](../skills/video2dsprite/scripts/register_clip.py), tests
[tests/test_video_registration.py](../tests/test_video_registration.py) (22 tests, one `ffmpeg`-marked), and the
rewritten [video prompt-rules.md](../skills/video2dsprite/references/prompt-rules.md) and
[sprite video-handoff.md](../skills/generate2dsprite/references/video-handoff.md).

How it works: `prepare` places the master (or one view of a sheet) on the provider canvas with the root on a fixed
pixel (640, 620 on 1280x720) at one scale, and records `referenceScale`/`referenceOffset` in
`registration_job.v1`. `apply` maps every keyed frame back with the single inverse (output = video *
inverse.scale + inverse.offset), using `forge_core.resample_rgba` with anchors (premultiplied, grid pinned).
Nothing is fitted per frame, so the body never pumps in size and contact motion survives.

## 1. CLIs

Run from the user's project root; `<skill-dir>` is the video2dsprite skill folder (`${CLAUDE_SKILL_DIR}` in
Claude Code).

    python "<skill-dir>/scripts/prepare_i2v_input.py" prepare --master art/hero.png --action attack --subject "compact knight with a red scarf" --facing right --output-dir jobs/hero-attack
    python "<skill-dir>/scripts/prepare_i2v_input.py" prepare --master art/hero.png --action idle --pixel-art --output-dir jobs/hero-idle
    python "<skill-dir>/scripts/prepare_i2v_input.py" prepare --master art/sheet.png --view-box 557,0,1115,941 --action walk --output-dir jobs/hero-front-walk
    python "<skill-dir>/scripts/prepare_i2v_input.py" lint --prompt-file my-prompt.txt --key magenta
    python "<skill-dir>/scripts/register_clip.py" qc --job jobs/hero-attack/registration_job.json --video takes/hero-attack-1.mp4
    python "<skill-dir>/scripts/register_clip.py" apply --job jobs/hero-attack/registration_job.json --frames work/hero-attack-1/frames-clean --output-dir work/hero-attack-reg
    python "<skill-dir>/scripts/register_clip.py" apply --job jobs/hero-walk/registration_job.json --frames work/hero-walk-1/frames-clean --lock feet,hip --character-profile profiles/hero.json --output-dir work/hero-walk-reg
    python "<skill-dir>/scripts/register_clip.py" apply --job jobs/heal-fx/registration_job.json --frames work/heal-1/frames-clean --profile fx --output-dir work/heal-reg
    python "<skill-dir>/scripts/register_clip.py" profile --registration work/hero-idle-reg/registration.json --id hero --output profiles/hero.json
    python "<skill-dir>/scripts/register_clip.py" validate-profile --profile profiles/hero.json --registration work/hero-idle-reg/registration.json --registration work/hero-walk-reg/registration.json
    python "<skill-dir>/scripts/register_clip.py" palette-repair --frames work/hero-walk-reg/frames --job jobs/hero-walk/registration_job.json --region 120,420,330,552 --hue 70,170 --palette-box 150,400,300,448 --output-dir work/hero-walk-repaired

- `prepare` writes `input.png` (send it), `prompt.txt` (send it), `registration_job.json`, a byte copy of the master
  (`master.png`, so the job is self-contained and hashed) and `review-guide.png` (work region and root drawn; never
  send it). Key: `--key auto` picks magenta, then green, then blue with `forge_matte.choose_key_color` and refuses a
  key the master's colours lean to (`--allow-key-conflict` overrides, recorded). `--scale auto` fits the subject to
  0.736 of the canvas height inside a 20 px margin; `--pixel-art` floors it to an integer and places at an integer
  offset with NEAREST. Default action padding per template: one-shots `[96, 80, 96, 24]` on 448 px (scaled with
  the canvas), fx 160 px a side, loops none. Placement QC (subject inside the margin), a key conflict, a fractional
  pixel-art scale and `--strict-lint` findings fail the run with nothing published. Summary keys: `output`,
  `metadata` (the job), `input`, `prompt`, `action`, `referenceScale`, `referenceRoot`, `keyColor`, `workRegion`,
  `padding`, `warnings`; lint warnings also go to stderr as `warning: ...`.
- `lint` prints `{"status": "pass"|"warn", "findings": [...]}`; `--strict` exits 1 on any finding.
- `apply` reads keyed RGBA frames at the video's size (B05 process `frames-clean`; natural sort, `--pattern`),
  writes `frames/frame_000000.png...`, `registration.json` and `review-contact.png`. `--fit auto` (default) is one
  factor when the aspect matches and a centred crop otherwise; `stretch`, `cover`, `contain` are explicit.
  `--action-padding` overrides the job's padding. Registration QA (fails with nothing published): subject touching
  the video frame (regenerate), subject past the padded canvas (the error prints the exact `--action-padding` that
  contains it), rest pose more than 2 px from the master's anchor or 3% off its height (both measured on the 50%
  alpha contour). `--allow-edge-touch` downgrades the first two to warnings. `--rest-frames` are source indices
  (default 0, fx none); `--range START:END` registers a slice. Summary keys: `output`, `metadata`, `frames`,
  `sourceSize`, `sourceAnchor`, `fitApplied`, `qa`, `warnings`.
- `profile` writes one `video2dsprite.character_profile.v1` file (`--output`, refused when it exists) from one or
  more registrations that agree on master, base canvas, anchor, anchor mode and reference scale (0.5% tolerance);
  matte settings come from flags (`--matte-mode soft`, `--key`, `--erode 0`, `--unmix`, `--despill-mode auto`).
- `validate-profile` writes nothing; it prints the JSON report (status, per-clip issues, `nativeFootBottomRange`)
  and exits 1 with `error: profile validation failed: ...` on a mismatch or a foot-line spread over
  `--foot-tolerance` (2 px).
- `qc` appends one `video2dsprite.take.v1` line per take to `takes.jsonl` (default: next to the job; a take id is
  recorded once, a repeat is refused) and prints `take`, `status`, `reasons`, `warnings`, `takes`/`metadata`. It
  exits 0 for kept and rejected takes; `--strict` exits 1 on a rejection (the line is still written: the log is the
  audit trail). `--video` decodes with forge_av (ffmpeg); `--frames` reads raw frames (`--fps`, default 24).
- `palette-repair` writes repaired `frames/`, `palette-repair.json` and `changed-mask.png` to a new folder; it fails
  with nothing published when the rule would change more than `--max-changed-fraction` (0.6) of a frame's visible
  region pixels.
- Every `--help` (both scripts, every verb) is ASCII and exits 0 under cp1252 and cp950 (tested).

## 2. SKILL.md routing rows

video2dsprite:

| Need | Route |
|---|---|
| Prepare an image-to-video request (input image, prompt, transform) | `scripts/prepare_i2v_input.py prepare --master <art> --action <idle,walk,run,attack,cast,guard,hurt,victory,defeat,ambient,fx>`; send `input.png` + `prompt.txt` |
| Check a hand-written motion prompt | `scripts/prepare_i2v_input.py lint --prompt-file <txt> --key <key>` |
| Accept or reject a returned take before processing | `scripts/register_clip.py qc --job <job> --video <take>`; read `takes.jsonl` |
| Register keyed frames onto the master canvas | `scripts/register_clip.py apply --job <job> --frames <frames-clean> --output-dir <new>`; then gait_loop or retime on `frames/` |
| Feet drift, jumps, knockback | `apply --lock feet` (add `hip` or `x` to pin x) |
| Spell or hit effect clips | `prepare --action fx`, then `apply --profile fx` (edge fade, dissolve tail, displayScale) |
| Keep every clip of a character on one scale and anchor | `register_clip.py profile`, then `apply --character-profile`, `validate-profile` |
| Generated colour flash in one region (boots, hands) | `register_clip.py palette-repair --region ... --hue ...`; alpha never changes |

generate2dsprite:

| Need | Route |
|---|---|
| Animate an accepted master with image-to-video | [video-handoff.md](../skills/generate2dsprite/references/video-handoff.md): registration by construction with video2dsprite `prepare_i2v_input.py` and `register_clip.py` |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `video2dsprite/scripts/prepare_i2v_input.py` | Places the master on the provider canvas at a recorded scale and root, chooses the key, writes the timeline prompt with a numeric work region and `registration_job.v1`; lints prompts | `test_job_roundtrip_schema`, `test_pixel_master_integer_scale`, `test_prompt_contains_work_region_and_timeline`, `test_lint_warns_on_tiny_and_extremely_slow` |
| `video2dsprite/scripts/register_clip.py` | Registers keyed frames with one inverse transform (padding, locks, actor/fx profiles), character profiles, take QC to `takes.jsonl`, region-limited palette repair | `test_anchor_error_within_half_pixel` (4 provider sizes), `test_action_padding_canvas_and_anchor`, `test_feet_lock_zero_ground_drift`, `test_profile_mismatch_fails`, `test_qc_flags_push_in_and_keeps_locked_clip`, `test_fx_profile_fades_edges`, `test_palette_repair_alpha_unchanged` |

## 4. CHANGELOG entries

- Added: `prepare_i2v_input.py prepare` / `lint`: master or sheet view on a 1280x720 provider canvas, root on a fixed
  pixel, integer NEAREST scale for pixel art, key choice, per-action timeline templates (idle, walk, run, attack,
  cast, guard, hurt, victory, defeat, ambient, fx) with a numeric work region, treadmill wording, locked camera,
  body-only rule, amplitude and cycle wording and negatives; `registration_job.v1`, `input.png`, `prompt.txt`
  (B06-T1).
- Added: `register_clip.py apply`: one stored inverse transform per clip (one factor for a same-aspect reply such as
  960x960 from a square input, a fixed centre crop for 1264x720 from 1280x720, `--fit stretch` for `sx = W/1280,
  sy = H/720`), premultiplied anchor-pinned resampling, action padding that grows the canvas and shifts the anchor
  (448 + [96, 80, 96, 24] = 640x552 at [320, 510]), `registration.json` with mode `construction`, registration QA
  and a review contact sheet (B06-T2).
- Added: `--lock feet|x|hip` in whole output pixels, character profiles (`profile`, `apply --character-profile`,
  `validate-profile`) and `nativeFootBottomRange` (B06-T3).
- Added: `register_clip.py qc`: 36 px landmark and head-region identity NCC (+-15 px search), camera scale and drift
  in frame 0 and the end calm span, edge key purity; one `take.v1` line per take in `takes.jsonl` (B06-T4).
- Added: `apply --profile fx` (padding, edge fade, dissolve tail, displayScale) and `palette-repair`
  (region-limited, alpha unchanged, audited) (B06-T5).
- Changed: video `prompt-rules.md` (prompt contract, timelines, amplitude and cycle table, negatives, no famous names,
  FX) and sprite `video-handoff.md` (construction workflow, padding contract, "regenerate, don't pad", early calm
  span) (B06-T6).
- BREAKING: none (new tools; `video2dsprite.py` keeps its fixed-envelope registration, owned by B05).
- Fixed: registration fitted to alpha bounds pumps body scale between frames and clips (amber-quay 2.2-4.8%,
  study-hd2d-demos/amber-quay-registration-findings.json; video2dsprite.py:226-254 fits a per-clip envelope);
  registration by construction uses the transform the input was built with. Motion-killing prompt words (hd2d
  MOTION-v2.md:15) are linted. A 1264x720 reply to a 1280x720 input is a centre crop (hd2d MOTION-v2.md, checked
  with 12 landmarks); `--fit auto` registers it as one, where the stretch formula `sx = W/1280` would be off by up
  to 4 px across a centred subject.

## 5. Schema change requests

Exact JSON Schema additions for `shared/schemas/video.schema.json` (then `tools/vendor_sync.py --write`). They are
additive: no existing field changes and no new required field on the frozen `$defs`. `tests/test_video_registration.py`
holds the same JSON (`REQUESTED_DEFS`, `REQUESTED_PROPERTIES`) and validates every document B06 writes against the
vendored schema with these additions applied in memory (`assert_requested`); delete that helper once integration
applies them and use `assert_valid_contract`.

**5.1 New `$defs.registration_v1`** in `shared/schemas/video.schema.json`. Producer: B06 `register_clip.py apply`. Consumers: B06 `profile` and `validate-profile`, B08 `package --registration`, B07 (frame selection on `frames/`). Reason: the plan names registration.json but A0 froze no contract for it.

```json
"registration_v1": {
  "description": "registration.json written by register_clip.py apply (registration by construction). One transform maps every frame of the provider video onto the source canvas: output = video * transform.inverse.scale + transform.inverse.offset + the frame's whole-pixel lock shift. sourceSize/sourceAnchor are the padded canvas (animation.json sourceSize/sourceAnchor); baseSize/baseAnchor the master canvas.",
  "type": "object",
  "required": [
    "schema", "mode", "jobSha256", "clip", "action", "profile", "master", "anchorMode", "keyColor",
    "referenceCanvas", "referenceScale", "referenceOffset", "baseSize", "baseAnchor", "padding", "sourceSize",
    "sourceAnchor", "video", "transform", "lock", "frames", "qa"
  ],
  "properties": {
    "schema": {"const": "video2dsprite.registration.v1"},
    "mode": {"const": "construction"},
    "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
    "jobSha256": {"$ref": "common.schema.json#/$defs/sha256"},
    "job": {"$ref": "common.schema.json#/$defs/fileRef"},
    "clip": {"type": "string", "minLength": 1},
    "action": {"type": "string", "minLength": 1},
    "profile": {"enum": ["actor", "fx"]},
    "master": {
      "allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}],
      "required": ["size", "anchor"],
      "properties": {
        "size": {"$ref": "common.schema.json#/$defs/size2"},
        "anchor": {"$ref": "common.schema.json#/$defs/point2"},
        "viewBox": {"$ref": "common.schema.json#/$defs/box"}
      }
    },
    "anchorMode": {"type": "string", "minLength": 1},
    "keyColor": {"$ref": "common.schema.json#/$defs/keyColor"},
    "referenceCanvas": {"$ref": "common.schema.json#/$defs/size2"},
    "referenceScale": {"type": "number", "exclusiveMinimum": 0},
    "referenceOffset": {"$ref": "common.schema.json#/$defs/point2"},
    "baseSize": {"$ref": "common.schema.json#/$defs/size2"},
    "baseAnchor": {"$ref": "common.schema.json#/$defs/point2"},
    "padding": {"$ref": "common.schema.json#/$defs/padding4"},
    "sourceSize": {"$ref": "common.schema.json#/$defs/size2"},
    "sourceAnchor": {"$ref": "common.schema.json#/$defs/point2"},
    "displayScale": {"type": "number", "exclusiveMinimum": 0},
    "masterGeometry": {"type": "object"},
    "video": {
      "type": "object",
      "required": ["size", "frameCount", "range"],
      "properties": {
        "size": {"$ref": "common.schema.json#/$defs/size2"},
        "frameCount": {"type": "integer", "minimum": 1},
        "range": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2},
        "frameDirectory": {"$ref": "common.schema.json#/$defs/relPath"}
      }
    },
    "transform": {
      "type": "object",
      "required": ["fit", "fitApplied", "videoScale", "videoOffset", "inverse", "resampler"],
      "properties": {
        "fit": {"enum": ["auto", "stretch", "cover", "contain"]},
        "fitApplied": {"enum": ["uniform", "stretch", "cover", "contain"]},
        "videoScale": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 2, "maxItems": 2},
        "videoOffset": {"$ref": "common.schema.json#/$defs/point2"},
        "anchorVideo": {"$ref": "common.schema.json#/$defs/point2"},
        "anchorOutput": {"$ref": "common.schema.json#/$defs/point2"},
        "inverse": {
          "type": "object",
          "required": ["scale", "offset"],
          "properties": {
            "scale": {
              "type": "array",
              "items": {"type": "number", "exclusiveMinimum": 0},
              "minItems": 2,
              "maxItems": 2
            },
            "offset": {"$ref": "common.schema.json#/$defs/point2"}
          }
        },
        "resampler": {"enum": ["box", "lanczos", "nearest"]}
      }
    },
    "lock": {
      "type": "object",
      "required": ["modes"],
      "properties": {
        "modes": {"type": "array", "items": {"enum": ["feet", "x", "hip"]}, "uniqueItems": true},
        "maxShift": {"type": ["integer", "null"], "minimum": 0},
        "groundMinRun": {"type": "integer", "minimum": 1},
        "targets": {"type": "object"}
      }
    },
    "rest": {
      "anyOf": [
        {"type": "null"},
        {
          "type": "object",
          "required": ["frames", "nativeFootBottom", "anchorError"],
          "properties": {
            "frames": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 0}},
            "nativeFootBottom": {"type": "number"},
            "anchorError": {"$ref": "common.schema.json#/$defs/point2"},
            "heightRatio": {"type": "number", "minimum": 0},
            "areaRatio": {"type": "number", "minimum": 0}
          }
        }
      ]
    },
    "nativeFootBottomRange": {
      "anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}]
    },
    "frames": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["file", "sha256", "sourceIndex", "shift"],
        "properties": {
          "file": {"$ref": "common.schema.json#/$defs/relPath"},
          "sha256": {"$ref": "common.schema.json#/$defs/sha256"},
          "sourceIndex": {"type": "integer", "minimum": 0},
          "sourceFile": {"type": "string", "minLength": 1},
          "sourceSha256": {"$ref": "common.schema.json#/$defs/sha256"},
          "shift": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
          "nativeFootBottom": {"type": ["number", "null"]}
        }
      }
    },
    "fx": {
      "type": "object",
      "properties": {
        "edgeFadePx": {"type": "number", "minimum": 0},
        "fadeRect": {"$ref": "common.schema.json#/$defs/box"},
        "fadeInFrames": {"type": "integer", "minimum": 0},
        "dissolveTailFrames": {"type": "integer", "minimum": 0}
      }
    },
    "review": {
      "type": "object",
      "properties": {
        "path": {"$ref": "common.schema.json#/$defs/relPath"},
        "frames": {"type": "array", "items": {"type": "integer", "minimum": 0}}
      }
    },
    "characterProfile": {"allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}]},
    "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
  }
}
```

**5.2 New `$defs.palette_repair_v1`**. Producer: B06 `register_clip.py palette-repair`. Consumer: agent review. Reason: a new document type needs its `schema` key (A0 section 5); it is a QA envelope plus the repair details.

```json
"palette_repair_v1": {
  "description": "palette-repair.json from register_clip.py palette-repair: a QA envelope plus the rule, regions, palette and per-frame changes. Alpha never changes.",
  "type": "object",
  "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
  "required": ["schema", "rule", "regions", "palette", "frames", "totalChangedPx", "alphaUnchanged"],
  "properties": {
    "schema": {"const": "video2dsprite.palette_repair.v1"},
    "rule": {"type": "object", "required": ["hueRange", "minSaturation", "minValue", "minAlpha"]},
    "regions": {"type": "array", "minItems": 1, "items": {"$ref": "common.schema.json#/$defs/box"}},
    "paletteSource": {"allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}]},
    "palette": {"type": "array", "minItems": 1, "items": {"$ref": "common.schema.json#/$defs/hexColor"}},
    "frames": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["file", "changedPx", "beforeSha256", "afterSha256"],
        "properties": {
          "file": {"$ref": "common.schema.json#/$defs/relPath"},
          "changedPx": {"type": "integer", "minimum": 0},
          "beforeSha256": {"$ref": "common.schema.json#/$defs/sha256"},
          "afterSha256": {"$ref": "common.schema.json#/$defs/sha256"}
        }
      }
    },
    "totalChangedPx": {"type": "integer", "minimum": 0},
    "alphaUnchanged": {"const": true}
  }
}
```

**5.3** Optional fields `prepare_i2v_input.py` writes; add to `$defs.registration_job_v1.properties`:

```json
{
  "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
  "actionKind": {"enum": ["loop", "oneshot", "hold", "fx"]},
  "returnsToRest": {"type": "boolean"},
  "sourceSize": {"$ref": "common.schema.json#/$defs/size2"},
  "sourceAnchor": {"$ref": "common.schema.json#/$defs/point2"},
  "anchorMode": {"type": "string", "minLength": 1},
  "referenceRoot": {"$ref": "common.schema.json#/$defs/point2"},
  "pixelArt": {"type": "boolean"},
  "placementResampler": {"enum": ["nearest", "lanczos"]},
  "keyChoice": {"type": "object"},
  "workRegionClipped": {"type": "array", "items": {"type": "number", "minimum": 0}, "minItems": 4, "maxItems": 4},
  "margin": {"type": "number", "minimum": 0},
  "durationS": {"type": "number", "exclusiveMinimum": 0},
  "calmSpanS": {"type": "number", "minimum": 0},
  "timeline": {
    "type": "array",
    "items": {
      "type": "object",
      "required": ["startS", "endS", "text"],
      "properties": {
        "startS": {"type": "number", "minimum": 0},
        "endS": {"type": "number", "exclusiveMinimum": 0},
        "text": {"type": "string", "minLength": 1}
      }
    }
  },
  "input": {
    "allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}],
    "properties": {"size": {"$ref": "common.schema.json#/$defs/size2"}}
  },
  "reviewGuide": {"allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}]},
  "lint": {"type": "array", "items": {"type": "object", "required": ["rule", "message"]}},
  "warnings": {"type": "array", "items": {"type": "string"}}
}
```

and to `$defs.registration_job_v1.properties.master.properties`:

```json
{"viewBox": {"$ref": "common.schema.json#/$defs/box"}, "sourceName": {"type": "string", "minLength": 1}}
```

**5.4** Optional fields `register_clip.py profile` writes; add to `$defs.character_profile_v1.properties`:

```json
{
  "nativeFootBottomRange": {"anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}]},
  "tool": {"$ref": "common.schema.json#/$defs/toolInfo"}
}
```

to `$defs.character_profile_v1.properties.registration.properties`:

```json
{
  "masterSha256": {"$ref": "common.schema.json#/$defs/sha256"},
  "referenceCanvas": {"$ref": "common.schema.json#/$defs/size2"},
  "fit": {"enum": ["auto", "stretch", "cover", "contain"]},
  "resampler": {"enum": ["box", "lanczos", "nearest"]},
  "groundMinRun": {"type": "integer", "minimum": 1},
  "restFootBottom": {"type": ["number", "null"]}
}
```

and to `$defs.character_profile_v1.properties.clips.additionalProperties.properties`:

```json
{
  "registration": {"$ref": "common.schema.json#/$defs/fileRef"},
  "jobSha256": {"$ref": "common.schema.json#/$defs/sha256"},
  "nativeFootBottom": {"type": ["number", "null"]}
}
```

**5.5** Optional fields of every `takes.jsonl` line (`register_clip.py qc`); add to `$defs.take_v1.properties`:

```json
{
  "job": {"$ref": "common.schema.json#/$defs/fileRef"},
  "action": {"type": "string", "minLength": 1},
  "warnings": {"type": "array", "items": {"type": "string", "minLength": 1}},
  "source": {
    "type": "object",
    "required": ["kind", "name", "frames", "size"],
    "properties": {
      "kind": {"enum": ["frames", "video"]},
      "frames": {"type": "integer", "minimum": 1},
      "size": {"$ref": "common.schema.json#/$defs/size2"},
      "sha256": {"$ref": "common.schema.json#/$defs/sha256"},
      "fps": {"type": "number", "exclusiveMinimum": 0}
    }
  },
  "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
}
```

## 6. Shared-helper promotion requests

- forge_core (integration): `register_clip._local_resample_anisotropic(pixels, scale_xy, resampler, anchor_src,
  anchor_dst, out_size)` with `_local_unpremultiply(planes)` (skills/video2dsprite/scripts/register_clip.py, "the
  transform" section). It is `forge_core.resample_rgba`'s anchor-pinned path with separate x and y scales (same
  premultiplied float planes, the same box filter for reductions of 2x or more, the same padded canvas). Request:
  let `resample_rgba` accept `scale` as a float or an `(sx, sy)` pair (nearest stays integer-only); then drop both
  private copies. Used only by `--fit stretch` with a provider that changed the aspect ratio. Tests:
  `test_anchor_error_within_half_pixel[stretch-1264]`.
- Optional, forge_core: `register_clip.ncc_search(template, image, x0, y0, search)` (zero-mean NCC with a +-search
  window, summed-area normalisation) and `pick_landmarks(luma, subject, count, patch, search)` (Shi-Tomasi patches
  with spacing). Dusk's scene `qa-registration.py` uses the same 36 px / +-15 px method for background plates, so
  B16 scene QA could share them. Tests: `test_qc_flags_push_in_and_keeps_locked_clip`.
- None for forge_matte: the key choice (`choose_key_color`), key estimate (`estimate_key`) and `KEY_TOLERANCE` are
  used as frozen.

## 7. Cross-module links that Z must add

- video2dsprite SKILL.md: the pipeline is key-plan (B05), `prepare_i2v_input.py prepare`, generate (host tool,
  generate2dmedia, or the CLI routes of B22), `register_clip.py qc`, triage and process (B05), `register_clip.py
  apply`, gait_loop select or retime (B07), package and verify (B08). Registration is by construction, not by
  bounding box; link references/prompt-rules.md for briefs. Add the section 2 rows.
- B05 (matte.md, `--matte-profile`): the profile's `matte` block is written by `register_clip.py profile` (mode, key,
  erode, unmix, despill; `params` optional). B05's frames-clean at the video's native size are `apply`'s input; any
  frame naming works (natural sort, `--pattern`). matte.md should link prompt-rules.md for the key choice.
- B08 (engine_export `package --registration`, pipeline.md): copy `{mode: "construction", jobSha256}` from
  registration.json into animation_v3 `registration`, take `sourceSize`/`sourceAnchor` (the padded canvas) and, for
  fx, `displayScale` from it; pipeline.md should name the padding contract in generate2dsprite's video-handoff.md.
- B07 (gait_loop, retime, animation-review.md): run on `apply`'s `frames/`; `registration.json` `frames[].sourceIndex`
  maps each registered frame back to the take. Keep the early calm span until retime cuts it.
- generate2dmedia and B22 docs: an image-to-video request sends the job folder's `input.png` and `prompt.txt`.
- generate2dsprite SKILL.md already routes to video-handoff.md; its row text can mention registration by
  construction.

## 8. Known limitations and what is not proven

- Thresholds come from the evidence, not from a corpus: rest anchor 2 px and height 3% (amber-quay: 2.2-4.8% scale
  pumping is visible), qc scale 3% and drift 2 source px (game-opus55: locked camera <= 1 px), landmark NCC 0.6 and
  identity 0.5 (game-opus55 takes: face >= 0.85 on idles, 0.59 on an ultimate), edge band 60% of the job margin with
  0.05% impure pixels, motion = luma change over 16 levels.
- Real-clip check (not a test; read-only evidence): the Ryo clip (512x512 input, 960x960 H.264, 145 frames). A job was
  rebuilt from its input with `prepare --canvas 512,512 --scale 1 --root <anchor> --margin 0` (input reproduced to a
  mean abs diff of 0.33). `qc` kept it: frame 0 matched 12/12 landmarks (NCC 0.998), identity 0.9997, scale 1.002,
  drift 0.0, no edge impurity; it warned that the take has no calm spans (its prompt predates the timeline contract)
  and that the end sits 9.9 px off (a running pose). `apply` on the cfed170 frames-clean: rest anchor error 0.0 px,
  height ratio 1.0, no overflow; `--lock feet,hip` put the feet on the anchor in every frame. Other providers, keys
  and art styles are synthetic-only.
- `--fit auto` assumes a centre crop when the provider changes the aspect ratio (hd2d MOTION-v2.md measured that for a
  1264x720 Grok reply with 12 landmarks). A provider that stretches or letterboxes needs `--fit stretch|contain`;
  `qc` (frame 0 scale and drift, landmarks) and `apply` (rest anchor and height) flag a wrong fit.
- qc sees only the subject on a flat key, so camera motion and subject motion are separated only in the calm spans:
  frame 0 and, for actions that return to rest, the last 0.4 s. A push-in that returns before the end, and camera
  motion in victory, defeat or fx takes, are not caught. Mid-clip identity is reported, not gated. The default
  identity box (top 35% of the subject, as wide as its top 20%) is a head heuristic; `--identity-box` overrides it.
- Rest checks, locks and foot lines use the 50% alpha contour, not the forge_core geometry threshold 16: a resampled
  body grows by about 1 px a side at alpha > 16 (3% on a 64 px subject), which would read as scale error. Edge and
  overflow checks keep alpha > 16. Locks pin to the registered rest frame (game-opus55), so resampling blur cannot
  shift every frame by a pixel; without rest frames they pin to the master.
- Speed on this Windows 11 machine (Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1): `apply` about 0.24 s
  per 960x960 frame (forge_core.resample_rgba about 0.09 s, the rest PNG I/O), 35 s for 145 frames, 42 s with
  `--lock feet,hip` (one registration per frame onto a canvas grown 6% along the locked axes); `qc` 6.3 s for 145
  frames. Not run: Linux, macOS, Python 3.10, Pillow 10.1, numpy 1.26.
- No provider was called: the templates follow the Dusk, hd2d and game-opus55 prompts that produced accepted takes,
  but their effect on any provider is not measured here.
- B05, B07 and B08 are not merged on this branch, so the full chain (process, apply, gait_loop, package) was not run;
  `apply` was exercised with synthetic perfect-matte frames and the cfed170 frames-clean of the Ryo clip.
- Deviations from the plan, with reasons:
  - `apply --profile actor|fx` is the processing profile the plan names; the character profile is inherited with
    `apply --character-profile <file>` because `--profile` was taken. The `profile` verb writes the character profile.
  - The plan's `sx = W/1280` is `--fit stretch`; the default `--fit auto` uses one factor (same aspect) or a centre
    crop (changed aspect), because the hd2d landmark check showed Grok crops 1264x720 rather than stretching.
  - Output conventions: `qc` appends to `takes.jsonl` (an append-only log like A4's ledger; a repeated take id is
    refused) instead of publishing a folder; `profile` writes one JSON file with `--output` (refused if it exists);
    `validate-profile` writes nothing. `apply`, `prepare` and `palette-repair` use staged `--output-dir` publishing.
  - `--rest-frames` are source indices, measured even outside `--range`, so a slice keeps the take's frame 0 as rest.
  - `palette-repair` generalises Dusk's boot-specific rule to a hue band with saturation, value and alpha floors,
    nearest master colour (RGB); its QA limit defaults to 0.6 of the region because flashes can cover most of it.
  - register_clip.py imports its sibling prepare_i2v_input.py (same skill) for the action templates, placement and
    key helpers; no cross-skill import.
- The job's `master.path` is a byte copy inside the job folder (`master.png`), so jobs never hold absolute paths; the
  original file name is kept as `master.sourceName`. registration.json refers to inputs by relative path or, across
  drives, by file name plus sha256.
