#!/usr/bin/env python3
"""Extract native rectangular L/M/R platform pieces and measure their joins."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


SCHEMA = "generate2dmap.platform_strip.v1"
ROLES = ("left_cap", "middle", "right_cap")
PREVIEW_ROLES = ("left_cap", "middle", "middle", "middle", "right_cap")


def sprite_helpers() -> Any:
    """Reuse the map chroma extractor without requiring an installed host skill."""
    path = Path(__file__).with_name("extract_prop_pack.py")
    name = "_platform_strip_sprite_helpers"
    cached = sys.modules.get(name)
    if cached is not None and Path(cached.__file__).resolve() == path:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load required sprite helpers: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def prepare_background(source: Image.Image, args: argparse.Namespace) -> Image.Image:
    image = source.convert("RGBA")
    alpha = image.getchannel("A").getextrema()
    if args.background_mode == "native_alpha":
        if alpha[0] == 255 or alpha[1] == 0:
            raise ValueError("native_alpha requires visible art and real transparent pixels.")
        return image
    if args.background_mode == "opaque":
        if alpha != (255, 255):
            raise ValueError("opaque mode cannot discard source transparency.")
        return image
    return sprite_helpers().remove_bg_magenta(image, args.threshold, args.edge_threshold)


def publish_directory_no_replace(stage: Path, final: Path) -> None:
    """Publish after QC without replacing even a racing empty destination."""
    if os.path.lexists(final):
        raise FileExistsError(f"Refusing existing output: {final}")
    if os.name == "nt":
        os.rename(stage, final)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(stage), -100, os.fsencode(final), 1)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(stage), os.fsencode(final), 4)
    else:
        raise RuntimeError("This platform lacks a supported atomic no-replace directory rename.")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(final))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer.")
    return value


def validate_spec(data: Any, source_size: tuple[int, int]) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("Spec must be a JSON object.")
    top = integer(data.get("surface_y_px"), "surface_y_px")
    depth = integer(data.get("collision_depth_px"), "collision_depth_px")
    raw_pieces = data.get("pieces")
    if not isinstance(raw_pieces, list) or len(raw_pieces) != 3:
        raise ValueError("Spec requires exactly three pieces: left_cap, middle, right_cap.")
    pieces: dict[str, dict[str, Any]] = {}
    ids: set[str] = set()
    reserved = {"strip-preview", "platform-strip"}
    for raw in raw_pieces:
        if not isinstance(raw, dict):
            raise ValueError("Each piece must be an object.")
        identity, role, box = raw.get("id"), raw.get("role"), raw.get("source_box")
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identity)
                or identity.lower() in ids | reserved
                or identity.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                        *(f"LPT{i}" for i in range(1, 10))}):
            raise ValueError("Piece id must be a unique, portable filename stem, excluding reserved names.")
        if not isinstance(role, str) or role not in ROLES or role in pieces:
            raise ValueError("Each of left_cap, middle, right_cap must occur exactly once.")
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
        pieces[role] = {"id": identity, "role": role, "source_box": box,
                        "size": [right - left, bottom - upper], "collision_span_px": span}
    ordered = [pieces[role] for role in ROLES]
    heights = {piece["size"][1] for piece in ordered}
    if len(heights) != 1:
        raise ValueError("All native rectangular pieces must have the same height; no resizing is performed.")
    if top < 0 or depth <= 0 or top + depth > ordered[0]["size"][1]:
        raise ValueError("Declared surface_y_px and collision_depth_px must define a nonempty in-canvas band.")
    return ordered


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


def validate_options(args: argparse.Namespace) -> None:
    if not 1 <= args.solid_alpha_threshold <= 255:
        raise ValueError("solid-alpha-threshold must be between 1 and 255.")
    if not math.isfinite(args.min_column_coverage) or not 0 < args.min_column_coverage <= 1:
        raise ValueError("min-column-coverage must be greater than 0 and at most 1.")
    for name in ("max_seam_rgb_mae", "max_seam_alpha_mae"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 255):
            raise ValueError(f"{name} must be between 0 and 255.")
    for name in ("threshold", "edge_threshold"):
        if not 0 <= getattr(args, name) <= 442:
            raise ValueError(f"{name} must be between 0 and 442 (RGB Euclidean distance).")


def extract(args: argparse.Namespace) -> dict[str, Any]:
    validate_options(args)
    output = args.output_dir.absolute()
    if os.path.lexists(output):
        raise FileExistsError(f"Output directory already exists: {output}")
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    with Image.open(args.input) as opened:
        source_mode = opened.mode
        if source_mode not in {"RGB", "RGBA"}:
            raise ValueError("Input must be an 8-bit RGB or RGBA image.")
        source = opened.copy()
    pieces = validate_spec(spec, source.size)
    # Deliberately no trim_border, clean_edges, bbox fit, resizing, or anchor estimation.
    prepared = prepare_background(source, args)
    top, depth = spec["surface_y_px"], spec["collision_depth_px"]
    images: dict[str, Image.Image] = {}
    arrays: dict[str, np.ndarray] = {}
    issues: list[str] = []
    for piece in pieces:
        image = prepared.crop(piece["source_box"])
        pixels = np.asarray(image)
        images[piece["role"]] = image
        arrays[piece["role"]] = pixels
        band = pixels[top:top + depth, :, 3] >= args.solid_alpha_threshold
        coverage = band.mean(axis=0)
        x0, x1 = piece["collision_span_px"]
        bad_columns = (np.flatnonzero(coverage[x0:x1] < args.min_column_coverage) + x0).tolist()
        # Missing the declared top is a distinct defect, even if band coverage is relaxed.
        missing_top = (np.flatnonzero(~band[0, x0:x1]) + x0).tolist()
        piece.update({
            "path": f"{piece['id']}.png", "anchor_px": [0, top],
            "collision_rect_px": [x0, top, x1 - x0, depth],
            "surface_y_source_px": piece["source_box"][1] + top,
            "coverage": {
                "solid_fraction_by_column": coverage.tolist(),
                "minimum_column_fraction": float(coverage[x0:x1].min()),
                "insufficient_columns": bad_columns,
                "missing_surface_columns": missing_top,
                "non_solid_band_pixels": int((~band[:, x0:x1]).sum()),
            },
        })
        if bad_columns or missing_top:
            issues.append(f"{piece['id']}: structural coverage fails in {len(bad_columns)} band columns; "
                          f"{len(missing_top)} declared surface columns are not solid.")
    structural_passed = not issues
    joins = []
    for left_role, right_role in (("left_cap", "middle"), ("middle", "middle"), ("middle", "right_cap")):
        left_edge = arrays[left_role][:, -1, :]
        right_edge = arrays[right_role][:, 0, :]
        full = edge_metrics(left_edge, right_edge)
        band_left, band_right = left_edge[top:top + depth], right_edge[top:top + depth]
        contact = ((band_left[:, 3] >= args.solid_alpha_threshold)
                   & (band_right[:, 3] >= args.solid_alpha_threshold))
        label = f"{left_role}->{right_role}"
        joins.append({"join": label, "full_edge": full,
                      "contact_band": {**edge_metrics(band_left, band_right),
                                       "y_range_px": [top, top + depth],
                                       "solid_contact_by_row": contact.tolist(),
                                       "solid_contact_fraction": float(contact.mean())}})
        if args.max_seam_rgb_mae is not None and full["visible_rgb_mae"] is not None and full["visible_rgb_mae"] > args.max_seam_rgb_mae:
            issues.append(f"{label}: visible RGB edge MAE exceeds reviewed threshold {args.max_seam_rgb_mae}.")
        if args.max_seam_alpha_mae is not None and full["alpha_mae"] > args.max_seam_alpha_mae:
            issues.append(f"{label}: alpha edge MAE exceeds reviewed threshold {args.max_seam_alpha_mae}.")
    if args.strict_qc and issues:
        raise ValueError("Platform strip QC failed:\n- " + "\n- ".join(issues))

    source_hash, spec_hash = sha256(args.input), sha256(args.spec)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "source": {"path": str(args.input.resolve()), "sha256": source_hash,
                   "size": list(source.size), "mode": source_mode},
        "spec": {"path": str(args.spec.resolve()), "sha256": spec_hash, "content": spec},
        "processing": {"background_mode": args.background_mode,
                       "threshold": args.threshold, "edge_threshold": args.edge_threshold,
                       "geometry": "explicit_native_rectangles", "resized": False,
                       "trimmed": False, "aligned": False},
        "surface_y_px": top, "collision_depth_px": depth,
        "coordinate_contract": "Origin at crop top-left; +x right, +y down. Collision rect is [x,y,width,height]. "
                               "At uniform scale s, place crop at (collision_left-s*collision_span_px[0], "
                               "collision_top-s*surface_y_px). anchor_px is the visual left/top-surface anchor, "
                               "not necessarily the collision-left when a cap has outer padding. "
                               "Collision is declared metadata; alpha coverage does not infer physical geometry.",
        "pieces": pieces, "joins": joins,
        "qc": {"passed": not issues, "structural_passed": structural_passed, "issues": issues,
               "solid_alpha_threshold": args.solid_alpha_threshold,
               "min_column_coverage": args.min_column_coverage,
               "max_seam_rgb_mae": args.max_seam_rgb_mae, "max_seam_alpha_mae": args.max_seam_alpha_mae,
               "seam_metrics_note": "0..255 edge diagnostics. RGB uses rows visible on both sides; "
                                    "alpha and premultiplied RGB also expose transparency discontinuities. "
                                    "No seam is repaired and no aesthetic seamlessness is certified."},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        for piece in pieces:
            images[piece["role"]].save(stage / piece["path"])
            piece["sha256"] = sha256(stage / piece["path"])
        preview = Image.new("RGBA", (sum(images[role].width for role in PREVIEW_ROLES), images["left_cap"].height))
        x = 0
        placements = []
        for role in PREVIEW_ROLES:
            image = images[role]
            preview.paste(image, (x, 0))  # No alpha mask: preserve native RGBA values.
            placements.append({"role": role, "left": x, "top": 0, "size": list(image.size)})
            x += image.width
        preview.save(stage / "strip-preview.png")
        payload["preview"] = {"path": "strip-preview.png", "size": list(preview.size),
                              "sha256": sha256(stage / "strip-preview.png"), "placements": placements}
        (stage / "platform-strip.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        publish_directory_no_replace(stage, output)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path, help="New directory only; strict QC runs before atomic no-clobber publication.")
    parser.add_argument("--background-mode", choices=("chroma_key", "native_alpha", "opaque"), default="chroma_key",
                        help="Chroma removes magenta; native_alpha requires real transparency; opaque requires opaque input. All retain explicit rectangular crop geometry.")
    parser.add_argument("--threshold", type=int, default=100)
    parser.add_argument("--edge-threshold", type=int, default=150)
    parser.add_argument("--solid-alpha-threshold", type=int, default=255, help="Alpha required for declared surface and collision band (1..255).")
    parser.add_argument("--min-column-coverage", type=float, default=1.0,
                        help="Minimum solid fraction in each collision-band column, (0,1]. Declared top row must still be solid in every column.")
    parser.add_argument("--max-seam-rgb-mae", type=float, help="Optional reviewed full-edge RGB MAE threshold (0..255), for rows visible on both sides; omitted means diagnostic only.")
    parser.add_argument("--max-seam-alpha-mae", type=float, help="Optional reviewed full-edge alpha MAE threshold (0..255); omitted means diagnostic only.")
    parser.add_argument("--strict-qc", action="store_true", help="Fail without publishing if structural or explicitly configured seam checks fail.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = extract(args)
    print(json.dumps({"manifest": str((args.output_dir / "platform-strip.json").resolve()), "qc": result["qc"]}))


if __name__ == "__main__":
    main()
