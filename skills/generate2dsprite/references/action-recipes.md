# Action recipes

Phase lists, grid shapes and project presets for sprite briefs. Combine them with [prompt-rules.md](prompt-rules.md) (prompt structure, NEAR/FAR limbs, palette and containment) and [character-animation.md](character-animation.md). Phase names are an animation brief, not proof that the model drew good motion: check every sheet with `sheet_qc.py` and look at the frames at game size.

## Choosing the grid

| Action | Grid | Phases, in reading order |
|---|---|---|
| Idle, standard actor | 2x2 | neutral, inhale or settle, accent, return |
| Idle, large creature or showcase | 3x3 | 9 phases of one loop; subject fills about 55-65% of each cell |
| Walk, side view | 2x4 | contact, down, passing, up; then the same with NEAR and FAR swapped |
| Run, side view | 2x4 | contact, down, passing, flight; then the same with NEAR and FAR swapped |
| Run prototype plus idle | 3x3 | cells 0-7 the run, cell 8 a neutral idle (separate clip) |
| Attack | 2x2 or 2x3 | wind-up, strike, follow-through, recovery (2x3 adds anticipation and ready) |
| Cast | 2x3 | readiness, energy gather, stronger gather, release start, release peak, settle |
| Hurt | 2x2 | impact, recoil, stagger, recovery |
| Combat, compact enemy | 2x2 | top-left attack wind-up, top-right strike, bottom-left hurt impact, bottom-right hurt recovery |
| Projectile | 2x2, or 1x4 when long and narrow | same projectile, small shape pulse, constant travel direction |
| Impact or explosion | 2x2 | ignition or contact, expansion, peak burst, fade or collapse |
| Player, four directions | 4x4 | rows down, left, right, up; columns neutral, step, neutral, step |
| Long single action | 4x4 | 16 phases of one action (summon, charge, transformation, death), anticipation to settle |

`3x3` is a packing choice, not a smoothness setting: nine nearly identical poses still look stiff, and four well-spaced poses can suit a restrained style. Choose useful phases first, then the grid.

## Recipes

### Idle

Neutral stance with subtle motion: a weight shift or breathing, secondary hair and cloth motion, the strongest accent just before the loop returns. A massive grounded boss keeps both feet and the pelvis fixed: compress the torso, pulse an inner core, settle the shoulders and let attached ornaments follow a beat later; forbid whole-body sideways translation.

### Walk, run, hover

State the travel behaviour: grounded stride, hover bob, crawl, slither or mechanical glide. For a side-view biped give each phase with NEAR and FAR limbs ([prompt-rules.md](prompt-rules.md)):

- **Run, 8 frames.** 0 contact: NEAR leg reaches forward, heel on the ground line; FAR leg trails, foot lifted. 1 down: NEAR leg under the body, knee bent, carrying the weight; FAR leg folds up behind. 2 passing: NEAR leg pushes off behind; FAR knee swings forward past it. 3 flight: both feet off the ground; FAR leg reaches forward for the next contact. 4-7: the same with NEAR and FAR swapped. Frames 4-7 must not repeat frames 0-3.
- **Walk, 8 frames.** Contact, down (weight settles over the planted leg), passing (free leg passes under the body) and up (body at its highest, free foot just clear of the ground), then the same with NEAR and FAR swapped. A walk always keeps one foot down; never draw flight.
- Arms counter the legs; costume motion stays restrained. Keep planned vertical body motion: a fixed root does not mean every lowest pixel sits on one line.
- A run prototype may place 8 run phases plus one neutral idle in a 3x3, with the run playing cells 0-7 only. This exception does not extend to unrelated attacks, jumps, damage or death.

### Attack

Wind-up, strike, follow-through, recovery. For playable heroes the body is generated alone: no slash arc, weapon trail, muzzle flash, projectile, impact burst or dust; the weapon stays close enough that the body bbox stays near idle and run size, and the feet stay on the shared root. Generate the effect separately. For long quadrupeds, serpents and wide tails, lock one shared silhouette envelope: fixed torso centre, the central 70-72% of the cell, the tail tucked in, and attack energy shown through compression, neck or limb extension and recoil instead of travel across the cell.

### Cast

A 2x3 is a good default: readiness, energy gather, stronger gather, release start, release peak, settle or hold. Keep the gathered energy compact and inside the cell; large circles and portals belong in an effect sheet.

### Hurt

Impact, recoil, stagger, recovery. Keep the feet planted for a grounded hurt; use a motion-relative envelope for knockback.

### Combat, compact enemy sheet

A 2x2 for low-stakes enemies: top-left attack wind-up, top-right attack strike, bottom-left hurt impact, bottom-right hurt recovery. Do not use combined sheets for controllable heroes.

### Projectile

Prefer 2x2 for a compact loop; use 1x4 for a long, narrow projectile when that cell shape fits the whole motion. Same projectile identity in every frame, constant travel direction, small loopable shape changes, glow or trail inside the frame.

### Impact and explosion

Usually 2x2: ignition or contact, expansion, peak burst, fade or collapse. A ground-contact loop such as fire keeps one horizontal ignition baseline at a fixed share of every cell's height; only the tips deform. For a billboarded overlay, forbid baked ground, lava pools, tile plates and shadows.

### Four-direction player sheet

A 4x4 with rows down, left, right, up and columns neutral, step, neutral, step. In the down and up rows both feet are visible: column 2 is the left foot forward and column 4 the right foot forward. In the left and right rows (side views) use depth instead: column 2 has the NEAR foot forward and column 4 the FAR foot forward. Try it without a layout guide first; a guide tends to centre directional poses and weaken the stride.

### Large 3x3 idle

Exactly 9 equal cells, one anatomical scale and one reserved motion envelope; the subject fills about 55-65% of each cell; nothing crosses an edge. Use a layout guide only when an earlier 3x3 drifted in spacing or scale.

### Long single action, 4x4

For casting, summoning, charging, transformation and death: 16 equal cells read left to right and row by row, each phase in order from anticipation through the peak to the settle or loop return. Keep identity stable while pose, energy and compact attached effects change. Not a shortcut for four unrelated actions.

### Mixed actions and atlases

Never pack unrelated actions into one raw sheet because the engine wants a 4x4 or 5x5 atlas ("row 1 idle, row 2 run, row 3 shoot, row 4 jump"). Generate each action with its own phase plan and checks, process each one, then assemble the atlas. Allowed raw multi-row sheets: one locomotion family in four directions, one long action, the 8-run-plus-idle prototype with separate clips, prop packs and tile-like atlases, and compact low-stakes enemy combat sheets. Reject a body action whose body is more than 10-15% smaller than idle because a wide pose or effect forced it to shrink; reprocess it with the idle's scale profile instead.

### Character anchor sheets

For high-value grounded actions that keep drifting in scale or feet placement, accept a neutral master, repeat it into every cell with `make_anchor_layout.py`, attach both images and write:

```text
Image 1 is the exact character identity and art reference.
Image 2 is a scale-and-root template made from the same accepted character.
Keep Image 2's cell locations, camera distance, anatomical scale, root and padding. Contact poses use the
shared ground line; planned compression, recoil or flight may move the body within the cell. Change only the
pose in each cell. Never zoom or resize a pose to fill its cell.
```

Do not use a grounded anchor sheet for jumps, falls, knockback, flying actors, projectiles, impacts or effects that change scale on purpose.

### Layout guides: where they fit

- Good fit: 3x3 and 4x4 prop packs, tile-like atlases, fixed atlas rows and 16-frame single actions (cast, summon, charge, death, transformation).
- Possible fit: 3x3 large idles when earlier results drifted in scale, spacing or edge safety; run and walk sheets that kept crossing cells or repeating the leading leg (`plan_guide.py`, opt-in).
- Risky fit: four-direction walk sheets, where guide pressure centres the poses and weakens the stride.

### Bundles

Write each asset's prompt on its own. Good splits: caster unit, projectile, impact; or idle, combat, walk. Do not force unrelated assets into one giant sheet.

### Quick prompt pattern

1. The asset type and the generation strategy: grid shape, or one complete frame.
2. The identity, and the role of each reference with its invariants.
3. The logical canvas, scale and palette.
4. The motion, frame by frame, with NEAR/FAR limbs for side views.
5. Same scale, same root and containment.
6. The background contract and "no text, labels, grid lines or checkerboard".

## Project presets

Each preset is a starting brief for one kind of game. Copy it into the project's notes, then adjust the numbers to the real game. Canvases are logical game pixels; `render_scale` is how much larger the image model draws them. Timing is in 60 Hz game ticks (5 ticks = 83 ms, 12 ticks = 200 ms), the unit `scale_frames.py --emit-clips --ticks` writes, so every frame shows for a whole number of updates.

```yaml
jrpg_walker:
  use: overworld hero or NPC that walks in four directions on a tile map
  camera: three-quarter top-down, slightly above; feet on one shared root
  logical_canvas: [32, 48]
  body_height_px: 40
  render_scale: 8
  palette: {max_colors: 16, outline: "1 logical px dark outline", file: palette.json}
  light: upper front-left, three tones per material
  identity: accept a front, side and back turnaround master before the walk sheet
  directions: [down, left, right, up]
  actions:
    walk: {grid: 4x4, rows: "down, left, right, up", columns: "neutral, step, neutral, step", side_rows: "NEAR foot forward, then FAR foot forward", ticks: 7, loop: true}
    idle: {grid: 2x2, ticks: 12, loop: true}
  pipeline:
    - "sheet_qc.py spill --rows 4 --cols 4"
    - "sheet_qc.py frames on each direction row (--frames-per-row 4)"
    - "scale_frames.py --scale-from 1/8 --resampler nearest --anchor feet --row-baseline --emit-clips --ticks 7"
    - "build_animation_clips.py"
  checks: [no part crosses a cell line, one scale for every direction, head ratio within 5 percent]
```

```yaml
platform_hero:
  use: side-scroller hero with idle, run, jump and attack
  camera: side elevation facing right; the runtime mirrors it for left
  logical_canvas: [48, 64]
  body_height_px: 44
  render_scale: 8
  palette: {max_colors: 16, outline: "1 logical px dark outline", near_far: "FAR limbs one shade darker"}
  light: upper front, three tones per material
  anchor: stance, so a mirrored turn keeps the feet in place
  actions:
    idle: {grid: 2x2, ticks: [10, 10, 10, 13], loop: true}
    run: {grid: 2x4, phases: [contact, down, passing, flight, contact, down, passing, flight], lead: "NEAR leg leads frames 0-3, FAR leg leads frames 4-7", ticks: 5, loop: true}
    jump: {grid: 1x4, phases: [rise, apex, fall, land], loop: false, registration: "shared source root; no feet lock"}
    attack: {grid: 2x3, phases: [anticipation, wind-up, strike, follow-through, recovery, ready], body_only: true, loop: false}
  pipeline:
    - "plan_guide.py --frames 8 --cycle run (opt-in, when sheets keep crossing cells)"
    - "sheet_qc.py spill, then sheet_qc.py frames --cycle run --game-pixel 8"
    - "scale_frames.py --scale-from 1/8 --resampler nearest --root-lock torso-x --row-baseline --emit-clips --ticks 5"
    - "later actions reuse the run's scale-frames.json with --profile and grow the canvas with --action-padding"
    - "build_animation_clips.py"
  checks: [leading leg alternates, torso drift under 2 game px, attack body within 10 percent of idle size]
```

```yaml
iso_tactics_unit:
  use: unit for a 2:1 isometric tactics map
  camera: 2:1 dimetric isometric (ground lines rise 1 px per 2 px, 26.57 degrees), no perspective
  logical_canvas: [32, 48]
  footprint: {tile: [32, 16], root: centre of the diamond the unit stands on}
  render_scale: 8
  palette: {max_colors: 16, outline: "selective: darker shade of the local colour"}
  light: one fixed screen direction (upper left) for every facing
  directions: [south-east, south-west, north-east, north-west]
  actions:
    idle: {grid: 2x2, ticks: 13, loop: true}
    walk: {grid: 2x2, per_direction: true, ticks: 8, loop: true}
    attack: {grid: 2x3, body_only: true, loop: false}
    hit: {grid: 2x2, phases: [impact, recoil, stagger, recovery], loop: false}
  pipeline:
    - "turnaround master in the four diagonal facings first"
    - "sheet_qc.py spill per action sheet"
    - "scale_frames.py --scale-from 1/8 --resampler nearest --anchor feet --lock feet (grounded actions)"
    - "build_animation_clips.py"
  checks: [root on the diamond centre in every frame, same light direction in every facing]
```

```yaml
td_tower:
  use: tower-defense tower with idle, shoot and upgrade levels
  camera: three-quarter top-down; the base footprint never moves
  logical_canvas: [48, 64]
  footprint: {base_rows: 16, root: centre of the base}
  render_scale: 8
  palette: {max_colors: 16, outline: "1 logical px dark outline", team_color: one reserved accent}
  light: upper front-left, three tones per material
  actions:
    idle: {grid: 2x2, motion: "rooted; only flags, crystals or lights move", ticks: 11, loop: true}
    shoot: {grid: 2x2, phases: [ready, recoil, fire peak, settle], body_only: true, effects: "projectile and muzzle flash as separate fx sheets", loop: false}
    upgrades: {grid: 1x3, phases: [level 1, level 2, level 3], rule: "same base footprint, root and palette; only the top grows"}
  pipeline:
    - "sheet_qc.py spill per sheet"
    - "scale_frames.py --scale-from 1/8 --resampler nearest --anchor bbox --lock feet"
    - "build_animation_clips.py"
  checks: [base footprint identical across levels, effects never baked into the tower body]
```
