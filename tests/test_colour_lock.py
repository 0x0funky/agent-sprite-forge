"""video2dsprite colour_lock: the HD colour lock (design colours kept on the master's, chroma only) and the
measurements behind the QC colour gate.

Synthetic, deterministic figures: a master with boots, legs, a torso and a head stacked from the feet up, and
frames whose boots drift olive or maroon from frame to frame like the 2026-10-06 live run.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from forge_testutils import load_script, run_cli, script_path

CL = load_script("video2dsprite", "colour_lock")
FP = load_script("video2dsprite", "forge_palette")
SCRIPT = script_path("video2dsprite", "colour_lock")

BOOT = (120, 78, 45)        # warm mid-brown leather
LEGGINGS = (58, 58, 62)     # charcoal grey
TORSO = (40, 110, 140)      # teal
SKIN = (232, 190, 160)
SIZE = (60, 120)


def figure(boot=BOOT, *, shift=0, noise=0.0, seed=0, hand=None) -> np.ndarray:
    """RGBA: boots (rows 96-110), leggings (70-96), torso (35-70), head (12-35) over transparency; ``hand`` paints
    a 6x6 hand at mid height in that colour; ``shift`` moves the figure right."""
    width, height = SIZE
    rgba = np.zeros((height, width, 4), np.uint8)
    rng = np.random.default_rng(seed)
    x0, x1 = 20 + shift, 40 + shift
    for (top, bottom), colour in (((96, 110), boot), ((70, 96), LEGGINGS), ((35, 70), TORSO), ((12, 35), SKIN)):
        block = np.empty((bottom - top, x1 - x0, 3), np.float64)
        block[...] = colour
        shade = np.linspace(-14, 14, x1 - x0)[None, :, None]     # a lightness ramp: HD shading to keep
        block = block + shade
        if noise:
            block = block + rng.normal(0, noise, block.shape)
        rgba[top:bottom, x0:x1, :3] = np.clip(block + 0.5, 0, 255).astype(np.uint8)
        rgba[top:bottom, x0:x1, 3] = 255
    if hand is not None:
        rgba[50:56, x1:x1 + 6, :3] = hand
        rgba[50:56, x1:x1 + 6, 3] = 255
    return rgba


def drift(colour, da, db):
    """``colour`` with its OKLab chroma moved by (da, db)."""
    lab = FP.to_oklab(np.array(colour, np.uint8)).astype(np.float64)
    lab[1] += da
    lab[2] += db
    return tuple(int(v) for v in FP.from_oklab(lab))


OLIVE, MAROON = drift(BOOT, -0.035, 0.015), drift(BOOT, 0.035, -0.03)


@pytest.fixture(scope="module")
def master():
    return CL.master_colours(figure(hand=SKIN))


def test_master_colours_are_learned_per_height_band(master):
    assert len(master) >= 10 and set(master.band.tolist()) == set(range(CL.BANDS))
    boot_lab = FP.to_oklab(np.array(BOOT, np.uint8)).astype(np.float64)
    low = master.lab[master.band == 0]
    top = master.lab[master.band == CL.BANDS - 1]
    assert np.sqrt(((low - boot_lab) ** 2).sum(1)).min() < 0.03       # the boots live in the feet band ...
    assert np.sqrt(((top - boot_lab) ** 2).sum(1)).min() > 0.08       # ... and never in the head band
    with pytest.raises(CL.ColourLockError, match="transparency"):
        CL.master_colours(np.full((20, 20, 4), 255, np.uint8))


def test_lock_brings_drifting_boots_back_and_keeps_lightness(master):
    frames = [figure(OLIVE if i % 2 else MAROON, shift=i % 3) for i in range(8)]   # olive, maroon, olive, ...
    before = CL.measure(frames, master, loop=True)
    locked, stats = CL.lock_frames(frames, master, loop=True)
    after = CL.measure(locked, master, loop=True)
    assert stats["frames"] == 8 and stats["meanShift"] > 0
    assert after["hueFlipsPerPair"] < 0.25 * before["hueFlipsPerPair"]
    assert after["regionSpreadMax"] < 0.5 * before["regionSpreadMax"]
    boot_ab = FP.to_oklab(np.array(BOOT, np.uint8)).astype(np.float64)[1:]
    for source, out in zip(frames, locked):
        assert (out[..., 3] == source[..., 3]).all()                   # alpha is never touched
        boots = out[98:108, 22:38, :3].reshape(-1, 3)
        lab_out = FP.to_oklab(boots).astype(np.float64)
        lab_in = FP.to_oklab(source[98:108, 22:38, :3].reshape(-1, 3)).astype(np.float64)
        assert np.abs(lab_out[:, 0] - lab_in[:, 0]).max() < 0.01        # lightness (the shading) is kept
        assert np.sqrt(((lab_out[:, 1:].mean(0) - boot_ab) ** 2).sum()) < 0.012   # chroma back on the master
        assert np.ptp(lab_out[:, 0]) > 0.05                              # not posterised: the ramp survives


def test_lock_leaves_greys_and_far_colours_alone(master):
    purple = (150, 60, 170)
    frame = figure()
    frame[100:106, 22:28, :3] = purple                                    # a hue the design does not have
    locked, _ = CL.lock_frames([frame], master)
    grey_in = FP.to_oklab(frame[75:90, 22:38, :3].reshape(-1, 3)).astype(np.float64)
    grey_out = FP.to_oklab(locked[0][75:90, 22:38, :3].reshape(-1, 3)).astype(np.float64)
    assert np.sqrt((grey_out[:, 1:] ** 2).sum(1)).max() <= np.sqrt((grey_in[:, 1:] ** 2).sum(1)).max() + 0.021
    assert np.abs(locked[0][100:106, 22:28, :3].astype(int) - np.array(purple)).max() <= 2   # foreign: untouched


def test_temporal_smoothing_holds_still_pixels_without_ghosting(master):
    still = [figure(noise=3.0, seed=i) for i in range(6)]
    locked, stats = CL.lock_frames(still, master, loop=True)
    plain, _ = CL.lock_frames(still, master, loop=True, temporal=False)
    assert stats["temporalHeldPx"] > 0

    def jitter(frames):
        labs = [FP.to_oklab(frame[40:60, 22:38, :3]).astype(np.float64) for frame in frames]
        return float(np.mean([np.abs(b[..., 1:] - a[..., 1:]).mean() for a, b in zip(labs, labs[1:])]))

    assert jitter(locked) < jitter(plain)
    moving = [figure(shift=6 * i) for i in range(3)]                   # moves 6 px per frame: never blended
    out, _ = CL.lock_frames(moving, master)
    alone = [CL.lock_frames([frame], master)[0][0] for frame in moving]
    assert all(np.array_equal(a[..., 3], b[..., 3]) for a, b in zip(out, alone))
    assert np.abs(out[1][:, 26:32, :3].astype(int) - alone[1][:, 26:32, :3].astype(int)).max() <= 3


def test_foreign_drift_reports_bleed_and_region_drift(master):
    clean = CL.foreign_drift(figure(), master)
    assert clean["foreign"] < 0.005 and clean["drift"] < 0.02
    bled = figure()
    bled[96:110, 20:40, :3] = (150, 60, 170)                            # purple boots: key bleed
    assert CL.foreign_drift(bled, master)["foreign"] > 0.05
    olive = CL.foreign_drift(figure(OLIVE), master)
    assert olive["foreign"] < 0.02 and olive["drift"] > 0.03           # a drift (the lock's job), not bleed


def test_cli_measure_prints_one_ascii_json_line(tmp_path, master):
    from PIL import Image
    (tmp_path / "set").mkdir()
    for index in range(4):
        Image.fromarray(figure(OLIVE if index % 2 else MAROON)).save(tmp_path / "set" / f"frame_{index:06d}.png")
    Image.fromarray(figure(hand=SKIN)).save(tmp_path / "master.png")
    result = run_cli([SCRIPT, "measure", "--frames", tmp_path / "set", "--master", tmp_path / "master.png",
                      "--loop"], "cp1252")
    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii() and len(result.stdout.strip().splitlines()) == 1
    summary = json.loads(result.stdout)
    assert summary["sets"][0]["hueFlipsPerPair"] > 0 and summary["masterColours"] >= 10
