# Validation record — 2026-10-06 (0.4.0)

Release check of branch `asf/integration` at `2824c1b` before the docs commit. Local only: nothing was pushed, tagged or published; these local runs are the record.

## Automated results

| Check | Command | Result |
| --- | --- | --- |
| Python suite, serial | `python -m pytest -q` | 2610 passed |
| Fresh clone | `git clone`, `pip install -r requirements-dev.txt`, then the suite | green |
| Python 3.10 floor | full suite under a Python 3.10 interpreter | green |
| Without scipy | suite with the numpy fallbacks (`FORGE_CORE_NO_SCIPY=1`) | green |
| JavaScript | `node --test "tests/js/*.test.mjs"` (node 22.15) | 83 passed |
| Plugin manifest | `claude plugin validate --strict .` (Claude Code 2.1.289) | passed |
| Vendored copies | `python tools/vendor_sync.py --check --strict` | in sync |
| Skill packages and links | `python -m pytest tests/test_skill_packages.py`; `python tools/check_links.py` | passed; 0 broken links, every README image exists |

Opt-in markers (`bench`, the headless-browser checks) are not part of these counts. The `bench` tests reproduce the keyer, loop and palette numbers quoted in the [CHANGELOG](../CHANGELOG.md) on the owner's evidence clips and need their paths in `FORGE_BENCH_*` variables.

Environment: Windows 11, Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1, resvg-py 0.5.0, ffmpeg 8.0.1, node 22.15.0.

## README showcase

The code-art images in `src/codeart/` were rendered for this release with no image model and no quota, from the shipped examples:

- `hero-walk.gif`: `rig_animate.py --anim skills/codeart2d/examples/hero.anim.json --build-clips --strict-qc` (QA pass, walk frames upscaled 4x on a dark background).
- `fx-set.gif`: `fx_build.py --spec skills/codeart2d/examples/slash.fx.json --build-clips` (QA pass; slash, impact, dust and orb side by side, 3x).
- `meadow-layout.png`: `autotile_build.py --material-spec skills/codeart2d/examples/dirt-path.material.json --strict-qc` (seam proof 0 mismatches), then `layout_build.py` on `meadow-layout.json` with its `path` material named `dirt` to match that tileset, `--tiles <wang81 tileset> --preview --strict-qc` (QA pass, every exit reachable).

## Live validation

Cross-host runs with the owner's local tools (Codex, Claude Code, Grok CLI) under the owner-approved caps. Every call went into the ledger, and no credential file was read.

The owner asked for a lean, key-only set: three fresh sessions with no history, each on the 0.4.0 tree (`asf/integration`). Quota actually used: 0 Codex images, 1 Grok video, 0 paid API calls, and one Claude Code session (US$1.32). The baseline is the 2026-10-05 cold-start test on the old skill (cfed170).

### 1. Codex host: the fox-run request from the cold test

`codex exec` (codex-cli 0.155.1, workspace-write, image tool enabled) got the same request: an 8-frame side-view pixel fox run in 48x64 cells at 80 ms. It finished on its own in 11 min 09 s (exit 0). The 45–46 px fox is inside the code-art envelope (owner decision 7), so it went to `codeart2d` and used 0 image generations.

| `sheet_qc`, run from 0.4.0 | old run | 0.4.0 run |
|---|---|---|
| Cross-cell tail / boundary band / specks | fail (1 / 246 px / 7) | pass (0 / 0 / 0) |
| Head-scale identity | fail (0.918–1.0) | pass (1.00 on 8/8) |
| Colours per frame / partial-alpha pixels | 594–709 / 7,547 | 20–21 / 0 |
| Loop seam (max / median step) | 1.38 | 1.27 |
| Manual fixes | 1 hand-made crop box | 0 |
| Leg alternation | fail (duplicated half-cycle) | unclear (near-duplicate half-cycle frames) |

Trade-off: the image-model fox reads livelier. The live fix pass added a walk/run half-cycle duplicate warning and near/far-leg contrast guidance to codeart2d. The image-sheet path (`sheet_qc` → `process` → `scale_frames`) was not exercised live on Codex in this run.

### 2. Grok image-to-video through video2dsprite

Same character as the cold test. Steps: `key-plan`, then `prepare_i2v_input` (1280x720, root fixed), then `cli_media.py video --route grok-acp --execute`. That took 49.4 s, returned 1280x720 at 24 fps with 145 frames, and recorded the route proof. Then the soft matte, `register_clip apply`, `gait_loop select`, `retime`, `package png,webm,packed`, `verify` (49 checks) and `validate_animation`, all passing. The old cfed170 keyer ran on the same new clip for comparison.

| Same clip | old keyer | 0.4.0 |
|---|---:|---:|
| Outer-ring fringe (mean / worst) | 0.704 / 0.810 | 0.000 / 0.000 |
| Opaque key pixels leaked | 7,780 | 0 |
| Enclosed key pockets | 19 | 0 |
| Alpha flips per frame pair | 22.3 | 3.38 |
| Keying time, 145 frames | 271 s | 44 s |
| Loop | none (whole clip rejected: wrap 1.96) | auto 74–88, wrap 1.016, root drift 0.31 %/loop |

Known gaps:
- Grok ignored the requested start and end holds.
- A faint blue-violet tint remains on one navy edge, and `matte_qa` does not count it.
- The live fix pass made `prepare_i2v_input` accept an opaque master on a key colour and fixed the subject-phrase bug in the prompt contract.

### 3. Claude Code host with the plugin (code art, no image quota)

`claude -p --plugin-dir <repo>` got one zh-TW sentence: 32x32 slime with idle and jump, a hit FX, and a 16x16-tile grass and water map with collision. It finished in 180 s (US$1.32), exit 0, with 0 permission denials.
- The plugin skills triggered: `generate2dsprite` and `generate2dmap`, routed to `codeart2d`.
- The reply disclosed the art as code-drawn, and every `codeart-meta.json` has `art_source=code`.
- Exports: Aseprite, Godot and Tiled.
- Independent checks: `map_bundle validate` pass (0 errors, 0 warnings); `map_nav check` reachable, with 2 one-cell pockets.
- The hit FX published with a QA warn (`l_corners` 13 > 10) that the reply summarised as "all checks passed". The live fix pass now requires WARN results to be reported verbatim.

Baseline: the old skill had no Claude Code route. It offered only the host image tool or a paid API, and shipped no plugin and no codeart2d.

Artifacts for all three runs are in the owner's local validation folder. They are not committed; the README images come from them (`src/validation/`).

## What remains unproven

See [known-limitations.md](./known-limitations.md): editor imports, device playback, macOS, single-clip thresholds and live provider behaviour beyond the runs recorded above.
