#!/usr/bin/env python3
"""A character as a real 1x pixel sprite that follows a reference picture, and what is made from that sprite.

The look is never described in words. It comes from a picture: a sprite you like (cut from a style picture an
image model drew freely, or from your own art), or a sprite this tool made before. The words only fix the format.

  ask-master  Write prompt.txt and request.json for the picture of the first master of a look. --how is the
              picture that shows HOW to draw (the manner, the size of the pixels, the build): its figure is drawn
              again, once and large, on a flat key colour. Nothing is generated here: give prompt.txt and the
              reference to generate2dmedia/scripts/route_media.py image.
  master      The picture -> the master sprite: the picture's own grid is found and every cell gives one colour,
              what is left of the key colour on the edge goes, the outline is closed by rule, and the colours are
              brought down without losing a hue. A picture whose cells are under 8 px is refused: small soft
              cells give no edge (ask for ONE figure, large). Writes <name>.png (1x), <name>-how.png (the sprite
              on one exact grid: the --how of the next character), <name>-review.png (the picture beside the
              sprite), <name>-grounds.png (the sprite on a pale, a grey and a dark ground: look at its edge on
              all of them) and <name>.json.
  ask-cast    Every further character of the cast: the master stands at the left of a wide reference picture and
              the prompt says "THE ONLY CHANGE: in the empty area to the right, add a SECOND figure: the character
              of the SECOND image" (--who, a picture of the new character), on the same pixel grid, as tall as the
              first figure (or as --size says). A character drawn alone in the first one's manner comes back a
              tenth to a quarter taller, with smaller pixels; drawn beside the master it keeps the pixel size.
  cast        The picture that came back -> the new character's master: the first figure is the ruler (it gives the
              cell size), the second is read with that cell size on its own lattice, with its own colours, and
              its outline is closed in the first master's line colour. Writes the same files as master.
  ask-pose    A pose, a weapon, an expression: the master on an exact grid is the only reference and the prompt
              says "reproduce the attached image; THE ONLY CHANGE: ...". Writes reference.png, prompt.txt and
              request.json.
  pose        The picture that came back -> the master with only the changed part taken: the picture is read on
              the master's own lattice, the new cells outside the silhouette and the differing cells that hang
              together with them are taken, every other cell stays the master's cell. So the build cannot change.
  ask-view    Another view (a side view for walking, a back view): the master stands on the left half of a wide
              reference picture and the prompt says "THE ONLY CHANGE: in the empty right half, add a SECOND
              figure: the same character" in that view, exactly as tall. Writes reference.png, prompt.txt and
              request.json.
  view        The picture that came back -> the second figure as a sprite. The first figure is the ruler: it gives
              the cell size, so one cell of the new view is one cell of the master. The second figure is read with
              that cell size on its own lattice (its cells can lie half a cell beside the first figure's).
  lineup      The master, sprites made from it and the drawings of motion sets on ONE ground line, with lines at
              the master's top, neck and feet: lineup.png and the heights as numbers. Run it before any motion is
              shown: a figure that grew, shrank or got thinner shows at once.

An image model paints "pixel art" as squares that are not all one size and do not sit on a grid. `master` follows
the picture's own grid, so the sprite's size is what the picture had, not a number from a prompt: look at the
review picture and at the size in <name>.json.

Names and texts name no title. Success prints one ASCII JSON line; errors print "error: ..." and exit 1 and leave
nothing behind; usage errors exit 2. Nothing is ever replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import forge_core  # noqa: E402  (this skill's vendored copies)
import forge_pixelgrid as pg  # noqa: E402

TOOL_NAME = "pixel_look"
SPRITE_SCHEMA = "generate2dsprite.pixel_look.v2"
REQUEST_SCHEMA = "generate2dsprite.pixel_look_request.v1"
LINEUP_SCHEMA = "generate2dsprite.pixel_lineup.v1"
PICTURE = 1024                     # a square picture
WIDE = (1536, 1024)                # the picture of two figures
MIN_CELL = 8                       # px: a picture with smaller cells is no source of pixels
POSE_CELL, VIEW_CELL = 16, 14      # px a sprite pixel in a reference picture, at most
SHOWS_MASTER = 0.6                 # the share of the master's cells a picture must give back to count as "the master again"
GREY = (90, 90, 102)
GROUNDS = (("near white", (236, 232, 224)), ("mid grey", (128, 128, 128)), ("near black", (20, 18, 22)))

KEYS = {"magenta": ((255, 0, 255), "magenta (#FF00FF)", "magenta or pink"),
        "green": ((0, 255, 0), "green (#00FF00)", "green"),
        "blue": ((0, 0, 255), "blue (#0000FF)", "pure blue")}
VIEWS = {
    "side-right": "turned to a true side view, a profile, facing the viewer's right, standing upright: we see its right side",
    "side-left": "turned to a true side view, a profile, facing the viewer's left, standing upright: we see its left side",
    "back": "seen from behind, standing upright, facing away from the viewer",
    "front": "seen from the front, standing upright, facing the viewer",
}


class LookError(ValueError):
    """A refused input: printed as one "error: ..." line."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hex(colour: Sequence[int]) -> str:
    return "#%02x%02x%02x" % (int(colour[0]), int(colour[1]), int(colour[2]))


def _rgb(text: str) -> tuple[int, int, int]:
    return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _new_dir(path: str) -> Path:
    out = Path(path)
    if out.exists() and any(out.iterdir()):
        raise LookError(f"{out} exists already; name a new folder (nothing is ever replaced)")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1), encoding="utf-8", newline="\n")


def _sprite(path: str | Path, what: str = "sprite") -> np.ndarray:
    p = Path(path)
    if not p.is_file():
        raise LookError(f"{what} {p} does not exist")
    rgba = np.asarray(Image.open(p).convert("RGBA"))
    if not (rgba[..., 3] > 0).any():
        raise LookError(f"{what} {p.name} is empty")
    rgba = rgba.copy()
    rgba[..., 3] = np.where(rgba[..., 3] > 127, 255, 0)
    return pg.crop_to_figure(rgba).copy()


def far_key(colours: np.ndarray) -> str:
    """The key colour that lies farthest from a figure's colours (the 2nd percentile of the distances)."""
    colours = np.asarray(colours, np.float32).reshape(-1, 3)
    best, name = -1.0, "magenta"
    for key, (rgb, _, _) in KEYS.items():
        near = float(np.percentile(np.sqrt(((colours - np.asarray(rgb, np.float32)) ** 2).sum(axis=1)), 2))
        if near > best:
            best, name = near, key
    return name


def named_key(measured: Sequence[float]) -> str:
    return min(KEYS, key=lambda k: sum((a - b) ** 2 for a, b in zip(KEYS[k][0], measured)))


def _picture_colours(path: Path) -> np.ndarray:
    """A picture's colours without its flat ground: for choosing a key colour the figure does not have."""
    rgb = np.asarray(Image.open(path).convert("RGB").resize((96, 96), Image.BOX)).astype(np.float32)
    ground = pg.measure_key(rgb, ring=4)
    flat = rgb.reshape(-1, 3)
    figure = flat[np.sqrt(((flat - ground) ** 2).sum(axis=1)) > pg.KEY_DISTANCE]
    return figure if len(figure) else flat


BACKGROUND = ("The background is one perfectly flat pure {name} everywhere outside the figure, and there is no {avoid} "
              "on the figure. No shadow, no floor, no glow, no effect, no text, no frame. The figure ends in a crisp, "
              "closed dark outline against the background, with no soft halo around it.")


# --------------------------------------------------------------------------- ask-master

def master_prompt(subject: str, key: str) -> str:
    name, avoid = KEYS[key][1], KEYS[key][2]
    return "\n\n".join([
        "Create exactly ONE square image.",
        f"One image is attached. It shows a small pixel-art game sprite, enlarged: {subject.strip().rstrip('.')}.",
        "Draw this same sprite again, exactly as it is drawn there: the same pose, the same proportions, the same "
        "colours, the same shading, the same face, the same amount of detail. Do not redesign it, do not add "
        "detail, do not make the body taller. Keep all of its shading tones.",
        "Draw it ONCE, large and centred: the whole figure, from its highest point to its lowest, fills about three "
        "quarters of the picture's height.",
        "It is real pixel art: the figure is built from square pixel blocks, each block clearly visible, with sharp "
        "edges between the blocks.",
        "The only changes from the attached image: " + BACKGROUND.format(name=name, avoid=avoid) + " Nothing else is "
        "in the picture.",
    ]) + "\n"


def cmd_ask_master(o: argparse.Namespace) -> dict:
    how = Path(o.how)
    if not how.is_file():
        raise LookError(f"reference {how} does not exist")
    key = o.key if o.key != "auto" else far_key(_picture_colours(how))
    text = master_prompt(o.subject, key)
    out = _new_dir(o.output_dir)
    (out / "prompt.txt").write_text(text, encoding="utf-8", newline="\n")
    record = {"schema": REQUEST_SCHEMA, "tool": TOOL_NAME, "kind": "master", "subject": o.subject, "key": key,
              "references": [str(how)], "size": f"{PICTURE}x{PICTURE}",
              "next": f"route_media.py image --prompt-file prompt.txt --reference {how} --size {PICTURE}x{PICTURE} "
                      "--out-dir <take>; then pixel_look.py master --image <take>/generated.png"}
    _write_json(out / "request.json", record)
    return {"status": "ok", "prompt": str(out / "prompt.txt"), "request": str(out / "request.json"), "key": key,
            "references": record["references"], "words": len(text.split())}


# --------------------------------------------------------------------------- master

def clean_edge(sprite: np.ndarray, key: Sequence[float]) -> dict:
    """What a key-colour ground leaves on a read sprite (in place): edge cells mixed with the key, rare colours
    tinted by it, edge cells that are still nearly the key, small gaps inside the figure that are, and islands."""
    line = pg.outline_colour(sprite)
    mixed = pg.drop_key_mixed(sprite, key, line=line)
    return {"keyMixed": mixed, "keyTinted": int(pg.drop_key_tinted(sprite, key)), "keyNear": int(pg.drop_key_near(sprite, key)),
            "keyHoles": int(pg.drop_key_holes(sprite, key)), "keyInked": int(pg.ink_key_mixed(sprite, key, line)),
            "islandCells": int(pg.remove_islands(sprite, 4))}


def finish_colours(sprite: np.ndarray, colors: int, line: Sequence[int]) -> None:
    """The sprite's colours brought down to ``colors`` (in place), the line colour kept as it is."""
    if not colors:
        return
    opaque = sprite[..., 3] > 0
    is_line = opaque & (sprite[..., :3] == np.asarray(line, np.uint8)).all(axis=2)
    rest = opaque & ~is_line
    if rest.any():
        sprite[rest, :3] = pg.fewer_colours(sprite[rest][:, :3], max(1, colors - 1))


def master_from(picture: np.ndarray, *, colors: int = 48, line: Sequence[int] | None = None) -> tuple[np.ndarray, dict]:
    """The model's picture -> the master and the record of what was done."""
    snap = pg.snap_picture(picture, colors=4096, gap=0.0)
    cell = min(float(v) for v in snap["period"])
    if cell < MIN_CELL:
        raise LookError(f"the picture's cells are only about {cell:.1f} px: cells this small are soft and give no clean edge. "
                        f"Ask for ONE figure, large (cells of {MIN_CELL} px or more)")
    sprite = snap["sprite"].copy()
    edge = clean_edge(sprite, snap["key"])
    sprite = pg.crop_to_figure(sprite).copy()
    sprite, outline = pg.rule_outline(sprite, line)
    finish_colours(sprite, colors, outline["line"])
    info = {"method": snap["method"], "cellPx": snap["period"], "purity": snap["purity"], "key": snap["key"], "edge": edge,
            "outline": outline}
    return sprite, info


def _how_picture(sprite: np.ndarray, key: Sequence[int]) -> tuple[np.ndarray, int]:
    zoom = max(1, min(24, (PICTURE - 64) // max(sprite.shape[0], sprite.shape[1])))
    return pg.enlarge(sprite, zoom, key, size=PICTURE), zoom


def _review(left: Image.Image, sprite: np.ndarray, ground: Sequence[int], height: int = 720) -> Image.Image:
    left = left.resize((round(left.width * height / left.height), height), Image.LANCZOS)
    k = max(1, (height - 40) // sprite.shape[0])
    right = Image.fromarray(pg.enlarge(sprite, k, ground, margin=2), "RGB")
    page = Image.new("RGB", (left.width + 20 + right.width, height), (24, 26, 32))
    page.paste(left, (0, 0))
    page.paste(right, (left.width + 20, (height - right.height) // 2))
    return page


def _grounds(sprite: np.ndarray, extra: Sequence[int] | None = None) -> Image.Image:
    grounds = list(GROUNDS) + ([("game ground", tuple(extra))] if extra else [])
    k = max(1, min(12, 480 // sprite.shape[0], 1600 // (len(grounds) * (sprite.shape[1] + 6))))
    panes = [Image.fromarray(pg.enlarge(sprite, k, colour, margin=3), "RGB") for _, colour in grounds]
    page = Image.new("RGB", (sum(p.width for p in panes) + 8 * (len(panes) - 1), panes[0].height + 16), (24, 26, 32))
    draw = ImageDraw.Draw(page)
    x = 0
    for (label, _), pane in zip(grounds, panes):
        page.paste(pane, (x, 16))
        draw.text((x + 4, 2), label, fill=(235, 235, 240))
        x += pane.width + 8
    return page


def _sprite_record(sprite: np.ndarray, name: str, kind: str) -> dict:
    mid, base = pg.boots(sprite)
    palette = pg.palette_of(sprite)
    return {"schema": SPRITE_SCHEMA, "tool": TOOL_NAME, "kind": kind, "name": name,
            "size": [int(sprite.shape[1]), int(sprite.shape[0])], "cells": int((sprite[..., 3] > 0).sum()),
            "colours": int(len(palette)), "palette": [_hex(c) for c in palette],
            "feet": {"middle": mid, "row": int(base)}, "anchor": [mid + 0.5, int(base) + 1]}


def cmd_master(o: argparse.Namespace) -> dict:
    source = Path(o.image)
    if not source.is_file():
        raise LookError(f"{source} does not exist")
    picture = np.asarray(Image.open(source).convert("RGB"))
    try:
        sprite, info = master_from(picture, colors=o.colors, line=_rgb(o.line) if o.line else None)
    except ValueError as error:
        raise LookError(str(error)) from None
    out = _new_dir(o.output_dir)
    name = o.name
    key_name = named_key(info["key"])
    Image.fromarray(sprite, "RGBA").save(out / f"{name}.png")
    how, zoom = _how_picture(sprite, KEYS[key_name][0])
    Image.fromarray(how, "RGB").save(out / f"{name}-how.png")
    _review(Image.open(source).convert("RGB"), sprite, info["key"]).save(out / f"{name}-review.png")
    _grounds(sprite, _rgb(o.ground) if o.ground else None).save(out / f"{name}-grounds.png")
    record = _sprite_record(sprite, name, "master")
    record.update({"key": key_name, "howZoom": zoom, "read": info, "source": {"image": str(source), "sha256": _sha256(source)},
                   "files": {"sprite": f"{name}.png", "how": f"{name}-how.png", "review": f"{name}-review.png",
                             "grounds": f"{name}-grounds.png"},
                   "note": "The sprite's size is what the picture had. Look at the review picture, and at the sprite's edge on "
                           "every ground, before anything is made from it."})
    _write_json(out / f"{name}.json", record)
    return {"status": "ok", "sprite": str(out / f"{name}.png"), "size": record["size"], "colours": record["colours"],
            "cellPx": info["cellPx"], "outlineClosed": info["outline"]["closed"], "how": str(out / f"{name}-how.png"),
            "review": str(out / f"{name}-review.png"), "grounds": str(out / f"{name}-grounds.png"),
            "record": str(out / f"{name}.json")}


# --------------------------------------------------------------------------- the master on a lattice (ask-pose, ask-view)

def _request(path: str | Path, kind: str) -> tuple[dict, np.ndarray, Path]:
    p = Path(path)
    if p.is_dir():
        p = p / "request.json"
    if not p.is_file():
        raise LookError(f"{p} does not exist (the request.json of pixel_look.py ask-{kind})")
    record = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("schema") != REQUEST_SCHEMA or record.get("kind") != kind:
        raise LookError(f"{p.name} is not the request of ask-{kind}")
    master_path = p.parent / record["master"]["file"]
    if not master_path.is_file() or _sha256(master_path) != record["master"]["sha256"]:
        raise LookError(f"the request's copy of the master ({master_path.name}) is missing or was changed")
    return record, _sprite(master_path, "master"), p.parent


def _lattice_reference(master: np.ndarray, canvas: Sequence[int], cell: int, at: Sequence[int], key: Sequence[int]) -> np.ndarray:
    page = np.empty((canvas[1], canvas[0], 3), dtype=np.uint8)
    page[...] = np.asarray(key, np.uint8)
    big = np.repeat(np.repeat(master, cell, axis=0), cell, axis=1)
    region = page[at[1] * cell:at[1] * cell + big.shape[0], at[0] * cell:at[0] * cell + big.shape[1]]
    mask = big[..., 3] > 0
    region[mask] = big[mask][:, :3]
    return page


def _ask_common(o: argparse.Namespace, kind: str, canvas: Sequence[int], cell: int, at: Sequence[int], text: str,
                master: np.ndarray, key: str, extra: dict) -> dict:
    out = _new_dir(o.output_dir)
    Image.fromarray(master, "RGBA").save(out / "master.png")
    Image.fromarray(_lattice_reference(master, canvas, cell, at, KEYS[key][0]), "RGB").save(out / "reference.png")
    (out / "prompt.txt").write_text(text, encoding="utf-8", newline="\n")
    size = f"{canvas[0]}x{canvas[1]}"
    record = {"schema": REQUEST_SCHEMA, "tool": TOOL_NAME, "kind": kind, "key": key, "cell": int(cell), "canvas": list(canvas),
              "masterAt": [int(at[0]), int(at[1])], "master": {"file": "master.png", "sha256": _sha256(out / "master.png"),
                                                               "size": [int(master.shape[1]), int(master.shape[0])]},
              "references": ["reference.png"], "size": size,
              "next": f"route_media.py image --prompt-file prompt.txt --reference reference.png --size {size} --out-dir <take>; "
                      f"then pixel_look.py {kind} --image <take>/generated.png --request <this folder>"}
    record.update(extra)
    _write_json(out / "request.json", record)
    return {"status": "ok", "reference": str(out / "reference.png"), "prompt": str(out / "prompt.txt"),
            "request": str(out / "request.json"), "key": key, "cell": int(cell), "size": size, "words": len(text.split())}


def pose_prompt(change: str, cell: int, key: str) -> str:
    name = KEYS[key][1]
    return "\n\n".join([
        "Create exactly ONE square image.",
        f"One image is attached: a pixel-art game sprite on a flat {name} background, shown enlarged. Every square of "
        f"{cell} px in it is one pixel of the sprite.",
        "Reproduce the attached image: the same single figure at exactly the same size and in the same place on the "
        "canvas, the same pixel art on the same coarse, regular square pixel grid, every pixel exactly as large as in "
        "the attached image, one flat colour per pixel, hard edges, the same dark outline one pixel wide, the same colours.",
        "THE ONLY CHANGE: " + _sentence(change),
        "Everything that the change does not name is NOT redrawn: it stays exactly as it is, pixel for pixel, in the "
        f"same place. The background stays the same flat pure {name} everywhere, with no shadow and nothing else in the "
        "picture.",
    ]) + "\n"


def cmd_ask_pose(o: argparse.Namespace) -> dict:
    master = _sprite(o.master, "master")
    h, w = master.shape[:2]
    if o.room:
        try:
            left, top, right, bottom = (int(v) for v in o.room.split(","))
        except ValueError:
            raise LookError("--room is L,T,R,B in sprite pixels") from None
        if min(left, top, right, bottom) < 0:
            raise LookError("--room is L,T,R,B in sprite pixels, none below 0")
    else:
        side = max(w, h) // 2
        left, top, right, bottom = side, 6, side, 3
    cell = o.cell or min(POSE_CELL, PICTURE // (w + left + right), PICTURE // (h + top + bottom))
    if cell < MIN_CELL:
        raise LookError(f"a {w}x{h} sprite with this room needs cells of {cell} px in a {PICTURE} px picture; the least is "
                        f"{MIN_CELL}: give less --room")
    if (w + left + right) * cell > PICTURE or (h + top + bottom) * cell > PICTURE:
        raise LookError(f"--cell {cell} does not fit the sprite and its room into a {PICTURE} px picture")
    spare_x = PICTURE // cell - (w + left + right)
    spare_y = PICTURE // cell - (h + top + bottom)
    at = (left + spare_x // 2, top + spare_y // 2)
    key = o.key if o.key != "auto" else far_key(master[master[..., 3] > 0][:, :3])
    return _ask_common(o, "pose", (PICTURE, PICTURE), cell, at, pose_prompt(o.change, cell, key), master, key,
                       {"change": o.change})


def view_prompt(view: str, details: Sequence[str], cell: int, key: str) -> str:
    name = KEYS[key][1]
    parts = [
        "Create exactly ONE wide landscape image (3:2), the same size and layout as the attached image.",
        f"One image is attached: on its left half stands a pixel-art game sprite on a flat {name} background, shown "
        f"enlarged. Every square of {cell} px in it is one pixel of the sprite. The right half is empty.",
        "Reproduce the attached image: the same figure on the left, at exactly the same size and in the same place, the "
        "same pixel art on the same coarse, regular square pixel grid, one flat colour per pixel, hard edges, the same "
        "dark outline one pixel wide, the same colours.",
        "THE ONLY CHANGE: in the empty right half, add a SECOND figure: the SAME character " + _sentence(view),
        "The second figure is drawn by the same pixel artist on the same pixel grid, with pixels exactly as large as the "
        "first figure's. It is exactly as tall as the first figure: its highest point and the soles of its feet are on "
        "the same two horizontal lines as the first figure's. It has the same head size, the same body and legs, the "
        "same colours, the same kind of outline and shading. It is not taller, not thinner and not more detailed than "
        "the first figure.",
    ]
    if details:
        parts.append("In this view: " + " ".join(_sentence(d) for d in details))
    parts.append(f"The two figures stand well apart and do not touch. The background stays the same flat pure {name} "
                 "everywhere, with no shadow, no floor, no text and nothing else in the picture.")
    return "\n\n".join(parts) + "\n"


def cmd_ask_view(o: argparse.Namespace) -> dict:
    master = _sprite(o.master, "master")
    h, w = master.shape[:2]
    words = o.view_words or VIEWS.get(o.view)
    if not words:
        raise LookError("give --view (one of: " + ", ".join(sorted(VIEWS)) + ") or --view-words")
    half = WIDE[0] // 2
    cell = o.cell or min(VIEW_CELL, (half - 40) // w, (WIDE[1] - 120) // h)
    if cell < MIN_CELL:
        raise LookError(f"a {w}x{h} sprite needs cells of {cell} px to stand on half of a {WIDE[0]}x{WIDE[1]} picture; the "
                        f"least is {MIN_CELL}")
    if w * cell > half - 20 or h * cell > WIDE[1] - 40:
        raise LookError(f"--cell {cell} does not fit the sprite on half of the picture")
    at = (max(1, (half // cell - w) // 2), max(1, (WIDE[1] // cell - h) // 2))
    key = o.key if o.key != "auto" else far_key(master[master[..., 3] > 0][:, :3])
    return _ask_common(o, "view", WIDE, cell, at, view_prompt(words, o.detail or (), cell, key), master, key,
                       {"view": o.view or "custom", "viewWords": words})


SAME_HEIGHT = ("It is exactly as tall as the first figure: the top of its head and the soles of its feet are on the same "
               "two horizontal lines as the first figure's head and feet. Only what it holds or wears on its head (a "
               "staff, a blade, horns, a hat) may rise above that line.")


def cast_prompt(subject: str, pose: str | None, size: str | None, cell: int, key: str) -> str:
    name, avoid = KEYS[key][1], KEYS[key][2]
    return "\n\n".join([
        "Create exactly ONE wide landscape image (3:2), the same size and layout as the FIRST attached image.",
        "Two images are attached.",
        f"The FIRST image: at its left stands a pixel-art game sprite on a flat {name} background, shown enlarged. "
        f"Every square of {cell} px in it is one pixel of the sprite. The rest of the picture, to the right of it, is empty.",
        f"The SECOND image is a picture of another character of the same game: {subject.strip().rstrip('.')}. It shows "
        "WHO to draw: the design and the colours, not its style and not its proportions.",
        "Reproduce the FIRST image: the same figure at the left, at exactly the same size and in the same place, the "
        "same pixel art on the same coarse, regular square pixel grid, one flat colour per pixel, hard edges, the same "
        "dark outline one pixel wide, the same colours.",
        "THE ONLY CHANGE: in the empty area to the right, add a SECOND figure: the character of the SECOND image, "
        "drawn as a sprite of the same game by the same pixel artist, right after the first sprite.",
        "The second figure is drawn on the same pixel grid, with pixels exactly as large as the first figure's, and it "
        "stands on the same ground line: the soles of its feet are on the same horizontal line as the first figure's. "
        + _sentence(size or SAME_HEIGHT) + " It has the same kind of body build and head size as the first figure, "
        "the same kind of outline, the same kind of shading and the same colour mood. It is not more detailed than "
        "the first figure.",
        "Pose: " + _sentence(pose or "the same view and the same stance as the first figure"),
        f"The two figures stand well apart and do not touch. The background stays the same flat pure {name} "
        f"everywhere, there is no {avoid} on the second figure, and there is no shadow, no floor, no glow, no text and "
        "nothing else in the picture.",
    ]) + "\n"


def cmd_ask_cast(o: argparse.Namespace) -> dict:
    master = _sprite(o.master, "master")
    who = Path(o.who)
    if not who.is_file():
        raise LookError(f"reference {who} does not exist")
    h, w = master.shape[:2]
    # the master in the left third where it fits; a wide master keeps cells of MIN_CELL and takes up to half the picture
    cell = o.cell or min(VIEW_CELL, max(MIN_CELL, (WIDE[0] // 3) // (w + 4)), (WIDE[1] - 120) // h)
    if cell < MIN_CELL:
        raise LookError(f"a {w}x{h} sprite needs cells of {cell} px to stand at the left of a {WIDE[0]}x{WIDE[1]} picture; "
                        f"the least is {MIN_CELL}")
    cols, rows = WIDE[0] // cell, WIDE[1] // cell
    if w + 4 > cols // 2 or h + 6 > rows:
        raise LookError(f"cells of {cell} px leave no room beside the {w}x{h} sprite in a {WIDE[0]}x{WIDE[1]} picture: it is "
                        "too large to be the ruler of a cast")
    at = (2, rows - h - 4)                                   # at the left, on a ground line near the bottom: room above
    colours = np.concatenate([master[master[..., 3] > 0][:, :3].astype(np.float32), _picture_colours(who)])
    key = o.key if o.key != "auto" else far_key(colours)
    text = cast_prompt(o.subject, o.pose, o.size, cell, key)
    size = f"{WIDE[0]}x{WIDE[1]}"
    return _ask_common(o, "cast", WIDE, cell, at, text, master, key,
                       {"subject": o.subject, "sameHeight": not o.size, "references": ["reference.png", str(who)],
                        "next": f"route_media.py image --prompt-file prompt.txt --reference reference.png --reference {who} "
                                f"--size {size} --out-dir <take>; then pixel_look.py cast --image <take>/generated.png "
                                "--request <this folder>"})


def _fit(picture: np.ndarray, request: dict, master: np.ndarray) -> tuple[dict, int, int]:
    """The lattice of a picture that shows the request's reference again, and the lattice's columns and rows."""
    expected = request["cell"] * picture.shape[1] / float(request["canvas"][0])
    fit = pg.fit_lattice(picture, master, expected, request["masterAt"])
    if fit["share"] < SHOWS_MASTER:
        raise LookError(f"the picture does not show the master again: only {round(100 * fit['share'])} percent of its cells "
                        "come back equal. Generate again (the prompt asks for the same figure in the same place)")
    return fit, request["canvas"][0] // request["cell"], request["canvas"][1] // request["cell"]


# --------------------------------------------------------------------------- pose

def take_change(cells: np.ndarray, on: np.ndarray, full: np.ndarray, smallest: int = 10) -> tuple[np.ndarray, np.ndarray, dict]:
    """The master with only the changed part of a picture's cells. ``cells`` and ``on`` are the picture read on the
    master's lattice (colours and which cells are figure), ``full`` is the master on that lattice (RGBA). Taken are
    the new cells outside the master's silhouette that form a group of at least ``smallest`` cells (a weapon, a
    raised arm) and the differing cells that hang together with them; when the change adds nothing outside the
    silhouette, the differing groups of at least ``smallest`` cells are taken instead. Returns the sprite on the
    lattice, the mask of taken cells and the record."""
    m_on = full[..., 3] > 0
    dist = np.abs(cells - full[..., :3].astype(np.float32)).sum(axis=2)
    equal = m_on & on & (dist < pg.EQUAL)
    differs = (m_on ^ on) | (m_on & on & (dist >= pg.EQUAL))
    seeds = np.zeros_like(on)
    for group in pg.components(on & ~m_on):
        if len(group) >= smallest:
            for j, i in group:
                seeds[j, i] = True
    how = "the new part outside the silhouette and what hangs together with it"
    region = np.zeros_like(on)
    if seeds.any():
        stack = [(int(j), int(i)) for j, i in zip(*np.where(seeds))]
        rows, cols = on.shape
        while stack:
            j, i = stack.pop()
            if not (0 <= j < rows and 0 <= i < cols) or region[j, i] or not differs[j, i]:
                continue
            region[j, i] = True
            stack += [(j + 1, i), (j - 1, i), (j, i + 1), (j, i - 1)]
    else:
        how = f"the differing groups of {smallest} cells or more (nothing new lies outside the silhouette)"
        for group in pg.components(differs):
            if len(group) >= smallest:
                for j, i in group:
                    region[j, i] = True
    out = full.copy()
    take = region & on
    out[take, :3] = np.round(cells[take])
    out[take, 3] = 255
    out[region & ~on, 3] = 0
    return out, region, {"how": how, "pictureEqual": int(equal.sum()), "cellsTaken": int(region.sum()),
                         "newCells": int((take & ~m_on).sum()), "cellsEmptied": int((region & ~on & m_on).sum())}


def cmd_pose(o: argparse.Namespace) -> dict:
    request, master, _ = _request(o.request, "pose")
    source = Path(o.image)
    if not source.is_file():
        raise LookError(f"{source} does not exist")
    picture = np.asarray(Image.open(source).convert("RGB"))
    fit, cols, rows = _fit(picture, request, master)
    cells = pg.read_lattice(picture, fit["cell"], fit["offset"], cols, rows)
    key = pg.measure_key(picture.astype(np.float32))
    on = np.sqrt(((cells - key) ** 2).sum(axis=2)) >= pg.KEY_DISTANCE
    at = request["masterAt"]
    mh, mw = master.shape[:2]
    full = np.zeros((rows, cols, 4), dtype=np.uint8)
    full[at[1]:at[1] + mh, at[0]:at[0] + mw] = master
    out, region, taken = take_change(cells, on, full, o.smallest)
    if not region.any():
        raise LookError("the picture shows no change: every cell is the master's. Generate again, or say the change more plainly")
    # the taken part only: the key colour left on it (as on a master), its colours, its outline; every other cell
    # stays the master's
    cleaned = out.copy()
    edge = clean_edge(cleaned, key)
    out[region] = cleaned[region]
    opaque = out[..., 3] > 0
    take = region & opaque
    new_colours = 0
    if take.any():
        colours, new_colours = pg.to_palette(out[take][:, :3], pg.palette_of(master), extra=o.new_colours)
        out[take, :3] = colours
    pg.remove_islands(out, 4)
    opaque = out[..., 3] > 0
    ys, xs = np.where(opaque)
    x0, y0 = int(xs.min()), int(ys.min())
    cropped = np.ascontiguousarray(out[y0:int(ys.max()) + 1, x0:int(xs.max()) + 1])
    only = np.ascontiguousarray((region & opaque)[y0:int(ys.max()) + 1, x0:int(xs.max()) + 1])
    sprite, outline = pg.rule_outline(cropped, pg.outline_colour(master), only=only)
    where = [at[0] - x0 + outline["offset"][0], at[1] - y0 + outline["offset"][1]]       # the master's cell (0, 0) in the pose
    part = sprite[where[1]:where[1] + mh, where[0]:where[0] + mw]
    kept = int(((master[..., 3] > 0) & (part[..., 3] > 0) & (part[..., :3] == master[..., :3]).all(axis=2)).sum())
    total = int((master[..., 3] > 0).sum())
    out_dir = _new_dir(o.output_dir)
    name = o.name
    Image.fromarray(sprite, "RGBA").save(out_dir / f"{name}.png")
    k = max(1, min(12, 600 // max(sprite.shape[0], mh)))
    left = Image.fromarray(pg.enlarge(master, k, GREY, margin=2), "RGB")
    right = Image.fromarray(pg.enlarge(sprite, k, GREY, margin=2), "RGB")
    review = Image.new("RGB", (left.width + 12 + right.width, max(left.height, right.height)), (24, 26, 32))
    review.paste(left, (0, review.height - left.height))
    review.paste(right, (left.width + 12, review.height - right.height))
    review.save(out_dir / f"{name}-review.png")
    record = _sprite_record(sprite, name, "pose")
    record.update({"change": request.get("change"), "masterAt": where, "lattice": fit, "taken": taken,
                   "masterCellsKept": kept, "masterCells": total, "masterKeptShare": round(kept / float(max(1, total)), 4),
                   "newColours": new_colours, "edge": edge, "outline": outline,
                   "source": {"image": str(source), "sha256": _sha256(source)},
                   "files": {"sprite": f"{name}.png", "review": f"{name}-review.png"}})
    _write_json(out_dir / f"{name}.json", record)
    return {"status": "ok", "sprite": str(out_dir / f"{name}.png"), "size": record["size"], "masterAt": where,
            "pictureEqualShare": fit["share"], "cellsTaken": taken["cellsTaken"], "masterKeptShare": record["masterKeptShare"],
            "review": str(out_dir / f"{name}-review.png"), "record": str(out_dir / f"{name}.json")}


# --------------------------------------------------------------------------- view

def second_figure(picture: np.ndarray, request: dict, master: np.ndarray) -> tuple[np.ndarray, dict]:
    """The figure an image model drew to the right of the request's master, as cells (RGBA, cropped to the figure;
    the key colour left on its edge is cleaned). The first figure is the ruler: it must come back as the master and
    gives the cell size. The second figure is read with that cell size on its OWN lattice (its cells can lie half a
    cell beside the first figure's). Every part of it of some size is taken (a staff held apart from the body is a
    part), specks are not. Returns the cells and {"lattice", "ownLattice", "edge", "key", "top": its first row
    under the master's first row, in cells}."""
    fit, cols, rows = _fit(picture, request, master)
    at = request["masterAt"]
    mw = master.shape[1]
    phase = pg.lattice_phase(picture, fit["cell"], fit["offset"], (at[0] + mw + 2, 0, cols, rows))
    place = [fit["offset"][0] + phase["shift"][0], fit["offset"][1] + phase["shift"][1]]
    cells = pg.read_lattice(picture, fit["cell"], place, cols, rows)
    key = pg.measure_key(picture.astype(np.float32))
    on = np.sqrt(((cells - key) ** 2).sum(axis=2)) >= pg.KEY_DISTANCE
    on[:, :at[0] + mw + 2] = False
    groups = pg.components(on)
    count = int((master[..., 3] > 0).sum())
    if not groups or len(max(groups, key=len)) < 0.3 * count:
        raise LookError("there is no second figure to the right of the master in the picture. Generate again")
    mask = np.zeros_like(on)
    for group in groups:
        if len(group) >= max(12, 0.02 * count):
            for j, i in group:
                mask[j, i] = True
    lattice = np.zeros((rows, cols, 4), dtype=np.uint8)
    lattice[mask, :3] = np.round(cells[mask])
    lattice[mask, 3] = 255
    edge = clean_edge(lattice, key)
    ys, xs = np.where(lattice[..., 3] > 0)
    x0, y0 = int(xs.min()), int(ys.min())
    cropped = np.ascontiguousarray(lattice[y0:int(ys.max()) + 1, x0:int(xs.max()) + 1])
    return cropped, {"lattice": fit, "ownLattice": phase, "edge": edge, "key": [int(round(float(v))) for v in key],
                     "top": y0 + phase["shift"][1] / fit["cell"] - at[1]}


def _heights(sprite: np.ndarray, master: np.ndarray) -> tuple[int, int]:
    """The body's height (the column over the feet, down to the soles) of a sprite and of its master, in rows."""
    reach = _reach(master)
    return body_height(sprite, pg.boots(sprite), reach), body_height(master, pg.boots(master), reach)


def _write_beside(out_dir: Path, name: str, sprite: np.ndarray, master: np.ndarray, source: Path, key_name: str,
                  ground: str | None) -> dict:
    """The files of a sprite that was drawn beside its master: the sprite, its exact-grid picture, the generated
    picture above the two figures on one ground line, and the sprite on three grounds."""
    Image.fromarray(sprite, "RGBA").save(out_dir / f"{name}.png")
    how, zoom = _how_picture(sprite, KEYS[key_name][0])
    Image.fromarray(how, "RGB").save(out_dir / f"{name}-how.png")
    line = lineup_picture([("master", master, pg.boots(master)), (name, sprite, pg.boots(sprite))], master)
    shown = Image.open(source).convert("RGB")
    shown = shown.resize((line.width, max(1, round(shown.height * line.width / shown.width))), Image.LANCZOS)
    page = Image.new("RGB", (line.width, shown.height + 8 + line.height), (24, 26, 32))
    page.paste(shown, (0, 0))
    page.paste(line, (0, shown.height + 8))
    page.save(out_dir / f"{name}-review.png")
    _grounds(sprite, _rgb(ground) if ground else None).save(out_dir / f"{name}-grounds.png")
    return {"sprite": f"{name}.png", "how": f"{name}-how.png", "review": f"{name}-review.png", "grounds": f"{name}-grounds.png",
            "howZoom": zoom}


def cmd_view(o: argparse.Namespace) -> dict:
    request, master, _ = _request(o.request, "view")
    source = Path(o.image)
    if not source.is_file():
        raise LookError(f"{source} does not exist")
    picture = np.asarray(Image.open(source).convert("RGB"))
    cropped, read = second_figure(picture, request, master)
    opaque = cropped[..., 3] > 0
    colours, new_colours = pg.to_palette(cropped[opaque][:, :3], pg.palette_of(master), extra=o.new_colours)
    cropped[opaque, :3] = colours
    sprite, outline = pg.rule_outline(cropped, pg.outline_colour(master))
    tall, ruler_tall = _heights(sprite, master)
    soles = read["top"] - outline["offset"][1] + pg.boots(sprite)[1] - pg.boots(master)[1]     # under the master's, in cells
    warnings = []
    if abs(tall - ruler_tall) > 1:
        warnings.append(f"the new view's body is {tall} rows tall, the master's {ruler_tall}: it is not the same height; "
                        "generate again")
    out_dir = _new_dir(o.output_dir)
    name = o.name
    key_name = named_key(read["key"])
    files = _write_beside(out_dir, name, sprite, master, source, key_name, None)
    record = _sprite_record(sprite, name, "view")
    record.update({"view": request.get("view"), "lattice": read["lattice"], "rulerEqualShare": read["lattice"]["share"],
                   "heightRows": tall, "masterHeightRows": ruler_tall, "solesBelowMasters": round(float(soles), 2),
                   "ownLattice": read["ownLattice"], "edge": read["edge"], "newColours": new_colours, "outline": outline,
                   "key": key_name, "howZoom": files.pop("howZoom"), "warnings": warnings,
                   "source": {"image": str(source), "sha256": _sha256(source)}, "files": files})
    _write_json(out_dir / f"{name}.json", record)
    return {"status": "ok", "sprite": str(out_dir / f"{name}.png"), "size": record["size"],
            "rulerEqualShare": read["lattice"]["share"], "heightRows": tall, "masterHeightRows": ruler_tall, "warnings": warnings,
            "review": str(out_dir / f"{name}-review.png"), "record": str(out_dir / f"{name}.json")}


# --------------------------------------------------------------------------- cast

def cmd_cast(o: argparse.Namespace) -> dict:
    request, master, _ = _request(o.request, "cast")
    source = Path(o.image)
    if not source.is_file():
        raise LookError(f"{source} does not exist")
    picture = np.asarray(Image.open(source).convert("RGB"))
    cropped, read = second_figure(picture, request, master)
    sprite, outline = pg.rule_outline(cropped, pg.outline_colour(master))
    finish_colours(sprite, o.colors, outline["line"])
    tall, ruler_tall = _heights(sprite, master)
    soles = read["top"] - outline["offset"][1] + pg.boots(sprite)[1] - pg.boots(master)[1]
    warnings = []
    if request.get("sameHeight") and abs(tall - ruler_tall) > 2:
        warnings.append(f"the new character's body is {tall} rows tall, the first master's {ruler_tall}: it was asked to be "
                        "as tall; generate again, or accept another height knowingly")
    out_dir = _new_dir(o.output_dir)
    name = o.name
    key_name = named_key(read["key"])
    files = _write_beside(out_dir, name, sprite, master, source, key_name, o.ground)
    record = _sprite_record(sprite, name, "master")
    record.update({"key": key_name, "howZoom": files.pop("howZoom"),
                   "read": {"method": "beside the first master", "cellPx": [read["lattice"]["cell"]] * 2, "lattice": read["lattice"],
                            "ownLattice": read["ownLattice"], "key": read["key"], "edge": read["edge"], "outline": outline},
                   "rulerEqualShare": read["lattice"]["share"], "heightRows": tall, "masterHeightRows": ruler_tall,
                   "solesBelowMasters": round(float(soles), 2), "warnings": warnings,
                   "source": {"image": str(source), "sha256": _sha256(source)}, "files": files,
                   "note": "Drawn beside the first master, so one pixel of this sprite is one pixel of that one. Look at the "
                           "review picture and at the sprite's edge on every ground before anything is made from it."})
    _write_json(out_dir / f"{name}.json", record)
    return {"status": "ok", "sprite": str(out_dir / f"{name}.png"), "size": record["size"], "colours": record["colours"],
            "cellPx": read["lattice"]["cell"], "rulerEqualShare": read["lattice"]["share"], "heightRows": tall,
            "masterHeightRows": ruler_tall, "outlineClosed": outline["closed"], "warnings": warnings,
            "how": str(out_dir / f"{name}-how.png"), "review": str(out_dir / f"{name}-review.png"),
            "grounds": str(out_dir / f"{name}-grounds.png"), "record": str(out_dir / f"{name}.json")}


# --------------------------------------------------------------------------- lineup

def _neck(master: np.ndarray) -> int:
    """The row of a figure's neck: the narrowest row under the widest row of its upper half."""
    widths = (master[..., 3] > 0).sum(axis=1)
    half = max(2, master.shape[0] // 2)
    widest = int(widths[:half].argmax())
    return widest + int(widths[widest:max(widest + 1, master.shape[0] * 2 // 3)].argmin())


def _reach(master: np.ndarray) -> int:
    """How many columns to each side of the feet's middle are the body's column: an eighth of the master's width, so
    that a staff carried beside the body, a blade and a helmet's horns are outside it and the head is inside."""
    return max(2, int(round(0.12 * master.shape[1])))


def body_height(drawing: np.ndarray, feet: Sequence[float], reach: int) -> int:
    """Rows from the top of the column over the feet down to the feet's row (what is held to one side is outside)."""
    x, base = int(round(feet[0])), int(feet[1])
    column = (drawing[:, max(0, x - reach):x + reach + 1, 3] > 0).any(axis=1)
    rows = np.where(column)[0]
    return int(base - rows.min() + 1) if len(rows) else 0


def lineup_picture(items: Sequence[tuple[str, np.ndarray, Sequence[float]]], master: np.ndarray, zoom: int | None = None,
                   per_row: int = 6) -> Image.Image:
    """Drawings (label, RGBA, (feet middle, feet row)) on one ground line a row, with the master's top, neck and
    ground as lines."""
    m_mid, m_base = pg.boots(master)
    left = max(int(np.ceil(feet[0])) for _, _, feet in items) + 3
    right = max(d.shape[1] - int(feet[0]) for _, d, feet in items) + 3
    above = max(int(feet[1]) for _, _, feet in items) + 5
    below = max(d.shape[0] - int(feet[1]) for _, d, feet in items) + 2
    cw, ch = left + right, above + below
    k = zoom or max(1, min(8, 2200 // (min(per_row, len(items)) * cw), 420 // ch))
    rows = (len(items) + per_row - 1) // per_row
    page = Image.new("RGB", (min(per_row, len(items)) * cw * k, rows * ch * k), GREY)
    draw = ImageDraw.Draw(page)
    lines = ((int(m_base), (255, 230, 120)), (int(m_base) - _neck(master), (120, 220, 255)), (-1, (255, 255, 255)))
    for n, (label, drawing, feet) in enumerate(items):
        px, py = (n % per_row) * cw * k, (n // per_row) * ch * k
        for up, colour in lines:
            y = py + (above - up) * k
            draw.line([(px, y), (px + cw * k - 1, y)], fill=colour, width=1)
        big = Image.fromarray(drawing, "RGBA").resize((drawing.shape[1] * k, drawing.shape[0] * k), Image.NEAREST)
        page.paste(big, (px + int(round((left - feet[0] - 0.5) * k)), py + (above - int(feet[1])) * k), big)
        draw.text((px + 4, py + 2), label, fill=(255, 255, 255))
    return page


def _set_drawings(folder: Path) -> list[tuple[str, np.ndarray, tuple[float, int]]]:
    """The drawings of a motion set (pixel_motion.py finish or breath): clean/frame_*.png and motion.json's anchor."""
    try:
        record = json.loads((folder / "motion.json").read_text(encoding="utf-8"))
        ax, ay = (float(v) for v in record["anchor"])
    except (ValueError, KeyError, TypeError):
        raise LookError(f"{folder}/motion.json is not the record of a motion set with an anchor (the point of the feet)") from None
    files = sorted((folder / "clean").glob("frame_*.png"))
    if not files:
        raise LookError(f"{folder} holds no clean/frame_*.png")
    name = str(record.get("name") or folder.name)
    return [(f"{name} {n + 1}", np.asarray(Image.open(f).convert("RGBA")), (ax - 0.5, int(round(ay)) - 1))
            for n, f in enumerate(files)]


def cmd_lineup(o: argparse.Namespace) -> dict:
    master = _sprite(o.master, "master")
    m_feet = pg.boots(master)
    reach = _reach(master)
    want = body_height(master, m_feet, reach)
    items: list[tuple[str, np.ndarray, Sequence[float]]] = [("master", master, m_feet)]
    report = []
    for item in o.item or ():
        path = Path(item)
        if path.is_dir():
            if not (path / "motion.json").is_file():
                raise LookError(f"{path} is no motion set (it holds no motion.json)")
            drawings = _set_drawings(path)
            kind = "set"
        elif path.is_file():
            sprite = _sprite(path)
            drawings = [(path.stem, sprite, pg.boots(sprite))]
            kind = "sprite"
        else:
            raise LookError(f"{path} does not exist")
        heights = [body_height(d, feet, reach) for _, d, feet in drawings]
        items += drawings
        report.append({"item": str(path), "kind": kind, "drawings": len(drawings), "heights": heights,
                       "firstDiffersBy": int(heights[0] - want)})
    if len(items) == 1:
        raise LookError("give at least one --item (a sprite PNG or the folder of a motion set)")
    out = _new_dir(o.output_dir)
    lineup_picture(items, master, o.zoom).save(out / "lineup.png")
    differing = [r["item"] for r in report if abs(r["firstDiffersBy"]) > o.tolerance]
    record = {"schema": LINEUP_SCHEMA, "tool": TOOL_NAME, "master": str(o.master), "masterHeight": want, "tolerance": o.tolerance,
              "items": report, "firstDrawingDiffers": differing, "verdict": "differs" if differing else "same",
              "note": "The first drawing of every item is an upright one and is compared with the master; the others are "
                      "poses (a crouch is lower, a blade over the head is higher): look at lineup.png."}
    _write_json(out / "lineup.json", record)
    return {"status": "ok", "picture": str(out / "lineup.png"), "record": str(out / "lineup.json"), "masterHeight": want,
            "verdict": record["verdict"], "firstDrawingDiffers": differing,
            "heights": {r["item"]: r["heights"] for r in report}}


# --------------------------------------------------------------------------- command line

def _staged(handler, options: argparse.Namespace) -> dict:
    """Run a command into a staging folder beside its --output-dir and publish that folder on a clean exit
    (forge_core.staged_output): a run that fails leaves nothing behind. The result names the published folder."""
    final = Path(options.output_dir)
    if os.path.lexists(final):
        raise LookError(f"{final} exists already; name a new folder (nothing is ever replaced)")
    try:
        with forge_core.staged_output(final) as stage:
            options.output_dir = str(stage)
            result = handler(options)
    except FileExistsError:
        raise LookError(f"{final} exists already; name a new folder (nothing is ever replaced)") from None
    published = str(final.parent.resolve() / final.name)
    return json.loads(json.dumps(result).replace(json.dumps(str(stage))[1:-1], json.dumps(published)[1:-1]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    keys = ["auto"] + sorted(KEYS)
    a = sub.add_parser("ask-master", help="the prompt of the first master's picture")
    a.add_argument("--how", required=True, help="the picture that shows how to draw: one figure in the manner wanted (cut "
                                                "from a style picture, or a sprite of your own)")
    a.add_argument("--subject", required=True, help="who the figure is, in one sentence")
    a.add_argument("--key", default="auto", choices=keys, help="the flat ground colour (default: the one the figure has least of)")
    a.add_argument("--output-dir", "--out-dir", required=True, help="new folder for prompt.txt and request.json")
    m = sub.add_parser("master", help="the picture -> the master sprite")
    m.add_argument("--image", required=True, help="the picture the image model drew (one figure on a flat key colour)")
    m.add_argument("--name", default="master", help="file stem (default master)")
    m.add_argument("--colors", type=int, default=48, help="the most colours the sprite keeps (default 48; 0 keeps all)")
    m.add_argument("--line", help="the outline colour #rrggbb (default: the picture's own)")
    m.add_argument("--ground", help="#rrggbb: the game's ground colour, as a fourth ground of *-grounds.png")
    m.add_argument("--output-dir", "--out-dir", required=True, help="new folder for the sprite and its files")
    c = sub.add_parser("ask-cast", help="the reference and the prompt of 'a second figure: another character, beside the master'")
    c.add_argument("--master", required=True, help="the first master sprite (1x PNG): the ruler of the cast")
    c.add_argument("--who", required=True, help="a picture of the new character (its design and colours)")
    c.add_argument("--subject", required=True, help="who the new character is, in one sentence")
    c.add_argument("--pose", help="the view and the stance in words (default: those of the master)")
    c.add_argument("--size", help="how tall it is beside the master, in words (default: exactly as tall)")
    c.add_argument("--cell", type=int, help=f"px a sprite pixel in the reference (default: the largest up to {VIEW_CELL} that "
                                            "fits; smaller gives a wide or tall figure more room)")
    c.add_argument("--key", default="auto", choices=keys)
    c.add_argument("--output-dir", "--out-dir", required=True, help="new folder for reference.png, prompt.txt, request.json")
    d = sub.add_parser("cast", help="the picture -> the new character's master, read beside the first master")
    d.add_argument("--image", required=True, help="the picture the image model drew from the request's references")
    d.add_argument("--request", required=True, help="the folder of ask-cast (or its request.json)")
    d.add_argument("--name", default="master", help="file stem (default master)")
    d.add_argument("--colors", type=int, default=48, help="the most colours the sprite keeps (default 48; 0 keeps all)")
    d.add_argument("--ground", help="#rrggbb: the game's ground colour, as a fourth ground of *-grounds.png")
    d.add_argument("--output-dir", "--out-dir", required=True, help="new folder for the sprite and its files")
    p = sub.add_parser("ask-pose", help="the reference and the prompt of 'the master with one change'")
    p.add_argument("--master", required=True, help="the master sprite (1x PNG)")
    p.add_argument("--change", required=True, help="THE ONLY CHANGE, in plain words: what is different and where")
    p.add_argument("--room", help="L,T,R,B: empty sprite pixels around the master for what the change adds (default: half "
                                  "of its larger side left and right, 6 above, 3 below)")
    p.add_argument("--cell", type=int, help=f"px a sprite pixel in the reference (default: the largest up to {POSE_CELL} that fits)")
    p.add_argument("--key", default="auto", choices=keys)
    p.add_argument("--output-dir", "--out-dir", required=True, help="new folder for reference.png, prompt.txt, request.json")
    q = sub.add_parser("pose", help="the picture -> the master with only the changed part taken")
    q.add_argument("--image", required=True, help="the picture the image model drew from the request's reference")
    q.add_argument("--request", required=True, help="the folder of ask-pose (or its request.json)")
    q.add_argument("--name", default="pose", help="file stem (default pose)")
    q.add_argument("--smallest", type=int, default=10, help="the smallest group of new or differing cells that is a change "
                                                            "and not noise (default 10)")
    q.add_argument("--new-colours", type=int, default=8, help="the most colours the change may add (default 8)")
    q.add_argument("--output-dir", "--out-dir", required=True, help="new folder for the sprite and its files")
    v = sub.add_parser("ask-view", help="the reference and the prompt of 'a second figure: the same character in another view'")
    v.add_argument("--master", required=True, help="the master sprite (1x PNG)")
    v.add_argument("--view", choices=sorted(VIEWS), help="the view")
    v.add_argument("--view-words", help="the view in your own words, instead of --view")
    v.add_argument("--detail", action="append", help="one thing that is seen in this view (which hand holds what, what the "
                                                     "cloak does); repeat")
    v.add_argument("--cell", type=int, help=f"px a sprite pixel in the reference (default: the largest up to {VIEW_CELL} that fits)")
    v.add_argument("--key", default="auto", choices=keys)
    v.add_argument("--output-dir", "--out-dir", required=True, help="new folder for reference.png, prompt.txt, request.json")
    w = sub.add_parser("view", help="the picture -> the second figure as a sprite on the master's lattice")
    w.add_argument("--image", required=True, help="the picture the image model drew from the request's reference")
    w.add_argument("--request", required=True, help="the folder of ask-view (or its request.json)")
    w.add_argument("--name", default="view", help="file stem (default view)")
    w.add_argument("--new-colours", type=int, default=10, help="the most colours the view may add (default 10)")
    w.add_argument("--output-dir", "--out-dir", required=True, help="new folder for the sprite and its files")
    u = sub.add_parser("lineup", help="the master and what was made from it on one ground line")
    u.add_argument("--master", required=True, help="the master sprite (1x PNG)")
    u.add_argument("--item", action="append", help="a sprite PNG, or the folder of a motion set; repeat")
    u.add_argument("--tolerance", type=int, default=1, help="rows an upright drawing may differ from the master (default 1)")
    u.add_argument("--zoom", type=int, help="px a sprite pixel in lineup.png (default: what fits)")
    u.add_argument("--output-dir", "--out-dir", required=True, help="new folder for lineup.png and lineup.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    for flag in ("line", "ground"):
        value = getattr(options, flag, None)
        if value and not (len(value) == 7 and value.startswith("#")):
            parser.error(f"--{flag} must be #rrggbb")
    handlers = {"ask-master": cmd_ask_master, "master": cmd_master, "ask-cast": cmd_ask_cast, "cast": cmd_cast,
                "ask-pose": cmd_ask_pose, "pose": cmd_pose, "ask-view": cmd_ask_view, "view": cmd_view, "lineup": cmd_lineup}
    try:
        result = _staged(handlers[options.command], options)
    except (LookError, OSError) as error:           # OSError: a file that is no picture, or one that cannot be read
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
