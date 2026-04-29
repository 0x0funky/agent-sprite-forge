#!/usr/bin/env python3
"""Agent-agnostic image generation wrapper.

Codex provides built-in `image_gen`. Other agents (Claude Code, Cursor, generic
CLI agents) shell out to this script instead. It produces the same artifact:
a PNG written to a known path that the local sprite/map post-processor can read.

Backends, in priority order:

1. ``openai`` - OpenAI Images API (default model: ``gpt-image-2``).
2. ``gemini`` - Google Gemini 2.5 Flash Image.

The backend is chosen via ``--backend`` or the ``SPRITE_FORGE_BACKEND`` env var.
``auto`` (default) picks the first backend whose API key is present.

Usage:

    python scripts/image_gen.py \\
        --prompt "fire mage cast 2x3 sheet, solid #FF00FF background" \\
        --out raw-sheet.png \\
        --size 1024x1024

    # With a reference image (image edit / variation)
    python scripts/image_gen.py \\
        --prompt "same character, walk cycle 4x4" \\
        --reference path/to/character.png \\
        --out walk-sheet.png

Env vars:

    OPENAI_API_KEY       Required for the ``openai`` backend.
    GEMINI_API_KEY       Required for the ``gemini`` backend.
    SPRITE_FORGE_BACKEND One of: auto | openai | gemini. Default: auto.
    SPRITE_FORGE_MODEL   Override the default model id for the chosen backend.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional


DEFAULT_OPENAI_MODEL = "gpt-image-2"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-image"


def _err(msg: str, code: int = 1) -> None:
    print(f"image_gen: {msg}", file=sys.stderr)
    sys.exit(code)


def _detect_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    _err(
        "no backend available. Set OPENAI_API_KEY or GEMINI_API_KEY, "
        "or pass --backend explicitly."
    )
    return ""  # unreachable


def _generate_openai(
    prompt: str,
    out_path: Path,
    size: str,
    reference: Optional[Path],
    model: str,
) -> dict:
    try:
        from openai import OpenAI
    except ImportError:
        _err(
            "openai SDK not installed. Run: pip install 'openai>=1.50' "
            "(or install the optional extras: pip install -r requirements-openai.txt)"
        )

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _err("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=api_key)

    if reference is not None:
        if not reference.exists():
            _err(f"reference not found: {reference}")
        with reference.open("rb") as fh:
            response = client.images.edit(
                model=model,
                image=fh,
                prompt=prompt,
                size=size,
            )
    else:
        response = client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
        )

    data = response.data[0]
    b64 = getattr(data, "b64_json", None)
    if b64 is None:
        # Some models return a URL instead of b64
        url = getattr(data, "url", None)
        if not url:
            _err("OpenAI response contained neither b64_json nor url")
        import urllib.request

        with urllib.request.urlopen(url) as r:
            out_path.write_bytes(r.read())
    else:
        out_path.write_bytes(base64.b64decode(b64))

    return {"backend": "openai", "model": model, "path": str(out_path)}


def _generate_gemini(
    prompt: str,
    out_path: Path,
    size: str,
    reference: Optional[Path],
    model: str,
) -> dict:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        _err(
            "google-genai SDK not installed. Run: pip install 'google-genai>=0.3'"
        )

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        _err("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")

    client = genai.Client(api_key=api_key)

    contents: list = [prompt]
    if reference is not None:
        if not reference.exists():
            _err(f"reference not found: {reference}")
        contents.append(
            types.Part.from_bytes(
                data=reference.read_bytes(),
                mime_type="image/png",
            )
        )

    response = client.models.generate_content(
        model=model,
        contents=contents,
    )

    for part in response.candidates[0].content.parts:
        if getattr(part, "inline_data", None) is not None:
            out_path.write_bytes(part.inline_data.data)
            return {"backend": "gemini", "model": model, "path": str(out_path)}

    _err("Gemini response contained no inline image data")
    return {}  # unreachable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True, help="Creative image prompt.")
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output PNG path (will be created or overwritten).",
    )
    parser.add_argument(
        "--size",
        default="1024x1024",
        help="Image size (e.g. 1024x1024, 1024x1536). Default: 1024x1024.",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="Optional reference image for image-edit / image-to-image flows.",
    )
    parser.add_argument(
        "--backend",
        default=os.environ.get("SPRITE_FORGE_BACKEND", "auto"),
        choices=["auto", "openai", "gemini"],
        help="Which provider to use. Default: auto (first with an API key set).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("SPRITE_FORGE_MODEL"),
        help="Override the default model id for the chosen backend.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the JSON status line on stdout.",
    )
    args = parser.parse_args()

    backend = _detect_backend(args.backend)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    if backend == "openai":
        model = args.model or DEFAULT_OPENAI_MODEL
        result = _generate_openai(args.prompt, args.out, args.size, args.reference, model)
    elif backend == "gemini":
        model = args.model or DEFAULT_GEMINI_MODEL
        result = _generate_gemini(args.prompt, args.out, args.size, args.reference, model)
    else:
        _err(f"unknown backend: {backend}")
        return

    if not args.quiet:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
