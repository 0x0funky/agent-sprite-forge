"""Assemble native full-frame PNGs without resizing, alignment, masks, or chroma keying.

Examples:
  python assemble_frames.py --input a.png b.png --output-dir animation
  python assemble_frames.py --sheet sheet.png --rows 2 --cols 2 --output-dir animation

JSON options accept inline JSON or a JSON file path. Crop boxes use Pillow's
[left, top, right, bottom] convention (right/bottom excluded). Inputs must be
still, 8-bit RGB/RGBA PNGs; refusing other modes avoids implicit data conversion.
Numerical diagnostics are not visual approval of motion or a seamless loop.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile

import numpy as np
from PIL import Image, features


MAX_WEBP_DURATION = (1 << 24) - 1


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_option(value: str | None) -> object | None:
    if value is None:
        return None
    if value.lstrip().startswith(("[", "{")):
        return json.loads(value)
    return json.loads(Path(value).read_text(encoding="utf-8"))


def validate_box(value: object, size: tuple[int, int], label: str) -> tuple[int, int, int, int]:
    require(isinstance(value, list) and len(value) == 4, f"{label} must have four coordinates.")
    require(all(type(v) is int for v in value), f"{label} coordinates must be integers.")
    left, top, right, bottom = value
    require(0 <= left < right <= size[0] and 0 <= top < bottom <= size[1],
            f"{label} is empty or outside the image: {value}; image size is {size}.")
    return left, top, right, bottom


def load_png(path: Path) -> tuple[Image.Image, dict[str, object]]:
    raw = path.read_bytes()
    require(len(raw) >= 33 and raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR",
            f"Input must be a PNG: {path}")
    require(raw[24] == 8, f"Input must be an 8-bit PNG; refusing bit-depth conversion: {path}")
    with Image.open(io.BytesIO(raw)) as source:
        require(getattr(source, "n_frames", 1) == 1, f"Input must be a still PNG: {path}")
        require(source.mode in {"RGB", "RGBA"},
                f"Input mode must be RGB or RGBA; got {source.mode}: {path}")
        source.load()
        frame = source.copy()
    return frame, {
        "path": str(path.resolve()), "file_sha256": digest(raw), "bytes": len(raw),
        "size": list(frame.size), "mode": frame.mode,
    }


def load_frames(args: argparse.Namespace) -> tuple[list[Image.Image], list[dict[str, object]], list[dict[str, object]]]:
    source_records: list[dict[str, object]] = []
    frame_origins: list[dict[str, object]] = []
    if args.input:
        require(args.rows is None and args.cols is None and args.crop_boxes is None,
                "--rows, --cols and --crop-boxes require --sheet.")
        frames = []
        for index, path in enumerate(args.input):
            frame, record = load_png(path)
            frames.append(frame)
            source_records.append(record)
            frame_origins.append({"source_index": index})
    else:
        sheet, record = load_png(args.sheet)
        source_records.append(record)
        if args.crop_boxes is not None:
            require(args.rows is None and args.cols is None,
                    "Use --crop-boxes or --rows/--cols, not both.")
            raw_boxes = json_option(args.crop_boxes)
            require(isinstance(raw_boxes, list) and bool(raw_boxes), "--crop-boxes must be a nonempty JSON list.")
            boxes = [validate_box(box, sheet.size, f"crop {index}") for index, box in enumerate(raw_boxes)]
        else:
            require(type(args.rows) is int and type(args.cols) is int and args.rows > 0 and args.cols > 0,
                    "--sheet requires positive --rows and --cols, or explicit --crop-boxes.")
            width, height = sheet.size
            require(width % args.cols == 0 and height % args.rows == 0,
                    "Sheet dimensions are not divisible by the grid; supply reviewed --crop-boxes instead.")
            cell_width, cell_height = width // args.cols, height // args.rows
            require(cell_width > 0 and cell_height > 0, "Grid cells must have positive dimensions.")
            boxes = [(col * cell_width, row * cell_height, (col + 1) * cell_width, (row + 1) * cell_height)
                     for row in range(args.rows) for col in range(args.cols)]
        frames = [sheet.crop(box) for box in boxes]
        frame_origins = [{"source_index": 0, "crop_box": list(box)} for box in boxes]
    require(bool(frames), "At least one input frame is required.")
    require(len({frame.size for frame in frames}) == 1,
            f"Frame dimensions differ: {[frame.size for frame in frames]}; no resizing is performed.")
    return frames, source_records, frame_origins


def parse_sequence(value: str | None, frame_count: int) -> list[int]:
    if value is None:
        return list(range(frame_count))
    try:
        indices = [int(token.strip()) for token in value.split(",")]
    except ValueError as error:
        raise ValueError("--sequence must be comma-separated integer frame indices.") from error
    require(bool(indices) and all(0 <= index < frame_count for index in indices),
            f"Sequence indices must be between 0 and {frame_count - 1}.")
    return indices


def pixel_array(frame: Image.Image) -> np.ndarray:
    return np.asarray(frame.convert("RGBA"), dtype=np.uint8)


def same_visible_pixels(left: np.ndarray, right: np.ndarray) -> bool:
    if left.shape != right.shape or not np.array_equal(left[..., 3], right[..., 3]):
        return False
    return np.array_equal(left[..., :3][left[..., 3] > 0], right[..., :3][right[..., 3] > 0])


def transition_metrics(left: np.ndarray, right: np.ndarray) -> dict[str, float | int]:
    # Alpha-weighted RGB ignores meaningless hidden colors, while alpha is also
    # reported separately. For opaque frames this is ordinary full-frame RGB MAE.
    left_rgb = left[..., :3].astype(np.float32) * (left[..., 3:4].astype(np.float32) / 255)
    right_rgb = right[..., :3].astype(np.float32) * (right[..., 3:4].astype(np.float32) / 255)
    delta = np.abs(left_rgb - right_rgb)
    alpha_delta = np.abs(left[..., 3].astype(np.int16) - right[..., 3].astype(np.int16))
    return {
        "premultiplied_rgb_mae": round(float(delta.mean()), 6),
        "alpha_mae": round(float(alpha_delta.mean()), 6),
        "changed_visible_pixels": int(np.count_nonzero(np.any(delta > 0, axis=2) | (alpha_delta > 0))),
    }


def _riff_chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) % 2 else b"")


def _timed_static_webp(path: Path, frame: Image.Image, total_duration: int) -> None:
    """Retain ANIM timing when libwebp collapses all identical frames to a still.

    A single ANMF is valid animated WebP. Full-canvas no-blend frames retain the
    original pixels; no artificial pixel changes are used to defeat merging.
    """
    buffer = io.BytesIO()
    frame.save(buffer, format="WEBP", lossless=True, exact=True, quality=75, method=3)
    data = buffer.getvalue()
    require(data[:4] == b"RIFF" and data[8:12] == b"WEBP", "Invalid static WebP encoder output.")
    image_chunks = []
    offset = 12
    while offset + 8 <= len(data):
        tag = data[offset:offset + 4]
        length = int.from_bytes(data[offset + 4:offset + 8], "little")
        end = offset + 8 + length + length % 2
        require(end <= len(data), "Truncated static WebP chunk.")
        if tag in {b"VP8L", b"VP8 ", b"ALPH"}:
            image_chunks.append(data[offset:end])
        offset = end
    require(bool(image_chunks), "Static WebP has no image payload.")
    u24 = lambda value: value.to_bytes(3, "little")
    width, height = frame.size
    has_alpha = frame.mode == "RGBA" and frame.getchannel("A").getextrema()[0] < 255
    flags = 0x02 | (0x10 if has_alpha else 0)
    parts = [_riff_chunk(b"VP8X", bytes([flags, 0, 0, 0]) + u24(width - 1) + u24(height - 1)),
             _riff_chunk(b"ANIM", b"\0" * 6)]
    remaining = total_duration
    while remaining:
        duration = min(remaining, MAX_WEBP_DURATION)
        header = b"\0" * 6 + u24(width - 1) + u24(height - 1) + u24(duration) + b"\x02"
        parts.append(_riff_chunk(b"ANMF", header + b"".join(image_chunks)))
        remaining -= duration
    body = b"WEBP" + b"".join(parts)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def validate_webp(path: Path, pixels: list[np.ndarray], sequence: list[int], duration: int) -> dict[str, object]:
    expected_total = len(sequence) * duration
    elapsed = 0
    decoded_records = []
    with Image.open(path) as decoded:
        require(decoded.size == (pixels[0].shape[1], pixels[0].shape[0]), "WebP dimensions changed.")
        require(decoded.info.get("loop") == 0, "WebP infinite-loop flag is missing.")
        for index in range(decoded.n_frames):
            decoded.seek(index)
            decoded.load()
            actual_duration = decoded.info.get("duration")
            require(type(actual_duration) is int and actual_duration > 0, "WebP has no positive frame duration.")
            require(decoded.info.get("timestamp") == elapsed, "WebP timestamps are discontinuous.")
            end = elapsed + actual_duration
            require(end <= expected_total, "WebP timeline exceeds the requested duration.")
            actual = pixel_array(decoded)
            position = elapsed
            matching_positions = []
            while position < end:
                requested_position = position // duration
                require(same_visible_pixels(actual, pixels[sequence[requested_position]]),
                        f"WebP alpha/visible RGB mismatch at requested sequence position {requested_position}.")
                matching_positions.append(requested_position)
                position = min(end, (requested_position + 1) * duration)
            decoded_records.append({
                "index": index, "timestamp_ms": elapsed, "duration_ms": actual_duration,
                "sequence_positions": matching_positions,
            })
            elapsed = end
    require(elapsed == expected_total, f"WebP duration {elapsed} differs from requested {expected_total} ms.")
    return {
        "decoded_frame_count": len(decoded_records), "decoded_frames": decoded_records,
        "total_duration_ms": elapsed, "loop": 0,
        "alpha_and_visible_rgb_exact": True, "timeline_verified": True,
        "hidden_rgb_guaranteed": False,
    }


def encode_webp(path: Path, frames: list[Image.Image], pixels: list[np.ndarray], sequence: list[int], duration: int) -> dict[str, object]:
    selected = [frames[index] for index in sequence]
    selected[0].save(path, format="WEBP", save_all=True, append_images=selected[1:],
                     duration=duration, loop=0, lossless=True, quality=75, method=3, allow_mixed=False)
    with Image.open(path) as decoded:
        decoded.load()
        collapsed_to_still = decoded.n_frames == 1 and (
            decoded.info.get("duration", 0) <= 0 or decoded.info.get("loop") != 0
        )
    if collapsed_to_still:
        require(all(same_visible_pixels(pixels[sequence[0]], pixels[index]) for index in sequence),
                "Encoder collapsed distinct visible frames into one still.")
        _timed_static_webp(path, selected[0], len(sequence) * duration)
    result = validate_webp(path, pixels, sequence, duration)
    result.update({"file": path.name, "file_sha256": digest(path.read_bytes()), "bytes": path.stat().st_size,
                   "lossless": True, "quality": 75, "method": 3,
                   "timed_static_container": collapsed_to_still})
    return result


def publish_directory(stage: Path, final: Path) -> None:
    """Atomically rename a directory without replacing even an empty destination."""
    if os.path.lexists(final):
        raise FileExistsError(f"Refusing existing output: {final}")
    if os.name == "nt":
        # Windows rename fails if any destination already exists, including dirs.
        os.rename(stage, final)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(stage), -100, os.fsencode(final), 1)  # RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(stage), os.fsencode(final), 4)  # RENAME_EXCL
    else:
        raise RuntimeError("This platform lacks a supported atomic no-replace directory rename.")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(final))


def assemble(args: argparse.Namespace) -> dict[str, object]:
    if os.path.lexists(args.output_dir):
        raise FileExistsError(f"Refusing existing output: {args.output_dir}")
    require(type(args.duration) is int and 0 < args.duration <= MAX_WEBP_DURATION,
            f"Duration must be between 1 and {MAX_WEBP_DURATION} integer milliseconds.")
    require(features.check("webp"), "Pillow requires WebP support.")
    frames, sources, origins = load_frames(args)
    sequence = parse_sequence(args.sequence, len(frames))
    require(len(sequence) * args.duration < (1 << 31), "Total duration exceeds the WebP encoder timestamp range.")
    pixels = [pixel_array(frame) for frame in frames]
    manifest = json_option(args.manifest)
    require(manifest is None or isinstance(manifest, dict), "--manifest must be a JSON object.")
    regions = json_option(args.static_regions)
    require(regions is None or isinstance(regions, dict), "--static-regions must be a JSON object of named boxes.")
    region_boxes = {name: validate_box(box, frames[0].size, f"region {name}")
                    for name, box in (regions or {}).items()}
    final = args.output_dir.parent.resolve() / args.output_dir.name
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{final.name}.stage-", dir=final.parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        frame_dir = stage / "frames"
        frame_dir.mkdir()
        records = []
        for index, frame in enumerate(frames):
            path = frame_dir / f"frame-{index:02d}.png"
            frame.save(path, compress_level=3)
            with Image.open(path) as decoded:
                require(np.array_equal(pixel_array(decoded), pixels[index]), "PNG pixel roundtrip failed.")
            records.append({
                "index": index, "file": path.relative_to(stage).as_posix(), **origins[index],
                "size": list(frame.size), "mode": frame.mode,
                "file_sha256": digest(path.read_bytes()), "rgba_pixel_sha256": digest(pixels[index].tobytes()),
            })
        columns = math.ceil(math.sqrt(len(frames)))
        rows = math.ceil(len(frames) / columns)
        width, height = frames[0].size
        atlas = Image.new("RGBA", (columns * width, rows * height))
        for index, frame in enumerate(frames):
            atlas.paste(frame, ((index % columns) * width, (index // columns) * height))
        atlas_path = stage / "atlas.png"
        atlas.save(atlas_path, compress_level=3)
        with Image.open(atlas_path) as decoded:
            for index, expected in enumerate(pixels):
                x, y = (index % columns) * width, (index // columns) * height
                require(np.array_equal(pixel_array(decoded.crop((x, y, x + width, y + height))), expected),
                        "Atlas pixel roundtrip failed.")
        webp = encode_webp(stage / "animation.webp", frames, pixels, sequence, args.duration)
        transitions = [{"from_sequence_position": position,
                        "to_sequence_position": (position + 1) % len(sequence),
                        "from_frame": index, "to_frame": sequence[(position + 1) % len(sequence)],
                        "wrap": position == len(sequence) - 1,
                        **transition_metrics(pixels[index], pixels[sequence[(position + 1) % len(sequence)]])}
                       for position, index in enumerate(sequence)]
        roi_reports = {}
        for name, (left, top, right, bottom) in region_boxes.items():
            crops = [array[top:bottom, left:right] for array in pixels]
            roi_reports[name] = {
                "box": [left, top, right, bottom], "reference_frame": sequence[0],
                "against_reference": [{"frame": index, **transition_metrics(crops[sequence[0]], crop)}
                                      for index, crop in enumerate(crops)],
                "registration_performed": False,
            }
        result = {
            "schema": "generate2dsprite.full_frames.v1", "output_directory": str(final),
            "frame_size": [width, height], "source_frame_count": len(frames),
            "sources": sources, "frames": records, "sequence": sequence,
            "duration_ms": args.duration, "total_duration_ms": len(sequence) * args.duration, "loop": 0,
            "atlas": {"file": "atlas.png", "size": list(atlas.size), "rows": rows, "cols": columns,
                      "file_sha256": digest(atlas_path.read_bytes()), "rgba_pixels_exact": True},
            "webp": webp, "transitions": transitions, "static_regions": roi_reports,
            "provenance": {"user_manifest": manifest, "user_assertions_verified": False},
            "validation": {"png_rgba_exact": True, "atlas_rgba_exact": True,
                           "webp_timeline_verified": True, "visual_approval": False,
                           "note": "Numerical diagnostics only; no claim of seamless motion or visual continuity."},
            "processing": {"resized": False, "aligned": False, "chroma_keyed": False, "masked": False},
        }
        (stage / "animation.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        publish_directory(stage, final)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", nargs="+", type=Path, help="Ordered still 8-bit RGB/RGBA PNG files.")
    source.add_argument("--sheet", type=Path, help="One still PNG containing the frames.")
    parser.add_argument("--rows", type=int)
    parser.add_argument("--cols", type=int)
    parser.add_argument("--crop-boxes", help="JSON list of explicit crop boxes, or path to that JSON.")
    parser.add_argument("--sequence", help="Comma-separated zero-based source frame indices; default input order.")
    parser.add_argument("--duration", type=int, default=250, help="Duration in milliseconds for each sequence position.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory; existing paths are refused.")
    parser.add_argument("--manifest", help="User-authored provenance JSON object, or path to that JSON.")
    parser.add_argument("--static-regions", help="JSON {name: [left, top, right, bottom]}, or path to that JSON.")
    return parser


def main() -> int:
    parser = build_parser()
    try:
        result = assemble(parser.parse_args())
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(2, f"Assembly failed: {error}\n")
    print(result["output_directory"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
