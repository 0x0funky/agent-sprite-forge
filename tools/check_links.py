"""Read-only local link checker for the repository's Markdown files.

Checks every *.md under the root (skipping .git, .tmp, outputs and caches):
  - Markdown inline links and images  [x](target) / ![x](target)
  - HTML src= / href= / srcset= attributes
  - reference-style definitions  [id]: target
  - #anchors against GitHub-style heading slugs
Links inside fenced code blocks are ignored. External URLs are not fetched.
Exit code 1 when any local target is missing.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote

SKIP = {".git", ".tmp", "outputs", "__pycache__", ".pytest_cache", "venv", ".venv",
        ".agents", ".forge", "node_modules", "handoff"}

MD_LINK = re.compile(r"!?\[(?:[^\]\[]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HTML_ATTR = re.compile(r"\b(?:src|href|srcset)\s*=\s*\"([^\"]+)\"")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(\S+)", re.M)
FENCE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)


def slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").strip().lower()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "-_ ":
            out.append("-" if ch == " " else ch)
        elif cat[0] in "LN" or cat in ("Mn", "Mc"):
            out.append(ch)
    return "".join(out)


def anchors(path: Path) -> set[str]:
    body = FENCE.sub("", path.read_text(encoding="utf-8", errors="replace"))
    seen: dict[str, int] = {}
    result = set()
    for _, title in HEADING.findall(body):
        s = slug(title)
        n = seen.get(s, 0)
        result.add(s if n == 0 else f"{s}-{n}")
        seen[s] = n + 1
    result.update(re.findall(r"<a\s+(?:name|id)=\"([^\"]+)\"", body))
    return result


def md_files(root: Path):
    for p in sorted(root.rglob("*.md")):
        if any(part in SKIP for part in p.relative_to(root).parts):
            continue
        yield p


def check(root: Path, files=None) -> list[tuple[Path, str, str]]:
    """Return (doc, target, reason) for every broken local link."""
    root = Path(root).resolve()
    broken = []
    cache: dict[Path, set[str]] = {}
    for doc in (files if files is not None else md_files(root)):
        text = FENCE.sub("", doc.read_text(encoding="utf-8", errors="replace"))
        text = INLINE_CODE.sub("", text)
        targets = MD_LINK.findall(text) + HTML_ATTR.findall(text) + REF_DEF.findall(text)
        for target in targets:
            for t in target.split(","):  # srcset
                t = t.strip().split(" ")[0]
                if not t or "://" in t or t.startswith(("mailto:", "data:", "{", "$")):
                    continue
                path_part, _, frag = t.partition("#")
                dest = doc if not path_part else (doc.parent / unquote(path_part))
                if not dest.exists():
                    broken.append((doc, t, "missing file"))
                    continue
                if frag and dest.suffix == ".md":
                    if dest not in cache:
                        cache[dest] = anchors(dest)
                    if frag not in cache[dest]:
                        broken.append((doc, t, "missing anchor"))
    return broken


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check local Markdown/HTML links and anchors (read-only).")
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]),
                        help="repository root to scan (default: this repo)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    broken = check(root)
    print(f"checked {sum(1 for _ in md_files(root))} markdown files, {len(broken)} broken")
    for doc, t, why in broken:
        line = f"BROKEN [{why}] {doc.relative_to(root).as_posix()} -> {t}"
        print(line.encode("ascii", "backslashreplace").decode("ascii"))
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
