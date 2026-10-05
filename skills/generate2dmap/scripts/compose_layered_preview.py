#!/usr/bin/env python3
"""Compose a flattened layered-map preview from a base image and prop placements."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_path(value: str, roots: list[Path]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    for root in roots:
        candidate = root / path
        if candidate.exists():
            return candidate
    return roots[0] / path


def load_props(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = []
        found = False
        for key in ("props", "objects", "actors", "foreground"):
            value = data.get(key)
            if isinstance(value, list):
                found = True
                if key == "foreground":
                    items.extend({**item, "layer": "foreground"} for item in value)
                else:
                    items.extend(value)
            elif key in data:
                raise ValueError(f"Placement field {key!r} must be a list.")
        if found:
            return items
    raise ValueError("Placement JSON must be a list or an object with a 'props' list.")


def placement_xy(
    prop: dict[str, Any], width: int, height: int, source_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    anchor = str(prop.get("anchor", "center-bottom"))
    x = float(prop.get("x", 0))
    y = float(prop.get("y", 0))

    if "anchorPx" in prop:
        point = prop["anchorPx"]
        if (not isinstance(point, list) or len(point) != 2
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in point)):
            raise ValueError("anchorPx must be two finite source-pixel coordinates.")
        sw, sh = source_size or (width, height)
        if not (0 <= point[0] <= sw and 0 <= point[1] <= sh):
            raise ValueError("anchorPx must be inside the source canvas.")
        left = x - point[0] * width / sw
        top = y - point[1] * height / sh
    elif anchor == "top-left":
        left = x
        top = y
    elif anchor == "center":
        left = x - width / 2
        top = y - height / 2
    elif anchor == "bottom-left":
        left = x
        top = y - height
    elif anchor == "center-bottom":
        left = x - width / 2
        top = y - height
    else:
        raise ValueError(f"Unknown anchor: {anchor}")
    return round(left), round(top)


def effective_sort_y(prop: dict[str, Any]) -> float:
    value = float(prop.get("sortY", prop.get("y", 0)))
    if not math.isfinite(value):
        raise ValueError("sortY must be finite.")
    return value


def paste_prop(
    canvas: Image.Image, prop: dict[str, Any], roots: list[Path], resampler: str = "lanczos",
) -> dict[str, Any]:
    image_key = prop.get("image") or prop.get("path")
    if not image_key:
        raise ValueError(f"Prop is missing image/path: {prop}")
    image_path = resolve_path(str(image_key), roots)
    if not image_path.exists():
        raise FileNotFoundError(f"Prop image not found: {image_path}")

    with Image.open(image_path) as source:
        img = source.convert("RGBA")
    source_size = img.size
    width = int(prop.get("w", prop.get("width", img.width)))
    height = int(prop.get("h", prop.get("height", img.height)))
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid prop size for {image_path}: {width}x{height}")
    resampler = str(prop.get("resampler", resampler))
    if resampler not in {"nearest", "lanczos"}:
        raise ValueError(f"Unknown resampler: {resampler}")
    if (width, height) != img.size:
        img = img.resize((width, height), getattr(Image.Resampling, resampler.upper()))

    opacity = float(prop.get("opacity", 1.0))
    if not math.isfinite(opacity) or not 0 <= opacity <= 1:
        raise ValueError("opacity must be between 0 and 1.")
    if opacity < 1:
        alpha = img.getchannel("A").point(lambda value: int(value * max(0.0, min(1.0, opacity))))
        img.putalpha(alpha)

    left, top = placement_xy(prop, width, height, source_size)
    canvas.alpha_composite(img, (left, top))
    return {
        "id": prop.get("id", image_path.stem),
        "image": str(image_path),
        "left": left,
        "top": top,
        "w": width,
        "h": height,
        "source_size": list(source_size),
        "anchorPx": prop.get("anchorPx"),
        "resampler": resampler,
        "clipped": left < 0 or top < 0 or left + width > canvas.width or top + height > canvas.height,
        "sortY": effective_sort_y(prop),
        "layer": prop.get("layer", "props"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--placements", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--resampler", choices=["nearest", "lanczos"], default="lanczos")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    with Image.open(args.base) as source:
        base = source.convert("RGBA")
    data = read_json(args.placements)
    props = load_props(data)
    roots = [args.placements.parent, args.base.parent, args.project_root]

    props_layer = [prop for prop in props if str(prop.get("layer", "props")) != "foreground"]
    foreground_layer = [prop for prop in props if str(prop.get("layer", "props")) == "foreground"]
    props_layer.sort(key=effective_sort_y)
    foreground_layer.sort(key=effective_sort_y)

    pasted = []
    for prop in props_layer + foreground_layer:
        pasted.append(paste_prop(base, prop, roots, args.resampler))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    base.save(args.output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "base": str(args.base),
                    "placements": str(args.placements),
                    "output": str(args.output),
                    "pasted": pasted,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    print(str(args.output.resolve()))


if __name__ == "__main__":
    main()
