# PixelSpec v1: pixel art as text

PixelSpec is a JSON file that holds small pixel art as editable text: a palette of single
characters, rows of those characters, layers, poses, frames, palette variants and clips.
`scripts/render_pixelspec.py` turns it into checked 8-bit RGBA PNG frames with numpy and
Pillow only (no SVG rasterizer, no image model). codeart2d is the last resort: use it only when
the user asks for code-drawn art or no image route exists. It suits small pixel characters, props
and FX up to about 48 px of visible height; see [style-envelope.md](style-envelope.md).

The contract is `codeart2d.pixelspec.v1` in
[schemas/codeart.schema.json](schemas/codeart.schema.json) (`$defs/pixelspec_v1`). The
older id `codeart.pixelspec.v1` is still accepted; write the new one.

Working examples: [slime.pixelspec.json](../examples/slime.pixelspec.json) (32x32, 4 frames,
3 palette variants, one clip) and
[walker16x24.pixelspec.json](../examples/walker16x24.pixelspec.json) (16x24, walk in four
directions, layers, poses, mirrored legs, flipped side view, 3 variants, 4 clips).

## Hello sprite

```json
{
  "schema": "codeart2d.pixelspec.v1",
  "name": "gem",
  "canvas": [12, 12],
  "anchor_px": [6, 11],
  "palette": {"k": "#1a1c2c", "a": "#d04040", "h": "#ffb0a0"},
  "variants": {"ruby": {}, "sapphire": {"a": "#3050c0", "h": "#a0c0ff"}},
  "outline": {"mode": "solid", "color": "k"},
  "layers": [{"name": "body", "origin": [4, 6], "rows": [".aa.", "ahaa", "aaaa", ".aa."]}]
}
```

```text
python "<skill-dir>/scripts/render_pixelspec.py" --spec gem.pixelspec.json --output-dir out/gem-v1 --preview-scale 8 --strict-qc
```

This writes `out/gem-v1/ruby/frames/gem.png`, `out/gem-v1/sapphire/frames/gem.png`,
`out/gem-v1/preview-x8.png` and `out/gem-v1/codeart-meta.json`, then prints one JSON line
with the output and metadata paths. In Claude Code, `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.
Run commands from the project root and keep outputs inside the project, never inside the
skill folder (the tool refuses that).

## Fields

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | `codeart2d.pixelspec.v1` |
| `canvas` | yes | `[width, height]` in pixels; every frame uses this one canvas |
| `palette` | yes | `{char: colour}`; colours are `#rrggbb` or `#rrggbbaa` (`#rgb`, `#rgba` and a missing `#` are accepted). `.` and space are transparent and cannot be keys. A colour with alpha 00 erases the layers below it |
| `layers` | yes | painted in `z` order (lower first, ties keep list order); see below |
| `name` | no | asset name; also names the image when the spec has no frames |
| `anchor_px` | for clips | `[x, y]` canvas root shared by every frame, usually the bottom centre of the feet: the bottom edge of the lowest visible row (outline included) |
| `variants` | no | `{name: {char: colour}}`; each variant overrides some base colours (an empty object is the base palette under a name) |
| `poses` | no | `{name: grid}`; a grid is a list of rows, `{"rows": [...]}` or `{"segments": [...]}` |
| `frames` | no | list of frames; without frames the base layers render as one image |
| `outline` | no | `{"mode": "none" \| "solid" \| "selout", "color": char, "map": {fill char: outline char}}` |
| `clips` | no | `{name: clip}` using the sprite clip contract (`sprite.schema.json` `$defs/clipSpec`) |

Layer: `{"name", "z", "origin": [x, y], "rows": pose name or rows, "segments": [...],
"mirror", "mirror_safe", "hidden"}`. Frame: `{"name", "dx", "dy", "flip_x", "layers":
{layer name: {"rows" | "segments", "dx", "dy", "mirror", "hidden"}}}`. Booleans must be
`true` or `false`; numbers that place pixels must be integers.

### Rows and segments

Rows are strings, top to bottom, one character per pixel; shorter rows are padded with
`.` on the right. Segments are run-length rows for wide art (recommended above 64 px):
`"13.6l13."` is 13 `.`, 6 `l` and 13 `.`. A count is 1 or more; a character without a
count is one pixel. Digits are counts in segments, so digit palette keys only work in rows.

### Layers, poses and frames

- A layer's pixels replace what is below them. `origin` places its top-left corner.
- A layer takes its pixels from `rows` (literal rows, or the name of a pose) or `segments`.
  A frame can swap them (`"layers": {"legs": {"rows": "legs_step"}}`) and move the layer
  with `dx`/`dy`. Override only what changes: a frame that lists nothing renders the
  layer defaults.
- `mirror` flips one layer; `flip_x` on a frame mirrors every layer's placement across the
  canvas, so a left-facing frame is the right-facing one with `"flip_x": true`. A layer
  with `"mirror_safe": false` (an emblem, text) moves with the flip but keeps its own
  orientation.
- `hidden` skips a layer, for example an effect that has faded out.
- Every pixel must land inside the canvas; anything outside is an error, never a crop.

### Outline

The outline is drawn after compositing, on the 4-neighbour ring around every visible
pixel (holes included), so diagonals never thicken. `solid` paints the ring with `color`;
`selout` paints each ring pixel with the darkest outline colour that `map` assigns to the
fills it touches, falling back to `color`. The outline needs a 1 px margin: visible
pixels on the canvas border are an error.

### Variants

A variant overrides base colours only, so every variant has the same silhouette and the
same frames. `--variants all` renders each named variant (or the base palette when there
are none); `--variants base` renders the base palette; `--variants a,b` picks variants.

### Clips

Clips follow the sprite clip contract: `frames` are frame names or 0-based indices,
timing is `duration_ms` (one integer or one per frame) or `ticks` at `tick_hz` (default
60), looping is `loop` or `loop_policy`. Manifests are written as
`generate2dsprite.animation_clips.v2`, so events, ticks and the top-level `sampling: nearest`,
`pixel_art` and `art_source: code` reach the compiled clips. `--clips-schema v1` writes the v1
id for a build_animation_clips that predates the v2 reader; it refuses clips that use a v2
field (`ticks`, `loop_policy`, `events`, `keys`, `entry_frame`, `transitions`, `cadence_ms`,
`role`, ...).

## What render_pixelspec checks before rendering

1. The spec follows `pixelspec_v1`, read from the skill's own
   `references/schemas/codeart.schema.json` (no extra Python package needed). This
   refuses specs the renderer alone would accept: integer RGB palette entries, a pose
   that names another pose, a pose with both `rows` and `segments`, and non-boolean flags
   such as `"mirror": 1`. Each error names its JSON path, for example
   `$.layers[0].mirror: 1 is not of type 'boolean'`.
2. The references a schema cannot express: variant keys exist in the base palette; every
   character of every pose, layer and frame is in the palette (unused poses too); pose,
   layer, frame and clip names exist; layer and frame names are unique; every layer a
   frame renders has pixels; `duration_ms`/`ticks` lists match the clip length; event,
   key and entry positions fall inside the clip; transitions name existing clips.
3. The JSON itself: UTF-8, one object, no duplicate keys, no NaN or Infinity.

## Output folder

```text
out/slime-v1/
  codeart-meta.json            art_source "code", spec sha256, palette, outputs, QA envelope
  green/frames/rest.png        one PNG per distinct frame (8-bit RGBA, RGB zeroed under alpha 0)
  green/clips.json             --clips-manifest / --build-clips
  green/bundle/                --build-clips: build_animation_clips output for this variant
  blue/...  red/...
  preview-x6.png               --preview-scale 6: rows are variants, columns are distinct frames
```

- **Repeated poses are reused by index.** Frames that render the same pixels in every
  variant share one file; clips point at it by index (`"frames": [0, 1, 0, 2]`).
  `codeart-meta.json` maps every spec frame to its file (`"reuses": 0`).
- **Empty frames.** A frame with no visible pixel is not written. In a clip, empty frames
  at the end are dropped and their time is added to the last visible frame (a fading
  effect keeps its total length); events and keys past the new end move onto that frame.
  An empty frame earlier in a clip is an error, because build_animation_clips needs
  visible and transparent pixels in every frame. A frame that fills the whole canvas is
  refused for clips for the same reason.
- **QA.** Every written PNG is read back and measured: partial alpha 0, off-palette 0
  (against that variant's palette) and, with an outline, outline gaps 0 and at most 10
  L-corners per frame. The QA envelope in `codeart-meta.json` records the worst frame per
  check and per-frame numbers. With `--strict-qc` a failed check publishes nothing;
  without it the frames are published for inspection and the script still exits 1 after
  its summary line (`error: published with QA status fail: <check ids> ...`).
- **Publication.** Everything is written to a stage folder beside the output, checked,
  then published in one step. An existing output folder is refused; any failure,
  including a failed build, leaves nothing behind.
- **Determinism.** The same spec gives byte-identical frames, manifests, preview and
  `codeart-meta.json` (no timestamps).

## Handing frames on

- `--build-clips` runs `generate2dsprite/scripts/build_animation_clips.py` from the
  sibling generate2dsprite skill by path (`--clips-builder` points elsewhere). The bundle
  holds the frames, lossless WebP previews and `animation-clips.json` for engines.
- Never send code art through `generate2dsprite.py process`: its fit and preserve modes
  resample and re-anchor art that is already exact.
- Review at game size with `scripts/pixel_qa.py`:

```text
python "<skill-dir>/scripts/pixel_qa.py" --input "out/slime-v1/green/frames/*.png" --palette out/slime-v1/codeart-meta.json --variant green --outline "#1a1c2c" --anchor 16,31 --review out/slime-review.png --onion --strict --report out/slime-qa.json
```

## Authoring tips

- Size by visible height, not canvas: the walker is 23 px tall on a 16x24 canvas.
- Put the root at the bottom edge of the feet outline and keep it the same in every
  frame; walk bob moves the head and body layers, not the feet.
- Build animation from poses plus small frame overrides; a walk cycle needs only the
  contact and passing poses, and passing frames repeat (they are stored once).
- Give far limbs a clearly darker shade so the stride reads (the walker's far leg uses `P`); in side-view
  walk and run cycles the near and far legs need a clear value contrast and the arms or forelegs stay
  visible. A walk/run clip whose frame i and i+n/2 share a silhouette (IoU >= 0.95) gets a
  `half_cycle_duplicates` warning; the walker's passing poses repeat by design, so its clips pass
  `--allow-duplicate-half-cycle` after a look at the review sheet.
- Keep rows the same length inside a pose so the art stays aligned in diffs.
- Read the review sheet at 1x and 2x before adding detail; at 16 to 32 px every pixel
  is a decision.
