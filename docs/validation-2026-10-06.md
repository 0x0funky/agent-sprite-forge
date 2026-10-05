# Validation record — 2026-10-06 (0.4.0)

Release check of branch `asf/integration` at `2824c1b` before the docs commit. Local only: nothing was pushed, tagged or published. CI (`.github/workflows/tests.yml`: Ubuntu and Windows, Python 3.10 and 3.13) runs the same commands on every push; its results supersede these counts once pushed.

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

Cross-host runs with the owner's local tools (Codex, Claude Code, Grok CLI) under the default caps: Codex images at most 10, Grok images at most 4, Grok videos at most 4, every call in the ledger, no credential read.

<!-- LIVE:results -->

## What remains unproven

See [known-limitations.md](./known-limitations.md): editor imports, device playback, macOS, single-clip thresholds and live provider behaviour beyond the runs recorded above.
