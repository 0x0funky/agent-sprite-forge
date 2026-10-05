#!/usr/bin/env python3
"""Deterministic key fixtures for tests/test_forge_matte.py (module A2-forge-matte).

Synthetic generators (numpy only, integer-hash noise, supersampled coverage, so
the same arrays come out on every platform; ``grok_jpeg_pink`` adds Pillow's
JPEG codec and is deterministic for one Pillow build):

* ``grok_jpeg_pink``      subject on the drifted Grok JPEG key (235, 21, 175)
* ``codex_png_noisy``     subject on the Codex PNG key (249, 5, 249) with +-3 noise
* ``native_alpha``        anti-aliased subject with real transparency, no backdrop
* ``purple_costume_edge`` purple costume (180, 60, 200) on the silhouette edge
* ``enclosed_hole_ring``  ring subject around an enclosed key hole of >= 300 px

plus the scenes the soft-matte acceptance tests need (outlined disk with
ground-truth alpha, 4:2:0 chroma blur, thin line, fog and slit glow, purple
material, a static frame with matte noise).

The real fixtures are four <= 256 px crops of the owner-generated Ryo clip
(report v2 frames 57, 65, 70 and 89, 0-based); PROVENANCE.json records the
source frames, crop boxes and sha256 values, and ``ryo-crops`` regenerates them:

    python tests/fixtures/keys/make_key_fixtures.py ryo-crops --frames <frames-raw dir> --output-dir <new dir>
    python tests/fixtures/keys/make_key_fixtures.py synthetic --output-dir <new dir>
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

MAGENTA = (255, 0, 255)
GROK_KEY = (235, 21, 175)
CODEX_KEY = (249, 5, 249)
PURPLE = (180, 60, 200)
NAVY = (30, 40, 90)
OUTLINE = (20, 24, 32)
FILL = (40, 160, 210)

# 0-based Ryo frame -> (x0, y0, x1, y1) crop box of frames-raw/frame_{i+1:04d}.png (960 x 960)
RYO_CROPS = {
    57: (256, 736, 448, 928),   # back boot: key tint painted inside the subject
    65: (400, 624, 600, 824),   # enclosed key hole between the legs (cfed170 kept 229-700 px)
    70: (176, 336, 432, 592),   # scarf: narrow-gap glow and chroma-smeared outline
    89: (272, 400, 512, 640),   # enclosed key hole between fist, forearm and torso (1,420 px)
}


def hash_noise(shape: tuple[int, int], amplitude: int, seed: int = 0) -> np.ndarray:
    """Integer noise in ``[-amplitude, amplitude]`` from a fixed integer hash (no RNG stream)."""
    ys, xs = np.mgrid[0:shape[0], 0:shape[1]].astype(np.int64)
    mixed = (ys * 73856093) ^ (xs * 19349663) ^ (seed * 83492791 + 0x5bd1e995)
    mixed = (mixed ^ (mixed >> 13)) * 0x2545F491
    return ((mixed ^ (mixed >> 16)) % (2 * amplitude + 1) - amplitude).astype(np.int16)


def coverage(inside, size: tuple[int, int], samples: int = 8) -> np.ndarray:
    """Per-pixel area fraction of the region ``inside(x, y)`` (pixel centres at +0.5), supersampled."""
    height, width = size
    offsets = (np.arange(samples) + 0.5) / samples
    ys = (np.arange(height)[:, None] + offsets[None, :]).reshape(-1)
    xs = (np.arange(width)[:, None] + offsets[None, :]).reshape(-1)
    grid = inside(xs[None, :], ys[:, None]).astype(np.float32)
    return grid.reshape(height, samples, width, samples).mean(axis=(1, 3))


def composite(layers: list[tuple[np.ndarray, tuple[int, int, int]]], key, size) -> np.ndarray:
    """Paint coverage layers (bottom first) over a flat key; returns float32 RGB."""
    image = np.empty((*size, 3), np.float32)
    image[...] = key
    for alpha, colour in layers:
        image = alpha[..., None] * np.asarray(colour, np.float32) + (1.0 - alpha[..., None]) * image
    return image


def to_u8(image: np.ndarray) -> np.ndarray:
    return np.clip(np.floor(image + 0.5), 0, 255).astype(np.uint8)


def _disk(cx: float, cy: float, radius: float):
    return lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def outlined_disk(size: int = 96, radius: float = 30.0, outline: float = 3.0, key=MAGENTA):
    """Outlined disk with exact supersampled coverage: ``(rgb uint8, true_alpha float32)``."""
    centre = size / 2.0
    outer = coverage(_disk(centre, centre, radius), (size, size))
    inner = coverage(_disk(centre, centre, radius - outline), (size, size))
    rgb = composite([(outer, OUTLINE), (inner, FILL)], key, (size, size))
    return to_u8(rgb), outer


def chroma_420(rgb: np.ndarray) -> np.ndarray:
    """Simulate 4:2:0 video: BT.601 full-range YCbCr, chroma averaged per 2x2 block, back to RGB."""
    pixels = rgb.astype(np.float32)
    height, width = pixels.shape[:2]
    y = pixels @ np.array([0.299, 0.587, 0.114], np.float32)
    cb = (pixels[..., 2] - y) * 0.564
    cr = (pixels[..., 0] - y) * 0.713
    pad_h, pad_w = height + height % 2, width + width % 2
    for plane in (cb, cr):
        padded = np.pad(plane, ((0, pad_h - height), (0, pad_w - width)), mode="edge")
        blocks = padded.reshape(pad_h // 2, 2, pad_w // 2, 2).mean(axis=(1, 3))
        plane[...] = np.repeat(np.repeat(blocks, 2, axis=0), 2, axis=1)[:height, :width]
    red = y + cr / 0.713
    blue = y + cb / 0.564
    green = (y - 0.299 * red - 0.114 * blue) / 0.587
    return to_u8(np.stack([red, green, blue], axis=-1))


def thin_line(size: int = 64, width: float = 2.0, key=MAGENTA) -> tuple[np.ndarray, np.ndarray]:
    """A dark vertical line ``width`` px wide at x = 31 .. 31 + width: ``(rgb, core_mask)``."""
    x0 = 31.0
    line = coverage(lambda x, y: (x >= x0) & (x < x0 + width) & (y >= 8) & (y < size - 8), (size, size))
    rgb = composite([(line, OUTLINE)], key, (size, size))
    return to_u8(rgb), line >= 0.999


def fog_and_slit(size: int = 96, key=MAGENTA) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Haze patches (lighter and darker key) and a 3 px slit of key glow between two blocks.

    Returns ``(rgb, fog_mask, slit_mask)``. The haze and glow colours stay within
    the soft matte's key-like band, as painted backdrop variants do.
    """
    rgb = np.empty((size, size, 3), np.float32)
    rgb[...] = key
    fog = np.zeros((size, size), bool)
    fog[8:28, 8:40] = True
    rgb[8:28, 8:24] = (255, 45, 255)    # lighter haze
    rgb[8:28, 24:40] = (205, 0, 205)    # darker haze
    blocks = np.zeros((size, size), bool)
    blocks[40:88, 20:44] = True
    blocks[40:88, 47:71] = True
    rgb[blocks] = OUTLINE
    rgb[42:86, 22:42] = FILL
    rgb[42:86, 49:69] = FILL
    slit = np.zeros((size, size), bool)
    slit[40:88, 44:47] = True
    rgb[slit] = (255, 40, 255)
    return to_u8(rgb), fog, slit


def plain_body(size: int = 128, key=MAGENTA) -> np.ndarray:
    """Outlined navy body on the key (a design with no key-coloured material)."""
    rgb = np.empty((size, size, 3), np.uint8)
    rgb[...] = key
    rgb[12:size - 12, 12:size - 12] = OUTLINE
    rgb[14:size - 14, 14:size - 14] = NAVY
    return rgb


def purple_material(size: int = 128, purple=PURPLE, key=MAGENTA) -> tuple[np.ndarray, np.ndarray]:
    """Navy body with an interior purple panel that never touches the backdrop: ``(rgb, interior_mask)``."""
    rgb = plain_body(size, key)
    rgb[40:88, 40:88] = purple
    interior = np.zeros((size, size), bool)
    interior[42:86, 42:86] = True
    return rgb, interior


def tinted_body(size: int = 128, key=MAGENTA) -> tuple[np.ndarray, np.ndarray]:
    """Navy body with a small key tint a generator painted inside (0.2% of the deep interior)."""
    rgb = plain_body(size, key)
    tint = np.zeros((size, size), bool)
    tint[62:66, 62:66] = True
    rgb[tint] = (96, 40, 110)  # magenta dominance 56 over a navy-ish base
    return rgb, tint


def grok_jpeg_pink(size: int = 128, quality: int = 82) -> dict:
    """Outlined subject on the drifted Grok key, JPEG round trip (4:2:0, ``quality``)."""
    rgb, alpha = outlined_disk(size, radius=size * 0.3, outline=3.0, key=GROK_KEY)
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="JPEG", quality=quality, subsampling=2)
    decoded = np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))
    return {"rgb": decoded, "key": GROK_KEY, "alpha": alpha}


def codex_png_noisy(size: int = 128, amplitude: int = 3, seed: int = 1) -> dict:
    """Outlined subject on the Codex PNG key with deterministic +-``amplitude`` noise."""
    rgb, alpha = outlined_disk(size, radius=size * 0.3, outline=3.0, key=CODEX_KEY)
    noisy = rgb.astype(np.int16)
    for channel in range(3):
        noisy[..., channel] += hash_noise(rgb.shape[:2], amplitude, seed + channel)
    return {"rgb": np.clip(noisy, 0, 255).astype(np.uint8), "key": CODEX_KEY, "alpha": alpha}


def native_alpha(size: int = 96) -> dict:
    """Anti-aliased outlined disk with real transparency (straight alpha, RGB 0 where alpha is 0)."""
    centre = size / 2.0
    outer = coverage(_disk(centre, centre, size * 0.3), (size, size))
    inner = coverage(_disk(centre, centre, size * 0.3 - 3.0), (size, size))
    colour = np.where(inner[..., None] > 0, np.asarray(FILL, np.float32), np.asarray(OUTLINE, np.float32))
    alpha8 = to_u8(outer * 255.0)
    rgba = np.dstack([to_u8(colour), alpha8])
    rgba[alpha8 == 0] = 0
    return {"rgba": rgba, "alpha": outer}


def purple_costume_edge(size: int = 64, key=MAGENTA) -> dict:
    """jev purple_edge_check scene: navy body, purple patches enclosed and on the outer edge."""
    rgb = np.empty((size, size, 3), np.uint8)
    rgb[...] = key
    rgb[8:56, 8:56] = NAVY
    rgb[20:30, 20:30] = PURPLE   # enclosed patch
    rgb[40:50, 8:18] = PURPLE    # patch on the body's outer edge (x = 8)
    enclosed = np.zeros((size, size), bool)
    enclosed[20:30, 20:30] = True
    edge = np.zeros((size, size), bool)
    edge[40:50, 8:18] = True
    return {"rgb": rgb, "key": key, "enclosed": enclosed, "edge": edge}


def enclosed_hole_ring(size: int = 96, outer: float = 36.0, inner: float = 12.0, key=MAGENTA) -> dict:
    """Outlined ring around an enclosed key hole (pi * 12^2 = 452 px >= 300 px)."""
    centre = size / 2.0
    ring = coverage(lambda x, y: ((x - centre) ** 2 + (y - centre) ** 2 <= outer ** 2)
                    & ((x - centre) ** 2 + (y - centre) ** 2 >= inner ** 2), (size, size))
    body = coverage(lambda x, y: ((x - centre) ** 2 + (y - centre) ** 2 <= (outer - 3) ** 2)
                    & ((x - centre) ** 2 + (y - centre) ** 2 >= (inner + 3) ** 2), (size, size))
    rgb = composite([(ring, OUTLINE), (body, FILL)], key, (size, size))
    ys, xs = np.mgrid[0:size, 0:size]
    hole = (xs + 0.5 - centre) ** 2 + (ys + 0.5 - centre) ** 2 <= (inner - 1.5) ** 2
    return {"rgb": to_u8(rgb), "key": key, "hole": hole, "alpha": ring}


def noisy_static_sequence(frames: int = 12, size: int = 64, amplitude: float = 0.12, seed: int = 7) -> dict:
    """A static soft-edged matte with per-frame alpha noise on the edge band (keyer flicker)."""
    centre = size / 2.0
    truth = coverage(_disk(centre, centre, size * 0.3), (size, size), samples=4)
    ys, xs = np.mgrid[0:size, 0:size]
    radial = np.hypot(xs + 0.5 - centre, ys + 0.5 - centre)
    soft = np.clip((size * 0.3 + 2.0 - radial) / 4.0, 0.0, 1.0).astype(np.float32)
    edge = (soft > 0) & (soft < 1)
    sequence = []
    for index in range(frames):
        noise = hash_noise((size, size), 1000, seed + index).astype(np.float32) / 1000.0 * amplitude * 2.0
        sequence.append(np.where(edge, np.clip(soft + noise, 0.0, 1.0), soft).astype(np.float32))
    return {"alphas": sequence, "truth": soft, "edge": edge, "coverage": truth}


def legacy_sheet(size: int = 2048, cells: int = 4) -> np.ndarray:
    """RGBA chroma sheet that exercises every branch of the cfed170 keyers (S03, S11).

    Per cell: an outlined ellipse subject, a key-mixed fringe whose mix ratio
    straddles the 100/150 hard-key thresholds and the 55 flood distance, an
    enclosed near-key hole, a bright-pink patch, hidden RGB under a transparent
    stripe and a semi-transparent patch; the backdrop carries near-key noise.
    Arithmetic only, so the bytes are identical on every platform.
    """
    ys, xs = np.mgrid[0:size, 0:size]
    rgba = np.empty((size, size, 4), np.int32)
    rgba[..., :3] = MAGENTA
    rgba[..., 3] = 255
    noise = hash_noise((size, size), 40, seed=11).astype(np.int32)
    loud = hash_noise((size, size), 3, seed=12) == 3
    rgba[..., 0] -= np.abs(noise) * loud
    rgba[..., 1] += np.abs(hash_noise((size, size), 40, seed=13)) * loud
    rgba[..., 2] -= np.abs(hash_noise((size, size), 40, seed=14)) * loud
    step = size // cells
    for index in range(cells * cells):
        row, col = divmod(index, cells)
        top, left = row * step, col * step
        window = (slice(top, top + step), slice(left, left + step))
        cy, cx = top + step / 2.0, left + step / 2.0
        ry, rx = step * (0.28 + 0.02 * (index % 4)), step * (0.22 + 0.03 * (index % 3))
        radial = ((xs[window] + 0.5 - cx) / rx) ** 2 + ((ys[window] + 0.5 - cy) / ry) ** 2
        cell = rgba[window]
        mix = 0.15 + 0.1 * (index % 6)  # fringe colour share: distances 60..170 from #FF00FF
        fringe = (radial > 1.0) & (radial <= 1.18)
        outline = np.array([20, 24, 32])
        cell[fringe, :3] = np.round(mix * outline + (1 - mix) * np.array(MAGENTA))
        cell[radial <= 1.0, :3] = outline
        fill = np.array([(40 + 37 * index) % 200 + 20, (160 + 53 * index) % 200 + 20, (90 + 71 * index) % 200 + 20])
        cell[radial <= 0.85, :3] = fill
        hole = (((xs[window] + 0.5 - cx) / (rx * 0.25)) ** 2 + ((ys[window] + 0.5 - cy) / (ry * 0.25)) ** 2) <= 1
        cell[hole, :3] = (255 - 6 * (index % 9), 4 * (index % 7), 255 - 5 * (index % 8))
        local_y, local_x = ys[window] - top, xs[window] - left
        cell[(local_y > step * 0.1) & (local_y < step * 0.16) & (local_x > step * 0.3), :3] = (230, 90, 200)
        stripe = (local_x > step * 0.05) & (local_x < step * 0.09)
        cell[stripe, 3] = 0
        cell[stripe, 0] = (local_y[stripe] * 7) % 256
        soft = (local_y > step * 0.85) & (local_x > step * 0.5)
        cell[soft, 3] = 40 + 9 * index
    return np.clip(rgba, 0, 255).astype(np.uint8)


SYNTHETIC = {
    "grok_jpeg_pink": lambda: grok_jpeg_pink()["rgb"],
    "codex_png_noisy": lambda: codex_png_noisy()["rgb"],
    "native_alpha": lambda: native_alpha()["rgba"],
    "purple_costume_edge": lambda: purple_costume_edge()["rgb"],
    "enclosed_hole_ring": lambda: enclosed_hole_ring()["rgb"],
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_synthetic(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=False)
    paths = []
    for name, build in SYNTHETIC.items():
        path = output_dir / f"{name}.png"
        Image.fromarray(build()).save(path)
        paths.append(path)
    return paths


def pixels_sha256(path: Path) -> str:
    """sha256 of the decoded RGB bytes: PNG bytes vary with the zlib build, pixels do not."""
    with Image.open(path) as image:
        return hashlib.sha256(np.ascontiguousarray(np.asarray(image.convert("RGB"))).tobytes()).hexdigest()


def write_ryo_crops(frames_dir: Path, output_dir: Path) -> list[dict]:
    """Crop the four Ryo fixtures from frames-raw/frame_{i+1:04d}.png (rows use PROVENANCE.json fields)."""
    output_dir.mkdir(parents=True, exist_ok=False)
    rows = []
    for index, box in RYO_CROPS.items():
        source = frames_dir / f"frame_{index + 1:04d}.png"
        with Image.open(source) as image:
            crop = image.convert("RGB").crop(box)
        target = output_dir / f"ryo-f{index:03d}-crop.png"
        crop.save(target, format="PNG", optimize=True)
        rows.append({"file": target.name, "sha256": _sha256(target), "pixels_sha256": pixels_sha256(target),
                     "size": list(crop.size), "frame_index_0based": index, "crop_box": list(box),
                     "decoded_frame": source.name, "decoded_frame_sha256": _sha256(source)})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the forge_matte key fixtures (synthetic or Ryo crops).")
    sub = parser.add_subparsers(dest="command", required=True)
    synthetic = sub.add_parser("synthetic", help="Write the five synthetic generator images as PNGs.")
    synthetic.add_argument("--output-dir", type=Path, required=True, help="New directory for the PNGs.")
    ryo = sub.add_parser("ryo-crops", help="Crop the four Ryo fixtures from the decoded raw frames.")
    ryo.add_argument("--frames", type=Path, required=True, help="Directory of frame_0001.png ... (frames-raw).")
    ryo.add_argument("--output-dir", type=Path, required=True, help="New directory for the crops.")
    args = parser.parse_args(argv)
    try:
        if args.command == "synthetic":
            print(json.dumps({"written": [path.name for path in write_synthetic(args.output_dir)]}))
        else:
            print(json.dumps(write_ryo_crops(args.frames, args.output_dir), indent=2))
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
