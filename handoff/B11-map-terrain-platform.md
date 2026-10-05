# B11-map-terrain-platform: terrain overlays, iso and hex tiles, Wang rows and seam-checked fills; platform kits with middle variants, surface QC and normalised seams

Branch `asf/B11-map-terrain-platform` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files:
[extract_terrain_tiles.py](../skills/generate2dmap/scripts/extract_terrain_tiles.py),
[extract_platform_strip.py](../skills/generate2dmap/scripts/extract_platform_strip.py),
[side-scroll-scenes.md](../skills/generate2dmap/references/side-scroll-scenes.md),
[tests/test_generate2dmap_terrain.py](../tests/test_generate2dmap_terrain.py) (63 tests) and
[tests/test_extract_platform_strip.py](../tests/test_extract_platform_strip.py) (53 tests, including the
improved fork's 16).

> **Integration (2026-10-05, group pass "map-core", branch `asf/int-g-map-core`).** Resolved: the seam metric is
> `forge_core.edge_seam_report`, shared with validate_parallax. Both `_local_edge_seam_report` copies (with
> `_premultiplied` and `_window_max`) are deleted, the verdicts are snake_case (`continuous`, `seam`, `duplicate_edge`,
> `flat`, `too_small`), and every gate fails only `seam` and `duplicate_edge` (D9). The platform tool no longer imports
> `remove_bg_magenta` (D10). The terrain sidecar rollback removes `--manifest` only while it is still the file this run
> published (st_dev/st_ino, review B11). The tests validate against the vendored schemas through `contract_errors`
> instead of the in-memory `proposed_map_errors` (D33). The schema requests of section 5 are in, applied by the shared
> stage (`edgeSeam` became the common def, D9). Also done: `forge_core.file_ref` (D30), BOM-tolerant JSON (D28),
> `run_cli` (D27) and `tool.version` `0.4.0` (D29). map-strategies.md now has a single-line terrain example, and
> side-scroll-scenes.md links `validate_layout.py` and engine-maps.md. Open items are marked **Z** (or name their owner).

## 1. CLIs

Commands run from the user's project root; `<skill-dir>` is `${CLAUDE_SKILL_DIR}` in Claude Code.

    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/terrain.png --output-dir art/terrain-v1 --rows 2 --cols 3 --terrain-row grass=0 --terrain-row dirt=1 --tile-size 64 --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/terrain.png --output-dir art/terrain-v2 --rows 2 --cols 3 --terrain-row grass=0 --terrain-row dirt=1 --tile-size 64 --edge-policy seamless --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/iso.png --output-dir art/iso-v1 --rows 1 --cols 4 --terrain-row grass=0 --shape iso-diamond --tile-width 128 --background-mode shape_fill --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/hex.png --output-dir art/hex-v1 --rows 1 --cols 4 --terrain-row sand=0 --shape hex-pointy --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/tufts.png --output-dir art/tufts-v1 --rows 1 --cols 4 --terrain-row tufts=0 --layer overlay --shape rect --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/shore.png --output-dir art/shore-v1 --rows 3 --cols 14 --terrain-row water=0 --terrain-row shore=1 --terrain-row grass=2 --wang shore=water/grass:0001,0010,0011,0100,0101,0110,0111,1000,1001,1010,1011,1100,1101,1110 --edge-policy seamless --strict-qc
    python "<skill-dir>/scripts/extract_terrain_tiles.py" --input art/terrain.png --output-dir art/terrain-legacy --rows 2 --cols 3 --terrain-row grass=0 --terrain-row dirt=1 --emit-runtime-defaults
    python "<skill-dir>/scripts/extract_platform_strip.py" --input art/platform.png --spec art/platform.json --output-dir art/platform-v1 --background-mode chroma_key --decoration-band-px 3 --max-seam-ratio 1.25 --strict-qc

- `--help` of both tools is ASCII and exits 0 under cp1252 and cp950 (`assert_cli_help` in both test files).
- Success prints one ASCII JSON line: `output` (the published directory), `manifest` (`terrain-bundle.json` or the
  `--manifest` path; `platform-strip.json`), `schema`, `status` (the QA envelope status) and `qc` (terrain: `passed`,
  `warnings`; platform: `passed`, `structural_passed`, `issues`, `warnings`).
- Runtime errors print `error: <message>` to stderr and exit 1 with nothing published (missing file, existing output,
  QC failure under `--strict-qc`, bad values). argparse usage errors (unknown flag, malformed `--wang`) keep argparse's
  exit 2 (D26); anything unexpected prints `error: internal error (<Type>: <message>)` and exits 1 (`forge_core.run_cli`,
  D27). The platform spec may carry a UTF-8 BOM (D28); QA envelopes carry `tool.version` `0.4.0` (D29).
- Both tools refuse an existing `--output-dir`, work in `forge_core.staged_output` and publish only after QC. The terrain
  `--manifest` outside the output directory is a sidecar published with `publish_file_no_replace` and removed again if
  the directory publish fails, but only while it is still the file this run published (same st_dev and st_ino).
- Seams (MAP-14, D9): every platform join, terrain wrap and Wang join is `forge_core.edge_seam_report` (common
  `edgeSeam`): the join step against the art's own steps near it, with `seam_ratio`, `near_median` and a verdict
  `continuous`, `seam`, `duplicate_edge`, `flat` (a plain colour: nothing to judge) or `too_small`. `--max-seam-ratio`
  gates fail `seam` and `duplicate_edge` only.

## 2. SKILL.md routing rows (**Z**)

generate2dmap, route table:

| Need | Route |
|---|---|
| Opaque terrain fills from an atlas (rows are terrains, columns variants) | `scripts/extract_terrain_tiles.py ... --strict-qc`; add `--edge-policy seamless` only for art meant to tile, which then must wrap |
| Iso-diamond or hex terrain tiles | `scripts/extract_terrain_tiles.py --shape iso-diamond`, `hex-pointy` or `hex-flat` with `--tile-width`/`--tile-height`; transparent corners (native_alpha) or `--background-mode shape_fill` for a flat corner colour |
| Transparent overlays (tufts, decals, transition overlays) | `scripts/extract_terrain_tiles.py --layer overlay` (native alpha or `--background-mode chroma_key`) |
| Wang corner transition rows | `scripts/extract_terrain_tiles.py --wang NAME=A/B:MASKS` (TL TR BL BR per column); name fill rows after the materials so coverage and joins include them |
| Platform caps and middle variants | read `references/side-scroll-scenes.md`, then `scripts/extract_platform_strip.py --strict-qc` |

generate2dmap, tools table (replaces the two current rows):

| Tool | Use |
|---|---|
| `scripts/extract_terrain_tiles.py` | Terrain fills, RGBA overlays, rect, iso-diamond and hex tiles, Wang rows; border-frame, footprint and (seamless policy) wrap and Wang seam QC; runtime numbers only when given |
| `scripts/extract_platform_strip.py` | Exact left cap, middle-variant and right cap crops; collision band, per-column surface and normalised seam QC; no bbox fitting |

Processing-notes bullets (generate2dmap):

- Terrain runtime and material numbers (`world_size`, `surface_y`, `roughness`, `emission_energy`, `engine_target`) are written only when given; `--emit-runtime-defaults` restores the v1 values and lists them in `runtime_defaults_applied`.
- `--edge-policy seamless` resizes fills as one period of an endless repeat and checks every wrap; image tiles never get `seamless_verified: true` (that needs an exact proof, as codeart2d autotiles have).
- A platform's declared surface row must be solid in every collision column, and the art may rise above it only by `--surface-tolerance-px` plus a declared `--decoration-band-px`.

## 3. README tool-table rows (**Z**)

| Tool | What it does | Verified by |
|---|---|---|
| `extract_terrain_tiles.py` | Slices a terrain atlas into fills, overlays, rect, iso-diamond or hex tiles and Wang transition rows with border, footprint and seam QC; no hidden runtime defaults | tests/test_generate2dmap_terrain.py: repro_10/10b (wrap resize within 1/255), repro_11, repro_12, repro_13 (100% of diamond pixels) |
| `extract_platform_strip.py` | Cuts exact platform caps and middle variants, measures the surface per column and every join with a normalised seam ratio | tests/test_extract_platform_strip.py: the fork's 16 tests, repro_14, repro_15, repro_16 |

## 4. CHANGELOG entries (**Z**)

- Added: terrain `--shape rect|iso-diamond|hex-pointy|hex-flat` with `--tile-width`/`--tile-height`, `--layer overlay` for RGBA overlays, `--background-mode auto|opaque|native_alpha|chroma_key|shape_fill` (`--fill-tolerance`, `--despill-radius`, `--threshold`, `--edge-threshold`), footprint coverage and spill QC (`--min-shape-coverage`, `--max-shape-spill`) (B11-T2; MAP-11).
- Added: terrain border-frame check for drawn grid lines and gutters along opposite tile edges (`--max-border-delta`, 1 disables) (B11-T2; MAP-10).
- Added: terrain `--wang NAME=A/B:MASKS`: Wang corner masks per transition row, `wang_coverage`, and under the seamless policy a seam check of every legal Wang join, including fills named after the materials (B11-T2).
- Added: terrain `--grid-rounding nearest` (rounded cell edges, per-cell `source_box`) and CJK terrain names kept as `display_name` with `terrain-<row>` file stems (B11-T2; MAP-04, MAP-23).
- Added: platform middle variants (1-16 interchangeable middles; every join measured; preview cycles them), per-column surface measurement (`--surface-tolerance-px`, `--decoration-band-px`), normalised seam ratio per join with an opt-in gate (`--max-seam-ratio`, which also flags duplicated edges), palette and grey inputs, and the improved fork's `--despill-radius 0..3` (B11-T1, B11-T3; MAP-07, MAP-14, MAP-19).
- Added: QA envelopes (method, notProven, checks, input and output sha256) in `terrain-bundle.json` and `platform-strip.json` (B11-T2, B11-T3).
- Added: side-scroll-scenes.md: stage plan template, prompt templates, platform QC flag table, render bands, camera notes, stage checklist and the layout validator pointer (B11-T4).
- Changed: `--edge-policy seamless` now changes processing: fills are Lanczos-resized wrap-aware (one period of an endless repeat) and every fill must wrap within `--max-seam-ratio` (default 1.25) without a duplicated edge. `isolated` keeps the old pixels exactly (B11-T2; MAP-15).
- Changed: both tools use the shared core: staged no-clobber publication (forge_core), the vectorised bit-identical legacy keyer and edge despill (forge_matte); the platform tool no longer loads extract_prop_pack.py through importlib (B11-T1; MAP-12, F-03).
- Changed: manifests are `generate2dmap.terrain_tile_bundle.v2` and `generate2dmap.platform_strip.v2`: manifest-relative paths (the platform tool stored absolute paths), `source` and `prompt` are fileRefs, the terrain `edge_policy` moved to `processing`; tiles and pieces are RGBA PNGs with RGB zeroed under alpha 0 (terrain tiles were RGB) (B11-T1, B11-T2).
- Changed (integration, D9): joins and wraps are measured by the shared `forge_core.edge_seam_report`, the same metric validate_parallax uses for repeat seams; verdicts are `continuous`, `seam`, `duplicate_edge`, `flat` and `too_small`, and gates fail only `seam` and `duplicate_edge` (a plain-colour join now reports `flat` and still passes).
- Changed (integration): QA `tool.version` is the package version `0.4.0` (D29); file references use `forge_core.file_ref` (D30).
- BREAKING: terrain runtime 3D fields are omitted unless given; legacy switch `--emit-runtime-defaults` (plan Appendix H, row "terrain").
- BREAKING (`--strict-qc` users; not yet in Appendix H): strict QC now also fails a terrain tile with a drawn frame or gutter along opposite edges (relax with `--max-border-delta`, 1 disables the check) and a platform piece whose art rises above the declared surface (allow it with `--surface-tolerance-px` and `--decoration-band-px`). Both used to pass (MAP-10, MAP-07).
- BREAKING: the terrain tool refuses an existing output directory (stale tiles were mixed into a new manifest); use a new folder, as for prop packs (Appendix H, prop-pack row).
- Fixed (integration, review B11): a failed terrain publish could delete a `--manifest` file that replaced the sidecar in the meantime; it now checks the file identity first.
- Fixed: DOC-14, MAP-16 (hidden runtime defaults), MAP-10 (gutters passed strict QC), MAP-11 (iso atlases rejected or cropped), MAP-15 (edge-clamped resize up to 19/255 off on periodic tiles), MAP-04 (no grid rounding), MAP-23 (CJK names rejected), DOC-10 (stale outputs), MAP-07 (art above the declared surface passed), MAP-14 (raw-equality seam metric failed true joins and passed duplicated edges), MAP-19 (palette PNGs rejected), MAP-12 (helper copies and importlib hack), F-03 (WSL publication), MAP-24 (absolute paths in the platform manifest).

## 5. Schema change requests

Resolved: `platform_strip_v2` and `terrain_tile_bundle_v2` are in the vendored map schema and the seam def is the common `edgeSeam` (`common.schema.json#/$defs/edgeSeam`, D9: one def for platform joins, terrain wraps, Wang joins and parallax repeat seams, with the snake_case verdicts `continuous`, `seam`, `duplicate_edge`, `flat` and `too_small`). The shared stage applied them and added the contract fixtures (`map.platform_strip_v2.*`, `map.terrain_tile_bundle_v2.*`, `common.edgeSeam.*`). Both test files validate every manifest they produce with `contract_errors(..., "map", "platform_strip_v2" | "terrain_tile_bundle_v2", skill="generate2dmap")`; the in-memory `PROPOSED_*_DEFS` and `proposed_map_errors` are gone (D33).

Optional fields the producers write beyond what the fragments require (all allowed, objects stay open):
terrain `processing.background_mode_requested`, `keyer`, `threshold`, `edge_threshold`, `despill_radius`,
`despill_changed_px`, `cell_shape`, `wrap_aware_resize`; `qc.min_contrast`, `min_variant_difference`,
`max_border_delta`, `min_shape_coverage`, `max_shape_spill`, `max_seam_ratio`; per terrain `wang_coverage`
(`masks`, `of`, `complete`, `missing_count`, `missing`), `wang_seams` (`legal_joins`, `failed[]`),
`cross_variant_seams` (`pairs`, `max_seam_ratio`, `not_continuous[]`, `note`); per variant `crop_box`,
`shape_fill` (`fill_rgb`, `corner_fill_share`, `keyed_px`), `border.max_delta`. Platform `processing.keyer`,
`threshold`, `edge_threshold`, `despill_changed_px`, `transparent_rgb`; `coordinate_contract`; per piece
`surface.reason` when not measured; per join `full_edge` and `contact_band` (the fork's metrics); `qc` thresholds and
`seam_metrics_note`; preview placements carry `id`.

## 6. Shared-helper promotion requests

- Resolved (D9, D30): `_local_edge_seam_report` (with `_premultiplied` and `_window_max`) is `forge_core.edge_seam_report`; `_local_file_ref` is `forge_core.file_ref(path, base, sha256=..., size=...)`.
- Open (not in D30's list, so both stay private in extract_terrain_tiles.py): `_local_wrap_resize(image, size) -> Image`, the Lanczos resize of a periodic opaque tile as one period of an endless repeat (proposed: a `wrap=True` option of `forge_core.resample_rgba`; tests `test_repro_10_wrap_aware_resize_matches_the_reference`, `test_repro_10b_bricks_wrap_within_one_level`), and `_local_shape_mask(shape, width, height, grow=0.0) -> ndarray[bool]` with `shape_polygon`, the pixel-centre raster of rect, iso-diamond and hex footprints that partitions the plane at `grow=0` (proposed: `forge_core.footprint_mask(polygon, width, height, grow=0.0)`, which iso and hex map exporters and codeart2d autotiles would share; tests `test_hex_footprints_partition_the_plane`, `test_iso_diamond_partitions_the_plane`).

## 7. Cross-module links

- skills/generate2dmap/SKILL.md (**Z**): the routing and tools rows of section 2 (the current tools rows still say "opaque
  square terrain variants" and "contact-band and repeat-preview checks").
- Done at integration (same group): map-strategies.md has a single-line terrain example and describes shapes,
  overlays, Wang rows and the seamless policy; side-scroll-scenes.md shows the `validate_layout.py` command and links
  engine-maps.md and map-presets.md; validate_parallax measures repeat seams with the same `edge_seam_report` (D9).
- export_tiled / export_godot (B13, B14) read `tileset_v1` (one tilesheet image). A terrain bundle with Wang rows has
  the corner masks (`wang`, `materials`) but one PNG per tile; a small converter (pack the tiles, write `tileset_v1`
  with `kind: wang_corner` and `seamless_verified: false`) would let image-model terrain reach those exporters.
- CHANGELOG (**Z**): section 4; README tool table: section 3.

## 8. Known limitations and what is not proven

- Seam ratio thresholds are synthetic. `seam_ratio = max(seam / local max step, worst 4-row window of the join /
  worst window of the local steps)`, with local steps within 8 columns of the join (now `forge_core.edge_seam_report`,
  unchanged in value; a join where neither the join nor the art near it has any step reports `flat`). On 400 synthetic trials
  (smooth periodic noise of several spectra, iid pixel noise) seamless joins reached at most 1.19; the 5th
  percentile of unrelated joins was 1.38 for smooth noise and 1.23 for iid noise offset by 25 levels. Sine and brick strips give 1.06 and 1.00; a duplicated
  column gives 0.05. Not calibrated on real image-model art; 1.25 is a default, not a proof. Each tile is also
  judged against its own sharpest nearby step, so a seam is invisible to the metric next to an equally sharp
  feature.
- `--edge-policy seamless` verifies each fill against itself and legal Wang joins; joins between different variants
  of a fill row are reported (`cross_variant_seams`) but not gated, so variants meant for random mixing need a
  look at that list. Iso, hex, overlay and crop-square tiles cannot use the seamless policy (no iso-periodic wrap).
- The border check uses luminance only: a frame of another hue at the same luminance, or a gradual vignette, is not
  found. Art that legitimately repeats a line on both opposite edges (bricks aligned to the tile grid, mortar on two
  edges) is reported as a frame; `--max-border-delta 1` turns the check off. Tiles smaller than 8 px are not judged.
- Iso-diamond and hex tiles are flat footprints inscribed in the tile; block tiles with visible sides spill outside
  the footprint and belong in `--layer overlay`. The pixel-centre footprint partitions the plane exactly for the
  2:1 diamond and the tested hex sizes (28x32 pointy, 32x28 flat); other hex sizes can have pixel centres exactly on
  an edge, which then count for both neighbours.
- `shape_fill` keys every pixel near the corner colour that touches the corners: art whose edge has that colour (a
  black outline on black fill) loses it. Use native alpha or a magenta key for such art.
- Platform surface QC reads alpha: `opaque` pieces cannot be measured and record `measured: false` (the fork test
  that runs opaque art with painted rows above the surface depends on that). The declared surface row must still be
  solid in every collision column, so a tolerance only allows art rising above it, never dipping below.
- MAP-19 names P, PA and LA input. PNG has no palette+alpha mode, so PA never comes from a PNG; P with tRNS, LA and L
  with a tRNS key are tested. repro_15's own file (`quantize()` then `transparency=0`) makes the brown platform index
  transparent, so it can only show acceptance; a palette whose transparent index is the background passes strict QC.
- Deviations from the improved fork's tests (three, marked "Port:" in their docstrings): the schema id is
  `generate2dmap.platform_strip.v2` (manifest paths, variants and QA changed meaning); the exact-bytes comparison zeroes
  RGB under alpha 0 first (plan Appendix D; the fork kept hidden RGB such as (199, 20, 80, 0)); the publish race is
  injected into `forge_core.publish_directory_no_replace`, which `staged_output` calls, instead of a private
  module-level copy. The adjusted race test also asserts that no stage directory is left behind.
- Deviation: `--despill-radius` with `native_alpha` or `opaque` is ignored and recorded as 0 (the fork's semantics),
  not an error.
- Deviation: RGBA terrain tiles (overlays, iso, hex) resize through `forge_core.resample_rgba` (premultiplied; box
  filter for reductions of 2x or more); opaque fills keep the old Pillow Lanczos in RGB, so `isolated` output pixels
  are unchanged. Terrain PNGs are now RGBA (were RGB) with identical colour values.
- The terrain tool keeps `--manifest` (legacy); a sidecar must be on the same drive as the output directory so tile
  paths stay relative.
- Not run: Linux, macOS, Python 3.10, Pillow 10.1 and numpy 1.26 floors (this machine: Windows 11, Python 3.13,
  Pillow 12.3, numpy 2.5, scipy 1.18; the module tests also pass with `FORGE_CORE_NO_SCIPY=1`). No engine import of
  iso, hex or Wang tiles was tried, and no real image-model atlas was used: every fixture is synthetic, mirroring the
  map-audit repros 10-16 and the study-asf-improved despill validation.
