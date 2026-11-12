"""Synthetic sprites and pictures for the pixel-look tests (test_pixelgrid, test_pixel_look, test_pixel_motion).

Deterministic and offline: a toy sprite with a closed dark outline, the "pixel art" picture an image model would
paint of it (squares that are not all the same size, soft cell edges, a flat key colour), and whole-cell poses.
"""
from __future__ import annotations

import numpy as np

KEY = (255, 0, 255)
LINE = (24, 16, 12)
COLOURS = ((226, 178, 132), (118, 62, 40), (200, 44, 52), (44, 92, 188), (238, 208, 96), (92, 160, 84))
HEAD_ROWS = 9             # of the default 16 x 24 sprite


def toy_sprite(width: int = 16, height: int = 24) -> np.ndarray:
    """A figure with a head, a body and two legs that fills ``width`` x ``height``: a closed outline in LINE and
    six flat colours in small clusters."""
    y, x = np.mgrid[0:height, 0:width]
    cx, head_y = (width - 1) / 2.0, 0.19 * height
    head = ((x - cx) / (width * 0.5)) ** 2 + ((y - head_y) / (head_y + 0.05)) ** 2 <= 1.0
    body = (np.abs(x - cx) <= width * 0.36) & (y >= 0.33 * height) & (y < 0.75 * height)
    legs = (np.abs(np.abs(x - cx) - width * 0.23) <= width * 0.13) & (y >= 0.75 * height)
    figure = head | body | legs
    padded = np.pad(figure, 1, constant_values=False)
    inner = padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2] & padded[1:-1, 2:]
    sprite = np.zeros((height, width, 4), dtype=np.uint8)
    pick = (x // 2 + (y // 2) * 3 + (x * y) % 3) % len(COLOURS)
    sprite[figure, :3] = np.asarray(COLOURS, np.uint8)[pick[figure]]
    sprite[figure & ~inner, :3] = LINE
    sprite[figure, 3] = 255
    rows, cols = np.where(figure)
    assert (cols.min(), cols.max(), rows.min(), rows.max()) == (0, width - 1, 0, height - 1), "the figure fills the sprite"
    return sprite


def cell_sizes(count: int, base: int, spread: int) -> list[int]:
    """Uneven cell sizes around ``base`` px, as an image model paints them."""
    return [base + ((i * 7) % (2 * spread + 1)) - spread for i in range(count)] if spread else [base] * count


def fake_picture(sprite: np.ndarray, base: int = 22, spread: int = 2, margin: int = 60, soften: bool = True,
                 tall: int | None = None) -> np.ndarray:
    """The picture an image model paints of ``sprite``: RGB uint8 on the key colour. Its blocks are about ``base`` px
    wide and ``tall`` px high (``base`` when not given: square blocks)."""
    widths, heights = cell_sizes(sprite.shape[1], base, spread), cell_sizes(sprite.shape[0], tall or base, spread)
    xs, ys = np.concatenate([[0], np.cumsum(widths)]) + margin, np.concatenate([[0], np.cumsum(heights)]) + margin
    picture = np.empty((int(ys[-1]) + margin, int(xs[-1]) + margin, 3), dtype=np.float64)
    picture[...] = KEY
    for row in range(sprite.shape[0]):
        for col in range(sprite.shape[1]):
            if sprite[row, col, 3]:
                picture[ys[row]:ys[row + 1], xs[col]:xs[col + 1]] = sprite[row, col, :3]
    if soften:                                        # soft cell edges: a 3 x 3 box
        padded = np.pad(picture, ((1, 1), (1, 1), (0, 0)), mode="edge")
        picture = sum(padded[dy:dy + picture.shape[0], dx:dx + picture.shape[1]] for dy in range(3) for dx in range(3)) / 9.0
    return np.clip(picture + 0.5, 0, 255).astype(np.uint8)


def on_canvas(sprite: np.ndarray, size: tuple[int, int], at: tuple[int, int]) -> np.ndarray:
    """``sprite`` on a transparent canvas of ``size`` (cols, rows) with its corner at ``at`` (x, y)."""
    canvas = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    canvas[at[1]:at[1] + sprite.shape[0], at[0]:at[0] + sprite.shape[1]] = sprite
    return canvas


def with_arm(canvas: np.ndarray, at: tuple[int, int]) -> np.ndarray:
    """A small outlined block (an arm held out) with its corner at ``at`` (x, y): the outline stays closed."""
    out = canvas.copy()
    x, y = at
    out[y:y + 4, x:x + 4] = (*LINE, 255)
    out[y + 1:y + 3, x + 1:x + 3] = (*COLOURS[2], 255)
    return out


def video_frame(cells: np.ndarray, scale: int, corner: tuple[int, int], size: tuple[int, int], key=KEY) -> np.ndarray:
    """A 1x RGBA picture enlarged by ``scale`` with its corner at ``corner`` on an opaque ``size`` (w, h) frame."""
    frame = np.empty((size[1], size[0], 4), dtype=np.uint8)
    frame[...] = (*key, 255)
    big = np.repeat(np.repeat(cells, scale, axis=0), scale, axis=1)
    region = frame[corner[1]:corner[1] + big.shape[0], corner[0]:corner[0] + big.shape[1]]
    mask = big[..., 3] > 0
    region[mask, :3] = big[mask][:, :3]
    return frame


def with_blade(sprite: np.ndarray, reach: int = 8) -> np.ndarray:
    """``sprite`` on a canvas ``reach`` cells wider on the left, holding a blade two cells wide that runs from its
    side down to the ground, away from the feet: the sprite's own cells are at x + ``reach``."""
    height, width = sprite.shape[:2]
    out = np.zeros((height, width + reach, 4), dtype=np.uint8)
    out[:, reach:] = sprite
    for step in range(reach):
        out[height - 1 - step, step:step + 2] = (*LINE, 255)
    return out


def lattice_picture(cells: np.ndarray, cell: int, size: tuple[int, int], key=KEY, shift: tuple[int, int] = (0, 0),
                    soften: bool = True) -> np.ndarray:
    """The picture an image model gives back for "reproduce the attached image": ``cells`` (RGBA, one a sprite
    pixel, the whole lattice of a reference) at ``cell`` px on an RGB picture of ``size`` (w, h), moved by ``shift``
    px, with soft cell edges."""
    frame = video_frame(cells, cell, (0, 0), size, key)[..., :3].astype(np.float64)
    moved = np.empty_like(frame)
    moved[...] = key
    dx, dy = shift
    h, w = frame.shape[:2]
    moved[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)] = frame[max(0, -dy):h - max(0, dy), max(0, -dx):w - max(0, dx)]
    if soften:
        padded = np.pad(moved, ((1, 1), (1, 1), (0, 0)), mode="edge")
        moved = sum(padded[y:y + h, x:x + w] for y in range(3) for x in range(3)) / 9.0
    return np.clip(moved + 0.5, 0, 255).astype(np.uint8)


def walk_frames(steps: int = 6, pairs: int = 4, still: int = 4, facing: str = "right", twice: bool = False,
                pole: bool = False) -> tuple[list, np.ndarray, tuple[int, int]]:
    """A walk in place as a clip gives it, on a canvas with room around the sprite: the toy sprite's head and body
    stand still above two feet. Each foot stands on the ground for ``steps`` frames and travels back one cell a
    frame, then is carried forward, lifted out of the ground rows, and is put down in front. With ``pole`` a
    staff stands on the ground in front of the feet in every frame (it is no part of the sprite). Returns the frames
    (RGBA, ``still`` equal frames before and after ``pairs`` pairs of steps; every frame twice when ``twice``), the
    standing sprite and where it lies on the canvas (x, y)."""
    body = toy_sprite()
    body[18:] = 0                                            # the legs go: the feet below are drawn per frame
    at, size = (12, 4), (40, 30)
    foot = np.zeros((3, 4, 4), dtype=np.uint8)
    foot[...] = (*LINE, 255)
    foot[1, 1:3] = (*COLOURS[1], 255)

    def frame(first: int | None, second: int | None) -> np.ndarray:
        """The body and a foot at each given offset from the middle (None: that foot is in the air, lifted)."""
        canvas = on_canvas(body, size, at)
        for offset, lifted in ((first, False), (second, False)):
            if offset is not None:
                x = at[0] + 6 + offset
                canvas[at[1] + 21:at[1] + 24, x:x + 4] = foot
        canvas[at[1] + 18:at[1] + 21, at[0] + 6:at[0] + 10] = (*COLOURS[3], 255)      # one leg stub joins body and feet
        if pole:                                              # a staff set on the ground in front of the feet
            canvas[at[1] + 4:at[1] + 24, at[0] + 20:at[0] + 22] = (*LINE, 255)
        return canvas

    rest = frame(-3, 3)
    moving = []
    for n in range(2 * pairs * steps):
        phase = n % steps
        standing = 3 - phase                                  # the standing foot: from +3 back, one cell a frame
        moving.append(frame(standing, None) if phase else frame(standing, -3))    # the other foot leaves the ground at a footfall
    frames = [rest] * still + moving + [rest] * still
    if facing == "left":
        frames = [np.ascontiguousarray(f[:, ::-1]) for f in frames]
    if twice:
        frames = [f for f in frames for _ in range(2)]
    sprite = rest[at[1]:at[1] + 24, at[0]:at[0] + 16].copy()
    if facing == "left":
        sprite = frames[0][at[1]:at[1] + 24, size[0] - at[0] - 16:size[0] - at[0]]
    return frames, np.ascontiguousarray(sprite), (at if facing == "right" else (size[0] - at[0] - 16, at[1]))
