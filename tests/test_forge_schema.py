"""shared/forge_schema.py: the standard-library Draft 2020-12 evaluator (integration D31).

B13's built-in evaluator (map_bundle.py), promoted and completed. These tests prove that it
gives the jsonschema package's verdict on every contract fixture under tests/fixtures/contracts
(against shared/schemas and against every skill's vendored copy), on synthetic schemas for
every keyword it supports (including uniqueItems and dependentRequired, which pending schema
requests add) and on random mutations of real documents; that it refuses what it does not
implement; and that it is a drop-in for B13's and B18's private evaluators.
"""
from __future__ import annotations

import copy
import json
import random
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from forge_testutils import (
    FIXTURES_DIR, REPO_ROOT, SHARED_SCHEMAS_DIR, SKILLS_DIR, contract_errors, load_script, load_shared,
)

fs = load_shared("forge_schema")
CONTRACTS_DIR = FIXTURES_DIR / "contracts"
SKILL_SCHEMA_DIRS = sorted(path for path in SKILLS_DIR.glob("*/references/schemas") if path.is_dir())


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


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


def fixture_documents():
    """(domain, def, document, label, named error or None) for every contract fixture document."""
    for path in sorted(CONTRACTS_DIR.glob("*.json")):
        domain, name, kind = path.name[:-len(".json")].split(".", 2)
        if kind == "invalid":
            valid = read_json(CONTRACTS_DIR / f"{domain}.{name}.valid.json")
            for case in read_json(path)["cases"]:
                yield domain, name, mutate(valid, case), f"{path.name}: {case.get('why')}", case.get("error")
        else:
            yield domain, name, read_json(path), path.name, None


FIXTURES = list(fixture_documents())


# --------------------------------------------------------------------------- agreement with jsonschema

def test_agrees_with_jsonschema_on_every_contract_fixture():
    schemas = fs.schema_set(SHARED_SCHEMAS_DIR)
    disagreements, valid, invalid = [], 0, 0
    for domain, name, document, label, _ in FIXTURES:
        ours = schemas.contract_errors(document, domain, name)
        theirs = contract_errors(document, domain, name)
        if bool(ours) != bool(theirs):
            disagreements.append(f"{label}: jsonschema {'rejects' if theirs else 'accepts'}; ours: {ours[:2]}")
        valid += not theirs
        invalid += bool(theirs)
    assert not disagreements, "\n".join(disagreements)
    assert valid > 100 and invalid > 250, (valid, invalid)  # every fixture document of all six domains


@pytest.mark.parametrize("directory", SKILL_SCHEMA_DIRS, ids=lambda path: path.parent.parent.name)
def test_agrees_with_jsonschema_on_each_skills_vendored_schemas(directory):
    skill = directory.parent.parent.name
    vendored = {path.name[:-len(".schema.json")] for path in directory.glob("*.schema.json")}
    schemas = fs.schema_set(directory)
    checked = 0
    for domain, name, document, label, _ in FIXTURES:
        if domain not in vendored:
            continue
        ours = schemas.contract_errors(document, domain, name)
        theirs = contract_errors(document, domain, name, skill=skill)
        assert bool(ours) == bool(theirs), (label, ours[:2], theirs[:2])
        checked += 1
    assert checked > 20


def test_named_fixture_errors_are_reproduced():
    """Invalid fixture cases that name the error they must produce (a substring of one jsonschema
    "$.path: message" line) find it in forge_schema's lines too: same paths, same wording."""
    schemas = fs.schema_set(SHARED_SCHEMAS_DIR)
    named = [(domain, name, document, label, error) for domain, name, document, label, error in FIXTURES if error]
    missing = [f"{label}: {error!r} not in {schemas.contract_errors(document, domain, name)[:3]}"
               for domain, name, document, label, error in named
               if not any(error in line for line in schemas.contract_errors(document, domain, name))]
    assert not missing, "\n".join(missing)
    assert len(named) > 40


def test_every_keyword_of_every_schema_is_supported():
    """The shared schemas and every vendored copy use only keywords forge_schema implements (or
    treats as annotations), including those the pending schema requests add."""
    for directory in (SHARED_SCHEMAS_DIR, *SKILL_SCHEMA_DIRS):
        schemas = fs.SchemaSet(directory)
        schemas.check_supported()
        used = set().union(*(fs.schema_keywords(schemas.document(name)) for name in schemas.names()))
        assert used <= fs.KEYWORDS, (directory, used - fs.KEYWORDS)
    assert {"uniqueItems", "dependentRequired"} <= fs.ASSERTIONS  # B06 and B16 schema requests


# --------------------------------------------------------------------------- synthetic schemas

BASE = "https://example.invalid/schemas/"
OTHER = {"$id": BASE + "other.schema.json", "$defs": {
    "pair": {"type": "array", "prefixItems": [{"const": [1, {"a": 2}]}, {"type": "integer"}], "items": False},
    "name": {"type": "string", "pattern": "^[a-z]+$", "minLength": 2, "maxLength": 5},
}}
MAIN = {"$id": BASE + "main.schema.json", "$defs": {
    "thing": {
        "type": "object", "minProperties": 1, "maxProperties": 6, "propertyNames": {"maxLength": 6},
        "required": ["kind"],
        "properties": {
            "kind": {"enum": ["a", "b", 1, True, None, [1], {"k": 1}]},
            "n": {"type": "number", "exclusiveMaximum": 5, "minimum": -3, "multipleOf": 0.5},
            "m": {"type": "integer", "multipleOf": 3, "maximum": 30, "exclusiveMinimum": 0},
            "s": {"$ref": "other.schema.json#/$defs/name"},
            "p": {"$ref": "other.schema.json#/$defs/pair"},
            "tags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True, "minItems": 1, "maxItems": 4},
            "bag": {"type": "array", "contains": {"type": "integer", "minimum": 10}, "minContains": 2,
                    "maxContains": 3},
            "any": {"contains": {"const": "x"}},
            "map": {"type": "object", "patternProperties": {"^x-": {"type": "integer"}, "^y": {"type": "string"}},
                    "additionalProperties": False},
            "loose": {"type": "object", "patternProperties": {"^n": {"type": "number"}},
                      "additionalProperties": {"type": "boolean"}},
            "choice": {"oneOf": [{"type": "integer"}, {"type": "number", "minimum": 2}]},
            "either": {"anyOf": [{"type": "string"}, {"type": "array", "maxItems": 0}]},
            "never": False, "always": True,
        },
        "dependentRequired": {"n": ["m"]},
        "allOf": [{"properties": {"kind": {"not": {"const": "z"}}}}],
        "dependentSchemas": {"s": {"properties": {"m": {"maximum": 9}}}},
        "if": {"properties": {"kind": {"const": "a"}}},
        "then": {"required": ["n"]},
        "else": {"not": {"required": ["tags"]}},
        "additionalProperties": {"type": ["string", "null"]},
        "description": "A synthetic schema with every keyword forge_schema implements.",
    },
    # the two requests that add keywords neither B13's nor B18's evaluator supported
    "lock": {"type": "object", "required": ["modes"], "properties": {
        "modes": {"type": "array", "items": {"enum": ["feet", "x", "hip"]}, "uniqueItems": True}}},
    "registration": {"type": "object", "properties": {"fit": {"enum": ["contain", "cover"]},
                                                      "scale": {"type": "number", "exclusiveMinimum": 0},
                                                      "offset": {"type": "array", "minItems": 2, "maxItems": 2}},
                     "dependentRequired": {"scale": ["offset"], "offset": ["scale"]}},
}}
INSTANCES = {
    "thing": [
        {"kind": "a", "n": 4.5, "m": 3}, {"kind": "a", "n": 5, "m": 3}, {"kind": "a"}, {"kind": "b"}, {},
        {"kind": "b", "tags": ["x"]}, {"kind": 1.0}, {"kind": 1}, {"kind": True}, {"kind": None}, {"kind": [1.0]},
        {"kind": {"k": 1.0}}, {"kind": {"k": True}}, {"kind": "c"}, {"kind": "b", "n": 1.25, "m": 3},
        {"kind": "b", "n": -3, "m": 3}, {"kind": "b", "n": -3.5, "m": 3}, {"kind": "b", "n": 2, "m": 0},
        {"kind": "b", "n": 1, "m": 31}, {"kind": "b", "n": 1, "m": 2}, {"kind": "b", "n": 1, "m": 6.0},
        {"kind": "b", "n": 1}, {"kind": "b", "s": "ab"}, {"kind": "b", "s": "abc", "m": 12},
        {"kind": "b", "s": "abc", "m": 9}, {"kind": "b", "s": "a"}, {"kind": "b", "s": "abcdef"},
        {"kind": "b", "s": "AB"}, {"kind": "b", "p": [[1, {"a": 2}], 3]}, {"kind": "b", "p": [[1, {"a": 2.0}], 3]},
        {"kind": "b", "p": [[1, {"a": 2}], 3, 4]}, {"kind": "b", "p": [[True, {"a": 2}], 3]},
        {"kind": "b", "p": [[1, {"a": 2}]]}, {"kind": "b", "p": []}, {"kind": "b", "p": [[1, {"a": 2}], 3.5]},
        {"kind": "a", "n": 1, "m": 3, "tags": ["a", "b"]}, {"kind": "a", "n": 1, "m": 3, "tags": ["a", "a"]},
        {"kind": "b", "tags": []}, {"kind": "a", "n": 0, "m": 3, "tags": ["a", "b", "c", "d", "e"]},
        {"kind": "b", "bag": [10, 11]}, {"kind": "b", "bag": [10]}, {"kind": "b", "bag": [10, 11, 12, 13]},
        {"kind": "b", "bag": [10, 11, 12, "x"]}, {"kind": "b", "bag": []}, {"kind": "b", "bag": [10.0, 11.0]},
        {"kind": "b", "any": ["y", "x"]}, {"kind": "b", "any": []}, {"kind": "b", "any": "x"},
        {"kind": "b", "map": {"x-a": 1, "yes": "s"}}, {"kind": "b", "map": {"x-a": 1.5}},
        {"kind": "b", "map": {"zz": 1}}, {"kind": "b", "map": {"zz": 1, "ww": 2}}, {"kind": "b", "map": {}},
        {"kind": "b", "loose": {"n1": 2, "flag": True}}, {"kind": "b", "loose": {"n1": "2"}},
        {"kind": "b", "loose": {"flag": 1}}, {"kind": "b", "choice": 1}, {"kind": "b", "choice": 3},
        {"kind": "b", "choice": 2.5}, {"kind": "b", "choice": 1.5}, {"kind": "b", "choice": "1"},
        {"kind": "b", "either": "s"}, {"kind": "b", "either": []}, {"kind": "b", "either": [1]},
        {"kind": "b", "never": 1}, {"kind": "b", "always": {"x": 1}}, {"kind": "b", "extra": "s"},
        {"kind": "b", "extra": None}, {"kind": "b", "extra": 1}, {"kind": "b", "toolongname": "s"},
        {"kind": "b", "a": "1", "b": "2", "c": "3", "d": "4", "e": "5", "f": "6"}, {"kind": "z"}, [], "thing", 3,
        None,
    ],
    "lock": [{"modes": ["feet", "x"]}, {"modes": ["feet", "feet"]}, {"modes": []}, {"modes": ["hip", "head"]}, {}],
    "registration": [{}, {"fit": "cover"}, {"scale": 2, "offset": [0, 0]}, {"scale": 2}, {"offset": [1, 2]},
                     {"scale": 0, "offset": [0, 0]}, {"fit": "cover", "scale": 1.5, "offset": [1, 2, 3]}],
}


@pytest.fixture()
def synthetic(tmp_path):
    for schema in (OTHER, MAIN):
        (tmp_path / schema["$id"].rsplit("/", 1)[1]).write_text(json.dumps(schema), encoding="utf-8")
    registry = Registry().with_resources((s["$id"], DRAFT202012.create_resource(s)) for s in (OTHER, MAIN))
    return fs.SchemaSet(tmp_path), registry


def test_every_supported_keyword_agrees_with_jsonschema(synthetic):
    ours, registry = synthetic
    checked = {True: 0, False: 0}
    for definition, instances in INSTANCES.items():
        theirs = Draft202012Validator({"$ref": f"{BASE}main.schema.json#/$defs/{definition}"}, registry=registry)
        for instance in instances:
            expected = theirs.is_valid(instance)
            assert ours.is_valid(instance, f"main.schema.json#/$defs/{definition}") == expected, (definition, instance)
            checked[expected] += 1
    assert checked[True] > 15 and checked[False] > 40, checked
    Draft202012Validator.check_schema(MAIN)
    assert fs.ASSERTIONS <= fs.schema_keywords(MAIN) | fs.schema_keywords(OTHER)  # every assertion is exercised


def test_refs_resolve_by_id_by_file_name_and_inside_documents(synthetic):
    ours, _ = synthetic
    by_id = ours.errors({"kind": "c"}, f"{BASE}main.schema.json#/$defs/thing")
    by_name = ours.errors({"kind": "c"}, "main.schema.json#/$defs/thing")
    assert by_id == by_name and by_id[0].startswith("$.kind: 'c' is not one of")
    assert ours.document("other.schema.json") is ours.document(f"{BASE}other.schema.json")
    assert ours.errors("ab", "other.schema.json#/$defs/name") == []
    escaped = {"$id": BASE + "esc.schema.json", "$defs": {"a/b": {"type": "string"}, "c~d": {"$ref": "#/$defs/a~1b"},
                                                         "e%f": {"$ref": "#/$defs/c~0d"}}}
    (ours.directory / "esc.schema.json").write_text(json.dumps(escaped), encoding="utf-8")
    fresh = fs.SchemaSet(ours.directory)
    assert fresh.errors(1, "esc.schema.json#/$defs/e%25f") == ["$: 1 is not of type 'string'"]
    with pytest.raises(fs.SchemaError, match="cannot resolve"):
        fresh.errors(1, "missing.schema.json#/$defs/x")
    with pytest.raises(fs.SchemaError, match="no 'nope'"):
        fresh.errors(1, "esc.schema.json#/$defs/nope")


def test_unsupported_schemas_fail_loudly(tmp_path):
    cases = [
        ({"unevaluatedProperties": False}, "unsupported JSON Schema keyword"),
        ({"dependencies": {"a": ["b"]}}, "unsupported JSON Schema keyword"),
        ({"type": "object", "propertis": {}}, "unsupported JSON Schema keyword"),
        ({"$ref": "#thing"}, "plain-name fragments"),
        ({"properties": {"a": {"$id": "https://x/y.json", "type": "string"}}}, "nested \\$id"),
        ({"type": "text"}, "unknown JSON Schema type"),
        ({"properties": {"a": {"pattern": "("}}}, "not a valid regular expression"),
        ({"properties": {"a": {"$ref": "#/$defs/x"}}, "$ref": "#/$defs/x"}, "deeper than"),
        ({"$ref": 5}, "\\$ref must be a string"),
        ({"allOf": [3]}, "must be an object or a boolean"),
    ]
    for index, (schema, message) in enumerate(cases):
        document = {"$id": f"https://example.invalid/case{index}.schema.json", "$defs": {"x": schema}}
        (tmp_path / f"case{index}.schema.json").write_text(json.dumps(document), encoding="utf-8")
        schemas = fs.SchemaSet(tmp_path)
        with pytest.raises(fs.SchemaError, match=message):
            schemas.errors({"a": "x"}, f"case{index}.schema.json#/$defs/x")
    bad_pattern = tmp_path / "patterns"
    bad_pattern.mkdir()
    (bad_pattern / "p.schema.json").write_text(json.dumps({"$defs": {"x": {"patternProperties": {"[": True}}}}),
                                               encoding="utf-8")
    with pytest.raises(fs.SchemaError, match="not a valid regular expression"):
        fs.SchemaSet(bad_pattern).check_supported()  # patterns compile eagerly too
    with pytest.raises(fs.SchemaError, match="unsupported"):
        fs.SchemaSet(tmp_path).check_supported()
    (tmp_path / "broken.schema.json").write_text("{", encoding="utf-8")
    with pytest.raises(fs.SchemaError, match="cannot load schema broken.schema.json"):
        fs.SchemaSet(tmp_path).names()
    empty = fs.SchemaSet(tmp_path / "nothing-here")
    assert empty.names() == []
    with pytest.raises(fs.SchemaError, match="no schema"):
        empty.document("map.schema.json")


def test_messages_follow_jsonschema(synthetic):
    ours, registry = synthetic
    lines = ours.errors({"kind": "b", "s": "AB", "map": {"zz": 1}, "tags": ["a", "a"], "bag": [10], "n": 1},
                        "main.schema.json#/$defs/thing")
    assert "$.s: 'AB' does not match '^[a-z]+$'" in lines
    assert "$.map: 'zz' does not match any of the regexes: '^x-', '^y'" in lines
    assert "$.tags: ['a', 'a'] has non-unique elements" in lines
    assert "$.bag: Too few items match the given schema (expected at least 2 but only 1 matched)" in lines
    assert "$: 'm' is a dependency of 'n'" in lines
    assert ours.errors({"kind": "b", "toolongname": "s"}, "main.schema.json#/$defs/thing") == [
        "$: property name 'toolongname' is not allowed ('toolongname' is too long)"]
    assert ours.errors({"kind": "b", "choice": 3, "never": 1}, "main.schema.json#/$defs/thing") == [
        "$.choice: 3 is valid under each of [{'type': 'integer'}, {'type': 'number', 'minimum': 2}]",
        "$.never: False schema does not allow 1"]  # in the schema's property order
    either = ours.errors({"kind": "b", "either": [1]}, "main.schema.json#/$defs/thing")
    assert either == ["$.either: [1] is not valid under any of the given schemas"]
    assert ours.errors({"kind": "b", "p": [[1, {"a": 3}], 1]}, "main.schema.json#/$defs/thing") == [
        "$.p[0]: [1, {'a': 2}] was expected"]
    nested = {"$id": BASE + "nested.schema.json", "$defs": {"x": {"properties": {"v": {"anyOf": [
        {"type": "object", "properties": {"q": {"type": "string"}}}, {"type": "array"}],
        "description": "An object with a text q, or a list."}}}}}
    (ours.directory / "nested.schema.json").write_text(json.dumps(nested), encoding="utf-8")
    hinted = fs.SchemaSet(ours.directory)
    assert hinted.errors({"v": {"q": 1}}, "nested.schema.json#/$defs/x") == [
        "$.v: {'q': 1} is not valid under any of the given schemas (closest: $.v.q: 1 is not of type 'string')"]
    assert hinted.errors({"v": 2}, "nested.schema.json#/$defs/x") == [
        "$.v: 2 is not valid under any of the given schemas (An object with a text q, or a list.)"]
    keys = {"odd key": 1, "it's": 1, "back\\slash": 1, "_under": 1, "x1": 1}
    loose = {"$id": BASE + "keys.schema.json", "$defs": {"k": {"additionalProperties": {"type": "string"}}}}
    (ours.directory / "keys.schema.json").write_text(json.dumps(loose), encoding="utf-8")
    lines = fs.SchemaSet(ours.directory).errors(keys, "keys.schema.json#/$defs/k")
    ours_paths = sorted(line.split(": ", 1)[0] for line in lines)
    validator = Draft202012Validator(loose["$defs"]["k"])
    assert ours_paths == sorted(error.json_path for error in validator.iter_errors(keys))  # jsonschema's json_path
    long = fs.SchemaSet(ours.directory).errors(list(range(100)), "other.schema.json#/$defs/name")
    assert long[0].endswith("... is not of type 'string'") and len(long[0]) < 140  # long values are shortened


def _mutations(document, rng: random.Random, count: int):
    """Random single-field mutations: wrong types, out-of-range numbers, removed keys (B13's fuzz)."""
    pointers = []

    def walk(node, pointer):
        pointers.append(pointer)
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, pointer + [key])
        elif isinstance(node, list):
            for index, value in enumerate(node[:3]):
                walk(value, pointer + [index])

    walk(document, [])
    replacements = [None, True, -1, 0, 1.5, "", "x:y", "/abs", [], {}, [1, 2], "#zz", 1e9, "#ff00ff", [1, 2, 3, 4]]
    for _ in range(count):
        mutated = copy.deepcopy(document)
        pointer = rng.choice(pointers[1:])
        parent = mutated
        for part in pointer[:-1]:
            parent = parent[part]
        if isinstance(parent, dict) and rng.random() < 0.3:
            del parent[pointer[-1]]
        else:
            parent[pointer[-1]] = copy.deepcopy(rng.choice(replacements))
        yield mutated


def test_mutations_of_real_documents_agree_with_jsonschema(tmp_path):
    from test_map_bundle import write_demo_bundle

    schemas = fs.schema_set(SHARED_SCHEMAS_DIR)
    sources = [("map", "map_bundle_v2", read_json(write_demo_bundle(tmp_path / "map"))),
               ("codeart", "pixelspec_v1",
                read_json(SKILLS_DIR / "codeart2d" / "examples" / "walker16x24.pixelspec.json"))]
    for path in sorted(CONTRACTS_DIR.glob("*.valid.json")):
        domain, name, _ = path.name[:-len(".json")].split(".", 2)
        if (domain, name) in {("video", "animation_v3"), ("media", "job_v2"), ("sprite", "pipeline_meta_v2"),
                              ("codeart", "rig_anim_v1"), ("map", "tileset_v1"), ("common", "qaEnvelope")}:
            sources.append((domain, name, read_json(path)))
    rng = random.Random(31)
    checked = 0
    for domain, name, document in sources:
        assert not schemas.contract_errors(document, domain, name), (domain, name)
        for mutated in _mutations(document, rng, 120):
            ours = schemas.contract_errors(mutated, domain, name)
            theirs = contract_errors(mutated, domain, name)
            assert bool(ours) == bool(theirs), (domain, name, ours[:2], theirs[:2])
            checked += 1
    assert checked >= 120 * 8


# --------------------------------------------------------------------------- a drop-in for B13 and B18

def test_drop_in_for_b13_and_b18_evaluators():
    """D31 evidence: on every fixture document their schema folders cover, forge_schema returns the
    verdict of B13's _LocalSchemaSet (map_bundle.py) and of B18's _LocalContracts (render_pixelspec.py).
    A document the private evaluators cannot evaluate (a keyword they lack) is counted, not compared;
    once both adopt forge_schema the private classes are gone and this test skips."""
    map_bundle = load_script("generate2dmap", "map_bundle")
    render = load_script("codeart2d", "render_pixelspec")
    legacy = []
    if hasattr(map_bundle, "_LocalSchemaSet"):
        b13 = map_bundle._LocalSchemaSet(map_bundle.SCHEMA_DIR)
        legacy.append(("B13", map_bundle.SCHEMA_DIR, lambda doc, d, n: b13.errors(doc, f"{d}.schema.json#/$defs/{n}")))
    if hasattr(render, "_LocalContracts"):
        b18 = render._LocalContracts(render.SCHEMA_DIR)
        legacy.append(("B18", render.SCHEMA_DIR, lambda doc, d, n: b18.errors(doc, d, n)))
    if not legacy:
        pytest.skip("B13 and B18 adopted forge_schema; nothing private is left to compare")
    for owner, directory, evaluate in legacy:
        schemas = fs.schema_set(directory)
        vendored = {path.name[:-len(".schema.json")] for path in Path(directory).glob("*.schema.json")}
        compared = skipped = 0
        for domain, name, document, label, _ in FIXTURES:
            if domain not in vendored:
                continue
            try:
                expected = bool(evaluate(document, domain, name))
            except (ValueError, RuntimeError):
                skipped += 1
                continue
            assert bool(schemas.contract_errors(document, domain, name)) == expected, (owner, label)
            compared += 1
        assert compared > 100 and skipped <= compared // 10, (owner, compared, skipped)


def test_vendored_copies_match_the_canonical():
    canonical = (REPO_ROOT / "shared" / "forge_schema.py").read_bytes().replace(b"\r\n", b"\n")
    for skill in ("generate2dmap", "codeart2d", "video2dsprite"):
        path = SKILLS_DIR / skill / "scripts" / "forge_schema.py"
        assert path.read_bytes().replace(b"\r\n", b"\n") == canonical, skill
        copy_module = load_script(skill, "forge_schema")
        schemas = copy_module.schema_set(SKILLS_DIR / skill / "references" / "schemas")
        schemas.check_supported()
        assert copy_module.FORGE_SCHEMA_API_VERSION == fs.FORGE_SCHEMA_API_VERSION


def test_shortcuts_and_helpers():
    assert fs.schema_set(SHARED_SCHEMAS_DIR) is fs.schema_set(str(SHARED_SCHEMAS_DIR))  # cached per folder
    assert fs.contract_errors({"path": "a.png"}, "common", "fileRef", schema_dir=SHARED_SCHEMAS_DIR) == [
        "$: 'sha256' is a required property"]
    assert fs.json_path("$", "a") == "$.a" and fs.json_path("$", 0) == "$[0]" and fs.json_path("$", "_a") == "$['_a']"
    assert fs.json_equal([1, {"a": 2.0}], [1, {"a": 2}])
    assert not fs.json_equal([True], [1]) and not fs.json_equal("1", 1)
    assert fs.is_type(1.0, "integer") and not fs.is_type(True, "number") and fs.is_type(None, ["string", "null"])
    assert re.match(r"^\$", fs.SchemaSet(SHARED_SCHEMAS_DIR).errors(3, "common.schema.json#/$defs/fileRef")[0])
