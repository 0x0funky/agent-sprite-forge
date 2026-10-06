#!/usr/bin/env python3
"""One approved master still: the step every character animation starts from.

  prompt    Write the base-still prompt (exactly ONE square 1024x1024 image on a
            flat key colour) from a spec (--spec JSON and/or flags): prompt.txt,
            spec.json and run.json in a new --output-dir.
  generate  The same, then --takes N candidates through generate2dmedia's
            route_media.py (API key first, then the local daemon). Exit 3 means
            no route: fall back to codeart2d (the prompt is still published).
  edit      Fix by edit: "Reproduce the FIRST image exactly ... THE ONLY CHANGE:
            ..." with the approved still as the FIRST reference, then generate
            (or --prompt-only for the host's own image tool).
  pad       Key a still with forge_matte, flatten its backdrop to the pure key,
            crop the subject with a 6 px margin and LANCZOS-scale it to the
            class framing on a 1024x1024 canvas (hero: 788 px tall, top margin
            138 px). The transform is recorded in pad.json.
  approve   Pad the chosen still and write master.png (opaque, on the pure key),
            master_rgba.png and master.json (generate2dsprite.master.v1): the
            record every motion step reads.

Every command writes a new --output-dir (staged beside it, never replacing
anything) and prints one ASCII JSON line. Errors print 'error: ...' and exit 1;
usage errors exit 2. The media CLI is found beside this skill
(../generate2dmedia/scripts/route_media.py); --media-cli overrides it, and
FORGE_ROUTE_MEDIA_FAKE=<script.py> substitutes a fake for tests (recorded in
run.json). Details: references/master-still.md.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_matte  # noqa: E402

TOOL_NAME = "master_still"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION
MASTER_SCHEMA = "generate2dsprite.master.v1"
SPEC_SCHEMA = "generate2dsprite.master_spec.v1"
RUN_SCHEMA = "generate2dsprite.master_run.v1"
PAD_SCHEMA = "generate2dsprite.master_pad.v1"

CANVAS = 1024                    # the master canvas is square
CROP_MARGIN_PX = 6               # game-opus55 pad: the subject is cropped with a 6 px margin
EDGE_SAFE_PX = 16                # a placed subject keeps this far from the canvas edge
MIN_PART_AREA = 24               # source px; smaller alpha islands are specks
GEOMETRY_ALPHA = forge_core.ALPHA_GEOMETRY_THRESHOLD  # alpha > 16 counts for geometry
DEFAULT_KEY = "#FF00FF"
NO_ROUTE_EXIT = 3
MEDIA_FAKE_ENV = "FORGE_ROUTE_MEDIA_FAKE"
DEFAULT_MEDIA_CLI = HERE.parent.parent / "generate2dmedia" / "scripts" / "route_media.py"
MAX_TAKES = 8
DEFAULT_TIMEOUT_S = 1800.0
ORDINALS = ("FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH", "SEVENTH", "EIGHTH")

FACINGS = ("left", "right", "front", "back", "none")
FINISHES = ("hd", "pixel")
PRONOUNS = {"he": "his", "she": "her", "it": "its", "they": "their"}
MATTES = ("soft", "dominance", "hard")
KEY_NAMES = {"#FF00FF": "magenta", "#00FF00": "green", "#0000FF": "blue"}
TEXT_BANS = ("text", "letters", "numbers", "logo", "watermark", "signature", "UI", "border", "frame", "grid")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SPEC_FIELDS = ("name", "subject", "identity", "pose", "recap", "facing", "facing_parts", "view", "class", "finish",
               "key", "genre", "pronoun", "identity_ref", "style_ref", "allowed_text", "extra_bans", "opaque_parts",
               "extra")
REF_FIELDS = {"identity_ref": ("path", "sha256", "what", "copy"),
              "style_ref": ("path", "sha256", "what", "do_not_copy")}
DEFAULT_KEEP = "costume, colours, silhouette, proportions, pose, held items and framing"
DEFAULT_EXTRA_ROLE = "an extra reference for THE ONLY CHANGE below: use it only for what the change names"


@dataclass(frozen=True)
class ClassPreset:
    """Framing of one character class on the 1024 canvas, from the game-opus55 pads.

    ``subject_height_px`` / ``top_margin_px`` / ``lead_px`` are the pad target (identical for every
    character of the class); ``prompt_top_pct`` / ``prompt_bottom_pct`` are what the prompt asks the
    generator for (it draws larger than asked; the pad normalises it)."""

    name: str
    subject_height_px: int
    top_margin_px: int
    lead_px: int
    prompt_top_pct: int
    prompt_bottom_pct: int
    noun: str
    evidence: str


CLASSES = {preset.name: preset for preset in (
    ClassPreset("hero", 788, 138, 0, 14, 14, "figure",
                "base_kanetsugu_pad / base_chiyome_pad: 788 px (77%) tall, top 138 px, centred"),
    ClassPreset("mob", 471, 406, 100, 35, 15, "figure",
                "base_ashigaru_pad / base_akazonae_pad: 471-476 px tall, feet line 877 px, body 100 px behind centre"),
    ClassPreset("boss", 728, 221, 0, 14, 8, "figure", "base_kage_pad: 728 px tall, top 221 px"),
    ClassPreset("prop", 614, 205, 0, 20, 20, "object", "centred object, 60% of the canvas height"),
)}


@dataclass(frozen=True)
class Style:
    line: str    # the style sentence; {for_genre} becomes " for <genre>" or nothing
    short: str   # the style named in an edit prompt
    traits: str  # the style traits repeated in an edit prompt
    word: str    # "match its <word> style" for the peer sprite


STYLES = {
    "pixel": Style(
        "premium 16-bit pixel art (SNES era){for_genre}, crisp visible chunky pixels, limited palette, strong dark "
        "outline, 3-tone cel shading, no smooth gradients, no blur",
        "premium 16-bit pixel-art style",
        "crisp visible chunky pixels, limited palette, strong dark outline, 3-tone cel shading, no smooth gradients, "
        "no blur",
        "pixel-art"),
    "hd": Style(
        "premium HD 2D game art{for_genre}: a clean, high-resolution hand-painted sprite illustration with crisp "
        "sharp edges, a clean dark outline, rich but controlled colours, cel shading with soft highlights and clear "
        "light and shadow shapes, fine readable detail, no blur, no photo-realism, no 3D render look",
        "premium HD 2D game-art style",
        "crisp sharp edges, a clean dark outline, rich but controlled colours, cel shading with soft highlights, fine "
        "readable detail, no blur, no photo-realism, no 3D render look",
        "art"),
}


# --------------------------------------------------------------------------- small helpers

_ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.I)
_PATHISH = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|~)")
_PATH_KEYS = frozenset({"artifact", "artifacts", "out_dir", "output", "output_dir", "path", "prompt_file", "job",
                        "metadata", "run", "files"})


def _ascii(text: Any) -> str:
    return forge_core.ascii_text(str(text))


def clean_text(value: Any, field: str) -> str | None:
    """Whitespace-collapsed text, or None when empty."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"spec field {field} must be a string; got {type(value).__name__}.")
    text = " ".join(value.split())
    return text or None


def strip_article(text: str) -> str:
    return _ARTICLE.sub("", text.strip(), count=1)


def strip_period(text: str) -> str:
    return text.strip().rstrip(".").rstrip()


def sentence(text: str) -> str:
    text = text.strip()
    return text if text[-1:] in ".!?" else text + "."


def capitalised(text: str) -> str:
    """The text as a sentence of its own: first letter upper case, final full stop."""
    text = sentence(text)
    return text[:1].upper() + text[1:]


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"needs a positive whole number; got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"needs a positive whole number; got {text!r}")
    return value


def normalise_key(text: Any) -> str:
    """``#RRGGBB`` (upper case) from a hex colour or a declared name; the key must be a chroma colour."""
    value = str(text).strip().lower()
    named = {name: hex_key for hex_key, name in KEY_NAMES.items()}
    if value in named:
        return named[value]
    digits = value[1:] if value.startswith("#") else value
    if not re.fullmatch(r"[0-9a-f]{6}", digits):
        raise ValueError(f"a key needs #RRGGBB, magenta, green or blue; got {text!r}")
    rgb = [int(digits[i:i + 2], 16) for i in (0, 2, 4)]
    if not any(c > 127 for c in rgb) or not any(c <= 127 for c in rgb):
        raise ValueError(f"key {text!r} is not a chroma colour: it needs strong and weak channels "
                         "(use magenta, green or blue)")
    return "#" + digits.upper()


def key_arg(text: str) -> str:
    try:
        return normalise_key(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def key_rgb(hex_key: str) -> tuple[int, int, int]:
    return tuple(int(hex_key[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def key_words(hex_key: str) -> str:
    """'magenta #FF00FF' for a declared key, else the hex colour."""
    name = KEY_NAMES.get(hex_key)
    return f"{name} {hex_key}" if name else hex_key


def key_name(hex_key: str) -> str:
    return KEY_NAMES.get(hex_key, "key-colour")


def probe_image(path: Path) -> tuple[int, int]:
    """(width, height) of an image file; ValueError when it is not one."""
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError(f"{Path(path).name} is not a readable image ({error})") from None


def final_path(output: Path) -> Path:
    output = Path(output)
    return output.parent.resolve() / output.name


def file_ref(path: Path, base: Path, *, size: bool = False) -> dict[str, Any]:
    ref = forge_core.file_ref(path, base)
    if size:
        ref["size"] = list(probe_image(path))
    return ref


# --------------------------------------------------------------------------- spec

def _ref_from_spec(value: Any, field: str, base: Path) -> dict[str, Any]:
    """A reference entry of a spec file: a path string or {path, ...}; relative paths use ``base``."""
    if isinstance(value, str):
        value = {"path": value}
    if not isinstance(value, dict):
        raise ValueError(f"spec field {field} must be a path or an object with a path.")
    unknown = sorted(set(value) - set(REF_FIELDS[field]))
    if unknown:
        raise ValueError(f"spec field {field} has unknown key(s) {', '.join(unknown)}; "
                         f"allowed: {', '.join(REF_FIELDS[field])}.")
    if not isinstance(value.get("path"), str) or not value["path"].strip():
        raise ValueError(f"spec field {field} needs a path.")
    ref = dict(value)
    path = Path(value["path"])
    ref["path"] = path if path.is_absolute() else base / path
    return ref


def load_spec_file(path: Path) -> dict[str, Any]:
    """Read a master spec (generate2dsprite.master_spec.v1); reference paths become absolute."""
    path = Path(path)
    try:
        data = forge_core.read_json(path)
    except ValueError as error:
        raise ValueError(f"{path.name} is not valid JSON ({error})") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must hold a JSON object.")
    data = dict(data)
    schema = data.pop("schema", SPEC_SCHEMA)
    if schema != SPEC_SCHEMA:
        raise ValueError(f"{path.name} has schema {schema!r}; expected {SPEC_SCHEMA}.")
    unknown = sorted(set(data) - set(SPEC_FIELDS))
    if unknown:
        raise ValueError(f"{path.name} has unknown spec field(s) {', '.join(unknown)}; "
                         f"allowed: {', '.join(SPEC_FIELDS)}.")
    base = path.resolve().parent
    for field in REF_FIELDS:
        if data.get(field) is not None:
            data[field] = _ref_from_spec(data[field], field, base)
    return data


def _read_text_file(path: str, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{what} {Path(path).name} is not UTF-8 text.") from None


def merge_flags(base: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Overlay the spec flags on ``base`` (flags win; --ban adds to the spec's bans)."""
    spec = dict(base)
    simple = {"name": args.name, "subject": args.subject, "pose": args.pose, "recap": args.recap,
              "facing": args.facing, "facing_parts": args.facing_parts, "view": args.view, "class": args.klass,
              "finish": args.finish, "key": args.key, "genre": args.genre, "pronoun": args.pronoun,
              "allowed_text": args.allowed_text, "opaque_parts": args.opaque_parts, "extra": args.extra}
    for field, value in simple.items():
        if value is not None:
            spec[field] = value
    if args.identity is not None and args.identity_file:
        raise ValueError("pass --identity or --identity-file, not both.")
    if args.identity_file:
        spec["identity"] = _read_text_file(args.identity_file, "--identity-file")
    elif args.identity is not None:
        spec["identity"] = args.identity
    for field, path, what, other, other_key in (
            ("identity_ref", args.identity_ref, args.identity_ref_what, args.identity_copy, "copy"),
            ("style_ref", args.style_ref, args.style_ref_what, args.style_dont_copy, "do_not_copy")):
        ref = dict(spec.get(field) or {})
        if path:
            ref = {key: value for key, value in ref.items() if key != "sha256"}
            ref["path"] = Path(path).resolve()
        if what is not None or other is not None:
            if "path" not in ref:
                flag = "--identity-ref" if field == "identity_ref" else "--style-ref"
                raise ValueError(f"{flag} is needed before its description flags.")
            if what is not None:
                ref["what"] = what
            if other is not None:
                ref[other_key] = other
        if ref:
            spec[field] = ref
    if args.ban:
        spec["extra_bans"] = [*(spec.get("extra_bans") or []), *args.ban]
    return spec


def _normalise_ref(ref: dict[str, Any], field: str, *, attach: bool, warnings: list[str]) -> dict[str, Any]:
    path = Path(ref["path"])
    label = "identity reference" if field == "identity_ref" else "style reference"
    out: dict[str, Any] = {"path": path}
    for key in REF_FIELDS[field][2:]:
        value = ref.get(key)
        if key == "do_not_copy" and isinstance(value, str) and not value.strip():
            out[key] = ""  # an explicit empty value drops the "do NOT copy" clause
            continue
        text = clean_text(value, f"{field}.{key}")
        if text is not None:
            out[key] = text
    if not path.is_file():
        if attach:
            raise ValueError(f"The {label} {path.as_posix()} does not exist.")
        warnings.append(f"the {label} {path.name} is missing; its recorded sha256 is kept")
        if isinstance(ref.get("sha256"), str):
            out["sha256"] = ref["sha256"]
        return out
    probe_image(path)
    digest = forge_core.sha256_file(path)
    recorded = ref.get("sha256")
    if isinstance(recorded, str) and recorded.lower() != digest:
        warnings.append(f"the {label} {path.name} changed since the spec was written (sha256 differs)")
    out["sha256"] = digest
    return out


def normalise_spec(raw: dict[str, Any], *, attach: bool) -> tuple[dict[str, Any], list[str]]:
    """Validate a merged spec and fill the defaults. ``attach``: references must exist (they are sent)."""
    warnings: list[str] = []
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name.strip()):
        raise ValueError("--name is required: 1-64 letters, digits, '.', '_' or '-', starting with a letter or "
                         f"digit; got {name!r}.")
    klass = str(raw.get("class") or "hero").strip().lower()
    if klass not in CLASSES:
        raise ValueError(f"class must be one of {', '.join(CLASSES)}; got {klass!r}.")
    finish = str(raw.get("finish") or "hd").strip().lower()
    if finish not in FINISHES:
        raise ValueError(f"finish must be hd or pixel; got {finish!r}.")
    facing = str(raw.get("facing") or ("none" if klass == "prop" else "right")).strip().lower()
    if facing not in FACINGS:
        raise ValueError(f"facing must be one of {', '.join(FACINGS)}; got {facing!r}.")
    pronoun = str(raw.get("pronoun") or "they").strip().lower()
    if pronoun not in PRONOUNS:
        raise ValueError(f"pronoun must be one of {', '.join(PRONOUNS)}; got {pronoun!r}.")
    spec: dict[str, Any] = {"name": name.strip(), "class": klass, "finish": finish, "facing": facing,
                            "key": normalise_key(raw.get("key") or DEFAULT_KEY), "pronoun": pronoun}
    for field in ("subject", "identity", "pose", "recap", "facing_parts", "view", "genre", "allowed_text",
                  "opaque_parts", "extra"):
        spec[field] = clean_text(raw.get(field), field)
    if not spec["subject"]:
        raise ValueError("--subject is required: a short noun phrase such as 'a young fox ranger, full body'.")
    if not spec["identity"]:
        raise ValueError("--identity (or --identity-file) is required: a long, precise visual description "
                         "(face, hair, body, costume, colours, held items).")
    bans = raw.get("extra_bans") or []
    if isinstance(bans, str) or not isinstance(bans, list):
        raise ValueError("spec field extra_bans must be a list of strings.")
    spec["extra_bans"] = []
    for item in bans:
        text = clean_text(item, "extra_bans")
        if text:
            spec["extra_bans"].append(re.sub(r"^no\s+", "", strip_period(text), flags=re.I))
    for field in REF_FIELDS:
        ref = raw.get(field)
        spec[field] = _normalise_ref(ref, field, attach=attach, warnings=warnings) if ref else None
    return spec, warnings


def spec_document(spec: dict[str, Any], base: Path) -> dict[str, Any]:
    """The spec as written to spec.json in ``base``: reference paths relative to it, with sha256."""
    doc: dict[str, Any] = {"schema": SPEC_SCHEMA}
    for field in SPEC_FIELDS:
        value = spec.get(field)
        if field in REF_FIELDS and value:
            ref: dict[str, Any] = {"path": forge_core.manifest_path(value["path"], base)}
            if value.get("sha256"):
                ref["sha256"] = value["sha256"]
            for key in REF_FIELDS[field][2:]:
                if key in value:
                    ref[key] = value[key]
            doc[field] = ref
        else:
            doc[field] = value
    return doc


def attached_references(spec: dict[str, Any]) -> list[tuple[str, Path]]:
    """The base-still references in attachment order: identity (FIRST), then the peer sprite."""
    refs = []
    for role, field in (("identity", "identity_ref"), ("style", "style_ref")):
        if spec.get(field):
            refs.append((role, Path(spec[field]["path"])))
    return refs


def identity_recap(spec: dict[str, Any]) -> str:
    """The noun phrase motion prompts reuse after 'The same': the spec's recap, else subject (identity)."""
    recap = spec.get("recap")
    if not recap:
        recap = f"{strip_period(strip_article(spec['subject']))} ({strip_period(spec['identity'])})"
    return strip_period(strip_article(recap))


# --------------------------------------------------------------------------- prompts

def default_view(spec: dict[str, Any]) -> str:
    if spec.get("view"):
        return spec["view"]
    if spec["class"] == "prop":
        return "three-quarter view"
    return {"left": "side view (profile to three-quarter)", "right": "side view (profile to three-quarter)",
            "front": "front view", "back": "back view", "none": "three-quarter view"}[spec["facing"]]


def facing_clause(spec: dict[str, Any]) -> str:
    """The facing stated three ways: the FACING label, the parts turned to an edge, forward and back."""
    pos = PRONOUNS[spec["pronoun"]]
    facing = spec["facing"]
    parts = spec.get("facing_parts") or ("front" if spec["class"] == "prop" else "face, gaze and chest")
    verb = "turn" if ("," in parts or " and " in parts) else "turns"
    if facing in ("left", "right"):
        side, other = facing.upper(), "RIGHT" if facing == "left" else "LEFT"
        return (f"clearly FACING {side}: {pos} {parts} {verb} toward the {side} edge of the image; forward is "
                f"{side} and {pos} back is toward the {other} edge.")
    if facing == "front":
        return (f"clearly FACING THE VIEWER: {pos} {parts} {verb} straight toward the viewer; forward is out of the "
                f"picture and {pos} back is never visible.")
    if facing == "back":
        return (f"clearly FACING AWAY from the viewer: {pos} back and the back of {pos} head turn toward the "
                f"viewer; forward is into the picture and {pos} face is not visible.")
    return ""


def view_and_facing(spec: dict[str, Any]) -> str:
    view = default_view(spec)
    return {"left": f"{view}, facing LEFT", "right": f"{view}, facing RIGHT", "front": f"{view}, facing the viewer",
            "back": f"{view}, facing away from the viewer", "none": view}[spec["facing"]]


def ban_sentence(spec: dict[str, Any]) -> str:
    text = "No " + ", no ".join([*TEXT_BANS, *spec.get("extra_bans", [])]) + "."
    if spec.get("allowed_text"):
        text += (f" The ONLY written character allowed anywhere is {strip_period(spec['allowed_text'])}; nothing "
                 "else carries any character, letter or symbol.")
    return text


def background_sentence(spec: dict[str, Any]) -> str:
    place = "object" if CLASSES[spec["class"]].noun == "object" else "character"
    return (f"Background: solid flat pure {key_words(spec['key'])} everywhere outside the {place}, no shadow, no "
            "ground, no glow on the background.")


def reference_paragraph(spec: dict[str, Any]) -> str | None:
    """Reference roles: FIRST = identity (copy exactly), SECOND = approved peer sprite (style only)."""
    ident, peer = spec.get("identity_ref"), spec.get("style_ref")
    if not ident and not peer:
        return None
    pos = PRONOUNS[spec["pronoun"]]
    parts = []
    if ident:
        what = ident.get("what") or "an image of this exact character"
        copy = ident.get("copy") or f"{pos} face, hair, body shape, costume, colours, markings and equipment"
        lead = "the FIRST attached image" if peer else "the attached image"
        parts.append(f"{lead} is {strip_period(what)}; copy {strip_period(copy)} exactly.")
    if peer:
        lead = "The SECOND attached image" if ident else "the attached image"
        what = peer.get("what")
        body = (f"is an approved in-game sprite from the same game ({strip_period(what)})" if what
                else "is an approved in-game sprite of another character from the same game")
        text = (f"{lead} {body}: match its {STYLES[spec['finish']].word} style, outline weight, shading, figure size "
                "and position on the canvas")
        dont = peer.get("do_not_copy")
        if dont is None:
            dont = "that character's face, hair, outfit, equipment or colours"
        parts.append(text + (f"; do NOT copy {strip_period(dont)}." if dont else "."))
    return ("Reference images: " if ident and peer else "Reference image: ") + " ".join(parts)


def subject_paragraph(spec: dict[str, Any]) -> str:
    clause = facing_clause(spec)
    head = f"Subject: {strip_period(spec['subject'])}, {default_view(spec)}"
    text = f"{head}, {clause}" if clause else f"{head}."
    text += f" {capitalised(spec['identity'])}"
    if spec.get("pose"):
        text += f" {capitalised(spec['pose'])}"
    return text


def size_paragraph(spec: dict[str, Any]) -> str:
    """Framing as canvas-edge percentages, nothing touching an edge, solid opaque pixels."""
    preset = CLASSES[spec["class"]]
    noun = preset.noun
    top, bottom = preset.prompt_top_pct, preset.prompt_bottom_pct
    extent = ("including hair, headgear, raised weapons and effects" if noun == "figure"
              else "including every attached part")
    text = (f"Size and position: one single {noun}, the top of the {noun} ({extent}) about {top} percent below the "
            f"top edge of the canvas and its lowest point about {bottom} percent above the bottom edge (the {noun} "
            f"about {100 - top - bottom} percent of the canvas height), ")
    facing = spec["facing"]
    if preset.lead_px and facing in ("left", "right"):
        behind = "right" if facing == "left" else "left"
        text += (f"the body a little {behind} of centre so there is extra empty room on the {facing.upper()} side "
                 "for forward motion, ")
    else:
        text += "centred horizontally, "
    text += (f"generous empty {key_name(spec['key'])} margin on every side, nothing touching or crossing the edges. "
             f"The whole {noun}")
    if spec.get("opaque_parts"):
        text += f" ({strip_period(spec['opaque_parts'])})"
    return text + (" is drawn with solid, fully opaque pixels: no transparency, no semi-transparent glow, no soft "
                   "aura, nothing blended with the background.")


def build_prompt(spec: dict[str, Any]) -> str:
    """The base-still prompt of the game-opus55 recipe (base_kanetsugu.txt), for one square image."""
    style = STYLES[spec["finish"]]
    for_genre = f" for {strip_period(spec['genre'])}" if spec.get("genre") else ""
    paragraphs = [f"Create exactly ONE square image ({CANVAS}x{CANVAS}).",
                  f"Style: {style.line.format(for_genre=for_genre)}. {ban_sentence(spec)}"]
    references = reference_paragraph(spec)
    if references:
        paragraphs.append(references)
    paragraphs += [subject_paragraph(spec), size_paragraph(spec)]
    if spec.get("extra"):
        paragraphs.append(capitalised(spec["extra"]))
    paragraphs.append(background_sentence(spec))
    return "\n\n".join(paragraphs) + "\n"


def build_edit_prompt(spec: dict[str, Any], *, change: str, keep: str | None, roles: Sequence[str],
                      framing: str | None) -> str:
    """Fix by edit (base_nobu_v2a.txt): reproduce the FIRST image exactly, with THE ONLY CHANGE."""
    preset = CLASSES[spec["class"]]
    style = STYLES[spec["finish"]]
    lines = [f"The FIRST attached image is the approved master still on a flat {key_words(spec['key'])} background: "
             f"{strip_period(spec['subject'])}."]
    for index, role in enumerate(roles, start=1):
        lines.append(f"The {ORDINALS[index]} attached image is {strip_period(role)}.")
    position = f" ({framing})" if framing else ""
    reproduce = (f"Reproduce the FIRST image exactly: the same single {preset.noun} at exactly the same size and the "
                 f"same position on the canvas{position}, the same pose, {view_and_facing(spec)}, the same "
                 f"{identity_recap(spec)}. Same {style.short} as the FIRST image: {style.traits}.")
    only = (f"THE ONLY CHANGE: {sentence(change)} Everything else ({strip_period(keep or DEFAULT_KEEP)}) stays "
            "exactly as in the FIRST image.")
    return "\n\n".join([f"Create exactly ONE square image ({CANVAS}x{CANVAS}).", " ".join(lines), reproduce, only,
                        f"{background_sentence(spec)} {ban_sentence(spec)}"]) + "\n"


# --------------------------------------------------------------------------- pad

@dataclass(frozen=True)
class Framing:
    canvas: int
    subject_height_px: int
    top_margin_px: int
    lead_px: int
    preset: str

    def record(self) -> dict[str, Any]:
        return {"canvas": [self.canvas, self.canvas], "subject_height_px": self.subject_height_px,
                "top_margin_px": self.top_margin_px, "lead_px": self.lead_px, "preset": self.preset}


def framing_for(klass: str, args: argparse.Namespace | None = None) -> Framing:
    """The class preset, with --subject-height / --top-margin / --lead overrides (preset 'custom')."""
    preset = CLASSES[klass]
    height = getattr(args, "subject_height", None)
    top = getattr(args, "top_margin", None)
    lead = getattr(args, "lead", None)
    custom = any(value is not None for value in (height, top, lead))
    framing = Framing(CANVAS, preset.subject_height_px if height is None else height,
                      preset.top_margin_px if top is None else top, preset.lead_px if lead is None else lead,
                      "custom" if custom else preset.name)
    if not 64 <= framing.subject_height_px <= CANVAS - 2 * EDGE_SAFE_PX:
        raise ValueError(f"--subject-height must be 64-{CANVAS - 2 * EDGE_SAFE_PX} px; "
                         f"got {framing.subject_height_px}.")
    if framing.top_margin_px < 0 or framing.top_margin_px + framing.subject_height_px > CANVAS:
        raise ValueError(f"--top-margin {framing.top_margin_px} with a {framing.subject_height_px} px subject leaves "
                         f"the {CANVAS} px canvas.")
    if not 0 <= framing.lead_px <= CANVAS // 4:
        raise ValueError(f"--lead must be 0-{CANVAS // 4} px; got {framing.lead_px}.")
    return framing


def key_subject(pixels: np.ndarray, key_hex: str, matte: str) -> tuple[np.ndarray, dict[str, Any]]:
    """Key a still with forge_matte against its estimated backdrop; native alpha is kept as it is."""
    declared = KEY_NAMES.get(key_hex) or key_hex.lower()
    estimate_rgb, estimate = forge_matte.estimate_key(pixels, declared)
    record: dict[str, Any] = {"requested": matte, "key_estimate": {
        name: estimate.get(name) for name in ("key", "source", "valid", "ring_share", "coverage",
                                              "distance_from_declared")}}
    if estimate["use_native_alpha"]:  # real transparency and no key backdrop: keep the image's own alpha
        keyed = np.array(pixels, dtype=np.uint8)
        record.update(quality="native_alpha", qa=forge_matte.matte_qa(keyed, key_rgb(key_hex)))
        return keyed, record
    if not estimate["valid"]:
        raise ValueError(f"The still has no flat {key_words(key_hex)} backdrop ({estimate['reason']}); "
                         "regenerate it on the flat key or pass the right --key.")
    keyed_image, info = forge_matte.key_still(pixels, quality=matte, key=[float(v) for v in estimate_rgb])
    keyed = np.array(keyed_image.convert("RGBA"), dtype=np.uint8)
    qa = info.get("qa") or {}
    record.update(quality=info["quality"], interior_despill=info.get("interior_despill"),
                  qa={name: qa[name] for name in ("opaque_key_px", "outer_ring_spill_fraction",
                                                   "semitransparent_fraction", "enclosed_key_pockets",
                                                   "key_hued_px", "visible_px") if name in qa})
    return keyed, record


def subject_parts(alpha: np.ndarray, min_area: int) -> dict[str, Any]:
    """The subject box: every 8-connected part with at least ``min_area`` px of alpha > 16."""
    components = forge_core.connected_components(alpha, threshold=GEOMETRY_ALPHA)
    kept = [part for part in components if part["area"] >= min_area]
    if not kept:
        raise ValueError(f"No subject found: no keyed part has {min_area} px or more.")
    x0 = min(part["bbox"][0] for part in kept)
    y0 = min(part["bbox"][1] for part in kept)
    x1 = max(part["bbox"][2] for part in kept)
    y1 = max(part["bbox"][3] for part in kept)
    height, width = alpha.shape
    touches = [side for side, hit in (("left", x0 == 0), ("top", y0 == 0), ("right", x1 == width),
                                      ("bottom", y1 == height)) if hit]
    return {"bbox": [x0, y0, x1, y1], "parts": len(kept), "ignored_parts": len(components) - len(kept),
            "ignored_px": int(sum(part["area"] for part in components if part["area"] < min_area)),
            "touches": touches}


def composite_on_key(rgba: np.ndarray, rgb: Sequence[int]) -> np.ndarray:
    """Straight-alpha 'over' onto the flat key; outside the subject every pixel is exactly the key."""
    alpha = rgba[..., 3:].astype(np.float64) / 255.0
    mixed = rgba[..., :3].astype(np.float64) * alpha + np.asarray(rgb, np.float64) * (1.0 - alpha)
    return np.floor(mixed + 0.5).astype(np.uint8)


def pad_still(pixels: np.ndarray, *, key_hex: str, framing: Framing, facing: str, matte: str = "soft",
              crop_margin: int = CROP_MARGIN_PX, min_part_area: int = MIN_PART_AREA) -> dict[str, Any]:
    """Key, find, crop (margin), LANCZOS-place on the canvas at the class framing, flatten onto the pure key.

    Returns ``padded`` (opaque RGB), ``cutout`` (RGBA), ``framing``, ``transform``, ``matte``,
    ``key_choice`` and ``warnings``. The transform maps source to master: ``master = scale * source +
    offset`` in continuous pixel coordinates.
    """
    warnings: list[str] = []
    keyed, matte_record = key_subject(pixels, key_hex, matte)
    height, width = keyed.shape[:2]
    parts = subject_parts(keyed[..., 3], min_part_area)
    x0, y0, x1, y1 = parts["bbox"]
    box_w, box_h = x1 - x0, y1 - y0
    if box_h < 16 or box_w < 4:
        raise ValueError(f"The subject is too small to pad: {box_w}x{box_h} px.")
    if box_w * box_h >= 0.98 * width * height:
        raise ValueError("The keyed subject fills the whole still: the backdrop was not keyed. Pass the right --key.")
    canvas = framing.canvas
    scale = framing.subject_height_px / box_h
    width_limited = box_w * scale > canvas - 2 * EDGE_SAFE_PX
    if width_limited:
        scale = (canvas - 2 * EDGE_SAFE_PX) / box_w
    sign = {"left": 1.0, "right": -1.0}.get(facing, 0.0)
    half = box_w * scale / 2.0
    centre = canvas / 2.0 + sign * framing.lead_px
    centre = min(max(centre, EDGE_SAFE_PX + half), canvas - EDGE_SAFE_PX - half)
    offset = (centre - scale * (x0 + x1) / 2.0, framing.top_margin_px - scale * y0)
    crop = [max(0, x0 - crop_margin), max(0, y0 - crop_margin), min(width, x1 + crop_margin),
            min(height, y1 + crop_margin)]
    region = np.ascontiguousarray(keyed[crop[1]:crop[3], crop[0]:crop[2]])
    placed = forge_core.resample_rgba(region, scale, "lanczos", anchor_src=(0.0, 0.0),
                                      anchor_dst=(scale * crop[0] + offset[0], scale * crop[1] + offset[1]),
                                      out_size=(canvas, canvas))
    placed, hygiene = forge_core.alpha_hygiene(placed, mode="floor")
    cutout = np.array(placed, dtype=np.uint8)
    padded = composite_on_key(cutout, key_rgb(key_hex))
    measured = forge_core.subject_bbox(cutout[..., 3])
    if measured is None:
        raise ValueError("The placed subject is empty; check --key and --min-part-area.")
    measured_h = measured[3] - measured[1]
    anchor = forge_core.anchor_from_mask(forge_core.subject_mask(cutout[..., 3]), "stance",
                                         band_rows=max(2, forge_core.round_half_up(0.04 * measured_h)))
    if parts["touches"]:
        warnings.append(f"the subject touches the {', '.join(parts['touches'])} edge of the still and may be cut "
                        "off: regenerate or edit it with more margin")
    if scale > 1.0 + 1e-9:
        warnings.append(f"the subject is upscaled x{scale:.2f} ({box_h} px source height): detail will be soft; "
                        "ask the route for a larger figure")
    if width_limited:
        warnings.append(f"the subject is too wide for the {framing.preset} framing: scaled to fit the width, "
                        f"{measured_h} px tall instead of {framing.subject_height_px} px")
    elif abs(measured_h - framing.subject_height_px) > 3 or abs(measured[1] - framing.top_margin_px) > 3:
        warnings.append(f"the placed subject measures {measured_h} px tall at top {measured[1]} px (target "
                        f"{framing.subject_height_px} px at {framing.top_margin_px} px)")
    choice = forge_matte.choose_key_color(keyed, (key_hex.lower(),))
    row = choice["candidates"][0]
    key_choice = {"status": choice["status"], "overlap_share": round(float(row["overlap_share"]), 6),
                  "rule": choice["rule"]}
    if row["rejected"]:
        warnings.append(f"{row['overlap_share']:.1%} of the subject's colours lean to the key {key_hex} (more than "
                        "0.5%): check that those parts survive keying; for purple or pink designs use --key green "
                        "or blue")
    if matte_record.get("qa", {}).get("opaque_key_px", 0) > 0:
        warnings.append(f"{matte_record['qa']['opaque_key_px']} opaque key-coloured pixels remain in the subject: "
                        "check the master over a dark background")
    framing_record = {**framing.record(), "subject_bbox": [int(v) for v in measured],
                      "anchor": [float(anchor[0]), float(anchor[1])],
                      "opaque_area_px": int((cutout[..., 3] >= 128).sum()), "width_limited": bool(width_limited)}
    transform = {
        "mapping": "master = scale * source + offset (continuous pixel coordinates: pixel i spans [i, i + 1))",
        "source_size": [int(width), int(height)],
        "source_subject_bbox": [int(v) for v in parts["bbox"]],
        "crop_box": [int(v) for v in crop],
        "crop_margin_px": int(crop_margin),
        "scale": float(scale),
        "offset": [float(offset[0]), float(offset[1])],
        "canvas": [canvas, canvas],
        "resampler": "box" if scale <= 0.5 else "lanczos",
        "premultiplied": True,
        "alpha_floor_px": int(hygiene["floor_px"]),
        "subject_parts": parts["parts"],
        "ignored_parts": parts["ignored_parts"],
        "ignored_px": parts["ignored_px"],
        "source_touches": parts["touches"],
        "key_rgb": list(key_rgb(key_hex)),
    }
    return {"padded": padded, "cutout": cutout, "framing": framing_record, "transform": transform,
            "matte": matte_record, "key_choice": key_choice, "warnings": warnings}


def describe_framing(path: Path, key_hex: str, noun: str) -> str | None:
    """The FIRST image's framing as canvas-edge percentages (for an edit), or None when it cannot be keyed."""
    try:
        image, _ = forge_core.load_rgba(path)
        keyed, _ = key_subject(np.asarray(image), key_hex, "soft")
        x0, y0, x1, y1 = subject_parts(keyed[..., 3], MIN_PART_AREA)["bbox"]
    except ValueError:
        return None
    height, width = keyed.shape[:2]

    def pct(value: float, total: int) -> int:
        return forge_core.round_half_up(100.0 * value / total)

    return (f"the top of the {noun} about {pct(y0, height)} percent below the top edge, its lowest point about "
            f"{pct(height - y1, height)} percent above the bottom edge, its left-most point about {pct(x0, width)} "
            f"percent from the left edge and its right-most point about {pct(width - x1, width)} percent from the "
            "right edge")


# --------------------------------------------------------------------------- media CLI

@dataclass(frozen=True)
class MediaCli:
    path: Path
    source: str
    route: str | None
    args: tuple[str, ...]
    timeout: float | None

    def record(self) -> dict[str, Any]:
        return {"cli": self.path.name, "cli_source": self.source, "cli_sha256": forge_core.sha256_file(self.path),
                "route_requested": self.route, "size": f"{CANVAS}x{CANVAS}", "args": list(self.args),
                "timeout_s": self.timeout}


def locate_media_cli(explicit: str | None) -> tuple[Path, str]:
    """--media-cli, else FORGE_ROUTE_MEDIA_FAKE (a .py script), else route_media.py beside this skill."""
    if explicit:
        path, source = Path(explicit), "--media-cli"
    else:
        fake = os.environ.get(MEDIA_FAKE_ENV, "").strip()
        if fake.lower().endswith(".py"):
            path, source = Path(fake), MEDIA_FAKE_ENV
        else:
            path, source = DEFAULT_MEDIA_CLI, "sibling"
    if not path.is_file():
        if source == "sibling":
            raise ValueError(f"The media CLI is missing: {path.as_posix()} (generate2dmedia route_media.py). Install "
                             "the generate2dmedia skill beside this one, pass --media-cli, or use 'prompt' and "
                             "generate with the host's own image tool.")
        raise ValueError(f"{source} names a missing file: {path}")
    return path.resolve(), source


def media_from_args(args: argparse.Namespace) -> MediaCli:
    path, source = locate_media_cli(args.media_cli)
    return MediaCli(path, source, args.route, tuple(args.media_arg or ()), args.timeout)


def parse_summary(stdout: str) -> dict[str, Any] | None:
    """The last JSON object line of the media CLI's stdout."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def sanitise_summary(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """Scalar fields of the media summary without local paths (manifests never store absolute paths)."""
    if not isinstance(summary, dict):
        return None
    clean: dict[str, Any] = {}
    for key, value in summary.items():
        if key in _PATH_KEYS:
            continue
        if value is None or isinstance(value, bool) or isinstance(value, int):
            clean[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            clean[key] = value
        elif isinstance(value, str) and len(value) <= 500 and not _PATHISH.match(value):
            clean[key] = redact(value)
    return clean


_SECRET = re.compile(r"\b(?:sk|xai|key)-[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I)


def redact(text: str) -> str:
    """Hide anything shaped like an API key or bearer token (keys are never printed or stored)."""
    return _SECRET.sub("[redacted]", text)


def _run_child(argv: list[str], timeout: float | None, label: str) -> tuple[int, str, list[str], bool]:
    """Run the media CLI: stdout captured line by line, stderr echoed live (prefixed, redacted) and kept."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env)
    out_lines: list[str] = []
    err_lines: list[str] = []

    def read_out() -> None:
        try:  # line by line: the summary line is kept even if a grandchild holds the pipe open
            for raw in iter(process.stdout.readline, b""):
                out_lines.append(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass

    def read_err() -> None:
        try:
            for raw in iter(process.stderr.readline, b""):
                text = redact(raw.decode("utf-8", "replace").rstrip("\r\n"))
                err_lines.append(text)
                print(_ascii(f"[{label}] {text}"), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass

    threads = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        code = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    for thread in threads:
        thread.join(timeout=10)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    return code, "".join(out_lines), err_lines, timed_out


def _last_error(lines: Sequence[str]) -> str | None:
    for line in reversed(lines):
        if line.strip().lower().startswith("error:"):
            return line.strip()[6:].strip()
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return None


def call_media(media: MediaCli, *, prompt_file: Path, references: Sequence[Path], out_dir: Path, take: int,
               stage: Path) -> dict[str, Any]:
    """One take: route_media.py image ... --out-dir <take>; its one-line JSON decides the result."""
    argv = [sys.executable, str(media.path), "image", "--prompt-file", str(prompt_file)]
    for reference in references:
        argv += ["--reference", str(reference)]
    argv += ["--size", f"{CANVAS}x{CANVAS}", "--out-dir", str(out_dir)]
    if media.route:
        argv += ["--route", media.route]
    argv += list(media.args)
    started = time.monotonic()
    code, stdout, err_lines, timed_out = _run_child(argv, media.timeout, f"take {take}")
    summary = parse_summary(stdout)
    for line in stdout.splitlines():
        if line.strip() and not line.strip().startswith("{"):
            print(_ascii(f"[take {take}] {redact(line.strip())}"), file=sys.stderr, flush=True)
    record: dict[str, Any] = {"take": take, "exit_code": code, "seconds": round(time.monotonic() - started, 3)}
    if timed_out:
        record.update(status="fail", error=f"the media CLI did not finish within {media.timeout:g} s")
        return record
    status = (summary or {}).get("status")
    record["media"] = sanitise_summary(summary)
    if code == NO_ROUTE_EXIT or status == "no-route":
        record.update(status="no-route", fallback=str((summary or {}).get("fallback") or "codeart2d"))
        return record
    if code != 0 or status != "ok":
        error = (summary or {}).get("error") or _last_error(err_lines) or f"exit {code}, status {status!r}"
        record.update(status="fail", error=redact(str(error)))
        return record
    artifact = summary.get("artifact")
    if not isinstance(artifact, str) or not artifact.strip():
        record.update(status="fail", error="the media CLI reported ok without an artifact path")
        return record
    path = Path(artifact)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        record.update(status="fail", error=f"the reported artifact does not exist: {artifact}")
        return record
    try:
        path.resolve().relative_to(out_dir.resolve())
        record["copied"] = False
    except ValueError:  # keep the take self-contained
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / ("generated" + (path.suffix.lower() or ".png"))
        index = 2
        while target.exists():
            target = out_dir / f"generated-{index}{path.suffix.lower() or '.png'}"
            index += 1
        shutil.copyfile(path, target)
        path, record["copied"] = target, True
    try:
        record["artifact"] = file_ref(path, stage, size=True)
    except ValueError as error:
        record.update(status="fail", error=str(error))
        return record
    record.update(status="ok", route=str(summary.get("route") or media.route or "unknown"))
    return record


def run_takes(stage: Path, count: int, *, prompt_file: Path, references: Sequence[Path],
              media: MediaCli) -> list[dict[str, Any]]:
    takes_dir = stage / "takes"
    takes_dir.mkdir()
    records = []
    for take in range(1, count + 1):
        record = call_media(media, prompt_file=prompt_file, references=references,
                            out_dir=takes_dir / f"take-{take:02d}", take=take, stage=stage)
        records.append(record)
        if record["status"] == "no-route":
            break  # no route for this take means none for the next
    return records


def takes_status(records: Sequence[dict[str, Any]], count: int) -> tuple[str, str | None]:
    ok = sum(1 for record in records if record["status"] == "ok")
    if ok == count:
        return "ok", None
    if ok:
        return "partial", None
    if records and records[0]["status"] == "no-route":
        return "no-route", records[0].get("fallback") or "codeart2d"
    return "fail", None


def contact_sheet(items: Sequence[tuple[Path, str]], cell: int = 320) -> Image.Image:
    """Thumbnails of the takes side by side, labelled, for choosing one at a glance."""
    columns = min(4, len(items))
    rows = math.ceil(len(items) / columns)
    sheet = Image.new("RGB", (columns * cell, rows * (cell + 20)), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    for index, (path, label) in enumerate(items):
        with Image.open(path) as source:
            thumb = source.convert("RGB")
        thumb.thumbnail((cell, cell), Image.Resampling.LANCZOS)
        column, row = index % columns, index // columns
        left = column * cell + (cell - thumb.width) // 2
        sheet.paste(thumb, (left, row * (cell + 20) + 20 + (cell - thumb.height) // 2))
        draw.text((column * cell + 6, row * (cell + 20) + 4), label, fill=(255, 255, 255))
    return sheet


# --------------------------------------------------------------------------- documents

def run_document(command: str, spec: dict[str, Any], stage: Path, *, references: Sequence[tuple[str, Path]],
                 media: MediaCli | None, takes: list[dict[str, Any]], status: str, warnings: list[str],
                 fallback: str | None = None, edit: dict[str, Any] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "command": command,
        "created": forge_core.utc_timestamp(),
        "name": spec["name"], "class": spec["class"], "finish": spec["finish"], "facing": spec["facing"],
        "key": spec["key"],
        "prompt": forge_core.file_ref(stage / "prompt.txt", stage),
        "spec": forge_core.file_ref(stage / "spec.json", stage),
        "references": [{"role": role, "ordinal": ORDINALS[index], **forge_core.file_ref(path, stage)}
                       for index, (role, path) in enumerate(references)],
        "media": media.record() if media else None,
        "takes": takes,
        "status": status,
    }
    if fallback:
        doc["fallback"] = fallback
    if edit:
        doc["edit"] = edit
    doc["warnings"] = list(warnings)
    return doc


def load_run(path: str | Path) -> tuple[Path, dict[str, Any]]:
    path = Path(path)
    run_file = path / "run.json" if path.is_dir() else path
    if not run_file.is_file():
        raise ValueError(f"No run.json in {path.as_posix()}; pass the output folder of prompt, generate or edit.")
    doc = forge_core.read_json(run_file)
    if not isinstance(doc, dict) or doc.get("schema") != RUN_SCHEMA:
        raise ValueError(f"{run_file.as_posix()} is not a {RUN_SCHEMA} record.")
    return run_file.resolve().parent, doc


def find_take(run_dir: Path, run: dict[str, Any], take: int, warnings: list[str]) -> tuple[dict[str, Any], Path]:
    for record in run.get("takes") or []:
        if record.get("take") == take:
            if record.get("status") != "ok":
                raise ValueError(f"take {take} of {run_dir.name} has status {record.get('status')!r}; "
                                 "choose an ok take.")
            path = run_dir / record["artifact"]["path"]
            if not path.is_file():
                raise ValueError(f"take {take} image {path.as_posix()} is missing.")
            if forge_core.sha256_file(path) != record["artifact"]["sha256"]:
                warnings.append(f"take {take} image changed since the run (sha256 differs)")
            return record, path
    raise ValueError(f"{run_dir.name} has no take {take}.")


def load_master(path: str | Path) -> tuple[Path, dict[str, Any]]:
    path = Path(path)
    master_file = path / "master.json" if path.is_dir() else path
    if not master_file.is_file():
        raise ValueError(f"No master.json at {path.as_posix()}.")
    doc = forge_core.read_json(master_file)
    if not isinstance(doc, dict) or doc.get("schema") != MASTER_SCHEMA:
        raise ValueError(f"{master_file.as_posix()} is not a {MASTER_SCHEMA} record.")
    return master_file.resolve().parent, doc


def _is_rel_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith("/") and "\\" not in value and \
        not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_master(doc: Any) -> list[str]:
    """Errors in a generate2dsprite.master.v1 record (the fields motion steps rely on); empty when valid."""
    if not isinstance(doc, dict):
        return ["master.json must be an object"]
    errors = []
    if doc.get("schema") != MASTER_SCHEMA:
        errors.append(f"schema must be {MASTER_SCHEMA}")
    if not isinstance(doc.get("name"), str) or not NAME_RE.match(doc["name"]):
        errors.append("name must be an asset id")
    if not isinstance(doc.get("identity_recap"), str) or not doc["identity_recap"].strip():
        errors.append("identity_recap must be a non-empty string")
    for field, allowed in (("facing", FACINGS), ("finish", FINISHES), ("class", tuple(CLASSES))):
        if doc.get(field) not in allowed:
            errors.append(f"{field} must be one of {', '.join(allowed)}")
    framing = doc.get("framing")
    if not isinstance(framing, dict):
        errors.append("framing must be an object")
    else:
        canvas = framing.get("canvas")
        if not (isinstance(canvas, list) and len(canvas) == 2 and all(isinstance(v, int) and v > 0 for v in canvas)):
            errors.append("framing.canvas must be [width, height]")
        for field in ("subject_height_px", "top_margin_px"):
            if not isinstance(framing.get(field), int) or isinstance(framing.get(field), bool) or framing[field] < 0:
                errors.append(f"framing.{field} must be a whole number >= 0")
    if not isinstance(doc.get("key"), str) or not re.fullmatch(r"#[0-9A-F]{6}", doc["key"]):
        errors.append("key must be #RRGGBB (upper case)")
    files = doc.get("files")
    if not isinstance(files, dict):
        errors.append("files must be an object")
    else:
        for name in ("master", "cutout", "source", "spec"):
            ref = files.get(name)
            if not isinstance(ref, dict) or not _is_rel_path(ref.get("path")) or not _is_sha(ref.get("sha256")):
                errors.append(f"files.{name} must be a fileRef with a relative path and a sha256")
        prompt = files.get("prompt")
        if prompt is not None and (not isinstance(prompt, dict) or not _is_sha(prompt.get("sha256"))):
            errors.append("files.prompt must be null or a fileRef")
    references = doc.get("references")
    if not isinstance(references, list) or any(
            not isinstance(ref, dict) or ref.get("role") not in ("identity", "style")
            or not _is_rel_path(ref.get("path")) for ref in references):
        errors.append("references must be a list of {role: identity|style, path, sha256}")
    transform = doc.get("transform")
    scale = transform.get("scale") if isinstance(transform, dict) else None
    offset = transform.get("offset") if isinstance(transform, dict) else None
    if not isinstance(scale, (int, float)) or not scale > 0 or not (isinstance(offset, list) and len(offset) == 2):
        errors.append("transform must hold scale > 0 and offset [x, y]")
    if not isinstance(doc.get("route"), str) or not doc["route"].strip():
        errors.append("route must be a non-empty string")
    return errors


# --------------------------------------------------------------------------- commands

def _write_prompt_and_spec(stage: Path, spec: dict[str, Any], prompt: str) -> None:
    (stage / "prompt.txt").write_bytes(prompt.encode("utf-8"))
    forge_core.write_json(stage / "spec.json", spec_document(spec, stage))


def _emit(warnings: Sequence[str]) -> None:
    for warning in warnings:
        print("warning: " + _ascii(warning), file=sys.stderr)


def _base_spec(args: argparse.Namespace) -> dict[str, Any]:
    return load_spec_file(Path(args.spec)) if getattr(args, "spec", None) else {}


def _takes_summary(final: Path, takes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for record in takes:
        row = {"take": record["take"], "status": record["status"]}
        if record["status"] == "ok":
            row.update(route=record["route"], artifact=str(final / record["artifact"]["path"]))
        elif record.get("error"):
            row["error"] = record["error"]
        rows.append(row)
    return rows


def _generation(command: str, args: argparse.Namespace, spec: dict[str, Any], prompt: str,
                references: Sequence[tuple[str, Path]], warnings: list[str], *, edit: dict[str, Any] | None = None,
                generate: bool = True) -> tuple[dict[str, Any], int]:
    """Shared body of generate and edit: publish prompt, spec and the takes; summarise."""
    media = media_from_args(args) if generate else None
    if media is not None and media.source == MEDIA_FAKE_ENV:
        warnings.append(f"{MEDIA_FAKE_ENV} is set: the takes come from a fake media CLI (tests only), not a real "
                        "generation")
    output = Path(args.output_dir)
    status, fallback, takes = "ok", None, []
    with forge_core.staged_output(output) as stage:
        _write_prompt_and_spec(stage, spec, prompt)
        if edit is not None:
            edit = {**edit, "still": forge_core.file_ref(edit["still"], stage),
                    "extra": [{"role": role, **forge_core.file_ref(path, stage)} for path, role in edit["extra"]]}
        if media is not None:
            takes = run_takes(stage, args.takes, prompt_file=stage / "prompt.txt",
                              references=[path for _, path in references], media=media)
            status, fallback = takes_status(takes, args.takes)
        good = [record for record in takes if record["status"] == "ok"]
        contact = None
        if len(good) >= 2:
            sheet = contact_sheet([(stage / record["artifact"]["path"], f"take {record['take']}") for record in good])
            forge_core.save_png(sheet, stage / "takes.png")
            contact = forge_core.file_ref(stage / "takes.png", stage)
        doc = run_document(command, spec, stage, references=references, media=media, takes=takes, status=status,
                           warnings=warnings, fallback=fallback, edit=edit)
        if contact:
            doc["contact"] = contact
        forge_core.write_json(stage / "run.json", doc)
    _emit(warnings)
    final = final_path(output)
    summary: dict[str, Any] = {
        "status": status, "command": command, "output": str(final), "prompt": str(final / "prompt.txt"),
        "spec": str(final / "spec.json"), "run": str(final / "run.json"), "name": spec["name"],
        "class": spec["class"], "finish": spec["finish"], "facing": spec["facing"],
        "references": [str(path) for _, path in references],
    }
    if media is not None:
        summary.update(takes=_takes_summary(final, takes), media_cli_source=media.source)
        good = [record for record in takes if record["status"] == "ok"]
        summary["route"] = good[0]["route"] if good else None
        if len(good) >= 2:
            summary["contact"] = str(final / "takes.png")
    if fallback:
        summary["fallback"] = fallback
    summary["warnings"] = len(warnings)
    if status == "fail":
        errors = "; ".join(f"take {record['take']}: {record.get('error', record['status'])}" for record in takes)
        summary["error"] = f"every take failed ({errors})"
        return summary, 1
    return summary, NO_ROUTE_EXIT if status == "no-route" else 0


def cmd_prompt(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    spec, warnings = normalise_spec(merge_flags(_base_spec(args), args), attach=True)
    return _generation("prompt", args, spec, build_prompt(spec), attached_references(spec), warnings,
                       generate=False)


def cmd_generate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    spec, warnings = normalise_spec(merge_flags(_base_spec(args), args), attach=True)
    return _generation("generate", args, spec, build_prompt(spec), attached_references(spec), warnings)


def cmd_edit(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    sources = [bool(args.master), bool(args.run), bool(args.spec)]
    if sum(sources) > 1:
        raise ValueError("pass one of --master, --run or --spec.")
    if args.take and not args.run:
        raise ValueError("--take needs --run.")
    warnings: list[str] = []
    base: dict[str, Any] = {}
    still = None
    if args.master:
        master_dir, master = load_master(args.master)
        base = load_spec_file(master_dir / master["files"]["spec"]["path"])
        base["recap"] = master.get("identity_recap") or base.get("recap")
        still = master_dir / master["files"]["master"]["path"]
    elif args.run:
        run_dir, run = load_run(args.run)
        base = load_spec_file(run_dir / "spec.json")
        if args.take:
            _, still = find_take(run_dir, run, args.take, warnings)
    elif args.spec:
        base = load_spec_file(Path(args.spec))
    if args.still:
        still = Path(args.still)
    if still is None:
        raise ValueError("pass the image to edit: --still, --master, or --run with --take.")
    if not still.is_file():
        raise ValueError(f"The still to edit does not exist: {still.as_posix()}")
    probe_image(still)
    if args.change is not None and args.change_file:
        raise ValueError("pass --change or --change-file, not both.")
    change = _read_text_file(args.change_file, "--change-file") if args.change_file else args.change
    change = clean_text(change, "change")
    if not change:
        raise ValueError("--change (or --change-file) is required: THE ONLY CHANGE to make.")
    extras = [Path(path) for path in args.extra_reference]
    if len(args.extra_role) > len(extras):
        raise ValueError("more --extra-role than --extra-reference values.")
    if 1 + len(extras) > len(ORDINALS):
        raise ValueError(f"at most {len(ORDINALS) - 1} --extra-reference images.")
    for path in extras:
        if not path.is_file():
            raise ValueError(f"--extra-reference {path.as_posix()} does not exist.")
        probe_image(path)
    roles = [clean_text(role, "extra-role") or DEFAULT_EXTRA_ROLE for role in args.extra_role]
    roles += [DEFAULT_EXTRA_ROLE] * (len(extras) - len(roles))
    spec, spec_warnings = normalise_spec(merge_flags(base, args), attach=False)
    warnings += spec_warnings
    framing = describe_framing(still, spec["key"], CLASSES[spec["class"]].noun)
    if framing is None:
        warnings.append(f"the FIRST image has no flat {key_words(spec['key'])} backdrop to measure; the prompt "
                        "asks for the same size and position without numbers")
    prompt = build_edit_prompt(spec, change=change, keep=clean_text(args.keep, "keep"), roles=roles, framing=framing)
    references = [("edit", still.resolve()), *[("extra", path.resolve()) for path in extras]]
    edit = {"still": still.resolve(), "change": change, "keep": clean_text(args.keep, "keep") or DEFAULT_KEEP,
            "extra": list(zip([path.resolve() for path in extras], roles)), "framing": framing}
    return _generation("edit", args, spec, prompt, references, warnings, edit=edit, generate=not args.prompt_only)


def _still_and_spec(args: argparse.Namespace, warnings: list[str]) -> dict[str, Any]:
    """Resolve the still (--still/--input or --run with --take) and the base spec for pad and approve."""
    still_arg = getattr(args, "still", None) or getattr(args, "input", None)
    if args.take and not args.run:
        raise ValueError("--take needs --run.")
    if args.run and args.spec:
        raise ValueError("pass --run or --spec, not both (the run holds its spec).")
    context: dict[str, Any] = {"run_dir": None, "run": None, "take": None, "still": None, "base": {}}
    if args.run:
        run_dir, run = load_run(args.run)
        context.update(run_dir=run_dir, run=run, base=load_spec_file(run_dir / "spec.json"))
        if args.take:
            context["take"], context["still"] = find_take(run_dir, run, args.take, warnings)
    elif args.spec:
        context["base"] = load_spec_file(Path(args.spec))
    if still_arg:
        if context["take"] is not None:
            raise ValueError("pass --take or the still path, not both.")
        context["still"] = Path(still_arg)
    if context["still"] is None:
        raise ValueError("pass the still: --still/--input PATH, or --run with --take N.")
    if not context["still"].is_file():
        raise ValueError(f"The still does not exist: {context['still'].as_posix()}")
    return context


def cmd_pad(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    warnings: list[str] = []
    context = _still_and_spec(args, warnings)
    base = context["base"]
    klass = (args.klass or base.get("class") or "hero").strip().lower()
    if klass not in CLASSES:
        raise ValueError(f"class must be one of {', '.join(CLASSES)}; got {klass!r}.")
    facing = (args.facing or base.get("facing") or "none").strip().lower()
    if facing not in FACINGS:
        raise ValueError(f"facing must be one of {', '.join(FACINGS)}; got {facing!r}.")
    key_hex = normalise_key(args.key or base.get("key") or DEFAULT_KEY)
    framing = framing_for(klass, args)
    image, info = forge_core.load_rgba(context["still"])
    result = pad_still(np.asarray(image), key_hex=key_hex, framing=framing, facing=facing, matte=args.matte,
                       crop_margin=args.crop_margin, min_part_area=args.min_part_area)
    warnings += result["warnings"]
    output = Path(args.output_dir)
    with forge_core.staged_output(output) as stage:
        forge_core.save_png(result["padded"], stage / "padded.png")
        forge_core.save_png(result["cutout"], stage / "padded_rgba.png")
        doc = {
            "schema": PAD_SCHEMA, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "created": forge_core.utc_timestamp(),
            "source": {"name": context["still"].name, "sha256": info["sha256"], "bytes": info["bytes"],
                       "size": info["size"]},
            "class": klass, "facing": facing, "key": key_hex,
            "framing": result["framing"], "transform": result["transform"], "matte": result["matte"],
            "key_choice": result["key_choice"],
            "files": {"padded": file_ref(stage / "padded.png", stage, size=True),
                      "cutout": file_ref(stage / "padded_rgba.png", stage, size=True)},
            "warnings": warnings,
        }
        forge_core.write_json(stage / "pad.json", doc)
    _emit(warnings)
    final = final_path(output)
    return {"status": "ok", "command": "pad", "output": str(final), "padded": str(final / "padded.png"),
            "cutout": str(final / "padded_rgba.png"), "metadata": str(final / "pad.json"), "class": klass,
            "scale": result["transform"]["scale"], "offset": result["transform"]["offset"],
            "subject_bbox": result["framing"]["subject_bbox"], "warnings": len(warnings)}, 0


def cmd_approve(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    warnings: list[str] = []
    context = _still_and_spec(args, warnings)
    take, run, run_dir, still = context["take"], context["run"], context["run_dir"], context["still"]
    if take is not None and args.route:
        raise ValueError("the take's route is recorded in its run; drop --route.")
    if args.identity_recap is not None and args.identity_recap_file:
        raise ValueError("pass --identity-recap or --identity-recap-file, not both.")
    raw = merge_flags(context["base"], args)
    if args.identity_recap_file:
        raw["recap"] = _read_text_file(args.identity_recap_file, "--identity-recap-file")
    elif args.identity_recap is not None:
        raw["recap"] = args.identity_recap
    spec, spec_warnings = normalise_spec(raw, attach=False)
    warnings += spec_warnings
    spec["recap"] = identity_recap(spec)
    prompt_path = None
    if run_dir is not None and (run_dir / "prompt.txt").is_file():
        prompt_path = run_dir / "prompt.txt"
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.is_file():
            raise ValueError(f"--prompt-file {prompt_path.as_posix()} does not exist.")
    route = take["route"] if take is not None else (clean_text(args.route, "route") or "external")
    media_source = ((run or {}).get("media") or {}).get("cli_source")
    if take is not None and media_source == MEDIA_FAKE_ENV:
        warnings.append(f"this take came from a fake media CLI ({MEDIA_FAKE_ENV}), not a real generation")
    framing = framing_for(spec["class"], args)
    image, info = forge_core.load_rgba(still)
    result = pad_still(np.asarray(image), key_hex=spec["key"], framing=framing, facing=spec["facing"],
                       matte=args.matte, crop_margin=args.crop_margin, min_part_area=args.min_part_area)
    warnings += result["warnings"]
    output = Path(args.output_dir)
    with forge_core.staged_output(output) as stage:
        forge_core.save_png(result["padded"], stage / "master.png")
        forge_core.save_png(result["cutout"], stage / "master_rgba.png")
        source = stage / ("source" + (still.suffix.lower() if still.suffix else ".png"))
        shutil.copyfile(still, source)
        prompt_ref = None
        if prompt_path is not None:
            shutil.copyfile(prompt_path, stage / "prompt.txt")
            prompt_ref = forge_core.file_ref(stage / "prompt.txt", stage)
        forge_core.write_json(stage / "spec.json", spec_document(spec, stage))
        references = []
        for role, field in (("identity", "identity_ref"), ("style", "style_ref")):
            ref = spec.get(field)
            if ref:
                entry = {"role": role, "path": forge_core.manifest_path(ref["path"], stage),
                         "sha256": ref.get("sha256")}
                if Path(ref["path"]).is_file():
                    entry["bytes"] = Path(ref["path"]).stat().st_size
                if ref.get("what"):
                    entry["what"] = ref["what"]
                references.append(entry)
        edit = None
        if run is not None and run.get("command") == "edit" and isinstance(run.get("edit"), dict):
            edited = run["edit"].get("still") or {}
            edit = {"from": {"path": forge_core.manifest_path(run_dir / edited["path"], stage),
                             "sha256": edited.get("sha256")} if edited.get("path") else None,
                    "change": run["edit"].get("change")}
        doc = {
            "schema": MASTER_SCHEMA,
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "created": forge_core.utc_timestamp(),
            "name": spec["name"],
            "subject": spec["subject"],
            "identity_recap": spec["recap"],
            "facing": spec["facing"],
            "view": default_view(spec),
            "finish": spec["finish"],
            "class": spec["class"],
            "framing": result["framing"],
            "key": spec["key"],
            "files": {
                "master": file_ref(stage / "master.png", stage, size=True),
                "cutout": file_ref(stage / "master_rgba.png", stage, size=True),
                "source": file_ref(source, stage, size=True),
                "spec": forge_core.file_ref(stage / "spec.json", stage),
                "prompt": prompt_ref,
            },
            "references": references,
            "transform": result["transform"],
            "route": route,
            "provenance": {
                "command": run.get("command") if run else "external",
                "run": forge_core.manifest_path(run_dir, stage) if run_dir is not None else None,
                "take": take["take"] if take is not None else None,
                "source_name": still.name,
                "source_sha256": info["sha256"],
                "media": take.get("media") if take is not None else None,
                "media_cli_source": media_source,
                "edit": edit,
            },
            "matte": {**result["matte"], "key_choice": result["key_choice"]},
            "warnings": warnings,
        }
        errors = validate_master(doc)
        if errors:
            raise ValueError("master.json self-check failed: " + "; ".join(errors))
        forge_core.write_json(stage / "master.json", doc)
    _emit(warnings)
    final = final_path(output)
    return {"status": "ok", "command": "approve", "output": str(final), "master": str(final / "master.png"),
            "cutout": str(final / "master_rgba.png"), "metadata": str(final / "master.json"), "name": spec["name"],
            "class": spec["class"], "finish": spec["finish"], "facing": spec["facing"], "route": route,
            "framing": {key: result["framing"][key] for key in ("canvas", "subject_height_px", "top_margin_px",
                                                                "subject_bbox")},
            "warnings": len(warnings)}, 0


# --------------------------------------------------------------------------- parser

def add_spec_arguments(parser: argparse.ArgumentParser, *, with_spec_file: bool = True) -> None:
    group = parser.add_argument_group("spec (a --spec JSON file and/or these flags; flags win)")
    if with_spec_file:
        group.add_argument("--spec", help="master spec JSON (generate2dsprite.master_spec.v1); its relative paths "
                                          "are relative to the file")
    group.add_argument("--name", help="asset id: 1-64 letters, digits, '.', '_' or '-'")
    group.add_argument("--subject", help="short noun phrase: what it is, e.g. 'a young fox ranger, full body'")
    group.add_argument("--identity", help="long, precise visual description: face, hair, body, costume, colours, "
                                          "held items")
    group.add_argument("--identity-file", help="UTF-8 text file holding the identity description")
    group.add_argument("--pose", help="stance or pose of the still (optional)")
    group.add_argument("--recap", help="identity recap reused verbatim by motion prompts: a noun phrase that reads "
                                       "after 'The same' (default: subject (identity))")
    group.add_argument("--facing", choices=FACINGS, help="left, right, front, back or none (default right; props none)")
    group.add_argument("--facing-parts", help="parts named in the facing sentence (default 'face, gaze and chest'; "
                                              "props 'front')")
    group.add_argument("--view", help="camera view words (default by facing, e.g. 'side view (profile to "
                                      "three-quarter)')")
    group.add_argument("--class", dest="klass", choices=sorted(CLASSES),
                       help="framing preset: hero, mob, boss or prop (default hero)")
    group.add_argument("--finish", choices=FINISHES, help="hd (default) or pixel: sets the style line")
    group.add_argument("--key", type=key_arg, help="flat key colour: #RRGGBB, magenta, green or blue (default #FF00FF)")
    group.add_argument("--genre", help="the game, for the style line, e.g. 'a side-scrolling action game' "
                                       "(default: none)")
    group.add_argument("--pronoun", choices=sorted(PRONOUNS), help="he, she, it or they (default they)")
    group.add_argument("--identity-ref", help="identity reference (FIRST attached image): this exact character, "
                                              "copied exactly")
    group.add_argument("--identity-ref-what", help="what the identity reference is, e.g. 'the dialogue portrait of "
                                                   "this exact character'")
    group.add_argument("--identity-copy", help="what to copy from it exactly (default: face, hair, body shape, "
                                               "costume, colours, markings and equipment)")
    group.add_argument("--style-ref", help="peer sprite (SECOND attached image): an approved in-game sprite whose "
                                           "style, outline, shading, size and position are matched")
    group.add_argument("--style-ref-what", help="what the peer sprite is, e.g. 'the ghost samurai summon, facing "
                                                "right'")
    group.add_argument("--style-dont-copy", help="what NOT to copy from the peer sprite (default: its face, hair, "
                                                 "outfit, equipment or colours; an empty value drops the clause)")
    group.add_argument("--allowed-text", help="the one written mark allowed, e.g. 'the single gold crest symbol'; "
                                              "every other text stays banned")
    group.add_argument("--ban", action="append", help="extra 'no ...' item, repeatable (adds to the spec's bans)")
    group.add_argument("--opaque-parts", help="parts named in the opaque-pixels sentence, e.g. 'hair, spear, flames'")
    group.add_argument("--extra", help="extra art-direction paragraph, placed before the background line")


def add_media_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("generation (generate2dmedia route_media.py)")
    group.add_argument("--takes", type=positive_int, default=1,
                       help=f"candidate stills to generate, 1-{MAX_TAKES}, one after another (default 1)")
    group.add_argument("--route", help="route passed to route_media.py --route (default: its own order, the API "
                                       "key first, then the local daemon)")
    group.add_argument("--media-cli", help="path of route_media.py (default: "
                                           "../generate2dmedia/scripts/route_media.py beside this skill)")
    group.add_argument("--media-arg", action="append", default=[], metavar="ARG",
                       help="extra argument for route_media.py, repeatable; write flags as --media-arg=--flag")
    group.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                       help=f"seconds before one take is stopped (default {DEFAULT_TIMEOUT_S:g})")


def add_pad_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("framing and keying")
    group.add_argument("--subject-height", type=int,
                       help=f"subject height in px on the {CANVAS} canvas (default: the class preset, hero 788)")
    group.add_argument("--top-margin", type=int,
                       help="px from the canvas top to the subject top (default: the class preset, hero 138)")
    group.add_argument("--lead", type=int,
                       help="px the subject centre moves away from its facing side (default: the class preset, "
                            "mob 100)")
    group.add_argument("--crop-margin", type=int, default=CROP_MARGIN_PX,
                       help=f"px kept around the subject when cropping (default {CROP_MARGIN_PX})")
    group.add_argument("--matte", choices=MATTES, default="soft",
                       help="forge_matte keyer that finds the subject (default soft)")
    group.add_argument("--min-part-area", type=positive_int, default=MIN_PART_AREA,
                       help=f"parts smaller than this many source px are specks: ignored for the subject box and "
                            f"dropped outside it (default {MIN_PART_AREA})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="master_still.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    pp = sub.add_parser("prompt", help="write the base-still prompt, spec.json and run.json",
                        description="Write the base-still prompt for one square 1024x1024 image on a flat key.")
    add_spec_arguments(pp)
    pp.add_argument("--output-dir", required=True, help="new folder (must not exist)")
    pp.set_defaults(func=cmd_prompt)

    pg = sub.add_parser("generate", help="write the prompt, then --takes N stills through route_media.py",
                        description="Write the prompt, then generate candidates through route_media.py. Exit 3: "
                                    "no route (status no-route, fallback codeart2d); the prompt is still published.")
    add_spec_arguments(pg)
    add_media_arguments(pg)
    pg.add_argument("--output-dir", required=True, help="new folder (must not exist)")
    pg.set_defaults(func=cmd_generate)

    pe = sub.add_parser("edit", help="fix by edit: reproduce the FIRST image exactly with ONE change",
                        description="Build 'Reproduce the FIRST image exactly ... THE ONLY CHANGE: ...' with the "
                                    "approved still as the FIRST reference, then generate.")
    source = pe.add_argument_group("the still to edit (the FIRST attached image)")
    source.add_argument("--master", help="master.json (or its folder): its master.png, spec and recap are used")
    source.add_argument("--run", help="output folder of prompt, generate or edit (its spec.json is used)")
    source.add_argument("--take", type=positive_int, help="with --run: edit this take")
    source.add_argument("--still", help="the image to edit (overrides the one from --master or --take)")
    change = pe.add_argument_group("the change")
    change.add_argument("--change", help="THE ONLY CHANGE, e.g. 'repaint the face with a warm tan skin tone'")
    change.add_argument("--change-file", help="UTF-8 text file holding the change")
    change.add_argument("--keep", help=f"what stays exactly as in the FIRST image (default: {DEFAULT_KEEP})")
    change.add_argument("--extra-reference", action="append", default=[],
                        help="another attached image (SECOND, THIRD, ...), repeatable")
    change.add_argument("--extra-role", action="append", default=[],
                        help="what each --extra-reference is and how to use it, in the same order")
    change.add_argument("--prompt-only", action="store_true",
                        help="write the edit prompt and run.json only; attach the listed references to your own "
                             "image tool")
    add_spec_arguments(pe)
    add_media_arguments(pe)
    pe.add_argument("--output-dir", required=True, help="new folder (must not exist)")
    pe.set_defaults(func=cmd_edit)

    pd = sub.add_parser("pad", help="key, crop and LANCZOS-scale a still to the class framing on 1024x1024",
                        description="Flatten the backdrop to the pure key, find the subject with forge_matte, crop "
                                    "it with a 6 px margin and LANCZOS-scale it to the class framing; record the "
                                    "transform.")
    still = pd.add_argument_group("the still")
    still.add_argument("--input", help="the generated still (any size, on a flat key or with real alpha)")
    still.add_argument("--run", help="output folder of generate or edit")
    still.add_argument("--take", type=positive_int, help="with --run: pad this take")
    still.add_argument("--spec", help="spec JSON: class, facing and key come from it")
    still.add_argument("--class", dest="klass", choices=sorted(CLASSES), help="framing preset (default hero)")
    still.add_argument("--facing", choices=FACINGS, help="left or right moves a mob's body behind centre "
                                                        "(default none)")
    still.add_argument("--key", type=key_arg, help="key colour of the still (default #FF00FF)")
    add_pad_arguments(pd)
    pd.add_argument("--output-dir", required=True, help="new folder (must not exist)")
    pd.set_defaults(func=cmd_pad)

    pa = sub.add_parser("approve", help="pad the chosen still and write master.png and master.json",
                        description="Pad the chosen still to the class framing and write master.png, "
                                    "master_rgba.png, source, spec.json, prompt.txt and master.json "
                                    f"({MASTER_SCHEMA}).")
    chosen = pa.add_argument_group("the chosen still")
    chosen.add_argument("--still", help="the chosen still (any route, or the host's own image tool)")
    chosen.add_argument("--run", help="output folder of prompt, generate or edit: its spec and prompt are used")
    chosen.add_argument("--take", type=positive_int, help="with --run: approve this take (its route is recorded)")
    chosen.add_argument("--route", help="route that made --still, e.g. codex-cli, grok-cli, openai, xai, "
                                        "host-image_gen (default external)")
    chosen.add_argument("--prompt-file", help="prompt that made --still (copied for provenance)")
    chosen.add_argument("--identity-recap", help="identity recap for motion prompts (default: the spec's recap, "
                                                 "else subject (identity))")
    chosen.add_argument("--identity-recap-file", help="UTF-8 text file holding the identity recap")
    add_spec_arguments(pa)
    add_pad_arguments(pa)
    pa.add_argument("--output-dir", required=True, help="new folder (must not exist)")
    pa.set_defaults(func=cmd_approve)
    return parser


def _check_args(args: argparse.Namespace) -> None:
    if getattr(args, "takes", 1) > MAX_TAKES:
        raise ValueError(f"--takes must be 1-{MAX_TAKES}; got {args.takes}.")
    timeout = getattr(args, "timeout", None)
    if timeout is not None and not (math.isfinite(timeout) and timeout > 0):
        raise ValueError(f"--timeout must be a positive number of seconds; got {timeout:g}.")
    margin = getattr(args, "crop_margin", None)
    if margin is not None and not 0 <= margin <= 64:
        raise ValueError(f"--crop-margin must be 0-64 px; got {margin}.")


def run(argv: Sequence[str] | None = None) -> tuple[dict[str, Any], int]:
    """Parse argv and run the command; returns (JSON summary, exit status). Raises on errors."""
    args = build_parser().parse_args(argv)
    _check_args(args)
    return args.func(args)


def _run(argv: Sequence[str] | None = None) -> int:
    summary, code = run(argv)
    print(json.dumps(summary, ensure_ascii=True))
    if code == 1 and summary.get("error"):
        print("error: " + _ascii(summary["error"]), file=sys.stderr)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors exit 2; refused input exits 1 with 'error: ...'; no route exits 3 (forge_core.run_cli)."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
