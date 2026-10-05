"""Export explicit animation clips from already-extracted shared-canvas sprites.

Usage: python build_animation_clips.py --manifest clips.json --output-dir new-bundle

Manifest paths are relative to the manifest file. A shared anchor_px is a canvas
root origin (often a ground reference), never each frame's visible bottom.
No frame is cropped, aligned, normalized, keyed or regenerated. PNGs are copied
byte-for-byte; labeled contact sheets are diagnostic previews only.

Example manifest:
{
  "schema": "generate2dsprite.animation_clips.v1",
  "frames": ["frame-1.png", "frame-2.png", "idle.png"],
  "anchor_px": [64, 112],
  "clips": {
    "run": {"frames": [0, 1], "duration_ms": [80, 100], "loop": true},
    "idle": {"frames": [2], "duration_ms": 400, "loop": true}
  },
  "states": {"moving": "run", "idle": "idle"}
}
Frame entries may instead be {"name": "run-1", "file": "frame-1.png"};
clip frames accept zero-based indices or unique names. Optional positive
stride_world_units declares travel per clip cycle; it is not measured from art.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont


_SPEC = importlib.util.spec_from_file_location(
    "_animation_clip_frame_utils", Path(__file__).with_name("assemble_frames.py")
)
assert _SPEC and _SPEC.loader
FRAME_UTILS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(FRAME_UTILS)
require = FRAME_UTILS.require
SCHEMA = "generate2dsprite.animation_clips.v1"


def load_contract(path: Path) -> tuple[dict, list[Image.Image], list[np.ndarray], list[dict]]:
    contract = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(contract, dict), "Manifest must be a JSON object.")
    require(contract.get("schema", SCHEMA) == SCHEMA, f"Expected schema {SCHEMA}.")
    entries = contract.get("frames")
    require(isinstance(entries, list) and bool(entries), "frames must be a nonempty list.")
    images, arrays, records = [], [], []
    names = set()
    for index, entry in enumerate(entries):
        require(isinstance(entry, (str, dict)), f"Frame {index} must be a path or named file object.")
        if isinstance(entry, str):
            file, name = entry, Path(entry).stem
        else:
            require("anchor_px" not in entry and "anchorPx" not in entry,
                    "Per-frame anchors are not supported; declare one shared anchor_px.")
            file, name = entry.get("file"), entry.get("name")
        require(isinstance(file, str) and bool(file), f"Frame {index} needs a file path.")
        require(isinstance(name, str) and bool(name.strip()) and name not in names,
                f"Frame {index} needs a unique nonempty name; duplicate stems need explicit names.")
        names.add(name)
        source = (path.parent / file).resolve()
        image, provenance = FRAME_UTILS.load_png(source)
        array = FRAME_UTILS.pixel_array(image)
        require(image.mode == "RGBA" and bool(np.any(array[..., 3] == 0)) and bool(np.any(array[..., 3] > 0)),
                f"Frame {name} must contain both transparent and visible pixels in RGBA; use extracted sprites.")
        images.append(image)
        arrays.append(array)
        records.append({"index": index, "name": name, "source": provenance,
                        "rgba_pixel_sha256": hashlib.sha256(array.tobytes()).hexdigest(),
                        "transparent_pixel_count": int(np.count_nonzero(array[..., 3] == 0)),
                        "visible_pixel_count": int(np.count_nonzero(array[..., 3] > 0))})
    require(len({image.size for image in images}) == 1, "All frames must have the same native canvas size.")
    anchor = contract.get("anchor_px")
    require(isinstance(anchor, list) and len(anchor) == 2
            and all(type(value) in (int, float) and math.isfinite(value) for value in anchor),
            "anchor_px must be two finite numeric shared-canvas coordinates.")
    width, height = images[0].size
    require(0 <= anchor[0] <= width and 0 <= anchor[1] <= height,
            "Shared anchor_px must be inside or on the edge of the source canvas.")
    return contract, images, arrays, records


def resolve_clips(contract: dict, frame_records: list[dict]) -> tuple[dict, dict]:
    raw_clips = contract.get("clips")
    require(isinstance(raw_clips, dict) and bool(raw_clips), "clips must be a nonempty object.")
    names = {record["name"]: record["index"] for record in frame_records}
    clips = {}
    for name, value in raw_clips.items():
        require(isinstance(name, str) and bool(name.strip()) and isinstance(value, dict),
                "Each clip needs a nonempty name and an object definition.")
        references = value.get("frames")
        require(isinstance(references, list) and bool(references), f"Clip {name} must contain frames.")
        indices = []
        for reference in references:
            if type(reference) is int:
                require(0 <= reference < len(frame_records), f"Clip {name} has an out-of-range frame index.")
                indices.append(reference)
            elif isinstance(reference, str):
                require(reference in names, f"Clip {name} refers to unknown frame {reference!r}.")
                indices.append(names[reference])
            else:
                raise ValueError(f"Clip {name} frame references must be integer indices or names.")
        timing = value.get("duration_ms")
        durations = [timing] * len(indices) if type(timing) is int else timing
        require(isinstance(durations, list) and len(durations) == len(indices),
                f"Clip {name} duration_ms must be an integer or one integer per frame.")
        require(all(type(duration) is int and 0 < duration <= FRAME_UTILS.MAX_WEBP_DURATION
                    for duration in durations), f"Clip {name} frame durations must be positive integer milliseconds.")
        total = sum(durations)
        require(total < (1 << 31), f"Clip {name} exceeds WebP timestamp limits.")
        loop = value.get("loop")
        require(type(loop) is bool, f"Clip {name} must declare loop as true or false.")
        clip = {"frames": indices, "duration_ms": durations, "loop": loop, "total_duration_ms": total,
                "nominal_fps": 1000 / durations[0] if len(set(durations)) == 1 else None,
                "average_fps": len(indices) * 1000 / total}
        if "stride_world_units" in value:
            stride = value["stride_world_units"]
            require(type(stride) in (int, float) and math.isfinite(stride) and stride > 0,
                    f"Clip {name} stride_world_units must be a positive finite number.")
            clip.update({"stride_world_units": stride, "stride_source": "user_declared",
                         "nominal_travel_speed_world_units_per_second": stride * 1000 / total})
        clips[name] = clip
    states = contract.get("states", {})
    require(isinstance(states, dict), "states must map state names to clip names.")
    for state, target in states.items():
        require(isinstance(state, str) and bool(state.strip()) and isinstance(target, str) and target in clips,
                f"State {state!r} must reference an existing clip.")
    return clips, states


def _set_native_webp_loop(path: Path, count: int) -> None:
    # The existing timed-static helper emits loop=0; change only the ANIM loop
    # field for a single-play clip. Encoded image payloads are untouched.
    data = bytearray(path.read_bytes())
    position = 12
    while position + 8 <= len(data):
        length = int.from_bytes(data[position + 4:position + 8], "little")
        require(position + 8 + length <= len(data), "Truncated WebP chunk.")
        if data[position:position + 4] == b"ANIM":
            require(length == 6, "Unexpected WebP ANIM chunk.")
            data[position + 12:position + 14] = count.to_bytes(2, "little")
            path.write_bytes(data)
            return
        position += 8 + length + length % 2
    raise ValueError("Timed WebP has no ANIM chunk.")


def encode_clip(path: Path, images: list[Image.Image], arrays: list[np.ndarray], clip: dict) -> dict:
    indices, durations = clip["frames"], clip["duration_ms"]
    selected = [images[index] for index in indices]
    loop_count = 0 if clip["loop"] else 1
    selected[0].save(path, format="WEBP", save_all=True, append_images=selected[1:],
                     duration=durations, loop=loop_count, lossless=True, quality=75,
                     method=3, allow_mixed=False)
    with Image.open(path) as decoded:
        decoded.load()
        collapsed = decoded.n_frames == 1 and decoded.info.get("duration", 0) <= 0
    if collapsed:
        require(all(FRAME_UTILS.same_visible_pixels(arrays[indices[0]], arrays[index]) for index in indices),
                "Encoder collapsed visually distinct frames.")
        FRAME_UTILS._timed_static_webp(path, selected[0], sum(durations))
        if loop_count != 0:
            _set_native_webp_loop(path, loop_count)
    boundaries = np.cumsum(durations).tolist()
    elapsed = 0
    decoded_records = []
    with Image.open(path) as decoded:
        require(decoded.size == images[0].size, "WebP preview changed the shared canvas size.")
        require(decoded.info.get("loop") == loop_count, "WebP loop policy differs from the clip.")
        for index in range(decoded.n_frames):
            decoded.seek(index)
            decoded.load()
            duration = decoded.info.get("duration")
            require(type(duration) is int and duration > 0, "WebP preview lost frame timing.")
            require(decoded.info.get("timestamp") == elapsed, "WebP timestamps are discontinuous.")
            end = elapsed + duration
            require(end <= sum(durations), "WebP playback exceeds the clip duration.")
            actual = FRAME_UTILS.pixel_array(decoded)
            position = elapsed
            matched = []
            while position < end:
                requested = bisect_right(boundaries, position)
                require(FRAME_UTILS.same_visible_pixels(actual, arrays[indices[requested]]),
                        f"WebP changed alpha/visible RGB at clip position {requested}.")
                matched.append(requested)
                position = min(end, boundaries[requested])
            decoded_records.append({"index": index, "start_ms": elapsed, "duration_ms": duration,
                                    "clip_positions": matched})
            elapsed = end
    require(elapsed == sum(durations), "WebP total playback duration differs from the clip.")
    return {"decoded_frames": decoded_records, "decoded_frame_count": len(decoded_records),
            "total_duration_ms": elapsed, "native_loop_count": loop_count,
            "timeline_verified": True, "alpha_and_visible_rgb_exact": True,
            "hidden_rgb_guaranteed": False, "lossless": True,
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def make_contact_sheet(path: Path, images: list[Image.Image], names: list[str], anchor: list) -> None:
    width, height = images[0].size
    columns = min(4, math.ceil(math.sqrt(len(images))))
    rows = math.ceil(len(images) / columns)
    cell_width, cell_height = max(width, 180) + 16, height + 48
    canvas = Image.new("RGBA", (columns * cell_width, rows * cell_height + 30), (28, 35, 44, 255))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=13)
    draw.text((8, 7), "NATIVE CANVASES | + shared root | diagnostic preview", font=font, fill=(232, 236, 239))
    for index, image in enumerate(images):
        col, row = index % columns, index // columns
        left = col * cell_width + (cell_width - width) // 2
        top = 30 + row * cell_height
        draw.rectangle((left, top, left + width - 1, top + height - 1), fill=(40, 50, 61))
        canvas.alpha_composite(image, (left, top))
        x, y = round(left + anchor[0]), round(top + anchor[1])
        draw.line((x - 4, y, x + 4, y), fill=(255, 220, 80), width=1)
        draw.line((x, y - 4, x, y + 4), fill=(255, 220, 80), width=1)
        draw.text((col * cell_width + 8, top + height + 10), f"{index}: {names[index][:22]}",
                  font=font, fill=(232, 236, 239))
    canvas.convert("RGB").save(path, compress_level=3)


def build(manifest_path: Path, output_dir: Path) -> dict:
    require(not os.path.lexists(output_dir), f"Refusing existing output directory: {output_dir}")
    source_manifest = manifest_path.read_bytes()
    contract, images, arrays, records = load_contract(manifest_path)
    clips, states = resolve_clips(contract, records)
    anchor = contract["anchor_px"]
    visible_hashes = {}
    for index, array in enumerate(arrays):
        canonical = array.copy()
        canonical[canonical[..., 3] == 0, :3] = 0
        key = hashlib.sha256(canonical.tobytes()).hexdigest()
        visible_hashes.setdefault(key, []).append(index)
    duplicates = [group for group in visible_hashes.values() if len(group) > 1]
    final = output_dir.parent.resolve() / output_dir.name
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{final.name}.stage-", dir=final.parent) as temporary:
        stage = Path(temporary) / "bundle"
        (stage / "frames").mkdir(parents=True)
        (stage / "clips").mkdir()
        for index, record in enumerate(records):
            destination = stage / "frames" / f"frame-{index:02d}.png"
            shutil.copyfile(record["source"]["path"], destination)
            require(hashlib.sha256(destination.read_bytes()).hexdigest() == record["source"]["file_sha256"],
                    "Source changed while copying; no output will be published.")
            record["file"] = destination.relative_to(stage).as_posix()
        for clip_index, (name, clip) in enumerate(clips.items()):
            indices = clip["frames"]
            preview = stage / "clips" / f"clip-{clip_index:02d}.webp"
            clip["preview"] = {"file": preview.relative_to(stage).as_posix(),
                               **encode_clip(preview, images, arrays, clip)}
            clip["transitions"] = [
                {"from_position": position, "to_position": position + 1,
                 **FRAME_UTILS.transition_metrics(arrays[indices[position]], arrays[indices[position + 1]])}
                for position in range(len(indices) - 1)
            ]
            clip["last_to_first"] = {"applies_to_playback": clip["loop"],
                                     **FRAME_UTILS.transition_metrics(arrays[indices[-1]], arrays[indices[0]])}
        contact = stage / "contact-sheet.png"
        make_contact_sheet(contact, images, [record["name"] for record in records], anchor)
        result = {
            "schema": SCHEMA, "source_manifest": {"path": str(manifest_path.resolve()),
                        "file_sha256": hashlib.sha256(source_manifest).hexdigest()},
            "frame_size": list(images[0].size), "anchor_px": anchor,
            "anchor_semantics": "shared canvas/root origin, often a ground reference; not per-frame visible bottom",
            "frames": records, "clips": clips, "states": states,
            "diagnostics": {"visible_pixel_duplicate_groups": duplicates, "visual_approval": False,
                            "note": "Pixel differences and duplicates do not establish animation quality or physical root stability."},
            "contact_sheet": {"file": "contact-sheet.png", "native_scale": True,
                              "annotated_with_shared_root": True, "not_a_runtime_atlas": True,
                              "file_sha256": hashlib.sha256(contact.read_bytes()).hexdigest()},
            "processing": {"source_pngs_byte_identical": True, "resized": False, "cropped": False,
                           "per_frame_alignment": False, "chroma_keyed": False},
            "alpha_validation": "Every input has fully transparent and visible pixels; edge quality still requires visual review.",
        }
        (stage / "source-manifest.json").write_bytes(source_manifest)
        (stage / "animation-clips.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        FRAME_UTILS.publish_directory(stage, final)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="New output directory; never overwrite existing paths.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        build(args.manifest, args.output_dir)
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(2, f"Clip export failed: {error}\n")
    print(str(args.output_dir.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
