#!/usr/bin/env python3
"""使用 OpenAI Image API 根据提示词生成图片。

示例：
    export OPENAI_API_KEY="你的 API Key"
    python3 genimg.py "一座建在岔路铁轨旁的水元素防御塔，2D 游戏素材"
    python3 genimg.py "像素风火焰怪物，透明背景" -o asset-source/exports/fire_enemy.png
    python3 genimg.py "水元素防御塔" --base-url https://example.com/v1

不传提示词时，脚本会在终端中交互式询问。API 基础地址也可以通过
OPENAI_BASE_URL 环境变量设置，命令行参数的优先级更高。
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path
from typing import Sequence

DEFAULT_MODEL = "gpt-image-2"
DEFAULT_OUTPUT = Path("generated.png")
CONFIG_ENV_KEYS = {"OPENAI_API_KEY", "OPENAI_BASE_URL"}


def default_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "generate2dspriteapi.env"
    return Path.home() / ".codex" / "generate2dspriteapi.env"


def _parse_config_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
        return value[1:-1]
    return value


def load_config_file(path: Path | None) -> None:
    """Load only supported API settings without executing shell code.

    Existing process environment variables always win over the user config.
    """
    if path is None or not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if separator and key in CONFIG_ENV_KEYS and key not in os.environ:
            os.environ[key] = _parse_config_value(value)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据提示词调用 OpenAI Image API 生成图片。")
    parser.add_argument(
        "prompt",
        nargs="?",
        help="生图提示词；省略时会在终端中交互输入。",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"图片保存路径（默认：{DEFAULT_OUTPUT}）。",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"图像模型（默认：{DEFAULT_MODEL}）。",
    )
    parser.add_argument(
        "--base-url",
        "--api-url",
        dest="base_url",
        default=os.environ.get("OPENAI_BASE_URL"),
        help="API 基础地址，例如 https://example.com/v1；默认读取 OPENAI_BASE_URL。",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("GENIMG_CONFIG", default_config_path())),
        help="用户级配置文件（默认：~/.codex/generate2dspriteapi.env）。",
    )
    parser.add_argument(
        "--no-config",
        action="store_true",
        help="不读取用户级配置文件，仅使用当前进程环境变量和命令行参数。",
    )
    parser.add_argument(
        "--size",
        default="1024x1024",
        help="图片尺寸（默认：1024x1024）。",
    )
    parser.add_argument(
        "--quality",
        choices=("low", "medium", "high"),
        default="high",
        help="生成质量（默认：high）。",
    )
    return parser.parse_args(argv)


def resolve_prompt(prompt: str | None) -> str:
    if prompt is not None and prompt.strip():
        return prompt.strip()

    if not sys.stdin.isatty():
        raise ValueError("缺少提示词。请将提示词作为参数传入。")

    entered_prompt = input("请输入生图提示词：").strip()
    if not entered_prompt:
        raise ValueError("提示词不能为空。")
    return entered_prompt


def generate_image(
    prompt: str,
    output: Path,
    model: str,
    base_url: str | None,
    size: str,
    quality: str,
) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("未设置 OPENAI_API_KEY 环境变量。")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("缺少 openai 包，请先运行：python3 -m pip install openai") from exc

    client_options = {"base_url": base_url} if base_url else {}
    client = OpenAI(**client_options)
    response = client.images.generate(
        model=model,
        prompt=prompt,
        size=size,
        quality=quality,
    )

    if not response.data or not response.data[0].b64_json:
        raise RuntimeError("API 未返回可保存的图像数据。")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(response.data[0].b64_json, validate=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.no_config:
        load_config_file(args.config)
    if args.base_url is None:
        args.base_url = os.environ.get("OPENAI_BASE_URL")

    try:
        prompt = resolve_prompt(args.prompt)
        generate_image(
            prompt,
            args.output,
            args.model,
            args.base_url,
            args.size,
            args.quality,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # OpenAI SDK/API 错误包含在此处并简洁展示。
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1

    print(f"图片已保存至 {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
