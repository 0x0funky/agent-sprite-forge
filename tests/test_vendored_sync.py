"""Vendored copies of shared/ files stay identical to their canonicals (shared/VENDORED.json),
and tools/vendor_sync.py checks and repairs them safely."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from forge_testutils import REPO_ROOT, run_cli

TOOL = REPO_ROOT / "tools" / "vendor_sync.py"
MANIFEST = json.loads((REPO_ROOT / "shared" / "VENDORED.json").read_text(encoding="utf-8"))["files"]
TEXT_SUFFIXES = {".py", ".json", ".md", ".js", ".mjs", ".txt"}
EXPECTED_CANONICALS = {
    "shared/forge_core.py", "shared/forge_matte.py", "shared/forge_av.py", "shared/forge_palette.py",
    "shared/forge_nav.py", "shared/forge_schema.py",  # integration D4, D31
    *(f"shared/schemas/{domain}.schema.json" for domain in ("common", "sprite", "video", "map", "codeart", "media")),
}


def digest(path: Path) -> str:
    """The vendoring contract: sha256 of the content, with CRLF normalised to LF in text files."""
    data = path.read_bytes()
    if path.suffix in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- this checkout

def test_manifest_covers_the_shared_modules_and_schemas():
    assert {entry["canonical"] for entry in MANIFEST} == EXPECTED_CANONICALS
    for entry in MANIFEST:
        assert entry["targets"], entry["canonical"]
        for target in entry["targets"]:
            assert target.startswith("skills/") and Path(target).name == Path(entry["canonical"]).name


def test_every_existing_canonical_matches_its_copies():
    for entry in MANIFEST:
        canonical = REPO_ROOT / entry["canonical"]
        if not canonical.is_file():
            continue
        for target in entry["targets"]:
            copy = REPO_ROOT / target
            assert copy.is_file(), f"{target} is missing; run python tools/vendor_sync.py --write"
            assert digest(copy) == digest(canonical), f"{target} differs from {entry['canonical']}"


def test_no_copy_exists_without_its_canonical():
    for entry in MANIFEST:
        if not (REPO_ROOT / entry["canonical"]).is_file():
            orphans = [target for target in entry["targets"] if (REPO_ROOT / target).exists()]
            assert not orphans, f"copies without {entry['canonical']}: {orphans}"


@pytest.mark.skipif(os.environ.get("FORGE_VENDOR_STRICT") != "1", reason="set FORGE_VENDOR_STRICT=1 once every Wave A module has landed")
def test_strict_mode_every_canonical_exists():
    missing = [entry["canonical"] for entry in MANIFEST if not (REPO_ROOT / entry["canonical"]).is_file()]
    assert not missing


def test_check_passes_on_this_checkout_under_cp1252():
    result = run_cli([TOOL, "--check"], "cp1252")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ok"


@pytest.mark.parametrize("encoding", ["cp1252", "cp950"])
def test_help_works_on_narrow_consoles(encoding):
    result = run_cli([TOOL, "--help"], encoding)
    assert result.returncode == 0, result.stderr
    assert "--write" in result.stdout and result.stdout.isascii()


# --------------------------------------------------------------------------- synthetic trees

def make_tree(root: Path, manifest: list[dict], files: dict[str, bytes]) -> Path:
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "VENDORED.json").write_text(json.dumps({"files": manifest}), encoding="utf-8")
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


DEMO = [{"canonical": "shared/forge_demo.py", "targets": ["skills/a/scripts/forge_demo.py", "skills/b/scripts/forge_demo.py"]}]


def run_tool(root: Path, *args: str, strict: bool = False):
    return run_cli([TOOL, "--root", root, *args], "cp1252", env={"FORGE_VENDOR_STRICT": "1" if strict else "0"})


def test_write_repairs_drift_and_never_touches_the_canonical(tmp_path):
    canonical = b"VALUE = 2\n"
    root = make_tree(tmp_path, DEMO, {"shared/forge_demo.py": canonical, "skills/a/scripts/forge_demo.py": b"VALUE = 1\n"})
    checked = run_tool(root, "--check")
    assert checked.returncode == 1
    assert "stale copy: skills/a/scripts/forge_demo.py" in checked.stderr
    assert "missing copy: skills/b/scripts/forge_demo.py" in checked.stderr
    assert checked.stderr.rstrip().splitlines()[-1].startswith("error: 2 vendoring problem(s)")
    written = run_tool(root, "--write")
    assert written.returncode == 0, written.stderr
    assert json.loads(written.stdout)["written"] == ["skills/a/scripts/forge_demo.py", "skills/b/scripts/forge_demo.py"]
    assert (root / "shared/forge_demo.py").read_bytes() == canonical
    for target in DEMO[0]["targets"]:
        assert (root / target).read_bytes() == canonical
    assert run_tool(root, "--check").returncode == 0
    assert not list(root.rglob("*.vendoring")), "no temporary files are left behind"


def test_crlf_checkout_is_not_drift(tmp_path):
    root = make_tree(tmp_path, DEMO, {"shared/forge_demo.py": b"A = 1\nB = 2\n",
                                       "skills/a/scripts/forge_demo.py": b"A = 1\r\nB = 2\r\n",
                                       "skills/b/scripts/forge_demo.py": b"A = 1\nB = 2\n"})
    assert run_tool(root, "--check").returncode == 0


def test_missing_canonical_is_pending_unless_strict(tmp_path):
    root = make_tree(tmp_path, DEMO, {})
    relaxed = run_tool(root, "--check")
    assert relaxed.returncode == 0 and json.loads(relaxed.stdout)["pending_canonicals"] == ["shared/forge_demo.py"]
    strict = run_tool(root, "--check", strict=True)
    assert strict.returncode == 1 and "missing canonical: shared/forge_demo.py" in strict.stderr


def test_copy_without_canonical_is_an_error_even_for_write(tmp_path):
    root = make_tree(tmp_path, DEMO, {"skills/a/scripts/forge_demo.py": b"ORPHAN = True\n"})
    for mode in ("--check", "--write"):
        result = run_tool(root, mode)
        assert result.returncode == 1 and "copy without canonical: skills/a/scripts/forge_demo.py" in result.stderr
    assert (root / "skills/a/scripts/forge_demo.py").read_bytes() == b"ORPHAN = True\n"


def test_unlisted_copy_is_reported(tmp_path):
    root = make_tree(tmp_path, DEMO, {"shared/forge_demo.py": b"X = 1\n", "skills/a/scripts/forge_demo.py": b"X = 1\n",
                                       "skills/b/scripts/forge_demo.py": b"X = 1\n", "skills/c/scripts/forge_demo.py": b"X = 1\n"})
    result = run_tool(root, "--check")
    assert result.returncode == 1 and "unlisted copy: skills/c/scripts/forge_demo.py" in result.stderr


@pytest.mark.parametrize("encoding", ["cp1252", "utf-8", "cp950"])
def test_error_and_problem_lines_escape_non_ascii_paths(tmp_path, encoding):
    """r2-conventions finding 14: with --root in a folder named '測試 ü', the 'error: cannot read' line put raw
    bytes on stderr (0xFC for 'ü' under cp1252, raw UTF-8 or Big5 otherwise). Every line is ASCII now."""
    root = tmp_path / "測試 ü"
    root.mkdir()
    result = run_cli([TOOL, "--check", "--root", root], encoding)
    assert result.returncode == 1 and result.stderr.isascii(), result.stderr
    assert result.stderr.startswith("error: cannot read shared/VENDORED.json") and "\\u6e2c\\u8a66 \\xfc" in result.stderr
    manifest = [{"canonical": "shared/forge_測.py", "targets": ["skills/測試/scripts/forge_測.py"]},
                {"canonical": "shared/forge_試.py", "targets": ["skills/測試/scripts/forge_試.py"]}]
    tree = make_tree(tmp_path / "tree", manifest, {"skills/測試/scripts/forge_測.py": b"X = 1\n",
                                                    "shared/forge_試.py": b"Y = 2\n",
                                                    "skills/測試/scripts/forge_試.py": b"Y = 1\n"})
    result = run_cli([TOOL, "--check", "--root", tree], encoding)
    assert result.returncode == 1 and result.stderr.isascii(), result.stderr
    assert "stale copy: skills/\\u6e2c\\u8a66/scripts/forge_\\u8a66.py (canonical shared/forge_\\u8a66.py)" in result.stderr
    assert "copy without canonical: skills/\\u6e2c\\u8a66/scripts/forge_\\u6e2c.py" in result.stderr


@pytest.mark.parametrize("entry", [
    {"canonical": "shared/forge_demo.py", "targets": ["tools/forge_demo.py"]},
    {"canonical": "shared/forge_demo.py", "targets": ["skills/a/scripts/other.py"]},
    {"canonical": "shared/forge_demo.py", "targets": ["skills/a/../../forge_demo.py"]},
    {"canonical": "C:/shared/forge_demo.py", "targets": ["skills/a/scripts/forge_demo.py"]},
], ids=["outside-skills", "renamed", "escapes", "absolute"])
def test_unsafe_manifest_is_refused_without_traceback(tmp_path, entry):
    root = make_tree(tmp_path, [entry], {"shared/forge_demo.py": b"X = 1\n"})
    result = run_tool(root, "--write")
    assert result.returncode == 1 and result.stderr.startswith("error: ") and "Traceback" not in result.stderr
    assert not (root / "tools").exists() and not (root / "forge_demo.py").exists()
