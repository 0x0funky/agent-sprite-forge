#!/usr/bin/env python3
"""Motion for a 1x pixel sprite: the frames of an image-to-video clip put back on the sprite's own grid and palette.

  prepare  The sprite on the provider canvas for one action: prepare_i2v_input.py --pixel-art (the sprite enlarged by
           a whole number on a flat key colour, so every sprite pixel is a known square of the video) with one more
           paragraph in the prompt that tells the video model to keep the pixels. Without --scale the largest whole
           scale is taken at which the action's room still lies inside the canvas. The root is the middle of the
           sprite's feet (a weapon held to one side does not move it); --anchor names another. Writes a new job
           folder; give its prompt.txt and input.png to generate2dmedia/scripts/route_media.py video (this script
           calls no provider).
  frames   The clip -> every frame as a 1x picture: the lattice is the job's (the first frame's shift is found once
           and kept for the whole clip, so what stands still stays on its cells; a model gives the picture back a
           few px beside the job's place, to either side, and the first frame shows the sprite: the lattice is the
           one on which the sprite lies where the job put it), every cell gives the median of its
           inner part, a cell that is mostly key colour is empty, the key colour that compression blends into the
           figure's edge is unmixed, and every colour goes to the nearest colour of the sprite. Writes a new --output-dir: frames/frame_000.png ..., frames.json (per frame: cells, cells that
           were not flat, whether the figure touches the edge; the loop found in the clip) and frames-sheet.png
           (every frame with its number, cut to what the figure uses, for the host agent to LOOK at before it picks).
  finish   Pick the drawings (one cycle found in the clip, an action at equal steps of movement, or named frames),
           clean them (the sprite's own head put back where it fits, and a drawing whose head cannot be put back
           gives way to a neighbouring frame; islands off, single holes filled, the closed outline of a look that
           has one), trim them to what they use and write a new --output-dir: clean/frame_000.png ..., strip.png,
           selection.json (forge-frame-selection/v2), motion.json, review.png, and package/ (engine_export.py
           package: animation.json 3.0 with nearest sampling).
  measure  A walk or a run read BEFORE its drawings are picked: the footfalls (the frames at which the feet's front
           edge is farthest forward; a pole set on the ground is no foot, and when a blade's tip drags in front the
           toe-offs at the back edge are taken), the length of a step (checked against the lags at which the clip is
           like itself, and read from those alone when a robe hides the feet), the frames that only repeat the one
           before (a clip drawn "on twos"), the ground speed in game pixels a second (how fast the standing foot
           travels back), the pair of steps that closes best, and the drawings and durations to give to finish. Writes measure.json and
           legs.png (the legs of those drawings, enlarged, on the ground line). LOOK at legs.png: the leg in front
           at one footfall is the leg behind at the next. Legs that only open and close belong to a figure seen from
           the front: a move across the screen is made from a side view (pixel_look.py ask-view).
  breath   An idle without a clip: the sprite and the sprite with its upper part one cell lower (one row of the
           lower legs is taken out, what hangs from the hand comes down whole), with one empty cell around them. A video model's breath is smaller
           than one sprite pixel, and where its clip does move it redraws the pose and the face; this idle keeps
           every cell of the sprite. Writes the same set as finish.

Frame numbers are 0-based video frames everywhere. Durations are whole milliseconds. Success prints one ASCII JSON
line; errors print "error: ..." and exit 1 and leave nothing behind; usage errors exit 2. Nothing is ever replaced.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import forge_av  # noqa: E402  (this skill's vendored copies)
import forge_core  # noqa: E402
import forge_pixelgrid as pg  # noqa: E402

TOOL_NAME = "pixel_motion"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION
JOB_SCHEMA = "video2dsprite.registration_job.v1"
FRAMES_SCHEMA = "video2dsprite.pixel_frames.v1"
MOTION_SCHEMA = "video2dsprite.pixel_motion.v1"
WALK_SCHEMA = "video2dsprite.pixel_walk.v1"
SELECTION_SCHEMA = "forge-frame-selection/v2"
JOB_FILE, FRAMES_FILE = "registration_job.json", "frames.json"
GREY = (90, 90, 102)
SMALL_CELL = 5            # px: cells under this are small for a video model
ONESHOT_MS = 80           # a drawing of an action when no duration is given
SHEET_COLUMNS = 12        # frames a row of frames-sheet.png
REVIEW_COLUMNS = 6        # pictures a row of review.png (the sprite first)
LOOPS = ("cycle", "oneshot", "pingpong")

PIXEL_PARAGRAPH = ("This is low-resolution pixel art shown enlarged: in every frame keep the big square pixels, the flat "
                   "colours, the dark outline and the hard edges exactly as in the first frame; no smoothing, no blur, no "
                   "anti-aliasing, no new detail, no change of light.")
PIXEL_PARAGRAPH_DRAWN = ("This is low-resolution pixel art shown enlarged: in every frame keep the big square pixels, the flat "
                         "colours and the hard edges exactly as in the first frame, with no outline added around the figure; "
                         "no smoothing, no blur, no anti-aliasing, no new detail, no change of light.")


class MotionError(ValueError):
    """A refused input: printed as one "error: ..." line."""


def _hex(colour: Sequence[int]) -> str:
    return "#%02x%02x%02x" % (int(colour[0]), int(colour[1]), int(colour[2]))


def _rgb(text: str) -> tuple[int, int, int]:
    return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))


def _new_dir(path: str) -> Path:
    out = Path(path)
    if out.exists() and any(out.iterdir()):
        raise MotionError(f"{out} exists already; name a new folder (nothing is ever replaced)")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1), encoding="utf-8", newline="\n")


def _run_json(argv: Sequence[str]) -> dict:
    """Run one of this skill's scripts; its last stdout line is its JSON result."""
    done = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        lines = [line for line in (done.stderr or done.stdout or "").strip().splitlines() if line.strip()]
        raise MotionError(f"{Path(argv[1]).name} failed: " + (lines[-1].removeprefix("error: ") if lines else f"exit {done.returncode}"))
    lines = [line for line in done.stdout.strip().splitlines() if line.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        raise MotionError(f"{Path(argv[1]).name} printed no JSON result") from None


# --------------------------------------------------------------------------- prepare

KEYS = (("magenta", (255, 0, 255)), ("green", (0, 255, 0)), ("blue", (0, 0, 255)))
LEANS = 40.0              # a cell whose key channels lead the others by this much is a colour of that key's family ...
OWN_MOST = 0.005          # ... and a sprite with more than this share of such cells fights that key (prepare_i2v_input)
RING_MOST = 0.01          # the share of edge cells that may lean to the key before the package counts key residue


def key_leanings(sprite: np.ndarray) -> dict:
    """Per key colour: the share of the sprite's edge cells that lean to it by the package gate's measure, and the
    share of all its cells that are a colour of that key's family."""
    opaque = sprite[..., 3] > 0
    ring = opaque & pg.edge_mask(opaque)
    if not ring.any():
        return {name: (0.0, 0.0) for name, _ in KEYS}
    edge, cells = sprite[ring][:, :3], sprite[opaque][:, :3]
    return {name: (round(float((pg.key_lead(edge, rgb) > pg.SPILL).mean()), 4),
                   round(float((pg.key_lead(cells, rgb) >= LEANS).mean()), 4)) for name, rgb in KEYS}


def quiet_key(sprite: np.ndarray) -> str | None:
    """The key colour for a sprite's clips: of the keys the sprite's own colours do not fight, the one its edge
    cells lean to least (a purplish edge on a magenta ground is counted as key residue in every frame), then
    magenta, green, blue in that order. None when it fights all three: the preparation then says so."""
    leans = key_leanings(sprite)
    free = [name for name, _ in KEYS if leans[name][1] <= OWN_MOST]
    return min(free, key=lambda name: leans[name][0]) if free else None


def fitting_scale(job: dict) -> int:
    """The largest whole scale, at most the job's, at which the action's padded room lies inside the canvas margin."""
    width, height = job["referenceCanvas"]
    margin = float(job["margin"])
    w, h = job["sourceSize"]
    ax, ay = job["sourceAnchor"]
    rx, ry = job["referenceRoot"]
    left, top, right, bottom = job["padding"]
    for scale in range(int(job["referenceScale"]), 1, -1):
        ox, oy = forge_core.round_half_up(rx - scale * ax), forge_core.round_half_up(ry - scale * ay)
        if (ox - scale * left >= margin and oy - scale * top >= margin and ox + scale * (w + right) <= width - margin
                and oy + scale * (h + bottom) <= height - margin):
            return scale
    return 1


def cmd_prepare(o: argparse.Namespace) -> dict:
    sprite = Path(o.sprite)
    if not sprite.is_file():
        raise MotionError(f"{sprite} does not exist")
    if Path(o.output_dir).exists():
        raise MotionError(f"{o.output_dir} exists already; name a new folder (nothing is ever replaced)")
    extra = (PIXEL_PARAGRAPH_DRAWN if o.outline == "drawn" else PIXEL_PARAGRAPH) + (" " + o.extra.strip() if o.extra else "")
    try:
        cells = np.asarray(Image.open(sprite).convert("RGBA"))
    except (OSError, ValueError) as error:
        raise MotionError(f"cannot read {sprite.name}: {error}") from None
    anchor = o.anchor
    if anchor is None:       # the middle of the feet, not of the picture: a sword held to one side is no part of the root
        middle, row = pg.boots(cells)
        anchor = f"{middle + 0.5:g},{row + 1}"
    key = o.key if o.key not in (None, "auto") else quiet_key(cells)
    leaning = key_leanings(cells).get(key or "", (0.0, 0.0))[0]
    base = [sys.executable, str(HERE / "prepare_i2v_input.py"), "prepare", "--master", str(sprite), "--action", o.action,
            "--pixel-art", "--anchor", anchor, "--extra", extra]
    for flag, value in (("--subject", o.subject), ("--facing", o.facing), ("--key", key), ("--duration", o.duration),
                        ("--subject-height", o.subject_height), ("--canvas", o.canvas)):
        if value is not None:
            base += [flag, str(value)]
    scale, lowered = o.scale, None
    if scale is None:
        with tempfile.TemporaryDirectory() as scratch:
            _run_json(base + ["--output-dir", str(Path(scratch) / "job")])
            job = forge_core.read_json(Path(scratch) / "job" / JOB_FILE)
        scale = fitting_scale(job)
        if scale != job["referenceScale"]:
            lowered = [int(job["referenceScale"]), scale]
    result = _run_json(base + ["--scale", str(scale), "--output-dir", o.output_dir])
    job = forge_core.read_json(Path(result["metadata"]))
    warnings = [str(w) for w in job.get("warnings") or []]
    if scale < SMALL_CELL:
        warnings.append(f"a sprite pixel is only {scale} px of the video: video models blur cells this small; use a "
                        "smaller sprite or a larger canvas if the frames do not come back on the grid")
    if leaning > RING_MOST:
        warnings.append(f"{round(100 * leaning, 1)} percent of the sprite's edge cells lean to the {key} key: the package "
                        "counts them as key residue in every drawing; another --key, if the sprite's colours allow one")
    if any(v > 0 for v in job["workRegionClipped"]):
        warnings.append("the action's room does not fit the canvas even at this scale; a wide motion may leave the picture")
    return {"status": "ok", "output": result["output"], "metadata": result["metadata"], "input": result["input"],
            "prompt": result["prompt"], "action": o.action, "scale": int(scale), "scaleLowered": lowered,
            "anchor": job["sourceAnchor"], "workRegion": job["workRegion"], "keyColor": job["keyColor"], "warnings": warnings,
            "next": "route_media.py video --prompt-file <prompt> --reference <input> --duration 6 --resolution 720p; then "
                    "pixel_motion.py frames --clip <generated.mp4> --job <output>"}


# --------------------------------------------------------------------------- the job and the lattice

def read_job(path: str | Path) -> tuple[dict, np.ndarray, Path]:
    """The registration job of a pixel-art sprite, its 1x sprite (RGBA) and the job's folder."""
    job_path = Path(path)
    if job_path.is_dir():
        job_path = job_path / JOB_FILE
    if not job_path.is_file():
        raise MotionError(f"{job_path} does not exist (the job folder of pixel_motion.py prepare)")
    job = forge_core.read_json(job_path)
    if not isinstance(job, dict) or job.get("schema") != JOB_SCHEMA:
        raise MotionError(f"{job_path.name} is not a {JOB_SCHEMA}")
    if not job.get("pixelArt"):
        raise MotionError("the job was not prepared with --pixel-art: its sprite is not on a whole-number grid")
    if "viewBox" in job.get("master", {}):
        raise MotionError("the job is one view of a sheet; prepare the single sprite instead")
    scale = job.get("referenceScale")
    if not isinstance(scale, int) or scale < 2:
        raise MotionError(f"the job's scale is {scale!r}; a sprite pixel must be a whole number of at least 2 video px")
    sprite_path = job_path.parent / job["master"]["path"]
    if not sprite_path.is_file():
        raise MotionError(f"the job's sprite {sprite_path.name} is missing")
    sprite = np.asarray(Image.open(sprite_path).convert("RGBA"))
    if [sprite.shape[1], sprite.shape[0]] != list(job["sourceSize"]):
        raise MotionError("the job's sprite does not have the job's size")
    return job, sprite, job_path.parent


def room_limits(job: dict, frame_size: Sequence[int]) -> tuple[int, int, int, int]:
    """The most cells of room (left, top, right, bottom) around the sprite that the video canvas can hold; one cell
    stays free on the right and at the bottom for the lattice's shift."""
    scale = int(job["referenceScale"])
    ox, oy = (int(v) for v in job["referenceOffset"])
    w, h = job["sourceSize"]
    return (ox // scale, oy // scale, (frame_size[0] - ox) // scale - w - 1, (frame_size[1] - oy) // scale - h - 1)


def default_room(job: dict, frame_size: Sequence[int]) -> tuple[int, int, int, int]:
    """Room for the motion: the job's work region, or the sprite's larger side to the left and to the right (a
    narrow side view swings a long weapon as far as a wide front view), half its height above and a little below,
    whatever is larger, as far as the video canvas goes. A wide room costs nothing: finish trims the drawings."""
    scale = int(job["referenceScale"])
    ox, oy = (int(v) for v in job["referenceOffset"])
    w, h = job["sourceSize"]
    x0, y0, x1, y1 = job["workRegion"]
    region = (math.ceil((ox - x0) / scale), math.ceil((oy - y0) / scale), math.ceil((x1 - ox - w * scale) / scale),
              math.ceil((y1 - oy - h * scale) / scale))
    share = (max(w, h), math.ceil(0.5 * h), max(w, h), forge_core.round_half_up(0.15 * h))
    return tuple(max(0, min(limit, max(a, b))) for a, b, limit in zip(region, share, room_limits(job, frame_size)))


def contact_sheet(frames: Sequence[np.ndarray], numbers: Sequence[int], zoom: int, columns: int) -> Image.Image:
    """Frames in rows on flat grey, each with its number above it."""
    height, width = frames[0].shape[:2]
    cell_w, cell_h, label = width * zoom + 6, height * zoom + 16, 12
    rows = math.ceil(len(frames) / columns)
    sheet = Image.new("RGB", (columns * cell_w + 6, rows * cell_h + 6), GREY)
    draw = ImageDraw.Draw(sheet)
    for n, (frame, number) in enumerate(zip(frames, numbers)):
        x, y = 6 + (n % columns) * cell_w, 6 + (n // columns) * cell_h
        draw.text((x, y), str(number), fill=(235, 235, 240))
        big = Image.fromarray(frame, "RGBA").resize((width * zoom, height * zoom), Image.NEAREST)
        sheet.paste(big, (x, y + label), big)
    return sheet


# --------------------------------------------------------------------------- frames

def cmd_frames(o: argparse.Namespace) -> dict:
    clip = Path(o.clip)
    if not clip.is_file():
        raise MotionError(f"{clip} does not exist")
    job, sprite, job_dir = read_job(o.job)
    scale = int(job["referenceScale"])
    ox, oy = (int(v) for v in job["referenceOffset"])
    w, h = job["sourceSize"]
    try:
        info = forge_av.probe(clip)
    except (forge_av.ForgeAVError, OSError, ValueError) as error:
        raise MotionError(f"cannot read {clip.name}: {error}") from None
    size = (info["width"], info["height"])
    if list(size) != list(job["referenceCanvas"]):
        raise MotionError(f"the clip is {size[0]}x{size[1]}, the job's canvas is {job['referenceCanvas'][0]}x"
                          f"{job['referenceCanvas'][1]}: the sprite's pixels are not where the job says")
    limits = room_limits(job, size)
    if min(limits) < 0:
        raise MotionError("the sprite fills the video canvas: there is no room for the lattice; prepare at a smaller --scale")
    if o.room:
        room = tuple(int(v) for v in o.room.split(","))
        if len(room) != 4 or min(room) < 0:
            raise MotionError("--room is L,T,R,B in sprite pixels")
        if any(r > limit for r, limit in zip(room, limits)):
            raise MotionError(f"--room {list(room)} does not fit the video canvas; the most is {list(limits)}")
    else:
        room = default_room(job, size)
    left, top, right, bottom = room
    cols, rows = w + left + right, h + top + bottom
    x0, y0 = ox - left * scale, oy - top * scale
    out = _new_dir(o.output_dir)
    (out / "frames").mkdir()
    frames, records, key, shift, palette, tinted = [], [], None, (0, 0), None, 0
    try:
        stream = forge_av.iter_rgba(clip, alpha="off")
        for index, picture in enumerate(stream):
            rgb = picture[..., :3]
            if index == 0:
                key = pg.measure_key(rgb)
                if not o.raw_edges:       # a rare edge colour that is another colour of the sprite plus key is no colour
                    sprite = sprite.copy()
                    tinted = pg.drop_key_tinted(sprite, key)
                palette = pg.palette_of(sprite)
                Image.fromarray(sprite, "RGBA").save(out / "sprite.png")
            pad = 2 * scale                                      # two cells of key around: room for a lattice that lies
            padded = np.empty((rgb.shape[0] + 2 * pad, rgb.shape[1] + 2 * pad, 3), dtype=rgb.dtype)     # before the job's
            padded[...] = np.round(key).astype(rgb.dtype)
            padded[pad:-pad, pad:-pad] = rgb

            def read(at: Sequence[int]) -> tuple[np.ndarray, dict]:
                return pg.snap_frame(padded, x0 + pad + at[0], y0 + pad + at[1], cols, rows, scale, key, palette,
                                     key_mixed=not o.raw_edges)

            if index == 0:
                # The lattice is known to a cell's phase only, and the first frame shows the sprite: of the lattices
                # one cell either way, the one on which the sprite lies where the job put it.
                phase = pg.lattice_shift(rgb, x0, y0, cols, rows, scale, key)
                tries = []
                for dy in (0, -1, 1):
                    for dx in (0, -1, 1):
                        at = (phase[0] + dx * scale, phase[1] + dy * scale)
                        there = read(at)[0][top:top + h, left:left + w]
                        tries.append((int((there != sprite).any(axis=2).sum()), abs(dx) + abs(dy), at))
                shift = min(tries)[2]
            elif o.refit:                # the cells stay the first frame's: of this frame's phases, the one nearest the last
                phase = pg.lattice_shift(rgb, x0, y0, cols, rows, scale, key)
                shift = tuple(p + scale * forge_core.round_half_up((s - p) / float(scale)) for p, s in zip(phase, shift))
            frame, cells = read(shift)
            opaque = frame[..., 3] > 0
            touches = bool(opaque[0].any() or opaque[-1].any() or opaque[:, 0].any() or opaque[:, -1].any())
            Image.fromarray(frame, "RGBA").save(out / "frames" / f"frame_{index:03d}.png")
            frames.append(frame)
            records.append({"index": index, "shift": [int(shift[0]), int(shift[1])], "cells": cells["cells"],
                            "notFlat": cells["notFlat"], "keyMixed": cells["keyMixed"], "touchesEdge": touches})
    except (forge_av.ForgeAVError, OSError, ValueError) as error:
        raise MotionError(f"cannot decode {clip.name}: {error}") from None
    if len(frames) < 2:
        raise MotionError(f"{clip.name} has {len(frames)} frame(s)")
    first = frames[0][top:top + h, left:left + w]
    differs = int((first != sprite).any(axis=2).sum())
    cycle = pg.find_cycle(frames)
    span = pg.moving_span(frames)
    fps = info.get("fps_rational") or "24/1"
    not_flat = [r["notFlat"] / max(1, r["cells"]) for r in records]
    touching = [r["index"] for r in records if r["touchesEdge"]]
    ax, ay = job["sourceAnchor"]
    warnings = []
    if differs > 0.1 * w * h:
        warnings.append(f"the clip's first frame is not the sprite ({differs} of {w * h} cells differ): the model did not "
                        "start on the reference, or the clip was resized")
    if touching:
        warnings.append(f"the figure touches the edge of the room in {len(touching)} frame(s) (first: {touching[0]}); "
                        "give more --room if those frames are wanted, or prepare at a smaller --scale")
    record = {"schema": FRAMES_SCHEMA, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
              "clip": {"path": str(clip), "sha256": forge_core.sha256_file(clip), "size": list(size), "fps": fps,
                       "frames": len(frames)},
              "job": {"path": str(job_dir / JOB_FILE), "sha256": forge_core.sha256_file(job_dir / JOB_FILE),
                      "action": job.get("action"), "actionKind": job.get("actionKind")},
              "sprite": {"path": "sprite.png", "size": [w, h], "colours": int(len(palette)), "keyTintedCells": int(tinted),
                         "sha256": forge_core.sha256_file(out / "sprite.png")},
              "scale": scale, "canvas": [cols, rows], "spriteAt": [left, top], "anchor": [ax + left, ay + top],
              "key": [int(round(float(v))) for v in key], "keyColor": job.get("keyColor"),
              "lattice": "fitted per frame" if o.refit else "the first frame's for every frame",
              "keyMixedEdgeCells": "kept" if o.raw_edges else "dropped",
              "firstFrameDiffers": differs, "movingSpan": [int(span[0]), int(span[1])], "cycle": cycle,
              "notFlatShare": {"mean": round(float(np.mean(not_flat)), 4), "worst": round(float(max(not_flat)), 4),
                               "worstFrame": int(np.argmax(not_flat))},
              "framesTouchingEdge": touching, "warnings": warnings, "frames": records}
    _write_json(out / FRAMES_FILE, record)
    ys, xs = np.where(np.any([frame[..., 3] > 0 for frame in frames], axis=0))     # the sheet shows what the figure uses
    used = [frame[max(0, ys.min() - 1):ys.max() + 2, max(0, xs.min() - 1):xs.max() + 2] for frame in frames]
    zoom = max(1, min(6, 2000 // (SHEET_COLUMNS * used[0].shape[1])))
    contact_sheet(used, range(len(frames)), zoom, SHEET_COLUMNS).save(out / "frames-sheet.png")
    return {"status": "ok", "output": str(out), "metadata": str(out / FRAMES_FILE), "frames": len(frames), "fps": fps,
            "canvas": [cols, rows], "spriteAt": [left, top], "scale": scale, "firstFrameDiffers": differs,
            "cycle": {key_: cycle[key_] for key_ in ("period", "start", "closure")}, "movingSpan": record["movingSpan"],
            "sheet": str(out / "frames-sheet.png"), "warnings": warnings}


# --------------------------------------------------------------------------- finish

def load_frames(folder: Path) -> tuple[dict, list[np.ndarray], np.ndarray]:
    record_path = folder / FRAMES_FILE
    if not record_path.is_file():
        raise MotionError(f"{folder} holds no {FRAMES_FILE} (the output folder of pixel_motion.py frames)")
    record = forge_core.read_json(record_path)
    if not isinstance(record, dict) or record.get("schema") != FRAMES_SCHEMA:
        raise MotionError(f"{record_path.name} is not a {FRAMES_SCHEMA}")
    files = sorted((folder / "frames").glob("frame_*.png"))
    if len(files) != record["clip"]["frames"]:
        raise MotionError(f"{folder / 'frames'} holds {len(files)} frames, the record says {record['clip']['frames']}")
    frames = [np.array(Image.open(f).convert("RGBA")) for f in files]
    sprite = np.array(Image.open(folder / record["sprite"]["path"]).convert("RGBA"))
    return record, frames, sprite


def _fps(text: str) -> float:
    num, _, den = str(text).partition("/")
    return float(num) / float(den or 1)


def choose_frames(o: argparse.Namespace, record: dict, frames: Sequence[np.ndarray]) -> tuple[list[int], dict]:
    """The video frames of the drawings and how they were chosen."""
    count = len(frames)
    if o.pick:
        indices = [int(v) for v in o.pick.split(",")]
        if any(not 0 <= i < count for i in indices):
            raise MotionError(f"--pick names a frame outside 0..{count - 1}")
        return indices, {"mode": "named"}
    kind = record["job"].get("actionKind")
    mode = o.mode if o.mode != "auto" else ("action" if (o.first is not None or o.last is not None or kind != "loop") else "cycle")
    if mode == "cycle":
        cycle = pg.find_cycle(frames, o.shortest, o.longest) if (o.period is None or o.start is None) else None
        period = o.period if o.period is not None else cycle["period"]
        if not period:
            raise MotionError("no loop was found in the clip; name the frames with --pick, or use --mode action")
        if o.start is not None:
            start = o.start
        elif o.period is not None:       # the given period's best closure
            first, last = pg.moving_span(frames)
            starts = [(pg.frame_distance(frames[i], frames[i + period]), i) for i in range(first, max(first + 1, last - period))
                      if i + period < count]
            if not starts:
                raise MotionError(f"a period of {period} frames does not fit the clip's moving part")
            start = min(starts)[1]
        else:
            start = cycle["start"]
        n = o.count or 8
        if n > period:
            raise MotionError(f"--count {n} is more than the cycle's {period} video frames")
        indices = pg.pick_cycle(start, period, n)
        if indices[-1] >= count:
            raise MotionError(f"a cycle of {period} frames from frame {start} runs past the clip's end")
        how = {"mode": "cycle", "period": int(period), "start": int(start),
               "closure": int(pg.frame_distance(frames[start], frames[min(count - 1, start + period)])),
               "cycleSeconds": round(period / _fps(record["clip"]["fps"]), 3)}
        return indices, how
    if (o.first is not None and not 0 <= o.first < count) or (o.last is not None and not 0 <= o.last < count):
        raise MotionError(f"--from and --to are frames 0..{count - 1}")
    indices = pg.pick_motion(frames, o.count or 6, o.first, o.last)
    return indices, {"mode": "action", "from": indices[0], "to": indices[-1]}


def durations_of(o: argparse.Namespace, n: int, how: dict, fps: float) -> list[int]:
    if o.ms_list:
        values = [int(v) for v in o.ms_list.split(",")]
        if len(values) != n or min(values) < 1:
            raise MotionError(f"--ms-list needs {n} whole durations of at least 1 ms")
        return values
    if o.total_ms is not None:
        try:
            return forge_core.frame_durations(o.total_ms, n)
        except ValueError as error:
            raise MotionError(str(error)) from None
    if o.ms is not None:
        if o.ms < 1:
            raise MotionError("--ms is at least 1")
        return [o.ms] * n
    if how["mode"] == "cycle":
        return forge_core.frame_durations(max(n, forge_core.round_half_up(1000.0 * how["period"] / fps)), n)
    return [ONESHOT_MS] * n


def events_of(o: argparse.Namespace, total_ms: int) -> list[dict]:
    events = []
    for item in o.event or ():
        name, sep, at = item.partition("=")
        if not sep or not name or not at.isdigit():
            raise MotionError(f"--event is NAME=MS (whole ms), not {item!r}")
        if int(at) > total_ms:
            raise MotionError(f"the event {name} at {at} ms lies after the clip's {total_ms} ms")
        events.append({"name": name, "atMs": int(at)})
    return events


def clean_frame(frame: np.ndarray, sprite: np.ndarray, at: Sequence[int], *, head: np.ndarray | None,
                line: Sequence[int] | None, closed: bool) -> dict:
    """One picked frame made ready (in place): the head lock, islands, single holes, the closed outline."""
    report: dict[str, Any] = {}
    if head is not None and head.any():
        report["head"] = pg.head_lock(frame, sprite, head, at, line=line)
    report["islandCellsRemoved"] = pg.remove_islands(frame, 8)
    report["holesFilled"] = pg.fill_single_holes(frame)
    report["edgeCellsMadeLine"] = pg.close_outline(frame, line) if closed and line is not None else 0
    report["cells"] = int((frame[..., 3] > 0).sum())
    return report


def cmd_finish(o: argparse.Namespace) -> dict:
    folder = Path(o.frames_dir)
    record, frames, sprite = load_frames(folder)
    fps = _fps(record["clip"]["fps"])
    indices, how = choose_frames(o, record, frames)
    durations = durations_of(o, len(indices), how, fps)
    events = events_of(o, sum(durations))
    kind = record["job"].get("actionKind")
    loop = o.loop or ("cycle" if (how["mode"] == "cycle" or kind == "loop") else "oneshot")
    line = _rgb(o.line) if o.line else None
    if o.closed_outline and line is None:
        line = pg.outline_colour(sprite)
    head = None
    if o.head_rows:
        if not 0 < o.head_rows < sprite.shape[0]:
            raise MotionError(f"--head-rows is 1..{sprite.shape[0] - 1} rows of the sprite")
        head = np.zeros(sprite.shape[:2], dtype=bool)
        head[:o.head_rows] = sprite[:o.head_rows, :, 3] > 0
    elif o.head_box:
        try:
            bx0, by0, bx1, by1 = (int(v) for v in o.head_box.split(","))
        except ValueError:
            raise MotionError("--head-box is X0,Y0,X1,Y1 in sprite pixels") from None
        if not (0 <= bx0 < bx1 <= sprite.shape[1] and 0 <= by0 < by1 <= sprite.shape[0]):
            raise MotionError(f"--head-box lies outside the {sprite.shape[1]}x{sprite.shape[0]} sprite")
        head = np.zeros(sprite.shape[:2], dtype=bool)
        head[by0:by1, bx0:bx1] = sprite[by0:by1, bx0:bx1, 3] > 0
    at = record["spriteAt"]
    picked, reports = [], []
    closed = bool(o.closed_outline or o.line)
    for n, index in enumerate(indices):
        # a drawing whose head cannot be locked is a boiled face: the nearest video frame whose head can be takes
        # its place (not among named frames: those are the caller's choice)
        near = [index + step for step in (1, -1, 2, -2)] if head is not None and not o.pick else []
        for candidate in [index] + [i for i in near if 0 <= i < len(frames) and i not in indices]:
            frame = frames[candidate].copy()
            report = clean_frame(frame, sprite, at, head=head, line=line, closed=closed)
            if "head" not in report or report["head"]["locked"]:
                break
        else:
            candidate, frame = index, frames[index].copy()
            report = clean_frame(frame, sprite, at, head=head, line=line, closed=closed)
        report["videoFrame"] = int(candidate)
        if candidate != index:
            report["insteadOf"] = int(index)
            indices[n] = candidate
        picked.append(frame)
        reports.append(report)
    ax, ay = (float(v) for v in record["anchor"])
    cols, rows = record["canvas"]
    if o.flip:
        picked = [np.ascontiguousarray(frame[:, ::-1]) for frame in picked]
        ax = cols - ax
    rest = np.zeros((rows, cols, 4), dtype=np.uint8)            # the sprite where it stands, for the review picture
    rest[at[1]:at[1] + sprite.shape[0], at[0]:at[0] + sprite.shape[1]] = sprite
    if o.flip:
        rest = np.ascontiguousarray(rest[:, ::-1])
    trimmed = None
    if not o.no_trim:
        opaque = np.any([frame[..., 3] > 0 for frame in picked], axis=0)
        ys, xs = np.where(opaque)
        if len(xs) == 0:
            raise MotionError("the picked frames are empty")
        x0 = max(0, min(int(xs.min()) - 1, math.floor(ax)))
        y0 = max(0, min(int(ys.min()) - 1, math.floor(ay)))
        x1 = min(cols, max(int(xs.max()) + 2, math.ceil(ax)))
        y1 = min(rows, max(int(ys.max()) + 2, math.ceil(ay)))
        picked = [np.ascontiguousarray(frame[y0:y1, x0:x1]) for frame in picked]
        rest = np.ascontiguousarray(rest[y0:y1, x0:x1])
        ax, ay, cols, rows = ax - x0, ay - y0, x1 - x0, y1 - y0
        trimmed = [x0, y0, x1, y1]
    out = _new_dir(o.output_dir)
    locks = [r["head"] for r in reports if "head" in r]
    fields = {"frames": {"path": str(folder), "sha256": forge_core.sha256_file(folder / FRAMES_FILE)},
              "picked": how, "videoFrames": [int(i) for i in indices], "flipped": bool(o.flip), "trimmedTo": trimmed,
              "headRows": o.head_rows or None, "headBox": o.head_box or None, "line": _hex(line) if line is not None else None,
              "closedOutline": bool(o.closed_outline or o.line), "clean": reports}
    package = write_set(out, o.name, picked, durations, loop, events, (ax, ay), rest=rest,
                        labels=[f"{i}: {d} ms" for i, d in zip(indices, durations)],
                        method=f"pixel_motion {how['mode']}: video frames {','.join(str(i) for i in indices)}",
                        key=record.get("keyColor") or "magenta", art_source="video", package=not o.no_package, fields=fields)
    return {"status": "ok", "output": str(out), "metadata": str(out / "motion.json"), "name": o.name, "frames": len(picked),
            "videoFrames": [int(i) for i in indices], "durationsMs": durations, "loopPolicy": loop,
            "frameSize": [int(cols), int(rows)], "anchor": [ax, ay], "picked": how,
            "headLocked": [bool(lock["locked"]) for lock in locks] if locks else None,
            "review": str(out / "review.png"), "package": str(out / "package") if package else None}


def write_set(out: Path, name: str, drawings: Sequence[np.ndarray], durations: Sequence[int], loop: str,
              events: Sequence[dict], anchor: Sequence[float], *, rest: np.ndarray, labels: Sequence[str], method: str,
              key: str, art_source: str, package: bool, fields: dict) -> dict | None:
    """A motion set written into the new folder ``out``: clean/frame_000.png ..., strip.png, selection.json
    (forge-frame-selection/v2), review.png (``rest``, the sprite where it stands, then every drawing with its
    label), package/ (engine_export.py package, unless ``package`` is false) and motion.json (``fields`` and what
    every set has: the durations, the loop policy, the events, the frame size and the anchor, which is the point
    of the feet in every drawing). Returns the package's result or None."""
    rows, cols = drawings[0].shape[:2]
    ax, ay = (float(v) for v in anchor)
    durations = [int(d) for d in durations]
    (out / "clean").mkdir()
    for n, frame in enumerate(drawings):
        Image.fromarray(frame, "RGBA").save(out / "clean" / f"frame_{n:03d}.png")
    strip = Image.new("RGBA", (cols * len(drawings), rows), (0, 0, 0, 0))
    for n, frame in enumerate(drawings):
        strip.paste(Image.fromarray(frame, "RGBA"), (n * cols, 0))
    strip.save(out / "strip.png")
    hashes = [forge_core.sha256_file(out / "clean" / f"frame_{n:03d}.png") for n in range(len(drawings))]
    selection = {"schema": SELECTION_SCHEMA, "sourceDirectory": "clean", "sourceHashes": hashes, "start": 0,
                 "endExclusive": len(drawings), "sourceIndices": list(range(len(drawings))), "durations_ms": durations,
                 "loopPolicy": loop, "events": list(events), "status": "selected", "method": method}
    _write_json(out / "selection.json", selection)
    per_row = min(len(drawings) + 1, REVIEW_COLUMNS)
    zoom = max(1, min(8, 1800 // (cols * per_row), 480 // rows))
    contact_sheet([rest] + list(drawings), ["sprite"] + list(labels), zoom, per_row).save(out / "review.png")
    motion = {"schema": MOTION_SCHEMA, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION}, "name": name}
    motion.update(fields)
    motion.update({"durationsMs": durations, "totalMs": int(sum(durations)), "loopPolicy": loop, "events": list(events),
                   "frameSize": [int(cols), int(rows)], "anchor": [ax, ay], "strip": "strip.png",
                   "differenceToNext": [pg.frame_distance(drawings[i], drawings[(i + 1) % len(drawings)])
                                        for i in range(len(drawings))]})
    result = None
    if package:
        argv = [sys.executable, str(HERE / "engine_export.py"), "package", "--clean-dir", str(out / "clean"),
                "--output-dir", str(out / "package"), "--name", name, "--selection", str(out / "selection.json"),
                "--source-size", f"{cols},{rows}", "--source-anchor", f"{ax:g},{ay:g}", "--pixel-art", "--sampling", "nearest",
                "--resampler", "nearest", "--formats", "png", "--art-source", art_source, "--key", str(key)]
        try:
            result = _run_json(argv)
        except MotionError as error:
            raise MotionError(f"{error} (nothing was written; pass --no-package to get the cleaned drawings without a "
                              "package and look at them)") from None
        motion["package"] = {"path": "package", "manifest": "package/animation.json", "qa": result.get("status")}
    _write_json(out / "motion.json", motion)
    return result


# --------------------------------------------------------------------------- measure (a walk, before it is picked)

NEARLY_SAME = 0.3         # a frame that changes less than this share of a typical step only repeats the frame before
ON_TWOS = 0.35            # this share of repeating frames says the clip is drawn on twos
BODY_REACH = 0.12         # of the sprite's width: the columns over the feet that are the body (a staff beside it is not)
FEET_REACH = 0.6          # of the sprite's width: how far from the feet's middle a foot is looked for
PINNED = 0.4              # an edge of the feet that stands at one place for this share of the drawings is no foot


def _feet_of(record: dict, sprite: np.ndarray) -> tuple[float, int]:
    """The feet's middle (a column) and their row on the frames' canvas, from the sprite's own feet."""
    middle, row = pg.boots(sprite)
    return record["spriteAt"][0] + middle, int(record["spriteAt"][1] + row)


def _ground_runs(opaque: np.ndarray, y: int, x0: int, x1: int) -> list[tuple[int, int]]:
    """The runs of columns (first, last) between x0 and x1 that hold a cell in the ground rows: the two rows above
    the feet's row, that row and the one below."""
    band = opaque[max(0, y - 2):y + 2, x0:x1 + 1].any(axis=0)
    runs, start = [], None
    for i, on in enumerate(list(band) + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append((x0 + start, x0 + i - 1))
            start = None
    return runs


def _is_pole(opaque: np.ndarray, run: Sequence[int], y: int, need: int) -> bool:
    """Is a run of the ground rows the foot of a pole (a staff, a spear set on the ground)? It is at most four
    cells wide and stays thin for ``need`` rows above the ground rows; a leg joins the body or a robe sooner."""
    a, b = run
    if b - a + 1 > 4:
        return False
    rows, width = 0, opaque.shape[1]
    for r in range(max(0, y - 2) - 1, -1, -1):
        if not opaque[r, a:b + 1].any():
            break
        lo, hi = a, b
        while lo > 0 and opaque[r, lo - 1]:
            lo -= 1
        while hi < width - 1 and opaque[r, hi + 1]:
            hi += 1
        if hi - lo + 1 > 5:
            break
        rows += 1
    return rows >= need


def feet_series(frames: Sequence[np.ndarray], feet: Sequence[float], width: int, height: int | None = None) -> np.ndarray:
    """Per frame: the body's height over the feet's row, and the left and the right edge of what stands on the
    ground (cells from the feet's middle), read in the two rows above the feet's row, that row and the one below.
    A pole that is set on the ground (a staff) is no foot and is left out, unless nothing else stands there."""
    x, y = int(round(feet[0])), int(feet[1])
    body, reach = max(2, int(round(BODY_REACH * width))), max(8, int(round(FEET_REACH * width)))
    need = max(6, int(round(0.12 * (height or 50))))
    out = np.zeros((len(frames), 3), dtype=np.int32)
    for n, frame in enumerate(frames):
        opaque = frame[..., 3] > 0
        rows = np.where(opaque[:, max(0, x - body):x + body + 1].any(axis=1))[0]
        out[n, 0] = int(y - rows.min() + 1) if len(rows) else 0
        runs = _ground_runs(opaque, y, max(0, x - reach), min(opaque.shape[1] - 1, x + reach))
        stands = [run for run in runs if not _is_pole(opaque, run, y, need)] or runs
        if stands:
            out[n, 1], out[n, 2] = stands[0][0] - x, stands[-1][1] - x
    return out


def facing_of(series: np.ndarray) -> str:
    """Which way a walk in place faces: the standing foot travels back slowly and the front edge jumps forward at
    a footfall, so the many small steps of the feet's edges point away from the facing."""
    small = np.concatenate([np.diff(series[:, 1]), np.diff(series[:, 2])])
    small = small[(small != 0) & (np.abs(small) <= 3)]
    return "right" if (small < 0).sum() >= (small > 0).sum() else "left"


def _pinned(edge: np.ndarray) -> bool:
    """Does an edge of the feet stand still most of the time (a blade's tip that drags on the ground)?"""
    if not len(edge):
        return True
    values, counts = np.unique(np.round(edge), return_counts=True)
    most = values[counts.argmax()]
    return float((np.abs(edge - most) <= 1).mean()) > PINNED


def steps_by_lags(frames: Sequence[np.ndarray], first: int, last: int, shortest: int = 6, longest: int = 16) -> tuple[int, int]:
    """(video frames a step, video frames a pair of steps) from the lags at which the moving part of a clip is most
    like itself; (0, 0) when the clip is too short."""
    lo, hi = first + 4, last - 4

    def cost(lag: int) -> float:
        pairs = [pg.frame_distance(frames[k], frames[k + lag]) for k in range(lo, hi - lag)]
        return float(np.mean(pairs)) if pairs else float("inf")

    costs = {lag: cost(lag) for lag in range(shortest, longest + 1)}
    if not costs or min(costs.values()) == float("inf"):
        return 0, 0
    step = min(costs, key=lambda lag: (costs[lag], lag))
    wide = {lag: cost(lag) for lag in range(int(round(1.6 * step)), int(round(2.4 * step)) + 1)}
    pair = min(wide, key=lambda lag: (wide[lag], abs(lag - 2 * step)))
    if wide[pair] == float("inf"):
        return step, 0
    return step, pair


def measure_walk(frames: Sequence[np.ndarray], feet: Sequence[float], width: int, fps: float, *, facing: str | None = None,
                 per_step: int = 4, height: int | None = None) -> dict:
    """What a walk clip's frames say before any is picked (see the module's text)."""
    count = len(frames)
    series = feet_series(frames, feet, width, height)
    facing = facing or facing_of(series)
    front = (series[:, 2] if facing == "right" else -series[:, 1]).astype(np.float64)
    back = (series[:, 1] if facing == "right" else -series[:, 2]).astype(np.float64)
    steps = [pg.frame_distance(frames[i], frames[i + 1]) for i in range(count - 1)]
    first, last = pg.moving_span(frames)
    typical = float(np.percentile(steps[first:last], 75)) if last > first else 0.0
    repeats = {i + 1 for i in range(first, last) if steps[i] < NEARLY_SAME * typical}
    kept = [k for k in range(first, last + 1) if k not in repeats]          # the drawings: a clip on twos shows each twice
    lag_step, lag_pair = steps_by_lags(frames, first, last) if typical > 0 else (0, 0)      # a clip that stands still has no step
    # the footfalls: where the front edge of the feet is farthest forward. When something pins that edge (a blade's
    # tip that drags in front), the toe-offs: where the back edge is farthest back
    if not _pinned(front[kept]):
        marks, edge, sign = "footfalls (the front edge of the feet)", front[kept], 1.0
    elif not _pinned(back[kept]):
        marks, edge, sign = "toe-offs (the back edge of the feet)", back[kept], -1.0
    else:
        marks, edge, sign = "none (both edges of the feet stand still)", front[kept], 1.0
    peak = sign * edge
    high = float(np.percentile(peak, 75)) if len(kept) > 1 else 0.0
    falls = [k for j, k in enumerate(kept[:-1])
             if peak[j] >= peak[max(0, j - 1)] and peak[j] > peak[j + 1] and peak[j] >= high
             and peak[j] >= peak[max(0, j - 4):j + 5].max()] if typical > 0 and not marks.startswith("none") else []
    gaps = [b - a for a, b in zip(falls, falls[1:])]
    stride = float(np.median([front[k] - back[k] for k in falls])) if falls else 0.0
    # how fast the standing foot travels back: that edge over two drawings, where it falls steadily (a footfall
    # throws it forward and a foot that rolls makes it stand still for a drawing; the median leaves both out)
    moving = front if sign > 0 else back
    travel = [(moving[a] - moving[b]) / float(b - a) for a, b in zip(kept, kept[2:])
              if 0 < moving[a] - moving[b] <= max(8.0, 0.5 * stride)]
    per_frame = float(np.median(travel)) if travel else 0.0
    even = bool(len(gaps) >= 2 and float(np.std(gaps)) <= 0.2 * float(np.mean(gaps))
                and (not lag_step or abs(float(np.median(gaps)) - lag_step) <= 0.25 * lag_step))
    step = float(np.median(gaps)) if even else (float(lag_step) if lag_step else None)
    result = {"facing": facing, "frames": count, "fps": round(fps, 3), "movingSpan": [int(first), int(last)],
              "marks": marks, "footfalls": [int(k) for k in falls], "framesBetweenFootfalls": [int(g) for g in gaps],
              "evenSteps": even, "stepByLags": [int(lag_step), int(lag_pair)],
              "stepFrames": step, "stepSeconds": round(step / fps, 3) if step else None,
              "strideCells": round(stride, 1), "repeatingFrames": len(repeats),
              "onTwos": bool(len(repeats) > ON_TWOS * max(1, last - first)),
              "cellsBackPerFrame": round(per_frame, 2), "speedPxPerSecond": round(per_frame * fps, 1),
              "series": {"bodyHeight": [int(v) for v in series[:, 0]], "back": [int(v) for v in back],
                         "front": [int(v) for v in front]},
              "stepPair": None, "pick": None, "msList": None, "warnings": []}
    pairs = [(pg.frame_distance(frames[a], frames[b]), a, b) for a, b in zip(falls, falls[2:])] if even else []
    if pairs:
        # of the pairs that close about as well as the best one: the pair whose two steps are most alike in length
        # (an even rhythm), and of those the latest (a walk has settled by then)
        least = min(pair[0] for pair in pairs)
        middle = {a: b for a, b in zip(falls, falls[1:])}
        closure, start, end = min((pair for pair in pairs if pair[0] <= 1.15 * least),
                                  key=lambda pair: (abs((middle[pair[1]] - pair[1]) - (pair[2] - middle[pair[1]])), -pair[1]))
        how = "from a footfall to the footfall after the next"
    elif lag_pair:
        # no steady marks: the stretch of a pair of steps' length (as the lags say) that closes best, at its start and
        # one frame on; the latest of those that are as good
        tried = [((pg.frame_distance(frames[s], frames[s + p]) + pg.frame_distance(frames[s + 1], frames[s + p + 1])) / 2.0, -s, s, s + p)
                 for p in (lag_pair - 1, lag_pair, lag_pair + 1) for s in range(first + 2, last - p - 2)]
        if not tried:
            tried = [(float(pg.frame_distance(frames[first], frames[min(count - 1, first + lag_pair)])), -first, first,
                      min(count - 1, first + lag_pair))]
        closure, _, start, end = min(tried)
        how = "the stretch of two steps' length that closes best (the feet's edges give no steady marks)"
        result["warnings"].append("the feet's edges give no steady footfalls (a robe, a pole or a blade on the ground hides "
                                  "them): the step is read from the lags at which the clip is like itself; look at legs.png")
    else:
        result["warnings"].append("no step was found: this is no steady walk in place; look at the frames sheet and name the "
                                  "frames yourself")
        return result
    distinct = [k for k in kept if start <= k < end]
    want = 2 * per_step
    if len(distinct) < want:
        result["warnings"].append(f"the pair of steps holds only {len(distinct)} different drawings, fewer than {want}: "
                                  "all of them are taken")
        picks = distinct
    else:
        picks = [distinct[int(k * len(distinct) / want)] for k in range(want)]
    holds = [b - a for a, b in zip(picks, picks[1:] + [end])]
    result.update({"stepPair": {"from": int(start), "to": int(end), "cellsThatDiffer": int(round(closure)), "how": how},
                   "pick": [int(k) for k in picks],
                   "msList": [max(1, forge_core.round_half_up(1000.0 * hold / fps)) for hold in holds]})
    heights = series[picks, 0]
    if int(heights.max() - heights.min()) > 3:
        result["warnings"].append(f"the body's height changes by {int(heights.max() - heights.min())} cells within the "
                                  "pair of steps: a hop, not a walk; look at legs.png")
    return result


def legs_picture(frames: Sequence[np.ndarray], numbers: Sequence[int], feet: Sequence[float], width: int, height: int,
                 per_row: int = 4) -> Image.Image:
    """The lower body of the named frames, enlarged, each with its number and the ground line."""
    x, y = int(round(feet[0])), int(feet[1])
    reach = max(8, int(round(FEET_REACH * width))) + 2
    above = max(8, int(round(0.55 * height)))
    cw, ch = 2 * reach + 1, above + 3
    per_row = max(1, min(per_row, len(numbers)))
    zoom = max(2, min(12, 2000 // (per_row * cw)))
    rows = math.ceil(len(numbers) / per_row)
    page = Image.new("RGB", (per_row * cw * zoom, rows * (ch * zoom + 16)), (128, 128, 136))
    draw = ImageDraw.Draw(page)
    for n, number in enumerate(numbers):
        frame = frames[number]
        part = np.zeros((ch, cw, 4), dtype=np.uint8)
        x0, y0 = x - reach, y - above
        sx0, sy0 = max(0, x0), max(0, y0)
        sx1, sy1 = min(frame.shape[1], x0 + cw), min(frame.shape[0], y0 + ch)
        if sx1 > sx0 and sy1 > sy0:
            part[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = frame[sy0:sy1, sx0:sx1]
        big = Image.fromarray(part, "RGBA").resize((cw * zoom, ch * zoom), Image.NEAREST)
        px, py = (n % per_row) * cw * zoom, (n // per_row) * (ch * zoom + 16)
        page.paste(big, (px, py + 16), big)
        draw.text((px + 4, py + 2), f"frame {number}", fill=(255, 255, 255))
        ground = py + 16 + (above + 1) * zoom
        draw.line([(px, ground), (px + cw * zoom - 1, ground)], fill=(255, 255, 255))
    return page


def cmd_measure(o: argparse.Namespace) -> dict:
    folder = Path(o.frames_dir)
    record, frames, sprite = load_frames(folder)
    fps = _fps(record["clip"]["fps"])
    feet = _feet_of(record, sprite)
    height, width = sprite.shape[:2]
    if o.per_step < 2:
        raise MotionError("--per-step is at least 2 drawings a step")
    found = measure_walk(frames, feet, width, fps, facing=o.facing, per_step=o.per_step, height=height)
    if o.show:
        try:
            shown = [int(v) for v in o.show.split(",")]
        except ValueError:
            raise MotionError("--show is video frames by number, a,b,c,...") from None
        if any(not 0 <= k < len(frames) for k in shown):
            raise MotionError(f"--show names a frame outside 0..{len(frames) - 1}")
    else:
        shown = found["pick"] or found["footfalls"] or list(range(0, len(frames), max(1, len(frames) // 8)))[:8]
    out = _new_dir(o.output_dir)
    legs_picture(frames, shown, feet, width, height).save(out / "legs.png")
    finish = None
    if found["pick"]:
        finish = (f"--pick {','.join(str(k) for k in found['pick'])} --ms-list {','.join(str(v) for v in found['msList'])} "
                  "--loop cycle")
    report = {"schema": WALK_SCHEMA, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
              "frames": {"path": str(folder), "sha256": forge_core.sha256_file(folder / FRAMES_FILE)},
              "feet": {"middle": feet[0], "row": feet[1]}, "drawingsAStep": o.per_step, "legs": "legs.png",
              "legsShow": [int(k) for k in shown], "finish": finish,
              "look": "legs.png: the leg in front at one footfall must be the leg behind at the next. Legs that only "
                      "open and close, or a hop, cannot be mended by picking: make the walk from a side view."}
    report.update(found)
    _write_json(out / "measure.json", report)
    return {"status": "ok", "output": str(out), "metadata": str(out / "measure.json"), "legs": str(out / "legs.png"),
            "facing": found["facing"], "marks": found["marks"], "footfalls": found["footfalls"], "stepFrames": found["stepFrames"],
            "stepByLags": found["stepByLags"],
            "onTwos": found["onTwos"], "repeatingFrames": found["repeatingFrames"],
            "speedPxPerSecond": found["speedPxPerSecond"], "stepPair": found["stepPair"], "pick": found["pick"],
            "msList": found["msList"], "finish": finish, "warnings": found["warnings"]}


# --------------------------------------------------------------------------- breath (an idle made from the sprite)

LOWER_LEGS = (0.72, 0.88)     # of the height over the feet: the rows among which one is taken out
BREATH_MS = (620, 520)        # how long the sprite stands, and how long it is one cell lower


def breath_drawing(sprite: np.ndarray, rows: Sequence[int] | None = None) -> tuple[np.ndarray, dict]:
    """The sprite with its upper part one cell lower. One row of the lower legs is taken out of what stands on the
    feet (the row most like the row under it, so that taking it out shows least); everything above it comes down
    one cell; a part that reaches below that row without standing on the feet (a blade that hangs from the hand)
    comes down whole, and what would leave the sprite's last row is cut. Returns the drawing (the sprite's size)
    and {"rowTakenOut", "cost", "cellsThatChange"}."""
    sprite = np.asarray(sprite, np.uint8)
    opaque = sprite[..., 3] > 0
    height = sprite.shape[0]
    runs, base = pg.feet_runs(sprite)
    lo, hi = rows if rows else (int(round(LOWER_LEGS[0] * (base + 1))), int(round(LOWER_LEGS[1] * (base + 1))))
    lo, hi = max(1, lo), min(base, hi)
    if lo >= hi:
        raise MotionError(f"there is no row to take out between rows {lo} and {hi}: the sprite is too small for a "
                          "breath of one cell, or --rows names no row above the feet")
    feet = np.zeros_like(opaque)
    for x0, x1 in runs:
        feet[max(0, base - 1):base + 1, x0:x1 + 1] = opaque[max(0, base - 1):base + 1, x0:x1 + 1]
    best = None
    for row in range(lo, hi):
        below = opaque.copy()
        below[:row] = False
        stands = np.zeros_like(opaque)
        for group in pg.components(below):
            cells = np.asarray(group)
            if feet[cells[:, 0], cells[:, 1]].any():
                stands[cells[:, 0], cells[:, 1]] = True
        if not stands[row].any():
            continue
        cost = int((stands[row] & (sprite[row] != sprite[row + 1]).any(axis=1)).sum())
        if best is None or cost < best[0]:
            best = (cost, row, stands)
    if best is None:
        raise MotionError("no row of the lower legs stands on the feet: name the rows with --rows")
    cost, row, stands = best
    down = np.zeros_like(sprite)
    stays = stands.copy()
    stays[:row + 1] = False
    down[stays] = sprite[stays]
    moves = opaque & ~stands
    moves[:row] = opaque[:row]
    moves[height - 1] = False                      # nothing goes under the sprite's last row
    ys, xs = np.where(moves)
    down[ys + 1, xs] = sprite[ys, xs]
    return down, {"rowTakenOut": int(row), "cost": cost, "cellsThatChange": int((down != sprite).any(axis=2).sum())}


def cmd_breath(o: argparse.Namespace) -> dict:
    path = Path(o.sprite)
    if not path.is_file():
        raise MotionError(f"{path} does not exist")
    try:
        sprite = np.asarray(Image.open(path).convert("RGBA")).copy()
    except OSError as error:
        raise MotionError(f"cannot read {path.name}: {error}") from None
    sprite[..., 3] = np.where(sprite[..., 3] > 127, 255, 0)
    if not (sprite[..., 3] > 0).any():
        raise MotionError(f"{path.name} is empty")
    sprite = np.ascontiguousarray(pg.crop_to_figure(sprite))
    rows = None
    if o.rows:
        try:
            rows = tuple(int(v) for v in o.rows.split(","))
        except ValueError:
            rows = ()
        if len(rows) != 2:
            raise MotionError("--rows is A,B: the sprite's rows among which one is taken out (A up to, not including, B)")
    if o.ms_list:
        try:
            durations = [int(v) for v in o.ms_list.split(",")]
        except ValueError:
            durations = []
        if len(durations) != 2 or min(durations) < 1:
            raise MotionError("--ms-list is two whole durations in ms: the sprite, then the breath")
    else:
        durations = list(BREATH_MS)
    down, report = breath_drawing(sprite, rows)
    middle, base = pg.boots(sprite)
    drawings = [np.pad(drawing, ((1, 1), (1, 1), (0, 0))) for drawing in (sprite, down)]     # one empty cell around, as finish leaves
    anchor = (middle + 0.5 + 1, base + 1 + 1)
    key = o.key
    if key is None:                                 # the record pixel_look.py wrote beside the sprite names its key colour
        beside = path.with_suffix(".json")
        try:
            key = str(json.loads(beside.read_text(encoding="utf-8")).get("key") or "auto") if beside.is_file() else "auto"
        except (ValueError, OSError, AttributeError):
            key = "auto"
    out = _new_dir(o.output_dir)
    fields = {"frames": None, "picked": dict({"mode": "breath"}, **report), "videoFrames": [], "flipped": False, "trimmedTo": None,
              "madeFrom": {"sprite": str(path), "sha256": forge_core.sha256_file(path)}}
    package = write_set(out, o.name, drawings, durations, "cycle", [], anchor, rest=drawings[0],
                        labels=[f"sprite: {durations[0]} ms", f"breath: {durations[1]} ms"],
                        method=f"pixel_motion breath: row {report['rowTakenOut']} taken out", key=key, art_source="mixed",
                        package=not o.no_package, fields=fields)
    return {"status": "ok", "output": str(out), "metadata": str(out / "motion.json"), "name": o.name, "frames": 2,
            "durationsMs": durations, "loopPolicy": "cycle", "frameSize": [int(sprite.shape[1]) + 2, int(sprite.shape[0]) + 2],
            "anchor": [anchor[0], anchor[1]], "rowTakenOut": report["rowTakenOut"], "cellsThatChange": report["cellsThatChange"],
            "review": str(out / "review.png"), "package": str(out / "package") if package else None}


# --------------------------------------------------------------------------- command line

def _staged(handler, options: argparse.Namespace) -> dict:
    """Run a command into a staging folder beside its --output-dir and publish that folder on a clean exit
    (forge_core.staged_output): a run that fails leaves nothing behind. The result names the published folder."""
    final = Path(options.output_dir)
    if os.path.lexists(final):
        raise MotionError(f"{final} exists already; name a new folder (nothing is ever replaced)")
    try:
        with forge_core.staged_output(final) as stage:
            options.output_dir = str(stage)
            result = handler(options)
    except FileExistsError:
        raise MotionError(f"{final} exists already; name a new folder (nothing is ever replaced)") from None
    published = str(final.parent.resolve() / final.name)
    return json.loads(json.dumps(result).replace(json.dumps(str(stage))[1:-1], json.dumps(published)[1:-1]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="the sprite on the provider canvas for one action, with the pixel paragraph")
    p.add_argument("--sprite", required=True, help="the 1x sprite (RGBA PNG, transparent around the figure)")
    p.add_argument("--action", required=True, help="the timeline template of prepare_i2v_input.py: idle, walk, attack ...")
    p.add_argument("--output-dir", "--out-dir", required=True, help="new job folder")
    p.add_argument("--subject", help="who the sprite shows, in a few words (goes into the prompt)")
    p.add_argument("--facing", help="facing direction: left, right, front, back or free text")
    p.add_argument("--extra", help="what the action is, in words; added after the pixel paragraph")
    p.add_argument("--scale", type=int, help="video px a sprite pixel (default: the largest at which the action's room fits)")
    p.add_argument("--anchor", help="the root X,Y in sprite pixels (default: the middle of the feet, on the ground line)")
    p.add_argument("--outline", default="rule", choices=("rule", "drawn"),
                   help="drawn: the sprite's look has no outline, and the prompt does not speak of one (default rule)")
    p.add_argument("--subject-height", type=float, help="the sprite's height as a share of the canvas height")
    p.add_argument("--canvas", help="provider canvas W,H (default 1280,720)")
    p.add_argument("--key", help="key colour: auto, magenta, green or blue (default auto: the one the sprite's own edge "
                                 "and colours lean to least)")
    p.add_argument("--duration", type=float, help="the take's length in seconds (default 6)")
    f = sub.add_parser("frames", help="every frame of the clip on the sprite's grid and palette")
    f.add_argument("--clip", required=True, help="the image-to-video clip made from the job's input.png")
    f.add_argument("--job", required=True, help="the job folder of prepare (or its registration_job.json)")
    f.add_argument("--output-dir", "--out-dir", required=True, help="new folder for the frames")
    f.add_argument("--room", help="L,T,R,B: sprite pixels of room around the sprite (default: the action's room)")
    f.add_argument("--refit", action="store_true", help="fit the lattice's shift on every frame (default: the first "
                                                         "frame's shift for the whole clip)")
    f.add_argument("--raw-edges", action="store_true", help="read the edge cells as they are (default: the key colour "
                                                             "that compression blends into them is unmixed)")
    e = sub.add_parser("finish", help="pick the drawings, clean them and write the clip")
    e.add_argument("--frames-dir", required=True, help="the output folder of frames")
    e.add_argument("--name", required=True, help="the clip's name: letters, digits, - and _")
    e.add_argument("--output-dir", "--out-dir", required=True, help="new folder for the clip")
    e.add_argument("--mode", default="auto", choices=("auto", "cycle", "action"),
                   help="cycle: one loop found in the clip; action: equal steps of movement between two frames "
                        "(default: cycle for a loop action, else action)")
    e.add_argument("--pick", help="video frames by number, a,b,c,...: overrides --mode")
    e.add_argument("--count", type=int, help="how many drawings (default 8 for a cycle, 6 for an action)")
    e.add_argument("--period", type=int, help="cycle: its length in video frames (default: found)")
    e.add_argument("--start", type=int, help="cycle: its first video frame (default: where it closes best)")
    e.add_argument("--shortest", type=int, default=10, help="cycle: the shortest period tried (default 10)")
    e.add_argument("--longest", type=int, default=46, help="cycle: the longest period tried (default 46)")
    e.add_argument("--from", dest="first", type=int, help="action: its first video frame (default: where the motion starts)")
    e.add_argument("--to", dest="last", type=int, help="action: its last video frame (default: where the motion ends)")
    e.add_argument("--ms", type=int, help="every drawing's duration in ms")
    e.add_argument("--ms-list", help="the drawings' durations in ms, a,b,c,...")
    e.add_argument("--total-ms", type=int, help="the clip's length in ms, split evenly")
    e.add_argument("--loop", choices=LOOPS, help="loop policy (default: cycle for a loop action, else oneshot)")
    e.add_argument("--event", action="append", help="NAME=MS: a gameplay event (hit, step_l, ...); repeat")
    e.add_argument("--head-rows", type=int, help="the sprite's first rows that are its head: the sprite's own head is put "
                                                 "back where at least 45 percent of it is already there (walks, idles)")
    e.add_argument("--head-box", help="X0,Y0,X1,Y1 (sprite pixels, half-open): the head as a box, when a weapon or a "
                                      "wing stands beside it in the same rows; instead of --head-rows")
    e.add_argument("--closed-outline", action="store_true", help="every cell that touches the outside becomes the line "
                                                                 "colour (a look with a closed outline)")
    e.add_argument("--line", help="the line colour #rrggbb (default: the sprite's own); implies --closed-outline")
    e.add_argument("--flip", action="store_true", help="mirror every drawing (a view that faces the other way)")
    e.add_argument("--no-trim", action="store_true", help="keep the whole room around the sprite (default: trim to the drawings)")
    e.add_argument("--no-package", action="store_true", help="do not run engine_export.py package")
    m = sub.add_parser("measure", help="a walk read before it is picked: footfalls, step, speed, the drawings to take")
    m.add_argument("--frames-dir", required=True, help="the output folder of frames")
    m.add_argument("--output-dir", "--out-dir", required=True, help="new folder for measure.json and legs.png")
    m.add_argument("--facing", choices=("left", "right"), help="which way the figure walks (default: read from the feet)")
    m.add_argument("--per-step", type=int, default=4, help="drawings a step (default 4: eight for a pair of steps)")
    m.add_argument("--show", help="video frames a,b,c,... for legs.png (default: the drawings it would take)")
    b = sub.add_parser("breath", help="an idle made from the sprite itself: its upper part one cell lower")
    b.add_argument("--sprite", required=True, help="the 1x sprite (RGBA PNG)")
    b.add_argument("--name", required=True, help="the clip's name: letters, digits, - and _")
    b.add_argument("--output-dir", "--out-dir", required=True, help="new folder for the clip")
    b.add_argument("--rows", help="A,B: the sprite's rows among which one is taken out (default: the lower legs)")
    b.add_argument("--ms-list", help=f"two durations in ms, the sprite then the breath (default {BREATH_MS[0]},{BREATH_MS[1]})")
    b.add_argument("--key", help="the key colour the sprite was made on, for the package's residue gate: magenta, green, "
                                 "blue, #rrggbb or none (default: the record beside the sprite, else auto)")
    b.add_argument("--no-package", action="store_true", help="do not run engine_export.py package")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    if getattr(options, "line", None) and not (len(options.line) == 7 and options.line.startswith("#")):
        parser.error("--line must be #rrggbb")
    if options.command == "finish" and sum(v is not None for v in (options.ms, options.ms_list, options.total_ms)) > 1:
        parser.error("give one of --ms, --ms-list and --total-ms")
    if options.command == "finish" and options.head_rows and options.head_box:
        parser.error("give one of --head-rows and --head-box")
    if options.command == "prepare" and options.anchor is not None:
        try:
            if len([float(v) for v in options.anchor.split(",")]) != 2:
                raise ValueError(options.anchor)
        except ValueError:
            parser.error("--anchor is X,Y in sprite pixels")
    try:
        handler = {"prepare": cmd_prepare, "frames": cmd_frames, "finish": cmd_finish, "measure": cmd_measure,
                   "breath": cmd_breath}[options.command]
        result = handler(options) if options.command == "prepare" else _staged(handler, options)   # prepare stages itself
    except MotionError as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
