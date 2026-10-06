# Processor and export contracts

Run every command from your project root as `python "<skill-dir>/scripts/<tool>.py" ...`, where `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Write outputs to a **new** folder inside your project, never inside the skill folder. The tools need Python 3.10+, numpy and Pillow 10.1 or newer; scipy only speeds up component labelling.

`process` is for generated or painted sheets that still need keying, cell splitting and registration. Code art never goes through `process`: codeart2d frames are already on their pixel grid and go straight to `build_animation_clips.py`. Packaging registered frames (`build_animation_clips.py`) and complete rectangular frames (`assemble_frames.py`) is described in [frames-and-clips.md](frames-and-clips.md). Before `process`, run `sheet_qc.py spill` on a raw sheet; for one scale and whole-pixel locks across an action, `scale_frames.py` replaces per-frame fitting ([prompt-rules.md](prompt-rules.md) lists the check order).

## Normalize a generated sheet

Smooth art from a host image tool, native transparency, one shared scale, feet on one ground line:

```bash
python "<skill-dir>/scripts/generate2dsprite.py" process --input raw/hero-idle.png --target asset --mode idle --rows 2 --cols 3 --label-prefix hero-idle --output-dir sprites/hero-idle --background-mode native_alpha --align feet --scale-strategy preserve --cell-size 128 --fit-scale 0.8 --strict-qc --max-body-scale-cv 0.08 --max-anchor-y-std 0.05 --write-scale-profile sprites/hero-scale.json
```

Later actions of the same character reuse that scale profile; pass the drift limit explicitly (a profile stores 0.10 unless you set it):

```bash
python "<skill-dir>/scripts/generate2dsprite.py" process --input raw/hero-run.png --target asset --mode run --rows 2 --cols 4 --output-dir sprites/hero-run --scale-profile sprites/hero-scale.json --max-profile-scale-drift 0.08 --strict-qc
```

Pixel art that the generator drew at 8 source pixels per art pixel, keeping its drawn bob and jump:

```bash
python "<skill-dir>/scripts/generate2dsprite.py" process --input raw/slime-hop.png --target asset --mode idle --rows 2 --cols 2 --output-dir sprites/slime-hop --background-mode native_alpha --resampler nearest --logical-pixel 8 --pixel-scale 2 --scale-strategy registered --align feet --cell-size 96 --strict-qc
```

- `--target` and `--mode` must match (`list-options` prints the pairs). An unknown mode fails; `--mode sheet` needs `--rows` and `--cols`. `single`, `player` and `npc` modes normalize one image into `clean.png` and run through the same 1x1-grid geometry and QC as a sheet (`--single-size`, default 256 px).
- `--rows` and `--cols` describe the measured input; they never create missing poses.

## Before strict QC: look at the alpha

Host image tools often leave invisible haze: the 2026-10 fox sheet had 67,907 pixels with alpha 1-4 and made strict QC flag all eight cells. Check the alpha histogram of a native-alpha sheet before trusting edge checks (counts for alpha 0, 1-4, 5-16, 17-31, 32-254 and 255):

```bash
python -c "import numpy as np; from PIL import Image; a = np.asarray(Image.open('raw/hero-idle.png').convert('RGBA'))[..., 3]; print(np.histogram(a, [0, 1, 5, 17, 32, 255, 256])[0])"
```

`native_alpha` input gets `--alpha-hygiene both` by default: alpha at or below `--alpha-floor` (4) becomes 0 and faint islands with no solid pixel (alpha 32 or more) within 2 px are removed. The counts are in `pipeline-meta.json` under `hygiene`. Visible art is never changed by hygiene; `--alpha-hygiene none` keeps every pixel. A `faint_specks` warning from `sheet_qc.py spill` counts this same haze; `--alpha-hygiene both` is its fix.

## Background modes and keying

- `chroma_key` (default) keys a flat key-colour backdrop, then trims `--trim-border` px (4) and cleans `--edge-clean-depth` px (3) of dark or key-coloured pixels at each cell edge. Set both to 0 when the source needs no border repair.
  - `--key-quality auto` (default) uses the soft still matte for smooth art and the binary magenta keyer for `--resampler nearest`. `soft` keeps anti-aliased edges as partial alpha and un-mixes the key colour out of them; `hard` is the legacy binary keyer, byte for byte; `dominance` is a fast channel-dominance key.
  - `--key magenta|green|blue` names the backdrop (default magenta). A green or blue key always uses the soft matte, because the binary keyer is magenta-only.
  - `--despill-radius 1..3` removes key excess within that many pixels of transparency. It can desaturate real purple at an edge; default 0.
  - Quote the `matte.qa` numbers of `pipeline-meta.json` (opaque key pixels, ring spill share, semi-transparent share, enclosed pockets) when you claim a clean key. After a despill, `matte.qa_after_despill` measures the published sheet again.
  - The `qa` envelope warns with `key_residue` (opaque key-coloured pixels) and `key_ring_spill` (more than 1% of the outer edge ring still key-coloured, the video residue gate). The binary key leaves that fringe on anti-aliased edges; `--despill-radius 1` or `--key-quality soft` clears it. Both are warnings: `--strict-qc` does not fail on them.
- `native_alpha` needs real transparent pixels plus visible art and keeps RGBA as delivered, including purple. A painted checkerboard is refused.
- `opaque` needs fully opaque input and only works for single images. A grid of opaque cells is refused: package complete rectangular frames with `assemble_frames.py` instead.

## Geometry v2

- Measurement counts pixels with alpha above `--alpha-geometry-threshold` (16): components, subject boxes, anchors, edge checks, clamping and the output subject height. The threshold never changes a pixel.
- Components are 8-connected, so a 1-px diagonal blade or spear stays one piece.
- The anchor sits on the ground line, the bottom edge of the main component's lowest row, measured on the subject's own box (a held spear cannot become the feet). `--anchor-mode`: `feet` (median support column, the default with `--align feet`), `stance` (middle of the support span; a mirrored pose gets a mirrored anchor, so turns do not slide), `bbox`, `center` (default with `--align center`), `centroid`, or `legacy-p98`.
- `--anchor-px X,Y` declares the anchor in source-cell pixels for every frame, for sheets drawn on a known registration point (for example an anchor template).
- Source-edge checks use the unpadded subject box; `--component-padding` only pads the crop of `fit`.
- Every frame record has `trim_offset` and `source_rect` (the subject box in the input sheet's pixels, `null` for an empty cell), so results map back to the source.

Changed defaults and their legacy switches:

| New default | Legacy switch |
|---|---|
| 8-connected components | `--connectivity 4` |
| anchor on the bottom edge of the lowest stable row | `--anchor-mode legacy-p98` |
| geometry ignores alpha at or below 16 | `--alpha-geometry-threshold 0` |
| alpha hygiene `both` for native alpha | `--alpha-hygiene none` |
| fractional nearest scales refused | `--legacy-fractional-nearest` |
| soft key for chroma + lanczos | `--key-quality hard` |
| a profile/flag conflict is an error | `--profile-override` |
| `--align bottom` is an alias of `feet` (warns) | none |
| `preserve` samples every cell on one shared grid and moves frames by whole output pixels, so static parts never shimmer; its frames differ slightly from earlier releases | none |

## Scale strategies and resampling

- `fit` (default) scales each subject box into `--fit-scale` of the cell, per frame or with one `--shared-scale`. It erases drawn motion and size changes; use it for isolated poses and icons.
- `preserve` uses one scale for the sheet, resamples every cell on the same sampling grid and moves each frame by whole output pixels so its anchor lands on the shared origin. A body part that does not move gets identical pixels in every frame. Frames that would leave the cell are shifted inside and reported as clamped. `geometry.injected_shift_px` in the metadata reports how far the frames were moved relative to each other: that motion is erased.
- `registered` uses one scale and one offset for every frame: jumps, bob and recoil drawn in the sheet survive. Use it for sheets drawn on a shared registration point; its scale reference ignores airborne frames.
- `--resampler nearest` keeps pixel-art colours and only accepts whole-pixel scales (N or 1/N). Give `--pixel-scale N` (output pixels per art pixel) and, for art drawn at M source pixels per art pixel, `--logical-pixel M` (the centre of every art pixel is sampled). `qc_summary.pixel_grid_off_edges` counts colour edges off the pixel grid (0 for integer scales). `--legacy-fractional-nearest` restores uneven 1-2 px pixels.
- `lanczos` (default) suits smooth art; resampling works on premultiplied colour, so soft edges do not darken.

## Sizes that do not divide into the grid

Host image tools return their own canvas: 1254x1254, 1672x941 and 1774x887 are common. `process` refuses a size that does not divide exactly and names the grid as rows x columns. Then either:

- `--grid-rounding nearest`: cell edges are rounded, so cells differ by at most 1 px (1254 px in 4 columns gives 314, 313, 314, 313);
- `--pad-to-grid`: transparent padding until the grid divides; `geometry.pad_offset` records it and `source_rect` stays in the original image's pixels.

## Components

`--component-mode all` (default) keeps every component; `--min-component-area N` drops specks smaller than N pixels. `largest` keeps only the main component and deletes everything else, including separate swords, hands, ornaments and FX sparks (a probe sheet lost its staff sparks this way and still passed strict QC). Use `all` for pixel art and FX; use `largest` only for one-piece bodies after inspection.

## QC and scale profiles

`--strict-qc` fails on empty frames, clamped frames, output-edge contact and source-edge contact, plus the optional limits below. A failed run publishes nothing; an existing output folder is never touched. Exit status: 0 on success (the `qa` envelope may still warn), 1 for an `error: ...` (nothing published), 2 for a usage error. Scale profiles, Godot contracts and `--prompt-file` may be UTF-8 with or without a byte-order mark (Windows PowerShell 5.1 writes one).

- `--max-body-scale-cv` limits the variation of the visible output subject height across frames (0.08 suits grounded humanoids; crouches, flight and creature posture changes legitimately exceed it).
- `--max-anchor-y-std` limits the normalized vertical anchor spread (0.05).
- `--scale-profile` compares the median output subject height with the profile's reference height (`--max-profile-scale-drift`, 0.08 recommended). Version 2 profiles store `reference.output_subject_height_px`; version 1 profiles are still read and keep their old cell-area metric. A flag that differs from the profile is an error unless `--profile-override`; applied values are printed and recorded under `profile_applied`.
- `--allow-source-edge-touch` accepts a visually reviewed raw contour that touches its cell edge. It never accepts clipped output, clamping or empty frames. Regenerate a cut-off snout, tail or weapon.

Numbers do not prove pose quality, identity, timing or the loop seam; review frames at game size.

## Outputs

The output folder holds `raw-sheet.<ext>` (byte copy of the input; `raw.<ext>` for single images), `raw-sheet-clean.png` (the keyed sheet that was split), one PNG per cell, `sheet-transparent.png`, `animation.gif` or per-row `<direction>-strip.png` and `<direction>.gif`, `prompt-used.txt` when a prompt was given, and `pipeline-meta.json`. On success the command prints one JSON line with the output folder and metadata path.

`pipeline-meta.json` follows `generate2dsprite.pipeline_meta.v2` in [schemas/sprite.schema.json](schemas/sprite.schema.json): geometry, hygiene, provenance (input sha256, Pillow mode, bit depth, conversion), the matte report, per-frame records and a `qa` envelope with every check, its method and what it does not prove. Paths in it are relative to the output folder.

PNGs are the alpha source of truth. GIF previews have binary alpha and store frame times in 10 ms units: `--duration 125` plays as 120 ms, which is printed as a warning and recorded in `gif_decoded_duration_ms`.

`player_sheet` rows are down, left, right, up. `--direction-order` names the facing of each row, top to bottom (for example `down,up,left,right`, or 8-direction names such as `down-left`), and the strips, GIFs and frame names follow it.

## Anchor template and layout guide

Repeat an accepted character into a fixed template that a generator can trace for every pose (same scale, same feet line). It keeps native RGBA, or produces a magenta RGB template from a chroma source:

```bash
python "<skill-dir>/scripts/make_anchor_layout.py" --input accepted/hero-master.png --rows 2 --cols 4 --cell-width 384 --cell-height 384 --feet-ratio 0.82 --background-mode native_alpha --resampler nearest --output guides/hero-anchor-2x4.png
```

Draw an empty layout guide with safe frames; each margin must be at least 0 and less than half the cell:

```bash
python "<skill-dir>/scripts/make_layout_guide.py" --rows 2 --cols 4 --cell-width 384 --cell-height 384 --safe-margin-x 58 --safe-margin-y 58 --output guides/layout-2x4.png
```

Both refuse an existing output file. Measure the sheet that comes back: when it keeps the template's cell size, its poses share the template's feet point, so process it with `--scale-strategy registered` (or `--anchor-px` at cell width / 2, cell height x feet ratio).

## Godot Sprite3D (optional)

`process --godot-world-height 0.70` writes `godot-sprite3d.json` with a pixel size derived from the visible subject height, the feet origin converted to a Sprite3D offset, frame paths relative to the contract file and the timing. Later actions using the reference profile reuse that pixel size and world height, preserving legitimate crouching and recoil.

```bash
python "<skill-dir>/scripts/generate2dsprite.py" build-godot-bundle --action idle=sprites/hero-idle/godot-sprite3d.json --action attack=sprites/hero-attack/godot-sprite3d.json --default-action idle --one-shot attack --output sprites/hero-godot-bundle.json
```

The bundle refuses an existing file, contracts whose frames do not resolve and incompatible world height or pixel size. Do not compensate a wrong action scale with per-action runtime magnification. Other engines can consume the PNGs and the declared origin directly.
