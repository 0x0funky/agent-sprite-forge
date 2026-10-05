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
| `REPO_ROOT`, `SKILLS_DIR`, `SHARED_DIR`, `SHARED_SCHEMAS_DIR`, `FIXTURES_DIR`, `REAL_FIXTURES_DIR` | paths |
| `script_path(skill, name) -> Path` | `skills/<skill>/scripts/<name>.py` (raises if missing) |
| `load_script(skill, name, *, fresh=False) -> module` | import a skill script by path; cached in `sys.modules` as `forge_<skill>_<name>`; `fresh=True` runs a new copy that is registered only while it executes (dataclasses need that) and leaves the cached module in place |
| `load_shared(name, *, fresh=False) -> module` | the same for a canonical `shared/<name>.py` (`forge_core`, `forge_matte`, `forge_av`, ...), cached as `forge_shared_<name>`; replaces the private `_local_load*` loaders of A1 and A2 at integration |
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
- generate2dmedia: `| Read job.json, ledger lines, a batch progress file, doctor output or prices.json | references/schemas/media.schema.json ($defs job_v2, ledger_line_v1, batch_progress_v1, doctor_v1, prices_v1) |`

Shared types (fileRef, qaEnvelope, artSource, provenance, boxes, colours, events, fps, seamReport) are in `references/schemas/common.schema.json` in every skill.

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

- Added: frozen JSON Schema 2020-12 contracts for all five skills in `shared/schemas/` (common, sprite, video, map, codeart, media; 97 `$defs`), vendored into `skills/*/references/schemas/` (A0-T5; report v2 P0-2).
- Added: `shared/VENDORED.json` and `tools/vendor_sync.py --check/--write/--strict`, one canonical per shared file with a sha256 sync test (A0-T6; S11, MAP-12).
- Added: `pytest.ini` with markers perf, bench (opt-in: `-m bench`), ffmpeg, node, resvg, e2e; bench tests are deselected by default (A0-T2).
- Added: `tests/forge_testutils.py` shared test helpers and contract validation (A0-T4).
- Added: real-art regression fixtures with `tests/fixtures/real/PROVENANCE.json` (A0-T7; F-02).
- Changed: Pillow floor raised to 10.1 (S13); scipy added to `requirements.txt` (owner decision 3); new `requirements-codeart.txt`; `requirements-dev.txt` adds jsonschema and the codeart pin (A0-T1).
- Changed: tests reorganised so every file has one owner: `test_generate2dmap_pipeline.py` split into `test_compose_layered_preview.py`, `test_extract_prop_pack.py`, `test_validate_parallax.py`, `test_extract_platform_strip.py`; the engine_export tests live in `test_engine_export.py` (A0-T3).
- BREAKING: none from A0. The requirement floors belong to the plan Appendix H "repo" row (no legacy switch).
- Fixed: F-02 (fixtures existed only in git-ignored outputs/), S13 (Pillow floor).

Reconciliation with the Wave A producers (post-review; see section 8):

- Added: `media.schema.json` `batch_progress_v1` (`generate2dmedia.batch_progress.v1`, the `generate_media.py batch --execute` progress file) and `common.schema.json` `seamReport` (`forge_core.seam_report`).
- Added: PixelSpec building blocks `pixelspecColor`, `pixelspecPalette`, `pixelSegments` and `pixelGrid` in `codeart.schema.json`.
- Added: `forge_testutils.load_shared(name, *, fresh=False)` and `SHARED_DIR`, to test the canonical `shared/*.py` modules.
- Changed: `job_v2` accepts unpriced and purpose-less jobs (`estimate.usd` and `receipt.purpose` may be null) and documents A4's `route`, `apiBase`, `consent`, `ledger`, `clientRequestId`, `providerRequestId`, `uploadUrl`, `partialArtifact`, `error`, `providerUsage`, `estimate.currency` and `estimate.items`; `ledger_line_v1.reservedUsd` may be null and `reservationId` is required; `prices_v1` gains `version`, `currency`, `notes` and the row qualifiers `resolution`, `quality`, `size`.
- Changed: `pixelspec_v1` follows the form `codeart_core.render_pixelspec` reads (A3 section 5): only `schema`, `canvas`, `palette` and `layers` are required; poses may be rows, `{rows}` or `{segments}`; `z` is any number; frames may be unnamed and shift every layer with `dx`/`dy`; layers may be `hidden` or take their rows from frames; selout outlines and their `map` are described; colours may be `#rgb`, `#rgba` or omit `#`. `codeart.pixelspec.v1` stays accepted as the legacy alias of `codeart2d.pixelspec.v1`.
- Changed: `codeart_meta_v1.palette` also accepts `{colors, variants}` (what `write_codeart_meta` writes), `renderer` documents the rasterizer extras (`backend`, `engine`, `zoom`, `size`) and `disclosure` is listed; `qa` stays a common `qaEnvelope`.
- Changed: `pipeline_meta_v2` documents the full `forge_core.alpha_hygiene` report (`solid_min`, `attach_radius`, `connectivity`, `max_removed_alpha`; `floor` up to 255), `provenance.bit_depth` and `.conversion`, and an optional `matte` block (`forge_matte.key_still` info); `matte_report_v1` documents `key_estimate`, `interior_despill`, `params`, `local_background`, `temporal` and `key_hued_px`; `character_profile_v1.matte.params` pins KeyParams.
- Fixed: `load_script(..., fresh=True)` failed on modules that define dataclasses under `from __future__ import annotations` (forge_matte's `KeyParams`); a fresh copy is now registered while it executes.

## 5. Schema change requests

None: A0 owns the schemas. Modules that need a change file a diff here in their own handoff; integration applies it to `shared/schemas/` and runs `tools/vendor_sync.py --write`. The section 5 requests of A1, A2, A3, A4 and A5 have been applied (see "Reconciliation (post-review)" in section 8).

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
| codeart/pixelspec_v1 | `codeart2d.pixelspec.v1` (canonical; the legacy alias `codeart.pixelspec.v1` of roadmap 4.5 and the prototype stays accepted) | agent | A3 render_pixelspec, B18 |
| codeart/rig_anim_v1, fx_v1, material_spec_v1 | `codeart2d.rig_anim.v1`, `codeart2d.fx.v1`, `codeart2d.material_spec.v1` | agent | B19, B20 |
| codeart/codeart_meta_v1 | `codeart2d.codeart_meta.v1` | A3 write_codeart_meta, B18-B21 | agent, Z disclosure |
| media/job_v2 | `schemaVersion` 2 (1 stays valid and resumable) | A4 | A4, B22 |
| media/ledger_line_v1 | optional `generate2dmedia.ledger_line.v1` (JSONL line) | A4 media_ledger | A4, B22 |
| media/batch_progress_v1 | `generate2dmedia.batch_progress.v1` | A4 `generate_media.py batch --execute` | agent |
| media/doctor_v1, prices_v1 | `generate2dmedia.doctor.v1`, `generate2dmedia.prices.v1` | B22, A4 | agent, A4 |
| common/seamReport | none (embedded in QA envelopes and reports) | forge_core.seam_report in loop and seam QA (B07, B08, B11, B12, B16) | agent, Z docs |

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
- Semantic guards: review verdicts other than `accepted` name an issue; a rejected take names a reason; doctor checks with FAIL or MISSING carry a remedy; a `done` job records its artifact; a v1 clips manifest needs `duration_ms` and `loop` per clip; per-frame anchors in clips manifests are refused; a `generated` batch result names its artifact, a stopped batch has a `stopReason` and a `complete` one has nothing in flight or remaining; solid and selout PixelSpec outlines need `color`, and selout also a non-empty `map`.
- job_v2 `status` is the cfed170 set plus `not_sent`; `fingerprint` is a sha256 hex digest. `artifact` is never in `required`: it appears when the job is done. `route` takes the ledger's four routes (generate_media.py writes `rest`; CLI routes are B22's). Null means unknown or unpriced: `estimate.usd`, `consent.estimateUsd`, `ledger_line_v1.reservedUsd` and `actualUsd`, and the receipt's `submittedAt`, `completedAt`, `wallMs`, `providerMs`, `purpose` and `outcomeCode`. `estimate.currency` and `prices_v1.currency` are `USD` (amounts are named `usd`).
- The job's provider `error` is the one closed object (`additionalProperties: false`): only the whitelisted, scrubbed `httpStatus`, `code`, `type`, `param`, `message` (at most 300 characters) and `requestId`, so a raw provider body cannot be stored there.
- `prices_v1.verifiedAt` is a calendar date `YYYY-MM-DD` (A4's `load_prices` refuses date-times); the A0 draft also allowed a date-time, which no producer wrote.
- `ledger_line_v1.reservationId` is required: readers fold lines per reservation and skip lines without one.
- `matte_report_v1.key` stays `common/keyColor` (a name, `#rrggbb` or integer RGB). forge_matte reports keys as float RGB (`matte_qa`, `key_still`, `estimate_key`), so B05 rounds the key it writes there; the floats belong in `key_estimate.key`. Integral floats such as `255.0` already validate.
- PixelSpec: the schema and `codeart_core.render_pixelspec` agree on one form. B18 should validate a spec against `pixelspec_v1` before rendering, because the renderer is more lenient than the contract in four ways: integer RGB palette entries, a pose given as another pose's name (an alias), a pose object with both `rows` and `segments` (segments win), and truthy non-boolean flags. Those are not part of the contract.
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
- The contracts are proven against hand-written examples (one valid document and 1 to 13 targeted negative cases per `$def`, 325 in total; 64 of them name the error they must produce), against 17 further valid documents (`*.valid-<variant>.json`, most of them Wave A producer output) and against documents produced by the cfed170 writers (pipeline-meta, scale profile, clips input and output, prop pack, animation.json 2.0 in PNG-only and webm+packed variants, media job). No Wave B producer exists yet; expect change requests.
- JSON Schema cannot express cross-field rules: `len(durationsMs) == frameCount`, `impactMs <= durationMs`, `x0 < x1`, PixelSpec row characters present in the palette, PixelSpec pose, layer and frame names that exist, variant keys present in the base palette, unique ids, frame indices in range. Validators in B08, B13 and B18 must check those.

### Reconciliation (post-review)

An independent review merged Wave A in the planned order (A0, A1, A5, A3, A2, A4) and found that A3 and A4 output did not match these schemas. This commit applies every section 5 request of A1-A5, additively: every valid and legacy fixture of the first A0 commit still validates. Invalid cases changed only where a decision relaxed a rule (the PixelSpec `anchor_px`, layer rows-or-segments and frame `name` requirements).

- media: `job_v2` accepts `estimate.usd: null` (unpriced: OpenAI has no price row) and `receipt.purpose: null`, and documents every other field of A4 section 5 (`artifact` stays conditional on `done`, never in `required`); `ledger_line_v1.reservedUsd` may be null and `reservationId` is required; `prices_v1` keeps its id `generate2dmedia.prices.v1` (A4 renamed its file to it in f1070d1) and gains `version`, `currency`, `notes`, row qualifiers and a date-only `verifiedAt`; new `batch_progress_v1` with the id `generate2dmedia.batch_progress.v1` (A4 writes it since f1070d1).
- codeart: `codeart_meta_v1.palette` gains the `{colors, variants}` branch that `write_codeart_meta` writes, `renderer` the rasterizer extras, plus `disclosure`; `qa` stays a common `qaEnvelope` (A3 now emits one: fc833f8). `pixelspec_v1` follows A3 section 5 and the renderer (pixelGrid, pixelSegments, pixelspecPalette; required only `schema`, `canvas`, `palette`, `layers`; `z` a number; legacy alias kept). Where A0 was stricter than the renderer it was relaxed; `name`, `anchor_px` and `clips`, which the renderer ignores, stay optional and say so. Two rules are tighter than A3's sketch because the renderer enforces them: run counts of 0 are refused, and selout needs `color`.
- sprite: `pipeline_meta_v2` gains A1's hygiene and provenance fields and A2's optional `matte` block. common: A1's `seamReport`. video: A2's `matte_report_v1` additions and `character_profile_v1.matte.params`; A5's fps request needed no change, because `animation_v3` `fps`, `packedAlpha.fps` and `mobilePackedAlpha[].fps` already use `common/fpsValue` (a positive number or `N/D`); a `30000/1001` fixture and a negative case now prove it.
- Tests: `test_contracts.py` accepts `<domain>.<def>.valid-<variant>.json` fixtures (validated by `test_variant_examples_validate`), and a negative case may name its expected error (`"error"`: a substring of one `$.path: message` line), which every new case does. `load_shared` and the `fresh=True` fix are tested with a temporary shared folder and, once they land, with each canonical in `shared/VENDORED.json`.
- Verified with producer output, not only fixtures: documents written by A4's `generate_media.py` (done unpriced, done priced, interrupted video with `uploadUrl`, failed with a provider `error`, `partialArtifact`, done video), its ledger lines and both progress files; A3's `write_codeart_meta` (PixelSpec and resvg renderer) and `slime.pixelspec.json`; A1's `alpha_hygiene`, `load_rgba` and `seam_report` reports; A2's `key_still`, `estimate_key`, `matte_qa` and `auto_interior_despill`; A5's `encode_packed_alpha` result at `30000/1001`. All validate. A 34-spec matrix run through A3's `render_pixelspec` agrees with `pixelspec_v1` except the four renderer leniencies of section 5 and two cross-field rules.
- Merge simulation (private scratch clone): this commit plus asf/A1-forge-core 21d3533, A5-forge-av c1d1b95, A3-codeart-core fc833f8, A2-forge-matte 7e80804 and A4-media-safety f1070d1, merged in that order: STD green after every merge (674 passed, 2 skipped at the end), `vendor_sync --check` exit 0 after every merge, `--strict` reports only `shared/forge_palette.py` (B04). With the earlier A3 eb82722 and A4 38ad78e the only failures left were theirs: raw `qa_pixels` metrics as `qa` and the old `prices_v1` id.
- Not proven: `vendor_sync --check --strict` in this worktree alone reports four missing canonicals (forge_core, forge_matte, forge_av, forge_palette) because A1, A2 and A5 are not merged into this branch. B01, B05, B07, B08, B16 and B22 have not written any of the new optional blocks yet; their types follow the A1, A2 and A5 libraries as of the commits above.
- `map_bundle.v1` never existed in cfed170; v1 is recognised only by its schema key. B13 decides what its v1 reader accepts.
- The `$id` URLs resolve only after the owner pushes `main`.
- Additions beyond the plan's lists: `forge_testutils` also exports `script_path`, `assert_cli_help`, `real_fixture`, the contract helpers and path constants; `pytest.ini` sets `minversion = 8.0` and repeats pytest's default `norecursedirs` (setting the option replaces them); `tests/test_contracts.py` also covers the shared helpers and real-fixture provenance, since no separate test file was allotted. The engine_export copy keeps the original class names and drops the unused `subprocess` import.
