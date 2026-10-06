"""forge_av on forge_core (D30): frame files load through forge_core.load_rgba and finished encodes are
published with forge_core.publish_file_no_replace; forge_core is imported as forge_av's sibling in every
skill that vendors forge_av. Synthetic and offline: ffmpeg is never run here.
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import REPO_ROOT, SKILLS_DIR, load_shared

AV = load_shared("forge_av")
VENDORING_SKILLS = ("video2dsprite", "generate2dmap")


def _png16_grey(path: Path, values: np.ndarray) -> None:
    """A hand-encoded 16-bit greyscale PNG (colour type 0)."""
    height, width = values.shape
    rows = values.astype(">u2")
    raw = b"".join(b"\0" + rows[y].tobytes() for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 16, 0, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw))
                     + chunk(b"IEND", b""))


def test_forge_av_imports_its_sibling_forge_core():
    """Every skill that ships forge_av ships forge_core beside it, and forge_av imports exactly that copy."""
    for skill in VENDORING_SKILLS:
        scripts = SKILLS_DIR / skill / "scripts"
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import forge_av, forge_core; "
                "print(forge_av.forge_core.__file__); print(forge_av.forge_core is forge_core)")
        done = subprocess.run([sys.executable, "-c", code, str(scripts)], capture_output=True, encoding="utf-8",
                              errors="replace", cwd=str(REPO_ROOT / "tests"), check=True,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        location, same = done.stdout.splitlines()
        assert Path(location).resolve() == (scripts / "forge_core.py").resolve() and same == "True", skill


def test_frame_files_load_through_load_rgba(tmp_path):
    """A5's request: 16-bit grey keeps its high byte (Pillow's convert clipped it), palette tRNS and LA
    keep their alpha, and an animated file is refused, exactly as forge_core.load_rgba decides."""
    ramp = np.linspace(0, 65535, 64).astype(np.uint16).reshape(4, 16)
    grey16 = tmp_path / "grey16.png"
    _png16_grey(grey16, ramp)
    palette = Image.frombytes("P", (3, 1), bytes([0, 1, 2]))
    palette.putpalette([255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * 759)
    palette.save(tmp_path / "palette.png", transparency=1)
    Image.fromarray(np.array([[[10, 0], [90, 128], [250, 255]]], np.uint8)).save(tmp_path / "la.png")
    for path in (grey16, tmp_path / "palette.png", tmp_path / "la.png"):
        frame = AV._load_frame(path)
        expected = np.asarray(AV.forge_core.load_rgba(path)[0])
        assert frame.flags["C_CONTIGUOUS"] and frame.dtype == np.uint8 and np.array_equal(frame, expected), path
        assert np.array_equal(AV._load_frame(str(path)), expected)
    grey = AV._load_frame(grey16)[..., 0]
    assert np.array_equal(grey, (ramp >> 8).astype(np.uint8)) and grey.max() == 255 and grey.min() == 0
    assert AV._load_frame(tmp_path / "palette.png")[0, :, 3].tolist() == [255, 0, 255]
    animated = tmp_path / "walk.png"
    first, second = Image.new("RGBA", (4, 4), (1, 2, 3, 255)), Image.new("RGBA", (4, 4), (9, 8, 7, 255))
    first.save(animated, save_all=True, append_images=[second], duration=100)
    with pytest.raises(ValueError, match="animated"):
        AV._load_frame(animated)
    rgb = np.zeros((2, 3, 3), np.uint8)  # in-memory frames keep their behaviour: RGB becomes opaque
    assert AV._load_frame(rgb)[..., 3].tolist() == [[255] * 3] * 2
    assert np.array_equal(AV._load_frame(Image.fromarray(rgb)), AV._load_frame(rgb))


def test_frame_directories_feed_the_encoder_through_load_rgba(tmp_path):
    """The encoders' input (_Clip) reads a folder of 16-bit PNG frames with the corrected values."""
    folder = tmp_path / "frames"
    folder.mkdir()
    values = [np.full((4, 6), level, np.uint16) for level in (0, 300, 40000, 65535)]
    for index, plane in enumerate(values):
        _png16_grey(folder / f"frame_{index}.png", plane)
    clip = AV._Clip(folder, "24/1", 60)
    frames = [frame for _, frame in clip.frames()]
    assert [int(frame[0, 0, 0]) for frame in frames] == [0, 1, 156, 255]
    assert clip.size == (6, 4) and clip.count == 4


def _fake_encoder(monkeypatch, frames: int, keyint: int, payload: bytes = b"encoded"):
    """Stand-ins for ffmpeg and ffprobe: _feed writes the partial file, _packets reports keyframes."""
    def feed(argv, chunks, timeout):
        for _ in chunks:
            pass
        Path(argv[-1]).write_bytes(payload)

    def packets(path, index):
        return [{"pts_time": f"{i / 24:.6f}", "flags": "K__" if i % keyint == 0 else "___"} for i in range(frames)]

    monkeypatch.setattr(AV, "_feed", feed)
    monkeypatch.setattr(AV, "_packets", packets)


def test_encodes_are_published_with_forge_core(tmp_path, monkeypatch):
    """D30: _encode_file hands the verified partial file to forge_core.publish_file_no_replace."""
    _fake_encoder(monkeypatch, frames=4, keyint=2)
    published = []
    original = AV.forge_core.publish_file_no_replace

    def spy(src, dst):
        published.append((Path(src).name, Path(dst)))
        return original(src, dst)

    monkeypatch.setattr(AV.forge_core, "publish_file_no_replace", spy)
    target = tmp_path / "out" / "clip.mp4"
    result, keyframes = AV._encode_file(target, "mp4", ["ffmpeg"], iter([b"a", b"b"]), 4, 2, 5)
    assert result == target and target.read_bytes() == b"encoded" and keyframes == [0, 2]
    assert len(published) == 1 and published[0][1] == target
    assert published[0][0].startswith(".clip.mp4.") and published[0][0].endswith(".partial")
    assert sorted(path.name for path in target.parent.iterdir()) == ["clip.mp4"]  # partial and stage removed


def test_a_target_that_appears_during_encoding_is_never_replaced(tmp_path, monkeypatch):
    target = tmp_path / "clip.mp4"
    _fake_encoder(monkeypatch, frames=2, keyint=2)
    original = AV._feed

    def racing_feed(argv, chunks, timeout):
        original(argv, chunks, timeout)
        target.write_bytes(b"someone else's")  # another process wins the name mid-encode

    monkeypatch.setattr(AV, "_feed", racing_feed)
    with pytest.raises(FileExistsError, match="appeared during encoding; refusing to replace it"):
        AV._encode_file(target, "mp4", ["ffmpeg"], iter([b"a"]), 2, 2, 5)
    assert target.read_bytes() == b"someone else's"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["clip.mp4"]
    target.unlink()
    target.write_bytes(b"old")
    with pytest.raises(FileExistsError, match="never replaces"):  # the up-front check is unchanged
        AV._encode_file(target, "mp4", ["ffmpeg"], iter([b"a"]), 2, 2, 5)


def test_failed_verification_publishes_nothing(tmp_path, monkeypatch):
    _fake_encoder(monkeypatch, frames=3, keyint=2)
    target = tmp_path / "clip.webm"
    with pytest.raises(AV.ForgeAVError, match="packets for 4 frames"):
        AV._encode_file(target, "webm", ["ffmpeg"], iter([]), 4, None, 5)
    with pytest.raises(AV.ForgeAVError, match="do not follow keyint 3"):
        AV._encode_file(target, "webm", ["ffmpeg"], iter([]), 3, 3, 5)
    assert not list(tmp_path.iterdir())


def test_forge_av_refuses_a_foreign_forge_core(tmp_path):
    """A stale or foreign forge_core beside forge_av is refused at import, not half-used.

    r3-platform finding 5: the folder name is non-ASCII on purpose, so the child's traceback always holds
    non-ASCII text. The output is decoded as UTF-8 with errors=replace (Appendix D); decoding it with the
    locale codec (text=True) raised UnicodeDecodeError under cp950 and left stderr None."""
    folder = tmp_path / "測試 ü"
    folder.mkdir()
    (folder / "forge_core.py").write_text('FORGE_CORE_API_VERSION = "2.0"\n', encoding="utf-8")
    (folder / "forge_av.py").write_bytes((REPO_ROOT / "shared" / "forge_av.py").read_bytes())
    done = subprocess.run([sys.executable, "-c", "import forge_av"], capture_output=True, encoding="utf-8",
                          errors="replace", cwd=str(folder),
                          env={**os.environ, "PYTHONPATH": str(folder), "PYTHONIOENCODING": "utf-8"})
    assert done.returncode != 0 and "forge_av needs forge_core API version 1.x" in done.stderr
    assert "測試 ü" in done.stderr  # the traceback's path survives the round trip
