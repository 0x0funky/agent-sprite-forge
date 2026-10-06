#!/usr/bin/env python3
"""One approved still in, a whole game-ready sprite set out.

  plan     Read master.json (generate2dsprite.master.v1) and write a new set folder with
           set_plan.json: one image-to-video clip per action and view, each a loop or a
           one-shot with its own canvas and headroom (jump taller, attack wider), the end
           frame pinned to the master for idle and attack, numeric QC gates, and the motion
           prompt built from references/motion-prompts.md.
  run      Make every action: prepare_i2v_input.py input, motion prompt, generate2dmedia
           route_media.py video, video2dsprite.py process --matte soft, the numeric QC
           gates, automatic retakes with prompt fixes (up to --max-takes), register_clip.py
           apply, gait_loop.py select (loops) or retime.py (one-shots), finish_frames.py
           hd|pixel, engine_export.py package and verify. Every step is recorded in
           set_state.json and takes.jsonl, so running it again resumes where it stopped.
  review   Contact sheet per action and take, a cast line-up and review.json, for the host
           agent to LOOK at before it accepts anything.
  accept   Record the agent's semantic approval of one take.
  retake   Record a semantic rejection with a prompt fix; the next run makes a new take.
  report   One JSON with every action's route, takes, QC numbers, loop or timing and output
           files.

Exit codes: 0 done; 1 refused input or failed step (error: ... on stderr); 2 usage; 3 the
run waits for a clip because no generation route exists (drop the clip into the take's
media folder named in the summary and run again).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_matte  # noqa: E402
import prepare_i2v_input as prep  # noqa: E402  (sibling: opaque-master keying, stance anchor, prompt lint)
import register_clip  # noqa: E402  (sibling: job geometry, the master as placed in the video)

TOOL_NAME = "sprite_set"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION
MASTER_SCHEMA = "generate2dsprite.master.v1"
PLAN_SCHEMA = "video2dsprite.sprite_set_plan.v1"
STATE_SCHEMA = "video2dsprite.sprite_set_state.v1"
TAKE_SCHEMA = "video2dsprite.sprite_set_take.v1"
QC_SCHEMA = "video2dsprite.sprite_set_qc.v1"
REVIEW_SCHEMA = "video2dsprite.sprite_set_review.v1"
REPORT_SCHEMA = "video2dsprite.sprite_set_report.v1"
STANDIN_SCHEMA = "video2dsprite.finish_standin.v1"
PLAN_FILE, STATE_FILE, TAKES_FILE = "set_plan.json", "set_state.json", "takes.jsonl"
LIBRARY_FILE = HERE.parent / "references" / "motion-prompts.md"

SCRIPTS = {name: HERE / f"{name}.py" for name in
           ("prepare_i2v_input", "video2dsprite", "register_clip", "gait_loop", "retime", "engine_export")}
FINISH_SCRIPT = HERE / "finish_frames.py"
ROUTE_MEDIA = HERE.parents[1] / "generate2dmedia" / "scripts" / "route_media.py"
MEDIA_FAKE_ENV = "FORGE_ROUTE_MEDIA_FAKE"     # tests: path of a fake media CLI (same argv and JSON line)
FINISH_ENV = "FORGE_FINISH_FRAMES"           # 'standin' forces the private stand-in; a path names a finisher
EXIT_NO_ROUTE = 3
TOOL_TIMEOUT = 1800.0
DEFAULT_ACTIONS = ("idle", "walk", "run", "attack", "jump", "hurt")
VIEWS = ("side", "front", "back")
FINISHES = ("hd", "pixel")
TARGET_HEIGHT = {"hd": 256, "pixel": 80}      # finished body height in px for a hero
CLASS_HEIGHT = {"boss": 2.0}                 # cast rule: a boss is about twice the hero; mobs and spirits 1.0
HOVER_CLASSES = ("spirit", "ghost", "hover", "flying")
LUMA = np.array([0.299, 0.587, 0.114], np.float32)
QC_SUBJECT_PX = 112                          # QC works on frames reduced to about this body height
GATE_IDS = ("area", "feet", "identity", "zoom", "turn", "edge", "background", "extra", "motion", "end-pose",
            "loop", "registration", "keying")


# --------------------------------------------------------------------------- action and canvas presets

@dataclass(frozen=True)
class ActionPreset:
    """How one action is generated and cut."""

    kind: str                  # loop | oneshot
    loop_kind: str | None      # gait_loop select --kind: gait | idle (hover for floating classes)
    gait_state: str | None     # gait_loop select --state: walk | run
    prepare_action: str        # prepare_i2v_input --action (its job records this template)
    canvas: str                # square | tall | wide
    pin_last: bool             # end frame pinned to the master when the route takes a last frame
    lock: str                  # register_clip --lock: feet | none
    airborne: bool             # the feet leave the ground (the feet gate checks start and end only)
    hit_tick: bool             # one-shot with a hit event on its largest reach
    returns_to_rest: bool      # the clip starts and ends on the master pose
    min_motion: float          # pose change (share of the master area) that proves the action happened
    retime_kind: str           # retime.py --kind


ACTION_PRESETS: dict[str, ActionPreset] = {
    "idle": ActionPreset("loop", "idle", None, "idle", "square", True, "feet", False, False, True, 0.006, "idle"),
    "walk": ActionPreset("loop", "gait", "walk", "walk", "square", False, "feet", False, False, True, 0.05, "walk"),
    "run": ActionPreset("loop", "gait", "run", "run", "square", False, "none", False, False, True, 0.06, "run"),
    "attack": ActionPreset("oneshot", None, None, "attack", "wide", True, "feet", False, True, True, 0.03,
                           "attack"),
    "jump": ActionPreset("oneshot", None, None, "attack", "tall", False, "feet", True, False, True, 0.08, "other"),
    "hurt": ActionPreset("oneshot", None, None, "hurt", "square", False, "feet", False, False, True, 0.02, "hurt"),
    "cast": ActionPreset("oneshot", None, None, "cast", "wide", False, "feet", False, False, True, 0.02, "cast"),
    "guard": ActionPreset("oneshot", None, None, "guard", "square", False, "feet", False, False, True, 0.02,
                          "guard"),
    "victory": ActionPreset("oneshot", None, None, "victory", "tall", False, "feet", False, False, False, 0.03,
                            "victory"),
    "defeat": ActionPreset("oneshot", None, None, "defeat", "wide", False, "none", False, False, False, 0.04,
                           "defeat"),
}

# sprite-gen per-state canvases: jump 3:4 with 34% headroom, attack 16:9; square keeps the master framing.
CANVAS_PRESETS = {"square": {"aspect": None, "headroom": None, "lead": 0.0},
                  "tall": {"aspect": (3, 4), "headroom": 0.34, "lead": 0.0},
                  "wide": {"aspect": (16, 9), "headroom": None, "lead": 0.06}}

BASE_GATES: dict[str, dict[str, Any]] = {
    "area": {"min": 0.72, "max": 1.32, "maxBadShare": 0.1},
    "feet": {"max": 0.05, "badFrames": 3, "mode": "all"},
    "identity": {"start": 0.8, "min": 0.5, "badFrames": 2},
    "zoom": {"max": 0.08},
    "turn": {"enabled": True, "margin": 0.08, "run": 3},
    "edge": {"px": 2},
    "background": {"tolerance": 48.0, "maxImpure": 0.004, "badFrames": 2},
    "extra": {"max": 0.015, "badFrames": 2},
    "motion": {"min": 0.02},
    "endPose": {"enabled": True, "min": 0.8},
    "window": {"loopSeconds": 1.0},
}


# --------------------------------------------------------------------------- small helpers

def _ascii(text: Any) -> str:
    return forge_core.ascii_text(str(text))


def _now() -> str:
    return forge_core.utc_timestamp()


def _even(value: float) -> int:
    return max(16, 2 * forge_core.round_half_up(value / 2))


_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2}|/)")


_EMBEDDED = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|*?]*")


def _last_part(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1] or path


def _scrub(value: Any) -> Any:
    """A JSON value with every absolute path cut to its last component, also inside messages (written JSON
    never holds one); keys starting with _ are dropped."""
    if isinstance(value, dict):
        return {str(key): _scrub(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        if _ABSOLUTE.match(value) and not any(char.isspace() for char in value):
            return _last_part(value)
        return _EMBEDDED.sub(lambda match: _last_part(match.group(0)), value)
    if isinstance(value, np.generic):
        return value.item()
    return value


def _json_line(text: str) -> dict | None:
    """The last line of ``text`` that is a JSON object (every forge CLI prints one summary line)."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _error_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("error:")]
    text = errors[-1] if errors else (lines[-1] if lines else "no output")
    return text[6:].strip() if text.startswith("error:") else text


def _natural(path: Path) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def _pngs(folder: Path) -> list[Path]:
    return sorted((path for path in folder.glob("*.png") if path.is_file()), key=_natural)


def _ranges(frames: Sequence[int], limit: int = 8) -> str:
    """'3-7, 12, 20-21' for a sorted list of frame numbers."""
    runs: list[list[int]] = []
    for frame in sorted(set(int(f) for f in frames)):
        if runs and frame == runs[-1][1] + 1:
            runs[-1][1] = frame
        else:
            runs.append([frame, frame])
    text = [f"{a}-{b}" if b > a else str(a) for a, b in runs[:limit]]
    return ", ".join(text) + (f" and {len(runs) - limit} more runs" if len(runs) > limit else "")


def _round(value: Any, digits: int = 4) -> Any:
    if isinstance(value, float):
        return round(value, digits) if math.isfinite(value) else None
    if isinstance(value, np.floating):
        return _round(float(value), digits)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (list, tuple)):
        return [_round(item, digits) for item in value]
    if isinstance(value, dict):
        return {key: _round(item, digits) for key, item in value.items()}
    return value


def _move_aside(path: Path) -> Path:
    """Keep an unrecorded leftover (a crash between a tool's publish and the state save) beside the new run."""
    for number in range(1, 1000):
        target = path.with_name(f"{path.name}.orphan-{number}")
        if not os.path.lexists(target):
            os.replace(path, target)
            return target
    raise ValueError(f"too many orphaned copies of {path.name}; clean the folder")


def _write_new_json(path: Path, data: Any) -> None:
    """Publish a JSON file that must not exist yet (no-replace)."""
    stage = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        forge_core.write_json(stage, data, no_clobber=False)
        forge_core.publish_file_no_replace(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def _publish_png(image: Image.Image | np.ndarray, path: Path) -> None:
    stage = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        forge_core.save_png(image, stage)
        forge_core.publish_file_no_replace(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def _next_numbered(folder: Path, stem: str, suffix: str) -> Path:
    for number in range(1, 10000):
        candidate = folder / f"{stem}-{number:03d}{suffix}"
        if not os.path.lexists(candidate):
            return candidate
    raise ValueError(f"no free {stem}-NNN{suffix} name in {folder.name}")


def _fps_value(text: Any) -> float:
    rate = Fraction(str(text).strip()) if not isinstance(text, (int, float)) else Fraction(text)
    if rate <= 0:
        raise ValueError(f"frame rate must be positive, got {text!r}")
    return float(rate)


def _duration_arg(value: Any) -> str:
    """Clip seconds as route_media.py parses them (an int): the plan stores 6.0, the media CLI gets "6"."""
    seconds = float(value)
    return str(int(seconds)) if seconds.is_integer() else f"{seconds:g}"


# --------------------------------------------------------------------------- tools by path

class ToolFailure(RuntimeError):
    """A sibling CLI exited non-zero; ``message`` is its error line."""

    def __init__(self, tool: str, code: int, message: str):
        super().__init__(f"{tool} exited {code}: {message}")
        self.tool, self.code, self.message = tool, code, message


def _child_env() -> dict[str, str]:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}


def run_tool(script: Path, args: Sequence[Any], *, timeout: float = TOOL_TIMEOUT,
             label: str | None = None) -> tuple[dict, list[str]]:
    """Run a forge CLI by path; returns its one-line JSON summary and its warning lines."""
    label = label or script.name
    argv = [sys.executable, str(script), *(str(arg) for arg in args)]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, env=_child_env(), check=False)
    except subprocess.TimeoutExpired:
        raise ToolFailure(label, -1, f"timed out after {timeout:g} s") from None
    stdout = done.stdout.decode("utf-8", "replace")
    stderr = done.stderr.decode("utf-8", "replace")
    if done.returncode != 0:
        raise ToolFailure(label, done.returncode, _error_line(stderr))
    summary = _json_line(stdout)
    if summary is None:
        raise ToolFailure(label, 0, "printed no JSON summary line")
    warnings = [line.strip()[8:].strip() for line in stderr.splitlines() if line.strip().startswith("warning:")]
    return summary, warnings


# --------------------------------------------------------------------------- the prompt library

_FENCE = re.compile(r"^```(template|negatives|clauses|gatefix)(?:[ \t]+([A-Za-z0-9_-]+))?[ \t]*\r?\n(.*?)^```[ \t]*$",
                    re.M | re.S)
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True)
class Library:
    """references/motion-prompts.md: per-action templates, negatives and the failure clause library."""

    sha256: str
    templates: dict[str, str]
    negatives: dict[str, str]
    clauses: dict[str, str]
    gatefix: dict[str, list[str]]


def load_library(path: Path = LIBRARY_FILE) -> Library:
    """Parse the fenced ``template``/``negatives``/``clauses``/``gatefix`` blocks of motion-prompts.md."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")   # a CRLF checkout (core.autocrlf)
    templates: dict[str, str] = {}
    negatives: dict[str, str] = {}
    clauses: dict[str, str] = {}
    gatefix: dict[str, list[str]] = {}
    for kind, name, body in _FENCE.findall(text):
        if kind in ("template", "negatives"):
            target = templates if kind == "template" else negatives
            if not name or name in target:
                raise ValueError(f"{Path(path).name}: every ```{kind} block needs one new action name")
            target[name] = " ".join(body.split())
            continue
        for number, line in enumerate(body.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            ident, sep, value = line.partition("|")
            ident, value = ident.strip(), " ".join(value.split())
            if not sep or not re.fullmatch(r"[a-z0-9-]+", ident) or not value:
                raise ValueError(f"{Path(path).name}: ```{kind} line {number} must be 'id | text'")
            if kind == "clauses":
                clauses[ident] = value
            else:
                gatefix[ident] = [item.strip() for item in value.split(",") if item.strip()]
    missing = [action for action in ACTION_PRESETS if action not in templates]
    unknown = sorted({ident for ids in gatefix.values() for ident in ids if ident not in clauses})
    unmapped = [gate for gate in GATE_IDS if gate not in gatefix]
    if missing or unknown or unmapped:
        raise ValueError(f"{Path(path).name} is incomplete: templates missing for {missing}, unknown clause ids "
                         f"{unknown}, gates without a fix {unmapped}")
    return Library(forge_core.sha256_bytes(raw), templates, negatives, clauses, gatefix)


def fill(text: str, words: dict[str, str]) -> str:
    """Replace {facing}, {toward}, {away}, {pos}, {key}, {style} and {action}; unknown names stay."""
    return _PLACEHOLDER.sub(lambda match: words.get(match.group(1), match.group(0)), text)


def prompt_words(facing: str, key: str, style: str, pronoun: str, action: str) -> dict[str, str]:
    lock = {"left": "LEFT", "right": "RIGHT", "front": "the viewer", "back": "away from the viewer"}[facing]
    toward = {"left": "to the LEFT", "right": "to the RIGHT", "front": "toward the viewer",
              "back": "away from the viewer"}[facing]
    away = {"left": "to the RIGHT", "right": "to the LEFT", "front": "away from the viewer",
            "back": "toward the viewer"}[facing]
    return {"facing": lock, "toward": toward, "away": away, "pos": pronoun, "key": key_words(key)[0],
            "style": style, "action": action}


def key_words(key: str) -> tuple[str, str]:
    """(colour word, background clause) for the flat key backdrop."""
    if key == "magenta":
        return "magenta", "solid flat pure magenta background only for the whole shot"
    if key == "green":
        return "green", "solid flat pure bright green background (#00FF00 green screen) only for the whole shot"
    if key == "blue":
        return "blue", "solid flat pure blue background (#0000FF) only for the whole shot"
    return key, f"solid flat pure {key} background only for the whole shot"


def identity_phrase(recap: str) -> str:
    """'The same <recap>' without a doubled article or a closing full stop."""
    text = " ".join(str(recap).split()).rstrip(" .")
    text = re.sub(r"^(?:the same|a|an|the)\s+", "", text, flags=re.I)
    return text


def build_prompt(entry: dict, plan: dict, fixes: Sequence[str]) -> str:
    """The game-opus55 prompt shape: 'The same <identity>' + facing lock + ONE action + same start
    and end pose + negatives and fix clauses + locked camera, flat key background, exact style."""
    master = plan["masters"][entry["view"]]
    key = plan["key"]
    words = prompt_words(master["facing"], key, plan["style"], plan["pronoun"], entry["action"])
    parts = [f"The same {identity_phrase(master['identityRecap'])}, facing {words['facing']} the entire time and "
             f"never turning around, {fill(entry['motion'], words).strip()}"]
    if entry["returnsToRest"]:
        parts.append("The clip starts AND ends in exactly the same pose as the still.")
    seen = set()
    for clause in [entry.get("negatives") or "", *fixes]:
        text = fill(str(clause), words).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            parts.append(text if text.endswith((".", "!", "?")) else text + ".")
    parts.append(f"camera locked, no zoom, no pan; {key_words(key)[1]}; keep the exact {plan['style']} look, palette, "
                 "proportions and costume of the still; single continuous action.")
    return " ".join(part.strip() for part in parts if part.strip()) + "\n"


def lint_findings(prompt: str, key: str) -> list[dict[str, str]]:
    """prepare_i2v_input lint, minus the numeric work region and timeline rules: the game-opus55 recipe
    frames the clip by the input image and names one action instead."""
    return [item for item in prep.lint_prompt(prompt, key if key in forge_matte.DECLARED_KEYS else None)
            if item["rule"] not in ("missing-work-region", "missing-timeline")]


# --------------------------------------------------------------------------- the master

@dataclass(frozen=True)
class Master:
    """generate2dsprite.master.v1 plus the geometry measured on its padded PNG."""

    json_path: Path
    png_path: Path
    data: dict
    name: str
    identity_recap: str
    facing: str
    finish: str
    klass: str
    key: str
    key_rgb: tuple[int, int, int]
    size: tuple[int, int]
    box: tuple[int, int, int, int]
    anchor: tuple[float, float]
    area: int
    framing: dict
    style: str | None


def _parse_key(value: Any) -> tuple[str, tuple[int, int, int]]:
    if isinstance(value, dict):
        value = next((value[k] for k in ("color", "colour", "name", "hex", "rgb", "key") if value.get(k) is not None),
                     None)
    if value is None:
        return "magenta", forge_matte.DECLARED_KEYS["magenta"]
    if isinstance(value, (list, tuple)) and len(value) == 3:
        value = "#" + "".join(f"{int(channel):02x}" for channel in value)
    text = str(value).strip().lower()
    if text in forge_matte.DECLARED_KEYS:
        return text, tuple(forge_matte.DECLARED_KEYS[text])
    digits = text[1:] if text.startswith("#") else text
    if not re.fullmatch(r"[0-9a-f]{6}", digits):
        raise ValueError(f"master.json key must be magenta, green, blue, #rrggbb or [r, g, b]; got {value!r}")
    rgb = tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
    for name, declared in forge_matte.DECLARED_KEYS.items():
        if tuple(declared) == rgb:
            return name, rgb
    return "#" + digits, rgb


def _parse_facing(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("direction") or value.get("value") or value.get("facing")
    text = str(value or "").strip().lower()
    aliases = {"l": "left", "r": "right", "screen left": "left", "screen right": "right", "viewer": "front",
               "toward the viewer": "front", "away": "back", "away from the viewer": "back"}
    text = aliases.get(text, text)
    if text not in ("left", "right", "front", "back"):
        raise ValueError(f"master.json facing must be left, right, front or back; got {value!r}")
    return text


def _parse_size(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (int, float)) and value > 0:
        return int(value), int(value)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    if isinstance(value, dict) and "width" in value and "height" in value:
        return int(value["width"]), int(value["height"])
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)\s*[x,]\s*(\d+)\s*", value)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _master_png_ref(data: dict, base: Path) -> tuple[Path, str | None]:
    """The padded master PNG named in master.json ``files`` (a dict or a list of {path, sha256})."""
    found: list[tuple[str, str, str | None]] = []

    def add(entry: Any, label: str) -> None:
        if isinstance(entry, str):
            found.append((label, entry, None))
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
            found.append((" ".join(str(entry.get(k) or "") for k in ("role", "name", "kind")) + " " + label,
                          entry["path"], entry.get("sha256")))
        elif isinstance(entry, dict) and label.lower().endswith(".png"):   # {"master.png": {"sha256": ...}}
            found.append((label, label, entry.get("sha256")))

    files = data.get("files")
    if isinstance(files, dict):
        for name, entry in files.items():
            add(entry, str(name))
    elif isinstance(files, list):
        for entry in files:
            add(entry, "")
    pngs = [item for item in found if item[1].lower().endswith(".png")]
    if not pngs:
        raise ValueError("master.json files names no PNG; it needs the padded master.png with its sha256")

    def rank(item: tuple[str, str, str | None]) -> int:
        words = (item[0] + " " + Path(item[1]).name).lower()
        return 2 if "pad" in words else (1 if "master" in words else 0)

    _, relative, digest = max(pngs, key=rank)   # first of the best rank: padded, then master, then any PNG
    path = Path(relative)
    return (path if path.is_absolute() else base / path), digest


def load_master(path: Path) -> Master:
    """Read master.json, check the padded PNG's sha256, key an opaque master like prepare_i2v_input does
    and measure the subject box, the stance anchor and the opaque area."""
    path = Path(path)
    try:
        data = forge_core.read_json(path)
    except ValueError as error:
        raise ValueError(f"{path.name} is not valid JSON: {error}") from None
    if not isinstance(data, dict) or data.get("schema") != MASTER_SCHEMA:
        raise ValueError(f"{path.name} is not a {MASTER_SCHEMA} document")
    recap = data.get("identity_recap")
    if not isinstance(recap, str) or len(recap.split()) < 2:
        raise ValueError(f"{path.name} needs identity_recap, the long identity phrase every motion prompt repeats")
    png, digest = _master_png_ref(data, path.parent)
    if not png.is_file():
        raise ValueError(f"the master PNG {png.name} named in {path.name} is missing")
    image, info = forge_core.load_rgba(png)
    if digest and str(digest).lower() != info["sha256"]:
        raise ValueError(f"{png.name} changed since {path.name} recorded it (sha256 mismatch)")
    key, key_rgb = _parse_key(data.get("key"))
    pixels = np.asarray(image)
    if int(pixels[..., 3].min()) == 255:
        pixels, _record = prep.key_opaque_master(pixels, key, False)
    alpha = pixels[..., 3]
    box = forge_core.subject_bbox(alpha)
    if box is None:
        raise ValueError(f"{png.name} has no subject after keying its {key} backdrop")
    anchor = prep.measure_anchor(alpha, "stance")
    finish = str(data.get("finish") or "hd").strip().lower()
    if finish not in FINISHES:
        raise ValueError(f"master.json finish must be hd or pixel; got {data.get('finish')!r}")
    framing = data.get("framing") if isinstance(data.get("framing"), dict) else {}
    stated = _parse_size(framing.get("canvas"))
    if stated is not None and stated != (alpha.shape[1], alpha.shape[0]):
        raise ValueError(f"{path.name} framing.canvas {list(stated)} does not match {png.name} "
                         f"({alpha.shape[1]}x{alpha.shape[0]}); the master.json is stale")
    style = data.get("style") if isinstance(data.get("style"), str) and data.get("style").strip() else None
    return Master(json_path=path, png_path=png, data=data, name=str(data.get("name") or path.parent.name or "sprite"),
                  identity_recap=recap, facing=_parse_facing(data.get("facing")), finish=finish,
                  klass=str(data.get("class") or "hero").strip().lower(), key=key, key_rgb=key_rgb,
                  size=(alpha.shape[1], alpha.shape[0]), box=box, anchor=(float(anchor[0]), float(anchor[1])),
                  area=int((alpha > 127).sum()), framing=framing, style=style)


# --------------------------------------------------------------------------- placement

def placement(master: Master, preset: str) -> dict:
    """The provider canvas of one action: the master placed by one recorded transform (prepare_i2v_input
    --canvas --scale --root --anchor), its margin and the action padding that covers the whole video
    footprint on the master canvas (so register_clip never clips motion)."""
    spec = CANVAS_PRESETS[preset]
    W0, H0 = master.size
    x0, y0, x1, y1 = master.box
    ax, ay = master.anchor
    if spec["aspect"] is None:
        width, height, scale, root = W0, H0, 1.0, (ax, ay)
    else:
        aw, ah = spec["aspect"]
        height = H0
        width = _even(H0 * aw / ah)
        scale = 1.0
        bottom = max(H0 - ay, 8.0)
        root_y = ay
        if spec["headroom"]:
            top = spec["headroom"] * height
            scale = min(1.0, (height - bottom - top) / max(1.0, ay - y0), (width - 16.0) / max(1.0, x1 - x0))
            root_y = height - bottom
        lead = {"left": 1.0, "right": -1.0}.get(master.facing, 0.0) * spec["lead"] * width
        low, high = scale * (ax - x0) + 6.0, width - scale * (x1 - ax) - 6.0
        root_x = min(max(width / 2 + lead, low), high) if low <= high else width / 2
        root = (root_x, root_y)
    offset = (root[0] - scale * ax, root[1] - scale * ay)
    placed = (offset[0] + scale * x0, offset[1] + scale * y0, offset[0] + scale * x1, offset[1] + scale * y1)
    gap = min(placed[0], placed[1], width - placed[2], height - placed[3])
    if gap < 1:
        raise ValueError(f"the master's subject touches the {preset} canvas edge ({width}x{height}); re-pad the master "
                         "with a free margin on every side")
    margin = max(0, min(20, int(math.floor(gap)) - 3))
    padding = [max(0, math.ceil(offset[0] / scale - 1e-9)), max(0, math.ceil(offset[1] / scale - 1e-9)),
               max(0, math.ceil((width - offset[0]) / scale - W0 - 1e-9)),
               max(0, math.ceil((height - offset[1]) / scale - H0 - 1e-9))]
    return {"preset": preset, "canvas": [width, height], "scale": round(scale, 6),
            "root": [round(root[0], 4), round(root[1], 4)], "anchor": [ax, ay],
            "subjectBox": [round(value, 2) for value in placed], "margin": margin, "padding": padding,
            "headroom": round(placed[1] / height, 4)}


# --------------------------------------------------------------------------- QC gates (numeric)

def _block_mean(array: np.ndarray, factor: int) -> np.ndarray:
    """Mean of factor x factor blocks (the trailing partial block is dropped), as float32."""
    if factor <= 1:
        return array.astype(np.float32)
    height, width = (array.shape[0] // factor) * factor, (array.shape[1] // factor) * factor
    cut = array[:height, :width].astype(np.float32)
    return cut.reshape(height // factor, factor, width // factor, factor, *cut.shape[2:]).mean(axis=(1, 3))


@dataclass
class QcFrame:
    """One take frame reduced for the gates: alpha (0-255), luma over mid grey, raw RGB and the edge touch
    measured at full resolution."""

    alpha: np.ndarray
    luma: np.ndarray
    raw: np.ndarray | None
    edge: bool


def qc_frame(rgba: np.ndarray, raw: np.ndarray | None, factor: int, edge_px: int) -> QcFrame:
    alpha = rgba[..., 3]
    band = max(1, int(edge_px))
    edge = bool((alpha[:band] > 16).any() or (alpha[-band:] > 16).any()
                or (alpha[:, :band] > 16).any() or (alpha[:, -band:] > 16).any())
    weight = alpha.astype(np.float32) / 255.0
    luma = (rgba[..., :3].astype(np.float32) @ LUMA) * weight + 128.0 * (1.0 - weight)
    return QcFrame(_block_mean(alpha, factor), _block_mean(luma, factor),
                   None if raw is None else _block_mean(raw[..., :3], factor), edge)


def _box_sum(values: np.ndarray, height: int, width: int) -> np.ndarray:
    table = np.zeros((values.shape[0] + 1, values.shape[1] + 1), np.float64)
    table[1:, 1:] = values.astype(np.float64).cumsum(0).cumsum(1)
    return table[height:, width:] - table[:-height, width:] - table[height:, :-width] + table[:-height, :-width]


def _crop_padded(image: np.ndarray, left: int, top: int, width: int, height: int, fill: float) -> np.ndarray:
    out = np.full((height, width), fill, np.float64)
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(image.shape[1], left + width), min(image.shape[0], top + height)
    if x0 < x1 and y0 < y1:
        out[y0 - top:y1 - top, x0 - left:x1 - left] = image[y0:y1, x0:x1]
    return out


def ncc_best(template: np.ndarray, image: np.ndarray, x0: int, y0: int, search: int) -> float | None:
    """Best zero-mean NCC of ``template`` with its top-left within +-search px of (x0, y0) in ``image``
    (padded with the mid-grey backdrop); None for a flat template."""
    height, width = template.shape
    centred = template.astype(np.float64) - float(template.mean())
    norm = math.sqrt(float((centred * centred).sum()))
    if norm < 2.0 * math.sqrt(centred.size):   # luma spread under 2 levels: nothing to match
        return None
    region = _crop_padded(image, x0 - search, y0 - search, width + 2 * search, height + 2 * search, 128.0)
    windows = np.lib.stride_tricks.sliding_window_view(region, (height, width))
    numerator = np.einsum("ijkl,kl->ij", windows, centred)
    count = height * width
    sums = _box_sum(region, height, width)
    spread = np.sqrt(np.maximum(_box_sum(region * region, height, width) - sums * sums / count, 0.0))
    return float((numerator / np.maximum(spread * norm, 1e-9)).max())


def _mask_stats(mask: np.ndarray) -> dict | None:
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return None
    columns = np.flatnonzero(mask.any(axis=0))
    box = (int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1)
    try:
        ground = forge_core.ground_row(mask, min_run=2)
    except ValueError:
        ground = box[3]
    height = max(1, ground - box[1])
    band = max(2, forge_core.round_half_up(0.04 * height))
    try:
        stance = float(forge_core.anchor_from_mask(mask, "stance", band_rows=band, min_run=2)[0])
    except ValueError:
        stance = (box[0] + box[2]) / 2
    head_rows = mask[box[1]:box[1] + max(1, forge_core.round_half_up(0.35 * height))]
    ys, xs = np.nonzero(head_rows)
    top = (float(xs.mean()) + 0.5, float(ys.mean()) + 0.5 + box[1]) if xs.size else ((box[0] + box[2]) / 2, box[1])
    return {"area": int(mask.sum()), "box": box, "ground": ground, "height": height, "stance": stance, "top": top}


def _extra_area(mask: np.ndarray, min_area: int) -> int:
    """Opaque area outside the largest component (components under ``min_area`` are specks)."""
    labels, count = forge_core.label_components(mask)
    if count <= 1:
        return 0
    areas = np.bincount(labels.ravel())[1:]
    areas = areas[areas >= min_area]
    return int(areas.sum() - areas.max()) if areas.size > 1 else 0


def _shift(mask: np.ndarray, dx: float, dy: float) -> np.ndarray:
    dx, dy = forge_core.round_half_up(dx), forge_core.round_half_up(dy)
    out = np.zeros_like(mask)
    height, width = mask.shape
    sx0, sx1, sy0, sy1 = max(0, -dx), min(width, width - dx), max(0, -dy), min(height, height - dy)
    if sx0 < sx1 and sy0 < sy1:
        out[sy0 + dy:sy1 + dy, sx0 + dx:sx1 + dx] = mask[sy0:sy1, sx0:sx1]
    return out


def _iou(first: np.ndarray, second: np.ndarray) -> float:
    union = int((first | second).sum())
    return float((first & second).sum()) / union if union else 0.0


def _head_box(mask: np.ndarray, stats: dict) -> tuple[int, int, int, int] | None:
    """The top of the subject: its top 35%, as wide as its top 20% plus a 10% margin (register_clip qc)."""
    x0, y0, x1, _ = stats["box"]
    height = stats["height"]
    crown = np.flatnonzero(mask[y0:y0 + max(1, forge_core.round_half_up(0.2 * height))].any(axis=0))
    if crown.size == 0:
        return None
    margin = forge_core.round_half_up(0.1 * (crown[-1] + 1 - crown[0]))
    hx0, hx1 = max(x0, int(crown[0]) - margin), min(x1, int(crown[-1]) + 1 + margin)
    hy0, hy1 = y0, min(mask.shape[0], y0 + max(6, forge_core.round_half_up(0.35 * height)))
    return (hx0, hy0, hx1, hy1) if hx1 - hx0 >= 6 and hy1 - hy0 >= 6 else None


def _runs(flags: Sequence[bool]) -> list[tuple[int, int]]:
    """[start, end) of every run of True."""
    runs, start = [], None
    for index, flag in enumerate(list(flags) + [False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    return runs


def _rolling_median(values: Sequence[float], window: int) -> np.ndarray:
    data = np.asarray(values, np.float64)
    half = max(0, window // 2)
    return np.array([np.median(data[max(0, i - half):i + half + 1]) for i in range(data.size)])


def evaluate_take(frames: Sequence[QcFrame], reference: QcFrame, *, key: str, gates: dict, fps: float,
                  facing: str, kind: str, airborne: bool, returns_to_rest: bool, hit_tick: bool) -> dict:
    """Score one keyed take against the master as the job placed it (both reduced alike).

    Gates (game-opus55 take loop): opaque-area ratio to the master, feet drift, identity NCC of the head
    region (frame 0 against the master, every frame against frame 0), camera push-in or zoom, turning
    around (mirrored head NCC and mirrored silhouette), edge touch, flat background, detached extra
    objects, visible motion and, for one-shots, the return to the start pose. Returns the QC document with
    per-frame numbers, the failing gate ids, readable reasons with frame numbers and the longest usable
    window of frames that pass every per-frame gate."""
    count = len(frames)
    if count < 2:
        raise ValueError("a take needs at least two frames")
    ref_mask = reference.alpha > 127.5
    ref = _mask_stats(ref_mask)
    if ref is None:
        raise ValueError("the master does not land inside the video frame; check the job and the fit")
    ref_area, ref_height = max(1, ref["area"]), max(1, ref["height"])
    min_component = max(2, forge_core.round_half_up(0.001 * ref_area))
    ref_extra = _extra_area(ref_mask, min_component)
    head = _head_box(ref_mask, ref)
    search = max(3, forge_core.round_half_up(0.15 * ref_height))
    masks = [frame.alpha > 127.5 for frame in frames]
    stats = [_mask_stats(mask) for mask in masks]
    first = stats[0]
    if first is None:
        raise ValueError("frame 0 of the take is empty after keying")
    width = masks[0].shape[1]
    master_t = reference.luma[head[1]:head[3], head[0]:head[2]] if head else None
    frame0_t = frames[0].luma[head[1]:head[3], head[0]:head[2]] if head else None
    declared = key if key in forge_matte.DECLARED_KEYS else tuple(int(key[i:i + 2], 16) for i in (1, 3, 5))
    declared_rgb = np.asarray(forge_matte.DECLARED_KEYS.get(key, declared), np.float32)
    bg_tol = float(gates["background"]["tolerance"])
    start_ncc = None
    if master_t is not None:
        start_ncc = ncc_best(master_t, frames[0].luma, head[0], head[1], 3)

    rows: list[dict[str, Any]] = []
    for frame, mask, st in zip(frames, masks, stats):
        row: dict[str, Any] = {"edge": frame.edge}
        if st is None:
            row.update(empty=True, area=0.0, feet=None, lift=None, pose=1.0, iou0=0.0, extra=0.0, reach=0.0,
                       direct=0.0, mirror=0.0, ncc=None, nccMaster=None, nccMirror=None)
        else:
            row["area"] = st["area"] / ref_area
            row["feet"] = (st["ground"] - ref["ground"]) / ref_height
            row["lift"] = (ref["ground"] - st["ground"]) / ref_height
            row["height"] = st["height"] / max(1, first["height"])
            row["pose"] = float((mask ^ masks[0]).sum()) / ref_area
            row["iou0"] = _iou(mask, masks[0])
            row["extra"] = max(0, _extra_area(mask, min_component) - ref_extra) / ref_area
            if facing == "left":
                reach = first["box"][0] - st["box"][0]
            elif facing == "right":
                reach = st["box"][2] - first["box"][2]
            else:
                reach = max(first["box"][0] - st["box"][0], st["box"][2] - first["box"][2])
            row["reach"] = reach / ref_height
            dy = st["ground"] - first["ground"]
            row["direct"] = _iou(mask, _shift(masks[0], st["stance"] - first["stance"], dy))
            row["mirror"] = _iou(mask, _shift(masks[0][:, ::-1], st["stance"] - (width - first["stance"]), dy))
            if head:
                shift = (forge_core.round_half_up(st["top"][0] - first["top"][0]),
                         forge_core.round_half_up(st["top"][1] - first["top"][1]))
                centres = {(head[0], head[1]), (head[0] + shift[0], head[1] + shift[1])}

                def best(template: np.ndarray, image: np.ndarray = frame.luma) -> float | None:
                    scores = [ncc_best(template, image, x, y, search) for x, y in centres]
                    scores = [score for score in scores if score is not None]
                    return max(scores) if scores else None

                row["ncc"] = best(frame0_t)
                row["nccMaster"] = best(master_t)
                row["nccMirror"] = best(master_t[:, ::-1])
            else:
                row.update(ncc=None, nccMaster=None, nccMirror=None)
        if frame.raw is not None:
            covered = forge_core.dilate_square(frame.alpha > 2.0, 2)
            background = ~covered
            pixels = int(background.sum())
            raw8 = np.clip(frame.raw + 0.5, 0, 255).astype(np.uint8)
            estimate, _info = forge_matte.estimate_key(raw8, declared, ring=1)
            if pixels:
                distance = np.sqrt(((frame.raw[background] - estimate) ** 2).sum(axis=-1))
                row["bg"] = float((distance > bg_tol).sum()) / pixels
            else:
                row["bg"] = 0.0
            row["keyDrift"] = float(np.sqrt(((estimate - declared_rgb) ** 2).sum()))
        rows.append(row)

    gate_results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    reasons: list[str] = []
    bad: dict[str, list[bool]] = {}

    def record(gate: str, status: str, value: Any, threshold: Any, frames_bad: Sequence[int] = (),
               reason: str | None = None) -> None:
        gate_results[gate] = {"status": status, "value": _round(value), "threshold": _round(threshold),
                              "frames": [int(i) for i in frames_bad]}
        if status == "fail":
            failures.append(gate)
            if reason:
                reasons.append(f"{gate}: {reason}")

    # area ratio against the master
    area_gate = gates["area"]
    ratios = [row["area"] for row in rows]
    bad["area"] = [not (area_gate["min"] <= value <= area_gate["max"]) for value in ratios]
    median = float(np.median(ratios))
    share = sum(bad["area"]) / count
    frames_bad = [i for i, flag in enumerate(bad["area"]) if flag]
    ok = area_gate["min"] <= median <= area_gate["max"] and share <= area_gate["maxBadShare"]
    record("area", "pass" if ok else "fail", {"median": median, "min": min(ratios), "max": max(ratios)},
           [area_gate["min"], area_gate["max"]], frames_bad,
           f"opaque area {median:.2f}x the master (band {area_gate['min']}-{area_gate['max']}); out of band in "
           f"frames {_ranges(frames_bad)}")

    # feet drift (airborne actions: only the calm start and end spans)
    feet_gate = gates["feet"]
    calm = max(2, forge_core.round_half_up(0.25 * fps))
    checked = [True] * count
    if feet_gate.get("mode") == "start-end" or airborne:
        checked = [i < calm or i >= count - calm for i in range(count)]
    drifts = [abs(row["feet"]) if row["feet"] is not None else 1.0 for row in rows]
    bad["feet"] = [check and drift > feet_gate["max"] for check, drift in zip(checked, drifts)]
    frames_bad = [i for i, flag in enumerate(bad["feet"]) if flag]
    worst = max((drift for check, drift in zip(checked, drifts) if check), default=0.0)
    record("feet", "fail" if len(frames_bad) > feet_gate["badFrames"] else "pass", worst, feet_gate["max"],
           frames_bad, f"feet drift up to {worst:.1%} of the body height (limit {feet_gate['max']:.0%}) in frames "
           f"{_ranges(frames_bad)}")

    # identity NCC: frame 0 against the master, every frame against frame 0
    id_gate = gates["identity"]
    nccs = [row.get("ncc") for row in rows]
    if start_ncc is None or all(value is None for value in nccs):
        record("identity", "skipped", None, id_gate["min"])
        bad["identity"] = [False] * count
    else:
        bad["identity"] = [value is not None and value < id_gate["min"] for value in nccs]
        frames_bad = [i for i, flag in enumerate(bad["identity"]) if flag]
        low = min(value for value in nccs if value is not None)
        start_ok = start_ncc >= id_gate["start"]
        ok = start_ok and len(frames_bad) <= id_gate["badFrames"]
        reason = (f"frame 0 does not reproduce the master (head NCC {start_ncc:.2f} < {id_gate['start']})"
                  if not start_ok else
                  f"head NCC against frame 0 drops to {low:.2f} (limit {id_gate['min']}) in frames "
                  f"{_ranges(frames_bad)}: design drift or the face is covered")
        record("identity", "pass" if ok else "fail", {"start": start_ncc, "min": low},
               {"start": id_gate["start"], "min": id_gate["min"]}, frames_bad, reason)

    # camera push-in or zoom
    zoom_gate = gates["zoom"]
    base_area = max(1, first["area"])
    scales = [math.sqrt(max(row["area"], 0.0) * ref_area / base_area) for row in rows]
    bad["zoom"] = [False] * count
    if kind == "loop":
        window = max(5, forge_core.round_half_up(fps))
        smooth = _rolling_median(scales, window)
        deviation = np.abs(smooth - 1.0)
        limit = zoom_gate.get("loopMax", zoom_gate["max"] * 1.25)
        over = np.flatnonzero(deviation > limit)
        if over.size:
            onset = int(over[0])
            bad["zoom"] = [i >= onset for i in range(count)]
        record("zoom", "fail" if over.size else "pass", float(deviation.max()), limit,
               [int(i) for i in over], f"camera zoom: the body scale drifts {float(deviation.max()):.0%} from "
               f"frame 0 (limit {limit:.0%}) from frame {int(over[0]) if over.size else 0}")
    elif returns_to_rest:
        span = list(range(max(1, count - max(3, forge_core.round_half_up(0.4 * fps))), count))
        end_scale = float(np.median([scales[i] for i in span]))
        end_height = float(np.median([rows[i].get("height", 1.0) for i in span]))
        zoomed = (abs(end_scale - 1) > zoom_gate["max"] and abs(end_height - 1) > 0.75 * zoom_gate["max"]
                  and (end_scale - 1) * (end_height - 1) > 0)
        record("zoom", "fail" if zoomed else "pass", {"endScale": end_scale, "endHeight": end_height},
               zoom_gate["max"], span if zoomed else [],
               f"camera push-in: the body comes back {end_scale:.2f}x the size of frame 0 at the end (frames "
               f"{_ranges(span)})")
    else:
        record("zoom", "skipped", None, zoom_gate["max"])

    # turning around: the mirrored head matches better and the silhouette agrees
    turn_gate = gates["turn"]
    bad["turn"] = [False] * count
    if not turn_gate.get("enabled", True) or facing in ("front", "back"):
        record("turn", "skipped", None, turn_gate["margin"])
    else:
        flags = []
        for row in rows:
            direct, mirror = row.get("nccMaster"), row.get("nccMirror")
            silhouette = row["mirror"] >= row["direct"] - 0.02
            if direct is not None and mirror is not None:
                flags.append(mirror - direct > turn_gate["margin"] and silhouette)
            else:
                flags.append(row["mirror"] > row["direct"] + 0.1)
        runs = [run for run in _runs(flags) if run[1] - run[0] >= turn_gate["run"]]
        turned = sorted({i for a, b in runs for i in range(a, b)})
        bad["turn"] = [i in set(turned) for i in range(count)]
        evidence = max(((row.get("nccMirror") or 0) - (row.get("nccMaster") or 0) for row in rows), default=0.0)
        record("turn", "fail" if runs else "pass", evidence, turn_gate["margin"], turned,
               f"turned around in frames {_ranges(turned)} (the mirrored head matches {evidence:+.2f} better)")

    # edge touch at full resolution
    bad["edge"] = [row["edge"] for row in rows]
    frames_bad = [i for i, flag in enumerate(bad["edge"]) if flag]
    record("edge", "fail" if frames_bad else "pass", len(frames_bad), 0, frames_bad,
           f"the subject touches the frame edge in frames {_ranges(frames_bad)} (cut by the generator: never pad)")

    # background flatness (raw frames)
    bg_gate = gates["background"]
    if any("bg" in row for row in rows):
        values = [row.get("bg", 0.0) for row in rows]
        bad["background"] = [value > bg_gate["maxImpure"] for value in values]
        frames_bad = [i for i, flag in enumerate(bad["background"]) if flag]
        drift = max(row.get("keyDrift", 0.0) for row in rows)
        record("background", "fail" if len(frames_bad) > bg_gate["badFrames"] else "pass",
               {"worst": max(values), "keyDrift": drift}, bg_gate["maxImpure"], frames_bad,
               f"background not flat in frames {_ranges(frames_bad)} (worst {max(values):.2%} of the backdrop off "
               "the key)")
    else:
        bad["background"] = [False] * count
        record("background", "skipped", None, bg_gate["maxImpure"])

    # detached extra objects (dust, orbs, projectiles)
    extra_gate = gates["extra"]
    extras = [row["extra"] for row in rows]
    bad["extra"] = [value > extra_gate["max"] for value in extras]
    frames_bad = [i for i, flag in enumerate(bad["extra"]) if flag]
    record("extra", "fail" if len(frames_bad) > extra_gate["badFrames"] else "pass", max(extras), extra_gate["max"],
           frames_bad, f"extra objects detached from the body in frames {_ranges(frames_bad)} (worst "
           f"{max(extras):.1%} of the body area)")

    # the action happens
    motion_gate = gates["motion"]
    poses = [row["pose"] for row in rows]
    peak = float(max(poses))
    record("motion", "pass" if peak >= motion_gate["min"] else "fail", peak, motion_gate["min"], [],
           f"no visible action (peak pose change {peak:.1%} of the body area, needs {motion_gate['min']:.1%})")

    # one-shots return to the start pose
    end_gate = gates["endPose"]
    if end_gate.get("enabled", True) and returns_to_rest and kind == "oneshot":
        tail = max(row["iou0"] for row in rows[-3:])
        record("end-pose", "pass" if tail >= end_gate["min"] else "fail", tail, end_gate["min"],
               [] if tail >= end_gate["min"] else list(range(count - 3, count)),
               f"the clip does not return to the start pose (silhouette IoU {tail:.2f} with frame 0)")
    else:
        record("end-pose", "skipped", None, end_gate["min"])

    # motion landmarks (take frame numbers) for retime: onset, settle, impact, cancel, takeoff, land
    threshold = max(0.01, 0.5 * motion_gate["min"])
    moving = [i for i, value in enumerate(poses) if value > threshold]
    motion: dict[str, Any] = {"peak": peak, "peakFrame": int(np.argmax(poses)),
                              "onset": moving[0] if moving else None, "settle": moving[-1] if moving else None}
    if moving and hit_tick:
        impact = max(range(moving[0], moving[-1] + 1), key=lambda i: (rows[i]["reach"], -i))
        motion["impact"] = int(impact)
        after = [i for i in range(impact + 1, moving[-1] + 1) if poses[i] < 0.5 * poses[impact]]
        motion["cancel"] = after[0] if after else None
    if airborne:
        lifted = [i for i, row in enumerate(rows) if row["lift"] is not None and row["lift"] > 0.03]
        motion["takeoff"] = lifted[0] if lifted else None
        motion["land"] = lifted[-1] + 1 if lifted and lifted[-1] + 1 < count else None

    usable = [not any(bad[name][i] for name in bad) for i in range(count)]
    window = None
    runs = sorted(_runs(usable), key=lambda run: (run[1] - run[0], -run[0]), reverse=True)
    if runs:
        a, b = runs[0]
        if kind == "loop":
            if b - a >= max(8, forge_core.round_half_up(gates["window"]["loopSeconds"] * fps)):
                window = [a, b]
        elif moving and a <= max(0, moving[0] - 1) and b >= min(count, moving[-1] + 1):
            window = [a, b]
    per_frame = {name: [_round(row.get(name), 4) for row in rows]
                 for name in ("area", "feet", "lift", "pose", "iou0", "extra", "reach", "direct", "mirror", "ncc",
                              "nccMaster", "nccMirror", "bg")}
    per_frame["edge"] = [bool(row["edge"]) for row in rows]
    per_frame["usable"] = usable
    per_frame["bad"] = {name: [i for i, flag in enumerate(flags) if flag] for name, flags in bad.items()}
    return {
        "schema": QC_SCHEMA,
        "status": "fail" if failures else "pass",
        "failures": failures,
        "reasons": reasons,
        "gates": gate_results,
        "frames": count,
        "fps": _round(fps),
        "usableWindow": window,
        "usableFrames": int(sum(usable)),
        "motion": _round(motion),
        "reference": {"area": ref_area, "height": ref_height, "headBox": list(head) if head else None,
                      "search": search},
        "perFrame": per_frame,
    }


def qc_take(job_path: Path, clean_dir: Path, raw_dir: Path | None, *, key: str, gates: dict, fps: float,
            facing: str, kind: str, airborne: bool, returns_to_rest: bool, hit_tick: bool) -> dict:
    """Load a keyed take (video2dsprite process frames-clean and frames-raw) and the master as the job placed it
    in the video, reduce both alike and run evaluate_take."""
    clean = _pngs(clean_dir)
    if len(clean) < 2:
        raise ValueError(f"{clean_dir.name} holds {len(clean)} keyed frames; a take needs at least two")
    raw = _pngs(raw_dir) if raw_dir is not None and raw_dir.is_dir() else []
    if raw and len(raw) != len(clean):
        raw = []
    job = register_clip.load_job(job_path)
    view = register_clip.load_master_view(job)
    with Image.open(clean[0]) as probe:
        size = probe.size
    fit = register_clip.video_fit(job.canvas, size, "auto")
    placed = register_clip.place_in_video(view, job, fit)
    box = forge_core.subject_bbox(placed[..., 3], 127)
    if box is None:
        raise ValueError("the master does not land inside the video frame")
    factor = max(1, forge_core.round_half_up((box[3] - box[1]) / QC_SUBJECT_PX))
    edge_px = int(gates["edge"]["px"])
    reference = qc_frame(placed, None, factor, edge_px)
    frames = []
    for index, path in enumerate(clean):
        rgba = np.asarray(Image.open(path).convert("RGBA"))
        if (rgba.shape[1], rgba.shape[0]) != size:
            raise ValueError(f"{path.name} is {rgba.shape[1]}x{rgba.shape[0]}; the take is {size[0]}x{size[1]}")
        rgb = np.asarray(Image.open(raw[index]).convert("RGB")) if raw else None
        frames.append(qc_frame(rgba, rgb, factor, edge_px))
    result = evaluate_take(frames, reference, key=key, gates=gates, fps=fps, facing=facing, kind=kind,
                           airborne=airborne, returns_to_rest=returns_to_rest, hit_tick=hit_tick)
    result["reduction"] = factor
    result["videoSize"] = list(size)
    result["fitApplied"] = fit.applied
    return result


# --------------------------------------------------------------------------- plan

def default_gates(action: str, preset: ActionPreset, klass: str, view: str) -> dict:
    """BASE_GATES tuned per action: runs bob in flight, jumps leave the ground, hovering classes have no
    feet, front and back views cannot show a mirrored head, loops and held endings skip the end pose."""
    gates = json.loads(json.dumps(BASE_GATES))
    gates["motion"]["min"] = preset.min_motion
    if action == "run":
        gates["feet"]["max"] = 0.10
    if preset.airborne:
        gates["feet"]["mode"] = "start-end"
    if klass in HOVER_CLASSES:
        gates["feet"]["max"] = max(gates["feet"]["max"], 0.08)
    if view in ("front", "back"):
        gates["turn"]["enabled"] = False
    if preset.kind == "loop" or not preset.returns_to_rest:
        gates["endPose"]["enabled"] = False
    return gates


def _parse_list(text: str, allowed: Sequence[str], name: str) -> list[str]:
    items = [item.strip().lower() for item in str(text).split(",") if item.strip()]
    unknown = [item for item in items if item not in allowed]
    if not items or unknown:
        raise ValueError(f"{name} takes a comma list of {', '.join(allowed)}; got {text!r}")
    return list(dict.fromkeys(items))


def _parse_view_masters(values: Sequence[str]) -> dict[str, Path]:
    found = {}
    for value in values or []:
        view, sep, path = str(value).partition("=")
        view = view.strip().lower()
        if not sep or view not in VIEWS or not path.strip():
            raise ValueError(f"--view-master needs VIEW=master.json with VIEW one of {', '.join(VIEWS)}; got {value!r}")
        found[view] = Path(path.strip())
    return found


def _master_record(master: Master, stage: Path, view: str) -> dict:
    folder = stage / "master" / view
    folder.mkdir(parents=True)
    json_copy, png_copy = folder / "master.json", folder / master.png_path.name
    shutil.copyfile(master.json_path, json_copy)
    shutil.copyfile(master.png_path, png_copy)
    return {"json": forge_core.file_ref(json_copy, stage), "png": forge_core.file_ref(png_copy, stage),
            "sourceName": master.json_path.name, "name": master.name, "identityRecap": master.identity_recap,
            "facing": master.facing, "finish": master.finish, "class": master.klass, "key": master.key,
            "size": list(master.size), "subjectBox": list(master.box), "anchor": list(master.anchor),
            "area": master.area, "framing": _scrub(master.framing)}


def cmd_plan(args: argparse.Namespace) -> dict:
    """Write a new set folder: master copies, set_plan.json (and nothing else; run does the work)."""
    master = load_master(Path(args.master))
    library = load_library(Path(args.library) if args.library else LIBRARY_FILE)
    actions = _parse_list(args.actions, tuple(ACTION_PRESETS), "--actions")
    views = _parse_list(args.views, VIEWS, "--views")
    extra_masters = _parse_view_masters(args.view_master)
    masters: dict[str, Master] = {}
    for view in views:
        if view in extra_masters:
            masters[view] = load_master(extra_masters[view])
        elif view == "side" or ("side" not in views and view == views[0]):
            masters[view] = master   # --master is the side still, or the first view's when side is not planned
        else:
            raise ValueError(f"the {view} view needs its own approved still: --view-master {view}=<master.json>")
        if masters[view].key != master.key:
            raise ValueError(f"every view must share one key colour; {view} uses {masters[view].key}, the master "
                             f"{master.key}")
    finish = args.finish or master.finish
    target = args.target_height or forge_core.round_half_up(TARGET_HEIGHT[finish] * CLASS_HEIGHT.get(master.klass, 1.0))
    if target < 8:
        raise ValueError("--target-height must be at least 8 px")
    fps = args.fps if args.fps is not None else (12 if finish == "pixel" else None)
    if fps is not None and not (1 <= fps <= 60 and float(fps).is_integer()):
        raise ValueError("--fps must be a whole number 1-60 (or leave it out to keep the clip's own rate)")
    if not float(args.duration).is_integer():
        raise ValueError("--duration must be whole seconds (route_media.py video takes an integer --duration)")
    formats = _parse_list(args.formats, ("png", "webm", "packed"), "--formats")
    if "png" not in formats:
        formats.insert(0, "png")
    style = args.style or master.style or ("16-bit pixel-art" if finish == "pixel" else "HD hand-drawn 2D game-art")
    entries_spec = [(view, action) for view in views for action in actions]
    ref_action = "idle" if "idle" in actions else actions[0]
    ref_view = "side" if "side" in views else views[0]
    ref_id = ref_action if ref_view == "side" else f"{ref_view}-{ref_action}"
    output = Path(args.output_dir)
    with forge_core.staged_output(output) as stage:
        records = {view: _master_record(item, stage, view) for view, item in masters.items()}
        palette = None
        if args.palette:
            source = Path(args.palette)
            if not source.is_file():
                raise ValueError(f"--palette {source.name} does not exist")
            target_path = stage / "master" / ("palette" + source.suffix.lower())
            shutil.copyfile(source, target_path)
            palette = forge_core.file_ref(target_path, stage)
        plan: dict[str, Any] = {
            "schema": PLAN_SCHEMA,
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "createdAt": _now(),
            "name": master.name,
            "class": master.klass,
            "key": master.key,
            "keyRgb": list(master.key_rgb),
            "style": style,
            "pronoun": args.pronoun,
            "views": views,
            "masters": records,
            "generation": {"duration": args.duration, "resolution": args.resolution, "maxTakes": args.max_takes,
                           "jobs": args.jobs, "pinLastFrame": args.pin_last, "timeoutSeconds": args.gen_timeout},
            "finish": {"mode": finish, "targetHeight": int(target), "scaleRefAction": ref_id, "palette": palette},
            "package": {"formats": formats, "tiers": args.tiers, "fps": int(fps) if fps else None},
            "library": {"name": LIBRARY_FILE.name if not args.library else Path(args.library).name,
                        "sha256": library.sha256},
            "clauses": library.clauses,
            "gatefix": library.gatefix,
            "actions": [],
        }
        for view, action in entries_spec:
            preset = ACTION_PRESETS[action]
            item = masters[view]
            hover = item.klass in HOVER_CLASSES
            pin = {"auto": preset.pin_last, "on": preset.returns_to_rest, "off": False}[args.pin_last]
            entry = {
                "id": action if view == "side" else f"{view}-{action}",
                "action": action, "view": view, "kind": preset.kind,
                "loopKind": ("hover" if hover and preset.loop_kind == "idle" else preset.loop_kind),
                "gaitState": preset.gait_state, "prepareAction": preset.prepare_action,
                "retimeKind": preset.retime_kind, "placement": placement(item, preset.canvas),
                "pinLastFrame": bool(pin), "lock": "none" if hover else preset.lock, "airborne": preset.airborne,
                "hitTick": preset.hit_tick, "returnsToRest": preset.returns_to_rest,
                "motion": library.templates.get(f"{action}-hover", library.templates[action]) if hover
                else library.templates[action],
                "negatives": library.negatives.get(action, ""),
                "gates": default_gates(action, preset, item.klass, view),
            }
            entry["prompt"] = build_prompt(entry, plan, [])
            entry["lint"] = lint_findings(entry["prompt"], master.key)
            plan["actions"].append(entry)
        plan["estimate"] = {"clips": len(plan["actions"]), "maxClips": len(plan["actions"]) * args.max_takes,
                            "maxVideoSeconds": len(plan["actions"]) * args.max_takes * args.duration,
                            "note": "route_media.py prints and ledgers the real price of every clip"}
        forge_core.write_json(stage / PLAN_FILE, _scrub(plan))
    final = output.parent.resolve() / output.name
    return {"output": str(final), "metadata": str(final / PLAN_FILE), "plan": str(final / PLAN_FILE),
            "name": master.name, "finish": finish, "targetHeight": int(target),
            "actions": [entry["id"] for entry in plan["actions"]],
            "canvases": {entry["id"]: entry["placement"]["canvas"] for entry in plan["actions"]},
            "pinned": [entry["id"] for entry in plan["actions"] if entry["pinLastFrame"]],
            "maxClips": plan["estimate"]["maxClips"],
            "lintWarnings": sum(len(entry["lint"]) for entry in plan["actions"])}


# --------------------------------------------------------------------------- finishing (seam)

def resolve_finisher() -> tuple[Path | None, str]:
    """finish_frames.py beside this script, or the private stand-in while it is not installed.
    FORGE_FINISH_FRAMES=standin forces the stand-in; any other value names a finisher script."""
    value = os.environ.get(FINISH_ENV, "").strip()
    if value.lower() == "standin":
        return None, "sprite_set stand-in"
    if value:
        path = Path(value)
        if not path.is_file():
            raise ValueError(f"{FINISH_ENV} names {path.name}, which does not exist")
        return path, path.name
    if FINISH_SCRIPT.is_file():
        return FINISH_SCRIPT, FINISH_SCRIPT.name
    return None, "sprite_set stand-in (finish_frames.py is not installed)"


def _palette_image(colours: np.ndarray) -> Image.Image:
    """A P-mode palette image for Image.quantize(palette=...); unused slots repeat the first colour."""
    colours = np.asarray(colours, np.uint8).reshape(-1, 3)[:256]
    flat = np.concatenate([colours, np.repeat(colours[:1], 256 - len(colours), axis=0)]).reshape(-1)
    image = Image.new("P", (1, 1))
    image.putpalette([int(value) for value in flat])
    return image


def _load_palette(path: Path) -> np.ndarray:
    if path.suffix.lower() == ".json":
        data = forge_core.read_json(path)
        values = data.get("colors") or data.get("colours") or data.get("palette") if isinstance(data, dict) else data
        colours = [[int(str(value).lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)] for value in values or []]
    else:
        rgba = np.asarray(forge_core.load_rgba(path)[0])
        colours = np.unique(rgba[rgba[..., 3] > 0][:, :3], axis=0).tolist()
    if not 1 <= len(colours) <= 256:
        raise ValueError(f"palette {path.name} needs 1-256 colours; it has {len(colours)}")
    return np.asarray(colours, np.uint8)


def standin_finish(mode: str, frames_dir: Path, out: Path, target_height: int, scale_ref: Path | None,
                   palette: Path | None, colours: int = 32) -> dict:
    """Private stand-in for finish_frames.py (same arguments, same one-line summary).

    The rest pose is frame 0: scale = target height / its body height, or the scale reference's scale times
    sqrt(rest-area ratio) clamped to +-3% (game-opus55 scale_ref). The output grid is pinned to the rest
    stance anchor on the feet line and every frame is resampled with the premultiplied box filter; hd keeps
    full colour and clean alpha, pixel binarises alpha at 0.5 and maps every frame to one palette (median
    cut over all frames, or --palette) without dither."""
    files = _pngs(frames_dir)
    if not files:
        raise ValueError(f"no PNG frames in {frames_dir.name}")
    first = np.asarray(forge_core.load_rgba(files[0])[0])
    rest = _mask_stats(first[..., 3] > 127)
    if rest is None:
        raise ValueError("the rest frame (frame 0) is empty")
    band = max(2, forge_core.round_half_up(0.04 * rest["height"]))
    fx, fy = forge_core.anchor_from_mask(first[..., 3] > 127, "stance", band_rows=band, min_run=2)
    reference = None
    if scale_ref is not None:
        reference = forge_core.read_json(scale_ref)
        if not isinstance(reference, dict) or reference.get("schema") != STANDIN_SCHEMA:
            raise ValueError(f"--scale-ref {Path(scale_ref).name} is not a {STANDIN_SCHEMA} record")
        correction = float(np.clip(math.sqrt(float(reference["restArea"]) / max(1, rest["area"])), 0.97, 1.03))
        scale = float(reference["scale"]) * correction
    else:
        scale = target_height / rest["height"]
    boxes = []
    for path in files:
        with Image.open(path) as image:
            bbox = image.convert("RGBA").getchannel("A").getbbox()
        if bbox:
            boxes.append(bbox)
    x0, y0 = min(box[0] for box in boxes), min(box[1] for box in boxes)
    x1, y1 = max(box[2] for box in boxes), max(box[3] for box in boxes)
    ax, ay = math.ceil((fx - x0) * scale) + 1, math.ceil((fy - y0) * scale) + 1
    width = ax + max(0, math.ceil((x1 - fx) * scale)) + 1
    height = ay + max(0, math.ceil((y1 - fy) * scale)) + 1
    finished = []
    for path in files:
        pixels = np.asarray(forge_core.load_rgba(path)[0])
        small = np.asarray(forge_core.resample_rgba(pixels, scale, "box", anchor_src=(fx, fy), anchor_dst=(ax, ay),
                                                    out_size=(width, height)))
        if mode == "hd":
            small = np.asarray(forge_core.alpha_hygiene(small, mode="floor", floor=4)[0])
        else:
            small = small.copy()
            small[..., 3] = np.where(small[..., 3] >= 128, 255, 0).astype(np.uint8)
            small[small[..., 3] == 0] = 0
        finished.append(small)
    palette_record = None
    if mode == "pixel":
        if palette is not None:
            table = _load_palette(palette)
            source = {"file": Path(palette).name, "sha256": forge_core.sha256_file(palette)}
        else:
            opaque = np.concatenate([frame[frame[..., 3] == 255][:, :3] for frame in finished])
            if opaque.size == 0:
                raise ValueError("pixel finish: no opaque pixels to build a palette from")
            step = max(1, len(opaque) // 200000)
            strip = Image.fromarray(np.ascontiguousarray(opaque[::step]).reshape(1, -1, 3), "RGB")
            quantised = strip.quantize(colors=colours, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            used = sorted(set(np.asarray(quantised).reshape(-1).tolist()))
            table = np.asarray(quantised.getpalette()[:768], np.uint8).reshape(-1, 3)[used]
            source = {"method": "median cut over every frame", "colours": colours}
        lookup = _palette_image(table)
        for frame in finished:
            rgb = Image.fromarray(np.ascontiguousarray(frame[..., :3]), "RGB")
            mapped = np.asarray(rgb.quantize(palette=lookup, dither=Image.Dither.NONE).convert("RGB"))
            frame[..., :3] = np.where(frame[..., 3:] > 0, mapped, 0)
        palette_record = {**source, "size": int(len(table)), "colors": ["#%02x%02x%02x" % tuple(int(v) for v in c)
                                                                        for c in table]}
    with forge_core.staged_output(out) as stage:
        (stage / "frames").mkdir()
        for index, frame in enumerate(finished):
            forge_core.save_png(frame, stage / "frames" / f"frame_{index:06d}.png")
        record = {"schema": STANDIN_SCHEMA, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION,
                                                     "note": "stand-in until finish_frames.py is installed"},
                  "mode": mode, "targetHeight": int(target_height), "scale": scale,
                  "restHeight": rest["height"], "restArea": rest["area"], "sourceAnchor": [fx, fy],
                  "anchor": [ax, ay], "size": [width, height], "frames": len(finished),
                  "resampler": "box (premultiplied area)", "alpha": "floor 4" if mode == "hd" else "binary at 0.5",
                  "palette": palette_record,
                  "scaleRef": None if scale_ref is None else {"file": Path(scale_ref).name,
                                                              "sha256": forge_core.sha256_file(scale_ref)}}
        forge_core.write_json(stage / "finish.json", record)
    final = out.parent.resolve() / out.name
    return {"output": str(final), "metadata": str(final / "finish.json"), "framesDir": str(final / "frames"),
            "frames": len(finished), "mode": mode, "scale": scale, "anchor": [ax, ay], "size": [width, height]}


def rebind_selection(selection: Path, frames_dir: Path, expected: int, out: Path) -> dict:
    """Bind a forge-frame-selection made on the registered frames to the finished frames (one finished frame
    per registered frame, same order): new sourceDirectory, sourceHashes and sourceFiles; indices, durations,
    events and the loop policy are kept."""
    document = forge_core.read_json(selection)
    files = _pngs(frames_dir)
    if len(files) != expected:
        raise ValueError(f"the finisher wrote {len(files)} frames for {expected} registered frames; the set needs one "
                         "finished frame per registered frame")
    start, end = int(document["start"]), int(document["endExclusive"])
    with forge_core.staged_output(out) as stage:
        rebound = dict(document)
        rebound["sourceDirectory"] = forge_core.manifest_path(frames_dir, stage)
        rebound["sourceHashes"] = [forge_core.sha256_file(files[i]) for i in range(start, end)]
        rebound["sourceFiles"] = [files[i].name for i in range(start, end)]
        rebound["reboundFrom"] = {"path": forge_core.manifest_path(selection, stage),
                                  "sha256": forge_core.sha256_file(selection)}
        rebound["method"] = f"{document.get('method', '')}; rebound 1:1 to the finished frames (sprite_set)".strip("; ")
        forge_core.write_json(stage / "selection.json", rebound)
    return {"selection": out / "selection.json", "frames": len(document.get("sourceIndices") or []),
            "start": start, "endExclusive": end}


def _tick_grid(fps: int) -> tuple[int, int]:
    """(tick Hz, ticks per frame): the 60 Hz forge grid when it divides evenly, else one tick per frame."""
    fps = int(fps)
    return (60, 60 // fps) if 60 % fps == 0 else (fps, 1)


# --------------------------------------------------------------------------- the set runner

_CLIP_SUFFIXES = (".mp4", ".webm", ".mov", ".m4v")
_LAST_FRAME_WORDS = re.compile(r"last[-_ ]?frame|unrecognized arguments", re.I)


def _find_clip(media: Path) -> Path | None:
    """A clip already in the take's media folder (generated earlier, or dropped in by the user or host)."""
    if not media.is_dir():
        return None
    clips = sorted((path for path in media.iterdir() if path.is_file() and path.suffix.lower() in _CLIP_SUFFIXES),
                   key=lambda path: (path.name != "clip.mp4", path.name))
    return clips[0] if clips else None


class SetRunner:
    """set_plan.json + set_state.json: every verb after plan works through this."""

    def __init__(self, plan_path: Path):
        plan_path = Path(plan_path)
        if plan_path.is_dir():
            plan_path = plan_path / PLAN_FILE
        if not plan_path.is_file():
            raise ValueError(f"no {PLAN_FILE} at {plan_path.as_posix()}")
        self.plan_path = plan_path.resolve()
        self.root = self.plan_path.parent
        try:
            plan = forge_core.read_json(self.plan_path)
        except ValueError as error:
            raise ValueError(f"{PLAN_FILE} is not valid JSON: {error}") from None
        if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
            raise ValueError(f"{self.plan_path.name} is not a {PLAN_SCHEMA} document")
        self.plan = plan
        self.entries = {entry["id"]: entry for entry in plan["actions"]}
        self.plan_sha = forge_core.sha256_file(self.plan_path)
        self.state_path = self.root / STATE_FILE
        if self.state_path.is_file():
            state = forge_core.read_json(self.state_path)
            if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
                raise ValueError(f"{STATE_FILE} is not a {STATE_SCHEMA} document")
            self.state = state
        else:
            self.state = {"schema": STATE_SCHEMA, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
                          "plan": {"path": PLAN_FILE, "sha256": self.plan_sha}, "createdAt": _now(), "runs": 0,
                          "actions": {}, "review": None}
        self.notes: list[str] = []
        self.media_argv: list[str] | None = None
        self.media_name = ""

    # ----------------------------------------------------------------- bookkeeping

    def rel(self, path: Path | str) -> str:
        return forge_core.manifest_path(path, self.root)

    def save(self) -> None:
        self.state["updatedAt"] = _now()
        forge_core.write_json(self.state_path, _scrub(self.state), no_clobber=False)

    def astate(self, ident: str) -> dict:
        if ident not in self.entries:
            raise ValueError(f"no action {ident!r} in {PLAN_FILE}; it plans {', '.join(self.entries)}")
        return self.state["actions"].setdefault(ident, {"status": "pending", "round": 1, "fixes": [], "job": None,
                                                        "takes": [], "chosen": None, "accepted": None,
                                                        "output": None, "message": None})

    def take(self, ident: str, number: int) -> dict:
        for record in self.astate(ident)["takes"]:
            if record["take"] == number:
                return record
        raise ValueError(f"{ident} has no take {number}")

    def order(self, only: Sequence[str] | None = None) -> list[str]:
        ids = [entry["id"] for entry in self.plan["actions"]]
        ref = self.plan["finish"].get("scaleRefAction")
        if ref in ids:
            ids.remove(ref)
            ids.insert(0, ref)
        if only:
            unknown = [ident for ident in only if ident not in ids]
            if unknown:
                raise ValueError(f"--actions names {unknown}, not planned (planned: {', '.join(ids)})")
            ids = [ident for ident in ids if ident in only]
        return ids

    def master(self, ident: str) -> dict:
        return self.plan["masters"][self.entries[ident]["view"]]

    def take_dir(self, ident: str, number: int) -> Path:
        return self.root / "actions" / ident / "takes" / f"t{number:02d}"

    def log_take(self, ident: str, record: dict) -> None:
        """Append the take's verdict to takes.jsonl once (an audit log; the state is the truth)."""
        if record.get("logged"):
            return
        qc = record.get("qc") or {}
        line = {"schema": TAKE_SCHEMA, "at": _now(), "action": ident, "take": record["take"],
                "round": record["round"], "status": record["status"], "failures": record.get("failures", []),
                "reasons": record.get("reasons", []), "badFrames": qc.get("badFrames", {}),
                "usableWindow": qc.get("usableWindow"), "fixesUsed": record.get("fixes", []),
                "fixNext": record.get("fixNext", []), "route": record.get("route"),
                "prompt": record.get("prompt"), "clip": record.get("clip"), "message": record.get("message")}
        with open(self.root / TAKES_FILE, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(_scrub(line), ensure_ascii=False, allow_nan=False) + "\n")
        record["logged"] = True

    def stage(self, record: dict, name: str, make: Callable[[Path], dict]) -> dict:
        """Run one tool step into <take>/<name> unless the state already records it (resume). A folder the
        state does not know (a crash between publish and save) is kept aside as <name>.orphan-N."""
        stages = record.setdefault("stages", {})
        out = self.root / record["dir"] / name
        done = stages.get(name)
        if done and out.exists():
            return done
        if os.path.lexists(out):
            self.notes.append(f"kept an unrecorded {self.rel(out)} aside as {_move_aside(out).name}")
        started = time.perf_counter()
        result = make(out)
        result["dir"] = self.rel(out)
        result["seconds"] = round(time.perf_counter() - started, 2)
        stages[name] = _scrub(result)
        self.save()
        return stages[name]

    def check_masters(self) -> None:
        for view, record in self.plan["masters"].items():
            for role in ("json", "png"):
                path = self.root / record[role]["path"]
                if not path.is_file() or forge_core.sha256_file(path) != record[role]["sha256"]:
                    raise ValueError(f"the {view} master {record[role]['path']} is missing or changed since the plan; "
                                     "plan a new set for a new master")

    # ----------------------------------------------------------------- jobs and prompts

    def ensure_job(self, ident: str) -> dict:
        st = self.astate(ident)
        out = self.root / "actions" / ident / "job"
        if st.get("job") and (out / "registration_job.json").is_file():
            return st["job"]
        if os.path.lexists(out):
            self.notes.append(f"kept an unrecorded {self.rel(out)} aside as {_move_aside(out).name}")
        entry, master = self.entries[ident], self.master(ident)
        place = entry["placement"]
        out.parent.mkdir(parents=True, exist_ok=True)
        prompt_file = out.parent / f".prompt-{os.getpid()}.txt"
        prompt_file.write_bytes(build_prompt(entry, self.plan, []).encode("utf-8"))
        args = ["prepare", "--master", self.root / master["png"]["path"], "--action", entry["prepareAction"],
                "--output-dir", out, "--canvas", "{},{}".format(*place["canvas"]),
                "--root", "{!r},{!r}".format(*place["root"]), "--scale", repr(place["scale"]),
                "--anchor", "{!r},{!r}".format(*place["anchor"]), "--key", self.plan["key"],
                "--master-key", self.plan["key"], "--allow-key-conflict", "--margin", place["margin"],
                "--action-padding", ",".join(str(v) for v in place["padding"]),
                "--duration", self.plan["generation"]["duration"], "--facing", master["facing"],
                "--subject", master["name"], "--prompt-file", prompt_file]
        try:
            summary, warnings = run_tool(SCRIPTS["prepare_i2v_input"], args)
        finally:
            prompt_file.unlink(missing_ok=True)
        job = out / "registration_job.json"
        st["job"] = {"dir": self.rel(out), "job": self.rel(job), "sha256": forge_core.sha256_file(job),
                     "input": self.rel(out / "input.png"), "referenceScale": summary.get("referenceScale"),
                     "keyColor": summary.get("keyColor"), "padding": summary.get("padding"),
                     "warnings": len(warnings)}
        self.save()
        return st["job"]

    def open_take(self, ident: str) -> dict:
        """The take to work on: the unfinished one of this round, or a new take with the prompt and every fix."""
        st = self.astate(ident)
        last = st["takes"][-1] if st["takes"] else None
        if last and last["round"] == st["round"] and last["status"] in ("open", "generated", "no-route"):
            return last
        number = len(st["takes"]) + 1
        folder = self.take_dir(ident, number)
        folder.mkdir(parents=True, exist_ok=True)
        prompt = build_prompt(self.entries[ident], self.plan, [fix["text"] for fix in st["fixes"]])
        payload = prompt.encode("utf-8")
        path = folder / "prompt.txt"
        if path.exists() and path.read_bytes() != payload:
            _move_aside(path)
        if not path.exists():
            with open(path, "xb") as stream:
                stream.write(payload)
        record = {"take": number, "round": st["round"], "dir": self.rel(folder), "status": "open", "createdAt": _now(),
                  "prompt": {"path": self.rel(path), "sha256": forge_core.sha256_bytes(payload)},
                  "fixes": [fix["id"] for fix in st["fixes"]], "lint": lint_findings(prompt, self.plan["key"]),
                  "stages": {}}
        st["takes"].append(record)
        st["status"] = "generating"
        self.save()
        return record

    # ----------------------------------------------------------------- generation

    def resolve_media(self, override: str | None) -> None:
        if override:
            path = Path(override)
            if not path.is_file():
                raise ValueError(f"--media-cli {path.name} does not exist")
            self.media_argv, self.media_name = [sys.executable, str(path.resolve())], path.name
            return
        fake = os.environ.get(MEDIA_FAKE_ENV, "").strip()
        if fake and Path(fake).is_file():
            self.media_argv, self.media_name = [sys.executable, str(Path(fake).resolve())], f"{MEDIA_FAKE_ENV}"
            return
        if ROUTE_MEDIA.is_file():
            self.media_argv, self.media_name = [sys.executable, str(ROUTE_MEDIA)], "generate2dmedia/route_media.py"
            return
        self.media_argv, self.media_name = None, "generate2dmedia/route_media.py (not installed)"

    def _call_media(self, args: Sequence[Any], timeout: float) -> dict:
        argv = [*self.media_argv, *(str(arg) for arg in args)]
        try:
            done = subprocess.run(argv, capture_output=True, timeout=timeout, env=_child_env(), check=False)
        except subprocess.TimeoutExpired:
            return {"code": -1, "summary": None, "message": f"timed out after {timeout:g} s"}
        stderr = done.stderr.decode("utf-8", "replace")
        return {"code": done.returncode, "summary": _json_line(done.stdout.decode("utf-8", "replace")),
                "message": _error_line(stderr) if done.returncode else ""}

    def generate(self, ident: str, record: dict, timeout: float) -> dict:
        """One clip through the media CLI (a worker thread: no state changes here)."""
        folder = self.root / record["dir"]
        media = folder / "media"
        clip = _find_clip(media)
        if clip is not None:
            return {"status": "ok", "route": record.get("route") or "supplied", "artifact": clip, "adopted": True,
                    "pinned": None}
        if self.media_argv is None:
            return {"status": "no-route", "message": f"{self.media_name}: no media CLI to generate with"}
        if os.path.lexists(media):
            _move_aside(media)
        job = self.root / self.astate(ident)["job"]["dir"]
        generation = self.plan["generation"]
        base = ["video", "--prompt-file", folder / "prompt.txt", "--reference", job / "input.png",
                "--duration", _duration_arg(generation["duration"]), "--resolution", generation["resolution"],
                "--out-dir", media]
        pin = bool(self.entries[ident]["pinLastFrame"])
        result = self._call_media(base + (["--last-frame", job / "input.png"] if pin else []), timeout)
        pin_note = None
        if pin and result["code"] not in (0, EXIT_NO_ROUTE) and _LAST_FRAME_WORDS.search(result["message"] or ""):
            if os.path.lexists(media):
                _move_aside(media)
            result = self._call_media(base, timeout)
            pin, pin_note = False, "the route takes no last frame; generated without it"
        if result["code"] == EXIT_NO_ROUTE:
            return {"status": "no-route", "message": result["message"] or "no generation route (exit 3)"}
        summary = result["summary"] or {}
        if result["code"] != 0 or summary.get("status", "ok") != "ok":
            return {"status": "error", "message": result["message"] or f"media CLI status {summary.get('status')!r}"}
        named = Path(str(summary.get("artifact") or ""))
        candidates = [named] if named.is_absolute() else [media / named, Path.cwd() / named]
        artifact = next((path for path in candidates if str(named) not in ("", ".") and path.is_file()), None)
        artifact = artifact or _find_clip(media)
        if artifact is None:
            return {"status": "error", "message": f"the media CLI reported {named.name or 'no artifact'}, which does "
                                                  "not exist"}
        if media.resolve() not in artifact.resolve().parents:   # keep the set self-contained
            media.mkdir(parents=True, exist_ok=True)
            copy = media / artifact.name
            if not copy.exists():
                shutil.copyfile(artifact, copy)
            artifact = copy
        flags = [summary.get(key) for key in ("lastFrame", "last_frame", "lastFrameUsed", "pinned")]
        if pin and any(flag is False or (isinstance(flag, str) and flag.lower() in ("unsupported", "ignored", "no"))
                       for flag in flags):
            pin, pin_note = False, "the route ignored the last frame"
        return {"status": "ok", "route": summary.get("route"), "artifact": artifact, "summary": summary,
                "pinned": pin, "pinNote": pin_note}

    def record_generation(self, ident: str, record: dict, result: dict) -> str:
        st = self.astate(ident)
        folder = self.root / record["dir"]
        if result["status"] == "ok":
            clip = Path(result["artifact"])
            record["clip"] = {"path": self.rel(clip), "sha256": forge_core.sha256_file(clip)}
            record["route"] = _scrub(result.get("route")) or "unknown"
            record["pinnedLastFrame"] = result.get("pinned")
            if result.get("pinNote"):
                record["pinNote"] = result["pinNote"]
            route_file = folder / "route.json"
            if not route_file.exists():
                _write_new_json(route_file, _scrub({
                    "schema": "video2dsprite.sprite_set_route.v1", "action": ident, "take": record["take"],
                    "cli": self.media_name if not result.get("adopted") else "adopted clip",
                    "route": result.get("route"), "clip": record["clip"], "pinnedLastFrame": result.get("pinned"),
                    "pinNote": result.get("pinNote"), "summary": result.get("summary")}))
            record["status"], record["message"] = "generated", None
            self.save()
            return "ok"
        record["message"] = result["message"]
        if result["status"] == "no-route":
            record["status"] = "no-route"
            st["status"] = "no-route"
            st["message"] = (f"no generation route ({result['message']}); generate the clip with the host tool from "
                             f"{self.rel(self.root / st['job']['dir'] / 'input.png')} and "
                             f"{self.rel(folder / 'prompt.txt')}, save it as "
                             f"{self.rel(folder / 'media' / 'clip.mp4')} and run again")
            self.save()
            return "no-route"
        record["status"] = "error"
        st["status"] = "error"
        st["message"] = f"take {record['take']}: {result['message']} (the next run tries again)"
        self.log_take(ident, record)
        self.save()
        return "error"

    # ----------------------------------------------------------------- per-take tools

    def run_key(self, ident: str, record: dict, out: Path) -> dict:
        job = self.root / self.astate(ident)["job"]["dir"]
        args = ["process", "--video", self.root / record["clip"]["path"], "--output-dir", out, "--matte", "soft",
                "--reference", job / "master.png", "--key", self.plan["key"], "--frame-counts", "8",
                "--name", f"{ident}-t{record['take']:02d}"]
        summary, warnings = run_tool(SCRIPTS["video2dsprite"], args, label="video2dsprite.py process")
        probe = (forge_core.read_json(out / "pipeline-meta.json").get("probe") or {})
        return {"frames": summary.get("frames"), "fps": probe.get("fps_rational") or "24/1",
                "size": [probe.get("width"), probe.get("height")], "matte": summary.get("matte"),
                "warnings": warnings[:6]}

    def run_qc(self, ident: str, record: dict) -> dict:
        path = self.root / record["dir"] / "qc.json"
        if record.get("qc") and path.is_file():
            return record["qc"]
        if os.path.lexists(path):
            _move_aside(path)
        entry, st = self.entries[ident], self.astate(ident)
        keyed = self.root / record["stages"]["keyed"]["dir"]
        document = qc_take(self.root / st["job"]["job"], keyed / "frames-clean", keyed / "frames-raw",
                           key=self.plan["key"], gates=entry["gates"], fps=_fps_value(record["stages"]["keyed"]["fps"]),
                           facing=self.master(ident)["facing"], kind=entry["kind"], airborne=entry["airborne"],
                           returns_to_rest=entry["returnsToRest"], hit_tick=entry["hitTick"])
        document.update({"action": ident, "take": record["take"], "clip": record.get("clip"),
                         "job": {"path": st["job"]["job"], "sha256": st["job"]["sha256"]}})
        _write_new_json(path, _scrub(document))
        record["qc"] = {"file": self.rel(path), "status": document["status"], "failures": document["failures"],
                        "reasons": document["reasons"], "usableWindow": document["usableWindow"],
                        "usableFrames": document["usableFrames"], "frames": document["frames"],
                        "motion": document["motion"], "badFrames": document["perFrame"]["bad"],
                        "gates": {name: {"status": gate["status"], "value": gate["value"]}
                                  for name, gate in document["gates"].items()}}
        self.save()
        return record["qc"]

    def run_register(self, ident: str, record: dict, out: Path, window: Sequence[int] | None) -> dict:
        entry, st = self.entries[ident], self.astate(ident)
        keyed = self.root / record["stages"]["keyed"]["dir"]
        height = self.master(ident)["size"][1]
        args = ["apply", "--job", self.root / st["job"]["job"], "--frames", keyed / "frames-clean",
                "--output-dir", out, "--clip", ident, "--max-anchor-error", f"{max(2.0, 0.004 * height):.3g}",
                "--max-scale-error", "0.04"]
        if entry["lock"] == "feet":
            args += ["--lock", "feet"]
        if window:
            args += ["--range", f"{window[0]}:{window[1]}"]
        summary, warnings = run_tool(SCRIPTS["register_clip"], args, label="register_clip.py apply")
        return {"frames": summary["frames"], "sourceSize": summary["sourceSize"],
                "sourceAnchor": summary["sourceAnchor"], "fit": summary.get("fitApplied"),
                "range": list(window) if window else None,
                "registration": self.rel(out / "registration.json"), "warnings": warnings[:6]}

    def run_motion(self, ident: str, record: dict, out: Path, tag: str, window: Sequence[int] | None) -> dict:
        """Loops: gait_loop select. One-shots: retime over the motion window with ticks, the hit on the largest
        reach (attacks), a cancel tick, and takeoff/land for airborne actions."""
        entry = self.entries[ident]
        registered = record["stages"]["registered" + tag]
        frames = self.root / registered["dir"] / "frames"
        fps_text = record["stages"]["keyed"]["fps"]
        if entry["kind"] == "loop":
            args = ["select", "--frames-dir", frames, "--fps", fps_text, "--output-dir", out,
                    "--kind", entry["loopKind"]]
            if entry["loopKind"] == "gait":
                args += ["--state", entry["gaitState"]]
            summary, _ = run_tool(SCRIPTS["gait_loop"], args, label="gait_loop.py select")
            selection = forge_core.read_json(out / "selection.json")
            return {"tool": "gait_loop select", "selection": self.rel(out / "selection.json"),
                    "policy": summary["policy"], "start": summary["start"], "endExclusive": summary["endExclusive"],
                    "frames": summary["frames"], "confidence": summary.get("confidence"),
                    "durationMs": int(sum(selection["durations_ms"])), "fps": selection.get("fps")}
        motion = record["qc"]["motion"]
        offset = window[0] if window else 0
        count = int(registered["frames"])
        onset = motion.get("onset") if motion.get("onset") is not None else offset
        settle = motion.get("settle") if motion.get("settle") is not None else offset + count - 1
        start = max(0, min(count - 1, onset - 2 - offset))
        end = max(start + 1, min(count, settle + 4 - offset))
        if end - start < 4:
            start, end = max(0, start - 2), min(count, end + 2)
        source = _fps_value(fps_text)
        target = self.plan["package"].get("fps")
        frames_out = end - start
        if target and target < source:
            frames_out = max(4, min(end - start, forge_core.round_half_up((end - start) * target / source)))
            hz, per = _tick_grid(int(target))
        else:
            hz, per = max(1, forge_core.round_half_up(source)), 1

        def tick(frame: int | None) -> int | None:
            if frame is None or not start <= frame - offset < end:
                return None
            position = forge_core.round_half_up((frame - offset - start) * (frames_out - 1) / max(1, end - start - 1))
            return position * per

        args = ["--frames-dir", frames, "--fps", fps_text, "--output-dir", out, "--kind", entry["retimeKind"],
                "--range", f"{start}:{end}", "--ticks", frames_out * per, "--tick-hz", hz, "--policy", "oneshot"]
        if frames_out < end - start:
            args += ["--max-frames", frames_out]
        impact = motion.get("impact")
        if entry["hitTick"] and impact is not None and start <= impact - offset < end:
            args += ["--impact-source", impact - offset]
        events = []
        for name, frame in (("cancel", motion.get("cancel")), ("custom:takeoff", motion.get("takeoff")),
                            ("custom:land", motion.get("land"))):
            position = tick(frame)
            if position is not None and 0 < position < frames_out * per:
                events.append(f"{name}@{position}t")
        for event in events:
            args += ["--event", event]
        summary, _ = run_tool(SCRIPTS["retime"], args, label="retime.py")
        selection = forge_core.read_json(out / "selection.json")
        return {"tool": "retime", "selection": self.rel(out / "selection.json"), "policy": "oneshot",
                "start": start, "endExclusive": end, "frames": summary["frames"], "durationMs": summary["durationMs"],
                "impactMs": summary.get("impactMs"), "tickHz": hz, "events": selection.get("events", [])}

    def run_timed(self, ident: str, record: dict, out: Path, tag: str) -> dict:
        """A loop resampled to the plan's fps on the tick grid (retime --range over the gait_loop window)."""
        entry = self.entries[ident]
        motion = record["stages"]["motion" + tag]
        frames = self.root / record["stages"]["registered" + tag]["dir"] / "frames"
        fps_text = record["stages"]["keyed"]["fps"]
        start, end = motion["start"], motion["endExclusive"]
        target = int(self.plan["package"]["fps"])
        frames_out = max(4, forge_core.round_half_up((end - start) * target / _fps_value(fps_text)))
        hz, per = _tick_grid(target)
        policy = motion["policy"] if motion["policy"] in ("cycle", "pingpong") else "cycle"
        args = ["--frames-dir", frames, "--fps", fps_text, "--output-dir", out, "--kind", entry["retimeKind"],
                "--range", f"{start}:{end}", "--max-frames", frames_out, "--ticks", frames_out * per,
                "--tick-hz", hz, "--policy", policy]
        summary, _ = run_tool(SCRIPTS["retime"], args, label="retime.py")
        return {"tool": "retime", "selection": self.rel(out / "selection.json"), "policy": policy, "start": start,
                "endExclusive": end, "frames": summary["frames"], "durationMs": summary["durationMs"], "tickHz": hz}

    def needs_timed(self, ident: str, record: dict, tag: str) -> bool:
        target = self.plan["package"].get("fps")
        motion = record["stages"].get("motion" + tag) or {}
        if self.entries[ident]["kind"] != "loop" or not target:
            return False
        source = _fps_value(record["stages"]["keyed"]["fps"])
        length = motion.get("endExclusive", 0) - motion.get("start", 0)
        return target < source and forge_core.round_half_up(length * target / source) < length

    def cut(self, ident: str, record: dict, tag: str, window: Sequence[int] | None) -> dict:
        """Registration and the loop or timeline of one take (or of its usable window)."""
        self.stage(record, "registered" + tag, lambda out: self.run_register(ident, record, out, window))
        motion = self.stage(record, "motion" + tag, lambda out: self.run_motion(ident, record, out, tag, window))
        if self.needs_timed(ident, record, tag):
            return self.stage(record, "timed" + tag, lambda out: self.run_timed(ident, record, out, tag))
        return motion

    # ----------------------------------------------------------------- evaluation and retakes

    def classify(self, ident: str, step: str, failure: Exception) -> str:
        """The gate a failed tool step stands for: keying, edge, registration, loop or motion."""
        text = str(getattr(failure, "message", failure))
        if step in ("keyed", "qc"):
            return "keying"
        if step == "registered":
            return "edge" if re.search(r"video-edge-touch|canvas-overflow", text) else "registration"
        return "loop" if self.entries[ident]["kind"] == "loop" else "motion"

    def evaluate(self, ident: str, record: dict, max_takes: int) -> None:
        """Key the take, run the gates, then register and cut it; a failure rejects the take, adds the mapped
        fix clauses to the next prompt and, once the round is spent, keeps the best take's usable window."""
        st = self.astate(ident)
        step = "keyed"
        try:
            self.stage(record, "keyed", lambda out: self.run_key(ident, record, out))
            step = "qc"
            qc = self.run_qc(ident, record)
            if qc["status"] == "pass":
                step = "registered"
                self.stage(record, "registered", lambda out: self.run_register(ident, record, out, None))
                step = "motion"
                self.stage(record, "motion", lambda out: self.run_motion(ident, record, out, "", None))
                if self.needs_timed(ident, record, ""):
                    self.stage(record, "timed", lambda out: self.run_timed(ident, record, out, ""))
                record.update(status="kept", failures=[], reasons=[])
                st["chosen"] = {"take": record["take"], "reason": "first take that passed every gate",
                                "window": None, "tag": ""}
                st["status"], st["message"] = "chosen", None
            else:
                record.update(status="rejected", failures=list(qc["failures"]), reasons=list(qc["reasons"]))
        except (ToolFailure, ValueError) as failure:
            gate = self.classify(ident, step, failure)
            message = failure.message if isinstance(failure, ToolFailure) else str(failure)
            label = failure.tool if isinstance(failure, ToolFailure) else step
            record.update(status="rejected", failures=[gate], reasons=[_ascii(f"{gate}: {label}: {message}")[:600]])
        if record["status"] == "rejected":
            self.add_fixes(ident, record)
        self.log_take(ident, record)
        self.save()
        if record["status"] == "rejected":
            decided = [t for t in st["takes"] if t["round"] == st["round"] and t["status"] in ("kept", "rejected")]
            if len(decided) >= max_takes:
                self.choose_best(ident)
            else:
                st["status"] = "retake"
                self.save()

    def add_fixes(self, ident: str, record: dict) -> None:
        st = self.astate(ident)
        wanted = []
        for gate in record.get("failures", []):
            wanted += self.plan["gatefix"].get(gate, [])
        present = {fix["id"] for fix in st["fixes"]}
        added = []
        for clause in dict.fromkeys(wanted):
            if clause in present or clause not in self.plan["clauses"]:
                continue
            st["fixes"].append({"id": clause, "text": self.plan["clauses"][clause], "source": "auto",
                                "take": record["take"], "round": st["round"]})
            added.append(clause)
        record["fixNext"] = added

    def choose_best(self, ident: str) -> None:
        st = self.astate(ident)
        if st.get("chosen"):
            return
        candidates = [t for t in st["takes"] if t["round"] == st["round"] and t["status"] == "rejected"
                      and (t.get("qc") or {}).get("usableWindow")]
        candidates.sort(key=lambda t: (len(t.get("failures", [])),
                                       -(t["qc"]["usableWindow"][1] - t["qc"]["usableWindow"][0]), t["take"]))
        decided = sum(1 for t in st["takes"] if t["round"] == st["round"] and t["status"] in ("kept", "rejected"))
        for record in candidates:
            window = record["qc"]["usableWindow"]
            tag = f"-w{window[0]}-{window[1]}"
            try:
                self.cut(ident, record, tag, window)
            except (ToolFailure, ValueError) as failure:
                record.setdefault("windowErrors", []).append(_ascii(str(failure))[:300])
                self.save()
                continue
            st["chosen"] = {"take": record["take"], "window": list(window), "tag": tag,
                            "reason": f"best of {decided} failed takes: kept its usable frames {window[0]}-"
                                      f"{window[1] - 1} ({', '.join(record.get('failures', []))} logged)"}
            st["status"], st["message"] = "chosen", None
            with open(self.root / TAKES_FILE, "a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(_scrub({"schema": TAKE_SCHEMA, "at": _now(), "action": ident,
                                                "take": record["take"], "round": st["round"], "status": "best-window",
                                                "usableWindow": window, "failures": record.get("failures", []),
                                                "reasons": record.get("reasons", [])}), ensure_ascii=False) + "\n")
            self.save()
            return
        st["status"] = "failed"
        st["message"] = (f"no take of round {st['round']} passed or kept a usable window; retake with a fix "
                         "(sprite_set.py retake) or supply a clip")
        self.save()

    def wants_take(self, ident: str, max_takes: int, errors: dict[str, int]) -> bool:
        st = self.astate(ident)
        if st.get("chosen") or st["status"] == "failed" or errors.get(ident, 0) >= 2:
            return False
        last = st["takes"][-1] if st["takes"] else None
        if last and last["round"] == st["round"] and last["status"] in ("open", "generated", "no-route"):
            return True
        decided = [t for t in st["takes"] if t["round"] == st["round"] and t["status"] in ("kept", "rejected")]
        if len(decided) < max_takes:
            return True
        self.choose_best(ident)
        return False

    # ----------------------------------------------------------------- finishing, packaging, verify

    def scale_ref_file(self, ident: str) -> Path | None:
        ref = self.plan["finish"].get("scaleRefAction")
        if not ref or ref == ident:
            return None
        chosen = (self.state["actions"].get(ref) or {}).get("chosen")
        if chosen:
            record = self.take(ref, chosen["take"])
            finished = record.get("stages", {}).get("finished" + chosen.get("tag", ""))
            if finished and finished.get("scaleRef"):
                path = self.root / finished["scaleRef"]
                if path.is_file():
                    return path
        self.notes.append(f"{ident}: the scale reference {ref} has no finished frames; {ident} uses its own rest pose")
        return None

    def run_finish(self, ident: str, record: dict, out: Path, tag: str) -> dict:
        finish = self.plan["finish"]
        frames = self.root / record["stages"]["registered" + tag]["dir"] / "frames"
        scale_ref = self.scale_ref_file(ident)
        palette = self.root / finish["palette"]["path"] if finish.get("palette") else None
        script, name = resolve_finisher()
        if script is None:
            summary = standin_finish(finish["mode"], frames, out, int(finish["targetHeight"]), scale_ref, palette)
        else:
            args = [finish["mode"], "--frames", frames, "--out", out, "--target-height", finish["targetHeight"]]
            if scale_ref is not None:
                args += ["--scale-ref", scale_ref]
            if palette is not None:
                args += ["--palette", palette]
            summary, _ = run_tool(script, args, label=name)
        frames_dir = None
        for key in ("framesDir", "frames_dir"):
            if isinstance(summary.get(key), str) and Path(summary[key]).is_dir():
                frames_dir = Path(summary[key])
        if frames_dir is None:
            frames_dir = out / "frames" if (out / "frames").is_dir() and _pngs(out / "frames") else out
        files = _pngs(frames_dir)
        if not files:
            raise ValueError(f"{name} wrote no PNG frames into {self.rel(out)}")
        with Image.open(files[0]) as image:
            size = list(image.size)
        anchor = summary.get("anchor") or summary.get("sourceAnchor")
        if not (isinstance(anchor, (list, tuple)) and len(anchor) == 2):
            rest = np.asarray(forge_core.load_rgba(files[0])[0])[..., 3] > 127
            stats = _mask_stats(rest)
            band = max(2, forge_core.round_half_up(0.04 * stats["height"]))
            anchor = list(forge_core.anchor_from_mask(rest, "stance", band_rows=band, min_run=2))
        reference = next((summary[key] for key in ("scale_ref", "scaleRef", "metadata")
                          if isinstance(summary.get(key), str) and Path(summary[key]).is_file()), None)
        return {"finisher": name, "mode": finish["mode"], "targetHeight": finish["targetHeight"],
                "frames": len(files), "framesDir": self.rel(frames_dir), "size": size,
                "anchor": [float(anchor[0]), float(anchor[1])], "scale": summary.get("scale"),
                "scaleRef": self.rel(reference) if reference else None,
                "scaleRefFrom": self.rel(scale_ref) if scale_ref else None}

    def run_rebind(self, ident: str, record: dict, out: Path, tag: str) -> dict:
        stages = record["stages"]
        motion = stages.get("timed" + tag) or stages["motion" + tag]
        finished = stages["finished" + tag]
        result = rebind_selection(self.root / motion["selection"], self.root / finished["framesDir"],
                                  int(stages["registered" + tag]["frames"]), out)
        return {"selection": self.rel(result["selection"]), "from": motion["selection"], "frames": result["frames"]}

    def run_package(self, ident: str, record: dict, out: Path, tag: str, crf: tuple[int, int] | None) -> dict:
        stages = record["stages"]
        finished, selection = stages["finished" + tag], stages["selection" + tag]
        package = self.plan["package"]
        width, height = finished["size"]
        args = ["package", "--clean-dir", self.root / finished["framesDir"], "--selection",
                self.root / selection["selection"], "--output-dir", out, "--name", ident,
                "--formats", ",".join(package["formats"]), "--crop-union",
                "--max-side", min(4096, max(width, height, 2)),
                "--source-size", f"{width},{height}", "--source-anchor", "{!r},{!r}".format(*finished["anchor"]),
                "--body-height-px", self.plan["finish"]["targetHeight"], "--key", self.plan["key"]]
        if self.plan["finish"]["mode"] == "pixel":
            args += ["--pixel-art", "--sampling", "nearest"]
        if package.get("tiers"):
            args += ["--tiers", package["tiers"]]
        if crf:
            args += ["--crf-webm", crf[0], "--crf-packed", crf[1]]
        summary, warnings = run_tool(SCRIPTS["engine_export"], args, label="engine_export.py package")
        manifest = forge_core.read_json(out / "animation.json")
        files = {"manifest": self.rel(out / "animation.json"), "qa": self.rel(out / "animation-qa.json"),
                 "provenance": self.rel(out / "provenance.json"),
                 "poster": self.rel(out / manifest["poster"]["file"]) if manifest.get("poster") else None,
                 "atlas": [self.rel(out / page["file"]) for page in (manifest.get("fallback") or {}).get("pages", [])],
                 "webm": self.rel(out / manifest["webm"]["file"]) if manifest.get("webm") else None,
                 "packed": self.rel(out / manifest["packedAlpha"]["file"]) if manifest.get("packedAlpha") else None,
                 "tiers": [self.rel(out / tier["file"]) for tier in manifest.get("mobilePackedAlpha") or []]}
        return {"status": summary.get("status"), "reviewStatus": summary.get("reviewStatus"),
                "frameCount": summary.get("frameCount"), "fps": summary.get("fps"),
                "durationMs": summary.get("durationMs"), "loopPolicy": manifest.get("loopPolicy"),
                "events": manifest.get("events", []), "impactMs": manifest.get("impactMs"),
                "files": files, "crf": list(crf) if crf else None, "warnings": warnings[:6]}

    def verify(self, record: dict, package_stage: str) -> dict:
        package = self.root / record["stages"][package_stage]["dir"]
        summary, _ = run_tool(SCRIPTS["engine_export"], ["verify", "--package", package],
                              label="engine_export.py verify")
        return {"status": summary.get("status"), "report": self.rel(package / "verify-qa.json"),
                "transports": summary.get("transports")}

    def post_process(self, ident: str) -> None:
        """Finish, rebind, package and verify the chosen take (or its usable window)."""
        st = self.astate(ident)
        chosen = st.get("chosen")
        if not chosen:
            return
        output = st.get("output") or {}
        if (st["status"] in ("done", "done-warn") and output.get("take") == chosen["take"]
                and output.get("tag") == chosen.get("tag", "")):
            return
        record = self.take(ident, chosen["take"])
        tag, window = chosen.get("tag", ""), chosen.get("window")
        try:
            if "keyed" not in record.get("stages", {}):
                raise ValueError(f"take {record['take']} was never keyed; run review only on evaluated takes")
            if not record.get("qc"):
                self.run_qc(ident, record)
            self.cut(ident, record, tag, window)
            self.stage(record, "finished" + tag, lambda out: self.run_finish(ident, record, out, tag))
            self.stage(record, "selection" + tag, lambda out: self.run_rebind(ident, record, out, tag))
            package_stage = "package" + tag
            self.stage(record, package_stage, lambda out: self.run_package(ident, record, out, tag, None))
            verify_key = "verify" + tag
            video = set(self.plan["package"]["formats"]) & {"webm", "packed"} or self.plan["package"].get("tiers")
            if not video:
                record["stages"][verify_key] = {"status": "skipped", "reason": "png-only package: nothing to decode"}
            elif not record["stages"].get(verify_key):
                try:
                    record["stages"][verify_key] = self.verify(record, package_stage)
                except ToolFailure as first:
                    package_stage = "package-r2" + tag
                    self.stage(record, package_stage, lambda out: self.run_package(ident, record, out, tag, (18, 12)))
                    try:
                        record["stages"][verify_key] = {**self.verify(record, package_stage),
                                                        "retry": _ascii(first.message)[:300]}
                    except ToolFailure as second:
                        record["stages"][verify_key] = {"status": "fail", "message": _ascii(second.message)[:400],
                                                        "package": record["stages"][package_stage]["dir"]}
            verify = record["stages"][verify_key]
            package_stage = "package-r2" + tag if "package-r2" + tag in record["stages"] else "package" + tag
            package = record["stages"][package_stage]
            st["output"] = {"take": record["take"], "tag": tag, "package": package["dir"],
                            "manifest": package["files"]["manifest"], "verify": verify.get("status")}
            st["status"] = "done" if verify.get("status") in ("pass", "skipped") else "done-warn"
            st["message"] = None if st["status"] == "done" else f"verify failed: {verify.get('message')}"
        except (ToolFailure, ValueError) as failure:
            st["status"] = "post-error"
            st["message"] = _ascii(str(failure))[:500]
        self.save()

    # ----------------------------------------------------------------- the whole run

    def run(self, only: Sequence[str] | None, *, max_takes: int, jobs: int, stagger: float, media_cli: str | None,
            timeout: float) -> tuple[dict, int]:
        self.check_masters()
        if self.state["plan"].get("sha256") != self.plan_sha:
            self.notes.append(f"{PLAN_FILE} changed since the last run; later takes use the edited plan")
            self.state["plan"]["sha256"] = self.plan_sha
        self.state["runs"] = int(self.state.get("runs", 0)) + 1
        self.save()
        self.resolve_media(media_cli)
        order = self.order(only)
        for ident in order:
            self.ensure_job(ident)
        errors: dict[str, int] = {}
        generated = 0
        blocked = False
        while not blocked:
            batch = [ident for ident in order if self.wants_take(ident, max_takes, errors)]
            if not batch:
                break
            records = {ident: self.open_take(ident) for ident in batch}
            pending = [ident for ident in batch if records[ident]["status"] in ("open", "no-route")]
            results: dict[str, dict] = {}
            if pending:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(jobs, len(pending)))) as pool:
                    futures = {}
                    for number, ident in enumerate(pending):
                        if number and stagger > 0 and jobs > 1:
                            time.sleep(stagger)
                        futures[ident] = pool.submit(self.generate, ident, records[ident], timeout)
                    for ident, future in futures.items():
                        results[ident] = future.result()
            for ident in batch:
                record = records[ident]
                if ident in results:
                    outcome = self.record_generation(ident, record, results[ident])
                    if outcome == "no-route":
                        blocked = True
                        continue
                    if outcome == "error":
                        errors[ident] = errors.get(ident, 0) + 1
                        continue
                    generated += 0 if results[ident].get("adopted") else 1
                if record["status"] == "generated":
                    self.evaluate(ident, record, max_takes)
        for ident in order:
            self.post_process(ident)
        return self.summary(order, generated)

    def summary(self, order: Sequence[str], generated: int) -> tuple[dict, int]:
        actions = {}
        for ident in order:
            st = self.astate(ident)
            actions[ident] = {"status": st["status"], "takes": len(st["takes"]),
                              "chosen": (st.get("chosen") or {}).get("take"),
                              "accepted": bool(st.get("accepted")),
                              "package": (st.get("output") or {}).get("package"),
                              "message": st.get("message")}
        statuses = {item["status"] for item in actions.values()}
        if statuses <= {"done"}:
            status, code = "complete", 0
        elif "no-route" in statuses and not statuses & {"failed", "post-error", "error", "generating", "retake"}:
            status, code = "waiting-for-clips", EXIT_NO_ROUTE
        elif statuses <= {"done", "done-warn"}:
            status, code = "complete-with-warnings", 0
        else:
            status, code = "incomplete", 1
        summary = {"output": str(self.root), "plan": str(self.plan_path), "state": str(self.state_path),
                   "status": status, "generated": generated, "media": self.media_name,
                   "finisher": resolve_finisher()[1], "actions": actions,
                   "needsReview": [ident for ident, item in actions.items() if not item["accepted"]],
                   "notes": [_ascii(note) for note in self.notes[:12]]}
        return summary, code


# --------------------------------------------------------------------------- review sheets

GREY, PANEL, INK, BAD = (118, 118, 118), (32, 36, 44), (236, 236, 236), (235, 60, 60)
LETTERS = {"area": "A", "feet": "F", "identity": "I", "zoom": "Z", "turn": "T", "edge": "E", "background": "B",
           "extra": "X"}
CHECKLIST = ("the same character, face, colours and costume as the master in every frame",
             "facing {facing} the whole time; never turned around or toward the camera",
             "the {action} really happens, once, and reads at game size",
             "no extra objects: dust, orbs, sparks, projectiles, trails, text or a second weapon",
             "nothing covers the face; the weapon keeps its shape and length",
             "loops close without a jump; one-shots start and end on the rest pose")
_FONT: list[ImageFont.ImageFont] = []


def _font() -> ImageFont.ImageFont:
    if not _FONT:
        _FONT.append(ImageFont.load_default())
    return _FONT[0]


def _thumb(source: Image.Image | np.ndarray, size: int) -> Image.Image:
    rgba = (source if isinstance(source, Image.Image) else Image.fromarray(np.asarray(source))).convert("RGBA")
    factor = min(1.0, size / max(rgba.size))
    if factor < 1.0:
        out = (max(1, forge_core.round_half_up(rgba.width * factor)),
               max(1, forge_core.round_half_up(rgba.height * factor)))
        rgba = forge_core.resample_rgba(rgba, factor, "box", out_size=out)
    tile = Image.new("RGB", rgba.size, GREY)
    tile.paste(rgba, (0, 0), rgba)
    return tile


def _fit_lines(lines: Sequence[str], width: int) -> list[str]:
    """Wrap header lines to ``width`` px of the review font (measured, so nothing is clipped)."""
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    fitted = []
    for text in lines:
        words, line = _ascii(text).split(" "), ""
        for word in words:
            trial = f"{line} {word}" if line else word
            if line and measure.textlength(trial, font=_font()) > width:
                fitted.append(line)
                line = "    " + word
            else:
                line = trial
        fitted.append(line)
    return fitted


def _grid(header: Sequence[str], tiles: Sequence[tuple[Image.Image, str, bool]], columns: int = 6) -> Image.Image:
    cell_w = max(tile.width for tile, _, _ in tiles)
    cell_h = max(tile.height for tile, _, _ in tiles) + 14
    rows = math.ceil(len(tiles) / columns)
    width = max(min(columns, len(tiles)) * (cell_w + 10) + 10, 760)
    header = _fit_lines(header, width - 16)
    head = 14 * len(header) + 12
    sheet = Image.new("RGB", (width, head + rows * (cell_h + 10) + 6), PANEL)
    draw = ImageDraw.Draw(sheet)
    for number, line in enumerate(header):
        draw.text((8, 6 + 14 * number), line, fill=INK, font=_font())
    for index, (tile, label, flagged) in enumerate(tiles):
        row, column = divmod(index, columns)
        x, y = 10 + column * (cell_w + 10), head + row * (cell_h + 10)
        sheet.paste(tile, (x, y))
        if flagged:
            draw.rectangle([x - 2, y - 2, x + tile.width + 1, y + tile.height + 1], outline=BAD, width=2)
        draw.text((x, y + tile.height + 2), _ascii(label), fill=BAD if flagged else INK, font=_font())
    return sheet


def take_sheet(runner: SetRunner, ident: str, record: dict) -> Image.Image:
    """Contact sheet of one take: the input still, evenly spaced frames and the first frame of every failing
    run, each labelled with its frame number and the letters of the gates it fails."""
    files = _pngs(runner.root / record["stages"]["keyed"]["dir"] / "frames-clean")
    count = len(files)
    qc = record.get("qc") or {}
    bad = {gate: set(frames) for gate, frames in (qc.get("badFrames") or {}).items()}
    letters = {i: "".join(code for gate, code in LETTERS.items() if i in bad.get(gate, set())) for i in range(count)}
    picks = {forge_core.round_half_up(i * (count - 1) / 13) for i in range(14)} if count > 1 else {0}
    for frames in bad.values():
        for start, _ in _runs([i in frames for i in range(count)]):
            picks.add(start)
    tiles = [(_thumb(Image.open(runner.root / runner.astate(ident)["job"]["input"]), 150), "input (master)", False)]
    for index in sorted(picks)[:29]:
        with Image.open(files[index]) as image:
            tiles.append((_thumb(image, 150), f"#{index} {letters[index]}".strip(), bool(letters[index])))
    facing = runner.master(ident)["facing"]
    words = {"facing": prompt_words(facing, runner.plan["key"], "", "", ident)["facing"],
             "action": runner.entries[ident]["action"]}
    header = [f"{runner.plan['name']} / {ident} / take {record['take']} (round {record['round']}) / "
              f"{record['status']} / route {record.get('route') or '-'} / {count} frames"]
    failing = [f"{gate}={item['status']}" for gate, item in (qc.get("gates") or {}).items() if item["status"] != "pass"]
    header.append("gates not passing: " + (", ".join(failing) if failing else "none"))
    header += [str(reason) for reason in qc.get("reasons", [])[:6]]
    if qc.get("usableWindow"):
        header.append(f"usable frames {qc['usableWindow'][0]}-{qc['usableWindow'][1] - 1} of {count}")
    header.append("red: A area F feet I identity Z zoom T turned E edge B background X extra objects")
    header.append("LOOK FOR: " + "; ".join(fill(item, words) for item in CHECKLIST))
    return _grid(header, tiles)


def final_sheet(runner: SetRunner, ident: str, record: dict, tag: str) -> Image.Image | None:
    """The played frames of the finished clip at game scale (x3 nearest for pixel art), on the feet line."""
    stages = record.get("stages", {})
    finished, selection = stages.get("finished" + tag), stages.get("selection" + tag)
    if not finished or not selection:
        return None
    files = _pngs(runner.root / finished["framesDir"])
    document = forge_core.read_json(runner.root / selection["selection"])
    played = list(document["sourceIndices"])
    zoom = 3 if runner.plan["finish"]["mode"] == "pixel" else (2 if max(finished["size"]) < 96 else 1)
    anchor_y = forge_core.round_half_up(finished["anchor"][1] * zoom)
    tiles = []
    for index in list(dict.fromkeys(played))[:24]:
        with Image.open(files[index]) as image:
            rgba = image.convert("RGBA")
        if zoom > 1:
            rgba = rgba.resize((rgba.width * zoom, rgba.height * zoom), Image.Resampling.NEAREST)
        tile = Image.new("RGB", rgba.size, GREY)
        tile.paste(rgba, (0, 0), rgba)
        ImageDraw.Draw(tile).line([0, min(anchor_y, tile.height - 1), tile.width, min(anchor_y, tile.height - 1)],
                                  fill=(0, 220, 255))
        tiles.append((tile, f"src {index}", False))
    events = ", ".join(f"{event['name']}@{event['atMs']}ms" for event in document.get("events", []))
    header = [f"{runner.plan['name']} / {ident} / take {record['take']} finished ({runner.plan['finish']['mode']}, "
              f"x{zoom}) / {len(played)} frames, {sum(document['durations_ms'])} ms, {document['loopPolicy']}",
              "events: " + (events or "none") + " / cyan line: feet anchor"]
    return _grid(header, tiles, columns=8)


def _rest_frames(runner: SetRunner, prefix: str = "") -> list[tuple[str, np.ndarray, list[float]]]:
    items = []
    for ident in runner.order():
        st = runner.astate(ident)
        chosen = st.get("chosen")
        if not chosen:
            continue
        record = runner.take(ident, chosen["take"])
        finished = record.get("stages", {}).get("finished" + chosen.get("tag", ""))
        if not finished:
            continue
        files = _pngs(runner.root / finished["framesDir"])
        if files:
            items.append((prefix + ident, np.asarray(forge_core.load_rgba(files[0])[0]), finished["anchor"]))
    return items


def lineup_sheet(runner: SetRunner, peers: Sequence[SetRunner]) -> tuple[Image.Image, dict] | None:
    """Cast line-up: the finished rest frame of every action (and of every peer set) on one feet line, at x1 and
    x3, with body height and opaque area so one character keeps one height and the cast keeps its ratios."""
    items = _rest_frames(runner)
    for peer in peers:
        items += _rest_frames(peer, peer.plan["name"] + "/")
    if not items:
        return None
    rows, measures = [], {}
    for label, rgba, _anchor in items:
        stats = _mask_stats(rgba[..., 3] > 127)
        measures[label] = {"heightPx": stats["height"] if stats else 0, "areaPx": stats["area"] if stats else 0}
    for zoom in (1, 3):
        above = max(forge_core.round_half_up(anchor[1] * zoom) for _, _, anchor in items)
        below = max(rgba.shape[0] * zoom - forge_core.round_half_up(anchor[1] * zoom) for _, rgba, anchor in items)
        width = sum(rgba.shape[1] * zoom + 16 for _, rgba, _ in items) + 16
        row = Image.new("RGB", (max(width, 320), above + below + 34), GREY)
        draw = ImageDraw.Draw(row)
        x = 16
        for label, rgba, anchor in items:
            image = Image.fromarray(rgba)
            if zoom > 1:
                image = image.resize((image.width * zoom, image.height * zoom), Image.Resampling.NEAREST)
            row.paste(image, (x, above - forge_core.round_half_up(anchor[1] * zoom)), image)
            text = f"{label} h{measures[label]['heightPx']}" if zoom == 1 else label
            draw.text((x, above + below + 6), _ascii(text), fill=INK, font=_font())
            x += image.width + 16
        draw.line([0, above, row.width, above], fill=(0, 220, 255))
        rows.append(row)
    header = [f"cast line-up: {runner.plan['name']} ({runner.plan['class']}), finish {runner.plan['finish']['mode']}, "
              f"target height {runner.plan['finish']['targetHeight']} px; rows x1 and x3; cyan line: feet",
              "one character keeps one body height; mobs no taller than the hero; a boss about 2x the hero"]
    width = max(row.width for row in rows)
    sheet = Image.new("RGB", (width, 14 * len(header) + 12 + sum(row.height + 8 for row in rows)), PANEL)
    draw = ImageDraw.Draw(sheet)
    for number, line in enumerate(header):
        draw.text((8, 6 + 14 * number), _ascii(line), fill=INK, font=_font())
    y = 14 * len(header) + 12
    for row in rows:
        sheet.paste(row, (0, y))
        y += row.height + 8
    return sheet, measures


def cmd_review(args: argparse.Namespace) -> dict:
    runner = SetRunner(Path(args.plan))
    peers = [SetRunner(Path(peer)) for peer in args.peer or []]
    folder = runner.root / "review"
    sheets = []
    for ident in runner.order():
        st = runner.astate(ident)
        chosen = st.get("chosen") or {}
        for record in st["takes"]:
            if "keyed" not in record.get("stages", {}):
                continue
            path = folder / ident / f"t{record['take']:02d}-sheet.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_file():
                _publish_png(take_sheet(runner, ident, record), path)
            item = {"action": ident, "take": record["take"], "round": record["round"], "status": record["status"],
                    "chosen": chosen.get("take") == record["take"],
                    "accepted": (st.get("accepted") or {}).get("take") == record["take"],
                    "failures": record.get("failures", []), "reasons": record.get("reasons", []),
                    "usableWindow": (record.get("qc") or {}).get("usableWindow"),
                    "sheet": forge_core.file_ref(path, runner.root), "final": None}
            tag = chosen.get("tag", "") if item["chosen"] else None
            if tag is not None:
                final = folder / ident / f"t{record['take']:02d}{tag}-final.png"
                if not final.is_file():
                    image = final_sheet(runner, ident, record, tag)
                    if image is not None:
                        _publish_png(image, final)
                if final.is_file():
                    item["final"] = forge_core.file_ref(final, runner.root)
            sheets.append(item)
    folder.mkdir(parents=True, exist_ok=True)
    lineup = None
    made = lineup_sheet(runner, peers)
    if made is not None:
        image, measures = made
        path = _next_numbered(folder, "lineup", ".png")
        _publish_png(image, path)
        lineup = {**forge_core.file_ref(path, runner.root), "scales": [1, 3], "measures": measures}
    document = {
        "schema": REVIEW_SCHEMA, "createdAt": _now(), "set": runner.plan["name"],
        "instructions": ("Look at EVERY sheet (Read in Claude Code, view_image in Codex) before accept or retake. "
                         "Numbers cannot see identity, facing, extra objects or a covered face."),
        "checklist": list(CHECKLIST), "sheets": sheets, "lineup": lineup,
        "next": ["sprite_set.py accept --plan <set>/set_plan.json --action <action> --take <n>",
                 "sprite_set.py retake --plan <set>/set_plan.json --action <action> --fix <clause id or text>"],
    }
    path = _next_numbered(folder, "review", ".json")
    _write_new_json(path, _scrub(document))
    runner.state["review"] = {"latest": runner.rel(path), "at": _now()}
    runner.save()
    return {"output": str(folder.resolve()), "metadata": str(path.resolve()), "review": str(path.resolve()),
            "sheets": [str((runner.root / item["sheet"]["path"]).resolve()) for item in sheets],
            "finals": [str((runner.root / item["final"]["path"]).resolve()) for item in sheets if item["final"]],
            "lineup": str((runner.root / lineup["path"]).resolve()) if lineup else None,
            "look": "open and look at every sheet, the finals and the lineup before accept"}


# --------------------------------------------------------------------------- accept, retake, report

def _parse_window(text: str | None) -> list[int] | None:
    if not text:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", text)
    if not match or int(match.group(2)) <= int(match.group(1)):
        raise ValueError(f"--window needs START:END (0-based take frames, END exclusive); got {text!r}")
    return [int(match.group(1)), int(match.group(2))]


def cmd_accept(args: argparse.Namespace) -> dict:
    runner = SetRunner(Path(args.plan))
    st = runner.astate(args.action)
    record = runner.take(args.action, args.take)
    if "keyed" not in record.get("stages", {}):
        raise ValueError(f"{args.action} take {args.take} was never keyed; there is nothing to look at or accept")
    latest = (runner.state.get("review") or {}).get("latest")
    if not latest or not (runner.root / latest).is_file():
        raise ValueError("run sprite_set.py review and LOOK at the contact sheets before accepting")
    review = forge_core.read_json(runner.root / latest)
    sheet = next((item for item in review.get("sheets", [])
                  if item["action"] == args.action and item["take"] == args.take), None)
    if sheet is None:
        raise ValueError(f"the latest review has no sheet for {args.action} take {args.take}; run review again and "
                         "look at it")
    sheet_path = runner.root / sheet["sheet"]["path"]
    if not sheet_path.is_file() or forge_core.sha256_file(sheet_path) != sheet["sheet"]["sha256"]:
        raise ValueError("the reviewed contact sheet is missing or changed; run review again and look at it")
    frames = int(record["stages"]["keyed"].get("frames") or 0)
    window = _parse_window(args.window)
    if window and frames and window[1] > frames:
        raise ValueError(f"--window ends after the take's {frames} frames")
    chosen = st.get("chosen") or {}
    if window is None and record["status"] != "kept":
        if chosen.get("take") == args.take:
            window = chosen.get("window")
        else:
            window = (record.get("qc") or {}).get("usableWindow")
            if window is None:
                raise ValueError(f"{args.action} take {args.take} failed the gates without a usable window; pass "
                                 "--window START:END to keep the frames you checked")
    tag = "" if window is None else f"-w{window[0]}-{window[1]}"
    if chosen.get("take") != args.take or chosen.get("tag", "") != tag:
        st["chosen"] = {"take": args.take, "window": window, "tag": tag,
                        "reason": "accepted by the agent after looking at the review"}
        st["status"], st["message"] = "chosen", None
    st["accepted"] = {"take": args.take, "window": window, "note": args.note or "", "at": _now(), "review": latest,
                      "sheet": sheet["sheet"]}
    runner.save()
    done = st["status"] in ("done", "done-warn")
    return {"output": str(runner.root), "metadata": str(runner.state_path), "action": args.action, "take": args.take,
            "accepted": True, "window": window, "status": st["status"],
            "next": None if done else "run sprite_set.py run to finish and package the accepted take"}


def cmd_retake(args: argparse.Namespace) -> dict:
    runner = SetRunner(Path(args.plan))
    st = runner.astate(args.action)
    fixes = []
    for value in args.fix:
        text = " ".join(str(value).split())
        if not text:
            raise ValueError("--fix needs a clause id from motion-prompts.md or the clause text")
        if text in runner.plan["clauses"]:
            fixes.append({"id": text, "text": runner.plan["clauses"][text]})
        else:
            fixes.append({"id": "agent", "text": text})
    chosen = st.get("chosen")
    if chosen:
        runner.take(args.action, chosen["take"])["review"] = {"verdict": "retake", "at": _now(),
                                                              "fixes": [fix["id"] for fix in fixes]}
    st["round"] = int(st.get("round", 1)) + 1
    known = {fix["text"].lower() for fix in st["fixes"]}
    for fix in fixes:
        if fix["text"].lower() not in known:
            st["fixes"].append({**fix, "source": "agent", "round": st["round"]})
            known.add(fix["text"].lower())
    st.update(chosen=None, accepted=None, status="pending", message=None)
    with open(runner.root / TAKES_FILE, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(_scrub({"schema": TAKE_SCHEMA, "at": _now(), "action": args.action,
                                        "take": chosen["take"] if chosen else None, "round": st["round"],
                                        "status": "retake-requested", "fixNext": [fix["id"] for fix in fixes],
                                        "reasons": [fix["text"] for fix in fixes]}), ensure_ascii=False) + "\n")
    runner.save()
    return {"output": str(runner.root), "metadata": str(runner.state_path), "action": args.action,
            "round": st["round"], "fixes": [fix["id"] for fix in fixes], "status": "pending",
            "next": "run sprite_set.py run to generate the retake"}


def build_report(runner: SetRunner) -> dict:
    """Every action's route, takes, QC numbers, loop or timing and output files (relative paths only)."""
    actions = []
    for ident in runner.order():
        st, entry = runner.astate(ident), runner.entries[ident]
        takes = []
        for record in st["takes"]:
            qc = record.get("qc") or {}
            takes.append({"take": record["take"], "round": record["round"], "status": record["status"],
                          "route": record.get("route"), "pinnedLastFrame": record.get("pinnedLastFrame"),
                          "prompt": record.get("prompt"), "fixes": record.get("fixes", []),
                          "clip": record.get("clip"), "failures": record.get("failures", []),
                          "reasons": record.get("reasons", []), "message": record.get("message"),
                          "qc": {"file": qc.get("file"), "gates": qc.get("gates"),
                                 "usableWindow": qc.get("usableWindow"),
                                 "usableFrames": qc.get("usableFrames"), "frames": qc.get("frames"),
                                 "motion": qc.get("motion")} if qc else None})
        chosen, output = st.get("chosen"), st.get("output") or {}
        loop = timing = outputs = finish = None
        if chosen:
            record = runner.take(ident, chosen["take"])
            stages, tag = record.get("stages", {}), chosen.get("tag", "")
            motion = stages.get("timed" + tag) or stages.get("motion" + tag)
            package = stages.get("package-r2" + tag) or stages.get("package" + tag)
            finished = stages.get("finished" + tag)
            if motion and entry["kind"] == "loop":
                loop = {key: motion.get(key) for key in ("tool", "policy", "start", "endExclusive", "frames",
                                                         "durationMs", "confidence", "selection")}
            elif motion:
                timing = {key: motion.get(key) for key in ("tool", "start", "endExclusive", "frames", "durationMs",
                                                           "impactMs", "tickHz", "events", "selection")}
            if package:
                outputs = {**package["files"], "package": package["dir"], "status": package.get("status"),
                           "fps": package.get("fps"), "frameCount": package.get("frameCount"),
                           "durationMs": package.get("durationMs"), "loopPolicy": package.get("loopPolicy"),
                           "verify": (stages.get("verify" + tag) or {}).get("status"),
                           "registration": (stages.get("registered" + tag) or {}).get("registration")}
            if finished:
                finish = {key: finished.get(key) for key in ("finisher", "mode", "targetHeight", "scale", "size",
                                                             "anchor", "scaleRefFrom")}
        actions.append({"id": ident, "action": entry["action"], "view": entry["view"], "kind": entry["kind"],
                        "status": st["status"], "message": st.get("message"),
                        "canvas": entry["placement"]["canvas"], "pinLastFrame": entry["pinLastFrame"],
                        "route": next((t["route"] for t in reversed(takes) if t.get("route")), None),
                        "chosen": chosen, "accepted": st.get("accepted"), "fixes": st.get("fixes", []),
                        "takes": takes, "loop": loop, "timing": timing, "finish": finish, "outputs": outputs})
    statuses = {item["status"] for item in actions}
    status = ("complete" if statuses <= {"done"} else
              "complete-with-warnings" if statuses <= {"done", "done-warn"} else "incomplete")
    return {"schema": REPORT_SCHEMA, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION}, "createdAt": _now(),
            "name": runner.plan["name"], "class": runner.plan["class"], "status": status,
            "plan": {"path": PLAN_FILE, "sha256": runner.plan_sha}, "finish": runner.plan["finish"],
            "package": runner.plan["package"], "accepted": [item["id"] for item in actions if item["accepted"]],
            "needsReview": [item["id"] for item in actions if not item["accepted"]],
            "review": runner.state.get("review"), "actions": actions}


def cmd_report(args: argparse.Namespace) -> dict:
    runner = SetRunner(Path(args.plan))
    report = _scrub(build_report(runner))
    if args.output:
        path = Path(args.output)
        if os.path.lexists(path):
            raise ValueError(f"{path.name} already exists; reports are never overwritten")
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        folder = runner.root / "reports"
        folder.mkdir(exist_ok=True)
        path = _next_numbered(folder, "report", ".json")
    _write_new_json(path, report)
    return {"output": str(path.resolve()), "metadata": str(path.resolve()), "status": report["status"],
            "actions": {item["id"]: item["status"] for item in report["actions"]},
            "needsReview": report["needsReview"]}


# --------------------------------------------------------------------------- CLI

def _positive_int(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"needs a whole number {low}-{high}; got {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"needs a whole number {low}-{high}; got {text!r}")
        return value
    return parse


def _seconds(low: float, high: float) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"needs seconds {low:g}-{high:g}; got {text!r}") from None
        if not (math.isfinite(value) and low <= value <= high):
            raise argparse.ArgumentTypeError(f"needs seconds {low:g}-{high:g}; got {text!r}")
        return value
    return parse


def cmd_run(args: argparse.Namespace) -> tuple[dict, int]:
    runner = SetRunner(Path(args.plan))
    generation = runner.plan["generation"]
    only = [item.strip() for item in args.actions.split(",") if item.strip()] if args.actions else None
    return runner.run(only, max_takes=args.max_takes or int(generation["maxTakes"]),
                      jobs=args.jobs or int(generation["jobs"]), stagger=args.stagger, media_cli=args.media_cli,
                      timeout=args.timeout or float(generation.get("timeoutSeconds") or 1200))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sprite_set.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("plan", help="write a new set folder with set_plan.json from an approved master.json",
                        description="Read master.json (generate2dsprite.master.v1) and write a new set folder: "
                                    "master copies and set_plan.json with one clip per action and view.")
    pl.add_argument("--master", required=True, help="master.json (generate2dsprite.master.v1); its padded PNG is "
                                                    "found through its files entry and checked by sha256")
    pl.add_argument("--output-dir", required=True, help="new set folder (refused if it exists)")
    pl.add_argument("--actions", default=",".join(DEFAULT_ACTIONS),
                    help=f"comma list from {', '.join(ACTION_PRESETS)} (default {','.join(DEFAULT_ACTIONS)})")
    pl.add_argument("--views", default="side", help="comma list of side, front, back (default side)")
    pl.add_argument("--view-master", action="append", default=[], metavar="VIEW=FILE",
                    help="master.json of another view (front or back); repeatable")
    pl.add_argument("--finish", choices=FINISHES, help="hd (full colour) or pixel (one palette); default: the "
                                                       "master's finish, else hd")
    pl.add_argument("--target-height", type=_positive_int(8, 4096),
                    help="finished body height in px (default hd 256, pixel 80; a boss twice that)")
    pl.add_argument("--fps", type=_positive_int(1, 60),
                    help="playback rate of the finished frames (default: the clip's own rate for hd, 12 for pixel)")
    pl.add_argument("--formats", default="png,webm,packed", help="engine_export formats (default png,webm,packed)")
    pl.add_argument("--tiers", help="engine_export mobile tiers: actor, prop, fx or NAME:EDGE@FPS (default none)")
    pl.add_argument("--duration", type=_seconds(2, 30), default=6.0, help="clip seconds (default 6)")
    pl.add_argument("--resolution", default="720p", help="generation resolution (default 720p)")
    pl.add_argument("--max-takes", type=_positive_int(1, 10), default=3,
                    help="automatic takes per action and round (default 3)")
    pl.add_argument("--jobs", type=_positive_int(1, 16), default=3, help="clips generated at once (default 3)")
    pl.add_argument("--pin-last", choices=("auto", "on", "off"), default="auto",
                    help="pin the end frame to the master: auto = idle and attack (default), on = every action "
                         "that returns to rest, off; the route must take a last frame")
    pl.add_argument("--gen-timeout", type=_seconds(30, 7200), default=1200.0,
                    help="seconds one generation may take (default 1200)")
    pl.add_argument("--style", help="the 'keep the exact <style> look' words (default from the finish)")
    pl.add_argument("--pronoun", default="their", help="possessive in the templates: his, her, its, their (default)")
    pl.add_argument("--palette", help="pixel finish: shared palette PNG or JSON for the whole cast")
    pl.add_argument("--library", help="motion-prompts.md to read (default: this skill's references copy)")
    pl.set_defaults(func=cmd_plan)

    rn = sub.add_parser("run", help="generate, check, retake, register, cut, finish, package and verify (resumable)",
                        description="Make every planned action; running it again resumes from set_state.json.")
    rn.add_argument("--plan", required=True, help="set_plan.json (or its set folder)")
    rn.add_argument("--actions", help="comma list of action ids to work on (default: every planned action)")
    rn.add_argument("--max-takes", type=_positive_int(1, 10), help="override the plan's takes per round")
    rn.add_argument("--jobs", type=_positive_int(1, 16), help="override the plan's parallel generations")
    rn.add_argument("--stagger", type=_seconds(0, 60), default=2.0,
                    help="seconds between parallel generation starts (default 2)")
    rn.add_argument("--media-cli", help="media CLI script to call instead of generate2dmedia/scripts/route_media.py "
                                        "(same arguments and JSON line)")
    rn.add_argument("--timeout", type=_seconds(30, 7200), help="override the plan's generation timeout")

    rv = sub.add_parser("review", help="contact sheets per take, finished strips, a cast line-up and review.json",
                        description="Write the review images; the agent must look at every one before accept.")
    rv.add_argument("--plan", required=True, help="set_plan.json (or its set folder)")
    rv.add_argument("--peer", action="append", default=[], help="another set (plan or folder) for the cast line-up")
    rv.set_defaults(func=cmd_review)

    ac = sub.add_parser("accept", help="record the agent's approval of one take after looking at its sheet")
    ac.add_argument("--plan", required=True, help="set_plan.json (or its set folder)")
    ac.add_argument("--action", required=True, help="action id")
    ac.add_argument("--take", required=True, type=_positive_int(1, 999), help="take number")
    ac.add_argument("--window", help="START:END take frames to keep (END exclusive) for a take that failed a gate")
    ac.add_argument("--note", help="what was checked")
    ac.set_defaults(func=cmd_accept)

    rt = sub.add_parser("retake", help="reject an action semantically; the next run makes a new take with the fix")
    rt.add_argument("--plan", required=True, help="set_plan.json (or its set folder)")
    rt.add_argument("--action", required=True, help="action id")
    rt.add_argument("--fix", required=True, action="append",
                    help="clause id from motion-prompts.md (for example never-turn) or the clause text; repeatable")
    rt.set_defaults(func=cmd_retake)

    rp = sub.add_parser("report", help="one JSON with routes, takes, QC numbers, loops or timing and output files")
    rp.add_argument("--plan", required=True, help="set_plan.json (or its set folder)")
    rp.add_argument("--output", help="new report file (default <set>/reports/report-NNN.json)")
    rp.set_defaults(func=cmd_report)
    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        summary, code = cmd_run(args)
        print(json.dumps(summary, ensure_ascii=True))
        if code == EXIT_NO_ROUTE:
            print(_ascii("error: no generation route; the summary names the media folders that wait for a clip"),
                  file=sys.stderr)
            return EXIT_NO_ROUTE
        if code:
            failing = [f"{ident} {item['status']}" + (f" ({item['message']})" if item.get("message") else "")
                       for ident, item in summary["actions"].items() if item["status"] not in ("done", "done-warn")]
            raise ValueError("the set is incomplete: " + "; ".join(failing))
        return 0
    print(json.dumps(args.func(args), ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Usage errors exit 2; refused input or a failed step prints error: ... and exits 1; exit 3 means the run
    waits for a clip because no generation route exists (D26, D27; forge_core.run_cli)."""
    return forge_core.run_cli(_run, argv, expected=forge_core.CLI_EXPECTED_ERRORS + (ToolFailure,))


if __name__ == "__main__":
    raise SystemExit(main())
