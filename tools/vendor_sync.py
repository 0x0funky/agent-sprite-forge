#!/usr/bin/env python3
"""Keep the vendored copies of shared/ files identical to their canonicals.

Skills never import across skill folders, so every skill ships its own copy of
the shared modules and contract schemas. shared/VENDORED.json maps each
canonical file to those copies.

  python tools/vendor_sync.py --check    report drift; exit 1 if any (default)
  python tools/vendor_sync.py --write    copy each canonical over stale or missing copies

Text files are compared with CRLF normalised to LF, so Windows autocrlf and
Linux checkouts agree; --write copies exact bytes and never edits a canonical.
A canonical that does not exist yet is skipped unless --strict or
FORGE_VENDOR_STRICT=1 is set. A copy without its canonical, and a file inside
skills/ that carries a vendored file name without being listed, are errors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MANIFEST = PurePosixPath("shared/VENDORED.json")
TEXT_SUFFIXES = frozenset({".py", ".json", ".md", ".js", ".mjs", ".txt"})


class ManifestError(ValueError):
    """shared/VENDORED.json is unreadable, malformed or points outside its folders."""


@dataclass(frozen=True)
class Entry:
    canonical: str
    targets: tuple[str, ...]


@dataclass
class Report:
    drift: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, target, canonical); --write fixes these
    problems: list[str] = field(default_factory=list)  # --write cannot fix these
    pending: list[str] = field(default_factory=list)  # canonicals not created yet
    in_sync: int = 0


def _local_utf8_stdio() -> None:
    """Never crash on an unencodable character; this tool itself prints ASCII."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")


def _repo_path(text: object, role: str) -> PurePosixPath:
    if not isinstance(text, str) or not text or "\\" in text or ":" in text:
        raise ManifestError(f"{role} must be a repository-relative POSIX path: {text!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"{role} must stay inside the repository: {text!r}")
    return path


def load_manifest(root: Path) -> list[Entry]:
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ManifestError(f"cannot read {MANIFEST}: {error}") from None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not files:
        raise ManifestError(f"{MANIFEST} needs a nonempty 'files' list")
    entries: list[Entry] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("targets"), list):
            raise ManifestError("each manifest entry needs a canonical path and a targets list")
        canonical = _repo_path(item.get("canonical"), "canonical")
        if canonical.parts[0] != "shared" or str(canonical) in seen:
            raise ManifestError(f"canonical {canonical} must live under shared/ and be listed once")
        seen.add(str(canonical))
        targets = []
        for text in item["targets"]:
            target = _repo_path(text, "target")
            if target.parts[0] != "skills" or len(target.parts) < 4:
                raise ManifestError(f"target {target} must live inside a skill folder")
            if target.name != canonical.name:
                raise ManifestError(f"target {target} must keep the canonical file name {canonical.name}")
            if str(target) in seen:
                raise ManifestError(f"{target} is listed more than once")
            seen.add(str(target))
            targets.append(str(target))
        entries.append(Entry(str(canonical), tuple(targets)))
    return entries


def content_digest(path: Path) -> str:
    """sha256 of the file; text files hash with CRLF normalised to LF."""
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def unlisted_copies(root: Path, entries: list[Entry]) -> list[str]:
    """Files under skills/ named like a canonical but missing from the manifest."""
    names = {PurePosixPath(entry.canonical).name for entry in entries}
    listed = {target for entry in entries for target in entry.targets}
    skills = root / "skills"
    if not skills.is_dir():
        return []
    found = (path.relative_to(root).as_posix() for path in skills.rglob("*") if path.name in names and path.is_file())
    return sorted(path for path in found if path not in listed)


def inspect(root: Path, entries: list[Entry], strict: bool) -> Report:
    report = Report()
    for entry in entries:
        canonical = root / entry.canonical
        if not canonical.is_file():
            orphans = [target for target in entry.targets if (root / target).exists()]
            report.problems.extend(f"copy without canonical: {target} (missing {entry.canonical})" for target in orphans)
            if strict:
                report.problems.append(f"missing canonical: {entry.canonical} (strict mode)")
            elif not orphans:
                report.pending.append(entry.canonical)
            continue
        expected = content_digest(canonical)
        for target in entry.targets:
            path = root / target
            if not path.is_file():
                report.drift.append(("missing copy", target, entry.canonical))
            elif content_digest(path) != expected:
                report.drift.append(("stale copy", target, entry.canonical))
            else:
                report.in_sync += 1
    report.problems.extend(f"unlisted copy: {path} is not a target in {MANIFEST}" for path in unlisted_copies(root, entries))
    return report


def copy_atomically(source: Path, target: Path) -> None:
    """Replace target with the exact bytes of source; readers never see a partial file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    handle = tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".vendoring", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report drift and exit 1 if there is any (default)")
    mode.add_argument("--write", action="store_true", help="copy canonicals over stale or missing copies")
    parser.add_argument("--strict", action="store_true",
                        help="require every canonical to exist (same as FORGE_VENDOR_STRICT=1)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="repository root (default: the checkout containing this script)")
    return parser


def main(argv: list[str] | None = None) -> int:
    _local_utf8_stdio()
    args = build_parser().parse_args(argv)
    strict = args.strict or os.environ.get("FORGE_VENDOR_STRICT") == "1"
    root = args.root.resolve()
    try:
        entries = load_manifest(root)
        report = inspect(root, entries, strict)
        written = []
        if args.write and report.drift:
            for _, target, canonical in report.drift:
                copy_atomically(root / canonical, root / target)
                written.append(target)
            report = inspect(root, entries, strict)
    except (ManifestError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for kind, target, canonical in report.drift:
        print(f"{kind}: {target} (canonical {canonical})", file=sys.stderr)
    for problem in report.problems:
        print(problem, file=sys.stderr)
    if report.drift or report.problems:
        hint = "; run python tools/vendor_sync.py --write" if report.drift else ""
        print(f"error: {len(report.drift) + len(report.problems)} vendoring problem(s){hint}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "mode": "write" if args.write else "check", "strict": strict,
                      "copies_in_sync": report.in_sync, "written": written, "pending_canonicals": report.pending}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
