# Prop Pack Contract

Prop packs batch multiple small static map props into one generated sheet, then extract each cell into a transparent prop PNG. Square prop packs are for compact props only, not for floors, platforms, bridges, walls, side-scroller ledges, or other wide/collision-critical scene objects.

Use prop packs to reduce repeated image-generation calls and prompt overhead. They trade per-prop control for speed, so use them only when the props can share one style, scale, perspective, and quality bar.

## When To Use

Good candidates:

- rocks, shrubs, flowers, mushrooms, logs
- crates, barrels, sacks, pots
- small signs, lamps, lanterns, fences, posts
- floor ornaments, small statues, ruins, debris
- repeated environmental dressing for one biome

Avoid prop packs for:

- buildings, gates, trees with wide canopies, bridges
- floors, walkable platforms, terrain chunks, ledges, wall runs, rails, ladders, road segments, fence rows, long spike traps, pipes, conveyors, ramps, slopes, or any long horizontal object
- hero objects, key story artifacts, readable statues
- animated props or props with multiple states
- props requiring exact silhouette, scale, or identity
- props that are too wide/tall for equal square cells
- props that must line up exactly with collision, walkable edges, build pads, doorways, gate openings, checkpoints, hazards, exits, or engine editor handles

## Asset Strategy Gate

Classify every object before choosing a generation shape:

- `compact_prop`: small/medium, roughly square or vertical, decorative or simple blocker, no exact edge alignment requirement.
- `wide_or_long_object`: expected aspect ratio wider than about `1.6:1`, such as floors, platforms, bridges, ledges, wall runs, fence rows, long traps, rails, pipes, roads, conveyors, or long signs.
- `tall_or_large_object`: expected aspect ratio taller than about `1.6:1` or visually dominant, such as buildings, gates, large trees, towers, doors, banners, statues, or shrine pieces.
- `collision_bearing_object`: must align with collision, walkable edges, build pads, hazards, doors, gates, checkpoints, exits, or engine handles.
- `tileset_or_strip_piece`: should repeat or assemble from caps, middles, corners, slopes, tops, sides, or tile pieces.

Only `compact_prop` objects may use square `2x2`, `3x3`, or `4x4` prop packs. Everything else must use one-by-one generation, a strip/tileset workflow, custom wide cells, or engine-native tile/object layers.

Do not mix strategy classes in one sheet. A sheet of small rocks, crates, lamps, and grass is acceptable. A sheet that mixes rocks with platforms, floor pieces, gates, ladders, or spike hazards is not acceptable.

## Sheet Size Selection

- `2x2`: 4 props, safest batch size.
- `3x3`: 9 props, best default for compact small/medium environmental sets.
- `4x4`: 16 props, only for very simple small props with strong margins.
- `1x3 platform strip`: non-actor left cap, middle repeat, right cap for walkable floors/platforms.
- `1x4 platform strip`: non-actor left cap, middle repeat, right cap, plus one slope/corner/end variant. This is not an animation-frame format.
- `custom_wide_pack`: several related wide props using explicit wide cells such as `768x256`, `1024x384`, or another non-square cell size.

Use `3x3` by default only when the user asks for a set of compact map props and does not specify count.
Use one-by-one, platform strips, custom wide packs, or tile/object layers instead of a square pack for hero props, wide gates, buildings, wide trees, floors, platforms, bridges, walls, long hazards, ladders, or anything that must line up exactly with collision.

If a square pack fails because a wide/tall object touches the cell edge, do not pass it by relaxing QC and do not keep retrying the same square pack. Reclassify that object and regenerate it with one-by-one, a platform strip, a custom wide pack, or a tile/object-layer workflow.

For side-scroll platformers, make a shared stage object library rather than segment-specific prop packs:

- Generate platform strips, platform tiles, or terrain chunks once per stage style.
- Reuse that platform library across all camera-width segments.
- Put compact decorations, pickups, small terminals, small crates, lamps, and debris in square prop packs only after the structural platform library exists.
- Store platform placements and collision as explicit metadata or engine-native nodes; the prop image is only the visual skin.

## Prompt Pattern

For `3x3` and `4x4` packs, create a layout-only guide first with `$generate2dsprite`:

```bash
python skills/generate2dsprite/scripts/make_layout_guide.py \
  --rows <ROWS> \
  --cols <COLS> \
  --cell-width 384 \
  --cell-height 384 \
  --output assets/props/raw/<name>-layout-guide.png
```

Make the guide visible before image generation. Tell the model to use it only for invisible slot count, spacing, centering, and safe padding. The output must not copy guide boxes, safe-area rectangles, center marks, labels, borders, or guide background.

```text
Create exactly one <ROWS>x<COLS> prop sheet for a top-down 2D RPG map.
Each cell contains one separate static environmental prop from this list, in row-major order:
1. <prop>
2. <prop>
...
All props share the same biome, palette, camera angle, selected map art style, and scale.
Use clean hand-painted HD 2D game asset style by default: crisp silhouettes, smooth surfaces, low texture noise, controlled accent lighting. Do not make pixel art unless the user asked for it.
Mostly front-facing top-down RPG object view: upright objects are vertical and centered, with only a small visible top face. Avoid strong isometric diagonal rotation; crates and barrels should not become diamond-shaped or tilted unless the user explicitly asks for isometric art.
Full object visible, centered in its own cell, crisp but not chunky outlines.
Each prop must fit fully inside the central 50% to 60% of its cell with generous flat magenta gutters on all four sides.
No prop, branch, roof, sign, glow, cable, smoke, sparkle, shadow, or fragment may touch or cross a cell edge.
This square prop sheet must contain only compact props. Do not include floors, platforms, bridges, wall runs, ladders, long hazards, gates, doors, buildings, wide trees, roads, ramps, slopes, or any object that needs exact collision or walkable-edge alignment.
Background must be 100% solid flat #FF00FF magenta in every cell, no gradients, no texture, no shadows, no floor plane.
No text, labels, UI, watermark, numbers, arrows, borders, grid lines, or readable letters.
```

If a cell should stay empty, explicitly say `empty magenta cell`.

## Platform Strip Prompt Pattern

For repeatable floors, platforms, bridges, or terrain chunks, do not use a square `3x3` prop pack. Use a strip or tileset-like atlas with wide cells and a layout guide:

```text
Create exactly one 1x3 platform strip asset sheet for a 2D game map.
Cells, left to right:
1. left end cap of the platform
2. seamless middle repeat segment
3. right end cap of the platform

Each cell is a wide non-square cell, intended for platform/floor collision alignment.
Every segment must have a perfectly horizontal walkable top edge at the same y-position across all cells.
The middle segment must tile seamlessly left-to-right.
No segment may touch or cross its cell edge except intentional seamless side edges on the middle repeat cell.
Use solid flat #FF00FF magenta background, no floor plane, no shadows, no labels, no UI, no guide lines.
```

Use `1x4` only for non-actor platform strips when a slope, corner, broken variant, or underside piece is needed. If a platform is unique, large, or very important, generate it one-by-one on a wide canvas instead of using a strip. Never use this format for characters, enemies, creatures, NPCs, summons, or animated body assets.

## Extraction

Before extraction, run a chroma-key cleanup pass when the sheet has antialiased magenta fringe or when the props will be composited over a dark/detailed base. This is often better than direct hard-key extraction:

```bash
python $CODEX_HOME/skills/.system/imagegen/scripts/remove_chroma_key.py \
  --input assets/props/raw/forest-props-sheet.png \
  --out assets/props/raw/forest-props-sheet-alpha.png \
  --key-color '#ff00ff' \
  --soft-matte \
  --transparent-threshold 35 \
  --opaque-threshold 160 \
  --despill \
  --edge-contract 1 \
  --force
```

Use `scripts/extract_prop_pack.py`:

```bash
python skills/generate2dmap/scripts/extract_prop_pack.py \
  --input assets/props/raw/forest-props-sheet-alpha.png \
  --rows 3 \
  --cols 3 \
  --labels mossy-rock,shrub,fallen-log,small-lantern,wooden-sign,flower-patch,stump,crate,grass-tuft \
  --output-dir assets/props \
  --manifest assets/props/forest-prop-pack.json \
  --component-mode largest \
  --component-padding 8 \
  --min-component-area 200 \
  --reject-edge-touch
```

Output shape:

```text
assets/props/<label>/prop.png
assets/props/forest-prop-pack.json
```

The manifest contains source cell coordinates, crop boxes, alpha bounds, extracted image size, component counts, and `edge_touch` flags.

If the first pack fails because large props touch cell edges, regenerate with stricter occupancy wording such as `each prop must fit inside the central 50% of its cell`. Do not pass a failed pack by relaxing QC unless the clipped asset is intentionally discarded.

## Placement

After extraction, create placement JSON:

```json
{
  "props": [
    {
      "id": "mossy-rock-1",
      "image": "assets/props/mossy-rock/prop.png",
      "x": 420,
      "y": 512,
      "w": 96,
      "h": 72,
      "sortY": 512,
      "layer": "props"
    }
  ]
}
```

Then compose a QA preview with `scripts/compose_layered_preview.py`.

## QC Rules

Reject or regenerate the pack when:

- any accepted prop has `edge_touch: true`
- labels do not match the requested cells
- a prop has text, UI, shadows, or floor baked in
- prop identity drifts into character/NPC-like art
- a prop is too large for the intended placement scale
- a square pack contains a wide/long, tall/large, collision-bearing, platform, floor, bridge, wall, ladder, gate, door, or tileset/strip object

For noisy particles or edge debris, reprocess with `--component-mode largest`. For intentional multi-part props, use `--component-mode all` and increase the prompt margin.
