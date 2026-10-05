"""Shared test helpers for the Agent Sprite Forge suite.

The public names below are frozen after Wave A; add new helpers to your own test
module instead of changing these. Test modules import this file directly
(``from forge_testutils import load_script``): pytest's default import mode and
``python -m unittest discover tests`` both put ``tests/`` on ``sys.path``.

Everything here is deterministic and offline. Optional tools are skipped with
``unittest.SkipTest``, which pytest and unittest both report as a skip.
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
SHARED_SCHEMAS_DIR = REPO_ROOT / "shared" / "schemas"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
REAL_FIXTURES_DIR = FIXTURES_DIR / "real"

__all__ = [
    "REPO_ROOT", "SKILLS_DIR", "SHARED_SCHEMAS_DIR", "FIXTURES_DIR", "REAL_FIXTURES_DIR",
    "script_path", "load_script", "run_cli", "assert_cli_help", "make_magenta_sheet",
    "require_ffmpeg", "require_node", "require_resvg", "real_fixture",
    "contract_validator", "contract_errors", "assert_valid_contract",
]


# --------------------------------------------------------------------------- scripts and CLIs

def script_path(skill: str, name: str) -> Path:
    """Return ``skills/<skill>/scripts/<name>.py``; ``name`` may omit the ``.py`` suffix."""
    filename = name if name.endswith(".py") else f"{name}.py"
    path = SKILLS_DIR / skill / "scripts" / filename
    if not path.is_file():
        raise FileNotFoundError(f"No script {filename} in skill {skill}: {path}")
    return path


def load_script(skill: str, name: str, *, fresh: bool = False) -> ModuleType:
    """Import a skill script by path, the way the skills import their own siblings.

    The module is registered in ``sys.modules`` as ``forge_<skill>_<name>`` and reused
    by later calls, like a normal import. ``fresh=True`` executes a new, unregistered
    copy, for tests that depend on import-time state such as environment variables.
    """
    path = script_path(skill, name)
    module_name = f"forge_{skill}_{path.stem}"
    if not fresh and module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    if not fresh:
        sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if not fresh:
            sys.modules.pop(module_name, None)
        raise
    return module


def run_cli(argv: Sequence[str | os.PathLike], encoding: str | None = None, *,
            cwd: str | os.PathLike | None = None, env: Mapping[str, str] | None = None,
            timeout: float = 300.0, input_text: str | None = None) -> subprocess.CompletedProcess:
    """Run ``python <argv...>`` in a subprocess and return the completed process.

    ``argv`` starts with the script path (see ``script_path``). ``encoding`` sets the
    child's PYTHONIOENCODING (for example ``"cp1252"`` or ``"cp950"``; default
    UTF-8), so a console that cannot encode the output makes the child fail as it
    would for a user. Output is decoded with that same encoding, replacing
    undecodable bytes. The child never writes ``__pycache__`` into skill folders.
    ``env`` entries override the inherited environment.
    """
    child_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": encoding or "utf-8"}
    if env:
        child_env.update(env)
    codec = child_env["PYTHONIOENCODING"].split(":", 1)[0] or "utf-8"
    completed = subprocess.run(
        [sys.executable, *(os.fspath(part) for part in argv)],
        input=None if input_text is None else input_text.encode(codec),
        capture_output=True, cwd=cwd, env=child_env, timeout=timeout, check=False)
    return subprocess.CompletedProcess(
        completed.args, completed.returncode,
        completed.stdout.decode(codec, errors="replace"),
        completed.stderr.decode(codec, errors="replace"))


def assert_cli_help(skill: str, name: str, encodings: Sequence[str] = ("cp1252", "cp950")) -> None:
    """Assert that ``--help`` exits 0 with ASCII-only output under each console encoding."""
    path = script_path(skill, name)
    for encoding in encodings:
        result = run_cli([path, "--help"], encoding, timeout=120)
        if result.returncode != 0:
            raise AssertionError(f"{path.name} --help exited {result.returncode} under {encoding}:\n{result.stderr}")
        if not result.stdout.strip():
            raise AssertionError(f"{path.name} --help printed nothing under {encoding}")
        if not result.stdout.isascii():
            raise AssertionError(f"{path.name} --help output is not ASCII under {encoding}")


# --------------------------------------------------------------------------- synthetic images

def make_magenta_sheet(rows: int = 2, cols: int = 2, cell: int | tuple[int, int] = 64, *,
                       key: tuple[int, int, int] = (255, 0, 255),
                       color: tuple[int, int, int] = (40, 160, 210),
                       outline: tuple[int, int, int] = (20, 30, 40),
                       margin: int = 12, fringe: bool = False, spill_px: int = 0,
                       return_boxes: bool = False):
    """Build a deterministic, fully opaque chroma-key sheet with one subject per cell.

    Each subject is an outlined rectangle body with a head block, inset ``margin``
    (at least 4) px from its cell; subject ``i`` (row-major) is shifted right by
    ``i % 3`` px so the frames differ. ``fringe`` adds a 1 px ring mixed 50/50 with
    the key colour, like compression contamination. ``spill_px`` extends the first
    subject across its cell's right border by that many pixels (a cross-cell tail
    that ends in the next cell's empty margin, so it never touches that subject).

    Returns an RGBA image, or ``(image, boxes)`` when ``return_boxes`` is true, where
    ``boxes`` lists each subject's ``[x0, y0, x1, y1)`` box (fringe excluded).
    """
    cell_w, cell_h = (cell, cell) if isinstance(cell, int) else cell
    if rows < 1 or cols < 1 or margin < 4 or min(cell_w, cell_h) - 2 * margin < 8:
        raise ValueError("need rows, cols >= 1, margin >= 4 and room for an 8 px subject")
    if spill_px and (cols < 2 or not 0 < spill_px <= margin - 2):
        raise ValueError("spill_px needs a second column and must end in that cell's margin (<= margin - 2)")
    pixels = np.empty((rows * cell_h, cols * cell_w, 4), np.uint8)
    pixels[...] = (*key, 255)
    key_rgb = np.array(key, np.uint16)
    boxes = []
    for index in range(rows * cols):
        row, col = divmod(index, cols)
        x0 = col * cell_w + margin + index % 3
        y0 = row * cell_h + margin
        x1 = (col + 1) * cell_w - margin + index % 3
        y1 = (row + 1) * cell_h - margin
        if index == 0 and spill_px:
            x1 = cell_w + spill_px
        head = (x1 - x0) // 3
        if fringe:
            ring = pixels[y0 - 1:y1 + 1, x0 - 1:x1 + 1, :3]
            ring[...] = ((key_rgb + np.array(outline, np.uint16)) // 2).astype(np.uint8)
        pixels[y0:y1, x0:x1, :3] = outline
        pixels[y0 + 1:y1 - 1, x0 + 1:x1 - 1, :3] = color
        pixels[y0 + 3:y0 + 3 + head, x0 + head:x0 + 2 * head, :3] = outline
        boxes.append([x0, y0, x1, y1])
    image = Image.fromarray(pixels)  # uint8 HxWx4 is RGBA in every supported Pillow
    return (image, boxes) if return_boxes else image


# --------------------------------------------------------------------------- optional tools

def require_ffmpeg(*, ffprobe: bool = True) -> str:
    """Return the ffmpeg path, or skip the test when ffmpeg (or ffprobe) is missing."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or (ffprobe and not shutil.which("ffprobe")):
        raise unittest.SkipTest("ffmpeg and ffprobe are not both on PATH")
    return ffmpeg


def require_node() -> str:
    """Return the node path, or skip the test when node is missing."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not on PATH")
    return node


def require_resvg() -> ModuleType:
    """Return the ``resvg_py`` module, or skip the test when it is not installed."""
    try:
        import resvg_py
    except ImportError:
        raise unittest.SkipTest("resvg-py is not installed (pip install -r requirements-codeart.txt)") from None
    return resvg_py


# --------------------------------------------------------------------------- real fixtures

@functools.lru_cache(maxsize=None)
def _verified_real_fixtures() -> dict[str, str]:
    provenance = json.loads((REAL_FIXTURES_DIR / "PROVENANCE.json").read_text(encoding="utf-8"))
    return {entry["file"]: entry["sha256"] for entry in provenance["fixtures"]}


def real_fixture(name: str) -> Path:
    """Return a real-art fixture after checking it against ``PROVENANCE.json``.

    Real fixtures are owner-generated and test-only; anything not listed with a
    matching sha256 is refused so unrecorded art cannot slip into the suite.
    """
    expected = _verified_real_fixtures().get(name)
    if expected is None:
        raise AssertionError(f"{name} is not listed in {REAL_FIXTURES_DIR / 'PROVENANCE.json'}")
    path = REAL_FIXTURES_DIR / name
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise AssertionError(f"{name} sha256 {actual} does not match PROVENANCE.json {expected}")
    return path


# --------------------------------------------------------------------------- contracts

def _schema_dir(skill: str | None) -> Path:
    return SKILLS_DIR / skill / "references" / "schemas" if skill else SHARED_SCHEMAS_DIR


@functools.lru_cache(maxsize=None)
def _schema_set(directory: Path) -> tuple[dict[str, dict], Any]:
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    schemas = {}
    for path in sorted(directory.glob("*.schema.json")):
        schemas[path.name.removesuffix(".schema.json")] = json.loads(path.read_text(encoding="utf-8"))
    if not schemas:
        raise FileNotFoundError(f"No *.schema.json files in {directory}")
    registry = Registry().with_resources(
        (schema["$id"], DRAFT202012.create_resource(schema)) for schema in schemas.values())
    return schemas, registry


def contract_validator(domain: str, name: str, *, skill: str | None = None):
    """Return a Draft 2020-12 validator for one contract, e.g. ``("map", "map_bundle_v2")``.

    ``domain`` is a schema file stem (common, sprite, video, map, codeart, media) and
    ``name`` one of its ``$defs``. By default the canonical ``shared/schemas`` copy is
    used; ``skill="generate2dmap"`` validates against that skill's vendored copy.
    """
    from jsonschema import Draft202012Validator

    schemas, registry = _schema_set(_schema_dir(skill))
    if domain not in schemas:
        raise KeyError(f"No {domain}.schema.json in {_schema_dir(skill)}; have {sorted(schemas)}")
    defs = schemas[domain].get("$defs", {})
    if name not in defs:
        raise KeyError(f"{domain}.schema.json has no $defs/{name}; have {sorted(defs)}")
    return Draft202012Validator({"$ref": f"{schemas[domain]['$id']}#/$defs/{name}"}, registry=registry)


def contract_errors(instance: Any, domain: str, name: str, *, skill: str | None = None) -> list[str]:
    """Return readable validation errors (``$.json.path: message``), empty when valid."""
    validator = contract_validator(domain, name, skill=skill)
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(map(str, error.absolute_path)))
    return [f"{error.json_path}: {error.message}" for error in errors]


def assert_valid_contract(instance: Any, domain: str, name: str, *, skill: str | None = None) -> None:
    """Raise AssertionError listing every violation of the named contract."""
    errors = contract_errors(instance, domain, name, skill=skill)
    if errors:
        raise AssertionError(f"Document violates {domain}/{name}:\n  " + "\n  ".join(errors))
