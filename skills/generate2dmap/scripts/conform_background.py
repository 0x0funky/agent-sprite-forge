#!/usr/bin/env python3
"""Conform a background painting to a target size, and check subject margins per screen aspect.

conform: --mode cover takes the largest window of the target aspect, placed
by --focus, exactly like PIL ImageOps.fit. --mode ground-fit takes the
smallest uniform zoom that also puts the painted ground row (--ground-y,
source pixels) on the authored floor row (--floor-y, output pixels) with no
padding. conform.json records the transform:

    out = (src - src_rect[0:2]) * scale        src = out / scale + src_rect[0:2]

Subjects (--subject ID=X0,Y0,X1,Y1 in source pixels, or --subjects FILE) are
mapped through it and must stay inside the output; with --aspects they must
also stay inside the cover crops of the output at those aspects.

validate-crops: for each screen aspect (default 16:9, 19.5:9 and 4:3) the
window a cover-cropping runtime shows, placed by --focus (repeatable) and
shrunk by --zoom for a plate pan, must keep every subject box inside with
--min-margin pixels to spare. It writes crops-qa.json and crops-overlay.png
and exits 1 when a subject is cut.

Both verbs write into a new --output-dir that is published only when
complete; with --strict a failed subject check publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)


CONFORM_SCHEMA = "generate2dmap.conform.v1"
CROPS_SCHEMA = "generate2dmap.crops_qa.v1"
TOOL = {"name": "conform_background", "version": "1.0"}
MODES = ("cover", "ground-fit")
RESAMPLERS = {"lanczos": Image.Resampling.LANCZOS, "bicubic": Image.Resampling.BICUBIC,
              "box": Image.Resampling.BOX, "nearest": Image.Resampling.NEAREST}
DEFAULT_ASPECTS = "16:9,19.5:9,4:3"
WINDOW_COLORS = ((52, 152, 219), (241, 196, 15), (155, 89, 182), (26, 188, 156), (230, 126, 34), (236, 240, 241))
PASS_COLOR, FAIL_COLOR = (46, 204, 113), (231, 76, 60)
CONFORM_NOT_PROVEN = [
    "The ground row is the one given; the tool does not find the painted ground in the art.",
    "Cropping keeps the art's own perspective; a zoomed ground-fit can lose content at the edges "
    "(see dropped_source_px).",
    "Subject and crop checks measure boxes only; look at the output at gameplay scale.",
]
CROPS_NOT_PROVEN = [
    "Subject boxes are taken as given; the tool does not find subjects in the art.",
    "Windows model a runtime that cover-crops the plate at each aspect around the focus (and divides by the plate-pan "
    "zoom); letterboxing, safe-area insets, UI overlays and other stretch rules are not modelled.",
    "A box inside the window can still read badly at gameplay scale; look at the overlay.",
]


# --------------------------------------------------------------------------- parsing

def _size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX,]\s*(\d+)\s*", text)
    if not match or min(int(match.group(1)), int(match.group(2))) < 1:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT in whole pixels, for example 1280x720")
    return int(match.group(1)), int(match.group(2))


def _focus(text: str) -> tuple[float, float]:
    try:
        values = tuple(float(part) for part in text.split(","))
    except ValueError:
        values = ()
    if len(values) != 2 or not all(math.isfinite(v) and 0 <= v <= 1 for v in values):
        raise argparse.ArgumentTypeError("use FX,FY with both in 0..1, for example 0.5,0.4")
    return values


def _local_parse_aspect(text: Any) -> tuple[float, float]:
    """'16:9', '19.5:9', '16/9' or a plain ratio such as 1.7778, as (width share, height share)."""
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*(?:[:/]\s*([0-9]*\.?[0-9]+))?\s*", str(text))
    if not match:
        raise ValueError(f"aspect {text!r} must look like 16:9 or 1.7778.")
    width, height = float(match.group(1)), float(match.group(2) or 1)
    if width <= 0 or height <= 0:
        raise ValueError(f"aspect {text!r} must be a positive ratio.")
    return width, height


def parse_aspects(text: str) -> list[tuple[str, tuple[float, float]]]:
    if not text or text.strip().lower() == "none":
        return []
    return [(part.strip(), _local_parse_aspect(part)) for part in text.split(",") if part.strip()]


def _box(values: Any, name: str) -> tuple[float, float, float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError(f"{name} must be [x0, y0, x1, y1].")
    box = tuple(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else math.nan for v in values)
    if not all(math.isfinite(v) for v in box) or box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError(f"{name} must be four finite numbers with x0 < x1 and y0 < y1.")
    return box


def parse_subjects(pairs: Sequence[str] | None, path: Path | None) -> list[dict[str, Any]]:
    """Subjects from --subject ID=X0,Y0,X1,Y1 and a JSON file of [{id, box}] (or {"subjects": [...]})."""
    subjects = []
    for text in pairs or []:
        ident, separator, numbers = text.partition("=")
        if not separator or not ident.strip():
            raise ValueError(f"--subject {text!r} must look like crest=600,40,680,120.")
        try:
            values = [float(part) for part in numbers.split(",")]
        except ValueError as error:
            raise ValueError(f"--subject {text!r} must look like crest=600,40,680,120.") from error
        subjects.append({"id": ident.strip(), "box": _box(values, f"subject {ident.strip()}")})
    if path is not None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = data.get("subjects") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ValueError(f"{path} must be a list of {{id, box}} or an object with a subjects list.")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                raise ValueError(f"{path} subject {index} needs an id and a box.")
            subjects.append({"id": entry["id"], "box": _box(entry.get("box"), f"subject {entry['id']}")})
    seen = set()
    for subject in subjects:
        if subject["id"] in seen:
            raise ValueError(f"subject id {subject['id']!r} is used twice.")
        seen.add(subject["id"])
    return subjects


# --------------------------------------------------------------------------- transforms and windows

@dataclass(frozen=True)
class Transform:
    """Uniform scale of a source window onto the output: out = (src - src_rect[0:2]) * scale."""
    scale: float
    src_rect: tuple[float, float, float, float]
    out_size: tuple[int, int]

    def to_out(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.src_rect[0]) * self.scale, (y - self.src_rect[1]) * self.scale

    def to_src(self, x: float, y: float) -> tuple[float, float]:
        return x / self.scale + self.src_rect[0], y / self.scale + self.src_rect[1]

    def box_to_out(self, box: Sequence[float]) -> tuple[float, float, float, float]:
        return (*self.to_out(box[0], box[1]), *self.to_out(box[2], box[3]))


def cover_transform(src_size: tuple[int, int], out_size: tuple[int, int],
                    focus: tuple[float, float] = (0.5, 0.5)) -> Transform:
    """The largest window of the output aspect, placed by ``focus``; the same arithmetic as ImageOps.fit, so
    resizing that window gives ImageOps.fit's pixels."""
    live_w, live_h = float(src_size[0]), float(src_size[1])
    live_ratio, out_ratio = live_w / live_h, out_size[0] / out_size[1]
    if live_ratio == out_ratio:
        crop_w, crop_h = live_w, live_h
    elif live_ratio >= out_ratio:
        crop_w, crop_h = out_ratio * live_h, live_h
    else:
        crop_w, crop_h = live_w, live_w / out_ratio
    left, top = (live_w - crop_w) * focus[0], (live_h - crop_h) * focus[1]
    return Transform(out_size[0] / crop_w, (left, top, left + crop_w, top + crop_h), tuple(out_size))


def ground_fit_transform(src_size: tuple[int, int], out_size: tuple[int, int], ground_y: float, floor_y: float,
                         focus_x: float = 0.5) -> Transform:
    """The smallest uniform scale that covers the output and maps source row ``ground_y`` onto output row
    ``floor_y`` without padding (report v2 5.5)."""
    (sw, sh), (ow, oh) = src_size, out_size
    if not 0 < ground_y < sh:
        raise ValueError(f"--ground-y must lie inside the source height (0..{sh}); got {ground_y:g}.")
    if not 0 < floor_y < oh:
        raise ValueError(f"--floor-y must lie inside the output height (0..{oh}); got {floor_y:g}.")
    scale = max(ow / sw, oh / sh, floor_y / ground_y, (oh - floor_y) / (sh - ground_y))
    crop_w, crop_h = ow / scale, oh / scale
    top = ground_y - floor_y / scale
    left = (sw - crop_w) * focus_x
    if top < -1e-9 or top + crop_h > sh + 1e-9 or crop_w > sw + 1e-9:
        raise ValueError("ground-fit needs padding for this ground row; choose another --floor-y.")
    return Transform(scale, (left, top, left + crop_w, top + crop_h), tuple(out_size))


def _local_cover_window(size: tuple[int, int], aspect: tuple[float, float], zoom: float = 1.0,
                focus: tuple[float, float] = (0.5, 0.5)) -> tuple[float, float, float, float]:
    """The part of a ``size`` plate a runtime shows on an ``aspect`` screen when it cover-crops around
    ``focus``, divided by a plate-pan ``zoom`` (game-opus55 drawCoverR)."""
    width, height = size
    share_w, share_h = aspect
    if share_w * height >= width * share_h:
        window_w, window_h = float(width), width * share_h / share_w
    else:
        window_w, window_h = height * share_w / share_h, float(height)
    window_w, window_h = window_w / zoom, window_h / zoom
    left, top = (width - window_w) * focus[0], (height - window_h) * focus[1]
    return left, top, left + window_w, top + window_h


def margins(box: Sequence[float], window: Sequence[float]) -> dict[str, Any]:
    """Distances from the box to each window edge (negative = cut) and the visible share of the box."""
    left, top = box[0] - window[0], box[1] - window[1]
    right, bottom = window[2] - box[2], window[3] - box[3]
    inter_w = max(0.0, min(box[2], window[2]) - max(box[0], window[0]))
    inter_h = max(0.0, min(box[3], window[3]) - max(box[1], window[1]))
    area = (box[2] - box[0]) * (box[3] - box[1])
    return {"left": left, "top": top, "right": right, "bottom": bottom, "min": min(left, top, right, bottom),
            "visible_fraction": inter_w * inter_h / area}


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6) + 0.0
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(item) for item in value]
    return value


def window_checks(size: tuple[int, int], subjects: list[dict[str, Any]], aspects: list[tuple[str, tuple]],
                  focuses: Sequence[tuple[float, float]], zoom: float, min_margin: float) -> list[dict[str, Any]]:
    """One QA check per (aspect, focus) window: every subject inside with ``min_margin`` to spare."""
    checks = []
    for name, aspect in aspects:
        for focus in focuses:
            window = _local_cover_window(size, aspect, zoom, focus)
            measured = {subject["id"]: margins(subject["box"], window) for subject in subjects}
            failed = sorted(ident for ident, value in measured.items() if value["min"] < min_margin)
            checks.append({"id": f"crop {name} focus {focus[0]:g},{focus[1]:g}", "status": "fail" if failed else "pass",
                           "value": _round({"aspect": name, "focus": list(focus), "zoom": zoom, "window": list(window),
                                            "subjects": measured, "cut": failed}),
                           "threshold": {"min_margin_px": min_margin}})
    return checks


def _envelope(checks: list[dict[str, Any]], method: str, not_proven: list[str], inputs: list[dict[str, Any]],
              outputs: list[dict[str, Any]]) -> dict[str, Any]:
    status = "fail" if any(check["status"] == "fail" for check in checks) else "pass"
    return {"status": status, "method": method, "notProven": list(not_proven), "checks": checks,
            "inputs": inputs, "outputs": outputs, "tool": dict(TOOL)}


def _local_file_ref(path: Path, base_dir: Path, sha256: str | None = None) -> dict[str, Any]:
    """Manifest-relative POSIX path, or only the file name when there is no relative route (another drive)."""
    relative = forge_core.portable_path(path, base_dir)
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        relative = Path(path).name
    return {"path": relative, "sha256": sha256 or forge_core.sha256_file(path), "bytes": Path(path).stat().st_size}


# --------------------------------------------------------------------------- overlay

def render_crops_overlay(image: Image.Image, checks: list[dict[str, Any]],
                         subjects: list[dict[str, Any]]) -> Image.Image:
    """The plate with every crop window outlined and labelled, and each subject green (kept) or red (cut)."""
    overlay = image.convert("RGBA").copy()
    layer = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    line = max(1, round(min(overlay.size) / 300))
    try:
        font = ImageFont.load_default(size=max(10, round(min(overlay.size) / 45)))
    except (TypeError, OSError, ImportError):
        font = ImageFont.load_default()
    text_kwargs = {"font": font}
    if isinstance(font, ImageFont.FreeTypeFont):
        text_kwargs.update(stroke_width=max(1, line // 2 + 1), stroke_fill=(0, 0, 0, 255))
    cut = {ident for check in checks for ident in check["value"]["cut"]}
    for index, check in enumerate(checks):
        color = WINDOW_COLORS[index % len(WINDOW_COLORS)]
        window = check["value"]["window"]
        draw.rectangle(window, outline=(*color, 255), width=line)
        draw.text((window[0] + 3 * line, window[1] + 3 * line + index * (line + 14)),
                  forge_core.ascii_text(check["id"]), fill=(*color, 255), **text_kwargs)
    for subject in subjects:
        box = subject["box"]
        color = FAIL_COLOR if subject["id"] in cut else PASS_COLOR
        draw.rectangle(box, outline=(*color, 255), fill=(*color, 60), width=line)
        draw.text((box[0] + line, box[1] + line), forge_core.ascii_text(subject["id"]), fill=(*color, 255),
                  **text_kwargs)
    overlay.alpha_composite(layer)
    return overlay


# --------------------------------------------------------------------------- verbs

def cmd_conform(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode == "ground-fit" and (args.ground_y is None or args.floor_y is None):
        raise ValueError("ground-fit needs --ground-y (painted ground row, source px) and --floor-y (output row).")
    if args.mode == "cover" and (args.ground_y is not None or args.floor_y is not None):
        raise ValueError("--ground-y and --floor-y belong to --mode ground-fit.")
    if not math.isfinite(args.min_margin) or args.min_margin < 0:
        raise ValueError("--min-margin must be zero or more pixels.")
    subjects = parse_subjects(args.subject, args.subjects)
    aspects = parse_aspects(args.aspects)
    if aspects and not subjects:
        raise ValueError("--aspects checks subjects; give --subject or --subjects.")
    source, info = forge_core.load_rgba(args.input)
    out_size = args.size
    if args.mode == "cover":
        transform = cover_transform(source.size, out_size, args.focus)
    else:
        transform = ground_fit_transform(source.size, out_size, args.ground_y, args.floor_y, args.focus[0])
    cover = cover_transform(source.size, out_size, args.focus)
    for subject in subjects:
        box = subject["box"]
        if box[0] < 0 or box[1] < 0 or box[2] > source.width or box[3] > source.height:
            raise ValueError(f"subject {subject['id']} lies outside the {source.width}x{source.height} source.")
    background = source.resize(out_size, RESAMPLERS[args.resampler], box=transform.src_rect)
    mapped = [{"id": subject["id"], "box": transform.box_to_out(subject["box"]), "source_box": subject["box"]}
              for subject in subjects]
    checks = []
    if subjects:
        measured = {subject["id"]: margins(subject["box"], (0, 0, *out_size)) for subject in mapped}
        failed = sorted(ident for ident, value in measured.items() if value["min"] < args.min_margin)
        checks.append({"id": "subjects in output", "status": "fail" if failed else "pass",
                       "value": _round({"subjects": measured, "cut": failed}),
                       "threshold": {"min_margin_px": args.min_margin}})
        checks += window_checks(out_size, mapped, aspects, [args.crop_focus], 1.0, args.min_margin)
    if args.mode == "ground-fit":
        landed = transform.to_out(0, args.ground_y)[1]
        checks.append({"id": "ground row on floor", "status": "pass" if abs(landed - args.floor_y) <= 1e-6 else "fail",
                       "value": _round(landed), "threshold": args.floor_y})
    warnings = []
    if source.getchannel("A").getextrema()[0] < 255:
        warnings.append("the source has transparent pixels; a background is usually opaque.")
    if args.strict and any(check["status"] == "fail" for check in checks):
        failed = [check["id"] for check in checks if check["status"] == "fail"]
        raise ValueError(f"strict check failed ({', '.join(failed)}); nothing was written.")

    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        forge_core.save_png(background, stage / "background.png")
        output_ref = _local_file_ref(stage / "background.png", stage)
        source_ref = _local_file_ref(args.input, final, info["sha256"])
        record = {
            "schema": CONFORM_SCHEMA, "tool": dict(TOOL), "mode": args.mode,
            "source": {**source_ref, "size": list(source.size), "source_mode": info["source_mode"]},
            "output": {**output_ref, "size": list(out_size)},
            "transform": {
                "scale": transform.scale, "src_rect": list(transform.src_rect), "out_size": list(out_size),
                "resampler": args.resampler, "focus": list(args.focus),
                "map": "out = (src - src_rect[0:2]) * scale", "inverse": "src = out / scale + src_rect[0:2]",
                "zoom_vs_cover": transform.scale / cover.scale,
                "dropped_source_px": {"left": transform.src_rect[0], "top": transform.src_rect[1],
                                      "right": source.width - transform.src_rect[2],
                                      "bottom": source.height - transform.src_rect[3]},
                **({"ground": {"source_y": args.ground_y, "output_y": args.floor_y}} if args.mode == "ground-fit"
                   else {}),
            },
            "subjects": _round([{"id": item["id"], "source_box": list(item["source_box"]),
                                 "output_box": list(item["box"])} for item in mapped]),
            "warnings": warnings,
            "qa": _envelope(checks, "conform_background: transform arithmetic (ImageOps.fit crop for cover), subject "
                                    "boxes mapped through it and measured against the output and its cover crops.",
                            CONFORM_NOT_PROVEN, [source_ref], [output_ref]),
        }
        forge_core.write_json(stage / "conform.json", record)
    return {"output_dir": str(final.resolve()), "background": str((final / "background.png").resolve()),
            "metadata": str((final / "conform.json").resolve()), "mode": args.mode,
            "scale": round(transform.scale, 9), "qa_status": record["qa"]["status"], "_warnings": warnings}


def cmd_validate_crops(args: argparse.Namespace) -> dict[str, Any]:
    if not math.isfinite(args.zoom) or args.zoom < 1:
        raise ValueError("--zoom must be at least 1 (the window is the cover crop divided by the zoom).")
    if not math.isfinite(args.min_margin) or args.min_margin < 0:
        raise ValueError("--min-margin must be zero or more pixels.")
    subjects = parse_subjects(args.subject, args.subjects)
    if not subjects:
        raise ValueError("validate-crops needs at least one --subject or --subjects entry.")
    aspects = parse_aspects(args.aspects)
    if not aspects:
        raise ValueError("validate-crops needs at least one aspect.")
    plate, info = forge_core.load_rgba(args.input)
    for subject in subjects:
        box = subject["box"]
        if box[0] < 0 or box[1] < 0 or box[2] > plate.width or box[3] > plate.height:
            raise ValueError(f"subject {subject['id']} lies outside the {plate.width}x{plate.height} plate.")
    focuses = args.focus or [(0.5, 0.5)]
    checks = window_checks(plate.size, subjects, aspects, focuses, args.zoom, args.min_margin)
    failed = [check["id"] for check in checks if check["status"] == "fail"]
    if args.strict and failed:
        raise ValueError(f"strict check failed ({', '.join(failed)}); nothing was written.")
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        forge_core.save_png(render_crops_overlay(plate, checks, subjects), stage / "crops-overlay.png")
        report = _envelope(checks, "validate-crops: cover-crop window per aspect and focus (divided by --zoom), "
                                   "subject box margins to each window edge in plate pixels.", CROPS_NOT_PROVEN,
                           [_local_file_ref(args.input, final, info["sha256"])],
                           [_local_file_ref(stage / "crops-overlay.png", stage)])
        report = {"schema": CROPS_SCHEMA, **report, "plate_size": list(plate.size),
                  "subjects": _round([{"id": subject["id"], "box": list(subject["box"])} for subject in subjects])}
        forge_core.write_json(stage / "crops-qa.json", report)
    return {"output_dir": str(final.resolve()), "report": str((final / "crops-qa.json").resolve()),
            "overlay": str((final / "crops-overlay.png").resolve()), "status": report["status"], "cut": failed,
            "_warnings": []}


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)

    def subjects_options(verb: argparse.ArgumentParser, unit: str) -> None:
        verb.add_argument("--subject", action="append", metavar="ID=X0,Y0,X1,Y1",
                          help=f"A subject box that must stay visible, in {unit} pixels (repeatable).")
        verb.add_argument("--subjects", type=Path, help="JSON list of {id, box} subject boxes.")
        verb.add_argument("--min-margin", type=float, default=0.0,
                          help="Pixels a subject must keep from every window edge (default 0).")
        verb.add_argument("--strict", action="store_true", help="Exit 1 and publish nothing when a subject is cut.")
        verb.add_argument("--output-dir", type=Path, required=True, help="New folder for the outputs; must not exist.")

    conform = verbs.add_parser("conform", help="Cover or ground-fit a background to a target size.",
                               description="Cover or ground-fit a background to a target size; writes "
                                           "background.png and conform.json with the transform.")
    conform.add_argument("--input", type=Path, required=True, help="Background painting.")
    conform.add_argument("--size", type=_size, required=True, metavar="WxH", help="Output size, e.g. 1280x720.")
    conform.add_argument("--mode", choices=MODES, default="cover", help="cover (default) or ground-fit.")
    conform.add_argument("--focus", type=_focus, default=(0.5, 0.5), metavar="FX,FY",
                         help="Where the window sits in the source, 0..1 per axis (default 0.5,0.5); "
                              "ground-fit uses FX only.")
    conform.add_argument("--ground-y", type=float, help="ground-fit: painted ground row in source pixels.")
    conform.add_argument("--floor-y", type=float, help="ground-fit: floor row it must land on, in output pixels.")
    conform.add_argument("--resampler", choices=tuple(RESAMPLERS), default="lanczos",
                         help="Resampling filter (default lanczos).")
    conform.add_argument("--aspects", default="",
                         help="Also check the subjects on cover crops of the output at these aspects, "
                              "e.g. 16:9,19.5:9,4:3 (default: none).")
    conform.add_argument("--crop-focus", type=_focus, default=(0.5, 0.5), metavar="FX,FY",
                         help="Focus of those crops in the output (default 0.5,0.5).")
    subjects_options(conform, "source")

    crops = verbs.add_parser("validate-crops", help="Check subject margins in each aspect's cover crop of a plate.",
                             description="Check that subject boxes keep their margins inside the cover crop each "
                                         "screen aspect shows; writes crops-qa.json and crops-overlay.png.")
    crops.add_argument("--input", type=Path, required=True, help="The plate as the game loads it.")
    crops.add_argument("--aspects", default=DEFAULT_ASPECTS,
                       help=f"Comma-separated screen aspects (default {DEFAULT_ASPECTS}).")
    crops.add_argument("--focus", type=_focus, action="append", metavar="FX,FY",
                       help="Crop focus 0..1 per axis (repeatable, e.g. 0,0.4 and 1,0.4 for both pan ends; "
                            "default 0.5,0.5).")
    crops.add_argument("--zoom", type=float, default=1.0,
                       help="Plate-pan zoom: the window is the cover crop divided by it (default 1).")
    subjects_options(crops, "plate")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        summary = cmd_conform(args) if args.verb == "conform" else cmd_validate_crops(args)
    except (ValueError, OSError, Image.DecompressionBombError) as error:
        print(f"error: {forge_core.ascii_text(str(error) or type(error).__name__)}", file=sys.stderr)
        return 1
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if args.verb == "validate-crops" and summary["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
