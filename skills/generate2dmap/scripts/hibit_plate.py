#!/usr/bin/env python3
"""Turn a flat painted background into a hi-bit pixel-art plate (forge_hibit, the hi-bit studio recipe).

The image model paints the scene as clean flat art (lineless shapes, at most 3 tones per material; never
ask it for "pixel art"). This tool makes the pixels:

  1. crop the painting to the plate aspect (--focus FX,FY places the window on the axis with room),
  2. one palette of --colors (default 14) colours, k-means++ on luminance-weighted RGB of the painting
     (or --palette, a palette file to share across the plates of one game),
  3. every plate pixel takes the palette colour covering most of its cell (a majority vote, never a blend),
  4. lone pixels take their neighbours' majority unless they contrast strongly (lights, stars and eyes stay),
  5. --smooth 3x3 majority passes (default 1) merge specks into the big shapes: the paper-cut look.

It writes a new --output-dir with plate.png (native size, default 480x270, opaque), plate-x<k>.png (a nearest
preview, default x4), palette.json (generate2dsprite.palette.v1), shade-maps.json (per colour the in-palette
colour one step darker and one step lighter, so a game can light and flash without new colours) and
plate.json (crop, palette, QA). Success prints one ASCII JSON line; errors print "error: ..." and exit 1.
Draw the plate with nearest sampling at whole-number zoom (480x270 x4 = 1920x1080).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copies)
import forge_hibit as fh  # noqa: E402

PLATE_SCHEMA = "generate2dmap.hibit_plate.v1"
PALETTE_SCHEMA = "generate2dsprite.palette.v1"
SHADES_SCHEMA = "generate2dmap.hibit_shades.v1"
TOOL = {"name": "hibit_plate", "version": forge_core.FORGE_PACKAGE_VERSION}
DEFAULT_SIZE = (480, 270)
MIN_REDUCTION = 2.0      # a plate reduced less than 2x has too little to vote with; warn
NOT_PROVEN = [
    "That the painting reads as the intended scene at the plate size (look at plate-x4.png at gameplay scale).",
    "That characters and FX read against it: finish the sprites with their own palettes and look at a mock-up.",
    "Where the walkable ground is: the tool does not find the floor line in the art.",
]
METHOD = ("hibit_plate v1 (forge_hibit, the hi-bit studio recipe): cover crop of the painting to the plate aspect; "
          "one palette (k-means++ on luminance-weighted RGB, or a given palette); every source pixel indexed to its "
          "nearest palette colour and every plate pixel the colour covering most of its cell (majority, never a "
          "blend); forge_hibit.clean_isolated (lone pixels take the 4-neighbour majority unless 90 apart in "
          "luminance-weighted RGB); 3x3 majority smoothing passes; opaque; nearest preview only for review")


def _size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX,]\s*(\d+)\s*", text)
    if not match or min(int(match.group(1)), int(match.group(2))) < 8:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT of at least 8 px, for example 480x270")
    return int(match.group(1)), int(match.group(2))


def _focus(text: str) -> tuple[float, float]:
    try:
        values = tuple(float(part) for part in text.split(","))
    except ValueError:
        values = ()
    if len(values) != 2 or not all(math.isfinite(v) and 0 <= v <= 1 for v in values):
        raise argparse.ArgumentTypeError("use FX,FY with both in 0..1, for example 0.5,0.7")
    return values


def read_palette_colours(path: Path) -> np.ndarray:
    """Colours of a palette.v1 JSON ({"colors": [{"hex": ...} | "#rrggbb", ...]}) or a .hex file (one per line);
    a colour named "outline" (a hibit character palette) is skipped."""
    if path.suffix.lower() == ".json":
        data = forge_core.read_json(path)
        entries = data.get("colors") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ValueError(f"{path.name} has no colour list")
        colours = []
        for entry in entries:
            if isinstance(entry, dict):
                if entry.get("name") == "outline":
                    continue
                entry = entry.get("hex")
            colours.append(fh.parse_hex(str(entry)))
    else:
        colours = [fh.parse_hex(line) for line in path.read_text(encoding="utf-8-sig").splitlines()
                   if line.strip() and not line.strip().startswith(";")]
    if not 2 <= len(colours) <= 254:
        raise ValueError(f"{path.name} needs 2..254 colours; it has {len(colours)}")
    return np.array(colours, np.uint8)


def palette_document(palette: np.ndarray, source: str) -> dict[str, Any]:
    return {"schema": PALETTE_SCHEMA, "colors": [{"hex": fh.hex_of(colour)} for colour in palette],
            "transparent_index": 255, "locked": False, "source": source}


def plate_qa(idx: np.ndarray, size: tuple[int, int], colors: int, reduction: float) -> list[dict[str, Any]]:
    height, width = idx.shape
    used = int(np.unique(idx).size)
    padded = np.pad(idx, 1, mode="edge")
    lone = ((padded[1:-1, 2:] != idx) & (padded[1:-1, :-2] != idx) & (padded[2:, 1:-1] != idx)
            & (padded[:-2, 1:-1] != idx))
    checks = [
        {"id": "size", "status": "pass" if (width, height) == tuple(size) else "fail", "value": [width, height],
         "threshold": list(size)},
        {"id": "opaque", "status": "pass" if bool((idx >= 0).all()) else "fail",
         "value": int((idx < 0).sum()), "threshold": 0},
        {"id": "colours", "status": "pass" if used <= colors else "fail", "value": used, "threshold": colors},
        {"id": "lone-pixels", "status": "info", "value": round(float(lone.mean()), 5), "threshold": None,
         "note": "share of plate pixels with no 4-neighbour of their colour (kept features: lights, stars, eyes)"},
        {"id": "reduction", "status": "pass" if reduction >= MIN_REDUCTION else "warn", "value": round(reduction, 3),
         "threshold": MIN_REDUCTION, "note": "painting px per plate px; under 2x the majority has little to vote"},
    ]
    return checks


def cmd_plate(args: argparse.Namespace) -> dict[str, Any]:
    if args.colors is not None and args.palette is not None:
        raise ValueError("--colors learns a palette; drop it when --palette is given")
    colors = fh.PLATE_COLORS if args.colors is None else int(args.colors)
    if not 2 <= colors <= 254:
        raise ValueError("--colors must be 2..254")
    if not 0 <= args.smooth <= 4:
        raise ValueError("--smooth must be 0..4 passes")
    if not 1 <= args.preview_scale <= 8:
        raise ValueError("--preview-scale must be 1..8")
    image, info = forge_core.load_rgba(args.input)
    rgba = np.asarray(image)
    warnings = []
    if (rgba[..., 3] < 255).any():
        warnings.append("the painting has transparent pixels; they are read as their RGB (a plate is opaque)")
    out_w, out_h = args.size
    crop = fh.plate_crop(rgba.shape[1], rgba.shape[0], out_w, out_h, args.focus[0], args.focus[1])
    given = None if args.palette is None else read_palette_colours(args.palette)
    idx, palette = fh.pixelize_plate(rgba[..., :3], out_w, out_h, colors=colors, smooth=args.smooth, seed=args.seed,
                                     x_frac=args.focus[0], y_frac=args.focus[1], palette=given)
    used = np.unique(idx)
    if given is None:   # drop learned colours the plate does not use, keep the dark -> light order
        remap = np.full(len(palette), -1, np.int64)
        remap[used] = np.arange(len(used))
        idx, palette = remap[idx].astype(np.int32), palette[used]
    rendered = fh.render(idx, palette)[..., :3]
    dark, light = fh.shade_maps(palette)
    reduction = (crop[2] - crop[0]) / out_w
    checks = plate_qa(idx, (out_w, out_h), len(palette) if given is not None else colors, reduction)
    for check in checks:
        if check["status"] == "warn":
            warnings.append(f"{check['id']}: {check['value']} (threshold {check['threshold']})")
    final = Path(args.output_dir)
    source_text = (f"hibit_plate: k-means++ on luminance-weighted RGB of {args.input.name}, {colors} colours, seed "
                   f"{args.seed}" if given is None else f"hibit_plate: the colours of {args.palette.name}")
    with forge_core.staged_output(final) as stage:
        forge_core.save_png(Image.fromarray(rendered, "RGB").convert("RGBA"), stage / "plate.png")
        preview_name = f"plate-x{args.preview_scale}.png"
        zoomed = np.repeat(np.repeat(rendered, args.preview_scale, axis=0), args.preview_scale, axis=1)
        forge_core.save_png(Image.fromarray(zoomed, "RGB").convert("RGBA"), stage / preview_name)
        forge_core.write_json(stage / "palette.json", palette_document(palette, source_text))
        shades = {"schema": SHADES_SCHEMA, "colors": [fh.hex_of(c) for c in palette],
                  "dark": dark.tolist(), "light": light.tolist(),
                  "note": "dark[i] / light[i]: the palette index one step darker / lighter than colour i "
                          "(itself when none is close); light a plate or flash a sprite without new colours"}
        forge_core.write_json(stage / "shade-maps.json", shades)
        outputs = [forge_core.file_ref(stage / name, stage)
                   for name in ("plate.png", preview_name, "palette.json", "shade-maps.json")]
        inputs = [forge_core.file_ref(args.input, final, sha256=info["sha256"])]
        if args.palette is not None:
            inputs.append(forge_core.file_ref(args.palette, final))
        status = "fail" if any(c["status"] == "fail" for c in checks) else "needs-visual-review"
        record = {
            "schema": PLATE_SCHEMA, "tool": dict(TOOL), "hibit": fh.SCHEMA,
            "source": {**inputs[0], "size": [int(rgba.shape[1]), int(rgba.shape[0])]},
            "size": [out_w, out_h], "crop": list(crop), "focus": list(args.focus),
            "colors": int(len(palette)), "palette": [fh.hex_of(c) for c in palette], "paletteGiven": given is not None,
            "seed": args.seed, "smooth": args.smooth, "previewScale": args.preview_scale,
            "engine": {"sampling": "nearest", "pixelArt": True,
                       "note": "draw at whole-number zoom (480x270 x4 = 1920x1080); never filter the plate"},
            "warnings": warnings,
            "qa": {"status": status, "method": METHOD, "notProven": NOT_PROVEN, "checks": checks,
                   "inputs": inputs, "outputs": outputs, "tool": dict(TOOL)},
        }
        forge_core.write_json(stage / "plate.json", record)
    final = final.resolve()
    return {"status": status, "output": str(final), "plate": str(final / "plate.png"),
            "preview": str(final / preview_name), "palette": str(final / "palette.json"),
            "metadata": str(final / "plate.json"), "size": [out_w, out_h], "colors": int(len(palette)),
            "crop": list(crop), "_warnings": warnings}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hibit_plate.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    plate = verbs.add_parser("plate", help="a flat painting -> a hi-bit pixel-art plate (480x270 by default)",
                             description="Crop, palette, majority downsample, cleanup and paper-cut smoothing of a "
                                         "flat painted background; writes plate.png, a preview, palette.json, "
                                         "shade-maps.json and plate.json into a new folder.")
    plate.add_argument("--input", type=Path, required=True, help="the flat painted background (PNG, JPEG or WebP)")
    plate.add_argument("--output-dir", type=Path, required=True, help="new folder for the plate (must not exist)")
    plate.add_argument("--size", type=_size, default=DEFAULT_SIZE, metavar="WxH",
                       help="native plate size (default 480x270; x4 = 1920x1080)")
    plate.add_argument("--colors", type=int, help=f"palette size learned from the painting (default {fh.PLATE_COLORS})")
    plate.add_argument("--palette", type=Path, help="palette file (palette.v1 .json or .hex) shared by a game's plates")
    plate.add_argument("--focus", type=_focus, default=(0.5, 0.5), metavar="FX,FY",
                       help="where the crop window sits on the axis with room, 0..1 (default 0.5,0.5; 0.5,1 keeps "
                            "the bottom of a tall painting)")
    plate.add_argument("--smooth", type=int, default=1, help="3x3 majority passes, the paper-cut look (default 1)")
    plate.add_argument("--seed", type=int, default=0, help="k-means++ seed (default 0)")
    plate.add_argument("--preview-scale", type=int, default=4, help="nearest preview zoom (default 4)")
    plate.set_defaults(func=cmd_plate)
    return parser


def _cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = args.func(args)
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if summary["status"] == "fail" else 0


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27)."""
    return forge_core.run_cli(_cli, argv)


if __name__ == "__main__":
    raise SystemExit(main())
