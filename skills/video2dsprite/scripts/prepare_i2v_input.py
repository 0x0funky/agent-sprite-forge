#!/usr/bin/env python3
"""Prepare an image-to-video input that can be registered by construction.

  prepare  Place the approved master (or one view of a multi-view sheet) on the
           provider canvas (default 1280x720) with its root on a fixed pixel and
           one recorded scale (integer NEAREST scaling for pixel art), over a flat
           key colour the master does not use (an opaque master on a flat
           magenta, green or blue backdrop is keyed first; the job records
           masterKeying). Writes input.png, prompt.txt (an
           action timeline with a numeric work region), a byte copy of the
           master, review-guide.png and registration_job.json
           (video2dsprite.registration_job.v1) into a new --output-dir.
  lint     Check a motion prompt for wording that weakens or breaks a clip.

register_clip.py later applies the inverse of the recorded transform to the
returned clip, so registration never depends on per-frame bounding boxes. This
script never calls a provider: generate the clip with the host tool,
generate2dmedia or a CLI route, then run register_clip.py.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_matte  # noqa: E402

TOOL_NAME = "prepare_i2v_input"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # QA envelopes and jobs record the package version (D29)
JOB_SCHEMA = "video2dsprite.registration_job.v1"
JOB_FILE = "registration_job.json"

DEFAULT_CANVAS = (1280, 720)
ROOT_Y_FRACTION = 620 / 720        # Dusk Crossing production root (640, 620) on 1280x720
SUBJECT_HEIGHT_FRACTION = 530 / 720  # Dusk Crossing subject height on the provider canvas
DEFAULT_MARGIN = 20                # px of the provider canvas kept free on every side
BLUR_ALLOWANCE = 2                 # px the resampled edge may spread past the subject box
TEMPLATE_DURATION_S = 6.0          # templates are written for a 6 s take and retimed
CALM_SPAN_S = 0.4                  # early calm span: the take starts on the exact reference pose
MIN_DURATION_S, MAX_DURATION_S = 2.0, 30.0

ACTION_PADDING = (96 / 448, 80 / 448, 96 / 448, 24 / 448)  # Dusk one-shot padding [96,80,96,24] on 448 px
FX_PADDING = (160 / 448,) * 4                             # Dusk FX padding 160 px on 448 px
NO_PADDING = (0.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class ActionTemplate:
    """One action's prompt contract. Timeline times are seconds of a 6 s take."""

    name: str
    kind: str                 # loop | oneshot | hold | fx
    role: str                 # actor | object | effect
    returns_to_rest: bool     # the take ends on the reference pose (end calm span for qc)
    padding: tuple[float, float, float, float]  # fractions of the source canvas (left, top, right, bottom)
    motion: str
    timeline: tuple[tuple[float, float, str], ...]
    avoid: str


_REST = (0.0, CALM_SPAN_S, "hold exactly the supplied reference pose, nothing moves yet")

ACTIONS: dict[str, ActionTemplate] = {template.name: template for template in (
    ActionTemplate(
        "idle", "loop", "actor", True, NO_PADDING,
        "Calm standing idle in place: breathing is clearly visible at game size, the chest and shoulders rise "
        "and settle by a few pixels, and hair, cloth and attached parts follow with a gentle delay. The feet "
        "stay planted on the same ground line.",
        (_REST,
         (0.4, 5.6, "two complete slow breathing cycles, about one every 2.6 seconds"),
         (5.6, 6.0, "settle back into exactly the starting pose, position and scale")),
        "no steps, gestures, attacks, turning, whole-body sliding or large bobbing"),
    ActionTemplate(
        "walk", "loop", "actor", True, NO_PADDING,
        "Walk IN PLACE like a game walk-cycle asset (treadmill): the body stays centred above the same ground "
        "anchor and never travels across the image; the game adds world movement. Alternating left and right "
        "steps with heel contact, bent-knee passing and toe-off, one foot always supporting on the original "
        "ground line, arms swinging opposite to the legs, small natural vertical weight transfer.",
        (_REST,
         (0.4, 5.0, "a steady walk in place, one full left-right pair of steps about every 0.8 seconds"),
         (5.0, 5.6, "finish the current step and bring the feet back into the starting stance"),
         (5.6, 6.0, "hold exactly the starting pose, position and scale")),
        "no hopping, skating, running, turning or sliding the whole image; both feet stay distinct and intact"),
    ActionTemplate(
        "run", "loop", "actor", True, NO_PADDING,
        "Run IN PLACE like a game run-cycle asset (treadmill): the body stays centred above the same ground "
        "anchor and never travels across the image; the game adds world movement. Clearly alternating legs "
        "with compression, push-off and a brief flight phase, opposing arm swing and restrained follow-through.",
        (_REST,
         (0.4, 5.0, "a steady run in place, one full left-right pair of strides about every 0.5 seconds"),
         (5.0, 5.6, "slow to a stop and bring the feet back into the starting stance"),
         (5.6, 6.0, "hold exactly the starting pose, position and scale")),
        "no travel across the image, turning, jumping away or sliding the whole image"),
    ActionTemplate(
        "attack", "oneshot", "actor", True, ACTION_PADDING,
        "ONE compact, readable attack in the existing facing direction. A small body lean is fine; the feet "
        "and root stay at the original location. Never repeat the strike.",
        (_REST,
         (0.4, 1.2, "obvious anticipation: wind up away from the target"),
         (1.2, 1.7, "ONE sharp strike toward the facing direction"),
         (1.7, 3.2, "recoil and fully return to the neutral stance"),
         (3.2, 6.0, "hold the exact original neutral stance")),
        "no repeated strikes, travel away from the root, detached energy waves, dust or weapon trails"),
    ActionTemplate(
        "cast", "oneshot", "actor", True, ACTION_PADDING,
        "ONE deliberate casting gesture with the existing hands or held item. The game renderer adds the "
        "magic separately.",
        (_REST,
         (0.4, 1.2, "raise a hand (or the existing held item) into a focused casting pose"),
         (1.2, 2.0, "sustain the clear casting pose"),
         (2.0, 3.5, "lower the hand and return to the neutral stance"),
         (3.5, 6.0, "hold the exact original neutral stance")),
        "no floating runes, energy effects, glowing hands or new props"),
    ActionTemplate(
        "guard", "oneshot", "actor", True, ACTION_PADDING,
        "Brace into a compact defensive stance with bent knees and forearms or existing equipment protecting "
        "the torso, hold it with only breathing, then relax. Grounded support and original body scale.",
        (_REST,
         (0.4, 1.3, "brace into the guarded pose"),
         (1.3, 3.5, "HOLD the guarded pose with only breathing"),
         (3.5, 4.5, "relax back toward neutral"),
         (4.5, 6.0, "hold the exact original neutral stance")),
        "no new shield or weapon, no effects, no stepping away from the root"),
    ActionTemplate(
        "hurt", "oneshot", "actor", True, ACTION_PADDING,
        "ONE brief, non-graphic hit reaction: a wince and a small recoil of the torso, then recovery. The "
        "feet stay on the ground line.",
        (_REST,
         (0.4, 0.8, "wince and recoil slightly backward at the torso"),
         (0.8, 1.8, "recover balance and return to the exact original pose"),
         (1.8, 6.0, "hold the exact original neutral stance")),
        "no wounds, blood, missing limbs, flying backward or falling over"),
    ActionTemplate(
        "victory", "hold", "actor", False, ACTION_PADDING,
        "ONE confident celebration that ends in a held victory pose; hair and cloth settle naturally.",
        (_REST,
         (0.4, 1.8, "celebrate with one confident gesture, such as a raised fist"),
         (1.8, 6.0, "HOLD the victory pose with relaxed breathing and settling hair and cloth")),
        "no jumping, spinning, new objects or equipment swung out of the work region"),
    ActionTemplate(
        "defeat", "hold", "actor", False, ACTION_PADDING,
        "A gentle, stylised, non-graphic defeat: lose balance and slump into a compact exhausted kneel or "
        "crouch inside the original sprite region.",
        (_REST,
         (0.4, 2.4, "lose balance and slump into a compact kneel or crouch"),
         (2.4, 6.0, "remain still in that defeated pose with faint breathing")),
        "no dissolving body, fragments, gore, ghosts or extra objects"),
    ActionTemplate(
        "ambient", "loop", "object", True, NO_PADDING,
        "Animate only the attached moving parts (leaves, cloth, flame, water) with a clearly visible cycle, "
        "about 2 to 4 percent of the moving part's size. Structural parts (trunk, base, walls, frame) stay "
        "exactly fixed.",
        (_REST,
         (0.4, 5.6, "a gentle, clearly visible cycle about every 2 seconds"),
         (5.6, 6.0, "return to exactly the reference state")),
        "no new branches, falling objects, rings, starbursts, pulsing glow or global brightness changes"),
    ActionTemplate(
        "fx", "fx", "effect", False, FX_PADDING,
        "Animate only the supplied game effect, centred on its origin: it plays once, grows, peaks, then "
        "dissipates smoothly until completely invisible.",
        (_REST,
         (0.4, 4.0, "the effect plays once: grows, peaks and dissipates until completely invisible"),
         (4.0, 6.0, "only the empty flat background remains")),
        "no starbursts or cross flares, text or rune letters, rectangular haze, global flash or pulsing"),
)}

_FACING_WORDS = {"left": "screen left", "right": "screen right", "front": "the viewer", "back": "away from the viewer"}


# --------------------------------------------------------------------------- argument parsing

def parse_numbers(text: str, count: int, name: str, *, integer: bool = False) -> tuple:
    """``count`` comma- (or x-) separated finite numbers, for argparse types."""
    parts = [part.strip() for part in re.split(r"[,x]", str(text).strip().lower())]
    if len(parts) != count or not all(parts):
        raise argparse.ArgumentTypeError(f"{name} needs {count} comma-separated numbers; got {text!r}")
    try:
        values = tuple(int(part) if integer else float(part) for part in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{name} needs {'integers' if integer else 'numbers'}; got {text!r}") from None
    if not all(math.isfinite(value) for value in values):
        raise argparse.ArgumentTypeError(f"{name} needs finite numbers; got {text!r}")
    return values


def parse_point(text: str) -> tuple[float, float]:
    return parse_numbers(text, 2, "a point X,Y")


def parse_size(text: str) -> tuple[int, int]:
    size = parse_numbers(text, 2, "a size W,H", integer=True)
    if min(size) < 16:
        raise argparse.ArgumentTypeError(f"a canvas needs at least 16x16 px; got {text!r}")
    return size


def parse_box(text: str) -> tuple[int, int, int, int]:
    box = parse_numbers(text, 4, "a box X0,Y0,X1,Y1", integer=True)
    if box[0] < 0 or box[1] < 0 or box[2] <= box[0] or box[3] <= box[1]:
        raise argparse.ArgumentTypeError(f"a box needs 0 <= X0 < X1 and 0 <= Y0 < Y1; got {text!r}")
    return box


def parse_padding(text: str) -> tuple[int, int, int, int]:
    padding = parse_numbers(text, 4, "padding L,T,R,B", integer=True)
    if min(padding) < 0:
        raise argparse.ArgumentTypeError(f"padding cannot be negative; got {text!r}")
    return padding


def parse_scale(text: str) -> float | None:
    if str(text).strip().lower() == "auto":
        return None
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--scale needs auto or a positive number; got {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"--scale needs auto or a positive number; got {text!r}")
    return value


# --------------------------------------------------------------------------- geometry

round_half_up = forge_core.round_half_up  # floor(value + 1/2), never banker's rounding (D30)


def default_padding(template: ActionTemplate, size: Sequence[int]) -> tuple[int, int, int, int]:
    """The template's padding fractions in source px (448 px gives Dusk's [96, 80, 96, 24])."""
    width, height = size
    left, top, right, bottom = template.padding
    return (round_half_up(left * width), round_half_up(top * height),
            round_half_up(right * width), round_half_up(bottom * height))


def default_root(canvas: Sequence[int], template: ActionTemplate, margin: float) -> tuple[float, float]:
    """(W/2, 0.861 H): Dusk's (640, 620) on 1280x720, kept inside the margin; effects: the centre."""
    width, height = canvas
    if template.role == "effect":
        return width / 2, height / 2
    return width / 2, float(min(round_half_up(height * ROOT_Y_FRACTION), math.floor(height - margin) - BLUR_ALLOWANCE))


def measure_anchor(alpha: np.ndarray, mode: str) -> tuple[float, float]:
    """Root of the master's subject (alpha > 16) with a support band scaled to its height."""
    box = forge_core.subject_bbox(alpha)
    if box is None:
        raise ValueError("The master has no visible pixels (alpha > 16).")
    band = max(2, round_half_up(0.04 * (box[3] - box[1])))
    return forge_core.anchor_from_mask(forge_core.subject_mask(alpha), mode, band_rows=band)


def fit_scale(subject_box: Sequence[float], anchor: Sequence[float], root: Sequence[float],
              canvas: Sequence[int], margin: float, height_fraction: float) -> float:
    """Largest scale that keeps the subject box inside the canvas margins, capped at the target height."""
    x0, y0, x1, y1 = subject_box
    ax, ay = anchor
    rx, ry = root
    width, height = canvas
    limits = [height_fraction * height / (y1 - y0)]
    for extent, room in ((ax - x0, rx - margin), (x1 - ax, width - margin - rx),
                         (ay - y0, ry - margin), (y1 - ay, height - margin - ry)):
        if extent > 0:
            limits.append(room / extent)
    scale = min(limits)
    if scale <= 0:
        raise ValueError(f"The root ({rx:g}, {ry:g}) leaves no room for the subject inside the {margin:g} px margin.")
    return scale


def place_master(view: np.ndarray, scale: float, offset: Sequence[float], canvas: Sequence[int],
                 pixel_art: bool) -> np.ndarray:
    """The master on a transparent provider canvas: source pixel (0, 0) lands on ``offset``."""
    resampler = "nearest" if pixel_art else "lanczos"
    placed = forge_core.resample_rgba(view, scale, resampler, anchor_src=(0.0, 0.0),
                                      anchor_dst=(float(offset[0]), float(offset[1])), out_size=tuple(canvas))
    return np.asarray(placed)


def composite_on_key(placed: np.ndarray, key_rgb: Sequence[int]) -> np.ndarray:
    """Straight-alpha 'over' onto the flat key: the opaque RGB the provider receives."""
    alpha = placed[..., 3:].astype(np.float64) / 255.0
    key = np.asarray(key_rgb, np.float64)
    mixed = placed[..., :3].astype(np.float64) * alpha + key * (1.0 - alpha)
    return np.floor(mixed + 0.5).astype(np.uint8)


def work_region(offset: Sequence[float], scale: float, size: Sequence[int], padding: Sequence[int],
                canvas: Sequence[int], margin: float) -> tuple[list[int], list[float]]:
    """Footprint of the padded source canvas on the provider canvas, clipped to the margin.

    Returns the half-open integer box and how far (provider px) the padded canvas
    reaches past the margin on each side.
    """
    left, top, right, bottom = padding
    fx0, fy0 = offset[0] - scale * left, offset[1] - scale * top
    fx1, fy1 = offset[0] + scale * (size[0] + right), offset[1] + scale * (size[1] + bottom)
    width, height = canvas
    box = [max(int(math.ceil(margin)), int(math.floor(fx0))), max(int(math.ceil(margin)), int(math.floor(fy0))),
           min(int(math.floor(width - margin)), int(math.ceil(fx1))),
           min(int(math.floor(height - margin)), int(math.ceil(fy1)))]
    clipped = [max(0.0, margin - fx0), max(0.0, margin - fy0), max(0.0, fx1 - (width - margin)),
               max(0.0, fy1 - (height - margin))]
    return box, [round(value, 3) for value in clipped]


# --------------------------------------------------------------------------- key colour

def key_rgb(key: str) -> tuple[int, int, int]:
    rgb = forge_matte.DECLARED_KEYS.get(key)
    if rgb is not None:
        return tuple(rgb)
    return tuple(int(key[i:i + 2], 16) for i in (1, 3, 5))


def normalise_key(text: str) -> str:
    value = str(text).strip().lower()
    if value in forge_matte.DECLARED_KEYS or value == "auto":
        return value
    digits = value[1:] if value.startswith("#") else value
    if re.fullmatch(r"[0-9a-f]{6}", digits):
        return "#" + digits
    raise argparse.ArgumentTypeError(f"--key needs auto, magenta, green, blue or #rrggbb; got {text!r}")


def choose_key(view: np.ndarray, requested: str) -> tuple[str, dict[str, Any]]:
    """Pick the key (auto) or check the requested one against the master's own colours."""
    candidates = ("magenta", "green", "blue") if requested == "auto" else (requested,)
    report = forge_matte.choose_key_color(view, candidates)
    rows = [{"key": row["key"], "overlapShare": round(row["overlap_share"], 6), "rejected": row["rejected"]}
            for row in report["candidates"]]
    key = report["key"] if requested == "auto" else requested
    return key, {"requested": requested, "status": report["status"], "candidates": rows, "rule": report["rule"]}


def describe_key(key: str) -> tuple[str, str]:
    """(prompt description, colour word for the key-coloured-light negative)."""
    r, g, b = key_rgb(key)
    if key in forge_matte.DECLARED_KEYS:
        return f"flat pure {key.upper()} RGB({r}, {g}, {b})", key
    return f"flat RGB({r}, {g}, {b}) ({key})", "key"


# --------------------------------------------------------------------------- opaque master

def key_opaque_master(sheet: np.ndarray, requested: str, pixel_art: bool) -> tuple[np.ndarray, dict[str, Any]]:
    """Key an opaque master on a flat magenta, green or blue backdrop (or the --master-key colour)
    with forge_matte.key_still's soft matte; pixel art keeps binary alpha (threshold 128).

    ``requested`` auto takes the declared key whose border-ring estimate is valid and covers at
    least half of the ring. Raises ValueError when no key backdrop is found or nothing is keyed."""
    if requested == "auto":
        best = None
        for name in forge_matte.DECLARED_KEYS:
            _, estimate = forge_matte.estimate_key(sheet, name)
            if estimate["valid"] and estimate["ring_share"] >= 0.5 and (
                    best is None or estimate["ring_share"] > best[1]["ring_share"]):
                best = (name, estimate)
        if best is None:
            raise ValueError("The master has no transparent pixels and no flat magenta, green or blue backdrop. "
                             "Pass the approved RGBA cut-out (for example generate2dsprite process output), or "
                             "name its backdrop with --master-key.")
        backdrop = best[0]
    else:
        backdrop = requested
    keyed_image, info = forge_matte.key_still(sheet, quality="soft", key=backdrop)
    keyed = np.array(keyed_image.convert("RGBA"), dtype=np.uint8)
    if pixel_art:
        keyed[..., 3] = np.where(keyed[..., 3] >= 128, 255, 0).astype(np.uint8)
    if int(keyed[..., 3].min()) == 255:
        raise ValueError(f"Keying the opaque master on {backdrop} removed nothing; pass the RGBA cut-out "
                         "or the right --master-key.")
    qa = info.get("qa", {})
    record = {"method": "forge_matte.key_still (soft matte)" + (", binary alpha at 128" if pixel_art else ""),
              "backdrop": backdrop, "requested": requested,
              "keyRgb": info.get("key"), "quality": info.get("quality"),
              "transparentShare": round(float((keyed[..., 3] == 0).mean()), 6),
              "qa": {name: qa[name] for name in ("opaque_key_px", "outer_ring_spill_fraction", "enclosed_key_pockets",
                                                 "key_hued_px") if name in qa}}
    return keyed, record


# --------------------------------------------------------------------------- prompt

def _round1(value: float) -> float:
    return math.floor(value * 10 + 0.5) / 10


def retime(seconds: float, duration: float) -> float:
    """Template time for a take of ``duration`` s; the early calm span keeps its length."""
    if seconds <= CALM_SPAN_S:
        return _round1(seconds)
    span = (duration - CALM_SPAN_S) / (TEMPLATE_DURATION_S - CALM_SPAN_S)
    return _round1(CALM_SPAN_S + (seconds - CALM_SPAN_S) * span)


def timeline(template: ActionTemplate, duration: float) -> list[dict[str, Any]]:
    rows = []
    for start, end, text in template.timeline:
        first, last = retime(start, duration), retime(end, duration)
        if last <= first:
            raise ValueError(f"A {duration:g} s take is too short for the {template.name} timeline.")
        rows.append({"startS": first, "endS": last, "text": text})
    return rows


def _seconds(value: float) -> str:
    return f"{value:.1f}"


_HEAD_BREAK = re.compile(r"\s*(?:[,;(]|\b(?:with|in|of|wearing|holding|carrying|on|who|that)\b)", re.I)
_ARTICLE = re.compile(r"^(?:a|an|the|this|one)\s+", re.I)


def subject_head(subject: str) -> str:
    """The subject phrase before its first 'with/in/of/...' clause, without a leading article
    ('side-view adventurer with a scarf' -> 'side-view adventurer'); the whole phrase when nothing is left."""
    text = " ".join(str(subject).split())
    head = _HEAD_BREAK.split(text, maxsplit=1)[0].strip()
    head = _ARTICLE.sub("", head).strip() or _ARTICLE.sub("", text).strip()
    return head or text


def build_prompt(template: ActionTemplate, *, subject: str, facing: str | None, key: str,
                 region: Sequence[int], canvas: Sequence[int], root: Sequence[float], duration: float,
                 rows: Sequence[dict[str, Any]], pixel_art: bool, extra: str | None) -> str:
    """The motion prompt: identity, locked camera, key, numeric work region, root, motion,
    timeline, body-only rule and negatives, one block per line."""
    key_text, key_word = describe_key(key)
    noun = {"actor": subject or "character", "object": subject or "object", "effect": subject or "game effect"}[
        template.role]
    facing_text = ""
    if facing:
        facing_text = f" (facing {_FACING_WORDS.get(facing.strip().lower(), facing.strip())})"
    style = ("its hand-drawn pixel clusters and flat colours, with no smoothing, realistic shading or 3D redesign"
             if pixel_art else "its drawing style and texture, with no realistic 3D redesign")
    x0, y0, x1, y1 = region
    rx, ry = (round_half_up(value) for value in root)
    width, height = canvas
    lines = [
        f"Animate this EXACT isolated {noun}. Preserve its identity, silhouette, proportions, colours, number of "
        f"limbs and existing facing direction{facing_text}; keep {style}.",
        "LOCKED camera: fixed framing and fixed scale for the whole clip; no zoom, push-in, pull-out, pan, tilt, "
        "shake, rotation toward the camera or cut.",
        f"Everything behind the {subject_head(noun)} stays a perfectly uniform {key_text} for the whole clip: no "
        f"floor, contact shadow, horizon, reflection, gradient, vignette, scenery, text or {key_word}-coloured "
        "light.",
        f"The ENTIRE motion, including hair, cloth, weapons and every appendage, stays inside the work region "
        f"x={x0}..{x1 - 1}, y={y0}..{y1 - 1} of this {width}x{height} image, with clear margins; no visible guide "
        "borders.",
    ]
    if template.role == "actor":
        lines.append(f"Keep the root fixed at pixel ({rx}, {ry}): the feet stay on the same invisible ground line "
                     f"at y={ry}.")
    elif template.role == "object":
        lines.append(f"Keep the base fixed at pixel ({rx}, {ry}).")
    else:
        lines.append(f"Keep the effect centred on pixel ({rx}, {ry}).")
    lines.append(template.motion)
    steps = "; ".join(f"{_seconds(row['startS'])}-{_seconds(row['endS'])} s {row['text']}" for row in rows)
    lines.append(f"Timeline for this {duration:g}-second take: {steps}.")
    if template.role == "actor":
        lines.append("Body only: animate only the body, clothing and attached equipment; the game adds effects "
                     "separately, so no particles, sparks, energy, rings, starbursts, pulsing glow, speed lines or "
                     "dust.")
    lines.append(f"Avoid: {template.avoid}; no extra limbs, new objects, text or camera motion.")
    if extra:
        lines.append(extra.strip())
    return "\n".join(lines) + "\n"


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    """Replace {work_region}, {root}, {canvas}, {key}, {timeline} and {duration} in a custom prompt."""
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    return text


# --------------------------------------------------------------------------- lint

_NEGATORS = {"no", "not", "never", "without", "avoid", "avoids", "nor", "zero", "don't", "dont", "cannot", "neither"}
WEAK_AMPLITUDE = ("tiny", "extremely slow", "very slow", "very fine", "very subtle", "barely", "imperceptible",
                  "nearly static", "almost static", "almost still", "slow motion", "slow-motion",
                  "micro-movement", "micro movement")
CAMERA_MOVES = ("zoom in", "zooms in", "zoom out", "push in", "push-in", "pushes in", "pull out", "pull-out",
                "dolly", "pan across", "panning", "tracking shot", "camera follows", "camera moves", "orbit",
                "crane shot", "handheld")
ALPHA_REQUESTS = ("transparent background", "transparent backdrop", "alpha channel", "alpha background")
_KEY_HUES = {"magenta": ("magenta", "pink", "purple", "violet", "fuchsia"), "green": ("green", "lime"),
             "blue": ("blue", "cyan")}
_LIGHT_WORDS = r"(?:light|lights|glow|glows|glowing|aura|flash|flashes|sparks?|sparkles?|haze|mist|smoke|reflections?)"


def _negated(text: str, start: int) -> bool:
    """True when a negator word precedes ``start`` within the same clause."""
    clause_start = max(text.rfind(mark, 0, start) for mark in (".", ";", ":", "!", "?", "\n")) + 1
    words = re.findall(r"[a-z']+", text[clause_start:start].lower())
    return any(word in _NEGATORS for word in words)


def _find(text: str, phrases: Sequence[str]) -> list[tuple[str, int]]:
    lowered = text.lower()
    hits = []
    for phrase in phrases:
        for match in re.finditer(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", lowered):
            hits.append((phrase, match.start()))
    return sorted(hits, key=lambda hit: hit[1])


def lint_prompt(text: str, key: str | None = None) -> list[dict[str, str]]:
    """Warnings for wording that weakens or breaks an image-to-video take (hd2d MOTION-v2: 'very fine',
    'tiny' and 'extremely slow' made the generated motion invisible at game size)."""
    findings: list[dict[str, str]] = []

    def add(rule: str, message: str, match: str | None = None) -> None:
        finding = {"rule": rule, "message": message}
        if match:
            finding["match"] = match
        findings.append(finding)

    for phrase, start in _find(text, WEAK_AMPLITUDE):
        if not _negated(text, start):
            add("weak-amplitude", f"'{phrase}' shrinks the generated motion until it is invisible at game size; "
                "ask for clearly visible motion with a cycle length instead", phrase)
    for phrase, start in _find(text, CAMERA_MOVES):
        if not _negated(text, start):
            add("camera-move", f"'{phrase}' asks for camera motion; registration by construction needs a locked "
                "camera", phrase)
    for phrase, start in _find(text, ALPHA_REQUESTS):
        if not _negated(text, start):
            add("alpha-request", f"'{phrase}': providers return opaque video; ask for the flat key colour", phrase)
    lowered = text.lower()
    if not re.search(r"\b(locked|fixed|static)[- ]?(off )?camera\b|\bcamera[^.;\n]{0,20}\b(locked|fixed|static)\b"
                     r"|\blocked[- ]off\b", lowered):
        add("missing-locked-camera", "say 'locked camera, fixed framing and fixed scale'")
    if not (re.search(r"\bx\s*=\s*\d+\s*\.\.\s*\d+", lowered) and re.search(r"\by\s*=\s*\d+\s*\.\.\s*\d+", lowered)):
        add("missing-work-region", "give the numeric work region, e.g. 'inside the work region x=316..965, "
            "y=20..646 of this 1280x720 image'")
    if not re.search(r"\b\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*s\b", lowered):
        add("missing-timeline", "give a timeline in seconds, e.g. '0.0-0.4 s hold the reference pose; ...'")
    if key in _KEY_HUES:
        pattern = r"(?<![a-z])(" + "|".join(_KEY_HUES[key]) + r")[- ]" + _LIGHT_WORDS + r"(?![a-z])"
        for match in re.finditer(pattern, lowered):
            if not _negated(text, match.start()):
                add("key-coloured-light", f"'{match.group(0)}' is close to the {key} key and will be keyed out "
                    "with the background", match.group(0))
    return findings


# --------------------------------------------------------------------------- review guide

def review_guide(rgb: np.ndarray, region: Sequence[int], root: Sequence[float], margin: float) -> Image.Image:
    """input.png with the margin, the work region and the root drawn on it (never send it to the provider)."""
    image = Image.fromarray(rgb, "RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    inset = int(math.ceil(margin))
    draw.rectangle([inset, inset, width - 1 - inset, height - 1 - inset], outline=(128, 128, 128))
    x0, y0, x1, y1 = region
    draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(255, 220, 0), width=2)
    rx, ry = (round_half_up(value) for value in root)
    draw.line([rx - 12, ry, rx + 12, ry], fill=(0, 230, 255), width=2)
    draw.line([rx, ry - 12, rx, ry + 12], fill=(0, 230, 255), width=2)
    return image


# --------------------------------------------------------------------------- commands

def _ascii(text: Any) -> str:
    return forge_core.ascii_text(str(text))


def cmd_prepare(args: argparse.Namespace) -> dict[str, Any]:
    """Build the job in a stage, run its placement QC, then publish the folder."""
    template = ACTIONS[args.action]
    master_path = Path(args.master)
    master_image, info = forge_core.load_rgba(master_path)
    sheet = np.asarray(master_image)
    origin = (0, 0)
    view = sheet
    if args.view_box:
        x0, y0, x1, y1 = args.view_box
        if x1 > sheet.shape[1] or y1 > sheet.shape[0]:
            raise ValueError(f"--view-box {list(args.view_box)} lies outside the "
                             f"{sheet.shape[1]}x{sheet.shape[0]} master.")
        view = sheet[y0:y1, x0:x1].copy()
        origin = (x0, y0)
    master_keying = None
    if int(view[..., 3].min()) == 255:
        # an opaque master on a key backdrop (a previous input.png, a generated still): key it once
        sheet, master_keying = key_opaque_master(sheet, args.master_key, args.pixel_art)
        master_keying.update({"sourceName": master_path.name, "sourceSha256": info["sha256"]})
        view = sheet[origin[1]:origin[1] + view.shape[0], origin[0]:origin[0] + view.shape[1]].copy()
        if int(view[..., 3].min()) == 255:
            raise ValueError("The --view-box of the keyed master still has no transparent pixels; "
                             "pass the RGBA cut-out.")
    size = (view.shape[1], view.shape[0])
    subject_box = forge_core.subject_bbox(view[..., 3])
    if subject_box is None:
        raise ValueError("The master has no visible pixels (alpha > 16).")
    if args.anchor is not None:
        anchor = (args.anchor[0] - origin[0], args.anchor[1] - origin[1])
        anchor_mode = "explicit"
    elif template.role == "effect":
        anchor, anchor_mode = (size[0] / 2, size[1] / 2), "canvas-center"
    else:
        anchor, anchor_mode = measure_anchor(view[..., 3], args.anchor_mode), args.anchor_mode
    if not (0 <= anchor[0] <= size[0] and 0 <= anchor[1] <= size[1]):
        raise ValueError(f"The anchor {list(anchor)} lies outside the {size[0]}x{size[1]} source canvas.")

    canvas = tuple(args.canvas)
    margin = float(args.margin)
    root = tuple(args.root) if args.root else default_root(canvas, template, margin)
    if not (margin <= root[0] <= canvas[0] - margin and margin <= root[1] <= canvas[1] - margin):
        raise ValueError(f"The root ({root[0]:g}, {root[1]:g}) lies outside the {canvas[0]}x{canvas[1]} canvas margin.")
    if args.scale is None:
        fit_margin = margin + (1.0 if args.pixel_art else BLUR_ALLOWANCE)  # offset rounding / edge blur
        scale = fit_scale(subject_box, anchor, root, canvas, fit_margin, args.subject_height)
        if args.pixel_art:
            scale = int(math.floor(scale + 1e-9))
            if scale < 1:
                raise ValueError("At integer scale 1 the pixel-art master does not fit the canvas; "
                                 "use a larger --canvas or a smaller master.")
    else:
        scale = forge_core.integer_scale(args.scale) if args.pixel_art else args.scale
    if args.pixel_art:
        offset = (round_half_up(root[0] - scale * anchor[0]), round_half_up(root[1] - scale * anchor[1]))
    else:
        offset = (root[0] - scale * anchor[0], root[1] - scale * anchor[1])
    actual_root = (offset[0] + scale * anchor[0], offset[1] + scale * anchor[1])

    key, key_choice = choose_key(view, args.key)
    if key_choice["status"] == "conflict" and not args.allow_key_conflict:
        shares = ", ".join(f"{row['key']} {row['overlapShare']:.3f}" for row in key_choice["candidates"])
        raise ValueError(f"The master's own colours fight the key ({shares} of subject pixels lean to it). "
                         "Pick another --key (green or blue for purple and pink designs), or pass "
                         "--allow-key-conflict and protect those colours when keying.")
    rgb_key = key_rgb(key)

    placed = place_master(view, scale, offset, canvas, args.pixel_art)
    placed_box = forge_core.subject_bbox(placed[..., 3])
    inside = (placed_box is not None and placed_box[0] >= margin and placed_box[1] >= margin
              and placed_box[2] <= canvas[0] - margin and placed_box[3] <= canvas[1] - margin)
    if not inside:
        raise ValueError(f"Placement QC failed: at scale {scale:g} with the root at ({actual_root[0]:g}, "
                         f"{actual_root[1]:g}) the subject box {placed_box} leaves the {margin:g} px margin of the "
                         f"{canvas[0]}x{canvas[1]} canvas. Lower --scale, move --root or use a larger --canvas.")
    input_rgb = composite_on_key(placed, rgb_key)

    padding = tuple(args.action_padding) if args.action_padding else default_padding(template, size)
    region, clipped = work_region(offset, scale, size, padding, canvas, margin)
    if region[2] <= region[0] or region[3] <= region[1]:
        raise ValueError("The work region is empty; check --root, --scale and --margin.")
    rows = timeline(template, args.duration)
    region_text = f"x={region[0]}..{region[2] - 1}, y={region[1]}..{region[3] - 1}"
    if args.prompt_file:
        custom = Path(args.prompt_file).read_text(encoding="utf-8-sig")
        prompt = fill_placeholders(custom, {
            "work_region": region_text, "canvas": f"{canvas[0]}x{canvas[1]}",
            "root": f"({round_half_up(actual_root[0])}, {round_half_up(actual_root[1])})",
            "key": describe_key(key)[0], "duration": f"{args.duration:g}",
            "timeline": "; ".join(f"{_seconds(r['startS'])}-{_seconds(r['endS'])} s {r['text']}" for r in rows)})
        if not prompt.endswith("\n"):
            prompt += "\n"
    else:
        prompt = build_prompt(template, subject=args.subject, facing=args.facing, key=key, region=region,
                              canvas=canvas, root=actual_root, duration=args.duration, rows=rows,
                              pixel_art=args.pixel_art, extra=args.extra)
    findings = lint_prompt(prompt, key)
    if findings and args.strict_lint:
        raise ValueError("Prompt lint failed (--strict-lint): " + "; ".join(
            f"{item['rule']}: {item['message']}" for item in findings))
    warnings = [f"lint {item['rule']}: {item['message']}" for item in findings]
    if master_keying is not None:
        warnings.append(f"the master was opaque: keyed its {master_keying['backdrop']} backdrop with "
                        "forge_matte.key_still (masterKeying in the job); check master.png over light and dark")
    if key_choice["status"] == "conflict":
        warnings.append(f"key {key} overlaps the master's colours (--allow-key-conflict)")
    subject_height = (placed_box[3] - placed_box[1]) / canvas[1]
    if subject_height < 0.25:
        warnings.append(f"the subject fills only {subject_height:.0%} of the canvas height; generators "
                        "under-animate small subjects, so raise --scale")

    master_name = "master.png" if master_keying is not None else "master" + (master_path.suffix.lower() or ".png")
    prompt_bytes = prompt.encode("utf-8")
    job: dict[str, Any] = {
        "schema": JOB_SCHEMA,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "action": template.name,
        "actionKind": template.kind,
        "returnsToRest": template.returns_to_rest,
        "master": {"path": master_name, "sha256": info["sha256"], "size": [sheet.shape[1], sheet.shape[0]],
                   "anchor": [anchor[0] + origin[0], anchor[1] + origin[1]], "sourceName": master_path.name},
        "sourceSize": list(size),
        "sourceAnchor": [anchor[0], anchor[1]],
        "anchorMode": anchor_mode,
        "referenceCanvas": list(canvas),
        "referenceScale": scale,
        "referenceOffset": [offset[0], offset[1]],
        "referenceRoot": [actual_root[0], actual_root[1]],
        "pixelArt": bool(args.pixel_art),
        "placementResampler": "nearest" if args.pixel_art else "lanczos",
        "keyColor": key,
        "keyChoice": key_choice,
        "workRegion": region,
        "workRegionClipped": clipped,
        "margin": margin,
        "padding": list(padding),
        "durationS": args.duration,
        "calmSpanS": CALM_SPAN_S,
        "timeline": rows,
        "prompt": {"path": "prompt.txt", "sha256": forge_core.sha256_bytes(prompt_bytes)},
        "lint": findings,
        "warnings": warnings,
    }
    if args.view_box:
        job["master"]["viewBox"] = list(args.view_box)
    if master_keying is not None:
        job["masterKeying"] = master_keying
    output = Path(args.output_dir)
    with forge_core.staged_output(output) as stage:
        if master_keying is not None:  # the job's master is the keyed RGBA cut-out
            forge_core.save_png(sheet, stage / master_name)
            job["master"]["sha256"] = forge_core.sha256_file(stage / master_name)
        else:
            shutil.copyfile(master_path, stage / master_name)
        forge_core.save_png(input_rgb, stage / "input.png")
        (stage / "prompt.txt").write_bytes(prompt_bytes)
        guide = review_guide(input_rgb, region, actual_root, margin)
        forge_core.save_png(np.asarray(guide), stage / "review-guide.png")
        job["input"] = {"path": "input.png", "sha256": forge_core.sha256_file(stage / "input.png"),
                        "size": list(canvas)}
        job["reviewGuide"] = {"path": "review-guide.png", "sha256": forge_core.sha256_file(stage / "review-guide.png"),
                              "note": "review aid only; never send it to the provider"}
        forge_core.write_json(stage / JOB_FILE, job)
    for warning in warnings:
        print("warning: " + _ascii(warning), file=sys.stderr)
    final = output.parent.resolve() / output.name
    return {"output": str(final), "metadata": str(final / JOB_FILE), "input": str(final / "input.png"),
            "prompt": str(final / "prompt.txt"), "action": template.name, "referenceScale": scale,
            "referenceRoot": [actual_root[0], actual_root[1]], "keyColor": key, "workRegion": region,
            "padding": list(padding), "warnings": len(warnings)}


def cmd_lint(args: argparse.Namespace) -> dict[str, Any]:
    text = Path(args.prompt_file).read_text(encoding="utf-8-sig") if args.prompt_file else args.text
    findings = lint_prompt(text, args.key)
    if findings and args.strict:
        raise ValueError("Prompt lint failed: " + "; ".join(f"{item['rule']}: {item['message']}" for item in findings))
    return {"status": "warn" if findings else "pass", "findings": findings}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    pp = sub.add_parser("prepare", help="write input.png, prompt.txt and registration_job.json for one action")
    pp.add_argument("--master", required=True,
                    help="approved RGBA master (or multi-view sheet) PNG; an opaque one on a flat key backdrop is keyed")
    pp.add_argument("--view-box", type=parse_box, help="X0,Y0,X1,Y1: one view of a multi-view sheet (half-open, px)")
    pp.add_argument("--action", required=True, choices=sorted(ACTIONS), help="timeline template")
    pp.add_argument("--output-dir", required=True, help="new folder for the job (must not exist)")
    pp.add_argument("--anchor", type=parse_point, help="root X,Y in master px (default: measured from alpha)")
    pp.add_argument("--anchor-mode", choices=forge_core.ANCHOR_MODES, default="stance",
                    help="how the root is measured when --anchor is not given (default stance)")
    pp.add_argument("--canvas", type=parse_size, default=DEFAULT_CANVAS, help="provider canvas W,H (default 1280,720)")
    pp.add_argument("--root", type=parse_point,
                    help="fixed pixel for the root on the canvas (default W/2, 0.861*H; effects: the centre)")
    pp.add_argument("--scale", type=parse_scale, default=None,
                    help="master px to canvas px: auto (default) fits the subject to --subject-height "
                         "inside the margin")
    pp.add_argument("--subject-height", type=float, default=SUBJECT_HEIGHT_FRACTION,
                    help="auto scale target: subject height as a share of the canvas height (default 0.736)")
    pp.add_argument("--pixel-art", action="store_true", help="integer NEAREST scaling at an integer offset")
    pp.add_argument("--margin", type=float, default=DEFAULT_MARGIN, help="free canvas border in px (default 20)")
    pp.add_argument("--key", type=normalise_key, default="auto",
                    help="auto (a key the master does not use), magenta, green, blue or #rrggbb")
    pp.add_argument("--master-key", type=normalise_key, default="auto",
                    help="backdrop of an opaque master: auto (detect a flat magenta, green or blue border), "
                         "magenta, green, blue or #rrggbb; it is keyed with forge_matte.key_still and the job "
                         "records masterKeying (an RGBA master with transparency is used as it is)")
    pp.add_argument("--allow-key-conflict", action="store_true",
                    help="keep a key that the master's own colours lean to (recorded as a warning)")
    pp.add_argument("--action-padding", type=parse_padding,
                    help="L,T,R,B source px of motion room around the master canvas (default per action)")
    pp.add_argument("--duration", type=float, default=TEMPLATE_DURATION_S,
                    help="take length in seconds the timeline is written for (default 6)")
    pp.add_argument("--subject", default="",
                    help="identity description, e.g. 'compact pixel-art knight with a red scarf'")
    pp.add_argument("--facing", help="facing direction: left, right, front, back or free text")
    pp.add_argument("--extra", help="extra action wording appended to the prompt")
    pp.add_argument("--prompt-file", help="custom prompt; {work_region} {root} {canvas} {key} {timeline} {duration} "
                                          "are filled in")
    pp.add_argument("--strict-lint", action="store_true", help="lint warnings fail the run (nothing is written)")
    pp.set_defaults(func=cmd_prepare)

    pl = sub.add_parser("lint", help="check a motion prompt (prints JSON)")
    source = pl.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt-file", help="prompt text file")
    source.add_argument("--text", help="prompt text")
    pl.add_argument("--key", type=normalise_key, help="key colour, to catch key-coloured light")
    pl.add_argument("--strict", action="store_true", help="exit 1 when there is any finding")
    pl.set_defaults(func=cmd_lint)
    return parser


def _check_args(args: argparse.Namespace) -> None:
    if args.command != "prepare":
        return
    if not MIN_DURATION_S <= args.duration <= MAX_DURATION_S:
        raise ValueError(f"--duration must be {MIN_DURATION_S:g}-{MAX_DURATION_S:g} s; got {args.duration:g}")
    if not 0.05 <= args.subject_height <= 0.95:
        raise ValueError(f"--subject-height must be 0.05-0.95; got {args.subject_height:g}")
    if not 0 <= args.margin < min(args.canvas) / 4:
        raise ValueError(f"--margin must be 0 to a quarter of the canvas; got {args.margin:g}")


def run(argv: Sequence[str] | None = None) -> dict[str, Any]:
    """Parse argv and run the command; returns the JSON summary (raises on errors)."""
    args = build_parser().parse_args(argv)
    _check_args(args)
    return args.func(args)


def _run(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(run(argv), ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors exit 2; refused input and failed placement QC exit 1 with ``error: ...``; anything
    unexpected prints ``error: internal error (...)`` (D26, D27; forge_core.run_cli)."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
