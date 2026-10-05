# B10-map-prop-pack: extract_prop_pack v2 (despill port, staged publish, anchors, 8-connectivity, rounding, keep-canvas, auto-boxes, footprints, hygiene, strategy gate)

Branch `asf/B10-map-prop-pack` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files: [extract_prop_pack.py](../skills/generate2dmap/scripts/extract_prop_pack.py), [prop-pack-contract.md](../skills/generate2dmap/references/prop-pack-contract.md), [tests/test_extract_prop_pack.py](../tests/test_extract_prop_pack.py) (71 tests).

> **Integration (2026-10-05, group pass "map-core", branch `asf/int-g-map-core`).** Resolved: the dead
> `remove_bg_magenta` wrapper and its test are deleted (D10); argparse usage errors exit 2 again, because the private
> `_Parser` is gone (D26); `main` runs under `forge_core.run_cli` (D27); JSON inputs (boxes files, manifests read by
> `read_manifest`) go through `forge_core.read_json` and may carry a BOM (D28); QA `tool.version` is `0.4.0` (D29);
> `forge_core.dilate_square`, `round_half_up`, `file_ref` and `manifest_path` replace the local copies (D30); the
> `cropBoxes` request is in the vendored common schema, applied by the shared stage, and the tests use
> `assert_valid_contract`. prop-pack-contract.md now defines the footprint `basis` names (D7), says how map bundles and
> compose use a pack, and states the exit codes. compose reads v1 packs through `read_manifest` (D10), which passes
> hand-made v1 items without a `source_box` through unchanged. Open items are marked **Z** (or name their owner).

The tool keys and labels with the shared libraries: `forge_matte.legacy_hard_key` (bit-exact cfed170 keyer), `forge_matte.despill` (edge mode, margin 12, bit-exact with the fork's port for radius 0-3), `forge_core.label_components`, `alpha_hygiene`, `anchor_from_mask`, `rounded_grid_boxes`, `dilate_square`, `round_half_up`, `load_rgba`, `save_png`, `staged_output`, `publish_file_no_replace`, `write_json`, `read_json`, `file_ref`, `manifest_path`, `sha256_file`, `run_cli`, `ascii_text`. No pure-Python pixel loop is left.

## 1. CLIs

Run from the user's project root; `<skill-dir>` is `skills/generate2dmap` (`${CLAUDE_SKILL_DIR}` in Claude Code).

    python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --rows 2 --cols 2 --labels shrub,lantern,crate,stump --output-dir assets/props --reject-edge-touch
    python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --rows 4 --cols 4 --grid-rounding nearest --output-dir assets/props
    python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --auto-boxes --labels tree,lantern,rock --output-dir assets/props --world-scale 3/8 --suggest-footprint ellipse
    python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --boxes-file raw/boxes.json --component-mode all --keep-canvas --output-dir assets/props

- One command, no subcommands. Layout is one of `--rows/--cols` (`--grid-rounding exact|nearest`), `--boxes-file` (unified `forge-crop-boxes/v1` items or the legacy `props` list) or `--auto-boxes` (`--auto-box-gap`).
- New flags: `--despill-radius 0..3` (default 1, chroma only), `--alpha-hygiene none|floor|detached|both` (`--alpha-floor N` alone means `floor` at N), `--connectivity 4|8` (default 8), `--min-component-area N|auto` (default auto), `--keep-canvas`, `--anchor-mode stance|feet|bbox|center|centroid` (default stance), `--world-scale 0.5|3/8`, `--suggest-footprint ellipse|rect`, `--footprint-depth-ratio` (0.5), `--max-dropped-fraction`, `--art-source`. Kept: `--threshold`, `--edge-threshold`, `--trim-border`, `--edge-clean-depth`, `--component-mode`, `--component-padding`, `--edge-touch-margin`, `--reject-edge-touch`, `--keep-empty`, `--labels`, `--labels-file`, `--manifest`.
- `--help` is ASCII and exits 0 under cp1252 and cp950 (tested); its epilog has two single-line examples.
- Success prints one ASCII JSON line: `status`, `output_dir`, `manifest` (absolute paths), `accepted`, `placeholders`, `rejected` (counts), `qa` (pass or warn) and `warnings`. An argparse usage error (unknown or malformed flag) exits 2 (D26); every other failure is one `error: ...` line on stderr with exit 1 and publishes nothing; anything unexpected prints `error: internal error (<Type>: <message>)` and exits 1 (`forge_core.run_cli`, D27). JSON inputs may carry a UTF-8 BOM (D28); the QA envelope's `tool.version` is `0.4.0` (D29).
- Library entry points in the same skill: `extract(args) -> manifest`, `read_manifest(path)` (v1 and v2; compose_layered_preview reads v1 packs through it, D10), `key_sheet(pixels, mode, threshold, edge_threshold, despill_radius)`, `read_crop_boxes(path, size)`, `find_auto_boxes(...)`, `grid_boxes(...)`. `remove_bg_magenta` was deleted at integration (D10): extract_platform_strip keys with forge_matte directly.

## 2. SKILL.md routing rows (**Z**)

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

## 3. README tool-table rows (**Z**)

| Tool | What it does | Verified by |
|---|---|---|
| `extract_prop_pack.py` | Splits a prop sheet into anchored transparent props with a prop_pack.v2 manifest: rounded grids, auto boxes, anchors, footprints, despill | `tests/test_extract_prop_pack.py`: fork despill parity (r 0-3), cfed170 parity under the legacy switches, map-audit repro_04/05/09/18/19/20/21/22, forest replica, meadow real fixture, CLI subprocess |

## 4. CHANGELOG entries (**Z**)

- Added: `--grid-rounding nearest` (rounded cell edges, MAP-04), `--auto-boxes` with `auto-boxes.json` (report v2 P1-6), `--keep-canvas` (probe), `--world-scale` with integer-exact padding (anchor error 0.000 px), `--suggest-footprint`, `--anchor-mode`, `--alpha-hygiene` (`--alpha-floor` kept as its floor preset, report v2 P1-4), `--despill-radius` (fork port), `--connectivity`, `--max-dropped-fraction`, `--art-source` (B10-T1..T4).
- Added: manifest `generate2dmap.prop_pack.v2`: `display_name`, `cell_box`, `source_rect`, `trim_offset`, `padding`, `anchor_px`, `dropped_components`/`dropped_area`, `sha256` per image, footprint and occlusion fields from boxes files, a QA envelope, and `read_manifest()` for v1 manifests (B10-T3).
- Added: the asset strategy gate warns when a prop in a square grid is taller or wider than 1.6:1 (B10-T4); `prop-pack-contract.md` rewritten with the gate, prompt template, tall props, anchors, footprints, occlusion, pixel props, despill and auto boxes, all commands single-line (B10-T5).
- Changed: keying and components use forge_matte and forge_core; a 1254^2 sheet keys in about 0.16 s (12.6 s before, MAP-13), 0.5 s including the first scipy import.
- Changed: the unified crop-box file `{"items": [{"id", "box"}]}` (DOC-18); the legacy `{"props": [{"label", "source_box"}]}` form is still read.
- BREAKING: refuses an existing `--output-dir` and exits 1 when nothing is accepted; legacy switch: choose a new folder (Appendix H).
- BREAKING: chroma sheets get edge despill radius 1; legacy switch `--despill-radius 0` (Appendix H).
- BREAKING: 8-connected components and manifest v2 with `anchor_px`; legacy switch `--connectivity 4`; v1 manifests stay readable (Appendix H). With `--connectivity 4 --despill-radius 0 --min-component-area 100` the prop pixels equal cfed170 (tested).
- BREAKING (not yet in Appendix H, please add): `--min-component-area` defaults to auto (100 px for a 418 px cell, scaled with the cell area, between 1 and 100), so small pixel props are no longer judged empty; legacy switch `--min-component-area 100`.
- BREAKING (not yet in Appendix H, bug fixes without a switch): `--keep-empty` items have status `placeholder` and `output_size` [1, 1] (MAP-20); `--labels` with `--labels-file` is an error (the file used to win silently); labels that become a Windows device name are refused (repro_08); stdout is a JSON summary instead of the manifest path; PNGs carry no metadata chunks and zero RGB under alpha 0. (Usage errors keep argparse's exit 2, D26.)
- Changed (integration): prop-pack-contract.md defines the footprint `basis` names that `--suggest-footprint` writes (`prop_px`, D7); JSON inputs may carry a BOM (D28); the QA envelope's `tool.version` is the package version `0.4.0` (D29); `read_manifest` passes hand-made v1 items without a `source_box` through unchanged (they used to raise KeyError).
- Removed (integration, D10): `extract_prop_pack.remove_bg_magenta`, the cfed170 keyer wrapper that only extract_platform_strip used; forge_matte.legacy_hard_key is the same keyer.
- Fixed: MAP-02 (props floated 0-8 px: manifest anchors), MAP-03 (magenta fringe: 2419 -> 111 tinted px on the meadow cell), MAP-04, MAP-06 (diagonal strokes kept, drops reported), MAP-09 (chroma path and CLI tested), MAP-12, MAP-13, MAP-17/DOC-10 (stale files), MAP-18 (trim offsets), MAP-20, MAP-23 (CJK labels), MAP-24 (manifest-relative paths with sha256), DOC-18, and the probes "all-empty run exits 0", "16 px coin judged empty" and "re-crop moves authored anchors".

## 5. Schema change requests

Resolved: Request 1 (`common.schema.json#/$defs/cropBoxes`, DOC-18) is in the vendored common schema, applied by the shared stage; `tests/test_extract_prop_pack.py::validate_crop_boxes` now calls `assert_valid_contract(doc, "common", "cropBoxes")` and the in-memory copy of the def is gone. Request 2's optional `propItem` fields (`anchor_source` with `derived-v1`, `trim_offset`, `kept_area`, `world_size`, `world_anchor`, `edge_fringe_px`) and the `prop_pack_v2` `layout_mode` and `qa` are in the vendored map schema too; the footprint `basis` enum is `prop_px`, `world_px` and `image_px` (D7).

Optional fields the producer still writes without a listing (objects stay open, nothing to change): `prop_pack_v2` top level `tool`, `input`, `source_mode`, `boxes_file`, `labels_file`, `rows`, `cols`, `threshold`, `edge_threshold`, `despill_radius`, `despill`, `hygiene`, `alpha_floor`, `alpha_floor_pixels_removed`, `trim_border`, `edge_clean_depth`, `component_mode`, `component_padding`, `min_component_area`, `edge_touch_margin`, `geometry`, `auto_boxes`, `edge_touch_props`, `warnings`; `propItem` `index`, `grid`, `source_box`, `output_size`, `trim_applied`, `component_mode`, `component_count`, `min_component_area`, `crop_bbox`, `padded_crop_bbox`, `selected_component_area`, `selected_component_bbox`, `edge_touch`; `rejected` items `index`, `label`, `display_name`, `grid`, `cell_box`, `source_box`, `status` (`empty` or `skipped-label`) and for empty cells `component_count`, `dropped_components`, `dropped_area`, `edge_touch`, `min_component_area`. The contract fixtures `common.cropBoxes.valid.json`, `valid-optional-id` and `invalid` exist (shared stage).

## 6. Shared-helper promotion requests

- Resolved (D30): `_local_dilate_square` is `forge_core.dilate_square`; `_round_half_up` is `forge_core.round_half_up`; the local fileRef helper is `forge_core.file_ref` (and `manifest_path` for manifest-relative paths).
- Open, low priority: `_grow_labels(owner, allowed, steps)` (breadth-first label growth through allowed pixels, smallest neighbouring label wins, order independent) stays private; it is not in D30's list. Promote it as `forge_core.grow_labels(labels, allowed, steps)` only if B02's ownership slicing (`forge_core.ownership_slice`, D14) wants the same growth. Covered by the forest-replica tests.

## 7. Cross-module links

- Done at integration (same group): extract_platform_strip keys with forge_matte and `remove_bg_magenta` is deleted (B11, D10); compose places `anchor: manifest` props by `anchor_px` and reads v1 manifests with `read_manifest()` (B12, D10); map_bundle and forge_nav read the item's `footprint` in prop pixels (`basis: prop_px`), centred at `anchor_px + offset`, scaled once and never inflated, with `solid`, `contact`, `occlusion_class` and `occupant_policy` from the item (B13, D7); layered-map-contract.md uses the propItem enums (DOC-20).
- B02 (assemble_frames, sprite group): adopt `forge-crop-boxes/v1` (section 5 request 1) for `--crop-boxes` so both tools share one format (DOC-18).
- generate2dmap SKILL.md (**Z**): the routing and tool rows of section 2; "Acceptance" should say prop packs are verified by the manifest QA plus a visual check of anchors and footprints.
- map-strategies.md or SKILL.md (**Z**): host image tools keep aspect and area, not the requested size, so grids may not divide; point to `--grid-rounding nearest` and `--auto-boxes`.
- codeart2d references (B18/B21/**Z**): props drawn as one sheet go through `--boxes-file` with authored `anchor_px` and `--keep-canvas`; individual PNGs need no extraction.

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
- The repro_20 test emulates the compositor (pastes at x - anchor_x, y - anchor_y); it does not run B12's compose_layered_preview. At integration the reviewer's repro was rerun through compose on the real forest-shrine pack (9/9 props on their manifest anchors, 0 px anchor error) and compose's own tests cover v1 packs through `read_manifest`.
