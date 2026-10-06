"""Pixel-art QA for PNG frames: metrics, palette and outline gates, grid detection, review sheet.

Run from the project root; the report and review sheet are new files inside the project:
  python "<skill-dir>/scripts/pixel_qa.py" --input "out/slime-v1/green/frames/*.png" --palette slime.pixelspec.json --variant green --strict
  python "<skill-dir>/scripts/pixel_qa.py" --input "out/slime-v1/green/frames/*.png" --palette slime.pixelspec.json --variant green --outline "#1a1c2c" --detect-grid --anchor 16,31 --review review.png --scales 1,2,4 --bg light,dark,checker --onion --strict --report qa.json

Inputs are PNG paths or quoted glob patterns (expanded here in natural order, so frame-2
comes before frame-10); any PNG mode loads as 8-bit RGBA. --palette takes a palette file
(.json, .hex, .gpl), a PixelSpec or a codeart-meta.json; --variant picks one of its
variants. Checks, each valued at the worst frame:
  partial_alpha   pixels with 0 < alpha < 255                  fail above 0
  off_palette     visible pixels whose RGB is not in the palette fail above 0 (needs --palette)
  outline_gaps    non-outline pixels touching transparency     fail above 0 (needs --outline)
  l_corners       outline pixels removable for a 1 px line     fail above 10 (needs --outline)
  anchor          --anchor inside every canvas                 fail when outside
  same_size       every frame has the same canvas              warn otherwise
  grid            --detect-grid: integer pixel period, uniform blocks; warn when absent
                  (native 1x art has none; upscaled pixel art should)
  loop_seam       --onion with 3+ frames (not --once): last->first change over the median
                  step; warn above 2.0 (the loop pops)
--report writes a common QA envelope (method, notProven, checks, hashed inputs and
outputs) plus per-frame metrics. --review writes a contact sheet: every frame at each
--scales factor (integer nearest only) on each --bg, an --onion row (previous frame red,
next blue, --anchor magenta), palette swatches and the per-frame metrics.
A failed check exits 1: without --strict the report and review sheet are still written (so
the failure can be inspected), with --strict neither file is written. Warnings never fail.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Callable, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    import numpy as np
    import codeart_core
    import forge_core
except ImportError as _import_error:  # a clean machine: main() prints the pip command instead of a traceback
    _MISSING: ImportError | None = _import_error
else:
    _MISSING = None

TOOL_NAME = "pixel_qa.py"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION if _MISSING is None else "0.4.0"  # D29: the package version
SKILL_DIR = SCRIPTS_DIR.parent
META_SCHEMA = "codeart2d.codeart_meta.v1"
LOOP_SEAM_LIMIT = 2.0
MAX_IMAGE_PIXELS = 1 << 26  # 64 Mpx (256 MB as RGBA): the largest review sheet drawn
QA_METHOD = ("pixel_qa.py: every input PNG is loaded as 8-bit RGBA and measured with codeart_core.qa_pixels "
             "(alpha census, exact palette lookup, 4-neighbour outline gaps and L-corners against "
             "codeart_core.QA_PIXEL_GATES); optional codeart_core.detect_grid, an anchor bounds check and "
             "forge_core.seam_report on the frame loop; each check is valued at the worst frame")
QA_NOT_PROVEN = (
    "Readability, silhouette and appeal at game scale; look at the review sheet.",
    "Motion quality, timing and spacing; the onion row and loop seam only flag symptoms.",
    "That the frames share a registered root; the anchor is only checked to lie inside each canvas.",
)


class QAFailure(Exception):
    """--strict and a failed check; nothing is written."""


def _natural_key(path: Path) -> list:
    text = path.as_posix()
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)] + [text]


def expand_inputs(items: Sequence[str]) -> list[Path]:
    """Paths and glob patterns (natural order within a pattern), each file once."""
    found: dict[Path, Path] = {}
    for item in items:
        if re.search(r"[*?[]", item):
            matches = sorted((Path(match) for match in glob.glob(item, recursive=True) if Path(match).is_file()),
                             key=_natural_key)
            if not matches:
                raise codeart_core.CodeArtError(f"no files match {item}")
        else:
            matches = [Path(item)]
            if not matches[0].is_file():
                raise codeart_core.CodeArtError(f"input not found: {item}")
        for path in matches:
            found.setdefault(path.resolve(), path)
    return list(found.values())


def _pair(text: str | None, label: str) -> tuple[float, float] | None:
    if text is None:
        return None
    parts = [part for part in re.split(r"[\s,]+", text.strip()) if part]
    try:
        x, y = (float(part) for part in parts)
    except ValueError:
        raise codeart_core.CodeArtError(f"{label} must be two numbers x,y, got {text!r}") from None
    if not all(np.isfinite([x, y])):
        raise codeart_core.CodeArtError(f"{label} must be finite")
    return x, y


def _int_list(text: str, label: str) -> list[int]:
    try:
        values = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise codeart_core.CodeArtError(f"{label} must be comma-separated integers, got {text!r}") from None
    if not values or any(value < 1 for value in values):
        raise codeart_core.CodeArtError(f"{label} needs positive integers")
    return values


def load_palette(path: Path) -> Any:
    """A codeart_core.Palette from a palette file (.json, .hex, .gpl), a PixelSpec or a codeart-meta.json."""
    if not path.is_file():
        raise codeart_core.CodeArtError(f"palette not found: {path}")
    if path.suffix.lower() != ".json":
        return codeart_core.parse_palette(path)
    try:
        data = forge_core.read_json(path)  # UTF-8 with an optional BOM (D28)
    except (OSError, ValueError) as error:
        raise codeart_core.CodeArtError(f"cannot read palette {path.name}: {error}") from None
    return codeart_core.parse_palette(data["palette"] if isinstance(data, dict) and data.get("schema") == META_SCHEMA
                                      else data)


def outline_colours(text: str | None, palette: Any, variant: str | None) -> list[tuple] | None:
    """--outline: comma-separated #hex colours or palette colour names (resolved in the checked variant)."""
    if not text:
        return None
    names = palette.resolve(variant) if palette is not None else {}
    colours = []
    for token in (part.strip() for part in text.split(",") if part.strip()):
        if token in names:
            colours.append(names[token])
        else:
            try:
                colours.append(codeart_core.hex_to_rgba(token))
            except codeart_core.CodeArtError:
                raise codeart_core.CodeArtError(f"--outline {token!r} is neither a #hex colour nor a palette "
                                                "colour name") from None
    return colours


def _file_ref(path: Path, base: Path) -> dict:
    """fileRef relative to `base` (POSIX); a file on another drive is recorded by its name (forge_core.file_ref)."""
    return forge_core.file_ref(path, base)


def _status(checks: list[dict]) -> str:
    order = {"pass": 0, "needs-visual-review": 1, "warn": 2, "fail": 3}
    measured = [check["status"] for check in checks if check["status"] != "skipped"]
    return max(measured, key=order.__getitem__) if measured else "pass"


def gate_checks(frames: list[dict]) -> list[dict]:
    """One check per codeart_core.QA_PIXEL_GATES entry, valued at the worst frame."""
    checks = []
    for name, limit in codeart_core.QA_PIXEL_GATES:
        values = [(frame["metrics"][name], frame["label"]) for frame in frames if frame["metrics"][name] is not None]
        if not values:
            checks.append({"id": name, "status": "skipped", "value": None, "threshold": limit})
            continue
        worst = max(value for value, _ in values)
        check = {"id": name, "status": "pass" if worst <= limit else "fail", "value": worst, "threshold": limit}
        failing = [label for value, label in values if value > limit]
        if failing:
            check["failing"] = failing
        checks.append(check)
    return checks


def analyse(paths: list[Path], args: argparse.Namespace) -> tuple[list[dict], list[dict], list[np.ndarray], Any]:
    """Load and measure every frame; returns (frames, checks, pixels, palette colours or None)."""
    palette = colours = None
    if args.palette:
        palette = load_palette(Path(args.palette))
        colours = list(palette.resolve(args.variant).values())
    elif args.variant:
        raise codeart_core.CodeArtError("--variant needs --palette")
    outline = outline_colours(args.outline, palette, args.variant)
    anchor = _pair(args.anchor, "--anchor")
    frames, pixels = [], []
    for path in paths:
        try:
            image, info = forge_core.load_rgba(path)
        except (OSError, ValueError) as error:
            raise codeart_core.CodeArtError(f"cannot read {path}: {error}") from None
        rgba = np.asarray(image).copy()
        metrics = codeart_core.qa_pixels(rgba, colours, outline)
        record = {"path": path, "label": path.name, "size": [rgba.shape[1], rgba.shape[0]],
                  "source_mode": info["source_mode"], "metrics": metrics}
        if anchor is not None and metrics["bbox"] is not None:
            record["ground_gap_px"] = round(anchor[1] - metrics["bbox"][3], 3)
        if args.detect_grid:
            record["grid"] = codeart_core.detect_grid(rgba)
        frames.append(record)
        pixels.append(rgba)
    checks = gate_checks(frames)
    if anchor is not None:
        outside = [frame["label"] for frame in frames
                   if not (0 <= anchor[0] <= frame["size"][0] and 0 <= anchor[1] <= frame["size"][1])]
        checks.append({"id": "anchor", "status": "fail" if outside else "pass", "value": list(anchor),
                       "threshold": "inside [0, width] x [0, height]", **({"failing": outside} if outside else {})})
    sizes = sorted({tuple(frame["size"]) for frame in frames})
    checks.append({"id": "same_size", "status": "pass" if len(sizes) == 1 else "warn",
                   "value": [list(size) for size in sizes], "threshold": "one canvas size"})
    if args.detect_grid:
        grids = [frame["grid"] for frame in frames]
        periods = sorted({grid["period"] for grid in grids})
        uniform = all(grid["has_grid"] and grid["uniformity"] == 1.0 for grid in grids)
        checks.append({"id": "grid", "status": "pass" if uniform and len(periods) == 1 else "warn",
                       "value": {"periods": periods, "min_score": min(grid["score"] for grid in grids),
                                 "min_uniformity": min(grid["uniformity"] for grid in grids)},
                       "threshold": "one integer period >= 2 with uniform blocks"})
    if args.onion and not args.once and len(frames) >= 3:
        if len(sizes) == 1:
            seam = forge_core.seam_report(pixels)
            value = round(float(seam["seam_over_median"]), 3)
            checks.append({"id": "loop_seam", "status": "pass" if value <= LOOP_SEAM_LIMIT else "warn",
                           "value": value, "threshold": LOOP_SEAM_LIMIT,
                           "detail": {key: round(float(seam[key]), 4) for key in ("seam", "adjacent_median")}})
        else:
            checks.append({"id": "loop_seam", "status": "skipped", "value": None, "threshold": LOOP_SEAM_LIMIT})
    return frames, checks, pixels, colours


def _local_publish_files(items: list[tuple[Path, Callable[[Path], None]]]) -> None:
    """Write each file beside its target, publish it without replacing; roll back all on failure."""
    published: list[Path] = []
    try:
        for target, write in items:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            os.close(handle)
            temporary = Path(name)
            try:
                write(temporary)
                forge_core.publish_file_no_replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            published.append(target)
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        raise


def _local_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def run(args: argparse.Namespace) -> dict:
    targets = [Path(path) for path in (args.report, args.review) if path]
    for target in targets:
        if os.path.lexists(target):
            raise FileExistsError(f"refusing to replace existing file {target}")
        if _local_inside(target, SKILL_DIR):
            raise codeart_core.CodeArtError("write reports inside your project, not inside the codeart2d skill folder")
    if args.report and args.review and Path(args.report).resolve() == Path(args.review).resolve():
        raise codeart_core.CodeArtError("--report and --review must be different files")
    scales = _int_list(args.scales, "--scales")
    backgrounds = [name.strip() for name in args.bg.split(",") if name.strip()]
    unknown = [name for name in backgrounds if name not in codeart_core.REVIEW_BACKGROUNDS]
    if unknown or not backgrounds:
        raise codeart_core.CodeArtError(f"--bg takes {', '.join(codeart_core.REVIEW_BACKGROUNDS)}, got {args.bg!r}")
    paths = expand_inputs(args.input)
    frames, checks, pixels, palette = analyse(paths, args)
    status = _status(checks)
    failed = [check for check in checks if check["status"] == "fail"]
    if args.strict and failed:
        details = [f"{check['id']} {check['value']} (limit {check['threshold']}) in "
                   f"{', '.join(check.get('failing', [])[:4]) or 'the frames'}" for check in failed]
        raise QAFailure("strict QA failed, nothing was written: " + "; ".join(details))

    review_image = None
    if args.review:
        if len({tuple(frame["size"]) for frame in frames}) != 1:
            raise codeart_core.CodeArtError("--review needs frames of one canvas size")
        width, height = frames[0]["size"]
        rows = sum(height * scale for scale in scales) * len(backgrounds) + (height * max(scales) if args.onion else 0)
        if len(frames) * width * max(scales) * rows > MAX_IMAGE_PIXELS:
            raise codeart_core.CodeArtError(f"a review sheet of {len(frames)} {width}x{height} frame(s) at --scales "
                                            f"{args.scales} would exceed {MAX_IMAGE_PIXELS} pixels; review fewer "
                                            "frames or use smaller --scales")
        first, last = frames[0]["label"], frames[-1]["label"]
        title = f"pixel_qa: {len(frames)} frame(s) {first}" + (f" .. {last}" if len(frames) > 1 else "")
        review_image = codeart_core.review_sheet(
            pixels, scales=scales, backgrounds=backgrounds, onion=args.onion, palette=palette,
            qa=[frame["metrics"] for frame in frames], anchor=_pair(args.anchor, "--anchor"),
            title=forge_core.ascii_text(title), loop=not args.once)
    items: list[tuple[Path, Callable[[Path], None]]] = []
    if review_image is not None:
        items.append((Path(args.review), lambda path: codeart_core.save_png(review_image, path)))
    if args.report:
        base = Path(args.report).resolve().parent
        frame_refs = [_file_ref(frame["path"], base) for frame in frames]
        palette_refs = [_file_ref(Path(args.palette), base)] if args.palette else []
        report = {
            "status": status, "method": QA_METHOD,
            "notProven": list(QA_NOT_PROVEN) + _skipped_notes(checks), "checks": checks,
            "inputs": frame_refs + palette_refs,
            "outputs": [],  # filled after the review sheet exists
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "frames": [_frame_entry(frame, ref["path"]) for frame, ref in zip(frames, frame_refs)],
        }
        if palette_refs:
            report["palette"] = {"file": palette_refs[0]["path"], "variant": args.variant}

        def write_report(path: Path) -> None:
            if args.review:
                report["outputs"] = [_file_ref(Path(args.review), base)]
            forge_core.write_json(path, report, no_clobber=False)

        items.append((Path(args.report), write_report))
    _local_publish_files(items)
    warned = [check["id"] for check in checks if check["status"] == "warn"]
    summary = {"status": status, "frames": len(frames), "failed": [check["id"] for check in failed],
               "warned": warned, "report": str(Path(args.report).resolve()) if args.report else None,
               "review": str(Path(args.review).resolve()) if args.review else None}
    if failed:  # D26: a failed QA exits 1 although its report is written; main() prints this line
        summary["error"] = (f"QA failed: {', '.join(check['id'] for check in failed)}"
                            + (" (report written)" if args.report else "") + "; --strict writes nothing on failure")
    return summary


def _skipped_notes(checks: list[dict]) -> list[str]:
    notes = {"off_palette": "Palette conformance: no --palette was given.",
             "outline_gaps": "Outline continuity and L-corners: no --outline colour was given."}
    return [notes[check["id"]] for check in checks if check["status"] == "skipped" and check["id"] in notes]


def _frame_entry(frame: dict, file: str) -> dict:
    keep = ("visible", "partial_alpha", "off_palette", "colors", "orphans", "l_corners", "outline_gaps", "bbox")
    entry = {"file": file, "size": frame["size"], "source_mode": frame["source_mode"],
             **{key: frame["metrics"][key] for key in keep}}
    for key in ("ground_gap_px", "grid"):
        if key in frame:
            entry[key] = frame[key]
    return entry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", nargs="+", required=True, help="PNG files or quoted glob patterns")
    parser.add_argument("--palette", help="palette (.json/.hex/.gpl), PixelSpec or codeart-meta.json")
    parser.add_argument("--variant", help="palette variant to check against (default: the base colours)")
    parser.add_argument("--outline", help="outline colour(s): #hex or palette names, comma-separated")
    parser.add_argument("--anchor", help="shared root x,y: checked inside every canvas, marked on the review")
    parser.add_argument("--detect-grid", action="store_true", help="report the integer pixel period of upscaled art")
    parser.add_argument("--review", help="write a review sheet PNG (a new file)")
    parser.add_argument("--scales", default="1,2,4", help="review scales, integers (default 1,2,4)")
    parser.add_argument("--bg", default="light,dark,checker", help="review backgrounds (default light,dark,checker)")
    parser.add_argument("--onion", action="store_true", help="onion-skin row on the review and the loop seam check")
    parser.add_argument("--once", action="store_true", help="frames are a one-shot: no wrap, no loop seam check")
    parser.add_argument("--strict", action="store_true", help="exit 1 and write nothing when a check fails")
    parser.add_argument("--report", help="write the QA envelope JSON (a new file)")
    return parser


def _local_dependency_problem() -> str | None:
    """The pip command for missing numpy/Pillow, or why the skill's own modules did not import."""
    if _MISSING is None:
        return None
    import importlib.util

    missing = [name for name in ("numpy", "PIL") if importlib.util.find_spec(name) is None]
    if not missing:
        return f"error: cannot import the codeart2d libraries ({_MISSING}); reinstall the codeart2d skill"
    packages = " ".join("Pillow" if name == "PIL" else name for name in missing)
    interpreter = sys.executable.encode("ascii", "backslashreplace").decode("ascii")
    return (f"error: missing Python module(s): {', '.join(missing)}\n"
            f"install with: python -m pip install {packages}\n"
            f"(run it with the interpreter that runs this tool: {interpreter})")


def main(argv: Sequence[str] | None = None) -> int:
    if _MISSING is None:
        forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    problem = _local_dependency_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    try:
        summary = run(args)
    except KeyboardInterrupt:
        print("error: interrupted; nothing was written", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except (QAFailure, codeart_core.CodeArtError, ValueError, OSError) as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    except BaseException as error:  # noqa: BLE001  D27: never a traceback for the user, not even for a
        # BaseException such as a Rust panic from an extension
        print(f"error: internal error ({type(error).__name__}: {forge_core.ascii_text(str(error))})", file=sys.stderr)
        return 1
    message = summary.pop("error", None)
    print(json.dumps(summary, ensure_ascii=True))
    if message:  # D26: a failed check exits 1 although the report and review sheet are written
        print(f"error: {forge_core.ascii_text(message)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
