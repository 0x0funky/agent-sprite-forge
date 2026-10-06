#!/usr/bin/env python3
"""Validate native parallax layer images and camera-envelope canvas coverage.

Each layer is placed with one uniform scale. Componentwise, with the zoom
pivot P (camera.pivot: "top-left" = [0, 0], "center" = viewport / 2, or an
[x, y] point in viewport pixels):

  screenTopLeft = P + (offset - anchor_px * scale - camera * scroll_factor - P) * zoom
  screenSize    = sourceSize * scale * zoom

camera is the world position at the viewport's top-left at zoom 1; zoom scales
the picture about P. Coverage is checked at every camera x/y and zoom extreme,
which is exact for this transform. The sky must cover the viewport; by default
repeated layers and near/foreground layers must cover it too (--coverage
sky-only is the old rule). Repeat seams are the one seam metric of every edge
tool, forge_core.edge_seam_report (D9): the wrap step against the art's own
column (or row) steps near it. Pixel-art plans ("pixel_art": true or --pixel-grid) must
keep whole screen pixels per source pixel. An aspect sweep (16:9, 19.5:9 and
4:3 by default) re-checks coverage on wider and taller screens; aspects the
plan lists under "aspects" must pass. The JSON result is printed on one line;
--report also saves it and never replaces a file. Exit 0 on pass, 1 on failure.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)


SCHEMA = "generate2dmap.parallax_validation.v2"
COVERAGE_POLICIES = ("auto", "sky-only", "all")
NEAR_ROLE_PREFIXES = ("near", "foreground")
DEFAULT_ASPECTS = ("16:9", "19.5:9", "4:3")
ASPECT_POLICIES = forge_core.ASPECT_POLICIES  # expand, fixed-height, fixed-width
PIVOT_NAMES = {"top-left": (0.0, 0.0), "center": (0.5, 0.5)}
EPSILON = 1e-7
GRID_TOLERANCE = 1e-6


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


def is_near_role(role: str) -> bool:
    """near, foreground, foreground_overlay, near_trees, ...: layers at or in front of the action."""
    return role.lower().replace("-", "_").split("_")[0] in NEAR_ROLE_PREFIXES


def pivot_fraction(camera: dict[str, Any], viewport: list[float]) -> tuple[str, tuple[float, float]]:
    """camera.pivot as a mode name and a fraction of the viewport (top-left 0,0; center 0.5,0.5)."""
    value = camera.get("pivot", "top-left")
    if isinstance(value, str):
        if value not in PIVOT_NAMES:
            raise ValueError("camera.pivot must be top-left, center or an [x, y] point in viewport pixels.")
        return value, PIVOT_NAMES[value]
    point = pair(value, "camera.pivot")
    return "point", (point[0] / viewport[0], point[1] / viewport[1])


def seam_metrics(image: Image.Image | np.ndarray, axis: str) -> dict[str, Any]:
    """Wrap-seam diagnostics for one repeat axis (x: last column -> first, y: last row -> first).

    The edge-equality numbers (alpha_mae, visible_rgb_mae, premultiplied_rgb_mae) are kept for
    readers of v1 reports. The seam fields and the verdict are forge_core.edge_seam_report of the
    layer with itself (the rows axis on the transposed image), the one seam metric of every tool that
    judges edges (D9, MAP-14): the wrap step against the art's own steps near it, as a ratio and a
    snake_case verdict (continuous, seam, duplicate_edge, flat, too_small). A duplicated edge column
    has a zero step and passes edge equality, which is why equality is not the measure. layer_p95 keeps
    forge_core.seam_report's comparison with every column (or row) step of the layer. This is a
    statistic, not a proof: seamless_verified stays false.
    """
    pixels = np.asarray(image.convert("RGBA") if isinstance(image, Image.Image) else image)
    first, last = (pixels[:, 0], pixels[:, -1]) if axis == "x" else (pixels[0], pixels[-1])
    first, last = first.astype(np.int64), last.astype(np.int64)
    count = len(first)
    joint = (first[:, 3] > 0) & (last[:, 3] > 0)
    visible = int(joint.sum())
    premultiplied = np.abs(first[:, :3] * first[:, 3:] - last[:, :3] * last[:, 3:]).sum()
    report: dict[str, Any] = {
        "axis": axis,
        "alpha_mae": float(np.abs(first[:, 3] - last[:, 3]).sum() / count),
        "visible_rgb_mae": float(np.abs(first[joint, :3] - last[joint, :3]).sum() / (visible * 3)) if visible else None,
        "jointly_visible_pixels": visible,
        "premultiplied_rgb_mae": float(premultiplied / 255 / (count * 3)),
        "seamless_verified": False,
    }
    oriented = pixels if axis == "x" else np.ascontiguousarray(np.swapaxes(pixels, 0, 1))
    report.update(forge_core.edge_seam_report(oriented, oriented))
    steps = oriented.shape[1]
    if steps >= 2:  # one column (or row) has nothing to compare: verdict too_small
        report["frames"] = steps
        layer = forge_core.seam_report(oriented[:, i:i + 1] for i in range(steps))
        report["layer_p95"] = {"adjacent_p95": layer["adjacent_p95"], "seam_over_p95": layer["seam_over_p95"],
                               "method": "forge_core.seam_report: the wrap step against every step of the layer"}
    return report


class LayerGeometry:
    """One layer's transform: screen rectangles, coverage and gaps for any viewport, pivot and camera."""

    def __init__(self, base: list[float], display: list[float], factor: list[float], repeat: list[bool],
                 bbox: tuple[int, int, int, int] | None, scale: float, identity: str) -> None:
        self.base, self.display, self.factor, self.repeat = base, display, factor, repeat
        self.bbox, self.scale, self.identity = bbox, scale, identity

    def snapshot(self, camera: tuple[float, float], zoom: float, viewport: list[float],
                 pivot: tuple[float, float]) -> dict[str, Any]:
        position = [pivot[i] + (self.base[i] - camera[i] * self.factor[i] - pivot[i]) * zoom for i in range(2)]
        rendered = [value * zoom for value in self.display]
        if not all(math.isfinite(v) for v in position + rendered):
            raise ValueError(f"{self.identity}: camera transform is not finite.")
        extent = [position[0], position[1], position[0] + rendered[0], position[1] + rendered[1]]
        if not all(math.isfinite(v) for v in extent):
            raise ValueError(f"{self.identity}: transformed canvas bounds are not finite.")
        covered = [self.repeat[i] or (position[i] <= EPSILON and extent[i + 2] >= viewport[i] - EPSILON)
                   for i in range(2)]
        visible = [self.repeat[i] or (position[i] < viewport[i] and extent[i + 2] > 0) for i in range(2)]
        gaps = [0.0 if self.repeat[0] else max(0.0, position[0]), 0.0 if self.repeat[1] else max(0.0, position[1]),
                0.0 if self.repeat[0] else max(0.0, viewport[0] - extent[2]),
                0.0 if self.repeat[1] else max(0.0, viewport[1] - extent[3])]
        transformed_bbox = None
        if self.bbox:
            transformed_bbox = [position[i % 2] + self.bbox[i] * self.scale * zoom for i in range(4)]
        return {"camera": [camera[0], camera[1]], "zoom": zoom, "screen_canvas_rect": extent,
                "canvas_coverage_axes": covered, "canvas_covers_viewport": all(covered),
                "canvas_visible": all(visible), "gaps_px": gaps, "nonzero_alpha_bbox_screen": transformed_bbox}


def _camera_extremes(ranges: dict[str, list[float]]) -> list[tuple[float, float, float]]:
    """Every (x, y, zoom) corner of the camera box; coverage is linear in camera and zoom, so corners suffice."""
    return list(itertools.product(*(sorted(set(ranges[key])) for key in ("x", "y", "zoom"))))


def _pixel_grid(geometry: LayerGeometry, zooms: list[float], pivot: tuple[float, float]) -> dict[str, Any]:
    """Whole screen pixels per source pixel, the rest position (camera 0) on the pixel grid and whether a
    one-pixel camera move scrolls the layer by a fraction of a pixel, at each zoom extreme."""
    ratios = [geometry.scale * zoom for zoom in zooms]
    integer_pixels = all(abs(r - round(r)) <= GRID_TOLERANCE and round(r) >= 1 for r in ratios)
    rest = [[pivot[i] + (geometry.base[i] - pivot[i]) * zoom for i in range(2)] for zoom in zooms]
    rest_integral = all(abs(v - round(v)) <= GRID_TOLERANCE for point in rest for v in point)
    scroll = [[geometry.factor[i] * zoom for i in range(2)] for zoom in zooms]
    subpixel = any(abs(v - round(v)) > GRID_TOLERANCE for step in scroll for v in step)
    return {"screen_px_per_source_px": ratios, "integer_pixels": integer_pixels,
            "rest_position_at_camera_0": rest, "rest_position_integral": rest_integral,
            "scroll_px_per_camera_px": scroll, "subpixel_scroll": subpixel}


LIMITS = [
    "Coverage checks the rectangular image canvas, not pixels hidden by transparency. Only a fully opaque sky "
    "can certify opaque viewport coverage.",
    "Alpha bounding boxes include empty holes; repeating layers report the seed tile bbox only.",
    "Endpoint checks cover independent bounded camera x/y and positive uniform zoom under this transform and pivot. "
    "Rotations, perspective, animation, shake, renderer rounding and culling are not simulated.",
    "The aspect sweep keeps the pivot's share of the viewport fixed (a centred camera grows both sides); other "
    "runtime stretch rules are not modelled.",
    "Repeat seams are forge_core.edge_seam_report ratios (the wrap step against the art's own steps near it, D9); "
    "they are diagnostics, not a seamlessness proof (seamless_verified stays false).",
    "The pixel grid checks scale x zoom and the rest position at the zoom extremes only; zoom values between them "
    "and the runtime's snapping are not checked.",
]


def _coverage_rule(layer: dict[str, Any], identity: str, role: str, repeat: list[bool],
                   coverage: str) -> tuple[bool, str]:
    """Whether the layer's canvas must cover the viewport, and why."""
    explicit = layer.get("require_canvas_coverage")
    if explicit is not None:
        explicit = boolean(explicit, f"{identity}.require_canvas_coverage")
    if role == "sky":
        return True, "sky"
    if explicit is not None:
        return explicit, "explicit"
    if coverage == "sky-only":
        return False, "not required (--coverage sky-only)"
    if coverage == "all":
        return True, "--coverage all"
    if any(repeat):
        return True, "repeated layer"
    if is_near_role(role):
        return True, "near or foreground role"
    return False, "not required"


def _check_layer(layer: Any, base_dir: Path, context: dict[str, Any],
                 warnings: list[str]) -> tuple[dict[str, Any], LayerGeometry]:
    """One layer's report (its own issues, unprefixed) and geometry; warnings are appended with its id."""
    if not isinstance(layer, dict):
        raise ValueError("Every layer must be an object.")
    identity = layer.get("id")
    if not isinstance(identity, str) or not identity.strip() or identity in context["ids"]:
        raise ValueError("Layer ids must be unique nonempty strings.")
    context["ids"].add(identity)
    role = layer.get("role")
    if not isinstance(role, str) or not role.strip():
        raise ValueError(f"{identity}: role must be a nonempty string.")
    sky = role == "sky"
    expected = layer.get("alpha")
    if not isinstance(expected, str) or expected not in {"opaque", "transparent"}:
        raise ValueError(f"{identity}: alpha must explicitly be opaque or transparent.")
    if any(key in layer for key in ("width", "height", "display_size", "scale_xy", "repeat_width", "repeat_height")):
        raise ValueError(f"{identity}: use one uniform scale; display size and repeat period come from the "
                         f"actual image.")
    scale = number(layer.get("scale", 1), f"{identity}.scale", positive=True)
    offset = pair(layer.get("offset", [0, 0]), f"{identity}.offset")
    anchor = pair(layer.get("anchor_px", [0, 0]), f"{identity}.anchor_px")
    factor = pair(layer.get("scroll_factor", [1, 1]), f"{identity}.scroll_factor")
    repeat = layer.get("repeat", [False, False])
    if not isinstance(repeat, list) or len(repeat) != 2:
        raise ValueError(f"{identity}: repeat must contain two booleans.")
    repeat = [boolean(v, f"{identity}.repeat") for v in repeat]
    required, rule = _coverage_rule(layer, identity, role, repeat, context["coverage"])
    allow_empty = boolean(layer.get("allow_empty", False), f"{identity}.allow_empty")
    path_value = layer.get("image")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"{identity}: image must be a path string.")
    path = Path(path_value)
    path = (base_dir / path).resolve() if not path.is_absolute() else path.resolve()
    if Path(path_value).is_absolute():  # reports keep plan-relative paths (MAP-24): never an absolute one
        path_value = forge_core.manifest_path(path, base_dir)
    image, info = forge_core.load_rgba(path)
    source_size = list(image.size)
    if any(not 0 <= anchor[i] <= source_size[i] for i in range(2)):
        raise ValueError(f"{identity}: anchor_px must lie within the source image canvas.")
    display = [value * scale for value in source_size]
    if not all(math.isfinite(v) for v in display):
        raise ValueError(f"{identity}: computed display dimensions are not finite.")
    alpha = image.getchannel("A")
    histogram = alpha.histogram()
    minimum, maximum = alpha.getextrema()
    bbox = alpha.getbbox()
    alpha_info = {"expected": expected, "min": minimum, "max": maximum,
                  "fully_clear_pixels": histogram[0], "partly_transparent_pixels": sum(histogram[1:255]),
                  "fully_opaque_pixels": histogram[255], "total_pixels": image.width * image.height,
                  "nonzero_bbox_px": list(bbox) if bbox else None, "allow_empty": allow_empty}
    issues: list[str] = []
    if expected == "opaque" and minimum != 255:
        issues.append("Declared opaque image contains transparent pixels.")
    if expected == "transparent" and minimum == 255:
        issues.append("Declared transparent image is fully opaque; RGB checkerboard is not alpha.")
    if maximum == 0 and not allow_empty:
        issues.append("Layer is entirely transparent; use allow_empty only for an intentional empty overlay.")
    if sky and (expected != "opaque" or minimum != 255):
        issues.append("Sky must declare opaque and be fully opaque.")
    if role in {"foreground", "foreground_overlay"} and (expected != "transparent" or minimum == 255):
        issues.append("Foreground overlays must declare transparent and contain actual transparency.")

    geometry = LayerGeometry([offset[i] - anchor[i] * scale for i in range(2)], display, factor, repeat, bbox,
                             scale, identity)
    snapshots = [geometry.snapshot((cx, cy), zoom, context["viewport"], context["pivot"])
                 for cx, cy, zoom in context["extremes"]]
    canvas_passed = all(sample["canvas_covers_viewport"] for sample in snapshots)
    if required and not canvas_passed:
        worst = max(snapshots, key=lambda sample: max(sample["gaps_px"]))
        issues.append(f"Image canvas does not cover the viewport at every camera/zoom extremum (coverage rule: "
                      f"{rule}; worst gap {max(worst['gaps_px']):.6g} px at camera {worst['camera']}, "
                      f"zoom {worst['zoom']:g}).")
    elif not required and not all(sample["canvas_visible"] for sample in snapshots):
        warnings.append(f"{identity}: the layer leaves the viewport entirely at some camera/zoom extreme; set "
                        f"require_canvas_coverage or check its offset if it should stay in view.")

    seams = [seam_metrics(image, axis) for axis, enabled in zip(("x", "y"), repeat) if enabled]
    for seam in seams:
        if seam["verdict"] in forge_core.EDGE_SEAM_DEFECTS:
            what = "seam" if seam["verdict"] == "seam" else "duplicated edge"
            detail = (f"Repeat seam on {seam['axis']} is a {what} ({seam['verdict']}: seam ratio "
                      f"{seam['seam_ratio']:.3g}, wrap step {seam['seam']:.3g} vs nearby steps up to "
                      f"{seam['adjacent_max']:.3g}).")
            if context["strict_seams"]:
                issues.append(detail)
            else:
                warnings.append(f"{identity}: {detail}")

    grid = _pixel_grid(geometry, context["zooms"], context["pivot"])
    if context["grid_source"]:
        if not grid["integer_pixels"]:
            issues.append(f"Pixel art needs whole screen pixels per source pixel; scale x zoom is "
                          f"{', '.join(f'{r:g}' for r in grid['screen_px_per_source_px'])}.")
        if not grid["rest_position_integral"]:
            issues.append("Pixel art needs the layer's rest position on whole screen pixels; adjust offset or "
                          "anchor_px.")
        if grid["subpixel_scroll"]:
            warnings.append(f"{identity}: scroll_factor x zoom is fractional, so the layer moves by sub-pixels; "
                            f"snap its drawn position to whole pixels each frame.")
    report = {"id": identity, "role": role, "image": path_value, "source_mode": info["source_mode"],
              "source_sha256": info["sha256"], "source_size": source_size, "scale": scale,
              "display_size": display, "offset": offset, "anchor_px": anchor, "scroll_factor": factor,
              "repeat": repeat, "repeat_period_world_px": [display[i] if repeat[i] else None for i in range(2)],
              "alpha": alpha_info, "require_canvas_coverage": required, "coverage_rule": rule,
              "canvas_coverage_passed": canvas_passed,
              "opaque_viewport_coverage_verified": sky and minimum == 255 and canvas_passed,
              "extrema": snapshots, "repeat_seams": seams, "pixel_grid": grid,
              "passed": not issues, "issues": issues}
    return report, geometry


def _aspect_sweep(sweep: dict[str, tuple[float, float]], required_aspects: list[str], policy: str,
                  context: dict[str, Any], checked: list[tuple[LayerGeometry, bool]]) -> list[dict[str, Any]]:
    """Coverage of the required layers on screens of other aspects. The pivot keeps its share of the viewport,
    so the world point under the pivot stays put (a centred camera grows both sides)."""
    viewport, share = context["viewport"], context["pivot_share"]
    results = []
    for text, aspect in sweep.items():
        swept = forge_core.aspect_viewport(viewport, aspect, policy)
        shift = [share[i] * (swept[i] - viewport[i]) for i in range(2)]
        swept_pivot = (share[0] * swept[0], share[1] * swept[1])
        failing = []
        for geometry, required in checked:
            if not required:
                continue
            samples = [geometry.snapshot((cx - shift[0], cy - shift[1]), zoom, swept, swept_pivot)
                       for cx, cy, zoom in context["extremes"]]
            if not all(sample["canvas_covers_viewport"] for sample in samples):
                failing.append({"id": geometry.identity,
                                "worst_gap_px": max(max(sample["gaps_px"]) for sample in samples)})
        results.append({"aspect": text, "ratio": aspect[0] / aspect[1], "viewport": swept,
                        "camera_shift": [-v for v in shift], "required": text in required_aspects,
                        "passed": not failing, "failing_layers": failing})
    return results


def validate_plan(plan: Any, base_dir: Path, *, coverage: str = "auto", pixel_grid: bool = False,
                  aspects: Sequence[str] = DEFAULT_ASPECTS, aspect_policy: str | None = None,
                  strict_seams: bool = False) -> dict[str, Any]:
    """Check a parallax plan against its actual images. Raises ValueError for a malformed plan; a plan that
    is well formed but fails a check returns passed false with the issues listed."""
    if coverage not in COVERAGE_POLICIES:
        raise ValueError(f"coverage must be one of {', '.join(COVERAGE_POLICIES)}.")
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
    pivot_mode, pivot_share = pivot_fraction(camera, viewport)
    policy = aspect_policy or plan.get("aspect_policy", "expand")
    if policy not in ASPECT_POLICIES:
        raise ValueError(f"aspect_policy must be one of {', '.join(ASPECT_POLICIES)}.")
    required_aspects = plan.get("aspects", [])
    if not isinstance(required_aspects, list):
        raise ValueError('aspects must be a list such as ["16:9", "19.5:9"].')
    required_aspects = [str(text) for text in required_aspects]
    sweep = {str(text): forge_core.parse_aspect(text) for text in [*aspects, *required_aspects]}
    pixel_art = plan.get("pixel_art", False)
    if type(pixel_art) is not bool:
        raise ValueError("pixel_art must be true or false.")
    if pixel_grid:
        grid_source = "--pixel-grid"
    elif pixel_art:
        grid_source = "plan pixel_art"
    else:
        grid_source = "plan sampling nearest" if plan.get("sampling") == "nearest" else None
    layers = plan.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("layers must be a nonempty list.")
    context = {"viewport": viewport, "pivot_share": pivot_share,
               "pivot": (pivot_share[0] * viewport[0], pivot_share[1] * viewport[1]),
               "extremes": _camera_extremes(ranges), "zooms": sorted(set(ranges["zoom"])), "coverage": coverage,
               "grid_source": grid_source, "strict_seams": strict_seams, "ids": set()}
    issues: list[str] = []
    warnings: list[str] = []
    reports, checked = [], []
    for layer in layers:
        report, geometry = _check_layer(layer, base_dir, context, warnings)
        issues.extend(f"{report['id']}: {message}" for message in report["issues"])
        reports.append(report)
        checked.append((geometry, report["require_canvas_coverage"]))
    if sum(report["role"] == "sky" for report in reports) != 1:
        issues.append("Plan requires exactly one opaque sky base layer.")
    aspect_reports = _aspect_sweep(sweep, required_aspects, policy, context, checked)
    for entry in aspect_reports:
        if not entry["passed"]:
            message = (f"aspect {entry['aspect']} ({entry['viewport'][0]:g}x{entry['viewport'][1]:g} viewport, "
                       f"{policy}): {', '.join(item['id'] for item in entry['failing_layers'])} do not cover it.")
            (issues if entry["required"] else warnings).append(message)
    return {"schema": SCHEMA, "passed": not issues, "issues": issues, "warnings": warnings, "viewport": viewport,
            "camera": ranges, "pivot": {"mode": pivot_mode, "point": list(context["pivot"])},
            "coverage_policy": coverage, "camera_extrema_checked": len(context["extremes"]), "layers": reports,
            "aspect_sweep": {"policy": policy, "aspects": aspect_reports},
            "pixel_grid": {"enforced": grid_source is not None, "source": grid_source},
            "transform": "screenTopLeft=pivot+(offset-anchor_px*scale-camera*scroll_factor-pivot)*zoom; "
                         "screenSize=sourceSize*scale*zoom. camera is the world position at the viewport's top-left "
                         "at zoom 1. Repeats use an unbounded integer tile lattice.",
            "limits": list(LIMITS)}


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


def write_report(report: Path, result: dict[str, Any]) -> None:
    """Publish the report as a complete file, never replacing an existing one."""
    with tempfile.TemporaryDirectory(prefix="parallax-report-") as temporary:
        staged = Path(temporary) / "report.json"
        forge_core.write_json(staged, result)
        forge_core.publish_file_no_replace(staged, report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", type=Path, required=True, help="Parallax plan JSON; image paths are plan-relative.")
    parser.add_argument("--report", type=Path,
                        help="Also save the JSON result here (must not exist); images are never modified.")
    parser.add_argument("--coverage", choices=COVERAGE_POLICIES, default="auto",
                        help="Which layers must cover the viewport: auto (default) = sky, repeated and "
                             "near/foreground layers; sky-only = legacy rule; all = every layer. A layer's "
                             "require_canvas_coverage always wins, except for the sky.")
    parser.add_argument("--pixel-grid", action="store_true",
                        help="Enforce whole screen pixels per source pixel even if the plan lacks pixel_art.")
    parser.add_argument("--aspects", default=",".join(DEFAULT_ASPECTS),
                        help="Comma-separated screen aspects to sweep (default 16:9,19.5:9,4:3; 'none' to skip). "
                             "Failures warn unless the plan lists the aspect under aspects.")
    parser.add_argument("--aspect-policy", choices=ASPECT_POLICIES,
                        help="How a screen of another aspect changes the viewport (default: the plan's "
                             "aspect_policy, else expand).")
    parser.add_argument("--strict-seams", action="store_true",
                        help="Fail repeat seams (verdict seam or duplicate_edge of forge_core.edge_seam_report): a "
                             "wrap step sharper than the art's own steps near it, or a repeated edge column.")
    parser.add_argument("--strict", action="store_true", help="On failure, exit 1 without writing --report.")
    return parser


def _cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    aspects = ([] if args.aspects.strip().lower() == "none"
               else [text for text in args.aspects.split(",") if text.strip()])
    plan = None
    errors: list[str] = []
    try:
        plan = forge_core.read_json(args.spec)  # D28: UTF-8 with an optional BOM
        result = validate_plan(plan, args.spec.resolve().parent, coverage=args.coverage, pixel_grid=args.pixel_grid,
                               aspects=aspects, aspect_policy=args.aspect_policy, strict_seams=args.strict_seams)
    except (ValueError, OSError, Image.DecompressionBombError) as error:
        errors.append(str(error))
        result = {"schema": SCHEMA, "passed": False, "issues": [str(error)]}
    report = args.report
    if report:
        try:
            check_report_destination(report, args.spec, plan)
            if os.path.lexists(report):
                raise FileExistsError(f"refusing to replace existing report {report}; choose a new path or delete it.")
        except (ValueError, OSError) as error:
            errors.append(str(error))
            result = {"schema": SCHEMA, "passed": False, "issues": [str(error)]}
            report = None
    if report and not (args.strict and not result["passed"]):
        try:
            write_report(report, result)
        except (ValueError, OSError) as error:
            errors.append(f"cannot write report: {error}")
            report = None
    elif report:
        report = None
    for message in errors:
        print(f"error: {forge_core.ascii_text(message)}", file=sys.stderr)
    print(json.dumps({**result, "report": str(report.resolve()) if report else None}, ensure_ascii=True))
    return 0 if result["passed"] and not errors else 1


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27): a failed plan exits 1 with its result printed, usage
    errors exit 2, and no traceback reaches the user."""
    return forge_core.run_cli(_cli, argv)


if __name__ == "__main__":
    raise SystemExit(main())
