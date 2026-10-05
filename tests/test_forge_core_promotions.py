"""Tests for the forge_core 1.1 promotions of the Phase 3 integration (D30, with D9, D14, D26-D29).

Each promoted helper is checked against verbatim copies of the private
helpers it replaces (the ``_oracle_*`` functions below, copied from the Wave B
modules as merged on asf/integration), so the equivalence keeps its meaning
after the modules swap their copies for forge_core. Where the copies
differed, the test names the choice. All inputs are synthetic and seeded,
except the fox fixture (tests/fixtures/real/PROVENANCE.json).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath

import numpy as np
import pytest
from PIL import Image, ImageFilter, ImageOps

from forge_testutils import (REPO_ROOT, assert_valid_contract, load_shared, make_magenta_sheet, real_fixture)

fc = load_shared("forge_core")
EPSILON = 1e-6


# --------------------------------------------------------------------------- API surface

def test_api_version_and_package_version():
    """D29, D30: API 1.1 is additive; the package version is the integrated release."""
    assert fc.FORGE_CORE_API_VERSION == "1.1"
    assert fc.FORGE_PACKAGE_VERSION == "0.4.0"
    assert fc.EDGE_SEAM_VERDICTS == ("continuous", "seam", "duplicate_edge", "flat", "too_small")
    assert fc.EDGE_SEAM_DEFECTS == ("seam", "duplicate_edge") and fc.EDGE_SEAM_NOMINAL_RATIO == 1.25
    assert fc.HAZE_POLICIES == ("keep", "drop")
    assert fc.ASPECT_POLICIES == ("expand", "fixed-height", "fixed-width")


def test_new_signatures_keep_extras_keyword_only():
    import inspect
    expected = {
        "run_cli": (["main", "argv"], ["expected"]),
        "utc_timestamp": (["moment"], []),
        "manifest_path": (["path", "base"], []),
        "file_ref": (["path", "base"], ["sha256", "size"]),
        "parse_json": (["data"], ["strict"]),
        "read_json": (["path"], ["strict"]),
        "round_half_up": (["value"], []),
        "parse_aspect": (["text"], []),
        "aspect_viewport": (["viewport", "aspect", "policy"], []),
        "cover_window": (["size", "aspect", "zoom", "focus"], []),
        "dilate_square": (["mask", "radius"], []),
        "distance_to": (["mask", "cap"], []),
        "merge_rects": (["mask"], []),
        "ownership_slice": (["rgba", "rows", "cols"],
                            ["boxes", "alpha_threshold", "min_area", "haze", "attach_radius", "count"]),
        "edge_seam_report": (["left", "right"], ["left_start", "right_stop", "gate"]),
    }
    for name, (positional, keyword_only) in expected.items():
        parameters = inspect.signature(getattr(fc, name)).parameters.values()
        assert [p.name for p in parameters if p.kind is p.POSITIONAL_OR_KEYWORD] == positional, name
        assert [p.name for p in parameters if p.kind is p.KEYWORD_ONLY] == keyword_only, name


# --------------------------------------------------------------------------- run_cli (D26, D27)

def _main_returning(value):
    return lambda argv=None: value


def test_run_cli_statuses_and_messages(capsys, monkeypatch):
    monkeypatch.delenv("FORGE_DEBUG", raising=False)
    assert fc.run_cli(_main_returning(None)) == 0
    assert fc.run_cli(_main_returning(3)) == 3
    seen = []
    assert fc.run_cli(lambda argv=None: seen.append(argv), ["--x", "1"]) == 0 and seen == [["--x", "1"]]

    def broken_input(argv=None):
        raise ValueError("--rows must be positive \u2192 got 0")

    assert fc.run_cli(broken_input) == 1
    assert capsys.readouterr().err == "error: --rows must be positive -> got 0\n"

    def missing(argv=None):
        raise FileNotFoundError(2, "No such file or directory", "sheet.png")

    assert fc.run_cli(missing) == 1
    assert capsys.readouterr().err.startswith("error: [Errno 2] No such file or directory")

    def bomb(argv=None):
        raise Image.DecompressionBombError("Image size (300000000 pixels) exceeds limit")

    assert fc.run_cli(bomb) == 1
    assert capsys.readouterr().err == "error: Image size (300000000 pixels) exceeds limit\n"

    def bug(argv=None):
        return {}["frames"]

    assert fc.run_cli(bug) == 1
    assert capsys.readouterr().err == "error: internal error (KeyError: 'frames')\n"  # D27's exact form

    class ToolError(Exception):
        pass

    def tool_error(argv=None):
        raise ToolError("palette has 300 colours")

    assert fc.run_cli(tool_error) == 1
    assert "internal error (ToolError" in capsys.readouterr().err
    assert fc.run_cli(tool_error, expected=(ToolError, ValueError, OSError)) == 1
    assert capsys.readouterr().err == "error: palette has 300 colours\n"

    def interrupted(argv=None):
        raise KeyboardInterrupt

    assert fc.run_cli(interrupted) == 130
    assert capsys.readouterr().err == "error: interrupted\n"


def test_run_cli_lets_argparse_and_explicit_exits_through(capsys):
    """D26: usage errors keep argparse's exit 2 and 'usage: ... error: ...'; --help exits 0."""
    def main(argv=None):
        parser = argparse.ArgumentParser(prog="tool")
        parser.add_argument("--rows", type=int, required=True)
        parser.parse_args(argv)
        return 0

    with pytest.raises(SystemExit) as usage:
        fc.run_cli(main, [])
    assert usage.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("usage: tool") and "error: the following arguments are required: --rows" in err
    with pytest.raises(SystemExit) as helped:
        fc.run_cli(main, ["--help"])
    assert helped.value.code == 0
    with pytest.raises(SystemExit) as explicit:
        fc.run_cli(lambda argv=None: sys.exit(4))
    assert explicit.value.code == 4


def test_run_cli_debug_reraises(monkeypatch):
    monkeypatch.setenv("FORGE_DEBUG", "1")
    with pytest.raises(ZeroDivisionError):
        fc.run_cli(lambda argv=None: 1 / 0)
    with pytest.raises(ValueError):
        fc.run_cli(lambda argv=None: int("x"))


def test_run_cli_in_a_real_process_prints_one_ascii_line_and_no_traceback(tmp_path):
    """D27 end to end: a CLI built on run_cli never shows a traceback, whatever fails."""
    script = tmp_path / "tool.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'shared')!r})\n"
        "import forge_core\n"
        "def main(argv=None):\n"
        "    mode = sys.argv[1]\n"
        "    if mode == 'bug':\n"
        "        return [][1]\n"
        "    if mode == 'input':\n"
        "        raise ValueError('bad \\u00d7 value')\n"
        "    return 0\n"
        "raise SystemExit(forge_core.run_cli(main))\n", encoding="utf-8")
    environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    environment.pop("FORGE_DEBUG", None)
    for mode, status, message in (("ok", 0, ""), ("input", 1, "error: bad x value\n"),
                                  ("bug", 1, "error: internal error (IndexError: list index out of range)\n")):
        done = subprocess.run([sys.executable, str(script), mode], capture_output=True, env=environment)
        assert done.returncode == status, (mode, done.stderr)
        assert done.stderr.decode("ascii").replace("\r\n", "\n") == message
        assert b"Traceback" not in done.stderr


# --------------------------------------------------------------------------- utc_timestamp (A4 twin)

def _oracle_media_ledger_utc_timestamp(moment=None):
    """media_ledger.utc_timestamp (A4), verbatim."""
    moment = moment or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def test_utc_timestamp_matches_media_ledger_and_the_common_timestamp():
    moment = datetime(2026, 10, 5, 4, 16, 0, 987654, tzinfo=timezone.utc)
    assert fc.utc_timestamp(moment) == _oracle_media_ledger_utc_timestamp(moment) == "2026-10-05T04:16:00.987Z"
    taipei = moment.astimezone(timezone(timedelta(hours=8)))
    assert fc.utc_timestamp(taipei) == "2026-10-05T04:16:00.987Z"  # converted, not mislabelled as Z
    assert fc.utc_timestamp(moment.replace(tzinfo=None)) == "2026-10-05T04:16:00.987Z"  # naive means UTC
    now = fc.utc_timestamp()
    assert_valid_contract(now, "common", "timestamp")
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60


# --------------------------------------------------------------------------- manifest_path and file_ref

_REL_PATH_B13 = re.compile(r"^(?!/)(?![A-Za-z][A-Za-z0-9+.-]*:)[^\\]+$")


def _oracle_b11(relative, path):  # extract_platform_strip / extract_terrain_tiles / forge_palette
    return Path(path).name if relative.startswith("/") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", relative) \
        else relative


def _oracle_b12(relative, path):  # compose, conform, export_godot, validate_*, build_scene_preview, export_engine
    return Path(path).name if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative) else relative


def _oracle_b13(relative, path):  # map_bundle._local_file_ref
    return relative if _REL_PATH_B13.match(relative) else Path(path).name


def _oracle_b02(relative, path):  # assemble_frames.manifest_path
    return Path(path).name if PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).drive else relative


def _oracle_b07(relative, path):  # gait_loop._relative_or_name
    return Path(path).resolve().name if relative.startswith("/") or (len(relative) > 1 and relative[1] == ":") \
        else relative


ORACLES = (_oracle_b11, _oracle_b12, _oracle_b13, _oracle_b02, _oracle_b07)


@pytest.mark.parametrize("answer", [
    "frames/f0.png", "../input/sheet.png", "x.png", ".", "../../a b/c.png", "sub/name:with-colon.png",
    "C:/Users/me/sheet.png", "z:/x.png", "//server/share/x.png", "/home/me/x.png",
])
def test_manifest_path_unifies_the_four_regexes(tmp_path, monkeypatch, answer):
    """map-core review: four regexes decided 'no relative route'; one relPath rule now does.

    For every answer portable_path really gives (a relative path, or an absolute POSIX path,
    drive or UNC, when there is no relative route) all private copies agree with manifest_path.
    """
    target = tmp_path / "x.png"
    target.write_bytes(b"png")
    monkeypatch.setattr(fc, "portable_path", lambda path, base: answer)
    result = fc.manifest_path(target, tmp_path)
    assert _REL_PATH_B13.match(result)
    for oracle in ORACLES:
        assert oracle(answer, target) == result, oracle.__name__


def test_manifest_path_documents_where_the_copies_differed(tmp_path, monkeypatch):
    """Answers no Windows or POSIX portable_path gives, where the copies split: the relPath rule wins."""
    target = tmp_path / "x.png"
    target.write_bytes(b"png")
    monkeypatch.setattr(fc, "portable_path", lambda path, base: "http:/x.png")  # scheme-like: B12/B02 kept it
    assert fc.manifest_path(target, tmp_path) == "x.png" == _oracle_b13("http:/x.png", target)
    assert _oracle_b12("http:/x.png", target) == "http:/x.png"  # an invalid relPath, now refused
    monkeypatch.setattr(fc, "portable_path", lambda path, base: "a\\b.png")  # a backslash (POSIX file name)
    assert fc.manifest_path(target, tmp_path) == "x.png" == _oracle_b13("a\\b.png", target)


def test_manifest_path_real_paths_and_unrepresentable_names(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    (tmp_path / "in").mkdir()
    source = tmp_path / "in" / "sheet.png"
    source.write_bytes(b"png")
    assert fc.manifest_path(source, tmp_path / "out") == "../in/sheet.png"
    assert fc.manifest_path(tmp_path / "out", tmp_path / "out") == "."
    monkeypatch.setattr(fc, "portable_path", lambda path, base: "Z:/elsewhere/dir")
    monkeypatch.chdir(tmp_path / "in")
    assert fc.manifest_path(".", tmp_path) == "in"  # no name of its own: the resolved folder's name
    assert fc.manifest_path("..", tmp_path) == tmp_path.name
    with pytest.raises(ValueError, match="cannot be stored"):
        fc.manifest_path(f"{tmp_path.as_posix()}/in/a:b", tmp_path)  # a POSIX-only name no relPath can hold


def test_manifest_path_across_real_drives(tmp_path):
    """On Windows the temp folder and the repository usually sit on different drives."""
    if tmp_path.drive.lower() == REPO_ROOT.drive.lower() or not tmp_path.drive:
        pytest.skip("needs two drives (Windows): the temp folder is on the repository's drive")
    source = tmp_path / "sheet.png"
    source.write_bytes(b"png")
    assert fc.portable_path(source, REPO_ROOT).startswith(tmp_path.drive[0])  # absolute: no relative route
    assert fc.manifest_path(source, REPO_ROOT) == "sheet.png"
    ref = fc.file_ref(source, REPO_ROOT)
    assert ref == {"path": "sheet.png", "sha256": hashlib.sha256(b"png").hexdigest(), "bytes": 3}


def test_file_ref_shape_passthrough_and_validation(tmp_path):
    data = b"\x89PNG fake"
    source = tmp_path / "a.png"
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    ref = fc.file_ref(source, tmp_path)
    assert list(ref) == ["path", "sha256", "bytes"] and ref == {"path": "a.png", "sha256": digest, "bytes": 9}
    assert_valid_contract(ref, "common", "fileRef")
    assert fc.file_ref(source, tmp_path, sha256=digest.upper(), size=9) == ref  # values the caller holds
    assert fc.file_ref(source, tmp_path, sha256="", size=None) == ref  # an empty digest means "hash it"
    with pytest.raises(ValueError, match="64 hexadecimal"):
        fc.file_ref(source, tmp_path, sha256="abc")
    with pytest.raises(ValueError, match="size"):
        fc.file_ref(source, tmp_path, size=-1)
    with pytest.raises(TypeError):
        fc.file_ref(source, tmp_path, size=9.0)


# --------------------------------------------------------------------------- read_json / parse_json (D28)

class _BundleError(ValueError):
    pass


def _oracle_map_bundle_read_json(path):
    """map_bundle._local_read_json (B13), verbatim apart from the error class."""
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def no_constants(name):
        raise ValueError(f"{name} is not valid JSON")

    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=no_constants)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise _BundleError(f"cannot read JSON {Path(path).name}: {error}") from error


def _wrapped(path):
    """What map_bundle does after the swap: forge_core.read_json(strict=True) plus its own wrapper."""
    try:
        return fc.read_json(path, strict=True)
    except (OSError, ValueError) as error:
        raise _BundleError(f"cannot read JSON {Path(path).name}: {error}") from error


@pytest.mark.parametrize("text", [
    '{"a": 1, "b": [1.5, "x", null, true]}', '\ufeff{"bom": "PowerShell 5.1"}', '[]', '"\u00e9t\u00e9"',
    '{"a": 1, "a": 2}', '{"x": NaN}', '{"x": -Infinity}', '{"a": }', '', '{"a": 1} trailing',
])
def test_strict_read_json_matches_map_bundle(tmp_path, text):
    """D28 and B13's request: strict reading gives map_bundle's values and messages."""
    path = tmp_path / "doc.json"
    path.write_bytes(text.encode("utf-8"))
    try:
        expected = _oracle_map_bundle_read_json(path)
    except _BundleError as error:
        with pytest.raises(_BundleError) as raised:
            _wrapped(path)
        assert str(raised.value) == str(error)
    else:
        assert _wrapped(path) == expected


def test_read_json_lenient_is_stdlib_plus_bom_and_strict_refuses_more(tmp_path):
    path = tmp_path / "profile.json"
    path.write_bytes(b'\xef\xbb\xbf{"scale": 2.5, "nan": NaN, "k": 1, "k": 2}')
    lenient = fc.read_json(path)
    assert lenient["scale"] == 2.5 and math.isnan(lenient["nan"]) and lenient["k"] == 2  # json.loads semantics
    with pytest.raises(ValueError, match="NaN is not valid JSON"):
        fc.read_json(path, strict=True)
    assert fc.parse_json(b'{"big": 1e999}')["big"] == math.inf
    with pytest.raises(ValueError, match="1e999 is not a finite JSON number"):
        fc.parse_json(b'{"big": 1e999}', strict=True)  # an overflowing literal is Infinity in disguise
    assert fc.parse_json('\ufeff[1, 2]') == [1, 2] == fc.parse_json(bytearray(b"[1, 2]"))
    assert fc.parse_json(b'{"n": 12345678901234567890}', strict=True)["n"] == 12345678901234567890
    with pytest.raises(UnicodeDecodeError):  # UTF-16 is not D28's encoding; still a ValueError
        fc.parse_json('{"a": 1}'.encode("utf-16"))
    with pytest.raises(TypeError):
        fc.parse_json(Path("x.json"))
    with pytest.raises(FileNotFoundError):
        fc.read_json(tmp_path / "missing.json")


# --------------------------------------------------------------------------- round_half_up

def _oracle_float(value):  # forge_core 1.0, compose, export_engine, scale_frames, register_clip, ...
    return int(math.floor(value + 0.5))


def _oracle_fraction(value):  # gait_loop.round_half_up / engine_export._local_round_half_up
    if isinstance(value, Fraction):
        return int(math.floor(value + Fraction(1, 2)))
    return int(math.floor(value + 0.5))


def test_round_half_up_matches_every_private_copy():
    rng = np.random.default_rng(20261005)
    floats = np.concatenate([rng.uniform(-1e4, 1e4, 4000), np.arange(-50, 50) + 0.5, rng.integers(-99, 99, 200)
                             + rng.choice([0.4999999, 0.5, 0.5000001], 200)])
    for value in floats.tolist() + [np.float32(2.5), np.float64(-2.5), 0.49999999999999994]:
        assert fc.round_half_up(value) == _oracle_float(value) == _oracle_fraction(value), value
    for numerator, denominator in rng.integers(-10_000, 10_000, (2000, 2)).tolist():
        if denominator:
            value = Fraction(numerator, denominator)
            assert fc.round_half_up(value) == _oracle_fraction(value), value
    assert [fc.round_half_up(v) for v in (2.5, -2.5, 3.5, -3.5, 0.5, -0.5)] == [3, -2, 4, -3, 1, 0]


def test_round_half_up_is_exact_for_integers_fractions_and_decimals():
    big = 2 ** 60 + 1
    assert fc.round_half_up(big) == big and _oracle_float(big) != big  # the float path loses the last bit
    assert fc.round_half_up(np.int64(-7)) == -7 and isinstance(fc.round_half_up(np.int64(-7)), int)
    assert fc.round_half_up(Fraction(1001, 2)) == 501 and fc.round_half_up(Fraction(-1001, 2)) == -500
    assert fc.round_half_up(Fraction(10 ** 30 + 1, 2)) == (10 ** 30 + 2) // 2
    assert fc.round_half_up(Decimal("2.5")) == 3 and fc.round_half_up(Decimal("-2.5")) == -2
    with pytest.raises(ValueError):
        fc.round_half_up(float("nan"))
    with pytest.raises(OverflowError):
        fc.round_half_up(float("inf"))


# --------------------------------------------------------------------------- screen aspect helpers (B12)

def _oracle_parse_aspect_parallax(text):
    """validate_parallax._local_parse_aspect, verbatim."""
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*(?:[:/]\s*([0-9]*\.?[0-9]+))?\s*", str(text))
    if not match:
        raise ValueError(f"aspect {text!r} must look like 16:9 or 1.7778.")
    width, height = float(match.group(1)), float(match.group(2) or 1)
    if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
        raise ValueError(f"aspect {text!r} must be a positive ratio.")
    return width, height


def _oracle_parse_aspect_conform(text):
    """conform_background._local_parse_aspect, verbatim (no finiteness check)."""
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*(?:[:/]\s*([0-9]*\.?[0-9]+))?\s*", str(text))
    if not match:
        raise ValueError(f"aspect {text!r} must look like 16:9 or 1.7778.")
    width, height = float(match.group(1)), float(match.group(2) or 1)
    if width <= 0 or height <= 0:
        raise ValueError(f"aspect {text!r} must be a positive ratio.")
    return width, height


def _oracle_aspect_viewport(viewport, aspect, policy):
    """validate_parallax._local_aspect_viewport, verbatim."""
    width, height = viewport
    share_w, share_h = aspect
    if policy == "fixed-height" or (policy == "expand" and share_w * height >= width * share_h):
        return [height * share_w / share_h, height]
    return [width, width * share_h / share_w]


def _oracle_cover_window(size, aspect, zoom=1.0, focus=(0.5, 0.5)):
    """conform_background._local_cover_window, verbatim."""
    width, height = size
    share_w, share_h = aspect
    if share_w * height >= width * share_h:
        window_w, window_h = float(width), width * share_h / share_w
    else:
        window_w, window_h = height * share_w / share_h, float(height)
    window_w, window_h = window_w / zoom, window_h / zoom
    left, top = (width - window_w) * focus[0], (height - window_h) * focus[1]
    return left, top, left + window_w, top + window_h


@pytest.mark.parametrize("text", ["16:9", "19.5:9", "16/9", " 4 : 3 ", "1.7778", ".5", "21:9", "9:19.5",
                                  "0:9", "16:0", "abc", "16:9:1", "-4:3", "", "1e3", "9" * 400])
def test_parse_aspect_matches_both_copies(text):
    """B12 request: shares, not a ratio. The copies differ only on a share too large for a float."""
    def outcome(function):
        try:
            return function(text)
        except ValueError as error:
            return str(error)
    assert outcome(fc.parse_aspect) == outcome(_oracle_parse_aspect_parallax)
    if text != "9" * 400:
        assert outcome(fc.parse_aspect) == outcome(_oracle_parse_aspect_conform)
    else:  # conform_background accepted an infinite share; the validate_parallax rule is kept
        assert _oracle_parse_aspect_conform(text)[0] == math.inf
    if text == "19.5:9":
        share_w, share_h = fc.parse_aspect(text)
        assert 540 * share_w / share_h == 1170.0


def test_aspect_viewport_and_cover_window_match_their_copies():
    rng = np.random.default_rng(7)
    for _ in range(3000):
        viewport = [float(rng.uniform(64, 4096)), float(rng.uniform(64, 4096))]
        aspect = (float(rng.choice([4, 16, 19.5, 21, 9, 1])), float(rng.choice([3, 9, 19.5, 1])))
        policy = str(rng.choice(fc.ASPECT_POLICIES))
        assert fc.aspect_viewport(viewport, aspect, policy) == _oracle_aspect_viewport(viewport, aspect, policy)
        size = (int(rng.integers(1, 3000)), int(rng.integers(1, 3000)))
        zoom = float(rng.choice([1, 1.25, 2.0, 0.8]))
        focus = (float(rng.random()), float(rng.random()))
        assert fc.cover_window(size, aspect, zoom, focus) == _oracle_cover_window(size, aspect, zoom, focus)
    with pytest.raises(ValueError, match="aspect policy"):
        fc.aspect_viewport([960, 540], (16, 9), "stretch")
    for zoom in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="zoom"):
            fc.cover_window((100, 100), (16, 9), zoom)


def test_cover_window_equals_imageops_fit():
    """B12's ValidateCropsTests: the centred cover window is the crop ImageOps.fit takes (whole-pixel cases)."""
    rng = np.random.default_rng(3)
    for size, aspect in (((512, 300), (4, 3)), ((512, 300), (16, 9)), ((512, 288), (9, 16)), ((512, 300), (1, 1)),
                         ((300, 512), (3, 4)), ((640, 480), (16, 9))):
        plate = Image.fromarray(rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8))
        x0, y0, x1, y1 = fc.cover_window(plate.size, aspect)
        assert all(float(value).is_integer() for value in (x0, y0, x1, y1)), (size, aspect)
        out = (int(x1 - x0), int(y1 - y0))
        crop = plate.crop((int(x0), int(y0), int(x1), int(y1)))
        assert np.array_equal(np.asarray(crop), np.asarray(ImageOps.fit(plate, out, Image.Resampling.NEAREST)))


# --------------------------------------------------------------------------- dilate_square and distance_to

def _oracle_grow(mask):
    """forge_matte._grow (A2), verbatim."""
    grown = mask.copy()
    grown[:, 1:] |= mask[:, :-1]
    grown[:, :-1] |= mask[:, 1:]
    result = grown.copy()
    result[1:] |= grown[:-1]
    result[:-1] |= grown[1:]
    return result


def _oracle_distance_to(mask, cap):
    """forge_matte._distance_to (A2), verbatim."""
    distance = np.full(mask.shape, cap + 1, np.uint8)
    distance[mask] = 0
    reached = mask.copy()
    for step in range(1, cap + 1):
        grown = _oracle_grow(reached)
        distance[grown & ~reached] = step
        reached = grown
    return distance


def _oracle_dilate(mask, radius):
    """forge_matte._dilate = forge_core 1.0 _dilate_square = B01/B03/B10 copies, verbatim."""
    if radius <= 0:
        return mask.copy()
    size = 2 * radius + 1
    table = np.pad(np.pad(mask, radius).astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    return (table[size:, size:] - table[:-size, size:] - table[size:, :-size] + table[:-size, :-size]) > 0


def _oracle_dilate_validate_stage(mask, radius):
    """validate_stage._local_dilate (B15): the int64 variant, verbatim."""
    if radius <= 0:
        return mask.copy()
    size = 2 * radius + 1
    table = np.zeros((mask.shape[0] + size, mask.shape[1] + size), np.int64)
    table[1:, 1:] = np.pad(mask, radius).astype(np.int64).cumsum(0).cumsum(1)
    window = table[size:, size:] - table[:-size, size:] - table[size:, :-size] + table[:-size, :-size]
    return window > 0


def test_dilate_square_matches_every_copy_and_pillow():
    rng = np.random.default_rng(11)
    for case in range(400):
        height, width = int(rng.integers(1, 48)), int(rng.integers(1, 48))
        mask = rng.random((height, width)) < rng.random() * 0.2
        radius = int(rng.integers(-1, 9))
        result = fc.dilate_square(mask, radius)
        assert result.dtype == bool and result.shape == mask.shape
        assert np.array_equal(result, _oracle_dilate(mask, radius)), case
        assert np.array_equal(result, _oracle_dilate_validate_stage(mask, radius)), case
        if radius > 0:
            pillow = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(2 * radius + 1))
            assert np.array_equal(result, np.asarray(pillow) > 0), case
    mask = np.zeros((5, 7), bool)
    mask[2, 3] = True
    assert fc.dilate_square(mask, 10 ** 9).all()  # clamped radius: no gigantic padding
    assert np.array_equal(fc.dilate_square(mask.astype(np.uint8) * 7, 1), fc.dilate_square(mask, 1))
    assert fc.dilate_square(np.zeros((0, 4), bool), 3).shape == (0, 4)
    with pytest.raises(ValueError, match="2-D"):
        fc.dilate_square(np.zeros((2, 2, 2), bool), 1)
    with pytest.raises(TypeError):
        fc.dilate_square(mask, 1.5)


def test_distance_to_matches_forge_matte_and_dilate_square():
    rng = np.random.default_rng(12)
    for case in range(300):
        height, width = int(rng.integers(1, 40)), int(rng.integers(1, 40))
        mask = rng.random((height, width)) < rng.random() * 0.1
        cap = int(rng.integers(0, 12))
        distance = fc.distance_to(mask, cap)
        assert distance.dtype == np.uint8
        assert np.array_equal(distance, _oracle_distance_to(mask, cap)), case
        for radius in range(cap + 1):
            assert np.array_equal(distance <= radius, fc.dilate_square(mask, radius)), (case, radius)
    empty = np.zeros((4, 4), bool)
    assert (fc.distance_to(empty, 3) == 4).all()
    assert fc.distance_to(empty, 254).dtype == np.uint8 and fc.distance_to(empty, 255).dtype == np.uint16
    line = np.zeros((1, 400), bool)
    line[0, 0] = True
    far = fc.distance_to(line, 300)
    assert far.dtype == np.uint16 and far[0, 300] == 300 and far[0, 301] == 301 and far[0, 399] == 301
    with pytest.raises(ValueError, match="cap"):
        fc.distance_to(empty, -1)


# --------------------------------------------------------------------------- merge_rects (B13)

def _oracle_merge_rects(mask):
    """map_bundle.merge_rects (B13), verbatim."""
    blocked = np.asarray(mask, bool)
    if blocked.ndim != 2:
        raise ValueError("merge_rects needs a 2-D mask")
    rows = blocked.shape[0]
    used = np.zeros_like(blocked)
    rects = []
    for y in range(rows):
        free = blocked[y] & ~used[y]
        if not free.any():
            continue
        edges = np.flatnonzero(np.diff(np.concatenate(([False], free, [False])).astype(np.int8)))
        for x0, x1 in zip(edges[0::2], edges[1::2]):
            y1 = y + 1
            while y1 < rows and blocked[y1, x0:x1].all() and not used[y1, x0:x1].any():
                y1 += 1
            used[y:y1, x0:x1] = True
            rects.append((int(x0), int(y), int(x1 - x0), int(y1 - y)))
    return rects


def _union(rects, shape):
    cover = np.zeros(shape, np.int32)
    for x, y, w, h in rects:
        cover[y:y + h, x:x + w] += 1
    return cover


def test_merge_rects_is_b13s_cover_rectangle_for_rectangle():
    rng = np.random.default_rng(13)
    for case in range(2500):
        height, width = int(rng.integers(0, 33)), int(rng.integers(0, 33))
        density = float(rng.random())
        if case % 3 == 0:  # room-like masks with large blocked areas
            coarse = rng.random((height // 4 + 1, width // 4 + 1)) < density
            mask = np.kron(coarse, np.ones((4, 4), bool))[:height, :width]
        else:
            mask = rng.random((height, width)) < density
        rects = fc.merge_rects(mask)
        assert rects == _oracle_merge_rects(mask), case
        cover = _union(rects, mask.shape)
        assert (cover <= 1).all() and np.array_equal(cover == 1, mask)  # disjoint, union exact
        assert all(type(value) is int for rect in rects for value in rect)
    with pytest.raises(ValueError, match="2-D"):
        fc.merge_rects(np.zeros(4, bool))


@pytest.mark.perf
def test_merge_rects_scales_on_a_fine_collision_raster():
    """A per-pixel collision raster (B21 layout_build) stays fast: 512^2 cells of 50% noise, about 58,000 runs."""
    mask = np.random.default_rng(14).random((512, 512)) < 0.5
    started = time.perf_counter()
    rects = fc.merge_rects(mask)
    elapsed = time.perf_counter() - started
    assert np.array_equal(_union(rects, mask.shape) == 1, mask)
    assert elapsed <= 0.5, elapsed


# --------------------------------------------------------------------------- edge_seam_report (D9, B11)

_SEAM_LOCAL_STEPS, _SEAM_WINDOW_ROWS, _SEAM_NEAR_STEPS = 8, 4, 3
_SEAM_FLOOR, _DUPLICATE_RATIO, _DUPLICATE_FLOOR, _NOMINAL_SEAM_RATIO = 1.0, 0.25, 2.0, 1.25


def _oracle_premultiplied(pixels):
    values = pixels.astype(np.float64)
    values[..., :3] *= values[..., 3:] / 255.0
    return values


def _oracle_window_max(values, window):
    if values.size == 0:
        return 0.0
    window = min(window, values.shape[0])
    sums = np.cumsum(np.pad(values, ((1, 0), (0, 0))), axis=0)
    return float(((sums[window:] - sums[:-window]) / window).max())


def _oracle_edge_seam_report(left, right, *, left_start=0, right_stop=None, gate=None):
    """extract_platform_strip._local_edge_seam_report (B11; extract_terrain_tiles holds the same), verbatim."""
    a = _oracle_premultiplied(left[:, left_start:][:, -(_SEAM_LOCAL_STEPS + 1):])
    b = _oracle_premultiplied(right[:, :right_stop][:, :_SEAM_LOCAL_STEPS + 1])
    join_rows = np.abs(a[:, -1] - b[:, 0]).mean(axis=1)
    left_steps = np.abs(np.diff(a, axis=1)).mean(axis=2)
    right_steps = np.abs(np.diff(b, axis=1)).mean(axis=2)
    local = np.concatenate([left_steps, right_steps], axis=1)
    means = local.mean(axis=0) if local.size else np.zeros(1)
    near = np.concatenate([left_steps[:, -_SEAM_NEAR_STEPS:].mean(axis=0) if left_steps.size else np.zeros(0),
                           right_steps[:, :_SEAM_NEAR_STEPS].mean(axis=0) if right_steps.size else np.zeros(0)])
    seam = float(join_rows.mean())
    median, p95, peak = float(np.median(means)), float(np.percentile(means, 95)), float(means.max())
    near_median = float(np.median(near)) if near.size else 0.0
    ratio = max(seam / max(peak, EPSILON),
                _oracle_window_max(join_rows[:, None], _SEAM_WINDOW_ROWS)
                / max(_oracle_window_max(local, _SEAM_WINDOW_ROWS), EPSILON))
    if near_median >= _DUPLICATE_FLOOR and seam < _DUPLICATE_RATIO * near_median:
        verdict = "duplicate_edge"
    elif seam > _SEAM_FLOOR and ratio > (gate if gate is not None else _NOMINAL_SEAM_RATIO):
        verdict = "seam"
    else:
        verdict = "continuous"
    return {
        "seam": round(seam, 6), "adjacent_median": round(median, 6), "adjacent_p95": round(p95, 6),
        "adjacent_max": round(peak, 6), "seam_over_median": round(seam / max(median, EPSILON), 6),
        "seam_over_p95": round(seam / max(p95, EPSILON), 6), "seam_ratio": round(ratio, 6),
        "near_median": round(near_median, 6), "verdict": verdict,
        "method": (f"premultiplied RGBA column steps (0-255); join vs interior steps within {_SEAM_LOCAL_STEPS} "
                   f"columns per side, whole edge and worst {_SEAM_WINDOW_ROWS}-row window"),
    }


def _tile(rng, height, width, kind):
    if kind == "noise":
        pixels = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
    elif kind == "gradient":
        xs = np.linspace(0, 255, width)[None, :, None] * np.ones((height, 1, 1))
        pixels = np.concatenate([np.repeat(xs, 3, axis=2), np.full((height, width, 1), 255.0)], axis=2)
        pixels = np.clip(pixels + rng.normal(0, 2, pixels.shape), 0, 255).astype(np.uint8)
    else:  # sprite-like: transparent background, an opaque blob
        pixels = np.zeros((height, width, 4), np.uint8)
        pixels[height // 4:, :, :3] = rng.integers(0, 256, 3, dtype=np.uint8)
        pixels[height // 4:, :, 3] = 255
    return pixels


def test_edge_seam_report_is_b11s_metric_number_for_number():
    """D9: B11's metric, ported exactly; its three verdicts hold wherever it applies."""
    rng = np.random.default_rng(15)
    compared = 0
    for case in range(1500):
        height = int(rng.integers(1, 40))
        kinds = ("noise", "gradient", "sprite")
        left = _tile(rng, height, int(rng.integers(1, 24)), kinds[case % 3])
        right = _tile(rng, height, int(rng.integers(1, 24)), kinds[(case // 3) % 3])
        if case % 5 == 0:
            right[:, 0] = left[:, -1]  # a duplicated edge column
        left_start = int(rng.integers(0, left.shape[1]))
        right_stop = None if case % 2 else int(rng.integers(1, right.shape[1] + 1))
        gate = None if case % 4 else float(rng.uniform(0, 3))
        expected = _oracle_edge_seam_report(left, right, left_start=left_start, right_stop=right_stop, gate=gate)
        result = fc.edge_seam_report(left, right, left_start=left_start, right_stop=right_stop, gate=gate)
        if result["verdict"] in ("too_small", "flat"):
            continue  # relabelled cases, covered by the next test
        assert result == expected, case
        compared += 1
    assert compared > 1300


def test_edge_seam_report_d9_verdicts():
    flat = np.zeros((10, 10, 4), np.uint8)
    flat[...] = (40, 160, 90, 255)
    report = fc.edge_seam_report(flat, flat)
    assert report["verdict"] == "flat" and _oracle_edge_seam_report(flat, flat)["verdict"] == "continuous"
    assert {key: report[key] for key in report if key != "verdict"} == {
        key: value for key, value in _oracle_edge_seam_report(flat, flat).items() if key != "verdict"}
    other = flat.copy()
    other[...] = (200, 20, 20, 255)
    assert fc.edge_seam_report(flat, other)["verdict"] == "seam"  # a step between two flat sides is a seam
    stripe = flat.copy()
    stripe[:, 0] = (200, 20, 20, 255)  # ... but a stripe as strong as the join next to it reads as art
    assert fc.edge_seam_report(flat, stripe)["verdict"] == "continuous"
    one = flat[:, :1]
    small = fc.edge_seam_report(one, one)
    assert small["verdict"] == "too_small" and small["seam_ratio"] == small["seam_over_p95"] == 0.0
    assert fc.edge_seam_report(one, other[:, :1])["verdict"] == "too_small"
    assert fc.edge_seam_report(flat[:, :2], one)["verdict"] == "flat"  # one interior step is enough to judge
    ramp = np.zeros((12, 12, 4), np.uint8)
    ramp[..., :3] = (np.arange(12) * 20).astype(np.uint8)[None, :, None]
    ramp[..., 3] = 255
    assert fc.edge_seam_report(ramp, ramp)["verdict"] == "seam"  # the ramp's wrap jumps from 220 back to 0
    assert fc.edge_seam_report(ramp[:, :6], ramp[:, 5:])["verdict"] == "duplicate_edge"  # column 5 twice
    assert fc.edge_seam_report(ramp[:, :6], ramp[:, 6:])["verdict"] == "continuous"
    for report in (report, small):
        assert report["verdict"] in fc.EDGE_SEAM_VERDICTS
        assert_valid_contract(report, "common", "seamReport")


def test_edge_seam_report_inputs():
    rgb = np.full((6, 5, 3), 80, np.uint8)
    rgba = np.dstack([rgb, np.full((6, 5), 255, np.uint8)])
    assert fc.edge_seam_report(rgb, Image.fromarray(rgba)) == fc.edge_seam_report(rgba, rgba)
    vertical = fc.edge_seam_report(np.swapaxes(rgba, 0, 1), np.swapaxes(rgba, 0, 1))
    assert vertical["verdict"] == "flat"
    with pytest.raises(ValueError, match="same non-zero height"):
        fc.edge_seam_report(rgba, rgba[:5])
    with pytest.raises(ValueError, match="no column"):
        fc.edge_seam_report(rgba, rgba, left_start=5)
    with pytest.raises(ValueError, match="no column"):
        fc.edge_seam_report(rgba, rgba, right_stop=0)
    with pytest.raises(ValueError, match="gate"):
        fc.edge_seam_report(rgba, rgba, gate=float("nan"))


# --------------------------------------------------------------------------- ownership_slice (D14)

_B02_NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
_PROTOTYPE_NEIGHBOURS = ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1))


def _oracle_b02_shift(array, dy, dx, fill):
    height, width = array.shape
    result = np.full_like(array, fill)
    result[max(0, dy):height + min(0, dy), max(0, dx):width + min(0, dx)] = \
        array[max(0, -dy):height + min(0, -dy), max(0, -dx):width + min(0, -dx)]
    return result


def _oracle_b02_ownership_slice(rgba, boxes, *, threshold=16, min_area=4, attach_radius=6,
                                neighbours=_B02_NEIGHBOURS):
    """assemble_frames.ownership_slice (B02), verbatim except the neighbour order is a parameter."""
    alpha = rgba[..., 3]
    labels, count = fc.label_components(alpha > threshold, 8)
    areas = np.bincount(labels.ravel(), minlength=count + 1)
    counts = np.stack([np.bincount(labels[y0:y1, x0:x1].ravel(), minlength=count + 1)
                       for x0, y0, x1, y1 in [box for _, box in boxes]])
    owner = counts.argmax(axis=0).astype(np.int32)
    owner[(counts.max(axis=0) == 0) | (areas < min_area)] = -1
    owner[0] = -1
    owners = owner[labels]
    loose = (alpha > 0) & (owners < 0)
    attached = 0
    for _ in range(attach_radius):
        pending = loose & (owners < 0)
        if not pending.any():
            break
        grown = np.full(owners.shape, -1, np.int32)
        for dy, dx in neighbours:
            neighbour = _oracle_b02_shift(owners, dy, dx, -1)
            take = pending & (grown < 0) & (neighbour >= 0)
            grown[take] = neighbour[take]
        attached += int((grown >= 0).sum())
        owners = np.where(grown >= 0, grown, owners)
    fallback = 0
    for index, (_, (x0, y0, x1, y1)) in enumerate(boxes):
        region = owners[y0:y1, x0:x1]
        unassigned = loose[y0:y1, x0:x1] & (region < 0)
        fallback += int(unassigned.sum())
        region[unassigned] = index
    dropped = int(((alpha > 0) & (owners < 0)).sum())
    ys, xs = np.nonzero(owners >= 0)
    assigned = owners[ys, xs]
    order = np.argsort(assigned, kind="stable")
    ys, xs, assigned = ys[order], xs[order], assigned[order]
    splits = np.searchsorted(assigned, np.arange(len(boxes) + 1))
    left = top = 0
    right = max(box[2] - box[0] for _, box in boxes)
    bottom = max(box[3] - box[1] for _, box in boxes)
    for index, (_, (x0, y0, _x1, _y1)) in enumerate(boxes):
        lo, hi = splits[index], splits[index + 1]
        if lo < hi:
            left = min(left, int(xs[lo:hi].min()) - x0)
            top = min(top, int(ys[lo:hi].min()) - y0)
            right = max(right, int(xs[lo:hi].max()) + 1 - x0)
            bottom = max(bottom, int(ys[lo:hi].max()) + 1 - y0)
    pad_left, pad_top = -left, -top
    canvas = (right - left, bottom - top)
    frames, records = [], []
    for index, (item_id, (x0, y0, x1, y1)) in enumerate(boxes):
        lo, hi = splits[index], splits[index + 1]
        frame = np.zeros((canvas[1], canvas[0], 4), np.uint8)
        fy, fx = ys[lo:hi], xs[lo:hi]
        frame[fy - y0 + pad_top, fx - x0 + pad_left] = rgba[fy, fx]
        outside = int((~((fx >= x0) & (fx < x1) & (fy >= y0) & (fy < y1))).sum())
        owned = np.flatnonzero(owner == index)
        frames.append(frame)
        records.append({"sheet_origin": [x0 - pad_left, y0 - pad_top], "owned_components": int(owned.size),
                        "pixels_from_outside_cell": outside})
    max_width = max(box[2] - box[0] for _, box in boxes)
    max_height = max(box[3] - box[1] for _, box in boxes)
    report = {
        "mode": "ownership", "threshold": threshold, "min_area": min_area, "attach_radius": attach_radius,
        "connectivity": 8, "canvas": [int(canvas[0]), int(canvas[1])],
        "padding": [pad_left, pad_top, int(canvas[0] - pad_left - max_width), int(canvas[1] - pad_top - max_height)],
        "attached_px": attached, "cell_fallback_px": fallback, "dropped_px": dropped,
    }
    return frames, report, records


class _QcError(ValueError):
    pass


def _oracle_b03_dilate(mask, radius):
    if radius <= 0:
        return mask.copy()
    size = 2 * radius + 1
    table = np.pad(np.pad(mask, radius).astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    return (table[size:, size:] - table[:-size, size:] - table[size:, :-size] + table[:-size, :-size]) > 0


def _oracle_b03_attach_soft(owner, soft, radius):
    height, width = owner.shape
    owner = owner.copy()
    ys, xs = np.nonzero(soft & _oracle_b03_dilate(owner >= 0, radius))
    for _ in range(radius):
        if ys.size == 0:
            break
        found = np.full(ys.size, -1, owner.dtype)
        for dy, dx in _PROTOTYPE_NEIGHBOURS:
            open_ = found < 0
            sy, sx = ys[open_] - dy, xs[open_] - dx
            inside = (sy >= 0) & (sy < height) & (sx >= 0) & (sx < width)
            values = np.full(sy.size, -1, owner.dtype)
            values[inside] = owner[sy[inside], sx[inside]]
            found[np.flatnonzero(open_)[values >= 0]] = values[values >= 0]
        reached = found >= 0
        owner[ys[reached], xs[reached]] = found[reached]
        ys, xs = ys[~reached], xs[~reached]
    return owner


def _oracle_b03_ownership_slice(rgba, boxes, *, threshold=127, min_area=64, attach_radius=6, count=None):
    """sheet_qc.ownership_slice (B03) with _cell_map and _components, verbatim (QcError local)."""
    height, width = rgba.shape[:2]
    alpha = rgba[..., 3]
    cells = len(boxes)
    count = cells if count is None else count
    if not 1 <= count <= cells:
        raise _QcError(f"--count must be between 1 and {cells} (rows x cols); got {count}.")
    cell_map = np.empty((height, width), np.int32)
    for index, (x0, y0, x1, y1) in enumerate(boxes):
        cell_map[y0:y1, x0:x1] = index
    labels, total = fc.label_components(alpha > threshold, 8)
    ys, xs = np.nonzero(labels)
    ids = labels[ys, xs]
    counts = np.bincount(ids.astype(np.int64) * cells + cell_map[ys, xs], minlength=(total + 1) * cells)
    counts = counts.reshape(total + 1, cells)
    area = counts.sum(axis=1)
    owner_of = np.full(total + 1, -1, np.int16)
    big = area >= min_area
    big[0] = False
    owner_of[big] = np.argmax(counts[big], axis=1)
    owner = owner_of[labels]
    owner = _oracle_b03_attach_soft(owner, (alpha > 0) & (owner < 0), attach_radius)
    cols = len({box[0] for box in boxes})
    boxes = boxes[:count]
    extents = []
    for index, (x0, y0, _x1, _y1) in enumerate(boxes):
        ys, xs = np.nonzero(owner == index)
        if xs.size == 0:
            raise _QcError(f"Cell {[index // cols, index % cols]} holds no subject; check --rows/--cols, --count or "
                           "the key.")
        extents.append((int(xs.min()) - x0, int(ys.min()) - y0, int(xs.max()) + 1 - x0, int(ys.max()) + 1 - y0))
    cell_w = max(box[2] - box[0] for box in boxes)
    cell_h = max(box[3] - box[1] for box in boxes)
    pad_l = max(0, -min(extent[0] for extent in extents))
    pad_t = max(0, -min(extent[1] for extent in extents))
    pad_r = max(0, max(extent[2] for extent in extents) - cell_w)
    pad_b = max(0, max(extent[3] for extent in extents) - cell_h)
    canvas = (cell_h + pad_t + pad_b, cell_w + pad_l + pad_r)
    frames = []
    for index, (x0, y0, _x1, _y1) in enumerate(boxes):
        frame = np.zeros(canvas + (4,), np.uint8)
        ys, xs = np.nonzero(owner == index)
        frame[ys - y0 + pad_t, xs - x0 + pad_l] = rgba[ys, xs]
        frames.append(frame)
    dropped = (alpha > 0) & (owner < 0)
    info = {
        "threshold": threshold, "min_area": min_area, "attach_radius": attach_radius, "cells_used": count,
        "unused_cells_px": int(((owner >= count) & (alpha > 0)).sum()),
        "padding": [int(pad_l), int(pad_t), int(pad_r), int(pad_b)],
        "canvas": [int(canvas[1]), int(canvas[0])],
        "frame_origins_in_sheet": [[int(box[0]) - pad_l, int(box[1]) - pad_t] for box in boxes],
        "dropped_px": int(dropped.sum()),
        "dropped_max_alpha": int(alpha[dropped].max()) if dropped.any() else 0,
    }
    return frames, info


def _random_sheet(rng, rows, cols, cell):
    """Soft blobs that cross cell lines, faint haze bridges and isolated specks, on transparency."""
    height, width = rows * cell, cols * cell
    yy, xx = np.mgrid[:height, :width]
    alpha = np.zeros((height, width), np.float64)
    for index in range(rows * cols):
        row, col = divmod(index, cols)
        cy = row * cell + cell * rng.uniform(0.3, 0.7)
        cx = col * cell + cell * rng.uniform(0.25, 0.95)  # some blobs lean over the right cell line
        radius = cell * rng.uniform(0.15, 0.3)
        alpha = np.maximum(alpha, np.clip(1.4 - np.hypot(yy - cy, xx - cx) / radius, 0, 1))
    alpha = np.where(rng.random(alpha.shape) < 0.01, rng.uniform(0.005, 0.2, alpha.shape), alpha)  # haze specks
    rgba = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
    rgba[..., 3] = np.floor(alpha * 255 + 0.5).astype(np.uint8)
    rgba[rgba[..., 3] == 0] = 0
    return rgba


def _b02_view(report):
    keys = ("mode", "threshold", "min_area", "attach_radius", "connectivity", "canvas", "padding", "attached_px",
            "cell_fallback_px", "dropped_px")
    return {key: report[key] for key in keys}


def _b03_view(report):
    keys = ("threshold", "min_area", "attach_radius", "cells_used", "unused_cells_px", "padding", "canvas",
            "frame_origins_in_sheet", "dropped_px", "dropped_max_alpha")
    return {key: report[key] for key in keys}


def _records(report):
    return [{key: frame[key] for key in ("sheet_origin", "owned_components", "pixels_from_outside_cell")}
            for frame in report["frames"]]


def test_ownership_slice_haze_drop_is_b03s_slicer():
    """D14: spill QC (B03) policy, drop haze at alpha > 127 and 64 px; frames and info unchanged."""
    rng = np.random.default_rng(16)
    for case in range(40):
        rows, cols, cell = int(rng.integers(1, 4)), int(rng.integers(1, 5)), int(rng.integers(24, 64))
        sheet = _random_sheet(rng, rows, cols, cell)
        boxes = fc.rounded_grid_boxes(sheet.shape[1], sheet.shape[0], rows, cols)
        try:
            expected_frames, expected = _oracle_b03_ownership_slice(sheet, boxes, min_area=16)
        except _QcError:
            continue  # B03 refuses a sheet with an empty cell; the wrapper keeps doing that (empty_cells)
        frames, report = fc.ownership_slice(sheet, rows, cols, alpha_threshold=127, min_area=16, haze="drop")
        assert _b03_view(report) == expected, case
        assert all(np.array_equal(a, b) for a, b in zip(frames, expected_frames)) and len(frames) == len(expected_frames)
        assert report["empty_cells"] == []


def test_ownership_slice_haze_keep_is_b02s_slicer_up_to_the_neighbour_order():
    """D14: frame assembly (B02) policy, keep haze at alpha > 16 and 4 px. With the prototype's neighbour
    order B02's slicer gives the same frames, report and records; its own order only breaks ties."""
    rng = np.random.default_rng(17)
    for case in range(40):
        rows, cols, cell = int(rng.integers(1, 4)), int(rng.integers(1, 5)), int(rng.integers(24, 64))
        sheet = _random_sheet(rng, rows, cols, cell)
        boxes = [(f"c{index}", box) for index, box in
                 enumerate(fc.rounded_grid_boxes(sheet.shape[1], sheet.shape[0], rows, cols))]
        expected_frames, expected, records = _oracle_b02_ownership_slice(sheet, boxes,
                                                                         neighbours=_PROTOTYPE_NEIGHBOURS)
        frames, report = fc.ownership_slice(sheet, rows, cols, alpha_threshold=16, min_area=4, haze="keep")
        assert _b02_view(report) == expected, case
        assert _records(report) == records, case
        assert all(np.array_equal(a, b) for a, b in zip(frames, expected_frames)), case


def test_ownership_tie_break_follows_the_report_v2_prototype():
    """A haze pixel two owners reach in the same step goes to its left neighbour's owner (prototype order:
    left, right, above, below, diagonals); B02's private copy preferred the lower right."""
    sheet = np.zeros((8, 20, 4), np.uint8)
    sheet[2:6, 2:9] = (200, 40, 40, 255)    # owned by cell 0
    sheet[2:6, 10:18] = (40, 40, 200, 255)  # owned by cell 1
    sheet[3, 9] = (90, 90, 90, 6)           # faint haze touching both
    frames, report = fc.ownership_slice(sheet, 1, 2, alpha_threshold=16, min_area=4, haze="keep")
    assert frames[0][3, 9 + report["padding"][0], 3] == 6 and report["frames"][0]["pixels_from_outside_cell"] == 0
    b02_frames, _, _ = _oracle_b02_ownership_slice(sheet, [("a", (0, 0, 10, 8)), ("b", (10, 0, 20, 8))])
    assert b02_frames[1][3, 9 - 10 + 1, 3] == 6  # B02's order gave it to cell 1 (one px outside, padded left)


def test_ownership_slice_on_the_fox_fixture_both_policies():
    """Real fixture (PROVENANCE.json): report v2 5.4 bytes for B03; B02's slicer byte for byte."""
    with Image.open(real_fixture("raw-fox-run-v1.png")) as image:
        fox = np.asarray(image.convert("RGBA"))
    frames, report = fc.ownership_slice(fox, 2, 4, alpha_threshold=127, min_area=64, haze="drop")
    assert hashlib.sha256(np.stack(frames).tobytes()).hexdigest() == \
        "dbbd0093a14f537f000fcd8771a784ac693555013a4452bebc994201133a8565"  # sheet_qc's pinned prototype hash
    assert report["padding"] == [12, 0, 0, 0] and report["canvas"] == [396, 512]
    assert report["dropped_px"] == 17337 and report["dropped_max_alpha"] <= 5  # the review's figure
    boxes = [(f"c{index}", box) for index, box in enumerate(fc.rounded_grid_boxes(1536, 1024, 2, 4))]
    expected_frames, expected, records = _oracle_b02_ownership_slice(fox, boxes)  # B02's own neighbour order
    frames, report = fc.ownership_slice(fox, 2, 4, alpha_threshold=16, min_area=4, haze="keep")
    assert _b02_view(report) == expected and _records(report) == records
    assert all(np.array_equal(a, b) for a, b in zip(frames, expected_frames))
    assert report["dropped_px"] == 0 and report["canvas"] == [398, 512] and report["padding"] == [13, 0, 1, 0]


def test_ownership_slice_boxes_count_and_validation():
    sheet = np.array(make_magenta_sheet(1, 3, 48, spill_px=6))
    keyed = sheet.copy()
    keyed[(keyed[..., :3] == (255, 0, 255)).all(-1)] = 0
    frames, report = fc.ownership_slice(keyed, 1, 3, alpha_threshold=16, min_area=4, haze="keep")
    assert report["padding"] == [0, 0, 6, 0] and report["canvas"] == [54, 48]
    same, crop = fc.ownership_slice(keyed, boxes=[(0, 0, 48, 48), (48, 0, 96, 48), (96, 0, 144, 48)],
                                    alpha_threshold=16, min_area=4, haze="keep")
    assert all(np.array_equal(a, b) for a, b in zip(frames, same)) and crop == report
    spare = np.zeros((48, 192, 4), np.uint8)
    spare[:, :144] = keyed
    frames, report = fc.ownership_slice(spare, 1, 4, alpha_threshold=16, min_area=4, haze="drop", count=3)
    assert len(frames) == 3 and report["cells_used"] == 3 and report["empty_cells"] == []
    frames, report = fc.ownership_slice(spare, 1, 4, alpha_threshold=16, min_area=4, haze="drop")
    assert report["empty_cells"] == [3] and not frames[3].any()
    haze = keyed.copy()
    haze[1, 100] = (9, 9, 9, 3)  # detached haze in cell 2
    _, kept = fc.ownership_slice(haze, 1, 3, alpha_threshold=16, min_area=4, haze="keep")
    _, dropped = fc.ownership_slice(haze, 1, 3, alpha_threshold=16, min_area=4, haze="drop")
    assert (kept["cell_fallback_px"], kept["dropped_px"]) == (1, 0)
    assert (dropped["cell_fallback_px"], dropped["dropped_px"], dropped["dropped_max_alpha"]) == (0, 1, 3)
    shared_haze = keyed.copy()
    shared_haze[1, 45] = (9, 9, 9, 3)  # detached haze where two crop boxes overlap: the first box keeps it
    frames, report = fc.ownership_slice(shared_haze, boxes=[(0, 0, 60, 48), (40, 0, 144, 48)],
                                        alpha_threshold=16, min_area=4, haze="keep")
    pad_l, pad_t = report["padding"][:2]
    assert report["cells"] == 2 and report["cell_fallback_px"] == 1
    assert frames[0][1 + pad_t, 45 + pad_l, 3] == 3 and report["frames"][1]["owned_components"] == 2
    for kwargs, message in (({"haze": "maybe"}, "haze"), ({"count": 4}, "count"), ({"count": 0}, "count"),
                            ({"alpha_threshold": 255}, "alpha_threshold"), ({"min_area": -1}, "min_area")):
        arguments = {"alpha_threshold": 16, "min_area": 4, "haze": "keep", **kwargs}
        with pytest.raises(ValueError, match=message):
            fc.ownership_slice(keyed, 1, 3, **arguments)
    with pytest.raises(ValueError, match="rows and cols"):
        fc.ownership_slice(keyed, alpha_threshold=16, min_area=4, haze="keep")
    with pytest.raises(ValueError, match="not both"):
        fc.ownership_slice(keyed, 1, 3, boxes=[(0, 0, 48, 48)], alpha_threshold=16, min_area=4, haze="keep")
    with pytest.raises(ValueError, match="inside"):
        fc.ownership_slice(keyed, boxes=[(0, 0, 200, 48)], alpha_threshold=16, min_area=4, haze="keep")
    with pytest.raises(TypeError):
        fc.ownership_slice(keyed, 1, 3, alpha_threshold=16, min_area=4)  # the policy is always explicit (D14)
