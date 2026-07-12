# Prompt Rules

Use this file when writing sprite prompts by hand.

Do not delegate prompt writing to a script unless you specifically need parity with an older generated prompt.

## Global Rules

Always keep these constraints:

- background is 100% solid flat magenta `#FF00FF`
- no gradients in the background
- no text
- no labels
- no UI
- no speech bubbles
- exact grid count only
- no borders or frames between cells
- same asset identity across frames
- same camera distance and standing-equivalent anatomical scale across body frames; allow natural pose bbox changes without per-pose zoom
- raw sprite art must come from built-in `image_gen`, not Three.js, Canvas, SVG, HTML/CSS drawing, PIL shape drawing, procedural geometry, placeholder primitives, or code-rendered screenshots

## Style Rules

Choose the art style from the user request, project context, map context, or reference:

- `pixel_art`: general sprite default for classic 2D game actors and animation sheets.
- `clean_hd`: clean hand-painted HD 2D game asset style, crisp silhouettes, smooth surfaces, low texture noise, controlled lighting, no chunky pixels.
- `pixel_inspired`: clean modern pixel-art-inspired style without 16-bit wording, heavy dithering, or noisy microtexture.
- `retro_pixel`: 16-bit pixel art or retro JRPG pixel art, only when explicitly requested.
- `map_style` or `project-native`: match the visible reference, existing game, or `$generate2dmap` selected art style.

Do not write `16-bit`, `retro JRPG`, or `chunky pixel-art` unless the user asks for that look. For clean HD map props, explicitly say `Do not make pixel art`.

## Reference Rules

Use these rules when the user attaches a reference, points to a local image, asks for consistency with an earlier generated image, or asks for an evolution/variant of an existing sprite:

- Make the reference image visible to built-in `image_gen` before generation. If the reference is a local file, call `view_image` first; do not assume a path string is a visual input.
- In the prompt, say `use the image just shown as the visual reference`.
- State what must stay fixed: silhouette family, palette, face/eyes, costume or markings, accessories, material language, and art style.
- State what may change: pose, animation phase, action energy, size progression, evolution traits, or FX intensity.
- For animation sheets, preserve the same character identity in every cell and only change the animation pose or effect state.
- For evolution lines, keep visible lineage markers while allowing larger silhouette, added details, or stronger colors per form.
- Keep the normal magenta-background and containment rules even when using a reference.

## Layout Guide Rules

Use a layout guide when the sheet needs stronger geometric control than text alone can provide:

- good fit: `3x3` and `4x4` prop packs, tileset-like atlases, fixed atlas rows, and non-directional 16-frame sequences such as casting, summoning, charging, death, or transformation
- possible fit: `3x3` large idles or showcase loops when earlier generations drift in scale, spacing, or edge safety
- risky fit: four-direction walk sheets, because guide pressure can make directional poses too centered and reduce locomotion clarity

When using a layout guide, make the guide image visible first and write:

```text
Use the layout guide image just shown as a layout-only reference. Use it only to understand the rows, columns, equal invisible frame slots, centering, spacing, and safe padding. Do not reproduce the guide: no visible boxes, no safe-area rectangles, no center marks, no labels, no borders, no guide background.
```

Keep the creative prompt agent-written. The layout guide only provides geometry; it must not replace the action plan, art style, identity lock, or containment rules.

### Character Anchor Sheets

For high-value grounded player/hero body actions, use a character anchor sheet when ordinary prompting still drifts in character scale or feet placement. This is stronger than an abstract box guide:

1. Accept one neutral/idle master frame with the correct identity, camera distance, standing scale, and padding.
2. Run `scripts/make_anchor_layout.py` to repeat that exact frame into every intended grid cell at one fixed scale and feet line.
3. Make both images visible to built-in `image_gen`:
   - master frame: identity, art style, anatomy, camera, and material reference
   - anchor sheet: slot, scale, body-root, feet-line, and padding reference
4. Ask the model to change only the poses while preserving the anchor sheet's geometry.

Use prompt language like:

```text
Image 1 is the exact character identity and art reference.
Image 2 is a scale-and-root template made from the same accepted character.
Preserve Image 2's exact cell locations, fixed camera distance, standing-equivalent anatomical scale, body-root position, grounded foot-contact line, and padding. Change only the action pose in each slot. Never zoom or resize a pose to fill its cell. Natural crouching may change the visible pose bbox, but torso, head, limb thickness, costume, and weapon scale must remain constant.
```

Do not use a grounded character anchor sheet as a blind default for jumps, falls, knockback, flying/hovering motion, projectiles, impacts, creatures whose attack strongly changes silhouette/posture, or intentionally changing-scale FX. Those assets need center/root, motion-relative layouts, or visual QC instead of a fixed humanoid feet-line contract.

For a multi-action character bundle, reuse the same master image, character anchor sheet, raw grid geometry, and generation resolution for every grounded body action. After accepting idle or run, create one processor scale profile from it and apply that profile to later actions. The anchor sheet controls the model; the scale profile prevents postprocessing from assigning different magnification to attack, hurt, idle, or run.

## Containment Rules

For any sheet mode, say this explicitly when consistency matters:

- the entire subject must fit fully inside each cell
- no body part, effect, weapon, tail, wing tip, orb, spark, or smoke trail may cross a cell edge
- leave magenta margin on all four sides
- use the same silhouette scale in every frame

If detached FX are undesirable, say:

- no floating detached effects outside the main silhouette

If detached FX are required, say:

- detached effects must remain tightly grouped near the main subject and still fit inside the cell

## View Rules

- `topdown`: for overworld actors and player / NPC sheets
- `side`: for projectiles, side-view units, impact FX
- `3/4`: for creature battle sprites, bosses, showcase idles, side-view spellcasters

## Character Style

For `player` and `npc` when the request does not specify another style:

- top-down 2D pixel art for a 16-bit RPG overworld
- 3/4 view from slightly above
- full body visible
- chunky readable pixel-art with crisp dark outlines
- enough margin for clean engine rendering

## Map Prop Style

For `prop` assets requested by `$generate2dmap`, match the selected map art style:

- `clean_hd`: clean hand-painted HD 2D game asset style, crisp silhouettes, smooth painted surfaces, low texture noise, controlled accent lighting, no chunky pixels.
- `pixel_inspired`: clean modern pixel-art-inspired prop, crisp readable shape, no 16-bit wording, no heavy dithering.
- `retro_pixel`: 16-bit or retro JRPG pixel-art prop, only when the map is explicitly retro pixel.

For clean HD props, use mostly front-facing top-down RPG object view: upright objects are vertical and centered, with only a small visible top face. Avoid strong isometric diagonal rotation unless requested.

## Creature and FX Style

For `creature`, `spell`, `projectile`, `impact`, `summon`, and `fx`:

- strong silhouette
- readable body colors or effect shape
- battle-ready or gameplay-readable pose
- avoid painterly composition drift between frames
- if humanoid, keep it clearly non-player unless the user explicitly wants a player-like unit

## Action Rules

### `idle`

Use:

- neutral stance
- subtle motion
- weight shift or aura pulse
- strongest idle accent before looping

For a massive grounded boss, replace lateral weight shift with rooted weight: keep both feet and the pelvis fixed, compress the torso vertically, pulse an internal core, settle shoulders, and animate attached ornaments with small delayed secondary motion. Explicitly forbid whole-body left/right translation.

Prefer:

- `2x2` for standard actors
- `3x3` for large creatures and showcase idles

### `cast`

A `2x3` cast is often the best default:

- readiness
- energy gather
- stronger gather
- release start
- release peak
- settle or hold

### `attack`

For a compact attack-only sheet, describe:

- wind-up
- strike
- follow-through
- recovery

For long quadrupeds, wolves, serpentine bodies, or actors with wide tails, do not describe a pounce as whole-body travel across the cell. Lock one shared silhouette envelope across all frames: fixed torso center, central 70% to 72% safe width/height, tail or long appendages tucked inward, and attack energy shown through in-place compression, neck/limb extension, and recoil. This is stronger and more reliable than a generic request for generous margins.

For controllable heroes, main characters, and fixed-cell game sprites, write attack body prompts as body-only:

- no detached slash arc
- no wide weapon trail
- no muzzle flash
- no projectile
- no impact burst
- no detached dust cloud
- weapon remains close enough that the body bbox stays near idle/run size
- body height and feet/bottom anchor match the accepted idle/run sheet

If the attack needs a large slash arc, sword trail, muzzle flash, or hit spark, generate it as a separate `fx`, `projectile`, or `impact` sheet and layer it in the runtime.

For a high-value grounded hero whose attack sheets repeatedly drift in size or feet placement, build a character anchor sheet from the accepted idle/master frame before regenerating the body action. Keep the action body-only and use strict grounded-body QC after processing.

### `hurt`

For a hurt-only sheet, describe:

- impact
- recoil
- stagger
- recovery

### `combat`

For a compact combined sheet:

- top-left: attack wind-up
- top-right: attack strike
- bottom-left: hurt impact
- bottom-right: hurt recovery

### `projectile`

Usually prefer `1x4` or `2x2`.

Describe:

- same projectile identity in all frames
- travel direction stays consistent
- shape changes are small and loopable
- glow or trail stays inside the frame

### `impact` / `explode`

Usually prefer `2x2`.

Describe:

- ignition or contact
- expansion
- peak burst
- fade or collapse

For a ground-contact looping effect such as fire, define one shared horizontal ignition baseline at a fixed percentage of every cell height. Let tips deform above it while the contact line, width, camera distance, and scale remain fixed. For a billboarded runtime overlay, forbid baked ground, lava pools, tile plates, shadows, and perspective floor art; those elements make the effect appear offset after engine placement.

### `walk` / `run` / `hover`

State the travel behavior clearly:

- grounded stride
- hover bob
- crawl
- slither
- mechanical glide

## Sheet-Specific Rules

### Mixed-action atlas guardrail

Do not use a single raw generated sheet to pack unrelated actions just because the target engine wants a `4x4`, `5x5`, or custom atlas.

Avoid prompts like:

- row 1 idle, row 2 run, row 3 shoot, row 4 jump
- first row walk, second row attack, third row hurt, fourth row death
- one big atlas containing every hero action

For controllable heroes, main characters, and high-value player assets:

1. Generate each action as its own multi-row grid sheet, usually `2x2` for 4-frame actions, `2x3` for 6-frame actions, and `2x4`, `3x3`, `3x4`, or `4x4` for longer actions.
2. Keep attack/shoot/cast body animation separate from projectile, muzzle flash, slash arc, weapon trail, impact, and dust unless the runtime explicitly supports wider per-action cells plus explicit origins.
3. Process and visually QC each action independently for feet line, body center, scale, silhouette, and edge safety.
4. Reject a body action when the body appears more than about 10-15% smaller than idle/run because a wide FX bbox forced it to shrink.
5. If the raw action has good scale but the processor shrinks it because a sword, spear, cape, or integrated weapon pose makes the bbox too wide, reprocess with preserve-scale feet alignment instead of accepting the smaller body.
6. Assemble a `4x4`, `5x5`, or custom engine atlas only after the separate action sheets pass QC.
7. Create one scale profile from the accepted idle/run action and apply it to all compatible body actions; reject unexplained cross-action body-scale drift rather than adding action-specific runtime compensation.

Allowed raw multi-row sheets:

- canonical four-direction locomotion sheets where every row is the same walk/run action in a different direction
- one continuous non-directional long action sequence, read left-to-right across rows
- prop packs or tileset-like atlases where each cell is intentionally a separate object
- compact low-stakes enemy combat sheets, but not controllable hero production assets

### `4x4` player sheet

Use:

- row 1: down
- row 2: left
- row 3: right
- row 4: up
- column 1: neutral
- column 2: left foot forward
- column 3: neutral again
- column 4: right foot forward

Do not use a layout guide by default for this sheet. Try an unguided prompt first unless the previous result crossed cell edges or failed the grid shape.

### `3x3` large idle

Say:

- exactly 9 equal cells in a `3x3` grid
- same bounding box in all 9 cells
- subject fills only about 55% to 65% of each cell
- no edge crossing anywhere

Use a layout guide when a previous 3x3 result has uneven spacing, inconsistent scale, or edge-touching frames.

### `4x4` non-directional action sequence

Use for casting, summoning, charging, transformation, death, and other single-action loops:

- exactly 16 equal cells in a `4x4` grid
- read frames left-to-right across each row, then continue on the next row
- describe each phase in order, from anticipation through peak action to settle or loop return
- keep the subject identity stable while allowing pose, energy, and compact attached effects to change
- use a layout guide when the action includes VFX, portals, circles, summons, or other elements that might cross cell boundaries

Do not use this format as a shortcut for four unrelated hero actions. If the requested rows are different actions, treat it as a `hero_action_bundle` or `engine_atlas` delivery problem instead.

### Preserve-scale Processing

Use preserve-scale processing when the raw sheet already has stable subject size but bbox-fit normalization makes the playable character smaller:

- grounded player or main hero melee attacks with a sword, spear, staff, gun, cape, or wide body pose integrated into the body sheet
- no separate runtime FX layer exists, so the weapon cannot be moved to a separate slash/projectile/impact sheet
- the raw grid has enough empty cell padding and no edge-touching frames

Recommended processor shape:

```bash
python scripts/generate2dsprite.py process \
  --input <raw-sheet.png> \
  --target player \
  --mode attack \
  --output-dir <out-dir> \
  --rows 2 \
  --cols 3 \
  --align feet \
  --scale-strategy preserve \
  --component-mode largest \
  --strict-qc \
  --max-body-scale-cv 0.08 \
  --max-anchor-y-std 0.05
```

For a multi-action bundle, add `--write-scale-profile <profile.json>` to the accepted idle/run reference process, then use `--scale-profile <profile.json>` for attack, hurt, cast-body, walk, and other compatible grounded actions. Do not create a separate profile for each action.

Preserve-scale means: fixed-grid cut, chroma-key cleanup, apply one uniform raw-cell scale and safety margin to every frame, then translate each detected subject to one shared feet/bottom anchor. It does not apply a different bbox-fit scale per frame. A scale profile extends this contract across separate action sheets. QC should require no empty, edge-touching, or paste-clamped frames. For grounded high-value humanoid body actions, treat `body_scale_cv > 0.08`, normalized `anchor_y_std > 0.05`, or unexplained profile scale drift above the profile limit as a regeneration signal. Do not use these grounded thresholds for jumps, knockback, flying actors, creatures with large posture/silhouette changes, or FX.

For elongated creature attacks, regenerate whenever output-edge contact or paste clamping occurs. A visually complete raw contour may use the explicit source-edge override only after processed output-edge and clamp counts are both zero; the override is never a substitute for a smaller shared motion envelope.

### `5x5` and custom grids

Use raw `5x5` or custom-grid generation only when the entire sheet is one coherent action family, a prop pack, a tileset-like atlas, or a single long sequence.

For mixed action requirements, generate each action separately and assemble the final grid after QC. The assembled atlas is a delivery artifact, not the raw image-generation target.

### `1x4` projectile

Say:

- exactly 4 equal cells in one row
- same projectile size in every frame
- only the internal energy or shape pulse changes

## Bundle Prompting

When generating a bundle, write each asset prompt independently.

Good default decomposition:

- caster unit
- projectile
- impact

or:

- idle
- combat
- walk

Do not try to force unrelated assets into one giant sheet.

## Quick Prompt Pattern

1. state the asset type and sheet shape
2. describe the subject identity
3. if applicable, state the reference role and invariants
4. describe frame-by-frame motion
5. restate same-scale and containment rules
6. restate magenta background and no-text rules
