#!/usr/bin/env python3
"""Compose a flattened layered-map preview from a base image and prop placements.

Placements are a JSON object with props, objects, actors and foreground lists
(a bare list counts as props). The compositing order is explicit: the base,
then placements on layer "background", then the world band (props, objects,
actors and every other layer), then the foreground band. Inside a band the
order is (sortY, x, id): sortY is the placement's own sortY or else its ground
line, the y where its anchor meets the ground (y for contact anchors, y + h/2
for "center", y + h for "top-left"). --sort raw-y restores the old rule.

A placement without anchor or anchorPx takes anchor_px from the prop-pack
manifest that lists its image (--prop-pack, or a prop-pack.json beside the
image or one folder up; a v1 pack through extract_prop_pack.read_manifest, so
its props stand on the art's bottom edge, D10), else the bottom centre.
--anchor px restores the old rule. Positions round half up. flip_x mirrors a
placement's art around its anchor x, and its footprint with it (D6).

Optional outputs: --report (draw order, anchors, sortY, anchor_world_error),
--debug-overlay (walk regions, spawns, exits, approach points, collision,
footprints and masks from --bundle/--stage/--mask), --audit-out (feet inside
walkable areas, bounds and overlaps) and --plate-pan (a single-plate pan
contact sheet). With --bundle, actor feet are judged by the vendored forge_nav
on the bundle's blocking set (D2, D4), exactly as map_nav.py judges them;
without one, by the same closed-set rules on the canvas, the stage ground
polygons and the solid placement footprints. No output may exist already or
alias an input, and nothing is written unless the whole run, including the
--strict audit, succeeds.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_nav  # noqa: E402  (the shared collision rule book, D4)


REPORT_SCHEMA = "generate2dmap.compose_report.v2"
AUDIT_SCHEMA = "generate2dmap.compose_audit.v1"
TOOL = {"name": "compose_layered_preview", "version": forge_core.FORGE_PACKAGE_VERSION}  # D29

GROUPS = ("props", "objects", "actors", "foreground")
KINDS = {"props": "prop", "objects": "object", "actors": "actor", "foreground": "foreground"}
BOX_ANCHORS = ("top-left", "center", "bottom-left", "center-bottom")
ANCHOR_NAMES = ("manifest", "px") + BOX_ANCHORS
RESAMPLERS = ("nearest", "lanczos")
SORT_MODES = ("ground-line", "raw-y")
ANCHOR_POLICIES = ("manifest", "px")
WORLD_LAYERS = ("props", "objects", "actors", "world")
BANDS = ("background", "world", "foreground")
OCCLUSION_CLASSES = ("low", "tall", "foreground")
OCCUPANT_POLICIES = ("y_sort", "rear_shift_and_fade", "static_front", "static_back")
FOOTPRINT_SHAPES = ("ellipse", "rect", "none")
FOOTPRINT_BASES = ("prop_px", "world_px", "image_px")  # D7: prop_px is the default, image_px its legacy alias
PORTAL_ACTIVATIONS = ("crossing", "intent")
MATERIAL_CLASSES = ("solid", "one_way", "liquid", "hazard", "decor")
PROP_PACK_NAME = "prop-pack.json"
PROP_PACK_SCHEMAS = ("generate2dmap.prop_pack.v2",)
PLACEMENT_SCHEMAS = ("generate2dmap.placements.v2",)
BUNDLE_SCHEMAS = ("generate2dmap.map_bundle.v1", "generate2dmap.map_bundle.v2")
VISIBLE_ALPHA = forge_core.ALPHA_GEOMETRY_THRESHOLD
PAN_GAP_PX = 4

COLORS = {
    "walk": (46, 204, 113), "hole": (241, 196, 15), "solid": (231, 76, 60), "footprint": (255, 0, 255),
    "portal": (230, 126, 34), "spawn": (52, 152, 219), "approach": (241, 196, 15), "anchor": (155, 89, 182),
    "interaction": (26, 188, 156), "protected": (149, 165, 166), "effect": (0, 206, 209),
    "foot": (0, 255, 255), "invalid": (255, 40, 40),
    "slot_hero": (52, 152, 219), "slot_enemy": (231, 76, 60), "slot_boss": (142, 68, 173),
}
MASK_COLORS = ((0, 191, 255), (255, 105, 180), (255, 215, 0), (127, 255, 0), (255, 127, 80))
MATERIAL_COLORS = {"solid": (231, 76, 60), "one_way": (241, 196, 15), "liquid": (52, 152, 219),
                   "hazard": (230, 126, 34)}

AUDIT_NOT_PROVEN = [
    "Feet are tested at the placement positions: actors with the forge_nav rules (centre and 8 footprint samples), "
    "on the bundle's blocking set with --bundle (D2), else on the canvas, stage ground polygons and solid placement "
    "footprints; props as points. Reachability between points is map_nav's job.",
    "Overlaps are checked only between footprints from prop-pack manifests or placements; "
    "placements without a footprint are counted, not checked.",
    "Occlusion is measured on the flattened preview at alpha > 16; runtime sorting, animation, "
    "camera, lighting and scale are not simulated.",
    "The preview is not proof that the art reads well at gameplay scale; look at it.",
]


# --------------------------------------------------------------------------- small helpers

def read_json(path: Path) -> Any:
    """JSON input as UTF-8 with an optional BOM (D28, forge_core.read_json)."""
    return forge_core.read_json(path)


def path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def resolve_path(value: str, roots: list[Path]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    for root in roots:
        candidate = root / path
        if candidate.exists():
            return candidate
    return roots[0] / path


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    return float(value)


def _point(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must be [x, y].")
    return _number(value[0], name), _number(value[1], name)


def _size_arg(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX,]\s*(\d+)\s*", text)
    if not match or min(int(match.group(1)), int(match.group(2))) < 1:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT in whole pixels, for example 960x540")
    return int(match.group(1)), int(match.group(2))


def _clean(value: Any) -> Any:
    """Round floats for stable, readable JSON (6 decimals), recursively."""
    if isinstance(value, float):
        return round(value, 6) + 0.0
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


# --------------------------------------------------------------------------- placements

def collect_placements(data: Any) -> list[tuple[str, dict[str, Any]]]:
    """(group, placement) pairs in file order. Entries of the foreground list are put on layer foreground."""
    if isinstance(data, list):
        pairs = [("props", item) for item in data]
    elif isinstance(data, dict):
        pairs = []
        found = False
        for key in GROUPS:
            value = data.get(key)
            if isinstance(value, list):
                found = True
                pairs.extend((key, item) for item in value)
            elif key in data:
                raise ValueError(f"Placement field {key!r} must be a list.")
        if not found:
            raise ValueError("Placement JSON must be a list or an object with a 'props' list.")
    else:
        raise ValueError("Placement JSON must be a list or an object with a 'props' list.")
    for group, item in pairs:
        if not isinstance(item, dict):
            raise ValueError(f"Every {group} placement must be a JSON object.")
    return [(group, {**item, "layer": "foreground"} if group == "foreground" else item) for group, item in pairs]


def load_props(data: Any) -> list[dict[str, Any]]:
    """Placements in file order (props, objects, actors, then foreground), as the cfed170 reader returned them."""
    return [item for _, item in collect_placements(data)]


def placement_file_warnings(data: Any) -> list[str]:
    """Unknown schema ids and unread top-level lists, which would otherwise drop placements silently."""
    warnings = []
    if isinstance(data, dict):
        schema = data.get("schema")
        if schema is not None and schema not in PLACEMENT_SCHEMAS:
            warnings.append(f"placements schema {schema!r} is not one of {', '.join(PLACEMENT_SCHEMAS)}; read as v2.")
        for key, value in data.items():
            if key not in GROUPS and isinstance(value, list):
                warnings.append(f"placements key {key!r} is not read; placements go in props, objects, actors "
                                f"or foreground.")
    return warnings


def _box_anchor_offset(name: str, width: float, height: float) -> tuple[float, float]:
    return {"top-left": (0.0, 0.0), "center": (width / 2, height / 2), "bottom-left": (0.0, float(height)),
            "center-bottom": (width / 2, float(height))}[name]


def _anchor_px(value: Any, source_size: tuple[float, float], name: str = "anchorPx") -> tuple[float, float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value)):
        raise ValueError(f"{name} must be two finite source-pixel coordinates.")
    if not (0 <= value[0] <= source_size[0] and 0 <= value[1] <= source_size[1]):
        raise ValueError(f"{name} must be inside the source canvas.")
    return float(value[0]), float(value[1])


def placement_origin(prop: dict[str, Any], width: float, height: float,
                     source_size: tuple[int, int] | None = None) -> tuple[float, float]:
    """Unrounded top-left of a placement drawn ``width`` x ``height`` (the cfed170 anchor rules)."""
    x = _number(prop.get("x", 0), "x")
    y = _number(prop.get("y", 0), "y")
    if "anchorPx" in prop:
        sw, sh = source_size or (width, height)
        point = _anchor_px(prop["anchorPx"], (sw, sh))
        return x - point[0] * width / sw, y - point[1] * height / sh
    anchor = prop.get("anchor", "center-bottom")
    if anchor not in BOX_ANCHORS:
        raise ValueError(f"Unknown anchor: {anchor}")
    dx, dy = _box_anchor_offset(anchor, width, height)
    return x - dx, y - dy


def placement_xy(prop: dict[str, Any], width: int, height: int,
                 source_size: tuple[int, int] | None = None) -> tuple[int, int]:
    """Integer top-left of a placement, rounded half up (MAP-21)."""
    left, top = placement_origin(prop, width, height, source_size)
    return forge_core.round_half_up(left), forge_core.round_half_up(top)


def effective_sort_y(prop: dict[str, Any], ground_y: float | None = None) -> float:
    """The placement's sortY, else ``ground_y`` (the ground line) when given, else its raw y (cfed170)."""
    value = prop.get("sortY", prop.get("y", 0) if ground_y is None else ground_y)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("sortY must be finite.")
    return float(value)


# --------------------------------------------------------------------------- prop-pack manifests

@dataclass
class PackEntry:
    manifest: Path
    label: str
    anchor_px: tuple[float, float] | None
    footprint: dict[str, Any] | None
    sha256: str | None
    solid: bool | None
    occlusion_class: str | None
    occupant_policy: str | None


class PropPackIndex:
    """prop-pack.json manifests (v1 or v2) by image, for manifest anchors and footprints (MAP-02).

    Explicit manifests load at once; with ``discover`` a prop-pack.json beside a placement's image
    or one folder up (extract_prop_pack writes <pack>/<label>/prop.png) is loaded on first use.
    """

    def __init__(self, manifests: Iterable[Path] = (), *, discover: bool = True) -> None:
        self.discover = discover
        self.manifests: dict[str, Path] = {}
        self.entries: dict[str, PackEntry] = {}
        self.warnings: list[str] = []
        for path in manifests:
            self.load(Path(path))

    def load(self, path: Path) -> None:
        key = path_key(path)
        if key in self.manifests:
            return
        self.manifests[key] = path.resolve()
        try:
            data = read_json(path)
        except (OSError, ValueError) as error:
            raise ValueError(f"Cannot read prop-pack manifest {path}: {error}") from error
        if not isinstance(data, dict) or not isinstance(data.get("accepted"), list):
            raise ValueError(f"{path} is not a prop-pack manifest (it has no accepted list).")
        schema = data.get("schema")
        if schema is not None and schema not in PROP_PACK_SCHEMAS:
            self.warnings.append(f"{path.name}: prop-pack schema {schema!r} is unknown; read like v2.")
        if schema is None:  # a v1 (cfed170) pack: B10's reader derives anchors on the art's bottom edge (D10)
            data = _v1_pack_view(path)
            if any("anchor_px" in item for item in data["accepted"] if isinstance(item, dict)):
                self.warnings.append(f"{path.name}: a v1 prop pack; anchors are the art's bottom centre "
                                     "(extract_prop_pack.read_manifest). Re-extract it for measured anchors and "
                                     "footprints.")
        for item in data["accepted"]:
            if not isinstance(item, dict) or not isinstance(item.get("image"), str) or not item["image"]:
                continue
            label = str(item.get("label", item["image"]))
            entry = PackEntry(
                manifest=path.resolve(), label=label,
                anchor_px=_point(item["anchor_px"], f"{path.name} {label} anchor_px") if "anchor_px" in item else None,
                footprint=item.get("footprint") if isinstance(item.get("footprint"), dict) else None,
                sha256=item.get("sha256") if isinstance(item.get("sha256"), str) else None,
                solid=item.get("solid") if isinstance(item.get("solid"), bool) else None,
                occlusion_class=item.get("occlusion_class"), occupant_policy=item.get("occupant_policy"))
            for field_name, allowed in (("occlusion_class", OCCLUSION_CLASSES), ("occupant_policy", OCCUPANT_POLICIES)):
                value = getattr(entry, field_name)
                if value is not None and value not in allowed:
                    self.warnings.append(f"{path.name} {label}: {field_name} {value!r} is not one of "
                                         f"{', '.join(allowed)}.")
            image_key = path_key(path.parent / item["image"])
            previous = self.entries.get(image_key)
            if previous is not None and previous.anchor_px != entry.anchor_px:
                raise ValueError(f"{item['image']} is listed in {previous.manifest.name} and {path.name} with "
                                 f"different anchors; pass the right one with --prop-pack.")
            self.entries.setdefault(image_key, entry)

    def lookup(self, image_path: Path) -> PackEntry | None:
        key = path_key(image_path)
        if key not in self.entries and self.discover:
            folder = Path(image_path).resolve().parent
            for candidate in (folder / PROP_PACK_NAME, folder.parent / PROP_PACK_NAME):
                if candidate.is_file():
                    self.load(candidate)
        return self.entries.get(key)


def _v1_pack_view(path: Path) -> dict[str, Any]:
    """A v1 prop-pack manifest as the v2-shaped view of the sibling extract_prop_pack.read_manifest (D10)."""
    import extract_prop_pack  # the B10 extractor beside this script (same skill)

    return extract_prop_pack.read_manifest(path)


# --------------------------------------------------------------------------- one placement

@dataclass
class Placed:
    """A placement resolved to canvas pixels."""
    group: str
    id: str
    image_path: Path
    image_sha256: str
    source_size: tuple[int, int]
    sprite: Image.Image
    left: int
    top: int
    width: int
    height: int
    world: tuple[float, float]
    anchor_px: tuple[float, float]
    anchor_source: str
    anchor_canvas: tuple[float, float]
    sort_y: float
    sort_source: str
    layer: str
    band: str
    resampler: str
    footprint: dict[str, Any] | None
    canvas_scale: float = 1.0
    warnings: list[str] = field(default_factory=list)
    draw_index: int = -1
    flip_x: bool = False

    @property
    def kind(self) -> str:
        return KINDS.get(self.group, "prop")

    @property
    def anchor_world_error(self) -> tuple[float, float]:
        """Where the anchor actually landed minus where it was asked to land, in world pixels."""
        scale = self.canvas_scale
        return self.anchor_canvas[0] / scale - self.world[0], self.anchor_canvas[1] / scale - self.world[1]


class ImageCache:
    """Loads each image once with forge_core.load_rgba (palette, grey and 16-bit PNGs become 8-bit RGBA)."""

    def __init__(self) -> None:
        self.images: dict[str, tuple[Image.Image, dict[str, Any]]] = {}

    def load(self, path: Path) -> tuple[Image.Image, dict[str, Any]]:
        key = path_key(path)
        if key not in self.images:
            self.images[key] = forge_core.load_rgba(path)
        return self.images[key]


def _resample_filter(name: str) -> Image.Resampling:
    return Image.Resampling.NEAREST if name == "nearest" else Image.Resampling.LANCZOS


def _footprint(spec: Any, origin: str, ident: str, ref_anchor: tuple[float, float], left: int, top: int,
               scale_xy: tuple[float, float], world_scale: float, warnings: list[str], *,
               flip_width: float | None = None) -> dict[str, Any] | None:
    """A prop footprint (map.schema footprint) placed on the canvas; None for shape none.

    basis prop_px (the default; image_px is its legacy alias, D7) is in prop-image pixels and
    follows the drawn sprite; world_px is in world pixels and only follows --scale. With
    ``flip_width`` (the source width of a flip_x placement) the footprint is mirrored with the
    art around the anchor x: the x offset and the rotation change sign (D6)."""
    if not isinstance(spec, dict):
        raise ValueError(f"{ident}: footprint must be an object.")
    shape = spec.get("shape")
    if shape not in FOOTPRINT_SHAPES:
        warnings.append(f"{ident}: footprint shape {shape!r} is not one of {', '.join(FOOTPRINT_SHAPES)}; ignored.")
        return None
    if shape == "none":
        return None
    width = _number(spec.get("width"), f"{ident} footprint width")
    depth = _number(spec.get("depth"), f"{ident} footprint depth")
    offset = _point(spec.get("offset", [0, 0]), f"{ident} footprint offset")
    rotate = _number(spec.get("rotate", 0), f"{ident} footprint rotate")
    basis = spec.get("basis", "prop_px")
    if basis not in FOOTPRINT_BASES:
        warnings.append(f"{ident}: footprint basis {basis!r} is not one of {', '.join(FOOTPRINT_BASES)}; "
                        f"read as prop_px.")
        basis = "prop_px"
    if width < 0 or depth < 0:
        raise ValueError(f"{ident}: footprint width and depth must not be negative.")
    anchor_x = ref_anchor[0]
    if flip_width is not None:  # D6: mirrored with the art around the anchor x
        anchor_x, offset, rotate = flip_width - anchor_x, (-offset[0], offset[1]), -rotate
    sx, sy = scale_xy if basis != "world_px" else (world_scale, world_scale)
    if basis != "world_px":
        cx, cy = left + (anchor_x + offset[0]) * sx, top + (ref_anchor[1] + offset[1]) * sy
    else:
        cx = left + anchor_x * scale_xy[0] + offset[0] * sx
        cy = top + ref_anchor[1] * scale_xy[1] + offset[1] * sy
    return {"shape": shape, "cx": cx, "cy": cy, "rx": width / 2 * sx, "ry": depth / 2 * sy, "rotate": rotate,
            "source": origin}


def prepare_placement(prop: dict[str, Any], roots: list[Path], *, group: str = "props",
                      resampler: str = "lanczos", sort: str = "ground-line", anchor_policy: str = "manifest",
                      scale: float = 1.0, packs: PropPackIndex | None = None,
                      images: ImageCache | None = None) -> Placed:
    """Resolve one placement: image, size, anchor (manifest, anchorPx or box), canvas position, sortY, footprint."""
    if sort not in SORT_MODES:
        raise ValueError(f"Unknown sort mode: {sort}")
    if anchor_policy not in ANCHOR_POLICIES:
        raise ValueError(f"Unknown anchor policy: {anchor_policy}")
    warnings: list[str] = []
    image_key = prop.get("image") or prop.get("path")
    if not image_key:
        raise ValueError(f"Prop is missing image/path: {prop}")
    image_path = resolve_path(str(image_key), roots)
    if not image_path.exists():
        raise FileNotFoundError(f"Prop image not found: {image_path}")
    source, info = (images or ImageCache()).load(image_path)
    sw, sh = source.size
    raw_id = prop.get("id")
    ident = str(raw_id) if raw_id not in (None, "") else image_path.stem

    if "scale" in prop:
        if any(key in prop for key in ("w", "width", "h", "height")):
            raise ValueError(f"{ident}: give either scale or w/h, not both.")
        instance = _number(prop["scale"], f"{ident} scale")
        if instance <= 0:
            raise ValueError(f"{ident}: scale must be positive.")
        width_f, height_f = sw * instance, sh * instance
    else:
        width_f = _number(prop.get("w", prop.get("width", sw)), f"{ident} w")
        height_f = _number(prop.get("h", prop.get("height", sh)), f"{ident} h")
    width, height = forge_core.round_half_up(width_f * scale), forge_core.round_half_up(height_f * scale)
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid prop size for {image_path}: {width}x{height}")

    chosen = str(prop.get("resampler", resampler))
    if chosen not in RESAMPLERS:
        raise ValueError(f"Unknown resampler: {chosen}")
    flip = prop.get("flip_x", False)
    if not isinstance(flip, bool):
        raise ValueError(f"{ident}: flip_x must be true or false.")
    art = source.transpose(Image.Transpose.FLIP_LEFT_RIGHT) if flip else source  # D6: mirrored art
    sprite = art if (width, height) == art.size else art.resize((width, height), _resample_filter(chosen))
    opacity = prop.get("opacity", 1.0)
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not math.isfinite(opacity) \
            or not 0 <= opacity <= 1:
        raise ValueError("opacity must be between 0 and 1.")
    if opacity < 1:
        sprite = sprite.copy()
        sprite.putalpha(sprite.getchannel("A").point(lambda value: int(value * opacity)))

    manifest = packs.lookup(image_path) if packs is not None else None
    if manifest is not None and manifest.sha256 is not None and manifest.sha256 != info["sha256"]:
        stale = f"{image_path.name} changed since {manifest.manifest.name} was written (sha256 differs)"
    else:
        stale = None
    anchor_name = prop.get("anchor")
    known_name = isinstance(anchor_name, str) and anchor_name in ANCHOR_NAMES
    if anchor_name == "manifest":
        if "anchorPx" in prop:
            raise ValueError(f"{ident}: anchor 'manifest' conflicts with anchorPx; keep one.")
        if manifest is None or manifest.anchor_px is None:
            raise ValueError(f"{ident}: anchor 'manifest' needs {image_path.name} listed with anchor_px in a "
                             f"prop-pack manifest (pass --prop-pack).")
        anchor_source = "manifest"
    elif "anchorPx" in prop:
        anchor_source = "px"
        if anchor_name is not None and anchor_name != "px":
            reason = ("is ignored because anchorPx is given" if known_name
                      else "is unknown and ignored (anchorPx is given)")
            warnings.append(f"{ident}: anchor {anchor_name!r} {reason}.")
    elif anchor_name == "px":
        raise ValueError(f"{ident}: anchor 'px' needs anchorPx.")
    elif anchor_name in BOX_ANCHORS:
        anchor_source = f"box:{anchor_name}"
    elif anchor_name is not None:
        raise ValueError(f"Unknown anchor: {anchor_name}")
    elif anchor_policy == "manifest" and manifest is not None and manifest.anchor_px is not None:
        anchor_source = "manifest"
    else:
        anchor_source = "box:center-bottom"
        if anchor_policy == "manifest" and manifest is not None:
            warnings.append(f"{ident}: {manifest.manifest.name} lists {image_path.name} without anchor_px (a v1 "
                            f"manifest); drawn by its bottom centre. Re-extract with extract_prop_pack for anchors.")

    if anchor_source == "manifest":
        if stale:
            raise ValueError(f"{ident}: {stale}; re-run extract_prop_pack, or place it with anchorPx or --anchor px.")
        anchor_px = _anchor_px(list(manifest.anchor_px), (sw, sh), f"{ident}: manifest anchor_px")
    elif anchor_source == "px":
        anchor_px = _anchor_px(prop["anchorPx"], (sw, sh))
    else:
        bx, by = _box_anchor_offset(anchor_source[4:], sw, sh)
        anchor_px = (bx, by)
    if stale and anchor_source != "manifest" and manifest.footprint is not None:
        warnings.append(f"{ident}: {stale}; its manifest footprint may not match.")

    x = _number(prop.get("x", 0), f"{ident} x")
    y = _number(prop.get("y", 0), f"{ident} y")
    if anchor_source.startswith("box:"):
        offset_x, offset_y = _box_anchor_offset(anchor_source[4:], width, height)
        if flip:  # the mirrored art puts the box anchor at the mirrored x
            offset_x = width - offset_x
    else:
        offset_x = (sw - anchor_px[0] if flip else anchor_px[0]) * width / sw
        offset_y = anchor_px[1] * height / sh
    left, top = forge_core.round_half_up(x * scale - offset_x), forge_core.round_half_up(y * scale - offset_y)

    if "sortY" in prop:
        sort_y, sort_source = effective_sort_y(prop), "explicit"
    elif sort == "raw-y":
        sort_y, sort_source = effective_sort_y(prop), "raw-y"
    else:
        ground = {"box:center": y + height_f / 2, "box:top-left": y + height_f}.get(anchor_source, y)
        sort_y, sort_source = effective_sort_y(prop, ground), "ground-line"

    layer = str(prop.get("layer", "props"))
    if layer in ("background", "foreground"):
        band = layer
    else:
        band = "world"
        if layer not in WORLD_LAYERS:
            warnings.append(f"{ident}: layer {layer!r} is not one of background, props, objects, actors, world, "
                            f"foreground; drawn in the y-sorted world band.")

    scale_xy = (width / sw, height / sh)
    footprint = None
    mirror = sw if flip else None
    if "footprint" in prop:
        footprint = _footprint(prop["footprint"], "placement", ident, anchor_px, left, top, scale_xy, scale, warnings,
                               flip_width=mirror)
    elif manifest is not None and manifest.footprint is not None:
        reference = manifest.anchor_px if manifest.anchor_px is not None else anchor_px
        footprint = _footprint(manifest.footprint, "manifest", ident, reference, left, top, scale_xy, scale, warnings,
                               flip_width=mirror)
    if footprint is not None:
        solid = prop.get("solid", manifest.solid if manifest is not None else None)
        footprint["solid"] = solid is not False

    return Placed(
        group=group, id=ident, image_path=image_path, image_sha256=info["sha256"],
        source_size=(sw, sh), sprite=sprite, left=left, top=top, width=width, height=height, world=(x, y),
        anchor_px=anchor_px, anchor_source=anchor_source, anchor_canvas=(left + offset_x, top + offset_y),
        sort_y=sort_y, sort_source=sort_source, layer=layer, band=band, resampler=chosen,
        footprint=footprint, canvas_scale=scale, warnings=warnings, flip_x=flip)


def visible_bounds(item: Placed) -> tuple[int, int, int, int] | None:
    """Canvas box of the sprite's visible pixels (alpha > 16), not clipped to the canvas."""
    box = forge_core.subject_bbox(np.asarray(item.sprite.getchannel("A")), VISIBLE_ALPHA)
    if box is None:
        return None
    return box[0] + item.left, box[1] + item.top, box[2] + item.left, box[3] + item.top


def report_entry(item: Placed, canvas_size: tuple[int, int], report_dir: Path | None = None) -> dict[str, Any]:
    width, height = canvas_size
    bounds = visible_bounds(item)
    error = item.anchor_world_error
    return _clean({
        "id": item.id, "group": item.group, "kind": item.kind, "layer": item.layer, "band": item.band,
        "draw_index": item.draw_index,
        "image": forge_core.manifest_path(item.image_path, report_dir) if report_dir is not None else str(item.image_path),
        "image_sha256": item.image_sha256,
        "left": item.left, "top": item.top, "w": item.width, "h": item.height,
        "source_size": list(item.source_size), "anchorPx": list(item.anchor_px), "anchor_source": item.anchor_source,
        "anchor_world": list(item.world), "anchor_canvas": list(item.anchor_canvas),
        "anchor_world_error": list(error),
        "resampler": item.resampler,
        "clipped": item.left < 0 or item.top < 0 or item.left + item.width > width or item.top + item.height > height,
        "visible_bounds": list(bounds) if bounds else None,
        "sortY": item.sort_y, "sort_source": item.sort_source,
        "footprint": item.footprint,
        **({"flip_x": True} if item.flip_x else {}),
        "warnings": item.warnings,
    })


def paste_prop(canvas: Image.Image, prop: dict[str, Any], roots: list[Path], resampler: str = "lanczos", *,
               sort: str = "ground-line", anchor_policy: str = "manifest", scale: float = 1.0,
               packs: PropPackIndex | None = None, group: str = "props") -> dict[str, Any]:
    """Draw one placement onto ``canvas`` and return its report entry (library use; the CLI sorts first)."""
    item = prepare_placement(prop, roots, group=group, resampler=resampler, sort=sort,
                             anchor_policy=anchor_policy, scale=scale, packs=packs)
    canvas.alpha_composite(item.sprite, (item.left, item.top))
    return report_entry(item, canvas.size)


def draw_order(placed: Sequence[Placed], sort: str = "ground-line") -> list[Placed]:
    """Background band, world band, foreground band; inside each (sortY, x, id), or sortY then file order (raw-y)."""
    def key(item: Placed) -> tuple:
        return (item.sort_y, item.world[0], item.id) if sort == "ground-line" else (item.sort_y,)

    ordered: list[Placed] = []
    for band in BANDS:
        ordered.extend(sorted((item for item in placed if item.band == band), key=key))
    for index, item in enumerate(ordered):
        item.draw_index = index
    return ordered


def compose_scene(base: Image.Image, ordered: Sequence[Placed], *,
                  track_owners: bool = False) -> tuple[Image.Image, np.ndarray | None]:
    """Draw the placements in order. With ``track_owners`` also return, per canvas pixel, the draw index of the
    topmost placement whose alpha there exceeds 16 (-1 for none), which the audit uses for occlusion."""
    canvas = base.copy()
    owners = np.full((canvas.height, canvas.width), -1, np.int32) if track_owners else None
    for item in ordered:
        canvas.alpha_composite(item.sprite, (item.left, item.top))
        if owners is None:
            continue
        x0, y0 = max(0, item.left), max(0, item.top)
        x1, y1 = min(canvas.width, item.left + item.width), min(canvas.height, item.top + item.height)
        if x0 >= x1 or y0 >= y1:
            continue
        alpha = np.asarray(item.sprite.getchannel("A"))[y0 - item.top:y1 - item.top, x0 - item.left:x1 - item.left]
        owners[y0:y1, x0:x1][alpha > VISIBLE_ALPHA] = item.draw_index
    return canvas, owners


# --------------------------------------------------------------------------- scene geometry (bundle, stage, masks)

@dataclass
class Geometry:
    """Debug and audit geometry in canvas pixels."""
    canvas_size: tuple[int, int]
    walk: list[tuple[np.ndarray, list[np.ndarray]]] = field(default_factory=list)
    walk_source: str | None = None
    solids: list[dict[str, Any]] = field(default_factory=list)
    portals: list[dict[str, Any]] = field(default_factory=list)
    spawns: list[dict[str, Any]] = field(default_factory=list)
    approach: list[dict[str, Any]] = field(default_factory=list)
    anchors: list[dict[str, Any]] = field(default_factory=list)
    interactions: list[dict[str, Any]] = field(default_factory=list)
    slots: list[dict[str, Any]] = field(default_factory=list)
    protected: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    bands: list[float] = field(default_factory=list)
    masks: list[dict[str, Any]] = field(default_factory=list)
    actor_radius: float = 0.0
    y_squash: float = 1.0
    inputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocking_mask: np.ndarray | None = None  # canvas pixels of blocking material classes (forge_nav rule N8)
    collision: forge_nav.BlockingSet | None = None  # the bundle's D2 set, when forge_nav can read it (D4)
    collision_note: str = "no --bundle"
    base_to_world: tuple[float, float] = (1.0, 1.0)  # placement (base) pixels -> bundle world pixels


def _polygon(value: Any, name: str, transform) -> np.ndarray:
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError(f"{name} must be a polygon of at least three [x, y] points.")
    return np.array([transform(*_point(point, name)) for point in value], np.float64)


def _parse_color(value: Any) -> tuple[int, int, int] | None:
    if isinstance(value, str):
        text = value.lstrip("#")
        if re.fullmatch(r"[0-9a-fA-F]{3}", text):
            text = "".join(char * 2 for char in text)
        if re.fullmatch(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", text):
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    if isinstance(value, list) and len(value) >= 3 and all(isinstance(v, int) and 0 <= v <= 255 for v in value[:3]):
        return value[0], value[1], value[2]
    return None


def _mask_image(path: Path, canvas_size: tuple[int, int], warnings: list[str]) -> np.ndarray:
    """Boolean mask: alpha > 0 when the image has transparency, else luminance > 127; resized to the canvas."""
    image, _ = forge_core.load_rgba(path)
    pixels = np.asarray(image)
    if pixels[..., 3].min() < 255:
        plane = (pixels[..., 3] > 0).astype(np.uint8) * 255
    else:
        plane = (np.asarray(image.convert("L")) > 127).astype(np.uint8) * 255
    mask = Image.fromarray(plane)
    if mask.size != canvas_size:
        if abs(mask.width / mask.height - canvas_size[0] / canvas_size[1]) > 0.01:
            warnings.append(f"mask {path.name} has another aspect ratio than the canvas; stretched to fit.")
        mask = mask.resize(canvas_size, Image.Resampling.NEAREST)
    return np.asarray(mask) > 0


def load_bundle(path: Path, geometry: Geometry, base_size: tuple[int, int], scale: float) -> None:
    """Add a map_bundle (v1 or v2) to ``geometry``: collision, portals, spawns, anchors, interactions,
    material map and an inline or linked stage. World pixels map onto the base image (and --scale)."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a map bundle object.")
    warnings = geometry.warnings
    if data.get("schema") not in BUNDLE_SCHEMAS:
        warnings.append(f"{path.name}: schema {data.get('schema')!r} is not one of {', '.join(BUNDLE_SCHEMAS)}; "
                        f"read as v2.")
    kx = ky = scale
    world = data.get("world")
    if isinstance(world, dict) and "width" in world and "height" in world:
        world_w, world_h = _number(world["width"], "world.width"), _number(world["height"], "world.height")
        if world_w <= 0 or world_h <= 0:
            raise ValueError(f"{path.name}: world width and height must be positive.")
        kx, ky = scale * base_size[0] / world_w, scale * base_size[1] / world_h
        geometry.base_to_world = (world_w / base_size[0], world_h / base_size[1])
        if abs(kx - ky) > 0.01 * kx:
            warnings.append(f"{path.name}: world {world_w:g}x{world_h:g} and base {base_size[0]}x{base_size[1]} "
                            f"differ in aspect; geometry is stretched to the base.")

    def transform(x: float, y: float) -> tuple[float, float]:
        return x * kx, y * ky

    collision = data.get("collision") if isinstance(data.get("collision"), dict) else {}
    if "actorRadius" in collision:
        geometry.actor_radius = _number(collision["actorRadius"], "collision.actorRadius") * kx
    if "ySquash" in collision:
        geometry.y_squash = _number(collision["ySquash"], "collision.ySquash")
    for index, region in enumerate(collision.get("walkRegions") or []):
        if not isinstance(region, dict):
            raise ValueError(f"{path.name}: collision.walkRegions[{index}] must be an object.")
        polygon = _polygon(region.get("polygon"), f"walkRegions[{index}].polygon", transform)
        holes = [_polygon(hole, f"walkRegions[{index}].holes", transform) for hole in region.get("holes") or []]
        geometry.walk.append((polygon, holes))
    if collision.get("walkRegions"):
        geometry.walk_source = f"{path.name} collision.walkRegions"
    for index, solid in enumerate(collision.get("solids") or []):
        if not isinstance(solid, dict):
            raise ValueError(f"{path.name}: collision.solids[{index}] must be an object.")
        ident = str(solid.get("id", f"solid-{index}"))
        shape = solid.get("shape")
        if shape == "rect":
            x0, y0 = transform(_number(solid.get("x"), f"{ident}.x"), _number(solid.get("y"), f"{ident}.y"))
            w, h = _number(solid.get("w"), f"{ident}.w") * kx, _number(solid.get("h"), f"{ident}.h") * ky
            geometry.solids.append({"id": ident, "shape": "rect", "cx": x0 + w / 2, "cy": y0 + h / 2,
                                    "rx": w / 2, "ry": h / 2, "rotate": 0.0, "source": "bundle"})
        elif shape == "ellipse":
            cx, cy = transform(_number(solid.get("cx"), f"{ident}.cx"), _number(solid.get("cy"), f"{ident}.cy"))
            geometry.solids.append({"id": ident, "shape": "ellipse", "cx": cx, "cy": cy,
                                    "rx": _number(solid.get("rx"), f"{ident}.rx") * kx,
                                    "ry": _number(solid.get("ry"), f"{ident}.ry") * ky,
                                    "rotate": _number(solid.get("rotate", 0), f"{ident}.rotate"), "source": "bundle"})
        elif shape == "polygon":
            geometry.solids.append({"id": ident, "shape": "polygon", "source": "bundle",
                                    "points": _polygon(solid.get("points"), f"{ident}.points", transform)})
        else:
            warnings.append(f"{path.name}: solid {ident} shape {shape!r} is not rect, ellipse or polygon; ignored.")
    for index, rect in enumerate(collision.get("rects") or []):
        if not isinstance(rect, list) or len(rect) != 4:
            raise ValueError(f"{path.name}: collision.rects[{index}] must be [x, y, w, h].")
        x0, y0 = transform(_number(rect[0], "rects x"), _number(rect[1], "rects y"))
        w, h = _number(rect[2], "rects w") * kx, _number(rect[3], "rects h") * ky
        geometry.solids.append({"id": f"rect-{index}", "shape": "rect", "cx": x0 + w / 2, "cy": y0 + h / 2,
                                "rx": w / 2, "ry": h / 2, "rotate": 0.0, "source": "bundle"})
    for index, portal in enumerate(data.get("portals") or []):
        if not isinstance(portal, dict):
            raise ValueError(f"{path.name}: portals[{index}] must be an object.")
        ident = str(portal.get("id", f"portal-{index}"))
        entry: dict[str, Any] = {"id": ident, "to": str(portal.get("to", ""))}
        activation = portal.get("activation")
        if activation is not None and activation not in PORTAL_ACTIVATIONS:
            warnings.append(f"{path.name}: portal {ident} activation {activation!r} is not crossing or intent.")
        if isinstance(portal.get("rect"), list) and len(portal["rect"]) == 4:
            x0, y0 = transform(_number(portal["rect"][0], "portal x"), _number(portal["rect"][1], "portal y"))
            entry["rect"] = [x0, y0, x0 + _number(portal["rect"][2], "portal w") * kx,
                             y0 + _number(portal["rect"][3], "portal h") * ky]
        elif isinstance(portal.get("circle"), list) and len(portal["circle"]) == 3:
            cx, cy = transform(_number(portal["circle"][0], "portal cx"), _number(portal["circle"][1], "portal cy"))
            entry["circle"] = [cx, cy, _number(portal["circle"][2], "portal r") * kx]
        else:
            raise ValueError(f"{path.name}: portal {ident} needs rect [x, y, w, h] or circle [cx, cy, r].")
        if "travelDirection" in portal:
            entry["direction"] = _point(portal["travelDirection"], f"portal {ident} travelDirection")
        geometry.portals.append(entry)
    for index, spawn in enumerate(data.get("spawns") or []):
        if not isinstance(spawn, dict):
            raise ValueError(f"{path.name}: spawns[{index}] must be an object.")
        point = transform(_number(spawn.get("x"), "spawn x"), _number(spawn.get("y"), "spawn y"))
        geometry.spawns.append({"id": str(spawn.get("id", f"spawn-{index}")), "point": point,
                                "facing": spawn.get("facing")})
    anchors = data.get("anchors") if isinstance(data.get("anchors"), dict) else {}
    for name, anchor in anchors.items():
        if not isinstance(anchor, dict) or "point" not in anchor:
            raise ValueError(f"{path.name}: anchor {name} needs a point.")
        geometry.anchors.append({"id": str(name), "point": transform(*_point(anchor["point"], f"anchor {name}"))})
        approach = anchor.get("approach")
        if approach is not None:
            points = [approach] if approach and not isinstance(approach[0], list) else approach
            for number, point in enumerate(points):
                label = str(name) if len(points) == 1 else f"{name}.{number}"
                geometry.approach.append({"id": label, "point": transform(*_point(point, f"anchor {name} approach"))})
    for index, interaction in enumerate(data.get("interactions") or []):
        if not isinstance(interaction, dict):
            raise ValueError(f"{path.name}: interactions[{index}] must be an object.")
        point = transform(_number(interaction.get("x"), "interaction x"),
                          _number(interaction.get("y"), "interaction y"))
        geometry.interactions.append({"id": str(interaction.get("id", f"interaction-{index}")), "point": point,
                                      "reach": _number(interaction.get("reach", 0), "interaction reach") * kx})
    material_map = data.get("material_map")
    if isinstance(material_map, dict) and isinstance(material_map.get("image"), str):
        _load_material_map(path.parent / material_map["image"], material_map.get("materials") or {}, geometry)
    stage = data.get("stage")
    if isinstance(stage, str):
        load_stage(path.parent / stage, geometry)
    elif isinstance(stage, dict):
        _add_stage(stage, f"{path.name} stage", geometry)
    _load_collision(path, geometry, kx, ky)


def _load_collision(path: Path, geometry: Geometry, kx: float, ky: float) -> None:
    """The bundle's D2 blocking set as the vendored forge_nav reads it (D2, D4): actor feet are judged
    on it, and the overlay draws it (solids, rects, object footprints and tile collision). A bundle
    forge_nav cannot read keeps compose's own parse of it, with a warning."""
    try:
        blocking = forge_nav.read_blocking_set(path)
    except (forge_nav.NavError, OSError, ValueError) as error:
        geometry.collision_note = f"compose's own parse of {path.name} (forge_nav cannot read it: {error})"
        geometry.warnings.append(f"{path.name}: forge_nav cannot read its collision ({error}); actor feet are "
                                 "checked against compose's own parse of the bundle instead of the D2 blocking set.")
        return
    geometry.collision = blocking
    geometry.collision_note = f"forge_nav D2 blocking set of {path.name}"
    geometry.solids = [_canvas_solid(solid, kx, ky) for solid in blocking.solids]


def _canvas_solid(solid: dict[str, Any], kx: float, ky: float) -> dict[str, Any]:
    """A forge_nav solid (world px) as an overlay shape in canvas pixels."""
    ident = str(solid.get("source", ""))
    if solid["shape"] == "rect":
        w, h = solid["w"] * kx, solid["h"] * ky
        return {"id": ident, "shape": "rect", "cx": solid["x"] * kx + w / 2, "cy": solid["y"] * ky + h / 2,
                "rx": w / 2, "ry": h / 2, "rotate": 0.0, "source": "bundle"}
    if solid["shape"] == "ellipse":
        return {"id": ident, "shape": "ellipse", "cx": solid["cx"] * kx, "cy": solid["cy"] * ky,
                "rx": solid["rx"] * kx, "ry": solid["ry"] * ky, "rotate": float(solid.get("rotate", 0) or 0),
                "source": "bundle"}
    points = np.asarray(solid["points"], np.float64) * np.array([kx, ky])
    return {"id": ident, "shape": "polygon", "points": points, "source": "bundle"}


def _load_material_map(path: Path, materials: dict[str, Any], geometry: Geometry) -> None:
    """Tint mask per blocking material class: palette index for P images, else exact RGB colour."""
    geometry.inputs.append(path)
    with Image.open(path) as image:
        indices = np.asarray(image) if image.mode == "P" else None
        rgb = None if indices is not None else np.asarray(image.convert("RGB"))
    classes: dict[str, np.ndarray] = {}
    blocking = None
    for name, material in materials.items():
        if not isinstance(material, dict):
            continue
        kind = material.get("class")
        if kind not in MATERIAL_CLASSES:
            geometry.warnings.append(f"material {name}: class {kind!r} is not one of {', '.join(MATERIAL_CLASSES)}.")
            continue
        blocks = kind == "solid" or (kind in ("liquid", "hazard") and material.get("walkable") is not True)  # N8
        if kind == "decor" or (material.get("walkable") is True and not blocks):
            continue
        if indices is not None and isinstance(material.get("index"), int):
            hit = indices == material["index"]
        elif rgb is not None and _parse_color(material.get("color")) is not None:
            hit = np.all(rgb == np.array(_parse_color(material["color"]), np.uint8), axis=-1)
        else:
            geometry.warnings.append(f"material {name}: no usable index or color for {path.name}; not drawn.")
            continue
        if blocks:
            blocking = hit if blocking is None else blocking | hit
        if material.get("walkable") is not True:
            classes[kind] = classes.get(kind, np.zeros(hit.shape, bool)) | hit
    for kind, hit in classes.items():
        geometry.masks.append({"id": f"material:{kind}", "mask": _canvas_mask(hit, geometry.canvas_size),
                               "color": MATERIAL_COLORS.get(kind, (255, 255, 255))})
    if blocking is not None:
        geometry.blocking_mask = _canvas_mask(blocking, geometry.canvas_size)


def _canvas_mask(hit: np.ndarray, canvas_size: tuple[int, int]) -> np.ndarray:
    return np.asarray(Image.fromarray(hit.astype(np.uint8) * 255).resize(canvas_size, Image.Resampling.NEAREST)) > 0


def load_stage(path: Path, geometry: Geometry) -> None:
    geometry.inputs.append(path)
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a stage object.")
    _add_stage(data, path.name, geometry)


def _add_stage(data: dict[str, Any], name: str, geometry: Geometry) -> None:
    """Stage (stage_v1): UV coordinates of the plate, drawn over the whole canvas."""
    width, height = geometry.canvas_size

    def transform(u: float, v: float) -> tuple[float, float]:
        return u * width, v * height

    polygons = data.get("groundPolygons") or []
    ground = [(_polygon(polygon, f"{name} groundPolygons", transform), []) for polygon in polygons]
    if ground and not geometry.walk:
        geometry.walk.extend(ground)
        geometry.walk_source = f"{name} groundPolygons"
    elif ground:
        geometry.protected.extend({"id": f"ground-{i}", "polygon": polygon, "kind": "ground"}
                                  for i, (polygon, _) in enumerate(ground))
    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    for kind in ("hero", "enemy"):
        for index, point in enumerate(slots.get(kind) or []):
            geometry.slots.append({"id": f"{kind}{index + 1}", "kind": kind,
                                   "point": transform(*_point(point, f"{name} slots.{kind}"))})
    if slots.get("boss") is not None:
        geometry.slots.append({"id": "boss", "kind": "boss", "point": transform(*_point(slots["boss"], "slots.boss"))})
    for index, region in enumerate(data.get("protectedRegions") or []):
        ident = str(region.get("id", f"protected-{index}")) if isinstance(region, dict) else f"protected-{index}"
        if isinstance(region, dict) and isinstance(region.get("polygon"), list):
            polygon = _polygon(region["polygon"], f"{name} protectedRegions", transform)
        elif isinstance(region, dict) and isinstance(region.get("box"), list) and len(region["box"]) == 4:
            u0, v0, u1, v1 = (_number(v, f"{name} protected box") for v in region["box"])
            polygon = np.array([transform(u0, v0), transform(u1, v0), transform(u1, v1), transform(u0, v1)])
        else:
            raise ValueError(f"{name}: protected region {ident} needs a polygon or a box.")
        geometry.protected.append({"id": ident, "polygon": polygon, "kind": "protected"})
    for index, effect in enumerate(data.get("effects") or []):
        if isinstance(effect, dict) and isinstance(effect.get("polygon"), list):
            geometry.effects.append({"id": str(effect.get("id", f"effect-{index}")), "kind": str(effect.get("kind")),
                                     "polygon": _polygon(effect["polygon"], f"{name} effects", transform)})
    for index, point in enumerate(data.get("approachPoints") or []):
        geometry.approach.append({"id": f"approach-{index + 1}", "point": transform(*_point(point, "approachPoints"))})
    band = data.get("playableBand")
    if isinstance(band, list) and len(band) == 2:
        geometry.bands.extend(_number(value, "playableBand") * height for value in band)


def load_geometry(args: argparse.Namespace, canvas_size: tuple[int, int], base_size: tuple[int, int]) -> Geometry:
    geometry = Geometry(canvas_size=canvas_size)
    if args.bundle:
        geometry.inputs.append(args.bundle)
        load_bundle(args.bundle, geometry, base_size, args.scale)
    if args.stage:
        load_stage(args.stage, geometry)
    for index, path in enumerate(args.mask or []):
        geometry.inputs.append(path)
        geometry.masks.append({"id": path.name, "mask": _mask_image(path, canvas_size, geometry.warnings),
                               "color": MASK_COLORS[index % len(MASK_COLORS)]})
    return geometry


# --------------------------------------------------------------------------- geometry tests

def nav_solid(shape: dict[str, Any]) -> dict[str, Any]:
    """A compose shape (canvas pixels: rect or ellipse by centre and radii with a rotation, or a
    polygon) as a forge_nav solid, a closed set (D1)."""
    ident = str(shape.get("id", ""))
    if shape["shape"] == "polygon":
        return {"shape": "polygon", "points": np.asarray(shape["points"], np.float64).tolist(), "source": ident}
    footprint = {"shape": shape["shape"], "width": 2 * shape["rx"], "depth": 2 * shape["ry"],
                 "rotate": shape.get("rotate", 0.0), "basis": "world_px"}
    solid = forge_nav.footprint_solid(shape["cx"], shape["cy"], footprint, source=ident)
    return solid or {"shape": "rect", "x": shape["cx"], "y": shape["cy"], "w": 0.0, "h": 0.0, "source": ident}


def _shape_hits(model: forge_nav.CollisionModel, xs: np.ndarray, ys: np.ndarray) -> tuple[list[str], np.ndarray]:
    """(sources of the model's solids that contain any of the points, which points lie in a solid)."""
    hits: list[str] = []
    inside = np.zeros(np.shape(xs), bool)
    for shape, source in zip(model.solids, model.solid_sources):
        x0, y0, x1, y1 = shape.bounds
        near = (xs >= x0) & (xs <= x1) & (ys >= y0) & (ys <= y1)
        if not near.any():
            continue
        found = np.zeros(np.shape(xs), bool)
        found[near] = shape.contains(xs[near], ys[near])
        if found.any():
            inside |= found
            if source not in hits:
                hits.append(source)
    return hits, inside


@dataclass
class FeetModel:
    """What actor feet are judged against: the bundle's D2 set in world pixels (kind "bundle",
    forge_nav, D4), or the canvas with the stage ground polygons, compose's own parse of a bundle
    and the solid placement footprints (kind "canvas"), with the same closed-set rules (D1)."""
    kind: str
    model: forge_nav.CollisionModel
    to_model: tuple[float, float]  # multiply a placement position (base pixels) by this
    note: str
    mask: np.ndarray | None = None  # canvas blocking material mask (canvas kind only)

    def position(self, item: Placed) -> tuple[float, float]:
        return item.world[0] * self.to_model[0], item.world[1] * self.to_model[1]

    def samples(self, point: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
        offsets = forge_nav.footprint_offsets(self.model.radius, self.model.y_squash)
        return point[0] + offsets[:, 0], point[1] + offsets[:, 1]

    def _mask_hit(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        hit = np.zeros(np.shape(xs), bool)
        if self.mask is not None:
            height, width = self.mask.shape
            cols, rows = np.floor(xs).astype(np.int64), np.floor(ys).astype(np.int64)
            inside = (cols >= 0) & (cols < width) & (rows >= 0) & (rows < height)
            hit[inside] = self.mask[rows[inside], cols[inside]]
        return hit

    def actor(self, item: Placed) -> tuple[bool, list[str], tuple[float, float]]:
        """(valid, blocker sources, the position tested) of an actor (forge_nav rule N9); a sample
        blocked by no solid lies on a blocking material pixel ("material_map")."""
        point = self.position(item)
        xs, ys = self.samples(point)
        mask_hit = self._mask_hit(xs, ys)
        valid = bool(self.model.valid(point[0], point[1])) and not mask_hit.any()
        blockers, in_solid = _shape_hits(self.model, xs, ys)
        if (self.model.blocked(xs, ys) & ~in_solid).any() or mask_hit.any():
            blockers.append("material_map")
        return valid, blockers, point

    def walkable(self, item: Placed) -> bool:
        point = self.position(item)
        return bool(self.model.area_ok(point[0], point[1]))


def feet_model(geometry: Geometry, ordered: Sequence[Placed], scale: float) -> FeetModel:
    """The bundle's forge_nav model when there is one (D4), else the canvas model (closed sets, D1)."""
    if geometry.collision is not None:
        return FeetModel("bundle", geometry.collision.model(), geometry.base_to_world, geometry.collision_note)
    solids = [nav_solid(shape) for shape in geometry.solids]
    solids += [nav_solid({**item.footprint, "id": item.id}) for item in ordered
               if item.footprint and item.footprint["solid"] and item.kind != "actor"]
    width, height = geometry.canvas_size
    model = forge_nav.CollisionModel(width, height, geometry.actor_radius, geometry.y_squash,
                                     [(polygon, holes) for polygon, holes in geometry.walk], solids)
    note = geometry.collision_note if geometry.collision_note != "no --bundle" else (
        f"canvas: {geometry.walk_source or 'canvas bounds'}, solid placement footprints")
    return FeetModel("canvas", model, (scale, scale), note, geometry.blocking_mask)


def shape_mask(shape: dict[str, Any]) -> tuple[tuple[int, int, int, int], np.ndarray]:
    """Rasterise a shape at pixel centres over its bounding box; returns (box, boolean mask)."""
    if shape["shape"] == "polygon":
        xs, ys = shape["points"][:, 0], shape["points"][:, 1]
        box = (math.floor(xs.min()), math.floor(ys.min()), math.ceil(xs.max()), math.ceil(ys.max()))
    else:
        reach = math.hypot(shape["rx"], shape["ry"]) if shape.get("rotate") else None
        ext_x, ext_y = (reach, reach) if reach else (shape["rx"], shape["ry"])
        box = (math.floor(shape["cx"] - ext_x), math.floor(shape["cy"] - ext_y),
               math.ceil(shape["cx"] + ext_x), math.ceil(shape["cy"] + ext_y))
    width, height = max(0, box[2] - box[0]), max(0, box[3] - box[1])
    if width == 0 or height == 0:
        return box, np.zeros((height, width), bool)
    gy, gx = np.mgrid[box[1]:box[3], box[0]:box[2]]
    model = forge_nav.CollisionModel(box[2], box[3], 0, solids=[nav_solid(shape)])
    return box, model.blocked(gx + 0.5, gy + 0.5)


def overlap_area(first: tuple[tuple[int, int, int, int], np.ndarray],
                 second: tuple[tuple[int, int, int, int], np.ndarray]) -> int:
    (a, mask_a), (b, mask_b) = first, second
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x0 >= x1 or y0 >= y1:
        return 0
    return int(np.count_nonzero(mask_a[y0 - a[1]:y1 - a[1], x0 - a[0]:x1 - a[0]]
                                & mask_b[y0 - b[1]:y1 - b[1], x0 - b[0]:x1 - b[0]]))


# --------------------------------------------------------------------------- audit

def _check(identifier: str, status: str, value: Any, threshold: Any = None) -> dict[str, Any]:
    return {"id": identifier, "status": status, "value": _clean(value), "threshold": threshold}


def audit_placements(ordered: Sequence[Placed], owners: np.ndarray, geometry: Geometry,
                     scale: float = 1.0) -> dict[str, Any]:
    """Feet inside walkable areas, bounds, footprint overlaps, duplicates and actor visibility (no I/O).

    Actor feet follow forge_nav rule N9 at the placement position: with a bundle on its D2 blocking
    set (D2, D4: the same answer as map_nav.py query), else on the canvas model (closed sets, D1)."""
    width, height = geometry.canvas_size
    rows: list[dict[str, Any]] = []
    actor_invalid, outside, clipped, off_canvas, empty, hidden = [], [], [], [], [], []
    feet = feet_model(geometry, ordered, scale)
    props = [nav_solid({**item.footprint, "id": item.id}) for item in ordered
             if item.footprint and item.footprint["solid"] and item.kind != "actor"]
    prop_model = forge_nav.CollisionModel(width, height, geometry.actor_radius, geometry.y_squash, solids=props)
    in_props: list[str] = []
    for item in ordered:
        foot = item.anchor_canvas
        bounds = visible_bounds(item)
        row: dict[str, Any] = {"id": item.id, "kind": item.kind, "band": item.band, "draw_index": item.draw_index,
                               "foot": list(foot), "visible_bounds": list(bounds) if bounds else None}
        if item.kind == "actor":
            valid, hits, point = feet.actor(item)
            row["foot_valid"] = valid
            row["blocked_by"] = hits
            row["foot_checked"] = list(point)
            if not valid:
                actor_invalid.append(item.id)
            if feet.kind == "bundle":
                canvas_xs, canvas_ys = (item.world[0] * scale + prop_model.offsets[:, 0],
                                        item.world[1] * scale + prop_model.offsets[:, 1])
                row["in_placement_footprints"] = _shape_hits(prop_model, canvas_xs, canvas_ys)[0]
                if valid and row["in_placement_footprints"]:
                    in_props.append(item.id)
        elif item.band != "foreground":
            row["foot_walkable"] = feet.walkable(item)
            if not row["foot_walkable"]:
                outside.append(item.id)
        if bounds is None:
            empty.append(item.id)
            row["occluded_fraction"] = None
        else:
            in_canvas = bounds[0] < width and bounds[1] < height and bounds[2] > 0 and bounds[3] > 0
            if not in_canvas:
                off_canvas.append(item.id)
            elif bounds[0] < 0 or bounds[1] < 0 or bounds[2] > width or bounds[3] > height:
                clipped.append(item.id)
            x0, y0 = max(0, item.left), max(0, item.top)
            x1, y1 = min(width, item.left + item.width), min(height, item.top + item.height)
            if x0 < x1 and y0 < y1:
                alpha = np.asarray(item.sprite.getchannel("A"))[y0 - item.top:y1 - item.top,
                                                                x0 - item.left:x1 - item.left]
                region = owners[y0:y1, x0:x1][alpha > VISIBLE_ALPHA]
            else:
                region = np.zeros(0, np.int32)
            covered = region[region != item.draw_index]
            row["occluded_fraction"] = float(covered.size / region.size) if region.size else None
            if covered.size:
                counts = np.bincount(covered)
                top = np.argsort(-counts, kind="stable")[:3]
                row["occluded_by"] = [ordered[int(index)].id for index in top if counts[index]]
            if item.kind == "actor" and region.size and covered.size == region.size:
                hidden.append(item.id)
        row["footprint"] = _clean(item.footprint)
        rows.append(row)

    solid_prints = [item for item in ordered if item.footprint and item.footprint["solid"] and item.kind != "actor"]
    masks = {item.draw_index: shape_mask(item.footprint) for item in solid_prints}
    overlaps = []
    for index, first in enumerate(solid_prints):
        for second in solid_prints[index + 1:]:
            area = overlap_area(masks[first.draw_index], masks[second.draw_index])
            if area:
                overlaps.append({"a": first.id, "b": second.id, "kind": "footprint", "area_px": area})
    without = sum(1 for item in ordered if item.footprint is None and item.band != "foreground")

    seen: dict[tuple, str] = {}
    duplicates = []
    for item in ordered:
        key = (item.image_sha256, forge_core.round_half_up(item.world[0] * 2), forge_core.round_half_up(item.world[1] * 2),
               item.band)
        if key in seen:
            duplicates.append({"a": seen[key], "b": item.id})
        else:
            seen[key] = item.id

    actors = [item for item in ordered if item.kind == "actor"]
    checks = [
        _check("actor_feet_valid", "skipped" if not actors else ("fail" if actor_invalid else "pass"),
               {"checked": len(actors), "invalid": actor_invalid, "actor_radius_px": geometry.actor_radius,
                "y_squash": geometry.y_squash, "model": feet.kind, "collision": feet.note},
               "forge_nav rule N9: the foot and 8 footprint samples in the walk area and off every blocker "
               "(with --bundle the bundle's D2 blocking set, as map_nav.py judges it)"),
        _check("actor_feet_off_placement_footprints",
               "skipped" if not actors or feet.kind != "bundle" or not props else ("warn" if in_props else "pass"),
               {"checked": len(actors) if feet.kind == "bundle" else 0, "actors": in_props},
               "placement footprints are compose-only (the bundle's blocking set decides validity, D2); an actor "
               "standing in one is reported so the prop can be added to the bundle"),
        _check("feet_in_walk_area", "warn" if outside else "pass",
               {"checked": sum(1 for row in rows if "foot_walkable" in row), "outside": outside,
                "walk_area": geometry.walk_source or "canvas bounds"}, "foot inside a walk area"),
        _check("bounds", "fail" if off_canvas else ("warn" if clipped or empty else "pass"),
               {"off_canvas": off_canvas, "clipped": clipped, "empty": empty},
               "visible pixels (alpha > 16) inside the canvas"),
        _check("footprint_overlaps", "skipped" if len(solid_prints) < 2 else ("warn" if overlaps else "pass"),
               {"pairs": overlaps, "checked": len(solid_prints), "without_footprint": without},
               "solid footprints do not overlap"),
        _check("duplicate_placements", "warn" if duplicates else "pass", duplicates,
               "no two placements of the same image at the same point"),
        _check("actors_visible", "skipped" if not actors else ("warn" if hidden else "pass"), {"hidden": hidden},
               "no actor fully covered by later draws"),
    ]
    statuses = {check["status"] for check in checks}
    status = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "pass")
    return {"schema": AUDIT_SCHEMA, "status": status,
            "method": "compose_layered_preview placement audit: actor feet by forge_nav rule N9 at the placement "
                      "positions (with --bundle on the bundle's D2 blocking set in world pixels, else on the "
                      "canvas, stage ground polygons and solid placement footprints, closed sets), prop feet as "
                      "points in the walk area, solid footprints rasterised at pixel centres, occlusion from the "
                      "draw-index owner map at alpha > 16.",
            "notProven": list(AUDIT_NOT_PROVEN), "checks": checks, "inputs": [], "outputs": [], "tool": dict(TOOL),
            "placements": rows, "overlaps": overlaps, "walk_area": geometry.walk_source or "canvas bounds"}


# --------------------------------------------------------------------------- debug overlay

def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError, ImportError):
        return ImageFont.load_default()


def _ellipse_points(cx: float, cy: float, rx: float, ry: float, rotate: float, count: int = 48) -> list[tuple]:
    theta = math.radians(rotate)
    points = []
    for index in range(count):
        angle = 2 * math.pi * index / count
        u, v = rx * math.cos(angle), ry * math.sin(angle)
        points.append((cx + u * math.cos(theta) - v * math.sin(theta), cy + u * math.sin(theta) + v * math.cos(theta)))
    return points


def _shape_outline(shape: dict[str, Any]) -> list[tuple]:
    if shape["shape"] == "polygon":
        return [tuple(point) for point in shape["points"]]
    if shape["shape"] == "rect":
        theta = math.radians(shape.get("rotate", 0.0))
        corners = [(-shape["rx"], -shape["ry"]), (shape["rx"], -shape["ry"]), (shape["rx"], shape["ry"]),
                   (-shape["rx"], shape["ry"])]
        return [(shape["cx"] + u * math.cos(theta) - v * math.sin(theta),
                 shape["cy"] + u * math.sin(theta) + v * math.cos(theta)) for u, v in corners]
    return _ellipse_points(shape["cx"], shape["cy"], shape["rx"], shape["ry"], shape.get("rotate", 0.0))


def render_debug_overlay(canvas: Image.Image, ordered: Sequence[Placed], geometry: Geometry,
                         audit: dict[str, Any] | None, labels: str = "actors") -> Image.Image:
    """The preview with walk regions, holes, solids, footprints, exits, spawns, approach points, anchors,
    interactions, stage slots, protected regions, masks and placement feet drawn on top (diagnostic only).
    The map keeps the preview's pixel coordinates; a legend panel with counts is appended on the right."""
    width, height = canvas.size
    line = max(1, round(min(width, height) / 360))
    mark = max(3, line * 3)
    font = _font(max(10, round(min(width, height) / 55)))
    measure_kwargs: dict[str, Any] = {"font": font}
    if isinstance(font, ImageFont.FreeTypeFont):
        measure_kwargs["stroke_width"] = max(1, line // 2)
    text_kwargs = dict(measure_kwargs)
    if "stroke_width" in text_kwargs:
        text_kwargs["stroke_fill"] = (0, 0, 0, 255)
    composite = canvas.copy()
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def flush() -> None:
        """Blend the finished layer onto the picture and start a new one: ImageDraw replaces the pixels of an
        RGBA layer instead of blending, so each category gets its own layer and they stack in order."""
        nonlocal layer, draw
        composite.alpha_composite(layer)
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

    def text(xy: tuple[float, float], value: str, color: tuple[int, int, int]) -> None:
        """A label beside a point, flipped to the other side when it would leave the canvas."""
        value = forge_core.ascii_text(value)
        box = draw.textbbox((0, 0), value, **measure_kwargs)
        label_w, label_h = box[2] - box[0], box[3] - box[1]
        x = xy[0] + mark + 1 if xy[0] + mark + 1 + label_w <= width else xy[0] - mark - 1 - label_w
        y = xy[1] - mark - 1 - label_h if xy[1] - mark - 1 - label_h >= 0 else xy[1] + mark + 1
        draw.text((max(0, x) - box[0], y - box[1]), value, fill=(*color, 255), **text_kwargs)

    def cross(xy: tuple[float, float], color: tuple[int, int, int], size: int = mark) -> None:
        draw.line([(xy[0] - size, xy[1]), (xy[0] + size, xy[1])], fill=(*color, 255), width=line)
        draw.line([(xy[0], xy[1] - size), (xy[0], xy[1] + size)], fill=(*color, 255), width=line)

    def diamond(xy: tuple[float, float], color: tuple[int, int, int]) -> None:
        x, y = xy
        draw.polygon([(x, y - mark), (x + mark, y), (x, y + mark), (x - mark, y)], fill=(*color, 160),
                     outline=(*color, 255), width=line)

    def ring(xy: tuple[float, float], radius: float, color: tuple[int, int, int], fill_alpha: int = 0) -> None:
        x, y = xy
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=(*color, 255),
                     fill=(*color, fill_alpha) if fill_alpha else None, width=line)

    def arrow(start: tuple[float, float], direction: tuple[float, float], length: float,
              color: tuple[int, int, int]) -> None:
        norm = math.hypot(*direction)
        if norm == 0:
            return
        ux, uy = direction[0] / norm, direction[1] / norm
        end = (start[0] + ux * length, start[1] + uy * length)
        draw.line([start, end], fill=(*color, 255), width=line)
        for side in (-1, 1):
            hx = end[0] - ux * mark * 2 + side * -uy * mark
            hy = end[1] - uy * mark * 2 + side * ux * mark
            draw.line([end, (hx, hy)], fill=(*color, 255), width=line)

    for polygon, holes in geometry.walk:
        draw.polygon([tuple(p) for p in polygon], fill=(*COLORS["walk"], 56), outline=(*COLORS["walk"], 255),
                     width=line)
        for hole in holes:
            draw.polygon([tuple(p) for p in hole], fill=(0, 0, 0, 70), outline=(*COLORS["hole"], 255), width=line)
    flush()
    tint = np.zeros((height, width, 4), np.uint8)
    for entry in geometry.masks:
        tint[entry["mask"]] = (*entry["color"], 90)
    composite.alpha_composite(Image.fromarray(tint))
    for region in geometry.protected:
        color = COLORS["protected"] if region["kind"] == "protected" else COLORS["walk"]
        draw.polygon([tuple(p) for p in region["polygon"]], fill=(*color, 60), outline=(*color, 255), width=line)
        text(tuple(region["polygon"][0]), region["id"], color)
    for effect in geometry.effects:
        draw.polygon([tuple(p) for p in effect["polygon"]], outline=(*COLORS["effect"], 255), width=line)
        text(tuple(effect["polygon"][0]), f"{effect['id']} ({effect['kind']})", COLORS["effect"])
    for row in geometry.bands:
        draw.line([(0, row), (width, row)], fill=(*COLORS["approach"], 255), width=line)
    flush()
    for shape in geometry.solids:
        draw.polygon(_shape_outline(shape), fill=(*COLORS["solid"], 70), outline=(*COLORS["solid"], 255), width=line)
    flush()
    for item in ordered:
        if item.footprint is not None:
            color = COLORS["footprint"] if item.footprint["solid"] else COLORS["hole"]
            draw.polygon(_shape_outline(item.footprint), fill=(*color, 60), outline=(*color, 255), width=line)
    flush()
    for portal in geometry.portals:
        if "rect" in portal:
            draw.rectangle(portal["rect"], outline=(*COLORS["portal"], 255), fill=(*COLORS["portal"], 50), width=line)
            centre = ((portal["rect"][0] + portal["rect"][2]) / 2, (portal["rect"][1] + portal["rect"][3]) / 2)
        else:
            centre = (portal["circle"][0], portal["circle"][1])
            ring(centre, portal["circle"][2], COLORS["portal"], 50)
        if "direction" in portal:
            arrow(centre, portal["direction"], mark * 5, COLORS["portal"])
        text(centre, f"exit {portal['id']} -> {portal['to']}", COLORS["portal"])
    for interaction in geometry.interactions:
        ring(interaction["point"], max(interaction["reach"], mark), COLORS["interaction"], 40)
        text(interaction["point"], interaction["id"], COLORS["interaction"])
    for anchor in geometry.anchors:
        diamond(anchor["point"], COLORS["anchor"])
        text(anchor["point"], anchor["id"], COLORS["anchor"])
    for point in geometry.approach:
        ring(point["point"], mark, COLORS["approach"], 120)
        text(point["point"], point["id"], COLORS["approach"])
    for spawn in geometry.spawns:
        ring(spawn["point"], mark * 1.5, COLORS["spawn"], 120)
        facing = spawn.get("facing")
        if isinstance(facing, (int, float)) and not isinstance(facing, bool):
            arrow(spawn["point"], (math.cos(math.radians(facing)), math.sin(math.radians(facing))), mark * 4,
                  COLORS["spawn"])
        text(spawn["point"], f"spawn {spawn['id']}", COLORS["spawn"])
    for slot in geometry.slots:
        diamond(slot["point"], COLORS[f"slot_{slot['kind']}"])
        text(slot["point"], slot["id"], COLORS[f"slot_{slot['kind']}"])
    invalid = set()
    if audit is not None:
        for check in audit["checks"]:
            if check["id"] == "actor_feet_valid":
                invalid = set(check["value"]["invalid"])
    for item in ordered:
        color = COLORS["invalid"] if item.id in invalid else COLORS["foot"]
        cross(item.anchor_canvas, color)
        if item.kind == "actor":
            bounds = visible_bounds(item)
            if bounds:
                draw.rectangle(bounds, outline=(*color, 200), width=line)
            if geometry.actor_radius > 0:
                draw.polygon(_ellipse_points(*item.anchor_canvas, geometry.actor_radius,
                                             geometry.actor_radius * geometry.y_squash, 0.0),
                             outline=(*color, 255), width=line)
        if labels == "all" or (labels == "actors" and item.kind == "actor"):
            text(item.anchor_canvas, f"{item.kind} {item.id}", color)
    footprints = sum(1 for item in ordered if item.footprint is not None)
    legend = [
        (f"walk area ({len(geometry.walk)}, {geometry.walk_source or 'none: canvas bounds'})", COLORS["walk"]),
        (f"solid ({len(geometry.solids)})", COLORS["solid"]), (f"footprint ({footprints})", COLORS["footprint"]),
        (f"exit ({len(geometry.portals)})", COLORS["portal"]), (f"spawn ({len(geometry.spawns)})", COLORS["spawn"]),
        (f"approach ({len(geometry.approach)})", COLORS["approach"]),
        (f"anchor ({len(geometry.anchors)})", COLORS["anchor"]),
        (f"interaction ({len(geometry.interactions)})", COLORS["interaction"]),
        (f"slot ({len(geometry.slots)})", COLORS["slot_hero"]),
        (f"protected ({len(geometry.protected)})", COLORS["protected"]),
        (f"mask ({len(geometry.masks)})", MASK_COLORS[0]),
        (f"foot ({len(ordered)}; red = invalid actor)", COLORS["foot"]),
    ]
    flush()
    step = (font.size if isinstance(font, ImageFont.FreeTypeFont) else 11) + 6
    measure = ImageDraw.Draw(composite)
    panel_w = 16 + step + max(measure.textbbox((0, 0), forge_core.ascii_text(label), **measure_kwargs)[2]
                              for label, _ in legend)
    result = Image.new("RGBA", (width + panel_w, max(height, 8 + step * len(legend))), (24, 24, 24, 255))
    result.paste(composite, (0, 0))
    panel = ImageDraw.Draw(result)
    for index, (label, color) in enumerate(legend):
        top = 4 + index * step
        panel.rectangle([width + 6, top, width + step, top + step - 6], fill=(*color, 255))
        panel.text((width + 10 + step, top), forge_core.ascii_text(label), fill=(255, 255, 255, 255), **text_kwargs)
    return result


# --------------------------------------------------------------------------- plate pan

def plate_pan(plate: Image.Image, viewport: tuple[int, int], zoom: float = 1.12, frames: int = 5,
              focus_v: float = 0.5, resampler: str = "lanczos") -> tuple[Image.Image, list[dict[str, Any]]]:
    """Single-plate pan: a ``zoom``-times cover window of the plate panned left to right (u = 0..1) at
    vertical focus ``focus_v``, each frame scaled to ``viewport``; frames stacked top to bottom with a
    4 px transparent gap. Returns the sheet and each frame's source window [x0, y0, x1, y1)."""
    width, height = plate.size
    view_w, view_h = viewport
    window_w, window_h = width / zoom, height / zoom
    if window_w / window_h > view_w / view_h:
        window_w = window_h * view_w / view_h
    else:
        window_h = window_w * view_h / view_w
    sheet = Image.new("RGBA", (view_w, frames * view_h + (frames - 1) * PAN_GAP_PX), (0, 0, 0, 0))
    windows = []
    for index in range(frames):
        u = 0.5 if frames == 1 else index / (frames - 1)
        x0, y0 = (width - window_w) * u, (height - window_h) * focus_v
        box = (x0, y0, x0 + window_w, y0 + window_h)
        sheet.paste(plate.resize((view_w, view_h), _resample_filter(resampler), box=box),
                    (0, index * (view_h + PAN_GAP_PX)))
        windows.append({"u": u, "v": focus_v, "window": list(box), "sheet_top": index * (view_h + PAN_GAP_PX)})
    return sheet, _clean(windows)


# --------------------------------------------------------------------------- outputs

def _output_paths(args: argparse.Namespace) -> list[tuple[str, Path]]:
    roles = [("--output", args.output), ("--report", args.report), ("--debug-overlay", args.debug_overlay),
             ("--audit-out", args.audit_out), ("--plate-pan", args.plate_pan)]
    return [(role, Path(path)) for role, path in roles if path is not None]


def check_outputs(outputs: Sequence[tuple[str, Path]], inputs: Iterable[tuple[str, Path]], *,
                  require_new: bool = True) -> None:
    """Outputs must be distinct, must not alias an input (MAP-22) and, with ``require_new``, must not exist."""
    seen: dict[str, str] = {}
    inputs = [(role, Path(path)) for role, path in inputs]
    for role, path in outputs:
        key = path_key(path)
        if key in seen:
            raise ValueError(f"{role} and {seen[key]} name the same file: {path}")
        seen[key] = role
        for source_role, source in inputs:
            same = key == path_key(source)
            if not same and path.exists() and source.exists():
                with contextlib.suppress(OSError):
                    same = path.samefile(source)
            if same:
                raise ValueError(f"{role} aliases the input {source_role}: {path}")
        if require_new and os.path.lexists(path):
            raise FileExistsError(f"refusing to replace existing output {role}: {path}")


def publish_outputs(files: Sequence[tuple[Path, Path]]) -> None:
    """Publish staged files without replacing anything; on any failure remove the ones already published."""
    published: list[Path] = []
    try:
        for source, destination in files:
            forge_core.publish_file_no_replace(source, destination)
            published.append(destination)
    except BaseException:
        for path in published:
            with contextlib.suppress(OSError):
                path.unlink()
        raise


def _json_bytes(data: Any) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def compositing_order(sort: str) -> list[dict[str, Any]]:
    keys = ["sortY", "x", "id"] if sort == "ground-line" else ["sortY", "file order"]
    return [{"band": "base", "draws": "the base image (scaled by --scale)"},
            {"band": "background", "draws": "placements on layer background", "sorted_by": keys},
            {"band": "world", "draws": "props, objects, actors and every other layer", "sorted_by": keys},
            {"band": "foreground", "draws": "the foreground list and layer foreground", "sorted_by": keys},
            {"band": "not baked", "draws": "the debug overlay and plate pan are separate files"}]


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The CLI without console I/O: compose, audit, render the optional outputs, then publish them."""
    if not math.isfinite(args.scale) or args.scale <= 0:
        raise ValueError("--scale must be a positive number.")
    if args.resampler == "nearest" and abs(args.scale - round(args.scale)) > 1e-9:
        raise ValueError("--scale must be a whole number with --resampler nearest (pixel art).")
    if not 1 <= args.pan_frames <= 64:
        raise ValueError("--pan-frames must be between 1 and 64.")
    if not math.isfinite(args.pan_zoom) or args.pan_zoom < 1:
        raise ValueError("--pan-zoom must be at least 1 (the window is the plate divided by the zoom).")
    if not math.isfinite(args.pan_v) or not 0 <= args.pan_v <= 1:
        raise ValueError("--pan-v must be between 0 and 1.")
    outputs = _output_paths(args)
    inputs = [("--base", args.base), ("--placements", args.placements)]
    inputs += [("--prop-pack", path) for path in args.prop_pack or []]
    inputs += [(role, path) for role, path in (("--bundle", args.bundle), ("--stage", args.stage)) if path]
    inputs += [("--mask", path) for path in args.mask or []]
    check_outputs(outputs, inputs, require_new=False)  # existence is checked once every input is known

    base, base_info = forge_core.load_rgba(args.base)
    data = read_json(args.placements)
    pairs = collect_placements(data)
    warnings = placement_file_warnings(data)
    need_audit = bool(args.audit_out or args.strict or args.debug_overlay)
    # Legacy --anchor px never takes anchors from manifests; it still finds them for audit footprints.
    packs = PropPackIndex(args.prop_pack or [], discover=args.anchor == "manifest" or need_audit)
    roots = [args.placements.parent, args.base.parent, args.project_root]
    images = ImageCache()
    placed = [prepare_placement(prop, roots, group=group, resampler=args.resampler, sort=args.sort,
                                anchor_policy=args.anchor, scale=args.scale, packs=packs, images=images)
              for group, prop in pairs]
    warnings += packs.warnings
    seen_ids: set[str] = set()
    for item in placed:
        warnings += item.warnings
        if item.id in seen_ids:
            warnings.append(f"placement id {item.id!r} is used more than once; draw-order ties use ids.")
        seen_ids.add(item.id)
    check_outputs(outputs, inputs + [("prop image", item.image_path) for item in placed]
                  + [("prop-pack manifest", path) for path in packs.manifests.values()])

    if args.scale == 1:
        scaled = base
    else:
        size = (forge_core.round_half_up(base.width * args.scale), forge_core.round_half_up(base.height * args.scale))
        scaled = base.resize(size, _resample_filter(args.resampler))
    ordered = draw_order(placed, args.sort)
    canvas, owners = compose_scene(scaled, ordered, track_owners=need_audit)
    geometry = None
    if need_audit:
        geometry = load_geometry(args, canvas.size, base.size)
        warnings += geometry.warnings
        check_outputs(outputs, inputs + [("geometry input", path) for path in geometry.inputs])
    elif args.bundle or args.stage or args.mask:
        warnings.append("--bundle, --stage and --mask are read only with --debug-overlay, --audit-out or --strict.")
    audit = audit_placements(ordered, owners, geometry, args.scale) if need_audit else None
    if args.strict and audit["status"] == "fail":
        failed = [check["id"] for check in audit["checks"] if check["status"] == "fail"]
        raise ValueError(f"strict audit failed ({', '.join(failed)}); nothing was written.")

    with tempfile.TemporaryDirectory(prefix="compose-layered-") as temporary:
        stage = Path(temporary)
        staged: list[tuple[Path, Path]] = []
        preview = stage / "preview.png"
        forge_core.save_png(canvas, preview)
        preview_sha = forge_core.sha256_file(preview)
        staged.append((preview, args.output))
        pan_windows = None
        if args.plate_pan:
            sheet, pan_windows = plate_pan(canvas, args.pan_viewport, args.pan_zoom, args.pan_frames, args.pan_v,
                                           args.resampler)
            forge_core.save_png(sheet, stage / "pan.png")
            staged.append((stage / "pan.png", args.plate_pan))
        if args.debug_overlay:
            overlay = render_debug_overlay(canvas, ordered, geometry, audit, args.overlay_labels)
            forge_core.save_png(overlay, stage / "overlay.png")
            staged.append((stage / "overlay.png", args.debug_overlay))
        if audit is not None and args.audit_out:
            audit_dir = Path(args.audit_out).resolve().parent
            audit_doc = dict(audit)
            audit_doc["inputs"] = _input_refs(args, placed, packs, geometry, audit_dir)
            audit_doc["outputs"] = [{"path": forge_core.manifest_path(args.output, audit_dir), "sha256": preview_sha,
                                     "bytes": preview.stat().st_size}]
            (stage / "audit.json").write_bytes(_json_bytes(audit_doc))
            staged.append((stage / "audit.json", args.audit_out))
        if args.report:
            report_dir = Path(args.report).resolve().parent
            report = {
                "schema": REPORT_SCHEMA, "tool": dict(TOOL),
                "base": forge_core.manifest_path(args.base, report_dir), "base_sha256": base_info["sha256"],
                "placements": forge_core.manifest_path(args.placements, report_dir),
                "placements_sha256": forge_core.sha256_file(args.placements),
                "output": forge_core.manifest_path(args.output, report_dir), "output_sha256": preview_sha,
                "canvas_size": list(canvas.size), "scale": args.scale, "resampler": args.resampler,
                "sort": args.sort, "anchor_policy": args.anchor, "compositing_order": compositing_order(args.sort),
                "prop_packs": [forge_core.file_ref(path, report_dir) for path in packs.manifests.values()],
                "pasted": [report_entry(item, canvas.size, report_dir) for item in ordered],
                "warnings": warnings,
            }
            if audit is not None:
                report["audit_status"] = audit["status"]
            if pan_windows is not None:
                report["plate_pan"] = {"file": forge_core.manifest_path(args.plate_pan, report_dir),
                                       "viewport": list(args.pan_viewport), "zoom": args.pan_zoom,
                                       "gap_px": PAN_GAP_PX, "frames": pan_windows}
            (stage / "report.json").write_bytes(_json_bytes(_clean(report)))
            staged.append((stage / "report.json", args.report))
        publish_outputs(staged)

    summary: dict[str, Any] = {role.lstrip("-").replace("-", "_"): str(Path(path).resolve()) for role, path in outputs}
    summary.update(placed=len(ordered), warnings=len(warnings), audit_status=audit["status"] if audit else None)
    summary["_warnings"] = warnings
    return summary


def _input_refs(args: argparse.Namespace, placed: Sequence[Placed], packs: PropPackIndex,
                geometry: Geometry | None, base_dir: Path) -> list[dict[str, Any]]:
    paths = [args.base, args.placements, *(item.image_path for item in placed), *packs.manifests.values()]
    if geometry is not None:
        paths += geometry.inputs
    refs, seen = [], set()
    for path in paths:
        key = path_key(path)
        if key not in seen:
            seen.add(key)
            refs.append(forge_core.file_ref(Path(path), base_dir))
    return refs


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, type=Path, help="Base map image (world pixels).")
    parser.add_argument("--placements", required=True, type=Path, help="Placements JSON (placements_v2 or v1).")
    parser.add_argument("--output", required=True, type=Path, help="Composed preview PNG; must not exist.")
    parser.add_argument("--report", type=Path,
                        help="JSON report: draw order, anchors, sortY and anchor_world_error per placement.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd(),
                        help="Extra folder for relative image paths (default: the current directory).")
    parser.add_argument("--resampler", choices=RESAMPLERS, default="lanczos",
                        help="Resampler for resized placements and --scale (nearest for pixel art).")
    parser.add_argument("--sort", choices=SORT_MODES, default="ground-line",
                        help="ground-line (default): sortY or the ground line, ties by x then id. "
                             "raw-y: legacy rule, sortY or y, ties in file order.")
    parser.add_argument("--anchor", choices=ANCHOR_POLICIES, default="manifest",
                        help="manifest (default): placements without anchor/anchorPx use the prop-pack anchor_px. "
                             "px: legacy rule, anchorPx or the named box anchor only.")
    parser.add_argument("--prop-pack", type=Path, action="append",
                        help="prop-pack.json with anchors and footprints (repeatable; one beside an image or one "
                             "folder up is found automatically).")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="Scale the whole preview, base and placements (whole numbers with nearest).")
    parser.add_argument("--debug-overlay", type=Path,
                        help="PNG of the preview with walk regions, spawns, exits, approach points, collision, "
                             "footprints and masks drawn on top.")
    parser.add_argument("--bundle", type=Path, help="map_bundle.json with collision, portals, spawns, anchors, "
                                                    "interactions and material map.")
    parser.add_argument("--stage", type=Path, help="stage.json (UV ground polygons, slots, protected regions).")
    parser.add_argument("--mask", type=Path, action="append",
                        help="Mask image to tint in the overlay: alpha > 0, or luminance > 127 (repeatable).")
    parser.add_argument("--overlay-labels", choices=("actors", "all", "none"), default="actors",
                        help="Which placements get id labels in the overlay (default: actors).")
    parser.add_argument("--audit-out", type=Path,
                        help="Audit JSON (QA envelope): feet inside walkable areas, bounds and overlaps.")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 and write nothing when an audit check fails.")
    parser.add_argument("--plate-pan", type=Path, help="PNG contact sheet of a single-plate pan across the preview.")
    parser.add_argument("--pan-viewport", type=_size_arg, default=(960, 540), metavar="WxH",
                        help="Plate-pan frame size (default 960x540).")
    parser.add_argument("--pan-zoom", type=float, default=1.12, help="Plate-pan zoom, at least 1 (default 1.12).")
    parser.add_argument("--pan-frames", type=int, default=5, help="Plate-pan frames, u from 0 to 1 (default 5).")
    parser.add_argument("--pan-v", type=float, default=0.5, help="Plate-pan vertical focus 0..1 (default 0.5).")
    return parser


def _cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(args)
    except (ValueError, OSError, TypeError, KeyError, Image.DecompressionBombError) as error:
        print(f"error: {forge_core.ascii_text(str(error) or type(error).__name__)}", file=sys.stderr)
        return 1
    for warning in summary.pop("_warnings"):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27): usage errors exit 2, input errors print 'error: ...'
    and exit 1, and nothing else ever shows a traceback."""
    return forge_core.run_cli(_cli, argv)


if __name__ == "__main__":
    raise SystemExit(main())
