"""Pixel looks: a generated "pixel art" picture -> a real 1x sprite, and video frames -> that sprite's own grid.

An image model paints pixel art as big squares that are not all the same size and do not sit on an integer
lattice; a video model then moves the figure off whatever grid it had. This module makes real pixels of both:

1. ``snap_picture``: the picture's own grid is found (edge profiles, the finest strong autocorrelation period
   of each axis, an elastic walk along the profile's peaks) and every cell gives one colour: the median of its
   inner part; a cell that is mostly key colour is transparent. ``merge_palette`` then merges the cell colours
   in OKLab, closest pair first, so a rare colour (an eye) keeps its own place.
2. ``lock_palette`` / ``close_outline`` / ``drop_key_mixed`` / ``remove_islands`` / ``fill_single_holes``: the
   small mechanical fixes of a snapped sprite or frame. Nothing here blends two colours.
3. ``enlarge``: a 1x sprite as a picture on one exact grid (nearest, a whole factor): the reference an image
   model is given for the next view, the manner anchor for the next character, the first frame of a clip.
4. ``lattice_shift`` + ``snap_frame``: a clip whose first frame is such a picture (prepare_i2v_input.py
   --pixel-art) has a known lattice. Every frame is read on that one lattice, which is the game's own grid:
   what stands still stays on its cells, what moves is read where it is, and every colour goes to the nearest
   colour of the sprite's palette.
5. ``find_cycle`` / ``pick_cycle`` / ``pick_motion``: the frames of one loop, found from the frames themselves,
   or of one action, spaced by how much moves.
6. ``head_lock``: where a frame's head is nearly the sprite's own head, the sprite's head is put there (whole
   pixels), so a face never boils.

7. ``fewer_colours`` / ``rule_outline`` / ``boots`` / ``fit_lattice`` / ``read_lattice`` / ``to_palette``: a master
   and what is made from it. A softly shaded sprite keeps its hues (k-means, not pair merging); its outline is
   closed by rule; a picture that shows a known sprite again (with one change, or with a second figure beside
   it) is read on that sprite's own lattice, so one cell of the picture is one cell of the sprite.

Only numpy is needed. Sprites and frames are uint8 RGBA arrays with binary alpha (0 or 255).
"""
from __future__ import annotations

import heapq
import math
from typing import Any, Sequence

import numpy as np

SCHEMA = "forge.pixelgrid.v1"
FLOOR = 10.0              # a channel step below this is noise, not an edge
KEY_DISTANCE = 70.0       # RGB distance to the key colour that is still background in a still picture
VIDEO_KEY_DISTANCE = 110.0  # ... in a video frame (compression pulls the key toward the figure)
PERIOD_MIN, PERIOD_MAX = 3, 80
WALK_MIN = 8              # cells under this many px are snapped on the bent lattice, not by the walk
STRONG_PEAK = 0.5         # an autocorrelation peak this strong against the strongest is a period, not noise
MERGE_GAP = 0.035         # OKLab distance under which two cell colours always merge
PURE = 48.0               # RGB distance to a cell's colour that still counts as that colour (the purity of a snap)
TILE = 12                 # cells of one tile of the bent lattice
TILE_SURE = 0.25          # how much more change a tile's best shift must lie on than its average shift
FLAT_SPREAD = 40.0        # a video cell whose inner part spreads more than this (max channel std) is "not flat"
LOCK_SHARE = 0.45         # the share of a head's cells that must already be equal before it is locked
UNMIX_PENALTY = 40.0      # RGB distance a full share of key costs when an edge cell is explained as palette colour + key
UNMIX_MOST = 0.5          # an edge cell of a video frame that is more key than this goes
UNMIX_CLEAR = 0.15        # a share of key that is counted as "this cell was mixed"
UNMIX_PASSES = 3          # rings of edge cells that are unmixed, each bared by the one before
UNMIX_LEFT = 28.0         # RGB distance an explanation as colour + key may leave over and still be believed
NEAR4 = ((-1, 0), (1, 0), (0, -1), (0, 1))


# --------------------------------------------------------------------------- colour

def to_oklab(rgb: Any) -> np.ndarray:
    """sRGB 0..255 (..., 3) -> OKLab float64 (..., 3)."""
    c = np.asarray(rgb, np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    l = 0.4122214708 * c[..., 0] + 0.5363325363 * c[..., 1] + 0.0514459929 * c[..., 2]
    m = 0.2119034982 * c[..., 0] + 0.6806995451 * c[..., 1] + 0.1073969566 * c[..., 2]
    s = 0.0883024619 * c[..., 0] + 0.2817188376 * c[..., 1] + 0.6299787005 * c[..., 2]
    l, m, s = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    return np.stack([0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
                     1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
                     0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s], axis=-1)


def from_oklab(lab: Any) -> np.ndarray:
    """OKLab (..., 3) -> sRGB 0..255 float64 (..., 3), clipped."""
    lab = np.asarray(lab, np.float64)
    l = lab[..., 0] + 0.3963377774 * lab[..., 1] + 0.2158037573 * lab[..., 2]
    m = lab[..., 0] - 0.1055613458 * lab[..., 1] - 0.0638541728 * lab[..., 2]
    s = lab[..., 0] - 0.0894841775 * lab[..., 1] - 1.2914855480 * lab[..., 2]
    l, m, s = l ** 3, m ** 3, s ** 3
    c = np.stack([4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                  -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                  -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s], axis=-1)
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055) * 255.0


def measure_key(rgb: np.ndarray, ring: int = 8) -> np.ndarray:
    """The flat key colour of a picture or frame: the median of its border ring."""
    a = np.asarray(rgb, np.float64)[..., :3]
    edge = np.concatenate([a[:ring].reshape(-1, 3), a[-ring:].reshape(-1, 3), a[:, :ring].reshape(-1, 3),
                           a[:, -ring:].reshape(-1, 3)])
    return np.median(edge, axis=0)


def palette_of(rgba: np.ndarray) -> np.ndarray:
    """The sprite's colours, sorted, uint8 (n, 3)."""
    op = rgba[..., 3] > 0
    return np.array(sorted({tuple(int(v) for v in c) for c in rgba[op][:, :3]}), dtype=np.uint8).reshape(-1, 3)


def merge_palette(colours: np.ndarray, count: int, gap: float = MERGE_GAP) -> tuple[np.ndarray, np.ndarray]:
    """Cell colours (n, 3) merged in OKLab, closest pair first, down to ``count`` colours; a pair closer than
    ``gap`` always merges. Returns the palette (dark to light, uint8) and every input colour's palette index."""
    uniq, inverse, counts = np.unique(np.asarray(colours, np.uint8).reshape(-1, 3), axis=0, return_inverse=True,
                                      return_counts=True)
    lab = to_oklab(uniq)
    weight = counts.astype(np.float64)
    total = len(uniq)
    group = np.arange(total)
    alive = np.ones(total, dtype=bool)
    left = total

    def nearest(c: int) -> tuple[np.ndarray, int, float]:
        d = ((lab - lab[c]) ** 2).sum(axis=1)
        d[~alive] = np.inf
        d[c] = np.inf
        j = int(d.argmin())
        return d, j, float(d[j])

    near_d = np.full(total, np.inf)
    near_j = np.zeros(total, dtype=np.int64)
    if total > 1:
        for c in range(total):
            _, near_j[c], near_d[c] = nearest(c)
    while left > 1:
        a = int(near_d.argmin())
        b = int(near_j[a])
        if left <= count and near_d[a] >= gap * gap:
            break
        if b < a:
            a, b = b, a
        lab[a] = (lab[a] * weight[a] + lab[b] * weight[b]) / (weight[a] + weight[b])
        weight[a] += weight[b]
        group[group == b] = a
        alive[b] = False
        near_d[b] = np.inf
        left -= 1
        if left == 1:
            break
        d, near_j[a], near_d[a] = nearest(a)
        stale = alive & ((near_j == a) | (near_j == b))
        stale[a] = False
        rest = alive & ~stale
        closer = rest & (d < near_d)
        near_d[closer] = d[closer]
        near_j[closer] = a
        near_j[rest & (d == near_d) & (a < near_j)] = a
        for c in np.where(stale)[0]:
            _, near_j[c], near_d[c] = nearest(int(c))
    ids = [int(g) for g in np.where(alive)[0]]
    order = sorted(ids, key=lambda g: lab[g][0])
    palette = np.clip(from_oklab(lab[order]) + 0.5, 0, 255).astype(np.uint8)
    remap = {g: k for k, g in enumerate(order)}
    index = np.array([remap[int(g)] for g in group])[inverse.reshape(-1)]
    return palette, index


def lock_palette(rgba: np.ndarray, palette: np.ndarray) -> tuple[np.ndarray, dict]:
    """Every opaque colour to the nearest colour of ``palette`` (OKLab). Returns the sprite and what moved."""
    out = rgba.copy()
    op = out[..., 3] > 0
    if not op.any():
        return out, {"cells": 0, "moved": 0, "worst": 0.0}
    pal = np.asarray(palette, np.uint8).reshape(-1, 3)
    d = ((to_oklab(out[op][:, :3])[:, None, :] - to_oklab(pal)[None, :, :]) ** 2).sum(axis=2)
    out[op, :3] = pal[d.argmin(axis=1)]
    far = np.sqrt(d.min(axis=1))
    return out, {"cells": int(op.sum()), "moved": int((far > 0.02).sum()), "worst": round(float(far.max()), 4)}


# --------------------------------------------------------------------------- the grid of a still picture

def _smooth(p: np.ndarray, k: int = 3) -> np.ndarray:
    return np.convolve(p, np.ones(k) / k, mode="same")


def edge_profile(a: np.ndarray, axis: int, lo: int, hi: int) -> np.ndarray:
    """Colour change across every boundary along ``axis`` (1: between columns), summed over [lo, hi) of the other."""
    if axis == 1:
        d = np.abs(a[lo:hi, 1:] - a[lo:hi, :-1]).max(axis=2)
    else:
        d = np.abs(a[1:, lo:hi] - a[:-1, lo:hi]).max(axis=2)
    d = np.where(d < FLOOR, 0.0, d)
    return _smooth(d.sum(axis=0 if axis == 1 else 1))


def _autocorrelation(p: np.ndarray, top: int) -> np.ndarray:
    q = p - p.mean()
    top = min(top, len(q) - 2)
    return np.array([0.0] + [float((q[:-lag] * q[lag:]).sum() / max(1, len(q) - lag)) for lag in range(1, top + 1)])


def find_period(profile: np.ndarray, lo: int = PERIOD_MIN, hi: int = PERIOD_MAX) -> tuple[float, int]:
    """The grid period of an edge profile in px: the finest lag whose autocorrelation peak is strong (a period
    shows at its multiples too, the multiples never below it), refined by the peaks near its multiples.
    Returns (period, how many multiples agreed); (0.0, 0) when the profile has no period."""
    ac = _autocorrelation(profile, hi * 8 + 4)
    hi = min(hi, len(ac) - 2)
    found = [(lag, ac[lag]) for lag in range(max(2, lo), hi + 1) if ac[lag] > 0 and ac[lag] >= ac[lag - 1] and ac[lag] >= ac[lag + 1]]
    if not found:
        return 0.0, 0
    top = max(v for _, v in found)
    lag = min(l for l, v in found if v >= STRONG_PEAK * top)
    period, used = float(lag), 1
    for k in range(2, 9):
        centre = int(round(period * k))
        if centre + 3 >= len(ac):
            break
        window = ac[centre - 2:centre + 3]
        period, used = (centre - 2 + int(np.argmax(window))) / k, k
    return round(period, 3), used


def find_period_coarse(profile: np.ndarray, lo: int = WALK_MIN, hi: int = PERIOD_MAX) -> float:
    """The period the walk starts from, for cells of ``lo`` px and more: the first strong autocorrelation peak,
    refined by the peaks at twice and three times its lag. 0.0 when there is none."""
    ac = _autocorrelation(profile, hi * 3 + 2)
    hi = min(hi, len(ac) - 2)
    if hi <= lo:
        return 0.0
    best = float(ac[lo:hi + 1].max())
    first = next((lag for lag in range(lo + 1, hi) if ac[lag] >= 0.55 * best and ac[lag] >= ac[lag - 1] and ac[lag] >= ac[lag + 1]),
                 lo + int(ac[lo:hi + 1].argmax()))
    period = float(first)
    for k in (2, 3):
        centre = int(round(period * k))
        reach = max(2, int(round(period * 0.2)))
        a, b = max(1, centre - reach), min(len(ac) - 1, centre + reach)
        if b > a:
            j = a + int(ac[a:b + 1].argmax())
            if ac[j] >= 0.3 * ac[first]:
                period = j / k
    return period


def grid_lines(profile: np.ndarray, period: float, lo: int, hi: int) -> tuple[list[float], int]:
    """Grid lines over [lo, hi]: an elastic walk from the strongest peak, both ways; each step snaps to the nearest
    profile peak when there is one, so a drifting grid is followed. Line k is the boundary before pixel k."""
    p = profile
    peaks = [i for i in range(1, len(p) - 1) if p[i] >= p[i - 1] and p[i] >= p[i + 1] and p[i] > 0]
    inside = [i for i in peaks if lo <= i <= hi]
    if not inside or period <= 0:
        return [], 0
    strongest = sorted((p[i] for i in peaks), reverse=True)[:20]
    threshold = 0.2 * float(np.median(strongest))
    lines = [max(inside, key=lambda i: p[i]) + 1.0]
    snapped = 1

    def step(pos: float, sign: int, per: float) -> tuple[float, bool]:
        predicted = pos + sign * per
        reach = max(1, int(round(per * 0.3)))
        a, b = max(0, int(round(predicted - 1)) - reach), min(len(p) - 1, int(round(predicted - 1)) + reach)
        if b <= a:
            return predicted, False
        j = a + int(p[a:b + 1].argmax())
        return (j + 1.0, True) if p[j] >= threshold else (predicted, False)

    for sign in (1, -1):
        pos, per, gaps = lines[0], period, []
        while lo - period <= pos + sign * per <= hi + period:
            new, ok = step(pos, sign, per)
            if ok:
                snapped += 1
                gaps.append(abs(new - pos))
                per = 0.5 * period + 0.5 * float(np.mean(gaps[-4:]))
            pos = new
            lines.append(pos)
    return sorted(lines), snapped


def _profile_at(p: np.ndarray, pos: Any) -> np.ndarray:
    """The profile at float line positions (line k is the boundary before pixel k: index k - 1)."""
    return np.interp(np.asarray(pos, np.float64) - 1.0, np.arange(len(p)), p, left=0.0, right=0.0)


def _regular_lattice(p: np.ndarray, lo: int, hi: int, period: float) -> tuple[float, float]:
    """The regular lattice (a period within 3 percent of ``period``, and its first line) on the most colour change."""
    best = (-1.0, period, float(lo))
    for per in np.arange(period * 0.97, period * 1.03 + 1e-9, 0.005):
        k = np.arange(0, int((hi - lo) / per) + 2)
        for phase in np.arange(0, per, 0.25):
            score = float(_profile_at(p, lo + phase + k * per).mean())
            if score > best[0]:
                best = (score, float(per), float(lo + phase))
    return best[1], best[2]


def _tile_shift(p: np.ndarray, origin: float, per: float, k0: int, k1: int, centre: float) -> tuple[float, float]:
    """The shift within half a cell of ``centre`` that puts the lines k0..k1 on the most change, and how sure."""
    k = np.arange(k0, k1 + 1)
    shifts = centre + np.arange(-per / 2, per / 2, 0.25)
    score = np.array([_profile_at(p, origin + k * per + d).mean() for d in shifts])
    mean = float(score.mean())
    if mean <= 1e-6:
        return centre, 0.0
    j = int(score.argmax())
    return float(shifts[j]), float(score[j] / mean - 1.0)


def _shift_field(profiles: Sequence[np.ndarray], origin: float, per: float, bands: int, windows: int, step: int) -> np.ndarray:
    """The shifts of one line family on the tile grid (bands x windows), grown from the surest tile outwards: a
    tile looks for its shift only within half a cell of what its finished neighbours say."""
    first = np.zeros((bands, windows))
    for b in range(bands):
        for w in range(windows):
            first[b, w] = _tile_shift(profiles[b], origin, per, w * step, w * step + TILE, 0.0)[1]
    shift = np.zeros((bands, windows))
    sure = np.zeros((bands, windows))
    done = np.zeros((bands, windows), dtype=bool)
    b, w = np.unravel_index(int(first.argmax()), first.shape)
    heap = [(-first[b, w], int(b), int(w))]
    queued = {(int(b), int(w))}
    while heap:
        _, b, w = heapq.heappop(heap)
        near = [(bb, ww) for bb in (b - 1, b, b + 1) for ww in (w - 1, w, w + 1)
                if 0 <= bb < bands and 0 <= ww < windows and done[bb, ww]]
        centre = 0.0
        if near:
            weight = np.array([sure[t] + 0.05 for t in near])
            centre = float((np.array([shift[t] for t in near]) * weight).sum() / weight.sum())
        d, c = _tile_shift(profiles[b], origin, per, w * step, w * step + TILE, centre)
        shift[b, w], sure[b, w] = (d, c) if c >= TILE_SURE else (centre, 0.0)
        done[b, w] = True
        for bb in (b - 1, b, b + 1):
            for ww in (w - 1, w, w + 1):
                if 0 <= bb < bands and 0 <= ww < windows and not done[bb, ww] and (bb, ww) not in queued:
                    queued.add((bb, ww))
                    heapq.heappush(heap, (-first[bb, ww], bb, ww))
    out = shift.copy()
    for b in range(bands):
        for w in range(windows):
            box = (slice(max(0, b - 1), b + 2), slice(max(0, w - 1), w + 2))
            weight = sure[box] + 0.05
            weight[min(b, 1), min(w, 1)] += 1.0
            out[b, w] = (shift[box] * weight).sum() / weight.sum()
    return out


def _sample_cells(a: np.ndarray, background: np.ndarray, left: np.ndarray, right: np.ndarray, top: np.ndarray,
                  bottom: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One colour per cell (the median of its inner part), its opacity and its purity: the share of the inner
    part that has the cell's colour. ``left`` .. ``bottom`` are the cell's lines, (rows, cols) each."""
    height, width = a.shape[:2]
    rows, cols = left.shape
    colour = np.zeros((rows, cols, 3), dtype=np.uint8)
    alpha = np.zeros((rows, cols), dtype=np.uint8)
    purity = np.zeros((rows, cols), dtype=np.float32)
    for j in range(rows):
        for i in range(cols):
            mx = max(1.0, (right[j, i] - left[j, i]) * 0.2)
            my = max(1.0, (bottom[j, i] - top[j, i]) * 0.2)
            xa, ya = max(0, int(round(left[j, i] + mx))), max(0, int(round(top[j, i] + my)))
            xb = min(width, max(int(round(right[j, i] - mx)), xa + 1))
            yb = min(height, max(int(round(bottom[j, i] - my)), ya + 1))
            if xa >= width or ya >= height or xb <= 0 or yb <= 0:
                continue
            keep = ~background[ya:yb, xa:xb].reshape(-1)
            if keep.mean() <= 0.5:
                continue
            block = a[ya:yb, xa:xb].reshape(-1, 3)[keep]
            median = np.median(block, axis=0)
            colour[j, i] = median.round().astype(np.uint8)
            alpha[j, i] = 255
            purity[j, i] = float((np.sqrt(((block - median) ** 2).sum(axis=1)) < PURE).mean())
    return colour, alpha, purity


def _mean_step(lines: np.ndarray, lo: int, hi: int) -> float:
    """The mean distance between the grid lines that lie over [lo, hi] (between all lines when under three do)."""
    inside = lines[(lines >= lo - 1) & (lines <= hi + 1)]
    return float(np.diff(inside if len(inside) >= 3 else lines).mean())


def _walk_cells(a: np.ndarray, background: np.ndarray, profile_x: np.ndarray, profile_y: np.ndarray,
                period_x: float, period_y: float, box: tuple[int, int, int, int]) -> dict | None:
    x0, x1, y0, y1 = box
    lines_x, snapped_x = grid_lines(profile_x, period_x, x0, x1)
    lines_y, snapped_y = grid_lines(profile_y, period_y, y0, y1)
    if len(lines_x) < 3 or len(lines_y) < 3:
        return None
    lx, ly = np.asarray(lines_x), np.asarray(lines_y)
    left, right = np.tile(lx[:-1], (len(ly) - 1, 1)), np.tile(lx[1:], (len(ly) - 1, 1))
    top, bottom = np.tile(ly[:-1, None], (1, len(lx) - 1)), np.tile(ly[1:, None], (1, len(lx) - 1))
    colour, alpha, purity = _sample_cells(a, background, left, right, top, bottom)
    return {"colour": colour, "alpha": alpha, "purity": purity, "snapped": [snapped_x, snapped_y],
            "period": [period_x, period_y], "cell": [_mean_step(lx, x0, x1), _mean_step(ly, y0, y1)]}


def _lattice_cells(a: np.ndarray, background: np.ndarray, period_x: float, period_y: float,
                   box: tuple[int, int, int, int]) -> dict:
    """One regular lattice that bends: the number of cells is fixed by the lattice, only where it sits is
    measured locally (tiles of TILE cells), so a grid that drifts by a few px adds no cell."""
    height, width = a.shape[:2]
    x0, x1, y0, y1 = box
    change_x = np.abs(a[:, 1:] - a[:, :-1]).max(axis=2)
    change_x = np.where(change_x < FLOOR, 0.0, change_x)
    change_y = np.abs(a[1:] - a[:-1]).max(axis=2)
    change_y = np.where(change_y < FLOOR, 0.0, change_y)
    per_x, origin_x = _regular_lattice(_smooth(change_x[y0:y1].sum(axis=0)), x0, x1, period_x)
    per_y, origin_y = _regular_lattice(_smooth(change_y[:, x0:x1].sum(axis=1)), y0, y1, period_y)
    origin_x -= 2 * per_x
    origin_y -= 2 * per_y
    cols, rows = int((x1 - origin_x) / per_x) + 3, int((y1 - origin_y) / per_y) + 3
    step = max(1, TILE // 2)
    windows, bands = max(1, (cols - TILE) // step + 2), max(1, (rows - TILE) // step + 2)
    band_rows = [(int(round(origin_y + b * step * per_y)), int(round(origin_y + (b * step + TILE) * per_y))) for b in range(bands)]
    band_cols = [(int(round(origin_x + w * step * per_x)), int(round(origin_x + (w * step + TILE) * per_x))) for w in range(windows)]
    profiles_x = [_smooth(change_x[max(0, r0):max(1, min(height, r1))].sum(axis=0)) for r0, r1 in band_rows]
    profiles_y = [_smooth(change_y[:, max(0, c0):max(1, min(width, c1))].sum(axis=1)) for c0, c1 in band_cols]
    field_x = _shift_field(profiles_x, origin_x, per_x, bands, windows, step)
    field_y = _shift_field(profiles_y, origin_y, per_y, windows, bands, step).T
    cell_rows, cell_cols = np.arange(rows) + 0.5, np.arange(cols) + 0.5
    centres_b, centres_w = np.arange(bands) * step + TILE / 2.0, np.arange(windows) * step + TILE / 2.0

    def spread(field: np.ndarray) -> np.ndarray:
        along = np.array([np.interp(cell_cols, centres_w, field[b]) for b in range(field.shape[0])])
        return np.array([np.interp(cell_rows, centres_b, along[:, i]) for i in range(len(cell_cols))]).T

    left = np.tile(origin_x + np.arange(cols) * per_x, (rows, 1)) + spread(field_x)
    top = np.tile((origin_y + np.arange(rows) * per_y)[:, None], (1, cols)) + spread(field_y)
    colour, alpha, purity = _sample_cells(a, background, left, left + per_x, top, top + per_y)
    return {"colour": colour, "alpha": alpha, "purity": purity, "snapped": None, "period": [per_x, per_y],
            "cell": [per_x, per_y]}


def snap_picture(rgb: np.ndarray, *, colors: int = 32, gap: float = MERGE_GAP, key: Any = None,
                 key_distance: float = KEY_DISTANCE, period: float | None = None, method: str = "auto") -> dict:
    """A generated pixel-art picture on a flat key colour -> the 1x sprite.

    Two grids: ``walk`` (lines that follow the picture's own peaks one period at a time: right for big uneven
    cells) and ``lattice`` (one regular lattice that bends: right for cells under WALK_MIN px, where following
    every peak adds cells). ``method`` "auto" takes the lattice when the finest period is under WALK_MIN px,
    else the walk. (Purity, the share of a cell's pixels that have the cell's colour, is reported but is not
    the judge: a grid with too many lines is purer.)

    Returns {"sprite": RGBA uint8 cropped to the figure, "method", "period": [px, py] (what the search for the
    grid started from), "cell": [px, py] (the mean size of the grid's cells over the figure, as measured: an image
    model's blocks are often not square), "purity": {method: mean}, "snapped", "key": [r, g, b], "palette":
    uint8 (n, 3)}. Raises ValueError when the picture has no figure or no grid."""
    if method not in ("auto", "walk", "lattice"):
        raise ValueError("method must be auto, walk or lattice")
    a = np.asarray(rgb, np.float32)[..., :3]
    key = measure_key(a) if key is None else np.asarray(key, np.float64)
    background = np.sqrt(((a - key) ** 2).sum(axis=2)) < key_distance
    ys, xs = np.where(~background)
    if len(xs) < 16:
        raise ValueError("the picture has no figure on its key colour")
    box = (int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1)
    x0, x1, y0, y1 = box
    profile_x, profile_y = edge_profile(a, 1, y0, y1), edge_profile(a, 0, x0, x1)
    span_x, span_y = profile_x[max(0, x0 - 2):x1 + 2], profile_y[max(0, y0 - 2):y1 + 2]
    if period:
        fine_x = fine_y = coarse_x = coarse_y = float(period)
    else:
        fine_x, fine_y = find_period(span_x)[0], find_period(span_y)[0]
        coarse_x, coarse_y = find_period_coarse(span_x), find_period_coarse(span_y)
    if min(fine_x, fine_y) <= 0:
        raise ValueError("no pixel grid was found in the picture")
    if method == "auto":
        method = "lattice" if min(fine_x, fine_y) < WALK_MIN else "walk"
    if method == "walk":
        grid = _walk_cells(a, background, profile_x, profile_y, coarse_x or fine_x, coarse_y or fine_y, box)
    else:
        grid = _lattice_cells(a, background, fine_x, fine_y, box)
    if grid is None or not (grid["alpha"] > 0).any():
        raise ValueError("no pixel grid was found in the picture")
    chosen = method
    purity = {method: float(grid["purity"][grid["alpha"] > 0].mean())}
    opaque = grid["alpha"] > 0
    palette, index = merge_palette(grid["colour"][opaque], colors, gap)
    sprite = np.zeros(opaque.shape + (4,), dtype=np.uint8)
    sprite[opaque, :3] = palette[index]
    sprite[opaque, 3] = 255
    sprite = crop_to_figure(sprite)
    return {"sprite": sprite, "method": chosen, "period": [round(float(v), 3) for v in grid["period"]],
            "cell": [round(float(v), 3) for v in grid["cell"]],
            "purity": {name: round(v, 4) for name, v in purity.items()}, "snapped": grid["snapped"],
            "key": [int(round(v)) for v in key], "palette": palette_of(sprite)}


# --------------------------------------------------------------------------- small fixes of a sprite or frame

def edge_mask(opaque: np.ndarray) -> np.ndarray:
    """Opaque cells that touch the outside (4 neighbours) or the canvas border."""
    padded = np.pad(opaque, 1, constant_values=False)
    inner = padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2] & padded[1:-1, 2:]
    return opaque & ~inner


def outline_colour(rgba: np.ndarray) -> tuple[int, int, int] | None:
    """The colour most cells on the silhouette's edge have: the outline colour of a sprite with a closed outline."""
    edge = edge_mask(rgba[..., 3] > 0)
    if not edge.any():
        return None
    colours, counts = np.unique(rgba[edge][:, :3], axis=0, return_counts=True)
    return tuple(int(v) for v in colours[int(counts.argmax())])


def close_outline(rgba: np.ndarray, line: Sequence[int]) -> int:
    """Every coloured cell that touches the outside becomes the line colour (in place). Returns how many."""
    opaque = rgba[..., 3] > 0
    is_line = opaque & (rgba[..., 0] == line[0]) & (rgba[..., 1] == line[1]) & (rgba[..., 2] == line[2])
    hit = edge_mask(opaque) & ~is_line
    rgba[hit, :3] = np.asarray(line, np.uint8)
    return int(hit.sum())


def _key_share(cell: np.ndarray, neighbour: np.ndarray, key: np.ndarray) -> float | None:
    """The key colour's share in ``cell`` when it is a blend of ``neighbour`` and the key (a third or more of key),
    else None."""
    span = key - neighbour
    length = float((span ** 2).sum())
    if length < 1.0:
        return None
    t = float(((cell - neighbour) * span).sum()) / length
    residual = float(np.sqrt(((cell - (neighbour + t * span)) ** 2).sum()))
    return t if 0.3 <= t <= 1.05 and residual < 28.0 else None


def _mixed_with_key(cell: np.ndarray, neighbour: np.ndarray, key: np.ndarray) -> bool:
    """Is ``cell`` a blend of ``neighbour`` and the key colour (a third or more of key)?"""
    return _key_share(cell, neighbour, key) is not None


def drop_key_mixed(rgba: np.ndarray, key: Sequence[float], line: Sequence[int] | None = None) -> dict:
    """Edge cells that are a blend of the key colour and the figure (in place), again and again until the edge
    has none: a cell that would leave an inside cell bare takes its darkest opaque neighbour, the others go.
    With ``line`` (the outline's colour) an edge cell that is the line's colour blended with at most three
    quarters of key is an outline cell that the ground half covers: it takes the line's colour and stays."""
    key = np.asarray(key, np.float64)
    height, width = rgba.shape[:2]
    removed = darkened = 0
    ink = None if line is None else np.asarray(line, np.float64)[:3]
    for _ in range(4):
        opaque = rgba[..., 3] > 0
        edge = edge_mask(opaque)
        todo = []
        for y, x in zip(*np.nonzero(edge)):
            cell = rgba[y, x, :3].astype(np.float64)
            if ink is not None:
                share = _key_share(cell, ink, key)
                if share is not None and share <= 0.75:
                    rgba[y, x, :3] = np.round(ink)
                    darkened += 1
                    continue
            near = [(y + dy, x + dx) for dy, dx in NEAR4 if 0 <= y + dy < height and 0 <= x + dx < width and opaque[y + dy, x + dx]]
            inner = [q for q in near if not edge[q]]
            if any(_mixed_with_key(cell, rgba[q][:3].astype(np.float64), key) for q in (inner or near)):
                todo.append((y, x, near, inner))
        if not todo:
            break
        for y, x, near, inner in todo:
            if inner:
                darkest = min(near, key=lambda q: int(rgba[q][0]) * 3 + int(rgba[q][1]) * 6 + int(rgba[q][2]))
                rgba[y, x, :3] = rgba[darkest][:3]
                darkened += 1
            else:
                rgba[y, x] = 0
                removed += 1
    return {"removed": removed, "darkened": darkened}


def components(opaque: np.ndarray) -> list[list[tuple[int, int]]]:
    """The 8-connected groups of opaque cells."""
    height, width = opaque.shape
    seen = np.zeros_like(opaque)
    out = []
    for y0 in range(height):
        for x0 in range(width):
            if opaque[y0, x0] and not seen[y0, x0]:
                group, stack = [], [(y0, x0)]
                seen[y0, x0] = True
                while stack:
                    y, x = stack.pop()
                    group.append((y, x))
                    for yy in (y - 1, y, y + 1):
                        for xx in (x - 1, x, x + 1):
                            if 0 <= yy < height and 0 <= xx < width and opaque[yy, xx] and not seen[yy, xx]:
                                seen[yy, xx] = True
                                stack.append((yy, xx))
                out.append(group)
    return out


def remove_islands(rgba: np.ndarray, smallest: int = 8) -> int:
    """Groups of fewer than ``smallest`` cells that are not the figure go (in place). Returns the cells removed."""
    groups = components(rgba[..., 3] > 0)
    if not groups:
        return 0
    figure = max(groups, key=len)
    removed = 0
    for group in groups:
        if group is not figure and len(group) < smallest:
            for y, x in group:
                rgba[y, x] = 0
            removed += len(group)
    return removed


def fill_single_holes(rgba: np.ndarray) -> int:
    """A transparent cell with four opaque neighbours takes their most common colour (in place)."""
    opaque = rgba[..., 3] > 0
    height, width = opaque.shape
    filled = 0
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            if not opaque[y, x] and opaque[y - 1, x] and opaque[y + 1, x] and opaque[y, x - 1] and opaque[y, x + 1]:
                near = [tuple(int(v) for v in rgba[q]) for q in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1))]
                rgba[y, x] = max(set(near), key=near.count)
                filled += 1
    return filled


def crop_to_figure(rgba: np.ndarray) -> np.ndarray:
    rows, cols = np.where(rgba[..., 3] > 0)
    return rgba[rows.min():rows.max() + 1, cols.min():cols.max() + 1]


def enlarge(rgba: np.ndarray, scale: int, background: Sequence[int], margin: int = 0, size: int | None = None) -> np.ndarray:
    """A 1x sprite as an RGB picture on one exact grid: every sprite pixel a ``scale`` px square (nearest) on a
    flat background, with ``margin`` cells around it, or centred on a ``size`` px square at a whole cell."""
    scale = int(scale)
    height, width = rgba.shape[:2]
    if size is None:
        canvas = np.empty(((height + 2 * margin) * scale, (width + 2 * margin) * scale, 3), dtype=np.uint8)
        left = top = margin * scale
    else:
        if width * scale > size or height * scale > size:
            raise ValueError(f"a {width}x{height} sprite at {scale}x does not fit a {size} px square")
        canvas = np.empty((size, size, 3), dtype=np.uint8)
        left, top = (size - width * scale) // 2, (size - height * scale) // 2
        left, top = left - left % scale, top - top % scale
    canvas[...] = np.asarray(background, np.uint8)
    big = np.repeat(np.repeat(rgba, scale, axis=0), scale, axis=1)
    region = canvas[top:top + height * scale, left:left + width * scale]
    mask = big[..., 3] > 0
    region[mask] = big[mask][:, :3]
    return canvas


def feet_point(rgba: np.ndarray) -> tuple[int, int]:
    """The feet point of a standing sprite: the middle of its lowest three rows, on its last row."""
    opaque = rgba[..., 3] > 0
    rows = np.where(opaque.any(axis=1))[0]
    last = int(rows.max())
    cols = np.where(opaque[max(0, last - 2):last + 1].any(axis=0))[0]
    return int(round((int(cols.min()) + int(cols.max())) / 2.0)), last


# --------------------------------------------------------------------------- video frames on a known lattice

def _cells(frame: np.ndarray, x0: int, y0: int, width: int, height: int, scale: int) -> np.ndarray:
    quarter = max(1, scale // 4)
    inner = slice(quarter, scale - quarter)
    block = frame[y0:y0 + height * scale, x0:x0 + width * scale]
    return block.reshape(height, scale, width, scale, 3)[:, inner][:, :, :, inner]


def lattice_shift(frame: np.ndarray, x0: int, y0: int, width: int, height: int, scale: int, key: Any,
                  key_distance: float = VIDEO_KEY_DISTANCE) -> tuple[int, int]:
    """The shift (sx, sy) at which the cells of ``frame`` are flattest, within half a cell each way (more than
    -scale/2, at most scale/2 px): a lattice that lies 1 px before (x0, y0) is -1, not scale - 1, at which every
    cell would be read one cell further on. The inner part of a cell stays flat a few px off the true lattice, so
    among the shifts that are as flat as the flattest the one nearest to (0, 0) is taken. A caller that reads at
    (x0 + sx, y0 + sy) needs room for a shift backwards (pixel_motion.py pads the frame by one cell)."""
    frame = np.asarray(frame, np.float32)[..., :3]
    key = np.asarray(key, np.float32)
    found = []
    for sy in range(scale):
        for sx in range(scale):
            c = _cells(frame, x0 + sx, y0 + sy, width, height, scale)
            subject = (np.sqrt(((c - key) ** 2).sum(axis=-1)) > key_distance).mean(axis=(1, 3)) > 0.5
            if subject.any():
                found.append((float(c.std(axis=(1, 3)).max(axis=-1)[subject].mean()), sx, sy))
    if not found:
        return 0, 0
    low = min(f[0] for f in found)
    best = min((f for f in found if f[0] <= low + 0.5),
               key=lambda f: (max(min(f[1], scale - f[1]), min(f[2], scale - f[2])), f[0]))
    return tuple(int(s - scale if 2 * s > scale else s) for s in best[1:])


def _unmix(cells: Any, key: Any, palette: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    r = np.asarray(cells, np.float64).reshape(-1, 1, 3)
    p = np.asarray(palette, np.float64).reshape(1, -1, 3)
    d = np.asarray(key, np.float64).reshape(1, 1, 3) - p
    share = np.clip(((r - p) * d).sum(axis=2) / np.maximum((d ** 2).sum(axis=2), 1.0), 0.0, 1.0)
    left = np.sqrt(((r - (p + share[..., None] * d)) ** 2).sum(axis=2))
    best = (left + UNMIX_PENALTY * share).argmin(axis=1)
    rows = np.arange(len(best))
    return best, share[rows, best], left[rows, best]


def unmix_edge(cells: Any, key: Any, palette: Any) -> tuple[np.ndarray, np.ndarray]:
    """Edge cells of a video frame, explained as a colour of the palette mixed with the key colour. For each
    cell colour (n, 3): the palette index and the key's share (0 .. 1) of the cheapest explanation, where the
    cost is the RGB distance left over plus UNMIX_PENALTY for a full share of key, so a colour that is in the
    palette stays itself and a half-covered outline cell is the outline's colour again, not the palette's
    nearest purple."""
    best, share, _ = _unmix(cells, key, palette)
    return best, share


def drop_key_tinted(rgba: np.ndarray, key: Any, rare: int = 3) -> int:
    """What is left of the key colour on a sprite's edge (in place): a rare colour that only cells on the
    silhouette's edge have, and that is another colour of the sprite with a clear share of the key colour.
    Such a cell takes that other colour, or goes when it is more key than colour. Returns the cells changed.
    Without this the sprite's palette holds a key-tinted colour, and every frame of its clips locks its edge to
    it. (A design with colours of the key's own hue needs another key colour, here as everywhere.)"""
    opaque = rgba[..., 3] > 0
    if not opaque.any():
        return 0
    edge = edge_mask(opaque)[opaque]
    colours, inverse, counts = np.unique(rgba[opaque][:, :3], axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    on_edge = np.bincount(inverse, weights=edge.astype(np.float64), minlength=len(colours))
    suspect = (counts <= max(rare, int(0.002 * opaque.sum()))) & (on_edge == counts)
    if not suspect.any() or suspect.all():
        return 0
    others = colours[~suspect]
    best, share, left = _unmix(colours[suspect], key, others)
    ys, xs = np.nonzero(opaque)
    changed = 0
    for n, colour in enumerate(np.nonzero(suspect)[0]):
        if share[n] < UNMIX_CLEAR or left[n] >= UNMIX_LEFT:
            continue                                   # a colour of its own, not a tint
        cells = inverse == colour
        if share[n] > UNMIX_MOST:
            rgba[ys[cells], xs[cells]] = 0
        else:
            rgba[ys[cells], xs[cells], :3] = others[best[n]]
        changed += int(cells.sum())
    return changed


def snap_frame(frame: np.ndarray, x0: int, y0: int, width: int, height: int, scale: int, key: Any,
               palette: np.ndarray, key_distance: float = VIDEO_KEY_DISTANCE, key_mixed: bool = True) -> tuple[np.ndarray, dict]:
    """One video frame on the lattice whose corner is (x0, y0): ``width`` x ``height`` cells of ``scale`` px.
    Every cell gives the median of its inner part; a cell that is mostly key colour is transparent; every colour
    goes to the nearest colour of ``palette`` (OKLab). With ``key_mixed`` the cells on the silhouette's edge are
    unmixed first (unmix_edge): compression blends the key into the edge of a moving figure, and the palette
    alone would turn those cells into a fringe of its most key-like colours; an edge cell that is more key than
    figure goes, and the cells it bares are unmixed in turn. Returns the RGBA frame and {"cells", "notFlat",
    "keyMixed": edge cells that went or lost a clear share of key}."""
    frame = np.asarray(frame, np.float32)[..., :3]
    key = np.asarray(key, np.float32)
    c = _cells(frame, x0, y0, width, height, scale)
    subject = (np.sqrt(((c - key) ** 2).sum(axis=-1)) > key_distance).mean(axis=(1, 3)) > 0.5
    spread = c.std(axis=(1, 3)).max(axis=-1)
    median = np.median(c.transpose(0, 2, 1, 3, 4).reshape(height, width, -1, 3), axis=2)
    report = {"cells": int(subject.sum()), "notFlat": int((spread[subject] > FLAT_SPREAD).sum()), "keyMixed": 0}
    pal = np.asarray(palette, np.uint8).reshape(-1, 3)
    out = np.zeros((height, width, 4), dtype=np.uint8)
    done = np.zeros((height, width), dtype=bool)
    if key_mixed:
        for _ in range(UNMIX_PASSES):
            ys, xs = np.nonzero(edge_mask(subject) & ~done)
            if not len(ys):
                break
            best, share = unmix_edge(median[ys, xs], key, pal)
            keep = share <= UNMIX_MOST
            out[ys[keep], xs[keep], :3] = pal[best[keep]]
            done[ys[keep], xs[keep]] = True
            subject[ys[~keep], xs[~keep]] = False
            report["keyMixed"] += int((~keep).sum()) + int((share[keep] > UNMIX_CLEAR).sum())
            if keep.all():
                break
    rest = subject & ~done
    if rest.any():
        d = ((to_oklab(median[rest])[:, None, :] - to_oklab(pal)[None, :, :]) ** 2).sum(axis=2)
        out[rest, :3] = pal[d.argmin(axis=1)]
    out[subject, 3] = 255
    return out, report


def frame_distance(a: np.ndarray, b: np.ndarray) -> int:
    """How many cells of two frames differ."""
    return int((a != b).any(axis=2).sum())


def moving_span(frames: Sequence[np.ndarray], share: float = 0.25) -> tuple[int, int]:
    """The still frame before the motion and the still frame after it: between them the figure really moves from
    one frame to the next. (Not "differs from the first frame": a figure that comes to rest one cell beside
    where it started would count as moving to the clip's end.) A step counts when it changes more than ``share``
    of what the clip's third largest step changes, so that one or two glitches do not set the measure."""
    steps = [frame_distance(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
    top = sorted(steps)[-3] if len(steps) >= 3 else max(steps, default=0)
    busy = [i for i, step in enumerate(steps) if step > share * top] if top else []
    return (busy[0], busy[-1] + 1) if busy else (0, len(frames) - 1)


def find_cycle(frames: Sequence[np.ndarray], shortest: int = 10, longest: int = 46) -> dict:
    """The loop of a clip, from its frames: the period is the lag at which frames are most alike on average, the
    start is the frame that is most like the frame one period later. Returns {"period", "start", "closure",
    "scores"} (frame indices are 0-based); "period" is 0 when the clip is too short."""
    first, last = moving_span(frames)
    scores = {}
    for lag in range(shortest, longest + 1):
        pairs = [frame_distance(frames[i], frames[i + lag]) for i in range(first, last - lag)]
        if len(pairs) >= min(lag, 12):
            scores[lag] = float(np.mean(pairs))
    if not scores:
        return {"period": 0, "start": first, "closure": 0, "scores": {}}
    period = min(scores, key=scores.get)
    starts = [(frame_distance(frames[i], frames[i + period]), i) for i in range(first + 2, last - period - 1)]
    closure, start = min(starts) if starts else (frame_distance(frames[first], frames[first + period]), first)
    return {"period": int(period), "start": int(start), "closure": int(closure),
            "scores": {int(k): round(v, 1) for k, v in sorted(scores.items())}}


def pick_cycle(start: int, period: int, count: int) -> list[int]:
    """``count`` frames of one cycle, evenly spaced."""
    return [start + int(round(j * period / count)) for j in range(count)]


def pick_motion(frames: Sequence[np.ndarray], count: int, first: int | None = None, last: int | None = None) -> list[int]:
    """``count`` frames of one action between ``first`` and ``last`` (default: the moving span, which starts and
    ends on a still frame), at equal steps of how much has moved: a fast part gets more frames than a hold."""
    lo, hi = moving_span(frames)
    first = lo if first is None else first
    last = hi if last is None else last
    if last <= first or count < 2:
        return [first]
    steps = np.array([frame_distance(frames[i], frames[i + 1]) for i in range(first, last)], dtype=np.float64) + 1.0
    travelled = np.concatenate([[0.0], np.cumsum(steps)])
    targets = np.linspace(0.0, travelled[-1], count)
    picked = [first + int(np.abs(travelled - t).argmin()) for t in targets]
    out = []
    for i in picked:                                  # no frame twice: move a repeat to the next free frame
        while i in out and i < last:
            i += 1
        if i not in out:
            out.append(i)
    return sorted(out)


def head_lock(frame: np.ndarray, sprite: np.ndarray, head: np.ndarray, at: Sequence[int], *,
              line: Sequence[int] | None = None, share: float = LOCK_SHARE, reach: int = 10) -> dict:
    """Put the sprite's own head where the frame has its head (in place).

    ``head`` is a bool mask on the sprite; ``at`` is the sprite's (x, y) on the frame's canvas. The whole-pixel
    offset within ``reach`` at which most head cells are already equal is found; when at least ``share`` of them
    are, the frame's own head around that place goes (cells in the head's colours, then line cells left with no
    coloured cell beside them; a blade raised over the head stays) and the sprite's head is pasted.
    Returns {"offset": [dx, dy], "share", "locked"}."""
    hy, hx = np.nonzero(head)
    height, width = frame.shape[:2]
    best = (-1, 0, 0)
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            ys, xs = hy + at[1] + dy, hx + at[0] + dx
            if ys.min() < 0 or xs.min() < 0 or ys.max() >= height or xs.max() >= width:
                continue
            same = int((frame[ys, xs] == sprite[hy, hx]).all(axis=1).sum())
            if same > best[0]:
                best = (same, dx, dy)
    same, dx, dy = best
    info = {"offset": [dx, dy], "share": round(max(same, 0) / max(1, len(hy)), 3), "locked": False}
    if same < 0 or same / len(hy) < share:
        return info
    ys, xs = hy + at[1] + dy, hx + at[0] + dx
    lines = {tuple(int(v) for v in line)} if line is not None else set()
    colours = {tuple(int(v) for v in c) for c in sprite[hy, hx][:, :3]} - lines
    under = np.zeros((height, width), dtype=bool)
    under[ys, xs] = True
    bottom, left, right = int(ys.max()) - 3, max(0, int(xs.min()) - 1), min(width, int(xs.max()) + 2)
    for y in range(0, max(0, bottom)):
        for x in range(left, right):
            if frame[y, x, 3] and not under[y, x] and tuple(int(v) for v in frame[y, x, :3]) in colours:
                frame[y, x] = 0
    frame[ys, xs] = sprite[hy, hx]
    for y in range(0, max(0, bottom)):
        for x in range(left, right):
            if frame[y, x, 3] and not under[y, x] and tuple(int(v) for v in frame[y, x, :3]) in lines:
                near = frame[max(0, y - 1):y + 2, max(0, x - 1):x + 2].reshape(-1, 4)
                if not any(c[3] and tuple(int(v) for v in c[:3]) not in lines for c in near):
                    frame[y, x] = 0
    frame[ys, xs] = sprite[hy, hx]
    info["locked"] = True
    return info


# --------------------------------------------------------------------------- a master and what is made from it

LINE_DARK = 48.0          # a cell at most this light, or 24 above the line colour, is a cell of the dark outline
EQUAL = 60.0              # channel differences (summed) under which a picture's cell is the cell of the sprite it shows
NEAR_KEY = 1.8            # an edge cell nearer to the key colour than this many KEY_DISTANCEs is key left on the edge
LEG_SHARE = 0.16          # a column this tall (a share of the sprite's height) above the ground is a leg, not a blade's tip
FEW_COLOURS = 16          # a palette of at most this many colours ...
FEW_HUE = 3.0             # ... counts hue this much more than lightness when it is brought down (fewer_colours)
FLAT_RANGE = 36.0         # the range of a cell's colours (max - min of every channel, summed) under which the cell is flat
SPILL = 20.0              # an edge cell whose key channels lead the others by more than this carries key (the package's gate)
SPILL_BALANCE = 0.5       # ... for a key of two channels, only where min/max of those two is at least this
CLEAR = 0.2               # OKLab distance from which two colours are clearly apart (what a doubled row shows)
_LIGHT = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def lightness(rgb: Any) -> np.ndarray:
    """The luma (0..255) of colours or of a sprite's cells."""
    return np.asarray(rgb, np.float32)[..., :3] @ _LIGHT


def fewer_colours(colours: Any, count: int, rounds: int = 30, hue: float = 1.0, common: bool = False) -> np.ndarray:
    """Colours (n, 3) brought down to at most ``count``: k-means in OKLab from a farthest-point start, so every
    hue keeps a centre and the result is the same on every run; a centre takes the median RGB of its colours.
    (Merging the closest pair again and again runs dark navy, brown and black together: that is ``merge_palette``,
    which is right for the few flat tones of a small sprite and wrong for a softly shaded one.) ``hue`` weighs
    the two colour axes against lightness: above 1, the steps of a grey ramp merge before a pale skin tone is
    lost among them, which a palette of a dozen colours needs. With ``common`` a centre takes the most common
    colour of its group instead of the median: a few flat colours stay colours the picture had (the median of
    each channel on its own can be a colour no cell has)."""
    colours = np.asarray(colours, np.uint8).reshape(-1, 3)
    if count < 1:
        raise ValueError("count is at least 1")
    lab = np.asarray(to_oklab(colours.astype(np.float32)), np.float64) * np.array([1.0, hue, hue])
    uniq = np.unique(np.round(lab, 4), axis=0)
    if len(uniq) <= count:
        return colours.copy()
    centres = [uniq[0]]
    dist = ((uniq - centres[0]) ** 2).sum(axis=1)
    for _ in range(count - 1):
        centres.append(uniq[int(dist.argmax())])
        dist = np.minimum(dist, ((uniq - centres[-1]) ** 2).sum(axis=1))
    centres = np.array(centres, np.float64)
    near = None
    for _ in range(rounds):
        now = ((lab[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
        if near is not None and (now == near).all():
            break
        near = now
        for k in range(count):
            if (near == k).any():
                centres[k] = lab[near == k].mean(axis=0)
    out = colours.copy()
    for k in range(count):
        if (near == k).any():
            if common:
                members, counts = np.unique(colours[near == k], axis=0, return_counts=True)
                out[near == k] = members[int(counts.argmax())]
            else:
                out[near == k] = np.round(np.median(colours[near == k], axis=0))
    return out


def _shows(cells: np.ndarray) -> np.ndarray:
    """Per cell, what one more row of it would show: the growth of the run of equal cells it lies in (1/n for a run
    of n rows) times how clearly that run stands out from what lies above it and below it (0..1: the smaller of
    the two OKLab distances over CLEAR; the outside counts as 1). A one-row line on another colour is 1 a cell;
    a cell of a long run, or of a soft step of shading, is nearly 0."""
    height, width = cells.shape[:2]
    lab = to_oklab(cells[..., :3])
    out = np.zeros((height, width), dtype=np.float64)

    def apart(y: int, x: int, other: int) -> float:
        if other < 0 or other >= height or cells[other, x, 3] == 0:
            return 1.0
        return min(1.0, float(np.sqrt(((lab[y, x] - lab[other, x]) ** 2).sum())) / CLEAR)

    for x in range(width):
        y = 0
        while y < height:
            end = y + 1
            while end < height and (cells[end, x] == cells[y, x]).all():
                end += 1
            if cells[y, x, 3] > 0:
                out[y:end, x] = min(apart(y, x, y - 1), apart(end - 1, x, end)) / (end - y)
            y = end
    return out


def _doubled(cells: np.ndarray, row: int, line: Sequence[int] | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Row ``row`` as the two rows it becomes when it is doubled, and per column which run grows by it (0: the
    cell's own, 1: the run below, -1: the run above). Both rows are the row itself, except where that would make
    the outline two cells thick: a cell of the ``line`` colour that touches the outside above, and whose copy
    underneath would touch it nowhere, gives that copy the colour of the cell below; and the same the other way
    round. So the figure's inside grows, not its outline."""
    height, width = cells.shape[:2]
    upper, lower = cells[row].copy(), cells[row].copy()
    grown = np.zeros(width, dtype=np.int64)
    if line is None:
        return upper, lower, grown
    ink = np.asarray(line, np.uint8)
    empty = np.zeros((width, 4), dtype=np.uint8)
    here = cells[row]
    above = cells[row - 1] if row > 0 else empty
    below = cells[row + 1] if row + 1 < height else empty
    opaque = here[:, 3] > 0
    padded = np.pad(opaque, 1, constant_values=False)
    inked = opaque & (here[:, :3] == ink).all(axis=1) & padded[:-2] & padded[2:]        # ... with a cell on both sides

    def inside(cells_: np.ndarray) -> np.ndarray:
        return (cells_[:, 3] > 0) & ~(cells_[:, :3] == ink).all(axis=1)

    take_below = inked & (above[:, 3] == 0) & inside(below)
    take_above = inked & (below[:, 3] == 0) & inside(above)
    lower[take_below] = below[take_below]
    upper[take_above] = above[take_above]
    grown[take_below], grown[take_above] = 1, -1
    return upper, lower, grown


def doubling_cost(rgba: np.ndarray, line: Sequence[int] | None = None) -> np.ndarray:
    """Per row, how much doubling that row would show: the sum over its cells of what one more row of the run that
    grows there shows (``_shows``; with ``line``, the outline does not grow, see ``_doubled``). A row across an
    eye, a belt or a thin blade is dear, a row across flat colour and upright outline is cheap."""
    cells = np.asarray(rgba, np.uint8)
    height, width = cells.shape[:2]
    shows = _shows(cells)
    columns = np.arange(width)
    return np.array([shows[np.clip(row + _doubled(cells, row, line)[2], 0, height - 1), columns].sum()
                     for row in range(height)], dtype=np.float64)


def add_rows(rgba: np.ndarray, count: int, line: Sequence[int] | None = None) -> tuple[np.ndarray, list[int]]:
    """A sprite made ``count`` rows taller without redrawing it: the figure is cut into ``count`` stretches of equal
    height and in each the row that shows least when doubled (``doubling_cost``) is doubled, so the figure grows
    evenly, its one-row lines stay one row and, with ``line`` (the colour of its outline), its outline stays one
    cell thick. Returns the sprite and the rows of the input that were doubled."""
    cells = np.asarray(rgba, np.uint8)
    height = cells.shape[0]
    count = int(min(max(0, count), height))
    if count == 0:
        return cells.copy(), []
    cost = doubling_cost(cells, line)
    edges = np.linspace(0, height, count + 1)
    picks = []
    for k in range(count):
        lo = int(round(edges[k]))
        hi = max(lo + 1, int(round(edges[k + 1])))
        part = cost[lo:hi]
        cheapest = np.where(part <= part.min() + 1e-9)[0]
        picks.append(lo + int(cheapest[len(cheapest) // 2]))
    rows = []
    for row in range(height):
        if row in picks:
            rows.extend(_doubled(cells, row, line)[:2])
        else:
            rows.append(cells[row])
    return np.ascontiguousarray(np.stack(rows)), picks


def add_columns(rgba: np.ndarray, count: int, line: Sequence[int] | None = None) -> tuple[np.ndarray, list[int]]:
    """``add_rows`` for the other axis: the sprite made ``count`` columns wider."""
    wider, picks = add_rows(np.swapaxes(np.asarray(rgba, np.uint8), 0, 1), count, line)
    return np.ascontiguousarray(np.swapaxes(wider, 0, 1)), picks


def feet_runs(rgba: np.ndarray) -> tuple[list[tuple[int, int]], int]:
    """The runs of cells (first column, last column) that are a standing figure's feet, and the feet's row. A
    weapon or a staff held to one side can reach the ground too. The feet's row is the lowest row that is at least
    six cells wide; of the runs of cells in it and in the row above, a foot is a run that carries a column at
    least LEG_SHARE of the sprite's height tall (a blade's tip is low) and is at least three cells wide and at
    least 0.6 of the widest such run (a staff is thin; the far leg of a turned figure is a little narrower than
    the near one). When no run is a foot by that rule, the widest run is taken."""
    opaque = rgba[..., 3] > 0
    wide = np.where(opaque.sum(axis=1) >= 6)[0]
    if not len(wide):
        x, y = feet_point(rgba)
        return [(int(x), int(x))], int(y)
    base = int(wide.max())
    need = max(8, int(round(LEG_SHARE * opaque.shape[0])))
    band = opaque[max(0, base - 1):base + 1].any(axis=0)

    def tall(x: int) -> int:
        y = base if opaque[base, x] else base - 1
        count = 0
        while y >= 0 and opaque[y, x]:
            count += 1
            y -= 1
        return count

    runs, start = [], None
    for x, on in enumerate(list(band) + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            runs.append((start, x - 1))
            start = None
    legs = [r for r in runs if max(tall(x) for x in range(r[0], r[1] + 1)) >= need]
    widest = max((r[1] - r[0] + 1 for r in legs), default=0)
    feet = [r for r in legs if r[1] - r[0] + 1 >= max(3, math.ceil(0.6 * widest))]
    if not feet:
        feet = [max(runs, key=lambda r: r[1] - r[0])]
    return feet, base


def boots(rgba: np.ndarray) -> tuple[float, int]:
    """The middle of a standing figure's feet (a column, possibly a half) and their lowest row: the point a figure
    stands on, which a weapon held to one side does not move (see ``feet_runs``)."""
    runs, base = feet_runs(rgba)
    return (runs[0][0] + runs[-1][1]) / 2.0, base


def drop_key_near(rgba: np.ndarray, key: Any, times: float = NEAR_KEY, passes: int = 3) -> int:
    """Edge cells that are still nearly the key colour go (in place), ring after ring: a cell on the silhouette's
    edge can be more key than figure and yet not have counted as background. Returns how many went."""
    key = np.asarray(key, np.float32)
    dropped = 0
    for _ in range(passes):
        opaque = rgba[..., 3] > 0
        near = opaque & edge_mask(opaque) & (np.sqrt(((rgba[..., :3].astype(np.float32) - key) ** 2).sum(axis=2))
                                              < times * KEY_DISTANCE)
        if not near.any():
            break
        rgba[near, 3] = 0
        dropped += int(near.sum())
    return dropped


def key_lead(rgb: Any, key: Any) -> np.ndarray:
    """How far a colour's key channels lead its other channels: min(the key's channels) - max(the others), the
    measure of the package's residue gate (forge_matte.matte_qa). For a key of two channels (magenta) only where
    those two are balanced: a crimson leads green by as much and is no spill."""
    key = np.asarray(key, np.float32)[:3]
    high = key > 127.5
    colours = np.asarray(rgb, np.float32)[..., :3]
    if high.all() or not high.any():
        return np.zeros(colours.shape[:-1], dtype=np.float32)
    lead = colours[..., high].min(axis=-1) - colours[..., ~high].max(axis=-1)
    if int(high.sum()) == 2:
        two = colours[..., high]
        lead = np.where(two.min(axis=-1) / np.maximum(two.max(axis=-1), 1.0) >= SPILL_BALANCE, lead, 0.0)
    return lead


def drop_key_spill(rgba: np.ndarray, key: Any, passes: int = 3) -> dict:
    """Edge cells that carry key colour by the package gate's measure (``key_lead`` above SPILL), in place. Each is
    explained as a colour the figure has elsewhere plus a share of key (``unmix_edge``): a cell that is more key
    than figure goes, the others take that colour. A look with a dark outline hides a tinted edge; a look
    without one shows it, and one percent of such cells fails the package."""
    removed = recoloured = 0
    for _ in range(passes):
        opaque = rgba[..., 3] > 0
        spill = opaque & edge_mask(opaque) & (key_lead(rgba, key) > SPILL)
        if not spill.any():
            break
        palette = np.unique(rgba[opaque & ~spill][:, :3], axis=0) if (opaque & ~spill).any() else np.zeros((0, 3), np.uint8)
        clean = palette[key_lead(palette, key) <= SPILL] if len(palette) else palette
        cells = np.argwhere(spill)
        if not len(clean):
            rgba[spill, 3] = 0
            removed += len(cells)
            break
        best, share = unmix_edge(rgba[spill][:, :3], key, clean)
        gone = share > UNMIX_MOST
        rgba[cells[gone, 0], cells[gone, 1], 3] = 0
        rgba[cells[~gone, 0], cells[~gone, 1], :3] = clean[best[~gone]]
        removed += int(gone.sum())
        recoloured += int((~gone).sum())
    return {"removed": removed, "recoloured": recoloured}


def drop_key_holes(rgba: np.ndarray, key: Any, times: float = NEAR_KEY, most: int = 3) -> int:
    """Cells inside the figure that are still nearly the key colour go (in place): a gap in the figure a cell or two
    wide (the inside of a ring, the space between a crystal and its setting) is read as key mixed with its
    neighbours and is no edge cell. Only groups of at most ``most`` such cells go: a larger patch near the key
    colour is the figure's own colour, and the key was badly chosen. Returns how many went."""
    key = np.asarray(key, np.float32)
    near = (rgba[..., 3] > 0) & (np.sqrt(((rgba[..., :3].astype(np.float32) - key) ** 2).sum(axis=2)) < times * KEY_DISTANCE)
    dropped = 0
    for group in components(near):
        if len(group) <= most:
            for j, i in group:
                rgba[j, i, 3] = 0
            dropped += len(group)
    return dropped


def ink_key_mixed(rgba: np.ndarray, key: Any, line: Sequence[int], most: int = 3) -> int:
    """Cells anywhere in the figure that are the outline's colour blended with an eighth to three quarters of key
    take the outline's colour (in place): a narrow gap between two parts (hair and a staff beside it) is painted
    as such a blend and is no edge cell. Left in, one such cell is a colour of the sprite's palette, and every
    edge cell of a video frame that compression tints with key is then read as that colour. Only groups of at
    most ``most`` such cells: a larger patch on that line of colours (a purple cloak on a magenta ground) is the
    figure's own colour. Returns how many were inked."""
    key = np.asarray(key, np.float64)
    ink = np.asarray(line, np.float64)[:3]
    span = key - ink
    length = float((span ** 2).sum())
    if length < 1.0:
        return 0
    cells = rgba[..., :3].astype(np.float64)
    share = ((cells - ink) * span).sum(axis=2) / length
    left = np.sqrt(((cells - (ink + share[..., None] * span)) ** 2).sum(axis=2))
    blend = (rgba[..., 3] > 0) & (share >= 0.125) & (share <= 0.75) & (left < 28.0)
    inked = 0
    for group in components(blend):
        if len(group) <= most:
            for j, i in group:
                rgba[j, i, :3] = np.round(ink)
            inked += len(group)
    return inked


def _sides(opaque: np.ndarray) -> np.ndarray:
    """How many of a cell's four neighbours are opaque."""
    p = np.pad(opaque, 1, constant_values=False)
    return p[:-2, 1:-1].astype(np.int32) + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]


def _beside(mask: np.ndarray) -> np.ndarray:
    """The cells that have a cell of ``mask`` as one of their four neighbours."""
    out = np.zeros_like(mask)
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


def rule_outline(rgba: np.ndarray, line: Sequence[int] | None = None, only: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """A sprite's outline made one closed line by rule. A dark cell that hangs on the figure by one side or none
    is a speck and goes; where a lit cell lies bare on the edge, the line is drawn OUTSIDE it (the figure loses
    nothing); every dark cell on the edge takes the one line colour (``line``, else the sprite's most common edge
    colour). ``only`` (a mask of the sprite's cells) limits the rule to those cells and what grows from them: the
    rest of the sprite is left exactly as it is. Returns the new sprite, cropped to the figure, and {"line",
    "specks", "bareCells", "lineCellsAdded", "closed", "offset": where the input's cell (0, 0) lies in the result}."""
    out = np.pad(np.asarray(rgba, np.uint8), ((1, 1), (1, 1), (0, 0)))
    where = np.ones(out.shape[:2], dtype=bool) if only is None else np.pad(np.asarray(only, bool), 1, constant_values=False)
    colour = line if line is not None else outline_colour(rgba)
    if colour is None:
        raise ValueError("the sprite is empty")
    line_rgb = np.asarray(colour, np.float32)[:3]
    limit = max(LINE_DARK, float(lightness(line_rgb)) + 24.0)
    opaque = out[..., 3] > 0
    specks = 0
    while True:
        hang = opaque & where & (lightness(out) <= limit) & (_sides(opaque) <= 1)
        if not hang.any():
            break
        out[hang, 3] = 0
        specks += int(hang.sum())
        opaque = out[..., 3] > 0
    bare = edge_mask(opaque) & where & (lightness(out) > limit)
    grow = _beside(bare) & ~opaque
    out[grow, :3] = np.round(line_rgb)
    out[grow, 3] = 255
    opaque = out[..., 3] > 0
    edge = edge_mask(opaque)
    dark = opaque & (lightness(out) <= limit)
    out[edge & dark & (where | grow), :3] = np.round(line_rgb)
    closed = not (edge & ~dark & (where | grow)).any()
    ys, xs = np.where(opaque)
    if not len(xs):
        raise ValueError("the sprite is empty")
    x0, y0 = int(xs.min()), int(ys.min())
    sprite = np.ascontiguousarray(out[y0:int(ys.max()) + 1, x0:int(xs.max()) + 1])
    return sprite, {"line": [int(round(float(v))) for v in line_rgb], "specks": specks, "bareCells": int(bare.sum()),
                    "lineCellsAdded": int(grow.sum()), "closed": bool(closed), "offset": [1 - x0, 1 - y0]}


def _middles(rgb: np.ndarray, cell: float, ox: float, oy: float, cols: int, rows: int) -> np.ndarray:
    """The colour at the middle of every cell of a lattice (the mean of nine points there): fast, for a search."""
    xs = np.clip((ox + (np.arange(cols) + 0.5) * cell).astype(np.int64), 2, rgb.shape[1] - 3)
    ys = np.clip((oy + (np.arange(rows) + 0.5) * cell).astype(np.int64), 2, rgb.shape[0] - 3)
    out = np.zeros((rows, cols, 3), dtype=np.float32)
    for dy in (-2, 0, 2):
        for dx in (-2, 0, 2):
            out += rgb[np.ix_(ys + dy, xs + dx)]
    return out / 9.0


def fit_lattice(rgb: np.ndarray, ruler: np.ndarray, cell: float, at: Sequence[int] = (0, 0), *, spread: float = 1.0,
                reach: float = 36.0) -> dict:
    """Where a known 1x sprite (``ruler``) lies in a picture that shows it enlarged. The picture is expected to
    have cells of about ``cell`` px, with the ruler's cell (0, 0) at cell ``at`` of a lattice that starts at the
    picture's corner (the picture an image model returns for "reproduce the attached image" is such a picture,
    a little moved and a little resized). First the place is searched (within ``reach`` px) at which most of the
    ruler's cells come out equal at their middles. Then the cell size (within ``spread`` px) and the place are
    searched again by a strict count: a cell is equal when every one of nine points spread over its inner half is
    equal, so a lattice that drifts across the sprite loses cells. A cell is flat, so several sizes and places
    are as good as the best one: the middle of each such stretch is taken, which is where the cells really lie.
    Returns {"cell", "offset": [x, y] (px of the lattice's corner), "equal", "cells", "share"} by the strict count."""
    a = np.asarray(rgb, np.float32)[..., :3]
    ruler = np.asarray(ruler, np.uint8)
    rows, cols = ruler.shape[:2]
    on = ruler[..., 3] > 0
    want = ruler[..., :3].astype(np.float32)
    total = int(on.sum())
    high, wide = a.shape[0] - 1, a.shape[1] - 1

    def loose(c: float, ox: float, oy: float) -> int:
        got = _middles(a, c, ox + at[0] * c, oy + at[1] * c, cols, rows)
        return int(((np.abs(got - want).sum(axis=2) < EQUAL) & on).sum())

    def strict(c: float, ox: float, oy: float) -> int:
        xs = ox + (at[0] + np.arange(cols) + 0.5) * c
        ys = oy + (at[1] + np.arange(rows) + 0.5) * c
        worst = np.zeros((rows, cols), dtype=np.float32)
        for dy in (-0.25 * c, 0.0, 0.25 * c):
            for dx in (-0.25 * c, 0.0, 0.25 * c):
                got = a[np.ix_(np.clip(ys + dy, 0, high).astype(np.int64), np.clip(xs + dx, 0, wide).astype(np.int64))]
                worst = np.maximum(worst, np.abs(got - want).sum(axis=2))
        return int(((worst < EQUAL) & on).sum())

    step = max(1.0, round(cell / 5.0))
    best = (-1, float(cell), 0.0, 0.0)
    for c in np.arange(cell - spread, cell + spread + 1e-6, 0.1):
        for oy in np.arange(-reach, reach + 1e-6, step):
            for ox in np.arange(-reach, reach + 1e-6, step):
                n = loose(float(c), float(ox), float(oy))
                if n > best[0]:
                    best = (n, float(c), float(ox), float(oy))
    _, c0, x0, y0 = best
    mid_x, mid_y = at[0] + cols / 2.0, at[1] + rows / 2.0          # the ruler's middle in cells: it stays put when the
    px, py = x0 + mid_x * c0, y0 + mid_y * c0                      # cell size changes
    slack = max(1, int(round(0.005 * total)))
    sizes = [float(c) for c in np.arange(cell - spread, cell + spread + 1e-6, 0.05)]
    found = []
    for c in sizes:
        found.append(max((strict(c, px + dx - mid_x * c, py + dy - mid_y * c), -abs(dx) - abs(dy), float(dx), float(dy))
                         for dy in np.arange(-step, step + 1e-6, 1.0) for dx in np.arange(-step, step + 1e-6, 1.0)))
    top = max(n for n, _, _, _ in found)
    peak = max(range(len(sizes)), key=lambda i: (found[i][0], -abs(sizes[i] - cell)))
    lo = hi = peak
    while lo > 0 and found[lo - 1][0] >= top - slack:
        lo -= 1
    while hi < len(sizes) - 1 and found[hi + 1][0] >= top - slack:
        hi += 1
    c1 = (sizes[lo] + sizes[hi]) / 2.0
    near = min(range(len(sizes)), key=lambda i: abs(sizes[i] - c1))
    x1, y1 = px + found[near][2] - mid_x * c1, py + found[near][3] - mid_y * c1
    count = strict(c1, x1, y1)

    def middle(value) -> float:
        """The middle of the stretch of whole px around 0 at which ``value`` stays within ``slack`` of the count."""
        lo = hi = 0
        while lo > -cell and value(lo - 1) >= count - slack:
            lo -= 1
        while hi < cell and value(hi + 1) >= count - slack:
            hi += 1
        return (lo + hi) / 2.0

    x1 += middle(lambda d: strict(c1, x1 + d, y1))
    y1 += middle(lambda d: strict(c1, x1, y1 + d))
    count = strict(c1, x1, y1)
    return {"cell": round(c1, 3), "offset": [round(x1, 2), round(y1, 2)], "equal": int(count), "cells": total,
            "share": round(count / float(max(1, total)), 4)}


def _flat_cells(a: np.ndarray, cell: float, ox: float, oy: float, cols: int, rows: int, points: int = 5,
                inner: float = 0.25) -> np.ndarray:
    """Which cells of a lattice are flat: the colours at 25 points spread over a cell's inner half lie within
    FLAT_RANGE of each other (the range of every channel, summed)."""
    low = high = None
    for fy in np.linspace(inner, 1.0 - inner, points):
        ys = np.clip(oy + (np.arange(rows) + fy) * cell, 0, a.shape[0] - 1).astype(np.int64)
        for fx in np.linspace(inner, 1.0 - inner, points):
            xs = np.clip(ox + (np.arange(cols) + fx) * cell, 0, a.shape[1] - 1).astype(np.int64)
            got = a[np.ix_(ys, xs)]
            low = got if low is None else np.minimum(low, got)
            high = got if high is None else np.maximum(high, got)
    return (high - low).sum(axis=2) < FLAT_RANGE


def lattice_phase(rgb: np.ndarray, cell: float, offset: Sequence[float], box: Sequence[int]) -> dict:
    """The shift of a lattice (whole px, within half a cell each way) at which the cells of ``box`` (first column,
    first row, end column, end row, in cells of the lattice at ``offset``) are flattest. A second figure that an
    image model draws beside the first has cells of the same size, but its cells need not lie on the first
    figure's lattice: half a cell off, every cell read there would be a mix of two. Of shifts that are as good the
    middle one is taken. Returns {"shift": [dx, dy], "flat", "flatUnshifted", "cells"}."""
    a = np.asarray(rgb, np.float32)[..., :3]
    col0, row0, col1, row1 = (int(v) for v in box)
    cols, rows = col1 - col0, row1 - row0
    if cols < 1 or rows < 1:
        raise ValueError("the box holds no cell")

    def flat(dx: float, dy: float) -> int:
        return int(_flat_cells(a, cell, offset[0] + dx + col0 * cell, offset[1] + dy + row0 * cell, cols, rows).sum())

    half = int(cell // 2)
    score, _, dx, dy = max((flat(x, y), -abs(x) - abs(y), x, y) for y in range(-half, half + 1) for x in range(-half, half + 1))
    slack = max(1, int(round(0.005 * score)))

    def middle(value) -> float:
        lo = hi = 0
        while lo > -half and value(lo - 1) >= score - slack:
            lo -= 1
        while hi < half and value(hi + 1) >= score - slack:
            hi += 1
        return (lo + hi) / 2.0

    dx = dx + middle(lambda d: flat(dx + d, dy))
    dy = dy + middle(lambda d: flat(dx, dy + d))
    return {"shift": [float(dx), float(dy)], "flat": flat(dx, dy), "flatUnshifted": flat(0, 0), "cells": cols * rows}


def read_lattice(rgb: np.ndarray, cell: float, offset: Sequence[float], cols: int, rows: int, inner: float = 0.2) -> np.ndarray:
    """Every cell of a lattice as one colour: the median of its inner part (float32, rows x cols x 3). A cell that
    lies outside the picture is black."""
    a = np.asarray(rgb, np.float32)[..., :3]
    out = np.zeros((rows, cols, 3), dtype=np.float32)
    m = inner * cell
    for j in range(rows):
        y0, y1 = int(round(offset[1] + j * cell + m)), int(round(offset[1] + (j + 1) * cell - m))
        if y1 <= 0 or y0 >= a.shape[0]:
            continue
        for i in range(cols):
            x0, x1 = int(round(offset[0] + i * cell + m)), int(round(offset[0] + (i + 1) * cell - m))
            part = a[max(0, y0):max(1, y1), max(0, x0):max(1, x1)]
            if part.size:
                out[j, i] = np.median(part.reshape(-1, 3), axis=0)
    return out


def to_palette(colours: Any, palette: Any, near: float = 0.05, extra: int = 8) -> tuple[np.ndarray, int]:
    """Colours taken to a sprite's palette where one of its colours is near (OKLab distance under ``near``); the
    others are new colours of their own, brought down to at most ``extra``. Returns the colours and how many new
    ones there are."""
    colours = np.asarray(colours, np.uint8).reshape(-1, 3)
    palette = np.asarray(palette, np.uint8).reshape(-1, 3)
    if not len(colours):
        return colours.copy(), 0
    lab = np.asarray(to_oklab(colours.astype(np.float32)), np.float64)
    pal = np.asarray(to_oklab(palette.astype(np.float32)), np.float64)
    dist = ((lab[:, None, :] - pal[None, :, :]) ** 2).sum(axis=2)
    index, how = dist.argmin(axis=1), np.sqrt(dist.min(axis=1))
    out = colours.copy()
    close = how < near
    out[close] = palette[index[close]]
    new = 0
    if (~close).any():
        out[~close] = fewer_colours(colours[~close], extra)
        new = int(len(np.unique(out[~close], axis=0)))
    return out, new
