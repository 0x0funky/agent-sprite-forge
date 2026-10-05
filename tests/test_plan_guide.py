"""Tests for skills/generate2dsprite/scripts/plan_guide.py (B03-T4), the B03 prompt docs (B03-T5) and the
schema requests of handoff/B03-sprite-authoring-qc.md section 5.

The host-size rule comes from report v2 3.3.1 (3 of 3 host calls kept the aspect at about 1,572,864 px;
DOC-03 adds the 2:1 case, 1774x887).
"""
from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from forge_testutils import SKILLS_DIR, assert_cli_help, load_script, run_cli, script_path

SKILL = "generate2dsprite"
SCRIPT = script_path(SKILL, "plan_guide")
pg = load_script(SKILL, "plan_guide")
sq = load_script(SKILL, "sheet_qc")
REFERENCES = SKILLS_DIR / SKILL / "references"
DOCS = [REFERENCES / "prompt-rules.md", REFERENCES / "action-recipes.md"]

# --------------------------------------------------------------------------- schema requests (handoff section 5)

POSITIVE_PAIR = {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 2, "maxItems": 2}
CELL_RC = {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2}
SHEET_PLAN_V1 = {
    "description": "plan_guide.py output (opt-in generation aid, not A/B tested with an image model): the host size "
                   "predicted for an aspect-only request, the chosen grid and the candidates, the guide's cells "
                   "(safe box, ground line, root) and gait phases, and the guide's self-check as a QA envelope.",
    "type": "object",
    "required": ["schema", "predicted_size", "aspect", "layout", "qa"],
    "properties": {
        "schema": {"const": "generate2dsprite.sheet_plan.v1"},
        "request": {"type": "object"},
        "predicted_size": {"$ref": "common.schema.json#/$defs/size2"},
        "aspect": {"type": "string", "pattern": "^[1-9][0-9]*:[1-9][0-9]*$"},
        "layout": {
            "type": "object",
            "required": ["rows", "cols", "cell", "safe_box", "envelope_px"],
            "properties": {
                "rows": {"type": "integer", "minimum": 1},
                "cols": {"type": "integer", "minimum": 1},
                "empty_cells": {"type": "integer", "minimum": 0},
                "cell": POSITIVE_PAIR, "safe_box": POSITIVE_PAIR, "envelope_px": POSITIVE_PAIR,
                "divides_exactly": {"type": "boolean"},
            },
        },
        "candidates": {"type": "array", "items": {"type": "object", "required": ["aspect", "rows", "cols"]}},
        "cells": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["cell", "box", "safe_box", "ground_y", "root_x"],
                "properties": {
                    "cell": CELL_RC,
                    "box": {"$ref": "common.schema.json#/$defs/box"},
                    "safe_box": {"$ref": "common.schema.json#/$defs/box"},
                    "gutter_px": {"type": "integer", "minimum": 1},
                    "ground_y": {"type": "number"}, "root_x": {"type": "number"},
                    "used": {"type": "boolean"}, "phase": {"type": "string"},
                    "pose_bounds": {"$ref": "common.schema.json#/$defs/box"},
                    "inside_safe_box": {"type": "boolean"},
                    "toe_ahead": {"enum": ["near", "far"]},
                },
            },
        },
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "name", "half", "near", "far", "contact_leg", "ground"],
                "properties": {
                    "index": {"type": "integer", "minimum": 0}, "name": {"type": "string", "minLength": 1},
                    "half": {"enum": [0, 1]},
                    "near": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                    "far": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                    "lift": {"type": "number"}, "contact_leg": {"enum": ["near", "far"]},
                    "ground": {"type": "object", "required": ["near", "far"],
                               "properties": {"near": {"type": "boolean"}, "far": {"type": "boolean"}}},
                },
            },
        },
        "prompt": {"$ref": "common.schema.json#/$defs/relPath"},
        "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
        "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
    },
}
# Optional properties the B03 producers write (objects are open; these document them).
SHEET_QC_V1_PROPERTIES = {
    "image": {"type": "object", "description": "spill: input facts (sha256, size, source mode, bit depth, "
                                               "conversion) and the key used for an opaque chroma sheet"},
    "input": {"type": "object", "description": "frames: input facts and the ownership slicing record"},
    "params": {"type": "object"},
    "crossing_components": {"type": "array", "items": {
        "type": "object", "required": ["label", "area", "bbox", "owner_cell", "pixels_over_line", "overhang_px"],
        "properties": {"bbox": {"$ref": "common.schema.json#/$defs/box"}, "owner_cell": CELL_RC,
                       "pixels_over_line": {"type": "integer", "minimum": 0},
                       "overhang_px": {"type": "integer", "minimum": 0},
                       "pixels_per_cell": {"type": "object", "additionalProperties": {"type": "integer"}}}}},
    "visible_crossing_components": {"type": "array", "items": {"type": "object"}},
    "boundary_band": {"type": "array", "items": {
        "type": "object", "required": ["axis", "at", "solid_px"],
        "properties": {"axis": {"enum": ["x", "y"]}, "at": {"type": "integer"},
                       "solid_px": {"type": "integer", "minimum": 0}}}},
    "specks": {"type": "object", "properties": {"floor_px": {"type": "integer", "minimum": 0},
                                                "detached_px": {"type": "integer", "minimum": 0},
                                                "detached_components": {"type": "integer", "minimum": 0}}},
    "identity": {"type": "object"},
    "near_duplicates": {"type": "object"},
    "half_cycle": {"type": ["object", "null"]},
    "alternation": {"type": "object", "properties": {
        "verdict": {"type": "string"}, "near_leading": {"type": "integer", "minimum": 0},
        "far_leading": {"type": "integer", "minimum": 0}, "unclear": {"type": "integer", "minimum": 0}}},
    "torso": {"type": "object"},
    "row_baselines": {"type": "object", "additionalProperties": {"type": "integer"}},
    "seam_ranking": {"type": "array", "items": {
        "type": "object", "required": ["from", "to", "mae", "over_median"],
        "properties": {"from": {"type": "integer", "minimum": 0}, "to": {"type": "integer", "minimum": 0},
                       "mae": {"type": "number", "minimum": 0}, "over_median": {"type": "number", "minimum": 0}}}},
    "seam": {"anyOf": [{"$ref": "common.schema.json#/$defs/seamReport"}, {"type": "null"}]},
    "cells": {"type": "array", "items": {"type": "object", "properties": {
        "cell": {"anyOf": [CELL_RC, {"type": "null"}]}, "index": {"type": "integer", "minimum": 0},
        "frame": {"type": "integer", "minimum": 0}, "box": {"$ref": "common.schema.json#/$defs/box"},
        "pixels_over_line": {"type": "integer", "minimum": 0}, "overhang_px": {"type": "integer", "minimum": 0},
        "lead": {"type": "object", "properties": {"lead": {"enum": ["near", "far", "unclear"]}}}}}},
}
SCALE_FRAMES_V1_PROPERTIES = {
    "scale_from": {"type": "string", "description": "neutral, union, profile, or the fixed scale as a fraction"},
    "target_body_px": {"type": ["number", "null"]},
    "base_canvas": {"anyOf": [{"$ref": "common.schema.json#/$defs/size2"}, {"type": "null"}]},
    "base_anchor_px": {"anyOf": [{"$ref": "common.schema.json#/$defs/point2"}, {"type": "null"}]},
    "needed_padding": {"anyOf": [{"$ref": "common.schema.json#/$defs/padding4"}, {"type": "null"}]},
    "margin": {"type": ["integer", "null"], "minimum": 0},
    "anchor_mode": {"enum": ["stance", "feet", "bbox"]},
    "source_anchor": {"$ref": "common.schema.json#/$defs/point2"},
    "reference_frame": {"type": "integer", "minimum": 0},
    "lock": {"type": "array", "items": {"enum": ["feet", "x", "hip"]}},
    "row_baseline": {"type": "boolean"},
    "shift_quantum": {"type": "integer", "minimum": 1},
    "alpha_threshold": {"type": "integer", "minimum": 0, "maximum": 254},
    "frames_per_row": {"type": "integer", "minimum": 1},
    "torso": {"type": "object"},
    "transitions": {"type": "object", "required": ["pairs", "before", "after"]},
    "seam": {"anyOf": [{"type": "null"}, {"type": "object", "required": ["before", "after"], "properties": {
        "before": {"$ref": "common.schema.json#/$defs/seamReport"},
        "after": {"$ref": "common.schema.json#/$defs/seamReport"}}}]},
    "input": {"type": "object"},
    "profile": {"anyOf": [{"$ref": "common.schema.json#/$defs/fileRef"}, {"type": "null"}]},
    "clips": {"$ref": "common.schema.json#/$defs/relPath"},
    "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
    "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
}
SCALE_FRAMES_V1_FRAME_PROPERTIES = {
    "cell": {"anyOf": [CELL_RC, {"type": "null"}]},
    "output_bbox": {"$ref": "common.schema.json#/$defs/box"},
    "turn_slide_px": {"type": "number", "minimum": 0},
    "turn_slide_output_px": {"type": ["number", "null"], "minimum": 0},
    "measure": {"type": "object"},
}


def requested_validator(name: str):
    """Validator for sprite/<name> with this module's schema requests applied in memory (a no-op once the
    integration pass has merged them into shared/schemas)."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    folder = SKILLS_DIR / SKILL / "references" / "schemas"
    common = json.loads((folder / "common.schema.json").read_text(encoding="utf-8"))
    sprite = json.loads((folder / "sprite.schema.json").read_text(encoding="utf-8"))
    defs = sprite["$defs"]
    defs.setdefault("sheet_plan_v1", copy.deepcopy(SHEET_PLAN_V1))
    for key, value in SHEET_QC_V1_PROPERTIES.items():
        defs["sheet_qc_v1"]["properties"].setdefault(key, copy.deepcopy(value))
    for key, value in SCALE_FRAMES_V1_PROPERTIES.items():
        defs["scale_frames_v1"]["properties"].setdefault(key, copy.deepcopy(value))
    frame = defs["scale_frames_v1"]["properties"]["frames"]["items"]["properties"]
    for key, value in SCALE_FRAMES_V1_FRAME_PROPERTIES.items():
        frame.setdefault(key, copy.deepcopy(value))
    registry = Registry().with_resources([(common["$id"], DRAFT202012.create_resource(common)),
                                          (sprite["$id"], DRAFT202012.create_resource(sprite))])
    return Draft202012Validator({"$ref": f"{sprite['$id']}#/$defs/{name}"}, registry=registry)


def assert_requested_contract(document: dict, name: str) -> None:
    errors = [f"{error.json_path}: {error.message}" for error in requested_validator(name).iter_errors(document)]
    assert not errors, "\n".join(errors)


# --------------------------------------------------------------------------- helpers

def run(*args) -> dict:
    result = run_cli([SCRIPT, *map(str, args)])
    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii()
    return json.loads(result.stdout)


def plan_of(summary: dict) -> dict:
    return json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- CLI conventions

def test_help_works_under_cp1252():
    assert_cli_help(SKILL, "plan_guide")


def test_refuses_an_existing_output_dir(tmp_path):
    existing = tmp_path / "guide"
    existing.mkdir()
    result = run_cli([SCRIPT, "--frames", "8", "--output-dir", existing])
    assert result.returncode == 1 and result.stderr.startswith("error:")
    assert list(existing.iterdir()) == []


def test_self_check_failure_publishes_nothing(tmp_path):
    out = tmp_path / "guide"
    result = run_cli([SCRIPT, "--frames", "8", "--cycle", "run", "--min-envelope-px", "5000", "--output-dir", out])
    assert result.returncode == 1
    assert "self-check failed" in result.stderr and "min_envelope" in result.stderr
    assert "Traceback" not in result.stderr
    assert not out.exists() and not any(".stage-" in child.name for child in tmp_path.iterdir())


# --------------------------------------------------------------------------- B03-T4 acceptance

@pytest.mark.parametrize("aspect, size", [("3:2", (1536, 1024)), ("16:9", (1672, 941)), ("1:1", (1254, 1254)),
                                          ("2:1", (1774, 887)), ("2:3", (1024, 1536))])
def test_predicts_the_host_image_size(aspect, size):
    """B03-T4 acceptance: aspect kept, area about 1,572,864 px."""
    assert pg.predict_size(pg.parse_aspect(aspect)) == size
    width, height = size
    assert abs(width * height - pg.HOST_BUDGET_PX) / pg.HOST_BUDGET_PX < 0.001


@pytest.mark.parametrize("cycle, frames", [("run", 8), ("walk", 6), ("run", 12)])
def test_self_check_passes_the_alternation_test(tmp_path, cycle, frames):
    """B03-T4 acceptance: sheet_qc's leading-leg test, run on the guide's own skeleton, alternates."""
    summary = run("--frames", frames, "--cycle", cycle, "--output-dir", tmp_path / "guide")
    plan = plan_of(summary)
    checks = {item["id"]: item for item in plan["qa"]["checks"]}
    assert checks["leg_alternation"]["status"] == "pass" and checks["leg_alternation"]["verdict"] == "alternates"
    assert checks["skeleton_in_safe_box"]["status"] == "pass" and checks["ground_contact"]["status"] == "pass"
    assert plan["qa"]["status"] == "pass" and summary["status"] == "pass"
    half = frames // 2
    for index in range(half):
        first, second = plan["phases"][index], plan["phases"][index + half]
        assert first["near"] == second["far"] and first["far"] == second["near"]
        assert first["contact_leg"] == "near" and second["contact_leg"] == "far"
    assert_requested_contract(plan, "sheet_plan_v1")


def test_guide_outputs_layout_and_prompt(tmp_path):
    summary = run("--frames", 8, "--cycle", "run", "--aspect", "3:2", "--output-dir", tmp_path / "guide")
    folder = tmp_path / "guide"
    plan = plan_of(summary)
    assert summary["predicted_size"] == [1536, 1024] and summary["grid"] == [2, 4]
    assert plan["layout"]["divides_exactly"] is True and plan["layout"]["cell"] == [384.0, 512.0]
    with Image.open(folder / "guide.png") as guide:
        assert guide.size == (1536, 1024)
        pixels = np.asarray(guide.convert("RGB"))
    assert (pixels == pg.NEAR).all(axis=-1).any() and (pixels == pg.FAR).all(axis=-1).any()
    assert (pixels[0:8, :] == pg.GUTTER).all()  # the gutter band along the top edge stays empty and grey
    svg = ElementTree.parse(folder / "guide.svg").getroot()
    assert svg.get("width") == "1536" and len(svg.findall("{http://www.w3.org/2000/svg}polyline")) == 8 * 5
    prompt = (folder / "prompt.txt").read_text(encoding="ascii")
    assert prompt.startswith("Request aspect 3:2. Do not state pixel sizes")
    assert "NEAR" in prompt and "FAR" in prompt and "left leg" not in prompt.lower()
    assert "Frames 4-7 must not repeat frames 0-3" in prompt
    outputs = {item["path"] for item in plan["qa"]["outputs"]}
    assert outputs == {"guide.png", "guide.svg", "guide-annotated.png", "prompt.txt"}


def test_layout_planning_prefers_the_largest_envelope_and_respects_a_forced_grid(tmp_path):
    layouts = pg.candidate_layouts(8, list(pg.STANDARD_ASPECTS), envelope_aspect=1.0, safe=0.15)
    heights = [item["envelope_px"][1] for item in layouts]
    assert heights == sorted(heights, reverse=True)
    assert all(8 <= item["rows"] * item["cols"] <= 9 for item in layouts)
    forced = plan_of(run("--frames", 8, "--aspect", "1:1", "--rows", 3, "--cols", 3, "--output-dir", tmp_path / "g"))
    assert (forced["aspect"], forced["layout"]["rows"], forced["layout"]["cols"]) == ("1:1", 3, 3)
    assert forced["layout"]["empty_cells"] == 1 and forced["cells"][8]["used"] is False


def test_layout_only_guide_and_input_errors(tmp_path):
    plan = plan_of(run("--frames", 4, "--output-dir", tmp_path / "layout"))
    assert plan["phases"] == [] and {item["id"]: item for item in plan["qa"]["checks"]}["leg_alternation"][
        "status"] == "skipped"
    for args, message in ((["--frames", "7", "--cycle", "run"], "even number"),
                          (["--frames", "8", "--aspect", "3x2"], "3:2"),
                          (["--frames", "8", "--rows", "2"], "go together"),
                          (["--frames", "8", "--gutter", "0.3"], "--gutter")):
        result = run_cli([SCRIPT, *args, "--output-dir", tmp_path / "bad"])
        assert result.returncode == 1 and message in result.stderr, (args, result.stderr)
        assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------- schema requests

def test_requested_schema_additions_accept_every_b03_producer(tmp_path):
    """handoff section 5: the new sheet_plan_v1 and the documented optional fields of sheet_qc_v1 and
    scale_frames_v1 accept what plan_guide.py, sheet_qc.py and scale_frames.py write."""
    from forge_testutils import make_magenta_sheet

    plan = plan_of(run("--frames", 8, "--cycle", "run", "--output-dir", tmp_path / "plan"))
    assert_requested_contract(plan, "sheet_plan_v1")
    broken = copy.deepcopy(plan)
    broken["aspect"] = "wide"
    assert any("aspect" in error.json_path for error in requested_validator("sheet_plan_v1").iter_errors(broken))

    sheet = tmp_path / "sheet.png"
    make_magenta_sheet(2, 2, 64, spill_px=6).save(sheet)
    spill = run_cli([script_path(SKILL, "sheet_qc"), "spill", "--input", sheet, "--rows", "2", "--cols", "2", "--output-dir",
                     tmp_path / "spill"])
    assert spill.returncode == 0, spill.stderr
    assert_requested_contract(json.loads((tmp_path / "spill" / "sheet-qc.json").read_text(encoding="utf-8")),
                              "sheet_qc_v1")
    frames = run_cli([script_path(SKILL, "sheet_qc"), "frames", "--sheet", sheet, "--rows", "2", "--cols", "2", "--cycle", "walk",
                      "--output-dir", tmp_path / "frames"])
    assert frames.returncode == 0, frames.stderr
    assert_requested_contract(json.loads((tmp_path / "frames" / "sheet-qc.json").read_text(encoding="utf-8")),
                              "sheet_qc_v1")
    scaled = run_cli([script_path(SKILL, "scale_frames"), "--sheet", sheet, "--rows", "2", "--cols", "2",
                      "--scale-from", "1/2", "--resampler", "box", "--root-lock", "stance", "--emit-clips",
                      "--output-dir", tmp_path / "scaled"])
    assert scaled.returncode == 0, scaled.stderr
    assert_requested_contract(json.loads((tmp_path / "scaled" / "scale-frames.json").read_text(encoding="utf-8")),
                              "scale_frames_v1")


# --------------------------------------------------------------------------- B03-T5 docs

FRANCHISES = ("pokemon", "pok" + chr(0xE9) + "mon", "digimon", "final fantasy", "octopath", "zelda", "mario", "sonic the",
              "street fighter", "castlevania", "chrono trigger", "kingdom rush", "mega man", "megaman", "metroid",
              "kirby", "undertale", "stardew", "hollow knight", "celeste", "fire emblem", "advance wars",
              "dragon quest", "earthbound", "terraria", "dota", "league of legends", "bloons", "plants vs")


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: path.name)
def test_prompt_docs_single_line_commands_no_franchise_names_no_16_bit_default(doc):
    """B03-T5 acceptance: single-line commands, no '16-bit' default, no franchise names (DOC-16)."""
    text = doc.read_text(encoding="utf-8")
    lowered = text.lower()
    for name in FRANCHISES:
        assert not re.search(rf"\b{re.escape(name)}\b", lowered), f"{doc.name} names {name!r}"
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.rstrip()
        assert not stripped.endswith("\\") and not stripped.endswith(" ^") and not stripped.endswith(" `"), (
            f"{doc.name}:{number} continues a command on the next line")
        if "16-bit" in line.lower():
            assert re.search(r"only|unless|asks|requested", line, re.I), f"{doc.name}:{number}: {line}"
        if re.match(r"\s*python ", line):
            assert re.match(r'\s*python "(<skill-dir>|\$\{CLAUDE_SKILL_DIR\})/scripts/[a-z0-9_]+\.py"', line), (
                f"{doc.name}:{number}: {line}")
    for target in re.findall(r"\]\(([^\s)]+)\)", text):
        if "://" not in target and not target.startswith("#"):
            assert (doc.parent / target.split("#", 1)[0]).exists(), f"{doc.name} links to missing {target}"


def test_action_recipes_yaml_presets_parse():
    text = (REFERENCES / "action-recipes.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.S)
    presets = {}
    for block in blocks:
        data = yaml.safe_load(block)
        assert isinstance(data, dict) and len(data) == 1
        presets.update(data)
    assert {"jrpg_walker", "platform_hero", "iso_tactics_unit", "td_tower"} <= set(presets)
    for name, preset in presets.items():
        for key in ("camera", "logical_canvas", "palette", "actions", "pipeline"):
            assert key in preset, f"{name} lacks {key}"
        assert "16-bit" not in json.dumps(preset).lower()
