# Agent Sprite Forge

言語：[English](./README.md) | [繁體中文](./README.zh-TW.md) | [简体中文](./README.zh-CN.md) | [日本語](./README.ja.md) | [한국어](./README.ko.md)

<p align="center">
  <img src="./src/banner.png" alt="Agent Sprite Forge banner" width="900" />
</p>

<p align="center">
  <strong>エージェント向け 2D ゲームアセット skills：ゲームで使えるスプライト、コードで描くピクセルアート、AI 動画アニメーション、遊べるマップ。</strong>
</p>

> このページは英語版と同期した要約です。ショーケース、ツール表、詳しい説明は [English README](./README.md) が正です（繁體中文は全訳）。

Codex、Claude Code、Grok で使えます。エージェントがアセットを計画してアートの入手経路（コード描画、ホストの画像生成ツール、ローカルの Codex／Grok CLI、同意を得た有料 API）を選び、決定的な Python ツールがキー抜き、分割、位置合わせ、検査を行い、Godot、Tiled、LDtk、Aseprite、Web ゲーム向けに書き出します。

## 0.4.0 の新機能

- **5 つの sibling skills：** `generate2dsprite`、`generate2dmap`、`video2dsprite`、`generate2dmedia`、新しい **`codeart2d`**（画像モデルを使わずコードでゲームアートを描く）。
- **Claude Code プラグイン**。Codex と Grok はバックアップとドリフト検査付きのフォルダインストール。
- **ローカルエージェント優先：** ホストの画像ツール（Codex `image_gen`）→ ローカル Codex CLI → Grok CLI（ワンショット画像モード）。動画は ACP モードの Grok CLI。ローカル経路は `forge_doctor` が VERIFIED と判定した後のみ使用し、すべての呼び出しはプロジェクトの ledger に記録され、セッション上限があります（既定：12 時間ごとに画像 8 枚、動画 2 本）。有料 API はリクエストごとの同意がある場合のみ実行。
- **ソフトマット（紫フリンジなし）**、**計測による歩行ループ選択とリタイム**、**構築による位置合わせ**。
- **Engine export 3.0 と runtime**（animation.json 3.0、WebM alpha、iPhone 向け packed MP4、Aseprite／Godot 書き出し）。
- **map_bundle v2、map_nav**、Tiled／Godot／LDtk 書き出し、**HD-2D** の stage とシーンモーション。
- **コードアート パイプライン：** PixelSpec、リグ、FX、オートタイル、レイアウト。そして能力チェックの **`forge_doctor`**。

詳細：[What's new](./README.md#whats-new-in-040)、[CHANGELOG](./CHANGELOG.md)。

## インストール

Claude Code：

```bash
claude plugin marketplace add 0x0funky/agent-sprite-forge --sparse .claude-plugin skills
claude plugin install agent-sprite-forge@agent-sprite-forge
python -m pip install "numpy>=1.26" "Pillow>=10.1" "scipy>=1.11"
```

Codex（Grok は `--host grok`。コピーによる代替インストールは `--host claude` でも可）：

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd agent-sprite-forge
python -m pip install -r requirements.txt
python tools/install_skills.py --apply --host codex
```

Python 3.10+ が必要です。動画機能には ffmpeg 5.1+ が必要です。詳しくは [Install](./README.md#install) と [Requirements](./README.md#requirements)。

## リンク

- [Showcase](./README.md#showcase)、[Skills](./README.md#included-skills)、[Tools](./README.md#tools)、[アート経路と支出の安全性](./README.md#art-routes-and-spend-safety)
- エンジン／エディタへのインポートは未検証：[English README](./README.md#godot-editable-tilemap) を参照
- [既知の制限](./docs/known-limitations.md)、[検証記録](./docs/validation-2026-10-06.md)
- 生成アセットのライセンス：[Generated Assets And Licensing](./README.md#generated-assets-and-licensing)

## ライセンス

MIT。[LICENSE](./LICENSE) を参照。
