"""codeart2d core library: SVG compile/lint, rasterizer doctor, pixel finishing, PixelSpec, QA, review, meta.

All inputs are synthetic. Raster fixtures in tests/fixtures/codeart/raster come from the design
raster-test corpus (see goldens.json); slime.pixelspec.json is the code-generated design prototype
(design/proto/pixelspec_demo.py output).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "codeart2d" / "scripts" / "codeart_core.py"
SPEC = importlib.util.spec_from_file_location("codeart_core", SCRIPT)
assert SPEC and SPEC.loader
core = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = core
SPEC.loader.exec_module(core)

FIXTURES = ROOT / "tests" / "fixtures" / "codeart"
RASTER = FIXTURES / "raster"
GOLDENS = json.loads((RASTER / "goldens.json").read_text(encoding="utf-8"))
SLIME = json.loads((FIXTURES / "slime.pixelspec.json").read_text(encoding="utf-8"))
SVG_HEAD = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
CASES = {case.id: case for case in core.doctor_cases()}
GRID_PALETTE = {"colors": {"o": "#22142b", "s": "#f0c090", "h": "#ffe0b0", "r": "#c03040", "d": "#801830",
                           "b": "#3060c0", "w": "#ffffff"},
                "variants": {"swap": {"r": "#2a8040", "d": "#185028"}}}


def premultiplied_mae(a: np.ndarray, b: np.ndarray) -> float:
    fa, fb = a.astype(np.float64), b.astype(np.float64)
    fa[..., :3] *= fa[..., 3:4] / 255.0
    fb[..., :3] *= fb[..., 3:4] / 255.0
    return float(np.abs(fa - fb).mean())


def load_rgba(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA")).copy()


def class_source() -> str:
    """t16 with its <style> removed: palette classes only, the compiler writes the rules."""
    return re.sub(r"<style>.*?</style>", "", CASES["t16"].svg)


# ----------------------------------------------------------------------------- A3-T1 compile_svg

def test_compile_variants_exact():
    base = core.compile_svg(class_source(), GRID_PALETTE)
    swap = core.compile_svg(class_source(), GRID_PALETTE, "swap")
    rules = re.findall(r"<style>(.*?)</style>", base)
    assert rules == [".c-o{fill:#22142b}.c-s{fill:#f0c090}.c-h{fill:#ffe0b0}.c-w{fill:#ffffff}"
                     ".c-r{fill:#c03040}.c-d{fill:#801830}.c-b{fill:#3060c0}"]
    override = '<style data-codeart-variant="swap">.c-r{fill:#2a8040}.c-d{fill:#185028}</style></svg>'
    assert swap.endswith(override)
    assert swap[:-len(override)] == base[:-len("</svg>")]
    # A source that already has literal-hex class rules gets the same override block.
    assert core.compile_svg(CASES["t16"].svg, GRID_PALETTE, "swap").endswith(override)
    # var() sources compile to the effective variant colours.
    rig = (SVG_HEAD + "<style>svg{--tunic:#3b5dc9;--out:#1a1c2c}.c-shirt{fill:var(--tunic);stroke:var(--out)}</style>"
           '<rect class="c-shirt" x="2" y="2" width="8" height="8"/><rect x="1" y="1" width="2" height="2" '
           'fill="var(--tunic)"/></svg>')
    palette = {"colors": {"tunic": "#3b5dc9"}, "variants": {"red": {"tunic": "#b13e53"}}}
    red = core.compile_svg(rig, palette, "red")
    assert "var(" not in red and "--tunic" not in red
    assert ".c-shirt{fill:#3b5dc9;stroke:#1a1c2c}" in red
    assert red.endswith('<style data-codeart-variant="red">.c-shirt{fill:#b13e53}</style></svg>')
    assert 'fill="#b13e53"' in red
    for svg in (base, swap, red):
        assert core.lint_portable_svg(svg) == []


def test_compile_rejects_unknown_palette_class_and_variant():
    with pytest.raises(core.CodeArtError, match="c-q"):
        core.compile_svg(SVG_HEAD + '<rect class="c-q" width="2" height="2"/></svg>', {"o": "#000000"})
    with pytest.raises(core.CodeArtError, match="unknown palette variant"):
        core.compile_svg(class_source(), GRID_PALETTE, "night")
    with pytest.raises(core.CodeArtError, match="unknown palette colour"):
        core.parse_palette({"colors": {"o": "#000"}, "variants": {"v": {"x": "#fff"}}})


@pytest.mark.parametrize("fragment, kind", [
    ('<path d="M61.13-0.00j,4 L3 4"/>', "complex"),
    ('<polygon points="0,0 (61.1-0.0j),3 4,4"/>', "complex"),
    ('<path d="M nan 3 L 4 4"/>', "NaN"),
    ('<rect x="1" y="inf" width="2" height="2"/>', "NaN"),
    ('<g transform="rotate(NaN 1 2)"><rect width="1" height="1"/></g>', "NaN"),
    ('<rect width="2" height="2" style="stroke-width:-Infinity"/>', "NaN"),
    ('<path d="M 1 2 L 3 4 Q 5 x 6 7"/>', "invalid character"),
])
def test_nan_and_complex_paths_rejected(fragment, kind):
    """Probe-fox finding: x ** 0.65 on -1e-17 wrote '61.13-0.00j' and Chrome silently broke the tail."""
    svg = SVG_HEAD + fragment + "</svg>"
    with pytest.raises(core.CodeArtError, match=kind):
        core.compile_svg(svg)
    assert any(problem.startswith("number:") for problem in core.lint_portable_svg(svg))


def test_clip_ids_prefixed():
    """Probe-fox finding: frames batched in one page shared clipPath ids, so frame 2 used frame 0's clip."""
    frame = (SVG_HEAD + "<style>#body{fill:#c03040}.c-o{stroke:#22142b}</style><defs><clipPath id=\"c1\">"
             '<rect width="4" height="8"/></clipPath><linearGradient id="g"><stop offset="0" stop-color="#000"/>'
             '</linearGradient></defs><rect id="body" class="c-o" width="8" height="8" clip-path="url(#c1)"/>'
             '<rect y="9" width="8" height="2" fill="url(#g)"/></svg>')
    pages = [core.compile_svg(frame, id_prefix=f"f{index}_") for index in range(2)]
    seen: set[str] = set()
    for index, svg in enumerate(pages):
        root = ET.fromstring(svg)
        ids = {element.get("id") for element in root.iter() if element.get("id")}
        assert ids == {f"f{index}_c1", f"f{index}_g", f"f{index}_body"}
        assert not ids & seen
        seen |= ids
        references = re.findall(r"url\(#([^)]+)\)", svg)
        assert references and all(reference in ids for reference in references)
        assert f"#f{index}_body{{fill:#c03040}}" in svg and ".c-o{stroke:#22142b}" in svg
    with pytest.raises(core.CodeArtError, match="id prefix"):
        core.compile_svg(frame, id_prefix="0bad")


def test_use_is_expanded_and_id_styled_reuse_is_refused():
    compiled = core.compile_svg(CASES["t07"].svg)
    assert "<use" not in compiled and "translate(26 2) scale(-1 1)" in compiled
    assert '<svg viewBox="0 0 4 4" width="8" height="4">' in compiled
    styled = (SVG_HEAD + "<style>#leaf{fill:#2a8040}</style><defs><rect id=\"leaf\" width=\"2\" height=\"2\"/></defs>"
              '<use href="#leaf" x="3"/></svg>')
    with pytest.raises(core.CodeArtError, match="classes"):
        core.compile_svg(styled)
    with pytest.raises(core.CodeArtError, match="ancestor"):
        core.compile_svg(SVG_HEAD + '<g id="a"><use href="#a"/></g></svg>')


@pytest.mark.resvg
def test_compiled_svgs_render_exact():
    pytest.importorskip("resvg_py")
    base, _ = core.rasterize(core.compile_svg(class_source(), GRID_PALETTE), 1, "resvg_py")
    swap, _ = core.rasterize(core.compile_svg(class_source(), GRID_PALETTE, "swap"), 1, "resvg_py")
    assert np.array_equal(base, CASES["t16"].truth)
    assert np.array_equal(swap, CASES["t17"].truth)
    # Raw var() sources render black in resvg (raster test t04); compiled they are exact.
    var_source = class_source().replace(">", "><style>" + "".join(
        f".c-{name}{{fill:var(--{name})}}" for name in GRID_PALETTE["colors"]) + "</style>", 1)
    raw, _ = core.rasterize(var_source, 1, "resvg_py")
    assert not np.array_equal(raw, CASES["t16"].truth)
    compiled, _ = core.rasterize(core.compile_svg(var_source, GRID_PALETTE, "swap"), 1, "resvg_py")
    assert np.array_equal(compiled, CASES["t17"].truth)
    used, _ = core.rasterize(core.compile_svg(CASES["t07"].svg), 1, "resvg_py")
    assert np.array_equal(used, CASES["t07"].truth)
    plain, _ = core.rasterize(CASES["t06A"].svg, 1, "resvg_py")
    prefixed, _ = core.rasterize(core.compile_svg(CASES["t06A"].svg, id_prefix="f3_"), 1, "resvg_py")
    assert np.array_equal(plain, prefixed)


def test_inline_css_vars_text_level():
    source = (SVG_HEAD + "<style>svg{--skin:#f0b98c;--line:#1a1c2c}.a{fill:var(--skin)}</style>"
              '<rect class="a" width="2" height="2" style="--w:1;stroke:var(--line)"/>'
              '<rect width="1" height="1" fill="var(--missing, #ff0000)"/></svg>')
    out = core.inline_css_vars(source, {"skin": "#d09070"})
    assert ".a{fill:#d09070}" in out and 'style="stroke:#1a1c2c"' in out and 'fill="#ff0000"' in out
    assert "var(" not in out and "--" not in out
    with pytest.raises(core.CodeArtError, match="--nope"):
        core.inline_css_vars(SVG_HEAD + '<rect fill="var(--nope)"/></svg>')


def test_parse_palette_formats(tmp_path):
    (tmp_path / "p.hex").write_text("1a1c2c\n; comment\nFFFFFF\n", encoding="utf-8")
    (tmp_path / "p.gpl").write_text("GIMP Palette\nName: t\n#\n 26  28  44\toutline\n255 255 255\n", encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps({"colors": [{"hex": "#ff0000", "name": "red"}, {"hex": "#0f0"}],
                                                 "transparent_index": 255}), encoding="utf-8")
    assert core.parse_palette(tmp_path / "p.hex").hex() == {"0": "#1a1c2c", "1": "#ffffff"}
    assert core.parse_palette(tmp_path / "p.gpl").hex() == {"outline": "#1a1c2c", "1": "#ffffff"}
    assert core.parse_palette(tmp_path / "p.json").hex() == {"red": "#ff0000", "1": "#00ff00"}
    palette = core.parse_palette(GRID_PALETTE)
    assert palette.hex("swap")["r"] == "#2a8040" and palette.hex()["r"] == "#c03040"
    assert core.rgba_to_hex("#12345680") == "#12345680" and core.hex_to_rgba("#abc") == (170, 187, 204, 255)
    with pytest.raises(core.CodeArtError):
        core.hex_to_rgba("#12")


# ----------------------------------------------------------------------------- A3-T2 lint

@pytest.mark.parametrize("code, svg", [
    ("css-transform", SVG_HEAD + '<g style="transform:rotate(9deg)"><rect width="4" height="4"/></g></svg>'),
    ("css-transform", SVG_HEAD + "<style>.a{transform:translate(1px,2px)}</style><rect class=\"a\" width=\"4\" "
                                 'height="4"/></svg>'),
    ("transform-origin", SVG_HEAD + '<g transform-origin="2 2" transform="rotate(9)"><rect width="4" height="4"/>'
                                    "</g></svg>"),
    ("transform-origin", SVG_HEAD + '<g style="transform-origin:2px 2px"><rect width="4" height="4"/></g></svg>'),
    ("var", SVG_HEAD + '<rect width="4" height="4" fill="var(--a)"/></svg>'),
    ("text", SVG_HEAD + '<text x="1" y="8">Hi</text></svg>'),
    ("feTurbulence", SVG_HEAD + '<filter id="n"><feTurbulence baseFrequency="0.1"/></filter></svg>'),
    ("feDisplacementMap", SVG_HEAD + '<filter id="d"><feDisplacementMap scale="3"/></filter></svg>'),
    ("mix-blend-mode", SVG_HEAD + '<rect width="4" height="4" style="mix-blend-mode:multiply"/></svg>'),
    ("mix-blend-mode", SVG_HEAD + '<rect width="4" height="4" mix-blend-mode="multiply"/></svg>'),
    ("image", SVG_HEAD + '<image href="data:image/png;base64,AAAA" width="4" height="4"/></svg>'),
    ("viewbox", '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 16 16"/>'),
    ("viewbox", '<svg xmlns="http://www.w3.org/2000/svg" width="16.5" height="16" viewBox="0 0 16.5 16"/>'),
    ("viewbox", '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"/>'),
])
def test_lint_rejects_each_forbidden_construct(code, svg):
    problems = core.lint_portable_svg(svg)
    assert [problem.split(":", 1)[0] for problem in problems] == [code]
    assert len(problems[0]) > len(code) + 20 and problems[0].isascii()


def test_lint_messages_are_specific_and_clean_svgs_pass():
    messages = {}
    for code, svg in [("var", SVG_HEAD + '<rect fill="var(--a)"/></svg>'),
                      ("text", SVG_HEAD + "<text>x</text></svg>"),
                      ("image", SVG_HEAD + '<image width="1" height="1"/></svg>'),
                      ("mix-blend-mode", SVG_HEAD + '<rect style="mix-blend-mode:screen"/></svg>')]:
        messages[code] = core.lint_portable_svg(svg)[0]
    assert len(set(messages.values())) == len(messages)
    for case in CASES.values():
        if case.kind != "lint":
            assert core.lint_portable_svg(case.svg) == [], case.id
    crisp = SVG_HEAD.replace(">", ' shape-rendering="crispEdges">', 1) + '<rect width="4" height="4"/></svg>'
    assert core.lint_portable_svg(crisp, "pixel") == []
    soft = SVG_HEAD + '<defs><linearGradient id="g"/></defs><rect width="4" height="4" opacity="0.5"/></svg>'
    assert [p.split(":")[0] for p in core.lint_portable_svg(soft, "pixel")] == ["crisp-edges", "gradient", "opacity"]
    assert core.lint_portable_svg("<svg")[0].startswith("xml:")


# ----------------------------------------------------------------------------- A3-T3 rasterize + doctor

def test_doctor_corpus_matches_fixtures():
    render_cases = [case for case in core.doctor_cases() if case.kind != "lint"]
    assert [case.id for case in render_cases] == list(GOLDENS["cases"])
    for case in render_cases:
        assert (RASTER / GOLDENS["cases"][case.id]).read_text(encoding="utf-8") == case.svg, case.id
    pinned = core._DOCTOR_PINNED
    assert (pinned["resvg_py"], pinned["platform"]) == (GOLDENS["resvg_py"]["version"], GOLDENS["resvg_py"]["platform"])
    assert all(GOLDENS["resvg_py"]["sha256"][key] == digest for key, digest in pinned["sha256"].items())
    lint_cases = {case.id: case for case in core.doctor_cases() if case.kind == "lint"}
    assert set(lint_cases) == {"t06B", "t06C", "t13B"}
    for case in lint_cases.values():
        assert core.check_doctor_case(case, None)["status"] == "pass"


def test_doctor_probes_pass_on_chrome_references():
    for key, image in GOLDENS["chrome"]["images"].items():
        case = CASES[key.split("@")[0]]
        pixels = load_rgba(RASTER / image)
        assert core.rgba_sha256(pixels) == GOLDENS["chrome"]["sha256"][key]
        result = core.check_doctor_case(case, pixels, 1, {"backend": "chrome", "version": GOLDENS["chrome"]["version"]})
        assert result == {"status": "pass", "method": "probes", "max_probe_error": 0}
        black = np.zeros_like(pixels)
        black[..., 3] = pixels[..., 3]
        assert core.check_doctor_case(case, black, 1, {"backend": "chrome"})["status"] == "fail"


@pytest.mark.resvg
def test_doctor_cases_resvg():
    pytest.importorskip("resvg_py")
    report = core.run_doctor(["resvg_py"])
    assert report["status"] == "pass" and report["primary"] == "resvg_py"
    results = {(r["case"], r["zoom"]): r for r in report["backends"]["resvg_py"]["results"]}
    assert set(results) == {("t01", 1), ("t01", 4), ("t03", 1), ("t06A", 1), ("t07", 1), ("t09", 1), ("t10", 1),
                            ("t13", 1), ("t16", 1), ("t17", 1)}
    for key in [("t01", 1), ("t01", 4), ("t06A", 1), ("t07", 1), ("t16", 1), ("t17", 1)]:
        assert results[key]["mismatch_px"] == 0, key
    assert results[("t03", 1)]["off_palette_px"] == 0 and results[("t03", 1)]["partial_alpha_px"] == 0
    info = core.backend_info("resvg_py")
    pinned = info["version"] == GOLDENS["resvg_py"]["version"] and sys.platform == GOLDENS["resvg_py"]["platform"]
    for case_id in ("t09", "t10", "t13"):
        pixels, render = core.rasterize(CASES[case_id].svg, 1, "resvg_py")
        if pinned:
            assert results[(case_id, 1)]["method"] == "sha256"
            assert core.rgba_sha256(pixels) == GOLDENS["resvg_py"]["sha256"][f"{case_id}@1x"]
        chrome = load_rgba(RASTER / GOLDENS["chrome"]["images"][f"{case_id}@1x"])
        assert premultiplied_mae(pixels, chrome) <= GOLDENS["max_mae_vs_chrome"], case_id
    assert render["name"] == "resvg-py" and render["engine"].startswith("resvg ")
    assert {"t06B", "t06C", "t13B"} == {item["case"] for item in report["lint"] if item["status"] == "pass"}


def test_rasterize_without_backend_gives_pip_hint(monkeypatch):
    def unavailable(name):
        raise core.RasterError(f"{name} missing")

    monkeypatch.setattr(core, "backend_info", unavailable)
    with pytest.raises(core.RasterError, match=r'pip install "resvg-py>=0.5,<0.6"'):
        core.rasterize(CASES["t01"].svg)
    report = core.run_doctor()
    assert report["status"] == "fail" and report["primary"] is None and "pip install" in report["hint"]
    with pytest.raises(core.CodeArtError, match="whole number"):
        core.rasterize(CASES["t01"].svg, 1.3)


def fake_screenshot(calls: list, returncode: int = 0):
    def run(command, **kwargs):
        calls.append(command)
        target = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--screenshot=")), None)
        if target is None:
            target = command[-1]
        if returncode == 0:
            pixels = np.zeros((32, 32, 4), np.uint8)
            pixels[4:8, 4:8] = (200, 10, 10, 255)
            Image.fromarray(pixels).save(target)
        return subprocess.CompletedProcess(command, returncode, "", "boom" if returncode else "")
    return run


def test_chrome_backend_uses_headless_file_url(monkeypatch, tmp_path):
    calls: list = []
    fake = tmp_path / "chrome.exe"
    monkeypatch.setattr(core, "_find_chrome", lambda: str(fake))
    monkeypatch.setattr(core, "_cli_version", lambda executable, label: "154.0.8037.57")
    monkeypatch.setattr(core.subprocess, "run", fake_screenshot(calls))
    pixels, info = core.rasterize(CASES["t01"].svg, 2, "chrome")
    command = calls[0]
    assert command[0] == str(fake) and "--headless" in command and "--default-background-color=00000000" in command
    assert "--force-device-scale-factor=2" in command and "--window-size=16,16" in command
    assert any(arg.startswith("--user-data-dir=") for arg in command)
    assert command[-1].startswith("file:///") and command[-1].endswith("/in.svg")
    assert info == {"backend": "chrome", "name": "chrome", "version": "154.0.8037.57", "zoom": 2, "size": [32, 32]}
    assert pixels[5, 5].tolist() == [200, 10, 10, 255] and pixels[0, 0].tolist() == [0, 0, 0, 0]
    monkeypatch.setattr(core.subprocess, "run", fake_screenshot(calls, returncode=1))
    with pytest.raises(core.RasterError, match="chrome failed"):
        core.rasterize(CASES["t01"].svg, 2, "chrome")


def test_resvg_js_cli_backend_command(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(core.shutil, "which", lambda name: str(tmp_path / name) if name == "resvg-js-cli" else None)
    monkeypatch.setattr(core, "_cli_version", lambda executable, label: "2.6.2")
    monkeypatch.setattr(core.subprocess, "run", fake_screenshot(calls))
    pixels, info = core.rasterize(CASES["t01"].svg, 2, "resvg_js_cli")
    assert calls[0][:4] == [str(tmp_path / "resvg-js-cli"), "--fit-zoom", "2", "--no-system-font"]
    assert info["backend"] == "resvg_js_cli" and info["version"] == "2.6.2" and pixels.shape == (32, 32, 4)


# ----------------------------------------------------------------------------- A3-T4 pixel finishing

SS = 8
SKIN = {"hi": "#ffd8b0", "mid": "#f0b98c", "lo": "#c8845e", "dark": "#8a5236", "out": "#4a2a1c"}
TUNIC = {"hi": "#6d8ef0", "mid": "#3b5dc9", "lo": "#29366f", "dark": "#1c2450", "out": "#101530"}
PANTS = {"hi": "#6a7ab0", "mid": "#4a5a85", "lo": "#333c57", "dark": "#232a40", "out": "#141824"}
OUTLINE = "#1a1c2c"


def supersampled(size: int = 64) -> tuple[np.ndarray, np.ndarray]:
    ys, xs = np.mgrid[0:size * SS, 0:size * SS]
    return (xs + 0.5) / SS, (ys + 0.5) / SS


def ellipse(cx, cy, rx, ry, size=64):
    x, y = supersampled(size)
    return ((((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2) <= 1.0).astype(np.uint8) * 255


def capsule(x0, y0, x1, y1, width, size=64):
    x, y = supersampled(size)
    dx, dy = x1 - x0, y1 - y0
    t = np.clip(((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy), 0, 1)
    return (np.hypot(x - (x0 + t * dx), y - (y0 + t * dy)) <= width / 2).astype(np.uint8) * 255


def creature_slots() -> list[dict]:
    """A 64 px biped from analytic coverage (what a rig renders per slot at 8x), back to front."""
    return [
        {"name": "arm_back", "alpha": capsule(24, 34, 15, 41, 4.5), "ramp": SKIN, "group": "arm_b"},
        {"name": "leg_l", "alpha": capsule(27, 47, 25, 58, 5.5), "ramp": PANTS, "group": "leg_l"},
        {"name": "leg_r", "alpha": capsule(37, 47, 40, 58, 5.5), "ramp": PANTS, "group": "leg_r"},
        {"name": "body", "alpha": ellipse(32, 40, 11, 10), "ramp": TUNIC, "group": "torso"},
        {"name": "head", "alpha": ellipse(32, 22, 9.5, 9), "ramp": SKIN, "group": "head"},
        {"name": "arm_front", "alpha": capsule(40, 34, 50, 41, 4.5), "ramp": SKIN, "group": "arm_f"},
    ]


EYES = [(29, 21, OUTLINE), (35, 21, OUTLINE)]
CREATURE_PALETTE = [colour for ramp in (SKIN, TUNIC, PANTS) for colour in ramp.values()] + [OUTLINE]


@pytest.mark.parametrize("mode, outline_colours", [("solid", [OUTLINE]),
                                                  ("selout", [OUTLINE, SKIN["out"], TUNIC["out"], PANTS["out"]])])
def test_pixel_finish_qa(mode, outline_colours):
    rgba, report = core.pixel_finish(creature_slots(), (64, 64), outline=mode, outline_color=OUTLINE, stamps=EYES)
    qa = core.qa_pixels(rgba, CREATURE_PALETTE, outline_colours)
    assert qa["partial_alpha"] == 0 and qa["off_palette"] == 0 and qa["outline_gaps"] == 0
    assert qa["l_corners"] <= 10
    assert report["stages"] == ["coverage", "labels", "cleanup", "shading", "inner_lines", "outline"]
    assert report["inner_line_px"] > 0 and report["outline_px"] > 0 and report["border_contact_px"] == 0
    assert rgba[21, 29].tolist() == [0x1A, 0x1C, 0x2C, 255]
    shades = {tuple(c) for c in rgba[rgba[..., 3] > 0][:, :3].tolist()}
    assert {core.hex_to_rgba(SKIN[k])[:3] for k in ("hi", "mid", "lo")} <= shades
    flat, _ = core.pixel_finish(creature_slots(), (64, 64), light=None, outline="none", inner_lines=False)
    assert core.qa_pixels(flat)["colors"] == 3


def test_outline_after_cleanup_order():
    """Roadmap 4.7: removing orphans after outlining opened 18-37 outline gaps per frame."""
    slots = creature_slots()
    speck = np.zeros((64 * SS, 64 * SS), np.uint8)
    speck[4 * SS:5 * SS, 60 * SS:61 * SS] = 255  # an isolated 1 px sliver of a slot
    slots.append({"name": "speck", "alpha": speck, "fill": SKIN["mid"]})
    rgba, report = core.pixel_finish(slots, (64, 64), outline_color=OUTLINE, stamps=EYES)
    assert report["cleanup"]["specks_removed"] == 1
    assert not rgba[2:7, 58:63, 3].any(), "the speck and its outline must be gone"
    order = report["stages"]
    assert order.index("cleanup") < order.index("shading") < order.index("inner_lines") < order.index("outline")
    assert core.qa_pixels(rgba, CREATURE_PALETTE, OUTLINE)["outline_gaps"] == 0
    # The wrong order: clean orphans after outlining. Diagonal outline pixels are 4-neighbour
    # orphans, so removing them opens gaps in the silhouette.
    wrong = rgba.copy()
    keys = core._pack(wrong)
    same = sum((core._neighbour(keys, dy, dx, 0) == keys).astype(int) for dy, dx in core._DIRS4)
    wrong[(wrong[..., 3] > 0) & (same == 0)] = 0
    assert core.qa_pixels(wrong, CREATURE_PALETTE, OUTLINE)["outline_gaps"] > 0


def test_pixel_finish_rejects_inputs_that_break_the_contract():
    slots = creature_slots()[:2]
    flat_light, _ = core.pixel_finish(slots, (64, 64), light=(0, 0), outline="none", inner_lines=False)
    flat_none, _ = core.pixel_finish(slots, (64, 64), light=None, outline="none", inner_lines=False)
    assert np.array_equal(flat_light, flat_none)
    with pytest.raises(core.CodeArtError, match="opaque"):
        core.pixel_finish([{"alpha": slots[0]["alpha"], "fill": "#ff000080"}], (64, 64))
    with pytest.raises(core.CodeArtError, match="opaque"):
        core.pixel_finish(slots, (64, 64), stamps=[(3, 3, "#00000000")])
    with pytest.raises(core.CodeArtError, match="bands"):
        core.pixel_finish(slots, (64, 64), bands=(0.7, 0.3, 0.9))
    with pytest.raises(core.CodeArtError, match="exactly one"):
        core.pixel_finish([{"alpha": slots[0]["alpha"]}], (64, 64))
    with pytest.raises(core.CodeArtError, match="coverage must be"):
        core.pixel_finish([{"alpha": slots[0]["alpha"][:-8], "fill": "#ff0000"}], (64, 64))


@pytest.mark.perf
def test_pixel_finish_perf():
    slots = creature_slots()
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        core.pixel_finish(slots, (64, 64), stamps=EYES)
        timings.append(time.perf_counter() - start)
    assert sorted(timings)[2] <= 0.050


def test_add_outline_modes_and_snap_exact():
    shape = np.zeros((6, 8, 4), np.uint8)
    shape[2:4, 1:4] = (200, 50, 50, 255)
    shape[2:4, 4:7] = (50, 200, 50, 255)
    solid = core.add_outline(shape, "#000000")
    assert solid[1, 2].tolist() == [0, 0, 0, 255] and solid[1, 0].tolist() == [0, 0, 0, 0]
    selout = core.add_outline(shape, "#000000", "selout", selout={(200, 50, 50): "#400000", "#32c832": "#004000"})
    assert selout[1, 2].tolist() == [64, 0, 0, 255] and selout[1, 5].tolist() == [0, 64, 0, 255]
    assert selout[2, 0].tolist() == [64, 0, 0, 255] and selout[2, 7].tolist() == [0, 64, 0, 255]
    assert np.array_equal(core.add_outline(shape, "#000", "none"), shape)
    with pytest.raises(core.CodeArtError):
        core.add_outline(shape, "#000", "selout")
    noisy = shape.copy()
    noisy[2, 1] = (203, 47, 52, 140)
    noisy[3, 6] = (50, 200, 50, 90)
    snapped = core.snap_exact(noisy, ["#c83232", "#32c832"])
    assert snapped[2, 1].tolist() == [200, 50, 50, 255] and snapped[3, 6].tolist() == [0, 0, 0, 0]
    assert core.qa_pixels(snapped, ["#c83232", "#32c832"])["off_palette"] == 0
    with pytest.raises(core.CodeArtError, match="RGB units"):
        core.snap_exact(noisy, ["#000000"], max_distance=20)


# ----------------------------------------------------------------------------- A3-T5 PixelSpec

# sha256 of the RGBA pixels; equal to design/proto/out/pixelspec/<variant>/frames/<frame>.png.
SLIME_PIXELS = {
    ("green", "rest"): "bb4073b297493b56", ("green", "squash"): "bb98c3f7b3f48653",
    ("green", "stretch"): "778ab05b35d413f3", ("blue", "rest"): "de9cc2cc9534e3de",
    ("red", "rest"): "1db9b6775a8b1800", ("red", "stretch"): "6ee788115e2a1fde",
}


def test_slime_spec_renders_three_variants():
    masks = {}
    for variant in ("green", "blue", "red"):
        palette = list(core.parse_palette({"colors": SLIME["palette"], "variants": SLIME["variants"]})
                       .resolve(variant).values())
        for frame in SLIME["frames"]:
            pixels = core.render_pixelspec(SLIME, frame["name"], variant)
            qa = core.qa_pixels(pixels, palette, SLIME["palette"]["k"])
            assert (qa["partial_alpha"], qa["off_palette"], qa["outline_gaps"]) == (0, 0, 0)
            masks.setdefault(frame["name"], pixels[..., 3])
            assert np.array_equal(masks[frame["name"]], pixels[..., 3]), "variants change colours only"
            if (variant, frame["name"]) in SLIME_PIXELS:
                assert core.rgba_sha256(pixels).startswith(SLIME_PIXELS[(variant, frame["name"])])
    assert np.array_equal(core.render_pixelspec(SLIME, 0, "green"), core.render_pixelspec(SLIME, "rest-2", "green"))
    assert not np.array_equal(core.render_pixelspec(SLIME, 0, "blue"), core.render_pixelspec(SLIME, 0, "red"))


def small_spec(**changes) -> dict:
    spec = {"schema": "codeart2d.pixelspec.v1", "canvas": [12, 8], "palette": {"k": "#101010", "a": "#d04040",
            "b": "#4040d0", "x": "#00000000"},
            "layers": [{"name": "body", "z": 0, "origin": [2, 2], "rows": ["aaa", "aab", "aaa"]},
                       {"name": "badge", "z": 1, "origin": [3, 3], "rows": ["bx"], "mirror_safe": False}],
            "outline": {"mode": "solid", "color": "k"}}
    spec.update(changes)
    return spec


def test_overflow_raises():
    with pytest.raises(core.CodeArtError, match="overflows"):
        core.render_pixelspec(small_spec(), {"name": "push", "layers": {"body": {"dx": 8}}})
    with pytest.raises(core.CodeArtError, match="outline would overflow"):
        core.render_pixelspec(small_spec(), {"name": "edge", "dx": -2})
    with pytest.raises(core.CodeArtError, match="not in the palette"):
        core.render_pixelspec(small_spec(), {"layers": {"body": {"rows": ["aq"]}}})
    with pytest.raises(core.CodeArtError, match="schema"):
        core.render_pixelspec(small_spec(schema="other"))
    with pytest.raises(core.CodeArtError, match="unknown layer"):
        core.render_pixelspec(small_spec(), {"name": "f", "layers": {"cape": {"dx": 1}}})


def test_pixelspec_layers_mirror_flip_segments_and_selout():
    base = core.render_pixelspec(small_spec(outline={"mode": "none"}))
    assert base[3, 4].tolist() == [0, 0, 0, 0], "an alpha-0 palette colour erases lower layers"
    assert base[3, 3].tolist() == [0x40, 0x40, 0xD0, 255] and base[3, 2].tolist() == [0xD0, 0x40, 0x40, 255]
    flipped = core.render_pixelspec(small_spec(outline={"mode": "none"}), {"name": "left", "flip_x": True})
    # The body mirrors with the frame (row "aab" becomes "baa" at x 7..9); the mirror_safe=false
    # badge "bx" moves to x 7..8 but keeps its orientation, so its erase pixel lands on x 8.
    assert flipped[3, 7:10].tolist() == [[0x40, 0x40, 0xD0, 255], [0, 0, 0, 0], [0xD0, 0x40, 0x40, 255]]
    assert flipped[2, 7:10, 3].tolist() == [255, 255, 255] and not flipped[:, :7, 3].any()
    mirrored = core.render_pixelspec(small_spec(outline={"mode": "none"}), {"layers": {"body": {"mirror": True}}})
    assert mirrored[3, 2].tolist() == [0x40, 0x40, 0xD0, 255]
    hidden = core.render_pixelspec(small_spec(outline={"mode": "none"}), {"layers": {"badge": {"hidden": True}}})
    assert hidden[3, 3].tolist() == [0xD0, 0x40, 0x40, 255]
    shifted = core.render_pixelspec(small_spec(outline={"mode": "none"}), {"dx": 1, "dy": 1})
    assert np.array_equal(shifted[1:, 1:], base[:-1, :-1])
    wide_rows = ["." * 3 + "a" * 70 + "b" * 5 + "." * 2]
    wide = small_spec(canvas=[84, 3], outline={"mode": "none"},
                      layers=[{"name": "strip", "origin": [1, 1], "rows": wide_rows}])
    rle = small_spec(canvas=[84, 3], outline={"mode": "none"},
                     layers=[{"name": "strip", "origin": [1, 1], "segments": ["3.70a5b2."]}])
    assert np.array_equal(core.render_pixelspec(wide), core.render_pixelspec(rle))
    selout = small_spec(palette={"k": "#101010", "a": "#d04040", "A": "#601010", "b": "#4040d0", "x": "#00000000"},
                        outline={"mode": "selout", "color": "k", "map": {"a": "A"}})
    out = core.render_pixelspec(selout)
    assert out[1, 3].tolist() == [0x60, 0x10, 0x10, 255]  # next to fill "a": its mapped shade "A"
    # The erased pixel (4, 3) borders "a" (-> "A") and the unmapped "b" (-> "k"): the darkest wins.
    assert out[3, 4].tolist() == [0x10, 0x10, 0x10, 255]


def test_deterministic_bytes(tmp_path):
    for run in range(2):
        for frame in SLIME["frames"]:
            pixels = core.render_pixelspec(SLIME, frame["name"], "red")
            core.save_png(pixels, tmp_path / f"run{run}" / f"{frame['name']}.png")
    for frame in SLIME["frames"]:
        first = (tmp_path / "run0" / f"{frame['name']}.png").read_bytes()
        assert first == (tmp_path / "run1" / f"{frame['name']}.png").read_bytes()
        chunks, position = [], 8
        while position < len(first):
            length, kind = struct.unpack(">I4s", first[position:position + 8])
            chunks.append(kind.decode())
            position += 12 + length
        assert chunks[0] == "IHDR" and chunks[-1] == "IEND" and set(chunks) == {"IHDR", "IDAT", "IEND"}
        with Image.open(tmp_path / "run0" / f"{frame['name']}.png") as image:
            assert image.mode == "RGBA"


# ----------------------------------------------------------------------------- A3-T6 QA and grid

def test_qa_pixels_metrics():
    image = np.zeros((7, 7, 4), np.uint8)
    image[1:6, 1:6] = (10, 10, 10, 255)
    image[2:5, 2:5] = (200, 100, 50, 255)
    image[3, 3] = (1, 2, 3, 255)
    image[0, 6] = (200, 100, 50, 128)
    qa = core.qa_pixels(image, ["#0a0a0a", "#c86432"], "#0a0a0a")
    assert qa["visible"] == 26 and qa["partial_alpha"] == 1 and qa["colors"] == 4 and qa["off_palette"] == 1
    assert qa["orphans"] == 2 and qa["orphan_examples"] == [[6, 0], [3, 3]]
    # A full 5x5 border has 4 doubled corners (a 4-neighbour outline would not); the partial
    # pixel outside the outline is the one gap.
    assert qa["l_corners"] == 4 and qa["outline_gaps"] == 1 and qa["bbox"] == [1, 0, 7, 6]
    gap = image.copy()
    gap[1, 3] = 0
    assert core.qa_pixels(gap, outline="#0a0a0a")["outline_gaps"] == 2
    bent = np.zeros((5, 5, 4), np.uint8)
    bent[1, 1:4] = bent[1:4, 3] = (0, 0, 0, 255)
    assert core.qa_pixels(bent, outline=[(0, 0, 0)])["l_corners"] == 1
    assert core.qa_pixels(np.zeros((3, 3, 4), np.uint8))["bbox"] is None


def test_detect_grid_nearest6x():
    frame = core.render_pixelspec(SLIME, "rest", "green")
    result = core.detect_grid(core.upscale_nearest(frame, 6))
    assert result["period"] == 6 and result["phase"] == [0, 0]
    assert result["score"] == 1.0 and result["uniformity"] == 1.0 and result["has_grid"]
    cropped = core.detect_grid(core.upscale_nearest(frame, 6)[5:, 2:])
    assert cropped["period"] == 6 and cropped["phase"] == [4, 1]
    assert core.detect_grid(core.upscale_nearest(frame, 5))["period"] == 5


def painted_crop(seed: int, size: int = 96) -> np.ndarray:
    """Continuous-tone stand-in for an image-model crop: smooth colour waves plus sensor-like noise."""
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:size, 0:size].astype(float)
    rgb = np.full((size, size, 3), 128.0)
    for channel in range(3):
        for _ in range(6):
            fx, fy = rng.uniform(0.01, 0.12, 2)
            rgb[..., channel] += 25 * np.sin(2 * np.pi * (fx * xs + fy * ys) + rng.uniform(0, 6.3))
    rgb += rng.normal(0, 18, rgb.shape)
    out = np.full((size, size, 4), 255, np.uint8)
    out[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    return out


def test_detect_grid_no_grid_on_painted_crop():
    for seed in range(3):
        result = core.detect_grid(painted_crop(seed))
        assert result["score"] < 0.05 and not result["has_grid"], result
    flat = core.detect_grid(np.full((16, 16, 4), 255, np.uint8))
    assert flat["period"] == 1 and flat["score"] == 0.0


# ----------------------------------------------------------------------------- A3-T7 review + meta

def test_review_sheet_dimensions():
    frames = [core.render_pixelspec(SLIME, name, "blue")[8:32, 8:24] for name in ("rest", "squash")]
    qa = {"partial_alpha": 0, "off_palette": 0, "outline_gaps": 0}
    sheet = core.review_sheet(frames, palette=SLIME["palette"], qa=qa, anchor=(8, 23))
    width, height, count, scales = 16, 24, 2, (1, 2, 4)
    content = max(core.REVIEW_MIN_CONTENT, count * width * 4 + (count - 1) * core.REVIEW_GAP)
    expected_height = (2 * core.REVIEW_PAD + core.REVIEW_LINE * (2 + len(qa))
                       + sum(core.REVIEW_LINE + height * s + core.REVIEW_GAP for s in scales) * 3
                       + core.REVIEW_LINE + height * 4 + core.REVIEW_GAP
                       + core.REVIEW_LINE + core.REVIEW_SWATCH + core.REVIEW_GAP)
    assert sheet.size == (content + 2 * core.REVIEW_PAD, expected_height) == (240, 864)
    assert sheet.mode == "RGBA"
    wide = core.review_sheet([np.zeros((10, 70, 4), np.uint8)] * 3, scales=(2,), backgrounds=("dark",), onion=False)
    assert wide.size == (2 * core.REVIEW_PAD + 3 * 140 + 2 * core.REVIEW_GAP,
                         2 * core.REVIEW_PAD + 2 * core.REVIEW_LINE + core.REVIEW_LINE + 20 + core.REVIEW_GAP)
    pixels = np.asarray(sheet)
    top = core.REVIEW_PAD + core.REVIEW_LINE * 5 + core.REVIEW_LINE
    cell = pixels[top:top + height, core.REVIEW_PAD:core.REVIEW_PAD + width]
    assert np.array_equal(cell[frames[0][..., 3] > 0][:, :3], frames[0][frames[0][..., 3] > 0][:, :3])


CODEART_META_FALLBACK_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["art_source", "generator", "spec_sha256", "renderer", "palette", "outputs", "qa"],
    "properties": {
        "art_source": {"const": "code"},
        "generator": {"type": "string", "minLength": 1},
        "spec_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "renderer": {"type": "object", "required": ["name", "version"],
                     "properties": {"name": {"type": "string"}, "version": {"type": "string"}}},
        "outputs": {"type": "array", "items": {"type": "object", "required": ["path", "sha256"], "properties": {
            "path": {"type": "string"}, "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "bytes": {"type": "integer", "minimum": 0}}}},
        "qa": {"type": "object"},
    },
}


def test_meta_validates_against_schema(tmp_path):
    """Validates against plan Appendix B codeart_meta_v1, and the vendored A0 schema once it exists."""
    jsonschema = pytest.importorskip("jsonschema")
    frame = core.render_pixelspec(SLIME, "rest", "green")
    core.save_png(frame, tmp_path / "frames" / "rest.png")
    spec_sha = core._local_sha256_file(FIXTURES / "slime.pixelspec.json")
    renderer = {"name": "codeart_core.render_pixelspec", "version": core.CODEART_CORE_API_VERSION}
    palette = {"colors": SLIME["palette"], "variants": SLIME["variants"]}
    meta = core.write_codeart_meta(tmp_path / "codeart-meta.json", generator="test", spec_sha256=spec_sha,
                                   renderer=renderer, palette=palette, outputs=[tmp_path / "frames" / "rest.png"],
                                   qa=core.qa_pixels(frame))
    written = json.loads((tmp_path / "codeart-meta.json").read_text(encoding="utf-8"))
    assert written == meta and written["art_source"] == "code" and written["disclosure"] == core.DISCLOSURE
    assert written["outputs"][0]["path"] == "frames/rest.png"
    assert written["palette"]["variants"]["blue"]["g"] == "#3b5dc9"
    jsonschema.Draft202012Validator(CODEART_META_FALLBACK_SCHEMA).validate(written)
    vendored = ROOT / "skills" / "codeart2d" / "references" / "schemas"
    if (vendored / "codeart.schema.json").is_file():
        from referencing import Registry, Resource

        resources = []
        for path in vendored.glob("*.schema.json"):
            document = json.loads(path.read_text(encoding="utf-8"))
            resources.append((document.get("$id", path.name), Resource.from_contents(document)))
        codeart = json.loads((vendored / "codeart.schema.json").read_text(encoding="utf-8"))
        assert "codeart_meta_v1" in codeart.get("$defs", {}), "Appendix B names the def codeart_meta_v1"
        validator = jsonschema.Draft202012Validator(
            {"$ref": f"{codeart.get('$id', 'codeart.schema.json')}#/$defs/codeart_meta_v1"},
            registry=Registry().with_resources(resources))
        validator.validate(written)
    with pytest.raises(FileExistsError):
        core.write_codeart_meta(tmp_path / "codeart-meta.json", generator="test", spec_sha256=spec_sha,
                                renderer=renderer, palette=SLIME["palette"], outputs=[], qa={})
    with pytest.raises(core.CodeArtError, match="core codeart-meta"):
        core.write_codeart_meta(tmp_path / "other.json", generator="test", spec_sha256=spec_sha, renderer=renderer,
                                palette=SLIME["palette"], outputs=[], qa={}, extra={"art_source": "api"})
