# B18-codeart-pixel-cli: codeart2d P0 CLIs (render_pixelspec, svg_render render/lint/doctor, pixel_qa), three examples, four references

Branch `asf/B18-codeart-pixel-cli` (from `wip/asf-upgrade-20261005` @ 3f9252d).
New files: [render_pixelspec.py](../skills/codeart2d/scripts/render_pixelspec.py),
[svg_render.py](../skills/codeart2d/scripts/svg_render.py),
[pixel_qa.py](../skills/codeart2d/scripts/pixel_qa.py), the examples
[slime.pixelspec.json](../skills/codeart2d/examples/slime.pixelspec.json),
[walker16x24.pixelspec.json](../skills/codeart2d/examples/walker16x24.pixelspec.json),
[potion-icons.svg](../skills/codeart2d/examples/potion-icons.svg) and
[palette.json](../skills/codeart2d/examples/palette.json), the references
[pixelspec.md](../skills/codeart2d/references/pixelspec.md),
[svg-profile.md](../skills/codeart2d/references/svg-profile.md),
[rasterizers.md](../skills/codeart2d/references/rasterizers.md) and
[style-envelope.md](../skills/codeart2d/references/style-envelope.md), and the tests
[test_codeart2d_pixelspec.py](../tests/test_codeart2d_pixelspec.py) (60 tests) and
[test_codeart2d_svg.py](../tests/test_codeart2d_svg.py) (24 tests).

## 1. CLIs

All commands run from the user's project root; outputs are new paths inside the project
(every tool refuses to write inside the codeart2d skill folder). In Claude Code notes
`<skill-dir>` is `${CLAUDE_SKILL_DIR}`.

    python "<skill-dir>/scripts/render_pixelspec.py" --spec art/slime.pixelspec.json --output-dir out/slime-v1
    python "<skill-dir>/scripts/render_pixelspec.py" --spec art/slime.pixelspec.json --output-dir out/slime-v1 --variants all --build-clips --preview-scale 6 --strict-qc
    python "<skill-dir>/scripts/render_pixelspec.py" --spec art/walker.pixelspec.json --output-dir out/walker-v1 --variants forest,sky --clips-manifest
    python "<skill-dir>/scripts/svg_render.py" render --svg art/potion-icons.svg --palette art/palette.json --output-dir out/potions-v1 --zoom 4
    python "<skill-dir>/scripts/svg_render.py" render --svg art/potion-icons.svg --palette art/palette.json --output-dir out/potions-px --crisp --zoom 4 --anchor 16,31 --strict-qc
    python "<skill-dir>/scripts/svg_render.py" lint --svg art/potion-icons.svg --profile portable
    python "<skill-dir>/scripts/svg_render.py" lint --svg art/potion-icons.svg --compile --palette art/palette.json --variant mana
    python "<skill-dir>/scripts/svg_render.py" doctor --report out/doctor.json
    python "<skill-dir>/scripts/pixel_qa.py" --input "out/slime-v1/green/frames/*.png" --palette out/slime-v1/codeart-meta.json --variant green --outline "#1a1c2c" --detect-grid --anchor 16,31 --review out/slime-review.png --scales 1,2,4 --bg light,dark,checker --onion --strict --report out/slime-qa.json

`--help` (each tool, and `svg_render.py render|lint|doctor --help`) prints the module
docstring and options in ASCII under cp1252 and cp950 (tested), and still works when numpy
is missing. Errors: one `error: ...` line on stderr, exit 1, never a traceback; a missing
numpy/Pillow prints `install with: python -m pip install numpy Pillow` (only the missing
ones); no SVG backend prints `python -m pip install "resvg-py>=0.5,<0.6"`.

One-line ASCII JSON summary on stdout:

- render_pixelspec: `output`, `metadata` (codeart-meta.json), `qa`, `variants`, `frames` (distinct frame files per variant), `files`, and `bundles` with `--build-clips`.
- svg_render render: `output`, `metadata`, `qa`, `mode` (vector or crisp), `variants`, `files`, `renderer`.
- svg_render lint: `status` (pass or fail), `svg`, `profile`, `compiled`, `problems`; exit 1 when any problem (also listed on stderr).
- svg_render doctor: `status`, `primary`, `backends` ({name: pass, fail or unavailable}), `failed` (check ids), `report`; exit 1 unless the primary backend passes every case.
- pixel_qa: `status`, `frames`, `failed`, `warned`, `report`, `review`.

Behaviour summary:

- **render_pixelspec** validates the spec against the vendored `codeart.schema.json#/$defs/pixelspec_v1` (a built-in Draft 2020-12 validator, no jsonschema at run time) and then the cross-references a schema cannot express, before rendering. It writes `<variant>/frames/<frame>.png` (8-bit RGBA, RGB zeroed under alpha 0; frames that render identically in every variant share one file and clips reuse them by index), `<variant>/clips.json` (`--clips-manifest`; v1 unless a clip uses v2-only fields, then v2), `<variant>/bundle/` (`--build-clips`: `generate2dsprite/scripts/build_animation_clips.py` run by path, never `generate2dsprite.py process`), `preview-x<N>.png` (`--preview-scale N`) and `codeart-meta.json`. Empty frames are not written; trailing empty frames of a clip are dropped and their `duration_ms`/`ticks` added to the last visible frame; an empty frame earlier in a clip, or a frame without transparent pixels, is refused for clips. QA reads every PNG back: partial alpha 0, off-palette 0 and, with an outline, outline gaps 0 and L-corners at most 10 (`codeart_core.QA_PIXEL_GATES`); `--strict-qc` publishes nothing on failure.
- **svg_render render** compiles the SVG with the palette (one override `<style>` per variant), lints the compiled SVG (portable profile; pixel profile with `--crisp`) and stops on any problem, rasterizes it (`--backend auto|resvg_py|resvg_js_cli|chrome`), and writes `<variant>/<name>.svg` (the compiled SVG that was rendered), `<variant>/<name>.png` and, with `--crisp --zoom N`, `<variant>/<name>@Nx.png` (integer nearest of the 1x render). Crisp QA: partial alpha 0 and off-palette 0. Without `--palette` the palette is the `#hex` colours the SVG writes. `--anchor` (or the root `data-anchor`) is recorded per image at its scale.
- **svg_render doctor** wraps `codeart_core.run_doctor`; `--report` writes A3's report shape extended into a common QA envelope, even when the doctor fails; fallback-backend deviations are warnings.
- **pixel_qa** measures every input (any PNG mode), aggregates checks at the worst frame (partial_alpha, off_palette, outline_gaps, l_corners, anchor, same_size, grid, loop_seam), writes `--review` (codeart_core.review_sheet) and `--report` (QA envelope) without replacing files and with rollback; `--strict` exits 1 and writes nothing when a check fails; warnings (grid, loop_seam above 2.0, same_size) never fail.
- Large outputs are refused before allocation: previews, renders and review sheets above 64 Mpx.

## 2. SKILL.md routing rows

For `skills/codeart2d/SKILL.md` (Z):

| Need | Route |
|---|---|
| Pixel character, prop or FX up to 48 px visible height (49-64 px with consent), with palette variants | write a PixelSpec (references/pixelspec.md); `scripts/render_pixelspec.py --spec <spec> --output-dir <new> --build-clips --strict-qc`; review with `scripts/pixel_qa.py --review` |
| Flat vector character, prop, UI or icon set | write portable SVG with palette classes (references/svg-profile.md); `scripts/svg_render.py render --svg <svg> --palette <palette.json> --output-dir <new> --zoom N` |
| Crisp pixel art drawn with SVG shapes | `scripts/svg_render.py render --crisp --zoom N --strict-qc` (1x render, integer nearest upscale) |
| Check an SVG before rendering | `scripts/svg_render.py lint --svg <svg> --profile portable` (add `--compile --palette <p>` for what render rasterizes) |
| Check this machine's SVG renderers | `scripts/svg_render.py doctor --report <new.json>` once per machine (references/rasterizers.md) |
| QA and review any pixel frames (code art, image-model output, exports) | `scripts/pixel_qa.py --input "<glob>" --palette <palette, spec or codeart-meta.json> --review <new.png> --onion --strict --report <new.json>` |
| Decide whether code art fits the request; disclosure | references/style-envelope.md |

Hello-sprite quickstart for SKILL.md (answers issue #9), paste-ready:

```text
1. Write gem.pixelspec.json (the "Hello sprite" block in references/pixelspec.md).
2. python "<skill-dir>/scripts/render_pixelspec.py" --spec gem.pixelspec.json --output-dir out/gem-v1 --preview-scale 8 --strict-qc
3. Open out/gem-v1/preview-x8.png; the frames are in out/gem-v1/<variant>/frames/.
4. Tell the user it is code-drawn (no image model); codeart-meta.json records it.
```

Dependencies paragraph (adds to A3's): "PixelSpec rendering and pixel_qa need only numpy
and Pillow. svg_render needs one rasterizer (run its doctor once). jsonschema is not
needed at run time: render_pixelspec validates specs with its own reader of the vendored
schemas."

For `skills/generate2dsprite/SKILL.md` (art source `code`, roadmap 4.13):

| Need | Route |
|---|---|
| art_source code, pixel frames from a PixelSpec | codeart2d `render_pixelspec.py --build-clips` (calls this skill's build_animation_clips.py by path); never `generate2dsprite.py process` |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `codeart2d/scripts/render_pixelspec.py` | Validates a PixelSpec, renders checked 8-bit RGBA frames per palette variant (repeated poses stored once), writes clips manifests and builds clip bundles, codeart-meta.json with a QA envelope | `tests/test_codeart2d_pixelspec.py` (slime 4 frames x 3 variants build, byte-identical reruns, contract and cross-reference refusals, empty-tail merge, builder failure leaves nothing) |
| `codeart2d/scripts/svg_render.py` | `render`: palette-compiled, linted SVG to PNG (vector at any zoom, or crisp pixel art with nearest upscales); `lint`: portable/pixel profile; `doctor`: renderer conformance corpus | `tests/test_codeart2d_svg.py` (potion icons in 3 variants, crisp exactness and determinism, each lint code, doctor pass/deviation/fallback, pip hint with every backend blocked) |
| `codeart2d/scripts/pixel_qa.py` | Pixel-art QA over PNG frames (palette, alpha, outline, anchor, grid, loop seam) with a review sheet and a QA envelope | `tests/test_codeart2d_pixelspec.py` (strict fails on off-palette pixels and writes nothing; review sheet and report written) |

Requirements row (generated README section): "codeart2d PixelSpec and pixel QA:
numpy, Pillow (nothing else); codeart2d SVG: plus one rasterizer, resvg-py preferred".

## 4. CHANGELOG entries

- Added: `codeart2d/scripts/render_pixelspec.py`: PixelSpec to 8-bit RGBA frames per palette variant, schema and cross-reference validation before rendering, repeated poses stored once and reused by index, empty clip tails merged into the last visible frame, clips manifests for build_animation_clips (v1, or v2 when clips use v2 fields), `--build-clips` through the sibling builder by path, integer `--preview-scale` previews, codeart-meta.json with a per-frame QA envelope, staged no-replace publication (B18-T1; roadmap P0-5, 4.5, 4.11; the probe's preview-size problem).
- Added: `codeart2d/scripts/svg_render.py` with `render` (palette compile, lint, rasterize, vector or `--crisp` pixel route), `lint` (portable and pixel profiles, one specific message per construct) and `doctor` (conformance corpus per backend, non-zero exit when the primary backend deviates, pip hint on a clean machine) (B18-T2; roadmap P0-5, 4.4, 4.6, 7.2).
- Added: `codeart2d/scripts/pixel_qa.py` with `--detect-grid`, `--review`, `--onion`, `--strict` and `--report` (B18-T3; roadmap P0-5, 4.7, 4.12).
- Added: codeart2d examples `slime.pixelspec.json` (3 variants), `walker16x24.pixelspec.json` (four-direction walk) and `potion-icons.svg` with `palette.json`; references `pixelspec.md`, `svg-profile.md`, `rasterizers.md`, `style-envelope.md` (B18-T4; roadmap 4.3-4.7, 6.1).
- Changed: none (new tools).
- BREAKING: none.
- Fixed: none in existing files (B18 creates new files only).

## 5. Schema change requests

None is required: every document B18 writes validates against the frozen vendored
schemas (tests assert `codeart_meta_v1`, `clips_input` and `qaEnvelope`). Optional fields
the producers add (objects are open), with documentation fragments integration may add:

`codeart.schema.json` `$defs/codeart_meta_v1/properties` (JSON pointer
`/$defs/codeart_meta_v1/properties/pixelspec` and `/svg`):

```json
"pixelspec": {
  "description": "render_pixelspec.py details: the spec's name, schema id, canvas, anchor_px and outline mode; variants {label: folder}; frames [{index, name, file (relative to the variant folder) or null, empty?, reuses?}]; clips {manifest_schema, manifests, bundles, per_clip {name: {frames, merged_empty_tail}}}; preview {file, scale, layout}.",
  "type": "object"
},
"svg": {
  "description": "svg_render.py render details: mode vector|crisp, lint profile, zoom, canvas, anchor_px, palette_source file|svg-colours, variants {label: folder}, images [{variant, file, scale, size, anchor_px, visible?, partial_alpha?, off_palette?, colors?, from?, method?}].",
  "type": "object"
}
```

Extra keys inside QA envelopes (common `qaEnvelope` and `qaCheck` are open): envelope
`frames` (per-frame metrics, render_pixelspec and pixel_qa), `palette` {file, variant}
(pixel_qa); check keys `failing` (files or frame names over the gate), `profile` (lint),
`detail` (loop_seam: seam, adjacent_median).

Clips manifests written by render_pixelspec use the existing optional top-level
clips_input fields `art_source: "code"`, `placeholder: false`, `pixel_art: true`,
`sampling: "nearest"` in v1 and v2 manifests alike.

Proposed new def for the doctor report (documentation; nothing validates against it
yet), `codeart.schema.json` `/$defs/doctor_report_v1`:

```json
"doctor_report_v1": {
  "description": "svg_render.py doctor --report: codeart_core.run_doctor output (cases, backends, primary, lint, hint?) extended into a common QA envelope, plus the platform. status is pass only when the primary backend passes every case and the portable lint rejects every lint case; deviations of fallback backends are warn checks.",
  "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
  "type": "object",
  "required": ["primary", "cases", "backends", "lint"],
  "properties": {
    "primary": {"anyOf": [{"enum": ["resvg_py", "resvg_js_cli", "chrome"]}, {"type": "null"}]},
    "cases": {"type": "array", "items": {"type": "string"}},
    "backends": {"type": "object"},
    "lint": {"type": "array"},
    "hint": {"type": "string"},
    "platform": {"type": "object", "properties": {"system": {"type": "string"}, "python": {"type": "string"}}}
  }
}
```

## 6. Shared-helper promotion requests

| Helper (file) | Proposed home | Why and tests |
|---|---|---|
| `_LocalContracts`, `_local_contract_errors(instance, domain, definition)` ([render_pixelspec.py](../skills/codeart2d/scripts/render_pixelspec.py), "contract validation" section) | `forge_core.contract_errors(instance, domain, definition, *, schema_dir)` (stdlib only), or codeart_core if kept codeart-only | A Draft 2020-12 validator for exactly the keywords the vendored schemas use (unknown keywords raise), so CLIs validate inputs against the skill's own `references/schemas` without jsonschema. B19 (rig_anim_v1, fx_v1), B20 (material_spec_v1), B21 and B13 validators need the same. Tests: `test_validator_agrees_with_jsonschema_on_every_vendored_contract_fixture` (348 fixture documents of the codeart, common, sprite and map schemas: 99 valid, 249 invalid, full agreement), `test_validator_agrees_with_jsonschema_on_pixelspec_mutations`, `test_validator_supports_every_keyword_of_the_vendored_schemas` |
| `_local_file_ref(path, base)` (all three CLIs) | `forge_core.file_ref(path, base)` | fileRef with a manifest-relative POSIX path, or the file name plus sha256 when the file is on another drive (the Wave A rule; `portable_path` returns an absolute path there). codeart_core has the same logic privately in `_file_ref` |
| `_local_publish_files(items)` (pixel_qa.py), `_local_publish_file(target, data)` (svg_render.py) | `forge_core.publish_files_no_replace(items: list[(Path, writer)])` | Appendix D "sidecars are published with publish_file_no_replace, with rollback": write each beside its target, publish without replacing, remove the already published ones on failure |
| `_local_safe_stem(name, used, fallback)` (render_pixelspec.py, svg_render.py) | `forge_core.safe_stem` | case-insensitively unique, Windows-safe file stems from user-given names |
| `_local_inside(path, folder)` (all three CLIs) | `forge_core.path_inside` | the "outputs never inside the skill folder" guard every CLI can share |
| `select_variants(...)` (render_pixelspec.py and svg_render.py, same semantics) | `codeart_core.select_variants(named, text)` | `all` / `base` / names, shared by every codeart2d CLI with palette variants (B19-B21 too) |
| `_local_dependency_problem()` (all three CLIs) | keep local, or a stdlib-only vendored `forge_cli.py` | it must run before numpy is imported, and forge_core imports numpy at import time, so it cannot move into forge_core as is |

## 7. Cross-module links that Z must add

- codeart2d SKILL.md (Z): link references/pixelspec.md, references/svg-profile.md, references/rasterizers.md and references/style-envelope.md; paste the routing rows and the hello-sprite quickstart above; mention the three examples.
- references/svg-profile.md names "rig_animate.py, planned for the codeart2d P1 release" in plain text: once B19 lands, link its rig-animation.md there.
- references/style-envelope.md step 3 mentions the P1 tools in plain text: once B19, B20 and B21 land, link rig-animation.md, tiles-and-maps.md and layouts-and-parallax.md.
- generate2dsprite SKILL.md and B02's frames-and-clips.md: "art_source code: render frames with codeart2d render_pixelspec.py --build-clips; never through generate2dsprite.py process".
- B02 (build_animation_clips v2):
  - the builder records absolute paths in `animation-clips.json` (`source_manifest.path` and every `frames[].source.path`). When render_pixelspec builds inside its stage folder, those paths point at the stage, which is removed after publication; please write manifest-relative paths (Wave A rule), which also makes bundles byte-identical across runs;
  - render_pixelspec writes v1 manifests carrying the optional clips_input fields `art_source`, `placeholder`, `pixel_art` and `sampling`; the v1 reader must keep accepting them;
  - clips that use v2 fields (`ticks`, `loop_policy`, `events`, `keys`, `entry_frame`, `transitions`, ...) are written as `.v2` manifests: `--build-clips` on them works once B02's v2 reader lands (today the builder answers `Expected schema generate2dsprite.animation_clips.v1.` and render_pixelspec publishes nothing);
  - once `--preview-scale` exists in the builder, render_pixelspec may forward its own `--preview-scale` to it.
- Integration e2e smoke (Appendix I, pipeline 3): `render_pixelspec.py --spec skills/codeart2d/examples/slime.pixelspec.json --output-dir <tmp> --build-clips --strict-qc`.
- README requirements and tool tables: the rows in section 3.

## 8. Known limitations and what is not proven

- Platform: everything ran on Windows 11 with Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, resvg-py 0.5.0 (resvg 0.48.1). Linux, macOS, Python 3.10 and Pillow 10.1 were not run. No test launches Chrome or the resvg-js CLI: the doctor fallback path uses a monkeypatched fake backend, and the clean-machine tests hide every backend.
- Determinism: frames, clips manifests, previews, compiled SVGs, PNGs and `codeart-meta.json` are byte-identical across runs (tested). The builder's `animation-clips.json` inside `<variant>/bundle/` is not, because it embeds absolute stage paths (section 7, B02). PNG bytes are deterministic per Pillow/zlib build; resvg output per resvg version.
- QA scope: the pixel gates prove alpha, palette and outline continuity, not appeal, anatomy or motion (each envelope says so in `notProven`). The `l_corners` limit of 10 comes from A3; measured here: slime 0, walker at most 3 per frame. Grid detection and the loop seam check are advisory (warnings). The loop-seam limit 2.0 (last-to-first change over the median step) is my choice: measured 0.74 on the slime idle and 5.0 on a loop that drifts 1 px per frame; it is not calibrated on real animation.
- The contract validator implements the keywords the vendored schemas use; `format` is an annotation (as in jsonschema without a format checker) and patterns run on Python `re`, which matches ECMA-262 for these simple patterns. An unknown keyword raises, so a schema update that adds one fails the keyword test and the tool loudly rather than passing silently.
- Examples are programmer art made for this module (no owner or image-model art): the walker and potion icons were reviewed only by me on 6x previews; the slime is A3's design-prototype spec with the canonical schema id.
- Not implemented: indexed PNG and palette LUT export (roadmap 4.5 "optional", not a B18 task); slicing an SVG icon sheet into one PNG per icon (render one SVG per icon, or cut the sheet with generate2dmap's extract_prop_pack.py).
- Deviations and decisions where the plan was silent:
  - `--clips-manifest` (write manifests without building) and `--clips-builder PATH` (point at the builder when the skills are not siblings) are extra flags; the roadmap's example command already used `--clips-manifest`.
  - `--variants all` means every named variant (the base palette when there are none), so the slime renders exactly its 3 variants; `base` selects the base palette explicitly.
  - One output folder per spec; variants are subfolders; one `codeart-meta.json` describes every variant (its QA envelope covers every frame of every variant).
  - `svg_render render` takes one SVG per run, because `codeart_meta_v1` binds outputs to one `spec_sha256`.
  - A lint problem always stops `svg_render render` (not only under `--strict-qc`): out-of-profile SVG renders differently per renderer. `--crisp` adds `shape-rendering="crispEdges"` to the root when missing instead of requiring it in the source, so one SVG serves both routes.
  - `svg_render lint` checks the file as written; `--compile` lints the compiled SVG.
  - `svg_render doctor --report` writes the report even when the doctor fails (the report is the diagnosis); art outputs follow the no-partial-output rule.
  - `pixel_qa --strict` writes neither the report nor the review sheet on failure (the convention); run without `--strict` to keep a failing report.
  - Each CLI carries `TOOL_VERSION = "0.4.0"` (the planned release, owner decision 12) for its QA envelope `tool`.
- STD in this worktree: `758 passed, 2 skipped, 35 subtests passed` (base 3f9252d: 674 passed, 2 skipped; +84 new tests) and `tools/vendor_sync.py --check` exit 0 (`pending_canonicals`: `shared/forge_palette.py`, B04). `tests/test_skill_packages.py` still fails only for codeart2d (no SKILL.md until Z).
