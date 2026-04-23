# Agent Sprite Forge

Codex-first 2D sprite generation skill for game-ready pixel assets.

This repository currently ships one generic skill: [`skills/generate2dsprite`](skills/generate2dsprite).

It is designed for Codex first because Codex has built-in image generation. That means you can stay inside one agent workflow:

1. Let the agent plan the asset.
2. Let Codex generate the raw sprite sheet.
3. Let the local processor remove the magenta background, split frames, align sprites, run basic QC, and export transparent PNG/GIF outputs.

The current focus is self-contained 2D assets, not whole game packs.

## English

### What It Can Generate

- Creatures
- Characters
- Players and NPCs
- Spell casts
- Projectiles
- Impacts and explosions
- FX sheets
- Small bundles such as:
  - `unit_bundle`
  - `spell_bundle`
  - `combat_bundle`

### Why Codex First

This repo is intentionally Codex-first because Codex can generate images directly inside the same workflow.

That gives you a much cleaner pipeline:

- no extra image API wiring
- no separate prompt builder service
- no external sprite backend
- one agent decides the plan
- one local processor handles deterministic cleanup and export

The processor script is not the creative brain. The agent decides:

- asset type
- action type
- bundle shape
- sheet layout
- frame count
- alignment strategy
- whether detached effects should be kept or filtered

The script only performs deterministic pixel operations.

### Repository Layout

```text
agent-sprite-forge/
  README.md
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

### Install

#### Option 1: Windows PowerShell

Clone the repo, then copy the skill into your Codex skills directory:

```powershell
git clone https://github.com/0x0funky/agent-sprite-forge.git
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.codex\skills" | Out-Null
Copy-Item -Recurse -Force `
  ".\agent-sprite-forge\skills\generate2dsprite" `
  "$env:USERPROFILE\.codex\skills\generate2dsprite"
```

#### Option 2: macOS / Linux

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
mkdir -p ~/.codex/skills
cp -R ./agent-sprite-forge/skills/generate2dsprite ~/.codex/skills/generate2dsprite
```

After installation, start a new Codex session so the skill is loaded cleanly.

### Suggested Prompts

#### Basic

```text
Use $generate2dsprite to create a 3x3 idle for an ultimate earth titan.
```

```text
Use $generate2dsprite to create a side-view lightning knight attack animation.
```

```text
Use $generate2dsprite to create a late-Sengoku player_sheet for a wandering fire swordsman.
```

#### Spell / FX

```text
Use $generate2dsprite to create a wizard spell bundle with cast, projectile, and impact sprites.
```

```text
Use $generate2dsprite to create a fireball projectile loop and a matching explosion impact.
```

```text
Use $generate2dsprite to create a side-view summon entrance effect for a thunder wolf spirit.
```

#### Character / Monster Examples

```text
Use $generate2dsprite to create Omegamon attack and right-move animation assets.
```

```text
Use $generate2dsprite to create a golden divine boar 2x2 idle animation.
```

```text
Use $generate2dsprite to create a Naruto-style rasengan cast sheet in 2x3.
```

### What You Get

For a typical sheet output:

- `raw-sheet.png`
- `raw-sheet-clean.png`
- `sheet-transparent.png`
- frame PNGs
- `animation.gif`
- `prompt-used.txt`
- `pipeline-meta.json`

For player walk sheets, you also get direction strips and per-direction GIFs.

### Notes

- Best results come from prompts that clearly specify:
  - view
  - action
  - sheet size or expected motion style
  - containment rules
- Large creatures often work better as `3x3 idle`.
- Small spells and projectiles often work better as `1x4`, `2x2`, or `2x3`.
- For commercial projects, prefer original characters or IP that you control.

## 繁體中文

### 這個 Repo 是做什麼的

這是一個以 Codex 為主的 2D sprite 技能 repo，目前只保留一個通用 skill：

- [`skills/generate2dsprite`](skills/generate2dsprite)

它的定位不是整包遊戲產生器，而是專門處理可重用的 2D 資產，例如：

- 角色
- 怪物
- 法術施法
- 飛行物
- 命中特效
- 爆炸特效
- 小型動畫 bundle

### 為什麼先以 Codex 為主

因為 Codex 有內建 image generation，可以把整個流程收在同一個 agent 裡：

1. Agent 規劃資產類型與動畫形式
2. Codex 直接生 raw sprite sheet
3. 本地 processor 做洋紅去背、切格、對齊、QC、輸出透明 PNG / GIF

也就是說，這個 repo 的優勢是：

- 不需要額外接 image API
- 不需要另開 prompt builder 服務
- 不需要獨立 sprite backend
- 規劃交給 agent
- 可重現的像素操作交給 script

### 安裝方式

#### Windows PowerShell

```powershell
git clone https://github.com/0x0funky/agent-sprite-forge.git
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.codex\skills" | Out-Null
Copy-Item -Recurse -Force `
  ".\agent-sprite-forge\skills\generate2dsprite" `
  "$env:USERPROFILE\.codex\skills\generate2dsprite"
```

#### macOS / Linux

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
mkdir -p ~/.codex/skills
cp -R ./agent-sprite-forge/skills/generate2dsprite ~/.codex/skills/generate2dsprite
```

安裝完之後，建議開一個新的 Codex session，讓 skill 重新載入。

### 推薦使用方式

#### 基本範例

```text
使用 $generate2dsprite 幫我做一個究極地元素巨人的 3x3 idle 動畫
```

```text
使用 $generate2dsprite 幫我做一個側視雷電騎士的攻擊動畫
```

```text
使用 $generate2dsprite 幫我做一個戰國浪人主角的 4 方向 player_sheet
```

#### 法術 / 特效範例

```text
使用 $generate2dsprite 幫我做一個巫師施法 bundle，包含 cast、projectile、impact
```

```text
使用 $generate2dsprite 幫我做一個火球飛行循環跟對應的爆炸命中特效
```

```text
使用 $generate2dsprite 幫我做一個雷狼召喚登場特效
```

#### 角色 / 怪物範例

```text
使用 $generate2dsprite 幫我做一個奧米加獸的攻擊元素和向右移動元素
```

```text
使用 $generate2dsprite 幫我做一個黃金版神豬的 2x2 idle 動畫
```

```text
使用 $generate2dsprite 幫我做一個漩渦鳴人使用螺旋丸的 2x3 cast sheet
```

### 會輸出什麼

一般 sheet 類型通常會輸出：

- `raw-sheet.png`
- `raw-sheet-clean.png`
- `sheet-transparent.png`
- 每格 frame PNG
- `animation.gif`
- `prompt-used.txt`
- `pipeline-meta.json`

如果是 `player_sheet`，還會多出：

- 四方向 strip
- 四方向 GIF

### 設計理念

這個 skill 的核心不是把規則全都寫死，而是：

- 美術規劃交給 agent
- 去背、切格、對齊、輸出交給 deterministic processor

也就是：

- Agent 決定要做 `idle`、`cast`、`projectile`、`impact` 還是 `bundle`
- Agent 決定用 `2x2`、`2x3`、`3x3`、`1x4` 還是 `4x4`
- Script 只處理可重現的像素工作

這樣比較容易往後延伸到更多通用 2D 資產流程。
