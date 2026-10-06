"""Tests for skills/generate2dsprite/scripts/master_still.py, the one-still master step.

No real generation and no network: generate and edit run against a fake media CLI
named by FORGE_ROUTE_MEDIA_FAKE (or --media-cli) that records its arguments and
copies a synthetic still on a noisy magenta backdrop. The recipe under test is
game-opus55's: base_kanetsugu.txt (prompt), base_nobu_v2a.txt (fix by edit) and
the _pad stills (788 px subject, top margin 138 px on 1024x1024).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from forge_testutils import assert_cli_help, load_script, run_cli, script_path

SKILL = "generate2dsprite"
SCRIPT = script_path(SKILL, "master_still")
ms = load_script(SKILL, "master_still")
KEY = np.array([255, 0, 255])

FAKE_MEDIA = r'''
"""Fake route_media.py for tests: records its arguments and copies FAKE_MEDIA_IMAGE; never generates."""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("verb", choices=["image"])
parser.add_argument("--prompt-file", required=True)
parser.add_argument("--reference", action="append", default=[])
parser.add_argument("--size", required=True)
parser.add_argument("--out-dir", required=True)
parser.add_argument("--route")
args, extra = parser.parse_known_args()
out = Path(args.out_dir)
log = os.environ.get("FAKE_MEDIA_LOG")
if log:
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"prompt": Path(args.prompt_file).read_text(encoding="utf-8"),
                                 "references": args.reference, "size": args.size, "route": args.route,
                                 "extra": extra, "take": out.name}) + "\n")
mode = os.environ.get("FAKE_MEDIA_MODE", "ok")
if mode == "no-route":
    print(json.dumps({"status": "no-route", "fallback": "codeart2d"}))
    sys.exit(3)
if out.name in os.environ.get("FAKE_MEDIA_FAIL", "").split(","):
    print("error: fake provider refused the request", file=sys.stderr)
    sys.exit(1)
if out.exists():
    print("error: --out-dir already exists", file=sys.stderr)
    sys.exit(1)
out.mkdir(parents=True)
target = out / "generated.png"
if mode == "outside":
    target = Path(os.environ["FAKE_MEDIA_OUTSIDE"]) / (out.name + ".png")
    target.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(os.environ["FAKE_MEDIA_IMAGE"], target)
print("warning: fake estimate 0.00 USD", file=sys.stderr)
summary = {"status": "ok", "route": args.route or "fake-route", "artifact": str(target.resolve()),
           "provider": "fake", "model": "fake-image-1", "job": str(out.resolve())}
if mode == "leak":  # a misbehaving route: keys must never reach the console or the records
    print("debug: Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", file=sys.stderr)
    print("debug: OPENAI_API_KEY=sk-proj-0123456789abcdefghij")
    summary["note"] = "used key xai-0123456789abcdefghijkl"
print(json.dumps(summary))
'''

IDENTITY = ("a slim red fox with amber eyes, a cream chest, a dark green hooded cloak with a bronze clasp, "
            "brown leather boots and a short wooden bow")


# --------------------------------------------------------------------------- synthetic art

RAW_SIZE = 1000
RAW_BOX = (330, 100, 610, 920)  # x1 610: the detached mote ends 30 px further right


def synthetic_still(path: Path, *, size: int = RAW_SIZE, box: tuple[int, int, int, int] = RAW_BOX,
                    background: tuple[int, int, int] = (250, 4, 249), seed: int = 3, specks: bool = True) -> Path:
    """A generated-looking still: an outlined figure with a detached mote on a noisy magenta backdrop."""
    rng = np.random.default_rng(seed)
    big = Image.new("RGB", (size * 2, size * 2), background)
    draw = ImageDraw.Draw(big)
    x0, y0, x1, y1 = (value * 2 for value in box)
    head = (y1 - y0) // 4
    draw.rectangle([x0, y0 + head, x1, y1], fill=(40, 90, 170), outline=(20, 20, 30), width=6)
    draw.ellipse([x0 + (x1 - x0) // 4, y0, x1 - (x1 - x0) // 4, y0 + head + 8], fill=(230, 190, 150),
                 outline=(20, 20, 30), width=6)
    draw.ellipse([x1 + 24, y0 + 60, x1 + 60, y0 + 96], fill=(250, 220, 90))  # a mote: part of the subject
    pixels = np.asarray(big.resize((size, size), Image.Resampling.LANCZOS)).astype(np.float64)
    pixels = np.clip(np.floor(pixels + rng.normal(0.0, 3.0, pixels.shape) + 0.5), 0, 255).astype(np.uint8)
    if specks:  # single-pixel specks far from the subject: not part of it
        pixels[10, size - 12] = (120, 120, 120)
        pixels[size - 9, 15] = (110, 130, 120)
    Image.fromarray(pixels).save(path)
    return path


def solid_image(path: Path, colour: tuple[int, int, int], size: int = 64) -> Path:
    Image.new("RGB", (size, size), colour).save(path)
    return path


def load_rgb(path) -> np.ndarray:
    with Image.open(path) as image:
        assert image.mode == "RGB"
        return np.asarray(image.convert("RGB")).astype(int)


def subject_box(rgb: np.ndarray) -> tuple[int, int, int, int]:
    """Box of the pixels that are not the exact key."""
    ys, xs = np.nonzero((rgb != KEY).any(axis=-1))
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    root = tmp_path_factory.mktemp("master-assets")
    fake = root / "fake_route_media.py"
    fake.write_text(FAKE_MEDIA, encoding="utf-8")
    return SimpleNamespace(root=root, fake=fake, raw=synthetic_still(root / "raw.png"),
                           ident=solid_image(root / "identity.png", (190, 80, 40)),
                           peer=solid_image(root / "peer.png", (40, 110, 190)),
                           face=solid_image(root / "face.png", (220, 180, 140)))


def media_env(assets, log: Path | None = None, **extra) -> dict[str, str]:
    env = {"FORGE_ROUTE_MEDIA_FAKE": str(assets.fake), "FAKE_MEDIA_IMAGE": str(assets.raw),
           "FAKE_MEDIA_MODE": "ok", "FAKE_MEDIA_FAIL": ""}
    if log is not None:
        env["FAKE_MEDIA_LOG"] = str(log)
    env.update(extra)
    return env


def run_json(args, *, code: int = 0, env: dict[str, str] | None = None, cwd=None):
    result = run_cli([SCRIPT, *map(str, args)], env=env, cwd=cwd)
    assert result.returncode == code, f"exit {result.returncode}:\n{result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].isascii(), result.stdout
    return json.loads(lines[0]), result


def run_error(args, *, code: int = 1, env: dict[str, str] | None = None) -> str:
    result = run_cli([SCRIPT, *map(str, args)], env=env)
    assert result.returncode == code, f"exit {result.returncode}:\n{result.stdout}\n{result.stderr}"
    assert "Traceback" not in result.stderr
    return result.stderr


def spec_args(assets, *extra) -> list:
    return ["--name", "fox-ranger", "--subject", "a young fox ranger, full body", "--identity", IDENTITY,
            "--identity-ref", assets.ident, "--style-ref", assets.peer, *extra]


def make_spec(**fields) -> dict:
    raw = {"name": "fox-ranger", "subject": "a young fox ranger, full body", "identity": IDENTITY, **fields}
    spec, _ = ms.normalise_spec(raw, attach=False)
    return spec


def read_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def no_stage_left(folder: Path) -> bool:
    return not any(".stage-" in child.name for child in folder.iterdir())


@pytest.fixture(scope="module")
def generated(assets, tmp_path_factory):
    """One generate run with two fake takes, shared by the approve and edit tests (read-only)."""
    root = tmp_path_factory.mktemp("master-generated")
    log = root / "calls.jsonl"
    summary, result = run_json(["generate", *spec_args(assets, "--facing", "left", "--pronoun", "she"), "--takes",
                                "2", "--route", "codex-cli", "--media-arg=--execute", "--output-dir", root / "gen"],
                               env=media_env(assets, log))
    return SimpleNamespace(root=root, run=root / "gen", summary=summary, stderr=result.stderr, calls=read_calls(log))


@pytest.fixture(scope="module")
def approved(generated, tmp_path_factory):
    root = tmp_path_factory.mktemp("master-approved")
    summary, result = run_json(["approve", "--run", generated.run, "--take", "2", "--recap",
                                "a young fox ranger (slim red fox, dark green hooded cloak, short wooden bow)",
                                "--output-dir", root / "master"])
    return SimpleNamespace(dir=root / "master", summary=summary, stderr=result.stderr,
                           doc=json.loads((root / "master" / "master.json").read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- CLI conventions

def test_help_is_ascii_under_cp1252_for_every_command():
    assert_cli_help(SKILL, "master_still", encodings=("cp1252",))
    for command in ("prompt", "generate", "edit", "pad", "approve"):
        result = run_cli([SCRIPT, command, "--help"], "cp1252", timeout=120)
        assert result.returncode == 0, result.stderr
        assert result.stdout.isascii() and "--output-dir" in result.stdout


def test_usage_errors_exit_2_and_refused_input_exits_1(tmp_path, assets):
    assert run_error(["prompt", *spec_args(assets), "--facing", "sideways", "--output-dir", tmp_path / "a"],
                     code=2).count("usage:") == 1
    assert run_error(["prompt", *spec_args(assets), "--key", "#808080", "--output-dir", tmp_path / "a"], code=2)
    existing = tmp_path / "existing"
    existing.mkdir()
    stderr = run_error(["prompt", *spec_args(assets), "--output-dir", existing])
    assert stderr.startswith("error:") and "Refusing to replace" in stderr
    assert list(existing.iterdir()) == []
    for args, message in (
            (["--name", "x", "--subject", "a fox"], "--identity"),
            (["--name", "bad name!", "--subject", "a fox", "--identity", IDENTITY], "--name"),
            (["--name", "x", "--identity", IDENTITY], "--subject"),
            (["--name", "x", "--subject", "a fox", "--identity", IDENTITY, "--identity-ref", tmp_path / "nope.png"],
             "does not exist"),
            (["--name", "x", "--subject", "a fox", "--identity", IDENTITY, "--style-ref-what", "a peer"],
             "--style-ref is needed")):
        stderr = run_error(["prompt", *args, "--output-dir", tmp_path / "out"])
        assert stderr.startswith("error:") and message in stderr, stderr
    assert not (tmp_path / "out").exists() and no_stage_left(tmp_path)


# --------------------------------------------------------------------------- prompt

def test_prompt_follows_the_base_still_recipe_with_hd_by_default(tmp_path, assets):
    summary, _ = run_json(["prompt", *spec_args(assets, "--facing", "left", "--pronoun", "she", "--style-ref-what",
                                                "the approved ranger sprite, facing left", "--style-dont-copy",
                                                "her armour or spear"), "--output-dir", tmp_path / "p"])
    assert summary["status"] == "ok" and summary["finish"] == "hd" and summary["class"] == "hero"
    assert summary["references"] == [str(assets.ident.resolve()), str(assets.peer.resolve())]
    prompt = (tmp_path / "p" / "prompt.txt").read_text(encoding="utf-8")
    paragraphs = prompt.strip().split("\n\n")
    assert paragraphs[0] == "Create exactly ONE square image (1024x1024)."
    assert paragraphs[1].startswith("Style: premium HD 2D game art: a clean, high-resolution hand-painted sprite")
    assert "16-bit" not in prompt and "pixel art" not in prompt
    assert ("No text, no letters, no numbers, no logo, no watermark, no signature, no UI, no border, no frame, "
            "no grid.") in paragraphs[1]
    # reference roles: FIRST = identity (copy exactly), SECOND = approved peer sprite (style only)
    assert paragraphs[2].startswith(
        "Reference images: the FIRST attached image is an image of this exact character; copy her face, hair, body "
        "shape, costume, colours, markings and equipment exactly.")
    assert ("The SECOND attached image is an approved in-game sprite from the same game (the approved ranger sprite, "
            "facing left): match its art style, outline weight, shading, figure size and position on the canvas; do "
            "NOT copy her armour or spear.") in paragraphs[2]
    # facing stated three ways
    subject = paragraphs[3]
    assert subject.startswith("Subject: a young fox ranger, full body, side view (profile to three-quarter), "
                              "clearly FACING LEFT:")
    assert "her face, gaze and chest turn toward the LEFT edge of the image" in subject
    assert "forward is LEFT and her back is toward the RIGHT edge." in subject
    assert " " + ms.capitalised(IDENTITY) in subject and "A slim red fox with amber eyes" in subject
    # framing as canvas-edge percentages, nothing touching an edge, opaque pixels
    size = paragraphs[4]
    assert "about 14 percent below the top edge of the canvas" in size
    assert "about 14 percent above the bottom edge" in size and "about 72 percent of the canvas height" in size
    assert "centred horizontally" in size and "nothing touching or crossing the edges" in size
    assert ("is drawn with solid, fully opaque pixels: no transparency, no semi-transparent glow, no soft aura, "
            "nothing blended with the background.") in size
    assert paragraphs[-1] == ("Background: solid flat pure magenta #FF00FF everywhere outside the character, no "
                              "shadow, no ground, no glow on the background.")
    spec = json.loads((tmp_path / "p" / "spec.json").read_text(encoding="utf-8"))
    assert spec["schema"] == "generate2dsprite.master_spec.v1" and spec["facing"] == "left"
    assert spec["identity_ref"]["sha256"] == ms.forge_core.sha256_file(assets.ident)
    run = json.loads((tmp_path / "p" / "run.json").read_text(encoding="utf-8"))
    assert run["schema"] == "generate2dsprite.master_run.v1" and run["command"] == "prompt"
    assert [(ref["role"], ref["ordinal"]) for ref in run["references"]] == [("identity", "FIRST"), ("style", "SECOND")]
    assert run["takes"] == [] and run["media"] is None
    assert run["prompt"]["sha256"] == ms.forge_core.sha256_file(tmp_path / "p" / "prompt.txt")


def test_pixel_finish_uses_the_16_bit_style_line_and_no_pixel_grid_rules():
    spec = make_spec(finish="pixel", genre="a side-scrolling action game", allowed_text="the single gold crest symbol",
                     extra_bans=["no speech bubbles", "skulls."])
    prompt = ms.build_prompt(spec)
    assert ("Style: premium 16-bit pixel art (SNES era) for a side-scrolling action game, crisp visible chunky "
            "pixels, limited palette, strong dark outline, 3-tone cel shading, no smooth gradients, no blur.") in prompt
    assert "no grid, no speech bubbles, no skulls." in prompt
    assert ("The ONLY written character allowed anywhere is the single gold crest symbol; nothing else carries any "
            "character, letter or symbol.") in prompt
    lowered = prompt.lower()
    for banned in ("logical pixel", "logical-pixel", "8x8 block", "palette:", "#1a1c2c"):
        assert banned not in lowered


def test_reference_paragraph_adapts_to_the_references_given(tmp_path):
    ident = {"path": solid_image(tmp_path / "i.png", (10, 20, 30)), "what": "the dialogue portrait of this exact "
             "character", "copy": "his face, helmet and armour"}
    peer = {"path": solid_image(tmp_path / "s.png", (30, 20, 10)), "do_not_copy": ""}
    both = ms.build_prompt(make_spec(identity_ref=ident, style_ref=peer, pronoun="he"))
    assert ("Reference images: the FIRST attached image is the dialogue portrait of this exact character; copy his "
            "face, helmet and armour exactly. The SECOND attached image is an approved in-game sprite of another "
            "character from the same game: match its art style") in both
    assert "do NOT copy" not in both  # an explicit empty do_not_copy drops the clause
    only_style = ms.build_prompt(make_spec(style_ref={"path": peer["path"]}))
    assert "Reference image: the attached image is an approved in-game sprite" in only_style
    assert "FIRST" not in only_style and "do NOT copy that character's face, hair, outfit, equipment or colours." \
        in only_style
    only_identity = ms.build_prompt(make_spec(identity_ref={"path": ident["path"]}))
    assert "Reference image: the attached image is an image of this exact character; copy their face" in only_identity
    assert "Reference" not in ms.build_prompt(make_spec())


@pytest.mark.parametrize("facing, expected", [
    ("right", ["clearly FACING RIGHT:", "turn toward the RIGHT edge of the image", "forward is RIGHT and their back "
               "is toward the LEFT edge."]),
    ("front", ["front view, clearly FACING THE VIEWER:", "turn straight toward the viewer", "back is never visible"]),
    ("back", ["back view, clearly FACING AWAY from the viewer:", "the back of their head", "face is not visible"]),
])
def test_facing_is_stated_three_ways(facing, expected):
    prompt = ms.build_prompt(make_spec(facing=facing))
    for phrase in expected:
        assert phrase in prompt, phrase
    edit = ms.build_edit_prompt(make_spec(facing=facing), change="x", keep=None, roles=[], framing=None)
    words = {"right": "facing RIGHT", "front": "facing the viewer", "back": "facing away from the viewer"}
    assert words[facing] in edit


def test_class_presets_set_the_prompt_framing():
    mob = ms.build_prompt(make_spec(**{"class": "mob", "facing": "left"}))
    assert "about 35 percent below the top edge" in mob and "about 15 percent above the bottom edge" in mob
    assert "the body a little right of centre so there is extra empty room on the LEFT side" in mob
    boss = ms.build_prompt(make_spec(**{"class": "boss"}))
    assert "about 78 percent of the canvas height" in boss and "centred horizontally" in boss
    prop = ms.build_prompt(make_spec(**{"class": "prop"}))
    assert "one single object" in prop and "FACING" not in prop and "outside the object" in prop
    assert {name: (preset.subject_height_px, preset.top_margin_px) for name, preset in ms.CLASSES.items()} == {
        "hero": (788, 138), "mob": (471, 406), "boss": (728, 221), "prop": (614, 205)}


def test_spec_file_paths_are_relative_to_it_and_flags_win(tmp_path, assets):
    folder = tmp_path / "specs"
    folder.mkdir()
    solid_image(folder / "ident.png", (200, 60, 40))
    (folder / "hero.json").write_text(json.dumps({
        "schema": "generate2dsprite.master_spec.v1", "name": "hero", "subject": "a knight", "identity": IDENTITY,
        "identity_ref": {"path": "ident.png", "what": "the portrait"}, "facing": "left", "class": "hero",
        "extra_bans": ["capes"]}), encoding="utf-8-sig")
    summary, _ = run_json(["prompt", "--spec", folder / "hero.json", "--facing", "right", "--class", "boss", "--ban",
                           "shields", "--output-dir", tmp_path / "p"])
    assert summary["facing"] == "right" and summary["class"] == "boss"
    assert summary["references"] == [str((folder / "ident.png").resolve())]
    spec = json.loads((tmp_path / "p" / "spec.json").read_text(encoding="utf-8"))
    assert spec["identity_ref"]["path"] == "../specs/ident.png" and spec["extra_bans"] == ["capes", "shields"]
    assert "no capes, no shields." in (tmp_path / "p" / "prompt.txt").read_text(encoding="utf-8")
    (folder / "bad.json").write_text(json.dumps({"name": "x", "colour": "red"}), encoding="utf-8")
    assert "unknown spec field(s) colour" in run_error(["prompt", "--spec", folder / "bad.json", "--output-dir",
                                                         tmp_path / "q"])


# --------------------------------------------------------------------------- generate

def test_generate_calls_the_media_cli_once_per_take(generated, assets):
    summary = generated.summary
    assert summary["status"] == "ok" and summary["media_cli_source"] == "FORGE_ROUTE_MEDIA_FAKE"
    assert [take["status"] for take in summary["takes"]] == ["ok", "ok"] and summary["route"] == "codex-cli"
    assert len(generated.calls) == 2
    prompt = (generated.run / "prompt.txt").read_text(encoding="utf-8")
    for index, call in enumerate(generated.calls, start=1):
        assert call["take"] == f"take-{index:02d}" and call["size"] == "1024x1024" and call["route"] == "codex-cli"
        assert call["references"] == [str(assets.ident.resolve()), str(assets.peer.resolve())]  # identity first
        assert call["extra"] == ["--execute"] and call["prompt"] == prompt
    run = json.loads((generated.run / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "ok" and run["media"]["cli_source"] == "FORGE_ROUTE_MEDIA_FAKE"
    assert run["media"]["args"] == ["--execute"] and run["media"]["route_requested"] == "codex-cli"
    for take in run["takes"]:
        artifact = generated.run / take["artifact"]["path"]
        assert take["artifact"]["path"].startswith("takes/take-") and artifact.is_file()
        assert take["artifact"]["sha256"] == ms.forge_core.sha256_file(artifact) and take["copied"] is False
        assert "job" not in take["media"] and take["media"]["model"] == "fake-image-1"  # local paths are dropped
    assert (generated.run / "takes.png").is_file() and run["contact"]["path"] == "takes.png"
    assert "[take 1] warning: fake estimate 0.00 USD" in generated.stderr
    assert "warning: FORGE_ROUTE_MEDIA_FAKE is set" in generated.stderr


def test_generate_no_route_exits_3_and_keeps_the_prompt(tmp_path, assets):
    log = tmp_path / "calls.jsonl"
    summary, _ = run_json(["generate", *spec_args(assets), "--takes", "3", "--output-dir", tmp_path / "gen"], code=3,
                          env=media_env(assets, log, FAKE_MEDIA_MODE="no-route"))
    assert summary["status"] == "no-route" and summary["fallback"] == "codeart2d" and summary["route"] is None
    assert len(read_calls(log)) == 1  # no route for take 1 means none for the others
    run = json.loads((tmp_path / "gen" / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "no-route" and run["fallback"] == "codeart2d"
    assert (tmp_path / "gen" / "prompt.txt").is_file() and run["takes"][0]["exit_code"] == 3


def test_generate_reports_partial_and_total_failure(tmp_path, assets):
    summary, _ = run_json(["generate", *spec_args(assets), "--takes", "2", "--output-dir", tmp_path / "partial"],
                          env=media_env(assets, FAKE_MEDIA_FAIL="take-02"))
    assert summary["status"] == "partial" and [t["status"] for t in summary["takes"]] == ["ok", "fail"]
    assert summary["takes"][1]["error"] == "fake provider refused the request"
    result = run_cli([SCRIPT, "generate", *map(str, spec_args(assets)), "--output-dir", str(tmp_path / "fail")],
                     env=media_env(assets, FAKE_MEDIA_FAIL="take-01"))
    assert result.returncode == 1 and json.loads(result.stdout)["status"] == "fail"
    assert "error: every take failed (take 1: fake provider refused the request)" in result.stderr
    assert json.loads((tmp_path / "fail" / "run.json").read_text(encoding="utf-8"))["status"] == "fail"


def test_an_artifact_outside_the_take_folder_is_copied_into_it(tmp_path, assets):
    summary, _ = run_json(["generate", *spec_args(assets), "--output-dir", tmp_path / "gen"],
                          env=media_env(assets, FAKE_MEDIA_MODE="outside", FAKE_MEDIA_OUTSIDE=str(tmp_path / "far")))
    run = json.loads((tmp_path / "gen" / "run.json").read_text(encoding="utf-8"))
    assert run["takes"][0]["copied"] is True and run["takes"][0]["artifact"]["path"] == "takes/take-01/generated.png"
    assert Path(summary["takes"][0]["artifact"]).is_file()


def test_key_shaped_text_from_the_route_is_redacted(tmp_path, assets):
    summary, result = run_json(["generate", *spec_args(assets), "--output-dir", tmp_path / "gen"],
                               env=media_env(assets, FAKE_MEDIA_MODE="leak"))
    assert summary["status"] == "ok"
    record = (tmp_path / "gen" / "run.json").read_text(encoding="utf-8")
    for secret in ("abcdefghijklmnopqrstuvwxyz0123", "sk-proj-0123456789abcdefghij", "xai-0123456789abcdefghijkl"):
        assert secret not in result.stderr and secret not in result.stdout and secret not in record
    assert "Authorization: [redacted]" in result.stderr
    assert "[redacted]" in json.loads(record)["takes"][0]["media"]["note"]


def test_media_cli_flag_and_missing_cli(tmp_path, assets, monkeypatch):
    sibling = Path(ms.__file__).resolve().parents[2] / "generate2dmedia" / "scripts" / "route_media.py"
    assert ms.DEFAULT_MEDIA_CLI == sibling  # located relative to this skill folder
    env = {"FORGE_ROUTE_MEDIA_FAKE": "", "FAKE_MEDIA_IMAGE": str(assets.raw)}
    summary, _ = run_json(["generate", *spec_args(assets), "--media-cli", assets.fake, "--output-dir", tmp_path / "g"],
                          env=env)
    assert summary["status"] == "ok" and summary["media_cli_source"] == "--media-cli"
    stderr = run_error(["generate", *spec_args(assets), "--media-cli", tmp_path / "missing.py", "--output-dir",
                        tmp_path / "h"], env=env)
    assert "--media-cli names a missing file" in stderr and not (tmp_path / "h").exists()
    monkeypatch.delenv("FORGE_ROUTE_MEDIA_FAKE", raising=False)
    monkeypatch.setattr(ms, "DEFAULT_MEDIA_CLI", tmp_path / "skills" / "generate2dmedia" / "scripts" / "route_media.py")
    with pytest.raises(ValueError, match="route_media.py"):
        ms.locate_media_cli(None)


def test_media_summary_parsing_and_sanitising():
    stdout = 'progress 10%\n{"status": "ok", "route": "openai"}\nnot json {\n{"status": "ok", "artifact": "C:/x.png"}\n'
    assert ms.parse_summary(stdout) == {"status": "ok", "artifact": "C:/x.png"}
    assert ms.parse_summary("nothing here") is None
    clean = ms.sanitise_summary({"status": "ok", "artifact": "/tmp/a.png", "route": "openai", "usd": 0.04,
                                 "note": "D:\\project\\x", "nan": float("nan"), "nested": {"a": 1}, "ok": True})
    assert clean == {"status": "ok", "route": "openai", "usd": 0.04, "ok": True}


# --------------------------------------------------------------------------- pad

def test_pad_normalises_a_noisy_still_to_the_hero_framing(tmp_path, assets):
    summary, _ = run_json(["pad", "--input", assets.raw, "--class", "hero", "--output-dir", tmp_path / "pad"])
    padded = load_rgb(tmp_path / "pad" / "padded.png")
    assert padded.shape == (1024, 1024, 3)
    x0, y0, x1, y1 = subject_box(padded)
    assert abs((y1 - y0) - 788) <= 2 and abs(y0 - 138) <= 2 and abs((x0 + x1) / 2 - 512) <= 2
    with Image.open(tmp_path / "pad" / "padded_rgba.png") as image:
        cutout = np.asarray(image.convert("RGBA"))
    # the backdrop is flattened to exactly the key everywhere outside the subject
    assert (padded[cutout[..., 3] == 0] == KEY).all()
    assert (padded[:, :8] == KEY).all() and (padded[:8] == KEY).all() and (padded[-8:] == KEY).all()
    doc = json.loads((tmp_path / "pad" / "pad.json").read_text(encoding="utf-8"))
    assert doc["schema"] == "generate2dsprite.master_pad.v1" and doc["warnings"] == []
    transform = doc["transform"]
    assert transform["crop_margin_px"] == 6 and transform["resampler"] == "lanczos" and transform["ignored_parts"] >= 2
    sx0, sy0, sx1, sy1 = transform["source_subject_bbox"]
    scale, (ox, oy) = transform["scale"], transform["offset"]
    assert abs(scale * (sy1 - sy0) - 788) < 1e-6 and abs(scale * sy0 + oy - 138) < 1e-6
    assert abs(scale * sx0 + ox - doc["framing"]["subject_bbox"][0]) <= 2
    assert sx1 >= RAW_BOX[2] + 25  # the detached mote is part of the subject box
    assert doc["matte"]["key_estimate"]["valid"] and doc["matte"]["qa"]["opaque_key_px"] == 0
    assert doc["framing"]["opaque_area_px"] > 0 and summary["subject_bbox"] == doc["framing"]["subject_bbox"]


@pytest.mark.parametrize("facing, centre", [("left", 612), ("right", 412), ("none", 512)])
def test_pad_mob_framing_leaves_lead_room_on_the_facing_side(assets, facing, centre):
    with Image.open(assets.raw) as image:
        still = np.asarray(image.convert("RGBA"))
    result = ms.pad_still(still, key_hex="#FF00FF", framing=ms.framing_for("mob"), facing=facing)
    x0, y0, x1, y1 = result["framing"]["subject_bbox"]
    assert abs((y1 - y0) - 471) <= 2 and abs(y0 - 406) <= 2 and abs((x0 + x1) / 2 - centre) <= 2
    assert result["framing"]["preset"] == "mob" and result["framing"]["lead_px"] == 100


def test_pad_warnings_for_edges_upscaling_width_and_custom_framing(tmp_path):
    edge = np.asarray(Image.open(synthetic_still(tmp_path / "edge.png", box=(0, 120, 260, 800))).convert("RGBA"))
    result = ms.pad_still(edge, key_hex="#FF00FF", framing=ms.framing_for("hero"), facing="none")
    assert result["transform"]["source_touches"] == ["left"] and "touches the left edge" in result["warnings"][0]
    small = np.asarray(Image.open(synthetic_still(tmp_path / "small.png", size=300, box=(120, 80, 170, 200),
                                                  specks=False)).convert("RGBA"))
    result = ms.pad_still(small, key_hex="#FF00FF", framing=ms.framing_for("hero"), facing="none")
    assert result["transform"]["scale"] > 1 and any("upscaled" in warning for warning in result["warnings"])
    wide = np.asarray(Image.open(synthetic_still(tmp_path / "wide.png", box=(40, 400, 820, 520),
                                                 specks=False)).convert("RGBA"))
    result = ms.pad_still(wide, key_hex="#FF00FF", framing=ms.framing_for("hero"), facing="none")
    x0, _, x1, _ = result["framing"]["subject_bbox"]
    assert result["framing"]["width_limited"] and x0 >= 14 and x1 <= 1010
    assert any("too wide" in warning for warning in result["warnings"])
    args = SimpleNamespace(subject_height=600, top_margin=200, lead=None)
    framing = ms.framing_for("hero", args)
    assert (framing.subject_height_px, framing.top_margin_px, framing.preset) == (600, 200, "custom")
    with pytest.raises(ValueError, match="--top-margin"):
        ms.framing_for("hero", SimpleNamespace(subject_height=900, top_margin=200, lead=None))


def test_pad_keeps_native_alpha_and_refuses_a_still_without_a_key(tmp_path):
    rgba = np.zeros((400, 400, 4), np.uint8)
    rgba[100:300, 150:250] = (60, 120, 200, 255)
    result = ms.pad_still(rgba, key_hex="#FF00FF", framing=ms.framing_for("hero"), facing="none")
    assert result["matte"]["quality"] == "native_alpha" and abs(result["framing"]["subject_bbox"][1] - 138) <= 2
    white = Image.new("RGB", (300, 300), (255, 255, 255))
    ImageDraw.Draw(white).rectangle([100, 60, 200, 240], fill=(30, 40, 50))
    white.save(tmp_path / "white.png")
    stderr = run_error(["pad", "--input", tmp_path / "white.png", "--output-dir", tmp_path / "pad"])
    assert "no flat magenta #FF00FF backdrop" in stderr and not (tmp_path / "pad").exists()
    assert no_stage_left(tmp_path)


def test_pad_flags_a_subject_whose_colours_lean_to_the_key(tmp_path):
    purple = synthetic_still(tmp_path / "purple.png")
    pixels = np.asarray(Image.open(purple).convert("RGB")).copy()
    pixels[300:500, 330:520] = (150, 40, 190)  # a violet panel on the body
    Image.fromarray(pixels).save(purple)
    still = np.asarray(Image.open(purple).convert("RGBA"))
    result = ms.pad_still(still, key_hex="#FF00FF", framing=ms.framing_for("hero"), facing="none")
    assert result["key_choice"]["status"] == "conflict"
    assert any("lean to the key #FF00FF" in warning for warning in result["warnings"])


# --------------------------------------------------------------------------- approve

def test_approve_writes_master_v1_from_a_generate_take(approved, generated, assets):
    doc, folder = approved.doc, approved.dir
    assert ms.validate_master(doc) == []
    assert doc["schema"] == "generate2dsprite.master.v1" and doc["name"] == "fox-ranger"
    assert doc["identity_recap"] == "young fox ranger (slim red fox, dark green hooded cloak, short wooden bow)"
    assert (doc["facing"], doc["finish"], doc["class"], doc["key"]) == ("left", "hd", "hero", "#FF00FF")
    framing = doc["framing"]
    assert framing["canvas"] == [1024, 1024] and framing["subject_height_px"] == 788 and framing["top_margin_px"] == 138
    for name in ("master", "cutout", "source", "spec", "prompt"):
        ref = doc["files"][name]
        assert ms.forge_core.sha256_file(folder / ref["path"]) == ref["sha256"], name
    assert doc["files"]["source"]["sha256"] == ms.forge_core.sha256_file(generated.run / "takes/take-02/generated.png")
    assert doc["files"]["master"]["size"] == [1024, 1024] and doc["files"]["source"]["size"] == [RAW_SIZE] * 2
    assert [(ref["role"], ref["sha256"]) for ref in doc["references"]] == [
        ("identity", ms.forge_core.sha256_file(assets.ident)), ("style", ms.forge_core.sha256_file(assets.peer))]
    assert doc["route"] == "codex-cli"
    assert doc["transform"]["scale"] > 0 and len(doc["transform"]["offset"]) == 2
    provenance = doc["provenance"]
    assert provenance["command"] == "generate" and provenance["take"] == 2 and provenance["run"].endswith("gen")
    assert provenance["media_cli_source"] == "FORGE_ROUTE_MEDIA_FAKE" and provenance["edit"] is None
    assert any("fake media CLI" in warning for warning in doc["warnings"])
    master = load_rgb(folder / "master.png")
    x0, y0, x1, y1 = subject_box(master)
    assert abs((y1 - y0) - 788) <= 2 and abs(y0 - 138) <= 2
    assert (master[:8] == KEY).all() and (master[-8:] == KEY).all()
    spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    assert spec["recap"] == doc["identity_recap"] and spec["facing"] == "left"
    assert approved.summary["framing"]["subject_height_px"] == 788 and approved.summary["route"] == "codex-cli"


def test_approve_an_external_still_with_a_spec(tmp_path, assets):
    summary, _ = run_json(["prompt", *spec_args(assets, "--class", "mob", "--facing", "right"), "--output-dir",
                           tmp_path / "p"])
    summary, _ = run_json(["approve", "--still", assets.raw, "--spec", Path(summary["spec"]), "--route",
                           "host-image_gen", "--prompt-file", Path(summary["prompt"]), "--output-dir",
                           tmp_path / "m"])
    doc = json.loads((tmp_path / "m" / "master.json").read_text(encoding="utf-8"))
    assert ms.validate_master(doc) == [] and doc["route"] == "host-image_gen" and doc["class"] == "mob"
    assert doc["provenance"]["command"] == "external" and doc["provenance"]["run"] is None
    assert doc["identity_recap"] == f"young fox ranger, full body ({IDENTITY})"
    x0, y0, x1, y1 = doc["framing"]["subject_bbox"]
    assert abs((y1 - y0) - 471) <= 2 and abs((x0 + x1) / 2 - 412) <= 2  # a right-facing mob sits left of centre
    assert doc["files"]["prompt"]["path"] == "prompt.txt"


def test_reapproving_a_master_is_stable(approved, tmp_path):
    summary, _ = run_json(["approve", "--still", approved.dir / "master.png", "--spec", approved.dir / "spec.json",
                           "--route", "codex-cli", "--output-dir", tmp_path / "again"])
    doc = json.loads((tmp_path / "again" / "master.json").read_text(encoding="utf-8"))
    assert abs(doc["transform"]["scale"] - 1.0) < 0.01
    assert all(abs(a - b) <= 1 for a, b in zip(doc["framing"]["subject_bbox"], approved.doc["framing"]["subject_bbox"]))
    first, second = load_rgb(approved.dir / "master.png"), load_rgb(tmp_path / "again" / "master.png")
    assert np.abs(first - second).mean() < 1.5
    assert doc["identity_recap"] == approved.doc["identity_recap"]


def test_approve_refuses_contradictory_choices_and_publishes_nothing(tmp_path, generated, assets):
    out = tmp_path / "m"
    for args, message in (
            (["--take", "1"], "--take needs --run"),
            (["--run", generated.run, "--take", "1", "--route", "openai"], "drop --route"),
            (["--run", generated.run, "--spec", generated.run / "spec.json", "--take", "1"], "not both"),
            (["--run", generated.run, "--take", "9"], "no take 9"),
            (["--run", generated.run], "pass the still"),
            (["--run", generated.run, "--take", "1", "--still", assets.raw], "not both")):
        stderr = run_error(["approve", *args, "--output-dir", out])
        assert stderr.startswith("error:") and message in stderr, (args, stderr)
    assert not out.exists() and no_stage_left(tmp_path)


def test_validate_master_names_missing_fields(approved):
    broken = json.loads(json.dumps(approved.doc))
    del broken["identity_recap"]
    broken["framing"]["canvas"] = 1024
    broken["key"] = "#ff00ff"
    broken["files"]["master"]["path"] = "C:/abs/master.png"
    errors = ms.validate_master(broken)
    assert any("identity_recap" in error for error in errors) and any("framing.canvas" in error for error in errors)
    assert any(error.startswith("key") for error in errors) and any("files.master" in error for error in errors)


# --------------------------------------------------------------------------- edit

def test_edit_reproduces_the_first_image_with_one_change(approved, assets, tmp_path):
    log = tmp_path / "calls.jsonl"
    summary, _ = run_json(["edit", "--master", approved.dir, "--change", "repaint her cloak dark blue",
                           "--extra-reference", assets.face, "--extra-role", "her portrait: it shows her real face",
                           "--route", "grok-cli", "--output-dir", tmp_path / "edit"], env=media_env(assets, log))
    assert summary["status"] == "ok" and summary["takes"][0]["route"] == "grok-cli"
    master_png = (approved.dir / "master.png").resolve()
    call, = read_calls(log)
    assert call["references"] == [str(master_png), str(assets.face.resolve())]  # the approved still is FIRST
    prompt = call["prompt"]
    paragraphs = prompt.strip().split("\n\n")
    assert paragraphs[0] == "Create exactly ONE square image (1024x1024)."
    assert paragraphs[1] == ("The FIRST attached image is the approved master still on a flat magenta #FF00FF "
                             "background: a young fox ranger, full body. The SECOND attached image is her portrait: "
                             "it shows her real face.")
    assert paragraphs[2].startswith("Reproduce the FIRST image exactly: the same single figure at exactly the same "
                                    "size and the same position on the canvas (the top of the figure about 13 percent "
                                    "below the top edge, its lowest point about 10 percent above the bottom edge")
    assert "facing LEFT, the same young fox ranger (slim red fox, dark green hooded cloak, short wooden bow)." \
        in paragraphs[2]
    assert "Same premium HD 2D game-art style as the FIRST image:" in paragraphs[2]
    assert paragraphs[3] == ("THE ONLY CHANGE: repaint her cloak dark blue. Everything else (costume, colours, "
                             "silhouette, proportions, pose, held items and framing) stays exactly as in the FIRST "
                             "image.")
    assert paragraphs[4].startswith("Background: solid flat pure magenta #FF00FF") and "no watermark" in paragraphs[4]
    run = json.loads((tmp_path / "edit" / "run.json").read_text(encoding="utf-8"))
    assert run["command"] == "edit" and run["edit"]["change"] == "repaint her cloak dark blue"
    assert run["edit"]["still"]["sha256"] == approved.doc["files"]["master"]["sha256"]
    assert [ref["role"] for ref in run["references"]] == ["edit", "extra"]
    summary, _ = run_json(["approve", "--run", tmp_path / "edit", "--take", "1", "--output-dir", tmp_path / "m2"])
    doc = json.loads((tmp_path / "m2" / "master.json").read_text(encoding="utf-8"))
    assert doc["route"] == "grok-cli" and doc["provenance"]["command"] == "edit"
    assert doc["provenance"]["edit"]["change"] == "repaint her cloak dark blue"
    assert doc["provenance"]["edit"]["from"]["sha256"] == approved.doc["files"]["master"]["sha256"]
    assert doc["identity_recap"] == approved.doc["identity_recap"]


def test_edit_prompt_only_writes_no_takes(approved, assets, tmp_path):
    log = tmp_path / "calls.jsonl"
    summary, _ = run_json(["edit", "--master", approved.dir / "master.json", "--change-file",
                           write(tmp_path / "change.txt", "add a small silver feather to the hood\n"), "--keep",
                           "everything else", "--prompt-only", "--output-dir", tmp_path / "e"],
                          env=media_env(assets, log))
    assert summary["status"] == "ok" and "takes" not in summary and read_calls(log) == []
    prompt = (tmp_path / "e" / "prompt.txt").read_text(encoding="utf-8")
    assert "THE ONLY CHANGE: add a small silver feather to the hood. Everything else (everything else) stays" in prompt
    run = json.loads((tmp_path / "e" / "run.json").read_text(encoding="utf-8"))
    assert run["takes"] == [] and run["media"] is None and run["edit"]["keep"] == "everything else"
    env = media_env(assets, log)  # never the real route, even if validation changes
    assert "pass the image to edit" in run_error(["edit", "--change", "x", "--name", "a", "--subject", "b",
                                                  "--identity", "c", "--output-dir", tmp_path / "f"], env=env)
    assert "--change" in run_error(["edit", "--master", approved.dir, "--output-dir", tmp_path / "g"], env=env)
    assert read_calls(log) == []


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- helpers

def test_text_helpers_and_keys():
    assert ms.identity_recap({"recap": "A pixel-art spearman (dark hat).", "subject": "x", "identity": "y"}) == \
        "pixel-art spearman (dark hat)"
    assert ms.identity_recap({"recap": None, "subject": "the ghost knight", "identity": "pale armour."}) == \
        "ghost knight (pale armour)"
    assert [ms.normalise_key(text) for text in ("magenta", "#ff00ff", "00ff00", "Blue", "#12AB34")] == [
        "#FF00FF", "#FF00FF", "#00FF00", "#0000FF", "#12AB34"]
    for bad in ("#FFFFFF", "#000000", "pink", "#12345"):
        with pytest.raises(ValueError):
            ms.normalise_key(bad)
