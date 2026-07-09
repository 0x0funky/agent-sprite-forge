# Agent Sprite Forge

语言：[English](./README.md) | [繁體中文](./README.zh-TW.md) | [简体中文](./README.zh-CN.md) | [日本語](./README.ja.md) | [한국어](./README.ko.md)

<p align="center">
  <img src="./src/banner.png" alt="Agent Sprite Forge banner" width="900" />
</p>

<p align="center">
  <strong>面向 Codex 的 2D 游戏资产技能：生成可用的角色精灵、分层地图，以及能交给游戏引擎继续编辑的原型素材。</strong>
</p>

<p align="center">
  用自然语言描述需求，Codex 负责规划资产流程，用内置图像生成产出原始视觉，再用本地处理器去背、切格、对齐、验证，并导出给 Godot、Unity 或普通 2D 游戏项目使用。
</p>

<p align="center">
  <a href="#showcase">Showcase</a> |
  <a href="#included-skills">Skills</a> |
  <a href="#install">Install</a> |
  <a href="#suggested-prompts">Prompts</a> |
  <a href="#star-history">Star History</a>
</p>

## 有什么不同

Agent Sprite Forge 不是一组 prompt 模板。它是一套 Codex-first 的 2D 游戏资产工作流：agent 先判断需要什么资产、图像生成负责创作原始视觉，本地脚本只做可重复的清理、切割、对齐、验证和导出。

<table>
  <tr>
    <td width="25%"><strong>精灵表</strong><br />角色、怪物、NPC、道具、攻击、法术、投射物、命中特效、idle、walk，以及参考图驱动的变体。</td>
    <td width="25%"><strong>分层地图</strong><br />ground-only base、dressed reference、prop pack、透明 props、y-sort 摆放、碰撞、区域和预览图。</td>
    <td width="25%"><strong>引擎交付</strong><br />Godot 场景、可编辑 TileMapLayer、分离式 props、遇怪草丛、碰撞体、出口和 debug player。</td>
    <td width="25%"><strong>本地清理</strong><br />洋红去背、frame extraction、alignment、透明 PNG/GIF 导出、prop pack 切割和 QA metadata。</td>
  </tr>
</table>

## Showcase

### Engine-Ready Prototypes

这些案例使用 Codex 和 `agent-sprite-forge` 工作流组装，重点是完整闭环：生成资产、结构化场景数据，以及可玩的 prototype wiring。

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/summon-survivors-game-preview1.png" alt="Summon Survivors Unity WebGL gameplay" width="420" />
      <br />
      <strong>Summon Survivors - Unity WebGL</strong>
      <br />
      生成地图、主角 sheet、召唤物、进化、敌人、Boss、拾取物、HUD、FX、升级选项和 WebGL 部署。
      <br />
      <a href="https://summon-survivors.vercel.app/">Play build</a> | <a href="https://drive.google.com/file/d/1TL7qRX95przTToZILVQ1EFwEXm3flB6t/view?usp=sharing">Build conversation</a>
    </td>
    <td align="center" width="50%">
      <img src="./src/kingdomrush-forest-pass.png" alt="Forest Pass Defense Godot tower-defense map" width="420" />
      <br />
      <strong>Forest Pass Defense - Godot Tower Defense</strong>
      <br />
      Godot 4 塔防原型，包含地图、分离式 props、塔位、塔、敌人 sheet、Boss、飞行敌、波次、HUD、建造 / 升级 / 出售流程和投射物规则。
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/godot-editor.png" alt="Generate2DMap Godot editor scene" width="420" />
      <br />
      <strong>Editable RPG Map - Godot TileMap</strong>
      <br />
      图像生成 tileset 和 prop sheet，再接进可编辑 <code>TileMapLayer</code>、<code>Sprite2D</code> props、遇怪草丛 <code>Area2D</code>、<code>StaticBody2D</code> 碰撞、出口、metadata 和 debug player/camera。
    </td>
    <td align="center" width="50%">
      <img src="./src/neon-breach.png" alt="Neon Breach cyberpunk side-scroller" width="420" />
      <br />
      <strong>Neon Breach - Cyberpunk Side-Scroller</strong>
      <br />
      使用生成的角色、攻击、地图和 gameplay assets 组装出的可玩横向卷轴 prototype。
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/pokemonlike2.png" alt="Sengoku Era JavaScript RPG starter selection" width="420" />
      <br />
      <strong>Sengoku Era - JavaScript monster-taming RPG</strong>
      <br />
      浏览器 RPG prototype，包含生成角色、初始怪物选择、地图流程和战斗 UI。
      <br />
      <a href="https://sengoku-era.vercel.app/">Play build</a>
    </td>
    <td align="center" width="50%">
      <img src="./src/pokemonlike.png" alt="Sengoku Era JavaScript RPG battle scene" width="420" />
      <br />
      <strong>Starter selection and battle loop</strong>
      <br />
      用 skill workflow 生成 sprite、monster、battle 和 map assets 后完成的小型 JavaScript 游戏展示。
    </td>
  </tr>
</table>

### Sprite Sheets And FX

当你需要动画单位、玩家角色、怪物、props、spell bundles、projectile/impact FX，或参考图驱动的变体时，使用 `$generate2dsprite`。

<table>
  <tr>
    <td align="center" width="25%"><img src="./src/goku-kame.gif" alt="Goku Kamehameha sprite animation" width="170" /><br /><strong>Text to sprite</strong><br />从自然语言生成攻击动画。</td>
    <td align="center" width="25%"><img src="./src/naruto-rasengan.gif" alt="Naruto Rasengan sprite animation" width="170" /><br /><strong>Character action</strong><br />紧凑的 2D 动作 sheet 和透明导出。</td>
    <td align="center" width="25%"><img src="./src/cast.gif" alt="Fire mage cast animation" width="150" /><br /><strong>Spell cast</strong><br />适合 bundle 的施法动画。</td>
    <td align="center" width="25%"><img src="./src/projectile.gif" alt="Fire mage projectile animation" width="150" /><br /><strong>Projectile</strong><br />匹配的 projectile / impact workflow。</td>
  </tr>
</table>

### Layered RPG Map Pipeline

当你需要地图而不是单独 sprite 时，使用 `$generate2dmap`。可读性较高的 layered raster map 目前推荐 clean hand-painted HD game-map style：先生成 ground-only base，再生成 dressed reference，接着生成 prop pack，最后做透明 prop extraction 和 layered preview composition。

<table>
  <tr>
    <td align="center" width="33%"><img src="./src/cyber-canal-base.png" alt="Ground-only cyberpunk canal RPG base map" width="300" /><br /><strong>Ground-only base</strong></td>
    <td align="center" width="33%"><img src="./src/cyber-canal-dressed-reference.png" alt="Dressed cyberpunk canal reference map" width="300" /><br /><strong>Dressed reference</strong></td>
    <td align="center" width="33%"><img src="./src/cyber-canal-prop-pack.png" alt="Generated 3x3 cyberpunk canal prop pack" width="300" /><br /><strong>3x3 prop pack</strong></td>
  </tr>
</table>

<p align="center">
  <img src="./src/cyber-canal-layered-preview.png" alt="Layered cyberpunk canal RPG map preview" width="760" />
  <br />
  <strong>Flattened layered RPG map preview</strong>
</p>

```text
layered_raster + y_sorted_props + precise_shapes + trigger_zones + raw_canvas
```

### Godot Editable TileMap Export

`$generate2dmap` 也可以输出可编辑 Godot map project，而不是只有一张 flattened image。这个 showcase 使用图像生成的 tileset 和 3x3 prop sheet，再接入 Godot 4.5 scene。

<p align="center">
  <img src="./src/godot-editor.png" alt="Generate2DMap Godot editor scene with editable TileMapLayer and nodes" width="860" />
  <br />
  <strong>Godot editor scene: editable layers, props, zones, collision, exits, and debug player</strong>
</p>

Godot 输出可以包含可编辑 `TileMapLayer` nodes、独立 `Sprite2D` props、遇怪草丛 `Area2D` zones、`StaticBody2D` collision blockers、exit `Area2D` zones，以及 debug player/camera。

```text
image_gen tileset + prop_pack_3x3 + layered_tilemap + separate_props + trigger_zones + Godot_TileMap
```

## Included Skills

| Skill | 用途 | 输出 | 运行环境 |
| --- | --- | --- | --- |
| [`generate2dsprite`](./skills/generate2dsprite) | Sprites、animation sheets、props、spell bundles、FX、参考图变体、固定 frame sheet 的 layout guide | raw sheet、cleaned transparent sheet、frames、GIFs、metadata | Codex / Grok |
| [`generate2dmap`](./skills/generate2dmap) | baked maps、layered raster maps、clean HD RPG maps、prop packs、collision/zones、Godot-editable scenes、side-scroll/parallax scenes | base map、dressed/stage reference、prop pack、extracted props、preview、scene metadata | Codex / Grok |
| [`video2dsprite`](./skills/video2dsprite) | **视频驱动的更密动作 sprite**：静帧 → `image_to_video` → 抽帧 → 品红抠图 → 多密度 strip/GIF | video、frames、8/16/24/48 sprites | **仅 Grok Build** |

> **`$video2dsprite` 仅 Grok Build 可用**（需要 `image_to_video`）。安装到 `~/.grok/skills`。追求硬像素生产 sheet 仍优先 `$generate2dsprite`。

`$generate2dmap` 只有在地图流程需要可复用透明 props 时，才会搭配 `$generate2dsprite`。小型环境 props 可以批成 `2x2`、`3x3` 或 `4x4` prop packs，再切成独立透明 props。平台、地板、桥、墙、门和长条 hazard 这类碰撞关键物件，通常应该单独生成或用 tile/object layer 表达。

## How It Works

1. 用户请 Codex 生成 sprite、prop pack、map 或 engine-ready prototype。
2. Agent 判断 asset type、action、bundle shape、sheet layout、frame count、style 和 alignment strategy。
3. 内置图像生成产出 raw visual asset。
4. 本地脚本做 deterministic post-processing：chroma-key cleanup、despill、frame extraction、alignment、prop-pack slicing、GIF/PNG export 和 validation metadata。
5. 对地图和 prototype，Codex 也可以组装 placement metadata、collision、trigger zones、Godot scenes 或 Unity project wiring。

脚本不是创意大脑。Agent 负责视觉和 pipeline 决策；Python 工具只做可重复的像素处理和导出。

## Install

### Windows PowerShell

```powershell
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd .\agent-sprite-forge
python -m pip install -r .\requirements.txt
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.codex\skills" | Out-Null
Copy-Item -Recurse -Force `
  ".\skills\*" `
  "$env:USERPROFILE\.codex\skills\"
```

### macOS / Linux

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd ./agent-sprite-forge
python3 -m pip install -r ./requirements.txt
mkdir -p ~/.codex/skills
cp -R ./skills/* ~/.codex/skills/
```

安装后请重开 Codex session，让 skills 被干净载入。

## Suggested Prompts

### Sprite

```text
Use $generate2dsprite to create a 3x3 idle for an ultimate earth titan.
```

```text
Use $generate2dsprite to create a side-view lightning knight attack animation.
```

```text
Use $generate2dsprite to create a wizard spell bundle with cast, projectile, and impact sprites.
```

### Map

```text
Use $generate2dmap to create a Godot-editable RPG map with separated props, encounter grass Area2D zones, collision StaticBody2D blockers, exit zones, and a debug player scene.
```

```text
Use $generate2dmap to create a playable side_scroll_mode platformer stage with parallax layers, stage-reference, separate platform_objects, collision metadata, camera bounds, and a stage-preview.
```

## What You Get

典型 sprite sheet 输出：

- `raw-sheet.png`
- `raw-sheet-clean.png`
- `sheet-transparent.png`
- frame PNGs
- `animation.gif`
- `prompt-used.txt`
- `pipeline-meta.json`

地图输出取决于 pipeline：

- Single baked map：完整地图图像、可选 prompt file、可选 collision metadata。
- Layered raster map：base map、dressed reference、prop folders 或 prop-pack extraction manifest、prop placement metadata、collision/zones metadata、flattened layered preview。
- Side-scroll map：parallax layers、stage reference、separate platform/object assets、objects/collision metadata、camera bounds、stage preview。
- Godot editable map：tileset/prop assets、scene files、layer metadata、collision/zones、exits、debug player setup。

## Notes

- 最好的结果来自明确指定视角、动作和动作节奏的 prompt。
- 大型 creature 通常更适合 `3x3 idle`。
- 小型 spell、projectile 和 impact 通常适合 `2x2` 或 `2x3`。
- 主角攻击、射击、施法动作建议 body-only；大范围 slash、muzzle flash、projectile、impact 独立生成成 FX。
- 商业项目请优先使用原创角色或你拥有权利的 IP。

## Star History

<a href="https://www.star-history.com/?repos=0x0funky%2Fagent-sprite-forge&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=0x0funky/agent-sprite-forge&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=0x0funky/agent-sprite-forge&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=0x0funky/agent-sprite-forge&type=date&legend=top-left" />
 </picture>
</a>

## License

MIT. See [LICENSE](./LICENSE).
