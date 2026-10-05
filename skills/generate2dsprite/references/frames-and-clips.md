# Frames and clips

Two packaging tools turn accepted frames into game-ready bundles. Neither generates, resizes or re-poses art.

Run commands from the user's project root. `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Every command writes a **new** `--output-dir` inside the project. Work happens in a stage beside it, and the stage is published only after every check passes. A failure leaves nothing behind, and an existing directory is never replaced.

| You have | Tool | Result |
|---|---|---|
| Registered sprite frames on one canvas with one shared root: processed sheets, scaled frames, or code-art frames | `build_animation_clips.py` | Clips with exact timing, events and transitions; byte-copied frames; verified previews; review sheets |
| Complete rectangular frames: scene loops, full-frame phases, a sheet of whole images, or ambient FX | `assemble_frames.py` | Lossless frames, an atlas, a timed WebP and loop diagnostics; optional keying and spill-safe slicing |

Code-art frames go straight to `build_animation_clips.py` and never through `generate2dsprite.py process`: that path resamples and re-anchors them. No pipeline-meta file is needed. Image sheets go through `process` first, then come here.

Planning poses and timing is covered in [animation-planning.md](animation-planning.md). Wiring clips into a game loop is covered in [runtime-integration.md](runtime-integration.md).

## build_animation_clips.py

```bash
python "<skill-dir>/scripts/build_animation_clips.py" --manifest output/hero/clips.json --output-dir output/hero/clips-v1
python "<skill-dir>/scripts/build_animation_clips.py" --manifest output/hero/clips.json --output-dir output/hero/clips-v2 --preview-scale 4 --preview-background checker --strict
```

Inputs must meet these rules:

- Frames are still 8-bit RGBA PNGs on one shared canvas. Each frame has both fully transparent and visible pixels.
- Indexed (palette) and 16-bit PNGs are refused. Convert them first; `assemble_frames.py` accepts them and writes RGBA.
- Paths in the manifest are relative to the manifest file.
- `anchor_px` is one shared canvas root, such as the ground point under the character. It is never a per-frame visible bottom, so per-frame anchors are refused.
- The builder does not crop, align, key or resize anything.

### Manifest v1 (still accepted unchanged)

```json
{
  "schema": "generate2dsprite.animation_clips.v1",
  "frames": ["frames/run-0.png", "frames/run-1.png", {"name": "idle", "file": "frames/idle.png"}],
  "anchor_px": [48, 86],
  "clips": {
    "run": {"frames": [0, 1], "duration_ms": [90, 110], "loop": true, "stride_world_units": 80},
    "idle": {"frames": ["idle"], "duration_ms": 400, "loop": true}
  },
  "states": {"moving": "run", "idle": "idle"}
}
```

A manifest without `schema` is v1. Every v1 clip needs `duration_ms` (one integer, or one per frame) and `loop` (true or false). Clip frames are zero-based indices or unique frame names; string frames are named after their file stem. A v1 manifest gives the same output values as before. Fields that exist only in v2 are ignored under v1, and a lint warning names them.

### Manifest v2

Set `"schema": "generate2dsprite.animation_clips.v2"` to use the optional fields of the contract (`references/schemas/sprite.schema.json`, `$defs/clips_input`).

```json
{
  "schema": "generate2dsprite.animation_clips.v2",
  "frames": ["walk-0.png", "walk-1.png", "walk-2.png", "walk-3.png", "slash-0.png", "slash-1.png", "slash-2.png", "idle.png"],
  "anchor_px": [32, 60],
  "clips": {
    "walk": {"frames": [0, 1, 2, 3], "ticks": 6, "loop_policy": "cycle", "entry_frame": 0,
             "events": [{"at": 0, "name": "step_l"}, {"at": 2, "name": "step_r"}],
             "stride_world_units": 48, "speed_ref": 120,
             "transitions": [{"to": "idle", "dissolve_ms": 80, "mode": "premultiplied"}], "role": "player"},
    "slash": {"frames": [4, 5, 6], "ticks": [30, 4, 12], "loop_policy": "oneshot",
              "events": [{"at": 0, "name": "tell"}, {"at": 1, "name": "hit", "data": {"damage": 2}}, {"at": 2, "name": "end"}],
              "keys": {"wind_start": 0, "strike": 1, "recover": 2}, "hitstop_ticks": 4, "role": "enemy"},
    "idle": {"frames": [7], "duration_ms": 400, "loop": true}
  },
  "states": {"moving": "walk", "attack": "slash", "idle": "idle"},
  "sampling": "nearest", "pixel_art": true, "art_source": "code", "body_height_px": 44,
  "shadow": {"rx": 10, "ry": 4, "opacity": 0.35}
}
```

| Field | Meaning |
|---|---|
| `ticks`, `tick_hz` | Timing in game ticks: one value, or one per frame. `tick_hz` defaults to 60. A clip uses `ticks` or `duration_ms`, not both. |
| `loop_policy` | `cycle` repeats; `pingpong` plays forward then back without repeating either end (`[0,1,2,3]` plays `0 1 2 3 2 1`); `oneshot` plays once and holds. `loop` stays valid; contradicting values are refused. |
| `events` | `{at, name, data?}` at an authored frame position. Names: `in`, `tell`, `hit`, `active_end`, `cancel`, `chain`, `impact`, `hold`, `end`, `sfx`, `step_l`, `step_r`, or `custom:<name>`. |
| `keys` | Key poses as positions: `wind_start`, `wind_peak`, `strike`, `recover`, `end`, in that order. |
| `entry_frame` | Where the clip starts when another state enters it. |
| `stride_world_units`, `stride_px_per_frame`, `cadence_ms`, `speed_ref` | Declared travel per cycle, travel per frame, step cadence and the speed the timing was authored for. All are declared, not measured from the art. |
| `transitions` | Hints `{to, entry_frame, dissolve_ms, mode}` for switching to another clip; `mode` is `premultiplied` (default) or `dither`. |
| `hitstop_ticks`, `role` | Freeze length on a hit or impact event; `player`, `enemy`, `npc`, `fx` or `prop`. |
| Top level | `sampling` (`nearest` or `linear`), `pixel_art`, `palette_ref` (a file next to the manifest, hashed), `art_source`, `placeholder`, `body_height_px`, `shadow`. |

### Timing and the tick grid

Ticks become integer milliseconds whose running total never drifts more than 0.5 ms from the tick grid. For example, three frames of 5 ticks at 60 Hz become 83, 84 and 83 ms. Each clip in the output carries `tick_grid`, which shows how the timeline lands on a 60 Hz game loop (`--tick-hz` changes the rate):

- `frame_ticks` gives how many ticks each position is shown.
- `max_drift_ms` gives the worst distance between a frame boundary and its tick.
- `cycle_aligned` says whether a loop wraps exactly on a tick.

At 60 Hz, 80 ms frames show for 5, 5, 4 and 5 ticks: uniform timing that plays unevenly. The builder warns (`uneven_ticks`); author ticks instead. A frame shorter than half a tick never shows (`vanishing_frames`).

### Output

| Path | Content |
|---|---|
| `frames/frame-NN.png` | Byte copies of the source frames (sha256 checked) |
| `clips/clip-NN.webp` | Lossless preview per clip; decoded and verified for timing, alpha and visible RGB; scaled by `--preview-scale` |
| `contact-sheet.png` | Every frame with the shared root marked |
| `review/filmstrip-NN.png` | Each played position on light, dark and checker backgrounds, with the ground line, the root and labels (`p<position> f<frame> <ms> <ticks>t`, events, `HOLD`) |
| `review/onion-NN.png` | Each position over red (previous) and blue (next) ghosts |
| `review/turn-test.png` | Each clip's entry frame above its mirror about the anchor x; the dotted yellow line is the anchor |
| `review/transition-NN-MM.png` | The A-to-B dissolve of each transition hint, drawn once per step (no opacity dip) |
| `source-manifest.json` | The manifest as given, byte for byte |
| `animation-clips.json` | `animation_clips_v2` (`$defs/animation_clips_v2`) |

Every v1 key keeps its meaning in `animation-clips.json`. Pingpong clips list the played order in `frames` and keep the authored order in `authored_frames`. Each clip also gets:

- `events_ms`: every played occurrence, as `at_ms` and `at_tick`.
- `tick_grid`, `holds` and `seam` (the loop wrap: `wrap_ratio` is the seam over the mean step).
- The resolved `keys_ms`, `entry_ms`, `hitstop_ms` and `transition_hints`.

Frame records list `near_duplicates` and the `holds` they take part in. `qa` is a QA envelope with method, `notProven` and the sha256 of every input and output.

Options: `--preview-scale N` (integer, nearest neighbour; frames stay native), `--preview-background all|light|dark|checker|#rrggbb`, `--no-reviews`, `--tick-hz`, `--near-duplicate-mae`, `--strict`.

### Lints

| Code | Meaning and fix |
|---|---|
| `telegraph_short` | An enemy's `tell` comes less than 28 ticks (60 Hz) before its `hit`. Lengthen the wind-up; see [animation-planning.md](animation-planning.md). |
| `telegraph_missing` | An enemy clip has a `hit` event but no `tell`. |
| `uneven_ticks`, `vanishing_frames` | Uniform millisecond timing lands unevenly on the tick grid, or a frame never shows. Author `ticks`. |
| `near_duplicate_hold` | Consecutive positions show near-identical frames (premultiplied mean difference at most 1.5 over the visible pixels). Merge them into one frame with the summed duration, or redraw the pose. Repeated indices you wrote yourself are reported as holds, not warned. |
| `hitstop_without_hit` | `hitstop_ticks` without a `hit` or `impact` event to freeze on. |
| `pixel_art_linear_sampling`, `unknown_key`, `v2_field_ignored` | Contradictory or ignored manifest fields. |

`--strict` turns any warning into a failure and publishes nothing. The numbers find symptoms; they never approve gait, anatomy or appeal.

## assemble_frames.py

```bash
python "<skill-dir>/scripts/assemble_frames.py" --input output/scene/f0.png output/scene/f1.png output/scene/f2.png output/scene/f3.png --duration 250 --output-dir output/scene/loop
python "<skill-dir>/scripts/assemble_frames.py" --sheet output/scene/raw-sheet.png --rows 2 --cols 2 --output-dir output/scene/frames
python "<skill-dir>/scripts/assemble_frames.py" --sheet output/fox/raw-sheet.png --rows 2 --cols 4 --slice ownership --output-dir output/fox/frames
python "<skill-dir>/scripts/assemble_frames.py" --sheet output/props/sheet.png --crop-boxes output/props/boxes.json --key chroma --output-dir output/props/frames
```

**Inputs.** Still PNGs of any mode are accepted: palette with transparency, 1, 2 and 4-bit, grey, grey with alpha, and 16-bit. They expand to 8-bit without loss; 16-bit keeps the high byte, and the conversion is recorded. 8-bit RGB stays RGB. With the default `--key none`, pixels stay exact, including RGB hidden under alpha 0. Animated PNGs and other formats are refused.

**Keying.** `--key chroma` keys a uniform backdrop with the shared keyer (`forge_matte.key_still`) before slicing:

- `--key-color auto|magenta|green|blue|#rrggbb` chooses the backdrop.
- `--key-quality auto|soft|hard|dominance` chooses the keyer. `auto` gives soft edges; with `--pixel-art` it keeps binary alpha.
- Keyed frames have RGB zeroed under alpha 0.
- QA reports opaque key residue, and warns when no key backdrop was found.

**Crop boxes.** `--crop-boxes` takes inline JSON or a file. One schema serves every tool: `common.schema.json`, `$defs/cropBoxes`, as proposed by this module. Boxes are `[left, top, right, bottom]` with exclusive right and bottom.

```json
{"schema": "forge-crop-boxes/v1", "items": [{"id": "walk-0", "box": [0, 0, 384, 512]}, {"id": "walk-1", "box": [384, 0, 768, 512]}]}
```

Two legacy forms are still read: a bare list of boxes, and a prop-pack `{"props": [{"label", "source_box"}]}` file. The `id` names the frame (`crop_id`).

**Cross-cell spill.** Before cutting a sheet that has a transparent background, the tool checks for subject components that cross their cell. A subject component is 8-connected `alpha > 16`, and it is owned by the cell that holds most of its pixels. Specks under 4 px never count. On a crossing, the run fails and names the component, its owner, the pixels over the line and the overhang. For example, frame 3 of the fox fixture has 77 px of tail in frame 2's cell, overhanging by 7 px. Then pick one fix:

- `--slice ownership` keeps every component whole in its owner's frame. Faint edge pixels join the nearest subject within `--attach-radius` px (default 6). Every frame shares one padded canvas, and frame pixel `(u, v)` is sheet pixel `(u - padding[0] + cell x0, v - padding[1] + cell y0)`. Registration therefore stays the sheet's. Ownership slicing also accepts sheets whose size does not divide by the grid, using rounded cells.
- Regenerate the sheet with wider cells (see [animation-planning.md](animation-planning.md)).
- `--allow-spill` cuts anyway and records a warning.

Opaque sheets of complete scenes skip the check.

**Loops.** `animation.json` records each adjacent step and the wrap seam. `loop_seam.wrap_ratio` is the seam over the mean step: about 1 is even, well above 1 is a pop at the wrap, and near 0 is a duplicated end frame (an unintended hold). `--sequence 0,1,2,1` sets the played order.

`--static-regions '{"door": [x0, y0, x1, y1]}'` names areas that must stay still. With `--static-region-search N`, each area's drift is measured by normalized cross-correlation within plus or minus N px. A shifted landmark reports its `[dx, dy]`; flat areas say they have no texture to register.

**Loop overlap.** `--loop-overlap K --ambient` crossfades the last K played frames into the first K with a smoothstep curve, in premultiplied colour. A loop with a pop at the wrap then drops below a wrap ratio of 1, as the Dusk plates did (3.50 to 0.73). The loop gets K fewer positions; blended frames are new pixels, and each records its sources and weight. Use it for water, fire, mist and foliage only. Never crossfade a character: blending two poses ghosts the limbs, so the tool refuses `--loop-overlap` without `--ambient`.

**Output.** The tool writes:

- `frames/frame-NN.png`, including any blended frames.
- `atlas.png`, a rectangular atlas with exact pixels.
- `animation.webp`, a lossless file with its decoded timeline verified.
- `animation.json` (`generate2dsprite.full_frames.v2`), which holds sources with their conversions, key reports, the slicing record, `spill_check`, `transitions`, `loop_seam`, `loop_overlap`, `static_regions` and a `qa` envelope.

Paths are relative to `animation.json`. A source on another drive is recorded by file name and sha256.

## Notes for every run

- Errors print `error: ...` and exit 1. Success prints one JSON line with the output folder and the metadata path.
- Both tools re-encode a preview once with every frame a keyframe if the first encode fails decoded verification. libwebp 1.6 drops the alpha flag of some animations whose first frame is an opaque block, so the re-encode is recorded as `all_keyframes`.
- Previews are review aids. Runtime engines read the PNG frames and the JSON; see [runtime-integration.md](runtime-integration.md).
