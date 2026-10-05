#!/usr/bin/env python3
"""Turn a planned HD-2D stage into a layout guide image and a prompt block for the plate generator.

Write the stage first (generate2dmap.stage.v1: sourceSize, ground polygons, playableBand, slots,
protected landmarks, planned effects, bakedContent), then run this tool before generating the plate:

  guide.png         the planning canvas (sourceSize) with the walkable ground, the playable band, the
                    standing spots, landmarks and planned moving surfaces, on a 10% grid. Attach it to
                    the image request as a layout reference.
  prompt-block.txt  the same geometry in percent of the image (x from the left, y from the top), the
                    list of things forbidden inside the walkable ground, and what the plate may paint
                    (bakedContent). Paste it into the plate prompt. Percent coordinates survive the
                    generator returning another pixel size.
  guide.json        what was written: the percentages, the forbidden list, bakedContent, file sha256s
                    and a QA envelope of the plan's own consistency (validate_stage's static checks).

The same stage always gives the same bytes. Exit status 1 when the plan is inconsistent (a slot off
the ground, on water or in a landmark); --strict then publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)
import validate_stage as vs  # noqa: E402  (the stage library beside this script)


GUIDE_SCHEMA = "generate2dmap.scene_guide.v1"
TOOL = {"name": "scene_layout_guide", "version": forge_core.FORGE_PACKAGE_VERSION}
DEFAULT_FORBIDDEN = (
    "people, characters, creatures or silhouettes",
    "trees, tall shrubs, grass clumps or hedges",
    "lamps, lanterns, pillars, posts or statues",
    "furniture, tables, crates, barrels or piles",
    "stairs, steps, ledges or raised platforms",
    "walls, fences, railings, gates or balustrades",
    "ponds, puddles, wells or streams",
    "boats, carts or vehicles",
    "loose items: tools, bottles, rope, debris or pickups",
    "large cast shadows",
    "text, signs, symbols, labels or UI",
)
ACTOR_ITEMS = frozenset({"people, characters, creatures or silhouettes"})
COLLECTIBLE_ITEMS = frozenset({"loose items: tools, bottles, rope, debris or pickups"})
SURFACE_WORDS = {"ripple": "water", "shimmer": "shimmering surface", "sway": "swaying foliage or cloth",
                 "glow": "light glow"}
ROLE_WORDS = {"hero": "hero", "enemy": "enemy", "boss": "boss"}
NOT_PROVEN = [
    "The guide and prompt describe the plan; they do not make an image model follow it. Measure the returned "
    "plate, fix sourceSize and the polygons after looking at it, then run validate_stage.py on the real plate.",
    "Percentages are rounded to whole percent; the stage file keeps the exact values.",
    "The always-visible area assumes the stage's fit and objectPosition at the two ends of reviewedAspectRange.",
]

SCENERY_RGB = (214, 218, 224)
CROPPED_RGB = (176, 182, 194)
GRID_RGB = (150, 156, 168)
GROUND_FILL, GROUND_LINE = (164, 205, 150), (52, 118, 64)
WATER_FILL, WATER_LINE = (150, 192, 232), (40, 100, 170)
SURFACE_FILL, SURFACE_LINE = (205, 186, 232), (110, 80, 160)
LANDMARK_FILL, LANDMARK_LINE = (232, 176, 128), (160, 84, 36)
ROLE_FILL = {"hero": (54, 120, 214), "enemy": (214, 112, 36), "boss": (190, 44, 58)}
INK = (24, 28, 36)


def pct(value: float) -> int:
    """Whole percent, rounded half-up (the forge rule)."""
    return vs.round_half_up(value * 100.0)


def span(low: float, high: float, axis: str) -> str:
    return f"{axis}{pct(low)}%-{pct(high)}%"


def point_text(point: Sequence[float]) -> str:
    return f"x{pct(point[0])}% y{pct(point[1])}%"


def clip_to_band(polygon: Sequence[Sequence[float]], low: float, high: float) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clip of a polygon to the horizontal slab low <= v <= high."""
    points = [tuple(map(float, point)) for point in polygon]
    for limit, keep_above in ((low, True), (high, False)):
        inside = (lambda p: p[1] >= limit) if keep_above else (lambda p: p[1] <= limit)
        clipped: list[tuple[float, float]] = []
        for index, current in enumerate(points):
            previous = points[index - 1]
            if inside(current) != inside(previous):
                t = (limit - previous[1]) / (current[1] - previous[1])
                clipped.append((previous[0] + t * (current[0] - previous[0]), limit))
            if inside(current):
                clipped.append(current)
        points = clipped
        if not points:
            break
    return points


def always_visible(stage: vs.Stage) -> vs.Box:
    """The UV window every reviewed screen aspect shows: the intersection of the projections at both
    ends of reviewedAspectRange (the whole plate for fit contain)."""
    if stage.fit == "contain":
        return 0.0, 0.0, 1.0, 1.0
    windows = [vs.make_projection(stage, vs.viewport_for(ratio)).visible_uv() for ratio in stage.aspect_range]
    return (max(w[0] for w in windows), max(w[1] for w in windows), min(w[2] for w in windows),
            min(w[3] for w in windows))


def forbidden_items(stage: vs.Stage, extra: Sequence[str], defaults: bool) -> list[str]:
    """Default list minus what bakedContent says the plate may paint, plus --forbid items (in order, unique)."""
    baked = stage.baked or {}
    items = list(DEFAULT_FORBIDDEN) if defaults else []
    if baked.get("actors") not in (None, False, []):
        items = [item for item in items if item not in ACTOR_ITEMS]
    if baked.get("collectibles") not in (None, False, []):
        items = [item for item in items if item not in COLLECTIBLE_ITEMS]
    for item in extra:
        text = " ".join(str(item).split())
        if text and text not in items:
            items.append(text)
    return items


def baked_sentences(stage: vs.Stage) -> tuple[list[str], dict[str, Any]]:
    baked = stage.baked or {}
    sentences, record = [], {}
    for key, nothing, painted in (
            ("actors", "No people, characters, creatures or silhouettes are painted into the plate; actors are "
                       "separate runtime sprites.", "Actors painted into the plate"),
            ("collectibles", "No collectibles or loose pickups are painted; they are separate runtime sprites.",
             "Collectibles painted into the plate")):
        value = baked.get(key, False)
        record[key] = value
        if value in (False, None, []):
            sentences.append(nothing)
        elif value is True:
            sentences.append(f"{painted}: yes (bakedContent.{key} is true); keep them where the guide leaves room.")
        else:
            names = ", ".join(forge_core.ascii_text(str(item)) for item in value)
            sentences.append(f"{painted}: {names}.")
    return sentences, record


def plan_percentages(stage: vs.Stage) -> dict[str, Any]:
    """Every figure the prompt states, in whole percent."""
    columns = [p[0] for polygon in stage.ground for p in polygon]
    rows = [p[1] for polygon in stage.ground for p in polygon]
    band = None
    if stage.band is not None:
        clipped = [p for polygon in stage.ground for p in clip_to_band(polygon, *stage.band)]
        band = {"y": [pct(stage.band[0]), pct(stage.band[1])],
                "x": [pct(min(p[0] for p in clipped)), pct(max(p[0] for p in clipped))] if clipped else None}
    visible = always_visible(stage)
    return {
        "ground": {"box": [pct(min(columns)), pct(min(rows)), pct(max(columns)), pct(max(rows))],
                   "polygons": [[[pct(u), pct(v)] for u, v in polygon] for polygon in stage.ground]},
        "band": band,
        "alwaysVisible": [pct(value) for value in visible],
        "slots": [{"id": ident, "role": role, "x": pct(uv[0]), "y": pct(uv[1])} for ident, role, uv in stage.slots],
        "approach": [{"x": pct(u), "y": pct(v)} for u, v in stage.approach],
        "landmarks": [{"id": region.id, "box": [pct(value) for value in _bbox(region.outline())]}
                      for region in stage.regions_for("walk")],
        "surfaces": [{"id": effect.id, "kind": effect.kind, "box": [pct(value) for value in _bbox(effect.polygon)]}
                     for effect in stage.effects],
    }


def _bbox(points: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    array = np.asarray(points, float)
    return float(array[:, 0].min()), float(array[:, 1].min()), float(array[:, 0].max()), float(array[:, 1].max())


def prompt_block(stage: vs.Stage, figures: dict[str, Any], forbidden: Sequence[str]) -> str:
    """The pasteable ASCII block: geometry in percent, standing spots, landmarks, surfaces, forbidden
    items, bakedContent and a do-not-copy line for the guide image."""
    width, height = stage.source_size
    orientation = "landscape" if width >= height else "portrait"
    box = figures["ground"]["box"]
    lines = [
        "LAYOUT CONTRACT (positions in percent of the image: x from the left edge, y from the top edge)",
        f"- Canvas: {width}x{height} px ({orientation}, aspect {width / height:.2f}); keep the fixed camera and "
        f"framing of the attached layout guide.",
    ]
    for index, polygon in enumerate(stage.ground):
        outline = ", ".join(f"({pct(u)}%, {pct(v)}%)" for u, v in polygon)
        name = "Walkable ground" if len(stage.ground) == 1 else f"Walkable ground area {index + 1}"
        u0, v0, u1, v1 = _bbox(polygon)
        lines.append(f"- {name}: {span(u0, u1, 'x')}, {span(v0, v1, 'y')}; broad, flat, dry and open. "
                     f"Outline: {outline}.")
    if len(stage.ground) > 1:
        lines.append(f"- All walkable ground lies within x{box[0]}%-{box[2]}%, y{box[1]}%-{box[3]}%.")
    if figures["band"] is not None:
        band = figures["band"]
        across = f", walkable from x{band['x'][0]}% to x{band['x'][1]}%" if band["x"] else ""
        lines.append(f"- Playable band y{band['y'][0]}%-{band['y'][1]}%{across}: keep it open, level and "
                     f"unobstructed.")
    visible = figures["alwaysVisible"]
    if visible != [0, 0, 100, 100]:
        lines.append(f"- Always on screen: x{visible[0]}%-{visible[2]}%, y{visible[1]}%-{visible[3]}%; keep every "
                     f"landmark and the heart of the ground inside it (some screens crop the rest).")
    if stage.slots:
        spots = "; ".join(f"{ROLE_WORDS[role]} {ident[1:] if role != 'boss' else ''}".strip()
                          + f" x{pct(uv[0])}% y{pct(uv[1])}%" for ident, role, uv in stage.slots)
        lines.append(f"- Clear standing spots (paint nothing on them): {spots}.")
    if stage.approach:
        lines.append("- Keep open for walking: " + "; ".join(point_text(p) for p in stage.approach) + ".")
    if figures["landmarks"]:
        marks = "; ".join(f"{forge_core.ascii_text(item['id'])} x{item['box'][0]}%-{item['box'][2]}% "
                          f"y{item['box'][1]}%-{item['box'][3]}%" for item in figures["landmarks"])
        lines.append(f"- Landmarks, keep them exactly there; nobody walks on them: {marks}.")
    if figures["surfaces"]:
        surfaces = "; ".join(f"{forge_core.ascii_text(item['id'])} ({SURFACE_WORDS[item['kind']]}) "
                             f"x{item['box'][0]}%-{item['box'][2]}% y{item['box'][1]}%-{item['box'][3]}%"
                             for item in figures["surfaces"])
        lines.append(f"- Planned surfaces, paint them there (they move later; nobody stands on water): {surfaces}.")
    if forbidden:
        where = "outside the planned surfaces" if figures["surfaces"] else "anywhere"
        lines.append(f"- Forbidden on the walkable ground ({where}): " + "; ".join(forbidden) + ".")
    baked, _ = baked_sentences(stage)
    lines += [f"- {sentence}" for sentence in baked]
    lines.append("- The layout guide shows geometry only: do not copy its colours, outlines, dots, grid, numbers "
                 "or labels; paint one continuous scene.")
    return "\n".join(forge_core.ascii_text(line) for line in lines) + "\n"


# --------------------------------------------------------------------------- guide image

def _dashed(draw: ImageDraw.ImageDraw, start: Sequence[float], end: Sequence[float], dash: float,
            fill: Sequence[int], width: int) -> None:
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    if length == 0:
        return
    steps = int(length // (2 * dash)) + 1
    for step in range(steps):
        a = min(1.0, 2 * step * dash / length)
        b = min(1.0, (2 * step + 1) * dash / length)
        draw.line([(start[0] + a * (end[0] - start[0]), start[1] + a * (end[1] - start[1])),
                   (start[0] + b * (end[0] - start[0]), start[1] + b * (end[1] - start[1]))],
                  fill=tuple(fill), width=width)


def render_guide(stage: vs.Stage) -> Image.Image:
    """Flat-colour layout guide at sourceSize: scenery, cropped margins, ground, band, surfaces,
    landmarks, standing spots, approach points, a 10% grid and a legend."""
    width, height = stage.source_size
    image = Image.new("RGB", (width, height), SCENERY_RGB)
    draw = ImageDraw.Draw(image)
    line = max(1, vs.round_half_up(min(width, height) / 300))
    typeface = vs.font(max(10, vs.round_half_up(min(width, height) / 40)))
    small = vs.font(max(9, vs.round_half_up(min(width, height) / 56)))
    to_px = np.array([width, height], float)
    u0, v0, u1, v1 = always_visible(stage)
    for box in ((0, 0, u0, 1), (u1, 0, 1, 1), (u0, 0, u1, v0), (u0, v1, u1, 1)):
        if box[2] > box[0] and box[3] > box[1]:
            draw.rectangle((box[0] * width, box[1] * height, box[2] * width, box[3] * height), fill=CROPPED_RGB)
    for step in range(1, 10):
        x, y = width * step / 10.0, height * step / 10.0
        draw.line([(x, 0), (x, height)], fill=GRID_RGB, width=1)
        draw.line([(0, y), (width, y)], fill=GRID_RGB, width=1)
    for polygon in stage.ground:
        xy = [tuple(p) for p in np.asarray(polygon) * to_px]
        draw.polygon(xy, fill=GROUND_FILL)
        draw.line(xy + xy[:1], fill=GROUND_LINE, width=line + 1, joint="curve")
    for effect in stage.effects:
        fill, outline = (WATER_FILL, WATER_LINE) if effect.kind in vs.WATER_KINDS else (SURFACE_FILL, SURFACE_LINE)
        xy = [tuple(p) for p in np.asarray(effect.polygon) * to_px]
        draw.polygon(xy, fill=fill)
        draw.line(xy + xy[:1], fill=outline, width=line, joint="curve")
    for region in stage.regions_for("walk"):
        xy = [tuple(p) for p in np.asarray(region.outline()) * to_px]
        draw.polygon(xy, fill=LANDMARK_FILL)
        draw.line(xy + xy[:1], fill=LANDMARK_LINE, width=line + 1, joint="curve")
        x0, y0, _, _ = _bbox(region.outline())
        vs.label(draw, (x0 * width + 3 * line, y0 * height + 2 * line), region.id, INK, small, stroke=0)
    if stage.band is not None:
        for v in stage.band:
            _dashed(draw, (0, v * height), (width, v * height), 6 * line, GROUND_LINE, line)
    radius = max(4.0, 0.012 * width)
    for ident, role, uv in stage.slots:
        x, y = uv[0] * width, uv[1] * height
        draw.ellipse((x - radius, y - radius * 0.6, x + radius, y + radius * 0.6), fill=ROLE_FILL[role],
                     outline=(255, 255, 255), width=line)
        vs.label(draw, (x + radius + 2 * line, y - vs.text_height(typeface)),
                 f"{ident} {pct(uv[0])}%,{pct(uv[1])}%", ROLE_FILL[role], typeface, stroke=0)
    for index, (u, v) in enumerate(stage.approach, start=1):
        x, y = u * width, v * height
        draw.polygon([(x, y - radius), (x + radius, y), (x, y + radius), (x - radius, y)], outline=INK, width=line)
        vs.label(draw, (x + radius + 2 * line, y - vs.text_height(small) / 2), f"A{index}", INK, small, stroke=0)
    for step in range(1, 10):
        vs.label(draw, (width * step / 10.0 + 2 * line, 2 * line), f"{step * 10}%", INK, small, stroke=0)
        vs.label(draw, (2 * line, height * step / 10.0 + line), f"{step * 10}%", INK, small, stroke=0)
    legend = ["LAYOUT GUIDE (geometry only)", "green: walkable ground   dashes: playable band",
              "dots: standing spots   orange: landmarks", "blue: water   violet: other moving surfaces",
              "grey margin: cropped on some screens"]
    rows = vs.text_height(small) + 2 * line
    box_w = max(draw.textlength(text, font=small) for text in legend) + 8 * line
    left = draw.textlength("90%", font=small) + 6 * line  # clear of the percent labels on the left edge
    top = vs.text_height(small) + 6 * line  # and of those along the top edge
    draw.rectangle((left, top, left + box_w, top + rows * len(legend) + 4 * line), fill=(250, 250, 248),
                   outline=INK, width=1)
    for row, text in enumerate(legend):
        vs.label(draw, (left + 4 * line, top + 2 * line + row * rows), text, INK, small, stroke=0)
    return image


def guide_checks(stage: vs.Stage) -> list[dict[str, Any]]:
    """Plan checks specific to the guide: slots a reviewed aspect crops away."""
    u0, v0, u1, v1 = always_visible(stage)
    cropped = [{"slot": ident, "uv": list(uv)} for ident, _, uv in stage.slots
               if not (u0 <= uv[0] <= u1 and v0 <= uv[1] <= v1)]
    return [vs.check("slots always on screen", "warn" if cropped else "pass", cropped or None,
                     {"alwaysVisibleUv": [u0, v0, u1, v1],
                      "note": "cropped slots rely on the runtime layout moving actors (see validate_stage.py)"})]


# --------------------------------------------------------------------------- CLI

def run(args: argparse.Namespace) -> dict[str, Any]:
    final = Path(args.output_dir)
    vs.refuse_existing(final)
    if not math.isfinite(args.foot_radius) or not 0 <= args.foot_radius < 0.5:
        raise ValueError("--foot-radius must be a fraction of the plate width in 0..0.5")
    if not math.isfinite(args.y_squash) or not 0 < args.y_squash <= 1:
        raise ValueError("--y-squash must be in (0, 1]")
    stage_path = Path(args.stage)
    stage = vs.load_stage(stage_path)
    figures = plan_percentages(stage)
    forbidden = forbidden_items(stage, args.forbid or [], not args.no_default_forbid)
    text = prompt_block(stage, figures, forbidden)
    guide = render_guide(stage)
    checks = vs.static_checks(stage, foot_radius=args.foot_radius, y_squash=args.y_squash) + guide_checks(stage)
    failed = [item["id"] for item in checks if item["status"] == "fail"]
    if args.strict and failed:
        raise vs.StageError(f"strict check failed ({'; '.join(failed)}); nothing was written")
    _, baked = baked_sentences(stage)
    with forge_core.staged_output(final) as stage_dir:
        forge_core.save_png(guide, stage_dir / "guide.png")
        (stage_dir / "prompt-block.txt").write_bytes(text.encode("ascii"))
        guide_ref = forge_core.file_ref(stage_dir / "guide.png", stage_dir)
        prompt_ref = forge_core.file_ref(stage_dir / "prompt-block.txt", stage_dir)
        stage_ref = forge_core.file_ref(stage_path, final)
        method = ("scene_layout_guide: the stage's UV geometry drawn at sourceSize and restated in whole percent; "
                  "the plan checked with validate_stage's static checks (footprint point-in-polygon tests of every "
                  "slot against ground, landmarks and water; polygon simplicity; effect overlaps).")
        record = {"schema": GUIDE_SCHEMA, "tool": dict(TOOL), "stage": stage_ref,
                  "canvas": list(stage.source_size), "guide": guide_ref, "promptBlock": prompt_ref,
                  "percent": figures, "forbiddenInWalk": forbidden, "bakedContent": baked,
                  "qa": vs._local_qa_envelope(checks, method=method, not_proven=NOT_PROVEN, inputs=[stage_ref],
                                              outputs=[guide_ref, prompt_ref], tool=TOOL),
                  "warnings": list(stage.notes)}
        forge_core.write_json(stage_dir / "guide.json", record)
    return {"output_dir": str(final.resolve()), "metadata": str((final / "guide.json").resolve()),
            "guide": str((final / "guide.png").resolve()), "prompt_block": str((final / "prompt-block.txt").resolve()),
            "status": record["qa"]["status"], "failed": failed, "_warnings": list(stage.notes)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", type=Path, required=True, help="Planned stage.json (generate2dmap.stage.v1).")
    parser.add_argument("--output-dir", type=Path, required=True, help="New folder for guide.png, prompt-block.txt "
                                                                       "and guide.json.")
    parser.add_argument("--forbid", action="append", metavar="ITEM",
                        help="Add an item to the forbidden-in-walk list (repeatable).")
    parser.add_argument("--no-default-forbid", action="store_true",
                        help="Start the forbidden list empty instead of the default list.")
    parser.add_argument("--foot-radius", type=float, default=vs.DEFAULT_FOOT_RADIUS,
                        help="Footprint radius for the plan checks, a fraction of the plate width (default 0.01).")
    parser.add_argument("--y-squash", type=float, default=vs.DEFAULT_Y_SQUASH,
                        help="Footprint vertical/horizontal ratio (default 0.58).")
    parser.add_argument("--strict", action="store_true", help="Publish nothing and exit 1 when a plan check fails.")
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if summary["status"] == "fail" else 0


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI: exit 0 (pass or warn), 1 (a published report with status fail, D26; or an error,
    printed as one error: line, D27), 2 (usage)."""
    return forge_core.run_cli(_main, argv)


if __name__ == "__main__":
    raise SystemExit(main())
