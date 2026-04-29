#!/usr/bin/env python3
"""Compatibility shim for ``view_image``.

Codex has a built-in ``view_image`` tool that surfaces a local image into the
conversation context. Other agents (Claude Code, Cursor, generic CLI agents)
typically use a generic file-read tool that already supports images.

For non-Codex agents the recommended flow is:

1. Use the agent's native file-read tool on the path. Claude Code's ``Read``
   tool, Cursor's file-read, and most others render images directly.
2. Then call ``scripts/image_gen.py --reference <path> ...`` so the image is
   passed to the image-edit endpoint.

This script is provided for parity. It validates that the path exists and is
an image, prints its dimensions and format, and exits non-zero if the image is
unusable. Useful inside scripted pipelines where the agent wants a single
"reference is ready" signal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Path to a local image file.")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the JSON status line on stdout.",
    )
    args = parser.parse_args()

    if not args.path.exists():
        print(f"view_image: not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    try:
        from PIL import Image
    except ImportError:
        print(
            "view_image: Pillow not installed. Run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with Image.open(args.path) as img:
            info = {
                "path": str(args.path.resolve()),
                "format": img.format,
                "mode": img.mode,
                "size": list(img.size),
            }
    except Exception as exc:  # noqa: BLE001
        print(f"view_image: cannot open {args.path}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not args.quiet:
        print(json.dumps(info))


if __name__ == "__main__":
    main()
