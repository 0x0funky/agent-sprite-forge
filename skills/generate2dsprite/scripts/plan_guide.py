#!/usr/bin/env python3
"""Plan a sprite sheet for the host image tool and draw a layout and pose guide (opt-in).

The host image tool keeps the requested aspect and returns about 1,572,864 px
(3:2 -> 1536x1024, 16:9 -> 1672x941, 1:1 -> 1254x1254, 2:1 -> 1774x887), so
prompts should name an aspect, never pixel sizes. This tool predicts that size,
picks rows and columns that give each pose the most room inside a 15% safe
frame, and writes into a new --output-dir:

  guide.png            layout and pose reference to attach to the generation call:
                       grey gutters, blue safe boxes, black ground line, root ticks
                       and, for --cycle run|walk, a NEAR (orange) / FAR (blue) leg
                       skeleton whose leading leg alternates between the halves
  guide.svg            the same drawing as vector art
  guide-annotated.png  the same with phase labels, for people (do not attach)
  prompt.txt           an aspect-only prompt block that explains the guide
  sheet-plan.json      the plan, the candidate layouts and a self-check

The self-check runs sheet_qc's leading-leg test on the guide's own skeleton and
checks that every pose stays inside its safe box; a failure exits 1 and
publishes nothing. Usage errors exit 2 (argparse); every other error prints one
"error: ..." line and exits 1. The guide is a generation aid that has not been A/B tested
with an image model: use it when a sheet keeps crossing cells or repeating the
leading leg, not as a default.

Run from the project root, for example:
  python "<skill-dir>/scripts/plan_guide.py" --frames 8 --cycle run --facing right --output-dir guides/hero-run
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)
import sheet_qc  # noqa: E402  (sibling: the leading-leg test used by the self-check)

TOOL = {"name": "plan_guide.py", "version": forge_core.FORGE_PACKAGE_VERSION}  # the package version (D29)
SCHEMA = "generate2dsprite.sheet_plan.v1"
PLAN_NAME = "sheet-plan.json"
HOST_BUDGET_PX = 1536 * 1024  # observed host image area (3 of 3 calls within +-0.031%, report v2 3.3.1)
# Most cells a planned grid may hold: about 39 px per cell on a 1.5-megapixel host image. It bounds --frames and
# the layout search, which spun for minutes on --frames 99999999 (r2-conventions finding 13).
MAX_CELLS = 1024
MAX_BUDGET_PX = 4096 * 4096  # largest host image the guides are drawn for (they are drawn at the predicted size)
STANDARD_ASPECTS = ("3:2", "2:3", "1:1", "16:9", "9:16", "2:1", "1:2")
CYCLES = ("run", "walk", "none")
NEAR = (255, 122, 0)
FAR = (32, 80, 160)
BODY = (110, 110, 110)
GUTTER = (200, 200, 200)
SAFE = (0, 170, 255)
INK = (0, 0, 0)

# Key poses of one half-cycle for a side view facing right. Angles are degrees from straight down,
# positive forward: (thigh, knee bend) for leg A (the contact leg of this half) and leg B. In the
# first half A is NEAR, in the second half A is FAR, so the leading leg alternates.
# Columns: name, A, B, body lift in thigh lengths, A on the ground, B on the ground.
RUN_KEYS = (
    ("contact", (30, 5), (-28, 75), 0.00, True, False),
    ("down", (5, 50), (-10, 110), 0.00, True, False),
    ("passing", (-15, 10), (22, 95), 0.00, True, False),
    ("flight", (-34, 35), (36, 15), 0.14, False, False),
)
WALK_KEYS = (
    ("contact", (24, 4), (-24, 18), 0.00, True, False),
    ("down", (12, 22), (-20, 55), 0.00, True, False),
    ("passing", (-4, 6), (14, 70), 0.00, True, False),
    ("up", (-18, 4), (26, 30), 0.00, True, False),
)
PHASE_TEXT = {
    "contact": "{a} leg reaches forward, heel on the ground line; {b} leg trails{b_lift}",
    "down": "{a} leg under the body, knee bent, carrying the weight; {b} leg folds up behind",
    "passing": "{a} leg pushes off behind; {b} knee swings forward past it",
    "flight": "both feet off the ground; {b} leg reaches forward for the next contact, {a} leg trails",
    "up": "{a} leg straightens under the body (highest hip); {b} foot swings forward, just clear of the ground",
}


class PlanError(ValueError):
    """A user-facing problem, printed as 'error: ...'."""


# --------------------------------------------------------------------------- planning

def parse_aspect(text: str) -> tuple[int, int]:
    """'3:2' -> (3, 2)."""
    try:
        width, height = (int(part) for part in text.split(":"))
    except ValueError:
        raise PlanError(f"Aspects look like 3:2; got {text!r}.") from None
    if width < 1 or height < 1:
        raise PlanError(f"Aspects need positive sides; got {text!r}.")
    return width, height


def parse_ratio(text: str) -> float:
    """'0.98', '384/391' or '4:5' -> width / height."""
    try:
        value = float(Fraction(text.replace(":", "/")))
    except (ValueError, ZeroDivisionError):
        raise PlanError(f"--envelope-aspect is width/height such as 1.0, 384/391 or 4:5; got {text!r}.") from None
    if not 0.1 <= value <= 10:
        raise PlanError(f"--envelope-aspect must be between 0.1 and 10; got {text!r}.")
    return value


def predict_size(aspect: tuple[int, int], budget: int = HOST_BUDGET_PX) -> tuple[int, int]:
    """Host output size for a requested aspect: the aspect is kept and the area is about ``budget`` px."""
    ratio = aspect[0] / aspect[1]
    return forge_core.round_half_up(math.sqrt(budget * ratio)), forge_core.round_half_up(math.sqrt(budget / ratio))


def candidate_layouts(frames: int, aspects: Sequence[str], *, envelope_aspect: float, safe: float,
                      budget: int = HOST_BUDGET_PX, max_empty: int = 1) -> list[dict]:
    """Every rows x cols grid holding ``frames`` poses (at most ``max_empty`` spare cells, at most MAX_CELLS
    cells) for each aspect, largest motion envelope first (report v2 plan_and_guide).

    Only the columns that fit are visited, ceil(frames / rows) up to cells / rows, so the search is
    linear in the cell count instead of quadratic."""
    layouts = []
    cells_max = min(frames + max_empty, MAX_CELLS)
    for order, text in enumerate(aspects):
        aspect = parse_aspect(text)
        width, height = predict_size(aspect, budget)
        for rows in range(1, cells_max + 1):
            for cols in range(-(-frames // rows), cells_max // rows + 1):
                cell_w, cell_h = width / cols, height / rows
                safe_w, safe_h = cell_w * (1 - 2 * safe), cell_h * (1 - 2 * safe)
                envelope_h = min(safe_h, safe_w / envelope_aspect)
                layouts.append({
                    "aspect": text, "predicted_size": [width, height], "rows": rows, "cols": cols,
                    "empty_cells": rows * cols - frames, "cell": [round(cell_w, 1), round(cell_h, 1)],
                    "divides_exactly": width % cols == 0 and height % rows == 0,
                    "safe_box": [round(safe_w, 1), round(safe_h, 1)],
                    "envelope_px": [round(envelope_h * envelope_aspect, 1), round(envelope_h, 1)],
                    "_order": order,
                })
    layouts.sort(key=lambda item: (-item["envelope_px"][1], item["empty_cells"], item["_order"], item["rows"]))
    for item in layouts:
        item.pop("_order")
    return layouts


# --------------------------------------------------------------------------- skeleton

def gait_phases(frames: int, cycle: str) -> list[dict]:
    """Per-frame skeleton parameters: NEAR/FAR thigh and knee angles, lift and ground contacts.

    Each half-cycle samples the four key poses evenly (in-betweens interpolate
    the angles); the second half repeats the first with NEAR and FAR swapped.
    """
    keys = RUN_KEYS if cycle == "run" else WALK_KEYS
    half = frames // 2
    phases = []
    for index in range(frames):
        second = index >= half
        position = (index % half) * len(keys) / half
        low = int(math.floor(position))
        mix = position - low
        start = keys[low]
        if low + 1 < len(keys):
            end, swap = keys[low + 1], False
        else:  # the last key blends into the next half's contact, where A and B trade places
            end, swap = keys[0], True
        a_end, b_end = (end[2], end[1]) if swap else (end[1], end[2])
        a = tuple(start[1][k] + mix * (a_end[k] - start[1][k]) for k in range(2))
        b = tuple(start[2][k] + mix * (b_end[k] - start[2][k]) for k in range(2))
        lift = start[3] + mix * (end[3] - start[3])
        a_down = start[4] if mix < 0.5 else (end[5] if swap else end[4])
        b_down = start[5] if mix < 0.5 else (end[4] if swap else end[5])
        name = start[0] if mix == 0 else f"{start[0]}-{'contact' if swap else end[0]}"
        near_is_a = not second
        phases.append({
            "index": index, "name": name, "half": int(second),
            "near": a if near_is_a else b, "far": b if near_is_a else a, "lift": lift,
            "contact_leg": "near" if near_is_a else "far",
            "ground": {"near": a_down if near_is_a else b_down, "far": b_down if near_is_a else a_down},
        })
    return phases


def _leg(hip: tuple[float, float], thigh_deg: float, knee_deg: float, length: float,
         sign: float) -> list[tuple[float, float]]:
    """Hip, knee, ankle and toe of one leg; the shin bends backward from the thigh."""
    thigh = math.radians(thigh_deg)
    knee = (hip[0] + sign * length * math.sin(thigh), hip[1] + length * math.cos(thigh))
    shin = math.radians(thigh_deg - knee_deg)
    ankle = (knee[0] + sign * length * math.sin(shin), knee[1] + length * math.cos(shin))
    return [hip, knee, ankle, (ankle[0] + sign * 0.35 * length, ankle[1])]


def skeleton(phase: dict, *, keys: Sequence[tuple], root_x: float, ground_y: float, length: float,
             sign: float) -> dict:
    """Polylines of one pose: far leg and arm, body, head, near leg and arm (drawing order).

    The hip sits so the planted foot touches the ground line; with no planted
    foot (flight) it rises ``lift`` thigh lengths above the highest planted hip
    of the cycle's key poses.
    """

    def drop(thigh_knee: Sequence[float]) -> float:
        thigh, knee = (math.radians(value) for value in thigh_knee)
        return length * (math.cos(thigh) + math.cos(thigh - knee))

    planted = [drop(phase[side]) for side in ("near", "far") if phase["ground"][side]]
    standing = max(drop(key[1]) for key in keys if key[4])
    hip_y = ground_y - (max(planted) if planted else standing + phase["lift"] * length)
    hip = (root_x, hip_y)
    neck = (hip[0] + sign * 0.12 * length, hip[1] - 1.55 * length)
    head = (neck[0] + sign * 0.15 * length, neck[1] - 0.55 * length)
    shoulder = (neck[0], neck[1] + 0.25 * length)
    parts: dict[str, Any] = {"head": head, "head_r": 0.5 * length, "body": [hip, neck]}
    for side in ("far", "near"):
        parts[f"{side}_leg"] = _leg(hip, *phase[side], length, sign)
        swing = math.radians(-0.9 * phase[side][0])  # arms counter the leg on the same side
        elbow = (shoulder[0] + sign * 0.75 * length * math.sin(swing), shoulder[1] + 0.75 * length * math.cos(swing))
        hand = (elbow[0] + sign * 0.6 * length * math.sin(swing + math.radians(70)),
                elbow[1] + 0.6 * length * math.cos(swing + math.radians(70)) - 0.2 * length)
        parts[f"{side}_arm"] = [shoulder, elbow, hand]
    return parts


def _widths(length: float) -> dict:
    return {"leg": max(3, int(round(length * 0.18))), "arm": max(2, int(round(length * 0.14))),
            "body": max(4, int(round(length * 0.3))), "head": max(2, int(round(length * 0.08)))}


def draw_pose(draw: ImageDraw.ImageDraw, parts: dict, widths: dict) -> None:
    for side, color in (("far", FAR), ("near", NEAR)):
        if side == "near":
            draw.line(parts["body"], fill=BODY, width=widths["body"])
            cx, cy = parts["head"]
            radius = parts["head_r"]
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=BODY, width=widths["head"])
        draw.line(parts[f"{side}_leg"], fill=color, width=widths["leg"], joint="curve")
        draw.line(parts[f"{side}_arm"], fill=color, width=widths["arm"], joint="curve")


def _svg_pose(parts: dict, widths: dict) -> list[str]:
    def points(items: Sequence[Sequence[float]]) -> str:
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in items)

    hexa = sheet_qc.hex_color
    out = []
    for side, color in (("far", FAR), ("near", NEAR)):
        if side == "near":
            out.append(f'<polyline points="{points(parts["body"])}" fill="none" stroke="{hexa(BODY)}" '
                       f'stroke-width="{widths["body"]}" stroke-linecap="round"/>')
            cx, cy = parts["head"]
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{parts["head_r"]:.1f}" fill="none" '
                       f'stroke="{hexa(BODY)}" stroke-width="{widths["head"]}"/>')
        for limb, width in (("leg", widths["leg"]), ("arm", widths["arm"])):
            out.append(f'<polyline points="{points(parts[f"{side}_{limb}"])}" fill="none" stroke="{hexa(color)}" '
                       f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>')
    return out


def _offset(parts: dict, dx: float, dy: float) -> dict:
    """A copy of a pose moved by (dx, dy)."""
    moved = {key: [(x + dx, y + dy) for x, y in value] for key, value in parts.items()
             if key in ("body", "near_leg", "far_leg", "near_arm", "far_arm")}
    moved.update(head=(parts["head"][0] + dx, parts["head"][1] + dy), head_r=parts["head_r"])
    return moved


def _pose_bounds(parts: dict, widths: dict) -> tuple[float, float, float, float]:
    pad = max(widths.values()) / 2.0
    points = [point for key in ("body", "near_leg", "far_leg", "near_arm", "far_arm") for point in parts[key]]
    cx, cy = parts["head"]
    radius = parts["head_r"] + widths["head"] / 2.0
    xs = [x for x, _ in points] + [cx - radius, cx + radius]
    ys = [y for _, y in points] + [cy - radius, cy + radius]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


# --------------------------------------------------------------------------- guide

def build_guide(plan: dict, *, cycle: str, facing: str, safe: float, gutter: float,
                envelope_aspect: float) -> dict:
    """Draw guide.png, the annotated copy and the SVG; return them with the per-cell geometry.

    Cells are forge_core.rounded_grid_boxes of the predicted size; one motion
    envelope (the smallest that fits every cell's safe box) sizes every skeleton,
    so all poses share one scale.
    """
    width, height = plan["predicted_size"]
    rows, cols, frames = plan["rows"], plan["cols"], plan["frames"]
    boxes = forge_core.rounded_grid_boxes(width, height, rows, cols)
    envelope_h = min(min((y1 - y0) * (1 - 2 * safe), (x1 - x0) * (1 - 2 * safe) / envelope_aspect)
                     for x0, y0, x1, y1 in boxes)
    sign = 1.0 if facing == "right" else -1.0
    guide = Image.new("RGB", (width, height), (255, 255, 255))
    annotated = Image.new("RGB", (width, height), (255, 255, 255))
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}">', f'<rect width="{width}" height="{height}" fill="#ffffff"/>']
    phases = gait_phases(frames, cycle) if cycle != "none" else []
    font = ImageFont.load_default()
    cells = []
    for index, (x0, y0, x1, y1) in enumerate(boxes):
        cell_w, cell_h = x1 - x0, y1 - y0
        band = max(2, forge_core.round_half_up(gutter * min(cell_w, cell_h)))
        sx0, sy0 = x0 + safe * cell_w, y0 + safe * cell_h
        sx1, sy1 = x1 - safe * cell_w, y1 - safe * cell_h
        if band >= min(sx0 - x0, sy0 - y0):
            raise PlanError(f"The {band} px gutter reaches the safe box; lower --gutter or raise --safe.")
        ground_y = sy1
        root_x = sx0 + (0.55 if facing == "right" else 0.45) * (sx1 - sx0)
        record: dict[str, Any] = {"cell": [index // cols, index % cols], "box": [x0, y0, x1, y1],
                                  "safe_box": [round(sx0, 1), round(sy0, 1), round(sx1, 1), round(sy1, 1)],
                                  "gutter_px": band, "ground_y": round(ground_y, 1), "root_x": round(root_x, 1),
                                  "used": index < frames}
        for image in (guide, annotated):
            draw = ImageDraw.Draw(image)
            for strip in ((x0, y0, x1 - 1, y0 + band - 1), (x0, y1 - band, x1 - 1, y1 - 1),
                          (x0, y0, x0 + band - 1, y1 - 1), (x1 - band, y0, x1 - 1, y1 - 1)):
                draw.rectangle(strip, fill=GUTTER)
            draw.rectangle((sx0, sy0, sx1, sy1), outline=SAFE, width=3)
            if index < frames:
                draw.line((sx0, ground_y, sx1, ground_y), fill=INK, width=2)
                draw.line((root_x, ground_y - 12, root_x, ground_y + 12), fill=INK, width=2)
        svg.extend([
            f'<path d="M{x0},{y0}h{cell_w}v{cell_h}h{-cell_w}z M{x0 + band},{y0 + band}v{cell_h - 2 * band}'
            f'h{cell_w - 2 * band}v{-(cell_h - 2 * band)}z" fill="{sheet_qc.hex_color(GUTTER)}" fill-rule="evenodd"/>',
            f'<rect x="{sx0:.1f}" y="{sy0:.1f}" width="{sx1 - sx0:.1f}" height="{sy1 - sy0:.1f}" fill="none" '
            f'stroke="{sheet_qc.hex_color(SAFE)}" stroke-width="3"/>'])
        if index < frames:
            svg.extend([f'<line x1="{sx0:.1f}" y1="{ground_y:.1f}" x2="{sx1:.1f}" y2="{ground_y:.1f}" stroke="#000000" '
                        f'stroke-width="2"/>',
                        f'<line x1="{root_x:.1f}" y1="{ground_y - 12:.1f}" x2="{root_x:.1f}" y2="{ground_y + 12:.1f}" '
                        f'stroke="#000000" stroke-width="2"/>'])
        if index < len(phases):
            length = envelope_h / 4.8
            parts = skeleton(phases[index], keys=RUN_KEYS if cycle == "run" else WALK_KEYS, root_x=root_x,
                             ground_y=ground_y, length=length, sign=sign)
            widths = _widths(length)
            for image in (guide, annotated):
                draw_pose(ImageDraw.Draw(image), parts, widths)
            svg.extend(_svg_pose(parts, widths))
            bounds = _pose_bounds(parts, widths)
            toes = {side: parts[f"{side}_leg"][3] for side in ("near", "far")}
            ahead = "near" if (toes["near"][0] - toes["far"][0]) * sign > 0 else "far"
            record.update({"phase": phases[index]["name"], "pose_bounds": [round(value, 1) for value in bounds],
                           "inside_safe_box": bounds[0] >= sx0 and bounds[1] >= sy0 and bounds[2] <= sx1
                           and bounds[3] <= ground_y + widths["leg"],
                           "toe_ahead": ahead,
                           "foot_gap_px": {side: round(ground_y - max(parts[f"{side}_leg"][2][1], toes[side][1]), 1)
                                           for side in ("near", "far")},
                           "_parts": parts, "_widths": widths})
            label = (f"{index}: {phases[index]['name']} | contact leg {phases[index]['contact_leg'].upper()} | "
                     f"ahead {ahead.upper()}")
            ImageDraw.Draw(annotated).text((sx0 + 6, sy0 + 6), label, fill=INK, font=font)
        cells.append(record)
    svg.append("</svg>")
    return {"guide": guide, "annotated": annotated, "svg": "\n".join(svg) + "\n", "cells": cells, "phases": phases,
            "envelope_px": [round(envelope_h * envelope_aspect, 1), round(envelope_h, 1)]}


def self_check(guide: dict, *, cycle: str, facing: str, min_envelope: float, envelope_h: float) -> list[dict]:
    """Checks the guide must pass before it is published."""
    checks = [sheet_qc.check("min_envelope", "fail" if envelope_h < min_envelope else "pass",
                             round(envelope_h, 1), min_envelope,
                             note="predicted motion-envelope height per pose, in host pixels")]
    if cycle == "none":
        checks.append(sheet_qc.check("leg_alternation", "skipped", None, None, note="no skeleton without --cycle"))
        return checks
    used = [cell for cell in guide["cells"] if "phase" in cell]
    outside = [cell["cell"] for cell in used if not cell["inside_safe_box"]]
    checks.append(sheet_qc.check("skeleton_in_safe_box", "fail" if outside else "pass", outside, [],
                                 note="every pose, line widths included, inside its safe box"))
    grounded = []
    for cell, phase in zip(used, guide["phases"]):
        for side in ("near", "far"):
            gap = cell["foot_gap_px"][side]
            if phase["ground"][side] and abs(gap) > 1.0:
                grounded.append([cell["cell"], side, gap])
            if not any(phase["ground"].values()) and gap <= 0:
                grounded.append([cell["cell"], side, gap])
    checks.append(sheet_qc.check("ground_contact", "fail" if grounded else "pass", grounded, 1.0,
                                 note="planted feet touch the ground line; airborne poses clear it"))
    # The guide's own skeleton, drawn clean on transparency, must pass sheet_qc's leading-leg test.
    leads = []
    for cell in used:
        x0, y0, x1, y1 = cell["box"]
        model = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        draw_pose(ImageDraw.Draw(model), _offset(cell["_parts"], -x0, -y0), cell["_widths"])
        leads.append(sheet_qc.leg_lead(np.asarray(model), facing=facing, near=[NEAR], far=[FAR]))
    verdict = sheet_qc.alternation_verdict(leads)
    checks.append(sheet_qc.check("leg_alternation", verdict["status"], [item["lead"] for item in verdict["leads"]],
                                 "lead differs between i and i+n/2", verdict=verdict["verdict"],
                                 pairs=verdict["pairs"],
                                 note="sheet_qc.py frames' NEAR/FAR test run on the guide's own skeleton"))
    return checks


def prompt_block(plan: dict, *, cycle: str, facing: str, phases: Sequence[dict]) -> str:
    """The aspect-only prompt text that explains the guide image."""
    rows, cols, frames = plan["rows"], plan["cols"], plan["frames"]
    width, height = plan["predicted_size"]
    lines = [
        f"Request aspect {plan['aspect']}. Do not state pixel sizes: the host image tool keeps the aspect and",
        f"returns about {width}x{height} for it (area about {HOST_BUDGET_PX:,} px).",
        "Attach guide.png as a layout and pose reference only. Do not draw any of its lines, boxes, colours,",
        "grey bands or stick figures.",
        f"- {frames} poses in {rows} rows x {cols} columns, read left to right, top row first"
        + (f"; leave the last {rows * cols - frames} cell(s) empty." if rows * cols > frames else "."),
        "- The grey band around every cell is empty gutter: no pixel of the character may enter it.",
        "- Keep the whole character (ears, tail, both feet, held items) inside the blue safe box of its cell.",
        "- The black line is the ground and the tick is the fixed root: one body scale and one root in every cell.",
        f"- The character faces {facing.upper()} in every cell.",
    ]
    if cycle != "none":
        half = frames // 2
        lines += [
            "- Match each cell's stick pose. ORANGE limbs are NEAR limbs (viewer side): normal brightness, drawn in",
            "  front. BLUE limbs are FAR limbs (away side): one shade darker, partly hidden behind the body.",
            "Poses (NEAR/FAR, never left/right):",
        ]
        for phase in phases:
            key = phase["name"].split("-")[0]
            a, b = phase["contact_leg"].upper(), ("FAR" if phase["contact_leg"] == "near" else "NEAR")
            b_lift = ", foot lifted" if cycle == "run" else ", heel lifted"
            text = PHASE_TEXT[key].format(a=a, b=b, b_lift=b_lift)
            if "-" in phase["name"]:
                text = f"between {key} and {phase['name'].split('-', 1)[1]}: " + text
            lines.append(f"{phase['index']} {key}: {text}.")
        lines.append(f"Frames {half}-{frames - 1} must not repeat frames 0-{half - 1}: the leg drawn in front swaps "
                     "between NEAR and FAR.")
    lines += [
        "Background: real transparency, or one flat chroma colour absent from the character.",
        "No text, labels, numbers, grid lines, shadows, dust or checkerboard.",
        "",
        "After generation: run sheet_qc.py spill on the raw sheet, then sheet_qc.py frames"
        + (f" --cycle {cycle}." if cycle != "none" else "."),
    ]
    if cycle != "none":
        lines += ["If the leading-leg test reports a duplicated half-cycle, regenerate only the second half,",
                  "attaching the first half and this guide."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI

def run(args: argparse.Namespace) -> dict:
    if args.frames < 1:
        raise PlanError("--frames must be at least 1.")
    if args.frames > MAX_CELLS:
        raise PlanError(f"--frames must be at most {MAX_CELLS}: a host sheet of about 1.5 megapixels has no room "
                        "for more poses; split the action into several sheets.")
    if args.cycle != "none" and (args.frames % 2 or args.frames < 4):
        raise PlanError("--cycle run|walk needs an even number of frames, at least 4 (two half-cycles).")
    if not 0.0 <= args.safe < 0.45:
        raise PlanError("--safe is a fraction of the cell in [0, 0.45).")
    if not 0.0 < args.gutter < 0.2:
        raise PlanError("--gutter is a fraction of the cell in (0, 0.2).")
    if not 4096 <= args.budget <= MAX_BUDGET_PX:  # the guides are drawn at this size; 1e10 px never finished
        raise PlanError(f"--budget is the host image area in pixels (about 1572864, at most {MAX_BUDGET_PX}).")
    if args.max_empty < 0:
        raise PlanError("--max-empty must be 0 or more.")
    envelope_aspect = parse_ratio(args.envelope_aspect)
    aspects = list(STANDARD_ASPECTS) if args.aspect == "auto" else [args.aspect]
    for text in aspects:
        parse_aspect(text)
    layouts = candidate_layouts(args.frames, aspects, envelope_aspect=envelope_aspect, safe=args.safe,
                                budget=args.budget, max_empty=args.max_empty)
    if args.rows or args.cols:
        if not (args.rows and args.cols):
            raise PlanError("--rows and --cols go together.")
        if args.rows < 1 or args.cols < 1 or args.rows * args.cols > MAX_CELLS:
            raise PlanError(f"--rows and --cols must be positive with at most {MAX_CELLS} cells in all.")
        if args.rows * args.cols < args.frames:
            raise PlanError(f"{args.rows} x {args.cols} cells cannot hold {args.frames} frames.")
        layouts = candidate_layouts(args.frames, aspects, envelope_aspect=envelope_aspect, safe=args.safe,
                                    budget=args.budget, max_empty=args.rows * args.cols - args.frames)
        layouts = [item for item in layouts if item["rows"] == args.rows and item["cols"] == args.cols]
    if not layouts:
        raise PlanError("No grid fits; raise --max-empty.")
    chosen = dict(layouts[0], frames=args.frames)
    guide = build_guide(chosen, cycle=args.cycle, facing=args.facing, safe=args.safe, gutter=args.gutter,
                        envelope_aspect=envelope_aspect)
    chosen["envelope_px"] = guide["envelope_px"]  # measured on the whole-pixel cells that were drawn
    checks = self_check(guide, cycle=args.cycle, facing=args.facing, min_envelope=args.min_envelope_px,
                        envelope_h=chosen["envelope_px"][1])
    status = sheet_qc.worst_status([item["status"] for item in checks])
    if status == "fail":
        failed = "; ".join(f"{item['id']}={json.dumps(item['value'], ensure_ascii=True)}"
                           for item in checks if item["status"] == "fail")
        raise PlanError(f"guide self-check failed ({failed}); nothing was published")
    prompt = prompt_block(chosen, cycle=args.cycle, facing=args.facing, phases=guide["phases"])
    output_dir = Path(args.output_dir)
    with forge_core.staged_output(output_dir) as stage:
        forge_core.save_png(guide["guide"], stage / "guide.png")
        forge_core.save_png(guide["annotated"], stage / "guide-annotated.png")
        (stage / "guide.svg").write_bytes(guide["svg"].encode("ascii"))
        (stage / "prompt.txt").write_bytes(prompt.encode("ascii"))
        outputs = [forge_core.file_ref(stage / name, stage)
                   for name in ("guide.png", "guide.svg", "guide-annotated.png", "prompt.txt")]
        cells = [{key: value for key, value in cell.items() if not key.startswith("_")} for cell in guide["cells"]]
        plan = {
            "schema": SCHEMA,
            "request": {"frames": args.frames, "cycle": args.cycle, "facing": args.facing, "aspect": args.aspect,
                        "envelope_aspect": round(envelope_aspect, 4), "safe": args.safe, "gutter": args.gutter,
                        "budget_px": args.budget, "max_empty": args.max_empty},
            "predicted_size": chosen["predicted_size"], "aspect": chosen["aspect"],
            "layout": {key: chosen[key] for key in ("rows", "cols", "empty_cells", "cell", "divides_exactly",
                                                    "safe_box", "envelope_px")},
            "candidates": layouts[:8],
            "cells": cells,
            "phases": [{key: (list(value) if isinstance(value, tuple) else value) for key, value in phase.items()}
                       for phase in guide["phases"]],
            "prompt": "prompt.txt",
            "qa": {"status": status,
                   "method": "geometry of the drawn guide plus sheet_qc.leg_lead/alternation_verdict on its own "
                             "skeleton drawn clean on transparency",
                   "notProven": ["That an image model follows the guide: not A/B tested (report v2 P2-4).",
                                 "The host size rule was observed on 3 calls of one host tool; other tools differ."],
                   "checks": checks, "inputs": [], "outputs": outputs, "tool": dict(TOOL)},
            "tool": dict(TOOL),
        }
        forge_core.write_json(stage / PLAN_NAME, plan)
    return {"status": status, "output": str(output_dir), "metadata": str(output_dir / PLAN_NAME),
            "aspect": chosen["aspect"], "predicted_size": chosen["predicted_size"],
            "grid": [chosen["rows"], chosen["cols"]], "guide": str(output_dir / "guide.png")}


def _aspect(text: str) -> str:
    value = text.strip()
    if value == "auto":
        return value
    try:
        parse_aspect(value)
    except PlanError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--frames", type=int, required=True,
                        help=f"number of poses in the sheet (1 to {MAX_CELLS})")
    parser.add_argument("--output-dir", required=True, help="new directory for the guide, prompt and plan")
    parser.add_argument("--cycle", choices=CYCLES, default="none",
                        help="run or walk draws the NEAR/FAR leg skeleton (default none: layout only)")
    parser.add_argument("--facing", choices=("right", "left"), default="right")
    parser.add_argument("--aspect", type=_aspect, default="auto",
                        help="host aspect W:H, or auto to try 3:2, 2:3, 1:1, 16:9, 9:16, 2:1 and 1:2 (default auto)")
    parser.add_argument("--rows", type=int, default=None, help="force the rows (with --cols)")
    parser.add_argument("--cols", type=int, default=None, help="force the columns (with --rows)")
    parser.add_argument("--envelope-aspect", default="1.0",
                        help="width/height of the whole action's motion envelope (default 1.0)")
    parser.add_argument("--safe", type=float, default=0.15, help="safe frame per cell side (default 0.15)")
    parser.add_argument("--gutter", type=float, default=0.04, help="empty band at each cell edge (default 0.04)")
    parser.add_argument("--max-empty", type=int, default=1, help="spare cells allowed (default 1)")
    parser.add_argument("--budget", type=int, default=HOST_BUDGET_PX,
                        help=f"host image area in pixels (default {HOST_BUDGET_PX}, at most {MAX_BUDGET_PX})")
    parser.add_argument("--min-envelope-px", type=float, default=0.0,
                        help="fail when a pose gets less envelope height than this many host pixels")
    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    print(json.dumps(summary, ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry (D26, D27): usage errors exit 2 (argparse); every other failure, a failed self-check
    included, prints one ``error: ...`` line and exits 1 with nothing published."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
