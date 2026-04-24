# Agent Sprite Forge

Traditional Chinese README: [README.zh-TW.md](./README.zh-TW.md)

![Agent Sprite Forge Banner](./src/banner.png)

> Turn natural-language prompts into game-ready 2D sprites with Codex.
>
> Plan with an agent. Render with built-in image generation. Export clean transparent sheets and GIFs.

## Showcase

### Text To Sprite

<table>
  <tr>
    <td align="center" width="33%">
      <img src="./src/goku-kame.gif" alt="Goku Kamehameha" width="192" />
      <br />
      <strong>Goku with Kamehameha</strong>
      <br />
      <code>help me to use $generate2dsprite to create a goku is attacking with Kamehameha</code>
    </td>
    <td align="center" width="33%">
      <img src="./src/naruto-rasengan.gif" alt="Naruto Rasengan" width="192" />
      <br />
      <strong>Naruto using Rasengan</strong>
      <br />
      <code>使用 $generate2dsprite 幫我做一個鳴人使用螺旋丸的元素</code>
    </td>
  </tr>
</table>

### Spell Bundle / Cast, Projectile, Impact

<table>
  <tr>
    <td align="center" width="33%">
      <img src="./src/cast.gif" alt="Fire mage cast animation" width="176" />
      <br />
      <strong>Cast</strong>
    </td>
    <td align="center" width="33%">
      <img src="./src/projectile.gif" alt="Fire mage projectile animation" width="176" />
      <br />
      <strong>Projectile</strong>
    </td>
    <td align="center" width="33%">
      <img src="./src/impact.gif" alt="Fire mage impact animation" width="176" />
      <br />
      <strong>Impact</strong>
    </td>
  </tr>
</table>

Prompt:

```text
Use  $generate2dsprite to create a fire mage cast animation with projectile and impact.
```

### Game Sprite / Four-Direction Walk

<table>
  <tr>
    <td align="center" width="25%">
      <img src="./src/down.gif" alt="Samurai walking down" width="144" />
      <br />
      <strong>Down</strong>
    </td>
    <td align="center" width="25%">
      <img src="./src/left.gif" alt="Samurai walking left" width="144" />
      <br />
      <strong>Left</strong>
    </td>
    <td align="center" width="25%">
      <img src="./src/right.gif" alt="Samurai walking right" width="144" />
      <br />
      <strong>Right</strong>
    </td>
    <td align="center" width="25%">
      <img src="./src/up.gif" alt="Samurai walking up" width="144" />
      <br />
      <strong>Up</strong>
    </td>
  </tr>
</table>

Prompt:

```text
Use Generate 2D Sprite to create a top-down 4x4 player_sheet for a wandering young samurai with a red scarf and short katana.
Make a four-direction walk sprite sheet with 4 frames per direction.
Row order: down, left, right, up.
Same character, same outfit, same proportions, same pixel scale in every frame.
Solid #FF00FF background.
Each frame must fit fully inside its cell, with clear margin on all sides.
Retro JRPG pixel-art style.
```

### Reference To Sprite

<table>
  <tr>
    <td align="center" width="35%">
      <img src="./src/ref1.jpg" alt="Reference crocodile" width="180" />
      <br />
      <strong>Reference</strong>
    </td>
    <td align="center" width="65%">
      <img src="./src/croc_stone_play.gif" alt="Crocodile playing with a stone" width="220" />
      <br />
      <strong>Generated sprite animation</strong>
      <br />
      <code>幫我使用 $generate2dsprite 做一個這隻鱷魚玩手上石頭的元素</code>
    </td>
  </tr>
  <tr>
    <td align="center" width="35%">
      <img src="./src/ref2.jpg" alt="Reference male character" width="180" />
      <br />
      <strong>Reference</strong>
    </td>
    <td align="center" width="65%">
      <img src="./src/cz.gif" alt="Male character teaching animation" width="220" />
      <br />
      <strong>Generated sprite animation</strong>
      <br />
      <code>Use  $generate2dsprite to create this male character teaching.</code>
    </td>
  </tr>
</table>

Codex-first 2D sprite generation skill for game-ready pixel assets.

This repository currently ships one generic skill: [`skills/generate2dsprite`](./skills/generate2dsprite).

Codex is the primary target because Codex already has built-in image generation. That lets one agent handle the full loop:

1. Plan the asset.
2. Generate the raw sprite sheet.
3. Run deterministic local post-processing for chroma-key cleanup, frame extraction, alignment, QC, and transparent PNG/GIF export.

The current focus is self-contained 2D assets, not full game packs.

## What It Can Generate

- Creatures
- Characters
- Players and NPCs
- Spell casts
- Projectiles
- Impacts and explosions
- FX sheets
- Small bundles such as `unit_bundle`, `spell_bundle`, and `combat_bundle`

## Why Codex First

This repo is intentionally Codex-first because Codex can generate images directly inside the same workflow.

That gives you a much cleaner pipeline:

- No separate image API wiring
- No external sprite backend
- No extra prompt-builder service
- One agent decides the asset plan
- One local processor handles deterministic cleanup and export

The script is not the creative brain. The agent decides:

- Asset type
- Action type
- Bundle shape
- Sheet layout
- Frame count
- Alignment strategy
- Whether detached effects should be kept or filtered

The Python script only performs deterministic pixel operations.

## Repository Layout

```text
agent-sprite-forge/
  README.md
  README.zh-TW.md
  requirements.txt
  src/
  skills/
    generate2dsprite/
      SKILL.md
      agents/
        openai.yaml
      references/
        modes.md
        prompt-rules.md
      scripts/
        generate2dsprite.py
```

## Install

### Option 1: Windows PowerShell

Clone the repo, install the local processor dependencies, then copy the skill into your Codex skills directory:

```powershell
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd .\agent-sprite-forge
python -m pip install -r .\requirements.txt
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.codex\skills" | Out-Null
Copy-Item -Recurse -Force `
  ".\skills\generate2dsprite" `
  "$env:USERPROFILE\.codex\skills\generate2dsprite"
```

### Option 2: macOS / Linux

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd ./agent-sprite-forge
python3 -m pip install -r ./requirements.txt
mkdir -p ~/.codex/skills
cp -R ./skills/generate2dsprite ~/.codex/skills/generate2dsprite
```

Start a new Codex session after installation so the skill is loaded cleanly.

## Python Requirements

The local post-processor depends on:

- `Pillow`
- `numpy`

They are listed in [`requirements.txt`](./requirements.txt). Codex handles image generation itself, but these Python packages are still needed for:

- Magenta background removal
- Frame splitting
- Bounding-box extraction
- Alignment and rescaling
- Transparent GIF and PNG export

## Suggested Prompts

### Basic

```text
Use  $generate2dsprite to create a 3x3 idle for an ultimate earth titan.
```

```text
Use  $generate2dsprite to create a side-view lightning knight attack animation.
```

```text
Use  $generate2dsprite to create a late-Sengoku player_sheet for a wandering fire swordsman.
```

### Spell / FX

```text
Use  $generate2dsprite to create a wizard spell bundle with cast, projectile, and impact sprites.
```

```text
Use  $generate2dsprite to create a fireball projectile loop and a matching explosion impact.
```

```text
Use  $generate2dsprite to create a side-view summon entrance effect for a thunder wolf spirit.
```

### Character / Monster Examples

```text
Use  $generate2dsprite to create Omegamon attack and right-move animation assets.
```

```text
Use  $generate2dsprite to create a golden divine boar 2x2 idle animation.
```

```text
Use  $generate2dsprite to create a Naruto-style rasengan cast sheet in 2x3.
```

## What You Get

For a typical sheet output:

- `raw-sheet.png`
- `raw-sheet-clean.png`
- `sheet-transparent.png`
- Frame PNGs
- `animation.gif`
- `prompt-used.txt`
- `pipeline-meta.json`

For player walk sheets, you also get direction strips and per-direction GIFs.

## Notes

- Best results come from prompts that clearly specify view, action, and the desired motion style.
- Large creatures often work better as `3x3 idle`.
- Small spells and projectiles often work better as `1x4`, `2x2`, or `2x3`.
- For commercial projects, prefer original characters or IP that you control.

## License

MIT. See [LICENSE](./LICENSE).
