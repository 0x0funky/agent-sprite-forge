"""Tests for skills/generate2dsprite/scripts/sheet_qc.py (module B03-sprite-authoring-qc, tasks B03-T1/T2).

Synthetic sheets and walkers are built in this file. The only real art is the
provenance-tracked fox sheet (tests/fixtures/real/raw-fox-run-v1.png), whose
numbers come from report v2 3.2 (fox_spill_report.json, fox_frame_report.json,
fox_head_scale_fast.json).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from forge_testutils import (
    assert_cli_help, assert_valid_contract, load_script, make_magenta_sheet, real_fixture, run_cli, script_path,
)

SKILL = "generate2dsprite"
SCRIPT = script_path(SKILL, "sheet_qc")
sq = load_script(SKILL, "sheet_qc")

# Cluster centres of the report v2 k-means palette of the fox fixture's solid pixels (palette10.npy,
# rounded): NEAR = light orange fur and light boot, FAR = the darker shades, TUNIC = the two teal shades.
FOX_NEAR = "#d15823,#4b2418"
FOX_FAR = "#a03c19,#39180f"
FOX_TUNIC = "#255e5e,#152e2d"

NEAR = (232, 122, 44)
FAR = (150, 72, 22)
BODY = (40, 92, 160)
HEAD = (236, 204, 160)
INK = (24, 22, 30)


# --------------------------------------------------------------------------- synthetic art

def keyed(image: Image.Image, key=(255, 0, 255)) -> np.ndarray:
    """Exact alpha for a flat chroma sheet: the key colour becomes transparent."""
    pixels = np.array(image.convert("RGBA"))
    pixels[(pixels[..., :3] == key).all(axis=-1)] = 0
    return pixels


def save(array: np.ndarray, path: Path) -> Path:
    Image.fromarray(array, "RGBA").save(path)
    return path


def walker(lead: str, phase: int, *, head_scale: float = 1.0, dx: int = 0, dy: int = 0,
           size=(120, 176)) -> np.ndarray:
    """A side-view walker facing right: far leg, torso, head, near leg (drawing order).

    ``lead`` names the leg whose stride this half-cycle starts with (it is forward at contact);
    ``phase`` 0..3 is contact, down, passing, up.
    """
    feet = {0: ((90, 150), (32, 146)), 1: ((68, 150), (40, 136)), 2: ((52, 150), (74, 138)), 3: ((38, 150), (86, 140))}
    lead_foot, trail_foot = feet[phase]
    lead_foot = (lead_foot[0] + dx, lead_foot[1] + dy)
    trail_foot = (trail_foot[0] + dx, trail_foot[1] + dy)
    near_foot, far_foot = (lead_foot, trail_foot) if lead == "near" else (trail_foot, lead_foot)
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    hip = (60 + dx, 100 + dy)

    def leg(foot, color):
        knee = ((hip[0] + foot[0]) / 2 + 6, (hip[1] + foot[1]) / 2)
        draw.line([hip, knee, foot], fill=color + (255,), width=9, joint="curve")
        draw.rectangle((foot[0] - 2, foot[1] - 5, foot[0] + 9, foot[1] - 1), fill=color + (255,))

    leg(far_foot, FAR)
    draw.rectangle((49 + dx, 55 + dy, 71 + dx, 101 + dy), fill=INK + (255,))
    draw.rectangle((51 + dx, 57 + dy, 69 + dx, 99 + dy), fill=BODY + (255,))
    half = 16 * head_scale
    cx, cy = 62 + dx, 55 + dy - half
    draw.rectangle((cx - half, cy - half, cx + half, cy + half), fill=INK + (255,))
    draw.rectangle((cx - half + 2, cy - half + 2, cx + half - 2, cy + half - 2), fill=HEAD + (255,))
    draw.rectangle((cx + half * 0.3, cy - half * 0.2, cx + half * 0.6, cy + half * 0.1), fill=INK + (255,))
    leg(near_foot, NEAR)
    return np.asarray(image).copy()


def walk_cycle(alternating: bool, **kwargs) -> list[np.ndarray]:
    """Eight frames: contact, down, passing, up twice; the second half swaps the lead when alternating."""
    second = "far" if alternating else "near"
    return [walker("near", phase, **kwargs) for phase in range(4)] + \
           [walker(second, phase, dy=1, **kwargs) for phase in range(4)]


def tail_sheet(tmp_path: Path, spill_px: int = 6) -> tuple[Path, list[list[int]]]:
    """1x2 RGBA sheet of 64 px cells; subject 0 crosses into cell 1 by ``spill_px``."""
    image, boxes = make_magenta_sheet(1, 2, 64, spill_px=spill_px, return_boxes=True)
    return save(keyed(image), tmp_path / "tail-sheet.png"), boxes


def no_stage_left(parent: Path) -> bool:
    return not any(".stage-" in child.name for child in parent.iterdir())


def run_ok(*args) -> dict:
    result = run_cli([SCRIPT, *map(str, args)])
    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii()
    return json.loads(result.stdout)


# --------------------------------------------------------------------------- CLI conventions

def test_help_works_under_cp1252():
    assert_cli_help(SKILL, "sheet_qc")
    for command in ("spill", "frames"):
        result = run_cli([SCRIPT, command, "--help"], "cp1252")
        assert result.returncode == 0, result.stderr
        assert result.stdout.isascii() and "--output-dir" in result.stdout


def test_refuses_an_existing_output_dir(tmp_path):
    sheet, _ = tail_sheet(tmp_path)
    existing = tmp_path / "qc"
    existing.mkdir()
    (existing / "keep.txt").write_text("mine", encoding="utf-8")
    result = run_cli([SCRIPT, "spill", "--input", sheet, "--rows", "1", "--cols", "2", "--output-dir", existing])
    assert result.returncode == 1
    assert result.stderr.startswith("error:") and "Traceback" not in result.stderr
    assert sorted(path.name for path in existing.iterdir()) == ["keep.txt"]


def test_strict_qc_failure_publishes_nothing(tmp_path):
    sheet, _ = tail_sheet(tmp_path)
    out = tmp_path / "qc"
    result = run_cli([SCRIPT, "spill", "--input", sheet, "--rows", "1", "--cols", "2", "--strict", "--output-dir", out])
    assert result.returncode == 1
    assert "cross_cell_components" in result.stderr and "nothing was published" in result.stderr
    assert not out.exists() and no_stage_left(tmp_path)
    frames_out = tmp_path / "frames-qc"
    walk = [save(frame, tmp_path / f"w{index}.png") for index, frame in enumerate(walk_cycle(False))]
    result = run_cli([SCRIPT, "frames", "--frames", *walk, "--cycle", "walk", "--near-colors", "#e87a2c",
                      "--far-colors", "#964816", "--strict", "--output-dir", frames_out])
    assert result.returncode == 1 and "leg_alternation" in result.stderr
    assert not frames_out.exists() and no_stage_left(tmp_path)


def test_errors_are_one_line_without_traceback(tmp_path):
    opaque = tmp_path / "opaque.png"
    Image.new("RGB", (40, 40), (10, 200, 30)).save(opaque)
    result = run_cli([SCRIPT, "spill", "--input", opaque, "--rows", "1", "--cols", "1", "--key", "none",
                      "--output-dir", tmp_path / "qc"])
    assert result.returncode == 1
    assert result.stderr.startswith("error:") and "no transparent pixels" in result.stderr
    assert "Traceback" not in result.stderr
    result = run_cli([SCRIPT, "frames", "--frames", opaque, "--near-colors", "#zzzzzz", "--output-dir", tmp_path / "x"])
    assert result.returncode == 1 and "Traceback" not in result.stderr


# --------------------------------------------------------------------------- spill (B03-T1)

def test_spill_measures_owner_pixels_over_line_and_overhang(tmp_path):
    """report v2 P1-3: a tail crossing the cell line is reported with its owner cell, the pixels
    over the line and the overhang, before any slicing."""
    sheet, boxes = tail_sheet(tmp_path, spill_px=6)
    summary = run_ok("spill", "--input", sheet, "--rows", 1, "--cols", 2, "--output-dir", tmp_path / "qc")
    assert summary["status"] == "fail" and summary["failed"] == ["cross_cell_components", "cross_cell_visible"]
    report = json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))
    assert_valid_contract(report, "sprite", "sheet_qc_v1", skill=SKILL)
    _x0, y0, x1, y1 = boxes[0]
    owner = report["cells"][0]
    assert owner["cell"] == [0, 0]
    assert owner["components_over_line"] == 1
    assert owner["pixels_over_line"] == (x1 - 64) * (y1 - y0) == 6 * (y1 - y0)
    assert owner["overhang_px"] == 6 and owner["cut_by_cell_edge"] is True
    assert report["cells"][1]["intruders"] == [{"label": 1, "owner_cell": [0, 0],
                                                "pixels_in_this_cell": owner["pixels_over_line"]}]
    assert report["crossing_components"][0]["overhang_sides"] == {"left": 0, "top": 0, "right": 6, "bottom": 0}
    assert report["inputs"][0]["path"] == "../tail-sheet.png"
    assert report["outputs"] == [{"path": "spill-overlay.png",
                                  "sha256": hashlib.sha256((tmp_path / "qc" / "spill-overlay.png").read_bytes()).hexdigest(),
                                  "bytes": (tmp_path / "qc" / "spill-overlay.png").stat().st_size}]


def test_clean_sheet_passes_and_chroma_sheets_are_keyed(tmp_path):
    clean = make_magenta_sheet(2, 2, 64)
    path = tmp_path / "clean.png"
    clean.save(path)  # opaque magenta: --key auto keys it with the shared soft keyer
    summary = run_ok("spill", "--input", path, "--rows", 2, "--cols", 2, "--output-dir", tmp_path / "qc")
    report = json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in report["checks"]}
    assert report["image"]["keyed"]["key"] == "magenta"
    assert by_id["cross_cell_components"]["status"] == "pass"
    assert by_id["empty_cells"]["status"] == "pass" and by_id["sheet_edge"]["status"] == "pass"
    spill_sheet = tmp_path / "spill.png"
    make_magenta_sheet(1, 2, 64, spill_px=6).save(spill_sheet)
    summary = run_ok("spill", "--input", spill_sheet, "--rows", 1, "--cols", 2, "--output-dir", tmp_path / "qc2")
    report = json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))
    assert report["cells"][0]["overhang_px"] == 6


def test_empty_cells_sheet_edge_and_expect():
    pixels = np.zeros((40, 120, 4), np.uint8)
    pixels[10:30, 10:30] = (200, 50, 50, 255)
    pixels[10:30, 50:70] = (50, 200, 50, 255)
    analysis = sq.analyse_spill(pixels, 1, 3)
    by_id = {item["id"]: item for item in analysis["checks"]}
    assert by_id["empty_cells"]["status"] == "fail" and by_id["empty_cells"]["value"] == [[0, 2]]
    assert {item["id"]: item for item in sq.analyse_spill(pixels, 1, 3, expect=2)["checks"]}["empty_cells"][
        "status"] == "pass"
    pixels[0:5, 90:100] = (50, 50, 200, 255)  # touches the top edge of the image
    pixels[5:30, 90:110] = (50, 50, 200, 255)
    by_id = {item["id"]: item for item in sq.analyse_spill(pixels, 1, 3)["checks"]}
    assert by_id["sheet_edge"]["status"] == "fail" and by_id["sheet_edge"]["value"] == 1


def test_faint_specks_band_and_safe_frame_are_warnings():
    pixels = np.zeros((64, 128, 4), np.uint8)
    pixels[20:44, 20:44] = (200, 60, 60, 255)
    pixels[20:44, 58:64] = (200, 60, 60, 255)  # ends inside cell 0, within the band of the line x = 64
    pixels[20:44, 84:108] = (60, 60, 200, 255)
    pixels[2:4, 2:6] = (255, 255, 255, 3)       # haze at alpha 3 (floor)
    pixels[50:52, 100:102] = (255, 255, 255, 10)  # a detached faint island
    analysis = sq.analyse_spill(pixels, 1, 2, band=4)
    by_id = {item["id"]: item for item in analysis["checks"]}
    assert by_id["cross_cell_components"]["status"] == "pass"
    assert by_id["boundary_band"]["status"] == "warn" and by_id["boundary_band"]["value"] == 24 * 4  # x 60..63
    assert by_id["safe_frame"]["status"] == "warn" and by_id["safe_frame"]["cells"] == [[0, 0]]
    assert by_id["faint_specks"]["status"] == "warn"
    assert analysis["specks"]["floor_px"] == 8 and analysis["specks"]["detached_components"] == 1
    assert sq.worst_status([item["status"] for item in analysis["checks"]]) == "warn"


def test_fox_spill_tail_58_px_over_the_line_overhang_6(tmp_path):
    """B03-T1 acceptance on the real fox sheet (report v2 3.2.1, fox_spill_report.json): frame 3's tail
    has 58 solid px over the x = 1152 line and overhangs it by 6 px (77 px and 7 px at the visible edge)."""
    fox = real_fixture("raw-fox-run-v1.png")
    summary = run_ok("spill", "--input", fox, "--rows", 2, "--cols", 4, "--output-dir", tmp_path / "fox-spill")
    report = json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))
    assert_valid_contract(report, "sprite", "sheet_qc_v1", skill=SKILL)
    crossing = report["crossing_components"]
    assert len(crossing) == 1
    assert crossing[0]["owner_cell"] == [0, 3] and crossing[0]["pixels_per_cell"] == {"2": 58, "3": 50621}
    frame3 = report["cells"][3]
    assert (frame3["pixels_over_line"], frame3["overhang_px"]) == (58, 6)
    assert (frame3["visible_pixels_over_line"], frame3["visible_overhang_px"]) == (77, 7)
    assert frame3["margins_px"]["left"] == -6
    assert report["cells"][2]["intruders"][0]["pixels_in_this_cell"] == 58
    assert [band["solid_px"] for band in report["boundary_band"]] == [24, 0, 246, 0]
    assert report["specks"]["floor_px"] == 67907 and report["specks"]["detached_components"] == 7
    assert summary["warnings"] == ["boundary_band", "safe_frame", "faint_specks"]


@pytest.mark.perf
def test_fox_spill_runs_within_1_5_s(tmp_path):
    """B03-T1 acceptance: the whole spill check (load, analysis, overlay, report) on the 1536x1024 fox
    sheet takes at most 1.5 s in-process."""
    fox = real_fixture("raw-fox-run-v1.png")
    sq.main(["spill", "--input", str(fox), "--rows", "2", "--cols", "4", "--output-dir", str(tmp_path / "warm")])
    start = time.perf_counter()
    status = sq.main(["spill", "--input", str(fox), "--rows", "2", "--cols", "4", "--output-dir", str(tmp_path / "t")])
    elapsed = time.perf_counter() - start
    assert status == 0
    assert elapsed <= 1.5, f"spill took {elapsed:.2f} s"


# --------------------------------------------------------------------------- ownership slicing

def test_ownership_slice_keeps_every_part_on_one_registered_canvas():
    image, boxes = make_magenta_sheet(1, 2, 64, spill_px=6, return_boxes=True)
    pixels = keyed(image)
    frames, info = sq.ownership_slice(pixels, [(0, 0, 64, 64), (64, 0, 128, 64)], min_area=16)
    assert info["padding"] == [0, 0, 6, 0] and info["canvas"] == [70, 64]
    first, second = frames
    x0, y0, x1, y1 = boxes[0]
    assert (first[y0:y1, x0:x1, 3] == 255).all()  # the whole subject, tail included, in frame 0
    assert int((first[..., 3] > 0).sum()) == (x1 - x0) * (y1 - y0)
    bx0, by0, bx1, by1 = boxes[1]
    assert (second[by0:by1, bx0 - 64:bx1 - 64, 3] == 255).all()  # nominal cell origin is kept
    assert info["dropped_px"] == 0
    spare = np.zeros((64, 192, 4), np.uint8)
    spare[:, :128] = pixels  # a third cell left empty: --count 2 uses the first two cells only
    frames, info = sq.ownership_slice(spare, [(0, 0, 64, 64), (64, 0, 128, 64), (128, 0, 192, 64)], min_area=16,
                                      count=2)
    assert len(frames) == 2 and info["cells_used"] == 2 and info["unused_cells_px"] == 0
    with pytest.raises(sq.QcError, match=r"Cell \[0, 2\] holds no subject"):
        sq.ownership_slice(spare, [(0, 0, 64, 64), (64, 0, 128, 64), (128, 0, 192, 64)], min_area=16)


def test_ownership_slice_reproduces_report_v2_fox_frames():
    """report v2 5.4: all 8 fox frames fit one 396x512 canvas (shared 12 px left padding). The bytes
    equal the prototype's own_frames.npy (pinned here by hash)."""
    pixels, _ = sq.load_input(real_fixture("raw-fox-run-v1.png"))
    import forge_core
    frames, info = sq.ownership_slice(pixels, forge_core.rounded_grid_boxes(1536, 1024, 2, 4))
    assert info["padding"] == [12, 0, 0, 0] and info["canvas"] == [396, 512]
    assert info["dropped_max_alpha"] <= 5  # only faint haze is dropped
    digest = hashlib.sha256(np.stack(frames).tobytes()).hexdigest()
    assert digest == "dbbd0093a14f537f000fcd8771a784ac693555013a4452bebc994201133a8565"


# --------------------------------------------------------------------------- frames (B03-T2)

@pytest.fixture(scope="module")
def fox_frames():
    import forge_core
    pixels, _ = sq.load_input(real_fixture("raw-fox-run-v1.png"))
    frames, _ = sq.ownership_slice(pixels, forge_core.rounded_grid_boxes(1536, 1024, 2, 4))
    return frames


def test_fox_frames_near_leg_leads_8_of_8_head_ratio_0_94_drift_6_6(tmp_path):
    """B03-T2 acceptance (report v2 3.2.3-3.2.5): with the fox's NEAR/FAR shades and tunic colours the
    check reports 8/8 frames with the NEAR leg forward (a duplicated half-cycle), frame 6's head
    ratio 0.94 and a torso drift of 6.6 game px; it also flags the row baseline and the 3->4 seam."""
    fox = real_fixture("raw-fox-run-v1.png")
    summary = run_ok("frames", "--sheet", fox, "--rows", 2, "--cols", 4, "--cycle", "run", "--game-pixel", 8,
                     "--near-colors", FOX_NEAR, "--far-colors", FOX_FAR, "--torso-colors", FOX_TUNIC,
                     "--output-dir", tmp_path / "fox-frames")
    report = json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))
    assert_valid_contract(report, "sprite", "sheet_qc_v1", skill=SKILL)
    by_id = {item["id"]: item for item in report["checks"]}
    alternation = by_id["leg_alternation"]
    assert alternation["value"] == ["near"] * 8 and alternation["status"] == "fail"
    assert alternation["verdict"].startswith("duplicated half-cycle")
    assert alternation["fix"].startswith("regenerate frames 4-7")
    assert report["cells"][6]["head"]["head_ratio"] == pytest.approx(0.94, abs=0.005)
    assert by_id["identity_head_ratio"]["status"] == "fail" and by_id["identity_head_ratio"]["frames"] == [6]
    assert by_id["torso_drift"]["value"] == pytest.approx(6.6, abs=0.05)
    assert by_id["row_baseline"]["status"] == "warn" and by_id["row_baseline"]["value"] == pytest.approx(1.125, abs=0.01)
    assert report["seam_ranking"][0]["from"] == 3 and report["seam_ranking"][0]["to"] == 4
    assert by_id["phase_coverage"]["status"] == "pass"
    assert report["half_cycle"]["half_cycle_iou_median"] > report["half_cycle"]["adjacent_iou_median"]
    assert summary["failed"] == ["identity_head_ratio", "leg_alternation"]
    assert (tmp_path / "fox-frames" / "frames-review.png").is_file()


def test_fox_auto_mode_also_finds_the_duplicated_half_cycle(fox_frames):
    """Without declared colours the lightness convention (FAR one shade darker) still finds the same
    lead in both halves; the narrow 'down' frame is left unclear rather than guessed."""
    analysis = sq.analyse_frames(fox_frames, cycle="run", game_pixel=8, row_size=4)
    by_id = {item["id"]: item for item in analysis["checks"]}
    leads = by_id["leg_alternation"]["value"]
    assert by_id["leg_alternation"]["status"] == "fail" and by_id["leg_alternation"]["mode"] == "auto-luminance"
    assert "far" not in leads and leads.count("near") >= 6
    assert 5.5 <= by_id["torso_drift"]["value"] <= 8.0  # trunk geometry, no costume colours


@pytest.mark.parametrize("colours", [True, False], ids=["declared-colours", "auto-lightness"])
def test_same_leg_walk_fails_and_alternating_walk_passes(colours):
    """B03-T2 acceptance (jev walk_phase_check.json: a same-leg walk passed strict QC)."""
    options = {"near_colors": [NEAR], "far_colors": [FAR]} if colours else {}
    same = sq.analyse_frames(walk_cycle(False), cycle="walk", **options)
    alternating = sq.analyse_frames(walk_cycle(True), cycle="walk", **options)
    same_check = {item["id"]: item for item in same["checks"]}["leg_alternation"]
    alt_check = {item["id"]: item for item in alternating["checks"]}["leg_alternation"]
    assert same_check["status"] == "fail", same_check
    assert alt_check["status"] == "pass", alt_check
    assert all(not same for _, _, same in alt_check["pairs"]) and len(alt_check["pairs"]) >= 3


def test_frames_cli_on_frame_files_validates(tmp_path):
    paths = [save(frame, tmp_path / f"walk-{index}.png") for index, frame in enumerate(walk_cycle(True))]
    summary = run_ok("frames", "--frames", *paths, "--cycle", "walk", "--frames-per-row", 4,
                     "--output-dir", tmp_path / "qc")
    report = json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))
    assert_valid_contract(report, "sprite", "sheet_qc_v1", skill=SKILL)
    assert report["grid"] == {"rows": 2, "cols": 4}
    assert [ref["path"] for ref in report["inputs"]] == [f"../walk-{index}.png" for index in range(8)]
    assert report["cells"][0]["source"] == "walk-0.png" and report["cells"][0]["cell"] is None
    assert {item["id"]: item for item in report["checks"]}["leg_alternation"]["status"] == "pass"


def test_identity_flags_a_shrunken_head():
    frames = [walker("near", phase % 4, head_scale=0.86 if phase == 5 else 1.0) for phase in range(8)]
    analysis = sq.analyse_frames(frames)
    by_id = {item["id"]: item for item in analysis["checks"]}
    assert by_id["identity_head_ratio"]["status"] == "fail" and by_id["identity_head_ratio"]["frames"] == [5]
    assert analysis["cells"][5]["head"]["head_ratio"] <= 0.92
    assert all(abs(cell["head"]["head_ratio"] - 1) <= 0.03 for index, cell in enumerate(analysis["cells"]) if index != 5)


def test_master_head_is_the_identity_template():
    frames = [walker("near", phase) for phase in range(4)]
    master = walker("near", 0)
    analysis = sq.analyse_frames(frames, master=master)
    assert analysis["identity"]["template"] == "master"
    assert analysis["cells"][0]["head"]["residual"] == pytest.approx(0.0, abs=1e-6)
    assert all(cell["head"]["template"] is False for cell in analysis["cells"])


def test_near_duplicates_torso_drift_row_baseline_and_seam_in_game_px():
    frames = [walker("near", 0, dx=0), walker("near", 1, dx=8), walker("near", 2, dx=16),
              walker("near", 2, dx=16), walker("far", 0, dy=9), walker("far", 1, dy=9),
              walker("far", 2, dy=9), walker("far", 2, dx=-24, dy=9)]  # row 1 = row 0's poses, 9 px lower
    analysis = sq.analyse_frames(frames, game_pixel=8, row_size=4, torso_colors=[BODY], loop=True)
    by_id = {item["id"]: item for item in analysis["checks"]}
    assert [2, 3] in by_id["near_duplicates"]["value"] and by_id["near_duplicates"]["status"] == "warn"
    assert by_id["torso_drift"]["value"] == pytest.approx(40 / 8)  # torso x from -24 to +16 px
    assert by_id["torso_drift"]["method"] == "colour"
    assert by_id["row_baseline"]["value"] == pytest.approx(9 / 8, abs=0.01) and by_id["row_baseline"]["status"] == "warn"
    worst = analysis["seam_ranking"][0]
    assert (worst["from"], worst["to"]) in ((6, 7), (7, 0))
    assert by_id["seam"]["status"] == "warn"
    assert by_id["leg_alternation"]["status"] == "skipped" and by_id["phase_coverage"]["status"] == "skipped"


def test_phase_labels_find_contact_and_flight():
    frames = [walker("near", 0), walker("near", 1), walker("near", 2, dy=-12), walker("near", 3),
              walker("far", 0), walker("far", 1), walker("far", 2, dy=-12), walker("far", 3)]
    analysis = sq.analyse_frames(frames, cycle="run")
    labels = [cell["phase"]["label"] for cell in analysis["cells"]]
    assert labels[0] == labels[4] == "contact" and labels[2] == labels[6] == "flight"
    assert {item["id"]: item for item in analysis["checks"]}["phase_coverage"]["status"] == "pass"
    walk = sq.analyse_frames(frames, cycle="walk")
    coverage = {item["id"]: item for item in walk["checks"]}["phase_coverage"]
    assert coverage["status"] == "warn" and any("leaves the ground" in text for text in coverage["problems"])


def test_inputs_must_share_one_canvas_and_colours_come_in_pairs():
    with pytest.raises(sq.QcError, match="one canvas"):
        sq.analyse_frames([walker("near", 0), walker("near", 1, size=(100, 176))])
    with pytest.raises(sq.QcError, match="both --near-colors and --far-colors"):
        sq.analyse_frames(walk_cycle(True), cycle="walk", near_colors=[NEAR])
    with pytest.raises(sq.QcError, match="#rrggbb"):
        sq.parse_colors("#12345,#ffffff")
