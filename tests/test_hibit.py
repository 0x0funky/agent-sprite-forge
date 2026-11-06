"""Hi-bit finish: the forge_hibit core, finish_frames hibit, sprite_set --finish hibit, hibit_plate and
fx_pixelfx (offline: a stand-in pixelfx renderer writes the sheet, so neither node nor pixelfx is needed).

Synthetic, deterministic art: a flat-shaded character (three flat tones on the body, a dark eye, a red scarf,
soft anti-aliased edges, magenta under alpha 0) that sways with the frame index, like the flat lineless art the
hibit master prompt asks the image model for.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import assert_cli_help, load_script, load_shared, run_cli, script_path

FH = load_shared("forge_hibit")
FF = load_script("video2dsprite", "finish_frames")
FP = load_script("video2dsprite", "forge_palette")
PLATE = load_script("generate2dmap", "hibit_plate")
PFX = load_script("generate2dsprite", "fx_pixelfx")
FINISH = script_path("video2dsprite", "finish_frames")
SPRITE_SET = script_path("video2dsprite", "sprite_set")

MAGENTA = (255, 0, 255)
BODY = ((40, 70, 150), (70, 120, 210), (150, 200, 250))    # shadow, base, highlight: flat tones of one material
SKIN, EYE, SCARF = (240, 200, 160), (20, 16, 30), (210, 50, 60)
STUDIO_ORANGE = (0xF1, 0x6E, 0x2E)


def _disc(px, py, cx, cy, r):
    return np.hypot(px - cx, py - cy) - r


def flat_character(phase: float = 0.0, size=(240, 240), soft: float = 1.5) -> np.ndarray:
    width, height = size
    py, px = np.mgrid[0:height, 0:width].astype(np.float32) + 0.5
    sway = 4.0 * math.sin(2 * math.pi * phase)
    cx, ground = width / 2 + sway, height - 20.0
    body = np.hypot((px - cx) / 46, (py - (ground - 70)) / 70) - 1.0
    tone = np.where(px < cx - 16, 0, np.where(px > cx + 18, 2, 1))       # crisp angular light and shadow shapes
    layers = [(body * 46, None), (_disc(px, py, cx, ground - 160, 34), SKIN),
              (_disc(px, py, cx + 12, ground - 165, 5.5), EYE),
              (np.maximum(np.abs(py - (ground - 128)) - 7, np.abs(px - cx) - 40), SCARF)]
    rgb = np.zeros((height, width, 3), np.float32)
    alpha = np.zeros((height, width), np.float32)
    for distance, colour in layers:
        cover = np.clip(0.5 - distance / soft, 0.0, 1.0)
        paint = np.array(BODY, np.float32)[tone] if colour is None else np.broadcast_to(np.array(colour, np.float32),
                                                                                         rgb.shape)
        total = cover + alpha * (1 - cover)
        rgb = (paint * cover[..., None] + rgb * (alpha * (1 - cover))[..., None]) / np.maximum(total, 1e-6)[..., None]
        alpha = total
    a8 = np.clip(np.floor(alpha * 255 + 0.5), 0, 255).astype(np.uint8)
    rgb8 = np.clip(np.floor(rgb + 0.5), 0, 255).astype(np.uint8)
    rgb8[a8 == 0] = MAGENTA
    return np.dstack([rgb8, a8])


def write_clip(folder: Path, count: int = 6, **kwargs) -> Path:
    folder.mkdir(parents=True)
    for index in range(count):
        Image.fromarray(flat_character(index / count, **kwargs)).save(folder / f"frame_{index:06d}.png")
    return folder


def frames_of(folder: Path) -> list[np.ndarray]:
    return [np.asarray(Image.open(path).convert("RGBA")) for path in sorted(folder.glob("*.png"))]


def colours_of(frame: np.ndarray) -> set[tuple[int, int, int]]:
    return {tuple(int(v) for v in c) for c in frame[..., :3][frame[..., 3] > 0]}


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    return write_clip(tmp_path_factory.mktemp("hibit") / "idle")


# --------------------------------------------------------------------------- forge_hibit core

def test_kmeans_palette_recovers_flat_colours_and_is_deterministic():
    rng = np.random.default_rng(3)
    colours = np.array([(20, 30, 40), (200, 60, 50), (90, 180, 120), (240, 230, 200)], np.uint8)
    pixels = colours[rng.integers(0, 4, 5000)]
    first = FH.kmeans_palette(pixels, 4, seed=1)
    assert np.array_equal(first, FH.kmeans_palette(pixels, 4, seed=1))
    assert {tuple(c) for c in first.tolist()} == {tuple(c) for c in colours.tolist()}
    luma = first.astype(np.float32) @ FH.LUMA
    assert np.all(np.diff(luma) >= 0)                               # sorted dark -> light
    assert len(FH.kmeans_palette(pixels, 9, seed=1)) == 4             # never more colours than the image has


def test_majority_downsample_votes_never_blends_and_cuts_low_coverage():
    sub = np.full((8, 16), -1, np.int32)
    sub[:, :8] = 0
    sub[:3, :8] = 1                       # 24 of 64 sub-pixels: the minority loses, no in-between colour appears
    sub[:2, 8:] = 2                       # 16 of 64 covered: under the 45% coverage cut, transparent
    out = FH.mode_downsample_blocks(sub, 8, 3)
    assert out.tolist() == [[0, -1]]
    sub[:4, 8:] = 2                       # 32 of 64: covered enough
    assert FH.mode_downsample_blocks(sub, 8, 3).tolist() == [[0, 2]]


def test_majority_hysteresis_keeps_a_close_previous_colour_and_a_thinning_edge():
    sub = np.zeros((8, 8), np.int32)
    sub[:, :4] = 1
    sub[0, 4] = 1                         # 33 vs 31: a near tie
    assert FH.mode_downsample_blocks(sub, 8, 2).tolist() == [[1]]
    assert FH.mode_downsample_blocks(sub, 8, 2, previous=np.array([[0]])).tolist() == [[0]]   # held
    edge = np.full((8, 8), -1, np.int32)
    edge[:5, :5] = 0                      # 25/64 = 39%: under 45%, but a cell that was opaque stays down to 33%
    assert FH.mode_downsample_blocks(edge, 8, 1).tolist() == [[-1]]
    assert FH.mode_downsample_blocks(edge, 8, 1, previous=np.array([[0]])).tolist() == [[0]]


def test_clean_isolated_merges_specks_keeps_eyes_and_drops_islands():
    palette = np.array([(60, 120, 200), (70, 128, 206), (15, 12, 25)], np.uint8)
    idx = np.zeros((7, 7), np.int32)
    idx[2, 2] = 1                         # a speck of a near colour: merged
    idx[4, 4] = 2                         # a dark eye: kept
    idx = np.pad(idx, 2, constant_values=-1)
    idx[0, 0] = 0                         # a 1 px island off the body: dropped
    out = FH.clean_isolated(idx, palette)
    assert out[4, 4] == 0 and out[6, 6] == 2 and out[0, 0] == -1


def test_auto_outline_colour_comes_from_the_image_and_is_never_the_studio_orange():
    blue = np.array([(40, 70, 150), (70, 120, 210), (150, 200, 250), (240, 200, 160)], np.uint8)
    colour = FH.auto_outline_color(blue, [500, 900, 300, 200])
    lab = FH.to_oklab(np.array(colour, np.uint8))
    assert abs(lab[0] - 0.22) < 0.02                                  # very dark
    hue = math.degrees(math.atan2(lab[2], lab[1])) % 360
    assert 200 <= hue <= 290, hue                                     # the sprite's dominant (blue) hue
    assert colour != STUDIO_ORANGE
    grey = np.array([(60, 60, 60), (128, 128, 128), (200, 200, 200)], np.uint8)
    cool = FH.to_oklab(np.array(FH.auto_outline_color(grey), np.uint8))
    assert cool[0] < 0.3 and math.hypot(cool[1], cool[2]) < 0.06      # no saturated colour: a cool near-black


def test_plate_crop_shade_maps_and_plate_pixelize():
    assert FH.plate_crop(1774, 887, 480, 270) == (98, 0, 1675, 887)
    assert FH.plate_crop(1000, 1000, 480, 270, y_frac=1.0) == (0, 438, 1000, 1000)
    palette = np.array([(10, 10, 20), (60, 60, 90), (130, 130, 170), (230, 230, 250)], np.uint8)
    dark, light = FH.shade_maps(palette)
    luma = palette.astype(np.float64) @ FH.LUMA.astype(np.float64)
    assert all(luma[dark[i]] < luma[i] or dark[i] == i for i in range(4))
    assert all(luma[light[i]] > luma[i] or light[i] == i for i in range(4))
    painting = np.zeros((90, 160, 3), np.uint8)
    painting[:45] = (230, 120, 110)
    painting[45:] = (40, 90, 80)
    painting[60:70, 40:60] = (240, 220, 90)
    idx, pal = FH.pixelize_plate(painting, 48, 27, colors=6, seed=2)
    assert idx.shape == (27, 48) and (idx >= 0).all() and len(pal) <= 6


# --------------------------------------------------------------------------- finish_frames hibit

def test_hibit_finish_is_palette_exact_binary_with_an_image_outline(clip, tmp_path):
    summary = FF.finish_clip(clip, tmp_path / "hibit", mode="hibit", target_height=40)
    assert summary["mode"] == "hibit" and summary["qa"]["alphaBinary"] is True
    pal = FP.read_palette(tmp_path / "hibit" / FF.PALETTE_FILE)
    assert "outline" in pal.names and len(pal) <= FF.HIBIT_COLORS + 1
    outline = tuple(int(v) for v in pal.rgb[pal.names.index("outline")])
    assert outline != STUDIO_ORANGE and summary["qa"]["outlineColor"] == FH.hex_of(outline)
    allowed = {tuple(c) for c in pal.rgb.tolist()}
    frames = frames_of(tmp_path / "hibit" / "frames")
    for frame in frames:
        assert colours_of(frame) <= allowed
        assert set(np.unique(frame[..., 3]).tolist()) <= {0, 255}
        solid = frame[..., 3] > 0
        inner = solid & ~(np.pad(solid, 1)[2:, 1:-1] & np.pad(solid, 1)[:-2, 1:-1]
                          & np.pad(solid, 1)[1:-1, 2:] & np.pad(solid, 1)[1:-1, :-2])
        rim = {tuple(int(v) for v in c) for c in frame[..., :3][inner]}
        assert outline in rim                                         # the 1 px rim is the outline colour
    eye_like = {c for frame in frames for c in colours_of(frame)
                if c != outline and sum(abs(a - b) for a, b in zip(c, EYE)) < 60}
    assert eye_like, "the 1-2 px dark eye must survive the majority vote and the lone-pixel cleanup"
    document = json.loads((tmp_path / "hibit" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    assert document["hibit"]["supersample"] == 8 and document["hibit"]["coverage"] == FF.HIBIT_COVERAGE
    assert document["engine"]["sampling"] == "nearest" and document["engine"]["pixelArt"] is True
    assert document["palette"]["outlineColor"] == FH.hex_of(outline)
    assert document["qa"]["method"].startswith("finish_frames hibit v1")


def test_hibit_second_clip_reuses_the_palette_and_a_fixed_outline_colour(clip, tmp_path):
    FF.finish_clip(clip, tmp_path / "first", mode="hibit", target_height=40)
    first = FP.read_palette(tmp_path / "first" / FF.PALETTE_FILE)
    again = FF.finish_clip(clip, tmp_path / "again", mode="hibit", target_height=40,
                           palette=tmp_path / "first" / FF.PALETTE_FILE)
    assert again["palette"].endswith(FF.PALETTE_FILE) and not (tmp_path / "again" / FF.PALETTE_FILE).exists()
    assert again["qa"]["outlineColor"] == FH.hex_of(first.rgb[first.names.index("outline")])
    fixed = FF.finish_clip(clip, tmp_path / "fixed", mode="hibit", target_height=40, outline="#123456", colors=8)
    assert fixed["qa"]["outlineColor"] == "#123456" and fixed["paletteColors"] <= 9
    none = FF.finish_clip(clip, tmp_path / "none", mode="hibit", target_height=40, outline="none")
    assert none["qa"]["outlinePx"] == 0
    with pytest.raises(FF.FinishError, match="outline colour is"):
        FF.finish_clip(clip, tmp_path / "clash", mode="hibit", target_height=40, outline="#123456",
                       palette=tmp_path / "first" / FF.PALETTE_FILE)


def test_hibit_refuses_pixel_options_and_hd_refuses_coverage(clip, tmp_path):
    for option in ({"downscale": "box"}, {"contrast": 1.2}, {"shade_gap": 0.04}, {"colour_lock": "rest"},
                   {"reserve": ["#ff0000"]}):
        with pytest.raises(FF.FinishError, match="pixel finish"):
            FF.finish_clip(clip, tmp_path / "bad", mode="hibit", target_height=40, **option)
    with pytest.raises(FF.FinishError, match="hibit outline"):
        FF.finish_clip(clip, tmp_path / "bad", mode="hibit", target_height=40, outline="orange-ish")
    with pytest.raises(FF.FinishError, match="coverage"):
        FF.finish_clip(clip, tmp_path / "bad", mode="hibit", target_height=40, coverage=1.5)
    with pytest.raises(FF.FinishError, match="pixel finish"):
        FF.finish_clip(clip, tmp_path / "bad", target_height=40, coverage=0.5)
    with pytest.raises(FF.FinishError, match="hibit finish"):
        FF.finish_clip(clip, tmp_path / "bad", mode="pixel", target_height=40, coverage=0.5)
    assert not (tmp_path / "bad").exists()


def test_hibit_cli_prints_one_json_line(clip, tmp_path):
    result = run_cli([FINISH, "hibit", "--frames", clip, "--output-dir", tmp_path / "cli", "--target-height", "36",
                      "--outline", "auto", "--coverage", "0.5", "--indexed-sheet"], "cp1252")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["mode"] == "hibit" and summary["indexedSheet"]
    assert (tmp_path / "cli" / FF.SHEET_FILE).is_file()


# --------------------------------------------------------------------------- sprite_set --finish hibit

def _master(folder: Path) -> Path:
    folder.mkdir(parents=True)
    still = flat_character(0.0, soft=1.0)
    alpha = still[..., 3:].astype(np.float64) / 255
    rgb = still[..., :3] * alpha + np.array(MAGENTA, np.float64) * (1 - alpha)
    Image.fromarray(np.clip(rgb + 0.5, 0, 255).astype(np.uint8), "RGB").save(folder / "master.png")
    rows = np.flatnonzero((still[..., 3] > 127).any(1))
    sha = load_script("video2dsprite", "sprite_set").forge_core.sha256_file(folder / "master.png")
    master = {"schema": "generate2dsprite.master.v1", "name": "flatkid", "identity_recap": "a flat-shaded kid",
              "facing": "right", "finish": "hibit", "class": "hero",
              "framing": {"canvas": [240, 240], "subject_height_px": int(rows[-1] + 1 - rows[0]),
                          "top_margin_px": int(rows[0])},
              "key": "magenta", "files": {"master": {"path": "master.png", "sha256": sha}},
              "references": [], "transform": {"pad": "test"}, "route": "test"}
    (folder / "master.json").write_text(json.dumps(master), encoding="utf-8")
    return folder / "master.json"


def test_sprite_set_plans_a_hibit_set(tmp_path):
    master = _master(tmp_path / "art")
    result = run_cli([SPRITE_SET, "plan", "--master", master, "--output-dir", tmp_path / "set", "--actions",
                      "idle,cast", "--outline", "#0A1B2C", "--colors", "12"], "cp1252")
    assert result.returncode == 0, result.stderr
    plan = json.loads((tmp_path / "set" / "set_plan.json").read_text(encoding="utf-8"))
    assert plan["finish"]["mode"] == "hibit" and plan["finish"]["targetHeight"] == 80
    assert plan["finish"]["outline"] == "#0a1b2c" and plan["finish"]["colors"] == 12
    assert plan["finish"]["colourLock"] is False and plan["package"]["fps"] == 12
    assert "flat lineless" in plan["style"]
    refused = run_cli([SPRITE_SET, "plan", "--master", master, "--output-dir", tmp_path / "bad", "--outline", "fuzzy"],
                      "cp1252")
    assert refused.returncode == 1 and "hibit --outline" in refused.stderr and not (tmp_path / "bad").exists()


# --------------------------------------------------------------------------- hibit_plate

def test_hibit_plate_writes_a_native_plate_palette_and_shades(tmp_path):
    painting = np.zeros((300, 600, 3), np.uint8)
    painting[:150] = (232, 118, 106)
    painting[150:230] = (44, 92, 104)
    painting[230:] = (176, 120, 84)
    painting[180:200, 100:140] = (120, 80, 140)
    Image.fromarray(painting, "RGB").save(tmp_path / "painting.png")
    result = run_cli([script_path("generate2dmap", "hibit_plate"), "plate", "--input", tmp_path / "painting.png",
                      "--output-dir", tmp_path / "plate", "--size", "160x90", "--colors", "6"], "cp1252")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    plate = np.asarray(Image.open(tmp_path / "plate" / "plate.png").convert("RGBA"))
    assert plate.shape == (90, 160, 4) and (plate[..., 3] == 255).all()
    pal = FP.read_palette(tmp_path / "plate" / "palette.json")
    assert colours_of(plate) <= {tuple(c) for c in pal.rgb.tolist()} and summary["colors"] == len(pal) <= 6
    preview = Image.open(tmp_path / "plate" / "plate-x4.png")
    assert preview.size == (640, 360)
    shades = json.loads((tmp_path / "plate" / "shade-maps.json").read_text(encoding="utf-8"))
    assert len(shades["dark"]) == len(shades["light"]) == len(pal)
    record = json.loads((tmp_path / "plate" / "plate.json").read_text(encoding="utf-8"))
    assert record["schema"] == PLATE.PLATE_SCHEMA and record["engine"]["sampling"] == "nearest"
    shared = run_cli([script_path("generate2dmap", "hibit_plate"), "plate", "--input", tmp_path / "painting.png",
                      "--output-dir", tmp_path / "shared", "--size", "160x90", "--palette",
                      tmp_path / "plate" / "palette.json"], "cp1252")
    assert shared.returncode == 0, shared.stderr
    again = np.asarray(Image.open(tmp_path / "shared" / "plate.png").convert("RGBA"))
    assert colours_of(again) <= {tuple(c) for c in pal.rgb.tolist()}


# --------------------------------------------------------------------------- fx_pixelfx (offline)

LICENSE_TEXT = "MIT License\n\nCopyright (c) 2026 Patch\nCopyright (c) 2026 Hugo Duprez\n\nPermission is hereby granted\n"
NOTICE_TEXT = "# Notices\n\n**Hugo Duprez.** sim.js adapts Sprite Fusion.\n"


def fake_checkout(root: Path) -> Path:
    (root / "tools").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "pixelfx", "version": "0.3.0", "license": "MIT"}),
                                       encoding="utf-8")
    (root / "tools" / "render.mjs").write_text("// stand-in\n", encoding="utf-8")
    (root / "LICENSE").write_text(LICENSE_TEXT, encoding="utf-8")
    (root / "NOTICE.md").write_text(NOTICE_TEXT, encoding="utf-8")
    return root


def fake_render(root, script, args, timeout=0):
    """What pixelfx's render.mjs --sheet writes: <out>/<id>/<name>.png and .json (4 frames of 8x6)."""
    assert script == "tools/render.mjs" and "--no-actor" in args and args[args.index("--bg") + 1] == "none"
    effect, out = args[0], Path(args[args.index("--out") + 1])
    name = effect.split("/")[1]
    folder = out / effect
    folder.mkdir(parents=True)
    sheet = np.zeros((12, 16, 4), np.uint8)
    frames = []
    for i in range(4):
        x, y = (i % 2) * 8, (i // 2) * 6
        sheet[y + 2:y + 4, x + 1 + i:x + 3 + i] = (250, 230, 120, 255)
        sheet[y + 1, x + 2 + i] = (255, 255, 255, 96)                  # a soft glow pixel
        frames.append({"filename": f"{name} {i}", "frame": {"x": x, "y": y, "w": 8, "h": 6}})
    Image.fromarray(sheet, "RGBA").save(folder / f"{name}.png")
    meta = {"pixelfx": {"id": effect, "name": "Test", "fps": 24, "loop": False, "frames": 4, "frameSize": [8, 6],
                        "pivot": [4, 5], "columns": 2}}
    (folder / f"{name}.json").write_text(json.dumps({"frames": frames, "meta": meta}), encoding="utf-8")
    return f"{effect}  4 frames"


def test_fx_pixelfx_needs_a_checkout_and_says_where_to_get_it(tmp_path, monkeypatch):
    monkeypatch.delenv(PFX.ENV, raising=False)
    with pytest.raises(ValueError, match="git clone https://github.com/patch-gg/pixelfx"):
        PFX.find_checkout(None)
    (tmp_path / "other").mkdir()
    with pytest.raises(ValueError, match="not a pixelfx checkout"):
        PFX.find_checkout(str(tmp_path / "other"))


def test_fx_pixelfx_render_writes_frames_credits_and_provenance(tmp_path, monkeypatch):
    root = fake_checkout(tmp_path / "pixelfx")
    monkeypatch.setattr(PFX, "run_node", fake_render)
    palette = tmp_path / "palette.json"
    FP.write_palette(FP.as_palette([(20, 16, 30), (240, 210, 90), (250, 250, 240)]), palette, "json")
    parser = PFX.build_parser()
    args = parser.parse_args(["render", "--pixelfx", str(root), "--id", "arcane/star-fall", "--output-dir",
                              str(tmp_path / "fx"), "--ramp", "#1a1030,#ffd84a,#fffbe0", "--snap", str(palette),
                              "--seed", "7"])
    summary = PFX.cmd_render(args)
    assert summary["frames"] == 4 and summary["frameSize"] == [8, 6] and summary["pivot"] == [4, 5]
    out = tmp_path / "fx"
    frames = frames_of(out / "frames")
    allowed = {(20, 16, 30), (240, 210, 90), (250, 250, 240)}
    assert len(frames) == 4 and all(colours_of(frame) <= allowed for frame in frames)
    credits = (out / "CREDITS.txt").read_text(encoding="utf-8")
    assert "https://github.com/patch-gg/pixelfx" in credits and "Copyright (c) 2026 Patch" in credits
    assert "Hugo Duprez" in credits and "Patch Pixel font" in credits and "MIT" in credits
    record = json.loads((out / "fx.json").read_text(encoding="utf-8"))
    assert record["pixelfx"]["license"] == "MIT" and record["pixelfx"]["version"] == "0.3.0"
    assert record["pixelfx"]["copyright"] == ["Copyright (c) 2026 Patch", "Copyright (c) 2026 Hugo Duprez"]
    assert record["options"]["palette"] == ["#1a1030", "#ffd84a", "#fffbe0"] and record["actor"] is False
    assert "pixelfx" in record["artSource"] and record["recolour"]["snap"]["colors"] == 3
    sheet = json.loads((out / "sheet.json").read_text(encoding="utf-8"))
    assert len(sheet["frames"]) == 4 and "MIT" in sheet["meta"]["license"]
    assert Image.open(out / "preview.gif").n_frames == 4
    assert all(set(np.unique(f[..., 3]).tolist()) <= {0, 255} for f in frames)     # binary alpha by default
    assert record["alpha"] == "binary"
    soft = PFX.cmd_render(parser.parse_args(["render", "--pixelfx", str(root), "--id", "arcane/star-fall",
                                             "--output-dir", str(tmp_path / "fx2"), "--alpha", "keep"]))
    assert soft["frames"] == 4
    assert any(96 in np.unique(f[..., 3]).tolist() for f in frames_of(tmp_path / "fx2" / "frames"))   # glow kept
    with pytest.raises(ValueError, match="effect id"):
        PFX.cmd_render(parser.parse_args(["render", "--pixelfx", str(root), "--id", "fire/*", "--output-dir",
                                          str(tmp_path / "fx3")]))


def test_fx_check_picks_pixelfx_only_with_a_checkout_and_node(tmp_path, monkeypatch):
    monkeypatch.delenv(PFX.ENV, raising=False)
    parser = PFX.build_parser()
    monkeypatch.setattr(PFX, "node_version", lambda: ("v22.15.0", 22))
    missing = PFX.cmd_check(parser.parse_args(["check"]))
    assert missing["route"] == "image" and missing["pixelfx"] is None
    assert any("hibit_fx.py pixelize" in step for step in missing["next"]) and "git clone" in missing["install"]
    root = fake_checkout(tmp_path / "pixelfx")
    found = PFX.cmd_check(parser.parse_args(["check", "--pixelfx", str(root)]))
    assert found["route"] == "pixelfx" and found["pixelfx"]["license"] == "MIT" and found["install"] is None
    monkeypatch.setattr(PFX, "node_version", lambda: (None, None))
    no_node = PFX.cmd_check(parser.parse_args(["check", "--pixelfx", str(root)]))
    assert no_node["route"] == "image" and any("Node" in reason for reason in no_node["reasons"])


def fx_frames(folder: Path) -> Path:
    """Four registered frames of a growing gold ground burst with a cream sparkle, on transparency."""
    folder.mkdir()
    yy, xx = np.mgrid[0:256, 0:256]
    for i in range(4):
        frame = np.zeros((256, 256, 4), np.uint8)
        radius = 20 + 25 * i
        burst = (np.hypot(xx - 128, yy - 200) < radius) & (yy <= 200)
        frame[burst] = (247, 194, 72, 255)
        frame[burst & (np.hypot(xx - 128, yy - 200) < radius * 0.5)] = (253, 240, 4, 255)
        frame[40 + i * 8:58 + i * 8, 30:48] = (251, 219, 162, 255)          # an 18 px sparkle, a detached island
        Image.fromarray(frame).save(folder / f"sheet-{i + 1}.png")
    return folder


def test_hibit_fx_pixelizes_registered_frames_into_one_palette(tmp_path):
    frames_dir = fx_frames(tmp_path / "burst")
    result = run_cli([script_path("generate2dsprite", "hibit_fx"), "pixelize", "--frames", frames_dir, "--output-dir",
                      tmp_path / "fx", "--target-px", "40", "--colors", "4", "--anchor", "bottom", "--frame-ms",
                      "80", "--name", "burst"], "cp1252")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["route"] == "image" and summary["frames"] == 4
    frames = frames_of(tmp_path / "fx" / "frames")
    h, w = frames[0].shape[:2]
    assert summary["frameSize"] == [w, h] and summary["anchor"] == [w / 2, h - 1]
    pal = FP.read_palette(tmp_path / "fx" / "palette.json")
    allowed = {tuple(c) for c in pal.rgb.tolist()}
    assert all(colours_of(f) <= allowed for f in frames)
    assert all(set(np.unique(f[..., 3]).tolist()) <= {0, 255} for f in frames)
    widths = [int((f[..., 3] > 0).any(0).sum()) for f in frames]
    assert widths == sorted(widths) and widths[-1] > widths[0]                 # one scale: the burst really grows
    assert (251, 219, 162) in colours_of(frames[0])                             # the detached sparkle is kept
    record = json.loads((tmp_path / "fx" / "fx.json").read_text(encoding="utf-8"))
    assert record["route"] == "image" and record["qa"]["status"] == "needs-visual-review"
    assert json.loads((tmp_path / "fx" / "sheet.json").read_text(encoding="utf-8"))["meta"]["frameTags"][0]["to"] == 3
    refused = run_cli([script_path("generate2dsprite", "hibit_fx"), "pixelize", "--frames", frames_dir, "--output-dir",
                       tmp_path / "bad", "--target-px", "40", "--colors", "4", "--palette", tmp_path / "fx" /
                       "palette.json"], "cp1252")
    assert refused.returncode == 1 and "drop it" in refused.stderr and not (tmp_path / "bad").exists()


def test_new_clis_print_ascii_help():
    for skill, name in (("generate2dmap", "hibit_plate"), ("generate2dsprite", "fx_pixelfx"),
                        ("generate2dsprite", "hibit_fx")):
        assert_cli_help(skill, name)
