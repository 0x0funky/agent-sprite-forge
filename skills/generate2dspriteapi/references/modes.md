# Modes

Use this file when the user's wording leaves room for multiple valid asset plans.

## Asset Types

- `player`: controllable overworld hero
- `npc`: role-readable town or field character
- `creature`: monster, beast, spirit, boss, summon
- `character`: side-view or non-overworld humanoid unit that is not specifically a player or NPC
- `spell`: castable magic or skill sequence
- `projectile`: loopable traveling object such as orb, arrow, fireball, bullet, beam segment
- `impact`: hit burst, explosion, contact FX
- `prop`: item, weapon, shrine object, pickup, deployable
- `summon`: conjured unit or creature entrance asset
- `fx`: generic visual effect sheet

## Actions

- `single`: one static sprite
- `idle`: looped breathing / stance / aura cycle
- `cast`: spell or skill wind-up / release
- `attack`: attack-only body animation; for controllable heroes/main characters, wide slash arcs and hit FX should be separate `fx`/`impact` sheets
- `shoot`: ranged attack body action; projectile and muzzle flash should usually be separate assets
- `jump`: airborne takeoff / rise / fall / landing action
- `hurt`: damage reaction
- `combat`: combined attack + hurt sheet
- `walk`: travel loop
- `run`: faster travel loop
- `hover`: airborne idle / travel loop
- `charge`: power-up or dash prep
- `projectile`: loopable travel motion
- `impact`: contact burst
- `explode`: stronger impact or destruction burst
- `death`: defeat / vanish / collapse sequence

## Bundle Presets

- `single_asset`: one sprite or one sheet
- `unit_bundle`
  - default: `idle` + `combat`
  - optional: `walk`
- `spell_bundle`
  - default: `cast` + `projectile` + `impact`
- `combat_bundle`
  - default: `idle` + `attack` + `hurt`
- `line_bundle`
  - default: 1-3 forms
  - per form, choose only the needed sheets
- `hero_action_bundle`
  - default: one sheet per action, often `idle` + `run` + `attack` or `shoot` + `jump`
  - keep projectiles, muzzle flashes, slash arcs, weapon trails, impacts, and dust as separate assets unless the runtime supports wider per-action cells plus explicit origins
  - body action sheets should preserve idle/run body scale; split wide FX rather than letting it shrink the body in a fixed cell
  - assemble an engine atlas only after each action passes QC
- `engine_atlas`
  - delivery format only when it combines unrelated actions
  - build from processed action sheets, not from one mixed-action raw image

## Sheet Presets

- `1x4`
  - projectiles
  - simple looping FX
- `2x2`
  - standard idle
  - attack / hurt / impact
  - compact side-view walk
- `2x3`
  - cast sequences
  - death sequences
  - slightly richer combat actions
- `3x3`
  - large creature idle
  - boss aura loops
  - high-value showcase idles
- `4x4`
  - topdown 4-direction player walk sheet
  - non-directional 16-frame action sequence when the user asks for richer casting, summoning, charging, transformation, or death animation
- `5x5` or `custom_grid`
  - use as raw generation only for one coherent long action sequence, prop packs, tileset-like atlases, or another single action family
  - use as a delivery atlas for mixed actions only after separate action sheets have been generated and QC'd

## Agent-First Mapping Hints

- `"make a 4-direction main hero"` -> `player` + `player_sheet`
- `"make a side-view hero with idle, run, shoot, and jump"` -> `player` + `hero_action_bundle`; generate one action sheet per action and keep projectile/impact assets separate
- `"make a side-view hero melee attack"` -> `player` + `hero_action_bundle`; generate a body-only attack grid and separate slash/impact FX when the attack needs a wide arc
- `"make a healer npc"` -> `npc` + `single_asset`, `role=healer`
- `"make a healer npc walk sheet"` -> `npc` + `walk`
- `"make a boss idle"` -> `creature` + `idle`; prefer `3x3`
- `"make a wizard throwing a magic orb"` -> `spell_bundle`
- `"make a fireball projectile"` -> `projectile` + `projectile`; prefer `1x4`
- `"make a hit explosion"` -> `impact` + `impact`; prefer `2x2`
- `"make a summon entrance"` -> `summon` + `cast` or `impact`
- `"make a full fire samurai creature line"` -> `line_bundle`; plan 1-3 forms, then choose sheets per form

## Legacy Compatibility

Keep these mappings working:

- `player_sheet`: 4-direction overworld walk
- `player_walk`: 2x2 down-facing walk
- `npc_walk`: 2x2 down-facing walk
- `combat`: 2x2 attack + hurt
- `evolution`: legacy concept sheet

## Processor Defaults

- use `shared_scale=true` for any multi-frame sheet unless inconsistent scale is intentional
- use `align=bottom` or `feet` for grounded actors
- use `align=center` for floating effects, projectiles, and detached FX
- use `component_mode=largest` when raw sheets contain detached sparkles or edge debris
- use `component_mode=all` when detached effects are an intentional part of the asset silhouette
- for body-only controllable hero actions, use `component_mode=largest`; for projectile, impact, aura, slash FX, or intentionally detached FX sheets, use `component_mode=all`
- reject fixed-cell hero/player body actions when the body visibly shrinks compared with the accepted idle/run scale; split the wide FX or use a runtime with wider per-action cells and explicit origins
- for grounded hero/player melee attacks where the weapon must remain integrated and no FX layer exists, use `scale_strategy=preserve` with `align=feet`: preserve raw-cell scale, translate frames to a shared feet/bottom anchor, and avoid bbox-fit shrinking
- for high-value grounded humanoid body actions that drift during image generation, create a repeated character anchor sheet from the accepted master frame before regenerating; use it to lock camera distance, standing-equivalent scale, root position, feet line, and padding while changing only poses
- for grounded high-value humanoid body sheets, use strict QC with `max_body_scale_cv=0.08` and `max_anchor_y_std=0.05`; treat failures as regeneration signals instead of applying per-frame scale correction
- for a grounded multi-action hero bundle, create one scale profile from the accepted idle/run action and apply it to all compatible body sheets; never select an independent processor scale per action
- for Godot hybrid 2.5D projects, create the scale profile and QC-derived pixel size from the accepted idle/run reference, then reuse both for every compatible action; never recalculate runtime scale from each action's changing pose bbox
- do not apply those humanoid scale/feet gates to jumps, flying actors, or creatures whose action substantially changes posture or silhouette; use action-relative anchors and visual QC instead
- do not use preserve-scale as a blind default for floating FX, projectiles, impact bursts, or sheets where deliberate normalization is useful
- use a layout guide for prop packs, tileset-like atlases, fixed atlas rows, and non-directional 16-frame VFX-heavy action sequences; avoid making it the default for 4-direction walk sheets

## Output Shape

- any sheet mode: transparent sheet + per-frame PNGs + GIF
- Godot `Sprite3D` grid delivery: the normal sheet outputs plus `godot-sprite3d.json`
- Godot multi-action unit delivery: one per-action contract plus a validated `godot-sprite3d-bundle.json`; mark attack, hurt, death, and other non-looping actions as one-shot
- `player_sheet`: plus direction strips and four GIFs
- `single_asset`: cleaned transparent PNG
- `hero_action_bundle`: per-action folders, per-action GIFs, separate projectile/impact assets when needed, and optional assembled engine atlas
- bundles: one output folder per asset inside the bundle root
