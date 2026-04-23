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
- same bounding box and same pixel scale across frames

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

For `player` and `npc`:

- top-down 2D pixel art for a 16-bit RPG overworld
- 3/4 view from slightly above
- full body visible
- chunky readable pixel-art with crisp dark outlines
- enough margin for clean engine rendering

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

### `walk` / `run` / `hover`

State the travel behavior clearly:

- grounded stride
- hover bob
- crawl
- slither
- mechanical glide

## Sheet-Specific Rules

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

### `3x3` large idle

Say:

- exactly 9 equal cells in a `3x3` grid
- same bounding box in all 9 cells
- subject fills only about 55% to 65% of each cell
- no edge crossing anywhere

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
3. describe frame-by-frame motion
4. restate same-scale and containment rules
5. restate magenta background and no-text rules
