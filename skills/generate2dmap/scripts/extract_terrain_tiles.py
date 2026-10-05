#!/usr/bin/env python3
"""Slice an opaque terrain atlas into validated project-ready tile variants."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


SCHEMA = "generate2dmap.terrain_tile_bundle.v1"


def slug(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if not result:
        raise ValueError("Terrain names must contain letters or numbers.")
    return result


def portable_path(path: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def parse_row(value: str) -> tuple[str, int]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Use TERRAIN=ROW, for example plain=0.")
    name, row_text = value.split("=", 1)
    try:
        row = int(row_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Terrain row must be an integer.") from exc
    return slug(name), row


def parse_float_map(value: str) -> tuple[str, float]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Use TERRAIN=VALUE, for example fire=1.2.")
    name, number = value.split("=", 1)
    try:
        return slug(name), float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Mapped value must be numeric.") from exc


def image_metrics(image: Image.Image) -> dict[str, float]:
    luminance = image.convert("L")
    stat = ImageStat.Stat(luminance)
    return {
        "mean_luminance": round(stat.mean[0] / 255.0, 6),
        "contrast": round(stat.stddev[0] / 255.0, 6),
    }


def normalized_difference(left: Image.Image, right: Image.Image) -> float:
    size = (64, 64)
    left_small = left.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    right_small = right.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    difference = ImageChops.difference(left_small, right_small)
    return round(statistics.fmean(ImageStat.Stat(difference).mean) / 255.0, 6)


def extract(args: argparse.Namespace) -> dict[str, object]:
    if args.rows <= 0 or args.cols <= 0:
        raise ValueError("rows and cols must be positive integers.")
    if args.tile_size is not None and args.tile_size <= 0:
        raise ValueError("tile-size must be positive.")
    for name in ("runtime_world_size", "surface_y", "roughness", "min_contrast", "min_variant_difference"):
        if not math.isfinite(getattr(args, name)):
            raise ValueError(f"{name} must be finite.")
    if args.runtime_world_size <= 0 or not 0 <= args.roughness <= 1:
        raise ValueError("runtime-world-size must be positive and roughness must be in [0,1].")
    if not 0 <= args.min_contrast <= 1 or not 0 <= args.min_variant_difference <= 1:
        raise ValueError("QC thresholds must be in [0,1].")
    with Image.open(args.input) as opened:
        if opened.convert("RGBA").getchannel("A").getextrema() != (255, 255):
            raise ValueError("Terrain atlas must be opaque; transparency cannot be discarded.")
        source = opened.convert("RGB")
    if source.width % args.cols or source.height % args.rows:
        raise ValueError(
            f"Atlas {source.size} is not evenly divisible by {args.cols} columns x {args.rows} rows."
        )

    row_map = dict(args.terrain_row)
    if len(row_map) != len(args.terrain_row):
        raise ValueError("Terrain names may only be mapped once.")
    if len(row_map) != args.rows or set(row_map.values()) != set(range(args.rows)):
        raise ValueError(f"Terrain rows must cover every row from 0 to {args.rows - 1} exactly once.")

    emissions = dict(args.emission)
    if len(emissions) != len(args.emission):
        raise ValueError("Emission names may only be mapped once.")
    if any(not math.isfinite(value) or value < 0 for value in emissions.values()):
        raise ValueError("Emission values must be finite and nonnegative.")
    unknown_emissions = set(emissions) - set(row_map)
    if unknown_emissions:
        raise ValueError(f"Emission references unknown terrain: {sorted(unknown_emissions)}")

    cell_width = source.width // args.cols
    cell_height = source.height // args.rows
    if cell_width != cell_height and args.cell_shape != "crop-square":
        raise ValueError("Non-square source cells require explicit --cell-shape crop-square; cropping loses pixels.")
    output_size = args.tile_size or min(cell_width, cell_height)
    manifest_path = args.manifest or args.output_dir / "terrain-bundle.json"
    output_paths = [args.output_dir / f"{terrain}-{col + 1}.png" for terrain in row_map for col in range(args.cols)]
    inputs = [args.input] + ([args.prompt] if args.prompt else [])
    destinations = [manifest_path] + output_paths
    if len({os.path.normcase(str(path.resolve())) for path in destinations}) != len(destinations):
        raise ValueError("Manifest and image output paths must be distinct.")
    for destination in destinations:
        for input_path in inputs:
            if destination.resolve() == input_path.resolve() or (destination.exists() and input_path.exists() and destination.samefile(input_path)):
                raise ValueError("Output path aliases an input file.")
    pending_images: list[tuple[Path, Image.Image]] = []

    terrain_payload: dict[str, object] = {}
    warnings: list[str] = []
    for terrain, row in sorted(row_map.items(), key=lambda item: item[1]):
        variants: list[dict[str, object]] = []
        variant_images: list[Image.Image] = []
        for col in range(args.cols):
            cell = source.crop(
                (col * cell_width, row * cell_height, (col + 1) * cell_width, (row + 1) * cell_height)
            )
            if cell.width != cell.height:
                crop_size = min(cell.width, cell.height)
                x0 = (cell.width - crop_size) // 2
                y0 = (cell.height - crop_size) // 2
                cell = cell.crop((x0, y0, x0 + crop_size, y0 + crop_size))
            if cell.size != (output_size, output_size):
                cell = cell.resize((output_size, output_size), getattr(Image.Resampling, args.resampler.upper()))

            metrics = image_metrics(cell)
            if metrics["contrast"] < args.min_contrast:
                warnings.append(
                    f"{terrain}-{col + 1} contrast {metrics['contrast']:.4f} is below {args.min_contrast:.4f}."
                )
            filename = f"{terrain}-{col + 1}.png"
            output_path = args.output_dir / filename
            pending_images.append((output_path, cell))
            variant_images.append(cell)
            variants.append(
                {
                    "path": portable_path(output_path, manifest_path.parent),
                    "source_cell": [row, col],
                    **metrics,
                }
            )

        pair_differences: list[float] = []
        for left_index in range(len(variant_images)):
            for right_index in range(left_index + 1, len(variant_images)):
                value = normalized_difference(variant_images[left_index], variant_images[right_index])
                pair_differences.append(value)
                if value < args.min_variant_difference:
                    warnings.append(
                        f"{terrain} variants {left_index + 1} and {right_index + 1} are too similar ({value:.4f})."
                    )

        terrain_payload[terrain] = {
            "row": row,
            "variants": variants,
            "variant_difference_min": min(pair_differences) if pair_differences else 1.0,
            "material": {
                "roughness": args.roughness,
                "emission_energy": emissions.get(terrain, 0.0),
            },
        }

    if args.strict_qc and warnings:
        raise ValueError("Terrain atlas QC failed:\n- " + "\n- ".join(warnings))

    payload: dict[str, object] = {
        "schema": SCHEMA,
        "source": portable_path(args.input, manifest_path.parent),
        "prompt": portable_path(args.prompt, manifest_path.parent) if args.prompt else "",
        "grid": {
            "rows": args.rows,
            "cols": args.cols,
            "source_cell_size": [cell_width, cell_height],
            "output_tile_size": [output_size, output_size],
        },
        "runtime": {
            "engine_target": args.engine_target,
            "world_size": args.runtime_world_size,
            "surface_y": args.surface_y,
            "edge_policy": args.edge_policy,
        },
        "terrains": terrain_payload,
        "qc": {
            "min_contrast": args.min_contrast,
            "min_variant_difference": args.min_variant_difference,
            "warnings": warnings,
            "passed": not warnings,
            "seamless_verified": False,
        },
        "processing": {"cell_shape": args.cell_shape, "resampler": args.resampler},
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    for output_path, image in pending_images:
        image.save(output_path)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--cols", type=int, required=True)
    parser.add_argument("--terrain-row", action="append", type=parse_row, required=True)
    parser.add_argument("--tile-size", type=int)
    parser.add_argument("--cell-shape", choices=("square", "crop-square"), default="square")
    parser.add_argument("--resampler", choices=("nearest", "lanczos"), default="lanczos")
    parser.add_argument("--engine-target", default="project-native")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--edge-policy", choices=("isolated", "seamless"), default="isolated")
    parser.add_argument("--runtime-world-size", type=float, default=0.94)
    parser.add_argument("--surface-y", type=float, default=0.011)
    parser.add_argument("--roughness", type=float, default=0.88)
    parser.add_argument("--emission", action="append", type=parse_float_map, default=[])
    parser.add_argument("--min-contrast", type=float, default=0.035)
    parser.add_argument("--min-variant-difference", type=float, default=0.025)
    parser.add_argument("--strict-qc", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = extract(args)
    print(json.dumps({"schema": payload["schema"], "qc": payload["qc"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
