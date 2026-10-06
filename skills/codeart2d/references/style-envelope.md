# Style envelope: what code art is for

codeart2d draws game art from text that Claude writes (PixelSpec JSON or portable SVG);
scripts render, check and package it. No image model is involved. It is a declared art
source, recorded as `art_source: "code"`, not a placeholder. This page says where it is
strong, where it is not, and how to tell the user.

## Before drawing: name the row

Size is the visible height of the character (outline included), not the canvas.

| Request | Code art | Prefer instead |
|---|---|---|
| Pixel characters, props and palette variants up to 48 px visible height | best fit when code art is requested or no image route exists: exact grid and palette, free variants, identical root in every action | an image model for rich costume, anatomy, dynamic perspective or a likeness |
| Pixel characters 49 to 64 px | possible, but shading stays flat and authoring gets long | same as above |
| Characters above 64 px | not offered as final art | the image route (generate2dmedia `route_media.py`: API key, then local daemon) |
| Flat vector characters, props, UI, HUD icons, 9-slices | when requested: native SVG at any resolution | painterly icons |
| FX: slashes, sparks, impact rings, projectiles, hit flashes, dust | strongest area: timing, alpha, palette and resolution are exact | realistic fire, smoke or water (video route) |
| Tile topology (Wang, blob, platform strips), layouts, collision, spawns | yes; these are data | hand-painted materials (procedural texture repeats) |
| Stylized parallax (gradients, ridges, silhouettes, clouds) | yes | painterly backgrounds |
| HD-2D scenes, key art, portraits, textured terrain | blockouts and guides only | the image route |
| Pose and layout guides for an image model | yes | - |

State the row before you start. Never present code art as image-model output, and never
substitute it silently when the user asked for a painterly look or a likeness of an
existing character: explain the gap and offer the image route, the user's own art, or a
stylized code-art alternative.

## Disclosure

- Every output folder has `codeart-meta.json` with `"art_source": "code"`,
  `"placeholder": false` and `"disclosure": "code-drawn, no image model"`, plus the sha256
  of the spec that made it.
- Say the same in the reply: code-drawn, no image model used.
- The spec files and the rendered art are the user's project files. A named palette or a
  bitmap font added later needs its licence or attribution recorded beside it.
- Do not trace licensed characters into specs.

## Review loop

1. Take the contract from the sprite or map plan: camera, logical pixels, display
   scale, palette, root, clips or map geometry.
2. Check the request against the table above.
3. Pick the representation: PixelSpec for pixel sprites and props, SVG for flat vector
   art and crisp pixel art built from shapes; skeletal SVG rigs and generators for tiles,
   layouts and parallax come with the codeart2d P1 tools.
4. Write the spec, render it, and open the review sheet from `pixel_qa.py --review`
   (1x/2x/4x nearest, light/dark/checker backgrounds, onion skin, palette swatches, QA
   numbers). Fix the text and render again. After three rounds without approval, ask the
   user.
5. Get the user's approval on the main pose before adding actions, variants and exports.

```text
python "<skill-dir>/scripts/pixel_qa.py" --input "out/hero-v1/base/frames/*.png" --palette out/hero-v1/codeart-meta.json --review out/hero-review.png --onion --anchor 16,31 --report out/hero-qa.json
```

The numbers catch symptoms (partial alpha, colours outside the palette, outline gaps,
L-corners, a loop that pops at the wrap, a root outside the canvas). They do not judge
appeal, anatomy or motion; Claude sees still images, so timing and feel still need a
human to play the animation.

## Making code art look intentional

- Palette: 4 to 6 colours per material (highlight, base, shade, dark, outline); share
  the outline colour across the sprite. Variants swap materials, never the silhouette.
- Outline: `solid` for readability on busy backgrounds; `selout` (each edge takes a
  darker shade of the fill it touches) for a softer look. Outlines are drawn last, after
  clean-up, and never thicken diagonals.
- Light from one direction (top-left by default); shade forms, not outlines.
- Silhouette first: check the 1x view on light and dark backgrounds before detail.
- Animate with few, strong poses: contact and passing for walks, anticipation, strike
  and recovery for attacks. Keep the root fixed and move the body over it.
- Far limbs one shade darker than near limbs so the stride direction reads.
- Exact integer scales only: render at the target size and upscale by whole numbers with
  nearest sampling; never resample pixel art to a fractional size.

## Known limits

- Programmer-art ceiling: flat shading, plain proportions and repeating procedural
  texture. Style references and the user's approval matter more than more pixels.
- Large multi-frame ASCII specs get long; use layers, poses and frame overrides, run
  length segments above 64 px, and skeletal rigs for characters with many actions.
- Self-review sees still images only.
