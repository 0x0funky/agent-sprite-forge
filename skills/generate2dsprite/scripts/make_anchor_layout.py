#!/usr/bin/env python3
"""Repeat an accepted character frame into a fixed scale/root sprite-sheet template."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

from generate2dsprite import remove_bg_magenta


def build_anchor_layout(
    source: Image.Image,
    *,
    rows: int,
    cols: int,
    cell_width: int,
    cell_height: int,
    subject_height_ratio: float,
    subject_width_ratio: float,
    feet_ratio: float,
    threshold: int,
    edge_threshold: int,
) -> Image.Image:
    if rows <= 0 or cols <= 0:
        raise ValueError("rows and cols must be positive")
    if cell_width <= 0 or cell_height <= 0:
        raise ValueError("cell dimensions must be positive")
    for name, value in {
        "subject_height_ratio": subject_height_ratio,
        "subject_width_ratio": subject_width_ratio,
        "feet_ratio": feet_ratio,
    }.items():
        if not 0 < value < 1:
            raise ValueError(f"{name} must be between 0 and 1")

    cleaned = remove_bg_magenta(source.convert("RGBA"), threshold, edge_threshold)
    bbox = cleaned.getbbox()
    if not bbox:
        raise ValueError("input has no visible subject after background removal")
    subject = cleaned.crop(bbox)

    target_height = cell_height * subject_height_ratio
    target_width = cell_width * subject_width_ratio
    scale = min(target_height / subject.height, target_width / subject.width)
    out_width = max(1, int(round(subject.width * scale)))
    out_height = max(1, int(round(subject.height * scale)))
    subject = subject.resize((out_width, out_height), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (cols * cell_width, rows * cell_height), (255, 0, 255, 255))
    feet_y = int(round(cell_height * feet_ratio))
    paste_x_in_cell = (cell_width - out_width) // 2
    paste_y_in_cell = feet_y - out_height
    if paste_x_in_cell < 0 or paste_y_in_cell < 0:
        raise ValueError("subject ratios place the reference outside the cell")

    for row in range(rows):
        for col in range(cols):
            canvas.alpha_composite(
                subject,
                (col * cell_width + paste_x_in_cell, row * cell_height + paste_y_in_cell),
            )
    return canvas


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--cols", type=int, required=True)
    parser.add_argument("--cell-width", type=int, default=512)
    parser.add_argument("--cell-height", type=int, default=512)
    parser.add_argument("--subject-height-ratio", type=float, default=0.66)
    parser.add_argument("--subject-width-ratio", type=float, default=0.72)
    parser.add_argument("--feet-ratio", type=float, default=0.82)
    parser.add_argument("--threshold", type=int, default=100)
    parser.add_argument("--edge-threshold", type=int, default=150)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    layout = build_anchor_layout(
        Image.open(args.input),
        rows=args.rows,
        cols=args.cols,
        cell_width=args.cell_width,
        cell_height=args.cell_height,
        subject_height_ratio=args.subject_height_ratio,
        subject_width_ratio=args.subject_width_ratio,
        feet_ratio=args.feet_ratio,
        threshold=args.threshold,
        edge_threshold=args.edge_threshold,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    layout.convert("RGB").save(args.output)


if __name__ == "__main__":
    main()
