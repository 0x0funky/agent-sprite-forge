"""Shared core for the Agent Sprite Forge skills (API version 1.1).

Canonical home of the helpers every skill needs: no-replace publication, image
I/O, connected components, alpha hygiene, anchors and grids, premultiplied
resampling, and timing/seam metrics.

API 1.1 (Phase 3 integration, D30) adds, without changing any 1.0 name or
behaviour:

* CLI and time: ``run_cli`` (D26/D27 error convention), ``utc_timestamp``,
  ``FORGE_PACKAGE_VERSION`` (D29).
* Paths and JSON: ``manifest_path`` and ``file_ref`` (one relPath rule for
  every manifest writer), ``parse_json`` and ``read_json`` (UTF-8 with an
  optional BOM, D28; opt-in strict mode).
* Rounding and screens: ``round_half_up``, ``parse_aspect``,
  ``aspect_viewport`` and ``cover_window``.
* Masks: ``dilate_square``, ``distance_to`` (capped Chebyshev) and
  ``merge_rects``.
* Sheets and seams: ``ownership_slice`` (D14) and ``edge_seam_report`` (D9).

This file is vendored byte-for-byte into each skill's ``scripts/`` directory as
listed in ``shared/VENDORED.json``. Edit only ``shared/forge_core.py``, then run
``python tools/vendor_sync.py --write`` and ``--check``. Inside a skill, import
the local copy (``sys.path.insert(0, str(Path(__file__).parent))``), never a
sibling skill's copy.

Conventions used throughout:

* Pixel coordinates are continuous: pixel column ``i`` spans ``[i, i + 1)``.
  Boxes are ``(x0, y0, x1, y1)`` with exclusive ``x1``/``y1``.
* A pixel belongs to the subject when ``alpha > threshold``; threshold 0 is
  the legacy ``alpha > 0`` rule.
* Rounding is half-up (``floor(x + 0.5)``), never banker's rounding.
* Images are 8-bit straight-alpha RGBA; published PNGs have RGB zeroed where
  alpha is 0.

Requires numpy and Pillow. scipy is optional: when it is missing, or when
``FORGE_CORE_NO_SCIPY=1``, component labelling uses a vectorised numpy
run-length union-find that returns identical labels.
"""

from __future__ import annotations

import contextlib
import ctypes
import decimal
import errno
import functools
import hashlib
import importlib
import io
import json
import math
import numbers
import operator
import os
import re
import secrets
import shutil
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import numpy as np
from PIL import Image


FORGE_CORE_API_VERSION = "1.1"
FORGE_PACKAGE_VERSION = "0.4.0"  # the integrated release; QA envelopes record it as tool.version (D29)
ALPHA_GEOMETRY_THRESHOLD = 16
BODY_ALPHA_THRESHOLD = 32

HYGIENE_MODES = ("none", "floor", "detached", "both")
ANCHOR_MODES = ("feet", "stance", "bbox", "centroid", "center")
RESAMPLERS = ("box", "lanczos", "nearest")
ASPECT_POLICIES = ("expand", "fixed-height", "fixed-width")
HAZE_POLICIES = ("keep", "drop")
EDGE_SEAM_VERDICTS = ("continuous", "seam", "duplicate_edge", "flat", "too_small")
EDGE_SEAM_DEFECTS = ("seam", "duplicate_edge")  # the verdicts a seam gate fails; flat and too_small are not seams
EDGE_SEAM_NOMINAL_RATIO = 1.25  # verdict gate when the caller sets none (B11's NOMINAL_SEAM_RATIO)
CLI_EXPECTED_ERRORS: tuple[type[BaseException], ...] = (OSError, ValueError, Image.DecompressionBombError)

_CHUNK = 1 << 20


# --------------------------------------------------------------------------- console and dependencies

_ASCII_MAP = str.maketrans({
    "\u2192": "->", "\u2190": "<-", "\u2194": "<->", "\u21d2": "=>",
    "\u2014": "-", "\u2013": "-", "\u2212": "-",
    "\u00d7": "x", "\u00b7": ".", "\u2026": "...",
    "\u2264": "<=", "\u2265": ">=", "\u2260": "!=", "\u2248": "~", "\u00b1": "+/-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " ",
})

_PIP_NAMES = {
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "resvg_py": "resvg-py",
    "pytiled_parser": "pytiled-parser",
}


def utf8_stdio() -> None:
    """Make stdout and stderr UTF-8 with backslash escapes (F-14).

    Call it first in every CLI. A cp1252 or cp950 console can then never turn
    a finished run into a UnicodeEncodeError, and parent processes can decode
    the output as UTF-8. Console text should still be ASCII (see ascii_text).
    Streams without ``reconfigure`` (None under pythonw, test doubles) are left
    alone.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def ascii_text(text: str) -> str:
    """Return console-safe ASCII: arrows, dashes, x, middle dot and common typography
    are spelled out (``->``, ``-``, ``x``, ``.``); anything else is backslash-escaped."""
    return str(text).translate(_ASCII_MAP).encode("ascii", "backslashreplace").decode("ascii")


def require_modules(names: Sequence[str]) -> None:
    """Exit with status 1 and the exact pip command when any module cannot be imported."""
    missing = []
    for name in names:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    if not missing:
        return
    packages = dict.fromkeys(_PIP_NAMES.get(name.split(".")[0], name.split(".")[0]) for name in missing)
    message = (
        f"error: missing Python module(s): {', '.join(missing)}\n"
        f"install with: python -m pip install {' '.join(packages)}\n"
        f"(run it with the interpreter that runs this tool: {sys.executable})"
    )
    print(ascii_text(message), file=sys.stderr)
    raise SystemExit(1)


def _debugging() -> bool:
    return os.environ.get("FORGE_DEBUG", "") not in ("", "0")


def run_cli(main: Callable[..., Any], argv: Sequence[str] | None = None, *,
            expected: tuple[type[BaseException], ...] = CLI_EXPECTED_ERRORS) -> int:
    """Run a CLI ``main`` with the forge error convention and return its exit status (D26, D27).

    utf8_stdio() runs first. ``main()`` is called, or ``main(argv)`` when
    ``argv`` is given; its return value is the exit status (None means 0).
    SystemExit passes through untouched, so argparse usage errors keep their
    exit 2 with ``usage: ... error: ...`` (D26) and ``--help`` exits 0. An
    ``expected`` error (default CLI_EXPECTED_ERRORS: OSError, ValueError, which
    includes JSONDecodeError and UnicodeError, and Pillow's
    DecompressionBombError) prints ``error: <message>`` and returns 1. Any
    other exception prints ``error: internal error (<Type>: <message>)`` and
    returns 1 (D27), and so does any other BaseException, such as the
    pyo3_runtime.PanicException a Rust extension raises when it panics; Ctrl+C
    prints ``error: interrupted`` and returns 130. Messages go to stderr as
    ASCII, and tracebacks never reach the user unless ``FORGE_DEBUG=1``, which
    re-raises for developers. A CLI whose published report has status ``fail``
    returns 1 from ``main`` itself (D26).

    Use: ``if __name__ == "__main__": raise SystemExit(forge_core.run_cli(main))``.
    """
    utf8_stdio()
    try:
        status = main() if argv is None else main(argv)
        return 0 if status is None else int(status)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        if _debugging():
            raise
        print("error: interrupted", file=sys.stderr)
        return 130
    except BaseException as error:  # noqa: BLE001  the catch-all of D27: a bug still reads as one clean line
        if _debugging():
            raise
        if isinstance(error, expected):
            message = str(error) or type(error).__name__
        else:
            message = f"internal error ({type(error).__name__}: {error})"
        print(ascii_text(f"error: {message}"), file=sys.stderr)
        return 1


def utc_timestamp(moment: datetime | None = None) -> str:
    """RFC 3339 UTC time with milliseconds, such as ``2026-10-05T12:00:00.000Z`` (common timestamp).

    ``moment`` defaults to now. An aware datetime is converted to UTC; a naive
    one is taken to be UTC already, as media_ledger.utc_timestamp does.
    """
    if moment is None:
        moment = datetime.now(timezone.utc)
    elif moment.utcoffset() is not None:
        moment = moment.astimezone(timezone.utc)
    return (f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T{moment.hour:02d}:{moment.minute:02d}:"
            f"{moment.second:02d}.{moment.microsecond // 1000:03d}Z")


# --------------------------------------------------------------------------- hashing, paths and JSON

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def portable_path(path: str | os.PathLike, base: str | os.PathLike) -> str:
    """Return ``path`` as a POSIX path relative to the directory ``base`` (MAP-24).

    Pass the directory that holds the manifest, not the manifest file. Inside
    staged_output() the stage and the final directory share a parent, so either
    gives the same answer. Without a relative route (another Windows drive) the
    absolute POSIX path is returned.
    """
    target = Path(path).resolve()
    try:
        return Path(os.path.relpath(target, Path(base).resolve())).as_posix()
    except ValueError:
        return target.as_posix()


# common.schema.json relPath: no leading slash, no drive letter or URL scheme, no backslash.
_REL_PATH = re.compile(r"^(?!/)(?![A-Za-z][A-Za-z0-9+.-]*:)[^\\]+$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def manifest_path(path: str | os.PathLike, base: str | os.PathLike) -> str:
    """``path`` as a common relPath for a manifest in the directory ``base`` (MAP-24, D30).

    portable_path() when its answer is a relPath; otherwise (another drive or
    UNC share, where portable_path returns an absolute path) only the file
    name, bound to the content by the sha256 the caller records beside it.
    Manifests never store absolute paths. One rule replaces the writers'
    regexes (``^[A-Za-z]:``, ``^[A-Za-z][A-Za-z0-9+.-]*:``, PureWindowsPath
    drives, map_bundle's relPath pattern): the result always matches
    common.schema.json's relPath pattern. A name that is empty, ``.`` or
    ``..`` is taken from the resolved path; a name that is still not a relPath
    (a POSIX name such as ``a:b`` or ``a\\b``) raises ValueError.
    """
    relative = portable_path(path, base)
    if _REL_PATH.match(relative):
        return relative
    name = Path(path).name
    if name in ("", ".", ".."):
        name = Path(path).resolve().name
    if name in ("", ".", "..") or not _REL_PATH.match(name):
        raise ValueError(f"{Path(path).as_posix()} has no relative route from {Path(base).as_posix()} and its "
                         "name cannot be stored as a manifest-relative path; rename or move the file.")
    return name


def file_ref(path: str | os.PathLike, base: str | os.PathLike, *, sha256: str | None = None,
             size: int | None = None) -> dict[str, Any]:
    """A common fileRef ``{"path", "sha256", "bytes"}`` of ``path`` for a manifest in the directory ``base``.

    ``path`` follows manifest_path(). ``sha256`` and ``size`` default to the
    file's own; pass the values you already hold (for example of bytes you
    just read or wrote) to bind the reference to exactly that content. A
    passed digest is lower-cased and must be 64 hex digits.
    """
    path = Path(path)
    if sha256:
        digest = str(sha256).lower()
        if not _SHA256_HEX.match(digest):
            raise ValueError(f"sha256 must be 64 hexadecimal digits; got {sha256!r}.")
    else:
        digest = sha256_file(path)
    if size is None:
        length = path.stat().st_size
    else:
        length = operator.index(size)
        if length < 0:
            raise ValueError(f"size must be >= 0; got {size!r}.")
    return {"path": manifest_path(path, base), "sha256": digest, "bytes": length}


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a finite JSON number")
    return value


def parse_json(data: bytes | bytearray | memoryview | str, *, strict: bool = False) -> Any:
    """Parse a JSON document; bytes are UTF-8 with or without a BOM (``utf-8-sig``, D28).

    A str may also start with a BOM (U+FEFF). ``strict`` additionally refuses
    what JSON forbids but Python's json module accepts: duplicate object keys,
    ``NaN``/``Infinity``/``-Infinity`` and number literals that overflow to
    infinity (``1e999``), so a strict document always survives write_json.
    Errors are ValueError (json.JSONDecodeError and UnicodeDecodeError are
    subclasses) without the file name; the caller adds it.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        text = bytes(data).decode("utf-8-sig")
    elif isinstance(data, str):
        text = data[1:] if data.startswith("﻿") else data
    else:
        raise TypeError(f"parse_json needs bytes or str; got {type(data).__name__}.")
    if not strict:
        return json.loads(text)
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_no_constant,
                      parse_float=_finite_float)


def read_json(path: str | os.PathLike, *, strict: bool = False) -> Any:
    """Read a JSON file as UTF-8 with an optional BOM (D28: PowerShell 5.1 writes one); see parse_json.

    OSError (a missing or unreadable file) propagates unchanged; decoding and
    syntax problems raise ValueError without the file name.
    """
    return parse_json(Path(path).read_bytes(), strict=strict)


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    hint = "; store paths with portable_path()" if isinstance(value, os.PathLike) else ""
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable{hint}")


def write_json(path: str | os.PathLike, data: Any, *, no_clobber: bool = True) -> None:
    """Write UTF-8 JSON (indent 2, ``ensure_ascii=False``, trailing newline).

    numpy scalars and arrays are converted; NaN and infinity are refused so the
    file stays valid for strict JSON readers such as browsers. With
    ``no_clobber`` an existing file raises FileExistsError; otherwise the file
    is replaced atomically.
    """
    payload = (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False, default=_json_default)
               + "\n").encode("utf-8")
    path = Path(path)
    if no_clobber:
        with open(path, "xb") as stream:
            try:
                stream.write(payload)
            except BaseException:
                stream.close()
                path.unlink(missing_ok=True)
                raise
        return
    temporary = _create_unique(path.parent, f".{path.name}.", ".tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# --------------------------------------------------------------------------- publishing

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1  # linux/fs.h
_RENAME_EXCL = 4  # macOS stdio.h, for renamex_np
_NO_ATOMIC_RENAME = frozenset(
    code for code in (errno.EINVAL, errno.ENOSYS, getattr(errno, "ENOTSUP", None),
                      getattr(errno, "EOPNOTSUPP", None))
    if code is not None
)


def _create_unique(parent: Path, prefix: str, suffix: str = "", *, directory: bool = False) -> Path:
    """Create an empty file or directory with a random unused name, honouring the umask."""
    for _ in range(100):
        candidate = parent / f"{prefix}{secrets.token_hex(4)}{suffix}"
        try:
            if directory:
                os.mkdir(candidate)
            else:
                os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                                 0o666))
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(errno.EEXIST, "Could not create a unique staging name", str(parent))


@functools.lru_cache(maxsize=1)
def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _rename_noreplace(source: Path, target: Path) -> None:
    """Rename atomically, refusing any existing ``target``; failures raise OSError with errno set."""
    if os.name == "nt":
        os.rename(source, target)  # MoveFileEx without REPLACE_EXISTING refuses even an empty directory
        return
    try:
        libc = _libc()
    except OSError as error:
        raise OSError(errno.ENOSYS, f"libc unavailable: {error}", str(target)) from error
    if sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        arguments = (_AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(target), _RENAME_NOREPLACE)
    elif sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(source), os.fsencode(target), _RENAME_EXCL)
    else:
        rename = None
    if rename is None:
        raise OSError(errno.ENOSYS, "No atomic no-replace rename on this platform", str(target))
    rename.argtypes = argtypes
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


def _publish_by_exclusive_mkdir(stage: Path, final: Path) -> None:
    """Fallback publish: exclusive mkdir, move the children, remove the stage.

    The mkdir refuses anything that already exists. Until the last child has
    moved, other processes can see ``final`` partially populated, and a process
    that writes into the brand-new ``final`` in that window can collide with a
    child. On failure the moved children are returned to ``stage``.
    """
    os.mkdir(final)
    moved: list[tuple[Path, Path]] = []
    try:
        for child in sorted(stage.iterdir()):
            target = final / child.name
            os.rename(child, target)
            moved.append((target, child))
        os.rmdir(stage)
    except BaseException:
        for target, child in reversed(moved):
            with contextlib.suppress(OSError):
                os.rename(target, child)
        with contextlib.suppress(OSError):
            os.rmdir(final)
        raise


def publish_directory_no_replace(stage: str | os.PathLike, final: str | os.PathLike) -> None:
    """Publish the finished directory ``stage`` as ``final`` without replacing anything (F-03).

    ``stage`` must sit on the same filesystem as ``final``; staged_output()
    creates it beside ``final``. The rename is atomic where the OS has a
    no-replace rename: os.rename on Windows, renameat2(RENAME_NOREPLACE) on
    Linux, renamex_np(RENAME_EXCL) on macOS. When that primitive fails with
    EINVAL, ENOTSUP or ENOSYS (WSL drvfs mounts such as /mnt/c, some network
    filesystems, old kernels) the publish falls back to an exclusive mkdir and
    moving the children, which is not atomic: see _publish_by_exclusive_mkdir.
    An existing ``final``, even an empty directory created by a racing process,
    always raises FileExistsError and leaves ``stage`` untouched.
    """
    stage, final = Path(stage), Path(final)
    if os.path.lexists(final):
        raise FileExistsError(errno.EEXIST, "Refusing to replace existing output", str(final))
    try:
        _rename_noreplace(stage, final)
    except OSError as error:
        if error.errno not in _NO_ATOMIC_RENAME:
            raise
        _publish_by_exclusive_mkdir(stage, final)


def _copy_exclusive(source: Path, destination: Path) -> None:
    """Copy into a file that must not exist yet (open 'xb'), fsync, remove our partial copy on failure."""
    with open(source, "rb") as input_file:
        output_file = open(destination, "xb")
        try:
            with output_file:
                shutil.copyfileobj(input_file, output_file, _CHUNK)
                output_file.flush()
                os.fsync(output_file.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def publish_file_no_replace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    """Publish a complete copy of ``src`` as ``dst``, never replacing ``dst``.

    The copy is written and fsynced beside ``dst`` and then hard-linked into
    place, so readers see either nothing or the whole file. Where hard links
    are unavailable (FAT/exFAT, some network or WSL mounts) it falls back to
    an exclusive ``open('xb')`` + fsync, which still never replaces but may be
    observed half-written. ``src`` is left in place. Callers publishing several
    sidecars roll back the ones already published when a later step fails.
    """
    src, dst = Path(src), Path(dst)
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "Refusing to replace existing file", str(dst))
    dst.parent.mkdir(parents=True, exist_ok=True)
    temporary = _create_unique(dst.parent, f".{dst.name}.", ".staged")
    try:
        with open(src, "rb") as input_file, open(temporary, "wb") as output_file:
            shutil.copyfileobj(input_file, output_file, _CHUNK)
            output_file.flush()
            os.fsync(output_file.fileno())
        try:
            os.link(temporary, dst)
        except FileExistsError:
            raise
        except (OSError, AttributeError, NotImplementedError):
            _copy_exclusive(temporary, dst)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def staged_output(final: str | os.PathLike, prefix: str = "") -> Iterator[Path]:
    """Yield a new stage directory beside ``final``; publish it as ``final`` on a clean exit.

    ``final`` must not exist. Work and QA happen inside the stage. On any
    exception, including a failed publish, the stage is deleted and ``final``
    is never created, so a failed run leaves nothing behind. The stage shares
    ``final``'s parent, so relative paths computed inside it stay valid.
    """
    final = Path(final)
    if final.name in ("", ".", ".."):
        raise ValueError(f"Output directory needs a name: {final}")
    final = final.parent.resolve() / final.name
    if os.path.lexists(final):
        raise FileExistsError(errno.EEXIST, "Refusing to replace existing output", str(final))
    final.parent.mkdir(parents=True, exist_ok=True)
    stage = _create_unique(final.parent, f".{prefix}{final.name}.stage-", directory=True)
    try:
        yield stage
        publish_directory_no_replace(stage, final)
    finally:
        if os.path.lexists(stage):
            shutil.rmtree(stage, ignore_errors=True)


# --------------------------------------------------------------------------- image I/O

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SIXTEEN_BIT_GREY = ("I;16", "I;16B", "I;16L", "I;16N")
_MODE_BITS = {"1": 1, "I;16": 16, "I;16B": 16, "I;16L": 16, "I;16N": 16, "I": 32, "F": 32}


def _png_bit_depth(raw: bytes) -> int | None:
    if len(raw) >= 26 and raw[:8] == _PNG_SIGNATURE and raw[12:16] == b"IHDR":
        return raw[24]
    return None


def _to_rgba8(image: Image.Image, bit_depth: int) -> tuple[Image.Image, str]:
    """Convert a loaded frame to 8-bit RGBA and describe the conversion."""
    mode = image.mode
    transparency = image.info.get("transparency")
    if mode in _SIXTEEN_BIT_GREY or (mode == "I" and bit_depth == 16):
        values = np.asarray(image).astype(np.int64)
        grey = (values >> 8).astype(np.uint8)
        alpha = np.full(grey.shape, 255, np.uint8)
        note = f"{mode} 16-bit grey -> RGBA (>> 8)"
        if isinstance(transparency, int):
            alpha[values == transparency] = 0
            note += " + tRNS"
        return Image.fromarray(np.dstack([grey, grey, grey, alpha])), note
    if mode in ("I", "F"):
        raise ValueError(f"Unsupported {bit_depth}-bit {mode} image; export an 8- or 16-bit PNG.")
    decoder_shift = " (16-bit samples >> 8 by the decoder)" if bit_depth == 16 else ""
    if mode == "RGBA":
        return image.copy(), ("none" if not decoder_shift else "RGBA" + decoder_shift)
    note = mode if bit_depth >= 8 or mode == "1" else f"{mode} ({bit_depth}-bit)"
    if transparency is not None:
        note += "+tRNS"
    if mode == "CMYK":
        return image.convert("RGB").convert("RGBA"), "CMYK -> RGB (naive, ICC ignored) -> RGBA"
    return image.convert("RGBA"), f"{note} -> RGBA{decoder_shift}"


def load_rgba(path: str | os.PathLike, *, allow_animated: bool = False) -> tuple[Image.Image, dict[str, Any]]:
    """Load a still image as 8-bit straight-alpha RGBA, with provenance (S16, S23, MAP-19).

    Accepts RGB(A), palette PNGs with tRNS (including 1/2/4-bit), P/PA, L/LA,
    1-bit, 16-bit grey (high byte, ``>> 8``), 16-bit RGB(A) (high byte, taken
    by Pillow's decoder) and CMYK (naive conversion). Animated inputs raise
    ValueError unless ``allow_animated``, which loads frame 0. RGB hidden under
    alpha 0 is kept as stored; EXIF orientation and ICC profiles are ignored.

    The provenance dict holds ``path``, ``sha256``, ``bytes``, ``format``,
    ``source_mode``, ``bit_depth``, ``size``, ``frames`` and ``conversion``.
    """
    path = Path(path)
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as source:
        frames = int(getattr(source, "n_frames", 1))
        if frames > 1 and not allow_animated:
            raise ValueError(f"Input is animated ({frames} frames); export a single still frame: {path}")
        source.load()
        bit_depth = _png_bit_depth(raw) or _MODE_BITS.get(source.mode, 8)
        image, conversion = _to_rgba8(source, bit_depth)
        info = {
            "path": path.resolve().as_posix(),
            "sha256": sha256_bytes(raw),
            "bytes": len(raw),
            "format": source.format,
            "source_mode": source.mode,
            "bit_depth": int(bit_depth),
            "size": list(image.size),
            "frames": frames,
            "conversion": conversion if frames == 1 else f"{conversion}; frame 0 of {frames}",
        }
    return image, info


def save_png(img: Image.Image | np.ndarray, path: str | os.PathLike, *, zero_transparent_rgb: bool = True) -> None:
    """Write an 8-bit RGBA, RGB, LA or L PNG deterministically, without metadata chunks.

    Only IHDR, IDAT and IEND are written (no text, time, ICC or EXIF carried
    over from the source). RGB under alpha 0 is zeroed unless
    ``zero_transparent_rgb`` is False. Indexed PNG is not written here.
    """
    image = img if isinstance(img, Image.Image) else Image.fromarray(np.asarray(img))
    if image.mode not in ("RGBA", "RGB", "LA", "L"):
        raise ValueError(f"save_png writes 8-bit RGBA, RGB, LA or L images; got mode {image.mode}.")
    if zero_transparent_rgb and image.mode in ("RGBA", "LA"):
        pixels = np.array(image)
        pixels[pixels[..., -1] == 0] = 0
        clean = Image.fromarray(pixels)
    else:
        clean = Image.frombytes(image.mode, image.size, image.tobytes())
    buffer = io.BytesIO()
    clean.save(buffer, format="PNG", optimize=False, compress_level=6)
    Path(path).write_bytes(buffer.getvalue())


# --------------------------------------------------------------------------- masks and components

def _scipy_ndimage() -> Any:
    """scipy.ndimage, or None when scipy is missing or FORGE_CORE_NO_SCIPY forces the numpy fallback."""
    if os.environ.get("FORGE_CORE_NO_SCIPY", "") not in ("", "0"):
        return None
    try:
        from scipy import ndimage
    except ImportError:
        return None
    return ndimage


def _alpha_plane(source: Any) -> np.ndarray:
    """2-D alpha (or mask) plane of an Image or array; images without alpha count as opaque."""
    if isinstance(source, Image.Image):
        if source.mode in ("L", "1"):
            return np.asarray(source)
        if "A" not in source.getbands():
            source = source.convert("RGBA")
        return np.asarray(source.getchannel("A"))
    array = np.asarray(source)
    if array.ndim == 2:
        return array
    if array.ndim == 3 and array.shape[2] in (2, 4):
        return array[..., -1]
    if array.ndim == 3 and array.shape[2] == 3:
        return np.full(array.shape[:2], 255, np.uint8)
    raise ValueError(f"Expected an alpha plane, a mask or an RGBA image; got shape {array.shape}.")


def _as_mask(mask: Any) -> np.ndarray:
    plane = _alpha_plane(mask)
    return plane if plane.dtype == bool else plane > 0


def _mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return None
    columns = np.flatnonzero(mask.any(axis=0))
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


def subject_mask(alpha: Any, threshold: int = ALPHA_GEOMETRY_THRESHOLD) -> np.ndarray:
    """Pixels that count for geometry: ``alpha > threshold`` (S01). Boolean input is returned as is."""
    plane = _alpha_plane(alpha)
    return plane.copy() if plane.dtype == bool else plane > threshold


def subject_bbox(alpha: Any, threshold: int = ALPHA_GEOMETRY_THRESHOLD) -> tuple[int, int, int, int] | None:
    """``(x0, y0, x1, y1)`` of subject_mask(alpha, threshold), or None when it is empty."""
    return _mask_bbox(subject_mask(alpha, threshold))


def _hook_and_compress(count: int, first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """Union-find over ``count`` nodes and edges first[i]-second[i], fully vectorised.

    Each round hooks every root to the smallest root it touches, then
    pointer-jumps until each node points at its root. A root never moves to a
    larger index, so every component ends rooted at its smallest node.
    """
    parent = np.arange(count, dtype=np.int64)
    while first.size:
        root_a, root_b = parent[first], parent[second]
        active = root_a != root_b
        if not active.any():
            break
        first, second = first[active], second[active]
        low = np.minimum(root_a[active], root_b[active])
        high = np.maximum(root_a[active], root_b[active])
        np.minimum.at(parent, high, low)
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
    return parent


def _label_runs(mask: np.ndarray, connectivity: int) -> tuple[np.ndarray, int]:
    """Numpy run-length union-find labelling; labels follow raster order like scipy.ndimage.label."""
    height, width = mask.shape
    labels = np.zeros((height, width), np.int32)
    stride = width + 2
    padded = np.zeros((height, stride), np.int8)
    padded[:, 1:-1] = mask
    delta = np.diff(padded.ravel())
    starts = np.flatnonzero(delta == 1) + 1
    if starts.size == 0:
        return labels, 0
    ends = np.flatnonzero(delta == -1) + 1
    rows = starts // stride
    run_start = starts - rows * stride - 1
    run_end = ends - rows * stride - 1  # exclusive
    # Keys are monotonic over all runs, so one searchsorted finds the touching runs of the previous row.
    key_start, key_end = rows * stride + run_start, rows * stride + run_end
    previous = (rows - 1) * stride
    if connectivity == 8:  # touching includes diagonal contact
        low = np.searchsorted(key_end, previous + run_start, side="left")
        high = np.searchsorted(key_start, previous + run_end, side="right")
    else:
        low = np.searchsorted(key_end, previous + run_start, side="right")
        high = np.searchsorted(key_start, previous + run_end, side="left")
    counts = np.maximum(high - low, 0)
    total = int(counts.sum())
    run_count = starts.size
    if total:
        source = np.repeat(np.arange(run_count), counts)
        offsets = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
        parent = _hook_and_compress(run_count, source, np.repeat(low, counts) + offsets)
    else:
        parent = np.arange(run_count)
    rank = np.cumsum(parent == np.arange(run_count))
    labels.reshape(-1)[np.flatnonzero(mask)] = np.repeat(rank[parent].astype(np.int32), run_end - run_start)
    return labels, int(rank[-1])


def label_components(mask: Any, connectivity: int = 8) -> tuple[np.ndarray, int]:
    """Label connected True pixels: ``(labels int32, count)`` with labels 1..count in raster order.

    8-connectivity is the default so 1-px diagonal strokes stay whole (S04,
    MAP-06). Uses scipy.ndimage when available; otherwise, or with
    ``FORGE_CORE_NO_SCIPY=1``, an equivalent numpy run-length union-find.
    """
    if connectivity not in (4, 8):
        raise ValueError(f"connectivity must be 4 or 8; got {connectivity!r}.")
    mask = np.asarray(mask, dtype=bool)
    if mask.ndim != 2:
        raise ValueError(f"label_components needs a 2-D mask; got shape {mask.shape}.")
    if mask.size == 0:
        return np.zeros(mask.shape, np.int32), 0
    ndimage = _scipy_ndimage()
    if ndimage is None:
        return _label_runs(mask, connectivity)
    structure = np.ones((3, 3), bool) if connectivity == 8 else ndimage.generate_binary_structure(2, 1)
    labels, count = ndimage.label(mask, structure=structure)
    return labels.astype(np.int32, copy=False), int(count)


def connected_components(alpha_or_rgba: Any, *, threshold: int = 0, min_area: int = 1, connectivity: int = 8,
                         with_masks: bool = False) -> list[dict[str, Any]]:
    """Components of ``alpha > threshold``, largest first (ties keep raster order).

    Accepts an alpha plane, a boolean mask (threshold ignored), an RGBA array
    or an Image. Each dict holds ``label`` (as in label_components), ``area``,
    ``bbox`` ``(x0, y0, x1, y1)`` and ``touches_edge``; with ``with_masks`` it
    also holds ``mask``, a boolean array cropped to ``bbox``. Components
    smaller than ``min_area`` are omitted.
    """
    plane = _alpha_plane(alpha_or_rgba)
    mask = plane if plane.dtype == bool else plane > threshold
    labels, count = label_components(mask, connectivity)
    if count == 0:
        return []
    height, width = mask.shape
    areas = np.bincount(labels.ravel(), minlength=count + 1)
    ys, xs = np.nonzero(labels)
    ids = labels[ys, xs]
    x0 = np.full(count + 1, width, np.int64)
    y0 = np.full(count + 1, height, np.int64)
    x1 = np.zeros(count + 1, np.int64)
    y1 = np.zeros(count + 1, np.int64)
    np.minimum.at(x0, ids, xs)
    np.minimum.at(y0, ids, ys)
    np.maximum.at(x1, ids, xs + 1)
    np.maximum.at(y1, ids, ys + 1)
    kept = np.flatnonzero(areas[1:] >= min_area) + 1
    components = []
    for label in kept[np.argsort(-areas[kept], kind="stable")].tolist():
        box = (int(x0[label]), int(y0[label]), int(x1[label]), int(y1[label]))
        item: dict[str, Any] = {
            "label": label,
            "area": int(areas[label]),
            "bbox": box,
            "touches_edge": box[0] == 0 or box[1] == 0 or box[2] == width or box[3] == height,
        }
        if with_masks:
            item["mask"] = labels[box[1]:box[3], box[0]:box[2]] == label
        components.append(item)
    return components


def _mask2d(mask: Any, name: str) -> np.ndarray:
    array = np.asarray(mask, dtype=bool)
    if array.ndim != 2:
        raise ValueError(f"{name} needs a 2-D mask; got shape {array.shape}.")
    return array


def dilate_square(mask: Any, radius: int) -> np.ndarray:
    """Chebyshev (square) dilation of a mask by ``radius`` px, from a summed-area table (D30).

    A pixel is True when a True pixel lies within ``radius`` px in x and in y.
    Windows are clipped at the canvas, so the exterior never counts: on a mask
    this equals Pillow's MaxFilter(2 * radius + 1). ``radius`` <= 0 returns a
    copy. One pass for any radius; a radius beyond the image is clamped, which
    changes nothing. Non-boolean masks mean ``!= 0``.
    """
    mask = _mask2d(mask, "dilate_square")
    radius = min(operator.index(radius), max(mask.shape, default=0))
    if radius <= 0 or mask.size == 0:
        return mask.copy()
    size = 2 * radius + 1
    accumulator = np.int32 if mask.size < 2 ** 31 else np.int64  # the table never exceeds the True count
    table = np.pad(np.pad(mask, radius).astype(accumulator).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    window = table[size:, size:] - table[:-size, size:] - table[size:, :-size] + table[:-size, :-size]
    return window > 0


_dilate_square = dilate_square  # the 1.0 private name, used by alpha_hygiene


def _grow8(mask: np.ndarray) -> np.ndarray:
    """One 3 x 3 (8-neighbour) dilation step; the canvas exterior never counts."""
    grown = mask.copy()
    grown[:, 1:] |= mask[:, :-1]
    grown[:, :-1] |= mask[:, 1:]
    result = grown.copy()
    result[1:] |= grown[:-1]
    result[:-1] |= grown[1:]
    return result


def distance_to(mask: Any, cap: int) -> np.ndarray:
    """Chebyshev (8-neighbour) distance from every pixel to the nearest True pixel of ``mask``, capped (D30).

    Pixels farther than ``cap``, and every pixel of an empty mask, get
    ``cap + 1``; the canvas exterior is not part of the mask. The dtype is
    uint8 while ``cap + 1`` fits (cap <= 254), then uint16, then int32. For
    every ``r <= cap``, ``distance_to(mask, cap) <= r`` equals
    ``dilate_square(mask, r)``. Ring by ring, so the cost grows with ``cap``
    (it stops early once nothing new is reached): meant for small caps such as
    forge_matte's alpha and colour bands.
    """
    mask = _mask2d(mask, "distance_to")
    cap = operator.index(cap)
    if cap < 0:
        raise ValueError(f"cap must be >= 0; got {cap}.")
    dtype = np.uint8 if cap < 255 else (np.uint16 if cap < 65535 else np.int32)
    distance = np.full(mask.shape, cap + 1, dtype)
    distance[mask] = 0
    reached = mask.copy()
    for step in range(1, cap + 1):
        grown = _grow8(reached)
        new = grown & ~reached
        if not new.any():
            break  # nothing left to reach (all reached, or an empty mask): the rest keep cap + 1
        distance[new] = step
        reached = grown
    return distance


def merge_rects(mask: Any) -> list[tuple[int, int, int, int]]:
    """Cover the True cells of a 2-D grid with disjoint rectangles ``(x, y, w, h)``; their union is exact (D30).

    The greedy cover of B13's map_bundle.merge_rects, rectangle for rectangle:
    rows are scanned top to bottom, and each maximal run of uncovered True
    cells in a row starts a rectangle that grows downward while every cell
    under the run stays True. Rectangles are listed by their top row, then
    left to right. Vectorised per row: a run grows by the shortest downward
    run of True cells beneath it (cells below a run can never be covered yet,
    because an earlier rectangle reaching them would also cover the run).
    """
    blocked = _mask2d(mask, "merge_rects")
    rows, cols = blocked.shape
    if not blocked.any():
        return []
    index = np.arange(rows, dtype=np.int32)[:, None]
    first_false = np.minimum.accumulate(np.where(blocked, np.int32(rows), index)[::-1], axis=0)[::-1]
    down = np.append(first_false - index, np.zeros((rows, 1), np.int32), axis=1)  # True cells from (y, x) down
    covered_until = np.zeros(cols, np.int64)  # column x is covered on rows below covered_until[x]
    padded = np.zeros(cols + 2, np.int8)
    rects: list[tuple[int, int, int, int]] = []
    for y in range(rows):
        free = blocked[y] & (covered_until <= y)
        if not free.any():
            continue
        padded[1:-1] = free
        edges = np.flatnonzero(np.diff(padded))
        starts, ends = edges[0::2], edges[1::2]
        heights = np.minimum.reduceat(down[y], edges)[0::2]  # the sentinel column keeps every index valid
        covered_until[free] = np.repeat(y + heights, ends - starts)
        rects.extend(zip(starts.tolist(), [y] * len(starts), (ends - starts).tolist(), heights.tolist()))
    return rects


def _rgba_array(source: Any) -> np.ndarray:
    """8-bit straight-alpha RGBA array view of an Image or array; premultiplied modes are refused."""
    if isinstance(source, Image.Image):
        if source.mode in ("RGBa", "La"):
            raise ValueError(f"Expected straight alpha; got premultiplied mode {source.mode}.")
        return np.asarray(source if source.mode == "RGBA" else source.convert("RGBA"))
    array = np.asarray(source)
    if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] not in (3, 4):
        raise ValueError(f"Expected an 8-bit RGB or RGBA array; got {array.dtype} {array.shape}.")
    if array.shape[2] == 3:
        return np.dstack([array, np.full(array.shape[:2], 255, np.uint8)])
    return array


def alpha_hygiene(rgba: Any, mode: str = "both", floor: int = 4, solid_min: int = 32,
                  attach_radius: int = 2) -> tuple[Image.Image, dict[str, Any]]:
    """Remove generator alpha noise without touching visible art (DOC-04, report v2 P1-4).

    ``floor`` zeroes pixels with ``0 < alpha <= floor``. ``detached`` removes
    8-connected ``alpha > 0`` islands with no pixel within ``attach_radius``
    (Chebyshev) of a solid pixel (``alpha >= solid_min``), so faint glow that
    hugs the art survives. ``both`` applies floor, then detached. Removed
    pixels become (0, 0, 0, 0); every other pixel is unchanged. The report
    holds ``floor_px``, ``detached_px``, ``detached_components`` and
    ``max_removed_alpha`` with the settings used.
    """
    if mode not in HYGIENE_MODES:
        raise ValueError(f"Unknown hygiene mode {mode!r}; use one of {', '.join(HYGIENE_MODES)}.")
    if not 0 <= floor <= 255 or not 1 <= solid_min <= 255 or attach_radius < 0:
        raise ValueError("Hygiene needs 0 <= floor <= 255, 1 <= solid_min <= 255 and attach_radius >= 0.")
    pixels = _rgba_array(rgba).copy()
    alpha = pixels[..., 3]
    removed = np.zeros(alpha.shape, bool)
    report: dict[str, Any] = {
        "mode": mode, "floor": floor, "solid_min": solid_min, "attach_radius": attach_radius,
        "connectivity": 8, "floor_px": 0, "detached_px": 0, "detached_components": 0, "max_removed_alpha": 0,
    }
    if mode in ("floor", "both"):
        removed = (alpha > 0) & (alpha <= floor)
        report["floor_px"] = int(removed.sum())
    if mode in ("detached", "both"):
        visible = (alpha > 0) & ~removed
        labels, count = label_components(visible, 8)
        if count:
            anchored = np.zeros(count + 1, bool)
            anchored[labels[_dilate_square(visible & (alpha >= solid_min), attach_radius)]] = True
            anchored[0] = True
            detached = ~anchored[labels]
            report["detached_px"] = int(detached.sum())
            report["detached_components"] = int(count + 1 - anchored.sum())
            removed |= detached
    if removed.any():
        report["max_removed_alpha"] = int(alpha[removed].max())
        pixels[removed] = 0
    return Image.fromarray(pixels), report


# --------------------------------------------------------------------------- rounding and screen aspect

_HALF = Fraction(1, 2)
_DECIMAL_HALF = decimal.Decimal("0.5")
_ASPECT = re.compile(r"\s*([0-9]*\.?[0-9]+)\s*(?:[:/]\s*([0-9]*\.?[0-9]+))?\s*")


def round_half_up(value: Any) -> int:
    """Nearest integer with halves rounded up, ``floor(value + 1/2)``: 2.5 -> 3, -2.5 -> -2 (MAP-21, D30).

    Never banker's rounding. Integers come back unchanged; Fractions (any
    exact rational) and Decimals round exactly. Floats, numpy floats
    included, compute ``floor(value + 0.5)`` in floating point exactly as the
    private copies did, so the one float below a half that the addition
    rounds up (0.49999999999999994) gives 1; pass a Fraction for exact
    rational timing. NaN raises ValueError and infinity OverflowError.
    """
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Rational):
        return int(math.floor(value + _HALF))
    if isinstance(value, decimal.Decimal):
        return int(math.floor(value + _DECIMAL_HALF))
    return int(math.floor(value + 0.5))


_round_half_up = round_half_up  # the 1.0 private name


def parse_aspect(text: Any) -> tuple[float, float]:
    """A screen aspect as ``(width share, height share)``: ``16:9``, ``19.5:9``, ``16/9`` or a ratio such as ``1.7778``.

    Shares are kept, not divided, so ``19.5:9`` on 540 rows is exactly 1170
    columns. Anything else, or a share that is zero or not finite, raises
    ValueError.
    """
    match = _ASPECT.fullmatch(str(text))
    if not match:
        raise ValueError(f"aspect {text!r} must look like 16:9 or 1.7778.")
    width, height = float(match.group(1)), float(match.group(2) or 1)
    if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
        raise ValueError(f"aspect {text!r} must be a positive ratio.")
    return width, height


def aspect_viewport(viewport: Sequence[float], aspect: Sequence[float], policy: str) -> list[float]:
    """``[width, height]`` a screen of ``aspect`` shows for a ``viewport`` designed at another aspect.

    ``expand`` keeps the whole designed viewport and grows the other side,
    ``fixed-height`` keeps the height and ``fixed-width`` the width (see
    ASPECT_POLICIES). ``aspect`` is a parse_aspect() pair.
    """
    if policy not in ASPECT_POLICIES:
        raise ValueError(f"Unknown aspect policy {policy!r}; use one of {', '.join(ASPECT_POLICIES)}.")
    width, height = viewport
    share_w, share_h = aspect
    if policy == "fixed-height" or (policy == "expand" and share_w * height >= width * share_h):
        return [height * share_w / share_h, height]
    return [width, width * share_h / share_w]


def cover_window(size: Sequence[float], aspect: Sequence[float], zoom: float = 1.0,
                 focus: Sequence[float] = (0.5, 0.5)) -> tuple[float, float, float, float]:
    """The part ``(x0, y0, x1, y1)`` of a ``size`` plate that a cover-cropping runtime shows on an ``aspect`` screen.

    The largest window of the screen's aspect that fits the plate, divided by
    a plate-pan ``zoom`` (> 0), placed at ``focus`` (0..1 in x and y; 0.5, 0.5
    centres it, as ImageOps.fit does) (game-opus55 drawCoverR).
    """
    zoom = float(zoom)
    if not math.isfinite(zoom) or zoom <= 0:
        raise ValueError(f"zoom must be a positive finite number; got {zoom!r}.")
    width, height = size
    share_w, share_h = aspect
    if share_w * height >= width * share_h:
        window_w, window_h = float(width), width * share_h / share_w
    else:
        window_w, window_h = height * share_w / share_h, float(height)
    window_w, window_h = window_w / zoom, window_h / zoom
    left, top = (width - window_w) * focus[0], (height - window_h) * focus[1]
    return left, top, left + window_w, top + window_h


# --------------------------------------------------------------------------- anchors and grids


def ground_row(mask: Any, *, min_run: int = 1) -> int:
    """Ground line: bottom edge of the lowest stable row, i.e. that row's index + 1 (S14).

    A row is stable when it holds at least ``min_run`` mask pixels; raise
    ``min_run`` to ignore thin tips or specks below the feet.
    """
    rows = np.flatnonzero(_as_mask(mask).sum(axis=1) >= max(1, min_run))
    if rows.size == 0:
        raise ValueError("ground_row needs a mask with at least one stable row.")
    return int(rows[-1]) + 1


def anchor_from_mask(mask: Any, mode: str = "feet", band_rows: int = 12,
                     subject_bbox: Sequence[int] | None = None, *, min_run: int = 1) -> tuple[float, float]:
    """Registration anchor ``(x, y)`` of a subject mask, in continuous pixel coordinates.

    ``feet``: median column of the support band, on the ground line.
    ``stance``: midpoint of the support band's horizontal extent, on the
    ground line; it mirrors exactly, so a turn does not slide the feet.
    ``bbox``: horizontal centre and bottom edge of the bounding box.
    ``centroid``: mean pixel centre. ``center``: centre of the bounding box.
    The support band is the ``band_rows`` rows above ground_row(); scale it
    with the subject height. ``subject_bbox`` restricts every mode to that box
    (the selected component's), so a weapon outside it cannot move the
    anchor (S15).
    """
    if mode not in ANCHOR_MODES:
        raise ValueError(f"Unknown anchor mode {mode!r}; use one of {', '.join(ANCHOR_MODES)}.")
    if band_rows < 1:
        raise ValueError("band_rows must be at least 1.")
    mask = _as_mask(mask)
    if subject_bbox is not None:
        left, top, right, bottom = (max(0, int(value)) for value in subject_bbox)
        restricted = np.zeros_like(mask)
        restricted[top:bottom, left:right] = mask[top:bottom, left:right]
        mask = restricted
    box = _mask_bbox(mask)
    if box is None:
        raise ValueError("anchor_from_mask needs a non-empty mask.")
    x0, y0, x1, y1 = box
    if mode == "center":
        return (x0 + x1) / 2, (y0 + y1) / 2
    if mode == "bbox":
        return (x0 + x1) / 2, float(y1)
    if mode == "centroid":
        ys, xs = np.nonzero(mask)
        return float(xs.mean()) + 0.5, float(ys.mean()) + 0.5
    ground = ground_row(mask, min_run=min_run)
    band = mask[max(0, ground - band_rows):ground]
    if mode == "stance":
        columns = np.flatnonzero(band.any(axis=0))
        return (int(columns[0]) + int(columns[-1]) + 1) / 2, float(ground)
    return float(np.median(np.nonzero(band)[1])) + 0.5, float(ground)


def rounded_grid_boxes(width: int, height: int, rows: int, cols: int) -> list[tuple[int, int, int, int]]:
    """Row-major cell boxes whose edges are ``round_half_up(i * size / n)`` (MAP-04, DOC-03).

    Cells differ by at most 1 px, cover every pixel exactly once and need no
    divisibility: 1254 px in 4 columns gives widths 314, 313, 314, 313.
    """
    if rows < 1 or cols < 1:
        raise ValueError("Grid rows and columns must be positive.")
    if width < cols or height < rows:
        raise ValueError(f"A {width}x{height} image cannot hold {rows} rows x {cols} columns of cells.")
    xs = [(2 * index * width + cols) // (2 * cols) for index in range(cols + 1)]
    ys = [(2 * index * height + rows) // (2 * rows) for index in range(rows + 1)]
    return [(xs[col], ys[row], xs[col + 1], ys[row + 1]) for row in range(rows) for col in range(cols)]


def pad_to_grid(img: Image.Image, rows: int, cols: int,
                fill: Any = (0, 0, 0, 0)) -> tuple[Image.Image, tuple[int, int]]:
    """Pad losslessly so the size divides into ``rows`` x ``cols`` cells (DOC-03).

    Padding is split evenly, the odd pixel going right/bottom. Returns the new
    image and the ``(left, top)`` offset of the original inside it. Pass the
    key colour as ``fill`` for chroma-key sheets.
    """
    if rows < 1 or cols < 1:
        raise ValueError("Grid rows and columns must be positive.")
    width, height = img.size
    extra_x, extra_y = -width % cols, -height % rows
    offset = (extra_x // 2, extra_y // 2)
    if not extra_x and not extra_y:
        return img.copy(), offset
    canvas = Image.new(img.mode, (width + extra_x, height + extra_y), fill)
    canvas.paste(img, offset)
    return canvas, offset


def integer_scale(scale: float, tol: float = 1e-6) -> int:
    """Return ``scale`` as a positive integer; raise ValueError for fractional scales (S06)."""
    value = float(scale)
    nearest = round(value) if math.isfinite(value) else 0
    if nearest < 1 or abs(value - nearest) > tol:
        raise ValueError(f"Nearest resampling needs an integer scale (1, 2, 3, ...); got {scale!r}.")
    return int(nearest)


# --------------------------------------------------------------------------- sheets

# The neighbour order of the report v2 prototype (sheet_checks.py): left, right, above, below, then the diagonals.
_ATTACH_DIRECTIONS = ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1))


def _slice_boxes(width: int, height: int, rows: Any, cols: Any, boxes: Any) -> list[tuple[int, int, int, int]]:
    if boxes is None:
        if rows is None or cols is None:
            raise ValueError("ownership_slice needs rows and cols, or boxes=.")
        return rounded_grid_boxes(width, height, operator.index(rows), operator.index(cols))
    if rows is not None or cols is not None:
        raise ValueError("Pass rows and cols, or boxes=, not both.")
    cells = []
    for box in boxes:
        x0, y0, x1, y1 = (operator.index(value) for value in box)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError(f"Box {[x0, y0, x1, y1]} must lie inside the {width}x{height} image with x0 < x1 "
                             "and y0 < y1.")
        cells.append((x0, y0, x1, y1))
    if not cells:
        raise ValueError("ownership_slice needs at least one box.")
    return cells


def _attach_soft(owner: np.ndarray, soft: np.ndarray, radius: int) -> tuple[np.ndarray, int]:
    """Give ``soft`` pixels the owner they reach within ``radius`` 8-steps through soft pixels.

    Breadth-first and synchronous: each step reads the owners of the step
    before, and the first neighbour in _ATTACH_DIRECTIONS order that is owned
    wins. Only soft pixels within ``radius`` px of an owned pixel can be
    reached, so only those are visited. Returns ``(owner, attached_px)``.
    """
    height, width = owner.shape
    owner = owner.copy()
    ys, xs = np.nonzero(soft & dilate_square(owner >= 0, radius))
    attached = 0
    for _ in range(radius):
        if ys.size == 0:
            break
        found = np.full(ys.size, -1, owner.dtype)
        for dy, dx in _ATTACH_DIRECTIONS:
            open_ = found < 0
            sy, sx = ys[open_] - dy, xs[open_] - dx
            inside = (sy >= 0) & (sy < height) & (sx >= 0) & (sx < width)
            values = np.full(sy.size, -1, owner.dtype)
            values[inside] = owner[sy[inside], sx[inside]]
            found[np.flatnonzero(open_)[values >= 0]] = values[values >= 0]
        reached = found >= 0
        owner[ys[reached], xs[reached]] = found[reached]
        attached += int(reached.sum())
        ys, xs = ys[~reached], xs[~reached]
    return owner, attached


def ownership_slice(rgba: Any, rows: int | None = None, cols: int | None = None, *,
                    boxes: Sequence[Sequence[int]] | None = None, alpha_threshold: int, min_area: int, haze: str,
                    attach_radius: int = 6, count: int | None = None) -> tuple[list[np.ndarray], dict[str, Any]]:
    """Slice a sheet so no subject is cut: each frame keeps its cell origin plus one shared padding (D14).

    Cells are ``rounded_grid_boxes(width, height, rows, cols)``, or explicit
    ``boxes`` ``(x0, y0, x1, y1)`` (crop boxes may leave gaps or overlap).
    8-connected components of ``alpha > alpha_threshold`` with at least
    ``min_area`` px go whole to the cell holding most of their pixels (ties:
    the lower cell index); a component outside every cell has no owner.
    Fainter visible pixels join the owner they reach within ``attach_radius``
    8-steps through visible unowned pixels (report v2 prototype order:
    left, right, above, below, diagonals). ``haze`` decides what stays
    unowned after that: ``keep`` gives it to the first cell containing it
    (only pixels outside every cell are dropped), ``drop`` drops it. Every
    frame is placed on one canvas at its cell origin minus one shared
    ``padding``, so the frames keep the sheet's registration, and only the
    owned pixels are copied (RGB elsewhere is zero). Only the first ``count``
    cells (default all) are returned and padded for; the rest may be empty.

    The two policies of D14: frame assembly (B02) keeps haze with
    ``alpha_threshold=16, min_area=4``; spill QC (B03) drops it with
    ``alpha_threshold=127, min_area=64``.

    Returns ``(frames, report)``. The report holds ``mode``, ``method``,
    ``haze``, ``threshold``, ``min_area``, ``attach_radius``,
    ``connectivity``, ``cells``, ``cells_used``, ``canvas`` [w, h],
    ``padding`` [left, top, right, bottom], ``registration``,
    ``frame_origins_in_sheet``, ``attached_px``, ``cell_fallback_px``,
    ``dropped_px``, ``dropped_max_alpha``, ``unused_cells_px``,
    ``empty_cells`` (returned cells without pixels) and ``frames`` (per frame:
    ``box``, ``sheet_origin``, ``owned_components``, ``owned_px`` and
    ``pixels_from_outside_cell``).
    """
    pixels = _rgba_array(rgba)
    height, width = pixels.shape[:2]
    cells = _slice_boxes(width, height, rows, cols, boxes)
    if haze not in HAZE_POLICIES:
        raise ValueError(f"Unknown haze policy {haze!r}; use one of {', '.join(HAZE_POLICIES)}.")
    threshold, min_area, attach_radius = (operator.index(value) for value in (alpha_threshold, min_area, attach_radius))
    if not 0 <= threshold <= 254 or min_area < 0 or attach_radius < 0:
        raise ValueError("ownership_slice needs 0 <= alpha_threshold <= 254, min_area >= 0 and attach_radius >= 0.")
    used = len(cells) if count is None else operator.index(count)
    if not 1 <= used <= len(cells):
        raise ValueError(f"count must be between 1 and {len(cells)}; got {used}.")
    alpha = pixels[..., 3]
    labels, total = label_components(alpha > threshold, 8)
    areas = np.bincount(labels.ravel(), minlength=total + 1)
    counts = np.stack([np.bincount(labels[y0:y1, x0:x1].ravel(), minlength=total + 1) for x0, y0, x1, y1 in cells])
    owner_of = counts.argmax(axis=0).astype(np.int32)
    owner_of[(counts.max(axis=0) == 0) | (areas < min_area)] = -1
    owner_of[0] = -1
    visible = alpha > 0
    owners = owner_of[labels]
    owners, attached = _attach_soft(owners, visible & (owners < 0), attach_radius)
    fallback = 0
    if haze == "keep":
        for index, (x0, y0, x1, y1) in enumerate(cells):
            region = owners[y0:y1, x0:x1]
            unassigned = visible[y0:y1, x0:x1] & (region < 0)
            fallback += int(unassigned.sum())
            region[unassigned] = index
    dropped = visible & (owners < 0)

    ys, xs = np.nonzero(owners >= 0)
    assigned = owners[ys, xs]
    order = np.argsort(assigned, kind="stable")
    ys, xs, assigned = ys[order], xs[order], assigned[order]
    splits = np.searchsorted(assigned, np.arange(len(cells) + 1))
    cell_w = max(x1 - x0 for x0, _y0, x1, _y1 in cells[:used])
    cell_h = max(y1 - y0 for _x0, y0, _x1, y1 in cells[:used])
    extents = [(int(xs[lo:hi].min()) - x0, int(ys[lo:hi].min()) - y0,
                int(xs[lo:hi].max()) + 1 - x0, int(ys[lo:hi].max()) + 1 - y0)
               for (x0, y0, _x1, _y1), lo, hi in zip(cells[:used], splits[:used], splits[1:used + 1]) if lo < hi]
    pad_l = max([0] + [-extent[0] for extent in extents])
    pad_t = max([0] + [-extent[1] for extent in extents])
    pad_r = max([0] + [extent[2] - cell_w for extent in extents])
    pad_b = max([0] + [extent[3] - cell_h for extent in extents])
    canvas = (cell_w + pad_l + pad_r, cell_h + pad_t + pad_b)
    owned_components = np.bincount(owner_of[owner_of >= 0], minlength=len(cells))
    frames, records, empty = [], [], []
    for index, (x0, y0, x1, y1) in enumerate(cells[:used]):
        lo, hi = splits[index], splits[index + 1]
        fy, fx = ys[lo:hi], xs[lo:hi]
        frame = np.zeros((canvas[1], canvas[0], 4), np.uint8)
        frame[fy - y0 + pad_t, fx - x0 + pad_l] = pixels[fy, fx]
        frames.append(frame)
        if lo == hi:
            empty.append(index)
        records.append({"box": [x0, y0, x1, y1], "sheet_origin": [x0 - pad_l, y0 - pad_t],
                        "owned_components": int(owned_components[index]), "owned_px": int(hi - lo),
                        "pixels_from_outside_cell": int((~((fx >= x0) & (fx < x1) & (fy >= y0) & (fy < y1))).sum())})
    report = {
        "mode": "ownership",
        "method": ("8-connected components of alpha > threshold with at least min_area px go whole to the cell "
                   "holding most of their pixels; fainter visible pixels join the owner they reach within "
                   "attach_radius steps; haze keep/drop decides the rest; one shared padding keeps registration"),
        "haze": haze, "threshold": threshold, "min_area": min_area, "attach_radius": attach_radius,
        "connectivity": 8, "cells": len(cells), "cells_used": used,
        "canvas": [int(canvas[0]), int(canvas[1])],
        "padding": [int(pad_l), int(pad_t), int(pad_r), int(pad_b)],
        "registration": "frame pixel (u, v) = sheet pixel (u - padding[0] + cell x0, v - padding[1] + cell y0)",
        "frame_origins_in_sheet": [record["sheet_origin"] for record in records],
        "attached_px": attached, "cell_fallback_px": fallback, "dropped_px": int(dropped.sum()),
        "dropped_max_alpha": int(alpha[dropped].max()) if dropped.any() else 0,
        "unused_cells_px": int(((owners >= used) & visible).sum()),
        "empty_cells": empty,
        "frames": records,
    }
    return frames, report


# --------------------------------------------------------------------------- resampling

_FILTERS = {"box": (Image.Resampling.BOX, 0.5), "lanczos": (Image.Resampling.LANCZOS, 3.0)}


def _filter_for(resampler: str, scale: float) -> tuple[Image.Resampling, float]:
    """Pillow filter and support; reductions of 2x or more always use the box (area) filter."""
    return _FILTERS["box" if resampler == "box" or scale <= 0.5 else resampler]


def _unpremultiply(planes: list[np.ndarray]) -> Image.Image:
    """Back to straight 8-bit RGBA. Colour is divided by the unclipped alpha, so filter
    overshoot (alpha above 1 next to an edge) cannot brighten it."""
    alpha = planes[3]
    alpha8 = np.floor(np.clip(alpha, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    premultiplied = np.clip(np.stack(planes[:3], axis=-1), 0.0, None)
    rgb = np.floor(premultiplied / np.maximum(alpha, 1e-6)[..., None] + 0.5)
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    rgb[alpha8 == 0] = 0
    return Image.fromarray(np.dstack([rgb, alpha8]))


def _resample_nearest(pixels: np.ndarray, scale: float, anchor_src: tuple[float, float] | None,
                      anchor_dst: tuple[float, float], out_size: tuple[int, int]) -> Image.Image:
    upscale = scale >= 1
    factor = integer_scale(scale if upscale else 1.0 / scale)
    height, width = pixels.shape[:2]
    if anchor_src is None:
        if not upscale and (width % factor or height % factor):
            raise ValueError(f"Nearest reduction by {factor} needs dimensions divisible by {factor}; got "
                             f"{width}x{height}. Pass anchor_src to place the logical grid.")
        expected = (width * factor, height * factor) if upscale else (width // factor, height // factor)
        if out_size != expected:
            raise ValueError(f"Nearest resampling of {width}x{height} by {scale:g} gives {expected}, "
                             f"not {out_size}; pass anchors to crop or pad.")
        anchor_src = (0.0, 0.0)

    def sample(count: int, anchor: float, target: float, limit: int) -> tuple[np.ndarray, np.ndarray]:
        """Source index under each output pixel centre, split at the anchor's integer pixel."""
        base = math.floor(anchor)
        centres = np.arange(count) + 0.5 - target
        steps = centres / factor if upscale else centres * factor
        index = base + np.floor(steps + (anchor - base)).astype(np.int64)
        return np.clip(index, 0, limit - 1), (index >= 0) & (index < limit)

    columns, valid_x = sample(out_size[0], anchor_src[0], anchor_dst[0], width)
    rows, valid_y = sample(out_size[1], anchor_src[1], anchor_dst[1], height)
    result = pixels[np.ix_(rows, columns)]
    result[~(valid_y[:, None] & valid_x[None, :])] = 0
    result[result[..., 3] == 0] = 0
    return Image.fromarray(result)


def resample_rgba(img: Any, scale: float, resampler: str = "lanczos", *,
                  anchor_src: Sequence[float] | None = None, anchor_dst: Sequence[float] | None = None,
                  out_size: Sequence[int] | None = None) -> Image.Image:
    """Resample straight-alpha RGBA on premultiplied float channels (S05, S06).

    Every channel is premultiplied and resized as its own float image, so
    Pillow never premultiplies a second time; premultiplied input modes
    (``RGBa``, ``La``) are refused. ``box`` averages areas; ``lanczos`` also
    switches to the box filter for reductions of 2x or more; ``nearest``
    accepts only integer scales ``N`` or ``1/N`` (reductions sample the centre
    of each logical pixel) and raises ValueError otherwise.

    Without anchors the whole image is mapped onto ``out_size`` (default
    ``round(size * scale)``), with Pillow's edge handling for opaque art. With
    ``anchor_src`` the grid is pinned: source point ``anchor_src`` lands
    exactly on ``anchor_dst`` (default ``anchor_src * scale``), the scale is
    exact, and everything outside the source is transparent. A pinned result
    depends only on the pixels around the anchor: the same frame cropped or
    placed differently (image and ``anchor_src`` shifted by whole pixels, same
    ``anchor_dst``) gives byte-identical output, so a rest pose shared by two
    clips stays identical.

    Caveat (D18): ``lanczos`` rings just outside an edge, and dividing that
    tiny premultiplied ringing by an alpha of 1-4 (out of 255) invents
    saturated colours there, often key-leaning ones such as (255, 222, 247).
    They are invisible (under 2% opacity) but fool colour-based QA: B06's
    registered frames carried 1.2-5.9% outer-ring "spill" from them, and a
    clean frame resampled to 0.9x showed 6.5% (``box`` showed none). Run
    ``alpha_hygiene(result, mode="floor")`` (floor 4) on resampled frames
    that will be published or gated, or measure colour QA on a copy with
    alpha <= ALPHA_GEOMETRY_THRESHOLD zeroed, as the residue gate does.
    """
    if resampler not in RESAMPLERS:
        raise ValueError(f"Unknown resampler {resampler!r}; use one of {', '.join(RESAMPLERS)}.")
    scale = float(scale)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError(f"Scale must be positive and finite; got {scale!r}.")
    if anchor_dst is not None and anchor_src is None:
        raise ValueError("anchor_dst needs anchor_src.")
    pixels = _rgba_array(img)
    height, width = pixels.shape[:2]
    if out_size is None:
        size = (max(1, _round_half_up(width * scale)), max(1, _round_half_up(height * scale)))
    else:
        size = (int(out_size[0]), int(out_size[1]))
        if min(size) < 1:
            raise ValueError(f"out_size must be positive; got {tuple(out_size)}.")
    source_anchor = None if anchor_src is None else (float(anchor_src[0]), float(anchor_src[1]))
    target_anchor = (0.0, 0.0) if source_anchor is None else (
        (source_anchor[0] * scale, source_anchor[1] * scale) if anchor_dst is None
        else (float(anchor_dst[0]), float(anchor_dst[1])))
    if resampler == "nearest":
        return _resample_nearest(pixels, scale, source_anchor, target_anchor, size)

    alpha = pixels[..., 3].astype(np.float32) / 255.0
    if source_anchor is None:
        resample_filter, _support = _filter_for(resampler, max(size[0] / width, size[1] / height))
        planes = [pixels[..., channel].astype(np.float32) * alpha for channel in range(3)] + [alpha]
        resized = [np.asarray(Image.fromarray(plane).resize(size, resample_filter)) for plane in planes]
        return _unpremultiply(resized)

    resample_filter, support = _filter_for(resampler, scale)
    # The canvas covers every filter window of every output pixel, so Pillow never clips a window
    # (clipping renormalises weights and would make the result depend on the crop).
    pad = math.ceil(support * max(1.0, 1.0 / scale)) + 2
    # Work relative to the anchor's integer pixel so integer shifts of the input give identical floats.
    base_x, base_y = math.floor(source_anchor[0]), math.floor(source_anchor[1])
    left = source_anchor[0] - base_x - target_anchor[0] / scale
    top = source_anchor[1] - base_y - target_anchor[1] / scale
    span_x, span_y = size[0] / scale, size[1] / scale
    col0, row0 = math.floor(left) - pad, math.floor(top) - pad
    col1, row1 = math.ceil(left + span_x) + pad, math.ceil(top + span_y) + pad
    canvas = np.zeros((row1 - row0, col1 - col0, 4), np.float32)
    src_x0, src_x1 = max(0, base_x + col0), min(width, base_x + col1)
    src_y0, src_y1 = max(0, base_y + row0), min(height, base_y + row1)
    if src_x0 < src_x1 and src_y0 < src_y1:
        region = pixels[src_y0:src_y1, src_x0:src_x1].astype(np.float32)
        region_alpha = region[..., 3:] / 255.0
        cy, cx = src_y0 - (base_y + row0), src_x0 - (base_x + col0)
        canvas[cy:cy + region.shape[0], cx:cx + region.shape[1], :3] = region[..., :3] * region_alpha
        canvas[cy:cy + region.shape[0], cx:cx + region.shape[1], 3] = region_alpha[..., 0]
    box = (left - col0, top - row0, left - col0 + span_x, top - row0 + span_y)
    resized = [np.asarray(Image.fromarray(np.ascontiguousarray(canvas[..., channel])).resize(
        size, resample_filter, box=box)) for channel in range(4)]
    return _unpremultiply(resized)


# --------------------------------------------------------------------------- timing and seams

_SEAM_EPSILON = 1e-6


def frame_durations(total_ms: int, n: int) -> list[int]:
    """Split ``total_ms`` into ``n`` integer frame durations that sum exactly.

    Frame ``i`` ends at ``round_half_up((i + 1) * total_ms / n)``, so the
    playhead never drifts more than 0.5 ms and durations differ by at most
    1 ms. Each frame needs at least 1 ms.
    """
    total_ms, n = operator.index(total_ms), operator.index(n)
    if n < 1:
        raise ValueError("Frame count must be at least 1.")
    if total_ms < n:
        raise ValueError(f"{total_ms} ms cannot hold {n} frames of at least 1 ms.")
    edges = [(2 * index * total_ms + n) // (2 * n) for index in range(n + 1)]
    return [end - start for start, end in zip(edges, edges[1:])]


def rational_fps(n: int, total_ms: int) -> str:
    """Exact frame rate of ``n`` frames in ``total_ms`` as a reduced ``"num/den"`` (ffmpeg syntax)."""
    n, total_ms = operator.index(n), operator.index(total_ms)
    if n < 1 or total_ms < 1:
        raise ValueError("Frame count and duration must be positive.")
    rate = Fraction(1000 * n, total_ms)
    return f"{rate.numerator}/{rate.denominator}"


def _premultiplied(frame: Any) -> np.ndarray:
    pixels = _rgba_array(frame).astype(np.float64)
    pixels[..., :3] *= pixels[..., 3:] / 255.0
    return pixels


def _weights(mask: Any, shape: tuple[int, int]) -> np.ndarray | None:
    if mask is None:
        return None
    plane = _alpha_plane(mask)
    weights = plane.astype(np.float64) if plane.dtype == bool else plane.astype(np.float64) / 255.0
    if weights.shape != shape:
        raise ValueError(f"Mask shape {weights.shape} does not match frames {shape}.")
    if weights.sum() <= 0:
        raise ValueError("Mask selects no pixels.")
    return weights


def _mean_difference(a: np.ndarray, b: np.ndarray, weights: np.ndarray | None) -> float:
    if a.shape != b.shape:
        raise ValueError(f"Frames differ in size: {a.shape[:2]} vs {b.shape[:2]}.")
    per_pixel = np.abs(a - b).mean(axis=-1)
    if weights is None:
        return float(per_pixel.mean())
    return float((per_pixel * weights).sum() / weights.sum())


def transition_mae(a: Any, b: Any, *, mask: Any = None) -> float:
    """Mean absolute difference of premultiplied RGBA, in 0-255 units.

    RGB hidden under alpha 0 does not count. ``mask`` (boolean, or 0-255
    weights) restricts the measure to a region, such as a motion mask.
    """
    first, second = _premultiplied(a), _premultiplied(b)
    return _mean_difference(first, second, _weights(mask, first.shape[:2]))


def seam_report(frames: Iterable[Any], *, mask: Any = None) -> dict[str, Any]:
    """Loop seam normalised by the clip's own motion (MAP-14, hd2d seam findings).

    ``seam`` is transition_mae(last, first); the adjacent steps are the
    transitions between neighbours. ``seam_over_median`` and ``seam_over_p95``
    near 1 mean a seamless loop, well above 1 a pop at the wrap, near 0 a held
    duplicate frame. Denominators are floored at 1e-6. Measure the decoded
    runtime file, inside the motion mask, for video loops.
    """
    iterator = iter(frames)
    try:
        first = previous = _premultiplied(next(iterator))
    except StopIteration:
        raise ValueError("seam_report needs at least two frames.") from None
    weights = _weights(mask, first.shape[:2])
    steps = []
    for frame in iterator:
        current = _premultiplied(frame)
        steps.append(_mean_difference(previous, current, weights))
        previous = current
    if not steps:
        raise ValueError("seam_report needs at least two frames.")
    seam = _mean_difference(previous, first, weights)
    adjacent = np.asarray(steps)
    median, p95 = float(np.median(adjacent)), float(np.percentile(adjacent, 95))
    return {
        "frames": len(steps) + 1,
        "seam": seam,
        "adjacent_median": median,
        "adjacent_p95": p95,
        "adjacent_max": float(adjacent.max()),
        "seam_over_median": seam / max(median, _SEAM_EPSILON),
        "seam_over_p95": seam / max(p95, _SEAM_EPSILON),
        "method": "premultiplied RGBA mean absolute difference (0-255); seam = last -> first",
    }


# B11's calibration (extract_platform_strip.py / extract_terrain_tiles.py, section 8 of its handoff).
_EDGE_LOCAL_STEPS = 8      # interior steps per side that describe the art around a join
_EDGE_WINDOW_ROWS = 4      # a partial seam must show along this many consecutive rows
_EDGE_NEAR_STEPS = 3       # steps per side that define a join's immediate neighbourhood
_EDGE_SEAM_FLOOR = 1.0     # joins below this step are continuous whatever the ratio
_EDGE_DUPLICATE_RATIO = 0.25  # a join step below this share of the neighbourhood duplicates an edge
_EDGE_DUPLICATE_FLOOR = 2.0   # flatter neighbourhoods cannot show a duplicated edge
_EDGE_FLAT_STEP = 1e-6     # a join and neighbourhood this still are flat: nothing to compare


def _window_max(values: np.ndarray, window: int) -> float:
    """Largest mean over ``window`` consecutive rows of any column of ``values`` (rows x columns)."""
    if values.size == 0:
        return 0.0
    window = min(window, values.shape[0])
    sums = np.cumsum(np.pad(values, ((1, 0), (0, 0))), axis=0)
    return float(((sums[window:] - sums[:-window]) / window).max())


def edge_seam_report(left: Any, right: Any, *, left_start: int = 0, right_stop: int | None = None,
                     gate: float | None = None) -> dict[str, Any]:
    """Normalised seam of the join ``left[:, -1] | right[:, 0]`` of two images (MAP-14; D9).

    The spatial twin of seam_report, ported exactly from B11's
    ``_local_edge_seam_report``. ``seam`` is the premultiplied RGBA step
    (0-255) across the join; ``adjacent_*`` describe the interior column steps
    within 8 columns of it on both sides, never using columns of ``left``
    before ``left_start`` or of ``right`` from ``right_stop`` on (outer cap
    padding). ``seam_ratio`` is the larger of seam / adjacent_max and the
    worst 4-row window of the join over the worst 4-row window of those
    steps: about 1 or less looks like the art, well above 1 is a seam. Test a
    vertical join (top/bottom) by passing the images transposed
    (``np.swapaxes(image, 0, 1)``); a wrap is ``edge_seam_report(image, image)``.

    ``verdict`` (EDGE_SEAM_VERDICTS, D9): ``too_small`` when neither side
    has an interior step to compare with (each side one column wide; the
    ratios are then 0); ``flat`` when the join and every interior step are at
    most 1e-6 (a flat colour: nothing to judge, and nothing wrong);
    ``duplicate_edge`` when the join is much flatter than its immediate
    neighbourhood (near_median >= 2 and seam < 0.25 * near_median: the edge
    column repeats, a stutter); ``seam`` when seam > 1 and seam_ratio exceeds
    ``gate`` (default EDGE_SEAM_NOMINAL_RATIO, 1.25); else ``continuous``.
    B11's three verdicts are unchanged wherever its metric applies; gates
    should fail EDGE_SEAM_DEFECTS only. Images are 8-bit RGBA or RGB arrays
    or Pillow images with the same height. The result is common seamReport
    plus ``seam_ratio``, ``near_median``, ``verdict`` and ``method``; numbers
    are rounded to 6 decimals.
    """
    left_pixels, right_pixels = _rgba_array(left), _rgba_array(right)
    if left_pixels.shape[0] != right_pixels.shape[0] or left_pixels.shape[0] == 0:
        raise ValueError(f"A join needs two images of the same non-zero height; got {left_pixels.shape[0]} and "
                         f"{right_pixels.shape[0]} rows.")
    if gate is not None and not (math.isfinite(gate) and gate >= 0):
        raise ValueError(f"gate must be a finite number >= 0; got {gate!r}.")
    left_part = left_pixels[:, operator.index(left_start):][:, -(_EDGE_LOCAL_STEPS + 1):]
    right_part = right_pixels[:, :right_stop][:, :_EDGE_LOCAL_STEPS + 1]
    if left_part.shape[1] == 0 or right_part.shape[1] == 0:
        raise ValueError("left_start and right_stop leave no column on one side of the join.")
    a = _premultiplied(left_part)  # only the columns near the join
    b = _premultiplied(right_part)
    join_rows = np.abs(a[:, -1] - b[:, 0]).mean(axis=1)
    left_steps = np.abs(np.diff(a, axis=1)).mean(axis=2)
    right_steps = np.abs(np.diff(b, axis=1)).mean(axis=2)
    local = np.concatenate([left_steps, right_steps], axis=1)
    means = local.mean(axis=0) if local.size else np.zeros(1)
    near = np.concatenate([left_steps[:, -_EDGE_NEAR_STEPS:].mean(axis=0) if left_steps.size else np.zeros(0),
                           right_steps[:, :_EDGE_NEAR_STEPS].mean(axis=0) if right_steps.size else np.zeros(0)])
    seam = float(join_rows.mean())
    median, p95, peak = float(np.median(means)), float(np.percentile(means, 95)), float(means.max())
    near_median = float(np.median(near)) if near.size else 0.0
    ratio = max(seam / max(peak, _SEAM_EPSILON),
                _window_max(join_rows[:, None], _EDGE_WINDOW_ROWS)
                / max(_window_max(local, _EDGE_WINDOW_ROWS), _SEAM_EPSILON))
    over_median, over_p95 = seam / max(median, _SEAM_EPSILON), seam / max(p95, _SEAM_EPSILON)
    if not local.size:
        verdict, ratio, over_median, over_p95 = "too_small", 0.0, 0.0, 0.0
    elif seam <= _EDGE_FLAT_STEP and peak <= _EDGE_FLAT_STEP:
        verdict = "flat"
    elif near_median >= _EDGE_DUPLICATE_FLOOR and seam < _EDGE_DUPLICATE_RATIO * near_median:
        verdict = "duplicate_edge"
    elif seam > _EDGE_SEAM_FLOOR and ratio > (gate if gate is not None else EDGE_SEAM_NOMINAL_RATIO):
        verdict = "seam"
    else:
        verdict = "continuous"
    return {
        "seam": round(seam, 6), "adjacent_median": round(median, 6), "adjacent_p95": round(p95, 6),
        "adjacent_max": round(peak, 6), "seam_over_median": round(over_median, 6),
        "seam_over_p95": round(over_p95, 6), "seam_ratio": round(ratio, 6),
        "near_median": round(near_median, 6), "verdict": verdict,
        "method": (f"premultiplied RGBA column steps (0-255); join vs interior steps within {_EDGE_LOCAL_STEPS} "
                   f"columns per side, whole edge and worst {_EDGE_WINDOW_ROWS}-row window"),
    }
