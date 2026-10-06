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
                           loop_policy="oneshot")
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
                      "--target-height", "22", "--colors", "16", "--indexed-sheet", "--loop-policy", "pingpong"],
                     "cp1252")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["paletteColors"] == 16 and summary["qa"]["alphaBinary"] is True
    assert Path(summary["indexedSheet"]).is_file() and Path(summary["palette"]).is_file()

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
