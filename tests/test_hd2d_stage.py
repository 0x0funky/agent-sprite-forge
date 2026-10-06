"""Tests for validate_stage.py (stage.v1 checks, aspect layout solver, overlays) and scene_layout_guide.py
(B15-T1, B15-T2). Every stage and plate here is synthetic."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from forge_testutils import (FIXTURES_DIR, SKILLS_DIR, assert_cli_help, assert_valid_contract, contract_errors,
                             load_script, run_cli, script_path)

SKILL = "generate2dmap"
STAGE = load_script(SKILL, "validate_stage")
GUIDE = load_script(SKILL, "scene_layout_guide")
STAGE_SCRIPT = script_path(SKILL, "validate_stage")
GUIDE_SCRIPT = script_path(SKILL, "scene_layout_guide")


def requested_errors(document, name):
    """Errors against the vendored generate2dmap schemas, which hold this module's section 5 requests (D33)."""
    return contract_errors(document, "map", name, skill=SKILL)


# --------------------------------------------------------------------------- fixtures

def make_stage(**changes):
    """A consistent 800x450 battle stage: wide ground, a pond off the ground, a tower landmark that
    blocks walking, a dry floor protected from motion only, five slots and a boss slot."""
    stage = {
        "schema": "generate2dmap.stage.v1",
        "plate": "plate.png",
        "sourceSize": [800, 450],
        "fit": "cover",
        "reviewedAspectRange": [1.3333, 2.3333],
        "groundPolygons": [[[0.04, 0.55], [0.96, 0.55], [0.99, 0.97], [0.01, 0.97]]],
        "playableBand": [0.55, 0.97],
        "slots": {"hero": [[0.66, 0.68], [0.84, 0.76]], "enemy": [[0.14, 0.7], [0.26, 0.66], [0.4, 0.74]],
                  "boss": [0.26, 0.72]},
        "approachPoints": [[0.5, 0.62]],
        "protectedRegions": [
            {"id": "tower", "box": [0.44, 0.08, 0.56, 0.5], "appliesTo": ["walk", "motion", "edit"]},
            {"id": "dry-floor", "box": [0.3, 0.6, 0.7, 0.9], "appliesTo": ["motion"]},
        ],
        "effects": [{"id": "pond", "kind": "ripple", "polygon": [[0.02, 0.38], [0.22, 0.38], [0.22, 0.5], [0.02, 0.5]],
                     "period": 4, "amplitude": 1.5, "wavelength": 60, "axis": "x"}],
        "bakedContent": {"actors": False, "collectibles": False},
    }
    stage.update(copy.deepcopy(changes))
    return stage


def paint_plate(stage, size=None):
    """Flat-colour plate: sky gradient, stone ground, blue water, grey landmarks."""
    width, height = size or stage["sourceSize"]
    rows = np.linspace(0, 1, height)[:, None, None]
    sky = (np.array([70, 90, 140]) * (1 - rows) + np.array([150, 160, 180]) * rows).repeat(width, axis=1)
    image = Image.fromarray(sky.astype(np.uint8))
    draw = ImageDraw.Draw(image)
    scale = np.array([width, height], float)
    for polygon in stage["groundPolygons"]:
        draw.polygon([tuple(p) for p in np.asarray(polygon) * scale], fill=(150, 140, 120))
    for effect in stage.get("effects", []):
        draw.polygon([tuple(p) for p in np.asarray(effect["polygon"]) * scale], fill=(60, 120, 190))
    for region in stage.get("protectedRegions", []):
        if "box" in region:
            u0, v0, u1, v1 = region["box"]
            if "walk" in region.get("appliesTo", ["walk"]):
                draw.rectangle((u0 * width, v0 * height, u1 * width, v1 * height), fill=(110, 110, 120))
    return image


def write_stage(folder: Path, stage, plate=True, plate_size=None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "stage.json"
    path.write_text(json.dumps(stage), encoding="utf-8")
    if plate:
        paint_plate(stage, plate_size).save(folder / stage.get("plate", "plate.png"))
    return path


def run_main(module, argv, capsys):
    code = module.main([str(item) for item in argv])
    captured = capsys.readouterr()
    summary = json.loads(captured.out) if captured.out.strip() else None
    return code, summary, captured.err


def inside(point, polygon):
    """Independent crossing-number test in plain Python (not the module's numpy version)."""
    x, y = point
    hit = False
    for (ax, ay), (bx, by) in zip(polygon, polygon[-1:] + polygon[:-1]):
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            hit = not hit
    return hit


def checks_by_id(report):
    return {item["id"]: item for item in report["checks"]}


# --------------------------------------------------------------------------- validate_stage

def test_valid_stage_writes_four_aspect_renders_and_validates(tmp_path, capsys):
    stage_path = write_stage(tmp_path / "scene", make_stage())
    out = tmp_path / "qa"
    code, summary, err = run_main(STAGE, ["--stage", stage_path, "--output-dir", out], capsys)
    assert code == 0, err
    report = json.loads((out / "stage-qa.json").read_text(encoding="utf-8"))
    assert report["status"] == "needs-visual-review"  # numbers pass; a plate must still be looked at
    assert summary["status"] == report["status"] and summary["failed"] == []
    expected = {"4x3": (960, 720), "16x9": (1280, 720), "21x9": (1680, 720), "9x19.5": (390, 845)}
    for name, size in expected.items():
        for suffix in ("", "-boss"):
            with Image.open(out / f"layout-{name}{suffix}.png") as render:
                assert render.size == size
    with Image.open(out / "stage-overlay.png") as overlay:
        assert overlay.size == (800, 450)
    assert len(summary["renders"]) == 8
    assert {ref["path"] for ref in report["outputs"]} == {
        "stage-overlay.png", *(f"layout-{name}{suffix}.png" for name in expected for suffix in ("", "-boss"))}
    assert [ref["path"] for ref in report["inputs"]] == ["../scene/stage.json", "../scene/plate.png"]
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
    assert requested_errors(report, "stage_qa_v1") == []
    statuses = {item["id"]: item["status"] for item in report["checks"]}
    assert statuses["slots stand on ground"] == statuses["slots stay off water"] == "pass"
    assert statuses["layout 16:9 party-vs-enemies"] == "pass"
    assert statuses["layout solves at both ends of reviewedAspectRange"] == "pass"


def test_every_foot_lands_inside_a_polygon(tmp_path, capsys):
    stage = make_stage()
    stage_path = write_stage(tmp_path / "scene", stage)
    out = tmp_path / "qa"
    assert run_main(STAGE, ["--stage", stage_path, "--output-dir", out], capsys)[0] == 0
    report = json.loads((out / "stage-qa.json").read_text(encoding="utf-8"))
    ground = [[tuple(p) for p in polygon] for polygon in stage["groundPolygons"]]
    pond = [tuple(p) for p in stage["effects"][0]["polygon"]]
    u0, v0, u1, v1 = stage["protectedRegions"][0]["box"]
    assert len(report["layouts"]) == 8
    for layout in report["layouts"]:
        width, height = layout["viewport"]
        in_range = 1.3333 * 0.999 <= layout["ratio"] <= 2.3333 * 1.001
        for actor in layout["actors"]:
            if in_range:
                assert actor["placed"], (layout["aspect"], actor["id"])
            if not actor["placed"]:
                continue
            foot = actor["footUv"]
            assert any(inside(foot, polygon) for polygon in ground), (layout["aspect"], actor["id"], foot)
            assert not inside(foot, pond)
            assert not (u0 <= foot[0] < u1 and v0 <= foot[1] < v1)
            assert 0.55 <= foot[1] <= 0.97
            x0, y0, x1, y1 = actor["box"]
            assert 6 - 1e-6 <= x0 and x1 <= width - 6 + 1e-6 and 6 - 1e-6 <= y0 and y1 <= height - 6 + 1e-6
            for panel in layout["ui"]:
                px0, py0, px1, py1 = panel["box"]
                assert not (x0 < px1 + 6 and x1 > px0 - 6 and y0 < py1 + 6 and y1 > py0 - 6), panel["id"]
        feet = {actor["id"]: actor["foot"][1] for actor in layout["actors"]}
        assert [feet[ident] for ident in layout["drawOrder"]] == sorted(feet.values())  # foot-y sort


def test_without_ui_actors_stay_on_their_slots(tmp_path, capsys):
    stage_path = write_stage(tmp_path / "scene", make_stage())
    out = tmp_path / "qa"
    code, _, err = run_main(STAGE, ["--stage", stage_path, "--output-dir", out, "--ui-profile", "none",
                                    "--aspects", "16:9"], capsys)
    assert code == 0, err
    report = json.loads((out / "stage-qa.json").read_text(encoding="utf-8"))
    for layout in report["layouts"]:
        for actor in layout["actors"]:
            assert actor["movedPx"] == 0 and actor["scale"] == 1.0
            assert actor["foot"] == pytest.approx([actor["slot"][0] * 1280, actor["slot"][1] * 720], abs=1e-3)
    with Image.open(out / "layout-16x9.png") as render:
        assert render.size == (1280, 720)


def test_battle_ui_pushes_actors_off_panels_and_keeps_them_grounded(tmp_path, capsys):
    stage = make_stage()
    stage_path = write_stage(tmp_path / "scene", stage)
    out = tmp_path / "qa"
    assert run_main(STAGE, ["--stage", stage_path, "--output-dir", out, "--aspects", "16:9"], capsys)[0] == 0
    layout = json.loads((out / "stage-qa.json").read_text(encoding="utf-8"))["layouts"][0]
    hero2 = next(actor for actor in layout["actors"] if actor["id"] == "H2")
    command = next(panel for panel in layout["ui"] if panel["id"] == "command")
    assert hero2["movedPx"] > 0 and hero2["placed"] and hero2["grounded"]
    assert hero2["box"][2] <= command["box"][0] - 6 or hero2["box"][1] >= command["box"][3] + 6 \
        or hero2["box"][3] <= command["box"][1] - 6


def test_slot_on_water_fails(tmp_path, capsys):
    stream = [[0.45, 0.62], [0.6, 0.62], [0.6, 0.72], [0.45, 0.72]]  # water inside the ground polygon
    stage = make_stage(effects=[{"id": "stream", "kind": "ripple", "polygon": stream}])
    stage["slots"]["hero"][0] = [0.52, 0.67]
    stage_path = write_stage(tmp_path / "scene", stage)
    out = tmp_path / "qa"
    code, summary, _ = run_main(STAGE, ["--stage", stage_path, "--output-dir", out], capsys)
    assert code == 1 and summary["status"] == "fail"
    checks = checks_by_id(json.loads((out / "stage-qa.json").read_text(encoding="utf-8")))
    assert checks["slots stay off water"]["status"] == "fail"
    assert checks["slots stay off water"]["value"] == [{"slot": "H1", "uv": [0.52, 0.67]}]
    assert checks["slots stand on ground"]["status"] == "pass"  # the stream lies inside the ground polygon
    assert checks["water stays off ground polygons"]["status"] == "warn"


def test_walk_protected_region_is_not_standable_but_motion_only_is(tmp_path, capsys):
    stage = make_stage()
    stage["slots"]["enemy"][0] = [0.5, 0.75]  # inside the dry floor, which is protected from motion only
    stage_path = write_stage(tmp_path / "a", stage)
    assert run_main(STAGE, ["--stage", stage_path, "--output-dir", tmp_path / "qa-a"], capsys)[0] == 0
    stage["protectedRegions"][1]["appliesTo"] = ["walk"]
    stage_path = write_stage(tmp_path / "b", stage)
    code, summary, _ = run_main(STAGE, ["--stage", stage_path, "--output-dir", tmp_path / "qa-b"], capsys)
    assert code == 1 and "slots avoid walk-protected regions" in summary["failed"]
    del stage["protectedRegions"][1]["appliesTo"]  # no appliesTo: protected from everything
    stage_path = write_stage(tmp_path / "c", stage)
    code, summary, _ = run_main(STAGE, ["--stage", stage_path, "--output-dir", tmp_path / "qa-c"], capsys)
    assert code == 1 and "slots avoid walk-protected regions" in summary["failed"]


def test_static_rules_fail_with_their_check(tmp_path, capsys):
    cases = {
        "slots inside playableBand": make_stage(playableBand=[0.7, 0.97]),  # E2 stands at v 0.66
        "slots stand on ground": make_stage(slots={"hero": [[0.66, 0.4]], "enemy": [[0.2, 0.7]]}),
        "ground polygons are simple": make_stage(groundPolygons=[[[0.1, 0.55], [0.9, 0.97], [0.9, 0.55],
                                                                  [0.1, 0.97]]]),
        "effects avoid motion-protected regions": make_stage(effects=[
            {"id": "glow", "kind": "glow", "polygon": [[0.4, 0.3], [0.6, 0.3], [0.6, 0.4], [0.4, 0.4]]}]),
        "approach points are standable": make_stage(approachPoints=[[0.5, 0.3]]),
    }
    for index, (check_id, stage) in enumerate(cases.items()):
        stage_path = write_stage(tmp_path / f"s{index}", stage)
        code, summary, _ = run_main(STAGE, ["--stage", stage_path, "--output-dir", tmp_path / f"q{index}",
                                            "--aspects", "16:9"], capsys)
        assert code == 1 and check_id in summary["failed"], (check_id, summary["failed"])


def test_reviewed_aspect_range_that_crops_the_ground_fails(tmp_path, capsys):
    """At 3.5:1 a cover projection of a 16:9 plate shows only rows 0.25..0.75; ground below 0.8 is gone."""
    stage = make_stage(groundPolygons=[[[0.05, 0.8], [0.95, 0.8], [0.95, 0.97], [0.05, 0.97]]],
                       playableBand=[0.8, 0.97], reviewedAspectRange=[1.3333, 3.5], approachPoints=[],
                       slots={"hero": [[0.7, 0.88]], "enemy": [[0.3, 0.88]]})
    stage_path = write_stage(tmp_path / "scene", stage)
    out = tmp_path / "qa"
    code, summary, _ = run_main(STAGE, ["--stage", stage_path, "--output-dir", out, "--ui-profile", "none"], capsys)
    assert code == 1
    assert summary["failed"] == ["layout solves at both ends of reviewedAspectRange"]
    ends = checks_by_id(json.loads((out / "stage-qa.json").read_text(encoding="utf-8")))[
        "layout solves at both ends of reviewedAspectRange"]["value"]
    assert {end["ratio"]: bool(end["unplaced"]) for end in ends} == {1.3333: False, 3.5: True}


def test_out_of_range_aspect_only_warns(tmp_path, capsys):
    """9:19.5 lies outside [4:3, 21:9]: a portrait HUD that covers the ground leaves actors unplaced, which
    warns but does not fail the stage."""
    stage_path = write_stage(tmp_path / "scene", make_stage())
    ui = tmp_path / "ui.json"
    ui.write_text(json.dumps({"portrait": [{"id": "sheet", "box": [0, 0.4, 1, 1]}]}), encoding="utf-8")
    out = tmp_path / "qa"
    code, summary, _ = run_main(STAGE, ["--stage", stage_path, "--output-dir", out, "--aspects", "9:19.5",
                                        "--ui", ui], capsys)
    assert code == 0 and summary["status"] == "warn"
    portrait = checks_by_id(json.loads((out / "stage-qa.json").read_text(encoding="utf-8")))[
        "layout 9:19.5 party-vs-enemies"]
    assert portrait["status"] == "warn" and portrait["value"]["unplaced"]
    assert portrait["value"]["inReviewedRange"] is False


def test_contain_fit_letterboxes_and_places_feet(tmp_path, capsys):
    stage_path = write_stage(tmp_path / "scene", make_stage(fit="contain", reviewedAspectRange=[1.3333, 2.3333]))
    out = tmp_path / "qa"
    code, _, err = run_main(STAGE, ["--stage", stage_path, "--output-dir", out, "--aspects", "21:9",
                                    "--ui-profile", "none"], capsys)
    assert code == 0, err
    layout = json.loads((out / "stage-qa.json").read_text(encoding="utf-8"))["layouts"][0]
    assert layout["scale"] == 1.6 and layout["offset"] == [200.0, 0.0]
    with Image.open(out / "layout-21x9.png") as render:
        pixels = np.asarray(render.convert("RGB"))
    assert (pixels[300:400, :150] == (12, 14, 20)).all()  # the letterbox bar left of the plate
    assert all(actor["placed"] for actor in layout["actors"])


def test_plate_of_another_size_fails(tmp_path, capsys):
    stage_path = write_stage(tmp_path / "scene", make_stage(), plate_size=(640, 360))
    code, summary, _ = run_main(STAGE, ["--stage", stage_path, "--output-dir", tmp_path / "qa"], capsys)
    assert code == 1 and summary["failed"] == ["plate matches sourceSize"]


def test_missing_plate_validates_geometry_only(tmp_path, capsys):
    stage_path = write_stage(tmp_path / "scene", make_stage(), plate=False)
    code, summary, err = run_main(STAGE, ["--stage", stage_path, "--output-dir", tmp_path / "qa"], capsys)
    assert code == 0 and summary["status"] == "pass"
    assert "plate plate.png named by the stage was not found" in err


def test_contract_errors_name_the_json_path_and_publish_nothing(tmp_path, capsys):
    cases = [
        (lambda s: s["slots"]["hero"].__setitem__(0, [1.2, 0.7]), "$.slots.hero[0][0] must lie in 0..1"),
        (lambda s: s["slots"].pop("enemy"), "$.slots.enemy must be a list"),
        (lambda s: s.__setitem__("reviewedAspectRange", [2.0, 1.5]), "$.reviewedAspectRange must be [min, max]"),
        (lambda s: s["protectedRegions"][0].__setitem__("polygon", [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]]),
         "exactly one of polygon or box"),
        (lambda s: s["effects"][0].__setitem__("kind", "wave"), "$.effects[0].kind must be one of"),
        (lambda s: s["protectedRegions"].append({"id": "tower", "box": [0.1, 0.1, 0.2, 0.2]}), "is used twice"),
    ]
    for index, (mutate, message) in enumerate(cases):
        stage = make_stage()
        mutate(stage)
        stage_path = write_stage(tmp_path / f"s{index}", stage, plate=False)
        out = tmp_path / f"q{index}"
        code, summary, err = run_main(STAGE, ["--stage", stage_path, "--output-dir", out], capsys)
        assert code == 1 and summary is None and err.startswith("error: ") and message in err, err
        assert not out.exists()


def _apply(document, case):
    """Apply one A0 negative case ({set|remove: JSON pointer})."""
    pointer = case.get("set") or case.get("remove")
    keys = [int(part) if part.isdigit() else part for part in pointer.strip("/").split("/")]
    target = document
    for key in keys[:-1]:
        target = target[key]
    if "set" in case:
        target[keys[-1]] = case["value"]
    else:
        del target[keys[-1]]


def test_parser_accepts_the_contract_fixture_and_refuses_its_negative_cases():
    valid = json.loads((FIXTURES_DIR / "contracts" / "map.stage_v1.valid.json").read_text(encoding="utf-8"))
    STAGE.parse_stage(valid)
    cases = json.loads((FIXTURES_DIR / "contracts" / "map.stage_v1.invalid.json").read_text(encoding="utf-8"))
    for case in cases["cases"]:
        document = copy.deepcopy(valid)
        _apply(document, case)
        with pytest.raises(STAGE.StageError):
            STAGE.parse_stage(document)
    assert_valid_contract(make_stage(), "map", "stage_v1", skill=SKILL)
    assert requested_errors(make_stage(objectPosition=[0.5, 0.4]), "stage_v1") == []


def test_closed_polygons_and_object_position(tmp_path):
    stage = STAGE.parse_stage(make_stage(groundPolygons=[[[0.1, 0.6], [0.9, 0.6], [0.9, 0.9], [0.1, 0.9],
                                                          [0.1, 0.6]]], objectPosition=[0.0, 0.5]))
    assert len(stage.ground[0]) == 4 and STAGE.polygon_problems(stage.ground[0], stage.source_size) == []
    projection = STAGE.make_projection(stage, (960, 720))
    assert projection.offset == (0.0, 0.0) and projection.visible_uv()[0] == 0.0  # window pinned to the left


def test_outputs_are_deterministic(tmp_path, capsys):
    stage_path = write_stage(tmp_path / "scene", make_stage())
    for name in ("a", "b"):
        assert run_main(STAGE, ["--stage", stage_path, "--output-dir", tmp_path / name], capsys)[0] == 0
    files = sorted(path.name for path in (tmp_path / "a").iterdir())
    assert files == sorted(path.name for path in (tmp_path / "b").iterdir())
    for name in files:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


def test_points_in_polygon_matches_plain_python():
    rng = np.random.default_rng(5)
    polygon = [(0.1, 0.2), (0.8, 0.1), (0.6, 0.5), (0.9, 0.9), (0.2, 0.7)]
    points = rng.random((500, 2))
    expected = [inside(tuple(point), polygon) for point in points]
    assert STAGE._local_points_in_polygon(points, polygon).tolist() == expected
    scaled = [(x * 40, y * 40) for x, y in polygon]
    mask = STAGE._local_polygon_mask(np.asarray(scaled), (40, 40))
    centres = [(x + 0.5, y + 0.5) for y in range(40) for x in range(40)]
    assert mask.ravel().tolist() == [inside(point, scaled) for point in centres]
    box = STAGE._local_box_mask((2.5, 1.0, 7.5, 3.49), (10, 5))  # pixel centres in [2.5, 7.5) x [1, 3.49)
    assert np.argwhere(box).tolist() == [[row, col] for row in (1, 2) for col in range(2, 7)]


# --------------------------------------------------------------------------- scene_layout_guide

def test_guide_writes_percent_prompt_and_metadata_deterministically(tmp_path, capsys):
    stage_path = write_stage(tmp_path / "plan", make_stage(), plate=False)
    for name in ("a", "b"):
        code, summary, err = run_main(GUIDE, ["--stage", stage_path, "--output-dir", tmp_path / name], capsys)
        assert code == 0, err
    for name in ("guide.png", "prompt-block.txt", "guide.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    text = (tmp_path / "a" / "prompt-block.txt").read_text(encoding="ascii")
    assert "Walkable ground: x1%-99%, y55%-97%" in text
    assert "Outline: (4%, 55%), (96%, 55%), (99%, 97%), (1%, 97%)." in text
    assert "Playable band y55%-97%, walkable from x1% to x99%" in text
    assert ("Clear standing spots (paint nothing on them): hero 1 x66% y68%; hero 2 x84% y76%; enemy 1 x14% y70%; "
            "enemy 2 x26% y66%; enemy 3 x40% y74%; boss x26% y72%.") in text
    assert "Landmarks, keep them exactly there; nobody walks on them: tower x44%-56% y8%-50%." in text
    assert "pond (water) x2%-22% y38%-50%" in text
    assert "Forbidden on the walkable ground (outside the planned surfaces): people, characters" in text
    assert "No people, characters, creatures or silhouettes are painted into the plate" in text
    assert "do not copy its colours" in text
    record = json.loads((tmp_path / "a" / "guide.json").read_text(encoding="utf-8"))
    assert requested_errors(record, "scene_guide_v1") == []
    assert_valid_contract(record["qa"], "common", "qaEnvelope", skill=SKILL)
    assert record["qa"]["status"] == "pass" and record["canvas"] == [800, 450]
    assert record["percent"]["slots"][0] == {"id": "H1", "role": "hero", "x": 66, "y": 68}
    assert summary["metadata"].endswith("guide.json") and summary["status"] == "pass"
    with Image.open(tmp_path / "a" / "guide.png") as guide:
        assert guide.size == (800, 450)


def test_guide_percentages_follow_the_stage_values():
    stage = STAGE.parse_stage(make_stage(slots={"hero": [[0.125, 0.705]], "enemy": [[0.3349, 0.7]]}))
    text = GUIDE.prompt_block(stage, GUIDE.plan_percentages(stage), [])
    assert "hero 1 x13% y71%" in text and "enemy 1 x33% y70%" in text  # half-up rounding
    assert GUIDE.pct(0.125) == 13 and GUIDE.pct(0.705) == 71


def test_guide_pixels_encode_ground_slots_and_landmarks(tmp_path, capsys):
    stage_path = write_stage(tmp_path / "plan", make_stage(), plate=False)
    assert run_main(GUIDE, ["--stage", stage_path, "--output-dir", tmp_path / "g"], capsys)[0] == 0
    pixels = np.asarray(Image.open(tmp_path / "g" / "guide.png").convert("RGB"))
    assert tuple(pixels[int(0.68 * 450), int(0.66 * 800)]) == GUIDE.ROLE_FILL["hero"]
    assert tuple(pixels[int(0.70 * 450), int(0.14 * 800)]) == GUIDE.ROLE_FILL["enemy"]
    assert tuple(pixels[int(0.9 * 450), int(0.6 * 800)]) == GUIDE.GROUND_FILL
    assert tuple(pixels[int(0.3 * 450), int(0.5 * 800)]) == GUIDE.LANDMARK_FILL
    assert tuple(pixels[int(0.45 * 450), int(0.1 * 800)]) == GUIDE.WATER_FILL


def test_baked_content_and_forbid_options(tmp_path, capsys):
    stage = make_stage(bakedContent={"actors": ["a sleeping cat"], "collectibles": True}, effects=[])
    stage_path = write_stage(tmp_path / "plan", stage, plate=False)
    out = tmp_path / "g"
    assert run_main(GUIDE, ["--stage", stage_path, "--output-dir", out, "--forbid", "fallen  logs"], capsys)[0] == 0
    text = (out / "prompt-block.txt").read_text(encoding="ascii")
    assert "Actors painted into the plate: a sleeping cat." in text
    assert "Collectibles painted into the plate: yes" in text
    record = json.loads((out / "guide.json").read_text(encoding="utf-8"))
    assert "people, characters, creatures or silhouettes" not in record["forbiddenInWalk"]
    assert not any("loose items" in item for item in record["forbiddenInWalk"])
    assert record["forbiddenInWalk"][-1] == "fallen logs"
    assert "Forbidden on the walkable ground (anywhere)" in text
    out2 = tmp_path / "g2"
    assert run_main(GUIDE, ["--stage", stage_path, "--output-dir", out2, "--no-default-forbid", "--forbid", "carts"],
                    capsys)[0] == 0
    assert json.loads((out2 / "guide.json").read_text(encoding="utf-8"))["forbiddenInWalk"] == ["carts"]


def test_guide_warns_about_slots_a_reviewed_aspect_crops(tmp_path, capsys):
    stage_path = write_stage(tmp_path / "plan", make_stage(reviewedAspectRange=[1.3333, 3.5]), plate=False)
    code, summary, _ = run_main(GUIDE, ["--stage", stage_path, "--output-dir", tmp_path / "g"], capsys)
    assert code == 0 and summary["status"] == "warn"
    record = json.loads((tmp_path / "g" / "guide.json").read_text(encoding="utf-8"))
    cropped = checks_by_id(record["qa"])["slots always on screen"]
    assert cropped["status"] == "warn" and {item["slot"] for item in cropped["value"]} >= {"H2"}
    assert "Always on screen: x13%-88%, y25%-75%" in (tmp_path / "g" / "prompt-block.txt").read_text()


def test_inconsistent_plan_fails_and_strict_publishes_nothing(tmp_path, capsys):
    stage = make_stage()
    stage["slots"]["enemy"][0] = [0.12, 0.45]  # off the ground
    stage_path = write_stage(tmp_path / "plan", stage, plate=False)
    code, summary, _ = run_main(GUIDE, ["--stage", stage_path, "--output-dir", tmp_path / "g"], capsys)
    assert code == 1 and "slots stand on ground" in summary["failed"]
    assert (tmp_path / "g" / "guide.png").exists()
    code, summary, err = run_main(GUIDE, ["--stage", stage_path, "--output-dir", tmp_path / "strict", "--strict"],
                                  capsys)
    assert code == 1 and summary is None and "strict check failed" in err
    assert not (tmp_path / "strict").exists()
    assert not any(path.name.startswith(".strict") for path in tmp_path.iterdir())


# --------------------------------------------------------------------------- reference docs

REFERENCES = SKILLS_DIR / SKILL / "references"
DOCS = [REFERENCES / "hd2d-plates.md", REFERENCES / "hd2d-presentation.md"]


def test_docs_stage_example_is_a_clean_stage(tmp_path, capsys):
    text = DOCS[0].read_text(encoding="utf-8")
    example = json.loads(re.search(r"## 2\. The stage contract.*?```json\n(.*?)\n```", text, re.S).group(1))
    assert requested_errors(example, "stage_v1") == []
    stage = STAGE.parse_stage(example)
    statuses = {item["id"]: item["status"] for item in
                STAGE.static_checks(stage, foot_radius=STAGE.DEFAULT_FOOT_RADIUS, y_squash=STAGE.DEFAULT_Y_SQUASH)}
    assert set(statuses.values()) <= {"pass", "skipped"}, statuses
    stage_path = write_stage(tmp_path / "scene", example, plate=False)
    code, summary, err = run_main(STAGE, ["--stage", stage_path, "--output-dir", tmp_path / "qa"], capsys)
    assert code == 0 and summary["failed"] == [], err


def test_docs_use_single_line_commands_that_parse():
    """Every documented command is one line and its flags parse with the script's own argparse parser."""
    import shlex

    parsers = {f"{name}.py": load_script(SKILL, name).build_parser() for name in
               ("validate_stage", "scene_layout_guide", "extract_scene_lights", "edit_locality_check")}
    seen = set()
    for doc in DOCS:
        for line in doc.read_text(encoding="utf-8").splitlines():
            if 'python "<skill-dir>/scripts/' not in line:
                continue
            assert not line.rstrip().endswith("\\"), line
            words = shlex.split(line.strip())
            assert words[0] == "python"
            script = words[1].rsplit("/", 1)[-1]
            assert script in parsers, line
            parsers[script].parse_args(words[2:])  # SystemExit on an unknown or malformed flag
            seen.add(script)
    assert seen == set(parsers)


# --------------------------------------------------------------------------- standard CLI tests

@pytest.mark.parametrize("name", ["validate_stage", "scene_layout_guide"])
def test_help_is_ascii_under_legacy_consoles(name):
    assert_cli_help(SKILL, name)


@pytest.mark.parametrize("script", [STAGE_SCRIPT, GUIDE_SCRIPT])
def test_existing_output_is_refused(tmp_path, script):
    stage_path = write_stage(tmp_path / "scene", make_stage())
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    result = run_cli([script, "--stage", stage_path, "--output-dir", out])
    assert result.returncode == 1 and result.stderr.startswith("error: ") and "Refusing" in result.stderr
    assert result.stdout == "" and sorted(path.name for path in out.iterdir()) == ["keep.txt"]


def test_strict_failure_publishes_nothing(tmp_path):
    stage = make_stage()
    stage["slots"]["hero"][0] = [0.1, 0.45]  # in the pond
    stage_path = write_stage(tmp_path / "scene", stage)
    out = tmp_path / "out"
    result = run_cli([STAGE_SCRIPT, "--stage", stage_path, "--output-dir", out, "--strict"])
    assert result.returncode == 1 and "strict check failed" in result.stderr and result.stdout == ""
    assert not out.exists() and sorted(path.name for path in tmp_path.iterdir()) == ["scene"]
    line = run_cli([STAGE_SCRIPT, "--stage", stage_path, "--output-dir", tmp_path / "loose"])
    assert line.returncode == 1 and json.loads(line.stdout)["status"] == "fail"  # without --strict: report kept
    assert line.stdout.isascii() and len(line.stdout.strip().splitlines()) == 1
