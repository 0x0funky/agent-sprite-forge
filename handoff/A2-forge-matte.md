# A2-forge-matte: one shared keyer (legacy-exact, soft v13, dominance, pockets, despill/protect, hysteresis, matte QA)

Module output: the canonical keyer [shared/forge_matte.py](../shared/forge_matte.py)
(`FORGE_MATTE_API_VERSION = "1"`, imports the `forge_core` copy beside it), byte-identical copies in
[generate2dsprite](../skills/generate2dsprite/scripts/forge_matte.py),
[generate2dmap](../skills/generate2dmap/scripts/forge_matte.py) and
[video2dsprite](../skills/video2dsprite/scripts/forge_matte.py), the tests
[tests/test_forge_matte.py](../tests/test_forge_matte.py) (49 tests), the fixture generator
[tests/fixtures/keys/make_key_fixtures.py](../tests/fixtures/keys/make_key_fixtures.py), four real crops with
[PROVENANCE.json](../tests/fixtures/keys/PROVENANCE.json), and the opt-in benchmark
[tests/benchmarks/keyer_bench.py](../tests/benchmarks/keyer_bench.py).

Every name in plan.md Appendix A "forge_matte" exists with its frozen parameters; additions are keyword-only,
extra dict keys or new names (section 5).

## 1. CLIs

No user-facing CLI: forge_matte is a library. Wave B adopts it like this (`fm` = the skill's own copy):

    sys.path.insert(0, str(Path(__file__).parent)); import forge_matte as fm   # after forge_core
    fm.key_still(image, quality=args.key_quality, resampler_hint=args.resampler)   # B01 --key-quality hard|soft|dominance|auto
    fm.legacy_hard_key(image, args.threshold, args.edge_threshold)                 # B01/B10/B11 legacy, bit-exact
    fm.despill(keyed, "edge", args.despill_radius)                                 # B01/B10/B11 --despill-radius, bit-exact
    fm.auto_interior_despill(reference_rgb, sampled_frames)                        # B05 once per clip -> KeyParams.interior_despill
    fm.soft_matte(rgb, fm.KeyParams(interior_despill=on), key, local_background=False, protect=mask)  # B05 per frame
    fm.legacy_border_flood_key(rgb, args.dist, args.despill)                       # B05 --matte binary, bit-exact
    fm.remove_enclosed_pockets(rgba, key)                                          # B05 pockets after any matte
    fm.temporal_alpha_hysteresis(frames, loop=is_loop)                             # B05 --temporal-stability alpha
    fm.matte_qa(rgba, key)                                                         # B05 matte_report.v1, B08 residue gate
    fm.choose_key_color(master_rgba)                                               # B05 key-plan --master

Developer tools (tests only, not shipped). Both `--help` texts are ASCII and exit 0 under cp1252 and cp950:

    python tests/fixtures/keys/make_key_fixtures.py synthetic --output-dir <new dir>
    python tests/fixtures/keys/make_key_fixtures.py ryo-crops --frames <frames-raw dir> --output-dir <new dir>
    python tests/benchmarks/keyer_bench.py --clip <grok-native.mp4 or frames-raw dir> --reference <input.png> --frames 57:88

`keyer_bench.py` prints report v2 tables 4-A and 4-B (markdown) and, with `--json-out <new file>`, the full
result; `python -m pytest tests/benchmarks/keyer_bench.py -m bench` asserts soft-matte fringe 0 and leak 0 on
frames 57:88 of `FORGE_BENCH_CLIP` (skips when unset).

## 2. SKILL.md routing rows

No routing rows of its own (the flags belong to B01, B05, B10 and B11). Processing-notes bullets, ready to
paste:

- generate2dsprite: chroma sheets are keyed by the shared keyer. `--key-quality auto` gives soft edges for
  lanczos output and the binary legacy key for nearest/pixel art; `--key-quality hard` reproduces the old
  keyer byte for byte. Keying-quality claims must quote matte_qa numbers (opaque key px, ring spill share,
  semi-transparent share, pockets).
- video2dsprite: plan the key before generating (`key-plan --master`): a purple, violet or pink design rejects
  magenta. The default soft matte keys enclosed holes and smoothed edges; auto interior despill is decided
  once per clip from the master and 16 sample frames. `--matte binary --despill-mode off` is the cfed170
  keyer. Light purples within the video keyer's reach need `--protect-color` or a green key.
- generate2dmap: prop and platform sheets use the same bit-exact legacy keyer; `--despill-radius` is the
  sprite skill's edge despill.

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `scripts/forge_matte.py` (vendored: sprite, map, video) | Shared chroma keyer: bit-exact legacy keyers, soft matte (frozen v13), dominance key, pocket removal, despill/unmix/protect, temporal hysteresis, matte QA, key estimation and choice | tests/test_forge_matte.py; tests/benchmarks/keyer_bench.py on the Ryo clip |

## 4. CHANGELOG entries

- Added: `forge_matte` shared keyer, one canonical copy vendored into the sprite, map and video skills
  (A2-T1..T9; S11, MAP-12). `soft_matte` is the frozen v13 prototype and reproduces its 62 evidence frames
  (Ryo 57-87, proto-auto and proto-edge) bit for bit. Also `dominance_matte`, `remove_enclosed_pockets`,
  `estimate_key`, `key_material_share`, `auto_interior_despill`, `choose_key_color`, `despill`, `unmix`,
  `protect_mask`, `protect_design_colours`, `complement_cleanup`, `temporal_alpha_hysteresis`, `flip_count`,
  `matte_qa`, `key_still`.
- Added: four real Ryo key fixtures (<= 256 px, provenance-checked) and an opt-in keyer benchmark that
  reproduces report v2 tables 4-A/4-B (A2-T8).
- Changed: the legacy keyers are vectorised with identical bytes. A 2048^2 sheet keys in 0.23 s instead of
  22.0 s (sprite/prop-pack keyer) and 0.32 s instead of 7.1 s (video keyer) (S03, MAP-13).
- BREAKING: none in this module. The defaults that use it are B01 `--key-quality auto` (legacy
  `--key-quality hard`), B05 `--matte soft --despill-mode auto` (legacy `--matte binary --despill-mode off`)
  and B10 despill radius 1 (legacy `--despill-radius 0`); see plan Appendix H.
- Fixed: enclosed key pockets (report v2 P0-1). The soft matte keys enclosed holes by colour, and
  `remove_enclosed_pockets` clears them after a binary key; the Ryo f065/f089 holes (537 and 1,222 opaque
  key px in the crops) go to 0.
- Fixed: purple fringe (report v2 P0-2). On Ryo 57-87 visible fringe falls from 8,504 to 0 px per frame,
  leak from 15.3 to 0, and the band semi-transparent share rises from 0.000 to 0.179.
- Fixed: residue metrics for the package gate (report v2 P0-3), and matte flicker (P2-1): 9.6 flips per frame
  pair become 3.5 with `temporal_alpha_hysteresis`.
- Fixed: the soft still key (P2-2), and edge despill parity with the sprite skill (S24, MAP-03).

## 5. Schema change requests

All optional (objects stay open). For `shared/schemas/video.schema.json`, add to
`$defs.matte_report_v1.properties` (producer B05, consumer B08):

```json
"key_estimate": {"type": "object", "properties": {
  "key": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
  "source": {"enum": ["border_median", "declared", "native_alpha"]},
  "valid": {"type": "boolean"}, "ring_share": {"type": "number"}, "coverage": {"type": "number"},
  "distance_from_declared": {"type": "number"}}},
"interior_despill": {"type": "object", "required": ["interior_despill", "rule"], "properties": {
  "interior_despill": {"type": "boolean"}, "rule": {"type": "string"},
  "reference_share": {"type": "number", "minimum": 0, "maximum": 1},
  "clip_sample_frames": {"type": "array", "items": {"type": "integer", "minimum": 0}},
  "clip_median_share": {"type": "number", "minimum": 0, "maximum": 1},
  "clip_max_share": {"type": "number", "minimum": 0, "maximum": 1}}},
"params": {"type": "object", "description": "forge_matte.KeyParams.to_dict() as used"},
"local_background": {"type": "integer", "minimum": 0},
"temporal": {"type": "object", "properties": {
  "mode": {"enum": ["off", "alpha"]},
  "band": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
  "max_step": {"type": "number"}, "loop": {"type": "boolean"},
  "flips_before": {"type": "number", "minimum": 0}, "flips_after": {"type": "number", "minimum": 0}}},
"key_hued_px": {"type": "integer", "minimum": 0}
```

`$defs.character_profile_v1.properties.matte.properties` (B05/B06): add `"params": {"type": "object"}` so a
profile can pin every KeyParams field, not only key/erode/unmix/despill.

`shared/schemas/sprite.schema.json` `$defs.pipeline_meta_v2.properties` (producer B01): add
`"matte": {"type": "object"}` for the key_still info (`quality`, `requested_quality`, `key`, `key_estimate`,
`params`, `interior_despill`, `key_material_share`, `qa`).

API additions beyond Appendix A (record for Z and Wave B):

- `auto_interior_despill(reference=None, frames=(), *, key=None, sample=16) -> dict`: the clip-level rule of
  report v2 5.1, so B05 does not reimplement it.
- `STILL_KEY_PARAMS`, `KEY_QUALITIES`, `DESPILL_MODES`, `KEY_MATERIAL_SHARE_MAX`, `KEY_TOLERANCE`,
  `OVERLAP_DOMINANCE`, `LOCAL_BACKGROUND_RADIUS`.
- `soft_matte(..., protect=None)` and `dominance_matte(..., protect=None)`; `despill(..., *, key="magenta")`.
- `despill` returns `(rgba, report)`; `report["applied"]` is the mode actually used (auto -> all or edge).
- `key_material_share(rgb, key=None, *, min_excess=8.0, subject_distance=64.0)`.
- `temporal_alpha_hysteresis(..., *, max_step=0.25)`; `flip_count(frames, *, delta=0.25, raw=None, raw_tolerance=20)`.
- `matte_qa(rgba, key, *, key_tolerance=48, spill_threshold=20, min_pocket_area=16)`, with extra keys
  `key`, `key_hued_px`, `visible_px`, `outer_ring_px`, `thresholds`, `method`.
- `protect_design_colours(rgb, key, *, params=KeyParams(), margin=8.0) -> (pixels, report)`;
  `complement_cleanup(rgba, hue_band=(32, 120), *, min_saturation=54, min_alpha=8, strength=0.94,
  tint=(1.035, 0.998, 0.93)) -> (rgba, changed_px)`; `remove_enclosed_pockets -> (rgba, pockets_removed)`.

Semantics Wave B relies on:

- Keys are `magenta|green|blue`, `#rrggbb` or an RGB triple. A key's dominance is
  `min(key channels) - max(other channels)`, which is exactly `min(R, B) - G` for magenta. Named keys
  passed to `soft_matte`, `key_material_share` and `key_still` are estimated from the border ring; explicit
  colours are used as given. `remove_enclosed_pockets` and `matte_qa` take the key colour the matte used.
- `soft_matte` with a magenta key and `local_background=False` is the frozen v13 prototype bit for bit.
  RGBA input keeps its own alpha as an upper bound. Output is straight-alpha RGBA with RGB 0 under alpha 0.
- Every legacy function returns a new image and never mutates its argument. The cfed170 originals did.
- `temporal_alpha_hysteresis` returns frames in their input form. Alpha only moves within its band while
  the coverage state holds, and alpha 0 is never raised.

## 6. Shared-helper promotion requests

- forge_core (A1 / integration): `forge_matte._dilate(mask, radius)` (shared/forge_matte.py, "array
  helpers") duplicates the private `forge_core._dilate_square`. Promote it to a public
  `forge_core.dilate_square(mask, radius)` (clipped windows; equals Pillow MaxFilter(2r+1) on a mask), then
  drop the copy here. `forge_matte._distance_to(mask, cap)` (capped Chebyshev distance) would sit beside
  it. Tests: test_edge_despill_matches_sprite_impl covers `_dilate`.
- forge_palette (B04 / integration): `forge_matte._to_oklab` duplicates the planned
  `forge_palette.to_oklab`. forge_palette is vendored only into the sprite skill, so keep the private copy
  unless OKLab moves into forge_core.
- A0 tests: `tests/forge_testutils.py` has no loader for `shared/` modules, so test_forge_matte.py keeps a
  private `_local_load()` (A1 asked for the same `load_shared(name)`). `test_vendored_copies_match_shared`
  duplicates part of tests/test_vendored_sync.py and can go at integration.
- A0 `pytest.ini` already registers `perf` and `bench`. Until it merges, the two perf tests emit
  PytestUnknownMarkWarning. A0's `norecursedirs` keeps tests/benchmarks and tests/fixtures out of
  collection; on this branch neither file matches `test_*.py`, so neither is collected.
- A0 `shared/VENDORED.json` already lists forge_matte with these three targets.

## 7. Cross-module links that Z must add

- skills/video2dsprite/references/matte.md (B05) and pipeline.md (B08) should link forge_matte's modes,
  the auto despill rule, protect, hysteresis and the residue gate (`opaque_key_px == 0`,
  `outer_ring_spill_fraction <= 0.01`).
- skills/generate2dsprite/references/processing.md (B01): `--key-quality` (soft/hard/dominance/auto) and
  "keying claims need matte_qa numbers".
- skills/generate2dmap/references/prop-pack-contract.md (B10) and side-scroll-scenes.md (B11): despill
  radius semantics (Chebyshev px from transparency, margin 12).
- CONTRIBUTING (Z): shared/forge_matte.py is canonical and imports the forge_core beside it; vendor both.
- README requirements: forge_matte needs numpy and Pillow; scipy only speeds up labelling (identical
  results with FORGE_CORE_NO_SCIPY=1).

## 8. Known limitations and what is not proven

- One clip only (report v2 7). The soft matte was tuned and verified on one clip: Ryo (magenta, black-outlined
  cartoon, H.264 4:2:0, 960x960). Green and blue keys, outline-free art and other codecs are tested on
  synthetic scenes only.
- Light purples are keyed out at the video weight (w_chroma 0.2). Colours within t_fg of magenta in the
  weighted space, such as (180, 60, 200) and #972fbf, get alpha 0 even inside the body. v13 needs that rule
  to key enclosed dark-key haze (203 such pixels over the 145 Ryo frames). Use `protect_mask`,
  `choose_key_color` (a green key), the still parameters or `protect_design_colours`; the last moves #972fbf
  by OKLab dE 0.07, which is visible.
- Faint rims are eroded. Rim pixels under about 1/3 coverage of a dark outline look like darker-key haze and
  get alpha 0. On the synthetic outlined disk the rim-only alpha error is 0.082 at the video weight and
  0.040 at the still weight; over the rim plus 1 px either side it is 0.025 and 0.014. The acceptance test
  uses that band.
- Narrow-gap glow. A gap filled with bright key glow (for example (255, 90, 255)) stays opaque, as in v13:
  173 key-hued px per frame on Ryo. `local_background` does not fix it, because B(x) comes from the plain
  backdrop around the gap. Its measured benefit is edge colour on a drifting backdrop: error 2-4x lower,
  alpha error better on 2 of 3 synthetic backdrops. It costs about 0.35-0.45 s per 960^2 frame.
- Hysteresis trade-off (Ryo 57-87, report metric). Flips per frame pair fall from 9.6 to 3.5 (3.6 with loop),
  fringe and leak stay 0, but key-hued opaque px rise from 173 to 196 per frame, luma error from 2.82 to 3.27,
  and in-band alpha changes lag by up to one frame. `max_step` equals the flip metric's 0.25, so held steps
  cannot count as flips by construction. The plain band hold the plan names made flips worse (10.0 vs 9.6).
- `dominance_matte` is fast (0.16 s per frame) and stable (2.2 flips per pair) but leaves a faint tint on
  opaque edge pixels (Ryo visible fringe 1,779 px per frame), and any subject colour with dominance above
  20 turns partly transparent.
- Speed was measured on this Windows 11 machine (Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1).
  Soft matte: 0.33-0.40 s per 960^2 frame when idle, 0.67 s inside the bench loop. Legacy keyers 2048^2:
  0.23 s and 0.32 s warm, 0.6 s cold. The perf budgets (0.8 s, 1 s) leave about 2x headroom on CI.
- Bit-identity is machine-local. It was verified on this machine against the frozen prototype's evidence
  frames; float32 BLAS and `np.exp` may differ in the last bit on other CPUs, so the tests pin behaviour, not
  soft-matte bytes. The legacy keyers use only exact arithmetic, and their 2048^2 output hashes are pinned.
- Python 3.10, numpy 1.26 and Pillow 10.1 floors were not run (sources avoid newer APIs). Linux and macOS
  were not run. The JPEG fixture (`grok_jpeg_pink`) depends on the Pillow/libjpeg build; tests assert
  properties, not bytes.
- Deviation: the `key_material_share` subject threshold is 64, not v13's 120. v13 missed purple designs
  100-120 from magenta, such as (180, 60, 200), turned interior despill on and greyed them. Ryo's decision
  is unchanged: reference 0.000116, clip median 0.0024, on.
- Deviation: hysteresis semantics. A Schmitt coverage state on the (0.4, 0.6) band plus a 0.25 per-frame
  step limit, because holding the previous value inside the band alone did not reduce flips on Ryo.
- Deviation: the 2048^2 byte-identity test compares output hashes produced once with verbatim copies of the
  cfed170 functions. The originals take 22 s and 7 s; live oracle comparisons run on 800 + 200 + 480 small
  cases.
- Deviation: `test_purple_material_interior_bit_identical` uses dark purple (120, 40, 140) at the video
  weight, and (180, 60, 200) and #972fbf at the still weight. Light purples at the video weight are covered
  by `test_protect_keeps_design_purple`.
- Not proven: perceptual quality, held-out clips, fast motion with hysteresis, and the 5.7-flip gate on any
  other clip.
