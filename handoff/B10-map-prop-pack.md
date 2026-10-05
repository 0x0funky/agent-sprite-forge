# B10-map-prop-pack: extract_prop_pack v2 (despill port, staged publish, anchors, 8-connectivity, rounding, keep-canvas, auto-boxes, footprints, hygiene, strategy gate)

Branch `asf/B10-map-prop-pack` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files: [extract_prop_pack.py](../skills/generate2dmap/scripts/extract_prop_pack.py), [prop-pack-contract.md](../skills/generate2dmap/references/prop-pack-contract.md), [tests/test_extract_prop_pack.py](../tests/test_extract_prop_pack.py) (71 tests).

The tool keys and labels with the shared libraries: `forge_matte.legacy_hard_key` (bit-exact cfed170 keyer), `forge_matte.despill` (edge mode, margin 12, bit-exact with the fork's port for radius 0-3), `forge_core.label_components`, `alpha_hygiene`, `anchor_from_mask`, `rounded_grid_boxes`, `load_rgba`, `save_png`, `staged_output`, `publish_file_no_replace`, `write_json`, `sha256_file`, `utf8_stdio`, `ascii_text`. No pure-Python pixel loop is left.

## 1. CLIs

Run from the user's project root; `<skill-dir>` is `skills/generate2dmap` (`${CLAUDE_SKILL_DIR}` in Claude Code).

    python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --rows 2 --cols 2 --labels shrub,lantern,crate,stump --output-dir assets/props --reject-edge-touch
    python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --rows 4 --cols 4 --grid-rounding nearest --output-dir assets/props
    python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --auto-boxes --labels tree,lantern,rock --output-dir assets/props --world-scale 3/8 --suggest-footprint ellipse
    python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --boxes-file raw/boxes.json --component-mode all --keep-canvas --output-dir assets/props

- One command, no subcommands. Layout is one of `--rows/--cols` (`--grid-rounding exact|nearest`), `--boxes-file` (unified `forge-crop-boxes/v1` items or the legacy `props` list) or `--auto-boxes` (`--auto-box-gap`).
- New flags: `--despill-radius 0..3` (default 1, chroma only), `--alpha-hygiene none|floor|detached|both` (`--alpha-floor N` alone means `floor` at N), `--connectivity 4|8` (default 8), `--min-component-area N|auto` (default auto), `--keep-canvas`, `--anchor-mode stance|feet|bbox|center|centroid` (default stance), `--world-scale 0.5|3/8`, `--suggest-footprint ellipse|rect`, `--footprint-depth-ratio` (0.5), `--max-dropped-fraction`, `--art-source`. Kept: `--threshold`, `--edge-threshold`, `--trim-border`, `--edge-clean-depth`, `--component-mode`, `--component-padding`, `--edge-touch-margin`, `--reject-edge-touch`, `--keep-empty`, `--labels`, `--labels-file`, `--manifest`.
- `--help` is ASCII and exits 0 under cp1252 and cp950 (tested); its epilog has two single-line examples.
- Success prints one ASCII JSON line: `status`, `output_dir`, `manifest` (absolute paths), `accepted`, `placeholders`, `rejected` (counts), `qa` (pass or warn) and `warnings`. Every failure, including argparse usage errors, is one `error: ...` line on stderr with exit 1, and publishes nothing.
- Library entry points in the same skill: `extract(args) -> manifest`, `read_manifest(path)` (v1 and v2), `key_sheet(pixels, mode, threshold, edge_threshold, despill_radius)`, `read_crop_boxes(path, size)`, `find_auto_boxes(...)`, `grid_boxes(...)`. `remove_bg_magenta(img, threshold, edge_threshold)` stays only for extract_platform_strip (section 7).

## 2. SKILL.md routing rows

generate2dmap, routing table:

| Need | Route |
|---|---|
| Compact props from a square prop sheet | `scripts/extract_prop_pack.py --rows R --cols C --labels ... --output-dir <new>`; add `--grid-rounding nearest` when the returned size does not divide; then place props by `anchor_px` |
| Props on a loose or off-grid sheet | `scripts/extract_prop_pack.py --auto-boxes --labels ...`; review `auto-boxes.json`, then rerun with `--boxes-file` |
| Code-art or exact-box props with authored anchors | `scripts/extract_prop_pack.py --boxes-file boxes.json --keep-canvas` (`anchor_px` per item) |
| Props at a fixed display scale without anchor drift | add `--world-scale 3/8` (any exact fraction); use `world_size` and `world_anchor` |
| Ground footprints for map collision | add `--suggest-footprint ellipse`, review over the art, author the final footprint in the boxes file |

generate2dmap, processing tools table (replaces the extract_prop_pack row):

| Script | Purpose / limits |
|---|---|
| `scripts/extract_prop_pack.py` | Exact or rounded grid, measured or auto boxes; native alpha or magenta with edge despill; 8-connected components with dropped-pixel reporting; ground anchors, footprint suggestions, world-scale padding; prop_pack.v2 manifest with QA; refuses an existing output folder; not structural strips |

Processing notes bullets: "Prop sheets: new output folder only; a run that accepts nothing exits 1. Chroma props get radius 1 edge despill (`--despill-radius 0` for the old output). Use `--alpha-hygiene both` on native-alpha model output."

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `extract_prop_pack.py` | Splits a prop sheet into anchored transparent props with a prop_pack.v2 manifest: rounded grids, auto boxes, anchors, footprints, despill | `tests/test_extract_prop_pack.py`: fork despill parity (r 0-3), cfed170 parity under the legacy switches, map-audit repro_04/05/09/18/19/20/21/22, forest replica, meadow real fixture, CLI subprocess |

## 4. CHANGELOG entries

- Added: `--grid-rounding nearest` (rounded cell edges, MAP-04), `--auto-boxes` with `auto-boxes.json` (report v2 P1-6), `--keep-canvas` (probe), `--world-scale` with integer-exact padding (anchor error 0.000 px), `--suggest-footprint`, `--anchor-mode`, `--alpha-hygiene` (`--alpha-floor` kept as its floor preset, report v2 P1-4), `--despill-radius` (fork port), `--connectivity`, `--max-dropped-fraction`, `--art-source` (B10-T1..T4).
- Added: manifest `generate2dmap.prop_pack.v2`: `display_name`, `cell_box`, `source_rect`, `trim_offset`, `padding`, `anchor_px`, `dropped_components`/`dropped_area`, `sha256` per image, footprint and occlusion fields from boxes files, a QA envelope, and `read_manifest()` for v1 manifests (B10-T3).
- Added: the asset strategy gate warns when a prop in a square grid is taller or wider than 1.6:1 (B10-T4); `prop-pack-contract.md` rewritten with the gate, prompt template, tall props, anchors, footprints, occlusion, pixel props, despill and auto boxes, all commands single-line (B10-T5).
- Changed: keying and components use forge_matte and forge_core; a 1254^2 sheet keys in about 0.16 s (12.6 s before, MAP-13), 0.5 s including the first scipy import.
- Changed: the unified crop-box file `{"items": [{"id", "box"}]}` (DOC-18); the legacy `{"props": [{"label", "source_box"}]}` form is still read.
- BREAKING: refuses an existing `--output-dir` and exits 1 when nothing is accepted; legacy switch: choose a new folder (Appendix H).
- BREAKING: chroma sheets get edge despill radius 1; legacy switch `--despill-radius 0` (Appendix H).
- BREAKING: 8-connected components and manifest v2 with `anchor_px`; legacy switch `--connectivity 4`; v1 manifests stay readable (Appendix H). With `--connectivity 4 --despill-radius 0 --min-component-area 100` the prop pixels equal cfed170 (tested).
- BREAKING (not yet in Appendix H, please add): `--min-component-area` defaults to auto (100 px for a 418 px cell, scaled with the cell area, between 1 and 100), so small pixel props are no longer judged empty; legacy switch `--min-component-area 100`.
- BREAKING (not yet in Appendix H, bug fixes without a switch): `--keep-empty` items have status `placeholder` and `output_size` [1, 1] (MAP-20); `--labels` with `--labels-file` is an error (the file used to win silently); labels that become a Windows device name are refused (repro_08); usage errors exit 1 instead of 2; stdout is a JSON summary instead of the manifest path; PNGs carry no metadata chunks and zero RGB under alpha 0.
- Fixed: MAP-02 (props floated 0-8 px: manifest anchors), MAP-03 (magenta fringe: 2419 -> 111 tinted px on the meadow cell), MAP-04, MAP-06 (diagonal strokes kept, drops reported), MAP-09 (chroma path and CLI tested), MAP-12, MAP-13, MAP-17/DOC-10 (stale files), MAP-18 (trim offsets), MAP-20, MAP-23 (CJK labels), MAP-24 (manifest-relative paths with sha256), DOC-18, and the probes "all-empty run exits 0", "16 px coin judged empty" and "re-crop moves authored anchors".

## 5. Schema change requests

prop_pack_v2 needs no change: every manifest the tests produce validates against the vendored `map.schema.json#/$defs/prop_pack_v2`, and its `qa` block against `common.schema.json#/$defs/qaEnvelope`.

Request 1 (new def, DOC-18). Add to `shared/schemas/common.schema.json` at JSON pointer `/$defs/cropBoxes`. Producer: B10 (`auto-boxes.json`). Consumers: B10 (`--boxes-file`), proposed for B02 (`assemble_frames --crop-boxes`). `tests/test_extract_prop_pack.py::validate_crop_boxes` applies exactly this def in memory to the vendored common schema; integration can drop that helper and use `assert_valid_contract(doc, "common", "cropBoxes")`.

```json
"cropBoxes": {
  "description": "forge-crop-boxes/v1: measured crop boxes in sheet pixels (DOC-18). extract_prop_pack reads it with --boxes-file and writes it as auto-boxes.json; assemble_frames --crop-boxes should read it too. Readers also accept the legacy {props: [{label, source_box}]} object and a bare list of boxes.",
  "type": "object",
  "required": ["items"],
  "properties": {
    "schema": {"const": "forge-crop-boxes/v1"},
    "source": {"$ref": "#/$defs/fileRef"},
    "items": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["id", "box"],
        "properties": {
          "id": {"type": "string", "minLength": 1},
          "box": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 4, "maxItems": 4},
          "display_name": {"type": "string", "minLength": 1},
          "anchor_px": {"$ref": "#/$defs/point2"}
        }
      }
    }
  }
}
```

Suggested fixtures for `tests/test_contracts.py`: `common.cropBoxes.valid.json` (the example in prop-pack-contract.md) and invalid cases "box has 3 numbers", "fractional box", "missing id", "empty items".

Request 2 (optional, documentation only; objects stay open). Optional fields the producer writes, so they do not drift:

- `prop_pack_v2` top level: `tool` {name, version}, `input` (relPath, or a bare file name for an input on another drive), `source_mode`, `layout_mode` (`grid`, `explicit_boxes`, `auto_boxes`), `boxes_file`, `labels_file`, `rows`, `cols`, `threshold`, `edge_threshold`, `despill_radius`, `despill` (forge_matte.despill report: mode, applied, radius, margin, changed_px), `hygiene` (forge_core.alpha_hygiene report), `alpha_floor`, `alpha_floor_pixels_removed`, `trim_border`, `edge_clean_depth`, `component_mode`, `component_padding`, `min_component_area` (integer or "auto"), `edge_touch_margin`, `geometry` {connectivity, alpha_geometry_threshold, anchor_mode, support_band_fraction, anchor_rounding, grid_rounding, keep_canvas, world_scale (string fraction or null)}, `auto_boxes` {objects, solid_alpha, min_solid_area, gap_px, attach_px, margin_px, unowned_px}, `edge_touch_props`, `warnings`, `qa` (qaEnvelope).
- `propItem`: `index`, `grid`, `source_box` (v1 alias of cell_box), `output_size`, `trim_offset` (point2), `trim_applied`, `anchor_source` (`measured`, `authored`, `placeholder`; read_manifest adds `derived-v1`), `component_mode`, `component_count`, `min_component_area`, `kept_area`, `crop_bbox`, `padded_crop_bbox`, `selected_component_area`, `selected_component_bbox`, `edge_touch`, `edge_fringe_px` (chroma only), `world_size` and `world_anchor` (integers, with --world-scale).
- `footprint`: `suggested` (true for --suggest-footprint) and `method`.
- `rejected` items: `index`, `label` ("" when skipped), `display_name`, `grid`, `cell_box`, `source_box`, `status` (`empty` or `skipped-label`), and for empty cells `component_count`, `dropped_components`, `dropped_area`, `edge_touch`, `min_component_area`.

If wanted, the fragment for `map.schema.json` at `/$defs/propItem/properties` is: `"anchor_source": {"enum": ["measured", "authored", "placeholder", "derived-v1"]}, "trim_offset": {"$ref": "common.schema.json#/$defs/point2"}, "kept_area": {"type": "integer", "minimum": 0}, "world_size": {"$ref": "common.schema.json#/$defs/size2"}, "world_anchor": {"$ref": "common.schema.json#/$defs/point2"}, "edge_fringe_px": {"type": "integer", "minimum": 0}`, and at `/$defs/prop_pack_v2/properties`: `"layout_mode": {"enum": ["grid", "explicit_boxes", "auto_boxes"]}, "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}`.

## 6. Shared-helper promotion requests

- `_local_dilate_square(mask, radius)` (extract_prop_pack.py:106): Chebyshev dilation with clipped windows, the same code as the private `forge_core._dilate_square` and `forge_matte._dilate`. Joins A2's request for a public `forge_core.dilate_square(mask, radius)`; then drop the local copy. Covered by the edge-fringe and auto-box tests.
- `_grow_labels(owner, allowed, steps)` (extract_prop_pack.py:349): breadth-first label growth through allowed pixels (smallest neighbouring label wins, order independent). Candidate `forge_core.grow_labels(labels, allowed, steps)` if B02's `assemble_frames --slice ownership` needs component ownership too; otherwise keep local. Covered by the forest-replica tests.
- `_round_half_up(value)` (extract_prop_pack.py:89) duplicates the private `forge_core._round_half_up`; a public `forge_core.round_half_up` would remove it. Low priority.

## 7. Cross-module links that Z must add

- B11 (extract_platform_strip.py): its `sprite_helpers()` still loads `remove_bg_magenta` from extract_prop_pack.py (kept as a wrapper of `forge_matte.legacy_hard_key`, bit-exact, tested). B11-T1 replaces that import with forge_matte; after B11 merges, integration may delete `remove_bg_magenta` from extract_prop_pack.py.
- B12 (compose_layered_preview.py): with placement `anchor: manifest`, put the prop's `anchor_px` (image pixels, a whole pixel on the ground line) on (x, y); `output_size` is the source size; read v1 manifests with `extract_prop_pack.read_manifest()` (bottom-centre anchor). The repro_20 test emulates this placement and gets 0 px float.
- B13 (map_bundle, map_nav): `footprint` is in prop pixels, centred at `anchor_px + offset`, scaled once by the instance scale and never inflated for actor size; `solid`, `contact`, `occlusion_class` and `occupant_policy` come from the prop item (authored in the boxes file).
- B02 (assemble_frames): adopt `forge-crop-boxes/v1` (section 5 request 1) for `--crop-boxes` so both tools share one format (DOC-18).
- layered-map-contract.md (B12/B13/Z, DOC-20): its placement example uses `occlusionClass: "canopy"` and `occupantPolicy {mode: fade-when-hidden}`; align with the propItem enums (`occlusion_class` low, tall, foreground; `occupant_policy` y_sort, rear_shift_and_fade, static_front, static_back), which prop-pack-contract.md now defines.
- generate2dmap SKILL.md (Z): the routing and tool rows of section 2; "Acceptance" should say prop packs are verified by the manifest QA plus a visual check of anchors and footprints.
- map-strategies.md or SKILL.md (Z): host image tools keep aspect and area, not the requested size, so grids may not divide; point to `--grid-rounding nearest` and `--auto-boxes`.
- codeart2d references (B18/B21/Z): props drawn as one sheet go through `--boxes-file` with authored `anchor_px` and `--keep-canvas`; individual PNGs need no extraction.

## 8. Known limitations and what is not proven

- Anchors and footprints are heuristics measured from alpha (> 16). The default anchor is `stance` over the bottom quarter of the art; on the six real meadow props it lands on the trunk, post or base of five, and on the leaning log it is 34 px left of the box centre (the feet rule put it 89 px left). Lying, leaning and overhanging props need an authored `anchor_px`. Footprint suggestions were judged visually on the same six props only; they are starting points.
- Deviation: forge_core's sprite convention measures feet over about 6% of the height (12 rows). Props use a quarter (`SUPPORT_BAND_FRACTION = 0.25`) and default to `stance`, because 6% put the log's anchor on its far end and gave footprints a third of the boulder and bush bases (rendered comparison of 6%, 15% and 25% on the meadow props).
- Deviation: plan T4 lists `--alpha-hygiene` without a default. It stays `none` for grid and box layouts, because Appendix H has no prop-pack hygiene row; it is `both` only with the new `--auto-boxes`, where haze would otherwise inflate the boxes.
- Deviation: `--min-component-area auto` (plan T3 "min area scaled to the cell") changes the default below 418 px cells; it is listed as BREAKING with the legacy switch in section 4.
- The forest acceptance (3 components, 536x672 tree canvas, lantern box equal to the hand-measured box, anchor error 0.000 at 3/8) is proven on a synthetic 1254^2 replica with the measured content boxes, alpha 1-4 haze and faint islands, not on the owner's git-ignored atlas (not a registered fixture). The meadow numbers (2419, 111, 36) are measured on the real fixture.
- Auto-box parameters (seed alpha 128, min seed area sheet/30000, gap short side/52, attach short side/156) were checked on the replica, the meadow crop (6 of 6 props, nothing unowned) and synthetic sheets only. Props closer than the gap merge; parts further apart than the gap split; faint parts that are not connected within the attach distance stay unowned (reported as a QA warning).
- QA thresholds: edge fringe warns above 2% of the pixels within 2 px of transparency (meadow at radius 1: 0.2-1.4%; radius 0: 24-30%); dropped pixels warn above 1%. Real purple at an edge counts as fringe and is neutralised by despill.
- `--world-scale` padding applies to accepted props only; placeholders get none. Authored anchors must be whole pixels with `--world-scale`.
- A manifest outside the output folder is published just before the folder and removed if the folder publication fails; the two paths are not one atomic transaction. Publication uses `forge_core.staged_output`, whose WSL and network-filesystem fallback is not atomic (A1 section 8).
- Not run: Linux, macOS, Python 3.10, Pillow 10.1, numpy 1.26 (Windows 11, Python 3.13, Pillow 12.3, numpy 2.5, scipy 1.18 only). Byte determinism is shown for two runs on one machine; PNG bytes depend on the zlib build.
- The repro_20 test emulates the compositor (pastes at x - anchor_x, y - anchor_y); it does not run B12's compose_layered_preview.
