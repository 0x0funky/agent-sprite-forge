#!/usr/bin/env python3
"""Create a layout-only guide image for sprite sheet generation.

Each cell gets its border, a blue safe frame inset by the safe margins and
dashed centre lines. Margins must satisfy 0 <= margin < half the cell, so a
safe frame never collapses or spills into a neighbouring cell. The output PNG
must be new; it is published without replacing anything, and a one-line JSON
summary is printed on success.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from PIL import Image
from PIL import ImageDraw

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)


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


def validate_layout(rows: int, cols: int, cell_width: int, cell_height: int,
                    safe_margin_x: int, safe_margin_y: int) -> None:
    """Refuse a grid or safe margin that cannot be drawn inside its own cell (S25)."""
    if rows <= 0 or cols <= 0:
        raise ValueError("--rows and --cols must be positive")
    if cell_width <= 0 or cell_height <= 0:
        raise ValueError("--cell-width and --cell-height must be positive")
    for flag, margin, size, axis in (("--safe-margin-x", safe_margin_x, cell_width, "width"),
                                     ("--safe-margin-y", safe_margin_y, cell_height, "height")):
        if not 0 <= 2 * margin < size:
            raise ValueError(f"{flag} must satisfy 0 <= margin < half the cell {axis} ({size} px cells allow "
                             f"0 to {(size - 1) // 2}); got {margin}")


def build_layout_guide(rows: int, cols: int, cell_width: int, cell_height: int, safe_margin_x: int,
                       safe_margin_y: int, label_cells: bool = False) -> Image.Image:
    validate_layout(rows, cols, cell_width, cell_height, safe_margin_x, safe_margin_y)
    width = cols * cell_width
    height = rows * cell_height
    image = Image.new("RGB", (width, height), "#f8f8f8")
    draw = ImageDraw.Draw(image)

    for row in range(rows):
        for col in range(cols):
            left = col * cell_width
            top = row * cell_height
            right = left + cell_width - 1
            bottom = top + cell_height - 1
            safe_left = left + safe_margin_x
            safe_top = top + safe_margin_y
            safe_right = right - safe_margin_x
            safe_bottom = bottom - safe_margin_y

            draw.rectangle((left, top, right, bottom), outline="#111111", width=4)
            draw.rectangle((safe_left, safe_top, safe_right, safe_bottom), outline="#2f80ed", width=3)

            center_x = left + cell_width // 2
            center_y = top + cell_height // 2
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

            if label_cells:
                draw.text((left + 12, top + 10), f"{row + 1},{col + 1}", fill="#777777")
    return image


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--cols", type=int, required=True)
    parser.add_argument("--cell-width", type=int, default=384)
    parser.add_argument("--cell-height", type=int, default=384)
    parser.add_argument("--safe-margin-x", type=int, default=52, help="0 <= margin < half the cell width.")
    parser.add_argument("--safe-margin-y", type=int, default=52, help="0 <= margin < half the cell height.")
    parser.add_argument("--output", type=Path, required=True,
                        help="New .png file; an existing file is never replaced.")
    parser.add_argument(
        "--label-cells",
        action="store_true",
        help="Draw small row,column labels. Leave off for normal imagegen references.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        output = args.output.resolve()
        if output.suffix.lower() != ".png":
            raise ValueError(f"--output must be a .png file: {output.name}")
        if os.path.lexists(output):
            raise FileExistsError(f"Refusing to overwrite existing output: {output}")
        image = build_layout_guide(args.rows, args.cols, args.cell_width, args.cell_height,
                                   args.safe_margin_x, args.safe_margin_y, args.label_cells)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as temporary:
            staged = Path(temporary) / output.name
            forge_core.save_png(image, staged)
            forge_core.publish_file_no_replace(staged, output)
    except (ValueError, OSError) as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(output), "size": list(image.size), "rows": args.rows, "cols": args.cols}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
