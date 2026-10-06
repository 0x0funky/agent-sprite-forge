"""sprite_set.py: plan presets, the prompt library, the numeric gates, and the whole set flow with a fake media CLI
(FORGE_ROUTE_MEDIA_FAKE): a forced failure and its retake, resume, review/accept/retake, report, a missing route
with a supplied clip, the best usable window after the takes run out, and the finish_frames.py seam.

No real generation and no network: the fake writes a magenta-backdrop clip of a procedural hero with ffmpeg from
numpy frames. A FAKE_FAIL mode breaks a take until the prompt carries the fix clause that cures it.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import SKILLS_DIR, load_script, require_ffmpeg, run_cli

SCRIPTS = SKILLS_DIR / "video2dsprite" / "scripts"
SPRITE_SET = SCRIPTS / "sprite_set.py"
DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")

FAKE_MEDIA = r'''
"""Fake generate2dmedia route_media.py: video --prompt-file P --reference input.png [--last-frame F] --duration S
--resolution R --out-dir D writes D/clip.mp4 (ffmpeg from numpy frames) and prints one JSON line."""
import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SIZE, GROUND = 192, 172
FIXES = {"turn": "never faces the camera", "zoom": "no push-in, no pull-out", "extra": "no pebbles",
         "edge": "clear margin on every side", "noise": "no gradient, no glow, no haze"}   # "tail" is never fixed


def _limb(draw, x0, y0, angle, length, width, colour):
    x1, y1 = x0 + length * math.sin(angle), y0 + length * math.cos(angle)
    draw.line((x0, y0, x1, y1), fill=colour, width=width)
    draw.ellipse((x1 - width / 2, y1 - width / 2, x1 + width / 2, y1 + width / 2), fill=colour)
    return x1, y1


def hero(t, action="idle"):
    """Side-view hero facing LEFT on a transparent 192 x 192 canvas; t = 0 of every action is the master."""
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx, hip = 96.0, 124.0
    bob, lean, lift, leg_a, leg_b, arm = 0.0, 0.0, 0.0, 0.25, -0.25, 0.35
    if action == "idle":
        bob = 2.5 * math.sin(2 * math.pi * t / 24)
        arm = 0.35 + 0.08 * math.sin(2 * math.pi * t / 24)
    elif action in ("walk", "run"):
        period, swing = (16, 0.55) if action == "walk" else (12, 0.75)
        phase = 2 * math.pi * t / period
        leg_a, leg_b = swing * math.sin(phase), -swing * math.sin(phase)
        bob, arm = -2.0 * abs(math.cos(phase)), 0.35 - 0.4 * math.sin(phase)
    elif action == "attack":
        if 8 <= t < 16:
            arm = 0.35 - 1.6 * (t - 8) / 8
        elif 16 <= t < 21:
            arm, lean = -1.25 + 2.85 * (t - 16) / 5, -4 * (t - 16) / 5
        elif 21 <= t < 33:
            arm, lean = 1.6 - 1.25 * (t - 21) / 12, -4 + 4 * (t - 21) / 12
    elif action == "jump":
        if 6 <= t < 11:
            bob = 6 * (t - 6) / 5
        elif 11 <= t < 27:
            u = (t - 11) / 16
            lift, leg_a, leg_b = 30 * 4 * u * (1 - u), 0.9, -0.2
        elif 27 <= t < 32:
            bob = 6 * (32 - t) / 5
    elif action == "hurt" and 8 <= t < 18:
        lean = 5 * math.sin(math.pi * (t - 8) / 10)
    hip_y, cx = hip + bob - lift, cx + lean
    for angle in (leg_b, leg_a):
        _limb(draw, cx, hip_y, angle, GROUND - hip - 2, 12, (50, 52, 74))
    draw.rounded_rectangle((cx - 16, hip_y - 50, cx + 16, hip_y + 4), 6, fill=(40, 110, 200))
    hx, hy = cx - 2, hip_y - 68
    draw.ellipse((hx - 19, hy - 19, hx + 19, hy + 19), fill=(232, 190, 158))
    draw.pieslice((hx - 20, hy - 21, hx + 22, hy + 14), 200, 360, fill=(96, 52, 22))
    draw.rectangle((hx + 8, hy - 8, hx + 21, hy + 12), fill=(96, 52, 22))
    draw.ellipse((hx - 13, hy - 5, hx - 7, hy + 1), fill=(20, 20, 30))
    draw.polygon([(hx - 18, hy - 2), (hx - 27, hy + 5), (hx - 17, hy + 8)], fill=(214, 166, 134))
    sx, sy = cx - 6, hip_y - 42
    hand = _limb(draw, sx, sy, -arm, 30, 9, (40, 110, 200))
    dx, dy = hand[0] - sx, hand[1] - sy
    norm = math.hypot(dx, dy) or 1.0
    draw.line((hand[0], hand[1], hand[0] + 34 * dx / norm, hand[1] + 34 * dy / norm), fill=(205, 205, 214), width=5)
    return image


def render(action, count, broken, scale, offset, size, key=(255, 0, 255)):
    """(keyed RGBA, provider RGB) per frame: the hero placed by (scale, offset) on a size canvas."""
    sys.path.insert(0, os.environ["FAKE_SCRIPTS"])
    import forge_core
    width, height = size
    key = np.asarray(key, np.float64)
    out = []
    for t in range(count):
        model = hero(t, action)
        if "turn" in broken and 0.3 * count <= t < 0.7 * count:
            model = model.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        frame_scale, frame_offset = scale, offset
        if "zoom" in broken:
            grow = 1 + 0.3 * t / max(1, count - 1)
            feet = (offset[0] + scale * 96, offset[1] + scale * GROUND)
            frame_scale = scale * grow
            frame_offset = (feet[0] - frame_scale * 96, feet[1] - frame_scale * GROUND)
        if "edge" in broken and 0.4 * count <= t < 0.6 * count:
            frame_offset = (-scale * 70, frame_offset[1])
        rgba = np.array(forge_core.resample_rgba(model, frame_scale, "lanczos", anchor_src=(0.0, 0.0),
                                                 anchor_dst=tuple(frame_offset), out_size=(width, height)))
        puff = ("extra" in broken and 0.3 * count <= t < 0.6 * count) or ("tail" in broken and t >= count - 8)
        if puff:
            yy, xx = np.mgrid[0:height, 0:width]
            centre = (offset[0] + scale * 52, offset[1] + scale * (GROUND - 6))
            rgba[(xx - centre[0]) ** 2 + (yy - centre[1]) ** 2 < (8 * scale) ** 2] = (150, 140, 120, 255)
        alpha = rgba[..., 3:].astype(np.float64) / 255.0
        rgb = rgba[..., :3] * alpha + key * (1 - alpha)
        if "noise" in broken and 0.3 * count <= t < 0.6 * count:
            rgb[: height // 3] = rgb[: height // 3] * 0.6 + np.array([90, 160, 90]) * 0.4
        out.append((rgba, np.clip(rgb + 0.5, 0, 255).astype(np.uint8)))
    return out


def main():
    parser = argparse.ArgumentParser()
    video = parser.add_subparsers(dest="command", required=True).add_parser("video")
    video.add_argument("--prompt-file", required=True)
    video.add_argument("--reference", required=True)
    if not os.environ.get("FAKE_NO_LAST_FRAME"):
        video.add_argument("--last-frame")
    video.add_argument("--duration", type=float, default=6)
    video.add_argument("--resolution", default="720p")
    video.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    out = Path(args.out_dir)
    action = out.parents[2].name.split("-")[-1]
    if os.environ.get("FAKE_LOG"):
        with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"action": action, "take": out.parent.name,
                                     "lastFrame": bool(getattr(args, "last_frame", None))}) + "\n")
    if os.environ.get("FAKE_NO_ROUTE"):
        print("error: no video route: no API key and no verified local daemon", file=sys.stderr)
        return 3
    if os.environ.get("FAKE_ERROR"):
        print("error: the provider timed out", file=sys.stderr)
        return 1
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    broken = set()
    for item in os.environ.get("FAKE_FAIL", "").split(","):
        name, _, mode = item.strip().partition(":")
        if name == action and mode and (mode not in FIXES or FIXES[mode] not in prompt):
            broken.add(mode)
    reference = np.asarray(Image.open(args.reference).convert("RGB")).astype(np.int32)
    key = reference[0, 0]
    subject = np.abs(reference - key).sum(-1) > 90
    rows, cols = np.flatnonzero(subject.any(1)), np.flatnonzero(subject.any(0))
    model = np.asarray(hero(0, action))[..., 3] > 127
    mrows, mcols = np.flatnonzero(model.any(1)), np.flatnonzero(model.any(0))
    scale = (rows[-1] + 1 - rows[0]) / (mrows[-1] + 1 - mrows[0])
    offset = (cols[0] - scale * mcols[0], rows[0] - scale * mrows[0])
    height, width = reference.shape[:2]
    count = int(round(min(args.duration, float(os.environ.get("FAKE_SECONDS", "1.5"))) * 24))
    frames = [rgb for _, rgb in render(action, count, broken, scale, offset, (width, height), key)]
    out.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt",
                    "rgb24", "-s", f"{width}x{height}", "-r", "24", "-i", "pipe:0", "-c:v", "libx264", "-crf", "12",
                    "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out / "clip.mp4")],
                   input=b"".join(np.ascontiguousarray(f).tobytes() for f in frames), check=True, capture_output=True,
                   timeout=300)
    print(json.dumps({"status": "ok", "route": "fake-i2v", "artifact": "clip.mp4", "frames": count,
                      "lastFrame": bool(getattr(args, "last_frame", None)), "broken": sorted(broken)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

FAKE_FINISHER = r'''
"""Fake finish_frames.py: the documented CLI, delegating to the sprite_set stand-in; logs its arguments."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["FAKE_SCRIPTS"])
import sprite_set

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("hd", "pixel"))
parser.add_argument("--frames", required=True)
parser.add_argument("--out", required=True)
parser.add_argument("--target-height", type=int, required=True)
parser.add_argument("--scale-ref")
parser.add_argument("--palette")
args = parser.parse_args()
with open(os.environ["FAKE_FINISH_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"out": Path(args.out).parent.parent.parent.name, "scaleRef": bool(args.scale_ref),
                             "targetHeight": args.target_height}) + "\n")
summary = sprite_set.standin_finish(args.mode, Path(args.frames), Path(args.out), args.target_height,
                                    Path(args.scale_ref) if args.scale_ref else None,
                                    Path(args.palette) if args.palette else None)
print(json.dumps(summary))
'''


# --------------------------------------------------------------------------- fixtures and helpers

@pytest.fixture(scope="module")
def ss():
    return load_script("video2dsprite", "sprite_set")


@pytest.fixture(scope="module")
def fake(tmp_path_factory):
    folder = tmp_path_factory.mktemp("fakes")
    media, finisher = folder / "fake_route_media.py", folder / "fake_finish_frames.py"
    media.write_text(FAKE_MEDIA, encoding="utf-8")
    finisher.write_text(FAKE_FINISHER, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("sprite_set_fake_media", media)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {"media": media, "finisher": finisher, "module": module}


def write_master(folder: Path, fake: dict, ss, *, size: int = 128, facing: str = "left", name: str = "testhero",
                 schema: str = "generate2dsprite.master.v1") -> Path:
    """An opaque padded master on flat magenta (the hero's frame 0 at size/192) and its master.json."""
    folder.mkdir(parents=True, exist_ok=True)
    model = np.asarray(fake["module"].hero(0, "idle"))
    small = np.asarray(ss.forge_core.resample_rgba(model, size / 192, "lanczos", out_size=(size, size)))
    alpha = small[..., 3:].astype(np.float64) / 255
    rgb = small[..., :3] * alpha + np.array([255.0, 0.0, 255.0]) * (1 - alpha)
    Image.fromarray(np.clip(rgb + 0.5, 0, 255).astype(np.uint8), "RGB").save(folder / "master.png")
    rows = np.flatnonzero((small[..., 3] > 127).any(1))
    master = {"schema": schema, "name": name,
              "identity_recap": "HD 2D hero boy with short brown hair, a blue tunic, dark trousers and a short steel "
                                "sword held low in his front hand",
              "facing": facing, "finish": "hd", "class": "hero",
              "framing": {"canvas": [size, size], "subject_height_px": int(rows[-1] + 1 - rows[0]),
                          "top_margin_px": int(rows[0])},
              "key": "magenta",
              "files": {"master": {"path": "master.png",
                                   "sha256": ss.forge_core.sha256_file(folder / "master.png")}},
              "references": [], "transform": {"pad": "test"}, "route": "test"}
    (folder / "master.json").write_text(json.dumps(master, indent=2), encoding="utf-8")
    return folder / "master.json"


def cli(*args, env: dict | None = None, cwd: Path | None = None):
    result = run_cli([SPRITE_SET, *[str(arg) for arg in args]], "cp1252", env=env, cwd=cwd, timeout=900)
    summary = None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if lines:
        try:
            summary = json.loads(lines[-1])
        except ValueError:
            summary = None
    return result, summary


def fake_env(fake: dict, tmp: Path, **extra) -> dict:
    env = {"FORGE_ROUTE_MEDIA_FAKE": str(fake["media"]), "FAKE_SCRIPTS": str(SCRIPTS), "FAKE_SECONDS": "1.5",
           "FAKE_LOG": str(tmp / "fake-media.log"), "FORGE_FINISH_FRAMES": "standin", "FAKE_FAIL": "",
           "FAKE_NO_ROUTE": "", "FAKE_NO_LAST_FRAME": "", "FAKE_ERROR": "",
           "FAKE_FINISH_LOG": str(tmp / "fake-finish.log")}
    env.update({key: str(value) for key, value in extra.items()})
    return env


def log_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


def assert_no_absolute_paths(folder: Path) -> None:
    for path in folder.rglob("*"):
        if path.suffix in (".json", ".jsonl") and path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not DRIVE_PATH.search(text), f"{path.relative_to(folder)} holds an absolute path"


# --------------------------------------------------------------------------- library and prompts

def test_library_covers_every_action_gate_and_clause(ss):
    library = ss.load_library()
    assert set(ss.ACTION_PRESETS) <= set(library.templates)
    assert set(ss.GATE_IDS) == set(library.gatefix)
    for gate, clauses in library.gatefix.items():
        assert clauses and all(clause in library.clauses for clause in clauses), gate
    # the clauses distilled from the game-opus55 rejection logs
    for clause in ("never-turn", "camera-locked", "identity-lock", "face-visible", "weapon-shape", "no-extras",
                   "no-text", "no-flares", "flat-background", "inside-frame", "same-start-end"):
        assert clause in library.clauses


def test_prompt_shape_fixes_once_and_lint_clean(ss):
    library = ss.load_library()
    plan = {"masters": {"side": {"facing": "left", "identityRecap": "A pixel-art ghost spearman with a wide hat."}},
            "key": "magenta", "style": "16-bit pixel-art", "pronoun": "his"}
    for action in ss.ACTION_PRESETS:
        entry = {"view": "side", "action": action, "motion": library.templates[action],
                 "negatives": library.negatives.get(action, ""),
                 "returnsToRest": ss.ACTION_PRESETS[action].returns_to_rest}
        prompt = ss.build_prompt(entry, plan, [library.clauses["never-turn"]] * 2)
        assert prompt.startswith("The same pixel-art ghost spearman with a wide hat, facing LEFT the entire time "
                                 "and never turning around, ")
        assert "{" not in prompt and prompt.count("never faces the camera") == 1
        assert prompt.rstrip().endswith("camera locked, no zoom, no pan; solid flat pure magenta background only "
                                        "for the whole shot; keep the exact 16-bit pixel-art look, palette, "
                                        "proportions and costume of the still; single continuous action.")
        assert ("starts AND ends in exactly the same pose as the still" in prompt) == entry["returnsToRest"]
        assert ss.lint_findings(prompt, "magenta") == [], action
    assert "to the LEFT" in ss.build_prompt({"view": "side", "action": "attack", "motion": library.templates["attack"],
                                             "negatives": "", "returnsToRest": True}, plan, [])


# --------------------------------------------------------------------------- the numeric gates

def _take(ss, fake, action, broken=(), count=36):
    rendered = fake["module"].render(action, count, set(broken), 1.0, (0.0, 0.0), (192, 192))
    frames = [ss.qc_frame(rgba, rgb, 1, 2) for rgba, rgb in rendered]
    reference = ss.qc_frame(np.asarray(fake["module"].hero(0, action)), None, 1, 2)
    return frames, reference


def _judge(ss, frames, reference, action):
    preset = ss.ACTION_PRESETS[action]
    return ss.evaluate_take(frames, reference, key="magenta", gates=ss.default_gates(action, preset, "hero", "side"),
                            fps=24.0, facing="left", kind=preset.kind, airborne=preset.airborne,
                            returns_to_rest=preset.returns_to_rest, hit_tick=preset.hit_tick)


@pytest.fixture()
def scripts_env(monkeypatch):
    monkeypatch.setenv("FAKE_SCRIPTS", str(SCRIPTS))


def test_clean_take_passes_and_finds_the_hit(ss, fake, scripts_env):
    result = _judge(ss, *_take(ss, fake, "attack"), "attack")
    assert result["status"] == "pass", result["reasons"]
    assert {gate: item["status"] for gate, item in result["gates"].items()} == {
        "area": "pass", "feet": "pass", "identity": "pass", "zoom": "pass", "turn": "pass", "edge": "pass",
        "background": "pass", "extra": "pass", "motion": "pass", "end-pose": "pass"}
    motion = result["motion"]
    assert 16 <= motion["impact"] <= 22 and motion["cancel"] > motion["impact"]   # the strike, then the recovery
    assert result["usableWindow"] == [0, 36] and result["gates"]["identity"]["value"]["start"] > 0.95


@pytest.mark.parametrize("action, broken, gate, frames", [
    ("attack", "turn", "turn", range(11, 26)),       # mirrored for 30%-70% of the clip
    ("attack", "zoom", "zoom", None),                # pushes in 30% by the end
    ("idle", "zoom", "zoom", None),                  # a loop drifting in scale
    ("walk", "extra", "extra", range(11, 22)),       # a dust puff detached from the body
    ("hurt", "edge", "edge", range(15, 22)),         # pushed off the left edge
    ("idle", "noise", "background", range(11, 22)),  # a green haze over the backdrop
])
def test_each_failure_fails_its_gate_with_frame_numbers(ss, fake, scripts_env, action, broken, gate, frames):
    result = _judge(ss, *_take(ss, fake, action, (broken,)), action)
    assert result["status"] == "fail" and gate in result["failures"], result["reasons"]
    reason = next(text for text in result["reasons"] if text.startswith(gate + ":"))
    assert re.search(r"\d", reason)
    if frames is not None:
        assert set(result["gates"][gate]["frames"]) <= set(frames) and result["gates"][gate]["frames"]
        assert not any(result["perFrame"]["usable"][i] for i in result["gates"][gate]["frames"])
    library = ss.load_library()
    assert library.gatefix[gate]   # every failing gate has a prompt fix


def test_frozen_take_fails_motion_and_usable_window_skips_a_bad_tail(ss, fake, scripts_env):
    frames, reference = _take(ss, fake, "idle")
    frozen = _judge(ss, [frames[0]] * len(frames), reference, "idle")
    assert "motion" in frozen["failures"]
    tail = _judge(ss, *_take(ss, fake, "idle", ("tail",)), "idle")   # extra objects in the last 8 frames
    assert tail["failures"] == ["extra"] and tail["usableWindow"] == [0, 28]


# --------------------------------------------------------------------------- plan

def test_plan_presets_pins_and_refusals(ss, fake, tmp_path):
    master = write_master(tmp_path / "art", fake, ss)
    result, summary = cli("plan", "--master", master, "--output-dir", tmp_path / "set", "--target-height", 64)
    assert result.returncode == 0, result.stderr
    assert summary["actions"] == ["idle", "walk", "run", "attack", "jump", "hurt"]
    assert summary["pinned"] == ["idle", "attack"]
    plan = json.loads((tmp_path / "set" / "set_plan.json").read_text(encoding="utf-8"))
    places = {entry["id"]: entry["placement"] for entry in plan["actions"]}
    assert places["idle"]["canvas"] == [128, 128] and places["idle"]["scale"] == 1.0
    width, height = places["attack"]["canvas"]
    assert height == 128 and abs(width / height - 16 / 9) < 0.02          # attack is wider
    assert places["attack"]["root"][0] > width / 2                         # led away from the facing (left)
    assert places["attack"]["padding"][0] > 0 and places["attack"]["padding"][2] > 0
    width, height = places["jump"]["canvas"]
    assert abs(width / height - 0.75) < 0.02 and places["jump"]["headroom"] >= 0.339   # jump has headroom
    assert places["jump"]["padding"][1] > 0 and places["jump"]["scale"] < 1
    kinds = {entry["id"]: (entry["kind"], entry["lock"]) for entry in plan["actions"]}
    assert kinds == {"idle": ("loop", "feet"), "walk": ("loop", "feet"), "run": ("loop", "none"),
                     "attack": ("oneshot", "feet"), "jump": ("oneshot", "feet"), "hurt": ("oneshot", "feet")}
    for entry in plan["actions"]:
        assert "facing LEFT the entire time and never turning around" in entry["prompt"]
        assert "blue tunic" in entry["prompt"] and entry["lint"] == []
    assert plan["finish"] == {"mode": "hd", "targetHeight": 64, "scaleRefAction": "idle", "palette": None}
    assert (tmp_path / "set" / "master" / "side" / "master.png").is_file()
    assert_no_absolute_paths(tmp_path / "set")

    again, _ = cli("plan", "--master", master, "--output-dir", tmp_path / "set")
    assert again.returncode == 1 and "error:" in again.stderr
    front, _ = cli("plan", "--master", master, "--output-dir", tmp_path / "front", "--views", "side,front")
    assert front.returncode == 1 and "--view-master" in front.stderr
    wrong = write_master(tmp_path / "wrong", fake, ss, schema="something.else.v1")
    bad, _ = cli("plan", "--master", wrong, "--output-dir", tmp_path / "bad")
    assert bad.returncode == 1 and "generate2dsprite.master.v1" in bad.stderr and not (tmp_path / "bad").exists()


def test_front_view_uses_its_own_master(ss, fake, tmp_path):
    side = write_master(tmp_path / "side", fake, ss)
    front = write_master(tmp_path / "front", fake, ss, facing="front")
    result, summary = cli("plan", "--master", side, "--output-dir", tmp_path / "set", "--actions", "idle,attack",
                          "--views", "side,front", "--view-master", f"front={front}")
    assert result.returncode == 0, result.stderr
    assert summary["actions"] == ["idle", "attack", "front-idle", "front-attack"]
    plan = json.loads((tmp_path / "set" / "set_plan.json").read_text(encoding="utf-8"))
    entry = next(item for item in plan["actions"] if item["id"] == "front-attack")
    assert "facing the viewer the entire time" in entry["prompt"] and "toward the viewer" in entry["prompt"]
    assert entry["gates"]["turn"]["enabled"] is False
    alone, summary = cli("plan", "--master", front, "--output-dir", tmp_path / "front-only", "--actions", "idle",
                         "--views", "front")   # without a side view, --master is the first view's still
    assert alone.returncode == 0 and summary["actions"] == ["front-idle"]


# --------------------------------------------------------------------------- the whole flow

@pytest.mark.ffmpeg
def test_set_flow_retake_resume_review_accept_retake_report(ss, fake, tmp_path):
    require_ffmpeg()
    master = write_master(tmp_path / "art", fake, ss)
    plan_path = tmp_path / "set" / "set_plan.json"
    _, summary = cli("plan", "--master", master, "--output-dir", tmp_path / "set", "--actions", "idle,walk,attack",
                     "--target-height", 48, "--jobs", 2)
    assert summary["maxClips"] == 9
    env = fake_env(fake, tmp_path, FAKE_FAIL="attack:turn", FORGE_FINISH_FRAMES=fake["finisher"])

    result, summary = cli("run", "--plan", plan_path, "--stagger", 0, env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert summary["status"] == "complete" and summary["generated"] == 4
    assert {k: (v["status"], v["takes"], v["chosen"]) for k, v in summary["actions"].items()} == {
        "idle": ("done", 1, 1), "walk": ("done", 1, 1), "attack": ("done", 2, 2)}
    assert summary["finisher"] == "fake_finish_frames.py" and summary["needsReview"] == ["idle", "walk", "attack"]
    calls = log_lines(tmp_path / "fake-media.log")
    assert sorted((c["action"], c["take"], c["lastFrame"]) for c in calls) == [
        ("attack", "t01", True), ("attack", "t02", True), ("idle", "t01", True),
        ("walk", "t01", False)]   # idle and attack are pinned to the master
    finishes = log_lines(tmp_path / "fake-finish.log")
    assert [(f["out"], f["scaleRef"]) for f in finishes] == [("idle", False), ("walk", True), ("attack", True)]

    takes = log_lines(tmp_path / "set" / "takes.jsonl")
    first = next(t for t in takes if t["action"] == "attack" and t["take"] == 1)
    assert first["status"] == "rejected" and "turn" in first["failures"] and "never-turn" in first["fixNext"]
    assert any(reason.startswith("turn: turned around in frames 1") for reason in first["reasons"])
    assert first["badFrames"]["turn"] and min(first["badFrames"]["turn"]) >= 10
    prompts = {n: (tmp_path / "set" / "actions" / "attack" / "takes" / f"t0{n}" / "prompt.txt").read_text("utf-8")
               for n in (1, 2)}
    assert "never faces the camera" not in prompts[1] and "never faces the camera" in prompts[2]

    state = json.loads((tmp_path / "set" / "set_state.json").read_text(encoding="utf-8"))
    for ident in ("idle", "walk", "attack"):
        output = state["actions"][ident]["output"]
        package = tmp_path / "set" / output["package"]
        assert output["verify"] == "pass" and (package / "verify-qa.json").is_file()
        assert {path.suffix for path in package.iterdir()} >= {".json", ".png", ".webm", ".mp4"}
    attack = json.loads((tmp_path / "set" / state["actions"]["attack"]["output"]["manifest"]).read_text("utf-8"))
    assert attack["loopPolicy"] == "oneshot" and attack["impactMs"] > 0
    assert {event["name"] for event in attack["events"]} >= {"in", "hit", "end"}
    idle = json.loads((tmp_path / "set" / state["actions"]["idle"]["output"]["manifest"]).read_text("utf-8"))
    assert idle["loopPolicy"] == "cycle" and idle["bodyHeightPx"] == 48
    walk = state["actions"]["walk"]["takes"][0]["stages"]["motion"]
    assert walk["tool"] == "gait_loop select" and walk["policy"] == "cycle"
    assert walk["endExclusive"] - walk["start"] == 16   # one stride of the fake's 16-frame walk

    # resume: nothing is generated or redone
    before = state["actions"]["attack"]["takes"][1]["stages"]["package"]
    result, summary = cli("run", "--plan", plan_path, "--stagger", 0, env=env)
    assert result.returncode == 0 and summary["generated"] == 0 and summary["status"] == "complete"
    assert len(log_lines(tmp_path / "fake-media.log")) == 4
    again = json.loads((tmp_path / "set" / "set_state.json").read_text(encoding="utf-8"))
    assert again["actions"]["attack"]["takes"][1]["stages"]["package"] == before and again["runs"] == 2

    # accept needs a review; review writes every sheet; accept then records the approval
    refused, _ = cli("accept", "--plan", plan_path, "--action", "idle", "--take", 1)
    assert refused.returncode == 1 and "review" in refused.stderr
    result, summary = cli("review", "--plan", plan_path)
    assert result.returncode == 0, result.stderr
    names = sorted(Path(path).relative_to(tmp_path / "set" / "review").as_posix() for path in summary["sheets"])
    assert names == ["attack/t01-sheet.png", "attack/t02-sheet.png", "idle/t01-sheet.png", "walk/t01-sheet.png"]
    assert len(summary["finals"]) == 3 and Path(summary["lineup"]).is_file()
    with Image.open(summary["sheets"][0]) as sheet:
        assert sheet.width >= 760 and sheet.height > 200
    review = json.loads(Path(summary["review"]).read_text(encoding="utf-8"))
    assert review["schema"] == "video2dsprite.sprite_set_review.v1" and "EVERY sheet" in review["instructions"]
    result, summary = cli("accept", "--plan", plan_path, "--action", "idle", "--take", 1, "--note", "looked")
    assert result.returncode == 0 and summary["accepted"] is True and summary["status"] == "done"

    # a semantic retake: the next run makes take 3 with the new clause and keeps every earlier fix
    result, summary = cli("retake", "--plan", plan_path, "--action", "attack", "--fix", "weapon-shape")
    assert result.returncode == 0 and summary["round"] == 2 and summary["fixes"] == ["weapon-shape"]
    result, summary = cli("run", "--plan", plan_path, "--stagger", 0, env=env)
    assert result.returncode == 0, result.stderr
    assert summary["actions"]["attack"] == {"status": "done", "takes": 3, "chosen": 3, "accepted": False,
                                            "package": "actions/attack/takes/t03/package", "message": None}
    assert summary["actions"]["idle"]["accepted"] is True and len(log_lines(tmp_path / "fake-media.log")) == 5
    third = (tmp_path / "set" / "actions" / "attack" / "takes" / "t03" / "prompt.txt").read_text("utf-8")
    assert "never doubling, no second blade" in third and "never faces the camera" in third

    result, summary = cli("report", "--plan", plan_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(Path(summary["output"]).read_text(encoding="utf-8"))
    assert report["schema"] == "video2dsprite.sprite_set_report.v1" and report["status"] == "complete"
    assert report["accepted"] == ["idle"] and report["needsReview"] == ["walk", "attack"]
    rows = {item["id"]: item for item in report["actions"]}
    assert rows["idle"]["route"] == "fake-i2v" and rows["idle"]["loop"]["policy"] in ("cycle", "pingpong")
    assert rows["walk"]["loop"]["policy"] == "cycle" and rows["walk"]["timing"] is None
    assert rows["attack"]["timing"]["impactMs"] > 0 and len(rows["attack"]["takes"]) == 3
    assert rows["attack"]["takes"][0]["qc"]["gates"]["turn"]["status"] == "fail"
    for item in rows.values():
        for key in ("manifest", "webm", "packed", "poster"):
            assert (tmp_path / "set" / item["outputs"][key]).is_file()
        assert item["finish"]["finisher"] == "fake_finish_frames.py"
    assert_no_absolute_paths(tmp_path / "set")


@pytest.mark.ffmpeg
def test_no_route_supplied_clip_unpinned_route_and_best_window(ss, fake, tmp_path):
    require_ffmpeg()
    master = write_master(tmp_path / "art", fake, ss)
    plan_path = tmp_path / "set" / "set_plan.json"
    cli("plan", "--master", master, "--output-dir", tmp_path / "set", "--actions", "idle", "--max-takes", 2,
        "--formats", "png", "--target-height", 40)

    # no route: the run waits (exit 3) and names the folder for a clip made elsewhere
    result, summary = cli("run", "--plan", plan_path, "--stagger", 0, env=fake_env(fake, tmp_path, FAKE_NO_ROUTE=1))
    assert result.returncode == 3 and summary["status"] == "waiting-for-clips"
    assert summary["actions"]["idle"]["status"] == "no-route"
    assert "actions/idle/takes/t01/media/clip.mp4" in summary["actions"]["idle"]["message"]

    # a host makes the clip by hand (here: the fake with a defect in the last 8 frames) and saves it there
    take = tmp_path / "set" / "actions" / "idle" / "takes" / "t01"
    made = run_cli([fake["media"], "video", "--prompt-file", take / "prompt.txt", "--reference",
                    tmp_path / "set" / "actions" / "idle" / "job" / "input.png", "--duration", "6", "--out-dir",
                    take / "media"], env=fake_env(fake, tmp_path, FAKE_FAIL="idle:tail"))
    assert made.returncode == 0, made.stderr

    # the supplied clip is adopted, a retake runs on a route without --last-frame, and the best window is kept
    env = fake_env(fake, tmp_path, FAKE_FAIL="idle:tail", FAKE_NO_LAST_FRAME=1)
    result, summary = cli("run", "--plan", plan_path, "--stagger", 0, env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert summary["generated"] == 1 and summary["actions"]["idle"]["status"] == "done"
    assert summary["finisher"].startswith("sprite_set stand-in")
    state = json.loads((tmp_path / "set" / "set_state.json").read_text(encoding="utf-8"))
    idle = state["actions"]["idle"]
    assert [t["status"] for t in idle["takes"]] == ["rejected", "rejected"]
    assert idle["takes"][0]["route"] == "supplied" and idle["takes"][1]["route"] == "fake-i2v"
    assert idle["takes"][1]["pinnedLastFrame"] is False and "last frame" in idle["takes"][1]["pinNote"]
    assert idle["chosen"]["window"] == [0, 28] and idle["chosen"]["take"] in (1, 2)
    assert "best of 2 failed takes" in idle["chosen"]["reason"]
    events = [t["status"] for t in log_lines(tmp_path / "set" / "takes.jsonl")]
    assert events == ["rejected", "rejected", "best-window"]
    finished = json.loads((tmp_path / "set" / idle["output"]["package"] / "animation.json").read_text("utf-8"))
    assert finished["bodyHeightPx"] == 40 and finished["loopPolicy"] == "cycle"
    assert idle["output"]["verify"] == "skipped"   # png-only: nothing to decode
    assert_no_absolute_paths(tmp_path / "set")


def test_provider_errors_stop_the_action_for_this_run_only(ss, fake, tmp_path):
    master = write_master(tmp_path / "art", fake, ss)
    plan_path = tmp_path / "set" / "set_plan.json"
    cli("plan", "--master", master, "--output-dir", tmp_path / "set", "--actions", "idle", "--formats", "png")
    result, summary = cli("run", "--plan", plan_path, "--stagger", 0, env=fake_env(fake, tmp_path, FAKE_ERROR=1))
    assert result.returncode == 1 and "error: the set is incomplete: idle error" in result.stderr
    assert summary["actions"]["idle"]["status"] == "error" and summary["generated"] == 0
    assert "the provider timed out" in summary["actions"]["idle"]["message"]
    lines = log_lines(tmp_path / "set" / "takes.jsonl")
    assert [(line["take"], line["status"]) for line in lines] == [(1, "error"), (2, "error")]
    assert len(log_lines(tmp_path / "fake-media.log")) == 2   # two tries per run, then the run moves on


def test_master_json_shapes_and_path_scrubbing(ss, tmp_path):
    base = tmp_path
    pick = ss._master_png_ref
    assert pick({"files": {"raw": {"path": "master_raw.png"}, "padded": {"path": "pad.png", "sha256": "ab"}}}, base) \
        == (base / "pad.png", "ab")
    assert pick({"files": [{"path": "x.json"}, {"path": "art/master.png", "role": "master"}]}, base)[0] \
        == base / "art" / "master.png"
    assert pick({"files": {"master.png": {"sha256": "cd"}}}, base) == (base / "master.png", "cd")
    with pytest.raises(ValueError, match="no PNG"):
        pick({"files": {"master": "master.json"}}, base)
    assert ss._parse_key(None) == ("magenta", (255, 0, 255))
    assert ss._parse_key("#FF00FF") == ("magenta", (255, 0, 255))
    assert ss._parse_key({"color": [0, 255, 0]}) == ("green", (0, 255, 0))
    assert ss._parse_key("#123456")[0] == "#123456"
    assert ss._parse_facing({"direction": "LEFT"}) == "left" and ss._parse_facing("viewer") == "front"
    with pytest.raises(ValueError):
        ss._parse_facing("up")
    scrubbed = ss._scrub({"a": r"C:\Users\me\clip.mp4", "b": "error: D:/x/y/frames-clean is empty",
                          "c": ["/tmp/z.png", "24/1", "rel/path.png"], "_private": 1})
    assert scrubbed == {"a": "clip.mp4", "b": "error: frames-clean is empty", "c": ["z.png", "24/1", "rel/path.png"]}
