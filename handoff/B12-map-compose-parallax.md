# B12-map-compose-parallax: ground-line compose with manifest anchors, overlay, audit and plate pan; parallax pivot, coverage default, seams, pixel grid and aspect sweep; conform_background

Branch `asf/B12-map-compose-parallax` (from `wip/asf-upgrade-20261005` @ 3f9252d). Files:
[compose_layered_preview.py](../skills/generate2dmap/scripts/compose_layered_preview.py),
[validate_parallax.py](../skills/generate2dmap/scripts/validate_parallax.py),
[conform_background.py](../skills/generate2dmap/scripts/conform_background.py) (new),
[parallax-backgrounds.md](../skills/generate2dmap/references/parallax-backgrounds.md),
[test_compose_layered_preview.py](../tests/test_compose_layered_preview.py) (50 tests),
[test_validate_parallax.py](../tests/test_validate_parallax.py) (46 tests),
[test_conform_background.py](../tests/test_conform_background.py) (12 tests, new).

> **Integration (2026-10-05, group pass "map-core", branch `asf/int-g-map-core`).** Resolved: both blocking review
> items. Footprint `basis` `prop_px` is the default and never warned about, `image_px` is its legacy alias (D7). With
> `--bundle` the actor-feet audit is the vendored forge_nav on the bundle's D2 blocking set, so it gives the same answer
> as `map_nav.py query` (D2, D4, D33). Without a bundle the canvas model uses closed rect, ellipse and polygon solids and
> blocks on blocking material classes (D1, D33). Also resolved: v1 prop packs anchor through
> `extract_prop_pack.read_manifest` (D10); repeat seams are `forge_core.edge_seam_report` with snake_case verdicts (D9);
> `conform` exits 1 when it publishes a failed QA report (D26); placements honour `flip_x` (D6); JSON may carry a BOM
> (D28); absolute plan image paths no longer reach the parallax report; placement `footprint` and `solid` are tested
> against the contract; `run_cli` (D27); `tool.version` `0.4.0` (D29);
> the helper promotions of section 6 (D30); and the schema requests of section 5, applied by the shared stage. The
> reviewer repros were rerun: an actor on a `solid` material pixel now fails in compose as in map_nav, 14 points on and
> around rect edges agree, and the real forest-shrine pack extracted by B10 composes with no basis warning. Open
> items are marked **Z** (or name their owner).

## 1. CLIs

Run from the project root; `<skill-dir>` is the generate2dmap folder (`${CLAUDE_SKILL_DIR}` in Claude Code).

    python "<skill-dir>/scripts/compose_layered_preview.py" --base map/base.png --placements map/placements.json --output map/qa/preview.png --report map/qa/compose-report.json
    python "<skill-dir>/scripts/compose_layered_preview.py" --base map/base.png --placements map/placements.json --output map/qa/preview.png --bundle map/map_bundle.json --debug-overlay map/qa/debug-overlay.png --audit-out map/qa/placement-audit.json --strict
    python "<skill-dir>/scripts/compose_layered_preview.py" --base scene/plate.png --placements scene/placements.json --output scene/qa/preview.png --stage scene/stage.json --mask scene/motion-mask.png --debug-overlay scene/qa/overlay.png --audit-out scene/qa/audit.json
    python "<skill-dir>/scripts/compose_layered_preview.py" --base map/base.png --placements map/placements.json --output map/qa/preview-3x.png --scale 3 --resampler nearest
    python "<skill-dir>/scripts/compose_layered_preview.py" --base stage/plate.png --placements stage/placements.json --output stage/qa/preview.png --plate-pan stage/qa/plate-pan.png --pan-viewport 960x540 --pan-zoom 1.12 --pan-frames 5
    python "<skill-dir>/scripts/compose_layered_preview.py" --base map/base.png --placements map/placements.json --output map/qa/legacy.png --sort raw-y --anchor px
    python "<skill-dir>/scripts/validate_parallax.py" --spec stage/parallax-plan.json --report stage/qa/parallax-qa.json
    python "<skill-dir>/scripts/validate_parallax.py" --spec stage/parallax-plan.json --coverage sky-only --aspects none
    python "<skill-dir>/scripts/validate_parallax.py" --spec stage/parallax-plan.json --pixel-grid --strict-seams --strict --report stage/qa/parallax-qa.json
    python "<skill-dir>/scripts/conform_background.py" conform --input art/forest.png --size 1280x720 --mode cover --output-dir stage/bg-cover
    python "<skill-dir>/scripts/conform_background.py" conform --input art/forest.png --size 1280x720 --mode ground-fit --ground-y 718.7 --floor-y 610 --subject well=700,500,820,700 --aspects 16:9,19.5:9,4:3 --strict --output-dir stage/bg-fit
    python "<skill-dir>/scripts/conform_background.py" validate-crops --input stage/plate.png --subject crest=600,200,680,280 --min-margin 8 --output-dir stage/qa/crops
    python "<skill-dir>/scripts/conform_background.py" validate-crops --input stage/plate.png --zoom 1.12 --focus 0,0.5 --focus 1,0.5 --subject crest=600,200,680,280 --output-dir stage/qa/pan-crops

`--help` (and `conform --help`, `validate-crops --help`) prints ASCII only and exits 0 under `PYTHONIOENCODING=cp1252` and `cp950` (tested with `forge_testutils.assert_cli_help` and per verb). Errors print `error: ...` on stderr and exit 1; argparse usage errors exit 2 (D26); anything unexpected prints `error: internal error (<Type>: <message>)` and exits 1 (`forge_core.run_cli`, D27); warnings print `warning: ...` on stderr. JSON inputs (placements, prop packs, bundle, stage, plan) may carry a UTF-8 BOM (`forge_core.read_json`, D28); QA envelopes carry `tool.version` `0.4.0` (D29).

One-line ASCII JSON on stdout:

- compose: the absolute path of every output given (`output`, `report`, `debug_overlay`, `audit_out`, `plate_pan`), `placed`, `warnings` (count) and `audit_status` (`pass`/`warn`/`fail`, or null without an audit). Exit 0 unless an error or a `--strict` audit failure.
- validate_parallax: the full result (schema `generate2dmap.parallax_validation.v2`) plus `report` (absolute path, or null). Exit 0 on pass, 1 on failure; on an input error the result is `{schema, passed: false, issues}` and the error is also on stderr.
- conform: `output_dir`, `background`, `metadata` (conform.json), `mode`, `scale`, `qa_status`. Exit 0 unless an error, a `--strict` failure, or a failed subject check without `--strict`: that run publishes, reports `qa_status: fail`, prints `error: conform published a failed QA report: <conform.json>` and exits 1 (D26).
- validate-crops: `output_dir`, `report` (crops-qa.json), `overlay` (crops-overlay.png), `status`, `cut` (failed windows). Exit 1 when a subject is cut.

Compose placements (D6, D7): `flip_x: true` mirrors a placement's art around its anchor x and its footprint with it (`offset[0]` and `rotate` change sign); the report entry then carries `flip_x: true`. Footprint `basis` is `prop_px` (the default; follows the drawn sprite), `world_px` (follows only `--scale`) or the legacy `image_px` (read as `prop_px`).

Compose audit (`--audit-out`): actor feet follow forge_nav rule N9 at the placement position. With `--bundle` they are judged on the bundle's blocking set in world pixels, read by the vendored forge_nav (collision solids and rects, solid object footprints, tile collision, blocking material pixels; D2, D4); a bundle forge_nav cannot read falls back to compose's own parse with a warning. Without a bundle they are judged on the canvas model (the canvas or the stage ground polygons as the walk area, solid placement footprints, blocking material masks) with the same closed-set rules. Each actor row records `foot_valid`, `blocked_by` (solid sources, or `material_map`) and `foot_checked` (the world point tested), with a bundle also `in_placement_footprints`; `actor_feet_valid.value` names the `model` (`bundle` or `canvas`) and the `collision` source. The check `actor_feet_off_placement_footprints` (warn, with a bundle only) lists actors standing inside a solid placement footprint: placement footprints are preview data and never block when the bundle decides.

Publication: compose writes only the files named, after the whole run (and the `--strict` audit) succeeded, with `forge_core.publish_file_no_replace` and rollback; it refuses an existing output, two outputs naming one file, and any output that aliases an input (base, placements, prop images, prop-pack manifests, bundle, stage, masks, material map; path, case, hard-link and symlink aliases). validate_parallax `--report` never replaces a file and is published whole; `--strict` writes nothing on failure. conform_background writes a new `--output-dir` through `forge_core.staged_output`.

## 2. SKILL.md routing rows (**Z**)

generate2dmap:

| Need | Route |
|---|---|
| Preview placed props, objects and actors with the right draw order and contact anchors | `scripts/compose_layered_preview.py --report`; prop-pack manifests supply `anchor_px` (MAP-02); check `anchor_world_error` and the picture |
| Check placements against walk areas, collision, exits, approach points and footprint overlaps | `scripts/compose_layered_preview.py --bundle <map_bundle.json> (or --stage) --debug-overlay --audit-out [--strict]` (with a bundle, actor feet are judged exactly as `map_nav.py query` judges them); then `map_nav` for reachability |
| Validate a parallax plan: coverage at every camera/zoom corner, pivot, repeat seams, pixel grid, screen aspects | `scripts/validate_parallax.py --spec <plan> --report <qa.json>`; declare `camera.pivot` (`center` for centred engine cameras) and the `aspects` you ship |
| Fit a returned painting to the game size, or put its painted ground on the authored floor | `scripts/conform_background.py conform --mode cover` or `--mode ground-fit --ground-y --floor-y`; move placements through `conform.json` |
| Keep crests, faces and props visible on 16:9, 19.5:9 and 4:3 screens and across a plate pan | `scripts/conform_background.py validate-crops --subject ... [--zoom --focus]` |
| Fake camera motion with one painted backdrop | `scripts/compose_layered_preview.py --plate-pan` preview, then `validate-crops --zoom` at both pan ends (parallax-backgrounds.md section 5) |

Processing-tools table rows (replace the two existing rows, add the new one):

| Script | Purpose / limits |
|---|---|
| `scripts/compose_layered_preview.py` | Ground-line draw order, manifest anchors, `anchor_world_error`, debug overlay and placement audit from map_bundle/stage/masks, plate-pan contact sheet; no gameplay execution, reachability or video |
| `scripts/validate_parallax.py` | Camera/zoom corner coverage with pivot, coverage default for sky/repeated/near layers, normalised repeat seams, pixel grid, aspect sweep; no seamlessness proof, finite-copy or renderer simulation |
| `scripts/conform_background.py` | Cover (ImageOps.fit pixels) or ground-fit with a recorded transform; subject margins per screen aspect and plate pan; no automatic ground or subject detection |

Acceptance bullets for the map SKILL.md (**Z**): compose the scene with `--debug-overlay` and `--audit-out` and look at both; parallax plans declare `camera.pivot` and pass `validate_parallax.py`.

## 3. README tool-table rows (**Z**)

| Tool | What it does | Verified by |
|---|---|---|
| `compose_layered_preview.py` | Composes a map preview in an explicit order (background, y-sorted world by ground line, foreground) with prop-pack anchors and mirrored placements, and writes an optional report, debug overlay, placement audit (actor feet by the shared forge_nav rules) and plate-pan sheet | `tests/test_compose_layered_preview.py` (repro_01/02/03, golden draw order, manifest anchors, audit overlap, compose and map_nav agree on boundary points and material pixels) |
| `validate_parallax.py` | Checks a parallax plan against its real images: alpha, coverage at every camera/zoom corner under a top-left or centred pivot, repeat seams (the shared edge seam metric), pixel grid and screen aspects | `tests/test_validate_parallax.py` (fork suite, centre pivot 10 px gap, probe layer, brute-force corner exactness) |
| `conform_background.py` | Conforms a background to the game size by cover or ground-fit with a recorded transform, and checks subject margins per screen aspect | `tests/test_conform_background.py` (cover max diff 0 vs ImageOps.fit, ground-fit floor at y = 610) |

## 4. CHANGELOG entries (**Z**)

- Added: `conform_background.py` with `conform` (`--mode cover|ground-fit`, transform in `conform.json`, subject and crop checks) and `validate-crops` (subject margins per aspect, focus list, plate-pan zoom, overlay) (B12-T5; report v2 P1-6, 5.5).
- Added: compose `--debug-overlay`, `--bundle`, `--stage`, `--mask`, `--overlay-labels`, `--audit-out` (QA envelope: actor feet by the shared forge_nav rules, on the bundle's blocking set with `--bundle`, exactly as `map_nav.py query`; actors standing in placement footprints; feet in walk areas, bounds, footprint overlaps, duplicates, hidden actors, occlusion), `--strict`, `--plate-pan` (`--pan-viewport`, `--pan-zoom`, `--pan-frames`, `--pan-v`), `--scale`, `--prop-pack` (B12-T2, B12-T3).
- Added: compose report `generate2dmap.compose_report.v2`: explicit `compositing_order`, per placement `group`, `kind` (actor labels), `band`, `draw_index`, effective `anchorPx` and `anchor_source`, `anchor_world`, `anchor_canvas`, `anchor_world_error`, `sort_source`, `visible_bounds`, `footprint`, warnings; every path manifest-relative with sha256 (B12-T2; MAP-24).
- Added: placements honour an instance `scale`, `flip_x` (art and footprint mirrored around the anchor x, D6), a per-placement `footprint` (basis `prop_px` by default, `world_px`, legacy `image_px`; D7) and `solid`; a placement on layer `background` draws before the world band (B12-T3).
- Added: compose reads v1 (cfed170) prop packs through `extract_prop_pack.read_manifest`, so their props stand on the art's bottom edge instead of floating by the padding (D10).
- Added: validate_parallax `camera.pivot` (`top-left`, `center`, `[x, y]`), `gaps_px` and `canvas_visible` per corner, `coverage_rule` per layer, `--pixel-grid` with plan `pixel_art`/`sampling: nearest`, aspect sweep (`--aspects`, `--aspect-policy`, plan `aspects`/`aspect_policy`), repeat-seam verdicts from the shared `forge_core.edge_seam_report` (`continuous`, `seam`, `duplicate_edge`, `flat`, `too_small`, with `seam_ratio`, `near_median` and the old whole-layer comparison as `layer_p95`) and `--strict-seams` (fails `seam` and `duplicate_edge` only), `--strict`, warnings (B12-T4, D9).
- Changed: compose prints a one-line JSON summary (was the bare output path) and warns about unknown layers, ignored anchor names, unread top-level lists, unknown footprint shapes/bases and unknown manifest `occlusion_class`/`occupant_policy` values (B12-T2).
- Changed: validate_parallax prints the result as one line of ASCII JSON (was indented), reports schema `generate2dmap.parallax_validation.v2`, reports each layer's `image` as written in the plan (an absolute plan path relative to the report, or as its file name; was an absolute path) and loads images with `forge_core.load_rgba` (B12-T4).
- Changed: `conform` exits 1 when it publishes a report whose QA status is fail (a cut subject without `--strict`), as `validate-crops` does for a cut window (D26). JSON inputs of the three tools may carry a UTF-8 BOM (D28); QA `tool.version` is the package version `0.4.0` (D29).
- Changed: parallax-backgrounds.md: planning kit, pivot formulas, validator defaults, sub-pixel presentation, single-plate pan, aspect-safe backdrops; single-line commands (B12-T6).
- BREAKING: compose sorts by the ground line with ties by (sortY, x, id) and takes anchors from prop-pack manifests; legacy switches `--sort raw-y` and `--anchor px` (plan Appendix H, compose row).
- BREAKING: validate_parallax requires canvas coverage for repeated and near/foreground layers; legacy switch `--coverage sky-only` (plan Appendix H, parallax row). A layer's `require_canvas_coverage: false` still opts it out.
- BREAKING: compose refuses an existing `--output`/`--report`/other output and validate_parallax refuses an existing `--report` (Appendix D); no switch, use a new path.
- Fixed: MAP-05 (raw-y sort drew short bushes over taller trees), MAP-21 (banker's rounding moved props 0 or 2 px per world px), MAP-22 (`--output`/`--report` could overwrite the base or placements), MAP-02 compose side (extracted props floated by their padding), MAP-24 (absolute paths in compose and parallax reports), MAP-08 (top-left zoom pivot hid gaps of centred cameras), MAP-14 (edge-equality seams passed duplicated columns and failed true joins), probe (a near layer scrolled fully off screen and still passed), report v2 P1-6 (`anchor_world_error`, conform), fractional placement `w`/`h` were truncated (now half up), compose palette/16-bit PNG props now load through `forge_core.load_rgba`.

## 5. Schema change requests

Resolved: every fragment of this section (`composedPlacement`, `compose_report_v2`, `parallaxSeam`, `parallaxExtreme`, `parallaxLayerReport`, `parallax_validation_v2`, `conform_v1`, the plan's `pixel_art`, `sampling`, `aspects` and `aspect_policy`, and the placement's `footprint` and `solid`) is in the vendored map schema, applied by the shared stage. `parallaxSeam.verdict` is the common `edgeSeam` verdict (D9), and the footprint `basis` enum is `prop_px`, `world_px` and `image_px` (D7). The three test files validate every document against the vendored schemas.

Open follow-ups for the schema owner (all additive):

- `parallaxSeam`: validate_parallax now writes `seam_ratio` and `near_median` for every verdict (`frames` and `layer_p95` only when the layer has at least two steps along the axis), so both can become required.
- `placement` and `composedPlacement`: add `flip_x` (`{"type": "boolean"}`). Compose reads it and writes `flip_x: true` into the report; both objects are open, so it validates today without being documented.
- compose `--audit-out` (`generate2dmap.compose_audit.v1`) and validate-crops `crops-qa.json` (`generate2dmap.crops_qa.v1`) stay plain `qaEnvelope` documents. Audit fields: `placements[]` (`id, kind, band, draw_index, foot, visible_bounds, foot_valid|foot_walkable, blocked_by, foot_checked, in_placement_footprints, occluded_fraction, occluded_by, footprint`), `overlaps[]` (`a, b, kind, area_px`), `walk_area`; check ids `actor_feet_valid` (its value names `model` and `collision`), `actor_feet_off_placement_footprints`, `feet_in_walk_area`, `bounds`, `footprint_overlaps`, `duplicate_placements`, `actors_visible`. crops-qa.json: `plate_size`, `subjects[]`, one check per `crop <aspect> focus <fx>,<fy>` with `value {aspect, focus, zoom, window, subjects{id: {left, top, right, bottom, min, visible_fraction}}, cut[]}`.

## 6. Shared-helper promotion requests

All resolved (D30, D4): `forge_core.round_half_up` replaces `_local_round_half_up`; `forge_core.file_ref` and `forge_core.manifest_path` replace `_local_file_ref` and `_local_portable_ref` (compose and conform); `forge_core.parse_aspect`, `aspect_viewport` and `cover_window` replace the screen-aspect helpers of validate_parallax and conform_background; the compose audit's point tests (`_local_points_in_polygon`, `_local_points_in_shape`, `_local_walkable`, `_local_footprint_samples`) are gone: actor feet use `forge_nav.CollisionModel` (`valid`, `blocked`, `area_ok`, `footprint_offsets`) on the bundle's blocking set or on the canvas model.

## 7. Cross-module links

- Map SKILL.md (**Z**): replace the compose and validate_parallax rows and add conform_background (section 2); acceptance bullets for `--debug-overlay`/`--audit-out` and `camera.pivot`; route "single painted backdrop that must pan" and "backdrop for several screen shapes" to parallax-backgrounds.md sections 5 and 6.
- Done at integration (same group): layered-map-contract.md documents compose's placement fields, that gameplay metadata lives in the bundle while placement footprints are preview data, the `--bundle` audit and `actor_feet_off_placement_footprints`; prop-pack-contract.md documents how compose finds manifests, takes `anchor_px`, `footprint` and `solid`, refuses a stale manifest anchor and reads v1 packs; side-scroll-scenes.md already links the parallax-backgrounds.md planning kit.
- background-scenes.md (B16): link conform_background (`conform`, `validate-crops`) for host-sized paintings and aspect-safe plates, and parallax-backgrounds.md section 5 for single-plate pans.
- hd2d-plates.md / edit_locality_check (B15): read `generate2dmap.conform.v1` `transform` (`scale`, `src_rect`) as the recorded conform transform; validate_stage's aspect solver can use `forge_core.parse_aspect` and `aspect_viewport`, as validate-crops does (D30).
- layouts-and-parallax.md and parallax_build (B21): plans must satisfy the new coverage default (repeated and near/foreground layers cover the viewport; set `require_canvas_coverage: false` only for local near pieces), should declare `camera.pivot`, and may list `aspects`; the validator now prints one-line JSON with a `report` key, its report is schema v2 with snake_case seam verdicts (D9), and `--report` refuses an existing file.
- scene-preview.md / build_scene_preview (B17): use the same `(sortY, x, id)` tie rule and ground-line sortY as compose so previews and composed stills agree.
- README tool table (**Z**): the three rows of section 3.

## 8. Known limitations and what is not proven

- Plan deviations, with reasons: the fork's compose test 5 asserted the cfed170 raw-y sortY, so it now runs under `sort="raw-y"` and also asserts the new default (5.0); the fork's parallax CLI test re-ran onto the same `--report`, so its second run uses a new path (reports never replace files, Appendix D). Both passed unmodified against the cfed170 scripts first (commit d42266a, B12-T1). The parallax report id moved to `.v2` because the transform (pivot) and the coverage rule changed.
- Legacy switches restore order and anchors, not bytes: `--sort raw-y --anchor px` keep the cfed170 draw order and anchor rules, but positions now round half up (MAP-21), fractional `w`/`h` round half up instead of truncating, images load through `load_rgba`, previews are saved by `save_png` (RGB zeroed under alpha 0), and a placement on layer `background` draws first.
- With `--bundle` the audit's actor test is forge_nav's on the bundle (D4): compose and `map_nav.py query` agree on the reviewer's repros (an actor on a `solid` material pixel; 14 points on and around rect edges) and in `IntegrationDecisionTests`. Parity with map-runtime.mjs is B17's (D4). A bundle forge_nav refuses falls back to compose's own parse of it, with a warning. Without a bundle the canvas model is compose's own geometry (stage ground polygons or the canvas, solid placement footprints, material masks resized to the canvas by nearest) under the same closed-set rules, in canvas pixels. Props are tested as points; footprint overlaps are rasterised at pixel centres. Footprint overlaps, props outside walk areas and actors standing in placement footprints are warnings, not failures; only invalid actors and placements entirely off the canvas fail.
- Manifest discovery assumes extract_prop_pack's layout (image beside prop-pack.json or one folder down); the module tests use synthetic prop_pack_v2-valid fixtures, and the reviewer's repro was rerun on B10's real output (the forest-shrine pack: 9/9 props anchored and audited from the manifest, no warnings). v1 manifests are read through `extract_prop_pack.read_manifest`: props anchor on the art's bottom edge (D10); a hand-made v1 item without `source_box` has no anchor and falls back to the image's bottom centre with a warning.
- The debug overlay's labels can overlap on small canvases, and it uses Pillow's default FreeType font, so overlay bytes can differ across FreeType builds. The plate-pan sheet and the preview are byte-deterministic on one Pillow build (tested by re-running).
- validate_parallax: corner checks are exact for its affine transform under any pivot (brute-force test over 240 random plans); rotation, perspective, shake, renderer rounding, culling and finite repeat copies are not simulated. The aspect sweep models `expand`, `fixed-height` and `fixed-width` with the pivot share kept; letterboxing and engine-specific stretch modes are not modelled. The pixel grid is checked at the zoom extremes only. Seam verdicts are statistics (`forge_core.edge_seam_report`, D9): the wrap step against the art's own steps near it; `duplicate_edge` needs a wrap step under a quarter of the median step right next to it (with that median at least 2), and `flat` and `too_small` never fail.
- conform_background: `cover` equals `ImageOps.fit` exactly on Pillow 12.3 (max diff 0 in the tests). The ground-fit acceptance used a synthetic painting with the report-v2 sizes (1672x941 to 1280x720) and reproduces the prototype's transform (scale 0.848769458, src_rect [81.967213, 0, 1590.032787, 848.286885]) and a measured floor edge at 610 +- 0.25 px; the real forest painting is not a provenance fixture. Ground rows and subject boxes are supplied by the user; nothing is detected in the art.
- Platforms: run on Windows 11 only (Python 3.13, Pillow 12.3, numpy 2.5, scipy 1.18). The symlink alias test skips without the symlink privilege. Python 3.10 / Pillow 10.1 floors and Linux/macOS were not run; `ImageFont.load_default(size=...)` needs Pillow 10.1 or later (a fallback to the bitmap font is in place).
- Not done: nothing in B12's task list is open. Out of scope and left to their owners: map SKILL.md and README rows (Z), layered-map/prop-pack/background-scenes docs (B13, B10, B16).
