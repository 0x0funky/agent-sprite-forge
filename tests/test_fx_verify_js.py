"""fx_verify.mjs and the exported fx.v1 runtime, driven from pytest (node marker).

Plan acceptance: the generated slash runtime passes and a Math.random mutant fails. Also: the
node:test suite in tests/js, the JS runtime draws the same geometry as fx_build.py bakes, the
--report is a common qaEnvelope, and the CLI conventions of fx_verify.mjs.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from forge_testutils import REPO_ROOT, assert_valid_contract, load_script, require_node, require_resvg, run_cli, script_path

pytestmark = pytest.mark.node

fx = load_script("codeart2d", "fx_build")
VERIFIER = REPO_ROOT / "skills" / "codeart2d" / "scripts" / "fx_verify.mjs"
JS_SUITE = REPO_ROOT / "tests" / "js" / "fx-verify.test.mjs"
EXAMPLE = REPO_ROOT / "skills" / "codeart2d" / "examples" / "slash.fx.json"
TRACE = """
import { pathToFileURL } from 'node:url';
const [file, queries] = process.argv.slice(1);
const runtime = await import(pathToFileURL(file).href);
const out = JSON.parse(queries).map(([id, t, seed]) => runtime.shapes(id, t, seed === null ? undefined : { seed }));
process.stdout.write(JSON.stringify(out));
"""


def node(*args: str | Path, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run([require_node(), *map(str, args)], capture_output=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=REPO_ROOT)


@pytest.fixture(scope="module")
def slash_runtime(tmp_path_factory) -> Path:
    """The example spec baked with --export-runtime (fx_build runs fx_verify itself when node is present)."""
    require_node()
    require_resvg()
    output = tmp_path_factory.mktemp("fx") / "slash"
    result = run_cli([script_path("codeart2d", "fx_build"), "--spec", EXAMPLE, "--output-dir", output,
                      "--export-runtime", "--strict-qc"])
    assert result.returncode == 0, result.stderr
    return output


def test_node_test_suite_passes():
    result = node("--test", JS_SUITE, timeout=600)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout


@pytest.mark.resvg
def test_generated_slash_runtime_passes(slash_runtime):
    checks = {item["id"]: item for item in json.loads((slash_runtime / "fx-report.json").read_text())["qa"]["checks"]}
    assert checks["runtime_verify"]["status"] == "pass"
    report = json.loads((slash_runtime / "fx-verify.json").read_text(encoding="utf-8"))
    assert report["status"] == "pass" and [effect["id"] for effect in report["effects"]] == ["slash", "impact", "dust", "orb"]
    result = node(VERIFIER, slash_runtime / "fx-runtime.mjs")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "pass" and summary["failed"] == [] and summary["report"] is None


@pytest.mark.resvg
def test_math_random_mutant_fails(slash_runtime, tmp_path):
    source = (slash_runtime / "fx-runtime.mjs").read_text(encoding="utf-8")
    original = "const angle = (p.angle + (hash01(first, i) - 0.5) * p.spread) * DEG;"
    assert original in source
    mutant = tmp_path / "mutant.mjs"
    mutant.write_text(source.replace(original, "const angle = (p.angle + (Math.random() - 0.5) * p.spread) * DEG;"),
                      encoding="utf-8")
    result = node(VERIFIER, mutant)
    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert summary["status"] == "fail"
    assert {"forbidden_apis", "no_randomness_or_clocks"} <= set(summary["failed"])
    assert result.stderr.startswith("error: fx.v1 verification failed:")


@pytest.mark.resvg
def test_runtime_geometry_matches_the_baked_shapes(slash_runtime):
    """shapes(id, t, {seed}) in JS equals fx_build.effect_shapes in Python: same numbers to 1e-9, same colours,
    same draw order, nothing outside the lifetime, loops wrapped, per-instance seeds applied."""
    spec = fx.FxSpec(json.loads(EXAMPLE.read_text(encoding="utf-8")), base=EXAMPLE.parent)
    queries = [[effect["id"], t, None] for effect in spec.effects
               for t in (-5, 0, 1, 12.5, 25, 74.9, 149.999, 150, 150.001, 175, 233.3, 299, 333, 399.999, 400, 812.5)]
    queries += [[effect["id"], 120, 99] for effect in spec.effects]
    result = node("--input-type=module", "-e", TRACE, slash_runtime / "fx-runtime.mjs", json.dumps(queries))
    assert result.returncode == 0, result.stderr
    compared = 0

    def same(python, javascript):
        nonlocal compared
        if isinstance(python, dict):
            assert python.keys() == javascript.keys()
            for key in python:
                same(python[key], javascript[key])
        elif isinstance(python, list):
            assert len(python) == len(javascript)
            for a, b in zip(python, javascript):
                same(a, b)
        elif isinstance(python, float) or isinstance(javascript, float):
            assert python == pytest.approx(javascript, abs=1e-9)
            compared += 1
        else:
            assert python == javascript

    effects = {effect["id"]: effect for effect in spec.effects}
    for (effect_id, t, seed), shapes in zip(queries, json.loads(result.stdout)):
        effect = effects[effect_id]
        local = t % effect["durationMs"] if effect["loop"] and t >= 0 else t if 0 <= t < effect["durationMs"] else None
        expected = [] if local is None else fx.effect_shapes(effect, local, None if seed is None else effect["seed"] ^ seed)
        same(expected, shapes)
    assert compared > 1000


@pytest.mark.resvg
def test_verify_report_is_a_qa_envelope(slash_runtime):
    report = json.loads((slash_runtime / "fx-verify.json").read_text(encoding="utf-8"))
    assert_valid_contract(report, "common", "qaEnvelope", skill="codeart2d")
    assert report["inputs"][0]["path"] == "fx-runtime.mjs" and report["tool"]["name"] == "codeart2d/fx_verify.mjs"
    assert {item["id"] for item in report["checks"]} >= {
        "forbidden_apis", "no_randomness_or_clocks", "finite_arguments", "balanced_state", "transparent_outside",
        "within_box", "deterministic", "visible_at_impact", "fullscreen_flash", "thin_strokes"}


def test_fx_verify_cli_conventions(tmp_path):
    result = node(VERIFIER, "--help")
    assert result.returncode == 0 and result.stdout.startswith("usage: node fx_verify.mjs") and result.stdout.isascii()
    missing = node(VERIFIER, tmp_path / "missing.mjs")
    assert missing.returncode == 1 and missing.stderr.startswith("error:") and not missing.stdout
    assert node(VERIFIER).stderr.startswith("error: give the fx.v1 module")
    template = REPO_ROOT / "skills" / "codeart2d" / "references" / "runtime" / "fx-template.mjs"
    report = tmp_path / "verify.json"
    report.write_text("{}", encoding="utf-8")
    refused = node(VERIFIER, template, "--report", report)
    assert refused.returncode == 1 and "EEXIST" in refused.stderr and report.read_text(encoding="utf-8") == "{}"
