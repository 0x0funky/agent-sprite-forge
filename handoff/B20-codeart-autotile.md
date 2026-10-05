# B20-codeart-autotile: seam-proven Wang-16, three-material (81), blob-47, bevel and flat tilesets from a material spec

Branch `asf/B20-codeart-autotile` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files: [autotile_build.py](../skills/codeart2d/scripts/autotile_build.py), [water-grass.material.json](../skills/codeart2d/examples/water-grass.material.json), [dirt-path.material.json](../skills/codeart2d/examples/dirt-path.material.json), [tiles-and-maps.md](../skills/codeart2d/references/tiles-and-maps.md), [test_codeart2d_tiles.py](../tests/test_codeart2d_tiles.py).

## Phase 3 integration status (branch asf/int-g-codeart)

Resolved in the codeart group fix pass (integration decisions cited as Dn):

- **D5, `qa` is a fileRef.** Each manifest's `qa` is `{path: "autotile-qa.json", sha256, bytes}`. The write order is: atlases and review images, then `autotile-qa.json` (its envelope's `outputs` are those images), then the manifests (each carrying the QA file's hash), then `codeart-meta.json`, which lists every file, manifests and QA file included. The QA envelope cannot list the manifests any more: a manifest holding the QA file's hash and a QA file holding the manifest's hash cannot both exist. Consumers that copy a manifest rewrite or drop `qa` (layout_build drops it and records the QA file in its provenance). With this, the relPath branch of `tileset_v1.qa` can be dropped from the schema (S1 follow-up).
- **D5, collision authority.** The per-tile `collision` and `properties.walkable` written here are the authoritative terrain collision of tile maps; layout_build (B21) now uses them instead of its vertex-square rule.
- **D26.** The `_Parser` override is gone: argument errors are argparse usage errors (`usage: autotile_build.py ...`, `error: ...`, exit 2). Runtime errors (a missing spec, a failed strict QC) stay exit 1.
- **D27.** `main()` runs through `forge_core.run_cli`: anything outside the narrow exception tuple is one `error: internal error (Type: message)` line, exit 1 (the codeart review's residual risk). `fuzz_b20.py` reports 0 problems.
- **D28.** The material spec is read through `forge_core.parse_json` (a UTF-8 BOM is accepted).
- **D29.** `TOOL_VERSION` is `forge_core.FORGE_PACKAGE_VERSION` (`0.4.0`): QA envelope `tool`, manifest `generator`, codeart-meta renderer.
- **D30.** `_file_ref` / `_input_ref` are `forge_core.file_ref`.
- **Schemas.** Section 5's requests are applied (S1, f3d7eb2); the tests validate against the real vendored schemas, and the in-memory `amended_validator` is gone.

## 1. CLIs

One verb (no subcommands). Run from the user's project root; outputs stay in the project:

    python "<skill-dir>/scripts/autotile_build.py" --material-spec "<skill-dir>/examples/water-grass.material.json" --kind both --tile-size 16 --variants 4 --output-dir out/tiles-v1 --preview-map 24x16 --max-repetition 0.35 --strict-qc
    python "<skill-dir>/scripts/autotile_build.py" --material-spec "<skill-dir>/examples/dirt-path.material.json" --output-dir out/tiles-v2 --strict-qc
    python "<skill-dir>/scripts/autotile_build.py" --material-spec my-tiles.material.json --output-dir out/tiles-draft --skip-seam-proof
    python "<skill-dir>/scripts/autotile_build.py" --material-spec my-tiles.material.json --material-texture grass=textures/grass16.png --quantize-textures --output-dir out/tiles-textured --strict-qc

Flags: `--material-spec` (codeart2d.material_spec.v1), `--output-dir` (must not exist), `--kind all|both|wang_corner|blob47|bevel|flat` (`wang`, `blob` aliases; `both` = wang_corner + blob47), `--tile-size 8-64`, `--variants 1-16`, `--seed` (these three override the spec), `--material-texture NAME=PATH` (repeatable; a one-tile image that wraps), `--quantize-textures`, `--preview-map WxH|none` (default 24x16), `--max-repetition R` (fail above R; default warn above 0.35), `--max-seam-ratio R` (default 1.0), `--skip-seam-proof`, `--strict-qc`.

- `--help` is ASCII and exits 0 under cp1252 and cp950 (`test_help_works_under_cp1252_and_cp950`). Usage errors are argparse's (`usage: ...`, exit 2, D26); runtime errors print `error: ...` and exit 1 (D27 catch-all through `forge_core.run_cli`).
- Success prints one ASCII JSON line: `status`, `output`, `metadata` (`<out>/codeart-meta.json`), `qa` (`<out>/autotile-qa.json`), `tilesets` (one `<set-id>.tileset.json` per set), `preview` (`<out>/preview-map.png` or null), `sets[]` (`id`, `kind`, `tiles`, `seamless_verified`, `pixels_compared`, `mismatches`, `repetition_index`), `repetition_index` (worst set) and `failed_checks`. Paths are POSIX, as given.
- Output folder: `<set-id>.png` (atlas), `<set-id>.tileset.json` (its `qa` is a fileRef of autotile-qa.json, D5), `review-<set-id>.png`, `preview-map.png`, `review.png`, `autotile-qa.json`, `codeart-meta.json`. Work happens in `forge_core.staged_output`; a failed check under `--strict-qc` raises before the stage exists, so nothing is published. An existing `--output-dir` is refused before any work.
- Timing on this machine (Windows 11, 4 BLAS threads): water-grass example 2.3 s; dirt-path example 9 s (the 81-tile set compares 80.8 million pixels).

## 2. SKILL.md routing rows

codeart2d `SKILL.md` (Z), tool table:

| Need | Route |
|---|---|
| Terrain transitions between two materials (shore, sand, snow) | `scripts/autotile_build.py` with a `wang_corner` set of 2 materials (Wang-16); see references/tiles-and-maps.md |
| Three materials meeting directly (a road reaching water) | `scripts/autotile_build.py` with a `wang_corner` set of 3 materials (81 tiles) |
| Paths, rivers or rugs drawn over ground | `scripts/autotile_build.py` with a `blob47` set `[under, fill]` (47 tiles, transparent overlay) |
| Walls, ledges, platform blocks | `scripts/autotile_build.py` with a `bevel` set (16 tiles) |
| Fill an image-tool texture into exact tile masks | `scripts/autotile_build.py --material-texture NAME=PATH --quantize-textures` (the image must be one tile and wrap) |

codeart2d `SKILL.md` text block, ready to paste:

```text
Tilesets: write a material spec (ramps, marks, edge and shadow bands) and run
autotile_build.py. Every set is seam-proven before it is published: tiles
assembled from the atlas are compared pixel for pixel with an independent
global render over every adjacency the set allows. Use --strict-qc; it never
passes without that proof. Code-drawn, no image model: say so.
```

generate2dmap `SKILL.md` (Z note from the plan), map routing table:

| Need | Route |
|---|---|
| Tile topology, autotiles, terrain transitions with collision from data | codeart2d `scripts/autotile_build.py` (seam-proven `generate2dmap.tileset.v1` manifests), then the map exporters (export_tiled / Godot / LDtk) |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `codeart2d/scripts/autotile_build.py` | Builds Wang-16, three-material (81), blob-47, bevel and flat tilesets from a material spec, with an exhaustive seam proof, a repetition index and `generate2dmap.tileset.v1` manifests (code-drawn) | `tests/test_codeart2d_tiles.py` (map equals the global render, 47 distinct blob tiles, 81 corner tiles, non-periodic control flagged, both examples pass `--strict-qc --max-repetition 0.35`) |

## 4. CHANGELOG entries

- Added: `codeart2d/scripts/autotile_build.py`, Wang-16 corner tilesets with interior-only variants, an exhaustive seam proof (map equals the global render over every 2x2 tile block) and `generate2dmap.tileset.v1` manifests with `seamless_verified` and `seam_proof` (B20-T1; roadmap 4.9, P1-2).
- Added: blob-47 overlays with 1 px edge bands and two-colour Tiled wang data, so the isolated tile is never an all-zero wangid (B20-T2; roadmap 4.9, 4.10).
- Added: three-material corner sets (81 tiles) so a road meets water directly, `--kind bevel` blocks, flat fill sets, `--material-texture` (an image-tool texture cut by the code masks; roadmap P2-2) and the repetition index (B20-T3; roadmap 4.9, P2-2).
- Added: `tiles-and-maps.md` and the `water-grass` and `dirt-path` material spec examples (B20-T4; roadmap P1-2).
- Changed: none (new tool).
- BREAKING: none.
- Fixed: the north-shore bank and shadow sampled above a tile and clamped at its top row in the 2026-10-05 map probe, leaving a 4 px colour jump between water tiles (roadmap 4.9). Corner weights now use a plateau kernel: look-ups up to `plateau` px across an edge are exact, and the seam proof fails a plain bilinear blend with the same shadow (`test_shore_shading_is_exact_with_the_plateau_and_the_probe_field_is_caught`).
- Fixed: two two-material sets forced a grass strip between a road and water, and once a dead-end road (roadmap 4.9, map probe); the three-material set removes the need.

## 5. Schema change requests

**Status: applied** by S1 (f3d7eb2), with `tileset_v1.qa` as `anyOf [fileRef, relPath]` per D5. Follow-up for the schema owner now that this module writes a fileRef: drop the relPath branch of `tileset_v1.qa` (S1's own note). The record of the requests follows.

None blocks this module: every document it writes validates against the frozen schemas (`test_outputs_validate_against_the_contracts`). The requests below document the optional fields the producer adds or reads, so they do not drift. They are applied in memory by `amended_validator()` in tests/test_codeart2d_tiles.py, and `test_requested_schema_additions_accept_the_outputs` checks every manifest and both example specs against them (and that a three-material blob47 set is refused). Producer: B20. Consumers: B13 export_tiled (TSX wangsets, per-tile collision), B14 Godot/LDtk exporters (terrain peering bits), B21 layout_build (tileset manifests on a vertex grid), Z docs.

5.1 `shared/schemas/map.schema.json`, merge into `/$defs/tile/properties`:

```json
"wangid": {"type": "array", "minItems": 8, "maxItems": 8, "items": {"type": "integer", "minimum": 0},
           "description": "Tiled wangid order top, top-right, right, bottom-right, bottom, bottom-left, left, top-left. Values are 1-based indices into the tileset's wangset.colors; 0 = position not used (corner sets use only corners, edge sets only edges, mixed sets all eight)."}
```

5.2 `shared/schemas/map.schema.json`, merge into `/$defs/tileset_v1/properties`:

```json
"id": {"type": "string", "minLength": 1},
"tilecount": {"type": "integer", "minimum": 1},
"variants": {"type": "integer", "minimum": 1},
"plateau": {"type": "integer", "minimum": 0, "description": "Wang sets: px around each tile edge that depend only on that edge's two corners."},
"wangset": {"type": "object", "required": ["type", "colors"], "properties": {
  "type": {"enum": ["corner", "edge", "mixed"]},
  "colors": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["name", "color"],
    "properties": {"name": {"type": "string", "minLength": 1}, "color": {"$ref": "common.schema.json#/$defs/hexColor"}}}}}},
"art_source": {"$ref": "common.schema.json#/$defs/artSource"},
"generator": {"$ref": "common.schema.json#/$defs/toolInfo"},
"spec_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
"qa": {"$ref": "common.schema.json#/$defs/relPath"}
```

5.3 `shared/schemas/codeart.schema.json`, `/$defs/material_spec_v1`:

- merge into `/properties`: `"collision_cell": {"type": "integer", "minimum": 1}`
- merge into `/properties/materials/additionalProperties/properties`:

```json
"walkable": {"type": "boolean"},
"edge": {"anyOf": [{"$ref": "#/$defs/materialBandRule"}, {"type": "array", "items": {"$ref": "#/$defs/materialBandRule"}}]},
"shadow": {"anyOf": [{"$ref": "#/$defs/materialBandRule"}, {"type": "array", "items": {"$ref": "#/$defs/materialBandRule"}}]},
"bevel": {"type": "object", "propertyNames": {"enum": ["top", "bottom", "left", "right"]},
          "additionalProperties": {"type": "array", "maxItems": 8, "items": {"type": "integer"}}}
```

- replace `/properties/materials/additionalProperties/properties/texture/anyOf/0` (`{"type": "object"}`) with:

```json
{"type": "object", "properties": {
  "base": {"type": "integer", "minimum": 0},
  "noise": {"type": "object", "required": ["levels"], "properties": {
    "frequency": {"type": "integer", "minimum": 1, "maximum": 8},
    "levels": {"type": "array", "minItems": 1, "items": {"type": "array", "minItems": 3, "maxItems": 3,
      "prefixItems": [{"type": "number", "minimum": 0}, {"type": "number", "minimum": 0}, {"type": "integer", "minimum": 0}]}}}},
  "marks": {"type": "array", "items": {"type": "object", "required": ["rows"], "properties": {
    "rows": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^[0-9. ]*$"}},
    "weight": {"type": "number", "exclusiveMinimum": 0}}}},
  "marks_per_tile": {"anyOf": [{"type": "integer", "minimum": 0},
    {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer", "minimum": 0}}]},
  "mark_margin": {"type": "integer", "minimum": 1},
  "image": {"$ref": "common.schema.json#/$defs/relPath"},
  "quantize": {"type": "boolean"}}}
```

- merge into `/properties/sets/items/properties`:

```json
"id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,47}$"},
"plateau": {"type": "integer", "minimum": 0},
"wobble": {"type": "number", "minimum": 0, "maximum": 0.9},
"margin": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer", "minimum": 1}}
```

- add to `/properties/sets/items` (the tool refuses these counts; the schema should too):

```json
"allOf": [
  {"if": {"properties": {"kind": {"const": "blob47"}}, "required": ["kind"]},
   "then": {"properties": {"materials": {"minItems": 2, "maxItems": 2}}}},
  {"if": {"properties": {"kind": {"enum": ["bevel", "flat"]}}, "required": ["kind"]},
   "then": {"properties": {"materials": {"maxItems": 1}}}}
]
```

- add `/$defs/materialBandRule` (the in-memory test inlines it as `RULE`):

```json
"materialBandRule": {"type": "object", "required": ["colors"], "properties": {
  "colors": {"type": "array", "minItems": 1, "maxItems": 8,
             "items": {"anyOf": [{"type": "integer", "minimum": 0}, {"$ref": "common.schema.json#/$defs/hexColor"}]}},
  "against": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
  "from": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}}}
```

Fields the producer adds to open objects, for the record: `autotile-qa.json` = the common qaEnvelope plus `metrics` (per set: kind, keys, tiles, variants, plateau, seam_proof cases and examples, seam_metric, nonperiodic_control, repetition with per-material horizontal/vertical/sample_range, colors); each check may carry a `note`. `codeart-meta.json` adds `params` (tile_size, variants, seed, kinds, collision_cell, quantize_textures) and `tilesets` (manifest file names) through `write_codeart_meta(extra=...)`; its `qa` is the envelope without `metrics`.

## 6. Shared-helper promotion requests

None required: the module has no `_local_*` stand-ins and uses forge_core (`utf8_stdio`, `ascii_text`, `sha256_bytes`, `sha256_file`, `write_json`, `staged_output`, `save_png`, `load_rgba`) and codeart_core (`qa_pixels`, `review_sheet`, `write_codeart_meta`) as frozen.

Optional candidates if other modules need them (no request filed):

- `collision_rects(blocked, cell)` (autotile_build.py:1400): greedy merge of a blocked grid into rectangles. B21 layout_build ("rect union equals the blocked set") and B13 map_nav merge blocked cells the same way; forge_core geometry would be the shared home.
- `PeriodicNoise` (autotile_build.py:135): sines with whole cycles per period, evaluated with exact integer phase reduction so a tile and a map agree bit for bit. B21 parallax_build's periodic layers could use it to make loop seams exact.

## 7. Cross-module links that Z must add

- codeart2d SKILL.md: route tilesets to `scripts/autotile_build.py` and link `references/tiles-and-maps.md`, `examples/water-grass.material.json`, `examples/dirt-path.material.json` (section 2 rows); disclose code-drawn art.
- generate2dmap SKILL.md: the section 2 row; generate2dmap references map-strategies.md should gain an "Autotile sets" paragraph pointing to codeart2d tiles-and-maps.md (roadmap P1-2 docs).
- B13 export_tiled: read `wangset` and per-tile `wangid` from tileset.v1 to write TSX wangsets (`corner` for wang_corner, `mixed` with two colours for blob47, `edge` for bevel), per-tile `collision` rects as objectgroups and `properties.walkable`; variants share a wangid, so give them equal `probability`. tiles-and-maps.md "Next steps" names these exporters in plain text; Z can turn that into links once B13/B14 land.
- B14 Godot/LDtk exporters: terrain peering bits from `wang` (corners) and `blob_mask` (bit i = N, NE, E, SE, S, SW, W, NW).
- B21 layout_build: Wang tile rank = `((tl * n + tr) * n + bl) * n + br` over material indices of the set (`materials` order); blob rank = position of the canonical mask in the manifest's sorted `blob_mask` values; tile index = variant * keys + rank. Its map_bundle.v2 `tilesets[].manifest` points at copies of these `*.tileset.json` files, and the placed tiles' `collision` is the bundle's terrain collision (D5, done).
- The integration e2e map pipeline (Appendix I step 4) starts with `autotile_build`; `--kind both` on water-grass.material.json is the fast choice (2.3 s).
- README tool table: section 3 row. CHANGELOG: section 4.
- `tests/test_skill_packages.py` keeps failing for codeart2d until Z adds `skills/codeart2d/SKILL.md` (unchanged by this module).

## 8. Known limitations and what is not proven

What the proof covers and what it does not:

- The exhaustive comparison checks that tiles assembled by key reproduce the global render, including look-ups across tile edges and noise at map coordinates. It does not judge the art; the global render shares the corner-weight formula, band rules and texture code with the tile renderer (recorded in `notProven`).
- Blob and bevel cells two away from a tile are tested all empty and all full, not enumerated (2^24 windows); by construction they cannot reach a tile (margins plus bands are at most half a tile), and the random map exercises mixed rings.
- Variants enter the exhaustive proof as rotations (every variant at every position of every case), not every combination of four variants per block (except flat sets, which enumerate all k^4 blocks).
- Image textures are sampled per tile in both renders, so the proof cannot see whether they wrap; `texture_wrap` (limit 1.25, from synthetic wrapping 0.95 vs non-wrapping 7.9) and the RGB seam metric measure it.

Deviations from the plan, with reasons:

- Non-periodic control. The roadmap's seam/interior flip ratio (prototype: 0.102 real vs 1.419 control) does not flag a non-periodic control on this construction: the plateau pins boundaries near edges, so a per-tile noise phase only moves crossing points by 1-2 px (measured ratios 0.43-0.91 for several controls, real sets 0.13-0.57). The control is therefore flagged by the exact comparison instead: noise is evaluated at map coordinates in the global render, and a copy detuned by half a cycle per tile fails it (8,500-14,000 mismatched pixels on the 30x20 map; `nonperiodic_control` check, `test_nonperiodic_control_is_flagged`). The seam metric is kept (mask ratio fails above 1.0, RGB ratio warns) and does flag a non-wrapping image texture (RGB 1.67).
- Repetition index: the roadmap's definition (13x13 single-material area, one-tile shift) is kept, but averaged over 8 seeded variant arrangements (one sample varies by about 0.1) and taken as the larger of the right and down shifts, worst material. Both examples measure 0.22-0.29, under the 0.35 target. Shared texture-noise bands (0.70) and image textures used alone (0.92) do not meet it; the doc says so. Z: claim the target only for sets that report it.
- Blob-47 is an overlay (transparent outside the fill, as in the design prototype); the `under` material only names the Tiled colour. Bevel uses 16 tiles (faces from the four sides; diagonal neighbours never show a face), not 47.
- Additions beyond the roadmap CLI: `--seed`, `--material-texture`, `--quantize-textures`, `--max-seam-ratio`, `--skip-seam-proof`, `--kind all|wang_corner|blob47|flat` and aliases.
- The preview map is a seeded sample for review (ground, a road of the third material, a blob path, two bevel blocks), not a layout tool (that is B21).

Other limits:

- Not opened in Tiled, Godot or LDtk: `wangid` follows Tiled's documented order; terrain-brush behaviour and Godot peering are unverified.
- Boundary shapes repeat every tile along long straight shores (variants vary only marks); a stronger wobble turns shores into zig-zags, so the default stays 0.6.
- `autotile-qa.json`'s envelope covers the atlases and review images, not the manifests (they carry its hash; D5); codeart-meta.json covers everything.
- Square tiles 8-64 px, one size per spec. The exhaustive proof is capped at 400 million pixel comparisons (`seam_proof` warns, or fails under `--strict-qc`, above it).
- Weights are float32 and noise uses `np.sin`; tile and global paths agree bit for bit on one machine (that is what the proof needs), but atlas bytes may differ across numpy builds or CPUs. Determinism is tested on one machine only (`test_outputs_are_deterministic`).
- Platforms: run only on Windows 11, Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1. Python 3.10, Pillow 10.1, Linux and macOS were not run; the code avoids newer APIs.
- The perf test (`test_three_material_exhaustive_proof_budget`, marker perf) took 3.6-4.8 s here against a 60 s budget.
- STD in this worktree: 711 passed, 2 skipped (674 base + 37 new); `tools/vendor_sync.py --check` exit 0.
