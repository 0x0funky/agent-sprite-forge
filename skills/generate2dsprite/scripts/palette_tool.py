#!/usr/bin/env python3
"""Game palettes in OKLab: build, apply, lock, LUTs, variants and clip quantizing.

Every subcommand writes a NEW --output-dir: the work is staged beside it,
checked, and published only when the checks pass, so a failed run leaves
nothing behind. Images come out as 8-bit RGBA PNGs with binary alpha (RGB
zeroed under alpha 0), ready for build_animation_clips; indexed PNGs are
written only with --indexed, into indexed/, and are never builder input. On
success one ASCII JSON line names the output folder and its metadata file.

  build         learn a palette (k-means++ in OKLab) from images; --base keeps an
                existing (locked) palette's colours at their indices and appends
  apply         snap images to a palette; with --lock, sources recorded in the lock
                are re-applied with the locked colours, so their outputs never change
  lock          freeze a palette for its sources: palette.json (locked: true) and
                palette-lock.json (sha256 and OKLab fit of every source)
  luts          palette-swap LUT rows (identity, hitflash, frozen, silhouette, skins)
                as luts.json and a 256-wide luts.png, plus an RGB palettize strip
  variants      bake hitflash / frozen / silhouette / skin variants of on-palette frames
  quantize-seq  quantize a clip with temporal hysteresis (cycle, pingpong or oneshot)

Usage errors exit 2 (argparse); every other failure prints one "error: ..." line,
publishes nothing and exits 1. JSON inputs may carry a UTF-8 byte-order mark.

Example: python palette_tool.py build --input frames --colors 16 --output-dir palette
Run "python palette_tool.py <command> --help" for each command's options.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import numpy as np
    import forge_core  # this skill's vendored copies
    import forge_palette as fp
except ImportError as missing:  # numpy and Pillow are needed before forge_core can report it
    sys.stderr.write(f"error: missing Python module {missing.name or missing}; "
                     "install with: python -m pip install numpy Pillow\n")
    raise SystemExit(1)

TOOL_NAME = "palette_tool"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # QA envelopes record the package version (D29)
LOOP_POLICIES = ("cycle", "pingpong", "oneshot")
LUT_VARIANTS = ("hitflash", "frozen", "silhouette")


class ToolError(ValueError):
    """A user-facing failure: bad input, or QA that must not publish (one ``error: ...`` line, exit 1)."""


# --------------------------------------------------------------------------- CLI helpers (pixel_reduce.py uses them)

def natural_key(path: Path) -> list[Any]:
    """Sort frame_2 before frame_10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def expand_inputs(paths: Sequence[Path], what: str = "input") -> list[Path]:
    """Files as given, folders as their *.png files in natural order; duplicates dropped."""
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found = sorted((child for child in path.iterdir()
                            if child.is_file() and child.suffix.lower() == ".png"), key=natural_key)
            if not found:
                raise ToolError(f"no PNG files in {path}")
            files.extend(found)
        elif path.is_file():
            files.append(path)
        else:
            raise ToolError(f"{what} not found: {path}")
    unique, seen = [], set()
    for path in files:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def output_names(paths: Sequence[Path]) -> list[str]:
    """One ``<stem>.png`` per input; two inputs with the same stem are refused."""
    names = [f"{path.stem}.png" for path in paths]
    clashes = sorted({name for name in names if names.count(name) > 1})
    if clashes:
        raise ToolError(f"two inputs would write the same output name: {', '.join(clashes)}")
    return names


def load_images(paths: Sequence[Path]) -> tuple[list[np.ndarray], list[dict[str, Any]]]:
    images, infos = [], []
    for path in paths:
        image, info = forge_core.load_rgba(path)
        images.append(np.asarray(image, dtype=np.uint8).copy())
        infos.append(info)
    return images, infos


def check(check_id: str, value: Any, threshold: Any, ok: bool | None, *, warn: bool = False,
          **extra: Any) -> dict[str, Any]:
    """A qaCheck. ``ok=None`` records an ungated measurement as ``skipped``; a failed
    check is ``warn`` instead of ``fail`` when ``warn`` is set."""
    status = "skipped" if ok is None else "pass" if ok else "warn" if warn else "fail"
    return {"id": check_id, "status": status, "value": value, "threshold": threshold, **extra}


def qa_envelope(*, checks: list[dict[str, Any]], method: str, not_proven: Sequence[str],
                inputs: Sequence[dict[str, Any]], outputs: Sequence[dict[str, Any]], tool: str = TOOL_NAME,
                **extra: Any) -> dict[str, Any]:
    """A common qaEnvelope; the status is the worst check (skipped checks never fail it)."""
    statuses = {item["status"] for item in checks}
    status = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    envelope = {"status": status, "method": method, "notProven": list(not_proven), "checks": checks,
                "inputs": list(inputs), "outputs": list(outputs), "tool": {"name": tool, "version": TOOL_VERSION}}
    envelope.update(extra)
    return envelope


def enforce(envelope: dict[str, Any], strict: bool, what: str) -> None:
    """Raise (so nothing is published) on a failed check, or on a warning under --strict."""
    failed = [item["id"] for item in envelope["checks"] if item["status"] == "fail"]
    warned = [item["id"] for item in envelope["checks"] if item["status"] == "warn"]
    if failed:
        raise ToolError(f"{what} QA failed ({', '.join(failed)}); nothing was published")
    if strict and warned:
        raise ToolError(f"{what} QA warned under --strict ({', '.join(warned)}); nothing was published")


def run_main(parser: argparse.ArgumentParser, commands: dict[str, Callable[[argparse.Namespace], dict]],
             argv: Sequence[str] | None = None) -> int:
    """Parse, run one command and print its one-line JSON summary, under forge_core.run_cli (D26, D27).

    Usage errors keep argparse's exit 2; a user-facing failure (ToolError, PaletteError, OSError,
    ValueError) prints one ``error: ...`` line and exits 1; anything else prints
    ``error: internal error (<Type>: <message>)`` and exits 1. Tracebacks never reach the user.
    """
    def main() -> int:
        args = parser.parse_args(argv)
        try:
            summary = commands[getattr(args, "command", None) or "run"](args)
        except FileExistsError as error:
            if not getattr(error, "filename", None):
                raise
            raise ToolError(f"output already exists, choose a new --output-dir: {error.filename}") from None
        print(json.dumps(summary, ensure_ascii=True))
        return 0

    return forge_core.run_cli(main)


def colour_arg(value: str) -> str:
    try:
        return fp.hex_color(fp.parse_color(value))
    except fp.PaletteError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def unit_float(value: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("expected a number in 0..1")
    return number


def alpha_level(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 255:
        raise argparse.ArgumentTypeError("expected an alpha level 1..255")
    return number


def slot_arg(value: str) -> int | str:
    if value.strip().lower() == "none":
        return "none"
    number = int(value)
    if not 0 <= number <= 255:
        raise argparse.ArgumentTypeError("expected an index 0..255 or 'none'")
    return number


# --------------------------------------------------------------------------- palette helpers

def _swatches(palette: fp.Palette, size: int = 12, columns: int = 16) -> np.ndarray:
    """A preview sheet: one size x size swatch per index, 1 px gaps, the transparent slot as a checker."""
    count = len(palette)
    rows = (count + columns - 1) // columns
    sheet = np.zeros((rows * (size + 1) + 1, min(count, columns) * (size + 1) + 1, 4), np.uint8)
    sheet[..., 3] = 255
    slot = palette.transparent_index
    checker = ((np.indices((size, size)).sum(0) // 3) % 2).astype(np.uint8)
    for index, colour in enumerate(palette.rgb):
        row, column = divmod(index, columns)
        y, x = 1 + row * (size + 1), 1 + column * (size + 1)
        if slot == index:
            sheet[y:y + size, x:x + size, :3] = (150 + 70 * checker)[..., None]
        else:
            sheet[y:y + size, x:x + size, :3] = colour
    return sheet


def _fit_summary(fits: Sequence[dict[str, Any]]) -> dict[str, Any]:
    measured = [fit for fit in fits if fit["measured_px"]]
    return {"max_delta_e": max((fit["max_delta_e"] for fit in measured), default=0.0),
            "worst_p95_delta_e": max((fit["p95_delta_e"] for fit in measured), default=0.0),
            "off_palette_px": int(sum(fit["off_palette_px"] for fit in fits))}


def _delta_e_check(value: float, limit: float | None) -> dict[str, Any]:
    return check("max_delta_e", value, limit, None if limit is None else value <= limit)


def measure_outputs(paths: Sequence[Path], palette: fp.Palette) -> tuple[int, int]:
    """Off-palette and partial-alpha pixels of written PNGs, re-read from disk."""
    off_palette = partial = 0
    for path in paths:
        fit = fp.fit_report(path, palette)
        off_palette += fit["off_palette_px"]
        partial += fit["partial_alpha_px"]
    return off_palette, partial


def _read_palette_arg(path: Path) -> fp.Palette:
    try:
        return fp.read_palette(path)
    except fp.PaletteError as error:
        raise ToolError(str(error)) from None


def _formats(text: str) -> list[str]:
    formats = [item.strip().lower() for item in text.split(",") if item.strip()]
    unknown = [item for item in formats if item not in fp.PALETTE_FORMATS]
    if unknown:
        raise ToolError(f"unknown palette format(s) {', '.join(unknown)}; use {', '.join(fp.PALETTE_FORMATS)}")
    return ["json"] + [item for item in dict.fromkeys(formats) if item != "json"]


def _lock_for(palette: fp.Palette, lock_path: Path | None) -> tuple[fp.Palette | None, set[str]]:
    if lock_path is None:
        return None, set()
    return fp.lock_view(palette, lock_path), fp.locked_sources(lock_path)


def _skin_rows(paths: Sequence[Path], palette: fp.Palette) -> dict[str, Any]:
    """``skin-<stem>`` -> mapping, from JSON maps ({index or '#from': '#to'}) or palette files."""
    rows: dict[str, Any] = {}
    for path in paths:
        name = "skin-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip("-")
        if name in rows:
            raise ToolError(f"two skin maps are both named {name}")
        if path.suffix.lower() == ".json":
            try:
                document = forge_core.read_json(path)  # UTF-8 with or without a BOM (D28)
            except ValueError as error:
                raise ToolError(f"skin map {path.name} is not valid JSON: {error}") from None
            is_palette = isinstance(document, dict) and (
                document.get("schema") == fp.PALETTE_SCHEMA or "colors" in document)
            rows[name] = fp.as_palette(document) if is_palette or isinstance(document, list) else document
        else:
            rows[name] = fp.read_palette(path)
        fp.variant_colors(palette, "skin", mapping=rows[name])  # validate now, before any output
    return rows


# --------------------------------------------------------------------------- build

def cmd_build(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.colors <= 256:
        raise ToolError("--colors must be 1..256")
    formats = _formats(args.formats)
    inputs = expand_inputs(args.input)
    images, infos = load_images(inputs)
    reserved: Any = [fp.parse_color(colour) for colour in args.reserve] or None
    if args.base is not None:
        base = _read_palette_arg(args.base)
        extra = [colour for colour in (reserved or []) if colour not in base.colors]
        reserved = base.replace(colors=base.colors + tuple(extra), names=base.names + (None,) * len(extra),
                                reserved=(True,) * (len(base) + len(extra)))
    weights = None
    if args.balance:
        weights = [1.0 / max(1, int((image[..., 3] >= args.alpha_threshold).sum())) for image in images]
    palette = fp.build_palette(images, args.colors, reserved=reserved, weights=weights, seed=args.seed,
                               cover=args.cover, alpha_threshold=args.alpha_threshold)
    note = (f"palette_tool build: OKLab k-means++ of {len(inputs)} image(s), {args.colors} colours, seed {args.seed}"
            + (f", extending {args.base.name}" if args.base is not None else ""))
    palette = palette.replace(source=note, name=args.name or palette.name)
    if args.transparent_index is not None:
        slot = None if args.transparent_index == "none" else args.transparent_index
        if args.base is not None and slot != palette.transparent_index:
            raise ToolError(f"--transparent-index cannot move the base palette's transparent index "
                            f"({palette.transparent_index}): indexed files made with it would break")
        if slot is not None and slot < len(palette):
            raise ToolError(f"--transparent-index {slot} would hide colour {slot}; "
                            f"use a free index >= {len(palette)}, 255 or none")
        palette = palette.replace(transparent_index=slot)
    fits = [fp.fit_report(image, palette, alpha_threshold=args.alpha_threshold) for image in images]
    empty = [path.name for path, fit in zip(inputs, fits) if not fit["measured_px"]]
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        written = []
        for fmt in formats:
            target = stage / f"palette.{fmt}"
            fp.write_palette(palette, target, fmt)
            written.append(target)
        forge_core.save_png(_swatches(palette), stage / "swatches.png")
        written.append(stage / "swatches.png")
        totals = _fit_summary(fits)
        checks = [check("palette_colors", len(palette), args.colors, len(palette) <= args.colors),
                  check("inputs_without_samples", len(empty), 0, not empty, warn=True, files=empty),
                  _delta_e_check(totals["max_delta_e"], args.max_delta_e)]
        envelope = qa_envelope(
            checks=checks,
            method=("forge_palette.build_palette (weighted k-means++ in OKLab over unique colours, reserved "
                    "colours fixed, coverage skipping) then forge_palette.fit_report of every input: OKLab dE of "
                    "each pixel with alpha >= the threshold to its nearest palette colour"),
            not_proven=["Visual quality of the palette: look at swatches.png and an applied frame.",
                        "Colours of pixels below the alpha threshold were not sampled or measured."]
                       + ([] if args.max_delta_e is not None else ["No --max-delta-e gate was set."]),
            inputs=[forge_core.file_ref(path, stage) for path in inputs],
            outputs=[forge_core.file_ref(path, stage) for path in written],
            palette={"colors": len(palette), "reserved": int(sum(palette.reserved)),
                     "transparent_index": palette.transparent_index, "seed": args.seed},
            fit={forge_core.manifest_path(path, stage): fit for path, fit in zip(inputs, fits)},
            totals=totals)
        forge_core.write_json(stage / "palette-qa.json", envelope)
        enforce(envelope, args.strict, "build")
    return {"status": envelope["status"], "output": str(final), "metadata": str(final / "palette.json"),
            "qa": str(final / "palette-qa.json"), "colors": len(palette),
            "files": [path.name for path in written]}


# --------------------------------------------------------------------------- apply

def cmd_apply(args: argparse.Namespace) -> dict[str, Any]:
    if args.indexed and args.keep_alpha:
        raise ToolError("--indexed needs binary alpha; drop --keep-alpha")
    palette = _read_palette_arg(args.palette)
    view, locked = _lock_for(palette, args.lock)
    inputs = expand_inputs(args.input)
    names = output_names(inputs)
    images, infos = load_images(inputs)
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        outputs, fits, locked_inputs = [], [], []
        for image, info, name in zip(images, infos, names):
            used = palette
            if view is not None and info["sha256"] in locked:
                used = view
                locked_inputs.append(name)
            fits.append(fp.fit_report(image, used, alpha_threshold=args.alpha_threshold))
            if args.keep_alpha:
                result = image.copy()
                result[..., :3] = used.rgb[fp.nearest_index(image, used)]
                result[result[..., 3] == 0] = 0
            else:
                index = fp.quantize_image(image, used, alpha_threshold=args.alpha_threshold, orphans=args.orphans)
                result = fp.render_indices(index, used)
                if args.indexed:
                    (stage / "indexed").mkdir(exist_ok=True)
                    fp.save_indexed_png(index, palette, stage / "indexed" / name, palette.transparent_index)
            forge_core.save_png(result, stage / name)
            outputs.append(stage / name)
        off_palette, partial = measure_outputs(outputs, palette)
        totals = _fit_summary(fits)
        checks = [check("output_off_palette_px", off_palette, 0, off_palette == 0),
                  check("output_partial_alpha_px", partial, 0, None if args.keep_alpha else partial == 0),
                  _delta_e_check(totals["max_delta_e"], args.max_delta_e)]
        indexed = sorted((stage / "indexed").glob("*.png")) if args.indexed else []
        envelope = qa_envelope(
            checks=checks,
            method=("forge_palette.quantize_image: OKLab-nearest palette colour per pixel (exact palette colours "
                    "kept), alpha binary at the threshold, then the written PNGs re-read and measured with "
                    "forge_palette.fit_report against the palette"),
            not_proven=["Visual quality after snapping: compare an output with its input.",
                        "Indexed PNGs are engine exports; they are not checked by builders."]
                       + ([] if args.max_delta_e is not None else ["No --max-delta-e gate was set."]),
            inputs=[forge_core.file_ref(path, stage) for path in inputs + [args.palette]]
                   + ([forge_core.file_ref(args.lock, stage)] if args.lock else []),
            outputs=[forge_core.file_ref(path, stage) for path in outputs + indexed],
            fit={name: fit for name, fit in zip(names, fits)}, totals=totals, locked_inputs=locked_inputs)
        forge_core.write_json(stage / "apply-qa.json", envelope)
        enforce(envelope, args.strict, "apply")
    return {"status": envelope["status"], "output": str(final), "metadata": str(final / "apply-qa.json"),
            "frames": len(outputs), "locked_inputs": len(locked_inputs), "indexed": bool(args.indexed)}


# --------------------------------------------------------------------------- lock

def cmd_lock(args: argparse.Namespace) -> dict[str, Any]:
    palette = _read_palette_arg(args.palette)
    sources = expand_inputs(args.source, "source")
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        locked = palette.replace(locked=True, source=f"{palette.source}; locked by palette_tool lock")
        fp.write_palette(locked, stage / "palette.json", "json")
        record = fp.lock_palette(stage / "palette.json", sources, base=stage, alpha_threshold=args.alpha_threshold)
        off_palette = [entry["path"] for entry in record["sources"] if entry["fit"]["off_palette_px"]]
        if args.strict and off_palette:
            raise ToolError(f"--strict: {len(off_palette)} source(s) are not exactly on the palette "
                            f"({', '.join(off_palette[:3])}); apply the palette first; nothing was published")
        forge_core.write_json(stage / "palette-lock.json", record)
    return {"status": "warn" if off_palette else "pass", "output": str(final),
            "metadata": str(final / "palette-lock.json"), "palette": str(final / "palette.json"),
            "sources": len(record["sources"]), "sources_off_palette": len(off_palette)}


# --------------------------------------------------------------------------- luts

def cmd_luts(args: argparse.Namespace) -> dict[str, Any]:
    palette = _read_palette_arg(args.palette)
    skins = _skin_rows(args.skin_map, palette)
    document = fp.luts(palette, variants=args.variant, skins=skins, flash_color=args.flash_color,
                       silhouette_color=args.silhouette_color, snap=args.snap)
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        forge_core.save_png(fp.lut_image(document), stage / "luts.png")
        record = {"schema": document["schema"], "palette": forge_core.file_ref(args.palette, stage),
                  "image": forge_core.file_ref(stage / "luts.png", stage)}
        record.update({key: value for key, value in document.items() if key != "schema"})
        if args.palettize_size:
            name = f"palettize-{args.palettize_size}.png"
            forge_core.save_png(fp.palettize_lut(palette, size=args.palettize_size), stage / name)
            record["palettize"] = {
                "image": forge_core.file_ref(stage / name, stage), "size": args.palettize_size,
                "layout": "texel (x = r + size * b, y = g) holds the OKLab-nearest palette colour of RGB level "
                          "(r, g, b); level v is 8-bit round(v * 255 / (size - 1)); sample with nearest filtering"}
        record["usage"] = ("luts.png row r is LUT row rows[r]; column i is the colour index i shows in that row; "
                           "the transparent index and unused columns are transparent")
        forge_core.write_json(stage / "luts.json", record)
    return {"status": "pass", "output": str(final), "metadata": str(final / "luts.json"),
            "rows": document["rows"], "palettize": bool(args.palettize_size)}


# --------------------------------------------------------------------------- variants

def cmd_variants(args: argparse.Namespace) -> dict[str, Any]:
    palette = _read_palette_arg(args.palette)
    skins = _skin_rows(args.skin_map, palette)
    if not args.variant and not skins:
        raise ToolError("choose at least one --variant or --skin-map")
    document = fp.luts(palette, variants=args.variant, skins=skins, flash_color=args.flash_color,
                       silhouette_color=args.silhouette_color, snap=args.snap)
    rows = [name for name in document["rows"] if name != "identity"]
    tables = {name: fp.lut_image({"rows": [name], "luts": document["luts"]})[0] for name in rows}
    inputs = expand_inputs(args.input)
    names = output_names(inputs)
    images, _ = load_images(inputs)
    off_inputs = {}
    for image, name in zip(images, names):
        count = fp.fit_report(image, palette)["off_palette_px"]
        if count:
            off_inputs[name] = count
    if off_inputs and not args.quantize:
        worst = ", ".join(f"{name} ({count} px)" for name, count in list(off_inputs.items())[:3])
        raise ToolError(f"inputs are not on the palette: {worst}; run palette_tool.py apply first or pass --quantize")
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        outputs, alpha_ok = [], True
        for image, name in zip(images, names):
            index = fp.nearest_index(image, palette)
            for row in rows:
                baked = tables[row][index].copy()
                baked[..., 3] = image[..., 3]
                baked[baked[..., 3] == 0] = 0
                (stage / row).mkdir(exist_ok=True)
                forge_core.save_png(baked, stage / row / name)
                outputs.append(stage / row / name)
                alpha_ok &= bool(np.array_equal(np.asarray(forge_core.load_rgba(stage / row / name)[0])[..., 3],
                                                image[..., 3]))
        snapped_off = measure_outputs(outputs, palette)[0] if args.snap else None
        checks = [check("input_off_palette_px", int(sum(off_inputs.values())), 0,
                        None if args.quantize else not off_inputs),
                  check("alpha_preserved", alpha_ok, True, alpha_ok),
                  check("output_off_palette_px", snapped_off, 0, None if snapped_off is None else snapped_off == 0)]
        envelope = qa_envelope(
            checks=checks,
            method=("each pixel's palette index (forge_palette.nearest_index) is looked up in the variant's LUT "
                    "row (forge_palette.luts); alpha is copied unchanged and re-read from the written PNGs"),
            not_proven=["Whether the variant reads well in game (flash timing, frozen tint): look at the frames."]
                       + ([] if args.snap else ["Without --snap the variant colours may lie off the palette."]),
            inputs=[forge_core.file_ref(path, stage) for path in inputs + [args.palette]],
            outputs=[forge_core.file_ref(path, stage) for path in outputs],
            luts={name: document["luts"][name] for name in rows})
        forge_core.write_json(stage / "variants-qa.json", envelope)
        enforce(envelope, args.strict, "variants")
    return {"status": envelope["status"], "output": str(final), "metadata": str(final / "variants-qa.json"),
            "variants": rows, "frames": len(inputs)}


# --------------------------------------------------------------------------- quantize-seq

def cmd_quantize_seq(args: argparse.Namespace) -> dict[str, Any]:
    low, high = args.alpha_band
    if low > high:
        raise ToolError("--alpha-band needs LOW <= HIGH")
    inputs = expand_inputs(args.input)
    names = output_names(inputs)
    images, infos = load_images(inputs)
    if any(image.shape != images[0].shape for image in images):
        raise ToolError("all frames of a sequence must have the same size")
    threshold = args.alpha_threshold / 255.0
    built = args.palette is None
    if built:
        if not 1 <= args.colors <= 256:
            raise ToolError("--colors must be 1..256")
        palette = fp.build_palette(images, args.colors, seed=args.seed, alpha_threshold=args.alpha_threshold)
        palette = palette.replace(source=f"palette_tool quantize-seq: OKLab k-means++ of {len(images)} frame(s), "
                                         f"{args.colors} colours, seed {args.seed}")
    else:
        palette = _read_palette_arg(args.palette)
    view, locked = _lock_for(palette, args.lock)
    used = palette
    if view is not None:
        hits = [info["sha256"] in locked for info in infos]
        if all(hits):
            used = view
        elif any(hits):
            raise ToolError("the clip mixes locked and unlocked frames; lock the whole clip or none of it")
    policy = args.loop_policy
    common = {"margin": args.margin, "alpha_band": (low, high), "alpha_threshold": threshold,
              "orphans": args.orphans, "despeckle": not args.no_despeckle}
    frames, stats = fp.quantize_sequence(images, used, loop=policy == "cycle", pingpong=policy == "pingpong",
                                         return_stats=True, **common)
    frames = frames[:len(images)]  # pingpong: the way back reuses these forward frames
    slot = used.transparent_index
    plain = [fp.quantize_sequence([image], used, loop=False, **common)[0] for image in images]
    flips = None
    if len(images) > 1:
        hysteresis = fp.flip_stats(frames, images, transparent_index=slot, loop=policy == "cycle")
        baseline = fp.flip_stats(plain, images, transparent_index=slot, loop=policy == "cycle")
        reduction = None if not baseline["noise_flip"] else round(1.0 - hysteresis["noise_flip"]
                                                                  / baseline["noise_flip"], 4)
        flips = {"hysteresis": hysteresis, "per_frame_nearest": baseline, "noise_flip_reduction": reduction}
    fits = [fp.fit_report(image, used, alpha_threshold=args.alpha_threshold) for image in images]
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        outputs = []
        for index, name in zip(frames, names):
            forge_core.save_png(fp.render_indices(index, used), stage / name)
            outputs.append(stage / name)
            if args.indexed:
                (stage / "indexed").mkdir(exist_ok=True)
                fp.save_indexed_png(index, palette, stage / "indexed" / name, palette.transparent_index)
        palette_file = args.palette
        if built:
            palette_file = stage / "palette.json"
            fp.write_palette(palette, palette_file, "json")
        off_palette, partial = measure_outputs(outputs, palette)
        totals = _fit_summary(fits)
        checks = [check("output_off_palette_px", off_palette, 0, off_palette == 0),
                  check("output_partial_alpha_px", partial, 0, partial == 0),
                  _delta_e_check(totals["max_delta_e"], args.max_delta_e)]
        indexed = sorted((stage / "indexed").glob("*.png")) if args.indexed else []
        envelope = qa_envelope(
            checks=checks,
            method=("forge_palette.quantize_sequence: OKLab-nearest colours with temporal hysteresis (a pixel keeps "
                    "last frame's index within the margin and last frame's opacity inside the alpha band), "
                    "cleanup_orphans and cleanup_alpha per frame; flips measured with forge_palette.flip_stats "
                    "against per-frame nearest quantizing with the same cleanup"),
            not_proven=["Motion quality: hysteresis can delay a real colour change smaller than the margin.",
                        "The flip reduction is measured on this clip only."]
                       + (["Forward frames only: play them with loop_policy pingpong; the way back reuses them."]
                          if policy == "pingpong" else [])
                       + ([] if args.max_delta_e is not None else ["No --max-delta-e gate was set."]),
            inputs=[forge_core.file_ref(path, stage) for path in inputs]
                   + ([] if built else [forge_core.file_ref(args.palette, stage)])
                   + ([forge_core.file_ref(args.lock, stage)] if args.lock else []),
            outputs=[forge_core.file_ref(path, stage) for path in outputs + indexed
                     + ([palette_file] if built else [])],
            loop_policy=policy, locked=used is view and view is not None,
            params={"margin": args.margin, "alpha_band": [low, high], "alpha_threshold": args.alpha_threshold,
                    "orphans": args.orphans, "despeckle": not args.no_despeckle},
            flips=flips, frames=[{"file": name, **frame_stats} for name, frame_stats in zip(names, stats)],
            totals=totals)
        forge_core.write_json(stage / "quantize-qa.json", envelope)
        enforce(envelope, args.strict, "quantize-seq")
    return {"status": envelope["status"], "output": str(final), "metadata": str(final / "quantize-qa.json"),
            "frames": len(outputs), "loop_policy": policy,
            "palette": str(final / "palette.json") if built else str(args.palette),
            "noise_flip_reduction": None if flips is None else flips["noise_flip_reduction"]}


# --------------------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="palette_tool.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, summary: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=summary, description=summary)
        sub.add_argument("--output-dir", type=Path, required=True, help="new folder for the results (must not exist)")
        return sub

    build = command("build", "Learn a palette from images with deterministic k-means++ in OKLab.")
    build.add_argument("--input", type=Path, nargs="+", action="extend", required=True,
                       help="images or folders of PNGs to sample")
    build.add_argument("--colors", type=int, default=16, help="palette size including reserved colours (default 16)")
    build.add_argument("--reserve", nargs="+", action="extend", default=[], type=colour_arg, metavar="#RRGGBB",
                       help="colours kept as-is at the first indices (outline, UI or engine colours)")
    build.add_argument("--base", type=Path, help="palette whose colours stay first at their indices; extends a "
                                                 "locked palette without changing it")
    build.add_argument("--seed", type=int, default=0, help="k-means++ seed (default 0)")
    build.add_argument("--cover", type=float, default=fp.COVER_DELTA_E,
                       help="skip samples within this OKLab dE of a reserved colour (default 0.03)")
    build.add_argument("--balance", action="store_true", help="give every input the same total weight")
    build.add_argument("--alpha-threshold", type=alpha_level, default=128,
                       help="sample pixels with alpha >= this (default 128)")
    build.add_argument("--name", help="palette name (palette.json and .gpl)")
    build.add_argument("--transparent-index", type=slot_arg,
                       help="index of transparent pixels in indexed exports: 255 (default; the palette can grow), "
                            "a free index after the colours (smaller files, but the palette cannot grow past "
                            "it), or none")
    build.add_argument("--formats", default="json,gpl,hex",
                       help="palette files to write: json (always), gpl, hex, pal (default json,gpl,hex)")
    build.add_argument("--max-delta-e", type=float, help="fail when any sampled pixel is farther than this OKLab dE")
    build.add_argument("--strict", action="store_true", help="treat QA warnings as failures")

    apply = command("apply", "Snap images to a palette (binary alpha); optional indexed PNGs.")
    apply.add_argument("--palette", type=Path, required=True, help="palette file (.json, .gpl, .hex, .pal)")
    apply.add_argument("--input", type=Path, nargs="+", action="extend", required=True,
                       help="images or folders of PNGs")
    apply.add_argument("--lock", type=Path, help="palette-lock.json: its sources use the locked colours only")
    apply.add_argument("--indexed", action="store_true", help="also write indexed PNGs into indexed/ (engine export)")
    apply.add_argument("--keep-alpha", action="store_true", help="snap colours but keep partial alpha")
    apply.add_argument("--alpha-threshold", type=alpha_level, default=128,
                       help="pixels with alpha >= this become opaque, the rest transparent (default 128)")
    apply.add_argument("--orphans", type=int, default=0, help="isolated-pixel cleanup passes (default 0)")
    apply.add_argument("--max-delta-e", type=float, help="fail when any input pixel is farther than this OKLab dE")
    apply.add_argument("--strict", action="store_true", help="treat QA warnings as failures")

    lock = command("lock", "Lock a palette for a set of sources (palette.json + palette-lock.json).")
    lock.add_argument("--palette", type=Path, required=True, help="palette file to lock")
    lock.add_argument("--source", type=Path, nargs="+", action="extend", required=True,
                      help="images or folders made with this palette")
    lock.add_argument("--alpha-threshold", type=alpha_level, default=1,
                      help="measure pixels with alpha >= this (default 1: every visible pixel)")
    lock.add_argument("--strict", action="store_true", help="fail unless every source is exactly on the palette")

    luts = command("luts", "Palette-swap LUT rows and an RGB palettize strip.")
    luts.add_argument("--palette", type=Path, required=True, help="palette file")
    luts.add_argument("--variant", nargs="*", choices=LUT_VARIANTS, default=list(LUT_VARIANTS),
                      help="LUT rows after identity (default: hitflash frozen silhouette)")
    luts.add_argument("--skin-map", type=Path, nargs="+", action="extend", default=[],
                      help="skin maps: JSON {index or '#from': '#to'} or a palette with one colour per index")
    luts.add_argument("--flash-color", type=colour_arg, help="hitflash colour (default #ffffff)")
    luts.add_argument("--silhouette-color", type=colour_arg, help="silhouette colour (default #000000)")
    luts.add_argument("--snap", action="store_true", help="snap variant colours to the palette (index remaps)")
    luts.add_argument("--palettize-size", type=int, default=32, choices=(0, 16, 32, 64),
                      help="RGB levels of the palettize strip; 0 skips it (default 32)")

    variants = command("variants", "Bake hitflash, frozen, silhouette or skin variants of on-palette frames.")
    variants.add_argument("--palette", type=Path, required=True, help="palette file")
    variants.add_argument("--input", type=Path, nargs="+", action="extend", required=True,
                          help="on-palette images or folders (palette_tool apply output)")
    variants.add_argument("--variant", nargs="*", choices=LUT_VARIANTS, default=list(LUT_VARIANTS),
                          help="variants to bake (default: hitflash frozen silhouette)")
    variants.add_argument("--skin-map", type=Path, nargs="+", action="extend", default=[],
                          help="skin maps: JSON {index or '#from': '#to'} or a palette with one colour per index")
    variants.add_argument("--flash-color", type=colour_arg, help="hitflash colour (default #ffffff)")
    variants.add_argument("--silhouette-color", type=colour_arg, help="silhouette colour (default #000000)")
    variants.add_argument("--snap", action="store_true", help="snap variant colours to the palette")
    variants.add_argument("--quantize", action="store_true", help="accept off-palette inputs (snapped first)")
    variants.add_argument("--strict", action="store_true", help="treat QA warnings as failures")

    sequence = command("quantize-seq", "Quantize a clip with temporal hysteresis against palette flicker.")
    sequence.add_argument("--input", type=Path, nargs="+", action="extend", required=True,
                          help="frames in play order, or a folder of PNGs (natural order)")
    source = sequence.add_mutually_exclusive_group(required=True)
    source.add_argument("--palette", type=Path, help="palette file")
    source.add_argument("--colors", type=int, help="build a palette of this many colours from the frames")
    sequence.add_argument("--seed", type=int, default=0, help="k-means++ seed with --colors (default 0)")
    sequence.add_argument("--loop-policy", choices=LOOP_POLICIES, default="cycle",
                          help="cycle: frame 0 follows the last; pingpong: frame 0 follows frame 1; oneshot")
    sequence.add_argument("--margin", type=float, default=fp.HYSTERESIS_MARGIN,
                          help="keep last frame's colour within this squared OKLab distance (default 4e-4 = dE 0.02)")
    sequence.add_argument("--alpha-band", type=unit_float, nargs=2, default=list(fp.ALPHA_BAND),
                          metavar=("LOW", "HIGH"), help="alpha (0..1) that keeps last frame's opacity "
                                                        "(default 0.4 0.6)")
    sequence.add_argument("--alpha-threshold", type=alpha_level, default=128,
                          help="alpha >= this is opaque (default 128)")
    sequence.add_argument("--orphans", type=int, default=1, help="isolated-pixel cleanup passes (default 1)")
    sequence.add_argument("--no-despeckle", action="store_true", help="keep lone opaque specks and pinholes")
    sequence.add_argument("--lock", type=Path, help="palette-lock.json: a locked clip uses the locked colours only")
    sequence.add_argument("--indexed", action="store_true", help="also write indexed PNGs into indexed/")
    sequence.add_argument("--max-delta-e", type=float, help="fail when any frame pixel is farther than this OKLab dE")
    sequence.add_argument("--strict", action="store_true", help="treat QA warnings as failures")
    return parser


COMMANDS = {"build": cmd_build, "apply": cmd_apply, "lock": cmd_lock, "luts": cmd_luts,
            "variants": cmd_variants, "quantize-seq": cmd_quantize_seq}


def main(argv: Sequence[str] | None = None) -> int:
    return run_main(build_parser(), COMMANDS, argv)


if __name__ == "__main__":
    raise SystemExit(main())
