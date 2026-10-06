"""fx_verify.mjs and the exported fx.v1 runtime, driven from pytest (node marker).

Plan acceptance: the generated slash runtime passes and a Math.random mutant fails. Also: the
node:test suite in tests/js, the JS runtime draws the same geometry as fx_build.py bakes, the
--report is a common qaEnvelope, and the CLI conventions of fx_verify.mjs (usage errors, ASCII console
lines, a skill folder reached through a junction or symlink).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from forge_testutils import (REPO_ROOT, assert_valid_contract, load_script, load_shared, require_node, require_resvg,
                             run_cli, script_path)

pytestmark = pytest.mark.node

fx = load_script("codeart2d", "fx_build")
forge_core = load_shared("forge_core")
VERIFIER = REPO_ROOT / "skills" / "codeart2d" / "scripts" / "fx_verify.mjs"
TEMPLATE = REPO_ROOT / "skills" / "codeart2d" / "references" / "runtime" / "fx-template.mjs"
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
    assert report["inputs"][0]["path"] == "fx-runtime.mjs"
    # D29 (r2-conventions F10c): the envelope's tool version is the package version, not the script's own "1"
    assert report["tool"] == {"name": "codeart2d/fx_verify.mjs", "version": forge_core.FORGE_PACKAGE_VERSION}
    assert {item["id"] for item in report["checks"]} >= {
        "forbidden_apis", "no_randomness_or_clocks", "finite_arguments", "balanced_state", "transparent_outside",
        "within_box", "deterministic", "visible_at_impact", "fullscreen_flash", "thin_strokes"}


def test_fx_verify_cli_conventions(tmp_path):
    result = node(VERIFIER, "--help")
    assert result.returncode == 0 and result.stdout.startswith("usage: node fx_verify.mjs") and result.stdout.isascii()
    missing = node(VERIFIER, tmp_path / "missing.mjs")
    assert missing.returncode == 1 and missing.stderr.startswith("error:") and not missing.stdout
    report = tmp_path / "verify.json"
    report.write_text("{}", encoding="utf-8")
    refused = node(VERIFIER, TEMPLATE, "--report", report)
    assert refused.returncode == 1 and "EEXIST" in refused.stderr and report.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("args, message", [
    ([], "give the fx.v1 module to check (see --help)"),
    (["--bogus-flag"], "unknown option --bogus-flag"),
    (["fx.mjs", "--scale"], "--scale needs a value"),
    (["fx.mjs", "--width", "-4"], "--width must be a positive number"),
    (["a.mjs", "b.mjs"], "unexpected argument b.mjs"),
])
def test_usage_errors_exit_2_with_a_usage_line(args, message):
    """D26 (r2-conventions F10b): an argument error is a usage error, the argparse way: the usage line, then
    'fx_verify.mjs: error: ...', exit 2, nothing on stdout. (A runtime error, such as a missing module, is 1.)"""
    result = node(VERIFIER, *args)
    assert result.returncode == 2, result.stderr
    assert result.stderr.splitlines() == ["usage: node fx_verify.mjs MODULE [--report FILE] [--scale N] [--width W] "
                                          "[--height H]", f"fx_verify.mjs: error: {message}"]
    assert not result.stdout


def _link_directory(link: Path, target: Path) -> None:
    """A directory junction on Windows (no privilege needed), a symbolic link elsewhere."""
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_runs_when_the_skill_folder_is_reached_through_a_junction_or_symlink(tmp_path):
    """r3-platform F2: node resolves links for import.meta.url but not for argv[1], so through a linked skills
    folder (a checkout junctioned or symlinked into ~/.claude/skills or ~/.codex/skills) main() never ran: an
    invalid module 'passed' with exit 0 and no output, and --help printed nothing."""
    linked = tmp_path / "linked-scripts"
    try:
        _link_directory(linked, VERIFIER.parent)
    except OSError as error:
        pytest.skip(f"cannot create a directory link here: {error}")
    try:
        bad = tmp_path / "bad.mjs"
        bad.write_text("export const nothing = 1;\n", encoding="utf-8")
        result = node(linked / VERIFIER.name, bad)
        assert result.returncode == 1 and result.stderr.startswith("error: the module lacks the fx.v1 exports")
        helped = node(linked / VERIFIER.name, "--help")
        assert helped.returncode == 0 and helped.stdout.startswith("usage: node fx_verify.mjs")
        passed = node(linked / VERIFIER.name, TEMPLATE)
        assert passed.returncode == 0 and json.loads(passed.stdout)["status"] == "pass", passed.stderr
    finally:  # remove the link itself, never what it points at
        if sys.platform == "win32":
            os.rmdir(linked)  # removes the junction, not the folder it points to
        else:
            os.unlink(linked)
    assert VERIFIER.is_file()


def test_summary_and_error_lines_are_ascii_in_a_non_ascii_folder(tmp_path):
    """r3-platform F3 / r2-conventions F10a: in a folder named with non-ASCII characters (a zh-TW user name or
    project folder) the summary line carried raw UTF-8. It is one ASCII JSON line now, non-ASCII written as
    JSON \\uXXXX escapes (like Python's json.dumps), decoding back to the real paths; error lines are ASCII."""
    folder = tmp_path / "fx \u6e2c\u8a66 \u00fc"
    folder.mkdir()
    module = folder / "fx-\u6a21\u7d44.mjs"
    module.write_bytes(TEMPLATE.read_bytes())
    report = folder / "\u5831\u544a.json"
    done = subprocess.run([require_node(), str(VERIFIER), str(module), "--report", str(report)], capture_output=True,
                          cwd=REPO_ROOT, timeout=300)
    assert done.returncode == 0, done.stderr
    assert done.stdout.isascii() and len(done.stdout.splitlines()) == 1 and b"\\u6e2c\\u8a66" in done.stdout
    summary = json.loads(done.stdout)
    assert Path(summary["module"]) == module and Path(summary["report"]) == report and summary["status"] == "pass"
    assert json.loads(report.read_text(encoding="utf-8"))["inputs"][0]["path"] == module.name
    failed = subprocess.run([require_node(), str(VERIFIER), str(folder / "\u7f3a.mjs")], capture_output=True,
                            cwd=REPO_ROOT, timeout=300)
    assert failed.returncode == 1 and failed.stderr.startswith(b"error: ") and failed.stderr.isascii()
    assert b"\\u7f3a.mjs" in failed.stderr and not failed.stdout
    usage = subprocess.run([require_node(), str(VERIFIER), "--\u00fcber"], capture_output=True, cwd=REPO_ROOT,
                           timeout=300)
    assert usage.returncode == 2 and usage.stderr.isascii() and b"unknown option --\\u00fcber" in usage.stderr
