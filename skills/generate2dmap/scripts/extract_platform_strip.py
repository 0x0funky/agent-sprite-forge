#!/usr/bin/env python3
"""Extract native rectangular platform pieces (left cap, middle variants, right cap) and measure them.

Pieces are exact source crops: no trim, resize, bbox fit or anchor guess. QC checks the declared
collision band, measures the art's top surface in every collision column against the declared
surface, and measures every join with a normalised seam ratio. The output directory must be new;
work is staged beside it and published only after QC, so a failed run leaves nothing behind.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_matte  # noqa: E402


SCHEMA = "generate2dmap.platform_strip.v2"
TOOL_NAME = "extract_platform_strip.py"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # D29: QA envelopes carry the package version
MANIFEST_NAME = "platform-strip.json"
PREVIEW_NAME = "strip-preview.png"
ROLES = ("left_cap", "middle", "right_cap")
BACKGROUND_MODES = ("chroma_key", "native_alpha", "opaque")
MAX_MIDDLES = 16
PREVIEW_MIDDLES = 3  # the preview repeats the middle variants up to at least this many
RESERVED_STEMS = {"strip-preview", "platform-strip"}
DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

# The normalised seam ratio (MAP-14) is forge_core.edge_seam_report, the one seam metric (D9): steps are
# premultiplied RGBA mean absolute differences (0-255) between neighbouring columns, and a join is
# compared with the interior steps near it.
NOMINAL_SEAM_RATIO = forge_core.EDGE_SEAM_NOMINAL_RATIO  # verdict label when no --max-seam-ratio gate is set


# --------------------------------------------------------------------------- spec

def integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer.")
    return value


def validate_spec(data: Any, source_size: tuple[int, int]) -> list[dict[str, Any]]:
    """Return the pieces in strip order: the left cap, the middle variants in spec order, the right cap."""
    if not isinstance(data, dict):
        raise ValueError("Spec must be a JSON object.")
    top = integer(data.get("surface_y_px"), "surface_y_px")
    depth = integer(data.get("collision_depth_px"), "collision_depth_px")
    raw_pieces = data.get("pieces")
    if not isinstance(raw_pieces, list) or not 3 <= len(raw_pieces) <= MAX_MIDDLES + 2:
        raise ValueError(f"Spec requires pieces: one left_cap, 1-{MAX_MIDDLES} middle variants and one right_cap.")
    caps: dict[str, dict[str, Any]] = {}
    middles: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in raw_pieces:
        if not isinstance(raw, dict):
            raise ValueError("Each piece must be an object.")
        identity, role, box = raw.get("id"), raw.get("role"), raw.get("source_box")
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identity)
                or identity.lower() in ids | RESERVED_STEMS or identity.upper() in DEVICE_NAMES):
            raise ValueError("Piece id must be a unique, portable filename stem, excluding reserved names.")
        if not isinstance(role, str) or role not in ROLES or role in caps:
            raise ValueError("Use exactly one left_cap, one or more middle variants and exactly one right_cap.")
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError("source_box must be [left, top, right, bottom].")
        left, upper, right, bottom = [integer(v, "source_box coordinate") for v in box]
        if not (0 <= left < right <= source_size[0] and 0 <= upper < bottom <= source_size[1]):
            raise ValueError(f"source_box for {identity} lies outside the source or is empty.")
        span = raw.get("collision_span_px", [0, right - left])
        if not isinstance(span, list) or len(span) != 2:
            raise ValueError("collision_span_px must be [x0,x1] in cropped-piece coordinates.")
        x0, x1 = [integer(v, "collision_span_px coordinate") for v in span]
        if not 0 <= x0 < x1 <= right - left:
            raise ValueError("collision_span_px must be nonempty and inside its piece.")
        if (role == "middle" and span != [0, right - left]
                or role == "left_cap" and x1 != right - left
                or role == "right_cap" and x0 != 0):
            raise ValueError("Collision spans must reach every join: middle both sides, left_cap right, right_cap left.")
        ids.add(identity.lower())
        piece = {"id": identity, "role": role, "source_box": box,
                 "size": [right - left, bottom - upper], "collision_span_px": span}
        if role == "middle":
            middles.append(piece)
        else:
            caps[role] = piece
    if set(caps) != {"left_cap", "right_cap"} or not middles:
        raise ValueError("Use exactly one left_cap, one or more middle variants and exactly one right_cap.")
    ordered = [caps["left_cap"], *middles, caps["right_cap"]]
    if len({piece["size"][1] for piece in ordered}) != 1:
        raise ValueError("All native rectangular pieces must have the same height; no resizing is performed.")
    if top < 0 or depth <= 0 or top + depth > ordered[0]["size"][1]:
        raise ValueError("Declared surface_y_px and collision_depth_px must define a nonempty in-canvas band.")
    return ordered


# --------------------------------------------------------------------------- background

def prepare_background(source: Any, args: argparse.Namespace) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply the background mode to the whole RGBA source (Image or array); geometry is never changed."""
    pixels = np.array(source, dtype=np.uint8)
    alpha = pixels[..., 3]
    report: dict[str, Any] = {"background_mode": args.background_mode}
    if args.background_mode == "native_alpha":
        if alpha.min() == 255 or alpha.max() == 0:
            raise ValueError("native_alpha requires visible art and real transparent pixels; "
                             "use --background-mode opaque or chroma_key for this source.")
        return pixels, report
    if args.background_mode == "opaque":
        if alpha.min() != 255:
            raise ValueError("opaque mode cannot discard source transparency; use --background-mode native_alpha.")
        return pixels, report
    keyed = np.array(forge_matte.legacy_hard_key(pixels, args.threshold, args.edge_threshold))
    report.update({"keyer": "forge_matte.legacy_hard_key", "threshold": args.threshold,
                   "edge_threshold": args.edge_threshold})
    if args.despill_radius:
        keyed, despill = forge_matte.despill(keyed, "edge", args.despill_radius)
        report["despill_changed_px"] = despill["changed_px"]
    return keyed, report


# --------------------------------------------------------------------------- measurements

def edge_metrics(left: np.ndarray, right: np.ndarray) -> dict[str, Any]:
    """Compare corresponding edge pixels; ignore hidden RGB when alpha is zero."""
    a = left.astype(np.float64)
    b = right.astype(np.float64)
    jointly_visible = (a[:, 3] > 0) & (b[:, 3] > 0)
    rgb = float(np.abs(a[jointly_visible, :3] - b[jointly_visible, :3]).mean()) if jointly_visible.any() else None
    premultiplied_a = a[:, :3] * a[:, 3:4] / 255.0
    premultiplied_b = b[:, :3] * b[:, 3:4] / 255.0
    return {
        "alpha_mae": round(float(np.abs(a[:, 3] - b[:, 3]).mean()), 6),
        "visible_rgb_mae": round(rgb, 6) if rgb is not None else None,
        "jointly_visible_rows": int(jointly_visible.sum()),
        "premultiplied_rgb_mae": round(float(np.abs(premultiplied_a - premultiplied_b).mean()), 6),
    }


def measure_surface(alpha: np.ndarray, top: int, span: list[int], *, solid: int, tolerance: int,
                    decoration: int, measurable: bool = True) -> dict[str, Any]:
    """Measure the art's top in each collision column against the declared surface (MAP-07).

    The measured top is the first row whose alpha reaches ``solid``; rows of the decoration band
    (``decoration`` rows directly above the surface) are ignored, so grass tips may stand there.
    Art that rises more than ``tolerance`` px above the surface outside that band is a defect:
    actors standing on the declared surface would sink into it. Opaque pieces have no alpha
    silhouette, so ``measurable=False`` records the surface as not measured.
    """
    if not measurable:
        return {"declared_y_px": top, "measured": False,
                "reason": "opaque background mode: every pixel is solid, so the art's top cannot be measured",
                "measured_y_px_by_column": None, "max_rise_px": 0, "columns_above_tolerance": [],
                "decoration_band_px": decoration, "decoration_px": 0}
    x0, x1 = span
    solid_mask = alpha[:, x0:x1] >= solid
    rows = np.arange(alpha.shape[0])[:, None]
    band = (rows >= top - decoration) & (rows < top)
    structural = solid_mask & ~band
    found = structural.any(axis=0)
    first = np.where(found, structural.argmax(axis=0), -1)
    rising = found & (first < top - tolerance)
    measured = [int(value) if ok else None for value, ok in zip(first.tolist(), found.tolist())]
    rise = int((top - first[found]).max()) if found.any() else 0
    return {
        "declared_y_px": top,
        "measured": True,
        "measured_y_px_by_column": measured,
        "max_rise_px": max(0, rise),
        "columns_above_tolerance": (np.flatnonzero(rising) + x0).tolist(),
        "decoration_band_px": decoration,
        "decoration_px": int((solid_mask & band).sum()),
    }


# --------------------------------------------------------------------------- QA

def _check(identifier: str, status: str, value: Any, threshold: Any) -> dict[str, Any]:
    return {"id": identifier, "status": status, "value": value, "threshold": threshold}


def build_checks(pieces: list[dict[str, Any]], joins: list[dict[str, Any]], args: argparse.Namespace) -> list[dict]:
    coverage = {piece["id"]: piece["coverage"]["minimum_column_fraction"] for piece in pieces}
    missing = sum(len(piece["coverage"]["missing_surface_columns"]) for piece in pieces)
    rise = max(piece["surface"]["max_rise_px"] for piece in pieces)
    rising = sum(len(piece["surface"]["columns_above_tolerance"]) for piece in pieces)
    ratio = max(join["seam"]["seam_ratio"] for join in joins)
    seams = sum(join["seam"]["verdict"] == "seam" for join in joins)
    duplicates = sum(join["seam"]["verdict"] == "duplicate_edge" for join in joins)
    gated = args.max_seam_ratio is not None
    measured = all(piece["surface"]["measured"] for piece in pieces)
    checks = [
        _check("collision_band_coverage", "pass" if min(coverage.values()) >= args.min_column_coverage else "fail",
               coverage, args.min_column_coverage),
        _check("surface_row_solid", "pass" if missing == 0 else "fail", missing, 0),
        _check("surface_rise_px", ("pass" if rising == 0 else "fail") if measured else "skipped",
               rise if measured else None, args.surface_tolerance_px),
        _check("seam_ratio", ("fail" if gated else "warn") if seams else "pass", ratio,
               args.max_seam_ratio if gated else NOMINAL_SEAM_RATIO),
        _check("duplicate_edges", ("fail" if gated else "warn") if duplicates else "pass", duplicates, 0),
    ]
    for name, key in (("seam_rgb_mae", "visible_rgb_mae"), ("seam_alpha_mae", "alpha_mae")):
        limit = getattr(args, f"max_{name}")
        values = [join["full_edge"][key] for join in joins if join["full_edge"][key] is not None]
        worst = max(values) if values else None
        if limit is None:
            checks.append(_check(name, "skipped", worst, None))
        else:
            checks.append(_check(name, "fail" if worst is not None and worst > limit else "pass", worst, limit))
    return checks


# --------------------------------------------------------------------------- extraction

def validate_options(args: argparse.Namespace) -> None:
    if not 1 <= args.solid_alpha_threshold <= 255:
        raise ValueError("solid-alpha-threshold must be between 1 and 255.")
    if not math.isfinite(args.min_column_coverage) or not 0 < args.min_column_coverage <= 1:
        raise ValueError("min-column-coverage must be greater than 0 and at most 1.")
    for name in ("max_seam_rgb_mae", "max_seam_alpha_mae"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 255):
            raise ValueError(f"{name.replace('_', '-')} must be between 0 and 255.")
    if args.max_seam_ratio is not None and (not math.isfinite(args.max_seam_ratio) or args.max_seam_ratio <= 0):
        raise ValueError("max-seam-ratio must be a positive number.")
    for name in ("threshold", "edge_threshold"):
        if not 0 <= getattr(args, name) <= 442:
            raise ValueError(f"{name.replace('_', '-')} must be between 0 and 442 (RGB Euclidean distance).")
    for name in ("surface_tolerance_px", "decoration_band_px"):
        if getattr(args, name) < 0:
            raise ValueError(f"{name.replace('_', '-')} must be zero or positive.")


def _join_pairs(pieces: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every join a strip can contain: left_cap|middle, middle|middle (all ordered pairs), middle|right_cap."""
    left, *middles, right = pieces
    return ([(left, middle) for middle in middles] + [(a, b) for a in middles for b in middles]
            + [(middle, right) for middle in middles])


def _preview_roles(pieces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    left, *middles, right = pieces
    count = max(PREVIEW_MIDDLES, len(middles))
    return [left, *(middles[index % len(middles)] for index in range(count)), right]


def extract(args: argparse.Namespace) -> dict[str, Any]:
    validate_options(args)
    output = Path(args.output_dir).absolute()
    if os.path.lexists(output):
        raise FileExistsError(f"Output directory already exists: {output}")
    spec_bytes = Path(args.spec).read_bytes()
    try:
        spec = forge_core.parse_json(spec_bytes)  # D28: UTF-8 with an optional BOM
    except ValueError as exc:
        raise ValueError(f"Spec is not valid UTF-8 JSON: {exc}") from exc
    source, info = forge_core.load_rgba(args.input)
    pieces = validate_spec(spec, source.size)
    # Deliberately no trim, edge clean, bbox fit, resizing or anchor estimation.
    prepared, background = prepare_background(source, args)
    top, depth = spec["surface_y_px"], spec["collision_depth_px"]
    arrays: dict[str, np.ndarray] = {}
    issues: list[str] = []
    for piece in pieces:
        left, upper, right, bottom = piece["source_box"]
        pixels = prepared[upper:bottom, left:right].copy()
        pixels[pixels[..., 3] == 0] = 0  # published PNGs carry no RGB under alpha 0
        arrays[piece["id"]] = pixels
        band = pixels[top:top + depth, :, 3] >= args.solid_alpha_threshold
        coverage = band.mean(axis=0)
        x0, x1 = piece["collision_span_px"]
        bad_columns = (np.flatnonzero(coverage[x0:x1] < args.min_column_coverage) + x0).tolist()
        # Missing the declared top is a distinct defect, even if band coverage is relaxed.
        missing_top = (np.flatnonzero(~band[0, x0:x1]) + x0).tolist()
        surface = measure_surface(pixels[..., 3], top, piece["collision_span_px"], solid=args.solid_alpha_threshold,
                                  tolerance=args.surface_tolerance_px, decoration=args.decoration_band_px,
                                  measurable=args.background_mode != "opaque")
        piece.update({
            "path": f"{piece['id']}.png", "anchor_px": [0, top],
            "collision_rect_px": [x0, top, x1 - x0, depth],
            "surface_y_source_px": upper + top,
            "coverage": {
                "solid_fraction_by_column": coverage.tolist(),
                "minimum_column_fraction": float(coverage[x0:x1].min()),
                "insufficient_columns": bad_columns,
                "missing_surface_columns": missing_top,
                "non_solid_band_pixels": int((~band[:, x0:x1]).sum()),
            },
            "surface": surface,
        })
        if bad_columns or missing_top:
            issues.append(f"{piece['id']}: structural coverage fails in {len(bad_columns)} band columns; "
                          f"{len(missing_top)} declared surface columns are not solid.")
        if surface["columns_above_tolerance"]:
            issues.append(f"{piece['id']}: art rises up to {surface['max_rise_px']} px above the declared surface in "
                          f"{len(surface['columns_above_tolerance'])} collision columns (tolerance "
                          f"{args.surface_tolerance_px} px, decoration band {args.decoration_band_px} px); "
                          "actors would sink into it.")
    structural_passed = not issues

    single_middle = len(pieces) == 3
    joins = []
    warnings: list[str] = []
    for left_piece, right_piece in _join_pairs(pieces):
        left_pixels, right_pixels = arrays[left_piece["id"]], arrays[right_piece["id"]]
        left_edge, right_edge = left_pixels[:, -1, :], right_pixels[:, 0, :]
        full = edge_metrics(left_edge, right_edge)
        band_left, band_right = left_edge[top:top + depth], right_edge[top:top + depth]
        contact = ((band_left[:, 3] >= args.solid_alpha_threshold)
                   & (band_right[:, 3] >= args.solid_alpha_threshold))
        label = f"{left_piece['role']}->{right_piece['role']}"
        name = label if single_middle else f"{label} ({left_piece['id']}|{right_piece['id']})"
        seam = forge_core.edge_seam_report(left_pixels, right_pixels, left_start=left_piece["collision_span_px"][0],
                                           right_stop=right_piece["collision_span_px"][1], gate=args.max_seam_ratio)
        joins.append({"join": label, "left": left_piece["id"], "right": right_piece["id"], "full_edge": full,
                      "contact_band": {**edge_metrics(band_left, band_right),
                                       "y_range_px": [top, top + depth],
                                       "solid_contact_by_row": contact.tolist(),
                                       "solid_contact_fraction": float(contact.mean())},
                      "seam": seam})
        if args.max_seam_rgb_mae is not None and full["visible_rgb_mae"] is not None and full["visible_rgb_mae"] > args.max_seam_rgb_mae:
            issues.append(f"{name}: visible RGB edge MAE exceeds reviewed threshold {args.max_seam_rgb_mae}.")
        if args.max_seam_alpha_mae is not None and full["alpha_mae"] > args.max_seam_alpha_mae:
            issues.append(f"{name}: alpha edge MAE exceeds reviewed threshold {args.max_seam_alpha_mae}.")
        if seam["verdict"] == "seam":
            message = (f"{name}: normalised seam ratio {seam['seam_ratio']:.2f} exceeds "
                       f"{args.max_seam_ratio if args.max_seam_ratio is not None else NOMINAL_SEAM_RATIO}; "
                       "the join is sharper than the art around it.")
        elif seam["verdict"] == "duplicate_edge":
            message = (f"{name}: duplicated edge (join step {seam['seam']:.2f} vs neighbouring steps "
                       f"{seam['near_median']:.2f}); the texture stutters at every repeat.")
        else:
            continue
        (issues if args.max_seam_ratio is not None else warnings).append(message)
    if args.strict_qc and issues:
        raise ValueError("Platform strip QC failed:\n- " + "\n- ".join(issues))

    checks = build_checks(pieces, joins, args)
    status = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass")
    spec_ref = forge_core.file_ref(args.spec, output, sha256=forge_core.sha256_bytes(spec_bytes),
                                   size=len(spec_bytes))
    source_ref = forge_core.file_ref(args.input, output, sha256=info["sha256"], size=info["bytes"])
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "source": {**source_ref, "size": info["size"], "mode": info["source_mode"],
                   "bit_depth": info["bit_depth"], "conversion": info["conversion"]},
        "spec": {**spec_ref, "content": spec},
        "processing": {**background,
                       "despill_radius": args.despill_radius if args.background_mode == "chroma_key" else 0,
                       "geometry": "explicit_native_rectangles", "resized": False,
                       "trimmed": False, "aligned": False, "transparent_rgb": "zeroed"},
        "surface_y_px": top, "collision_depth_px": depth,
        "coordinate_contract": "Origin at crop top-left; +x right, +y down. Collision rect is [x,y,width,height]. "
                               "At uniform scale s, place crop at (collision_left-s*collision_span_px[0], "
                               "collision_top-s*surface_y_px). anchor_px is the visual left/top-surface anchor, "
                               "not necessarily the collision-left when a cap has outer padding. "
                               "Middle variants are interchangeable: any middle may follow the left cap, any "
                               "other middle or itself, and precede the right cap. "
                               "Collision is declared metadata; alpha coverage does not infer physical geometry.",
        "pieces": pieces, "joins": joins,
        "qc": {"passed": not issues, "structural_passed": structural_passed, "issues": issues,
               "warnings": warnings,
               "solid_alpha_threshold": args.solid_alpha_threshold,
               "min_column_coverage": args.min_column_coverage,
               "surface_tolerance_px": args.surface_tolerance_px,
               "decoration_band_px": args.decoration_band_px,
               "max_seam_ratio": args.max_seam_ratio,
               "max_seam_rgb_mae": args.max_seam_rgb_mae, "max_seam_alpha_mae": args.max_seam_alpha_mae,
               "seam_metrics_note": "seam.seam_ratio compares each join with the interior steps near it: about 1 "
                                    "or less looks like the art, well above 1 is a seam, a join much flatter than "
                                    "its neighbours duplicates an edge. Raw edge MAE values are 0..255 "
                                    "diagnostics; no seam is repaired and no aesthetic seamlessness is certified."},
    }
    with forge_core.staged_output(output) as stage:
        outputs = []
        for piece in pieces:
            forge_core.save_png(arrays[piece["id"]], stage / piece["path"])
            piece["sha256"] = forge_core.sha256_file(stage / piece["path"])
            outputs.append({"path": piece["path"], "sha256": piece["sha256"],
                            "bytes": (stage / piece["path"]).stat().st_size})
        x = 0
        placements = []
        strip = []
        for piece in _preview_roles(pieces):
            strip.append(arrays[piece["id"]])  # placed side by side: native RGBA values, no compositing
            placements.append({"role": piece["role"], "id": piece["id"], "left": x, "top": 0,
                               "size": list(piece["size"])})
            x += piece["size"][0]
        forge_core.save_png(np.concatenate(strip, axis=1), stage / PREVIEW_NAME)
        preview_sha = forge_core.sha256_file(stage / PREVIEW_NAME)
        payload["preview"] = {"path": PREVIEW_NAME, "size": [x, pieces[0]["size"][1]], "sha256": preview_sha,
                              "placements": placements}
        outputs.append({"path": PREVIEW_NAME, "sha256": preview_sha, "bytes": (stage / PREVIEW_NAME).stat().st_size})
        payload["qa"] = {
            "status": status,
            "method": "Exact rectangular crops of the keyed source. Collision band coverage and a solid declared "
                      "surface row per collision column; the first solid row above it (outside the decoration "
                      "band) against the surface tolerance; every join (left cap, each middle variant, right cap) "
                      "by premultiplied column steps normalised by the art's own steps near the join.",
            "notProven": [
                "artistic continuity, lighting or style match at joins (only pixel steps are measured)",
                "runtime behaviour: crossing every join, one-way platforms, slopes and pixel snapping",
                "that the declared collision depth matches the intended physics",
                "chroma keying quality beyond the legacy keyer rules",
            ],
            "checks": checks,
            "inputs": [source_ref, spec_ref],
            "outputs": outputs,
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        }
        forge_core.write_json(stage / MANIFEST_NAME, payload)
    return payload


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path,
                        help="Platform atlas: RGB(A), palette PNG with transparency, grey or grey+alpha.")
    parser.add_argument("--spec", required=True, type=Path,
                        help="JSON spec: surface_y_px, collision_depth_px and pieces (one left_cap, 1-16 middle "
                             "variants, one right_cap) with source_box and optional collision_span_px.")
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="New directory only; strict QC runs before atomic no-clobber publication.")
    parser.add_argument("--background-mode", choices=BACKGROUND_MODES, default="chroma_key",
                        help="chroma_key removes magenta; native_alpha requires real transparency; opaque requires "
                             "opaque input. All keep the explicit rectangular crop geometry.")
    parser.add_argument("--threshold", type=int, default=100, help="Chroma: clear pixels this close to #FF00FF.")
    parser.add_argument("--edge-threshold", type=int, default=150,
                        help="Chroma: clear border-connected pixels this close to #FF00FF.")
    parser.add_argument("--despill-radius", type=int, choices=range(4), default=0,
                        help="Chroma only: remove magenta excess within this many px of transparency (0-3); "
                             "native_alpha and opaque ignore it. May neutralise legitimate purple at "
                             "transparent boundaries.")
    parser.add_argument("--solid-alpha-threshold", type=int, default=255,
                        help="Alpha required for the declared surface and collision band (1..255).")
    parser.add_argument("--min-column-coverage", type=float, default=1.0,
                        help="Minimum solid fraction in each collision-band column, (0,1]. The declared top row "
                             "must still be solid in every collision column.")
    parser.add_argument("--surface-tolerance-px", type=int, default=0,
                        help="How far the art's top may rise above the declared surface in a collision column.")
    parser.add_argument("--decoration-band-px", type=int, default=0,
                        help="Rows directly above the declared surface that may hold non-structural decoration "
                             "(grass tips); they are ignored when measuring the surface.")
    parser.add_argument("--max-seam-ratio", type=float,
                        help="Gate joins on the normalised seam ratio (try 1.25): a join sharper than the art "
                             "around it, or a duplicated edge, becomes a QC issue. Omitted: diagnostic only.")
    parser.add_argument("--max-seam-rgb-mae", type=float,
                        help="Legacy raw-equality gate: full-edge RGB MAE (0..255) for rows visible on both sides.")
    parser.add_argument("--max-seam-alpha-mae", type=float,
                        help="Legacy raw-equality gate: full-edge alpha MAE (0..255).")
    parser.add_argument("--strict-qc", action="store_true",
                        help="Fail without publishing if structural, surface or explicitly gated seam checks fail.")
    return parser


def _cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = extract(args)
    except (OSError, ValueError) as exc:
        message = f"file not found: {exc.filename}" if isinstance(exc, FileNotFoundError) and exc.filename else exc
        print(f"error: {forge_core.ascii_text(str(message))}", file=sys.stderr)
        return 1
    output = Path(args.output_dir).absolute()
    summary = {"output": str(output), "manifest": str(output / MANIFEST_NAME), "schema": payload["schema"],
               "status": payload["qa"]["status"],
               "qc": {key: payload["qc"][key] for key in ("passed", "structural_passed", "issues", "warnings")}}
    print(json.dumps(summary, ensure_ascii=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27): usage errors exit 2, runtime errors print
    'error: <message>' and exit 1, and no traceback reaches the user."""
    return forge_core.run_cli(_cli, argv)


if __name__ == "__main__":
    raise SystemExit(main())
