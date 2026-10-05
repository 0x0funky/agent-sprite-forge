# Asset and legacy mode selection

Infer useful outputs from the requested game instead of requiring the user to specify CLI parameters.

| Request | Suggested plan |
|---|---|
| Four-direction overworld hero | `player_sheet` (4x4: one row per facing, one walk cycle per row), or separate directional clips when more useful phases are needed. |
| Eight-direction character | One row per facing in the order you will play them; process with `--direction-order` (below). Mirror left/right rows only for a symmetric design. |
| Side-view playable hero | Separate idle, run, attack/hurt and actual airborne states; body + held equipment, wide FX separate. |
| NPC | One accepted static master; idle/walk only where gameplay needs them. Video secondary motion is optional. |
| Boss | Distinct readable master, idle and attacks with consistent scale/root; creature anatomy decides anchors and phase count. |
| Spell | Caster body action, projectile and impact as separate assets/layers. |
| Icons and HUD pieces | One `single` asset per icon at its display size, or one sheet of equal cells (`--mode sheet --rows R --cols C`, `--scale-strategy fit`). Small flat icons up to 48 px tall usually look better as codeart2d code art, which never goes through `process`. |
| Hitboxes and hurtboxes | Gameplay data, not pixels: author them per action in the game or as clip events relative to the frame origin (`output_origin` in `pipeline-meta.json`). Never draw debug boxes into the art. |
| Compact props | Individual assets or a reviewed pack; match map view and art. |
| Structural props | Map skill's dimensions, collision and join contract; avoid square bbox fitting. |
| Full scene loop | Complete rectangular frames or localized video patches through the map/video skills. |

## Existing processor names

`--target` accepts `player`, `npc`, `creature`, or generic `asset`. Use `asset` for props, spells, projectiles, impacts and other art; those planning labels are not CLI target values. `list-options` prints the modes each target accepts and their default grids; `process` rejects a mode its target does not have, and `--mode sheet` needs `--rows` and `--cols`. Explicit `--rows/--cols` override the predefined grid; action names alone do not guarantee the intended number of cells.

Legacy mappings remain supported:

- `player_sheet`: four-direction 4x4 overworld walk. Rows top to bottom face **down, left, right, up**; columns are **neutral, left foot forward, neutral, right foot forward**. Frames are named `down-1` ... `up-4`, and each row also gets `<direction>-strip.png` and `<direction>.gif`. If the generator returned another row order, pass it with `--direction-order` (for example `down,up,left,right`) instead of renaming files.
- `player_walk` / `npc_walk`: 2x2 down-facing walk with the same neutral, left, neutral, right column pattern.
- Generic `run` / `walk` and creature `walk`: legacy 2x2, four-pose defaults. They remain unchanged so existing four-frame inputs are not silently split into eight cells.
- `combat`: compact 2x2 attack + hurt; insufficient by itself for a full playable hero kit.
- `evolution`: old concept-sheet workflow.
- `single`, `player`, `npc`: one isolated static asset, normalized as a 1x1 grid into `clean.png`.

`--direction-order` works for any grid with one facing per row, top to bottom, using `down`, `down-left`, `left`, `up-left`, `up`, `up-right`, `right` and `down-right`; strips, GIFs and frame names follow it (custom grids name frames `<prefix>-<direction>-<column>`).

Bundle labels describe planning, not automatic generation commands: `single_asset`, `unit_bundle`, `combat_bundle`, `spell_bundle`, `hero_action_bundle`, `line_bundle`, `engine_atlas`. Generate only requested/useful assets. A final engine atlas is assembled after individual actions pass review.

## Pose counts by camera and action

Starting points, not requirements:

| Camera and action | Useful poses |
|---|---|
| Top-down walk, per direction | 3-4 (neutral, left foot, neutral, right foot) |
| Side-view walk or run | 6-10 |
| Idle (breathing, weight shift) | 4-6 |
| Attack (wind-up, strike, follow-through, recover) | 4-8 |
| Hurt | 2-4 |
| Cast | 4-6 |
| Death or knock-out | 4-6 |

`build-prompt` and `process` print an advisory when a locomotion layout supplies fewer than eight pose cells (per direction for `player_sheet`), without changing or rejecting the input; it targets side-view travel, so a classic four-column top-down walk triggers it too. `--intentional-low-frame-count` records a deliberate sparse choice in the JSON metadata and silences the advisory.

A restrained four-pose idle can use 2x2 and a six-phase attack/cast 2x3. Eight poses suit 2x4; twelve suit 3x4; a 3x3 plan can use eight run poses plus a separately indexed idle. A run need not play a spare idle cell. Multi-row sheets often improve actor containment; one-row strips remain valid when requested or proven. More cells reduce source detail per frame, and more frames do not repair unclear poses or bad timing.

`build-prompt` still emits the selected legacy layout. To generate a new 8-12-pose action, write its phase/grid contract directly using [character-animation.md](character-animation.md), then extract the **measured** layout; `process --rows 2 --cols 4` only describes an existing eight-cell input and never creates missing drawings. Prefer fixed-cell extraction and clip packaging for already registered run motion (`--scale-strategy registered` keeps drawn flight and bob).

Review actual contact, passing, compression/flight where appropriate, opposite-leg identity and the last-to-first seam. If a nominal eight-frame sheet repeats a four-pose half-cycle or changes costume/head position, record the observed defect; do not approve it from cell count alone or duplicate frames to satisfy a number.

Use all intended components for FX and pixel art. Use largest-component cleanup only when disconnected pixels are unwanted; it can remove an intentionally separate sword, hand, ornament or spark. Preserve shared registration for jump/run flight and effects. See [processing.md](processing.md) before choosing feet normalization, preserve or registered scaling, or QC limits.
