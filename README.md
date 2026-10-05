# Agent Sprite Forge

Languages: [English](./README.md) | [繁體中文](./README.zh-TW.md) | [简体中文](./README.zh-CN.md) | [日本語](./README.ja.md) | [한국어](./README.ko.md)

<p align="center">
  <img src="./src/banner.png" alt="Agent Sprite Forge banner" width="900" />
</p>

<p align="center">
  <strong>Agent skills for game-ready 2D sprites, code-drawn pixel art, AI video animation and playable maps.</strong>
</p>

<p align="center">
  Ask in natural language in Codex, Claude Code or Grok. The agent plans the asset, gets the art from the best route it has (code art, the host's image tool, your local Codex or Grok CLI, or a paid API with your consent), then deterministic Python tools key, slice, register, check and export it for Godot, Tiled, LDtk, Aseprite or a web game.
</p>

<p align="center">
  <a href="#whats-new-in-040">What's new</a> ·
  <a href="#showcase">Showcase</a> ·
  <a href="#included-skills">Skills</a> ·
  <a href="#install">Install</a> ·
  <a href="#tools">Tools</a> ·
  <a href="#suggested-prompts">Prompts</a>
</p>

<!-- PROMO -->

## What's new in 0.4.0

One integrated release; the full list, with every breaking default and its legacy switch, is in the [CHANGELOG](./CHANGELOG.md).

- **Five sibling skills.** `generate2dsprite`, `generate2dmap`, `video2dsprite`, `generate2dmedia` and the new **`codeart2d`**, which draws game art from code with no image model.
- **Claude Code plugin.** Install the five skills with `claude plugin marketplace add` (below). Codex and Grok keep the folder install, now with a backup and drift check.
- **Local agent first.** Images come from the host's own tool (Codex `image_gen`), then your local Codex CLI or Grok CLI (one-shot image mode), video from the Grok CLI in ACP mode. A local route is used only after `forge_doctor` has VERIFIED it for the installed version. Every call goes into a project ledger with a session cap (8 images and 2 videos per 12 hours by default). The paid OpenAI/xAI APIs run only with your consent for each request, with budget caps and a duplicate guard.
- **Soft matte, no purple fringe.** A shared keyer with a soft matte, pocket removal, auto despill and temporal hysteresis. On the Ryo test clip: visible fringe 8,504 to 0 px per frame, enclosed key pockets in 16 of 145 frames to 0, matte flicker 9.6 to 3.5 flips per frame pair.
- **Loops chosen by measurement.** `gait_loop.py` finds walk and run cycles (half-period guard, unusable frames, drift) and idle loops; `retime.py` times attacks to their impact and hold on a 60 Hz tick grid.
- **Registration by construction.** `prepare_i2v_input.py` places the master on the provider canvas at a recorded transform; `register_clip.py` undoes exactly that transform, so body scale no longer pumps between frames.
- **Engine export 3.0 and runtime.** `engine_export.py` writes animation.json 3.0, a PNG atlas, VP9-alpha WebM and packed-alpha MP4 for iPhone behind a key-residue gate, and `verify` decodes every file. `export_engine.py` writes Aseprite JSON and Godot SpriteFrames/Sprite3D. `forge-runtime.mjs` adds distance-driven walks, hit-stop and transitions; a WebGL compositor plays packed alpha.
- **Playable maps.** `map_bundle.v2`, `map_nav.py` (collision and reachability from data, portals) and exports to Tiled, Godot 4 and LDtk, plus a single-file walkable HTML preview.
- **HD-2D.** `stage.v1` with a battle layout solved at 4:3 to 9:19.5, light and atmosphere extraction, plate-variant locality checks, and masked scene motion with decoded-file loop QA.
- **Code-art pipeline.** PixelSpec sprites with palette variants, portable SVG with a renderer doctor, rigs with FK/IK and planted feet, FX with a JS runtime, seam-proven autotiles (Wang-16, three-material, blob-47), playable layouts with reachability, parallax and ambient plates.
- **`forge_doctor.py`.** One capability check per session: packages, ffmpeg, console encoding, install drift, API keys (presence only) and the Codex/Grok readiness ladder. It never reads credentials and spends nothing.

## What Makes It Different

Agent Sprite Forge is not a folder of prompts. The agent decides the plan and the art source; deterministic scripts turn the art into reusable game assets and prove what they can with numbers.

<table>
  <tr>
    <td width="25%">
      <strong>Sprites and animation</strong><br />
      Characters, monsters, props, attacks, spells, projectiles, impacts, idles and walks, from sheets, code or video.
    </td>
    <td width="25%">
      <strong>Maps and scenes</strong><br />
      Tiles and autotiles, prop packs, layered and HD-2D scenes, collision, navigation, exits and a walkable preview.
    </td>
    <td width="25%">
      <strong>Engine handoff</strong><br />
      Aseprite JSON, Godot SpriteFrames/Sprite3D/TileMapLayer, Tiled, LDtk, animation.json with WebM and packed MP4, JS runtimes.
    </td>
    <td width="25%">
      <strong>Measured cleanup</strong><br />
      Soft chroma key, slicing, registration, palettes, loop selection, QA envelopes and review sheets.
    </td>
  </tr>
</table>

## Showcase

### Code Art (no image model)

Rendered by the `codeart2d` tools from the specs in [`skills/codeart2d/examples`](./skills/codeart2d/examples). Code-drawn, no image model, no quota.

<table>
  <tr>
    <td align="center" width="25%">
      <img src="./src/codeart/hero-walk.gif" alt="Code-drawn pixel hero walk cycle" width="192" />
      <br />
      <strong>Rigged walk, planted feet</strong>
      <br />
      <code>rig_animate.py</code>: FK/IK with a ground constraint, 0 px foot slide, exact palette.
    </td>
    <td align="center" width="45%">
      <img src="./src/codeart/fx-set.gif" alt="Code-drawn slash, impact ring, dust and projectile FX" width="420" />
      <br />
      <strong>FX set</strong>
      <br />
      <code>fx_build.py</code>: slash, impact, dust and projectile with hit events and an fx.v1 JS runtime.
    </td>
    <td align="center" width="30%">
      <img src="./src/codeart/meadow-layout.png" alt="Code-made top-down meadow map with roads, pond, houses and trees" width="320" />
      <br />
      <strong>Playable layout</strong>
      <br />
      <code>autotile_build.py</code> + <code>layout_build.py</code>: seam-proven tiles, collision and every exit reachable.
    </td>
  </tr>
</table>

<!-- LIVE:codex-fox -->

### Engine-Ready Prototypes

These examples were assembled with Codex using `agent-sprite-forge` workflows. They show the full loop: generated assets, structured scene data and playable prototype wiring.

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/summon-survivors-game-preview1.png" alt="Summon Survivors Unity WebGL gameplay" width="420" />
      <br />
      <strong>Summon Survivors — Unity WebGL</strong>
      <br />
      Generated map art, hero sheets, summons, evolutions, enemies, bosses, pickups, HUD, FX, level-up choices, and WebGL deployment.
      <br />
      <a href="https://summon-survivors.vercel.app/">Play build</a> · <a href="https://drive.google.com/file/d/1TL7qRX95przTToZILVQ1EFwEXm3flB6t/view?usp=sharing">Build conversation</a>
    </td>
    <td align="center" width="50%">
      <img src="./src/kingdomrush-forest-pass.png" alt="Forest Pass Defense Godot tower-defense map" width="420" />
      <br />
      <strong>Forest Pass Defense — Godot Tower Defense</strong>
      <br />
      A Godot 4 prototype with map, separated props, tower slots, towers, enemy sheets, boss/flying enemies, waves, HUD, build/upgrade/sell flow, projectiles, and targeting rules.
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/godot-editor.png" alt="Generate2DMap Godot editor scene" width="420" />
      <br />
      <strong>Editable RPG Map — Godot TileMap</strong>
      <br />
      Image-generated tileset and prop sheet wired into editable <code>TileMapLayer</code>, <code>Sprite2D</code> props, encounter grass <code>Area2D</code>, <code>StaticBody2D</code> collision, exits, metadata, and debug player/camera.
    </td>
    <td align="center" width="50%">
      <img src="./src/neon-breach.png" alt="Neon Breach cyberpunk side-scroller" width="420" />
      <br />
      <strong>Neon Breach — Cyberpunk Side-Scroller</strong>
      <br />
      A playable side-scroller prototype built around generated character, attack, map, and gameplay assets.
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/pokemonlike2.png" alt="Sengoku Era JavaScript RPG starter selection" width="420" />
      <br />
      <strong>Sengoku Era — JavaScript monster RPG</strong>
      <br />
      A browser-based RPG prototype with generated characters, starter selection, map flow, and battle UI.
      <br />
      <a href="https://sengoku-era.vercel.app/">Play build</a>
    </td>
    <td align="center" width="50%">
      <img src="./src/pokemonlike.png" alt="Sengoku Era JavaScript RPG battle scene" width="420" />
      <br />
      <strong>Starter selection and battle loop</strong>
      <br />
      A compact JavaScript game showcase built from sprite, monster, battle, and map assets generated through the skill workflow.
    </td>
  </tr>
</table>

<details>
<summary>More Godot tower-defense output</summary>

<table>
  <tr>
    <td align="center" width="40%">
      <img src="./src/kingdomrush-enemy-roster.png" alt="Forest Pass Defense enemy roster" width="320" />
      <br />
      <strong>Enemy roster, including flyer and boss units</strong>
    </td>
    <td align="center" width="30%">
      <img src="./src/kingdomrush-tower-icons.png" alt="Forest Pass Defense tower icons" width="260" />
      <br />
      <strong>Tower lineup</strong>
    </td>
    <td align="center" width="30%">
      <img src="./src/kingdomrush-hud-icons.png" alt="Forest Pass Defense HUD icons" width="260" />
      <br />
      <strong>HUD and gameplay icons</strong>
    </td>
  </tr>
</table>

Godot prototype output includes:

- `scenes/ForestPass.tscn` with base map, separated props, enemy paths, tower slots, and HUD nodes.
- Six tower families with generated tower art and upgrade stages.
- Animated enemy sheets for ground units, flying units, and boss encounters.
- Wave, difficulty, tower catalog, collision, route, and tower-slot metadata.
- Runtime build, upgrade, sell, projectile, and targeting behavior connected in Godot.

```text
image_gen map + separated props + tower sheets + enemy animation sheets + HUD icons + Godot gameplay wiring
```

</details>

<details>
<summary>More Unity survivors-like output</summary>

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/summon-survivors-game-preview1.png" alt="Summon Survivors Unity WebGL gameplay with summons, enemies, pickups, HUD, and objective" width="420" />
      <br />
      <strong>Unity WebGL gameplay: summons, enemies, pickups, HUD, and objective flow</strong>
    </td>
    <td align="center" width="50%">
      <img src="./src/summon-survivors-game-preview2-levelup.png" alt="Summon Survivors Unity WebGL level-up menu" width="420" />
      <br />
      <strong>Level-up choices: summon unlocks, training, stats, and recovery</strong>
    </td>
  </tr>
</table>

Unity prototype output includes:

- `Assets/Survivors/Scenes/SummonSurvivors.unity` as the playable scene.
- `SurvivorContentDatabase.asset` connecting generated hero, summon, enemy, pickup, HUD, and FX sprites.
- Starter summon selection, survival objective, XP/coin pickups, level-up choices, summon training, and evolution flow.
- Enemy spawning pressure, boss timing, projectile attacks, area damage, health bars, and score tracking.
- WebGL build output under `Builds/WebGL` with Vercel deployment config.

```text
image_gen map + directional hero sheets + summon/evolution sheets + enemy sheets + FX/HUD icons + Unity runtime + WebGL deploy
```

</details>

### Sprite Sheets And FX

Use `$generate2dsprite` for animated units, playable characters, monsters, props, spell bundles, projectile/impact FX or reference-guided variants. Small pixel sprites (48 px visible height or less) and game FX are drawn with `codeart2d` first.

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/cast.gif" alt="Fire mage cast animation" width="150" />
      <br />
      <strong>Spell cast</strong>
      <br />
      Bundle-friendly cast animation.
    </td>
    <td align="center" width="50%">
      <img src="./src/projectile.gif" alt="Fire mage projectile animation" width="150" />
      <br />
      <strong>Projectile</strong>
      <br />
      Matching projectile / impact workflows.
    </td>
  </tr>
</table>

<table>
  <tr>
    <td align="center" width="25%">
      <img src="./src/down.gif" alt="Samurai walking down" width="132" />
      <br />
      <strong>Down</strong>
    </td>
    <td align="center" width="25%">
      <img src="./src/left.gif" alt="Samurai walking left" width="132" />
      <br />
      <strong>Left</strong>
    </td>
    <td align="center" width="25%">
      <img src="./src/right.gif" alt="Samurai walking right" width="132" />
      <br />
      <strong>Right</strong>
    </td>
    <td align="center" width="25%">
      <img src="./src/up.gif" alt="Samurai walking up" width="132" />
      <br />
      <strong>Up</strong>
    </td>
  </tr>
</table>

<table>
  <tr>
    <td align="center" width="35%">
      <img src="./src/ref1.jpg" alt="Reference crocodile" width="160" />
      <br />
      <strong>Reference</strong>
    </td>
    <td align="center" width="65%">
      <img src="./src/croc_stone_play.gif" alt="Crocodile playing with a stone" width="220" />
      <br />
      <strong>Reference-guided sprite animation</strong>
    </td>
  </tr>
  <tr>
    <td align="center" width="35%">
      <img src="./src/ref2.jpg" alt="Reference male character" width="160" />
      <br />
      <strong>Reference</strong>
    </td>
    <td align="center" width="65%">
      <img src="./src/cz.gif" alt="Male character teaching animation" width="220" />
      <br />
      <strong>Reference-guided character action</strong>
    </td>
  </tr>
</table>

<details>
<summary>Fan-art capability tests (not for commercial use)</summary>

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/goku-kame.gif" alt="Energy-beam attack sprite animation" width="170" />
      <br />
      <strong>Text to sprite</strong>
      <br />
      Attack animation from a plain-language request.
    </td>
    <td align="center" width="50%">
      <img src="./src/naruto-rasengan.gif" alt="Energy-sphere attack sprite animation" width="170" />
      <br />
      <strong>Character action</strong>
      <br />
      Compact 2D action sheet with transparent export.
    </td>
  </tr>
</table>

</details>

### Layered RPG Map Pipeline

Use `$generate2dmap` for maps instead of isolated sprites. For painted layered maps: ground-only base first, dressed reference second, prop pack third, then transparent prop extraction (anchored, prop_pack.v2) and a ground-line sorted preview.

<table>
  <tr>
    <td align="center" width="33%">
      <img src="./src/cyber-canal-base.png" alt="Ground-only cyberpunk canal RPG base map" width="300" />
      <br />
      <strong>Ground-only base</strong>
    </td>
    <td align="center" width="33%">
      <img src="./src/cyber-canal-dressed-reference.png" alt="Dressed cyberpunk canal reference map" width="300" />
      <br />
      <strong>Dressed reference</strong>
    </td>
    <td align="center" width="33%">
      <img src="./src/cyber-canal-prop-pack.png" alt="Generated 3x3 cyberpunk canal prop pack" width="300" />
      <br />
      <strong>3x3 prop pack</strong>
    </td>
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

### Godot Editable TileMap

An earlier showcase: an image-generated tileset and 3x3 prop sheet that the agent wired into a Godot 4.5 scene in the game project.

<p align="center">
  <img src="./src/godot-editor.png" alt="Generate2DMap Godot editor scene with editable TileMapLayer and nodes" width="860" />
  <br />
  <strong>Godot editor scene: editable layers, props, zones, collision, exits, and debug player</strong>
</p>

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/godot-meadow-layered-preview.png" alt="Godot meadow layered RPG map preview" width="360" />
      <br />
      <strong>Layered map preview</strong>
    </td>
    <td align="center" width="50%">
      <img src="./src/godot-meadow-debug-preview.png" alt="Godot meadow debug preview with collision and zones" width="360" />
      <br />
      <strong>Collision and zone debug overlay</strong>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/godot-meadow-tileset.png" alt="Image-generated Godot meadow tileset atlas" width="360" />
      <br />
      <strong>Image-generated tileset atlas</strong>
    </td>
    <td align="center" width="50%">
      <img src="./src/godot-meadow-prop-pack.png" alt="Image-generated 3x3 meadow prop pack" width="360" />
      <br />
      <strong>3x3 generated prop pack</strong>
    </td>
  </tr>
</table>

> **Engine imports: not yet verified.** In 0.4.0, map data is exported for Tiled (verified by re-rendering the exported files at 0 px difference) and, through `export_godot.py` and `export_ldtk.py`, for Godot 4.3+ and LDtk 1.5.3; those editor imports are verified at parse level only. The same holds for `export_engine.py` (Aseprite, Godot SpriteFrames/Sprite3D). Nobody has opened the 0.4.0 exports in the editors yet.

### Still → AI Video → Game-Ready Animation

`video2dsprite` takes motion from the host's image-to-video tool, the Grok CLI in ACP mode (once VERIFIED), the xAI API with your consent, or a clip you already have. Approve one master, prepare the input with a recorded transform, generate one action with a locked camera, then key, register, pick the loop or retime, package and verify. Keep sheets for exact pixel animation; video is not promised to be smaller, cheaper to decode or seamless.

<!-- LIVE:grok-video -->

#### Case study: Ryo run (16 denser frames)

Pipeline: **base still → image_to_video (6s) → chroma key → 16-frame strip**. Made with the earlier pipeline; the 0.4.0 keyer's numbers on this clip are in the [CHANGELOG](./CHANGELOG.md).

> GitHub READMEs do not reliably render `<video>`, so motion is shown as GIF; the MP4 files are in the repo.

| Base still | Motion (`image_to_video`) | Sprite result (16f) |
| --- | --- | --- |
| <img src="./src/video2dsprite-ryo/base.png" alt="Ryo base still on magenta" width="220" /> | <img src="./src/video2dsprite-ryo/run-6s-preview.gif" alt="Ryo 6s run video preview" width="240" /><br />[Download MP4](./src/video2dsprite-ryo/run-6s.mp4) | <img src="./src/video2dsprite-ryo/preview-16.gif" alt="Ryo 16-frame run loop" width="160" /> |

<p align="center">
  <img src="./src/video2dsprite-ryo/strip-16.png" alt="Ryo 16-frame run strip" width="720" /><br />
  <em>16-frame strip (feet-aligned, denser than a classic 6–8 pose sheet)</em>
</p>

Short skill intro (MP4): [intro.mp4](./src/video2dsprite-ryo/intro.mp4)

### Playable Game Prompt Examples

<details>
<summary>Cyberpunk side-scroller prompt</summary>

```text
use $generate2dsprite to create a 2D side-scrolling action game. It should include attack mechanics, map elements, and all the essential features. I would like you to design it, and all the necessary assets should be created using this skill. It needs to be an actually playable game, with a cyberpunk story setting.
```

</details>

<details>
<summary>Sengoku monster-RPG prototype</summary>

Link: <a href="https://sengoku-era.vercel.app/">Play the JavaScript browser build</a>

```text
Use $generate2dsprite to create a 2D monster-collecting RPG. You only need to build one scene for now. It must include a starter monster selection mechanic, a battle screen, and all basic gameplay functions. I would like you to design all the elements and the story, and you can also decide which game engine to use. Use this skill to create any assets you need. The story should be set in the Sengoku period.
```

</details>

## Included Skills

Keep the five skill folders together: they call each other's scripts by relative path.

| Skill | Use it for | Main outputs |
| --- | --- | --- |
| [`generate2dsprite`](./skills/generate2dsprite) | Characters, creatures, props and FX as stills, sheets or clips; packaging frames from any source | Registered frames, clips with ticks and events, palettes, QA, Aseprite/Godot exports |
| [`generate2dmap`](./skills/generate2dmap) | Top-down and side-scroll maps, tiles, prop kits, parallax, HD-2D plates | map_bundle.v2, collision and navigation checks, Tiled/Godot/LDtk exports, HTML preview, scene loops |
| [`video2dsprite`](./skills/video2dsprite) | Fluid motion from an approved still, or a supplied clip | Soft-keyed, registered, looped frames; animation.json 3.0, WebM alpha, packed MP4, PNG fallback |
| [`codeart2d`](./skills/codeart2d) | Code-drawn pixel sprites (up to 48 px; 49-64 px with consent), flat vector art, FX, autotiles, layouts, parallax | Exact-palette frames, clips, fx.v1 runtime, seam-proven tilesets, playable bundles, `codeart-meta.json` |
| [`generate2dmedia`](./skills/generate2dmedia) | The capability check, local Codex/Grok CLI routes and paid API calls with consent | Raw media with receipts, ledger lines, route proofs |

In Codex, `codeart2d` and `generate2dmedia` are explicit-only: the sprite and map skills route to them.

## Install

The skills need Python 3.10+ with numpy, Pillow and scipy (see [Requirements](#requirements)). Start a new agent session after installing so the skills load.

### Claude Code (plugin)

```bash
claude plugin marketplace add 0x0funky/agent-sprite-forge --sparse .claude-plugin skills
claude plugin install agent-sprite-forge@agent-sprite-forge
python -m pip install "numpy>=1.26" "Pillow>=10.1" "scipy>=1.11"
```

`--sparse` checks out only the manifest and the skills, not the showcase media. Copy fallback, from a clean clone (the installer copies every non-dot file of each skill folder, untracked files included):

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd agent-sprite-forge
python -m pip install -r requirements.txt
python tools/install_skills.py --apply --host claude
```

### Codex

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd agent-sprite-forge
python -m pip install -r requirements.txt
python tools/install_skills.py --apply --host codex
```

`--apply` backs up any installed copy, then writes a sha256 manifest; `python tools/install_skills.py --check --host codex` reports drift later. Plain copy fallback: `cp -R skills/* ~/.codex/skills/` (PowerShell: `Copy-Item -Recurse -Force .\skills\* "$env:USERPROFILE\.codex\skills\"`).

### Grok and other hosts

`python tools/install_skills.py --apply --host grok` (`~/.grok/skills`), `--host agents` (`~/.agents/skills`) or `--dest <skills folder>`.

### First run

Each skill starts with one capability check; you can run it yourself:

```bash
python skills/generate2dmedia/scripts/forge_doctor.py --host-tools none
```

It lists the art routes that work on this machine, local agent first. A Codex or Grok CLI that is installed but not yet verified is offered only after one verification call you approve.

## Requirements

| Need | Install |
| --- | --- |
| Every skill | Python 3.10+, `python -m pip install -r requirements.txt` (numpy>=1.26, Pillow>=10.1, scipy>=1.11; scipy is an accelerator with an identical numpy fallback) |
| codeart2d SVG art | `python -m pip install -r requirements-codeart.txt` (resvg-py>=0.5,<0.6), or the resvg-js CLI, or Chrome/Edge. PixelSpec sprites need nothing extra |
| Video, packaging and scene loops | ffmpeg 5.1+ with libvpx-vp9 and libx264 on `PATH` |
| JS runtimes, `fx_verify.mjs`, scene preview checks | node 22 (optional); `build_scene_preview.py --verify` also uses playwright when present |
| Local CLI routes (optional) | Your own signed-in Codex CLI or Grok CLI |
| Paid API routes (optional) | `OPENAI_API_KEY` or `XAI_API_KEY` in the environment; never in prompts, outputs or the shipped game |
| Contributors | `python -m pip install -r requirements-dev.txt`, then `python -m pytest -q` and `node --test "tests/js/*.test.mjs"` |

## Art Routes And Spend Safety

Every asset records its `art_source` (`code`, `host_image`, `api` or `existing`) and the agent names the route it used.

1. **Code art first** inside its envelope on every host: pixel sprites up to 48 px visible height (49-64 px with your consent), FX, tile topology, autotiles and map data. Always disclosed as "code-drawn, no image model".
2. **Images:** the host's own image tool, then a VERIFIED local Codex CLI, then a VERIFIED local Grok CLI (one-shot image or edit), then the paid REST API only with your consent for that request, else the agent explains the gap.
3. **Video:** the Grok CLI in ACP mode when VERIFIED, then REST with consent, then a clip you supply.

Local routes run without a per-call question but within the session cap (8 images, 2 videos per 12 hours per project; `FORGE_SESSION_IMAGES`, `FORGE_SESSION_VIDEOS`, `FORGE_SESSION_HOURS`). Paid calls are a dry run until `--execute`, show a consent block with provider, model, call count and estimate, honour `--budget-usd` / `--max-calls`, and refuse an identical earlier request. Every call is a line in `<project>/.forge/ledger.jsonl`. No credential is ever read by the doctor or the CLI routes. Details: [cli-routes.md](./skills/generate2dmedia/references/cli-routes.md), [api-usage.md](./skills/generate2dmedia/references/api-usage.md), [provider-survey.md](./skills/generate2dmedia/references/provider-survey.md).

## Tools

Run from your project root as `python "<skill-dir>/scripts/<tool>.py" ...`; every tool has `--help`, writes a new output folder (it never replaces one), prints one JSON line and exits 1 on failure. The SKILL.md files hold the routing tables.

| Skill | Tool | What it does |
| --- | --- | --- |
| generate2dsprite | `generate2dsprite.py process` | Keys (soft or hard), slices and registers a sheet on one sampling grid; pipeline-meta v2 with QA |
| | `sheet_qc.py` | `spill` finds parts crossing cell lines before slicing; `frames` checks identity, NEAR/FAR leg alternation, drift |
| | `scale_frames.py` | One scale and root per action; the canvas grows, nothing is clamped |
| | `plan_guide.py`, `make_anchor_layout.py`, `make_layout_guide.py` | Sheet planning, pose guides and fixed-scale templates for an image tool |
| | `build_animation_clips.py` | Clips v2: 60 Hz ticks, events, transitions, hit-stop, review sheets and lints |
| | `assemble_frames.py` | Packs whole frames losslessly; ownership slicing, loop seams, ambient crossfades |
| | `palette_tool.py`, `pixel_reduce.py` | OKLab palettes, locks, variants, flicker-free clip quantizing; integer pixel-grid reduction |
| | `export_engine.py` | Aseprite JSON, Godot SpriteFrames and AnimatedSprite3D (imports not yet verified) |
| video2dsprite | `video2dsprite.py` | `key-plan`, `triage`, soft-matte `process`/`clean`, `package`, `verify`, `doctor` |
| | `prepare_i2v_input.py`, `register_clip.py` | Registration by construction: input on a recorded transform, one inverse transform back, take QC |
| | `gait_loop.py`, `retime.py`, `animation_review.py` | Measured walk/run/idle loops and stride; impact/hold retiming on ticks; classified candidates |
| | `engine_export.py`, `validate_animation.py` | animation.json 3.0 with WebM alpha, packed MP4 and mobile tiers behind a residue gate; decoded verify; contract check |
| | `references/runtime/forge-runtime.mjs`, `packed-alpha-webgl.mjs` | Distance-driven walks, hit-stop, transitions; WebGL packed-alpha compositor |
| generate2dmap | `extract_prop_pack.py` | Anchored transparent props with footprints and despill (prop_pack.v2) |
| | `extract_terrain_tiles.py`, `extract_platform_strip.py` | Terrain fills, overlays, iso/hex and Wang rows; platform caps with surface and seam QC |
| | `compose_layered_preview.py`, `validate_parallax.py`, `conform_background.py` | Ground-line sorted previews with audit overlays; parallax coverage and seams; fitting paintings to screens |
| | `map_bundle.py`, `map_nav.py` | map_bundle.v2 validation; collision, reachability and portals from data |
| | `export_tiled.py`, `export_godot.py`, `export_ldtk.py` | Tiled 1.10 (re-render verified), Godot 4.3+ and LDtk 1.5.3 (parse level only) |
| | `validate_chunks.py`, `validate_layout.py` | Room-chunk sockets; side-scroll jumps, slopes and decks |
| | `build_scene_preview.py`, `references/runtime/map-runtime.mjs` | Single-file walkable HTML preview with a route check; JS collision that mirrors map_nav |
| | `scene_layout_guide.py`, `validate_stage.py`, `extract_scene_lights.py`, `edit_locality_check.py` | HD-2D stage guide, aspect-robust battle layout, lights and atmosphere, variant locality |
| | `build_motion_mask.py`, `scene_motion.py` | Masked motion on a still plate; GOP-aligned loops with decoded-seam QA |
| codeart2d | `render_pixelspec.py`, `pixel_qa.py` | PixelSpec to exact-palette frames and clips; pixel-art QA |
| | `svg_render.py` | Portable SVG `render`, `lint` and renderer `doctor` |
| | `rig_animate.py` | SVG rigs with FK, two-bone IK and a ground constraint |
| | `fx_build.py`, `fx_verify.mjs` | Six FX presets with hit events and the fx.v1 runtime; runtime checker |
| | `autotile_build.py` | Wang-16, three-material, blob-47 and bevel tilesets with an exhaustive seam proof |
| | `layout_build.py`, `parallax_build.py`, `ambient_bake.py` | Playable top-down layouts; periodic parallax layers; ambient loops on a plate |
| generate2dmedia | `forge_doctor.py` | Capability check and route readiness ladder; `--verify-route` |
| | `cli_media.py` | Local Codex/Grok CLI image, edit and video routes; `resume`, `adopt --codex-thread`, `batch` |
| | `generate_media.py`, `media_ledger.py` | Paid OpenAI/xAI API with consent, caps and receipts; ledger summary and settle |
| repository | `tools/install_skills.py`, `tools/vendor_sync.py`, `tools/check_links.py` | Guarded install and drift check; shared-module copies in sync; README/doc link check |

## How It Works

1. You ask for a sprite, an animation, a map or a prototype.
2. The agent plans size, camera, motion and art source, and runs the capability check once.
3. The art comes from code, the host's image tool, a verified local CLI, a paid API with consent, or your files; image-to-video adds motion when needed.
4. Local tools key, slice, register, palette, loop, validate and export, and write QA with numbers.
5. The agent looks at the review sheets before it reports, and wires the result into your engine if you ask.

The scripts are not the creative brain, and numeric QA never approves anatomy or motion by itself.

## Suggested Prompts

### Sprite

```text
Use $generate2dsprite to create a 3x3 idle for an ultimate earth titan.
```

```text
Use $generate2dsprite to create a side-view lightning knight attack animation, then export it for Godot.
```

```text
Use $generate2dsprite to create a 32 px pixel-art slime enemy with three colour variants and an idle loop.
```

```text
Use $generate2dsprite to create a wizard spell bundle with cast, projectile, and impact sprites.
```

### Video → dense sprites or transparent media

```text
Use $video2dsprite with my existing side-view hero PNG as the master. Generate a 6s in-place run, key it, pick the loop, and package PNG, WebM and packed MP4. Report paths and QA numbers.
```

### Map

```text
Use $generate2dmap to create a small top-down village with a pond, roads to three exits, collision, and a walkable HTML preview, then export it to Tiled.
```

```text
Use $generate2dmap to create a top-down RPG forest shrine map. Use a layered raster pipeline, a 3x3 prop pack for small environmental props, precise collision, encounter grass zones, a rest point, and actors that can walk in front of and behind tall props.
```

```text
Use $generate2dmap to create an HD-2D battle plate for a harbour at night with lantern lights and moving water.
```

## Notes

- Best results come from prompts that state view, size, action and motion style.
- Large creatures often work better as `3x3 idle`; small spells and projectiles as `1x4`, `2x2` or `2x3`.
- Thresholds that came from one clip or one sheet are flagged as such. What is and is not proven is listed in [docs/known-limitations.md](./docs/known-limitations.md); test results are in [docs/validation-2026-10-06.md](./docs/validation-2026-10-06.md).
- Engine and editor imports of the 0.4.0 exporters are not yet verified (see above).

## Generated Assets And Licensing

The MIT license below covers this repository's code and documentation. It does not cover what you generate with it. Images and videos from an image or video model are subject to that provider's terms; code-drawn art is produced from specs you or your agent write. Do not trace or prompt for characters you do not own; the fan-art tests above are capability demos, not licensed assets. For commercial projects, use original characters or IP you control, and check each provider's terms.

## Repository Layout

```text
agent-sprite-forge/
  .claude-plugin/        plugin.json, marketplace.json (Claude Code)
  skills/
    generate2dsprite/    SKILL.md, agents/openai.yaml, references/, scripts/
    generate2dmap/
    video2dsprite/
    codeart2d/           examples/ with every spec shown above
    generate2dmedia/
  shared/                canonical shared modules and JSON schemas (vendored into skills)
  tools/                 install_skills.py, vendor_sync.py, check_links.py
  tests/                 pytest and node suites, fixtures with provenance
  docs/                  validation record, known limitations, audit
  src/                   README media
  CHANGELOG.md
```

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
