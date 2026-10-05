# Changelog

## 0.4.0 — 2026-10-06

One integrated release of the five skills (plugin version `0.4.0`). Not yet tagged; tagging and publishing are the owner's. Measured numbers below come from the repository's fixtures and one real clip unless stated; see [docs/known-limitations.md](./docs/known-limitations.md).

### Added

**Repository and contracts**
- Frozen JSON Schema 2020-12 contracts for all five skills in `shared/schemas/` (common, sprite, video, map, codeart, media), vendored into each skill's `references/schemas/` and validated by the standard-library evaluator `forge_schema`.
- Shared libraries with one canonical copy in `shared/`, vendored into the skills and kept byte-identical by `tools/vendor_sync.py` (sha256 test): `forge_core` (publication, image loading, components, alpha hygiene, anchors, premultiplied resampling, frame timing, seam reports), `forge_matte` (keyers), `forge_av` (ffmpeg probes and encoders), `forge_palette` (OKLab palettes), `forge_nav` (collision rules).
- Claude Code plugin: `.claude-plugin/plugin.json` and `marketplace.json`; `claude plugin validate --strict .` passes.
- `tools/install_skills.py` (backup, sha256 manifest, `--check` drift report, never ships bytecode) and `tools/check_links.py`.
- Real regression fixtures with `tests/fixtures/real/PROVENANCE.json`; pytest markers `perf`, `bench` (opt-in), `ffmpeg`, `node`, `resvg`, `e2e`; an end-to-end smoke over five pipelines and a JS/Python collision parity test.

**codeart2d (new skill)**
- `render_pixelspec.py` (PixelSpec to exact-palette frames per palette variant, clips manifests), `svg_render.py` (`render`, `lint`, renderer `doctor`), `pixel_qa.py`.
- `rig_animate.py`: SVG rigs with FK, two-bone IK and a ground constraint; 0 px planted-foot drift on the hero example.
- `fx_build.py` (slash, sparks, ring, flash, dust, projectile; hit events; `--export-runtime` writes an fx.v1 module) and `fx_verify.mjs`.
- `autotile_build.py`: Wang-16, three-material (81 tiles), blob-47, bevel and flat sets, each with an exhaustive seam proof.
- `layout_build.py` (playable map_bundle.v2 with collision and a reachability gate), `parallax_build.py`, `ambient_bake.py`; examples for every tool.

**generate2dsprite**
- `process`: `--scale-strategy registered`, `--pixel-scale` / `--logical-pixel`, `--grid-rounding nearest`, `--pad-to-grid`, `--anchor-mode`, `--alpha-hygiene`, `--key-quality auto|soft|hard|dominance`, `--key magenta|green|blue`, `--direction-order`, `--profile-override`; pipeline-meta `qa`, `provenance` and a `key_ring_spill` warning.
- `sheet_qc.py` (`spill` before slicing; `frames`: identity, NEAR/FAR alternation, drift), `scale_frames.py` (one scale and root, never clamps), `plan_guide.py`, `references/action-recipes.md`.
- `build_animation_clips.py` v2: 60 Hz ticks with drift-free integer ms, events, keys, entry frame, stride, transition hints, hit-stop, review sheets (filmstrips, onion skins, turn test, dissolves) and lints. `assemble_frames.py`: chroma keying, one crop-box schema, spill check and `--slice ownership`, `--loop-overlap K --ambient`.
- `palette_tool.py` (`build`, `apply`, `lock`, `luts`, `variants`, `quantize-seq` with temporal hysteresis: -46.4% noise flips on a real game clip) and `pixel_reduce.py`.
- `export_engine.py`: Aseprite JSON, Godot SpriteFrames and AnimatedSprite3D with an engine mapping table.

**video2dsprite**
- Soft matte keying (`--matte soft|dominance|binary`), `--key auto`, pocket removal, `--despill-mode auto`, `--protect-color`, `--reference`, `--temporal-stability`, `--workers`, `matte-report.json`; `triage` and `key-plan` verbs; `--matte-profile`.
- `prepare_i2v_input.py` and `register_clip.py` (`apply`, `qc`, `profile`, `palette-repair`): registration by construction, padding contract, feet/x/hip locks, actor and fx profiles.
- `gait_loop.py` (`select` with half-period guard, idle/hover loops; `measure-stride`), `retime.py` (spans, impact/hold, three-key map, ticks, loop-policy rules), `animation_review.py select`.
- `engine_export.py` CLI (`package`, `verify`, `doctor`): animation.json 3.0, mobile packed tiers, loop-aligned closed GOPs, hash-bound `verify-qa.json`. `validate_animation.py` with 27 rules.
- `references/runtime/forge-runtime.mjs` (distance-driven walk, hit-stop, transitions, fixed-step loop) and `packed-alpha-webgl.mjs` (WebGL compositor with CPU fallback).

**generate2dmap**
- `extract_prop_pack.py` v2 (anchors, footprints, auto boxes, keep-canvas, world scale, despill), terrain overlays, iso and hex tiles, Wang rows, border check; platform middle variants and surface QC.
- `compose_layered_preview.py` debug overlay, placement audit and plate pan; `validate_parallax.py` pivot, aspect sweep, pixel grid and seam verdicts; `conform_background.py`.
- `map_bundle.py` (`validate`, `hash`), `map_nav.py` (`check`, `query`), `export_tiled.py` (re-render verified at 0 px), `export_godot.py` (Godot 4.3+), `export_ldtk.py` (LDtk 1.5.3), `validate_chunks.py`, `validate_layout.py`, `references/map-presets.md`, `references/engine-maps.md`.
- `build_scene_preview.py` (single-file walkable preview with a route check) and `references/runtime/map-runtime.mjs` (mirrors forge_nav rules N1-N15).
- HD-2D: `validate_stage.py`, `scene_layout_guide.py`, `extract_scene_lights.py`, `edit_locality_check.py`, `build_motion_mask.py`, `scene_motion.py` (`build`, `qa`).

**generate2dmedia**
- `forge_doctor.py`: capability check with the Codex/Grok CLI readiness ladder (PRESENT, AUTH_MODE, TOOL_EXPOSED, VERIFIED) and a ROUTES block; reads no credentials.
- `cli_media.py`: local routes `codex-cli` (image), `grok-cli` (image, edit), `grok-acp` (image-to-video), `--route auto`, dry run by default, provenance checks, `resume --adopt`, `adopt --codex-thread` (supersedes draft PR #5; thanks to its author), `batch`.
- Spend ledger `.forge/ledger.jsonl` and `media_ledger.py`; caps `--budget-usd`, `--max-calls`, `FORGE_MAX_PAID_REQUESTS`; local session cap (8 images, 2 videos per 12 hours); consent block with estimates from `references/prices.json`; job.json receipts; `batch` for paid jobs; `--base-url` with `--allow-custom-base-url`; `--upload-url` for xAI zero data retention (field name unverified).

- `prepare_i2v_input prepare --master-key` keys an opaque master on a flat magenta, green or blue backdrop (recorded as `masterKeying`); `video2dsprite.py clean` also accepts `raw_*.png`.
- `render_pixelspec` warns when a walk/run clip's half-cycle frames are near-duplicates (`half_cycle_duplicates`, silhouette IoU >= 0.95; override `--allow-duplicate-half-cycle`); the summary lists `warned_checks`.

### Changed

- `render_pixelspec --strict-qc` refuses only a `fail` status; a `warn` still publishes and is reported verbatim.
- Five SKILL.md files rewritten with art-source rules (code art first inside its envelope, then local agent first), host notes for Codex, Claude Code and Grok, and a capability check. `codeart2d` and `generate2dmedia` are explicit-only in Codex.
- Requirements: Python 3.10+, Pillow >= 10.1, numpy >= 1.26, scipy >= 1.11 (with a numpy fallback); `requirements-codeart.txt`; `requirements-dev.txt` adds jsonschema.
- Every CLI writes a new output folder through staged, no-replace publication, prints one ASCII JSON line, exits 1 on failure with one `error:` line and 2 on usage errors, and runs under cp1252/cp950 consoles. QA envelopes record `tool.version` 0.4.0. Manifests store relative paths; JSON inputs may carry a UTF-8 BOM.
- Speed: the legacy keyers are vectorised with identical bytes (2048^2 sheet: 22.0 s to 0.23 s); component labelling 2.5 s to 0.03-0.05 s per sheet; video keying 0.34 s per 960x960 frame on four threads.
- `video2dsprite` decodes and encodes through forge_av (functional ffmpeg probes, BT.709 tags, bitexact WebM, edge bleed under alpha).
- Terrain and platform manifests are v2 (relative paths, QA envelopes); terrain tiles are RGBA. `--edge-policy seamless` now changes processing and gates wrap seams.
- `packed-alpha-runtime.js` wraps the WebGL compositor and keeps its API.
- `generate_media.py` prints scrubbed provider errors; image submit timeout 300 s.

### BREAKING

Each new default has a legacy switch where one is possible; v1 documents remain readable.

| Area | New default | Legacy switch |
| --- | --- | --- |
| sprite process | 8-connected components | `--connectivity 4` |
| sprite process | anchor = bottom edge of the lowest stable row | `--anchor-mode legacy-p98` |
| sprite process | alpha geometry threshold 16 | `--alpha-geometry-threshold 0` |
| sprite process | alpha hygiene `both` for native-alpha input | `--alpha-hygiene none` |
| sprite process | unknown `--mode` fails; `sheet` needs rows/cols | none (bug fix) |
| sprite process | profile/flag conflict is an error | `--profile-override` |
| sprite process | fractional nearest scales refused | `--legacy-fractional-nearest` |
| sprite process | `--key-quality auto` (soft for chroma + lanczos) | `--key-quality hard` |
| sprite process | pipeline-meta v2, scale profile v2 | v1 profiles still read |
| sprite process | `--align bottom` is a deprecated alias of `feet` | none |
| sprite process | `preserve` samples every cell on one shared grid (frames differ from 0.3) | none |
| sprite process | opaque multi-cell grids refused (use `assemble_frames.py`); `build-godot-bundle` refuses existing output | none |
| sprite build-prompt | no franchise style text | `--legacy-style` |
| sprite build-prompt | `--write` / `--write-json` refuse an existing file | `--overwrite` |
| assemble_frames | `--sheet` refuses cross-cell spill | `--allow-spill` (better: `--slice ownership`) |
| assemble_frames | `--key chroma` needs a backdrop of the key colour on the border | pass `--key-color` or drop `--key chroma` |
| clips tools | print one JSON line instead of the bare output path; relative paths | none |
| video process | `--matte soft` + pockets + `--despill-mode auto` + alpha hysteresis | `--matte binary --despill-mode off` |
| video process | `--dist` / `--despill` need `--matte binary` | none |
| video (all verbs) | refuse an existing output folder | choose a new folder |
| video frames | raw frames are `frame_000000.png` (0-based) | none |
| video package | fails on key residue | `--allow-key-residue` |
| video package | animation.json 3.0 (every 2.0 key kept); runtimes crop packed alpha at `halfWidth` | none |
| prop pack | refuses an existing output dir; exit 1 when nothing is accepted | new folder |
| prop pack | despill radius 1 for chroma | `--despill-radius 0` |
| prop pack | 8-connected; manifest v2 with `anchor_px` | `--connectivity 4`; v1 readable |
| prop pack | `--min-component-area` auto (scaled to the cell) | `--min-component-area 100` |
| compose | ground-line sort + manifest anchors | `--sort raw-y`, `--anchor px` |
| compose, parallax | refuse an existing output or report file | new path |
| terrain | runtime 3D fields omitted unless given | `--emit-runtime-defaults` |
| terrain | refuses an existing output dir; `--strict-qc` also fails drawn borders and art above a platform surface | new folder; `--max-border-delta 1`, `--surface-tolerance-px` |
| parallax | coverage required for repeated and near layers | `--coverage sky-only` |
| media | identical successful request refused | `--allow-duplicate` |
| media | job.json v2; image submit timeout 300 s | v1 jobs resume; `--submit-timeout 180` |
| repo | Python 3.10+, Pillow >= 10.1, scipy added, vendored `forge_*` in skills | none |

### Fixed

- Found by the 2026-10-06 live validation: `prepare_i2v_input` refused an opaque master; the image-to-video prompt contract took the last noun of `--subject` ("everything behind the scarf"); an agent summarised a WARN QA envelope as "all checks passed" (SKILL.md now requires WARN/FAIL to be reported verbatim).
- Purple fringe and enclosed key pockets (report v2 P0-1, P0-2): on Ryo frames 57-87 visible fringe 8,504 to 0 px per frame, leak 15.3 to 0; frames with pockets 16 of 145 to 0; flicker 9.6 to 3.5 flips per frame pair.
- Packages shipped key residue (P0-3): residue is now refused; the old forge-cycle packages (70-83% ring spill) fail the gate.
- Invalid loop candidates (P0-4): six of eight old helper candidates on the report clip are listed as rejected with reasons.
- Body-scale pumping from alpha-bound registration; motion-killing prompt words are linted.
- Alpha haze drove geometry (S01, DOC-04: 67,907 faint pixels on the fox sheet removed); feet hung 2-3 px below the origin (S14); static-body shimmer (S05); uneven nearest pixels (S06); 16-bit grey turned white and animated input silently used frame 0 (S16); 22-29 s pure-Python keyer (S03).
- Props floated 0-8 px (MAP-02); magenta fringe on props 2,419 to 111 tinted px (MAP-03); raw-y sort drew short bushes over trees (MAP-05); banker's rounding moved props (MAP-21); outputs could overwrite inputs (MAP-22); absolute paths in manifests (MAP-24); hidden terrain runtime defaults (MAP-16, DOC-14); gutters passed strict QC (MAP-10); iso atlases rejected (MAP-11); seam metric failed true joins (MAP-14).
- No engine-usable map data (MAP-01, DOC-05, F-07): validated bundles, collision from data, reachability and Tiled/Godot/LDtk exports.
- A paid image was deleted on volumes without hard links (F-04); a paid success crashed a cp1252 console (F-14); provider error reasons were hidden (F-05, issue #11); the 180 s submit timeout was too short (F-16).
- Codex Desktop images that were not saved locally can be adopted (F-13, issue #4); stale skill installs are detected (F-11); an installed `grok.exe` is no longer mistaken for a video route.
- WSL `/mnt/c` publishing (F-03); `--help` crashed under cp1252 (report v2 P1-1); Pillow 12.3 wrote a corrupt GIF after a blank frame; libwebp 1.6 dropped the alpha flag of cropped previews; ffmpeg 8 left packed MP4 colour tags unspecified; WebM bytes changed on every run.
- Code-art probe findings: complex numbers and NaN in SVG geometry, clipPath id collisions between batched frames, outline gaps after finishing, 2.43 px foot slide, a 4 px shore colour jump between water tiles, and road-to-water sets that forced a grass strip.
- Sprite Sprite3D contract paths did not resolve (S19); Aseprite/Godot timing had no tool (DOC-13); odd-width packed videos were cropped at the wrong half.

## 0.3 and earlier

Grok Build and API video routes, packed-alpha video packaging and the 2026-10-05 audit fixes are described in [docs/upgrade-audit-2026-10-05.zh-TW.md](./docs/upgrade-audit-2026-10-05.zh-TW.md) and [docs/validation-2026-10-05.md](./docs/validation-2026-10-05.md).
