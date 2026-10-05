#!/usr/bin/env python3
"""Install the Agent Sprite Forge skills into an agent's skills folder, or check an install for drift.

  python tools/install_skills.py --check --host claude      report drift; exit 1 if any (default mode)
  python tools/install_skills.py --apply --host claude      back up, then replace the installed skills
  python tools/install_skills.py --apply --dest .claude/skills --skills generate2dsprite,generate2dmap

Hosts: claude (~/.claude/skills), codex ($CODEX_HOME/skills, default ~/.codex/skills), grok
($GROK_HOME/skills, default ~/.grok/skills) and agents (~/.agents/skills); --dest names any
other skills folder. Only the skill folders of this checkout are managed (those with a
SKILL.md); other skills in the destination are never touched.

Copied: every file of each skill except __pycache__, *.pyc/*.pyo, dot files and OS junk,
so a working tree never ships bytecode. --apply first copies each installed skill folder it
will replace to a backup folder beside the skills folder (--backup-dir to choose; it must not
exist), stages the new copies beside the skills folder, swaps them in and writes
<skills folder>/.agent-sprite-forge.install.json (every file with its sha256, the source
commit). Any failure restores the previous folders and writes no manifest.

--check compares the checkout with the installed copies: missing, changed (marked "edited
locally" when the installed file no longer matches the manifest, "outdated" when the
checkout moved on) and extra files. What an install never ships is never drift either:
__pycache__ and bytecode that running an installed skill writes, dot files and OS junk
are skipped on both sides (D24). forge_doctor.py reads the same manifest to report local
drift with the same rule. Stdlib only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

TOOL = "install_skills/1.0"
MANIFEST = ".agent-sprite-forge.install.json"
MANIFEST_SCHEMA = "agent-sprite-forge.install.v1"
SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", "node_modules"})
SKIP_FILES = frozenset({"Thumbs.db", "desktop.ini"})
SKIP_SUFFIXES = frozenset({".pyc", ".pyo"})
REPO = Path(__file__).resolve().parents[1]


class InstallError(Exception):
    """A request this tool refuses; nothing was changed."""


def _local_utf8_stdio() -> None:
    """Never crash on an unencodable character; this tool itself prints ASCII."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def shipped(relative: Path) -> bool:
    """Whether a file inside a skill folder belongs in an install."""
    if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
        return False
    name = relative.name
    return not (name.startswith(".") or name in SKIP_FILES or Path(name).suffix.lower() in SKIP_SUFFIXES)


def skill_files(folder: Path, *, everything: bool = False) -> dict[str, str]:
    """{posix path relative to the skills root: sha256} for one skill folder."""
    files = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if path.is_file() and (everything or shipped(relative)):
            files[(Path(folder.name) / relative).as_posix()] = sha256_file(path)
    return files


def source_skills(source: Path, wanted: list[str] | None) -> list[str]:
    available = sorted(p.name for p in source.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())
    if not available:
        raise InstallError(f"no skill folders with a SKILL.md in {source}")
    if wanted is None:
        return available
    unknown = sorted(set(wanted) - set(available))
    if unknown:
        raise InstallError(f"not a skill of this checkout: {', '.join(unknown)} (have {', '.join(available)})")
    return sorted(set(wanted))


def destination(args: argparse.Namespace) -> Path:
    if args.dest:
        return Path(args.dest).expanduser()
    home = Path.home()
    roots = {"claude": home / ".claude", "agents": home / ".agents",
             "codex": Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser(),
             "grok": Path(os.environ.get("GROK_HOME") or home / ".grok").expanduser()}
    return roots[args.host] / "skills"


def read_manifest(dest: Path) -> dict | None:
    try:
        data = json.loads((dest / MANIFEST).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("files"), list) else None


def source_commit(source: Path) -> dict:
    """The checkout's commit and whether its skills differ from it (None when git is unavailable)."""
    def git(*command: str) -> str | None:
        try:
            result = subprocess.run(["git", *command], cwd=source, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--", ".") if commit else None
    return {"commit": commit, "dirty": None if status is None else bool(status)}


def drift(source: Path, dest: Path, skills: list[str]) -> list[tuple[str, str]]:
    """[(kind, path)] where kind is missing, changed (edited locally), changed (outdated),
    changed or extra; an empty list means the install matches the checkout. Installed files that
    an install never ships (shipped() is false: __pycache__, bytecode, dot files, OS junk) are not
    extras: running an installed skill writes them (D24)."""
    manifest = read_manifest(dest)
    installed_as = {item["path"]: item.get("sha256") for item in manifest["files"]} if manifest else {}
    problems = []
    for skill in skills:
        wanted = skill_files(source / skill)
        present = skill_files(dest / skill) if (dest / skill).is_dir() else {}
        for path, digest in wanted.items():
            if path not in present:
                problems.append(("missing", path))
            elif present[path] != digest:
                recorded = installed_as.get(path)
                kind = ("changed (edited locally)" if recorded and recorded != present[path]
                        else "changed (outdated)" if recorded else "changed")
                problems.append((kind, path))
        problems += [("extra", path) for path in present if path not in wanted]
    return problems


def _copy_verified(source: Path, target: Path, digest: str) -> None:
    """Exclusive copy with fsync; the copy must hash to the source's sha256."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as reader, open(target, "xb") as writer:
        shutil.copyfileobj(reader, writer, 1 << 20)
        writer.flush()
        os.fsync(writer.fileno())
    if sha256_file(target) != digest:
        raise InstallError(f"copy of {target.name} failed sha256 verification")


def _write_manifest(dest: Path, data: dict) -> None:
    temporary = dest / f".{MANIFEST}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, dest / MANIFEST)
    finally:
        temporary.unlink(missing_ok=True)


def apply(source: Path, dest: Path, skills: list[str], backup_dir: Path | None) -> dict:
    """Back up, stage, swap in, then write the manifest; any failure restores the old folders."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    existing = [s for s in skills if (dest / s).exists()]
    backup = backup_dir or dest.parent / f"{dest.name}.asf-backup-{stamp}"
    if existing and os.path.lexists(backup):
        raise InstallError(f"the backup folder {backup} already exists; choose another --backup-dir")
    plan = {skill: skill_files(source / skill) for skill in skills}
    dest.mkdir(parents=True, exist_ok=True)
    if existing:
        backup.mkdir(parents=True)
        for skill in existing:
            shutil.copytree(dest / skill, backup / skill, symlinks=True)
    stage = dest.parent / f".{dest.name}.asf-stage-{uuid.uuid4().hex[:8]}"
    stage.mkdir()
    swapped: list[str] = []
    try:
        for skill, files in plan.items():
            for path, digest in files.items():
                _copy_verified(source / path, stage / "new" / path, digest)
        (stage / "old").mkdir()
        for skill in skills:
            if (dest / skill).exists():
                os.rename(dest / skill, stage / "old" / skill)
            swapped.append(skill)
            os.rename(stage / "new" / skill, dest / skill)
        manifest = {"schema": MANIFEST_SCHEMA, "tool": TOOL,
                    "installedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": source_commit(source), "skills": skills,
                    "files": [{"path": path, "sha256": digest, "bytes": (source / path).stat().st_size}
                              for files in plan.values() for path, digest in files.items()],
                    "backup": backup.name if existing else None}
        _write_manifest(dest, manifest)
    except BaseException:
        for skill in reversed(swapped):  # put every replaced folder back
            if (dest / skill).exists():
                os.rename(dest / skill, stage / "new" / skill)
            if (stage / "old" / skill).exists():
                os.rename(stage / "old" / skill, dest / skill)
        if existing:  # restored, so the backup is redundant; if a restore failed it stays as the safety copy
            shutil.rmtree(backup, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"status": "applied", "dest": str(dest), "skills": skills, "files": len(manifest["files"]),
            "backup": str(backup) if existing else None, "manifest": str(dest / MANIFEST)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report drift and exit 1 if there is any (default)")
    mode.add_argument("--apply", action="store_true", help="back up, then install the checkout's skills")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--host", choices=("claude", "codex", "grok", "agents"), help="install into that host's skills folder")
    where.add_argument("--dest", help="any other skills folder (for example a project's .claude/skills)")
    parser.add_argument("--skills", help="comma list of skill names (default: every skill of the checkout)")
    parser.add_argument("--backup-dir", type=Path, help="new folder for the replaced copies "
                        "(default: <skills folder>.asf-backup-<UTC time> beside it)")
    parser.add_argument("--source", type=Path, default=REPO / "skills", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    _local_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        source = args.source.resolve()
        skills = source_skills(source, [s.strip() for s in args.skills.split(",") if s.strip()] if args.skills else None)
        dest = destination(args)
        if dest.resolve() == source:
            raise InstallError("the destination is this checkout's own skills folder")
        if args.apply:
            if args.backup_dir is not None and os.path.lexists(args.backup_dir):
                raise InstallError(f"the backup folder {args.backup_dir} already exists; choose another --backup-dir")
            summary = apply(source, dest, skills, args.backup_dir)
        else:
            problems = drift(source, dest, skills)
            for kind, path in problems:
                print(f"{kind}: {path}", file=sys.stderr)
            if problems:
                print(f"error: {len(problems)} file(s) differ from this checkout; run with --apply to reinstall "
                      "(the old copies are backed up first)", file=sys.stderr)
                return 1
            summary = {"status": "in-sync", "dest": str(dest), "skills": skills,
                       "files": sum(len(skill_files(source / s)) for s in skills)}
    except (InstallError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001  (D27: tracebacks are never user-facing)
        print(f"error: internal error ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
