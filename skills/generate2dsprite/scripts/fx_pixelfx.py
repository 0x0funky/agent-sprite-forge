#!/usr/bin/env python3
"""Pixel-art FX from pixelfx for hibit games: list its effects, render one into game-ready frames.

pixelfx (https://github.com/patch-gg/pixelfx) is a third-party library of code-driven pixel-art effects by
Patch, MIT licensed; its NOTICE credits Sprite Fusion's pixel-art VFX by Hugo Duprez (MIT) and Hollow Crown
by rehan-remade (MIT) for parts it adapts. It is NOT bundled with the Forge. Install it yourself:

    git clone https://github.com/patch-gg/pixelfx

and pass --pixelfx <checkout> (or set PIXELFX_DIR). Node.js 18+ runs it; rendering needs no npm packages.

  check   which FX route this machine has for hibit games: A, pixelfx (a checkout and Node 18+ found;
          recommended), or B, the image route (an FX sheet, generate2dsprite.py process, hibit_fx.py)
  list    the checkout's effects (id, loop or once, length, size, whether it is drawn around a character,
          description); words filter them
  render  one effect into a new --output-dir: frames/ (transparent PNGs), sheet.png + sheet.json
          (Aseprite-style JSON), preview.gif (x3 on a dark backdrop, review only), fx.json (provenance:
          repository, commit, version, license, copyright lines, effect, options, recolour, QA) and
          CREDITS.txt (pixelfx's LICENSE and NOTICE, which travel with the frames)

Recolour to the game's colours: --ramp "#hex,#hex,..." (dark to light) is the effect's own palette option
(pixelfx picks ramp steps, it never blends them); --snap PALETTE then maps every pixel to its nearest colour
of a palette (a hibit character or plate palette.json; luminance-weighted RGB) so the FX share the game's
colours. Alpha is binary by default (cut at 50%), like the hibit finish: pixelfx draws its glow as additive
light, and on transparency that light comes out as near-black translucent pixels (measured on arcane/star-fall:
alpha about 23, luma about 27) that read as dark smudges on any light background. --alpha keep keeps them for
dark backgrounds or additive blending.

pixelfx's stand-in character is never drawn (its --no-actor), and the Patch Pixel font in its gallery (not
MIT) is never used. The frames are code-drawn art by pixelfx: say so when you show or ship them. Success
prints one ASCII JSON line; errors print "error: ..." and exit 1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copies)
import forge_hibit as fh  # noqa: E402
import forge_palette as fp  # noqa: E402

SCHEMA = "generate2dsprite.pixelfx.v1"
TOOL = {"name": "fx_pixelfx", "version": forge_core.FORGE_PACKAGE_VERSION}
REPOSITORY = "https://github.com/patch-gg/pixelfx"
ENV = "PIXELFX_DIR"
NODE_TIMEOUT = 600.0
EFFECT_ID = re.compile(r"[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*")
LIST_LINE = re.compile(r"^(\S+/\S+)\s+(loop|once)\s+([\d.]+)s\s+(\d+)x(\d+)\s+(actor\s+)?(.*)$")
PREVIEW_ZOOM = 3
PREVIEW_BACKDROP = (22, 22, 30)
ART_SOURCE = "code-drawn by pixelfx (third-party code-driven pixel-art effects, MIT)"
NOT_PROVEN = [
    "That the effect reads at the game's scale and against its backgrounds (look at preview.gif and in the game).",
    "Timing against the character's action (pivot and hit frames are the effect's own; place it in the game).",
    "That the license notice reaches the player-facing credits: CREDITS.txt must ship with the frames.",
]


# --------------------------------------------------------------------------- the pixelfx checkout

def find_checkout(value: str | None) -> Path:
    """The pixelfx checkout named by --pixelfx or PIXELFX_DIR, checked for package.json (name pixelfx),
    tools/render.mjs and LICENSE."""
    raw = value or os.environ.get(ENV, "").strip()
    if not raw:
        raise ValueError(f"pixelfx is not installed (it is a separate MIT project, not part of the Forge): "
                         f"git clone {REPOSITORY} <dir>, then pass --pixelfx <dir> or set {ENV}")
    root = Path(raw).expanduser().resolve()
    needed = [root / "package.json", root / "tools" / "render.mjs", root / "LICENSE"]
    if not all(path.is_file() for path in needed):
        raise ValueError(f"{root} is not a pixelfx checkout (package.json, tools/render.mjs and LICENSE expected); "
                         f"git clone {REPOSITORY}")
    package = forge_core.read_json(root / "package.json")
    if not isinstance(package, dict) or package.get("name") != "pixelfx":
        raise ValueError(f"{root / 'package.json'} does not name the pixelfx package")
    return root


def _git(root: Path, *args: str) -> str | None:
    git = shutil.which("git")
    if git is None or not (root / ".git").exists():
        return None
    try:
        done = subprocess.run([git, "-C", str(root), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def provenance(root: Path) -> dict[str, Any]:
    """Where the effects came from: repository, commit, version, license id, copyright lines and the texts
    of LICENSE and NOTICE.md (for CREDITS.txt)."""
    package = forge_core.read_json(root / "package.json")
    license_text = (root / "LICENSE").read_text(encoding="utf-8-sig")
    notice_path = root / "NOTICE.md"
    notice_text = notice_path.read_text(encoding="utf-8-sig") if notice_path.is_file() else None
    remote = _git(root, "config", "--get", "remote.origin.url")
    return {"repository": REPOSITORY, "remote": remote, "commit": _git(root, "rev-parse", "HEAD"),
            "dirty": None if remote is None else bool(_git(root, "status", "--porcelain")),
            "version": package.get("version"), "license": package.get("license"),
            "copyright": [line.strip() for line in license_text.splitlines()
                          if line.strip().lower().startswith("copyright")],
            "licenseText": license_text, "noticeText": notice_text}


def node_executable() -> str:
    node = shutil.which("node")
    if node is None:
        raise ValueError("pixelfx needs Node.js 18 or newer on PATH (https://nodejs.org)")
    return node


def run_node(root: Path, script: str, args: Sequence[str], timeout: float = NODE_TIMEOUT) -> str:
    try:
        done = subprocess.run([node_executable(), script, *args], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError(f"pixelfx {script} took longer than {timeout:g} s") from None
    if done.returncode != 0:
        lines = [line for line in (done.stderr or done.stdout or "").splitlines() if line.strip()]
        raise ValueError(f"pixelfx {script} failed: {lines[-1] if lines else f'exit {done.returncode}'}")
    return done.stdout


# --------------------------------------------------------------------------- list

def list_effects(root: Path, words: Sequence[str] = ()) -> list[dict[str, Any]]:
    effects = []
    for line in run_node(root, "tools/list.mjs", list(words)).splitlines():
        match = LIST_LINE.match(line.rstrip())
        if match:
            effects.append({"id": match.group(1), "loop": match.group(2) == "loop",
                            "seconds": float(match.group(3)), "size": [int(match.group(4)), int(match.group(5))],
                            "aroundCharacter": bool(match.group(6)), "description": match.group(7).strip()})
    return effects


ROUTE_B_STEPS = [
    "route_media.py image: a 2x2 FX sheet of flat lineless art on a flat key (4 phases, one scale)",
    "generate2dsprite.py process --target asset --mode sheet --rows 2 --cols 2 --scale-strategy registered "
    "--cell-size 512",
    "hibit_fx.py pixelize --frames <process output> --target-px N --palette <character palette.json>",
]


def node_version() -> tuple[str | None, int | None]:
    node = shutil.which("node")
    if node is None:
        return None, None
    try:
        done = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None, None
    text = done.stdout.strip()
    match = re.match(r"v(\d+)", text)
    return text or None, int(match.group(1)) if match else None


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    """Which FX route this machine has for hibit games: pixelfx (A) when a checkout and Node 18+ are found,
    else the image route (B). Always exits 0; the user naming a route wins over this answer."""
    version, major = node_version()
    reasons = []
    found = None
    try:
        root = find_checkout(args.pixelfx)
        info = provenance(root)
        found = {"path": str(root), "version": info["version"], "commit": info["commit"], "license": info["license"],
                 "copyright": info["copyright"]}
    except ValueError as error:
        reasons.append(str(error))
    if major is None:
        reasons.append("Node.js is not on PATH (pixelfx needs Node 18 or newer)")
    elif major < 18:
        reasons.append(f"Node.js {version} is too old (pixelfx needs 18 or newer)")
    route = "pixelfx" if found and not reasons else "image"
    return {"status": "ok", "route": route, "pixelfx": found, "node": version, "reasons": reasons,
            "next": (["fx_pixelfx.py list <words> --pixelfx <checkout>",
                      "fx_pixelfx.py render --id <category/effect> --ramp ... --snap <palette.json> --output-dir <new>",
                      "ship CREDITS.txt with the frames; say the FX are code-drawn by pixelfx"]
                     if route == "pixelfx" else ROUTE_B_STEPS),
            "install": None if route == "pixelfx" else f"git clone {REPOSITORY} <dir>, then --pixelfx <dir> or {ENV}"}


def cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    root = find_checkout(args.pixelfx)
    effects = list_effects(root, args.words)
    return {"status": "ok", "pixelfx": str(root), "count": len(effects), "effects": effects,
            "license": "MIT (pixelfx by Patch; see its LICENSE and NOTICE.md)"}


# --------------------------------------------------------------------------- render

def parse_ramp(text: str) -> list[str]:
    colours = [part.strip() for part in text.split(",") if part.strip()]
    if not 2 <= len(colours) <= 16:
        raise ValueError("--ramp takes 2..16 colours, dark to light, such as #1a1030,#5b3fa0,#ffd84a,#fffbe0")
    try:
        return [fh.hex_of(fh.parse_hex(colour)) for colour in colours]
    except ValueError as error:
        raise ValueError(f"--ramp: {error}") from None


def snap_colours(path: Path) -> np.ndarray:
    try:
        pal = fp.read_palette(path)
    except fp.PaletteError as error:
        raise ValueError(f"--snap: {error}") from None
    return np.asarray(pal.rgb[pal.usable], np.uint8)


def snap_frame(frame: np.ndarray, colours: np.ndarray) -> np.ndarray:
    out = frame.copy()
    visible = out[..., 3] > 0
    if visible.any():
        out[..., :3][visible] = colours[fh.index_rgb(out[..., :3][visible], colours)]
    return out


def slice_sheet(sheet: np.ndarray, document: dict[str, Any]) -> list[np.ndarray]:
    frames = []
    for entry in document.get("frames", []):
        box = entry["frame"]
        x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
        frames.append(np.array(sheet[y:y + h, x:x + w], copy=True))
    if not frames:
        raise ValueError("pixelfx wrote a sheet with no frames")
    return frames


def pack_sheet(frames: Sequence[np.ndarray], columns: int) -> np.ndarray:
    height, width = frames[0].shape[:2]
    rows = (len(frames) + columns - 1) // columns
    sheet = np.zeros((rows * height, columns * width, 4), np.uint8)
    for index, frame in enumerate(frames):
        row, column = divmod(index, columns)
        sheet[row * height:(row + 1) * height, column * width:(column + 1) * width] = frame
    return sheet


def preview_gif(frames: Sequence[np.ndarray], fps: float, path: Path) -> None:
    images = []
    for frame in frames:
        alpha = frame[..., 3:4].astype(np.float32) / 255.0
        rgb = frame[..., :3].astype(np.float32) * alpha + np.array(PREVIEW_BACKDROP, np.float32) * (1.0 - alpha)
        zoomed = np.repeat(np.repeat(np.clip(rgb + 0.5, 0, 255).astype(np.uint8), PREVIEW_ZOOM, 0), PREVIEW_ZOOM, 1)
        images.append(Image.fromarray(zoomed, "RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT,
                                                               dither=Image.Dither.NONE))
    images[0].save(path, save_all=True, append_images=images[1:], duration=max(20, round(1000 / fps)), loop=0,
                   disposal=1, optimize=False)


def credits_text(info: dict[str, Any], effect: dict[str, Any], options: dict[str, Any]) -> str:
    commit = info.get("commit") or "unknown commit"
    lines = [
        "These frames were rendered with pixelfx, code-driven pixel-art effects by Patch.",
        f"  {REPOSITORY}  (version {info.get('version') or '?'}, commit {commit})",
        f"  effect {effect['id']} ({effect.get('name') or effect['id']}), options {json.dumps(options, sort_keys=True)}",
        "",
        "pixelfx is MIT licensed. Its license and notice follow; keep them with these frames and credit pixelfx",
        "in the game's credits. (The Patch Pixel font in pixelfx's gallery is not MIT and was not used.)",
        "",
        "----- pixelfx LICENSE -----",
        info["licenseText"].rstrip(),
        "",
    ]
    if info.get("noticeText"):
        lines += ["----- pixelfx NOTICE.md -----", info["noticeText"].rstrip(), ""]
    return "\n".join(lines) + "\n"


def frame_checks(frames: Sequence[np.ndarray], expected: int, palette: np.ndarray | None) -> list[dict[str, Any]]:
    opaque = [int((frame[..., 3] > 0).sum()) for frame in frames]
    edge = [bool((frame[0, :, 3] > 0).any() or (frame[-1, :, 3] > 0).any() or (frame[:, 0, 3] > 0).any()
                 or (frame[:, -1, 3] > 0).any()) for frame in frames]
    stacked = np.concatenate([frame[..., :3][frame[..., 3] > 0] for frame in frames])
    used = np.unique(stacked, axis=0) if len(stacked) else np.zeros((0, 3), np.uint8)
    alpha = np.unique(np.concatenate([np.unique(frame[..., 3]) for frame in frames]))
    checks = [
        {"id": "frames", "status": "pass" if len(frames) == expected else "fail", "value": len(frames),
         "threshold": expected},
        {"id": "not-empty", "status": "pass" if max(opaque) > 0 else "fail", "value": max(opaque), "threshold": 1},
        {"id": "edge-contact", "status": "warn" if any(edge) else "pass", "value": [i for i, hit in enumerate(edge) if hit],
         "threshold": [], "note": "frames with visible pixels on the canvas border (a cut glow shows as a box)"},
        {"id": "colours", "status": "info", "value": int(len(used)), "threshold": None},
        {"id": "alpha-levels", "status": "info", "value": int(len(alpha)), "threshold": None,
         "note": "2 = binary alpha (the default); more = --alpha keep: soft, near-black glow edges"},
    ]
    if palette is not None:
        off = 0
        if len(used):
            packed_used = (used[:, 0].astype(np.int64) << 16) | (used[:, 1].astype(np.int64) << 8) | used[:, 2]
            packed_pal = (palette[:, 0].astype(np.int64) << 16) | (palette[:, 1].astype(np.int64) << 8) | palette[:, 2]
            off = int((~np.isin(packed_used, packed_pal)).sum())
        checks.append({"id": "palette-fit", "status": "pass" if off == 0 else "fail", "value": off, "threshold": 0})
    return checks


def cmd_render(args: argparse.Namespace) -> dict[str, Any]:
    if not EFFECT_ID.fullmatch(args.id):
        raise ValueError(f"--id must be one effect id such as arcane/star-fall (see list), got {args.id!r}")
    root = find_checkout(args.pixelfx)
    info = provenance(root)
    try:
        options = json.loads(args.opts) if args.opts else {}
    except json.JSONDecodeError as error:
        raise ValueError(f"--opts is not JSON: {error}") from None
    if not isinstance(options, dict):
        raise ValueError("--opts must be a JSON object such as {\"count\": 5}")
    if args.ramp:
        options["palette"] = parse_ramp(args.ramp)
    palette = snap_colours(args.snap) if args.snap is not None else None
    if args.fps is not None and not 1 <= args.fps <= 60:
        raise ValueError("--fps must be 1..60")
    final = Path(args.output_dir)
    if os.path.lexists(final):
        raise ValueError(f"output already exists, choose a new path: {final}")
    name = args.id.split("/")[1]
    with tempfile.TemporaryDirectory(prefix="pixelfx-") as temp:
        command = [args.id, "--sheet", "--no-actor", "--bg", "none", "--out", temp, "--max", "4"]
        if args.seed is not None:
            command += ["--seed", str(int(args.seed))]
        if args.fps is not None:
            command += ["--fps", str(int(args.fps))]
        if options:
            command += ["--opts", json.dumps(options)]
        run_node(root, "tools/render.mjs", command)
        made = Path(temp) / args.id
        sheet_json, sheet_png = made / f"{name}.json", made / f"{name}.png"
        if not (sheet_json.is_file() and sheet_png.is_file()):
            raise ValueError(f"pixelfx did not write {name}.png and {name}.json; is {args.id} an effect id?")
        document = forge_core.read_json(sheet_json)
        sheet = np.asarray(forge_core.load_rgba(sheet_png)[0])
    meta = (document.get("meta") or {}).get("pixelfx") or {}
    frames = slice_sheet(sheet, document)
    if args.alpha == "binary":
        for frame in frames:
            frame[..., 3] = np.where(frame[..., 3] >= 128, 255, 0)
            frame[frame[..., 3] == 0] = 0
    if palette is not None:
        frames = [snap_frame(frame, palette) for frame in frames]
    fps = float(meta.get("fps") or args.fps or 30)
    columns = int(meta.get("columns") or max(1, round(len(frames) ** 0.5)))
    effect = {"id": args.id, "name": meta.get("name"), "loop": meta.get("loop"), "fps": fps,
              "frames": len(frames), "frameSize": [int(frames[0].shape[1]), int(frames[0].shape[0])],
              "pivot": meta.get("pivot"), "act": meta.get("act")}
    checks = frame_checks(frames, int(meta.get("frames") or len(frames)), palette)
    status = "fail" if any(check["status"] == "fail" for check in checks) else "needs-visual-review"
    warnings = [f"{check['id']}: {check['value']}" for check in checks if check["status"] == "warn"]
    digits = max(3, len(str(len(frames) - 1)))
    with forge_core.staged_output(final) as stage:
        (stage / "frames").mkdir()
        written = []
        for index, frame in enumerate(frames):
            path = stage / "frames" / f"{name}_{index:0{digits}d}.png"
            forge_core.save_png(frame, path)
            written.append(path)
        packed = pack_sheet(frames, columns)
        forge_core.save_png(packed, stage / "sheet.png")
        height, width = frames[0].shape[:2]
        sheet_doc = {"frames": [{"filename": f"{name} {i}", "frame": {"x": (i % columns) * width,
                                                                      "y": (i // columns) * height,
                                                                      "w": width, "h": height},
                                 "rotated": False, "trimmed": False,
                                 "spriteSourceSize": {"x": 0, "y": 0, "w": width, "h": height},
                                 "sourceSize": {"w": width, "h": height}, "duration": round(1000 / fps)}
                                for i in range(len(frames))],
                     "meta": {"app": REPOSITORY, "image": "sheet.png", "format": "RGBA8888",
                              "size": {"w": int(packed.shape[1]), "h": int(packed.shape[0])}, "scale": "1",
                              "frameTags": [{"name": name, "from": 0, "to": len(frames) - 1,
                                             "direction": "forward"}],
                              "pixelfx": {**meta, "frames": len(frames), "columns": columns},
                              "license": "MIT (pixelfx by Patch); see CREDITS.txt"}}
        forge_core.write_json(stage / "sheet.json", sheet_doc)
        preview_gif(frames, fps, stage / "preview.gif")
        (stage / "CREDITS.txt").write_text(credits_text(info, effect, options), encoding="utf-8", newline="\n")
        outputs = [forge_core.file_ref(path, stage) for path in
                   [*written, stage / "sheet.png", stage / "sheet.json", stage / "preview.gif", stage / "CREDITS.txt"]]
        inputs = [] if args.snap is None else [forge_core.file_ref(args.snap, final)]
        record = {
            "schema": SCHEMA, "tool": dict(TOOL), "artSource": ART_SOURCE,
            "pixelfx": {key: info[key] for key in ("repository", "remote", "commit", "dirty", "version", "license",
                                                   "copyright")},
            "credits": "CREDITS.txt (pixelfx LICENSE and NOTICE.md; ship it with the frames)",
            "effect": effect, "options": options, "seed": args.seed, "actor": False,
            "recolour": {"ramp": options.get("palette") if args.ramp else None,
                         "snap": None if palette is None else {**inputs[0], "colors": int(len(palette))}},
            "alpha": args.alpha,
            "engine": {"sampling": "nearest", "pixelArt": True, "pivot": meta.get("pivot"),
                       "note": "pivot is the effect's origin in frame px (pixelfx meta.origin): place it on the "
                               "caster's hand, the target or the floor point the effect is drawn around"},
            "qa": {"status": status, "method": "fx_pixelfx v1: pixelfx tools/render.mjs --sheet --no-actor --bg none "
                                               "(the effect alone on transparency), sliced by its Aseprite JSON, "
                                               "optional binary alpha and nearest-colour snap (luminance-weighted "
                                               "RGB), re-packed",
                   "notProven": NOT_PROVEN, "checks": checks, "inputs": inputs, "outputs": outputs,
                   "tool": dict(TOOL)},
        }
        forge_core.write_json(stage / "fx.json", record)
    final = final.resolve()
    return {"status": status, "output": str(final), "metadata": str(final / "fx.json"),
            "frames": len(frames), "frameSize": effect["frameSize"], "fps": fps, "loop": effect["loop"],
            "pivot": effect["pivot"], "preview": str(final / "preview.gif"), "credits": str(final / "CREDITS.txt"),
            "pixelfx": {"version": info["version"], "commit": info["commit"], "license": info["license"]},
            "artSource": ART_SOURCE, "_warnings": warnings}


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fx_pixelfx.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pixelfx", help=f"the pixelfx checkout (default: ${ENV}); git clone {REPOSITORY}")
    check = verbs.add_parser("check", parents=[common],
                             help="which FX route this machine has: pixelfx (A) or the image route (B)",
                             description="Report the FX route for hibit games: pixelfx (A) when a pixelfx checkout "
                                         "and Node.js 18+ are found, else the image route (B: an FX sheet, "
                                         "process, hibit_fx.py). Always exits 0.")
    check.set_defaults(func=cmd_check)
    listing = verbs.add_parser("list", parents=[common], help="the checkout's effects as JSON (words filter)")
    listing.add_argument("words", nargs="*", help="filter words matched against ids, names, tags and descriptions")
    listing.set_defaults(func=cmd_list)
    render = verbs.add_parser("render", parents=[common], help="one effect -> frames, sheet, preview, fx.json, CREDITS",
                              description="Render one pixelfx effect (alone, on transparency) into a new folder with "
                                          "frames, an Aseprite-style sheet, a preview GIF, fx.json provenance and "
                                          "CREDITS.txt (pixelfx's MIT license and notice).")
    render.add_argument("--id", required=True, help="effect id, such as arcane/star-fall (see list)")
    render.add_argument("--output-dir", required=True, help="new folder (must not exist)")
    render.add_argument("--ramp", help="the effect's palette option: 2..16 colours dark to light, comma separated")
    render.add_argument("--snap", type=Path, help="palette file: map every pixel to its nearest colour of it")
    render.add_argument("--alpha", choices=("binary", "keep"), default="binary",
                        help="binary (default, cut at 50%%, like the hibit finish) or keep: pixelfx glow exported on "
                             "transparency is near-black and translucent, a dark smudge on light backgrounds")
    render.add_argument("--seed", type=int, help="the effect's random seed (default: the effect's own)")
    render.add_argument("--fps", type=int, help="frame rate 1..60 (default: the effect's own, usually 30)")
    render.add_argument("--opts", help="more effect options as a JSON object (the effect file's meta.options)")
    render.set_defaults(func=cmd_render)
    return parser


def _cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = args.func(args)
    for warning in summary.pop("_warnings", []):
        print(f"warning: {forge_core.ascii_text(warning)}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return 1 if summary.get("status") == "fail" else 0


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI under forge_core.run_cli (D26, D27)."""
    return forge_core.run_cli(_cli, argv)


if __name__ == "__main__":
    raise SystemExit(main())
