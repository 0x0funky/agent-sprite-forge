"""codeart2d fx_build.py: the six presets, exact palette, the hit frame (the slash arc ends on the hit
tick), tail dropping, seeded particles, both routes, the exported fx.v1 runtime data and the
contracts. Specs are synthetic; the example is skills/codeart2d/examples/slash.fx.json (code-drawn).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (
    REPO_ROOT, SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, require_resvg, run_cli, script_path,
)

fx = load_script("codeart2d", "fx_build")
core = fx.core
SCRIPT = script_path("codeart2d", "fx_build")
EXAMPLE = REPO_ROOT / "skills" / "codeart2d" / "examples" / "slash.fx.json"
SCHEMA_DIR = SKILLS_DIR / "codeart2d" / "references" / "schemas"
PALETTE = {"white": "#ffffff", "gold": "#ffcd75", "ember": "#ef7d57", "red": "#b13e53", "ink": "#1a1c2c"}

# Schema additions requested in handoff/B19-codeart-rig-fx.md section 5 (codeart.schema.json): the optional
# fields fx_build reads, one $def per preset, and the fx-report.json document. Applied in memory here.
NUM = {"type": "number"}
NONNEG = {"type": "number", "minimum": 0}
PAIR = {"$ref": "common.schema.json#/$defs/point2"}
START = {"anyOf": [{"const": "impact"}, NONNEG]}
COMMON_PRIMITIVE = {"ramp": {"type": "string", "minLength": 1},
                    "colors": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}}
PRESET_PARAMETERS = {
    "slash": {"center": PAIR, "radius": NONNEG, "from": NUM, "to": NUM, "width": NONNEG, "squash": NONNEG,
              "trail": {"type": "number", "minimum": 0, "maximum": 1}, "startMs": START, "endMs": START,
              "fadeMs": NONNEG},
    "sparks": {"origin": PAIR, "atMs": START, "count": {"type": "integer", "minimum": 1, "maximum": 256},
               "angle": NUM, "spread": NONNEG, "speed": NONNEG, "lifeMs": NONNEG, "length": NONNEG, "width": NONNEG,
               "gravity": NUM},
    "ring": {"origin": PAIR, "atMs": START, "lifeMs": NONNEG, "radius": PAIR, "width": PAIR, "squash": NONNEG},
    "flash": {"origin": PAIR, "atMs": START, "lifeMs": NONNEG, "radius": NONNEG,
              "rays": {"type": "integer", "minimum": 1, "maximum": 16}, "rayLength": NONNEG, "rotation": NUM},
    "dust": {"origin": PAIR, "atMs": START, "count": {"type": "integer", "minimum": 1, "maximum": 256},
             "spread": NONNEG, "rise": NONNEG, "drift": NUM, "radius": PAIR, "lifeMs": NONNEG},
    "projectile": {"origin": PAIR, "angle": NUM, "radius": NONNEG, "trail": NONNEG,
                   "orbiters": {"type": "integer", "minimum": 0, "maximum": 16}, "periodMs": NONNEG},
}
FX_ADDITIONS = {
    "$defs": {
        **{f"fx_{name}": {"type": "object", "required": ["type"],
                          "properties": {"type": {"const": name}, **COMMON_PRIMITIVE, **parameters},
                          "additionalProperties": False}
           for name, parameters in PRESET_PARAMETERS.items()},
        "fx_report_v1": {
            "description": "fx-report.json written by fx_build.py: per effect the frames (start, duration and "
                           "sample ms, QA metrics, margin), durations, hit frame, dropped tail frames, events and "
                           "loop seam; slash arc progress per frame; the build and runtime records; qa is the "
                           "envelope of codeart-meta.json.",
            "type": "object", "required": ["schema", "route", "canvas", "origin", "effects", "qa"],
            "properties": {
                "schema": {"const": "codeart2d.fx_report.v1"}, "route": {"enum": ["pixel", "vector"]},
                "canvas": {"$ref": "common.schema.json#/$defs/size2"}, "origin": PAIR,
                "effects": {"type": "object", "minProperties": 1, "additionalProperties": {
                    "type": "object", "required": ["frames", "duration_ms", "hit_frame", "events"],
                    "properties": {
                        "frames": {"type": "array", "minItems": 1, "items": {
                            "type": "object", "required": ["id", "start_ms", "duration_ms", "sample_ms", "file", "qa"],
                            "properties": {"file": {"$ref": "common.schema.json#/$defs/relPath"}}}},
                        "duration_ms": {"$ref": "common.schema.json#/$defs/durationsMs"},
                        "hit_frame": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
                        "events": {"type": "array", "items": {"$ref": "sprite.schema.json#/$defs/clipEvent"}},
                        "seam": {"anyOf": [{"$ref": "common.schema.json#/$defs/seamReport"}, {"type": "null"}]}}}},
                "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}}},
    },
    "fx_v1_properties": {"canvas": {"$ref": "common.schema.json#/$defs/size2"}, "origin": PAIR},
    "effect_properties": {
        "frameMs": {"type": "integer", "minimum": 1}, "loop": {"type": "boolean"},
        "outline": {"type": "string", "minLength": 1},
        "events": {"type": "array", "items": {"type": "object", "required": ["atMs", "name"], "properties": {
            "atMs": {"type": "integer", "minimum": 0}, "name": {"$ref": "common.schema.json#/$defs/eventName"}}}}},
    # Added to primitives.items (kept open): a known preset type must match its $def exactly.
    "primitive_items_allOf": [{"if": {"properties": {"type": {"const": name}}, "required": ["type"]},
                               "then": {"$ref": f"#/$defs/fx_{name}"}} for name in PRESET_PARAMETERS],
}


def patched_validator(definition: str):
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    schemas = {path.name.removesuffix(".schema.json"): json.loads(path.read_text(encoding="utf-8"))
               for path in SCHEMA_DIR.glob("*.schema.json")}
    codeart = schemas["codeart"]
    codeart["$defs"].update(copy.deepcopy(FX_ADDITIONS["$defs"]))
    fx_v1 = codeart["$defs"]["fx_v1"]
    fx_v1["properties"].update(copy.deepcopy(FX_ADDITIONS["fx_v1_properties"]))
    effect = fx_v1["properties"]["effects"]["items"]
    effect["properties"].update(copy.deepcopy(FX_ADDITIONS["effect_properties"]))
    effect["properties"]["primitives"]["items"]["allOf"] = copy.deepcopy(FX_ADDITIONS["primitive_items_allOf"])
    registry = Registry().with_resources((schema["$id"], DRAFT202012.create_resource(schema))
                                         for schema in schemas.values())
    return Draft202012Validator({"$ref": f"{codeart['$id']}#/$defs/{definition}"}, registry=registry)


def assert_patched_valid(document, definition: str) -> None:
    errors = [f"{error.json_path}: {error.message}" for error in patched_validator(definition).iter_errors(document)]
    assert not errors, "\n".join(errors)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def spec(*effects: dict, canvas=(48, 48), origin=None) -> dict:
    data = {"schema": "codeart2d.fx.v1", "canvas": list(canvas), "palette": PALETTE, "effects": list(effects)}
    if origin is not None:
        data["origin"] = list(origin)
    return data


def effect(effect_id: str, *primitives: dict, duration: int = 300, impact: int = 100, **extra) -> dict:
    return {"id": effect_id, "durationMs": duration, "impactMs": impact, "seed": 3,
            "ramps": {"hot": ["white", "gold", "ember", "red"]}, "primitives": list(primitives), **extra}


def write_spec(directory: Path, data: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "case.fx.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def run_fx(spec_path: Path, output: Path, *extra: str):
    return run_cli([SCRIPT, "--spec", spec_path, "--output-dir", output, *extra])


def normalised(data: dict) -> fx.FxSpec:
    return fx.FxSpec(copy.deepcopy(data), base=Path("."))


ALL_PRESETS = spec(
    effect("slash", {"type": "slash", "ramp": "hot"}),
    effect("sparks", {"type": "sparks", "ramp": "hot", "atMs": 0}, impact=0),
    effect("ring", {"type": "ring", "ramp": "hot"}, impact=0),
    effect("flash", {"type": "flash", "ramp": "hot"}, duration=150, impact=0),
    effect("dust", {"type": "dust", "colors": ["gold", "ember", "red"]}, impact=0),
    effect("orb", {"type": "projectile", "ramp": "hot"}, duration=400, impact=0, loop=True, outline="ink"),
)


# ----------------------------------------------------------------------------- CLI conventions

def test_help_cp1252_and_cp950():
    assert_cli_help("codeart2d", "fx_build")


def test_refuses_an_existing_output_dir(tmp_path):
    path = write_spec(tmp_path / "case", ALL_PRESETS)
    output = tmp_path / "out"
    output.mkdir()
    result = run_fx(path, output)
    assert result.returncode == 1 and result.stderr.startswith("error: refusing to replace existing output")
    assert list(output.iterdir()) == []


@pytest.mark.resvg
def test_strict_qc_failure_publishes_nothing(tmp_path):
    require_resvg()
    escaping = spec(effect("burst", {"type": "sparks", "ramp": "hot", "atMs": 0, "speed": 400, "lifeMs": 250},
                           impact=0))  # sparks fly past the 48 px canvas edge
    output = tmp_path / "out"
    result = run_fx(write_spec(tmp_path / "case", escaping), output, "--strict-qc")
    assert result.returncode == 1, result.stdout
    assert result.stderr.startswith("error: strict QC failed (margins)") and "nothing was published" in result.stderr
    assert not output.exists() and not [path for path in tmp_path.iterdir() if path.name.startswith(".out.stage-")]


# ----------------------------------------------------------------------------- plan acceptance

@pytest.mark.resvg
def test_every_preset_builds_with_an_exact_palette(tmp_path):
    require_resvg()
    output = tmp_path / "out"
    result = run_fx(write_spec(tmp_path / "case", ALL_PRESETS), output, "--build-clips", "--strict-qc")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["effects"] == ["slash", "sparks", "ring", "flash", "dust", "orb"]
    report = read_json(output / "fx-report.json")
    statuses = {item["id"]: item["status"] for item in report["qa"]["checks"]}
    assert "fail" not in statuses.values()
    assert all(check_id.startswith("seam:") for check_id, status in statuses.items() if status == "warn")
    palette = set(PALETTE.values())
    for effect_id, record in report["effects"].items():
        assert record["frames"], effect_id
        for frame in record["frames"]:
            assert frame["qa"]["partial_alpha"] == 0 and frame["qa"]["off_palette"] == 0, frame["id"]
            with Image.open(output / frame["file"]) as png:
                pixels = np.asarray(png.convert("RGBA"))
            visible = pixels[pixels[..., 3] > 0]
            assert set(np.unique(pixels[..., 3])) <= {0, 255}
            assert {"#%02x%02x%02x" % tuple(rgb) for rgb in visible[:, :3]} <= palette, frame["id"]
    checks = {item["id"]: item for item in report["qa"]["checks"]}
    assert checks["build_clips"]["status"] == "pass" and checks["off_palette"]["value"] == 0
    built = read_json(output / "compiled-clips" / "animation-clips.json")
    assert sorted(built["clips"]) == sorted(summary["effects"])


@pytest.mark.resvg
def test_slash_arc_ends_on_the_hit_tick(tmp_path):
    """The frame that starts at impactMs is the first one showing the complete sweep; the clip's hit event
    points at it and the frames before it add up to impactMs exactly."""
    require_resvg()
    output = tmp_path / "out"
    data = spec(effect("slash", {"type": "slash", "ramp": "hot"}, duration=400, impact=150))
    assert run_fx(write_spec(tmp_path / "case", data), output).returncode == 0
    report = read_json(output / "fx-report.json")
    slash = report["effects"]["slash"]
    hit = slash["hit_frame"]
    assert sum(slash["duration_ms"][:hit]) == 150 and slash["frames"][hit]["start_ms"] == 150
    arc = report["arcs"][0]
    assert arc["arc_complete_frame"] == hit == 3 and arc["progress"][hit] == 1.0
    assert all(value < 1.0 for value in arc["progress"][:hit])
    assert {"at": hit, "name": "hit", "data": {"impactMs": 150}} in read_json(output / "clips.json")["clips"]["slash"]["events"]
    assert {item["id"]: item for item in report["qa"]["checks"]}["arc_on_hit"]["status"] == "pass"
    primitive = normalised(data).effects[0]["primitives"][0]
    assert fx.slash_progress(primitive, 149.9) < 1.0 == fx.slash_progress(primitive, 150.0)


def test_frame_schedule_cuts_a_frame_at_the_impact():
    for duration, impact, step in ((400, 150, 50), (300, 0, 50), (333, 117, 40), (90, 89, 50)):
        record = {"durationMs": duration, "impactMs": impact, "frameMs": step, "loop": False}
        durations, hit = fx.frame_schedule(record)
        assert sum(durations) == duration and min(durations) >= 1
        assert sum(durations[:hit]) == impact
    durations, hit = fx.frame_schedule({"durationMs": 400, "impactMs": 0, "frameMs": 50, "loop": True})
    assert durations == [50] * 8 and hit is None


@pytest.mark.resvg
def test_transparent_tail_frames_are_dropped_and_their_time_merged(tmp_path):
    require_resvg()
    output = tmp_path / "out"
    data = spec(effect("pop", {"type": "flash", "ramp": "hot", "lifeMs": 80}, duration=300, impact=0))
    assert run_fx(write_spec(tmp_path / "case", data), output, "--build-clips").returncode == 0
    record = read_json(output / "fx-report.json")["effects"]["pop"]
    assert record["dropped_tail_frames"] == 4 and record["duration_ms"] == [50, 250]
    assert read_json(output / "clips.json")["clips"]["pop"]["duration_ms"] == [50, 250]
    assert read_json(output / "fx-report.json")["build_clips"]["returncode"] == 0


@pytest.mark.resvg
def test_an_empty_frame_before_the_tail_is_an_error(tmp_path):
    require_resvg()
    data = spec(effect("late", {"type": "sparks", "ramp": "hot", "atMs": 200}, duration=300, impact=200))
    result = run_fx(write_spec(tmp_path / "case", data), tmp_path / "out")
    assert result.returncode == 1 and "frame 0 (t=25 ms) is fully transparent" in result.stderr


def test_spec_errors_name_the_problem():
    bad_colour = spec(effect("x", {"type": "ring", "colors": ["#123456"]}))
    with pytest.raises(core.CodeArtError, match="not in the palette"):
        normalised(bad_colour)
    with pytest.raises(core.CodeArtError, match="type must be one of"):
        normalised(spec(effect("x", {"type": "laser"})))
    with pytest.raises(core.CodeArtError, match="impactMs must be below durationMs"):
        normalised(spec(effect("x", {"type": "ring"}, duration=100, impact=100)))
    with pytest.raises(core.CodeArtError, match="unknown sparks parameter 'colour'"):
        normalised(spec(effect("x", {"type": "sparks", "colour": "red"})))
    with pytest.raises(core.CodeArtError, match="arc must sweep before it ends"):
        normalised(spec(effect("x", {"type": "slash"}, impact=0)))
    with pytest.raises(core.CodeArtError, match="palette is required"):
        normalised({"schema": "codeart2d.fx.v1", "effects": [effect("x", {"type": "ring", "colors": ["#ffffff"]})]})


def test_particles_are_seeded_and_the_hash_is_pinned():
    """hash01 is the 32-bit mix the fx.v1 runtime reproduces bit for bit: the pinned values were computed with
    the runtime's JS formula (Math.imul) in node; test_fx_verify_js compares whole shape lists."""
    assert fx.hash01(0, 0) == 0.07026689755730331 and fx.hash01(7, 3) == 0.8173010400496423
    assert fx.hash01(2**31 - 1, 255) == 0.9033616967499256
    assert fx.stream(4000000000, 2, 1) == 2403569206 and fx.hash01(2403569206, 9) == 0.6608240678906441
    values = [fx.hash01(fx.stream(11, 2, 1), index) for index in range(4000)]
    assert 0.0 <= min(values) and max(values) < 1.0 and abs(float(np.mean(values)) - 0.5) < 0.02
    burst = normalised(spec(effect("b", {"type": "sparks", "ramp": "hot", "count": 12}))).effects[0]
    assert fx.effect_shapes(burst, 180.0) == fx.effect_shapes(copy.deepcopy(burst), 180.0)
    assert fx.effect_shapes(burst, 180.0) != fx.effect_shapes(burst, 180.0, seed=burst["seed"] ^ 99)


@pytest.mark.resvg
def test_vector_route_and_the_projectile_loop(tmp_path):
    require_resvg()
    output = tmp_path / "out"
    data = spec(effect("orb", {"type": "projectile", "ramp": "hot"}, duration=400, impact=0, loop=True, outline="ink"),
                canvas=(48, 32))
    result = run_fx(write_spec(tmp_path / "case", data), output, "--route", "vector", "--zoom", "2")
    assert result.returncode == 0, result.stderr
    report = read_json(output / "fx-report.json")
    assert report["canvas"] == [96, 64] and report["origin"] == [48.0, 32.0]
    orb = report["effects"]["orb"]
    assert orb["hit_frame"] is None and len(orb["frames"]) == 8 and orb["events"] == []
    assert 0.8 <= orb["seam"]["seam_over_median"] <= 1.25
    clips = read_json(output / "clips.json")
    assert clips["clips"]["orb"]["loop"] is True and clips["sampling"] == "linear" and clips["pixel_art"] is False
    with Image.open(output / orb["frames"][0]["file"]) as png:
        assert png.size == (96, 64)
        assert 0 < np.count_nonzero((np.asarray(png)[..., 3] > 0) & (np.asarray(png)[..., 3] < 255))  # anti-aliased


@pytest.mark.resvg
def test_same_spec_gives_identical_bytes(tmp_path):
    require_resvg()
    path = write_spec(tmp_path / "case", spec(effect("slash", {"type": "slash", "ramp": "hot"}),
                                              effect("dust", {"type": "dust", "ramp": "hot"}, impact=0)))
    for name in ("a", "b"):
        assert run_fx(path, tmp_path / name, "--export-runtime", "--no-verify").returncode == 0
    files = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(tmp_path / "b") for p in (tmp_path / "b").rglob("*") if p.is_file())
    for relative in files:
        assert (tmp_path / "a" / relative).read_bytes() == (tmp_path / "b" / relative).read_bytes(), relative


@pytest.mark.resvg
def test_exported_runtime_embeds_the_normalised_spec(tmp_path):
    require_resvg()
    output = tmp_path / "out"
    result = run_fx(EXAMPLE, output, "--export-runtime", "--no-verify", "--effects", "slash,orb")
    assert result.returncode == 0, result.stderr
    module = (output / "fx-runtime.mjs").read_text(encoding="utf-8")
    start = module.index(fx.DATA_START) + len(fx.DATA_START)
    data = json.loads(module[start:module.index(fx.DATA_END)])
    assert data == json.loads(json.dumps(fx.FxSpec(read_json(EXAMPLE), base=EXAMPLE.parent).runtime_data()))
    assert module.startswith("// fx.v1 runtime generated by codeart2d fx_build.py from slash.fx.json.")
    checks = {item["id"]: item for item in read_json(output / "fx-report.json")["qa"]["checks"]}
    assert checks["runtime_verify"]["status"] == "skipped"
    assert {ref["path"] for ref in read_json(output / "codeart-meta.json")["outputs"]} >= {"fx-runtime.mjs"}


def test_template_demo_data_is_the_normalised_example_slash():
    """The runtime template ships the example's slash so it stays runnable and verifiable on its own."""
    template = fx.RUNTIME_TEMPLATE.read_text(encoding="utf-8")
    start = template.index(fx.DATA_START) + len(fx.DATA_START)
    demo = json.loads(template[start:template.index(fx.DATA_END)])
    example = fx.FxSpec(read_json(EXAMPLE), base=EXAMPLE.parent).runtime_data()
    slash = next(item for item in example["effects"] if item["id"] == "slash")
    assert demo == json.loads(json.dumps({**example, "effects": [slash]}))


@pytest.mark.resvg
def test_example_outputs_validate_against_the_contracts(tmp_path):
    require_resvg()
    output = tmp_path / "out"
    result = run_fx(EXAMPLE, output, "--build-clips", "--no-verify", "--export-runtime", "--strict-qc")
    assert result.returncode == 0, result.stderr
    assert_valid_contract(read_json(EXAMPLE), "codeart", "fx_v1", skill="codeart2d")
    assert_patched_valid(read_json(EXAMPLE), "fx_v1")
    clips = read_json(output / "clips.json")
    assert_valid_contract(clips, "sprite", "clips_input", skill="codeart2d")
    assert all(clip["role"] == "fx" for clip in clips["clips"].values())
    meta = read_json(output / "codeart-meta.json")
    assert_valid_contract(meta, "codeart", "codeart_meta_v1", skill="codeart2d")
    assert_valid_contract(meta["qa"], "common", "qaEnvelope", skill="codeart2d")
    assert meta["qa"]["status"] == "pass" and meta["generator"] == "codeart2d/fx_build.py"
    assert_patched_valid(read_json(output / "fx-report.json"), "fx_report_v1")
    assert_valid_contract(read_json(output / "compiled-clips" / "animation-clips.json"), "sprite",
                          "animation_clips_v2", skill="codeart2d")
    unknown = copy.deepcopy(read_json(EXAMPLE))
    unknown["effects"][0]["primitives"][0]["spin"] = 3
    assert not patched_validator("fx_v1").is_valid(unknown)  # a preset's parameters are a closed set
    # The additions are additive: A0's contract fixture (other primitive types) stays valid.
    assert_patched_valid(read_json(REPO_ROOT / "tests" / "fixtures" / "contracts" / "codeart.fx_v1.valid.json"),
                         "fx_v1")
