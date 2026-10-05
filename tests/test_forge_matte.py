"""Tests for shared/forge_matte.py (module A2-forge-matte).

Audit finding ids (2026-10-05) are named in the docstrings. Fixtures are
synthetic (tests/fixtures/keys/make_key_fixtures.py) except four crops of the
owner-generated Ryo clip, which are checked against their PROVENANCE.json.
The cfed170 keyers are copied verbatim below as oracles, so the equivalence
tests keep their meaning after the skills switch to forge_matte.
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import inspect
import json
import math
import sys
import time
from collections import deque
from dataclasses import fields, replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "shared" / "forge_matte.py"
VENDORED_SKILLS = ("generate2dsprite", "generate2dmap", "video2dsprite")
KEYS = ROOT / "tests" / "fixtures" / "keys"


def _local_load(name: str, path: Path):
    """Load a module by path (tests/forge_testutils.py arrives with A0)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fm = _local_load("forge_matte_under_test", CANONICAL)
fx = _local_load("forge_key_fixtures", KEYS / "make_key_fixtures.py")


def _sha256(array: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def _best_seconds(function, *args, repeat: int = 3) -> float:
    function(*args)  # warm-up: scipy import, allocator
    best = math.inf
    for _ in range(repeat):
        start = time.perf_counter()
        function(*args)
        best = min(best, time.perf_counter() - start)
    return best


def _visible_fringe(rgba: np.ndarray) -> np.ndarray:
    """Report v2 E: alpha * max(0, min(R, B) - G), the magenta a pixel adds to any neutral backdrop."""
    colour = rgba[..., :3].astype(np.float32)
    return rgba[..., 3] / 255.0 * np.maximum(0.0, np.minimum(colour[..., 0], colour[..., 2]) - colour[..., 1])


# --------------------------------------------------------------------------- cfed170 oracles (verbatim)

def _cfed170_remove_bg_magenta(img: Image.Image, threshold: int = 100, edge_threshold: int = 150) -> Image.Image:
    """skills/generate2dsprite/scripts/generate2dsprite.py:426-471 at cfed170 (the map copy is identical)."""
    pixels = img.load()
    width, height = img.size

    def dist(r: int, g: int, b: int) -> float:
        return math.sqrt((r - 255) ** 2 + g**2 + (b - 255) ** 2)

    for x in range(width):
        for y in range(height):
            r, g, b, a = pixels[x, y]
            if a == 0:
                continue
            if dist(r, g, b) < threshold:
                pixels[x, y] = (0, 0, 0, 0)

    visited: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        queue.append((x, 0))
        queue.append((x, height - 1))
    for y in range(height):
        queue.append((0, y))
        queue.append((width - 1, y))

    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or x < 0 or x >= width or y < 0 or y >= height:
            continue
        visited.add((x, y))
        r, g, b, a = pixels[x, y]
        if a == 0:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    if (x + dx, y + dy) not in visited:
                        queue.append((x + dx, y + dy))
        elif dist(r, g, b) < edge_threshold:
            pixels[x, y] = (0, 0, 0, 0)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    if (x + dx, y + dy) not in visited:
                        queue.append((x + dx, y + dy))
    return img


_CFED170_MAGENTA = np.array([255, 0, 255], dtype=np.float32)


def _cfed170_near_magenta_mask(rgb: np.ndarray, dist: float = 55.0) -> np.ndarray:
    """skills/video2dsprite/scripts/video2dsprite.py:103-111 at cfed170."""
    f = rgb.astype(np.float32)
    d = np.linalg.norm(f - _CFED170_MAGENTA, axis=2)
    r, g, b = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    pinkish = (r > 160) & (b > 160) & (g < 140) & ((r + b) / 2 - g > 40)
    return (d <= dist) | pinkish


def _cfed170_chroma_key_rgba(im: Image.Image, dist: float = 55.0, despill: float = 0.0) -> Image.Image:
    """skills/video2dsprite/scripts/video2dsprite.py:114-173 at cfed170."""
    if not math.isfinite(despill) or not 0 <= despill <= 1:
        raise ValueError("despill must be between 0 and 1")
    rgba = im.convert("RGBA")
    arr = np.array(rgba)
    rgb = arr[:, :, :3]
    h, w = rgb.shape[:2]
    key = _cfed170_near_magenta_mask(rgb, dist=dist)

    visited = np.zeros((h, w), dtype=bool)
    q: deque[tuple[int, int]] = deque()
    for y, x in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)):
        if key[y, x]:
            visited[y, x] = True
            q.append((x, y))
    for x in range(w):
        for y in (0, h - 1):
            if key[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if key[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append((x, y))

    while q:
        x, y = q.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h and not visited[ny, nx] and key[ny, nx]:
                visited[ny, nx] = True
                q.append((nx, ny))

    out = arr.copy()
    out[visited, 3] = 0

    adjacent = np.zeros_like(visited)
    adjacent[1:] |= visited[:-1]
    adjacent[:-1] |= visited[1:]
    adjacent[:, 1:] |= visited[:, :-1]
    adjacent[:, :-1] |= visited[:, 1:]
    channels = out[:, :, :3].astype(np.float32)
    spill = np.maximum(0, np.minimum(channels[:, :, 0], channels[:, :, 2]) - channels[:, :, 1])
    fringe = ~visited & adjacent & (out[:, :, 3] > 0) & (spill > 20)
    if despill and fringe.any():
        fr = out[fringe].astype(np.float32)
        amount = spill[fringe] * despill
        fr[:, 0] = np.clip(fr[:, 0] - amount, 0, 255)
        fr[:, 2] = np.clip(fr[:, 2] - amount, 0, 255)
        out[fringe] = fr.astype(np.uint8)

    out[out[:, :, 3] == 0, :3] = 0
    return Image.fromarray(out, "RGBA")


_CFED170_DESPILL_MARGIN = 12


def _cfed170_despill_chroma_edges(img: Image.Image, radius: int = 0) -> Image.Image:
    """skills/generate2dsprite/scripts/generate2dsprite.py:479-501 at cfed170 (validation inlined)."""
    if type(radius) is not int or radius not in (0, 1, 2, 3):
        raise ValueError("Despill radius must be an integer from 0 to 3.")
    if radius == 0:
        return img
    pixels = np.array(img.convert("RGBA"))
    transparent = pixels[:, :, 3] == 0
    if not transparent.any():
        return img
    boundary = Image.fromarray(transparent.astype(np.uint8) * 255)
    nearby = np.asarray(boundary.filter(ImageFilter.MaxFilter(2 * radius + 1))) > 0
    red, green, blue = (pixels[:, :, channel].astype(np.int16) for channel in range(3))
    excess = np.minimum(red, blue) - green
    affected = nearby & (pixels[:, :, 3] > 0) & (excess > _CFED170_DESPILL_MARGIN)
    pixels[:, :, 0][affected] = (red[affected] - excess[affected]).astype(np.uint8)
    pixels[:, :, 2][affected] = (blue[affected] - excess[affected]).astype(np.uint8)
    return Image.fromarray(pixels)


# --------------------------------------------------------------------------- random cases

def _random_blobs(rng: np.random.Generator, width: int, height: int) -> np.ndarray:
    """sprite-audit verify_fast_ops.py: subject colours, near-key bands, hidden RGB, partial alpha, noise."""
    arr = np.zeros((height, width, 4), np.uint8)
    arr[..., :3] = (255, 0, 255)
    arr[..., 3] = 255
    for _ in range(int(rng.integers(1, 9))):
        x0, y0 = int(rng.integers(0, width)), int(rng.integers(0, height))
        x1, y1 = min(width, x0 + int(rng.integers(1, width + 1))), min(height, y0 + int(rng.integers(1, height + 1)))
        kind = rng.random()
        if kind < 0.4:
            arr[y0:y1, x0:x1, :3] = rng.integers(0, 256, 3)
        elif kind < 0.7:
            arr[y0:y1, x0:x1, :3] = (255 - rng.integers(0, 121), rng.integers(0, 121), 255 - rng.integers(0, 121))
        elif kind < 0.85:
            arr[y0:y1, x0:x1, 3] = 0
            arr[y0:y1, x0:x1, :3] = rng.integers(0, 256, 3)
        else:
            arr[y0:y1, x0:x1, 3] = rng.integers(1, 255)
    noise = rng.random((height, width)) < 0.05
    arr[noise, :3] = rng.integers(0, 256, (int(noise.sum()), 3))
    return arr


def _random_key_blocks(rng: np.random.Generator, width: int, height: int) -> np.ndarray:
    """sprite-audit m01_duplicate_keyers.py: opaque near-magenta blocks straddling every threshold."""
    arr = np.zeros((height, width, 4), np.uint8)
    arr[..., :3] = (255, 0, 255)
    arr[..., 3] = 255
    for _ in range(int(rng.integers(1, 7))):
        x0, y0 = int(rng.integers(0, width)), int(rng.integers(0, height))
        x1, y1 = min(width, x0 + int(rng.integers(1, width + 1))), min(height, y0 + int(rng.integers(1, height + 1)))
        if rng.random() < 0.5:
            arr[y0:y1, x0:x1, :3] = (255 - rng.integers(0, 161), rng.integers(0, 161), 255 - rng.integers(0, 161))
        else:
            arr[y0:y1, x0:x1, :3] = rng.integers(0, 256, 3)
    return arr


THRESHOLDS = [(100, 150), (60, 90), (100.5, 149.7), (0, 0), (120, 110), (442, 442)]

# The deterministic 2048^2 sheet (fixtures.legacy_sheet) and the sha256 of the cfed170 keyers' output on
# it, computed once with the oracles above (22 s and 7 s): remove_bg_magenta(100, 150), chroma_key_rgba(55, 0.5).
SHEET_SHA256 = "9e96616bfde505fdda39b63242c37fcdfb1a43e55ab90cd9f4b75b8f948c732d"
SHEET_HARD_KEY_SHA256 = "bded5150ab77bc76bc1f25517f6c333f1d1253637cfab7995e4f9852699500b5"
SHEET_FLOOD_KEY_SHA256 = "f1a1e728c8a263fb7532867fd6eebe952142127c688742af68ddf4d3a528c8f1"


@functools.lru_cache(maxsize=1)
def _legacy_sheet() -> np.ndarray:
    sheet = fx.legacy_sheet()
    sheet.setflags(write=False)
    return sheet


# --------------------------------------------------------------------------- API surface and vendoring

def test_api_surface_matches_appendix_a():
    """plan Appendix A: every frozen forge_matte name exists with its documented parameters."""
    assert fm.FORGE_MATTE_API_VERSION == "1"
    assert fm.DECLARED_KEYS == {"magenta": (255, 0, 255), "green": (0, 255, 0), "blue": (0, 0, 255)}
    expected = {
        "estimate_key": ["rgb", "declared", "ring"],
        "key_material_share": ["rgb", "key"],
        "choose_key_color": ["master_rgba", "candidates"],
        "legacy_hard_key": ["rgba", "threshold", "edge_threshold"],
        "legacy_border_flood_key": ["rgba", "dist", "despill"],
        "soft_matte": ["rgb", "params", "key", "local_background", "return_debug"],
        "dominance_matte": ["rgb", "key", "lo", "hi"],
        "remove_enclosed_pockets": ["rgba", "key", "min_area", "max_dist"],
        "despill": ["rgba", "mode", "radius", "margin", "protect"],
        "unmix": ["rgb", "alpha", "key"],
        "protect_mask": ["rgb", "colors", "tol"],
        "protect_design_colours": ["rgb", "key"],
        "complement_cleanup": ["rgba", "hue_band"],
        "temporal_alpha_hysteresis": ["alphas", "band", "loop"],
        "flip_count": ["frames"],
        "matte_qa": ["rgba", "key"],
        "key_still": ["rgba", "quality", "key", "resampler_hint"],
    }
    for name, parameters in expected.items():
        signature = inspect.signature(getattr(fm, name))
        assert [p for p in signature.parameters if p in parameters] == parameters, name
    defaults = inspect.signature(fm.key_still).parameters
    assert (defaults["quality"].default, defaults["key"].default, defaults["resampler_hint"].default) == (
        "auto", "auto", "lanczos")


def test_keyparams_fields_match_v13():
    """report v2 5.1: KeyParams keeps keyer_proto_v13_frozen.py's fields, order and defaults exactly."""
    v13 = [("w_chroma", 0.2), ("t_bg", 18.0), ("t_fg", 40.0), ("keylike", 0.5), ("r_a", 3), ("r_c", 6),
           ("radius", 4), ("tau", 6.0), ("kappa", 0.6), ("temp", 0.4), ("a_lo", 0.04), ("a_hi", 0.96),
           ("unmix_from", 0.0), ("excess_slack", 3.0), ("ref_r", 10), ("speck_steps", 3), ("monotone", True),
           ("interior_despill", False), ("snap", "ramp"), ("refine", "none"), ("gf_r", 2), ("gf_eps", 0.004)]
    assert [(f.name, f.default) for f in fields(fm.KeyParams)] == v13
    assert fm.KeyParams().to_dict() == dict(v13)
    with pytest.raises(Exception):
        fm.KeyParams().w_chroma = 1.0  # frozen
    with pytest.raises(ValueError):
        fm.KeyParams(snap="soft")
    assert (fm.STILL_KEY_PARAMS.w_chroma, fm.STILL_KEY_PARAMS.r_c) == (1.0, 3)


def test_vendored_copies_match_shared():
    """plan A2-T9: the sprite, map and video skills ship byte-identical copies next to their forge_core."""
    canonical = CANONICAL.read_bytes()
    for skill in VENDORED_SKILLS:
        scripts = ROOT / "skills" / skill / "scripts"
        assert (scripts / "forge_matte.py").read_bytes() == canonical, skill
        assert (scripts / "forge_core.py").is_file(), skill


# --------------------------------------------------------------------------- A2-T1 legacy keyers

def test_legacy_hard_key_equivalence():
    """S03, S11 (verify_fast_ops, m01): 400 + 400 random cases equal cfed170 remove_bg_magenta byte for byte."""
    rng = np.random.default_rng(20261005)
    mismatches = []
    for case in range(800):
        width, height = int(rng.integers(1, 49)), int(rng.integers(1, 49))
        pixels = (_random_blobs if case < 400 else _random_key_blocks)(rng, width, height)
        threshold, edge = THRESHOLDS[case % len(THRESHOLDS)]
        expected = np.asarray(_cfed170_remove_bg_magenta(Image.fromarray(pixels, "RGBA").copy(), threshold, edge))
        if not np.array_equal(np.asarray(fm.legacy_hard_key(pixels, threshold, edge)), expected):
            mismatches.append(case)
    assert mismatches == []


def test_legacy_border_flood_equivalence():
    """S11, report v2 4.3: 200 random cases equal cfed170 video chroma_key_rgba, despill included."""
    rng = np.random.default_rng(55)
    mismatches = []
    for case in range(200):
        width, height = int(rng.integers(1, 49)), int(rng.integers(1, 49))
        pixels = (_random_blobs if case % 2 else _random_key_blocks)(rng, width, height)
        dist = (55.0, 30.0, 90.5)[case % 3]
        despill = (0.0, 0.5, 1.0, 0.37)[case % 4]
        expected = np.asarray(_cfed170_chroma_key_rgba(Image.fromarray(pixels, "RGBA"), dist, despill))
        if not np.array_equal(np.asarray(fm.legacy_border_flood_key(pixels, dist, despill)), expected):
            mismatches.append(case)
    assert mismatches == []
    with pytest.raises(ValueError):
        fm.legacy_border_flood_key(_random_key_blocks(rng, 4, 4), despill=1.2)


def test_legacy_keyers_2048_sheet_match_cfed170_output():
    """S03, MAP-13: a 2048^2 sheet with every keyer branch keys byte-identically to the cfed170 originals.

    The pure-Python originals take 22 s and 7 s on this sheet, so their output
    hashes are pinned above (computed with the oracles in this file).
    """
    sheet = _legacy_sheet()
    assert _sha256(sheet) == SHEET_SHA256
    assert _sha256(np.asarray(fm.legacy_hard_key(sheet))) == SHEET_HARD_KEY_SHA256
    assert _sha256(np.asarray(fm.legacy_border_flood_key(sheet, 55.0, 0.5))) == SHEET_FLOOD_KEY_SHA256


def test_legacy_keyers_leave_input_untouched_and_keep_hidden_rgb():
    """The original mutated its argument; the shared keyer copies. Hidden RGB under alpha 0 survives (S11)."""
    pixels = _random_blobs(np.random.default_rng(3), 32, 32)
    pixels[0:4, 0:4] = (12, 34, 56, 0)
    before = pixels.copy()
    image = Image.fromarray(pixels, "RGBA")
    keyed = np.asarray(fm.legacy_hard_key(image))
    assert np.array_equal(np.asarray(image), before) and np.array_equal(pixels, before)
    assert keyed[0, 0].tolist() == [12, 34, 56, 0]


@pytest.mark.perf
def test_legacy_keyers_2048_perf():
    """S03 (22-29 s per 2048^2 sheet in cfed170), MAP-13 (12.6 s at 1254^2): each legacy keyer <= 1 s."""
    sheet = _legacy_sheet()
    assert _best_seconds(fm.legacy_hard_key, sheet) <= 1.0
    assert _best_seconds(fm.legacy_border_flood_key, sheet, 55.0, 0.5) <= 1.0


# --------------------------------------------------------------------------- A2-T2 key estimation and choice

def _drifted_scene(key, size: int = 128) -> np.ndarray:
    """A subject on a drifted key, touching the right border ring, with +-3 noise on the backdrop."""
    rgb = np.empty((size, size, 3), np.int16)
    rgb[...] = key
    for channel in range(3):
        rgb[..., channel] += fx.hash_noise((size, size), 3, seed=40 + channel)
    rgb[30:100, 40:size] = fx.NAVY
    rgb[34:96, 44:size - 2] = fx.FILL
    return np.clip(rgb, 0, 255).astype(np.uint8)


def test_estimate_key_on_drifted_keys():
    """report v2 5.1, jev keyer_compare.json, amber-quay: drifted keys are estimated within 3 RGB."""
    cases = [(fx.grok_jpeg_pink()["rgb"], fx.GROK_KEY), (fx.codex_png_noisy()["rgb"], fx.CODEX_KEY)]
    cases += [(_drifted_scene(key), key) for key in ((202, 80, 177), (214, 83, 185), (235, 21, 175))]
    for rgb, truth in cases:
        key, info = fm.estimate_key(rgb)
        assert info["valid"] and info["source"] == "border_median", info
        assert np.abs(key - np.asarray(truth, np.float32)).max() <= 3, (truth, key)
        assert not info["use_native_alpha"]
    green = _drifted_scene((12, 230, 30))
    key, info = fm.estimate_key(green, "green")
    assert info["valid"] and np.abs(key - (12, 230, 30)).max() <= 3


def test_estimate_key_falls_back_to_native_alpha_or_declared():
    """jev process-sprite.py:57-82: no key backdrop + real transparency -> keep the native alpha."""
    key, info = fm.estimate_key(fx.native_alpha()["rgba"])
    assert info["use_native_alpha"] and info["source"] == "native_alpha" and not info["valid"]
    white = np.full((64, 64, 3), 250, np.uint8)
    white[20:40, 20:40] = fx.NAVY
    key, info = fm.estimate_key(white)
    assert not info["valid"] and info["source"] == "declared" and key.tolist() == [255.0, 0.0, 255.0]


def test_key_material_share_counts_design_purple():
    """report v2 5.1 auto rule: a navy design owns no key material; purple and pink costumes do."""
    assert fm.key_material_share(fx.plain_body(), "#ff00ff") == 0.0
    for purple in ((180, 60, 200), (151, 47, 191), (120, 40, 140), (240, 80, 190)):
        assert fm.key_material_share(fx.purple_material(purple=purple)[0], "#ff00ff") > fm.KEY_MATERIAL_SHARE_MAX


def test_choose_key_avoids_purple_costume():
    """jev keyer_compare (purple cloth eaten by magenta keys), Dusk prepare-actors.py L18: pick a non-fighting key."""
    master = np.zeros((96, 96, 4), np.uint8)
    master[8:88, 16:80] = (*fx.NAVY, 255)
    master[20:60, 16:44] = (*fx.PURPLE, 255)
    choice = fm.choose_key_color(master)
    rows = {row["key"]: row for row in choice["candidates"]}
    assert rows["magenta"]["rejected"] and choice["key"] == "green" and choice["status"] == "ok"
    assert "green" in choice["prompt_background"]
    master[20:60, 16:44] = (230, 60, 140, 255)  # pink scarf
    assert fm.choose_key_color(master)["key"] != "magenta"
    master[20:60, 16:44] = (40, 160, 60, 255)   # green tunic keeps the magenta default
    assert fm.choose_key_color(master)["key"] == "magenta"
    disk, _ = fx.outlined_disk(96, 30.0)         # an opaque master on magenta: its anti-aliased rim is ignored
    choice = fm.choose_key_color(disk)
    assert choice["key"] == "magenta" and choice["candidates"][0]["overlap_share"] == 0.0


# --------------------------------------------------------------------------- A2-T3 soft matte

def test_outlined_disk_edge_alpha_error_le_0_06():
    """report v2 P0-2: mean |alpha - true coverage| over the edge band (the rim and 1 px either side) <= 0.06.

    The anti-aliased rim alone is held to 0.06 with the still parameters; the
    video weight keys rim pixels under about 1/3 coverage as darker-key haze
    (measured 0.082 on the rim alone, 0.025 over the band).
    """
    rgb, truth = fx.outlined_disk()
    rim = (truth > 0) & (truth < 1)
    band = fm._distance_to(rim, 1) <= 1
    for params in (fm.KeyParams(), fm.STILL_KEY_PARAMS):
        alpha = fm.soft_matte(rgb, params, "#ff00ff")[..., 3] / 255.0
        assert float(np.abs(alpha - truth)[band].mean()) <= 0.06, params.w_chroma
    still = fm.soft_matte(rgb, fm.STILL_KEY_PARAMS, "#ff00ff")[..., 3] / 255.0
    assert float(np.abs(still - truth)[rim].mean()) <= 0.06


def test_420_chroma_blur_no_visible_fringe():
    """report v2 4.3 root cause: 4:2:0 chroma smears key into the subject; no pixel adds visible magenta (E > 8)."""
    rgb, truth = fx.outlined_disk()
    keyed = fm.soft_matte(fx.chroma_420(rgb))
    assert int((_visible_fringe(keyed) > 8).sum()) == 0
    assert keyed[..., 3][truth >= 0.999].min() >= 128  # no erosion (report v2 erosion_luma_px = 0)


def test_thin_line_core_alpha_ge_0_9():
    """report v2 P0-2: a 2 px dark line keeps its core (alpha >= 0.9)."""
    rgb, core = fx.thin_line()
    for params in (fm.KeyParams(), fm.STILL_KEY_PARAMS):
        assert fm.soft_matte(rgb, params, "#ff00ff")[..., 3][core].min() >= 0.9 * 255


def test_fog_and_slit_glow_alpha_zero():
    """report v2 5.1 (f101 haze): lighter/darker key haze and key glow in a 3 px slit are background."""
    rgb, fog, slit = fx.fog_and_slit()
    for params in (fm.KeyParams(), fm.STILL_KEY_PARAMS):
        for local in (False, True):
            alpha = fm.soft_matte(rgb, params, "#ff00ff", local_background=local)[..., 3]
            assert alpha[fog].max() == 0 and alpha[slit].max() == 0


@pytest.mark.parametrize("purple, params", [((120, 40, 140), fm.KeyParams()), ((180, 60, 200), fm.STILL_KEY_PARAMS),
                                            ((151, 47, 191), fm.STILL_KEY_PARAMS)])
def test_purple_material_interior_bit_identical(purple, params):
    """report v2 P0-2: the auto rule keeps interior despill off for purple material, whose interior stays exact."""
    rgb, interior = fx.purple_material(purple=purple)
    decision = fm.auto_interior_despill(rgb, [rgb])
    assert decision["interior_despill"] is False
    keyed = fm.soft_matte(rgb, replace(params, interior_despill=decision["interior_despill"]), "#ff00ff")
    assert np.array_equal(keyed[..., :3][interior], rgb[interior])
    assert (keyed[..., 3][interior] == 255).all()


def test_auto_despill_rule_on_and_off():
    """report v2 5.1: a clean master turns interior despill on and the painted key tint goes; purple turns it off."""
    frame, tint = fx.tinted_body()
    on = fm.auto_interior_despill(fx.plain_body(), [frame])
    assert on["interior_despill"] and on["reference_share"] == 0.0 and on["clip_median_share"] <= 0.005
    for enabled, limit in ((True, 4), (False, 56)):
        keyed = fm.soft_matte(frame, replace(fm.KeyParams(), interior_despill=enabled), "#ff00ff")
        colour = keyed[..., :3].astype(int)
        dominance = np.minimum(colour[..., 0], colour[..., 2]) - colour[..., 1]
        assert dominance[tint].max() <= limit and (enabled or dominance[tint].max() == 56)
    off = fm.auto_interior_despill(fx.purple_material()[0], [frame])
    assert off["interior_despill"] is False and off["reference_share"] > 0.005
    ryo_like = fm.auto_interior_despill(frames=[frame] * 40, sample=16)
    assert ryo_like["clip_sample_frames"] == sorted(set(ryo_like["clip_sample_frames"])) and ryo_like["interior_despill"]


def test_ring_with_300px_pocket_fully_keyed():
    """report v2 P0-1 (16/145 Ryo frames kept enclosed key holes): the hole is fully transparent."""
    scene = fx.enclosed_hole_ring()
    hole = scene["hole"]
    assert int(hole.sum()) >= 300
    for params in (fm.KeyParams(), fm.STILL_KEY_PARAMS):
        assert fm.soft_matte(scene["rgb"], params, "#ff00ff")[..., 3][hole].max() == 0
    legacy = np.asarray(fm.legacy_border_flood_key(scene["rgb"]))
    assert legacy[..., 3][hole].min() == 255  # the cfed170 defect, kept by the legacy keyer
    cleaned, pockets = fm.remove_enclosed_pockets(legacy, "magenta")
    assert pockets == 1 and cleaned[..., 3][hole].max() == 0
    body = scene["alpha"] >= 0.999
    assert np.array_equal(cleaned[body], legacy[body])  # only the hole and its key-mixed rim go


def test_remove_enclosed_pockets_keeps_design_colours():
    """Pocket removal clears key-coloured holes only: purple cloth and its outline stay."""
    scene = fx.purple_costume_edge()
    keyed = np.asarray(fm.legacy_border_flood_key(scene["rgb"]))
    cleaned, pockets = fm.remove_enclosed_pockets(keyed, "magenta")
    assert pockets == 0 and np.array_equal(cleaned, keyed)


def test_deterministic_twice():
    """Same input, same bytes (plan Appendix D)."""
    rgb = fx.codex_png_noisy()["rgb"]
    first = fm.soft_matte(rgb, local_background=True)
    assert np.array_equal(first, fm.soft_matte(rgb, local_background=True))
    assert np.array_equal(fm.soft_matte(rgb), fm.soft_matte(rgb))


def test_local_background_follows_drifting_backdrop():
    """report v2 P2-1: with B(x) the edges over a drifting backdrop un-mix much closer to the subject colour."""
    size = 128
    coverage = fx.coverage(lambda x, y: (x - 64.0) ** 2 + (y - 64.0) ** 2 <= 40.0 ** 2, (size, size))
    ramp = np.linspace(0.0, 1.0, size, dtype=np.float32)[None, :, None]
    backdrop = (1 - ramp) * np.array(fx.MAGENTA, np.float32) + ramp * np.array((205, 40, 195), np.float32)
    subject = np.array(fx.FILL, np.float32)
    rgb = fx.to_u8(coverage[..., None] * subject + (1 - coverage[..., None]) * backdrop)
    edge = (coverage > 0.3) & (coverage < 1)
    edge[:, :64] = False
    errors = []
    for local in (False, True):
        keyed = fm.soft_matte(rgb, key="#ff00ff", local_background=local)
        visible = edge & (keyed[..., 3] > 0)
        errors.append(float(np.abs(keyed[..., :3].astype(np.float32) - subject).max(-1)[visible].mean()))
        assert int(((keyed[..., 3] > 127) & (coverage == 0)).sum()) == 0
    assert errors[1] <= 0.6 * errors[0], errors


def test_soft_matte_green_key_and_native_alpha_bound():
    """DECLARED_KEYS: the same matte keys a green screen; RGBA input keeps its alpha as an upper bound."""
    rgb, truth = fx.outlined_disk(key=(0, 255, 0))
    keyed = fm.soft_matte(rgb, fm.STILL_KEY_PARAMS, "green")
    assert keyed[..., 3][truth == 0].max() == 0 and keyed[..., 3][truth >= 0.999].min() == 255
    colour = keyed[..., :3].astype(int)
    green_excess = colour[..., 1] - np.maximum(colour[..., 0], colour[..., 2])
    assert int(((keyed[..., 3] / 255.0 * np.maximum(green_excess, 0)) > 8).sum()) == 0
    rgba = np.dstack([fx.outlined_disk()[0], np.full((96, 96), 255, np.uint8)])
    rgba[:, :48, 3] = 0
    assert fm.soft_matte(rgba, key="#ff00ff")[:, :48, 3].max() == 0


@pytest.mark.perf
def test_soft_matte_perf_960():
    """report v2 P0-2: at most 0.8 s per 960^2 frame (frozen v13: 0.4-0.8 s depending on load)."""
    size = 960
    ys, xs = np.mgrid[0:size, 0:size]
    rgb = np.empty((size, size, 3), np.uint8)
    rgb[...] = (253, 2, 250)
    body = ((xs - 480) / 300.0) ** 2 + ((ys - 480) / 420.0) ** 2
    rgb[body <= 1.0] = fx.OUTLINE
    rgb[body <= 0.93] = fx.FILL
    for index in range(12):
        rgb[(np.abs(xs - 200 - 50 * index) < 3) & (body <= 0.93)] = fx.OUTLINE
    rgb = fx.chroma_420(rgb)
    assert _best_seconds(fm.soft_matte, rgb) <= 0.8


def _ryo_crop(name: str) -> np.ndarray:
    provenance = json.loads((KEYS / "PROVENANCE.json").read_text(encoding="utf-8"))
    entry = {row["file"]: row for row in provenance["fixtures"]}[name]
    path = KEYS / name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], name
    with Image.open(path) as image:
        pixels = np.asarray(image.convert("RGB"))
    assert _sha256(pixels) == entry["pixels_sha256"] and max(pixels.shape[:2]) <= 256
    return pixels


@pytest.mark.parametrize("name", ["ryo-f057-crop.png", "ryo-f065-crop.png", "ryo-f070-crop.png", "ryo-f089-crop.png"])
def test_ryo_crops_soft_matte_clean(name):
    """report v2 4.3 and P0-3 on real frames: no key residue, pockets or visible fringe; soft edges present.

    Interior despill follows the clip decision recorded in PROVENANCE.json
    (reference 0.000117, clip median 0.00205 in the report: on).
    """
    rgb = _ryo_crop(name)
    key, info = fm.estimate_key(rgb)
    assert info["valid"] and np.abs(key - (253, 3, 249)).max() <= 4
    keyed = fm.soft_matte(rgb, replace(fm.KeyParams(), interior_despill=True), key)
    qa = fm.matte_qa(keyed, key)
    assert qa["opaque_key_px"] == 0 and qa["enclosed_key_pockets"] == 0, qa
    assert qa["outer_ring_spill_fraction"] <= 0.01 and qa["semitransparent_fraction"] > 0, qa
    assert int((_visible_fringe(keyed) > 8).sum()) == 0


@pytest.mark.parametrize("name", ["ryo-f065-crop.png", "ryo-f089-crop.png"])
def test_ryo_enclosed_holes_survive_legacy_and_pockets_remove_them(name):
    """report v2 P0-1: the cfed170 flood keyer keeps these holes opaque; remove_enclosed_pockets clears them."""
    rgb = _ryo_crop(name)
    key, _ = fm.estimate_key(rgb)
    legacy = np.asarray(fm.legacy_border_flood_key(rgb, despill=0.5))
    before = fm.matte_qa(legacy, key)
    assert before["enclosed_key_pockets"] >= 1 and before["opaque_key_px"] >= 500
    cleaned, removed = fm.remove_enclosed_pockets(legacy, key)
    after = fm.matte_qa(cleaned, key)
    assert removed >= 1 and after["enclosed_key_pockets"] == 0 and after["opaque_key_px"] <= 2


# --------------------------------------------------------------------------- A2-T4 dominance matte

def test_dominance_no_opaque_key_pixels():
    """study-dusk-crossing key_compare.json: 0 opaque key pixels (flood fill kept 179-1052) and soft edges."""
    for scene in (fx.grok_jpeg_pink(), fx.codex_png_noisy(), fx.enclosed_hole_ring()):
        keyed = fm.dominance_matte(scene["rgb"])
        qa = fm.matte_qa(keyed, scene["key"])
        assert qa["opaque_key_px"] == 0 and qa["enclosed_key_pockets"] == 0, qa
        assert qa["semitransparent_fraction"] > 0 and qa["outer_ring_spill_fraction"] == 0.0, qa
        colour = scene["rgb"].astype(int)
        pure = (colour[..., 0] > 145) & (colour[..., 2] > 145) & (colour[..., 1] < 30)
        assert keyed[..., 3][pure].max() == 0
    green, _ = fx.outlined_disk(key=(0, 255, 0))
    keyed = fm.dominance_matte(green, "green")
    assert keyed[0, 0, 3] == 0 and keyed[48, 48, 3] == 255


def test_dominance_matches_dusk_formula():
    """Dusk process-clips.py L15-29 (magenta branch), re-derived in the test."""
    rng = np.random.default_rng(9)
    rgb = rng.integers(0, 256, (40, 40, 3)).astype(np.uint8)
    a = rgb.astype(np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    dom = np.minimum(r, b) - g
    alpha = np.clip((140 - dom) / 120, 0, 1)
    alpha[(r > 145) & (b > 145) & (g < 30)] = 0
    excess = np.maximum(0, dom)
    expected = a.copy()
    expected[..., 0] = np.where(alpha < .995, r - excess, r)
    expected[..., 2] = np.where(alpha < .995, b - excess, b)
    expected = np.dstack((np.clip(expected, 0, 255), np.rint(alpha * 255))).astype(np.uint8)
    expected[expected[..., 3] == 0, :3] = 0
    assert np.array_equal(fm.dominance_matte(rgb), expected)


# --------------------------------------------------------------------------- A2-T5 despill toolkit

@pytest.mark.parametrize("radius", [0, 1, 2, 3])
def test_edge_despill_matches_sprite_impl(radius):
    """S24, MAP-03: edge mode with margin 12 equals cfed170 despill_chroma_edges for radius 0-3."""
    rng = np.random.default_rng(100 + radius)
    for case in range(120):
        width, height = int(rng.integers(1, 41)), int(rng.integers(1, 41))
        source = (_random_blobs if case % 2 else _random_key_blocks)(rng, width, height)
        keyed = _cfed170_remove_bg_magenta(Image.fromarray(source, "RGBA").copy(), 100, 150)
        expected = np.asarray(_cfed170_despill_chroma_edges(keyed.copy(), radius).convert("RGBA"))
        actual, report = fm.despill(keyed, "edge", radius, 12)
        assert np.array_equal(actual, expected), case
        assert report["applied"] == "edge"


def test_despill_modes_and_alpha_unchanged():
    """despill off/all/auto: alpha never changes; auto picks all only without key material."""
    keyed = np.asarray(fm.legacy_hard_key(np.dstack([fx.tinted_body()[0], np.full((128, 128), 255, np.uint8)])))
    for mode in ("off", "edge", "all", "auto"):
        out, report = fm.despill(keyed, mode)
        assert np.array_equal(out[..., 3], keyed[..., 3]), mode
    assert np.array_equal(fm.despill(keyed, "off")[0], keyed)
    out, report = fm.despill(keyed, "auto")
    assert report["applied"] == "all" and report["key_material_share"] <= 0.005
    purple = np.asarray(fm.legacy_hard_key(np.dstack([fx.purple_material(purple=(120, 40, 140))[0],
                                                     np.full((128, 128), 255, np.uint8)])))
    assert fm.despill(purple, "auto")[1]["applied"] == "edge"
    with pytest.raises(ValueError):
        fm.despill(keyed, "everything")


def test_unmix_recovers_subject_colour():
    """report v2 5.2: F = (C - (1 - a) K) / a recovers the subject within 1 per channel for a >= 0.2."""
    subject = np.array([40, 160, 210], np.float32)
    key = np.array([249, 5, 249], np.float32)
    alpha = np.linspace(0.0, 1.0, 64, dtype=np.float32)[None, :].repeat(4, 0)
    rgb = fx.to_u8(alpha[..., None] * subject + (1 - alpha[..., None]) * key)
    recovered = fm.unmix(rgb, alpha, key).astype(int)
    strong = alpha >= 0.2
    assert np.abs(recovered[strong] - subject).max() <= 3
    assert recovered[alpha == 0].max() == 0


def test_protect_keeps_design_purple():
    """masa_dragon_rekey.py L196-221 (#972fbf), jev purple_edge_check: protected design colours survive keying."""
    scene = fx.purple_costume_edge()
    rgb, purple = scene["rgb"], scene["enclosed"] | scene["edge"]
    plain = fm.soft_matte(rgb, key="magenta")
    assert plain[..., 3][purple].min() < 255  # the video weight keys (180, 60, 200) out
    guard = fm.protect_mask(rgb, ["#b43cc8"])
    assert np.array_equal(guard, purple)
    kept = fm.soft_matte(rgb, key="magenta", protect=guard)
    assert (kept[..., 3][purple] == 255).all() and np.array_equal(kept[..., :3][purple], rgb[purple])
    assert kept[0, 0, 3] == 0
    rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
    despilled, report = fm.despill(rgba, "all", protect=guard)
    assert np.array_equal(despilled[purple], rgba[purple]) and report["changed_px"] > 0
    assert (fm.dominance_matte(rgb, protect=guard)[..., 3][purple] == 255).all()
    master = np.zeros((64, 64, 4), np.uint8)
    master[8:56, 8:56] = (*fx.NAVY, 255)
    master[20:44, 20:44] = (0x97, 0x2F, 0xBF, 255)
    nudged, report = fm.protect_design_colours(master, "magenta")
    assert report["colors"] and report["colors"][0]["from"] == "#972fbf" and report["colors"][0]["delta_e"] <= 0.08
    assert np.array_equal(nudged[..., 3], master[..., 3]) and np.array_equal(nudged[8:20, 8:56], master[8:20, 8:56])
    for source, survives in ((master, False), (nudged, True)):
        scene = np.empty((64, 64, 3), np.uint8)
        scene[...] = fx.MAGENTA
        scene[source[..., 3] > 0] = source[..., :3][source[..., 3] > 0]
        alpha = fm.soft_matte(scene, key="magenta")[24:40, 24:40, 3]
        assert bool((alpha == 255).all()) is survives


def test_complement_cleanup_alpha_unchanged():
    """hd2d warehouse CHARACTER-QA.md:31: green chroma complements are neutralised; alpha and browns stay."""
    pixels = np.zeros((32, 32, 4), np.uint8)
    pixels[...] = (120, 90, 60, 255)            # brown costume
    pixels[8:16, 8:16] = (60, 200, 60, 255)     # green complement
    pixels[20:24, 20:24] = (60, 200, 60, 6)     # below min_alpha: untouched
    pixels[26:30, 2:6] = (0, 0, 0, 0)
    cleaned, changed = fm.complement_cleanup(pixels)
    assert changed == 64 and np.array_equal(cleaned[..., 3], pixels[..., 3])
    assert np.array_equal(cleaned[0, 0], pixels[0, 0]) and np.array_equal(cleaned[20:24, 20:24], pixels[20:24, 20:24])
    r, g, b = (int(v) for v in cleaned[10, 10, :3])
    assert max(r, g, b) - min(r, g, b) <= 20  # near-neutral now


# --------------------------------------------------------------------------- A2-T6 temporal stability

def test_hysteresis_reduces_flips_on_noisy_sequence():
    """report v2 P2-1 (9.6 flips per pair; Ryo 57-87 measured 9.6 -> 3.5): at least 40% fewer flips."""
    sequence = fx.noisy_static_sequence()["alphas"]
    for frames in (sequence, [np.floor(a * 255 + 0.5).astype(np.uint8) for a in sequence]):
        before = fm.flip_count(frames)
        for loop in (False, True):
            filtered = fm.temporal_alpha_hysteresis(frames, loop=loop)
            assert filtered[0].dtype == frames[0].dtype and len(filtered) == len(frames)
            assert fm.flip_count(filtered) <= 0.6 * before, (frames[0].dtype, loop)


def test_hysteresis_noop_on_static():
    """A static clip comes back unchanged (float, uint8 and RGBA frames, with and without loop)."""
    alpha = fx.noisy_static_sequence()["truth"]
    for frame in (alpha, np.floor(alpha * 255 + 0.5).astype(np.uint8)):
        for loop in (False, True):
            assert all(np.array_equal(a, frame) for a in fm.temporal_alpha_hysteresis([frame] * 5, loop=loop))
    rgba = fm.soft_matte(fx.codex_png_noisy()["rgb"])
    assert all(np.array_equal(a, rgba) for a in fm.temporal_alpha_hysteresis([rgba] * 3))


def test_hysteresis_passes_decisive_changes_and_never_invents_alpha():
    """A coverage change across the whole band passes at once; alpha 0 always stays 0."""
    frames = [np.array([[0.0, 1.0, 0.7, 0.3]], np.float32), np.array([[1.0, 0.0, 0.45, 0.0]], np.float32)]
    out = fm.temporal_alpha_hysteresis(frames)
    assert np.array_equal(out[1], frames[1])
    held = fm.temporal_alpha_hysteresis([np.array([[0.9]], np.float32), np.array([[0.55]], np.float32)])
    assert abs(float(held[1][0, 0]) - 0.65) < 1e-6  # still opaque-side: at most 0.25 per frame


def test_flip_count_with_raw_excludes_motion():
    """report v2 4.3 flip metric: |da| > 0.25 counts only where the source colour barely changed."""
    alpha_a = np.zeros((8, 8), np.uint8)
    alpha_b = alpha_a.copy()
    alpha_b[:, :4] = 255
    raw_a = np.zeros((8, 8, 3), np.uint8)
    raw_b = raw_a.copy()
    raw_b[:, :2] = 200  # half the changed pixels moved for real
    assert fm.flip_count([alpha_a, alpha_b]) == 32.0
    assert fm.flip_count([alpha_a, alpha_b], raw=[raw_a, raw_b]) == 16.0
    with pytest.raises(ValueError):
        fm.flip_count([alpha_a])


# --------------------------------------------------------------------------- A2-T7 QA and key_still

def test_matte_qa_flags_residue():
    """report v2 P0-3: the binary flood key ships an opaque pocket and a magenta ring; the soft matte does not."""
    scene = fx.enclosed_hole_ring()
    legacy = fm.matte_qa(np.asarray(fm.legacy_border_flood_key(scene["rgb"])), "magenta")
    assert legacy["opaque_key_px"] > 300 and legacy["enclosed_key_pockets"] == 1
    assert legacy["outer_ring_spill_fraction"] > 0.3 and legacy["semitransparent_fraction"] == 0.0
    soft = fm.matte_qa(fm.soft_matte(scene["rgb"]), "magenta")
    assert soft["opaque_key_px"] == 0 and soft["enclosed_key_pockets"] == 0
    assert soft["outer_ring_spill_fraction"] <= 0.01 and soft["semitransparent_fraction"] > 0
    required = {"opaque_key_px", "outer_ring_spill_fraction", "semitransparent_fraction", "enclosed_key_pockets"}
    assert required <= set(soft) and json.loads(json.dumps(soft)) == soft


def test_key_still_soft_has_soft_edges_and_no_magenta():
    """S24, report v2 P2-2: the still key has soft edges and leaves no magenta (Codex and Grok keys)."""
    for scene in (fx.codex_png_noisy(), fx.grok_jpeg_pink()):
        image, info = fm.key_still(scene["rgb"], quality="soft")
        keyed = np.asarray(image)
        assert info["quality"] == "soft" and info["params"]["w_chroma"] == 1.0 and info["params"]["r_c"] == 3
        assert info["qa"]["semitransparent_fraction"] > 0 and info["qa"]["opaque_key_px"] == 0
        assert info["qa"]["outer_ring_spill_fraction"] <= 0.01
        assert int((_visible_fringe(keyed) > 8).sum()) == 0
        assert np.abs(np.asarray(info["key"]) - scene["key"]).max() <= 3


def test_key_still_auto_modes():
    """plan Appendix H: auto is soft for lanczos, hard (legacy, binary) for nearest; native alpha passes through."""
    rgb = fx.codex_png_noisy()["rgb"]
    assert fm.key_still(rgb)[1]["quality"] == "soft"
    hard_image, hard = fm.key_still(rgb, resampler_hint="nearest")
    assert hard["quality"] == "hard" and hard["qa"]["semitransparent_fraction"] == 0.0
    assert np.array_equal(np.asarray(hard_image), np.asarray(fm.legacy_hard_key(rgb)))
    assert fm.key_still(rgb, quality="dominance")[1]["qa"]["opaque_key_px"] == 0
    native = fx.native_alpha()["rgba"]
    image, info = fm.key_still(native)
    assert info["quality"] == "native_alpha" and np.array_equal(np.asarray(image), native)
    with pytest.raises(ValueError):
        fm.key_still(rgb, quality="hard", key="green")
    with pytest.raises(ValueError):
        fm.key_still(rgb, quality="best")
