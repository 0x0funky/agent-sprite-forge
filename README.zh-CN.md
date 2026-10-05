# Agent Sprite Forge

语言：[English](./README.md) | [繁體中文](./README.zh-TW.md) | [简体中文](./README.zh-CN.md) | [日本語](./README.ja.md) | [한국어](./README.ko.md)

<p align="center">
  <img src="./src/banner.png" alt="Agent Sprite Forge banner" width="900" />
</p>

<p align="center">
  <strong>面向 agent 的 2D 游戏资产技能：可进游戏的 sprite、代码绘制的像素美术、AI 视频动画与可玩地图。</strong>
</p>

> 本页是与英文版同步的摘要。展示、工具表与完整说明以 [English README](./README.md) 为准（繁體中文为完整翻译）。

适用于 Codex、Claude Code 与 Grok。Agent 规划资产并选择美术来源（代码绘制、宿主的生图工具、你本机的 Codex／Grok CLI，或经你同意的付费 API），再由确定性的 Python 工具抠图、切格、对位、检查，并导出到 Godot、Tiled、LDtk、Aseprite 或网页游戏。

## 0.4.0 新功能

- **五个并列技能：** `generate2dsprite`、`generate2dmap`、`video2dsprite`、`generate2dmedia` 与新的 **`codeart2d`**（不用生图模型，用代码画游戏美术）。
- **Claude Code 插件**；Codex 与 Grok 使用带备份与漂移检查的文件夹安装。
- **本机 agent 优先：** 宿主生图工具（Codex `image_gen`）→ 本机 Codex CLI → Grok CLI（单次生图模式）；视频走 ACP 模式的 Grok CLI。本机路径须先由 `forge_doctor` 标记为 VERIFIED，所有调用写入项目 ledger，有会话上限（默认每 12 小时 8 张图、2 段视频）。付费 API 仅在你逐次同意后执行。
- **Soft matte，无紫边**；**按测量挑选步态循环与重新计时**；**构建式对位**。
- **Engine export 3.0 与 runtime**（animation.json 3.0、WebM alpha、iPhone 用 packed MP4、Aseprite／Godot 导出）。
- **map_bundle v2、map_nav** 与 Tiled／Godot／LDtk 导出；**HD-2D** stage 与场景动态。
- **代码美术流水线：** PixelSpec、骨骼动画、FX、autotile、layout；以及 **`forge_doctor`** 能力检查。

完整说明：[What's new](./README.md#whats-new-in-040)、[CHANGELOG](./CHANGELOG.md)。

## 安装

Claude Code：

```bash
claude plugin marketplace add 0x0funky/agent-sprite-forge --sparse .claude-plugin skills
claude plugin install agent-sprite-forge@agent-sprite-forge
python -m pip install "numpy>=1.26" "Pillow>=10.1" "scipy>=1.11"
```

Codex（Grok 用 `--host grok`；复制安装后备同样适用于 `--host claude`）：

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd agent-sprite-forge
python -m pip install -r requirements.txt
python tools/install_skills.py --apply --host codex
```

需要 Python 3.10+；视频功能需要 ffmpeg 5.1+。详见 [Install](./README.md#install) 与 [Requirements](./README.md#requirements)。

## 链接

- [Showcase](./README.md#showcase)、[Skills](./README.md#included-skills)、[Tools](./README.md#tools)、[美术路径与花费安全](./README.md#art-routes-and-spend-safety)
- 引擎／编辑器导入尚未验证：见 [English README](./README.md#godot-editable-tilemap)
- [已知限制](./docs/known-limitations.md)、[验证记录](./docs/validation-2026-10-06.md)
- 生成素材的授权说明：[Generated Assets And Licensing](./README.md#generated-assets-and-licensing)

## 授权

MIT，见 [LICENSE](./LICENSE)。
