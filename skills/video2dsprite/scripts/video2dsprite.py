#!/usr/bin/env python3
"""Postprocess Grok image_to_video clips into dense 2D sprites.

Pipeline steps (deterministic only — no creative generation):
  extract  → ffmpeg frames from mp4
  clean    → magenta flood-fill chroma + light despill
  sample   → even-index frame sets + feet/center normalize
  process  → extract + clean + sample in one shot

This skill is designed for Grok Build (image_gen + image_to_video).
The script itself only needs ffmpeg, Pillow, and numpy.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from PIL import Image

MAGENTA = np.array([255, 0, 255], dtype=np.float32)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_counts(text: str) -> list[int]:
    counts: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        n = int(part)
        if n < 1:
            raise ValueError(f"frame count must be >= 1, got {n}")
        counts.append(n)
    if not counts:
        raise ValueError("at least one frame count required")
    return counts


def sample_indices(n_total: int, n_want: int) -> list[int]:
    if n_total <= 0:
        return []
    if n_want >= n_total:
        return list(range(n_total))
    if n_want == 1:
        return [0]
    return [int(round(i * (n_total - 1) / (n_want - 1))) for i in range(n_want)]


def extract_frames(video: Path, out_dir: Path, fps: float = 0.0) -> list[Path]:
    _ensure_dir(out_dir)
    for old in out_dir.glob("frame_*.png"):
        old.unlink()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install ffmpeg to extract video frames."
        )
    pattern = str(out_dir / "frame_%04d.png")
    cmd = [ffmpeg, "-y", "-i", str(video)]
    if fps and fps > 0:
        cmd += ["-vf", f"fps={fps}"]
    else:
        cmd += ["-vsync", "0"]
    cmd.append(pattern)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed:\n" + (proc.stderr or proc.stdout or "unknown error")
        )
    frames = sorted(out_dir.glob("frame_*.png"))
    if not frames:
        raise RuntimeError(f"no frames extracted into {out_dir}")
    return frames


def _near_magenta_mask(rgb: np.ndarray, dist: float = 55.0) -> np.ndarray:
    """rgb: HxWx3 uint8 → bool mask of keyable magenta-ish pixels."""
    f = rgb.astype(np.float32)
    # Distance to pure magenta in RGB.
    d = np.linalg.norm(f - MAGENTA, axis=2)
    # Also catch bright pinks: high R+B, low G relative.
    r, g, b = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    pinkish = (r > 160) & (b > 160) & (g < 140) & ((r + b) / 2 - g > 40)
    return (d <= dist) | pinkish


def chroma_key_rgba(im: Image.Image, dist: float = 55.0) -> Image.Image:
    """Flood-fill magenta from corners, despill edges, return RGBA."""
    rgba = im.convert("RGBA")
    arr = np.array(rgba)
    rgb = arr[:, :, :3]
    alpha = arr[:, :, 3].astype(np.uint8)
    h, w = rgb.shape[:2]
    key = _near_magenta_mask(rgb, dist=dist)

    visited = np.zeros((h, w), dtype=bool)
    q: deque[tuple[int, int]] = deque()
    for y, x in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)):
        if key[y, x]:
            visited[y, x] = True
            q.append((x, y))
    # Also seed along edges where magenta is present.
    for x in range(w):
        for y in (0, h - 1):
            if key[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if key[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append((x, y))

    while q:
        x, y = q.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h and not visited[ny, nx] and key[ny, nx]:
                visited[ny, nx] = True
                q.append((nx, ny))

    out = arr.copy()
    out[visited, 3] = 0

    # Light despill on remaining near-magenta fringe (keep RGB, reduce alpha).
    fringe = key & ~visited
    if fringe.any():
        # Pull toward less magenta and soften alpha.
        fr = out[fringe].astype(np.float32)
        r, g, b, a = fr[:, 0], fr[:, 1], fr[:, 2], fr[:, 3]
        spill = np.maximum(0.0, (r + b) / 2.0 - g)
        factor = np.clip(1.0 - spill / 180.0, 0.15, 1.0)
        fr[:, 0] = np.clip(r - spill * 0.35, 0, 255)
        fr[:, 2] = np.clip(b - spill * 0.35, 0, 255)
        fr[:, 1] = np.clip(g + spill * 0.15, 0, 255)
        fr[:, 3] = np.clip(a * factor, 0, 255)
        out[fringe] = fr.astype(np.uint8)

    # Fully transparent where alpha is 0.
    out[out[:, :, 3] == 0, :3] = 0
    return Image.fromarray(out, "RGBA")


def content_bbox(im: Image.Image, alpha_min: int = 32) -> tuple[int, int, int, int] | None:
    arr = np.array(im.convert("RGBA"))
    mask = arr[:, :, 3] > alpha_min
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def normalize_sprite(
    im: Image.Image,
    cell: int = 128,
    body_height: int = 100,
    foot_y: int = 118,
    anchor: str = "feet",
) -> Image.Image:
    bb = content_bbox(im)
    canvas = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
    if not bb:
        return canvas
    crop = im.crop(bb)
    cw, ch = crop.size
    if ch <= 0 or cw <= 0:
        return canvas
    scale = body_height / float(ch)
    nw = max(1, int(round(cw * scale)))
    nh = max(1, int(round(ch * scale)))
    if nw > cell - 4:
        scale = (cell - 4) / float(cw)
        nw = max(1, int(round(cw * scale)))
        nh = max(1, int(round(ch * scale)))
    if nh > cell - 4:
        scale = (cell - 4) / float(ch)
        nw = max(1, int(round(cw * scale)))
        nh = max(1, int(round(ch * scale)))
    resized = crop.resize((nw, nh), Image.Resampling.LANCZOS)
    if anchor == "center":
        x = (cell - nw) // 2
        y = (cell - nh) // 2
    else:
        x = (cell - nw) // 2
        y = foot_y - nh
        if y < 0:
            y = 0
        if y + nh > cell:
            y = max(0, cell - nh)
    canvas.paste(resized, (x, y), resized)
    return canvas


def clean_frames(
    raw_dir: Path,
    clean_dir: Path,
    dist: float = 55.0,
) -> list[Path]:
    _ensure_dir(clean_dir)
    raws = sorted(raw_dir.glob("frame_*.png"))
    if not raws:
        raise RuntimeError(f"no raw frames in {raw_dir}")
    outs: list[Path] = []
    for i, path in enumerate(raws):
        im = Image.open(path)
        cleaned = chroma_key_rgba(im, dist=dist)
        out = clean_dir / f"clean_{i:04d}.png"
        cleaned.save(out)
        outs.append(out)
        if (i + 1) % 25 == 0 or i + 1 == len(raws):
            print(f"  cleaned {i + 1}/{len(raws)}")
    return outs


def build_exports(
    sprites: Sequence[Image.Image],
    out_sprite_dir: Path,
    tag: str,
    n_frames: int,
    gif_ms: int | None = None,
) -> dict:
    _ensure_dir(out_sprite_dir)
    sub = _ensure_dir(out_sprite_dir / tag) if tag else out_sprite_dir
    paths = []
    for i, sp in enumerate(sprites):
        p = sub / f"sprite_{i + 1:02d}.png"
        sp.save(p)
        paths.append(str(p))

    size = sprites[0].size[0]
    strip = Image.new("RGBA", (size * len(sprites), size), (0, 0, 0, 0))
    for i, sp in enumerate(sprites):
        strip.paste(sp, (i * size, 0), sp)
    strip_path = out_sprite_dir / f"run-strip-{n_frames}.png"
    strip.save(strip_path)

    cols = 8 if n_frames >= 16 else 4
    rows = int(math.ceil(len(sprites) / cols))
    grid = Image.new("RGBA", (size * cols, size * rows), (0, 0, 0, 0))
    for i, sp in enumerate(sprites):
        r, c = divmod(i, cols)
        grid.paste(sp, (c * size, r * size), sp)
    grid_path = out_sprite_dir / f"run-grid-{n_frames}.png"
    grid.save(grid_path)

    if gif_ms is None:
        if n_frames >= 40:
            gif_ms = 25
        elif n_frames >= 20:
            gif_ms = 40
        elif n_frames >= 12:
            gif_ms = 60
        else:
            gif_ms = 80

    frames_gif = []
    for sp in sprites:
        bg = Image.new("RGBA", sp.size, (30, 30, 40, 255))
        bg.paste(sp, (0, 0), sp)
        frames_gif.append(bg.convert("P", palette=Image.ADAPTIVE, colors=255))
    gif_path = out_sprite_dir / f"run-preview-{n_frames}.gif"
    frames_gif[0].save(
        gif_path,
        save_all=True,
        append_images=frames_gif[1:],
        duration=gif_ms,
        loop=0,
        disposal=2,
    )

    # Legacy alias for 8-frame default
    if n_frames == 8:
        alias = out_sprite_dir / "run-preview.gif"
        shutil.copy2(gif_path, alias)

    return {
        "count": n_frames,
        "tag": tag,
        "sprites": paths,
        "strip": str(strip_path),
        "grid": str(grid_path),
        "gif": str(gif_path),
        "gif_ms": gif_ms,
    }


def sample_and_export(
    clean_dir: Path,
    out_dir: Path,
    frame_counts: Sequence[int],
    cell: int = 128,
    body_height: int = 100,
    foot_y: int = 118,
    anchor: str = "feet",
) -> dict:
    cleans = sorted(clean_dir.glob("clean_*.png"))
    if not cleans:
        raise RuntimeError(f"no cleaned frames in {clean_dir}")
    sprite_dir = _ensure_dir(out_dir / "sprite")
    n_total = len(cleans)
    results = []
    for n_want in frame_counts:
        idxs = sample_indices(n_total, n_want)
        sprites = []
        for idx in idxs:
            im = Image.open(cleans[idx]).convert("RGBA")
            sprites.append(
                normalize_sprite(
                    im,
                    cell=cell,
                    body_height=body_height,
                    foot_y=foot_y,
                    anchor=anchor,
                )
            )
        tag = f"x{n_want}" if n_want != 8 else ""
        # Always also write under xN for consistency when n!=8;
        # for 8, write both root sprites and optional x8.
        if n_want == 8:
            # root-level sprite_01..08 for backwards compat
            info = build_exports(sprites, sprite_dir, tag="", n_frames=n_want)
            # also x8 folder
            build_exports(sprites, sprite_dir, tag="x8", n_frames=n_want)
        else:
            info = build_exports(sprites, sprite_dir, tag=tag, n_frames=n_want)
        info["indices"] = idxs
        results.append(info)
        print(f"exported {n_want} frames → {info['gif']}")
    return {"total_clean": n_total, "sets": results}


def write_readme(out_dir: Path, meta: dict) -> None:
    lines = [
        "Video2dsprite output (Grok Build pipeline)",
        "==========================================",
        "base/           base still on #FF00FF",
        "video/          image_to_video clip",
        "frames-raw/     decoded frames",
        "frames-clean/   chroma-keyed RGBA frames",
        "sprite/         sampled normalized sprites + strips/grids/GIFs",
        "pipeline-meta.json",
        "",
        "This folder was produced for Grok Build (image_gen + image_to_video).",
        "Codex/other agents cannot run the video step; they can still re-sample",
        "existing frames with: python video2dsprite.py sample --clean-dir ...",
        "",
        json.dumps(meta, indent=2),
        "",
    ]
    (out_dir / "README.txt").write_text("\n".join(lines), encoding="utf-8")


def cmd_extract(args: argparse.Namespace) -> int:
    frames = extract_frames(Path(args.video), Path(args.out_dir), fps=args.fps)
    print(f"extracted {len(frames)} frames → {args.out_dir}")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    outs = clean_frames(Path(args.raw_dir), Path(args.out_dir), dist=args.dist)
    print(f"cleaned {len(outs)} frames → {args.out_dir}")
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    counts = _parse_counts(args.frame_counts)
    meta = sample_and_export(
        clean_dir=Path(args.clean_dir),
        out_dir=Path(args.out_dir),
        frame_counts=counts,
        cell=args.cell_size,
        body_height=args.body_height,
        foot_y=args.foot_y,
        anchor=args.anchor,
    )
    out = Path(args.out_dir)
    full = {
        "mode": "sample",
        "clean_dir": str(Path(args.clean_dir).resolve()),
        **meta,
    }
    (out / "pipeline-meta.json").write_text(json.dumps(full, indent=2), encoding="utf-8")
    write_readme(out, full)
    print("sample done")
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    out = Path(args.out_dir)
    raw_dir = out / "frames-raw"
    clean_dir = out / "frames-clean"
    video = Path(args.video)
    if not video.is_file():
        raise FileNotFoundError(video)

    print(f"extract {video}")
    frames = extract_frames(video, raw_dir, fps=args.fps)
    print(f"clean {len(frames)} frames")
    clean_frames(raw_dir, clean_dir, dist=args.dist)
    counts = _parse_counts(args.frame_counts)
    print(f"sample counts={counts}")
    meta_sample = sample_and_export(
        clean_dir=clean_dir,
        out_dir=out,
        frame_counts=counts,
        cell=args.cell_size,
        body_height=args.body_height,
        foot_y=args.foot_y,
        anchor=args.anchor,
    )
    meta = {
        "skill": "video2dsprite",
        "platform": "Grok Build (image_to_video required for generation step)",
        "name": args.name,
        "video": str(video.resolve()),
        "out_dir": str(out.resolve()),
        "raw_frames": len(frames),
        "chroma_dist": args.dist,
        "cell_size": args.cell_size,
        "body_height": args.body_height,
        "foot_y": args.foot_y,
        "anchor": args.anchor,
        **meta_sample,
    }
    (out / "pipeline-meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    write_readme(out, meta)
    print("process done")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Video → dense 2D sprite postprocessor")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common_sample(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--frame-counts", default="8,16,24,48")
        sp.add_argument("--cell-size", type=int, default=128)
        sp.add_argument("--body-height", type=int, default=100)
        sp.add_argument("--foot-y", type=int, default=118)
        sp.add_argument("--anchor", choices=("feet", "center"), default="feet")

    pe = sub.add_parser("extract", help="ffmpeg extract frames")
    pe.add_argument("--video", required=True)
    pe.add_argument("--out-dir", required=True)
    pe.add_argument("--fps", type=float, default=0.0, help="0 = all frames")
    pe.set_defaults(func=cmd_extract)

    pc = sub.add_parser("clean", help="chroma-key raw frames")
    pc.add_argument("--raw-dir", required=True)
    pc.add_argument("--out-dir", required=True)
    pc.add_argument("--dist", type=float, default=55.0)
    pc.set_defaults(func=cmd_clean)

    ps = sub.add_parser("sample", help="sample cleaned frames into sprite sets")
    ps.add_argument("--clean-dir", required=True)
    ps.add_argument("--out-dir", required=True)
    add_common_sample(ps)
    ps.set_defaults(func=cmd_sample)

    pp = sub.add_parser("process", help="extract + clean + sample")
    pp.add_argument("--video", required=True)
    pp.add_argument("--out-dir", required=True)
    pp.add_argument("--name", default="clip")
    pp.add_argument("--fps", type=float, default=0.0)
    pp.add_argument("--dist", type=float, default=55.0)
    add_common_sample(pp)
    pp.set_defaults(func=cmd_process)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 — CLI surface
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
