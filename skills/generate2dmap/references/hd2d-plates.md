# HD-2D plates: stage contract, prompts and variants

A plate is a finished painting that actors stand on: a battle backdrop, a story scene or a fixed-camera exploration room. Its geometry is data, not pixels. Plan the stage first, generate the painting to fit it, then prove the actors' feet land on dry ground at every screen shape the game supports. Presentation (light cookie, shadows, depth blur, grade) is in [hd2d-presentation.md](hd2d-presentation.md); selected environment motion is in [background-scenes.md](background-scenes.md).

Commands run from the user's project root; `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Write outputs into the project, never into the skill folder. Every tool writes a new `--output-dir`, publishes it only when complete and refuses an existing one; with `--strict` a failed check publishes nothing.

## 1. Route

1. Write a planned `stage.json` (section 2) with the canvas you will ask for, usually 16:9.
2. Turn it into a layout guide and a prompt block:

       python "<skill-dir>/scripts/scene_layout_guide.py" --stage plan/stage.json --output-dir plan/guide-v1

3. Generate the plate with `guide.png` attached as a layout reference and `prompt-block.txt` pasted into the prompt (section 4). `guide.png` is an opaque RGB image (a reference for the image model, not a layer to composite). Measure the returned size; a requested size is not a returned size.
4. Set `sourceSize` to the real size and `plate` to the file, look at the painting, and move polygons, slots and regions onto what was actually painted. UV values survive a resize of the same aspect.
5. Validate the stage and look at every render:

       python "<skill-dir>/scripts/validate_stage.py" --stage scene/stage.json --output-dir scene/stage-qa-v1

6. Extract the painted lights, curate them and check the atmosphere data (sections 2 and 6 of [hd2d-presentation.md](hd2d-presentation.md)):

       python "<skill-dir>/scripts/extract_scene_lights.py" extract --plate scene/plate.png --stage scene/stage.json --output-dir scene/lights-v1 --expect 3

7. Add environment motion only inside regions that may move: the masked-motion tools (build_motion_mask and scene_motion, described in [background-scenes.md](background-scenes.md) when present) take source-pixel regions, so a stage region with `appliesTo` `motion` becomes a motion-plan `protected` entry at `uv * sourceSize`.
8. Check every variant of the plate against the master before it ships (section 5).

## 2. The stage contract

`generate2dmap.stage.v1` in [map.schema.json](schemas/map.schema.json). Every coordinate is UV of `sourceSize`: `[u, v]` with `u` from the left edge and `v` from the top, both 0..1. Slots are foot roots (where the soles touch the ground), never image-box centres.

```json
{
  "schema": "generate2dmap.stage.v1",
  "plate": "battle-forest.png",
  "sourceSize": [1672, 941],
  "fit": "cover",
  "objectPosition": [0.5, 0.5],
  "reviewedAspectRange": [1.3333, 2.3333],
  "groundPolygons": [[[0.17, 0.62], [0.83, 0.62], [0.96, 0.98], [0.04, 0.98]]],
  "playableBand": [0.6, 0.98],
  "slots": {"hero": [[0.69, 0.8], [0.78, 0.88]], "enemy": [[0.25, 0.78], [0.18, 0.86], [0.32, 0.9]], "boss": [0.33, 0.84]},
  "approachPoints": [[0.5, 0.82]],
  "protectedRegions": [
    {"id": "stone-arch", "box": [0.56, 0.2, 0.74, 0.55], "appliesTo": ["walk", "motion", "edit"]},
    {"id": "dry-floor", "box": [0.43, 0.62, 0.61, 0.76], "appliesTo": ["motion"]}
  ],
  "effects": [{"id": "pond", "kind": "ripple", "polygon": [[0.02, 0.5], [0.15, 0.5], [0.15, 0.58], [0.02, 0.58]], "period": 4.4, "amplitude": 1.8, "wavelength": 80, "axis": "x"}],
  "bakedContent": {"actors": false, "collectibles": false}
}
```

| Field | Meaning |
|---|---|
| `fit`, `objectPosition` | How the game projects the plate: `cover` fills the screen and crops, `contain` letterboxes. `objectPosition` ([fx, fy], default [0.5, 0.5]) places the visible window, like CSS `object-position`; it is an optional extension the tools read. |
| `reviewedAspectRange` | [min, max] screen width / height the layout must work at. The solver must place every actor at both ends; values are compared with 0.1% slack, so 2.3333 covers 21:9. |
| `groundPolygons` | Dry, level, open ground where feet may stand. There are no holes: split the ground around water or a pit instead of covering it. |
| `playableBand` | [v0, v1] rows where feet may stand at all; the layout searches inside it. Every slot must lie in it. |
| `slots` | `hero` and `enemy` foot roots (H1, H2..., E1...); `boss` is an alternative formation (heroes plus boss), solved separately. |
| `approachPoints` | Points actors walk to (an altar, a door step); they must be standable too. |
| `protectedRegions` | A `box` or `polygon` with `appliesTo`: `walk` (no feet or slots), `motion` (environment motion keeps out), `edit` (variants must not change it). Without `appliesTo` a region applies to all three. A dry floor actors stand on is `["motion"]`, never `walk`. |
| `effects` | Planned surfaces: `ripple` is water and never standable; `shimmer`, `sway` and `glow` only animate. `period`, `amplitude`, `wavelength` and `axis` are runtime hints. |
| `bakedContent` | What the painting itself contains. `false` for actors and collectibles means a clean plate with separate runtime sprites; a list names what is painted. |

## 3. What validate_stage proves

A foot is standable when the foot point and 8 samples on its footprint ellipse all lie in some ground polygon and outside every walk-protected region and every `ripple` polygon (the rule of map collision, plan Appendix C). The footprint radius is `--foot-radius` (default 0.01 of the plate width); its height is that times `--y-squash` (default 0.58).

| Check | Fails when |
|---|---|
| ground polygons are simple | a polygon crosses itself or has no area |
| slots stand on ground / avoid walk-protected regions / stay off water | any footprint sample of a slot misses the ground, enters a `walk` region or a ripple polygon (a slot on water fails) |
| slots inside playableBand | a slot's v is outside the band |
| approach points are standable | as for slots |
| effects avoid motion-protected regions | an effect polygon overlaps a region protected from motion |
| plate matches sourceSize | the plate image has another size |
| layout `<aspect>` `<formation>` | an actor finds no standable spot inside the reviewed range (outside it, only a warning) |
| layout solves at both ends of reviewedAspectRange | the range claims an aspect the layout cannot hold |

Warnings: footprints of two slots overlap, water lies inside a ground polygon, `bakedContent` is missing or paints actors, an actor needed less than 0.7 of its size.

The layout solver projects the plate onto a reference viewport per aspect (720 px high; 390 px wide for portrait) with the stage's fit, squeezes the formation sideways to stay on screen, and tries each slot's own spot first. When that foot is not standable, or the actor box leaves the screen, hits a reserved UI panel or crowds another actor, it searches a grid over the ground band for the nearest valid foot, shrinking the box along a scale ladder (1.0, 0.9, ... down to `--min-scale`, never below 38 px). Heroes and enemies keep their own side of the screen. Actors are drawn in foot-y order (`drawOrder`). The default `--ui-profile battle` reserves a typical battle HUD (top bar, party, command, detail, target list, log); pass your HUD with `--ui panels.json` (`{"landscape": [{"id": "command", "box": [0.8, 0.45, 0.98, 0.85]}], "portrait": [...]}`, boxes in viewport fractions) or switch it off with `--ui-profile none`. Actor boxes default to 0.2 (hero), 0.18 (enemy) and 0.4 (boss) of the plate height with the foot at 96% of the box; change them with `--actor-height hero=0.16`.

    python "<skill-dir>/scripts/validate_stage.py" --stage scene/stage.json --output-dir scene/stage-qa-v2 --aspects 4:3,16:9,21:9,9:19.5 --ui panels.json --actor-height hero=0.16 --strict

Read `layout-<aspect>.png` (and `-boss`): green ground, red no-walk regions, blue water, grey UI panels, actor boxes with their footprint, scale factor and draw order; red boxes are unplaced. `stage-overlay.png` shows the whole plate with every slot labelled. With a plate the best status is `needs-visual-review`: the numbers cannot see whether the painted ground really is open and dry where the polygon says.

## 4. Prompt

`prompt-block.txt` restates the plan in whole percent (x from the left, y from the top), so it stays true when the generator returns another pixel size: the canvas, the walkable ground and its outline, the playable band, the area every reviewed screen shows, the clear standing spots, landmarks, planned water and other moving surfaces, the forbidden-in-walk list and what may be painted (`bakedContent`). Paste it under the scene description:

```text
Use case: <battle backdrop | story scene>. One finished <style> painting, <W>x<H>, fixed camera seen from about
<20> degrees above the ground, no characters. Scene: <place, time of day, light, palette, landmarks>.
<paste prompt-block.txt here>
The attached layout guide is geometry only. Exact art style from the attached accepted master, if any.
No text, UI, labels, borders or watermark.
```

Add items with `--forbid "fallen logs"`; `--no-default-forbid` starts the list empty. The default list drops people when `bakedContent.actors` names painted actors, and loose items when collectibles are painted.

## 5. Variants

A night version, a lit lantern, a broken bridge or a festival dressing must keep the master's camera, canvas and every region the game relies on. Ask for an edit of the accepted master that changes only the named area, then check it:

    python "<skill-dir>/scripts/edit_locality_check.py" --before scene/plate.png --after scene/plate-lit.png --stage scene/stage.json --protect-ground --edit-box 0.62,0.18,0.74,0.42 --output-dir scene/lit-check-v1

- Protected: the stage's regions with `appliesTo` `edit` (or none), `--protect-ground` (ground polygons and playable band), `--protect-band V0:V1` and `--protect-box U0,V0,U1,V1`.
- With `--edit-box`, everything outside the boxes (past `--edit-margin`, default 0.02 of the width) must stay unchanged and the boxes must change.
- A region fails when more than `--max-changed` (0.002) of it changed by over `--pixel-threshold` (24 levels after a 3x3 blur), when its mean difference exceeds `--max-mae` (3 levels: a variant that darkens the whole room fails here), or when one connected change covers `--min-component` (0.0005) of the image (a lamp that went out elsewhere).
- Sizes must match or the resize must be recorded: `--conform conform.json` (conform_background.py output or any `{scale, src_rect, out_size}` transform) or `--resize cover|stretch`. An unrecorded resize is an error, never a guess.
- `--exact` (same size only) fails on any changed byte, for composited variants.

    python "<skill-dir>/scripts/edit_locality_check.py" --before scene/plate.png --after scene/plate-night-1280.png --resize stretch --protect-band 0.62:1 --output-dir scene/night-check-v1

When a check fails, regenerate or repaint the variant; masks and crops cannot restore protected content. Read `locality-diff.png`: changed pixels are red, sub-threshold differences faint orange, protected regions orange (red when they fail), edit boxes green.

## 6. Limits

- The tools check the stage's own geometry and the pixels you name; they do not see the painting. Look at every overlay and render before accepting a stage.
- Layouts are solved at reference viewports with rectangular actor boxes and the given HUD panels; depth-dependent actor scale, safe-area insets and the game's own layout code are not modelled. Compare the game's feet with `layouts[].actors[].footUv` or run the game at the same aspects.
- Light extraction proposes bright compact blobs and cannot tell a lantern from a bright flower or a reflection; review `lights-overlay.png` and curate `lights.json`.
- A variant can pass every locality check and still look wrong; the checks prove where it changed, not that the change is good.
