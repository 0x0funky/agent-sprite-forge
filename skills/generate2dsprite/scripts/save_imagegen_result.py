#!/usr/bin/env python3
"""Save base64 image data from a Codex image generation result."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DATA_URL_RE = re.compile(
    r"data:image/(?P<format>png|jpeg|jpg|webp|gif);base64,(?P<data>[A-Za-z0-9+/=_\-\s]+)",
    re.IGNORECASE,
)

BASE64_KEYS = {
    "b64",
    "b64_json",
    "base64",
    "data",
    "image",
    "image_base64",
    "image_data",
    "result",
}


def sniff_extension(data: bytes, hinted_format: str | None = None) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if hinted_format:
        return "jpg" if hinted_format.lower() == "jpeg" else hinted_format.lower()
    raise ValueError("Decoded bytes are not a recognized PNG, JPEG, WEBP, or GIF image.")


def decode_base64(candidate: str) -> bytes | None:
    cleaned = re.sub(r"\s+", "", candidate.strip())
    if len(cleaned) < 64:
        return None
    padding = "=" * (-len(cleaned) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(cleaned + padding)
        except (binascii.Error, ValueError):
            continue
    return None


def strings_from_json(value: Any, preferred: bool = False) -> Iterable[str]:
    if isinstance(value, dict):
        items = list(value.items())
        for key, child in items:
            if key.lower() in BASE64_KEYS:
                yield from strings_from_json(child, preferred=True)
        for key, child in items:
            if key.lower() not in BASE64_KEYS:
                yield from strings_from_json(child, preferred=preferred)
    elif isinstance(value, list):
        for item in value:
            yield from strings_from_json(item, preferred=preferred)
    elif isinstance(value, str):
        if preferred or len(value.strip()) >= 64:
            yield value


def image_generation_results_from_jsonl(text: str) -> Iterable[str]:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "image_generation_call":
            continue
        result = payload.get("result")
        if isinstance(result, str):
            yield result


def extract_image(text: str) -> tuple[bytes, str]:
    for candidate in reversed(list(image_generation_results_from_jsonl(text))):
        data = decode_base64(candidate)
        if data:
            ext = sniff_extension(data)
            return data, ext

    for match in DATA_URL_RE.finditer(text):
        data = decode_base64(match.group("data"))
        if data:
            ext = sniff_extension(data, match.group("format"))
            return data, ext

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    if payload is not None:
        for candidate in strings_from_json(payload):
            nested_match = DATA_URL_RE.search(candidate)
            if nested_match:
                data = decode_base64(nested_match.group("data"))
                if data:
                    ext = sniff_extension(data, nested_match.group("format"))
                    return data, ext
            data = decode_base64(candidate)
            if data:
                try:
                    ext = sniff_extension(data)
                except ValueError:
                    continue
                return data, ext

    data = decode_base64(text)
    if data:
        ext = sniff_extension(data)
        return data, ext

    raise ValueError("No base64 image data found in input.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        help="File containing session JSONL, image_generation_call.result JSON, a data URL, or raw base64. Defaults to stdin.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("generated_images"),
        help="Directory for the decoded image. Defaults to ./generated_images.",
    )
    parser.add_argument("--filename", help="Optional output filename. Extension is added when missing.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    text = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    data, ext = extract_image(text)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.filename:
        filename = args.filename
        if "." not in Path(filename).name:
            filename = f"{filename}.{ext}"
    else:
        digest = hashlib.sha256(data).hexdigest()[:16]
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"imagegen-{timestamp}-{digest}.{ext}"

    out_path = args.output_dir / filename
    out_path.write_bytes(data)
    print(str(out_path.resolve()))


if __name__ == "__main__":
    main()
