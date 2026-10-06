#!/usr/bin/env python3
"""Repeat an accepted character into a fixed template: magenta RGB by default,
or verified native-alpha RGBA. This is a generation reference, not pose alignment.

The output PNG must be new; it is written beside its destination and published
without replacing anything. Prints a one-line JSON summary on success.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)


def sprite_helpers():
    """Resolve the sibling processor without relying on cwd or sys.path."""
    name = "_anchor_layout_sprite_helpers"
    path = Path(__file__).resolve().with_name("generate2dsprite.py")
    cached = sys.modules.get(name)
    if cached is not None and Path(cached.__file__).resolve() == path:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load required sprite helpers: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


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
    background_mode: str = "chroma_key",
    resampler: str = "lanczos",
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

    if background_mode == "opaque":
        raise ValueError(
            "opaque is not supported for character anchor templates: an opaque rectangle "
            "does not isolate the subject. Supply verified native alpha or a chroma-key source."
        )
    helpers = sprite_helpers()
    resize_filter = helpers.resampling_filter(resampler)
    cleaned = helpers.prepare_background(source, background_mode, threshold, edge_threshold)
    bbox = cleaned.getchannel("A").getbbox()
    if not bbox:
        raise ValueError("input has no visible subject after background removal")
    subject = cleaned.crop(bbox)

    target_height = cell_height * subject_height_ratio
    target_width = cell_width * subject_width_ratio
    scale = min(target_height / subject.height, target_width / subject.width)
    out_width = max(1, int(round(subject.width * scale)))
    out_height = max(1, int(round(subject.height * scale)))
    subject = subject.resize((out_width, out_height), resize_filter)

    fill = (0, 0, 0, 0) if background_mode == "native_alpha" else (255, 0, 255, 255)
    canvas = Image.new("RGBA", (cols * cell_width, rows * cell_height), fill)
    feet_y = int(round(cell_height * feet_ratio))
    paste_x_in_cell = (cell_width - out_width) // 2
    paste_y_in_cell = feet_y - out_height
    if (paste_x_in_cell < 0 or paste_y_in_cell < 0
            or paste_x_in_cell + out_width > cell_width or paste_y_in_cell + out_height > cell_height):
        raise ValueError("subject ratios place the reference outside the cell")

    for row in range(rows):
        for col in range(cols):
            position = (col * cell_width + paste_x_in_cell, row * cell_height + paste_y_in_cell)
            if background_mode == "native_alpha":
                canvas.paste(subject, position)  # Copy RGBA directly; no alpha squaring or matte.
            else:
                canvas.alpha_composite(subject, position)
    return canvas


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    parser.add_argument("--background-mode", choices=("chroma_key", "native_alpha", "opaque"), default="chroma_key",
                        help="Default chroma_key produces a magenta RGB template. native_alpha requires actual transparency and retains RGBA. opaque is explicitly rejected because no isolated subject can be located.")
    parser.add_argument("--resampler", choices=("nearest", "lanczos"), default="lanczos",
                        help="Default lanczos preserves legacy behavior; nearest keeps the pixel-art palette when resizing.")
    parser.add_argument("--output", type=Path, required=True,
                        help="New .png file; an existing file is never replaced.")
    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output.resolve()
    if output.suffix.lower() != ".png":
        raise ValueError(f"--output must be a .png file: {output.name}")
    if os.path.lexists(output):
        raise FileExistsError(f"Refusing to overwrite existing output: {output}")
    source, info = forge_core.load_rgba(args.input)
    layout = build_anchor_layout(
        source,
        rows=args.rows,
        cols=args.cols,
        cell_width=args.cell_width,
        cell_height=args.cell_height,
        subject_height_ratio=args.subject_height_ratio,
        subject_width_ratio=args.subject_width_ratio,
        feet_ratio=args.feet_ratio,
        threshold=args.threshold,
        edge_threshold=args.edge_threshold,
        background_mode=args.background_mode,
        resampler=args.resampler,
    )
    image = layout if args.background_mode == "native_alpha" else layout.convert("RGB")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as temporary:
        staged = Path(temporary) / output.name
        forge_core.save_png(image, staged)
        forge_core.publish_file_no_replace(staged, output)
    print(json.dumps({"output": str(output), "size": list(image.size), "mode": image.mode,
                      "background_mode": args.background_mode, "input_sha256": info["sha256"]}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors exit 2; other failures print one ``error: ...`` line and exit 1 (D26, D27)."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
