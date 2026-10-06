#!/usr/bin/env python3
"""Extract map props from a grid, measured boxes or auto-detected boxes (prop_pack.v2).

Each prop becomes <output-dir>/<label>/prop.png plus one prop-pack.json manifest
(generate2dmap.prop_pack.v2): manifest-relative POSIX paths with sha256, the
cell box and the sheet rectangle of every image, the padding actually applied,
an integer ground anchor (anchor_px), dropped pixels and a QA envelope. The
output directory must be new; work is staged beside it and published only
after QA passes, so a failed run leaves nothing behind.

Chroma sheets use the legacy #FF00FF keyer plus edge despill (radius 1 by
default; --despill-radius 0 is the old output). Native-alpha sheets are never
keyed. Components are 8-connected (--connectivity 4 is the old rule).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, NamedTuple, Sequence

import numpy as np

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_matte  # noqa: E402


SCHEMA = "generate2dmap.prop_pack.v2"
CROP_BOXES_SCHEMA = "forge-crop-boxes/v1"
TOOL_NAME = "extract_prop_pack"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # D29: QA envelopes carry the package version
MANIFEST_NAME = "prop-pack.json"
AUTO_BOXES_NAME = "auto-boxes.json"

DESPILL_MARGIN = 12  # the sprite skill's despill_chroma_edges margin (forge_matte edge mode)
EMPTY_TOKENS = frozenset({"empty", "skip", "-"})
RESERVED_STEMS = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                            *(f"LPT{i}" for i in range(1, 10))})
MAX_SLUG_LENGTH = 64
LABEL_SEPARATORS = re.compile("[,\uff0c\u3001]")  # ASCII, full-width and ideographic commas

# --min-component-area auto: 100 px for a 418 px cell (one cell of a 1254 px 3x3 sheet, the
# cfed170 default), scaled with the cell area and kept between 1 and 100.
MIN_AREA_REFERENCE_CELL = 418 * 418
MIN_AREA_REFERENCE = 100

# Props rest on bases, not feet: the support band is the bottom quarter of the art (a character's
# feet band is about 6%). It sets the anchor column and the suggested footprint width.
SUPPORT_BAND_FRACTION = 0.25
STRATEGY_ASPECT_LIMIT = 1.6      # asset strategy gate: taller or wider than this is not a compact cell prop
DROPPED_WARN_FRACTION = 0.01     # warn when a prop loses more than 1% of its cell's visible pixels
EDGE_FRINGE_BAND_PX = 2          # fringe is measured on visible pixels within 2 px of transparency
EDGE_FRINGE_WARN_FRACTION = 0.02
MAX_WORLD_SCALE_DENOMINATOR = 64

AUTO_SOLID_ALPHA = 128           # auto boxes grow objects from pixels at least this opaque
AUTO_AREA_PER_SOLID_PX = 30000   # a seed needs 1 solid px per this many sheet px (52 px on 1254^2)
AUTO_GAP_DIVISOR = 52            # parts within short side / 52 px merge (24 px on 1254^2)
AUTO_ATTACH_DIVISOR = 156        # fainter pixels attach within short side / 156 px (8 px on 1254^2)

ART_SOURCES = ("host_image", "api", "code", "existing", "video", "mixed")
OCCLUSION_CLASSES = ("low", "tall", "foreground")
OCCUPANT_POLICIES = ("y_sort", "rear_shift_and_fade", "static_front", "static_back")
FOOTPRINT_SHAPES = ("ellipse", "rect", "none")

NOT_PROVEN = (
    "that each label names the art in its cell or box",
    "that a prop is complete: clipping is detected only where art meets the cell edge",
    "keyed-edge quality beyond the edge fringe count",
    "that anchors and suggested footprints match gameplay contact (both are measured from alpha)",
    "runtime placement, collision or occlusion",
)


# --------------------------------------------------------------------------- small helpers

def _number(value: float) -> int | float:
    """JSON-friendly number: whole values become int."""
    return int(value) if float(value).is_integer() else float(value)


def _touches_edge(box: Sequence[int] | None, width: int, height: int, margin: int) -> bool:
    """cfed170 bbox_touches_edge: art within ``margin`` px of a cell side."""
    if box is None:
        return False
    x0, y0, x1, y1 = box
    return x0 <= margin or y0 <= margin or x1 >= width - margin or y1 >= height - margin


def clean_edges(pixels: np.ndarray, depth: int) -> None:
    """cfed170 opt-in edge clean, in place: clear dark (every channel < 40) or near-magenta
    (RGB distance < 150) visible pixels in the outer ``depth`` rings."""
    if depth <= 0:
        return
    ring = np.zeros(pixels.shape[:2], bool)
    ring[:depth] = True
    ring[-depth:] = True
    ring[:, :depth] = True
    ring[:, -depth:] = True
    rgb = pixels[..., :3].astype(np.int32)
    dark = (rgb < 40).all(axis=-1)
    near_key = (rgb[..., 0] - 255) ** 2 + rgb[..., 1] ** 2 + (rgb[..., 2] - 255) ** 2 < 150 ** 2
    pixels[ring & (pixels[..., 3] > 0) & (dark | near_key)] = 0


def auto_min_area(cell_area: int) -> int:
    """--min-component-area auto for a cell of ``cell_area`` px."""
    scaled = forge_core.round_half_up(MIN_AREA_REFERENCE * cell_area / MIN_AREA_REFERENCE_CELL)
    return max(1, min(MIN_AREA_REFERENCE, scaled))


def edge_fringe(pixels: np.ndarray) -> tuple[int, int]:
    """(fringe px, band px): visible pixels within 2 px of transparency, and those whose
    magenta excess min(R, B) - G exceeds the despill margin (MAP-03)."""
    alpha = pixels[..., 3]
    band = forge_core.dilate_square(alpha == 0, EDGE_FRINGE_BAND_PX) & (alpha > 0)
    rgb = pixels[..., :3].astype(np.int16)
    excess = np.minimum(rgb[..., 0], rgb[..., 2]) - rgb[..., 1]
    return int(np.count_nonzero(band & (excess > DESPILL_MARGIN))), int(np.count_nonzero(band))


# --------------------------------------------------------------------------- labels

class CellLabel(NamedTuple):
    slug: str          # directory name; "" when the cell is skipped
    display_name: str  # the label as written (any script)


def slugify(text: str) -> tuple[str, bool]:
    """ASCII filename slug of ``text`` and whether letters or digits were lost (CJK, MAP-23)."""
    decomposed = unicodedata.normalize("NFKD", text.strip())
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    lossy = any(not char.isascii() and char.isalnum() for char in stripped)
    slug = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")[:MAX_SLUG_LENGTH].strip("-")
    return slug, lossy


def resolve_label(raw: str | None, index: int, *, allow_skip: bool = True) -> CellLabel:
    """Slug and display name of the label at ``index``.

    A missing label or one without ASCII letters becomes ``prop-<index+1>``; a slug
    that dropped non-ASCII letters gets ``-<index+1>`` so different CJK labels stay
    distinct ("oak tree" in two scripts). The original text is the display name.
    """
    text = (raw or "").strip()
    if text.lower() in EMPTY_TOKENS:
        if not allow_skip:
            raise ValueError("Explicit crop boxes must name props; omit empty cells.")
        return CellLabel("", text)
    slug, lossy = slugify(text)
    if not slug:
        slug = f"prop-{index + 1}"
    elif lossy:
        slug = f"{slug}-{index + 1}"
    if slug.upper() in RESERVED_STEMS:
        raise ValueError(f"Label {text!r} becomes the reserved Windows device name {slug!r}; choose another label.")
    return CellLabel(slug, text or slug)


def _check_unique(labels: Sequence[CellLabel]) -> None:
    seen: dict[str, str] = {}
    for label in labels:
        if label.slug in seen:
            raise ValueError(f"Labels must remain unique after filename normalization: {seen[label.slug]!r} and "
                             f"{label.display_name!r} both become {label.slug!r}.")
        if label.slug:
            seen[label.slug] = label.display_name


def read_label_tokens(args: argparse.Namespace) -> list[str]:
    """Raw labels from --labels (ASCII, full-width or ideographic commas) or --labels-file (one per line)."""
    if args.labels_file:
        return [line.strip() for line in Path(args.labels_file).read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    if args.labels:
        return [token.strip() for token in LABEL_SEPARATORS.split(args.labels)]
    return []


def resolve_labels(tokens: Sequence[str], count: int) -> list[CellLabel]:
    """Row-major labels for ``count`` cells; missing labels become prop-<n>."""
    if len(tokens) > count:
        raise ValueError(f"Got {len(tokens)} labels for {count} cells.")
    labels = [resolve_label(tokens[index] if index < len(tokens) else None, index) for index in range(count)]
    _check_unique(labels)
    return labels


def parse_labels(args: argparse.Namespace, expected_count: int) -> list[CellLabel]:
    """Labels for ``expected_count`` cells from the parsed CLI options."""
    return resolve_labels(read_label_tokens(args), expected_count)


# --------------------------------------------------------------------------- layouts

@dataclass(frozen=True)
class CellSpec:
    """One cell or box to extract; ``box`` is [x0, y0, x1, y1) in sheet pixels."""

    index: int
    label: CellLabel
    box: tuple[int, int, int, int]
    grid: tuple[int, int] | None = None
    owner: int = 0                                      # auto-box object id (0: every pixel of the box)
    meta: dict[str, Any] = field(default_factory=dict)  # authored fields from a boxes file


def grid_boxes(width: int, height: int, rows: int, cols: int, rounding: str = "exact") -> list[tuple[int, ...]]:
    """Row-major cell boxes. ``exact`` needs dimensions that divide exactly; ``nearest`` rounds
    the cell edges half-up (cells differ by at most 1 px and cover every pixel once, MAP-04)."""
    if rows <= 0 or cols <= 0:
        raise ValueError("Positive rows and cols are required.")
    if rounding == "exact" and (width % cols or height % rows):
        raise ValueError(
            f"A {width}x{height} sheet does not divide exactly into {rows} rows x {cols} columns. "
            "Pass --grid-rounding nearest for rounded cell edges, or measured --boxes-file.")
    return forge_core.rounded_grid_boxes(width, height, rows, cols)


def _finite_pair(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value))


def _validate_footprint(footprint: Any, label: str) -> dict[str, Any]:
    if not isinstance(footprint, dict) or footprint.get("shape") not in FOOTPRINT_SHAPES:
        raise ValueError(f"Crop {label!r} footprint needs a shape: {', '.join(FOOTPRINT_SHAPES)}.")
    if footprint["shape"] != "none":
        for key in ("width", "depth"):
            value = footprint.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Crop {label!r} footprint {key} must be a finite number >= 0.")
    if "offset" in footprint and not _finite_pair(footprint["offset"]):
        raise ValueError(f"Crop {label!r} footprint offset must be [dx, dy].")
    return dict(footprint)


def _authored_fields(entry: dict[str, Any], label: str, box: tuple[int, int, int, int]) -> dict[str, Any]:
    """Validate the optional per-prop fields of a boxes file; anchor_px is in box pixels."""
    meta: dict[str, Any] = {}
    width, height = box[2] - box[0], box[3] - box[1]
    if "anchor_px" in entry:
        anchor = entry["anchor_px"]
        if not _finite_pair(anchor) or not (0 <= anchor[0] <= width and 0 <= anchor[1] <= height):
            raise ValueError(f"Crop {label!r} anchor_px must be two finite numbers inside its {width}x{height} box.")
        meta["anchor_px"] = [_number(v) for v in anchor]
    if "display_name" in entry:
        if not isinstance(entry["display_name"], str) or not entry["display_name"].strip():
            raise ValueError(f"Crop {label!r} display_name must be a nonempty string.")
        meta["display_name"] = entry["display_name"].strip()
    if "footprint" in entry:
        meta["footprint"] = _validate_footprint(entry["footprint"], label)
    if "solid" in entry:
        if not isinstance(entry["solid"], bool):
            raise ValueError(f"Crop {label!r} solid must be true or false.")
        meta["solid"] = entry["solid"]
    if "contact" in entry:
        meta["contact"] = entry["contact"]
    for key, allowed in (("occlusion_class", OCCLUSION_CLASSES), ("occupant_policy", OCCUPANT_POLICIES)):
        if key in entry:
            if entry[key] not in allowed:
                raise ValueError(f"Crop {label!r} {key} must be one of {', '.join(allowed)}.")
            meta[key] = entry[key]
    return meta


def read_crop_boxes(path: Path, size: tuple[int, int]) -> list[CellSpec]:
    """Load inspected, nonoverlapping native boxes; never infer or resize art (DOC-18).

    Accepts the unified ``{"items": [{"id", "box"}]}`` form (optional ``schema``
    ``forge-crop-boxes/v1``), the legacy ``{"props": [{"label", "source_box"}]}``
    alias and a bare list of boxes. Boxes are integer ``[x0, y0, x1, y1)`` inside
    the sheet. Items may also carry anchor_px (box pixels), display_name,
    footprint, solid, contact, occlusion_class and occupant_policy.
    """
    data = forge_core.read_json(path)  # D28: UTF-8 with an optional BOM
    if isinstance(data, dict) and data.get("schema", CROP_BOXES_SCHEMA) != CROP_BOXES_SCHEMA:
        raise ValueError(f"Crop specification schema {data['schema']!r} is not {CROP_BOXES_SCHEMA!r}.")
    entries: Any = None
    if isinstance(data, dict) and "items" in data:
        entries, id_key, box_key = data["items"], "id", "box"
    elif isinstance(data, dict) and "props" in data:
        entries, id_key, box_key = data["props"], "label", "source_box"
    elif isinstance(data, list):
        entries, id_key, box_key = [{"box": box} for box in data], None, "box"
    if not isinstance(entries, list) or not entries:
        raise ValueError("Crop specification requires a nonempty 'items' list (or the legacy 'props' list).")
    width, height = size
    specs: list[CellSpec] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("Each crop must be an object with an id and a box.")
        if id_key is None:
            label = resolve_label(None, index)
        else:
            raw = entry.get(id_key)
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("Each crop requires a nonempty string label.")
            label = resolve_label(raw, index, allow_skip=False)
        raw_box = entry.get(box_key)
        if not isinstance(raw_box, list) or len(raw_box) != 4 or not all(type(v) is int for v in raw_box):
            raise ValueError(f"Crop {label.slug!r} {box_key} must contain four integer pixel coordinates.")
        x0, y0, x1, y1 = raw_box
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError(f"Crop {label.slug!r} {box_key} must be nonempty and inside the source canvas.")
        for other in specs:
            ox0, oy0, ox1, oy1 = other.box
            if max(x0, ox0) < min(x1, ox1) and max(y0, oy0) < min(y1, oy1):
                raise ValueError(f"Crop {label.slug!r} overlaps crop {other.label.slug!r}.")
        box = (x0, y0, x1, y1)
        meta = _authored_fields(entry, label.slug, box)
        if "display_name" in meta:
            label = CellLabel(label.slug, meta.pop("display_name"))
        specs.append(CellSpec(index, label, box, meta=meta))
    _check_unique([spec.label for spec in specs])
    return specs


def _grow_labels(owner: np.ndarray, allowed: np.ndarray, steps: int) -> None:
    """Breadth-first, in place: each allowed unlabelled pixel takes the smallest label among its
    8 neighbours, one ring per step, so fainter pixels join the object they touch."""
    height, width = owner.shape
    none = np.iinfo(np.int32).max
    for _ in range(steps):
        pending = allowed & (owner == 0)
        if not pending.any():
            return
        padded = np.pad(np.where(owner > 0, owner, none), 1, constant_values=none)
        nearest = np.full(owner.shape, none, np.int32)
        for dy in range(3):
            for dx in range(3):
                if dy != 1 or dx != 1:
                    np.minimum(nearest, padded[dy:dy + height, dx:dx + width], out=nearest)
        fill = pending & (nearest < none)
        if not fill.any():
            return
        owner[fill] = nearest[fill]


def _reading_order(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows by vertical centre (an object whose centre lies above the row's bottom joins it), then x."""
    rows: list[list[dict[str, Any]]] = []
    bottom = -math.inf
    for item in sorted(objects, key=lambda o: (o["content"][1] + o["content"][3], o["content"][0])):
        if rows and (item["content"][1] + item["content"][3]) / 2 < bottom:
            rows[-1].append(item)
            bottom = max(bottom, item["content"][3])
        else:
            rows.append([item])
            bottom = item["content"][3]
    return [item for row in rows for item in sorted(row, key=lambda o: (o["content"][0] + o["content"][2], o["content"][1]))]


def find_auto_boxes(pixels: np.ndarray, *, gap: int, attach: int, margin: int,
                    min_solid_area: int) -> tuple[list[dict[str, Any]], np.ndarray, dict[str, Any]]:
    """Find prop objects without a grid (report v2 P1-6).

    Seeds are 8-connected components of alpha >= 128 with at least
    ``min_solid_area`` px; seeds within ``gap`` px (Chebyshev) form one object.
    Every other visible pixel joins the object it reaches through visible pixels
    within ``attach`` steps; the rest stays unowned. A box is the object's
    content box plus ``margin`` px, clamped to the sheet. Boxes may overlap: the
    returned owner map (object ids, 0 for none) assigns every pixel. Objects come
    in reading order: rows by vertical centre, then left to right.
    """
    alpha = pixels[..., 3]
    height, width = alpha.shape
    solid = alpha >= AUTO_SOLID_ALPHA
    labels, count = forge_core.label_components(solid, 8)
    big = np.bincount(labels.ravel(), minlength=count + 1) >= min_solid_area
    big[0] = False
    seeds = big[labels]
    owner = np.zeros(alpha.shape, np.int32)
    if seeds.any():
        groups, _ = forge_core.label_components(forge_core.dilate_square(seeds, (gap + 1) // 2), 8)
        owner[seeds] = groups[seeds]
        _grow_labels(owner, alpha > 0, attach)
    ys, xs = np.nonzero(owner)
    ids = owner[ys, xs]
    objects: list[dict[str, Any]] = []
    if ids.size:
        top = int(ids.max()) + 1
        x0, y0 = np.full(top, width, np.int64), np.full(top, height, np.int64)
        x1, y1 = np.zeros(top, np.int64), np.zeros(top, np.int64)
        np.minimum.at(x0, ids, xs)
        np.minimum.at(y0, ids, ys)
        np.maximum.at(x1, ids, xs + 1)
        np.maximum.at(y1, ids, ys + 1)
        for object_id in np.flatnonzero(np.bincount(ids, minlength=top)).tolist():
            content = (int(x0[object_id]), int(y0[object_id]), int(x1[object_id]), int(y1[object_id]))
            objects.append({"owner": object_id, "content": content,
                            "box": (max(0, content[0] - margin), max(0, content[1] - margin),
                                    min(width, content[2] + margin), min(height, content[3] + margin))})
    report = {
        "objects": len(objects), "solid_alpha": AUTO_SOLID_ALPHA, "min_solid_area": min_solid_area,
        "gap_px": gap, "attach_px": attach, "margin_px": margin,
        "unowned_px": int(np.count_nonzero((alpha > 0) & (owner == 0))),
    }
    return _reading_order(objects), owner, report


# --------------------------------------------------------------------------- options

def parse_min_area(text: str) -> int | None:
    """argparse type for --min-component-area: a positive integer, or auto (None)."""
    if text.strip().lower() == "auto":
        return None
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer or auto") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("min-component-area must be positive")
    return value


def parse_world_scale(text: str) -> Fraction:
    """argparse type for --world-scale: an exact positive decimal or fraction such as 0.375 or 3/8."""
    try:
        scale = Fraction(text.strip())
    except (ValueError, ZeroDivisionError):
        raise argparse.ArgumentTypeError("expected a decimal or a fraction such as 0.5 or 3/8") from None
    if scale <= 0:
        raise argparse.ArgumentTypeError("world scale must be positive")
    if scale.denominator > MAX_WORLD_SCALE_DENOMINATOR:
        hint = scale.limit_denominator(MAX_WORLD_SCALE_DENOMINATOR)
        raise argparse.ArgumentTypeError(
            f"{text} needs a {scale.denominator} px padding step; write an exact fraction such as {hint}")
    return scale


@dataclass(frozen=True)
class Options:
    """Validated settings that shape every cell."""

    layout: str                      # grid | explicit_boxes | auto_boxes
    component_mode: str
    connectivity: int
    min_component_area: int | None   # None: auto per cell
    component_padding: int
    trim_border: int
    edge_clean_depth: int
    edge_touch_margin: int
    keep_canvas: bool
    keep_empty: bool
    anchor_mode: str
    world_scale: Fraction | None
    footprint_shape: str | None
    footprint_depth_ratio: float
    hygiene_mode: str
    hygiene_floor: int


def _resolve_options(args: argparse.Namespace) -> Options:
    if args.boxes_file and args.auto_boxes:
        raise ValueError("Use --boxes-file or --auto-boxes, not both.")
    if args.boxes_file:
        if any(value is not None for value in (args.rows, args.cols, args.labels, args.labels_file)):
            raise ValueError("boxes-file cannot be combined with rows, cols, labels or labels-file.")
        layout = "explicit_boxes"
    elif args.auto_boxes:
        if args.rows is not None or args.cols is not None:
            raise ValueError("auto-boxes cannot be combined with rows or cols.")
        layout = "auto_boxes"
    elif args.rows is None or args.cols is None or args.rows <= 0 or args.cols <= 0:
        raise ValueError("Positive rows and cols are required unless boxes-file or auto-boxes is supplied.")
    else:
        layout = "grid"
    if args.labels and args.labels_file:
        raise ValueError("Use --labels or --labels-file, not both.")
    for name in ("trim_border", "edge_clean_depth", "component_padding", "edge_touch_margin"):
        if getattr(args, name) < 0:
            raise ValueError(f"{name} must be nonnegative.")
    if args.alpha_floor is not None and not 0 <= args.alpha_floor <= 254:
        raise ValueError("alpha-floor must be between 0 and 254.")
    if not 0 <= args.threshold <= args.edge_threshold <= 442:
        raise ValueError("Chroma thresholds must satisfy 0 <= threshold <= edge-threshold <= 442.")
    if args.max_dropped_fraction is not None and not 0 <= args.max_dropped_fraction <= 1:
        raise ValueError("max-dropped-fraction must be between 0 and 1.")
    if not (math.isfinite(args.footprint_depth_ratio) and 0 < args.footprint_depth_ratio <= 4):
        raise ValueError("footprint-depth-ratio must be greater than 0 and at most 4.")
    if args.auto_box_gap is not None and args.auto_box_gap < 0:
        raise ValueError("auto-box-gap must be nonnegative.")
    hygiene = args.alpha_hygiene
    if hygiene is None:
        hygiene = "both" if layout == "auto_boxes" else ("floor" if args.alpha_floor else "none")
    return Options(
        layout=layout,
        component_mode=args.component_mode or ("all" if layout == "auto_boxes" else "largest"),
        connectivity=args.connectivity,
        min_component_area=args.min_component_area,
        component_padding=args.component_padding,
        trim_border=args.trim_border,
        edge_clean_depth=args.edge_clean_depth,
        edge_touch_margin=args.edge_touch_margin,
        keep_canvas=args.keep_canvas,
        keep_empty=args.keep_empty,
        anchor_mode=args.anchor_mode,
        world_scale=args.world_scale,
        footprint_shape=args.suggest_footprint,
        footprint_depth_ratio=args.footprint_depth_ratio,
        hygiene_mode=hygiene,
        hygiene_floor=4 if args.alpha_floor is None else args.alpha_floor,
    )


# --------------------------------------------------------------------------- keying

def key_sheet(pixels: np.ndarray, background_mode: str, threshold: float = 100, edge_threshold: float = 150,
              despill_radius: int = 1) -> tuple[np.ndarray, dict[str, Any]]:
    """Key a chroma sheet with the cfed170 keyer and edge despill; native alpha is copied.

    Chroma output equals the improved fork's remove_bg_magenta followed by
    despill_chroma_edges(radius) for radius 0-3 (MAP-03, MAP-12).
    """
    if background_mode == "native_alpha":
        return pixels.copy(), {"mode": "off", "applied": "off", "radius": 0, "margin": DESPILL_MARGIN,
                               "changed_px": 0}
    keyed = np.array(forge_matte.legacy_hard_key(pixels, threshold, edge_threshold))
    return forge_matte.despill(keyed, "edge", despill_radius, DESPILL_MARGIN)


# --------------------------------------------------------------------------- per-cell extraction

@dataclass
class CellResult:
    spec: CellSpec
    status: str                         # accepted | placeholder | empty
    image: np.ndarray | None
    record: dict[str, Any]
    visible_px: int                     # visible pixels of the cell before any cleanup
    content_size: tuple[int, int] | None = None
    fringe: tuple[int, int] | None = None


def _support_rows(top: int, bottom: int) -> int:
    return max(1, forge_core.round_half_up((bottom - top) * SUPPORT_BAND_FRACTION))


def _measured_anchor(frame: np.ndarray, selection: np.ndarray, mode: str) -> tuple[int, int]:
    """Ground anchor of the selected pixels; geometry ignores alpha <= 16; whole pixels, half-up."""
    geometry = selection & (frame[..., 3] > forge_core.ALPHA_GEOMETRY_THRESHOLD)
    if not geometry.any():
        geometry = selection
    _, top, _, bottom = forge_core.subject_bbox(geometry)
    x, y = forge_core.anchor_from_mask(geometry, mode, _support_rows(top, bottom))
    return forge_core.round_half_up(x), forge_core.round_half_up(y)


def suggest_footprint(image: np.ndarray, anchor: Sequence[float], shape: str,
                      depth_ratio: float) -> dict[str, Any]:
    """Ground footprint from the support band, in prop pixels relative to anchor_px.

    The support band is the bottom quarter of the art (alpha > 16). Width is the
    run of columns at least half covered in that band (the run nearest the anchor);
    depth is width * depth_ratio. The front edge sits on the art's ground line,
    so the offset is [run centre - anchor x, ground - depth / 2 - anchor y]. A
    starting point for review, not measured contact.
    """
    mask = image[..., 3] > forge_core.ALPHA_GEOMETRY_THRESHOLD
    if not mask.any():
        mask = image[..., 3] > 0
    _, top, _, bottom = forge_core.subject_bbox(mask)
    band = mask[bottom - _support_rows(top, bottom):bottom]
    covered = band.mean(axis=0) >= 0.5
    if not covered.any():
        covered = band.any(axis=0)
    edges = np.flatnonzero(np.diff(np.concatenate(([0], covered.astype(np.int8), [0]))))
    runs = list(zip(edges[::2].tolist(), edges[1::2].tolist()))
    x0, x1 = min(runs, key=lambda run: (max(run[0] - anchor[0], anchor[0] - run[1], 0), run[0]))
    width = x1 - x0
    depth = math.floor(width * depth_ratio * 100 + 0.5) / 100
    return {"shape": shape, "width": width, "depth": _number(depth),
            "offset": [_number((x0 + x1) / 2 - anchor[0]), _number(bottom - depth / 2 - anchor[1])],
            "rotate": 0, "basis": "prop_px", "suggested": True,
            "method": (f"columns at least half covered in the bottom {SUPPORT_BAND_FRACTION:.0%} of alpha > "
                       f"{forge_core.ALPHA_GEOMETRY_THRESHOLD}; depth = width x {depth_ratio:g}; "
                       "front edge on the ground line")}


def _exact_world_padding(anchor: tuple[float, float], size: tuple[int, int],
                         scale: Fraction) -> tuple[int, int, int, int]:
    """Extra (left, top, right, bottom) px so anchor * scale and size * scale are whole pixels."""
    if not all(float(value).is_integer() for value in anchor):
        raise ValueError("--world-scale needs whole-pixel anchors; authored anchor_px must be integers.")
    step = scale.denominator
    left, top = (-int(anchor[0])) % step, (-int(anchor[1])) % step
    return left, top, (-(size[0] + left)) % step, (-(size[1] + top)) % step


def extract_cell(cell: np.ndarray, spec: CellSpec, options: Options, *, chroma: bool) -> CellResult:
    """Extract one prop from a keyed cell (``cell`` is the RGBA array of ``spec.box``).

    The cfed170 steps are kept: edge touch is judged on every visible cell pixel,
    then the opt-in trim and edge clean run, then components above the min area
    are selected (``largest`` or ``all``). The image is the selection plus
    padding clamped to the cell, or the whole trimmed cell with --keep-canvas;
    --world-scale adds transparent padding so anchor and size scale to whole
    pixels. Pixels outside the selection are never copied.
    """
    height, width = cell.shape[:2]
    cell_x, cell_y = spec.box[:2]
    alpha = cell[..., 3]
    visible_px = int(np.count_nonzero(alpha))
    raw_touch = _touches_edge(forge_core.subject_bbox(alpha, 0), width, height, options.edge_touch_margin)
    border = options.trim_border
    trim = border if 0 < border and width > 2 * border and height > 2 * border else 0
    frame = cell[trim:height - trim, trim:width - trim].copy()
    clean_edges(frame, options.edge_clean_depth)
    frame_h, frame_w = frame.shape[:2]
    min_area = options.min_component_area or auto_min_area(width * height)
    labels, count = forge_core.label_components(frame[..., 3] > 0, options.connectivity)
    areas = np.bincount(labels.ravel(), minlength=count + 1)
    kept = [label for label in (np.argsort(-areas[1:], kind="stable") + 1).tolist() if areas[label] >= min_area]
    selected = kept[:1] if options.component_mode == "largest" else kept
    record: dict[str, Any] = {
        "index": spec.index, "label": spec.label.slug, "display_name": spec.label.display_name,
        "grid": list(spec.grid) if spec.grid else None, "cell_box": list(spec.box), "source_box": list(spec.box),
        "component_mode": options.component_mode, "component_count": len(kept), "min_component_area": min_area,
        "trim_applied": trim,
    }
    if not selected:
        record.update({"output_size": [0, 0], "dropped_components": count, "dropped_area": visible_px,
                       "edge_touch": raw_touch})
        if not options.keep_empty:
            record["status"] = "empty"
            return CellResult(spec, "empty", None, record, visible_px)
        image = np.zeros((frame_h, frame_w, 4) if options.keep_canvas else (1, 1, 4), np.uint8)
        size_h, size_w = image.shape[:2]
        record.update({
            "status": "placeholder", "output_size": [size_w, size_h],
            "source_rect": [cell_x + trim, cell_y + trim, cell_x + trim + size_w, cell_y + trim + size_h],
            "trim_offset": [trim, trim], "padding": [0, 0, 0, 0], "anchor_px": [size_w // 2, size_h],
            "anchor_source": "placeholder", "kept_area": 0, "edge_touch": False,
        })
        return CellResult(spec, "placeholder", image, record, visible_px)

    selection = np.isin(labels, selected)
    content = forge_core.subject_bbox(selection)
    if "anchor_px" in spec.meta:
        ax, ay = spec.meta["anchor_px"][0] - trim, spec.meta["anchor_px"][1] - trim
        if not (0 <= ax <= frame_w and 0 <= ay <= frame_h):
            raise ValueError(f"Prop {spec.label.slug!r} anchor_px lies outside the trimmed box.")
        anchor_source = "authored"
    else:
        ax, ay = _measured_anchor(frame, selection, options.anchor_mode)
        anchor_source = "measured"
    if options.keep_canvas:
        crop = [0, 0, frame_w, frame_h]
    else:
        pad = options.component_padding
        crop = [max(0, content[0] - pad), max(0, content[1] - pad),
                min(frame_w, content[2] + pad), min(frame_h, content[3] + pad)]
        crop = [min(crop[0], math.floor(ax)), min(crop[1], math.floor(ay)),  # the image holds its anchor
                max(crop[2], math.ceil(ax)), max(crop[3], math.ceil(ay))]
    if options.world_scale is not None:
        left, top, right, bottom = _exact_world_padding(
            (ax - crop[0], ay - crop[1]), (crop[2] - crop[0], crop[3] - crop[1]), options.world_scale)
        crop = [crop[0] - left, crop[1] - top, crop[2] + right, crop[3] + bottom]
    image = np.zeros((crop[3] - crop[1], crop[2] - crop[0], 4), np.uint8)
    x0, y0, x1, y1 = max(crop[0], 0), max(crop[1], 0), min(crop[2], frame_w), min(crop[3], frame_h)
    region = selection[y0:y1, x0:x1]
    image[y0 - crop[1]:y1 - crop[1], x0 - crop[0]:x1 - crop[0]][region] = frame[y0:y1, x0:x1][region]

    kept_px = int(np.count_nonzero(selection))
    anchor = (ax - crop[0], ay - crop[1])
    largest = options.component_mode == "largest"
    record.update({
        "status": "accepted", "output_size": [image.shape[1], image.shape[0]],
        "source_rect": [cell_x + trim + crop[0], cell_y + trim + crop[1],
                        cell_x + trim + crop[2], cell_y + trim + crop[3]],
        "trim_offset": [trim + crop[0], trim + crop[1]],
        "padding": [content[0] - crop[0], content[1] - crop[1], crop[2] - content[2], crop[3] - content[3]],
        "anchor_px": [_number(anchor[0]), _number(anchor[1])], "anchor_source": anchor_source,
        "crop_bbox": [value + trim for value in content],
        "padded_crop_bbox": [value + trim for value in crop],
        "selected_component_area": kept_px if largest else None,
        "selected_component_bbox": [value + trim for value in content] if largest else None,
        "kept_area": kept_px, "dropped_components": count - len(selected), "dropped_area": visible_px - kept_px,
        "edge_touch": raw_touch or _touches_edge(content, frame_w, frame_h, options.edge_touch_margin),
    })
    if options.world_scale is not None:
        scale = options.world_scale
        record["world_size"] = [int(image.shape[1] * scale), int(image.shape[0] * scale)]
        record["world_anchor"] = [int(Fraction(anchor[0]) * scale), int(Fraction(anchor[1]) * scale)]
    footprint = spec.meta.get("footprint")
    if footprint is None and options.footprint_shape:
        footprint = suggest_footprint(image, anchor, options.footprint_shape, options.footprint_depth_ratio)
    if footprint is not None:
        record["footprint"] = footprint
    for key in ("solid", "contact", "occlusion_class", "occupant_policy"):
        if key in spec.meta:
            record[key] = spec.meta[key]
    fringe = edge_fringe(image) if chroma else None
    if fringe is not None:
        record["edge_fringe_px"] = fringe[0]
    return CellResult(spec, "accepted", image, record, visible_px,
                      (content[2] - content[0], content[3] - content[1]), fringe)


# --------------------------------------------------------------------------- QA

def _check(check_id: str, status: str, value: Any, threshold: Any = None) -> dict[str, Any]:
    return {"id": check_id, "status": status, "value": value, "threshold": threshold}


def run_checks(results: Sequence[CellResult], *, chroma: bool, square_grid: bool, edge_touch_margin: int,
               reject_edge_touch: bool, max_dropped_fraction: float | None,
               auto_report: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """QA checks on the extracted props: (checks, warnings, failures). Any failure blocks publication."""
    accepted = [result for result in results if result.status == "accepted"]
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    failures: list[str] = []

    checks.append(_check("accepted", "pass" if accepted else "fail", len(accepted), ">= 1"))
    if not accepted:
        empty = [result.spec.label.slug for result in results]
        failures.append(f"No prop was accepted (empty cells: {empty}); nothing was published.")

    touching = [result.spec.label.slug for result in accepted if result.record["edge_touch"]]
    checks.append(_check("edge_touch", "pass" if not touching else ("fail" if reject_edge_touch else "warn"),
                         touching, edge_touch_margin))
    if touching and reject_edge_touch:
        failures.append(f"Accepted props touch a cell edge: {touching}")
    elif touching:
        warnings.append(f"props touch a cell edge (possible clipping): {touching}")

    shares = {result.spec.label.slug: result.record["dropped_area"] / result.visible_px
              for result in accepted if result.record["dropped_area"]}
    over = {label: round(share, 4) for label, share in shares.items()
            if max_dropped_fraction is not None and share > max_dropped_fraction}
    noted = {label: round(share, 4) for label, share in shares.items() if share > DROPPED_WARN_FRACTION}
    limit = DROPPED_WARN_FRACTION if max_dropped_fraction is None else max_dropped_fraction
    checks.append(_check("dropped_area", "fail" if over else ("warn" if noted else "pass"),
                         {label: round(share, 4) for label, share in shares.items()}, limit))
    if over:
        failures.append(f"Props dropped more than {max_dropped_fraction} of their visible pixels: {over}")
    elif noted:
        warnings.append(f"props dropped more than {DROPPED_WARN_FRACTION:.0%} of their cell's visible pixels "
                        f"(see dropped_components and dropped_area): {noted}")

    if square_grid:
        unfit = []
        for result in accepted:
            width, height = result.content_size
            if max(width / height, height / width) > STRATEGY_ASPECT_LIMIT:
                unfit.append(f"{result.spec.label.slug} {width}x{height}")
        checks.append(_check("strategy_aspect", "warn" if unfit else "pass", unfit, STRATEGY_ASPECT_LIMIT))
        if unfit:
            warnings.append(f"tall or wide props in a square grid (aspect > {STRATEGY_ASPECT_LIMIT}): {unfit}; "
                            "generate them one by one or in custom cells, or extract with --auto-boxes")
    else:
        checks.append(_check("strategy_aspect", "skipped", None, STRATEGY_ASPECT_LIMIT))

    if chroma:
        fringe = {result.spec.label.slug: result.fringe[0] / result.fringe[1]
                  for result in accepted if result.fringe and result.fringe[1]}
        fringed = {label: round(share, 4) for label, share in fringe.items() if share > EDGE_FRINGE_WARN_FRACTION}
        checks.append(_check("edge_fringe", "warn" if fringed else "pass", round(max(fringe.values(), default=0.0), 4),
                             EDGE_FRINGE_WARN_FRACTION))
        if fringed:
            warnings.append(f"magenta edge fringe above {EDGE_FRINGE_WARN_FRACTION:.0%} of edge pixels: {fringed}; "
                            "raise --despill-radius")
    else:
        checks.append(_check("edge_fringe", "skipped", None, EDGE_FRINGE_WARN_FRACTION))

    if auto_report is not None:
        unowned = auto_report["unowned_px"]
        checks.append(_check("auto_box_unowned", "warn" if unowned else "pass", unowned, 0))
        if unowned:
            warnings.append(f"{unowned} visible px belong to no auto box (small or faint parts); "
                            "review auto-boxes.json or pass --boxes-file")
    return checks, warnings, failures


# --------------------------------------------------------------------------- manifest reading

def read_manifest(path: str | os.PathLike) -> dict[str, Any]:
    """Read a prop-pack manifest, v2 or cfed170 v1, as a v2-shaped document.

    v2 is returned as written. v1 items gain cell_box, source_rect, padding,
    display_name and a bbox anchor (bottom centre of the art, ``anchor_source``
    ``derived-v1``); v1 keep-empty items (output_size [0, 0]) become placeholders.
    v1 never recorded trimming, so its boxes assume --trim-border 0. An item
    without a source_box (not written by the extractor) is passed through as is.
    compose_layered_preview reads v1 packs through this function (D10). JSON is
    read with forge_core.read_json (a BOM is tolerated, D28).
    """
    data = forge_core.read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("accepted"), list):
        raise ValueError(f"{path} is not a prop-pack manifest.")
    if "schema" in data:
        if data["schema"] != SCHEMA:
            raise ValueError(f"Unsupported prop-pack schema {data['schema']!r}.")
        return data
    items = []
    for item in data["accepted"]:
        cell = item.get("source_box") if isinstance(item, dict) else None
        if not (isinstance(cell, list) and len(cell) == 4):  # a hand-made v1 item: nothing to derive from
            items.append(item)
            continue
        padded, content = item.get("padded_crop_bbox"), item.get("crop_bbox")
        view = {**item, "display_name": item["label"], "cell_box": list(cell), "anchor_source": "derived-v1"}
        if item.get("output_size") == [0, 0] or not padded or not content:
            view.update({"status": "placeholder", "padding": [0, 0, 0, 0], "anchor_px": [0, 1],
                         "source_rect": [cell[0], cell[1], cell[0] + 1, cell[1] + 1]})
        else:
            view.update({
                "source_rect": [cell[0] + padded[0], cell[1] + padded[1], cell[0] + padded[2], cell[1] + padded[3]],
                "padding": [content[0] - padded[0], content[1] - padded[1], padded[2] - content[2],
                            padded[3] - content[3]],
                "anchor_px": [forge_core.round_half_up((content[0] + content[2]) / 2) - padded[0], content[3] - padded[1]],
            })
        items.append(view)
    return {**data, "schema_version": 1, "accepted": items}


# --------------------------------------------------------------------------- publication

def _relative_or_none(path: Path, base: Path) -> str | None:
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:  # another Windows drive
        return None


def _manifest_relative(path: Path, base: Path) -> str:
    """Manifest-relative POSIX path, or the bare file name when none exists (another drive):
    forge_core.manifest_path (D30)."""
    return forge_core.manifest_path(path, base)


def _file_ref(path: Path, base: Path) -> dict[str, Any]:
    return forge_core.file_ref(path, base)


def final_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """Absolute (output directory, manifest path) the run publishes."""
    output_dir = Path(args.output_dir)
    if output_dir.name in ("", ".", ".."):
        raise ValueError(f"Output directory needs a name: {output_dir}")
    final_dir = output_dir.parent.resolve() / output_dir.name
    manifest = Path(args.manifest) if args.manifest else final_dir / MANIFEST_NAME
    return final_dir, manifest.parent.resolve() / manifest.name


def _check_destinations(args: argparse.Namespace) -> tuple[Path, Path, str | None]:
    """Refuse unusable destinations before any work.

    Returns (output dir, manifest path, the manifest's POSIX path inside the
    output dir, or None for a sidecar manifest outside it).
    """
    final_dir, manifest = final_paths(args)
    for source in (Path(p) for p in (args.input, args.labels_file, args.boxes_file) if p):
        if manifest == source.resolve() or (manifest.exists() and manifest.samefile(source)):
            raise ValueError("Output path aliases an input file.")
    if os.path.lexists(final_dir):
        raise FileExistsError(f"Refusing to replace existing output directory {final_dir}; choose a new --output-dir.")
    if manifest == final_dir or final_dir.is_relative_to(manifest):
        raise ValueError("The manifest path cannot be the output directory or one of its ancestors.")
    if manifest.is_relative_to(final_dir):
        return final_dir, manifest, manifest.relative_to(final_dir).as_posix()
    if os.path.lexists(manifest):
        raise FileExistsError(f"Refusing to replace existing manifest {manifest}.")
    if _relative_or_none(final_dir, manifest.parent) is None:
        raise ValueError("--manifest must be on the same drive as --output-dir (image paths are manifest-relative).")
    return final_dir, manifest, None


def _check_manifest_clash(inside: str | None, labels: Sequence[str], auto: bool) -> None:
    """A manifest inside the output dir must not take an image path, a prop folder or auto-boxes.json."""
    if inside is None:
        return
    wanted = inside.casefold()
    for taken in [f"{label}/prop.png" for label in labels] + ([AUTO_BOXES_NAME] if auto else []):
        taken = taken.casefold()
        if wanted == taken or taken.startswith(wanted + "/") or wanted.startswith(taken.split("/")[0] + "/"):
            raise ValueError("Manifest and image output paths must be distinct.")


_RECORD_ORDER = ("index", "label", "display_name", "status", "image", "sha256", "output_size", "grid",
                 "cell_box", "source_box", "source_rect", "trim_offset", "padding", "anchor_px", "anchor_source")


def _ordered(record: dict[str, Any]) -> dict[str, Any]:
    ordered = {key: record[key] for key in _RECORD_ORDER if key in record}
    ordered.update((key, value) for key, value in record.items() if key not in ordered)
    return ordered


def _publish(results: Sequence[CellResult], manifest: dict[str, Any], destinations: tuple[Path, Path, str | None],
             auto_items: list[dict[str, Any]] | None, inputs: Sequence[Path]) -> None:
    """Write images, auto-boxes.json and the manifest into a stage, then publish the new directory.

    A manifest outside the output directory is a sidecar: it is published with
    publish_file_no_replace just before the directory and removed again if the
    directory publication fails.
    """
    final_dir, manifest_path, inside = destinations
    base = manifest_path.parent
    sidecar: tuple[Path, int, int] | None = None
    try:
        with forge_core.staged_output(final_dir) as stage:
            outputs = []
            for result in results:
                relative = f"{result.spec.label.slug}/prop.png"
                (stage / result.spec.label.slug).mkdir()
                forge_core.save_png(result.image, stage / relative)
                ref = _file_ref(stage / relative, stage)
                ref["path"] = _manifest_relative(final_dir / relative, base)
                result.record.update({"image": ref["path"], "sha256": ref["sha256"]})
                outputs.append(ref)
            if auto_items is not None:
                forge_core.write_json(stage / AUTO_BOXES_NAME, {
                    "schema": CROP_BOXES_SCHEMA,
                    "source": {"path": _manifest_relative(inputs[0], final_dir), "sha256": manifest["source_sha256"]},
                    "items": auto_items})
                ref = _file_ref(stage / AUTO_BOXES_NAME, stage)
                ref["path"] = _manifest_relative(final_dir / AUTO_BOXES_NAME, base)
                outputs.append(ref)
            manifest["accepted"] = [_ordered(result.record) for result in results]
            manifest["qa"]["inputs"] = [_file_ref(path, base) for path in inputs]
            manifest["qa"]["outputs"] = outputs
            if inside is not None:
                (stage / inside).parent.mkdir(parents=True, exist_ok=True)
                forge_core.write_json(stage / inside, manifest)
            else:
                staged = stage / f".{MANIFEST_NAME}.sidecar"
                forge_core.write_json(staged, manifest)
                forge_core.publish_file_no_replace(staged, manifest_path)
                identity = manifest_path.stat()
                sidecar = (manifest_path, identity.st_dev, identity.st_ino)
                staged.unlink()
    except BaseException:
        if sidecar is not None and sidecar[0].exists():
            current = sidecar[0].stat()
            if (current.st_dev, current.st_ino) == sidecar[1:]:  # never remove a file that replaced ours
                sidecar[0].unlink()
        raise


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    """argparse with its own convention for usage errors (D26): 'usage: ... error: ...', exit status 2."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("examples (one line each, run from the project root):\n"
                '  python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --rows 2 --cols 2'
                " --labels rock,bush,crate,stump --output-dir assets/props --reject-edge-touch\n"
                '  python "<skill-dir>/scripts/extract_prop_pack.py" --input raw/props.png --auto-boxes'
                " --labels tree,lantern,rock --output-dir assets/props --world-scale 3/8 --suggest-footprint ellipse"))
    layout = parser.add_argument_group("layout (one of: --rows/--cols, --boxes-file, --auto-boxes)")
    layout.add_argument("--input", required=True, type=Path, help="Prop sheet (PNG; palette, grey and 16-bit read).")
    layout.add_argument("--rows", type=int, help="Grid rows.")
    layout.add_argument("--cols", type=int, help="Grid columns.")
    layout.add_argument("--grid-rounding", choices=("exact", "nearest"), default="exact",
                        help="exact: sizes must divide (default); nearest: rounded cell edges, cells differ by <= 1 px.")
    layout.add_argument("--labels", help="Comma-separated labels in row-major order; empty, skip or - skips a cell.")
    layout.add_argument("--labels-file", type=Path, help="UTF-8 labels, one per line; # starts a comment.")
    layout.add_argument("--boxes-file", type=Path,
                        help='Measured boxes: {"items": [{"id": "tree", "box": [x0, y0, x1, y1]}]} '
                             '(legacy {"props": [{"label", "source_box"}]} accepted).')
    layout.add_argument("--auto-boxes", action="store_true",
                        help="Find props without a grid, in reading order; writes auto-boxes.json for review.")
    layout.add_argument("--auto-box-gap", type=int,
                        help="Auto boxes: merge parts closer than this many px (default: short side / 52).")

    output = parser.add_argument_group("output")
    output.add_argument("--output-dir", required=True, type=Path, help="New directory; an existing one is refused.")
    output.add_argument("--manifest", type=Path,
                        help="Manifest path (default <output-dir>/prop-pack.json); outside the directory it is a "
                             "new sidecar file.")
    output.add_argument("--art-source", choices=ART_SOURCES, help="Record where the sheet came from.")

    keying = parser.add_argument_group("background")
    keying.add_argument("--background-mode", choices=("auto", "chroma_key", "native_alpha"), default="auto",
                        help="auto: real transparency means native_alpha, otherwise the magenta chroma key.")
    keying.add_argument("--threshold", type=int, default=100, help="Clear #FF00FF within this RGB distance.")
    keying.add_argument("--edge-threshold", type=int, default=150,
                        help="Clear border-connected #FF00FF within this RGB distance.")
    keying.add_argument("--despill-radius", type=int, choices=(0, 1, 2, 3), default=1,
                        help="Chroma only: remove magenta spill within N px of transparency (default 1; 0 is the "
                             "legacy output). May neutralise real purple edges.")
    keying.add_argument("--alpha-hygiene", choices=forge_core.HYGIENE_MODES,
                        help="Alpha noise cleanup: floor zeroes alpha <= --alpha-floor; detached drops faint islands "
                             "away from solid art (default none; both with --auto-boxes).")
    keying.add_argument("--alpha-floor", type=int,
                        help="Floor for --alpha-hygiene floor or both (default 4); alone it means --alpha-hygiene floor.")

    cells = parser.add_argument_group("components and crops")
    cells.add_argument("--component-mode", choices=("all", "largest"),
                       help="largest keeps one component (default); all keeps multipart props (default with "
                            "--auto-boxes).")
    cells.add_argument("--connectivity", type=int, choices=(4, 8), default=8,
                       help="Pixel connectivity (default 8 keeps 1 px diagonal strokes; 4 is the legacy rule).")
    cells.add_argument("--min-component-area", type=parse_min_area, default=None,
                       help="Minimum component px, or auto: 100 for a 418 px cell, scaled with the cell area (default).")
    cells.add_argument("--component-padding", type=int, default=8,
                       help="Transparent px around the art, clamped to the cell (default 8).")
    cells.add_argument("--keep-canvas", action="store_true",
                       help="Keep the whole (trimmed) cell or box instead of cropping to the art.")
    cells.add_argument("--trim-border", type=int, default=0, help="Opt-in: cut N px from every cell side first.")
    cells.add_argument("--edge-clean-depth", type=int, default=0,
                       help="Opt-in: clear dark or magenta pixels in the outer N px rings.")
    cells.add_argument("--keep-empty", action="store_true",
                       help="Write a transparent placeholder (status placeholder) for each empty cell.")

    anchors = parser.add_argument_group("anchors and footprints")
    anchors.add_argument("--anchor-mode", choices=forge_core.ANCHOR_MODES, default="stance",
                         help="Ground anchor rule (default stance: middle of the bottom quarter of the art, on "
                              "its ground line; feet: median column of that band).")
    anchors.add_argument("--world-scale", type=parse_world_scale,
                         help="Pad so anchor and size scale to whole pixels at this display scale (0.5, 3/8, ...).")
    anchors.add_argument("--suggest-footprint", choices=("ellipse", "rect"),
                         help="Add a suggested ground footprint measured from the support band.")
    anchors.add_argument("--footprint-depth-ratio", type=float, default=0.5,
                         help="Suggested footprint depth as a share of its width (default 0.5).")

    qc = parser.add_argument_group("quality control")
    qc.add_argument("--edge-touch-margin", type=int, default=0, help="Art within N px of a cell side touches it.")
    qc.add_argument("--reject-edge-touch", action="store_true",
                    help="Fail, publishing nothing, when art touches a cell side.")
    qc.add_argument("--max-dropped-fraction", type=float,
                    help="Fail when a prop drops more than this share of its cell's visible pixels.")
    return parser


def _specs_before_keying(args: argparse.Namespace, options: Options, size: tuple[int, int]) -> list[CellSpec]:
    if options.layout == "explicit_boxes":
        return read_crop_boxes(args.boxes_file, size)
    boxes = grid_boxes(size[0], size[1], args.rows, args.cols, args.grid_rounding)
    labels = parse_labels(args, len(boxes))
    return [CellSpec(index, labels[index], tuple(box), divmod(index, args.cols)) for index, box in enumerate(boxes)]


def _auto_specs(args: argparse.Namespace, options: Options,
                cleaned: np.ndarray) -> tuple[list[CellSpec], np.ndarray, dict[str, Any]]:
    height, width = cleaned.shape[:2]
    short_side = min(width, height)
    gap = args.auto_box_gap if args.auto_box_gap is not None else max(1, forge_core.round_half_up(short_side / AUTO_GAP_DIVISOR))
    objects, owner, report = find_auto_boxes(
        cleaned, gap=gap, attach=max(1, forge_core.round_half_up(short_side / AUTO_ATTACH_DIVISOR)),
        margin=max(options.component_padding, options.edge_touch_margin + 1),
        min_solid_area=max(1, forge_core.round_half_up(width * height / AUTO_AREA_PER_SOLID_PX)))
    if not objects:
        raise ValueError("auto-boxes found no prop (no part with alpha >= 128 is large enough); nothing was published.")
    tokens = read_label_tokens(args)
    if tokens and len(tokens) != len(objects):
        raise ValueError(f"auto-boxes found {len(objects)} props but {len(tokens)} labels were given; boxes in "
                         f"reading order: {[list(item['box']) for item in objects]}. Fix the labels or pass "
                         "reviewed --boxes-file.")
    labels = resolve_labels(tokens, len(objects))
    specs = [CellSpec(index, labels[index], item["box"], owner=item["owner"]) for index, item in enumerate(objects)]
    return specs, owner, report


def extract(args: argparse.Namespace) -> dict[str, Any]:
    """Extract, check and publish a prop pack; return the manifest. Raises ValueError or OSError on failure."""
    options = _resolve_options(args)
    destinations = _check_destinations(args)
    image, info = forge_core.load_rgba(args.input)
    pixels = np.array(image)
    height, width = pixels.shape[:2]
    mode = args.background_mode
    if mode == "auto":
        mode = "native_alpha" if pixels[..., 3].min() < 255 else "chroma_key"
    if mode == "native_alpha" and pixels[..., 3].min() == 255:
        raise ValueError("native_alpha requires real transparency, not an RGB checkerboard.")
    chroma = mode == "chroma_key"
    despill_radius = args.despill_radius if chroma else 0
    auto = options.layout == "auto_boxes"
    specs = [] if auto else _specs_before_keying(args, options, (width, height))

    keyed, despill_report = key_sheet(pixels, mode, args.threshold, args.edge_threshold, despill_radius)
    cleaned_image, hygiene_report = forge_core.alpha_hygiene(keyed, options.hygiene_mode, options.hygiene_floor)
    cleaned = np.array(cleaned_image)
    owner, auto_report = None, None
    if auto:
        specs, owner, auto_report = _auto_specs(args, options, cleaned)
    _check_manifest_clash(destinations[2], [spec.label.slug for spec in specs if spec.label.slug], auto)

    results: list[CellResult] = []
    rejected: list[dict[str, Any]] = []
    for spec in specs:
        if not spec.label.slug:
            rejected.append({"index": spec.index, "label": "", "display_name": spec.label.display_name,
                             "grid": list(spec.grid) if spec.grid else None, "cell_box": list(spec.box),
                             "source_box": list(spec.box), "status": "skipped-label"})
            continue
        x0, y0, x1, y1 = spec.box
        cell = cleaned[y0:y1, x0:x1]
        if spec.owner:
            cell = np.where((owner[y0:y1, x0:x1] == spec.owner)[..., None], cell, 0).astype(np.uint8)
        result = extract_cell(cell, spec, options, chroma=chroma)
        results.append(result)
        if result.status == "empty":
            rejected.append(_ordered(result.record))

    square_grid = options.layout == "grid" and all(
        abs((spec.box[2] - spec.box[0]) - (spec.box[3] - spec.box[1])) <= 1 for spec in specs)
    checks, warnings, failures = run_checks(
        results, chroma=chroma, square_grid=square_grid, edge_touch_margin=options.edge_touch_margin,
        reject_edge_touch=args.reject_edge_touch, max_dropped_fraction=args.max_dropped_fraction,
        auto_report=auto_report)
    if failures:
        raise ValueError(" ".join(failures))

    published = [result for result in results if result.status != "empty"]
    inputs = [Path(p) for p in (args.input, args.boxes_file, args.labels_file) if p]
    base = destinations[1].parent
    keying = (f"legacy #FF00FF key ({args.threshold}/{args.edge_threshold}) and edge despill radius {despill_radius}"
              if chroma else "native alpha, not keyed")
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "input": _manifest_relative(Path(args.input), base),
        "source_size": [width, height],
        "source_sha256": info["sha256"],
        "source_mode": info["source_mode"],
        "layout_mode": options.layout,
        "boxes_file": _manifest_relative(Path(args.boxes_file), base) if args.boxes_file else None,
        "labels_file": _manifest_relative(Path(args.labels_file), base) if args.labels_file else None,
        "background_mode": mode,
        **({"art_source": args.art_source} if args.art_source else {}),
        "rows": args.rows if options.layout == "grid" else None,
        "cols": args.cols if options.layout == "grid" else None,
        "threshold": args.threshold,
        "edge_threshold": args.edge_threshold,
        "despill_radius": despill_radius,
        "despill": despill_report,
        "hygiene": hygiene_report,
        "alpha_floor": hygiene_report["floor"] if options.hygiene_mode in ("floor", "both") else 0,
        "alpha_floor_pixels_removed": hygiene_report["floor_px"],
        "trim_border": options.trim_border,
        "edge_clean_depth": options.edge_clean_depth,
        "component_mode": options.component_mode,
        "component_padding": options.component_padding,
        "min_component_area": options.min_component_area or "auto",
        "edge_touch_margin": options.edge_touch_margin,
        "geometry": {
            "connectivity": options.connectivity,
            "alpha_geometry_threshold": forge_core.ALPHA_GEOMETRY_THRESHOLD,
            "anchor_mode": options.anchor_mode,
            "support_band_fraction": SUPPORT_BAND_FRACTION,
            "anchor_rounding": "whole pixels, half-up",
            "grid_rounding": args.grid_rounding if options.layout == "grid" else None,
            "keep_canvas": options.keep_canvas,
            "world_scale": str(options.world_scale) if options.world_scale is not None else None,
        },
        **({"auto_boxes": auto_report} if auto_report is not None else {}),
        "accepted": [],
        "rejected": rejected,
        "edge_touch_props": [result.spec.label.slug for result in published
                             if result.status == "accepted" and result.record["edge_touch"]],
        "warnings": warnings,
        "qa": {
            "status": "warn" if any(check["status"] == "warn" for check in checks) else "pass",
            "method": (f"{keying}; alpha hygiene {options.hygiene_mode}; {options.connectivity}-connected "
                       f"components ({options.component_mode}); checks measured on the published pixels"),
            "notProven": list(NOT_PROVEN),
            "checks": checks,
            "inputs": [],
            "outputs": [],
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        },
    }
    auto_items = None
    if auto:
        auto_items = [{"id": spec.label.slug, "box": list(spec.box),
                       **({"display_name": spec.label.display_name} if spec.label.display_name != spec.label.slug
                          else {})} for spec in specs if spec.label.slug]
    _publish(published, manifest, destinations, auto_items, inputs)
    return manifest


def _cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = extract(args)
    output_dir, manifest_path = final_paths(args)
    statuses = [item["status"] for item in manifest["accepted"]]
    print(json.dumps({
        "status": "ok", "output_dir": str(output_dir), "manifest": str(manifest_path),
        "accepted": statuses.count("accepted"), "placeholders": statuses.count("placeholder"),
        "rejected": len(manifest["rejected"]), "qa": manifest["qa"]["status"],
        "warnings": [forge_core.ascii_text(text) for text in manifest["warnings"]],
    }, ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27): usage errors exit 2, a failed run prints
    'error: <message>' and exits 1, and no traceback reaches the user."""
    return forge_core.run_cli(_cli, argv)


if __name__ == "__main__":
    raise SystemExit(main())
