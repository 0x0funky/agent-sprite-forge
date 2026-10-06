"""video2dsprite finish_frames: HD (default) and pixel finishes, cast palettes and line-ups.

Synthetic, deterministic clips: a soft-edged character drawn from signed distance
fields (legs and an arm swing with the frame index), with magenta under alpha 0
like keyed video frames, and optional 8x8 block noise like compression.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import assert_valid_contract, load_script, run_cli, script_path

FF = load_script("video2dsprite", "finish_frames")
FP = load_script("video2dsprite", "forge_palette")
SKILL = "video2dsprite"
SCRIPT = script_path(SKILL, "finish_frames")

MAGENTA = (255, 0, 255)
FLAT = (200, 60, 50)
CANVAS = (200, 240)
REST_HEIGHT = 158  # source px of the rest-pose body at scale 1 (rows with >= 6 px above 50% alpha)
PLAIN = {"downscale": "box", "contrast": 1.0, "saturation": 1.0, "outline": "none"}   # the v1 pixel finish


# --------------------------------------------------------------------------- synthetic clips

def _capsule(px, py, ax, ay, bx, by, radius):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    t = np.clip((wx * vx + wy * vy) / max(vx * vx + vy * vy, 1e-9), 0.0, 1.0)
    return np.hypot(wx - t * vx, wy - t * vy) - radius


def _ellipse(px, py, cx, cy, rx, ry):
    return (np.hypot((px - cx) / rx, (py - cy) / ry) - 1.0) * min(rx, ry)


def _block_noise(rng, shape, sigma, block=8):
    height, width = shape[:2]
    coarse = rng.normal(0.0, sigma, ((height + block - 1) // block, (width + block - 1) // block) + shape[2:])
    return np.repeat(np.repeat(coarse, block, axis=0), block, axis=1)[:height, :width]


def character(phase=0.0, *, scale=1.0, size=CANVAS, soft=2.0, flat=False, noise=0.0, rng=None):
    """RGBA uint8 frame: legs, gradient torso, head and a swinging arm; magenta under alpha 0."""
    width, height = size
    py, px = np.mgrid[0:height, 0:width].astype(np.float32) + 0.5
    s, ground, cx = scale, height - 20.0, width / 2
    swing = 6 * s * math.sin(2 * math.pi * phase)
    shade = np.clip((py - (ground - 126 * s)) / (68 * s), 0, 1)[..., None]
    torso = (1 - shade) * np.array([90, 170, 220], np.float32) + shade * np.array([30, 70, 140], np.float32)
    parts = [
        (_capsule(px, py, cx - 8 * s, ground - 60 * s, cx - 8 * s + swing, ground - 7 * s, 7 * s), (60, 50, 90)),
        (_capsule(px, py, cx + 8 * s, ground - 60 * s, cx + 8 * s - swing, ground - 7 * s, 7 * s), (60, 50, 90)),
        (_ellipse(px, py, cx, ground - 92 * s, 22 * s, 34 * s), None),
        (_ellipse(px, py, cx, ground - 140 * s, 18 * s, 18 * s), (235, 190, 150)),
        (_capsule(px, py, cx + 20 * s, ground - 115 * s, cx + 30 * s + swing, ground - 75 * s, 5 * s),
         (235, 190, 150)),
    ]
    rgb = np.zeros((height, width, 3), np.float32)
    alpha = np.zeros((height, width), np.float32)
    for distance, colour in parts:
        cover = np.clip(0.5 - distance / soft, 0.0, 1.0)
        paint = (np.broadcast_to(np.array(FLAT if flat else colour, np.float32), rgb.shape)
                 if flat or colour is not None else torso)
        total = cover + alpha * (1 - cover)
        rgb = (paint * cover[..., None] + rgb * (alpha * (1 - cover))[..., None]) / np.maximum(total, 1e-6)[..., None]
        alpha = total
    if noise:
        rgb = rgb + _block_noise(rng, rgb.shape, noise)
        edge = (alpha > 0.02) & (alpha < 0.98)
        alpha = np.where(edge, alpha + _block_noise(rng, alpha.shape, noise / 128.0), alpha)
    a8 = np.clip(np.floor(alpha * 255 + 0.5), 0, 255).astype(np.uint8)
    rgb8 = np.clip(np.floor(rgb + 0.5), 0, 255).astype(np.uint8)
    rgb8[a8 == 0] = MAGENTA
    return np.dstack([rgb8, a8])


def write_clip(folder: Path, count=8, *, still=False, seed=0, pad=(0, 0, 0, 0), **kwargs) -> Path:
    """Frames frame_000000.png ... of one action; ``pad`` (L, T, R, B) places them on a bigger canvas."""
    folder.mkdir(parents=True)
    rng = np.random.default_rng(seed)
    for index in range(count):
        frame = character(0.0 if still else index / count, rng=rng, **kwargs)
        if any(pad):
            left, top, right, bottom = pad
            frame = np.pad(frame, ((top, bottom), (left, right), (0, 0)))
            frame[frame[..., 3] == 0] = (*MAGENTA, 0)
        Image.fromarray(frame).save(folder / f"frame_{index:06d}.png")
    return folder


def frames_of(folder: Path) -> list[np.ndarray]:
    return [np.asarray(Image.open(path).convert("RGBA")) for path in sorted(folder.glob("*.png"))]


def digest_tree(folder: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(folder.glob("*.png"))}


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    root = tmp_path_factory.mktemp("clips")
    return {
        "walk": write_clip(root / "walk"),
        "flat": write_clip(root / "flat", count=4, flat=True, soft=3.0),
        "hard": write_clip(root / "hard", count=3, flat=True, soft=0.05),
        "noisy": write_clip(root / "noisy", count=10, still=True, seed=11, noise=6.0),
        "padded": write_clip(root / "padded", pad=(7, 5, 3, 9)),
        "big": write_clip(root / "big", count=4, scale=1.1),
        "close": write_clip(root / "close", count=4, scale=1.008),
        "small": write_clip(root / "small", count=4, scale=0.9),
        "boss": write_clip(root / "boss", count=4, scale=1.2, size=(260, 300)),
    }


# --------------------------------------------------------------------------- hd

def test_hd_downscale_has_no_colour_fringe_at_alpha_edges(clips, tmp_path):
    source = frames_of(clips["flat"])
    assert all((frame[frame[..., 3] == 0, :3] == MAGENTA).all() for frame in source)  # key colour hidden under alpha 0
    summary = FF.finish_clip(clips["flat"], tmp_path / "hd", target_height=30)
    assert summary["mode"] == "hd" and summary["qa"]["alphaBinary"] is False
    frames = frames_of(tmp_path / "hd" / "frames")
    edge_px = 0
    for frame in frames:
        visible = frame[..., 3] > 0
        edge_px += int(((frame[..., 3] > 0) & (frame[..., 3] < 255)).sum())
        deviation = np.abs(frame[..., :3][visible].astype(int) - np.array(FLAT)).max()
        assert deviation <= 2, f"edge colour drifted by {deviation} toward the hidden key colour"
        assert (frame[~visible] == 0).all()  # published RGB under alpha 0 is zeroed
    assert edge_px > 20  # the edges really are soft, so the check above covered them


def test_hd_is_an_area_downscale_on_a_grid_pinned_to_the_feet(clips, tmp_path):
    hard = FF.finish_clip(clips["hard"], tmp_path / "hard", target_height=24)
    alpha = frames_of(tmp_path / "hard" / "frames")[0][..., 3]
    partial = ((alpha > 0) & (alpha < 255)).sum()
    assert partial > 10, "a binary-alpha source must come out with area coverage, never nearest samples"
    document = json.loads((tmp_path / "hard" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    assert document["resampler"] == "box" and document["anchorMode"] == "feet"
    assert hard["anchor"] == document["anchor"]

    a = FF.finish_clip(clips["walk"], tmp_path / "a", target_height=32, display_sizes="1x,2x")
    b = FF.finish_clip(clips["padded"], tmp_path / "b", target_height=32, display_sizes="1x,2x")
    # The same frames placed on another canvas finish to the same bytes, size and anchor.
    assert digest_tree(tmp_path / "a" / "frames") == digest_tree(tmp_path / "b" / "frames")
    assert digest_tree(tmp_path / "a" / "frames-2x") == digest_tree(tmp_path / "b" / "frames-2x")
    assert a["size"] == b["size"] and a["anchor"] == b["anchor"]
    rest = frames_of(tmp_path / "a" / "frames")[0]
    body = FF.measure_body(rest[..., 3], FF.OUTPUT_MIN_RUN)
    assert body.ground == a["anchor"][1]  # the feet line sits exactly on the anchor row edge
    assert abs(body.stance_x - a["anchor"][0]) <= 1.0
    assert abs(body.height - 32) <= 1


def test_display_sizes_are_rederived_from_the_source(clips, tmp_path):
    summary = FF.finish_clip(clips["walk"], tmp_path / "set", target_height=30, display_sizes="1x,2x")
    names = sorted(path.name for path in clips["walk"].glob("*.png"))
    assert sorted(path.name for path in (tmp_path / "set" / "frames").glob("*.png")) == names
    assert sorted(path.name for path in (tmp_path / "set" / "frames-2x").glob("*.png")) == names
    one, two = summary["displaySizes"]
    assert (one["label"], two["label"]) == ("1x", "2x")
    # Content scales by 2; the 1 px transparent crop margin does not.
    assert abs((two["size"][1] - 2) - 2 * (one["size"][1] - 2)) <= 2
    small = frames_of(tmp_path / "set" / "frames")[0]
    large = frames_of(tmp_path / "set" / "frames-2x")[0]
    for frame, entry, height in ((small, one, 30), (large, two, 60)):
        body = FF.measure_body(frame[..., 3], FF.OUTPUT_MIN_RUN)
        assert abs(body.height - height) <= 1 and body.ground == entry["anchor"][1]
    upscaled = np.repeat(np.repeat(small, 2, axis=0), 2, axis=1)
    assert upscaled.shape != large.shape or not np.array_equal(upscaled, large)  # not a nearest upscale of 1x
    assert len(np.unique(large[..., 3])) > len(np.unique(small[..., 3]))


def test_finishing_never_upscales(clips, tmp_path):
    with pytest.raises(FF.FinishError, match="upscale"):
        FF.finish_clip(clips["walk"], tmp_path / "up", target_height=REST_HEIGHT + 2)
    with pytest.raises(FF.FinishError, match="display size 2x would upscale"):
        FF.finish_clip(clips["walk"], tmp_path / "up2", target_height=100, display_sizes="1x,2x")
    assert not (tmp_path / "up").exists() and not (tmp_path / "up2").exists()
    FF.finish_clip(clips["walk"], tmp_path / "done", target_height=40)
    for finished in (tmp_path / "done", tmp_path / "done" / "frames"):
        with pytest.raises(FF.FinishError, match="never rescaled"):
            FF.finish_clip(finished, tmp_path / "again", mode="pixel", target_height=20)
    assert not (tmp_path / "again").exists()
    opaque = tmp_path / "opaque"
    opaque.mkdir()
    Image.new("RGBA", (64, 64), (255, 0, 255, 255)).save(opaque / "frame_000000.png")
    with pytest.raises(FF.FinishError, match="no transparency"):
        FF.finish_clip(opaque, tmp_path / "unkeyed", target_height=20)


# --------------------------------------------------------------------------- pixel

def test_pixel_mode_output_is_deterministic(clips, tmp_path):
    first = FF.finish_clip(clips["walk"], tmp_path / "one", mode="pixel", target_height=22, colors=24, seed=4)
    second = FF.finish_clip(clips["walk"], tmp_path / "two", mode="pixel", target_height=22, colors=24, seed=4)
    assert digest_tree(tmp_path / "one" / "frames") == digest_tree(tmp_path / "two" / "frames")
    assert (tmp_path / "one" / FF.PALETTE_FILE).read_bytes() == (tmp_path / "two" / FF.PALETTE_FILE).read_bytes()
    one = json.loads((tmp_path / "one" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    two = json.loads((tmp_path / "two" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    assert [frame["sha256"] for frame in one["frames"]] == [frame["sha256"] for frame in two["frames"]]
    assert first["qa"] == second["qa"] and first["qa"]["alphaBinary"] is True
    for frame in frames_of(tmp_path / "one" / "frames"):
        assert set(np.unique(frame[..., 3]).tolist()) <= {0, 255}
    pixel = one["pixel"]
    assert pixel["dither"] is False and pixel["alphaThreshold"] == 0.5 and one["palette"]["built"] is True
    assert_valid_contract(one["qa"], "common", "qaEnvelope")
    assert one["qa"]["method"] and one["qa"]["notProven"]


def test_palette_build_and_the_palette_size(clips, tmp_path):
    out = tmp_path / "cast.json"
    result = run_cli([SCRIPT, "palette", "build", "--frames", clips["walk"], clips["small"], "--colors", "8",
                      "--reserve", "#000000", "#ffffff", "--seed", "2", "--out", out], "cp1252")
    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii() and len(result.stdout.strip().splitlines()) == 1
    summary = json.loads(result.stdout)
    assert summary["colors"] <= 8 and summary["reserved"] == 2 and summary["sets"] == 2
    document = json.loads(out.read_text(encoding="utf-8"))
    assert_valid_contract(document, "sprite", "palette_v1")
    assert [entry["hex"] for entry in document["colors"][:2]] == ["#000000", "#ffffff"]
    assert all(entry.get("reserved") for entry in document["colors"][:2]) and document["transparent_index"] == 255
    again, _ = FF.build_cast_palette([clips["walk"], clips["small"]], 8, reserve=["#000000", "#ffffff"], seed=2)
    assert list(again.hex_colors) == [entry["hex"] for entry in document["colors"]]

    shared = FF.finish_clip(clips["walk"], tmp_path / "shared", mode="pixel", target_height=24, palette=out)
    assert shared["paletteColors"] == len(document["colors"]) and Path(shared["palette"]) == out.resolve()
    allowed = {tuple(int(entry["hex"][i:i + 2], 16) for i in (1, 3, 5)) for entry in document["colors"]}
    used = {tuple(pixel) for frame in frames_of(tmp_path / "shared" / "frames")
            for pixel in frame[..., :3][frame[..., 3] == 255].tolist()}
    assert used <= allowed and len(used) <= 8
    finished = json.loads((tmp_path / "shared" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    assert finished["palette"]["built"] is False and not (tmp_path / "shared" / FF.PALETTE_FILE).exists()
    assert next(c for c in finished["qa"]["checks"] if c["id"] == "palette-fit")["status"] == "pass"

    learned = FF.finish_clip(clips["walk"], tmp_path / "learned", mode="pixel", target_height=24, colors=12)
    colours = {tuple(pixel) for frame in frames_of(tmp_path / "learned" / "frames")
               for pixel in frame[..., :3][frame[..., 3] == 255].tolist()}
    assert learned["paletteColors"] <= 12 and len(colours) <= 12
    with pytest.raises(FF.FinishError, match="2..255"):
        FF.build_cast_palette([clips["walk"]], 256)
    refused = run_cli([SCRIPT, "palette", "build", "--frames", clips["walk"], "--colors", "8", "--out", out])
    assert refused.returncode == 1 and refused.stderr.startswith("error: output already exists")


def test_hysteresis_reduces_flips_against_per_frame_quantization(clips, tmp_path):
    hd = FF.finish_clip(clips["noisy"], tmp_path / "hd", target_height=26)
    pixel = FF.finish_clip(clips["noisy"], tmp_path / "px", mode="pixel", target_height=26, colors=16,
                           loop_policy="oneshot", **PLAIN)   # the v1 finish quantizes exactly the hd frames
    assert hd["size"] == pixel["size"] and hd["anchor"] == pixel["anchor"]  # one registration for both finishes
    palette = FP.read_palette(tmp_path / "px" / FF.PALETTE_FILE)
    sources = frames_of(tmp_path / "hd" / "frames")  # the hd frames are exactly what the pixel finish quantized
    held = frames_of(tmp_path / "px" / "frames")
    alone = [FP.render_indices(FP.quantize_sequence([frame], palette, loop=False, alpha_threshold=0.5)[0], palette)
             for frame in sources]

    def flips(frames):  # the clip is still: every changed pixel between neighbours is flicker
        return sum(int((frames[i] != frames[i - 1]).any(-1).sum()) for i in range(1, len(frames))) / (len(frames) - 1)

    assert flips(alone) > 1.0, "the synthetic noise must make per-frame quantization flicker"
    assert flips(held) < 0.5 * flips(alone)
    flicker = pixel["qa"]["flipsPerPair"]
    assert flicker["noise"] < flicker["noisePerFrameNearest"] and flicker["noiseReduction"] >= 0.5
    assert flicker["edge"] < flicker["edgePerFrameNearest"]  # silhouette flicker from the edge alpha noise
    document = json.loads((tmp_path / "px" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    assert next(c for c in document["qa"]["checks"] if c["id"] == "hysteresis")["status"] == "pass"
    assert document["pixel"]["loopPolicy"] == "oneshot" and document["flicker"]["pairs"] == len(held) - 1


def test_pixel_specks_and_indexed_sheet_on_request(clips, tmp_path):
    plain = FF.finish_clip(clips["noisy"], tmp_path / "plain", mode="pixel", target_height=26, colors=16)
    assert plain["indexedSheet"] is None and not (tmp_path / "plain" / FF.SHEET_FILE).exists()
    assert plain["qa"]["specksRemoved"] >= 0 and "lonePixelsFixed" in plain["qa"]
    sheet = FF.finish_clip(clips["noisy"], tmp_path / "sheet", mode="pixel", target_height=26, colors=16,
                           indexed_sheet=True)
    with Image.open(sheet["indexedSheet"]) as image:
        assert image.mode == "P" and image.info.get("transparency") is not None
        width, height = image.size
    document = json.loads((tmp_path / "sheet" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    info = document["indexedSheet"]
    assert (width, height) == (info["columns"] * sheet["size"][0], info["rows"] * sheet["size"][1])


# --------------------------------------------------------------------------- scale-ref

def test_scale_ref_inherits_and_clamps_the_area_correction(clips, tmp_path):
    reference = FF.finish_clip(clips["walk"], tmp_path / "idle", target_height=40, character="fox")
    ref_doc = json.loads((tmp_path / "idle" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    scale0 = ref_doc["scale"]
    assert reference["scale"] == pytest.approx(40 / REST_HEIGHT, rel=1e-6)

    big = FF.finish_clip(clips["big"], tmp_path / "big", target_height=None, scale_ref=tmp_path / "idle",
                         character="fox")
    big_doc = json.loads((tmp_path / "big" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    assert big_doc["scaleRef"]["correction"] < 0.95 and big_doc["scaleRef"]["clamped"] is True
    assert big_doc["scale"] == pytest.approx(scale0 * 0.97, rel=1e-9)
    assert big["status"] == "warn" and "scale-ref-clamp" in big["qa"]["warnings"]
    assert big_doc["scaleSource"] == "scale-ref" and big_doc["scaleRef"]["path"].endswith("finish.json")

    close = FF.finish_clip(clips["close"], tmp_path / "close", target_height=None,
                           scale_ref=tmp_path / "idle" / FF.FINISH_FILE)
    close_doc = json.loads((tmp_path / "close" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    expected = scale0 * math.sqrt(ref_doc["rest"]["areaPx"] / close_doc["rest"]["areaPx"])
    assert close_doc["scale"] == pytest.approx(expected, rel=1e-9) and close_doc["scaleRef"]["clamped"] is False
    assert close["status"] == "needs-visual-review" and not close["qa"]["warnings"]

    flat = FF.finish_clip(clips["big"], tmp_path / "raw", target_height=None, scale_ref=ref_doc, area_norm=False)
    assert flat["scale"] == pytest.approx(scale0, rel=1e-6)
    with pytest.raises(FF.FinishError, match="--strict|strict"):
        FF.finish_clip(clips["big"], tmp_path / "strict", target_height=None, scale_ref=ref_doc, strict=True)
    assert not (tmp_path / "strict").exists()
    with pytest.raises(FF.FinishError, match="character"):
        FF.finish_clip(clips["big"], tmp_path / "other", target_height=None, scale_ref=ref_doc, character="cat")
    with pytest.raises(FF.FinishError, match="exactly one"):
        FF.finish_clip(clips["big"], tmp_path / "both", target_height=40, scale_ref=ref_doc)


# --------------------------------------------------------------------------- lineup

def _cast(clips, root: Path) -> dict[str, Path]:
    FF.finish_clip(clips["walk"], root / "hero", target_height=40, role="hero")
    FF.finish_clip(clips["small"], root / "mob", target_height=36, role="mob")
    FF.finish_clip(clips["boss"], root / "boss", target_height=80, role="boss")
    FF.finish_clip(clips["walk"], root / "wisp", mode="pixel", target_height=40, colors=24, anchor="center",
                   role="spirit")
    FF.finish_clip(clips["small"], root / "brute", target_height=44, role="mob")
    return {name: root / name for name in ("hero", "mob", "boss", "wisp", "brute")}


def test_lineup_checks_the_cast_size_rules(clips, tmp_path):
    cast = _cast(clips, tmp_path / "cast")
    summary = FF.lineup([cast["hero"], cast["mob"], cast["boss"], cast["wisp"]], tmp_path / "lineup.png")
    assert summary["status"] == "pass" and not summary["failed"]
    assert summary["ratios"]["boss"] == pytest.approx(2.0, abs=0.01)
    assert summary["ratios"]["mob"] == pytest.approx(0.9, abs=0.01)
    report = json.loads((tmp_path / "lineup.json").read_text(encoding="utf-8"))
    assert report["schema"] == FF.LINEUP_SCHEMA and report["scales"] == [1, 3]
    assert {row["name"]: row["metric"] for row in report["sets"]}["wisp"] == "area"
    assert_valid_contract(report["qa"], "common", "qaEnvelope")
    with Image.open(tmp_path / "lineup.png") as image:
        width, height = image.size
    assert height > 4 * 80 and width > 3 * 40  # an x1 strip above an x3 strip

    result = run_cli([SCRIPT, "lineup", "--sets", cast["hero"], cast["brute"], f"boss={cast['mob']}",
                      "--out", tmp_path / "bad.png"], "cp1252")
    assert result.returncode == 1
    assert "error: lineup rule failed: brute (mob)" in result.stderr and "rule boss:~2" in result.stderr
    line = json.loads(result.stdout)
    assert line["status"] == "fail" and {item["set"] for item in line["failed"]} == {"brute", "mob"}
    assert (tmp_path / "bad.png").is_file() and (tmp_path / "bad.json").is_file()  # the verdict is still written
    with pytest.raises(FF.FinishError, match="role hero"):
        FF.lineup([cast["mob"], cast["boss"]], tmp_path / "nohero.png")


def test_lineup_reads_a_character_folder_and_checks_one_body_height(clips, tmp_path):
    fox = tmp_path / "fox"
    FF.finish_clip(clips["walk"], fox / "fox-idle", target_height=40, role="hero", character="fox")
    FF.finish_clip(clips["big"], fox / "fox-run", target_height=None, scale_ref=fox / "fox-idle", role="hero",
                   character="fox")
    FF.finish_clip(clips["small"], tmp_path / "rat", target_height=30)  # no role recorded
    summary = FF.lineup([fox, f"mob={tmp_path / 'rat'}"], tmp_path / "cast.png")
    report = json.loads((tmp_path / "cast.json").read_text(encoding="utf-8"))
    rows = {row["name"]: row for row in report["sets"]}
    assert [clip["name"] for clip in rows["fox"]["clips"]] == ["fox-idle", "fox-run"]
    assert rows["fox"]["heightPx"] == pytest.approx(40.0) and rows["rat"]["role"] == "mob"
    assert rows["fox"]["heightSpread"] > FF.SET_HEIGHT_TOLERANCE  # the run clip's rest pose is 3% taller
    assert summary["status"] == "warn" and summary["warnings"] == ["one-height:fox"]


# --------------------------------------------------------------------------- CLI

def test_cli_hd_and_pixel_print_one_ascii_json_line(clips, tmp_path):
    result = run_cli([SCRIPT, "hd", "--frames", clips["walk"], "--output-dir", tmp_path / "hd", "--target-height",
                      "30", "--display-sizes", "1x,2x", "--role", "hero"], "cp1252")
    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii() and len(result.stdout.strip().splitlines()) == 1
    summary = json.loads(result.stdout)
    for key in ("output", "metadata", "targetHeight", "scale", "size", "anchor", "displaySizes", "contact", "qa"):
        assert key in summary
    assert summary["targetHeight"] == 30 and summary["paletteColors"] is None
    assert set(summary["qa"]) >= {"flipsPerPair", "specksRemoved", "alphaBinary"}
    assert Path(summary["contact"]).is_file() and Path(summary["displaySizes"][1]["dir"]).is_dir()
    document = json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))
    assert document["role"] == "hero" and document["qa"]["status"] == "needs-visual-review"
    assert document["engine"]["sourceSize"] == summary["size"] and document["engine"]["sampling"] == "linear"
    assert_valid_contract(document["qa"], "common", "qaEnvelope")

    result = run_cli([SCRIPT, "pixel", "--frames", clips["walk"], "--output-dir", tmp_path / "px",
                      "--target-height", "22", "--colors", "16", "--indexed-sheet", "--loop-policy", "pingpong",
                      "--shade-gap", "0"], "cp1252")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["paletteColors"] == 16 and summary["qa"]["alphaBinary"] is True   # unspaced: exactly --colors
    assert Path(summary["indexedSheet"]).is_file() and Path(summary["palette"]).is_file()
    spaced = run_cli([SCRIPT, "pixel", "--frames", clips["walk"], "--output-dir", tmp_path / "px-spaced",
                      "--target-height", "22", "--colors", "16"], "cp1252")
    assert spaced.returncode == 0, spaced.stderr
    assert json.loads(spaced.stdout)["paletteColors"] < 16                          # the default spaces the palette

    again = run_cli([SCRIPT, "hd", "--frames", clips["walk"], "--output-dir", tmp_path / "hd", "--target-height", "30"])
    assert again.returncode == 1 and again.stderr.startswith("error: output already exists")
    upscale = run_cli([SCRIPT, "hd", "--frames", clips["walk"], "--output-dir", tmp_path / "up",
                       "--target-height", "500"])
    assert upscale.returncode == 1 and "never upscales" in upscale.stderr and not (tmp_path / "up").exists()
    both = run_cli([SCRIPT, "pixel", "--frames", clips["walk"], "--output-dir", tmp_path / "x",
                    "--target-height", "20", "--palette", tmp_path / "px" / "palette.json", "--colors", "8"])
    assert both.returncode == 1 and "drop them" in both.stderr
    usage = run_cli([SCRIPT, "hd", "--frames", clips["walk"], "--output-dir", tmp_path / "y"])
    assert usage.returncode == 2


def test_help_is_ascii_for_every_verb():
    # The top-level --help under cp1252 and cp950 is covered by test_cli_encoding.py.
    for verb in (["hd"], ["pixel"], ["palette", "build"], ["lineup"]):
        result = run_cli([SCRIPT, *verb, "--help"], "cp1252")
        assert result.returncode == 0, result.stderr
        assert result.stdout.isascii() and "--" in result.stdout


# --------------------------------------------------------------------------- bold pixel finish (f-quality)

def _colours(frame):
    return {tuple(pixel) for pixel in frame[..., :3][frame[..., 3] > 0].tolist()}


def test_bold_pixel_enforces_the_palette_outline_and_anchor(clips, tmp_path):
    summary = FF.finish_clip(clips["walk"], tmp_path / "bold", mode="pixel", target_height=30, colors=12)
    document = json.loads((tmp_path / "bold" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    palette = FP.read_palette(tmp_path / "bold" / FF.PALETTE_FILE)
    allowed = {tuple(colour) for colour in palette.rgb.tolist()}
    frames = frames_of(tmp_path / "bold" / "frames")
    for frame in frames:
        assert _colours(frame) <= allowed and len(_colours(frame)) <= 12      # at most N colours, all from the palette
        assert set(np.unique(frame[..., 3]).tolist()) <= {0, 255}
    pixel = document["pixel"]
    assert pixel["downscale"] == "crisp" and pixel["outline"] == "selective" and pixel["outlinePx"] > 0
    assert summary["qa"]["coloursPerFrameMax"] <= 12 and document["targetHeight"] == 30
    check = next(item for item in document["qa"]["checks"] if item["id"] == "palette-fit")
    assert check["status"] == "pass" and check["value"]["coloursPerFrameMax"] <= 12
    # the outline: the opaque pixels touching transparency (4-neighbour) are dark
    rest = frames[0]
    opaque = rest[..., 3] == 255
    padded = np.pad(opaque, 1)
    border = opaque & ~(padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2] & padded[1:-1, 2:])
    lightness = FP.to_oklab(rest[..., :3][border])[:, 0]
    assert np.median(lightness) <= FF.OUTLINE_MAX_L + 0.01
    # the target height includes the outline and the feet anchor sits on the outline's bottom edge
    body = FF.measure_body(rest[..., 3], FF.OUTPUT_MIN_RUN)
    assert abs(body.height - 30) <= 1.5 and abs(body.ground - summary["anchor"][1]) <= 1
    plain = FF.finish_clip(clips["walk"], tmp_path / "plain", mode="pixel", target_height=30, colors=12, **PLAIN)
    assert json.loads((tmp_path / "plain" / FF.FINISH_FILE).read_text(encoding="utf-8"))["pixel"]["outlinePx"] == 0
    assert plain["qa"]["coloursPerFrameMax"] <= 12


def test_crisp_downscale_keeps_a_thin_dark_line_that_box_blurs():
    """A 3-source-px dark line on a light body, reduced 1:4: the box average is a mid tone, crisp keeps it dark."""
    sub = np.zeros((64, 64, 4), np.uint8)
    sub[..., :3], sub[..., 3] = (230, 180, 120), 255
    sub[:, 29:32, :3] = (20, 15, 10)                      # 3 of the 4 sub-pixel columns of output column 7
    clusters = FF.crisp_clusters(sub, 4)
    crisp = FP.from_oklab(FF.crisp_choice(clusters, None))
    assert int(crisp[:, 7].astype(int).sum(-1).max()) < 120   # the line survives as a dark pixel
    box = sub.reshape(16, 4, 16, 4, 4).astype(float).mean(axis=(1, 3))
    assert box[:, 7, :3].sum(-1).min() > 150                  # the area average is a mid tone


def test_crisp_choice_keeps_details_and_holds_last_frames_cluster():
    dark = np.array([[[0.30, 0.02, 0.02]]], np.float32)
    light = np.array([[[0.80, 0.02, 0.05]]], np.float32)
    near = np.array([[[0.74, 0.02, 0.05]]], np.float32)
    assert np.allclose(FF.crisp_choice(FF.Clusters(dark, light, np.array([[0.40]], np.float32)), None), dark)
    close = FF.Clusters(near, light, np.array([[0.45]], np.float32))
    assert np.allclose(FF.crisp_choice(close, None), light)       # no detail gap: the heavier cluster wins
    assert np.allclose(FF.crisp_choice(close, near), near)        # a 45/55 split keeps last frame's choice


def test_outline_shades_follow_the_neighbour_hue():
    palette = FP.as_palette({"colors": ["#ff8020", "#208080", "#4a1c08", "#0c3a3a", "#101010"],
                             "transparent_index": 255})
    shades = FF.outline_shades(palette)
    order = {hex_colour: index for index, hex_colour in enumerate(palette.hex_colors)}
    orange, teal, brown, dark_teal = (order[c] for c in ("#ff8020", "#208080", "#4a1c08", "#0c3a3a"))
    assert shades[orange] == brown and shades[teal] == dark_teal and shades[255] == 255
    idx = np.full((7, 9), 255, np.uint8)
    idx[2:5, 1:4] = orange
    idx[2:5, 5:8] = teal
    outlined, ring = FF.add_outline(idx, palette, "selective", shades)
    assert ring > 0 and outlined[1, 2] == brown and outlined[1, 6] == dark_teal
    assert (outlined[2:5, 1:4] == orange).all()              # the fill is untouched
    dark, _ = FF.add_outline(idx, palette, "dark")
    assert dark[1, 2] == dark[1, 6] == order["#101010"]
    assert FF.add_outline(idx, palette, "none")[1] == 0


def test_lone_pixel_cleanup_keeps_features_and_merges_noise():
    palette = FP.as_palette({"colors": ["#d06020", "#c86828", "#141010"], "transparent_index": 255})
    order = {hex_colour: index for index, hex_colour in enumerate(palette.hex_colors)}
    idx = np.full((7, 7), order["#d06020"], np.uint8)
    idx[2, 2] = order["#c86828"]      # a lone pixel close to its neighbours: noise
    idx[4, 4] = order["#141010"]      # a lone dark pixel: an eye
    cleaned, fixed = FF.clean_lone_pixels(idx, palette)
    assert fixed == 1 and cleaned[2, 2] == order["#d06020"] and cleaned[4, 4] == order["#141010"]


def _min_pair_distance(palette):
    lab = palette.lab.astype(np.float64)
    distance = np.sqrt(((lab[:, None] - lab[None]) ** 2).sum(-1))
    np.fill_diagonal(distance, np.inf)
    return float(distance.min())


def _near_shade_share(frames, limit=0.06):
    """Share of 4-adjacent opaque pixel pairs whose colours differ by a barely visible step (0 < dE < limit)."""
    pairs = near = 0
    for frame in frames:
        lab = FP.to_oklab(frame[..., :3]).astype(np.float64)
        opaque = frame[..., 3] == 255
        for a, b, oa, ob in ((lab[:, :-1], lab[:, 1:], opaque[:, :-1], opaque[:, 1:]),
                             (lab[:-1], lab[1:], opaque[:-1], opaque[1:])):
            both = oa & ob
            step = np.sqrt(((a - b) ** 2).sum(-1))
            pairs += int(both.sum())
            near += int((both & (step > 1e-6) & (step < limit)).sum())
    return near / max(1, pairs)


def test_space_palette_merges_near_shades_and_keeps_extremes_and_reserved():
    """Three oranges 0.015 apart collapse into their usage-weighted mean; the lightest colour, a reserved colour
    and the colours already far apart stay exactly as they were."""
    def rgb(lab):
        return tuple(int(v) for v in FP.from_oklab(np.array([lab]))[0])

    teal = (58, 127, 128)
    teal_near = rgb(FP.to_oklab(np.array([teal], np.uint8))[0] + np.array([0.01, 0.0, 0.0]))
    oranges = [rgb([0.60 + 0.015 * i, 0.12, 0.11]) for i in range(3)]
    colours = [teal, (0, 0, 0), (32, 48, 64), *oranges, teal_near, (248, 248, 248), (255, 255, 255)]
    palette = FF.fp.Palette(tuple(colours), reserved=(True,) + (False,) * (len(colours) - 1),   # the finisher's
                            transparent_index=255)                                        # own module
    frame = np.zeros((4, 8, 4), np.uint8)
    frame[..., 3] = 255
    frame[0, :, :3] = oranges[0]                 # the darkest orange: 8 pixels
    frame[1, :2, :3] = oranges[2]                # the lightest orange: 2 pixels (the middle one is unused)
    frame[1, 2:, :3] = (255, 255, 255)
    frame[2, :, :3] = (0, 0, 0)
    frame[3, :, :3] = teal_near
    spaced, merged = FF.space_palette(palette, [frame], 0.04)
    assert _min_pair_distance(spaced) >= 0.04
    assert merged == len(palette) - len(spaced) == 4          # teal_near, two oranges and #f8f8f8
    assert spaced.colors[0] == teal and spaced.reserved == (True,) + (False,) * (len(spaced) - 1)
    assert teal_near not in spaced.colors                     # merged into the reserved colour, which did not move
    assert (255, 255, 255) in spaced.colors and (248, 248, 248) not in spaced.colors   # the extreme kept its place
    assert (0, 0, 0) in spaced.colors and (32, 48, 64) in spaced.colors                # far apart: exact RGB kept
    learned = np.array([c for c, r in zip(spaced.colors, spaced.reserved) if not r], np.uint8)
    assert np.all(np.diff(FP.to_oklab(learned)[:, 0]) >= 0)  # learned colours stay darkest first
    orange = [c for c in spaced.colors if c not in (teal, (0, 0, 0), (32, 48, 64), (255, 255, 255))]
    assert len(orange) == 1                                   # one orange is left ...
    lightness = float(FP.to_oklab(np.array(orange, np.uint8))[0, 0])
    assert 0.60 < lightness < 0.615                           # ... near the orange the pixels use most
    unchanged, none = FF.space_palette(palette, [frame], 0.0)
    assert unchanged is palette and none == 0


def test_bold_pixel_palette_has_no_near_duplicate_shades(clips, tmp_path):
    """Regression (2026-10-06 fox): k-means gave a shaded body eight shades 0.03 apart, speckle at 4x nearest.
    The bold finish spaces its learned palette; the frames then hold far fewer barely visible shade steps."""
    spaced = FF.finish_clip(clips["walk"], tmp_path / "spaced", mode="pixel", target_height=40, colors=32)
    raw = FF.finish_clip(clips["walk"], tmp_path / "raw", mode="pixel", target_height=40, colors=32, shade_gap=0)
    spaced_palette = FP.read_palette(tmp_path / "spaced" / FF.PALETTE_FILE)
    raw_palette = FP.read_palette(tmp_path / "raw" / FF.PALETTE_FILE)
    assert _min_pair_distance(raw_palette) < FF.SHADE_GAP                 # plain k-means keeps near-duplicate shades
    assert _min_pair_distance(spaced_palette) >= FF.SHADE_GAP
    assert spaced["paletteColors"] < raw["paletteColors"] <= 32
    document = json.loads((tmp_path / "spaced" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    assert document["pixel"]["shadeGap"] == FF.SHADE_GAP
    assert document["pixel"]["shadeMerged"] == raw["paletteColors"] - spaced["paletteColors"]
    assert next(c for c in document["qa"]["checks"] if c["id"] == "palette-fit")["status"] == "pass"
    near_spaced = _near_shade_share(frames_of(tmp_path / "spaced" / "frames"))
    near_raw = _near_shade_share(frames_of(tmp_path / "raw" / "frames"))
    assert near_spaced < 0.5 * near_raw
    for frame in frames_of(tmp_path / "spaced" / "frames"):                  # still one palette, binary alpha
        assert _colours(frame) <= {tuple(c) for c in spaced_palette.rgb.tolist()}
        assert set(np.unique(frame[..., 3]).tolist()) <= {0, 255}
    given = FF.finish_clip(clips["walk"], tmp_path / "given", mode="pixel", target_height=40,
                           palette=tmp_path / "raw" / FF.PALETTE_FILE)        # a given palette is used as it is
    assert given["paletteColors"] == raw["paletteColors"]
    with pytest.raises(FF.FinishError, match="shade gap"):
        FF.finish_clip(clips["walk"], tmp_path / "bad", mode="pixel", target_height=40, shade_gap=0.5)
    with pytest.raises(FF.FinishError, match="pixel finish"):
        FF.finish_clip(clips["walk"], tmp_path / "hd", target_height=40, shade_gap=0.04)


def test_lift_spreads_lightness_and_chroma_and_one_is_identity():
    frame = np.zeros((2, 2, 4), np.uint8)
    frame[..., :3] = [[(200, 120, 80), (90, 60, 50)], [(60, 110, 120), (0, 0, 0)]]
    frame[..., 3] = [[255, 255], [255, 0]]
    assert np.array_equal(FF.lift_frame(frame, 1.0, 1.0, 0.5), frame)
    lifted = FF.lift_frame(frame, 1.2, 1.3, 0.5)
    before, after = FP.to_oklab(frame[..., :3]), FP.to_oklab(lifted[..., :3])
    assert np.sqrt((after[0, 0, 1:] ** 2).sum()) > np.sqrt((before[0, 0, 1:] ** 2).sum())
    assert abs(after[0, 0, 0] - after[0, 1, 0]) > abs(before[0, 0, 0] - before[0, 1, 0])
    assert np.array_equal(lifted[1, 1], frame[1, 1])         # invisible pixels are untouched


def test_canvas_pads_with_the_anchor_kept_and_fits_when_too_small(clips, tmp_path):
    free = FF.finish_clip(clips["walk"], tmp_path / "free", target_height=40)
    cell = FF.finish_clip(clips["walk"], tmp_path / "cell", target_height=40, canvas="64x80")
    assert cell["size"] == [64, 80] and cell["anchor"][0] == 32 and cell["anchor"][1] == 79
    a, b = frames_of(tmp_path / "free" / "frames")[3], frames_of(tmp_path / "cell" / "frames")[3]

    def around_anchor(frame, anchor):   # every visible pixel relative to the anchor
        ys, xs = np.nonzero(frame[..., 3])
        return {(int(x) - anchor[0], int(y) - anchor[1], *map(int, frame[y, x])) for y, x in zip(ys, xs)}

    assert around_anchor(a, free["anchor"]) == around_anchor(b, cell["anchor"])   # the same pixels, the same anchor
    pinned = FF.finish_clip(clips["walk"], tmp_path / "pinned", target_height=40, canvas="64x80",
                            canvas_anchor="30,70")
    assert pinned["anchor"] == [30, 70]
    small = FF.finish_clip(clips["walk"], tmp_path / "small", mode="pixel", target_height=60, canvas="32x40",
                           colors=12)
    assert small["size"] == [32, 40] and "canvas-fit" in small["qa"]["warnings"]
    body = FF.measure_body(frames_of(tmp_path / "small" / "frames")[0][..., 3], FF.OUTPUT_MIN_RUN)
    assert body.height < 40 and small["targetHeight"] < 60
    with pytest.raises(FF.FinishError, match="canvas"):
        FF.finish_clip(clips["walk"], tmp_path / "bad", target_height=40, canvas="64by80")
    with pytest.raises(FF.FinishError, match="needs a canvas"):
        FF.finish_clip(clips["walk"], tmp_path / "bad2", target_height=40, canvas_anchor="3,4")


def test_colour_lock_in_the_finish_keeps_alpha_and_reports_numbers(clips, tmp_path):
    plain = FF.finish_clip(clips["noisy"], tmp_path / "plain", target_height=26)
    locked = FF.finish_clip(clips["noisy"], tmp_path / "locked", target_height=26, colour_lock="rest")
    assert plain["size"] == locked["size"] and plain["anchor"] == locked["anchor"]
    for a, b in zip(frames_of(tmp_path / "plain" / "frames"), frames_of(tmp_path / "locked" / "frames")):
        assert np.array_equal(a[..., 3], b[..., 3])
    document = json.loads((tmp_path / "locked" / FF.FINISH_FILE).read_text(encoding="utf-8"))
    size = document["colourLock"]["sizes"]["1x"]
    assert size["after"]["hueFlipsPerPair"] <= size["before"]["hueFlipsPerPair"]
    assert locked["qa"]["colourLock"]["stats"]["frames"] == 10
    with pytest.raises(FF.FinishError, match="not found"):
        FF.finish_clip(clips["noisy"], tmp_path / "missing", target_height=26, colour_lock=tmp_path / "none.png")


def test_pixel_only_options_are_refused_in_hd_and_the_cli_flags(clips, tmp_path):
    with pytest.raises(FF.FinishError, match="pixel finish"):
        FF.finish_clip(clips["walk"], tmp_path / "x", target_height=30, outline="dark")
    with pytest.raises(FF.FinishError, match="contrast"):
        FF.finish_clip(clips["walk"], tmp_path / "y", mode="pixel", target_height=30, contrast=5.0)
    result = run_cli([SCRIPT, "pixel", "--frames", clips["walk"], "--output-dir", tmp_path / "cli", "--target-height",
                      "24", "--colors", "10", "--outline", "dark", "--canvas", "40x48"], "cp1252")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["size"] == [40, 48] and summary["qa"]["coloursPerFrameMax"] <= 10
    refused = run_cli([SCRIPT, "hd", "--frames", clips["walk"], "--output-dir", tmp_path / "hd", "--target-height",
                       "24", "--outline", "dark"])
    assert refused.returncode == 2                           # --outline is a pixel flag
