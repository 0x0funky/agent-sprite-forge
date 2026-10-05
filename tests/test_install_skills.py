"""tools/install_skills.py: guarded skill install with a manifest and backup, plus drift checks
(plan B22-T4; F-11, roadmap 7.2).

Hermetic: the home folder points at a temporary one; sources are small synthetic skill trees,
except one test that installs this checkout's real skills into a temporary folder.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

from forge_testutils import REPO_ROOT, SKILLS_DIR, run_cli

TOOL = REPO_ROOT / "tools" / "install_skills.py"
_spec = importlib.util.spec_from_file_location("forge_tools_install_skills", TOOL)
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)

SCRIPTS = SKILLS_DIR / "generate2dmedia" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import forge_doctor  # noqa: E402


@pytest.fixture
def source(tmp_path):
    """Two skills with the junk a working tree accumulates, and one folder that is not a skill."""
    root = tmp_path / "checkout" / "skills"
    for skill in ("alpha", "beta"):
        (root / skill / "scripts").mkdir(parents=True)
        (root / skill / "SKILL.md").write_text(f"---\nname: {skill}\n---\n", encoding="utf-8")
        (root / skill / "scripts" / "tool.py").write_text("print('hi')\n", encoding="utf-8")
        (root / skill / "references").mkdir()
        (root / skill / "references" / "notes.md").write_text("notes\n", encoding="utf-8")
        (root / skill / "scripts" / "__pycache__").mkdir()
        (root / skill / "scripts" / "__pycache__" / "tool.cpython-313.pyc").write_bytes(b"\0bytecode")
        (root / skill / ".DS_Store").write_bytes(b"junk")
    (root / "draft").mkdir()
    (root / "draft" / "README.md").write_text("not a skill yet\n", encoding="utf-8")
    return root


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(folder))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return folder


def run(capsys, *argv):
    code = install.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    assert out.isascii() and err.isascii()
    return code, json.loads(out) if out.strip() else None, err


def test_help_works_under_cp1252():
    for encoding in ("cp1252", "cp950"):
        result = run_cli([TOOL, "--help"], encoding, timeout=120)
        assert result.returncode == 0 and result.stdout.isascii() and "--apply" in result.stdout, result.stderr


def test_temp_home_apply_works(source, home, capsys):
    code, summary, err = run(capsys, "--apply", "--host", "claude", "--source", source)
    assert code == 0, err
    dest = home / ".claude" / "skills"
    assert summary["dest"] == str(dest) and summary["skills"] == ["alpha", "beta"] and summary["backup"] is None
    installed = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert installed == [install.MANIFEST, "alpha/SKILL.md", "alpha/references/notes.md", "alpha/scripts/tool.py",
                         "beta/SKILL.md", "beta/references/notes.md", "beta/scripts/tool.py"]  # no bytecode or junk
    manifest = json.loads((dest / install.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["schema"] == "agent-sprite-forge.install.v1" and manifest["skills"] == ["alpha", "beta"]
    assert {f["path"]: f["sha256"] for f in manifest["files"]}["alpha/scripts/tool.py"] == \
        install.sha256_file(source / "alpha" / "scripts" / "tool.py")
    assert run(capsys, "--check", "--host", "claude", "--source", source)[0] == 0
    assert not list(dest.parent.glob(".skills.asf-stage-*"))


def test_drift_detected(source, home, capsys):
    dest = home / ".codex" / "skills"
    assert run(capsys, "--apply", "--host", "codex", "--source", source)[0] == 0
    (dest / "alpha" / "scripts" / "tool.py").write_text("print('edited')\n", encoding="utf-8")
    (dest / "alpha" / "scripts" / "__pycache__").mkdir()
    (dest / "alpha" / "scripts" / "__pycache__" / "tool.cpython-313.pyc").write_bytes(b"\0")
    (dest / "beta" / "references" / "notes.md").unlink()
    (source / "beta" / "SKILL.md").write_text("---\nname: beta\n---\nnew text\n", encoding="utf-8")
    code, _, err = run(capsys, "--check", "--host", "codex", "--source", source)
    assert code == 1
    assert "changed (edited locally): alpha/scripts/tool.py" in err
    assert "extra: alpha/scripts/__pycache__/tool.cpython-313.pyc" in err
    assert "missing: beta/references/notes.md" in err and "changed (outdated): beta/SKILL.md" in err
    assert "error: 4 file(s) differ" in err
    # The doctor sees the same local drift from the manifest, without the checkout.
    report = forge_doctor.diagnose(skills_root=dest, project_dir=home, run_versions=False, console_encoding="utf-8")
    drift = next(c for c in report["checks"] if c["id"] == "skills.install")
    assert drift["status"] == "WARN" and "1 changed, 1 missing, 1 extra" in drift["detail"]


def test_apply_backs_up_then_replaces(source, home, capsys):
    dest = home / ".claude" / "skills"
    (dest / "other-skill").mkdir(parents=True)
    (dest / "other-skill" / "SKILL.md").write_text("someone else's\n", encoding="utf-8")
    assert run(capsys, "--apply", "--host", "claude", "--source", source)[0] == 0
    (dest / "alpha" / "scripts" / "tool.py").write_text("print('edited')\n", encoding="utf-8")
    backup = home / "backup"
    code, summary, err = run(capsys, "--apply", "--host", "claude", "--source", source, "--backup-dir", backup)
    assert code == 0, err
    assert summary["backup"] == str(backup)
    assert (backup / "alpha" / "scripts" / "tool.py").read_text(encoding="utf-8") == "print('edited')\n"
    assert (dest / "alpha" / "scripts" / "tool.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert (dest / "other-skill" / "SKILL.md").is_file() and not (backup / "other-skill").exists()
    assert run(capsys, "--check", "--host", "claude", "--source", source)[0] == 0


def test_refuses_an_existing_backup_dir(source, home, capsys):
    assert run(capsys, "--apply", "--host", "claude", "--source", source)[0] == 0
    backup = home / "backup"
    backup.mkdir()
    code, _, err = run(capsys, "--apply", "--host", "claude", "--source", source, "--backup-dir", backup)
    assert code == 1 and "already exists" in err and not list(backup.iterdir())


def test_a_failed_apply_restores_everything(source, home, capsys, monkeypatch):
    dest = home / ".claude" / "skills"
    assert run(capsys, "--apply", "--host", "claude", "--source", source)[0] == 0
    (dest / "alpha" / "scripts" / "tool.py").write_text("print('mine')\n", encoding="utf-8")
    before = {p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob("*") if p.is_file()}
    real_copy, calls = install._copy_verified, []

    def flaky(source_path, target, digest):
        calls.append(target)
        if len(calls) == 4:
            raise OSError("disk full")
        real_copy(source_path, target, digest)

    monkeypatch.setattr(install, "_copy_verified", flaky)
    code, _, err = run(capsys, "--apply", "--host", "claude", "--source", source, "--backup-dir", home / "backup")
    assert code == 1 and "disk full" in err
    after = {p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob("*") if p.is_file()}
    assert after == before  # the old install and its manifest are untouched
    assert not (home / "backup").exists() and not list(dest.parent.glob(".skills.asf-stage-*"))
    monkeypatch.setattr(install, "_copy_verified", real_copy)
    real_rename, renames = os.rename, []

    def failing_swap(source_path, target):
        renames.append(target)
        if len(renames) == 3:  # alpha swapped in, then beta's old copy cannot move
            raise OSError("folder in use")
        real_rename(source_path, target)

    monkeypatch.setattr(install.os, "rename", failing_swap)
    code, _, err = run(capsys, "--apply", "--host", "claude", "--source", source)
    monkeypatch.setattr(install.os, "rename", real_rename)
    assert code == 1 and "folder in use" in err
    assert {p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob("*") if p.is_file()} == before


def test_requests_that_are_refused(source, home, capsys):
    code, _, err = run(capsys, "--check", "--host", "claude", "--source", source, "--skills", "alpha,nope")
    assert code == 1 and "not a skill of this checkout: nope" in err
    code, _, err = run(capsys, "--check", "--dest", source, "--source", source)
    assert code == 1 and "own skills folder" in err
    code, _, err = run(capsys, "--check", "--host", "grok", "--source", source)
    assert code == 1 and "missing: alpha/SKILL.md" in err  # nothing installed yet is drift too


def test_installs_this_checkout_into_a_temp_folder(tmp_path, capsys):
    dest = tmp_path / "skills"
    code, summary, err = run(capsys, "--apply", "--dest", dest)
    assert code == 0, err
    names = sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())
    assert summary["skills"] == names
    assert not list(dest.rglob("__pycache__")) and not list(dest.rglob("*.pyc"))
    assert run(capsys, "--check", "--dest", dest)[0] == 0
    report = forge_doctor.diagnose(skills_root=dest, project_dir=tmp_path, run_versions=False, console_encoding="utf-8")
    assert next(c for c in report["checks"] if c["id"] == "skills.install")["status"] == "OK"
    assert next(c for c in report["checks"] if c["id"] == "skills.vendored")["status"] == "OK"
