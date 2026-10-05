"""D15: forge_matte.key_still keys sheets region by region (soft_matte_regions), byte for byte.

B01's ``_local_soft_matte_regions`` was promoted into forge_matte. The bytes
must equal one whole-image ``soft_matte`` call on every sheet, so each test
compares against that call: the fox fixture (native alpha, and composited on
magenta), the meadow crop, noisy backdrops and the 2048 px synthetic sheet.

Measured speed-up of key_still's soft path on this suite's sheets (best of
2, Windows, 4 BLAS threads, 2026-10-05): synthetic 2048 px 4x4 sheet 3.15 s
-> 2.06 s (x1.53; 56% of the area matted), meadow crop 1.05 s -> 0.91 s
(x1.16; 74%); the fox sheets fall back to one whole-image call (their
subjects fill the sheet), as do noisy backdrops with more than 64 groups, at
a pre-check cost of 1-6%.

The promotion adds one guard B01's copy lacked: soft_matte's colour-clean
band sums whole numbers in float32 integral images, which stop being exact
past 2**24, and then a crop rounds differently from the whole sheet (B01's
copy differed by 5 px on a 1536 px sheet of large pink subjects). Such
sheets take the whole-image call.
"""
from __future__ import annotations

import math
import time

import numpy as np
import pytest
from PIL import Image

from forge_testutils import load_shared, make_magenta_sheet, real_fixture

fm = load_shared("forge_matte")


# --------------------------------------------------------------------------- B01's copy, verbatim (the oracle)

_LUMA_ROW_SQ = 0.299 ** 2 + 0.587 ** 2 + 0.114 ** 2
_CHROMA_ROWS_SQ = 0.168736 ** 2 + 0.331264 ** 2 + 0.5 ** 2 + 0.5 ** 2 + 0.418688 ** 2 + 0.081312 ** 2


def _oracle_b01_regions(pixels, params, key_rgb):
    """generate2dsprite._local_soft_matte_regions (B01), verbatim (module prefixes adapted)."""
    height, width = pixels.shape[:2]
    safe = 0.999 * params.t_bg / math.sqrt(_LUMA_ROW_SQ + params.w_chroma * _CHROMA_ROWS_SQ)
    offset = pixels[..., :3].astype(np.float32) - np.asarray(key_rgb, np.float32)
    content = (offset * offset).sum(-1) > np.float32(safe * safe)
    if pixels.shape[2] == 4:
        content &= pixels[..., 3] > 0
    tile = 16
    tiles_y, tiles_x = -(-height // tile), -(-width // tile)
    padded = np.zeros((tiles_y * tile, tiles_x * tile), bool)
    padded[:height, :width] = content
    tile_mask = padded.reshape(tiles_y, tile, tiles_x, tile).any(axis=(1, 3))
    out = np.zeros((height, width, 4), np.uint8)
    if not tile_mask.any():
        return out
    labels, count = fm.forge_core.label_components(tile_mask, 8)
    if count > 64:
        return fm.soft_matte(pixels, params, key_rgb)
    groups = []
    for label in range(1, count + 1):
        rows, cols = np.nonzero(labels == label)
        tile_box = (int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1)
        box = (max(0, tile_box[0] * tile - 32), max(0, tile_box[1] * tile - 32),
               min(width, tile_box[2] * tile + 32), min(height, tile_box[3] * tile + 32))
        groups.append((label, tile_box, box))
    if sum((x1 - x0) * (y1 - y0) for _label, _tiles, (x0, y0, x1, y1) in groups) >= 0.75 * height * width:
        return fm.soft_matte(pixels, params, key_rgb)
    for label, (tx0, ty0, tx1, ty1), (x0, y0, x1, y1) in groups:
        keyed = fm.soft_matte(pixels[y0:y1, x0:x1], params, key_rgb)
        own = np.repeat(np.repeat(labels[ty0:ty1, tx0:tx1] == label, tile, axis=0), tile, axis=1)
        top, left = ty0 * tile, tx0 * tile
        own = own[:height - top, :width - left]
        region = out[top:top + own.shape[0], left:left + own.shape[1]]
        source = keyed[top - y0:top - y0 + own.shape[0], left - x0:left - x0 + own.shape[1]]
        region[own] = source[own]
    return out


# --------------------------------------------------------------------------- sheets

def _params(interior: bool):
    return fm.KeyParams(**{**fm.STILL_KEY_PARAMS.to_dict(), "interior_despill": interior})


def _fox() -> np.ndarray:
    with Image.open(real_fixture("raw-fox-run-v1.png")) as image:
        return np.asarray(image.convert("RGBA"))


def _fox_on_magenta() -> np.ndarray:
    """The fox composited over #FF00FF: a fully opaque chroma sheet like the one image_gen would return."""
    fox = _fox()
    alpha = fox[..., 3:].astype(np.float32) / 255.0
    sheet = np.empty_like(fox)
    sheet[..., :3] = np.floor(fox[..., :3] * alpha + np.array([255.0, 0.0, 255.0]) * (1.0 - alpha) + 0.5)
    sheet[..., 3] = 255
    return sheet


def _meadow() -> np.ndarray:
    with Image.open(real_fixture("meadow-prop-pack-crop.png")) as image:
        return np.asarray(image.convert("RGB"))


def _noisy(amplitude: int, seed: int) -> np.ndarray:
    """Subjects on a magenta backdrop with uniform per-channel noise of +-amplitude."""
    sheet = np.array(make_magenta_sheet(2, 3, 256, margin=96, fringe=True))
    noise = np.random.default_rng(seed).integers(-amplitude, amplitude + 1, sheet[..., :3].shape)
    sheet[..., :3] = np.clip(sheet[..., :3].astype(int) + noise, 0, 255)
    return sheet


def _synthetic_2048() -> np.ndarray:
    """B01's perf sheet (test_process_2048_chroma_under_3s): 4x4 cells of 512 px, margin 100, fringe."""
    return np.array(make_magenta_sheet(4, 4, 512, margin=100, fringe=True))


def _pink(size: int = 1024, block: int = 320) -> np.ndarray:
    """Four large noisy pink subjects: the colour-clean band's dominance sums pass 2**24."""
    sheet = np.zeros((size, size, 4), np.uint8)
    sheet[...] = (255, 0, 255, 255)
    rng = np.random.default_rng(1)
    gap = (size - 2 * block) // 4
    for top in (gap, size - gap - block):
        for left in (gap, size - gap - block):
            colour = np.array([230, 90, 200]) + rng.integers(-25, 26, (block, block, 3))
            sheet[top:top + block, left:left + block, :3] = np.clip(colour, 0, 255)
    return sheet


@pytest.fixture
def matte_calls(monkeypatch):
    """Record the size of every soft_matte call soft_matte_regions makes."""
    calls = []
    original = fm.soft_matte

    def spy(pixels, *args, **kwargs):
        calls.append(tuple(np.asarray(pixels).shape[:2]))
        return original(pixels, *args, **kwargs)

    monkeypatch.setattr(fm, "soft_matte", spy)
    return calls, original


def _key_still_against_whole(pixels, original_soft_matte):
    image, info = fm.key_still(pixels, quality="soft")
    assert info["quality"] == "soft"
    rgba = pixels if pixels.shape[2] == 4 else np.dstack([pixels, np.full(pixels.shape[:2], 255, np.uint8)])
    key, _ = fm.estimate_key(rgba, "magenta")  # key_still's own estimate (info["key"] is rounded)
    whole = original_soft_matte(rgba, fm.KeyParams(**info["params"]), key)
    np.testing.assert_array_equal(np.asarray(image), whole)
    return info


# --------------------------------------------------------------------------- key_still end to end (D15)

def test_key_still_on_the_fox_fixture(matte_calls):
    """Real fixture: the fox on magenta keys byte for byte (its subjects fill the sheet: one whole call)."""
    calls, original = matte_calls
    sheet = _fox_on_magenta()
    _key_still_against_whole(sheet, original)
    assert calls == [sheet.shape[:2]]


def test_soft_matte_regions_on_the_native_alpha_fox(matte_calls):
    """Real fixture with its own alpha 0..254: content needs alpha > 0; alpha stays an upper bound."""
    calls, original = matte_calls
    fox = _fox()
    key = np.array([255, 0, 255], np.float32)
    for interior in (False, True):
        np.testing.assert_array_equal(fm.soft_matte_regions(fox, _params(interior), key),
                                      original(fox, _params(interior), key))


def test_key_still_on_the_meadow_crop_mattes_eight_regions(matte_calls):
    """Real fixture (RGB prop sheet): six props, eight crops, about three quarters of the area."""
    calls, original = matte_calls
    sheet = _meadow()
    _key_still_against_whole(sheet, original)
    area = sum(h * w for h, w in calls) / (sheet.shape[0] * sheet.shape[1])
    assert len(calls) > 1 and area < 0.75, (len(calls), area)


@pytest.mark.parametrize("amplitude, regions", [(8, True), (40, False)])
def test_key_still_on_noisy_backdrops(matte_calls, amplitude, regions):
    """Backdrop noise within the safe distance (+-8 per channel: under 16 RGB units at the still weight) is
    no content, so six crops; heavy noise makes more than 64 groups and one whole-image call. Both give
    the whole-image bytes."""
    calls, original = matte_calls
    sheet = _noisy(amplitude, seed=amplitude)
    _key_still_against_whole(sheet, original)
    if regions:
        assert len(calls) == 6 and sum(h * w for h, w in calls) < 0.5 * sheet.shape[0] * sheet.shape[1]
    else:
        assert calls == [sheet.shape[:2]]


def test_key_still_on_the_2048_synthetic_sheet(matte_calls):
    """B01's 2048 px perf sheet: 16 crops, about 56% of the area, the same bytes."""
    calls, original = matte_calls
    sheet = _synthetic_2048()
    _key_still_against_whole(sheet, original)
    area = sum(h * w for h, w in calls) / (2048 * 2048)
    assert len(calls) == 16 and 0.5 < area < 0.6, area


def test_large_pink_subjects_take_the_whole_image_call(matte_calls):
    """The exactness guard: dominance sums past 2**24 would round differently in a crop, so the whole
    sheet is matted at once; with interior despill the band's limit is constant and crops stay exact."""
    calls, original = matte_calls
    sheet = _pink()
    key = np.array([255, 0, 255], np.float32)
    np.testing.assert_array_equal(fm.soft_matte_regions(sheet, _params(False), key),
                                  original(sheet, _params(False), key))
    assert calls == [(1024, 1024)]
    calls.clear()
    np.testing.assert_array_equal(fm.soft_matte_regions(sheet, _params(True), key),
                                  original(sheet, _params(True), key))
    assert len(calls) == 4 and calls[0] != (1024, 1024)


# --------------------------------------------------------------------------- the promotion and its guard

def _b01_sheets():
    """B01's SoftRegionTests sheets: purple detail and a key hole, noise, partial alpha, a green key."""
    rng = np.random.default_rng(11)
    accents = np.array(make_magenta_sheet(3, 3, 96, fringe=True))
    accents[30:40, 30:44, :3] = (180, 60, 200)
    accents[140:150, 130:140, :3] = (255, 0, 255)
    noisy = np.array(make_magenta_sheet(2, 3, 80, margin=4))
    noisy[..., :3] = np.clip(noisy[..., :3].astype(int) + rng.integers(-6, 7, noisy[..., :3].shape), 0, 255)
    translucent = np.array(make_magenta_sheet(2, 2, 72))
    translucent[:, :20, 3] = 0
    translucent[100:110, 30:60, 3] = 120
    green = np.array(make_magenta_sheet(2, 2, 64, key=(0, 255, 0), color=(200, 60, 160)))
    sparse = np.array(make_magenta_sheet(3, 3, 256, margin=100))
    return [(accents, "magenta"), (noisy, "magenta"), (translucent, "magenta"), (green, "green"), (sparse, "magenta")]


def test_promotion_equals_b01s_copy_and_the_whole_image_call():
    """B01's sheets and settings: the promoted function, B01's copy and one whole call agree byte for byte."""
    for index, (sheet, key_name) in enumerate(_b01_sheets()):
        key, _info = fm.estimate_key(sheet, key_name)
        for interior in (False, True):
            whole = fm.soft_matte(sheet, _params(interior), key)
            np.testing.assert_array_equal(fm.soft_matte_regions(sheet, _params(interior), key), whole)
            np.testing.assert_array_equal(_oracle_b01_regions(sheet, _params(interior), key), whole)


def test_soft_matte_regions_keys_like_soft_matte():
    """No key or a declared name is estimated from the image colour, exactly as soft_matte does; Pillow
    images and RGB arrays work; an all-backdrop sheet is (0, 0, 0, 0) without any matting."""
    sheet = _b01_sheets()[0][0]
    for key in (None, "magenta", "#ff00ff", (255, 0, 255)):
        np.testing.assert_array_equal(fm.soft_matte_regions(Image.fromarray(sheet), key=key),
                                      fm.soft_matte(sheet, key=key))
    rgb = np.ascontiguousarray(sheet[..., :3])
    np.testing.assert_array_equal(fm.soft_matte_regions(rgb, fm.STILL_KEY_PARAMS, "magenta"),
                                  fm.soft_matte(rgb, fm.STILL_KEY_PARAMS, "magenta"))
    backdrop = np.zeros((96, 128, 3), np.uint8)
    backdrop[...] = (250, 4, 251)
    assert not fm.soft_matte_regions(backdrop, fm.STILL_KEY_PARAMS, "magenta").any()
    guided = fm.KeyParams(refine="guided")  # guided refinement reads the whole zone: one whole call
    np.testing.assert_array_equal(fm.soft_matte_regions(sheet, guided, "magenta"),
                                  fm.soft_matte(sheet, guided, "magenta"))


def test_soft_matte_reach_fits_the_margin_and_crops_are_local():
    """Every soft-matte step is local: an output pixel depends only on input within _soft_matte_reach (17 px).
    Matting any crop and keeping its interior beyond that reach gives the whole-image bytes."""
    assert fm._soft_matte_reach(fm.STILL_KEY_PARAMS) == fm._soft_matte_reach(fm.KeyParams()) == 17
    assert fm._soft_matte_reach(fm.STILL_KEY_PARAMS) <= fm._REGION_MARGIN == 32
    rng = np.random.default_rng(21)
    sheet = np.array(make_magenta_sheet(2, 2, 96, fringe=True))
    sheet[20:40, 20:50, :3] = (180, 60, 200)  # key-coloured material: the colour-clean band matters
    sheet[..., :3] = np.clip(sheet[..., :3].astype(int) + rng.integers(-12, 13, sheet[..., :3].shape), 0, 255)
    reach = 17
    for params in (_params(False), _params(True), fm.KeyParams()):
        whole = fm.soft_matte(sheet, params, "#ff00ff")
        for _ in range(6):
            y0, x0 = (int(v) for v in rng.integers(0, 60, 2))
            y1, x1 = y0 + int(rng.integers(2 * reach + 8, 130)), x0 + int(rng.integers(2 * reach + 8, 130))
            crop = fm.soft_matte(sheet[y0:y1, x0:x1], params, "#ff00ff")
            inner = (slice(reach if y0 else 0, (y1 - y0) - reach if y1 < sheet.shape[0] else None),
                     slice(reach if x0 else 0, (x1 - x0) - reach if x1 < sheet.shape[1] else None))
            expected = whole[y0:y1, x0:x1][inner]
            np.testing.assert_array_equal(crop[inner], expected)


def test_float32_box_sums_are_exact_only_up_to_2_pow_24():
    """The mechanism the guard covers: soft_matte's integral images accumulate in float32."""
    rng = np.random.default_rng(22)
    small = rng.integers(0, 16, (1024, 1024)).astype(np.float32)  # total about 7.9e6 < 2**24
    assert np.array_equal(fm._box_sum(small, 10), fm._box_sum(small.astype(np.float64), 10))
    large = rng.integers(0, 16, (2048, 2048)).astype(np.float32)  # total about 3.1e7 > 2**24
    assert not np.array_equal(fm._box_sum(large, 10), fm._box_sum(large.astype(np.float64), 10))


def test_float32_guard_bounds(monkeypatch):
    """_float32_sums_exact: the whole-content bound first, then the deep-content bound with per-crop checks."""
    monkeypatch.setattr(fm, "_FLOAT32_EXACT_SUM", 1000.0)
    model = fm._key_model("magenta")
    colour = np.zeros((40, 40, 3), np.uint8)
    colour[...] = (255, 0, 255)
    content = np.zeros((40, 40), bool)
    colour[5:15, 5:15] = (150, 50, 200)  # dominance 100 on 100 px: 10000 > 1000
    content[5:15, 5:15] = True
    params = fm.STILL_KEY_PARAMS  # r_c 3: deep content is the inner 4 x 4 block, 1600
    assert not fm._float32_sums_exact(colour, content, model, params, [(0, 0, 40, 40)])
    colour[7:13, 7:13] = (90, 90, 90)  # a grey centre: deep dominance 0, rim dominance 6400 > 1000
    assert fm._float32_sums_exact(colour, content, model, params, [(0, 0, 3, 3)])  # a crop without content
    assert not fm._float32_sums_exact(colour, content, model, params, [(0, 0, 40, 40)])  # a crop with the rim
    colour[5:15, 5:15] = (90, 90, 90)
    assert fm._float32_sums_exact(colour, content, model, params, [(0, 0, 40, 40)])  # whole-content bound 0


def test_key_still_hard_takes_the_legacy_thresholds():
    """B01's key_sheet passes --threshold/--edge-threshold to the binary keyer; key_still now can too."""
    sheet = np.array(make_magenta_sheet(2, 2, 48, fringe=True))
    image, info = fm.key_still(sheet, quality="hard", key="#ff00ff", threshold=60, edge_threshold=200)
    np.testing.assert_array_equal(np.asarray(image), np.asarray(fm.legacy_hard_key(sheet, 60, 200)))
    assert info["thresholds"] == {"threshold": 60, "edge_threshold": 200} and info["key_estimate"] is None
    default_image, default = fm.key_still(sheet, quality="hard")
    np.testing.assert_array_equal(np.asarray(default_image), np.asarray(fm.legacy_hard_key(sheet)))
    assert default["thresholds"] == {"threshold": 100, "edge_threshold": 150}
    assert "thresholds" not in fm.key_still(sheet, quality="soft")[1]


@pytest.mark.perf
def test_key_still_regions_speed_up_the_2048_sheet():
    """D15: key_still's soft matte step on B01's 2048 px sheet, region by region against one whole-image call
    (measured 2.06 s against 3.15 s, x1.53). key_still's key estimate, material share and matte QA run
    either way and are not timed here."""
    sheet = _synthetic_2048()
    key, _ = fm.estimate_key(sheet, "magenta")
    params = _params(fm.key_material_share(sheet, key) <= fm.KEY_MATERIAL_SHARE_MAX)

    def best(function, repeat=2):
        elapsed = math.inf
        for _ in range(repeat):
            started = time.perf_counter()
            function()
            elapsed = min(elapsed, time.perf_counter() - started)
        return elapsed

    whole = best(lambda: fm.soft_matte(sheet, params, key))
    regions = best(lambda: fm.soft_matte_regions(sheet, params, key))
    assert regions <= 0.85 * whole, (regions, whole)
