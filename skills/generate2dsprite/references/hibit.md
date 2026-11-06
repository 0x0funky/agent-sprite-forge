# Hi-bit finish (flat art -> real pixel art)

The hibit finish ports the hi-bit studio recipe: the image model paints **clean flat art** and the
finish makes the pixels. Use it when the user wants that crisp, modern pixel-art look (Owlboy-like
hi-bit, 480x270 games, pixel-art scene loops) rather than the bold `pixel` finish.

Never ask an image model for "pixel art": it paints fake, uneven pixels. Ask for flat lineless art and let the
finish quantize it.

## 1. Master still: flat lineless art

`master_still.py generate --finish hibit ...` sets the style line: lineless shapes with no outline, every
material in at most 3 flat tones (highlight, base, shadow), cool purple-leaning shadows and warm highlights,
crisp angular light and shadow shapes, a bold readable silhouette, no gradients, airbrush, texture noise,
dithering or pixel grid. Keep the usual rules: flat key (`--key green` or `blue` for purple and pink designs),
no FX in the still, nothing touching an edge.

## 2. Animate and finish (video2dsprite)

`sprite_set.py plan --master <master.json> --finish hibit` (the master's finish is the default): 80 px hero
body height (a boss twice that; give mobs `--target-height` from the master framing, for example
471/788 x 80 = 48 px for a small mob), 12 fps, the motion prompt keeps the "clean flat lineless cel-shaded"
look, no colour lock. One clip of one action:

```
python "<video2dsprite>/scripts/finish_frames.py" hibit --frames <registered> --output-dir <new> --target-height 80
```

| What it does | Default | Flag |
|---|---|---|
| One palette per character: k-means++ on luminance-weighted RGB of up to 200000 opaque SOURCE pixels | 16 colours | `--colors N`, `--palette <palette.json>` |
| 8x8 supersampled sub-pixels indexed to the palette; every output pixel = the colour covering most of its cell (a majority vote, never a blend) | coverage 0.45 | `--coverage` |
| Temporal hysteresis: the previous frame's colour stays while it holds 80% of the winner; an opaque cell stays down to 33% | on | `--margin` (index hold) |
| Lone pixels take the 4-neighbour majority unless they contrast (eyes, glints stay); 1 px islands go | 2 passes | |
| 1 px outline outside the silhouette | `auto`: the sprite's dominant saturated hue, very dark (OKLab L 0.22) | `--outline auto\|#RRGGBB\|selective\|dark\|none` |

The outline colour is taken from the image, never a fixed signature colour; a studio colour is a deliberate
`--outline #RRGGBB`. `palette.json` names it `"outline"`, so every other clip of the character reuses the same
palette and outline with `--palette <first clip>/palette.json` (sprite_set does this from the scale-reference
action). The finished frames are palette-exact with binary alpha: draw them with nearest sampling at whole
zoom (`engine_export.py package --pixel-art --sampling nearest`). `finish.json` `hibit` records the numbers;
`qa.flipsPerPair.noiseReduction` compares the held majority with the same majority without hysteresis.

## 3. Plates (generate2dmap)

Paint the background flat too, then `generate2dmap/scripts/hibit_plate.py plate --input <painting>
--output-dir <new>`: 480x270 (x4 = 1920x1080), 14 colours, majority downsample, lone-pixel cleanup and one
3x3 majority pass (the paper-cut look), plus `palette.json` and `shade-maps.json` (per colour the in-palette
step darker and lighter, for lighting and hit flashes without new colours). Small bright details (fireflies,
lamps) can merge into a bigger colour cluster: put them back as FX.

## 4. FX: route A (pixelfx) or route B (image route)

Hibit games take their effects from one of two routes. Ask the machine first; a route the user names wins:

```
python "<skill-dir>/scripts/fx_pixelfx.py" check [--pixelfx <checkout>]
```

It prints `"route": "pixelfx"` when a pixelfx checkout (`--pixelfx` or `PIXELFX_DIR`) and Node.js 18+ are found,
else `"route": "image"` with the reason and the route B steps. Say which route ran when you show the FX.

| | A: pixelfx (recommended when installed) | B: image route (always available) |
|---|---|---|
| Source | [pixelfx](https://github.com/patch-gg/pixelfx) by Patch, code-driven pixel FX, MIT | an image model paints a 2x2 FX sheet |
| Frames | 16-90 per effect at 30 fps: smooth particles, rings, trails | 4 per effect: reads, but steps |
| Cost | none (local Node) | one image per effect |
| Look | pixelfx's own style, recoloured to the game | the game's own flat style |
| Credit | ship `CREDITS.txt`; disclose as code-drawn by pixelfx | the image route, like every sprite |

### Route A: pixelfx

pixelfx is a library of code-driven pixel-art effects (fire, arcane, impacts, weather, pickups, UI) that matches
the hibit look. It is a separate MIT project, not part of the Forge, and its effects are code-drawn: say so when
you show them. Install it yourself (`git clone https://github.com/patch-gg/pixelfx`, Node.js 18+), then:

```
python "<skill-dir>/scripts/fx_pixelfx.py" list arcane --pixelfx <checkout>
python "<skill-dir>/scripts/fx_pixelfx.py" render --pixelfx <checkout> --id arcane/star-fall \
    --ramp "#2a1640,#7b4fb0,#f2c84b,#fff6c8" --snap <character>/palette.json --output-dir <new>
```

`--ramp` (dark to light) is the effect's own palette option; `--snap` maps every pixel to the nearest colour of
a hibit palette so the FX share the game's colours. Alpha is binary by default: pixelfx's glow is additive light,
which exported on transparency becomes near-black translucent pixels (dark smudges on a light plate); `--alpha
keep` keeps them for dark backgrounds or additive blending. The output has
`frames/`, `sheet.png` + `sheet.json` (Aseprite-style), `preview.gif`, `fx.json` (repository, commit, version,
license, copyright lines, effect, options) and `CREDITS.txt` (pixelfx's LICENSE and NOTICE). Keep CREDITS.txt
with the frames and credit pixelfx in the game. pixelfx's stand-in character is never drawn, and the Patch
Pixel font in its gallery is not MIT and is never used.

### Route B: the image route

1. Prompt one square 2x2 sheet: "4 frames of <effect> in 2 rows x 2 columns, read left to right, top row first",
   the 4 phases (for an impact: contact flash, wide burst with a ring, peak burst with sparkles, fading sparkles),
   each frame centred in its quarter inside the middle 70 percent with one scale and one centre point, the same
   flat lineless 3-tone style as the master and the character's palette colours by hex, solid opaque shapes on a
   flat key, no text, no ground. Generate it with `route_media.py image`.
2. Key and register with one scale and one offset for every frame, so a burst grows and a poof spreads as drawn:
   `generate2dsprite.py process --input <sheet> --target asset --mode sheet --rows 2 --cols 2 --scale-strategy
   registered --cell-size 512 --output-dir <new>`. (`fit` and `preserve` re-centre each frame: the effect jumps.)
3. Make the pixels: `hibit_fx.py pixelize --frames <process output> --target-px 60 --palette <character
   palette.json> --anchor bottom --frame-ms 85 --output-dir <new>`. One union crop and one scale, every output
   pixel the palette colour covering most of its cell, sparkles kept, binary alpha; `--anchor center` for sparks,
   projectiles and pickups, `bottom` for impacts and floor poofs; `--loop` for projectiles and pickups.
