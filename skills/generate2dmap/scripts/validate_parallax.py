#!/usr/bin/env python3
"""Validate native parallax layer images and camera-envelope canvas coverage."""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image


SCHEMA = "generate2dmap.parallax_validation.v1"


def number(value: Any, name: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if positive and value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return float(value)


def pair(value: Any, name: str, positive: bool = False) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must contain two numbers.")
    return [number(v, name, positive) for v in value]


def boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false.")
    return value


def seam_metrics(image: Image.Image, axis: str) -> dict[str, Any]:
    """Measure opposite edges, not artistic seamlessness or a render simulation."""
    count = image.height if axis == "x" else image.width
    alpha_error = rgb_error = premultiplied_error = 0.0
    visible = 0
    for i in range(count):
        a = image.getpixel((0, i) if axis == "x" else (i, 0))
        b = image.getpixel((image.width - 1, i) if axis == "x" else (i, image.height - 1))
        alpha_error += abs(a[3] - b[3])
        premultiplied_error += sum(abs(a[c] * a[3] - b[c] * b[3]) / 255 for c in range(3))
        if a[3] and b[3]:
            visible += 1
            rgb_error += sum(abs(a[c] - b[c]) for c in range(3))
    return {"axis": axis, "alpha_mae": alpha_error / count,
            "visible_rgb_mae": rgb_error / (visible * 3) if visible else None,
            "jointly_visible_pixels": visible,
            "premultiplied_rgb_mae": premultiplied_error / (count * 3),
            "seamless_verified": False}


def validate_plan(plan: Any, base_dir: Path) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("Plan must be a JSON object.")
    viewport = pair(plan.get("viewport"), "viewport", positive=True)
    camera = plan.get("camera", {})
    if not isinstance(camera, dict):
        raise ValueError("camera must be an object.")
    ranges = {key: pair(camera.get(key, default), f"camera.{key}", positive=key == "zoom")
              for key, default in (("x", [0, 0]), ("y", [0, 0]), ("zoom", [1, 1]))}
    if any(values[0] > values[1] for values in ranges.values()):
        raise ValueError("Camera intervals must be ordered [minimum,maximum].")
    extremes = list(itertools.product(*(sorted(set(ranges[key])) for key in ("x", "y", "zoom"))))
    layers = plan.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("layers must be a nonempty list.")
    ids: set[str] = set()
    reports: list[dict[str, Any]] = []
    issues: list[str] = []
    sky_count = 0
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError("Every layer must be an object.")
        identity = layer.get("id")
        if not isinstance(identity, str) or not identity.strip() or identity in ids:
            raise ValueError("Layer ids must be unique nonempty strings.")
        ids.add(identity)
        role = layer.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"{identity}: role must be a nonempty string.")
        sky = role == "sky"
        sky_count += int(sky)
        expected = layer.get("alpha")
        if not isinstance(expected, str) or expected not in {"opaque", "transparent"}:
            raise ValueError(f"{identity}: alpha must explicitly be opaque or transparent.")
        if any(key in layer for key in ("width", "height", "display_size", "scale_xy", "repeat_width", "repeat_height")):
            raise ValueError(f"{identity}: use one uniform scale; display size and repeat period come from the actual image.")
        scale = number(layer.get("scale", 1), f"{identity}.scale", positive=True)
        offset = pair(layer.get("offset", [0, 0]), f"{identity}.offset")
        anchor = pair(layer.get("anchor_px", [0, 0]), f"{identity}.anchor_px")
        factor = pair(layer.get("scroll_factor", [1, 1]), f"{identity}.scroll_factor")
        repeat = layer.get("repeat", [False, False])
        if not isinstance(repeat, list) or len(repeat) != 2:
            raise ValueError(f"{identity}: repeat must contain two booleans.")
        repeat = [boolean(v, f"{identity}.repeat") for v in repeat]
        required = boolean(layer.get("require_canvas_coverage", sky), f"{identity}.require_canvas_coverage") or sky
        allow_empty = boolean(layer.get("allow_empty", False), f"{identity}.allow_empty")
        path_value = layer.get("image")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError(f"{identity}: image must be a path string.")
        path = Path(path_value)
        path = (base_dir / path).resolve() if not path.is_absolute() else path.resolve()
        with Image.open(path) as opened:
            source_mode = opened.mode
            image = opened.convert("RGBA")
        source_size = list(image.size)
        if any(not 0 <= anchor[i] <= source_size[i] for i in range(2)):
            raise ValueError(f"{identity}: anchor_px must lie within the source image canvas.")
        display = [value * scale for value in source_size]
        if not all(math.isfinite(v) for v in display):
            raise ValueError(f"{identity}: computed display dimensions are not finite.")
        alpha = image.getchannel("A")
        histogram = alpha.histogram()
        total = image.width * image.height
        minimum, maximum = alpha.getextrema()
        bbox = alpha.getbbox()
        alpha_info = {"expected": expected, "min": minimum, "max": maximum,
                      "fully_clear_pixels": histogram[0], "partly_transparent_pixels": sum(histogram[1:255]),
                      "fully_opaque_pixels": histogram[255], "total_pixels": total,
                      "nonzero_bbox_px": list(bbox) if bbox else None, "allow_empty": allow_empty}
        local_issues: list[str] = []
        if expected == "opaque" and minimum != 255:
            local_issues.append("Declared opaque image contains transparent pixels.")
        if expected == "transparent" and minimum == 255:
            local_issues.append("Declared transparent image is fully opaque; RGB checkerboard is not alpha.")
        if maximum == 0 and not allow_empty:
            local_issues.append("Layer is entirely transparent; use allow_empty only for an intentional empty overlay.")
        if sky and (expected != "opaque" or minimum != 255):
            local_issues.append("Sky must declare opaque and be fully opaque.")
        if role in {"foreground", "foreground_overlay"} and (expected != "transparent" or minimum == 255):
            local_issues.append("Foreground overlays must declare transparent and contain actual transparency.")
        snapshots = []
        for cx, cy, zoom in extremes:
            position = [(offset[i] - anchor[i] * scale - (cx, cy)[i] * factor[i]) * zoom for i in range(2)]
            rendered = [value * zoom for value in display]
            if not all(math.isfinite(v) for v in position + rendered):
                raise ValueError(f"{identity}: camera transform is not finite.")
            extent = [position[0], position[1], position[0] + rendered[0], position[1] + rendered[1]]
            if not all(math.isfinite(v) for v in extent):
                raise ValueError(f"{identity}: transformed canvas bounds are not finite.")
            covered = [repeat[i] or (position[i] <= 1e-7 and extent[i + 2] >= viewport[i] - 1e-7) for i in range(2)]
            transformed_bbox = None
            if bbox:
                transformed_bbox = [position[i % 2] + bbox[i] * scale * zoom for i in range(4)]
            snapshots.append({"camera": [cx, cy], "zoom": zoom, "screen_canvas_rect": extent,
                              "canvas_coverage_axes": covered, "canvas_covers_viewport": all(covered),
                              "nonzero_alpha_bbox_screen": transformed_bbox})
        canvas_passed = all(sample["canvas_covers_viewport"] for sample in snapshots)
        if required and not canvas_passed:
            local_issues.append("Image canvas does not cover the viewport at every camera/zoom extremum.")
        seams = [seam_metrics(image, axis) for axis, enabled in zip(("x", "y"), repeat) if enabled]
        issues.extend(f"{identity}: {message}" for message in local_issues)
        reports.append({"id": identity, "role": role, "image": str(path), "source_mode": source_mode,
                        "source_size": source_size, "scale": scale, "display_size": display,
                        "offset": offset, "anchor_px": anchor, "scroll_factor": factor,
                        "repeat": repeat, "repeat_period_world_px": [display[i] if repeat[i] else None for i in range(2)],
                        "alpha": alpha_info, "require_canvas_coverage": required,
                        "canvas_coverage_passed": canvas_passed,
                        "opaque_viewport_coverage_verified": sky and minimum == 255 and canvas_passed,
                        "extrema": snapshots, "repeat_seams": seams,
                        "passed": not local_issues, "issues": local_issues})
    if sky_count != 1:
        issues.append("Plan requires exactly one opaque sky base layer.")
    return {"schema": SCHEMA, "passed": not issues, "issues": issues, "viewport": viewport,
            "camera": ranges, "camera_extrema_checked": len(extremes), "layers": reports,
            "transform": "screenTopLeft=(offset-anchor_px*scale-camera*scroll_factor)*zoom; "
                         "screenSize=sourceSize*scale*zoom. Repeats use an unbounded integer tile lattice.",
            "limits": ["Coverage checks the rectangular image canvas, not pixels hidden by transparency. "
                       "Only a fully opaque sky can certify opaque viewport coverage.",
                       "Alpha bounding boxes include empty holes; repeating layers report the seed tile bbox only.",
                       "Endpoint checks cover independent bounded camera x/y and positive uniform zoom under this transform. "
                       "Rotations, perspective, animation, renderer rounding and culling are not simulated.",
                       "Repeat edge metrics are diagnostics, not a seamlessness pass or image repair."]}


def check_report_destination(report: Path, spec: Path, plan: Any) -> None:
    """Reject input aliases, including resolved links and Windows path casing."""
    destination = report.resolve()
    spec_path = spec.resolve()
    inputs = [spec_path]
    layers = plan.get("layers") if isinstance(plan, dict) else None
    if isinstance(layers, list):
        for layer in layers:
            value = layer.get("image") if isinstance(layer, dict) else None
            if isinstance(value, str) and value:
                inputs.append((spec_path.parent / value).resolve())
    for source in inputs:
        same_path = os.path.normcase(str(destination)) == os.path.normcase(str(source))
        same_file = destination.exists() and source.exists() and destination.samefile(source)
        if same_path or same_file:
            raise ValueError(f"Report path aliases an input file: {source}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--report", type=Path, help="Optional JSON report file; images are never modified.")
    args = parser.parse_args()
    plan = None
    try:
        plan = json.loads(args.spec.read_text(encoding="utf-8"))
        result = validate_plan(plan, args.spec.resolve().parent)
    except (ValueError, OSError) as error:
        result = {"schema": SCHEMA, "passed": False, "issues": [str(error)]}
    if args.report:
        try:
            check_report_destination(args.report, args.spec, plan)
        except (ValueError, OSError) as error:
            result = {"schema": SCHEMA, "passed": False, "issues": [str(error)]}
            args.report = None
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
