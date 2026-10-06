#!/usr/bin/env python3
"""Remove the audio from a downloaded MP4 clip, locally, without re-encoding the video.

Most video APIs add a soundtrack by default (xAI, Seedance 2.x, Vidu, LTX, Wan, MiniMax, Veo).
ASF never uses clip audio, so every clip is published without it, even when the request
already asked for silence. strip_audio() edits the container only: it drops every sound
track (a 'trak' whose handler is 'soun') from 'moov' and, when 'moov' sits before the
media data, shifts the chunk offsets of the tracks it keeps. The video samples stay
byte-identical. A fragmented MP4 (top-level 'moof') is not edited here: remove_audio()
then uses ffmpeg (stream copy, no re-encode) when it is on PATH, and otherwise keeps the
clip as it came and says why.

  python media_mp4.py <clip.mp4> --out <silent.mp4>

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta")
FFMPEG_TIMEOUT = 120
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Mp4Error(ValueError):
    """The bytes are not an MP4 this module can read."""


class Mp4Unsupported(Mp4Error):
    """A valid MP4 layout this module does not edit (fragmented): ffmpeg may still remove the audio."""


def _boxes(data, start, end):
    """(type, box start, header size, box end) for each box in data[start:end]."""
    position = start
    while position < end:
        if end - position < 8:
            raise Mp4Error("truncated box header")
        size, kind = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            if end - position < 16:
                raise Mp4Error("truncated 64-bit box header")
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            raise Mp4Error(f"box {kind!r} overruns its parent")
        yield kind, position, header, position + size
        position += size


def _child(data, box, kind):
    _, start, header, end = box
    for found in _boxes(data, start + header, end):
        if found[0] == kind:
            return found
    return None


def _path(data, box, *kinds):
    for kind in kinds:
        if box is None:
            return None
        box = _child(data, box, kind)
    return box


def _handler(data, trak):
    hdlr = _path(data, trak, b"mdia", b"hdlr")
    if hdlr is None or hdlr[3] - hdlr[1] - hdlr[2] < 12:
        raise Mp4Error("a track has no handler")
    return bytes(data[hdlr[1] + hdlr[2] + 8: hdlr[1] + hdlr[2] + 12])


def _offset_tables(data, trak):
    """[(entry size, first entry position, count)] of the track's stco / co64 tables."""
    stbl = _path(data, trak, b"mdia", b"minf", b"stbl")
    tables = []
    if stbl is None:
        return tables
    for kind, start, header, end in _boxes(data, stbl[1] + stbl[2], stbl[3]):
        if kind in (b"stco", b"co64"):
            width = 4 if kind == b"stco" else 8
            count = struct.unpack_from(">I", data, start + header + 4)[0]
            first = start + header + 8
            if first + count * width > end:
                raise Mp4Error(f"{kind.decode()} table overruns its box")
            tables.append((width, first, count))
    return tables


def scan(data):
    """{'tracks': [handler types], 'fragmented': bool} without changing anything."""
    top = list(_boxes(data, 0, len(data)))
    moov = [box for box in top if box[0] == b"moov"]
    if len(moov) != 1:
        raise Mp4Error("expected exactly one moov box")
    tracks = [_handler(data, box).decode("latin-1") for box in _boxes(data, moov[0][1] + moov[0][2], moov[0][3])
              if box[0] == b"trak"]
    return {"tracks": tracks, "fragmented": any(box[0] == b"moof" for box in top)}


def strip_audio(data):
    """(bytes without sound tracks, {"audioTracks": found, "removed": removed}). Unchanged bytes come
    back when there is no sound track. Raises Mp4Error for a fragmented or unreadable file."""
    data = bytes(data)
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise Mp4Error("not an MP4 (no leading ftyp box)")
    top = list(_boxes(data, 0, len(data)))
    if any(box[0] == b"moof" for box in top):
        raise Mp4Unsupported("fragmented MP4 (moof boxes)")
    moovs = [box for box in top if box[0] == b"moov"]
    if len(moovs) != 1:
        raise Mp4Error("expected exactly one moov box")
    moov = moovs[0]
    children = list(_boxes(data, moov[1] + moov[2], moov[3]))
    traks = [box for box in children if box[0] == b"trak"]
    sound = [box for box in traks if _handler(data, box) == b"soun"]
    if not sound:
        return data, {"audioTracks": 0, "removed": 0}
    if not any(_handler(data, box) == b"vide" for box in traks):
        raise Mp4Error("the clip has no video track")
    kept = [box for box in children if box not in sound]
    payload_size = sum(box[3] - box[1] for box in kept)
    new_size = 8 + payload_size
    header = struct.pack(">I4s", new_size, b"moov") if new_size < 2 ** 32 else \
        struct.pack(">I4sQ", 1, b"moov", new_size + 8)
    delta = (moov[3] - moov[1]) - len(header) - payload_size
    pieces = [header]
    for box in kept:
        piece = bytearray(data[box[1]:box[3]])
        if box[0] == b"trak" and delta:
            for width, first, count in _offset_tables(data, box):
                code = ">I" if width == 4 else ">Q"
                for index in range(count):
                    absolute = first + index * width
                    offset = struct.unpack_from(code, data, absolute)[0]
                    if moov[1] <= offset < moov[3]:
                        raise Mp4Error("a chunk offset points into moov")
                    if offset >= moov[3]:
                        struct.pack_into(code, piece, absolute - box[1], offset - delta)
        pieces.append(bytes(piece))
    result = data[:moov[1]] + b"".join(pieces) + data[moov[3]:]
    check = scan(result)
    if "soun" in check["tracks"] or "vide" not in check["tracks"]:
        raise Mp4Error("the edited clip failed its own check")
    return result, {"audioTracks": len(sound), "removed": len(sound)}


def _ffmpeg_strip(data, ffmpeg):
    """Stream-copy the video only (-map 0:v -c copy -an); returns the new bytes or raises Mp4Error."""
    with tempfile.TemporaryDirectory(prefix="forge-audio-") as folder:
        source, target = Path(folder) / "in.mp4", Path(folder) / "out.mp4"
        source.write_bytes(data)
        command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:v",
                   "-c", "copy", "-an", "-f", "mp4", str(target)]
        try:
            done = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT,
                                  check=False, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Mp4Error(f"ffmpeg did not run ({type(exc).__name__})") from None
        if done.returncode != 0 or not target.is_file():
            raise Mp4Error(f"ffmpeg exited {done.returncode}")
        return target.read_bytes()


def remove_audio(data, *, ffmpeg=None):
    """Clip bytes without audio, plus a record for job.json:
    {"audioTracks", "removed", "method" ("mp4-boxes", "ffmpeg" or null), "rawSha256", "rawBytes",
    and "reason" when the audio could not be removed}. Never raises for a clip it cannot edit:
    the clip is then returned unchanged and the record says why."""
    record = {"rawSha256": hashlib.sha256(data).hexdigest(), "rawBytes": len(data)}
    try:
        silent, info = strip_audio(data)
        record.update(info, method="mp4-boxes" if info["removed"] else None)
        return silent, record
    except Mp4Unsupported as exc:
        reason = str(exc)
    except Mp4Error as exc:  # not a readable MP4: ffmpeg would fail too; the caller validates the clip
        record.update(audioTracks=None, removed=0, method=None, reason=str(exc))
        return data, record
    tool = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
    if tool:
        try:
            silent = _ffmpeg_strip(data, tool)
            record.update(audioTracks=None, removed=None, method="ffmpeg")
            return silent, record
        except Mp4Error as exc:
            reason += f"; {exc}"
    else:
        reason += "; ffmpeg is not on PATH"
    record.update(audioTracks=None, removed=0, method=None, reason=reason)
    return data, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clip", help="MP4 clip to read")
    parser.add_argument("--out", required=True, help="new file for the clip without audio (refuses an existing one)")
    args = parser.parse_args(argv)
    try:
        data = Path(args.clip).read_bytes()
        silent, record = remove_audio(data)
        with open(args.out, "xb") as stream:
            stream.write(silent)
    except (OSError, ValueError) as exc:
        print("error: " + str(exc).encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr)
        return 1
    record["sha256"] = hashlib.sha256(silent).hexdigest()
    print(json.dumps(record, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
