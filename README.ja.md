# Agent Sprite Forge

言語：[English](./README.md) | [繁體中文](./README.zh-TW.md) | [简体中文](./README.zh-CN.md) | [日本語](./README.ja.md) | [한국어](./README.ko.md)

<p align="center">
  <img src="./src/banner.png" alt="Agent Sprite Forge banner" width="900" />
</p>

<p align="center">
  <strong>Codex 向けの 2D ゲームアセット Skill。ゲームで使えるスプライト、レイヤー化されたマップ、エンジンへ渡せるプロトタイプ素材を生成します。</strong>
</p>

<p align="center">
  自然言語で依頼すると、Codex がアセット制作パイプラインを設計し、内蔵画像生成で元画像を作り、ローカル処理で背景除去、フレーム分割、整列、検証、Godot / Unity / 通常の 2D ゲーム向けエクスポートを行います。
</p>

<p align="center">
  <a href="#showcase">Showcase</a> |
  <a href="#included-skills">Skills</a> |
  <a href="#install">Install</a> |
  <a href="#suggested-prompts">Prompts</a> |
  <a href="#star-history">Star History</a>
</p>

## 何が違うのか

Agent Sprite Forge は単なる prompt 集ではありません。Codex-first の 2D ゲームアセット制作ワークフローです。Agent が必要なアセットと手順を判断し、画像生成がビジュアルを作り、決定論的なローカルスクリプトが再利用可能なゲーム素材へ整えます。

<table>
  <tr>
    <td width="25%"><strong>スプライトシート</strong><br />キャラクター、モンスター、NPC、props、攻撃、魔法、投射物、impact、idle、walk、参照画像ベースの派生。</td>
    <td width="25%"><strong>レイヤー化マップ</strong><br />ground-only base、dressed reference、prop pack、透明 props、y-sort 配置、collision、zones、preview。</td>
    <td width="25%"><strong>エンジン連携</strong><br />Godot scenes、編集可能な TileMapLayer、分離 props、エンカウント草むら、collision bodies、exits、debug player。</td>
    <td width="25%"><strong>ローカル処理</strong><br />マゼンタ背景除去、frame extraction、alignment、透明 PNG/GIF 出力、prop-pack slicing、QA metadata。</td>
  </tr>
</table>

## Showcase

### Engine-Ready Prototypes

以下は Codex と `agent-sprite-forge` workflow で組み立てた例です。生成アセット、構造化されたシーンデータ、実際に遊べる prototype wiring までを示します。

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/summon-survivors-game-preview1.png" alt="Summon Survivors Unity WebGL gameplay" width="420" />
      <br />
      <strong>Summon Survivors - Unity WebGL</strong>
      <br />
      マップ、hero sheets、summons、evolutions、enemies、bosses、pickups、HUD、FX、level-up choices、WebGL deployment を生成。
      <br />
      <a href="https://summon-survivors.vercel.app/">Play build</a> | <a href="https://drive.google.com/file/d/1TL7qRX95przTToZILVQ1EFwEXm3flB6t/view?usp=sharing">Build conversation</a>
    </td>
    <td align="center" width="50%">
      <img src="./src/kingdomrush-forest-pass.png" alt="Forest Pass Defense Godot tower-defense map" width="420" />
      <br />
      <strong>Forest Pass Defense - Godot Tower Defense</strong>
      <br />
      Godot 4 のタワーディフェンス prototype。マップ、分離 props、tower slots、towers、enemy sheets、boss/flying enemies、waves、HUD、build/upgrade/sell flow、projectiles を含みます。
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/godot-editor.png" alt="Generate2DMap Godot editor scene" width="420" />
      <br />
      <strong>Editable RPG Map - Godot TileMap</strong>
      <br />
      画像生成した tileset と prop sheet を、編集可能な <code>TileMapLayer</code>、<code>Sprite2D</code> props、草むら <code>Area2D</code>、<code>StaticBody2D</code> collision、exits、metadata、debug player/camera に接続。
    </td>
    <td align="center" width="50%">
      <img src="./src/neon-breach.png" alt="Neon Breach cyberpunk side-scroller" width="420" />
      <br />
      <strong>Neon Breach - Cyberpunk Side-Scroller</strong>
      <br />
      生成された character、attack、map、gameplay assets を使った、プレイ可能な横スクロール prototype。
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/pokemonlike2.png" alt="Sengoku Era JavaScript RPG starter selection" width="420" />
      <br />
      <strong>Sengoku Era - JavaScript monster-taming RPG</strong>
      <br />
      生成キャラクター、starter selection、map flow、battle UI を含むブラウザ RPG prototype。
      <br />
      <a href="https://sengoku-era.vercel.app/">Play build</a>
    </td>
    <td align="center" width="50%">
      <img src="./src/pokemonlike.png" alt="Sengoku Era JavaScript RPG battle scene" width="420" />
      <br />
      <strong>Starter selection and battle loop</strong>
      <br />
      sprite、monster、battle、map assets を skill workflow で生成して組み立てたコンパクトな JavaScript game showcase。
    </td>
  </tr>
</table>

### Sprite Sheets And FX

アニメーションユニット、プレイヤーキャラクター、モンスター、props、spell bundles、projectile/impact FX、参照画像ベースの派生が必要な場合は `$generate2dsprite` を使います。

<table>
  <tr>
    <td align="center" width="25%"><img src="./src/goku-kame.gif" alt="Goku Kamehameha sprite animation" width="170" /><br /><strong>Text to sprite</strong><br />自然言語から攻撃アニメーションを生成。</td>
    <td align="center" width="25%"><img src="./src/naruto-rasengan.gif" alt="Naruto Rasengan sprite animation" width="170" /><br /><strong>Character action</strong><br />透明出力つきのコンパクトな 2D action sheet。</td>
    <td align="center" width="25%"><img src="./src/cast.gif" alt="Fire mage cast animation" width="150" /><br /><strong>Spell cast</strong><br />bundle に使いやすい cast animation。</td>
    <td align="center" width="25%"><img src="./src/projectile.gif" alt="Fire mage projectile animation" width="150" /><br /><strong>Projectile</strong><br />対応する projectile / impact workflow。</td>
  </tr>
</table>

### Layered RPG Map Pipeline

スプライト単体ではなくマップが必要な場合は `$generate2dmap` を使います。読みやすい layered raster map では、clean hand-painted HD game-map style を推奨します。ground-only base、dressed reference、prop pack、transparent prop extraction、layered preview composition の順に進みます。

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

`$generate2dmap` は、1 枚の flattened image だけではなく、編集可能な Godot map project も出力できます。この showcase では、画像生成した tileset と 3x3 prop sheet を Godot 4.5 scene に接続しています。

<p align="center">
  <img src="./src/godot-editor.png" alt="Generate2DMap Godot editor scene with editable TileMapLayer and nodes" width="860" />
  <br />
  <strong>Godot editor scene: editable layers, props, zones, collision, exits, and debug player</strong>
</p>

Godot 出力には、編集可能な `TileMapLayer` nodes、独立した `Sprite2D` props、encounter grass `Area2D` zones、`StaticBody2D` collision blockers、exit `Area2D` zones、debug player/camera を含められます。

```text
image_gen tileset + prop_pack_3x3 + layered_tilemap + separate_props + trigger_zones + Godot_TileMap
```

## Included Skills

| Skill | 用途 | 出力 | 実行環境 |
| --- | --- | --- | --- |
| [`generate2dsprite`](./skills/generate2dsprite) | sprites、animation sheets、props、spell bundles、FX、reference variants、fixed-frame sheets 用 layout guides | raw sheet、cleaned transparent sheet、frames、GIFs、metadata | Codex / Grok |
| [`generate2dmap`](./skills/generate2dmap) | baked maps、layered raster maps、clean HD RPG maps、prop packs、collision/zones、Godot-editable scenes、side-scroll/parallax scenes | base map、dressed/stage reference、prop pack、extracted props、preview、scene metadata | Codex / Grok |
| [`video2dsprite`](./skills/video2dsprite) | **動画からの密なモーション sprite**：静止画 → `image_to_video` → フレーム抽出 → マゼンタ chroma → 多密度 strip/GIF | video、frames、8/16/24/48 sprites | **Grok Build 専用** |

> **`$video2dsprite` は Grok Build 専用**（`image_to_video` が必要）。`~/.grok/skills` にインストール。硬い pixel の本番 sheet は `$generate2dsprite` を優先。

`$generate2dmap` は、選ばれた map pipeline が再利用可能な透明 props を必要とする場合だけ `$generate2dsprite` を併用します。小さな環境 props は `2x2`、`3x3`、`4x4` の prop pack にできます。一方、platform、floor、bridge、wall、door、long hazard のような collision-critical object は、個別生成または tile/object layer として扱うのが安全です。

## How It Works

1. ユーザーが Codex に sprite、prop pack、map、engine-ready prototype を依頼します。
2. Agent が asset type、action、bundle shape、sheet layout、frame count、style、alignment strategy を決めます。
3. 内蔵画像生成が raw visual asset を作ります。
4. ローカルスクリプトが deterministic post-processing を行います：chroma-key cleanup、despill、frame extraction、alignment、prop-pack slicing、GIF/PNG export、validation metadata。
5. マップや prototype では、placement metadata、collision、trigger zones、Godot scenes、Unity project wiring も Codex が組み立てられます。

スクリプトは創造部分を担当しません。視覚と pipeline の判断は Agent が行い、Python tools は再現可能な pixel/export 処理だけを担当します。

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

インストール後は、新しい Codex session を開始して skills を読み込み直してください。

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

典型的な sprite sheet output：

- `raw-sheet.png`
- `raw-sheet-clean.png`
- `sheet-transparent.png`
- frame PNGs
- `animation.gif`
- `prompt-used.txt`
- `pipeline-meta.json`

map output は pipeline によって変わります：

- Single baked map：完成した map image、任意の prompt file、任意の collision metadata。
- Layered raster map：base map、dressed reference、prop folders または prop-pack extraction manifest、prop placement metadata、collision/zones metadata、flattened layered preview。
- Side-scroll map：parallax layers、stage reference、separate platform/object assets、objects/collision metadata、camera bounds、stage preview。
- Godot editable map：tileset/prop assets、scene files、layer metadata、collision/zones、exits、debug player setup。

## Notes

- 視点、動作、モーションの雰囲気を明確に書くほど結果が安定します。
- 大型 creature には `3x3 idle` が向いています。
- 小さな spell、projectile、impact は `2x2` または `2x3` が向いています。
- hero の attack/shoot/cast は body-only を推奨します。大きな slash、muzzle flash、projectile、impact は別 FX として生成します。
- 商用利用では、オリジナルキャラクターまたは権利を持つ IP を優先してください。

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
