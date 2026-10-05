# Agent Sprite Forge

언어: [English](./README.md) | [繁體中文](./README.zh-TW.md) | [简体中文](./README.zh-CN.md) | [日本語](./README.ja.md) | [한국어](./README.ko.md)

<p align="center">
  <img src="./src/banner.png" alt="Agent Sprite Forge banner" width="900" />
</p>

<p align="center">
  <strong>에이전트용 2D 게임 에셋 skills: 게임에 바로 쓰는 스프라이트, 코드로 그린 픽셀 아트, AI 영상 애니메이션, 플레이 가능한 맵.</strong>
</p>

> 이 페이지는 영어판과 동기화된 요약입니다. 쇼케이스, 도구 표, 자세한 설명은 [English README](./README.md)를 기준으로 합니다(繁體中文은 전체 번역).

Codex, Claude Code, Grok에서 사용할 수 있습니다. 에이전트가 에셋을 계획하고 아트 경로(코드 드로잉, 호스트의 이미지 도구, 로컬 Codex／Grok CLI, 동의를 받은 유료 API)를 고르면, 결정적인 Python 도구가 키잉, 분할, 정렬, 검사를 하고 Godot, Tiled, LDtk, Aseprite, 웹 게임용으로 내보냅니다.

## 0.4.0 새 기능

- **다섯 개의 sibling skills:** `generate2dsprite`, `generate2dmap`, `video2dsprite`, `generate2dmedia`, 그리고 새 **`codeart2d`**(이미지 모델 없이 코드로 게임 아트를 그림).
- **Claude Code 플러그인**. Codex와 Grok은 백업과 드리프트 검사가 있는 폴더 설치.
- **로컬 에이전트 우선:** 호스트 이미지 도구(Codex `image_gen`) → 로컬 Codex CLI → Grok CLI(원샷 이미지 모드). 영상은 ACP 모드의 Grok CLI. 로컬 경로는 `forge_doctor`가 VERIFIED로 확인한 뒤에만 쓰며, 모든 호출은 프로젝트 ledger에 기록되고 세션 상한이 있습니다(기본: 12시간마다 이미지 8장, 영상 2개). 유료 API는 요청마다 동의가 있을 때만 실행.
- **소프트 매트(보라색 테두리 없음)**, **측정 기반 걷기 루프 선택과 리타임**, **구성에 의한 정렬(registration by construction)**.
- **Engine export 3.0과 runtime**(animation.json 3.0, WebM alpha, iPhone용 packed MP4, Aseprite／Godot 내보내기).
- **map_bundle v2, map_nav**, Tiled／Godot／LDtk 내보내기, **HD-2D** stage와 장면 모션.
- **코드 아트 파이프라인:** PixelSpec, 리그, FX, 오토타일, 레이아웃. 그리고 능력 점검 **`forge_doctor`**.

자세히: [What's new](./README.md#whats-new-in-040), [CHANGELOG](./CHANGELOG.md).

## 설치

Claude Code:

```bash
claude plugin marketplace add 0x0funky/agent-sprite-forge --sparse .claude-plugin skills
claude plugin install agent-sprite-forge@agent-sprite-forge
python -m pip install "numpy>=1.26" "Pillow>=10.1" "scipy>=1.11"
```

Codex(Grok은 `--host grok`, 복사 방식 대체 설치는 `--host claude`도 가능):

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd agent-sprite-forge
python -m pip install -r requirements.txt
python tools/install_skills.py --apply --host codex
```

Python 3.10+가 필요하며, 영상 기능에는 ffmpeg 5.1+가 필요합니다. 자세한 내용은 [Install](./README.md#install)과 [Requirements](./README.md#requirements).

## 링크

- [Showcase](./README.md#showcase), [Skills](./README.md#included-skills), [Tools](./README.md#tools), [아트 경로와 지출 안전](./README.md#art-routes-and-spend-safety)
- 엔진／에디터 가져오기는 아직 검증되지 않음: [English README](./README.md#godot-editable-tilemap) 참고
- [알려진 제한](./docs/known-limitations.md), [검증 기록](./docs/validation-2026-10-06.md)
- 생성 에셋 라이선스: [Generated Assets And Licensing](./README.md#generated-assets-and-licensing)

## 라이선스

MIT. [LICENSE](./LICENSE) 참고.
