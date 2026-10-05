# Prop kits and extraction

A prop pack batches compact static props into one generated sheet, then extracts each object into its own transparent PNG with a ground anchor. It trades per-prop control for fewer generation calls, so use it only when the props share style, camera, scale and quality bar. Choose the kit from the scene's gameplay roles and reuse each accepted prop across placements; never invent props to fill a sheet.

Run commands from the user's project root; `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs stay in the project. Success prints one line of ASCII JSON (output folder, manifest, counts, QA status, warnings); failures print `error: ...` on stderr and exit 1.

## Asset strategy gate

Classify every object before choosing a generation shape:

| Class | Examples | Route |
|---|---|---|
| `compact_prop` | rocks, shrubs, crates, lamps, signs, small trees, shrines, chests whose full silhouette and detail fit a cell with margins | square 2x2, 3x3 or 4x4 pack |
| `tall_or_large_object` | canopy trees, statues, towers, anything taller or wider than about 1.6:1 or visually dominant | one by one, custom cells, or a looser sheet with `--auto-boxes` |
| `wide_or_long_object` | fence rows, bridges, rails, long signs, logs wider than about 1.6:1 | one by one, wide custom cells or a strip |
| `structural_edge_object` | floors, platforms, doorways, gates, ledges that must meet walkable edges | [side-scroll-scenes.md](side-scroll-scenes.md) strips or tiles |
| `tileset_or_strip_piece` | caps, middles, corners, slopes, repeating ground | tiles or platform strips, never this extractor |

Classify the actual asset, not its name: a small contained tree is a compact prop, a wide canopy is not. A simple authored collision footprint does not disqualify a compact prop. Do not mix classes in one sheet. In a square grid the extractor flags any prop whose art is taller or wider than 1.6:1 (`strategy_aspect` warning in the manifest QA).

Also decide per prop whether actors can share its footprint: a `blocker` needs only depth sorting; walkable low dressing stays below the actor's body; walkable tall dressing needs an occupant policy (below).

Sheet size: 2x2 is the safest batch; 3x3 suits up to nine compact props with real detail budget; 4x4 only very simple props with strong margins. Leave intentional cells empty rather than inventing objects.

## Prompt templates

Attach the accepted style or scene master as a real reference image. Pick the camera first: top-down props may show a small top face; side-view props need the game's side elevation (see [side-scroll-scenes.md](side-scroll-scenes.md)). Ask for proportions, not pixel sizes: host image tools keep the aspect and area but not the size (a 1536 px square request came back as 1254 px, so a 4x4 grid does not divide evenly).

```text
Create one <ROWS>x<COLS> prop sheet for a <top-down RPG | side-view 2D> game, row-major:
1. <prop>  2. <prop>  ...
Same biome, palette, light, camera and scale for every prop; complete silhouettes.
Each prop sits fully inside the central 50-60% of its own cell, with empty background on all four sides.
No branch, roof, glow, smoke, shadow or fragment touches or crosses a cell edge.
Background: <real transparency | 100% flat #FF00FF magenta, no gradient, texture, shadow or floor>.
No text, labels, UI, watermark, grid lines, borders or guide marks.
```

Use verified native transparency where the tool supports it. Otherwise choose a flat key colour absent from the art: magenta keying neutralises purple edges. An RGB checkerboard is failed transparency; regenerate instead of keying it. A tall tree or post must finish inside its own row; size it to the cell instead of giving every prop the same height, or move it to a one-by-one generation.

## Extraction

```bash
python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/forest-props.png --rows 2 --cols 2 --labels shrub,lantern,crate,stump --output-dir assets/props --reject-edge-touch
python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/meadow-props.png --rows 4 --cols 4 --grid-rounding nearest --labels-file raw/meadow-labels.txt --output-dir assets/meadow-props
python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/forest-props.png --auto-boxes --labels tree,lantern,rock --output-dir assets/forest-props --suggest-footprint ellipse
```

- **Publication.** `--output-dir` must be new; an existing folder is refused, so old files can never mix with a new manifest. Work is staged beside it and published after QA; a failed run, including `--reject-edge-touch` and a run that accepts no prop (exit 1), leaves nothing behind. Outputs are `<output-dir>/<label>/prop.png` and `prop-pack.json`; `--manifest` may place the manifest elsewhere (a new file, removed again if publication fails).
- **Grid.** `--rows/--cols` must divide the sheet exactly by default. `--grid-rounding nearest` uses rounded cell edges instead (cells differ by at most 1 px; no pixel is dropped or resized), which accepts 1024 px 3x3 and 1254 px 4x4 sheets.
- **Labels.** Row-major, comma-separated or one per line in `--labels-file`; `empty`, `skip` or `-` skips a cell. Folder names are ASCII slugs; the label as written is kept as `display_name`. Labels without ASCII letters (for example `樹`) become `prop-<n>`, and a slug that dropped letters gets `-<n>`, so CJK labels stay distinct. Windows device names (`aux`, `con`, ...) are refused.
- **Background.** `auto` keeps real transparency and never keys it; otherwise it keys #FF00FF (`--threshold 100`, `--edge-threshold 150`). Chroma sheets then get edge despill: `--despill-radius 1` (default) removes magenta spill within 1 px of transparency, 2 or 3 reach further, 0 is the old output. On the meadow showcase cell, magenta-tinted pixels fall from 2419 to 111 (radius 1) and 36 (radius 2). Despill cannot tell real purple at an edge from spill; interior purple is untouched.
- **Alpha hygiene.** `--alpha-hygiene floor` zeroes alpha at or below `--alpha-floor` (default 4); `detached` drops faint islands with no solid pixel (alpha 32+) within 2 px; `both` does both. `--alpha-floor N` alone is the old floor option. The default is `none` (native alpha is preserved), except `both` with `--auto-boxes`. Use `both` on native-alpha model output: invisible haze otherwise inflates crops and trips edge checks. Never use a large floor to hide clipping or erase intended smoke or glow.
- **Components.** 8-connected, so 1 px diagonal strokes stay attached (`--connectivity 4` is the old rule). `largest` keeps one component, `all` keeps multipart props; both drop components below `--min-component-area` (default auto: 100 px for a 418 px cell, scaled with the cell area down to 1, so 16 px pixel props survive). Every item reports `dropped_components` and `dropped_area` (visible cell pixels missing from the image: trimmed, edge-cleaned or unselected); more than 1% is a QA warning and `--max-dropped-fraction` makes it fatal.
- **Edges.** Edge touch is judged on every visible cell pixel before trimming; `--reject-edge-touch` turns it into a failure. It cannot repair clipping or prove that objects are separate. `--trim-border` and `--edge-clean-depth` stay opt-in because they can cut tips and dark outlines; boxes are still reported in cell and sheet coordinates.
- **Canvas.** The image is the art plus `--component-padding` (default 8, clamped to the cell). `--keep-canvas` keeps the whole cell or box instead, for code art and exact boxes whose canvas and anchor are authored. `--keep-empty` writes a 1x1 transparent placeholder with status `placeholder` for empty cells.

## Off-grid sheets: auto boxes and measured boxes

A requested grid is not evidence of the returned cell boundaries. When objects are complete and separated but cross the nominal grid, do not resize the sheet or cut through objects:

1. Run `--auto-boxes`. Objects grow from their solid parts (alpha 128+); parts within `--auto-box-gap` px (default: short side / 52, 24 px on a 1254 px sheet) are one object, fainter pixels attach to the object they touch, and every pixel belongs to at most one prop even where boxes overlap. Props come in reading order; `--labels` must then name every object, and a mismatch lists the boxes found. The run writes `auto-boxes.json` and warns about visible pixels that belong to no box.
2. Review `auto-boxes.json`, fix or add boxes and per-prop fields, and rerun with `--boxes-file` to make the boxes explicit.

```bash
python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/forest-props.png --boxes-file raw/forest-boxes.json --component-mode all --alpha-hygiene both --output-dir assets/forest-props-reviewed --reject-edge-touch
```

```json
{"schema": "forge-crop-boxes/v1", "items": [
  {"id": "tree", "box": [77, 27, 613, 699]},
  {"id": "lantern", "box": [780, 135, 1091, 679], "anchor_px": [155, 536], "occlusion_class": "tall"},
  {"id": "rock", "box": [40, 843, 606, 1193], "solid": true, "footprint": {"shape": "ellipse", "width": 380, "depth": 150, "offset": [0, -75]}}
]}
```

Boxes are `[left, top, right, bottom]` in sheet pixels with exclusive right and bottom, integer, nonoverlapping and inside the sheet; they are measured examples for one 1254 px sheet, not a layout. Optional per-item fields: `display_name`, `anchor_px` (box pixels), `footprint`, `solid`, `contact`, `occlusion_class`, `occupant_policy`. The legacy `{"props": [{"label", "source_box"}]}` form is still read. If objects overlap or are clipped, regenerate them; recovered static boxes are not a successful animation grid.

## Manifest (generate2dmap.prop_pack.v2)

Every accepted item records:

- `label` (folder) and `display_name`; `image` (manifest-relative POSIX path) and its `sha256`; `status` `accepted` or `placeholder`.
- `cell_box`: the grid cell or box in sheet pixels; `source_rect`: the sheet rectangle the image covers (it may extend past the cell where padding is transparent); `trim_offset`: image origin relative to the cell; `padding`: `[left, top, right, bottom]` pixels actually applied around the art.
- `anchor_px`: the ground contact in image pixels, `anchor_source` `measured` or `authored`.
- `component_count`, `kept_area`, `dropped_components`, `dropped_area`, `edge_touch`, and for chroma sheets `edge_fringe_px` (tinted pixels within 2 px of transparency).
- Optional `footprint`, `solid`, `contact`, `occlusion_class`, `occupant_policy`, and `world_size`/`world_anchor` with `--world-scale`.

The manifest also keeps the settings (despill, hygiene report, geometry, grid rounding) and a QA envelope: `accepted`, `edge_touch`, `dropped_area`, `strategy_aspect`, `edge_fringe` (warn above 2% of edge pixels) and, with auto boxes, `auto_box_unowned`, plus what the checks do not prove and the sha256 of every input and output. Same inputs give the same bytes. The cfed170 v1 keys (`source_box`, `crop_bbox`, `padded_crop_bbox`, `output_size`, ...) are still written, in cell coordinates. `extract_prop_pack.read_manifest()` reads v1 manifests as v2-shaped documents with a bottom-centre anchor.

## Anchors, footprints and occlusion

`anchor_px` is a whole pixel on the art's ground line: the bottom edge of its lowest row with alpha above 16. The default `--anchor-mode stance` puts it at the middle of the bottom quarter of the art, which lands on a trunk, post or base; `feet` uses the median column of that band, `bbox` the box centre (`center` and `centroid` leave the ground line). A placement puts `anchor_px` on the world point (x, y), so art never floats above its ground point regardless of padding. Lying or leaning props (a fallen log) and props whose contact is not their lowest point need an authored `anchor_px` in a boxes file.

`--world-scale` (for example `0.5` or `3/8`) adds transparent padding so the anchor and the image size scale to whole pixels; an integer placement at that scale then has no rounding error (`world_size`, `world_anchor`). Use an exact fraction; a scale needing more than a 64 px padding step is refused.

`--suggest-footprint ellipse|rect` adds a ground footprint measured from the bottom quarter of the art: width is the run of columns at least half covered there (a trunk, not the canopy), depth is width x `--footprint-depth-ratio` (0.5), and the offset from `anchor_px` puts its front edge on the ground line. Footprints are in prop pixels and are scaled once by the instance scale; never inflate them again for actor size (map collision accounts for the actor). Suggestions are starting points: review them over the art and author the final footprint.

- `occlusion_class`: `low` (stays below the actor's body), `tall` (can hide an actor behind it; y-sort by the anchor), `foreground` (always drawn over actors).
- `occupant_policy`: `y_sort` (ordinary depth sorting), `rear_shift_and_fade` (when an actor occupies the prop's cell, shift the decoration rear-ward and fade it), `static_front`, `static_back` (fixed order). Render priority alone is not an occlusion policy.

## Pixel and code-art props

Prefer writing individual PNGs and metadata directly from codeart2d. When a sheet is unavoidable, keep the default automatic min area (1 px on small cells), use a small `--component-padding`, `--keep-canvas` with exact boxes and authored `anchor_px`, and an integer `--world-scale` for whole-pixel display. Native alpha is never keyed or despilled. Do not combine `--reject-edge-touch` with art that is flush with its box by construction.

## Placement and motion

Static complete props may be cropped with measured or authored anchors. Animated frames need a registered canvas and support point; independent per-frame trimming makes them bob. Use [$video2dsprite](../../video2dsprite/SKILL.md) for requested organic animation: tree roots and trunks stay planted while leaves move; buildings stay static while smoke, cloth or lights animate. Image rectangle, collider, interaction reach and render policy are separate data. Test over light and dark backgrounds with an actor beside, in front of and behind the prop; PNG alpha does not prove mobile video transparency.
