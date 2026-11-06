"""Hi-bit finish: generated art -> real pixel art with a hi-bit studio look.

The image model paints clean flat (lineless, 3-tones-per-material) art; this module makes the pixels:

1. ``kmeans_palette``: one palette per character (or plate) from the SOURCE pixels, k-means++ on
   luminance-weighted RGB, 14-18 colours for a character, 12-16 for a plate. Deterministic for a seed.
2. ``index_rgb`` + ``mode_downsample_blocks`` / ``mode_downsample``: every source (or supersampled) pixel
   takes its nearest palette index, then every output pixel takes the index covering most of its cell
   (a majority vote, never a blend); a cell covered less than ``coverage`` is transparent. With the
   previous frame's map, an index or opacity that still holds most of the cell is kept (hysteresis).
3. ``clean_isolated``: a lone pixel (no 4-neighbour of its colour) takes the neighbours' majority unless it
   contrasts strongly (eyes, highlights); 1 px islands detached from the body go.
4. ``auto_outline_color``: the outline colour comes from the image: the sprite's dominant saturated hue,
   very dark. A forced colour (for example a studio signature colour) is the caller's choice.
5. ``mode_filter3`` (plates): a 3x3 majority filter merges specks into the big flat shapes (paper cut).
6. ``shade_maps``: per palette colour, the in-palette colour one step darker and one step lighter, so a game
   can light, shade and flash sprites without adding colours.

Only numpy is needed. Indices use -1 for transparent.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

SCHEMA = "forge.hibit.v1"
RGB_WEIGHTS = np.sqrt(np.array([0.30, 0.59, 0.11], np.float32))
LUMA = np.array([0.30, 0.59, 0.11], np.float32)
CHARACTER_COLORS = 16
PLATE_COLORS = 14
COVERAGE = 0.45
HOLD_SHARE = 0.8          # a previous index is kept while it holds 80% of the winner's sub-pixels
COVERAGE_HOLD = 0.12      # a previously opaque cell stays opaque down to coverage - 0.12
KEEP_CONTRAST = 90.0      # weighted-RGB distance above which a lone pixel is a feature, not noise
TRANSPARENT = -1


# --------------------------------------------------------------------------- colour helpers

def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055) * 255.0


def to_oklab(rgb: Any) -> np.ndarray:
    """sRGB uint8 (..., 3) -> OKLab float64 (..., 3)."""
    lin = _srgb_to_linear(np.asarray(rgb, np.float64))
    lms = lin @ np.array([[0.4122214708, 0.2119034982, 0.0883024619],
                          [0.5363325363, 0.6806995451, 0.2817188376],
                          [0.0514459929, 0.1073969566, 0.6299787005]])
    lms = np.cbrt(lms)
    return lms @ np.array([[0.2104542553, 1.9779984951, 0.0259040371],
                           [0.7936177850, -2.4285922050, 0.7827717662],
                           [-0.0040720468, 0.4505937099, -0.8086757660]])


def from_oklab(lab: Any) -> np.ndarray:
    """OKLab (..., 3) -> sRGB uint8 (..., 3)."""
    lab = np.asarray(lab, np.float64)
    lms = lab @ np.array([[1.0, 1.0, 1.0],
                          [0.3963377774, -0.1055613458, -0.0894841775],
                          [0.2158037573, -0.0638541728, -1.2914855480]])
    lms = lms ** 3
    lin = lms @ np.array([[4.0767416621, -1.2684380046, -0.0041960863],
                          [-3.3077115913, 2.6097574011, -0.7034186147],
                          [0.2309699292, -0.3413193965, 1.7076147010]])
    return np.clip(np.round(_linear_to_srgb(lin)), 0, 255).astype(np.uint8)


def parse_hex(value: str) -> tuple[int, int, int]:
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in text):
        raise ValueError(f"not a #RRGGBB colour: {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def hex_of(rgb: Sequence[int]) -> str:
    r, g, b = (int(v) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


# --------------------------------------------------------------------------- palette

def _assign(points: np.ndarray, centres: np.ndarray, chunk: int = 131072) -> np.ndarray:
    out = np.empty(len(points), np.int32)
    for start in range(0, len(points), chunk):
        block = points[start:start + chunk]
        out[start:start + chunk] = np.argmin(((block[:, None, :] - centres[None]) ** 2).sum(-1), axis=1)
    return out


def kmeans_palette(pixels: Any, k: int, *, seed: int = 0, sample: int = 60000, iters: int = 12) -> np.ndarray:
    """``(n, 3)`` uint8 palette (sorted dark -> light) of ``pixels`` (any (..., 3) uint8 array of opaque
    colours): k-means++ then Lloyd steps on luminance-weighted RGB, the hi-bit studio recipe."""
    points = np.asarray(pixels, np.float32).reshape(-1, 3)
    if len(points) == 0:
        raise ValueError("no opaque pixels to build a palette from")
    if not 1 <= int(k) <= 254:
        raise ValueError("k must be 1..254")
    rng = np.random.default_rng(int(seed))
    weighted = points * RGB_WEIGHTS
    if len(weighted) > sample:
        weighted = weighted[rng.choice(len(weighted), size=sample, replace=False)]
    distinct = np.unique(np.round(weighted, 2), axis=0)
    k = int(min(k, len(distinct)))
    centres = [weighted[rng.integers(len(weighted))]]
    nearest = ((weighted - centres[0]) ** 2).sum(1)
    for _ in range(k - 1):
        total = float(nearest.sum())
        pick = rng.choice(len(weighted), p=nearest / total) if total > 0 else int(rng.integers(len(weighted)))
        centres.append(weighted[pick])
        nearest = np.minimum(nearest, ((weighted - weighted[pick]) ** 2).sum(1))
    centres = np.array(centres, np.float32)
    for _ in range(int(iters)):
        labels = _assign(weighted, centres)
        for j in range(k):
            members = weighted[labels == j]
            if len(members):
                centres[j] = members.mean(0)
    palette = np.clip(np.round(centres / RGB_WEIGHTS), 0, 255).astype(np.uint8)
    palette = np.unique(palette, axis=0)
    return palette[np.argsort(palette.astype(np.float32) @ LUMA, kind="stable")]


def index_rgb(rgb: Any, palette: Any) -> np.ndarray:
    """Nearest palette index (luminance-weighted RGB) of every pixel of ``rgb`` (..., 3)."""
    rgb = np.asarray(rgb)
    flat = rgb.reshape(-1, 3).astype(np.float32) * RGB_WEIGHTS
    centres = np.asarray(palette, np.float32).reshape(-1, 3) * RGB_WEIGHTS
    return _assign(flat, centres).reshape(rgb.shape[:-1])


def index_rgba(rgba: Any, palette: Any, alpha_threshold: int = 128) -> np.ndarray:
    """Index map of an RGBA image: transparent (alpha below the threshold) is -1."""
    rgba = np.asarray(rgba)
    out = np.full(rgba.shape[:2], TRANSPARENT, np.int32)
    solid = rgba[..., 3] >= alpha_threshold
    if solid.any():
        out[solid] = index_rgb(rgba[..., :3][solid], palette)
    return out


def usage_counts(idx: np.ndarray, colors: int) -> np.ndarray:
    used = idx[idx >= 0]
    return np.bincount(used.ravel(), minlength=colors)[:colors]


# --------------------------------------------------------------------------- downsample

def mode_downsample_blocks(sub: np.ndarray, factor: int, colors: int, *, coverage: float = COVERAGE,
                           previous: np.ndarray | None = None, hold: float = HOLD_SHARE,
                           coverage_hold: float = COVERAGE_HOLD) -> np.ndarray:
    """``sub``: an index map with exactly ``factor`` x ``factor`` sub-pixels per output pixel (-1 transparent).
    Every output pixel takes the index covering most of its cell; a cell under ``coverage`` is transparent.
    ``previous`` (the last frame's output) keeps an index while it holds ``hold`` of the winner's count, and
    an opaque cell down to ``coverage - coverage_hold``: the hysteresis that stops shimmering."""
    f = int(factor)
    height, width = sub.shape[0] // f, sub.shape[1] // f
    blocks = sub[:height * f, :width * f].reshape(height, f, width, f).transpose(0, 2, 1, 3).reshape(-1, f * f)
    solid = blocks >= 0
    cover = solid.mean(1)
    cell = np.repeat(np.arange(height * width, dtype=np.int64), f * f).reshape(-1, f * f)
    counts = np.bincount((cell[solid] * colors + blocks[solid]).ravel(),
                         minlength=height * width * colors).reshape(height * width, colors)
    best = counts.argmax(1)
    best_n = counts.max(1)
    alive = cover >= coverage
    if previous is not None:
        prev = np.asarray(previous).reshape(-1)
        was = prev >= 0
        prev_n = np.where(was, counts[np.arange(height * width), np.clip(prev, 0, colors - 1)], 0)
        keep = was & (prev_n > 0) & (prev_n >= hold * best_n)
        best = np.where(keep, prev, best)
        alive = alive | (was & (cover >= coverage - coverage_hold) & (best_n > 0))
    return np.where(alive, best, TRANSPARENT).reshape(height, width).astype(np.int32)


def mode_downsample(idx: np.ndarray, out_h: int, out_w: int, colors: int, *, coverage: float = COVERAGE) -> np.ndarray:
    """Any-ratio majority downsample of a full-resolution index map (-1 transparent) to out_h x out_w."""
    height, width = idx.shape
    by = (np.arange(height) * out_h // height)[:, None]
    bx = (np.arange(width) * out_w // width)[None, :]
    block = (by * out_w + bx).astype(np.int64)
    cells = out_h * out_w
    solid = idx >= 0
    counts = np.bincount((block[solid] * colors + idx[solid]).ravel(), minlength=cells * colors).reshape(cells, colors)
    area = np.bincount(block.ravel(), minlength=cells)
    cover = counts.sum(1) / np.maximum(area, 1)
    out = counts.argmax(1)
    return np.where(cover >= coverage, out, TRANSPARENT).reshape(out_h, out_w).astype(np.int32)


# --------------------------------------------------------------------------- cleanup and filters

def _shift(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    out = np.zeros_like(mask)
    height, width = mask.shape
    out[max(dy, 0):height + min(dy, 0), max(dx, 0):width + min(dx, 0)] = \
        mask[max(-dy, 0):height + min(-dy, 0), max(-dx, 0):width + min(-dx, 0)]
    return out


def dilate4(mask: np.ndarray) -> np.ndarray:
    return mask | _shift(mask, 1, 0) | _shift(mask, -1, 0) | _shift(mask, 0, 1) | _shift(mask, 0, -1)


def erode4(mask: np.ndarray) -> np.ndarray:
    return mask & _shift(mask, 1, 0) & _shift(mask, -1, 0) & _shift(mask, 0, 1) & _shift(mask, 0, -1)


def clean_isolated(idx: np.ndarray, palette: Any | None = None, *, passes: int = 2,
                   keep_contrast: float = KEEP_CONTRAST, drop_islands: bool = True) -> np.ndarray:
    """Lone pixels inside the body take the 4-neighbours' majority unless they contrast by ``keep_contrast``
    (eyes, glints stay); with ``drop_islands`` 1 px islands detached from the body become transparent."""
    data = np.array(idx, np.int32, copy=True)
    pal = None if palette is None else np.asarray(palette, np.float32) * RGB_WEIGHTS
    for _ in range(int(passes)):
        padded = np.pad(data, 1, mode="edge")
        nb = np.stack([padded[1:-1, 2:], padded[1:-1, :-2], padded[2:, 1:-1], padded[:-2, 1:-1]])
        agree = np.stack([(nb == nb[j]).sum(0) for j in range(4)])
        best = np.take_along_axis(nb, agree.argmax(0)[None], 0)[0]
        solid = data >= 0
        lone = solid & erode4(solid) & ~np.any(nb == data, axis=0) & (agree.max(0) >= 2) & (best >= 0)
        if pal is not None:
            gap = np.sqrt(((pal[np.clip(data, 0, None)] - pal[np.clip(best, 0, None)]) ** 2).sum(-1))
            lone &= gap < keep_contrast
        data = np.where(lone, best, data)
    if drop_islands:
        solid = data >= 0
        island = solid & ~(_shift(solid, 1, 0) | _shift(solid, -1, 0) | _shift(solid, 0, 1) | _shift(solid, 0, -1))
        data[island] = TRANSPARENT
    return data


def mode_filter3(idx: np.ndarray, colors: int, passes: int = 1) -> np.ndarray:
    """3x3 majority filter (plates): specks merge into the surrounding big shapes; a tie keeps the colour."""
    data = np.array(idx, np.int32, copy=True)
    height, width = data.shape
    for _ in range(int(passes)):
        padded = np.pad(data, 1, mode="edge")
        nb = np.stack([padded[dy:dy + height, dx:dx + width] for dy in range(3) for dx in range(3)])
        counts = np.stack([(nb == c).sum(0) for c in range(colors)])
        own = np.take_along_axis(counts, np.clip(data, 0, None)[None], 0)[0]
        data = np.where((data >= 0) & (own < counts.max(0)), counts.argmax(0), data)
    return data


# --------------------------------------------------------------------------- outline and lighting

def auto_outline_color(palette: Any, counts: Any | None = None, *, lightness: float = 0.22) -> tuple[int, int, int]:
    """An outline colour taken from the image: the dominant saturated hue of the sprite (pixel count x
    chroma, the heaviest of 12 hue sectors), very dark and moderately saturated. A sprite with no
    saturated colour gets a cool near-black."""
    pal = np.asarray(palette, np.uint8).reshape(-1, 3)
    weights = np.ones(len(pal)) if counts is None else np.asarray(counts, np.float64)[:len(pal)]
    lab = to_oklab(pal)
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    hue = np.arctan2(lab[:, 2], lab[:, 1])
    usable = (chroma > 0.035) & (lab[:, 0] > 0.25) & (lab[:, 0] < 0.95)
    weight = np.where(usable, weights * chroma, 0.0)
    if weight.sum() <= 0:
        hue_c, chroma_c = math.radians(270.0), 0.04
    else:
        sector = np.floor((hue + math.pi) / (2 * math.pi) * 12).astype(int) % 12
        totals = np.bincount(sector, weights=weight, minlength=12)
        best = int(totals.argmax())
        member = (sector == best) & (weight > 0)
        w = weight[member]
        hue_c = math.atan2(float((np.sin(hue[member]) * w).sum()), float((np.cos(hue[member]) * w).sum()))
        chroma_c = float((chroma[member] * w).sum() / w.sum())
    c = float(np.clip(0.55 * chroma_c, 0.035, 0.085))
    rgb = from_oklab(np.array([lightness, c * math.cos(hue_c), c * math.sin(hue_c)]))
    return int(rgb[0]), int(rgb[1]), int(rgb[2])


def outline_ring(idx: np.ndarray) -> np.ndarray:
    """Transparent pixels 4-adjacent to the sprite (needs a 1 px transparent border to be complete)."""
    solid = idx >= 0
    return dilate4(solid) & ~solid


def shade_maps(palette: Any, *, k_dark: float = 0.68, k_light: float = 0.3, max_dist: float = 70.0
               ) -> tuple[np.ndarray, np.ndarray]:
    """Per colour, the in-palette colour one step darker and one step lighter (the nearest to the colour
    darkened / lightened, among colours at least 8 luma darker / lighter); itself when none is close enough."""
    p = np.asarray(palette, np.float64).reshape(-1, 3)
    luma = p @ LUMA.astype(np.float64)

    def pick(target: np.ndarray, ok: np.ndarray) -> np.ndarray:
        dist = ((p[None, :, :] - target[:, None, :]) ** 2).sum(-1)
        dist[~ok] = np.inf
        return np.where(dist.min(1) <= max_dist ** 2, dist.argmin(1), np.arange(len(p))).astype(np.int32)

    dark = pick(p * k_dark, luma[None, :] < luma[:, None] - 8)
    light = pick(p + (255 - p) * k_light, luma[None, :] > luma[:, None] + 8)
    return dark, light


def render(idx: np.ndarray, palette: Any) -> np.ndarray:
    """Index map -> RGBA uint8 (binary alpha)."""
    pal = np.asarray(palette, np.uint8).reshape(-1, 3)
    out = np.zeros(idx.shape + (4,), np.uint8)
    solid = idx >= 0
    out[solid, :3] = pal[idx[solid]]
    out[solid, 3] = 255
    return out


# --------------------------------------------------------------------------- whole images

def pixelize_rgba(rgba: Any, out_h: int, *, colors: int = CHARACTER_COLORS, coverage: float = COVERAGE,
                  palette: Any | None = None, seed: int = 0, outline: str | None = "auto",
                  alpha_threshold: int = 128) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """One keyed still (a master, a prop, an icon) -> (index map, palette, info) at ``out_h`` px tall
    (outline included when there is one). ``outline``: "auto", "none" or a #RRGGBB colour."""
    rgba = np.asarray(rgba, np.uint8)
    solid = rgba[..., 3] >= alpha_threshold
    if not solid.any():
        raise ValueError("the image has no opaque pixels")
    ys, xs = np.nonzero(solid)
    crop = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    pad = 0 if outline in (None, "none") else 1
    body_h = int(out_h) - 2 * pad
    if body_h < 4:
        raise ValueError("out_h is too small")
    pal = kmeans_palette(crop[..., :3][crop[..., 3] >= alpha_threshold], colors, seed=seed) if palette is None \
        else np.asarray(palette, np.uint8).reshape(-1, 3)
    full = index_rgba(crop, pal, alpha_threshold)
    out_w = max(1, round(crop.shape[1] * body_h / crop.shape[0]))
    small = clean_isolated(mode_downsample(full, body_h, out_w, len(pal), coverage=coverage), pal)
    info: dict[str, Any] = {"colors": int(len(pal)), "outline": None}
    if pad:
        small = np.pad(small, 1, constant_values=TRANSPARENT)
        colour = auto_outline_color(pal, usage_counts(small, len(pal))) if outline == "auto" else parse_hex(outline)
        pal = np.vstack([pal, np.array([colour], np.uint8)])
        ring = outline_ring(small)
        small = np.where(ring, len(pal) - 1, small)
        info["outline"] = {"mode": outline, "color": hex_of(colour), "index": int(len(pal) - 1)}
    return small, pal, info


def plate_crop(width: int, height: int, out_w: int, out_h: int, x_frac: float = 0.5,
               y_frac: float = 0.5) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1): the largest window of the out_w:out_h aspect inside a width x height painting, placed by
    ``x_frac`` / ``y_frac`` (0 = left / top, 1 = right / bottom) along the axis that has room to spare."""
    crop_h = round(width * out_h / out_w)
    if crop_h <= height:
        y0 = round((height - crop_h) * float(y_frac))
        return 0, y0, width, y0 + crop_h
    crop_w = min(width, round(height * out_w / out_h))
    x0 = round((width - crop_w) * float(x_frac))
    return x0, 0, x0 + crop_w, height


def pixelize_plate(rgb: Any, out_w: int, out_h: int, *, colors: int = PLATE_COLORS, y_frac: float = 0.5,
                   smooth: int = 1, seed: int = 0, x_frac: float = 0.5,
                   palette: Any | None = None) -> tuple[np.ndarray, np.ndarray]:
    """A background plate -> (index map out_h x out_w, palette): crop to the canvas aspect (``plate_crop``),
    palette (learned, or ``palette``), majority downsample, lone-pixel cleanup, ``smooth`` 3x3 majority passes."""
    rgb = np.asarray(rgb, np.uint8)[..., :3]
    x0, y0, x1, y1 = plate_crop(rgb.shape[1], rgb.shape[0], out_w, out_h, x_frac, y_frac)
    rgb = rgb[y0:y1, x0:x1]
    pal = kmeans_palette(rgb, colors, seed=seed) if palette is None \
        else np.asarray(palette, np.uint8).reshape(-1, 3)
    full = index_rgb(rgb, pal)
    small = mode_downsample(full, int(out_h), int(out_w), len(pal), coverage=0.0)
    small = clean_isolated(small, pal, drop_islands=False)
    return mode_filter3(small, len(pal), smooth), pal
