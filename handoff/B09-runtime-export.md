# B09-runtime-export: export_engine (Aseprite JSON, Godot 4 SpriteFrames/Sprite3D), forge-runtime.mjs, WebGL packed-alpha compositor

Branch `asf/B09-runtime-export` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files in this worktree:

- CLI: [skills/generate2dsprite/scripts/export_engine.py](../skills/generate2dsprite/scripts/export_engine.py) (B09-T1)
- Docs: [skills/generate2dsprite/references/engine-export.md](../skills/generate2dsprite/references/engine-export.md) (B09-T4)
- Runtime: [skills/video2dsprite/references/runtime/forge-runtime.mjs](../skills/video2dsprite/references/runtime/forge-runtime.mjs) (B09-T2)
- Compositor: [skills/video2dsprite/references/runtime/packed-alpha-webgl.mjs](../skills/video2dsprite/references/runtime/packed-alpha-webgl.mjs) and the thin wrapper [skills/video2dsprite/references/packed-alpha-runtime.js](../skills/video2dsprite/references/packed-alpha-runtime.js) (B09-T3)
- Tests: [tests/test_export_engine.py](../tests/test_export_engine.py) (19), [tests/test_runtime_js.py](../tests/test_runtime_js.py) (5 + 1 opt-in), [tests/js/forge-runtime.test.mjs](../tests/js/forge-runtime.test.mjs) (30, `node --test`)

## 1. CLIs

    python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target all --output-dir out/hero-engine
    python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target aseprite-json --output-dir out/hero-aseprite --sampling nearest
    python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target godot-spriteframes --output-dir out/hero-godot --godot-fps 12
    python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target godot-sprite3d --output-dir out/hero-3d --world-height 1.7 --billboard fixed-y

- One verb, no subcommands. `--target` is `aseprite-json`, `godot-spriteframes`, `godot-sprite3d` or `all` (default). Other flags: `--name`, `--max-atlas-size` (16..4096, default 4096), `--padding` (2), `--extrude` (1), `--sampling auto|nearest|linear`, `--godot-fps auto|N`, `--world-height` or `--pixel-size`, `--subject-height-px`, `--reference-clip`, `--billboard enabled|fixed-y|disabled`, `--ppu`, `--camera-pitch-deg`.
- Input is the built `animation-clips.json` (schema `generate2dsprite.animation_clips.v1` or `.v2`). A clips input manifest is refused with a pointer to `build_animation_clips.py`.
- `--help` is ASCII and exits 0 under cp1252 and cp950 (`test_help_under_cp1252_and_cp950`).
- Success prints one ASCII JSON line: `status` (`pass` or `warn`), `output`, `metadata` (the `engine-export.json` path), `targets`, `clips`, `frames`, `pages`. Errors print `error: ...` to stderr and exit 1; argparse usage errors keep argparse's exit 2 (as `tools/vendor_sync.py` does).
- Publication: everything is written in `forge_core.staged_output()`, every output is parsed back (round trip), then the directory is published. An existing output directory is refused; a stale frame (rgba hash mismatch), an impossible atlas or a failed round trip publishes nothing.

Runtime files (no CLI; ES modules, Node 22 for the tests):

    node --test tests/js/forge-runtime.test.mjs

## 2. SKILL.md routing rows

generate2dsprite:

| Need | Route |
|---|---|
| Hand built clips to an engine (Aseprite JSON for Phaser, PixiJS and Unity/Godot importers; Godot 4 SpriteFrames; Godot Sprite3D) | `scripts/export_engine.py --clips <animation-clips.json> --target all --output-dir <new-dir>`; then import by hand once per engine, see `references/engine-export.md` |
| Godot Sprite3D package with relative paths (S19) | `scripts/export_engine.py ... --target godot-sprite3d --world-height <units>`; open `godot-sprite3d/<name>.tscn` |

video2dsprite:

| Need | Route |
|---|---|
| Play packed-alpha MP4 (iPhone/Safari) in a browser game | `references/packed-alpha-runtime.js` (`createPackedAlphaDrawable`, `drawAnchoredFrame`) over `references/runtime/packed-alpha-webgl.mjs`: one shared WebGL context, CPU fallback, context-loss recovery; draw `lease.drawable`, control `lease.video` |
| Distance-driven walks, multi-hit time-warp, hit-stop, fixed step, dither transitions | `references/runtime/forge-runtime.mjs` (DOM-free; reads animation.json 2.0/3.0, animation-clips.json and engine-export.json clips) |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `generate2dsprite/scripts/export_engine.py` | Exports built clips to an Aseprite JSON atlas (frameTags, pivot slice, events), Godot 4 SpriteFrames + AnimatedSprite2D scene, and an AnimatedSprite3D package with relative-path Sprite3D contracts; writes the engine mapping table | `tests/test_export_engine.py`: parse-level round trips (exact pixels, durations, tags, events, pivot, relative paths); engine imports NOT verified |
| `video2dsprite/references/runtime/forge-runtime.mjs` | DOM-free playback helpers: frameAt, events, distance-driven walk, mapActionTime, clipK/clipLoop, FixedStepLoop, HitStopClock, dither dissolve, integer-scale snapping | `tests/js/forge-runtime.test.mjs` via `tests/test_runtime_js.py` (node marker) |
| `video2dsprite/references/runtime/packed-alpha-webgl.mjs` | Shared-context WebGL compositor for packed-alpha video: half-texel seam clamp, alpha snap, premultiplied output, reused buffers, context-loss recovery, CPU fallback, optional rim/ink outline | Node tests with fake canvases and a fake WebGL; parity with `forge_av.unpack_packed_alpha`; opt-in headless Edge/Chrome check (`FORGE_BROWSER`) |

README note (plan): engine imports are unverified until done manually.

## 4. CHANGELOG entries

- Added: `export_engine.py` with targets `aseprite-json`, `godot-spriteframes`, `godot-sprite3d`, `all`; atlas pages of at most 4096 px with padding, extrude and duplicate-frame sharing; `engine-export.json` with per-clip timing, events, transition hints and the Godot/Unity/Phaser/PixiJS/pitch-compensation mapping table; built-in round-trip QA (B09-T1; roadmap P1-6).
- Added: `references/engine-export.md`: targets, mapping table, manual import checklist, runtime usage (distance-driven walk, transitions, hit-stop, time-warp, integer-scale snapping, sampling) (B09-T4).
- Added: `video2dsprite/references/runtime/forge-runtime.mjs`: `walkPlayback`/`gaitFrame` with a shared distance-driven phase, `TravelMeter`, `phaseOffset`/`entryPhase`, `mapActionTime` for multi-hit actions, `clipK`/`clipLoop`, `frameAt`/`eventsCrossed`, `FixedStepLoop` with interpolation, `HitStopClock`, `ditherDissolve` and premultiplied mixes, `normalizeAnimation` for animation.json 2.0 and 3.0 (B09-T2).
- Added: `video2dsprite/references/runtime/packed-alpha-webgl.mjs`: WebGL packed-alpha compositor (B09-T3).
- Changed: `packed-alpha-runtime.js` is now a thin wrapper over the WebGL compositor (CPU Canvas2D readback only as the fallback); `createPackedAlphaDrawable(video, metadata)` and `drawAnchoredFrame(ctx, drawable, clip, x, y, scale)` keep their signatures and gain optional `options`, `lease` and `release()` (B09-T3).
- Changed: packed-alpha playback reads the 3.0 halves: RGB is cropped at (0, 0, width, height) and alpha at (halfWidth, 0, width, height), the video must be (2 x halfWidth) x halfHeight; 2.0 metadata (no halves) still works.
- BREAKING: none (new tools; the wrapper API is kept).
- Fixed: S19 (Sprite3D contract frame names did not resolve relative to the contract: export_engine writes relative frame paths and a relative bundle); DOC-13 (Aseprite tag and Godot SpriteFrames timing were documented without a tool); odd-width packed videos (403 -> 404 halves) were cropped at `width` instead of `halfWidth` by the old runtime.

## 5. Schema change requests

### 5.1 New sprite $defs (producer: B09 export_engine; consumers: games, B19 rig_animate optional Sprite3D, B01 build-godot-bundle)

`engine-export.json`, the Sprite3D contract and its bundle have no contract yet (`generate2dsprite.godot_sprite3d.v1` exists only in `generate2dsprite.py:733-857`). Add these to `shared/schemas/sprite.schema.json` `$defs` (JSON pointer `/$defs/<name>`). `tests/test_export_engine.py` (`PROPOSED_SPRITE_DEFS`, `assert_valid_proposed`) validates every document export_engine writes against the frozen schemas plus exactly this text:

```json
{
  "exportedClip": {
    "description": "A clip in engine-export.json: source frames, integer durations, events, per-target placement.",
    "type": "object",
    "required": ["frames", "duration_ms", "total_duration_ms", "loop", "loop_policy", "events_ms", "page"],
    "properties": {
      "frames": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 0}},
      "duration_ms": {"$ref": "common.schema.json#/$defs/durationsMs"},
      "total_duration_ms": {"type": "integer", "minimum": 1},
      "loop": {"type": "boolean"},
      "loop_policy": {"$ref": "common.schema.json#/$defs/loopPolicy"},
      "events_ms": {
        "type": "array",
        "items": {
          "type": "object",
          "required": ["name", "at_ms", "at"],
          "properties": {
            "name": {"$ref": "common.schema.json#/$defs/eventName"},
            "at_ms": {"type": "integer", "minimum": 0},
            "at": {"type": "integer", "minimum": 0},
            "data": true
          }
        }
      },
      "page": {"type": "integer", "minimum": 0},
      "transitions": {"type": "array", "items": {"$ref": "#/$defs/clipTransition"}},
      "aseprite": {
        "type": "object",
        "required": ["file", "from", "to"],
        "properties": {
          "file": {"$ref": "common.schema.json#/$defs/relPath"},
          "from": {"type": "integer", "minimum": 0},
          "to": {"type": "integer", "minimum": 0}
        }
      },
      "godot": {
        "type": "object",
        "required": ["speed", "relative_durations"],
        "properties": {
          "speed": {"type": "number", "exclusiveMinimum": 0},
          "relative_durations": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}},
          "basis": {"type": "string"}
        }
      },
      "sprite3d": {
        "type": "object",
        "required": ["action", "contract"],
        "properties": {
          "action": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$"},
          "contract": {"$ref": "common.schema.json#/$defs/relPath"}
        }
      }
    }
  },
  "engine_export_v1": {
    "description": "engine-export.json (export_engine.py): files, clip timing, mapping table, QA. Paths are relative.",
    "type": "object",
    "required": [
      "schema",
      "name",
      "tool",
      "source",
      "targets",
      "frame_size",
      "anchor_px",
      "sampling",
      "atlas",
      "clips",
      "mapping",
      "files",
      "qa"
    ],
    "properties": {
      "schema": {"const": "generate2dsprite.engine_export.v1"},
      "name": {"type": "string", "pattern": "^[A-Za-z0-9_-]+$"},
      "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
      "source": {
        "allOf": [{"$ref": "common.schema.json#/$defs/fileRef"}],
        "properties": {
          "schema": {"enum": ["generate2dsprite.animation_clips.v1", "generate2dsprite.animation_clips.v2"]}
        }
      },
      "targets": {
        "type": "array",
        "minItems": 1,
        "items": {"enum": ["aseprite-json", "godot-spriteframes", "godot-sprite3d"]}
      },
      "frame_size": {"$ref": "common.schema.json#/$defs/size2"},
      "anchor_px": {"$ref": "common.schema.json#/$defs/point2"},
      "sampling": {"$ref": "common.schema.json#/$defs/sampling"},
      "default_clip": {"type": "string", "minLength": 1},
      "atlas": {
        "type": "object",
        "required": ["max_size", "padding", "extrude", "pages"],
        "properties": {
          "max_size": {"type": "integer", "minimum": 16, "maximum": 4096},
          "padding": {"type": "integer", "minimum": 0},
          "extrude": {"type": "integer", "minimum": 0},
          "pages": {
            "type": "array",
            "minItems": 1,
            "items": {
              "type": "object",
              "required": ["index", "size", "cells", "clips"],
              "properties": {
                "index": {"type": "integer", "minimum": 0},
                "size": {"$ref": "common.schema.json#/$defs/size2"},
                "cells": {"type": "integer", "minimum": 1},
                "clips": {"type": "array", "items": {"type": "string"}}
              }
            }
          }
        }
      },
      "scale": {
        "type": "object",
        "required": ["pixel_size", "subject_height_px", "world_height"],
        "properties": {
          "pixel_size": {"type": "number", "exclusiveMinimum": 0},
          "subject_height_px": {"type": "number", "exclusiveMinimum": 0},
          "world_height": {"type": "number", "exclusiveMinimum": 0}
        }
      },
      "clips": {
        "type": "object",
        "minProperties": 1,
        "additionalProperties": {"$ref": "#/$defs/exportedClip"}
      },
      "states": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1}},
      "unused_frames": {"type": "array", "items": {"type": "integer", "minimum": 0}},
      "mapping": {
        "type": "object",
        "required": ["anchor_px", "frame_size", "godot", "unity", "phaser", "pitch_compensation"]
      },
      "files": {"type": "array", "items": {"$ref": "common.schema.json#/$defs/fileRef"}},
      "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
    }
  },
  "godot_sprite3d_v1": {
    "description": "Godot Sprite3D contract; frames are relative to this file (S19); durations_ms is exact per frame.",
    "type": "object",
    "required": [
      "schema",
      "frame_size",
      "output_origin",
      "sprite3d_offset",
      "world_height",
      "recommended_pixel_size",
      "duration_ms",
      "fps",
      "frames"
    ],
    "properties": {
      "schema": {"const": "generate2dsprite.godot_sprite3d.v1"},
      "clip": {"type": "string", "minLength": 1},
      "frame_size": {"$ref": "common.schema.json#/$defs/size2"},
      "output_origin": {"$ref": "common.schema.json#/$defs/point2"},
      "sprite3d_offset": {"$ref": "common.schema.json#/$defs/point2"},
      "reference_subject_height_px": {"type": "number", "exclusiveMinimum": 0},
      "world_height": {"type": "number", "exclusiveMinimum": 0},
      "recommended_pixel_size": {"type": "number", "exclusiveMinimum": 0},
      "rendered_subject_height_world": {"type": "number", "exclusiveMinimum": 0},
      "scale_source": {"type": "string"},
      "billboard": {"enum": ["enabled", "disabled", "fixed-y"]},
      "texture_filter": {"$ref": "common.schema.json#/$defs/sampling"},
      "duration_ms": {"type": "integer", "minimum": 1},
      "fps": {"type": "number", "exclusiveMinimum": 0},
      "frames": {"type": "array", "minItems": 1, "items": {"$ref": "common.schema.json#/$defs/relPath"}},
      "frame_sha256": {"type": "array", "items": {"$ref": "common.schema.json#/$defs/sha256"}},
      "durations_ms": {"$ref": "common.schema.json#/$defs/durationsMs"},
      "loop": {"type": "boolean"},
      "loop_policy": {"$ref": "common.schema.json#/$defs/loopPolicy"},
      "events_ms": {"type": "array"}
    }
  },
  "godot_sprite3d_bundle_v1": {
    "description": "Per-action Sprite3D contracts combined; contract paths are relative to this file.",
    "type": "object",
    "required": ["schema", "default_action", "world_height", "pixel_size", "actions"],
    "properties": {
      "schema": {"const": "generate2dsprite.godot_sprite3d_bundle.v1"},
      "default_action": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$"},
      "world_height": {"type": "number", "exclusiveMinimum": 0},
      "world_height_max_drift": {"type": "number", "minimum": 0},
      "pixel_size": {"type": "number", "exclusiveMinimum": 0},
      "pixel_size_max_drift": {"type": "number", "minimum": 0},
      "actions": {
        "type": "object",
        "minProperties": 1,
        "propertyNames": {"pattern": "^[a-z0-9][a-z0-9_-]*$"},
        "additionalProperties": {
          "type": "object",
          "required": ["contract", "loop"],
          "properties": {
            "contract": {"$ref": "common.schema.json#/$defs/relPath"},
            "loop": {"type": "boolean"},
            "clip": {"type": "string"}
          }
        }
      }
    }
  }
}
```

Document ids: `generate2dsprite.engine_export.v1`, `generate2dsprite.godot_sprite3d.v1`, `generate2dsprite.godot_sprite3d_bundle.v1`. The v1 Sprite3D fields are what `generate2dsprite.py process --godot-world-height` writes today; export_engine adds the optional `clip`, `texture_filter`, `frame_sha256`, `durations_ms`, `loop`, `loop_policy`, `events_ms` and, per bundle action, `clip`. The bundle's `contract` must be relative (the current `cmd_build_godot_bundle` falls back to an absolute path: B01-T6 / S19).

### 5.2 animation_v3 packed geometry (owner A0; producer B08; consumers B09 runtime, games)

The hand-written example `tests/fixtures/contracts/video.animation_v3.valid.json` declares `packedAlpha` `width 48, height 24, halfWidth 24, halfHeight 24`. forge_av (`packed_geometry`, `encode_packed_alpha`) defines `width`/`height` as the logical frame and `halfWidth`/`halfHeight` as the even halves, so `width <= halfWidth` always; a runtime following that example would crop alpha in the wrong place, and `forge-runtime.mjs` refuses it (`normalizeAnimation refuses packed geometry ...` test). Requests:

- Fix the example: `"width": 24, "height": 24` in `packedAlpha`; in `mobilePackedAlpha[0]` use the tier's logical `width`/`height` (24, 24) and add `"halfWidth": 24, "halfHeight": 24`.
- Document the semantics in `/$defs/animation_v3/properties/packedAlpha/description`: append "width/height are the logical frame (at most halfWidth/halfHeight); 2.0 has no halves and its width/height are the half size."
- Add optional tier fields at `/$defs/animation_v3/properties/mobilePackedAlpha/items/properties`:

```json
"layout": {"type": "string", "minLength": 1},
"halfWidth": {"type": "integer", "minimum": 1},
"halfHeight": {"type": "integer", "minimum": 1}
```

JSON Schema cannot express `width <= halfWidth`; B08 `validate_animation` should check it.

## 6. Shared-helper promotion requests

- `_local_round_half_up(value) -> int` at `skills/generate2dsprite/scripts/export_engine.py:77`: forge_core already has the private `_round_half_up` (`shared/forge_core.py:716`); publish it as `forge_core.round_half_up` (Appendix A addition) so writers stop copying it. Covered by the pivot-rounding test.
- `_local_file_ref(path, base) -> {path, sha256, bytes}` at `export_engine.py:871`: a fileRef relative to `base` that records only the file name when `portable_path` has no relative route (another drive), as A1 section 5 requires of every manifest writer. Proposed as `forge_core.file_ref(path, base)`; test `test_file_refs_never_record_absolute_paths`.
- Godot 4 text resources: `spriteframes_text` (:389), `scene_text` (:414), `parse_godot_resource` (:488), `_gd_number` (:372), `_gd_string` (:381). If B14's Godot 4 map exporter writes `.tres`/`.tscn` too, move these into one shared helper (not part of Appendix A today; integration decides).
- JS: none. `splitDurations` in forge-runtime.mjs mirrors `forge_core.frame_durations` and is parity-tested against it.

## 7. Cross-module links that Z must add

- generate2dsprite SKILL.md: route "engine handoff" to `scripts/export_engine.py` and link `references/engine-export.md`.
- generate2dsprite `references/processing.md` (B01), section "Godot Sprite3D (optional)": point to engine-export.md for SpriteFrames, Aseprite JSON and the relative-path Sprite3D package.
- generate2dsprite `references/runtime-integration.md` and `frames-and-clips.md` (B02): link engine-export.md; name the video2dsprite runtime (`references/runtime/forge-runtime.mjs` in that skill) in plain text, since skills install separately.
- video2dsprite SKILL.md ("Runtime and acceptance") and `references/pipeline.md` (B08): replace "Canvas2D reconstruction helper" with the WebGL compositor; link `references/runtime/forge-runtime.mjs` and `references/runtime/packed-alpha-webgl.mjs`; the existing pipeline.md example still runs unchanged (same API); say "draw `lease.drawable`, never the packed video; control playback with `lease.video`"; serve `.mjs` with a JavaScript MIME type.
- B02 (built clips v2): export_engine passes through `keys`, `entry_frame`, `stride_world_units`, `stride_px_per_frame`, `cadence_ms`, `speed_ref`, `hitstop_ticks`, `role`, `tick_grid`, `ticks`, `tick_hz`, top-level `sampling`, `pixel_art`, `body_height_px`, `art_source`, `placeholder`, `shadow`, and reads transition hints from `transitions` items that have `to` (v1 `transitions` metrics are ignored). Please carry `ticks`/`tick_hz` and the hints into the built v2 clips (Godot then plays exact tick timing). If the hints move to another key, tell integration; `forge-runtime.mjs` also accepts `transition_hints`.
- B08 (animation.json 3.0): keep forge_av semantics for `packedAlpha` (logical width/height, physical halves) and give each `mobilePackedAlpha` tier `halfWidth`/`halfHeight` plus `sourceWidth`/`sourceHeight` = the size the tier was scaled from: `forge-runtime.mjs` `drawableRegion` scales `contentSize` by `width / sourceWidth`. List pingpong selections expanded in `sourceIndices`/`durationsMs` (the runtime treats manifests as expanded).
- B19 (rig_animate "optional Sprite3D"): can call `export_engine.py --target godot-sprite3d` on its built clips instead of writing Sprite3D data itself.
- Integration e2e (Appendix I, pipelines 1 and 3): `export_engine.py --target all` on the built clips; the result validates with section 5.1.
- A0/Z: fix `video.animation_v3.valid.json` (section 5.2).
- Z CONTRIBUTING/CI: Node 22 runs `tests/js` through `tests/test_runtime_js.py` (node marker); the WebGL check runs only with `FORGE_BROWSER=<chrome or edge executable>` and is never part of STD.

## 8. Known limitations and what is not proven

- **No editor or engine import was run** (Aseprite, Godot 4, Phaser, PixiJS, Unity). The tests prove parse-level round trips only. Format facts taken from documentation and engine source knowledge, each needing one manual import: Phaser `createFromAseprite` looks frames up by `{frame}` names ("0", "1", ...); Aseprite 1.3 writes a tag's `repeat` as a string; Godot's text loader resolves a relative `ext_resource` path against the resource file; the Godot enum values used (`texture_filter` 1/2 in 2D and 0/3 in 3D, `billboard` 0/1/2); AtlasTexture `filter_clip`.
- **Atlas**: frames are not trimmed or rotated; a clip's distinct frames must fit one page (a 4096 page holds 9 frames of 1024 px); clips never span pages because an Aseprite tag addresses one image.
- **Input**: only built manifests. B02's v2 builder output did not exist yet; v2 was tested with hand-written manifests that validate as `sprite/animation_clips_v2`, and one test runs the current (v1) builder end to end.
- **Godot**: SpriteFrames cannot carry events (they are in engine-export.json and the Aseprite tag data). Import settings (Lossless, mipmaps) are not written. Relative ext_resource paths become `res://` paths when Godot re-saves the resource.
- **WebGL**: verified only through the opt-in check in headless Edge 154 and Chrome 154 on SwiftShader (no GPU), with a canvas standing in for the video element: GPU mode, outline shader, alpha exact after the snap, colour within 2 at alpha 64 (8-bit premultiplied canvas quantization), real `WEBGL_lose_context` loss and restore. Not run: a hardware GPU, Safari/iOS, a real `<video>` element, `requestVideoFrameCallback` in a browser, mobile memory budgets. That check found and fixed a real bug: snapping at exactly 2/255 and 253/255 was unreliable on the GPU, so the thresholds are now 2.5/255 and 252.5/255 (same 8-bit semantics).
- **CPU fallback**: tested with fake canvases in Node; it reads pixels back every frame (slow, correct) and skips the outline (`stats.outlineSkipped`).
- **Runtime**: `normalizeClip`/`normalizeAnimation` treat manifests as pingpong-expanded unless `{pingpongExpanded: false}`; 2.0 manifests get exact integer durations split from frameCount / fps. `walkPlayback` reports `paused` instead of driving `playbackRate` to 0. The outline colours follow an owner prototype and are not tuned.
- **Platforms**: Windows 11, Python 3.13.2, Node 22.15, Pillow 12.3, numpy 2.5 only. `node --check` of the `.js` wrapper needs Node 22.7+ module detection; the test retries older Node with an `.mjs` copy.
- **Deviations from the plan** (additions, nothing dropped): the `godot-sprite3d` target also writes an AnimatedSprite3D scene and a per-frame SpriteFrames next to the v1 contracts; `engine-export.json` is a new document type (section 5.1); the runtime adds `TravelMeter`, `eventsCrossed`, `anchoredRect`, `fallbackCell`, `pickPackedTransport` and `drawableRegion` beyond the plan's list; the plan's `phaseOffset` is `phaseOffset(distance, loopDistance, targetPhase)` with `entryPhase(clip)` for entry frames.
