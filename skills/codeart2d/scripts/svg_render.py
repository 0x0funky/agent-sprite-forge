"""Render, lint and check portable SVG code art (codeart2d).

Run from the project root; outputs go to new paths inside the project:
  python "<skill-dir>/scripts/svg_render.py" render --svg potion-icons.svg --palette palette.json --output-dir out/potions-v1 --zoom 4
  python "<skill-dir>/scripts/svg_render.py" render --svg potion-icons.svg --palette palette.json --output-dir out/potions-px --crisp --zoom 4 --anchor 16,31 --strict-qc
  python "<skill-dir>/scripts/svg_render.py" lint --svg potion-icons.svg --profile portable
  python "<skill-dir>/scripts/svg_render.py" doctor --report doctor.json

render  Compiles the SVG with the palette (literal-hex class rules; a variant appends one
        override <style>), lints the result (portable profile; pixel profile with --crisp)
        and stops on any lint problem, rasterizes it, and writes per variant
        <variant>/<name>.svg (the compiled SVG that was rendered) and <variant>/<name>.png,
        plus codeart-meta.json (renderer name and version, every output, a QA envelope).
        Vector mode renders at --zoom. --crisp is the pixel-art route: the root gets
        shape-rendering="crispEdges" when it has none, the art renders at 1 unit = 1 px,
        and --zoom N adds <name>@Nx.png by integer nearest upscaling; QA then requires
        0 partial-alpha and 0 off-palette pixels, and --strict-qc publishes nothing
        otherwise. Without --palette, the palette is the #hex colours the SVG writes.
        Variants: all (default: every palette variant, or the base palette), base, or names.
lint    Lists constructs that render differently across resvg and Chrome, one
        "<code>: <message>" line each; exit 1 when any is found. --compile lints what
        render rasterizes (palette applied, var() inlined, <use> expanded).
doctor  Renders the conformance corpus (t01, t03, t06A, t07, t09, t10, t13, t16, t17; lint
        cases t06B, t06C, t13B) on every available backend, or --backend ones; exit 1
        unless the primary backend passes every case. --report writes the full report,
        a QA envelope, even when the doctor fails.

Backends, in auto order: resvg-py (python -m pip install "resvg-py>=0.5,<0.6"), the
resvg-js CLI on PATH, Chrome/Edge (set CHROME_PATH when it is not found). PyMuPDF,
skia-python and cairosvg are never used: they silently ignore crispEdges, <style>,
clipPath or gradients.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
from typing import Any, Sequence
import xml.etree.ElementTree as ET

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    import numpy as np
    import codeart_core
    import forge_core
except ImportError as _import_error:  # a clean machine: main() prints the pip command instead of a traceback
    _MISSING: ImportError | None = _import_error
else:
    _MISSING = None

TOOL_NAME = "svg_render.py"
TOOL_VERSION = "0.4.0"
SKILL_DIR = SCRIPTS_DIR.parent
META_NAME = "codeart-meta.json"
BASE_VARIANT = "base"
BACKEND_CHOICES = ("auto", "resvg_py", "resvg_js_cli", "chrome")
MAX_CRISP_ZOOM = 64
MAX_IMAGE_PIXELS = 1 << 26  # 64 Mpx (256 MB as RGBA): the largest PNG written
_WINDOWS_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])")
_COLOUR_DECLARATION = re.compile(r"(?:^|[;{\s])(?:fill|stroke|stop-color|flood-color|lighting-color|color)\s*:\s*"
                                 r"(#[0-9A-Fa-f]{3,8})\b")
RENDER_NOT_PROVEN = (
    "Appearance, readability and appeal; look at the PNGs at game size.",
    "Agreement with other renderers on this machine; run svg_render.py doctor (renders are bit-exact only for "
    "the recorded renderer version).",
    "That the art matches the brief and the intent of the SVG.",
)
DOCTOR_METHOD = ("svg_render.py doctor: codeart_core.run_doctor renders the conformance corpus on each backend; "
                 "exact cases compare every pixel with the truth image (t01 also at 4x), the palette case checks "
                 "colours and alpha, golden cases compare pinned resvg-py hashes or Chrome-measured probe pixels, "
                 "and lint cases must be rejected by the portable lint. Only the primary backend gates the status; "
                 "deviations of fallback backends are warnings")
DOCTOR_NOT_PROVEN = (
    "SVG features outside the corpus, text and fonts (text is outside the portable profile).",
    "Bit-exact goldens are pinned for resvg-py 0.5.0 on win32 only; elsewhere probes allow 8/255.",
    "Rendering speed and memory use.",
)


class QAFailure(Exception):
    """Strict QA failed; nothing is published."""


# ----------------------------------------------------------------------------- helpers

def _local_safe_stem(name: str, used: set[str], fallback: str) -> str:
    """A file-system-safe, case-insensitively unique stem (Windows reserved names get a suffix)."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or fallback
    if _WINDOWS_RESERVED.fullmatch(stem.split(".", 1)[0]):
        stem += "_"
    candidate, counter = stem, 2
    while candidate.lower() in used:
        candidate, counter = f"{stem}-{counter}", counter + 1
    used.add(candidate.lower())
    return candidate


def _local_file_ref(path: Path, base: Path) -> dict:
    """fileRef relative to `base` (POSIX); a file on another drive is recorded by its name."""
    try:
        relative = Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:  # Windows: another drive has no relative path
        relative = path.name
    return {"path": relative, "sha256": forge_core.sha256_file(path), "bytes": path.stat().st_size}


def _local_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def _local_publish_file(target: Path, data: Any) -> None:
    """Write JSON beside `target`, then publish it without ever replacing an existing file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(handle)
    temporary = Path(name)
    try:
        forge_core.write_json(temporary, data, no_clobber=False)
        forge_core.publish_file_no_replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def read_svg(path: Path) -> tuple[str, bytes]:
    if not path.is_file():
        raise codeart_core.CodeArtError(f"SVG not found: {path}")
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig"), raw
    except UnicodeDecodeError:
        raise codeart_core.CodeArtError(f"{path.name} is not UTF-8 text") from None


def select_variants(palette: Any, text: str) -> list[tuple[str, str | None]]:
    """(label, variant name or None for the base palette) per requested variant, in order."""
    named = list(palette.variants) if palette is not None else []
    if text.strip() == "all":
        return [(name, name) for name in named] or [(BASE_VARIANT, None)]
    chosen: list[tuple[str, str | None]] = []
    for token in (part.strip() for part in text.split(",")):
        if not token:
            continue
        if token in named:
            item = (token, token)
        elif token == BASE_VARIANT:
            item = (BASE_VARIANT, None)
        elif palette is None:
            raise codeart_core.CodeArtError(f"variant {token!r} needs --palette with variants")
        else:
            raise codeart_core.CodeArtError(f"unknown variant {token!r}; the palette has: "
                                            f"{', '.join([BASE_VARIANT] + named)} (or use all)")
        if item not in chosen:
            chosen.append(item)
    if not chosen:
        raise codeart_core.CodeArtError("--variants needs 'all', 'base' or variant names")
    return chosen


def svg_colours(svg: str) -> list[str]:
    """The #hex paint colours an SVG writes (attributes, style attributes and <style> rules), in order."""
    root = ET.fromstring(svg)
    found: list[str] = []
    for element in root.iter():
        for name in codeart_core.COLOUR_PROPERTIES:
            value = (element.get(name) or "").strip()
            if value.startswith("#"):
                found.append(value)
        css = (element.get("style") or "") + (";" + (element.text or "") if element.tag.endswith("}style") else "")
        found += _COLOUR_DECLARATION.findall(css)
    colours = []
    for value in found:
        try:
            colours.append(codeart_core.rgba_to_hex(value))
        except codeart_core.CodeArtError:
            continue
    return list(dict.fromkeys(colours))


def _crisp(svg: str) -> str:
    """Add shape-rendering="crispEdges" to the root <svg> when it sets none."""
    head = re.search(r"<svg\b[^>]*>", svg)
    if head is None or re.search(r"\sshape-rendering\s*=", head.group(0)):
        return svg
    return svg[:head.start()] + '<svg shape-rendering="crispEdges"' + svg[head.start() + 4:]


def _root_canvas(svg: str) -> tuple[int, int, dict]:
    """Logical canvas (the integer viewBox size, which lint ties to the root size) and root attributes."""
    root = ET.fromstring(svg)
    parts = [float(part) for part in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if part]
    return int(parts[2]), int(parts[3]), dict(root.attrib)


def _point(text: str | None, label: str) -> tuple[float, float] | None:
    if text is None:
        return None
    parts = [part for part in re.split(r"[\s,]+", text.strip()) if part]
    try:
        x, y = (float(part) for part in parts)
    except ValueError:
        raise codeart_core.CodeArtError(f"{label} must be two numbers x,y, got {text!r}") from None
    if not all(np.isfinite([x, y])):
        raise codeart_core.CodeArtError(f"{label} must be finite")
    return x, y


# ----------------------------------------------------------------------------- render

def cmd_render(args: argparse.Namespace) -> dict:
    final = Path(args.output_dir)
    if os.path.lexists(final):
        raise FileExistsError(f"refusing to replace existing output {final}; choose a new --output-dir")
    if _local_inside(final, SKILL_DIR):
        raise codeart_core.CodeArtError("write outputs inside your project, not inside the codeart2d skill folder")
    if not (np.isfinite(args.zoom) and args.zoom > 0):
        raise codeart_core.CodeArtError("--zoom must be a positive number")
    zoom = int(args.zoom) if float(args.zoom).is_integer() else float(args.zoom)
    if args.crisp and (not isinstance(zoom, int) or not 1 <= zoom <= MAX_CRISP_ZOOM):
        raise codeart_core.CodeArtError(f"--crisp scales by integer nearest only: --zoom must be a whole number from "
                                        f"1 to {MAX_CRISP_ZOOM}")
    svg_path = Path(args.svg)
    source, raw = read_svg(svg_path)
    palette = codeart_core.parse_palette(Path(args.palette)) if args.palette else None
    variants = select_variants(palette, args.variants)
    profile = "pixel" if args.crisp else "portable"
    compiled = {}
    for label, variant in variants:
        text = codeart_core.compile_svg(source, palette, variant)
        compiled[label] = _crisp(text) if args.crisp else text
        problems = codeart_core.lint_portable_svg(compiled[label], profile)
        if problems:
            raise codeart_core.CodeArtError(f"the compiled SVG ({label}) breaks the {profile} profile, nothing was "
                                            "rendered: " + "; ".join(problems))
    if palette is None:
        colours = svg_colours(compiled[variants[0][0]])
        if not colours:
            raise codeart_core.CodeArtError("no palette: pass --palette, or write fill/stroke colours as #rrggbb")
        palette = codeart_core.parse_palette(colours)
        palette_source = "svg-colours"
    else:
        palette_source = "file"
    width, height, root = _root_canvas(compiled[variants[0][0]])
    if width * height * zoom ** 2 > MAX_IMAGE_PIXELS:
        raise codeart_core.CodeArtError(f"{width}x{height} at --zoom {zoom} would exceed {MAX_IMAGE_PIXELS} pixels; "
                                        "lower --zoom")
    anchor = _point(args.anchor, "--anchor") if args.anchor else _point(root.get("data-anchor"), "data-anchor")
    if anchor is not None and not (0 <= anchor[0] <= width and 0 <= anchor[1] <= height):
        raise codeart_core.CodeArtError(f"anchor {list(anchor)} lies outside the {width:g}x{height:g} canvas")
    render_zoom = 1 if args.crisp else zoom
    upscale = zoom if args.crisp and zoom > 1 else 0

    used: set[str] = set()
    stem = _local_safe_stem(svg_path.stem, set(), "art")
    variant_dirs = {label: _local_safe_stem(label, used, "variant") for label, _ in variants}
    with forge_core.staged_output(final) as stage:
        outputs, images, renderer = [], [], None
        for label, variant in variants:
            folder = stage / variant_dirs[label]
            folder.mkdir(parents=True)
            svg_out = folder / f"{stem}.svg"
            svg_out.write_text(compiled[label], encoding="utf-8", newline="\n")
            pixels, info = codeart_core.rasterize(compiled[label], render_zoom, args.backend)
            renderer = renderer or info
            png = folder / f"{stem}.png"
            codeart_core.save_png(pixels, png)
            outputs += [svg_out, png]
            colours = list(palette.resolve(variant).values())
            image, _ = forge_core.load_rgba(png)
            metrics = codeart_core.qa_pixels(np.asarray(image), colours)
            images.append({"variant": label, "file": png.relative_to(stage).as_posix(), "scale": render_zoom,
                           "size": list(pixels.shape[1::-1]),
                           "anchor_px": [anchor[0] * render_zoom, anchor[1] * render_zoom] if anchor else None,
                           **{key: metrics[key] for key in ("visible", "partial_alpha", "off_palette", "colors")}})
            if upscale:
                big = folder / f"{stem}@{upscale}x.png"
                codeart_core.save_png(codeart_core.upscale_nearest(pixels, upscale), big)
                outputs.append(big)
                images.append({"variant": label, "file": big.relative_to(stage).as_posix(), "scale": upscale,
                               "size": [pixels.shape[1] * upscale, pixels.shape[0] * upscale],
                               "anchor_px": [anchor[0] * upscale, anchor[1] * upscale] if anchor else None,
                               "from": png.relative_to(stage).as_posix(), "method": "integer nearest"})
        inputs = [_local_file_ref(svg_path, stage)] + ([_local_file_ref(Path(args.palette), stage)]
                                                       if args.palette else [])
        qa = render_envelope(images, profile, args.crisp, inputs, [_local_file_ref(path, stage) for path in outputs])
        if args.strict_qc and qa["status"] != "pass":
            failing = [f"{check['id']} {check['value']} in {', '.join(check['failing'][:3])}"
                       for check in qa["checks"] if check["status"] == "fail"]
            raise QAFailure("strict QC failed, nothing was published: " + "; ".join(failing))
        details = {"mode": "crisp" if args.crisp else "vector", "profile": profile, "zoom": zoom,
                   "canvas": [width, height], "anchor_px": list(anchor) if anchor else None,
                   "palette_source": palette_source, "variants": variant_dirs, "images": images}
        codeart_core.write_codeart_meta(
            stage / META_NAME, generator=TOOL_NAME, spec_sha256=forge_core.sha256_bytes(raw), renderer=renderer,
            palette=palette, outputs=outputs, qa=qa, extra={"svg": details})
    final = final.parent.resolve() / final.name
    if qa["status"] != "pass":
        failing = ", ".join(check["id"] for check in qa["checks"] if check["status"] == "fail")
        _warn(f"QA status {qa['status']} ({failing}); published because --strict-qc was not given")
    return {"output": str(final), "metadata": str(final / META_NAME), "qa": qa["status"],
            "mode": details["mode"], "variants": [label for label, _ in variants], "files": len(outputs),
            "renderer": f"{renderer['name']} {renderer['version']}"}


def render_envelope(images: list[dict], profile: str, crisp: bool, inputs: list, outputs: list) -> dict:
    """QA envelope of a render: lint result and, for crisp art, the pixel gates on every 1x image."""
    checks = [{"id": "lint", "status": "pass", "value": 0, "threshold": 0, "profile": profile}]
    not_proven = list(RENDER_NOT_PROVEN)
    if crisp:
        measured = [image for image in images if "from" not in image]
        for name in ("partial_alpha", "off_palette"):
            worst = max(image[name] for image in measured)
            check = {"id": name, "status": "pass" if worst == 0 else "fail", "value": worst, "threshold": 0}
            failing = [image["file"] for image in measured if image[name] > 0]
            if failing:
                check["failing"] = failing
            checks.append(check)
    else:
        not_proven.append("Pixel gates: vector renders are anti-aliased by design, so partial alpha and colours "
                          "outside the palette are expected and not checked (render with --crisp for pixel art).")
    method = (f"svg_render.py render: every variant's compiled SVG passed codeart_core.lint_portable_svg ({profile} "
              "profile) before rasterizing" + ("; every 1x PNG was read back and measured with codeart_core."
                                               "qa_pixels (alpha census, exact palette lookup), and the @Nx copies are "
                                               "integer nearest upscales of it" if crisp else ""))
    status = "fail" if any(check["status"] == "fail" for check in checks) else "pass"
    return {"status": status, "method": method, "notProven": not_proven, "checks": checks, "inputs": inputs,
            "outputs": outputs, "tool": {"name": f"{TOOL_NAME} render", "version": TOOL_VERSION}}


# ----------------------------------------------------------------------------- lint

def cmd_lint(args: argparse.Namespace) -> tuple[dict, str | None]:
    source, _ = read_svg(Path(args.svg))
    text, problems = source, []
    if args.variant and not args.palette:
        raise codeart_core.CodeArtError("--variant needs --palette")
    if args.compile:
        palette = codeart_core.parse_palette(Path(args.palette)) if args.palette else None
        try:
            text = codeart_core.compile_svg(source, palette, args.variant)
        except codeart_core.CodeArtError as error:
            problems.append(f"compile: {error}")
    elif args.palette or args.variant:
        raise codeart_core.CodeArtError("--palette and --variant apply with --compile")
    if not problems:
        problems = codeart_core.lint_portable_svg(text, args.profile)
    summary = {"status": "fail" if problems else "pass", "svg": str(Path(args.svg)), "profile": args.profile,
               "compiled": bool(args.compile), "problems": problems}
    if not problems:
        return summary, None
    for problem in problems:
        print(forge_core.ascii_text(problem), file=sys.stderr)
    return summary, f"{len(problems)} {args.profile}-profile problem(s) in {Path(args.svg).name}"


# ----------------------------------------------------------------------------- doctor

def doctor_envelope(report: dict) -> dict:
    """The doctor report (codeart_core.run_doctor shape) extended into a common QA envelope."""
    primary = report["primary"]
    checks = [{"id": "primary", "status": "pass" if primary else "fail", "value": primary,
               "threshold": "an available backend"}]
    for name, backend in report["backends"].items():
        if backend["status"] == "unavailable":
            checks.append({"id": f"{name}:available", "status": "skipped", "value": backend["reason"]})
            continue
        for result in backend["results"]:
            status = result["status"] if name == primary or result["status"] == "pass" else "warn"
            value = {key: item for key, item in result.items() if key not in ("case", "zoom", "status")}
            checks.append({"id": f"{name}:{result['case']}@{result['zoom']}x", "status": status, "value": value})
    for result in report["lint"]:
        checks.append({"id": f"lint:{result['case']}", "status": result["status"], "value": result["codes"],
                       "threshold": "rejected by the portable lint"})
    return {**report, "method": DOCTOR_METHOD, "notProven": list(DOCTOR_NOT_PROVEN), "checks": checks,
            "inputs": [], "outputs": [], "tool": {"name": f"{TOOL_NAME} doctor", "version": TOOL_VERSION},
            "platform": {"system": sys.platform, "python": platform.python_version()}}


def cmd_doctor(args: argparse.Namespace) -> tuple[dict, str | None]:
    target = Path(args.report) if args.report else None
    if target is not None:
        if os.path.lexists(target):
            raise FileExistsError(f"refusing to replace existing file {target}")
        if _local_inside(target, SKILL_DIR):
            raise codeart_core.CodeArtError("write the report inside your project, not inside the codeart2d skill "
                                            "folder")
    report = doctor_envelope(codeart_core.run_doctor(args.backend or None))
    if target is not None:
        _local_publish_file(target, report)
    failed = [check["id"] for check in report["checks"] if check["status"] == "fail"]
    summary = {"status": report["status"], "primary": report["primary"],
               "backends": {name: backend["status"] for name, backend in report["backends"].items()},
               "failed": failed, "report": str(target.resolve()) if target else None}
    if report["status"] == "pass":
        return summary, None
    if report.get("hint"):
        return summary, report["hint"]
    return summary, f"the primary backend {report['primary']} deviates on {', '.join(failed) or 'the lint cases'}"


# ----------------------------------------------------------------------------- command line

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render", help="compile, lint and rasterize one SVG into a new output folder")
    render.add_argument("--svg", required=True, help="authored SVG (portable profile)")
    render.add_argument("--output-dir", required=True, help="new folder inside your project; never replaced")
    render.add_argument("--palette", help="palette file (.json with colors/variants, .hex or .gpl)")
    render.add_argument("--variants", default="all", help="all (default), base, or palette variant names a,b")
    render.add_argument("--zoom", type=float, default=1.0, help="vector: render scale; --crisp: integer upscale")
    render.add_argument("--crisp", action="store_true", help="pixel-art route: crispEdges, 1x render, nearest upscale")
    render.add_argument("--anchor", help="root x,y in SVG units (default: the root data-anchor, if any)")
    render.add_argument("--backend", choices=BACKEND_CHOICES, default="auto", help="rasterizer (default auto)")
    render.add_argument("--strict-qc", action="store_true", help="publish nothing unless the pixel gates pass")
    lint = commands.add_parser("lint", help="check an SVG against the portable or pixel profile")
    lint.add_argument("--svg", required=True, help="SVG to check")
    lint.add_argument("--profile", choices=("portable", "pixel"), default="portable", help="default portable")
    lint.add_argument("--compile", action="store_true", help="lint the compiled SVG (what render rasterizes)")
    lint.add_argument("--palette", help="palette for --compile")
    lint.add_argument("--variant", help="palette variant for --compile")
    doctor = commands.add_parser("doctor", help="check the installed rasterizers against the conformance corpus")
    doctor.add_argument("--report", help="write the full report to this new JSON file")
    doctor.add_argument("--backend", action="append", choices=BACKEND_CHOICES[1:],
                        help="check only this backend (repeatable; the first available one is primary)")
    return parser


def _warn(message: str) -> None:
    print(f"warning: {forge_core.ascii_text(message)}", file=sys.stderr)


def _local_dependency_problem() -> str | None:
    """The pip command for missing numpy/Pillow, or why the skill's own modules did not import."""
    if _MISSING is None:
        return None
    import importlib.util

    missing = [name for name in ("numpy", "PIL") if importlib.util.find_spec(name) is None]
    if not missing:
        return f"error: cannot import the codeart2d libraries ({_MISSING}); reinstall the codeart2d skill"
    packages = " ".join("Pillow" if name == "PIL" else name for name in missing)
    interpreter = sys.executable.encode("ascii", "backslashreplace").decode("ascii")
    return (f"error: missing Python module(s): {', '.join(missing)}\n"
            f"install with: python -m pip install {packages}\n"
            f"(run it with the interpreter that runs this tool: {interpreter})")


def main(argv: Sequence[str] | None = None) -> int:
    if _MISSING is None:
        forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    problem = _local_dependency_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    try:
        if args.command == "render":
            summary, failure = cmd_render(args), None
        elif args.command == "lint":
            summary, failure = cmd_lint(args)
        else:
            summary, failure = cmd_doctor(args)
    except KeyboardInterrupt:
        print("error: interrupted; nothing was published", file=sys.stderr)
        return 130
    except (QAFailure, codeart_core.CodeArtError, codeart_core.RasterError, ValueError, OSError) as error:
        print(f"error: {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    except Exception as error:  # never a traceback for the user; a render stage is already removed
        print(f"error: internal error ({type(error).__name__}): {forge_core.ascii_text(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    if failure:
        print(f"error: {forge_core.ascii_text(failure)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
