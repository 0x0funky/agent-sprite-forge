# Agent Sprite Forge

English README: [README.md](./README.md)

![Agent Sprite Forge Banner](./src/banner.png)

> 把自然語言需求直接變成可用於遊戲的 2D sprite。
>
> 由 agent 規劃資產，由 Codex 生圖，再由本地 processor 輸出乾淨的透明 sheet 與 GIF。

## Showcase

### 文字到 Sprite

<table>
  <tr>
    <td align="center" width="33%">
      <img src="./src/goku-kame.gif" alt="Goku Kamehameha" width="192" />
      <br />
      <strong>悟空使用龜派氣功</strong>
      <br />
      <code>help me to use $generate2dsprite to create a goku is attacking with Kamehameha</code>
    </td>
    <td align="center" width="33%">
      <img src="./src/naruto-rasengan.gif" alt="Naruto Rasengan" width="192" />
      <br />
      <strong>鳴人使用螺旋丸</strong>
      <br />
      <code>使用 $generate2dsprite 幫我做一個鳴人使用螺旋丸的元素</code>
    </td>
  </tr>
</table>

### Spell Bundle / Cast、Projectile、Impact

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

Prompt：

```text
Use  $generate2dsprite to create a fire mage cast animation with projectile and impact.
```

### Game Sprite / 四方向行走

<table>
  <tr>
    <td align="center" width="25%">
      <img src="./src/down.gif" alt="Samurai walking down" width="144" />
      <br />
      <strong>下</strong>
    </td>
    <td align="center" width="25%">
      <img src="./src/left.gif" alt="Samurai walking left" width="144" />
      <br />
      <strong>左</strong>
    </td>
    <td align="center" width="25%">
      <img src="./src/right.gif" alt="Samurai walking right" width="144" />
      <br />
      <strong>右</strong>
    </td>
    <td align="center" width="25%">
      <img src="./src/up.gif" alt="Samurai walking up" width="144" />
      <br />
      <strong>上</strong>
    </td>
  </tr>
</table>

Prompt：

```text
Use Generate 2D Sprite to create a top-down 4x4 player_sheet for a wandering young samurai with a red scarf and short katana.
Make a four-direction walk sprite sheet with 4 frames per direction.
Row order: down, left, right, up.
Same character, same outfit, same proportions, same pixel scale in every frame.
Solid #FF00FF background.
Each frame must fit fully inside its cell, with clear margin on all sides.
Retro JRPG pixel-art style.
```

### 參考圖到 Sprite

<table>
  <tr>
    <td align="center" width="35%">
      <img src="./src/ref1.jpg" alt="Reference crocodile" width="180" />
      <br />
      <strong>參考圖</strong>
    </td>
    <td align="center" width="65%">
      <img src="./src/croc_stone_play.gif" alt="Crocodile playing with a stone" width="220" />
      <br />
      <strong>生成結果</strong>
      <br />
      <code>幫我使用 $generate2dsprite 做一個這隻鱷魚玩手上石頭的元素</code>
    </td>
  </tr>
  <tr>
    <td align="center" width="35%">
      <img src="./src/ref2.jpg" alt="Reference male character" width="180" />
      <br />
      <strong>參考圖</strong>
    </td>
    <td align="center" width="65%">
      <img src="./src/cz.gif" alt="Male character teaching animation" width="220" />
      <br />
      <strong>生成結果</strong>
      <br />
      <code>Use  $generate2dsprite to create this male character teaching.</code>
    </td>
  </tr>
</table>

這是一個以 Codex 為核心的 2D sprite 生成 skill，用來產出可直接拿去做遊戲資產的 pixel art。

這個 repo 目前只放一個通用型 skill：

- [`skills/generate2dsprite`](./skills/generate2dsprite)

之所以先以 Codex 為主，是因為 Codex 本身就有內建 image generation，所以整個流程可以留在同一個 agent 內完成：

1. 由 agent 規劃資產類型與動畫形式。
2. 由 Codex 生成 raw sprite sheet。
3. 由本地 processor 做去背、切格、對齊、基本 QC，最後輸出透明 PNG / GIF。

目前這個 repo 的重點是通用型 2D 資產，不是整包遊戲 pack。

## 可以生成什麼

- Creature
- Character
- Player / NPC
- Spell cast
- Projectile
- Impact / explosion
- FX sheet
- 小型 bundle，例如 `unit_bundle`、`spell_bundle`、`combat_bundle`

## 為什麼先做 Codex 版本

因為 Codex 可以直接在同一個工作流內生圖，流程會乾淨很多：

- 不需要另外接 image API
- 不需要額外的 prompt builder service
- 不需要獨立 sprite backend
- 由同一個 agent 規劃資產
- 由本地 processor 負責可重現的後處理

這個 repo 的設計理念是：

- 美術規劃交給 agent
- 去背、切圖、對齊、輸出交給 deterministic processor

也就是說，agent 會決定：

- asset type
- action type
- bundle shape
- sheet layout
- frame count
- alignment strategy
- detached effects 要不要保留

Python script 不負責創意判斷，只負責穩定執行像素層級的處理。

## Repo 結構

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

## 安裝方式

### Option 1: Windows PowerShell

先 clone repo，安裝本地 processor 依賴，再把 skill 複製到 Codex skills 目錄：

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

安裝完後建議重新開一個新的 Codex session，讓 skill 重新載入。

## Python 依賴

本地後處理目前依賴：

- `Pillow`
- `numpy`

這些都列在 [`requirements.txt`](./requirements.txt)。

雖然 Codex 本身負責生圖，但你還是需要這些 Python 套件來完成：

- 洋紅背景去背
- 切格
- 主體 bbox 偵測
- 對齊與縮放
- 透明 PNG / GIF 輸出

## 建議 Prompt

### 基本用法

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

### Character / Monster 範例

```text
Use  $generate2dsprite to create Omegamon attack and right-move animation assets.
```

```text
Use  $generate2dsprite to create a golden divine boar 2x2 idle animation.
```

```text
Use  $generate2dsprite to create a Naruto-style rasengan cast sheet in 2x3.
```

## 會輸出什麼

一般 sheet 類型的輸出通常包含：

- `raw-sheet.png`
- `raw-sheet-clean.png`
- `sheet-transparent.png`
- frame PNG
- `animation.gif`
- `prompt-used.txt`
- `pipeline-meta.json`

如果是 player walk sheet，通常還會額外輸出各方向 strip 與各方向 GIF。

## 備註

- Prompt 越清楚，結果通常越穩。最好明確描述視角、動作、動畫型態與尺寸。
- 大型 creature 通常比較適合 `3x3 idle`。
- 小型 spell / projectile 通常比較適合 `1x4`、`2x2` 或 `2x3`。
- 如果要商用，建議優先使用原創角色或你自己持有權利的 IP。

## 授權

MIT。請見 [LICENSE](./LICENSE)。
