"""Tests for edit_locality_check.py: plate variants may change only where allowed (B15-T4). The plate is a
synthetic texture with a ground band; variants are local glows, band edits, relighting and resizes."""
from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

from forge_testutils import assert_cli_help, assert_valid_contract, contract_errors, load_script, run_cli, script_path

SKILL = "generate2dmap"
LOCALITY = load_script(SKILL, "edit_locality_check")
SCRIPT = script_path(SKILL, "edit_locality_check")
WIDTH, HEIGHT = 640, 360
BAND = "0.62:1"
BAND_ID = "protected band y0.62-1 unchanged"
EDIT_BOX = "0.6,0.2,0.75,0.4"


def requested_errors(document, name):
    """Errors against the vendored generate2dmap schemas, which hold this module's section 5 requests (D33)."""
    return contract_errors(document, "map", name, skill=SKILL)


# --------------------------------------------------------------------------- fixtures

_YS, _XS = np.mgrid[0:HEIGHT, 0:WIDTH] + 0.5


def master():
    """Smooth coloured texture with noise, a paler ground band from row 220 (v 0.611) and a light landmark."""
    rng = np.random.default_rng(3)
    base = np.stack([90 + 60 * np.sin(_XS / 23.0) * np.cos(_YS / 17.0), 110 + 40 * np.sin(_XS / 41.0 + _YS / 29.0),
                     80 + 50 * np.cos(_XS / 13.0 - _YS / 37.0)], -1)
    base += rng.normal(0, 6, base.shape)
    base[220:] = base[220:] * 0.6 + np.array([150, 140, 120]) * 0.4
    base[60:120, 400:470] = (200, 190, 170)
    return np.clip(np.floor(base + 0.5), 0, 255).astype(np.uint8)


def glow(pixels, cx, cy, sigma=9.0, amount=0.9):
    halo = np.exp(-((_XS - cx) ** 2 + (_YS - cy) ** 2) / (2 * sigma ** 2))[..., None]
    return np.clip(np.floor(pixels + np.array([255.0, 180, 90]) * amount * halo + 0.5), 0, 255).astype(np.uint8)


def save(path, pixels):
    Image.fromarray(pixels).save(path)
    return path


@pytest.fixture()
def plates(tmp_path):
    before = master()
    return {
        "before": save(tmp_path / "before.png", before),
        "local": save(tmp_path / "local.png", glow(before, 420.0, 110.0)),  # inside the edit box
        "band": save(tmp_path / "band.png", glow(before, 300.0, 300.0)),  # in the protected ground band
        "dark": save(tmp_path / "dark.png", np.floor(before * 0.92 + 0.5).astype(np.uint8)),
    }


def run_main(argv, capsys):
    code = LOCALITY.main([str(item) for item in argv])
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None), captured.err


def checks_of(folder):
    report = json.loads((folder / "locality-qa.json").read_text(encoding="utf-8"))
    return report, {item["id"]: item for item in report["checks"]}


# --------------------------------------------------------------------------- acceptance

def test_local_edit_passes(plates, tmp_path, capsys):
    out = tmp_path / "check"
    code, summary, err = run_main(["--before", plates["before"], "--after", plates["local"], "--output-dir", out,
                                   "--protect-band", BAND, "--edit-box", EDIT_BOX], capsys)
    assert code == 0 and summary["status"] == "pass" and summary["failed"] == [], err
    report, checks = checks_of(out)
    assert checks[BAND_ID]["value"]["changedPx"] == 0 and checks[BAND_ID]["value"]["mae"] == 0
    assert checks["outside the edit unchanged"]["status"] == "pass"
    assert checks["edit is visible"]["status"] == "pass" and checks["edit is visible"]["value"]["changedPx"] > 100
    assert report["transform"]["kind"] == "identity" and report["comparison"]["space"] == "before"
    assert_valid_contract(report, "common", "qaEnvelope", skill=SKILL)
    assert requested_errors(report, "edit_locality_v1") == []
    assert [ref["path"] for ref in report["inputs"]] == ["../before.png", "../local.png"]
    with Image.open(out / "locality-diff.png") as diff:
        assert diff.size == (WIDTH, HEIGHT)


def test_edit_in_the_protected_band_fails(plates, tmp_path, capsys):
    out = tmp_path / "check"
    code, summary, _ = run_main(["--before", plates["before"], "--after", plates["band"], "--output-dir", out,
                                 "--protect-band", BAND, "--edit-box", EDIT_BOX], capsys)
    assert code == 1 and summary["status"] == "fail" and BAND_ID in summary["failed"]
    _, checks = checks_of(out)
    band = checks[BAND_ID]["value"]
    assert band["changedPx"] > 0 and band["largestComponentPx"] >= band["minComponentPx"]
    u0, v0, u1, v1 = band["components"][0]["uv"]
    assert u0 < 300 / WIDTH < u1 and v0 < 300 / HEIGHT < v1  # the change is found where it was made
    assert checks["edit is visible"]["status"] == "warn"
    diff = np.asarray(Image.open(out / "locality-diff.png").convert("RGB"))
    assert tuple(diff[300, 300]) == (255, 40, 40) or diff[300, 300, 0] > 200 > diff[300, 300, 1]


# --------------------------------------------------------------------------- transforms

def test_a_resize_must_be_recorded(plates, tmp_path, capsys):
    small = save(tmp_path / "small.png", np.asarray(Image.open(plates["local"]).resize((480, 270))))
    out = tmp_path / "check"
    code, summary, err = run_main(["--before", plates["before"], "--after", small, "--output-dir", out,
                                   "--protect-band", BAND], capsys)
    assert code == 1 and summary is None and "record how it was resized" in err and not out.exists()


def test_downscaled_variant_passes_through_a_stretch(plates, tmp_path, capsys):
    """The generator resized with bicubic; the check resamples the master with Lanczos: noise stays tiny."""
    small = Image.open(plates["local"]).resize((480, 270), Image.Resampling.BICUBIC)
    path = save(tmp_path / "small.png", np.asarray(small))
    out = tmp_path / "check"
    code, summary, err = run_main(["--before", plates["before"], "--after", path, "--output-dir", out,
                                   "--resize", "stretch", "--protect-band", BAND, "--edit-box", EDIT_BOX], capsys)
    assert code == 0 and summary["status"] == "pass", err
    report, checks = checks_of(out)
    assert report["comparison"] == {"space": "after", "size": [480, 270], "blur": 1, "pixelThreshold": 24.0,
                                    "exact": False}
    assert checks[BAND_ID]["value"]["mae"] < 1.0 and checks[BAND_ID]["value"]["changedPx"] == 0
    band_edit = Image.open(plates["band"]).resize((480, 270), Image.Resampling.BICUBIC)
    path = save(tmp_path / "small-band.png", np.asarray(band_edit))
    code, summary, _ = run_main(["--before", plates["before"], "--after", path, "--output-dir", tmp_path / "c2",
                                 "--resize", "stretch", "--protect-band", BAND], capsys)
    assert code == 1 and summary["failed"] == [BAND_ID]


def test_cover_crop_through_a_conform_file_or_resize_cover(plates, tmp_path, capsys):
    """A 4:3 variant cut from the middle of the 16:9 master (what conform_background.py cover records)."""
    for name, source in (("local", plates["local"]), ("band", plates["band"])):
        crop = Image.open(source).crop((80, 0, 560, 360)).resize((400, 300), Image.Resampling.LANCZOS)
        save(tmp_path / f"cover-{name}.png", np.asarray(crop))
    conform = tmp_path / "conform.json"
    conform.write_text(json.dumps({"schema": "generate2dmap.conform.v1", "mode": "cover",
                                   "transform": {"scale": 400 / 480, "src_rect": [80, 0, 560, 360],
                                                 "out_size": [400, 300], "resampler": "lanczos"}}), encoding="utf-8")
    common = ["--protect-band", BAND, "--edit-box", EDIT_BOX]
    code, summary, err = run_main(["--before", plates["before"], "--after", tmp_path / "cover-local.png",
                                   "--output-dir", tmp_path / "c1", "--conform", conform, *common], capsys)
    assert code == 0 and summary["status"] == "pass", err
    report, _ = checks_of(tmp_path / "c1")
    assert report["transform"]["kind"] == "conform" and report["transform"]["src_rect"] == [80, 0, 560, 360]
    assert report["inputs"][-1]["path"] == "../conform.json"
    code, summary, _ = run_main(["--before", plates["before"], "--after", tmp_path / "cover-local.png",
                                 "--output-dir", tmp_path / "c2", "--resize", "cover", *common], capsys)
    assert code == 0 and summary["status"] == "pass"
    code, summary, _ = run_main(["--before", plates["before"], "--after", tmp_path / "cover-band.png",
                                 "--output-dir", tmp_path / "c3", "--conform", conform, "--protect-band", BAND],
                                capsys)
    assert code == 1 and summary["failed"] == [BAND_ID]


def test_upscaled_variant_is_compared_in_master_space(plates, tmp_path, capsys):
    big = Image.open(plates["local"]).resize((1280, 720), Image.Resampling.LANCZOS)
    path = save(tmp_path / "big.png", np.asarray(big))
    code, summary, err = run_main(["--before", plates["before"], "--after", path, "--output-dir", tmp_path / "c",
                                   "--resize", "stretch", "--protect-band", BAND, "--edit-box", EDIT_BOX], capsys)
    assert code == 0 and summary["status"] == "pass", err
    report, _ = checks_of(tmp_path / "c")
    assert report["comparison"]["space"] == "before" and report["comparison"]["size"] == [WIDTH, HEIGHT]


def test_bad_conform_files_are_errors(plates, tmp_path, capsys):
    small = save(tmp_path / "small.png", np.asarray(Image.open(plates["before"]).resize((480, 270))))
    cases = [({"transform": {"scale": 0.75, "src_rect": [0, 0, 640, 360], "out_size": [400, 300]}},
              "maps onto 400x300"),
             ({"scale": 0.75, "src_rect": [-50, 0, 640, 360], "out_size": [480, 270]}, "src_rect leaves"),
             ({"scale": 0.5, "src_rect": [0, 0, 640, 360], "out_size": [480, 270]}, "does not give out_size"),
             ({"transform": {"scale": 0.75}}, "needs a transform with scale, src_rect and out_size")]
    for index, (document, message) in enumerate(cases):
        conform = tmp_path / f"conform{index}.json"
        conform.write_text(json.dumps(document), encoding="utf-8")
        code, summary, err = run_main(["--before", plates["before"], "--after", small, "--output-dir",
                                       tmp_path / f"c{index}", "--conform", conform, "--protect-band", BAND], capsys)
        assert code == 1 and summary is None and message in err, err
    code, _, err = run_main(["--before", plates["before"], "--after", small, "--output-dir", tmp_path / "both",
                             "--conform", conform, "--resize", "cover", "--protect-band", BAND], capsys)
    assert code == 1 and "not both" in err


# --------------------------------------------------------------------------- what counts as a change

def test_global_relighting_fails_on_mean_difference(plates, tmp_path, capsys):
    code, summary, _ = run_main(["--before", plates["before"], "--after", plates["dark"], "--output-dir",
                                 tmp_path / "c", "--protect-band", BAND, "--edit-box", EDIT_BOX], capsys)
    assert code == 1 and set(summary["failed"]) == {BAND_ID, "outside the edit unchanged"}
    _, checks = checks_of(tmp_path / "c")
    band = checks[BAND_ID]["value"]
    assert band["changedPx"] == 0 and band["mae"] > 3.0  # no pixel moved 24 levels, the whole band moved ~9


def test_unrelated_change_outside_the_edit_fails(plates, tmp_path, capsys):
    pixels = np.asarray(Image.open(plates["local"])).copy()
    pixels[30:44, 60:74] = (20, 20, 30)  # a lamp that went out, far from the edit and above the band
    path = save(tmp_path / "lamp.png", pixels)
    code, summary, _ = run_main(["--before", plates["before"], "--after", path, "--output-dir", tmp_path / "c",
                                 "--protect-band", BAND, "--edit-box", EDIT_BOX], capsys)
    assert code == 1 and summary["failed"] == ["outside the edit unchanged"]
    _, checks = checks_of(tmp_path / "c")
    outside = checks["outside the edit unchanged"]["value"]
    assert outside["changedFraction"] < 0.002  # too small for the fraction rule; the component rule catches it
    x0, y0, x1, y1 = outside["components"][0]["box"]
    assert x0 <= 61 and y0 <= 31 and x1 >= 73 and y1 >= 43


def test_without_an_edit_box_changes_need_a_look(plates, tmp_path, capsys):
    code, summary, _ = run_main(["--before", plates["before"], "--after", plates["local"], "--output-dir",
                                 tmp_path / "c", "--protect-band", BAND], capsys)
    assert code == 0 and summary["status"] == "needs-visual-review"
    _, checks = checks_of(tmp_path / "c")
    assert checks["changes overall"]["value"]["components"][0]["areaPx"] > 100


def test_exact_mode_fails_on_one_changed_byte(plates, tmp_path, capsys):
    pixels = np.asarray(Image.open(plates["before"])).copy()
    pixels[300, 100, 0] += 1
    path = save(tmp_path / "one.png", pixels)
    assert run_main(["--before", plates["before"], "--after", path, "--output-dir", tmp_path / "loose",
                     "--protect-band", BAND], capsys)[0] == 0
    code, summary, _ = run_main(["--before", plates["before"], "--after", path, "--output-dir", tmp_path / "exact",
                                 "--protect-band", BAND, "--exact"], capsys)
    assert code == 1 and summary["failed"] == [BAND_ID]
    _, checks = checks_of(tmp_path / "exact")
    assert checks[BAND_ID]["value"]["changedPx"] == 1 and checks[BAND_ID]["threshold"] == {"exact": True,
                                                                                         "changedPx": 0}
    small = save(tmp_path / "small.png", np.asarray(Image.open(plates["before"]).resize((480, 270))))
    code, _, err = run_main(["--before", plates["before"], "--after", small, "--output-dir", tmp_path / "x",
                             "--resize", "stretch", "--protect-band", BAND, "--exact"], capsys)
    assert code == 1 and "needs two images of the same size" in err


# --------------------------------------------------------------------------- stage regions

def stage_file(tmp_path, regions):
    stage = {"schema": "generate2dmap.stage.v1", "sourceSize": [WIDTH, HEIGHT], "fit": "cover",
             "reviewedAspectRange": [1.3333, 2.3333],
             "groundPolygons": [[[0.02, 0.64], [0.98, 0.64], [0.98, 0.98], [0.02, 0.98]]],
             "playableBand": [0.64, 0.98], "slots": {"hero": [[0.7, 0.8]], "enemy": [[0.3, 0.8]]},
             "protectedRegions": regions}
    path = tmp_path / "stage.json"
    path.write_text(json.dumps(stage), encoding="utf-8")
    return path


def test_stage_regions_that_apply_to_edits_are_protected(plates, tmp_path, capsys):
    landmark = save(tmp_path / "landmark.png", glow(master(), 435.0, 90.0, sigma=6.0))  # on the landmark box
    regions = [{"id": "landmark", "box": [0.62, 0.16, 0.74, 0.34], "appliesTo": ["edit"]},
               {"id": "rail", "box": [0.0, 0.0, 0.2, 0.1], "appliesTo": ["walk"]}]
    code, summary, _ = run_main(["--before", plates["before"], "--after", landmark, "--output-dir", tmp_path / "c",
                                 "--stage", stage_file(tmp_path, regions)], capsys)
    assert code == 1 and summary["failed"] == ["protected stage:landmark unchanged"]
    report, checks = checks_of(tmp_path / "c")
    assert "protected stage:rail unchanged" not in checks  # walk-only regions are not edit-protected
    assert report["regions"][0]["uv"] == [0.62, 0.16, 0.74, 0.34] and report["inputs"][-1]["path"] == "../stage.json"


def test_protect_ground_uses_the_stage_polygons_and_band(plates, tmp_path, capsys):
    stage = stage_file(tmp_path, [])
    code, summary, _ = run_main(["--before", plates["before"], "--after", plates["band"], "--output-dir",
                                 tmp_path / "c", "--stage", stage, "--protect-ground"], capsys)
    assert code == 1
    assert set(summary["failed"]) == {"protected ground[0] unchanged", "protected playableBand unchanged"}
    code, _, err = run_main(["--before", plates["before"], "--after", plates["band"], "--output-dir", tmp_path / "d",
                             "--protect-ground"], capsys)
    assert code == 1 and "--protect-ground needs --stage" in err


def test_nothing_to_check_is_an_error(plates, tmp_path, capsys):
    code, summary, err = run_main(["--before", plates["before"], "--after", plates["local"], "--output-dir",
                                   tmp_path / "c"], capsys)
    assert code == 1 and summary is None and "nothing to check" in err and not (tmp_path / "c").exists()


def test_outputs_are_deterministic(plates, tmp_path, capsys):
    for name in ("a", "b"):
        assert run_main(["--before", plates["before"], "--after", plates["band"], "--output-dir", tmp_path / name,
                         "--protect-band", BAND, "--edit-box", EDIT_BOX], capsys)[0] == 1
    for name in ("locality-qa.json", "locality-diff.png"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


# --------------------------------------------------------------------------- standard CLI tests

def test_help_is_ascii_under_legacy_consoles():
    assert_cli_help(SKILL, "edit_locality_check")


def test_existing_output_is_refused(plates, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    result = run_cli([SCRIPT, "--before", plates["before"], "--after", plates["local"], "--output-dir", out,
                      "--protect-band", BAND])
    assert result.returncode == 1 and result.stderr.startswith("error: ") and "Refusing" in result.stderr
    assert result.stdout == "" and sorted(path.name for path in out.iterdir()) == ["keep.txt"]


def test_strict_failure_publishes_nothing(plates, tmp_path):
    out = tmp_path / "out"
    result = run_cli([SCRIPT, "--before", plates["before"], "--after", plates["band"], "--output-dir", out,
                      "--protect-band", BAND, "--strict"])
    assert result.returncode == 1 and "strict check failed" in result.stderr and result.stdout == ""
    assert not out.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["band.png", "before.png", "dark.png", "local.png"]
    kept = run_cli([SCRIPT, "--before", plates["before"], "--after", plates["band"], "--output-dir",
                    tmp_path / "kept", "--protect-band", BAND])
    assert kept.returncode == 1 and json.loads(kept.stdout)["status"] == "fail" and kept.stdout.isascii()
