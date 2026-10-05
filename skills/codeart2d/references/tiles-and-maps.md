# Tiles and maps: autotile_build

`scripts/autotile_build.py` turns a material spec (JSON text you write) into seam-proven tilesets: Wang-16 corner sets, three-material corner sets (81 tiles, so a road can meet water), blob-47 overlays, bevelled blocks and plain fills. The art is code-drawn, no image model; say so when you hand it over (`art_source: "code"` is written into every manifest and into `codeart-meta.json`).

Use it for tile topology and terrain transitions in maps: shores, paths, cliffs and blocks whose seams must be exact and whose collision comes from data. Use an image route for painterly or richly textured ground; an image texture can still be cut by these masks (`--material-texture`, below).

## Quick start

Commands are single lines run from your project root. `<skill-dir>` is the codeart2d skill folder (in Claude Code, `${CLAUDE_SKILL_DIR}`). Outputs go into your project, never into the skill folder.

Build the shore and path example exactly as the roadmap gate runs it:

    python "<skill-dir>/scripts/autotile_build.py" --material-spec "<skill-dir>/examples/water-grass.material.json" --kind both --tile-size 16 --variants 4 --output-dir out/tiles-v1 --preview-map 24x16 --max-repetition 0.35 --strict-qc

Build every set of the three-material example (water, grass and dirt corners; a dirt path overlay; stone blocks; plain grass):

    python "<skill-dir>/scripts/autotile_build.py" --material-spec "<skill-dir>/examples/dirt-path.material.json" --output-dir out/tiles-v2 --strict-qc

Iterate on art quickly, then run the full proof before shipping (a draft is published with `seamless_verified: false`):

    python "<skill-dir>/scripts/autotile_build.py" --material-spec my-tiles.material.json --output-dir out/tiles-draft --skip-seam-proof

Fill a material with a one-tile image that wraps seamlessly (for example a texture from an image tool), snapped to the material's ramp:

    python "<skill-dir>/scripts/autotile_build.py" --material-spec my-tiles.material.json --material-texture grass=textures/grass16.png --quantize-textures --output-dir out/tiles-textured --strict-qc

Success prints one ASCII line of JSON: `status`, `output`, `metadata` (codeart-meta.json), `qa` (autotile-qa.json), `tilesets` (one manifest per set), `preview`, per-set `seamless_verified`, `pixels_compared`, `mismatches` and `repetition_index`, the overall `repetition_index` and `failed_checks`. Errors print `error: ...` on stderr and exit 1; a wrong argument is a usage error (argparse's `usage: ...` and exit 2). The material spec may be saved with a UTF-8 BOM. `--output-dir` must not exist; nothing is written until every check has run, and with `--strict-qc` a failed check publishes nothing.

## Choosing a kind

| Need | Set in the spec | Tiles per variant | Layer |
|---|---|---|---|
| Two terrains meeting (shore, sand, snow line) | `{"kind": "wang_corner", "materials": ["water", "grass"]}` | 16 | ground |
| Three terrains meeting directly (a road that reaches the water) | `{"kind": "wang_corner", "materials": ["water", "grass", "dirt"]}` | 81 | ground |
| Paths, rivers or rugs drawn over the ground | `{"kind": "blob47", "materials": ["grass", "dirt"]}` (under, fill) | 47 | overlay, transparent outside the fill |
| Walls, ledges, platforms, stone blocks | `{"kind": "bevel", "materials": ["stone"]}` | 16 | overlay, whole tiles |
| A plain fill with variety | `{"kind": "flat", "materials": ["grass"]}` | 1 | ground |

`--kind` picks which sets of the spec to build: `all` (default), `both` (wang_corner and blob47), `wang_corner`, `blob47`, `bevel` or `flat` (`wang` and `blob` are aliases).

A Wang map is a vertex grid: each grid point holds a material and each tile is chosen by its four corners. A blob or bevel map is a cell grid: each occupied cell is chosen by its neighbours. Two two-material sets cannot put a road directly against water (the generator has to leave a strip of grass between them); use one three-material set instead.

## The material spec

`codeart2d.material_spec.v1` (schema in `references/schemas/codeart.schema.json`):

```json
{
  "schema": "codeart2d.material_spec.v1",
  "tile_size": 16,
  "variants": 4,
  "seed": 7,
  "materials": {
    "water": {
      "ramp": ["#1d3d73", "#2c62ab", "#4b98dc", "#a5dcf5"],
      "walkable": false,
      "texture": {"base": 0, "marks": [{"rows": ["222"]}, {"rows": ["3"]}], "marks_per_tile": [2, 3]},
      "edge": {"colors": [3, 2], "against": ["grass"]},
      "shadow": {"colors": ["#43291a", "#6a4a2c", 1], "from": ["grass"]}
    },
    "grass": {
      "ramp": ["#23502f", "#377a3b", "#5aa344", "#8cc657"],
      "texture": {"base": 2, "marks": [{"rows": ["1.1", ".1."]}, {"rows": ["3", "1"]}], "marks_per_tile": [3, 5]},
      "edge": {"colors": [0], "against": ["water"]}
    }
  },
  "sets": [{"kind": "wang_corner", "materials": ["water", "grass"]}]
}
```

Top level:

| Field | Meaning |
|---|---|
| `tile_size` | Square tile size in px, 8-64 (`--tile-size` overrides it). |
| `variants` | Interior-only variants per tile, 1-16, default 4 (`--variants` overrides it). |
| `seed` | Fixes every random choice; the same spec and seed give the same bytes (`--seed` overrides it). |
| `collision_cell` | Optional; collision resolution in px, default a quarter tile. |

Per material:

| Field | Meaning |
|---|---|
| `ramp` | 1-16 opaque `#rrggbb` colours, dark to light. Every colour reference below is a ramp position (0 = darkest) or a literal `#rrggbb`. |
| `walkable` | Default true. Non-walkable pixels become per-tile collision rectangles; a tile is `walkable` when less than half of it is blocked. |
| `texture.base` | The flat fill (ramp position), default the middle of the ramp. |
| `texture.marks` | Small stamps: `rows` of digits (ramp positions) and `.` (untouched), optional `weight`. Each variant places its own non-overlapping marks, only where the whole mark sits inside the material, off every band, at least `mark_margin` px (default 1) from the tile edge. Marks are what make variants differ. |
| `texture.marks_per_tile` | A count or `[min, max]`, default `[2, 4]` when marks are given. |
| `texture.noise` | Optional `{"frequency": 2, "levels": [[low, high, ramp position], ...]}`: patches shared by every tile. They wrap exactly but repeat every tile, so they raise the repetition index; prefer marks. |
| `texture.image` or `texture: "file.png"` | A one-tile image (path relative to the spec) used as the fill; `quantize: true` snaps it to the ramp. |
| `edge` | One rule or a list: `colors` nearest first, painted on this material's pixels within that many pixels (4-neighbour distance) of a material in `against` (default: any other material of the set). Foam lines, rims, outlines. Later rules win. |
| `shadow` | One rule or a list: `colors` nearest first, painted on this material's pixels whose nearest `from` pixel straight above is that many pixels away. The north-shore bank and its shadow. Shadows win over edges. |
| `bevel` | For bevel sets: `{"top": [2, 1], "left": [-1], "right": [-1], "bottom": [-2]}`, ramp steps per pixel from an open side, nearest first (positive is lighter). |

Per set: `kind`, `materials`, optional `id` (default for example `water-grass-wang16`), and:

| Field | Kinds | Meaning |
|---|---|---|
| `plateau` | wang_corner | Px around each tile edge that depend only on that edge's two corners. Default max(longest band, tile/8); must be at least the longest `edge` or `shadow` colour list, and at most (tile - 2) / 2. |
| `wobble` | wang_corner | Boundary irregularity 0-0.9, default 0.6. Higher values turn shorelines into zig-zags. |
| `margin` | blob47 | `[min, max]` depth in px of the fill's edge where a neighbour is missing; min must be at least the fill's longest band. |

## How the seams stay exact

- Wang sets: each vertex weighs the pixels around it with a kernel that is exactly 1 within `plateau` px and exactly 0 beyond `tile - plateau` px. Everything within `plateau` px of an edge therefore depends only on that edge's two corners, so a tile can compute foam, rims, banks and shadows that reach across its edge from its own corners. The 2026-10-05 map probe sampled above the tile and clamped at its top row instead, which left a 4 px colour jump between water tiles; with the plateau kernel that look-up is exact, and the seam proof shows that a plain bilinear blend with the same 3 px shadow fails.
- Blob sets: the fill keeps a tile-periodic margin where a side neighbour is missing; an inner-corner notch is the intersection of the two neighbouring margin bands, so its edges line up with the margins the neighbours draw. Edge bands read a ring built from the neighbours' own rules.
- Bevel sets: faces come from the four side neighbours only, so solid-to-solid seams carry no face.
- All procedural noise (boundary wobble, texture patches, blob margins) is a sum of sines with whole cycles per tile, evaluated at map coordinates with exact integer phase arithmetic.
- Variants change only tile interiors: marks never touch the 1 px outer ring.

## QA and strict mode

`autotile-qa.json` is a QA envelope (`status`, `method`, `notProven`, `checks`, `inputs`, `outputs` with sha256, `tool` with the package version) plus detailed `metrics`; the same envelope is the `qa` of `codeart-meta.json`. Its `outputs` are the atlases and the review images it judged; each manifest then points back at it with a fileRef (`qa: {path, sha256, bytes}`), and `codeart-meta.json` lists every file, manifests and QA file included. Checks per set:

| Check | Fails when |
|---|---|
| `seam_proof` | Any pixel of the exhaustive comparison differs. Wang: every 2x2 block of tiles (all 3x3 corner lattices: 512 for two materials, 19,683 for three), in variant rotations. Blob and bevel: every 3x3 neighbourhood and every horizontal and vertical pair with its neighbourhood, with the cells two away all empty and all full. Flat: every 2x2 block of variants. Tiles are assembled from the atlas by key and compared with an independent global render that evaluates noise at map coordinates and reads look-ups at their true positions. Skipped proof: `warn`, or `fail` under `--strict-qc` (strict mode never passes without the proof). |
| `random_map` | A seeded random 30x20 map assembled from the tiles differs from the global render. |
| `nonperiodic_control` | A copy of the set whose noise is detuned by half a cycle per tile reproduces its global render (the proof would be blind). `skipped` when the set has no noise (flat fills, fixed margins). |
| `seam_metric_mask` | Material flips across tile seams exceed flips inside tiles (`--max-seam-ratio`, default 1.0). |
| `seam_metric_rgb` | Colour steps across seams exceed steps inside tiles: `warn` (an image texture that does not wrap shows up here); bevel sets are measured inside a solid area. |
| `repetition_index` | Above `--max-repetition` when given (`fail`); otherwise `warn` above 0.35. |
| `outer_rings_identical` | A variant differs from variant 0 on the 1 px outer ring. |
| `distinct_tiles` | Two topology keys render identically (`fail` for blob and bevel, `warn` for Wang). |
| `partial_alpha`, `off_palette` | Any semi-transparent pixel; any colour outside the ramps and rule colours (`skipped` for image textures used as given). |
| `texture_wrap/<material>` | An image texture's steps across its wrap edges exceed 1.25 times the 95th percentile of its inside steps. |

Without `--strict-qc` the result is published with its QA status (`fail` checks included) and manifests say `seamless_verified: false` unless the proof ran and found no mismatch.

## Outputs

| File | Content |
|---|---|
| `<set-id>.png` | The atlas: 8-bit RGBA, transparent RGB zeroed. Tile index = variant x keys + key rank, row-major, no spacing. Columns: 16 (Wang-16), 9 (81-tile), 8 (blob-47), 4 (bevel). |
| `<set-id>.tileset.json` | `generate2dmap.tileset.v1`: `image`, `sha256`, `tile_size`, `columns`, `kind`, `materials`, `tiles`, `seamless_verified`, `seam_proof {method, pixels_compared, mismatches}`, `repetition_index`, plus `id`, `tilecount`, `variants`, `wangset`, `plateau` (Wang), `art_source`, `generator`, `spec_sha256` and `qa` (a fileRef of `autotile-qa.json`: a tool that copies the manifest elsewhere rewrites or drops it). |
| `review-<set-id>.png` | The atlas at x2 (and x4 when small) on a checkerboard with the palette and QA summary. |
| `preview-map.png`, `review.png` | A seeded sample map assembled from the tiles: the first Wang (or flat) set as ground (with a road of its third material), the first blob set as a path, the first bevel set as blocks; `review.png` shows it at x1 and x2. |
| `autotile-qa.json`, `codeart-meta.json` | QA envelope with metrics; code-art metadata (`art_source: "code"`, disclosure, palette, outputs with sha256). |

Per tile in the manifest:

- Wang: `wang` = corner material indices `[top_left, top_right, bottom_left, bottom_right]` (indices into `materials`).
- Blob and bevel: `blob_mask` sets bit i for neighbour i of N, NE, E, SE, S, SW, W, NW (blob diagonals count only when both neighbouring sides are set; bevel uses the four sides only).
- `wangid`: Tiled order top, top-right, right, bottom-right, bottom, bottom-left, left, top-left, with colour ids from `wangset.colors` (1-based). Corner sets use the corner positions; blob sets are mixed sets with two colours (1 = under, 2 = fill, never 0, so the isolated tile is not mistaken for an unassigned one); bevel sets are edge sets (1 = open, 2 = solid).
- `variant`, `collision` (rectangles in tile pixels), `properties.walkable`. For tile maps this per-tile collision is the authoritative terrain collision: every reader of a map bundle (generate2dmap map_nav.py and exporters, codeart2d layout_build, the scene runtime) moves these shapes onto each placed tile, so a Tiled export and the bundle agree near shores.

## Repetition

The repetition index is the luminance correlation of a 13x13 single-material area with itself shifted one tile right and one tile down (the larger of the two, the worst material of the set), averaged over 8 arrangements with uniformly picked variants. 1 means wallpaper; a flat colour scores 0. With four variants that differ by marks the sets of both examples measure 0.22-0.29 (target 0.35). One variant scores about 1; shared noise patches and image textures repeat every tile and score high (an image texture used alone measured 0.92). To lower it: more variants, more and varied marks, no shared noise bands.

## Limits

- Not opened in Tiled, Godot or LDtk: `wangid` follows Tiled's documented order, but terrain-brush behaviour is not verified.
- Boundary shapes repeat every tile along long straight shores; variants vary marks only.
- Square tiles, 8-64 px, one tile size per spec. The exhaustive proof is capped at 400 million pixel comparisons (the 81-tile set at 16 px with 4 variants compares 80.6 million in a few seconds).
- Blob and bevel cells two away from a tile are tested all empty and all full, not enumerated.
- Image textures are cut per tile in both renders, so the proof cannot tell whether they wrap; `texture_wrap` and the seam metric measure it.
- Collision rectangles approximate blocked pixels at `collision_cell` resolution.

## Next steps

Feed the manifests to the map tools: a code-made layout ([layout_build.py](layouts-and-parallax.md)) places these tiles on a vertex grid and writes map_bundle.v2 whose terrain collision is these tiles' own collision, then the generate2dmap exporters write Tiled, Godot or LDtk files.
