#!/usr/bin/env python3
"""Index everything Agent Sprite Forge made under one folder (forge.asset_index.v1).

  python "<skill-dir>/scripts/forge_index.py" scan <dir> [--output <new file>]

scan walks <dir> and prints ONE ASCII JSON line, the index (shared/schemas/host.schema.json
asset_index_v1), for a host such as Agent Game Forge to list a project's art without knowing
every tool's files:

  masters  master.json (generate2dsprite.master.v1, master_still.py approve)
  sets     set_plan.json + set_state.json (video2dsprite sprite_set.py): per action its status,
           takes, chosen take (or usable window), the agent's accept, and the package of the
           chosen take (packageTake, packageTag) with its animation.json reviewStatus, poster and
           verify result. A package of an earlier take (after a retake, or an accept of another
           take or window) is never reported as the action's package: it is previousPackage.
  fx       fx.json: route A (generate2dsprite.pixelfx.v1, fx_pixelfx.py render) and route B
           (generate2dsprite.hibit_fx.v1, hibit_fx.py pixelize)
  plates   plate.json (generate2dmap.hibit_plate.v1, hibit_plate.py plate)
  maps     map bundles (generate2dmap.map_bundle.v1 / v2, any *.json that declares one)

Every path is POSIX and relative to the scanned folder ("root", absolute) and stays inside it:
a document path that leads outside (a '..' or a link) is left out with a warning. A folder that
holds one of these documents is not searched further (a set's master copies, takes and packages
are reported through the set). Hidden folders (staging folders such as .x.stage-1a2b3c4d,
.forge, .git), __pycache__, node_modules and the *.orphan-N / *.failed-* folders the tools set
aside are skipped. A set is read holding its set_state.lock (the lock sprite_set.py writes
under) when that file exists, waiting up to 10 s; the lock file is never created. A document
that cannot be read becomes a warning; the scan never writes into <dir>. --output also writes
the index to a new file (never replaced). Stdlib only, read-only.

Exit codes: 0 indexed (warnings included); 1 error (error: ... on stderr); 2 usage.

This file is vendored byte-for-byte into the skills listed in shared/VENDORED.json; edit only
shared/forge_index.py, then run python tools/vendor_sync.py --write and --check.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

INDEX_SCHEMA = "forge.asset_index.v1"
FORGE_PACKAGE_VERSION = "0.6.0"   # the package release (D29); a test keeps it equal to forge_core's
TOOL = {"name": "forge_index", "version": FORGE_PACKAGE_VERSION}
MASTER_SCHEMA = "generate2dsprite.master.v1"
PLAN_SCHEMA = "video2dsprite.sprite_set_plan.v1"
STATE_SCHEMA = "video2dsprite.sprite_set_state.v1"
FX_SCHEMAS = {"generate2dsprite.pixelfx.v1": ("A", "pixelfx"), "generate2dsprite.hibit_fx.v1": ("B", "image")}
PLATE_SCHEMA = "generate2dmap.hibit_plate.v1"
MAP_SCHEMAS = ("generate2dmap.map_bundle.v1", "generate2dmap.map_bundle.v2")
DOCUMENTS = ("master.json", "set_plan.json", "fx.json", "plate.json")   # a folder holding one is a leaf
SKIP_DIRS = frozenset({"__pycache__", "node_modules"})
SET_ASIDE = re.compile(r"\.orphan-\d+$|\.failed-[A-Za-z0-9_.:-]+$")
MAP_SNIFF_BYTES = 4096
MAX_MAP_BYTES = 16 << 20
QA_STATUSES = ("pass", "fail", "warn", "needs-visual-review")
LOCK_FILE = "set_state.lock"   # sprite_set.py's lock (forge_core.file_lock: byte 0, msvcrt or flock)
LOCK_WAIT_S = 10.0
READ_RETRY_S = 1.0   # a Windows read can meet a file in the middle of its atomic replace


class IndexError_(ValueError):
    """A scan that cannot run (the folder is missing); the CLI prints it as error: ..."""


def _utc() -> str:
    moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _ascii(text: object) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _read(path: Path) -> Any:
    deadline = time.monotonic() + READ_RETRY_S
    while True:
        try:
            data = path.read_bytes()
            break
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
    return json.loads(data.decode("utf-8-sig"))


def _rel(path: Path, root: Path) -> str:
    try:
        text = Path(os.path.relpath(path, root)).as_posix()
    except ValueError:   # another drive: never expected inside one scan
        text = path.as_posix()
    return text


def _within(path: Path, root: Path) -> bool:
    candidate, base = os.path.normcase(str(path)), os.path.normcase(str(root))
    return candidate == base or candidate.startswith(base.rstrip(os.sep) + os.sep)


def _inside(folder: Path, value: Any, root: Path, warnings: list[str]) -> Path | None:
    """``folder / value`` for a relative POSIX path from a document, resolved, else None.

    A path that resolves outside the scanned folder ``root`` (``..`` segments, a link that leaves it) is
    left out with a warning, so no index entry points outside root. A host still checks containment.
    """
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value \
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        return None
    try:
        path = (folder / value).resolve()
    except (OSError, RuntimeError):   # a link loop or an unreadable link
        path = None
    if path is None or not _within(path, root):
        warnings.append(_ascii(f"{_rel(folder, root)}: {value[:120]} leads outside the scanned folder; left out"))
        return None
    return path


def _existing(folder: Path, value: Any, root: Path, warnings: list[str]) -> str | None:
    path = _inside(folder, value, root, warnings)
    return _rel(path, root) if path is not None and path.exists() else None


# --------------------------------------------------------------------------- the set lock (read side)

if os.name == "nt":
    import msvcrt

    def _try_lock(handle: Any) -> bool:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(handle: Any) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def set_lock(folder: Path, root: Path, warnings: list[str], wait: float | None = None) -> Iterator[bool]:
    """Hold the set's set_state.lock while the set is read, when the file exists (it is never created).

    The same OS lock as sprite_set.py's forge_core.file_lock (byte 0 of the lock file, msvcrt on Windows,
    flock elsewhere), so a scan never reads set_state.json or a package while a run, review, accept or
    retake writes them, and never holds them open while a writer replaces them. After ``wait`` seconds
    (default LOCK_WAIT_S) the set is read without the lock and a warning says so. Yields whether the lock
    is held.
    """
    wait = LOCK_WAIT_S if wait is None else wait
    try:
        handle = open(folder / LOCK_FILE, "rb")
    except OSError:   # no lock file (a set that never ran) or an unreadable one
        yield False
        return
    with handle:
        deadline = time.monotonic() + max(0.0, float(wait))
        locked = _try_lock(handle)
        while not locked and time.monotonic() < deadline:
            time.sleep(0.05)
            locked = _try_lock(handle)
        if not locked:
            warnings.append(_ascii(f"{_rel(folder / LOCK_FILE, root)}: held by another process for {wait:g} s; "
                                   "the set was read without it"))
        try:
            yield locked
        finally:
            if locked:
                _unlock(handle)


def _qa_status(document: dict) -> str:
    status = (document.get("qa") or {}).get("status") if isinstance(document.get("qa"), dict) else None
    return status if status in QA_STATUSES else "needs-visual-review"


# --------------------------------------------------------------------------- walking

def walk(root: Path) -> Iterator[tuple[Path, list[str]]]:
    """(folder, file names) for every folder the scan reads; leaves (a folder with a document) are not entered."""
    for current, dirs, files in os.walk(root):
        folder = Path(current)
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and name not in SKIP_DIRS
                         and not SET_ASIDE.search(name))
        names = sorted(files)
        if any(name in names for name in DOCUMENTS):
            dirs[:] = []
        yield folder, names


# --------------------------------------------------------------------------- entries

def master_entry(folder: Path, root: Path, document: dict, warnings: list[str]) -> dict:
    files = document.get("files") if isinstance(document.get("files"), dict) else {}
    master = (files.get("master") or {}).get("path")
    cutout = (files.get("cutout") or {}).get("path")
    return {"id": _rel(folder, root), "name": str(document.get("name") or folder.name),
            "path": _rel(folder, root), "metadata": _rel(folder / "master.json", root),
            "image": _existing(folder, master, root, warnings) or _rel(folder / "master.png", root),
            "cutout": _existing(folder, cutout, root, warnings), "class": str(document.get("class") or ""),
            "finish": str(document.get("finish") or ""), "facing": str(document.get("facing") or ""),
            "route": document.get("route") if isinstance(document.get("route"), str) else None,
            "status": "approved"}


def _manifest_facts(package: Path | None, root: Path, warnings: list[str]) -> dict:
    facts = {"manifest": None, "poster": None, "reviewStatus": None}
    if package is None or not (package / "animation.json").is_file():
        return facts
    facts["manifest"] = _rel(package / "animation.json", root)
    with contextlib.suppress(OSError, ValueError):
        manifest = _read(package / "animation.json")
        if isinstance(manifest, dict):
            facts["reviewStatus"] = manifest.get("reviewStatus") if isinstance(manifest.get("reviewStatus"), str) \
                else None
            poster = (manifest.get("poster") or {}).get("file") if isinstance(manifest.get("poster"), dict) else None
            facts["poster"] = _existing(package, poster, root, warnings)
    return facts


def _window(value: Any) -> list[int] | None:
    return [int(value[0]), int(value[1])] if isinstance(value, list) and len(value) == 2 and \
        all(isinstance(item, int) and not isinstance(item, bool) for item in value) else None


def _take_package(folder: Path, root: Path, state: dict, chosen: dict | None, warnings: list[str]) -> Path | None:
    """The chosen take's package folder from its stages (package-r2 first), when output names none."""
    if not chosen:
        return None
    tag = chosen.get("tag") or ""
    for record in state.get("takes") or []:
        if isinstance(record, dict) and record.get("take") == chosen.get("take"):
            stages = record.get("stages") if isinstance(record.get("stages"), dict) else {}
            for name in ("package-r2" + tag, "package" + tag):
                stage = stages.get(name)
                if not isinstance(stage, dict) or not stage.get("dir"):
                    continue
                path = _inside(folder, stage.get("dir"), root, warnings)
                if path is not None and path.is_dir():
                    return path
    return None


def _same_take(output: dict | None, chosen: dict | None) -> bool:
    """True when the output sprite_set.py recorded is the package of the chosen take and window (tag)."""
    return bool(output and chosen and output.get("take") == chosen.get("take")
                and str(output.get("tag") or "") == str(chosen.get("tag") or ""))


def _read_state(folder: Path, root: Path, warnings: list[str]) -> dict:
    state_path = folder / "set_state.json"
    if not state_path.is_file():
        return {}
    try:
        loaded = _read(state_path)
    except (OSError, ValueError) as error:
        warnings.append(_ascii(f"{_rel(state_path, root)}: unreadable ({type(error).__name__})"))
        return {}
    if isinstance(loaded, dict) and loaded.get("schema") == STATE_SCHEMA:
        return loaded
    warnings.append(_ascii(f"{_rel(state_path, root)}: not a {STATE_SCHEMA} document"))
    return {}


def action_entry(folder: Path, root: Path, entry: dict, st: dict, warnings: list[str]) -> dict:
    """One planned action: its state, and the package only when it holds the chosen take and window."""
    ident = str(entry["id"])
    chosen = st.get("chosen") if isinstance(st.get("chosen"), dict) else None
    accepted = st.get("accepted") if isinstance(st.get("accepted"), dict) else None
    output = st.get("output") if isinstance(st.get("output"), dict) else None
    recorded = _inside(folder, output.get("package"), root, warnings) if output and output.get("package") else None
    recorded = recorded if recorded is not None and recorded.is_dir() else None
    package = previous = None
    if recorded is not None and _same_take(output, chosen):
        package = recorded
    else:
        previous = recorded   # a retake, or an accept of another take or window, left this package behind
        package = _take_package(folder, root, st, chosen, warnings)   # the chosen take packaged, output not saved
    facts = _manifest_facts(package, root, warnings)
    return {
        "id": ident, "action": str(entry.get("action") or ident), "view": str(entry.get("view") or "side"),
        "kind": entry.get("kind") if entry.get("kind") in ("loop", "oneshot") else "loop",
        "status": str(st.get("status") or "planned"),
        "takes": len(st.get("takes") or []) if isinstance(st.get("takes"), list) else 0,
        "chosen": None if chosen is None else {"take": chosen.get("take"), "window": _window(chosen.get("window")),
                                               "tag": str(chosen.get("tag") or "")},
        "accepted": None if accepted is None else {"take": accepted.get("take"),
                                                   "window": _window(accepted.get("window")),
                                                   "at": accepted.get("at")},
        "package": _rel(package, root) if package is not None else None,
        "packageTake": chosen.get("take") if package is not None and chosen else None,
        "packageTag": str(chosen.get("tag") or "") if package is not None and chosen else None,
        **facts,
        "verify": output.get("verify") if package is not None and package == recorded
        and isinstance(output.get("verify"), str) else None,
        "previousPackage": _rel(previous, root) if previous is not None and previous != package else None,
    }


def set_entry(folder: Path, root: Path, plan: dict, warnings: list[str]) -> dict:
    state_path = folder / "set_state.json"
    planned = [entry for entry in plan.get("actions") or [] if isinstance(entry, dict) and entry.get("id")]
    with set_lock(folder, root, warnings):   # set_state.json and the packages as one consistent moment
        state = _read_state(folder, root, warnings)
        states = state.get("actions") if isinstance(state.get("actions"), dict) else {}
        actions = []
        for entry in planned:
            st = states.get(str(entry["id"]))
            actions.append(action_entry(folder, root, entry, st if isinstance(st, dict) else {}, warnings))
    statuses = {item["status"] for item in actions}
    if not state:
        status = "planned"
    elif statuses <= {"done"}:
        status = "complete"
    elif statuses <= {"done", "done-warn"}:
        status = "complete-with-warnings"
    else:
        status = "incomplete"
    finish = plan.get("finish") if isinstance(plan.get("finish"), dict) else {}
    masters = plan.get("masters") if isinstance(plan.get("masters"), dict) else {}
    first = masters.get("side") or next(iter(masters.values()), None) if masters else None
    master_png = _existing(folder, ((first or {}).get("png") or {}).get("path"), root, warnings) \
        if isinstance(first, dict) else None
    return {"id": _rel(folder, root), "name": str(plan.get("name") or folder.name), "path": _rel(folder, root),
            "plan": _rel(folder / "set_plan.json", root),
            "state": _rel(state_path, root) if state else None, "class": str(plan.get("class") or ""),
            "finish": finish.get("mode") if finish.get("mode") in ("hd", "pixel", "hibit") else "hd",
            "targetHeight": finish.get("targetHeight") if isinstance(finish.get("targetHeight"), int) else None,
            "master": master_png, "status": status, "actions": actions}


def fx_entry(folder: Path, root: Path, document: dict, warnings: list[str]) -> dict:
    route, route_name = FX_SCHEMAS[document["schema"]]
    effect = document.get("effect") if isinstance(document.get("effect"), dict) else {}
    frames = effect.get("frames") if isinstance(effect.get("frames"), int) else 0
    size = effect.get("frameSize") if isinstance(effect.get("frameSize"), list) else None
    if route == "A":
        frame_ms = round(1000.0 / effect["fps"], 3) if isinstance(effect.get("fps"), (int, float)) and effect["fps"] \
            else None
        name = effect.get("id") or folder.name
    else:
        frame_ms = effect.get("frameMs") if isinstance(effect.get("frameMs"), (int, float)) else None
        name = effect.get("name") or folder.name
    preview = next((name_ for name_ in ("preview.gif",) if (folder / name_).is_file()), None)
    return {"id": _rel(folder, root), "name": str(name), "path": _rel(folder, root),
            "metadata": _rel(folder / "fx.json", root), "route": route, "routeName": route_name,
            "effect": effect.get("id") if route == "A" else effect.get("name"),
            "frames": max(1, int(frames or 1)), "frameSize": size if size and len(size) == 2 else [1, 1],
            "frameMs": frame_ms, "loop": effect.get("loop") if isinstance(effect.get("loop"), bool) else None,
            "anchor": (effect.get("anchor") if route == "B" else (document.get("engine") or {}).get("pivot")),
            "sheet": _rel(folder / "sheet.png", root),
            "sheetJson": _existing(folder, "sheet.json", root, warnings),
            "preview": _rel(folder / preview, root) if preview else None,
            "credits": _existing(folder, "CREDITS.txt", root, warnings), "status": _qa_status(document)}


def plate_entry(folder: Path, root: Path, document: dict, warnings: list[str]) -> dict:
    size = document.get("size") if isinstance(document.get("size"), list) and len(document["size"]) == 2 else [1, 1]
    preview = next(iter(sorted(folder.glob("plate-x*.png"))), None)
    return {"id": _rel(folder, root), "name": folder.name, "path": _rel(folder, root),
            "metadata": _rel(folder / "plate.json", root), "image": _rel(folder / "plate.png", root),
            "preview": _rel(preview, root) if preview else None,
            "palette": _existing(folder, "palette.json", root, warnings),
            "size": size, "colors": document.get("colors") if isinstance(document.get("colors"), int) else 1,
            "status": _qa_status(document)}


def map_entry(path: Path, root: Path, document: dict) -> dict:
    world = document.get("world") if isinstance(document.get("world"), dict) else None
    stem = re.sub(r"(\.map-bundle|-bundle|\.bundle)$", "", path.stem)
    name = path.parent.name if path.name == "map-bundle.json" else stem
    return {"id": _rel(path, root), "name": str(document.get("name") or document.get("id") or name),
            "path": _rel(path, root), "schemaVersion": document["schema"],
            "world": None if world is None else {"width": world.get("width"), "height": world.get("height")},
            "layers": len(document.get("layers") or []) if isinstance(document.get("layers"), list) else 0,
            "objects": len(document.get("objects") or []) if isinstance(document.get("objects"), list) else 0}


def _sniff_map(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_MAP_BYTES:
            return False
        with open(path, "rb") as stream:
            head = stream.read(MAP_SNIFF_BYTES)
    except OSError:
        return False
    return b"generate2dmap.map_bundle.v" in head


# --------------------------------------------------------------------------- scan

def scan(directory: str | os.PathLike) -> dict:
    """The forge.asset_index.v1 document of ``directory`` (read-only)."""
    root = Path(directory)
    if not root.is_dir():
        raise IndexError_(f"not a folder: {root}")
    root = root.resolve()
    index: dict[str, Any] = {"schema": INDEX_SCHEMA, "tool": dict(TOOL), "createdAt": _utc(),
                             "root": root.as_posix(), "masters": [], "sets": [], "fx": [], "plates": [], "maps": [],
                             "warnings": []}
    warnings: list[str] = index["warnings"]

    def load(path: Path) -> dict | None:
        try:
            document = _read(path)
        except (OSError, ValueError) as error:
            warnings.append(_ascii(f"{_rel(path, root)}: unreadable ({type(error).__name__})"))
            return None
        return document if isinstance(document, dict) else None

    for folder, names in walk(root):
        documents = {name: load(folder / name) for name in DOCUMENTS if name in names}
        plan = documents.get("set_plan.json")
        if plan is not None and plan.get("schema") == PLAN_SCHEMA:
            index["sets"].append(set_entry(folder, root, plan, warnings))
        master = documents.get("master.json")
        if master is not None and master.get("schema") == MASTER_SCHEMA:
            index["masters"].append(master_entry(folder, root, master, warnings))
        fx = documents.get("fx.json")
        if fx is not None and fx.get("schema") in FX_SCHEMAS:
            index["fx"].append(fx_entry(folder, root, fx, warnings))
        plate = documents.get("plate.json")
        if plate is not None and plate.get("schema") == PLATE_SCHEMA:
            index["plates"].append(plate_entry(folder, root, plate, warnings))
        for name in names:
            if not name.endswith(".json") or name in DOCUMENTS or name == "set_state.json":
                continue
            path = folder / name
            if not _sniff_map(path):
                continue
            document = load(path)
            if document is not None and document.get("schema") in MAP_SCHEMAS:
                index["maps"].append(map_entry(path, root, document))
    return index


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge_index.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    scan_parser = verbs.add_parser("scan", help="index the masters, sprite sets, FX, plates and maps under a folder",
                                   description="Print one JSON line: the forge.asset_index.v1 index of the folder.")
    scan_parser.add_argument("directory", help="folder to scan, such as <project>/art")
    scan_parser.add_argument("--output", help="also write the index to this new file (never replaced)")
    return parser


def _write_new(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    with open(path, "xb") as stream:
        stream.write(payload)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        index = scan(args.directory)
        if args.output:
            output = Path(args.output)
            if os.path.lexists(output):
                raise IndexError_(f"{output.name} already exists; --output writes a new file")
            _write_new(output, index)
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        print(_ascii(f"error: {error}"), file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001  (D27: one line, never a traceback)
        print(_ascii(f"error: internal error ({type(error).__name__}: {error})"), file=sys.stderr)
        return 1
    print(json.dumps(index, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
