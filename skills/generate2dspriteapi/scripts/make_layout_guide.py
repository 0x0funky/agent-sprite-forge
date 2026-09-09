#!/usr/bin/env python3
"""Create a layout-only guide image for sprite sheet generation."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image
from PIL import ImageDraw


def draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    fill: str,
    width: int,
    dash: int,
    gap: int,
) -> None:
    x1, y1 = start
    x2, y2 = end
    if x1 == x2:
        for y in range(min(y1, y2), max(y1, y2), dash + gap):
            draw.line((x1, y, x2, min(y + dash, max(y1, y2))), fill=fill, width=width)
        return
    if y1 == y2:
        for x in range(min(x1, x2), max(x1, x2), dash + gap):
            draw.line((x, y1, min(x + dash, max(x1, x2)), y2), fill=fill, width=width)
        return
    raise ValueError("draw_dashed_line only supports horizontal or vertical lines")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--cols", type=int, required=True)
    parser.add_argument("--cell-width", type=int, default=384)
    parser.add_argument("--cell-height", type=int, default=384)
    parser.add_argument("--safe-margin-x", type=int, default=52)
    parser.add_argument("--safe-margin-y", type=int, default=52)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--label-cells",
        action="store_true",
        help="Draw small row,column labels. Leave off for normal imagegen references.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.rows <= 0 or args.cols <= 0:
        raise SystemExit("--rows and --cols must be positive")
    if args.cell_width <= 0 or args.cell_height <= 0:
        raise SystemExit("--cell-width and --cell-height must be positive")

    width = args.cols * args.cell_width
    height = args.rows * args.cell_height
    image = Image.new("RGB", (width, height), "#f8f8f8")
    draw = ImageDraw.Draw(image)

    for row in range(args.rows):
        for col in range(args.cols):
            left = col * args.cell_width
            top = row * args.cell_height
            right = left + args.cell_width - 1
            bottom = top + args.cell_height - 1
            safe_left = left + args.safe_margin_x
            safe_top = top + args.safe_margin_y
            safe_right = right - args.safe_margin_x
            safe_bottom = bottom - args.safe_margin_y

            draw.rectangle((left, top, right, bottom), outline="#111111", width=4)
            draw.rectangle((safe_left, safe_top, safe_right, safe_bottom), outline="#2f80ed", width=3)

            center_x = left + args.cell_width // 2
            center_y = top + args.cell_height // 2
            draw_dashed_line(
                draw,
                (center_x, safe_top),
                (center_x, safe_bottom),
                fill="#b8b8b8",
                width=2,
                dash=14,
                gap=16,
            )
            draw_dashed_line(
                draw,
                (safe_left, center_y),
                (safe_right, center_y),
                fill="#b8b8b8",
                width=2,
                dash=14,
                gap=16,
            )

            if args.label_cells:
                draw.text((left + 12, top + 10), f"{row + 1},{col + 1}", fill="#777777")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)


if __name__ == "__main__":
    main()
