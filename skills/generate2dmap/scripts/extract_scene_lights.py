#!/usr/bin/env python3
"""Find the light sources painted into an HD-2D plate, bake a light cookie, and check atmosphere data.

extract: lamps, lanterns, braziers and windows are compact peaks of local contrast. The detector
  takes each pixel's luminance above its box-blurred local mean (--background, a fraction of the plate
  width) and finds the local maxima at least --min-contrast high, --min-brightness bright and --merge
  apart. Each peak's light is the region around it at half the peak or more: wider than --max-size it is
  a lit wall or sky, narrower than --min-size it is noise. A light is that region's contrast-weighted
  centroid (u, v in UV), the hue its glow adds at full brightness (--color warm needs R - B >= 0.08), a
  radius of --radius-scale times the core's equivalent radius (as a fraction of the plate width) and a
  strength (its peak contrast). With --stage, a light centred on the stage's ground polygons or water is
  rejected as a reflection or glint: the plate contract keeps lamps off the walkable ground (--keep-floor
  keeps them). Outputs, in a new --output-dir:
    lights.json        generate2dmap.lights.v1
    light-cookie.png   512x288 (--cookie-size) RGB: --ambient filled, each light a smooth radial pool
    lights-overlay.png the darkened plate with every light circled, numbered and colour-swatched
    lights-qa.json     a QA envelope (count against --expect, edge contact, opacity, flicker rate)

cookie: after deleting, moving or adding lights in lights.json by hand, check it and write it again with
  a re-baked cookie (and, with --plate, a new overlay) in a new --output-dir.

atmosphere: check a generate2dmap.atmosphere.v1 file (motes, mist, shafts, grade) for valid values and
  mote budgets; writes atmosphere-qa.json in a new --output-dir.

Exit status 1 when a check fails; --strict then publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import re
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


LIGHTS_SCHEMA = "generate2dmap.lights.v1"
LIGHTS_QA_SCHEMA = "generate2dmap.lights_qa.v1"
ATMOSPHERE_SCHEMA = "generate2dmap.atmosphere.v1"
ATMOSPHERE_QA_SCHEMA = "generate2dmap.atmosphere_qa.v1"
TOOL = {"name": "extract_scene_lights", "version": forge_core.FORGE_PACKAGE_VERSION}
LUMA = np.array([0.2126, 0.7152, 0.0722])
COLOR_MODES = ("warm", "any", "cool")
HUE_MARGIN = 0.08
COOKIE_SIZE = (512, 288)
DEFAULT_AMBIENT = "#243044"
RADIUS_LIMITS = (0.01, 0.15)
GOLDEN = 0.6180339887498949
SAFE_FLICKER_HZ = 3.0
BLENDS = ("normal", "additive", "screen")
MOTE_LIMITS = {"max_motes": 512, "max_mobile_motes": 160}

LIGHTS_NOT_PROVEN = [
    "The detector proposes compact contrast peaks of the chosen hue; it cannot tell a lantern from a bright flower, "
    "sign, window row, wet-floor glint or reflection outside the stage's ground and water, and it misses a lamp no "
    "brighter than its surroundings. Look at lights-overlay.png, edit lights.json and re-bake it with the cookie verb.",
    "Positions are glow centroids: a glow cut by an occluder or the plate edge pulls its centre.",
    "Colour is the blob's hue at full brightness, radius and strength are presentation defaults; neither measures "
    "real emission or falloff.",
    "The cookie is a screen-space tint texture, not light transport: everything under a pool gets the same tint.",
]
ATMOSPHERE_NOT_PROVEN = [
    "Values are checked for range and budget only; how motes, mist, shafts and the grade look depends on the "
    "runtime's shaders. Judge them in the game at gameplay scale, on a phone as well.",
    "Mote budgets are counts, not measured frame times.",
]


# --------------------------------------------------------------------------- arguments

def _size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", text)
    if not match or min(int(match.group(1)), int(match.group(2))) < 1:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT in whole pixels, for example 512x288")
    return int(match.group(1)), int(match.group(2))


def _hex(text: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        raise argparse.ArgumentTypeError("use a #rrggbb colour, for example #243044")
    return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)


def _flicker(text: str) -> tuple[float, float]:
    try:
        hz, depth = (float(part) for part in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError("use HZ:DEPTH, for example 0.25:0.15") from None
    if not (math.isfinite(hz) and 0 < hz <= 30 and math.isfinite(depth) and 0 <= depth <= 1):
        raise argparse.ArgumentTypeError("--flicker needs 0 < HZ <= 30 and 0 <= DEPTH <= 1")
    return hz, depth


def hex_color(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{min(255, max(0, vs.round_half_up(float(c)))):02x}" for c in rgb)


# --------------------------------------------------------------------------- detection

def _local_max_filter(plane: np.ndarray, radius: int) -> np.ndarray:
    """Maximum over a (2r+1)^2 square window with replicated edges, as two separable passes of shifted maxima."""
    if radius <= 0:
        return plane.copy()
    height, width = plane.shape
    padded = np.pad(plane, radius, mode="edge")
    rows = padded[:, :width].copy()
    for shift in range(1, 2 * radius + 1):
        np.maximum(rows, padded[:, shift:shift + width], out=rows)
    out = rows[:height].copy()
    for shift in range(1, 2 * radius + 1):
        np.maximum(out, rows[shift:shift + height], out=out)
    return out


def detect_lights(rgb: np.ndarray, *, color: str = "warm", min_brightness: float = 0.7, min_contrast: float = 0.12,
                  background: float = 0.03, min_size: float = 0.002, max_size: float = 0.08, merge: float = 0.012,
                  max_lights: int = 32, radius_scale: float = 20.0, exclude: np.ndarray | None = None
                  ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Light candidates of an RGB float image (0..1): (lights, statistics, rejected candidates with a reason).

    1. Local contrast: the luminance of each pixel's added colour (pixel minus its box mean of radius
       ``background`` x width, per channel), smoothed 3x3.
    2. Peaks: local maxima of that contrast within +-``merge`` x width, at least ``min_contrast``, on pixels
       whose 3x3 luminance is at least ``min_brightness``; touching equal maxima count once.
    3. Strongest first, each peak's light is the 8-connected region around it where the contrast is at least
       half the peak, searched within +-``max_size`` x width. A region that reaches that border is a broad
       bright area (a lit wall, a sky gap), one narrower than ``min_size`` x width is noise. Measuring each
       light against its own peak keeps a lamp apart from the lit wall around it.
    4. The light is the region's contrast-weighted centroid; its colour the added colour over the halo
       (20-60% of the peak, within three core radii), normalised to full brightness, because a painted flame
       core is near white and the scenery behind a glow is not the light's colour. A colour that fails the
       hue test, a centre on ``exclude`` (a boolean plate mask, such as the stage's ground and water) or
       within ``merge`` x width of a stronger light rejects the candidate. After ``max_lights`` lights the
       weaker peaks are not examined (``capped``).
    """
    height, width = rgb.shape[:2]
    window = max(2, vs.round_half_up(background * width))
    added = rgb - np.stack([vs._local_box_mean(rgb[..., channel], window) for channel in range(3)], axis=-1)
    excess = added @ LUMA  # luminance minus its local mean (the box mean is linear)
    contrast = vs._local_box_mean(excess, 1)
    brightness = vs._local_box_mean(rgb @ LUMA, 1)
    spacing = max(1, vs.round_half_up(merge * width))
    peaks = ((contrast >= _local_max_filter(contrast, spacing)) & (contrast >= min_contrast)
             & (brightness >= min_brightness))
    stats = {"backgroundWindowPx": 2 * window + 1, "candidates": 0, "tooSmall": 0, "tooLarge": 0, "offHue": 0,
             "onGroundOrWater": 0, "merged": 0, "capped": 0}
    labels, _count = forge_core.label_components(peaks, 8)
    ys, xs = np.nonzero(peaks)
    order = np.lexsort((xs, ys, -contrast[ys, xs]))  # strongest first, then raster order
    _, first = np.unique(labels[ys[order], xs[order]], return_index=True)
    seeds = order[np.sort(first)]  # one seed per plateau: its strongest pixel
    stats["candidates"] = int(len(seeds))
    reach = max(2, math.ceil(max_size * width))
    lights, rejected = [], []
    for rank, seed in enumerate(seeds.tolist()):
        if len(lights) == max_lights:
            stats["capped"] = int(len(seeds) - rank)
            break
        py, px = int(ys[seed]), int(xs[seed])
        peak = float(contrast[py, px])
        y0, y1, x0, x1 = max(0, py - reach), min(height, py + reach + 1), max(0, px - reach), min(width, px + reach + 1)
        local, _ = forge_core.label_components(contrast[y0:y1, x0:x1] >= 0.5 * peak, 8)
        region = local == local[py - y0, px - x0]
        ry, rx = np.nonzero(region)
        box = (int(rx.min()) + x0, int(ry.min()) + y0, int(rx.max()) + x0 + 1, int(ry.max()) + y0 + 1)
        extent = max(box[2] - box[0], box[3] - box[1])
        broad = ((box[0] == x0 and x0 > 0) or (box[1] == y0 and y0 > 0) or (box[2] == x1 and x1 < width)
                 or (box[3] == y1 and y1 < height))
        if broad or extent > max_size * width:
            stats["tooLarge"] += 1
            continue
        if extent < min_size * width:
            stats["tooSmall"] += 1
            continue
        weight = np.clip(excess[y0:y1, x0:x1][region], 1e-9, None)
        cx = float((weight * (rx + x0 + 0.5)).sum() / weight.sum())
        cy = float((weight * (ry + y0 + 0.5)).sum() / weight.sum())
        u, v = cx / width, cy / height
        core_radius = math.sqrt(len(rx) / math.pi)
        near = np.hypot(np.arange(x0, x1)[None, :] + 0.5 - cx, np.arange(y0, y1)[:, None] + 0.5 - cy)
        band = excess[y0:y1, x0:x1]
        halo = (band >= 0.2 * peak) & (band <= 0.6 * peak) & (near <= 3 * core_radius + 3)
        positive = np.clip(added[y0:y1, x0:x1], 0.0, None)
        hue_rgb = positive[halo].mean(axis=0) if halo.any() else positive[region].mean(axis=0)
        hue_rgb = hue_rgb / max(float(hue_rgb.max()), 1e-6) * 255.0
        drift = {"warm": hue_rgb[0] - hue_rgb[2], "cool": hue_rgb[2] - hue_rgb[0]}.get(color, math.inf)
        reason = None
        if drift < HUE_MARGIN * 255.0:  # the glow itself is not of the chosen hue (a white banner, a blue gap)
            stats["offHue"] += 1
            reason = "glow hue"
        elif exclude is not None and exclude[min(height - 1, int(cy)), min(width - 1, int(cx))]:
            stats["onGroundOrWater"] += 1
            reason = "on ground or water"
        elif any(math.hypot(cx - light["u"] * width, cy - light["v"] * height) < spacing for light in lights):
            stats["merged"] += 1
            continue
        if reason is not None:
            rejected.append({"u": u, "v": v, "strength": peak, "reason": reason})
            continue
        radius = core_radius * radius_scale / width
        lights.append({
            "u": u, "v": v, "color": hex_color(hue_rgb),
            "radius": float(min(RADIUS_LIMITS[1], max(RADIUS_LIMITS[0], radius))),
            "strength": float(min(1.0, peak)), "corePx": int(len(rx)), "box": list(box),
            "touchesEdge": bool(box[0] == 0 or box[1] == 0 or box[2] == width or box[3] == height),
        })
    lights.sort(key=lambda light: (round(light["v"], 4), round(light["u"], 4)))
    return lights, stats, rejected


def render_cookie(lights: Sequence[dict[str, Any]], plate_size: tuple[int, int], size: tuple[int, int] = COOKIE_SIZE,
                  ambient: Sequence[int] = (0x24, 0x30, 0x44)) -> Image.Image:
    """Light cookie over the plate's UV square: ``ambient`` everywhere, then each light composited over it
    as a smooth pool (alpha 1 - smoothstep(d), d the distance in radii). Pools are circles on the plate,
    so ellipses in the cookie when its aspect differs from the plate's."""
    width, height = size
    ys, xs = np.mgrid[0:height, 0:width]
    xs, ys = xs + 0.5, ys + 0.5
    out = np.empty((height, width, 3))
    out[:] = np.asarray(ambient, float) / 255.0
    aspect = plate_size[0] / plate_size[1]
    for light in lights:
        rx = light["radius"] * width
        ry = light["radius"] * aspect * height
        distance = np.clip(np.hypot((xs - light["u"] * width) / rx, (ys - light["v"] * height) / ry), 0.0, 1.0)
        alpha = (1.0 - distance * distance * (3.0 - 2.0 * distance))[..., None]
        rgb = np.array([int(light["color"][i:i + 2], 16) for i in (1, 3, 5)], float) / 255.0
        out = rgb * alpha + out * (1.0 - alpha)
    return Image.fromarray(np.floor(out * 255.0 + 0.5).astype(np.uint8))


def render_light_overlay(plate: Image.Image, lights: Sequence[dict[str, Any]]) -> Image.Image:
    """The plate darkened to 55% with each light's pool circled, its centre crossed, numbered and swatched."""
    base = np.asarray(plate.convert("RGB"), float) * 0.55
    image = Image.fromarray(np.floor(base + 0.5).astype(np.uint8)).convert("RGBA")
    width, height = image.size
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    line = max(1, vs.round_half_up(min(width, height) / 400))
    typeface = vs.font(max(10, vs.round_half_up(min(width, height) / 45)))
    for light in lights:
        x, y, r = light["u"] * width, light["v"] * height, light["radius"] * width
        rgb = tuple(int(light["color"][i:i + 2], 16) for i in (1, 3, 5))
        draw.ellipse((x - r, y - r, x + r, y + r), outline=(*rgb, 230), width=line)
        arm = max(6 * line, 0.2 * r)
        draw.line([(x - arm, y), (x + arm, y)], fill=(255, 255, 255, 255), width=line)
        draw.line([(x, y - arm), (x, y + arm)], fill=(255, 255, 255, 255), width=line)
        swatch = vs.text_height(typeface)
        draw.rectangle((x + arm + 2 * line, y + 2 * line, x + arm + 2 * line + swatch, y + 2 * line + swatch),
                       fill=(*rgb, 255), outline=(255, 255, 255, 255))
        vs.label(draw, (x + arm + 2 * line, y - swatch - 2 * line),
                 f"{light['id']} ({light['u']:.3f}, {light['v']:.3f})", (255, 255, 255, 255), typeface)
    vs.label(draw, (3 * line, 3 * line), f"{len(lights)} light(s) found; check every circle sits on a real light",
             (255, 255, 255, 255), typeface)
    image.alpha_composite(layer)
    return image


# --------------------------------------------------------------------------- atmosphere

def _is_color(value: Any) -> bool:
    if isinstance(value, str):
        return re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", value) is not None
    return (isinstance(value, list) and len(value) == 3
            and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in value))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _range_problems(entry: dict[str, Any], where: str, limits: dict[str, tuple[float, float]]) -> list[str]:
    problems = []
    for key, (low, high) in limits.items():
        if key in entry:
            number = _number(entry[key])
            if number is None or not low <= number <= high:
                problems.append(f"{where}.{key} must be a number in {low:g}..{high:g}")
    return problems


def atmosphere_checks(doc: Any, *, max_motes: int, max_mobile_motes: int) -> tuple[list[dict[str, Any]], dict]:
    """Structure of atmosphere.v1, then value ranges, colours and mote budgets."""
    if not isinstance(doc, dict):
        raise ValueError("the atmosphere file must be a JSON object")
    if doc.get("schema") != ATMOSPHERE_SCHEMA:
        raise ValueError(f"$.schema must be {ATMOSPHERE_SCHEMA!r}; got {doc.get('schema')!r}")
    for key, kind in (("motes", list), ("shafts", list), ("grade", dict)):
        if not isinstance(doc.get(key), kind):
            raise ValueError(f"$.{key} must be {'a list' if kind is list else 'an object'}")
    if "mist" in doc and doc["mist"] is not None and not isinstance(doc["mist"], dict):
        raise ValueError("$.mist must be an object or null")
    for key in ("motes", "shafts"):
        for index, entry in enumerate(doc[key]):
            if not isinstance(entry, dict):
                raise ValueError(f"$.{key}[{index}] must be an object")
    checks = []
    problems: list[str] = []
    total = mobile = 0
    for index, layer in enumerate(doc["motes"]):
        where = f"$.motes[{index}]"
        for key in ("count", "mobile"):
            if key in layer and (not isinstance(layer[key], int) or isinstance(layer[key], bool) or layer[key] < 0):
                problems.append(f"{where}.{key} must be a whole number >= 0")
        count = layer.get("count") if isinstance(layer.get("count"), int) else 0
        phone = layer.get("mobile") if isinstance(layer.get("mobile"), int) else count
        if isinstance(layer.get("mobile"), int) and isinstance(layer.get("count"), int) and phone > count:
            problems.append(f"{where}.mobile is more than count")
        total, mobile = total + max(0, count), mobile + max(0, phone)
        problems += _range_problems(layer, where, {"opacity": (0, 1), "size": (1e-9, math.inf),
                                                   "speed": (0, math.inf), "sway": (0, math.inf)})
        colors = layer.get("colors", [])
        if "color" in layer and not _is_color(layer["color"]):
            problems.append(f"{where}.color must be #rrggbb or [r, g, b]")
        if not isinstance(colors, list) or not all(_is_color(c) for c in colors):
            problems.append(f"{where}.colors must be a list of #rrggbb or [r, g, b] colours")
        if "blend" in layer and layer["blend"] not in BLENDS:
            problems.append(f"{where}.blend must be one of {', '.join(BLENDS)}")
    checks.append(vs.check("mote layers are valid", "fail" if problems else "pass", problems or None))
    over = total > max_motes or mobile > max_mobile_motes
    checks.append(vs.check("mote budget", "warn" if over else "pass", {"motes": total, "mobileMotes": mobile},
                           {"maxMotes": max_motes, "maxMobileMotes": max_mobile_motes}))

    mist = doc.get("mist")
    if mist is None:
        checks.append(vs.check("mist", "skipped", "no mist"))
    else:
        problems = _range_problems(mist, "$.mist", {"opacity": (0, 1), "scale": (1e-9, math.inf)})
        if "color" in mist and not _is_color(mist["color"]):
            problems.append("$.mist.color must be #rrggbb or [r, g, b]")
        heavy = (_number(mist.get("opacity")) or 0) > 0.3
        status = "fail" if problems else ("warn" if heavy else "pass")
        checks.append(vs.check("mist", status, problems or {"opacity": mist.get("opacity")},
                               {"opacity": [0, 1], "warnAbove": 0.3}))
    problems, heavy = [], []
    for index, shaft in enumerate(doc["shafts"]):
        where = f"$.shafts[{index}]"
        problems += _range_problems(shaft, where, {"opacity": (0, 1)})
        if "color" in shaft and not _is_color(shaft["color"]):
            problems.append(f"{where}.color must be #rrggbb or [r, g, b]")
        spots = shaft.get("spots", [])
        if not isinstance(spots, list) or not all(isinstance(s, list) and len(s) == 2
                                                  and all(_number(v) is not None for v in s) for s in spots):
            problems.append(f"{where}.spots must be a list of [x, y] numbers")
        if (_number(shaft.get("opacity")) or 0) > 0.25:
            heavy.append(where)
    status = "fail" if problems else ("warn" if heavy else ("skipped" if not doc["shafts"] else "pass"))
    checks.append(vs.check("light shafts", status, problems or heavy or None, {"opacity": [0, 1], "warnAbove": 0.25}))

    grade = doc["grade"]
    problems = _range_problems(grade, "$.grade", {"bloom": (0, math.inf), "saturation": (0, math.inf),
                                                  "vignette": (0, 1)})
    split = grade.get("splitTone")
    if split is not None:
        if not isinstance(split, dict):
            problems.append("$.grade.splitTone must be an object or null")
        else:
            for key in ("shadows", "highlights"):
                if key in split and not _is_color(split[key]):
                    problems.append(f"$.grade.splitTone.{key} must be #rrggbb or [r, g, b]")
            problems += _range_problems(split, "$.grade.splitTone", {"amount": (0, 1), "balance": (-1, 1)})
    bloom, saturation, vignette = (_number(grade.get(key)) for key in ("bloom", "saturation", "vignette"))
    loud = [text for flag, text in (
        (bloom is not None and bloom > 1.0, "bloom above 1 washes out highlights"),
        (saturation is not None and not 0.7 <= saturation <= 1.4, "saturation outside 0.7..1.4 shifts the art"),
        (vignette is not None and vignette > 0.6, "vignette above 0.6 hides the screen edges")) if flag]
    status = "fail" if problems else ("warn" if loud else "pass")
    checks.append(vs.check("grade", status, problems or loud or {key: grade.get(key) for key in
                                                                  ("bloom", "saturation", "vignette")},
                           {"bloom": [0, 1], "saturation": [0.7, 1.4], "vignette": [0, 0.6]}))
    summary = {"moteLayers": len(doc["motes"]), "motes": total, "mobileMotes": mobile, "shafts": len(doc["shafts"]),
               "mist": mist is not None}
    return checks, summary


# --------------------------------------------------------------------------- lights files

def parse_lights(doc: Any) -> tuple[list[dict[str, Any]], tuple[int, int] | None]:
    """Lights of a generate2dmap.lights.v1 document, checked and normalised (hex colours, ids L1.. where
    missing, strength 1 where missing), and its sourceSize when it records one."""
    if not isinstance(doc, dict) or doc.get("schema") != LIGHTS_SCHEMA:
        raise ValueError(f"$.schema must be {LIGHTS_SCHEMA!r}")
    if not isinstance(doc.get("lights"), list):
        raise ValueError("$.lights must be a list")
    lights, seen = [], set()
    for index, entry in enumerate(doc["lights"]):
        where = f"$.lights[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        light: dict[str, Any] = {}
        for key, open_low in (("u", False), ("v", False), ("radius", True), ("strength", False)):
            if key == "strength" and key not in entry:
                light[key] = 1.0
                continue
            number = _number(entry.get(key))
            if number is None or not 0 <= number <= 1 or (open_low and number == 0):
                raise ValueError(f"{where}.{key} must be a number in {'(0' if open_low else '0'}..1")
            light[key] = number
        color = entry.get("color")
        if not _is_color(color):
            raise ValueError(f"{where}.color must be #rrggbb or [r, g, b]")
        light["color"] = color[:7].lower() if isinstance(color, str) else hex_color(color)
        ident = entry.get("id", f"L{index + 1}")
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError(f"{where}.id must be a unique non-empty string")
        seen.add(ident)
        light["id"] = ident
        flicker = entry.get("flicker")
        if flicker is not None:
            if isinstance(flicker, dict):
                hz = _number(flicker.get("hz"))
                depth = _number(flicker.get("depth", 0.0))
                phase = _number(flicker.get("phase", 0.0))
                if hz is None or hz <= 0 or depth is None or not 0 <= depth <= 1 or phase is None:
                    raise ValueError(f"{where}.flicker needs hz > 0, depth 0..1 and a numeric phase")
            elif _number(flicker) is None or flicker < 0:
                raise ValueError(f"{where}.flicker must be a number >= 0 or {{hz, depth, phase}}")
            light["flicker"] = flicker
        lights.append(light)
    size = doc.get("sourceSize")
    valid_size = (isinstance(size, list) and len(size) == 2
                  and all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in size))
    return lights, ((size[0], size[1]) if valid_size else None)


def flicker_check(lights: Sequence[dict[str, Any]]) -> dict[str, Any]:
    rates = [light["flicker"]["hz"] for light in lights if isinstance(light.get("flicker"), dict)]
    if not rates:
        return vs.check("flicker rate", "skipped", "no flicker")
    return vs.check("flicker rate", "warn" if max(rates) > SAFE_FLICKER_HZ else "pass", max(rates),
                    {"maxHz": SAFE_FLICKER_HZ})


def publish_lights(final: Path, lights: list[dict[str, Any]], *, source_size: tuple[int, int],
                   cookie_size: tuple[int, int], ambient: tuple[int, int, int], plate: Image.Image | None,
                   inputs: list[tuple[Path, str | None]], plate_input: tuple[Path, str] | None,
                   checks: list[dict[str, Any]], method: str, extra: dict[str, Any]) -> dict[str, Any]:
    """Write lights.json, light-cookie.png, lights-overlay.png (with a plate) and lights-qa.json into a new
    ``final`` directory, published only when complete."""
    cookie = render_cookie(lights, source_size, cookie_size, ambient)
    overlay = render_light_overlay(plate, lights) if plate is not None else None
    with forge_core.staged_output(final) as stage_dir:
        forge_core.save_png(cookie, stage_dir / "light-cookie.png")
        names = ["lights.json", "light-cookie.png"]
        if overlay is not None:
            forge_core.save_png(overlay, stage_dir / "lights-overlay.png")
            names.append("lights-overlay.png")
        cookie_ref = forge_core.file_ref(stage_dir / "light-cookie.png", stage_dir)
        document: dict[str, Any] = {"schema": LIGHTS_SCHEMA, "tool": dict(TOOL)}
        if plate_input is not None:
            document["plate"] = forge_core.file_ref(plate_input[0], final, sha256=plate_input[1])
        document.update({
            "sourceSize": list(source_size), "radiusUnit": "plate-width", "ambient": hex_color(ambient),
            "cookie": cookie_ref["path"], "cookieSize": list(cookie_size), "cookieSha256": cookie_ref["sha256"],
            "lights": [vs.rounded({key: light[key] for key in ("id", "u", "v", "color", "radius", "strength", "flicker")
                                   if key in light}, 5) for light in lights],
        })
        forge_core.write_json(stage_dir / "lights.json", document)
        outputs = [forge_core.file_ref(stage_dir / name, stage_dir) for name in names]
        refs = [forge_core.file_ref(path, final, sha256=sha) for path, sha in inputs]
        report = {"schema": LIGHTS_QA_SCHEMA,
                  **vs._local_qa_envelope(checks, method=method, not_proven=LIGHTS_NOT_PROVEN, inputs=refs,
                                          outputs=outputs, tool=TOOL, visual=plate is not None),
                  **extra}
        forge_core.write_json(stage_dir / "lights-qa.json", report)
    summary = {"output_dir": str(final.resolve()), "metadata": str((final / "lights.json").resolve()),
               "report": str((final / "lights-qa.json").resolve()),
               "cookie": str((final / "light-cookie.png").resolve()), "lights": len(lights),
               "status": report["status"], "_warnings": []}
    if overlay is not None:
        summary["overlay"] = str((final / "lights-overlay.png").resolve())
    return summary


def _strict(args: argparse.Namespace, checks: list[dict[str, Any]]) -> list[str]:
    failed = [item["id"] for item in checks if item["status"] == "fail"]
    if args.strict and failed:
        raise ValueError(f"strict check failed ({'; '.join(failed)}); nothing was written")
    return failed


# --------------------------------------------------------------------------- verbs

def cmd_extract(args: argparse.Namespace) -> dict[str, Any]:
    final = Path(args.output_dir)
    vs.refuse_existing(final)
    for name, value, low, high in (("--min-brightness", args.min_brightness, 0, 1),
                                   ("--min-contrast", args.min_contrast, 1e-6, 1),
                                   ("--background", args.background, 1e-4, 0.5),
                                   ("--min-size", args.min_size, 0, 1), ("--max-size", args.max_size, 1e-6, 1),
                                   ("--merge", args.merge, 0, 0.25), ("--radius-scale", args.radius_scale, 1e-6, 1e3)):
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} must be in {low:g}..{high:g}")
    if args.min_size > args.max_size:
        raise ValueError("--min-size must not exceed --max-size")
    if args.max_lights < 1 or (args.expect is not None and args.expect < 0):
        raise ValueError("--max-lights must be at least 1 and --expect at least 0")
    if args.keep_floor and args.stage is None:
        raise ValueError("--keep-floor needs --stage")
    plate, info = forge_core.load_rgba(args.plate)
    stage = vs.load_stage(args.stage) if args.stage is not None else None
    pixels = np.asarray(plate, float) / 255.0
    rgb = pixels[..., :3] * pixels[..., 3:]  # premultiplied: transparent areas count as black
    exclude = None
    if stage is not None and not args.keep_floor:
        # The plate contract keeps lamps off the walkable ground, so a bright blob centred on the ground or on
        # water is a reflection or a glint (hd2d-plates.md).
        exclude = np.zeros((plate.height, plate.width), bool)
        polygons = list(stage.ground) + [effect.polygon for effect in stage.effects if effect.kind in vs.WATER_KINDS]
        for polygon in polygons:
            exclude |= vs._local_polygon_mask(np.asarray(polygon, float) * plate.size, plate.size)
    lights, stats, rejected = detect_lights(rgb, color=args.color, min_brightness=args.min_brightness,
                                            min_contrast=args.min_contrast, background=args.background,
                                            min_size=args.min_size, max_size=args.max_size, merge=args.merge,
                                            max_lights=args.max_lights, radius_scale=args.radius_scale,
                                            exclude=exclude)
    for number, light in enumerate(lights, start=1):
        light["id"] = f"L{number}"
        if args.flicker is not None:
            light["flicker"] = {"hz": args.flicker[0], "depth": args.flicker[1],
                                "phase": round((number * GOLDEN) % 1.0, 4)}
    checks = []
    if stage is not None:
        same = plate.size == tuple(stage.source_size)
        checks.append(vs.check("plate matches the stage's sourceSize", "pass" if same else "fail", list(plate.size),
                               list(stage.source_size)))
    found = len(lights)
    if args.expect is not None:
        checks.append(vs.check("lights found", "pass" if found == args.expect else "fail", found, args.expect))
    else:
        checks.append(vs.check("lights found", "pass" if found else "warn", found, ">= 1"))
    edge = [light["id"] for light in lights if light["touchesEdge"]]
    checks.append(vs.check("lights clear of the plate edge", "warn" if edge else "pass", edge or None,
                           "a glow cut by the edge pulls its centre inward"))
    opaque = plate.getchannel("A").getextrema()[0] == 255
    checks.append(vs.check("plate is opaque", "pass" if opaque else "warn", opaque))
    checks.append(flicker_check(lights))
    failed = _strict(args, checks)
    method = (f"extract_scene_lights extract: local contrast (luminance minus a {stats['backgroundWindowPx']} px box "
              f"mean, 3x3 smoothed); peaks >= {args.min_contrast:g} contrast and {args.min_brightness:g} luminance, "
              f"{args.merge:g} of the width apart; half-peak region {args.min_size:g}..{args.max_size:g} of the width "
              f"across; contrast-weighted centroid; colour from the halo's added colour; {args.color} hue test.")
    extra = {"detector": {**stats, "color": args.color, "minBrightness": args.min_brightness,
                          "minContrast": args.min_contrast, "background": args.background, "minSize": args.min_size,
                          "maxSize": args.max_size, "merge": args.merge, "maxLights": args.max_lights,
                          "radiusScale": args.radius_scale, "groundAndWaterExcluded": exclude is not None},
             "lights": vs.rounded([{key: light[key] for key in ("id", "u", "v", "strength", "corePx", "box",
                                                               "touchesEdge")} for light in lights], 5),
             "rejected": vs.rounded(rejected, 5)}
    plate_input = (Path(args.plate), info["sha256"])
    inputs = [plate_input] + ([(Path(args.stage), None)] if stage is not None else [])
    summary = publish_lights(final, lights, source_size=plate.size, cookie_size=args.cookie_size, ambient=args.ambient,
                             plate=plate, inputs=inputs, plate_input=plate_input, checks=checks, method=method,
                             extra=extra)
    return {**summary, "failed": failed}


def cmd_cookie(args: argparse.Namespace) -> dict[str, Any]:
    final = Path(args.output_dir)
    vs.refuse_existing(final)
    path = Path(args.lights)
    try:
        doc = forge_core.read_json(path, strict=True)  # D28
        lights, recorded = parse_lights(doc)
    except ValueError as error:
        raise ValueError(f"{path.name}: {error}") from None
    plate = info = None
    if args.plate is not None:
        plate, info = forge_core.load_rgba(args.plate)
    size = recorded or (plate.size if plate is not None else None)
    if size is None:
        raise ValueError(f"{path.name} records no sourceSize; pass --plate so the pools get the plate's aspect")
    ambient = args.ambient
    if ambient is None:
        recorded_ambient = doc.get("ambient")
        valid = isinstance(recorded_ambient, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", recorded_ambient)
        ambient = _hex(recorded_ambient if valid else DEFAULT_AMBIENT)
    checks = [vs.check("lights file is valid", "pass", len(lights))]
    if plate is not None:
        same = tuple(size) == plate.size
        checks.append(vs.check("plate matches sourceSize", "pass" if same else "fail", list(plate.size), list(size)))
    checks.append(flicker_check(lights))
    failed = _strict(args, checks)
    plate_input = (Path(args.plate), info["sha256"]) if plate is not None else None
    inputs = [(path, None)] + ([plate_input] if plate_input is not None else [])
    method = ("extract_scene_lights cookie: lights.v1 values checked (u, v and strength 0..1, radius in (0, 1], "
              "colours, flicker), then the cookie re-baked from the edited lights.")
    summary = publish_lights(final, lights, source_size=tuple(size), cookie_size=args.cookie_size, ambient=ambient,
                             plate=plate, inputs=inputs, plate_input=plate_input, checks=checks, method=method,
                             extra={})
    return {**summary, "failed": failed}


def cmd_atmosphere(args: argparse.Namespace) -> dict[str, Any]:
    final = Path(args.output_dir)
    vs.refuse_existing(final)
    if args.max_motes < 0 or args.max_mobile_motes < 0:
        raise ValueError("--max-motes and --max-mobile-motes must be at least 0")
    path = Path(args.atmosphere)
    try:
        doc = forge_core.read_json(path, strict=True)  # D28
    except ValueError as error:
        raise ValueError(f"{path.name} is not valid JSON: {error}") from None
    try:
        checks, summary = atmosphere_checks(doc, max_motes=args.max_motes, max_mobile_motes=args.max_mobile_motes)
    except ValueError as error:
        raise ValueError(f"{path.name}: {error}") from None
    failed = _strict(args, checks)
    with forge_core.staged_output(final) as stage_dir:
        method = ("extract_scene_lights atmosphere: atmosphere.v1 structure, value ranges (opacity 0..1, vignette "
                  "0..1, non-negative bloom and saturation), colours, blend modes and mote counts against budgets.")
        report = {"schema": ATMOSPHERE_QA_SCHEMA,
                  **vs._local_qa_envelope(checks, method=method, not_proven=ATMOSPHERE_NOT_PROVEN,
                                          inputs=[forge_core.file_ref(path, final)], outputs=[], tool=TOOL),
                  "summary": summary}
        forge_core.write_json(stage_dir / "atmosphere-qa.json", report)
    return {"output_dir": str(final.resolve()), "metadata": str((final / "atmosphere-qa.json").resolve()),
            "status": report["status"], "failed": failed, "_warnings": []}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    extract = verbs.add_parser("extract", help="Find lights in a plate; write lights.json, a cookie and an overlay.",
                               description="Find the lights painted into a plate; write lights.json "
                                           "(generate2dmap.lights.v1), light-cookie.png, lights-overlay.png and "
                                           "lights-qa.json.")
    extract.add_argument("--plate", type=Path, required=True, help="The plate image.")
    extract.add_argument("--output-dir", type=Path, required=True, help="New folder for the outputs.")
    extract.add_argument("--expect", type=int, help="Number of lights the plate should have; a mismatch fails.")
    extract.add_argument("--stage", type=Path,
                         help="stage.json of the plate: blobs centred on its ground polygons or water (ripple) are "
                              "reflections or glints and are rejected.")
    extract.add_argument("--keep-floor", action="store_true",
                         help="With --stage, keep blobs on ground and water anyway (a lamp painted on the floor).")
    extract.add_argument("--color", choices=COLOR_MODES, default="warm",
                         help="Hue of the lights: warm (default, R - B >= 0.08), cool or any.")
    extract.add_argument("--min-brightness", type=float, default=0.7,
                         help="Minimum luminance at a light's peak, 0..1 (default 0.7).")
    extract.add_argument("--min-contrast", type=float, default=0.12,
                         help="Minimum peak luminance above the local mean, 0..1 (default 0.12).")
    extract.add_argument("--background", type=float, default=0.03,
                         help="Local-mean window radius as a fraction of the plate width (default 0.03).")
    extract.add_argument("--min-size", type=float, default=0.002,
                         help="Smallest light core (half-peak region) across, as a fraction of the plate width "
                              "(default 0.002).")
    extract.add_argument("--max-size", type=float, default=0.08,
                         help="Largest light core across, as a fraction of the plate width; wider bright areas are "
                              "lit walls or sky (default 0.08).")
    extract.add_argument("--merge", type=float, default=0.012,
                         help="Peaks closer than this fraction of the width count as one light (default 0.012).")
    extract.add_argument("--max-lights", type=int, default=32,
                         help="Examine peaks strongest first until N lights are found (default 32).")
    extract.add_argument("--radius-scale", type=float, default=20.0,
                         help="Light radius = this x the core's equivalent radius, clamped to 0.01..0.15 of the "
                              "width (default 20).")
    extract.add_argument("--cookie-size", type=_size, default=COOKIE_SIZE, metavar="WxH",
                         help="Light cookie size (default 512x288).")
    extract.add_argument("--ambient", type=_hex, default=_hex(DEFAULT_AMBIENT), metavar="#RRGGBB",
                         help=f"Cookie colour away from the lights (default {DEFAULT_AMBIENT}).")
    extract.add_argument("--flicker", type=_flicker, metavar="HZ:DEPTH",
                         help="Give every light a flicker of HZ cycles per second and DEPTH 0..1, with staggered "
                              "phases (default: steady lights).")
    extract.add_argument("--strict", action="store_true", help="Publish nothing and exit 1 when a check fails.")

    cookie = verbs.add_parser("cookie", help="Re-bake the cookie from an edited lights.json.",
                              description="Check an edited lights.json (lights added, moved or deleted by hand) and "
                                          "write it again with a re-baked light-cookie.png, lights-qa.json and, with "
                                          "--plate, lights-overlay.png.")
    cookie.add_argument("--lights", type=Path, required=True, help="The edited lights.json.")
    cookie.add_argument("--output-dir", type=Path, required=True, help="New folder for the outputs.")
    cookie.add_argument("--plate", type=Path, help="The plate, for the overlay (and the size when the file has none).")
    cookie.add_argument("--cookie-size", type=_size, default=COOKIE_SIZE, metavar="WxH",
                        help="Light cookie size (default 512x288).")
    cookie.add_argument("--ambient", type=_hex, metavar="#RRGGBB",
                        help=f"Cookie colour away from the lights (default: the file's ambient, else "
                             f"{DEFAULT_AMBIENT}).")
    cookie.add_argument("--strict", action="store_true", help="Publish nothing and exit 1 when a check fails.")

    atmosphere = verbs.add_parser("atmosphere", help="Check an atmosphere.v1 file; write atmosphere-qa.json.",
                                  description="Check a generate2dmap.atmosphere.v1 file (motes, mist, shafts, "
                                              "grade) and write atmosphere-qa.json.")
    atmosphere.add_argument("--atmosphere", type=Path, required=True, help="atmosphere.json.")
    atmosphere.add_argument("--output-dir", type=Path, required=True, help="New folder for atmosphere-qa.json.")
    atmosphere.add_argument("--max-motes", type=int, default=MOTE_LIMITS["max_motes"],
                            help=f"Desktop mote budget (default {MOTE_LIMITS['max_motes']}).")
    atmosphere.add_argument("--max-mobile-motes", type=int, default=MOTE_LIMITS["max_mobile_motes"],
                            help=f"Phone mote budget, from each layer's mobile count (default "
                                 f"{MOTE_LIMITS['max_mobile_motes']}).")
    atmosphere.add_argument("--strict", action="store_true", help="Publish nothing and exit 1 when a check fails.")
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = {"extract": cmd_extract, "cookie": cmd_cookie, "atmosphere": cmd_atmosphere}[args.verb](args)
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
