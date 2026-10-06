# Finishing registered clips (finish_frames.py)

`scripts/finish_frames.py` turns registered RGBA frames (the `frames/` folder of
`register_clip.py apply`, or its output folder) into game-ready frames. **HD is the
default finish**; the pixel finish is an option. Both take the same registration and
the same downscale, so one clip finished both ways has the same canvas and anchor.

The pixel finish ports game-opus55 `tools/pixelate.py` (see the last section). Keying,
matte erosion and un-mixing stay in `video2dsprite.py clean/process` (soft matte). Locks
stay in `register_clip.py apply --lock`, and frame choice stays in `gait_loop.py` and
`retime.py`.

Commands run from your project root. `<skill-dir>` is this skill's folder
(`${CLAUDE_SKILL_DIR}` in Claude Code). `hd` and `pixel` write a new `--output-dir`.
`palette build` and `lineup` write new files. Nothing is ever replaced, and a failed run
publishes nothing. Success prints one ASCII JSON line. Errors print `error: ...` and exit
1. Usage errors exit 2.

## Where it sits

1. `register_clip.py apply`: registered frames on the master canvas.
2. `finish_frames.py hd` or `pixel`: finished frames.
3. `gait_loop.py select` or `retime.py`. A selection made on the registered frames
   still applies, because finished frames keep the source file names and order.
4. `engine_export.py package --clean-dir <fin>/frames --source-size W,H --source-anchor X,Y`.
   Take W,H and X,Y from `finish.json` `size` and `anchor`, and pass no
   `--registration`. A pixel finish adds `--pixel-art --sampling nearest`.

## HD finish (default)

```text
python "<skill-dir>/scripts/finish_frames.py" hd --frames work/hero-idle-reg --output-dir work/hero-idle-hd --target-height 96 --role hero --character hero
```

- **Scale.** `--target-height` is the output height, in px, of the rest-pose body at 1x.
  The rest pose is frame 0 (the master still) or `--rest-frame N`. The body height is
  measured at the 50% alpha contour, counting rows with at least 6 px, so specks and thin
  tips do not count. scale = target / rest height.
- **Grid.** forge_core.resample_rgba box (area) runs on premultiplied float channels.
  The output grid is pinned so that the anchor lands exactly on an output pixel edge. The
  anchor is the stance midpoint on the feet line; `--anchor center` uses the alpha
  centroid, for floating subjects. Every frame shares one grid phase, and the same frames
  placed on another canvas finish to the same bytes.
- **Alpha.** `forge_core.alpha_hygiene(both)` removes the alpha <= 4 halo and faint
  detached islands. The canvas is the union of alpha > 4 over all frames, plus 1 px.
  Straight-alpha PNGs have RGB zeroed under alpha 0, so draw them with premultiplied alpha.
- **Never nearest, never an upscale.** A target taller than the rest pose is refused.
  Finished folders (with a `finish.json`) are refused as input.
- **Display sizes.** `--display-sizes 1x,2x` writes `frames/` (1x) and `frames-2x/`.
  Each size is downscaled again from the source frames, never from another size, and
  each gets its own `size` and `anchor` in `finish.json`.

## Pixel finish

```text
python "<skill-dir>/scripts/finish_frames.py" palette build --frames work/hero-idle-reg work/hero-run-reg work/hero-attack-reg --colors 255 --reserve "#000000" --out work/hero-palette.json
python "<skill-dir>/scripts/finish_frames.py" pixel --frames work/hero-idle-reg --output-dir work/hero-idle-px --target-height 60 --palette work/hero-palette.json --loop-policy cycle
```

The pixel finish starts with the same registration and box downscale as HD. The target
should be about 1/8 to 1/12 of the source; a reduction below 4x warns. It then:

- maps colours to **one shared OKLab palette**. Build it once per character or cast with
  `palette build` and pass it to every clip. Without `--palette`, `--colors N` (default
  255) colours are learned from this clip's finished frames, `--reserve` colours first.
  That palette is written to `palette.json`.
- uses **no dither**.
- applies **temporal hysteresis** (`forge_palette.quantize_sequence`). A pixel keeps last
  frame's index while that colour stays within `--margin` (squared OKLab distance;
  default 0.0004, which is dE 0.02). It keeps last frame's opacity while its alpha stays
  between 0.4 and 0.6. With `--loop-policy cycle` (the default), frame 0 follows the
  last frame. `pingpong` and `oneshot` (attacks) are the other policies.
- cleans up lone pixels (one `cleanup_orphans` pass) and specks (`cleanup_alpha`, which
  also fills 1 px pinholes).
- makes alpha **binary at 0.5**.
- writes an **indexed sheet** (`sheet-indexed.png`, index 255 transparent) only with
  `--indexed-sheet`.

Hysteresis follows file order. A loop window chosen later by gait_loop does not hold its
own seam pair; the seam's two frames come from different places in the file.

## One character, many actions

Finish the idle (or the clip that holds the master still) with `--target-height`. Give
every other action `--scale-ref <idle finish folder or finish.json>`. The inherited scale
is the reference scale x sqrt(reference rest area / this rest area), clamped to +-3%
(pixelate.py). Two checks report on it:

- `scale-ref-area` warns when the correction exceeds 1%. The rest frame may not be the
  base still: pass `--rest-frame` or `--no-area-norm`.
- `scale-ref-clamp` warns when the clamp applied.

`--character` makes `--scale-ref` refuse a clip of another character. All actions of one
character then share one body height.

## Cast line-up

```text
python "<skill-dir>/scripts/finish_frames.py" lineup --sets work/hero-idle-hd work/slime-idle-hd boss=work/ogre-idle-hd work/wisp-idle-hd --out work/cast-lineup.png
```

What each `--sets` entry can be:

- a finished clip;
- a folder of one character's finished clips (an `idle` clip represents the set, and
  the `one-height` check warns when the clips' rest heights differ by more than 1 px);
- a plain frame folder (frame 0 is measured).

`ROLE=DIR` overrides the role that `--role` recorded.

**Rules** (`--rules`, default `hero:1.0,mob:<=1.0,boss:~2.0,spirit:~1.0`) are ratios to
the first set whose role is `--reference-role` (default hero):

- **Equal** (a bare number): within `--exact-tolerance` (default 2%), and a 1 px
  difference always passes.
- **`~` (about):** within `--about-tolerance` (default 10%).
- **`<=` and `>=`:** to the half pixel.
- **Metric:** heights are the rest-pose body heights from `finish.json`. `--area-roles`
  (default `spirit`) compares sqrt(opaque area) instead.

The PNG shows every rest frame on one ground line twice: at x1 with labels, and at x3 as
a nearest review zoom. A dotted line marks the reference height. The report goes to
`<out>.json` (`video2dsprite.lineup.v1`, with a QA envelope). A failed rule still writes
both files; it prints `error: lineup rule failed: ...` and exits 1.

## Outputs

| File | What |
| --- | --- |
| `frames/` | 1x finished frames, same names as the source frames |
| `frames-<k>x/` | each extra `--display-sizes` factor |
| `review-contact.png` | up to 12 frames at x1 and at x3 (or x2) nearest zoom; look before accepting |
| `palette.json` | pixel: the learned palette (palette.v1), when no `--palette` was given |
| `sheet-indexed.png` | pixel `--indexed-sheet`: every 1x frame in a grid, indexed PNG |
| `finish.json` | `video2dsprite.finish.v1` with the QA envelope |

`finish.json` holds:

- **Rest pose and scale:** `rest` (frame, top, feet, height and area in source px,
  stance x, centroid), `anchorSource`, `scale`, `scaleSource`, `targetHeight`, `scaleRef`.
- **Geometry:** `size` and `anchor` (1x, output px), `engine` (the engine_export
  geometry flags), `displaySizes`.
- **Measured rest pose:** `restOutput` (nominal and measured rest height and area at 1x).
- **Pixel settings:** `palette`, `pixel` (threshold, margin, band, cleanup, the
  per-frame baseline), `indexedSheet`.
- **Frames:** `frames` (file, sha256, source file and sha256, opaque px).
- **QA:** `flicker`, `specksRemoved`, `alphaBinary`, `review`, and `qa` (status, method,
  notProven, checks, hashed inputs and outputs).

## QA numbers

All flicker figures are mean counts per consecutive frame pair. A cycle adds the
last-to-first pair.

| Figure | Counts | Includes real motion |
| --- | --- | --- |
| `alphaFlipsPerPair` | opacity toggles (pixel: binary alpha; HD: the 50% contour) | yes |
| `indexFlipsPerPair` | palette-index changes on pixels opaque in both frames (pixel) | yes |
| `noiseFlipsPerPair` | either change where the finished source barely moved (OKLab dE < 0.02, alpha within 5%): pure flicker | no |
| `edgeFlipsPerPair` | the opacity toggles among those: silhouette-edge flicker (sprite-gen's target is 1.1) | no |

The pixel finish also quantizes every frame alone, as the baseline. `perFrameNearest` and
`noiseFlipReduction` show what hysteresis saved, and the `hysteresis` check warns if it
saved nothing.

`specksRemoved` counts:

- pixel: lone opaque specks removed and pinholes filled (with `lonePixelsFixed`
  reported beside it);
- HD: px in detached faint islands.

`alphaBinary` is true for pixel finishes.

**Checks:**

- `no-upscale`;
- `rest-height` (measured against nominal, at most 1.5 px apart);
- `empty-frames`;
- `pixel-reduction`, `alpha-binary`, `palette-fit` and `hysteresis` (pixel);
- `scale-ref-area` and `scale-ref-clamp` (with `--scale-ref`).

**Status:**

- `fail`: nothing is published;
- `warn`: a check warned, and `--strict` refuses to publish;
- `needs-visual-review`: every number passed, but a person or agent still has to look.

## Packaging note

engine_export's key-residue gate flags outer-ring pixels with min(R, B) - G > 20 under a
magenta key. A deep crimson design colour such as (186, 12, 33) scores 21. At 60 px a red
scarf is a large share of the silhouette ring, so a clean finish can be refused for its
design colour. Check which colours were flagged. If they belong to the design, package
with `--allow-key-residue`, which records the override. Real key fringe is fixed when
keying (`clean --erode N`), never by finishing.

## Library

The sprite-set flow calls the functions directly:

```python
finish_clip(frames_dir, out_dir, *, mode="hd", target_height, scale_ref=None, palette=None, rest_frame=0,
            anchor="feet", display_sizes=(1.0,), colors=None, reserve=(), seed=0, loop_policy="cycle",
            margin=0.0004, area_norm=True, indexed_sheet=False, pattern="*.png", clip=None,
            character=None, role=None, strict=False) -> dict
build_cast_palette(frame_dirs, colors=255, *, reserve=(), seed=0, frames_per_set=12,
                   samples_per_set=22000, name=None) -> (Palette, stats)
lineup(set_specs, out, *, rules=..., reference_role="hero", area_roles=("spirit",), exact=0.02,
       about=0.10, report=None) -> dict
```

`finish_clip` returns the CLI summary. Pass `target_height=None` with `scale_ref`. It
raises `FinishError` (a ValueError) on refused input or failed QA, and FileExistsError for
an existing output.

## Ported from game-opus55 pixelate.py

| pixelate.py | Here |
| --- | --- |
| `_sil` (:383-394): area, feet line, top, stance midpoint | `measure_body` |
| `prep_vframes` scale and scale_ref (:478-488) | `plan_scale` |
| anchor and pinned grid (:489-495, :528-535) | `full_grid` and `reduce_frame` |
| `resize_rgba` premultiplied area resize (:131-151) | forge_core.resample_rgba box |
| global palette: fixed colours plus k-means++ (:586-615) | `palette build`, forge_palette.build_palette |
| `quantize_seq` hysteresis (:228-247), no dither (:622) | forge_palette.quantize_sequence |
| `cleanup` and `cleanup_alpha` (:211-260) | forge_palette cleanup_orphans and cleanup_alpha |
| indexed sheet (:641-652) | `--indexed-sheet` |

The ASF equivalents of the rest:

- chroma key, unmix and erode (:57-128, :371-381) are the soft matte in `video2dsprite.py clean/process`;
- feet, x and hip locks (:510-524) are `register_clip.py apply --lock`;
- dedupe and the loop window (:413-452) are `gait_loop.py`;
- per-tick timelines are `retime.py`.
