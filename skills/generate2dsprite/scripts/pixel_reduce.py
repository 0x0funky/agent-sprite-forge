#!/usr/bin/env python3
"""Reduce an upscaled pixel-art image to its logical pixels on an OKLab palette.

Detects the integer grid (the logical pixel size, or period, and its phase),
takes one colour per grid block (the mode of palette indices, the box mean or
the block centre), snaps it to an OKLab palette, makes alpha binary and writes
the 1x image, plus an optional integer upscale. QC re-expands the result and
compares it with the input. Images without a clean grid are refused with
'no clean grid' unless --force: most image-model "pixel art" (soft edges, no
lattice) has none, and this tool never invents one.

The new --output-dir holds <stem>.png (1x), <stem>@<N>x.png (--upscale N),
indexed/<stem>.png (--indexed), palette.json and pixel-reduce-qa.json. On
success one ASCII JSON line names the folder and the QA file. Usage errors exit
2 (argparse); every other failure prints one "error: ..." line, publishes nothing
and exits 1.

Example: python pixel_reduce.py --input hero-6x.png --output-dir hero-1x
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import numpy as np
    import forge_core  # this skill's vendored copies
    import forge_palette as fp
    import palette_tool as tool
except ImportError as missing:  # numpy and Pillow are needed before forge_core can report it
    sys.stderr.write(f"error: missing Python module {missing.name or missing}; "
                     "install with: python -m pip install numpy Pillow\n")
    raise SystemExit(1)

TOOL_NAME = "pixel_reduce"
METHODS = ("mode", "box", "center")
AUTO_EXACT_COLOURS = 256     # --colors auto keeps every colour up to this many ...
AUTO_COLOURS = 32            # ... and learns this many beyond it


def _period(value: str) -> int:
    number = int(value)
    if number < 2:
        raise argparse.ArgumentTypeError("a grid period is at least 2")
    return number


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected an integer >= 1")
    return number


def measure_grid(image: np.ndarray, args: argparse.Namespace) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    """``(grid, clean, detected)``: the grid used (detected, or measured at --period), whether it is clean
    enough to reduce, and what detection alone finds (to check a given --period against)."""
    detected = fp.detect_grid(image, max_period=args.max_period, tol=args.tolerance, min_score=args.min_score)
    grid = detected
    if args.period:
        grid = fp.grid_score(image, args.period, phase=args.phase, tol=args.tolerance, min_score=args.min_score)
    clean = grid["period"] >= 2 and grid["score"] >= args.min_score and grid["uniformity"] >= args.min_uniformity
    return grid, clean, detected


def _block_mode(blocks: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Most frequent index of every row of ``blocks`` (ties to the lowest index) and its share."""
    count, size = blocks.shape
    modes = np.empty(count, np.uint8)
    shares = np.empty(count, np.float64)
    step = 16384
    for start in range(0, count, step):
        chunk = blocks[start:start + step].astype(np.int64)
        offsets = (np.arange(len(chunk))[:, None] * 256 + chunk).reshape(-1)
        counts = np.bincount(offsets, minlength=len(chunk) * 256).reshape(len(chunk), 256)
        modes[start:start + step] = counts.argmax(1)
        shares[start:start + step] = counts.max(1) / size
    return modes, shares


def reduce_image(pixels: np.ndarray, period: int, phase: Sequence[int], palette: fp.Palette, method: str,
                 alpha_threshold: int) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """``(index map, cropped input, block shares)`` for the whole grid blocks from ``phase`` on."""
    phase_x, phase_y = int(phase[0]), int(phase[1])
    height, width = pixels.shape[:2]
    rows, cols = (height - phase_y) // period, (width - phase_x) // period
    if rows < 1 or cols < 1:
        raise tool.ToolError(f"the image is smaller than one {period} px grid block")
    crop = pixels[phase_y:phase_y + rows * period, phase_x:phase_x + cols * period]
    shares = None
    if method == "center":
        sample = np.ascontiguousarray(crop[period // 2::period, period // 2::period][:rows, :cols])
        index = fp.quantize_image(sample, palette, alpha_threshold=alpha_threshold)
    elif method == "box":
        blocks = crop.reshape(rows, period, cols, period, 4).astype(np.float64)
        alpha = blocks[..., 3] / 255.0
        mass = alpha.sum(axis=(1, 3))
        colour = (blocks[..., :3] * alpha[..., None]).sum(axis=(1, 3)) / np.maximum(mass, 1e-9)[..., None]
        mean = np.zeros((rows, cols, 4), np.uint8)
        mean[..., :3] = np.floor(np.clip(colour, 0.0, 255.0) + 0.5)
        mean[..., 3] = np.floor(mass / (period * period) * 255.0 + 0.5)
        index = fp.quantize_image(mean, palette, alpha_threshold=alpha_threshold)
    else:
        full = fp.quantize_image(crop, palette, alpha_threshold=alpha_threshold)
        blocks = full.reshape(rows, period, cols, period).transpose(0, 2, 1, 3).reshape(rows * cols, -1)
        modes, shares = _block_mode(blocks)
        index = modes.reshape(rows, cols)
        shares = shares.reshape(rows, cols)
    return index, crop, shares


def _palette(args: argparse.Namespace, images: Sequence[np.ndarray]) -> tuple[fp.Palette, bool]:
    if args.palette is not None:
        return fp.read_palette(args.palette), False
    colours = np.concatenate([image[..., :3][image[..., 3] >= args.alpha_threshold] for image in images])
    if not len(colours):
        raise tool.ToolError("every pixel is below the alpha threshold; nothing to reduce")
    count = args.colors
    if count is None:
        distinct = len(np.unique(colours.astype(np.uint32) @ np.array([65536, 256, 1], np.uint32)))
        count = distinct if distinct <= AUTO_EXACT_COLOURS else AUTO_COLOURS
    palette = fp.build_palette(images, count, seed=args.seed, alpha_threshold=args.alpha_threshold)
    note = f"pixel_reduce: OKLab k-means++ of {len(images)} image(s), {count} colours, seed {args.seed}"
    return palette.replace(source=note), True


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.phase is not None and not args.period:
        raise tool.ToolError("--phase needs --period")
    if args.colors is not None and not 1 <= args.colors <= 256:
        raise tool.ToolError("--colors must be 1..256")
    inputs = tool.expand_inputs(args.input)
    names = tool.output_names(inputs)
    images, _ = tool.load_images(inputs)
    for image in images:
        image[image[..., 3] == 0] = 0
    grids = [measure_grid(image, args) for image in images]
    refused = [(path, grid) for path, (grid, clean, _) in zip(inputs, grids) if not clean]
    if refused and not args.force:
        path, grid = refused[0]
        raise tool.ToolError(
            f"no clean grid in {path.name}: best period {grid['period']} phase {tuple(grid['phase'])}, "
            f"score {grid['score']} (needs >= {args.min_score}), uniformity {grid['uniformity']} "
            f"(needs >= {args.min_uniformity}); it is not a scaled pixel-art image"
            + (f" ({len(refused)} of {len(inputs)} inputs refused)" if len(refused) > 1 else "")
            + ". Use --force (best with --period N) to reduce it anyway; the result is lossy")
    flat = [path.name for path, (grid, _, _) in zip(inputs, grids) if grid["period"] < 2]
    if flat:
        raise tool.ToolError(f"{flat[0]} has no colour edges to find a grid in; pass --period N with --force")
    palette, built = _palette(args, images)
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        checks, details, outputs = [], {}, []
        for name, image, (grid, clean, detected) in zip(names, images, grids):
            period = grid["period"]
            index, crop, shares = reduce_image(image, period, grid["phase"], palette, args.method,
                                               args.alpha_threshold)
            logical = fp.render_indices(index, palette)
            forge_core.save_png(logical, stage / name)
            outputs.append(stage / name)
            if args.upscale > 1:
                upscaled = np.repeat(np.repeat(logical, args.upscale, axis=0), args.upscale, axis=1)
                target = stage / f"{Path(name).stem}@{args.upscale}x.png"
                forge_core.save_png(upscaled, target)
                outputs.append(target)
            if args.indexed:
                (stage / "indexed").mkdir(exist_ok=True)
                fp.save_indexed_png(index, palette, stage / "indexed" / name, palette.transparent_index)
                outputs.append(stage / "indexed" / name)
            rebuilt = np.repeat(np.repeat(logical, period, axis=0), period, axis=1).astype(np.int16)
            mismatch = float((np.abs(rebuilt - crop.astype(np.int16)).max(-1) > args.tolerance).mean())
            visible = image[..., 3] >= args.alpha_threshold
            lost = int(visible.sum() - (crop[..., 3] >= args.alpha_threshold).sum())
            checks += [
                tool.check("grid_clean", {key: grid[key] for key in ("period", "phase", "score", "uniformity")},
                           {"min_score": args.min_score, "min_uniformity": args.min_uniformity}, clean,
                           warn=True, subject=name),
                tool.check("reconstruction_mismatch", round(mismatch, 6), args.max_mismatch,
                           mismatch <= args.max_mismatch, warn=True, subject=name),
                tool.check("lost_visible_px", lost, 0, lost == 0, warn=True, subject=name)]
            if args.period:  # a given period that detection disagrees with (even a divisor) needs a look
                agrees = detected["period"] == grid["period"] or not detected["has_grid"]
                checks.append(tool.check("period_agrees", {"given": grid["period"], "detected": detected["period"],
                                                           "detected_score": detected["score"]},
                                         "detected period", agrees, warn=True, subject=name))
            details[name] = {"grid": grid, "detected": detected, "forced": not clean, "method": args.method,
                             "logical_size": [int(index.shape[1]), int(index.shape[0])],
                             "mixed_blocks": None if shares is None else int((shares < 0.5).sum())}
        if built:
            fp.write_palette(palette, stage / "palette.json", "json")
            outputs.append(stage / "palette.json")
        rgba_outputs = [path for path in outputs if path.parent == stage and path.suffix == ".png"]
        off_palette, partial = tool.measure_outputs(rgba_outputs, palette)
        checks += [tool.check("output_off_palette_px", off_palette, 0, off_palette == 0),
                   tool.check("output_partial_alpha_px", partial, 0, partial == 0)]
        envelope = tool.qa_envelope(
            checks=checks, tool=TOOL_NAME,
            method=("forge_palette.detect_grid (boundary lattice score and block uniformity at tolerance "
                    f"{args.tolerance}); one {args.method} sample per grid block snapped to the OKLab palette with "
                    "binary alpha; the 1x result is re-expanded by the period and compared with the input"),
            not_proven=["Fractional grids (a non-integer pixel size) are not detected.",
                        "Whether the logical image keeps the art's intent: look at the 1x and upscaled output."]
                       + (["The grid was forced (--force): the reduction is lossy."] if refused else []),
            inputs=[forge_core.file_ref(path, stage) for path in inputs]
                   + ([] if built else [forge_core.file_ref(args.palette, stage)]),
            outputs=[forge_core.file_ref(path, stage) for path in outputs],
            images=details, palette={"colors": len(palette), "built": built,
                                     "transparent_index": palette.transparent_index})
        forge_core.write_json(stage / "pixel-reduce-qa.json", envelope)
        tool.enforce(envelope, args.strict, "pixel_reduce")
    return {"status": envelope["status"], "output": str(final), "metadata": str(final / "pixel-reduce-qa.json"),
            "palette": str(final / "palette.json") if built else str(args.palette),
            "images": [{"file": name, "period": details[name]["grid"]["period"],
                        "logical_size": details[name]["logical_size"], "forced": details[name]["forced"]}
                       for name in names]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pixel_reduce.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, nargs="+", action="extend", required=True,
                        help="upscaled pixel-art images, or folders of PNGs")
    parser.add_argument("--output-dir", type=Path, required=True, help="new folder for the results (must not exist)")
    colours = parser.add_mutually_exclusive_group()
    colours.add_argument("--palette", type=Path, help="palette file to snap to (a locked palette stays unchanged)")
    colours.add_argument("--colors", type=int,
                         help="learn this many colours (default: keep every colour up to 256, else learn 32)")
    parser.add_argument("--method", choices=METHODS, default="mode",
                        help="block sample: mode of palette indices (default), box mean, or centre pixel")
    parser.add_argument("--period", type=_period, help="logical pixel size in px; skips detection")
    parser.add_argument("--phase", type=int, nargs=2, metavar=("X", "Y"), help="grid offset with --period")
    parser.add_argument("--max-period", type=_period, default=32, help="largest period to search (default 32)")
    parser.add_argument("--tolerance", type=int, default=24,
                        help="channel step that counts as a boundary or breaks block uniformity (default 24)")
    parser.add_argument("--min-score", type=float, default=0.5, help="clean grid: lattice score at least (default 0.5)")
    parser.add_argument("--min-uniformity", type=float, default=0.9,
                        help="clean grid: share of uniform blocks at least (default 0.9)")
    parser.add_argument("--max-mismatch", type=float, default=0.05,
                        help="warn when more of the re-expanded result differs from the input (default 0.05)")
    parser.add_argument("--alpha-threshold", type=tool.alpha_level, default=128,
                        help="alpha >= this is opaque (default 128)")
    parser.add_argument("--upscale", type=_positive, default=1, help="also write an integer N x nearest upscale")
    parser.add_argument("--indexed", action="store_true", help="also write an indexed PNG into indexed/")
    parser.add_argument("--seed", type=int, default=0, help="k-means++ seed when learning colours (default 0)")
    parser.add_argument("--force", action="store_true", help="reduce even without a clean grid (lossy, warns)")
    parser.add_argument("--strict", action="store_true", help="treat QA warnings (forced grid, mismatch) as failures")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return tool.run_main(build_parser(), {"run": run}, argv)


if __name__ == "__main__":
    raise SystemExit(main())
