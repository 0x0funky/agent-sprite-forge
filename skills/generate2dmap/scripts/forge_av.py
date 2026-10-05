"""Canonical ffmpeg/ffprobe helpers for Agent Sprite Forge (forge_av, API v1).

One shared implementation of the video plumbing the skills need:

* discovery: ``ffmpeg_info()`` proves capabilities functionally. A VP9 frame
  with a transparent half must survive an encode and a libvpx-vp9 decode, and
  libx264 must really encode. ``ffmpeg -encoders`` text is never trusted.
* probing: ``probe()`` describes the real video stream. Cover art (attached
  pictures) and audio tracks are counted, never mistaken for the clip.
* decoding: ``extract_frames``, ``iter_rgba`` and ``decode_rgba`` keep source
  timing (passthrough) and decode WebM alpha with libvpx, because the native
  VP8/VP9 decoders silently drop the alpha plane.
* encoding: VP9-alpha WebM, packed-alpha H.264 (RGB left, alpha right, even
  halves, BT.709 limited range, faststart) and loop-aligned closed-GOP H.264.
  Frame rates are rational and capped at 60 fps. Colour under alpha 0 is edge
  bled so 4:2:0 chroma keeps soft edges true; ffmpeg scaling premultiplies.
* container checks: moov placement, packet count, duration and DTS order.

Every subprocess runs with stdin closed and decodes text as UTF-8 with
replacement. Failures raise ForgeAVError (a RuntimeError) carrying an ASCII
tail of ffmpeg's stderr. Encoders write deterministic bytes, never replace an
existing file and never leave a partial one behind. Requires ffmpeg 5.1+.

Canonical source: shared/forge_av.py. Skills vendor byte-identical copies
(shared/VENDORED.json); edit only this file, then run
``python tools/vendor_sync.py --write``.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

import numpy as np
from PIL import Image

FORGE_AV_API_VERSION = "1"
DEFAULT_TIMEOUT = 300.0
MAX_FPS = 60
PACKED_LAYOUT = "rgb-left-alpha-right"
ALPHA_MODES = ("auto", "on", "off")
DECODE_PIXEL_BUDGET = 1 << 28  # about 1 GiB of RGBA frames
# Alpha encoders fill colour under alpha 0 up to this many px from the subject.
# Invisible after compositing, it halves soft-edge error from 4:2:0 chroma
# (antialiased 320 px sprite, translucent-edge RGB MAE 20.0 -> 4.2).
EDGE_BLEED_PX = 3

# The native VP8/VP9 decoders ignore WebM's alpha side channel; libvpx keeps it.
_ALPHA_DECODERS = {"vp8": "libvpx", "vp9": "libvpx-vp9"}
_ALPHA_PIX_FMT = re.compile(r"^(yuva|gbrap|ya\d|rgba|bgra|argb|abgr|ayuv|vuya|pal8)")
_FFMPEG_GLOBAL = ("-hide_banner", "-loglevel", "error", "-nostdin", "-n")
# Bitexact muxing (no random WebM UIDs, no version tags) plus the fixed encoder
# thread counts below make identical frames give identical bytes.
_DETERMINISTIC_MUX = ("-map_metadata", "-1", "-fflags", "+bitexact")
_BT709_TAGS = ("-color_range", "tv", "-colorspace", "bt709",
               "-color_primaries", "bt709", "-color_trc", "bt709")


class ForgeAVError(RuntimeError):
    """ffmpeg/ffprobe is missing, failed, timed out or produced unusable output."""


# ---------------------------------------------------------------- processes

def run(cmd: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run ``cmd`` and return its stdout decoded as UTF-8 (errors replaced).

    stdin is closed, so ffmpeg can never wait for keyboard input. A missing
    executable, a timeout or a non-zero exit raises ForgeAVError with the tail
    of stderr.
    """
    argv = [str(part) for part in cmd]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=_positive(timeout, "timeout"))
    except FileNotFoundError as exc:
        raise ForgeAVError(f"executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ForgeAVError(f"{Path(argv[0]).stem} timed out after {timeout:g} s") from exc
    if proc.returncode:
        raise ForgeAVError(_failure(argv, proc.returncode, proc.stderr or proc.stdout))
    return proc.stdout


class _Child:
    """A watched ffmpeg child process: killed on timeout or when left early."""

    def __init__(self, argv: list[str], *, stdin, stdout, timeout: float) -> None:
        self.argv, self.timeout = argv, _positive(timeout, "timeout")
        self.expired = threading.Event()
        self._errors = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=self._errors)
        except FileNotFoundError as exc:
            self._errors.close()
            raise ForgeAVError(f"executable not found: {argv[0]}") from exc
        self._timer = threading.Timer(self.timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self) -> None:
        self.expired.set()
        self.proc.kill()

    def __enter__(self) -> _Child:
        return self

    def __exit__(self, *exc_info) -> None:
        self._timer.cancel()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for pipe in (self.proc.stdin, self.proc.stdout):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass
        self._errors.close()

    def finish(self) -> None:
        """Wait for exit; raise ForgeAVError on a timeout or a non-zero status."""
        code = self.proc.wait()
        if self.expired.is_set():
            raise ForgeAVError(f"{Path(self.argv[0]).stem} timed out after {self.timeout:g} s")
        if code:
            self._errors.seek(0)
            raise ForgeAVError(_failure(self.argv, code, self._errors.read().decode("utf-8", "replace")))


def _feed(argv: list[str], chunks: Iterable[bytes], timeout: float) -> None:
    """Run ffmpeg with ``chunks`` streamed to stdin (one frame in memory at a time)."""
    with _Child(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, timeout=timeout) as child:
        for chunk in chunks:
            try:
                child.proc.stdin.write(chunk)
            except OSError:  # ffmpeg exited early (EPIPE, or EINVAL on Windows); stderr says why
                break
        try:
            child.proc.stdin.close()
        except OSError:
            pass
        child.finish()


def _stream_frames(argv: list[str], shape: tuple[int, int, int], timeout: float) -> Iterator[np.ndarray]:
    """Yield writable uint8 frames of ``shape`` from ffmpeg's rawvideo stdout."""
    size = shape[0] * shape[1] * shape[2]
    with _Child(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, timeout=timeout) as child:
        while True:
            frame = np.empty(shape, np.uint8)
            got = _read_exact(child.proc.stdout, memoryview(frame).cast("B"))
            if not got:
                break
            if got != size:
                child.finish()
                raise ForgeAVError(f"truncated frame from ffmpeg: {got} of {size} bytes")
            yield frame
        child.finish()


def _read_exact(stream, view: memoryview) -> int:
    filled = 0
    while filled < len(view):
        count = stream.readinto(view[filled:])
        if not count:
            break
        filled += count
    return filled


def _failure(argv: Sequence[str], code: int, output: str) -> str:
    tail = output.strip()[-4000:].encode("ascii", "backslashreplace").decode("ascii")
    return f"{Path(argv[0]).stem} exited with status {code}: {tail or 'no error output'}"


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ForgeAVError(f"{name} not found on PATH; install ffmpeg 5.1 or newer (it ships {name})")
    return path


def _ffprobe_json(args: Sequence[str]) -> dict:
    text = run([_tool("ffprobe"), "-v", "error", "-of", "json", *args])
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ForgeAVError(f"ffprobe returned invalid JSON: {exc}") from exc


# ---------------------------------------------------------------- discovery

def ffmpeg_info() -> dict:
    """Locate ffmpeg/ffprobe and prove what they can do (cached per binary pair).

    Keys: ``ffmpeg`` and ``ffprobe`` (paths or None), ``version``, and
    ``functional`` with ``vp9_alpha`` (a VP9 frame with a transparent half
    survives forge_av's own encoder and a libvpx-vp9 decode) and ``libx264``
    (forge_av's packed H.264 encode decodes again). ``errors`` explains every
    failed check.
    """
    return copy.deepcopy(_probe_capabilities(shutil.which("ffmpeg"), shutil.which("ffprobe")))


@functools.lru_cache(maxsize=4)
def _probe_capabilities(ffmpeg: str | None, ffprobe: str | None) -> dict:
    info = {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "version": None,
            "functional": {"vp9_alpha": False, "libx264": False}, "errors": {}}
    if not ffprobe:
        info["errors"]["ffprobe"] = "ffprobe not found on PATH"
    if not ffmpeg:
        info["errors"]["ffmpeg"] = "ffmpeg not found on PATH"
        return info
    try:
        banner = run([ffmpeg, "-hide_banner", "-version"], timeout=30).split()
        info["version"] = banner[2] if banner[:2] == ["ffmpeg", "version"] and len(banner) > 2 else None
    except ForgeAVError as exc:
        info["errors"]["version"] = str(exc)
    with tempfile.TemporaryDirectory(prefix="forge-av-probe-") as tmp:
        for name, check in (("vp9_alpha", _check_vp9_alpha), ("libx264", _check_libx264)):
            try:
                check(ffmpeg, Path(tmp))
                info["functional"][name] = True
            except ForgeAVError as exc:
                info["errors"][name] = str(exc)
    return info


def _probe_frame(size: int = 32) -> np.ndarray:
    """Left half transparent, right half opaque orange."""
    frame = np.zeros((size, size, 4), np.uint8)
    frame[:, size // 2:] = (224, 96, 32, 255)
    return frame


def _check_vp9_alpha(ffmpeg: str, folder: Path, decoder: str = "libvpx-vp9") -> None:
    """Raise ForgeAVError unless a VP9 alpha frame round-trips through ``decoder``."""
    frame = _probe_frame()
    movie = folder / f"probe-{decoder}.webm"
    encode_vp9_alpha([frame], movie, 1, timeout=60)
    argv = [ffmpeg, *_FFMPEG_GLOBAL, "-c:v", decoder, "-i", str(movie),
            "-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1"]
    decoded = list(_stream_frames(argv, frame.shape, 60))
    if len(decoded) != 1:
        raise ForgeAVError(f"VP9 alpha probe decoded {len(decoded)} frames, expected 1")
    alpha = decoded[0][..., 3].astype(np.float64)
    quarter = frame.shape[1] // 4
    clear, solid = alpha[:, :quarter].mean(), alpha[:, -quarter:].mean()
    if clear > 16 or solid < 239:
        raise ForgeAVError(f"VP9 alpha did not survive a {decoder} decode "
                           f"(transparent mean {clear:.0f}, opaque mean {solid:.0f})")


def _check_libx264(ffmpeg: str, folder: Path) -> None:
    """Raise ForgeAVError unless forge_av's libx264 packed encode decodes again."""
    movie = folder / "probe-libx264.mp4"
    width, height = encode_packed_alpha([_probe_frame()] * 2, movie, 1, keyint=1, timeout=60)["encodedSize"]
    argv = [ffmpeg, *_FFMPEG_GLOBAL, "-i", str(movie), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    decoded = list(_stream_frames(argv, (height, width, 3), 60))
    if len(decoded) != 2:
        raise ForgeAVError(f"libx264 probe decoded {len(decoded)} frames, expected 2")


# ---------------------------------------------------------------- probing

def probe(path) -> dict:
    """Describe the main video stream of ``path``; attached cover art is skipped.

    Keys: ``codec``; ``width``/``height`` (display size after container
    rotation, which is what decode_rgba returns); ``pix_fmt`` as ffprobe
    reports it (VP9 alpha still shows yuv420p); ``fps_rational`` ("num/den" or
    None); ``nb_frames`` (container count, else demuxed packets, see
    ``nb_frames_source``); ``duration`` in seconds of the video stream (not
    the longer of video and audio); ``vp9_alpha``; ``alpha_mode`` (WebM
    AlphaMode, VP8 or VP9); ``has_alpha``; ``rotation``; ``stream_index``;
    ``audio_streams`` (count) with per-track ``audio`` details;
    ``attached_pics`` (cover-art streams); ``format``; ``path``.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    data = _ffprobe_json(["-show_streams", "-show_format", str(source)])
    streams = data.get("streams") or []
    video = _main_video_stream(streams, source)
    tags = {str(key).lower(): value for key, value in (video.get("tags") or {}).items()}
    codec, pix_fmt = video.get("codec_name"), video.get("pix_fmt")
    alpha_mode = str(tags.get("alpha_mode", "")).strip() == "1"
    rotation = _rotation(video, tags)
    width, height = int(video["width"]), int(video["height"])
    if rotation % 180 == 90:
        width, height = height, width
    index = int(video["index"])
    nb_frames, counted_by = _positive_int(video.get("nb_frames")), "container"
    if nb_frames is None:
        nb_frames, counted_by = _count_packets(source, index), "packets"
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    return {
        "path": str(source),
        "format": (data.get("format") or {}).get("format_name"),
        "stream_index": index,
        "codec": codec,
        "width": width,
        "height": height,
        "pix_fmt": pix_fmt,
        "fps_rational": _stream_fps(video),
        "nb_frames": nb_frames,
        "nb_frames_source": counted_by,
        "duration": _stream_duration(video, tags, data.get("format") or {}),
        "rotation": rotation,
        "vp9_alpha": codec == "vp9" and alpha_mode,
        "alpha_mode": alpha_mode,
        "has_alpha": alpha_mode or bool(pix_fmt and _ALPHA_PIX_FMT.match(pix_fmt)),
        "audio_streams": len(audio),
        "audio": [{"index": int(s["index"]), "codec": s.get("codec_name"),
                   "channels": s.get("channels"), "sample_rate": _positive_int(s.get("sample_rate")),
                   "duration": _seconds_or_none(s.get("duration"))} for s in audio],
        "attached_pics": sum(1 for s in streams if s.get("codec_type") == "video" and _is_attached_pic(s)),
    }


def _main_video_stream(streams: Sequence[Mapping], source: Path) -> Mapping:
    for stream in streams:
        if stream.get("codec_type") == "video" and not _is_attached_pic(stream):
            return stream
    raise ForgeAVError(f"no video stream in {source.name}")


def _is_attached_pic(stream: Mapping) -> bool:
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def _rotation(stream: Mapping, tags: Mapping) -> int:
    for item in stream.get("side_data_list") or []:
        if "rotation" in item:
            return int(round(float(item["rotation"]))) % 360
    try:
        return int(round(float(tags.get("rotate", 0)))) % 360
    except (TypeError, ValueError):
        return 0


def _stream_fps(stream: Mapping) -> str | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            rate = Fraction(str(stream.get(key) or ""))
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return _rational(rate)
    return None


def _stream_duration(stream: Mapping, tags: Mapping, fmt: Mapping) -> float | None:
    seconds = _seconds_or_none(stream.get("duration"))
    if seconds is None and tags.get("duration"):
        seconds = _clock_seconds(str(tags["duration"]))  # Matroska per-track "HH:MM:SS.nnnnnnnnn"
    if seconds is None:
        seconds = _seconds_or_none(fmt.get("duration"))
    return seconds


def _clock_seconds(text: str) -> float | None:
    parts = text.strip().split(":")
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None
    if len(values) != 3 or not all(math.isfinite(v) for v in values):
        return None
    return values[0] * 3600 + values[1] * 60 + values[2]


def _count_packets(source: Path, index: int) -> int:
    data = _ffprobe_json(["-select_streams", str(index), "-count_packets",
                          "-show_entries", "stream=nb_read_packets", str(source)])
    streams = data.get("streams") or [{}]
    return _positive_int(streams[0].get("nb_read_packets")) or 0


# ---------------------------------------------------------------- decoding

def extract_frames(path, out_dir, *, fps=0, start=0, duration=None, alpha="auto",
                   timeout: float = DEFAULT_TIMEOUT) -> list[Path]:
    """Decode the main video stream to ``out_dir/frame_000000.png`` and onwards.

    Files are numbered by 0-based frame index (six digits). ``fps=0`` keeps
    every source frame with its own timing (passthrough); ``fps>0`` resamples
    to that constant rate (number or "num/den"). ``start``/``duration`` keep
    the frames whose timestamps fall in [start, start + duration) seconds
    (accurate seek). ``alpha``: ``auto`` keeps the source alpha when there is
    one (WebM alpha via libvpx), ``on`` requires it, ``off`` drops it. Frames
    are RGBA PNGs when alpha is kept, else RGB. ``out_dir`` may exist but must
    hold no ``frame_*.png``; on failure the frames written so far are removed.
    """
    source = Path(path)
    info = probe(source)
    decoder, keep_alpha = _alpha_plan(info, alpha)
    rate = _fraction(fps, "fps", allow_zero=True)
    trim = _trim_args(start, duration)
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    if any(target.glob("frame_*.png")):
        raise FileExistsError(f"{target} already holds frame_*.png files; extract into a fresh directory")
    timing = ["-vf", f"fps={_rational(rate)}"] if rate else ["-fps_mode", "passthrough"]
    pattern = str(target).replace("%", "%%") + os.sep + "frame_%06d.png"  # image2 expands '%'
    argv = [_tool("ffmpeg"), *_FFMPEG_GLOBAL, *decoder, *trim, "-i", str(source),
            "-map", f"0:{info['stream_index']}", "-an", "-sn", "-dn", *timing,
            "-pix_fmt", "rgba" if keep_alpha else "rgb24", "-start_number", "0", pattern]
    try:
        run(argv, timeout=timeout)
        frames = sorted(target.glob("frame_*.png"), key=_natural_key)
        if not frames:
            raise ForgeAVError(f"no frames decoded from {source.name}; check start/duration")
    except BaseException:
        for leftover in target.glob("frame_*.png"):
            leftover.unlink(missing_ok=True)
        raise
    return frames


def iter_rgba(path, *, size=None, alpha="auto", start=0, duration=None,
              timeout: float = DEFAULT_TIMEOUT) -> Iterator[np.ndarray]:
    """Stream the main video stream as writable (H, W, 4) uint8 RGBA arrays.

    One frame is in memory at a time, so long clips can be verified without a
    pixel budget. Arguments match decode_rgba. ``size=(w, h)`` resizes inside
    ffmpeg (premultiplied when alpha is kept). Pixels are returned exactly as
    decoded: colour under alpha 0 is not zeroed. Leaving the loop early kills
    ffmpeg.
    """
    source = Path(path)
    info = probe(source)
    decoder, keep_alpha = _alpha_plan(info, alpha)
    width, height = (info["width"], info["height"]) if size is None else _size(size)
    filters = []
    if (width, height) != (info["width"], info["height"]):
        filters.append(premultiplied_scale_filter(width, height) if keep_alpha
                       else f"scale={width}:{height}:flags=lanczos")
    if not keep_alpha:
        filters.append("format=rgb24")  # discard any source alpha: the output is opaque
    argv = [_tool("ffmpeg"), *_FFMPEG_GLOBAL, *decoder, *_trim_args(start, duration), "-i", str(source),
            "-map", f"0:{info['stream_index']}", "-an", "-sn", "-dn",
            *(["-vf", ",".join(filters)] if filters else []), "-fps_mode", "passthrough",
            "-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1"]
    return _stream_frames(argv, (height, width, 4), timeout)


def decode_rgba(path, *, size=None, alpha="auto", start=0, duration=None,
                max_pixels: int = DECODE_PIXEL_BUDGET, timeout: float = DEFAULT_TIMEOUT) -> list[np.ndarray]:
    """Decode the main video stream to a list of (H, W, 4) uint8 RGBA arrays.

    Timing is passthrough (one array per coded frame). ``alpha``: ``auto``
    decodes alpha when the stream has it (VP8/VP9 WebM through libvpx),
    ``on`` raises ValueError for an opaque source, ``off`` returns opaque
    frames. ``start``/``duration`` keep frames timed in [start, start +
    duration) seconds. ``size=(w, h)`` resizes in ffmpeg, premultiplied. More
    than ``max_pixels`` decoded pixels raise ValueError; use iter_rgba() to
    stream instead.
    """
    budget = int(_positive(max_pixels, "max_pixels"))
    stream = iter_rgba(path, size=size, alpha=alpha, start=start, duration=duration, timeout=timeout)
    frames, pixels = [], 0
    try:
        for frame in stream:
            pixels += frame.shape[0] * frame.shape[1]
            if pixels > budget:
                raise ValueError(f"decoding {Path(path).name} exceeds max_pixels={budget}; trim with "
                                 "start/duration, pass size=, or stream with iter_rgba()")
            frames.append(frame)
    finally:
        stream.close()
    if not frames:
        raise ForgeAVError(f"no frames decoded from {Path(path).name}; check start/duration")
    return frames


def _alpha_plan(info: Mapping, alpha: str) -> tuple[list[str], bool]:
    """Return (decoder arguments, keep alpha) for an ``alpha`` mode."""
    if alpha not in ALPHA_MODES:
        raise ValueError(f"alpha must be one of: {', '.join(ALPHA_MODES)}")
    if alpha == "on" and not info["has_alpha"]:
        raise ValueError(f"{Path(info['path']).name} has no alpha channel "
                         f"({info['codec']}, {info['pix_fmt']}); alpha='on' needs a transparent source")
    if alpha == "off" or not info["has_alpha"]:
        return [], False
    decoder = _ALPHA_DECODERS.get(info["codec"]) if info["alpha_mode"] else None
    return (["-c:v", decoder] if decoder else []), True


def _trim_args(start, duration) -> list[str]:
    """Input-side -ss/-t: decode-accurate and selecting frames timed in [start, start + duration)."""
    begin = _seconds(start, "start", allow_zero=True)
    args = ["-ss", repr(begin)] if begin else []
    if duration is not None:
        args += ["-t", repr(_seconds(duration, "duration", allow_zero=False))]
    return args


# ---------------------------------------------------------------- encoding

def premultiplied_scale_filter(w: int, h: int) -> str:
    """ffmpeg filter chain that resizes straight RGBA without dark halos.

    Colour is premultiplied by alpha in 16-bit planar RGB, resized with
    lanczos, unpremultiplied and returned as straight 8-bit ``rgba``. A plain
    ``scale`` filters colour and alpha independently, so the zeroed colour of
    transparent pixels bleeds into soft edges (a red-255 disk measures about
    120 at its edge instead of 255).
    """
    width, height = _size((w, h))
    return (f"format=gbrap16le,premultiply=inplace=1:planes=7,scale={width}:{height}:flags=lanczos,"
            "unpremultiply=inplace=1:planes=7,format=rgba")


def packed_geometry(width: int, height: int) -> dict:
    """Packed-alpha geometry: each half is padded right/bottom to even pixels.

    ``width``/``height`` are the logical frame; ``halfWidth``/``halfHeight``
    the physical size of each encoded half (403 -> 404). A runtime crops RGB
    at (0, 0, width, height) and alpha at (halfWidth, 0, width, height).
    """
    w, h = _size((width, height))
    half_w, half_h = w + (w & 1), h + (h & 1)
    return {"layout": PACKED_LAYOUT, "width": w, "height": h,
            "halfWidth": half_w, "halfHeight": half_h, "encodedSize": [2 * half_w, half_h]}


def unpack_packed_alpha(frame: np.ndarray, geometry: Mapping) -> np.ndarray:
    """Rebuild straight RGBA from one decoded packed frame (alpha read from red)."""
    if geometry.get("layout", PACKED_LAYOUT) != PACKED_LAYOUT:
        raise ValueError(f"unsupported packed layout: {geometry.get('layout')}")
    w, h, half_w = int(geometry["width"]), int(geometry["height"]), int(geometry["halfWidth"])
    data = np.asarray(frame)
    if data.ndim != 3 or data.shape[2] < 3 or half_w < w or data.shape[0] < h or data.shape[1] < half_w + w:
        raise ValueError("frame is smaller than the packed geometry")
    out = np.empty((h, w, 4), np.uint8)
    out[..., :3] = data[:h, :w, :3]
    out[..., 3] = data[:h, half_w:half_w + w, 0]
    return out


def encode_vp9_alpha(frames_or_dir, out, fps_rational, crf: int = 28, *, keyint=None,
                     max_fps=MAX_FPS, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Encode straight-alpha RGBA frames to a VP9 WebM with a native alpha plane.

    ``frames_or_dir``: a directory of PNG frames (natural sort) or a sequence
    of (H, W, 3|4) uint8 arrays, PIL images or image paths, all one size;
    resize beforehand (forge_core.resample_rgba). Transparent colour is edge
    bled (see EDGE_BLEED_PX). Output is yuva420p tagged BT.709 limited range;
    decode it with libvpx-vp9 (decode_rgba does). ``keyint`` must divide the
    frame count so keyframes land on the loop wrap. Rates above ``max_fps``
    (at most 60) keep the nearest frame per tick; the result records
    ``fpsCapped`` and the ``inputIndices`` used. Returns file, bytes, sha256,
    size, timing and the keyframe indices.
    """
    clip = _Clip(frames_or_dir, fps_rational, max_fps)
    quality, gop = _crf(crf, 63), _keyint(keyint, clip.count)
    argv = [_tool("ffmpeg"), *_FFMPEG_GLOBAL, *_raw_input(clip, "rgba"), "-filter_complex_threads", "1",
            "-filter_complex", f"[0:v]{_to_bt709('yuva420p')}[v]", "-map", "[v]", "-an",
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-b:v", "0",
            "-crf", str(quality), "-row-mt", "1", "-threads", "2", *_BT709_TAGS,
            *(["-g", str(gop), "-keyint_min", str(gop)] if gop else []),
            "-fps_mode", "passthrough", *_DETERMINISTIC_MUX]
    target, keyframes = _encode_file(out, "webm", argv, (_edge_bleed(f).tobytes() for _, f in clip.frames()),
                                     clip.count, gop, timeout)
    return {**_digest(target), "mimeType": "video/webm", "codec": "vp9", "alpha": "native-vp9",
            "pixFmt": "yuva420p", "width": clip.size[0], "height": clip.size[1], "crf": quality,
            "keyint": gop, "keyframes": keyframes, **clip.timing()}


def encode_packed_alpha(frames, out, fps_rational, crf: int = 18, *, keyint=None,
                        max_fps=MAX_FPS, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Encode straight RGBA frames as packed-alpha H.264 (RGB left, alpha right).

    For browsers and iOS without VP9 alpha; a compositor recombines the halves.
    Each half is padded right/bottom with black to even pixels (403 -> 404,
    see packed_geometry); RGB stays straight, never composited over black,
    with transparent colour edge bled. H.264 Main, yuv420p, limited-range
    BT.709 tags, faststart, closed GOPs. ``frames``, ``keyint`` and
    ``max_fps`` behave as in encode_vp9_alpha. Returns the packed geometry
    (``layout``, ``width``, ``height``, ``halfWidth``, ``halfHeight``,
    ``encodedSize``), file, bytes, sha256, timing and keyframe indices.
    """
    clip = _Clip(frames, fps_rational, max_fps)
    quality, gop = _crf(crf, 51), _keyint(keyint, clip.count)
    geometry = packed_geometry(*clip.size)
    half = f"{geometry['halfWidth']}:{geometry['halfHeight']}:0:0:color=black"
    graph = (f"[0:v]split=2[c][a];[c]format=rgb24,pad={half}[c2];"
             f"[a]alphaextract,format=rgb24,pad={half}[a2];"
             f"[c2][a2]hstack=inputs=2,{_to_bt709('yuv420p')}[v]")
    argv = [_tool("ffmpeg"), *_FFMPEG_GLOBAL, *_raw_input(clip, "rgba"), "-filter_complex_threads", "1",
            "-filter_complex", graph, "-map", "[v]", "-an", *_x264_args(quality, gop, True),
            "-movflags", "+faststart", "-fps_mode", "passthrough", *_DETERMINISTIC_MUX]
    target, keyframes = _encode_file(out, "mp4", argv, (_edge_bleed(f).tobytes() for _, f in clip.frames()),
                                     clip.count, gop, timeout)
    return {**_digest(target), "mimeType": "video/mp4", "codec": "h264", "profile": "main", **geometry,
            "requiresCompositor": True, "crf": quality, "keyint": gop, "keyframes": keyframes, **clip.timing()}


def encode_h264_loop(frames, out, fps_rational, *, keyint=None, closed_gop: bool = True, crf: int = 18,
                     max_fps=MAX_FPS, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Encode an opaque loop as H.264 whose GOPs line up with the loop wrap.

    ``keyint`` defaults to the loop length (one closed GOP per loop) and must
    divide the frame count, so the wrap back to frame 0 is always a GOP
    boundary; scene-cut keyframes are disabled and placement is verified on
    the written file. Alignment is necessary, not sufficient: codec drift
    still pops at every keyframe (hd2d single GOP: decoded seam 3.90 vs
    adjacent p95 1.76; a synthetic masked loop stays near 2x p95 at any
    keyint and drops to 1.7x at crf 12). Gate on the decoded file and lower
    ``crf`` when the seam fails. Frames must be opaque with even dimensions.
    H.264 Main, yuv420p, limited-range BT.709 tags, faststart.
    """
    clip = _Clip(frames, fps_rational, max_fps)
    quality, gop = _crf(crf, 51), _keyint(keyint, clip.count, default=clip.count)
    width, height = clip.size
    if width % 2 or height % 2:
        raise ValueError(f"H.264 4:2:0 needs even dimensions, got {width}x{height}; crop or pad the frames")
    argv = [_tool("ffmpeg"), *_FFMPEG_GLOBAL, *_raw_input(clip, "rgb24"), "-filter_complex_threads", "1",
            "-filter_complex", f"[0:v]{_to_bt709('yuv420p')}[v]", "-map", "[v]", "-an",
            *_x264_args(quality, gop, bool(closed_gop)), "-movflags", "+faststart",
            "-fps_mode", "passthrough", *_DETERMINISTIC_MUX]
    target, keyframes = _encode_file(out, "mp4", argv, (_opaque_rgb(i, f).tobytes() for i, f in clip.frames()),
                                     clip.count, gop, timeout)
    return {**_digest(target), "mimeType": "video/mp4", "codec": "h264", "profile": "main",
            "width": width, "height": height, "crf": quality, "keyint": gop,
            "closedGop": bool(closed_gop), "keyframes": keyframes, **clip.timing()}


class _Clip:
    """Encoder input: lazily loaded frames of one size, with the fps cap applied."""

    def __init__(self, frames, fps_rational, max_fps) -> None:
        self.sources = _frame_sources(frames)
        first = _load_frame(self.sources[0])
        self.size = (first.shape[1], first.shape[0])
        self.input_fps = _fraction(fps_rational, "fps_rational")
        cap = _fraction(max_fps, "max_fps")
        if cap > MAX_FPS:
            raise ValueError(f"max_fps cannot exceed {MAX_FPS}")
        self.indices, self.fps = _fps_cap(len(self.sources), self.input_fps, cap)
        self.count = len(self.indices)

    def frames(self) -> Iterator[tuple[int, np.ndarray]]:
        for index in self.indices:
            frame = _load_frame(self.sources[index])
            if (frame.shape[1], frame.shape[0]) != self.size:
                raise ValueError(f"frame {index} is {frame.shape[1]}x{frame.shape[0]}; "
                                 f"every frame must be {self.size[0]}x{self.size[1]}")
            yield index, frame

    def timing(self) -> dict:
        return {"frameCount": self.count, "fps": _rational(self.fps),
                "durationMs": float(Fraction(1000 * self.count) / self.fps),
                "fpsCapped": self.fps != self.input_fps, "inputFps": _rational(self.input_fps),
                "inputFrameCount": len(self.sources), "inputIndices": list(self.indices)}


def _fps_cap(count: int, fps: Fraction, cap: Fraction) -> tuple[list[int], Fraction]:
    """Keep every frame at or below ``cap``; otherwise pick the nearest frame per 1/cap tick."""
    if fps <= cap:
        return list(range(count)), fps
    total = max(1, math.floor(count * cap / fps + Fraction(1, 2)))
    return [min(count - 1, math.floor(k * fps / cap + Fraction(1, 2))) for k in range(total)], cap


def _encode_file(out, muxer: str, argv: list[str], chunks: Iterable[bytes], frames: int,
                 keyint: int | None, timeout: float) -> tuple[Path, list[int]]:
    """Encode into a hidden sibling, verify packets and GOPs, then publish without replacing."""
    target = Path(out)
    if target.exists():
        raise FileExistsError(f"{target} already exists; forge_av never replaces files")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{uuid.uuid4().hex[:12]}.partial")
    try:
        _feed([*argv, "-f", muxer, str(partial)], chunks, timeout)
        packets = _packets(partial, 0)
        if len(packets) != frames:
            raise ForgeAVError(f"encoder wrote {len(packets)} packets for {frames} frames")
        keyframes = _keyframes(packets)
        if keyint and keyframes != list(range(0, frames, keyint)):
            raise ForgeAVError(f"keyframes at {keyframes} do not follow keyint {keyint}")
        _local_publish_file_no_replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target, keyframes


def _local_publish_file_no_replace(source: Path, target: Path) -> None:
    """Publish ``source`` as ``target``; an existing target is never replaced.

    A hard link is atomic and refuses an existing target. Filesystems without
    hard links fall back to an exclusive create plus copy. The caller removes
    ``source``.
    """
    try:
        os.link(source, target)
        return
    except FileExistsError:
        raise FileExistsError(f"{target} appeared during encoding; refusing to replace it") from None
    except OSError:
        pass
    with open(source, "rb") as src, open(target, "xb") as dst:
        try:
            shutil.copyfileobj(src, dst, 1 << 20)
            dst.flush()
            os.fsync(dst.fileno())
        except BaseException:
            dst.close()
            target.unlink(missing_ok=True)
            raise


def _raw_input(clip: _Clip, pix_fmt: str) -> list[str]:
    return ["-f", "rawvideo", "-pix_fmt", pix_fmt, "-video_size", f"{clip.size[0]}x{clip.size[1]}",
            "-framerate", _rational(clip.fps), "-i", "pipe:0"]


def _to_bt709(pix_fmt: str) -> str:
    """Full-range RGB to limited-range BT.709 ``pix_fmt``, tagged on the frames.

    ffmpeg 8 ignores -color_primaries/-color_trc unless the frames carry them,
    hence setparams; the matching output options are kept for older builds.
    """
    return (f"scale=in_range=full:out_range=tv:out_color_matrix=bt709,format={pix_fmt},"
            "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv")


def _x264_args(crf: int, keyint: int | None, closed_gop: bool) -> list[str]:
    args = ["-c:v", "libx264", "-profile:v", "main", "-preset", "fast", "-crf", str(crf),
            "-threads", "2", "-pix_fmt", "yuv420p", *_BT709_TAGS]
    if keyint:
        args += ["-g", str(keyint), "-keyint_min", str(keyint), "-sc_threshold", "0"]
    return args + (["-flags", "+cgop"] if closed_gop else ["-x264-params", "open-gop=1"])


def _edge_bleed(frame: np.ndarray) -> np.ndarray:
    """Straight RGBA with the colour of transparent pixels bled in from visible ones.

    Colour under alpha 0 is zeroed, then pixels within EDGE_BLEED_PX of the
    subject take the alpha-weighted mean colour of their visible 3x3
    neighbours, ring by ring. They stay fully transparent, so nothing visible
    changes, but 4:2:0 chroma and codec blocks along soft edges stop averaging
    in false black. Alpha is never touched.
    """
    out = np.where(frame[..., 3:] == 0, 0, frame).astype(np.uint8, copy=False)
    seen = out[..., 3] > 0
    if seen.all() or not seen.any():
        return out
    rows, cols = np.flatnonzero(seen.any(axis=1)), np.flatnonzero(seen.any(axis=0))
    y0, y1 = max(0, rows[0] - EDGE_BLEED_PX), min(out.shape[0], rows[-1] + EDGE_BLEED_PX + 1)
    x0, x1 = max(0, cols[0] - EDGE_BLEED_PX), min(out.shape[1], cols[-1] + EDGE_BLEED_PX + 1)
    view, seen = out[y0:y1, x0:x1], seen[y0:y1, x0:x1].copy()
    weight = view[..., 3].astype(np.float32) / 255
    total = view[..., :3].astype(np.float32) * weight[..., None]
    for _ in range(EDGE_BLEED_PX):
        sums, counts = _box3(total), _box3(weight)
        fill = ~seen & (counts > 0)
        if not fill.any():
            break
        colour = sums[fill] / counts[fill][:, None]
        total[fill], weight[fill], seen[fill] = colour, 1.0, True
        view[..., :3][fill] = np.rint(colour).astype(np.uint8)
    return out


def _box3(values: np.ndarray) -> np.ndarray:
    """3x3 neighbourhood sums (zero padded) over the first two axes."""
    padded = np.pad(values, ((1, 1), (1, 1)) + ((0, 0),) * (values.ndim - 2))
    rows = padded[:-2] + padded[1:-1] + padded[2:]
    return rows[:, :-2] + rows[:, 1:-1] + rows[:, 2:]


def _opaque_rgb(index: int, frame: np.ndarray) -> np.ndarray:
    if (frame[..., 3] != 255).any():
        raise ValueError(f"frame {index} has transparency; encode_h264_loop writes opaque video "
                         "(use encode_packed_alpha or encode_vp9_alpha for alpha)")
    return frame[..., :3]


def _frame_sources(frames) -> list:
    if isinstance(frames, (str, os.PathLike)):
        folder = Path(frames)
        if not folder.is_dir():
            raise ValueError(f"frame directory not found: {folder}")
        sources = sorted(folder.glob("*.png"), key=_natural_key)
        if not sources:
            raise ValueError(f"no PNG frames in {folder}")
        return sources
    if isinstance(frames, np.ndarray) and frames.ndim != 4:
        raise ValueError("a frame array must be shaped (frames, height, width, channels)")
    sources = list(frames)
    if not sources:
        raise ValueError("no frames to encode")
    return sources


def _load_frame(item) -> np.ndarray:
    """One frame as a C-contiguous (H, W, 4) uint8 array; RGB input is opaque."""
    if isinstance(item, (str, os.PathLike)):
        with Image.open(item) as image:
            if getattr(image, "n_frames", 1) > 1:
                raise ValueError(f"animated image is not one frame: {item}")
            array = np.asarray(image.convert("RGBA"))
    elif isinstance(item, Image.Image):
        array = np.asarray(item.convert("RGBA"))
    else:
        array = np.asarray(item)
        if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] not in (3, 4):
            raise ValueError("frames must be uint8 arrays shaped (height, width, 3 or 4)")
        if array.shape[2] == 3:
            array = np.dstack([array, np.full(array.shape[:2], 255, np.uint8)])
    return np.ascontiguousarray(array)


# ---------------------------------------------------------------- container checks

def moov_before_mdat(path) -> bool:
    """True when an MP4's top-level ``moov`` box precedes ``mdat`` (faststart).

    Reads box headers only. Raises ValueError for a file that is not an
    ISO-BMFF/MP4 file or whose boxes are truncated.
    """
    first: dict[str, int] = {}
    for kind, offset in _top_level_boxes(Path(path)):
        first.setdefault(kind, offset)
    return "moov" in first and "mdat" in first and first["moov"] < first["mdat"]


def packet_count(path) -> int:
    """Number of packets in the main video stream (one per frame for forge_av outputs)."""
    return len(_video_packets(Path(path)))


def duration_ms(path) -> float:
    """Presentation length of the main video stream in milliseconds.

    Measured from packets as max(pts + duration) - min(pts), so an audio track
    or cover art never inflates it; falls back to probe()'s duration.
    """
    starts, ends = [], []
    for packet in _video_packets(Path(path)):
        pts = _seconds_or_none(packet.get("pts_time"))
        if pts is not None:
            starts.append(pts)
            ends.append(pts + (_seconds_or_none(packet.get("duration_time")) or 0.0))
    if starts and max(ends) > min(starts):
        return round((max(ends) - min(starts)) * 1000, 6)
    seconds = probe(path)["duration"]
    if seconds is None:
        raise ForgeAVError(f"duration of {Path(path).name} is unknown")
    return seconds * 1000


def timestamps_increasing(path) -> bool:
    """True when the main video stream's decode timestamps strictly increase.

    H.264 B-frames reorder presentation timestamps in packet order, so DTS
    (falling back to PTS when a packet has none) is the clock that must rise.
    """
    stamps = []
    for packet in _video_packets(Path(path)):
        stamp = _seconds_or_none(packet.get("dts_time"))
        if stamp is None:
            stamp = _seconds_or_none(packet.get("pts_time"))
        if stamp is None:
            return False
        stamps.append(stamp)
    return bool(stamps) and all(a < b for a, b in zip(stamps, stamps[1:]))


def _video_packets(source: Path) -> list[dict]:
    """Packets of the main video stream in decode order (one ffprobe pass)."""
    if not source.is_file():
        raise FileNotFoundError(source)
    data = _ffprobe_json(["-show_entries", "stream=index,codec_type:stream_disposition=attached_pic:"
                          "packet=stream_index,pts_time,dts_time,duration_time,flags", str(source)])
    index = int(_main_video_stream(data.get("streams") or [], source)["index"])
    return [packet for packet in data.get("packets") or [] if packet.get("stream_index") == index]


def _packets(source: Path, index: int) -> list[dict]:
    """Packets of stream ``index`` in decode order."""
    data = _ffprobe_json(["-select_streams", str(index), "-show_entries",
                          "packet=pts_time,dts_time,duration_time,flags", str(source)])
    return data.get("packets") or []


def _keyframes(packets: Sequence[Mapping]) -> list[int]:
    """Presentation-order indices of keyframe packets."""
    order = sorted(packets, key=lambda p: _seconds_or_none(p.get("pts_time")) or 0.0)
    return [i for i, packet in enumerate(order) if "K" in str(packet.get("flags", ""))]


def _top_level_boxes(source: Path) -> list[tuple[str, int]]:
    boxes, total = [], source.stat().st_size
    with source.open("rb") as handle:
        offset = 0
        while offset < total:
            handle.seek(offset)
            header = handle.read(8)
            if len(header) < 8:
                raise ValueError(f"truncated MP4 box header at byte {offset}")
            size, kind = int.from_bytes(header[:4], "big"), header[4:]
            if not all(32 <= byte < 127 for byte in kind):
                raise ValueError(f"{source.name} is not an ISO-BMFF/MP4 file")
            if size == 1:
                large = handle.read(8)
                if len(large) < 8:
                    raise ValueError(f"truncated MP4 box header at byte {offset}")
                size = int.from_bytes(large, "big")
            elif size == 0:
                size = total - offset
            if size < 8 or offset + size > total:
                raise ValueError(f"invalid or truncated MP4 box {kind.decode('ascii')!r} at byte {offset}")
            boxes.append((kind.decode("ascii"), offset))
            offset += size
    return boxes


# ---------------------------------------------------------------- values

def _digest(path: Path) -> dict:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": sha.hexdigest()}


def _fraction(value, name: str, *, allow_zero: bool = False) -> Fraction:
    """Parse a positive rate given as int, float, Fraction or 'num/den' text."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Fraction, np.integer)):
        raise TypeError(f"{name} must be a number or a 'num/den' string")
    if isinstance(value, np.integer):
        value = int(value)
    try:
        rate = Fraction(str(value).strip()) if isinstance(value, (str, float)) else Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"{name} must be a rational like 24 or '30000/1001', got {value!r}") from None
    if rate < 0 or (rate == 0 and not allow_zero):
        raise ValueError(f"{name} must be positive, got {value!r}")
    return rate


def _rational(rate: Fraction) -> str:
    return f"{rate.numerator}/{rate.denominator}"


def _keyint(value, frames: int, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise TypeError("keyint must be a whole number of frames")
    keyint = int(value)
    if not 1 <= keyint <= frames:
        raise ValueError(f"keyint must be between 1 and the clip length ({frames} frames)")
    if frames % keyint:
        raise ValueError(f"keyint {keyint} does not divide the {frames}-frame loop; "
                         "keyframes must land on the loop wrap")
    return keyint


def _crf(value, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not 0 <= int(value) <= maximum:
        raise ValueError(f"crf must be a whole number from 0 to {maximum}")
    return int(value)


def _size(size) -> tuple[int, int]:
    values = tuple(size)
    if len(values) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, np.integer)) or v < 1
                               for v in values):
        raise ValueError(f"size must be (width, height) in positive whole pixels, got {size!r}")
    return int(values[0]), int(values[1])


def _positive(value, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return number


def _seconds(value, name: str, *, allow_zero: bool) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'nonnegative' if allow_zero else 'positive'} and finite seconds")
    return number


def _seconds_or_none(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _natural_key(path: Path) -> list:
    return [int(part) if i % 2 else part for i, part in enumerate(re.split(r"(\d+)", path.name))]
