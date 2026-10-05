"""Frozen JSON-schema contracts (plan Appendix B), their fixtures, the shared test
helpers, and the provenance of the real-art fixtures.

Fixtures in tests/fixtures/contracts are named <domain>.<def>.<kind>.json:
- valid: the one valid document of the def (negative cases mutate it);
- invalid: {"cases": [...]}; each case sets or removes one JSON pointer of the valid
  document ({"set": ptr, "value": ...} or {"remove": ptr}) or replaces it ({"document": ...}),
  says why it is invalid, and may name the error it must produce ("error": a substring of
  one "$.path: message" line);
- valid-<variant>: more valid documents, such as real producer output;
- legacy-<variant>: documents of older writers that must stay valid.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys

import numpy as np
import pytest
from jsonschema import Draft202012Validator
from PIL import Image
from referencing import Registry
from referencing.jsonschema import DRAFT202012

import forge_testutils
from forge_testutils import (
    FIXTURES_DIR, REAL_FIXTURES_DIR, REPO_ROOT, SHARED_DIR, SHARED_SCHEMAS_DIR, SKILLS_DIR,
    contract_errors, load_script, load_shared, make_magenta_sheet, real_fixture, run_cli,
)

CONTRACTS_DIR = FIXTURES_DIR / "contracts"
SCHEMA_BASE = "https://raw.githubusercontent.com/0x0funky/agent-sprite-forge/main/shared/schemas/"
DOMAINS = ("common", "sprite", "video", "map", "codeart", "media")
SCHEMAS = {domain: json.loads((SHARED_SCHEMAS_DIR / f"{domain}.schema.json").read_text(encoding="utf-8"))
           for domain in DOMAINS}
CONTRACTS = [(domain, name) for domain in DOMAINS for name in SCHEMAS[domain]["$defs"]]
DOCUMENT_ID_PREFIXES = {
    "sprite": ("generate2dsprite.",),
    "video": ("video2dsprite.", "forge-"),
    "map": ("generate2dmap.",),
    "codeart": ("codeart2d.", "codeart."),  # codeart.pixelspec.v1 is the legacy PixelSpec alias
    "media": ("generate2dmedia.",),
}
REQUIRED_LEGACY = ("video.frame_selection_v2.legacy-v1.json", "video.animation_v3.legacy-2.0.json")


def contract_id(item):
    return "/".join(item)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def schema_refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                yield value
            else:
                yield from schema_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from schema_refs(value)


def mutate(document, case):
    """Apply one invalid-fixture case (set, remove or document) to a copy of the valid document."""
    if "document" in case:
        return copy.deepcopy(case["document"])
    result = copy.deepcopy(document)
    pointer = case["set"] if "set" in case else case["remove"]
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    parent = result
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
    if "set" in case:
        parent[key] = copy.deepcopy(case["value"])
    else:
        del parent[key]
    return result


# --------------------------------------------------------------------------- schemas

@pytest.mark.parametrize("domain", DOMAINS)
def test_schemas_are_valid(domain):
    schema = SCHEMAS[domain]
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"{SCHEMA_BASE}{domain}.schema.json"
    assert schema["title"] and schema["description"]
    undocumented = [name for name, definition in schema["$defs"].items() if not definition.get("description")]
    assert not undocumented, f"$defs without a description: {undocumented}"
    registry = Registry().with_resources(
        (other["$id"], DRAFT202012.create_resource(other)) for other in SCHEMAS.values())
    resolver = registry.resolver(base_uri=schema["$id"])
    for reference in schema_refs(schema):
        resolver.lookup(reference)


def test_document_ids_follow_the_domain_prefixes():
    for domain, prefixes in DOCUMENT_ID_PREFIXES.items():
        for name, definition in SCHEMAS[domain]["$defs"].items():
            id_schema = definition.get("properties", {}).get("schema")
            if not id_schema:
                continue
            ids = [id_schema["const"]] if "const" in id_schema else id_schema["enum"]
            for value in ids:
                assert value.startswith(prefixes), f"{domain}/{name}: {value} lacks a {prefixes} prefix"
                assert re.search(r"[./]v[0-9]+$", value), f"{domain}/{name}: {value} has no version suffix"
            version = re.search(r"_v([0-9]+)$", name)
            if version:
                assert any(value.endswith(f"v{version.group(1)}") for value in ids), f"{domain}/{name}: {ids}"


@pytest.mark.parametrize("schema_dir", sorted(SKILLS_DIR.glob("*/references/schemas")), ids=lambda p: p.parents[1].name)
def test_vendored_schemas_are_self_contained(schema_dir):
    """Each skill ships every schema its schemas reference, and its copies validate the examples."""
    available = {path.name for path in schema_dir.glob("*.schema.json")}
    assert "common.schema.json" in available
    for name in sorted(available):
        for reference in schema_refs(read_json(schema_dir / name)):
            target = reference.split("#", 1)[0]
            assert not target or target in available, f"{name} references {target}, which this skill does not ship"
    skill = schema_dir.parents[1].name
    failures = {}
    for domain in (name.removesuffix(".schema.json") for name in sorted(available)):
        for name in SCHEMAS[domain]["$defs"]:
            errors = contract_errors(read_json(CONTRACTS_DIR / f"{domain}.{name}.valid.json"), domain, name, skill=skill)
            if errors:
                failures[f"{domain}/{name}"] = errors
    assert not failures


# --------------------------------------------------------------------------- fixtures

def test_every_contract_fixture_names_a_contract():
    pattern = re.compile(r"^(\w+)\.(\w+)\.(valid|invalid|valid-[A-Za-z0-9.-]+|legacy-[A-Za-z0-9.-]+)\.json$")
    for path in sorted(CONTRACTS_DIR.iterdir()):
        match = pattern.match(path.name)
        assert match, f"unexpected fixture name {path.name}"
        assert (match.group(1), match.group(2)) in CONTRACTS, f"{path.name} names no contract"


@pytest.mark.parametrize("contract", CONTRACTS, ids=contract_id)
def test_examples_validate(contract):
    domain, name = contract
    errors = contract_errors(read_json(CONTRACTS_DIR / f"{domain}.{name}.valid.json"), domain, name)
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("path", sorted(CONTRACTS_DIR.glob("*.valid-*.json")), ids=lambda p: p.name)
def test_variant_examples_validate(path):
    """Further valid documents of one def, such as real Wave A producer output (unpriced
    jobs, null receipt purposes, codeart-meta palettes, segment poses, rational fps)."""
    domain, name, _ = path.name.split(".", 2)
    errors = contract_errors(read_json(path), domain, name)
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("contract", CONTRACTS, ids=contract_id)
def test_invalid_examples_fail(contract):
    domain, name = contract
    valid = read_json(CONTRACTS_DIR / f"{domain}.{name}.valid.json")
    cases = read_json(CONTRACTS_DIR / f"{domain}.{name}.invalid.json")["cases"]
    assert cases, "every contract needs at least one negative case"
    accepted, wrong_reason = [], []
    for case in cases:
        assert case.get("why"), f"case {case} does not say why it is invalid"
        document = mutate(valid, case)
        assert document != valid, f"case {case['why']!r} does not change the document"
        errors = contract_errors(document, domain, name)
        if not errors:
            accepted.append(case["why"])
        elif "error" in case and not any(case["error"] in error for error in errors):
            wrong_reason.append(f"{case['why']!r}: expected {case['error']!r} in {errors}")
    assert not accepted, f"invalid documents accepted: {accepted}"
    assert not wrong_reason, "invalid documents rejected for another reason:\n" + "\n".join(wrong_reason)


@pytest.mark.parametrize("path", sorted(CONTRACTS_DIR.glob("*.legacy-*.json")), ids=lambda p: p.name)
def test_v1_documents_still_valid(path):
    """Documents written by the cfed170 tools (frame-selection v1, animation.json 2.0 from
    engine_export.py:240-250, v1 clips, metadata, profiles, prop packs, placements, jobs) stay valid."""
    domain, name, _ = path.name.split(".", 2)
    errors = contract_errors(read_json(path), domain, name)
    assert not errors, "\n".join(errors)


def test_mandated_legacy_documents_exist():
    for name in REQUIRED_LEGACY:
        assert (CONTRACTS_DIR / name).is_file(), name


# --------------------------------------------------------------------------- real fixtures

def test_real_fixture_provenance():
    provenance = read_json(REAL_FIXTURES_DIR / "PROVENANCE.json")
    entries = provenance["fixtures"]
    files = {path.name for path in REAL_FIXTURES_DIR.iterdir()} - {"PROVENANCE.json"}
    assert files == {entry["file"] for entry in entries}, "every real fixture is listed, and only those"
    for entry in entries:
        assert {"file", "source", "sha256", "generator", "date", "usage", "size", "mode"} <= entry.keys()
        assert entry["usage"] == "owner-generated, test-only"
        assert re.fullmatch(r"(?:src|outputs)/[^:\\]+", entry["source"]), "sources live in this repo (never D:/chain)"
        path = real_fixture(entry["file"])
        with Image.open(path) as image:
            assert [image.width, image.height] == entry["size"] and image.mode == entry["mode"]
    total = sum(path.stat().st_size for path in REAL_FIXTURES_DIR.iterdir())
    assert total <= 3_000_000, f"real fixtures use {total} bytes"


def test_meadow_crop_matches_its_tracked_source():
    entry = next(e for e in read_json(REAL_FIXTURES_DIR / "PROVENANCE.json")["fixtures"]
                 if e["file"] == "meadow-prop-pack-crop.png")
    source = REPO_ROOT / entry["source"]
    if not source.is_file():
        pytest.skip(f"{entry['source']} is not in this checkout")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == entry["source_sha256"]
    with Image.open(source) as full, Image.open(real_fixture(entry["file"])) as crop:
        expected = np.asarray(full.convert(crop.mode).crop(tuple(entry["crop_box"])))
        assert np.array_equal(np.asarray(crop), expected)


# --------------------------------------------------------------------------- shared test helpers

def test_magenta_sheet_is_deterministic_and_reports_boxes():
    sheet, boxes = make_magenta_sheet(2, 3, 48, fringe=True, spill_px=6, return_boxes=True)
    assert sheet.tobytes() == make_magenta_sheet(2, 3, 48, fringe=True, spill_px=6).tobytes()
    assert sheet.size == (144, 96) and sheet.mode == "RGBA"
    pixels = np.asarray(sheet)
    assert (pixels[..., 3] == 255).all()
    key = (pixels[..., :3] == (255, 0, 255)).all(axis=-1)
    assert len(boxes) == 6 and boxes[0][2] == 48 + 6, "the first subject's tail crosses into cell 1"
    for x0, y0, x1, y1 in boxes:
        assert not key[y0:y1, x0:x1].any()
    assert key[0, 0] and key[95, 143]


def test_load_script_reuses_one_module_unless_fresh():
    module = load_script("generate2dmap", "validate_parallax")
    assert load_script("generate2dmap", "validate_parallax") is module
    assert load_script("generate2dmap", "validate_parallax", fresh=True) is not module
    assert sys.modules["forge_generate2dmap_validate_parallax"] is module, "a fresh copy leaves the cache alone"


def _drop_modules_from(directory, before):
    """Forget modules imported from `directory` since `before` (a set of module names)."""
    for name, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None) or ""
        if name not in before and origin.startswith(str(directory)):
            del sys.modules[name]


def test_load_shared_imports_a_shared_module_by_path(tmp_path, monkeypatch):
    assert SHARED_DIR == REPO_ROOT / "shared"
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "forge_probe.py").write_text(  # imports its sibling and defines a dataclass, like forge_matte
        "from __future__ import annotations\n"
        "import sys\nfrom dataclasses import dataclass\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
        "import forge_probe_sibling\n\n\n"
        "@dataclass(frozen=True)\nclass Params:\n    value: int = forge_probe_sibling.VALUE + 1\n\n\n"
        "VALUE = Params().value\n", encoding="utf-8")
    (shared / "forge_probe_sibling.py").write_text("VALUE = 41\n", encoding="utf-8")
    (shared / "forge_broken.py").write_text("raise RuntimeError('import-time failure')\n", encoding="utf-8")
    monkeypatch.setattr(forge_testutils, "SHARED_DIR", shared)
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    try:
        first = load_shared("forge_probe", fresh=True)
        assert first.VALUE == 42 and "forge_shared_forge_probe" not in sys.modules, "a fresh copy is not cached"
        module = load_shared("forge_probe")
        assert module.VALUE == 42 and module.__file__ == str(shared / "forge_probe.py")
        assert sys.modules["forge_shared_forge_probe"] is module
        assert load_shared("forge_probe.py") is module
        fresh = load_shared("forge_probe", fresh=True)
        assert fresh is not module and fresh.VALUE == 42 and sys.modules["forge_shared_forge_probe"] is module
        with pytest.raises(FileNotFoundError, match="forge_missing.py"):
            load_shared("forge_missing")
        with pytest.raises(RuntimeError, match="import-time failure"):
            load_shared("forge_broken")
        assert "forge_shared_forge_broken" not in sys.modules, "a failed import is not cached"
    finally:
        _drop_modules_from(shared, before)


@pytest.mark.parametrize("canonical", sorted(
    entry["canonical"] for entry in read_json(SHARED_DIR / "VENDORED.json")["files"] if entry["canonical"].endswith(".py")))
def test_load_shared_loads_each_landed_canonical(canonical, monkeypatch):
    """Each canonical module imports by path from shared/, with its sibling imports (forge_matte
    needs forge_core beside it). Skipped until the owning Wave A or B module has landed."""
    path = REPO_ROOT / canonical
    if not path.is_file():
        pytest.skip(f"{canonical} has not landed in this checkout")
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    try:
        module = load_shared(path.stem, fresh=True)
        assert module.__file__ == str(path)
    finally:
        _drop_modules_from(SHARED_DIR, before)


def test_run_cli_reproduces_a_narrow_console():
    assert run_cli(["-c", "print('ok')"], "cp1252").stdout.strip() == "ok"
    failed = run_cli(["-c", "print('\\u2192')"], "cp1252")
    assert failed.returncode != 0 and "UnicodeEncodeError" in failed.stderr
