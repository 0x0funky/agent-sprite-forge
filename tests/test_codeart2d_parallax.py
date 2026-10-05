"""codeart2d parallax_build (periodic layers, sibling validator, camera sweep; plan B21-T2) and
ambient_bake (periodic displacement in feathered polygons over an exact period; plan B21-T3).

All inputs are synthetic: the shipped parallax-gen.json and plate-effects.json examples plus plates
drawn here with numpy.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from forge_testutils import SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, run_cli, script_path

SKILL = "codeart2d"
PARALLAX = script_path(SKILL, "parallax_build")
AMBIENT = script_path(SKILL, "ambient_bake")
EXAMPLES = SKILLS_DIR / SKILL / "examples"
SCHEMAS = SKILLS_DIR / SKILL / "references" / "schemas"
parallax_build = load_script(SKILL, "parallax_build")
ambient_bake = load_script(SKILL, "ambient_bake")
codeart_core = load_script(SKILL, "codeart_core")

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rgba(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA")).copy()


def write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# =========================================================================== parallax_build

def parallax(spec_path: Path, out: Path, *extra: str):
    return run_cli([PARALLAX, "--spec", spec_path, "--output-dir", out, *extra], timeout=600)


@pytest.fixture(scope="module")
def backdrop(tmp_path_factory):
    """The shipped parallax example with the sibling validator and a 49-frame sweep, built twice."""
    root = tmp_path_factory.mktemp("parallax")
    for name in ("a", "b"):
        result = parallax(EXAMPLES / "parallax-gen.json", root / name, "--validate", "--sweep-frames", "49",
                          "--strict-qc")
        assert result.returncode == 0, result.stderr
    return root


def test_parallax_help_is_ascii():
    assert_cli_help(SKILL, "parallax_build")


def test_parallax_refuses_existing_output(tmp_path):
    out = tmp_path / "bg"
    out.mkdir()
    result = parallax(EXAMPLES / "parallax-gen.json", out)
    assert result.returncode == 1 and result.stderr.startswith("error:") and "already exists" in result.stderr
    assert list(out.iterdir()) == []


def test_parallax_strict_qc_failure_publishes_nothing(tmp_path):
    """A repeating image layer whose edges do not meet fails the loop-step gate; nothing is published."""
    ramp = np.zeros((64, 96, 4), np.uint8)
    ramp[..., 0] = np.linspace(0, 255, 96, dtype=np.uint8)[None, :]
    ramp[32:, :, 3] = 255
    Image.fromarray(ramp).save(tmp_path / "ramp.png")
    spec = {"schema": "codeart2d.parallax_spec.v1", "viewport": [96, 64], "sweep_frames": 4,
            "layers": [{"id": "sky", "kind": "sky", "colors": ["#203050", "#8090a0"]},
                       {"id": "ramp", "kind": "image", "image": "ramp.png", "role": "near", "scroll": 1.0,
                        "repeat": [True, False]}]}
    spec_path = write_json(tmp_path / "spec.json", spec)
    result = parallax(spec_path, tmp_path / "bg", "--strict-qc")
    assert result.returncode == 1 and "loop_step:ramp" in result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ramp.png", "spec.json"]
    result = parallax(spec_path, tmp_path / "bg")
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "fail"
    qa = json.loads((tmp_path / "bg" / "parallax-qa.json").read_text(encoding="utf-8"))
    check = next(c for c in qa["checks"] if c["id"] == "loop_step:ramp")
    assert check["status"] == "fail" and check["value"] > 1.0


def test_parallax_example_validates_loops_and_sweeps(backdrop):
    out = backdrop / "a"
    qa = json.loads((out / "parallax-qa.json").read_text(encoding="utf-8"))
    assert qa["status"] == "pass", [c for c in qa["checks"] if c["status"] != "pass"]
    checks = {check["id"]: check for check in qa["checks"]}
    report = json.loads((out / "validate-parallax.json").read_text(encoding="utf-8"))
    assert report["passed"] is True and report["issues"] == [] and checks["sibling_validator"]["status"] == "pass"
    assert all("/" not in layer["image"] and "\\" not in layer["image"] for layer in report["layers"])
    plan = json.loads((out / "parallax-plan.json").read_text(encoding="utf-8"))
    repeating = [layer["id"] for layer in plan["layers"] if layer["repeat"][0]]
    assert repeating == ["clouds", "far", "mid", "near"]
    for layer_id in repeating:
        assert checks[f"periodic:{layer_id}"]["value"] == 0
        image = rgba(out / f"{layer_id}.png")
        seam = qa["seams"][layer_id]
        assert seam["seam"] <= seam["adjacent_p95"] + 1e-9, "loop step must be at most p95 of the column steps"
        # independent check: the wrap step is the smallest cyclic column step of the layer
        pre = image.astype(np.float64)
        pre[..., :3] *= pre[..., 3:] / 255.0
        steps = np.abs(pre - np.roll(pre, -1, axis=1)).mean(axis=(0, 2))
        assert steps[-1] == pytest.approx(steps.min())
        authored = json.loads((EXAMPLES / "parallax-gen.json").read_text(encoding="utf-8"))["layers"]
        assert image.shape[1] == next(entry for entry in authored if entry["id"] == layer_id)["period"]
    assert qa["sweep"]["frames"] == 49 and qa["sweep"]["frames_with_transparency"] == 0
    with Image.open(out / "sweep.webp") as animation:
        assert animation.n_frames == 49 and animation.size == (480, 270)
    assert (out / "sweep-sheet.png").is_file()


def test_parallax_outputs_validate_and_are_deterministic(backdrop):
    a, b = backdrop / "a", backdrop / "b"
    for name in ("parallax-plan.json", "parallax-qa.json", "codeart-meta.json", "validate-parallax.json",
                 "sky.png", "clouds.png", "far.png", "mid.png", "near.png", "sweep-sheet.png"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    plan = json.loads((a / "parallax-plan.json").read_text(encoding="utf-8"))
    assert_valid_contract(plan, "map", "parallax_plan", skill=SKILL)
    for layer in plan["layers"]:
        assert sha256(a / layer["image"]) == layer["sha256"]
        assert layer["require_canvas_coverage"] is True
    assert_valid_contract(json.loads((a / "parallax-qa.json").read_text(encoding="utf-8")), "common", "qaEnvelope",
                          skill=SKILL)
    meta = json.loads((a / "codeart-meta.json").read_text(encoding="utf-8"))
    assert_valid_contract(meta, "codeart", "codeart_meta_v1", skill=SKILL)
    for ref in meta["outputs"]:
        assert sha256(a / ref["path"]) == ref["sha256"]
    assert_valid_contract(json.loads((EXAMPLES / "parallax-gen.json").read_text(encoding="utf-8")), "codeart",
                          "parallax_spec_v1", skill=SKILL)
    qa = json.loads((a / "parallax-qa.json").read_text(encoding="utf-8"))
    assert qa["tool"] == {"name": "parallax_build", "version": "0.4.0"}  # D29


def test_parallax_pixel_art_layers_use_only_spec_colours(backdrop):
    spec = json.loads((EXAMPLES / "parallax-gen.json").read_text(encoding="utf-8"))
    for layer in spec["layers"][1:]:
        colours = {layer[key].lower() for key in ("fill", "rim", "shade") if key in layer}
        colours |= {value.lower() for key in ("trees", "grass") for value in [layer.get(key, {}).get("color")] if value}
        image = rgba(backdrop / "a" / f"{layer['id']}.png")
        visible = image[image[..., 3] > 0]
        assert set(np.unique(visible[:, 3]).tolist()) == {255}, "pixel art has no partial alpha"
        found = {"#%02x%02x%02x" % tuple(int(v) for v in pixel[:3]) for pixel in np.unique(visible, axis=0)}
        assert found <= colours, (layer["id"], found - colours)


def test_canvases_cover_zoom_out_and_vertical_camera(tmp_path):
    """Zoom down to 0.75 and a vertical camera range: canvases grow so every extreme is covered."""
    spec = json.loads((EXAMPLES / "parallax-gen.json").read_text(encoding="utf-8"))
    spec["camera"] = {"x": [0, 960], "y": [-40, 30], "zoom": [0.75, 1.25]}
    spec["layers"][2]["scroll"] = [0.2, 0.1]
    spec["layers"][4]["scroll"] = [1.25, 1.0]
    spec["sweep_frames"] = 9
    out = tmp_path / "bg"
    result = parallax(write_json(tmp_path / "spec.json", spec), out, "--strict-qc")
    assert result.returncode == 0, result.stderr
    report = json.loads((out / "validate-parallax.json").read_text(encoding="utf-8"))
    assert report["passed"] and report["camera_extrema_checked"] == 8
    plan = json.loads((out / "parallax-plan.json").read_text(encoding="utf-8"))
    near = next(layer for layer in plan["layers"] if layer["id"] == "near")
    height = rgba(out / "near.png").shape[0]
    assert near["offset"][1] == -40 and height >= math.ceil(270 / 0.75 + 30 + 40)
    sky = rgba(out / "sky.png")
    assert sky.shape[1] >= 480 / 0.75 and sky[..., 3].min() == 255


def test_periodicity_proof_and_calm_origin():
    spec = parallax_build.load_spec(EXAMPLES / "parallax-gen.json", 0)
    parallax_build.size_canvases(spec)
    mid = next(layer for layer in spec.layers if layer.id == "mid")
    single = parallax_build.render_layer(mid, spec, mid.width)
    double = parallax_build.render_layer(mid, spec, 3 * mid.width)
    np.testing.assert_array_equal(double, np.tile(single, (1, 3, 1)))
    rolled, shift = parallax_build.roll_to_calmest(single)
    np.testing.assert_array_equal(rolled, np.roll(single, -shift, axis=1))
    report = parallax_build.column_seam(rolled)
    assert report["seam"] <= report["adjacent_median"] + 1e-9


def test_smooth_layers_stay_periodic(tmp_path):
    spec = json.loads((EXAMPLES / "parallax-gen.json").read_text(encoding="utf-8"))
    spec["pixel_art"] = False
    spec["sweep_frames"] = 0
    out = tmp_path / "bg"
    result = parallax(write_json(tmp_path / "spec.json", spec), out, "--strict-qc", "--no-validate")
    assert result.returncode == 0, result.stderr
    qa = json.loads((out / "parallax-qa.json").read_text(encoding="utf-8"))
    checks = {check["id"]: check for check in qa["checks"]}
    assert all(checks[f"periodic:{name}"]["value"] == 0 for name in ("clouds", "far", "mid", "near"))
    assert checks["sibling_validator"]["status"] == "skipped"
    alpha = rgba(out / "far.png")[..., 3]
    assert ((alpha > 0) & (alpha < 255)).any(), "smooth layers are antialiased"


def test_validator_flag_errors(tmp_path):
    result = parallax(EXAMPLES / "parallax-gen.json", tmp_path / "bg", "--validate", "--validator",
                      tmp_path / "missing.py")
    assert result.returncode == 1 and "validator" in result.stderr and not (tmp_path / "bg").exists()
    bad = json.loads((EXAMPLES / "parallax-gen.json").read_text(encoding="utf-8"))
    del bad["layers"][2]["scroll"]
    result = parallax(write_json(tmp_path / "bad.json", bad), tmp_path / "bg")
    assert result.returncode == 1 and "scroll is required" in result.stderr


# =========================================================================== ambient_bake

def ambient(plate: Path, spec_path: Path, out: Path, *extra: str):
    return run_cli([AMBIENT, "--plate", plate, "--spec", spec_path, "--output-dir", out, *extra], timeout=600)


def textured_plate(path: Path, size=(320, 180)) -> np.ndarray:
    width, height = size
    ys, xs = np.mgrid[0:height, 0:width]
    plate = np.zeros((height, width, 4), np.uint8)
    plate[..., 0] = (xs * 255 // width).astype(np.uint8)
    plate[..., 1] = (ys * 255 // height).astype(np.uint8)
    plate[..., 2] = ((xs // 8 + ys // 8) % 2 * 120 + 60).astype(np.uint8)
    plate[..., 3] = 255
    Image.fromarray(plate).save(path)
    return plate


@pytest.fixture(scope="module")
def harbour(tmp_path_factory):
    root = tmp_path_factory.mktemp("ambient")
    plate = textured_plate(root / "plate.png")
    result = ambient(root / "plate.png", EXAMPLES / "plate-effects.json", root / "out", "--preview", "--strict-qc")
    assert result.returncode == 0, result.stderr
    return root, plate


def test_ambient_help_is_ascii():
    assert_cli_help(SKILL, "ambient_bake")


def test_ambient_refuses_existing_output(tmp_path):
    textured_plate(tmp_path / "plate.png")
    (tmp_path / "out").mkdir()
    result = ambient(tmp_path / "plate.png", EXAMPLES / "plate-effects.json", tmp_path / "out")
    assert result.returncode == 1 and "already exists" in result.stderr
    assert list((tmp_path / "out").iterdir()) == []


def test_ambient_strict_qc_failure_publishes_nothing(tmp_path):
    """On a flat-colour plate the ripple and shimmer move nothing: strict QC refuses to publish."""
    Image.new("RGBA", (200, 120), (40, 90, 160, 255)).save(tmp_path / "flat.png")
    result = ambient(tmp_path / "flat.png", EXAMPLES / "plate-effects.json", tmp_path / "out", "--strict-qc")
    assert result.returncode == 1 and "motion_present" in result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["flat.png"]


def test_ambient_loop_has_an_exact_period(harbour):
    root, plate = harbour
    document = json.loads((root / "out" / "ambient-loop.json").read_text(encoding="utf-8"))
    assert document["frame_count"] == 48 and document["period_ms"] == 4800
    assert sum(document["durations_ms"]) == 4800 and document["fps"] == "10/1"
    assert document["loop"] == {"policy": "cycle", "exact": True}
    assert {e["id"]: e["cycles"] for e in document["effects"]} == {"harbour-water": 1, "reeds": 2,
                                                                     "chimney-haze": 3, "lantern": 3}
    loop = ambient_bake.load_loop(EXAMPLES / "plate-effects.json", root / "plate.png", None, None)
    ambient_bake.prepare(loop)
    premultiplied = ambient_bake._premultiplied(loop.plate)
    first = rgba(root / "out" / "frames" / "frame_000000.png")
    np.testing.assert_array_equal(ambient_bake.render_frame(loop, 48, premultiplied), first)
    np.testing.assert_array_equal(ambient_bake.render_frame(loop, 96 + 5, premultiplied),
                                  rgba(root / "out" / "frames" / "frame_000005.png"))
    frames = [rgba(root / "out" / "frames" / f"frame_{i:06d}.png") for i in range(48)]
    assert len({frame.tobytes() for frame in frames}) == 48, "every frame differs inside the effects"


def test_ambient_pixels_outside_polygons_are_unchanged(harbour):
    root, plate = harbour
    height, width = plate.shape[:2]
    spec = json.loads((EXAMPLES / "plate-effects.json").read_text(encoding="utf-8"))
    ys, xs = np.mgrid[0:height, 0:width] + 0.5
    inside = np.zeros((height, width), bool)
    for effect in spec["effects"]:
        polygon = [(u * width, v * height) for u, v in effect["polygon"]]
        hit = np.zeros((height, width), bool)
        for (x0, y0), (x1, y1) in zip(polygon, polygon[1:] + polygon[:1]):
            if y0 != y1:
                hit ^= ((ys >= min(y0, y1)) & (ys < max(y0, y1))) & (xs < x0 + (ys - y0) * (x1 - x0) / (y1 - y0))
        inside |= hit
    x0, y0, x1, y1 = spec["protected"][0]["box"]
    protected = (xs >= x0 * width) & (xs < x1 * width) & (ys >= y0 * height) & (ys < y1 * height)
    frozen = ~inside | protected
    changed = np.zeros((height, width), bool)
    for index in range(48):
        frame = rgba(root / "out" / "frames" / f"frame_{index:06d}.png")
        np.testing.assert_array_equal(frame[frozen], plate[frozen])
        changed |= np.any(frame != plate, axis=-1)
    assert changed[inside & ~protected].mean() > 0.3
    mask = np.asarray(Image.open(root / "out" / "mask.png"))
    assert mask.dtype == np.uint8 and not mask[frozen].any() and mask[inside & ~protected].max() == 255


def test_ambient_outputs_validate(harbour):
    root, _ = harbour
    out = root / "out"
    document = json.loads((out / "ambient-loop.json").read_text(encoding="utf-8"))
    assert_valid_contract(document, "codeart", "ambient_loop_v1", skill=SKILL)
    assert_valid_contract(json.loads((EXAMPLES / "plate-effects.json").read_text(encoding="utf-8")), "codeart",
                          "plate_effects_v1", skill=SKILL)
    assert document["provenance"]["version"] == "0.4.0"  # D29
    assert document["art_source"] == "mixed" and document["plate_art_source"] == "existing"
    assert not (out / "codeart-meta.json").exists()
    qa = json.loads((out / "ambient-qa.json").read_text(encoding="utf-8"))
    assert_valid_contract(qa, "common", "qaEnvelope", skill=SKILL)
    assert qa["status"] == "pass"
    for ref in qa["outputs"] + document["frames"] + [document["mask"]]:
        assert sha256(out / ref["path"]) == ref["sha256"], ref["path"]
    with Image.open(out / "preview.webp") as animation:
        assert animation.n_frames == 48
    assert (out / "review.png").is_file()


def test_ambient_reads_stage_documents_and_nearest_sampling(tmp_path):
    """A generate2dmap.stage.v1 document drives the bake; nearest sampling with no feather keeps the
    plate's exact colours."""
    plate = textured_plate(tmp_path / "plate.png", (160, 90))
    stage = {"schema": "generate2dmap.stage.v1", "sourceSize": [1600, 900], "fit": "cover",
             "reviewedAspectRange": [1.5, 2.2], "groundPolygons": [[[0, 0.5], [1, 0.5], [1, 1], [0, 1]]],
             "slots": {"hero": [[0.3, 0.8]], "enemy": [[0.7, 0.8]]},
             "effects": [{"id": "pool", "kind": "ripple", "polygon": [[0.1, 0.6], [0.9, 0.6], [0.9, 0.95], [0.1, 0.95]],
                          "period": 2.0, "amplitude": 2.0, "wavelength": 30, "axis": "x"}],
             "protectedRegions": [{"id": "statue", "box": [0.4, 0.62, 0.5, 0.8]}]}
    assert_valid_contract(stage, "map", "stage_v1", skill=SKILL)
    spec_path = write_json(tmp_path / "stage.json", stage)
    loop = ambient_bake.load_loop(spec_path, tmp_path / "plate.png", 8, None)
    loop.sampling = "nearest"
    loop.effects[0].feather = 0.0
    ambient_bake.prepare(loop)
    premultiplied = ambient_bake._premultiplied(loop.plate)
    colours = {tuple(p) for p in plate.reshape(-1, 4).tolist()}
    for index in range(8):
        frame = ambient_bake.render_frame(loop, index, premultiplied)
        assert {tuple(p) for p in frame.reshape(-1, 4).tolist()} <= colours
    result = ambient(tmp_path / "plate.png", spec_path, tmp_path / "out", "--frames", "8")
    assert result.returncode == 0, result.stderr
    document = json.loads((tmp_path / "out" / "ambient-loop.json").read_text(encoding="utf-8"))
    assert document["source_schema"] == "generate2dmap.stage.v1" and document["period_ms"] == 2000
    assert document["durations_ms"] == [250] * 8


def test_feather_distance_without_scipy_is_identical(monkeypatch):
    """The feather is codeart_core.distance_field of the region's complement (the copy ambient_bake and
    layout_build each carried is gone); scipy and the numpy path agree with brute force."""
    assert not hasattr(ambient_bake, "_local_capped_edt") and not hasattr(ambient_bake, "_inside_polygon")
    rng = np.random.default_rng(9)
    ys, xs = np.mgrid[0:50, 0:70]
    for trial in range(3):
        cx, cy, r = rng.uniform(15, 55), rng.uniform(10, 40), rng.uniform(6, 20)
        inside = np.hypot(xs - cx, ys - cy) <= r
        monkeypatch.setenv("FORGE_CORE_NO_SCIPY", "1")
        fallback = ambient_bake._inside_distance(inside, 8.0)
        monkeypatch.delenv("FORGE_CORE_NO_SCIPY")
        np.testing.assert_array_equal(fallback, ambient_bake._inside_distance(inside, 8.0))
        oy, ox = np.nonzero(~inside)
        brute = np.hypot(xs[..., None] - ox, ys[..., None] - oy).min(axis=-1)
        expected = np.where(inside, np.where(brute <= 8.0, brute, np.inf), 0.0)
        np.testing.assert_array_equal(fallback, expected)


def test_ambient_rejects_periods_that_cannot_loop(tmp_path):
    textured_plate(tmp_path / "plate.png")
    spec = json.loads((EXAMPLES / "plate-effects.json").read_text(encoding="utf-8"))
    spec["effects"][1]["period"] = 1.7  # does not divide 4800 ms
    result = ambient(tmp_path / "plate.png", write_json(tmp_path / "spec.json", spec), tmp_path / "out")
    assert result.returncode == 1 and "does not divide" in result.stderr and not (tmp_path / "out").exists()
    del spec["period_ms"]
    spec["effects"][1]["period"] = 0.0007  # fractional milliseconds
    result = ambient(tmp_path / "plate.png", write_json(tmp_path / "spec.json", spec), tmp_path / "out")
    assert result.returncode == 1 and "whole number of milliseconds" in result.stderr


def test_dusk_style_phase_drift_would_break_the_loop():
    """Regression for the Dusk runtime formula (phase * 0.37): it does not repeat over one period, while
    ambient_bake's integer harmonics do."""
    phase_end = 2 * math.pi
    q = 0.0
    dusk_start = 0.72 * math.sin(q) + 0.28 * math.sin(q * 0.57 - 0 * 0.37)
    dusk_end = 0.72 * math.sin(q - phase_end) + 0.28 * math.sin((q - phase_end) * 0.57 - phase_end * 0.37)
    assert abs(dusk_end - dusk_start) > 0.05
    effect = ambient_bake.Effect("e", "ripple", [(0, 0), (1, 0), (1, 1)], 1000, 2.0, 30.0, "x", 0.0, 1.0, None)
    ys = np.linspace(0, 100, 7)
    np.testing.assert_allclose(ambient_bake.displacement(effect, 0.0, ys * 0, ys),
                               ambient_bake.displacement(effect, 2 * math.pi, ys * 0, ys), atol=1e-12)


# =========================================================================== conventions and limits

def test_usage_errors_exit_2_and_json_with_a_bom_is_read(tmp_path):
    """D26: argument errors are argparse usage errors (exit 2); D28: specs saved with a BOM are read."""
    textured_plate(tmp_path / "plate.png", (160, 90))
    for extra in (["--frames", "1"], ["--frames", "x"], ["--fps", "0"], ["--fps", "nan"]):
        result = ambient(tmp_path / "plate.png", EXAMPLES / "plate-effects.json", tmp_path / "never", *extra)
        assert result.returncode == 2 and "usage:" in result.stderr, extra
    result = parallax(EXAMPLES / "parallax-gen.json", tmp_path / "never", "--sweep-frames", "-1")
    assert result.returncode == 2 and "usage:" in result.stderr and not (tmp_path / "never").exists()
    for name in ("parallax-gen.json", "plate-effects.json"):
        (tmp_path / name).write_bytes(b"\xef\xbb\xbf" + (EXAMPLES / name).read_bytes())
    result = parallax(tmp_path / "parallax-gen.json", tmp_path / "bg", "--no-validate", "--sweep-frames", "0")
    assert result.returncode == 0, result.stderr
    result = ambient(tmp_path / "plate.png", tmp_path / "plate-effects.json", tmp_path / "amb", "--frames", "12")
    assert result.returncode == 0, result.stderr
    for index, text in enumerate(["[]", "{", '{"schema": "codeart2d.parallax_spec.v1", "viewport": "wide"}']):
        path = tmp_path / f"bad-{index}.json"
        path.write_text(text, encoding="utf-8")
        for result in (parallax(path, tmp_path / f"p{index}"), ambient(tmp_path / "plate.png", path, tmp_path / f"a{index}")):
            assert result.returncode == 1 and result.stderr.startswith("error:") and "Traceback" not in result.stderr
            assert "internal error" not in result.stderr


def test_parallax_refuses_canvases_that_would_not_fit_in_memory(tmp_path):
    """The codeart review grew a viewport [100000, 100000] to 10 GB: the canvas and sweep caps refuse it
    (and a tiny minimum zoom) before anything is allocated."""
    spec = json.loads((EXAMPLES / "parallax-gen.json").read_text(encoding="utf-8"))
    for change, needle in ((lambda s: s.update(viewport=[100000, 100000]), "composite"),
                           (lambda s: s.update(camera={**s.get("camera", {}), "zoom": [0.01, 1]}), "composite"),
                           (lambda s: s.update(viewport=[3840, 2160]), "sweep")):
        changed = json.loads(json.dumps(spec))
        change(changed)
        result = parallax(write_json(tmp_path / "huge.json", changed), tmp_path / "never", "--no-validate")
        assert result.returncode == 1 and needle in result.stderr and "at most" in result.stderr, result.stderr
        assert not (tmp_path / "never").exists()


def test_pixel_art_image_layers_need_a_whole_number_scale(tmp_path):
    """Nearest neighbour at a fractional scale gives uneven pixels (codeart review): refused for pixel art,
    smooth resampling when pixel_art is false."""
    tile = np.zeros((32, 48, 4), np.uint8)
    tile[16:, :, :] = (90, 140, 60, 255)
    Image.fromarray(tile).save(tmp_path / "hill.png")
    spec = {"schema": "codeart2d.parallax_spec.v1", "viewport": [96, 64], "sweep_frames": 0,
            "layers": [{"id": "sky", "kind": "sky", "colors": ["#203050", "#8090a0"]},
                       {"id": "hill", "kind": "image", "image": "hill.png", "role": "near", "scroll": 1.0,
                        "scale": 1.5, "repeat": [False, False], "require_canvas_coverage": False}]}
    result = parallax(write_json(tmp_path / "spec.json", spec), tmp_path / "px", "--no-validate")
    assert result.returncode == 1 and "whole-number scale" in result.stderr
    spec["layers"][1]["scale"] = 2
    assert parallax(write_json(tmp_path / "spec.json", spec), tmp_path / "px2", "--no-validate").returncode == 0
    spec["layers"][1]["scale"] = 1.5
    spec["pixel_art"] = False
    assert parallax(write_json(tmp_path / "spec.json", spec), tmp_path / "smooth", "--no-validate").returncode == 0
