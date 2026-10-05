# B05-video-keying: video2dsprite keying - soft matte by default, pocket removal, auto despill, temporal stability, key plan, triage, matte profile, CLI hardening

Branch `asf/B05-video-keying` (from `wip/asf-upgrade-20261005` @ 3f9252d). Module output:
[skills/video2dsprite/scripts/video2dsprite.py](../skills/video2dsprite/scripts/video2dsprite.py) (rewritten keying,
new verbs `key-plan` and `triage`), [skills/video2dsprite/references/matte.md](../skills/video2dsprite/references/matte.md),
[tests/test_video2dsprite.py](../tests/test_video2dsprite.py) (the 13 engine_export copies deleted, 12 tests added) and
[tests/test_video2dsprite_matte.py](../tests/test_video2dsprite_matte.py) (30 tests plus one opt-in bench).

## 1. CLIs

Run from the user's project root; `<skill-dir>` is `${CLAUDE_SKILL_DIR}` in Claude Code.

    python "<skill-dir>/scripts/video2dsprite.py" key-plan --master art/hero-master.png
    python "<skill-dir>/scripts/video2dsprite.py" triage --video raw/hero-idle.mp4 --output-dir work/hero-idle-triage
    python "<skill-dir>/scripts/video2dsprite.py" process --video raw/hero-idle.mp4 --output-dir work/hero-idle --start 1 --duration 2 --reference art/hero-master.png
    python "<skill-dir>/scripts/video2dsprite.py" extract --video raw/hero-idle.mp4 --output-dir work/hero-idle-raw --start 1 --duration 2
    python "<skill-dir>/scripts/video2dsprite.py" clean --raw-dir work/hero-idle-raw --output-dir work/hero-idle-clean --matte-profile art/hero-profile.json
    python "<skill-dir>/scripts/video2dsprite.py" sample --clean-dir work/hero-idle-clean --output-dir work/hero-idle-sample --frame-counts 12,24 --playback-duration 2
    python "<skill-dir>/scripts/video2dsprite.py" clean --raw-dir work/hero-idle-raw --output-dir work/hero-idle-legacy --matte binary --despill-mode off
    python "<skill-dir>/scripts/video2dsprite.py" doctor

- Every verb that writes files takes a new `--output-dir` (`--out-dir` is accepted as the old name): an existing
  directory is refused, work happens in `forge_core.staged_output()` beside it, and it is published only after the run
  and its QA succeed. `--strict` (clean, process, triage) turns a QA failure into exit 1 with nothing published;
  `key-plan --strict` exits 1 when every candidate key fights the master.
- Keying flags of `clean` and `process`: `--matte soft|dominance|binary` (default soft), `--key auto|magenta|green|blue|#rrggbb`,
  `--key-mode auto|always|magenta|none`, `--despill-mode auto|edge|all|off`, `--despill-radius N`, `--pockets auto|remove|keep`,
  `--protect-color HEX` (repeatable) and `--protect-tol`, `--erode N`, `--unmix/--no-unmix` (soft only),
  `--temporal-stability auto|off|alpha`, `--local-background [PX]`, `--matte-profile FILE`, `--reference FILE`, `--strict`;
  `--dist` and `--despill` (strength) belong to `--matte binary` and are refused with other mattes.
- `extract`/`process`: `--fps 0|N|N/D` (0 = every frame), `--start`, `--duration` (frames timed in [start, start + duration)),
  `--alpha auto|on|off`; `--decoder libvpx-vp9` stays as the old spelling of `--alpha auto`.
- `--help` of the tool and of every verb exits 0 with ASCII output under cp1252 and cp950 (tested for the tool under
  both and every verb under cp1252 in `test_help_and_extract_under_cp1252_and_cp950`; all 18 combinations checked by hand).
- Success prints one ASCII JSON line on stdout; progress and warnings go to stderr. Summary keys: `output` (always),
  `metadata` (`extract`: null; `clean`: `matte-report.json`; `sample` and `process`: `pipeline-meta.json`; `triage`:
  `raw-triage.json` plus `sheet`), `process` adds `outputs` (frames_raw, frames_clean, matte_report, sprite, readme),
  `frames`, `matte` (mode, status, key, opaque_key_px, enclosed_key_pockets, flips_per_frame_pair), `sets` and
  `key_seconds_per_frame`. `key-plan` prints the plan itself; `doctor` prints `forge_av.ffmpeg_info()` plus the old
  `webm`/`packed`/`png` keys (now functional results). `package` is unchanged apart from the `--output-dir` alias.
- `process` layout: `frames-raw/frame_000000.png...`, `frames-clean/clean_0000.png...` plus `frames-clean/matte-report.json`,
  `sprite/`, `pipeline-meta.json` (its `matte` block is the matte report without the file lists), `README.txt`.

## 2. SKILL.md routing rows

video2dsprite:

| Need | Route |
|---|---|
| Choose the chroma key before generating | `scripts/video2dsprite.py key-plan --master <master.png>`; paste `background_sentence` into the video prompt; protect or recolour `design_colours_at_risk` |
| Check a raw clip before processing | `scripts/video2dsprite.py triage --video <clip> --output-dir <new dir>`; a border touch inside the action means regenerate the clip, never pad it |
| Key a clip | `scripts/video2dsprite.py process --video <clip> --output-dir <new dir> --reference <master.png>`; then read `frames-clean/matte-report.json` (status, checks, warnings) and look at the frames over light and dark backgrounds |
| Key frames you already decoded | `scripts/video2dsprite.py clean --raw-dir <frames> --output-dir <new dir>` |
| Keep keying identical across a character's clips | `--matte-profile <character-profile.json>` (pins mode, key, erode, unmix, despill); deviating flags warn |
| A design colour near the key disappears | `--protect-color #rrggbb`, or regenerate on the key `key-plan` recommends |
| Fail a CI run on key residue | add `--strict` |
| Reproduce cfed170 keying | `--matte binary --despill-mode off` |

Processing-notes bullets: soft matte is the default (enclosed key holes are background, edges are soft and un-mixed,
interior despill is decided once per clip, alpha hysteresis damps flicker); plan the key before generation; triage
before processing; every verb needs a new `--output-dir`; keying claims quote the matte report's numbers and stay
`needs-visual-review`. Details: references/matte.md.

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `video2dsprite.py key-plan` | Picks the chroma key a master does not fight and writes the prompt's background sentence | `tests/test_video2dsprite_matte.py::test_key_plan_rejects_magenta_for_pink_master` |
| `video2dsprite.py triage` | Reports size, fps, frames, audio/cover-art streams, border-touching frames and key drift of a raw clip, with an overview sheet | `tests/test_video2dsprite.py::test_triage_flags_border_frames_and_audio` |
| `video2dsprite.py clean` / `process` | Soft-matte keying with pocket removal, auto despill, protected colours, alpha hysteresis and a hash-bound matte report | `test_ring_hole_keyed`, `test_purple_costume_kept`, `test_binary_matches_cfed170`, `test_meta_matte_block_validates`; opt-in bench on the Ryo clip |

## 4. CHANGELOG entries

- Added: soft matte keying for `clean` and `process` (`--matte soft|dominance|binary`), `--key auto|magenta|green|blue|#hex`
  with per-frame border estimates and a clip-median fallback, enclosed-pocket removal, `--despill-mode auto|edge|all|off`
  with the clip-level auto rule (forge_matte.auto_interior_despill; `process` samples the whole source clip when it
  trims), `--protect-color`, `--reference` (names design colours the video matte would key out), `--strict`, and
  `matte-report.json` (video2dsprite.matte_report.v1, a QA envelope) mirrored as pipeline-meta's `matte` block (B05-T2).
- Added: `--temporal-stability auto|off|alpha` (streaming alpha hysteresis) and `--local-background`; flips per frame
  pair before and after go into the report (B05-T3).
- Added: `triage` verb writing raw-triage.json (video2dsprite.raw_triage.v1) and triage-sheet.png (B05-T5).
- Added: `key-plan --master` and `--matte-profile` (character_profile.v1 matte pins with deviation warnings) (B05-T6).
- Added: exact per-frame preview GIF delays (`gif_durations_ms`, forge_core.frame_durations over centiseconds) (B05-T7).
- Added: references/matte.md (B05-T8).
- Changed: `extract`/`process` decode through forge_av: input-side trimming, WebM alpha kept automatically (libvpx),
  `--alpha`, rational `--fps`; `doctor` reports forge_av's functional probe (B05-T4, A5).
- Changed: console output is ASCII (`->`), `utf8_stdio()` runs first, subprocesses decode UTF-8 with replacement; stdout
  is one JSON summary line and progress moved to stderr; `process` prints its metadata path and output list (B05-T4).
- Changed: pipeline-meta.json stores output-relative POSIX paths, `video` as a fileRef (name plus sha256 when on another
  drive), `probe`, `extract` and `outputs` blocks; the absolute `video`/`out_dir` strings and the `chroma_dist`/`despill`
  keys are gone (the matte block supersedes them).
- Changed: the 13 engine_export tests live only in tests/test_engine_export.py (B05-T1).
- BREAKING: `--matte soft` with pocket removal, `--despill-mode auto` and alpha hysteresis are the defaults; legacy switch
  `--matte binary --despill-mode off` (plan Appendix H, row "video process").
- BREAKING: every writing verb refuses an existing `--output-dir`/`--out-dir` and publishes atomically; there are no
  in-place reruns (legacy: choose a new folder, as for the prop pack row).
- BREAKING: raw frames are `frame_000000.png` (0-based, six digits), not `frame_0001.png` (A5's change, adopted).
- BREAKING: `--dist` and `--despill` strength need `--matte binary`.
- Fixed: report v2 P0-1 enclosed key pockets (16 of 145 Ryo frames shipped holes; now 0 of 145).
- Fixed: report v2 P0-2 purple fringe (8,504 visible fringe px per frame on Ryo 57-87; now 0) and binary-only alpha.
- Fixed: report v2 P1-1 and 3.4: `--help` exited 1 under cp1252 and `extract` failed after writing frames on the arrow.
- Fixed: report v2 P1-7: the metadata file name had to be guessed; `process` now prints it.
- Fixed: report v2 P2-1 matte flicker: 10.1 flips per frame pair become 3.6 on Ryo 57-87 (report metric 3.6).
- Fixed: report v2 3.4/3.5: the per-pixel Python BFS (video2dsprite.py:125-148 at cfed170) is gone; Grok's audio track and
  cover art are reported by triage and dropped by extract.
- Fixed: video2dsprite.py:386 at cfed170 rounded one GIF delay for all frames; delays now sum exactly.

## 5. Schema change requests

None blocks this module: every document B05 writes validates against the frozen schemas (tests:
`test_meta_matte_block_validates`, `test_process_meta_matte_block_validates`, `test_triage_flags_border_frames_and_audio`).
Optional documentation of the extra fields producers now write (objects are open), for integration to add.

`shared/schemas/video.schema.json`, `$defs.matte_report_v1.properties` (producer B05, consumer B08):

```json
"key_request": {"type": "string", "description": "--key as given: auto, magenta, green, blue or #rrggbb"},
"key_declared": {"type": "string", "description": "the declared key the clip was keyed against (auto resolved)"},
"key_mode": {"enum": ["auto", "always", "magenta", "none"]},
"despill_applied": {"enum": ["off", "edge", "all"]},
"despill_radius": {"type": "integer", "minimum": 0},
"pockets": {"enum": ["remove", "keep"]},
"erode": {"type": "integer", "minimum": 0},
"unmix": {"type": "boolean"},
"frames_total": {"type": "integer", "minimum": 1},
"passthrough_frames": {"type": "array", "items": {"type": "integer", "minimum": 0}},
"warnings": {"type": "array", "items": {"type": "string"}},
"legacy": {"type": "object", "properties": {"dist": {"type": "number", "minimum": 0},
  "despill": {"type": "number", "minimum": 0, "maximum": 1}}},
"protect": {"type": "object", "properties": {"colors": {"type": "array", "items": {"$ref": "common.schema.json#/$defs/hexColor"}},
  "tol": {"type": "number", "exclusiveMinimum": 0}, "px": {"type": "integer", "minimum": 0}}},
"design_colours_at_risk": {"type": "array", "items": {"type": "object", "properties": {
  "from": {"$ref": "common.schema.json#/$defs/hexColor"}, "to": {"$ref": "common.schema.json#/$defs/hexColor"},
  "px": {"type": "integer", "minimum": 0}, "delta_e": {"type": "number", "minimum": 0}}}},
"profile": {"type": "object", "properties": {"file": {"$ref": "common.schema.json#/$defs/fileRef"},
  "id": {"type": "string"}, "pinned": {"type": "object"}, "params": {"type": "object"},
  "deviations": {"type": "array", "items": {"type": "object", "required": ["option", "profile", "used"]}}}},
"report_file": {"$ref": "common.schema.json#/$defs/relPath", "description": "pipeline-meta matte block only"}
```

and inside the same `$def`: `temporal.properties` gains `"requested": {"enum": ["auto", "off", "alpha"]}`,
`"active": {"type": "boolean"}`, `"skipped": {"type": "string"}`; `interior_despill.properties` gains
`"sampled_from": {"enum": ["source clip", "keyed frames"]}` and `"pinned_by_profile": {"type": "boolean"}`;
`key_estimate.properties` gains `declared`, `declared_rgb` (rgb), `spread`, `frames_estimated`, `invalid_frames`
(integer array) and `detected` (object); `frames.items.properties` documents `index`, `key` (rgb), `passthrough`,
`opaque_key_px`, `outer_ring_spill_fraction`, `semitransparent_fraction`, `enclosed_key_pockets`, `key_hued_px`,
`visible_px`, `pockets_removed`, `protected_px`, `despill_px`. Semantics to add to the description: matte-report.json is
a full QA envelope (status, method, notProven, checks, inputs, outputs, tool); residue counts (opaque_key_px,
enclosed_key_pockets, key_hued_px) are taken before any despill pass; `mode` "none" means `--key-mode none`.

`$defs.raw_triage_v1.properties` (producer B05): `codec`, `pixFmt` (string), `duration` (number|null), `hasAlpha`
(boolean), `nativeAlphaFrames`, `attachedPics`, `nbFramesContainer` (integer >= 0), `audio` (array of
{index, codec, channels, sample_rate, duration}), `border` ({ringPx, minPx, touches[{index, px, edges{top, bottom, left,
right}}]}), `overview` ({file: relPath, frames: integer array}), `rule` (string); `keyDrift` documents `declared`,
`declaredRgb`, `estimated` (rgb), `distance`, `spread`, `framesEstimated`, `invalidFrames`, `detected`. `keyDrift` is
omitted for a clip with native alpha (the schema types it as an object, so null is not written).

`$defs.character_profile_v1.properties.matte.properties.unmix` (B05/B06): add the description "soft matte only: false
takes edge colour from the neighbouring subject (KeyParams.unmix_from 1.0); must be false for dominance and binary".

## 6. Shared-helper promotion requests

- `_LocalAlphaHysteresis` (video2dsprite.py:634): `push(alpha_uint8) -> alpha_uint8`, the streaming twin of
  `forge_matte.temporal_alpha_hysteresis` (loop off). The library holds a whole clip as float planes (about 0.5 GB per
  145 frames at 960^2, three lists with loop on); B05 keys frame by frame. Request: `forge_matte.AlphaHysteresis(band,
  max_step)` with `push()`, and the list function built on it. Test: `test_streaming_hysteresis_matches_library`.
- `_local_still_mask` and `_local_pair_flips` (video2dsprite.py:675, 680): `forge_matte.flip_count` for one pair with a
  motion mask built once per pair (uint8 max-min, `|a - b| >= 64` is exactly `> 0.25`). Counting before and after the
  hysteresis with the library cost 0.14 s per 960^2 frame; this costs about 0.02 s. Request: `flip_count(..., still=None)`
  or `forge_matte.pair_flips(a, b, still)`. Test: `test_pair_flips_match_library`.
- `_local_key_dominance` (video2dsprite.py:465): public `forge_matte.key_dominance(rgb, key)` (the private `_dominance`
  with `_key_model`), used by triage's border rule.
- `_local_erode_alpha` (video2dsprite.py:690): `forge_matte.erode_alpha(rgba, steps)`, the 4-neighbour min filter of
  game-opus55 pixelate.py `_erode`, RGB zeroed under alpha 0. Test: `test_despill_modes_unmix_and_erode`.
- `_local_portable` and `_local_file_ref` (video2dsprite.py:112, 118): `forge_core.file_ref(path, base, sha256=None)`
  returning a common fileRef whose path falls back to the bare file name when `portable_path` has no relative route
  (A1 section 5 asks every writer to do this by hand). Tests: `test_meta_matte_block_validates`, `test_process_publishes_once_with_portable_meta`.

## 7. Cross-module links that Z must add

- B08 pipeline.md (owner B08): commands must use new output folders (`--output-dir`; reruns into an existing folder
  fail); `clean --despill 0..1` now needs `--matte binary` (the default soft matte despills with `--despill-mode`);
  `extract --decoder libvpx-vp9` is the old spelling of `--alpha auto`; raw frames are `frame_000000.png`; name the
  outputs `work/pipeline-meta.json` and `work/frames-clean/matte-report.json`; link references/matte.md.
- B08 engine_export residue gate: read `matte-report.json` next to the clean frames for the key the matte used (`key`,
  and per-frame `frames[].key`), because a keyed frame has no backdrop left to estimate it from.
- B08 package flags: engine_export.py has no CLI; the `package` verb's parser is in video2dsprite.py `build_parser`
  (B05's file). Integration adds B08's new `package` flags and verbs (`--selection`, `--registration`, `--tiers`,
  `--allow-key-residue`, `verify`) there, or B08 exposes a hook that build_parser calls. `cmd_package` still passes the
  namespace to `engine_export.package(args)` with `args.out_dir`.
- B08 tests: tests/test_engine_export.py loads video2dsprite.py by path without registering it in `sys.modules`, so the
  script must not define dataclasses under postponed annotations (B05 uses NamedTuple). Switching that test to
  `forge_testutils.load_script` removes the constraint.
- B06 profiles: register_clip's `profile` should write the `matte` block that `--matte-profile` reads (mode, key, erode,
  unmix, despill, optional `params`); `params.interior_despill` pins the auto despill decision; `unmix` must be false
  for dominance or binary. prepare_i2v_input can call `key-plan` (or forge_matte.choose_key_color) for the background
  sentence.
- B07: gait_loop and animation_review read `frames-clean/clean_0000.png` onwards (unchanged names).
- Z, video SKILL.md: the rows of section 2; the current "`--despill 0.5`" and "`process --out-dir work`" guidance must
  change; "soft matte is the default; key-plan before generation; triage before processing".
- Z, README and CHANGELOG: sections 3 and 4.

## 8. Known limitations and what is not proven

- One clip. The soft matte, the 0.5% auto despill rule, the 0.01 ring-spill gate and the 5.7-flip reference come from the
  Ryo clip (magenta, black-outlined cartoon, H.264 4:2:0, 960x960, 145 frames; report v2 section 7). Green and blue keys,
  outline-free art, other codecs and fast motion are covered by synthetic tests only.
- Bench (`FORGE_BENCH_CLIP` = outputs/fresh-agent-20261005/grok-native/grok-native.mp4, `FORGE_BENCH_REFERENCE` =
  input.png; default settings; this machine with up to 16 agents running): fringe 0 and leak 0 on frames 57-87 (report v2
  metrics via tests/benchmarks/keyer_bench.py), leak 0 on all 145 frames, enclosed pockets in 0 of 145 frames, opaque key
  px 0, 3.6 flips per frame pair (report metric; the 2.0 stretch goal is not met), soft keyer 0.728 and 0.672 s per
  frame in two runs (gate 0.8). The whole `clean` pipeline (plan pass, decode, matte, pockets, QA, hysteresis, PNG
  write) took 1.27-1.28 s per frame; the keyer alone measured about 0.37 s per frame when the machine was quieter (A2
  and an early run here). No parallel keying.
- The report's flips (forge_matte.flip_count over the whole frame) differ from report v2's band-restricted metric: on Ryo
  57-87 the report says 10.07 before and 3.6 after, the band metric 3.6 after. An eroded silhouette that moves counts as
  flips in either metric, so the flips check only warns.
- Inherited from forge_matte (A2 section 8): light colours within the video matte's reach of the key (#b43cc8, #972fbf
  against magenta) are keyed out unless protected; narrow gaps of bright key glow stay opaque (about 170 key-hued px per
  Ryo frame); rims under about a third coverage of a dark outline are eroded.
- The auto despill decision follows the frames it sees: `process` samples the whole source clip when it trims, `clean`
  only the frames it is given, so a trimmed window keyed with `clean` can decide differently (Ryo 57-87 alone: clip
  median 0.68%, despill edge; whole clip 0.24%, despill all). Pin it with `--despill-mode` or a profile.
- `--matte binary` equals cfed170 pixel for pixel, except that RGB hidden under alpha 0 is zeroed in every output PNG
  (plan Appendix D); that only touches frames passed through with their native alpha.
- Triage's border rule is colour-based on a 1 px ring (at least 4 subject px; Dusk used more than 3 with a chroma-contrast
  rule): key-coloured props or haze at the border can hide or fake a touch. key-plan lists at-risk colours only for an
  RGBA master; `clean`/`process --reference` also key an opaque reference first.
- Not run: Linux, macOS, Python 3.10, numpy 1.26, Pillow 10.1 (sources parse with the 3.10 grammar), ffmpeg older than 8.
- Deviations from the plan and why:
  - `--temporal-stability` defaults to `auto` (alpha for soft and dominance, off for binary): the plan names no default;
    on keeps the Ryo gate of at most 5.7 flips by default, and off for binary keeps the two-flag legacy switch exact.
  - `--key-mode always` was added; `magenta` stays as its old spelling with the magenta key.
  - `--despill` and `--dist` are refused unless `--matte binary`, instead of being ignored by the soft matte.
  - `unmix` belongs to the soft matte only (`--no-unmix` sets KeyParams.unmix_from 1.0): the dominance alpha is a 20-140
    ramp, not a coverage estimate. Worked example: a 50/50 mix of the outline (20, 24, 32) and magenta is (137, 12, 143),
    dominance alpha 0.125 instead of 0.5, and F = (C - (1 - a)K)/a clips to (0, 96, 0), a green edge.
  - Report v2 P0-2 asked for per-frame keying seconds in the matte block; files must be byte-deterministic (Appendix D),
    so timings appear only in the stdout summary (`seconds_per_frame`, `key_seconds_per_frame`).
  - Residue counts are taken before the despill pass of the dominance and binary mattes: with pockets kept, despill `all`
    turned a magenta hole into an opaque black blob that QA on the final frame could not see.
  - The `clean_frames()` Python API keeps its cfed170 in-place behaviour (binary keyer, stale frames removed) for library
    callers and also writes matte-report.json; the CLI verbs never write in place. `extract_frames()` likewise removes
    stale `frame_*.png` before calling forge_av, which refuses them.
  - `extract` writes no metadata file (its summary's `metadata` is null); provenance of a split run is in the matte
    report's hashed inputs.
