"""forge_av (A5): functional ffmpeg probes, decoding, encoders and container checks.

Fixtures are synthetic and deterministic. Tests that run ffmpeg carry the
``ffmpeg`` marker and skip when ffmpeg/ffprobe are not on PATH.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "shared" / "forge_av.py"
VENDORED = [ROOT / "skills" / skill / "scripts" / "forge_av.py" for skill in ("video2dsprite", "generate2dmap")]

_spec = importlib.util.spec_from_file_location("forge_av", CANONICAL)
AV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AV)

FFMPEG = shutil.which("ffmpeg")
HAVE_FFMPEG = bool(FFMPEG and shutil.which("ffprobe"))
_FFMPEG_MARK = pytest.mark.ffmpeg
_NEEDS_FFMPEG = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
QUIET = ["-hide_banner", "-loglevel", "error", "-nostdin"]


@pytest.fixture(scope="session")
def working_ffmpeg():
    """Skip unless this ffmpeg really round-trips VP9 alpha and encodes libx264."""
    info = AV.ffmpeg_info()
    if not all(info["functional"].values()):
        pytest.skip(f"ffmpeg lacks a working VP9-alpha or libx264 path: {info['errors']}")
    return info


def ffmpeg_test(test):
    """Mark ``test`` @ffmpeg; skip it when ffmpeg/ffprobe are missing or not functional."""
    return _FFMPEG_MARK(_NEEDS_FFMPEG(pytest.mark.usefixtures("working_ffmpeg")(test)))


def sprite_frames(count=6, size=(48, 40)):
    """A moving opaque disk and a half-transparent bar; colour is zero where alpha is 0."""
    w, h = size
    yy, xx = np.mgrid[:h, :w]
    frames = []
    for i in range(count):
        frame = np.zeros((h, w, 4), np.uint8)
        disk = (xx - (w // 4 + 3 * i)) ** 2 + (yy - h // 2) ** 2 < (h // 3) ** 2
        frame[disk] = (220, 70, 40, 255)
        frame[(xx >= w - max(2, w // 6)) & ~disk] = (30, 180, 90, 128)
        frames.append(frame)
    return frames


def antialiased_frames(count=4, size=96, supersample=4):
    """Soft-edged sprite: shapes drawn at 4x and downscaled (Pillow premultiplies)."""
    frames = []
    for i in range(count):
        big = Image.new("RGBA", (size * supersample, size * supersample))
        draw = ImageDraw.Draw(big)
        s = supersample
        draw.ellipse([(16 + 2 * i) * s, 12 * s, (80 + 2 * i) * s, 84 * s], fill=(230, 40, 180, 255))
        draw.rectangle([40 * s, (30 + i) * s, 64 * s, 90 * s], fill=(40, 200, 230, 255))
        frame = np.asarray(big.resize((size, size), Image.Resampling.LANCZOS)).copy()
        frame[frame[..., 3] == 0, :3] = 0
        frames.append(frame)
    return frames


def scene_frames(count=24, size=(64, 48)):
    """Opaque smooth texture that completes one cycle over ``count`` frames."""
    w, h = size
    yy, xx = np.mgrid[:h, :w].astype(np.float64)
    frames = []
    for i in range(count):
        phase = 2 * np.pi * i / count
        rgb = np.stack([128 + 90 * np.sin(xx / 7 + phase), 128 + 90 * np.cos(yy / 5 + phase),
                        128 + 60 * np.sin((xx + yy) / 9 + phase)], -1)
        frames.append(np.dstack([rgb.clip(0, 255).astype(np.uint8), np.full((h, w), 255, np.uint8)]))
    return frames


def alpha_gates(decoded, source):
    """Dusk packed-alpha gates: alpha MAE <= 3.5, endpoints 0/255, visible RGB MAE <= 12."""
    decoded, source = np.stack(decoded).astype(int), np.stack(source).astype(int)
    assert np.abs(decoded[..., 3] - source[..., 3]).mean() <= 3.5
    assert np.median(decoded[..., 3][source[..., 3] == 0]) <= 1
    assert np.median(decoded[..., 3][source[..., 3] == 255]) >= 253
    visible = source[..., 3] > 16
    assert np.abs(decoded[..., :3] - source[..., :3])[visible].mean() <= 12


def x264_options(path: Path) -> dict:
    """Settings x264 records in its SEI, e.g. keyint, scenecut and open_gop."""
    match = re.search(rb"x264 - core [^\x00]*", path.read_bytes())
    assert match, f"no x264 SEI in {path}"
    return dict(item.split("=", 1) for item in match.group(0).decode("latin-1").split() if "=" in item)


def box(kind: bytes, payload: bytes = b"", large: bool = False) -> bytes:
    if large:
        return (1).to_bytes(4, "big") + kind + (16 + len(payload)).to_bytes(8, "big") + payload
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


# ---------------------------------------------------------------- processes and discovery

def test_run_decodes_utf8_with_replacement():
    """report v2 P1-1: subprocess output is UTF-8 with replacement, never the console code page."""
    program = "import sys; sys.stdout.buffer.write(b'caf\\xc3\\xa9 \\xff ok')"
    assert AV.run([sys.executable, "-c", program]) == "caf\u00e9 \ufffd ok"


def test_run_raises_ascii_errors_for_failure_timeout_and_missing_tool(tmp_path):
    program = "import sys; sys.stderr.buffer.write('bad \\u2192 input'.encode('utf-8')); sys.exit(3)"
    with pytest.raises(AV.ForgeAVError) as failed:
        AV.run([sys.executable, "-c", program])
    message = str(failed.value)
    assert "status 3" in message and "bad \\u2192 input" in message
    message.encode("ascii")
    with pytest.raises(AV.ForgeAVError, match="timed out"):
        AV.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    with pytest.raises(AV.ForgeAVError, match="not found"):
        AV.run([str(tmp_path / "no-such-tool.exe")])


@ffmpeg_test
def test_capabilities_functional(tmp_path, monkeypatch):
    info = AV.ffmpeg_info()
    assert info["functional"] == {"vp9_alpha": True, "libx264": True}, info["errors"]
    assert info["ffmpeg"] and info["ffprobe"] and info["version"] and not info["errors"]
    info["functional"]["vp9_alpha"] = False
    assert AV.ffmpeg_info()["functional"]["vp9_alpha"], "callers must not mutate the cache"
    # The check is a real round trip: the native VP9 decoder drops alpha and fails it.
    AV._check_vp9_alpha(FFMPEG, tmp_path)
    with pytest.raises(AV.ForgeAVError, match="did not survive a vp9 decode"):
        AV._check_vp9_alpha(FFMPEG, tmp_path, decoder="vp9")
    monkeypatch.setattr(AV.shutil, "which", lambda name: None)
    missing = AV.ffmpeg_info()
    assert missing["functional"] == {"vp9_alpha": False, "libx264": False}
    assert missing["ffmpeg"] is None and set(missing["errors"]) == {"ffmpeg", "ffprobe"}


# ---------------------------------------------------------------- probing and decoding

@ffmpeg_test
def test_probe_reports_audio_stream(tmp_path):
    """report v2 3.5: Grok returned an AAC track and an MJPEG cover with the clip."""
    cover = tmp_path / "cover.png"
    Image.new("RGB", (128, 128), (10, 200, 30)).save(cover)
    clip = tmp_path / "grok-like.mp4"
    subprocess.run([FFMPEG, *QUIET, "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=24:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", "-i", str(cover),
                    "-map", "0:v", "-map", "1:a", "-map", "2:v", "-c:v:0", "libx264", "-pix_fmt:v:0", "yuv420p",
                    "-c:a", "aac", "-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic", str(clip)],
                   check=True, capture_output=True)
    info = AV.probe(clip)
    assert (info["codec"], info["width"], info["height"]) == ("h264", 64, 48)
    assert (info["fps_rational"], info["nb_frames"], info["nb_frames_source"]) == ("24/1", 24, "container")
    assert info["audio_streams"] == 1 and info["audio"][0]["codec"] == "aac"
    assert info["attached_pics"] == 1
    assert info["duration"] == pytest.approx(1.0, abs=0.002)  # the video, not the 1.5 s audio
    assert not info["vp9_alpha"] and not info["has_alpha"]
    assert AV.packet_count(clip) == 24 and abs(AV.duration_ms(clip) - 1000) <= 2
    frames = AV.decode_rgba(clip)
    assert len(frames) == 24 and frames[0].shape == (48, 64, 4)  # never the 128x128 cover
    assert all((frame[..., 3] == 255).all() for frame in frames)


@ffmpeg_test
def test_vp9_alpha_roundtrip(tmp_path):
    frames = sprite_frames()
    movie = tmp_path / "sprite.webm"
    result = AV.encode_vp9_alpha(frames, movie, "24/1")
    assert (result["alpha"], result["frameCount"], result["keyframes"], result["fps"]) == ("native-vp9", 6, [0], "24/1")
    assert result["sha256"] == hashlib.sha256(movie.read_bytes()).hexdigest()
    info = AV.probe(movie)
    assert info["vp9_alpha"] and info["has_alpha"] and info["codec"] == "vp9"
    assert (info["nb_frames"], info["fps_rational"], info["width"], info["height"]) == (6, "24/1", 48, 40)
    tags = json.loads(AV.run(["ffprobe", "-v", "error", "-select_streams", "0", "-show_entries",
                              "stream=color_space,color_range", "-of", "json", str(movie)]))["streams"][0]
    assert (tags["color_space"], tags["color_range"]) == ("bt709", "tv")
    alpha_gates(AV.decode_rgba(movie), frames)
    # The native decoder path (alpha="off") yields opaque frames.
    assert all((frame[..., 3] == 255).all() for frame in AV.decode_rgba(movie, alpha="off"))
    paths = AV.extract_frames(movie, tmp_path / "raw 100%")  # '%' must not reach image2 as a pattern
    assert [p.name for p in paths] == [f"frame_{i:06d}.png" for i in range(6)]
    assert all(p.parent == tmp_path / "raw 100%" for p in paths)
    with Image.open(paths[2]) as extracted:
        assert extracted.mode == "RGBA"
        assert np.abs(np.asarray(extracted)[..., 3].astype(int) - frames[2][..., 3]).mean() <= 3.5


@ffmpeg_test
def test_vp8_alpha_decodes_with_libvpx(tmp_path):
    """Older WebM tools write VP8 alpha; the native vp8 decoder drops it too."""
    frames = sprite_frames(3)
    movie = tmp_path / "vp8.webm"
    made = subprocess.run([FFMPEG, *QUIET, "-f", "rawvideo", "-pix_fmt", "rgba", "-video_size", "48x40",
                           "-framerate", "24", "-i", "pipe:0", "-c:v", "libvpx", "-pix_fmt", "yuva420p",
                           "-auto-alt-ref", "0", "-crf", "10", "-b:v", "1M", str(movie)],
                          input=b"".join(frame.tobytes() for frame in frames), capture_output=True)
    if made.returncode:
        pytest.skip("this ffmpeg has no libvpx VP8 encoder")
    info = AV.probe(movie)
    assert (info["codec"], info["alpha_mode"], info["vp9_alpha"], info["has_alpha"]) == ("vp8", True, False, True)
    alpha_gates(AV.decode_rgba(movie), frames)


@ffmpeg_test
def test_alpha_modes_and_decode_limits(tmp_path):
    clip = tmp_path / "scene.mp4"
    AV.encode_h264_loop(scene_frames(24), clip, 24)
    with pytest.raises(ValueError, match="no alpha channel"):
        AV.decode_rgba(clip, alpha="on")
    with pytest.raises(ValueError, match="alpha must be one of"):
        AV.decode_rgba(clip, alpha="maybe")
    with pytest.raises(ValueError, match="max_pixels"):
        AV.decode_rgba(clip, max_pixels=64 * 48 * 10)
    stream = AV.iter_rgba(clip)
    first = next(stream)
    first[0, 0] = 0  # frames are writable
    stream.close()  # leaving early kills ffmpeg quietly
    tail = AV.decode_rgba(clip, start=21.5 / 24)
    assert len(tail) == 2 and all((frame[..., 3] == 255).all() for frame in tail)


@ffmpeg_test
def test_extract_frames_timing_and_no_clobber(tmp_path):
    clip = tmp_path / "scene.mp4"
    AV.encode_h264_loop(scene_frames(24), clip, 24)
    every = AV.extract_frames(clip, tmp_path / "all")
    assert len(every) == 24
    with Image.open(every[0]) as first:
        assert (first.mode, first.size) == ("RGB", (64, 48))
    with pytest.raises(FileExistsError):
        AV.extract_frames(clip, tmp_path / "all")
    assert len(list((tmp_path / "all").glob("frame_*.png"))) == 24
    assert len(AV.extract_frames(clip, tmp_path / "half", fps=12)) == 12
    trimmed = AV.extract_frames(clip, tmp_path / "trim", start=5.5 / 24, duration=6 / 24)
    assert len(trimmed) == 6
    reference = [frame[..., :3].astype(int) for frame in AV.decode_rgba(clip)]
    with Image.open(trimmed[0]) as image:
        pixels = np.asarray(image).astype(int)
    errors = [np.abs(pixels - reference[i]).mean() for i in (5, 6, 7)]
    assert errors[1] < 1 and errors[1] < min(errors[0], errors[2])
    with pytest.raises(AV.ForgeAVError):
        AV.extract_frames(clip, tmp_path / "late", start=5)
    assert not list((tmp_path / "late").iterdir())


@ffmpeg_test
def test_display_rotation_swaps_probe_and_decode_size(tmp_path):
    clip = tmp_path / "scene.mp4"
    AV.encode_h264_loop(scene_frames(4), clip, 24)
    rotated = tmp_path / "rotated.mp4"
    made = subprocess.run([FFMPEG, *QUIET, "-display_rotation", "90", "-i", str(clip), "-c", "copy", str(rotated)],
                          capture_output=True)
    if made.returncode:
        pytest.skip("this ffmpeg cannot write display rotation (needs 7.0+)")
    info = AV.probe(rotated)
    assert info["rotation"] in (90, 270) and (info["width"], info["height"]) == (48, 64)
    assert AV.decode_rgba(rotated)[0].shape == (64, 48, 4)


# ---------------------------------------------------------------- encoders

def test_packed_geometry_pads_each_half_to_even():
    geometry = AV.packed_geometry(403, 407)
    assert geometry == {"layout": "rgb-left-alpha-right", "width": 403, "height": 407,
                        "halfWidth": 404, "halfHeight": 408, "encodedSize": [808, 408]}
    assert AV.packed_geometry(320, 240)["encodedSize"] == [640, 240]
    packed = np.zeros((408, 808, 3), np.uint8)
    packed[:407, :403] = (10, 20, 30)
    packed[:407, 404:807] = 200
    rgba = AV.unpack_packed_alpha(packed, geometry)
    assert rgba.shape == (407, 403, 4)
    assert (rgba[..., :3] == (10, 20, 30)).all() and (rgba[..., 3] == 200).all()
    with pytest.raises(ValueError):
        AV.unpack_packed_alpha(packed[:, :800], geometry)


@ffmpeg_test
def test_packed_alpha_even_padding(tmp_path):
    """Dusk iOS contract: a 403x407 clip packs into 404x408 halves (808x408 MP4)."""
    frames = sprite_frames(3, (403, 407))
    movie = tmp_path / "odd.mp4"
    result = AV.encode_packed_alpha(frames, movie, 24)
    assert (result["halfWidth"], result["halfHeight"], result["encodedSize"]) == (404, 408, [808, 408])
    assert (result["width"], result["height"], result["layout"]) == (403, 407, "rgb-left-alpha-right")
    info = AV.probe(movie)
    assert (info["codec"], info["width"], info["height"], info["pix_fmt"]) == ("h264", 808, 408, "yuv420p")
    tags = json.loads(AV.run(["ffprobe", "-v", "error", "-select_streams", "0", "-show_entries",
                              "stream=color_space,color_range,color_primaries,color_transfer",
                              "-of", "json", str(movie)]))["streams"][0]
    assert tags == {"color_range": "tv", "color_space": "bt709", "color_transfer": "bt709",
                    "color_primaries": "bt709"}
    assert AV.moov_before_mdat(movie)
    alpha_gates([AV.unpack_packed_alpha(frame, result) for frame in AV.decode_rgba(movie)], frames)


ENCODERS = {
    "vp9": (AV.encode_vp9_alpha, "clip.webm", sprite_frames),
    "packed": (AV.encode_packed_alpha, "clip.mp4", sprite_frames),
    "loop": (AV.encode_h264_loop, "clip.mp4", scene_frames),
}


@ffmpeg_test
@pytest.mark.parametrize("fps", ["30000/1001", "24000/1001"])
@pytest.mark.parametrize("encoder", sorted(ENCODERS))
def test_rational_fps_exact_duration(tmp_path, encoder, fps):
    encode, name, make = ENCODERS[encoder]
    movie = tmp_path / name
    result = encode(make(10, (32, 32)), movie, fps)
    rate = Fraction(fps)
    expected = float(10 * 1000 / rate)
    assert result["fps"] == AV.probe(movie)["fps_rational"] == f"{rate.numerator}/{rate.denominator}"
    assert result["durationMs"] == pytest.approx(expected)
    assert abs(AV.duration_ms(movie) - expected) <= 2


@ffmpeg_test
def test_premultiplied_scaling_no_dark_halo(tmp_path):
    """Dusk premult_check: a red-255 disk downscaled 64 -> 23; straight scaling darkens its edge."""
    yy, xx = np.mgrid[:64, :64]
    disk = np.zeros((64, 64, 4), np.uint8)
    disk[(xx - 32) ** 2 + (yy - 32) ** 2 < 20 ** 2] = (255, 40, 40, 255)
    png = tmp_path / "disk.png"
    Image.fromarray(disk, "RGBA").save(png)
    chain = AV.premultiplied_scale_filter(23, 23)
    assert chain.startswith("format=gbrap16le,premultiply=")
    assert chain.index("premultiply=") < chain.index("scale=23:23:flags=lanczos") < chain.index("unpremultiply=")

    def soft(frame):
        return (frame[..., 3] > 20) & (frame[..., 3] < 235)

    (premultiplied,) = AV.decode_rgba(png, size=(23, 23))
    straight = np.frombuffer(subprocess.run(
        [FFMPEG, *QUIET, "-i", str(png), "-vf", "format=rgba,scale=23:23:flags=lanczos",
         "-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1"], check=True, capture_output=True).stdout,
        np.uint8).reshape(23, 23, 4)
    assert soft(premultiplied).sum() >= 20
    assert premultiplied[soft(premultiplied)][:, 0].mean() >= 250
    assert straight[soft(straight)][:, 0].mean() < 150
    with pytest.raises(ValueError):
        AV.premultiplied_scale_filter(0, 23)


def test_edge_bleed_fills_only_the_transparent_ring():
    frame = np.zeros((12, 12, 4), np.uint8)
    frame[4:8, 4:8] = (200, 40, 10, 255)
    frame[4, 8] = (0, 0, 250, 64)  # a soft edge pixel keeps its own colour
    frame[0, 11] = (99, 99, 99, 0)  # stray colour under alpha 0, far from the subject
    bled = AV._edge_bleed(frame)
    assert np.array_equal(bled[..., 3], frame[..., 3])
    visible = frame[..., 3] > 0
    assert np.array_equal(bled[visible], frame[visible])
    assert tuple(bled[6, 3, :3]) == (200, 40, 10)  # next to the opaque block
    assert tuple(bled[6, 4 - AV.EDGE_BLEED_PX, :3]) == (200, 40, 10)
    assert not bled[:, : 4 - AV.EDGE_BLEED_PX, :3].any()  # beyond the ring stays black
    assert not bled[0, 11, :3].any()


@ffmpeg_test
def test_edge_bleed_keeps_soft_edges_true(tmp_path, monkeypatch):
    """4:2:0 chroma along soft edges must not average in the black under alpha 0."""
    frames = antialiased_frames()
    soft = (np.stack(frames)[..., 3] >= 32) & (np.stack(frames)[..., 3] <= 223)

    def edge_error(tag):
        webm = AV.encode_vp9_alpha(frames, tmp_path / f"{tag}.webm", 24)
        packed = AV.encode_packed_alpha(frames, tmp_path / f"{tag}.mp4", 24)
        decoded = (AV.decode_rgba(tmp_path / webm["file"]),
                   [AV.unpack_packed_alpha(f, packed) for f in AV.decode_rgba(tmp_path / packed["file"])])
        source = np.stack(frames)[..., :3].astype(int)
        return [np.abs(np.stack(d)[..., :3].astype(int) - source)[soft].mean() for d in decoded]

    bled = edge_error("bled")
    monkeypatch.setattr(AV, "_edge_bleed", lambda f: np.where(f[..., 3:] == 0, 0, f).astype(np.uint8))
    zeroed = edge_error("zeroed")
    for with_bleed, without in zip(bled, zeroed):
        assert with_bleed <= 9 and with_bleed * 2.5 <= without


@ffmpeg_test
def test_h264_loop_gop_aligned_and_closed(tmp_path):
    """hd2d seam-codec: loop GOPs must end exactly at the wrap; keyint divides the loop."""
    frames = scene_frames(24)
    aligned = AV.encode_h264_loop(frames, tmp_path / "k8.mp4", 24, keyint=8)
    assert aligned["keyframes"] == [0, 8, 16] and aligned["closedGop"]
    options = x264_options(tmp_path / "k8.mp4")
    assert (options["keyint"], options["scenecut"], options["open_gop"]) == ("8", "0", "0")
    single = AV.encode_h264_loop(frames, tmp_path / "one.mp4", 24)
    assert (single["keyint"], single["keyframes"]) == (24, [0])
    assert x264_options(tmp_path / "one.mp4")["keyint"] == "24"
    AV.encode_h264_loop(frames, tmp_path / "open.mp4", 24, keyint=12, closed_gop=False)
    assert x264_options(tmp_path / "open.mp4")["open_gop"] == "1"
    with pytest.raises(ValueError, match="does not divide"):
        AV.encode_h264_loop(frames, tmp_path / "bad.mp4", 24, keyint=7)
    translucent = [frame.copy() for frame in frames]
    translucent[3][0, 0, 3] = 128
    with pytest.raises(ValueError, match="frame 3 has transparency"):
        AV.encode_h264_loop(translucent, tmp_path / "alpha.mp4", 24)
    with pytest.raises(ValueError, match="even dimensions"):
        AV.encode_h264_loop(scene_frames(4, (63, 48)), tmp_path / "odd.mp4", 24)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["k8.mp4", "one.mp4", "open.mp4"]


@ffmpeg_test
def test_fps_capped_at_60(tmp_path):
    """Dusk iOS: 60 frames at 125 fps (0.480 s) became 29 frames at 60 fps (0.4833 s)."""
    result = AV.encode_packed_alpha(sprite_frames(60, (16, 16)), tmp_path / "fx.mp4", 125)
    assert (result["fps"], result["frameCount"], result["fpsCapped"], result["inputFps"]) == ("60/1", 29, True, "125/1")
    assert result["inputIndices"][:7] == [0, 2, 4, 6, 8, 10, 13] and result["inputIndices"][-1] == 58
    assert result["durationMs"] == pytest.approx(29000 / 60)
    assert AV.probe(tmp_path / "fx.mp4")["fps_rational"] == "60/1"
    assert abs(AV.duration_ms(tmp_path / "fx.mp4") - 29000 / 60) <= 2
    tier = AV.encode_vp9_alpha(sprite_frames(24, (16, 16)), tmp_path / "tier.webm", 24, max_fps=12)
    assert (tier["fps"], tier["frameCount"], tier["inputIndices"]) == ("12/1", 12, list(range(0, 24, 2)))
    with pytest.raises(ValueError, match="cannot exceed 60"):
        AV.encode_vp9_alpha(sprite_frames(2, (16, 16)), tmp_path / "fast.webm", 24, max_fps=120)


def test_encoder_arguments_are_validated_before_ffmpeg_runs(tmp_path):
    frames = sprite_frames(4, (16, 16))
    out = tmp_path / "clip.webm"
    for kwargs, error in [(dict(fps_rational=0), ValueError), (dict(fps_rational="fast"), ValueError),
                          (dict(fps_rational=True), TypeError), (dict(fps_rational=24, crf=99), ValueError),
                          (dict(fps_rational=24, keyint=3), ValueError), (dict(fps_rational=24, keyint=2.0), TypeError),
                          (dict(fps_rational=24, max_fps=90), ValueError)]:
        with pytest.raises(error):
            AV.encode_vp9_alpha(frames, out, **kwargs)
    with pytest.raises(ValueError, match="no frames"):
        AV.encode_packed_alpha([], out, 24)
    with pytest.raises(ValueError, match="uint8"):
        AV.encode_packed_alpha([np.zeros((4, 4, 4))], out, 24)
    assert not out.exists()


@ffmpeg_test
def test_encoders_never_clobber_and_leave_no_partial_output(tmp_path):
    taken = tmp_path / "taken.webm"
    taken.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        AV.encode_vp9_alpha(sprite_frames(2), taken, 24)
    assert taken.read_bytes() == b"keep"
    mixed = sprite_frames(3) + sprite_frames(1, (40, 40))  # fails after ffmpeg has started
    for name, encode in (("a.webm", AV.encode_vp9_alpha), ("b.mp4", AV.encode_packed_alpha)):
        with pytest.raises(ValueError, match="every frame must be 48x40"):
            encode(mixed, tmp_path / name, 24)
    with pytest.raises(AV.ForgeAVError, match="timed out"):
        AV.encode_vp9_alpha(sprite_frames(2), tmp_path / "slow.webm", 24, timeout=0.001)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["taken.webm"]


@ffmpeg_test
def test_encodes_are_deterministic_and_accept_frame_directories(tmp_path):
    frames = sprite_frames(4)
    folder = tmp_path / "frames"
    folder.mkdir()
    for i, frame in enumerate(frames):  # pose0, pose5, pose10, pose15: natural, not lexical, order
        Image.fromarray(frame, "RGBA").save(folder / f"pose{i * 5}.png")
    first = AV.encode_vp9_alpha(frames, tmp_path / "a.webm", 24)
    assert AV.encode_vp9_alpha(folder, tmp_path / "b.webm", 24)["sha256"] == first["sha256"]
    packed = [AV.encode_packed_alpha(folder, tmp_path / f"p{i}.mp4", 24)["sha256"] for i in range(2)]
    loops = [AV.encode_h264_loop(scene_frames(8), tmp_path / f"l{i}.mp4", 24)["sha256"] for i in range(2)]
    assert packed[0] == packed[1] and loops[0] == loops[1]


# ---------------------------------------------------------------- container checks

def test_moov_before_mdat_reads_box_headers(tmp_path):
    fast, slow, tail = tmp_path / "fast.mp4", tmp_path / "slow.mp4", tmp_path / "tail.mp4"
    fast.write_bytes(box(b"ftyp", b"isom") + box(b"moov", b"m" * 20) + box(b"mdat", b"d" * 50, large=True))
    slow.write_bytes(box(b"ftyp", b"isom") + box(b"mdat", b"d" * 50) + box(b"moov", b"m" * 20))
    tail.write_bytes(box(b"ftyp") + box(b"moov") + (0).to_bytes(4, "big") + b"mdat" + b"d" * 10)
    assert AV.moov_before_mdat(fast) and not AV.moov_before_mdat(slow) and AV.moov_before_mdat(tail)
    truncated, webm = tmp_path / "truncated.mp4", tmp_path / "clip.webm"
    truncated.write_bytes(box(b"ftyp") + (100).to_bytes(4, "big") + b"moov" + b"m")
    webm.write_bytes(bytes.fromhex("1a45dfa3") + bytes(28))
    for bad in (truncated, webm):
        with pytest.raises(ValueError):
            AV.moov_before_mdat(bad)


@ffmpeg_test
def test_faststart_detected(tmp_path):
    fast = tmp_path / "fast.mp4"
    AV.encode_h264_loop(scene_frames(4, (32, 32)), fast, 24)
    assert AV.moov_before_mdat(fast)
    slow = tmp_path / "slow.mp4"  # a plain remux writes moov after mdat
    subprocess.run([FFMPEG, *QUIET, "-i", str(fast), "-c", "copy", str(slow)], check=True, capture_output=True)
    assert not AV.moov_before_mdat(slow)


@ffmpeg_test
def test_duration_and_packet_count(tmp_path):
    loop, webm = tmp_path / "loop.mp4", tmp_path / "loop.webm"
    AV.encode_h264_loop(scene_frames(12, (32, 32)), loop, 12)
    AV.encode_vp9_alpha(sprite_frames(12, (32, 32)), webm, 12)
    for movie in (loop, webm):
        assert AV.packet_count(movie) == 12
        assert abs(AV.duration_ms(movie) - 1000) <= 2
        assert AV.timestamps_increasing(movie)


def test_timestamps_increasing_uses_dts_not_pts(tmp_path, monkeypatch):
    """Dusk qa-video-contract: B-frames reorder PTS in packet order; DTS must rise."""
    clip = tmp_path / "clip.mp4"

    def packets(*rows):
        monkeypatch.setattr(AV, "_video_packets", lambda path: list(rows))

    packets({"pts_time": "0.00", "dts_time": "-0.08"}, {"pts_time": "0.12", "dts_time": "-0.04"},
            {"pts_time": "0.04", "dts_time": "0.00"})
    assert AV.timestamps_increasing(clip)
    packets({"pts_time": "0.00", "dts_time": "0.00"}, {"pts_time": "0.04", "dts_time": "0.00"})
    assert not AV.timestamps_increasing(clip)
    packets({"pts_time": "0.00"}, {"pts_time": "0.04"})  # no DTS: PTS is the clock
    assert AV.timestamps_increasing(clip)
    packets({"flags": "K__"})
    assert not AV.timestamps_increasing(clip)
    packets()
    assert not AV.timestamps_increasing(clip)


def test_vendored_copies_match_canonical():
    digest = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    for vendored in VENDORED:
        assert hashlib.sha256(vendored.read_bytes()).hexdigest() == digest, vendored


def test_unlink_settled_retries_while_windows_still_holds_the_file(tmp_path, monkeypatch):
    # GitHub's Windows runners lock fresh files briefly (antivirus, a killed ffmpeg), so
    # cleanup must retry instead of failing with WinError 32 and leaving a partial file behind.
    target = tmp_path / ".clip.webm.0123456789ab.partial"
    target.write_bytes(b"partial")
    held = {"left": 2}
    real_unlink = Path.unlink

    def held_unlink(self, missing_ok=False):
        if self == target and held["left"]:
            held["left"] -= 1
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", held_unlink)
    monkeypatch.setattr(AV.os, "name", "nt")
    monkeypatch.setattr(AV.time, "sleep", lambda seconds: None)
    AV._unlink_settled(target)
    assert not target.exists() and held["left"] == 0

    target.write_bytes(b"partial")
    held["left"] = 5
    with pytest.raises(PermissionError):
        AV._unlink_settled(target, attempts=3)
