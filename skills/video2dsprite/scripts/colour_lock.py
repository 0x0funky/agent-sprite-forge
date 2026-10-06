#!/usr/bin/env python3
"""Colour lock: keep every design colour of a generated clip on the master still's colours (OKLab).

Image-to-video models drift colours from frame to frame: leather boots turn olive in one frame and
maroon in the next, a moving hand picks up a pink tint from the key. The lock works on finished
(or registered) straight-alpha RGBA frames and changes chroma (OKLab a, b) only, never lightness,
so the HD shading is kept and nothing is posterised:

  1. Master colours by height: the master's solid interior is cut into five overlapping height
     bands (feet to head) and each band gets its own OKLab k-means colours, so a boot pixel can
     only match boot-height colours and a hand only colours found at hand height.
  2. Matching: every visible pixel of a frame takes the nearest master colour of its own band and
     the neighbouring bands; it is locked fully within 0.6 x MATCH of it, not at all beyond MATCH.
     Its offset is its chroma minus that colour's chroma.
  3. Local lock: each pixel's offset is averaged over the pixels matched to the same master colour
     within 3% of the body height (normalized box filter) and that local drift is removed: a
     whole boot that turned maroon comes back while the other boot, the leggings next to it and
     the fine texture stay as they are.
  4. Snap: pixels still within SNAP of their master colour are pulled part of the way to its
     chroma (soft, at most SNAP_STRENGTH); farther pixels are left alone. The lock never adds
     more than max(0.02, half the pixel's own chroma), so a grey never turns brown.
  5. Temporal smoothing: a pixel whose source colour and opacity barely changed since the last
     frame keeps half of the last output's chroma (no shimmer on still parts; moving parts are
     never blended, so there is no ghosting). Cycles are warmed up from the last frame.

measure() gives the before/after numbers: hue flips per frame pair (pixels of similar lightness
whose chroma jumps by more than 0.03) and the per-region spread of the mean chroma across frames.

  python colour_lock.py measure --frames DIR [DIR ...] --master master.png [--loop]

Library: master_colours(), lock_frames(), measure(). finish_frames.py --colour-lock calls it.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import forge_core  # noqa: E402  (this skill's vendored copies)
import forge_palette as fp  # noqa: E402

TOOL_NAME = "colour_lock"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION
BANDS = 5                   # height bands of the master palette (feet to head), overlapping
BAND_COLOURS = 12           # colours learned per band
BAND_OVERLAP = 0.5          # each band also learns from half a band above and below
MASTER_SAMPLES = 20000      # interior master pixels sampled per band (seeded)
L_WEIGHT = 1.0              # lock: lightness weight of the colour distance (the lock never changes
                            # lightness, so it is as reliable a cue for the design part as the chroma)
CHROMA_GAIN = 0.02          # the lock may add at most max(0.02, half the pixel's chroma) of chroma: a grey
                            # never turns brown, a drifted brown still comes back
MEASURE_L_WEIGHT = 0.5      # measure(): lightness weight of the region distance (fixed definition)
MATCH = 0.12                # a pixel this close to a master colour is locked to it (fully up to 0.6 x MATCH)
SMOOTH = 0.03               # offset smoothing radius as a share of the body height
SNAP = 0.06                 # per-pixel pull radius (OKLab)
SNAP_STRENGTH = 0.35        # chroma share pulled toward the master colour at distance 0
STILL_DE = 0.04             # temporal: source colour change below this (and alpha within 5%) is still
STILL_ALPHA = 13
TEMPORAL_KEEP = 0.5         # still pixels keep this share of the previous output's chroma
FLIP_AB = 0.03              # measure(): a chroma jump this large between frames is a hue flip ...
FLIP_L = 0.05               # ... when the lightness moved less than this (the same surface)
MEASURE_MATCH = 0.10        # measure(): region membership radius (fixed, independent of the lock's MATCH)
METHOD = ("colour_lock v2: master colours = OKLab k-means, 12 per height band (5 overlapping bands from the feet to "
          "the top of the master's solid interior); a frame pixel matches the nearest colour of its band and the "
          "neighbouring bands (OKLab distance), locked fully within 0.072 and not beyond 0.12; its chroma offset is "
          "averaged over the pixels matched to the same colour within 3% of the body height and removed; pixels within "
          "0.06 of their colour are pulled up to 35% of the way in chroma; chroma grows by at most max(0.02, half the "
          "pixel's chroma); lightness never changes; still pixels (source dE < 0.04, alpha within 5%) keep 50% of the "
          "previous output's chroma; cycles warm up from the last frame")


class ColourLockError(ValueError):
    """A user-facing refusal."""


# --------------------------------------------------------------------------- body bands

def _rgba(image: Any) -> np.ndarray:
    if isinstance(image, (str, Path)):
        return np.asarray(forge_core.load_rgba(Path(image))[0], np.uint8)
    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] not in (3, 4):
        raise ColourLockError(f"expected an RGB(A) image, got shape {array.shape}")
    if array.shape[2] == 3:
        array = np.dstack([array, np.full(array.shape[:2], 255, np.uint8)])
    return array.astype(np.uint8)


def body_rows(alpha: np.ndarray) -> tuple[float, float] | None:
    """(top, feet) rows of the solid body (alpha >= 128; rows with at least 2 px), or None when empty."""
    solid = np.asarray(alpha) >= 128
    counts = solid.sum(axis=1)
    rows = np.flatnonzero(counts >= 2)
    if rows.size == 0:
        rows = np.flatnonzero(counts > 0)
        if rows.size == 0:
            return None
    return float(rows[0]), float(rows[-1] + 1)


def height_map(shape: tuple[int, int], rows: tuple[float, float]) -> np.ndarray:
    """Per pixel row: 0 at the feet line, 1 at the top of the body (clipped to -0.2..1.2)."""
    top, feet = rows
    span = max(1.0, feet - top)
    y = np.arange(shape[0], dtype=np.float64) + 0.5
    return np.broadcast_to(np.clip((feet - y) / span, -0.2, 1.2)[:, None], shape)


def _band_of(height: np.ndarray) -> np.ndarray:
    return np.clip(np.floor(height * BANDS), 0, BANDS - 1).astype(np.int64)


@dataclass(frozen=True)
class MasterColours:
    """The master's design colours: OKLab centres and the height band each was learned in."""

    lab: np.ndarray         # (K, 3)
    band: np.ndarray        # (K,)

    def __len__(self) -> int:
        return len(self.lab)


def master_colours(master: Any, *, per_band: int = BAND_COLOURS, seed: int = 0) -> MasterColours:
    """Learn the master's colours per height band from its solid interior (alpha >= 250, one px in from the
    edge, so edge mixes with the backdrop do not count). An opaque master is refused: key it first."""
    pixels = _rgba(master)
    alpha = pixels[..., 3]
    if (alpha == 255).all():
        raise ColourLockError("the colour-lock master has no transparency; pass the keyed master (RGBA)")
    rows = body_rows(alpha)
    if rows is None:
        raise ColourLockError("the colour-lock master is empty")
    solid = alpha >= 250
    interior = solid.copy()
    interior[1:] &= solid[:-1]
    interior[:-1] &= solid[1:]
    interior[:, 1:] &= solid[:, :-1]
    interior[:, :-1] &= solid[:, 1:]
    if interior.sum() < 64:
        interior = alpha >= 128
    heights = height_map(alpha.shape, rows)
    rng = np.random.default_rng(seed)
    labs, bands = [], []
    for band in range(BANDS):
        low, high = (band - BAND_OVERLAP) / BANDS, (band + 1 + BAND_OVERLAP) / BANDS
        if band == 0:
            low = -1.0
        if band == BANDS - 1:
            high = 2.0
        chosen = interior & (heights >= low) & (heights < high)
        samples = pixels[..., :3][chosen]
        if len(samples) < 8:
            continue
        if len(samples) > MASTER_SAMPLES:
            samples = samples[np.sort(rng.choice(len(samples), MASTER_SAMPLES, replace=False))]
        palette = fp.build_palette(np.ascontiguousarray(samples).reshape(1, -1, 3), int(per_band), seed=seed)
        labs.append(palette.lab.astype(np.float64))
        bands.append(np.full(len(palette), band, np.int64))
    if not labs:
        raise ColourLockError("the colour-lock master has too few solid pixels")
    return MasterColours(np.concatenate(labs), np.concatenate(bands))


def _as_master(value: Any) -> MasterColours:
    if isinstance(value, MasterColours):
        return value
    lab = np.asarray(value, np.float64).reshape(-1, 3)   # plain centres: every colour in every band
    return MasterColours(np.repeat(lab, BANDS, axis=0), np.tile(np.arange(BANDS), len(lab)))


def _assign(lab: np.ndarray, band: np.ndarray, master: MasterColours, l_weight: float = L_WEIGHT
            ) -> tuple[np.ndarray, np.ndarray]:
    """Nearest master colour among the pixel's band and the neighbouring bands (lightness weighted)."""
    weights = np.array([l_weight, 1.0, 1.0])
    best = np.zeros(len(lab), np.int64)
    best_d = np.full(len(lab), np.inf)
    for start in range(0, len(master), 16):
        block = master.lab[start:start + 16]
        allowed = np.abs(master.band[start:start + 16][None, :] - band[:, None]) <= 1
        distance = (((lab[:, None, :] - block[None]) ** 2) * weights).sum(-1)
        distance = np.where(allowed, distance, np.inf)
        index = distance.argmin(1)
        value = distance[np.arange(len(lab)), index]
        better = value < best_d
        best = np.where(better, index + start, best)
        best_d = np.where(better, value, best_d)
    return best, np.sqrt(best_d)


def _box(values: np.ndarray, radius: int) -> np.ndarray:
    """Separable box sum over (2r+1)^2 windows with zero padding (numpy only)."""
    if radius <= 0:
        return values
    out = values
    for axis in (0, 1):
        padded = np.pad(out, [(radius + 1, radius) if a == axis else (0, 0) for a in range(out.ndim)])
        summed = np.cumsum(padded, axis=axis)
        size = out.shape[axis]
        upper = np.take(summed, np.arange(2 * radius + 1, 2 * radius + 1 + size), axis=axis)
        lower = np.take(summed, np.arange(0, size), axis=axis)
        out = upper - lower
    return out


# --------------------------------------------------------------------------- the lock

REGION_PRIOR = 2.0          # px of zero offset mixed into every local average (few matched px: little change)


def _region_offsets(shape: tuple[int, int], visible: np.ndarray, index: np.ndarray, membership: np.ndarray,
                    offset: np.ndarray, radius: int) -> np.ndarray:
    """The local mean chroma offset of every visible pixel among the pixels matched to the same master
    colour within ``radius`` px (normalized box filter per colour, so a boot's drift is averaged over that
    boot only, never with the neighbouring bracer or leggings)."""
    height, width = shape
    ys, xs = np.nonzero(visible)
    smooth = np.zeros((len(ys), 2))
    for colour in np.unique(index[membership > 0]):
        own = np.flatnonzero(index == colour)
        members = own[membership[own] > 0]
        y0, y1 = max(0, int(ys[members].min()) - radius), min(height, int(ys[members].max()) + radius + 1)
        x0, x1 = max(0, int(xs[members].min()) - radius), min(width, int(xs[members].max()) + radius + 1)
        weight = np.zeros((y1 - y0, x1 - x0))
        field = np.zeros((y1 - y0, x1 - x0, 2))
        weight[ys[members] - y0, xs[members] - x0] = membership[members]
        field[ys[members] - y0, xs[members] - x0] = offset[members] * membership[members, None]
        total, summed = _box(weight, radius), _box(field, radius)
        inside = own[(ys[own] >= y0) & (ys[own] < y1) & (xs[own] >= x0) & (xs[own] < x1)]
        at_y, at_x = ys[inside] - y0, xs[inside] - x0
        smooth[inside] = summed[at_y, at_x] / (total[at_y, at_x] + REGION_PRIOR)[:, None]
    return smooth


def _lock_one(frame: np.ndarray, master: MasterColours, strength: float) -> tuple[np.ndarray, dict[str, Any]]:
    """Local lock and snap of one frame; returns OKLab (H, W, 3) and the frame's numbers."""
    lab = fp.to_oklab(frame[..., :3]).astype(np.float64)
    alpha = frame[..., 3]
    visible = alpha > 0
    stats = {"matched": 0, "meanShift": 0.0, "maxShift": 0.0}
    rows = body_rows(alpha)
    if rows is None or strength <= 0:
        return lab, stats
    heights = height_map(alpha.shape, rows)
    points = lab[visible]
    index, distance = _assign(points, _band_of(heights[visible]), master)
    own = np.clip((MATCH - distance) / (0.4 * MATCH), 0.0, 1.0)     # how much this pixel is locked
    membership = own * (alpha[visible] >= 128)                       # solid pixels measure the drift
    offset = points[:, 1:] - master.lab[index, 1:]
    smooth = _region_offsets(alpha.shape, visible, index, membership, offset,
                             max(1, int(round(SMOOTH * (rows[1] - rows[0])))))
    shifted = points.copy()
    shifted[:, 1:] -= strength * own[:, None] * smooth
    weights = np.array([L_WEIGHT, 1.0, 1.0])
    after = np.sqrt((((shifted - master.lab[index]) ** 2) * weights).sum(-1))
    pull = strength * SNAP_STRENGTH * np.clip(1.0 - after / SNAP, 0.0, 1.0)
    shifted[:, 1:] += pull[:, None] * (master.lab[index, 1:] - shifted[:, 1:])
    chroma = np.sqrt((points[:, 1:] ** 2).sum(-1))
    gain = np.sqrt((shifted[:, 1:] ** 2).sum(-1)) - chroma
    allowed = np.maximum(CHROMA_GAIN, 0.5 * chroma)
    scale = np.where(gain > allowed, allowed / np.maximum(gain, 1e-12), 1.0)
    shifted[:, 1:] = points[:, 1:] + scale[:, None] * (shifted[:, 1:] - points[:, 1:])
    moved = np.sqrt(((shifted[:, 1:] - points[:, 1:]) ** 2).sum(-1))
    lab[visible] = shifted
    stats = {"matched": int((membership > 0).sum()), "meanShift": float(moved.mean()) if moved.size else 0.0,
             "maxShift": float(moved.max()) if moved.size else 0.0}
    return lab, stats


def lock_frames(frames: Sequence[np.ndarray], master: Any, *, strength: float = 1.0, loop: bool = False,
                temporal: bool = True) -> tuple[list[np.ndarray], dict[str, Any]]:
    """Lock straight-alpha RGBA frames (one canvas) to the master colours; alpha and lightness never change.

    ``master`` is master_colours() (or plain OKLab centres). ``loop``: a cycle, so frame 0's temporal
    smoothing is seeded from the last frame."""
    master = _as_master(master)
    if not frames:
        return [], {"frames": 0}
    if not 0.0 <= float(strength) <= 1.0:
        raise ColourLockError("colour-lock strength must be 0..1")
    shape = frames[0].shape
    if any(np.asarray(frame).shape != shape for frame in frames):
        raise ColourLockError("colour-lock frames must share one canvas")
    locked, per_frame = [], []
    for frame in frames:
        lab, stats = _lock_one(np.asarray(frame, np.uint8), master, float(strength))
        locked.append(lab)
        per_frame.append(stats)
    held = 0
    if temporal and len(frames) > 1 and strength > 0:
        sources = [fp.to_oklab(np.asarray(frame)[..., :3]).astype(np.float64) for frame in frames]
        alphas = [np.asarray(frame)[..., 3].astype(np.int16) for frame in frames]
        previous = (len(frames) - 1, locked[-1].copy()) if loop else None
        for index in range(len(frames)):
            if previous is not None:
                before, output = previous
                still = ((((sources[index] - sources[before]) ** 2).sum(-1) < STILL_DE ** 2)
                         & (np.abs(alphas[index] - alphas[before]) < STILL_ALPHA)
                         & (alphas[index] > 0) & (alphas[before] > 0))
                if still.any():
                    keep = TEMPORAL_KEEP * float(strength)
                    current = locked[index]
                    current[..., 1:][still] += keep * (output[..., 1:][still] - current[..., 1:][still])
                    held += int(still.sum())
            previous = (index, locked[index])
    out = []
    for frame, lab in zip(frames, locked):
        result = np.array(frame, np.uint8, copy=True)
        visible = result[..., 3] > 0
        result[..., :3][visible] = fp.from_oklab(lab[visible])
        out.append(result)
    stats = {"frames": len(frames), "masterColours": int(len(master)), "strength": float(strength),
             "meanShift": round(float(np.mean([item["meanShift"] for item in per_frame])), 5),
             "maxShift": round(float(max(item["maxShift"] for item in per_frame)), 5),
             "temporalHeldPx": held, "temporal": bool(temporal)}
    return out, stats


# --------------------------------------------------------------------------- measurement

def measure(frames: Sequence[np.ndarray], master: Any, *, loop: bool = False,
            min_share: float = 0.005) -> dict[str, Any]:
    """Colour flicker of RGBA frames against the master colours.

    ``hueFlipsPerPair``: pixels solid (alpha >= 128) in both frames of a consecutive pair whose OKLab
    lightness moved less than FLIP_L while their chroma (a, b) moved more than FLIP_AB.
    ``regionSpread``: per master colour region (pixels matched to it within MEASURE_MATCH) holding at least
    ``min_share`` of the solid pixels in at least half the frames, the RMS distance of the region's
    per-frame mean chroma from its mean over the clip; the mean, the worst value and the worst regions
    are reported. ``foreignShare``: mean share of solid pixels farther than 2 x MEASURE_MATCH from every master
    colour of their band (bleed, tints and colours the design does not have)."""
    master = _as_master(master)
    labs, solids, regions = [], [], []
    for frame in frames:
        frame = np.asarray(frame)
        lab = fp.to_oklab(frame[..., :3]).astype(np.float64)
        solid = frame[..., 3] >= 128
        index = np.full(solid.shape, -1, np.int64)
        distance = np.full(solid.shape, np.inf)
        rows = body_rows(frame[..., 3])
        if rows is not None and solid.any():
            heights = height_map(solid.shape, rows)
            index[solid], distance[solid] = _assign(lab[solid], _band_of(heights[solid]), master,
                                                    MEASURE_L_WEIGHT)
        labs.append(lab)
        solids.append(solid)
        regions.append((index, distance))
    pairs = [(i - 1, i) for i in range(1, len(frames))]
    if loop and len(frames) > 2:
        pairs.append((len(frames) - 1, 0))
    flips = []
    for first, second in pairs:
        both = solids[first] & solids[second]
        d_l = np.abs(labs[second][..., 0] - labs[first][..., 0])
        d_ab = np.sqrt(((labs[second][..., 1:] - labs[first][..., 1:]) ** 2).sum(-1))
        flips.append(int((both & (d_l < FLIP_L) & (d_ab > FLIP_AB)).sum()))
    k = len(master)
    means = np.full((len(frames), k, 2), np.nan)
    shares = np.zeros((len(frames), k))
    foreign = []
    for f, ((index, distance), lab, solid) in enumerate(zip(regions, labs, solids)):
        total = max(1, int(solid.sum()))
        member = solid & (distance < MEASURE_MATCH)
        foreign.append(float((solid & (distance >= 2 * MEASURE_MATCH)).sum()) / total)
        counts = np.bincount(index[member], minlength=k)
        shares[f] = counts / total
        for channel in (1, 2):
            sums = np.bincount(index[member], weights=lab[..., channel][member], minlength=k)
            means[f, :, channel - 1] = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
    stable = (shares >= min_share).mean(0) >= 0.5
    spreads = {}
    for region in np.flatnonzero(stable):
        values = means[:, region][~np.isnan(means[:, region]).any(1)]
        if len(values) >= 2:
            spreads[int(region)] = float(np.sqrt(((values - values.mean(0)) ** 2).sum(1).mean()))
    worst = sorted(spreads.items(), key=lambda item: -item[1])[:5]
    return {"frames": len(frames), "pairs": len(pairs),
            "hueFlipsPerPair": round(float(np.mean(flips)) if flips else 0.0, 2),
            "regions": len(spreads),
            "regionSpreadMean": round(float(np.mean(list(spreads.values()))) if spreads else 0.0, 5),
            "regionSpreadMax": round(float(max(spreads.values())) if spreads else 0.0, 5),
            "worstRegions": [{"region": region, "band": int(master.band[region]),
                              "masterLab": [round(float(v), 4) for v in master.lab[region]],
                              "spread": round(spread, 5)} for region, spread in worst],
            "foreignShare": round(float(np.mean(foreign)) if foreign else 0.0, 5)}


FOREIGN_AB = 0.06           # foreign_drift(): a solid pixel 0.06 or more (OKLab a, b) from every master colour of its
FOREIGN_CHROMA = 0.05       # band and the neighbouring bands, with chroma above 0.05, shows a hue the design lacks
FOREIGN_ALPHA = 160         # foreign_drift(): solid pixels (alpha >= 160) only; soft edges mix with the backdrop
DRIFT_MIN_SHARE = 0.02      # foreign_drift(): regions under 2% of the solid pixels do not count for the drift


def foreign_drift(frame: np.ndarray, master: Any) -> dict[str, float]:
    """Colour bleed and drift of one RGBA frame against the master colours (the QC colour gate).

    ``foreign``: share of solid pixels whose hue no master colour at that height has: chroma above
    FOREIGN_CHROMA and at least FOREIGN_AB in (a, b) from every master colour of the pixel's band and the
    neighbouring bands (purple or green bleed on boots, a pink hand, a cyan blade); lightness is ignored, so
    shading never counts. ``drift``: the largest mean chroma offset of a master colour region holding at least
    DRIFT_MIN_SHARE of the solid pixels (a whole part shifted, which lock_frames undoes)."""
    master = _as_master(master)
    frame = np.asarray(frame)
    solid = frame[..., 3] >= FOREIGN_ALPHA
    rows = body_rows(frame[..., 3])
    if rows is None or not solid.any():
        return {"foreign": 0.0, "drift": 0.0}
    lab = fp.to_oklab(frame[..., :3][solid]).astype(np.float64)
    bands = _band_of(height_map(solid.shape, rows)[solid])
    total = len(lab)
    nearest_ab = np.full(total, np.inf)
    for start in range(0, len(master), 16):
        block = master.lab[start:start + 16, 1:]
        allowed = np.abs(master.band[start:start + 16][None, :] - bands[:, None]) <= 1
        distance = np.sqrt(((lab[:, None, 1:] - block[None]) ** 2).sum(-1))
        nearest_ab = np.minimum(nearest_ab, np.where(allowed, distance, np.inf).min(1))
    chroma = np.sqrt((lab[:, 1:] ** 2).sum(-1))
    foreign = float(((chroma > FOREIGN_CHROMA) & (nearest_ab >= FOREIGN_AB)).sum()) / total
    index, distance = _assign(lab, bands, master, MEASURE_L_WEIGHT)
    member = distance < MEASURE_MATCH
    counts = np.bincount(index[member], minlength=len(master))
    drift = 0.0
    big = counts >= max(3, DRIFT_MIN_SHARE * total)
    if big.any():
        sums_a = np.bincount(index[member], weights=lab[member, 1] - master.lab[index[member], 1],
                             minlength=len(master))
        sums_b = np.bincount(index[member], weights=lab[member, 2] - master.lab[index[member], 2],
                             minlength=len(master))
        drift = float(np.sqrt((sums_a[big] / counts[big]) ** 2 + (sums_b[big] / counts[big]) ** 2).max())
    return {"foreign": round(foreign, 5), "drift": round(drift, 5)}


# --------------------------------------------------------------------------- CLI

def _frames_of(folder: Path) -> list[np.ndarray]:
    if (folder / "frames").is_dir():
        folder = folder / "frames"
    files = sorted(path for path in folder.glob("*.png") if path.is_file())
    if not files:
        raise ColourLockError(f"no PNG frames in {folder}")
    return [np.asarray(forge_core.load_rgba(path)[0], np.uint8) for path in files]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="colour_lock.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    measure_parser = sub.add_parser("measure", help="colour flicker of frame folders against the master colours",
                                    description="Hue flips per frame pair and per-region chroma spread of each "
                                                "frame folder against the master's colours.")
    measure_parser.add_argument("--frames", nargs="+", required=True, help="frame folders (finish or register output)")
    measure_parser.add_argument("--master", required=True, help="the keyed master still (RGBA PNG)")
    measure_parser.add_argument("--loop", action="store_true", help="count the last-to-first pair (a cycle)")
    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    master = master_colours(Path(args.master))
    result = {"tool": {"name": TOOL_NAME, "version": TOOL_VERSION}, "master": Path(args.master).name,
              "masterColours": int(len(master)), "sets": []}
    for folder in args.frames:
        result["sets"].append({"frames": Path(folder).name, **measure(_frames_of(Path(folder)), master,
                                                                         loop=args.loop)})
    print(json.dumps(result, ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors exit 2; refused input exits 1 with error: ... (forge_core.run_cli)."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
