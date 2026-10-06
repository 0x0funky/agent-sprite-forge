"""Tests for extract_scene_lights.py: light extraction, the light cookie and overlay, cookie re-baking and
atmosphere.v1 checks (B15-T3). Plates are synthetic night scenes with known lantern centres."""
from __future__ import annotations

import copy
import hashlib
import json
import re

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (FIXTURES_DIR, SKILLS_DIR, assert_cli_help, assert_valid_contract, contract_errors,
                             load_script, run_cli, script_path)

SKILL = "generate2dmap"
LIGHTS = load_script(SKILL, "extract_scene_lights")
SCRIPT = script_path(SKILL, "extract_scene_lights")
PRESENTATION = SKILLS_DIR / SKILL / "references" / "hd2d-presentation.md"

WIDTH, HEIGHT = 960, 540
LANTERNS = [(225.3, 158.4), (615.2, 270.6), (832.9, 142.7)]  # true glow centres in plate pixels
GLOW_RGB = (255, 176, 92)
COOL_DOT = (480.0, 90.0)


def requested_errors(document, name):
    """Errors against the vendored generate2dmap schemas, which hold this module's section 5 requests (D33)."""
    return contract_errors(document, "map", name, skill=SKILL)


# --------------------------------------------------------------------------- fixtures

def night_plate(lanterns=LANTERNS, width=WIDTH, height=HEIGHT, seed=7):
    """A night street: sky gradient with a warm sunset strip at the top, two dark buildings, a large warm
    window band, the lanterns (warm glow plus a near-white core), a cool specular dot and mild noise."""
    ys, xs = np.mgrid[0:height, 0:width] + 0.5
    t = (ys / height)[..., None]
    image = np.array([46.0, 62, 108]) * (1 - t) + np.array([22.0, 26, 44]) * t
    sx, sy = width / 1280, height / 720
    image[int(300 * sy):int(520 * sy), int(80 * sx):int(420 * sx)] = (34, 36, 46)
    image[int(280 * sy):int(540 * sy), int(700 * sx):int(1000 * sx)] = (30, 33, 44)
    image[int(400 * sy):int(460 * sy), int(500 * sx):int(700 * sx)] = (205, 150, 90)
    sunset = np.clip(1 - ys / (50 * sy), 0, 1)[..., None]
    image = image * (1 - sunset) + np.array([250.0, 170, 110]) * sunset

    def glow(cx, cy, sigma, rgb, amplitude):
        return np.array(rgb) * amplitude * np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * sigma ** 2))[..., None]

    for cx, cy in lanterns:
        image = image + glow(cx, cy, 7.0 * sx, GLOW_RGB, 0.85) + glow(cx, cy, 2.2 * sx, (255, 236, 200), 0.9)
    image = image + glow(*COOL_DOT, 2.5 * sx, (225, 238, 255), 1.0)
    image = image + np.random.default_rng(seed).normal(0, 2.0, image.shape)
    return Image.fromarray(np.clip(np.floor(image + 0.5), 0, 255).astype(np.uint8))


@pytest.fixture(scope="module")
def plate_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("plate") / "night.png"
    night_plate().save(path)
    return path


def run_main(argv, capsys):
    code = LIGHTS.main([str(item) for item in argv])
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None), captured.err


def nearest(point, targets):
    return min(np.hypot(point[0] - x, point[1] - y) for x, y in targets)


# --------------------------------------------------------------------------- extract

def test_three_lanterns_found_as_three_lights_within_4px(plate_path, tmp_path, capsys):
    out = tmp_path / "lights"
    code, summary, err = run_main(["extract", "--plate", plate_path, "--output-dir", out, "--expect", "3"], capsys)
    assert code == 0, err
    assert summary["lights"] == 3 and summary["status"] == "needs-visual-review" and summary["failed"] == []
    document = json.loads((out / "lights.json").read_text(encoding="utf-8"))
    assert len(document["lights"]) == 3
    found = [(light["u"] * WIDTH, light["v"] * HEIGHT) for light in document["lights"]]
    for point in found:
        assert nearest(point, LANTERNS) <= 4.0
    for lantern in LANTERNS:  # one light per lantern
        assert nearest(lantern, found) <= 4.0
    assert [light["id"] for light in document["lights"]] == ["L1", "L2", "L3"]
    assert [light["v"] for light in document["lights"]] == sorted(light["v"] for light in document["lights"])
    assert document["sourceSize"] == [WIDTH, HEIGHT] and document["cookie"] == "light-cookie.png"
    assert_valid_contract(document, "map", "lights_v1", skill=SKILL)
    assert requested_errors(document, "lights_v1") == []
    report = json.loads((out / "lights-qa.json").read_text(encoding="utf-8"))
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
    assert requested_errors(report, "lights_qa_v1") == []
    band_x, band_y = (375, 525), (300, 345)  # the large warm window band is never a light
    assert not any(band_x[0] <= x <= band_x[1] and band_y[0] <= y <= band_y[1] for x, y in found)
    with Image.open(out / "light-cookie.png") as cookie, Image.open(out / "lights-overlay.png") as overlay:
        assert cookie.size == (512, 288) and cookie.mode == "RGB" and overlay.size == (WIDTH, HEIGHT)


def test_light_colour_is_the_glow_hue_not_the_white_core_or_the_sky(plate_path, tmp_path, capsys):
    assert run_main(["extract", "--plate", plate_path, "--output-dir", tmp_path / "l"], capsys)[0] == 0
    for light in json.loads((tmp_path / "l" / "lights.json").read_text(encoding="utf-8"))["lights"]:
        rgb = [int(light["color"][i:i + 2], 16) for i in (1, 3, 5)]
        assert all(abs(a - b) <= 12 for a, b in zip(rgb, GLOW_RGB)), light["color"]
        assert 0 < light["radius"] <= 0.15 and 0 < light["strength"] <= 1


def test_distractors_need_another_hue_setting(plate_path, tmp_path, capsys):
    assert run_main(["extract", "--plate", plate_path, "--output-dir", tmp_path / "any", "--color", "any"],
                    capsys)[0] == 0
    found = [(light["u"] * WIDTH, light["v"] * HEIGHT)
             for light in json.loads((tmp_path / "any" / "lights.json").read_text(encoding="utf-8"))["lights"]]
    assert len(found) == 4 and nearest(COOL_DOT, found) <= 4.0  # the cool dot joins; the window band never does
    assert run_main(["extract", "--plate", plate_path, "--output-dir", tmp_path / "cool", "--color", "cool"],
                    capsys)[0] == 0
    cool = json.loads((tmp_path / "cool" / "lights.json").read_text(encoding="utf-8"))["lights"]
    assert len(cool) == 1 and nearest((cool[0]["u"] * WIDTH, cool[0]["v"] * HEIGHT), [COOL_DOT]) <= 4.0


def _add_glow(path, out, cx, cy, layers):
    """Copy a plate with extra glows: layers of (sigma, rgb, amplitude) centred on (cx, cy)."""
    pixels = np.asarray(Image.open(path), float)
    ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH] + 0.5
    for sigma, rgb, amplitude in layers:
        pixels = pixels + np.array(rgb) * amplitude * np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2)
                                                             / (2 * sigma ** 2))[..., None]
    Image.fromarray(np.clip(np.floor(pixels + 0.5), 0, 255).astype(np.uint8)).save(out)
    return out


def test_broad_bright_areas_are_not_lights(plate_path, tmp_path, capsys):
    """A sunlit panel peaks along its edges, but the half-peak region of those peaks runs round the whole panel,
    wider than --max-size: no light is reported on it, while a lantern beside it is measured against its own
    peak and found."""
    pixels = np.asarray(Image.open(plate_path)).copy()
    pixels[380:470, 220:360] = (255, 214, 150)
    Image.fromarray(pixels).save(tmp_path / "panel.png")
    plate = _add_glow(tmp_path / "panel.png", tmp_path / "panel-lamp.png", 400.0, 420.0,
                      [(5.0, GLOW_RGB, 0.85), (1.6, (255, 236, 200), 0.9)])
    code, summary, err = run_main(["extract", "--plate", plate, "--output-dir", tmp_path / "l", "--expect", "4"],
                                  capsys)
    assert code == 0, err
    report = json.loads((tmp_path / "l" / "lights-qa.json").read_text(encoding="utf-8"))
    assert report["detector"]["tooLarge"] >= 1
    found = [(light["u"] * WIDTH, light["v"] * HEIGHT) for light in report["lights"]]
    assert nearest((400.0, 420.0), found) <= 4.0
    assert not any(215 <= x <= 365 and 375 <= y <= 475 for x, y in found)


def test_a_blob_whose_glow_is_not_warm_is_rejected(plate_path, tmp_path, capsys):
    """A warm pin-point inside a broad cool glow (a lit banner, a sky gap) has warm core pixels, but the light it
    adds is cool: it is rejected in warm mode and reported."""
    plate = _add_glow(plate_path, tmp_path / "banner.png", 150.0, 470.0,
                      [(4.0, (255, 190, 90), 1.0), (14.0, (90, 140, 255), 0.35)])
    assert run_main(["extract", "--plate", plate, "--output-dir", tmp_path / "l", "--expect", "3"], capsys)[0] == 0
    report = json.loads((tmp_path / "l" / "lights-qa.json").read_text(encoding="utf-8"))
    assert report["detector"]["offHue"] >= 1
    banner = [item for item in report["rejected"] if item["reason"] == "glow hue"]
    assert any(nearest((item["u"] * WIDTH, item["v"] * HEIGHT), [(150.0, 470.0)]) <= 4.0 for item in banner)
    assert requested_errors(report, "lights_qa_v1") == []


def test_stage_ground_and_water_reject_reflections(plate_path, tmp_path, capsys):
    """The plate contract keeps lamps off the walkable ground: a warm glint on the stage's floor is a reflection."""
    plate = _add_glow(plate_path, tmp_path / "glint.png", 480.0, 470.0,
                      [(5.0, GLOW_RGB, 0.85), (1.6, (255, 236, 200), 0.9)])
    stage = {"schema": "generate2dmap.stage.v1", "sourceSize": [WIDTH, HEIGHT], "fit": "cover",
             "reviewedAspectRange": [1.3333, 2.3333],
             "groundPolygons": [[[0.05, 0.8], [0.95, 0.8], [0.95, 0.98], [0.05, 0.98]]],
             "slots": {"hero": [[0.7, 0.9]], "enemy": [[0.3, 0.9]]}}
    stage_path = write_json(tmp_path / "stage.json", stage)
    assert run_main(["extract", "--plate", plate, "--output-dir", tmp_path / "plain"], capsys)[1]["lights"] == 4
    code, summary, err = run_main(["extract", "--plate", plate, "--output-dir", tmp_path / "staged", "--stage",
                                   stage_path, "--expect", "3"], capsys)
    assert code == 0 and summary["lights"] == 3, err
    report = json.loads((tmp_path / "staged" / "lights-qa.json").read_text(encoding="utf-8"))
    assert report["detector"]["onGroundOrWater"] == 1 and report["detector"]["groundAndWaterExcluded"] is True
    glint = [item for item in report["rejected"] if item["reason"] == "on ground or water"]
    assert len(glint) == 1 and nearest((glint[0]["u"] * WIDTH, glint[0]["v"] * HEIGHT), [(480.0, 470.0)]) <= 4.0
    checks = {item["id"]: item["status"] for item in report["checks"]}
    assert checks["plate matches the stage's sourceSize"] == "pass"
    assert report["inputs"][-1]["path"] == "../stage.json"
    assert run_main(["extract", "--plate", plate, "--output-dir", tmp_path / "kept", "--stage", stage_path,
                     "--keep-floor"], capsys)[1]["lights"] == 4
    code, _, err = run_main(["extract", "--plate", plate, "--output-dir", tmp_path / "x", "--keep-floor"], capsys)
    assert code == 1 and "--keep-floor needs --stage" in err


def test_expect_mismatch_fails_and_strict_publishes_nothing(plate_path, tmp_path, capsys):
    code, summary, _ = run_main(["extract", "--plate", plate_path, "--output-dir", tmp_path / "l", "--expect", "4"],
                                capsys)
    assert code == 1 and summary["failed"] == ["lights found"] and (tmp_path / "l" / "lights.json").exists()
    code, summary, err = run_main(["extract", "--plate", plate_path, "--output-dir", tmp_path / "s", "--expect", "4",
                                   "--strict"], capsys)
    assert code == 1 and summary is None and "strict check failed (lights found)" in err
    assert not (tmp_path / "s").exists()


def test_cookie_pools_sit_on_the_lights_and_bytes_repeat(plate_path, tmp_path, capsys):
    for name in ("a", "b"):
        assert run_main(["extract", "--plate", plate_path, "--output-dir", tmp_path / name], capsys)[0] == 0
    for name in ("lights.json", "light-cookie.png", "lights-overlay.png", "lights-qa.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    document = json.loads((tmp_path / "a" / "lights.json").read_text(encoding="utf-8"))
    cookie = np.asarray(Image.open(tmp_path / "a" / "light-cookie.png"))
    for light in document["lights"]:
        centre = cookie[int(light["v"] * 288), int(light["u"] * 512)].astype(int)
        target = [int(light["color"][i:i + 2], 16) for i in (1, 3, 5)]
        assert np.abs(centre - target).max() <= 3
    assert tuple(cookie[287, 0]) == (0x24, 0x30, 0x44) and document["ambient"] == "#243044"
    assert hashlib.sha256((tmp_path / "a" / "light-cookie.png").read_bytes()).hexdigest() == document["cookieSha256"]


def test_cookie_pools_are_round_on_the_plate():
    """A 4:3 plate in a 16:9 cookie: the pool is an ellipse in the cookie, a circle on the plate."""
    light = {"u": 0.5, "v": 0.5, "color": "#ffffff", "radius": 0.1}
    cookie = np.asarray(LIGHTS.render_cookie([light], (800, 600), (512, 288), (0, 0, 0)))[..., 0]
    lit = cookie > 0
    width = lit[144].sum() / 512 * 800   # horizontal extent in plate pixels
    height = lit[:, 256].sum() / 288 * 600
    assert abs(width - height) <= 6 and abs(width - 160) <= 6  # diameter 2 * 0.1 * 800 px


def test_flicker_phases_are_staggered_and_fast_flicker_warns(plate_path, tmp_path, capsys):
    assert run_main(["extract", "--plate", plate_path, "--output-dir", tmp_path / "slow", "--flicker", "0.25:0.15"],
                    capsys)[0] == 0
    lights = json.loads((tmp_path / "slow" / "lights.json").read_text(encoding="utf-8"))["lights"]
    phases = [light["flicker"]["phase"] for light in lights]
    assert len(set(phases)) == 3 and all(light["flicker"]["hz"] == 0.25 for light in lights)
    code, summary, _ = run_main(["extract", "--plate", plate_path, "--output-dir", tmp_path / "fast",
                                 "--flicker", "5:0.5"], capsys)
    assert code == 0 and summary["status"] == "warn"
    checks = {item["id"]: item for item in
              json.loads((tmp_path / "fast" / "lights-qa.json").read_text(encoding="utf-8"))["checks"]}
    assert checks["flicker rate"]["status"] == "warn"


def test_cookie_verb_rebakes_edited_lights(plate_path, tmp_path, capsys):
    assert run_main(["extract", "--plate", plate_path, "--output-dir", tmp_path / "first"], capsys)[0] == 0
    document = json.loads((tmp_path / "first" / "lights.json").read_text(encoding="utf-8"))
    edited = copy.deepcopy(document)
    edited["lights"] = edited["lights"][:2] + [{"u": 0.5, "v": 0.9, "color": [255, 200, 120], "radius": 0.05}]
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(edited), encoding="utf-8")
    code, summary, err = run_main(["cookie", "--lights", path, "--plate", plate_path, "--output-dir",
                                   tmp_path / "second"], capsys)
    assert code == 0, err
    rebaked = json.loads((tmp_path / "second" / "lights.json").read_text(encoding="utf-8"))
    assert [light["id"] for light in rebaked["lights"]] == ["L1", "L2", "L3"]
    assert rebaked["lights"][2]["color"] == "#ffc878" and rebaked["lights"][2]["strength"] == 1.0
    assert rebaked["cookieSha256"] != document["cookieSha256"]
    cookie = np.asarray(Image.open(tmp_path / "second" / "light-cookie.png"))
    assert tuple(cookie[int(0.9 * 288), 256]) == (255, 200, 120)
    assert requested_errors(rebaked, "lights_v1") == []
    report = json.loads((tmp_path / "second" / "lights-qa.json").read_text(encoding="utf-8"))
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
    assert requested_errors(report, "lights_qa_v1") == []
    edited_ref, plate_ref = report["inputs"]
    assert edited_ref["path"] == "../edited.json"
    assert plate_ref["path"].endswith("/night.png")
    assert plate_ref["sha256"] == hashlib.sha256(plate_path.read_bytes()).hexdigest()
    assert (tmp_path / "second" / "lights-overlay.png").exists() and "overlay" in summary

    edited["lights"][0]["u"] = 1.5
    path.write_text(json.dumps(edited), encoding="utf-8")
    code, summary, err = run_main(["cookie", "--lights", path, "--output-dir", tmp_path / "bad"], capsys)
    assert code == 1 and summary is None and "$.lights[0].u must be a number in 0..1" in err
    assert not (tmp_path / "bad").exists()
    bare = {"schema": "generate2dmap.lights.v1", "lights": [], "cookie": "light-cookie.png"}
    path.write_text(json.dumps(bare), encoding="utf-8")
    code, _, err = run_main(["cookie", "--lights", path, "--output-dir", tmp_path / "nosize"], capsys)
    assert code == 1 and "records no sourceSize; pass --plate" in err


def test_parse_lights_reads_the_contract_fixture():
    document = json.loads((FIXTURES_DIR / "contracts" / "map.lights_v1.valid.json").read_text(encoding="utf-8"))
    lights, size = LIGHTS.parse_lights(document)
    assert size is None and lights[0]["id"] == "L1" and lights[0]["flicker"] == {"hz": 3, "depth": 0.2}
    for change in ({"u": 1.5}, {"radius": 0}, {"color": "orange"}, {"flicker": {"depth": 0.2}}):
        broken = copy.deepcopy(document)
        broken["lights"][0].update(change)
        with pytest.raises(ValueError):
            LIGHTS.parse_lights(broken)


# --------------------------------------------------------------------------- atmosphere

def write_json(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def docs_atmosphere():
    text = PRESENTATION.read_text(encoding="utf-8")
    return json.loads(re.search(r"## 6\. Atmosphere\s+```json\n(.*?)\n```", text, re.S).group(1))


def test_atmosphere_examples_pass(tmp_path, capsys):
    for index, document in enumerate([
            docs_atmosphere(),
            json.loads((FIXTURES_DIR / "contracts" / "map.atmosphere_v1.valid.json").read_text(encoding="utf-8"))]):
        assert_valid_contract(document, "map", "atmosphere_v1", skill=SKILL)
        out = tmp_path / f"qa{index}"
        code, summary, err = run_main(["atmosphere", "--atmosphere", write_json(tmp_path / f"a{index}.json", document),
                                       "--output-dir", out], capsys)
        assert code == 0 and summary["status"] == "pass", err
        report = json.loads((out / "atmosphere-qa.json").read_text(encoding="utf-8"))
        assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
        assert requested_errors(report, "atmosphere_qa_v1") == []
    assert report["summary"] == {"moteLayers": 1, "motes": 40, "mobileMotes": 40, "shafts": 0, "mist": True}


@pytest.mark.parametrize("change, check_id, status", [
    (lambda d: d["motes"][0].update(opacity=1.5), "mote layers are valid", "fail"),
    (lambda d: d["motes"][0].update(mobile=500), "mote layers are valid", "fail"),
    (lambda d: d["motes"][0].update(colors=["orange"]), "mote layers are valid", "fail"),
    (lambda d: d["motes"][0].update(blend="multiply"), "mote layers are valid", "fail"),
    (lambda d: d["grade"].update(vignette=1.2), "grade", "fail"),
    (lambda d: d["grade"]["splitTone"].update(shadows="#12"), "grade", "fail"),
    (lambda d: d["mist"].update(opacity=2), "mist", "fail"),
    (lambda d: d["shafts"][0].update(spots=[[1, "x"]]), "light shafts", "fail"),
    (lambda d: d["motes"][0].update(count=600, mobile=10), "mote budget", "warn"),
    (lambda d: d["grade"].update(bloom=1.4), "grade", "warn"),
    (lambda d: d["grade"].update(saturation=1.8), "grade", "warn"),
    (lambda d: d["mist"].update(opacity=0.5), "mist", "warn"),
    (lambda d: d["shafts"][0].update(opacity=0.4), "light shafts", "warn"),
])
def test_atmosphere_values_fail_or_warn(tmp_path, capsys, change, check_id, status):
    document = docs_atmosphere()
    change(document)
    out = tmp_path / "qa"
    code, summary, _ = run_main(["atmosphere", "--atmosphere", write_json(tmp_path / "a.json", document),
                                 "--output-dir", out], capsys)
    assert summary["status"] == status and code == (1 if status == "fail" else 0)
    report = json.loads((out / "atmosphere-qa.json").read_text(encoding="utf-8"))
    checks = {item["id"]: item for item in report["checks"]}
    assert checks[check_id]["status"] == status
    assert [item["id"] for item in checks.values() if item["status"] == status] == [check_id]


def test_atmosphere_structure_errors(tmp_path, capsys):
    for index, (mutate, message) in enumerate([
            (lambda d: d.pop("grade"), "$.grade must be an object"),
            (lambda d: d.update(schema="generate2dmap.atmosphere.v2"), "$.schema must be"),
            (lambda d: d.update(mist=[1]), "$.mist must be an object or null"),
            (lambda d: d["motes"].append(3), "$.motes[1] must be an object")]):
        document = docs_atmosphere()
        mutate(document)
        out = tmp_path / f"qa{index}"
        code, summary, err = run_main(["atmosphere", "--atmosphere", write_json(tmp_path / f"a{index}.json", document),
                                       "--output-dir", out], capsys)
        assert code == 1 and summary is None and message in err and not out.exists()


# --------------------------------------------------------------------------- standard CLI tests

def test_help_is_ascii_under_legacy_consoles():
    assert_cli_help(SKILL, "extract_scene_lights")
    for verb in ("extract", "cookie", "atmosphere"):
        for encoding in ("cp1252", "cp950"):
            result = run_cli([SCRIPT, verb, "--help"], encoding)
            assert result.returncode == 0 and result.stdout.isascii() and "--output-dir" in result.stdout


def test_existing_output_is_refused(plate_path, tmp_path):
    atmosphere = write_json(tmp_path / "a.json", docs_atmosphere())
    lights = write_json(tmp_path / "l.json", {"schema": "generate2dmap.lights.v1", "sourceSize": [16, 9],
                                              "lights": [], "cookie": "light-cookie.png"})
    for verb, extra in (("extract", ["--plate", plate_path]), ("cookie", ["--lights", lights]),
                        ("atmosphere", ["--atmosphere", atmosphere])):
        out = tmp_path / f"out-{verb}"
        out.mkdir()
        (out / "keep.txt").write_text("mine", encoding="utf-8")
        result = run_cli([SCRIPT, verb, *extra, "--output-dir", out])
        assert result.returncode == 1 and result.stderr.startswith("error: ") and "Refusing" in result.stderr
        assert result.stdout == "" and sorted(path.name for path in out.iterdir()) == ["keep.txt"]


def test_strict_failure_publishes_nothing(plate_path, tmp_path):
    bad_atmosphere = docs_atmosphere()
    bad_atmosphere["grade"]["vignette"] = 3
    atmosphere = write_json(tmp_path / "a.json", bad_atmosphere)
    lights = write_json(tmp_path / "l.json", {"schema": "generate2dmap.lights.v1", "sourceSize": [WIDTH, HEIGHT],
                                              "lights": [], "cookie": "light-cookie.png"})
    small_plate = tmp_path / "small.png"
    night_plate(width=640, height=360).save(small_plate)  # another size than the lights file records
    for verb, extra in (("extract", ["--plate", plate_path, "--expect", "2"]),
                        ("cookie", ["--lights", lights, "--plate", small_plate]),
                        ("atmosphere", ["--atmosphere", atmosphere])):
        out = tmp_path / f"out-{verb}"
        result = run_cli([SCRIPT, verb, *extra, "--output-dir", out, "--strict"])
        assert result.returncode == 1 and "strict check failed" in result.stderr and result.stdout == ""
        assert not out.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.json", "l.json", "small.png"]
