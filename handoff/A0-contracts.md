# A0-contracts: frozen contracts, vendoring tool, dependency pins, test partitioning, real fixtures

Branch `asf/A0-contracts` (from `wip/asf-upgrade-20261005` @ cfed170). Everything below is in this worktree.

## 1. CLIs

Repository tooling (run from the repository root, not a user's project):

    python tools/vendor_sync.py --check
    python tools/vendor_sync.py --write
    python tools/vendor_sync.py --check --strict

- `--check` (the default) exits 1 on any drift and prints one line per problem to stderr (`stale copy: ...`, `missing copy: ...`, `copy without canonical: ...`, `unlisted copy: ...`, `missing canonical: ...`), then `error: N vendoring problem(s)`. On success it prints a one-line JSON summary: `status`, `mode`, `strict`, `copies_in_sync`, `written`, `pending_canonicals`.
- `--write` copies each canonical's exact bytes over stale or missing copies (atomic replace) and never edits a canonical. It cannot fix a copy without its canonical or an unlisted copy, and exits 1 for those.
- `--strict` (or `FORGE_VENDOR_STRICT=1`) also requires every canonical to exist. Without it, a canonical whose Wave A module has not landed is reported under `pending_canonicals`.
- Text files are hashed with CRLF normalised to LF, so Windows autocrlf and Linux checkouts agree.
- `--help` is ASCII and works under cp1252 and cp950 (tested).

Test library (not a CLI): `tests/forge_testutils.py`, frozen after Wave A. Public API:

| Name | Purpose |
|---|---|
| `REPO_ROOT`, `SKILLS_DIR`, `SHARED_SCHEMAS_DIR`, `FIXTURES_DIR`, `REAL_FIXTURES_DIR` | paths |
| `script_path(skill, name) -> Path` | `skills/<skill>/scripts/<name>.py` (raises if missing) |
| `load_script(skill, name, *, fresh=False) -> module` | import a skill script by path; cached in `sys.modules` as `forge_<skill>_<name>` unless `fresh=True` |
| `run_cli(argv, encoding=None, *, cwd, env, timeout=300, input_text) -> CompletedProcess[str]` | runs `python <argv...>` with `PYTHONIOENCODING=<encoding or utf-8>` and `PYTHONDONTWRITEBYTECODE=1`; decodes with the same codec |
| `assert_cli_help(skill, name, encodings=("cp1252", "cp950"))` | the required `--help` test: exit 0, non-empty, ASCII-only |
| `make_magenta_sheet(rows=2, cols=2, cell=64, *, key, color, outline, margin=12, fringe=False, spill_px=0, return_boxes=False)` | deterministic opaque chroma sheet; `fringe` adds a 50/50 key-mixed ring; `spill_px` makes a cross-cell tail; `return_boxes` adds the subject boxes |
| `require_ffmpeg(*, ffprobe=True)`, `require_node()`, `require_resvg()` | return the tool (path or module) or raise `unittest.SkipTest` (a skip in pytest and unittest) |
| `real_fixture(name) -> Path` | a real-art fixture, refused unless listed in `tests/fixtures/real/PROVENANCE.json` with a matching sha256 |
| `contract_validator(domain, name, *, skill=None)`, `contract_errors(...) -> list[str]`, `assert_valid_contract(instance, domain, name, *, skill=None)` | validate a document against `shared/schemas/<domain>.schema.json#/$defs/<name>`; `skill="generate2dmap"` uses that skill's vendored copy instead |

Use with the pytest markers: `@pytest.mark.ffmpeg` plus `require_ffmpeg()`, `@pytest.mark.node` plus `require_node()`, `@pytest.mark.resvg` plus `require_resvg()`.

## 2. SKILL.md routing rows

One row per skill, for a "Contracts" or "Data files" section:

- generate2dsprite: `| Check a clips manifest, pipeline-meta, scale profile, sheet QC or palette file | references/schemas/sprite.schema.json ($defs clips_input, animation_clips_v2, pipeline_meta_v2, scale_profile_v2, sheet_qc_v1, scale_frames_v1, palette_v1, palette_lock_v1) |`
- video2dsprite: `| Check a registration job, character profile, frame selection, matte or triage report, take, review verdict or animation.json | references/schemas/video.schema.json ($defs registration_job_v1, character_profile_v1, frame_selection_v2, matte_report_v1, raw_triage_v1, take_v1, review_verdict_v1, animation_v3) |`
- generate2dmap: `| Check a prop pack, placements, map bundle, tileset, stage, motion plan, lights, atmosphere, layout, room chunks or parallax plan | references/schemas/map.schema.json ($defs prop_pack_v2, placements_v2, map_bundle_v2, tileset_v1, stage_v1, motion_plan_v1, atmosphere_v1, lights_v1, layout_v1, room_chunk_v1, parallax_plan) |`
- codeart2d: `| Write a PixelSpec, rig animation, FX or material spec; read codeart-meta.json | references/schemas/codeart.schema.json ($defs pixelspec_v1, rig_anim_v1, fx_v1, material_spec_v1, codeart_meta_v1); frames and tiles then follow sprite.schema.json and map.schema.json |`
- generate2dmedia: `| Read job.json, ledger lines, doctor output or prices.json | references/schemas/media.schema.json ($defs job_v2, ledger_line_v1, doctor_v1, prices_v1) |`

Shared types (fileRef, qaEnvelope, artSource, provenance, boxes, colours, events, fps) are in `references/schemas/common.schema.json` in every skill.

## 3. README tool-table rows

Requirements section:

| Need | Install |
|---|---|
| Every skill | Python 3.10+, `python -m pip install -r requirements.txt` (numpy>=1.26, Pillow>=10.1, scipy>=1.11; scipy is an accelerator with a numpy fallback in forge_core) |
| codeart2d SVG rasterising | `python -m pip install -r requirements-codeart.txt` (resvg-py>=0.5,<0.6; PixelSpec needs only requirements.txt) |
| Contributors | `python -m pip install -r requirements-dev.txt` (adds pytest>=8, PyYAML>=6, jsonschema>=4.18 and the codeart pin) |

Repository tools table:

| Tool | What it does | Verified by |
|---|---|---|
| `tools/vendor_sync.py` | Checks and repairs the byte-identical copies of `shared/` modules and schemas inside each skill | `tests/test_vendored_sync.py` |

## 4. CHANGELOG entries

- Added: frozen JSON Schema 2020-12 contracts for all five skills in `shared/schemas/` (common, sprite, video, map, codeart, media; 91 `$defs`), vendored into `skills/*/references/schemas/` (A0-T5; report v2 P0-2).
- Added: `shared/VENDORED.json` and `tools/vendor_sync.py --check/--write/--strict`, one canonical per shared file with a sha256 sync test (A0-T6; S11, MAP-12).
- Added: `pytest.ini` with markers perf, bench (opt-in: `-m bench`), ffmpeg, node, resvg, e2e; bench tests are deselected by default (A0-T2).
- Added: `tests/forge_testutils.py` shared test helpers and contract validation (A0-T4).
- Added: real-art regression fixtures with `tests/fixtures/real/PROVENANCE.json` (A0-T7; F-02).
- Changed: Pillow floor raised to 10.1 (S13); scipy added to `requirements.txt` (owner decision 3); new `requirements-codeart.txt`; `requirements-dev.txt` adds jsonschema and the codeart pin (A0-T1).
- Changed: tests reorganised so every file has one owner: `test_generate2dmap_pipeline.py` split into `test_compose_layered_preview.py`, `test_extract_prop_pack.py`, `test_validate_parallax.py`, `test_extract_platform_strip.py`; the engine_export tests live in `test_engine_export.py` (A0-T3).
- BREAKING: none from A0. The requirement floors belong to the plan Appendix H "repo" row (no legacy switch).
- Fixed: F-02 (fixtures existed only in git-ignored outputs/), S13 (Pillow floor).

## 5. Schema change requests

None: A0 owns the schemas. Modules that need a change file a diff here in their own handoff; integration applies it to `shared/schemas/` and runs `tools/vendor_sync.py --write`.

Contract catalogue for producers and consumers (document id = the `schema` key unless noted):

| $def | Document id | Written by | Read by |
|---|---|---|---|
| sprite/clips_input | `generate2dsprite.animation_clips.v1` or `.v2` (missing means v1) | agent, B03 `--emit-clips`, B18, B19 | B02 build_animation_clips, B09 |
| sprite/animation_clips_v2 | `generate2dsprite.animation_clips.v2` (v1 output stays valid) | B02 | B09 |
| sprite/pipeline_meta_v2 | `generate2dsprite.pipeline_meta.v2` (v1 has no key) | B01 | B01 profiles, B03 |
| sprite/scale_profile_v2 | `version` 2 (1 stays valid); optional `generate2dsprite.scale_profile.v2` | B01 | B01 |
| sprite/sheet_qc_v1 | `generate2dsprite.sheet_qc.v1` | B03 | agent |
| sprite/scale_frames_v1 | `generate2dsprite.scale_frames.v1` | B03 | agent, B02 |
| sprite/palette_v1, palette_lock_v1 | `generate2dsprite.palette.v1`, `generate2dsprite.palette_lock.v1` | B04 | B03 prompt rules, B04 |
| video/registration_job_v1 | `video2dsprite.registration_job.v1` | B06 prepare_i2v_input | B06 register_clip, B08 |
| video/character_profile_v1 | `video2dsprite.character_profile.v1` | B06 | B05 `--matte-profile`, B06 |
| video/frame_selection_v2 | `forge-frame-selection/v2` (`/v1` stays valid) | B07 gait_loop, retime, review | B07 cut, B08 `--selection` |
| video/matte_report_v1 | `video2dsprite.matte_report.v1` | B05 (pipeline-meta matte block) | B08 |
| video/raw_triage_v1 | `video2dsprite.raw_triage.v1` | B05 triage | agent |
| video/take_v1 | optional `video2dsprite.take.v1` (JSONL line) | B06 qc | agent |
| video/review_verdict_v1 | `video2dsprite.review_verdict.v1` | agent or human review | B08, B16 |
| video/animation_v3 | `schemaVersion` "3.0" ("2.0" stays valid) | B08 | B09 runtime, games |
| map/prop_pack_v2 | `generate2dmap.prop_pack.v2` (v1 has no key) | B10 | B12 compose, B13 |
| map/placements_v2 | `generate2dmap.placements.v2` (v1 object or bare list stays valid) | agent | B12 |
| map/map_bundle_v2 | `generate2dmap.map_bundle.v2` (`.v1` key accepted) | B21 layout_build, agent | B13 map_bundle/map_nav/export_tiled, B14, B17 |
| map/tileset_v1 | `generate2dmap.tileset.v1` | B20 autotile_build | B13 export_tiled, B14 |
| map/stage_v1, lights_v1, atmosphere_v1 | `generate2dmap.stage.v1`, `.lights.v1`, `.atmosphere.v1` | B15 | B15, B17 |
| map/motion_plan_v1 | `generate2dmap.motion_plan.v1` | agent | B16 |
| map/layout_v1, room_chunk_v1 | `generate2dmap.layout.v1`, `generate2dmap.room_chunk.v1` | agent | B14 |
| map/parallax_plan | optional `generate2dmap.parallax_plan.v1` | agent, B21 parallax_build | B12 validate_parallax |
| codeart/pixelspec_v1 | `codeart2d.pixelspec.v1` (prototype `codeart.pixelspec.v1` accepted for reading only) | agent | A3 render_pixelspec, B18 |
| codeart/rig_anim_v1, fx_v1, material_spec_v1 | `codeart2d.rig_anim.v1`, `codeart2d.fx.v1`, `codeart2d.material_spec.v1` | agent | B19, B20 |
| codeart/codeart_meta_v1 | `codeart2d.codeart_meta.v1` | A3 write_codeart_meta, B18-B21 | agent, Z disclosure |
| media/job_v2 | `schemaVersion` 2 (1 stays valid and resumable) | A4 | A4, B22 |
| media/ledger_line_v1 | optional `generate2dmedia.ledger_line.v1` (JSONL line) | A4 media_ledger | A4, B22 |
| media/doctor_v1, prices_v1 | `generate2dmedia.doctor.v1`, `generate2dmedia.prices.v1` | B22, A4 | agent, A4 |

Decisions taken where plan Appendix B was silent (binding unless a module files a change request):

- Every new document type requires its `schema` key, except JSONL lines (take_v1, ledger_line_v1) and parallax_plan (legacy plans have none). job_v2 and animation_v3 use `schemaVersion`, scale profiles `version`.
- Pixel boxes are half-open `[x0, y0, x1, y1)` (source_rect, cell_box, workRegion, review issue boxes, motion boxes). `[x, y, w, h]` (`rectXYWH`) is used only for animation.json `sourceRect` (a 2.0 key), map portals `rect`, collision `rects` and camera `bounds`; solid rects use fields `x, y, w, h`; ellipses `cx, cy, rx, ry`; portal circles `[cx, cy, r]`.
- `fileRef.path` and every other path field are manifest-relative POSIX (no drive letter, leading slash, backslash or URL). An input on another drive records its file name plus sha256.
- QA: `sheet_qc_v1` and `raw_triage_v1` are QA envelopes themselves; `animation_v3.qa` (3.0) and `codeart_meta_v1.qa` hold one. `createdAt` is optional so QA files can stay byte-deterministic. A `pass` envelope cannot contain a failed check. Checks may also be `skipped`.
- Events: clips input `{at: clip position, name}`; built clips `events_ms[{name, at_ms}]`; video documents `{name, atMs, frame?}`; rig clips `{t: 0..1, name}`. Names come from `common/eventName` (`custom:` prefix for others).
- `frame_selection_v2` keeps the Appendix B spelling `durations_ms`; `animation_v3` uses `durationsMs`.
- Tilesets: `wang` is `[top_left, top_right, bottom_left, bottom_right]` material indices (the order of design/proto/autotile.py); `blob_mask` sets bit i for neighbour i of N, NE, E, SE, S, SW, W, NW. `seamless_verified: true` requires `seam_proof` with `mismatches` 0 over at least one pixel.
- Stage, slots and lights are UV in [0, 1]; motion plans are in source pixels.
- matte_report_v1: pixel counts are sums over frames, fractions the worst frame.
- Semantic guards: review verdicts other than `accepted` name an issue; a rejected take names a reason; doctor checks with FAIL or MISSING carry a remedy; a `done` job records its artifact; a v1 clips manifest needs `duration_ms` and `loop` per clip; per-frame anchors in clips manifests are refused.
- job_v2 `status` is the cfed170 set plus `not_sent`; `fingerprint` is a sha256 hex digest.
- `$id`s are `https://raw.githubusercontent.com/0x0funky/agent-sprite-forge/main/shared/schemas/<domain>.schema.json` and cross-file `$ref`s are relative (`common.schema.json#/$defs/fileRef`), so each skill's folder resolves on its own; tests never fetch them.

## 6. Shared-helper promotion requests

- `tools/vendor_sync.py::_local_utf8_stdio()` reconfigures stdout and stderr with `errors="backslashreplace"`. Recommendation: keep it local; the vendoring tool must stay stdlib-only and run before `shared/forge_core.py` exists. No promotion needed.

## 7. Cross-module links that Z must add

- CONTRIBUTING.md: shared/ is canonical, never edit vendored copies by hand; run `python tools/vendor_sync.py --write` then `--check`; schemas live in shared/schemas and are vendored into each skill's references/schemas; pytest markers (bench is opt-in with `-m bench`); real fixtures need an entry in tests/fixtures/real/PROVENANCE.json and are used through `forge_testutils.real_fixture()`; contract checks use `forge_testutils.assert_valid_contract()`.
- CI (Z-T6): install `requirements-dev.txt`; run `python tools/vendor_sync.py --check`; add a strict job with `FORGE_VENDOR_STRICT=1` once A1, A2, A5 and B04 have landed; `pytest.ini` already scopes collection to tests/ and deselects bench.
- `.gitattributes` (Z-T7): mark `*.png` binary. vendor_sync already tolerates CRLF, so `eol` rules are optional for it.
- Each SKILL.md: link its `references/schemas/*.schema.json` (they exist in this worktree).
- `tests/test_skill_packages.py` fails for codeart2d until Z adds `skills/codeart2d/SKILL.md` (the folder now exists for its vendored schemas).
- B05 must delete the 13 engine_export tests from `tests/test_video2dsprite.py` (they now live in `tests/test_engine_export.py`, owned by B08).

## 8. Known limitations and what is not proven

- A0-T1 "a clean Python 3.10 venv installs requirements-dev.txt" was not run: only Python 3.13 is installed here and installs need the network. Verified instead: the prepared venv satisfies every pin, `import scipy, resvg_py, jsonschema` works, Pillow is 12.3, resvg-py ships a `cp310-abi3` wheel, and all new Python files parse with the 3.10 grammar.
- Deviation, A0-T7: the meadow fixture is the top two rows (6 of 9 cells, 1254x836, crop box `[0, 0, 1254, 836]`) of `src/godot-meadow-prop-pack.png`, not all 3x3 cells. The full sheet (1.78 MB, 1.68 MB re-encoded) plus the byte-copied fox sheet (1.55 MB) exceeds the 3 MB budget. The crop keeps the 3-column, 418 px grid and reproduces the MAP-03 wheat-grass numbers exactly (2419 tinted px with the cfed170 map keyer, 111 at despill radius 1, 36 at radius 2). Real fixtures total 2,688,463 bytes.
- The contracts are proven against hand-written examples (one valid document and 1 to 7 targeted negative cases per `$def`, 264 in total) and against documents produced by the cfed170 writers (pipeline-meta, scale profile, clips input and output, prop pack, animation.json 2.0 in PNG-only and webm+packed variants, media job). No Wave B producer exists yet; expect change requests.
- JSON Schema cannot express cross-field rules: `len(durationsMs) == frameCount`, `impactMs <= durationMs`, `x0 < x1`, PixelSpec row characters present in the palette, unique ids, frame indices in range. Validators in B08, B13 and B18 must check those.
- `map_bundle.v1` never existed in cfed170; v1 is recognised only by its schema key. B13 decides what its v1 reader accepts.
- The `$id` URLs resolve only after the owner pushes `main`.
- Additions beyond the plan's lists: `forge_testutils` also exports `script_path`, `assert_cli_help`, `real_fixture`, the contract helpers and path constants; `pytest.ini` sets `minversion = 8.0` and repeats pytest's default `norecursedirs` (setting the option replaces them); `tests/test_contracts.py` also covers the shared helpers and real-fixture provenance, since no separate test file was allotted. The engine_export copy keeps the original class names and drops the unused `subprocess` import.
