#!/usr/bin/env python3
"""Postprocess generated or supplied video into registered 2D animation assets.

Pipeline steps (deterministic only — no creative generation):
  extract  → ffmpeg frames from mp4
  clean    → magenta flood-fill chroma + light despill
  sample   → even-index frame sets + feet/center normalize
  process  → extract + clean + sample in one shot

Generation is a separate provider step. This processor never makes API calls.
"""

from __future__ import annotations

import argparse
import importlib.util
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


def extract_frames(video: Path, out_dir: Path, fps: float = 0.0,
                   start: float = 0.0, duration: float | None = None,
                   decoder: str = "default") -> list[Path]:
    if not video.is_file():
        raise FileNotFoundError(video)
    if not math.isfinite(fps) or fps < 0 or not math.isfinite(start) or start < 0:
        raise ValueError("fps/start must be finite and nonnegative")
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError("duration must be positive")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install ffmpeg to extract video frames."
        )
    # Do not leave frames from a previous, longer clip in a rerun.
    _ensure_dir(out_dir)
    for old in out_dir.glob("frame_*.png"):
        old.unlink()
    pattern = str(out_dir / "frame_%04d.png")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if decoder == "libvpx-vp9":
        cmd += ["-c:v", "libvpx-vp9"]
    cmd += ["-i", str(video), "-ss", str(start)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    if fps and fps > 0:
        cmd += ["-vf", f"fps={fps}"]
    else:
        cmd += ["-fps_mode", "passthrough"]
    cmd.append(pattern)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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


def chroma_key_rgba(im: Image.Image, dist: float = 55.0, despill: float = 0.0) -> Image.Image:
    """Flood-fill magenta from corners, despill edges, return RGBA."""
    if not math.isfinite(despill) or not 0 <= despill <= 1:
        raise ValueError("despill must be between 0 and 1")
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

    # Optional correction on the first visible boundary only. Key candidates
    # adjacent to visited are already flood-filled, so testing key & ~visited
    # here would never reach a fringe. RGB correction leaves alpha untouched.
    adjacent = np.zeros_like(visited)
    adjacent[1:] |= visited[:-1]
    adjacent[:-1] |= visited[1:]
    adjacent[:, 1:] |= visited[:, :-1]
    adjacent[:, :-1] |= visited[:, 1:]
    channels = out[:, :, :3].astype(np.float32)
    spill = np.maximum(0, np.minimum(channels[:, :, 0], channels[:, :, 2]) - channels[:, :, 1])
    fringe = ~visited & adjacent & (out[:, :, 3] > 0) & (spill > 20)
    if despill and fringe.any():
        fr = out[fringe].astype(np.float32)
        amount = spill[fringe] * despill
        fr[:, 0] = np.clip(fr[:, 0] - amount, 0, 255)
        fr[:, 2] = np.clip(fr[:, 2] - amount, 0, 255)
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
    canvas.paste(resized, (x, y))
    return canvas


def fixed_envelope(sprites: Sequence[Image.Image], cell: int, body_height: int,
                   foot_y: int, anchor: str) -> tuple[list[Image.Image], dict]:
    """One scale and translation for the entire clip; motion stays authored."""
    if not sprites or cell < 4 or body_height < 1 or not 0 <= foot_y <= cell:
        raise ValueError("invalid frames, cell size, body height or foot line")
    size = sprites[0].size
    if any(im.size != size for im in sprites):
        raise ValueError("all frames must use one source canvas")
    boxes = [bb for im in sprites if (bb := im.getchannel("A").getbbox())]
    if not boxes:
        raise ValueError("all frames are empty")
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
           max(b[2] for b in boxes), max(b[3] for b in boxes))
    bw, bh = box[2] - box[0], box[3] - box[1]
    available_h = cell - 4 if anchor == "center" else min(cell - 4, foot_y)
    if available_h < 1:
        raise ValueError("foot-y leaves no space for the sprite")
    scale = min(body_height / bh, (cell - 4) / bw, available_h / bh)
    nw, nh = max(1, round(bw * scale)), max(1, round(bh * scale))
    x, y = (cell - nw) // 2, ((cell - nh) // 2 if anchor == "center" else foot_y - nh)
    frames = []
    for im in sprites:
        out = Image.new("RGBA", (cell, cell))
        # The shared crop retains deliberate changes in pose/height/contact.
        out.paste(im.crop(box).resize((nw, nh), Image.Resampling.LANCZOS), (x, y))
        frames.append(out)
    return frames, {"mode": "fixed-envelope", "sourceSize": list(size),
                    "unionRect": list(box), "scaleXY": [nw / bw, nh / bh],
                    "outputOffset": [x, y], "outputSize": [cell, cell]}


def clean_frames(
    raw_dir: Path,
    clean_dir: Path,
    dist: float = 55.0,
    key_mode: str = "auto",
    despill: float = 0.0,
) -> list[Path]:
    _ensure_dir(clean_dir)
    raws = sorted(raw_dir.glob("frame_*.png"))
    if not raws:
        raise RuntimeError(f"no raw frames in {raw_dir}")
    for old in clean_dir.glob("clean_*.png"):
        old.unlink()
    outs: list[Path] = []
    for i, path in enumerate(raws):
        im = Image.open(path)
        rgba = im.convert("RGBA")
        native_alpha = rgba.getchannel("A").getextrema()[0] < 255
        cleaned = rgba if key_mode == "none" or (key_mode == "auto" and native_alpha) else chroma_key_rgba(rgba, dist=dist, despill=despill)
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
    for old in sub.glob("sprite_*.png"):
        old.unlink()
    paths = []
    for i, sp in enumerate(sprites):
        p = sub / f"sprite_{i + 1:02d}.png"
        sp.save(p)
        paths.append(str(p))

    size = sprites[0].size[0]
    strip = Image.new("RGBA", (size * len(sprites), size), (0, 0, 0, 0))
    for i, sp in enumerate(sprites):
        strip.paste(sp, (i * size, 0))
    strip_path = out_sprite_dir / f"run-strip-{n_frames}.png"
    strip.save(strip_path)

    cols = 8 if n_frames >= 16 else 4
    rows = int(math.ceil(len(sprites) / cols))
    grid = Image.new("RGBA", (size * cols, size * rows), (0, 0, 0, 0))
    for i, sp in enumerate(sprites):
        r, c = divmod(i, cols)
        grid.paste(sp, (c * size, r * size))
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
    registration: str = "fixed",
    duration: float | None = None,
) -> dict:
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError("playback-duration must be positive and finite")
    cleans = sorted(clean_dir.glob("clean_*.png"))
    if not cleans:
        raise RuntimeError(f"no cleaned frames in {clean_dir}")
    sprite_dir = _ensure_dir(out_dir / "sprite")
    n_total = len(cleans)
    original = [Image.open(path).convert("RGBA") for path in cleans]
    if registration == "legacy-per-frame":
        normalized = [normalize_sprite(im, cell, body_height, foot_y, anchor) for im in original]
        registration_info = {"mode": "legacy-per-frame", "warning": "Per-frame resize changes body scale and erases contact motion."}
    else:
        normalized, registration_info = fixed_envelope(original, cell, body_height, foot_y, anchor)
    results = []
    for n_want in frame_counts:
        idxs = sample_indices(n_total, n_want)
        sprites = [normalized[idx] for idx in idxs]
        gif_ms = max(10, round(duration * 1000 / len(sprites))) if duration else None
        tag = f"x{n_want}" if n_want != 8 else ""
        # Always also write under xN for consistency when n!=8;
        # for 8, write both root sprites and optional x8.
        if n_want == 8:
            # root-level sprite_01..08 for backwards compat
            info = build_exports(sprites, sprite_dir, tag="", n_frames=n_want, gif_ms=gif_ms)
            # also x8 folder
            build_exports(sprites, sprite_dir, tag="x8", n_frames=n_want, gif_ms=gif_ms)
        else:
            info = build_exports(sprites, sprite_dir, tag=tag, n_frames=n_want, gif_ms=gif_ms)
        info["indices"] = idxs
        info["requestedCount"] = n_want
        info["count"] = len(sprites)
        results.append(info)
        print(f"exported {len(sprites)} frames (requested {n_want}) → {info['gif']}")
    return {"total_clean": n_total, "registration": registration_info,
            "previewTiming": "specified-duration" if duration else "legacy-preview-only",
            "sets": results}


def write_readme(out_dir: Path, meta: dict) -> None:
    lines = [
        "Video2dsprite output (provider-independent postprocessing)",
        "==========================================",
        "base/           base still on #FF00FF",
        "video/          image_to_video clip",
        "frames-raw/     decoded frames",
        "frames-clean/   chroma-keyed RGBA frames",
        "sprite/         sampled normalized sprites + strips/grids/GIFs",
        "pipeline-meta.json",
        "",
        "Generation is separate: native tools, generate2dmedia API, or supplied clip.",
        "Fixed-envelope registration is the default. Inspect source contact drift",
        "and loop seams; dense frames do not prove a seamless game-ready cycle.",
        "",
        json.dumps(meta, indent=2),
        "",
    ]
    (out_dir / "README.txt").write_text("\n".join(lines), encoding="utf-8")


def cmd_extract(args: argparse.Namespace) -> int:
    frames = extract_frames(Path(args.video), Path(args.out_dir), fps=args.fps,
                            start=args.start, duration=args.duration, decoder=args.decoder)
    print(f"extracted {len(frames)} frames → {args.out_dir}")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    outs = clean_frames(Path(args.raw_dir), Path(args.out_dir), dist=args.dist, key_mode=args.key_mode, despill=args.despill)
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
        registration=args.registration,
        duration=args.playback_duration,
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
    frames = extract_frames(video, raw_dir, fps=args.fps, start=args.start, duration=args.duration, decoder=args.decoder)
    print(f"clean {len(frames)} frames")
    clean_frames(raw_dir, clean_dir, dist=args.dist, key_mode=args.key_mode, despill=args.despill)
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
        registration=args.registration,
        duration=args.playback_duration,
    )
    meta = {
        "skill": "video2dsprite",
        "platform": "provider-independent offline processor",
        "name": args.name,
        "video": str(video.resolve()),
        "out_dir": str(out.resolve()),
        "raw_frames": len(frames),
        "chroma_dist": args.dist,
        "despill": args.despill,
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


def engine_module():
    spec = importlib.util.spec_from_file_location("forge_engine_export", Path(__file__).with_name("engine_export.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_package(args: argparse.Namespace) -> int:
    result = engine_module().package(args)
    print(json.dumps({"manifest": str((Path(args.out_dir)/"animation.json").resolve()),
                      "frameCount": result["frameCount"], "reviewStatus": result["reviewStatus"]}))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(json.dumps(engine_module().capabilities(), indent=2))
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
        sp.add_argument("--registration", choices=("fixed", "legacy-per-frame"), default="fixed")
        sp.add_argument("--playback-duration", type=float, help="Preview seconds; otherwise legacy comparison timing")

    def add_trim(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--start", type=float, default=0.0, help="Trim start in source seconds")
        sp.add_argument("--duration", type=float, help="Trim length in seconds")
        sp.add_argument("--decoder", choices=("default", "libvpx-vp9"), default="default",
                        help="Use libvpx-vp9 when extracting transparent VP9 WebM")

    pe = sub.add_parser("extract", help="ffmpeg extract frames")
    pe.add_argument("--video", required=True)
    pe.add_argument("--out-dir", required=True)
    pe.add_argument("--fps", type=float, default=0.0, help="0 = all frames")
    add_trim(pe)
    pe.set_defaults(func=cmd_extract)

    pc = sub.add_parser("clean", help="chroma-key raw frames")
    pc.add_argument("--raw-dir", required=True)
    pc.add_argument("--out-dir", required=True)
    pc.add_argument("--dist", type=float, default=55.0)
    pc.add_argument("--key-mode", choices=("auto", "magenta", "none"), default="auto")
    pc.add_argument("--despill", type=float, default=0.0, help="Opt-in boundary RGB correction strength, 0..1; alpha unchanged")
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
    pp.add_argument("--key-mode", choices=("auto", "magenta", "none"), default="auto")
    pp.add_argument("--despill", type=float, default=0.0, help="Opt-in boundary RGB correction strength, 0..1; alpha unchanged")
    add_trim(pp)
    add_common_sample(pp)
    pp.set_defaults(func=cmd_process)

    pk = sub.add_parser("package", help="fixed-canvas PNG fallback plus optional alpha video")
    pk.add_argument("--clean-dir", required=True, help="RGBA PNG frames sorted lexically")
    pk.add_argument("--out-dir", required=True)
    pk.add_argument("--name", default="clip")
    pk.add_argument("--fps", type=float, required=True, help="constant playback frame rate")
    pk.add_argument("--source-size", help="original art geometry W,H; defaults to input frames")
    pk.add_argument("--source-anchor", help="original art anchor X,Y; defaults to bottom-center")
    pk.add_argument("--max-side", type=int, default=384, help="encoding cap, never upscales")
    pk.add_argument("--crop-union", action="store_true", help="one shared alpha envelope for all frames")
    pk.add_argument("--formats", default="png", help="png,webm,packed; PNG fallback always included")
    pk.add_argument("--loop", action="store_true", help="request looping; seam still needs visual review")
    pk.set_defaults(func=cmd_package)

    pd = sub.add_parser("doctor", help="inspect local encoders without generating media")
    pd.set_defaults(func=cmd_doctor)

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
