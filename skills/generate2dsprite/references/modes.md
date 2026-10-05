# Asset and legacy mode selection

Infer useful outputs from the requested game instead of requiring the user to specify CLI parameters.

| Request | Suggested plan |
|---|---|
| Four-direction overworld hero | `player_sheet` (4x4 canonical walk family), or separate directional clips when more useful phases are needed. |
| Side-view playable hero | Separate idle, run, attack/hurt and actual airborne states; body + held equipment, wide FX separate. |
| NPC | One accepted static master; idle/walk only where gameplay needs them. Video secondary motion is optional. |
| Boss | Distinct readable master, idle and attacks with consistent scale/root; creature anatomy decides anchors and phase count. |
| Spell | Caster body action, projectile and impact as separate assets/layers. |
| Compact props | Individual assets or a reviewed pack; match map view and art. |
| Structural props | Map skill's dimensions, collision and join contract; avoid square bbox fitting. |
| Full scene loop | Complete rectangular frames or localized video patches through the map/video skills. |

## Existing processor names

`--target` accepts `player`, `npc`, `creature`, or generic `asset`. Use `asset` for props, spells, projectiles, impacts and other art; those planning labels are not CLI target values. Use `list-options` for the actual installed modes/default grid mappings. Explicit `--rows/--cols` override the predefined grid; action names alone do not guarantee the intended number of cells.

Legacy mappings remain supported:

- `player_sheet`: four-direction 4x4 overworld walk.
- `player_walk` / `npc_walk`: 2x2 down-facing walk.
- Generic `run` / `walk` and creature `walk`: legacy 2x2, four-pose defaults. They remain unchanged so existing four-frame inputs are not silently split into eight cells.
- `combat`: compact 2x2 attack + hurt; insufficient by itself for a full playable hero kit.
- `evolution`: old concept-sheet workflow.
- `single`: isolated static asset normalization.

Bundle labels describe planning, not automatic generation commands: `single_asset`, `unit_bundle`, `combat_bundle`, `spell_bundle`, `hero_action_bundle`, `line_bundle`, `engine_atlas`. Generate only requested/useful assets. A final engine atlas is assembled after individual actions pass review.

## Starting points, not requirements

A restrained four-pose idle can use 2x2 and a six-phase attack/cast 2x3. For newly planned run/walk actions, start with **8–12 useful poses per action or direction** unless the user intends a deliberately sparse style. This is a planning recommendation, not a smoothness guarantee or a minimum for accepting existing art. Eight poses suit 2x4; twelve suit 3x4; a 3x3 plan can use eight run poses plus a separately indexed idle. A run need not play a spare idle cell. Multi-row sheets often improve actor containment; one-row strips remain valid when requested or proven. More cells reduce source detail per frame, and more frames do not repair unclear poses or bad timing.

`build-prompt` still emits the selected legacy layout. `build-prompt` and `process` warn when a locomotion layout supplies fewer than eight pose cells (per direction for `player_sheet`), without changing or rejecting the input. `--intentional-low-frame-count` records a deliberate sparse style in the JSON metadata and suppresses that advisory. To generate a new 8–12-pose action, write its phase/grid contract directly using [character-animation.md](character-animation.md), then extract the **measured** layout; `process --rows 2 --cols 4` only describes an existing eight-cell input and never creates missing drawings. Prefer fixed-cell extraction and clip packaging for already registered run motion.

Review actual contact, passing, compression/flight where appropriate, opposite-leg identity and the last-to-first seam. If a nominal eight-frame sheet repeats a four-pose half-cycle or changes costume/head position, record the observed defect; do not approve it from cell count alone or duplicate frames to satisfy a number.

Use all intended components for FX. Use largest-component cleanup only when disconnected pixels are unwanted; it can remove an intentionally separate sword, hand or ornament. Preserve shared registration for jump/run flight and effects. See [processing.md](processing.md) before choosing feet normalization, preserve scaling or QC limits.
