# B04-palette-pixel: shared OKLab palette library, palette_tool, pixel_reduce and a temporal-hysteresis clip quantizer

Branch `asf/B04-palette-pixel` (from `wip/asf-upgrade-20261005` @ 3f9252d). The module consists of:

- The canonical library [shared/forge_palette.py](../shared/forge_palette.py) (`FORGE_PALETTE_API_VERSION = "1"`).
- Its byte-identical copy [skills/generate2dsprite/scripts/forge_palette.py](../skills/generate2dsprite/scripts/forge_palette.py), written by `tools/vendor_sync.py --write`. `vendor_sync --check --strict` now passes: this was the last pending canonical.
- Two CLIs: [palette_tool.py](../skills/generate2dsprite/scripts/palette_tool.py) and [pixel_reduce.py](../skills/generate2dsprite/scripts/pixel_reduce.py).
- The reference [palette-and-pixels.md](../skills/generate2dsprite/references/palette-and-pixels.md).
- Tests: [test_forge_palette.py](../tests/test_forge_palette.py), [test_palette_tool.py](../tests/test_palette_tool.py) and [test_pixel_reduce.py](../tests/test_pixel_reduce.py), 72 in all.

## 1. CLIs

Run from the user's project root:

    python "<skill-dir>/scripts/palette_tool.py" build --input output/hero/frames --colors 16 --output-dir output/palette
    python "<skill-dir>/scripts/palette_tool.py" build --base output/palette-lock/palette.json --input output/enemy/frames --colors 24 --output-dir output/palette-v2
    python "<skill-dir>/scripts/palette_tool.py" apply --palette output/palette/palette.json --input output/hero/frames --output-dir output/hero/on-palette
    python "<skill-dir>/scripts/palette_tool.py" apply --palette output/palette-v2/palette.json --lock output/palette-lock/palette-lock.json --input output/hero/frames --indexed --output-dir output/hero/on-palette-v2
    python "<skill-dir>/scripts/palette_tool.py" lock --palette output/palette/palette.json --source output/hero/on-palette --strict --output-dir output/palette-lock
    python "<skill-dir>/scripts/palette_tool.py" luts --palette output/palette/palette.json --skin-map output/palette/team-blue.json --output-dir output/palette-luts
    python "<skill-dir>/scripts/palette_tool.py" variants --palette output/palette/palette.json --input output/hero/on-palette --variant hitflash frozen silhouette --output-dir output/hero/variants
    python "<skill-dir>/scripts/palette_tool.py" quantize-seq --input output/hero/run-frames --palette output/palette/palette.json --loop-policy cycle --output-dir output/hero/run-quantized
    python "<skill-dir>/scripts/pixel_reduce.py" --input output/hero/hero-6x.png --upscale 6 --output-dir output/hero/1x

Behaviour shared by both tools:

- `--output-dir` must not exist. The work is staged with `forge_core.staged_output`, the QA envelope is written, and the folder is published only when no check failed (with `--strict`, also when nothing warned). A refused run leaves no output and no stage folder.
- On success, one ASCII JSON line on stdout. palette_tool prints `status`, `output` and `metadata` (the subcommand's main JSON: `palette.json`, `apply-qa.json`, `palette-lock.json`, `luts.json`, `variants-qa.json` or `quantize-qa.json`) plus per-command counts. pixel_reduce prints `status`, `output`, `metadata` (`pixel-reduce-qa.json`), `palette` and per-image `{file, period, logical_size, forced}`.
- Runtime errors and refused QA print one `error: ...` line on stderr and exit 1, never a traceback. Argparse usage errors keep argparse's exit 2, as the Wave A CLIs do.
- `--help` (top level and every palette_tool subcommand) is ASCII and exits 0 under cp1252 and cp950 (tested with `assert_cli_help`).
- Image outputs are 8-bit RGBA with binary alpha, written by `forge_core.save_png`. Indexed PNGs appear only with `--indexed`, under `indexed/`.

Subcommand outputs:

| Command | Writes |
|---|---|
| `build` | `palette.json` (palette_v1), `palette.gpl`, `palette.hex` (`--formats` adds `pal`), `swatches.png`, `palette-qa.json` |
| `apply` | `<stem>.png` per input, `apply-qa.json`, `indexed/` with `--indexed` |
| `lock` | `palette.json` (`locked: true`), `palette-lock.json` (palette_lock_v1). No QA file: the fit of every source is in the lock |
| `luts` | `luts.json` (palette_luts_v1, requested in section 5), `luts.png` (256 x rows), `palettize-32.png` (`--palettize-size 0` skips it) |
| `variants` | `<variant>/<stem>.png`, `variants-qa.json` |
| `quantize-seq` | `<stem>.png` per frame (forward frames only, also for pingpong), `quantize-qa.json`, `palette.json` with `--colors`, `indexed/` with `--indexed` |
| `pixel_reduce` | `<stem>.png` (1x), `<stem>@<N>x.png` (`--upscale N`), `indexed/` (`--indexed`), `palette.json` unless `--palette`, `pixel-reduce-qa.json` |

Library API: everything in plan Appendix A "forge_palette" has the frozen signature. The additions below are keyword-only arguments or new names, and they are binding for integration:

- Constants:
  - `PALETTE_SCHEMA`, `PALETTE_LOCK_SCHEMA`, `PALETTE_LUTS_SCHEMA`.
  - `DEFAULT_TRANSPARENT_INDEX` (255), `HYSTERESIS_MARGIN` (4e-4), `ALPHA_BAND` (0.4, 0.6), `COVER_DELTA_E` (0.03), `STILL_DELTA_E` (0.02).
  - `PALETTE_FORMATS` (json, gpl, hex, pal), `VARIANT_KINDS`, `LUT_WIDTH` (256).
- Types and parsing:
  - `PaletteError` (a ValueError).
  - The frozen `Palette` dataclass: `colors`, `names`, `reserved`, `transparent_index`, `locked`, `source`, `name`; properties `rgb`, `lab`, `hex_colors`, `usable`; methods `to_json()` and `replace()`.
  - `parse_color`, `hex_color`, `as_palette`, `palette_text`.
- `build_palette(..., cover=0.03, alpha_threshold=128, max_iter=30, max_samples=32768)`. `reserved` may also be a Palette or a palette path: a locked palette being extended keeps its names and transparent_index.
- Single images: `quantize_image(rgba, palette, *, alpha_threshold=128, orphans=0)`, `render_indices(idx, palette)`, `cleanup_orphans(idx, passes=1, *, transparent_index=255)`.
- Sequences:
  - `quantize_sequence(..., pingpong=False, alpha_threshold=0.5, orphans=1, despeckle=True, return_stats=False)`.
  - `cleanup_alpha(idx_frames, *, transparent_index=255)` takes one map or a list and returns the same form.
  - `flip_stats(idx_frames, sources=None, *, transparent_index=255, still_delta_e=0.02, loop=False)`.
- Fit and locks:
  - `fit_report(asset, palette, *, alpha_threshold=1)`.
  - `lock_palette(palette, sources, *, base=None, alpha_threshold=1)`. `palette` must be the palette file, because the lock binds its sha256.
  - `locked_sources(lock)`, `lock_view(palette, lock)`.
  - `manifest_path(path, base)`, `file_ref(path, base)`.
- LUTs: `variant_colors(palette, kind, *, color=None, mapping=None)`, `luts(palette, *, variants=(...), skins=None, flash_color=None, silhouette_color=None, snap=False)`, `lut_image(doc)`, `palettize_lut(palette, *, size=32)`.
- Grids: `detect_grid(rgba, max_period=24, tol=24, min_score=0.5)` gives the same results as codeart_core.detect_grid; `grid_score(rgba, period, *, phase=None, tol=24, min_score=0.5)` measures one given period.

Semantics later modules rely on:

- Ties go to the lowest index, and an exact palette colour always keeps its index without floating point.
- `to_oklab` is bit-identical to `forge_matte._to_oklab`: same code, tested on six shapes.
- `from_oklab` clips in linear RGB and rounds half-up. `from_oklab(to_oklab(c)) == c` for every tested 8-bit colour.
- Index maps are uint8. The palette's `transparent_index` marks transparent pixels; it is 255 unless the palette names another slot.
- `save_indexed_png` refuses a `transparent_index` that disagrees with the palette's own, and an index that would hide a colour.

## 2. SKILL.md routing rows

generate2dsprite:

| Need | Route |
|---|---|
| One shared palette for a character or a whole game | `scripts/palette_tool.py build` on the accepted frames; check `swatches.png`; then `apply` |
| Frames that must sit exactly on a palette (binary alpha, no off-palette pixels) | `scripts/palette_tool.py apply --palette palette.json`; then `build_animation_clips.py` on the RGBA output |
| Freeze approved art's palette; add colours for new art without changing it | `palette_tool.py lock`, then `build --base <locked palette>`, then `apply --lock palette-lock.json` |
| A frame sequence flickers between palette colours (video or image-model frames) | `scripts/palette_tool.py quantize-seq --loop-policy cycle\|pingpong\|oneshot`; read `flips` in `quantize-qa.json` |
| Hit flash, frozen, silhouette or team-colour variants | baked frames: `palette_tool.py variants`; engine palette swap: `palette_tool.py luts` (luts.png rows) |
| Image output to a strict logical pixel grid | `scripts/pixel_reduce.py` (only when it detects a clean grid; otherwise keep the art and use `palette_tool.py apply`) |
| Indexed PNG for an engine | `apply --indexed` (or `quantize-seq`/`pixel_reduce --indexed`); never feed `indexed/` to the builders |

Reference row: `| Palettes, locks, logical pixels, indexed export, clip hysteresis | references/palette-and-pixels.md |`

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `generate2dsprite/scripts/palette_tool.py` | OKLab palettes: deterministic k-means++ build with reserved colours, apply with binary alpha, locks that keep old outputs byte-identical as the palette grows, palette-swap LUTs and baked variants, clip quantizing with temporal hysteresis | `tests/test_palette_tool.py` (round trip, byte-identical lock, variants, LUT schema, strict QA publishes nothing) |
| `generate2dsprite/scripts/pixel_reduce.py` | Detects an integer pixel grid (period and phase) and reduces the image to logical pixels on an OKLab palette; refuses images with no clean grid | `tests/test_pixel_reduce.py` (6x upscale recovered exactly, the fox sheet refused) |
| `generate2dsprite/scripts/forge_palette.py` (vendored) | Shared palette library: OKLab, k-means++, nearest index, indexed PNG with tRNS, palette.v1/.gpl/.hex/.pal, fit and lock, LUTs, hysteresis quantizer, grid detection | `tests/test_forge_palette.py` |

Requirements: no new dependency (numpy and Pillow only).

## 4. CHANGELOG entries

Added:

- `forge_palette`, the shared OKLab palette library (B04-T1, T2). It is vendored into generate2dsprite, closes the last pending canonical of `shared/VENDORED.json`, and covers:
  - OKLab conversion, bit-identical to forge_matte's;
  - deterministic weighted k-means++ over unique colours, with reserved colours and coverage skipping;
  - nearest index;
  - indexed PNG with one tRNS transparent index;
  - palette.v1 JSON, GIMP .gpl, Lospec .hex and JASC .pal;
  - fit reports (OKLab dE) and palette locks;
  - LUTs;
  - the temporal-hysteresis clip quantizer (margin 4e-4, alpha band 0.4-0.6, loop seeding, ping-pong applied after quantizing);
  - integer grid detection.
- `palette_tool.py` subcommands `build`, `apply`, `lock`, `luts`, `variants` and `quantize-seq`, each with staged publish and a QA envelope (B04-T3; roadmap P2-2, DOC-11).
- `pixel_reduce.py`: integer grid period and phase, mode, box or centre downsample, OKLab palette, binary alpha, integer upscale and QC. It refuses `no clean grid` unless `--force` (B04-T4; roadmap P2-3, 4.7).
- `references/palette-and-pixels.md`: lock, logical resolution, indexed export (never builder input) and clip hysteresis (B04-T5; DOC-11).

Changed: none. B04 adds new tools only.

BREAKING: none.

Fixed:

- DOC-11, the B04 part: the skill had no palette lock, no logical-grid reduction and no off-palette or partial-alpha QC. B03-T5 owns the prompt-rule half.
- Roadmap P2-3: image-model "pixel art" is no longer treated as pixel art without a measured grid. The fox fixture is refused with its numbers.

## 5. Schema change requests

Producer: B04 (`palette_tool.py`, `forge_palette.lock_palette`). Consumers: B04 `apply --lock` and `quantize-seq --lock`, engine exporters (B09), and Z docs. Both diffs go into `shared/schemas/sprite.schema.json`, then `tools/vendor_sync.py --write`. `tests/test_palette_tool.py::amended_errors` applies exactly these fragments in memory and validates the real outputs against them.

(a) Document the lock's locked colours. The current documents already validate against the frozen schema, because objects are open. JSON pointer `/$defs/palette_lock_v1/properties`, add:

```json
"colors": {
  "description": "The locked colours in index order (#rrggbb); sources listed here are re-applied with exactly these colours after the palette grows (forge_palette.lock_view).",
  "type": "array", "minItems": 1, "maxItems": 256,
  "items": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}
},
"transparent_index": {"type": ["integer", "null"], "minimum": 0, "maximum": 255}
```

(b) New document `generate2dsprite.palette_luts.v1` (`luts.json`). JSON pointer `/$defs/palette_luts_v1`, add:

```json
{
  "description": "Palette-swap LUTs (palette_tool.py luts): one colour per palette index for every row; image is the 256-wide LUT texture, one row per entry of rows, in that order.",
  "type": "object",
  "required": ["schema", "palette", "size", "rows", "luts"],
  "properties": {
    "schema": {"const": "generate2dsprite.palette_luts.v1"},
    "palette": {"$ref": "common.schema.json#/$defs/fileRef"},
    "image": {"$ref": "common.schema.json#/$defs/fileRef"},
    "size": {"type": "integer", "minimum": 1, "maximum": 256},
    "transparent_index": {"type": ["integer", "null"], "minimum": 0, "maximum": 255},
    "rows": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
    "luts": {
      "type": "object", "minProperties": 1,
      "additionalProperties": {
        "type": "object", "required": ["kind", "colors"],
        "properties": {
          "kind": {"enum": ["identity", "hitflash", "frozen", "silhouette", "skin"]},
          "colors": {"type": "array", "minItems": 1, "maxItems": 256, "items": {"$ref": "common.schema.json#/$defs/hexColor"}},
          "index": {"type": "array", "minItems": 1, "maxItems": 256, "items": {"type": "integer", "minimum": 0, "maximum": 255}}
        }
      }
    },
    "palettize": {
      "type": "object", "required": ["image", "size"],
      "properties": {
        "image": {"$ref": "common.schema.json#/$defs/fileRef"},
        "size": {"type": "integer", "minimum": 2, "maximum": 64},
        "layout": {"type": "string"}
      }
    },
    "usage": {"type": "string"}
  }
}
```

The A0 contract catalogue gets a row: `sprite/palette_luts_v1 | generate2dsprite.palette_luts.v1 | B04 palette_tool luts | engines, B09`.

Optional fields B04 producers add. Objects are open, so nothing else needs a schema change; listed so they do not drift:

- `palette.json` (palette_v1): an optional top-level `name`, also used as the GIMP palette name.
- QA envelopes (common `qaEnvelope`), extra top-level keys:
  - `palette-qa.json`: `palette{colors, reserved, transparent_index, seed}`, `fit{<input path>: fit_report}`, `totals{max_delta_e, worst_p95_delta_e, off_palette_px}`.
  - `apply-qa.json`: `fit`, `totals`, `locked_inputs[]`.
  - `variants-qa.json`: `luts{<row>: {kind, colors, index?}}`.
  - `quantize-qa.json`: `loop_policy`, `locked`, `params{margin, alpha_band, alpha_threshold, orphans, despeckle}`, `flips{hysteresis, per_frame_nearest, noise_flip_reduction}`, `frames[{file, held_index_px, held_alpha_px, orphans_fixed_px, despeckled_px}]`, `totals`.
  - `pixel-reduce-qa.json`: `images{<file>: {grid, detected, forced, method, logical_size, mixed_blocks}}`, `palette{colors, built, transparent_index}`.
- Checks (qaCheck): an optional `subject` (the file the check is about; pixel_reduce) and `files[]` (build's `inputs_without_samples`). An ungated measurement is a check with status `skipped`, its `value` and a null `threshold`.
- A fit report (`fit_report`, lock `sources[].fit`) has `measured_px`, `alpha_threshold`, `off_palette_px`, `partial_alpha_px`, `colors`, `palette_colors_used`, `mean_delta_e`, `p95_delta_e`, `max_delta_e`.

## 6. Shared-helper promotion requests

1. `forge_palette.manifest_path(path, base)` and `forge_palette.file_ref(path, base)` (shared/forge_palette.py:1082 and :1091) belong in forge_core. Every Wave B writer needs a fileRef with the "file name when no relative path exists" rule that A1's `portable_path` leaves to callers. Tested by `test_file_ref_on_another_drive_records_the_file_name` and the fileRef checks in the CLI tests. After promotion, keep the forge_palette names as aliases: they are public now.
2. `palette_tool.check`, `qa_envelope` and `enforce` (skills/generate2dsprite/scripts/palette_tool.py:104, :112, :124) are a generic QA-envelope builder: the status is the worst check, ungated measurements are `skipped`, and a failure or a strict warning raises before publish. Candidate for `forge_core.qa_envelope`. pixel_reduce imports them from palette_tool, which is allowed within one skill.
3. `palette_tool.run_main` (:134), `expand_inputs` (:62), `natural_key` (:57), `output_names` (:86) and `load_images` (:95) are CLI plumbing: `error:` lines, the one-line JSON, folders expanded to PNGs in natural order. Candidates for forge_core once two or more skills need them.
4. OKLab dedupe: `forge_matte._to_oklab` (shared/forge_matte.py:318) is bit-identical to `forge_palette.to_oklab` (shared/forge_palette.py:96; `test_to_oklab_matches_forge_matte_bit_for_bit`). forge_palette is vendored only into generate2dsprite, while forge_matte also ships in video2dsprite and generate2dmap. So either move `to_oklab`/`from_oklab` into forge_core, or add forge_palette targets for those two skills, then have forge_matte import it. Either way no result changes.
5. Grid detection dedupe: `codeart_core.detect_grid` (skills/codeart2d/scripts/codeart_core.py:1747) and `forge_palette.detect_grid` (shared/forge_palette.py:1368) return identical dicts (`test_detect_grid_matches_codeart_core`, six images, two parameter sets). A3 suggested this move. Add `skills/codeart2d/scripts/forge_palette.py` to `shared/VENDORED.json` and let codeart_core delegate. `grid_score` (:1394) comes with it.

## 7. Cross-module links that Z must add

- generate2dsprite SKILL.md: the section 2 rows, and a link to `references/palette-and-pixels.md`. State that `indexed/` folders are never builder input.
- `references/character-animation.md` (B02) says "If an explicit reduction to a logical grid is needed, apply one shared reduction contract". Link it to `palette-and-pixels.md` section 1 and `scripts/pixel_reduce.py`.
- `references/prompt-rules.md` (B03-T5): palette list and colour cap with `palette.v1` should point to `palette_tool.py build`/`lock` and `palette-and-pixels.md`.
- B02 clips v2: `palette_ref` can name the `palette.json` from `build` or `lock`. With `quantize-seq --loop-policy pingpong`, the manifest should use `loop_policy: pingpong`: only the forward frames are written, and the builder's mirror reuses them.
- B09 export_engine: `luts.json` and `luts.png` (palette-swap rows) and `palettize-32.png` can ride along with Godot or Aseprite exports; the texture layout is in `luts.json.usage`.
- video2dsprite docs (B05/B08): pixel-art clips from video can be quantized with `generate2dsprite/scripts/palette_tool.py quantize-seq` (a sibling CLI called by path, never imported).
- codeart2d (B18): `palette.json` written by palette_tool is read correctly by `codeart_core.parse_palette` (json, gpl and hex; tested). B18's `pixel_qa.py` could reuse `forge_palette.fit_report` once vendored (section 6, item 5).
- CHANGELOG and README: section 3 rows; no new requirement.

## 8. Known limitations and what is not proven

- **Determinism** is proven per machine: two runs and two fresh processes with different hash seeds give the same palette, and CLI builds are byte-identical. Across OSes, `np.cbrt`, `pow` and BLAS (forge_matte's `@` form, kept for bit-identity) may differ in the last bit. That can flip a pixel that sits exactly between two colours, or move a k-means centroid. Exact palette colours never move. Linux and macOS were not run.
- **Hysteresis**, measured on synthetic noisy clips (48 px sprite, 12 frames, 3 seeds each), with the noise-flip reduction against per-frame nearest quantizing with the same cleanup:
  - sigma 1.5: 64-88%;
  - sigma 3: 47-67% (the acceptance test uses sigma 3 and 16 colours: 66-67%);
  - sigma 5: 36-53%;
  - sigma 8: 24-41%.

  The fixed dE 0.02 margin loses to heavy noise; `--margin` raises it. The game-opus55 study clip (-45%) was not re-run here, because it needs D:/chain art. The CLI reports the measured reduction for every clip in `quantize-qa.json`.
- **Indexed size**, measured:

  | Sheet | Colours | Indexed vs RGBA |
  |---|---|---|
  | 512x64, 8 frames | 16 | 1.81x smaller (2.11x with a compact slot) |
  | fox, 1536x1024 | 32 | 2.06x smaller |
  | fox, 1536x1024 | 255 | 2.14x smaller |

  These are below game-opus55's 2.86x, which was measured on its base64 manifest. With the default slot 255 the PLTE and tRNS cost about 1 KB, so a single tiny sprite is larger than its RGBA PNG.
- **Grids**: integer periods 2..32 only; fractional periods are deferred, as in the plan. Sparse art can score well at a wrong given period (a 6x image at period 4 scores 0.76). The added `period_agrees` check warns, but only when `--period` is given. Auto-detection picks the largest period within 0.02 of the best, so a no-grid image reports the maximum period; only the score and uniformity gates matter there.
- **Fox fixture**: refused with score 0.014 and uniformity 0.045. `--force --period 8` gives a usable 192x128 sprite, marked `warn`, with 11.6% reconstruction mismatch. My count is 69,599 distinct visible colours (alpha > 0); roadmap P2-3 says 16,208 colours under another counting. The fixture's alpha never reaches 255.
- **Variants**: `frozen` is a fixed OKLab tint (L' = 0.3 + 0.7 L, chroma pulled toward hue about 245 degrees). It was reviewed on synthetic sprites only, never in an engine. Without `--snap`, variant colours lie off the palette by design; LUT rows can hold any colour.
- **Deviations from the plan** and why:
  - `quantize_sequence` seeds a cycle with a warm-up pass. game-opus55 quantized the last frame on its own; the warm-up gives frame 0 the predecessor it really has, as forge_matte's `temporal_alpha_hysteresis` does.
  - Ping-pong seeds frame 0 from frame 1, its predecessor in playback. game-opus55 seeded from the last frame.
  - `quantize_sequence` also runs one `cleanup_orphans` pass per frame, game-opus55's `cleanup`, which its measurement included.
  - `quantize-seq` writes forward frames only, because B02's builder mirrors a `pingpong` clip; the library returns the full cycle.
  - `lock_palette` takes the palette file path, because the lock binds its sha256. The lock also records `colors` and `transparent_index` (section 5a); these make `lock_view` and `apply --lock` possible, the mechanism behind `test_lock_keeps_prior_outputs_byte_identical`.
  - `palette_tool lock` writes no QA envelope: each source's fit is in the lock record. `--strict` requires sources exactly on the palette.
  - Additions beyond the plan: JASC `.pal` read and write, swatch PNG reading, the RGB palettize strip, `build --transparent-index`, `--balance`, `--keep-alpha` and the `period_agrees` check.
- **Not run**: Python 3.10, numpy 1.26, Pillow 10.1, Linux, macOS. The sources avoid newer APIs and parse with the 3.10 grammar. All tests ran on Windows 11 with Python 3.13, numpy 2.5.3 and Pillow 12.3.0.
