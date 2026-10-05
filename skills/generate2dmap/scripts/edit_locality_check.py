#!/usr/bin/env python3
"""Check that a plate variant (an edit of an accepted plate) changed only where it was allowed to.

A night version, a lit lantern or a broken bridge must keep the geometry the stage, collision and
motion masks were built on. Give the accepted plate (--before), the variant (--after) and the regions:

  protected   must not change: the stage's protectedRegions that apply to edits (appliesTo "edit", or no
              appliesTo), --protect-ground (the stage's ground polygons and playableBand), --protect-band
              V0:V1 rows and --protect-box U0,V0,U1,V1 boxes. Regions are UV of the before image.
  edit        where the change belongs (--edit-box, repeatable); everything outside it, past an
              --edit-margin, must not change either, and a change inside it is expected.

Sizes must match, or the resize must be recorded: --conform conform.json (conform_background.py, or any
{"scale", "src_rect", "out_size"} transform with out = (src - src_rect[0:2]) * scale) or --resize
cover|stretch to declare how the generator resized the plate. The comparison runs at the lower of the
two resolutions (the other image is Lanczos-resampled through the transform), on 3x3 box-blurred RGB
(--blur) so resampling and codec noise do not count. A pixel has changed when a channel moved more than
--pixel-threshold levels. A region fails when more than --max-changed of it changed, its mean absolute
difference exceeds --max-mae, or one connected change covers --min-component of the image. --exact
(same size only) fails on any changed byte instead.

Outputs, in a new --output-dir: locality-qa.json (QA envelope, transform, per-region numbers, change
components) and locality-diff.png (changes in red over the dimmed variant, protected regions orange,
edit regions green). Exit status 1 when a check fails; --strict then publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)
import validate_stage as vs  # noqa: E402  (the stage library beside this script)


QA_SCHEMA = "generate2dmap.edit_locality.v1"
TOOL = {"name": "edit_locality_check", "version": forge_core.FORGE_PACKAGE_VERSION}
RESIZE_MODES = ("cover", "stretch")
ASPECT_WARN = 0.01
TOP_COMPONENTS = 8
NOT_PROVEN = [
    "Locality only: the tool does not judge whether the intended edit looks right, only where pixels changed.",
    "Changes below --pixel-threshold after the blur (soft relighting, slight blur or sharpening) pass unless they "
    "raise a region's mean difference above --max-mae.",
    "A resized variant is compared through the given transform with Lanczos resampling; a generator that also "
    "shifted, rotated or warped the picture shows up as change everywhere, and a sub-pixel misregistration as "
    "change along edges.",
    "Regions are the given UV boxes and polygons; content that should have been protected but was not listed is "
    "only covered by the outside-edit check (when --edit-box is given).",
]


@dataclass(frozen=True)
class Transform:
    """How the after image maps onto the before image: out = (src - src_rect[0:2]) * scale (per axis)."""
    kind: str  # identity, conform, cover or stretch
    scale: tuple[float, float]
    src_rect: vs.Box
    out_size: tuple[int, int]

    def record(self) -> dict[str, Any]:
        return {"kind": self.kind, "scale": list(self.scale), "src_rect": list(self.src_rect),
                "out_size": list(self.out_size), "map": "out = (src - src_rect[0:2]) * scale"}


# --------------------------------------------------------------------------- arguments

def _band(text: str) -> tuple[float, float]:
    try:
        low, high = (float(part) for part in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError("use V0:V1 in 0..1, for example 0.62:1") from None
    if not (0 <= low < high <= 1):
        raise argparse.ArgumentTypeError("a band needs 0 <= V0 < V1 <= 1 (UV rows of the before image)")
    return low, high


def _uv_box(text: str) -> vs.Box:
    try:
        values = [float(part) for part in text.split(",")]
        if len(values) != 4:
            raise ValueError
        return vs._uv_box(values, "box")
    except (ValueError, vs.StageError):
        raise argparse.ArgumentTypeError("use U0,V0,U1,V1 in 0..1 with U0 < U1 and V0 < V1") from None


# --------------------------------------------------------------------------- transform

def read_conform(path: Path, before: tuple[int, int], after: tuple[int, int]) -> Transform:
    """Transform from a conform.json (generate2dmap.conform.v1) or a bare {scale, src_rect, out_size} object."""
    try:
        data = forge_core.read_json(path, strict=True)  # D28
    except ValueError as error:
        raise ValueError(f"{path.name} is not valid JSON: {error}") from None
    block = data.get("transform", data) if isinstance(data, dict) else None
    if not isinstance(block, dict) or not {"scale", "src_rect", "out_size"} <= set(block):
        raise ValueError(f"{path.name} needs a transform with scale, src_rect and out_size")
    scale = block["scale"]
    scale = [scale, scale] if not isinstance(scale, list) else scale
    try:
        sx, sy = (float(value) for value in scale)
        x0, y0, x1, y1 = (float(value) for value in block["src_rect"])
        out_w, out_h = (int(value) for value in block["out_size"])
    except (TypeError, ValueError):
        raise ValueError(f"{path.name}: scale, src_rect and out_size must be numbers") from None
    if not all(math.isfinite(v) for v in (sx, sy, x0, y0, x1, y1)) or sx <= 0 or sy <= 0 or x0 >= x1 or y0 >= y1:
        raise ValueError(f"{path.name}: scale must be positive and src_rect [x0, y0, x1, y1] ordered")
    if (out_w, out_h) != after:
        raise ValueError(f"{path.name} maps onto {out_w}x{out_h} but the after image is {after[0]}x{after[1]}")
    if x0 < -1e-6 or y0 < -1e-6 or x1 > before[0] + 1e-6 or y1 > before[1] + 1e-6:
        raise ValueError(f"{path.name}: src_rect leaves the {before[0]}x{before[1]} before image")
    if abs((x1 - x0) * sx - out_w) > 1 or abs((y1 - y0) * sy - out_h) > 1:
        raise ValueError(f"{path.name}: src_rect times scale does not give out_size")
    return Transform("conform", (sx, sy), (max(0.0, x0), max(0.0, y0), min(before[0], x1), min(before[1], y1)),
                     (out_w, out_h))


def resize_transform(mode: str, before: tuple[int, int], after: tuple[int, int]) -> Transform:
    """cover: the largest centred window of the after aspect, scaled uniformly; stretch: the whole image."""
    (bw, bh), (aw, ah) = before, after
    if mode == "stretch":
        return Transform("stretch", (aw / bw, ah / bh), (0.0, 0.0, float(bw), float(bh)), after)
    scale = max(aw / bw, ah / bh)
    crop_w, crop_h = aw / scale, ah / scale
    left, top = (bw - crop_w) / 2.0, (bh - crop_h) / 2.0
    return Transform("cover", (scale, scale), (left, top, left + crop_w, top + crop_h), after)


def choose_transform(args: argparse.Namespace, before: tuple[int, int], after: tuple[int, int]) -> Transform:
    if args.conform is not None:
        return read_conform(Path(args.conform), before, after)
    if args.resize is not None:
        return resize_transform(args.resize, before, after)
    if before != after:
        raise ValueError(f"the after image is {after[0]}x{after[1]} but the before image is {before[0]}x{before[1]}; "
                         f"record how it was resized with --conform conform.json (conform_background.py) or "
                         f"--resize cover|stretch")
    return Transform("identity", (1.0, 1.0), (0.0, 0.0, float(before[0]), float(before[1])), after)


def comparison_pair(before: Image.Image, after: Image.Image,
                    transform: Transform) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Both images on one pixel grid, the lower-resolution one's: (reference, variant, space description).

    In "after" space the before image is resampled through the transform; in "before" space the after
    image is resampled back over the whole before pixels inside src_rect. ``space["to_px"]`` maps before
    pixels to comparison pixels as (offset_x, offset_y, scale_x, scale_y).
    """
    sx, sy = transform.scale
    x0, y0, x1, y1 = transform.src_rect
    if transform.kind == "identity":
        space = {"name": "before", "size": list(before.size), "to_px": (0.0, 0.0, 1.0, 1.0)}
        return _rgb(before), _rgb(after), space
    if sx * sy <= 1.0:
        reference = before.resize(after.size, Image.Resampling.LANCZOS, box=transform.src_rect)
        space = {"name": "after", "size": list(after.size), "to_px": (x0, y0, sx, sy)}
        return _rgb(reference), _rgb(after), space
    left, top, right, bottom = math.ceil(x0), math.ceil(y0), math.floor(x1), math.floor(y1)
    variant = after.resize((right - left, bottom - top), Image.Resampling.LANCZOS,
                           box=((left - x0) * sx, (top - y0) * sy, (right - x0) * sx, (bottom - y0) * sy))
    space = {"name": "before", "size": [right - left, bottom - top], "to_px": (float(left), float(top), 1.0, 1.0)}
    return _rgb(before.crop((left, top, right, bottom))), _rgb(variant), space


def _rgb(image: Image.Image) -> np.ndarray:
    """Premultiplied RGB floats 0..255 (transparent pixels compare as black)."""
    pixels = np.asarray(image.convert("RGBA"), np.float64)
    return pixels[..., :3] * (pixels[..., 3:] / 255.0)


# --------------------------------------------------------------------------- regions

def _to_comparison(points_uv: np.ndarray, before_size: tuple[int, int], space: dict[str, Any]) -> np.ndarray:
    ox, oy, sx, sy = space["to_px"]
    px = np.asarray(points_uv, float) * np.asarray(before_size, float)
    return np.stack([(px[:, 0] - ox) * sx, (px[:, 1] - oy) * sy], axis=1)


def region_mask(kind: str, shape: Any, before_size: tuple[int, int], space: dict[str, Any]) -> np.ndarray:
    size = tuple(space["size"])
    if kind == "polygon":
        return vs._local_polygon_mask(_to_comparison(np.asarray(shape, float), before_size, space), size)
    corners = _to_comparison(np.array([[shape[0], shape[1]], [shape[2], shape[3]]], float), before_size, space)
    return vs._local_box_mask((corners[0, 0], corners[0, 1], corners[1, 0], corners[1, 1]), size)


def collect_regions(args: argparse.Namespace, stage: vs.Stage | None) -> list[dict[str, Any]]:
    """Protected regions as {id, origin, kind, shape (UV)}: stage regions, ground, bands and boxes."""
    regions = []
    if stage is not None:
        for region in stage.regions_for("edit"):
            shape = region.polygon if region.polygon is not None else region.box
            regions.append({"id": f"stage:{region.id}", "origin": "stage", "kind": region.kind, "shape": shape})
        if args.protect_ground:
            regions += [{"id": f"ground[{i}]", "origin": "stage", "kind": "polygon", "shape": polygon}
                        for i, polygon in enumerate(stage.ground)]
            if stage.band is not None:
                regions.append({"id": "playableBand", "origin": "stage", "kind": "box",
                                "shape": (0.0, stage.band[0], 1.0, stage.band[1])})
    elif args.protect_ground:
        raise ValueError("--protect-ground needs --stage")
    for low, high in args.protect_band or []:
        regions.append({"id": f"band y{low:g}-{high:g}", "origin": "cli", "kind": "box",
                        "shape": (0.0, low, 1.0, high)})
    for box in args.protect_box or []:
        regions.append({"id": "box " + ",".join(f"{v:g}" for v in box), "origin": "cli", "kind": "box", "shape": box})
    return regions


# --------------------------------------------------------------------------- measuring

def measure(mask: np.ndarray, diff: np.ndarray, changed: np.ndarray, *, min_component_px: float,
            before_size: tuple[int, int], space: dict[str, Any]) -> dict[str, Any]:
    """Pixels, changed pixels and fraction, mean absolute difference, p99 of the per-pixel peak difference
    and the largest changed components (boxes in comparison pixels and before-image UV)."""
    pixels = int(mask.sum())
    if not pixels:
        return {"pixels": 0, "changedPx": 0, "changedFraction": 0.0, "mae": 0.0, "p99": 0.0, "largestComponentPx": 0,
                "components": []}
    peak = diff.max(axis=2)
    components = forge_core.connected_components(changed & mask, min_area=1)
    ox, oy, sx, sy = space["to_px"]
    listed = [{"areaPx": item["area"], "box": list(item["bbox"]),
               "uv": [(item["bbox"][0] / sx + ox) / before_size[0], (item["bbox"][1] / sy + oy) / before_size[1],
                      (item["bbox"][2] / sx + ox) / before_size[0], (item["bbox"][3] / sy + oy) / before_size[1]]}
              for item in components[:TOP_COMPONENTS]]
    changed_px = int((changed & mask).sum())
    return {"pixels": pixels, "changedPx": changed_px, "changedFraction": changed_px / pixels,
            "mae": float(diff[mask].mean()), "p99": float(np.percentile(peak[mask], 99)),
            "largestComponentPx": components[0]["area"] if components else 0, "components": listed,
            "minComponentPx": min_component_px}


def verdict(metrics: dict[str, Any], args: argparse.Namespace, min_component_px: float) -> str:
    if not metrics["pixels"]:
        return "skipped"
    if args.exact:
        return "fail" if metrics["changedPx"] else "pass"
    over = (metrics["changedFraction"] > args.max_changed or metrics["mae"] > args.max_mae
            or metrics["largestComponentPx"] >= min_component_px)
    return "fail" if over else "pass"


def render_diff(variant: np.ndarray, peak: np.ndarray, changed: np.ndarray, threshold: float,
                outlines: list[tuple[str, np.ndarray, tuple[int, int, int], bool]], header: str) -> Image.Image:
    """Dimmed grey variant; sub-threshold differences faint orange, changed pixels red; region outlines."""
    grey = (variant @ np.array([0.2126, 0.7152, 0.0722]))[..., None] * 0.45
    canvas = np.repeat(grey, 3, axis=2)
    faint = np.clip(peak / max(threshold, 1e-6), 0.0, 1.0)[..., None] * 0.45 * (~changed)[..., None]
    canvas = canvas * (1 - faint) + np.array([255.0, 150.0, 40.0]) * faint
    strong = changed[..., None] * 0.85
    canvas = canvas * (1 - strong) + np.array([255.0, 40.0, 40.0]) * strong
    image = Image.fromarray(np.clip(np.floor(canvas + 0.5), 0, 255).astype(np.uint8)).convert("RGBA")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    line = max(1, vs.round_half_up(min(width, height) / 360))
    typeface = vs.font(max(10, vs.round_half_up(min(width, height) / 45)))
    for name, mask, rgb, failed in outlines:
        edge = mask & ~_eroded(mask, line + (2 if failed else 0))
        overlay = np.zeros((height, width, 4), np.uint8)
        overlay[edge] = (*rgb, 255)
        image.alpha_composite(Image.fromarray(overlay))
        ys, xs = np.nonzero(mask)
        if len(xs):
            vs.label(draw, (int(xs.min()) + 3 * line, int(ys.min()) + 2 * line), name, (*rgb, 255), typeface)
    draw.rectangle((0, 0, width, 6 * line + vs.text_height(typeface)), fill=(4, 8, 16, 200))
    vs.label(draw, (3 * line, 2 * line), header, (255, 255, 255, 255), typeface)
    return image


def _eroded(mask: np.ndarray, radius: int) -> np.ndarray:
    """Chebyshev erosion: pixels whose whole (2r+1)^2 neighbourhood is in the mask. Pixels beyond the image
    count as inside, so a region running off the image gets no outline along the border."""
    return ~forge_core.dilate_square(~mask, radius)


# --------------------------------------------------------------------------- CLI

def run(args: argparse.Namespace) -> dict[str, Any]:
    final = Path(args.output_dir)
    vs.refuse_existing(final)
    for name, value, low, high in (("--pixel-threshold", args.pixel_threshold, 0, 255),
                                   ("--max-changed", args.max_changed, 0, 1), ("--max-mae", args.max_mae, 0, 255),
                                   ("--min-component", args.min_component, 0, 1),
                                   ("--edit-margin", args.edit_margin, 0, 0.5)):
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} must be in {low:g}..{high:g}")
    if args.blur < 0:
        raise ValueError("--blur must be 0 or more pixels")
    if args.conform is not None and args.resize is not None:
        raise ValueError("give --conform or --resize, not both")
    before, before_info = forge_core.load_rgba(args.before)
    after, after_info = forge_core.load_rgba(args.after)
    transform = choose_transform(args, before.size, after.size)
    if args.exact and transform.kind != "identity":
        raise ValueError("--exact compares bytes and needs two images of the same size")
    stage = vs.load_stage(args.stage) if args.stage is not None else None
    warnings = list(stage.notes) if stage is not None else []
    if stage is not None and tuple(stage.source_size) != before.size:
        stage_aspect = stage.source_size[0] / stage.source_size[1]
        if abs(stage_aspect / (before.width / before.height) - 1) > ASPECT_WARN:
            raise ValueError(f"the stage's sourceSize {stage.source_size[0]}x{stage.source_size[1]} has another aspect "
                             f"than the {before.width}x{before.height} before image; its UV regions would land "
                             f"elsewhere")
        warnings.append(f"the before image is {before.width}x{before.height}, the stage's sourceSize "
                        f"{stage.source_size[0]}x{stage.source_size[1]}; UV regions are scaled to the image")
    regions = collect_regions(args, stage)
    if not regions and not args.edit_box:
        raise ValueError("nothing to check: give --stage (protected regions), --protect-ground, --protect-band, "
                         "--protect-box or --edit-box")

    reference, variant, space = comparison_pair(before, after, transform)
    blur = 0 if args.exact else args.blur
    if blur:
        reference = np.stack([vs._local_box_mean(reference[..., c], blur) for c in range(3)], axis=-1)
        variant = np.stack([vs._local_box_mean(variant[..., c], blur) for c in range(3)], axis=-1)
    diff = np.abs(reference - variant)
    peak = diff.max(axis=2)
    threshold = 0.0 if args.exact else args.pixel_threshold
    changed = peak > threshold
    size = tuple(space["size"])
    min_component_px = max(1.0, args.min_component * size[0] * size[1])
    kw = {"min_component_px": min_component_px, "before_size": before.size, "space": space}

    checks = []
    aspect_change = (transform.scale[0] / transform.scale[1]) - 1.0
    checks.append(vs.check("transform recorded", "warn" if abs(aspect_change) > ASPECT_WARN else "pass",
                           {**transform.record(), "aspectDistortion": aspect_change},
                           {"maxAspectDistortion": ASPECT_WARN}))
    outlines: list[tuple[str, np.ndarray, tuple[int, int, int], bool]] = []
    region_records = []
    for region in regions:
        mask = region_mask(region["kind"], region["shape"], before.size, space)
        metrics = measure(mask, diff, changed, **kw)
        status = verdict(metrics, args, min_component_px)
        checks.append(vs.check(f"protected {region['id']} unchanged", status, metrics,
                               _thresholds(args, min_component_px)))
        region_records.append({"id": region["id"], "origin": region["origin"], "kind": region["kind"],
                               "uv": [list(p) for p in region["shape"]] if region["kind"] == "polygon"
                               else list(region["shape"]), "status": status})
        outlines.append((region["id"], mask, (255, 150, 40) if status != "fail" else (255, 40, 40), status == "fail"))
    if args.edit_box:
        edit = np.zeros((size[1], size[0]), bool)
        for box in args.edit_box:
            edit |= region_mask("box", box, before.size, space)
        allowed = forge_core.dilate_square(edit, vs.round_half_up(args.edit_margin * size[0]))
        outside = measure(~allowed, diff, changed, **kw)
        status = verdict(outside, args, min_component_px)
        checks.append(vs.check("outside the edit unchanged", status, outside, _thresholds(args, min_component_px)))
        inside = measure(edit, diff, changed, **kw)
        checks.append(vs.check("edit is visible", "pass" if inside["changedPx"] else "warn",
                               {"changedPx": inside["changedPx"], "mae": inside["mae"]}, "> 0 changed pixels"))
        outlines.append(("edit", edit, (60, 220, 110), False))
    everywhere = measure(np.ones((size[1], size[0]), bool), diff, changed, **kw)
    loose = everywhere["changedPx"] and not args.edit_box
    checks.append(vs.check("changes overall", "needs-visual-review" if loose else "pass", everywhere,
                           "with no --edit-box, look at every change component"))
    failed = [item["id"] for item in checks if item["status"] == "fail"]
    if args.strict and failed:
        raise ValueError(f"strict check failed ({'; '.join(failed)}); nothing was written")

    status_text = "FAIL" if failed else "OK"
    header = (f"{status_text}  {transform.kind} {after.width}x{after.height} vs {before.width}x{before.height}  "
              f"compared at {size[0]}x{size[1]}  red: changed > {threshold:g}")
    image = render_diff(variant, peak, changed, threshold, outlines, header)
    with forge_core.staged_output(final) as stage_dir:
        forge_core.save_png(image, stage_dir / "locality-diff.png")
        inputs = [forge_core.file_ref(args.before, final, sha256=before_info["sha256"]),
                  forge_core.file_ref(args.after, final, sha256=after_info["sha256"])]
        if args.stage is not None:
            inputs.append(forge_core.file_ref(args.stage, final))
        if args.conform is not None:
            inputs.append(forge_core.file_ref(args.conform, final))
        method = (f"edit_locality_check: {space['name']}-space comparison at {size[0]}x{size[1]} "
                  f"({transform.kind} transform, Lanczos), {2 * blur + 1}x{2 * blur + 1} box blur, per-pixel peak "
                  f"channel difference > {threshold:g} counts as changed; per region: changed fraction, mean "
                  f"absolute difference and 8-connected change components.")
        report = {"schema": QA_SCHEMA,
                  **vs._local_qa_envelope(checks, method=method, not_proven=NOT_PROVEN, inputs=inputs,
                                          outputs=[forge_core.file_ref(stage_dir / "locality-diff.png", stage_dir)],
                                          tool=TOOL),
                  "transform": transform.record(),
                  "comparison": {"space": space["name"], "size": list(size), "blur": blur,
                                 "pixelThreshold": threshold, "exact": bool(args.exact)},
                  "regions": region_records,
                  "editBoxes": [list(box) for box in args.edit_box or []],
                  "warnings": warnings}
        forge_core.write_json(stage_dir / "locality-qa.json", vs.rounded(report, 6))
    return {"output_dir": str(final.resolve()), "metadata": str((final / "locality-qa.json").resolve()),
            "diff": str((final / "locality-diff.png").resolve()), "status": report["status"], "failed": failed,
            "_warnings": warnings}


def _thresholds(args: argparse.Namespace, min_component_px: float) -> dict[str, Any]:
    if args.exact:
        return {"exact": True, "changedPx": 0}
    return {"pixelThreshold": args.pixel_threshold, "maxChangedFraction": args.max_changed, "maxMae": args.max_mae,
            "minComponentPx": min_component_px}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", type=Path, required=True, help="The accepted plate.")
    parser.add_argument("--after", type=Path, required=True, help="The edited variant.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New folder for the report and diff image.")
    parser.add_argument("--stage", type=Path, help="stage.json: its protected regions that apply to edits are checked.")
    parser.add_argument("--protect-ground", action="store_true",
                        help="Also protect the stage's ground polygons and playableBand.")
    parser.add_argument("--protect-band", type=_band, action="append", metavar="V0:V1",
                        help="Protect full-width rows V0..V1 (UV of the before image; repeatable).")
    parser.add_argument("--protect-box", type=_uv_box, action="append", metavar="U0,V0,U1,V1",
                        help="Protect a UV box of the before image (repeatable).")
    parser.add_argument("--edit-box", type=_uv_box, action="append", metavar="U0,V0,U1,V1",
                        help="Where the edit belongs (repeatable); everything else must stay unchanged.")
    parser.add_argument("--edit-margin", type=float, default=0.02,
                        help="Allowed spill around the edit boxes, a fraction of the width (default 0.02).")
    parser.add_argument("--conform", type=Path,
                        help="conform.json recording how the after image was cropped and scaled.")
    parser.add_argument("--resize", choices=RESIZE_MODES,
                        help="Declare the resize instead: cover (centred crop, uniform scale) or stretch.")
    parser.add_argument("--pixel-threshold", type=float, default=24.0,
                        help="Levels a channel must move (after the blur) for a pixel to count as changed "
                             "(default 24).")
    parser.add_argument("--max-changed", type=float, default=0.002,
                        help="Largest changed fraction of a region that still passes (default 0.002).")
    parser.add_argument("--max-mae", type=float, default=3.0,
                        help="Largest mean absolute difference of a region in levels (default 3; catches global "
                             "relighting).")
    parser.add_argument("--min-component", type=float, default=0.0005,
                        help="A connected change of this fraction of the image fails its region (default 0.0005).")
    parser.add_argument("--blur", type=int, default=1, help="Box-blur radius before comparing (default 1, a 3x3 box).")
    parser.add_argument("--exact", action="store_true",
                        help="Same-size images only: any changed byte in a checked region fails.")
    parser.add_argument("--strict", action="store_true", help="Publish nothing and exit 1 when a check fails.")
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if summary["status"] == "fail" else 0


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI: exit 0 (pass or warn), 1 (a published report with status fail, D26; or an error,
    printed as one error: line, D27), 2 (usage)."""
    return forge_core.run_cli(_main, argv)


if __name__ == "__main__":
    raise SystemExit(main())
