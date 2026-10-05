# Handoff: A1-forge-core

Module output: the canonical shared core [shared/forge_core.py](../shared/forge_core.py)
(`FORGE_CORE_API_VERSION = "1"`), byte-identical copies in
[generate2dsprite](../skills/generate2dsprite/scripts/forge_core.py),
[generate2dmap](../skills/generate2dmap/scripts/forge_core.py),
[video2dsprite](../skills/video2dsprite/scripts/forge_core.py) and
[codeart2d](../skills/codeart2d/scripts/forge_core.py), and the tests in
[tests/test_forge_core.py](../tests/test_forge_core.py).

Everything in plan.md Appendix A "forge_core" is implemented with the frozen
signatures. Additions are keyword-only or extra dict keys (listed in section 5).

## 1. CLIs

None: forge_core is a library. The CLI pattern every Wave B tool should follow:

```python
sys.path.insert(0, str(Path(__file__).parent)); import forge_core  # the skill's own copy
forge_core.utf8_stdio()                                             # first line of main()
with forge_core.staged_output(args.output_dir) as stage:           # refuses an existing dir
    ...write into stage, run QA, raise on strict-QC failure...      # failure -> nothing published
print(json.dumps({"output": str(args.output_dir), "metadata": str(meta)}))
```

## 2. SKILL.md routing rows

No routing rows (no CLI). Suggested shared "Processing notes" bullets for the
generate2dsprite, generate2dmap, video2dsprite and codeart2d SKILL.md files:

- Outputs go to a new `--output-dir`; work is staged beside it and published only after QA passes. A failed run leaves nothing behind.
- Geometry ignores alpha at or below 16 (body measurements: 32); pixels are never changed by that threshold. Components are 8-connected.
- Inputs may be palette PNGs with transparency, 1/2/4-bit, grey, grey+alpha, 16-bit (reduced to the high byte) or CMYK; animated inputs are refused.

## 3. README tool-table rows

| Skill | Tool | Purpose | Notes |
|---|---|---|---|
| all | `scripts/forge_core.py` (vendored) | Shared core: no-replace publishing, image loading, components, alpha hygiene, anchors, premultiplied resampling, frame timing, loop-seam metric | Library, not a CLI. numpy + Pillow; scipy optional (identical results without it) |

## 4. CHANGELOG entries

Added
- `forge_core` shared library (one canonical copy in `shared/`, vendored into each skill, sha256-checked): publication, `load_rgba`, `save_png`, 8-connected components, `alpha_hygiene`, `ground_row`/`anchor_from_mask`, `rounded_grid_boxes`, `pad_to_grid`, `resample_rgba`, `frame_durations`, `rational_fps`, `transition_mae`, `seam_report`, console and JSON helpers.

Changed (take effect as each Wave B tool adopts forge_core)
- Component labelling is vectorised (scipy, or a numpy run-length union-find): a whole 2048^2 sheet labels in about 0.03-0.05 s, where the pure-Python BFS took about 0.16 s per 512^2 cell, 2.5 s per sheet (S03, MAP-13). The 22-29 s keyer speed-up is forge_matte's (A2).
- Loop seams are measured relative to the clip's own frame steps (`seam_over_median`, `seam_over_p95`) instead of edge equality (MAP-14).

BREAKING (defaults belong to the adopting modules; legacy switches per plan.md Appendix H)
- 8-connectivity is the library default; `connectivity=4` reproduces the old labelling (B01 `--connectivity 4`, B10 likewise).
- Geometry threshold constant 16 (`ALPHA_GEOMETRY_THRESHOLD`); threshold 0 reproduces the legacy `alpha > 0` rule (B01 `--alpha-geometry-threshold 0`).
- Nearest resampling rejects fractional scales (B01 `--legacy-fractional-nearest`).

Fixed
- F-03: publishing works on WSL `/mnt/c` and other filesystems without RENAME_NOREPLACE (exclusive-mkdir fallback), and sidecars publish without hard links (`open('xb')` fallback).
- S23, S16, MAP-19: palette (incl. 1/2/4-bit with tRNS), grey/grey+alpha, 16-bit and CMYK PNGs load as exact 8-bit RGBA; 16-bit grey no longer turns white; animated inputs are refused instead of silently using frame 0.
- S04, MAP-06: 1-px diagonal strokes stay one component.
- DOC-04, report v2 P1-4: `alpha_hygiene` removes the 67,907 alpha 1-4 haze pixels of the host fox sheet and detached faint islands without touching visible art.
- S14, S15, MAP-04, DOC-03, S06: bottom-edge ground line (239 -> 240), stance anchor with zero turn slide, anchors restricted to the subject box, rounded grid slicing of 1254^2 sheets, lossless pad-to-grid, integer-only nearest scale.
- S05: pinned resampling gives byte-identical output for the same frame in different clips; premultiplied channels avoid dark or bright soft edges.
- F-14, report v2 P1-1: `utf8_stdio()` keeps cp1252/cp950 consoles from crashing a finished run.
- MAP-24: `portable_path` writes manifest-relative POSIX paths.

## 5. Schema change requests

All additions are optional properties (additionalProperties stays true). Proposed
for A0's schemas so producers that copy forge_core reports validate strictly:

In `sprite.schema.json` `$defs.pipeline_meta_v2.properties`:

```json
"hygiene": {
  "type": "object",
  "properties": {
    "mode": {"enum": ["none", "floor", "detached", "both"]},
    "floor": {"type": "integer", "minimum": 0, "maximum": 255},
    "solid_min": {"type": "integer", "minimum": 1, "maximum": 255},
    "attach_radius": {"type": "integer", "minimum": 0},
    "connectivity": {"enum": [4, 8]},
    "floor_px": {"type": "integer", "minimum": 0},
    "detached_px": {"type": "integer", "minimum": 0},
    "detached_components": {"type": "integer", "minimum": 0},
    "max_removed_alpha": {"type": "integer", "minimum": 0, "maximum": 255}
  }
},
"provenance": {
  "type": "object",
  "properties": {
    "input_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "source_mode": {"type": "string"},
    "bit_depth": {"enum": [1, 2, 4, 8, 16]},
    "conversion": {"type": "string"},
    "raw_copy": {"type": "string"}
  }
}
```

In `common.schema.json` `$defs` (used by QA envelopes of loop tools):

```json
"seamReport": {
  "type": "object",
  "required": ["seam", "adjacent_median", "adjacent_p95", "seam_over_median", "seam_over_p95"],
  "properties": {
    "frames": {"type": "integer", "minimum": 2},
    "seam": {"type": "number", "minimum": 0},
    "adjacent_median": {"type": "number", "minimum": 0},
    "adjacent_p95": {"type": "number", "minimum": 0},
    "adjacent_max": {"type": "number", "minimum": 0},
    "seam_over_median": {"type": "number", "minimum": 0},
    "seam_over_p95": {"type": "number", "minimum": 0},
    "method": {"type": "string"}
  }
}
```

API additions beyond Appendix A (all optional, record for Z and Wave B):
- `load_rgba` info also holds `bytes`, `format` and `frames`; `path` is the resolved POSIX path.
- `save_png(img, path, *, zero_transparent_rgb=True)`; accepts RGBA, RGB, LA or L (Image or uint8 array).
- `ground_row(mask, *, min_run=1)` and `anchor_from_mask(..., *, min_run=1)`: a row is stable when it holds at least `min_run` pixels.
- `transition_mae(a, b, *, mask=None)` and `seam_report(frames, *, mask=None)`: boolean or 0-255 weight mask (motion-mask QA); `seam_report` also returns `frames`, `adjacent_max` and `method`, and accepts any iterable.
- `alpha_hygiene` report also holds `mode`, `floor`, `solid_min`, `attach_radius`, `connectivity`, `max_removed_alpha`.
- `connected_components(..., with_masks=True)` gives `mask` cropped to `bbox` (memory-safe for many components).
- Constants for argparse choices: `HYGIENE_MODES`, `ANCHOR_MODES`, `RESAMPLERS`.

Semantics Wave B must rely on:
- Subject pixels are `alpha > threshold` everywhere (threshold 0 = legacy `alpha > 0`).
- Coordinates are continuous: the ground line is the bottom edge of the lowest row (row index + 1); anchors are floats.
- Rounding is half-up in `rounded_grid_boxes` (1254/4 -> 314, 313, 314, 313) and `frame_durations`.
- Labels from `label_components` are 1..n in raster order with either backend, so labels are stable across machines.
- `resample_rgba` without anchors maps the whole image onto `out_size` (opaque art); with `anchor_src` it pins the grid, treats the outside as transparent and is invariant to whole-pixel shifts of image plus `anchor_src` (same `anchor_dst`). `lanczos` uses the box filter at scale <= 0.5. `nearest` accepts N or 1/N only; reductions without anchors need dimensions divisible by N.
- `frame_durations(total_ms, n)` raises ValueError when `total_ms < n` and TypeError for non-integers; `rational_fps` always returns `"num/den"` (for example `"16/1"`).
- `write_json` refuses NaN, infinity and Path objects (store paths with `portable_path`). Inside `staged_output`, `portable_path(x, stage)` equals `portable_path(x, final)` because the stage shares the final directory's parent.
- Sidecar rollback stays with the caller (publish sidecars with `publish_file_no_replace`, remove them if the directory publish fails), as in generate2dsprite `cmd_process`.

## 6. Shared-helper promotion requests

None into forge_core (this module is the shared core). Requests to A0-contracts:
- `shared/VENDORED.json`: canonical `shared/forge_core.py` -> `skills/generate2dsprite/scripts/forge_core.py`, `skills/generate2dmap/scripts/forge_core.py`, `skills/video2dsprite/scripts/forge_core.py`, `skills/codeart2d/scripts/forge_core.py` (all four copies exist on this branch).
- `pytest.ini` must register the `perf` marker; until it lands, `test_components_perf_2048` emits PytestUnknownMarkWarning (the test still runs and passes).
- `tests/forge_testutils.py` has no loader for `shared/` modules; tests/test_forge_core.py keeps a private `_local_load_forge_core()` that loads the canonical file by path. A `load_shared(name)` helper would let the integration pass drop it.
- `test_vendored_copies_match_shared` in tests/test_forge_core.py duplicates part of tests/test_vendored_sync.py for this module; it may be removed at integration once the generic test exists.

## 7. Cross-module links Z must add

- CONTRIBUTING: shared/forge_core.py is canonical; never edit the vendored copies; run tools/vendor_sync.py --write then --check.
- skills/generate2dsprite/references/processing.md: the publish guarantee, accepted PNG modes, thresholds 16/32, 8-connectivity, alpha hygiene, ground line and stance anchor (B01 owns the flags).
- skills/generate2dmap/references/prop-pack-contract.md and layered-map-contract.md: rounded grid slicing, manifest-relative POSIX paths (B10, B13).
- skills/video2dsprite/references/pipeline.md: integer frame durations, rational fps and the normalised seam metric measured on the decoded file inside the motion mask (B05, B07, B08, B16).
- codeart2d references (Z/B18): outputs written with save_png inside staged_output.
- README requirements: numpy, Pillow >= 10.1, scipy optional (forge_core falls back to numpy with identical labels).

Notes for Z (from the plan): document the publish guarantee (new output directory, staged, atomic where the OS supports it, with the documented fallback race on WSL and network filesystems), the accepted PNG modes, alpha thresholds 16/32, 8-connectivity and scipy as optional. CHANGELOG: WSL publishing works (F-03); indexed and 16-bit PNG input accepted (S23, S16).

## 8. Known limitations and what is not proven

- Publication fallback race: on filesystems that reject RENAME_NOREPLACE/RENAME_EXCL (WSL drvfs, some network mounts) the directory appears through an exclusive mkdir plus child moves, so other processes can see it partially populated, and a writer racing into the brand-new directory can collide with a child. An existing destination is never replaced on any path. The hard-link-free sidecar fallback can be observed half-written.
- Platform coverage on this branch: all tests ran on Windows 11 (Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1). The Linux renameat2 and macOS renamex_np calls were not executed here (fallbacks and errno mapping are exercised through monkeypatching); Linux CI will run them natively; macOS is unproven. The Python 3.10 / Pillow 10.1 / numpy 1.26 floors were not run locally; the sources parse with the Python 3.10 grammar and avoid newer APIs.
- `test_hygiene_on_fox_fixture` skips until A0 adds tests/fixtures/real/raw-fox-run-v1.png. Its assertions were run by hand against the read-only copy in outputs/fresh-agent-20261005/sprite (sha256 b9f14eaa...bf6f8): 67,907 alpha 1-4 pixels before, 0 after floor 4, all other pixels unchanged; `both` removed 7 detached components (11 px, max alpha 5). With the 16 threshold, cells whose subject touches the cell edge drop from 8/8 to the 2 real tail overflows.
- 16-bit samples are reduced by the high byte (truncation, as Pillow does for 16-bit RGB(A)), not rounded. CMYK conversion is naive; ICC profiles and EXIF orientation are ignored.
- `save_png` bytes are deterministic for one Pillow/zlib build; across builds compare decoded pixels.
- Performance (Windows, this machine): 2048^2 labelling 0.03-0.09 s with scipy; numpy fallback 0.04 s on sprite sheets and 0.6-0.7 s on 50% random noise or long serpentine masks. Budgets in the perf test: 0.5 s (scipy) and 3 s (numpy).
- `ground_row` with the default `min_run=1` is the bottom edge of the given mask; speck tolerance comes from the mask the caller passes (subject threshold, hygiene, component selection) or a larger `min_run`.
- `seam_report` floors its denominators at 1e-6, so a static loop with a pop reports a very large finite ratio. Thresholds for pass/fail are the adopting module's choice.
- `resample_rgba` pinned mode builds a float canvas covering the requested output (memory scales with out_size / scale).
