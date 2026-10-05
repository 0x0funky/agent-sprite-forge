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

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, contract_validator, load_script,
                             run_cli, script_path)

SKILL = "generate2dsprite"
SCRIPT = script_path(SKILL, "plan_guide")
pg = load_script(SKILL, "plan_guide")
sq = load_script(SKILL, "sheet_qc")
REFERENCES = SKILLS_DIR / SKILL / "references"
DOCS = [REFERENCES / "prompt-rules.md", REFERENCES / "action-recipes.md"]

# --------------------------------------------------------------------------- schema requests (handoff section 5)
# Integrated into shared/schemas/sprite.schema.json (sheet_plan_v1, and the typed sheet_qc_v1 and scale_frames_v1
# fields, including the typed sheet_qc_v1 cells); validated against the vendored copy.

def requested_validator(name: str):
    """Validator for the vendored sprite/<name>, which holds this module's schema requests."""
    return contract_validator("sprite", name, skill=SKILL)


def assert_requested_contract(document: dict, name: str) -> None:
    assert_valid_contract(document, "sprite", name, skill=SKILL)


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


def test_huge_counts_fail_fast_and_the_bounded_search_finds_every_grid(tmp_path):
    """r2-conventions finding 13: --frames 99999999 spun at 100% CPU for minutes, because the layout search
    visited every rows x cols pair (O(frames^2)). Grids hold at most MAX_CELLS cells, the search visits only the
    columns that fit, and huge counts fail at once with one error line (a hang hits the 30 s timeout here)."""
    for args, message in ((["--frames", "99999999"], "--frames must be at most 1024"),
                          (["--frames", "1025"], "--frames must be at most 1024"),
                          (["--frames", "8", "--rows", "99999", "--cols", "99999"], "at most 1024 cells"),
                          (["--frames", "8", "--rows", "-2", "--cols", "-4"], "must be positive"),
                          (["--frames", "8", "--budget", "10000000000"], "--budget")):
        result = run_cli([SCRIPT, *args, "--output-dir", tmp_path / "bad"], timeout=30)
        assert result.returncode == 1 and message in result.stderr, (args, result.stderr)
        assert result.stderr.startswith("error:") and not (tmp_path / "bad").exists(), (args, result.stderr)
    loose = plan_of(run("--frames", 8, "--max-empty", 99999999, "--output-dir", tmp_path / "loose"))
    assert (loose["layout"]["rows"], loose["layout"]["cols"]) == (2, 4)
    widest = pg.candidate_layouts(pg.MAX_CELLS, ["1:1"], envelope_aspect=1.0, safe=0.15, max_empty=10 ** 9)
    assert widest[0]["rows"] * widest[0]["cols"] == pg.MAX_CELLS == 1024
    assert all(item["rows"] * item["cols"] == pg.MAX_CELLS for item in widest)
    for frames in (1, 7, 8, 9, 16):  # the same grids as the old full search, which tried every rows x cols pair
        for max_empty in (0, 1, 3):
            found = {(item["aspect"], item["rows"], item["cols"])
                     for item in pg.candidate_layouts(frames, list(pg.STANDARD_ASPECTS), envelope_aspect=1.0,
                                                      safe=0.15, max_empty=max_empty)}
            full = {(aspect, rows, cols) for aspect in pg.STANDARD_ASPECTS
                    for rows in range(1, frames + max_empty + 1) for cols in range(1, frames + max_empty + 1)
                    if frames <= rows * cols <= frames + max_empty}
            assert found == full, (frames, max_empty)


def test_layout_only_guide_and_input_errors(tmp_path):
    plan = plan_of(run("--frames", 4, "--output-dir", tmp_path / "layout"))
    assert plan["phases"] == [] and {item["id"]: item for item in plan["qa"]["checks"]}["leg_alternation"][
        "status"] == "skipped"
    # D26: argparse usage errors exit 2 (usage: ... error: ...); other refusals print one error line, exit 1.
    for args, message, code in ((["--frames", "7", "--cycle", "run"], "even number", 1),
                                (["--frames", "8", "--aspect", "3x2"], "3:2", 2),
                                (["--frames", "8", "--rows", "2"], "go together", 1),
                                (["--frames", "8", "--gutter", "0.3"], "--gutter", 1)):
        result = run_cli([SCRIPT, *args, "--output-dir", tmp_path / "bad"])
        assert result.returncode == code and message in result.stderr, (args, result.stderr)
        assert result.stderr.startswith("usage:" if code == 2 else "error:"), result.stderr
        assert "Traceback" not in result.stderr and not (tmp_path / "bad").exists()


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
    assert spill.returncode == 1 and json.loads(spill.stdout)["status"] == "fail", spill.stderr  # D26: spill found
    spill_report = json.loads((tmp_path / "spill" / "sheet-qc.json").read_text(encoding="utf-8"))
    assert_requested_contract(spill_report, "sheet_qc_v1")
    if spill_report.get("cells"):  # the typed cells replacement of section 5.2 is the integrated rule
        broken = copy.deepcopy(spill_report)
        broken["cells"][0]["cell"] = "0,0"
        assert any("cells" in error.json_path for error in requested_validator("sheet_qc_v1").iter_errors(broken))
    frames = run_cli([script_path(SKILL, "sheet_qc"), "frames", "--sheet", sheet, "--rows", "2", "--cols", "2", "--cycle", "walk",
                      "--output-dir", tmp_path / "frames"])
    assert frames.returncode == (1 if json.loads(frames.stdout)["status"] == "fail" else 0), frames.stderr
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
