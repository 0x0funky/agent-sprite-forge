#!/usr/bin/env python3
"""Hibit FX from the image route (FX route B): registered FX frames -> hi-bit pixel-art FX.

Hibit games take their effects from one of two routes; `fx_pixelfx.py check` says which one this machine has:

  A  pixelfx (recommended when installed): code-driven pixel FX from the user's own pixelfx checkout,
     fx_pixelfx.py render.
  B  the image route (this tool): an image model paints a 2x2 FX sheet as flat lineless art (route_media.py
     image), generate2dsprite.py process keys and registers it with ONE scale and ONE offset for every frame
     (--target asset --mode sheet --rows 2 --cols 2 --scale-strategy registered --cell-size 512), and this
     tool makes the pixels.

  pixelize  frames -> one union crop of every frame and one scale (--target-px along --axis), every source
            pixel indexed to a palette (--palette, such as the character's palette.json, or --colors learned
            by k-means++ on luminance-weighted RGB) and every output pixel the colour covering most of its
            cell (a majority vote, never a blend; --coverage), lone pixels cleaned with sparkles kept, binary
            alpha. Writes frames/, sheet.png + sheet.json (Aseprite-style), preview.gif (x4 on a dark
            backdrop, review only), palette.json and fx.json (route, sources, palette, scale, anchor, QA).

The anchor is the point the game places on the target: --anchor bottom (ground effects: impacts, dust, poofs on
the floor) or center (sparks, projectiles, pickups). Success prints one ASCII JSON line; errors print
"error: ..." and exit 1.
"""
from __future__ import annotations

import argparse
import json
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
import forge_palette as fp  # noqa: E402

SCHEMA = "generate2dsprite.hibit_fx.v1"
TOOL = {"name": "hibit_fx", "version": forge_core.FORGE_PACKAGE_VERSION}
MIN_REDUCTION = 3.0          # source px per output px; a majority vote needs a few sub-pixels per cell
PREVIEW_ZOOM = 4
PREVIEW_BACKDROP = (22, 22, 30)
NOT_PROVEN = [
    "That the effect reads at the game's scale and timing (look at preview.gif, then in the game).",
    "That the frames were registered with one scale and one offset (process --scale-strategy registered); "
    "frames centred one by one jump around.",
    "Smoothness: an image-generated 2x2 sheet has 4 frames; pixelfx effects have 16-90.",
]
METHOD = ("hibit_fx v1 (forge_hibit): union crop of every frame (alpha >= 128) and one scale; every source pixel "
          "indexed to its nearest palette colour (luminance-weighted RGB) and every output pixel the colour "
          "covering most of its cell (forge_hibit.mode_downsample, a majority vote, never a blend); "
          "forge_hibit.clean_isolated with islands kept (sparkles); binary alpha; 1 px transparent border")


def load_frames(folder: Path) -> list[tuple[Path, np.ndarray]]:
    """sheet-1.png, sheet-2.png ... (generate2dsprite.py process) in number order, else every PNG in name order."""
    numbered = sorted(folder.glob("sheet-*.png"), key=lambda p: int(p.stem.split("-")[-1])
                      if p.stem.split("-")[-1].isdigit() else 10 ** 9)
    numbered = [p for p in numbered if p.stem.split("-")[-1].isdigit()]
    files = numbered or sorted(p for p in folder.glob("*.png"))
    if not files:
        raise ValueError(f"{folder} has no PNG frames (expected generate2dsprite.py process output: sheet-1.png ...)")
    frames = [(path, np.asarray(forge_core.load_rgba(path)[0])) for path in files]
    size = frames[0][1].shape
    if any(frame.shape != size for _, frame in frames):
        raise ValueError("every frame must have the same size (registered cells)")
    return frames


def palette_colours(path: Path) -> np.ndarray:
    try:
        pal = fp.read_palette(path)
    except fp.PaletteError as error:
        raise ValueError(f"--palette: {error}") from None
    keep = [i for i in pal.usable.tolist() if (pal.names[i] if i < len(pal.names) else None) != "outline"]
    return np.asarray(pal.rgb[keep], np.uint8)


def pixelize(frames: Sequence[np.ndarray], *, target_px: float, axis: str, palette: np.ndarray | None,
             colors: int, coverage: float, seed: int = 0) -> tuple[list[np.ndarray], np.ndarray, dict[str, Any]]:
    union = np.zeros(frames[0].shape[:2], bool)
    for frame in frames:
        union |= frame[..., 3] >= 128
    if not union.any():
        raise ValueError("every frame is empty (no pixel with alpha >= 128)")
    ys, xs = np.nonzero(union)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    span = (x1 - x0) if axis == "w" else (y1 - y0)
    scale = float(target_px) / span
    out_w, out_h = max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale))
    if palette is None:
        samples = np.concatenate([f[..., :3][f[..., 3] >= 128] for f in frames])
        try:
            palette = fh.kmeans_palette(samples, colors, seed=seed)
        except ValueError as error:
            raise ValueError(f"palette: {error}") from None
    out = []
    for frame in frames:
        idx = fh.index_rgba(frame[y0:y1, x0:x1], palette, 128)
        small = fh.mode_downsample(idx, out_h, out_w, len(palette), coverage=coverage)
        small = fh.clean_isolated(small, palette, drop_islands=False)
        out.append(np.pad(fh.render(small, palette), ((1, 1), (1, 1), (0, 0))))
    info = {"crop": [x0, y0, x1, y1], "scale": scale, "reduction": 1.0 / scale, "size": [out_w + 2, out_h + 2]}
    return out, palette, info


def pack_sheet(frames: Sequence[np.ndarray], columns: int) -> np.ndarray:
    h, w = frames[0].shape[:2]
    rows = (len(frames) + columns - 1) // columns
    sheet = np.zeros((rows * h, columns * w, 4), np.uint8)
    for i, frame in enumerate(frames):
        r, c = divmod(i, columns)
        sheet[r * h:(r + 1) * h, c * w:(c + 1) * w] = frame
    return sheet


def preview_gif(frames: Sequence[np.ndarray], frame_ms: float, path: Path) -> None:
    images = []
    for frame in frames:
        alpha = frame[..., 3:4].astype(np.float32) / 255.0
        rgb = frame[..., :3].astype(np.float32) * alpha + np.array(PREVIEW_BACKDROP, np.float32) * (1 - alpha)
        big = np.repeat(np.repeat(np.clip(rgb + 0.5, 0, 255).astype(np.uint8), PREVIEW_ZOOM, 0), PREVIEW_ZOOM, 1)
        images.append(Image.fromarray(big, "RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT,
                                                            dither=Image.Dither.NONE))
    images[0].save(path, save_all=True, append_images=images[1:], duration=max(20, round(frame_ms)), loop=0,
                   disposal=1, optimize=False)


def cmd_pixelize(args: argparse.Namespace) -> dict[str, Any]:
    if args.palette is not None and args.colors is not None:
        raise ValueError("--colors learns a palette; drop it when --palette is given")
    if not (2 <= args.target_px <= 512):
        raise ValueError("--target-px must be 2..512")
    if not (0.05 <= args.coverage <= 0.95):
        raise ValueError("--coverage must be 0.05..0.95")
    if not (10 <= args.frame_ms <= 1000):
        raise ValueError("--frame-ms must be 10..1000")
    colors = 6 if args.colors is None else int(args.colors)
    if not 2 <= colors <= 64:
        raise ValueError("--colors must be 2..64")
    folder = Path(args.frames)
    if not folder.is_dir():
        raise ValueError(f"--frames {folder} is not a folder")
    sources = load_frames(folder)
    given = None if args.palette is None else palette_colours(args.palette)
    frames, palette, info = pixelize([f for _, f in sources], target_px=args.target_px, axis=args.axis,
                                     palette=given, colors=colors, coverage=args.coverage, seed=args.seed)
    h, w = frames[0].shape[:2]
    anchor = [w / 2, h - 1] if args.anchor == "bottom" else [w / 2, h / 2]
    name = args.name or folder.name
    used = np.unique(np.concatenate([f[..., :3][f[..., 3] > 0] for f in frames]), axis=0)
    packed_pal = {tuple(int(v) for v in c) for c in palette}
    off = sum(1 for c in used if tuple(int(v) for v in c) not in packed_pal)
    alpha_levels = np.unique(np.concatenate([np.unique(f[..., 3]) for f in frames]))
    empty = [i for i, f in enumerate(frames) if not (f[..., 3] > 0).any()]
    checks = [
        {"id": "frames", "status": "pass", "value": len(frames), "threshold": None},
        {"id": "palette-fit", "status": "pass" if off == 0 else "fail", "value": int(off), "threshold": 0},
        {"id": "alpha-binary", "status": "pass" if set(alpha_levels.tolist()) <= {0, 255} else "fail",
         "value": [int(v) for v in alpha_levels], "threshold": [0, 255]},
        {"id": "empty-frames", "status": "warn" if empty else "pass", "value": empty, "threshold": []},
        {"id": "reduction", "status": "pass" if info["reduction"] >= MIN_REDUCTION else "warn",
         "value": round(info["reduction"], 3), "threshold": MIN_REDUCTION,
         "note": "source px per output px; the majority vote wants about 4x or more"},
    ]
    status = "fail" if any(c["status"] == "fail" for c in checks) else "needs-visual-review"
    warnings = [f"{c['id']}: {c['value']}" for c in checks if c["status"] == "warn"]
    final = Path(args.output_dir)
    columns = min(len(frames), 8)
    with forge_core.staged_output(final) as stage:
        (stage / "frames").mkdir()
        written = []
        digits = max(3, len(str(len(frames) - 1)))
        for i, frame in enumerate(frames):
            path = stage / "frames" / f"{name}_{i:0{digits}d}.png"
            forge_core.save_png(frame, path)
            written.append(path)
        forge_core.save_png(pack_sheet(frames, columns), stage / "sheet.png")
        sheet_doc = {"frames": [{"filename": f"{name} {i}", "frame": {"x": (i % columns) * w, "y": (i // columns) * h,
                                                                      "w": w, "h": h},
                                 "rotated": False, "trimmed": False,
                                 "spriteSourceSize": {"x": 0, "y": 0, "w": w, "h": h},
                                 "sourceSize": {"w": w, "h": h}, "duration": round(args.frame_ms)}
                                for i in range(len(frames))],
                     "meta": {"app": "agent-sprite-forge hibit_fx", "image": "sheet.png", "format": "RGBA8888",
                              "size": {"w": columns * w, "h": ((len(frames) + columns - 1) // columns) * h},
                              "scale": "1", "frameTags": [{"name": name, "from": 0, "to": len(frames) - 1,
                                                           "direction": "forward"}],
                              "anchor": anchor, "loop": bool(args.loop)}}
        forge_core.write_json(stage / "sheet.json", sheet_doc)
        preview_gif(frames, args.frame_ms, stage / "preview.gif")
        pal_doc = {"schema": "generate2dsprite.palette.v1", "colors": [{"hex": fh.hex_of(c)} for c in palette],
                   "transparent_index": 255, "locked": False,
                   "source": "hibit_fx: " + ("the given palette" if given is not None
                                             else f"k-means++ on luminance-weighted RGB, {colors} colours")}
        forge_core.write_json(stage / "palette.json", pal_doc)
        outputs = [forge_core.file_ref(p, stage) for p in
                   [*written, stage / "sheet.png", stage / "sheet.json", stage / "preview.gif", stage / "palette.json"]]
        inputs = [forge_core.file_ref(path, final) for path, _ in sources]
        if args.palette is not None:
            inputs.append(forge_core.file_ref(args.palette, final))
        record = {
            "schema": SCHEMA, "tool": dict(TOOL), "route": "image", "hibit": fh.SCHEMA,
            "artSource": "image generation (an FX sheet from route_media.py), made pixel art by the hibit finish",
            "effect": {"name": name, "frames": len(frames), "frameSize": [w, h], "frameMs": args.frame_ms,
                       "loop": bool(args.loop), "anchor": anchor, "anchorAt": args.anchor},
            "scale": {"targetPx": args.target_px, "axis": args.axis, **info},
            "palette": {"colors": [fh.hex_of(c) for c in palette], "given": given is not None,
                        "coverage": args.coverage},
            "engine": {"sampling": "nearest", "pixelArt": True},
            "warnings": warnings,
            "qa": {"status": status, "method": METHOD, "notProven": NOT_PROVEN, "checks": checks,
                   "inputs": inputs, "outputs": outputs, "tool": dict(TOOL)},
        }
        forge_core.write_json(stage / "fx.json", record)
    final = final.resolve()
    return {"status": status, "route": "image", "output": str(final), "metadata": str(final / "fx.json"),
            "frames": len(frames), "frameSize": [w, h], "anchor": anchor, "colors": int(len(palette)),
            "scale": round(info["scale"], 5), "preview": str(final / "preview.gif"), "_warnings": warnings}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hibit_fx.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    px = verbs.add_parser("pixelize", help="registered FX frames -> hibit pixel-art FX (route B)",
                          description="One union crop and one scale, a palette, a majority-vote downscale, "
                                      "sparkles kept, binary alpha; frames, sheet, preview, palette and fx.json.")
    px.add_argument("--frames", required=True,
                    help="registered FX frames: generate2dsprite.py process output (sheet-1.png ...) or a PNG folder")
    px.add_argument("--output-dir", required=True, help="new folder (must not exist)")
    px.add_argument("--target-px", type=float, required=True,
                    help="output px of the union of every frame along --axis, such as 60 for a 60 px wide impact")
    px.add_argument("--axis", choices=("w", "h"), default="w", help="w (default, width) or h (height)")
    px.add_argument("--palette", type=Path,
                    help="palette file to index against (the character's palette.json shares its colours; its "
                         "outline colour is skipped)")
    px.add_argument("--colors", type=int, help="without --palette: learn this many colours (2..64, default 6)")
    px.add_argument("--coverage", type=float, default=0.4,
                    help="share of a cell that makes it opaque (default 0.4: thin rays and sparks survive)")
    px.add_argument("--anchor", choices=("center", "bottom"), default="center",
                    help="center (default: sparks, projectiles, pickups) or bottom (impacts, dust, floor poofs)")
    px.add_argument("--frame-ms", type=float, default=90.0, help="milliseconds per frame (default 90)")
    px.add_argument("--loop", action="store_true", help="a looping effect (projectile, pickup)")
    px.add_argument("--name", help="effect name for files and tags (default: the frames folder name)")
    px.add_argument("--seed", type=int, default=0, help="k-means++ seed of a learned palette (default 0)")
    px.set_defaults(func=cmd_pixelize)
    return parser


def _cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = args.func(args)
    for warning in summary.pop("_warnings", []):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if summary["status"] == "fail" else 0


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27)."""
    return forge_core.run_cli(_cli, argv)


if __name__ == "__main__":
    raise SystemExit(main())
