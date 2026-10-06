# Finishing registered clips (finish_frames.py)

`scripts/finish_frames.py` turns registered RGBA frames (the `frames/` folder of
`register_clip.py apply`, or its output folder) into game-ready frames. **HD is the
default finish**; the pixel finish is an option. Both take the same registration and
the same downscale of the silhouette, so one clip finished both ways has the same
anchor; the pixel finish's outline adds 1 px around it.

Both finishes can put every frame on a fixed cell (`--canvas`) and lock the design
colours to the master (`--colour-lock`, the default inside sprite sets). The plain pixel
finish ports game-opus55 `tools/pixelate.py` (see the last section); the default pixel
finish is the bold one below. Keying, matte erosion and un-mixing stay in
`video2dsprite.py clean/process` (soft matte). Locks stay in `register_clip.py apply
--lock`, and frame choice and timing stay in `gait_loop.py` and `retime.py`.

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

## Pixel finish (bold by default)

```text
python "<skill-dir>/scripts/finish_frames.py" pixel --frames work/fox-run-reg --output-dir work/fox-run-px --target-height 47 --canvas 48x64 --colors 32
python "<skill-dir>/scripts/finish_frames.py" palette build --frames work/hero-idle-reg work/hero-run-reg work/hero-attack-reg --colors 32 --reserve "#000000" --out work/hero-palette.json
python "<skill-dir>/scripts/finish_frames.py" pixel --frames work/hero-idle-reg --output-dir work/hero-idle-px --target-height 60 --palette work/hero-palette.json --loop-policy cycle
```

A video frame downscaled by area averaging comes out soft: every edge pixel is an
in-between colour, thin dark lines and eyes melt into the fill, and a 255-colour palette
keeps all of it (the 2026-10-06 live fox had 155-162 colours per frame from the 255-colour
plan palette; the palette itself was applied). The pixel finish therefore does more than
quantize:

1. **Alpha** comes from the same box downscale as HD (the same silhouette).
2. **Crisp colour.** Each output pixel is split into 4x4 sub-pixels (a box downscale at 4x
   on the same pinned grid). Their colours form two OKLab clusters (alpha-weighted Lloyd
   steps); the pixel takes the heavier cluster, so it is one real colour of the art, never
   an average across an edge. A cluster 0.15 L darker wins from 35% of the pixel, so thin
   dark lines, seams and eyes survive. Temporal hysteresis: last frame's cluster is kept
   while it still holds 30% of the pixel (no 50/50 flicker). `--downscale box` is the
   plain area average.
3. **Lift.** OKLab lightness is spread around the rest pose's mean lightness
   (`--contrast`, default 1.1) and chroma is scaled (`--saturation`, default 1.15); 1 is off.
4. **One palette, enforced.** `--palette` (a shared cast palette), or `--colors N`
   (default **32**) learned from the lifted frames, `--reserve` colours first. Every frame
   uses at most the palette's colours: the `palette-fit` check fails on an off-palette
   colour or on more colours per frame than the palette has, and `coloursPerFrameMax` is
   reported. A 45-90 px sprite reads as pixel art with 16-32 colours; 255 looks like a
   downscaled painting.
5. **No dither** and **temporal hysteresis** (`forge_palette.quantize_sequence`): a pixel
   keeps last frame's index while that colour stays within `--margin` (squared OKLab
   distance; default 0.0004, dE 0.02) and its opacity while alpha stays between 0.4 and
   0.6. With `--loop-policy cycle` (the default), frame 0 follows the last frame;
   `pingpong` and `oneshot` (attacks) are the other policies.
6. **Cleanup.** Specks and pinholes (`cleanup_alpha`), then two passes of lone-pixel
   cleanup: a pixel without a 4-neighbour of its own index takes the neighbours' majority,
   unless it is 0.22 L lighter or darker than every neighbour (an eye, a highlight), which
   stays.
7. **Outline** (`--outline`, default `selective`): 1 px on the transparent pixels
   4-adjacent to the sprite. `selective` paints each in the palette's dark shade of the
   darkest neighbouring colour (OKLab L at most 0.34 and 0.22 below it, the hue kept: dark
   red-brown beside orange fur, dark teal beside a teal tunic; the darkest colour when the
   palette has no such shade); `dark` uses the darkest palette colour everywhere; `none`
   skips it. The target height includes the outline (2 px), and the feet anchor moves to
   the outline's bottom edge, so the outline stands on the ground line.
8. **Binary alpha** at 0.5. `--indexed-sheet` writes `sheet-indexed.png` (index 255
   transparent).

`--downscale box --contrast 1 --saturation 1 --outline none` is the plain v1 finish (the
pixelate.py port: one orphan pass, the hd frames quantized as they are). Hysteresis
follows file order: a loop window chosen later by gait_loop does not hold its own seam
pair.

On the live fox run (Grok clip, 145 frames, 47 px with the outline): 28-31 colours per
frame instead of 155-162 (old cold-test sheet: 726-919), binary alpha instead of 199 alpha
levels, noise flips 13.7 per pair against 17.7 for the old 255-colour finish.

The finish says nothing about timing. Frame times are the selection's (gait_loop, retime;
`sprite_set.py plan --frame-ms 80` or `--fps 12.5` for exact 80 ms frames on a 25 Hz tick
grid).

## Fixed cell (--canvas)

`--canvas 48x64` puts every frame of every display size on one cell (scaled by the size's
factor), the anchor at one point: `--canvas-anchor X,Y`, or by default the centre column,
1 px above the bottom edge, moved the least that keeps the clip's content (outline
included) inside. Pixels keep their place around the anchor exactly; only the transparent
border changes. A clip that does not fit at the target is finished smaller so it fits
(the `canvas-fit` check warns with the fitted height). `finish.json` records `canvas`
{size, anchor, requestedAnchor}; sprite sets package such a clip without `--crop-union`,
so the cell survives into the atlas.

## Colour lock (--colour-lock)

Image-to-video models drift design colours between frames: the 2026-10-06 run's leather
boots turned olive in one frame and maroon in the next, and a fast hand picked up pink.
`--colour-lock MASTER.png` (the keyed master, RGBA; `rest` uses the rest frame) runs
`colour_lock.py` on the finished frames (pixel: before the lift and the palette):

- the master's colours are learned per height band (5 overlapping bands from the feet up,
  12 OKLab k-means colours each), so a boot pixel can only match boot-height colours;
- every pixel matches the nearest colour of its band and the neighbouring bands; within
  0.12 (fully up to 0.072) it is locked: its chroma offset from that colour, averaged over
  the pixels matched to the same colour within 3% of the body height, is removed (one boot
  drifted maroon comes back, the other boot and the leggings beside it stay as they are);
- pixels within 0.06 of their colour are pulled up to 35% of the way in chroma;
- **lightness never changes** (the shading stays, nothing is posterised) and chroma grows
  by at most max(0.02, half the pixel's own chroma), so a grey never turns brown;
- a pixel whose source colour and opacity barely moved since the last frame keeps half
  of the last output's chroma (temporal smoothing without ghosting; cycles warm up from
  the last frame);
- colours far from every master colour (bleed the design does not have) are left alone:
  the QC colour gate rejects those takes instead.

`--lock-strength` (0..1, default 1) scales every step. `finish.json` `colourLock` records
the stats and, at 1x, `measure()` before and after: hue flips per frame pair (pixels of
similar lightness whose chroma jumps by more than 0.03), the per-region spread of the mean
chroma across frames, and the foreign share. `colour_lock.py measure --frames DIR ...
--master master.png [--loop]` prints the same numbers for any frame folders. Measured on
Aria's live run (24-frame loop): hue flips 352 -> 170 per pair, region spread mean 0.0059 ->
0.0012 (max 0.0245 -> 0.0072), the boots' frame-to-frame chroma step 0.024 -> 0.003.

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
| `palette.json` | pixel: the learned palette (palette.v1), when no `--palette` was given; sprite sets pass the scale-reference action's to every other action |
| `sheet-indexed.png` | pixel `--indexed-sheet`: every 1x frame in a grid, indexed PNG |
| `finish.json` | `video2dsprite.finish.v1` with the QA envelope |

`finish.json` holds:

- **Rest pose and scale:** `rest` (frame, top, feet, height and area in source px,
  stance x, centroid), `anchorSource`, `scale`, `scaleSource`, `targetHeight`, `scaleRef`.
- **Geometry:** `size` and `anchor` (1x, output px), `engine` (the engine_export
  geometry flags), `displaySizes`.
- **Measured rest pose:** `restOutput` (nominal and measured rest height and area at 1x).
- **Pixel settings:** `palette`, `pixel` (threshold, margin, band, cleanup, downscale,
  supersample, lift, outline and `outlinePx`, `coloursPerFrameMax`, the per-frame
  baseline), `indexedSheet`.
- **Canvas and colour lock:** `canvas`, `colourLock` (strength, method, master, per size
  the lock stats and, at 1x, the before and after measurements).
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

- pixel: lone opaque specks removed and pinholes filled (with `lonePixelsFixed`,
  `coloursPerFrameMax` and `outlinePx` reported beside it; the flicker figures are
  counted before the outline, which only follows the silhouette);
- HD: px in detached faint islands.

`alphaBinary` is true for pixel finishes.

**Checks:**

- `no-upscale`;
- `rest-height` (measured against nominal, outline included, at most 1.5 px apart);
- `empty-frames`;
- `pixel-reduction`, `alpha-binary`, `palette-fit` (no off-palette colour, at most the
  palette's colours in every frame) and `hysteresis` (pixel);
- `canvas-fit` (with `--canvas`: warns when the clip was finished smaller to fit);
- `colour-lock` (with `--colour-lock`: the before and after numbers, ungated);
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
            character=None, role=None, strict=False, downscale=None, contrast=None, saturation=None,
            outline=None, canvas=None, canvas_anchor=None, colour_lock=None, lock_strength=1.0) -> dict
build_cast_palette(frame_dirs, colors=255, *, reserve=(), seed=0, frames_per_set=12,
                   samples_per_set=22000, name=None) -> (Palette, stats)
lineup(set_specs, out, *, rules=..., reference_role="hero", area_roles=("spirit",), exact=0.02,
       about=0.10, report=None) -> dict
```

`finish_clip` returns the CLI summary. Pass `target_height=None` with `scale_ref`. The
pixel-only keywords (`palette`, `colors`, `reserve`, `indexed_sheet`, `downscale`,
`contrast`, `saturation`, `outline`) are refused in HD; left as None in pixel they take the
bold defaults. It raises `FinishError` (a ValueError) on refused input or failed QA, and
FileExistsError for an existing output. The bold pieces are library functions too:
`crisp_clusters`/`crisp_choice`, `lift_frame`, `clean_lone_pixels`, `outline_shades`/
`add_outline`, `canvas_window`; the lock is `colour_lock.master_colours`, `lock_frames` and
`measure`.

## Ported from game-opus55 pixelate.py

| pixelate.py | Here |
| --- | --- |
| `_sil` (:383-394): area, feet line, top, stance midpoint | `measure_body` |
| `prep_vframes` scale and scale_ref (:478-488) | `plan_scale` |
| anchor and pinned grid (:489-495, :528-535) | `full_grid` and `reduce_frame` |
| `resize_rgba` premultiplied area resize (:131-151) | forge_core.resample_rgba box (the bold finish takes colour from the crisp 4x split) |
| global palette: fixed colours plus k-means++ (:586-615) | `palette build`, forge_palette.build_palette |
| `quantize_seq` hysteresis (:228-247), no dither (:622) | forge_palette.quantize_sequence |
| `cleanup` and `cleanup_alpha` (:211-260) | forge_palette cleanup_orphans and cleanup_alpha |
| indexed sheet (:641-652) | `--indexed-sheet` |

The ASF equivalents of the rest:

- chroma key, unmix and erode (:57-128, :371-381) are the soft matte in `video2dsprite.py clean/process`;
- feet, x and hip locks (:510-524) are `register_clip.py apply --lock`;
- dedupe and the loop window (:413-452) are `gait_loop.py`;
- per-tick timelines are `retime.py`.
