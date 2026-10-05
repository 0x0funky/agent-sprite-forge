"""codeart2d svg_render.py render / lint / doctor (plan B18-T2) and the SVG example (B18-T4).

Tests that rasterize are marked resvg and skip without resvg-py. No test launches a browser:
doctor runs are limited to --backend resvg_py, fake backends are monkeypatched, and the
clean-machine cases hide every backend (resvg_py import, resvg-js-cli on PATH, CHROME_PATH).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from forge_testutils import (
    SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, require_resvg, run_cli, script_path,
)

SVG = load_script("codeart2d", "svg_render")
core = SVG.codeart_core
EXAMPLES = SKILLS_DIR / "codeart2d" / "examples"
POTIONS = EXAMPLES / "potion-icons.svg"
PALETTE = EXAMPLES / "palette.json"
HEAD = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
BLOCKER = """import os, runpy, shutil, sys
for name in filter(None, sys.argv[1].split(",")):
    sys.modules[name] = None
hidden = set(filter(None, os.environ.get("BLOCK_EXECUTABLES", "").split(",")))
real_which = shutil.which
shutil.which = lambda name, *args, **kwargs: None if name in hidden else real_which(name, *args, **kwargs)
script = sys.argv[2]
sys.argv = [script, *sys.argv[3:]]
runpy.run_path(script, run_name="__main__")
"""


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def svg_cli(capsys, *argv) -> tuple[int, str, str]:
    """svg_render.main in-process: (exit code, stdout, stderr)."""
    code = SVG.main([str(item) for item in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def summary_of(out: str) -> dict:
    lines = out.strip().splitlines()
    assert len(lines) == 1 and lines[0].isascii(), out
    return json.loads(lines[0])


def rgba(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA")).copy()


def write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def crisp_badge(colour: str = "#d04040") -> str:
    return (HEAD + '<rect class="c-ink" x="2" y="2" width="12" height="12"/>'
            f'<rect x="5" y="5" width="6" height="6" fill="{colour}"/></svg>')


BADGE_PALETTE = {"colors": {"ink": "#202030", "red": "#d04040"}, "variants": {"night": {"ink": "#101018"}}}


def run_blocked(tmp_path: Path, modules: str, *argv, hide_backends: bool = False):
    blocker = write(tmp_path, "blocker.py", BLOCKER)
    env = {}
    if hide_backends:
        env = {"BLOCK_EXECUTABLES": "resvg-js-cli", "CHROME_PATH": str(tmp_path / "no-browser.exe")}
    return run_cli([blocker, modules, script_path("codeart2d", "svg_render"), *argv], "cp1252", cwd=tmp_path, env=env)


# ----------------------------------------------------------------------------- CLI basics

def test_svg_render_help_is_ascii_under_cp1252_and_cp950():
    assert_cli_help("codeart2d", "svg_render")
    for command in ("render", "lint", "doctor"):
        for encoding in ("cp1252", "cp950"):
            result = run_cli([script_path("codeart2d", "svg_render"), command, "--help"], encoding)
            assert result.returncode == 0 and result.stdout.isascii() and "usage:" in result.stdout, result.stderr


def test_render_refuses_an_existing_output_dir(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    code, stdout, stderr = svg_cli(capsys, "render", "--svg", POTIONS, "--output-dir", out)
    assert code == 1 and stdout == "" and stderr.startswith("error: refusing to replace existing output")
    assert list(out.iterdir()) == []


@pytest.mark.resvg
def test_render_strict_qc_failure_publishes_nothing(tmp_path, capsys):
    """A crisp render with a colour outside the palette fails strict QC and leaves nothing behind."""
    require_resvg()
    svg = write(tmp_path, "badge.svg", crisp_badge("#00ff00"))
    palette = write(tmp_path, "palette.json", json.dumps(BADGE_PALETTE))
    code, stdout, stderr = svg_cli(capsys, "render", "--svg", svg, "--palette", palette, "--output-dir",
                                   tmp_path / "out", "--crisp", "--backend", "resvg_py", "--strict-qc")
    assert code == 1 and stdout == "" and "strict QC failed" in stderr and "off_palette 36" in stderr
    assert not (tmp_path / "out").exists() and not [p for p in tmp_path.iterdir() if ".stage-" in p.name]
    code, stdout, stderr = svg_cli(capsys, "render", "--svg", svg, "--palette", palette, "--output-dir",
                                   tmp_path / "loose", "--crisp", "--backend", "resvg_py")
    assert code == 0 and summary_of(stdout)["qa"] == "fail" and "warning: QA status fail" in stderr


def test_render_stops_on_profile_problems_before_rasterizing(tmp_path, capsys):
    text = write(tmp_path, "text.svg", HEAD + '<text x="1" y="9">hi</text></svg>')
    code, stdout, stderr = svg_cli(capsys, "render", "--svg", text, "--output-dir", tmp_path / "out")
    assert code == 1 and "breaks the portable profile" in stderr and "text: <text> renders" in stderr
    gradient = write(tmp_path, "gradient.svg", HEAD + '<defs><linearGradient id="g"><stop offset="0" '
                     'stop-color="#ffffff"/></linearGradient></defs><rect width="4" height="4" fill="url(#g)"/></svg>')
    code, _, stderr = svg_cli(capsys, "render", "--svg", gradient, "--output-dir", tmp_path / "out", "--crisp")
    assert code == 1 and "breaks the pixel profile" in stderr and "gradient:" in stderr
    assert not (tmp_path / "out").exists()


def test_render_refuses_outputs_inside_the_skill_and_fractional_crisp_zoom(tmp_path, capsys):
    code, _, stderr = svg_cli(capsys, "render", "--svg", POTIONS, "--output-dir", EXAMPLES / "never-created")
    assert code == 1 and "not inside the codeart2d skill folder" in stderr
    code, _, stderr = svg_cli(capsys, "render", "--svg", POTIONS, "--output-dir", tmp_path / "out", "--crisp",
                              "--zoom", "2.5")
    assert code == 1 and "integer nearest only" in stderr


# ----------------------------------------------------------------------------- render (resvg-py)

@pytest.mark.resvg
def test_potion_example_renders_every_variant_as_vector_art(tmp_path, capsys):
    """B18-T4: the flat vector icon set renders in each palette variant at --zoom 4."""
    require_resvg()
    out = tmp_path / "potions"
    code, stdout, stderr = svg_cli(capsys, "render", "--svg", POTIONS, "--palette", PALETTE, "--output-dir", out,
                                   "--zoom", "4", "--backend", "resvg_py", "--strict-qc")
    assert code == 0, stderr
    summary = summary_of(stdout)
    assert summary["variants"] == ["health", "mana", "venom"] and summary["mode"] == "vector"
    meta = read_json(out / "codeart-meta.json")
    assert_valid_contract(meta, "codeart", "codeart_meta_v1", skill="codeart2d")
    assert meta["renderer"]["backend"] == "resvg_py" and meta["renderer"]["zoom"] == 4
    assert meta["renderer"]["size"] == [512, 128] and meta["spec_sha256"] == SVG.forge_core.sha256_file(POTIONS)
    assert meta["palette"]["variants"]["mana"]["liquid"] == "#3b6fe0" and meta["svg"]["palette_source"] == "file"
    assert meta["qa"]["status"] == "pass" and [check["id"] for check in meta["qa"]["checks"]] == ["lint"]
    assert any("anti-aliased" in text for text in meta["qa"]["notProven"])
    health = (out / "health" / "potion-icons.svg").read_text(encoding="utf-8")
    alphas = []
    for variant in ("health", "mana", "venom"):
        compiled = (out / variant / "potion-icons.svg").read_text(encoding="utf-8")
        assert core.lint_portable_svg(compiled) == [] and "<use" not in compiled
        if variant != "health":
            assert compiled.startswith(health[:-len("</svg>")]), "a variant only appends its override block"
            assert f'<style data-codeart-variant="{variant}">' in compiled
        pixels = rgba(out / variant / "potion-icons.png")
        assert pixels.shape == (128, 512, 4)
        alphas.append(pixels[..., 3])
    assert all(np.array_equal(alphas[0], alpha) for alpha in alphas[1:]), "variants change colours only"


@pytest.mark.resvg
def test_crisp_route_is_exact_pixel_art_and_byte_identical(tmp_path, capsys):
    require_resvg()
    spec = tmp_path / "potion-icons.svg"
    spec.write_bytes(POTIONS.read_bytes())
    palette = tmp_path / "palette.json"
    palette.write_bytes(PALETTE.read_bytes())
    for run in ("run1", "run2"):
        code, stdout, stderr = svg_cli(capsys, "render", "--svg", spec, "--palette", palette, "--output-dir",
                                       tmp_path / run, "--crisp", "--zoom", "4", "--backend", "resvg_py", "--strict-qc")
        assert code == 0, stderr
    out = tmp_path / "run1"
    meta = read_json(out / "codeart-meta.json")
    assert_valid_contract(meta, "codeart", "codeart_meta_v1", skill="codeart2d")
    checks = {check["id"]: check for check in meta["qa"]["checks"]}
    assert checks["lint"]["profile"] == "pixel" and checks["partial_alpha"]["value"] == 0
    assert checks["off_palette"]["value"] == 0 and meta["renderer"]["zoom"] == 1
    for variant in ("health", "mana", "venom"):
        small = rgba(out / variant / "potion-icons.png")
        assert small.shape == (32, 128, 4) and set(np.unique(small[..., 3]).tolist()) == {0, 255}
        assert np.array_equal(rgba(out / variant / "potion-icons@4x.png"), core.upscale_nearest(small, 4))
        assert 'shape-rendering="crispEdges"' in (out / variant / "potion-icons.svg").read_text(encoding="utf-8")
    first = sorted(path.relative_to(out) for path in out.rglob("*") if path.is_file())
    assert len(first) == 10
    for relative in first:
        assert (out / relative).read_bytes() == (tmp_path / "run2" / relative).read_bytes(), relative


@pytest.mark.resvg
def test_render_without_a_palette_uses_the_colours_the_svg_writes(tmp_path, capsys):
    require_resvg()
    svg = write(tmp_path, "plain.svg", HEAD.replace('viewBox="0 0 16 16"', 'viewBox="0 0 16 16" data-anchor="8 15"')
                + '<style>.ink{fill:#202030}</style><rect class="ink" x="2" y="2" width="12" height="13"/>'
                '<rect x="5" y="5" width="6" height="6" style="fill:#D04040"/></svg>')
    out = tmp_path / "out"
    code, stdout, stderr = svg_cli(capsys, "render", "--svg", svg, "--output-dir", out, "--crisp", "--zoom", "2",
                                   "--backend", "resvg_py", "--strict-qc")
    assert code == 0, stderr
    meta = read_json(out / "codeart-meta.json")
    assert meta["palette"]["colors"] == {"0": "#202030", "1": "#d04040"}
    assert meta["svg"]["palette_source"] == "svg-colours"
    images = meta["svg"]["images"]
    assert meta["svg"]["anchor_px"] == [8, 15] and [image["anchor_px"] for image in images] == [[8, 15], [16, 30]]
    assert summary_of(stdout)["variants"] == ["base"] and (out / "base" / "plain@2x.png").is_file()
    code, _, stderr = svg_cli(capsys, "render", "--svg", svg, "--output-dir", tmp_path / "x", "--variants", "night")
    assert code == 1 and "needs --palette" in stderr
    code, _, stderr = svg_cli(capsys, "render", "--svg", svg, "--output-dir", tmp_path / "y", "--anchor", "20,4")
    assert code == 1 and "outside the 16x16 canvas" in stderr


@pytest.mark.resvg
def test_palette_classes_and_variants_compile_exactly(tmp_path, capsys):
    require_resvg()
    svg = write(tmp_path, "badge.svg", crisp_badge())
    palette = write(tmp_path, "palette.json", json.dumps(BADGE_PALETTE))
    out = tmp_path / "out"
    code, stdout, stderr = svg_cli(capsys, "render", "--svg", svg, "--palette", palette, "--output-dir", out,
                                   "--crisp", "--variants", "base,night", "--backend", "resvg_py", "--strict-qc")
    assert code == 0, stderr
    base, night = rgba(out / "base" / "badge.png"), rgba(out / "night" / "badge.png")
    assert base[2, 2].tolist() == [0x20, 0x20, 0x30, 255] and night[2, 2].tolist() == [0x10, 0x10, 0x18, 255]
    assert base[8, 8].tolist() == night[8, 8].tolist() == [0xD0, 0x40, 0x40, 255]
    assert base[0, 0].tolist() == [0, 0, 0, 0]


# ----------------------------------------------------------------------------- lint

def test_lint_passes_the_examples(capsys):
    code, stdout, stderr = svg_cli(capsys, "lint", "--svg", POTIONS)
    assert code == 0 and stderr == ""
    assert summary_of(stdout) == {"status": "pass", "svg": str(POTIONS), "profile": "portable", "compiled": False,
                                  "problems": []}
    code, stdout, stderr = svg_cli(capsys, "lint", "--svg", POTIONS, "--profile", "pixel")
    assert code == 1 and "crisp-edges: the pixel profile needs" in stderr, "render --crisp adds it"


@pytest.mark.parametrize("code_name, fragment, message", [
    ("css-transform", '<g style="transform:rotate(20deg)"><rect width="2" height="2"/></g>',
     "use the transform attribute"),
    ("transform-origin", '<g transform-origin="4 4" transform="rotate(20)"><rect width="2" height="2"/></g>',
     "resvg-js draws the part off-canvas"),
    ("var", '<rect width="2" height="2" style="fill:var(--skin)"/>', "inline palette colours"),
    ("text", '<text x="1" y="9">hi</text>', "convert the text to paths"),
    ("feTurbulence", '<filter id="n"><feTurbulence baseFrequency="0.2"/></filter>', "bake noise in code"),
    ("feDisplacementMap", '<filter id="d"><feDisplacementMap scale="4"/></filter>', "bake the displacement"),
    ("mix-blend-mode", '<rect width="2" height="2" style="mix-blend-mode:multiply"/>', "blend layers in Python"),
])
def test_lint_rejects_each_construct_with_a_specific_message(tmp_path, capsys, code_name, fragment, message):
    """Roadmap P0-5: lint --profile portable blocks each construct with its own message."""
    svg = write(tmp_path, "art.svg", HEAD + fragment + "</svg>")
    code, stdout, stderr = svg_cli(capsys, "lint", "--svg", svg, "--profile", "portable")
    assert code == 1
    problems = summary_of(stdout)["problems"]
    hit = [problem for problem in problems if problem.startswith(f"{code_name}: ")]
    assert hit and message in hit[0], problems
    assert f"{code_name}: " in stderr and "error: " in stderr and "Traceback" not in stderr


def test_lint_compile_lints_what_render_rasterizes(tmp_path, capsys):
    source = write(tmp_path, "vars.svg", HEAD + '<style>.c-skin{fill:var(--skin)}</style>'
                   '<rect class="c-skin" width="4" height="4"/><use href="#x"/>'
                   '<rect id="x" width="1" height="1"/></svg>')
    palette = write(tmp_path, "palette.json", json.dumps({"colors": {"skin": "#f0c090"}}))
    code, stdout, _ = svg_cli(capsys, "lint", "--svg", source)
    assert code == 1 and any(problem.startswith("var: ") for problem in summary_of(stdout)["problems"])
    code, stdout, stderr = svg_cli(capsys, "lint", "--svg", source, "--compile", "--palette", palette)
    assert code == 0, stderr
    assert summary_of(stdout)["compiled"] is True
    undefined = write(tmp_path, "undefined.svg", HEAD + '<rect width="2" height="2" style="fill:var(--nope)"/></svg>')
    code, stdout, _ = svg_cli(capsys, "lint", "--svg", undefined, "--compile")
    assert code == 1 and summary_of(stdout)["problems"][0].startswith("compile: undefined CSS variable --nope")
    code, _, stderr = svg_cli(capsys, "lint", "--svg", source, "--palette", palette)
    assert code == 1 and "apply with --compile" in stderr


# ----------------------------------------------------------------------------- doctor

@pytest.mark.resvg
def test_doctor_passes_on_resvg_py_and_writes_a_qa_envelope(tmp_path, capsys):
    require_resvg()
    report = tmp_path / "doctor.json"
    code, stdout, stderr = svg_cli(capsys, "doctor", "--backend", "resvg_py", "--report", report)
    assert code == 0, stderr
    summary = summary_of(stdout)
    assert summary == {"status": "pass", "primary": "resvg_py", "backends": {"resvg_py": "pass"}, "failed": [],
                       "report": str(report.resolve())}
    data = read_json(report)
    assert_valid_contract(data, "common", "qaEnvelope", skill="codeart2d")
    assert {"cases", "backends", "primary", "lint"} <= set(data), "the codeart_core.run_doctor shape is kept"
    ids = {check["id"]: check["status"] for check in data["checks"]}
    assert ids["primary"] == "pass" and ids["resvg_py:t01@4x"] == "pass" and ids["resvg_py:t17@1x"] == "pass"
    assert ids["lint:t06B"] == ids["lint:t06C"] == ids["lint:t13B"] == "pass"
    code, _, stderr = svg_cli(capsys, "doctor", "--backend", "resvg_py", "--report", report)
    assert code == 1 and "refusing to replace existing file" in stderr


@pytest.mark.resvg
def test_doctor_exits_non_zero_when_the_primary_backend_deviates(tmp_path, capsys, monkeypatch):
    require_resvg()
    real = core.rasterize
    t01 = core.doctor_cases()[0]

    def off_by_one(svg, zoom=1, backend="auto", **kwargs):
        pixels, info = real(svg, zoom, backend, **kwargs)
        if svg == t01.svg:
            pixels = pixels.copy()
            pixels[0, 0] = (1, 2, 3, 255)
        return pixels, info

    monkeypatch.setattr(core, "rasterize", off_by_one)
    report = tmp_path / "doctor.json"
    code, stdout, stderr = svg_cli(capsys, "doctor", "--backend", "resvg_py", "--report", report)
    assert code == 1 and summary_of(stdout)["status"] == "fail"
    assert "error: the primary backend resvg_py deviates on resvg_py:t01@1x, resvg_py:t01@4x" in stderr
    data = read_json(report)
    assert_valid_contract(data, "common", "qaEnvelope", skill="codeart2d")
    assert data["status"] == "fail", "the report is written even when the doctor fails"


@pytest.mark.resvg
def test_a_deviating_fallback_backend_is_only_a_warning(tmp_path, capsys, monkeypatch):
    require_resvg()
    real_info, real_raster = core.backend_info, core.rasterize

    def info(name):
        return {"backend": name, "name": "fake-browser", "version": "1"} if name == "chrome" else real_info(name)

    def raster(svg, zoom=1, backend="auto", **kwargs):
        if backend == "chrome":
            pixels, details = real_raster(svg, zoom, "resvg_py", **kwargs)
            return np.zeros_like(pixels), {**details, "backend": "chrome"}
        return real_raster(svg, zoom, backend, **kwargs)

    monkeypatch.setattr(core, "backend_info", info)
    monkeypatch.setattr(core, "rasterize", raster)
    report = tmp_path / "doctor.json"
    code, stdout, stderr = svg_cli(capsys, "doctor", "--backend", "resvg_py", "--backend", "chrome", "--report", report)
    assert code == 0, stderr
    assert summary_of(stdout)["backends"] == {"resvg_py": "pass", "chrome": "fail"}
    data = read_json(report)
    assert_valid_contract(data, "common", "qaEnvelope", skill="codeart2d")
    statuses = {check["id"]: check["status"] for check in data["checks"]}
    assert statuses["chrome:t01@1x"] == "warn" and statuses["resvg_py:t01@1x"] == "pass"


# ----------------------------------------------------------------------------- clean machines

def test_without_any_rasterizer_the_cli_prints_the_pip_hint(tmp_path):
    """Acceptance B18-T2: with resvg blocked (and no fallback), a hint instead of a traceback."""
    result = run_blocked(tmp_path, "resvg_py", "doctor", "--report", "doctor.json", hide_backends=True)
    assert result.returncode == 1 and "Traceback" not in result.stderr
    assert 'python -m pip install "resvg-py>=0.5,<0.6"' in result.stderr
    assert summary_of(result.stdout)["backends"] == {"resvg_py": "unavailable", "resvg_js_cli": "unavailable",
                                                     "chrome": "unavailable"}
    report = read_json(tmp_path / "doctor.json")
    assert report["status"] == "fail" and report["primary"] is None and "pip install" in report["hint"]
    result = run_blocked(tmp_path, "resvg_py", "render", "--svg", str(POTIONS), "--palette", str(PALETTE),
                         "--output-dir", "out", hide_backends=True)
    assert result.returncode == 1 and "Traceback" not in result.stderr
    assert "no SVG rasterizer available" in result.stderr and "pip install" in result.stderr
    assert not (tmp_path / "out").exists()
    result = run_blocked(tmp_path, "resvg_py", "lint", "--svg", str(POTIONS), hide_backends=True)
    assert result.returncode == 0, "lint needs no rasterizer"


def test_without_numpy_the_cli_prints_the_pip_command(tmp_path):
    result = run_blocked(tmp_path, "numpy", "lint", "--svg", str(POTIONS))
    assert result.returncode == 1 and "Traceback" not in result.stderr
    assert "missing Python module(s): numpy" in result.stderr and "python -m pip install numpy" in result.stderr
    result = run_blocked(tmp_path, "numpy", "doctor", "--help")
    assert result.returncode == 0 and "usage:" in result.stdout


def test_oversized_renders_are_refused_before_rasterizing(tmp_path, capsys):
    code, _, stderr = svg_cli(capsys, "render", "--svg", POTIONS, "--palette", PALETTE, "--output-dir",
                              tmp_path / "out", "--zoom", "200")
    assert code == 1 and "lower --zoom" in stderr and not (tmp_path / "out").exists()
