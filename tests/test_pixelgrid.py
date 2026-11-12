"""shared/forge_pixelgrid.py: a generated "pixel art" picture -> a 1x sprite, video frames -> the sprite's grid.

Synthetic pictures only (forge_pixel_fixtures): squares that are not all the same size with soft edges on a key
colour, as an image model paints them, and whole-cell poses on a known lattice, as a clip gives them.
"""
from __future__ import annotations

import numpy as np
import pytest

from forge_pixel_fixtures import (COLOURS, HEAD_ROWS, KEY, LINE, fake_picture, lattice_picture, on_canvas, toy_sprite, video_frame,
                                  with_arm, with_blade)
from forge_testutils import load_shared

pg = load_shared("forge_pixelgrid")


def same(a: np.ndarray, b: np.ndarray) -> bool:
    return a.shape == b.shape and bool((a == b).all())


# --------------------------------------------------------------------------- the grid of a still picture

@pytest.mark.parametrize("size,base,spread,method", [((16, 24), 22, 2, "walk"), ((16, 24), 12, 1, "walk"),
                                                      ((16, 24), 9, 1, "walk"), ((40, 60), 6, 0, "lattice"),
                                                      ((48, 72), 5, 0, "lattice")])
def test_snap_picture_gives_the_sprite_back(size, base, spread, method):
    sprite = toy_sprite(*size)
    snap = pg.snap_picture(fake_picture(sprite, base, spread), colors=16)
    assert snap["method"] == method
    assert same(snap["sprite"], sprite)
    assert [int(v) for v in snap["key"]] == list(KEY)
    assert {tuple(c) for c in snap["palette"].tolist()} == {LINE, *COLOURS}


def test_snap_picture_merges_down_to_the_asked_colours():
    sprite = toy_sprite()
    snap = pg.snap_picture(fake_picture(sprite), colors=4)
    assert snap["sprite"].shape == sprite.shape and len(snap["palette"]) == 4
    assert ((snap["sprite"][..., 3] > 0) == (sprite[..., 3] > 0)).all()


def test_snap_picture_refuses_a_flat_picture_and_a_bad_method():
    flat = np.empty((200, 200, 3), dtype=np.uint8)
    flat[...] = KEY
    with pytest.raises(ValueError, match="no figure"):
        pg.snap_picture(flat)
    with pytest.raises(ValueError, match="method"):
        pg.snap_picture(fake_picture(toy_sprite()), method="guess")


# --------------------------------------------------------------------------- colours

def test_merge_palette_merges_the_closest_pair_first_and_keeps_a_rare_colour():
    colours = np.array([[200, 40, 40]] * 50 + [[202, 42, 41]] * 30 + [[20, 20, 20]] * 40 + [[250, 250, 250]] * 1, dtype=np.uint8)
    palette, index = pg.merge_palette(colours, 3)
    assert len(palette) == 3 and len(index) == len(colours)
    assert index[0] == index[50] and len({int(index[0]), int(index[80]), int(index[120])}) == 3
    assert palette[int(index[120])].tolist() == [250, 250, 250]           # one cell of white keeps its own place
    assert pg.to_oklab(palette)[:, 0].tolist() == sorted(pg.to_oklab(palette)[:, 0].tolist())  # dark to light


def test_oklab_round_trip_and_lock_palette():
    colours = np.array([LINE, *COLOURS], dtype=np.float64)
    assert np.abs(pg.from_oklab(pg.to_oklab(colours)) - colours).max() < 1e-3
    sprite = toy_sprite()
    tinted = sprite.copy()
    opaque = sprite[..., 3] > 0
    tinted[opaque, :3] = np.clip(tinted[opaque, :3].astype(np.int16) + 6, 0, 255).astype(np.uint8)
    locked, moved = pg.lock_palette(tinted, pg.palette_of(sprite))
    assert same(locked, sprite) and moved["cells"] == int((sprite[..., 3] > 0).sum())
    assert not (tinted == sprite).all()                                    # the input is left alone


# --------------------------------------------------------------------------- small fixes

def test_outline_colour_and_close_outline():
    sprite = toy_sprite()
    assert pg.outline_colour(sprite) == LINE
    open_edge = sprite.copy()
    open_edge[0, 7, :3] = COLOURS[0]
    open_edge[12, 2, :3] = COLOURS[3]
    assert pg.close_outline(open_edge, LINE) == 2 and same(open_edge, sprite)
    assert pg.close_outline(open_edge, LINE) == 0


def test_drop_key_mixed_takes_blended_cells_off_the_edge():
    sprite = toy_sprite()
    dirty = np.pad(sprite, ((0, 0), (1, 1), (0, 0)))
    blend = (*[(a + b) // 2 for a, b in zip(LINE, KEY)], 255)        # half line colour, half key
    dirty[3, 0] = blend       # beside a line cell that is itself on the edge: the blend goes
    dirty[14, 2] = blend      # beside a line cell with colour all around it: it takes the line colour, the outline stays whole
    assert pg.drop_key_mixed(dirty, KEY) == {"removed": 1, "darkened": 1}
    assert not dirty[3, 0, 3] and tuple(dirty[14, 2]) == (*LINE, 255)
    grown = sprite.copy()
    grown[14, 1] = (*LINE, 255)
    assert same(dirty[:, 1:-1], grown)


def test_drop_key_tinted_takes_a_rare_tinted_edge_colour_out_of_the_sprite():
    sprite = toy_sprite()
    tinted = sprite.copy()
    stain = tuple(round(0.75 * a + 0.25 * b) for a, b in zip(LINE, KEY))     # the outline's colour with a quarter of key
    tinted[12, 2, :3] = stain
    tinted[20, 13, :3] = stain
    assert pg.drop_key_tinted(tinted, KEY) == 2 and same(tinted, sprite)
    assert pg.drop_key_tinted(tinted, KEY) == 0
    own = sprite.copy()
    own[12, 2, :3] = (40, 200, 60)                    # a rare edge colour that is no tint of the key: it stays
    kept = own.copy()
    assert pg.drop_key_tinted(own, KEY) == 0 and same(own, kept)
    inside = sprite.copy()
    inside[12, 6, :3] = stain                         # the same colour inside the figure is a colour of the design
    inside[12, 2, :3] = stain
    kept = inside.copy()
    assert pg.drop_key_tinted(inside, KEY) == 0 and same(inside, kept)


def test_islands_and_single_holes():
    sprite = toy_sprite()
    canvas = on_canvas(sprite, (26, 28), (5, 2))
    noisy = canvas.copy()
    noisy[0, 0] = noisy[1, 1] = (*COLOURS[1], 255)       # an island of two cells
    assert pg.remove_islands(noisy, 8) == 2 and same(noisy, canvas)
    holed = canvas.copy()
    holed[12, 12] = 0
    assert pg.fill_single_holes(holed) == 1 and holed[12, 12, 3] == 255
    assert tuple(holed[12, 12, :3]) in {tuple(canvas[y, x, :3]) for y, x in ((11, 12), (13, 12), (12, 11), (12, 13))}


def test_enlarge_crop_and_feet_point():
    sprite = toy_sprite()
    picture = pg.enlarge(sprite, 4, (90, 90, 102), margin=2)
    assert picture.shape == ((24 + 4) * 4, (16 + 4) * 4, 3)
    assert tuple(picture[0, 0]) == (90, 90, 102) and tuple(picture[8 + 2, 8 + 7 * 4 + 1]) == tuple(sprite[0, 7, :3])
    still = pg.enlarge(sprite, 8, KEY, size=256)
    assert still.shape == (256, 256, 3) and tuple(still[0, 0]) == KEY
    ys, xs = np.where((still != np.asarray(KEY)).any(axis=2))
    assert xs.min() % 8 == 0 and ys.min() % 8 == 0           # on one exact grid of the picture
    with pytest.raises(ValueError, match="does not fit"):
        pg.enlarge(sprite, 16, KEY, size=256)
    assert same(pg.crop_to_figure(on_canvas(sprite, (30, 30), (4, 3))), sprite)
    assert pg.feet_point(sprite) == (8, 23)


# --------------------------------------------------------------------------- video frames on a known lattice

SCALE, CORNER, SIZE, ROOM = 8, (203, 47), (640, 360), (6, 4)      # the room's corner on the frame; cells of room


def poses() -> list[np.ndarray]:
    sprite = toy_sprite()
    canvas = (16 + 2 * ROOM[0], 24 + 2 * ROOM[1])
    rest, up = on_canvas(sprite, canvas, ROOM), on_canvas(sprite, canvas, (ROOM[0], ROOM[1] - 1))
    return [rest, up, with_arm(rest, (ROOM[0] + 12, ROOM[1] + 11)), with_arm(up, (ROOM[0] + 12, ROOM[1] + 10))]


def test_snap_frame_reads_a_frame_on_the_lattice_and_locks_its_colours():
    sprite = toy_sprite()
    palette = pg.palette_of(sprite)
    for pose in poses():
        frame = video_frame(pose, SCALE, CORNER, SIZE)[..., :3].astype(np.int16)
        frame += np.random.default_rng(3).integers(-9, 10, frame.shape, dtype=np.int16)      # compression noise
        frame = np.clip(frame, 0, 255).astype(np.uint8)
        got, report = pg.snap_frame(frame, CORNER[0], CORNER[1], pose.shape[1], pose.shape[0], SCALE, KEY, palette)
        assert same(got, pose) and report == {"cells": int((pose[..., 3] > 0).sum()), "notFlat": 0, "keyMixed": 0}


def test_snap_frame_unmixes_the_key_from_the_edge_cells():
    sprite = toy_sprite()
    palette = pg.palette_of(sprite)
    pose = poses()[0]
    cols, rows = pose.shape[1], pose.shape[0]

    def with_cell(row, col, colour):
        frame = video_frame(pose, SCALE, CORNER, SIZE)[..., :3].copy()
        frame[CORNER[1] + row * SCALE:CORNER[1] + (row + 1) * SCALE, CORNER[0] + col * SCALE:CORNER[0] + (col + 1) * SCALE] = colour
        return frame

    def blend(colour, share):
        return [round((1 - share) * a + share * b) for a, b in zip(colour, KEY)]

    row, col = ROOM[1] + 14, ROOM[0] + 2                          # a cell of the body's outline
    assert tuple(pose[row, col]) == (*LINE, 255)
    covered = with_cell(row, col, blend(LINE, 0.4))               # the outline cell, 40 percent key
    kept, _ = pg.snap_frame(covered, CORNER[0], CORNER[1], cols, rows, SCALE, KEY, palette, key_mixed=False)
    assert tuple(kept[row, col, :3]) != LINE                      # the palette alone: its most key-like colour
    got, report = pg.snap_frame(covered, CORNER[0], CORNER[1], cols, rows, SCALE, KEY, palette)
    assert same(got, pose) and report["keyMixed"] == 1            # unmixed: the outline's colour again
    beside = with_cell(row, col - 1, blend(LINE, 0.55))           # a cell outside the outline, more key than figure:
    assert (np.abs(np.asarray(blend(LINE, 0.55)) - np.asarray(KEY)) ** 2).sum() ** 0.5 > pg.VIDEO_KEY_DISTANCE  # it counts as figure
    got, report = pg.snap_frame(beside, CORNER[0], CORNER[1], cols, rows, SCALE, KEY, palette)
    assert same(got, pose) and report["keyMixed"] == 1            # it goes
    best, share = pg.unmix_edge([COLOURS[3], blend(COLOURS[4], 0.3)], KEY, palette)
    assert [tuple(palette[i]) for i in best] == [COLOURS[3], COLOURS[4]] and share[0] == 0 and abs(share[1] - 0.3) < 0.02


def test_lattice_shift_finds_a_moved_lattice_and_prefers_the_jobs_own():
    pose = poses()[0]
    cols, rows = pose.shape[1], pose.shape[0]
    palette = pg.palette_of(toy_sprite())
    frame = video_frame(pose, SCALE, CORNER, SIZE)[..., :3]
    assert pg.lattice_shift(frame, CORNER[0], CORNER[1], cols, rows, SCALE, KEY) == (0, 0)
    nudged = video_frame(pose, SCALE, (CORNER[0] + 1, CORNER[1] - 1), SIZE)[..., :3]     # the cells' inner parts stay flat
    assert pg.lattice_shift(nudged, CORNER[0], CORNER[1], cols, rows, SCALE, KEY) == (0, 0)
    moved = video_frame(pose, SCALE, (CORNER[0] + 4, CORNER[1] + 4), SIZE)[..., :3]      # half a cell off
    shift = pg.lattice_shift(moved, CORNER[0], CORNER[1], cols, rows, SCALE, KEY)
    at_job = pg.snap_frame(moved, CORNER[0], CORNER[1], cols, rows, SCALE, KEY, palette)[1]
    at_shift = pg.snap_frame(moved, CORNER[0] + shift[0], CORNER[1] + shift[1], cols, rows, SCALE, KEY, palette)[1]
    assert shift != (0, 0) and at_shift["notFlat"] == 0 < at_job["notFlat"]


def test_find_cycle_and_the_picks():
    four = poses()
    clip = [four[0]] * 6 + [four[(i // 3) % 4] for i in range(60)] + [four[0]] * 6
    cycle = pg.find_cycle(clip)
    assert cycle["period"] == 12 and cycle["closure"] == 0
    picked = pg.pick_cycle(cycle["start"], cycle["period"], 4)
    assert len(picked) == 4 and len({clip[i].tobytes() for i in picked}) == 4      # one drawing of each pose
    assert pg.find_cycle(clip[:9])["period"] == 0                                   # too short for a loop
    action = [four[0]] * 5 + [four[1]] * 2 + [four[3]] * 2 + [four[1]] * 2 + [four[0]] * 5
    assert pg.moving_span(action) == (4, 11)                    # the still frames around the motion
    drifted = action[:11] + [np.roll(four[0], 1, axis=1)] * 5   # it comes to rest one cell beside where it started
    assert pg.moving_span(drifted) == (4, 11)
    chosen = pg.pick_motion(action, 4)
    assert chosen == sorted(set(chosen)) and chosen[0] == 4 and chosen[-1] == 11
    assert pg.pick_motion(action, 3, first=5, last=10) == sorted(pg.pick_motion(action, 3, first=5, last=10))


def test_head_lock_puts_the_sprites_head_back_and_leaves_another_head_alone():
    sprite = toy_sprite()
    head = np.zeros(sprite.shape[:2], dtype=bool)
    head[:HEAD_ROWS] = sprite[:HEAD_ROWS, :, 3] > 0
    up = poses()[1]
    boiled = up.copy()
    ys, xs = np.where(head)
    for n in range(0, len(ys), 5):                         # a fifth of the head's cells boil to another colour
        boiled[ys[n] + ROOM[1] - 1, xs[n] + ROOM[0], :3] = COLOURS[(n // 5) % 6]
    report = pg.head_lock(boiled, sprite, head, ROOM, line=LINE)
    assert report["locked"] and report["offset"] == [0, -1] and same(boiled, up)
    other = up.copy()
    other[ROOM[1] - 1:ROOM[1] - 1 + HEAD_ROWS][other[ROOM[1] - 1:ROOM[1] - 1 + HEAD_ROWS, :, 3] > 0] = (*COLOURS[4], 255)
    before = other.copy()
    report = pg.head_lock(other, sprite, head, ROOM, line=LINE)
    assert not report["locked"] and report["share"] < pg.LOCK_SHARE and same(other, before)


# --------------------------------------------------------------------------- a master and what is made from it

def test_fewer_colours_keeps_every_hue_and_is_the_same_on_every_run():
    rng = np.random.default_rng(7)
    ramps = [np.clip(np.asarray(base) + rng.integers(-6, 7, size=(40, 3)), 0, 255) for base in
             ((20, 24, 60), (60, 34, 20), (8, 8, 10), (230, 200, 170), (200, 30, 40))]       # navy, brown, black, skin, red
    colours = np.concatenate(ramps).astype(np.uint8)
    out = pg.fewer_colours(colours, 5)
    assert out.shape == colours.shape and len(np.unique(out, axis=0)) == 5
    for n in range(5):                                         # every ramp keeps one colour of its own
        assert len(np.unique(out[n * 40:(n + 1) * 40], axis=0)) == 1
    assert (pg.fewer_colours(colours, 5) == out).all() and (pg.fewer_colours(colours, 400) == colours).all()
    with pytest.raises(ValueError, match="at least 1"):
        pg.fewer_colours(colours, 0)


def test_boots_are_the_feet_and_a_blade_on_the_ground_does_not_move_them():
    sprite = toy_sprite()
    assert pg.boots(sprite) == (7.5, 23) and pg.feet_runs(sprite) == ([(2, 5), (10, 13)], 23)
    assert pg.boots(toy_sprite(12, 24)) == (5.5, 23)           # legs three cells wide are feet too
    armed = with_blade(sprite, 8)
    assert pg.boots(armed) == (8 + 7.5, 23)                    # the blade's tip is on the ground row and is no foot
    staff = np.pad(sprite, ((0, 0), (3, 0), (0, 0)))
    staff[2:, 0:2] = (*LINE, 255)                              # tall but thin
    assert pg.boots(staff) == (3 + 7.5, 23)


def test_drop_key_near_takes_what_is_still_nearly_key_off_the_edge():
    sprite = toy_sprite()
    spoiled = np.pad(sprite, ((0, 0), (0, 2), (0, 0)))
    spoiled[10:14, 16] = (230, 40, 220, 255)                   # nearly magenta, on the edge
    spoiled[10:14, 17] = (250, 10, 250, 255)
    assert pg.drop_key_near(spoiled, KEY) == 8 and same(spoiled[:, :16], sprite)
    assert pg.drop_key_near(sprite.copy(), KEY) == 0


def test_drop_key_mixed_makes_a_half_covered_outline_cell_an_outline_cell_again():
    sprite = toy_sprite()
    half = sprite.copy()
    half[12, 2, :3] = (np.asarray(LINE) + np.asarray(KEY)) // 2            # the outline's colour, half key
    kept = half.copy()
    assert pg.drop_key_mixed(kept, KEY, line=LINE) == {"removed": 0, "darkened": 1} and same(kept, sprite)
    alone = half.copy()                                        # without the line colour the cell is judged by its inner
    assert pg.drop_key_mixed(alone, KEY) == {"removed": 0, "darkened": 0} and same(alone, half)     # neighbours: no blend of those
    assert pg.drop_key_mixed(sprite.copy(), KEY, line=LINE) == {"removed": 0, "darkened": 0}


def test_drop_key_holes_opens_a_small_gap_inside_the_figure_and_leaves_a_patch_of_its_own_colour():
    sprite = toy_sprite()
    gap = sprite.copy()
    gap[12, 7] = (184, 2, 182, 255)                            # one cell inside the body: key mixed with its neighbours
    assert pg.drop_key_holes(gap, KEY) == 1 and gap[12, 7, 3] == 0
    gap[12, 7] = sprite[12, 7]
    assert same(gap, sprite)
    patch = sprite.copy()
    patch[10:13, 6:9] = (200, 30, 190, 255)                    # nine cells: that is a colour of the figure
    assert pg.drop_key_holes(patch, KEY) == 0 and (patch[10:13, 6:9, 3] == 255).all()
    assert pg.drop_key_holes(sprite.copy(), KEY) == 0


def test_ink_key_mixed_inks_a_small_blend_of_line_and_key_and_leaves_a_patch():
    sprite = toy_sprite()
    blend = tuple(int(v) for v in (np.asarray(LINE) + np.asarray(KEY)) // 2)
    gap = sprite.copy()
    gap[12, 6:8, :3] = blend                                   # two cells inside the body
    assert pg.ink_key_mixed(gap, KEY, LINE) == 2 and (gap[12, 6:8, :3] == np.asarray(LINE)).all()
    cloak = sprite.copy()
    cloak[10:13, 5:9, :3] = blend                              # twelve cells: a colour of the figure
    assert pg.ink_key_mixed(cloak, KEY, LINE) == 0 and (cloak[10:13, 5:9, :3] == np.asarray(blend)).all()
    assert pg.ink_key_mixed(sprite.copy(), KEY, LINE) == 0


def test_rule_outline_takes_specks_off_and_draws_the_line_outside_a_bare_cell():
    sprite = toy_sprite()
    again, report = pg.rule_outline(sprite)
    assert same(again, sprite) and report["closed"] and report["lineCellsAdded"] == 0 and report["offset"] == [0, 0]
    assert report["line"] == list(LINE)
    bare = sprite.copy()
    bare[12, 2, :3] = COLOURS[3]                               # a lit cell on the silhouette's edge
    closed, report = pg.rule_outline(bare)
    assert report == {"line": list(LINE), "specks": 0, "bareCells": 1, "lineCellsAdded": 1, "closed": True, "offset": [0, 0]}
    assert tuple(closed[12, 1]) == (*LINE, 255) and tuple(closed[12, 2, :3]) == COLOURS[3]      # outside it: nothing is lost
    speck = np.pad(sprite, ((0, 0), (0, 3), (0, 0)))
    speck[12, 16] = (*LINE, 255)                               # hangs on the figure by one side
    cleaned, report = pg.rule_outline(speck)
    assert same(cleaned, sprite) and report["specks"] == 1
    grown = np.pad(sprite, ((0, 0), (4, 0), (0, 0)))           # the sprite's cells are now at x + 4
    grown[12, 3:6] = (*COLOURS[4], 255)                        # a lit bar with no line around it, joined to the body
    only = np.zeros(grown.shape[:2], dtype=bool)
    only[12, 3:6] = True
    outlined, report = pg.rule_outline(grown, only=only)
    assert report["closed"] and report["lineCellsAdded"] == 3 + 3 + 1 and report["offset"] == [-2, 0]
    assert tuple(outlined[12, 0]) == (*LINE, 255) and (outlined[12, 1:4, :3] == COLOURS[4]).all()
    rest = np.ones(sprite.shape[:2], dtype=bool)
    rest[11:14, 0:2] = False                                   # where the bar and its line lie
    assert (outlined[:, 2:][rest] == sprite[rest]).all()       # what is not named stays as it is, cell for cell
    with pytest.raises(ValueError, match="empty"):
        pg.rule_outline(np.zeros((4, 4, 4), dtype=np.uint8))


def test_fit_lattice_finds_a_sprite_in_a_moved_picture_and_read_lattice_reads_it():
    sprite = toy_sprite()
    cells = on_canvas(sprite, (40, 40), (9, 6))
    picture = lattice_picture(cells, 16, (640, 640), shift=(5, -3))
    fit = pg.fit_lattice(picture, sprite, 16, (9, 6))
    assert fit["cell"] == 16.0 and fit["share"] == 1.0 and fit["cells"] == fit["equal"]
    assert abs(fit["offset"][0] - 5.0) <= 1.0 and abs(fit["offset"][1] + 3.0) <= 1.0
    read = pg.read_lattice(picture, fit["cell"], fit["offset"], 40, 40)
    part = read[6:30, 9:25]
    opaque = sprite[..., 3] > 0
    assert (np.round(part[opaque]) == sprite[opaque][:, :3]).all()
    assert (np.round(read[0, 5]) == np.asarray(KEY)).all()
    other = sprite.copy()
    other[..., :3] = 255 - other[..., :3]
    assert pg.fit_lattice(picture, other, 16, (9, 6))["share"] < 0.2            # another figure is not found there


def test_to_palette_takes_near_colours_to_the_palette_and_keeps_a_few_new_ones():
    palette = np.asarray([LINE, *COLOURS], np.uint8)
    colours = np.asarray([(26, 17, 13), COLOURS[2], (10, 200, 200), (12, 204, 198), (250, 250, 250)], np.uint8)
    out, new = pg.to_palette(colours, palette, extra=2)
    assert out[0].tolist() == list(LINE) and out[1].tolist() == list(COLOURS[2]) and new == 2
    assert out[2].tolist() == out[3].tolist() and out[4].tolist() == [250, 250, 250]
    empty, new = pg.to_palette(np.zeros((0, 3), np.uint8), palette)
    assert len(empty) == 0 and new == 0
