#!/usr/bin/env python3
"""Extract native-alpha or chroma-key map props from a grid or explicit crop boxes."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections import deque
from pathlib import Path
from typing import Iterable

from PIL import Image


MAGENTA = (255, 0, 255)


def color_distance(rgb: tuple[int, int, int], target: tuple[int, int, int] = MAGENTA) -> float:
    r, g, b = rgb
    tr, tg, tb = target
    return math.sqrt((r - tr) ** 2 + (g - tg) ** 2 + (b - tb) ** 2)


def remove_bg_magenta(img: Image.Image, threshold: int, edge_threshold: int) -> Image.Image:
    img = img.convert("RGBA")
    pixels = img.load()
    width, height = img.size

    for x in range(width):
        for y in range(height):
            r, g, b, a = pixels[x, y]
            if a > 0 and color_distance((r, g, b)) < threshold:
                pixels[x, y] = (0, 0, 0, 0)

    visited: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        queue.append((x, 0))
        queue.append((x, height - 1))
    for y in range(height):
        queue.append((0, y))
        queue.append((width - 1, y))

    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or x < 0 or x >= width or y < 0 or y >= height:
            continue
        visited.add((x, y))
        r, g, b, a = pixels[x, y]
        should_expand = a == 0
        if a > 0 and color_distance((r, g, b)) < edge_threshold:
            pixels[x, y] = (0, 0, 0, 0)
            should_expand = True
        if should_expand:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nxt = (x + dx, y + dy)
                    if nxt not in visited:
                        queue.append(nxt)

    return img


def trim_border(img: Image.Image, px: int) -> Image.Image:
    if px <= 0:
        return img
    width, height = img.size
    if width <= px * 2 or height <= px * 2:
        return img
    return img.crop((px, px, width - px, height - px))


def clean_edges(img: Image.Image, depth: int) -> Image.Image:
    if depth <= 0:
        return img
    pixels = img.load()
    width, height = img.size
    for d in range(depth):
        for x in range(width):
            for y in (d, height - 1 - d):
                if 0 <= y < height:
                    r, g, b, a = pixels[x, y]
                    if a > 0 and ((r < 40 and g < 40 and b < 40) or color_distance((r, g, b)) < 150):
                        pixels[x, y] = (0, 0, 0, 0)
        for y in range(height):
            for x in (d, width - 1 - d):
                if 0 <= x < width:
                    r, g, b, a = pixels[x, y]
                    if a > 0 and ((r < 40 and g < 40 and b < 40) or color_distance((r, g, b)) < 150):
                        pixels[x, y] = (0, 0, 0, 0)
    return img


def connected_components(img: Image.Image, min_area: int) -> list[dict[str, object]]:
    alpha = img.getchannel("A")
    pixels = alpha.load()
    width, height = img.size
    visited = [[False] * width for _ in range(height)]
    components: list[dict[str, object]] = []

    for y in range(height):
        for x in range(width):
            if pixels[x, y] == 0 or visited[y][x]:
                continue
            queue: deque[tuple[int, int]] = deque([(x, y)])
            visited[y][x] = True
            coords: list[tuple[int, int]] = []
            min_x = max_x = x
            min_y = max_y = y
            touches_edge = x == 0 or y == 0 or x == width - 1 or y == height - 1

            while queue:
                cx, cy = queue.popleft()
                coords.append((cx, cy))
                min_x = min(min_x, cx)
                min_y = min(min_y, cy)
                max_x = max(max_x, cx)
                max_y = max(max_y, cy)
                if cx == 0 or cy == 0 or cx == width - 1 or cy == height - 1:
                    touches_edge = True
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < width and 0 <= ny < height and pixels[nx, ny] > 0 and not visited[ny][nx]:
                        visited[ny][nx] = True
                        queue.append((nx, ny))

            if len(coords) >= min_area:
                components.append(
                    {
                        "area": len(coords),
                        "bbox": (min_x, min_y, max_x + 1, max_y + 1),
                        "touches_edge": touches_edge,
                        "coords": coords,
                    }
                )

    components.sort(key=lambda item: int(item["area"]), reverse=True)
    return components


def pad_bbox(bbox: tuple[int, int, int, int], padding: int, width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    return (
        max(0, x0 - padding),
        max(0, y0 - padding),
        min(width, x1 + padding),
        min(height, y1 + padding),
    )


def bbox_touches_edge(
    bbox: tuple[int, int, int, int] | None,
    width: int,
    height: int,
    margin: int,
) -> bool:
    if bbox is None:
        return False
    x0, y0, x1, y1 = bbox
    return x0 <= margin or y0 <= margin or x1 >= width - margin or y1 >= height - margin


def sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "prop"


def parse_labels(args: argparse.Namespace, expected_count: int) -> list[str]:
    labels: list[str] = []
    if args.labels:
        labels = [item.strip() for item in args.labels.split(",")]
    if args.labels_file:
        labels = [
            line.strip()
            for line in args.labels_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if not labels:
        labels = [f"prop-{index + 1}" for index in range(expected_count)]
    if len(labels) > expected_count:
        raise ValueError(f"Got {len(labels)} labels for {expected_count} cells.")
    labels.extend(f"prop-{index + 1}" for index in range(len(labels), expected_count))
    result = [sanitize_slug(label) if label.lower() not in {"empty", "skip", "-"} else "" for label in labels]
    nonempty = [label for label in result if label]
    if len(set(nonempty)) != len(nonempty):
        raise ValueError("Labels must remain unique after filename normalization.")
    return result


def alpha_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    return img.getchannel("A").getbbox()


def mask_to_component(img: Image.Image, component: dict[str, object]) -> Image.Image:
    selected = Image.new("RGBA", img.size, (0, 0, 0, 0))
    src = img.load()
    dst = selected.load()
    for x, y in component["coords"]:  # type: ignore[index]
        dst[x, y] = src[x, y]
    return selected


def extract_cell(
    cell: Image.Image,
    args: argparse.Namespace,
) -> tuple[Image.Image | None, dict[str, object]]:
    original_edge_touch = bbox_touches_edge(alpha_bbox(cell), cell.width, cell.height, args.edge_touch_margin)
    frame = trim_border(cell, args.trim_border)
    frame = clean_edges(frame, args.edge_clean_depth)
    components = connected_components(frame, args.min_component_area)
    selected_component = None
    bbox = alpha_bbox(frame)

    if args.component_mode == "largest" and components:
        selected_component = components[0]
        frame = mask_to_component(frame, selected_component)
        bbox = tuple(selected_component["bbox"])  # type: ignore[arg-type]
    elif args.component_mode == "all" and components:
        selected = Image.new("RGBA", frame.size)
        for component in components:
            part = mask_to_component(frame, component)
            selected.alpha_composite(part)
        frame = selected
        bbox = alpha_bbox(frame)
    else:
        bbox = None

    padded_bbox = pad_bbox(bbox, args.component_padding, frame.width, frame.height) if bbox else None
    edge_touch = original_edge_touch or bbox_touches_edge(bbox, frame.width, frame.height, args.edge_touch_margin)
    prop = frame.crop(padded_bbox) if padded_bbox else None

    return prop, {
        "component_mode": args.component_mode,
        "component_count": len(components),
        "selected_component_area": int(selected_component["area"]) if selected_component else None,
        "selected_component_bbox": list(selected_component["bbox"]) if selected_component else None,
        "crop_bbox": list(bbox) if bbox else None,
        "padded_crop_bbox": list(padded_bbox) if padded_bbox else None,
        "edge_touch": edge_touch,
        "output_size": list(prop.size) if prop else [0, 0],
    }


def iter_cells(img: Image.Image, rows: int, cols: int) -> Iterable[tuple[int, int, tuple[int, int, int, int], Image.Image]]:
    width, height = img.size
    if rows <= 0 or cols <= 0 or width % cols or height % rows:
        raise ValueError("Positive rows/cols must divide the source dimensions exactly.")
    cell_width = width // cols
    cell_height = height // rows
    for row in range(rows):
        for col in range(cols):
            box = (col * cell_width, row * cell_height, (col + 1) * cell_width, (row + 1) * cell_height)
            yield row, col, box, img.crop(box)


def read_crop_boxes(path: Path, size: tuple[int, int]) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Load inspected, nonoverlapping native boxes; never infer or resize art."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = data.get("props") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("Crop specification requires a nonempty 'props' list.")
    width, height = size
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or not entry["label"].strip():
            raise ValueError("Each crop requires a nonempty string label.")
        label = sanitize_slug(entry["label"])
        if entry["label"].strip().lower() in {"empty", "skip", "-"}:
            raise ValueError("Explicit crop boxes must name props; omit empty cells.")
        if any(previous == label for previous, _ in boxes):
            raise ValueError("Labels must remain unique after filename normalization.")
        raw_box = entry.get("source_box")
        if not isinstance(raw_box, list) or len(raw_box) != 4 or not all(type(v) is int for v in raw_box):
            raise ValueError(f"Crop {label!r} source_box must contain four integer pixel coordinates.")
        x0, y0, x1, y1 = raw_box
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError(f"Crop {label!r} source_box must be nonempty and inside the source canvas.")
        for other_label, (ox0, oy0, ox1, oy1) in boxes:
            if max(x0, ox0) < min(x1, ox1) and max(y0, oy0) < min(y1, oy1):
                raise ValueError(f"Crop {label!r} overlaps crop {other_label!r}.")
        boxes.append((label, (x0, y0, x1, y1)))
    return boxes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--rows", type=int)
    parser.add_argument("--cols", type=int)
    parser.add_argument("--boxes-file", type=Path,
                        help="JSON with props[{label, source_box:[left,top,right,bottom]}]; instead of grid options.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--labels", help="Comma-separated labels in row-major order.")
    parser.add_argument("--labels-file", type=Path)
    parser.add_argument("--threshold", type=int, default=100)
    parser.add_argument("--edge-threshold", type=int, default=150)
    parser.add_argument("--background-mode", choices=("auto", "chroma_key", "native_alpha"), default="auto")
    parser.add_argument("--alpha-floor", type=int, default=0,
                        help="Opt-in: zero alpha values at or below this 0..254 threshold; default preserves alpha.")
    parser.add_argument("--trim-border", type=int, default=0)
    parser.add_argument("--edge-clean-depth", type=int, default=0)
    parser.add_argument("--component-mode", choices=["all", "largest"], default="largest")
    parser.add_argument("--component-padding", type=int, default=8)
    parser.add_argument("--min-component-area", type=int, default=100)
    parser.add_argument("--edge-touch-margin", type=int, default=0)
    parser.add_argument("--reject-edge-touch", action="store_true")
    parser.add_argument("--keep-empty", action="store_true")
    return parser


def extract(args: argparse.Namespace) -> dict[str, object]:
    if args.boxes_file:
        if any(value is not None for value in (args.rows, args.cols, args.labels, args.labels_file)):
            raise ValueError("boxes-file cannot be combined with rows, cols, labels or labels-file.")
    elif args.rows is None or args.cols is None or args.rows <= 0 or args.cols <= 0:
        raise ValueError("Positive rows and cols are required unless boxes-file is supplied.")
    if not 0 <= args.alpha_floor <= 254:
        raise ValueError("alpha-floor must be between 0 and 254.")
    for name in ("trim_border", "edge_clean_depth", "component_padding", "edge_touch_margin"):
        if getattr(args, name) < 0:
            raise ValueError(f"{name} must be nonnegative.")
    if args.min_component_area <= 0:
        raise ValueError("min-component-area must be positive.")
    if not 0 <= args.threshold <= args.edge_threshold <= 442:
        raise ValueError("Chroma thresholds must satisfy 0 <= threshold <= edge-threshold <= 442.")
    with Image.open(args.input) as opened:
        raw = opened.convert("RGBA")
    if args.boxes_file:
        boxes = read_crop_boxes(args.boxes_file, raw.size)
        labels = [label for label, _ in boxes]
    else:
        labels = parse_labels(args, args.rows * args.cols)
        if raw.width % args.cols or raw.height % args.rows:
            raise ValueError("Source dimensions must divide exactly into the requested grid.")
    mode = args.background_mode
    if mode == "auto":
        mode = "native_alpha" if raw.getchannel("A").getextrema()[0] < 255 else "chroma_key"
    if mode == "native_alpha" and raw.getchannel("A").getextrema()[0] == 255:
        raise ValueError("native_alpha requires real transparency, not an RGB checkerboard.")
    cleaned = raw if mode == "native_alpha" else remove_bg_magenta(raw, args.threshold, args.edge_threshold)
    alpha = cleaned.getchannel("A")
    alpha_floor_pixels_removed = sum(alpha.histogram()[1:args.alpha_floor + 1])
    if args.alpha_floor:
        cleaned = cleaned.copy()
        cleaned.putalpha(alpha.point(lambda value: 0 if value <= args.alpha_floor else value))
    manifest_path = args.manifest or (args.output_dir / "prop-pack.json")
    pending: list[tuple[Path, Image.Image]] = []
    accepted: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []

    cells = ((None, None, box, cleaned.crop(box)) for _, box in boxes) if args.boxes_file else iter_cells(cleaned, args.rows, args.cols)
    for index, (row, col, source_box, cell) in enumerate(cells):
        label = labels[index]
        cell_info: dict[str, object] = {
            "index": index,
            "label": label,
            "grid": None if args.boxes_file else [row, col],
            "source_box": list(source_box),
        }
        if not label:
            cell_info["status"] = "skipped-label"
            rejected.append(cell_info)
            continue

        prop, info = extract_cell(cell, args)
        cell_info.update(info)

        if prop is None:
            cell_info["status"] = "empty"
            if args.keep_empty:
                prop = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            else:
                rejected.append(cell_info)
                continue

        prop_dir = args.output_dir / label
        prop_path = prop_dir / "prop.png"
        pending.append((prop_path, prop))
        cell_info["status"] = "accepted"
        cell_info["image"] = Path(os.path.relpath(prop_path.resolve(), manifest_path.parent.resolve())).as_posix()
        accepted.append(cell_info)

    edge_touch_props = [item["label"] for item in accepted if bool(item.get("edge_touch"))]
    manifest = {
        "input": str(args.input),
        "source_size": list(raw.size),
        "source_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "layout_mode": "explicit_boxes" if args.boxes_file else "grid",
        "boxes_file": str(args.boxes_file) if args.boxes_file else None,
        "background_mode": mode,
        "alpha_floor": args.alpha_floor,
        "alpha_floor_pixels_removed": alpha_floor_pixels_removed,
        "rows": args.rows,
        "cols": args.cols,
        "threshold": args.threshold,
        "edge_threshold": args.edge_threshold,
        "component_mode": args.component_mode,
        "component_padding": args.component_padding,
        "min_component_area": args.min_component_area,
        "edge_touch_margin": args.edge_touch_margin,
        "accepted": accepted,
        "rejected": rejected,
        "edge_touch_props": edge_touch_props,
    }
    if args.reject_edge_touch and edge_touch_props:
        raise ValueError(f"Accepted props touch a cell edge: {edge_touch_props}")
    destinations = [manifest_path] + [path for path, _ in pending]
    inputs = [args.input] + ([args.labels_file] if args.labels_file else []) + ([args.boxes_file] if args.boxes_file else [])
    if len({os.path.normcase(str(path.resolve())) for path in destinations}) != len(destinations):
        raise ValueError("Manifest and image output paths must be distinct.")
    for destination in destinations:
        for source in inputs:
            if destination.resolve() == source.resolve() or (destination.exists() and source.exists() and destination.samefile(source)):
                raise ValueError("Output path aliases an input file.")
    for path, image in pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    args = build_parser().parse_args()
    extract(args)
    manifest_path = args.manifest or (args.output_dir / "prop-pack.json")
    print(str(manifest_path.resolve()))


if __name__ == "__main__":
    main()
