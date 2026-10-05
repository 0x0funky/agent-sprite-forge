# Palettes, logical pixels and indexed exports

Use this reference when sprites must share one game palette, when an image-model result should become strict pixel art, when a clip flickers between palette colours, or when an engine wants palette swaps (hit flash, frozen, team colours). Two tools cover it:

- [`palette_tool.py`](../scripts/palette_tool.py): `build`, `apply`, `lock`, `luts`, `variants` and `quantize-seq`.
- [`pixel_reduce.py`](../scripts/pixel_reduce.py): image on a clean integer grid -> logical pixels.

Both are built on the shared OKLab library `scripts/forge_palette.py`. Run every command from the project root. `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs always go to a new `--output-dir` inside the project. Work is staged and published only when its QA passes, so a failed or refused run leaves nothing behind. Each run prints one JSON line naming the folder and its metadata file.

## Rules that do not change

- Image outputs are 8-bit RGBA PNGs with binary alpha and RGB zeroed under alpha 0. They are valid input for `build_animation_clips.py`.
- Indexed PNGs are written only with `--indexed`, into an `indexed/` subfolder. They are an engine export and are **never builder input**.
- Colours are compared in OKLab. A distance (dE) of 0.02 is about one visible step. Exact palette colours always keep their index.
- Palettes are ordered: a colour's position is its index in indexed files and LUTs. `palette.json` (`generate2dsprite.palette.v1`, see [the sprite schema](schemas/sprite.schema.json)) is the authoritative file. The `.gpl`, `.hex` and `.pal` files carry colours only.

## 1. Decide the logical resolution first

A prompt such as "32x32 pixel art" does not make an image-model result a 32x32 sprite. Such results are usually painterly, with soft edges and thousands of colours: the 2026-10-05 fox sheet has about 70,000 visible colours and no grid. Decide the logical size before generating: the body height in game pixels and the display scale.

- If the result really sits on an integer grid (a nearest-upscaled render, or a model that respected the grid), reduce it with `pixel_reduce.py`.
- If it does not, `pixel_reduce.py` refuses with `no clean grid` and the numbers it measured. Keep the art at its resolution and quantize it with `palette_tool.py apply`, or regenerate. `--force --period N` reduces it anyway, but the result is lossy, marked `warn`, and needs a visual check.

```text
python "<skill-dir>/scripts/pixel_reduce.py" --input output/hero/hero-6x.png --upscale 6 --output-dir output/hero/1x
python "<skill-dir>/scripts/pixel_reduce.py" --input output/hero/hero-6x.png --palette output/palette/palette.json --output-dir output/hero/1x-locked
python "<skill-dir>/scripts/pixel_reduce.py" --input output/raw/fox.png --force --period 8 --output-dir output/fox/forced-8px
```

How the grid is judged:

- `forge_palette.detect_grid` (the same results as codeart2d's `detect_grid`) searches integer periods 2..32 and their phases.
- A grid is clean when the boundary score is at least 0.5 and at least 90% of the blocks are uniform within 24 levels.
- Each block then takes the mode of its palette indices. `--method box` takes the premultiplied mean and `--method center` the centre pixel.
- Alpha becomes binary. With no `--palette`, the image's own colours are kept when there are at most 256, otherwise 32 are learned.
- `--palette` with a locked palette keeps every colour exact even through compression noise.

QA (`pixel-reduce-qa.json`, a common QA envelope):

| Check | Meaning |
|---|---|
| `grid_clean` | Score and uniformity against the thresholds. A forced grid shows here as `warn`. |
| `reconstruction_mismatch` | The 1x result is re-expanded and compared with the input. Above 5% differing it warns. |
| `lost_visible_px` | Visible pixels in partial edge strips that whole blocks do not cover. |
| `period_agrees` | With `--period`, whether detection agrees. A wrong period, or a divisor of the true one, warns. |
| `output_off_palette_px`, `output_partial_alpha_px` | Measured on the written files. Both must be 0. |

`--strict` turns warnings into failures. Fractional grids, such as a 3.46 px pixel, are not detected.

## 2. Build a palette

```text
python "<skill-dir>/scripts/palette_tool.py" build --input output/hero/frames --colors 16 --output-dir output/palette
python "<skill-dir>/scripts/palette_tool.py" build --input output/hero/frames output/enemy/frames --colors 32 --reserve "#1a1c2c" "#f4f4f4" --balance --name game --output-dir output/palette
```

- `build` runs a deterministic weighted k-means++ in OKLab over the unique colours of every pixel with alpha >= 128. The same inputs and `--seed` give the same bytes.
- Art with no more colours than asked for keeps exactly its colours.
- `--reserve` colours (outlines, UI or engine colours) stay first and unchanged. Samples within dE 0.03 of a reserved colour are skipped, so the learned colours go where the reserved ones do not reach.
- `--balance` gives every input the same weight, so a small item is not drowned out by a large sheet.
- It writes `palette.json`, `palette.gpl` (GIMP, Aseprite), `palette.hex` (Lospec) and `swatches.png`. `--formats` can add JASC `pal`.
- `palette-qa.json` records the OKLab fit of every input (mean, p95 and max dE).

## 3. Apply, lock and grow without churn

```text
python "<skill-dir>/scripts/palette_tool.py" apply --palette output/palette/palette.json --input output/hero/frames --output-dir output/hero/on-palette
python "<skill-dir>/scripts/palette_tool.py" lock --palette output/palette/palette.json --source output/hero/frames --output-dir output/palette-lock
python "<skill-dir>/scripts/palette_tool.py" build --base output/palette-lock/palette.json --input output/enemy/frames --colors 24 --output-dir output/palette-v2
python "<skill-dir>/scripts/palette_tool.py" apply --palette output/palette-v2/palette.json --lock output/palette-lock/palette-lock.json --input output/hero/frames --output-dir output/hero/on-palette-v2
```

`apply` snaps every pixel to its OKLab-nearest colour and makes alpha binary at `--alpha-threshold` (128).

- `--orphans 1` removes isolated single pixels.
- `--keep-alpha` snaps colours but keeps soft edges, for painted art.
- `--max-delta-e 0.05` fails the run when the art does not fit the palette.

`lock` freezes a palette for a set of sources. It writes a copy marked `locked: true` and `palette-lock.json` (`generate2dsprite.palette_lock.v1`). The lock holds the palette's sha256, the locked colours in index order, and every source's sha256 and fit. With `--strict`, every source must already be exactly on the palette.

To add colours for new art, build with `--base` on the locked palette. The locked colours keep their indices, new colours are appended, and samples the locked colours already serve are skipped.

Re-applying the grown palette to the old sources could still move their pixels onto the new colours. `apply --lock` and `quantize-seq --lock` prevent that: every input whose sha256 is recorded in the lock is quantized with the locked colours only, so its output stays byte-identical. A grown palette that changed a locked colour is refused. A clip must be locked whole or not at all.

The transparent index is 255 by default. Index maps and indexed PNGs then keep working as the palette grows. `build --transparent-index N` picks a free slot right after the colours for smaller indexed files. Such a palette cannot grow past that slot, and `build --base` refuses to move it.

## 4. Indexed export

`apply --indexed`, `quantize-seq --indexed` and `pixel_reduce.py --indexed` write `indexed/<name>.png`:

- PLTE holds the palette; tRNS gives one transparent index alpha 0.
- The bit depth follows the PLTE length (1, 2, 4 or 8 bits), and there are no metadata chunks.
- Decoding gives back the RGBA output exactly.

With index 255 the PLTE has 256 entries, about 1 KB, so a single tiny sprite can be larger than its RGBA PNG. Whole sheets come out smaller.

Engines that decode PNGs normally see the same pixels as the RGBA file. Palette-swap shaders read the raw index and look it up in a LUT. Keep feeding the RGBA files to `build_animation_clips.py`.

## 5. Clips: temporal hysteresis

```text
python "<skill-dir>/scripts/palette_tool.py" quantize-seq --input output/hero/run-frames --palette output/palette/palette.json --loop-policy cycle --output-dir output/hero/run-quantized
python "<skill-dir>/scripts/palette_tool.py" quantize-seq --input output/slime/idle-frames --colors 12 --loop-policy pingpong --output-dir output/slime/idle-quantized
```

Video frames and image-model frames carry noise, so per-frame nearest-colour quantizing flips pixels between two close palette colours every frame. `quantize-seq` keeps a pixel's previous index while that colour is within the margin of the best match (squared OKLab distance 4e-4, dE 0.02). It keeps the previous opacity while alpha is inside 0.4-0.6. Real changes still switch at once.

After quantizing, each frame loses lone specks and isolated pixels (`--orphans`, `--no-despeckle`).

Loop policies:

- `cycle`: frame 0 continues from the last frame (a warm-up pass seeds it).
- `pingpong`: frame 0 follows frame 1. Only the forward frames are written; set `loop_policy: pingpong` in the clips manifest so the way back reuses them pixel for pixel.
- `oneshot`: no seeding.

`quantize-qa.json` compares the result with per-frame nearest quantizing on the same clip. It reports `idx_flip`, `noise_flip` (pixels whose source barely moved) and `alpha_flip`, plus `noise_flip_reduction`.

On synthetic clips, noise flips fall with the noise level:

| RGB noise sigma | Noise flips cut |
|---|---|
| 1.5 | 64-88% |
| 3 | 47-67% |
| 5 | 36-53% |
| 8 | 24-41% |

On a real image-to-video clip (the game-opus55 study clip: 25 frames, a 255-colour palette, as 8-bit PNG frames) `quantize-seq` cuts noise flips by 46%, and by 49% as a loop with the wrap. On those frames it gives the game's own quantizer's index maps byte for byte; the study measured 45% on the game's unrounded frames. For noisier sources, raise `--margin`: `1e-3` is dE 0.032.

## 6. LUTs and baked variants

```text
python "<skill-dir>/scripts/palette_tool.py" luts --palette output/palette/palette.json --skin-map output/palette/team-blue.json --output-dir output/palette-luts
python "<skill-dir>/scripts/palette_tool.py" variants --palette output/palette/palette.json --input output/hero/on-palette --variant hitflash frozen silhouette --output-dir output/hero/variants
```

`luts` writes three files:

- `luts.json` (`generate2dsprite.palette_luts.v1`): one colour per palette index for each row. The rows are identity, `hitflash` (white), `frozen` (an icy OKLab tint that keeps the lightness order), `silhouette` (black), and one `skin-<name>` row per `--skin-map`.
- `luts.png`: the same rows as a 256-wide texture. Row r is `rows[r]`. The transparent index and unused columns are transparent.
- `palettize-32.png`: an RGB-to-palette strip for post-process palettizing. Texel `(x = r + 32 * b, y = g)` holds the nearest palette colour of 5-bit level `(r, g, b)`. Sample it with nearest filtering.

A skin map is a JSON object `{"#from": "#to"}` or `{"index": "#to"}`, or a palette with one colour per index. `--flash-color` and `--silhouette-color` change those rows. `--snap` keeps every variant colour on the palette and adds an `index` remap per row.

`variants` bakes the same rows into frames, `<variant>/<name>.png`, with alpha unchanged. The inputs must already be on the palette; `--quantize` snaps them first.

## 7. Limits

- Palettes and grids are deterministic on one machine. Another OS's floating point can flip a pixel that sits exactly between two colours. Exact palette colours never move.
- The hysteresis numbers come from synthetic noise and one study clip. Check the QA flips on your clip.
- `frozen` is a fixed tint. Review it in game, as with every variant.
- Logical-resolution choices for code art (up to 48 px visible height by default) belong to codeart2d. `pixel_reduce.py` is for images that already have a grid.
