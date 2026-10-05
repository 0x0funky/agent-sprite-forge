# B09-runtime-export: export_engine (Aseprite JSON, Godot 4 SpriteFrames/Sprite3D), forge-runtime.mjs, WebGL packed-alpha compositor

Branch `asf/B09-runtime-export` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files in this worktree:

> **Integration (2026-10-05, group pass "runtime-media", branch `asf/int-g-runtime-media`).** Resolved: the B02 cross-module
> blockers (D12 event positions and Godot ticks, D13 transition hints), the schema requests of section 5 (applied
> by the shared stage, D12, D13, D21), the helper promotions of section 6 (D30), the reviewer's non-blocking
> items (sprite3d-only atlas, faint-FX scale, states and event-name validation, the D21 fixture test) and the
> D26-D29 conventions. Sections 1-8 below are current; open items for Z are marked **Z**.

- CLI: [skills/generate2dsprite/scripts/export_engine.py](../skills/generate2dsprite/scripts/export_engine.py) (B09-T1)
- Docs: [skills/generate2dsprite/references/engine-export.md](../skills/generate2dsprite/references/engine-export.md) (B09-T4)
- Runtime: [skills/video2dsprite/references/runtime/forge-runtime.mjs](../skills/video2dsprite/references/runtime/forge-runtime.mjs) (B09-T2)
- Compositor: [skills/video2dsprite/references/runtime/packed-alpha-webgl.mjs](../skills/video2dsprite/references/runtime/packed-alpha-webgl.mjs) and the thin wrapper [skills/video2dsprite/references/packed-alpha-runtime.js](../skills/video2dsprite/references/packed-alpha-runtime.js) (B09-T3)
- Tests: [tests/test_export_engine.py](../tests/test_export_engine.py) (26), [tests/test_runtime_js.py](../tests/test_runtime_js.py) (6 + 1 opt-in), [tests/js/forge-runtime.test.mjs](../tests/js/forge-runtime.test.mjs) (32, `node --test`)

## 1. CLIs

    python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target all --output-dir out/hero-engine
    python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target aseprite-json --output-dir out/hero-aseprite --sampling nearest
    python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target godot-spriteframes --output-dir out/hero-godot --godot-fps 12
    python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target godot-sprite3d --output-dir out/hero-3d --world-height 1.7 --billboard fixed-y

- One verb, no subcommands. `--target` is `aseprite-json`, `godot-spriteframes`, `godot-sprite3d` or `all` (default). Other flags: `--name`, `--max-atlas-size` (16..4096, default 4096), `--padding` (2), `--extrude` (1), `--sampling auto|nearest|linear`, `--godot-fps auto|N`, `--world-height` or `--pixel-size`, `--subject-height-px`, `--reference-clip`, `--billboard enabled|fixed-y|disabled`, `--ppu`, `--camera-pitch-deg`.
- Input is the built `animation-clips.json` (schema `generate2dsprite.animation_clips.v1` or `.v2`), read BOM-tolerant and strict (no NaN, Infinity or duplicate keys; D28). A clips input manifest is refused with a pointer to `build_animation_clips.py`.
- Positions follow `sprite.schema.json` builtClip (D12): `frames`/`duration_ms` are the played timeline (pingpong expanded, `authored_frames` authored); `events_ms[].at_ms` is authoritative, `position` (played frame) is taken or derived from `at_ms` and must agree, `at` is the authored position and must agree through `authored_frames` and the loop policy. `ticks`, `keys`, `entry_frame` stay authored; Godot plays the authored ticks mapped onto the played frames. Transition hints come from `transition_hints`, else legacy `transitions` items with a string `to` (D13); they are written as `transition_hints`. States, hint targets and event names (`common/eventName`) are validated.
- `--help` is ASCII and exits 0 under cp1252 and cp950 (`test_help_under_cp1252_and_cp950`).
- Success prints one ASCII JSON line: `status` (`pass` or `warn`), `output`, `metadata` (the `engine-export.json` path), `targets`, `clips`, `frames`, `pages`. Errors print `error: ...` to stderr and exit 1; argparse usage errors exit 2 (D26); anything unexpected prints `error: internal error (<Type>: <message>)` and exits 1 through `forge_core.run_cli` (D27).
- Atlas pages are planned only for `aseprite-json`/`godot-spriteframes`: a `godot-sprite3d`-only export has no page limit (`atlas.pages` is `[]`, each clip's `page` is 0). The subject height is measured only for `godot-sprite3d` or `--world-height`; faint FX exported to the atlas targets get no `scale` block.
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
- Fixed (integration, D12): events of built pingpong clips are placed on the played frame shown at their `at_ms` (the second `step_l` of `0 1 2 1` sits on frame 3, not 1) in the Aseprite tag data, Sprite3D contracts, engine-export.json (`position` beside the authored `at`) and `forge-runtime.mjs`; a `position`/`frame`/`at` that contradicts `at_ms` is refused. Godot keeps exact tick timing for pingpong clips built from ticks (authored ticks mapped onto the played frames).
- Fixed (integration, D13): transition hints written by build_animation_clips v2 under `transition_hints` were dropped by export_engine and the runtime; both now read `transition_hints` first and fall back to legacy `transitions` items with `to`. engine-export.json writes `transition_hints`.
- Fixed (integration, reviewer notes): `--target godot-sprite3d` alone no longer fails on the atlas page limit; aseprite/SpriteFrames exports of faint FX no longer need `--subject-height-px`; a non-string state or unknown event name is a clean `error:` line instead of a traceback.
- Changed (integration): engine-export.json clips also carry `authored_frames`, `keys_ms`, `entry_ms` and `hitstop_ms`; QA `tool.version` is the package version `0.4.0` (D29).

## 5. Schema change requests

### 5.1 New sprite $defs: resolved (shared stage, per D12, D13)

`exportedClip`, `engine_export_v1`, `godot_sprite3d_v1` and `godot_sprite3d_bundle_v1` are in `shared/schemas/sprite.schema.json` (shared-stage commit f3d7eb2), adapted per D12/D13: `events_ms[].position`, `transition_hints` (with the pre-D13 `transitions` hint list still accepted) and `atlas.pages` allowed empty for a Sprite3D-only export. `tests/test_export_engine.py` validates every document export_engine writes against that real schema (`assert_valid_contract`); the in-memory `PROPOSED_SPRITE_DEFS` copy is gone.

### 5.2 animation_v3 packed geometry: resolved (shared stage, per D21)

The contract example `tests/fixtures/contracts/video.animation_v3.valid.json` uses forge_av's logical `width`/`height` with physical halves, and `tests/js/forge-runtime.test.mjs` changed with it in the same commit (reviewer note on line 117). B08 `validate_animation` enforcing `width <= halfWidth` is the video group's (D21).

### 5.3 Follow-ups for the schema owner (requests)

- `exportedClip.events_ms[].position`: export_engine now always writes it; it can become required.
- `exportedClip.transitions` (the pre-D13 hint list): export_engine writes `transition_hints` only; the legacy form can be dropped from the exported-clip schema (readers keep the D13 fallback).
- `exportedClip.page` is required, but a `godot-sprite3d`-only export has no atlas pages; export_engine writes `0` there until `page` is made optional (or absent) when `atlas.pages` is empty.

## 6. Shared-helper promotion requests

- Resolved (D30): `_local_round_half_up` and `_local_file_ref` are gone; export_engine uses `forge_core.round_half_up`, `forge_core.file_ref` (another drive keeps only the file name), `forge_core.read_json` (D28), `forge_core.run_cli` (D27) and `forge_core.FORGE_PACKAGE_VERSION` (D29).
- Godot 4 text resources in `export_engine.py`: `spriteframes_text` (:498), `scene_text` (:523), `parse_godot_resource` (:597), `_gd_number` (:481), `_gd_string` (:490). If B14's Godot 4 map exporter writes `.tres`/`.tscn` too, move these into one shared helper (not part of Appendix A today; integration decides).
- JS: none. `splitDurations` in forge-runtime.mjs mirrors `forge_core.frame_durations` and is parity-tested against it.

## 7. Cross-module links that Z must add

- generate2dsprite SKILL.md: route "engine handoff" to `scripts/export_engine.py` and link `references/engine-export.md`.
- generate2dsprite `references/processing.md` (B01), section "Godot Sprite3D (optional)": point to engine-export.md for SpriteFrames, Aseprite JSON and the relative-path Sprite3D package.
- generate2dsprite `references/runtime-integration.md` and `frames-and-clips.md` (B02): link engine-export.md; name the video2dsprite runtime (`references/runtime/forge-runtime.mjs` in that skill) in plain text, since skills install separately.
- video2dsprite SKILL.md ("Runtime and acceptance") and `references/pipeline.md` (B08): replace "Canvas2D reconstruction helper" with the WebGL compositor; link `references/runtime/forge-runtime.mjs` and `references/runtime/packed-alpha-webgl.mjs`; the existing pipeline.md example still runs unchanged (same API); say "draw `lease.drawable`, never the packed video; control playback with `lease.video`"; serve `.mjs` with a JavaScript MIME type.
- B02 (built clips v2): resolved (D12, D13). export_engine and `forge-runtime.mjs` read B02's as-built format: `transition_hints`, `events_ms[].position` with `at` authored, authored `ticks` mapped onto the played frames; they pass through `keys`, `keys_ms`, `entry_frame`, `entry_ms`, `stride_world_units`, `stride_px_per_frame`, `cadence_ms`, `speed_ref`, `hitstop_ticks`, `hitstop_ms`, `role`, `tick_grid`, `ticks`, `tick_hz`, `authored_frames`, top-level `sampling`, `pixel_art`, `body_height_px`, `art_source`, `placeholder`, `shadow`. Tests feed real `build_animation_clips.py` v2 output (`test_real_builder_v2_events_ticks_and_hints`, `test_runtime_reads_real_builder_and_export_output`).
- B08 (animation.json 3.0): resolved for the fixture (D21, shared stage). Keep forge_av semantics for `packedAlpha` (logical width/height, physical halves) and give each `mobilePackedAlpha` tier `halfWidth`/`halfHeight` plus `sourceWidth`/`sourceHeight` = the size the tier was scaled from: `forge-runtime.mjs` `drawableRegion` scales `contentSize` by `width / sourceWidth`. List pingpong selections expanded in `sourceIndices`/`durationsMs`; event `frame` values must agree with `atMs` (the runtime refuses a contradiction, as `validate_animation` does).
- B19 (rig_animate "optional Sprite3D"): can call `export_engine.py --target godot-sprite3d` on its built clips instead of writing Sprite3D data itself.
- Integration e2e (Appendix I, pipelines 1 and 3): `export_engine.py --target all` on the built clips; the result validates as `sprite/engine_export_v1`.
- **Z**: README/SKILL.md wording above; `pipeline.md` (B08) must say that consumers who copied only `packed-alpha-runtime.js` now also need the `runtime/` folder; CHANGELOG entries of section 4.
- Z CONTRIBUTING/CI: Node 22 runs `tests/js` through `tests/test_runtime_js.py` (node marker); the WebGL check runs only with `FORGE_BROWSER=<chrome or edge executable>` and is never part of STD.

## 8. Known limitations and what is not proven

- **No editor or engine import was run** (Aseprite, Godot 4, Phaser, PixiJS, Unity). The tests prove parse-level round trips only. Format facts taken from documentation and engine source knowledge, each needing one manual import: Phaser `createFromAseprite` looks frames up by `{frame}` names ("0", "1", ...); Aseprite 1.3 writes a tag's `repeat` as a string; Godot's text loader resolves a relative `ext_resource` path against the resource file; the Godot enum values used (`texture_filter` 1/2 in 2D and 0/3 in 3D, `billboard` 0/1/2); AtlasTexture `filter_clip`.
- **Atlas**: frames are not trimmed or rotated; for `aseprite-json` and `godot-spriteframes` a clip's distinct frames must fit one page (a 4096 page holds 9 frames of 1024 px); clips never span pages because an Aseprite tag addresses one image. `godot-sprite3d` alone has no such limit.
- **Input**: only built manifests. v1 and v2 are tested end to end with the real `build_animation_clips.py` (pingpong events, ticks and hints in v2) and with hand-written manifests that validate as `sprite/animation_clips_v2`.
- **Sprite3D-only exports** write `page: 0` per clip with an empty `atlas.pages` until the schema makes `page` optional (section 5.3).
- **Godot**: SpriteFrames cannot carry events (they are in engine-export.json and the Aseprite tag data). Import settings (Lossless, mipmaps) are not written. Relative ext_resource paths become `res://` paths when Godot re-saves the resource.
- **WebGL**: verified only through the opt-in check in headless Edge 154 and Chrome 154 on SwiftShader (no GPU), with a canvas standing in for the video element: GPU mode, outline shader, alpha exact after the snap, colour within 2 at alpha 64 (8-bit premultiplied canvas quantization), real `WEBGL_lose_context` loss and restore. Not run: a hardware GPU, Safari/iOS, a real `<video>` element, `requestVideoFrameCallback` in a browser, mobile memory budgets. That check found and fixed a real bug: snapping at exactly 2/255 and 253/255 was unreliable on the GPU, so the thresholds are now 2.5/255 and 252.5/255 (same 8-bit semantics).
- **CPU fallback**: tested with fake canvases in Node; it reads pixels back every frame (slow, correct) and skips the outline (`stats.outlineSkipped`).
- **Runtime**: `normalizeClip`/`normalizeAnimation` treat manifests as pingpong-expanded unless `{pingpongExpanded: false}`; 2.0 manifests get exact integer durations split from frameCount / fps. `walkPlayback` reports `paused` instead of driving `playbackRate` to 0. The outline colours follow an owner prototype and are not tuned.
- **Platforms**: Windows 11, Python 3.13.2, Node 22.15, Pillow 12.3, numpy 2.5 only. `node --check` of the `.js` wrapper needs Node 22.7+ module detection; the test retries older Node with an `.mjs` copy.
- **Deviations from the plan** (additions, nothing dropped): the `godot-sprite3d` target also writes an AnimatedSprite3D scene and a per-frame SpriteFrames next to the v1 contracts; `engine-export.json` is a new document type (section 5.1); the runtime adds `TravelMeter`, `eventsCrossed`, `anchoredRect`, `fallbackCell`, `pickPackedTransport` and `drawableRegion` beyond the plan's list; the plan's `phaseOffset` is `phaseOffset(distance, loopDistance, targetPhase)` with `entryPhase(clip)` for entry frames.
