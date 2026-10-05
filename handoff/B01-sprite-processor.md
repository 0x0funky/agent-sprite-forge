# B01-sprite-processor: generate2dsprite process on the shared core, geometry v2, soft still key, silent-error fixes, docs

Branch `asf/B01-sprite-processor` (from `wip/asf-upgrade-20261005` @ 3f9252d). Owned files changed:
[generate2dsprite.py](../skills/generate2dsprite/scripts/generate2dsprite.py),
[make_anchor_layout.py](../skills/generate2dsprite/scripts/make_anchor_layout.py),
[make_layout_guide.py](../skills/generate2dsprite/scripts/make_layout_guide.py),
[processing.md](../skills/generate2dsprite/references/processing.md),
[modes.md](../skills/generate2dsprite/references/modes.md),
[tests/test_generate2dsprite.py](../tests/test_generate2dsprite.py); new:
[tests/test_make_anchor_layout.py](../tests/test_make_anchor_layout.py),
[tests/test_sprite_geometry_v2.py](../tests/test_sprite_geometry_v2.py).

**Phase 3 integration status** (group fix pass "sprite", branch `asf/int-g-sprite`; the decisions file wins over the text below where they differ):

- Resolved per D15: `key_sheet` is one `forge_matte.key_still` call; `_local_soft_matte_regions` is deleted (S2 promoted it as `forge_matte.soft_matte_regions`, byte for byte the whole-sheet matte, with an exactness guard). `--key-quality hard` passes #FF00FF so it never estimates a backdrop (the cfed170 bytes). The 2048^2 default soft path is now perf-gated at 3 s (section 8).
- Resolved per D30: `_local_dilate`, `_round_half_up` and `_file_ref` are gone; `forge_core.dilate_square`, `round_half_up` and `file_ref` replace them (fileRefs now also carry `bytes`).
- Resolved per D27: all three CLIs run under `forge_core.run_cli`; usage errors keep exit 2 (D26).
- Resolved per D28: scale profiles, Godot contracts and `--prompt-file` are read as UTF-8 with or without a BOM.
- Resolved per D29: the QA envelope's `tool.version` is `forge_core.FORGE_PACKAGE_VERSION` (0.4.0).
- Resolved per D33: the section 5 schema request (nullable `source_rect`) and the optional documentation are in shared/schemas (S1, f3d7eb2); `tests/test_sprite_geometry_v2.py` validates against the vendored schema.
- Review fixes: a `key_ring_spill` QA warning (outer-ring key share above 0.01, measured after any despill); `--scale-strategy preserve`'s no-switch change is in the CHANGELOG and processing.md; processing.md links frames-and-clips.md; `RegisteredFrameExportTests` moved to B02's test files.

## 1. CLIs

Run from the user's project root (`<skill-dir>` is `${CLAUDE_SKILL_DIR}` in Claude Code):

    python "<skill-dir>/scripts/generate2dsprite.py" process --input raw/hero-idle.png --target asset --mode idle --rows 2 --cols 3 --output-dir sprites/hero-idle --background-mode native_alpha --align feet --scale-strategy preserve --cell-size 128 --fit-scale 0.8 --strict-qc --write-scale-profile sprites/hero-scale.json
    python "<skill-dir>/scripts/generate2dsprite.py" process --input raw/hero-run.png --target asset --mode run --rows 2 --cols 4 --output-dir sprites/hero-run --scale-profile sprites/hero-scale.json --max-profile-scale-drift 0.08 --strict-qc
    python "<skill-dir>/scripts/generate2dsprite.py" process --input raw/slime-hop.png --target asset --mode idle --rows 2 --cols 2 --output-dir sprites/slime-hop --background-mode native_alpha --resampler nearest --logical-pixel 8 --pixel-scale 2 --scale-strategy registered --align feet --cell-size 96 --strict-qc
    python "<skill-dir>/scripts/generate2dsprite.py" process --input raw/sheet-1254.png --target player --mode player_sheet --output-dir sprites/hero-walk --grid-rounding nearest --direction-order down,left,right,up
    python "<skill-dir>/scripts/generate2dsprite.py" process --input raw/chest.png --target asset --mode single --output-dir sprites/chest --key-quality soft
    python "<skill-dir>/scripts/generate2dsprite.py" build-prompt --target asset --mode single --prompt "a wooden chest" --write-json prompts/chest.json
    python "<skill-dir>/scripts/generate2dsprite.py" build-godot-bundle --action idle=sprites/hero-idle/godot-sprite3d.json --action attack=sprites/hero-attack/godot-sprite3d.json --default-action idle --one-shot attack --output sprites/hero-godot-bundle.json
    python "<skill-dir>/scripts/make_anchor_layout.py" --input accepted/hero-master.png --rows 2 --cols 4 --cell-width 384 --cell-height 384 --feet-ratio 0.82 --background-mode native_alpha --resampler nearest --output guides/hero-anchor-2x4.png
    python "<skill-dir>/scripts/make_layout_guide.py" --rows 2 --cols 4 --cell-width 384 --cell-height 384 --safe-margin-x 58 --safe-margin-y 58 --output guides/layout-2x4.png

New `process` flags: `--key-quality auto|soft|hard|dominance`, `--key magenta|green|blue`, `--alpha-hygiene none|floor|detached|both`, `--alpha-floor`, `--alpha-geometry-threshold`, `--connectivity 8|4`, `--anchor-mode feet|stance|bbox|center|centroid|legacy-p98`, `--anchor-px X,Y`, `--scale-strategy registered`, `--pixel-scale N`, `--logical-pixel M`, `--legacy-fractional-nearest`, `--grid-rounding exact|nearest`, `--pad-to-grid`, `--direction-order`, `--profile-override`, `--shared-scale/--no-shared-scale`. `build-prompt --legacy-style`.

- `--help` of all three scripts and of every `generate2dsprite.py` subcommand is ASCII and exits 0 under cp1252 and cp950 (tested).
- One-line JSON summary on stdout: `process` prints `output_dir`, `metadata`, `frames`, `qa_status` and, when written, `godot_sprite3d` and `scale_profile`; `build-godot-bundle` prints `output`, `actions`, `default_action`; `make_anchor_layout.py` prints `output`, `size`, `mode`, `background_mode`, `input_sha256`; `make_layout_guide.py` prints `output`, `size`, `rows`, `cols`. `build-prompt` still prints the prompt text itself and `list-options` indented JSON (both are their product, not a run summary).
- Exit codes (D26, D27): 0 success (the `qa` envelope may still warn); 1 for every domain error (`error: <message>` on stderr, no traceback, nothing published); 2 for argparse usage errors (for example `--cell-size 0`: `argument --cell-size: must be a positive integer`). An unexpected failure is one `error: internal error (<Type>: <message>)` line with exit 1 (`forge_core.run_cli`; `FORGE_DEBUG=1` re-raises).
- Existing outputs are never replaced: `--output-dir`, sidecars, bundles, anchor templates and layout guides are refused when they exist.
- JSON inputs (scale profiles, Godot contracts) and `--prompt-file` may be UTF-8 with or without a byte-order mark (D28); the BOM never reaches the prompt text.

## 2. SKILL.md routing rows

generate2dsprite (from the plan note: image sheets go through process, then sheet_qc and scale_frames; code-art frames go straight to build_animation_clips):

| Need | Route |
|---|---|
| Key, split and register a generated or painted sheet | `scripts/generate2dsprite.py process` (geometry v2); then `sheet_qc.py` and `scale_frames.py` when B03 lands; then `build_animation_clips.py` |
| Pixel art drawn at M source pixels per art pixel | `process --resampler nearest --logical-pixel M --pixel-scale N` (integer scales only) |
| Sheet drawn on one registration point, with jumps, bob or recoil | `process --scale-strategy registered` (or `--anchor-px X,Y`) |
| Keep one character's scale across actions | `process --write-scale-profile` on the grounded reference, then `--scale-profile ... --max-profile-scale-drift 0.08` |
| Host canvas does not divide into the grid (1254x1254, 1672x941) | `process --grid-rounding nearest` or `--pad-to-grid` |
| Four- or eight-direction sheet in a non-default row order | `process --direction-order down,up,left,right` |
| Generation reference with fixed scale and feet line | `scripts/make_anchor_layout.py`, or `scripts/make_layout_guide.py` for an empty safe-frame guide |
| Code-art frames (codeart2d) | never `process`; `build_animation_clips.py` directly |
| Godot Sprite3D | `process --godot-world-height H`, then `generate2dsprite.py build-godot-bundle` |

Processing notes, ready to paste:

- Geometry counts alpha above 16 and 8-connected components; native-alpha input gets alpha hygiene (`both`); anchors sit on the ground line of the main component. Every changed default has a legacy switch (references/processing.md table).
- `--key-quality auto` gives soft anti-aliased edges for smooth art and the binary keyer for nearest/pixel art; quote `pipeline-meta.json` `matte.qa` numbers when claiming a clean key. A `key_ring_spill` warning means the binary key left a key-coloured fringe: add `--despill-radius 1` or use `--key-quality soft`.
- `nearest` only takes whole-pixel scales; `preserve` and `registered` share one sampling grid, so static parts never shimmer.
- `pipeline-meta.json` is `generate2dsprite.pipeline_meta.v2` with a `qa` envelope; scale profiles are version 2 (version 1 still read).

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `generate2dsprite.py process` | Keys (soft or binary) or validates a sheet, removes alpha haze, splits it (exact, rounded or padded grid), registers frames on one sampling grid (fit, preserve, registered; integer nearest) and writes frames, sheet, previews and pipeline_meta.v2 with QA | tests/test_generate2dsprite.py, tests/test_sprite_geometry_v2.py (fox fixture: strict QC flags only [[0,2],[0,3]], visible height 37.5 px; repros r01-r21) |
| `generate2dsprite.py build-prompt` | Builds a generation prompt for a target/mode; neutral style by default, `--legacy-style` for the old text | tests/test_generate2dsprite.py `test_default_prompt_has_no_franchise_names` |
| `generate2dsprite.py build-godot-bundle` | Combines per-action Sprite3D contracts into one bundle; refuses existing files and unresolvable frames | `test_bundle_refuses_existing` |
| `make_anchor_layout.py` | Repeats an accepted character into a fixed-scale, fixed-feet template for generation | tests/test_make_anchor_layout.py |
| `make_layout_guide.py` | Draws an empty layout guide with per-cell safe frames | tests/test_make_anchor_layout.py `LayoutGuideTests` |

## 4. CHANGELOG entries

- Added: `process --scale-strategy registered` (one scale, one offset; drawn jumps survive, r16) (B01-T5; S07).
- Added: `--pixel-scale N` and `--logical-pixel M` for whole-pixel nearest with logical-centre sampling, and a pixel-grid QC count (`qc_summary.pixel_grid_off_edges`) (B01-T5; S06).
- Added: `--grid-rounding nearest` and `--pad-to-grid` for host canvases that do not divide; errors name rows x columns (B01-T5; DOC-03).
- Added: `--anchor-mode feet|stance|bbox|center|centroid|legacy-p98` and `--anchor-px` (B01-T4; S14, S15).
- Added: `--alpha-hygiene`, `--alpha-floor`, `--alpha-geometry-threshold`, `--connectivity` (B01-T2, T4; S01, S04, DOC-04).
- Added: `--key-quality auto|soft|hard|dominance` and `--key magenta|green|blue` through the shared keyer, with the matte report in pipeline-meta (B01-T6; S24, report v2 P2-2).
- Added: `--direction-order` for per-row strips, GIFs and frame names (B01-T6; DOC-08).
- Added: `--profile-override`; applied profile values are printed and recorded under `profile_applied` (B01-T3; S09).
- Added: pipeline-meta `qa` (common qaEnvelope), `provenance` (input sha256, mode, bit depth, conversion), per-frame `trim_offset` and `source_rect`, `gif_decoded_duration_ms` (B01-T4, T6; S21, S27).
- Changed: pipeline-meta.json is `generate2dsprite.pipeline_meta.v2`; new scale profiles are version 2 and store `reference.output_subject_height_px`; version 1 profiles are still read with their old drift metric (B01-T5; S02, S10).
- Changed: `body_scale_mean`/`body_scale_cv` are the visible output subject height over the cell size; the cfed170 cell-area metric moved to `legacy_body_scale_*` (B01-T5; S10).
- Changed: `preserve` resamples every cell on one shared grid and moves frames by whole output pixels (B01-T5; S05).
- Changed: single-image modes run as a 1x1 grid: `--fit-scale` (default 0.9 there), `--align`, component cleanup and full strict QC now apply (B01-T5; S17).
- Changed: publishing, image loading, components, hygiene, anchors and resampling come from the vendored forge_core; keying from forge_matte (B01-T2; F-03, S03, S11, S16).
- Changed: the raw copy (`raw-sheet.<ext>`, `raw.<ext>` for single images) is the input's exact bytes; `input` in pipeline-meta is the file name and sidecar paths are relative fileRefs (B01-T3; S27).
- Changed: Godot Sprite3D contracts list frame paths relative to the contract file (B01-T6; S19).
- Changed: `make_anchor_layout.py` and `make_layout_guide.py` refuse an existing `--output`, load inputs with forge_core and print a JSON summary (B01-T7).
- Changed: the soft still key runs through `forge_matte.key_still`, which mattes only the content regions of a sheet with the same bytes as one whole-sheet matte; the 2048^2 default path takes about 2.5 s instead of 3.1-4.4 s (integration, D15).
- Added: QA check `key_ring_spill` warns when more than 1% of the outer edge ring stays key-coloured (the binary key's fringe); it measures the published sheet, `matte.qa_after_despill` after a despill. A warning only (sprite review).
- Changed: an unexpected failure prints one `error: internal error (<Type>: <message>)` line and exits 1 instead of a traceback, in all three CLIs (integration, D27).
- Changed: QA envelopes record the package version `0.4.0` as `tool.version` (integration, D29).
- Changed: fileRefs in pipeline-meta (`qa` outputs, sidecars, the applied profile) also carry `bytes` (integration, D30).
- BREAKING: 8-connected components; legacy switch `--connectivity 4` (Appendix H).
- BREAKING: anchor on the bottom edge of the lowest stable row; legacy switch `--anchor-mode legacy-p98` (Appendix H).
- BREAKING: geometry ignores alpha at or below 16; legacy switch `--alpha-geometry-threshold 0` (Appendix H).
- BREAKING: alpha hygiene `both` for native_alpha input; legacy switch `--alpha-hygiene none` (Appendix H).
- BREAKING: an unknown `--mode`, a mode of another target, or `--mode sheet` without `--rows/--cols` fails; no switch (bug fix) (Appendix H).
- BREAKING: a profile/flag conflict is an error; legacy switch `--profile-override` (Appendix H).
- BREAKING: fractional nearest scales are refused; legacy switch `--legacy-fractional-nearest` (Appendix H).
- BREAKING: `--key-quality auto` (soft for chroma + lanczos); legacy switch `--key-quality hard` (Appendix H).
- BREAKING: pipeline-meta v2 and scale profile v2; version 1 profiles still read (Appendix H).
- BREAKING: `--align bottom` is a deprecated alias of `feet` (warns); no switch (Appendix H).
- BREAKING: `build-prompt` default style names no franchise; legacy switch `--legacy-style` (Appendix H).
- BREAKING: an opaque grid with more than one cell is refused (use assemble_frames.py); a missing `--prompt-file` fails; `--cell-size` must be positive; `--label-prefix` needs `--rows/--cols`; `build-godot-bundle` refuses an existing output (S22, S27, S18, S19).
- BREAKING: `--scale-strategy preserve` samples every cell on one shared grid and moves frames by whole output pixels (S05), so its frames differ from cfed170 (about 3,000 px per frame on a chroma preserve/feet sheet); no legacy switch. Appendix H should list it (sprite review).
- Fixed: S01 (alpha haze drove every geometric decision), S02 (profile drift judged in source-cell area), S03 (22-29 s pure-Python keyer and BFS per 2048^2 sheet), S04 (diagonal strokes split), S05 (static-body shimmer), S06 (uneven nearest pixels), S07, S08, S09, S10, S14 (anchor on the row index, feet hanging 2-3 px below the origin), S15, S16 (16-bit grey turned white, animated input silently used frame 0), S17, S18, S19, S20, S21, S22, S24, S25, S26, S27, DOC-03, DOC-04, DOC-08, DOC-15, DOC-16, DOC-22; probe `--fit-scale 1.0 --align feet` traceback.
- Fixed: Pillow 12.3 wrote a corrupt GIF (a second header) when a blank frame followed a disposal-2 frame; the preview writer now keeps blank frames ordinary frames and the decoded timing is recorded.
- Fixed: scale profiles, Godot contracts and prompt files written by Windows PowerShell 5.1 (UTF-8 with a BOM) are read (integration, D28; sprite review).

## 5. Schema change requests

**Status: applied.** The required diff and the optional fields below are in shared/schemas/sprite.schema.json (S1, commit f3d7eb2), and the tests validate against the vendored copy (per D33). The text below is kept as the record.

**New, optional (documentation only; objects are open, current outputs validate):** `pipeline_meta_v2.matte.qa_after_despill` (forge_matte.matte_qa metrics of the sheet after `--despill-radius`, written only when a despill ran) and the qa check id `key_ring_spill` (value: outer-ring key share; threshold 0.01). For the schemas owner: add `"qa_after_despill": {"type": "object"}` to `/$defs/pipeline_meta_v2/properties/matte/properties` and mention it in the matte description.

**Required** (B01 writes it; the test validates with this diff applied in memory, `tests/test_sprite_geometry_v2.py::pipeline_meta_errors`). An empty cell has no subject box, so `source_rect` is null there. Pointer `/$defs/pipeline_meta_v2/properties/frames/items/properties/source_rect` in `shared/schemas/sprite.schema.json`:

```diff
-        "source_rect": {
-          "description": "Subject box in sheet pixels, [x0, y0, x1, y1).",
-          "$ref": "common.schema.json#/$defs/box"
-        }
+        "source_rect": {
+          "description": "Subject box in sheet pixels, [x0, y0, x1, y1); null for an empty frame.",
+          "anyOf": [{"$ref": "common.schema.json#/$defs/box"}, {"type": "null"}]
+        }
```

Producer B01; consumers B03 (sheet_qc reading frame boxes), Z docs. With the frozen schema, the only error on a sheet with an empty cell is `$.frames[3].source_rect: None is not of type 'array'` (tested).

**Optional fields the producer adds** (objects are open; documenting them avoids drift). Proposed additions to `$defs.pipeline_meta_v2.properties`:

```json
"qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
"godot_sprite3d_output": {"$ref": "common.schema.json#/$defs/fileRef"},
"scale_profile_output": {"$ref": "common.schema.json#/$defs/fileRef"},
"profile_applied": {"type": "object", "required": ["name", "version", "applied", "overrides"], "properties": {
  "name": {"type": "string"}, "version": {"enum": [1, 2]}, "path": {"$ref": "common.schema.json#/$defs/fileRef"},
  "applied": {"type": "object"},
  "overrides": {"type": "array", "items": {"type": "object", "required": ["key", "profile", "flag"]}}}},
"gif_decoded_duration_ms": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "integer", "minimum": 0}}},
"warnings": {"type": "array", "items": {"type": "string"}},
"prompt_source": {"enum": ["file", "argument"]},
"prompt_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
"key_quality": {"enum": ["hard", "soft", "dominance", "auto"]},
"key": {"enum": ["magenta", "green", "blue"]},
"alpha_hygiene": {"enum": ["none", "floor", "detached", "both"]},
"alpha_floor": {"type": "integer", "minimum": 0, "maximum": 255},
"logical_pixel": {"type": "integer", "minimum": 1}
```

and to `geometry.properties`: `logical_pixel` (integer >= 1), `anchor_px` (point2 or null), `anchor_band_fraction` (number), `scale` (number or null), `scale_ratio` (`[num, den]` integers or null), `registration_anchor` (point2 or null), `injected_shift_px` (`{x: [min, max], y: [min, max]}` or null), `legacy_fractional_nearest` (boolean). Per frame: `subject_bbox` (box or null), `anchor_cell` (point2 or null), `offset_px` (point2), `output_anchor` (point2), `output_subject_height_px` (number), `body_scale` (number), `pixel_grid_off_edges` (integer). `qc_summary` gains `legacy_body_scale_mean`, `legacy_body_scale_cv`, `output_subject_height_median`, `scale_reference_height_px`, `scale_reference_frames`, `pixel_grid_off_edges`, `profile_body_scale_drift`; `provenance` gains `bytes`, `format`, `size`; `matte` gains `resampler_hint`, `thresholds` (hard key) and `despill` (forge_matte.despill report).

Type change to note: v1 metadata stored `godot_sprite3d_output`, `scale_profile_output` and `scale_profile.path` as absolute path strings; v2 stores fileRefs relative to the output folder (a file name plus sha256 when another drive has no relative route).

`$defs.scale_profile_v2`: version 2 profiles also write `schema`, `reference.legacy_body_scale_mean` and the processing keys `background_mode`, `resampler`, `despill_radius`, `key_quality`, `key`, `alpha_geometry_threshold`, `connectivity`, `anchor_mode`, `alpha_hygiene`, `alpha_floor`, `pixel_scale`, `logical_pixel`; written profiles validate against the frozen schema (tested).

## 6. Shared-helper promotion requests

**Status:** resolved except the optional `pixel_grid_off_edges`. S2 promoted the region matte (`forge_matte.soft_matte_regions`, used by `key_still`) and `forge_core.dilate_square`, `round_half_up` and `file_ref`; this pass adopted them and deleted the private copies (per D15, D30). `key_sheet` is now one `key_still` call (key_still gained the hard thresholds). The original requests follow as the record.

- Resolved (D15): `generate2dsprite._local_soft_matte_regions(pixels, params, key_rgb) -> ndarray` (generate2dsprite.py:595) into forge_matte, for example as the engine of `key_still(quality="soft")`. It runs `soft_matte` only on 8-connected groups of 16 px content tiles plus a 32 px margin and returns the same bytes as one whole-image call (every soft-matte step reaches at most about 20 px); with more than 64 groups (a noisy backdrop) or 75% of the area it makes that one whole-image call instead. On the 2048^2 perf sheet the matted area drops to about 56%. Exactness: `tests/test_sprite_geometry_v2.py::SoftRegionTests` (4 sheets incl. noise, holes, partial alpha and a green key, interior despill on and off). It needs a pixel bound on the matting space; it uses the BT.601 rows that forge_matte's `_matting_space` uses, which forge_matte could expose instead.
- Resolved (D30): `generate2dsprite._local_dilate(mask, radius)` (generate2dsprite.py:887) duplicates `forge_core._dilate_square` and `forge_matte._dilate`; promote one public `forge_core.dilate_square(mask, radius)` (A2 asked for the same).
- Optional: `generate2dsprite._off_grid_edges(frame, block, origin)` (generate2dsprite.py:932) as `forge_core.pixel_grid_off_edges`, if B04/B18 want the same run-length check on integer-scaled pixel art (tests: `test_r08_integer_nearest_blocks_and_fractional_rejected`).
- Resolved (D15): `key_sheet` (generate2dsprite.py:646) mirrors `forge_matte.key_still` but passes the user's `--threshold/--edge-threshold` to the binary keyer (key_still's hard mode uses the fixed 100/150) and uses the region matte. If key_still gained those two parameters and region matting, key_sheet could become one call.

## 7. Cross-module links that Z must add

- Done at integration: processing.md links [frames-and-clips.md](../skills/generate2dsprite/references/frames-and-clips.md) and names `sheet_qc.py spill` and `scale_frames.py` before `process`. B02's character-animation.md split may still want a link back to processing.md for registered processing.
- generate2dsprite SKILL.md: the routing rows of section 2, a link to references/schemas/sprite.schema.json, and "code art never goes through process".
- codeart2d SKILL.md (Z/B18): code-art frames skip `process` and go to build_animation_clips.
- prompt-rules.md (B03): the anchor template and `--scale-strategy registered` as the pairing for registered generation; build-prompt's default style is now neutral (no franchise names, no "16-bit").
- sheet_qc (B03) and scale_frames (B03) can read pipeline-meta v2 `frames[].source_rect`, `trim_offset`, `anchor_cell` and `output_origin`.
- build_animation_clips (B02): `anchor_px` of a clips manifest built from process output is `output_origin` in pipeline-meta.json; modes.md suggests hitboxes as clip events.
- export_engine (B09): Godot Sprite3D contracts now hold frame paths relative to the contract.
- Done at integration: `RegisteredFrameExportTests` (the fork's tests of B02's tools) moved to tests/test_build_animation_clips.py and tests/test_assemble_frames.py.
- CHANGELOG: the BREAKING rows of section 4 are plan Appendix H's sprite rows, plus the no-switch `preserve` row (Appendix H should gain it).

## 8. Known limitations and what is not proven

- Speed (re-measured at integration, after D15, with other agents running): on the 2048^2 4x4 perf sheet the default path (auto -> soft key) takes 2.2-2.7 s as the first run of a fresh process and 3.01 s best / 3.08 s median as a CLI (interpreter start included); `--key-quality hard` takes about 1.6 s. A profile of the soft run: key_still 1.84 s (region matte 1.14, matte_qa 0.32, key_material_share 0.20, estimate_key 0.15), the rest 0.65 s. Two gates: `test_process_2048_chroma_under_3s` (the S03 configuration, in process, best of 2) and `test_process_2048_default_soft_key_under_3s` (the default path, best of 3 in a fresh interpreter). The fresh interpreter matters: on this Windows machine a long-lived process runs the same numpy work 50-70% slower after its first heavy runs (later runs took 3.9-4.3 s; a plain numpy loop shows the same, 1.3 s -> 2.2 s), so the in-process version of the soft gate failed inside full test sessions (3.87-3.98 s) while passing alone. Headroom is about 10%: re-run the gate alone before believing a failure on a loaded machine. Repeated `process` calls in one Python process are slower for the same reason; the CLI runs one sheet per process. The soft matte's own budget stays A2's 0.8 s per 960^2 frame.
- One real sheet: the fox gates (strict QC [[0,2],[0,3]], no clamp, visible height 37.5 px, 67,907 haze pixels) are measured on raw-fox-run-v1.png only. The soft still key was tuned in A2 on the Ryo clip; for stills it is verified on synthetic anti-aliased edges (colour error <= 4/255, no magenta-leaning pixel), not on a real host chroma sheet.
- Platforms: Windows 11, Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1 only. Sources parse with the Python 3.10 grammar (checked) but the 3.10 / Pillow 10.1 / numpy 1.26 floors were not run; the GIF blank-frame workaround is verified on Pillow 12.3 only. Godot import of the contracts was not run.
- Deviation (T1): the plan's "3 known differences" (regexes `transparen` and `opaque`, mock target `MODULE.publish_directory_no_replace`) belong to the platform-strip test (B11's file). The 32 sprite tests and 9 anchor-layout tests were ported verbatim and passed against the unmodified script (73 passed, 28 subtests) in commit 492fdec.
- Ported tests changed on purpose in f492d06 where a default changed: `--target player --mode idle` became `asset/idle` (S08); nearest runs gained `--pixel-scale` (S06); parser-default checks go through `plan_process` (S09); publication mocks point at forge_core; sidecar paths are relative fileRefs; the equal-subject preserve test places its subjects a whole output pixel apart (S05: one shared grid); `center_single_sprite` is gone, single images run as a 1x1 grid.
- Removed internals: `center_single_sprite`, `estimate_anchor` (now `_legacy_p98_anchor`), `alpha_area`, `connected_components`, `_publish_file_no_replace`, `_publish_directory_no_replace`; redundant frame keys `scale_adjustment`, `bbox_scale_applied`, `scale_changed`, `preserved_subject_size`, `shared_center_x`, `shared_feet_y`.
- `--alpha-geometry-threshold` defaults to 16 for every background mode; binary alpha (hard key, opaque) measures the same as before. `fit` crops to the measured box but carries faint pixels just outside it; `preserve`/`registered` never crop.
- `preserve` keeps whole-pixel offsets, so the same subject drawn at a different sub-pixel position in another cell gets a different sampling phase (the price of S05); `registered` or `--anchor-px` avoids it for registered art.
- The locomotion advisory still counts eight poses for `player_sheet`, so a classic four-column top-down walk triggers it; modes.md explains it and `--intentional-low-frame-count`.
- Not proven: pose quality, identity, loop seams and timing (the qa envelope lists them under `notProven`); 8-direction and hitbox guidance in modes.md is documentation only.
