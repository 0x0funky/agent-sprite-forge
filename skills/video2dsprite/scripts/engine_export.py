"""Fixed-canvas animation packaging. No generation, network access or game edits."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:] or "encoder command failed")
    return result.stdout


def capabilities() -> dict:
    ffmpeg = shutil.which("ffmpeg")
    encoders = run([ffmpeg, "-hide_banner", "-encoders"]) if ffmpeg else ""
    return {"ffmpeg": ffmpeg, "ffprobe": shutil.which("ffprobe"),
            "webm": "libvpx-vp9" in encoders, "packed": "libx264" in encoders,
            "png": True}


def pair(text: str | None, fallback: tuple, integer: bool = False) -> tuple:
    values = tuple((int if integer else float)(v) for v in text.split(",")) if text else fallback
    if len(values) != 2 or not all(math.isfinite(v) for v in values):
        raise ValueError("expected two finite comma-separated coordinates")
    return values


def bbox(im: Image.Image) -> tuple | None:
    return im.getchannel("A").point(lambda a: 255 if a > 32 else 0).getbbox()


def analyze(frames: list[Image.Image], fps: float, loop: bool) -> dict:
    """Report diagnostics, never certify contact or identity from image bounds."""
    boxes = [bbox(im) for im in frames]
    valid = [b for b in boxes if b]
    w, h = frames[0].size
    def premult(im):
        a = np.asarray(im.resize((min(w, 128), min(h, 128))), dtype=np.float32) / 255
        a[..., :3] *= a[..., 3:4]
        return a
    errors = []
    first = premult(frames[0])
    previous = first
    for im in frames[1:]:
        current = premult(im)
        errors.append(float(np.abs(current - previous).mean()))
        previous = current
    seam = float(np.abs(previous - first).mean())
    median = float(np.median(errors)) if errors else 0
    return {
        "status": "needs-visual-review", "loopRequested": loop,
        "frameCount": len(frames), "durationSeconds": len(frames) / fps,
        "blankFrames": [i for i, b in enumerate(boxes) if not b],
        "edgeTouchFrames": [i for i, b in enumerate(boxes) if b and
                            (b[0] == 0 or b[1] == 0 or b[2] == w or b[3] == h)],
        "boundsBottomSpanPx": max(b[3] for b in valid) - min(b[3] for b in valid) if valid else None,
        "boundsCenterXSpanPx": max((b[0]+b[2])/2 for b in valid) - min((b[0]+b[2])/2 for b in valid) if valid else None,
        "seamPremultipliedMAE": round(seam, 6),
        "adjacentMedianMAE": round(median, 6),
        "seamToMedianRatio": round(seam/median, 3) if median > 1e-8 else None,
        "adjacentMaxMAE": round(max(errors, default=0), 6),
        "notes": ["Bounds bottom is a contact-drift proxy, not a tracked foot/root.",
                  "A similar first/last frame does not prove a seamless cycle or stable identity.",
                  "Airborne poses may legitimately change bottom position; review at runtime scale."],
    }


def prepare(frames: list[Image.Image], source_size: tuple, source_anchor: tuple,
            max_side: int, crop_union: bool) -> tuple[list[Image.Image], dict]:
    if not frames or not 2 <= max_side <= 4096 or min(source_size) <= 0:
        raise ValueError("nonempty frames, positive source size and max-side in 2..4096 required")
    input_size = frames[0].size
    if any(im.size != input_size for im in frames):
        raise ValueError("mixed source canvas dimensions are not supported")
    if abs(source_size[0]/source_size[1] - input_size[0]/input_size[1]) > 0.01:
        raise ValueError("source-size aspect differs from input canvas; register generated footage to the approved art canvas first")
    if not (0 <= source_anchor[0] <= source_size[0] and 0 <= source_anchor[1] <= source_size[1]):
        raise ValueError("source-anchor must be inside source-size")
    # Crop geometry includes every nonzero-alpha pixel, including faint FX.
    # Diagnostic body bounds use a threshold, but must never trim actual art.
    boxes = [bb for im in frames if (bb := im.getchannel("A").getbbox())]
    if not boxes:
        raise ValueError("all frames are transparent")
    rect = (0, 0, *input_size)
    if crop_union:
        # One envelope over the entire clip, never per-frame cropping.
        rect = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
    w, h = rect[2]-rect[0], rect[3]-rect[1]
    scale = min(1, max_side/max(w, h))
    cw, ch = max(1, round(w*scale)), max(1, round(h*scale))
    ew, eh = (cw+1)//2*2, (ch+1)//2*2
    prepared = []
    for im in frames:
        canvas = Image.new("RGBA", (ew, eh))
        # Plain paste copies straight alpha, unlike paste(image, mask=image).
        canvas.paste(im.crop(rect).resize((cw, ch), Image.Resampling.LANCZOS), (0, 0))
        prepared.append(canvas)
    sx, sy = source_size[0]/input_size[0], source_size[1]/input_size[1]
    source_rect = [rect[0]*sx, rect[1]*sy, w*sx, h*sy]
    return prepared, {
        "sourceSize": list(source_size), "sourceAnchor": list(source_anchor),
        "inputFrameSize": list(input_size),
        "sourceRect": source_rect, "sourceRectFormat": "x,y,width,height",
        "encodedSize": [ew, eh], "contentSize": [cw, ch],
        "encodedAnchor": [(source_anchor[0]-source_rect[0])*cw/source_rect[2],
                          (source_anchor[1]-source_rect[1])*ch/source_rect[3]],
        "registration": "fixed-union-envelope" if crop_union else "preserved-source-canvas",
    }


def digest(path: Path) -> dict:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            sha.update(block)
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": sha.hexdigest()}


def encode(frames: list[Image.Image], output: Path, name: str, fps: float,
           formats: set[str], caps: dict) -> dict:
    files = {}
    if not formats:
        return files
    with tempfile.TemporaryDirectory(prefix="forge-encode-") as tmp:
        folder = Path(tmp)
        for i, im in enumerate(frames):
            im.save(folder / f"frame_{i:05d}.png")
        base = [caps["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
                "-threads", "2", "-framerate", str(fps), "-i", str(folder / "frame_%05d.png"), "-an"]
        if "webm" in formats:
            path = output / f"{name}.webm"
            run(base + ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
                        "-b:v", "0", "-crf", "28", "-threads", "2", str(path)])
            files["webm"] = {**digest(path), "mimeType": "video/webm", "alpha": "native-vp9",
                             "requiresAlphaPlaybackVerification": True}
        if "packed" in formats:
            path = output / f"{name}-packed.mp4"
            graph = ("[0:v]format=rgba,split=2[c][a];[c]format=rgb24[c2];"
                     "[a]alphaextract,format=rgb24[a2];[c2][a2]hstack=inputs=2,"
                     "scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p[v]")
            run(base + ["-filter_complex_threads", "1", "-filter_complex", graph, "-map", "[v]",
                        "-c:v", "libx264", "-profile:v", "main", "-crf", "18", "-preset", "fast",
                        "-threads", "2", "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
                        "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", str(path)])
            w, h = frames[0].size
            files["packedAlpha"] = {**digest(path), "mimeType": "video/mp4",
                                     "layout": "rgb-left-alpha-right", "width": w, "height": h,
                                     "encodedSize": [w*2, h], "requiresCompositor": True}
        # Decode the complete stream to catch truncated encoder output. Browser
        # alpha/display verification is separately required, not implied here.
        for item in files.values():
            decoder = ["-c:v", "libvpx-vp9"] if item.get("alpha") else []
            run([caps["ffmpeg"], "-hide_banner", "-loglevel", "error", *decoder,
                 "-i", str(output / item["file"]), "-f", "null", "-"])
            item["fullDecodePassed"] = True
    return files


def package(args) -> dict:
    source = Path(args.clean_dir)
    paths = sorted(source.glob("clean_*.png")) or sorted(source.glob("*.png"))
    if not paths:
        raise ValueError("no PNG frames in clean-dir")
    if not math.isfinite(args.fps) or args.fps <= 0:
        raise ValueError("fps must be positive and finite")
    formats = set(p.strip() for p in args.formats.split(",") if p.strip())
    if not formats <= {"png", "webm", "packed"}:
        raise ValueError("formats supports png,webm,packed")
    if not args.name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in args.name):
        raise ValueError("name must contain only letters, digits, hyphens and underscores")
    caps = capabilities() if formats - {"png"} else {"png": True}
    for fmt in formats - {"png"}:
        if not caps.get(fmt):
            raise RuntimeError(f"{fmt} needs ffmpeg with {'libx264' if fmt == 'packed' else 'libvpx-vp9'}; use --formats png for offline fallback")
    sizes = []
    for path in paths:
        with Image.open(path) as image:
            sizes.append(image.size)
    if sum(w*h for w, h in sizes) > 128_000_000:
        raise ValueError("decoded frame budget exceeds 128 million pixels; trim the clip or extract at lower fps/resolution")
    frames = [Image.open(path).convert("RGBA") for path in paths]
    size = pair(args.source_size, frames[0].size, True)
    anchor = pair(args.source_anchor, (size[0]/2, size[1]))
    prepared, geometry = prepare(frames, size, anchor, args.max_side, args.crop_union)
    qa = analyze(frames, args.fps, args.loop)
    output = Path(args.out_dir)
    if source.resolve() == output.resolve():
        raise ValueError("out-dir must differ from clean-dir")
    if output.exists():
        raise ValueError("out-dir already exists; use a fresh destination to preserve accepted assets")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{args.name}-stage-", dir=output.parent))
    try:
        manifest = write_package(stage, args, prepared, geometry, qa, paths, caps, formats)
        # Recheck to avoid replacing a destination created while encoding.
        if output.exists():
            raise ValueError("out-dir was created during packaging; refusing to replace it")
        stage.rename(output)
        return manifest
    finally:
        if stage.exists():
            if stage.resolve().parent != output.parent.resolve() or not stage.name.startswith(f".{args.name}-stage-"):
                raise RuntimeError("staging cleanup target escaped the output parent")
            shutil.rmtree(stage)


def write_package(output, args, prepared, geometry, qa, paths, caps, formats):
    """Write only inside a fresh staging directory; caller publishes on success."""
    # PNG fallback is always included, even when compressed formats are selected.
    poster = output / f"{args.name}-poster.png"
    prepared[0].save(poster)
    width, height = prepared[0].size
    # Page atlases to stay below 4096 per side instead of an unbounded strip.
    cols = min(8, max(1, 4096//width))
    rows = max(1, 4096//height)
    page_frames = cols*rows
    atlases = []
    for start in range(0, len(prepared), page_frames):
        chunk = prepared[start:start+page_frames]
        actual_cols = min(cols, len(chunk))
        sheet = Image.new("RGBA", (width*actual_cols, height*math.ceil(len(chunk)/actual_cols)))
        for i, im in enumerate(chunk):
            sheet.paste(im, ((i%actual_cols)*width, (i//actual_cols)*height))
        path = output / f"{args.name}-atlas-{len(atlases):02d}.png"
        sheet.save(path)
        atlases.append({**digest(path), "firstFrame": start, "frameCount": len(chunk), "columns": actual_cols})
    files = encode(prepared, output, args.name, args.fps, formats-{"png"}, caps)
    manifest = {
        "schemaVersion": "2.0", "name": args.name, **geometry,
        "fps": args.fps, "frameCount": len(prepared), "durationSeconds": len(prepared)/args.fps,
        "loop": args.loop, "reviewStatus": "needs-visual-review", "hitEvents": [],
        "poster": digest(poster), "fallback": {"type": "png-atlas", "cellSize": [width, height], "pages": atlases},
        **files, "qa": qa,
        "inputFrames": [{**digest(p), "file": p.name} for p in paths],
        "runtimeNotes": ["Game logic owns hit timings, damage and root motion.",
                         "Packed MP4 has no native alpha; reconstruct RGB and alpha before displaying.",
                         "Keep sourceSize/sourceAnchor for world geometry when selecting smaller media.",
                         "Use the PNG poster until a decoded first frame exists; preload only current scene actions."],
    }
    (output / "animation.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    (output / "animation-qa.json").write_text(json.dumps(qa, indent=2)+"\n", encoding="utf-8")
    return manifest
