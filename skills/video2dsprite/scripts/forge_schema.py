"""A JSON Schema Draft 2020-12 evaluator for the forge contracts, standard library only.

Skills validate their inputs and outputs against their vendored references/schemas at run
time, without jsonschema (a dev-only dependency). This module is B13's built-in evaluator
(map_bundle.py) promoted per integration decision D31, merged with what B18's
_LocalContracts (render_pixelspec.py) did better, and completed with the remaining simple
Draft 2020-12 assertions, because pending schema requests use uniqueItems (B06) and
dependentRequired (B16), which neither evaluator supported. tests/test_forge_schema.py
proves the verdicts equal jsonschema's on every contract fixture.

Canonical copy: shared/forge_schema.py; byte-identical copies ship where skills validate
contracts at run time (shared/VENDORED.json).

API
  schema_set(directory) -> SchemaSet: the *.schema.json documents of one folder (cached).
  SchemaSet.errors(instance, ref) -> ["$.json.path: message", ...]; empty when valid.
      ref is "<file>#<json pointer>", for example "map.schema.json#/$defs/map_bundle_v2",
      or an absolute URI that matches a document's $id.
  SchemaSet.contract_errors(instance, domain, definition): the same for
      "<domain>.schema.json#/$defs/<definition>".
  SchemaSet.is_valid(instance, ref); SchemaSet.check_supported() raises on keywords this
      evaluator does not implement; contract_errors(instance, domain, definition,
      schema_dir=...) is a one-call shortcut.

Semantics (Draft 2020-12, as the jsonschema package's Draft202012Validator evaluates it):
  * Supported assertions and applicators: $ref, type, enum, const, multipleOf, minimum,
    maximum, exclusiveMinimum, exclusiveMaximum, minLength, maxLength, pattern, prefixItems,
    items, contains with minContains and maxContains, minItems, maxItems, uniqueItems,
    required, properties, patternProperties, additionalProperties, propertyNames,
    minProperties, maxProperties, dependentRequired, dependentSchemas, allOf, anyOf, oneOf,
    not, if / then / else, and boolean schemas.
  * Annotations, never asserted: $schema, $id (document roots only), $comment, $defs,
    $vocabulary, title, description, default, examples, deprecated, readOnly, writeOnly,
    format (as jsonschema does without a format checker), contentEncoding,
    contentMediaType and contentSchema.
  * Anything else (unevaluatedProperties, unevaluatedItems, $anchor, $dynamicRef,
    $dynamicAnchor, a nested $id, a draft-07 keyword such as dependencies, or a typo) raises
    SchemaError instead of being skipped, so a schema change that needs more than this
    evaluator fails loudly. jsonschema itself would ignore an unknown keyword.
  * Types: integer accepts whole floats (1.0); booleans are never numbers. Equality (enum,
    const, uniqueItems) is JSON equality: 1 == 1.0, true != 1, object key order ignored.
    Strings count code points; pattern uses Python's re.search, which matches ECMA-262 for
    the patterns the contracts use.
  * $ref resolves against the $id of the document it appears in (RFC 3986); a reference
    whose document is not a loaded $id falls back to the file of that name in the folder.
    Fragments are JSON pointers (~0, ~1 and percent escapes).

Error lines read "$.json.path: message". Paths follow jsonschema's json_path ($.a.b[0],
$['odd key']) and messages follow its wording ("'x' is a required property", "'q' is not
one of ['a', 'b']", ...). Differences, for readability: a failed type check skips the other
assertions on that value; long values are shortened; an anyOf / oneOf failure names the
closest branch error; a propertyNames failure names the property.
"""
from __future__ import annotations

import functools
import json
import os
import re
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin

FORGE_SCHEMA_API_VERSION = "1"

ASSERTIONS = frozenset({
    "$ref", "type", "enum", "const", "multipleOf", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "prefixItems", "items", "contains", "minContains", "maxContains",
    "minItems", "maxItems", "uniqueItems", "required", "properties", "patternProperties", "additionalProperties",
    "propertyNames", "minProperties", "maxProperties", "dependentRequired", "dependentSchemas", "allOf", "anyOf",
    "oneOf", "not", "if", "then", "else",
})
ANNOTATIONS = frozenset({
    "$schema", "$id", "$comment", "$defs", "$vocabulary", "title", "description", "default", "examples", "deprecated",
    "readOnly", "writeOnly", "format", "contentEncoding", "contentMediaType", "contentSchema",
})
KEYWORDS = ASSERTIONS | ANNOTATIONS
MAX_DEPTH = 256  # nested schema evaluations; deeper means a $ref loop that never consumes the instance
_PATH_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")  # jsonschema's json_path rule

__all__ = [
    "FORGE_SCHEMA_API_VERSION", "ASSERTIONS", "ANNOTATIONS", "KEYWORDS", "SchemaError", "SchemaSet", "schema_set",
    "contract_errors", "json_path", "is_type", "json_equal", "schema_keywords",
]


class SchemaError(ValueError):
    """A schema this evaluator cannot use: unreadable, unresolvable or with an unsupported keyword."""


# --------------------------------------------------------------------------- JSON values

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_type(value: Any, expected: Any) -> bool:
    """Draft 2020-12 type membership: integer accepts whole floats; booleans are not numbers."""
    if isinstance(expected, list):
        return any(is_type(value, item) for item in expected)
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return _is_number(value)
    if expected == "integer":
        return _is_number(value) and (isinstance(value, int) or float(value).is_integer())
    raise SchemaError(f"unknown JSON Schema type {expected!r}")


def json_equal(a: Any, b: Any) -> bool:
    """JSON equality: 1 == 1.0, but booleans never equal numbers; key order is ignored."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[key], b[key]) for key in a)
    return type(a) is type(b) and a == b


def _canonical(value: Any) -> Any:
    """A hashable key with json_equal semantics (for uniqueItems)."""
    if isinstance(value, bool):
        return ("b", value)
    if _is_number(value):
        return ("n", value)  # 1 and 1.0 hash and compare equal
    if isinstance(value, list):
        return ("l", tuple(_canonical(item) for item in value))
    if isinstance(value, dict):
        return ("d", frozenset((key, _canonical(item)) for key, item in value.items()))
    return ("v", type(value).__name__, value)


def _brief(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def json_path(base: str, key: str | int) -> str:
    """Extend a jsonschema json_path: $.a.b[0], or $['odd key'] for keys that are not identifiers."""
    if isinstance(key, int):
        return f"{base}[{key}]"
    if _PATH_KEY.match(key):
        return f"{base}.{key}"
    return "{}['{}']".format(base, key.replace("\\", "\\\\").replace("'", "\\'"))


def _too_few(limit: int) -> str:
    return "should be non-empty" if limit == 1 else "is too short"


def _too_many(limit: int) -> str:
    return "is expected to be empty" if limit == 0 else "is too long"


def _depth(line: str) -> int:
    path = line.split(": ", 1)[0]
    return path.count(".") + path.count("[")


def schema_keywords(schema: Any) -> set[str]:
    """Every keyword used anywhere in a schema document (property names and $defs names excluded)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                found.add(key)
                if key in ("properties", "patternProperties", "$defs", "dependentSchemas"):
                    if isinstance(value, dict):
                        for sub in value.values():
                            walk(sub)
                elif key not in ("enum", "const", "default", "examples", "required", "dependentRequired",
                                 "$vocabulary"):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


def _patterns_of(schema: Any) -> list[str]:
    """Every pattern and patternProperties key of a schema document."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "pattern" and isinstance(value, str):
                    found.append(value)
                elif key == "patternProperties" and isinstance(value, dict):
                    found.extend(value)
                if key not in ("enum", "const", "default", "examples"):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


# --------------------------------------------------------------------------- the schema set

class SchemaSet:
    """The *.schema.json documents of one folder, addressed by file name or by $id."""

    def __init__(self, directory: str | os.PathLike) -> None:
        self.directory = Path(directory)
        self._by_id: dict[str, Any] | None = None
        self._by_name: dict[str, tuple[str, Any]] = {}
        self._patterns: dict[str, re.Pattern[str]] = {}

    # -- documents

    def _load(self) -> None:
        if self._by_id is not None:
            return
        by_id: dict[str, Any] = {}
        for path in sorted(self.directory.glob("*.schema.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError) as error:
                raise SchemaError(f"cannot load schema {path.name}: {error}") from None
            base = document.get("$id") if isinstance(document, dict) else None
            if not isinstance(base, str) or not base:
                base = path.resolve().as_uri()
            base = urldefrag(base)[0]
            by_id[base] = document
            self._by_name[path.name] = (base, document)
        self._by_id = by_id

    def document(self, name: str) -> Any:
        """A loaded schema document by file name (map.schema.json) or $id."""
        self._load()
        if name in self._by_name:
            return self._by_name[name][1]
        if name in self._by_id:
            return self._by_id[name]
        raise SchemaError(f"no schema {name!r} in {self.directory}")

    def names(self) -> list[str]:
        self._load()
        return sorted(self._by_name)

    def check_supported(self) -> None:
        """Raise SchemaError when a loaded schema uses a keyword this evaluator does not implement
        or a pattern that Python's re cannot compile."""
        self._load()
        for name, (_, document) in sorted(self._by_name.items()):
            unknown = sorted(schema_keywords(document) - KEYWORDS)
            if unknown:
                raise SchemaError(f"unsupported JSON Schema keyword(s) {unknown} in {name}")
            for pattern in _patterns_of(document):
                self._pattern(pattern)

    def _resolve(self, ref: Any, base: str) -> tuple[str, Any, bool]:
        """(document base URI, schema node, whether the node is the document root) of a $ref seen
        in the document whose base URI is ``base``."""
        self._load()
        if not isinstance(ref, str):
            raise SchemaError(f"$ref must be a string, got {_brief(ref)}")
        target = urljoin(base, ref) if base else ref
        document_uri, fragment = urldefrag(target)
        if not document_uri:
            document_uri = base
        if document_uri in self._by_id:
            document = self._by_id[document_uri]
        else:
            name = document_uri.rstrip("/").rsplit("/", 1)[-1]
            if name not in self._by_name:
                raise SchemaError(f"cannot resolve $ref {ref!r} (from {base or self.directory})")
            document_uri, document = self._by_name[name]
        node = document
        if fragment:
            if not fragment.startswith("/"):
                raise SchemaError(f"$ref {ref!r}: plain-name fragments ($anchor) are not supported")
            for part in fragment.split("/")[1:]:
                part = unquote(part).replace("~1", "/").replace("~0", "~")
                try:
                    node = node[int(part)] if isinstance(node, list) else node[part]
                except (KeyError, IndexError, ValueError, TypeError):
                    raise SchemaError(f"cannot resolve $ref {ref!r}: no {part!r} in the target") from None
        return document_uri, node, node is document

    # -- public evaluation

    def errors(self, instance: Any, ref: str) -> list[str]:
        """Every violation of ``ref`` as "$.json.path: message" lines (empty when the instance is valid)."""
        base, schema, root = self._resolve(ref, "")
        out: list[str] = []
        self._check(instance, schema, base, "$", out, 0, root=root)
        return out

    def contract_errors(self, instance: Any, domain: str, definition: str) -> list[str]:
        """errors(instance, "<domain>.schema.json#/$defs/<definition>")."""
        return self.errors(instance, f"{domain}.schema.json#/$defs/{definition}")

    def is_valid(self, instance: Any, ref: str) -> bool:
        return not self.errors(instance, ref)

    # -- the evaluator

    def _sub(self, value: Any, schema: Any, base: str, path: str, depth: int) -> list[str]:
        out: list[str] = []
        self._check(value, schema, base, path, out, depth)
        return out

    def _pattern(self, pattern: str) -> re.Pattern[str]:
        compiled = self._patterns.get(pattern)
        if compiled is None:
            try:
                compiled = self._patterns[pattern] = re.compile(pattern)
            except re.error as error:
                raise SchemaError(f"pattern {pattern!r} is not a valid regular expression: {error}") from None
        return compiled

    def _check(self, value: Any, schema: Any, base: str, path: str, out: list[str], depth: int,
               root: bool = False) -> None:
        if depth > MAX_DEPTH:
            raise SchemaError(f"schema nesting deeper than {MAX_DEPTH} at {path} (a $ref loop?)")
        if schema is True:
            return
        if schema is False:
            out.append(f"{path}: False schema does not allow {_brief(value)}")
            return
        if not isinstance(schema, dict):
            raise SchemaError(f"a schema must be an object or a boolean, got {_brief(schema)} at {path}")
        unknown = set(schema) - KEYWORDS
        if unknown:
            raise SchemaError(f"unsupported JSON Schema keyword(s) {sorted(unknown)} (in {base or self.directory})")
        if "$id" in schema and not root:
            raise SchemaError(f"a nested $id ({schema['$id']!r}) is not supported (in {base or self.directory})")
        depth += 1
        if "$ref" in schema:
            target_base, target, is_root = self._resolve(schema["$ref"], base)
            self._check(value, target, target_base, path, out, depth, root=is_root)
        if "type" in schema and not is_type(value, schema["type"]):
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            out.append(f"{path}: {_brief(value)} is not of type {', '.join(repr(t) for t in types)}")
            return
        if "const" in schema and not json_equal(value, schema["const"]):
            out.append(f"{path}: {_brief(schema['const'])} was expected")
        if "enum" in schema and not any(json_equal(value, item) for item in schema["enum"]):
            out.append(f"{path}: {_brief(value)} is not one of {_brief(schema['enum'], 160)}")
        if _is_number(value):
            self._check_number(value, schema, path, out)
        elif isinstance(value, str):
            self._check_string(value, schema, path, out)
        elif isinstance(value, list):
            self._check_array(value, schema, base, path, out, depth)
        elif isinstance(value, dict):
            self._check_object(value, schema, base, path, out, depth)
        for sub in schema.get("allOf", ()):
            self._check(value, sub, base, path, out, depth)
        for keyword in ("anyOf", "oneOf"):
            if keyword in schema:
                self._check_alternatives(keyword, value, schema, base, path, out, depth)
        if "not" in schema and not self._sub(value, schema["not"], base, path, depth):
            out.append(f"{path}: {_brief(value)} should not be valid under {_brief(schema['not'])}")
        if "if" in schema:
            branch = "then" if not self._sub(value, schema["if"], base, path, depth) else "else"
            if branch in schema:
                self._check(value, schema[branch], base, path, out, depth)

    def _check_alternatives(self, keyword: str, value: Any, schema: dict, base: str, path: str, out: list[str],
                            depth: int) -> None:
        branches = [self._sub(value, sub, base, path, depth) for sub in schema[keyword]]
        passing = sum(1 for branch in branches if not branch)
        if passing == 0:
            failures = [line for branch in branches for line in branch]
            hint = ""
            if failures:
                deepest = max(failures, key=_depth)  # the first of the deepest
                if _depth(deepest) > _depth(path + ": "):
                    hint = f" (closest: {deepest})"
            if not hint and schema.get("description"):
                text = str(schema["description"])
                hint = f" ({text if len(text) <= 160 else text[:160].rsplit(' ', 1)[0] + '...'})"
            out.append(f"{path}: {_brief(value)} is not valid under any of the given schemas{hint}")
        elif keyword == "oneOf" and passing > 1:
            matched = [sub for sub, branch in zip(schema[keyword], branches) if not branch]
            out.append(f"{path}: {_brief(value)} is valid under each of {_brief(matched, 160)}")

    @staticmethod
    def _check_number(value: float, schema: dict, path: str, out: list[str]) -> None:
        if "multipleOf" in schema:
            divisor = schema["multipleOf"]
            if isinstance(divisor, float):
                quotient = value / divisor
                try:
                    failed = int(quotient) != quotient
                except OverflowError:
                    failed = (Fraction(value) / Fraction(divisor)).denominator != 1
            else:
                failed = bool(value % divisor)
            if failed:
                out.append(f"{path}: {value!r} is not a multiple of {divisor}")
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: {value!r} is less than the minimum of {schema['minimum']!r}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: {value!r} is greater than the maximum of {schema['maximum']!r}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append(f"{path}: {value!r} is less than or equal to the minimum of {schema['exclusiveMinimum']!r}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            out.append(f"{path}: {value!r} is greater than or equal to the maximum of {schema['exclusiveMaximum']!r}")

    def _check_string(self, value: str, schema: dict, path: str, out: list[str]) -> None:
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: {_brief(value)} {_too_few(schema['minLength'])}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: {_brief(value)} {_too_many(schema['maxLength'])}")
        if "pattern" in schema and not self._pattern(schema["pattern"]).search(value):
            out.append(f"{path}: {_brief(value)} does not match {schema['pattern']!r}")

    def _check_array(self, value: list, schema: dict, base: str, path: str, out: list[str], depth: int) -> None:
        prefix = schema.get("prefixItems", [])
        for index, (item, sub) in enumerate(zip(value, prefix)):
            self._check(item, sub, base, json_path(path, index), out, depth)
        if "items" in schema and len(value) > len(prefix):
            if schema["items"] is False:
                extra = value[len(prefix):]
                noun = "items" if len(prefix) != 1 else "item"
                rest = extra if len(extra) != 1 else extra[0]
                out.append(f"{path}: Expected at most {len(prefix)} {noun} but found {len(extra)} extra: "
                           f"{_brief(rest)}")
            else:
                for index in range(len(prefix), len(value)):
                    self._check(value[index], schema["items"], base, json_path(path, index), out, depth)
        if "contains" in schema:
            matches = sum(1 for item in value if not self._sub(item, schema["contains"], base, path, depth))
            least, most = schema.get("minContains", 1), schema.get("maxContains")
            if most is not None and matches > most:
                out.append(f"{path}: Too many items match the given schema (expected at most {most})")
            if matches < least:
                out.append(f"{path}: {_brief(value)} does not contain items matching the given schema" if not matches
                           else f"{path}: Too few items match the given schema (expected at least {least} but only "
                                f"{matches} matched)")
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: {_brief(value)} {_too_few(schema['minItems'])}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: {_brief(value)} {_too_many(schema['maxItems'])}")
        if schema.get("uniqueItems") is True and len({_canonical(item) for item in value}) != len(value):
            out.append(f"{path}: {_brief(value)} has non-unique elements")

    def _check_object(self, value: dict, schema: dict, base: str, path: str, out: list[str], depth: int) -> None:
        for key in schema.get("required", ()):
            if key not in value:
                out.append(f"{path}: {key!r} is a required property")
        for key, needed in schema.get("dependentRequired", {}).items():
            if key in value:
                for other in needed:
                    if other not in value:
                        out.append(f"{path}: {other!r} is a dependency of {key!r}")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                self._check(value[key], sub, base, json_path(path, key), out, depth)
        patterns = schema.get("patternProperties", {})
        matched: set[str] = set()
        for pattern, sub in patterns.items():
            compiled = self._pattern(pattern)
            for key in value:
                if compiled.search(key):
                    matched.add(key)
                    self._check(value[key], sub, base, json_path(path, key), out, depth)
        if "additionalProperties" in schema:
            extra = [key for key in value if key not in properties and key not in matched]
            rule = schema["additionalProperties"]
            if rule is False and extra:
                listed = ", ".join(repr(key) for key in sorted(extra, key=str))
                verb = "was" if len(extra) == 1 else "were"
                if patterns:
                    regexes = ", ".join(repr(p) for p in sorted(patterns))
                    out.append(f"{path}: {listed} {'does' if len(extra) == 1 else 'do'} not match any of the regexes: "
                               f"{regexes}")
                else:
                    out.append(f"{path}: Additional properties are not allowed ({listed} {verb} unexpected)")
            elif rule is not True and rule is not False:
                for key in extra:
                    self._check(value[key], rule, base, json_path(path, key), out, depth)
        if "propertyNames" in schema:
            for key in value:
                problems = self._sub(key, schema["propertyNames"], base, path, depth)
                if problems:
                    out.append(f"{path}: property name {key!r} is not allowed ({problems[0].split(': ', 1)[1]})")
        for key, sub in schema.get("dependentSchemas", {}).items():
            if key in value:
                self._check(value, sub, base, path, out, depth)
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            message = "should be non-empty" if schema["minProperties"] == 1 else "does not have enough properties"
            out.append(f"{path}: {_brief(value)} {message}")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            message = "is expected to be empty" if schema["maxProperties"] == 0 else "has too many properties"
            out.append(f"{path}: {_brief(value)} {message}")


# --------------------------------------------------------------------------- shortcuts

@functools.lru_cache(maxsize=None)
def _cached(directory: str) -> SchemaSet:
    return SchemaSet(directory)


def schema_set(directory: str | os.PathLike) -> SchemaSet:
    """The (cached) SchemaSet of a folder of *.schema.json files."""
    return _cached(str(Path(directory).resolve()))


def contract_errors(instance: Any, domain: str, definition: str, *, schema_dir: str | os.PathLike) -> list[str]:
    """Violations of <schema_dir>/<domain>.schema.json#/$defs/<definition> ("$.json.path: message" lines)."""
    return schema_set(schema_dir).contract_errors(instance, domain, definition)

