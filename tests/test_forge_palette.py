"""Tests for shared/forge_palette.py (module B04-palette-pixel, tasks B04-T1 and B04-T2).

Every image here is synthetic and seeded. The canonical module is tested; the
vendored copy in generate2dsprite is kept identical by tests/test_vendored_sync.py.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image

from forge_testutils import REPO_ROOT, assert_valid_contract, load_script, load_shared

fp = load_shared("forge_palette")
fc = load_shared("forge_core")


# --------------------------------------------------------------------------- synthetic art

def shaded_sprite(seed: int = 0, size: int = 40, *, noise: float = 0.0, flip_hue: bool = False) -> np.ndarray:
    """A round, outlined, shaded body with a soft-free binary alpha edge."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    centre = (size - 1) / 2
    radius = np.hypot(x - centre, (y - centre) * 1.2)
    shade = np.clip((x + y) / (2.0 * size), 0.0, 1.0)
    rgb = np.stack([70 + 160 * shade, 50 + 90 * (1 - shade), 40 + 40 * np.sin(x / 4.0)], axis=-1)
    if flip_hue:
        rgb = rgb[..., ::-1]
    rgb[(radius >= size * 0.36) & (radius < size * 0.42)] = (24, 20, 36)
    if noise:
        rgb = rgb + rng.normal(0.0, noise, rgb.shape)
    alpha = np.where(radius < size * 0.42, 255, 0)
    pixels = np.dstack([np.clip(np.floor(rgb + 0.5), 0, 255), alpha]).astype(np.uint8)
    pixels[alpha == 0] = 0
    return pixels


def soft_sprite(size: int = 48) -> np.ndarray:
    """Float RGBA (0..255) with smooth colour ramps and a soft alpha edge band (for clips)."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    centre = (size - 1) / 2
    radius = np.hypot(x - centre, (y - centre) * 1.15)
    shade = np.clip((x + y) / (2.0 * size), 0.0, 1.0)
    out = np.zeros((size, size, 4))
    out[..., 0] = 60 + 170 * shade
    out[..., 1] = 90 + 110 * (1 - shade)
    out[..., 2] = 140 + 60 * np.sin(x / 5.0)
    out[..., 3] = np.clip((size * 0.42 - radius) * 0.8 + 0.5, 0.0, 1.0) * 255
    return out


def noisy_clip(seed: int, frames: int = 12, sigma: float = 3.0) -> list[np.ndarray]:
    """A static sprite under per-frame noise (decoded-video-like), RGB sigma ``sigma`` and alpha sigma 10."""
    base = soft_sprite()
    rng = np.random.default_rng(seed)
    clip = []
    for _ in range(frames):
        frame = base.copy()
        frame[..., :3] += rng.normal(0.0, sigma, frame[..., :3].shape)
        frame[..., 3] += rng.normal(0.0, 10.0, frame[..., 3].shape) * (frame[..., 3] > 0)
        clip.append(np.clip(np.floor(frame + 0.5), 0, 255).astype(np.uint8))
    return clip


def png_bytes(rgba: np.ndarray, folder) -> bytes:
    """The bytes forge_core.save_png writes for ``rgba`` (what a CLI would publish)."""
    target = folder / "encoded.png"
    fc.save_png(rgba, target)
    data = target.read_bytes()
    target.unlink()
    return data


def png_chunks(data: bytes) -> dict[str, bytes]:
    chunks, offset = {}, 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8].decode("ascii")
        chunks.setdefault(kind, data[offset + 8:offset + 8 + length])
        offset += 12 + length
    return chunks


# --------------------------------------------------------------------------- OKLab

@pytest.mark.parametrize("shape", [(1, 3), (2, 3), (7, 3), (4096, 3), (24, 17, 3), (5, 1, 3)])
def test_to_oklab_matches_forge_matte_bit_for_bit(shape):
    """forge_matte keeps a private _to_oklab; the two must be interchangeable at integration."""
    forge_matte = load_shared("forge_matte")
    colours = np.random.default_rng(len(shape) * 1000 + shape[0]).integers(0, 256, shape).astype(np.uint8)
    ours = fp.to_oklab(colours)
    assert ours.dtype == np.float32
    assert np.array_equal(ours, forge_matte._to_oklab(colours))


def test_from_oklab_round_trips_8bit_colours():
    levels = np.arange(0, 256, 3, dtype=np.uint8)
    grid = np.stack(np.meshgrid(levels, levels, levels, indexing="ij"), axis=-1).reshape(-1, 3)
    extremes = np.array([[0, 0, 0], [255, 255, 255], [255, 0, 0], [0, 255, 0], [0, 0, 255], [1, 0, 0]], np.uint8)
    for colours in (grid, extremes, np.random.default_rng(5).integers(0, 256, (50000, 3)).astype(np.uint8)):
        back = fp.from_oklab(fp.to_oklab(colours))
        assert back.dtype == np.uint8
        assert np.array_equal(back, colours)
    white = fp.to_oklab(np.array([255, 255, 255], np.uint8))
    assert abs(float(white[0]) - 1.0) < 1e-4 and abs(float(white[1])) < 1e-4 and abs(float(white[2])) < 1e-4


def test_from_oklab_clips_out_of_gamut():
    assert fp.from_oklab([[1.4, 0.0, 0.0]]).tolist() == [[255, 255, 255]]
    assert fp.from_oklab([[-0.2, 0.0, 0.0]]).tolist() == [[0, 0, 0]]
    vivid = fp.from_oklab([[0.6, 0.4, 0.0]])  # far outside sRGB
    assert vivid.dtype == np.uint8 and vivid.shape == (1, 3)


# --------------------------------------------------------------------------- build_palette

def test_kmeans_deterministic(tmp_path):
    """B04-T1: same samples and seed -> the same palette, whatever the sample order or process."""
    frames = [shaded_sprite(seed, noise=4.0) for seed in range(3)]
    first = fp.build_palette(frames, 12, seed=3)
    assert first == fp.build_palette(frames, 12, seed=3)
    pixels = np.concatenate([frame.reshape(-1, 4) for frame in frames])
    shuffled = pixels[np.random.default_rng(9).permutation(len(pixels))]
    assert fp.build_palette(shuffled, 12, seed=3).colors == first.colors
    assert len(first) == 12 and len(set(first.colors)) == 12
    lightness = fp.to_oklab(first.rgb)[:, 0]
    assert np.all(np.diff(lightness) >= 0), "learned colours are ordered darkest first"
    script = ("import sys, json, numpy as np; sys.path.insert(0, sys.argv[1]); import forge_palette as fp; "
              "pixels = np.load(sys.argv[2]); print(json.dumps(fp.build_palette(pixels, 12, seed=3).hex_colors))")
    samples = tmp_path / "samples.npy"
    np.save(samples, shuffled)
    outputs = set()
    for hash_seed in ("1", "2"):
        result = subprocess.run([sys.executable, "-c", script, str(REPO_ROOT / "shared"), str(samples)],
                                capture_output=True, text=True, timeout=120,
                                env={**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONDONTWRITEBYTECODE": "1"})
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout.strip())
    assert outputs == {json.dumps(first.hex_colors)}, "a fresh process gives the same palette"


def test_build_palette_is_exact_for_art_with_few_colours():
    art = np.zeros((8, 8, 4), np.uint8)
    colours = [(10, 20, 30), (200, 40, 40), (40, 200, 40), (250, 250, 240), (90, 90, 160)]
    for row, colour in enumerate(colours):
        art[row, :, :3] = colour
        art[row, :, 3] = 255
    art[6:, :, 3] = 0
    art[6:, :, :3] = (255, 0, 255)  # hidden under alpha 0: never sampled
    palette = fp.build_palette(art, 8)
    assert sorted(palette.colors) == sorted(colours)
    assert palette.transparent_index == 255 and not palette.locked
    assert palette.reserved == (False,) * 5


def test_build_palette_reserved_colours_and_coverage_skipping():
    art = shaded_sprite(1, noise=2.0)
    reserved = [(24, 20, 36), (255, 255, 255)]
    palette = fp.build_palette(art, 8, reserved=["#181424", "#ffffff"])
    assert palette.colors[:2] == tuple(reserved)
    assert palette.reserved == (True, True) + (False,) * (len(palette) - 2)
    learned = fp.to_oklab(palette.rgb[2:]).astype(np.float64)
    anchors = fp.to_oklab(np.array(reserved, np.uint8)).astype(np.float64)
    distance = np.sqrt(((learned[:, None] - anchors[None]) ** 2).sum(-1)).min(1)
    assert distance.min() > fp.COVER_DELTA_E, "no learned colour is spent on what an outline colour already covers"
    plain = fp.build_palette(art, 8)
    plain_lab = fp.to_oklab(plain.rgb).astype(np.float64)
    assert np.sqrt(((plain_lab - anchors[0]) ** 2).sum(-1)).min() < fp.COVER_DELTA_E, \
        "without the reserved outline, k-means spends a colour on it"
    with pytest.raises(fp.PaletteError):
        fp.build_palette(art, 1, reserved=reserved)


def test_build_palette_weights_steer_the_colours():
    warm, cool = shaded_sprite(2, noise=3.0), shaded_sprite(3, noise=3.0, flip_hue=True)

    def warm_share(palette):
        lab = fp.to_oklab(palette.rgb).astype(np.float64)
        return int((lab[:, 2] > 0.02).sum())  # yellowish/orange colours

    warm_heavy = fp.build_palette([warm, cool], 10, weights=[10.0, 1.0])
    cool_heavy = fp.build_palette([warm, cool], 10, weights=[1.0, 10.0])
    assert warm_share(warm_heavy) > warm_share(cool_heavy)
    only_warm = fp.build_palette([warm, cool], 10, weights=[1.0, 0.0])
    assert only_warm == fp.build_palette([warm], 10)
    with pytest.raises(fp.PaletteError):
        fp.build_palette([warm, cool], 10, weights=[1.0])


def test_build_palette_extends_a_locked_base_without_moving_it():
    base = fp.build_palette(shaded_sprite(4), 6).replace(locked=True, name="hero")
    grown = fp.build_palette(shaded_sprite(5, flip_hue=True), 10, reserved=base)
    assert grown.colors[:len(base)] == base.colors
    assert grown.reserved[:len(base)] == (True,) * len(base)
    assert len(grown) > len(base) and grown.transparent_index == 255 and grown.name == "hero"
    with pytest.raises(fp.PaletteError):
        fp.build_palette(shaded_sprite(5), len(base) - 1, reserved=base)
    compact = base.replace(transparent_index=len(base))
    with pytest.raises(fp.PaletteError, match="transparent index"):
        fp.build_palette(shaded_sprite(5, flip_hue=True), 10, reserved=compact)


def test_build_palette_refuses_empty_samples_and_bad_k():
    empty = np.zeros((4, 4, 4), np.uint8)
    with pytest.raises(fp.PaletteError, match="no samples"):
        fp.build_palette(empty, 4)
    for k in (0, 257, 2.5, True):
        with pytest.raises(fp.PaletteError):
            fp.build_palette(shaded_sprite(), k)


def test_build_palette_bins_many_colours_quickly():
    rng = np.random.default_rng(11)
    photo = rng.integers(0, 256, (256, 256, 3)).astype(np.uint8)  # ~65k unique colours: binned path
    palette = fp.build_palette(photo, 32, max_samples=4096)
    assert len(palette) == 32
    assert palette == fp.build_palette(photo, 32, max_samples=4096)


# --------------------------------------------------------------------------- lookup and index maps

def test_nearest_index_exact_colours_ties_and_transparent_slot():
    palette = fp.Palette(((0, 0, 0), (255, 255, 255), (255, 255, 255), (200, 0, 0)), transparent_index=3)
    colours = np.array([[0, 0, 0], [255, 255, 255], [250, 250, 250], [210, 0, 0]], np.uint8)
    index = fp.nearest_index(colours, palette)
    assert index.dtype == np.uint8
    assert index[:3].tolist() == [0, 1, 1], "exact colours keep their lowest index"
    assert index[3] != 3, "the transparent slot's placeholder colour is never chosen"
    image = shaded_sprite(6)
    assert fp.nearest_index(image, palette).shape == image.shape[:2]


def test_quantize_image_alpha_threshold_and_render_round_trip():
    palette = fp.Palette(((20, 20, 20), (220, 60, 60)))
    image = np.zeros((4, 4, 4), np.uint8)
    image[..., :3] = (210, 70, 60)
    image[..., 3] = [[0, 100, 127, 128]] * 4
    index = fp.quantize_image(image, palette)
    assert index[0].tolist() == [255, 255, 255, 1]
    rgba = fp.render_indices(index, palette)
    assert rgba[0, 3].tolist() == [220, 60, 60, 255] and rgba[0, 0].tolist() == [0, 0, 0, 0]
    no_slot = palette.replace(transparent_index=None)
    with pytest.raises(fp.PaletteError, match="transparent"):
        fp.quantize_image(image, no_slot)
    with pytest.raises(fp.PaletteError):
        fp.render_indices(np.array([[7]]), palette)


def test_cleanup_orphans_and_cleanup_alpha():
    index = np.full((5, 5), 1, np.uint8)
    index[2, 2] = 2  # an orphan inside colour 1
    cleaned = fp.cleanup_orphans(index)
    assert cleaned[2, 2] == 1
    transparent = np.full((5, 5), 255, np.uint8)
    transparent[2, 2] = 3
    assert fp.cleanup_orphans(transparent)[2, 2] == 3, "a lone pixel on transparency is cleanup_alpha's job"
    speck = np.full((6, 6), 255, np.uint8)
    speck[1, 1] = 4                    # lone speck
    speck[3:6, 3:6] = 5
    speck[4, 4] = 255                  # pinhole
    result = fp.cleanup_alpha(speck)
    assert result[1, 1] == 255 and result[4, 4] == 5
    as_list = fp.cleanup_alpha([speck, speck])
    assert isinstance(as_list, list) and all(np.array_equal(item, result) for item in as_list)


# --------------------------------------------------------------------------- indexed PNG

def test_indexed_roundtrip_exact(tmp_path):
    """B04-T1: indices, palette and transparency survive an indexed PNG byte for byte."""
    frames = [shaded_sprite(seed, noise=3.0) for seed in range(4)]
    sheet = np.concatenate(frames, axis=1)
    palette = fp.build_palette(sheet, 16)
    index = fp.quantize_image(sheet, palette)
    target = tmp_path / "sheet.png"
    fp.save_indexed_png(index, palette, target)
    with Image.open(target) as decoded:
        assert decoded.mode == "P"
        assert np.array_equal(np.array(decoded), index)
        entries = np.array(decoded.getpalette(), np.uint8).reshape(-1, 3)
    assert np.array_equal(entries[:len(palette)], palette.rgb) and len(entries) == 256
    chunks = png_chunks(target.read_bytes())
    assert set(chunks) == {"IHDR", "PLTE", "tRNS", "IDAT", "IEND"}, "no metadata chunks"
    assert chunks["tRNS"] == bytes([255] * 255 + [0])
    loaded, info = fc.load_rgba(target)
    assert np.array_equal(np.asarray(loaded), fp.render_indices(index, palette))
    assert info["source_mode"] == "P"
    used = {palette.colors[i] for i in np.unique(index[index != 255]).tolist()}
    assert set(fp.read_palette(target).colors) == used, "a sprite PNG yields the colours it uses"
    rgba_size = len(png_bytes(fp.render_indices(index, palette), tmp_path))
    assert target.stat().st_size < rgba_size, "the indexed sheet is smaller than its RGBA PNG"
    compact = palette.replace(transparent_index=len(palette))
    compact_index = np.where(index == 255, len(palette), index).astype(np.uint8)
    small = tmp_path / "compact.png"
    fp.save_indexed_png(compact_index, compact, small, transparent_index=len(palette))
    assert small.read_bytes()[24] == 8 and png_chunks(small.read_bytes())["tRNS"] == bytes([255] * 16 + [0])
    tiny = fp.Palette(((0, 0, 0), (255, 255, 255), (255, 0, 0)), transparent_index=3)
    four_bit = tmp_path / "tiny.png"
    fp.save_indexed_png(np.array([[0, 1], [2, 3]], np.uint8), tiny, four_bit, transparent_index=3)
    assert four_bit.read_bytes()[24] == 2, "a 4-entry PLTE is written at 2 bits per pixel"
    first = tmp_path / "again.png"
    fp.save_indexed_png(index, palette, first)
    assert first.read_bytes() == target.read_bytes(), "byte-deterministic"


def test_save_indexed_png_refuses_ambiguous_slots(tmp_path):
    palette = fp.Palette(((0, 0, 0), (255, 255, 255)), transparent_index=0)
    index = np.array([[0, 1]], np.uint8)
    with pytest.raises(fp.PaletteError, match="transparent_index=0"):
        fp.save_indexed_png(index, palette, tmp_path / "a.png")  # default 255 vs the palette's slot 0
    fp.save_indexed_png(index, palette, tmp_path / "b.png", transparent_index=0)
    decoded = np.asarray(fc.load_rgba(tmp_path / "b.png")[0])
    assert decoded[0, 0, 3] == 0 and decoded[0, 1].tolist() == [255, 255, 255, 255]
    with pytest.raises(fp.PaletteError, match="neither"):
        fp.save_indexed_png(np.array([[2]], np.uint8), fp.Palette(((1, 2, 3), (4, 5, 6))), tmp_path / "c.png")
    with pytest.raises(fp.PaletteError, match="hide"):
        fp.save_indexed_png(index, fp.Palette(((1, 2, 3), (4, 5, 6)), transparent_index=None),
                            tmp_path / "d.png", transparent_index=1)


# --------------------------------------------------------------------------- palette files

def test_palette_files_round_trip_and_never_clobber(tmp_path):
    palette = fp.Palette(((26, 28, 44), (93, 39, 93), (255, 205, 117)), names=("ink", None, "high light"),
                         reserved=(True, False, False), transparent_index=255, locked=True,
                         source="test palette", name="Test")
    for fmt in fp.PALETTE_FORMATS:
        path = tmp_path / f"p.{fmt}"
        fp.write_palette(palette, path)
        back = fp.read_palette(path)
        assert back.colors == palette.colors, fmt
        with pytest.raises(FileExistsError):
            fp.write_palette(palette, path)
    assert fp.read_palette(tmp_path / "p.json") == palette
    gpl = fp.read_palette(tmp_path / "p.gpl")
    assert gpl.names == ("ink", None, "high light") and gpl.name == "Test"
    assert (tmp_path / "p.hex").read_text(encoding="utf-8") == "1a1c2c\n5d275d\nffcd75\n"
    assert (tmp_path / "p.pal").read_bytes().startswith(b"JASC-PAL\r\n0100\r\n3\r\n")
    document = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert_valid_contract(document, "sprite", "palette_v1", skill="generate2dsprite")
    codeart = load_script("codeart2d", "codeart_core")
    for fmt in ("json", "gpl", "hex"):
        parsed = codeart.parse_palette(tmp_path / f"p.{fmt}")
        assert [colour[:3] for colour in parsed.resolve().values()] == list(palette.colors), fmt


def test_read_palette_accepts_common_shapes_and_refuses_others(tmp_path):
    (tmp_path / "codeart.json").write_text(json.dumps({"colors": {"k": "#1a1c2c", "g": "#38b764"}}), "utf-8")
    assert fp.read_palette(tmp_path / "codeart.json").names == ("k", "g")
    (tmp_path / "list.json").write_text(json.dumps(["#000", "#ffffff"]), "utf-8")
    assert fp.read_palette(tmp_path / "list.json").colors == ((0, 0, 0), (255, 255, 255))
    (tmp_path / "spec.json").write_text(json.dumps({"schema": "codeart2d.pixelspec.v1",
                                                    "palette": {"k": "#1a1c2c"}}), "utf-8")
    assert fp.read_palette(tmp_path / "spec.json").colors == ((26, 28, 44),)
    (tmp_path / "lock.json").write_text(json.dumps({"schema": fp.PALETTE_LOCK_SCHEMA, "colors": ["#000000"]}), "utf-8")
    (tmp_path / "translucent.json").write_text(json.dumps(["#ff000080"]), "utf-8")
    (tmp_path / "bad.gpl").write_text("not a palette\n", "utf-8")
    (tmp_path / "bad.hex").write_text("12345\n", "utf-8")
    for name in ("lock.json", "translucent.json", "bad.gpl", "bad.hex", "missing.json"):
        with pytest.raises(fp.PaletteError):
            fp.read_palette(tmp_path / name)
    swatch = np.zeros((1, 4, 4), np.uint8)
    swatch[0, :, 3] = 255
    swatch[0, :, :3] = [(9, 9, 9), (200, 0, 0), (9, 9, 9), (0, 0, 200)]
    Image.fromarray(swatch).save(tmp_path / "lospec.png")
    assert fp.read_palette(tmp_path / "lospec.png").colors == ((9, 9, 9), (200, 0, 0), (0, 0, 200))


# --------------------------------------------------------------------------- fit, lock

def test_fit_report_measures_delta_e_and_off_palette_pixels():
    palette = fp.Palette(((0, 0, 0), (255, 255, 255)))
    image = np.zeros((2, 2, 4), np.uint8)
    image[..., 3] = 255
    image[0, 0, :3] = (255, 255, 255)
    image[0, 1, :3] = (250, 250, 250)
    image[1, 1, 3] = 100
    report = fp.fit_report(image, palette)
    expected = float(np.sqrt(((fp.to_oklab(np.array([250, 250, 250], np.uint8)).astype(np.float64)
                               - fp.to_oklab(np.array([255, 255, 255], np.uint8))) ** 2).sum()))
    assert report["measured_px"] == 4 and report["off_palette_px"] == 1 and report["partial_alpha_px"] == 1
    assert report["colors"] == 3 and report["palette_colors_used"] == 2
    assert report["max_delta_e"] == round(expected, 6) and report["p95_delta_e"] == round(expected, 6)
    assert report["mean_delta_e"] == round(expected / 4, 6)
    assert fp.fit_report(image, palette, alpha_threshold=128)["measured_px"] == 3
    assert fp.fit_report(np.zeros((2, 2, 4), np.uint8), palette)["max_delta_e"] == 0.0


def _gradient_art(start, stop, width=48, height=6):
    ramp = np.linspace(0.0, 1.0, width)[None, :, None]
    rgb = np.array(start, np.float64) + (np.array(stop, np.float64) - np.array(start, np.float64)) * ramp
    image = np.zeros((height, width, 4), np.uint8)
    image[..., :3] = np.floor(np.repeat(rgb, height, axis=0) + 0.5)
    image[..., 3] = 255
    return image


def test_lock_keeps_prior_outputs_byte_identical(tmp_path):
    """B04-T1: sources recorded in a lock re-quantize to the same bytes after the palette grows."""
    hero = _gradient_art((40, 30, 90), (230, 200, 120))
    hero_path = tmp_path / "hero.png"
    fc.save_png(hero, hero_path)
    palette = fp.build_palette(hero, 4)
    palette_path = tmp_path / "palette.json"
    fp.write_palette(palette.replace(locked=True), palette_path)
    before = png_bytes(fp.render_indices(fp.quantize_image(hero, palette), palette), tmp_path)
    lock = fp.lock_palette(palette_path, [hero_path], base=tmp_path)
    assert lock["colors"] == list(palette.hex_colors) and lock["sources"][0]["path"] == "hero.png"
    assert lock["palette"] == fp.file_ref(palette_path, tmp_path)
    assert lock["sources"][0]["sha256"] == fc.sha256_file(hero_path)
    assert_valid_contract(lock, "sprite", "palette_lock_v1", skill="generate2dsprite")
    # New art brings colours that sit between the hero's palette colours.
    newcomer = _gradient_art((90, 60, 100), (180, 140, 110))
    grown = fp.build_palette(newcomer, 8, reserved=fp.read_palette(palette_path))
    assert grown.colors[:4] == palette.colors and len(grown) > 4
    unlocked = png_bytes(fp.render_indices(fp.quantize_image(hero, grown), grown), tmp_path)
    assert unlocked != before, "without the lock the hero would pick up the new colours"
    view = fp.lock_view(grown, lock)
    assert fc.sha256_file(hero_path) in fp.locked_sources(lock)
    locked = png_bytes(fp.render_indices(fp.quantize_image(hero, view), view), tmp_path)
    assert locked == before
    assert fp.quantize_image(hero, view).max() < len(palette), "locked sources use only the locked indices"


def test_lock_view_refuses_a_palette_that_moved(tmp_path):
    palette = fp.Palette(((10, 10, 10), (200, 200, 200)))
    palette_path = tmp_path / "palette.json"
    fp.write_palette(palette, palette_path)
    image_path = tmp_path / "a.png"
    fc.save_png(shaded_sprite(), image_path)
    lock = fp.lock_palette(palette_path, [image_path])
    with pytest.raises(fp.PaletteError, match="index 1"):
        fp.lock_view(fp.Palette(((10, 10, 10), (201, 200, 200))), lock)
    with pytest.raises(fp.PaletteError, match="transparent index"):
        fp.lock_view(palette.replace(transparent_index=7), lock)
    with pytest.raises(fp.PaletteError):
        fp.lock_view(fp.Palette(((10, 10, 10),)), lock)
    with pytest.raises(fp.PaletteError):
        fp.lock_palette(palette, [image_path])  # needs the file to bind its sha256
    with pytest.raises(fp.PaletteError):
        fp.lock_palette(palette_path, [])


def test_file_ref_on_another_drive_records_the_file_name(tmp_path, monkeypatch):
    target = tmp_path / "x.png"
    target.write_bytes(b"abc")
    monkeypatch.setattr(fp.forge_core, "portable_path", lambda path, base: "Z:/elsewhere/x.png")
    assert fp.file_ref(target, tmp_path)["path"] == "x.png"


def test_manifest_path_and_file_ref_are_the_forge_core_promotions(tmp_path):
    """B04 request 1 (D30): the helpers moved to forge_core 1.1; the public forge_palette names stay as aliases."""
    assert fp.manifest_path is fp.forge_core.manifest_path and fp.file_ref is fp.forge_core.file_ref
    (tmp_path / "sub").mkdir()
    target = tmp_path / "sub" / "x.png"
    target.write_bytes(b"abc")
    assert fp.manifest_path(target, tmp_path) == "sub/x.png"
    assert fp.file_ref(target, tmp_path) == {"path": "sub/x.png", "sha256": fc.sha256_bytes(b"abc"), "bytes": 3}


# --------------------------------------------------------------------------- sequences (B04-T2)

def _plain(frames, palette):
    """Per-frame nearest quantizing with the same cleanup the sequence quantizer uses."""
    return [fp.cleanup_alpha(fp.cleanup_orphans(fp.quantize_image(frame, palette), 1)) for frame in frames]


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_hysteresis_cuts_noise_flips(seed):
    """B04-T2 (game-opus55 exp_hysteresis: -45%): noise flips fall by at least 40% at decoded-video noise."""
    clip = noisy_clip(seed)
    palette = fp.build_palette(clip, 16)
    plain = fp.flip_stats(_plain(clip, palette), clip)
    hysteresis = fp.flip_stats(fp.quantize_sequence(clip, palette, loop=False), clip)
    assert plain["noise_flip"] > 0.05
    assert hysteresis["noise_flip"] <= 0.6 * plain["noise_flip"]
    assert hysteresis["alpha_flip"] <= plain["alpha_flip"]


# Opt-in bench on the real clip of the -45% figure (plan study-game-opus55/exp_hysteresis.py). It runs only with
# -m bench and reads the owner's game folder in place; nothing is copied into this repository.
OPUS55_CLIP, OPUS55_SEGMENT_FRAMES = "a_homura_idle", 24  # the study's clip and its frame cap (segment 44:92:2)


def _opus55_root():
    """FORGE_BENCH_OPUS55, else D:/chain/game-opus55; None when its pixelate tool or palette is missing."""
    from pathlib import Path

    root = Path(os.environ.get("FORGE_BENCH_OPUS55") or "D:/chain/game-opus55")
    needed = (root / "tools" / "pixelate.py", root / "tools" / "assets.json", root / "art" / "proc" / "manifest.json")
    return root if all(path.is_file() for path in needed) else None


def _opus55_study_frames(root):
    """The study's frames, prepared by the game's own pixelate.prep_vframes, read only.

    Like exp_hysteresis.py, the tool's source is executed from text (never imported from its folder, so no
    bytecode cache is written there) with its one directory creation removed; prep_vframes only reads the
    raw video frames. Returns the game module, the (rgb float 0..255, alpha 0..1) frames and its palette."""
    import types

    source = (root / "tools" / "pixelate.py").read_text(encoding="utf-8")
    assert source.count("os.makedirs(PROC, exist_ok=True)") == 1, "pixelate.py changed: review the bench first"
    game = types.ModuleType("forge_bench_pixelate_ro")
    game.__file__ = str(root / "tools" / "pixelate.py")  # ROOT and RAW resolve to the game's own art
    exec(compile(source.replace("os.makedirs(PROC, exist_ok=True)", "pass  # read-only bench"),
                 "forge_bench_pixelate_ro", "exec"), game.__dict__)
    spec = dict(json.loads((root / "tools" / "assets.json").read_text(encoding="utf-8"))[OPUS55_CLIP])
    first, last, *rest = spec["segments"][0]
    step = rest[0] if rest else 1
    spec["segments"] = [[first, min(last, first + OPUS55_SEGMENT_FRAMES * step), step]]
    frames = game.prep_vframes(spec, OPUS55_CLIP)
    manifest = json.loads((root / "art" / "proc" / "manifest.json").read_text(encoding="utf-8"))
    return game, frames, [game.hex2rgb(value) for value in manifest["palette"]]


@pytest.mark.bench
def test_bench_hysteresis_on_the_game_opus55_clip():
    """B04-T2 on the real clip (opt-in, -m bench): the game-opus55 a_homura_idle study clip (25 frames of
    72x63 from its own video pipeline, 255-colour shipped palette). Harness check: the game's quantizer on the
    float frames reproduces the study's -45.1% noise flips. forge_palette on the 8-bit RGBA frames (what
    palette_tool quantize-seq reads from PNGs) must give the game's index maps byte for byte (study protocol:
    unseeded, consecutive pairs) and cut noise flips by at least 40% against per-frame nearest quantizing with
    the same cleanup. Measured at integration on 2026-10-05: -46.4% (study protocol), -48.1% seeded as a
    loop, -48.9% counting the wrap pair; the study measured -45.1% on the float frames."""
    root = _opus55_root()
    if root is None:
        pytest.skip("set FORGE_BENCH_OPUS55 to the game-opus55 folder (tools/pixelate.py, art/proc/manifest.json)")
    game, frames, colours = _opus55_study_frames(root)
    assert len(frames) == 25 and frames[0][1].shape == (63, 72)
    game_lab = game.to_oklab(np.array(colours, np.float32)).astype(np.float32)

    def game_quantize(clip, hysteresis):
        maps, previous = [], None
        for rgb, alpha in clip:
            if hysteresis:
                index = game.cleanup_alpha(game.cleanup(game.quantize_seq(rgb, alpha, game_lab, previous, 0.0004), 1))
            else:
                index = game.cleanup(game.quantize(rgb, alpha, game_lab, 0.0), 1)
            maps.append(index)
            previous = index
        return maps

    def study_noise_flip(maps, clip):
        labs = [game.to_oklab(rgb).astype(np.float32) for rgb, _alpha in clip]
        flips = []
        for second in range(1, len(maps)):
            both = (maps[second - 1] != game.TRANSP) & (maps[second] != game.TRANSP)
            still = both & (np.sqrt(((labs[second] - labs[second - 1]) ** 2).sum(-1)) < 0.02)
            flips.append((still & (maps[second - 1] != maps[second])).sum() / max(1, still.sum()))
        return float(np.mean(flips))

    study = 1 - study_noise_flip(game_quantize(frames, True), frames) / study_noise_flip(game_quantize(frames, False),
                                                                                         frames)
    assert abs(study - 0.451) < 0.0015, f"the study frames changed: {study:.4f}"

    clip = []
    for rgb, alpha in frames:  # 8-bit straight RGBA, rounded half-up, RGB zeroed under alpha 0
        pixels = np.zeros(alpha.shape + (4,), np.uint8)
        pixels[..., :3] = np.floor(np.clip(rgb, 0, 255) + 0.5)
        pixels[..., 3] = np.floor(np.clip(alpha, 0, 1) * 255 + 0.5)
        pixels[pixels[..., 3] == 0] = 0
        clip.append(pixels)
    palette = fp.Palette(tuple(tuple(int(value) for value in colour) for colour in colours), transparent_index=255)
    unseeded = fp.quantize_sequence(clip, palette, loop=False)
    eight_bit = [(pixels[..., :3].astype(np.float32), pixels[..., 3].astype(np.float32) / 255) for pixels in clip]
    assert all(np.array_equal(ours, theirs) for ours, theirs in zip(unseeded, game_quantize(eight_bit, True)))
    plain = [fp.cleanup_alpha(fp.cleanup_orphans(fp.quantize_image(pixels, palette), 1)) for pixels in clip]
    nearest, held = fp.flip_stats(plain, clip), fp.flip_stats(unseeded, clip)
    looped = fp.flip_stats(fp.quantize_sequence(clip, palette, loop=True), clip, loop=True)
    reduction = 1 - held["noise_flip"] / nearest["noise_flip"]
    loop_reduction = 1 - looped["noise_flip"] / fp.flip_stats(plain, clip, loop=True)["noise_flip"]
    print(f"\ngame-opus55 {OPUS55_CLIP}: noise flips {nearest['noise_flip']:.4f} -> {held['noise_flip']:.4f} "
          f"({reduction:.1%} fewer; looped {loop_reduction:.1%}; study {study:.1%})")
    assert reduction >= 0.40 and loop_reduction >= 0.40
    assert held["alpha_flip"] <= nearest["alpha_flip"]


def test_hysteresis_switches_real_changes_at_once():
    palette = fp.Palette(((200, 40, 40), (40, 40, 200), (20, 20, 20)))
    frames = []
    for step in range(5):
        frame = np.zeros((6, 6, 4), np.uint8)
        frame[..., 3] = 255
        frame[..., :3] = (198, 44, 42) if step < 3 else (44, 42, 198)
        frames.append(frame)
    out = fp.quantize_sequence(frames, palette, loop=False)
    assert [int(frame[3, 3]) for frame in out] == [0, 0, 0, 1, 1]


def test_alpha_band_keeps_previous_opacity():
    palette = fp.Palette(((120, 120, 120),))

    def frame(alpha):
        pixels = np.zeros((5, 5, 4), np.uint8)
        pixels[..., :3] = 120
        pixels[..., 3] = 255
        pixels[2, 2, 3] = alpha
        return pixels

    alphas = [255, 140, 120, 80, 120, 140, 200, 120]
    out = fp.quantize_sequence([frame(a) for a in alphas], palette, loop=False, despeckle=False, orphans=0)
    opaque = [int(index[2, 2]) != 255 for index in out]
    #        255   140(band, held)  120(band, held)  80(out: off)  120(held off)  140(held off)  200(on)  120(held on)
    assert opaque == [True, True, True, False, False, False, True, True]


def test_loop_seeds_frame_zero_from_the_last_frame():
    palette = fp.Palette(((100, 100, 100), (110, 110, 110)))
    lab = fp.to_oklab(palette.rgb).astype(np.float64)
    near_first = (104, 104, 104)  # slightly nearer index 0, but within the margin of index 1
    assert np.sum((fp.to_oklab(np.array(near_first, np.uint8)) - lab[0]) ** 2) < \
        np.sum((fp.to_oklab(np.array(near_first, np.uint8)) - lab[1]) ** 2)

    def flat(colour):
        pixels = np.zeros((4, 4, 4), np.uint8)
        pixels[..., :3] = colour
        pixels[..., 3] = 255
        return pixels

    clip = [flat(near_first), flat((110, 110, 110)), flat((110, 110, 110))]
    oneshot = fp.quantize_sequence(clip, palette, loop=False)
    cycle = fp.quantize_sequence(clip, palette, loop=True)
    assert oneshot[0][0, 0] == 0 and cycle[0][0, 0] == 1, "a loop's first frame continues from its last"
    pingpong = fp.quantize_sequence([flat(near_first), flat((110, 110, 110))], palette, pingpong=True)
    assert pingpong[0][0, 0] == 1, "a ping-pong's first frame follows frame 1"


def test_pingpong_after_quantize_exact_mirror():
    """B04-T2: ping-pong is applied after quantizing, so the way back is the way forth, pixel for pixel."""
    clip = noisy_clip(4, frames=6)
    palette = fp.build_palette(clip, 12)
    cycle = fp.quantize_sequence(clip, palette, pingpong=True)
    assert len(cycle) == 2 * len(clip) - 2
    forward, back = cycle[:len(clip)], cycle[len(clip):]
    for mirrored, original in zip(back, forward[-2:0:-1]):
        assert mirrored is original
    separately = fp.quantize_sequence(clip + clip[-2:0:-1], palette, loop=True)
    assert any(not np.array_equal(a, b) for a, b in zip(separately[len(clip):], forward[-2:0:-1])), \
        "quantizing the mirrored frames again would not give an exact mirror"
    assert len(fp.quantize_sequence(clip[:2], palette, pingpong=True)) == 2
    assert len(fp.quantize_sequence(clip[:1], palette, pingpong=True)) == 1


def test_quantize_sequence_stats_and_validation():
    clip = noisy_clip(5, frames=4)
    palette = fp.build_palette(clip, 8)
    frames, stats = fp.quantize_sequence(clip, palette, return_stats=True)
    assert len(frames) == len(stats) == 4
    assert set(stats[0]) == {"held_index_px", "held_alpha_px", "orphans_fixed_px", "despeckled_px"}
    assert sum(item["held_index_px"] for item in stats) > 0
    assert fp.quantize_sequence([], palette) == []
    with pytest.raises(fp.PaletteError):
        fp.quantize_sequence([clip[0], clip[0][:10]], palette)
    with pytest.raises(fp.PaletteError):
        fp.quantize_sequence(clip, palette, alpha_band=(0.7, 0.2))
    with pytest.raises(fp.PaletteError):
        fp.quantize_sequence(clip, palette.replace(transparent_index=None))


def test_flip_stats_counts_known_flips():
    a = np.array([[1, 1], [255, 2]], np.uint8)
    b = np.array([[1, 3], [2, 2]], np.uint8)
    stats = fp.flip_stats([a, b])
    assert stats == {"pairs": 1, "idx_flip": round(1 / 3, 6), "noise_flip": None, "alpha_flip": 0.25}
    assert fp.flip_stats([a, b, a], loop=True)["pairs"] == 3
    with pytest.raises(fp.PaletteError):
        fp.flip_stats([a])


# --------------------------------------------------------------------------- variants and LUTs

def test_variant_colors():
    palette = fp.Palette(((24, 20, 36), (200, 120, 60), (240, 220, 180), (0, 0, 0)), transparent_index=3)
    assert fp.variant_colors(palette, "hitflash")[:3].tolist() == [[255, 255, 255]] * 3
    assert fp.variant_colors(palette, "silhouette", color="#102030")[:3].tolist() == [[16, 32, 48]] * 3
    frozen = fp.variant_colors(palette, "frozen")
    lab, base = fp.to_oklab(frozen[:3]).astype(np.float64), fp.to_oklab(palette.rgb[:3]).astype(np.float64)
    assert np.all(lab[:, 0] >= base[:, 0] - 1e-6), "frozen art is lighter"
    assert np.all(lab[:, 2] < 0), "frozen art leans blue"
    assert np.all(np.diff(lab[:, 0]) > 0), "frozen keeps the lightness order (contrast)"
    skin = fp.variant_colors(palette, "skin", mapping={"#c8783c": "#3c78c8", 0: "#000000"})
    assert skin[:3].tolist() == [[0, 0, 0], [60, 120, 200], [240, 220, 180]]
    assert frozen[3].tolist() == [0, 0, 0] and skin[3].tolist() == [0, 0, 0], "the transparent slot is untouched"
    with pytest.raises(fp.PaletteError):
        fp.variant_colors(palette, "skin", mapping={"#123456": "#000000"})
    with pytest.raises(fp.PaletteError):
        fp.variant_colors(palette, "sparkle")


def test_luts_snap_lut_image_and_palettize():
    palette = fp.Palette(((0, 0, 0), (255, 255, 255), (220, 40, 40), (40, 60, 200)))
    document = fp.luts(palette, skins={"skin-blue": {"2": "#2850c8"}}, snap=True)
    assert document["rows"] == ["identity", "hitflash", "frozen", "silhouette", "skin-blue"]
    assert document["luts"]["hitflash"]["index"] == [1, 1, 1, 1]
    assert document["luts"]["silhouette"]["index"] == [0, 0, 0, 0]
    assert all(colour in palette.hex_colors for row in document["luts"].values() for colour in row["colors"])
    texture = fp.lut_image(document)
    assert texture.shape == (5, 256, 4)
    assert np.array_equal(texture[0, :4, :3], palette.rgb) and texture[0, 4:, 3].max() == 0
    strip = fp.palettize_lut(palette, size=16)
    assert strip.shape == (16, 256, 4) and strip[..., 3].min() == 255
    assert strip[0, 0, :3].tolist() == [0, 0, 0] and strip[15, 15 + 16 * 15, :3].tolist() == [255, 255, 255]
    assert strip[0, 15, :3].tolist() == [220, 40, 40], "pure red (r=15, g=0, b=0) is the red entry"
    with pytest.raises(fp.PaletteError):
        fp.luts(palette, variants=("sparkle",))
    slot = fp.luts(palette.replace(transparent_index=0), variants=("hitflash",))
    assert slot["luts"]["hitflash"]["colors"][0] == "#00000000"


# --------------------------------------------------------------------------- grid detection

def _upscaled(scale=6, pad=((2, 1), (3, 2))):
    logical = np.zeros((10, 12, 4), np.uint8)
    logical[1:9, 2:10] = (200, 80, 40, 255)
    logical[3:6, 4:7] = (30, 30, 60, 255)
    logical[7, 8] = (240, 220, 120, 255)
    big = np.repeat(np.repeat(logical, scale, 0), scale, 1)
    return np.pad(big, pad + ((0, 0),))


def test_detect_grid_matches_codeart_core():
    codeart = load_script("codeart2d", "codeart_core")
    rng = np.random.default_rng(12)
    noisy = _upscaled().astype(np.int16) + rng.integers(-9, 10, _upscaled().shape)
    noisy[..., 3] = _upscaled()[..., 3]
    cases = [_upscaled(), _upscaled(5, ((0, 0), (0, 0))), np.clip(noisy, 0, 255).astype(np.uint8),
             rng.integers(0, 256, (40, 50, 4)).astype(np.uint8), np.zeros((16, 16, 4), np.uint8),
             shaded_sprite(7, noise=6.0)]
    for case in cases:
        for kwargs in ({}, {"max_period": 32, "tol": 12}):
            assert fp.detect_grid(case, **kwargs) == codeart.detect_grid(case, **kwargs)
    found = fp.detect_grid(_upscaled())
    assert (found["period"], found["phase"], found["score"], found["has_grid"]) == (6, [3, 2], 1.0, True)


def test_grid_score_at_a_given_period():
    measured = fp.grid_score(_upscaled(), 6)
    assert measured == fp.detect_grid(_upscaled())
    assert fp.grid_score(_upscaled(), 6, phase=(3, 2))["phase"] == [3, 2]
    assert fp.grid_score(_upscaled(), 3)["score"] == 1.0, "divisors of the true period score the same"
    for wrong in (5, 7):
        assert not fp.grid_score(_upscaled(), wrong)["has_grid"]
    with pytest.raises(fp.PaletteError):
        fp.grid_score(_upscaled(), 1)
