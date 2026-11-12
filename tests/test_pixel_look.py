"""CLI tests for skills/generate2dsprite/scripts/pixel_look.py: a master sprite from the picture an image model
draws after a reference, and the poses, the views and the line-up made from that master. Synthetic pictures only
(forge_pixel_fixtures); nothing is generated."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from forge_pixel_fixtures import COLOURS, KEY, LINE, fake_picture, lattice_picture, on_canvas, toy_sprite
from forge_testutils import assert_cli_help, load_script, run_cli, script_path

TOOL = script_path("generate2dsprite", "pixel_look")
MOTION = script_path("video2dsprite", "pixel_motion")
look = load_script("generate2dsprite", "pixel_look")


def ok(*args, tool=TOOL) -> dict:
    result = run_cli([tool, *map(str, args)])
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].isascii() and result.stderr == ""
    return json.loads(lines[0])


def fails(*args, code: int = 1) -> str:
    result = run_cli([TOOL, *map(str, args)])
    assert result.returncode == code, (result.stdout, result.stderr)
    assert result.stdout == ""
    return result.stderr.strip().splitlines()[-1]


def picture(path: Path, sprite: np.ndarray, **how) -> Path:
    Image.fromarray(fake_picture(sprite, **how), "RGB").save(path)
    return path


def save(path: Path, sprite: np.ndarray) -> Path:
    Image.fromarray(sprite, "RGBA").save(path)
    return path


def rgba(path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGBA"))


def record(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_help_names_every_command():
    assert_cli_help("generate2dsprite", "pixel_look")
    usage = run_cli([TOOL, "--help"]).stdout
    for command in ("ask-master", "master", "ask-cast", "cast", "ask-pose", "pose", "ask-view", "view", "lineup"):
        assert command in usage


# --------------------------------------------------------------------------- ask-master

def test_ask_master_asks_for_the_liked_figure_again_once_and_large(tmp_path):
    how = picture(tmp_path / "liked.png", toy_sprite())
    out = tmp_path / "first"
    result = ok("ask-master", "--how", how, "--subject", "a small knight in a red scarf.", "--output-dir", out)
    text = (out / "prompt.txt").read_text(encoding="utf-8")
    assert text.isascii() and result["references"] == [str(how)] and result["key"] == "magenta"
    assert "One image is attached. It shows a small pixel-art game sprite, enlarged: a small knight in a red scarf." in text
    assert "Draw this same sprite again, exactly as it is drawn there" in text and "Draw it ONCE, large and centred" in text
    assert "one perfectly flat pure magenta (#FF00FF)" in text and "no magenta or pink on the figure" in text
    request = record(out / "request.json")
    assert request["schema"] == look.REQUEST_SCHEMA and request["kind"] == "master" and request["size"] == "1024x1024"
    pink = np.zeros((64, 64, 3), dtype=np.uint8)
    pink[...] = (40, 40, 60)
    pink[16:48, 16:48] = (250, 20, 240)                         # a figure in pink: magenta is no ground for it
    Image.fromarray(pink, "RGB").save(tmp_path / "pink.png")
    other = ok("ask-master", "--how", tmp_path / "pink.png", "--subject", "a mage in a pink robe", "--output-dir", tmp_path / "second")
    assert other["key"] != "magenta" and "magenta" not in (tmp_path / "second" / "prompt.txt").read_text(encoding="utf-8")
    green = ok("ask-master", "--how", how, "--subject", "a knight", "--key", "green", "--output-dir", tmp_path / "third")
    assert green["key"] == "green" and "pure green (#00FF00)" in (tmp_path / "third" / "prompt.txt").read_text(encoding="utf-8")


def test_far_key_is_the_key_colour_a_figure_has_least_of():
    assert look.far_key(np.asarray([(250, 20, 240), (20, 20, 30)])) != "magenta"
    assert look.far_key(np.asarray([(30, 220, 40), (20, 20, 30)])) != "green"
    assert look.far_key(np.asarray([(30, 40, 230), (20, 240, 30)])) == "magenta"
    assert look.named_key((5, 248, 7)) == "green" and look.named_key((250, 4, 251)) == "magenta"


def test_ask_master_refusals(tmp_path):
    how = picture(tmp_path / "liked.png", toy_sprite())
    assert "does not exist" in fails("ask-master", "--how", tmp_path / "none.png", "--subject", "a knight", "--output-dir", tmp_path / "a1")
    fails("ask-master", "--how", how, "--who", how, "--subject", "a knight", "--output-dir", tmp_path / "a2", code=2)
    (tmp_path / "taken").mkdir()
    assert "exists already" in fails("ask-master", "--how", how, "--subject", "a knight", "--output-dir", tmp_path / "taken")
    fails("ask-master", "--how", how, "--subject", "a knight", "--key", "orange", "--output-dir", tmp_path / "a3", code=2)
    assert not any((tmp_path / name).exists() for name in ("a1", "a2", "a3"))


# --------------------------------------------------------------------------- master

def test_master_reads_the_pictures_own_grid_and_writes_the_sprite_and_what_shows_it(tmp_path):
    sprite = toy_sprite()
    source = picture(tmp_path / "generated.png", sprite)
    out = tmp_path / "hero"
    result = ok("master", "--image", source, "--name", "hero", "--output-dir", out)
    assert result["size"] == [16, 24] and result["colours"] == 7 and result["outlineClosed"] is True
    assert sorted(p.name for p in out.iterdir()) == ["hero-grounds.png", "hero-how.png", "hero-review.png", "hero.json", "hero.png"]
    assert (rgba(out / "hero.png") == sprite).all()
    info = record(out / "hero.json")
    assert info["schema"] == look.SPRITE_SCHEMA and info["kind"] == "master" and info["key"] == "magenta"
    assert info["feet"] == {"middle": 7.5, "row": 23} and info["anchor"] == [8.0, 24]
    assert info["read"]["outline"]["line"] == list(LINE) and info["read"]["method"] == "walk"
    assert set(info["palette"]) == {"#%02x%02x%02x" % c for c in (LINE, *COLOURS)}
    how = np.asarray(Image.open(out / "hero-how.png").convert("RGB"))
    zoom = info["howZoom"]
    assert how.shape == (1024, 1024, 3) and tuple(how[0, 0]) == KEY and zoom == 24
    ys, xs = np.where((how != np.asarray(KEY)).any(axis=2))
    assert xs.min() % zoom == 0 and ys.min() % zoom == 0 and ys.max() + 1 - ys.min() == 24 * zoom
    grounds = Image.open(out / "hero-grounds.png")
    assert grounds.width > 3 * 16 and (out / "hero-review.png").is_file()
    four = ok("master", "--image", source, "--ground", "#3a5a40", "--output-dir", tmp_path / "four")
    assert Image.open(tmp_path / "four" / "master-grounds.png").width > grounds.width


def test_master_closes_the_outline_by_rule_and_brings_the_colours_down(tmp_path):
    sprite = toy_sprite()
    opened = sprite.copy()
    opened[12, 2, :3] = COLOURS[3]                              # a lit cell on the silhouette's edge
    out = tmp_path / "closed"
    ok("master", "--image", picture(tmp_path / "open.png", opened), "--output-dir", out)
    closed = rgba(out / "master.png")
    assert tuple(closed[12, 1]) == (*LINE, 255) and tuple(closed[12, 2, :3]) == COLOURS[3]      # the line goes outside it
    info = record(out / "master.json")["read"]["outline"]
    assert info["bareCells"] == 1 and info["lineCellsAdded"] == 1 and info["closed"] is True
    few = ok("master", "--image", picture(tmp_path / "few.png", sprite), "--colors", 4, "--output-dir", tmp_path / "few")
    assert few["colours"] == 4 and LINE in {tuple(c) for c in rgba(tmp_path / "few" / "master.png")[..., :3].reshape(-1, 3).tolist()}
    on_green = fake_picture(sprite).copy()
    on_green[(fake_picture(sprite, soften=False) == np.asarray(KEY)).all(axis=2)] = (0, 255, 0)
    Image.fromarray(on_green, "RGB").save(tmp_path / "green.png")
    ok("master", "--image", tmp_path / "green.png", "--output-dir", tmp_path / "green")
    assert (rgba(tmp_path / "green" / "master.png") == sprite).all()
    assert tuple(np.asarray(Image.open(tmp_path / "green" / "master-how.png").convert("RGB"))[0, 0]) == (0, 255, 0)
    assert record(tmp_path / "green" / "master.json")["key"] == "green"


def test_master_refusals(tmp_path):
    flat = tmp_path / "flat.png"
    Image.new("RGB", (256, 256), KEY).save(flat)
    assert "no figure" in fails("master", "--image", flat, "--output-dir", tmp_path / "m1")
    assert "does not exist" in fails("master", "--image", tmp_path / "none.png", "--output-dir", tmp_path / "m2")
    small = picture(tmp_path / "small.png", toy_sprite(40, 60), base=5, spread=0)
    assert "cells are only about" in fails("master", "--image", small, "--output-dir", tmp_path / "m3")
    good = picture(tmp_path / "good.png", toy_sprite())
    (tmp_path / "taken").mkdir()
    assert "exists already" in fails("master", "--image", good, "--output-dir", tmp_path / "taken")
    fails("master", "--image", good, "--line", "black", "--output-dir", tmp_path / "m4", code=2)
    assert not any((tmp_path / name).exists() for name in ("m1", "m2", "m3", "m4")) and not list(tmp_path.glob(".m*"))
    out = tmp_path / "published"
    result = run_cli([TOOL, "master", "--image", str(good), "--output-dir", str(out)])
    assert json.loads(result.stdout)["sprite"] == str(out.resolve() / "master.png") and not list(tmp_path.glob(".published*"))


# --------------------------------------------------------------------------- ask-cast, cast

def other_character(width: int = 20, height: int = 24) -> np.ndarray:
    """Another figure with colours of its own (none of them the toy sprite's) and a staff held apart from the body."""
    figure = toy_sprite(width, height)
    inside = (figure[..., 3] > 0) & (figure[..., :3] != np.asarray(LINE)).any(axis=2)
    figure[inside, :3] = 255 - figure[inside, :3]
    out = np.zeros((height, width + 4, 4), dtype=np.uint8)
    out[:, :width] = figure
    out[2:height, width + 2:width + 4] = (*LINE, 255)           # the staff: two cells beside the body, not touching it
    return out


def test_ask_cast_and_cast_draw_another_character_beside_the_master_at_its_pixel_size(tmp_path):
    sprite = toy_sprite()
    master = save(tmp_path / "hero.png", sprite)
    who = tmp_path / "design.png"
    Image.new("RGB", (64, 64), (60, 200, 190)).save(who)
    out = tmp_path / "ask-cast"
    result = ok("ask-cast", "--master", master, "--who", who, "--subject", "a mage in a teal robe", "--pose",
                "turned to the viewer's right, a staff in the far hand", "--output-dir", out)
    request = record(out / "request.json")
    assert result["size"] == "1536x1024" and request["kind"] == "cast" and request["cell"] == 14 and request["sameHeight"] is True
    at = request["masterAt"]
    assert at == [2, 73 - 24 - 4] and request["references"] == ["reference.png", str(who)]
    reference = np.asarray(Image.open(out / "reference.png").convert("RGB"))
    assert reference.shape == (1024, 1536, 3) and (reference[:, 22 * 14:] == np.asarray(KEY)).all()       # room to its right
    text = (out / "prompt.txt").read_text(encoding="utf-8")
    assert "Two images are attached." in text and "The SECOND image is a picture of another character of the same game: a mage in a teal robe." in text
    assert "THE ONLY CHANGE: in the empty area to the right, add a SECOND figure: the character of the SECOND image" in text
    assert "It is exactly as tall as the first figure" in text and "Pose: turned to the viewer's right, a staff in the far hand." in text

    other = other_character()
    cells = on_canvas(sprite, (109, 73), tuple(at))
    drawn = lattice_picture(cells, 14, (1536, 1024), shift=(3, -2)).astype(np.int32)
    beside = on_canvas(other, (109, 73), (60, at[1]))
    beside[at[1] + 3, 90:93] = (*LINE, 255)                     # a speck far from the figure
    second = lattice_picture(beside, 14, (1536, 1024), shift=(3 + 7, -2))                               # half a cell off
    drawn[:, 600:] = second[:, 600:]
    source = tmp_path / "generated.png"
    Image.fromarray(drawn.astype(np.uint8), "RGB").save(source)
    made = tmp_path / "mage"
    done = ok("cast", "--image", source, "--request", out, "--name", "mage", "--output-dir", made)
    assert done["size"] == [24, 24] and done["heightRows"] == done["masterHeightRows"] == 24 and done["warnings"] == []
    assert done["rulerEqualShare"] == 1.0 and abs(done["cellPx"] - 14.0) <= 0.1 and done["outlineClosed"] is True
    assert (rgba(made / "mage.png") == other).all()             # its own colours, the staff with it, the speck not
    info = record(made / "mage.json")
    assert info["kind"] == "master" and info["read"]["method"] == "beside the first master" and info["solesBelowMasters"] == 0.0
    assert sorted(p.name for p in made.iterdir()) == ["mage-grounds.png", "mage-how.png", "mage-review.png", "mage.json", "mage.png"]
    assert ok("lineup", "--master", master, "--item", made / "mage.png", "--output-dir", tmp_path / "line")["verdict"] == "same"

    tall = drawn.copy()
    tall[:, 600:] = lattice_picture(on_canvas(other_character(20, 30), (109, 73), (60, at[1] - 6)), 14, (1536, 1024), shift=(3, -2))[:, 600:]
    Image.fromarray(tall.astype(np.uint8), "RGB").save(tmp_path / "tall.png")
    warned = ok("cast", "--image", tmp_path / "tall.png", "--request", out, "--output-dir", tmp_path / "tall")
    assert warned["heightRows"] == 30 and "asked to be as tall" in warned["warnings"][0]
    big = tmp_path / "ask-big"
    asked = ok("ask-cast", "--master", master, "--who", who, "--subject", "a tall golem", "--size",
               "It is about one and a quarter times as tall as the first figure", "--cell", 12, "--output-dir", big)
    assert asked["cell"] == 12 and record(big / "request.json")["sameHeight"] is False
    assert "about one and a quarter times as tall as the first figure." in (big / "prompt.txt").read_text(encoding="utf-8")
    assert "does not exist" in fails("ask-cast", "--master", master, "--who", tmp_path / "none.png", "--subject", "x", "--output-dir",
                                     tmp_path / "c1")
    wide = save(tmp_path / "wide.png", toy_sprite(66, 52))      # a wide first master keeps cells of 8 px
    assert ok("ask-cast", "--master", wide, "--who", who, "--subject", "x", "--output-dir", tmp_path / "ask-wide")["cell"] == 8
    huge = save(tmp_path / "huge.png", toy_sprite(120, 90))
    assert "too large to be the ruler" in fails("ask-cast", "--master", huge, "--who", who, "--subject", "x", "--output-dir",
                                                tmp_path / "c4")
    (tmp_path / "text.png").write_text("no picture", encoding="utf-8")
    assert fails("ask-cast", "--master", tmp_path / "text.png", "--who", who, "--subject", "x", "--output-dir",
                 tmp_path / "c5").startswith("error: ")
    ok("ask-view", "--master", master, "--view", "back", "--output-dir", tmp_path / "ask-view")
    assert "not the request of ask-cast" in fails("cast", "--image", source, "--request", tmp_path / "ask-view", "--output-dir",
                                                  tmp_path / "c2")
    alone = tmp_path / "alone.png"
    Image.fromarray(lattice_picture(cells, 14, (1536, 1024)), "RGB").save(alone)
    assert "no second figure" in fails("cast", "--image", alone, "--request", out, "--output-dir", tmp_path / "c3")
    assert not any((tmp_path / name).exists() for name in ("c1", "c2", "c3", "c4", "c5"))


# --------------------------------------------------------------------------- ask-pose, pose

def asked_pose(tmp_path: Path) -> tuple[Path, dict]:
    master = save(tmp_path / "hero.png", toy_sprite())
    out = tmp_path / "ask-pose"
    result = ok("ask-pose", "--master", master, "--change", "it now holds a bar in the hand on the viewer's left", "--room",
                "8,2,2,2", "--cell", 16, "--output-dir", out)
    return out, result


def test_ask_pose_puts_the_master_on_an_exact_lattice_and_asks_for_one_change(tmp_path):
    out, result = asked_pose(tmp_path)
    request = record(out / "request.json")
    assert request["kind"] == "pose" and request["cell"] == 16 and request["canvas"] == [1024, 1024]
    assert request["masterAt"] == [27, 20] and request["master"]["size"] == [16, 24] and result["key"] == "magenta"
    reference = np.asarray(Image.open(out / "reference.png").convert("RGB"))
    assert reference.shape == (1024, 1024, 3) and tuple(reference[0, 0]) == KEY
    sprite = toy_sprite()
    for (row, col) in ((0, 7), (12, 2), (23, 13), (12, 0)):
        want = sprite[row, col, :3] if sprite[row, col, 3] else np.asarray(KEY)
        block = reference[(20 + row) * 16:(21 + row) * 16, (27 + col) * 16:(28 + col) * 16]
        assert (block == want).all()                           # every sprite pixel is one flat square of 16 px
    text = (out / "prompt.txt").read_text(encoding="utf-8")
    assert "Reproduce the attached image" in text and "Every square of 16 px in it is one pixel of the sprite." in text
    assert "THE ONLY CHANGE: it now holds a bar in the hand on the viewer's left." in text
    assert "is NOT redrawn: it stays exactly as it is, pixel for pixel" in text
    master = tmp_path / "hero.png"
    auto = ok("ask-pose", "--master", master, "--change", "it raises one arm", "--output-dir", tmp_path / "auto")
    assert auto["cell"] == 16 and record(tmp_path / "auto" / "request.json")["masterAt"][0] >= 12      # room on both sides
    assert "give less --room" in fails("ask-pose", "--master", master, "--change", "x", "--room", "80,60,80,60", "--output-dir",
                                       tmp_path / "p1")
    assert "L,T,R,B" in fails("ask-pose", "--master", master, "--change", "x", "--room", "8,2", "--output-dir", tmp_path / "p2")
    assert "does not exist" in fails("ask-pose", "--master", tmp_path / "none.png", "--change", "x", "--output-dir", tmp_path / "p3")
    assert not any((tmp_path / name).exists() for name in ("p1", "p2", "p3"))


def returned_pose(sprite: np.ndarray, noise: bool = True) -> np.ndarray:
    """What comes back for the pose request: the master on its lattice with a bar in its hand (new cells outside
    the silhouette, three cells of the arm changed) and one stray cell that differs elsewhere."""
    cells = on_canvas(sprite, (64, 64), (27, 20))
    cells[20 + 12:20 + 15, 27 - 8:27 + 2] = (*LINE, 255)       # the bar: ten cells long, three rows, outlined
    cells[20 + 13, 27 - 7:27 + 5] = (*COLOURS[4], 255)         # its inside runs on into the body: the arm
    if noise:
        cells[20 + 20, 27 + 11, :3] = COLOURS[5] if tuple(sprite[20, 11, :3]) != COLOURS[5] else COLOURS[0]
    return cells


def test_pose_takes_only_the_changed_part_and_keeps_every_other_cell_of_the_master(tmp_path):
    out, _ = asked_pose(tmp_path)
    sprite = toy_sprite()
    cells = returned_pose(sprite)
    source = tmp_path / "generated.png"
    Image.fromarray(lattice_picture(cells, 16, (1024, 1024), shift=(5, -3)), "RGB").save(source)
    made = tmp_path / "pose"
    result = ok("pose", "--image", source, "--request", out, "--name", "hero-bar", "--output-dir", made)
    assert result["size"] == [16 + 8, 24] and result["masterAt"] == [8, 0] and result["pictureEqualShare"] > 0.97
    pose = rgba(made / "hero-bar.png")
    want = returned_pose(sprite, noise=False)[20:44, 19:43]
    assert (pose == want).all()                                # the bar and the arm are taken, the stray cell is the master's
    info = record(made / "hero-bar.json")
    assert info["kind"] == "pose" and info["taken"]["newCells"] == 30 and info["taken"]["cellsTaken"] == 33
    assert info["masterCells"] - info["masterCellsKept"] == 3 and info["newColours"] == 0 and info["outline"]["closed"] is True
    assert info["feet"] == {"middle": 8 + 7.5, "row": 23} and (made / "hero-bar-review.png").is_file()
    assert ok("pose", "--image", source, "--request", out / "request.json", "--output-dir", tmp_path / "again")["size"] == [24, 24]


def test_pose_cleans_the_key_colour_left_on_the_taken_part(tmp_path):
    out, _ = asked_pose(tmp_path)
    sprite = toy_sprite()
    cells = returned_pose(sprite, noise=False)
    half = tuple(int(v) for v in (np.asarray(LINE) + np.asarray(KEY)) // 2)
    cells[20 + 12:20 + 15, 27 - 8, :3] = half                  # the bar's end: its outline, half covered by the ground
    source = tmp_path / "tinted.png"
    Image.fromarray(lattice_picture(cells, 16, (1024, 1024), shift=(-4, 2)), "RGB").save(source)
    made = tmp_path / "pose"
    ok("pose", "--image", source, "--request", out, "--name", "hero-bar", "--output-dir", made)
    assert (rgba(made / "hero-bar.png") == returned_pose(sprite, noise=False)[20:44, 19:43]).all()     # outline cells again
    assert record(made / "hero-bar.json")["edge"]["keyMixed"]["darkened"] == 3


def test_pose_refusals(tmp_path):
    out, _ = asked_pose(tmp_path)
    sprite = toy_sprite()
    same = tmp_path / "same.png"
    Image.fromarray(lattice_picture(on_canvas(sprite, (64, 64), (27, 20)), 16, (1024, 1024)), "RGB").save(same)
    assert "shows no change" in fails("pose", "--image", same, "--request", out, "--output-dir", tmp_path / "q1")
    other = sprite.copy()
    other[..., :3] = 255 - other[..., :3]
    wrong = tmp_path / "wrong.png"
    Image.fromarray(lattice_picture(on_canvas(other, (64, 64), (27, 20)), 16, (1024, 1024)), "RGB").save(wrong)
    assert "does not show the master again" in fails("pose", "--image", wrong, "--request", out, "--output-dir", tmp_path / "q2")
    assert "does not exist" in fails("pose", "--image", same, "--request", tmp_path / "none", "--output-dir", tmp_path / "q3")
    view_request = tmp_path / "ask-view"
    ok("ask-view", "--master", tmp_path / "hero.png", "--view", "side-right", "--output-dir", view_request)
    assert "not the request of ask-pose" in fails("pose", "--image", same, "--request", view_request, "--output-dir", tmp_path / "q4")
    save(out / "master.png", toy_sprite(16, 26))               # the request's own copy of the master was changed
    assert "missing or was changed" in fails("pose", "--image", same, "--request", out, "--output-dir", tmp_path / "q5")
    assert not any((tmp_path / name).exists() for name in ("q1", "q2", "q3", "q4", "q5"))


# --------------------------------------------------------------------------- ask-view, view

def test_ask_view_and_view_read_a_second_figure_on_its_own_lattice(tmp_path):
    sprite = toy_sprite()
    master = save(tmp_path / "hero.png", sprite)
    out = tmp_path / "ask-view"
    result = ok("ask-view", "--master", master, "--view", "side-right", "--detail", "the sword hangs at the near side",
                "--output-dir", out)
    request = record(out / "request.json")
    assert result["size"] == "1536x1024" and request["cell"] == 14 and request["canvas"] == [1536, 1024]
    at = request["masterAt"]
    assert at == [19, 24] and (at[0] + 16) * 14 < 768                  # the master stands on the left half
    reference = np.asarray(Image.open(out / "reference.png").convert("RGB"))
    assert reference.shape == (1024, 1536, 3) and (reference[:, 768:] == np.asarray(KEY)).all()
    text = (out / "prompt.txt").read_text(encoding="utf-8")
    assert "THE ONLY CHANGE: in the empty right half, add a SECOND figure: the SAME character turned to a true side view" in text
    assert "exactly as tall as the first figure" in text and "In this view: the sword hangs at the near side." in text
    words = ok("ask-view", "--master", master, "--view-words", "seen from above", "--output-dir", tmp_path / "words")
    assert "the SAME character seen from above." in (tmp_path / "words" / "prompt.txt").read_text(encoding="utf-8")
    assert words["cell"] == 14

    side = toy_sprite(12, 24)
    cells = on_canvas(sprite, (109, 73), tuple(at))
    picture_ = lattice_picture(cells, 14, (1536, 1024), shift=(-2, 3)).astype(np.int32)
    second = lattice_picture(on_canvas(side, (109, 73), (70, 24)), 14, (1536, 1024), shift=(-2 + 7, 3))    # half a cell off
    half = picture_.shape[1] // 2
    picture_[:, half:] = second[:, half:]
    source = tmp_path / "generated.png"
    Image.fromarray(picture_.astype(np.uint8), "RGB").save(source)
    made = tmp_path / "side"
    done = ok("view", "--image", source, "--request", out, "--name", "hero-side", "--output-dir", made)
    assert done["size"] == [12, 24] and done["heightRows"] == done["masterHeightRows"] == 24 and done["warnings"] == []
    assert done["rulerEqualShare"] == 1.0 and (rgba(made / "hero-side.png") == side).all()
    info = record(made / "hero-side.json")
    assert info["kind"] == "view" and info["view"] == "side-right" and abs(abs(info["ownLattice"]["shift"][0]) - 7) <= 1
    assert info["ownLattice"]["flat"] > info["ownLattice"]["flatUnshifted"] and info["newColours"] == 0
    assert sorted(p.name for p in made.iterdir()) == ["hero-side-grounds.png", "hero-side-how.png", "hero-side-review.png",
                                                      "hero-side.json", "hero-side.png"]

    tall = picture_.copy()
    tall[:, half:] = lattice_picture(on_canvas(toy_sprite(12, 30), (109, 73), (70, 18)), 14, (1536, 1024), shift=(-2, 3))[:, half:]
    Image.fromarray(tall.astype(np.uint8), "RGB").save(tmp_path / "tall.png")
    warned = ok("view", "--image", tmp_path / "tall.png", "--request", out, "--output-dir", tmp_path / "tall")
    assert warned["heightRows"] == 30 and "not the same height" in warned["warnings"][0]
    alone = tmp_path / "alone.png"
    Image.fromarray(lattice_picture(cells, 14, (1536, 1024)), "RGB").save(alone)
    assert "no second figure" in fails("view", "--image", alone, "--request", out, "--output-dir", tmp_path / "v1")
    assert "--view" in fails("ask-view", "--master", master, "--output-dir", tmp_path / "v2")
    assert not any((tmp_path / name).exists() for name in ("v1", "v2"))


# --------------------------------------------------------------------------- lineup

def test_lineup_puts_sprites_and_sets_on_one_ground_line_and_reports_the_heights(tmp_path):
    sprite = toy_sprite()
    master = save(tmp_path / "hero.png", sprite)
    armed = save(tmp_path / "hero-bar.png", returned_pose(sprite, noise=False)[20:44, 19:43])
    ok("breath", "--sprite", master, "--name", "hero_idle", "--no-package", "--output-dir", tmp_path / "idle", tool=MOTION)
    out = tmp_path / "lineup"
    result = ok("lineup", "--master", master, "--item", armed, "--item", tmp_path / "idle", "--output-dir", out)
    assert result["verdict"] == "same" and result["masterHeight"] == 24 and result["firstDrawingDiffers"] == []
    assert result["heights"] == {str(armed): [24], str(tmp_path / "idle"): [24, 23]}        # the breath is one cell lower
    info = record(out / "lineup.json")
    assert info["schema"] == look.LINEUP_SCHEMA and [item["kind"] for item in info["items"]] == ["sprite", "set"]
    page = Image.open(out / "lineup.png")
    assert page.width > 4 * 16 and page.height > 24
    grown = save(tmp_path / "grown.png", toy_sprite(16, 28))
    differs = ok("lineup", "--master", master, "--item", grown, "--output-dir", tmp_path / "differs")
    assert differs["verdict"] == "differs" and differs["firstDrawingDiffers"] == [str(grown)]
    assert "at least one --item" in fails("lineup", "--master", master, "--output-dir", tmp_path / "l1")
    (tmp_path / "empty").mkdir()
    assert "no motion set" in fails("lineup", "--master", master, "--item", tmp_path / "empty", "--output-dir", tmp_path / "l2")
    assert "does not exist" in fails("lineup", "--master", master, "--item", tmp_path / "none.png", "--output-dir", tmp_path / "l3")
    assert not any((tmp_path / name).exists() for name in ("l1", "l2", "l3"))
